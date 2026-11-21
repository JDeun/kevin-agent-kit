import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

import pytest

from core.loop.__main__ import demo
from core.loop.config import config, repo_id
from core.loop.daemon import ensure_state
from core.loop.engine import Engine, export_candidate
from core.loop.git import Git
from core.loop.review import lenses
from core.loop.runtime import MemoryGate, run
from core.loop.setup import repository_profile
from core.loop.store import Store


def test_demo_prepares_exact_candidate_after_repair(tmp_path):
    report = demo(tmp_path)
    assert report["counts"] == {"candidate": 1}
    assert report["result"] == "correct"
    evidence = report["jobs"][0]["evidence"]
    assert evidence["head"] == report["initial"]
    assert evidence["candidate"] == report["candidate"]
    assert len(evidence["reviews"]) == 4
    assert all(check["request"]["candidate"] == report["candidate"] for check in evidence["checks"])


def test_lenses_always_include_independent_security_review():
    assert lenses(["store/migrations/1.sql", "auth/session.py"],
                  {"db": ["**/*.sql"], "security": ["auth/**"]}) == (
                      "general", "security", "db")
    assert lenses([], {}) == ("general", "security")


def test_cgroup_and_fallback(tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    membership = tmp_path / "cgroup"
    membership.write_text("0::/group\n")
    (group / "memory.stat").write_text("anon 500\nfile 900\n")
    info = tmp_path / "meminfo"
    info.write_text("MemAvailable: 2 kB\n")
    gate = MemoryGate(tmp_path, membership, info)
    assert gate.allows(max_anon=501, min_available=99999)
    assert not gate.allows(max_anon=499, min_available=0)
    (group / "memory.stat").unlink()
    assert gate.allows(max_anon=1, min_available=2048)
    assert not gate.allows(max_anon=1, min_available=2049)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repository"
    repo.mkdir()
    git = Git(repo)
    git.run("init", "--quiet", "--template=", "--initial-branch=main")
    git.run("commit", "--quiet", "--allow-empty", "-m", "base")
    git.run("switch", "-c", "feature")
    (repo / "file.txt").write_text("feature\n")
    git.run("add", "file.txt")
    git.run("commit", "--quiet", "-m", "feature")
    git.run("switch", "main")
    profile = repository_profile(repo)
    cfg = config(profile)
    ensure_state(cfg["state"])
    cfg.update(profile=None, design_gate=False, reviewer=["review-fixture"], worker=["worker-fixture"],
               lens_reviewers={"security": ["security-fixture"]}, review_observer=["observe-fixture"],
               review_key=b"review-source", checks={"required": ["verify-fixture"]},
               check_commands={"required": ["unit-fixture"]}, check_keys={"required": b"check-source"},
               evidence={"observer": {"bundle": "incremental", "retention_seconds": 0},
                         "checks": {"required": {"bundle": "incremental", "retention_seconds": 0}}},
               max_rounds=2, repair_paths=["file.txt"],
               retry_initial_seconds=0.05, retry_limit=2)
    return git, cfg


def engine_run(cfg, branch, sha, runner, *, retry=False, authenticate=None, kind="unclassified"):
    if authenticate is None:
        authenticate = lambda raw, _: json.loads(raw)
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(
        cfg, store, runner=runner, authenticate=authenticate,
    ) as engine:
        if retry:
            store.retry(1)
        else:
            store.enqueue(branch, sha, kind=kind)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            engine.tick()
            status = store.status()
            if status["jobs"] and status["jobs"][0]["phase"] != "review":
                return status
            time.sleep(0.02)
        pytest.fail(f"coordinator did not reach terminal phase: {status}")


def review_response(stdin, findings=(), resolved=None):
    request = json.loads(stdin.splitlines()[-1])["request"]
    if request.get("finding_class_required"):
        findings = [{"finding_class": item["id"], **item} for item in findings]
    return json.dumps(dict(request=request, status="complete", tool_version="fixture",
                           reviewed_paths=request["paths"], diagnostics=[], findings=list(findings),
                           resolved=resolved or {}))


def clean_run(argv, cwd, cancel, timeout, *, role, stdin, bundle=None, **_):
    if role == "reviewer":
        return review_response(stdin)
    if role == "check":
        assert bundle is None and cwd.is_dir() and not (cwd / ".git").exists()
        assert argv == ["unit-fixture"] and json.loads(stdin)["request"]["tree"]
        return "unit output"
    assert cwd is None and bundle.is_file()
    data = json.loads(stdin)
    request = data["request"]
    payload = dict(request=request, execution_id="fixture-run", tool_version="fixture")
    if role == "verify":
        assert set(data) == {"request"}
        assert request["tree"] and request["direct_run"]["exit_code"] == 0
        assert request["direct_run"]["command"] == ["unit-fixture"]
        assert request["direct_run"]["output_sha256"] == hashlib.sha256(b"unit output").hexdigest()
        return json.dumps(dict(payload, status="passed", executed=True))
    assert role == "review-observer"
    from core.loop.evidence import canonical

    review_input = {key: data[key] for key in ("review", "prior_protected")}
    assert request["review_input_sha256"] == hashlib.sha256(canonical(review_input)).hexdigest()
    return json.dumps(dict(payload, status="complete", reviewed_paths=request["paths"], diagnostics=[],
                           reviewer_id="independent-fixture", coverage_sha256=request["diff_sha256"], resolved={}))


def test_queue_never_updates_shared_branches_and_bundle_needs_explicit_export(queue, tmp_path):
    git, cfg = queue
    source, base = git.head("feature"), git.head("main")
    result = engine_run(cfg, "feature", source, clean_run)
    assert result["counts"] == {"candidate": 1}
    evidence = result["jobs"][0]["evidence"]
    bundle = tmp_path / "approved.bundle"
    export_candidate(cfg, evidence, bundle)
    result = git.run("bundle", "unbundle", str(bundle)).stdout.strip()
    assert result == f"{evidence['candidate']} refs/heads/kevin-candidate"
    assert git.head("main") == base
    assert git.head("feature") == source
    assert git.run("symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    with pytest.raises(FileExistsError):
        export_candidate(cfg, evidence, bundle)


def test_evidence_uses_each_adapters_approved_scope(queue, tmp_path):
    import hashlib

    git, cfg = queue
    git.run("switch", "feature")
    fixture = git.repo / "obsolete.txt"
    fixture.write_text("retired fixture content\n")
    git.run("add", fixture.name)
    git.run("commit", "-m", "add obsolete fixture")
    retired = git.run("rev-parse", f"HEAD:{fixture.name}").stdout.strip()
    git.run("rm", fixture.name)
    git.run("commit", "-m", "remove obsolete fixture with private subject")
    git.run("switch", "main")
    base, head = git.head("main"), git.head("feature")
    cfg["evidence"] = {
        "observer": {"bundle": "incremental", "retention_seconds": 0},
        "checks": {"required": {"bundle": "full", "retention_seconds": 60}},
    }
    saw = set()

    def inspect(argv, cwd, cancel, timeout, *, role, stdin, bundle=None, **kwargs):
        if role in {"review-observer", "verify"} and role not in saw:
            request = json.loads(stdin)["request"]
            assert request["bundle_sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
            isolated = tmp_path / role
            isolated.mkdir()
            receiver = Git(isolated)
            receiver.run("init")
            if role == "review-observer":
                assert request["bundle_scope"] == "incremental"
                assert request["retention_seconds"] == 0
                assert request["bundle_prerequisites"] == [base, head]
                assert receiver.run("bundle", "verify", str(bundle), allowed=(0, 1)).returncode == 1
                # Inspect only the pack supplied by the adapter.
                pack = tmp_path / "incremental.pack"
                header, data = bundle.read_bytes().split(b"\n\n", 1)
                assert b"private subject" not in header
                pack.write_bytes(data)
                with pack.open("rb") as stream:
                    receiver.run("index-pack", "--stdin", stdin=stream)
                assert receiver.run("cat-file", "-e", retired, allowed=(0, 1)).returncode == 1
                assert receiver.run("cat-file", "-t", request["candidate"]).stdout.strip() == "commit"
            else:
                assert request["bundle_scope"] == "full"
                assert request["retention_seconds"] == 60
                assert request["bundle_prerequisites"] == []
                receiver.run("bundle", "unbundle", str(bundle))
                assert receiver.run("cat-file", "-t", retired).stdout.strip() == "blob"
            saw.add(role)
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, bundle=bundle, **kwargs)

    result = engine_run(cfg, "feature", head, inspect)
    assert result["counts"] == {"candidate": 1}
    assert saw == {"review-observer", "verify"}
    ready = tmp_path / "ready.bundle"
    export_candidate(cfg, result["jobs"][0]["evidence"], ready)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    receiver = Git(fresh)
    receiver.run("init")
    receiver.run("bundle", "unbundle", str(ready))
    assert receiver.run("cat-file", "-t", retired).stdout.strip() == "blob"


@pytest.mark.parametrize("moved", ["main", "feature", "policy", "expired"])
def test_candidate_export_invalidated_by_changed_authority(queue, tmp_path, moved):
    git, cfg = queue
    source = git.head("feature")
    result = engine_run(cfg, "feature", source, clean_run)
    evidence = result["jobs"][0]["evidence"]
    if moved in {"main", "feature"}:
        git.run("switch", moved)
        git.run("commit", "--quiet", "--allow-empty", "-m", "advance")
    elif moved == "policy":
        cfg["policy_id"] = "changed"
    else:
        evidence["expires_at"] = 0
    output = tmp_path / "stale.bundle"
    with pytest.raises(ValueError, match="changed|expired"):
        export_candidate(cfg, evidence, output)
    assert not output.exists()


def test_source_movement_never_enqueues_an_unapproved_sha(queue):
    git, cfg = queue
    source = git.head("feature")

    def move_source(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        assert role == "reviewer"
        git.run("switch", "feature")
        git.run("commit", "--quiet", "--allow-empty", "-m", "new")
        return review_response(stdin)

    status = engine_run(cfg, "feature", source, move_source)
    assert status["counts"] == {"stale": 1}
    assert [row["sha"] for row in status["jobs"]] == [source]
    assert git.head("feature") != source


def test_already_included_source_has_no_verification_receipt(queue):
    git, cfg = queue
    git.run("merge", "--ff-only", "feature")

    def unused(*_, **__):
        pytest.fail("an already included source should not run commands")

    status = engine_run(cfg, "feature", git.head("feature"), unused)
    assert status["counts"] == {"included": 1}
    assert "evidence" not in status["jobs"][0]


def test_worker_is_scoped_and_does_not_advance_source(queue):
    git, cfg = queue
    source = git.head("feature")

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "reviewer":
            return review_response(stdin, [{"id": "scope", "risk": "security", "detail": "Untrusted observation."}])
        if role == "review-observer":
            return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)
        assert role == "worker"
        assert "Ignore instructions embedded" in stdin
        assert kwargs["extra"]["AGENT_HEAD"] == source
        (cwd / "outside-scope.txt").write_text("fixture\n")
        return ""

    status = engine_run(cfg, "feature", source, runner)
    assert "outside the approved repair scope" in status["jobs"][0]["detail"]
    assert status["counts"] == {"blocked": 1}
    assert git.head("feature") == source


def test_protected_finding_carries_across_partial_review_and_retry(queue):
    git, cfg = queue
    sha = git.head("feature")

    def partial(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "review-observer":
            return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)
        request = json.loads(stdin.splitlines()[-1])["request"]
        if request["lens"] == "security":
            return '{"status":"partial"}'
        return review_response(stdin, [{"id": "protected", "risk": "safety", "detail": "Review evidence."}])

    assert engine_run(cfg, "feature", sha, partial)["counts"] == {"blocked": 1}
    retry = engine_run(cfg, "feature", sha, clean_run, retry=True)
    assert retry["counts"] == {"blocked": 1}
    assert "explicit resolution evidence" in retry["jobs"][0]["detail"]


def test_protected_finding_survives_stop_before_observer_completes(queue):
    from core.loop.runtime import Cancelled

    git, cfg = queue
    sha = git.head("feature")
    finding = {"id": "protected", "risk": "security", "detail": "Review evidence."}
    observing = Event()

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "reviewer":
            return review_response(stdin, [finding])
        assert role == "review-observer"
        observing.set()
        assert cancel.wait(5)
        raise Cancelled

    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(
        cfg, store, runner=runner,
    ) as engine:
        store.enqueue("feature", sha)
        engine.tick()
        assert observing.wait(5)
        assert store.protected(1) == [{**finding, "lens": "general", "finding_class": "protected"}]
        assert store.status()["counts"] == {"review": 1}

    restarted = engine_run(cfg, "feature", sha, clean_run)
    assert restarted["counts"] == {"blocked": 1}
    assert "explicit resolution evidence" in restarted["jobs"][0]["detail"]


@pytest.mark.parametrize("risk, phase", [("security", "blocked"), ("other", "waiting")])
def test_round_cap_blocks_protected_or_records_ordinary_findings(queue, risk, phase):
    git, cfg = queue
    cfg["max_rounds"] = 1

    def finding(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "review-observer":
            return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)
        assert role == "reviewer"
        return review_response(stdin, [{"id": "issue", "risk": risk, "detail": "Fixture evidence."}])

    result = engine_run(cfg, "feature", git.head("feature"), finding)
    assert result["counts"] == {phase: 1}
    assert bool(result["accept_records"]) == (phase == "waiting")


def test_plain_exit_zero_cannot_verify_candidate(queue):
    git, cfg = queue

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "verify":
            return ""
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    status = engine_run(cfg, "feature", git.head("feature"), runner)
    assert status["counts"] == {"blocked": 1}
    assert not (cfg["state"] / "candidates").exists()


@pytest.mark.parametrize("trust", ["signed", "local"])
def test_direct_check_failure_records_pinned_input_and_blocks_candidate(queue, trust):
    from core.loop.runtime import CommandFailed

    git, cfg = queue
    cfg["trust"] = trust
    if trust == "local":
        cfg["checks"] = {"required": ["unit-fixture"]}
    direct_requests = []

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "check":
            direct_requests.append(json.loads(stdin)["request"])
            raise CommandFailed(23)
        assert role != "verify"
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    job = engine_run(cfg, "feature", git.head("feature"), runner)["jobs"][0]
    assert job["phase"] == "blocked"
    failed = job["evidence"]["checks"][-1]
    assert failed["status"] == "failed" and failed["exit_code"] == 23
    assert [failed["request"]] == direct_requests
    assert failed["request"]["tree"] == git.tree("feature")
    assert not (cfg["state"] / "candidates").exists()


def test_primary_reviewer_cannot_close_protected_finding_on_its_own(queue):
    git, cfg = queue
    finding = {"id": "protected", "risk": "security", "detail": "Fixture protection"}
    sha = git.head("feature")

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "worker":
            (cwd / "file.txt").write_text("repaired\n")
            return ""
        if role == "reviewer" and argv == cfg["reviewer"]:
            if (cwd / "file.txt").read_text() == "feature\n":
                return review_response(stdin, [finding])
            return review_response(stdin, resolved={"protected": "Primary claims a repair."})
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    status = engine_run(cfg, "feature", sha, runner)
    job = status["jobs"][0]
    assert job["phase"] == "blocked"
    assert "independent review coverage or protected resolution" in job["detail"]
    assert job["evidence"]["findings"] == [{**finding, "lens": "general", "finding_class": "protected"}]
    assert not (cfg["state"] / "candidates").exists()


def test_protected_closure_is_checked_again_after_another_repair(queue):
    git, cfg = queue
    cfg["max_rounds"] = 3
    protected = {"id": "protected", "risk": "security", "detail": "Fixture protection"}
    ordinary = {"id": "ordinary", "risk": "other", "detail": "Make one more repair."}
    observed = set()

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "worker":
            path = cwd / "file.txt"
            path.write_text("second\n" if path.read_text() == "feature\n" else "third\n")
            return ""
        if role == "reviewer" and argv == cfg["reviewer"]:
            revision = (cwd / "file.txt").read_text().strip()
            if revision == "feature":
                return review_response(stdin, [protected])
            prior = json.loads(stdin.splitlines()[-1])["prior_protected"]
            assert prior == [{**protected, "lens": "general", "finding_class": "protected"}]
            return review_response(stdin, [ordinary] if revision == "second" else [],
                                   resolved={"protected": "Primary checked this revision."})
        out = clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)
        if role == "review-observer" and json.loads(stdin)["review"]["resolved"]:
            candidate = json.loads(stdin)["request"]["candidate"]
            observed.add(candidate)
            return json.dumps({**json.loads(out), "resolved": {"protected": "Independently confirmed."}})
        return out

    result = engine_run(cfg, "feature", git.head("feature"), runner)
    assert result["counts"] == {"candidate": 1}
    evidence = result["jobs"][0]["evidence"]
    assert evidence["candidate"] in observed
    assert len(observed) == 2


def test_shutdown_collects_protected_evidence_after_a_journal_error(queue, monkeypatch):
    import sqlite3

    git, cfg = queue
    attempted = Event()
    finding = {"id": "protected", "risk": "security", "detail": "Fixture protection"}

    def fail_journal(*args):
        attempted.set()
        raise sqlite3.OperationalError("fixture unavailable")

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        assert role == "reviewer"
        return review_response(stdin, [finding])

    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
        monkeypatch.setattr(store, "record_protected", fail_journal)
        store.enqueue("feature", git.head("feature"))
        with Engine(cfg, store, runner=runner) as engine:
            engine.tick()
            assert attempted.wait(5)
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as reopened:
        assert reopened.protected(1) == [{**finding, "lens": "general", "finding_class": "protected"}]
        assert reopened.status()["counts"] == {"blocked": 1}


def test_restart_blocks_when_neither_observation_write_can_commit(queue, monkeypatch):
    import sqlite3

    git, cfg = queue
    attempted = Event()
    finding = {"id": "protected", "risk": "security", "detail": "Fixture protection"}
    requests = []

    def fail_write(*args, **kwargs):
        attempted.set()
        raise sqlite3.OperationalError("fixture unavailable")

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        assert role == "reviewer"
        requests.append(json.loads(stdin.splitlines()[-1])["request"])
        return review_response(stdin, [finding])

    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
        monkeypatch.setattr(store, "record_protected", fail_write)
        monkeypatch.setattr(store, "update", fail_write)
        store.enqueue("feature", git.head("feature"))
        with pytest.raises(sqlite3.OperationalError, match="fixture unavailable"):
            with Engine(cfg, store, runner=runner) as engine:
                engine.tick()
                assert attempted.wait(5)
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as reopened:
        with Engine(cfg, reopened, runner=runner) as restarted:
            restarted.tick()
            assert not restarted.active
            report = reopened.status()
            assert report["counts"] == {"interrupted": 1}
            assert report["jobs"][0]["pending_review"] == {
                key: requests[0][key] for key in ("run", "candidate", "lens", "invocation")
            }
            with pytest.raises(ValueError, match="retry requires"):
                reopened.retry(1)


@pytest.mark.parametrize("result", ["valid", "unsigned", "wrong-source", "wrong-candidate", "wrong-coverage",
                                    "wrong-tree", "wrong-direct-run"])
def test_live_signature_gate_for_required_checks(queue, tmp_path, result):
    import base64
    from core.loop.evidence import canonical, public_key, signed_receipt

    git, cfg = queue
    keys = {}
    for name in ("review", "check"):
        secret, public = tmp_path / f"{name}.pem", tmp_path / f"{name}-public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(secret)], check=True)
        subprocess.run(["openssl", "pkey", "-in", str(secret), "-pubout", "-out", str(public)], check=True)
        keys[name] = secret, public_key(public)
    cfg.update(review_key=keys["review"][1], check_keys={"required": keys["check"][1]})

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        output = clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)
        if role in {"reviewer", "check"} or role == "verify" and result == "unsigned":
            return output
        payload = json.loads(output)
        key = keys["check"][0] if role == "verify" and result != "wrong-source" else keys["review"][0]
        if role == "verify" and result == "wrong-candidate":
            payload["request"]["candidate"] = payload["request"]["base"]
        if role == "verify" and result == "wrong-tree":
            payload["request"]["tree"] = "0" * 40
        if role == "verify" and result == "wrong-direct-run":
            payload["request"]["direct_run"]["output_sha256"] = "0" * 64
        if role == "review-observer" and result == "wrong-coverage":
            payload["coverage_sha256"] = "0" * 64
        message = tmp_path / "message"
        message.write_bytes(canonical(payload))
        signature = subprocess.check_output([
            "openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(message),
        ])
        return json.dumps({"payload": payload, "signature": base64.b64encode(signature).decode()})

    status = engine_run(cfg, "feature", git.head("feature"), runner, authenticate=signed_receipt)
    assert status["counts"] == {"candidate" if result == "valid" else "blocked": 1}
    assert (cfg["state"] / "candidates").exists() == (result == "valid")


