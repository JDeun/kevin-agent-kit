import os
from pathlib import Path
import sqlite3
from threading import Event, Thread
import time

import pytest

from core.loop import config as loop_config
from core.loop import install as kit
from core.loop import sandbox
from core.loop.engine import Engine
from core.loop.review import prompt
from core.loop.runtime import Cancelled, run
from core.loop.store import Store
from tests.test_loop import queue, review_response  # noqa: F401


def install(tmp_path, monkeypatch):
    """Point the loop at a separate kit tree with two rule documents."""
    root = tmp_path / "kit"
    rules = root / "rules"
    rules.mkdir(parents=True)
    (rules / "one.md").write_text("first rule\n")
    (rules / "two.md").write_text("second rule\n")
    real = kit.snapshot()
    monkeypatch.setattr(kit, "snapshot", lambda: type(real)(
        root, real.packages, {**{name: content for name, content in real.files.items()
                                 if not name.startswith("rules/")}, **kit.take(root, ()).files}))
    return rules


@pytest.mark.parametrize("unsafe", ["file-link", "file-mode", "directory-mode", "hard-link", "fifo",
                                    "nested-directory-mode", "directory-link"])
def test_installed_rules_share_the_module_trust_check(tmp_path, monkeypatch, unsafe):
    rules = install(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("external text\n")
    if unsafe == "file-link":
        (rules / "link.md").symlink_to(outside)
    elif unsafe == "file-mode":
        (rules / "one.md").chmod(0o664)
    elif unsafe == "directory-mode":
        rules.chmod(0o775)
    elif unsafe == "hard-link":
        os.link(outside, rules / "alias.md")
    elif unsafe == "fifo":
        os.mkfifo(rules / "pipe.md")
    elif unsafe == "nested-directory-mode":
        (rules / "nested").mkdir(mode=0o777)
        (rules / "nested").chmod(0o777)
    else:
        moved = rules.with_name("elsewhere")
        rules.rename(moved)
        rules.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="untrusted kit file"):
        prompt({"lens": "general", "kind": "review"}, [])
    with pytest.raises(ValueError, match="untrusted kit file"):
        kit.rules()


def test_policy_prompt_and_mount_use_one_validated_snapshot(tmp_path, monkeypatch):
    rules = install(tmp_path, monkeypatch)
    root = tmp_path / "kit"
    taken = kit.snapshot()
    monkeypatch.setattr(kit, "snapshot", lambda: taken)
    snapshot = kit.rules()
    original = kit.take(root, ())
    # Editing the installed file after the snapshot changes neither the prompt nor the mount.
    (rules / "one.md").write_text("tampered rule\n")
    request = {"lens": "general", "kind": "review"}
    assert "first rule" in prompt(request, []) and "tampered" not in prompt(request, [])
    assert kit.take(root, ()).digest != original.digest
    # The same bytes under another file name are a different snapshot, hence a different policy.
    (rules / "one.md").write_text("first rule\n")
    assert kit.take(root, ()).digest == original.digest
    (rules / "one.md").rename(rules / "one-renamed.md")
    renamed = kit.take(root, ())
    assert sorted(renamed.files.values()) == sorted(original.files.values())
    execution = {"tools": {}, "sockets": {}}
    assert (loop_config.policy_id(renamed.digest, b"raw", execution, ())
            != loop_config.policy_id(original.digest, b"raw", execution, ()))

    seen = {}

    def fake_run(cmd, cwd, cancel, timeout, **kwargs):
        source = Path(cmd[cmd.index("/rules") - 1])
        seen.update({path.name: path.read_bytes() for path in source.iterdir()})
        return ""

    monkeypatch.setattr(sandbox, "run", fake_run)
    policy = {**sandbox.execution_policy({}), "rules": snapshot}
    sandbox.execute(["/usr/bin/true"], None, Event(), 5, role="verify", policy=policy)
    assert seen == snapshot == {"one.md": b"first rule\n", "two.md": b"second rule\n"}


def test_coordinator_config_carries_the_snapshot(queue):  # noqa: F811
    _, cfg = queue
    assert cfg["execution"]["rules"]
    assert cfg["rules_text"] == loop_config.rules_text(cfg["execution"]["rules"])


def test_stop_keeps_stdout_of_a_reviewer(tmp_path):
    cancel = Event()
    script = ("import sys,time\nsys.stdout.write('{\"findings\":[{\"id\":\"p\"}]}')\n"
              "sys.stdout.flush()\ntime.sleep(30)\n")
    Thread(target=lambda: (time.sleep(1), cancel.set())).start()
    with pytest.raises(Cancelled) as caught:
        run(["python3", "-I", "-c", script], tmp_path, cancel, 30, capture_failed_output=True)
    assert caught.value.output == '{"findings":[{"id":"p"}]}'
    assert caught.value.output_truncated is False


FINDING = {"id": "protected", "risk": "security", "detail": "Review evidence."}


def stopping_reviewer(output, *, truncated=False):
    def runner(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
        assert role == "reviewer"
        runner.started.set()
        assert cancel.wait(5)
        stop = Cancelled()
        stop.output = output(stdin)
        stop.output_truncated = truncated
        raise stop
    runner.started = Event()
    return runner


def start_and_stop(cfg, git, runner):
    sha = git.head("feature")
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(
        cfg, store, runner=runner,
    ) as engine:
        store.enqueue("feature", sha)
        engine.tick()
        assert runner.started.wait(5)


def no_rerun(*args, **kwargs):
    raise AssertionError("an interrupted job must not rerun automatically")


def test_stop_records_a_protected_finding_printed_before_the_stop(queue):  # noqa: F811
    git, cfg = queue
    start_and_stop(cfg, git, stopping_reviewer(lambda stdin: review_response(stdin, [FINDING])))
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
        assert [item["id"] for item in store.protected(1)] == ["protected"]
        assert store.status()["counts"] == {"review": 1}


def test_stop_with_uncertain_output_keeps_the_pending_row(queue):  # noqa: F811
    git, cfg = queue
    start_and_stop(cfg, git, stopping_reviewer(lambda stdin: "", truncated=True))
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(
        cfg, store, runner=no_rerun,
    ) as engine:
        assert store.status()["counts"] == {"interrupted": 1}
        engine.tick()
        assert store.status()["counts"] == {"interrupted": 1}


def test_stop_with_failed_recording_keeps_the_pending_row(queue, monkeypatch):  # noqa: F811
    git, cfg = queue

    def broken(self, *args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "settle", broken)
    start_and_stop(cfg, git, stopping_reviewer(lambda stdin: review_response(stdin, [FINDING])))
    monkeypatch.undo()
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store, Engine(
        cfg, store, runner=no_rerun,
    ):
        assert store.status()["counts"] == {"interrupted": 1}


def test_clean_stop_before_any_output_still_lets_the_job_resume(queue):  # noqa: F811
    git, cfg = queue
    start_and_stop(cfg, git, stopping_reviewer(lambda stdin: ""))
    with Store(cfg["state"], cfg["repo_id"], cfg["target"]).connect() as store:
        assert store.status()["counts"] == {"review": 1}
        assert store.protected(1) == []
