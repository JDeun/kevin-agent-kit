import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import DEFAULT_DIR, WikiError, ingest, init, lint, lint_task
from .external import lint_remote


def checkout_root(path):
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    if result.returncode:
        raise WikiError("run inside a Git checkout or pass --root")
    return Path(result.stdout.strip())


def emit(text, output):
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"task written to {output}")
    else:
        print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wiki", description="Maintain a repository knowledge wiki.")
    parser.add_argument("--root", type=Path, help="checkout root (default: the current checkout)")
    parser.add_argument("--dir", default=DEFAULT_DIR, help=f"wiki directory (default {DEFAULT_DIR})")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("init", help="create the map, rules and sources/ without overwriting")
    add = commands.add_parser("ingest", help="store raw material and print the update task")
    add.add_argument("source", type=Path)
    add.add_argument("--name", help="slug for the stored file")
    add.add_argument("--date", help="YYYY-MM-DD (default today)")
    add.add_argument("--task-out", help="write the task to a file")
    check = commands.add_parser("lint", help="check links, front matter, sources and orphans")
    check.add_argument("--strict", action="store_true", help="treat warnings as errors")
    check.add_argument("--task-out", help="write the LLM judgement lint task to a file")
    remote = commands.add_parser("external", help="read and lint a hosted wiki, read-only")
    remote.add_argument("url", help="git URL or path of the wiki repository")
    args = parser.parse_args(argv)
    try:
        if args.action == "external":
            report = lint_remote(args.url)
            print(json.dumps(report, indent=2))
            return int(bool(report["errors"]))
        root = checkout_root(args.root or Path.cwd())
        if args.action == "init":
            print(json.dumps(init(root, args.dir), indent=2))
        elif args.action == "ingest":
            result = ingest(root, args.source, args.dir, today=args.date, name=args.name)
            print(json.dumps({key: result[key] for key in ("source", "sha256")}, indent=2))
            emit(result["task"], args.task_out)
        else:
            report = lint(root, args.dir, strict=args.strict)
            print(json.dumps(report, indent=2))
            if args.task_out:
                emit(lint_task(root, args.dir), args.task_out)
            return int(bool(report["errors"]))
    except (WikiError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"wiki: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
