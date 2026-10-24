"""The $400 live spend ledger of the AI Estimating autonomous build
(docs/AI_ESTIMATING_BUILD_SPEC.md 0.4, WS0 step 10, migration MA).

Dev only. Every live model call of the build happens inside an open sweep
(`scripts/eval_ledger.py open ...`); the guard below is what makes that
true. It runs inside `llm._guarded` for every provider call made in a
process whose AI_EVAL_LEDGER_ENFORCE is on (the sweep backend and the live
eval scripts), and refuses with EvalBudgetRefused before anything is sent:

- `unpriced_model`: the route's model has no verified llm_pricing row;
- `not_dev`: the process is not pointed at the dev BDR project;
- `no_open_sweep`: no sweep is open (or EVAL_SWEEP_ID names one that is not);
- `global_ceiling`: the build's spend plus what is in flight plus this
  call's reserve would pass ai_eval_budget.ceiling_usd ($400);
- `allocation`: the same against the sweep's workstream allocation;
- `sweep_budget`: the same against the sweep's own budget.

In-flight reservation. Admission happens under a process lock: a call is
admitted when spent + inflight + reserve <= limit for every limit, and its
reserve is added to `inflight` until the call ends (released in a finally,
after the call's llm_call_log row is written). The reservation is
in-process, which is why spec 0.4 rule 2 allows one live process per open
sweep.

Reserve = the request-derived maximum: estimated input tokens x the input
price (the whole request at the long-context multiplier when it is past the
row's threshold, through llm_pricing.estimate_cost) plus max_tokens x the
output price. A caller that already knows its estimate (plans_reader_agents
`_call_model` has est_usd, the tool loop has the round estimate) sets it
with `set_call_estimate(usd)` right before the call; the guard takes it
once. Otherwise the guard derives it from the request.

Attribution. Spend is the sum of `llm_call_log.cost_usd` for the sweep:
`llm_gate.log_call` stamps `eval_sweep_id` (EVAL_SWEEP_ID, else the sweep
the guard admitted the call under, see admitted_sweep_id). The process also
keeps what it committed itself, so a stale cached read or a dropped log row
never under-counts: a sweep's spend is max(database sum, the database sum
seen at the process's first call + what this process committed since).
Closed and aborted sweeps freeze `spent_usd` on the sweep row, so pruning
llm_call_log never changes the ledger. Global spend is the sum over every
sweep opened since `ai_eval_budget.epoch_at`; a workstream's spend is the
same over its sweeps.

Storage. The ai_eval_* tables through the Supabase client
(SupabaseLedgerStore, sync: never call it inside `async def`), or an
in-memory store (MemoryLedgerStore) that tests install with use_store().
Both refuse to run against anything but the dev project (check_dev_url).

`ai_eval_budget.enforce` is informational: it records that the operator
declared the ledger armed on the sweep backend (spec 0.4 rule 1). The guard
is switched by the process setting, never by the row.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import dataclasses
import logging
import math
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.services import llm_pricing
from app.services.llm_errors import KIND_TIMEOUT, EvalBudgetRefused

logger = logging.getLogger(__name__)

DEV_REF = "bpidntbyvoooqvaispup"
PACIFIC = ZoneInfo("America/Los_Angeles")

REFUSAL_CODES: tuple[str, ...] = (
    "no_open_sweep",
    "sweep_budget",
    "global_ceiling",
    "unpriced_model",
    "allocation",
    "not_dev",
)
PURPOSES: tuple[str, ...] = ("dev_probe", "smoke", "core", "holdout", "noise", "ab")
STATUSES: tuple[str, ...] = ("open", "closed", "aborted")
CEILING_MAX_USD = Decimal("400")
# Spec 0.4 rule 7: past this global spend only smoke or holdout sweeps open.
FINAL_ZONE_USD = Decimal("360")
FINAL_ZONE_PURPOSES: tuple[str, ...] = ("smoke", "holdout")
# Spec 0.4 rule 7: a sweep more than 15% over its budget is a stop condition.
OVERRUN_FRACTION = Decimal("0.15")
# Database reads of the guard are cached this long per process; the
# in-process committed tally covers the calls made meanwhile.
SNAPSHOT_TTL_S = 10.0
# Output tokens assumed when a request names no max_tokens.
DEFAULT_OUTPUT_TOKENS = 32_000
# Section 8.4 allocations (USD), seeded by migration MA.
SEED_ALLOCATIONS: dict[str, Decimal] = {
    "WS2": Decimal("29"),
    "WS3": Decimal("6"),
    "WS4": Decimal("26"),
    "WS5": Decimal("6"),
    "WS6": Decimal("8"),
    "WS7": Decimal("28"),
    "WS8": Decimal("5"),
    "shared": Decimal("278"),
    "contingency": Decimal("14"),
}
CONTINGENCY = "contingency"

_ZERO = Decimal("0")
_QUANT = Decimal("0.000001")
_PAGE = 1000
_MAX_PAGES = 200


class EvalLedgerError(RuntimeError):
    """A ledger management call (open, close, allocate) refused, or the
    store cannot be used (not the dev project, tables missing)."""


def check_dev_url(url: str | None) -> None:
    """Refuse anything but the dev BDR project (never BDR_Prod or staging)."""
    if not url or DEV_REF not in url:
        raise EvalLedgerError(
            f"The eval ledger runs only against the dev project {DEV_REF}; "
            f"SUPABASE_URL is {url or 'empty'}. Refusing."
        )


def _dec(value: Any) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a junk number reads as 0
        return _ZERO


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pacific(value: Any) -> str | None:
    """A timestamp as Pacific time text (every datetime shown is Pacific)."""
    dt = _parse_ts(value)
    if dt is None:
        return None
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M %Z")


# ── Stores ───────────────────────────────────────────────────────────────


class LedgerStore(Protocol):
    def get_budget(self) -> dict: ...
    def list_allocations(self) -> dict[str, dict]: ...
    def set_allocation(self, workstream: str, allocated_usd: Decimal, note: str | None) -> None: ...
    def list_sweeps(self, status: str | None = None) -> list[dict]: ...
    def get_sweep(self, sweep_id: str) -> dict | None: ...
    def insert_sweep(self, row: dict) -> dict: ...
    def update_sweep(self, sweep_id: str, fields: dict) -> dict: ...
    def sweep_call_spend(self, sweep_id: str) -> Decimal: ...


class MemoryLedgerStore:
    """The ledger tables in memory (tests and dry runs). `add_call_cost`
    stands in for an llm_call_log row stamped with a sweep id."""

    def __init__(
        self,
        *,
        ceiling_usd: Decimal | float | int = CEILING_MAX_USD,
        allocations: dict[str, Decimal | float | int] | None = None,
        epoch_at: datetime | None = None,
        enforce: bool = False,
        supabase_url: str | None = f"https://{DEV_REF}.supabase.co",
    ) -> None:
        check_dev_url(supabase_url)
        self._lock = threading.Lock()
        self.budget = {
            "ceiling_usd": _dec(ceiling_usd),
            "epoch_at": epoch_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
            "enforce": enforce,
        }
        source = SEED_ALLOCATIONS if allocations is None else allocations
        self.allocations: dict[str, dict] = {
            ws: {"workstream": ws, "allocated_usd": _dec(v), "note": None} for ws, v in source.items()
        }
        self.sweeps: dict[str, dict] = {}
        self.call_costs: list[tuple[str, Decimal]] = []
        self.reads = 0

    def get_budget(self) -> dict:
        self.reads += 1
        return dict(self.budget)

    def list_allocations(self) -> dict[str, dict]:
        self.reads += 1
        return {k: dict(v) for k, v in self.allocations.items()}

    def set_allocation(self, workstream: str, allocated_usd: Decimal, note: str | None) -> None:
        with self._lock:
            self.allocations[workstream] = {
                "workstream": workstream,
                "allocated_usd": _dec(allocated_usd),
                "note": note,
            }

    def list_sweeps(self, status: str | None = None) -> list[dict]:
        self.reads += 1
        rows = [dict(r) for r in self.sweeps.values()]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        return sorted(rows, key=lambda r: str(r.get("opened_at") or ""))

    def get_sweep(self, sweep_id: str) -> dict | None:
        self.reads += 1
        row = self.sweeps.get(str(sweep_id))
        return dict(row) if row else None

    def insert_sweep(self, row: dict) -> dict:
        with self._lock:
            if row.get("status", "open") == "open" and any(
                r.get("status") == "open" for r in self.sweeps.values()
            ):
                raise EvalLedgerError("Another sweep is already open; close it first.")
            stored = dict(row)
            stored.setdefault("id", str(uuid.uuid4()))
            self.sweeps[stored["id"]] = stored
            return dict(stored)

    def update_sweep(self, sweep_id: str, fields: dict) -> dict:
        with self._lock:
            row = self.sweeps[str(sweep_id)]
            row.update(fields)
            return dict(row)

    def sweep_call_spend(self, sweep_id: str) -> Decimal:
        self.reads += 1
        return sum((c for s, c in self.call_costs if s == str(sweep_id)), _ZERO)

    def add_call_cost(self, sweep_id: str, usd: Decimal | float | int) -> None:
        self.call_costs.append((str(sweep_id), _dec(usd)))


class SupabaseLedgerStore:
    """The ai_eval_* tables (migration MA) and llm_call_log.eval_sweep_id
    (migration MC) through the service-role client. Sync: call it from a
    plain thread, never inside `async def`."""

    def __init__(self, sb: Any, *, supabase_url: str | None = None) -> None:
        check_dev_url(supabase_url if supabase_url is not None else get_settings().supabase_url)
        self.sb = sb

    def get_budget(self) -> dict:
        rows = self.sb.table("ai_eval_budget").select("*").eq("id", 1).execute().data or []
        if not rows:
            raise EvalLedgerError("ai_eval_budget has no row; apply migration MA (0163) on dev.")
        row = rows[0]
        return {
            "ceiling_usd": _dec(row.get("ceiling_usd")),
            "epoch_at": _parse_ts(row.get("epoch_at")),
            "enforce": bool(row.get("enforce")),
        }

    def list_allocations(self) -> dict[str, dict]:
        rows = self.sb.table("ai_eval_allocations").select("*").execute().data or []
        return {
            r["workstream"]: {
                "workstream": r["workstream"],
                "allocated_usd": _dec(r.get("allocated_usd")),
                "note": r.get("note"),
            }
            for r in rows
        }

    def set_allocation(self, workstream: str, allocated_usd: Decimal, note: str | None) -> None:
        self.sb.table("ai_eval_allocations").upsert(
            {
                "workstream": workstream,
                "allocated_usd": float(allocated_usd),
                "note": note,
                "updated_at": _now().isoformat(),
            },
            on_conflict="workstream",
        ).execute()

    def list_sweeps(self, status: str | None = None) -> list[dict]:
        q = self.sb.table("ai_eval_sweeps").select("*")
        if status is not None:
            q = q.eq("status", status)
        return q.order("opened_at").execute().data or []

    def get_sweep(self, sweep_id: str) -> dict | None:
        rows = self.sb.table("ai_eval_sweeps").select("*").eq("id", str(sweep_id)).execute().data or []
        return rows[0] if rows else None

    def insert_sweep(self, row: dict) -> dict:
        body = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}
        try:
            data = self.sb.table("ai_eval_sweeps").insert(body).execute().data or []
        except Exception as exc:  # noqa: BLE001 - the one-open index is the likely cause
            if "ai_eval_sweeps_one_open" in str(exc) or "23505" in str(exc):
                raise EvalLedgerError("Another sweep is already open; close it first.") from exc
            raise
        if not data:
            raise EvalLedgerError("The sweep row was not written.")
        return data[0]

    def update_sweep(self, sweep_id: str, fields: dict) -> dict:
        body = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in fields.items()}
        data = self.sb.table("ai_eval_sweeps").update(body).eq("id", str(sweep_id)).execute().data or []
        if not data:
            raise EvalLedgerError(f"Sweep {sweep_id} was not found.")
        return data[0]

    def sweep_call_spend(self, sweep_id: str) -> Decimal:
        total = _ZERO
        for page in range(_MAX_PAGES):
            start = page * _PAGE
            chunk = (
                self.sb.table("llm_call_log")
                .select("id, cost_usd")
                .eq("eval_sweep_id", str(sweep_id))
                .not_.is_("cost_usd", "null")
                .order("id")
                .range(start, start + _PAGE - 1)
                .execute()
            ).data or []
            for row in chunk:
                total += _dec(row.get("cost_usd"))
            if len(chunk) < _PAGE:
                return total
        logger.error("eval_ledger: more than %d calls in sweep %s; sum truncated", _PAGE * _MAX_PAGES, sweep_id)
        return total


_default_store: LedgerStore | None = None


def use_store(store: LedgerStore | None) -> None:
    """Install the store the guard and the sb-less helpers use (tests).
    None goes back to the Supabase store. Clears the process state."""
    global _default_store
    _default_store = store
    reset()


def _store(sb: Any = None) -> LedgerStore:
    if sb is not None and hasattr(sb, "sweep_call_spend"):
        return sb
    if sb is not None:
        return SupabaseLedgerStore(sb)
    if _default_store is not None:
        return _default_store
    url = get_settings().supabase_url
    check_dev_url(url)  # before any client is built
    from app.core.supabase_client import get_supabase

    return SupabaseLedgerStore(get_supabase(), supabase_url=url)


# ── Process state: in-flight reservations and committed spend ───────────


_lock = threading.Lock()
_inflight: dict[str, Decimal] = {}
_base: dict[str, Decimal] = {}  # database sum seen at the first admission
_committed: dict[str, Decimal] = {}  # actual cost committed by this process
_snap_cache: dict[str, tuple[float, "_Snapshot"]] = {}
_open_cache: tuple[float, str | None] | None = None

_estimate: contextvars.ContextVar[Decimal | None] = contextvars.ContextVar(
    "eval_ledger_estimate", default=None
)
_admitted: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "eval_ledger_admitted_sweep", default=None
)


def reset() -> None:
    """Drop every in-process tally and cache (tests; a fresh process)."""
    global _open_cache
    with _lock:
        _inflight.clear()
        _base.clear()
        _committed.clear()
        _snap_cache.clear()
        _open_cache = None


def _invalidate() -> None:
    """Drop the cached database reads (after a management write); the
    in-flight and committed tallies stay."""
    global _open_cache
    with _lock:
        _snap_cache.clear()
        _open_cache = None


def inflight_usd(sweep_id: str | None = None) -> Decimal:
    """Reserved dollars of calls in flight in this process."""
    with _lock:
        if sweep_id is not None:
            return _inflight.get(str(sweep_id), _ZERO)
        return sum(_inflight.values(), _ZERO)


def set_call_estimate(usd: Decimal | float | int | None) -> contextvars.Token:
    """The reserve of the NEXT guarded call in this context (taken once).
    Callers that know their request's maximum cost set it right before the
    call; None clears it."""
    return _estimate.set(None if usd is None else max(_dec(usd), _ZERO))


def gate_round_hook(ctx: Any) -> None:
    """ai_spend.set_ledger_hook target: the tool loop's gate_round runs it
    before every round with the round's estimate, which becomes the
    reserve of the round's model call. No database read; the refusal
    itself happens in admit(), inside llm._guarded."""
    if getattr(get_settings(), "ai_eval_ledger_enforce", False):
        set_call_estimate(getattr(ctx, "estimated_usd", None))


def _take_estimate() -> Decimal | None:
    value = _estimate.get()
    if value is not None:
        _estimate.set(None)
    return value


def admitted_sweep_id() -> str | None:
    """The sweep the guard admitted the call in flight under (read by
    llm_gate.log_call to stamp eval_sweep_id when EVAL_SWEEP_ID is unset)."""
    return _admitted.get()


def sweep_id_for_log(settings: Any = None) -> str | None:
    """What llm_gate.log_call stamps: EVAL_SWEEP_ID, else the sweep the
    guard admitted the current call under, else None (the key is omitted)."""
    s = settings or get_settings()
    explicit = (getattr(s, "eval_sweep_id", "") or "").strip()
    return explicit or admitted_sweep_id()


# ── Request-derived reserve ──────────────────────────────────────────────


def _image_dims(data: bytes) -> tuple[int, int] | None:
    """(w, h) of a PNG or JPEG from its header, None when unknown."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return int(w), int(h)
            i += 2 + seg
    return None


