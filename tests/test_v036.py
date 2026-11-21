"""Regression tests for the 0.3.5 release-check scenarios: inheritance as a live dependency."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from core.loop.engine import Engine, check_candidate
from core.loop.runtime import _SUPERVISOR
from tests.test_loop import clean_run, queue  # noqa: F401
from tests.test_release_fixes import settle
from tests.test_v034 import FINDING, no_rerun
from tests.test_v035 import store_of


def successor_branch(git, name="successor"):
    git.run("switch", "-c", name, "feature")
    git.run("commit", "--quiet", "--allow-empty", "-m", name)
    sha = git.head(name)
    git.run("switch", "main")
    return sha


def record_parent_finding(cfg, job_id):
    """A parent reviewer records a protected finding, as a late review of that job would."""
    with store_of(cfg) as store:
        store.open_attempt(job_id, {"run": "late", "candidate": "c", "lens": "security",
                                    "invocation": "late-invocation"}, 1)
        store.observe("late-invocation", "", [FINDING])
        store.settle("late-invocation", "complete", "digest")


def candidate_pair(cfg, git, store):
    """A parent settled as a candidate and a child on another branch that inherits from it."""
    sha = successor_branch(git)
    assert settle(cfg, store, clean_run, store.enqueue("feature", git.head("feature"))["id"])["phase"] == "candidate"
    child = store.enqueue("successor", sha, inherits=1)
    assert settle(cfg, store, clean_run, child["id"])["phase"] == "candidate"
    return child


def test_child_waits_while_its_parent_is_still_in_review(queue):  # noqa: F811
    git, cfg = queue
    sha = successor_branch(git)
    with store_of(cfg) as store:
        parent = store.enqueue("feature", git.head("feature"))
        child = store.enqueue("successor", sha, inherits=parent["id"])
        assert [job["id"] for job in store.ready()] == [parent["id"]]
        store.update(parent, "candidate")
        assert [job["id"] for job in store.ready()] == [child["id"]]
        store.update(parent, "interrupted")
        assert store.ready() == []


def test_child_waits_for_a_grandparent_in_review(queue):  # noqa: F811
    git, cfg = queue
    middle = successor_branch(git, "middle")
    last = successor_branch(git, "last")
    with store_of(cfg) as store:
        root = store.enqueue("feature", git.head("feature"))
        mid = store.enqueue("middle", middle, inherits=root["id"])
        end = store.enqueue("last", last, inherits=mid["id"])
        store.update(mid, "candidate")
        assert [job["id"] for job in store.ready()] == [root["id"]]
        store.update(root, "candidate")
        assert {job["id"] for job in store.ready()} == {end["id"]}


def test_parent_finding_recorded_during_the_child_review_sends_it_back(queue):  # noqa: F811
    git, cfg = queue
    sha = successor_branch(git)
    with store_of(cfg) as store:
        assert settle(cfg, store, clean_run, store.enqueue("feature", git.head("feature"))["id"])["phase"] == "candidate"
        child = store.enqueue("successor", sha, inherits=1)

    def runner(argv, cwd, cancel, timeout, *, role, **kwargs):
        if role == "reviewer" and not runner.done:
            runner.done = True
            record_parent_finding(cfg, 1)
        return clean_run(argv, cwd, cancel, timeout, role=role, **kwargs)
    runner.done = False
    with store_of(cfg) as store, Engine(cfg, store, runner=runner, authenticate=lambda raw, _: json.loads(raw)) as engine:
        end = time.monotonic() + 8
        while time.monotonic() < end:
            engine.tick()
            job = {row["id"]: row for row in store.status()["jobs"]}[child["id"]]
            if job["phase"] != "review" or "lineage" in job["detail"]:
                break
            time.sleep(0.02)
        # The finding the child never saw cannot leave the child as a candidate.
        assert job["phase"] == "review" and "lineage" in job["detail"]
        assert store.status()["counts"].get("candidate") == 1


def test_export_check_reads_the_parent_journal_every_time(queue):  # noqa: F811
    git, cfg = queue
    with store_of(cfg) as store:
        child = candidate_pair(cfg, git, store)
        evidence = store.candidate(child["id"])
        check_candidate(cfg, evidence, store)
        record_parent_finding(cfg, 1)
        with pytest.raises(ValueError, match="lineage"):
            check_candidate(cfg, evidence, store)


def test_export_check_refuses_a_candidate_whose_parent_is_back_in_review(queue):  # noqa: F811
    git, cfg = queue
    with store_of(cfg) as store:
        child = candidate_pair(cfg, git, store)
        evidence = store.candidate(child["id"])
        store.update(store.status()["jobs"][0], "review")
        with pytest.raises(ValueError, match="still in review"):
            check_candidate(cfg, evidence, store)


# Forced stop of the coordinator ----------------------------------------------------------------
@pytest.mark.parametrize("cut, expected", [("}", "protected"), ("detail", "unread-")])
def test_restart_rescans_output_a_killed_coordinator_left_open(queue, cut, expected):  # noqa: F811
    git, cfg = queue
    sha = git.head("feature")
    with store_of(cfg) as store:
        job = store.enqueue("feature", sha)
        # What a SIGKILL leaves: an open attempt holding half a finding and nothing journaled.
        store.open_attempt(job["id"], {"run": "r", "candidate": "c", "lens": "security",
                                       "invocation": "killed"}, 1)
        text = json.dumps({"findings": [FINDING]})
        end = text.index(cut) + (len(cut) if cut == "detail" else 0) + (8 if cut == "detail" else 0)
        store.observe("killed", text[:end], [])
        assert store.protected(job["id"]) == []
    with store_of(cfg) as store, Engine(cfg, store, runner=no_rerun):
        assert store.status()["counts"] == {"interrupted": 1}
        [recovered] = store.protected(1)
        assert recovered["id"].startswith(expected)
        store.resume(1)
        # Resuming does not drop it: a clean review without a resolution is refused.
        assert settle(cfg, store, clean_run, 1)["phase"] == "blocked"


def test_supervisor_does_not_outlive_a_killed_parent(tmp_path):
    marker = tmp_path / "supervisor.pid"
    launcher = (
        "import os,subprocess,sys,time\n"
        "r,w=os.pipe()\n"
        f"p=subprocess.Popen([sys.executable,'-I','-c',{_SUPERVISOR!r},str(w),str(os.getpid()),'sleep','30'],"
        "start_new_session=True,pass_fds=(w,))\n"
        f"open({str(marker)!r},'w').write(str(p.pid))\n"
        "os.write(1,b'x')\n"
        "time.sleep(60)\n")
    parent = subprocess.Popen([sys.executable, "-c", launcher], stdout=subprocess.PIPE)
    assert parent.stdout.read(1) == b"x"
    pid = int(marker.read_text())
    os.kill(parent.pid, signal.SIGKILL)
    parent.wait()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]
        except (ProcessLookupError, FileNotFoundError):
            break
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        os.killpg(pid, signal.SIGKILL)
        pytest.fail("supervisor outlived its parent")
