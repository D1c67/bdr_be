"""BDR API — FastAPI application entrypoint."""

import asyncio
import contextlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings

settings = get_settings()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # RFQ reply polling — watches the bids@ inbox while RFQ sends are active.
    # Run one worker, or set RFQ_POLLING_ENABLED=false on the extras (a DB lease
    # also guards against double-running).
    poll_task: asyncio.Task | None = None
    if settings.rfq_polling_enabled and settings.ms_client_id:
        from app.services import rfq_inbox

        poll_task = asyncio.create_task(rfq_inbox.polling_loop())
    # Due-date reminder polling — no Graph dependency; extra workers are safe
    # (the ledger's unique index dedups), but DUE_REMINDERS_ENABLED=false can
    # still silence them. Leave it false on a fresh cloud deploy until the
    # migration is verified, to avoid a first-tick notification burst.
    reminder_task: asyncio.Task | None = None
    if settings.due_reminders_enabled and settings.supabase_url:
        from app.services import due_reminders

        reminder_task = asyncio.create_task(due_reminders.polling_loop())
    yield
    for task in (poll_task, reminder_task):
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="BDR API",
    description="Bidding-process automation for G3 Electrical",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


# Routers are mounted as each domain is implemented.
from app.routers import (  # noqa: E402
    analytics,
    boq_analysis,
    estimator,
    files,
    general_material,
    gono,
    notes,
    notifications,
    outcome,
    pricing,
    projects,
    proposals,
    reference,
    rfqs,
    todos,
    users,
    vendors,
    workflow,
)

app.include_router(users.router)
app.include_router(reference.router)
app.include_router(projects.router)
app.include_router(workflow.router)
app.include_router(gono.router)
app.include_router(estimator.router)
app.include_router(vendors.router)
app.include_router(rfqs.router)
app.include_router(boq_analysis.router)
app.include_router(general_material.router)
app.include_router(pricing.router)
app.include_router(proposals.router)
app.include_router(outcome.router)
app.include_router(analytics.router)
app.include_router(notifications.router)
app.include_router(notes.router)
app.include_router(todos.router)
app.include_router(files.router)
