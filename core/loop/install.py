"""The one view of the installed kit: its trust bootstrap and its policy snapshot.

A launcher or module restart has already read the install into a snapshot and
imported this package from it. A plain import (tests, packaging, `python -m`)
takes the same snapshot with the bootstrap beside the package. Every reader of
installed policy input (metadata, defaults, lenses, rules) goes through here.
"""
from functools import cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
MODULE_PREFIX = __package__.removesuffix("core.loop")
ROOT = PACKAGE_ROOT.parent if MODULE_PREFIX else PACKAGE_ROOT
PREFIX = MODULE_PREFIX.replace(".", "/")
BOOTSTRAP = PREFIX + "guards/agent-sec"

_launched = getattr(__spec__.loader, "kit_snapshot", None)
SOURCE = (_launched.files[BOOTSTRAP] if _launched is not None
          else (PACKAGE_ROOT / "guards" / "agent-sec").read_bytes()).decode("utf-8")
# A generated child starts with its own copy, before importing any kit code.
EMBEDDED = "_kit_embedded = True\n" + SOURCE
KIT = {"__name__": "kit_bootstrap", "_kit_embedded": True}
exec(compile(SOURCE, BOOTSTRAP, "exec"), KIT)
Untrusted = KIT["Untrusted"]


def take(root, packages=None):
    """Check and read another kit tree with the bootstrap's trust function."""
    if packages is None:
        packages = (MODULE_PREFIX.rstrip("."),) if MODULE_PREFIX else KIT["SOURCE_PACKAGES"]
    return KIT["snapshot"](root, packages)


@cache
def snapshot():
    if _launched is not None:
        return _launched
    taken = take(ROOT)
    if taken.files.get(BOOTSTRAP) != SOURCE.encode("utf-8"):
        raise Untrusted(f"untrusted kit file: {PACKAGE_ROOT / 'guards' / 'agent-sec'}")
    return taken


def project_bytes():
    try:
        return snapshot().files["project.toml"]
    except KeyError:
        raise ValueError("installed project.toml is missing") from None


def has(name):
    """Whether the package holds this file, named relative to the package root."""
    return PREFIX + name in snapshot().files


def text(name):
    try:
        return snapshot().files[PREFIX + name].decode("utf-8")
    except KeyError:
        raise ValueError(f"installed kit file is missing: {name}") from None


def rules():
    """The installed rule documents by file name. Prompts, the policy and the sandbox mount share them."""
    documents = {name.removeprefix("rules/"): content for name, content in snapshot().files.items()
                 if name.startswith("rules/") and name.endswith(".md") and name.count("/") == 1}
    if not documents:
        raise ValueError("installed rule documents are missing")
    return documents
