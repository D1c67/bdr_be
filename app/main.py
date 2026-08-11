"""BDR API — FastAPI application entrypoint."""

import asyncio
import contextlib
import logging

from fastapi import Depends, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings

settings = get_settings()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # RFQ reply polling — watches the bids@ inbox while RFQ sends are active.
    # Run one worker, or set RFQ_POLLING_ENABLED=false on the extras (a DB lease
    # also guards against double-running). Bidding-only: with the sub-app off
    # there are no RFQs to answer, so the poller would burn Graph calls forever.
    poll_task: asyncio.Task | None = None
    if settings.bidding_enabled and settings.rfq_polling_enabled and settings.ms_client_id:
        from app.services import rfq_inbox

        poll_task = asyncio.create_task(rfq_inbox.polling_loop())
    # Due-date reminder polling — no Graph dependency; extra workers are safe
    # (the ledger's unique index dedups), but DUE_REMINDERS_ENABLED=false can
    # still silence them. Leave it false on a fresh cloud deploy until the
    # migration is verified, to avoid a first-tick notification burst. Also
    # bidding-only: it reminds on BID deadlines and explicitly skips pm_only /
    # cp_only work, so with Bidding off it has nothing to fire on and would only
    # bell people about a module they can no longer open.
    reminder_task: asyncio.Task | None = None
    if settings.bidding_enabled and settings.due_reminders_enabled and settings.supabase_url:
        from app.services import due_reminders

        reminder_task = asyncio.create_task(due_reminders.polling_loop())
    # PM mailbox email ingestion — polls the configured mailbox (Inbox + Sent
    # Items) and runs the project-identification pipeline. Multi-worker safe
    # via the graph_sync_state lease. Deliberately NOT tied to PM_ENABLED: it
    # files mail against ANY project, bidding included (/emails is shared), and
    # EMAIL_INGEST_ENABLED is already its own switch.
    email_task: asyncio.Task | None = None
    if (
        settings.email_ingest_enabled
        and settings.email_ingest_mailbox
        and settings.ms_client_id
    ):
        from app.services import email_ingest

        email_task = asyncio.create_task(email_ingest.polling_loop())
    # AI model health — keeps the sidebar's Model status indicator warm by
    # probing the active LLM pool (see services/llm_health). No sub-app gate:
    # the features it covers span Bidding and PM. Each worker polls its own
    # snapshot; the probe is a free /models call, so that costs nothing worth
    # coordinating. LLM_HEALTH_ENABLED=false stops the polling — reads then
    # probe on demand instead.
    llm_health_task: asyncio.Task | None = None
    if settings.llm_health_enabled:
        from app.services import llm_health

        llm_health_task = asyncio.create_task(llm_health.polling_loop())
    # Durable LLM job queue (BOQ / general material / proposal lines). Runs in
    # EVERY worker on purpose: claims are atomic (claim_llm_jobs RPC, FOR
    # UPDATE SKIP LOCKED), so more workers just mean more job throughput.
    # LLM_QUEUE_ENABLED=false reverts dispatch to in-process BackgroundTasks.
    llm_queue_task: asyncio.Task | None = None
    if settings.llm_queue_enabled and settings.supabase_url:
        from app.services import llm_queue

        llm_queue_task = asyncio.create_task(llm_queue.worker_loop())
    elif settings.supabase_url:
        # Queue switched off: with no worker loop, leftover queued/running
        # jobs would strand their domain rows (pending forever, new starts
        # blocked). Release them once so users can re-run inline.
        from app.services import llm_queue

        try:
            await asyncio.to_thread(llm_queue.release_stranded_for_disabled_mode)
        except Exception:  # noqa: BLE001 - cleanup must never block boot
            logging.getLogger(__name__).exception("llm queue disabled-mode cleanup failed")
    yield
    for task in (poll_task, reminder_task, email_task, llm_health_task, llm_queue_task):
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


_is_prod = settings.environment == "production"

app = FastAPI(
    title="BDR API",
    description="Bidding-process automation for G3 Electrical",
    version="0.1.0",
    lifespan=lifespan,
    # Interactive docs + OpenAPI schema are dev conveniences; disable them in
    # production so the full API surface isn't published to anonymous callers.
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# Middleware is applied bottom-up: the LAST added wraps the others, so CORS is
# added last to stay outermost — every response, including a 413 from the body
# limit or an error, then carries CORS headers (browsers otherwise report a bare
# "Failed to fetch").
from app.core.middleware import (  # noqa: E402
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
)

app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)

_security_headers = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
if _is_prod:
    # Render terminates TLS in front of us; only assert HSTS where traffic is https.
    _security_headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
