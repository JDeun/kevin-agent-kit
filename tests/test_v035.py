"""Regression tests for the 0.3.4 release-check scenarios: stop integrity and install trust."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Event

import pytest

from core.loop.engine import Engine
from core.loop.runtime import run
from core.loop.store import Store
from core.verdict import Scanner, scan
from tests.test_loop import clean_run, queue, review_response  # noqa: F401
from tests.test_release_fixes import revise_feature, settle
from tests.test_v034 import FINDING, no_rerun, start_and_stop, stopping_reviewer

ROOT = Path(__file__).resolve().parents[1]


def store_of(cfg):
    return Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect()


def cut_before_closing_brace(stdin):
    """Every field of the finding is printed; the stop lands before its closing brace."""
    text = review_response(stdin, [FINDING])
    end = text.index("}", text.index('"findings"'))
    return text[:end]


def cut_after_complete_finding(stdin):
    text = review_response(stdin, [FINDING])
    return text[:text.index("}", text.index('"findings"')) + 1]


# Stop integrity ------------------------------------------------------------------------------
def test_stop_inside_a_finding_keeps_the_finding_and_interrupts(queue):  # noqa: F811
    git, cfg = queue
    start_and_stop(cfg, git, stopping_reviewer(cut_before_closing_brace))
    with store_of(cfg) as store, Engine(cfg, store, runner=no_rerun) as engine:
        assert store.status()["counts"] == {"interrupted": 1}
        assert [item["id"] for item in store.protected(1)] == ["protected"]
        engine.tick()
        assert not engine.active


def test_output_past_the_limit_keeps_a_complete_finding_and_interrupts(queue):  # noqa: F811
    git, cfg = queue
    start_and_stop(cfg, git, stopping_reviewer(cut_after_complete_finding, truncated=True))
    with store_of(cfg) as store, Engine(cfg, store, runner=no_rerun):
        assert store.status()["counts"] == {"interrupted": 1}
        assert [item["id"] for item in store.protected(1)] == ["protected"]


def test_real_supervisor_stop_of_a_partial_document_interrupts(queue):  # noqa: F811
    git, cfg = queue
    started = Event()

    def runner(argv, cwd, cancel, timeout, *, role, stdin, watch=None, **_):
        partial = cut_after_complete_finding(stdin)
        script = f"import sys, time\nsys.stdout.write({partial!r})\nsys.stdout.flush()\ntime.sleep(30)\n"
        started.set()
        return run([sys.executable, "-I", "-c", script], cwd, cancel, timeout,
                   capture_failed_output=True, watch=watch)

    with store_of(cfg) as store, Engine(cfg, store, runner=runner) as engine:
        store.enqueue("feature", git.head("feature"))
        engine.tick()
        assert started.wait(5)
        # The finding reaches the journal while the reviewer is still running.
        for _ in range(100):
            if store.protected(1):
                break
            Event().wait(0.05)
        assert [item["id"] for item in store.protected(1)] == ["protected"]
    with store_of(cfg) as store, Engine(cfg, store, runner=no_rerun):
        assert store.status()["counts"] == {"interrupted": 1}


def interrupted_queue(cfg, git):
    start_and_stop(cfg, git, stopping_reviewer(cut_after_complete_finding, truncated=True))


def test_same_sha_after_an_interruption_resumes_with_the_finding(queue):  # noqa: F811
    git, cfg = queue
    interrupted_queue(cfg, git)
    with store_of(cfg) as store:
        with Engine(cfg, store, runner=no_rerun):
            assert store.enqueue("feature", git.head("feature"))["phase"] == "interrupted"
            with pytest.raises(ValueError, match="retry requires"):
                store.retry(1)
        store.resume(1)
        assert [item["id"] for item in store.inherited_protected(store.status()["jobs"][0])] == ["protected"]
        # A clean review that does not resolve the carried finding cannot produce a candidate.
        assert settle(cfg, store, clean_run, 1)["phase"] == "blocked"
        with pytest.raises(ValueError, match="resume requires"):
            store.resume(1)


def test_successor_sha_after_an_interruption_inherits_the_finding(queue):  # noqa: F811
    git, cfg = queue
    interrupted_queue(cfg, git)
    with store_of(cfg) as store:
        with Engine(cfg, store, runner=no_rerun):
            pass
        job = store.enqueue("feature", revise_feature(git, "revised\n"))
        assert [item["id"] for item in store.inherited_protected(job)] == ["protected"]
        assert settle(cfg, store, clean_run, job["id"])["phase"] == "blocked"


def test_inherit_from_an_interrupted_job_supersedes_it(queue):  # noqa: F811
    git, cfg = queue
    interrupted_queue(cfg, git)
    git.run("switch", "-c", "successor", "feature")
    git.run("commit", "--quiet", "--allow-empty", "-m", "successor")
    sha = git.head("successor")
    git.run("switch", "main")
    with store_of(cfg) as store:
        with Engine(cfg, store, runner=no_rerun):
            pass
        job = store.enqueue("successor", sha, inherits=1)
        assert [item["id"] for item in store.inherited_protected(job)] == ["protected"]
        assert {row["id"]: row["phase"] for row in store.status()["jobs"]} == {1: "superseded", 2: "review"}
        with pytest.raises(ValueError, match="unknown job"):
            store.enqueue("successor", sha, inherits=99)


def test_journal_and_attempts_cannot_be_erased(queue):  # noqa: F811
    git, cfg = queue
    interrupted_queue(cfg, git)
    with store_of(cfg) as store:
        with Engine(cfg, store, runner=no_rerun):
            pass
        report = store.status()["jobs"][0]["pending_review"]
        assert report["reason"] == "output truncated" and report["journaled"] == 1
        assert FINDING["detail"] in store.held(1)[0]["output"]
        for statement in ("DELETE FROM finding_journal", "UPDATE finding_journal SET body='{}'",
                          "DELETE FROM review_attempts"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                store.db.execute(statement)
        store.db.rollback()
        # Moving the job to another phase keeps the open attempt and its evidence.
        store.update(store.status()["jobs"][0], "blocked", detail="operator")
        assert store.held(1) and [item["id"] for item in store.protected(1)] == ["protected"]
        assert store.ready() == []


def test_job_with_an_open_attempt_is_never_dispatched(queue):  # noqa: F811
    git, cfg = queue
    with store_of(cfg) as store:
        job = store.enqueue("feature", git.head("feature"))
        store.open_attempt(job["id"], {"run": "r", "candidate": "c", "lens": "general", "invocation": "i"}, 1)
        assert store.ready() == []
        with Engine(cfg, store, runner=no_rerun) as engine:
            engine.tick()
            assert store.status()["counts"] == {"interrupted": 1}


def test_settled_stop_closes_the_attempt_and_drops_raw_output(queue):  # noqa: F811
    git, cfg = queue
    start_and_stop(cfg, git, stopping_reviewer(lambda stdin: review_response(stdin, [FINDING])))
    with store_of(cfg) as store:
        assert store.status()["counts"] == {"review": 1} and store.held(1) == []
        row = store.db.execute("SELECT outcome, output_sha256 FROM review_attempts").fetchone()
        assert row["outcome"] == "stopped" and len(row["output_sha256"]) == 64
        assert store.db.execute("SELECT count(*) FROM review_output").fetchone()[0] == 0


def test_scanner_journals_each_finding_as_its_object_closes():
    text = json.dumps({"findings": [FINDING, {**FINDING, "id": "second"}], "status": "complete"})
    scanner = Scanner()
    seen = []
    for index, char in enumerate(text):
        found = scanner.feed(char)
        seen.extend((item["id"], index) for item in found)
    assert [name for name, _ in seen] == ["protected", "second"]
    assert seen[0][1] < text.index("second")
    assert scanner.finish() == [] and scanner.settled
    assert scan(text) == ([FINDING, {**FINDING, "id": "second"}], True)


@pytest.mark.parametrize(("text", "ids", "settled"), [
    ("", [], True),
    ("no findings here", [], True),
    ('{"findings":[{"id":"a","risk":"security","detail":"d"}', ["a"], False),
    ('{"findings":[{"id":"a","risk":"security","detail":"d"', ["a"], False),
    ('{"findings":[{"id":"a","risk":"security","detail":"d",', ["a"], False),
    ('{"findings":[{"id":"a","risk":"security","detail":"cut', [], False),
    ('{"findings":[{"id":"a","risk":"security"', [], False),
    ('{"findings":[{"id":"a","risk":"other","detail":"d"', [], False),
    ('{"findings":[],"status":"comp', [], False),
    ('{"findings":[]}', [], True),
])
def test_scan_reports_whether_the_output_ended_inside_a_structure(text, ids, settled):
    found, ended = scan(text)
    assert [item["id"] for item in found] == ids and ended is settled


def test_queue_from_0_3_4_migrates_observations_and_pending_requests(queue):  # noqa: F811
    git, cfg = queue
    path = cfg["state"] / "queue.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE project (id TEXT PRIMARY KEY, target TEXT NOT NULL);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, branch TEXT NOT NULL, sha TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'review', attempts INTEGER NOT NULL DEFAULT 0,
            next_at REAL NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'unclassified', inherits INTEGER REFERENCES jobs,
            created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0, UNIQUE(branch, sha));
        CREATE TABLE protected_observations (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
            run TEXT NOT NULL, candidate TEXT NOT NULL, round_number INTEGER NOT NULL,
            lens TEXT NOT NULL, invocation TEXT NOT NULL, findings TEXT NOT NULL);
        CREATE TABLE pending_reviews (job_id INTEGER PRIMARY KEY, run TEXT NOT NULL,
            candidate TEXT NOT NULL, lens TEXT NOT NULL, invocation TEXT NOT NULL);
    """)
    db.execute("INSERT INTO project VALUES (?,?)", (cfg["repo_id"], cfg["target"]))
    db.execute("INSERT INTO jobs (branch, sha) VALUES ('feature', ?)", (git.head("feature"),))
    db.execute("INSERT INTO protected_observations VALUES (1,1,'r','c',1,'general','old',?)",
               (json.dumps([FINDING]),))
    db.execute("INSERT INTO pending_reviews VALUES (1,'r','c','security','pending')")
    db.commit()
    db.close()
    path.chmod(0o600)
    with store_of(cfg) as store, Engine(cfg, store, runner=no_rerun):
        assert [item["id"] for item in store.protected(1)] == ["protected"]
        assert store.status()["counts"] == {"interrupted": 1}
        assert store.status()["jobs"][0]["pending_review"]["invocation"] == "pending"
        names = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not names & {"protected_observations", "pending_reviews"}