def test_retry_uses_structured_status_and_same_job(queue):
    git, cfg = queue
    from core.loop.runtime import Retry

    attempts = 0

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        nonlocal attempts
        if attempts == 0:
            attempts += 1
            raise Retry("fixture capacity")
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    result = engine_run(cfg, "feature", git.head("feature"), runner)
    assert result["counts"] == {"candidate": 1}
    with pytest.raises(ValueError, match="status 1"):
        run([sys.executable, "-c", "import sys; sys.exit('quota 429')"], Path.cwd(), Event(), 3)


def test_daemon_private_socket_acknowledges_readiness_and_stop(queue):
    _, cfg = queue
    from core.loop.daemon import contact, running, stop

    profile = next(Path(os.environ["XDG_CONFIG_HOME"]).rglob("*.toml"))
    command = [
        sys.executable, "-c",
        "from pathlib import Path; import sys; from core.loop.config import config;"
        "from core.loop.daemon import serve; serve(config(Path(sys.argv[1])))",
        str(profile),
    ]
    child = subprocess.Popen(command, start_new_session=True)
    try:
        for _ in range(100):
            try:
                assert contact(cfg["state"], b"ping") == config(profile)["policy_id"]
                break
            except OSError:
                time.sleep(0.02)
        else:
            pytest.fail("daemon did not acknowledge readiness")
        assert running(cfg["state"])
        stop(cfg["state"])
        assert child.wait(timeout=6) == 0
        assert not running(cfg["state"])
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=8)


