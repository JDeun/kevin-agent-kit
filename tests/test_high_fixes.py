"""Regression tests for the seven high-severity findings of the v0.3.1 release check."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import package
from core import agents
from core.loop.engine import Conflict
from core.loop.git import Git
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
    (tree / "fixture").mkdir(parents=True)
    good = tree / "fixture/__init__.py"
    good.write_text("x = 1\n")
    good.chmod(0o644)
    assert package.kit.take(tree, ("fixture",)).files == {"fixture/__init__.py": b"x = 1\n"}
    good.chmod(0o664)
    with pytest.raises(ValueError, match="untrusted kit file"):
        package.kit.take(tree, ("fixture",))
    good.chmod(0o644)
    (tree / "fixture/link.py").symlink_to(good)
    with pytest.raises(ValueError, match="untrusted kit file"):
        package.kit.take(tree, ("fixture",))


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
GOOD = {"id": "bad-1", "risk": "security", "detail": "Unsafe.", "finding_class": "unsafe-input"}


def reply(verdict):
    return agents.review_verdict(json.dumps(verdict), REQUEST, "fixture")


@pytest.mark.parametrize("verdict", [
    {"findings": [], "resolved": {}},  # no explicit status
    {"status": "complete", "findings": "none", "resolved": {}},
    {"status": "complete", "findings": [], "resolved": []},
    {"status": "complete", "findings": [{"id": "x", "risk": "bogus", "detail": "d"}], "resolved": {}},
    {"status": "complete", "findings": [{"id": "x", "risk": "other"}], "resolved": {}},
    {"status": "complete", "findings": [{"id": "x", "risk": "other", "detail": "d"}] * 2, "resolved": {}},
    {"status": "partial", "findings": [], "resolved": {}},
])
def test_r01_malformed_verdicts_are_never_a_complete_clean_review(verdict):
    receipt = reply(verdict)
    assert receipt["status"] != "complete" and receipt["diagnostics"]


def test_r01_incomplete_verdict_keeps_its_protected_findings():
    receipt = reply({"status": "partial", "findings": [GOOD], "resolved": {}})
    assert receipt["status"] == "partial"
    assert [item["id"] for item in receipt["findings"]] == ["bad-1"]
    from core.loop.review import review_receipt

    with pytest.raises(ValueError) as error:
        review_receipt(json.dumps(receipt), REQUEST, [])
    assert [item["id"] for item in error.value.salvaged] == ["bad-1"]


def test_r01_well_formed_verdict_is_complete():
    receipt = reply({"status": "complete", "findings": [GOOD], "resolved": {}})
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


def test_r02_repair_requires_pristine_candidate(tmp_path):
    git = repository(tmp_path / "repo")
    commit = git.resolve()
    git.pristine(commit)
    (git.repo / "stray.txt").write_text("x\n")
    with pytest.raises(Conflict):
        git.pristine(commit)
    (git.repo / "stray.txt").unlink()
    (git.repo / "a.txt").write_text("uncommitted edit\n")
    with pytest.raises(Conflict):
        git.pristine(commit)
    git.run("checkout", "--quiet", "--", "a.txt")
    git.run("commit", "--quiet", "--allow-empty", "-m", "moved")
    with pytest.raises(Conflict):
        git.pristine(commit)


def test_r02_check_reads_fresh_raw_git_tree_for_every_command(tmp_path):
    from core.loop import engine as engine_module

    git = repository(tmp_path / "repo")
    (git.repo / ".gitattributes").write_text("a.txt export-ignore\n")
    binary = git.repo / "script\rname"
    binary.write_bytes(b"\xff\x00\r\n")
    binary.chmod(0o755)
    git.run("add", "-A")
    git.run("commit", "-qm", "fixture modes and attributes")
    commit = git.resolve()
    tree = git.tree(commit)
    (git.repo / "a.txt").write_text("workspace copy\n")
    (git.repo / "scratch.txt").write_text("job scratch\n")
    evidence = {"candidate": commit, "tree": tree, "command": ["x"]}
    runner = engine_module.Engine.__new__(engine_module.Engine)
    workspaces = []

    def inspect(role, argv, work, ev, stdin):
        assert role == "check"
        assert json.loads(stdin)["request"]["tree"] == tree
        assert {p.name for p in work.iterdir()} == {"a.txt", ".gitattributes", binary.name}
        assert (work / "a.txt").read_text() == "one\n"
        assert (work / binary.name).read_bytes() == b"\xff\x00\r\n"
        assert (work / binary.name).stat().st_mode & stat.S_IXUSR
        assert not (work / "a.txt").stat().st_mode & stat.S_IXUSR
        workspaces.append(work)
        return "passed"

    runner.command = inspect
    for name in ("first", "second"):
        receipt = runner.direct_check(git, {**evidence, "check": name, "invocation": name})
        assert receipt["tree"] == tree and receipt["status"] == "passed"
    assert len(set(workspaces)) == 2
    assert all(not path.exists() for path in workspaces)


def test_r02_check_tree_preserves_in_tree_links_and_directory_traversal(tmp_path):
    git = repository(tmp_path / "repo")
    (git.repo / "links").mkdir()
    (git.repo / "links" / "up").symlink_to("..")
    (git.repo / "alias").symlink_to("links/up/a.txt")
    (git.repo / "another").symlink_to("alias")
    git.run("add", "-A")
    git.run("commit", "-qm", "linked fixture")
    with git.check_tree(git.tree("HEAD")) as work:
        assert os.readlink(work / "links" / "up") == ".."
        assert os.readlink(work / "alias") == "links/up/a.txt"
        assert (work / "another").read_bytes() == b"one\n"


@pytest.mark.parametrize("target", ["/outside", "missing", "link", "a.txt/../a.txt", "dir/up/../sibling"])
def test_r02_unresolved_or_external_links_refuse_check_dispatch(tmp_path, target):
    from core.loop.engine import Engine

    git = repository(tmp_path / "repo")
    (git.repo / "dir").mkdir()
    (git.repo / "dir" / "up").symlink_to("..")
    (git.repo / "link").symlink_to(target)
    git.run("add", "-A")
    git.run("commit", "-qm", "link fixture")
    runner = Engine.__new__(Engine)
    runner.command = lambda *args, **kwargs: pytest.fail("unsupported input reached command")
    with pytest.raises(Conflict, match="unsupported"):
        runner.direct_check(git, {"tree": git.tree("HEAD"), "command": ["x"]})


def test_r02_submodule_requires_unavailable_external_tree(tmp_path):
    git = repository(tmp_path / "repo")
    git.run("update-index", "--add", "--cacheinfo", f"160000,{git.resolve()},module")
    git.run("commit", "-qm", "gitlink fixture")
    with pytest.raises(Conflict, match="unsupported"):
        with git.check_tree(git.tree("HEAD")):
            pytest.fail("unsupported gitlink was materialized")


# R03 -------------------------------------------------------------------------------------
@pytest.mark.parametrize("trust", ["local", "signed"])
def test_old_profiles_refuse_host_execution(tmp_path, monkeypatch, trust):
    from core.loop.config import config
    from core.loop.setup import approval_line, repository_profile

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = repository(tmp_path / "repo")
    path = repository_profile(repo.repo)
    path.write_text(f'[loop]\nrepo = {json.dumps(str(repo.repo))}\ntrust = "{trust}"\n{approval_line()}\n'
                    '[loop.execution]\nisolation = "none"\n')
    with pytest.raises(ValueError, match="Bubblewrap"):
        config(path)
