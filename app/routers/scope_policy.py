"""G3 scope policy routes (docs/AI_ESTIMATING_BUILD_SPEC.md WS5, Q20, Q24, Q25).

Prefix `/ai-estimating/scope-policy`, behind the AI Estimating gates
(`require_plans_reader` 404 while PLANS_READER_ENABLED is off,
`require_internal`, `require_ai_estimating`).

- GET    ""                          the active policy (code seed until a row is active)
- POST   /validate                   validate a policy document (no write)
- GET    /drafts, POST /drafts       list or create drafts       Estimating Admin, Executive, IT Admin; live only
- GET    /drafts/{id}, PUT /drafts/{id}  read or edit a draft    same
- POST   /drafts/{id}/activate       activate a valid draft      VERIFY_ROLES (Executive, IT Admin); live only
- POST   /preview                    enforce a run's latest main output with the active policy or a draft,
                                     nothing written             draft editors
- GET    /runs/{run_id}              the run's ScopeContext summary and scope report
- GET    /runs/{run_id}/overrides    the run's scope overrides
- POST   /runs/{run_id}/overrides    record an include, exclude or accept override with a written reason of
                                     at least 10 characters      AI_GATE_OVERRIDE_ROLES (Q20);
                                     with AI_SCOPE_POLICY_ENFORCE on it is applied at once to the
                                     latest specs (scope_engine.apply_override)

Draft routes answer 404 unless AI_ESTIMATING_STATE=live (Q24). Proposal lines
in the report are suggestions only; nothing here writes a proposal (Q25).
Every handler is plain `def` (sync Supabase SDK in the threadpool).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_ai_estimating, require_internal
from app.core.features import require_plans_reader
from app.core.roles import AI_GATE_OVERRIDE_ROLES, VERIFY_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import scope_engine, scope_policy
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai-estimating/scope-policy",
    tags=["ai-estimating-scope"],
    dependencies=[Depends(require_plans_reader), Depends(require_internal), Depends(require_ai_estimating)],
)

DRAFT_EDIT_ROLES: frozenset[Role] = frozenset({Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN})
ACTIVATE_ROLES: frozenset[Role] = VERIFY_ROLES
OVERRIDE_ROLES: frozenset[Role] = AI_GATE_OVERRIDE_ROLES
MIN_REASON_CHARS = 10
TABLE = "ai_scope_policies"
OVERRIDES_TABLE = "plans_reader_scope_overrides"


class PolicyDocIn(BaseModel):
    doc: dict[str, Any]


class DraftIn(BaseModel):
    doc: dict[str, Any]
    notes: str | None = Field(default=None, max_length=2000)
    based_on: str | None = None


class DraftUpdateIn(BaseModel):
    doc: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class PreviewIn(BaseModel):
    run_id: str
    draft_id: str | None = None
    doc: dict[str, Any] | None = None


class OverrideIn(BaseModel):
    decision_key: str = Field(min_length=3, max_length=300)
    action: Literal["include", "exclude", "accept"]
    note: str = Field(max_length=2000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None


def _require_roles(user: CurrentUser, roles: frozenset[Role]) -> None:
    if user.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")


def _require_live() -> None:
    if getattr(get_settings(), "ai_estimating_state", "dev") != "live":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def _validation(doc: Any) -> dict[str, Any]:
    errors = scope_policy.validation_errors(doc)
    if errors:
        return {"valid": False, "errors": errors, "sha256": None, "version": None, "rule_ids": []}
    info = scope_policy.validate(doc)
    return {"valid": True, "errors": [], "sha256": info.sha256, "version": info.version,
            "rule_ids": list(info.rule_ids), "size_bytes": info.size_bytes}


def _audit(user: CurrentUser, action: str, entity_id: str | None, payload: dict[str, Any] | None = None) -> None:
    try:
        audit(user.id, action, "ai_scope_policy", entity_id, payload)
    except Exception:  # noqa: BLE001 - an audit failure never blocks the action
        logger.exception("scope_policy: audit %s failed", action)


def _draft(sb: Any, draft_id: str) -> dict[str, Any]:
    rows = sb.table(TABLE).select("*").eq("id", draft_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    return dict(rows[0])


def _next_policy_version(sb: Any) -> int:
    rows = sb.table(TABLE).select("version").order("version", desc=True).limit(1).execute().data or []
    seed = scope_policy.seed_policy().version
    return max(int(rows[0]["version"]) if rows else 0, seed) + 1


@router.get("")
def get_active_policy() -> dict[str, Any]:
    active = scope_policy.active_policy()
    return {
        "policy_id": active.policy_id,
        "version": active.version,
        "sha256": active.sha256,
        "source": active.source,
        "doc": active.doc,
    }


@router.post("/validate")
def validate_policy(body: PolicyDocIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_roles(user, DRAFT_EDIT_ROLES)
    return _validation(body.doc)


@router.get("/drafts")
def list_drafts(user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_live()
    _require_roles(user, DRAFT_EDIT_ROLES)
    sb = get_supabase()
    rows = (sb.table(TABLE).select("id, version, status, sha256, based_on, notes, created_by, created_at, "
                                   "activated_by, activated_at").order("version", desc=True).execute().data or [])
    return {"policies": rows}


@router.post("/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(body: DraftIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_live()
    _require_roles(user, DRAFT_EDIT_ROLES)
    sb = get_supabase()
    version = _next_policy_version(sb)
    doc = dict(body.doc)
    doc["version"] = version
    check = _validation(doc)
    if not check["valid"]:
        raise HTTPException(422, {"message": "Invalid scope policy", "errors": check["errors"]})
    row = {k: v for k, v in {
        "version": version,
        "status": "draft",
        "doc": doc,
        "sha256": check["sha256"],
        "based_on": body.based_on,
        "notes": body.notes,
        "created_by": user.id,
    }.items() if v is not None}
    created = (sb.table(TABLE).insert(row).execute().data or [row])[0]
    _audit(user, "scope_policy_draft_created", created.get("id"), {"version": version, "sha256": check["sha256"]})
    return created


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_live()
    _require_roles(user, DRAFT_EDIT_ROLES)
    return _draft(get_supabase(), _uuid(draft_id, "Draft"))


@router.put("/drafts/{draft_id}")
def update_draft(draft_id: str, body: DraftUpdateIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_live()
    _require_roles(user, DRAFT_EDIT_ROLES)
    sb = get_supabase()
    did = _uuid(draft_id, "Draft")
    row = _draft(sb, did)
    if row.get("status") != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft can be edited")
    patch: dict[str, Any] = {}
    if body.doc is not None:
        doc = dict(body.doc)
        doc["version"] = int(row.get("version") or doc.get("version") or 1)
        check = _validation(doc)
        if not check["valid"]:
            raise HTTPException(422,
                                {"message": "Invalid scope policy", "errors": check["errors"]})
        patch.update({"doc": doc, "sha256": check["sha256"]})
    if body.notes is not None:
        patch["notes"] = body.notes
    if not patch:
        return row
    sb.table(TABLE).update(patch).eq("id", did).eq("status", "draft").execute()
    _audit(user, "scope_policy_draft_updated", did, {"sha256": patch.get("sha256")})
    return {**row, **patch}


@router.post("/drafts/{draft_id}/activate")
def activate_draft(draft_id: str, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_live()
    _require_roles(user, ACTIVATE_ROLES)
    sb = get_supabase()
    did = _uuid(draft_id, "Draft")
    row = _draft(sb, did)
    if row.get("status") != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft can be activated")
    check = _validation(row.get("doc"))
    if not check["valid"]:
        raise HTTPException(422,
                            {"message": "The draft fails validation and cannot be activated", "errors": check["errors"]})
    # One active row (partial unique index): archive the active row, then
    # activate the draft guarded on status draft, and check that exactly one
    # row was activated; otherwise the archived row is put back. Nothing is
    # pinned in this process: every worker reads the active row through the
    # table loader (short TTL); this process's cache is expired now.
    previous = [str(r["id"]) for r in (sb.table(TABLE).select("id").eq("status", "active").execute().data or [])
                if r.get("id") and str(r["id"]) != did]
    for pid in previous:
        sb.table(TABLE).update({"status": "archived"}).eq("id", pid).eq("status", "active").execute()
    stamp = {"status": "active", "activated_by": user.id, "activated_at": _now()}
    try:
        activated = sb.table(TABLE).update(stamp).eq("id", did).eq("status", "draft").execute().data or []
    except Exception:
        activated = None
        logger.exception("scope_policy: activating draft %s failed", did)
    if not activated or len(activated) != 1:
        for pid in previous:
            sb.table(TABLE).update({"status": "active"}).eq("id", pid).eq("status", "archived").execute()
        scope_engine.invalidate_policy_cache()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The draft could not be activated (it changed meanwhile); the active policy is unchanged")
    scope_engine.invalidate_policy_cache()
    _audit(user, "scope_policy_activated", did, {"version": row.get("version"), "sha256": check["sha256"],
                                                 "archived": previous})
    return {**row, **stamp, "sha256": check["sha256"]}


def _latest_main(sb: Any, run_id: str) -> dict[str, Any]:
    rows = (sb.table("plans_reader_outputs").select("output, version").eq("run_id", run_id).eq("scope", "main")
            .order("version", desc=True).limit(1).execute().data or [])
    if not rows or not isinstance(rows[0].get("output"), dict):
        raise HTTPException(status.HTTP_409_CONFLICT, "The run has no consolidated output yet")
    return rows[0]["output"]


@router.post("/preview")
def preview(body: PreviewIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_roles(user, DRAFT_EDIT_ROLES)
    sb = get_supabase()
    run_id = _uuid(body.run_id, "Run")
    if body.draft_id or body.doc is not None:
        _require_live()
    if body.draft_id:
        doc = _draft(sb, _uuid(body.draft_id, "Draft")).get("doc")
    elif body.doc is not None:
        doc = body.doc
    else:
        doc = scope_policy.active_policy().doc
    check = _validation(doc)
    if not check["valid"]:
        raise HTTPException(422, {"message": "Invalid scope policy", "errors": check["errors"]})
    main = _latest_main(sb, run_id)
    ctx = scope_engine.context_for_run(sb, run_id) or {}
    enforced = scope_engine.enforce(main, ctx, doc, scope_engine.overrides_for_run(sb, run_id),
                                    settings=get_settings())
    report = enforced.get("scope_policy") or {}
    return {
        "run_id": run_id,
        "policy_sha256": check["sha256"],
        "counts": report.get("counts"),
        "exclusions": report.get("exclusions"),
        "clarifications": report.get("clarifications"),
        "needs_review": report.get("needs_review"),
        "excluded_lines": [
            {k: e.get(k) for k in ("line_id", "description", "quantity", "unit", "reason", "source", "rule_id", "fact_id")}
            for e in enforced.get("excluded_lines") or []
        ],
        "proposal_lines": report.get("proposal_lines"),
    }


@router.get("/runs/{run_id}")
def run_scope(run_id: str) -> dict[str, Any]:
    sb = get_supabase()
    rid = _uuid(run_id, "Run")
    ctx = scope_engine.context_for_run(sb, rid)
    rows = (sb.table("plans_reader_outputs").select("output, version").eq("run_id", rid).eq("scope", "main")
            .order("version", desc=True).limit(1).execute().data or [])
    main = rows[0].get("output") if rows else None
    report = main.get("scope_policy") if isinstance(main, dict) else None
    summary = None
    if ctx:
        summary = {k: ctx.get(k) for k in ("policy_id", "policy_sha", "policy_version", "bid_conditions", "standards",
                                           "breaker_plan", "inclusions", "device_facts", "facts", "stats", "digest")}
    return {"run_id": rid, "context": summary, "report": report,
            "excluded_lines": (main or {}).get("excluded_lines") if isinstance(main, dict) else None}


@router.get("/runs/{run_id}/overrides")
def list_overrides(run_id: str) -> dict[str, Any]:
    rid = _uuid(run_id, "Run")
    return {"run_id": rid, "overrides": scope_engine.overrides_for_run(get_supabase(), rid)}


@router.post("/runs/{run_id}/overrides", status_code=status.HTTP_201_CREATED)
def add_override(run_id: str, body: OverrideIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _require_roles(user, OVERRIDE_ROLES)
    note = (body.note or "").strip()
    if len(note) < MIN_REASON_CHARS:
        raise HTTPException(422,
                            f"A written reason of at least {MIN_REASON_CHARS} characters is required")
    sb = get_supabase()
    rid = _uuid(run_id, "Run")
    run = sb.table("plans_reader_runs").select("id").eq("id", rid).limit(1).execute().data or []
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    row = {"run_id": rid, "decision_key": body.decision_key, "action": body.action, "note": note,
           "created_by": user.id}
    saved = (sb.table(OVERRIDES_TABLE).upsert(row, on_conflict="run_id,decision_key").execute().data or [row])[0]
    _audit(user, "scope_override", rid, {"decision_key": body.decision_key, "action": body.action})
    # Apply only this decision's delta to the latest specs now (the BOQ,
    # estimate and brief whose spec changes get a new version). Off while
    # AI_SCOPE_POLICY_ENFORCE is off; never fails the request.
    result = scope_engine.apply_override(sb, rid, row, settings=get_settings())
    applies = "now" if result.get("applied") else "at the next enforcement of this run"
    return {**saved, "applies": applies, "applied": result}


__all__ = ["router", "DRAFT_EDIT_ROLES", "ACTIVATE_ROLES", "OVERRIDE_ROLES", "MIN_REASON_CHARS"]
