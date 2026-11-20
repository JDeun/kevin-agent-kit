from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
import hashlib
import json
import shutil
import tempfile
import time
from uuid import uuid4

from .config import config, repo_id
from .git import Conflict, Git
from .review import BLOCKING, brief, lenses, prompt, review_receipt, verification
from .runtime import Cancelled, MemoryGate, Retry, private_dir
from .sandbox import execute, redact
from .store import now


class SourceChanged(Conflict):
    pass


class InterruptedAttempt(Exception):
    def __init__(self, cause, evidence):
        self.cause = cause
        self.evidence = evidence


def check_inputs(cfg, evidence):
    git = Git(cfg["repo"])
    if repo_id(git.common()) != evidence["repo_id"]:
        raise Conflict("repository identity changed")
    if git.head(evidence["branch"]) != evidence["head"]:
        raise SourceChanged("enqueued source changed; explicitly enqueue the new SHA")
    if git.head(cfg["target"]) != evidence["base"]:
        raise Conflict("target changed; retry for a fresh candidate")
    if evidence["policy"] != cfg["policy_id"]:
        raise Conflict("execution policy changed; restart and retry")


def check_candidate(cfg, evidence):
    check_inputs(cfg, evidence)
    if cfg.get("profile") and config(cfg["profile"])["policy_id"] != evidence["policy"]:
        raise Conflict("execution policy changed; restart and retry")
    if evidence["expires_at"] <= time.time():
        raise Conflict("candidate evidence expired; retry the job")


def export_candidate(cfg, evidence, destination):
    check_candidate(cfg, evidence)
    path = cfg["state"] / "candidates" / f"{evidence['run']}.bundle"
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != evidence["bundle_sha256"]:
            raise Conflict("candidate bundle does not match the evidence")
        source.seek(0)
        # Prepare the file before exposing the requested output path. A hard link
        # publishes it atomically and fails if the user already has that path.
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".candidate-") as directory:
            staged = Path(directory) / "candidate.bundle"
            with staged.open("xb") as target:
                staged.chmod(0o600)
                shutil.copyfileobj(source, target)
            check_candidate(cfg, evidence)
            destination.hardlink_to(staged)


