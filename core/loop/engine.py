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

from ..verdict import Scanner
from .config import config, repo_id
from .design import DESIGN_LENSES, approval_matches, document, validate_document, wiki_landed
from .evidence import canonical, signed_receipt, source_id
from .git import Conflict, Git, MergeFailure
from .review import BLOCKING, brief, lenses, observed_review, prompt, review_receipt, verified_check
from .runtime import Cancelled, CommandFailed, MemoryGate, Retry, private_dir
from .sandbox import execute
from .store import SHA, now


class SourceChanged(Conflict):
    pass


class InterruptedAttempt(Exception):
    def __init__(self, cause, evidence):
        self.cause = cause
        self.evidence = evidence


class UnsettledReview(InterruptedAttempt):
    """A review attempt stays open: its output or its recording cannot be proven whole."""


def unread_findings(invocation, texts):
    return [{
        "id": f"unread-{hashlib.sha256((invocation + text).encode('utf-8', 'replace')).hexdigest()[:16]}",
        "risk": "honesty", "finding_class": "unread-review-output",
        "detail": "The coordinator could not read this finding from an earlier review of this change. "
                  "Review the affected code again before resolving it. Printed text: " + text[:1000],
    } for text in texts]


def rescan_open_attempts(store):
    """Journal what the stored output of every open attempt holds.

    A coordinator that was killed never ran `conclude`, so a finding printed up to
    the last poll sits only in the stored output. Reading it again, whole, journals
    each complete finding and turns each cut-off one into an obligation. Findings
    already journaled are kept as they are.
    """
    for invocation, text in store.open_outputs():
        scanner = Scanner()
        found = scanner.feed(text) + scanner.finish()
        store.observe(invocation, "", found + unread_findings(invocation, scanner.unread))


class ReviewOutput:
    """Scan reviewer output as it arrives and hand each protected finding to `record` at once.

    `record(text, findings)` commits the new output and the findings it completed.
    `conclude` reads the final output once more, so a runner that reports only at the
    end reaches the same record, and decides whether the attempt may close.
    """

    def __init__(self, record, invocation="", reason=None):
        self.record = record
        self.invocation = invocation
        self.scanner = Scanner()
        self.findings = []
        self.reason = reason
        self._text, self._found = "", []

    def __call__(self, text):
        self._commit(text, self.scanner.feed(text))

    def _commit(self, text, found):
        # Keep what a failed write could not store, so the next write carries it.
        self._text += text
        self._found += found
        self.findings += found
        self.record(self._text, self._found)
        self._text, self._found = "", []

    def unread(self):
        """Stand-in protected findings for objects the reviewer printed but the journal cannot hold.

        The next review must resolve each one explicitly, so an operator resume
        cannot drop a finding that was cut off or malformed.
        """
        return unread_findings(self.invocation, self.scanner.unread)

    def conclude(self, output, error=None):
        seen = self.scanner.text
        rest = seen if output is None else output
        if not rest.startswith(seen):
            self.reason = "output changed between reads"
            return
        found = self.scanner.feed(rest[len(seen):])
        found += self.scanner.finish()
        found += self.unread()
        if getattr(error, "output_truncated", False):
            self.reason = "output truncated"
        elif isinstance(error, sqlite3.Error):
            self.reason = "review could not be recorded"
        elif self.scanner.unread:
            self.reason = "a finding in the output could not be read"
        elif not self.scanner.settled:
            self.reason = "output is not one complete JSON document"
        self._commit(rest[len(seen):], found)

    @property
    def settled(self):
        return self.reason is None

    @property
    def digest(self):
        return hashlib.sha256(self.scanner.text.encode("utf-8", errors="replace")).hexdigest()


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


def check_candidate(cfg, evidence, store=None, *, export=False):
    check_inputs(cfg, evidence)
    if store is not None:
        check_lineage(store, evidence, export=export)
    if cfg.get("profile") and config(cfg["profile"])["policy_id"] != evidence["policy"]:
        raise Conflict("execution policy changed; restart and retry")
    if evidence["expires_at"] <= time.time():
        raise Conflict("candidate evidence expired; retry the job")


