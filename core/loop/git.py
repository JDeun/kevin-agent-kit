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

    def run(self, *args, input=None, allowed=(0,), stdin=None, stdout=None):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
             "-c", "core.quotePath=false", *args],
            cwd=self.repo, env=git_env(), input=input,
            stdin=stdin if stdin is not None or input is not None else subprocess.DEVNULL,
            stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="surrogateescape", timeout=90,
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

    def unchanged(self, commit):
        """After a command ran: HEAD and every tracked file are still `commit`."""
        if self.resolve() != commit or self.run("diff", "--quiet", "--ignore-submodules=none", commit,
                                                allowed=(0, 1)).returncode:
            raise Conflict("a command changed the candidate checkout")

    def tree(self, commit):
        return self.run("rev-parse", "--verify", f"{commit}^{{tree}}").stdout.strip()

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