def _image_tokens(data: bytes, provider: str) -> int:
    from app.services import llm_parts

    spec = llm_parts.image_spec_for("anthropic" if provider == "anthropic" else "openai")
    dims = _image_dims(data)
    if dims is not None:
        return spec.image_tokens(*dims)
    if provider == "anthropic":
        return llm_parts.ANTHROPIC_MAX_IMAGE_TOKENS
    from app.services.plans_reader_images import MAX_PATCHES_ORIGINAL, TOKENS_PER_PATCH

    return math.ceil(MAX_PATCHES_ORIGINAL * TOKENS_PER_PATCH)


def _blob_tokens(data: bytes, media_type: str, provider: str) -> int:
    if media_type.startswith("image/") or _image_dims(data) is not None:
        return _image_tokens(data, provider)
    return math.ceil(len(data) / 3)


def _walk_tokens(obj: Any, provider: str, depth: int = 0) -> int:
    """Conservative input tokens of an arbitrary request value: text at 3
    chars per token, images by their pixel size, other binaries by bytes."""
    if depth > 40 or obj is None or isinstance(obj, (bool, int, float, Decimal)):
        return 0
    if isinstance(obj, str):
        if obj.startswith("data:") and ";base64," in obj[:200]:
            head, _, b64 = obj.partition(",")
            media_type = head[5:].split(";")[0]
            try:
                raw = base64.b64decode(b64, validate=False)
            except (binascii.Error, ValueError):
                return math.ceil(len(obj) / 3)
            return _blob_tokens(raw, media_type, provider)
        return math.ceil(len(obj) / 3)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return _blob_tokens(bytes(obj), "", provider)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        data = getattr(obj, "png", None) or getattr(obj, "data", None)
        if isinstance(data, (bytes, bytearray)):
            media = str(getattr(obj, "media_type", "") or "")
            return _blob_tokens(bytes(data), media, provider)
        return sum(
            _walk_tokens(getattr(obj, f.name), provider, depth + 1) for f in dataclasses.fields(obj)
        )
    if isinstance(obj, dict):
        return sum(_walk_tokens(v, provider, depth + 1) for v in obj.values())
    if isinstance(obj, (list, tuple, set, frozenset)):
        return sum(_walk_tokens(v, provider, depth + 1) for v in obj)
    return 0


