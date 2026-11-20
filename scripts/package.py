import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.loop.config import NAME, project

RUNTIME = ("core", "reviews", "guards", "verify")
DOCUMENTS = ("reviews/persona/README.md", "verify/README.md")
ENTRIES = (
    "project.toml", "LICENSE", "README.md", "CHANGELOG.md", "AGENTS.md", "docs", "agents",
    "plugin", "rules", ".claude-plugin",
)


def copy_file(source, target, render):
    if source.resolve() != source or not source.is_file():
        raise ValueError(f"package input must be a regular file without symlinks: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix in (".md", ".json"):
        content = source.read_text(encoding="utf-8")
        target.write_text(render(content), encoding="utf-8")
    else:
        shutil.copy2(source, target)


def tracked_inputs():
    checkout = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], text=True,
    ).rstrip("\n")
    if Path(checkout).resolve() != ROOT:
        raise ValueError("build from a Git checkout of the kit")
    records = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "--stage", "-z"])
    for record in records.split(b"\0"):
        if not record:
            continue
        fields, path = record.split(b"\t", 1)
        mode, _, stage = fields.split()
        relative = Path(os.fsdecode(path))
        entry = relative.parts[0]
        if entry not in (*RUNTIME, *ENTRIES):
            continue
        if mode not in {b"100644", b"100755"} or stage != b"0":
            raise ValueError(f"package input must be a tracked regular file: {relative}")
        yield relative, entry in RUNTIME


def build(destination):
    meta = project(ROOT)
    name = meta["name"]
    module = name.replace("-", "_")
    # Render the upstream brand in source docs and plugin templates, keeping its URL.
    manifest = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    upstream = Path(manifest["repository"]).name.removesuffix(".git")
    names = {upstream: name, manifest["name"]: name}
    alternatives = "|".join(re.escape(value) for value in names)
    brand = re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-]|\.git)")

    def render(content):
        return brand.sub(lambda match: names[match[0]], content).replace(
            "@PROJECT_VERSION@", meta["version"])

    destination = destination or ROOT / "dist" / name
    if destination.is_symlink():
        raise ValueError(f"build output cannot be a symlink: {destination}")
    destination = destination.resolve()
    if any(destination.is_relative_to(ROOT / entry) for entry in (*ENTRIES, *RUNTIME)):
        raise ValueError(f"build output cannot be inside a package input: {destination}")
    if destination.exists():
        raise ValueError(f"build output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".build-") as temp:
        stage = Path(temp) / "product"
        stage.mkdir()
        package = stage / module
        for path, runtime in tracked_inputs():
            prefix = package if runtime else stage
            copy_file(ROOT / path, prefix.joinpath(*(render(part) for part in path.parts)), render)
            if path.as_posix() in DOCUMENTS:
                copy_file(ROOT / path, stage / path, render)
        (package / "__init__.py").touch()
        (package / "__main__.py").write_text(
            "from .core.loop.__main__ import main\n\nraise SystemExit(main())\n", encoding="utf-8")
        shutil.copy2(ROOT / "LICENSE", stage / "plugin" / "LICENSE")
        (stage / "bin").mkdir(exist_ok=True)
        command = stage / "bin" / name
        command.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'root=$(CDPATH="" cd -- "$(dirname -- "$0")/.." && pwd)\n'
            f'if [ ! -f "$root/{module}/__main__.py" ]; then root="$root/share/{name}"; fi\n'
            'export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"\n'
            f'exec python3 -P -m {module} "$@"\n',
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
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