app.add_middleware(SecurityHeadersMiddleware, headers=_security_headers)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Let the browser read the export download's filename + file count, and the
    # rate-limit signals (so a throttled client can show "retry in Ns" and the
    # scope code). allow_headers only governs *request* headers; response headers
    # must be explicitly exposed.
    expose_headers=[
        "Content-Disposition",
        "X-Export-File-Count",
        "Retry-After",
        "X-RateLimit-Scope",
    ],
)


# ── Storage failures → clean, CORS-safe responses ─────────────────────────────
# A Supabase Storage error — most commonly a 413 when an object exceeds the
# bucket/global size limit — is raised deep in the upload path as a
# StorageApiError. With no handler it escapes as an unhandled 500 synthesized by
# Starlette's OUTERMOST ServerErrorMiddleware, which sits above the CORS
# middleware, so that 500 never gets an Access-Control-Allow-Origin header and
# the browser reports a misleading "Failed to fetch"/CORS error instead of the
# real cause. A *registered* handler, by contrast, runs in the innermost
# ExceptionMiddleware (below CORS): its response flows back out through CORS and
# is both actionable and CORS-safe.
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from storage3.utils import StorageException  # noqa: E402


@app.exception_handler(StorageException)
async def _storage_exception_handler(_: Request, exc: StorageException) -> JSONResponse:
    raw_status = getattr(exc, "status", None)  # set on StorageApiError; else None
    try:
        code = int(raw_status)
    except (TypeError, ValueError):
        code = None

    if code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
        # The per-route streaming caps (files: upload_max_bytes; BOQ:
        # boq_max_bytes) normally reject first; reaching here means storage is
        # configured below the app cap, so keep the message limit-agnostic.
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"detail": "This file is too large to store and was not uploaded."},
        )

    # Storage rejects object keys containing characters outside its allowlist
    # with a 400 InvalidKey (seen in prod: "~" in the Windows 8.3 short name
    # "E002-E~1.PDF"). build_object_path passes the original filename through,
    # so this is a filename the user can fix, not an outage; saying
    # "temporarily unavailable, try again" sends them into a retry loop that
    # can never succeed.
    err_code = str(getattr(exc, "code", "") or "")
    err_message = str(getattr(exc, "message", "") or "")
    if (
        err_code.replace("_", "").lower() == "invalidkey"
        or "invalid key" in err_message.lower()
    ):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": (
                    "This file's name contains characters that cannot be stored. "
                    "Rename the file using letters, numbers, spaces, hyphens, or "
                    "periods, then try again."
                )
            },
        )

    # Any other storage failure is an upstream dependency problem, not the
    # client's fault → 502 (still CORS-safe) rather than a bare 500.
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": "File storage is temporarily unavailable. Please try again."},
    )


# Same CORS rationale as StorageException above, for the transport layer: a
# dropped Supabase connection surfaces as httpx.TransportError (seen in prod as
# RemoteProtocolError "ConnectionTerminated" when an HTTP/2 GOAWAY killed the
# shared connection mid-upload, taking unrelated in-flight requests with it).
# The supabase client now runs HTTP/1.1 (see core/supabase_client.py) which
# removes the shared-connection blast radius, but any residual connection drop
# is a transient upstream fault: answer 502 + retry guidance, never a bare 500.
import httpx  # noqa: E402


@app.exception_handler(httpx.TransportError)
async def _upstream_transport_error_handler(
    _: Request, exc: httpx.TransportError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "detail": (
                "A backend connection dropped while handling this request. "
                "Please try again."
            )
        },
    )


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


from app.core.deps import CurrentUser, get_current_user  # noqa: E402
from app.core.features import SubApp, enabled_map, require_feature  # noqa: E402


@app.get("/features", tags=["meta"])
def features(_: CurrentUser = Depends(get_current_user)) -> dict[str, bool]:
    """Which sub-apps this deployment serves — the frontend's source of truth.

    Read once at sign-in (see bdr_fe/lib/auth.tsx) to decide which nav entries,
    switcher tiles and cross-app links to render, so the UI and the API can
    never disagree about what exists. Authenticated, but allowed at aal1 (see
    AAL1_ALLOWED in app/core/deps.py) because the shell loads it alongside the
    profile, before the 2FA gate has been passed.
    """
    return enabled_map()


# Routers are mounted as each domain is implemented.
from app.routers import (  # noqa: E402
    analytics,
    boq_analysis,
    change_review,
    emails,
    estimator,
    files,
    general_material,
    gono,
    llm_monitor,
    llm_status,
    notes,
    notification_log,
    notifications,
    outcome,
    payroll_cpr,
    payroll_employee_documents,
    payroll_employees,
    payroll_projects,
    payroll_rates,
    payroll_reports,
    payroll_settings,
    pm,
    pm_documents,
    pm_field,
    pm_financials,
    pm_materials,
    pm_submittals,
    pricing,
    projects,
    proposals,
    reference,
    rfqs,
    submittals,
    todos,
    training,
    users,
    vendors,
    workflow,
)

