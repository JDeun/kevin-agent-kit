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


class Busy(Exception):
    pass


class Retry(Exception):
    pass


class Cancelled(Exception):
    pass


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


_CAPACITY = re.compile(
    r"rate.limit|too many requests|temporarily unavailable|capacity|quota|usage limit|\b429\b",
    re.I,
)

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


def run(argv, cwd, cancel, timeout, *, prompt=None, env=None):
    if cancel.is_set():
        raise Cancelled
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, \
            tempfile.TemporaryFile() as stderr:
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
                code = wait_for_status(read_fd, cancel, timeout)
            finally:
                stop_group(child)
        finally:
            os.close(read_fd)
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read().decode("utf-8", errors="replace")
        if code:
            error = stderr.read().decode("utf-8", errors="replace")
            if code == 75 or _CAPACITY.search(error + "\n" + output):
                raise Retry(f"{argv[0]} reported a temporary capacity error")
            raise ValueError(f"{argv[0]} exited with status {code}: {error.strip()[-500:]}")
        return output


def wait_for_status(fd, cancel, timeout):
    deadline = time.monotonic() + timeout
    while not cancel.is_set():
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
