"""Record and replay of model calls at the transport boundary
(docs/AI_ESTIMATING_BUILD_SPEC.md 0.4 rule 6, WS0 step 11).

`llm._guarded` opens a tape for every provider call (`begin`). The tape is
keyed by a content address: the sha256 of the canonical JSON of the request
fingerprint (feature, provider, model and the request the transport is about
to send). LLM_CASSETTE_DIR is the cassettes directory itself (spec 5.7 and
8.5: `$EVAL_ROOT/cassettes`, outside git): cassettes live at
`<LLM_CASSETTE_DIR>/<sha256>.json` and every hit, miss and recording is
appended to a per-sweep index, `<LLM_CASSETTE_DIR>/index/<sweep id or
no_sweep>.jsonl`.

Modes (LLM_CASSETTE_MODE):
- off: no tape at all (the default; nothing is fingerprinted or read).
- record: always call live and store the response, overwriting an earlier
  recording of the same request; never replays, so repeats measure noise.
- replay: a hit returns the recorded response; a miss raises CassetteMiss
  (permanent). Never live, $0. Every $0 suite and CI uses it.
- record_missing: replay on a hit, live on a miss and store it.

A hit sets `llm.last_usage()` to the recorded usage as an `llm.CassetteUsage`
(tagged `source == "cassette"`) and `llm.last_cost_usd()` to 0, returns
results whose `cost_usd` is 0, writes no llm_call_log row, takes no gate
slot and no ledger reservation, so nothing reaches plans_reader_calls as a
cost. Recorded response ids are replayed, so a recorded tool loop chained on
previous_response_id replays as long as the tool outputs are unchanged.

Fingerprint. Bytes and base64 data URLs are replaced by their sha256 (the
images and PDFs themselves are never stored in a cassette). Volatile fields
that do not change what the model is asked are excluded: at the top level of
a request, timeouts, cache keys, metadata, user and store flags; on the
neutral dataclasses, evidence refs and view ids, pixel sizes, cache
breakpoints and the run's route and step (provider, model and effort are
fingerprinted on their own). Keys inside arbitrary dicts (JSON schemas, tool
arguments) are never dropped.

Cassettes are dev only: any mode but off refuses every call in production.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.services.llm_errors import CassetteMiss

logger = logging.getLogger(__name__)

MODES: tuple[str, ...] = ("off", "record", "replay", "record_missing")
FINGERPRINT_VERSION = 1
CASSETTE_VERSION = 1
PACIFIC = ZoneInfo("America/Los_Angeles")

# Dropped from the TOP level of a request dict (the wire kwargs or the
# request llm.py builds), never from nested user data such as JSON schemas.
VOLATILE_REQUEST_KEYS = frozenset(
    {"timeout", "prompt_cache_key", "cache_key", "metadata", "user", "store", "max_retries", "route", "step"}
)
# Dropped from every dataclass (llm_parts parts, turns and ModelRequest).
VOLATILE_FIELDS = frozenset(
    {
        "timeout",
        "cache_key",
        "cache_breakpoints",
        "cache_break",
        "route",
        "step",
        "ref",
        "view_id",
        "w_px",
        "h_px",
        "pages",
    }
)

_index_lock = threading.Lock()


# ── Fingerprint ──────────────────────────────────────────────────────────


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj: Any, *, top: bool = False, depth: int = 0) -> Any:
    """A JSON-ready, deterministic image of a request value."""
    if depth > 60:
        return "<deep>"
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, str):
        if obj.startswith("data:") and ";base64," in obj[:200]:
            head, _, b64 = obj.partition(",")
            return f"{head};sha256={_sha(b64.encode('ascii', 'ignore'))}"
        return obj
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raw = bytes(obj)
        return {"__bytes__": _sha(raw), "n": len(raw)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {"__type__": type(obj).__name__}
        for f in dataclasses.fields(obj):
            if f.name in VOLATILE_FIELDS:
                continue
            out[f.name] = canonical(getattr(obj, f.name), depth=depth + 1)
        return out
    if isinstance(obj, dict):
        return {
            str(k): canonical(v, depth=depth + 1)
            for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))
            if not (top and str(k) in VOLATILE_REQUEST_KEYS)
        }
    if isinstance(obj, (list, tuple)):
        return [canonical(v, depth=depth + 1) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((canonical(v, depth=depth + 1) for v in obj), key=lambda v: json.dumps(v, sort_keys=True))
    return f"<{type(obj).__name__}>"


def fingerprint(feature: str, provider: str, model: str, request: Any) -> dict:
    """The request identity a cassette is keyed by."""
    return {
        "v": FINGERPRINT_VERSION,
        "feature": feature,
        "provider": provider,
        "model": model,
        "request": canonical(request, top=True),
    }


def key(fp: dict) -> str:
    """sha256 of the canonical JSON (sorted keys, compact separators)."""
    text = json.dumps(fp, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha(text.encode("utf-8"))


# ── Result encoding ──────────────────────────────────────────────────────


def _usage_dict(usage: Any) -> dict | None:
    if usage is None:
        return None
    names = (
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
    )
    return {n: int(getattr(usage, n, 0) or 0) for n in names}


def encode_result(result: Any) -> dict:
    """A JSON-ready image of a transport result (str, parsed JSON,
    llm.LlmResult or llm_parts.ModelTurn). Costs are never stored."""
    from app.services import llm, llm_parts

    if isinstance(result, llm.LlmResult):
        return {
            "type": "llm_result",
            "parsed": result.parsed,
            "text": result.text,
            "response_id": result.response_id,
            "tool_calls": list(result.tool_calls),
            "model": result.model,
        }
    if isinstance(result, llm_parts.ModelTurn):
        return {
            "type": "model_turn",
            "text": result.text,
            "parsed": result.parsed,
            "tool_calls": list(result.tool_calls),
            "response_id": result.response_id,
            "stop_reason": result.stop_reason,
            "provider": result.provider,
            "model": result.model,
            "raw_assistant": list(result.raw_assistant),
        }
    if isinstance(result, str):
        return {"type": "text", "value": result}
    return {"type": "json", "value": result}


def decode_result(data: dict, usage: Any) -> Any:
    """Rebuild a recorded result with cost 0 and the cassette usage."""
    from app.services import llm, llm_parts

    kind = data.get("type")
    if kind == "llm_result":
        return llm.LlmResult(
            parsed=data.get("parsed"),
            text=data.get("text") or "",
            usage=usage,
            response_id=data.get("response_id") or "",
            tool_calls=list(data.get("tool_calls") or []),
            model=data.get("model") or "",
            cost_usd=Decimal("0"),
        )
    if kind == "model_turn":
        return llm_parts.ModelTurn(
            text=data.get("text") or "",
            parsed=data.get("parsed"),
            tool_calls=list(data.get("tool_calls") or []),
            usage=usage,
            cost_usd=Decimal("0"),
            response_id=data.get("response_id") or "",
            stop_reason=data.get("stop_reason") or "final",
            provider=data.get("provider") or "",
            model=data.get("model") or "",
            raw_assistant=tuple(data.get("raw_assistant") or ()),
        )
    if kind in ("text", "json"):
        return data.get("value")
    raise CassetteMiss(f"Cassette holds an unknown result type {kind!r}; re-record it.")


def _cassette_usage(data: dict | None) -> Any:
    from app.services import llm

    fields = {k: int(v or 0) for k, v in (data or {}).items() if k in llm.LlmUsage.__dataclass_fields__}
    return llm.CassetteUsage(**fields)


# ── Store ────────────────────────────────────────────────────────────────


def _dir(settings: Any) -> Path:
    raw = (getattr(settings, "llm_cassette_dir", "") or "").strip()
    if not raw:
        raise CassetteMiss("LLM_CASSETTE_DIR is not set; cassettes need a directory.")
    return Path(raw)


def cassette_path(directory: Path, k: str) -> Path:
    return directory / f"{k}.json"


def load(directory: Path, k: str) -> dict | None:
    """The stored cassette for key k, or None (a corrupt file is a miss)."""
    path = cassette_path(directory, k)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("llm_cassette: unreadable cassette %s treated as a miss", path.name)
        return None
    if not isinstance(data, dict) or data.get("key") != k or "result" not in data:
        return None
    return data


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_pacific() -> str:
    return datetime.now(timezone.utc).astimezone(PACIFIC).isoformat(timespec="seconds")


def _index(directory: Path, settings: Any, event: str, k: str, feature: str, provider: str, model: str, mode: str) -> None:
    sweep = (getattr(settings, "eval_sweep_id", "") or "").strip() or "no_sweep"
    line = json.dumps(
        {
            "at": _now_pacific(),
            "event": event,
            "key": k,
            "mode": mode,
            "feature": feature,
            "provider": provider,
            "model": model,
        },
        sort_keys=True,
    )
    try:
        path = directory / "index" / f"{sweep}.jsonl"
        with _index_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        logger.warning("llm_cassette: could not append to the sweep index", exc_info=True)


# ── Tape ─────────────────────────────────────────────────────────────────


@dataclass
class Tape:
    """One call's cassette handle. hit: `result` and `usage` are the
    recorded ones. Otherwise `record(result, usage)` stores the live
    answer when the mode records (a no-op for off)."""

    mode: str = "off"
    hit: bool = False
    key: str | None = None
    result: Any = None
    usage: Any = None
    feature: str = ""
    provider: str = ""
    model: str = ""
    fp: dict | None = None
    directory: Path | None = None
    settings: Any = None

    def record(self, result: Any, usage: Any) -> None:
        """Store a live answer (record and record_missing). Never raises:
        a failed recording must not fail the call that already billed."""
        if self.hit or self.mode not in ("record", "record_missing") or self.directory is None or not self.key:
            return
        try:
            if usage is None:
                usage = getattr(result, "usage", None)
            entry = {
                "version": CASSETTE_VERSION,
                "key": self.key,
                "recorded_at": _now_pacific(),
                "feature": self.feature,
                "provider": self.provider,
                "model": self.model,
                "usage": _usage_dict(usage),
                "result": encode_result(result),
                "fingerprint": self.fp,
            }
            payload = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            _atomic_write(cassette_path(self.directory, self.key), payload)
            _index(self.directory, self.settings, "record", self.key, self.feature, self.provider, self.model, self.mode)
        except Exception:  # noqa: BLE001 - see docstring
            logger.warning("llm_cassette: recording %s failed", (self.key or "")[:12], exc_info=True)


OFF_TAPE = Tape()


def mode_of(settings: Any) -> str:
    mode = (getattr(settings, "llm_cassette_mode", "off") or "off").strip().lower()
    return mode if mode in MODES else "off"


def begin(feature: str, route: Any, settings: Any, request: Any) -> Tape:
    """Open the tape of one provider call (the cassette hook of
    llm._guarded). Returns OFF_TAPE when the mode is off; a hit tape in
    replay and record_missing when the request was recorded; raises
    CassetteMiss in replay mode on a miss (and in production for any mode
    but off)."""
    mode = mode_of(settings)
    if mode == "off":
        return OFF_TAPE
    provider = str(getattr(route, "provider", "") or "")
    model = str(getattr(route, "model", "") or "")
    if getattr(settings, "is_production", False):
        raise CassetteMiss("LLM cassettes are dev only; LLM_CASSETTE_MODE must be off in production.")
    if request is None:
        if mode == "replay":
            raise CassetteMiss(f"The {feature} call has no request fingerprint, so replay cannot serve it.")
        return Tape(mode="off")
    directory = _dir(settings)
    fp = fingerprint(feature, provider, model, request)
    k = key(fp)
    if mode in ("replay", "record_missing"):
        entry = load(directory, k)
        if entry is not None:
            usage = _cassette_usage(entry.get("usage"))
            result = decode_result(entry["result"], usage)
            _index(directory, settings, "hit", k, feature, provider, model, mode)
            return Tape(mode=mode, hit=True, key=k, result=result, usage=usage, feature=feature,
                        provider=provider, model=model, directory=directory, settings=settings)
        _index(directory, settings, "miss", k, feature, provider, model, mode)
        if mode == "replay":
            raise CassetteMiss(
                f"No recorded response for this {feature} request (cassette {k[:12]}). Replay mode never "
                "calls the model; record it inside an open sweep with LLM_CASSETTE_MODE=record_missing.",
                key=k,
            )
    return Tape(mode=mode, hit=False, key=k, feature=feature, provider=provider, model=model,
                fp=fp, directory=directory, settings=settings)
