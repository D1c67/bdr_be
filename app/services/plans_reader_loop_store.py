"""Durable rounds and tool calls of the AI Estimating tool loop (WS2 wave 2;
docs/AI_ESTIMATING_BUILD_SPEC.md 4.3 and 5.6 MC tables).

The loop (services/plans_reader_toolloop) never touches the database
itself: it talks to one small store with two implementations behind the
same methods, `SupabaseLoopStore` over the service-role client and
`FakeLoopStore` in memory for tests, replay and offline eval. Every method
is idempotent where the loop needs it to be:

- `insert_round` returns the existing `sent` row for the same key and
  round number instead of a second one (the partial unique index of
  migration 0165 would refuse it anyway), so a resumed loop finds the
  round it was in the middle of.
- `insert_tool_call` returns the stored row for a (key, call_id) that
  already exists. The loop inserts a call's row as PENDING (result_data
  `{"pending": true}`, no result_text) before it runs the handler and
  fills it with `finalize_tool_call` after, so a crash between the two
  leaves a pending row: the resumed loop re-runs a read tool (pure, $0)
  and never re-runs a write or review tool (it answers "interrupted").
- `record_call` writes one plans_reader_calls row per (key, round_no,
  attempt) and skips when that row exists, so the same round is never
  billed twice; an orphaned attempt is billed once at its estimate under
  its own attempt number.

Per-attempt keys (WS7 second sample): one group can run a second loop
under `loop_key(group_id=..., attempt=2)` ("group:<id>#a2"). The MC tables
key rows on coalesce(group_id, message_id) with round_no and call_id, so an
attempt's rows keep the group's uuid and are partitioned instead: round
numbers are stored at `(attempt - 1) * ATTEMPT_ROUND_BASE + round_no`
(round 0, the pre-call stage, included) and call ids are stored with the
prefix "a<attempt>:". The store translates both ways, so the loop sees
rounds 0, 1, 2... and its own call ids for every key. A plain key (attempt
1) is unchanged: same strings, same columns, same round numbers; its reads
ignore rows at or above ATTEMPT_ROUND_BASE.

Rows written to the two MC tables omit every key whose value is None
(the spec 5.6 rule for columns that may not exist yet), and nothing here
needs the tables at import time. Dates shown to people are formatted by
the router (America/Los_Angeles); this module stores UTC ISO strings.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.services.plans_reader_budgets import budget_key

logger = logging.getLogger(__name__)

ROUNDS_TABLE = "plans_reader_loop_rounds"
TOOL_CALLS_TABLE = "plans_reader_tool_calls"
CALLS_TABLE = "plans_reader_calls"
GROUPS_TABLE = "plans_reader_groups"

ROUND_STATUSES: tuple[str, ...] = ("sent", "done", "failed", "abandoned")
# A tool call row written before its handler ran (no status column in MC:
# the marker lives in result_data and result_text stays null).
PENDING_RESULT: dict[str, Any] = {"pending": True}


def is_pending(row: dict[str, Any] | None) -> bool:
    """True for a tool call row inserted before its handler ran and never
    finalized."""
    if not row:
        return False
    data = row.get("result_data")
    return isinstance(data, dict) and data.get("pending") is True and row.get("result_text") is None
# The deterministic pre-call stage stores its tool calls under round 0.
PRECALL_ROUND = 0
# Per-attempt keys: stored round numbers of attempt n start at
# (n - 1) * ATTEMPT_ROUND_BASE and stored call ids carry "a<n>:".
ATTEMPT_ROUND_BASE = 1000
ATTEMPT_SEP = "#a"
# plans_reader_groups.loop_state keys the loop does not own (the recount
# targets WS7 writes at insert time): kept when the loop saves its state.
PRESERVED_LOOP_STATE_KEYS: tuple[str, ...] = ("recount",)
_PAGE = 1000
_MAX_PAGES = 20


class StoreError(RuntimeError):
    """A store write failed in a way the loop cannot settle itself."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """The row without its None values (spec 5.6: new columns are omitted
    from inserts when the value is null)."""
    return {k: v for k, v in row.items() if v is not None}


def split_key(key: str) -> tuple[str, int]:
    """A loop key -> (base budget_key, attempt). Plain keys are attempt 1."""
    base, sep, tail = key.rpartition(ATTEMPT_SEP)
    if sep and base and tail.isdigit() and int(tail) >= 2:
        return base, int(tail)
    return key, 1


def key_attempt(key: str) -> int:
    return split_key(key)[1]


def round_offset(key: str) -> int:
    """Stored round number minus the loop's round number for this key."""
    return (key_attempt(key) - 1) * ATTEMPT_ROUND_BASE


