"""Regression tests for the v0.3.1 release fixes."""

import json
import time

import pytest

from core.loop.design import validate_document
from core.loop.engine import Engine
from core.loop.git import Git
from core.loop.store import Store
from test_loop import clean_run, engine_run, queue, review_response  # noqa: F401  (queue is a fixture)

SECURITY = {"id": "sec-1", "risk": "security", "detail": "Token reaches a log.", "lens": "security",
            "finding_class": "secret-exposure"}


def store_of(cfg):
    return Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect()


def hold_for_redesign(store, git):
    """Create a forest-held job that carries a protected finding."""
    store.enqueue("feature", git.head("feature"))
    job = store.status()["jobs"][0]
    store.update(job, "forest", detail="redesign", evidence={"findings": [SECURITY], "rounds": 2})
    return job["id"]


def settle(cfg, store, runner, job_id, seconds=8):
    with Engine(cfg, store, runner=runner, authenticate=lambda raw, _: json.loads(raw)) as engine:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            engine.tick()
            jobs = {job["id"]: job for job in store.status()["jobs"]}
            if jobs[job_id]["phase"] != "review":
                return jobs[job_id]
            time.sleep(0.02)
    raise AssertionError("job did not settle")


def revise_feature(git, text):
    git.run("switch", "feature")
    (git.repo / "file.txt").write_text(text)
    git.run("commit", "-qam", "redesign")
    git.run("switch", "main")
    return git.head("feature")


def test_redesign_on_the_same_branch_inherits_unresolved_protected_findings(queue, monkeypatch):
    git, cfg = queue
    monkeypatch.setattr(Engine, "design_gate", lambda self, job, base, forest, approval: (None, None))
    with store_of(cfg) as store:
        held = hold_for_redesign(store, git)
        store.enqueue("feature", revise_feature(git, "redesigned\n"))
        job = store.status()["jobs"][1]
        assert [item["id"] for item in store.inherited_protected(job)] == ["sec-1"]

        # A clean review that does not resolve the old finding cannot produce a candidate.
        outcome = settle(cfg, store, clean_run, job["id"])
        assert outcome["phase"] == "blocked" and held == 1


def test_redesign_resolving_the_inherited_finding_reaches_a_candidate(queue, monkeypatch):
    git, cfg = queue
    monkeypatch.setattr(Engine, "design_gate", lambda self, job, base, forest, approval: (None, None))
    with store_of(cfg) as store:
        hold_for_redesign(store, git)
        store.enqueue("feature", revise_feature(git, "redesigned\n"))
        job = store.status()["jobs"][1]

        def resolving(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
            if role == "reviewer":
                seen = json.loads(stdin.splitlines()[-1])["prior_protected"]
                resolved = {item["id"]: "removed the log statement" for item in seen}
                return review_response(stdin, resolved=resolved)
            if role == "review-observer":
                payload = json.loads(clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs))
                payload["resolved"] = json.loads(stdin)["review"]["resolved"]
                return json.dumps(payload)
            return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

        outcome = settle(cfg, store, resolving, job["id"])
        assert outcome["phase"] == "candidate", outcome["detail"]


def test_inheritance_can_be_named_for_a_new_branch_and_follows_the_chain(queue):
    git, cfg = queue
    with store_of(cfg) as store:
        held = hold_for_redesign(store, git)
        git.run("switch", "-c", "redesign", "main")
        (git.repo / "other.txt").write_text("x\n")
        git.run("add", "other.txt")
        git.run("commit", "-qm", "redesign")
        sha = git.head("redesign")
        git.run("switch", "main")
        store.enqueue("redesign", sha)
        assert store.inherited_protected(store.status()["jobs"][1]) == []
        git.run("switch", "-c", "redesign-2", "redesign")
        (git.repo / "other.txt").write_text("y\n")
        git.run("commit", "-qam", "again")
        sha2 = git.head("redesign-2")
        git.run("switch", "main")
        store.enqueue("redesign-2", sha2, inherits=held)
        carried = store.inherited_protected(store.status()["jobs"][2])
        assert [item["id"] for item in carried] == ["sec-1"]
        # A named parent need not be a forest hold, and its own sources follow transitively.
        git.run("switch", "-c", "redesign-3", "redesign-2")
        git.run("commit", "--quiet", "--allow-empty", "-m", "third")
        sha3 = git.head("redesign-3")
        git.run("switch", "main")
        third = store.enqueue("redesign-3", sha3, inherits=3)
        assert [item["id"] for item in store.inherited_protected(third)] == ["sec-1"]
        with pytest.raises(ValueError, match="unknown job"):
            store.enqueue("redesign-3", sha3, inherits=3 + 1)
        with pytest.raises(ValueError, match="without that inheritance"):
            store.enqueue("redesign", sha, inherits=held)


