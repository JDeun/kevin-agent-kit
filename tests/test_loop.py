import json
import subprocess
import sys

from core.loop.__main__ import demo
from core.loop.runtime import MemoryGate
from core.loop.review import findings, lenses

import pytest


def test_demo_repairs_and_merges(tmp_path):
    report = demo(tmp_path)
    assert report["counts"] == {"merged": 1, "superseded": 1}
    assert report["result"] == "correct"
    assert report["reviewed"] != report["initial"]


def test_lenses_and_malformed_response():
    assert lenses(["store/migrations/1.sql", "auth/session.py"],
                  {"db": ["**/*.sql"], "security": ["auth/**"]}) == (
                      "general", "db", "security")
    with pytest.raises(ValueError):
        findings('{"findings":[{"risk":"unrecognized","detail":"x"}]}')
    with pytest.raises(ValueError):
        findings('{"findings":[],"count":1}')


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


def wait_for(engine, predicate):
    import time

    end = time.monotonic() + 8
    while time.monotonic() < end:
        engine.tick()
        status = engine.store.status()
        if predicate(status):
            return status
        time.sleep(0.02)
    pytest.fail(f"coordinator did not reach expected phase: {status}")


@pytest.fixture
def queue(tmp_path):
    from core.loop.config import config
    from core.loop.git import Git

    repo = tmp_path / "repository"
    repo.mkdir()
    state = tmp_path / "state"
    git = Git(repo, state)
    git.run("init", "--quiet", "--initial-branch=main")
    git.run("config", "user.name", "Fixture")
    git.run("config", "user.email", "")
    git.run("config", "commit.gpgsign", "false")
    git.run("commit", "--quiet", "--allow-empty", "-m", "base")
    git.run("switch", "-c", "feature")
    git.run("commit", "--quiet", "--allow-empty", "-m", "feature")
    git.run("switch", "--detach", "main")
    cfg = config()
    cfg.update(repo=repo, state=state, poll_seconds=0.05, timeout_seconds=5,
               max_rounds=1, retry_initial_seconds=0.05)
    return git, cfg


def python_command(code):
    return [sys.executable, "-c", code]


def test_accept_record_only_after_protected_findings_close(queue):
    from core.loop.engine import Engine
    from core.loop.store import Store

    git, cfg = queue
    script = """
import json
import pathlib
import sys
repaired = pathlib.Path("repair").exists()
risk = "other" if repaired else "security"
if repaired:
    assert "Repair this." in sys.stdin.read()
print(json.dumps({"findings":[{"risk": risk, "detail": "Repair this."}]}))
"""
    cfg["reviewer"] = python_command(script)
    script = """
import pathlib
import subprocess
pathlib.Path("repair").touch()
subprocess.run(["git", "add", "repair"], check=True)
subprocess.run(["git", "commit", "-qm", "repair"], check=True)
"""
    cfg["worker"] = python_command(script)
    starting_base = git.head("main")
    starting_head = git.head("feature")
    with Store(cfg["state"]).connect() as store, Engine(cfg, store) as engine:
        store.enqueue("feature", starting_head)
        status = wait_for(engine, lambda x: x["counts"].get("waiting"))
        assert status["counts"] == {"superseded": 1, "waiting": 1}
        record, = status["accept_records"]
        assert record["rounds"] == 2
        assert record["sha"] == git.head("feature") != starting_head
        assert record["findings"][0]["risk"] == "other"
        store.decide(record["id"], "defer", "Release owner")
        assert store.status()["counts"]["deferred"] == 1
        assert store.status()["accept_records"][0]["source"] == "Release owner"
        assert git.head("main") == starting_base


@pytest.mark.parametrize("moving_branch", ["main", "feature"])
def test_train_checks_fresh_exact_candidate(queue, tmp_path, moving_branch):
    from core.loop.engine import Engine
    from core.loop.store import Store

    git, cfg = queue
    marker = tmp_path / "attempts"
    cfg["reviewer"] = python_command('print(\'{"findings": []}\')')
    cfg["verify"] = python_command(f"""
import os
from pathlib import Path
import subprocess
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
candidate = os.environ["AGENT_CANDIDATE"]
assert git("rev-parse", "HEAD") == candidate
marker = Path({str(marker)!r})
if not marker.exists():
    ref = {("refs/heads/" + moving_branch)!r}
    parent = git("rev-parse", ref)
    updated = git("commit-tree", parent + "^{{tree}}", "-p", parent, "-m", "advance")
    git("update-ref", ref, updated, parent)
with marker.open("a") as stream:
    print(candidate, file=stream)
""")
    initial = git.head("feature")
    with Store(cfg["state"]).connect() as store, Engine(cfg, store) as engine:
        store.enqueue("feature", initial)
        status = wait_for(engine, lambda x: x["counts"].get("merged"))
    discarded, integrated = marker.read_text().splitlines()
    assert discarded != integrated == git.head("main")
    if moving_branch == "feature":
        assert status["counts"] == {"merged": 1, "stale": 1}
        assert next(job for job in status["jobs"] if job["phase"] == "merged")["sha"] != initial
    else:
        assert status["counts"] == {"merged": 1}


