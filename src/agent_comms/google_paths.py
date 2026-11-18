"""Owner-only directories shared by Google Chat input and output paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class FileDirectoryError(ValueError):
    """A Google Chat file directory is not a real, owned directory."""


def work_files_root() -> Path:
    """Use the seat's work folder without creating or changing that folder."""
    work = Path.home() / "work"
    try:
        info = work.lstat()
    except OSError as exc:
        raise FileDirectoryError("agent work folder is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise FileDirectoryError("agent work folder is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise FileDirectoryError("agent work folder is not a directory")
    if info.st_uid != os.getuid():
        raise FileDirectoryError("agent work folder is not owned by this seat user")
    return work / "comms-files"


def ensure_owner_only_directory(path: Path) -> None:
    """Create or tighten one directory without following its final component."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise FileDirectoryError("file directory cannot be created") from exc
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise FileDirectoryError("file directory path contains a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise FileDirectoryError("file directory path is not a directory")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise FileDirectoryError("file directory is not a real directory") from exc
    try:
        current = os.fstat(descriptor)
        if current.st_uid != os.getuid():
            raise FileDirectoryError("file directory is not owned by this seat user")
        if stat.S_IMODE(current.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
    except OSError as exc:
        raise FileDirectoryError("file directory cannot be made owner-only") from exc
    finally:
        os.close(descriptor)