def _request_output_tokens(request: Any) -> int:
    if isinstance(request, dict):
        req = request.get("req")
        if req is not None and getattr(req, "max_tokens", None):
            return int(req.max_tokens)
        for key in ("max_tokens", "max_output_tokens"):
            value = request.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return DEFAULT_OUTPUT_TOKENS


def request_tokens(request: Any, provider: str) -> tuple[int, int]:
    """(input token estimate, max output tokens) of a guarded request."""
    payload = request
    if isinstance(request, dict) and request.get("req") is not None:
        req = request["req"]
        payload = {
            "system": getattr(req, "system", ""),
            "messages": getattr(req, "messages", ()),
            "tools": [
                {"name": t.name, "description": t.description, "schema": t.schema}
                for t in getattr(req, "tools", ())
            ],
            "schema": getattr(req, "response_schema", None),
        }
    return _walk_tokens(payload, provider), _request_output_tokens(request)


def _schema_tokens(obj: Any) -> int:
    import json

    try:
        return math.ceil(len(json.dumps(obj, default=str)) / 3)
    except (TypeError, ValueError):
        return 0


def estimate_request_usd(model: str, request: Any, provider: str = "openai") -> Decimal | None:
    """The request-derived maximum cost: estimated input at the input
    price (long-context multiplier applied by llm_pricing) plus max_tokens
    at the output price. None for an unpriced model."""
    input_tokens, output_tokens = request_tokens(request, provider)
    if isinstance(request, dict):
        # JSON schemas and tool schemas count as text even when nested as dicts.
        for key in ("schema", "text", "tools"):
            if key in request and isinstance(request[key], (dict, list)):
                input_tokens += _schema_tokens(request[key])
    return llm_pricing.estimate_cost(model, input_tokens=input_tokens, output_tokens=output_tokens)


