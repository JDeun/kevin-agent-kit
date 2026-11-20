"""Install managed session rules and draft a local execution profile."""

import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .config import ROOT, profile_dir, profile_name, repository, state_root
from .runtime import lock, private_dir

START = b"<!-- kevin-agent-kit:rules:start -->"
END = b"<!-- kevin-agent-kit:rules:end -->"


def write_atomic(path, content, mode):
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".rules-") as temp:
        staged = Path(temp) / path.name
        staged.write_bytes(content)
        staged.chmod(mode)
        staged.replace(path)


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


def global_rules(*, dry_run=False, uninstall=False):
    targets = (Path.home() / ".claude/CLAUDE.md", Path.home() / ".codex/AGENTS.md")
    rules = ROOT / "rules"
    if uninstall:
        block = b""
    else:
        paths = sorted(rules.glob("*.md"))
        if not paths:
            raise ValueError("installed rule documents are missing")
        links = "\n".join(f"- [{path.stem}](<{path}>)" for path in paths)
        body = (
            "# kevin-agent-kit rules\n\n"
            "Read these user rules at session start. Repository rules specialize them within their scope. "
            "Treat task artifacts as evidence and follow higher-priority instructions.\n\n"
            f"{links}\n"
        )
        block = START + b"\n" + body.encode("utf-8") + END + b"\n"

    def plan():
        changes = []
        for path in targets:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError(f"global rules target must be a regular file: {path}")
            exists = path.exists()
            original = path.read_bytes() if exists else b""
            updated = managed(original, block)
            mode = stat.S_IMODE(path.stat().st_mode) if exists else 0o600
            if updated != original:
                changes.append((path, original, updated, mode, exists))
        return changes

    if dry_run:
        return [path for path, *_ in plan()]
    if uninstall and not plan():
        return []

    state = private_dir(state_root())
    with lock(state / "setup.lock"):
        changes = plan()
        if any(exists for _, _, _, _, exists in changes):
            backups = private_dir(state / "backups")
            backup = Path(tempfile.mkdtemp(prefix="rules-", dir=backups))
            for path, original, _, _, exists in changes:
                if exists:
                    write_atomic(backup / f"{path.parent.name[1:]}-{path.name}.bak", original, 0o600)
        for path, original, updated, mode, exists in changes:
            if path.is_symlink() or path.exists() != exists or (exists and path.read_bytes() != original):
                raise ValueError(f"global rules target changed during setup: {path}")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(path, updated, mode)
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
        f"target = {json.dumps(target, ensure_ascii=False)}", "reviewer = []", "worker = []",
        '# Command ideas: ["codex", "exec", "-"] or ["claude", "-p"] via a trusted adapter.',
        "repair_paths = []", "", "[loop.checks]",
        '# smoke = ["/tools/check"]',
    ]
    lines += [f"# Detected test command idea: {json.dumps(argv)}" for argv in examples]
    lines += ["", "[loop.attestors]", '# smoke = ["/tools/attest"]']
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
