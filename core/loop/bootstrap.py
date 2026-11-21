"""Capture the trusted standalone launcher for use in newly started interpreters."""
from pathlib import Path

from .paths import directory, regular_at


def load_bootstrap():
    folder = Path(__file__).parent.parent.parent / "guards"
    with directory(folder) as parent, regular_at(parent, "agent-sec", single_link=True) as stream:
        return stream.read().decode("utf-8")


# A generated child starts with its own copy, before importing any kit code.
SOURCE = load_bootstrap()
EMBEDDED = "_kit_embedded = True\n" + SOURCE