def test_unclassified_queue_entry_can_be_reclassified_with_an_audit_row(queue):
    git, cfg = queue
    cfg["design_gate"] = True
    with store_of(cfg) as store:
        store.enqueue("feature", git.head("feature"))
        job = store.status()["jobs"][0]
        store.update(job, "design-needed", detail="needs design")
        store.enqueue("feature", git.head("feature"), kind="routine")
        after = store.status()["jobs"][0]
        assert (after["kind"], after["phase"], after["sha"]) == ("routine", "review", job["sha"])
        audit = store.db.execute("SELECT body FROM evidence WHERE phase='reclassified'").fetchall()
        assert json.loads(audit[0]["body"])["to"] == "routine"
        with pytest.raises(ValueError, match="different task kind"):
            store.enqueue("feature", git.head("feature"), kind="feature")


def test_repeat_class_is_scoped_to_the_lens_that_raised_it(queue):
    git, cfg = queue
    cfg.update(max_rounds=3, repeat_class_rounds=2)
    calls = {"worker": 0}

    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "worker":
            calls["worker"] += 1
            (cwd / "file.txt").write_text(f"repair {calls['worker']}\n")
            return ""
        if role == "reviewer":
            general = argv == cfg["reviewer"]
            round_one, round_two = calls["worker"] == 0, calls["worker"] == 1
            if (general and round_one) or (not general and round_two):
                return review_response(stdin, [{"id": "case", "risk": "other", "detail": "Fixture.",
                                                "finding_class": "coupling"}])
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    result = engine_run(cfg, "feature", git.head("feature"), runner)
    # The same name from two different lenses is not one recurring mechanism.
    assert result["counts"] == {"candidate": 1}
    assert result["metrics"]["forest_escalations"] == 0


def test_rejected_partial_receipt_still_preserves_its_protected_finding(queue):
    git, cfg = queue

    def partial(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        if role == "reviewer":
            request = json.loads(stdin.splitlines()[-1])["request"]
            return json.dumps(dict(request=request, status="partial", tool_version="fixture",
                                   reviewed_paths=[], diagnostics=["cut off"],
                                   findings=[{"id": "sec-9", "risk": "security", "detail": "Leaks a key.",
                                              "finding_class": "secret-exposure"}], resolved={}))
        return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)

    result = engine_run(cfg, "feature", git.head("feature"), partial)
    assert result["counts"] == {"blocked": 1}
    with store_of(cfg) as store:
        kept = store.protected(result["jobs"][0]["id"])
    assert [(item["id"], item["lens"]) for item in kept] == [("sec-9", "general")]


def test_status_exposes_runnable_work_and_its_age(queue):
    git, cfg = queue
    with store_of(cfg) as store:
        assert store.status()["metrics"]["oldest_runnable_age_seconds"] is None
        store.enqueue("feature", git.head("feature"))
        time.sleep(0.02)
        metrics = store.status()["metrics"]
        assert metrics["runnable"] == 1 and metrics["oldest_runnable_age_seconds"] >= 0
        job = store.status()["jobs"][0]
        store.update(job, "review", attempts=1, next_at=time.time() + 600)
        metrics = store.status()["metrics"]
        assert metrics["queue_length"] == 1 and metrics["runnable"] == 0


def test_lens_table_rejects_names_that_never_apply_to_implementation_review(queue, tmp_path):
    from core.loop.config import config
    from core.loop.setup import repository_profile

    git, cfg = queue
    profile = next((tmp_path / "config").rglob("*.toml"))
    original = profile.read_text()
    for name in ("architect", "sre", "security", "general"):
        profile.write_text(original + f'\n[loop.lenses]\n{name} = ["**/*.py"]\n')
        with pytest.raises(ValueError, match=name):
            config(profile)
    profile.write_text(original + '\n[loop.lenses]\ndb = ["**/*.sql"]\n')
    assert config(profile)["lenses"] == {"db": ["**/*.sql"]}
    assert repository_profile  # imported to keep the fixture's profile path stable


def design_blob(git, text):
    return git.run("hash-object", "-w", "--stdin", input=text).stdout.strip()


FULL = ("## Canonical design\nA.\n## Failure modes and rollback\nB.\n## Latency budget\nC.\n"
        "## Memory budget\nD.\n## Test plan\nE.\n")


def test_design_sections_are_read_as_markdown_structure(queue):
    git, _ = queue
    validate_document(git, design_blob(git, "# T\n" + FULL))
    fenced = "# T\n```md\n" + FULL + "```\n"
    with pytest.raises(ValueError, match="need content"):
        validate_document(git, design_blob(git, fenced))
    with pytest.raises(ValueError, match="more than once"):
        validate_document(git, design_blob(git, FULL + "## Test plan\nAgain.\n"))
    placeholder = FULL.replace("A.", "<!-- describe the design -->")
    with pytest.raises(ValueError, match="canonical design"):
        validate_document(git, design_blob(git, placeholder))


def test_start_refuses_a_daemon_log_that_is_not_private(tmp_path, monkeypatch):
    from core.loop import daemon

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(daemon, "validate_execution", lambda cfg: None)
    log = state / "daemon.log"
    log.write_text("old")
    log.chmod(0o644)
    cfg = dict(state=state, policy_id="p", profile=tmp_path / "p.toml")
    with pytest.raises(ValueError, match="daemon log"):
        daemon.start(cfg)
    log.chmod(0o600)
    (state / "second-name").hardlink_to(log)
    with pytest.raises(ValueError, match="daemon log"):
        daemon.start(cfg)