# ── Guard ────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _Snapshot:
    sweep: dict
    ceiling_usd: Decimal
    allocated_usd: Decimal | None
    sweep_db: Decimal
    global_db: Decimal
    workstream_db: Decimal


class Ticket:
    """One admitted call. release(cost) frees the reservation and commits
    the actual cost to the process tally; idempotent, never raises."""

    __slots__ = ("sweep_id", "reserve", "_token", "_done")

    def __init__(self, sweep_id: str | None, reserve: Decimal, token: contextvars.Token | None) -> None:
        self.sweep_id = sweep_id
        self.reserve = reserve
        self._token = token
        self._done = False

    def release(self, cost: Decimal | None = None, error: BaseException | None = None) -> None:
        if self._done:
            return
        self._done = True
        try:
            if self.sweep_id is None:
                return
            commit = _dec(cost) if cost is not None else _ZERO
            if cost is None and error is not None:
                # A timeout without a usage report was probably billed:
                # commit the reserve (conservative, never under-counts).
                from app.services import llm_errors

                try:
                    if llm_errors.classify(error) == KIND_TIMEOUT:  # type: ignore[arg-type]
                        commit = self.reserve
                except Exception:  # noqa: BLE001
                    commit = self.reserve
            with _lock:
                left = _inflight.get(self.sweep_id, _ZERO) - self.reserve
                _inflight[self.sweep_id] = left if left > _ZERO else _ZERO
                _committed[self.sweep_id] = _committed.get(self.sweep_id, _ZERO) + commit
        except Exception:  # noqa: BLE001 - accounting must never break the call
            logger.exception("eval_ledger: releasing a reservation failed")
        finally:
            if self._token is not None:
                try:
                    _admitted.reset(self._token)
                except ValueError:  # reset from another context
                    _admitted.set(None)


