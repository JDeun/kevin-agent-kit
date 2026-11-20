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
    cfg.update(profile=None, reviewer=["review-fixture"], worker=["worker-fixture"],
               lens_reviewers={"security": ["security-fixture"]}, review_observer=["observe-fixture"],
               review_key=b"review-source", checks={"required": ["verify-fixture"]},
               check_keys={"required": b"check-source"},
               max_rounds=2, repair_paths=["file.txt"],
               retry_initial_seconds=0.05, retry_limit=2)
    return git, cfg


def engine_run(cfg, branch, sha, runner, *, retry=False, authenticate=None):
    if authenticate is None:
        authenticate = lambda raw, _: json.loads(raw)
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(
        cfg, store, runner=runner, authenticate=authenticate,
    ) as engine:
        if retry:
            store.retry(1)
        else:
            store.enqueue(branch, sha)
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
    return json.dumps(dict(request=request, status="complete", tool_version="fixture",
                           reviewed_paths=request["paths"], diagnostics=[], findings=list(findings),
                           resolved=resolved or {}))


def clean_run(argv, cwd, cancel, timeout, *, role, stdin, bundle=None, **_):
    if role == "reviewer":
        return review_response(stdin)
    assert cwd is None and bundle.is_file()
    data = json.loads(stdin)
    request = data["request"]
    payload = dict(request=request, execution_id="fixture-run", tool_version="fixture")
    if role == "verify":
        assert set(data) == {"request"}
        return json.dumps(dict(payload, status="passed", executed=True))
    assert role == "review-observer"
    import hashlib
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
    assert job["evidence"]["findings"] == [{**finding, "lens": "general"}]
    assert not (cfg["state"] / "candidates").exists()


@pytest.mark.parametrize("result", ["valid", "unsigned", "wrong-source", "wrong-candidate", "wrong-coverage"])
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
        if role == "reviewer" or role == "verify" and result == "unsigned":
            return output
        payload = json.loads(output)
        key = keys["check"][0] if role == "verify" and result != "wrong-source" else keys["review"][0]
        if role == "verify" and result == "wrong-candidate":
            payload["request"]["candidate"] = payload["request"]["base"]
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
