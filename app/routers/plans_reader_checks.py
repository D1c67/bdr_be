"""AI Estimating checks routes (docs/AI_ESTIMATING_BUILD_SPEC.md 4.4, 5.4, 5.5;
WS4): the integrity gate report of a run, the human override of a finding
and the recount request.

Prefix `/ai-estimating`, behind the AI Estimating gates (`require_plans_reader`
404 while PLANS_READER_ENABLED is off, `require_internal`,
`require_ai_estimating`; the external estimator never reaches it):

- GET  /runs/{run_id}/checks                           the gate report: the run's gate_summary,
                                                       pricing_allowed with its reasons, one status
                                                       per gate id, every finding row with its route
                                                       and status, counts per stage; every internal
                                                       role reads it (the Accountant included)
- POST /runs/{run_id}/checks/findings/{id}/override    mark an open or routed finding overridden with a
                                                       written reason of at least 10 characters and
                                                       recompute the run's gate_summary;
                                                       AI_GATE_OVERRIDE_ROLES (Estimating Admin,
                                                       Executive, IT Admin); audited
- POST /runs/{run_id}/checks/recount                   ask for a recount of a census floor: routes the
                                                       matching census_floor findings to `recount` with
                                                       the note and hands the targets to the WS7 recount
                                                       seam when one is installed; engineers and writer
                                                       roles, dev environments only (404 in production)

Every handler is plain `def` (the sync Supabase SDK never runs inside
`async def`). Blocking findings never lower a number: an override records
who accepted the finding and why; it changes no quantity.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_ai_estimating, require_internal
from app.core.features import require_plans_reader
from app.core.roles import AI_GATE_OVERRIDE_ROLES, ESTIMATING_ENGINEER_ROLES, WRITER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import plans_reader_gates as gates
from app.services.estimating_contracts import (
    ACTIVE_FINDING_STATUSES,
    GATE_IDS,
    GateFinding,
    gate_report_from_findings,
)
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai-estimating",
    tags=["ai-estimating-checks"],
    dependencies=[Depends(require_plans_reader), Depends(require_internal), Depends(require_ai_estimating)],
)

MIN_REASON_CHARS = 10
OVERRIDE_ROLES: frozenset[Role] = AI_GATE_OVERRIDE_ROLES
RECOUNT_ROLES: frozenset[Role] = WRITER_ROLES | ESTIMATING_ENGINEER_ROLES
RUNS_TABLE = "plans_reader_runs"
FINDINGS_TABLE = gates.FINDINGS_TABLE
MSG_RUN_NOT_FOUND = "AI Estimating run not found"
MSG_FINDING_NOT_FOUND = "Finding not found"
COMPANY_TZ = ZoneInfo("America/Los_Angeles")
GATE_STATUSES: tuple[str, ...] = ("pass", "block", "warn", "not_applicable", "not_run")

# WS7 seam (set by the integrator): `(sb, run, findings, user, note) -> list`
# of the recount groups queued. None means the request is only recorded on
# the findings (route recount) for the next recount pass.
RECOUNT_REQUESTER: Callable[..., list[Any]] | None = None


class OverrideIn(BaseModel):
    reason: str = Field(min_length=MIN_REASON_CHARS, max_length=1000)


class RecountIn(BaseModel):
    sheet_key: str | None = Field(default=None, max_length=16)
    family: str | None = Field(default=None, max_length=80)
    finding_id: str | None = Field(default=None, max_length=64)
    note: str = Field(default="", max_length=1000)


def _now_iso() -> str:
    return datetime.now(COMPANY_TZ).isoformat(timespec="seconds")


def _uuid_or_404(value: str, what: str) -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None
    return str(value)


def _run_or_404(sb: Any, run_id: str) -> dict[str, Any]:
    rows = sb.table(RUNS_TABLE).select("*").eq("id", run_id).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_RUN_NOT_FOUND)
    return dict(rows[0])


def _finding_rows(sb: Any, run_id: str) -> list[dict[str, Any]]:
    rows = sb.table(FINDINGS_TABLE).select("*").eq("run_id", run_id).execute().data or []
    return [dict(r) for r in rows if isinstance(r, Mapping)]


def _finding_view(row: Mapping[str, Any]) -> dict[str, Any]:
    f = GateFinding.from_row(row)
    return {
        "id": row.get("id"),
        "stage": f.stage,
        "gate_id": f.gate_id,
        "severity": f.severity,
        "route": f.route,
        "status": f.status,
        "blocking": f.blocking,
        "target": dict(f.target),
        "line_ids": f.line_ids(),
        "message": f.message,
        "evidence": dict(f.evidence),
        "output_version": f.output_version,
        "fingerprint": f.fingerprint,
        "routed_group_id": f.routed_group_id,
        "resolved_by": f.resolved_by,
        "resolved_at": f.resolved_at,
        "note": f.note,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def gate_statuses(summary: Mapping[str, Any] | None, findings: list[GateFinding]) -> list[dict[str, Any]]:
    """One row per gate id: pass, block, warn, not_applicable or not_run,
    from the run's gate_summary (the marker that the final gates ran) and
    the active findings of every stage."""
    summary = summary or {}
    evaluated = {str(g) for g in summary.get("evaluated") or []}
    not_applicable = {str(g) for g in summary.get("not_applicable") or []}
    active = [f for f in findings if f.status in ACTIVE_FINDING_STATUSES]
    blocks = {f.gate_id for f in active if f.severity == "block"}
    warns = {f.gate_id for f in active if f.severity == "warn"}
    out: list[dict[str, Any]] = []
    for gate_id in GATE_IDS:
        count = sum(1 for f in active if f.gate_id == gate_id)
        if gate_id in blocks:
            state = "block"
        elif gate_id in warns:
            state = "warn"
        elif gate_id in evaluated:
            state = "pass"
        elif gate_id in not_applicable:
            state = "not_applicable"
        else:
            state = "not_run"
        out.append({"gate_id": gate_id, "status": state, "count": count})
    return out


def _report(sb: Any, run: Mapping[str, Any], user: CurrentUser) -> dict[str, Any]:
    run_id = str(run["id"])
    rows = _finding_rows(sb, run_id)
    findings = [GateFinding.from_row(r) for r in rows]
    summary = run.get("gate_summary") if isinstance(run.get("gate_summary"), Mapping) else None
    allowed, reasons = gates.pricing_allowed(run, findings=findings if findings or summary is None else None)
    stages: dict[str, int] = {}
    for f in findings:
        stages[f.stage] = stages.get(f.stage, 0) + 1
    s = get_settings()
    return {
        "run_id": run_id,
        "run_status": run.get("status"),
        "autonomy_state": run.get("autonomy_state"),
        "gate_summary": dict(summary) if summary else None,
        "gate_report": gate_report_from_findings(findings),
        "pricing_allowed": {"allowed": allowed, "reasons": reasons},
        "gates": gate_statuses(summary, findings),
        "findings": sorted((_finding_view(r) for r in rows), key=lambda f: (f["status"] not in ACTIVE_FINDING_STATUSES, f["severity"] != "block", f["gate_id"])),
        "stages": stages,
        "enforced": bool(summary.get("enforced")) if summary else bool(getattr(s, "plans_reader_gates_blocking", False)),
        "can_override": user.role in OVERRIDE_ROLES,
        "can_request_recount": user.role in RECOUNT_ROLES and not getattr(s, "is_production", True),
    }


@router.get("/runs/{run_id}/checks")
def get_checks(run_id: str, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    run_id = _uuid_or_404(run_id, "Run")
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    return _report(sb, run, user)


def _audit(user: CurrentUser, action: str, run_id: str, payload: dict[str, Any] | None = None) -> None:
    try:
        audit(user.id, action, "ai_estimating_run", run_id, payload)
    except Exception:  # noqa: BLE001 - an audit failure never blocks the action
        logger.exception("plans_reader_checks: audit %s failed", action)


def _recompute_summary(sb: Any, run: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The run's gate_summary after a status change: the stage final
    findings as they stand now, keeping the summary's enforced flag, mode,
    version and not_applicable list (the marker that the gates ran)."""
    prior = run.get("gate_summary") if isinstance(run.get("gate_summary"), Mapping) else None
    if prior is None:
        return None
    final = [GateFinding.from_row(r) for r in rows if r.get("stage") == "final"]
    summary = gates.gate_summary_from_findings(
        final, enforced=bool(prior.get("enforced", True)), output_version=prior.get("output_version"), mode=prior.get("mode"),
        not_applicable=prior.get("not_applicable") or (), manual_lines_unapproved=int(prior.get("manual_lines_unapproved") or 0),
    )
    summary["evaluated"] = list(prior.get("evaluated") or [])
    sb.table(RUNS_TABLE).update({"gate_summary": summary}).eq("id", run["id"]).execute()
    try:
        from app.services import plans_reader_runs

        plans_reader_runs.recompute_autonomy(sb, str(run["id"]))
    except Exception:  # noqa: BLE001 - the autonomy state is a display layer
        logger.exception("plans_reader_checks: autonomy recompute for run %s failed", run.get("id"))
    return summary


