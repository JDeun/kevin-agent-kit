from collections import deque
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class Conflict(ValueError):
    pass


class MergeFailure(Conflict):
    def __init__(self, reason):
        super().__init__(f"candidate merge failed: {reason}")
        self.reason = reason


def install_check_links(root, links):
    # Resolve components with the committed link targets before creating any
    # symlink. Even a chain that would leave the root and re-enter is rejected.
    for link in links:
        current = link.parent
        pending = deque([link.name])
        hops = 0
        while pending:
            if not current.is_dir():
                raise Conflict("unsupported symlink through a non-directory")
            part = pending.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if current == root:
                    raise Conflict("unsupported symlink outside the candidate tree")
                current = current.parent
                continue
            entry = current / part
            if entry not in links:
                current = entry
                continue
            hops += 1
            if hops > 40:
                raise Conflict("unsupported symlink cycle or excessive link depth")
            pending.extendleft(reversed(links[entry].split("/")))
        if not current.exists():
            raise Conflict("unsupported dangling candidate symlink")
    for path, target in links.items():
        path.symlink_to(target)


def git_env():
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
        "GIT_AUTHOR_NAME": "Kit Coordinator", "GIT_COMMITTER_NAME": "Kit Coordinator",
        "GIT_AUTHOR_EMAIL": "kit@example.invalid", "GIT_COMMITTER_EMAIL": "kit@example.invalid",
    }


