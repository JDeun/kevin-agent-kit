"""Adapters that run a real coding-agent CLI as the loop's reviewer or worker."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

CLIS = ("codex", "claude")
READ_TOOLS = "Read Grep Glob Bash(git diff:*) Bash(git log:*) Bash(git show:*) Bash(git status:*)"
WRITE_TOOLS = "Edit Write " + READ_TOOLS
FINDING_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")
FINDING_CLASS = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
RISKS = {"safety", "honesty", "security", "other"}
RATE_LIMIT = re.compile(r"rate.?limit|usage limit|quota|429|overloaded|temporarily unavailable", re.I)

EXIT_RETRY = 75
EXIT_NO_RESULT = 3


class AgentError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def command(cli, mode, cwd, model=None, unsandboxed=False):
    """Build the argv for one non-interactive CLI call. The prompt arrives on stdin."""
    if cli not in CLIS:
        raise AgentError(f"unsupported agent CLI: {cli}", 2)
    binary = shutil.which(cli)
    if binary is None:
        raise AgentError(f"{cli} was not found on PATH; install it or run `doctor`", 127)
    named = [] if model in (None, "", "default") else ["-m" if cli == "codex" else "--model", model]
    if cli == "codex":
        # Codex's own sandbox needs user namespaces. Where the operator chose the unisolated mode
        # because those are missing, it cannot start, so the outer boundary is the only one.
        sandbox = ("danger-full-access" if unsandboxed else "read-only" if mode == "read" else "workspace-write")
        return [binary, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
                "--sandbox", sandbox, "-C", str(cwd), *named, "-"]
    permission = "default" if mode == "read" else "acceptEdits"
    tools = READ_TOOLS if mode == "read" else WRITE_TOOLS
    return [binary, "-p", "--permission-mode", permission, "--allowedTools", tools, *named]


def version(cli):
    binary = shutil.which(cli)
    if binary is None:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout or out.stderr).strip().splitlines()[0][:80] if (out.stdout or out.stderr).strip() else None


def invoke(cli, mode, prompt, cwd, model=None, timeout=None, unsandboxed=False):
    argv = command(cli, mode, cwd, model, unsandboxed)
    try:
        result = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AgentError(f"{cli} timed out", EXIT_RETRY) from None
    except OSError as error:
        raise AgentError(f"{cli} could not start: {error}", 127) from None
    if result.returncode:
        detail = (result.stderr or result.stdout)[-400:]
        raise AgentError(f"{cli} exited with status {result.returncode}: {detail.strip()}",
                         EXIT_RETRY if RATE_LIMIT.search(detail) else result.returncode)
    return result.stdout


def last_object(text, key):
    """Return the last JSON object in free-form model text that contains `key`."""
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and key in value:
            found = value
    return found


def clean_findings(raw):
    items, seen = [], set()
    for index, item in enumerate(raw if isinstance(raw, list) else [], 1):
        if not isinstance(item, dict) or not str(item.get("detail", "")).strip():
            continue
        name = str(item.get("id", ""))
        if not FINDING_ID.fullmatch(name) or name in seen:
            name = f"finding-{index}"
        seen.add(name)
        risk = item.get("risk") if item.get("risk") in RISKS else "other"
        category = item.get("finding_class")
        if not isinstance(category, str) or not FINDING_CLASS.fullmatch(category) or len(category) > 80:
            category = "uncategorized"
        items.append({"id": name, "risk": risk, "detail": str(item["detail"]), "finding_class": category})
    return items


def verdict_problems(verdict):
    """Everything wrong with a model verdict. A response is complete only when this is empty."""
    problems = []
    if verdict.get("status") != "complete":
        problems.append(f"status is {verdict.get('status')!r}, not 'complete'")
    items = verdict.get("findings")
    if not isinstance(items, list):
        problems.append("findings is not an array")
        items = []
    seen = set()
    for index, item in enumerate(items, 1):
        label = f"finding {index}"
        if not isinstance(item, dict):
            problems.append(f"{label} is not an object")
            continue
        name = item.get("id")
        if not isinstance(name, str) or not FINDING_ID.fullmatch(name) or name in seen:
            problems.append(f"{label} has a missing, invalid or duplicate id")
        else:
            seen.add(name)
        if item.get("risk") not in RISKS:
            problems.append(f"{label} has an invalid risk")
        if not isinstance(item.get("detail"), str) or not item["detail"].strip():
            problems.append(f"{label} has no detail")
        category = item.get("finding_class")
        if category is not None and (not isinstance(category, str) or len(category) > 80
                                     or not FINDING_CLASS.fullmatch(category)):
            problems.append(f"{label} has an invalid finding_class")
    resolved = verdict.get("resolved")
    if not isinstance(resolved, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in resolved.items()):
        problems.append("resolved is not an object of non-empty evidence strings")
    return problems


def protected(items):
    """Well-formed protected findings, kept even when the rest of a response is rejected."""
    if not isinstance(items, list):
        return []
    return clean_findings([
        item for item in items if isinstance(item, dict) and item.get("risk") in RISKS - {"other"}
        and isinstance(item.get("id"), str) and FINDING_ID.fullmatch(item["id"])
    ])


def review(cli, prompt, cwd, model=None, timeout=None, unsandboxed=False):
    """Return the receipt dict the loop expects, built from the model's JSON verdict.

    A verdict that is not an explicit, well-formed complete review is returned as a `partial`
    receipt carrying its protected findings, so the loop rejects it but keeps them.
    """
    request = json.loads(prompt.splitlines()[-1])["request"]
    text = invoke(cli, "read", prompt, cwd, model, timeout, unsandboxed)
    verdict = last_object(text, "findings")
    if verdict is None:
        raise AgentError(f"{cli} returned no JSON verdict with a findings array", EXIT_NO_RESULT)
    problems = verdict_problems(verdict)
    tool = f"{cli} {version(cli) or 'unknown'}"
    if problems:
        return {"request": request, "status": "partial", "tool_version": tool,
                "reviewed_paths": request["paths"], "diagnostics": problems[:20],
                "findings": protected(verdict.get("findings")), "resolved": {}}
    return {
        "request": request, "status": "complete", "tool_version": tool,
        "reviewed_paths": request["paths"], "diagnostics": [],
        "findings": clean_findings(verdict["findings"]),
        "resolved": dict(verdict["resolved"]),
    }


def work(cli, prompt, cwd, model=None, timeout=None, unsandboxed=False):
    invoke(cli, "write", prompt, cwd, model, timeout, unsandboxed)


def workspace():
    path = os.environ.get("AGENT_WORKSPACE")
    if not path or not Path(path).is_dir():
        raise AgentError("AGENT_WORKSPACE is not set to a checkout directory", 2)
    return Path(path)
