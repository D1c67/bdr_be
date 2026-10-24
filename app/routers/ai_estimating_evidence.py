"""AI Estimating evidence, autonomy, sign-off and annotated plans routes
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS8 "routes", 5.4 AutonomyStatus,
5.5 visibility, 5.6 MG): a separate router under the `/ai-estimating` prefix
because `routers/plans_reader.py` is a hot file of WS4 in this wave.

Gates, in order: `require_plans_reader` (404 while PLANS_READER_ENABLED is
off, before auth), `require_internal` (the external estimator is refused
everywhere), `require_ai_estimating` (AI_ESTIMATING_STATE's roles). Money
never leaves this router to a caller who fails `ai_pricing_allowed`: the
evidence routes attach price chips only for pricing viewers (and the stored
evidence carries no money at all, plans_reader_evidence.money_keys), the
autonomy route drops the pricing params and the sign-off note for them,
and the sign-off POST takes `require_pricing_view` plus a Verify role.

Routes:

- GET runs/{run_id}/evidence: the latest stored evidence output (`?sheet=`
  keeps one sheet's marks; `?sheets=0` drops every sheet for a light read);
- GET runs/{run_id}/evidence/lines/{line_id}: one line with its marks on
  every sheet and the open gate findings on it;
- GET runs/{run_id}/evidence/crop.png: a PNG crop of one document page
  (page fractions, `ppi` capped) for the evidence panel; recorded in
  file_access_events as a `view` (the 0161 CHECK allows view, download,
  export and prefetch) with the crop and ppi in its detail;
- GET runs/{run_id}/review: the latest independent review result
  (plans_reader_review.result_for; 404 before any review); a caller who
  fails `ai_pricing_allowed` gets plans_reader_review.redact (money bands
  nulled, no impact dollars, no cost) with every money-looking key dropped;
- GET runs/{run_id}/autonomy: plans_reader_autonomy.status for the
  caller's audience;
- POST runs/{run_id}/signoff (VERIFY_ROLES who may see prices): records
  `verified` or `rejected` on plans_reader_price_signoffs with the autonomy
  and totals snapshots; `verified` needs `ready_for_verification` or an
  explicit note per accepted failing check (plans_reader_autonomy
  .validate_signoff); audited; the autonomy state is recomputed;
- GET runs/{run_id}/annotated-plans, GET .../{version}/download, GET
  .../{version}/preview-url and POST .../rerender: the `annotated_plans`
  code kind (plans_reader_render_evidence.CODE_KINDS): download, preview
  and re-render from the stored evidence only, never a modify or a chat
  change (the legacy deliverable routes and chat refuse the kind).

Every handler is a plain `def` (the sync Supabase SDK and storage never run
inside `async def`). Service errors map like routers/plans_reader.py: a
declared 4xx status wins, LookupError is 404, a ValueError on an action is
409 (input routes 400).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, require_ai_estimating, require_internal, require_pricing_view, require_writer
from app.core.features import ai_pricing_allowed, require_plans_reader
from app.core.roles import VERIFY_ROLES
from app.core.supabase_client import get_supabase
from app.services import file_access, storage
from app.services import plans_reader_autonomy as autonomy
from app.services import plans_reader_evidence as evidence_mod
from app.services import plans_reader_images as images
from app.services import plans_reader_render_evidence as render_evidence
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai-estimating",
    tags=["ai-estimating-evidence"],
    dependencies=[Depends(require_plans_reader), Depends(require_internal), Depends(require_ai_estimating)],
)

MSG_RUN_NOT_FOUND = "Run not found"
MSG_NO_EVIDENCE = "This run has no evidence yet."
MSG_LINE_NOT_FOUND = "Line not found"
MSG_DOC_NOT_FOUND = "Document not found"
MSG_NOT_FINISHED = "Annotated plans can be rendered once the run has finished."
MSG_CHANGE_ACTIVE = "Another change is active on this run; try again when it finishes."
MSG_VERIFY_ROLE = "Only the Verify roles may sign off a priced estimate"
MSG_NO_PRICING = "This run has no priced version to sign off."
CROP_MIN_PPI = 30
CROP_MAX_PPI = 200
CROP_MAX_PATCHES = 1500
SOURCE_DOCUMENT = "ai_estimating_document"
SOURCE_DELIVERABLE = "ai_estimating_deliverable"
_FINISHED = ("done", "done_with_errors")
_RUN_COLUMNS = "*"


# ── Helpers ───────────────────────────────────────────────────────────────


def _uuid_or_404(value: str, what: str = "Run") -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None
    return str(value)


def _first(sb: Any, table: str, **eq: Any) -> dict | None:
    query = sb.table(table).select(_RUN_COLUMNS)
    for col, val in eq.items():
        query = query.eq(col, val)
    rows = query.limit(1).execute().data or []
    return rows[0] if rows else None


def _run_or_404(sb: Any, run_id: str) -> dict:
    _uuid_or_404(run_id)
    run = _first(sb, "plans_reader_runs", id=run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_RUN_NOT_FOUND)
    return run


def _pricing_viewer(user: CurrentUser) -> bool:
    try:
        return bool(ai_pricing_allowed(user.role))
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _evidence_or_404(sb: Any, run_id: str) -> dict:
    data = render_evidence.latest_evidence(sb, run_id)
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NO_EVIDENCE)
    return data


def _scrub(data: Any) -> Any:
    """Drop any money-looking key (plans_reader_evidence.MONEY_KEY_RE) from a
    payload bound for a reader outside the pricing roles. The stored
    evidence has none by contract; this is the belt to that suspender."""
    if isinstance(data, dict):
        return {k: _scrub(v) for k, v in data.items() if not evidence_mod.MONEY_KEY_RE.search(str(k))}
    if isinstance(data, list):
        return [_scrub(v) for v in data]
    return data


def _priced_lines(sb: Any, run_id: str) -> list[dict]:
    try:
        from app.services import pricing_run

        latest = pricing_run.latest_version(sb, run_id)
        if not latest:
            return []
        return [r.get("line") for r in pricing_run.version_lines(sb, str(latest["id"])) if isinstance(r.get("line"), dict)]
    except Exception:  # noqa: BLE001 - before ME, or no pricing yet
        logger.info("evidence routes: priced lines of run %s unavailable", run_id, exc_info=True)
        return []


def _with_audience(sb: Any, run_id: str, data: dict, user: CurrentUser) -> tuple[dict, bool]:
    """(payload, pricing_visible): price chips attached for pricing viewers,
    money keys scrubbed for everyone else."""
    if _pricing_viewer(user):
        return evidence_mod.attach_price_chips(data, _priced_lines(sb, run_id)), True
    return _scrub(data), False


def _gate_findings(sb: Any, run_id: str) -> list[dict]:
    try:
        rows = sb.table("plans_reader_gate_findings").select("*").eq("run_id", run_id).execute().data or []
    except Exception:  # noqa: BLE001 - before MC
        return []
    return [r for r in rows if isinstance(r, dict)]


def _findings_on_line(findings: list[dict], line_id: str) -> list[dict]:
    out = []
    for f in findings:
        if f.get("status") not in ("open", "routed"):
            continue
        target = f.get("target") if isinstance(f.get("target"), dict) else {}
        ids = [target.get("line_id")] + list(target.get("row_ids") or [])
        if line_id in ids:
            out.append(
                {"gate_id": f.get("gate_id"), "severity": f.get("severity"), "status": f.get("status"),
                 "stage": f.get("stage"), "message": str(f.get("message") or "")[:400]}
            )
    return out


def _deliverable_out(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "storage_path"}


# ── Evidence ──────────────────────────────────────────────────────────────


@router.get("/runs/{run_id}/evidence")
def get_evidence(
    run_id: str,
    sheet: str | None = Query(default=None, max_length=40),
    sheets: int = Query(default=1, ge=0, le=1),
    user: CurrentUser = Depends(require_internal),
) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    data = _evidence_or_404(sb, run_id)
    payload, visible = _with_audience(sb, run_id, data, user)
    if not sheets:
        payload["sheets"] = []
    elif sheet:
        payload["sheets"] = [s for s in payload.get("sheets") or [] if s.get("sheet_key") == sheet]
    payload["pricing_visible"] = visible
    payload["annotated_plans"] = _deliverable_out(render_evidence.latest_annotated(sb, run_id) or {}) or None
    return payload


@router.get("/runs/{run_id}/evidence/lines/{line_id}")
def get_evidence_line(run_id: str, line_id: str, user: CurrentUser = Depends(require_internal)) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    data = _evidence_or_404(sb, run_id)
    payload, visible = _with_audience(sb, run_id, data, user)
    line = next((ln for ln in payload.get("lines") or [] if ln.get("line_id") == line_id), None)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_LINE_NOT_FOUND)
    marks = []
    for s in payload.get("sheets") or []:
        for m in s.get("marks") or []:
            if m.get("line_id") == line_id:
                marks.append({**m, "sheet_key": s.get("sheet_key"), "sheet_number": s.get("sheet_number"),
                              "document_id": s.get("document_id"), "page": s.get("page"),
                              "w_in": s.get("w_in"), "h_in": s.get("h_in")})
    return {
        "line": line,
        "marks": marks,
        "gate_findings": _findings_on_line(_gate_findings(sb, run_id), line_id),
        "pricing_visible": visible,
    }


@router.get("/runs/{run_id}/evidence/crop.png")
def evidence_crop(
    run_id: str,
    document_id: str = Query(...),
    page: int = Query(..., ge=1),
    x0: float = Query(..., ge=0.0, le=1.0),
    y0: float = Query(..., ge=0.0, le=1.0),
    x1: float = Query(..., ge=0.0, le=1.0),
    y1: float = Query(..., ge=0.0, le=1.0),
    ppi: int = Query(default=100, ge=CROP_MIN_PPI, le=CROP_MAX_PPI),
    user: CurrentUser = Depends(require_internal),
) -> Response:
    """A PNG crop of one page of one run document, in displayed page
    fractions (origin top-left). The crop is rendered through PDFium's
    bitmap crop, so a 200 ppi view of a 42 x 30 sheet never rasterizes the
    whole page; the patch cap bounds the pixels."""
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    _uuid_or_404(document_id, "Document")
    doc = _first(sb, "plans_reader_documents", id=document_id, run_id=run_id)
    if not doc or not doc.get("storage_path"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_DOC_NOT_FOUND)
    if x1 <= x0 or y1 <= y0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The crop region is empty.")
    count = int(doc.get("page_count") or 0)
    if count and page > count:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    pdf = storage.download_file(str(doc["storage_path"]))
    try:
        sizes = images.page_sizes_in(pdf)
        if page > len(sizes):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
        w_in, h_in = sizes[page - 1]
        w_px, h_px = int(round(w_in * ppi)), int(round(h_in * ppi))
        box = images.box_from_fractions((x0, y0, x1, y1), w_px, h_px)
        png, _w, _h = images.render_page_png(pdf, page - 1, ppi, box, max_patches=CROP_MAX_PATCHES)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    file_access.record(
        user,
        action="view",
        source=SOURCE_DOCUMENT,
        file_id=None,
        project_id=run.get("project_id"),
        filename=doc.get("name"),
        category=doc.get("category"),
        detail={"run_id": run_id, "document_id": document_id, "page": page, "crop": [x0, y0, x1, y1], "ppi": ppi},
    )
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


# ── Independent review ────────────────────────────────────────────────────

MSG_NO_REVIEW = "This run has no independent review yet."


@router.get("/runs/{run_id}/review")
def get_review(run_id: str, user: CurrentUser = Depends(require_internal)) -> dict:
    """The latest independent review of the run (WS7 ReviewResult, spec
    5.4) for the caller's audience (5.5): a pricing viewer reads it whole;
    everyone else gets plans_reader_review.redact (money bands nulled, no
    impact dollars, no revision dollar move, no cost) and then the money
    key scrub the evidence routes use. 404 until a review exists."""
    from app.services import plans_reader_review as review_mod

    sb = get_supabase()
    _run_or_404(sb, run_id)
    result = review_mod.result_for(sb, run_id)
    if not result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NO_REVIEW)
    visible = _pricing_viewer(user)
    payload = dict(result) if visible else _scrub(review_mod.redact(result))
    payload["pricing_visible"] = visible
    return payload


# ── Autonomy and sign-off ─────────────────────────────────────────────────


@router.get("/runs/{run_id}/autonomy")
def get_autonomy(run_id: str, user: CurrentUser = Depends(require_internal)) -> dict:
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    st = autonomy.status(sb, run, include_pricing=_pricing_viewer(user))
    out = st.to_dict()
    out["pricing_visible"] = _pricing_viewer(user)
    out["can_sign_off"] = bool(user.role in VERIFY_ROLES and _pricing_viewer(user))
    return out


class SignoffIn(BaseModel):
    decision: str = Field(pattern="^(verified|rejected)$")
    pricing_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=4000)
    accepted: dict[str, str] = Field(default_factory=dict)


def _totals_snapshot(sb: Any, run_id: str, pricing_version: int) -> dict:
    """Hours, material, Verify bid and bid total of the signed version
    (plans_reader_pricing.verify_draft; migration 0169 comment)."""
    try:
        from app.services import pricing_run

        rows = [r for r in pricing_run.versions(sb, run_id) if int(r.get("version") or 0) == pricing_version]
    except Exception:  # noqa: BLE001 - before ME
        rows = []
    if not rows:
        return {}
    row = rows[0]
    draft = row.get("verify_draft") if isinstance(row.get("verify_draft"), dict) else {}
    keys = ("labor_hours_basis", "labor_hours_reviewer", "labor_rate", "labor_amount", "materials_amount",
            "gear_amount", "underground_amount", "low_voltage_amount", "bid_total")
    return {
        "pricing_id": row.get("id"),
        "pricing_version": pricing_version,
        "status": row.get("status"),
        "boq_version": row.get("boq_version"),
        "workbook_basis": row.get("workbook_basis"),
        "range_low": row.get("range_low"),
        "range_high": row.get("range_high"),
        **{k: draft.get(k) for k in keys},
    }


@router.post("/runs/{run_id}/signoff", status_code=status.HTTP_201_CREATED)
def post_signoff(run_id: str, body: SignoffIn, user: CurrentUser = Depends(require_pricing_view)) -> dict:
    if user.role not in VERIFY_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, MSG_VERIFY_ROLE)
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    st = autonomy.status(sb, run, include_pricing=True)
    if st.pricing_version is None:
        raise HTTPException(status.HTTP_409_CONFLICT, MSG_NO_PRICING)
    errors = autonomy.validate_signoff(st, body.decision, body.note, accepted=body.accepted,
                                       pricing_version=body.pricing_version)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(errors))
    totals = _totals_snapshot(sb, run_id, body.pricing_version)
    row = autonomy.signoff_row(run_id, body.pricing_version, body.decision, body.note, st, totals,
                               created_by=user.id, accepted=body.accepted)
    inserted = sb.table("plans_reader_price_signoffs").insert(row).execute().data[0]
    audit(user.id, f"ai_pricing.signoff_{body.decision}", "plans_reader_run", run_id,
          {"pricing_version": body.pricing_version, "overall": st.overall, "accepted": sorted(body.accepted)})
    try:
        autonomy.recompute(sb, run_id)
    except Exception:  # noqa: BLE001 - the sign-off stands; the state is display only
        logger.exception("evidence routes: autonomy recompute after sign-off failed for run %s", run_id)
    return {
        "id": inserted.get("id"),
        "decision": body.decision,
        "pricing_version": body.pricing_version,
        "created_by": user.id,
        "created_at": autonomy.pacific(inserted.get("created_at")),
        "autonomy": autonomy.status(sb, run, include_pricing=True).to_dict(),
    }


# ── Annotated plans (code kind) ───────────────────────────────────────────


def _annotated_or_404(sb: Any, run_id: str, version: int) -> dict:
    row = _first(sb, "plans_reader_deliverables", run_id=run_id, kind=render_evidence.ANNOTATED_KIND, version=version)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deliverable not found")
    return row


@router.get("/runs/{run_id}/annotated-plans")
def list_annotated(run_id: str, user: CurrentUser = Depends(require_internal)) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    rows = (
        sb.table("plans_reader_deliverables").select("*").eq("run_id", run_id).eq("kind", render_evidence.ANNOTATED_KIND)
        .execute().data or []
    )
    rows.sort(key=lambda r: -int(r.get("version") or 0))
    return {"run_id": run_id, "kind": render_evidence.ANNOTATED_KIND, "versions": [_deliverable_out(r) for r in rows]}


@router.get("/runs/{run_id}/annotated-plans/{version}/download")
def download_annotated(run_id: str, version: int, user: CurrentUser = Depends(require_internal)) -> dict:
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    row = _annotated_or_404(sb, run_id, version)
    name = str(row.get("filename") or render_evidence.ANNOTATED_FILENAME)
    url = storage.signed_url(str(row["storage_path"]), download=name)
    file_access.record(
        user, action="download", source=SOURCE_DELIVERABLE, file_id=None, project_id=run.get("project_id"),
        filename=name, category=render_evidence.ANNOTATED_KIND,
        detail={"run_id": run_id, "kind": render_evidence.ANNOTATED_KIND, "version": version, "format": "original"},
    )
    return {"url": url}


@router.get("/runs/{run_id}/annotated-plans/{version}/preview-url")
def preview_annotated(run_id: str, version: int, user: CurrentUser = Depends(require_internal)) -> dict:
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    row = _annotated_or_404(sb, run_id, version)
    name = str(row.get("filename") or render_evidence.ANNOTATED_FILENAME)
    url = storage.signed_url(str(row["storage_path"]), download=False)
    file_access.record(
        user, action="view", source=SOURCE_DELIVERABLE, file_id=None, project_id=run.get("project_id"),
        filename=name, category=render_evidence.ANNOTATED_KIND,
        detail={"run_id": run_id, "kind": render_evidence.ANNOTATED_KIND, "version": version, "format": "preview"},
    )
    return {"preview_status": "ready", "url": url, "filename": name}


@router.post("/runs/{run_id}/annotated-plans/rerender", status_code=status.HTTP_201_CREATED)
def rerender_annotated(run_id: str, user: CurrentUser = Depends(require_writer)) -> dict:
    """Render the next annotated plans version from the stored evidence
    (code only, no model). Takes the run's one change slot for the render
    so it never overlaps a modify or a re-render, and releases it after."""
    from app.services import plans_reader_runs as runs

    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") not in _FINISHED:
        raise HTTPException(status.HTTP_409_CONFLICT, MSG_NOT_FINISHED)
    try:
        group = runs.claim_change_slot(
            sb, run_id, output={"kind": render_evidence.ANNOTATED_KIND, "rerender": True, "inline": True}, status="running"
        )
    except runs.RunRefused as exc:
        raise HTTPException(int(getattr(exc, "status_code", 409) or 409), str(exc) or MSG_CHANGE_ACTIVE) from exc
    row: dict | None = None
    error: str | None = None
    try:
        row = render_evidence.rerender_from_evidence(sb, run, user=user)
    except LookupError as exc:
        error = str(exc)
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except render_evidence.AnnotatedPlansStoreError as exc:
        error = str(exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        error = str(exc)
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    finally:
        patch = {
            "status": "done" if row else "failed",
            "error": None if row else (error or "The annotated plans could not be rendered."),
            "finished_at": autonomy.now_pacific_iso(),
            "output": {"kind": render_evidence.ANNOTATED_KIND, "rerender": True, "inline": True,
                       **({"deliverable_id": row.get("id"), "version": row.get("version")} if row else {})},
        }
        try:
            sb.table("plans_reader_groups").update(patch).eq("id", group["id"]).in_("status", ["queued", "running"]).execute()
        except Exception:  # noqa: BLE001 - plans_reader_runs.release_orphan_changes sweeps a stuck slot
            logger.exception("evidence routes: closing the change slot %s failed", group.get("id"))
    audit(user.id, "ai_estimating.annotated_plans_rerendered", "plans_reader_run", run_id,
          {"version": row.get("version"), "deliverable_id": row.get("id")})
    return _deliverable_out(row)
