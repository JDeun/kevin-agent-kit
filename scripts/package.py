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

RUNTIME = ("core", "reviews", "guards", "verify", "wiki")
DOCUMENTS = ("reviews/persona/README.md", "verify/README.md")
ENTRIES = (
    "project.toml", "LICENSE", "README.md", "CHANGELOG.md", "AGENTS.md", "docs", "agents",
    "plugin", "rules", ".claude-plugin",
)


def copy_file(relative, target, render, dirty=False):
    source = ROOT / relative
    if source.resolve() != source or not source.is_file():
        raise ValueError(f"package input must be a regular file without symlinks: {source}")
    data = source.read_bytes()
    if not dirty:
        # Ship the committed bytes; refuse when the working tree differs from HEAD.
        committed = subprocess.check_output(["git", "-C", str(ROOT), "show", f"HEAD:{relative.as_posix()}"])
        if committed != data:
            raise ValueError(f"package input differs from HEAD (commit it or pass --allow-dirty): {relative}")
        data = committed
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix in (".md", ".json"):
        target.write_text(render(data.decode("utf-8")), encoding="utf-8")
    else:
        target.write_bytes(data)
    target.chmod(source.stat().st_mode & 0o777)


def tracked_inputs(dirty=False):
    checkout = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], text=True,
    ).rstrip("\n")
    if Path(checkout).resolve() != ROOT:
        raise ValueError("build from a Git checkout of the kit")
    if dirty:
        raw = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "--stage", "-z"])
        entries = [(f.split()[0], f.split()[2], path) for f, _, path in
                   (item.partition(b"\t") for item in raw.split(b"\0") if item)]
    else:
        # Read the committed tree, not the index, so an unreviewed staged file cannot ship.
        raw = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", "HEAD"])
        entries = [(f.split()[0], b"0", path) for f, _, path in
                   (item.partition(b"\t") for item in raw.split(b"\0") if item)]
    for mode, stage, path in entries:
        relative = Path(os.fsdecode(path))
        entry = relative.parts[0]
        if entry not in (*RUNTIME, *ENTRIES):
            continue
        if mode not in {b"100644", b"100755"} or stage != b"0":
            raise ValueError(f"package input must be a tracked regular file: {relative}")
        yield relative, entry in RUNTIME


# The same check runs at install time here and at every launch inside the launcher.
TRUST_CHECK = """\
def trusted(path):
    import os, stat
    uid = os.getuid()
    for entry in (path, *path.parents):
        info = entry.lstat()
        sticky = bool(info.st_mode & stat.S_ISVTX) and entry != path
        if info.st_uid not in (0, uid) or (info.st_mode & 0o022 and not sticky):
            raise SystemExit(f"untrusted kit path (owner or write access): {entry}")
"""
exec(TRUST_CHECK)


def write_launcher(command, module, relative_root):
    command.write_text(
        "#!/usr/bin/python3 -I\n"
        "from pathlib import Path\n"
        "import runpy\n"
        "import sys\n\n"
        f"{TRUST_CHECK}\n"
        f"root = (Path(__file__).resolve().parents[1] / {relative_root!r}).resolve()\n"
        "trusted(root)\n"
        f"if not (root / {module!r} / '__init__.py').is_file():\n"
        "    raise SystemExit(f'kit package is missing from {root}')\n"
        "sys.path.insert(0, str(root))\n"
        f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)\n",
        encoding="utf-8",
    )
    command.chmod(0o755)


def build(destination, dirty=False):
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
        for path, runtime in tracked_inputs(dirty):
            prefix = package if runtime else stage
            copy_file(path, prefix.joinpath(*(render(part) for part in path.parts)), render, dirty)
            if path.as_posix() in DOCUMENTS:
                copy_file(path, stage / path, render, dirty)
        (package / "__init__.py").touch()
        (package / "__main__.py").write_text(
            "from .core.loop.__main__ import main\n\nraise SystemExit(main())\n", encoding="utf-8")
        shutil.copy2(ROOT / "LICENSE", stage / "plugin" / "LICENSE")
        (stage / "bin").mkdir(exist_ok=True)
        write_launcher(stage / "bin" / name, module, ".")
        stage.rename(destination)
    return destination


def install(prefix, dirty=False):
    name = project(ROOT)["name"]
    binary = prefix / "bin" / name
    destination = prefix / "share" / name
    if binary.exists() or binary.is_symlink() or destination.exists() or destination.is_symlink():
        raise ValueError(f"install path already exists for {name}")
    for part in (prefix / "bin", prefix / "share"):
        if part.is_symlink():
            raise ValueError(f"install directory cannot be a symlink: {part}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    trusted(binary.parent)
    trusted(destination.parent)
    target = build(destination, dirty=dirty)
    created = target.lstat()
    try:
        with tempfile.TemporaryDirectory(dir=binary.parent, prefix=".install-") as temp:
            staged = Path(temp) / name
            write_launcher(staged, name.replace("-", "_"), f"share/{name}")
            os.link(staged, binary)
    except OSError:
        # Remove only the directory this run created, never a path swapped in since.
        current = target.lstat()
        if ((current.st_dev, current.st_ino, current.st_uid) == (created.st_dev, created.st_ino, os.getuid())
                and not target.is_symlink()):
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
    dirty_help = "package the working-tree bytes instead of the committed HEAD tree (development only)"
    builder = sub.add_parser("build")
    builder.add_argument("destination", type=Path, nargs="?")
    builder.add_argument("--allow-dirty", action="store_true", help=dirty_help)
    installer = sub.add_parser("install")
    installer.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    installer.add_argument("--allow-dirty", action="store_true", help=dirty_help)
    sub.add_parser("rename").add_argument("name")
    args = parser.parse_args()
    try:
        if args.action == "build":
            print(build(args.destination, args.allow_dirty))
        elif args.action == "install":
            print(install(args.prefix.resolve(), args.allow_dirty))
        else:
            print(rename(args.name))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
