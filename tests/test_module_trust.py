import os
from pathlib import Path
import subprocess
import shutil

import pytest

from core.loop.runtime import module_command
from scripts.package import write_launcher


def fixture_package(root, name):
    package = root / name
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "__main__.py").write_text("print('ready')\n")
    return package


@pytest.mark.parametrize("entry", ["launcher", "internal"])
@pytest.mark.parametrize("unsafe", ["ancestor-link", "root-link", "package-link", "module-mode",
                                    "directory-mode", "nested-link", "fifo", "hard-link"])
def test_module_start_refuses_untrusted_objects(tmp_path, entry, unsafe):
    base = tmp_path / "prefix"
    (base / "bin").mkdir(parents=True)
    root = base / "lib"
    package = fixture_package(root, "fixture_kit")
    if unsafe in {"ancestor-link", "root-link", "package-link"}:
        path = {"ancestor-link": base, "root-link": root, "package-link": package}[unsafe]
        destination = tmp_path / "elsewhere"
        path.rename(destination)
        path.symlink_to(destination, target_is_directory=True)
    elif unsafe == "module-mode":
        (package / "__main__.py").chmod(0o664)
    elif unsafe == "directory-mode":
        package.chmod(0o775)
    elif unsafe == "nested-link":
        (package / "nested").symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "fifo":
        os.mkfifo(package / "fixture.fifo")
    else:
        (package / "alias.py").hardlink_to(package / "__init__.py")

    if entry == "launcher":
        launcher = base / "bin" / "fixture"
        write_launcher(launcher, "fixture_kit", "lib")
        argv = [launcher]
    else:
        argv = module_command(root, "fixture_kit")
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and "untrusted kit" in result.stderr
    assert not result.stdout


def test_restart_checks_source_sibling_packages(tmp_path):
    for name in ("core", "reviews", "guards", "verify", "wiki"):
        fixture_package(tmp_path, name)
    (tmp_path / "guards" / "__init__.py").chmod(0o666)
    result = subprocess.run(module_command(tmp_path, "core"), capture_output=True, text=True)
    assert result.returncode != 0 and "untrusted kit" in result.stderr


@pytest.mark.parametrize("entry", ["launcher", "internal"])
def test_restart_exposes_only_the_validated_package(tmp_path, entry):
    package = fixture_package(tmp_path, "fixture_kit")
    (package / "__main__.py").write_text(
        "from importlib.util import find_spec\n"
        "print(find_spec('sibling_fixture') is None)\n"
    )
    (tmp_path / "sibling_fixture.py").touch()
    if entry == "launcher":
        launcher = tmp_path / "bin/fixture"
        launcher.parent.mkdir()
        write_launcher(launcher, "fixture_kit", ".")
        argv = [launcher]
    else:
        argv = module_command(tmp_path, "fixture_kit")
    result = subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "True"


def test_restart_preserves_checked_tree_under_permissive_umask(tmp_path):
    package = fixture_package(tmp_path, "fixture_kit")
    result = subprocess.run(module_command(tmp_path, "fixture_kit"), umask=0, check=True,
                            capture_output=True, text=True)
    assert result.stdout == "ready\n"
    assert not (package / "__pycache__").exists()


def test_restart_refuses_package_names_already_loaded_by_python(tmp_path):
    fixture_package(tmp_path, "stat")
    result = subprocess.run(module_command(tmp_path, "stat"), capture_output=True, text=True)
    assert result.returncode != 0 and "already loaded" in result.stderr


def test_daemon_start_rejects_changed_source_tree(tmp_path, monkeypatch):
    from core.loop import daemon

    for name in ("core", "reviews", "guards", "verify", "wiki"):
        fixture_package(tmp_path, name)
    fixture_package(tmp_path / "core", "loop")
    (tmp_path / "core/loop/__main__.py").chmod(0o664)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "validate_execution", lambda cfg: None)
    with pytest.raises(ValueError, match="daemon did not start"):
        daemon.start(dict(state=state, profile=tmp_path / "p.toml", policy_id="test"))
    assert "untrusted kit file" in (state / "daemon.log").read_text()


def test_standalone_guard_checks_source_packages(tmp_path):
    for name in ("core", "reviews", "guards", "verify", "wiki"):
        fixture_package(tmp_path, name)
    (tmp_path / "project.toml").touch()
    guard = tmp_path / "guards/agent-sec"
    shutil.copy2(Path(__file__).resolve().parents[1] / "guards/agent-sec", guard)
    (tmp_path / "core/__init__.py").chmod(0o664)
    result = subprocess.run([guard, "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0 and "untrusted kit file" in result.stderr
