"""Unsigned broker-client profiles and candidate evidence through the coordinator."""

import json
import time

import pytest

from core.loop.__main__ import doctor, main
from core.loop.config import config
from core.loop.daemon import ensure_state, validate_execution
from core.loop.engine import Engine, export_candidate
from core.loop.git import Git
from core.loop.runtime import CommandFailed
from core.loop.sandbox import execute, isolation_status
from core.loop.setup import repository_profile
from core.loop.store import Store


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git = Git(repo)
    git.run("init", "--quiet", "--template=", "--initial-branch=main")
    (repo / "Makefile").write_text(
        "test:\n\ttest -f file.txt && test ! -e .git\n"
        "\tpython3 -c 'import os; assert os.statvfs(\".\").f_flag & os.ST_RDONLY'\n"
    )
    git.run("add", "Makefile")
    git.run("commit", "-qm", "base")
    git.run("switch", "-c", "feature")
    (repo / "file.txt").write_text("feature\n")
    git.run("add", "file.txt")
    git.run("commit", "-qm", "feature")
    git.run("switch", "main")
    path = repository_profile(repo, agent="codex")
    client = tmp_path / "client"
    client.write_text("""#!/bin/sh
set -eu
test "$1" = review
test "$PWD" = /home/agent
test "$HOME" = /home/agent
test -f "$AGENT_WORKSPACE/file.txt"
printf '%s\\n' '{"status":"complete","findings":[],"resolved":{}}'
""")
    client.chmod(0o700)
    text = path.read_text()
    path.write_text(text.replace('# codex-client = "/absolute/path/to/codex-client"',
                                 f'codex-client = {json.dumps(str(client))}'))
    return git, path


def drive(git, profile, runner):
    cfg = config(profile)
    ensure_state(cfg["state"])
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(
            cfg, store, runner=runner) as engine:
        store.enqueue("feature", git.head("feature"))
        end = time.monotonic() + 15
        while time.monotonic() < end:
            engine.tick()
            job = store.status()["jobs"][0]
            if job["phase"] != "review":
                return job
            time.sleep(0.02)
    pytest.fail("job did not settle")


@pytest.mark.parametrize("exit_code", [0, 7])
def test_unsigned_review_and_check_evidence(profile, tmp_path, exit_code):
    git, path = profile

    def fixture_runner(argv, work, cancel, timeout, *, role, stdin, **kwargs):
        if role == "reviewer":
            assert argv == ["/tools/codex-client", "review"]
            return json.dumps({"status": "complete", "findings": [], "resolved": {}})
        assert role == "check" and argv == ["make", "test"]
        assert (work / "file.txt").read_text() == "feature\n"
        if exit_code:
            raise CommandFailed(exit_code)
        return "passed"

    job = drive(git, path, fixture_runner)
    evidence = job["evidence"]
    check = evidence["checks"][-1]
    assert check["exit_code"] == exit_code
    if exit_code:
        assert job["phase"] == "blocked" and check["status"] == "failed"
    else:
        assert job["phase"] == "candidate" and check["source"] == "local-unsigned"
        assert check["tree"] == check["request"]["tree"]
        assert len(evidence["reviews"]) == 2
        output = tmp_path / "ready.bundle"
        export_candidate(config(path), evidence, output)
        git.run("bundle", "unbundle", str(output))
        assert git.tree(evidence["candidate"]) == check["tree"]


def test_unsigned_profile_on_live_sandbox(profile):
    available, reason = isolation_status()
    if not available:
        pytest.skip(reason)
    git, path = profile
    job = drive(git, path, execute)
    assert job["phase"] == "candidate", job["detail"]


def test_doctor_and_start_refuse_missing_isolation(profile, monkeypatch, capsys):
    _, path = profile
    from core.loop import sandbox

    monkeypatch.setattr(sandbox, "isolation_status", lambda: (False, "unsupported namespaces"))
    report = doctor(path)
    assert not report["ok"] and report["mode"] == "isolated/local-evidence"
    assert next(c for c in report["checks"] if c["name"] == "bubblewrap isolation")["required"]
    assert main(["--config", str(path), "doctor"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    with pytest.raises(ValueError, match="unsupported namespaces"):
        validate_execution(config(path))


def test_status_says_when_the_daemon_is_not_running(profile, capsys):
    git, path = profile
    assert main(["--config", str(path), "enqueue", "feature", git.head("feature")]) == 0
    capsys.readouterr()
    assert main(["--config", str(path), "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["running"] is False and "not running" in report["note"]


def test_failed_reviewer_output_requires_resolution_on_retry(profile):
    from core.loop.runtime import Retry

    git, path = profile
    path.write_text(path.read_text().replace('[loop]\n', '[loop]\nretry_initial_seconds = 1\n'))
    finding = {"id": "scope", "risk": "security", "detail": "Check the scope.", "finding_class": "review-scope"}
    calls = []

    def fixture_runner(argv, work, cancel, timeout, *, role, stdin, **kwargs):
        if role == "reviewer":
            calls.append(json.loads(stdin.splitlines()[-1]))
            if len(calls) == 1:
                error = Retry("temporary failure")
                error.output = json.dumps({"findings": [finding]})
                raise error
            return json.dumps({"status": "complete", "findings": [], "resolved": {}})
        return "passed"

    job = drive(git, path, fixture_runner)
    assert job["phase"] == "blocked"
    assert calls[1]["prior_protected"] == [{**finding, "lens": "general"}]
    assert "explicit resolution" in job["detail"]
