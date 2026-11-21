"""Regression tests for 0.3.8: the finding list and the reviewed-lineage record share one snapshot."""

import json
import sqlite3
from contextlib import closing

from core.loop.store import Store, content_hash
from tests.test_loop import queue  # noqa: F401
from tests.test_v034 import FINDING
from tests.test_v035 import store_of
from tests.test_v036 import successor_branch
from tests.test_v037 import record

OTHER = {**FINDING, "id": "sec-2", "detail": "A second, different problem."}


def peer_records(cfg, job_id, invocation):
    """A peer worker journals a finding through its own connection; a blocked write is fine."""
    try:
        with closing(sqlite3.connect(cfg["state"] / "queue.sqlite3", timeout=0.05)) as db, db:
            db.execute("INSERT INTO review_attempts (invocation,job_id,run,candidate,lens,round_number,opened_at) "
                       "VALUES (?,?,?,?,?,?,?)", (invocation, job_id, "r", "c", "security", 1, "t"))
            db.execute("INSERT INTO finding_journal (invocation,finding_id,body,recorded_at) VALUES (?,?,?,?)",
                       (invocation, OTHER["id"], json.dumps(OTHER), "t"))
    except sqlite3.OperationalError:
        pass


def test_lineage_reviewed_holds_only_what_the_reviewer_was_given(queue, monkeypatch):  # noqa: F811
    git, cfg = queue
    sha = successor_branch(git)
    with store_of(cfg) as store:
        parent = store.enqueue("feature", git.head("feature"))
        store.update(parent, "candidate")
        record(cfg, parent["id"], FINDING, "first")
        child = store.enqueue("successor", sha, inherits=parent["id"])
        real = Store._protected_rows
        fired = []

        def interleaved(self, job_id):
            yield from real(self, job_id)
            # The parent's journal has just been read in full; a peer records another finding now.
            if job_id == parent["id"] and not fired:
                fired.append(job_id)
                peer_records(cfg, parent["id"], "intruder")

        monkeypatch.setattr(Store, "_protected_rows", interleaved)
        prior, lineage = store.inherited_lineage(child)
        given = {(item["lens"], content_hash(item)) for item in prior}
        recorded = {(lens, digest) for _, lens, digest, _ in lineage}
        assert recorded <= given, "a finding the reviewer never saw is recorded as reviewed"
        assert given == recorded


def test_snapshot_joins_an_open_transaction(queue):  # noqa: F811
    _, cfg = queue
    with store_of(cfg) as store:
        with store.snapshot():
            assert store.db.in_transaction
            with store.snapshot():
                pass
            assert store.db.in_transaction
        assert not store.db.in_transaction
