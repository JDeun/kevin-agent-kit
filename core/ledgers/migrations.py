import fnmatch
import os
import re
import subprocess
from pathlib import Path, PurePosixPath


ORDINAL = re.compile(r"^([0-9]+)(?:[_-]|\.)")


def git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or f"git {args[0]} failed") from error


def inventory(repo: Path, commit: str, directory: str, pattern: str) -> list[str]:
    output = git(repo, "ls-tree", "--full-tree", "-rz", "--name-only", commit)
    return [
        name
        for name in map(os.fsdecode, output.split(b"\0"))
        if name
        and (directory == "." or name.startswith(directory + "/"))
        and fnmatch.fnmatchcase(PurePosixPath(name).name, pattern)
    ]


def ordinals(paths: list[str], label: str) -> tuple[dict[str, int], list[str]]:
    ids = {}
    seen = {}
    issues = []
    for path in paths:
        match = ORDINAL.match(PurePosixPath(path).name)
        if match is None or int(match[1]) < 1:
            issues.append(f"{label}: {path!r} needs a positive ordinal prefix")
            continue
        ordinal = int(match[1])
        ids[path] = ordinal
        previous = seen.setdefault(ordinal, path)
        if previous != path:
            issues.append(f"{label}: duplicate ordinal {ordinal}: {previous!r}, {path!r}")
    return ids, issues


def check_migrations(repo: Path, target: str, head: str, directory: str, pattern: str,
                     *, immutable: bool = False) -> list[str]:
    scope = PurePosixPath(directory)
    if scope.is_absolute() or ".." in scope.parts:
        raise ValueError("Migration directory must be relative to the repository")
    target_commit = git(repo, "rev-parse", "--verify", "--end-of-options", target + "^{commit}").decode().strip()
    head_commit = git(repo, "rev-parse", "--verify", "--end-of-options", head + "^{commit}").decode().strip()
    merge_base = git(repo, "merge-base", target_commit, head_commit).decode().strip()
    target_ids, target_issues = ordinals(inventory(repo, target_commit, str(scope), pattern), "target")
    head_ids, head_issues = ordinals(inventory(repo, head_commit, str(scope), pattern), "head")
    inherited = set(inventory(repo, merge_base, str(scope), pattern))
    target_max = max(target_ids.values(), default=0)
    too_low = [
        f"head: {path!r} has ordinal {ordinal}, which must exceed target maximum {target_max}"
        for path, ordinal in head_ids.items()
        if path not in inherited and ordinal <= target_max
    ]
    changed = []
    if immutable:
        paths = git(repo, "diff", "--name-only", "--no-renames", "-z", target_commit, head_commit, "--")
        changed = [
            f"head: applied migration {name!r} was modified or deleted"
            for name in map(os.fsdecode, paths.split(b"\0")) if name in target_ids
        ]
    return target_issues + head_issues + too_low + changed
