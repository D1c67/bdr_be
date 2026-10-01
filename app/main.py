"""BDR API — FastAPI application entrypoint."""

import asyncio
import contextlib
import logging

from fastapi import Depends, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings

settings = get_settings()

# How long boot waits for the RFP Ingestion sandbox self-test (a child spawn
# on the embedded one-page PDF). A healthy self-test takes a few seconds; the
# bound only matters when something is badly wrong (a hung spawn, a uid slot
# held by a stuck worker), and then the worker must still come up: the thread
# finishes on its own and caches its verdict for GET /status and execute().
_RFP_SELF_TEST_BOOT_SECONDS = 120


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
    # Daily "bids due today" digest email: weekday mornings (Pacific), to all
    # internal users except accountants. Multi-worker safe: the due_digest_log
    # (digest_date, user_id) unique index means only one worker's claim wins,
    # so extra workers are harmless. Needs Graph creds to send, and is
    # bidding-only for the same reason as the reminders above.
    digest_task: asyncio.Task | None = None
    if (
        settings.bidding_enabled
        and settings.due_digest_enabled
        and settings.supabase_url
        and settings.ms_client_id
    ):
        from app.services import due_digest

        digest_task = asyncio.create_task(due_digest.polling_loop())
    # Calling In (docs/CALLING_IN.md section 5): claims a call_in_entries row
    # when a project lands on a call list and notifies Executive + Estimating
    # Engineer Labor once, then closes the row when the project leaves.
    # Multi-worker safe: the partial unique index on call_in_entries is the
    # claim, so only the worker whose insert wins notifies. Bidding-only.
    call_in_task: asyncio.Task | None = None
    if settings.bidding_enabled and settings.call_in_enabled and settings.supabase_url:
        from app.services import calling_in as calling_in_service

        call_in_task = asyncio.create_task(calling_in_service.polling_loop())
    # PM mailbox email ingestion — polls the configured mailbox (Inbox + Sent
    # Items) and runs the project-identification pipeline. Multi-worker safe
    # via the graph_sync_state lease. Deliberately NOT tied to PM_ENABLED: it
    # files mail against ANY project, bidding included (/emails is shared), and
    # EMAIL_INGEST_ENABLED is already its own switch.
    # The RFP test bench (docs/RFP_TESTING.md 4.2) drives the same loop at
    # the test mailbox while a session is active, so the loop also starts
    # when the bench is on and the filer's own mailbox is unset or off.
    email_task: asyncio.Task | None = None
    rfp_testing = settings.rfp_testing_enabled and settings.rfp_ingest_enabled
    if settings.ms_client_id and (
        (settings.email_ingest_enabled and settings.email_ingest_mailbox) or rfp_testing
    ):
        from app.services import email_ingest

        email_task = asyncio.create_task(email_ingest.polling_loop())
    # RFP Ingestion email intake: watches the RFP_EMAIL_INGESTION_INBOXES_ALLOWED
    # mailboxes under the shared RFP_INGESTION_ENABLED switch. Multi-worker
    # safe via its own graph_sync_state lease (docs/RFP_EMAIL_INGESTION.md).
    # The test bench (docs/RFP_TESTING.md 4.1) drives the same loop at the
    # test mailbox, so it also starts with no real mailbox listed.
    rfp_email_task: asyncio.Task | None = None
    if (settings.rfp_email_ingestion_enabled or rfp_testing) and settings.ms_client_id:
        from app.core.supabase_client import get_supabase
        from app.services import rfp_email_ingest

        # Once, before the first sweep tick: rows the intake slice parked at
        # `done` and never extracted move to `extract` (docs/RFP_MATCHING.md
        # section 4). Idempotent, and a failure must never block boot: the
        # sweep still runs, and the next boot repeats the pass.
        try:
            moved = await asyncio.to_thread(rfp_email_ingest.backfill_parked_rows, get_supabase())
            if moved:
                logging.getLogger(__name__).info(
                    "rfp email ingest: %d parked row(s) moved to extract at boot", moved
                )
        except Exception:  # noqa: BLE001 - the backfill must never block boot
            logging.getLogger(__name__).exception("rfp email ingest startup backfill failed")
        rfp_email_task = asyncio.create_task(rfp_email_ingest.polling_loop())
    # Portal invitations: NGEM (docs/RFP_NGEM_PORTAL.md) and BuildingConnected
    # (docs/RFP_BUILDINGCONNECTED.md) share one scheduler loop that claims
    # each portal's scan slots and runs the invitation sweep, every worker
    # (the slot ledger and the sweep lease keep the workers disjoint). The
    # loop starts when EITHER portal is active (master switch, its own
    # switch, a configured account or APS app, and the queue that runs its
    # jobs); poll_once gates each portal on its own. The boot lines say
    # whether each is configured; passwords and secrets are never logged.
    rfp_portal_task: asyncio.Task | None = None
    if settings.rfp_ingest_enabled and settings.rfp_ngem_enabled:
        logging.getLogger(__name__).info(
            "rfp portal (NGEM): enabled, account %s, entry url %s, schedule %s",
            "configured" if settings.ngem_configured else "NOT configured",
            "set" if settings.ngem_entry_url.strip() else "unset",
            settings.rfp_ngem_schedule_times,
        )
    if settings.rfp_ingest_enabled and settings.rfp_bc_enabled:
        logging.getLogger(__name__).info(
            "rfp portal (BuildingConnected): enabled, APS app %s, redirect url %s, "
            "poll every %s min, full sync %s PT%s",
            "configured" if settings.bc_configured else "NOT configured",
            "set" if settings.rfp_bc_redirect_url.strip() else "unset",
            settings.rfp_bc_poll_minutes,
            settings.rfp_bc_full_sync_time,
            ", TEST MODE" if settings.rfp_bc_test_mode else "",
        )
    if settings.rfp_portal_any_active and settings.supabase_url:
        from app.services import rfp_portal_ingest

        rfp_portal_task = asyncio.create_task(rfp_portal_ingest.polling_loop())
    elif settings.rfp_portal_any_enabled:
        logging.getLogger(__name__).warning(
            "rfp portal: the scheduler is not running (no portal account or APS app "
            "configured, or the LLM queue is off); the /rfp-portal routes still answer"
        )
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
    # RFP Ingestion sandbox self-test (docs/RFP_INGESTION_SANDBOX.md 3.6): with
    # the flag on, spawn the sandbox child once on the embedded page so a
    # broken image (pypdfium2 missing, an unusable uid pool, a scratch dir
    # that cannot be written) shows up in the boot log instead of on the
    # first real run. The service caches the verdict and refuses to start
    # runs while it reads failed; GET /status?self_test=1 re-runs it (a plain
    # /status read serves the cached verdict instead of spawning a child).
    # Bounded and best-effort: a failure is logged LOUDLY, never raised, and
    # boot never waits longer than _RFP_SELF_TEST_BOOT_SECONDS for it.
    if settings.rfp_ingest_enabled:
        from app.services import rfp_ingest as rfp_ingest_service

        boot_logger = logging.getLogger(__name__)
        try:
            verdict = await asyncio.wait_for(
                asyncio.to_thread(rfp_ingest_service.self_test),
                timeout=_RFP_SELF_TEST_BOOT_SECONDS,
            )
        except TimeoutError:
            boot_logger.error(
                "RFP INGEST SELF-TEST did not finish within %d s; the sandbox may be "
                "unusable on this worker (check GET /rfp-ingest/status?self_test=1)",
                _RFP_SELF_TEST_BOOT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - the self-test must never block boot
            boot_logger.exception("RFP INGEST SELF-TEST could not run")
        else:
            if verdict.get("ok"):
                boot_logger.info(
                    "rfp ingest self-test ok in %s ms (uid switch %s)",
                    verdict.get("elapsed_ms"),
                    "applied" if verdict.get("uid_switch_applied") else "not applied",
                )
            else:
                boot_logger.error(
                    "RFP INGEST SELF-TEST FAILED: %s (runs will be refused until "
                    "GET /rfp-ingest/status?self_test=1 passes)",
                    verdict.get("detail"),
                )
    yield
    # Tell a sandbox run in flight to stop cleanly BEFORE its queue task is
    # cancelled: the runner's monitor loop sees the event from its worker
    # thread (a CancelledError never reaches a thread), kills the child,
    # leaves the file running and requeues its job, so a redeploy mid-file
    # resumes on the next worker instead of waiting out a lease expiry.
    # Always set, flag or not: it is inert when nothing is running.
    from app.services import rfp_ingest as rfp_ingest_service

    rfp_ingest_service.SHUTTING_DOWN.set()
    for task in (
        poll_task, reminder_task, digest_task, email_task, rfp_email_task,
        rfp_portal_task, llm_health_task, llm_queue_task, call_in_task,
    ):
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    # Free the RFP intake and NGEM sweep leases this process holds, so the
    # next worker (a redeploy, or the dev server's --reload) does not wait
    # out a dead process's lease before it can sync a mailbox.
    if rfp_email_task or rfp_portal_task:
        from app.core.supabase_client import get_supabase as _sb
        from app.services import rfp_email_ingest as _intake

        try:
            await asyncio.to_thread(_intake.release_leases, _sb())
        except Exception:  # noqa: BLE001 - shutdown must never fail on this
            logging.getLogger(__name__).exception("RFP intake lease release failed on shutdown")


# Fail closed: anything but an explicit dev ENVIRONMENT gets the prod posture.
_is_prod = settings.is_production

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
    form_routes_matcher,
)

