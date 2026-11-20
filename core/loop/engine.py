from collections import deque
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
from .design import DESIGN_LENSES, approval_matches, document, validate_document, wiki_landed
from .evidence import canonical, signed_receipt, source_id
from .git import Conflict, Git, MergeFailure
from .review import BLOCKING, brief, lenses, observed_review, prompt, review_receipt, verified_check
from .runtime import Cancelled, CommandFailed, MemoryGate, Retry, private_dir
from .sandbox import execute, redact
from .store import SHA, now


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
        self.runs = {}

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
        started = time.monotonic()
        try:
            output = self.runner(argv, work, self.cancel, self.cfg["timeout_seconds"], role=role,
                                 policy=self.cfg["execution"], stdin=stdin, extra=extra, bundle=bundle)
        except Exception as error:
            code = error.code if isinstance(error, CommandFailed) else None
            self.note(evidence, role, argv, code, type(error).__name__, started)
            raise
        self.note(evidence, role, argv, 0, None, started, output)
        return output

    def note(self, evidence, role, argv, code, error, started, output=""):
        """Keep the exit code and size of every command as evidence of what actually ran."""
        runs = self.runs.setdefault(evidence["run"], [])
        if len(runs) < 200:
            runs.append({
                "role": role, "command": Path(argv[0]).name if argv else "", "exit_code": code,
                "error": error, "seconds": round(time.monotonic() - started, 3),
                "output_sha256": hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
            })

    def trace(self, evidence):
        return list(self.runs.get(evidence["run"], []))

    def review_result(self, argv, work, evidence, request, previous=(), prior_round=()):
        output = self.command("reviewer", argv, work, evidence, stdin=prompt(request, previous, prior_round))
        review = review_receipt(output, request, previous)
        env_names = self.cfg["execution"].get("env", {}).get("reviewer", [])
        review["tool_version"] = redact(review["tool_version"], env_names)
        review["resolved"] = {key: redact(value, env_names) for key, value in review["resolved"].items()}
        for item in review["findings"]:
            item["detail"] = redact(item["detail"], env_names)
            if "finding_class" in item and redact(item["finding_class"], env_names) != item["finding_class"]:
                raise ValueError("finding_class contains a protected value")
        return review

    def design_review(self, branch, head, path):
        base = self.git.head(self.cfg["target"])
        if not base or branch == self.cfg["target"] or not SHA.fullmatch(head) or self.git.head(branch) != head:
            raise ValueError("design-review requires the exact current SHA of a distinct source branch")
        forest = self.store.latest_forest(branch)
        if not self.git.ancestor(base, head) or self.git.paths(base, head) != [path]:
            raise ValueError("the branch must start at the target and change only the design document")
        # A reverted implementation still leaves implementation commits in this range.
        history = self.git.run("log", "--format=", "--name-only", "-m", "-z", f"{base}..{head}").stdout
        if set(history.strip("\0").split("\0")) != {path}:
            raise ValueError("design history may change only the design document")
        if forest and self.git.ancestor(head, forest["sha"]):
            raise ValueError("a branch hold requires a new design commit")
        blob = document(self.git, head, path)
        validate_document(self.git, blob)
        adapters = self.cfg["lens_reviewers"]
        if not all(adapters.get(lens) for lens in DESIGN_LENSES):
            raise ValueError("configure distinct architect and sre review adapters in the trusted profile")
        evidence = {
            "repo_id": self.cfg["repo_id"], "target": self.cfg["target"], "base": base, "head": head,
            "branch": branch, "candidate": head, "policy": self.cfg["policy_id"], "run": uuid4().hex,
            "design_blob": blob,
        }
        reviews = []
        with self.git.snapshot(self.cfg["state"], base, head) as (git, _):
            git.run("checkout", "--quiet", "--detach", head)
            for lens in DESIGN_LENSES:
                request = {**evidence, "kind": "design-review", "paths": [path], "lens": lens,
                           "invocation": uuid4().hex}
                reviews.append(self.review_result(adapters[lens], git.repo, evidence, request))
        self.authority(evidence)
        report = {
            **evidence, "path": path, "blob": blob, "reviews": reviews, "reviewed_at": now(),
            "forest_job": forest["id"] if forest else None,
            "status": "changes-requested" if any(review["findings"] for review in reviews) else "approved",
        }
        self.store.save_design(report)
        return report

    def review(self, job_id, work, package, evidence, paths, prior, history, rounds, prior_round):
        bundle, scope = package
        observer = self.cfg["evidence"].get("observer")
        items = list(prior)
        selected = dict.fromkeys((*lenses(paths, self.cfg["lenses"]), *(item["lens"] for item in prior)))
        for lens in selected:
            previous = [item for item in prior if item["lens"] == lens]
            request = {**evidence, "kind": "review", "lens": lens, "paths": paths, "invocation": uuid4().hex,
                       "finding_class_required": True}
            review = None
            try:
                argv = self.cfg["reviewer"] if lens == "general" else self.cfg["lens_reviewers"].get(lens)
                if not argv:
                    raise ValueError(f"configure a dedicated review adapter for the {lens} lens")
                self.store.begin_review(job_id, request)
                review = self.review_result(argv, work, evidence, request, previous, prior_round)
                self.store.record_protected(job_id, request, rounds,
                                            [item for item in review["findings"] if item["risk"] in BLOCKING])
                if self.cfg.get("trust", "signed") == "local":
                    # No independent observer: record the reviewer's own result as unsigned.
                    review["observation"] = {"source": "local-unsigned", "status": "complete",
                                             "reviewed_paths": paths, "resolved": review["resolved"]}
                    review["collected_at"] = now()
                    history.append(review)
                    items = [item for item in items if item["lens"] != lens]
                    items.extend({**item, "lens": lens} for item in review["findings"])
                    continue
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
                found = review["findings"] if review is not None else getattr(cause, "salvaged", [])
                if found:
                    known = {item["id"] for item in items if item["lens"] == lens}
                    items.extend({**item, "lens": lens} for item in found
                                 if item["risk"] in BLOCKING and item["id"] not in known)
                report = {**evidence, "reviews": history, "findings": items, "rounds": rounds,
                          "runs": self.trace(evidence)}
                raise InterruptedAttempt(cause, report) from cause
            review["collected_at"] = now()
            history.append(review)
            items = [item for item in items if item["lens"] != lens]
            items.extend({**item, "lens": lens} for item in review["findings"])
        return items

    def step(self, job, prior, forest, approval):
        base = self.git.head(self.cfg["target"])
        branch, head = job["branch"], job["sha"]
        if branch == self.cfg["target"] or not base:
            raise Conflict("source and existing target must be distinct branches")
        if self.git.head(branch) != head:
            return "stale", "enqueued source changed; explicitly enqueue the new SHA", None
        if self.git.ancestor(head, base):
            return "included", "source is already included in target", None
        phase, approval = self.design_gate(job, base, forest, approval)
        if phase:
            return phase, "review a design-only revision before implementation review", None
        merge_base = self.git.run("merge-base", base, head, allowed=(0, 1)).stdout.strip()
        if not merge_base:
            raise MergeFailure("unrelated-histories")
        if (self.cfg["wiki_landing"] and job["kind"] == "research"
                and not wiki_landed(self.git.paths(merge_base, head), self.cfg["wiki_dir"])):
            return "wiki-needed", "land the research in a wiki synthesis page before review", None
        with self.git.snapshot(self.cfg["state"], base, head) as (git, root), ExitStack() as repairs:
            candidate = git.candidate(base, head)
            evidence = {
                "repo_id": self.cfg["repo_id"], "target": self.cfg["target"], "base": base,
                "head": head, "branch": branch, "merge_base": merge_base, "candidate": candidate,
                "policy": self.cfg["policy_id"], "run": uuid4().hex,
            }
            history = []
            prior_round = []
            classes = deque(maxlen=self.cfg["repeat_class_rounds"])
            approvals = self.cfg["evidence"]
            local = self.cfg.get("trust", "signed") == "local"
            observer = approvals.get("observer")
            scopes = set() if local else {
                adapter["bundle"] for adapter in (observer, *approvals["checks"].values())}
            seen = {(item["lens"], item["id"]): item for item in prior}
            for rounds in range(1, self.cfg["max_rounds"] + 1):
                if approval and not approval_matches(git, approval, base, evidence["candidate"], self.cfg["policy_id"]):
                    return "design-needed", "candidate changed the approved design", None
                packages = {scope: candidate_bundle(git, root, evidence, scope) for scope in sorted(scopes)}
                evidence["diff_sha256"] = git.diff_digest(base, evidence["candidate"])
                paths = sorted(git.paths(base, evidence["candidate"]))
                items = self.review(job["id"], git.repo, (None, None) if local else packages[observer["bundle"]],
                                    evidence, paths, seen.values(), history, rounds, prior_round)
                open_protected = [item for item in items if item["risk"] in BLOCKING]
                seen.update(((item["lens"], item["id"]), item) for item in open_protected)
                report = {**evidence, "reviews": history, "rounds": rounds, "findings": items,
                          "runs": self.trace(evidence)}
                if approval:
                    report["design"] = {key: approval[key] for key in ("head", "path", "blob", "reviewed_at")}
                prior_round = items
                classes.append({(item["lens"], item["finding_class"]) for item in items})
                if len(classes) == classes.maxlen and (repeated := set.intersection(*classes)):
                    self.authority(evidence)
                    self.store.record_round(
                        job, evidence["run"], rounds,
                        escalation={**report, "repeated_classes": sorted({name for _, name in repeated}),
                                    "repeated_by_lens": sorted(f"{lens}:{name}" for lens, name in repeated)})
                    return None  # The worker already recorded the hold and its evidence atomically.
                self.store.record_round(job, evidence["run"], rounds)
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
                if local:
                    try:
                        receipts.append(self.local_check(name, argv, git.repo, evidence))
                    except (OSError, ValueError, Retry) as cause:
                        failed = {"check": name, "status": "failed", "source": "local-unsigned",
                                  "exit_code": getattr(cause, "code", None)}
                        raise InterruptedAttempt(
                            cause, {**report, "checks": [*receipts, failed], "runs": self.trace(evidence)}) from cause
                    continue
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
                **report, **export_scope, "checks": receipts, "runs": self.trace(evidence),
                "trust": self.cfg.get("trust", "signed"),
                "expires_at": time.time() + self.cfg["evidence_ttl_seconds"],
            }

    def local_check(self, name, argv, work, evidence):
        """Run a required check directly in the candidate checkout and keep its exit code."""
        started = time.monotonic()
        request = {**evidence, "kind": "required-check", "check": name, "invocation": uuid4().hex}
        output = self.command("verify", argv, work, evidence, stdin=json.dumps({"request": request}))
        return {
            "request": request, "check": name, "status": "passed", "executed": True, "exit_code": 0,
            "duration_seconds": round(time.monotonic() - started, 3),
            "output_sha256": hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
            "tool_version": "local", "execution_id": uuid4().hex, "collected_at": now(),
            "source": "local-unsigned",
        }

    def collect(self, job, future):
        try:
            outcome = future.result()
        except Cancelled:
            self.store.cancel_review(job["id"])
            return
        except InterruptedAttempt as error:
            self.record_failure(job, error.cause, error.evidence)
            return
        except Exception as error:
            self.record_failure(job, error, None)
            return
        if outcome is not None:
            phase, detail, evidence = outcome
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
        if isinstance(error, MergeFailure):
            evidence = {"merge_failure": error.reason}
        self.store.update(job, phase, detail=detail[:500], evidence=evidence)

    def design_gate(self, job, base, forest, approval):
        required = self.cfg["design_gate"] and job["kind"] not in ("routine", "research")
        if not required and not forest:
            return None, None
        approved = approval_matches(self.git, approval, base, job["sha"], self.cfg["policy_id"])
        if forest and (not approved or approval["forest_job"] != forest["id"]):
            return "forest-held", None
        return (None, approval) if approved else ("design-needed", None)

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
            forest = self.store.latest_forest(job["branch"])
            approval = self.store.design(job["branch"])
            prior = self.store.inherited_protected(job)
            self.active[job["id"]] = job, self.executor.submit(self.step, job, prior, forest, approval)
            active_branches.add(job["branch"])