NO_TICKET = Ticket(None, _ZERO, None)


def _refuse(code: str, message: str) -> EvalBudgetRefused:
    return EvalBudgetRefused(message, code=code)


def _open_sweep_id(store: LedgerStore) -> str | None:
    global _open_cache
    now = time.monotonic()
    cached = _open_cache
    if cached is not None and now - cached[0] < SNAPSHOT_TTL_S:
        return cached[1]
    rows = store.list_sweeps("open")
    sid = str(rows[0]["id"]) if rows else None
    _open_cache = (now, sid)
    return sid


def current_sweep_id(settings: Any = None, store: LedgerStore | None = None) -> str | None:
    """EVAL_SWEEP_ID, else the single open sweep (spec 0.4 rule 2)."""
    s = settings or get_settings()
    explicit = (getattr(s, "eval_sweep_id", "") or "").strip()
    if explicit:
        return explicit
    return _open_sweep_id(store or _store())


def _since_epoch(rows: Iterable[dict], epoch: datetime | None) -> list[dict]:
    if epoch is None:
        return list(rows)
    out = []
    for r in rows:
        opened = _parse_ts(r.get("opened_at"))
        if opened is None or opened >= epoch:
            out.append(r)
    return out


def _frozen_or_live(store: LedgerStore, row: dict) -> Decimal:
    if row.get("status") != "open" and row.get("spent_usd") is not None:
        return _dec(row.get("spent_usd"))
    return store.sweep_call_spend(str(row["id"]))


def _snapshot(store: LedgerStore, sweep_id: str) -> _Snapshot | None:
    now = time.monotonic()
    cached = _snap_cache.get(sweep_id)
    if cached is not None and now - cached[0] < SNAPSHOT_TTL_S:
        return cached[1]
    sweep = store.get_sweep(sweep_id)
    if sweep is None:
        return None
    budget = store.get_budget()
    allocations = store.list_allocations()
    rows = _since_epoch(store.list_sweeps(), budget.get("epoch_at"))
    sweep_db = store.sweep_call_spend(sweep_id)
    global_db = _ZERO
    ws_db = _ZERO
    for r in rows:
        spent = sweep_db if str(r["id"]) == sweep_id else _frozen_or_live(store, r)
        global_db += spent
        if r.get("workstream") == sweep.get("workstream"):
            ws_db += spent
    if not any(str(r["id"]) == sweep_id for r in rows):
        global_db += sweep_db
        ws_db += sweep_db
    alloc = allocations.get(str(sweep.get("workstream")))
    snap = _Snapshot(
        sweep=sweep,
        ceiling_usd=min(_dec(budget.get("ceiling_usd")), CEILING_MAX_USD),
        allocated_usd=_dec(alloc["allocated_usd"]) if alloc else None,
        sweep_db=sweep_db,
        global_db=global_db,
        workstream_db=ws_db,
    )
    _snap_cache[sweep_id] = (now, snap)
    return snap