# The 460 MB allowance applies only to the routes that declare Form/File body
# params (read lazily from app.routes on the first request, after every
# include_router below). Every other route, whatever Content-Type the client
# sends, gets the 16 MB cap: FastAPI buffers those bodies in RAM before auth.
app.add_middleware(
    MaxBodySizeMiddleware,
    max_bytes=settings.max_request_body_bytes,
    max_json_bytes=settings.max_json_body_bytes,
    upload_routes=form_routes_matcher(app),
)

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
    # Let the browser read the export download's filename + file count, the
    # rate-limit signals (so a throttled client can show "retry in Ns" and the
    # scope code) and the machine-readable error code a refusal carries (the
    # frontend branches on it, e.g. rfp_match_unmerge_required opens the
    # Merged by System modal). allow_headers only governs *request* headers;
    # response headers must be explicitly exposed.
    expose_headers=[
        "Content-Disposition",
        "X-Export-File-Count",
        "Retry-After",
        "X-RateLimit-Scope",
        "X-Error-Code",
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

_upstream_logger = logging.getLogger("app.upstream")


def _log_upstream_error(exc: BaseException, message: str, *args: object) -> None:
    """Log an upstream failure with its stack but never its raw text: httpx
    quotes the full request URL (query string included) in str(exc), and
    Graph upload-session URLs carry a pre-authenticated token there. The
    exception text goes through redact_text and the traceback is formatted
    frames only, so no logger.exception / exc_info that would re-print it."""
    import traceback

    from app.core.redact import redact_text

    frames = "".join(traceback.format_tb(exc.__traceback__))
    _upstream_logger.error(
        message + ": %s: %s\nTraceback (most recent call last):\n%s",
        *args,
        type(exc).__name__,
        redact_text(exc),
        frames,
    )


@app.exception_handler(httpx.TransportError)
async def _upstream_transport_error_handler(
    request: Request, exc: httpx.TransportError
) -> JSONResponse:
    # A registered handler bypasses ServerErrorMiddleware's traceback logging,
    # so log here or the drop is invisible (which upstream, which call).
    try:
        host = exc.request.url.host
    except RuntimeError:  # httpx raises when no request was attached
        host = "<unknown host>"
    _log_upstream_error(
        exc,
        "Upstream transport error (%s) from %s while handling %s %s",
        type(exc).__name__,
        host,
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "detail": (
                "A backend connection dropped while handling this request. "
                "Please try again."
            )
        },
    )


