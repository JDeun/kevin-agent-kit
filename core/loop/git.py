from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile


class Conflict(ValueError):
    pass


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
        changed = self.run("diff", "--name-only", "--no-renames", "-z", *revs, "--").stdout
        return changed.rstrip("\0").split("\0") if changed else []

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
        self.run("merge", "--no-edit", "--no-ff", head)
        return self.resolve()

    def repair(self, allowed_paths):
        from .review import matches

        self.run("add", "-A", "--", ".")
        paths = self.paths("--cached")
        if not paths:
            raise Conflict("worker left no staged repair changes")
        if any(not any(matches(path, pattern) for pattern in allowed_paths) for path in paths):
            raise Conflict("worker changed a path outside the approved repair scope")
        self.run("commit", "--quiet", "-m", "fix: apply scoped review repairs")
        return self.resolve()

    def bundle(self, path, sha):
        ref = "refs/heads/kevin-candidate"
        self.run("update-ref", ref, sha)
        self.run("bundle", "create", str(path), ref)
