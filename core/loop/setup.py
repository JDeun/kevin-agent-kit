"""Install managed session rules and draft a local execution profile."""

from contextlib import ExitStack
import ctypes
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import uuid4

from .config import ROOT, profile_dir, profile_name, project, repository, state_root
from .paths import directory, directory_at, identity, regular_at
from .runtime import Busy, private_dir

START = b"<!-- kevin-agent-kit:rules:start -->"
END = b"<!-- kevin-agent-kit:rules:end -->"


@dataclass(frozen=True)
class Original:
    content: bytes
    stamp: tuple
    mode: int
    gid: int
    attributes: dict


def read_original(parent, name):
    if parent is None:
        return None
    try:
        with regular_at(parent, name, owner=os.getuid(), single_link=True) as source:
            fd = source.fileno()
            info = os.fstat(fd)
            if not hasattr(os, "listxattr"):
                raise ValueError("updating existing global rules requires filesystem metadata support")
            attributes = {key: os.getxattr(fd, key) for key in os.listxattr(fd)}
            stamp = (*identity(info), info.st_mtime_ns, info.st_ctime_ns)
            return Original(source.read(), stamp, stat.S_IMODE(info.st_mode), info.st_gid, attributes)
    except FileNotFoundError:
        return None


def exchange_files(source, from_name, destination, to_name):
    try:
        exchange = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise ValueError("updating existing global rules requires atomic file exchange") from None
    exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    exchange.restype = ctypes.c_int
    rename_exchange = 2
    if exchange(source, os.fsencode(from_name), destination, os.fsencode(to_name), rename_exchange) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), to_name)


def matches_after_exchange(parent, name, expected):
    try:
        actual = read_original(parent, name)
    except (OSError, ValueError):
        return False
    # Renaming an inode changes ctime. Its identity, mtime, content, and metadata must match.
    return actual is not None and replace(actual, stamp=(*actual.stamp[:-1], expected.stamp[-1])) == expected


def write_atomic(parent, name, content, *, original=None, location=None):
    staged = f".rules-{uuid4().hex}"
    os.mkdir(staged, 0o700, dir_fd=parent)
    retain = False
    try:
        with directory_at(parent, staged, private=True) as stage:
            fd = os.open("new", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage)
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(content)
                    target.flush()
                    if original:
                        preserve_metadata(fd, original)
                    else:
                        os.fchmod(fd, 0o600)
                    os.fsync(fd)
                if location is not None:
                    with directory(location, owner=os.getuid()) as current:
                        if identity(os.fstat(parent)) != identity(os.fstat(current)):
                            raise ValueError(f"global rules directory changed during setup: {location}")
                if read_original(parent, name) != original:
                    raise ValueError(f"global rules target changed during setup: {name}")
                if original is None:
                    os.link("new", name, src_dir_fd=stage, dst_dir_fd=parent, follow_symlinks=False)
                    os.unlink("new", dir_fd=stage)
                else:
                    replacement = read_original(stage, "new")
                    exchange_files(stage, "new", parent, name)
                    retain = True
                    if not matches_after_exchange(stage, "new", original):
                        if matches_after_exchange(parent, name, replacement):
                            exchange_files(stage, "new", parent, name)
                            retain = not matches_after_exchange(stage, "new", replacement)
                        os.fsync(stage)
                        os.fsync(parent)
                        saved = f". Displaced file kept at {location / staged / 'new'}" if retain else ""
                        raise ValueError(f"global rules target changed during setup: {name}{saved}")
                    retain = False
                os.fsync(parent)
            finally:
                if not retain:
                    try:
                        os.unlink("new", dir_fd=stage)
                    except FileNotFoundError:
                        pass
    finally:
        if not retain:
            os.rmdir(staged, dir_fd=parent)


def preserve_metadata(fd, original):
    if os.fstat(fd).st_gid != original.gid:
        os.fchown(fd, -1, original.gid)
    os.fchmod(fd, original.mode)
    for key in set(os.listxattr(fd)) - original.attributes.keys():
        os.removexattr(fd, key)
    for key, value in original.attributes.items():
        os.setxattr(fd, key, value)
    info = os.fstat(fd)
    attributes = {key: os.getxattr(fd, key) for key in os.listxattr(fd)}
    if (stat.S_IMODE(info.st_mode), info.st_gid, attributes) != (original.mode, original.gid, original.attributes):
        raise ValueError("cannot preserve global rules file metadata")


def managed(original, block):
    markers = list(re.finditer(rb"(?m)^(?:" + re.escape(START) + b"|" + re.escape(END) + rb")\r?$", original))
    if not markers:
        return original + b"\n\n" + block if block else original
    if len(markers) != 2 or [m[0].rstrip(b"\r") for m in markers] != [START, END]:
        raise ValueError("malformed managed block")
    begin, end = markers[0].start(), markers[1].end()
    if original[end:end + 1] == b"\n":
        end += 1
    if not block and end == len(original) and original[begin - 2:begin] == b"\n\n":
        begin -= 2
    return original[:begin] + block + original[end:]


