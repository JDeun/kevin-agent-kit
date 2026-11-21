"""Open trusted filesystem objects without following symlinks.

The checks themselves belong to the trust bootstrap, so the install snapshot,
profiles, tools and state all pass through one implementation.
"""

from contextlib import contextmanager
from pathlib import Path
import stat
import tempfile

from .install import KIT

trusted = KIT["trusted"]
directory = KIT["directory"]
directory_at = KIT["directory_at"]
regular_at = KIT["regular_at"]


def identity(info):
    return info.st_dev, info.st_ino


@contextmanager
def private_temporary_directory(prefix):
    root = Path("/tmp")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or not trusted(info, owner=0, sticky=True):
        raise ValueError("temporary staging requires a trusted /tmp")
    with tempfile.TemporaryDirectory(dir=root, prefix=prefix) as temp:
        yield Path(temp)
