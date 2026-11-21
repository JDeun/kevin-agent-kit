import codecs
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time

from .install import EMBEDDED, MODULE_PREFIX


class Busy(Exception):
    pass


class Retry(Exception):
    pass


class CommandFailed(ValueError):
    """A command ran and exited nonzero; the exit code is evidence."""

    def __init__(self, code):
        super().__init__(f"command exited with status {code}")
        self.code = code


class Cancelled(Exception):
    pass


def module_command(root, module, *args):
    start = ("root, module = sys.argv[1:3]\ndel sys.argv[:2]\n"
             f"run_kit(Path(root), module, source={not MODULE_PREFIX})\n")
    return [sys.executable, "-I", "-c", EMBEDDED + start, str(root), module, *args]


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"directory must be private and user-owned: {path}")
    return path


@contextmanager
def lock(path):
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy from error
        yield


@dataclass
class MemoryGate:
    mounts: Path = Path("/sys/fs/cgroup")
    membership: Path = Path("/proc/self/cgroup")
    proc: Path = Path("/proc/meminfo")

    def anon(self):
        try:
            lines = self.membership.read_text().splitlines()
        except OSError:
            lines = []
        group = next((line.split(":", 2)[2] for line in lines if line.startswith("0::")), "")
        paths = (self.mounts / group.lstrip("/") / "memory.stat", self.mounts / "memory.stat")
        for path in dict.fromkeys(paths):
            if not path.resolve().is_relative_to(self.mounts.resolve()):
                continue
            try:
                match = re.search(r"^anon (\d+)$", path.read_text(), re.M)
                if match:
                    return int(match[1])
            except OSError:
                continue
        return None

    def allows(self, max_anon, min_available):
        if not (max_anon or min_available):
            return True
        anon = self.anon()
        if anon is not None and max_anon:
            return anon < max_anon
        if not min_available:
            return max_anon == 0
        try:
            match = re.search(r"^MemAvailable:\s+(\d+) kB\s*$", self.proc.read_text(), re.M)
        except OSError:
            return False
        return bool(match and int(match[1]) * 1024 >= min_available)


_SUPERVISOR = """
import os
import signal
import subprocess
import sys

status = int(sys.argv[1])
try:
    code = subprocess.run(sys.argv[2:], check=False).returncode
except OSError as error:
    print(error, file=sys.stderr)
    code = 127
os.write(status, str(code).encode("ascii"))
os.close(status)
while True:
    signal.pause()
"""


OUTPUT_LIMIT = 1024 * 1024


def readable(text):
    # Literal NUL is illegal even inside a JSON string. A damaged entry stays
    # invalid while complete, well-formed siblings remain readable.
    return re.sub(r"[\udc80-\udcff]", "\0", text)


@contextmanager
def failed_stdout(stream, capture):
    try:
        yield
    except Exception as error:
        if capture:
            # A stop reads the same final bytes as a failure: the supervisor has
            # already ended the process group. Anything cut off or unreadable
            # leaves the caller unable to prove it saw every finding.
            try:
                stream.seek(0)
                raw = stream.read(OUTPUT_LIMIT + 1)
            except (OSError, ValueError):
                error.output_truncated = True
            else:
                error.output_truncated = len(raw) > OUTPUT_LIMIT
                error.output = readable(raw[:OUTPUT_LIMIT].decode("utf-8", errors="surrogateescape"))
        raise


def follow(stream, watch):
    """Return a function that hands newly written output to `watch` while the command runs."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
    offset = 0

    def pump():
        nonlocal offset
        while offset < OUTPUT_LIMIT:
            chunk = os.pread(stream.fileno(), min(2**16, OUTPUT_LIMIT - offset), offset)
            if not chunk:
                break
            offset += len(chunk)
            text = readable(decoder.decode(chunk))
            if text:
                watch(text)

    return pump


def run(argv, cwd, cancel, timeout, *, prompt=None, env=None, capture_failed_output=False, watch=None):
    """Run a supervised command. `watch` receives standard output as text while it is produced."""
    if cancel.is_set():
        raise Cancelled
    with (tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout,
          tempfile.TemporaryFile() as stderr, failed_stdout(stdout, capture_failed_output)):
        pump = follow(stdout, watch) if watch else None
        if prompt is not None:
            stdin.write(prompt.encode("utf-8"))
            stdin.seek(0)
        read_fd, write_fd = os.pipe()
        try:
            try:
                child = subprocess.Popen(
                    [sys.executable, "-I", "-c", _SUPERVISOR, str(write_fd), *argv],
                    cwd=cwd, env=env, stdin=stdin if prompt is not None else subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True, pass_fds=(write_fd,),
                )
            finally:
                os.close(write_fd)
            try:
                code = wait_for_status(read_fd, cancel, timeout, (stdout, stderr), pump)
            finally:
                stop_group(child)
        finally:
            os.close(read_fd)
        stdout.seek(0)
        if code == 75:
            raise Retry("command reported a temporary failure")
        if code:
            raise CommandFailed(code)
        output = stdout.read(OUTPUT_LIMIT + 1)
        if len(output) > OUTPUT_LIMIT or os.fstat(stderr.fileno()).st_size > OUTPUT_LIMIT:
            raise ValueError("command output exceeded 1 MiB")
        try:
            return output.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError("command output was not UTF-8") from None


def wait_for_status(fd, cancel, timeout, outputs, pump=None):
    deadline = time.monotonic() + timeout
    while not cancel.is_set():
        if pump:
            pump()
        if any(os.fstat(stream.fileno()).st_size > OUTPUT_LIMIT for stream in outputs):
            raise ValueError("command output exceeded 1 MiB")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Retry("command timed out")
        readable, _, _ = select.select([fd], [], [], min(remaining, 0.05))
        if readable:
            result = os.read(fd, 64)
            if not result:
                raise ValueError("command supervisor exited before reporting a status")
            return int(result)
    raise Cancelled


def stop_group(child):
    # The supervisor stays waitable until after signaling, so its PGID cannot be reused.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            time.sleep(0.05)
    child.wait()