def test_capacity_retry_uses_same_job(queue, tmp_path):
    from core.loop.engine import Engine
    from core.loop.store import Store

    _, cfg = queue
    marker = tmp_path / "reviewed"
    cfg["reviewer"] = python_command(f"""
from pathlib import Path
import sys
marker = Path({str(marker)!r})
if not marker.exists():
    marker.touch()
    print("rate limit", file=sys.stderr)
    raise SystemExit(75)
print('{{"findings":[]}}')
""")
    cfg["verify"] = python_command("pass")
    with Store(cfg["state"]).connect() as store, Engine(cfg, store) as engine:
        sha = engine.git.head("feature")
        store.enqueue("feature", sha)
        store.enqueue("feature", sha)
        status = wait_for(engine, lambda x: any(job["attempts"] for job in x["jobs"]))
        assert status["jobs"][0]["attempts"] == 1
        assert wait_for(engine, lambda x: x["counts"].get("merged"))["counts"] == {"merged": 1}


def test_detached_daemon_starts_and_stops(tmp_path):
    import tempfile
    import time
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="kit-daemon-") as directory:
        path = Path(directory)
        config_path = path / "config.toml"
        config_path.write_text('[loop]\nstate = "state"\npoll_seconds = 1\n')
        command = [sys.executable, "-m", "core.loop", "--config", str(config_path)]
        subprocess.run([*command, "start"], check=True, timeout=8)
        try:
            report = json.loads(subprocess.check_output([*command, "status"], timeout=5))
            assert report["running"] is True
        finally:
            subprocess.run([*command, "stop"], check=True, timeout=5)
        for _ in range(50):
            report = json.loads(subprocess.check_output([*command, "status"], timeout=5))
            if not report["running"]:
                break
            time.sleep(0.05)
        assert report["running"] is False


def test_reviewer_cannot_leave_checkout_at_another_commit(queue):
    from core.loop.engine import Engine
    from core.loop.store import Store

    git, cfg = queue
    cfg["reviewer"] = python_command("""
import subprocess
subprocess.run(["git", "switch", "--detach", "main"], check=True)
print('{"findings":[]}')
""")
    base = git.head("main")
    cfg["verify"] = python_command("pass")
    with Store(cfg["state"]).connect() as store, Engine(cfg, store) as engine:
        store.enqueue("feature", git.head("feature"))
        status = wait_for(engine, lambda x: x["counts"].get("blocked") or x["counts"].get("merged"))
    assert status["counts"] == {"blocked": 1}
    assert "reviewer moved" in status["jobs"][0]["detail"]
    assert git.head("main") == base


def test_symbolic_target_does_not_move_another_branch(queue):
    from core.loop.engine import Engine
    from core.loop.store import Store

    git, cfg = queue
    cfg["target"] = "alias"
    cfg["reviewer"] = python_command('print(\'{"findings": []}\')')
    cfg["verify"] = python_command("pass")
    git.run("symbolic-ref", "refs/heads/alias", "refs/heads/main")
    git.run("switch", "main")
    before = git.head("main")
    with Store(cfg["state"]).connect() as store, Engine(cfg, store) as engine:
        store.enqueue("feature", git.head("feature"))
        status = wait_for(engine, lambda x: x["counts"].get("blocked") or x["counts"].get("merged"))
    assert status["counts"] == {"blocked": 1}
    assert "direct Git ref" in status["jobs"][0]["detail"]
    assert git.head("main") == before


def test_command_cleans_up_only_its_process_group(tmp_path):
    from threading import Event

    from core.loop.runtime import run

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
    outsider = subprocess.Popen(python_command("import time; time.sleep(30)"), start_new_session=True)
    try:
        assert run(python_command(parent), tmp_path, Event(), 5) == ""
        assert (tmp_path / "stopped").exists()
        assert outsider.poll() is None
    finally:
        outsider.terminate()
        outsider.wait(timeout=5)
