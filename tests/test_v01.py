import subprocess
import tomllib

import pytest


def test_global_install_preserves_bytes_and_backs_up(tmp_path, monkeypatch):
    from core.loop.setup import global_rules

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    targets = [tmp_path / ".claude/CLAUDE.md", tmp_path / ".codex/AGENTS.md"]
    original = b"personal rule\r\nlast line"
    for target in targets:
        target.parent.mkdir()
        target.write_bytes(original)
    global_rules(dry_run=True)
    assert all(target.read_bytes() == original for target in targets)
    assert not (tmp_path / "state").exists()
    global_rules()
    installed = [target.read_bytes() for target in targets]
    global_rules()
    assert [target.read_bytes() for target in targets] == installed
    assert all(blob.startswith(original) and b"rules snapshot sha256=" in blob for blob in installed)
    assert len(list((tmp_path / "state").rglob("*.bak"))) == 2
    global_rules(uninstall=True)
    assert all(target.read_bytes() == original for target in targets)
    assert len(list((tmp_path / "state").rglob("*.bak"))) == 4


def test_global_install_rejects_bad_markers_before_any_write(tmp_path, monkeypatch):
    from core.loop.setup import START, global_rules

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    codex = tmp_path / ".codex/AGENTS.md"
    codex.parent.mkdir()
    codex.write_bytes(START + b"\n")
    with pytest.raises(ValueError, match="managed block"):
        global_rules()
    assert not (tmp_path / ".claude").exists()
    assert codex.read_bytes() == START + b"\n"


def test_global_uninstall_preserves_personal_text_below_block(tmp_path, monkeypatch):
    from core.loop.setup import global_rules

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / ".claude/CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(b"personal before")
    global_rules()
    with target.open("ab") as file:
        file.write(b"personal after")
    global_rules(uninstall=True)
    assert target.read_bytes() == b"personal before\n\npersonal after"


def test_init_never_overwrites_and_suggests_detected_runner(tmp_path, monkeypatch):
    from core.loop.setup import repository_profile

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-qb", "trunk", str(repo)], check=True)
    (repo / "pytest.ini").touch()
    result = repository_profile(repo)
    before = result.read_bytes()
    assert b"pytest" in before
    assert tomllib.loads(before.decode())["loop"]["target"] == "trunk"
    with pytest.raises(FileExistsError):
        repository_profile(repo)
    assert result.read_bytes() == before


def test_repository_config_cannot_choose_commands(tmp_path, monkeypatch):
    from core.loop.config import config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    profile = tmp_path / "project.toml"
    profile.write_text('[loop]\nreviewer = ["unexpected"]\n')
    with pytest.raises(ValueError, match="trusted profiles"):
        config(profile)


def test_config_inside_git_checkout_is_rejected_even_as_xdg_path(tmp_path, monkeypatch):
    from core.loop.config import config, profile_dir

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    profile = profile_dir() / "inside.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text('[loop]\nrepo = "/elsewhere"\n')
    with pytest.raises(ValueError, match="outside Git checkouts"):
        config(profile)


def test_role_environment_is_deny_by_default(monkeypatch):
    from core.loop.sandbox import environment

    monkeypatch.setenv("KEVIN_TEST_TOKEN", "present")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/a/socket")
    monkeypatch.setenv("PYTHONPATH", "/a/project")
    monkeypatch.setenv("HOME", "/actual/home")
    assert environment([])["HOME"] == "/home/agent"
    assert not {"KEVIN_TEST_TOKEN", "SSH_AUTH_SOCK", "PYTHONPATH"} & environment([]).keys()
    assert environment(["KEVIN_TEST_TOKEN"])["KEVIN_TEST_TOKEN"] == "present"
    for name in ("HOME", "AGENT_HEAD", "PYTHONPATH", "SSH_AUTH_SOCK"):
        with pytest.raises(ValueError):
            environment([name])


