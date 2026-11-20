import json
import os
from pathlib import Path
from threading import Event
import subprocess

import pytest


def test_global_rules_rejects_symlinked_client_directory(tmp_path, monkeypatch):
    from core.loop.setup import global_rules

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "CLAUDE.md"
    target.write_bytes(b"personal rules")
    (tmp_path / ".claude").symlink_to(outside, target_is_directory=True)
    for dry_run in (True, False):
        with pytest.raises(ValueError, match="directory"):
            global_rules(dry_run=dry_run)
    assert target.read_bytes() == b"personal rules"
    assert not (tmp_path / ".codex").exists()


def test_global_rules_embed_an_immutable_copy(tmp_path, monkeypatch):
    from core.loop import setup

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "source\nwith [brackets]"
    rules = source / "rules"
    rules.mkdir(parents=True)
    text = b"# Fixture rule\n\nUse the approved text.\n"
    rule = rules / "fixture.md"
    rule.write_bytes(text)
    monkeypatch.setattr(setup, "ROOT", source)
    setup.global_rules()
    rule.write_bytes(b"# Edited rule\n")
    installed = (tmp_path / ".claude/CLAUDE.md").read_bytes()
    assert text in installed
    assert b"sha256" in installed and b"0.1.0" in installed
    assert str(source).encode() not in installed


def test_global_rules_preserve_file_metadata(tmp_path, monkeypatch):
    from core.loop.setup import global_rules

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / ".claude/CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(b"personal rules")
    target.chmod(0o640)
    os.setxattr(target, "user.fixture", b"untouched")
    original = target.stat()
    for uninstall in (False, True):
        global_rules(uninstall=uninstall)
        updated = target.stat()
        assert (updated.st_uid, updated.st_gid, updated.st_mode) == (
            original.st_uid, original.st_gid, original.st_mode,
        )
        assert os.getxattr(target, "user.fixture") == b"untouched"
    assert target.read_bytes() == b"personal rules"


def test_global_rules_stop_when_pinned_parent_moves(tmp_path, monkeypatch):
    from core.loop import setup

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    client = tmp_path / ".claude"
    client.mkdir()
    (client / "CLAUDE.md").write_bytes(b"original")
    write = setup.write_atomic

    def move_parent(parent, name, content, **kwargs):
        if kwargs.get("location") == client:
            client.rename(tmp_path / "moved")
            client.mkdir()
            (client / name).write_bytes(b"replacement")
        return write(parent, name, content, **kwargs)

    monkeypatch.setattr(setup, "write_atomic", move_parent)
    with pytest.raises(ValueError, match="directory changed"):
        setup.global_rules()
    assert (client / "CLAUDE.md").read_bytes() == b"replacement"
    assert (tmp_path / "moved/CLAUDE.md").read_bytes() == b"original"


def test_existing_target_saved_during_publication_is_preserved(tmp_path, monkeypatch):
    from core.loop import setup

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / ".claude/CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(b"old")
    exchange = setup.exchange_files
    edited = False

    def save_before_exchange(source, from_name, destination, to_name):
        nonlocal edited
        if not edited:
            edited = True
            pending = target.with_suffix(".pending")
            pending.write_bytes(b"new human edit")
            pending.replace(target)
        return exchange(source, from_name, destination, to_name)

    monkeypatch.setattr(setup, "exchange_files", save_before_exchange)
    with pytest.raises(ValueError, match="changed during setup"):
        setup.global_rules()
    assert target.read_bytes() == b"new human edit"


def test_only_requested_tools_are_passed_to_bubblewrap(tmp_path, monkeypatch):
    from core.loop import sandbox

    root = tmp_path / "tools"
    root.mkdir()
    runner = root / "runner"
    secret = root / "role-only"
    runner.write_text("run fixture")
    secret.write_text("private fixture")
    policy = sandbox.execution_policy({"tools": {"runner": str(runner), "secret": str(secret)}})
    scratch = tmp_path / "shared"
    scratch.mkdir(mode=0o777)
    monkeypatch.setenv("TMPDIR", str(scratch))

    def inspect(argv, *args, **kwargs):
        assert "/tools/runner" in argv
        assert "/tools/secret" not in argv
        src = Path(argv[argv.index("/tools/runner") - 1])
        assert src != runner and src.read_bytes() == runner.read_bytes()
        assert not src.is_relative_to(scratch)
        return ""

    monkeypatch.setattr(sandbox, "run", inspect)
    sandbox.execute(["/tools/runner"], None, Event(), 1, role="verify", policy=policy)
    runner.unlink()
    runner.write_text("replacement")
    with pytest.raises(ValueError, match="changed"):
        sandbox.execute(["/tools/runner"], None, Event(), 1, role="verify", policy=policy)
    root.chmod(0o777)
    with pytest.raises(ValueError, match="directory"):
        sandbox.execution_policy({"tools": {"runner": str(runner)}})


def test_receipt_signature_binds_source_and_exact_request(tmp_path):
    import base64
    from core.loop.evidence import canonical, public_key, signed_receipt

    secret, public = tmp_path / "signer.pem", tmp_path / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(secret)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(secret), "-pubout", "-out", str(public)], check=True)
    key = public_key(public)
    request = {"candidate": "commit", "invocation": "unique", "check": "unit"}
    payload = {"request": request, "execution_id": "run-1", "status": "passed"}
    message = tmp_path / "message"
    message.write_bytes(canonical(payload))
    signature = subprocess.check_output([
        "openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(secret), "-in", str(message),
    ])
    envelope = {"payload": payload, "signature": base64.b64encode(signature).decode()}
    assert signed_receipt(json.dumps(envelope), key) == payload
    envelope["payload"]["request"] = {**request, "candidate": "different"}
    with pytest.raises(ValueError, match="signature"):
        signed_receipt(json.dumps(envelope), key)
    with pytest.raises(ValueError, match="signed evidence"):
        signed_receipt(json.dumps(payload), key)


def test_legacy_profile_still_resolves_daemon_stop_target(tmp_path, monkeypatch):
    from core.loop.config import config
    from core.loop.git import Git
    from core.loop.setup import repository_profile

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git = Git(repo)
    git.run("init", "--initial-branch=main")
    git.run("commit", "--allow-empty", "-m", "base")
    profile = repository_profile(repo)
    expected = config(profile)["state"]
    with profile.open("a", encoding="utf-8") as stream:
        stream.write('\n[loop.attestors]\nunit = ["/tools/legacy"]\n')
    assert config(profile, control_only=True) == {"state": expected}
    with pytest.raises(ValueError, match="replace legacy attestors"):
        config(profile)
