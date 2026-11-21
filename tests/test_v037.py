"""Regression tests for 0.3.7: a finding is a journal item, not a reviewer-chosen name."""

import json

import pytest

from core.loop.engine import check_candidate
from tests.test_loop import clean_run, queue, review_response  # noqa: F401
from tests.test_release_fixes import settle
from tests.test_v034 import FINDING
from tests.test_v035 import store_of
from tests.test_v036 import successor_branch


def record(cfg, job_id, finding, invocation, lens="security"):
    """Journal a protected finding on a job, as a review of that job would."""
    with store_of(cfg) as store:
        store.open_attempt(job_id, {"run": invocation, "candidate": "c", "lens": lens,
                                    "invocation": invocation}, 1)
        store.observe(invocation, "", [finding])
        store.settle(invocation, "complete", "digest")


def resolving(argv, cwd, cancel, timeout, *, role, stdin, **kwargs):
    if role == "reviewer":
        seen = json.loads(stdin.splitlines()[-1])["prior_protected"]
        return review_response(stdin, resolved={item["id"]: "repaired" for item in seen})
    if role == "review-observer":
        payload = json.loads(clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs))
        payload["resolved"] = json.loads(stdin)["review"]["resolved"]
        return json.dumps(payload)
    return clean_run(argv, cwd, cancel, timeout, role=role, stdin=stdin, **kwargs)


def reviewed_child(cfg, git, store):
    """A parent holding one finding and a child that reviewed (resolved) exactly that finding."""
    sha = successor_branch(git)
    parent = store.enqueue("feature", git.head("feature"))
    store.update(parent, "candidate")
    record(cfg, parent["id"], FINDING, "first")
    child = store.enqueue("successor", sha, inherits=parent["id"])
    outcome = settle(cfg, store, resolving, child["id"])
    assert outcome["phase"] == "candidate", outcome["detail"]
    return parent, child


def test_parent_recording_the_same_id_for_a_different_finding_is_not_reviewed(queue):  # noqa: F811
    git, cfg = queue
    with store_of(cfg) as store:
        parent, child = reviewed_child(cfg, git, store)
        evidence = store.candidate(child["id"])
        check_candidate(cfg, evidence, store)
        record(cfg, parent["id"], {**FINDING, "detail": "A different problem under the same ID."}, "second")
        with pytest.raises(ValueError, match="lineage"):
            check_candidate(cfg, evidence, store)


def test_the_same_finding_journaled_again_is_not_new(queue):  # noqa: F811
    git, cfg = queue
    with store_of(cfg) as store:
        parent, child = reviewed_child(cfg, git, store)
        evidence = store.candidate(child["id"])
        record(cfg, parent["id"], FINDING, "again")
        check_candidate(cfg, evidence, store)


def test_inherited_finding_whose_id_collides_with_a_child_finding_still_needs_resolution(queue):  # noqa: F811
    git, cfg = queue
    sha = successor_branch(git)
    with store_of(cfg) as store:
        parent = store.enqueue("feature", git.head("feature"))
        store.update(parent, "candidate")
        record(cfg, parent["id"], FINDING, "parent-finding")
        child = store.enqueue("successor", sha, inherits=parent["id"])
        record(cfg, child["id"], {**FINDING, "detail": "The child's own, unrelated finding."}, "child-finding")
        inherited = store.inherited_protected(store.status()["jobs"][1])
        assert len(inherited) == 2 and len({(item["lens"], item["id"]) for item in inherited}) == 2
        assert sorted(item["detail"] for item in inherited) == [
            "Review evidence.", "The child's own, unrelated finding."]
        # A review that resolves nothing leaves both findings unresolved.
        assert settle(cfg, store, clean_run, child["id"])["phase"] == "blocked"
        store.retry(child["id"])
        assert settle(cfg, store, resolving, child["id"])["phase"] == "candidate"


def test_export_waits_for_an_open_review_on_the_parent_branch(queue):  # noqa: F811
    git, cfg = queue
    sha = successor_branch(git)
    with store_of(cfg) as store:
        parent = store.enqueue("feature", git.head("feature"))
        store.update(parent, "candidate")
        child = store.enqueue("successor", sha, inherits=parent["id"])
        assert settle(cfg, store, clean_run, child["id"])["phase"] == "candidate"
        evidence = store.candidate(child["id"])
        check_candidate(cfg, evidence, store, export=True)
        # Another job on the parent's branch starts a review; the parent job itself stays settled.
        git.run("switch", "feature")
        git.run("commit", "--quiet", "--allow-empty", "-m", "more")
        git.run("switch", "main")
        store.enqueue("feature", git.head("feature"))
        with pytest.raises(ValueError, match="still in review"):
            check_candidate(cfg, evidence, store, export=True)
        check_candidate(cfg, evidence, store)