def stored_call_id(key: str, call_id: str) -> str:
    attempt = key_attempt(key)
    return f"a{attempt}:{call_id}" if attempt > 1 else call_id


def loop_call_id(key: str, call_id: Any) -> str:
    """The stored call id as the loop knows it (prefix removed)."""
    text = str(call_id)
    attempt = key_attempt(key)
    prefix = f"a{attempt}:"
    return text[len(prefix):] if attempt > 1 and text.startswith(prefix) else text


def _in_attempt(key: str, stored_round_no: Any) -> bool:
    off = round_offset(key)
    n = int(stored_round_no or 0)
    return off <= n < off + ATTEMPT_ROUND_BASE


def _to_loop(key: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored row as the loop sees it (round number and call id of its
    own attempt). Attempt 1 rows come back unchanged."""
    if row is None or key_attempt(key) == 1:
        return row
    out = dict(row)
    if out.get("round_no") is not None:
        out["round_no"] = int(out["round_no"]) - round_offset(key)
    if out.get("call_id") is not None:
        out["call_id"] = loop_call_id(key, out["call_id"])
    return out


def key_columns(key: str) -> dict[str, str]:
    """`budget_key` string (with or without an attempt suffix) ->
    {group_id: ...} or {message_id: ...}."""
    kind, _, value = split_key(key)[0].partition(":")
    if kind == "group":
        return {"group_id": value}
    if kind == "message":
        return {"message_id": value}
    raise ValueError(f"unknown loop key {key!r}")


def loop_key(*, group_id: str | None = None, message_id: str | None = None, attempt: int | None = None) -> str:
    """The loop's store key. `attempt` >= 2 adds the per-attempt suffix
    ("group:<id>#a2"); None or 1 gives the plain budget_key unchanged."""
    base = budget_key(group_id=group_id, message_id=message_id)
    if attempt is None or int(attempt) <= 1:
        return base
    return f"{base}{ATTEMPT_SEP}{int(attempt)}"


# ---------------------------------------------------------------------------
# Supabase store
# ---------------------------------------------------------------------------


class SupabaseLoopStore:
    """Rounds, tool calls and call rows over the service-role client.
    `run_id` is stamped on every row; the loop key decides group_id or
    message_id."""

    def __init__(self, sb: Any, *, run_id: str) -> None:
        self.sb = sb
        self.run_id = run_id

    # -- reads --------------------------------------------------------------

    def _scoped(self, table: str, key: str, columns: str = "*") -> Any:
        query = self.sb.table(table).select(columns).eq("run_id", self.run_id)
        for col, val in key_columns(key).items():
            query = query.eq(col, val)
        return query

    def _all(self, query: Any, order: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(_MAX_PAGES):
            start = page * _PAGE
            chunk = query.order(order).range(start, start + _PAGE - 1).execute().data or []
            out.extend(chunk)
            if len(chunk) < _PAGE:
                return out
        logger.error("plans_reader_loop_store: more than %d rows; truncated", _PAGE * _MAX_PAGES)
        return out

    def rounds(self, key: str) -> list[dict[str, Any]]:
        """Every round row of the key (abandoned ones included), ordered by
        round number then creation."""
        rows = self._all(self._scoped(ROUNDS_TABLE, key), "created_at")
        rows = [_to_loop(key, r) for r in rows if _in_attempt(key, r.get("round_no"))]
        return sorted(rows, key=lambda r: (int(r.get("round_no") or 0), str(r.get("created_at") or "")))

    def tool_calls(self, key: str) -> list[dict[str, Any]]:
        rows = self._all(self._scoped(TOOL_CALLS_TABLE, key), "created_at")
        rows = [_to_loop(key, r) for r in rows if _in_attempt(key, r.get("round_no"))]
        return sorted(rows, key=lambda r: (int(r.get("round_no") or 0), str(r.get("created_at") or "")))

    def get_tool_call(self, key: str, call_id: str) -> dict[str, Any] | None:
        query = self._scoped(TOOL_CALLS_TABLE, key).eq("call_id", stored_call_id(key, call_id))
        rows = query.limit(1).execute().data or []
        return _to_loop(key, rows[0]) if rows else None

    # -- writes -------------------------------------------------------------

    def insert_round(self, key: str, round_no: int, *, request_sha256: str, est_usd: Decimal) -> dict[str, Any]:
        """A `sent` round row; the existing sent row of the same round is
        returned instead of inserting a second one."""
        existing = [
            r for r in self.rounds(key) if int(r.get("round_no") or -1) == round_no and r.get("status") == "sent"
        ]
        if existing:
            return existing[0]
        row = _compact(
            {
                "run_id": self.run_id,
                **key_columns(key),
                "round_no": round_no + round_offset(key),
                "status": "sent",
                "request_sha256": request_sha256,
                "est_usd": _float(est_usd),
            }
        )
        data = self.sb.table(ROUNDS_TABLE).insert(row).execute().data or []
        if not data:
            raise StoreError("the loop round row was not written")
        return _to_loop(key, data[0])

    def update_round(self, round_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        payload = _compact({**fields, "updated_at": _now_iso()})
        if "cost_usd" in payload:
            payload["cost_usd"] = _float(payload["cost_usd"])
        data = self.sb.table(ROUNDS_TABLE).update(payload).eq("id", round_id).execute().data or []
        return data[0] if data else None

    def insert_tool_call(self, key: str, round_no: int, call: dict[str, Any]) -> dict[str, Any]:
        """Store one tool call result; the stored row wins when the call id
        is already there."""
        call_id = str(call["call_id"])
        existing = self.get_tool_call(key, call_id)
        if existing is not None:
            return existing
        row = _compact(
            {
                "run_id": self.run_id,
                **key_columns(key),
                "round_no": round_no + round_offset(key),
                "call_id": stored_call_id(key, call_id),
                "tool_name": str(call.get("tool_name") or ""),
                "args": call.get("args"),
                "result_text": call.get("result_text"),
                "result_data": call.get("result_data"),
                "citations": call.get("citations"),
                "views": call.get("views"),
                "refused": bool(call.get("refused", False)),
                "duration_ms": call.get("duration_ms"),
            }
        )
        try:
            data = self.sb.table(TOOL_CALLS_TABLE).insert(row).execute().data or []
        except Exception as exc:  # noqa: BLE001 - a unique violation means another attempt stored it
            again = self.get_tool_call(key, call_id)
            if again is not None:
                return again
            raise StoreError(f"the tool call row for {call_id} was not written") from exc
        return _to_loop(key, data[0] if data else row)

    def finalize_tool_call(self, key: str, call_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Fill a pending tool call row with its result (result_text,
        result_data, citations, views, refused, duration_ms)."""
        allowed = ("result_text", "result_data", "citations", "views", "refused", "duration_ms")
        payload = _compact({k: fields.get(k) for k in allowed})
        query = self.sb.table(TOOL_CALLS_TABLE).update(payload).eq("run_id", self.run_id)
        for col, val in key_columns(key).items():
            query = query.eq(col, val)
        data = query.eq("call_id", stored_call_id(key, call_id)).execute().data or []
        return _to_loop(key, data[0]) if data else None

    def has_call_row(self, key: str, round_no: int, attempt: int) -> bool:
        cols = key_columns(key)
        col, val = next(iter(cols.items()))
        rows = (
            self.sb.table(CALLS_TABLE)
            .select("id, request_snapshot")
            .eq("run_id", self.run_id)
            .eq(col, val)
            .eq("round_no", round_no + round_offset(key))
            .execute()
            .data
            or []
        )
        for r in rows:
            snap = r.get("request_snapshot") or {}
            if int((snap or {}).get("loop_attempt") or 1) == attempt:
                return True
        return False

    def record_call(self, key: str, round_no: int, attempt: int, row: dict[str, Any]) -> bool:
        """Insert a plans_reader_calls row for one round attempt unless one
        exists. Returns True when a row was written. A failed insert is
        logged, never raised: the call already happened."""
        try:
            if self.has_call_row(key, round_no, attempt):
                return False
        except Exception:  # noqa: BLE001 - a read failure must not block the loop
            logger.exception("plans_reader_loop_store: could not check the call row")
        payload = _compact(
            {**row, "run_id": self.run_id, **key_columns(key), "round_no": round_no + round_offset(key)}
        )
        snap = dict(payload.get("request_snapshot") or {})
        snap["loop_attempt"] = attempt
        if key_attempt(key) > 1:
            snap["loop_key_attempt"] = key_attempt(key)
        payload["request_snapshot"] = snap
        if "cost_usd" in payload:
            payload["cost_usd"] = _float(payload["cost_usd"])
        if payload.get("conservative_cost") is False:
            payload.pop("conservative_cost")
        try:
            self.sb.table(CALLS_TABLE).insert(payload).execute()
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception("plans_reader_loop_store: could not record the loop call for %s", key)
            return False
        return True

    def _current_loop_state(self, group_id: str) -> dict[str, Any]:
        rows = self.sb.table(GROUPS_TABLE).select("loop_state").eq("id", group_id).limit(1).execute().data or []
        state = rows[0].get("loop_state") if rows else None
        return dict(state) if isinstance(state, dict) else {}

    def save_loop_state(self, key: str, state: dict[str, Any]) -> None:
        """plans_reader_groups.loop_state (groups only; chat messages have no
        column). Best effort: a failure is logged. Keys the loop does not
        own (PRESERVED_LOOP_STATE_KEYS, e.g. the recount targets, and the
        state of other attempts) are kept; an attempt >= 2 writes its state
        under "attempt_<n>" and leaves the top level alone."""
        cols = key_columns(key)
        group_id = cols.get("group_id")
        if not group_id:
            return
        try:
            try:
                current = self._current_loop_state(group_id)
            except Exception:  # noqa: BLE001 - a failed read keeps today's overwrite
                logger.exception("plans_reader_loop_store: could not read loop_state for %s", group_id)
                current = {}
            attempt = key_attempt(key)
            if attempt > 1:
                payload = {**current, f"attempt_{attempt}": dict(state)}
            else:
                payload = dict(state)
                for k, v in current.items():
                    if (k in PRESERVED_LOOP_STATE_KEYS or k.startswith("attempt_")) and k not in payload:
                        payload[k] = v
            self.sb.table(GROUPS_TABLE).update({"loop_state": payload}).eq("id", group_id).execute()
        except Exception:  # noqa: BLE001 - informative column only
            logger.exception("plans_reader_loop_store: could not save loop_state for %s", group_id)


# ---------------------------------------------------------------------------
# Fake store
# ---------------------------------------------------------------------------


class Killed(BaseException):
    """Raised by FakeLoopStore when its kill switch fires (a simulated
    process death right before a write). A BaseException so no
    `except Exception` in the loop can swallow it, like a real kill."""


class FakeLoopStore:
    """In-memory store with the same methods. `kill_at` stops the loop with
    `Killed` right before the N-th write (1-based, counting every write
    attempt); `writes` lists every write that happened and
    `duplicate_attempts` counts the inserts that found their row already
    stored (zero means the loop never tried to write the same thing twice)."""

    def __init__(self, *, run_id: str = "run", kill_at: int | None = None) -> None:
        self.run_id = run_id
        self.kill_at = kill_at
        self.write_count = 0
        self.writes: list[tuple[str, Any]] = []
        self.duplicate_attempts = 0
        self._rounds: list[dict[str, Any]] = []
        self._tool_calls: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.loop_states: dict[str, dict[str, Any]] = {}
        self._clock = 0

    # -- kill switch --------------------------------------------------------

    def _write(self, label: str, payload: Any) -> None:
        self.write_count += 1
        if self.kill_at is not None and self.write_count >= self.kill_at:
            self.kill_at = None  # fire once; the resumed loop runs through
            raise Killed(f"killed before write {self.write_count} ({label})")
        self.writes.append((label, payload))

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-10-21T12:00:00.{self._clock:06d}+00:00"

    # -- reads --------------------------------------------------------------

    def rounds(self, key: str) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._rounds if r["_key"] == key]
        return sorted(rows, key=lambda r: (r["round_no"], r["created_at"]))

    def tool_calls(self, key: str) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._tool_calls if r["_key"] == key]
        return sorted(rows, key=lambda r: (r["round_no"], r["created_at"]))

    def get_tool_call(self, key: str, call_id: str) -> dict[str, Any] | None:
        for r in self._tool_calls:
            if r["_key"] == key and r["call_id"] == call_id:
                return dict(r)
        return None

    # -- writes -------------------------------------------------------------

    def insert_round(self, key: str, round_no: int, *, request_sha256: str, est_usd: Decimal) -> dict[str, Any]:
        for r in self._rounds:
            if r["_key"] == key and r["round_no"] == round_no and r["status"] == "sent":
                self.duplicate_attempts += 1
                return dict(r)
        row = {
            "id": str(uuid.uuid4()),
            "_key": key,
            "run_id": self.run_id,
            **key_columns(key),
            "round_no": round_no,
            "status": "sent",
            "request_sha256": request_sha256,
            "est_usd": _float(est_usd),
            "created_at": self._stamp(),
        }
        self._write("round", {"round_no": round_no})
        self._rounds.append(row)
        return dict(row)

    def update_round(self, round_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        for r in self._rounds:
            if r["id"] == round_id:
                self._write("round_update", {"round_no": r["round_no"], "status": fields.get("status")})
                payload = _compact(dict(fields))
                if "cost_usd" in payload:
                    payload["cost_usd"] = _float(payload["cost_usd"])
                r.update(payload)
                r["updated_at"] = self._stamp()
                return dict(r)
        return None

    def insert_tool_call(self, key: str, round_no: int, call: dict[str, Any]) -> dict[str, Any]:
        call_id = str(call["call_id"])
        existing = self.get_tool_call(key, call_id)
        if existing is not None:
            self.duplicate_attempts += 1
            return existing
        row = {
            "id": str(uuid.uuid4()),
            "_key": key,
            "run_id": self.run_id,
            **key_columns(key),
            "round_no": round_no,
            "call_id": call_id,
            "tool_name": str(call.get("tool_name") or ""),
            "args": call.get("args"),
            "result_text": call.get("result_text"),
            "result_data": call.get("result_data"),
            "citations": call.get("citations"),
            "views": call.get("views"),
            "refused": bool(call.get("refused", False)),
            "duration_ms": call.get("duration_ms"),
            "created_at": self._stamp(),
        }
        self._write("tool_call", {"round_no": round_no, "call_id": call_id})
        self._tool_calls.append(row)
        return dict(row)

    def finalize_tool_call(self, key: str, call_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        allowed = ("result_text", "result_data", "citations", "views", "refused", "duration_ms")
        for r in self._tool_calls:
            if r["_key"] == key and r["call_id"] == call_id:
                self._write("tool_call_update", {"round_no": r["round_no"], "call_id": call_id})
                r.update({k: fields.get(k) for k in allowed if k in fields})
                return dict(r)
        return None

    def has_call_row(self, key: str, round_no: int, attempt: int) -> bool:
        return any(
            c["_key"] == key and c["round_no"] == round_no and c["attempt"] == attempt for c in self.calls
        )

    def record_call(self, key: str, round_no: int, attempt: int, row: dict[str, Any]) -> bool:
        if self.has_call_row(key, round_no, attempt):
            self.duplicate_attempts += 1
            return False
        self._write("call", {"round_no": round_no, "attempt": attempt})
        payload = {**_compact(dict(row)), "_key": key, **key_columns(key), "round_no": round_no, "attempt": attempt}
        if "cost_usd" in payload:
            payload["cost_usd"] = _float(payload["cost_usd"])
        self.calls.append(payload)
        return True

    def save_loop_state(self, key: str, state: dict[str, Any]) -> None:
        self._write("loop_state", {"round_no": state.get("round_no")})
        self.loop_states[key] = dict(state)

    # -- assertions ---------------------------------------------------------

    def done_rounds(self, key: str) -> list[dict[str, Any]]:
        return [r for r in self.rounds(key) if r["status"] == "done"]

    def duplicate_done_rounds(self, key: str) -> int:
        seen: dict[int, int] = {}
        for r in self.done_rounds(key):
            seen[r["round_no"]] = seen.get(r["round_no"], 0) + 1
        return sum(n - 1 for n in seen.values() if n > 1)


__all__ = [
    "ATTEMPT_ROUND_BASE",
    "CALLS_TABLE",
    "FakeLoopStore",
    "GROUPS_TABLE",
    "Killed",
    "PRECALL_ROUND",
    "ROUNDS_TABLE",
    "ROUND_STATUSES",
    "StoreError",
    "SupabaseLoopStore",
    "PENDING_RESULT",
    "TOOL_CALLS_TABLE",
    "is_pending",
    "key_attempt",
    "key_columns",
    "loop_call_id",
    "loop_key",
    "round_offset",
    "split_key",
    "stored_call_id",
]