def _money(v: Decimal) -> str:
    return f"${v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def admit(feature: str, route: Any, settings: Any = None, request: Any = None) -> Ticket:
    """The guard `llm._guarded` runs before every provider call. Returns
    NO_TICKET when AI_EVAL_LEDGER_ENFORCE is off or the route is
    self-hosted (no per-token price); otherwise reserves the call's
    maximum cost under the open sweep or raises EvalBudgetRefused. The
    caller releases the ticket after the call's log row is written."""
    s = settings or get_settings()
    if not getattr(s, "ai_eval_ledger_enforce", False):
        return NO_TICKET
    provider = getattr(route, "provider", "openai")
    if provider == "self_hosted":
        return NO_TICKET
    model = getattr(route, "model", "") or ""
    explicit = _take_estimate()
    if getattr(s, "is_production", False):
        raise _refuse("not_dev", "The eval ledger never runs in production; AI_EVAL_LEDGER_ENFORCE must be off.")
    if llm_pricing.price_for(model) is None:
        raise _refuse(
            "unpriced_model",
            f"The eval ledger refused a call to {model or 'an unnamed model'} ({feature}): it has no "
            "verified price row. Add one to llm_pricing.PRICES before any live eval call.",
        )
    try:
        store = _store()
    except EvalLedgerError as exc:
        raise _refuse("not_dev", str(exc)) from exc
    sweep_id = current_sweep_id(s, store)
    if not sweep_id:
        raise _refuse(
            "no_open_sweep",
            f"The eval ledger refused a live call ({feature}): no sweep is open. Open one with "
            "scripts/eval_ledger.py open --workstream WSn --purpose ... --budget ... first.",
        )
    snap = _snapshot(store, sweep_id)
    if snap is None or snap.sweep.get("status") != "open":
        raise _refuse(
            "no_open_sweep",
            f"The eval ledger refused a live call ({feature}): sweep {sweep_id} is not open.",
        )
    reserve = explicit if explicit is not None else estimate_request_usd(model, request, provider)
    reserve = _dec(reserve)
    with _lock:
        if sweep_id not in _base:
            _base[sweep_id] = snap.sweep_db
            _committed.setdefault(sweep_id, _ZERO)
        sweep_spent = max(snap.sweep_db, _base[sweep_id] + _committed.get(sweep_id, _ZERO))
        delta = sweep_spent - snap.sweep_db
        inflight = sum(_inflight.values(), _ZERO)
        global_spent = snap.global_db + delta
        ws_spent = snap.workstream_db + delta
        sweep_inflight = _inflight.get(sweep_id, _ZERO)
        if global_spent + inflight + reserve > snap.ceiling_usd:
            raise _refuse(
                "global_ceiling",
                f"The eval ledger refused a live call ({feature}): the build's live spend "
                f"({_money(global_spent)} spent, {_money(inflight)} in flight, this call up to "
                f"{_money(reserve)}) would pass the {_money(snap.ceiling_usd)} ceiling.",
            )
        if snap.allocated_usd is None or ws_spent + sweep_inflight + reserve > snap.allocated_usd:
            ws = snap.sweep.get("workstream")
            limit = _money(snap.allocated_usd) if snap.allocated_usd is not None else "no allocation"
            raise _refuse(
                "allocation",
                f"The eval ledger refused a live call ({feature}): workstream {ws} "
                f"({_money(ws_spent)} spent, this call up to {_money(reserve)}) would pass its "
                f"allocation ({limit}). Release contingency with an owner-visible note first.",
            )
        budget = _dec(snap.sweep.get("budget_usd"))
        if sweep_spent + sweep_inflight + reserve > budget:
            raise _refuse(
                "sweep_budget",
                f"The eval ledger refused a live call ({feature}): sweep "
                f"{snap.sweep.get('label') or sweep_id} ({_money(sweep_spent)} spent, "
                f"{_money(sweep_inflight)} in flight, this call up to {_money(reserve)}) would pass "
                f"its {_money(budget)} budget.",
            )
        _inflight[sweep_id] = sweep_inflight + reserve
    token = _admitted.set(sweep_id)
    return Ticket(sweep_id, reserve, token)


# ── Spend sums ───────────────────────────────────────────────────────────


def spent_for_sweep(sb: Any = None, sweep_id: str | None = None) -> Decimal:
    """Dollars a sweep spent: frozen spent_usd for a closed or aborted
    sweep, else max(llm_call_log sum, this process's view)."""
    store = _store(sb)
    sid = sweep_id or _open_sweep_id(store)
    if not sid:
        return _ZERO
    row = store.get_sweep(sid)
    if row is None:
        raise EvalLedgerError(f"Sweep {sid} was not found.")
    if row.get("status") != "open" and row.get("spent_usd") is not None:
        return _dec(row.get("spent_usd"))
    db = store.sweep_call_spend(sid)
    with _lock:
        if sid in _base:
            return _q(max(db, _base[sid] + _committed.get(sid, _ZERO)))
    return _q(db)


def _rows_since_epoch(store: LedgerStore) -> tuple[dict, list[dict]]:
    budget = store.get_budget()
    return budget, _since_epoch(store.list_sweeps(), budget.get("epoch_at"))


def global_spent(sb: Any = None) -> Decimal:
    """Live spend of the build: every sweep opened since the epoch."""
    store = _store(sb)
    _, rows = _rows_since_epoch(store)
    return _q(sum((spent_for_sweep(store, str(r["id"])) for r in rows), _ZERO))


def workstream_spent(sb: Any = None, workstream: str = "") -> Decimal:
    store = _store(sb)
    _, rows = _rows_since_epoch(store)
    return _q(
        sum((spent_for_sweep(store, str(r["id"])) for r in rows if r.get("workstream") == workstream), _ZERO)
    )


# ── Management ───────────────────────────────────────────────────────────


