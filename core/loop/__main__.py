import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from .config import config, project
from .daemon import notify, running, serve, start
from .engine import Engine
from .git import Git
from .runtime import Busy
from .store import Store


def demo(root):
    root = root.resolve()
    repo, state = root / "repo", root / "state"
    repo.mkdir()
    git = Git(repo, state)
    git.run("init", "--quiet", "--initial-branch=main")
    git.run("config", "user.name", "Demo Author")
    git.run("config", "user.email", "demo@example.invalid")
    git.run("config", "commit.gpgsign", "false")
    file = repo / "example.txt"
    file.write_text("baseline\n", encoding="utf-8")
    git.run("add", "example.txt")
    git.run("commit", "--quiet", "-m", "chore: add baseline")
    git.run("switch", "-c", "change")
    file.write_text("wrong\n", encoding="utf-8")
    git.run("commit", "--quiet", "-am", "feat: propose change")
    initial = git.head("change")
    git.run("switch", "--detach", "main")
    reviewer = """
import json
from pathlib import Path
import sys
assert "review" in sys.stdin.read().lower()
wrong = Path("example.txt").read_text().strip() != "correct"
print(json.dumps({"findings": [{"risk": "other", "detail": "Use the expected text."}] if wrong else []}))
"""
    worker = """
from pathlib import Path
import subprocess
import sys
assert "Use the expected text." in sys.stdin.read()
Path("example.txt").write_text("correct" + chr(10))
subprocess.run(["git", "commit", "--quiet", "-am", "fix: correct example"], check=True)
"""
    verify = """
import os
from pathlib import Path
import subprocess
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["AGENT_CANDIDATE"]
assert Path("example.txt").read_text().strip() == "correct"
"""
    cfg = config()
    cfg.update(
        repo=repo, state=state, target="main",
        reviewer=[sys.executable, "-c", reviewer],
        worker=[sys.executable, "-c", worker], verify=[sys.executable, "-c", verify],
        timeout_seconds=10, memory={stage: {"max_anon_mib": 0, "min_available_mib": 0}
                                   for stage in ("review", "test")},
    )
    with Store(state).connect() as store, Engine(cfg, store) as engine:
        store.enqueue("change", initial)
        store.enqueue("change", initial)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            engine.tick()
            if not engine.active and not store.ready():
                break
            time.sleep(0.02)
        report = store.status()
        if not report["counts"].get("merged"):
            raise ValueError(f"demo did not merge: {report}")
    return {
        **report, "initial": initial, "reviewed": git.head("change"),
        "result": git.run("show", "main:example.txt").stdout.strip(),
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    modules = {
        "persona-review": "reviews.persona",
        "hosting": "core.adapters",
        "ledger": "core.ledgers",
        "guard": "guards",
        "screenshot": "verify.screenshot",
    }
    if argv and argv[0] in modules:
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "PYTHONPATH": str(root)}
        os.execve(sys.executable, [sys.executable, "-P", "-m", modules[argv[0]], *argv[1:]], env)
    parser = argparse.ArgumentParser(prog=project()["name"])
    parser.add_argument("--config", type=Path, help="load loop overrides from a TOML file")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("demo", "run", "start", "stop", "status"):
        commands.add_parser(name)
    for name in modules:
        commands.add_parser(name)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("branch")
    enqueue.add_argument("sha")
    accept = commands.add_parser("accept-record")
    accept.add_argument("record_id", type=int, nargs="?")
    decide = commands.add_parser("decide")
    decide.add_argument("record_id", type=int)
    decide.add_argument("disposition", choices=("revise", "defer"))
    decide.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "demo":
            with tempfile.TemporaryDirectory(prefix="agent-demo-") as temp:
                print(json.dumps(demo(Path(temp)), indent=2))
            return 0
        cfg = config(args.config)
        if args.action == "run":
            serve(cfg)
        elif args.action == "start":
            start(cfg, args.config)
        elif args.action == "stop":
            if running(cfg["state"]):
                notify(cfg["state"])
        else:
            with Store(cfg["state"]).connect() as store:
                if args.action == "enqueue":
                    Git(cfg["repo"], cfg["state"]).ref(args.branch)
                    store.enqueue(args.branch, args.sha)
                elif args.action == "decide":
                    store.decide(args.record_id, args.disposition, args.source)
                elif args.action == "accept-record":
                    records = store.status()["accept_records"]
                    if args.record_id is not None:
                        records = [item for item in records if item["id"] == args.record_id]
                        if not records:
                            raise ValueError("accept record not found")
                    print(json.dumps(records, indent=2))
                else:
                    rejected = store.ingest()
                    print(json.dumps({
                        "running": running(cfg["state"]), "rejected_requests": rejected,
                        **store.status(),
                    }, indent=2))
    except (ValueError, OSError, Busy) as error:
        print(f"{parser.prog}: {error or 'coordinator is already running'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