class Git:
    def __init__(self, repo):
        self.repo = repo

    def run(self, *args, input=None, allowed=(0,), stdin=None, stdout=None, binary=False):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
             "-c", "core.quotePath=false", *args],
            cwd=self.repo, env=git_env(), input=input,
            stdin=stdin if stdin is not None or input is not None else subprocess.DEVNULL,
            stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE, text=not binary, encoding=None if binary else "utf-8",
            errors=None if binary else "surrogateescape", timeout=90,
        )
        if result.returncode not in allowed:
            raise Conflict(f"git {args[0]} failed ({result.returncode})")
        return result

    def ref(self, branch):
        self.run("check-ref-format", f"refs/heads/{branch}")
        return f"refs/heads/{branch}"

    def head(self, branch):
        ref = self.ref(branch)
        if self.run("symbolic-ref", "--quiet", ref, allowed=(0, 1, 128)).returncode == 0:
            raise Conflict(f"branch {branch} must be a direct Git ref")
        result = self.run("show-ref", "--verify", "--hash", ref, allowed=(0, 128))
        return result.stdout.strip() or None

    def resolve(self, ref="HEAD"):
        return self.run("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").stdout.strip()

    def common(self):
        result = self.run("rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(result.stdout.rstrip("\n")).resolve()

    def paths(self, *revs):
        changed = self.run("diff", "--name-only", "--no-renames", "--ignore-submodules=none", "-z", *revs, "--").stdout
        return changed.rstrip("\0").split("\0") if changed else []

    def diff_digest(self, base, candidate):
        with tempfile.TemporaryFile() as patch:
            self.run("diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff",
                     "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
                     "--ignore-submodules=none", "--submodule=short", base, candidate, "--", stdout=patch)
            patch.seek(0)
            return hashlib.file_digest(patch, "sha256").hexdigest()

    def ancestor(self, base, head):
        result = self.run("merge-base", "--is-ancestor", base, head, allowed=(0, 1))
        return result.returncode == 0

    @contextmanager
    def snapshot(self, state, *commits):
        with tempfile.TemporaryDirectory(prefix="job-", dir=state) as temp:
            root = Path(temp)
            work = root / "workspace"
            work.mkdir()
            isolated = Git(work)
            object_format = self.run("rev-parse", "--show-object-format").stdout.strip()
            isolated.run("init", "--quiet", "--template=", f"--object-format={object_format}")
            with tempfile.TemporaryFile(dir=root) as pack:
                self.run("pack-objects", "--revs", "--stdout", input="\n".join(commits) + "\n", stdout=pack)
                pack.seek(0)
                isolated.run("index-pack", "--stdin", stdin=pack)
            yield isolated, root

    def candidate(self, base, head):
        self.run("checkout", "--quiet", "--detach", base)
        try:
            self.run("merge", "--no-edit", "--no-ff", head)
        except subprocess.TimeoutExpired as error:
            raise MergeFailure("timeout") from error
        except Conflict as error:
            unmerged = self.run("ls-files", "-u", "-z").stdout
            raise MergeFailure("conflict" if unmerged else "merge-error") from error
        return self.resolve()

    def pristine(self, commit):
        """The checkout is exactly `commit`: HEAD, tracked files and untracked files all match."""
        if self.resolve() != commit:
            raise Conflict("the checkout is not at the candidate commit")
        if self.run("status", "--porcelain", "-z", "--untracked-files=all").stdout:
            raise Conflict("the candidate checkout has uncommitted or untracked changes")

    def tree(self, commit):
        return self.run("rev-parse", "--verify", f"{commit}^{{tree}}").stdout.strip()

    def write_check_files(self, files):
        # Read objects in one Git invocation. Batch headers report the stored type
        # without peeling tags or applying any working-tree conversion.
        links = {}
        with tempfile.TemporaryFile(dir=self.repo.parent) as blobs:
            self.run("cat-file", "--batch", input=b"".join(oid + b"\n" for oid, _, _ in files),
                     stdout=blobs, binary=True)
            blobs.seek(0)
            for oid, path, mode in files:
                fields = blobs.readline().split()
                if len(fields) != 3 or fields[:2] != [oid, b"blob"]:
                    raise Conflict("unsupported object in candidate check tree")
                remaining = int(fields[2])
                if mode == b"120000":
                    if not 0 < remaining < 4096 or path in links:
                        raise Conflict("unsupported symlink in candidate tree")
                    target = os.fsdecode(blobs.read(remaining))
                    if target.startswith("/") or "\0" in target:
                        raise Conflict("unsupported symlink target in candidate tree")
                    links[path] = target
                else:
                    with path.open("xb") as stream:
                        while remaining:
                            chunk = blobs.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                raise Conflict("incomplete candidate blob")
                            stream.write(chunk)
                            remaining -= len(chunk)
                    path.chmod(0o755 if mode == b"100755" else 0o644)
                if blobs.read(1) != b"\n":
                    raise Conflict("incomplete candidate blob")
        return links

    @contextmanager
    def check_tree(self, tree):
        """Export raw objects into a new private directory for a single read-only mount."""
        entries = self.run("ls-tree", "--full-tree", "-rtz", tree, binary=True).stdout
        with tempfile.TemporaryDirectory(prefix="check-", dir=self.repo.parent) as directory:
            work = Path(directory)
            files = []
            for entry in entries.split(b"\0")[:-1]:
                metadata, name = entry.split(b"\t", 1)
                mode, kind, oid = metadata.split()
                if any(part.lower() in {b"", b".", b"..", b".git"} for part in name.split(b"/")):
                    raise Conflict("unsupported path in candidate check tree")
                path = work / os.fsdecode(name)
                if mode == b"040000" and kind == b"tree":
                    path.mkdir()
                elif mode in {b"100644", b"100755", b"120000"} and kind == b"blob":
                    files.append((oid, path, mode))
                else:
                    raise Conflict(f"unsupported entry mode {os.fsdecode(mode)} in candidate check tree")
            links = self.write_check_files(files)
            install_check_links(work, links)
            yield work

    def repair(self, allowed_paths, expected=None):
        from .review import matches

        if expected is not None and self.resolve() != expected:
            raise Conflict("worker moved HEAD; only staged changes are accepted as a repair")
        self.run("add", "-A", "--", ".")
        paths = self.paths("--cached")
        if not paths:
            raise Conflict("worker left no staged repair changes")
        if any(not any(matches(path, pattern) for pattern in allowed_paths) for path in paths):
            raise Conflict("worker changed a path outside the approved repair scope")
        self.run("commit", "--quiet", "-m", "fix: apply scoped review repairs")
        return self.resolve()

    def bundle(self, path, sha, *prerequisites):
        ref = "refs/heads/kevin-candidate"
        self.run("update-ref", ref, sha)
        if not prerequisites:
            self.run("bundle", "create", str(path), ref)
            return
        with tempfile.TemporaryFile(dir=path.parent) as source:
            self.run("bundle", "create", "-", ref, *(f"^{commit}" for commit in prerequisites), stdout=source)
            source.seek(0)
            with path.open("xb") as target:
                while (line := source.readline()) != b"\n":
                    if not line:
                        raise Conflict("incomplete Git bundle header")
                    # Git includes the excluded commit's subject as an advisory comment.
                    target.write(line.split(b" ", 1)[0] + b" prerequisite\n" if line.startswith(b"-") else line)
                target.write(b"\n")
                shutil.copyfileobj(source, target)