# An upstream service can also ANSWER with an HTTP error that raise_for_status()
# turns into httpx.HTTPStatusError — a sibling of TransportError under
# HTTPError, not a subclass, so the handler above never sees it. Seen in prod on
# the RFQ bulk-send OneDrive path (a drawing set over the inline limit is
# uploaded to OneDrive before any email goes out): a Graph 429/5xx there escaped
# as an unhandled 500 that lost its CORS headers and reached the browser as a
# bare "Failed to fetch". Same treatment: 502, CORS-safe, retry guidance. The
# request URL is deliberately not echoed to the client (Graph upload URLs embed
# pre-authenticated tokens) and the detail is logged here instead (URLs
# redacted to scheme, host and path by _log_upstream_error), because a
# registered handler bypasses ServerErrorMiddleware's traceback logging.


@app.exception_handler(httpx.HTTPStatusError)
async def _upstream_http_status_error_handler(
    request: Request, exc: httpx.HTTPStatusError
) -> JSONResponse:
    _log_upstream_error(
        exc,
        "Upstream HTTP %s from %s while handling %s %s",
        exc.response.status_code,
        exc.request.url.host,
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "detail": (
                "An upstream service rejected a request "
                f"(HTTP {exc.response.status_code}) while handling this one. "
                "Please try again."
            )
        },
    )


# A `.single()` read that found no row (PostgREST PGRST116) is a missing
# record, not a server fault: a deleted project's stale tab or link
# (docs/PROJECT_DELETE.md) reaches many project-scoped GETs that read the
# project with `.single()`. Answer 404, CORS-safe. Any other PostgREST error
# is re-raised untouched and keeps today's behavior.
from postgrest.exceptions import APIError as _PostgrestAPIError  # noqa: E402


