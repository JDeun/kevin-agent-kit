import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

from .config import MODULE_PREFIX, ROOT, config, project, repo_id
from .daemon import ensure_state, running, serve, start, stop, validate_execution
from .engine import Engine, check_candidate, export_candidate
from .git import Git
from .runtime import Busy, run
from .setup import global_rules, repository_profile
from .store import SHA, Store


def demo(root):
    """Exercise the queue with embedded trusted fixtures and temporary repositories."""
    repo, state = root / "repo", root / "state"
    repo.mkdir()
    state.mkdir()
    git = Git(repo)
    git.run("init", "--quiet", "--template=", "--initial-branch=main")
    file = repo / "example.txt"
    file.write_text("baseline\n", encoding="utf-8")
    git.run("add", "example.txt")
    git.run("commit", "--quiet", "-m", "chore: add baseline")
    base = git.head("main")
    git.run("switch", "-c", "change")
    file.write_text("wrong\n", encoding="utf-8")
    git.run("commit", "--quiet", "-am", "feat: propose change")
    initial = git.head("change")
    reviewer = """
import json
from pathlib import Path
import sys
request = json.loads(sys.stdin.read().splitlines()[-1])["request"]
wrong = Path("example.txt").read_text().strip() != "correct"
items = [{"id": "wrong-text", "risk": "other", "detail": "Use the expected text."}] if wrong else []
print(json.dumps(dict(request=request, status="complete", tool_version="demo",
                      reviewed_paths=request["paths"], diagnostics=[], findings=items, resolved={})))
"""
    worker = """
from pathlib import Path
import sys
assert "Use the expected text." in sys.stdin.read()
Path("example.txt").write_text("correct" + chr(10))
"""
    verify = """
import os
from pathlib import Path
import subprocess
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["AGENT_CANDIDATE"]
assert Path("example.txt").read_text().strip() == "correct"
print("example checked")
"""
    attest = """
import json
import sys
data = json.load(sys.stdin)
request = data["request"]
assert data["observation"] == {"exit_status": 0, "stdout": "example checked" + chr(10)}
print(json.dumps(dict(request=request, status="passed", tool_version="demo", executed=True)))
"""

    def demo_runner(argv, cwd, cancel, timeout, *, stdin, extra, **_):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **extra}
        return run(argv, cwd, cancel, timeout, prompt=stdin, env=env)

    cfg = dict(
        repo=repo, state=state, target="main", repo_id=repo_id(git.common()), policy_id="demo",
        reviewer=[sys.executable, "-I", "-c", reviewer], reviewer_model="demo",
        worker=[sys.executable, "-I", "-c", worker], worker_model="demo",
        checks={"example": [sys.executable, "-I", "-c", verify]}, repair_paths=["example.txt"],
        attestors={"example": [sys.executable, "-I", "-c", attest]},
        lenses={}, lens_reviewers={}, execution={}, max_jobs=1, max_rounds=3, timeout_seconds=10,
        evidence_ttl_seconds=60, retry_limit=1, retry_initial_seconds=1, retry_max_seconds=1,
        memory={stage: {"max_anon_mib": 0, "min_available_mib": 0} for stage in ("review", "test")},
    )
    with Store(state, cfg["repo_id"], cfg["target"]).connect() as store, Engine(cfg, store, runner=demo_runner) as engine:
        store.enqueue("change", initial)
        store.enqueue("change", initial)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            engine.tick()
            if not engine.active and not store.ready():
                break
            time.sleep(0.02)
        report = store.status()
        if report["counts"] != {"candidate": 1}:
            raise ValueError(f"demo did not prepare a candidate: {report['counts']}")
        evidence = store.candidate(1)
        bundle = root / "ready.bundle"
        export_candidate(cfg, evidence, bundle)
    assert git.head("main") == base and git.head("change") == initial
    git.run("bundle", "unbundle", str(bundle))
    return {
        **report, "initial": initial, "candidate": evidence["candidate"],
        "result": git.run("show", f"{evidence['candidate']}:example.txt").stdout.strip(),
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
        module = MODULE_PREFIX + modules[argv[0]]
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        os.execve(sys.executable, [sys.executable, "-P", "-m", module, *argv[1:]], env)
    meta = project()
    parser = argparse.ArgumentParser(prog=meta["name"], allow_abbrev=False)
    parser.add_argument("--version", action="version", version=meta["version"])
    parser.add_argument("--config", type=Path, help="user-owned execution profile")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("demo", "run", "start", "stop", "status", *modules):
        commands.add_parser(name)
    setup = commands.add_parser("init", help="draft a profile for the current repository")
    setup.add_argument("--global", dest="global_rules", action="store_true")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--uninstall", action="store_true")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("branch")
    enqueue.add_argument("sha")
    retry = commands.add_parser("retry")
    retry.add_argument("job_id", type=int)
    candidate = commands.add_parser("candidate", help="export an exact ready candidate bundle")
    candidate.add_argument("job_id", type=int)
    candidate.add_argument("--output", type=Path, required=True)
    accept = commands.add_parser("accept-record")
    accept.add_argument("record_id", type=int, nargs="?")
    decide = commands.add_parser("decide")
    decide.add_argument("record_id", type=int)
    decide.add_argument("disposition", choices=("revise", "defer"))
    decide.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            if args.uninstall and not args.global_rules:
                raise ValueError("--uninstall requires --global")
            if args.global_rules:
                for path in global_rules(dry_run=args.dry_run, uninstall=args.uninstall):
                    print(path)
            else:
                print(repository_profile(Path.cwd(), dry_run=args.dry_run))
            return 0
        if args.action == "demo":
            with tempfile.TemporaryDirectory(prefix="agent-demo-") as temp:
                print(json.dumps(demo(Path(temp)), indent=2))
            return 0
        if args.action == "stop":
            stop(config(args.config, control_only=True)["state"])
            return 0
        cfg = config(args.config)
        state = ensure_state(cfg["state"])
        if args.action == "run":
            validate_execution(cfg)
            serve(cfg)
        elif args.action == "start":
            start(cfg)
        else:
            with Store(state, cfg["repo_id"], cfg["target"]).connect() as store:
                if args.action == "enqueue":
                    git = Git(cfg["repo"])
                    if (not SHA.fullmatch(args.sha) or args.branch == cfg["target"]
                            or git.head(args.branch) != args.sha):
                        raise ValueError("enqueue requires the exact current SHA of a distinct source branch")
                    store.enqueue(args.branch, args.sha)
                elif args.action == "retry":
                    store.retry(args.job_id)
                elif args.action == "candidate":
                    evidence = store.candidate(args.job_id)
                    export_candidate(cfg, evidence, args.output.absolute())
                    print(evidence["candidate"])
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
                    report = store.status()
                    for job in report["jobs"]:
                        if job["phase"] != "candidate":
                            continue
                        try:
                            check_candidate(cfg, job["evidence"])
                        except ValueError as error:
                            job["phase"] = "stale"
                            job["detail"] = str(error)
                            report["counts"]["candidate"] -= 1
                            report["counts"]["stale"] = report["counts"].get("stale", 0) + 1
                    report["counts"] = {phase: count for phase, count in report["counts"].items() if count}
                    print(json.dumps({"running": running(state), **report}, indent=2))
    except (ValueError, OSError, sqlite3.Error, Busy) as error:
        print(f"{parser.prog}: {error or 'coordinator is already running'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
