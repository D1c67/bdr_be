"""The model-facing inbox route for the chat tool `inbox_list`
(docs/ASSISTANT_PHASE8_PLAN.md 6.1, 8.1; owner question 16 (a)).

`GET /assistant/tools/inbox` answers the assistant's own turn principal only:
`_require_assistant_principal(user)` first (a human caller gets a bare 404),
then the inbox feature flag (404 `inbox_off`), then
`assistant_inbox.page_for(...)`, which shares the Inbox visibility rules with
the human route instead of copying them. Hidden and dismissed rows are
excluded, a record row reads as read when its source bell was read, and
nothing is ever marked read or dismissed here (the route only reads). The
`project` argument takes a project number (a full project id also works); any
other value (including a short id, which the executor only expands for keys
ending in `_id` or `_ids`) or a number no project carries is a coded 422
`unknown_project` whose message says "Use the project number.", never an
empty page. An empty page means a real project
with no rows.

Every row is flattened to the fields the tool returns (6.1): enums and the
server-rendered title and why templates, nothing else. Full ids are returned;
the executor's trimmers turn them into short ids for the model. The route is
not under a firewall prefix (it is reached only through the turn allowlist the
tool spec derives, WI-10), and it is not in `firewall._ASSISTANT_INBOX`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, status

from app.assistant.tools._common import is_uuid, pt
from app.assistant.triage import project_number
from app.core import supabase_client
from app.core.deps import CurrentUser, require_internal
from app.routers import assistant_inbox
from app.routers.assistant import _require_assistant_principal

logger = logging.getLogger(__name__)

ROUTE_PATH = "/assistant/tools/inbox"
LIMIT_DEFAULT = 20
LIMIT_MAX = 50
UNKNOWN_PROJECT = "unknown_project"

router = APIRouter(tags=["assistant"], dependencies=[Depends(require_internal)])


def _unknown_project() -> Any:
    return assistant_inbox._error(
        status.HTTP_422_UNPROCESSABLE_CONTENT, UNKNOWN_PROJECT,
        "That project could not be found. Use the project number.",
    )


def _resolve_project(value: str | None) -> str | None:
    """The project id to filter by, or None for no project filter. A uuid
    passes through; a project number is looked up once. A value that is
    neither (a short id the executor did not expand, a typo) or a number no
    project carries answers a coded 422 `unknown_project`, so a failed lookup
    is never mistaken for a project with no rows. An unreadable lookup is a
    503 for the same reason."""
    text = (value or "").strip()
    if not text:
        return None
    if is_uuid(text):
        return text.lower()
    number = project_number(text)
    if number is None:
        raise _unknown_project()
    try:
        rows = (supabase_client.get_supabase().table("projects").select("id").eq("number", number)
                .limit(1).execute()).data or []
    except Exception as exc:  # noqa: BLE001 - an unreadable lookup is not "no rows"
        logger.exception("assistant inbox tool: project number lookup failed")
        raise assistant_inbox._error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "project_lookup_failed",
            "The project could not be looked up right now. Try again shortly.",
        ) from exc
    if not rows:
        raise _unknown_project()
    return str(rows[0]["id"])


def _row(item: dict[str, Any]) -> dict[str, Any]:
    triage = item.get("triage")
    project = item.get("project") or {}
    return {
        "id": item["id"],
        "kind": item.get("event_type"),
        "kind_label": item.get("kind_label"),
        "group": item.get("group"),
        "severity": item.get("severity"),
        "delivery": item.get("delivery"),
        "from_bell": item.get("delivery") == "record",
        "project_number": project.get("number"),
        "project_id": project.get("id"),
        "received_at": pt(item.get("created_at")),
        "read": bool(item.get("read")),
        "triage": (
            {"decision": triage.get("decision"), "reason": triage.get("reason"), "action": triage.get("action")}
            if triage else None
        ),
        "summary": item.get("title"),
        "detail": item.get("why"),
    }


def _digest(digest: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": digest["id"], "slot": digest.get("slot"), "date": digest.get("digest_date"),
        "item_count": digest.get("item_count"), "record_count": digest.get("record_count"),
    }


@router.get(ROUTE_PATH)
def list_inbox_for_assistant(
    segment: str = Query(default="unread", pattern="^(unread|all|digests)$"),
    kind: str | None = Query(default=None, max_length=64),
    project: str | None = Query(default=None, max_length=64),
    before: str | None = Query(default=None, max_length=96),
    limit: int = Query(default=LIMIT_DEFAULT, ge=1, le=LIMIT_MAX),
    user: CurrentUser = Depends(require_internal),
) -> dict[str, Any]:
    _require_assistant_principal(user)
    if not assistant_inbox.features_flag(user):
        raise assistant_inbox._error(status.HTTP_404_NOT_FOUND, assistant_inbox.INBOX_OFF)
    project_id = _resolve_project(project)
    page = assistant_inbox.page_for(
        user, segment, assistant_inbox.parse_cursor(before), limit, (kind or "").strip() or None, project_id,
    )
    out: dict[str, Any] = {"items": [_row(item) for item in page["items"]], "next": page["next"]}
    if segment == "digests":
        out["digests"] = [_digest(d) for d in page.get("digests", [])]
    return out
