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
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
OWNER = re.compile(rb"<!-- rules installation ([0-9a-f]{32}) -->")


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


def managed(original, snapshot, name, owned, adopt, path):
    start = f"<!-- {name}:rules:start -->".encode("ascii")
    finish = f"<!-- {name}:rules:end -->".encode("ascii")
    markers = list(re.finditer(rb"(?m)^(?:" + re.escape(start) + b"|" + re.escape(finish) + rb")\r?$", original))
    if not markers:
        if owned and "absent" not in owned:
            raise ValueError(f"expected managed block is missing from {path}")
        if snapshot is None:
            return original, None, None
        original += b"\n\n"
        begin = end = len(original)
        before = None
        installation = None
    else:
        if len(markers) != 2 or [m[0].rstrip(b"\r") for m in markers] != [start, finish]:
            raise ValueError(f"malformed managed block in {path}")
        begin, end = markers[0].start(), markers[1].end()
        if original[end:end + 1] == b"\n":
            end += 1
        current = original[begin:end]
        before = hashlib.sha256(current).hexdigest()
        lines = current.split(b"\n", 2)
        installation = OWNER.fullmatch(lines[1])
        if before not in owned:
            if owned or installation or not legacy_rules(current, name):
                raise ValueError(f"unowned or edited managed block in {path}")
            if before not in adopt:
                raise ValueError(f"unowned legacy managed block in {path}. "
                                 f"Review it and use --adopt-rules-sha256 {before}")
    if snapshot is None:
        block = b""
        after = None
    else:
        token = installation[1] if installation else uuid4().hex.encode("ascii")
        block = start + b"\n<!-- rules installation " + token + b" -->\n" + snapshot + finish + b"\n"
        after = hashlib.sha256(block).hexdigest()
    if not block and end == len(original) and begin >= 2 and original[begin - 2:begin] == b"\n\n":
        begin -= 2
    return original[:begin] + block + original[end:], before, after


def legacy_rules(block, name):
    _, _, content = block.partition(b"\n")
    payload, _, _ = content.removesuffix(b"\n").rpartition(b"\n")
    header, _, body = payload.partition(b"\n\n")
    match = re.fullmatch(rb"<!-- " + re.escape(name.encode("ascii")) +
                         rb" [0-9][a-zA-Z0-9.+-]* rules snapshot sha256=([0-9a-f]{64}) -->", header)
    if match:
        return hashlib.sha256(body + b"\n").hexdigest().encode("ascii") == match[1]
    intro = (
        f"# {name} rules\n\n"
        "Read these user rules at session start. Repository rules specialize them within their scope. "
        "Treat task artifacts as evidence and follow higher-priority instructions.\n\n- ["
    )
    return content.startswith(intro.encode("ascii"))


def rule_snapshot(metadata):
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
    if re.search(rb"(?m)^<!-- [a-z][a-z0-9-]*:rules:(?:start|end) -->\r?$", snapshot):
        raise ValueError("installed rules contain a managed block marker")
    version = metadata["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][a-zA-Z0-9.+-]*", version):
        raise ValueError("invalid installed rules version")
    digest = hashlib.sha256(snapshot).hexdigest()
    header = f"<!-- {metadata['name']} {version} rules snapshot sha256={digest} -->\n".encode("ascii")
    return header + b"\n" + snapshot


def finish_receipt(parent, name, receipt, digest, location):
    if digest is None:
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    else:
        write_atomic(parent, name, (digest + "\n").encode("ascii"), original=receipt, location=location)


