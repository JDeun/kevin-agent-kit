from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile


class Conflict(ValueError):
    pass


class Changed(Exception):
    pass


class Git:
    def __init__(self, repo, state):
        self.repo = repo
        self.trees = state / "worktrees"

    def run(self, *args, cwd=None, input=None, allowed=(0,)):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd or self.repo,
            input=input, text=True, encoding="utf-8", errors="surrogateescape",
            capture_output=True, timeout=90,
        )
        if result.returncode not in allowed:
            raise Conflict(f"git {args[0]}: {result.stderr.strip() or result.stdout.strip()}")
        return result

    def ref(self, branch):
        self.run("check-ref-format", f"refs/heads/{branch}")
        return f"refs/heads/{branch}"

    def head(self, branch):
        ref = self.ref(branch)
        symbolic = self.run("symbolic-ref", "--quiet", ref, allowed=(0, 1, 128))
        if symbolic.returncode == 0:
            raise Conflict(f"branch {branch} must be a direct Git ref")
        result = self.run("show-ref", "--verify", "--hash", ref, allowed=(0, 128))
        return result.stdout.strip() or None

    def resolve(self, ref, *, cwd=None):
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cwd).stdout.strip()

    def common(self):
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())

    def paths(self, base, sha):
        changed = self.run("diff", "--name-only", "-z", f"{base}...{sha}", "--").stdout
        return changed.rstrip("\0").split("\0") if changed else []

    def ancestor(self, base, head):
        result = self.run("merge-base", "--is-ancestor", base, head, allowed=(0, 1))
        return result.returncode == 0

    def unoccupied(self, branch):
        entries = self.run("worktree", "list", "--porcelain", "-z").stdout.split("\0")
        if f"branch {self.ref(branch)}" in entries:
            raise Conflict(f"branch {branch} is checked out, so it cannot be updated by the loop")

    def publish(self, branch, sha, old, *, verify=None):
        self.unoccupied(branch)
        if self.head(branch) != old:
            raise Changed
        command = "start\n"
        if verify:
            source, expected = verify
            if self.head(source) != expected:
                raise Changed
            command += f"option no-deref\nverify {self.ref(source)} {expected}\n"
        command += f"option no-deref\nupdate {self.ref(branch)} {sha} {old}\nprepare\ncommit\n"
        try:
            self.run("update-ref", "--stdin", input=command)
        except Conflict:
            if self.head(branch) != old or (verify and self.head(source) != expected):
                raise Changed from None
            raise

    @contextmanager
    def checkout(self, sha):
        self.trees.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="job-", dir=self.trees))
        try:
            self.run("worktree", "add", "--detach", "--quiet", str(path), sha)
        except Conflict:
            if not any(path.iterdir()):
                path.rmdir()
            raise
        try:
            yield path
        finally:
            try:
                self.run("worktree", "remove", str(path))
            except Conflict as error:
                raise Conflict(f"worktree preserved at {path}: {error}") from error

    def candidate(self, path, sha):
        result = self.run("merge", "--no-edit", "--no-ff", sha, cwd=path, allowed=(0, 1, 128))
        if result.returncode:
            self.run("merge", "--abort", cwd=path, allowed=(0, 128))
            raise Conflict(f"cannot build merge candidate: {result.stderr.strip() or result.stdout.strip()}")
        return self.resolve("HEAD", cwd=path)

    def clean(self, path):
        if self.run("status", "--porcelain", "--untracked-files=all", cwd=path).stdout:
            raise Conflict("command left uncommitted changes")


def job_env(cfg, job, **extra):
    return {
        **os.environ, "AGENT_BRANCH": job["branch"], "AGENT_HEAD": job["sha"],
        "AGENT_TARGET": cfg["target"], "AGENT_MAX_MODEL": cfg["max_model"],
        "AGENT_QUEUE": str(cfg["state"] / "requests.log"), **extra,
    }
