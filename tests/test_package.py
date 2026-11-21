import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def invoke(repo, tool, *args, check=True):
    return subprocess.run([str(repo / "scripts" / f"{tool}.sh"), *map(str, args)],
                          cwd=repo, check=check, capture_output=True, text=True)


def test_rename_drives_build_docs_manifest_cli_and_install(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
        ".git", "dist", "__pycache__", ".pytest_cache", ".agent-queue"))
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "--", "."], cwd=source, check=True)
    (source / "plugin/not-for-release.txt").write_text("private fixture")
    (source / "core/not-for-release.py").write_text("private fixture")
    before = (source / "project.toml").read_bytes()
    assert invoke(source, "rename", "../escape", check=False).returncode == 1
    assert (source / "project.toml").read_bytes() == before
    bad_output = source / "docs" / "output"
    assert invoke(source, "build", bad_output, check=False).returncode == 1
    assert not bad_output.exists()
    invoke(source, "rename", "different-kit")
    updated = (source / "project.toml").read_bytes()
    original_config = tomllib.loads(before.decode())
    original_config["project"]["name"] = "different-kit"
    assert tomllib.loads(updated.decode()) == original_config
    result = tmp_path / "artifact"
    invoke(source, "build", result, "--allow-dirty")
    manifest = json.loads((result / "plugin/.claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "different-kit"
    assert manifest["version"] == original_config["project"]["version"] == "0.3.7"
    market = json.loads((result / ".claude-plugin/marketplace.json").read_text())
    assert market["name"] == market["plugins"][0]["name"] == "different-kit"
    assert market["plugins"][0]["source"] == "./plugin"
    assert "`different-kit:dispatch`" in (result / "plugin/commands/run-dispatch.md").read_text()
    assert not (result / "plugin/not-for-release.txt").exists()
    assert not (result / "different_kit/core/not-for-release.py").exists()
    assert (result / "rules/secrets.md").is_file()
    assert "@PROJECT_NAME@" not in (result / "README.md").read_text()
    assert (result / "README.md").read_text().startswith("# different-kit\n")
    assert (result / "docs/ko/README.md").read_text().startswith("# different-kit\n")
    assert "different-kit demo" in (result / "README.md").read_text()
    module = subprocess.run(
        [sys.executable, "-P", "-m", "different_kit", "ledger", "--help"],
        check=True, capture_output=True, text=True, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(result), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert "decision" in module.stdout
    isolated_env = {**os.environ, "PATH": str(tmp_path), "PYTHONPATH": str(source),
                    "PYTHONHOME": str(tmp_path / "unused-python-home")}
    assert "different-kit status" in subprocess.run(
        [str(result / "bin/different-kit"), "status", "--help"], check=True, capture_output=True,
        text=True, cwd=source, env=isolated_env,
    ).stdout
    prefix = tmp_path / "install space"
    prefix.mkdir()
    (prefix / "bin").touch()
    assert invoke(source, "install", "--prefix", prefix, "--allow-dirty", check=False).returncode == 1
    assert not (prefix / "share/different-kit").exists()
    (prefix / "bin").unlink()
    invoke(source, "install", "--prefix", prefix, "--allow-dirty")
    assert (prefix / "share/different-kit/plugin/.claude-plugin/plugin.json").is_file()
    output = subprocess.run([str(prefix / "bin/different-kit"), "ledger", "--help"],
                            cwd=source, capture_output=True, check=True, text=True, env=isolated_env)
    assert "decision" in output.stdout
    output = subprocess.run([str(prefix / "bin/different-kit"), "fitness", "--help"], cwd=source,
                            capture_output=True, check=True, text=True, env=isolated_env)
    assert "ratchet" in output.stdout
    output = subprocess.run([str(prefix / "bin/different-kit"), "wiki", "--help"], cwd=source,
                            capture_output=True, check=True, text=True, env=isolated_env)
    assert "knowledge wiki" in output.stdout
    guard = prefix / "share/different-kit/different_kit/guards/agent-sec"
    output = subprocess.run([str(guard), "--help"], cwd=source, capture_output=True,
                            check=True, text=True, env=isolated_env)
    assert "--root" in output.stdout


def test_scan_checks_all_historical_names_of_the_same_blob(tmp_path):
    repo = tmp_path / "scan"
    (repo / "scripts").mkdir(parents=True)
    for name in ("scan-leaks.sh", "scan_leaks.py"):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    denylist = tmp_path / "scan.denylist"
    denylist.write_text("zz-internal-marker\n")

    def git(*args):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=repo, capture_output=True, check=True)

    git("init", "-q")
    (repo / "allowed").write_text("same bytes")
    (repo / "zz-internal-marker").write_text("same bytes")
    git("add", "allowed", "zz-internal-marker")
    git("commit", "-qm", "add files")
    git("rm", "zz-internal-marker")
    git("commit", "-qm", "remove file")
    result = subprocess.run([str(repo / "scripts/scan-leaks.sh")],
                            cwd=repo, text=True, capture_output=True)
    assert result.returncode == 1
    assert "history path" in result.stdout
    assert "zz-internal-marker" not in result.stdout


def committed_copy(tmp_path):
    source = tmp_path / "committed"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
        ".git", "dist", "__pycache__", ".pytest_cache", ".agent-queue"))

    def git(*args):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", *args], cwd=source, capture_output=True, check=True)

    git("init", "-q")
    git("add", "--", ".")
    git("commit", "-qm", "fixture")
    return source, git