def check_lineage(store, evidence, *, export=False):
    """Read the inherited findings again: a candidate stands only while its lineage has settled."""
    job = evidence.get("job")
    if job is None:
        raise Conflict("candidate evidence has no lineage record; retry the job")
    parent = store.unsettled_parent(job)
    if parent is not None:
        raise Conflict(f"job {parent} that this job inherits from is still in review; retry after it settles")
    reviewed = evidence.get("lineage_reviewed", [])
    if any(not isinstance(entry, list) or len(entry) != 4 for entry in reviewed):
        raise Conflict("candidate lineage record predates finding identity; retry the job")
    gap = store.lineage_gap(job, reviewed)
    if gap:
        raise Conflict("lineage findings this review never saw were recorded since: "
                       + ", ".join(f"{lens}:{item}" for _, lens, _, item in gap) + "; retry the job")
    if export:
        # An open review anywhere on the lineage (any job on this branch or on a parent's
        # branch) may still record findings this candidate has not seen, so export waits.
        open_review = store.open_lineage_review(job)
        if open_review is not None:
            raise Conflict(f"job {open_review} on the same lineage is still in review; export after it settles")


def export_candidate(cfg, evidence, destination, store=None):
    check_candidate(cfg, evidence, store, export=True)
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
            check_candidate(cfg, evidence, store, export=True)
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


