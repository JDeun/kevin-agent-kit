from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import stat
import time

from .design import TASK_KINDS
from .runtime import Busy, lock
from ..verdict import BLOCKING

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# Phases whose last review left no protected finding open. Every other job on a
# branch still owes its findings to the next review of that branch.
SETTLED_PHASES = ("candidate", "included", "waiting", "deferred")
OPEN_ATTEMPT = "SELECT 1 FROM review_attempts a WHERE a.job_id=jobs.id AND a.closed_at IS NULL"
APPEND_ONLY = "BEGIN SELECT RAISE(ABORT, 'the review journal is append-only'); END"
# An attempt is open until `closed_at` is set. Nothing deletes an attempt or a journal row.
JOURNAL = (
    """CREATE TABLE IF NOT EXISTS review_attempts (
        invocation TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs,
        run TEXT NOT NULL, candidate TEXT NOT NULL, lens TEXT NOT NULL, round_number INTEGER NOT NULL,
        opened_at TEXT NOT NULL, outcome TEXT, closed_at TEXT, reason TEXT, output_sha256 TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS finding_journal (
        id INTEGER PRIMARY KEY, invocation TEXT NOT NULL REFERENCES review_attempts,
        finding_id TEXT NOT NULL, body TEXT NOT NULL, recorded_at TEXT NOT NULL,
        UNIQUE(invocation, finding_id)
    )""",
    """CREATE TABLE IF NOT EXISTS review_output (
        id INTEGER PRIMARY KEY, invocation TEXT NOT NULL REFERENCES review_attempts, chunk BLOB NOT NULL
    )""",
    f"CREATE TRIGGER IF NOT EXISTS journal_keeps_rows BEFORE DELETE ON finding_journal {APPEND_ONLY}",
    f"CREATE TRIGGER IF NOT EXISTS journal_keeps_bodies BEFORE UPDATE ON finding_journal {APPEND_ONLY}",
    f"CREATE TRIGGER IF NOT EXISTS attempts_keep_rows BEFORE DELETE ON review_attempts {APPEND_ONLY}",
    "CREATE TRIGGER IF NOT EXISTS attempts_close_once BEFORE UPDATE ON review_attempts "
    f"WHEN OLD.closed_at IS NOT NULL {APPEND_ONLY}",
)


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
                    kind TEXT NOT NULL DEFAULT 'unclassified',
                    inherits INTEGER REFERENCES jobs,
                    created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
                    UNIQUE(branch, sha)
                )
            """)
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
            for name, definition in (
                ("kind", "TEXT NOT NULL DEFAULT 'unclassified'"), ("inherits", "INTEGER REFERENCES jobs"),
                ("created_at", "REAL NOT NULL DEFAULT 0"), ("updated_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS design_reviews (branch TEXT PRIMARY KEY, body TEXT NOT NULL)
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS review_rounds (
                    job_id INTEGER NOT NULL REFERENCES jobs, run TEXT NOT NULL, number INTEGER NOT NULL,
                    PRIMARY KEY (job_id, run, number)
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs,
                    phase TEXT NOT NULL, body TEXT NOT NULL
                )
            """)
            for statement in JOURNAL:
                self.db.execute(statement)
            self.migrate_observations(names)
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

    def migrate_observations(self, names):
        """Carry a 0.3.4 queue into the journal: observations become rows, pending requests stay open."""
        stamp = now()
        if "protected_observations" in names:
            for row in self.db.execute("SELECT * FROM protected_observations ORDER BY id").fetchall():
                self.db.execute(
                    "INSERT OR IGNORE INTO review_attempts VALUES (?,?,?,?,?,?,?,'complete',?,NULL,NULL)",
                    (row["invocation"], row["job_id"], row["run"], row["candidate"], row["lens"],
                     row["round_number"], stamp, stamp))
                self._journal(self.db, row["invocation"], json.loads(row["findings"]))
            self.db.execute("DROP TABLE protected_observations")
        if "pending_reviews" in names:
            for row in self.db.execute("SELECT * FROM pending_reviews").fetchall():
                self.db.execute(
                    "INSERT OR IGNORE INTO review_attempts VALUES (?,?,?,?,?,0,?,NULL,NULL,NULL,NULL)",
                    (row["invocation"], row["job_id"], row["run"], row["candidate"], row["lens"], stamp))
            self.db.execute("DROP TABLE pending_reviews")

    def enqueue(self, branch, sha, *, kind="unclassified", inherits=None):
        """Queue a source SHA and return its job row. A named parent may be any other job."""
        if not SHA.fullmatch(sha):
            raise ValueError("expected a full lowercase commit SHA")
        if kind not in TASK_KINDS:
            raise ValueError("unknown task kind")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if inherits is not None:
                source = self.db.execute("SELECT branch,sha,phase FROM jobs WHERE id=?", (inherits,)).fetchone()
                if source is None or (source["branch"], source["sha"]) == (branch, sha):
                    raise ValueError("--inherit-from names an unknown job or this job itself")
            stamp = time.time()
            self.db.execute("INSERT OR IGNORE INTO jobs(branch,sha,kind,inherits,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,?)", (branch, sha, kind, inherits, stamp, stamp))
            recorded = self.db.execute("SELECT id,kind,phase,inherits FROM jobs WHERE branch=? AND sha=?",
                                       (branch, sha)).fetchone()
            if inherits is not None and recorded["inherits"] != inherits:
                raise ValueError("this branch and SHA are already queued without that inheritance")
            if inherits is not None and source["phase"] == "interrupted":
                if recorded["phase"] != "review":
                    raise ValueError("that job was already reviewed without these findings; "
                                     "resume the interrupted job or enqueue a new SHA")
                # The successor now owes the journaled findings; the interrupted job is done.
                self.db.execute("UPDATE jobs SET phase='superseded',detail=?,updated_at=? WHERE id=?",
                                (f"protected findings carried to job {recorded['id']}", stamp, inherits))
            if kind != "unclassified" and recorded["kind"] != kind:
                if recorded["kind"] != "unclassified" or recorded["phase"] not in ("review", "design-needed"):
                    raise ValueError("this branch and SHA already have a different task kind")
                # Correct the classification of an untouched queue entry; keep the SHA and evidence.
                self.db.execute(
                    "UPDATE jobs SET kind=?,updated_at=?,"
                    "phase=CASE WHEN phase='design-needed' THEN 'review' ELSE phase END WHERE id=?",
                    (kind, time.time(), recorded["id"]))
                self.db.execute("INSERT INTO evidence (job_id,phase,body) VALUES (?,?,?)",
                                (recorded["id"], "reclassified",
                                 json.dumps({"from": "unclassified", "to": kind, "at": now()})))
            return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (recorded["id"],)).fetchone())

    def latest_forest(self, branch):
        return self.db.execute(
            """SELECT j.id,j.sha FROM jobs j JOIN evidence e ON e.job_id=j.id
               WHERE j.branch=? AND e.phase='forest' ORDER BY e.id DESC LIMIT 1""", (branch,),
        ).fetchone()

    def design(self, branch):
        row = self.db.execute("SELECT body FROM design_reviews WHERE branch=?", (branch,)).fetchone()
        return json.loads(row["body"]) if row else None

    @contextmanager
    def design_lock(self, branch):
        name = hashlib.sha256(branch.encode("utf-8")).hexdigest()
        try:
            with lock(self.path.parent / f"design-{name}.lock"):
                yield
        except Busy:
            raise ValueError("a design review is already running on this branch") from None

    def design_protected(self, branch):
        previous = self.design(branch) or {}
        if "open_protected" in previous:
            return previous["open_protected"]
        # Older reports record the findings only in each complete lens receipt.
        return {review["request"]["lens"]: [item for item in review["findings"] if item["risk"] in BLOCKING]
                for review in previous.get("reviews", [])}

    def save_design(self, report):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            forest = self.latest_forest(report["branch"])
            if report["status"] != "incomplete" and (forest["id"] if forest else None) != report["forest_job"]:
                raise ValueError("branch hold changed during design review; review the new redesign")
            self.db.execute(
                "INSERT OR REPLACE INTO design_reviews (branch,body) VALUES (?,?)",
                (report["branch"], json.dumps(report)),
            )

    def record_round(self, job, run, number, *, escalation=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO review_rounds VALUES (?,?,?)", (job["id"], run, number))
            if escalation:
                self._update(db, job, "forest", "redesign required after repeated finding classes",
                             0, 0, escalation)

    def ready(self):
        # A job with an unsettled review attempt is never dispatched again.
        return self.db.execute(
            f"SELECT * FROM jobs WHERE phase='review' AND next_at <= ? AND NOT EXISTS ({OPEN_ATTEMPT}) "
            "ORDER BY id", (time.time(),),
        ).fetchall()

    def update(self, job, phase, *, detail="", attempts=0, next_at=0, evidence=None):
        with self.db:
            self._update(self.db, job, phase, detail, attempts, next_at, evidence)

    def _update(self, db, job, phase, detail, attempts, next_at, evidence):
        db.execute("UPDATE jobs SET phase=?,detail=?,attempts=?,next_at=?,updated_at=? WHERE id=?",
                   (phase, detail, attempts, next_at, time.time(), job["id"]))
        if evidence is not None:
            db.execute("INSERT INTO evidence (job_id,phase,body) VALUES (?,?,?)",
                       (job["id"], phase, json.dumps(evidence)))
        if phase == "waiting":
            db.execute(
                "INSERT INTO accept_records (job_id,recorded_at,branch,sha,rounds,findings) VALUES (?,?,?,?,?,?)",
                (job["id"], now(), job["branch"], job["sha"], evidence["rounds"], json.dumps(evidence["findings"])),
            )

    def interrupt_unsettled(self):
        """Hold every queued job whose review attempt never reached its close marker."""
        with self.db:
            self.db.execute(
                "UPDATE jobs SET phase='interrupted', detail='a review attempt did not settle; "
                f"inspect it with evidence, then resume' WHERE phase='review' AND EXISTS ({OPEN_ATTEMPT})")

    def resume(self, job_id):
        """Return an interrupted job to review. Its journaled findings stay and must be resolved."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute(
                "UPDATE jobs SET phase='review',attempts=0,next_at=0,detail='' WHERE id=? AND phase='interrupted'",
                (job_id,))
            if changed.rowcount != 1:
                raise ValueError("resume requires an interrupted job")
            self.db.execute(
                "UPDATE review_attempts SET outcome='resumed',closed_at=? WHERE job_id=? AND closed_at IS NULL",
                (now(), job_id))

    def held(self, job_id):
        """The unsettled attempts of a job with the reviewer output kept as evidence."""
        rows = self.db.execute(
            "SELECT * FROM review_attempts WHERE job_id=? AND (closed_at IS NULL OR outcome='resumed') "
            "ORDER BY opened_at, invocation", (job_id,)).fetchall()
        return [{**dict(row), "output": self._output(row["invocation"]),
                 "findings": self._journaled(row["invocation"])} for row in rows]

    def _output(self, invocation):
        chunks = self.db.execute("SELECT chunk FROM review_output WHERE invocation=? ORDER BY id", (invocation,))
        return b"".join(row["chunk"] for row in chunks).decode("utf-8", errors="replace")

    def _journaled(self, invocation):
        rows = self.db.execute("SELECT body FROM finding_journal WHERE invocation=? ORDER BY id", (invocation,))
        return [json.loads(row["body"]) for row in rows]

    def retry(self, job_id):
        with self.db:
            changed = self.db.execute(
                "UPDATE jobs SET phase='review',attempts=0,next_at=0,detail='' "
                "WHERE id=? AND phase IN ('blocked','candidate','stale','design-needed','forest-held','wiki-needed') "
                f"AND NOT EXISTS ({OPEN_ATTEMPT})", (job_id,),
            )
            if changed.rowcount != 1:
                raise ValueError("retry requires a blocked, candidate, stale or design-pending job")

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

    # The worker thread journals through its own connection, and every call commits by itself.
    def open_attempt(self, job_id, request, round_number):
        """Open a review attempt before its reviewer starts. Only `settle` or `resume` closes it."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO review_attempts (invocation,job_id,run,candidate,lens,round_number,opened_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (request["invocation"], job_id, request["run"], request["candidate"], request["lens"],
                 round_number, now()))

    def observe(self, invocation, text, findings):
        """Commit newly read reviewer output together with the protected findings it completed."""
        with closing(sqlite3.connect(self.path)) as db, db:
            if text:
                db.execute("INSERT INTO review_output (invocation,chunk) VALUES (?,?)",
                           (invocation, text.encode("utf-8", errors="replace")))
            self._journal(db, invocation, findings)

    def settle(self, invocation, outcome, digest, reason=None):
        """Write the close marker of a settled attempt, or keep an unsettled one open with its reason."""
        with closing(sqlite3.connect(self.path)) as db, db:
            if outcome is None:
                db.execute("UPDATE review_attempts SET reason=?,output_sha256=? WHERE invocation=?",
                           (reason, digest, invocation))
            else:
                # The journal holds every finding, so the raw output of a closed attempt is dropped.
                db.execute("DELETE FROM review_output WHERE invocation=?", (invocation,))
                db.execute("UPDATE review_attempts SET outcome=?,closed_at=?,output_sha256=? WHERE invocation=?",
                           (outcome, now(), digest, invocation))

    @staticmethod
    def _journal(db, invocation, findings):
        for finding in findings:
            db.execute("INSERT OR IGNORE INTO finding_journal (invocation,finding_id,body,recorded_at) "
                       "VALUES (?,?,?,?)", (invocation, finding["id"], json.dumps(finding), now()))

    def inherited_protected(self, job):
        """Protected findings this job must still resolve.

        Sources are every other job on its branch whose last state left findings open
        and its named parent, followed transitively.
        """
        marks = ",".join("?" * len(SETTLED_PHASES))
        sources, pending = {job["id"]}, [job["id"]]
        while pending:
            current = self.db.execute("SELECT branch,inherits FROM jobs WHERE id=?", (pending.pop(),)).fetchone()
            linked = {row["id"] for row in self.db.execute(
                f"SELECT id FROM jobs WHERE branch=? AND phase NOT IN ({marks})",
                (current["branch"], *SETTLED_PHASES))}
            if current["inherits"] is not None:
                linked.add(current["inherits"])
            pending.extend(linked - sources)
            sources |= linked
        merged = {}
        for source in (*sorted(sources - {job["id"]}), job["id"]):
            for finding in self.protected(source):
                merged[finding["lens"], finding["id"]] = finding
        return list(merged.values())

    def protected(self, job_id):
        # A retry rebuilds from the approved source, so prior repair resolutions
        # must be re-checked against the rebuilt candidate.
        protected = {}
        for row in self.db.execute(
            "SELECT body FROM evidence WHERE job_id=? ORDER BY id", (job_id,),
        ):
            evidence = json.loads(row["body"])
            for review in evidence.get("reviews", []):
                lens = review["request"]["lens"]
                for finding in review["findings"]:
                    if finding["risk"] != "other":
                        protected[lens, finding["id"]] = {**finding, "lens": lens}
            for finding in evidence.get("findings", []):
                if finding["risk"] != "other":
                    protected[finding["lens"], finding["id"]] = finding
        for row in self.db.execute(
            """SELECT a.lens, f.body FROM finding_journal f JOIN review_attempts a USING (invocation)
               WHERE a.job_id=? ORDER BY f.id""", (job_id,),
        ):
            finding = json.loads(row["body"])
            protected[row["lens"], finding["id"]] = {**finding, "lens": row["lens"]}
        return list(protected.values())

    def status(self):
        # One read transaction so counts, jobs and counters describe the same moment.
        self.db.execute("BEGIN")
        try:
            return self._status()
        finally:
            self.db.rollback()

    def _status(self):
        rows = []
        for row in self.db.execute("""
            SELECT j.*, e.body FROM jobs j LEFT JOIN evidence e ON e.id =
                (SELECT max(id) FROM evidence WHERE job_id=j.id) ORDER BY j.id
        """):
            job = dict(row)
            body = job.pop("body")
            if body is not None:
                job["evidence"] = json.loads(body)
            request = self.db.execute(
                """SELECT a.run, a.candidate, a.lens, a.invocation, a.reason,
                          (SELECT count(*) FROM finding_journal f WHERE f.invocation=a.invocation) AS journaled,
                          (SELECT coalesce(sum(length(chunk)), 0) FROM review_output o
                           WHERE o.invocation=a.invocation) AS output_bytes
                   FROM review_attempts a WHERE a.job_id=? AND a.closed_at IS NULL
                   ORDER BY a.opened_at DESC, a.invocation LIMIT 1""", (job["id"],),
            ).fetchone()
            if request:
                job["pending_review"] = dict(request)
            rows.append(job)
        counts = {}
        for row in rows:
            counts[row["phase"]] = counts.get(row["phase"], 0) + 1
        records = [
            {**dict(row), "findings": json.loads(row["findings"])} for row in
            self.db.execute("SELECT * FROM accept_records ORDER BY id")
        ]
        merge_failures = dict(self.db.execute("""
            SELECT json_extract(body, '$.merge_failure') AS reason, count(*) FROM evidence
            WHERE reason IS NOT NULL GROUP BY reason ORDER BY reason
        """))
        stamp = time.time()
        runnable = [row for row in rows if row["phase"] == "review" and row["next_at"] <= stamp]
        started = [row["updated_at"] for row in runnable if row["updated_at"]]
        metrics = {
            "queue_length": counts.get("review", 0),
            "runnable": len(runnable),
            "oldest_runnable_age_seconds": round(stamp - min(started), 1) if started else None,
            "review_rounds": self.db.execute("SELECT count(*) FROM review_rounds").fetchone()[0],
            "forest_escalations": counts.get("forest", 0),
            "merge_failures": merge_failures,
        }
        return {"counts": counts, "jobs": rows, "accept_records": records, "metrics": metrics}