# ── Who owns what ─────────────────────────────────────────────────────────────
# The one place recording which sub-app each router belongs to. A router listed
# under a sub-app carries that sub-app's feature guard, so when the module is
# switched off every one of its routes 404s before auth even runs (router-level
# dependencies are solved first) — see app/core/features.py.
#
# Modules are imported unconditionally whatever the flags say: several routers
# import helpers from each other at module scope (payroll_reports and
# payroll_employee_documents pull _read_capped from routers/files; payroll_projects
# pulls redact_for_role from routers/projects; pm_documents shares the bidding
# export lock), so an import-level kill switch would take down the sub-apps that
# are still on. Gate the routes, never the imports.
_BIDDING = [Depends(require_feature(SubApp.BIDDING))]
_PM = [Depends(require_feature(SubApp.PM))]
_CP = [Depends(require_feature(SubApp.CERTIFIED_PAYROLL))]

# Shared spine — served no matter which sub-apps are enabled, because switching
# one module off must not break the others. `projects` is the row PM and CP both
# create and read (a handful of its routes ARE bidding-only and carry the flag
# individually — see the note in routers/projects.py); `reference`/`vendors` are
# the master data PM validates against; `notification_log` is opened from both
# the bidding side menu and the PM rail; `submittals` is the company-global bank,
# offered from both the Bidding and the PM nav; `todos` is a personal task list
# every internal user keeps, with no project or stage on it at all — it merely
# happens to be reached from the Bidding nav today.
app.include_router(users.router)
app.include_router(notifications.router)
app.include_router(reference.router)
app.include_router(vendors.router)
app.include_router(projects.router)
app.include_router(notification_log.router)
app.include_router(submittals.router)
app.include_router(todos.router)
# AI model status — the sidebar indicator. Shared: the AI features it reports on
# belong to Bidding and PM alike, so it must survive either being switched off.
app.include_router(llm_status.router)
# Dev AI monitor (queue, failures, retries). Shared for the same reason; every
# route requires a dev account (require_dev) on top of auth.
app.include_router(llm_monitor.router)

# Bidding — the bid pipeline, its files/notes, and the external estimator portal.
app.include_router(workflow.router, dependencies=_BIDDING)
app.include_router(gono.router, dependencies=_BIDDING)
app.include_router(estimator.router, dependencies=_BIDDING)
app.include_router(rfqs.router, dependencies=_BIDDING)
app.include_router(boq_analysis.router, dependencies=_BIDDING)
app.include_router(general_material.router, dependencies=_BIDDING)
app.include_router(pricing.router, dependencies=_BIDDING)
app.include_router(proposals.router, dependencies=_BIDDING)
app.include_router(outcome.router, dependencies=_BIDDING)
# NOTE: /analytics goes with Bidding as a whole, including its /activity feed.
# That feed is the one audit surface spanning all three modules (it reads the
# shared audit_log, pm.* and cp.* rows included), so a future PM-only deployment
# that wants it must split that one route out of routers/analytics.py — nothing
# else in the router is reusable, every other metric is derived from bid stages.
app.include_router(analytics.router, dependencies=_BIDDING)
app.include_router(notes.router, dependencies=_BIDDING)
app.include_router(files.router, dependencies=_BIDDING)
app.include_router(change_review.router, dependencies=_BIDDING)
app.include_router(training.router, dependencies=_BIDDING)

# Project Management — won work. (require_pm_read/require_pm_write carry the
# same guard, so a future PM router that forgets this table still fails closed.)
# `emails` sits here rather than on the spine: the ingestion poller files mail
# against any project, but the triage UI it feeds exists only under /pm — the
# bidding project page has no email surface.
app.include_router(emails.router, dependencies=_PM)
app.include_router(pm.router, dependencies=_PM)
app.include_router(pm_financials.router, dependencies=_PM)
app.include_router(pm_field.router, dependencies=_PM)
app.include_router(pm_documents.router, dependencies=_PM)
app.include_router(pm_materials.router, dependencies=_PM)
app.include_router(pm_submittals.router, dependencies=_PM)

# Certified Payroll — everything under /payroll. (Likewise mirrored in
# require_cp_read/require_cp_write.)
app.include_router(payroll_projects.router, dependencies=_CP)
app.include_router(payroll_reports.router, dependencies=_CP)
app.include_router(payroll_cpr.router, dependencies=_CP)
app.include_router(payroll_employees.router, dependencies=_CP)
app.include_router(payroll_employee_documents.router, dependencies=_CP)
app.include_router(payroll_rates.router, dependencies=_CP)
app.include_router(payroll_settings.router, dependencies=_CP)