def rule_block():
    rules = ROOT / "rules"
    with directory(rules) as source:
        names = sorted(name for name in os.listdir(source) if name.endswith(".md"))
        documents = []
        for name in names:
            with regular_at(source, name) as document:
                documents.append(document.read())
    if not documents:
        raise ValueError("installed rule documents are missing")
    snapshot = b"\n\n".join(documents)
    if not snapshot.endswith(b"\n"):
        snapshot += b"\n"
    snapshot.decode("utf-8")
    if START in snapshot or END in snapshot:
        raise ValueError("installed rules contain a managed block marker")
    metadata = project()
    version = metadata["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][a-zA-Z0-9.+-]*", version):
        raise ValueError("invalid installed rules version")
    digest = hashlib.sha256(snapshot).hexdigest()
    header = f"<!-- {metadata['name']} {version} rules snapshot sha256={digest} -->\n".encode("ascii")
    return START + b"\n" + header + b"\n" + snapshot + END + b"\n"


def global_rules(*, dry_run=False, uninstall=False):
    home = Path.home()
    targets = (home / ".claude/CLAUDE.md", home / ".codex/AGENTS.md")
    block = b"" if uninstall else rule_block()

    def plan(stack, home_fd):
        changes = []
        for path in targets:
            try:
                parent = stack.enter_context(directory_at(home_fd, path.parent.name))
            except FileNotFoundError:
                parent = None
            original = read_original(parent, path.name)
            before = original.content if original else b""
            updated = managed(before, block)
            if updated != before:
                changes.append((path, parent, original, updated))
        return changes

    with ExitStack() as stack:
        home_fd = stack.enter_context(directory(home, owner=os.getuid()))
        initial = plan(stack, home_fd)
        if dry_run or not initial:
            return [path for path, *_ in initial]
    with ExitStack() as stack:
        state = state_root()
        state_fd = stack.enter_context(directory(state, create=True, owner=os.getuid(), private=True))
        lock = stack.enter_context(regular_at(state_fd, "setup.lock", flags=os.O_RDONLY | os.O_CREAT,
                                             owner=os.getuid(), single_link=True))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy from error
        home_fd = stack.enter_context(directory(home, owner=os.getuid()))
        changes = plan(stack, home_fd)
        if any(original for _, _, original, _ in changes):
            backups = stack.enter_context(directory_at(state_fd, "backups", create=True, private=True))
            backup = stack.enter_context(directory_at(backups, f"rules-{uuid4().hex}", create=True, private=True))
            for path, _, original, _ in changes:
                if original:
                    write_atomic(backup, f"{path.parent.name[1:]}-{path.name}.bak", original.content)
        for path, parent, original, updated in changes:
            if parent is None:
                parent = stack.enter_context(directory_at(home_fd, path.parent.name, create=True))
            write_atomic(parent, path.name, updated, original=original, location=path.parent)
    return [path for path, *_ in changes]


def repository_profile(cwd, *, dry_run=False):
    repo, common, git = repository(cwd)
    target = git.run("symbolic-ref", "--short", "refs/remotes/origin/HEAD", allowed=(0, 1, 128)).stdout.strip()
    remote = target.removeprefix("origin/")
    target = next((name for name in (remote, "main", "master", "trunk") if name and git.head(name)), "")
    if not target:
        target = git.run("symbolic-ref", "--short", "HEAD", allowed=(0, 1, 128)).stdout.strip() or "main"
    destination = profile_dir() / profile_name(repo, common)
    if destination.is_relative_to(repo) or destination.is_relative_to(common):
        raise ValueError("trusted profiles must be outside the checkout and Git directory")
    examples = []
    if any((repo / name).is_file() for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")) or (repo / "tests").is_dir():
        examples.append(["python3", "-m", "pytest", "-q"])
    for file, argv in (
        ("package.json", ["npm", "test"]), ("Cargo.toml", ["cargo", "test"]),
        ("go.mod", ["go", "test", "./..."]), ("Makefile", ["make", "test"]),
    ):
        if (repo / file).is_file():
            examples.append(argv)
    lines = [
        "# Review this user-owned profile before starting the loop.",
        "[loop]", f"repo = {json.dumps(str(repo), ensure_ascii=False)}",
        f"target = {json.dumps(target, ensure_ascii=False)}", "reviewer = []", "review_observer = []",
        'review_key = ""', "worker = []", "repair_paths = []", "",
        "[loop.lens_reviewers]", '# security = ["/tools/security-review"]', "",
        "[loop.checks]", '# smoke = ["/tools/check-evidence"]',
    ]
    lines += [f"# Detected test command idea: {json.dumps(argv)}" for argv in examples]
    lines += ["", "[loop.check_keys]", '# smoke = "/absolute/path/to/check-source.pem"']
    if dry_run:
        if os.path.lexists(destination):
            raise FileExistsError(f"profile already exists: {destination}")
        return destination
    private_dir(destination.parent.parent)
    private_dir(destination.parent)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".profile-") as temp:
        staged = Path(temp) / destination.name
        staged.write_text("\n".join(lines) + "\n", encoding="utf-8")
        staged.chmod(0o600)
        destination.hardlink_to(staged)
    return destination