def retain_design_findings(previous, findings):
    retained = {item["id"]: item for item in previous}
    for item in findings:
        if item["risk"] in BLOCKING:
            retained.setdefault(item["id"], item)
    return list(retained.values())


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
        rescan_open_attempts(self.store)
        self.store.interrupt_unsettled()
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

    def command(self, role, argv, work, evidence, *, stdin, bundle=None, watch=None):
        self.authority(evidence)
        limit = self.cfg["memory"]["test" if role in {"check", "verify"} else "review"]
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
                                 policy=self.cfg["execution"], stdin=stdin, extra=extra, bundle=bundle,
                                 **({"watch": watch} if watch else {}))
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

    def review_result(self, argv, work, evidence, request, previous=(), prior_round=(), *, record):
        """Run one reviewer and return its validated receipt.

        Every protected finding reaches `record` as soon as the output completes it.
        A failure carries `observed`, the scanned output that decides whether the
        attempt settled, and `salvaged`, the protected findings recovered from it.
        """
        model_verdict = self.cfg.get("review_format") == "verdict"
        observed = ReviewOutput(record, request["invocation"])

        def conclude(output, error=None):
            try:
                observed.conclude(output, error)
            except (OSError, sqlite3.Error):
                observed.reason = "review could not be recorded"

        try:
            output = self.command("reviewer", argv, work, evidence, watch=observed,
                                  stdin=prompt(request, previous, prior_round, model_verdict=model_verdict,
                                               rule_text=self.cfg.get("rules_text")))
        except Exception as error:
            conclude(getattr(error, "output", None), error)
            error.observed, error.salvaged = observed, observed.findings
            raise
        conclude(output)
        try:
            if model_verdict:
                from ..agents import review_verdict

                tool = self.cfg["execution"]["tools"][argv[0].removeprefix("/tools/")]
                output = json.dumps(review_verdict(output, request, f"{tool.path.name}@{tool.digest}"))
            review = review_receipt(output, request, previous)
        except ValueError as error:
            error.observed, error.salvaged = observed, observed.findings
            raise
        if not observed.settled:
            error = ValueError(observed.reason)
            error.observed, error.salvaged = observed, observed.findings
            raise error
        return review, observed

    def design_review(self, branch, head, path):
        with self.store.design_lock(branch):
            return self._design_review(branch, head, path)

    def _design_review(self, branch, head, path):
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
        evidence = {
            "repo_id": self.cfg["repo_id"], "target": self.cfg["target"], "base": base, "head": head,
            "branch": branch, "candidate": head, "policy": self.cfg["policy_id"], "run": uuid4().hex,
            "design_blob": blob,
        }
        protected = self.store.design_protected(branch)
        # Output of earlier unsettled design reviews stays with the branch (the last three).
        held = (self.store.design(branch) or {}).get("unsettled", [])
        report = {**evidence, "path": path, "blob": blob, "forest_job": forest["id"] if forest else None,
                  "reviews": [], "open_protected": protected, "status": "incomplete", "unsettled": held}
        self.store.save_design(report)
        adapters = self.cfg["lens_reviewers"]
        if not all(adapters.get(lens) for lens in DESIGN_LENSES):
            raise ValueError("configure distinct architect and sre review adapters in the trusted profile")
        with self.git.snapshot(self.cfg["state"], base, head) as (git, _):
            git.run("checkout", "--quiet", "--detach", head)
            for lens in DESIGN_LENSES:
                request = {**evidence, "kind": "design-review", "paths": [path], "lens": lens,
                           "invocation": uuid4().hex}
                previous = protected.get(lens, [])

                def record(_, found, lens=lens, previous=previous, request=request):
                    # The branch's design report is the journal of a design review.
                    if found:
                        report["rejected_request"] = request
                        protected[lens] = retain_design_findings(protected.get(lens, previous), found)
                        self.store.save_design(report)

                try:
                    review, _ = self.review_result(adapters[lens], git.repo, evidence, request, previous,
                                                   record=record)
                except Exception as error:
                    observed = getattr(error, "observed", None)
                    if observed is not None and not observed.settled:
                        report["unsettled"] = [*held[-2:], {"request": request, "reason": observed.reason,
                                                            "output": observed.scanner.text}]
                        self.store.save_design(report)
                    raise
                report.pop("rejected_request", None)
                report["reviews"].append(review)
                protected[lens] = retain_design_findings(previous, review["findings"])
                self.store.save_design(report)
        self.authority(evidence)
        # Commit closures only after both receipts and the branch authority check.
        report["open_protected"] = {review["request"]["lens"]: [item for item in review["findings"]
                                                                if item["risk"] in BLOCKING]
                                    for review in report["reviews"]}
        report["reviewed_at"] = now()
        report["status"] = "changes-requested" if any(review["findings"] for review in report["reviews"]) else "approved"
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
                review = self.attempt(job_id, argv, work, evidence, request, previous, prior_round, rounds,
                                      history, items)
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
                raise InterruptedAttempt(
                    cause, self.failure_report(evidence, history, items, lens, found, rounds)) from cause
            review["collected_at"] = now()
            history.append(review)
            items = [item for item in items if item["lens"] != lens]
            items.extend({**item, "lens": lens} for item in review["findings"])
        return items

    def failure_report(self, evidence, history, items, lens, found, rounds):
        known = {item["id"] for item in items if item["lens"] == lens}
        items = [*items, *({**item, "lens": lens} for item in found
                           if item["risk"] in BLOCKING and item["id"] not in known)]
        return {**evidence, "reviews": history, "findings": items, "rounds": rounds,
                "runs": self.trace(evidence)}

    def attempt(self, job_id, argv, work, evidence, request, previous, prior_round, rounds, history, items):
        """Run one journaled reviewer attempt and return its receipt.

        The attempt closes only when its whole output was read, ended outside every JSON
        structure and was recorded. Otherwise it stays open with its output, and the job
        is interrupted. A settled stop leaves the job queued for the next start.
        """
        invocation = request["invocation"]
        self.store.open_attempt(job_id, request, rounds)
        try:
            review, observed = self.review_result(
                argv, work, evidence, request, previous, prior_round,
                record=lambda text, found: self.store.observe(invocation, text, found))
            outcome, cause = "complete", None
        except Exception as error:
            observed = getattr(error, "observed", None) or ReviewOutput(None, reason="review failed unexpectedly")
            cause = error
            outcome = "stopped" if isinstance(error, Cancelled) else "rejected"
        try:
            self.store.settle(invocation, outcome if observed.settled else None, observed.digest, observed.reason)
        except (OSError, sqlite3.Error) as error:
            observed.reason, cause = "review could not be recorded", cause or error
        if not observed.settled:
            report = self.failure_report(evidence, history, items, request["lens"], observed.findings, rounds)
            raise UnsettledReview(cause or ValueError(observed.reason),
                                  {**report, "unsettled": observed.reason}) from cause
        if cause is not None:
            raise cause
        return review

    def step(self, job, prior, forest, approval, lineage=None):
        if lineage is None:
            lineage = self.store.inherited_lineage(job)[1]
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
                    git.pristine(evidence["candidate"])
                    self.command("worker", self.cfg["worker"], git.repo, evidence,
                                 stdin=brief(evidence, items, self.cfg["repair_paths"], self.cfg.get("rules_text")))
                    repaired = git.repair(self.cfg["repair_paths"], evidence["candidate"])
                    with ExitStack() as next_checkout:
                        git, _ = next_checkout.enter_context(git.snapshot(self.cfg["state"], base, repaired))
                        git.run("checkout", "--quiet", "--detach", repaired)
                        repairs.close()
                        repairs.enter_context(next_checkout.pop_all())
                    evidence["candidate"] = repaired
                except (OSError, ValueError, Retry) as cause:
                    raise InterruptedAttempt(cause, report) from cause

            receipts = []
            tree = git.tree(evidence["candidate"])
            for name, argv in sorted(self.cfg["checks"].items()):
                command = argv if local else self.cfg["check_commands"][name]
                request = {**evidence, "tree": tree, "kind": "required-check", "check": name,
                           "command": command, "invocation": uuid4().hex}
                try:
                    direct = self.direct_check(git, request)
                except (OSError, ValueError, Retry) as cause:
                    failed = {"request": request, "tree": tree, "check": name, "status": "failed",
                              "source": "local-unsigned" if local else "coordinator",
                              "exit_code": getattr(cause, "code", None)}
                    raise InterruptedAttempt(cause, {**report, "checks": [*receipts, failed],
                                                     "runs": self.trace(evidence)}) from cause
                if local:
                    receipts.append({**direct, "source": "local-unsigned"})
                    continue
                adapter = approvals["checks"][name]
                staged, scope = packages[adapter["bundle"]]
                direct_run = {key: direct[key] for key in ("command", "execution_id", "exit_code", "output_sha256")}
                request = {**evidence, **scope, "tree": direct["tree"], "direct_run": direct_run,
                           "retention_seconds": adapter["retention_seconds"],
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
                    raise InterruptedAttempt(cause, {**report, "checks": receipts,
                                                     "runs": self.trace(evidence)}) from cause
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
                "job": job["id"], "lineage_reviewed": [list(entry) for entry in lineage],
                "trust": self.cfg.get("trust", "signed"),
                "expires_at": time.time() + self.cfg["evidence_ttl_seconds"],
            }

    def direct_check(self, git, request):
        """Dispatch the command on its pinned input using the read-only check role."""
        started = time.monotonic()
        with git.check_tree(request["tree"]) as work:
            output = self.command("check", request["command"], work, request, stdin=json.dumps({"request": request}))
        return {
            "request": request, "tree": request["tree"], "check": request["check"], "command": request["command"],
            "status": "passed", "executed": True, "exit_code": 0,
            "duration_seconds": round(time.monotonic() - started, 3),
            "output_sha256": hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
            "tool_version": "local", "execution_id": request["invocation"], "collected_at": now(),
        }

    def collect(self, job, future):
        try:
            outcome = future.result()
        except Cancelled:
            return  # Every attempt of this job settled; it runs again after the next start.
        except UnsettledReview as error:
            self.store.update(job, "interrupted", detail=f"review attempt held: {error.evidence['unsettled']}",
                              evidence=error.evidence)
            return
        except InterruptedAttempt as error:
            self.record_failure(job, error.cause, error.evidence)
            return
        except Exception as error:
            self.record_failure(job, error, None)
            return
        if outcome is not None:
            phase, detail, evidence = outcome
            if phase == "candidate":
                try:
                    check_lineage(self.store, evidence)
                except Conflict as error:
                    # The lineage moved while this review ran; review again against its current journal.
                    self.store.update(job, "review", detail=f"lineage changed: {error}")
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
            prior, lineage = self.store.inherited_lineage(job)
            self.active[job["id"]] = job, self.executor.submit(self.step, job, prior, forest, approval, lineage)
            active_branches.add(job["branch"])