def test_build_ships_committed_bytes_and_rejects_uncommitted_edits(tmp_path):
    source, git = committed_copy(tmp_path)
    ok = invoke(source, "build", tmp_path / "ok")
    assert (tmp_path / "ok/README.md").read_bytes() == (source / "README.md").read_bytes().replace(
        b"@PROJECT_NAME@", b"kevin-agent-kit")
    (source / "README.md").write_text("edited after review\n")
    refused = invoke(source, "build", tmp_path / "dirty", check=False)
    assert refused.returncode == 1 and "differs from HEAD" in refused.stderr + refused.stdout
    assert not (tmp_path / "dirty").exists()
    # A staged but uncommitted new file never reaches the package either.
    git("checkout", "--", "README.md")
    (source / "plugin/staged.md").write_text("staged only")
    git("add", "plugin/staged.md")
    invoke(source, "build", tmp_path / "staged")
    assert not (tmp_path / "staged/plugin/staged.md").exists()
    assert ok.returncode == 0


def test_install_rejects_prefixes_other_accounts_can_write(tmp_path):
    source, _ = committed_copy(tmp_path)
    prefix = tmp_path / "shared"
    (prefix / "share").mkdir(parents=True)
    (prefix / "share").chmod(0o777)
    result = invoke(source, "install", "--prefix", prefix, check=False)
    assert result.returncode == 1 and "untrusted kit path" in result.stdout + result.stderr
    assert not (prefix / "share/kevin-agent-kit").exists() and not (prefix / "bin/kevin-agent-kit").exists()
    (prefix / "share").chmod(0o755)
    redirected = tmp_path / "redirected"
    (redirected / "elsewhere").mkdir(parents=True)
    (redirected / "bin").symlink_to(redirected / "elsewhere")
    result = invoke(source, "install", "--prefix", redirected, check=False)
    assert result.returncode == 1 and "symlink" in result.stdout + result.stderr


def test_launcher_refuses_a_package_in_a_writable_directory(tmp_path):
    source, _ = committed_copy(tmp_path)
    prefix = tmp_path / "prefix"
    invoke(source, "install", "--prefix", prefix)
    command = prefix / "bin/kevin-agent-kit"
    assert subprocess.run([str(command), "--version"], capture_output=True, text=True).returncode == 0
    (prefix / "share/kevin-agent-kit").chmod(0o777)
    result = subprocess.run([str(command), "--version"], capture_output=True, text=True)
    assert result.returncode != 0 and "untrusted kit path" in result.stderr
