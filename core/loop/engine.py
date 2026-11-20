from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Event
import time

from .git import Changed, Conflict, Git, job_env
from .review import BLOCKING, brief, findings, lenses, prompt
from .runtime import Busy, Cancelled, MemoryGate, Retry, lock, run


class Engine:
    def __init__(self, cfg, store, cancel=None):
        self.cfg = cfg
        self.store = store
        self.git = Git(cfg["repo"], cfg["state"])
        self.cancel = cancel or Event()
        self.memory = MemoryGate()
        self.executor = ThreadPoolExecutor(max_workers=cfg["max_jobs"])
        self.active = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cancel.set()
        self.executor.shutdown(wait=True)

    def command(self, role, path, job, *, stdin=None, **env):
        return run(
            self.cfg[role], path, self.cancel, self.cfg["timeout_seconds"],
            prompt=stdin, env=job_env(self.cfg, job, **env),
        )

    def review(self, job, base):
        items = []
        prior = [item for item in json.loads(job["report"]) if item["risk"] in BLOCKING]
        selected = dict.fromkeys((*lenses(self.git.paths(base, job["sha"]), self.cfg["lenses"]),
                                  *(item["lens"] for item in prior)))
        with self.git.checkout(job["sha"]) as path:
            for lens in selected:
                previous = [item for item in prior if item["lens"] == lens]
                message = prompt(base, job["branch"], job["sha"], lens, previous)
                output = self.command(
                    "reviewer", path, job, stdin=message, AGENT_LENS=lens,
                    AGENT_MODEL=self.cfg["reviewer_model"],
                )
                self.git.clean(path)
                if self.git.resolve("HEAD", cwd=path) != job["sha"]:
                    raise Conflict("reviewer moved the requested checkout")
                items.extend({**item, "lens": lens} for item in findings(output))
        return items

    def repair(self, job):
        self.git.unoccupied(job["branch"])
        text = brief(job["branch"], job["sha"], json.loads(job["report"]), self.cfg["max_model"])
        with self.git.checkout(job["sha"]) as path:
            self.command("worker", path, job, stdin=text, AGENT_MODEL=self.cfg["worker_model"])
            self.git.clean(path)
            result = self.git.resolve("HEAD", cwd=path)
            if result == job["sha"] or not self.git.ancestor(job["sha"], result):
                raise Conflict("worker must commit a descendant of the reviewed head")
        if self.cancel.is_set():
            raise Cancelled
        self.git.publish(job["branch"], result, job["sha"])
        return result

    def train(self, job, base):
        if self.git.ancestor(job["sha"], base):
            return "merged", "reviewed head is already in target"
        self.git.unoccupied(self.cfg["target"])
        if not self.cfg["verify"]:
            raise Conflict("configure an integration verifier before advancing the target")
        with self.git.checkout(base) as path:
            sha = self.git.candidate(path, job["sha"])
            self.command("verify", path, job, AGENT_CANDIDATE=sha, AGENT_BASE=base)
            self.git.clean(path)
            if self.git.resolve("HEAD", cwd=path) != sha:
                raise Conflict("verifier moved the merge candidate checkout")
        if self.cancel.is_set():
            raise Cancelled
        try:
            self.git.publish(self.cfg["target"], sha, base, verify=(job["branch"], job["sha"]))
        except Changed:
            current = self.git.head(job["branch"])
            if current != job["sha"]:
                return "stale", current
            raise Busy("target advanced during verification") from None
        return "merged", sha

    def step(self, job):
        common = self.git.common()
        key = hashlib.sha256(job["branch"].encode()).hexdigest()
        try:
            with lock(common / f"agent-branch-{key}.lock"):
                if job["branch"] == self.cfg["target"]:
                    raise Conflict("source and target must be distinct")
                current = self.git.head(job["branch"])
                if current != job["sha"]:
                    return "stale", current
                base = self.git.head(self.cfg["target"])
                if not base:
                    raise Conflict("target branch does not exist")
                if job["phase"] == "review":
                    result = self.review(job, base)
                    current = self.git.head(job["branch"])
                    if current != job["sha"]:
                        return "stale", current
                    return "reviewed", result
                if job["phase"] == "repair":
                    return "superseded", self.repair(job)
                with lock(common / "agent-train.lock"):
                    return self.train(job, base)
        except Changed:
            return "stale", self.git.head(job["branch"])

    def collect(self, job, future):
        try:
            phase, result = future.result()
        except Cancelled:
            return
        except Busy as error:
            self.store.update(job, job["phase"], detail=str(error) or "work key is held",
                              attempts=job["attempts"], next_at=time.time() + self.cfg["poll_seconds"])
            return
        except Retry as error:
            attempts = job["attempts"] + 1
            delay = min(self.cfg["retry_max_seconds"],
                        self.cfg["retry_initial_seconds"] * 2 ** min(attempts - 1, 30))
            if attempts >= self.cfg["retry_limit"]:
                self.store.update(job, "blocked", detail=str(error), attempts=attempts)
            else:
                self.store.update(job, job["phase"], detail=str(error), attempts=attempts,
                                  next_at=time.time() + delay)
            return
        except Exception as error:
            self.store.update(job, "blocked", detail=f"{type(error).__name__}: {error}")
            return
        if phase == "reviewed":
            rounds = job["rounds"] + bool(result)
            if not result:
                self.store.update(job, "train", report=result)
            elif rounds >= self.cfg["max_rounds"] and not any(
                finding["risk"] in BLOCKING for finding in result
            ):
                self.store.accept_record(job, rounds, result)
            else:
                self.store.update(job, "repair", rounds=rounds, report=result)
        elif phase == "superseded" or (phase == "stale" and result):
            self.store.successor(job, phase, result)
        else:
            self.store.update(job, phase, detail=result or "")

    def tick(self):
        for job_id, (job, future) in list(self.active.items()):
            if future.done():
                del self.active[job_id]
                self.collect(job, future)
        self.store.ingest()
        active_branches = {job["branch"] for job, _ in self.active.values()}
        train_busy = any(job["phase"] == "train" for job, _ in self.active.values())
        for job in self.store.ready():
            if self.cancel.is_set() or len(self.active) >= self.cfg["max_jobs"]:
                break
            if job["branch"] in active_branches or (job["phase"] == "train" and train_busy):
                continue
            profile = self.cfg["memory"]["review" if job["phase"] == "review" else "test"]
            if not self.memory.allows(profile["max_anon_mib"] * 2**20,
                                      profile["min_available_mib"] * 2**20):
                continue
            self.active[job["id"]] = job, self.executor.submit(self.step, job)
            active_branches.add(job["branch"])
            train_busy |= job["phase"] == "train"
