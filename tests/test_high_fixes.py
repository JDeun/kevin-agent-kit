"""Regression tests for the seven high-severity findings of the v0.3.1 release check."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from threading import Event

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import package
from core import agents
from core.loop.engine import Conflict
from core.loop.git import Git
from core.loop.sandbox import execute, execution_policy
from wiki import WikiError, ingest, init, lint
from wiki.external import audit


def repository(path):
    path.mkdir()
    git = Git(path)
    git.run("init", "--quiet", "--template=", "--initial-branch=main")
    (path / "a.txt").write_text("one\n")
    git.run("add", "a.txt")
    git.run("commit", "--quiet", "-m", "base")
    return git


# N11 -------------------------------------------------------------------------------------
def test_n11_installed_files_ignore_work_tree_permissions(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
        ".git", "dist", "__pycache__", ".pytest_cache", ".agent-queue"))
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "--", "."], cwd=source, check=True)
    for relative in ("core/loop/engine.py", "wiki/__init__.py", "README.md"):
        (source / relative).chmod(0o666)
    (source / "core").chmod(0o777)
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(source / "scripts/package.py"), "build", str(out), "--allow-dirty"],
                   check=True, capture_output=True)
    modes = {path: stat.S_IMODE(path.lstat().st_mode) for path in out.rglob("*") if path.is_file()}
    assert modes and all(mode & 0o022 == 0 for mode in modes.values())
    assert modes[next(out.rglob("engine.py"))] == 0o644


def test_n11_tree_check_rejects_writable_and_linked_files(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    good = tree / "good.py"
    good.write_text("x = 1\n")
    good.chmod(0o644)
    package.trusted_tree(tree)
    good.chmod(0o664)
    with pytest.raises(SystemExit, match="untrusted kit file"):
        package.trusted_tree(tree)
    good.chmod(0o644)
    (tree / "link.py").symlink_to(good)
    with pytest.raises(SystemExit, match="untrusted kit file"):
        package.trusted_tree(tree)


# W01 -------------------------------------------------------------------------------------
def test_w01_wiki_refuses_symlinks_inside_the_tree(tmp_path):
    git = repository(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text('{"secret.md": "x"}\n')
    base = git.repo / "docs" / "research"
    base.mkdir(parents=True)
    (base / "sources").symlink_to(outside)
    with pytest.raises(WikiError, match="symlink"):
        init(git.repo)
    assert sorted(path.name for path in outside.iterdir()) == ["manifest.json"]
    (base / "sources").unlink()
    init(git.repo)
    manifest = base / "sources" / "manifest.json"
    manifest.unlink()
    manifest.symlink_to(outside / "manifest.json")
    note = tmp_path / "note.md"
    note.write_text("hello\n")
    with pytest.raises(WikiError, match="symlink"):
        ingest(git.repo, note, today="2026-01-01")
    with pytest.raises(WikiError, match="symlink"):
        lint(git.repo)
    assert (outside / "manifest.json").read_text() == '{"secret.md": "x"}\n'


def test_w01_external_audit_does_not_follow_links(tmp_path):
    checkout = tmp_path / "wiki"
    checkout.mkdir()
    (tmp_path / "private.md").write_text("[gone](missing.md)\n")
    (checkout / "Home.md").write_text("[a](a.md)\n")
    (checkout / "a.md").write_text("ok\n")
    (checkout / "leak.md").symlink_to(tmp_path / "private.md")
    (checkout / "dir").symlink_to(tmp_path)
    report = audit(checkout)
    assert "leak.md" not in report["pages"] and not report["errors"]


def test_w01_task_out_never_overwrites(tmp_path):
    from wiki.__main__ import main

    git = repository(tmp_path / "repo")
    init(git.repo)
    target = tmp_path / "keep.txt"
    target.write_text("precious\n")
    assert main(["--root", str(git.repo), "lint", "--task-out", str(target)]) == 2
    assert target.read_text() == "precious\n"


# R01 -------------------------------------------------------------------------------------
REQUEST = {"paths": ["a.txt"], "lens": "general"}
PROMPT = "instructions\n" + json.dumps({"request": REQUEST}) + "\n"
GOOD = {"id": "bad-1", "risk": "security", "detail": "Unsafe.", "finding_class": "unsafe-input"}


def reply(monkeypatch, verdict):
    text = "thinking\n" + json.dumps(verdict)
    monkeypatch.setattr(agents, "invoke", lambda *args, **kwargs: text)
    monkeypatch.setattr(agents, "version", lambda cli: "1.0")
    return agents.review("codex", PROMPT, ".")


@pytest.mark.parametrize("verdict", [
    {"findings": [], "resolved": {}},  # no explicit status
    {"status": "complete", "findings": "none", "resolved": {}},
    {"status": "complete", "findings": [], "resolved": []},
    {"status": "complete", "findings": [{"id": "x", "risk": "bogus", "detail": "d"}], "resolved": {}},
    {"status": "complete", "findings": [{"id": "x", "risk": "other"}], "resolved": {}},
    {"status": "complete", "findings": [{"id": "x", "risk": "other", "detail": "d"}] * 2, "resolved": {}},
    {"status": "partial", "findings": [], "resolved": {}},
])
def test_r01_malformed_verdicts_are_never_a_complete_clean_review(monkeypatch, verdict):
    receipt = reply(monkeypatch, verdict)
    assert receipt["status"] != "complete" and receipt["diagnostics"]


def test_r01_incomplete_verdict_keeps_its_protected_findings(monkeypatch):
    receipt = reply(monkeypatch, {"status": "partial", "findings": [GOOD], "resolved": {}})
    assert receipt["status"] == "partial"
    assert [item["id"] for item in receipt["findings"]] == ["bad-1"]
    from core.loop.review import review_receipt

    with pytest.raises(ValueError) as error:
        review_receipt(json.dumps(receipt), REQUEST, [])
    assert [item["id"] for item in error.value.salvaged] == ["bad-1"]


def test_r01_well_formed_verdict_is_complete(monkeypatch):
    receipt = reply(monkeypatch, {"status": "complete", "findings": [GOOD], "resolved": {}})
    assert receipt["status"] == "complete" and receipt["findings"][0]["id"] == "bad-1"


# R02 -------------------------------------------------------------------------------------
def test_r02_repair_rejects_a_worker_commit(tmp_path):
    git = repository(tmp_path / "repo")
    start = git.resolve()
    (git.repo / "a.txt").write_text("two\n")
    git.run("commit", "--quiet", "-am", "worker commit")
    with pytest.raises(Conflict, match="moved HEAD"):
        git.repair(["**/*"], start)
    git.run("reset", "--quiet", "--hard", start)
    (git.repo / "a.txt").write_text("three\n")
    assert git.repair(["**/*"], start) != start


def test_r02_candidate_must_be_pristine_before_and_unchanged_after(tmp_path):
    git = repository(tmp_path / "repo")
    commit = git.resolve()
    git.pristine(commit)
    git.unchanged(commit)
    (git.repo / "stray.txt").write_text("x\n")
    with pytest.raises(Conflict):
        git.pristine(commit)
    (git.repo / "stray.txt").unlink()
    (git.repo / "a.txt").write_text("changed by a check\n")
    with pytest.raises(Conflict):
        git.unchanged(commit)
    git.run("checkout", "--quiet", "--", "a.txt")
    git.unchanged(commit)
    git.run("commit", "--quiet", "--allow-empty", "-m", "moved")
    with pytest.raises(Conflict):
        git.unchanged(commit)


def test_r02_local_check_fails_when_the_check_edits_tracked_files(tmp_path, monkeypatch):
    from core.loop import engine as engine_module

    git = repository(tmp_path / "repo")
    commit = git.resolve()
    evidence = {"candidate": commit, "run": "r1"}
    runner = engine_module.Engine.__new__(engine_module.Engine)
    runner.command = lambda role, argv, work, ev, stdin=None: (work / "a.txt").write_text("mutated\n") or ""
    with pytest.raises(ValueError, match="exact candidate"):
        runner.local_check("t", ["x"], git, evidence)
    git.run("checkout", "--quiet", "--", "a.txt")
    runner.command = lambda role, argv, work, ev, stdin=None: "ok"
    receipt = runner.local_check("t", ["x"], git, evidence)
    assert receipt["tree"] == git.tree(commit) and receipt["status"] == "passed"


# R03 -------------------------------------------------------------------------------------
def test_r03_required_checks_get_an_empty_home(tmp_path, monkeypatch):
    (tmp_path / "real-home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    script = tmp_path / "tool.py"
    script.write_text("#!/usr/bin/python3\nimport os\nprint(os.environ['HOME'], len(os.listdir(os.environ['HOME'])))\n")
    script.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    policy = execution_policy({"isolation": "none", "tools": {"probe": str(script)}})
    check = execute(["/tools/probe"], work, Event(), 10, role="verify", policy=policy).split()
    reviewer = execute(["/tools/probe"], work, Event(), 10, role="reviewer", policy=policy).split()
    assert check[0] != str(tmp_path / "real-home") and check[1] == "0"
    assert reviewer[0] == str(tmp_path / "real-home")


def test_r03_signed_trust_cannot_run_unisolated(tmp_path, monkeypatch):
    from core.loop.config import config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    git = repository(tmp_path / "repo")
    from core.loop.setup import repository_profile

    profile = repository_profile(git.repo, agent="codex")
    profile.write_text(profile.read_text().replace('trust = "local"', 'trust = "signed"'))
    with pytest.raises(ValueError):
        config(profile)


# R04 -------------------------------------------------------------------------------------
def test_r04_unisolated_profiles_do_not_start_the_cli_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.shutil, "which", lambda name: f"/usr/bin/{name}")
    for mode in ("read", "write"):
        native = agents.command("codex", mode, tmp_path)
        assert native[native.index("--sandbox") + 1] == ("read-only" if mode == "read" else "workspace-write")
        open_ = agents.command("codex", mode, tmp_path, unsandboxed=True)
        assert open_[open_.index("--sandbox") + 1] == "danger-full-access"
    from core.loop.setup import repository_profile

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    git = repository(tmp_path / "repo")
    assert "--unsandboxed" in repository_profile(git.repo, agent="codex").read_text()
