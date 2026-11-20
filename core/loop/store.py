from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
import stat
import time

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, state, identity, target):
        path = state / "queue.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("queue database must be private and user-owned")
        finally:
            os.close(fd)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self.initialize(identity, target)
        except BaseException:
            self.db.close()
            raise

    def initialize(self, identity, target):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" in names and "project" not in names:
                raise ValueError("legacy queue requires a new state directory")
            self.db.execute("CREATE TABLE IF NOT EXISTS project (id TEXT PRIMARY KEY, target TEXT NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO project VALUES (?,?)", (identity, target))
            if [tuple(row) for row in self.db.execute("SELECT * FROM project")] != [(identity, target)]:
                raise ValueError("queue repository or target does not match the profile")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, branch TEXT NOT NULL, sha TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'review', attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
                    UNIQUE(branch, sha)
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs,
                    phase TEXT NOT NULL, body TEXT NOT NULL
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS protected_observations (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs,
                    run TEXT NOT NULL, candidate TEXT NOT NULL, round_number INTEGER NOT NULL,
                    lens TEXT NOT NULL, invocation TEXT NOT NULL, findings TEXT NOT NULL
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS pending_reviews (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs,
                    run TEXT NOT NULL, candidate TEXT NOT NULL, lens TEXT NOT NULL, invocation TEXT NOT NULL
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS accept_records (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs,
                    recorded_at TEXT NOT NULL, branch TEXT NOT NULL, sha TEXT NOT NULL,
                    rounds INTEGER NOT NULL, findings TEXT NOT NULL,
                    disposition TEXT, source TEXT, decided_at TEXT
                )
            """)

    @contextmanager
    def connect(self):
        try:
            yield self
        finally:
            self.db.close()

    def enqueue(self, branch, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("expected a full lowercase commit SHA")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(branch,sha) VALUES (?,?)", (branch, sha))

    def ready(self):
        return self.db.execute(
            "SELECT * FROM jobs WHERE phase='review' AND next_at <= ? ORDER BY id", (time.time(),),
        ).fetchall()

    def update(self, job, phase, *, detail="", attempts=0, next_at=0, evidence=None):
        with self.db:
            self.db.execute("UPDATE jobs SET phase=?,detail=?,attempts=?,next_at=? WHERE id=?",
                            (phase, detail, attempts, next_at, job["id"]))
            if evidence is not None:
                self.db.execute("INSERT INTO evidence (job_id,phase,body) VALUES (?,?,?)",
                                (job["id"], phase, json.dumps(evidence)))
            if phase == "waiting":
                self.db.execute(
                    "INSERT INTO accept_records (job_id,recorded_at,branch,sha,rounds,findings) VALUES (?,?,?,?,?,?)",
                    (job["id"], now(), job["branch"], job["sha"], evidence["rounds"], json.dumps(evidence["findings"])),
                )
            self.db.execute("DELETE FROM pending_reviews WHERE job_id=?", (job["id"],))

    def recover_pending_reviews(self):
        # A previous daemon could not establish that it saved every observation.
        # Keep the pending request for diagnosis and prevent ordinary retries.
        with self.db:
            self.db.execute(
                "UPDATE jobs SET phase='interrupted', detail='review observation recording was interrupted' "
                "WHERE phase='review' AND id IN (SELECT job_id FROM pending_reviews)",
            )

    def cancel_review(self, job_id):
        with self.db:
            self.db.execute("DELETE FROM pending_reviews WHERE job_id=?", (job_id,))

    def retry(self, job_id):
        with self.db:
            changed = self.db.execute(
                "UPDATE jobs SET phase='review',attempts=0,next_at=0,detail='' "
                "WHERE id=? AND phase IN ('blocked','candidate','stale')", (job_id,),
            )
            if changed.rowcount != 1:
                raise ValueError("retry requires a blocked, candidate or stale job")

    def decide(self, record, disposition, source):
        if not source.strip():
            raise ValueError("a human decision source is required")
        with self.db:
            changed = self.db.execute(
                """UPDATE accept_records SET disposition=?,source=?,decided_at=?
                   WHERE id=? AND disposition IS NULL RETURNING job_id""",
                (disposition, source, now(), record),
            ).fetchone()
            if not changed:
                raise ValueError("no undecided accept record with that ID")
            changed = self.db.execute(
                "UPDATE jobs SET phase=?,detail='',attempts=0,next_at=0 WHERE id=? AND phase='waiting'",
                ("review" if disposition == "revise" else "deferred", changed["job_id"]),
            )
            if changed.rowcount != 1:
                raise ValueError("job is no longer awaiting a decision")

    def candidate(self, job_id):
        row = self.db.execute(
            """SELECT e.body FROM evidence e JOIN jobs j ON j.id=e.job_id
               WHERE j.id=? AND j.phase='candidate' AND e.phase='candidate' ORDER BY e.id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("job has no ready candidate")
        return json.loads(row["body"])

    def begin_review(self, job_id, request):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO pending_reviews (job_id, run, candidate, lens, invocation) VALUES (?, ?, ?, ?, ?)",
                (job_id, request["run"], request["candidate"], request["lens"], request["invocation"]),
            )

    def record_protected(self, job_id, request, round_number, findings):
        # The worker commits through its own connection before calling the observer.
        with closing(sqlite3.connect(self.path)) as db, db:
            if findings:
                db.execute(
                    """INSERT INTO protected_observations
                       (job_id, run, candidate, round_number, lens, invocation, findings)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (job_id, request["run"], request["candidate"], round_number,
                     request["lens"], request["invocation"], json.dumps(findings)),
                )
            db.execute("DELETE FROM pending_reviews WHERE job_id=?", (job_id,))

    def protected(self, job_id):
        # A retry rebuilds from the approved source, so prior repair resolutions
        # must be re-checked against the rebuilt candidate.
        protected = {}
        for row in self.db.execute(
            "SELECT body FROM evidence WHERE job_id=? ORDER BY id", (job_id,),
        ):
            evidence = json.loads(row["body"])
            for review in evidence["reviews"]:
                lens = review["request"]["lens"]
                for finding in review["findings"]:
                    if finding["risk"] != "other":
                        protected[lens, finding["id"]] = {**finding, "lens": lens}
            for finding in evidence["findings"]:
                if finding["risk"] != "other":
                    protected[finding["lens"], finding["id"]] = finding
        for row in self.db.execute(
            "SELECT lens, findings FROM protected_observations WHERE job_id=? ORDER BY id", (job_id,),
        ):
            for finding in json.loads(row["findings"]):
                protected[row["lens"], finding["id"]] = {**finding, "lens": row["lens"]}
        return list(protected.values())

    def status(self):
        rows = []
        for row in self.db.execute("""
            SELECT j.*, e.body FROM jobs j LEFT JOIN evidence e ON e.id =
                (SELECT max(id) FROM evidence WHERE job_id=j.id) ORDER BY j.id
        """):
            job = dict(row)
            body = job.pop("body")
            if body is not None:
                job["evidence"] = json.loads(body)
            if job["phase"] == "interrupted":
                request = self.db.execute(
                    "SELECT run,candidate,lens,invocation FROM pending_reviews WHERE job_id=?", (job["id"],),
                ).fetchone()
                job["pending_review"] = dict(request)
            rows.append(job)
        counts = {}
        for row in rows:
            counts[row["phase"]] = counts.get(row["phase"], 0) + 1
        records = [
            {**dict(row), "findings": json.loads(row["findings"])} for row in
            self.db.execute("SELECT * FROM accept_records ORDER BY id")
        ]
        return {"counts": counts, "jobs": rows, "accept_records": records}
