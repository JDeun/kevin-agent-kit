import argparse
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.loop.config import NAME, project

TOKEN = "@PROJECT_NAME@"
ENTRIES = ("project.toml", "LICENSE", "README.md", "AGENTS.md", "core", "docs",
           "agents", "reviews", "guards", "verify", "plugin")


def copy_tree(source, target, name, version):
    if source.is_symlink():
        raise ValueError(f"symlinks are outside the package inputs: {source}")
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            if item.name not in {"__pycache__", "node_modules", ".pytest_cache", ".venv"}:
                copy_tree(item, target / item.name.replace(TOKEN, name), name, version)
        return
    if source.suffix in (".md", ".json"):
        content = source.read_text(encoding="utf-8")
        target.write_text(
            content.replace(TOKEN, name).replace("@PROJECT_VERSION@", version), encoding="utf-8")
    else:
        shutil.copy2(source, target)


def build(destination):
    meta = project(ROOT)
    name = meta["name"]
    destination = destination or ROOT / "dist" / name
    if destination.is_symlink():
        raise ValueError(f"build output cannot be a symlink: {destination}")
    destination = destination.resolve()
    if any(destination.is_relative_to(ROOT / entry) for entry in ENTRIES):
        raise ValueError(f"build output cannot be inside a package input: {destination}")
    if destination.exists():
        raise ValueError(f"build output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".build-") as temp:
        stage = Path(temp) / "product"
        stage.mkdir()
        for entry in ENTRIES:
            copy_tree(ROOT / entry, stage / entry, name, meta["version"])
        shutil.copy2(ROOT / "LICENSE", stage / "plugin" / "LICENSE")
        (stage / "bin").mkdir()
        command = stage / "bin" / name
        command.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'root=$(CDPATH="" cd -- "$(dirname -- "$0")/.." && pwd)\n'
            f'if [ ! -d "$root/core/loop" ]; then root="$root/share/{name}"; fi\n'
            'export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"\n'
            'exec python3 -P -m core.loop "$@"\n',
            encoding="utf-8",
        )
        command.chmod(0o755)
        stage.rename(destination)
    return destination


def install(prefix):
    name = project(ROOT)["name"]
    binary = prefix / "bin" / name
    destination = prefix / "share" / name
    if binary.exists() or binary.is_symlink() or destination.exists() or destination.is_symlink():
        raise ValueError(f"install path already exists for {name}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    target = build(destination)
    try:
        with tempfile.TemporaryDirectory(dir=binary.parent, prefix=".install-") as temp:
            staged = Path(temp) / name
            shutil.copy2(target / "bin" / name, staged)
            os.link(staged, binary)
    except OSError:
        shutil.rmtree(target)
        raise
    return target


def rename(name):
    if len(name) > 64 or not NAME.fullmatch(name):
        raise ValueError("project name must be a lowercase slug of up to 64 characters")
    current = project(ROOT)["name"]
    path = ROOT / "project.toml"
    source = path.read_text(encoding="utf-8")
    line = f'name = "{current}"'
    if source.splitlines().count(line) != 1:
        raise ValueError("expected one canonical project name assignment")
    updated = re.sub(
        rf"^{re.escape(line)}$", f'name = "{name}"', source, flags=re.M,
    )
    with tempfile.TemporaryDirectory(dir=ROOT, prefix=".rename-") as temp:
        staged = Path(temp)
        (staged / path.name).write_text(updated, encoding="utf-8")
        if project(staged)["name"] != name:
            raise ValueError("updated metadata did not contain the requested name")
        (staged / path.name).chmod(path.stat().st_mode & 0o777)
        (staged / path.name).replace(path)
    return name


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("build").add_argument("destination", type=Path, nargs="?")
    sub.add_parser("install").add_argument("--prefix", type=Path, default=Path.home() / ".local")
    sub.add_parser("rename").add_argument("name")
    args = parser.parse_args()
    try:
        if args.action == "build":
            print(build(args.destination))
        elif args.action == "install":
            print(install(args.prefix.resolve()))
        else:
            print(rename(args.name))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