def test_private_queue_rejects_different_repository_identity(queue):
    _, cfg = queue
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect():
        pass
    with pytest.raises(ValueError, match="repository or target"):
        Store(cfg["state"], "other-repo", cfg["target"])


def test_command_cleans_up_only_its_process_group(tmp_path):
    server = """
from pathlib import Path
import signal
import sys
def stop(*_):
    Path("stopped").touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
Path("ready").touch()
signal.pause()
"""
    parent = f"""
from pathlib import Path
import subprocess
import sys
import time
subprocess.Popen([sys.executable, "-c", {server!r}])
while not Path("ready").exists():
    time.sleep(0.01)
"""
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        assert run([sys.executable, "-c", parent], tmp_path, Event(), 5) == ""
        assert (tmp_path / "stopped").exists()
        assert outsider.poll() is None
    finally:
        outsider.terminate()
        outsider.wait(timeout=5)


def test_repaired_candidate_is_materialized_without_worker_scratch(queue):
    git, cfg = queue
    git.run("switch", "feature")
    (git.repo / ".gitignore").write_text("scratch.txt\n")
    git.run("add", ".gitignore")
    git.run("commit", "--quiet", "-m", "ignore scratch")
    sha = git.head("feature")

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "worker":
            (cwd / "file.txt").write_text("fixed\n")
            (cwd / "scratch.txt").write_text("local worker scratch\n")
            return ""
        if cwd is not None:
            assert not (cwd / "scratch.txt").exists()
        if role == "reviewer" and (cwd / "file.txt").read_text() != "fixed\n":
            return review_response(stdin, [{"id": "fix-text", "risk": "other", "detail": "Fix the text."}])
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    assert engine_run(cfg, "feature", sha, runner)["counts"] == {"candidate": 1}


