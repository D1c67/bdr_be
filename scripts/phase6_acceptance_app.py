"""Explicit development acceptance lifespan; unrelated queues/pollers never run.

Run only through the isolated launcher (scripts/start_phase6_acceptance.sh),
which since browser v2 (WI-20) starts it with two uvicorn workers and the v2
flags. This module reuses the normal HTTP authorization, job execution, lease
renewal, and browser worker routes. Its maintenance runs only the personal
browser sweeps and the v2 chat outbox (results, cards and the owner's Teams
alerts), never general maintenance, retention or email delivery.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from fastapi.responses import JSONResponse, Response

from app.main import app
from app.core.config import get_settings
from app.core.redact import install_httpx_filter
from app.assistant import repo
from app.services import llm_queue, llm_target
from scripts.start_phase6_credential_setup import ACCEPTANCE_ON_FLAGS, DEV_URL, OFF_FLAGS

logger = logging.getLogger(__name__)
CLAIM_TYPES = frozenset({'assistant_turn', 'assistant_approval_execute', 'assistant_continue'})
SHUTDOWN_SECONDS = 30
RECONNECT_SCRIPT = Path(__file__).resolve().parent / 'phase6_reconnect_personal_worker.ps1'
# Browser v2 release step 5: the user pool firewall script reaches shop-pc-1
# through this tunnel, byte for byte, and the printed block checks its hash.
FIREWALL_SCRIPT = Path(__file__).resolve().parent.parent / 'agent_worker' / 'install' / 'apply_user_pool_firewall.ps1'


class AcceptanceError(ValueError):
    pass


def guard(settings) -> None:
    if os.environ.get('PHASE6_ACCEPTANCE') != 'true':
        raise AcceptanceError('Explicit PHASE6_ACCEPTANCE=true is required.')
    try:
        UUID(os.environ['PHASE6_ACCEPTANCE_OWNER'])
        # The personal worker is optional: before the 1.1.0 re-enrollment is
        # approved there is none. When given it must be a UUID.
        if os.environ.get('PHASE6_ACCEPTANCE_WORKER'):
            UUID(os.environ['PHASE6_ACCEPTANCE_WORKER'])
        if not re.fullmatch(r'[a-f0-9]{32}', os.environ.get('PHASE6_ACCEPTANCE_RUN', '')):
            raise ValueError
    except (KeyError, ValueError):
        raise AcceptanceError('Acceptance requires designated owner and run identifiers.') from None
    if settings.supabase_url.rstrip('/') != DEV_URL or settings.is_production:
        raise AcceptanceError('Acceptance requires the exact development project and development posture.')
    if not all(getattr(settings, name, False) is True for name in (
        'assistant_enabled', 'assistant_browser_enabled', 'browser_agents_enabled', 'llm_queue_enabled',
    )):
        raise AcceptanceError('Acceptance requires assistant, personal browser, browser platform and queue flags.')
    if not all(getattr(settings, name, False) is True for name in (
        'assistant_browser_v2_enabled', 'assistant_hosted_search_enabled',
    )):
        raise AcceptanceError('Acceptance requires the browser v2 and hosted search flags.')
    allowed = llm_queue.claim_types_allowed(settings)
    if set(allowed) != CLAIM_TYPES or len(allowed) != len(CLAIM_TYPES):
        raise AcceptanceError('Acceptance requires exactly the three assistant queue types.')
    if getattr(settings, 'assistant_concurrency', None) != 1:
        raise AcceptanceError('Acceptance requires assistant concurrency one.')
    fields = {flag.lower() for flag in OFF_FLAGS if flag != 'LLM_QUEUE_ENABLED' and flag not in ACCEPTANCE_ON_FLAGS}
    fields.update({'rfp_ingest_enabled', 'rfp_bc_enabled'})
    if any(getattr(settings, field, False) for field in fields):
        raise AcceptanceError('Unrelated pollers and job features must remain disabled.')


def validate_claims(rows) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 1:
        raise AcceptanceError('The acceptance claim violated its single-job capacity.')
    for job in rows:
        if not isinstance(job, dict) or job.get('job_type') not in CLAIM_TYPES:
            raise AcceptanceError('The acceptance claim returned an unrelated job type.')
        try:
            UUID(str(job['id']))
            expires = datetime.fromisoformat(str(job['lease_expires_at']).replace('Z', '+00:00'))
            valid = (job.get('status') == 'running' and job.get('claimed_by') == llm_queue._WORKER_TOKEN
                and isinstance(job.get('payload'), dict)
                and type(job.get('attempts')) is int and job['attempts'] >= 1
                and expires.tzinfo is not None and expires > datetime.now(timezone.utc))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise AcceptanceError('The acceptance claim returned invalid lease metadata.')
    return rows


async def pause(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def assistant_loop(stop: asyncio.Event, application=app) -> None:
    while not stop.is_set():
        try:
            settings = get_settings()
            guard(settings)
            # Deliberately bypass worker_loop/_claim_tick/_sweep. The generic
            # sweeper touches expired jobs and retention across all job types.
            rows = validate_claims(await asyncio.to_thread(llm_queue._claim_assistant, settings, []))
            for job in rows:
                # Once claimed, finish this one job even if shutdown arrived
                # during the RPC. Existing _execute owns its lease and retries.
                await asyncio.to_thread(llm_queue._execute, job)
        except AcceptanceError:
            application.state.phase6_acceptance_failed = True
            logger.error('Acceptance stopped claiming: its configuration or lease boundary changed.')
            stop.set()
            return
        except Exception as exc:
            logger.warning('Acceptance assistant claim/execute failed (%s); no unrelated job was run.', type(exc).__name__)
        await pause(stop, 1)


def drain_browser_chat() -> dict:
    from app.services import assistant_browser_chat
    return assistant_browser_chat.drain_outbox(limit=20)


async def browser_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            # This SQL function is fixed to principal_kind=user. It settles
            # private leases/expiry and creates owner inbox/bell notices. Do
            # not invoke general maintenance, retention, or email delivery.
            await asyncio.to_thread(repo.browser_rpc, None, 'assistant_browser_sweep', {'p_limit': 10})
        except Exception as exc:
            logger.warning('Acceptance private browser sweep failed (%s).', type(exc).__name__)
        try:
            # Browser v2 (plan 4.5, 6.6, 12.3): holds, parks, lapsed leases and
            # admissions, then the chat outbox (one message per event and the
            # owner's Teams alert for open cards; a finished task may queue its
            # one follow-up assistant_turn, which assistant_loop runs).
            await asyncio.to_thread(repo.browser_sweep_v2, None, limit=10)
        except Exception as exc:
            logger.warning('Acceptance browser v2 sweep failed (%s).', type(exc).__name__)
        try:
            await asyncio.to_thread(drain_browser_chat)
        except Exception as exc:
            logger.warning('Acceptance browser chat outbox drain failed (%s).', type(exc).__name__)
        await pause(stop, 20)


@contextlib.asynccontextmanager
async def lifespan(application):
    guard(get_settings())
    install_httpx_filter()
    await asyncio.to_thread(llm_target.refresh)
    stop = asyncio.Event()
    application.state.phase6_acceptance_failed = False
    application.state.phase6_acceptance_run = os.environ['PHASE6_ACCEPTANCE_RUN']
    tasks = [asyncio.create_task(assistant_loop(stop, application)), asyncio.create_task(browser_loop(stop))]
    application.state.phase6_acceptance_ready = True
    try:
        yield
    finally:
        application.state.phase6_acceptance_ready = False
        stop.set()
        # A canceled asyncio waiter cannot stop an executing Python thread.
        # Wait for real execution, then retain normal DB lease semantics if
        # the launcher must stop the process after this bounded grace period.
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_SECONDS)
        if pending:
            logger.warning('Acceptance shutdown grace elapsed; in-flight work retains its existing lease. No success or cancellation was fabricated.')


@app.get('/phase6-acceptance/ready', include_in_schema=False)
def ready():
    if (not getattr(app.state, 'phase6_acceptance_ready', False)
            or getattr(app.state, 'phase6_acceptance_failed', False)):
        return JSONResponse({'ready': False}, status_code=503, headers={'Cache-Control': 'no-store'})
    return JSONResponse({'run': app.state.phase6_acceptance_run, 'ready': True},
                        headers={'Cache-Control': 'no-store'})


def _fixed_script(path: Path, filename: str):
    readiness = ready()
    if readiness.status_code != 200:
        return readiness
    try:
        content = path.read_bytes()
    except OSError:
        return JSONResponse({'ready': False}, status_code=503, headers={'Cache-Control': 'no-store'})
    return Response(content=content, media_type='text/plain; charset=utf-8', headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Disposition': f'attachment; filename="{filename}"',
    })


@app.get('/phase6-acceptance/reconnect.ps1', include_in_schema=False)
def reconnect_script():
    return _fixed_script(RECONNECT_SCRIPT, 'phase6-reconnect-personal-worker.ps1')


@app.get('/phase6-acceptance/firewall.ps1', include_in_schema=False)
def firewall_script():
    return _fixed_script(FIREWALL_SCRIPT, 'apply_user_pool_firewall.ps1')


app.router.lifespan_context = lifespan