@app.exception_handler(_PostgrestAPIError)
async def _postgrest_not_found_handler(request: Request, exc: _PostgrestAPIError) -> JSONResponse:
    if getattr(exc, "code", None) == "PGRST116":
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not found"})
    # A write into a project deleted from another tab (e.g. a file upload)
    # trips the projects foreign key: same missing record, same 404.
    if getattr(exc, "code", None) == "23503" and 'table "projects"' in str(
        getattr(exc, "details", "") or ""
    ):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Project not found"}
        )
    raise exc


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    # Unauthenticated: never reveal which environment (and so which guards) is live.
    return {"status": "ok"}


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
    bid_drafts,
    bid_splitter,
    boq_analysis,
    calling_in,
    change_review,
    deleted_projects,
    emails,
    estimator,
    external_submission,
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
    rfp_bc,
    rfp_created,
    rfp_ingest,
    rfp_emails,
    rfp_portal,
    rfp_processing,
    rfp_testing,
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
# Bid File Splitter — experimental standalone AI tool, gated by its OWN env flag
# (BID_FILE_SPLITTER_ENABLED, default off → every route 404s), not a sub-app:
# it is not connected to the bidding pipeline yet, so it must not ride the
# BIDDING flag. The dependency lives on the router itself (core/features.py).
app.include_router(bid_splitter.router)
# RFP Ingestion sandbox: the out-of-process PDF sanitizer's dev-only bench,
# gated by its OWN env flag (RFP_INGEST_ENABLED, default off: every route
# 404s) on the router itself, plus require_dev on every endpoint. Not a
# sub-app and not connected to the bidding pipeline, so it must not ride the
# BIDDING flag either (splitter precedent).
app.include_router(rfp_ingest.router)
# RFP Ingestion email intake: review queue + authorized-sender rules. Same
# master switch as the sandbox (gated on the router itself), internal roles
# only; not tied to BIDDING for the same reason as the sandbox.
app.include_router(rfp_emails.router)
# RFP Processing: the operations view over every in-flight intake row, in
# lanes, plus Retry on a stuck email row (docs/RFP_PROCESSING.md). Same gate
# as /rfp-emails (the email intake served, on the router itself) and the same
# roles; not tied to BIDDING for the same reason as the router above.
app.include_router(rfp_processing.router)
# NGEM portal invitations: the NGEM tab of /rfp-emails and its settings
# block. Gated on the router itself (RFP_INGESTION_ENABLED and
# RFP_NGEM_ENABLED), review-queue roles only; not tied to BIDDING for the
# same reason as the two routers above.
app.include_router(rfp_portal.router)
app.include_router(rfp_bc.router)  # BuildingConnected: connect/status/run/aliases (RFP_BC_ENABLED)
app.include_router(rfp_bc.callback_router)  # the OAuth callback: gate + state only, no auth
# "Created from RFPs": every bidding project the RFP creation step made and
# its flags (docs/RFP_CREATE.md 8). Gated on the router itself (the email
# intake OR the NGEM slice served), Estimating Admin / Executive / IT Admin
# only; not tied to BIDDING for the same reason as the three routers above.
app.include_router(rfp_created.router)
# RFP test bench: the dev-only test mode and monitor (docs/RFP_TESTING.md).
# Gated on the router itself (RFP_TESTING_ENABLED under RFP_INGESTION_ENABLED,
# every route 404s while off) plus a dev it_admin caller on every endpoint;
# not tied to BIDDING for the same reason as the routers above.
app.include_router(rfp_testing.router)

# Bidding — the bid pipeline, its files/notes, and the external estimator portal.
app.include_router(workflow.router, dependencies=_BIDDING)
app.include_router(gono.router, dependencies=_BIDDING)
# Saved New Bid drafts - the intake form parked before any project row exists.
app.include_router(bid_drafts.router, dependencies=_BIDDING)
app.include_router(estimator.router, dependencies=_BIDDING)
app.include_router(rfqs.router, dependencies=_BIDDING)
app.include_router(boq_analysis.router, dependencies=_BIDDING)
app.include_router(general_material.router, dependencies=_BIDDING)
app.include_router(pricing.router, dependencies=_BIDDING)
app.include_router(proposals.router, dependencies=_BIDDING)
app.include_router(outcome.router, dependencies=_BIDDING)
# Mark submitted from the project side menu (0140): a proposal sent outside the app.
app.include_router(external_submission.router, dependencies=_BIDDING)
# Calling In (docs/CALLING_IN.md): the two call lists, plus the read-only
# project-page call log mounted under /projects (bidding-only like the lists).
app.include_router(calling_in.router, dependencies=_BIDDING)
app.include_router(calling_in.project_router, dependencies=_BIDDING)
# Deleted Projects (0141, docs/PROJECT_DELETE.md): the IT Admin's archive of
# deleted projects and the restore. The delete itself lives on /projects.
app.include_router(deleted_projects.router, dependencies=_BIDDING)
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
