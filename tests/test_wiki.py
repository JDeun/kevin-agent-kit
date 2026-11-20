import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wiki import WikiError, ingest, init, lint, lint_task
from wiki.__main__ import main
from wiki.external import lint_remote

PAGE = """---
title: Cache sizing
status: current
updated: 2026-01-02
sources:
  - sources/2026-01-01-notes.md
---
# Cache sizing

Findings from [the notes](sources/2026-01-01-notes.md).
"""


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


@pytest.fixture
def seeded(repo, tmp_path):
    init(repo)
    note = tmp_path / "notes.md"
    note.write_text("raw finding\n")
    ingest(repo, note, today="2026-01-01")
    base = repo / "docs/research"
    (base / "cache.md").write_text(PAGE)
    readme = base / "README.md"
    readme.write_text(readme.read_text().replace("## Pages\n", "## Pages\n\n- [Cache sizing](cache.md)\n"))
    return repo, base


def test_init_creates_files_and_never_overwrites(repo):
    first = init(repo)
    assert first["kept"] == [] and len(first["created"]) == 3
    readme = repo / "docs/research/README.md"
    readme.write_text("# my own map\n")
    second = init(repo)
    assert second["created"] == [] and len(second["kept"]) == 3
    assert readme.read_text() == "# my own map\n"


def test_init_rejects_paths_outside_the_checkout(repo):
    for bad in ("../escape", "/abs", ""):
        with pytest.raises(WikiError):
            init(repo, bad)


def test_ingest_stores_dated_immutable_source_and_task(repo, tmp_path):
    init(repo)
    note = tmp_path / "Market Notes.md"
    note.write_text("raw\n")
    result = ingest(repo, note, today="2026-03-04")
    assert result["source"] == "docs/research/sources/2026-03-04-market-notes.md"
    manifest = json.loads((repo / "docs/research/sources/manifest.json").read_text())
    assert list(manifest) == ["2026-03-04-market-notes.md"]
    assert "CONFLICT" in result["task"] and "sources/2026-03-04-market-notes.md" in result["task"]
    with pytest.raises(WikiError, match="immutable"):
        ingest(repo, note, today="2026-03-04")
    with pytest.raises(WikiError, match="YYYY-MM-DD"):
        ingest(repo, note, today="03/04/2026")


def test_ingest_requires_init_and_regular_file(repo, tmp_path):
    note = tmp_path / "n.md"
    note.write_text("x")
    with pytest.raises(WikiError, match="init"):
        ingest(repo, note)
    init(repo)
    with pytest.raises(WikiError, match="regular file"):
        ingest(repo, tmp_path)


def test_lint_passes_a_consistent_wiki(seeded):
    repo, _ = seeded
    report = lint(repo)
    assert report["errors"] == [] and report["warnings"] == []


@pytest.mark.parametrize("change, expected", [
    (lambda b: (b / "cache.md").write_text(PAGE.replace("sources/2026-01-01-notes.md\n", "sources/2026-09-09-gone.md\n", 1)),
     "not an ingested file"),
    (lambda b: (b / "cache.md").write_text(PAGE.replace("---\ntitle", "title", 1)), "missing front matter"),
    (lambda b: (b / "cache.md").write_text(PAGE.replace("status: current", "status: maybe")), "status must be"),
    (lambda b: (b / "cache.md").write_text(PAGE.replace("2026-01-02", "yesterday")), "updated must be"),
    (lambda b: (b / "cache.md").write_text(PAGE + "\n[x](missing.md)\n"), "broken link"),
    (lambda b: (b / "extra.md").write_text(PAGE), "orphan page"),
    (lambda b: (b / "sources/2026-01-01-notes.md").write_text("edited\n"), "bytes changed"),
    (lambda b: (b / "sources/2026-01-01-notes.md").unlink(), "missing"),
    (lambda b: (b / "sources/stray.md").write_text("x"), "not recorded"),
    (lambda b: (b / "cache.md").write_text(PAGE.replace("sources:\n  - sources/2026-01-01-notes.md\n", "sources:\n")),
     "at least one source"),
])
def test_lint_reports_each_defect(seeded, change, expected):
    repo, base = seeded
    change(base)
    errors = lint(repo)["errors"]
    assert any(expected in error for error in errors), errors


def test_lint_warns_about_conflicts_and_uncited_sources(seeded, tmp_path):
    repo, base = seeded
    (base / "cache.md").write_text(PAGE + "\n> CONFLICT: sources disagree\n")
    extra = tmp_path / "later.md"
    extra.write_text("later\n")
    ingest(repo, extra, today="2026-02-01")
    report = lint(repo)
    assert report["errors"] == []
    assert any("CONFLICT" in item for item in report["warnings"])
    assert any("no synthesis page cites" in item for item in report["warnings"])
    assert lint(repo, strict=True)["errors"]


def test_lint_task_lists_pages(seeded):
    repo, _ = seeded
    task = lint_task(repo)
    assert "docs/research/cache.md" in task and "contradiction" in task


def test_cli_lint_exit_codes_and_task_file(seeded, tmp_path, monkeypatch, capsys):
    repo, base = seeded
    monkeypatch.chdir(repo)
    out = tmp_path / "task.md"
    assert main(["lint", "--task-out", str(out)]) == 0
    assert out.read_text().startswith("# Wiki lint task")
    (base / "extra.md").write_text(PAGE)
    assert main(["lint"]) == 1
    capsys.readouterr()
    assert main(["--dir", "../x", "init"]) == 2


def test_external_lint_is_read_only(tmp_path):
    remote = tmp_path / "remote.wiki"
    remote.mkdir()
    git(remote, "init", "-q")
    (remote / "Home.md").write_text("[a](Guide) [b](Missing)\n")
    (remote / "Guide.md").write_text("guide\n")
    (remote / "Lonely.md").write_text("alone\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "seed")
    head = subprocess.check_output(["git", "-C", str(remote), "rev-parse", "HEAD"], text=True)
    report = lint_remote(str(remote))
    assert report["read_only"] is True
    assert any("Missing" in item for item in report["errors"])
    assert any("Lonely.md" in item for item in report["warnings"])
    assert subprocess.check_output(["git", "-C", str(remote), "rev-parse", "HEAD"], text=True) == head
    with pytest.raises(WikiError):
        lint_remote("--upload-pack=x")