# Install trust -------------------------------------------------------------------------------
@pytest.fixture
def kit(tmp_path, monkeypatch):
    """A separate copy of the kit with its own repository and approved profile."""
    root = tmp_path / "kit"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(
        ".git", ".claude", "dist", "__pycache__", ".pytest_cache", ".hypothesis", "tests"))
    repo = tmp_path / "repository"
    repo.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(root),
           "PYTHONDONTWRITEBYTECODE": "1", "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "XDG_STATE_HOME": str(tmp_path / "state"), "GIT_CONFIG_GLOBAL": "/dev/null"}
    for command in (["init", "--quiet", "--initial-branch=main"],
                    ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--quiet", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *command], cwd=repo, check=True, env=env)

    def invoke(*args):
        return subprocess.run([sys.executable, "-P", "-m", "core.loop", *args], cwd=repo, env=env,
                              capture_output=True, text=True)

    profile = invoke("init")
    assert profile.returncode == 0, profile.stderr

    def loaded():
        """Load the profile in a new process: (policy ID, wiki_landing) or the refusal."""
        code = ("from core.loop.config import config\nimport json\n"
                "cfg = config()\nprint(json.dumps([cfg['policy_id'], cfg['wiki_landing']]))\n")
        result = subprocess.run([sys.executable, "-P", "-c", code], cwd=repo, env=env,
                                capture_output=True, text=True)
        return json.loads(result.stdout) if result.returncode == 0 else result.stderr

    def doctor():
        result = invoke("doctor")
        checks = {item["name"]: item for item in json.loads(result.stdout)["checks"]}
        return result.returncode, checks

    return root, invoke, loaded, doctor


def test_group_writable_project_metadata_is_refused(kit):
    root, invoke, loaded, doctor = kit
    assert isinstance(loaded(), list)
    (root / "project.toml").chmod(0o664)
    assert "untrusted kit file" in loaded()
    code, checks = doctor()
    assert code == 1 and not checks["install"]["ok"] and "untrusted kit file" in checks["install"]["detail"]
    assert not checks["profile"]["ok"]
    assert invoke("status").returncode == 1


def test_changed_install_default_needs_a_new_approval(kit):
    root, invoke, loaded, doctor = kit
    before, landing = loaded()
    assert landing is True
    metadata = root / "project.toml"
    text = metadata.read_text()
    assert text.count("wiki_landing = true") == 1
    metadata.write_text(text.replace("wiki_landing = true", "wiki_landing = false"))
    assert "does not approve the installed kit" in loaded()
    code, checks = doctor()
    assert code == 1 and not checks["profile"]["ok"]
    assert "does not approve" in checks["profile"]["detail"] and checks["install"]["ok"]
    for action in (["status"], ["enqueue", "x", "0" * 40], ["start"]):
        assert invoke(*action).returncode == 1
    assert invoke("stop").returncode == 0
    approved = invoke("init", "--approve-install")
    assert approved.returncode == 0, approved.stderr
    after, landing = loaded()
    assert landing is False and after != before
    assert doctor()[1]["profile"]["ok"]


def test_renamed_rule_file_changes_the_policy(kit):
    root, invoke, loaded, doctor = kit
    before, _ = loaded()
    (root / "rules/secrets.md").rename(root / "rules/secrets-renamed.md")
    assert "does not approve the installed kit" in loaded()
    assert invoke("init", "--approve-install").returncode == 0
    assert loaded()[0] != before


@pytest.mark.parametrize("unsafe", ["rule-mode", "rule-link", "rules-directory-mode", "lens-mode", "nested-fifo"])
def test_every_policy_input_shares_the_trust_check(kit, tmp_path, unsafe):
    root, invoke, loaded, doctor = kit
    if unsafe == "rule-mode":
        (root / "rules/secrets.md").chmod(0o664)
    elif unsafe == "rule-link":
        (root / "rules/link.md").symlink_to(tmp_path / "config")
    elif unsafe == "rules-directory-mode":
        (root / "rules").chmod(0o775)
    elif unsafe == "lens-mode":
        (root / "reviews/lenses/general.md").chmod(0o646)
    else:
        os.mkfifo(root / "rules/pipe.md")
    assert "untrusted kit file" in loaded()


def test_approval_is_recorded_once_and_operator_commands_report_plainly(kit):
    root, invoke, loaded, doctor = kit
    first = invoke("init", "--approve-install")
    profile = Path(first.stdout.strip())
    text = profile.read_text()
    assert text.count("install_sha256") == 1 and invoke("init", "--approve-install").returncode == 0
    assert profile.read_text() == text and oct(profile.stat().st_mode & 0o777) == "0o600"
    # A profile written before 0.3.5 has no approval line and gains one below [loop].
    profile.write_text("\n".join(line for line in text.splitlines() if "install_sha256" not in line) + "\n")
    assert "does not approve the installed kit" in loaded()
    assert invoke("init", "--approve-install").returncode == 0 and isinstance(loaded(), list)
    assert json.loads(invoke("evidence", "1").stdout) == []
    refused = invoke("resume", "1")
    assert refused.returncode == 1 and "resume requires an interrupted job" in refused.stderr
    code, checks = doctor()
    assert checks["install"]["ok"] and checks["profile"]["ok"]


def test_launcher_imports_only_the_snapshot(tmp_path):
    from scripts.package import write_launcher

    package = tmp_path / "fixture_kit"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "helper.py").write_text("VALUE = 'snapshot'\n")
    (package / "__main__.py").write_text(
        "from pathlib import Path\n"
        "here = Path(__file__).parent\n"
        "(here / 'helper.py').write_text(\"VALUE = 'edited after the snapshot'\\n\")\n"
        "(here / 'late.py').write_text('VALUE = 1\\n')\n"
        "from . import helper\n"
        "print(helper.VALUE, len(__spec__.loader.kit_snapshot.digest))\n"
        "try:\n"
        "    import fixture_kit.late\n"
        "except ModuleNotFoundError as error:\n"
        "    print('refused', error.name)\n"
    )
    launcher = tmp_path / "bin/fixture"
    launcher.parent.mkdir()
    write_launcher(launcher, "fixture_kit", ".")
    result = subprocess.run([launcher], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["snapshot 64", "refused fixture_kit.late"]


def test_design_review_saves_findings_as_they_appear_and_keeps_unsettled_output(queue):  # noqa: F811
    from core.loop.runtime import Retry

    repo, cfg = queue
    cfg["lens_reviewers"].update(architect=["architect-fixture"], sre=["sre-fixture"])
    repo.run("switch", "-c", "planned", "main")
    design = repo.repo / "design.md"
    design.write_text("\n".join(f"## {title}\nFixture content." for title in (
        "Canonical design", "Failure modes and rollback", "Latency budget", "Memory budget", "Test plan")))
    repo.run("add", design.name)
    repo.run("commit", "-m", "propose design")
    saved = []

    def runner(argv, cwd, cancel, timeout, *, role, stdin, watch, **_):
        if json.loads(stdin.splitlines()[-1])["request"]["lens"] == "architect":
            return review_response(stdin)
        partial = cut_after_complete_finding(stdin)
        watch(partial)
        with store_of(cfg) as other:
            saved.append(other.design("planned")["open_protected"]["sre"])
        expired = Retry("command timed out")
        expired.output, expired.output_truncated = partial, False
        raise expired

    with store_of(cfg) as store, Engine(cfg, store, runner=runner) as engine:
        with pytest.raises(Retry):
            engine.design_review("planned", repo.head("planned"), design.name)
        report = store.design("planned")
    # The finding was in the report before the reviewer ended.
    assert [item["id"] for item in saved[0]] == ["protected"]
    assert report["status"] == "incomplete"
    assert [item["id"] for item in report["open_protected"]["sre"]] == ["protected"]
    assert report["unsettled"][-1]["reason"] == "output is not one complete JSON document"
    assert FINDING["detail"] in report["unsettled"][-1]["output"]
    # The next design review keeps that evidence and must resolve the finding.
    with store_of(cfg) as store, Engine(cfg, store, runner=lambda *a, stdin, **k: review_response(stdin)) as engine:
        with pytest.raises(ValueError, match="explicit resolution"):
            engine.design_review("planned", repo.head("planned"), design.name)
        assert FINDING["detail"] in store.design("planned")["unsettled"][-1]["output"]


def stray_quote_then_cut(stdin):
    """Prose with an odd number of quotes before the document, cut after a complete finding."""
    return 'Note: the panel is 5" wide.\n' + cut_after_complete_finding(stdin)


def cut_inside_detail(stdin):
    text = review_response(stdin, [FINDING])
    return text[:text.index(FINDING["detail"]) + 6]


def test_stray_quote_before_the_document_cannot_settle_a_cut_review(queue):  # noqa: F811
    git, cfg = queue
    assert scan('5" {"findings":[{"id":"a","risk":"security","detail":"d"}')[1] is False
    assert scan('5" {"findings":[{"id":"a","risk":"security","detail":"d"}]}')[1] is False
    start_and_stop(cfg, git, stopping_reviewer(stray_quote_then_cut))
    with store_of(cfg) as store, Engine(cfg, store, runner=no_rerun):
        assert store.status()["counts"] == {"interrupted": 1}
        assert FINDING["detail"] in store.held(1)[0]["output"]


@pytest.mark.parametrize("output", [cut_inside_detail, lambda stdin: review_response(
    stdin, [{"id": "bad id", "risk": "security", "detail": "Malformed but protected."}])])
def test_finding_the_journal_cannot_hold_becomes_an_obligation(queue, output):  # noqa: F811
    git, cfg = queue
    start_and_stop(cfg, git, stopping_reviewer(output))
    with store_of(cfg) as store:
        with Engine(cfg, store, runner=no_rerun):
            assert store.status()["counts"] == {"interrupted": 1}
        [stand_in] = store.protected(1)
        assert stand_in["id"].startswith("unread-") and stand_in["risk"] == "honesty"
        assert "protected" in stand_in["detail"]
        store.resume(1)
        # Resuming does not drop it: a clean review without a resolution is refused.
        assert settle(cfg, store, clean_run, 1)["phase"] == "blocked"


def test_successor_already_reviewed_cannot_supersede_an_interrupted_job(queue):  # noqa: F811
    git, cfg = queue
    interrupted_queue(cfg, git)
    git.run("switch", "-c", "successor", "feature")
    git.run("commit", "--quiet", "--allow-empty", "-m", "successor")
    sha = git.head("successor")
    git.run("switch", "main")
    with store_of(cfg) as store:
        with Engine(cfg, store, runner=no_rerun):
            pass
        store.resume(1)
        job = store.enqueue("successor", sha, inherits=1)
        store.update(job, "candidate")
        store.update(store.status()["jobs"][0], "interrupted")
        with pytest.raises(ValueError, match="already reviewed"):
            store.enqueue("successor", sha, inherits=1)
        assert store.status()["jobs"][0]["phase"] == "interrupted"


def test_output_a_failed_write_missed_is_stored_with_the_next_write():
    from core.loop.engine import ReviewOutput

    written = []

    def record(text, found):
        if not written:
            written.append(None)
            raise sqlite3.OperationalError("busy")
        written.append((text, [item["id"] for item in found]))

    observed = ReviewOutput(record, "invocation")
    text = json.dumps({"findings": [FINDING]})
    with pytest.raises(sqlite3.OperationalError):
        observed(text[:-1])
    observed.conclude(text)
    assert written[1:] == [(text, ["protected"])] and observed.settled


def test_retry_refuses_a_job_with_an_open_attempt(queue):  # noqa: F811
    git, cfg = queue
    with store_of(cfg) as store:
        job = store.enqueue("feature", git.head("feature"))
        store.open_attempt(job["id"], {"run": "r", "candidate": "c", "lens": "general", "invocation": "i"}, 1)
        store.update(job, "blocked")
        with pytest.raises(ValueError, match="retry requires"):
            store.retry(job["id"])
        assert store.status()["jobs"][0]["pending_review"]["invocation"] == "i"
