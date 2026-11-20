from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3
import time

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
REQUEST = re.compile(r"NEED-REVIEW ([^\s]+) ([0-9a-f]+)\Z")


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, state):
        state.mkdir(parents=True, exist_ok=True)
        self.queue = state / "requests.log"
        self.db = sqlite3.connect(state / "queue.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, branch TEXT NOT NULL, sha TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'review', rounds INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '[]',
                UNIQUE(branch, sha)
            );
            CREATE TABLE IF NOT EXISTS accept_records (
                id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, recorded_at TEXT NOT NULL,
                branch TEXT NOT NULL, sha TEXT NOT NULL, rounds INTEGER NOT NULL,
                findings TEXT NOT NULL, disposition TEXT, source TEXT, decided_at TEXT
            );
        """)

    @contextmanager
    def connect(self):
        try:
            yield self
        finally:
            self.db.close()

    def enqueue(self, branch, sha):
        if not SHA.fullmatch(sha) or not REQUEST.fullmatch(f"NEED-REVIEW {branch} {sha}"):
            raise ValueError("expected branch name and full lowercase commit SHA")
        with self.queue.open("a", encoding="utf-8") as stream:
            stream.write(f"NEED-REVIEW {branch} {sha}\n")

    def ingest(self):
        if not self.queue.exists():
            return 0
        rejected = 0
        with self.queue.open(encoding="utf-8", errors="replace") as stream, self.db:
            for line in stream:
                if not line.endswith("\n"):
                    break
                match = REQUEST.fullmatch(line.rstrip("\r\n"))
                if not match or not SHA.fullmatch(match[2]):
                    rejected += 1
                    continue
                self.db.execute("INSERT OR IGNORE INTO jobs(branch,sha) VALUES (?,?)", match.groups())
        return rejected

    def ready(self):
        return self.db.execute(
            "SELECT * FROM jobs WHERE phase IN ('review','repair','train') AND next_at <= ? ORDER BY id",
            (time.time(),),
        ).fetchall()

    def update(self, job, phase, *, detail="", attempts=0, next_at=0, rounds=None, report=None):
        with self.db:
            self._update(job, phase, detail, attempts, next_at, rounds, report)

    def _update(self, job, phase, detail, attempts, next_at, rounds, report):
        self.db.execute(
            """UPDATE jobs SET phase=?, detail=?, attempts=?, next_at=?,
               rounds=coalesce(?, rounds), report=coalesce(?, report) WHERE id=?""",
            (phase, detail, attempts, next_at, rounds,
             json.dumps(report, ensure_ascii=True) if report is not None else None, job["id"]),
        )

    def successor(self, job, phase, head):
        with self.db:
            self._update(job, phase, "", 0, 0, None, None)
            self.db.execute(
                """INSERT INTO jobs(branch,sha,rounds,report) VALUES (?,?,?,?)
                   ON CONFLICT(branch,sha) DO UPDATE SET rounds=max(rounds,excluded.rounds),
                   report=CASE WHEN jobs.report='[]' THEN excluded.report ELSE jobs.report END
                   WHERE phase='review'""",
                (job["branch"], head, job["rounds"], job["report"]),
            )

    def accept_record(self, job, rounds, report):
        with self.db:
            self._update(job, "waiting", "human decision needed", 0, 0, rounds, report)
            self.db.execute(
                """INSERT INTO accept_records (job_id,recorded_at,branch,sha,rounds,findings)
                   VALUES (?,?,?,?,?,?)""",
                (job["id"], now(), job["branch"], job["sha"], rounds, json.dumps(report)),
            )

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
                """UPDATE jobs SET phase=?,detail='',rounds=0 WHERE id=? AND phase='waiting'""",
                ("repair" if disposition == "revise" else "deferred", changed["job_id"]),
            )
            if changed.rowcount != 1:
                raise ValueError("job is no longer awaiting a decision")

    def status(self):
        rows = [dict(row) for row in self.db.execute(
            "SELECT id,branch,sha,phase,rounds,attempts,next_at,detail FROM jobs ORDER BY id"
        )]
        counts = {}
        for row in rows:
            counts[row["phase"]] = counts.get(row["phase"], 0) + 1
        records = [
            {**dict(row), "findings": json.loads(row["findings"])} for row in
            self.db.execute("SELECT * FROM accept_records ORDER BY id")
        ]
        return {"counts": counts, "jobs": rows, "accept_records": records}
