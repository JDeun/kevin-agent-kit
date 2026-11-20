import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def ledgers(*args):
    return subprocess.run(
        [sys.executable, "-m", "core.ledgers", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def record(path, date="2026-02-04"):
    return ledgers(
        "decision", "record", "--ledger", path, "--id", "stable-export",
        "--date", date, "--decided-by", "Example Maintainer",
        "--source", "https://example.org/decisions/17",
        "--summary", "Keep CSV as the default export",
    )


def test_decision_records_human_source_and_cannot_be_overwritten(tmp_path):
    path = tmp_path / "decisions.jsonl"
    assert record(path, "2026-02-30").returncode != 0
    assert not path.exists()
    result = record(path)
    assert result.returncode == 0, result.stderr
    assert record(path, "2026-02-05").returncode != 0
    result = ledgers("decision", "show", "--ledger", path, "--id", "stable-export")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "id": "stable-export", "date": "2026-02-04", "decided_by": "Example Maintainer",
        "source": "https://example.org/decisions/17",
        "summary": "Keep CSV as the default export",
    }


def test_pytest_pin_checks_behavior_and_points_to_human_source(tmp_path):
    result = record(tmp_path / "decisions.jsonl")
    assert result.returncode == 0, result.stderr
    from core.ledgers.pins import assert_decision_pin

    ledger = tmp_path / "decisions.jsonl"
    assert_decision_pin("csv", "csv", ledger=ledger, decision_id="stable-export")
    with pytest.raises(AssertionError, match=(
        r"Changing decision stable-export requires a human decision .*example\.org/decisions/17"
    )):
        assert_decision_pin("json", "csv", ledger=ledger, decision_id="stable-export")
    with pytest.raises(AssertionError, match="missing"):
        assert_decision_pin("csv", "csv", ledger=ledger, decision_id="missing")


def test_vitest_helper_checks_behavior_and_points_to_human_source(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node is optional")
    ledger = tmp_path / "decisions.jsonl"
    result = record(ledger)
    assert result.returncode == 0, result.stderr
    helper = (ROOT / "core" / "ledgers" / "pins.mjs").as_uri()
    code = r"""
import assert from 'node:assert/strict'
import { assertDecisionPin } from DIST
const options = { ledger: process.argv[1], decisionId: 'stable-export' }
assertDecisionPin({ format: 'csv' }, { format: 'csv' }, options)
assert.throws(() => assertDecisionPin('json', 'csv', options),
  /Changing decision stable-export requires a human decision .*example\.org/)
assert.throws(() => assertDecisionPin('csv', 'csv', { ...options, decisionId: 'missing' }), /missing/)
""".replace("DIST", json.dumps(helper))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code, str(ledger)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_concurrent_reservations_are_unique_and_never_reuse_numbers(tmp_path):
    path = tmp_path / "ordinals.jsonl"
    commands = [
        subprocess.Popen(
            [sys.executable, "-m", "core.ledgers", "ordinal", "reserve",
             "--ledger", str(path), "--resource", "database", "--after", "12"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(5)
    ]
    ordinals = []
    for process in commands:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        ordinals.append(int(stdout))
    assert sorted(ordinals) == [13, 14, 15, 16, 17]
    result = ledgers("ordinal", "reserve", "--ledger", path, "--resource", "database")
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == 18
    result = ledgers("ordinal", "reserve", "--ledger", path, "--resource", "other")
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == 1


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_migration_guard_checks_target_max_and_duplicate_ids(tmp_path):
    repo = tmp_path
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Example Tester")
    git(repo, "config", "user.email", "tester@example.org")
    directory = repo / "migrations"
    directory.mkdir()
    (directory / "012_start.sql").touch()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "target")
    target = git(repo, "rev-parse", "HEAD")

    def check():
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "migration")
        return ledgers(
            "ordinal", "check-migrations", "--repo", repo, "--target", target,
            "--directory", "migrations",
        )

    (directory / "010_late.sql").touch()
    result = check()
    assert result.returncode == 1
    assert "010_late.sql" in result.stderr and "12" in result.stderr
    (directory / "010_late.sql").rename(directory / "013_next.sql")
    (directory / "13-other.sql").touch()
    result = check()
    assert result.returncode == 1
    assert "13-other.sql" in result.stderr and "013_next.sql" in result.stderr
    (directory / "13-other.sql").unlink()
    result = check()
    assert result.returncode == 0, result.stderr


def test_migration_guard_uses_updated_target_even_for_same_named_addition(tmp_path):
    repo = tmp_path
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Example Tester")
    git(repo, "config", "user.email", "tester@example.org")
    git(repo, "commit", "--allow-empty", "-qm", "common")
    target = git(repo, "branch", "--show-current")
    git(repo, "checkout", "-qb", "feature")
    directory = repo / "migrations"
    directory.mkdir()
    path = directory / "013_next.sql"
    path.write_text("-- feature version\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "feature")
    git(repo, "checkout", "-q", target)
    directory.mkdir(exist_ok=True)
    path.write_text("-- target version\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "target advanced")

    result = ledgers(
        "ordinal", "check-migrations", "--repo", repo, "--target", target,
        "--head", "feature",
    )

    assert result.returncode == 1
    assert "013_next.sql" in result.stderr and "target maximum 13" in result.stderr
