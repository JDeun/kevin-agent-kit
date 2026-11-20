import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Each fake invocation consumes one expected request and returns its scripted reply.
FAKE_CLI = """\
import json
import os
from pathlib import Path
import sys

plan = Path(os.environ["FAKE_API_PLAN"])
requests = json.loads(plan.read_bytes())
assert requests, sys.argv
request = requests.pop(0)
plan.write_text(json.dumps(requests), encoding="utf-8")
args = sys.argv[1:]
assert Path(sys.argv[0]).name == request["cli"], sys.argv
assert args[:6] == [
    "api", "--hostname", request["host"],
    "--method", request["method"], request["path"],
], args
if "payload" in request:
    assert args[6:9] == ["--header", "Content-Type: application/json", "--input"], args
    assert args[9:] == ["-"], args
    assert json.load(sys.stdin) == request["payload"]
else:
    assert args[6:] == request.get("options", []), args
    assert not sys.stdin.buffer.read()
if request.get("exit"):
    sys.exit(request["exit"])
print(json.dumps(request["response"]))
"""


@pytest.fixture
def fake_api(tmp_path):
    for executable in ("gh", "glab"):
        path = tmp_path / executable
        path.write_text(f"#!{sys.executable}\n{FAKE_CLI}", encoding="utf-8")
        path.chmod(0o700)

    plan = tmp_path / "api.json"

    def run(*args, requests=()):
        plan.write_text(json.dumps(requests), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "core.adapters", *map(str, args)],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": str(tmp_path),
                "FAKE_API_PLAN": str(plan),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert json.loads(plan.read_bytes()) == [], result.stderr
        return result

    return run


def request(provider, method, path, response=None, **kwargs):
    cli, host = {
        "github": ("gh", "github.com"),
        "gitlab": ("glab", "gitlab.example.com"),
    }[provider]
    return dict(cli=cli, host=host, method=method, path=path, response=response, **kwargs)


def arguments(provider):
    if provider == "github":
        return ["github", "--repo", "demo/widgets"]
    return ["gitlab", "--host", "gitlab.example.com", "--repo", "demo/team/widgets"]


