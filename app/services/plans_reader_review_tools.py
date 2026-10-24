"""The reviewer's one write tool: `record_finding` (docs/AI_ESTIMATING_BUILD_SPEC.md
5.2 catalog; WS7). Mode `review` (it writes only review findings, never the
estimate), kind RV (the review group).

The model states a claim about BOQ lines, cites the ledger refs that support
it and may suggest a fix. Code keeps the claim only when its citations are in
the group's ledger (refs returned by a tool in this group, or carried in by
the deterministic pre-call stage); a finding with no verified citation is
kept at severity info, as REVIEW_SCHEMA validation does for the final
answer. Severity is input only: the review lane recomputes it from code
impact.

Durability (spec 4.3: a loop is kill-and-resume safe, idempotent by call
id). The tool loop stores every handled call's `result_data` in
`plans_reader_tool_calls` keyed by (group, call_id) through
`plans_reader_loop_store`, and on resume it serves stored calls without
calling the handler. So:

- the full finding (line ids, claim, kept and claimed severity, verified
  evidence, suggested fix) is part of the result data, and the store row
  is the record of truth;
- `finding_id` is `RV<n>` where n is the ordinal of this handler-reached
  `record_finding` call in the group (`ToolContext.calls_made`, which the
  loop rebuilds from the stored calls on resume), so a resumed group never
  renumbers and a post-resume finding never reuses an id already cited
  (`f:RV1`) in the transcript;
- `pop_recorded(run_id, group_id, store=...)` rebuilds the group's findings
  from the loop store's rows (call order), merged by call id with the
  in-process bucket (calls made outside a stored loop, tests), so findings
  recorded before a crash are never lost.

The reviewer module (`plans_reader_reviewer`, wave 3) drains them with the
group's loop store and writes them to `plans_reader_review_findings` with
the review id. No database of its own, no model, $0.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from app.services.estimating_contracts import BOQ_LINE_PREFIX, is_row_id
from app.services.plans_reader_loop_store import is_pending, loop_key
from app.services.plans_reader_review_schemas import FINDING_SEVERITIES
from app.services.plans_reader_tools import (
    REGISTRY,
    ToolContext,
    ToolResult,
    ToolSpec,
    is_ref,
    register,
)

TOOL_NAME = "record_finding"
FINDING_REF_PREFIX = "RV"
MAX_CLAIM_CHARS = 600
MAX_FIX_CHARS = 300
MAX_LINES = 40
MAX_EVIDENCE = 12
MAX_CALLS_PER_GROUP = 20

ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": list(FINDING_SEVERITIES)},
        "line_ids": {"type": "array", "items": {"type": "string"}},
        "claim": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "suggested_fix": {"type": ["string", "null"]},
    },
    "required": ["severity", "line_ids", "claim", "evidence", "suggested_fix"],
    "additionalProperties": False,
}

RESULT_DOC: dict[str, Any] = {
    "finding_id": "RV<n>, unique within the review group",
    "ref": "f:<finding_id>, citable in the final answer",
    "severity": "the severity kept (info when no citation verified)",
    "verified_evidence": "the cited refs found in this group's ledger",
    "dropped": "line ids or refs that were not accepted",
    "recorded": "the finding as stored (line_ids, claim, severity_claimed, suggested_fix)",
}

DESCRIPTION = (
    "Record one review finding about BOQ lines (ids like B12): a short claim, the severity you "
    "believe, the citation refs from earlier tool results that support it (for example t:, c:, s:, "
    "l:, m:, r:, p:, v: refs) and an optional suggested fix. It does not change the estimate. A "
    "finding without a citation from this review is kept as info."
)

_LOCK = threading.Lock()
_RECORDED: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _key(ctx: ToolContext) -> tuple[str, str]:
    return (str(ctx.run_id), str(ctx.group_id or ctx.message_id or ""))


def record_finding(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    claim = " ".join(str(args.get("claim") or "").split())
    if len(claim) < 10:
        return ToolResult.refusal("claim_too_short", "state the claim in at least one short sentence")
    raw_lines = [str(x).strip() for x in args.get("line_ids") or []][:MAX_LINES]
    lines = [x for x in dict.fromkeys(raw_lines) if is_row_id(x, BOQ_LINE_PREFIX)]
    bad_lines = [x for x in raw_lines if x not in lines]
    if not lines and raw_lines:
        return ToolResult.refusal("unknown_lines", "line_ids must be BOQ line ids like B12", {"line_ids": bad_lines})
    refs = [str(r).strip() for r in args.get("evidence") or []][:MAX_EVIDENCE]
    verified = [r for r in dict.fromkeys(refs) if is_ref(r) and r in ctx.ledger_refs]
    dropped_refs = [r for r in refs if r not in verified]
    severity = str(args.get("severity"))
    kept = severity if verified else "info"
    fix = args.get("suggested_fix")
    fix = " ".join(str(fix).split())[:MAX_FIX_CHARS] if fix else None
    with _LOCK:
        bucket = _RECORDED.setdefault(_key(ctx), [])
        same = next((x for x in bucket if ctx.call_id and x.get("call_id") == ctx.call_id), None)
        ordinal = ctx.calls_made(TOOL_NAME)
        if same is not None:
            finding_id = str(same["finding_id"])  # the same call delivered twice keeps its id
        elif ordinal > 0:
            finding_id = f"{FINDING_REF_PREFIX}{ordinal}"
        else:  # called outside dispatch (no loop counter)
            finding_id = f"{FINDING_REF_PREFIX}{len(bucket) + 1}"
        item = {
            "finding_id": finding_id,
            "severity_claimed": severity,
            "severity": kept,
            "line_ids": lines,
            "claim": claim[:MAX_CLAIM_CHARS],
            "evidence": verified,
            "suggested_fix": fix,
            "call_id": ctx.call_id,
        }
        if same is not None:
            bucket[bucket.index(same)] = item
        else:
            bucket.append(item)
    ref = f"f:{finding_id}"
    data = {
        "finding_id": finding_id,
        "ref": ref,
        "severity": kept,
        "verified_evidence": verified,
        "dropped": {"line_ids": bad_lines, "evidence": dropped_refs},
        "recorded": {"line_ids": lines, "claim": item["claim"], "severity_claimed": severity, "suggested_fix": fix},
    }
    return ToolResult.ok(data, citations=[ref])


def _from_store_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """A stored record_finding tool call row as a finding, or None for a
    refused, pending or foreign row."""
    if str(row.get("tool_name") or "") != TOOL_NAME or row.get("refused") or is_pending(dict(row)):
        return None
    data = row.get("result_data")
    if not isinstance(data, Mapping) or not data.get("finding_id"):
        return None
    rec = data.get("recorded") if isinstance(data.get("recorded"), Mapping) else {}
    return {
        "finding_id": str(data["finding_id"]),
        "severity_claimed": rec.get("severity_claimed", data.get("severity")),
        "severity": data.get("severity"),
        "line_ids": list(rec.get("line_ids") or []),
        "claim": str(rec.get("claim") or ""),
        "evidence": list(data.get("verified_evidence") or []),
        "suggested_fix": rec.get("suggested_fix"),
        "call_id": row.get("call_id"),
    }


def recorded_from_store(store: Any, group_id: str) -> list[dict[str, Any]]:
    """The findings a review group recorded, rebuilt from the loop store's
    `plans_reader_tool_calls` rows (round then creation order)."""
    out: list[dict[str, Any]] = []
    for row in store.tool_calls(loop_key(group_id=str(group_id))) or []:
        item = _from_store_row(row) if isinstance(row, Mapping) else None
        if item is not None:
            out.append(item)
    return out


def _merge(stored: list[dict[str, Any]], local: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stored findings first (the record of truth), then in-process ones
    whose call id the store does not hold; one finding per call id and per
    finding id."""
    out = [dict(x) for x in stored]
    calls = {x.get("call_id") for x in out if x.get("call_id")}
    ids = {x.get("finding_id") for x in out}
    for x in local:
        if (x.get("call_id") and x.get("call_id") in calls) or x.get("finding_id") in ids:
            continue
        out.append(dict(x))
        ids.add(x.get("finding_id"))
    return out


