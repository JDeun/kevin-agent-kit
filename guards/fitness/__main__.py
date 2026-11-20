import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import tomllib

from . import compare, read_counts, relative_path, rows
from .python import scan

if __package__ == "guards.fitness":
    from core.loop.git import Git
else:
    from ...core.loop.git import Git


def contents(root, name):
    path = root / relative_path(name)
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"expected a regular file inside the checkout: {name}")
    return path.read_bytes()


def target_blob(git, commit, name):
    entry = git.run("ls-tree", "-z", "--full-tree", commit, "--", f":(literal){name}").stdout
    if not entry:
        return None
    meta, _, found = entry.rstrip("\0").partition("\t")
    mode, kind, blob = meta.split()
    if found != name or mode not in ("100644", "100755") or kind != "blob":
        raise ValueError(f"{name} must be a regular Git blob in the trusted target")
    return git.run("cat-file", "blob", blob).stdout


def target_counts(root, target, baseline):
    git = Git(root)
    actual_root = Path(git.run("rev-parse", "--show-toplevel").stdout.rstrip("\n")).resolve()
    if root != actual_root:
        raise ValueError("--root must be the Git checkout root when checking a baseline")
    commit = git.resolve(target)
    entry = git.run("ls-tree", "-z", "--full-tree", commit, "--", f":(literal){baseline}").stdout
    if not entry:
        return Counter()
    meta, _, name = entry.rstrip("\0").partition("\t")
    mode, kind, blob = meta.split()
    if name != baseline or mode not in ("100644", "100755") or kind != "blob":
        raise ValueError("the target baseline must be a regular Git blob")
    return read_counts(git.run("cat-file", "blob", blob).stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check architecture fitness and ratchet known violations.")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("scan", "check"):
        command = commands.add_parser(action)
        command.add_argument("--root", type=Path, default=Path("."))
        command.add_argument("--config", help="repository-relative TOML for the Python checker")
        command.add_argument("--findings", action="append", default=[], help="counted JSON from another analyzer")
        if action == "check":
            command.add_argument("--baseline", required=True, help="repository-relative proposed baseline")
            command.add_argument("--target", required=True, help="trusted Git target ref or commit")
            command.add_argument("--policy-from-target", action="store_true",
                                 help="read --config from the trusted target instead of the candidate, "
                                      "so a candidate cannot weaken its own policy")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if not args.config and not args.findings:
            raise ValueError("provide a Python --config or at least one analyzer's --findings")
        observed = Counter()
        if args.config:
            if args.action == "check" and args.policy_from_target:
                git = Git(root)
                text = target_blob(git, git.resolve(args.target), str(relative_path(args.config)))
                if text is None:
                    raise ValueError("the trusted target has no policy file; land the policy first")
                settings = tomllib.loads(text)
            else:
                settings = tomllib.loads(contents(root, args.config).decode("utf-8"))
            if settings.keys() != {"python"}:
                raise ValueError("configure the Python checker under [python]")
            observed.update(scan(root, settings["python"]))
        for path in args.findings:
            observed.update(read_counts(contents(root, path)))
        if args.action == "scan":
            print(json.dumps(rows(observed), indent=2))
            return 0
        proposed = read_counts(contents(root, args.baseline))
        report = compare(observed, proposed, target_counts(root, args.target, args.baseline))
        print(json.dumps(report, indent=2))
        return int(any(report.values()))
    except (OSError, ValueError, SyntaxError, RecursionError) as error:
        print(f"fitness: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