@pytest.fixture
def body_file(tmp_path):
    def write(data):
        path = tmp_path / "body with spaces.md"
        path.write_bytes(data)
        return path

    return write


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_create_round_trips_literal_bytes(fake_api, body_file, tmp_path, provider):
    marker = tmp_path / "unexpected"
    body = f"naïve and 한글\n\n```sh\n/close\n`touch {marker}`\n```\n\n$(touch {marker})\n"
    if provider == "github":
        path = "repos/demo/widgets/pulls"
        field = "body"
        branches = {"head": "feature/a", "base": "main"}
        created = {"number": 7, "html_url": "https://github.com/demo/widgets/pull/7"}
    else:
        path = "projects/demo%2Fteam%2Fwidgets/merge_requests"
        field = "description"
        branches = {"source_branch": "feature/a", "target_branch": "main"}
        created = {"iid": 7, "web_url": "https://gitlab.example.com/demo/team/widgets/-/merge_requests/7"}

    result = fake_api(
        *arguments(provider),
        "create", "--title", "Example", "--head", "feature/a", "--base", "main",
        "--body-file", body_file(body.encode()),
        requests=[
            request(provider, "POST", path, created, payload={"title": "Example", field: body, **branches}),
            request(provider, "GET", f"{path}/7", {**created, field: body}),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["number"] == 7
    assert not marker.exists()


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_update_uses_independent_readback(fake_api, body_file, provider):
    path, method, field = {
        "github": ("repos/demo/widgets/pulls/7", "PATCH", "body"),
        "gitlab": ("projects/demo%2Fteam%2Fwidgets/merge_requests/7", "PUT", "description"),
    }[provider]
    body = "line 1\r\nline 2  \n"
    result = fake_api(
        *arguments(provider),
        "update-body", "--number", "7", "--body-file", body_file(body.encode()),
        requests=[
            request(provider, method, path, {field: body}, payload={field: body}),
            request(provider, "GET", path, {field: "line 1\nline 2"}),
        ],
    )

    assert result.returncode == 1
    assert "byte" in result.stderr
    assert path in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_comments_are_read_back_by_created_id(fake_api, body_file, provider):
    post_path, get_path = {
        "github": ("repos/demo/widgets/issues/7/comments", "repos/demo/widgets/issues/comments/13"),
        "gitlab": (
            "projects/demo%2Fteam%2Fwidgets/merge_requests/7/notes",
            "projects/demo%2Fteam%2Fwidgets/merge_requests/7/notes/13",
        ),
    }[provider]
    body = "Review note about `/some_command`"
    result = fake_api(
        *arguments(provider),
        "comment", "--number", "7", "--body-file", body_file(body.encode()),
        requests=[
            request(provider, "POST", post_path, {"id": 13}, payload={"body": body}),
            request(provider, "GET", get_path, {"id": 13, "body": body}),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"number": 7, "comment_id": 13}


@pytest.mark.parametrize("data", [
    b"First line\n  /some_new_action option",
    b"\xfe",
    b"Line\n/cl\rose",
    b"```bad`info\n/close\n```",
    b"<!--\n```\n-->\n/close",
    b"\v```\n/close",
])
def test_invalid_body_is_rejected_before_provider_call(fake_api, body_file, data):
    result = fake_api(
        *arguments("gitlab"),
        "comment", "--number", "7", "--body-file", body_file(data),
    )

    assert result.returncode == 1
    assert "UTF-8" in result.stderr or "quick action" in result.stderr


def test_inline_body_flag_is_rejected(fake_api):
    result = fake_api(*arguments("github"), "comment", "--number", "7", "--body", "text")
    assert result.returncode == 2
    assert "--body-file" in result.stderr


def test_reference_cannot_override_the_endpoint(fake_api):
    result = fake_api("github", "--repo", "demo/../other", "pipeline-status", "--number", "7")
    assert result.returncode == 1
    assert "repository" in result.stderr


@pytest.mark.parametrize("body", [b"note\n", b"note  ", b"first\r\nsecond"])
def test_gitlab_comment_rejects_input_that_would_be_normalized(fake_api, body_file, body):
    result = fake_api(
        *arguments("gitlab"),
        "comment", "--number", "7", "--body-file", body_file(body),
    )

    assert result.returncode == 1
    assert "normalize" in result.stderr


@pytest.mark.parametrize("pipeline", [None, {"id": 12, "status": "running", "sha": "b" * 40}])
def test_gitlab_status_reads_merge_request_head_pipeline(fake_api, pipeline):
    result = fake_api(
        *arguments("gitlab"),
        "pipeline-status", "--number", "7",
        requests=[
            request(
                "gitlab", "GET", "projects/demo%2Fteam%2Fwidgets/merge_requests/7",
                {"sha": "c" * 40, "head_pipeline": pipeline},
            ),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "number": 7,
        "sha": "b" * 40 if pipeline else "c" * 40,
        "status": "running" if pipeline else "unknown",
        "pipeline_id": 12 if pipeline else None,
    }


@pytest.mark.parametrize(("combined", "pages", "expected"), [
    (
        {"state": "pending", "total_count": 0},
        [{"check_runs": [{"status": "completed", "conclusion": "success"}]}],
        "success",
    ),
    (
        {"state": "success", "total_count": 1},
        [{"check_runs": [{"status": "in_progress", "conclusion": None}]}],
        "pending",
    ),
    (
        {"state": "pending", "total_count": 1},
        [
            {"check_runs": [{"status": "completed", "conclusion": "success"}]},
            {"check_runs": [{"status": "completed", "conclusion": "failure"}]},
        ],
        "failed",
    ),
    (
        {"state": "failure", "total_count": 1},
        [{"check_runs": []}],
        "failed",
    ),
    (
        {"state": "pending", "total_count": 0},
        [{"check_runs": []}],
        "unknown",
    ),
])
def test_github_status_combines_check_runs_and_commit_status(fake_api, combined, pages, expected):
    sha = "a" * 40
    result = fake_api(
        *arguments("github"),
        "pipeline-status", "--number", "7",
        requests=[
            request("github", "GET", "repos/demo/widgets/pulls/7", {"head": {"sha": sha}}),
            request("github", "GET", f"repos/demo/widgets/commits/{sha}/status", combined),
            request(
                "github", "GET", f"repos/demo/widgets/commits/{sha}/check-runs", pages,
                options=["--paginate", "--slurp"],
            ),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"number": 7, "sha": sha, "status": expected}


def test_readback_failure_identifies_the_created_comment(fake_api, body_file):
    body = "Review note"
    result = fake_api(
        *arguments("github"),
        "comment", "--number", "7", "--body-file", body_file(body.encode()),
        requests=[
            request("github", "POST", "repos/demo/widgets/issues/7/comments", {"id": 13}, payload={"body": body}),
            request("github", "GET", "repos/demo/widgets/issues/comments/13", exit=19),
        ],
    )

    assert result.returncode == 1
    assert "read-back" in result.stderr
    assert "issues/comments/13" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("conclusion, missing, moved", [
    ("success", False, False), ("neutral", False, False), ("skipped", False, False),
    ("success", True, False), ("success", False, True),
])
def test_github_pipeline_gate_requires_fresh_exact_named_checks(fake_api, conclusion, missing, moved):
    head, base = "a" * 40, "b" * 40
    pull_path = "repos/demo/widgets/pulls/7"
    target_path = "repos/demo/widgets/git/ref/heads/main"
    suite_path = "repos/demo/widgets/check-suites/12"
    pull = {"head": {"sha": head}, "base": {"ref": "main", "sha": base, "repo": {"full_name": "demo/widgets"}}}
    app = {"slug": "github-actions"}
    suite = dict(id=12, head_sha=head, status="completed", conclusion="success", app=app)
    check = dict(id=34, name="unit", head_sha=head, status="completed", conclusion=conclusion,
                 app=app, check_suite={"id": 12})
    requests = [
        request("github", "GET", pull_path, pull),
        request("github", "GET", target_path, {"object": {"sha": base}}),
        request("github", "GET", suite_path, suite),
        request("github", "GET", suite_path + "/check-runs?filter=latest", [{"check_runs": [] if missing else [check]}],
                options=["--paginate", "--slurp"]),
    ]
    if conclusion == "success" and not missing:
        updated = {**pull, "head": {"sha": "c" * 40}} if moved else pull
        requests.append(request("github", "GET", pull_path, updated))
        if not moved:
            requests.append(request("github", "GET", target_path, {"object": {"sha": base}}))
    result = fake_api(
        *arguments("github"), "pipeline-gate", "--number", 7, "--sha", head, "--target", "main",
        "--base-sha", base, "--run-id", 12, "--source", "github-actions", "--require", "unit",
        requests=requests,
    )
    if conclusion == "success" and not missing and not moved:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["checks"] == {"unit": 34}
    else:
        assert result.returncode == 1
        assert not result.stdout


@pytest.mark.parametrize("pipeline_sha", ["a" * 40, "c" * 40])
def test_gitlab_pipeline_gate_cross_checks_provider_run_sha(fake_api, pipeline_sha):
    head, base = "a" * 40, "b" * 40
    root = "projects/demo%2Fteam%2Fwidgets"
    change = {
        "sha": head, "target_branch": "main", "target_project_id": 5,
        "head_pipeline": {"id": 12, "sha": pipeline_sha, "project_id": 5},
    }
    before = [
        request("gitlab", "GET", root + "/merge_requests/7", change),
        request("gitlab", "GET", root + "/repository/branches/main", {"commit": {"id": base}}),
    ]
    requests = [
        *before,
        request("gitlab", "GET", root + "/pipelines/12",
                dict(id=12, sha=head, source="merge_request_event", status="success")),
        request("gitlab", "GET", root + "/pipelines/12/jobs?per_page=100&page=1&include_retried=false", [
            dict(id=10, name="unit", status="success", commit={"id": head}, pipeline={"id": 12}),
        ]),
        *before,
    ] if pipeline_sha == head else before[:1]
    result = fake_api(
        *arguments("gitlab"), "pipeline-gate", "--number", 7, "--sha", head, "--target", "main",
        "--base-sha", base, "--run-id", 12, "--source", "merge_request_event", "--require", "unit",
        requests=requests,
    )
    if pipeline_sha == head:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["checks"] == {"unit": 10}
    else:
        assert result.returncode == 1
        assert not result.stdout
