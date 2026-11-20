"""Regression tests for the real-CLI path: doctor, unisolated mode and local evidence."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

import pytest

from core.agents import AgentError, clean_findings, last_object, review
from core.loop.__main__ import doctor, main
from core.loop.config import config
from core.loop.daemon import ensure_state, validate_execution
from core.loop.engine import Engine
from core.loop.git import Git
from core.loop.runtime import module_command
from core.loop.sandbox import execute, execution_policy
from core.loop.setup import repository_profile
from core.loop.store import Store

FAKE = """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fake-cli 1.2.3")
    raise SystemExit(0)
prompt = sys.stdin.read()
write = ("workspace-write" in args) or ("acceptEdits" in args)
if write:
    open("file.txt", "w").write("fixed\\n")
    print("edited")
    raise SystemExit(0)
mode = open("MODE").read().strip() if __import__("os").path.exists("MODE") else ""
text = open("file.txt").read()
if mode == "limit":
    sys.stderr.write("429 rate limit exceeded")
    raise SystemExit(1)
if mode == "prose":
    print("I looked at it and it seems fine.")
    raise SystemExit(0)
findings = [] if "fixed" in text else [
    {"id": "needs-fix", "risk": "other", "detail": "File must say fixed.", "finding_class": "wrong-text"}]
print("Reasoning first {not json}\\n" + json.dumps({"status": "complete", "findings": findings, "resolved": {}}))
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name in ("codex", "claude"):
        path = bin_dir / name
        path.write_text(FAKE)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


def prepare(tmp_path, monkeypatch, makefile):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repository"
    repo.mkdir()
    git = Git(repo)
    git.run("init", "--quiet", "--template=", "--initial-branch=main")
    (repo / "Makefile").write_text(makefile)
    git.run("add", "Makefile")
    git.run("commit", "--quiet", "-m", "base")
    git.run("switch", "-c", "feature")
    (repo / "file.txt").write_text("feature\n")
    git.run("add", "file.txt")
    git.run("commit", "--quiet", "-m", "feature")
    git.run("switch", "main")
    return repo, git


def drive(cfg, sha):
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(cfg, store) as engine:
        store.enqueue("feature", sha)
        end = time.monotonic() + 60
        while time.monotonic() < end:
            engine.tick()
            status = store.status()
            if status["jobs"][0]["phase"] != "review":
                return status
            time.sleep(0.05)
    raise AssertionError("job did not settle")


def test_generated_profile_drives_a_real_cli_and_collects_exit_codes(tmp_path, monkeypatch, fake_cli):
    repo, git = prepare(tmp_path, monkeypatch, "test:\n\ttest -f file.txt\n")
    profile = repository_profile(repo, agent="codex")
    text = profile.read_text()
    assert 'trust = "local"' in text and 'isolation = "none"' in text and '"make", "test"' in text
    cfg = config(profile)
    ensure_state(cfg["state"])
    assert cfg["trust"] == "local" and cfg["execution"]["isolation"] == "none"
    status = drive(cfg, git.head("feature"))
    assert status["counts"] == {"candidate": 1}, status["jobs"][0]["detail"]
    evidence = status["jobs"][0]["evidence"]
    assert evidence["trust"] == "local"
    # The worker repaired the first round's finding, and the second review was clean.
    assert [run["role"] for run in evidence["runs"]].count("worker") == 1
    assert all(run["exit_code"] == 0 for run in evidence["runs"])
    check = evidence["checks"][0]
    assert (check["exit_code"], check["source"], check["status"]) == (0, "local-unsigned", "passed")
    assert all(review["observation"]["source"] == "local-unsigned" for review in evidence["reviews"])
    from core.loop.engine import export_candidate

    bundle = tmp_path / "ready.bundle"
    export_candidate(cfg, evidence, bundle)
    git.run("bundle", "unbundle", str(bundle))
    assert git.run("show", f"{evidence['candidate']}:file.txt").stdout == "fixed\n"


def test_failing_required_check_keeps_its_exit_code(tmp_path, monkeypatch, fake_cli):
    repo, git = prepare(tmp_path, monkeypatch, "test:\n\texit 7\n")
    cfg = config(repository_profile(repo, agent="claude"))
    ensure_state(cfg["state"])
    status = drive(cfg, git.head("feature"))
    job = status["jobs"][0]
    assert job["phase"] == "blocked"
    failed = job["evidence"]["checks"][-1]
    assert failed["status"] == "failed" and failed["exit_code"] in (2, 7)
    assert any(run["exit_code"] not in (0, None) for run in job["evidence"]["runs"])


def test_local_trust_requires_the_isolation_acknowledgement(tmp_path, monkeypatch, fake_cli):
    repo, _ = prepare(tmp_path, monkeypatch, "test:\n\ttrue\n")
    profile = repository_profile(repo, agent="codex")
    profile.write_text(profile.read_text().replace('isolation = "none"', 'isolation = "bwrap"'))
    with pytest.raises(ValueError, match='isolation = "none"'):
        config(profile)


