"""The canary: a harmless worker self-test type (SPEC.md 12.4 M1).

A canary job proves the whole path end to end without touching any outside
site: claim, a fresh browser context, the model loop (navigate, read_page,
find_text, finish), steps, artifacts and finish. The only page it may open
is BDR's own static GET /agent-worker/v1/canary/page (protocol.CEILINGS
"canary": nav host $api_host, that one path). Success is
finish(succeeded, self_test_ok) once the expected text was found.

Nothing here reads or writes project data, so a canary can run on any
deployment where agents are on; IT Admin starts one from Settings
(POST /browser-agents/admin/canary) after enrolling or updating a worker.
"""

from __future__ import annotations

import html
import uuid
from typing import Any

from pydantic import ValidationError

from app.services.browser_agents import protocol
from app.services.browser_agents.registry import AgentTypeSpec, PrepareResult

KEY = "canary"
# The text the page shows and CanaryInput.expect_text defaults to.
CANARY_TEXT = "BDR canary ok"
CANARY_PRIORITY = 50          # ahead of the default 100: a self-test answers fast
CANARY_TTL_HOURS = 2          # a canary nobody claimed in 2 h is stale news


def canary_page_html() -> str:
    """The static self-test page. No script, no forms, no outside resources,
    no user data: the only thing on it is the canary text and a fixed list
    the model can read."""
    text = html.escape(CANARY_TEXT)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>BDR agent self-test</title></head>"
        "<body>"
        "<h1>BDR agent self-test</h1>"
        f'<p id="canary">{text}</p>'
        "<ul>"
        "<li>Step 1: open this page</li>"
        "<li>Step 2: read it</li>"
        "<li>Step 3: find the canary text</li>"
        "<li>Step 4: finish</li>"
        "</ul>"
        "</body></html>\n"
    )


# The page is fixed, so the only text a canary can ever find is on it.
CANARY_PAGE_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def prepare(job: dict[str, Any]) -> PrepareResult:
    """The seed must parse and ask for text the static page actually shows;
    anything else could never succeed, so it is failed at claim instead of
    burning a browser and model turns on it."""
    try:
        seed = protocol.CanaryInput.model_validate(job.get("input") or {})
    except ValidationError:
        return PrepareResult.fail("unsupported", "The canary seed is not valid")
    if seed.expect_text not in CANARY_TEXT:
        return PrepareResult.fail("unsupported", "The canary page never shows the expected text")
    return PrepareResult.run()


SPEC = AgentTypeSpec(
    key=KEY,
    label="Worker self-test",
    input_model=protocol.CanaryInput,
    checkpoint_model=None,
    identity=None,
    prompt_version="canary.v1",
    succeed_outcomes=("self_test_ok",),
    prepare=prepare,
    allows_capture=False,
    help_reasons=("unexpected_page", "cap_reached", "provider_config", "other"),
    platform=None,
)


def enqueue_canary(*, created_by: str | None = None, expect_text: str = CANARY_TEXT) -> dict[str, Any]:
    """Queue one canary job and return its row. Each call is its own job
    (the dedupe key is fresh); the caller rate limits."""
    from app.services.browser_agents import queue

    result = queue.enqueue(
        KEY,
        f"canary:{uuid.uuid4()}",
        protocol.CanaryInput(expect_text=expect_text).model_dump(mode="json"),
        priority=CANARY_PRIORITY,
        created_by=created_by,
        ttl_hours=CANARY_TTL_HOURS,
    )
    return result.job
