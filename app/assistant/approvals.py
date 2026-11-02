"""Approval cards (docs/ASSISTANT.md, Permissions items 5 to 8;
docs/ASSISTANT_PHASE1_PLAN.md 3.1, 3.5, 3.6, 3.9 and 3.10).

A confirm tool called inside a turn never writes: `propose` reads the
target through the tool's preview routes, lets the tool build the one write
and its card, binds free text and URLs against the taint corpus, and inserts
one pending assistant_approvals row. The human approves or rejects it under
their own session (the decide RPC); the execute job
(app/assistant/approvals_job.py) re-reads, checks the precondition and sends
the write. Reverts are proposals too: `propose_revert` builds the inverse
from the ledger's before and after snapshots and inserts a new pending card.

Payloads and preconditions are hashed over canonical JSON, which refuses
floats (and Decimal, datetime, date and set): a float does not survive the
jsonb round trip byte-identically, and the execute job re-hashes the payload
it reads back.

Plain `def` throughout: the executor runs the shim on its own event loop,
and every database call goes through app/assistant/repo.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.assistant import binding, registry, repo
from app.assistant.executor import Executor, LeaseLost, RequestFailed
from app.assistant.registry import BuildContext, ProposalRefused, RevertNotPossible, ToolSpec
from app.assistant.repo import RevertExists
from app.assistant.tools._common import ToolArgumentError, pt
from app.core.roles import Role

logger = logging.getLogger(__name__)

__all__ = [
    "ApprovalServiceError", "MAX_CARDS_PER_TURN", "MAX_SUPERSEDE_DEPTH", "ProposalResult",
    "RevertExists", "STRANDED_AFTER", "TTL_BY_EFFECT", "WRITE_TTL", "approval_out", "canonical_json",
    "display_status", "format_value", "out_for", "outs_for", "propose", "propose_revert",
    "recheck", "recover_stranded", "resolve", "sha256_hex",
]

WRITE_TTL = timedelta(hours=1)
# How long a card waits for its decision, by the tool's effect (phase 2,
# docs/ASSISTANT.md Permissions: expensive and external_send cards 4 hours).
TTL_BY_EFFECT: dict[str, timedelta] = {
    registry.EFFECT_WRITE_INTERNAL: WRITE_TTL,
    registry.EFFECT_EXPENSIVE: timedelta(hours=4),
    registry.EFFECT_EXTERNAL_SEND: timedelta(hours=4),
    # Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.2.1): a person evidence card.
    registry.EFFECT_SENSITIVE_READ: timedelta(hours=1),
}
MAX_CARDS_PER_TURN = 5
MAX_SUPERSEDE_DEPTH = 3
STRANDED_AFTER = timedelta(minutes=5)

PAYLOAD_VERSION = 1
PRECONDITION_VERSION = 1
PREVIEW_VERSION = 1

# Statuses a revert may coexist with (the unique index ignores them).
DEAD_STATUSES = ("rejected", "expired", "failed")
LIVE_STATUSES = ("pending", "approved", "executing")

STRANDED_ERROR = "The change could not be started. Try again from a new request."
UNDO_PREFIX = "Undo: "

WARNING_TAINTED = {
    "code": "tainted_conversation",
    "text": "This conversation read text written by other people or imported from email. "
            "Check every value before approving.",
}
WARNING_CHANGED_SINCE_CHANGE = {
    "code": "changed_since_change",
    "text": "This record changed after the assistant's edit. Reverting overwrites those later changes.",
}

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_MONEY_FIELDS = frozenset({"amount", "labor_amount"})


class ApprovalServiceError(Exception):
    """A refusal the router maps to an HTTP answer: `status_code`, the
    X-Error-Code `code`, and an optional user-facing `message`."""

    def __init__(self, code: str, status_code: int, message: str | None = None):
        super().__init__(message or code)
        self.code, self.status_code, self.message = code, status_code, message


@dataclass(frozen=True)
class ProposalResult:
    ok: bool
    approval: dict | None          # the inserted row
    model_output: dict             # JSON the model gets back for this call
    # bad_args | refused | firewall | http_<n> | route_mismatch | not_offered
    # | tool_error | revert_exists | not_executing
    error_kind: str | None


# ── Canonical JSON and hashing (3.1) ─────────────────────────────────────────


def _check_canonical(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        raise TypeError(f"canonical_json: float at {path}; send money and amounts as strings")
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical_json: non-string key at {path}")
            _check_canonical(inner, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, inner in enumerate(value):
            _check_canonical(inner, f"{path}[{index}]")
        return
    # Decimal, datetime, date, set and everything else: callers convert first.
    raise TypeError(f"canonical_json: {type(value).__name__} at {path}; convert it to a string first")


def canonical_json(value: Any) -> str:
    """sort_keys, separators (",", ":"), ensure_ascii=False. Raises TypeError
    on any float anywhere in the value (money and amounts are strings with
    two decimals, counts are ints), on Decimal, datetime, date or set
    (callers convert first). Reason: a float does not survive the jsonb
    round trip byte-identically, and the execute job re-hashes the payload
    it reads back."""
    _check_canonical(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def precondition_for(target: Any, gate: Any) -> dict:
    return {"v": PRECONDITION_VERSION, "target_sha256": sha256_hex(target), "gate": gate}


# ── Small helpers ────────────────────────────────────────────────────────────


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_stripped(first: Iterable[dict], second: Iterable[dict]) -> list[dict]:
    """Stripped entries deduplicated by field, first occurrence wins."""
    out: list[dict] = []
    seen: set[str] = set()
    for entry in [*(first or ()), *(second or ())]:
        if not isinstance(entry, Mapping):
            continue
        field = str(entry.get("field") or "")
        if not field or field in seen:
            continue
        seen.add(field)
        out.append({"field": field, "reason": str(entry.get("reason") or binding.REASON_TAINTED)})
    return out


def _with_warning(warnings: list[dict], warning: dict) -> list[dict]:
    if any(isinstance(w, Mapping) and w.get("code") == warning["code"] for w in warnings):
        return warnings
    return [*warnings, dict(warning)]


def _taint_fields_for(tool_name: str) -> tuple[str, ...] | None:
    """The producing tool's taint fields; None for an unknown tool."""
    try:
        spec = registry.get(tool_name)
    except Exception:  # noqa: BLE001 - an unloadable registry counts as unknown
        spec = None
    return tuple(spec.taint_fields) if spec is not None else None


