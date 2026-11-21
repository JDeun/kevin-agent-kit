import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from . import install
from .config import config, project, repo_id
from .install import MODULE_PREFIX, ROOT
from .daemon import ensure_state, running, serve, start, stop, validate_execution
from .design import TASK_KINDS
from .engine import Engine, check_candidate, export_candidate
from .git import Git
from .runtime import Busy, Retry, module_command, run
from .setup import approve_install, global_rules, repository_profile
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
items = [{"id": "wrong-text", "risk": "other", "detail": "Use the expected text.",
          "finding_class": "unexpected-output"}] if wrong else []
print(json.dumps(dict(request=request, status="complete", tool_version="demo",
                      reviewed_paths=request["paths"], diagnostics=[], findings=items, resolved={})))
"""
    worker = """
from pathlib import Path
import sys
assert "Use the expected text." in sys.stdin.read()
Path("example.txt").write_text("correct" + chr(10))
"""
    check = """
from pathlib import Path
assert Path("example.txt").read_bytes() == b"correct\\n"
"""
    def demo_runner(argv, cwd, cancel, timeout, *, role, stdin, extra, bundle, **_):
        if role in {"verify", "review-observer"}:
            request = json.loads(stdin)["request"]
            with bundle.open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == request["bundle_sha256"]
            with git.snapshot(root, *request["bundle_prerequisites"]) as (checked, _):
                checked.run("bundle", "unbundle", str(bundle))
                sha = request["candidate"]
                result = dict(request=request, tool_version="demo", execution_id=request["invocation"])
                if role == "verify":
                    assert checked.tree(sha) == request["tree"] and request["direct_run"]["exit_code"] == 0
                    assert checked.run("show", f"{sha}:example.txt").stdout.strip() == "correct"
                    return json.dumps(dict(result, status="passed", executed=True))
                paths = sorted(checked.paths(request["base"], sha))
                digest = checked.diff_digest(request["base"], sha)
                return json.dumps(dict(result, status="complete", reviewed_paths=paths, diagnostics=[],
                                       coverage_sha256=digest, reviewer_id="demo-observer", resolved={}))
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **extra}
        return run(argv, cwd, cancel, timeout, prompt=stdin, env=env)

    review_command = [sys.executable, "-I", "-c", reviewer]
    cfg = dict(
        repo=repo, state=state, target="main", repo_id=repo_id(git.common()), policy_id="demo",
        reviewer=review_command, reviewer_model="demo", review_observer=["observe-fixture"],
        review_key=b"demo-review-source", check_keys={"example": b"demo-check-source"},
        evidence={"observer": {"bundle": "incremental", "retention_seconds": 0},
                  "checks": {"example": {"bundle": "incremental", "retention_seconds": 0}}},
        worker=[sys.executable, "-I", "-c", worker], worker_model="demo",
        checks={"example": ["check-fixture"]}, repair_paths=["example.txt"],
        check_commands={"example": [sys.executable, "-I", "-c", check]},
        lenses={}, lens_reviewers={"security": review_command}, execution={}, max_jobs=1, max_rounds=3, timeout_seconds=10,
        evidence_ttl_seconds=60, retry_limit=1, retry_initial_seconds=1, retry_max_seconds=1,
        design_gate=False, repeat_class_rounds=2, wiki_landing=False, wiki_dir="docs/research",
        memory={stage: {"max_anon_mib": 0, "min_available_mib": 0} for stage in ("review", "test")},
    )
    # The standalone demo uses only its in-process fixture sources.
    with Store(state, cfg["repo_id"], cfg["target"]).connect() as store, Engine(
        cfg, store, runner=demo_runner, authenticate=lambda raw, _: json.loads(raw),
    ) as engine:
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


def doctor(profile):
    """Diagnose this machine before a profile or a daemon exists. Never raises."""
    from .sandbox import ISOLATION_HELP, isolation_status

    checks, advice = [], []

    def add(name, ok, detail, required=True):
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    git = shutil.which("git")
    add("git", git, git or "git is not on PATH")
    trust = "signed"
    try:
        add("install", True, f"trusted snapshot sha256={install.snapshot().digest}")
    except (ValueError, OSError) as error:
        add("install", False, str(error)[:250])
        advice.append("reinstall the kit, or restore the owner and permissions of the reported path")
    try:
        cfg = config(profile)
        trust = cfg["trust"]
        add("profile", True, f"valid; trust={trust}, isolation=bwrap")
        add("reviewer and checks", bool(cfg["reviewer"] and cfg["checks"]),
            "configured" if cfg["reviewer"] and cfg["checks"] else "set reviewer and [loop.checks]")
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        add("profile", False, f"not usable yet: {str(error)[:200]}",
            required=profile is not None or not isinstance(error, FileNotFoundError))
        advice.append("configure a trusted profile and register its broker clients before starting the loop"
                      if "approve" not in str(error) else
                      "the profile approves a different install; review it, then run `init --approve-install`")
    available, detail = isolation_status()
    add("bubblewrap isolation", available, detail)
    if not available:
        advice.append(ISOLATION_HELP)
    if trust == "signed":
        from .evidence import OPENSSL

        add("openssl", os.access(OPENSSL, os.X_OK), str(OPENSSL))
    ok = all(item["ok"] for item in checks if item["required"])
    mode = f"isolated/{trust}-evidence"
    return {"ok": ok, "mode": mode, "checks": checks, "advice": advice}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    modules = {
        "persona-review": "reviews.persona",
        "hosting": "core.adapters",
        "ledger": "core.ledgers",
        "guard": "guards",
        "fitness": "guards.fitness",
        "wiki": "wiki",
        "screenshot": "verify.screenshot",
    }
    if argv and argv[0] in modules:
        command = module_command(ROOT, MODULE_PREFIX + modules[argv[0]], *argv[1:])
        os.execv(command[0], command)
    try:
        meta = project()
    except (ValueError, OSError) as error:
        # An untrusted install cannot name itself; doctor still reports what it found.
        if argv[:1] == ["doctor"]:
            print(json.dumps(doctor(None), indent=2))
        else:
            print(error, file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(prog=meta["name"], allow_abbrev=False)
    parser.add_argument("--version", action="version", version=meta["version"])
    parser.add_argument("--config", type=Path, help="user-owned execution profile")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("demo", "run", "start", "stop", "status", "doctor", *modules):
        commands.add_parser(name)
    setup = commands.add_parser("init", help="draft a profile for the current repository")
    setup.add_argument("--global", dest="global_rules", action="store_true")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--uninstall", action="store_true")
    setup.add_argument("--approve-install", action="store_true",
                       help="record the installed kit snapshot in the existing profile after reviewing it")
    setup.add_argument("--agent", choices=("codex", "claude"),
                       help="draft an isolated unsigned profile for an operator-provided model broker client")
    setup.add_argument("--adopt-rules-sha256", action="append", default=[], metavar="SHA256",
                       help="approve the exact bytes of a reviewed legacy global rules block")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("branch")
    enqueue.add_argument("sha")
    enqueue.add_argument("--kind", choices=TASK_KINDS, default="unclassified")
    enqueue.add_argument("--inherit-from", type=int, metavar="JOB_ID",
                         help="carry the protected findings of another job into this one")
    design = commands.add_parser("design-review", help="review a design-only source revision before implementation")
    design.add_argument("branch")
    design.add_argument("sha")
    design.add_argument("--design", required=True, help="repository-relative Markdown path")
    retry = commands.add_parser("retry")
    retry.add_argument("job_id", type=int)
    resume = commands.add_parser("resume", help="return an interrupted job to review with its journaled findings")
    resume.add_argument("job_id", type=int)
    held = commands.add_parser("evidence", help="print the unsettled review attempts of a job")
    held.add_argument("job_id", type=int)
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
            if (args.uninstall or args.adopt_rules_sha256) and not args.global_rules:
                raise ValueError("--uninstall and --adopt-rules-sha256 require --global")
            if args.global_rules:
                for path in global_rules(dry_run=args.dry_run, uninstall=args.uninstall, adopt=args.adopt_rules_sha256):
                    print(path)
            elif args.approve_install:
                print(approve_install(args.config))
            else:
                print(repository_profile(Path.cwd(), dry_run=args.dry_run, agent=args.agent))
            return 0
        if args.action == "doctor":
            report = doctor(args.config)
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
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
                    job = store.enqueue(args.branch, args.sha, kind=args.kind, inherits=args.inherit_from)
                    if job["phase"] == "interrupted":
                        print(f"job {job['id']} is interrupted: read `evidence {job['id']}`, then "
                              f"`resume {job['id']}` or enqueue a new SHA", file=sys.stderr)
                elif args.action == "design-review":
                    report = Engine(cfg, store).design_review(args.branch, args.sha, args.design)
                    print(json.dumps(report, indent=2))
                    return int(report["status"] != "approved")
                elif args.action == "retry":
                    store.retry(args.job_id)
                elif args.action == "resume":
                    store.resume(args.job_id)
                elif args.action == "evidence":
                    print(json.dumps(store.held(args.job_id), indent=2))
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
                    alive = running(state)
                    waiting = report["counts"].get("review", 0)
                    note = ("daemon is not running; queued jobs wait until `start`" if waiting and not alive else None)
                    print(json.dumps({"running": alive, **({"note": note} if note else {}), **report}, indent=2))
    except (ValueError, OSError, sqlite3.Error, Busy, Retry) as error:
        print(f"{parser.prog}: {error or 'coordinator is already running'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
