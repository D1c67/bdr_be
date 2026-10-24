"""One hash over everything the AI Estimating models are told
(docs/AI_ESTIMATING_BUILD_SPEC.md 5.2, 5.7, WS0 step 12).

`register_prompt_part(name, text)` adds a named part: every tool definition
registers itself here (plans_reader_tools.register), and any module with a
system prompt or a schema the model sees may register it too. `prompt_hash()`
is the sha256 over every registered part plus the default parts: the
system prompts (`*_SYSTEM`, `*_PROMPT`, `*_RULES`) of plans_reader_agents
and the model-facing schemas (`*_SCHEMA`, `*_SCHEMAS`) of
plans_reader_schemas. Ordering is stable (sorted by name), and dict values
are serialized with sorted keys, so the hash depends only on content. The
run settings snapshot and the eval sweep row store it, so two runs with the
same hash were asked the same things.

`code_version()` is the companion: BDR_CODE_VERSION when set, else the git
HEAD of bdr_be (with "-dirty" when the tree has changes), read once per
process; None when neither is available.

Pure apart from the lazy import of the two default modules and the one git
read; never raises from prompt_hash() (a default module that fails to import
is recorded as a named error part, which changes the hash visibly).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

HASH_VERSION = 1
DEFAULT_MODULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("app.services.plans_reader_agents", ("_SYSTEM", "_PROMPT", "_RULES")),
    ("app.services.plans_reader_schemas", ("_SCHEMA", "_SCHEMAS")),
)

_lock = threading.Lock()
_parts: dict[str, str] = {}
_code_version: tuple[bool, str | None] = (False, None)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def register_prompt_part(name: str, text: Any) -> None:
    """Add or replace one named part (a system prompt, a tool definition,
    a schema). Non-string values are serialized as canonical JSON."""
    key = str(name or "").strip()
    if not key:
        raise ValueError("A prompt part needs a name.")
    with _lock:
        _parts[key] = _as_text(text)


def unregister_prompt_part(name: str) -> None:
    with _lock:
        _parts.pop(str(name), None)


def registered_parts() -> dict[str, str]:
    """A copy of the explicitly registered parts."""
    with _lock:
        return dict(_parts)


def clear_registered_parts() -> None:
    """Test hook: forget every explicitly registered part."""
    with _lock:
        _parts.clear()


def default_parts() -> dict[str, str]:
    """The module-level prompts and schemas of the default modules, named
    `<module tail>.<CONSTANT>`."""
    import importlib

    out: dict[str, str] = {}
    for module_name, suffixes in DEFAULT_MODULES:
        tail = module_name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - a broken module changes the hash visibly
            out[f"{tail}.__import_error__"] = type(exc).__name__
            continue
        for attr in sorted(vars(module)):
            if not attr.isupper() or not attr.endswith(suffixes):
                continue
            value = getattr(module, attr)
            if isinstance(value, (str, dict, list, tuple)):
                out[f"{tail}.{attr}"] = _as_text(value)
    return out


def all_parts(*, include_defaults: bool = True) -> dict[str, str]:
    """Every part the hash covers (registered parts win on a name clash)."""
    parts = default_parts() if include_defaults else {}
    parts.update(registered_parts())
    return parts


def prompt_hash(*, include_defaults: bool = True) -> str:
    """sha256 hex over every part, sorted by name."""
    h = hashlib.sha256(f"bdr-prompt-hash/{HASH_VERSION}\n".encode())
    for name, text in sorted(all_parts(include_defaults=include_defaults).items()):
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha256(text.encode("utf-8")).digest())
        h.update(b"\n")
    return h.hexdigest()


def code_version() -> str | None:
    """BDR_CODE_VERSION, else bdr_be's git HEAD (+ "-dirty"), cached."""
    global _code_version
    with _lock:
        if _code_version[0]:
            return _code_version[1]
    value = (os.environ.get("BDR_CODE_VERSION") or "").strip() or _git_version()
    with _lock:
        _code_version = (True, value)
    return value


def _git_version() -> str | None:
    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],  # untracked source counts as dirty
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not sha:
        return None
    return f"{sha}-dirty" if dirty else sha


def reset_code_version() -> None:
    """Test hook: read the version again on the next call."""
    global _code_version
    with _lock:
        _code_version = (False, None)
