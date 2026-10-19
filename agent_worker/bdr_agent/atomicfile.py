"""Crash-safe small-file writes (spec 8 "Atomic files").

Every write is: temp file in the same directory, write, flush, fsync, then
os.replace over the target, with the previous good copy kept as `<name>.bak`.
A hard power loss at any line leaves either the old file, the new file, or a
stray temp file; never a half-written target. Readers fall back to `.bak` when
the target is missing or fails to parse.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

_IS_WINDOWS = sys.platform == "win32"


def fsync_dir(path: Path) -> None:
    """Persist a directory entry change (create/rename). No-op on Windows,
    where directories cannot be opened for fsync and NTFS journals renames."""
    if _IS_WINDOWS:
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def bak_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.tmp")


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600, keep_bak: bool = True) -> None:
    """Write `data` to `path` atomically. The previous copy becomes `.bak`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if not _IS_WINDOWS:
        os.chmod(tmp, mode)
    if keep_bak and path.exists():
        # Keep the last good copy. os.replace is atomic, so a crash between the
        # two replaces leaves the .bak (good) and no target, which readers handle.
        os.replace(path, bak_path(path))
    os.replace(tmp, path)
    fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any, *, mode: int = 0o600) -> None:
    data = json.dumps(obj, sort_keys=True, indent=2).encode("utf-8")
    atomic_write_bytes(path, data, mode=mode)


def read_with_bak(path: Path, parse: Callable[[bytes], T]) -> T | None:
    """Parse `path`, falling back to `.bak` when it is missing or corrupt.
    Returns None when neither copy is usable."""
    path = Path(path)
    for candidate in (path, bak_path(path)):
        try:
            raw = candidate.read_bytes()
        except FileNotFoundError:
            continue
        try:
            return parse(raw)
        except Exception:
            continue
    return None


def read_json_with_bak(path: Path) -> Any | None:
    def _parse(raw: bytes) -> Any:
        return json.loads(raw.decode("utf-8"))

    return read_with_bak(path, _parse)


def remove_stray_temps(directory: Path) -> int:
    """Delete temp files a crash left behind (`.<name>.<pid>.tmp`)."""
    removed = 0
    try:
        entries = list(Path(directory).iterdir())
    except FileNotFoundError:
        return 0
    for entry in entries:
        if entry.is_file() and entry.name.startswith(".") and entry.name.endswith(".tmp"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed
