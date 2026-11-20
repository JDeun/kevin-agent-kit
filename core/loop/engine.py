from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from uuid import uuid4

from .config import config, repo_id
from .evidence import canonical, signed_receipt, source_id
from .git import Conflict, Git
from .review import BLOCKING, brief, lenses, observed_review, prompt, review_receipt, verified_check
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


def candidate_bundle(git, root, evidence, scope):
    prerequisites = [evidence["base"], evidence["head"]] if scope == "incremental" else []
    path = root / f"{scope}.bundle"
    path.unlink(missing_ok=True)
    git.bundle(path, evidence["candidate"], *prerequisites)
    path.chmod(0o400)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return path, {
        "bundle_sha256": digest, "bundle_scope": scope, "bundle_prerequisites": prerequisites,
    }


class Engine:
    def __init__(self, cfg, store, cancel=None, *, runner=execute, authenticate=signed_receipt):
        self.cfg = cfg
        self.store = store
        self.git = Git(cfg["repo"])
        self.cancel = cancel or Event()
        self.runner = runner
        self.authenticate = authenticate
        self.memory = MemoryGate()
        self.executor = ThreadPoolExecutor(max_workers=cfg["max_jobs"])
        self.active = {}

    def __enter__(self):
        self.store.recover_pending_reviews()
        return self

    def __exit__(self, exc_type, *_):
        self.cancel.set()
        self.executor.shutdown(wait=True)
        if exc_type is None:
            for job, future in self.active.values():
                self.collect(job, future)
        self.active.clear()

    def authority(self, evidence):
        check_inputs(self.cfg, evidence)
        if self.cfg.get("profile") and config(self.cfg["profile"])["policy_id"] != evidence["policy"]:
            raise Conflict("execution policy changed; restart and retry")
        if self.cancel.is_set():
            raise Cancelled

    def command(self, role, argv, work, evidence, *, stdin, bundle=None):
        self.authority(evidence)
        limit = self.cfg["memory"]["test" if role == "verify" else "review"]
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
                           policy=self.cfg["execution"], stdin=stdin, extra=extra, bundle=bundle)

    def review(self, job_id, work, package, evidence, paths, prior, history, rounds):
        bundle, scope = package
        observer = self.cfg["evidence"]["observer"]
        items = list(prior)
        selected = dict.fromkeys((*lenses(paths, self.cfg["lenses"]), *(item["lens"] for item in prior)))
        for lens in selected:
            previous = [item for item in prior if item["lens"] == lens]
            request = {**evidence, "kind": "review", "lens": lens, "paths": paths, "invocation": uuid4().hex}
            review = None
            try:
                argv = self.cfg["reviewer"] if lens == "general" else self.cfg["lens_reviewers"].get(lens)
                if not argv:
                    raise ValueError(f"configure a dedicated review adapter for the {lens} lens")
                self.store.begin_review(job_id, request)
                output = self.command("reviewer", argv, work, evidence, stdin=prompt(request, previous))
                review = review_receipt(output, request, previous)
                env_names = self.cfg["execution"].get("env", {}).get("reviewer", [])
                review["tool_version"] = redact(review["tool_version"], env_names)
                review["resolved"] = {key: redact(value, env_names) for key, value in review["resolved"].items()}
                for item in review["findings"]:
                    item["detail"] = redact(item["detail"], env_names)
                self.store.record_protected(job_id, request, rounds,
                                            [item for item in review["findings"] if item["risk"] in BLOCKING])
                review_input = {"review": review, "prior_protected": previous}
                observation = {**request, **scope, "retention_seconds": observer["retention_seconds"],
                               "kind": "review-observation", "invocation": uuid4().hex,
                               "review_input_sha256": hashlib.sha256(canonical(review_input)).hexdigest()}
                key = self.cfg["review_key"]
                output = self.command(
                    "review-observer", self.cfg["review_observer"], None, evidence, bundle=bundle,
                    stdin=json.dumps({"request": observation, **review_input}, ensure_ascii=True),
                )
                observed = observed_review(self.authenticate(output, key), observation, review["resolved"])
                review["observation"] = {**observed, "source": source_id(key)}
            except (OSError, ValueError, Retry, sqlite3.Error) as cause:
                if review is not None:
                    known = {item["id"] for item in items if item["lens"] == lens}
                    items.extend({**item, "lens": lens} for item in review["findings"]
                                 if item["risk"] in BLOCKING and item["id"] not in known)
                report = {**evidence, "reviews": history, "findings": items, "rounds": rounds}
                raise InterruptedAttempt(cause, report) from cause
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
            candidate = git.candidate(base, head)
            evidence = {
                "repo_id": self.cfg["repo_id"], "target": self.cfg["target"], "base": base,
                "head": head, "branch": branch, "merge_base": merge_base, "candidate": candidate,
                "policy": self.cfg["policy_id"], "run": uuid4().hex,
            }
            history = []
            approvals = self.cfg["evidence"]
            observer = approvals["observer"]
            scopes = {adapter["bundle"] for adapter in (observer, *approvals["checks"].values())}
            seen = {(item["lens"], item["id"]): item for item in prior}
            for rounds in range(1, self.cfg["max_rounds"] + 1):
                packages = {scope: candidate_bundle(git, root, evidence, scope) for scope in sorted(scopes)}
                evidence["diff_sha256"] = git.diff_digest(base, evidence["candidate"])
                paths = sorted(git.paths(base, evidence["candidate"]))
                items = self.review(job["id"], git.repo, packages[observer["bundle"]],
                                    evidence, paths, seen.values(), history, rounds)
                open_protected = [item for item in items if item["risk"] in BLOCKING]
                seen.update(((item["lens"], item["id"]), item) for item in open_protected)
                report = {**evidence, "reviews": history, "rounds": rounds, "findings": items}
                if not items:
                    break
                if rounds == self.cfg["max_rounds"] or not (self.cfg["worker"] and self.cfg["repair_paths"]):
                    self.authority(evidence)
                    if open_protected:
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
                adapter = approvals["checks"][name]
                staged, scope = packages[adapter["bundle"]]
                request = {**evidence, **scope, "retention_seconds": adapter["retention_seconds"],
                           "kind": "required-check", "check": name, "invocation": uuid4().hex}
                try:
                    output = self.command("verify", argv, None, evidence, bundle=staged,
                                          stdin=json.dumps({"request": request}, ensure_ascii=True))
                    key = self.cfg["check_keys"][name]
                    receipts.append({
                        **verified_check(self.authenticate(output, key), request),
                        "collected_at": now(), "source": source_id(key),
                    })
                except (OSError, ValueError, Retry) as cause:
                    raise InterruptedAttempt(cause, {**report, "checks": receipts}) from cause
            if not receipts:
                raise Conflict("configure mandatory checks before preparing candidates")
            self.authority(evidence)
            full = packages.get("full") or candidate_bundle(git, root, evidence, "full")
            staged, export_scope = full
            directory = private_dir(self.cfg["state"] / "candidates")
            bundle = directory / f"{evidence['run']}.bundle"
            with staged.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != export_scope["bundle_sha256"]:
                    raise Conflict("candidate bundle changed during evidence collection")
            self.authority(evidence)
            staged.replace(bundle)
            return "candidate", "awaiting repository owner", {
                **report, **export_scope, "checks": receipts,
                "expires_at": time.time() + self.cfg["evidence_ttl_seconds"],
            }

    def collect(self, job, future):
        try:
            phase, detail, evidence = future.result()
        except Cancelled:
            self.store.cancel_review(job["id"])
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