def recorded(run_id: str, group_id: str, *, store: Any = None) -> list[dict[str, Any]]:
    with _LOCK:
        local = [dict(x) for x in _RECORDED.get((str(run_id), str(group_id)), [])]
    return _merge(recorded_from_store(store, group_id), local) if store is not None else local


def pop_recorded(run_id: str, group_id: str, *, store: Any = None) -> list[dict[str, Any]]:
    """Drain the findings a review group recorded (the reviewer persists
    them with the review id). With the group's loop store the findings are
    rebuilt from its stored calls, so a resumed group loses none."""
    with _LOCK:
        local = _RECORDED.pop((str(run_id), str(group_id)), [])
    return _merge(recorded_from_store(store, group_id), local) if store is not None else local


SPEC = ToolSpec(
    name=TOOL_NAME,
    description=DESCRIPTION,
    args_schema=ARGS_SCHEMA,
    result_doc=RESULT_DOC,
    mode="review",
    kinds=frozenset({"review"}),
    handler=record_finding,
    cost_class="free",
    max_calls_per_group=MAX_CALLS_PER_GROUP,
    timeout_s=5.0,
)


def ensure_registered() -> ToolSpec:
    """Register `record_finding` in plans_reader_tools.REGISTRY once."""
    existing = REGISTRY.get(TOOL_NAME)
    if existing is not None:
        return existing
    return register(SPEC)


ensure_registered()


__all__ = [
    "TOOL_NAME",
    "SPEC",
    "ARGS_SCHEMA",
    "record_finding",
    "recorded",
    "recorded_from_store",
    "pop_recorded",
    "ensure_registered",
]