def open_sweep(
    sb: Any = None,
    *,
    label: str,
    workstream: str,
    purpose: str,
    budget_usd: Decimal | float | int,
    cases: Iterable[str] = (),
    config: dict | None = None,
    code_version: str | None = None,
    prompt_hash: str | None = None,
) -> dict:
    """Open a sweep (one at a time). Refuses an unknown purpose or
    workstream, a budget past the workstream's remaining allocation or the
    remaining ceiling, and, once global spend reaches $360, any purpose but
    smoke or holdout (spec 0.4 rule 7)."""
    store = _store(sb)
    label = (label or "").strip()
    if not label:
        raise EvalLedgerError("A sweep needs a label.")
    if purpose not in PURPOSES:
        raise EvalLedgerError(f"purpose must be one of {', '.join(PURPOSES)}.")
    budget = _dec(budget_usd)
    if budget <= 0:
        raise EvalLedgerError("budget_usd must be positive.")
    allocations = store.list_allocations()
    if workstream not in allocations:
        raise EvalLedgerError(f"Workstream {workstream!r} has no allocation row.")
    if workstream == CONTINGENCY:
        raise EvalLedgerError("Contingency is never spent directly; allocate it to a workstream with a note.")
    if store.list_sweeps("open"):
        raise EvalLedgerError("Another sweep is already open; close it first.")
    budget_row = store.get_budget()
    ceiling = min(_dec(budget_row.get("ceiling_usd")), CEILING_MAX_USD)
    spent_all = global_spent(store)
    if spent_all >= FINAL_ZONE_USD and purpose not in FINAL_ZONE_PURPOSES:
        raise EvalLedgerError(
            f"Global spend is {_money(spent_all)} (at or past {_money(FINAL_ZONE_USD)}): only smoke or "
            "holdout sweeps may open now (spec 0.4 rule 7). Report to the owner."
        )
    if spent_all + budget > ceiling:
        raise EvalLedgerError(
            f"A {_money(budget)} sweep would pass the {_money(ceiling)} ceiling ({_money(spent_all)} spent)."
        )
    ws_left = _dec(allocations[workstream]["allocated_usd"]) - workstream_spent(store, workstream)
    if budget > ws_left:
        raise EvalLedgerError(
            f"A {_money(budget)} sweep is more than {workstream}'s remaining allocation ({_money(ws_left)})."
        )
    row = {
        "id": str(uuid.uuid4()),
        "label": label,
        "workstream": workstream,
        "purpose": purpose,
        "budget_usd": budget,
        "status": "open",
        "opened_at": _now().isoformat(),
        "code_version": code_version,
        "prompt_hash": prompt_hash,
        "config": dict(config or {}),
        "cases": list(cases),
    }
    created = store.insert_sweep(row)
    _invalidate()
    return created


def _finish(store: LedgerStore, sweep_id: str | None, status: str, result_path: str | None) -> dict:
    sid = sweep_id or _open_sweep_id(store)
    if not sid:
        raise EvalLedgerError("No sweep is open.")
    row = store.get_sweep(sid)
    if row is None:
        raise EvalLedgerError(f"Sweep {sid} was not found.")
    if row.get("status") != "open":
        raise EvalLedgerError(f"Sweep {sid} is already {row.get('status')}.")
    if inflight_usd(sid) > 0:
        raise EvalLedgerError("Calls of this sweep are still in flight in this process.")
    spent = spent_for_sweep(store, sid)
    fields = {"status": status, "closed_at": _now().isoformat(), "spent_usd": spent}
    if result_path is not None:
        fields["result_path"] = result_path
    updated = store.update_sweep(sid, fields)
    _invalidate()
    return updated


def close_sweep(sb: Any = None, sweep_id: str | None = None, *, result_path: str | None = None) -> dict:
    """Close a sweep (default: the open one) and freeze its spent_usd."""
    return _finish(_store(sb), sweep_id, "closed", result_path)


def abort_sweep(sb: Any = None, sweep_id: str | None = None, *, result_path: str | None = None) -> dict:
    """Abort a sweep (default: the open one); spend is frozen the same way."""
    return _finish(_store(sb), sweep_id, "aborted", result_path)


def allocate(
    sb: Any = None,
    *,
    workstream: str,
    usd: Decimal | float | int,
    note: str,
    from_workstream: str | None = None,
) -> dict[str, dict]:
    """Set a workstream's allocation to `usd`, or with `from_workstream`
    move `usd` from that workstream to this one (how contingency is
    released). Every change needs a note (owner-visible); allocations never
    drop below what a workstream already spent and their sum never passes
    the ceiling."""
    store = _store(sb)
    note = (note or "").strip()
    if not note:
        raise EvalLedgerError("Every allocation change needs a note the owner can read.")
    amount = _dec(usd)
    if amount < 0:
        raise EvalLedgerError("usd must not be negative.")
    allocations = store.list_allocations()
    ceiling = min(_dec(store.get_budget().get("ceiling_usd")), CEILING_MAX_USD)
    new = {k: _dec(v["allocated_usd"]) for k, v in allocations.items()}
    if from_workstream is not None:
        if from_workstream not in new:
            raise EvalLedgerError(f"Workstream {from_workstream!r} has no allocation row.")
        if from_workstream == workstream:
            raise EvalLedgerError("Move from one workstream to another.")
        new[from_workstream] -= amount
        new[workstream] = new.get(workstream, _ZERO) + amount
        changed = (from_workstream, workstream)
    else:
        new[workstream] = amount
        changed = (workstream,)
    for ws in changed:
        if new[ws] < 0:
            raise EvalLedgerError(f"{ws} would go below zero.")
        spent = workstream_spent(store, ws)
        if new[ws] < spent:
            raise EvalLedgerError(f"{ws} already spent {_money(spent)}; its allocation cannot go below that.")
    if sum(new.values(), _ZERO) > ceiling:
        raise EvalLedgerError(
            f"Allocations would total {_money(sum(new.values(), _ZERO))}, past the {_money(ceiling)} ceiling."
        )
    for ws in changed:
        text = note if from_workstream is None else f"{note} (moved {_money(amount)} {from_workstream} -> {workstream})"
        store.set_allocation(ws, new[ws], text)
    _invalidate()
    return store.list_allocations()