@router.post("/runs/{run_id}/checks/findings/{finding_id}/override")
def override_finding(run_id: str, finding_id: str, body: OverrideIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    if user.role not in OVERRIDE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an Estimating Admin, Executive or IT Admin may override a check")
    run_id = _uuid_or_404(run_id, "Run")
    reason = body.reason.strip()
    if len(reason) < MIN_REASON_CHARS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Give a reason of at least {MIN_REASON_CHARS} characters")
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    rows = _finding_rows(sb, run_id)
    row = next((r for r in rows if str(r.get("id")) == str(finding_id)), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_FINDING_NOT_FOUND)
    if row.get("status") not in ACTIVE_FINDING_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This finding is {row.get('status')}; only an open or routed finding can be overridden")
    patch = {"status": "overridden", "resolved_by": user.id, "resolved_at": _now_iso(), "note": reason, "updated_at": _now_iso()}
    sb.table(FINDINGS_TABLE).update(patch).eq("id", row["id"]).execute()
    row.update(patch)
    summary = _recompute_summary(sb, run, rows)
    _audit(user, "ai_estimating_check_overridden", run_id, {"finding_id": row["id"], "gate_id": row.get("gate_id"), "reason": reason})
    return {"finding": _finding_view(row), "gate_summary": summary}


@router.post("/runs/{run_id}/checks/recount", status_code=status.HTTP_202_ACCEPTED)
def request_recount(run_id: str, body: RecountIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    s = get_settings()
    if getattr(s, "is_production", True):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if user.role not in RECOUNT_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only or insufficient role")
    run_id = _uuid_or_404(run_id, "Run")
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    rows = _finding_rows(sb, run_id)
    matches: list[dict[str, Any]] = []
    for r in rows:
        if r.get("gate_id") != "census_floor" or r.get("status") not in ACTIVE_FINDING_STATUSES:
            continue
        target = r.get("target") if isinstance(r.get("target"), Mapping) else {}
        if body.finding_id and str(r.get("id")) != str(body.finding_id):
            continue
        if body.sheet_key and str(target.get("sheet_key") or "") != body.sheet_key:
            continue
        if body.family and str(target.get("family") or "") != body.family:
            continue
        matches.append(r)
    if not matches:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No open census_floor finding matches this request")
    note = body.note.strip() or None
    for r in matches:
        patch = {"status": "routed", "route": "recount", "note": note or r.get("note"), "updated_at": _now_iso()}
        sb.table(FINDINGS_TABLE).update(patch).eq("id", r["id"]).execute()
        r.update(patch)
    queued: list[Any] = []
    if RECOUNT_REQUESTER is not None:
        try:
            queued = list(RECOUNT_REQUESTER(sb, run, [GateFinding.from_row(r) for r in matches], user, note) or [])
        except Exception:  # noqa: BLE001 - the request stays recorded on the findings
            logger.exception("plans_reader_checks: recount requester failed for run %s", run_id)
    _audit(user, "ai_estimating_recount_requested", run_id, {"finding_ids": [r["id"] for r in matches], "note": note})
    return {"requested": [_finding_view(r) for r in matches], "queued": len(queued)}


__all__ = ["router", "gate_statuses", "GATE_STATUSES", "MIN_REASON_CHARS", "OVERRIDE_ROLES", "RECOUNT_ROLES", "RECOUNT_REQUESTER"]