def test_review_requires_complete_coverage_and_explicit_protected_resolution():
    from core.loop.review import findings

    request = {"lens": "general", "base": "old", "candidate": "new", "paths": ["src/a"]}
    prior = [{"id": "unsafe", "risk": "security", "detail": "Specific evidence", "lens": "general"}]
    good = dict(request=request, status="complete", tool_version="fixture-1", reviewed_paths=["src/a"],
                diagnostics=[], findings=[], resolved={"unsafe": "src/a now rejects access."})
    import json

    assert findings(json.dumps(good), request, prior) == []
    for changes in (
        {"status": "partial"}, {"reviewed_paths": []}, {"request": {**request, "base": "stale"}},
        {"resolved": {}}, {"tool_version": ""}, {"diagnostics": ["diff truncated"]},
    ):
        with pytest.raises(ValueError):
            findings(json.dumps({**good, **changes}), request, prior)


def test_verification_requires_fresh_executed_check_receipt():
    from core.loop.review import verified_check

    request = {"check": "required", "candidate": "current", "run": "fresh"}
    good = dict(request=request, status="passed", executed=True, tool_version="fixture-1", execution_id="run-1")
    assert verified_check(good, request)["executed"]
    for bad in ({}, {**good, "executed": False}, {**good, "execution_id": ""},
                {**good, "request": {**request, "run": "previous"}}):
        with pytest.raises(ValueError):
            verified_check(bad, request)


def test_sandbox_has_no_host_home_mount_or_verify_credentials(tmp_path, monkeypatch):
    from core.loop.sandbox import command_line, execution_policy

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv("HOME", str(private))
    monkeypatch.setenv("KEVIN_TEST_TOKEN", "fixture")
    policy = execution_policy({"env": {"reviewer": ["KEVIN_TEST_TOKEN"]}})
    review, reviewer_env = command_line(["python3"], workspace, "reviewer", policy)
    worker, worker_env = command_line(["python3"], workspace, "worker", policy)
    verify, verify_env = command_line(["python3"], None, "verify", policy, bundle=tmp_path / "candidate.bundle")
    assert review[review.index("/workspace") - 2] == "--ro-bind"
    assert worker[worker.index("/workspace") - 2] == "--bind"
    assert worker[worker.index("/workspace/.git") - 2] == "--ro-bind"
    assert review[review.index("--chdir") + 1] == "/home/agent"
    assert worker[worker.index("--chdir") + 1] == "/home/agent"
    assert "/workspace" not in verify
    assert verify[verify.index("/candidate.bundle") - 2] == "--ro-bind"
    assert "--unshare-all" in verify and "--share-net" not in verify
    assert all(str(private) not in args for args in (review, worker, verify))
    assert reviewer_env["KEVIN_TEST_TOKEN"] == "fixture"
    assert "KEVIN_TEST_TOKEN" not in worker_env and "KEVIN_TEST_TOKEN" not in verify_env
    assert not any("AGENT_QUEUE" in env for env in (reviewer_env, worker_env, verify_env))
    with pytest.raises(ValueError, match="limited to agent"):
        execution_policy({"env": {"verify": ["KEVIN_TEST_TOKEN"]}})


def test_unavailable_sandbox_stops_before_loading_jobs(tmp_path, monkeypatch):
    from core.loop import daemon

    def denied():
        raise ValueError("Bubblewrap isolation unavailable")

    monkeypatch.setattr(daemon, "probe", denied)
    with pytest.raises(ValueError, match="isolation unavailable"):
        daemon.start(dict(state=tmp_path / "state", reviewer=["review"], checks={"test": ["verify"]}))
    assert not (tmp_path / "state").exists()


def test_live_sandbox_hides_host_paths_and_state(tmp_path, monkeypatch):
    from threading import Event
    from core.loop.sandbox import execute, execution_policy, probe

    try:
        probe()
    except ValueError as error:
        pytest.skip(str(error))

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    private = tmp_path / "private"
    private.mkdir()
    marker = private / "credential-fixture"
    marker.touch()
    monkeypatch.setenv("HOME", str(private))
    monkeypatch.setenv("KEVIN_TEST_TOKEN", "fixture")
    script = """
import os
from pathlib import Path
import sys
assert not Path(sys.argv[1]).exists()
assert os.environ['HOME'] == '/home/agent'
assert 'KEVIN_TEST_TOKEN' not in os.environ
assert Path('/workspace/.git').is_dir()
print('isolated')
"""
    assert execute(["python3", "-I", "-c", script, str(marker)], workspace, Event(), 5,
                   role="reviewer", policy=execution_policy({})).strip() == "isolated"
