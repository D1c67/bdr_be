"""Crash-safe small-file writes for the launcher (stdlib only).

Same rules as bdr_agent/atomicfile.py, which the launcher cannot import (it
runs on the bare runtime): temp file in the same folder, write, flush, fsync,
then os.replace over the target, keeping the previous copy as `<name>.bak`.
Readers fall back to `.bak` when the target is missing or does not parse.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

_IS_WINDOWS = sys.platform == "win32"


def bak_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _fsync_dir(path: Path) -> None:
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


def write_bytes(path: Path, data: bytes, *, keep_bak: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if keep_bak and path.exists():
        os.replace(path, bak_path(path))
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def write_json(path: Path, obj: Any) -> None:
    write_bytes(path, json.dumps(obj, sort_keys=True, indent=2).encode("utf-8"))


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def read_json(path: Path) -> Any | None:
    """The target, else its `.bak`, else None."""
    for candidate in (Path(path), bak_path(Path(path))):
        try:
            return json.loads(candidate.read_bytes().decode("utf-8"))
        except (FileNotFoundError, ValueError, UnicodeDecodeError):
            continue
        except OSError:
            continue
    return None


def remove(path: Path) -> None:
    """Delete a pointer file and its `.bak` (missing is fine)."""
    for candidate in (Path(path), bak_path(Path(path))):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
