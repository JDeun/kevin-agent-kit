"""File-based GitHub and GitLab change request operations."""

import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


class AdapterError(Exception):
    """A local validation, provider, or verification failure."""


_FENCE = re.compile(r"(`{3,}|~{3,})(.*)")
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_SHA = re.compile(r"[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}")


def _guard_quick_actions(text: str) -> None:
    # Accept ordinary top-level fences. Once HTML appears, its block rules
    # can change the meaning of later fence markers, so check those lines too.
    fence = ""
    saw_html = False
    for number, line in enumerate(text.split("\n"), start=1):
        marker = _FENCE.fullmatch(line)
        if fence:
            if marker:
                run, suffix = marker.groups()
                if run.startswith(fence) and not suffix.strip(" \t"):
                    fence = ""
            continue

        if line.lstrip().lstrip("\ufeff").lstrip().startswith("/"):
            raise AdapterError(f"possible quick action on body line {number}")
        saw_html = saw_html or "<" in line
        if marker and not saw_html:
            run, suffix = marker.groups()
            if run.startswith("~") or "`" not in suffix:
                fence = run


def _read_body(path: Path, *, gitlab_note: bool = False) -> tuple[bytes, str]:
    try:
        if not path.is_file():
            raise AdapterError(f"body path is not a regular file: {path}")
        original = path.read_bytes()
        body = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AdapterError("body file must contain valid UTF-8") from error
    except OSError as error:
        raise AdapterError(f"could not read body file: {error}") from error

    _guard_quick_actions(body.replace("\r\n", "\n").replace("\r", "\n"))
    if "\r" in body:
        _guard_quick_actions(body.replace("\r", ""))
    if gitlab_note and ("\r" in body or body.rstrip(" \t\n\v\f\0") != body):
        raise AdapterError("GitLab would normalize this comment. Use LF and remove trailing blank characters.")
    return original, body


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise AdapterError(f"expected a positive integer for {name}")
    return value


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise AdapterError(f"expected an object for {name}")
    return value


