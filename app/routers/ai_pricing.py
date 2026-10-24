"""AI pricing routes (AI_ESTIMATING_BUILD_SPEC WS6, 5.5): /ai-estimating/pricing.

Gates, in order: `require_plans_reader` (404 while PLANS_READER_ENABLED
is off), `require_ai_estimating` (the AI Estimating state's roles; the
external estimator is refused), `require_pricing_view` (404 while
AI_PRICING_ENABLED is off, 403 for a role AI_PRICING_STATE does not admit:
the accountant never, the engineers only in `live`). Writes add
`require_pricing_write`; the policy PUT needs a Verify role
(AI_PRICING_POLICY_ROLES) and is audited.

Routes:

- POST runs/{run_id}/price: queue the ai_pricing job (202); 409 while one
  is already queued or running for the run;
- GET runs/{run_id}: the latest version (no lines), every version's
  summary, the active job and the quote count;
- GET versions/{pricing_id}/lines: the priced lines of one version;
- GET versions/{pricing_id}/xlsx: a signed link to the priced workbook,
  recorded in file_access_events with source ai_pricing_estimate;
- POST and GET runs/{run_id}/quotes: upload a vendor quote PDF into the
  run's private storage (parsed from its text layer at once; the LLM
  fallback runs only inside the pricing job) and list the run's quotes;
  GET runs/{run_id}/quotes/{quote_id}/download records source
  ai_pricing_quote; POST .../select marks the human-selected quote;
- GET lookup: `price_lookup` over the cost library (aggregates only);
- GET and PUT settings: the pricing policy row.

Every route is a plain `def` (the sync Supabase SDK never runs inside
`async def`). The priced workbook is not a deliverable kind: it is served
only here (spec 5.6).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_ai_estimating, require_pricing_view, require_pricing_write
from app.core.features import require_plans_reader
from app.core.roles import AI_PRICING_POLICY_ROLES
from app.core.supabase_client import get_supabase
from app.services import file_access, pricing_run, pricing_tools, storage
from app.services import vendor_quotes as vq
from app.services.cost_library import CostLibrary, LibraryUnavailable
from app.services.notifications import audit
from app.services.plans_reader_schemas import MATERIAL_CATEGORIES

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai-estimating/pricing",
    tags=["ai-pricing"],
    dependencies=[Depends(require_plans_reader), Depends(require_ai_estimating), Depends(require_pricing_view)],
)

SOURCE_ESTIMATE = "ai_pricing_estimate"
SOURCE_QUOTE = "ai_pricing_quote"
QUOTE_MAX_BYTES = 25 * 1024 * 1024
QUOTE_FOLDER = "quotes"
LIBRARY_TTL_S = 300
MSG_RUN_NOT_FOUND = "AI Estimating run not found"
MSG_VERSION_NOT_FOUND = "Pricing version not found"
MSG_QUOTE_NOT_FOUND = "Vendor quote not found"
MSG_JOB_ACTIVE = "A pricing job is already queued or running for this run."
_LIBRARY_CACHE: dict[str, tuple[float, CostLibrary]] = {}


def _uuid_or_404(value: str, what: str) -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None
    return str(value)


def _run_or_404(sb: Any, run_id: str) -> dict:
    _uuid_or_404(run_id, "AI Estimating run")
    try:
        return pricing_run.load_run(sb, run_id)
    except pricing_run.PricingRefused as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_RUN_NOT_FOUND) from exc


def _refused(exc: pricing_run.PricingRefused) -> HTTPException:
    return HTTPException(int(exc.status_code), str(exc))


def _version_or_404(sb: Any, pricing_id: str) -> dict:
    _uuid_or_404(pricing_id, "Pricing version")
    row = pricing_run.version_row(sb, pricing_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_VERSION_NOT_FOUND)
    return row


# ── Pricing a run ─────────────────────────────────────────────────────────


@router.post("/runs/{run_id}/price", status_code=status.HTTP_202_ACCEPTED)
def price_run(run_id: str, user: CurrentUser = Depends(require_pricing_write)) -> dict:
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    if run.get("status") not in pricing_run.PRICEABLE_RUN_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Pricing needs a finished run (quantities complete).")
    active = pricing_run.active_job(sb, run_id)
    if active:
        raise HTTPException(status.HTTP_409_CONFLICT, MSG_JOB_ACTIVE)
    try:
        job = pricing_run.enqueue_pricing(sb, run, user=user, settings=get_settings(), reason="manual")
    except pricing_run.PricingRefused as exc:
        raise _refused(exc) from exc
    audit(user.id, "ai_pricing.price_requested", "plans_reader_run", run_id, {"job_id": job.get("id")})
    return {"run_id": run_id, "job": {"id": job.get("id"), "status": job.get("status")}}


@router.get("/runs/{run_id}")
def run_pricing(run_id: str, user: CurrentUser = Depends(require_pricing_view)) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    return pricing_run.pricing_status(sb, run_id)


VERSION_DETAIL_KEYS: tuple[str, ...] = ("categories", "quality_summary", "unpriced")


@router.get("/versions/{pricing_id}/lines")
def version_lines(pricing_id: str, user: CurrentUser = Depends(require_pricing_view)) -> dict:
    sb = get_supabase()
    row = _version_or_404(sb, pricing_id)
    lines = pricing_run.version_lines(sb, pricing_id)
    # The pricing tab reads the chosen quote per category, the reconciliation
    # (quality_summary) and the unpriced lines here; summary_row drops them
    # from the light listings (pricing_run._HEAVY).
    version = {**(pricing_run.summary_row(row) or {}), **{k: row.get(k) for k in VERSION_DETAIL_KEYS}}
    return {"version": version, "lines": [ln.get("line") for ln in lines]}


@router.get("/versions/{pricing_id}/xlsx")
def version_xlsx(pricing_id: str, user: CurrentUser = Depends(require_pricing_view)) -> dict:
    sb = get_supabase()
    row = _version_or_404(sb, pricing_id)
    path = row.get("storage_path")
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This pricing version has no workbook")
    run = _run_or_404(sb, str(row["run_id"]))
    name = row.get("filename") or f"ai-priced-estimate-v{row.get('version')}.xlsx"
    url = storage.signed_url(path, download=name)
    file_access.record(
        user,
        action="download",
        source=SOURCE_ESTIMATE,
        file_id=str(row["id"]),
        project_id=run.get("project_id"),
        filename=name,
        category="ai_pricing",
        detail={"run_id": str(row["run_id"]), "version": row.get("version")},
    )
    return {"url": url}


# ── Vendor quotes ─────────────────────────────────────────────────────────


def _quote_out(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in ("storage_path",)}
    return out


@router.post("/runs/{run_id}/quotes", status_code=status.HTTP_201_CREATED)
def upload_quote(
    run_id: str,
    file: UploadFile = File(...),
    category: str = Form("Lighting"),
    user: CurrentUser = Depends(require_pricing_write),
) -> dict:
    if category not in MATERIAL_CATEGORIES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown material category")
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    data = file.file.read(QUOTE_MAX_BYTES + 1)
    if len(data) > QUOTE_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Quote file is too large (limit 25 MB).")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Vendor quotes must be PDF files.")
    filename = storage.safe_key_component(file.filename or "quote.pdf")
    digest = vq.sha256_bytes(data)
    existing = (
        sb.table("vendor_quote_extractions").select("*").eq("run_id", run_id).eq("file_sha256", digest).limit(1).execute()
    ).data or []
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "This quote file is already on the run.")
    path = storage.build_plans_reader_path(run_id, QUOTE_FOLDER, filename)
    storage.upload_file(path, data, "application/pdf")
    parsed = vq.parse_quote_pdf(data, filename=filename)  # text layer only; the LLM fallback runs in the job
    row = {
        "run_id": run_id,
        "file_sha256": digest,
        "filename": filename,
        "storage_path": path,
        "size_bytes": len(data),
        "category": category,
        "created_by": user.id,
        **parsed.to_row(),
    }
    inserted = sb.table("vendor_quote_extractions").insert(row).execute().data[0]
    audit(user.id, "ai_pricing.quote_uploaded", "plans_reader_run", run_id, {
        "quote_id": inserted.get("id"), "category": category, "status": parsed.status, "project_id": run.get("project_id"),
    })
    return _quote_out(inserted)


@router.get("/runs/{run_id}/quotes")
def list_quotes(run_id: str, user: CurrentUser = Depends(require_pricing_view)) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    rows = pricing_run.run_quotes(sb, run_id)
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return {"run_id": run_id, "quotes": [_quote_out(r) for r in rows]}


@router.get("/runs/{run_id}/quotes/{quote_id}/download")
def download_quote(run_id: str, quote_id: str, user: CurrentUser = Depends(require_pricing_view)) -> dict:
    sb = get_supabase()
    run = _run_or_404(sb, run_id)
    _uuid_or_404(quote_id, "Vendor quote")
    rows = sb.table("vendor_quote_extractions").select("*").eq("id", quote_id).eq("run_id", run_id).limit(1).execute().data or []
    if not rows or not rows[0].get("storage_path"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_QUOTE_NOT_FOUND)
    row = rows[0]
    name = row.get("filename") or "quote.pdf"
    url = storage.signed_url(row["storage_path"], download=name)
    file_access.record(
        user,
        action="download",
        source=SOURCE_QUOTE,
        file_id=str(row["id"]),
        project_id=run.get("project_id"),
        filename=name,
        category=row.get("category"),
        detail={"run_id": run_id, "vendor": row.get("vendor")},
    )
    return {"url": url}


class QuoteSelect(BaseModel):
    selected: bool = True


@router.post("/runs/{run_id}/quotes/{quote_id}/select")
def select_quote(
    run_id: str, quote_id: str, body: QuoteSelect, user: CurrentUser = Depends(require_pricing_write)
) -> dict:
    sb = get_supabase()
    _run_or_404(sb, run_id)
    _uuid_or_404(quote_id, "Vendor quote")
    rows = sb.table("vendor_quote_extractions").select("*").eq("id", quote_id).eq("run_id", run_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_QUOTE_NOT_FOUND)
    updated = sb.table("vendor_quote_extractions").update({"selected": bool(body.selected)}).eq("id", quote_id).execute().data or []
    audit(user.id, "ai_pricing.quote_selected", "plans_reader_run", run_id, {"quote_id": quote_id, "selected": body.selected})
    return _quote_out(updated[0] if updated else {**rows[0], "selected": body.selected})


# ── Lookup ────────────────────────────────────────────────────────────────


def _library(sb: Any) -> CostLibrary:
    """The cost library over the ME tables, cached per process for
    LIBRARY_TTL_S (the holdout is excluded; a lookup is never leave-one-out
    because it belongs to no run)."""
    from app.services import eval_manifest

    excluded = eval_manifest.excluded_projects(pricing_run.LIBRARY_PURPOSE)
    key = ",".join(sorted(excluded))
    hit = _LIBRARY_CACHE.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < LIBRARY_TTL_S:
        return hit[1]
    try:
        lib = CostLibrary.load(sb, exclude_projects=excluded)
    except LibraryUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The cost library is not available.") from exc
    _LIBRARY_CACHE[key] = (now, lib)
    return lib


@router.get("/lookup")
def lookup(
    description: str = Query(..., min_length=1, max_length=300),
    uom: str = Query("EA", pattern="^[A-Za-z ]{1,6}$"),
    quantity: float | None = Query(None, ge=0),
    user: CurrentUser = Depends(require_pricing_view),
) -> dict:
    sb = get_supabase()
    policy, _meta = pricing_run.load_policy(sb)
    service = pricing_tools.LibraryPriceService(_library(sb), policy=policy)
    return service.lookup({"description": description, "uom": uom.upper(), "quantity": quantity, "spec_facts": []})


# ── Settings ──────────────────────────────────────────────────────────────


class PolicyBody(BaseModel):
    policy: dict[str, Any] = Field(default_factory=dict)


def _require_policy_role(user: CurrentUser) -> None:
    if user.role not in AI_PRICING_POLICY_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the Verify roles may change the pricing policy")


@router.get("/settings")
def get_settings_route(user: CurrentUser = Depends(require_pricing_view)) -> dict:
    sb = get_supabase()
    policy, meta = pricing_run.load_policy(sb)
    return {"policy": policy.to_dict(), "version": meta.get("version"), "source": meta.get("source")}


@router.put("/settings")
def put_settings_route(body: PolicyBody, user: CurrentUser = Depends(require_pricing_write)) -> dict:
    _require_policy_role(user)
    sb = get_supabase()
    before, _meta = pricing_run.load_policy(sb)
    try:
        row = pricing_run.save_policy(sb, body.policy, user=user)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid pricing policy: {exc}") from exc
    after = pricing_run.PricingPolicy.from_dict(row.get("policy"))
    changed = sorted(k for k in after.to_dict() if after.to_dict()[k] != before.to_dict().get(k))
    audit(user.id, "ai_pricing.settings_updated", "ai_pricing_settings", "1", {
        "version": row.get("version"), "changed": changed,
    })
    return {"policy": after.to_dict(), "version": row.get("version"), "source": "table"}