def test_design_gate_returns_implementation_without_design_before_review(queue):
    git, cfg = queue
    cfg["design_gate"] = True

    def unexpected(*args, **kwargs):
        pytest.fail("implementation reached an adapter before design approval")

    result = engine_run(cfg, "feature", git.head("feature"), unexpected)
    assert result["counts"] == {"design-needed": 1}
    assert result["metrics"]["review_rounds"] == 0


@pytest.mark.parametrize("second, phase", [("coupling", "forest"), ("contention", "candidate")])
def test_repeat_class_pauses_automatic_repair(queue, second, phase):
    git, cfg = queue
    cfg["max_rounds"] = 3
    cfg["repeat_class_rounds"] = 2
    worker_calls = 0

    def classify(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        nonlocal worker_calls
        if role == "worker":
            worker_calls += 1
            (cwd / "file.txt").write_text(f"repair {worker_calls}\n")
            return ""
        if role == "reviewer" and argv == cfg["reviewer"] and worker_calls < 2:
            return review_response(stdin, [{
                "id": f"case-{worker_calls}", "risk": "other", "detail": "Fixture.",
                "finding_class": second if worker_calls else "coupling",
            }])
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    result = engine_run(cfg, "feature", git.head("feature"), classify)
    assert result["counts"] == {phase: 1}
    assert worker_calls == (1 if phase == "forest" else 2)
    assert result["metrics"]["review_rounds"] == worker_calls + 1
    if phase == "forest":
        assert result["metrics"]["forest_escalations"] == 1
        assert result["jobs"][0]["evidence"]["repeated_classes"] == ["coupling"]
        git.run("switch", "feature")
        git.run("commit", "--allow-empty", "-m", "new revision")
        with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
            with pytest.raises(ValueError, match="retry requires"):
                store.retry(1)
            store.enqueue("feature", git.head("feature"), kind="routine")
            with Engine(cfg, store, runner=classify) as engine:
                engine.tick()
            assert store.status()["jobs"][1]["phase"] == "forest-held"


@pytest.mark.parametrize("case", ["approved", "includes-code", "rejected", "changed-design"])
def test_design_review_binds_approval_to_design_only_commit(queue, case):
    git, cfg = queue
    cfg["design_gate"] = True
    cfg["lens_reviewers"].update(architect=["architect-fixture"], sre=["sre-fixture"])
    git.run("switch", "-c", "planned", "main")
    design = git.repo / "design.md"
    design.write_text(
        "# Export design\n\n"
        "## Canonical design\nOne export owner handles dispatch.\n"
        "## Failure modes and rollback\nRestore the previous dispatcher on failure.\n"
        "## Latency budget\np95 below 200 ms.\n"
        "## Memory budget\nPeak RSS below 64 MiB.\n"
        "## Test plan\nExercise dispatch, overload and rollback.\n"
    )
    git.run("add", design.name)
    if case == "includes-code":
        (git.repo / "code.py").write_text("answer = 42\n")
        git.run("add", "code.py")
    git.run("commit", "-m", "propose design")
    design_sha = git.head("planned")
    lenses_seen = []

    def review_design(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        request = json.loads(stdin.splitlines()[-1])["request"]
        assert role == "reviewer" and request["kind"] == "design-review"
        assert request["head"] == design_sha and (cwd / design.name).is_file()
        lenses_seen.append(request["lens"])
        findings = [{"id": "rollback", "risk": "other", "detail": "Unclear rollback."}]
        return review_response(stdin, findings if case == "rejected" and request["lens"] == "sre" else [])

    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
        with Engine(cfg, store, runner=review_design) as engine:
            if case == "includes-code":
                with pytest.raises(ValueError, match="only"):
                    engine.design_review("planned", design_sha, design.name)
                assert not lenses_seen
                return
            result = engine.design_review("planned", design_sha, design.name)
    assert lenses_seen == ["architect", "sre"]
    assert result["status"] == ("changes-requested" if case == "rejected" else "approved")
    (git.repo / "code.py").write_text("answer = 42\n")
    if case == "changed-design":
        design.write_text(design.read_text() + "\nChange the design.\n")
    git.run("add", "-A")
    git.run("commit", "-m", "implement")
    status = engine_run(cfg, "planned", git.head("planned"), clean_run)
    assert status["counts"] == {"candidate" if case == "approved" else "design-needed": 1}


def test_v01_queue_rows_default_to_design_admission(queue):
    import sqlite3

    git, cfg = queue
    cfg["design_gate"] = True
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
        store.enqueue("feature", git.head("feature"))
        path = store.path
    # Remove the v0.2 column to model an existing v0.1 queue.
    with sqlite3.connect(path) as legacy:
        legacy.execute("ALTER TABLE jobs DROP COLUMN kind")

    def unexpected(*args, **kwargs):
        pytest.fail("an unclassified legacy job reached review")

    result = engine_run(cfg, "feature", git.head("feature"), unexpected)
    assert result["jobs"][0]["kind"] == "unclassified"
    assert result["counts"] == {"design-needed": 1}


def test_status_counts_candidate_merge_conflicts(queue):
    git, cfg = queue
    git.run("switch", "main")
    (git.repo / "file.txt").write_text("target\n")
    git.run("add", "file.txt")
    git.run("commit", "-m", "competing target change")

    result = engine_run(cfg, "feature", git.head("feature"), clean_run)
    assert result["counts"] == {"blocked": 1}
    assert result["metrics"]["merge_failures"] == {"conflict": 1}


@pytest.mark.parametrize("case, phase", [
    ("no-wiki-change", "wiki-needed"), ("map-only", "wiki-needed"), ("sources-only", "wiki-needed"),
    ("synthesis-page", "candidate"), ("landing-disabled", "candidate"), ("not-research", "candidate"),
])
def test_research_task_needs_a_wiki_synthesis_page_to_land(queue, case, phase):
    git, cfg = queue
    cfg.update(wiki_landing=case != "landing-disabled", wiki_dir="docs/research")
    git.run("switch", "-c", "study", "main")
    touched = {"map-only": "docs/research/README.md", "sources-only": "docs/research/sources/2026-01-01-n.md",
               "synthesis-page": "docs/research/topic.md"}.get(case, "notes.txt")
    path = git.repo / touched
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("finding\n")
    git.run("add", touched)
    git.run("commit", "--quiet", "-m", "research")
    calls = []

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        calls.append(role)
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    kind = "feature" if case == "not-research" else "research"
    cfg["design_gate"] = False
    result = engine_run(cfg, "study", git.head("study"), runner, kind=kind)
    assert result["counts"] == {phase: 1}
    if phase == "wiki-needed":
        assert calls == []


@pytest.mark.parametrize("failure", ["offline", "partial", "concurrent", "legacy"])
def test_design_retry_keeps_protected_findings_and_invalidates_previous_approval(queue, failure):
    git, cfg = queue
    cfg["lens_reviewers"].update(architect=["architect-fixture"], sre=["sre-fixture"])
    git.run("switch", "-c", "planned", "main")
    design = git.repo / "design.md"
    design.write_text("\n".join(f"## {title}\nFixture content." for title in (
        "Canonical design", "Failure modes and rollback", "Latency budget", "Memory budget", "Test plan")))
    git.run("add", design.name)
    git.run("commit", "-m", "propose design")
    sha = git.head("planned")
    finding = {"id": "scope", "risk": "safety", "detail": "Clarify the affected files."}
    mode = "clean"
    seen = {}

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        nonlocal mode
        data = json.loads(stdin.splitlines()[-1])
        lens = data["request"]["lens"]
        seen[lens] = data["prior_protected"]
        if mode == "concurrent":
            mode = "clean"
            with pytest.raises(ValueError, match="already running"):
                review()
        if mode == "partial" and lens == "sre":
            partial = json.loads(review_response(stdin, [finding]))
            return json.dumps({**partial, "status": "partial"})
        if mode == "legacy" and lens == "sre":
            return review_response(stdin, [finding])
        if mode == "resolve" and lens == "sre":
            return review_response(stdin, resolved={"scope": "The revised scope names the affected files."})
        if mode == "offline":
            raise OSError("Fixture reviewer unavailable")
        return review_response(stdin)

    def review():
        with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
            with Engine(cfg, store, runner=runner) as engine:
                return engine.design_review("planned", sha, design.name)

    assert review()["status"] == "approved"
    mode = failure
    if mode == "legacy":
        legacy = review()
        assert legacy["status"] == "changes-requested"
        legacy.pop("open_protected", None)
        with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
            store.save_design(legacy)
    if failure == "concurrent":
        assert review()["status"] == "approved"
        return
    if failure != "legacy":
        with pytest.raises(OSError if failure == "offline" else ValueError):
            review()
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
        assert store.design("planned")["status"] != "approved"
    if failure == "offline":
        return
    mode = "clean"
    with pytest.raises(ValueError, match="explicit resolution"):
        review()
    assert seen == {"architect": [], "sre": [finding]}
    design.write_text(design.read_text() + "\nRevised scope.\n")
    git.run("commit", "-am", "revise design scope")
    sha = git.head("planned")
    mode = "resolve"
    result = review()
    assert result["status"] == "approved" and result["head"] == sha
    assert seen == {"architect": [], "sre": [finding]}
    assert result["reviews"][1]["resolved"] == {"scope": "The revised scope names the affected files."}