def report(sb: Any = None) -> dict:
    """Ceiling, spend, remaining, per workstream and per sweep (Pacific
    times; overrun flags the >15% stop condition of spec 0.4 rule 7)."""
    store = _store(sb)
    budget, rows = _rows_since_epoch(store)
    ceiling = min(_dec(budget.get("ceiling_usd")), CEILING_MAX_USD)
    allocations = store.list_allocations()
    sweeps = []
    by_ws: dict[str, Decimal] = {}
    total = _ZERO
    open_id = None
    for r in rows:
        spent = spent_for_sweep(store, str(r["id"]))
        total += spent
        by_ws[str(r.get("workstream"))] = by_ws.get(str(r.get("workstream")), _ZERO) + spent
        b = _dec(r.get("budget_usd"))
        if r.get("status") == "open":
            open_id = str(r["id"])
        sweeps.append(
            {
                "id": str(r["id"]),
                "label": r.get("label"),
                "workstream": r.get("workstream"),
                "purpose": r.get("purpose"),
                "status": r.get("status"),
                "budget_usd": b,
                "spent_usd": spent,
                "overrun": bool(b > 0 and spent > b * (1 + OVERRUN_FRACTION)),
                "opened_at": pacific(r.get("opened_at")),
                "closed_at": pacific(r.get("closed_at")),
                "result_path": r.get("result_path"),
            }
        )
    workstreams = []
    for ws, alloc in sorted(allocations.items()):
        allocated = _dec(alloc["allocated_usd"])
        spent = by_ws.get(ws, _ZERO)
        workstreams.append(
            {
                "workstream": ws,
                "allocated_usd": allocated,
                "spent_usd": _q(spent),
                "remaining_usd": _q(allocated - spent),
                "note": alloc.get("note"),
            }
        )
    total = _q(total)
    return {
        "ceiling_usd": ceiling,
        "epoch_at": pacific(budget.get("epoch_at")),
        "enforce": bool(budget.get("enforce")),
        "spent_usd": total,
        "remaining_usd": _q(ceiling - total),
        "allocated_total_usd": sum((_dec(a["allocated_usd"]) for a in allocations.values()), _ZERO),
        "final_zone": total >= FINAL_ZONE_USD,
        "open_sweep_id": open_id,
        "inflight_usd": inflight_usd(),
        "workstreams": workstreams,
        "sweeps": sweeps,
    }


def report_markdown(rep: dict) -> str:
    """The report as markdown tables (the CLI prints this)."""
    lines = [
        f"Ceiling {_money(rep['ceiling_usd'])}, spent {_money(rep['spent_usd'])}, "
        f"remaining {_money(rep['remaining_usd'])} (epoch {rep['epoch_at']}; enforce row "
        f"{'on' if rep['enforce'] else 'off'}).",
        "",
        "| Workstream | Allocated | Spent | Remaining | Note |",
        "|---|---|---|---|---|",
    ]
    for w in rep["workstreams"]:
        lines.append(
            f"| {w['workstream']} | {_money(w['allocated_usd'])} | {_money(w['spent_usd'])} | "
            f"{_money(w['remaining_usd'])} | {w.get('note') or ''} |"
        )
    lines += ["", "| Sweep | Workstream | Purpose | Status | Budget | Spent | Opened | Closed |", "|---|---|---|---|---|---|---|---|"]
    for s in rep["sweeps"]:
        flag = " OVERRUN" if s["overrun"] else ""
        lines.append(
            f"| {s['label']} ({s['id'][:8]}) | {s['workstream']} | {s['purpose']} | {s['status']}{flag} | "
            f"{_money(s['budget_usd'])} | {_money(s['spent_usd'])} | {s['opened_at'] or ''} | {s['closed_at'] or ''} |"
        )
    return "\n".join(lines)


def status_block(settings: Any = None, sb: Any = None) -> dict:
    """The `eval_ledger` block of GET /ai-estimating/status. With the
    ledger off it touches no table (prod has none)."""
    s = settings or get_settings()
    enforced = bool(getattr(s, "ai_eval_ledger_enforce", False))
    block: dict = {
        "enforced": enforced,
        "open_sweep_id": None,
        "spent": None,
        "remaining": None,
        "sweep_spent": None,
        "sweep_remaining": None,
    }
    if not enforced:
        return block
    try:
        store = _store(sb)
        budget = store.get_budget()
        ceiling = min(_dec(budget.get("ceiling_usd")), CEILING_MAX_USD)
        spent = global_spent(store)
        sid = current_sweep_id(s, store)
        block.update(open_sweep_id=sid, spent=float(spent), remaining=float(_q(ceiling - spent)))
        if sid:
            row = store.get_sweep(sid)
            if row is not None:
                sweep_spent = spent_for_sweep(store, sid)
                block.update(
                    sweep_spent=float(sweep_spent),
                    sweep_remaining=float(_q(_dec(row.get("budget_usd")) - sweep_spent)),
                )
    except Exception as exc:  # noqa: BLE001 - the status page must still load
        logger.warning("eval_ledger: status block failed: %s", exc)
        block["error"] = "The eval ledger could not be read."
    return block