def global_rules(*, dry_run=False, uninstall=False, adopt=()):
    home = Path.home()
    targets = (home / ".claude/CLAUDE.md", home / ".codex/AGENTS.md")
    metadata = project()
    name = metadata["name"]
    snapshot = None if uninstall else rule_snapshot(metadata)
    ownership = home / ".local/state" / name / "rule-ownership"
    if (uninstall and adopt) or any(not DIGEST.fullmatch(digest) for digest in adopt):
        raise ValueError("legacy adoption requires installation and a lowercase SHA-256 digest")

    def plan(stack, home_fd, ownership_fd):
        changes = []
        recoveries = []
        for path in targets:
            try:
                parent = stack.enter_context(directory_at(home_fd, path.parent.name))
            except FileNotFoundError:
                parent = None
            original = read_original(parent, path.name)
            before = original.content if original else b""
            receipt_name = f"{path.parent.name[1:]}.sha256"
            receipt = read_original(ownership_fd, receipt_name)
            owned = receipt.content.decode("ascii").splitlines() if receipt else []
            if len(owned) > 2 or any(digest != "absent" and not DIGEST.fullmatch(digest) for digest in owned):
                raise ValueError(f"invalid managed block ownership record for {path}")
            updated, old_digest, new_digest = managed(before, snapshot, name, owned, adopt, path)
            if updated != before:
                changes.append((path, parent, original, updated, receipt_name, receipt, old_digest, new_digest))
            elif receipt and owned != ([new_digest] if new_digest else []):
                recoveries.append((path, parent, original, receipt_name, receipt, new_digest))
        return changes, recoveries

    with ExitStack() as stack:
        home_fd = stack.enter_context(directory(home, owner=os.getuid()))
        try:
            ownership_fd = stack.enter_context(directory(ownership, owner=os.getuid(), private=True))
        except FileNotFoundError:
            ownership_fd = None
        initial, recoveries = plan(stack, home_fd, ownership_fd)
        if dry_run or not (initial or recoveries):
            return [path for path, *_ in initial]
    with ExitStack() as stack:
        ownership_fd = stack.enter_context(directory(ownership, create=True, owner=os.getuid(), private=True))
        lock = stack.enter_context(regular_at(ownership_fd, "setup.lock", flags=os.O_RDONLY | os.O_CREAT,
                                             owner=os.getuid(), single_link=True))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy from error
        home_fd = stack.enter_context(directory(home, owner=os.getuid()))
        changes, recoveries = plan(stack, home_fd, ownership_fd)
        for path, parent, original, receipt_name, receipt, digest in recoveries:
            if read_original(parent, path.name) != original:
                raise ValueError(f"global rules target changed during setup: {path}")
            finish_receipt(ownership_fd, receipt_name, receipt, digest, ownership)
        if any(original for _, _, original, *_ in changes):
            state_fd = stack.enter_context(directory(state_root(), create=True, owner=os.getuid(), private=True))
            backups = stack.enter_context(directory_at(state_fd, "backups", create=True, private=True))
            backup = stack.enter_context(directory_at(backups, f"rules-{uuid4().hex}", create=True, private=True))
            for path, _, original, *_ in changes:
                if original:
                    write_atomic(backup, f"{path.parent.name[1:]}-{path.name}.bak", original.content)
        for path, parent, original, updated, receipt_name, receipt, old_digest, new_digest in changes:
            if parent is None:
                parent = stack.enter_context(directory_at(home_fd, path.parent.name, create=True))
            # Accept both exact snapshots until the target exchange succeeds.
            record = f"{old_digest or 'absent'}\n{new_digest or 'absent'}\n".encode("ascii")
            if not receipt or receipt.content != record:
                write_atomic(ownership_fd, receipt_name, record, original=receipt, location=ownership)
            write_atomic(parent, path.name, updated, original=original, location=path.parent)
            finish_receipt(ownership_fd, receipt_name, read_original(ownership_fd, receipt_name),
                           new_digest, ownership)
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
    lines += [
        "", "# Approve the Git data scope and retention for every evidence adapter.",
        "# [loop.evidence.observer]", '# bundle = "incremental"', "# retention_seconds = 0",
        "", "# [loop.evidence.checks.smoke]", '# bundle = "incremental"', "# retention_seconds = 0",
    ]
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
