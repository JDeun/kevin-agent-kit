import json

import pytest

from core.loop.git import Git
from guards.fitness.__main__ import main


@pytest.mark.parametrize(
    "new_violation, baseline_count, expected",
    [(False, 1, 0), (True, 1, 1), (True, 2, 1), (False, 0, 1)],
)
def test_baseline_only_accepts_existing_violations(tmp_path, capsys, new_violation, baseline_count, expected):
    git = Git(tmp_path)
    git.run("init", "--initial-branch=main")
    (tmp_path / "fitness.toml").write_text(
        '[python]\n[[python.forbidden_imports]]\n'
        'paths = ["**/*.py"]\nmodules = ["storage.private"]\n'
    )
    source = tmp_path / "service.py"
    source.write_text("import storage.private\n")
    args = ["--root", str(tmp_path), "--config", "fitness.toml"]
    assert main(["scan", *args]) == 0
    baseline = json.loads(capsys.readouterr().out)
    assert baseline == [{"rule": "forbidden-import", "path": "service.py", "subject": "storage.private", "count": 1}]
    debt = tmp_path / "baseline.json"
    debt.write_text(json.dumps(baseline))
    git.run("add", ".")
    git.run("commit", "-m", "known debt")
    source.write_text("\n\nimport storage.private\n" * (1 + new_violation))
    debt.write_text(json.dumps([{**baseline[0], "count": baseline_count}] if baseline_count else []))
    result = main(["check", *args, "--baseline", debt.name, "--target", "main"])
    assert result == expected
    report = json.loads(capsys.readouterr().out)
    assert bool(report["baseline_growth"]) == (baseline_count > 1)


def test_python_fitness_checks_aliases_and_async_scope(tmp_path, capsys):
    (tmp_path / "fitness.toml").write_text("[python]\n")
    (tmp_path / "operations.py").write_text(
        "import time as clock\n"
        "from subprocess import run as launch\n"
        "async def process():\n"
        "    clock.sleep(1)\n"
        "    launch(['true'])\n"
        "    def offload():\n"
        "        clock.sleep(1)\n"
        "    await asyncio.to_thread(clock.sleep, 1)\n"
        "def sync():\n"
        "    clock.sleep(1)\n"
    )
    assert main(["scan", "--root", str(tmp_path), "--config", "fitness.toml"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"rule": "async-blocking-call", "path": "operations.py", "subject": "process:subprocess.run", "count": 1},
        {"rule": "async-blocking-call", "path": "operations.py", "subject": "process:time.sleep", "count": 1},
    ]


def test_external_analyzer_can_retire_a_baseline_entry(tmp_path, capsys):
    git = Git(tmp_path)
    git.run("init", "--initial-branch=main")
    debt = tmp_path / "baseline.json"
    debt.write_text(json.dumps([{"rule": "dependency", "path": "app.toml", "subject": "layer", "count": 1}]))
    git.run("add", debt.name)
    git.run("commit", "-m", "known debt")
    (tmp_path / "observed.json").write_text("[]")
    args = ["check", "--root", str(tmp_path), "--findings", "observed.json",
            "--baseline", debt.name, "--target", "main"]
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["retired_violations"]
    debt.write_text("[]")
    assert main(args) == 0
    assert not any(json.loads(capsys.readouterr().out).values())


def test_policy_from_target_ignores_a_candidate_that_weakens_its_own_policy(tmp_path, capsys):
    git = Git(tmp_path)
    git.run("init", "--initial-branch=main")
    policy = '[python]\n[[python.forbidden_imports]]\npaths = ["**/*.py"]\nmodules = ["storage.private"]\n'
    (tmp_path / "fitness.toml").write_text(policy)
    (tmp_path / "service.py").write_text("x = 1\n")
    (tmp_path / "baseline.json").write_text("[]")
    git.run("add", ".")
    git.run("commit", "-m", "policy")
    # The candidate adds a violation and deletes the rule that would catch it.
    (tmp_path / "service.py").write_text("import storage.private\n")
    (tmp_path / "fitness.toml").write_text("[python]\n")
    args = ["check", "--root", str(tmp_path), "--config", "fitness.toml", "--baseline", "baseline.json",
            "--target", "main"]
    assert main(args) == 0  # the candidate's own policy hides the violation
    capsys.readouterr()
    assert main([*args, "--policy-from-target"]) == 1
    assert "storage.private" in capsys.readouterr().out
    git.run("rm", "--cached", "-q", "fitness.toml")
    git.run("commit", "-qm", "drop policy on target")
    assert main([*args, "--policy-from-target"]) == 2
