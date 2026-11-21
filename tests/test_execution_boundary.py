from pathlib import Path

import pytest

from core.loop import sandbox


def test_all_live_profiles_require_namespaces():
    with pytest.raises(ValueError, match="Bubblewrap"):
        sandbox.execution_policy({"isolation": "none"})


def test_reviewers_cannot_forward_host_environment():
    with pytest.raises(ValueError, match="broker"):
        sandbox.execution_policy({"env": {"reviewer": ["MODEL_ACCESS"]}})


def test_command_builder_refuses_legacy_policy_before_launch():
    policy = {"tools": {}, "sockets": {}, "env": {}, "isolation": "none"}
    with pytest.raises(ValueError, match="Bubblewrap"):
        sandbox.command_line(["true"], Path("/fixture"), "reviewer", policy)


def test_check_has_a_readonly_tree_and_no_broker(tmp_path):
    policy = sandbox.execution_policy({})
    command, env = sandbox.command_line(["true"], tmp_path, "check", policy)
    mount = command.index(str(tmp_path))
    assert command[mount - 1:mount + 2] == ["--ro-bind", str(tmp_path), "/workspace"]
    assert command[command.index("--chdir") + 1] == "/workspace"
    assert "--unshare-all" in command and "--new-session" in command
    assert "AGENT_MODEL_SOCKET" not in env and "AGENT_EVIDENCE_SOCKET" not in env
