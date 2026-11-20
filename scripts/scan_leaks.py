import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


class Audit:
    def __init__(self, terms):
        self.terms = terms
        self.hits = 0

    def check(self, label, value):
        normalized = value.decode("utf-8", errors="replace").casefold()
        found = sum(term in normalized for term in self.terms)
        if found:
            print(f"match in {label}: {found} rule(s)")
            self.hits += found
        return bool(found)

    def worktree(self):
        for base, dirs, files in os.walk(ROOT, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for name in sorted(set(dirs) | set(files)):
                if name == ".git":
                    continue
                path = Path(base) / name
                label = f"working tree entry {self.checked}"
                self.checked += 1
                relative = path.relative_to(ROOT)
                if not self.check(label + " path", os.fsencode(relative)):
                    label = f"working tree {str(relative)!r}"
                if path.is_symlink():
                    self.check(label + " symlink", os.fsencode(os.readlink(path)))
                elif path.is_file():
                    self.check(label, path.read_bytes())

    def history(self):
        objects = {}
        for index, refname in enumerate(git("for-each-ref", "--format=%(refname)").splitlines()):
            self.check(f"ref {index}", refname)
        for row in git("rev-list", "--objects", "--all").splitlines():
            oid, _, _ = row.partition(b" ")
            objects[oid] = None
        trees = {line.decode("ascii") for line in git("log", "--all", "--format=%T").splitlines()}
        for tree in trees:
            for index, entry in enumerate(git("ls-tree", "-r", "-t", "-z", tree).split(b"\0")):
                if entry:
                    _, _, name = entry.partition(b"\t")
                    self.check(f"history path {tree[:12]}/{index}", name)
        for row in git("ls-files", "-sz").split(b"\0"):
            if row:
                detail, _, name = row.partition(b"\t")
                mode, oid, _ = detail.split()
                if mode != b"160000":
                    objects[oid] = b"blob"
                self.check(f"index path {oid.decode('ascii')[:12]}", name)
        for oid, known in objects.items():
            ref = oid.decode("ascii")
            kind = known or git("cat-file", "-t", ref).strip()
            if kind == b"blob":
                self.check(f"Git blob {ref[:12]}", git("cat-file", "blob", ref))
            elif kind in {b"commit", b"tag"}:
                self.check(f"Git {kind.decode('ascii')} {ref[:12]}",
                           git("cat-file", kind.decode("ascii"), ref))


def main():
    parser = argparse.ArgumentParser(description="Scan project paths, working files, index and reachable history")
    parser.add_argument("denylist", type=Path, nargs="?", default=ROOT.with_name(ROOT.name + ".denylist"))
    args = parser.parse_args()
    try:
        denylist = args.denylist.resolve()
        if denylist.is_relative_to(ROOT):
            raise ValueError("denylist must live outside the project")
        terms = {line.casefold() for line in denylist.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")}
        if not terms:
            raise ValueError("denylist has no entries")
        audit = Audit(terms)
        audit.checked = 0
        audit.worktree()
        audit.history()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"scan failed: {error}", file=sys.stderr)
        return 2
    print(f"Leak scan: {audit.hits} match(es)")
    return int(audit.hits != 0)


if __name__ == "__main__":
    raise SystemExit(main())