class Engine:
    def __init__(self, cfg, store, cancel=None, *, runner=execute):
        self.cfg = cfg
        self.store = store
        self.git = Git(cfg["repo"])
        self.cancel = cancel or Event()
        self.runner = runner
        self.memory = MemoryGate()
        self.executor = ThreadPoolExecutor(max_workers=cfg["max_jobs"])
        self.active = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cancel.set()
        self.executor.shutdown(wait=True)

    def authority(self, evidence):
        check_inputs(self.cfg, evidence)
        if self.cfg.get("profile") and config(self.cfg["profile"])["policy_id"] != evidence["policy"]:
            raise Conflict("execution policy changed; restart and retry")
        if self.cancel.is_set():
            raise Cancelled

    def command(self, role, argv, work, evidence, *, stdin, baseline=None):
        self.authority(evidence)
        limit = self.cfg["memory"]["test" if role in {"verify", "attest"} else "review"]
        if not self.memory.allows(limit["max_anon_mib"] * 2**20, limit["min_available_mib"] * 2**20):
            raise Retry("stage memory limit")
        extra = {
            "AGENT_BASE": evidence["base"], "AGENT_HEAD": evidence["head"],
            "AGENT_CANDIDATE": evidence["candidate"], "AGENT_TARGET": evidence["target"],
            "AGENT_BRANCH": evidence["branch"],
        }
        if role in {"reviewer", "worker"}:
            extra["AGENT_MODEL"] = self.cfg[f"{role}_model"]
        return self.runner(argv, work, self.cancel, self.cfg["timeout_seconds"], role=role,
                           policy=self.cfg["execution"], stdin=stdin, extra=extra, checks=baseline)

    def review(self, work, evidence, paths, prior, history, rounds):
        items = list(prior)
        selected = dict.fromkeys((*lenses(paths, self.cfg["lenses"]), *(item["lens"] for item in prior)))
        for lens in selected:
            previous = [item for item in prior if item["lens"] == lens]
            request = {**evidence, "lens": lens, "paths": paths, "invocation": uuid4().hex}
            try:
                argv = self.cfg["lens_reviewers"].get(lens, self.cfg["reviewer"])
                output = self.command("reviewer", argv, work, evidence,
                                      stdin=prompt(request, previous))
                review = review_receipt(output, request, previous)
            except (OSError, ValueError, Retry) as cause:
                report = {**evidence, "reviews": history, "findings": items, "rounds": rounds}
                raise InterruptedAttempt(cause, report) from cause
            env_names = self.cfg["execution"].get("env", {}).get("reviewer", [])
            review["tool_version"] = redact(review["tool_version"], env_names)
            review["resolved"] = {key: redact(value, env_names) for key, value in review["resolved"].items()}
            for item in review["findings"]:
                item["detail"] = redact(item["detail"], env_names)
            review["collected_at"] = now()
            history.append(review)
            items = [item for item in items if item["lens"] != lens]
            items.extend({**item, "lens": lens} for item in review["findings"])
        return items

    def step(self, job, prior):
        base = self.git.head(self.cfg["target"])
        branch, head = job["branch"], job["sha"]
        if branch == self.cfg["target"] or not base:
            raise Conflict("source and existing target must be distinct branches")
        if self.git.head(branch) != head:
            return "stale", "enqueued source changed; explicitly enqueue the new SHA", None
        if self.git.ancestor(head, base):
            return "included", "source is already included in target", None
        merge_base = self.git.run("merge-base", base, head).stdout.strip()
        with self.git.snapshot(self.cfg["state"], base, head) as (git, root), ExitStack() as repairs:
            baseline = root / "baseline"
            baseline.mkdir()
            git.run("checkout", "--quiet", "--detach", base)
            git.run("checkout-index", "--all", f"--prefix={baseline}/")
            candidate = git.candidate(base, head)
            evidence = {
                "repo_id": self.cfg["repo_id"], "target": self.cfg["target"], "base": base,
                "head": head, "branch": branch, "merge_base": merge_base, "candidate": candidate,
                "policy": self.cfg["policy_id"], "run": uuid4().hex,
            }
            history = []
            for rounds in range(1, self.cfg["max_rounds"] + 1):
                paths = sorted(git.paths(base, evidence["candidate"]))
                items = self.review(git.repo, evidence, paths, prior, history, rounds)
                prior = [item for item in items if item["risk"] in BLOCKING]
                report = {**evidence, "reviews": history, "rounds": rounds, "findings": items}
                if not items:
                    break
                if rounds == self.cfg["max_rounds"] or not (self.cfg["worker"] and self.cfg["repair_paths"]):
                    self.authority(evidence)
                    if prior:
                        return "blocked", "protected findings require resolution", report
                    return "waiting", "human decision needed", report
                try:
                    self.command("worker", self.cfg["worker"], git.repo, evidence,
                                 stdin=brief(evidence, items, self.cfg["repair_paths"]))
                    repaired = git.repair(self.cfg["repair_paths"])
                    with ExitStack() as next_checkout:
                        git, _ = next_checkout.enter_context(git.snapshot(self.cfg["state"], base, repaired))
                        git.run("checkout", "--quiet", "--detach", repaired)
                        repairs.close()
                        repairs.enter_context(next_checkout.pop_all())
                    evidence["candidate"] = repaired
                except (OSError, ValueError, Retry) as cause:
                    raise InterruptedAttempt(cause, report) from cause

            receipts = []
            for name, argv in sorted(self.cfg["checks"].items()):
                request = {**evidence, "check": name, "invocation": uuid4().hex}
                try:
                    output = self.command("verify", argv, git.repo, evidence, stdin=None, baseline=baseline)
                    observation = {"exit_status": 0, "stdout": output}
                    attestation = self.command(
                        "attest", self.cfg["attestors"][name], None, evidence,
                        stdin=json.dumps({"request": request, "observation": observation}, ensure_ascii=True),
                    )
                    receipts.append({
                        **verification(attestation, request), "collected_at": now(),
                        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                    })
                except (OSError, ValueError, Retry) as cause:
                    raise InterruptedAttempt(cause, {**report, "checks": receipts}) from cause
            if not receipts:
                raise Conflict("configure mandatory checks before preparing candidates")
            self.authority(evidence)
            directory = private_dir(self.cfg["state"] / "candidates")
            bundle = directory / f"{evidence['run']}.bundle"
            staged = root / "candidate.bundle"
            git.bundle(staged, evidence["candidate"])
            staged.chmod(0o600)
            with staged.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            self.authority(evidence)
            staged.replace(bundle)
            return "candidate", "awaiting repository owner", {
                **report, "checks": receipts, "expires_at": time.time() + self.cfg["evidence_ttl_seconds"],
                "bundle_sha256": digest,
            }

    def collect(self, job, future):
        try:
            phase, detail, evidence = future.result()
        except Cancelled:
            return
        except InterruptedAttempt as error:
            self.record_failure(job, error.cause, error.evidence)
            return
        except Exception as error:
            self.record_failure(job, error, None)
            return
        self.store.update(job, phase, detail=detail, evidence=evidence)

    def record_failure(self, job, error, evidence):
        if isinstance(error, Retry):
            attempts = job["attempts"] + 1
            delay = min(self.cfg["retry_max_seconds"],
                        self.cfg["retry_initial_seconds"] * 2 ** min(attempts - 1, 30))
            if attempts < self.cfg["retry_limit"]:
                self.store.update(job, "review", detail=str(error), attempts=attempts,
                                  next_at=time.time() + delay, evidence=evidence)
            else:
                self.store.update(job, "blocked", detail=str(error), attempts=attempts, evidence=evidence)
            return
        phase = "stale" if isinstance(error, SourceChanged) else "blocked"
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        self.store.update(job, phase, detail=detail[:500], evidence=evidence)

    def tick(self):
        for job_id, (job, future) in list(self.active.items()):
            if future.done():
                del self.active[job_id]
                self.collect(job, future)
        active_branches = {job["branch"] for job, _ in self.active.values()}
        for job in self.store.ready():
            if self.cancel.is_set() or len(self.active) >= self.cfg["max_jobs"]:
                break
            if job["branch"] in active_branches:
                continue
            prior = self.store.protected(job["id"])
            self.active[job["id"]] = job, self.executor.submit(self.step, job, prior)
            active_branches.add(job["branch"])