def test_unisolated_execute_maps_tools_and_runs_in_the_checkout(tmp_path):
    from threading import Event

    script = tmp_path / "tool.py"
    script.write_text("#!/usr/bin/python3\nimport os,sys\nprint(os.getcwd(), os.environ['AGENT_WORKSPACE'])\n")
    script.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    policy = execution_policy({"isolation": "none", "tools": {"probe": str(script)}})
    out = execute(["/tools/probe"], work, Event(), 10, role="reviewer", policy=policy)
    assert out.split() == [str(work), str(work)]
    with pytest.raises(ValueError, match="isolation must be"):
        execution_policy({"isolation": "maybe"})


def test_missing_isolation_gives_actionable_guidance(monkeypatch):
    from core.loop import daemon, sandbox

    monkeypatch.setattr(sandbox, "isolation_status", lambda: (False, "Failed to make / slave"))
    with pytest.raises(ValueError, match="isolation = \"none\""):
        sandbox.probe()
    monkeypatch.setattr(daemon, "probe", sandbox.probe)
    with pytest.raises(ValueError, match="Failed to make / slave"):
        validate_execution({"reviewer": ["r"], "checks": {"t": ["t"]}, "execution": {}})


def test_doctor_reports_without_a_profile_and_with_one(tmp_path, monkeypatch, fake_cli, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from core.loop import sandbox

    monkeypatch.setattr(sandbox, "isolation_status", lambda: (False, "no user namespaces"))
    report = doctor(tmp_path / "missing.toml")
    names = {item["name"]: item for item in report["checks"]}
    assert not report["ok"] and not names["bubblewrap isolation"]["ok"]
    assert names["codex cli"]["ok"] and "fake-cli" in names["codex cli"]["detail"]
    assert any("isolation = " in item for item in report["advice"])
    repo, _ = prepare(tmp_path, monkeypatch, "test:\n\ttrue\n")
    profile = repository_profile(repo, agent="codex")
    report = doctor(profile)
    assert report["ok"] and report["mode"] == "unisolated/local-evidence"
    required = {item["name"] for item in report["checks"] if item["required"]}
    assert "bubblewrap isolation" not in required and "openssl" not in required
    monkeypatch.chdir(repo)
    assert main(["--config", str(profile), "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_status_says_when_the_daemon_is_not_running(tmp_path, monkeypatch, fake_cli, capsys):
    repo, git = prepare(tmp_path, monkeypatch, "test:\n\ttrue\n")
    profile = repository_profile(repo, agent="codex")
    monkeypatch.chdir(repo)
    assert main(["--config", str(profile), "enqueue", "feature", git.head("feature")]) == 0
    capsys.readouterr()
    assert main(["--config", str(profile), "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["running"] is False and "not running" in report["note"]


def test_adapter_builds_an_exact_receipt_from_noisy_model_text(tmp_path, monkeypatch, fake_cli):
    (tmp_path / "file.txt").write_text("feature\n")
    monkeypatch.chdir(tmp_path)
    request = {"paths": ["file.txt"], "lens": "general", "finding_class_required": True}
    prompt = "instructions\n" + json.dumps({"request": request, "prior_protected": []}) + "\n"
    receipt = review("codex", prompt, tmp_path)
    assert receipt["request"] == request and receipt["status"] == "complete"
    assert receipt["reviewed_paths"] == ["file.txt"] and receipt["diagnostics"] == []
    assert receipt["findings"][0]["finding_class"] == "wrong-text"
    assert receipt["tool_version"] == "codex fake-cli 1.2.3"
    (tmp_path / "MODE").write_text("prose")
    with pytest.raises(AgentError) as error:
        review("claude", prompt, tmp_path)
    assert error.value.code == 3
    (tmp_path / "MODE").write_text("limit")
    with pytest.raises(AgentError) as error:
        review("codex", prompt, tmp_path)
    assert error.value.code == 75


def test_adapter_helpers_normalise_model_findings():
    assert last_object('{"findings": 1} noise {"findings": [], "x": 2} {bad', "findings") == {"findings": [], "x": 2}
    items = clean_findings([{"id": "bad id!", "risk": "weird", "detail": "d", "finding_class": "Bad"},
                            {"detail": ""}, "x"])
    assert items == [{"id": "finding-1", "risk": "other", "detail": "d", "finding_class": "uncategorized"}]


def test_agent_module_is_dispatched_by_the_cli(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(module_command(root, "core.agents", "versions"), capture_output=True, text=True,
                            cwd=tmp_path)
    assert result.returncode == 0 and set(json.loads(result.stdout)) == {"codex", "claude"}
