"""Open trusted filesystem objects without following symlinks."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import tempfile

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def trusted(info, *, owner=None, sticky=False):
    owners = {0, os.getuid()} if owner is None else {owner}
    protected = sticky and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
    return info.st_uid in owners and (not info.st_mode & 0o022 or protected)


def open_directory(parent, name, label, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        return os.open(name, DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        if error.errno in {errno.ENOTDIR, errno.ELOOP}:
            raise ValueError(f"untrusted directory: {label}") from None
        raise


@contextmanager
def directory(path, *, create=False, owner=None, private=False):
    if not path.is_absolute() or ".." in path.parts or path.anchor != "/":
        raise ValueError(f"trusted directory must be an absolute path: {path}")
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = open_directory(fd, part, path, create=create)
            os.close(fd)
            fd = child
            if not trusted(os.fstat(fd), sticky=True):
                raise ValueError(f"untrusted directory: {path}")
        info = os.fstat(fd)
        if not trusted(info, owner=owner) or private and info.st_mode & 0o077:
            raise ValueError(f"untrusted directory: {path}")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def directory_at(parent, name, *, create=False, private=False):
    fd = open_directory(parent, name, name, create=create)
    try:
        info = os.fstat(fd)
        if not trusted(info, owner=os.getuid()) or private and info.st_mode & 0o077:
            raise ValueError(f"untrusted directory: {name}")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular_at(fd, name, *, flags=os.O_RDONLY, owner=None, single_link=False):
    try:
        opened = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
            raise ValueError(f"untrusted regular file: {name}") from None
        raise
    with os.fdopen(opened, "rb") as stream:
        info = os.fstat(opened)
        if (not stat.S_ISREG(info.st_mode) or not trusted(info, owner=owner)
                or single_link and info.st_nlink != 1):
            raise ValueError(f"untrusted regular file: {name}")
        yield stream


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