class HostingAdapter:
    """Use an authenticated provider CLI against one explicit repository."""

    def __init__(self, provider: str, repo: str, host: str | None = None) -> None:
        if provider not in ("github", "gitlab"):
            raise AdapterError("choose github or gitlab")
        parts = repo.split("/")
        github = provider == "github"
        if (
            len(parts) < 2
            or (github and len(parts) != 2)
            or any(not _SEGMENT.fullmatch(part) or part in (".", "..") for part in parts)
        ):
            raise AdapterError("repository must be an owner and repository path")
        self.provider = provider
        self.cli = "gh" if github else "glab"
        self.host = ("github.com" if github else "gitlab.com") if host is None else host
        if not _HOSTNAME.fullmatch(self.host):
            raise AdapterError("host must be a hostname")
        self.api_root = f"repos/{repo}" if github else f"projects/{quote(repo, safe='')}"
        self.changes = f"{self.api_root}/{'pulls' if github else 'merge_requests'}"
        self.body_field = "body" if github else "description"

    def _api(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        options: tuple[str, ...] = (),
    ) -> object:
        args = [self.cli, "api", "--hostname", self.host, "--method", method, path, *options]
        data = b""
        if payload is not None:
            args.extend(("--header", "Content-Type: application/json", "--input", "-"))
            data = json.dumps(payload).encode("utf-8")
        try:
            result = subprocess.run(args, input=data, capture_output=True, timeout=60, check=False)
        except FileNotFoundError as error:
            raise AdapterError(f"{self.cli} executable was not found") from error
        except OSError as error:
            raise AdapterError(f"could not run {self.cli}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise AdapterError(f"{self.cli} timed out on {method} {path}") from error
        if result.returncode:
            raise AdapterError(f"{self.cli} failed on {method} {path} with exit status {result.returncode}")
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise AdapterError(f"{self.cli} returned invalid UTF-8 JSON for {method} {path}") from error

    def _verify(self, path: str, field: str, original: bytes) -> dict:
        try:
            saved = _object(self._api("GET", path), path)
        except AdapterError as error:
            raise AdapterError(f"write sent but read-back failed for {path}: {error}") from error

        value = saved.get(field)
        if field == "body" and field in saved and value is None:
            # GitHub can represent an empty pull request body as null.
            value = ""
        try:
            actual = value.encode("utf-8") if isinstance(value, str) else None
        except UnicodeEncodeError:
            actual = None
        if actual != original:
            raise AdapterError(f"read-back body bytes differ from the file for {path}")
        return saved

    def create(self, *, title: str, head: str, base: str, body_file: Path) -> dict:
        """Create a change request and verify its stored body."""
        original, body = _read_body(body_file)
        github = self.provider == "github"
        branches = {"head": head, "base": base} if github else {"source_branch": head, "target_branch": base}
        payload = {"title": title, self.body_field: body, **branches}
        created = _object(self._api("POST", self.changes, payload), self.changes)
        id_field, url_field = ("number", "html_url") if github else ("iid", "web_url")
        number = _positive_id(created.get(id_field), id_field)
        saved = self._verify(f"{self.changes}/{number}", self.body_field, original)
        return {"number": number, "url": saved.get(url_field)}

    def update_body(self, number: int, *, body_file: Path) -> dict:
        """Replace a change request body and verify the stored bytes."""
        path = f"{self.changes}/{_positive_id(number, 'number')}"
        original, body = _read_body(body_file)
        github = self.provider == "github"
        self._api("PATCH" if github else "PUT", path, {self.body_field: body})
        saved = self._verify(path, self.body_field, original)
        return {"number": number, "url": saved.get("html_url" if github else "web_url")}

    def comment(self, number: int, *, body_file: Path) -> dict:
        """Post a top-level comment and verify it by its returned ID."""
        _positive_id(number, "number")
        github = self.provider == "github"
        original, body = _read_body(body_file, gitlab_note=not github)
        collection = f"{self.api_root}/issues/{number}/comments" if github else f"{self.changes}/{number}/notes"
        created = _object(self._api("POST", collection, {"body": body}), collection)
        comment_id = _positive_id(created.get("id"), "comment ID")
        lookup = f"{self.api_root}/issues/comments" if github else collection
        self._verify(f"{lookup}/{comment_id}", "body", original)
        return {"number": number, "comment_id": comment_id}

    def pipeline_status(self, number: int) -> dict:
        """Read GitHub head checks or the GitLab MR head pipeline."""
        path = f"{self.changes}/{_positive_id(number, 'number')}"
        change = _object(self._api("GET", path), path)
        if self.provider == "gitlab":
            pipeline = change.get("head_pipeline")
            if pipeline is None:
                return {"number": number, "sha": change.get("sha"), "status": "unknown", "pipeline_id": None}
            pipeline = _object(pipeline, "head pipeline")
            return {
                "number": number,
                "sha": pipeline.get("sha"),
                "status": pipeline.get("status") or "unknown",
                "pipeline_id": pipeline.get("id"),
            }

        head = _object(change.get("head"), "pull request head")
        sha = head.get("sha")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise AdapterError("expected a commit SHA for the pull request head")
        return {"number": number, "sha": sha, "status": self._github_status(sha)}

    def _github_status(self, sha: str) -> str:
        commit = f"{self.api_root}/commits/{sha}"
        combined = _object(self._api("GET", f"{commit}/status"), "combined commit status")
        pages = self._api("GET", f"{commit}/check-runs", options=("--paginate", "--slurp"))
        if not isinstance(pages, list):
            raise AdapterError("expected a list of check run pages")

        statuses = set()
        count = combined.get("total_count")
        if type(count) is not int or count < 0:
            raise AdapterError("expected a status count for the pull request head")
        if count:
            state = combined.get("state")
            if state in ("failure", "error"):
                statuses.add("failed")
            elif state in ("pending", "success"):
                statuses.add(state)
            else:
                statuses.add("unknown")

        for page in pages:
            runs = _object(page, "check run page").get("check_runs")
            if not isinstance(runs, list):
                raise AdapterError("expected a list of check runs")
            for value in runs:
                run = _object(value, "check run")
                state, conclusion = run.get("status"), run.get("conclusion")
                if state == "completed" and conclusion in ("success", "neutral", "skipped"):
                    statuses.add("success")
                elif state == "completed" and conclusion in (
                    "failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure",
                ):
                    statuses.add("failed")
                elif state in ("expected", "in_progress", "pending", "queued", "requested", "waiting"):
                    statuses.add("pending")
                else:
                    statuses.add("unknown")

        return next((state for state in ("failed", "pending", "unknown", "success") if state in statuses), "unknown")