def ttl_for(spec: ToolSpec) -> timedelta:
    """The card's time to live: TTL_BY_EFFECT, WRITE_TTL for any other effect."""
    return TTL_BY_EFFECT.get(spec.effect, WRITE_TTL)


_COST_KEYS = ("estimate_usd", "user_remaining_usd", "global_remaining_usd")
_TOTALS_KEYS = ("files", "pages")
# The optional preview `runs` (phase 3, 3.6): the next run times of a
# routine card, display only (never in the gate).
MAX_PREVIEW_RUNS = 3
_RUN_MAX_CHARS = 40


def _cost_block(value: Any) -> dict | None:
    """The optional preview `cost` (phase 2, 3.9.2): money as strings only,
    so the preview stays canonical-JSON safe. Anything else is dropped."""
    if not isinstance(value, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in _COST_KEYS:
        raw = value.get(key)
        if raw is None:
            out[key] = None
        elif isinstance(raw, (Decimal, int)) and not isinstance(raw, bool):
            out[key] = str(raw)
        elif isinstance(raw, str):
            out[key] = raw
        else:
            out[key] = None
    return out


def _totals_block(value: Any) -> dict | None:
    """The optional preview `totals` (phase 2, 3.9.2): ints only."""
    if not isinstance(value, Mapping):
        return None
    out: dict[str, int] = {}
    for key in _TOTALS_KEYS:
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            out[key] = raw
    return out


def _runs_block(value: Any) -> list[str] | None:
    """The optional preview `runs` (phase 3, 3.6): at most three ISO
    instants with an offset, as strings. Anything else is dropped."""
    if not isinstance(value, (list, tuple)):
        return None
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or len(raw.strip()) > _RUN_MAX_CHARS:
            continue
        text = raw.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        out.append(text)
        if len(out) >= MAX_PREVIEW_RUNS:
            break
    return out


# The reason keys of a sensitive_read preview (phase 4, 3.2.1). The model's
# proposed reason is only the prefill of the field the human edits; the
# reason the human types never enters the preview, the payload or its hash.
REASON_KEYS = ("reason_required", "reason_proposed")
REASON_PROPOSED_MAX = 500
_REASON_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_REASON_DASHES = re.compile("[\u2013\u2014]")


def _reason_block(preview: Mapping[str, Any]) -> dict:
    """The sanitized reason keys of a sensitive card: `reason_required`
    True and `reason_proposed` the model's proposal as plain text, control
    characters removed, em and en dashes turned into commas, whitespace
    collapsed, at most 500 characters ("" when absent or not text). Nothing
    else about a reason survives, so the preview stays canonical JSON."""
    raw = preview.get("reason_proposed")
    text = raw if isinstance(raw, str) else ""
    text = _REASON_DASHES.sub(", ", _REASON_CONTROL.sub(" ", text))
    text = " ".join(text.split())[:REASON_PROPOSED_MAX].strip()
    return {"reason_required": True, "reason_proposed": text}


def _failed(kind: str, message: str) -> ProposalResult:
    return ProposalResult(ok=False, approval=None, model_output={"error": message}, error_kind=kind)


def _ledger_snapshots(ledger_rows: Iterable[Mapping[str, Any]] | None) -> tuple[dict | None, dict | None]:
    """(before snapshot of the begun row, after snapshot of the completed
    row) of one approval's ledger."""
    before = after = None
    for row in ledger_rows or ():
        if row.get("phase") == "begun" and isinstance(row.get("before_snapshot"), Mapping):
            before = dict(row["before_snapshot"])
        elif row.get("phase") == "completed" and isinstance(row.get("after_snapshot"), Mapping):
            after = dict(row["after_snapshot"])
    return before, after


# ── Proposals inside a turn (3.5) ────────────────────────────────────────────


def propose(
    executor: Executor,
    name: str,
    args: Mapping[str, Any],
    *,
    user_texts: tuple[str, ...],
    conversation_id: str | None,
    message_id: str | None,
    reverts_approval_id: str | None = None,
    supersede_depth: int = 0,
    skip_binding: bool = False,
    supersedes_approval_id: str | None = None,
    inherited_taint: bool = False,
    inherited_taint_level: str = "none",
    inherited_stripped: tuple[dict, ...] = (),
    now: datetime | None = None,
) -> ProposalResult:
    """Turn one confirm tool call into one pending approval card.

    Writes the assistant_approvals row and no ledger row (a proposal is not
    a write). With `supersedes_approval_id` the old row (executing) is
    expired and the new one inserted in one transaction through the
    supersede RPC. `payload.args` always stores the post-binding args, so a
    later supersede (which skips binding) can never bring a stripped value
    back. LeaseLost propagates; every other failure comes back as a
    ProposalResult with an error_kind and a message for the model."""
    now = _now(now)
    in_turn = reverts_approval_id is None and supersedes_approval_id is None
    spec = executor.specs.get(name)
    if spec is None or spec.write is None or (in_turn and not spec.offered):
        return _failed("not_offered", f"{name} is not available here.")
    if spec.namespace in {"mail", "browser"}:
        try:
            executor.establish_preview_context(spec)
        except LeaseLost:
            raise
        except Exception:
            return _failed("tool_error", "Browser authority could not be verified." if spec.namespace == "browser"
                           else "Mail authority could not be verified.")
    args = executor.expand(args or {})
    approval_id = str(uuid.uuid4())

    tainted = bool(executor.tainted_context or executor.tainted or inherited_taint)
    stripped = _merge_stripped(inherited_stripped, ())
    pre_requests: list[Any] | None = None
    pre_reads: list[Any] | None = None
    keep: dict[str, Any] = {}
    if tainted and not skip_binding and spec.write.bind_keep is not None:
        # The stored values binding may keep come from the record itself.
        try:
            pre_requests = spec.write.preview_requests(args)
            pre_reads = executor.read(pre_requests, spec)
            keep = dict(spec.write.bind_keep(args, pre_reads) or {})
        except ToolArgumentError as exc:
            return _failed("bad_args", str(exc))
        except RequestFailed as exc:
            return _failed(exc.kind, exc.message)
        except LeaseLost:
            raise
        except Exception:  # noqa: BLE001 - a builder bug is a failed proposal, not a dead turn
            logger.exception("assistant approvals: %s failed while reading its record", name)
            return _failed("tool_error", "The change could not be prepared.")
    if skip_binding:
        computed = binding.taint_level(True, executor.tainted_texts, ()) if executor.tainted else "none"
    else:
        corpus: list[tuple[str, str]] = []
        if tainted:
            corpus = list(executor.tainted_texts)
            try:
                stored = repo.tainted_results(executor.user_id, conversation_id, limit=50)
            except Exception:  # noqa: BLE001 - binding fails closed: no corpus, no card
                logger.exception("assistant approvals: taint corpus read failed for %s", name)
                return _failed("tool_error", "The change could not be prepared. Try again.")
            corpus.extend(binding.corpus_from_results(stored, _taint_fields_for))
            args, new_stripped = binding.bind_args(
                args,
                text_fields=spec.write.bound_text_fields,
                url_fields=spec.write.bound_url_fields,
                corpus=[text for _field, text in corpus],
                user_texts=user_texts,
                tainted=True,
                keep=keep,
            )
            stripped = _merge_stripped(stripped, new_stripped)
        computed = binding.taint_level(tainted, corpus, stripped)
    level = binding.level_max(inherited_taint_level or "none", computed)
    if tainted and level == "none":
        level = "high"
    if stripped:
        level = "high"

    ctx = BuildContext(
        approval_id=approval_id, role=executor.role, short=executor.short, tainted=tainted,
        stripped=tuple(stripped), now=now, is_revert=reverts_approval_id is not None,
    )
    try:
        requests = spec.write.preview_requests(args)
        reads = pre_reads if pre_reads is not None and requests == pre_requests else executor.read(requests, spec)
        proposal = spec.write.build(args, reads, ctx)
    except ToolArgumentError as exc:
        return _failed("bad_args", str(exc))
    except ProposalRefused as exc:
        return _failed("refused", str(exc))
    except RequestFailed as exc:
        return _failed(exc.kind, exc.message)
    except LeaseLost:
        raise
    except Exception:  # noqa: BLE001 - a builder bug is a failed proposal, not a dead turn
        logger.exception("assistant approvals: %s failed while building its card", name)
        return _failed("tool_error", "The change could not be prepared.")
    if proposal.request.route != spec.route:
        logger.error("assistant approvals: %s built a write for %s, not %s", name, proposal.request.route, spec.route)
        return _failed("tool_error", "The change could not be prepared.")

    request = proposal.request
    request_out: dict[str, Any] = {
        "method": request.route[0],
        "path_format": request.route[1],
        "path": request.path,
        "json": request.json,
        "headers": {str(k): str(v) for k, v in dict(request.headers or {}).items()},
    }
    if request.params:
        request_out["params"] = dict(request.params)
    payload = {
        "v": PAYLOAD_VERSION,
        "tool": spec.name,
        "tool_version": spec.version,
        "args": args,
        "request": request_out,
        "project_id": proposal.project_id,
        "conversation_id": conversation_id,
        "tainted": tainted,
        "taint_level": level,
        "stripped": stripped,
        "reverts_approval_id": reverts_approval_id,
        "supersede_depth": int(supersede_depth),
    }
    try:
        payload_sha = sha256_hex(payload)
        precondition = precondition_for(proposal.target, proposal.gate)
    except TypeError:
        logger.exception("assistant approvals: %s built a payload that is not canonical JSON", name)
        return _failed("tool_error", "The change could not be prepared.")

    preview = _final_preview(proposal.preview, spec, tainted=tainted, level=level, stripped=stripped)
    if reverts_approval_id is not None:
        preview = _revert_preview(executor.user_id, preview, reverts_approval_id, proposal.target)

    row = {
        "id": approval_id,
        "user_label": executor.user_label,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "project_id": proposal.project_id,
        "action_kind": spec.name,
        "payload": payload,
        "payload_sha256": payload_sha,
        "preview": preview,
        "precondition": precondition,
        "status": "pending",
        "expires_at": (now + ttl_for(spec)).isoformat(),
        "reverts_approval_id": reverts_approval_id,
    }
    try:
        if supersedes_approval_id is None:
            inserted = repo.insert_approval(executor.user_id, row)
        else:
            res = repo.supersede_approval(executor.user_id, supersedes_approval_id, row)
            if not res.get("ok"):
                return _failed(str(res.get("reason") or "not_executing"),
                               "The earlier card was no longer running, so no new card was made.")
            inserted = _dict(res.get("row")) or row
    except RevertExists:
        return _failed("revert_exists", "An undo of this change already exists.")
    except Exception:  # noqa: BLE001 - the card could not be stored; the model is told
        logger.exception("assistant approvals: approval insert failed for %s", name)
        return _failed("tool_error", "The change could not be saved for approval. Try again.")

    model_output = {
        "status": "waiting_for_approval",
        "approval": executor.short(approval_id),
        "summary": proposal.summary,
        "stripped_fields": [s["field"] for s in stripped],
    }
    return ProposalResult(ok=True, approval=inserted, model_output=model_output, error_kind=None)


def _final_preview(preview: Mapping[str, Any], spec: ToolSpec, *, tainted: bool, level: str,
                   stripped: list[dict]) -> dict:
    """The tool's preview with the service's taint fields and warnings
    merged in (3.6): tainted_conversation only at level high,
    stripped_value whenever a value was stripped."""
    out = dict(preview or {})
    out.setdefault("v", PREVIEW_VERSION)
    out.setdefault("tool", spec.name)
    out.setdefault("title", spec.progress_label)
    out.setdefault("tool_label", spec.progress_label)
    out.setdefault("project", None)
    out.setdefault("rows", [])
    out.setdefault("items", [])
    out.setdefault("similar", [])
    out.setdefault("revert_of", None)
    for key, clean in (("cost", _cost_block), ("totals", _totals_block), ("runs", _runs_block)):
        if key in out:
            block = clean(out[key])
            if block is None:
                out.pop(key)
            else:
                out[key] = block
    # Phase 4 (3.2.1): only a sensitive_read card carries the reason keys,
    # sanitized; a `reason` key never reaches any preview.
    out.pop("reason", None)
    if spec.effect == registry.EFFECT_SENSITIVE_READ:
        for key in ("cost", "totals", "runs"):
            out.pop(key, None)
        out.update(_reason_block(out))
    else:
        for key in REASON_KEYS:
            out.pop(key, None)
    warnings = [dict(w) for w in out.get("warnings") or () if isinstance(w, Mapping)]
    if level == "high":
        warnings = _with_warning(warnings, WARNING_TAINTED)
    if stripped:
        fields = ", ".join(s["field"] for s in stripped)
        warnings = _with_warning(warnings, {
            "code": "stripped_value",
            "text": f"Some values were removed because they came from untrusted text: {fields}.",
        })
    out["warnings"] = warnings
    out["tainted"] = tainted
    out["taint_level"] = level
    out["stripped"] = [dict(s) for s in stripped]
    return out


def _revert_preview(user_id: str, preview: dict, reverts_approval_id: str, target: Any) -> dict:
    """A revert card: revert_of, the "Undo: " title, and the
    changed_since_change warning when the record moved on since the
    original change (best effort; a ledger read error skips the warning)."""
    out = dict(preview)
    out["revert_of"] = reverts_approval_id
    title = str(out.get("title") or "")
    if not title.startswith(UNDO_PREFIX):
        out["title"] = UNDO_PREFIX + title
    try:
        ledger = repo.tool_calls_for_approvals(user_id, [reverts_approval_id]).get(str(reverts_approval_id), [])
        _before, after = _ledger_snapshots(ledger)
        after_target = (after or {}).get("target") if after else None
        if after is not None and "target" in after and after_target != target:
            out["warnings"] = _with_warning(list(out.get("warnings") or []), WARNING_CHANGED_SINCE_CHANGE)
    except Exception:  # noqa: BLE001 - the warning is advisory
        logger.exception("assistant approvals: could not compare the revert target of %s", reverts_approval_id)
    return out


# ── Display (3.9) ────────────────────────────────────────────────────────────


def display_status(row: Mapping[str, Any], now: datetime | None = None) -> str:
    """expired with superseded_by: superseded; failed with
    may_have_partially_run: partial; pending past expires_at: expired;
    otherwise the status."""
    status = str(row.get("status") or "")
    if status == "expired" and row.get("superseded_by"):
        return "superseded"
    if status == "failed" and _dict(row.get("result")).get("may_have_partially_run"):
        return "partial"
    if status == "pending":
        expires = _parse_ts(row.get("expires_at"))
        if expires is not None and expires <= _now(now):
            return "expired"
    return status


def format_value(field: str, value: Any) -> str:
    """A target value as the card shows it: money "$12,500.00", datetimes in
    Pacific time, booleans Yes or No, null "Empty"."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "Empty"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if field in _MONEY_FIELDS or field.endswith("_amount"):
        try:
            return f"${Decimal(str(value)):,.2f}"
        except (InvalidOperation, ValueError):
            return str(value)
    if field == "labor_breakdown" and isinstance(value, list):
        total = Decimal("0")
        for line in value:
            try:
                total += Decimal(str(_dict(line).get("amount") or "0"))
            except (InvalidOperation, ValueError):
                continue
        count = len(value)
        return f"{count} line{'s' if count != 1 else ''}, total ${total:,.2f}"
    if isinstance(value, str):
        if _DATETIME_RE.match(value):
            return pt(value) or value
        return value
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        if all(isinstance(v, (str, int, bool)) or v is None for v in value):
            return ", ".join(format_value(field, v) for v in value) or "Empty"
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _diff(row: Mapping[str, Any], before: dict | None, after: dict | None) -> list[dict] | None:
    """Rows from the ledger's before and after targets, labelled with the
    preview rows' labels."""
    before_t = _dict((before or {}).get("target"))
    after_t = _dict((after or {}).get("target"))
    if not before_t and not after_t:
        return None
    out: list[dict] = []
    for prow in _dict(row.get("preview")).get("rows") or ():
        if not isinstance(prow, Mapping) or not prow.get("field"):
            continue
        field = str(prow["field"])
        if field not in before_t and field not in after_t:
            continue
        b, a = before_t.get(field), after_t.get(field)
        out.append({
            "field": field,
            "label": str(prow.get("label") or field),
            "before": format_value(field, b),
            "after": format_value(field, a),
            "changed": b != a,
        })
    # Rows that all match say nothing about the change: add_project_gc's
    # only target-backed row is the chosen contacts, the same before and
    # after, so its diff read "Contacts: Empty, Empty" and hid the GC.
    if not any(r["changed"] for r in out):
        return None
    return out


def _revertibility(row: Mapping[str, Any], before: dict | None, after: dict | None,
                   revert_row: Mapping[str, Any] | None) -> tuple[bool, str | None]:
    """(revertible, revert_blocked) per 3.9."""
    if row.get("status") != "executed" or row.get("reverts_approval_id"):
        return False, None
    spec = registry.get(str(row.get("action_kind") or ""))
    if spec is None or spec.write is None or not spec.write.revert_tool or spec.write.revert_args is None:
        return False, None
    if revert_row is not None and revert_row.get("status") not in DEAD_STATUSES:
        return False, None
    if before is None or after is None:
        return False, None
    args = _dict(_dict(row.get("payload")).get("args"))
    try:
        spec.write.revert_args(args, before, after)
    except RevertNotPossible as exc:
        return False, str(exc)
    except Exception:  # noqa: BLE001 - a revert builder bug makes the row not revertible
        logger.exception("assistant approvals: revert_args failed for %s", row.get("id"))
        return False, None
    return True, None


def approval_out(row: Mapping[str, Any], ledger_rows: Iterable[Mapping[str, Any]] | None,
                 revert_row: Mapping[str, Any] | None, now: datetime | None = None) -> dict:
    """The ApprovalOut shape (section 5.2). `payload` is never served."""
    before, after = _ledger_snapshots(ledger_rows)
    revertible, blocked = _revertibility(row, before, after, revert_row)
    out = {key: row.get(key) for key in (
        "id", "status", "action_kind", "project_id", "conversation_id", "message_id",
        "payload_sha256", "expires_at", "expired_reason", "superseded_by",
        "reverts_approval_id", "decided_at", "executed_at", "created_at", "result",
    )}
    out["display_status"] = display_status(row, now)
    out["preview"] = _dict(row.get("preview"))
    out["precondition"] = _dict(row.get("precondition"))
    out["revertible"] = revertible
    out["revert_blocked"] = blocked
    out["revert"] = (
        {"id": revert_row.get("id"), "status": revert_row.get("status")} if revert_row is not None else None
    )
    out["diff"] = _diff(row, before, after) if row.get("status") == "executed" else None
    return out


def outs_for(user_id: str, rows: list[Mapping[str, Any]], now: datetime | None = None) -> list[dict]:
    """ApprovalOut for a page of rows: one ledger read and one revert read."""
    ids = [str(r.get("id")) for r in rows if r.get("id")]
    ledger = repo.tool_calls_for_approvals(user_id, ids) if ids else {}
    executed = [i for i, r in zip(ids, rows) if r.get("status") == "executed"]
    reverts = repo.reverts_for(user_id, executed) if executed else {}
    return [approval_out(r, ledger.get(str(r.get("id")), []), reverts.get(str(r.get("id"))), now) for r in rows]


def out_for(user_id: str, row: Mapping[str, Any], now: datetime | None = None) -> dict:
    return outs_for(user_id, [row], now)[0]


# ── Stranded approvals (3.10) ────────────────────────────────────────────────


def recover_stranded(user_id: str, row: Mapping[str, Any] | None, now: datetime | None = None) -> dict | None:
    """The lazy repair for a process that died between the approve CAS and
    the enqueue, or a job that ended without its failure handler: an
    approved row decided STRANDED_AFTER ago with no active execute job is
    failed (nothing was sent: the job's own CAS to executing precedes any
    write) and its message settled. A queue read error leaves it alone.
    Returns the row as it now stands."""
    if not row or row.get("status") != "approved":
        return dict(row) if row else None
    decided = _parse_ts(row.get("decided_at"))
    if decided is None or decided > _now(now) - STRANDED_AFTER:
        return dict(row)
    try:
        from app.services import llm_queue

        job = llm_queue.latest_job(llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE, str(row["id"]))
        active = llm_queue._ACTIVE_STATUSES
    except Exception:  # noqa: BLE001 - unknown queue state: leave the row alone
        logger.exception("assistant approvals: queue read failed for approval %s", row.get("id"))
        return dict(row)
    if job and job.get("status") in active:
        return dict(row)
    if repo.mark_approval(user_id, str(row["id"]), {
        "status": "failed", "result": {"ok": False, "error": STRANDED_ERROR},
    }, expect_status="approved"):
        repo.settle_message(user_id, row.get("message_id"))
    return repo.get_approval(user_id, str(row["id"])) or dict(row)


# ── Revert, recheck, resolve (3.10) ──────────────────────────────────────────


def _role_of(user: Any) -> Role | None:
    try:
        return Role(getattr(user, "role", None))
    except ValueError:
        return None


def preview_executor(user_id: str, user_label: str, spec: ToolSpec, *, role: Role | None,
                     conversation_id: str | None, tainted_context: bool = False) -> Executor:
    """An executor without an approval over one write spec: its shim
    reaches the preview routes only (turn allowlist)."""
    return Executor(
        user_id=user_id, user_label=user_label, run_id=str(uuid.uuid4()),
        specs={spec.name: spec}, lease_owner=f"assistant-preview:{uuid.uuid4()}",
        conversation_id=conversation_id, message_id=None, approval_id=None,
        tainted_context=tainted_context, role=role,
    )


def _ledger_for(user_id: str, approval_id: str) -> tuple[dict | None, dict | None]:
    rows = repo.tool_calls_for_approvals(user_id, [approval_id]).get(str(approval_id), [])
    return _ledger_snapshots(rows)


def propose_revert(user: Any, approval_id: str, *, now: datetime | None = None) -> dict:
    """A pending revert card for an executed, revertible approval (3.10
    steps 2 to 4). The router checks via_assistant_run_id and the delegation
    first. Raises ApprovalServiceError: 404 approval_not_found, 409
    approval_not_revertible (with the RevertNotPossible text), 409
    revert_exists, 409 revert_refused, 502 revert_read_failed."""
    user_id = str(user.id)
    row = repo.get_approval(user_id, approval_id)
    if row is None:
        raise ApprovalServiceError("approval_not_found", 404)
    spec = registry.get(str(row.get("action_kind") or ""))
    if (row.get("status") != "executed" or row.get("reverts_approval_id") or spec is None
            or spec.write is None or not spec.write.revert_tool or spec.write.revert_args is None):
        raise ApprovalServiceError("approval_not_revertible", 409)
    existing = repo.reverts_for(user_id, [approval_id]).get(str(approval_id))
    if existing is not None and existing.get("status") not in DEAD_STATUSES:
        raise ApprovalServiceError("revert_exists", 409)
    before, after = _ledger_for(user_id, approval_id)
    if before is None or after is None:
        raise ApprovalServiceError("approval_not_revertible", 409)
    payload = _dict(row.get("payload"))
    try:
        args = spec.write.revert_args(_dict(payload.get("args")), before, after)
    except RevertNotPossible as exc:
        raise ApprovalServiceError("approval_not_revertible", 409, str(exc)) from None
    revert_spec = registry.get(spec.write.revert_tool)
    if revert_spec is None or revert_spec.write is None:
        raise ApprovalServiceError("approval_not_revertible", 409)
    executor = preview_executor(
        user_id, str(row.get("user_label") or user_id), revert_spec,
        role=_role_of(user), conversation_id=row.get("conversation_id"),
    )
    res = propose(
        executor, revert_spec.name, args, user_texts=(), conversation_id=row.get("conversation_id"),
        message_id=None, reverts_approval_id=str(approval_id), skip_binding=True, now=now,
    )
    if res.ok and res.approval is not None:
        return res.approval
    message = str(res.model_output.get("error") or "") or None
    if res.error_kind == "revert_exists":
        raise ApprovalServiceError("revert_exists", 409)
    if res.error_kind in ("refused", "bad_args"):
        raise ApprovalServiceError("revert_refused", 409, message)
    raise ApprovalServiceError("revert_read_failed", 502, message)


def _same(a: Any, b: Any) -> bool:
    """Best-effort value equality across the target form and the request
    form: equal values, equal instants, or equal numbers."""
    if a == b:
        return True
    if isinstance(a, str) and isinstance(b, str):
        da, db = _parse_ts(a) if _DATETIME_RE.match(a) else None, _parse_ts(b) if _DATETIME_RE.match(b) else None
        if da is not None and db is not None:
            return da == db
    if isinstance(a, (str, int)) and isinstance(b, (str, int)) and not isinstance(a, bool) and not isinstance(b, bool):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except (InvalidOperation, ValueError):
            return False
    if isinstance(a, (list, dict)) and isinstance(b, (list, dict)):
        try:
            return canonical_json(a) == canonical_json(b)
        except TypeError:
            return False
    return False


def recheck(user: Any, approval_id: str, *, now: datetime | None = None) -> dict:
    """For a partial card: the preview reads once more, the before target
    against the live one, and whether the live state matches the before
    state or the intended one (null for creates). Raises
    ApprovalServiceError 404 approval_not_found, 409 approval_not_partial,
    502 recheck_failed."""
    user_id = str(user.id)
    row = repo.get_approval(user_id, approval_id)
    if row is None:
        raise ApprovalServiceError("approval_not_found", 404)
    if display_status(row, now) != "partial":
        raise ApprovalServiceError("approval_not_partial", 409)
    spec = registry.get(str(row.get("action_kind") or ""))
    if spec is None or spec.write is None:
        raise ApprovalServiceError("recheck_failed", 502)
    payload = _dict(row.get("payload"))
    args = _dict(payload.get("args"))
    executor = preview_executor(user_id, str(row.get("user_label") or user_id), spec,
                                role=_role_of(user), conversation_id=row.get("conversation_id"))
    try:
        reads = executor.read(spec.write.preview_requests(args), spec)
        live, _gate = spec.write.snapshot(args, reads)
    except (RequestFailed, ToolArgumentError, ProposalRefused) as exc:
        raise ApprovalServiceError("recheck_failed", 502, str(exc)) from None
    before, _after = _ledger_for(user_id, approval_id)
    before_t = _dict((before or {}).get("target"))
    live_t = _dict(live)
    labels = {str(r.get("field")): str(r.get("label") or r.get("field"))
              for r in _dict(row.get("preview")).get("rows") or () if isinstance(r, Mapping) and r.get("field")}
    keys = [k for k in labels if k in before_t or k in live_t]
    keys += [k for k in sorted(set(before_t) | set(live_t)) if k not in labels]
    rows = [{
        "field": k, "label": labels.get(k, k),
        "before": format_value(k, before_t.get(k)), "live": format_value(k, live_t.get(k)),
    } for k in keys]
    matches_intended: bool | None = None
    if not spec.write.creates:
        intended = _dict(_dict(payload.get("request")).get("json"))
        matches_intended = bool(intended) and all(
            _same(live_t.get(k), v) for k, v in intended.items() if k in live_t
        )
    return {
        "approval": out_for(user_id, row, now),
        "rows": rows,
        "matches_before": before is not None and before_t == live_t,
        "matches_intended": matches_intended,
    }


def resolve(user: Any, approval_id: str, outcome: str, *, now: datetime | None = None) -> dict:
    """For a partial card: record the human's verdict in result.review and
    move the message from waiting_review to done. Raises
    ApprovalServiceError 404 approval_not_found or 409 approval_not_partial."""
    if outcome not in ("applied", "not_applied"):
        raise ApprovalServiceError("bad_outcome", 422)
    user_id = str(user.id)
    row = repo.get_approval(user_id, approval_id)
    if row is None:
        raise ApprovalServiceError("approval_not_found", 404)
    if display_status(row, now) != "partial":
        raise ApprovalServiceError("approval_not_partial", 409)
    result = {**_dict(row.get("result")),
              "review": {"outcome": outcome, "by": user_id, "at": _now(now).isoformat()}}
    if not repo.mark_approval(user_id, str(approval_id), {"result": result}, expect_status="failed"):
        raise ApprovalServiceError("approval_not_partial", 409)
    repo.finish_message_review(user_id, row.get("message_id"))
    fresh = repo.get_approval(user_id, str(approval_id)) or {**row, "result": result}
    return out_for(user_id, fresh, now)
