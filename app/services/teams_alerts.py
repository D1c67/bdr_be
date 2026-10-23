"""Teams copy of every mirrored notification: the "BDR Alerts" bot (0175).

Whatever goes to a person's inbox through the notification email mirror
(services/notification_email.queue) is also sent to them in Microsoft Teams as
a 1:1 message from the BDR Alerts bot: an Adaptive Card with the same heading,
project and message, and a button to the same deep link the email uses. Setup
and the Azure side live in docs/TEAMS_ALERTS.md.

How a message reaches a person (all app-only, no user sign-in):
1. Their Entra object id, from Graph by their profile email.
2. The BDR Alerts Teams app installed for them through Graph (proactive
   install; 409 = already installed), found in the org catalog by
   teams_app_manifest_id.
3. The bot's 1:1 chat id from that installation.
4. The card posted to that chat through the Bot Connector, signed in as the
   bot's app registration.
Steps 1 to 3 run once per person: the object id and chat id are cached in
teams_alert_users. A cached chat that stops working (the person removed the
app) is resolved again once before the send counts as failed.

Same contract as the email mirror:
- Fire-and-forget: `queue()` hands off to a daemon thread; a Teams failure
  never blocks or breaks the flow that created the notification.
- Best-effort: every send is isolated; the last error is kept per person for
  the Settings status line.
- Self-gating: sends only when teams_alerts_enabled is set and the Graph and
  bot credentials exist. Tests force the flag off (tests/conftest.py).
- Internal users only: the external estimator never gets a Teams copy.
- An active RFP test session (docs/RFP_TESTING.md) holds every Teams send:
  test mode rewrites outgoing mail to one address and has no Teams
  equivalent, so it fails closed.

Outside text (an RFP-derived project name, a note preview) reaches the card
only through RichTextBlock TextRuns, which Teams renders as plain text: no
markdown, so no link the house did not write.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import msal

from app.core.config import get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import graph_email, notification_email

logger = logging.getLogger(__name__)

TABLE = "teams_alert_users"

_BOT_SCOPE = ["https://api.botframework.com/.default"]
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# A card message body stays far under the Teams 28 KB activity limit.
_MESSAGE_MAX_CHARS = 4000
# The installation's chat can 404 for a moment right after a fresh install.
_CHAT_LOOKUP_ATTEMPTS = 3
_CHAT_LOOKUP_DELAY_SECONDS = 2.0

_bot_app: msal.ConfidentialClientApplication | None = None
_catalog_app_id: str | None = None


class TeamsAlertError(Exception):
    """A Teams delivery step failed. The message is shown on the Settings
    status line, so it says what to fix in plain words."""


def _bot_credentials() -> tuple[str, str]:
    s = get_settings()
    return (s.teams_bot_app_id or s.ms_client_id, s.teams_bot_app_secret or s.ms_client_secret)


def is_configured() -> bool:
    """Whether this server sends Teams alerts at all."""
    s = get_settings()
    app_id, secret = _bot_credentials()
    return bool(
        s.teams_alerts_enabled
        and s.ms_tenant_id
        and s.ms_client_id
        and app_id
        and secret
        and s.teams_app_manifest_id
    )


# ── Delivery from the notification mirror ────────────────────────────────────


def queue(rows: list[dict]) -> None:
    """Schedule Teams alerts for freshly created notification rows (the same
    rows, with their `id`, handed to the email mirror). No-op, never raises,
    when the feature is off or not configured."""
    rows = [r for r in (rows or []) if r.get("user_id")]
    if not rows or not is_configured():
        return
    threading.Thread(target=_run, args=(rows,), daemon=True).start()


def _run(rows: list[dict]) -> None:
    try:
        sb = get_supabase()
        if graph_email._active_test_session(sb):
            logger.info("Teams alerts held: an RFP test session is active (%d rows)", len(rows))
            return
        user_ids = sorted({r["user_id"] for r in rows})
        profiles = (
            sb.table("profiles")
            .select("id, full_name, email, role, is_active")
            .in_("id", user_ids)
            .execute()
        ).data or []
        profile_by_id = {p["id"]: p for p in profiles}
        states = (sb.table(TABLE).select("*").in_("user_id", user_ids).execute()).data or []
        state_by_user = {s["user_id"]: s for s in states}
        project_ids = sorted({r["project_id"] for r in rows if r.get("project_id")})
        project_by_id: dict[str, dict] = {}
        if project_ids:
            projects = (
                sb.table("projects").select("id, name, number").in_("id", project_ids).execute()
            ).data or []
            project_by_id = {p["id"]: p for p in projects}
    except Exception:  # noqa: BLE001 - background work must never crash the worker
        logger.exception("Teams alert batch setup failed")
        return

    for row in rows:
        try:
            _send_row(
                row,
                profile_by_id.get(row["user_id"]),
                project_by_id.get(row.get("project_id")),
                state_by_user,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Teams alert failed (type=%s user=%s project=%s)",
                row.get("type"), row.get("user_id"), row.get("project_id"),
            )


def _send_row(
    row: dict, profile: dict | None, project: dict | None, state_by_user: dict[str, dict]
) -> None:
    if not _eligible(profile):
        return
    user_id = profile["id"]
    state = state_by_user.get(user_id) or {}
    if state.get("enabled") is False:
        return
    type_ = row.get("type") or ""
    heading, cta_label = notification_email._meta(type_)
    card = build_card(
        heading=heading,
        message=row.get("message") or "",
        project_label=notification_email._project_label(project),
        cta_label=cta_label,
        cta_url=notification_email._deep_link(
            row.get("project_id"), profile.get("role"), type_, row.get("metadata")
        ),
    )
    summary = _summary(heading, project, type_, row.get("metadata"))
    try:
        state_by_user[user_id] = _deliver(user_id, profile["email"], card, summary, state)
    except TeamsAlertError as exc:
        logger.warning("Teams alert not delivered to user %s: %s", user_id, exc)
        return
    if row.get("id"):
        try:
            get_supabase().table("notifications").update({"teams_sent_at": _now()}).eq(
                "id", row["id"]
            ).execute()
        except Exception:  # noqa: BLE001 - the alert is already delivered
            logger.warning("Could not stamp teams_sent_at on %s", row["id"], exc_info=True)


def _eligible(profile: dict | None) -> bool:
    return bool(
        profile
        and profile.get("is_active")
        and profile.get("email")
        and profile.get("role") != Role.ESTIMATOR.value
    )


def _summary(heading: str, project: dict | None, type_: str, metadata: dict | None) -> str:
    """The one line Teams shows in the toast and the chat list: the email
    subject without the "G3 BDR" prefix, with a middle dot in place of the
    older subjects' em dash."""
    subject = notification_email._subject(heading, project, type_, metadata)
    return subject.removeprefix("G3 BDR · ").replace(" — ", " · ")


# ── The card ─────────────────────────────────────────────────────────────────


def _plain(text: str, **run) -> dict:
    """Outside text as a plain-text block (TextRuns take no markdown)."""
    return {"type": "RichTextBlock", "inlines": [{"type": "TextRun", "text": text, **run}]}


def build_card(
    *, heading: str, message: str, project_label: str | None, cta_label: str, cta_url: str
) -> dict:
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "G3 BDR",
            "size": "Small",
            "weight": "Bolder",
            "color": "Accent",
            "spacing": "None",
        },
        {
            "type": "TextBlock",
            "text": heading,
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Small",
        },
    ]
    if project_label:
        body.append({**_plain(project_label, isSubtle=True), "spacing": "None"})
    text = message.strip()
    if len(text) > _MESSAGE_MAX_CHARS:
        text = text[: _MESSAGE_MAX_CHARS - 1].rstrip() + "…"
    if text:
        body.append({**_plain(text), "spacing": "Medium"})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [{"type": "Action.OpenUrl", "title": cta_label, "url": cta_url}],
    }


# ── Delivery ─────────────────────────────────────────────────────────────────


def _deliver(user_id: str, email: str, card: dict, summary: str, state: dict) -> dict:
    """Send one card to one person, resolving (or re-resolving) their chat as
    needed. Records the outcome on their teams_alert_users row and returns the
    updated state. Raises TeamsAlertError when it could not be delivered."""
    try:
        chat_id = state.get("conversation_id")
        resolved_now = False
        if not chat_id:
            state = _resolve(user_id, email, state)
            chat_id, resolved_now = state["conversation_id"], True
        try:
            _post_card(chat_id, card, summary)
        except _ChatGone:
            if resolved_now:
                raise TeamsAlertError("Teams refused the new BDR Alerts chat") from None
            # The person removed the app or the chat is gone: install again once.
            state = _resolve(user_id, email, {**state, "conversation_id": None})
            try:
                _post_card(state["conversation_id"], card, summary)
            except _ChatGone:
                raise TeamsAlertError("Teams refused the BDR Alerts chat") from None
    except TeamsAlertError as exc:
        _save_state(user_id, {"last_error": str(exc)[:500], "last_error_at": _now()})
        raise
    sent = {"last_sent_at": _now(), "last_error": None, "last_error_at": None}
    _save_state(user_id, sent)
    return {**state, **sent}


def _resolve(user_id: str, email: str, state: dict) -> dict:
    """Entra object id -> app installed -> the bot's 1:1 chat id; cached."""
    aad_id = state.get("aad_object_id") or _lookup_user(email)
    catalog_id = _catalog_id()
    _install(aad_id, catalog_id)
    chat_id = _chat_id(aad_id, catalog_id)
    fields = {"aad_object_id": aad_id, "conversation_id": chat_id}
    _save_state(user_id, fields)
    return {**state, **fields}


def _graph(
    method: str,
    path: str,
    *,
    step: str,
    ok: tuple[int, ...] = (),
    forbidden: str = "the app registration is missing a Graph permission",
    **kw,
) -> httpx.Response | None:
    """graph_email.graph_request with each failure turned into a readable
    TeamsAlertError carrying Microsoft's own message. A status in `ok`
    returns None instead of raising; `forbidden` is the hint for a 401/403."""
    try:
        return graph_email.graph_request(method, path, **kw)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in ok:
            return None
        detail = _graph_message(exc.response)
        hint = forbidden if code in (401, 403) else "Microsoft Graph refused the call"
        raise TeamsAlertError(
            f"{step}: {hint} (HTTP {code}{': ' + detail if detail else ''})"
        ) from None
    except (httpx.HTTPError, RuntimeError) as exc:
        raise TeamsAlertError(f"{step}: could not reach Microsoft Graph ({exc.__class__.__name__})") from None


def _graph_message(resp: httpx.Response) -> str:
    try:
        return str((resp.json().get("error") or {}).get("message") or "")[:200]
    except ValueError:
        return ""


def _lookup_user(email: str) -> str:
    """The person's Entra object id: by UPN first (usually the email), then by
    the mail attribute for accounts whose UPN differs."""
    resp = _graph(
        "GET", f"/users/{quote(email, safe='@')}", step="Find the Teams user",
        params={"$select": "id"}, ok=(404,),
    )
    if resp is not None:
        return resp.json()["id"]
    escaped = email.replace("'", "''")
    resp = _graph(
        "GET", "/users", step="Find the Teams user",
        params={"$filter": f"mail eq '{escaped}'", "$select": "id"},
    )
    found = (resp.json().get("value") or []) if resp is not None else []
    if not found:
        raise TeamsAlertError(f"No Microsoft 365 account found for {email}")
    return found[0]["id"]


def _catalog_id() -> str:
    """The BDR Alerts app's id in the org app catalog (cached per process)."""
    global _catalog_app_id
    if _catalog_app_id:
        return _catalog_app_id
    manifest_id = get_settings().teams_app_manifest_id.replace("'", "''")
    resp = _graph(
        "GET", "/appCatalogs/teamsApps", step="Find the BDR Alerts app",
        params={"$filter": f"externalId eq '{manifest_id}'", "$select": "id"},
    )
    apps = (resp.json().get("value") or []) if resp is not None else []
    if not apps:
        raise TeamsAlertError(
            "The BDR Alerts app is not in the Teams app catalog yet (upload the app package)"
        )
    _catalog_app_id = apps[0]["id"]
    return _catalog_app_id


def _install(aad_id: str, catalog_id: str) -> None:
    global _catalog_app_id
    try:
        _graph(
            "POST", f"/users/{aad_id}/teamwork/installedApps", step="Install BDR Alerts",
            json={"teamsApp@odata.bind": f"{_GRAPH_BASE}/appCatalogs/teamsApps/{catalog_id}"},
            ok=(409,),  # already installed
            # The "Self" install permission covers only a Teams app whose
            # manifest names this registration in webApplicationInfo.
            forbidden=(
                "Teams will not let BDR install this app package (upload the package "
                "from scripts/build_teams_alerts_app.py, which names the app registration)"
            ),
        )
    except TeamsAlertError:
        _catalog_app_id = None  # the app may have been re-uploaded under a new id
        raise


def _chat_id(aad_id: str, catalog_id: str) -> str:
    for attempt in range(_CHAT_LOOKUP_ATTEMPTS):
        resp = _graph(
            "GET", f"/users/{aad_id}/teamwork/installedApps", step="Open the BDR Alerts chat",
            params={"$expand": "teamsApp", "$filter": f"teamsApp/id eq '{catalog_id}'"},
        )
        installs = (resp.json().get("value") or []) if resp is not None else []
        if installs:
            chat = _graph(
                "GET",
                f"/users/{aad_id}/teamwork/installedApps/{installs[0]['id']}/chat",
                step="Open the BDR Alerts chat",
                ok=(404,),
            )
            if chat is not None and chat.json().get("id"):
                return chat.json()["id"]
        if attempt + 1 < _CHAT_LOOKUP_ATTEMPTS:
            time.sleep(_CHAT_LOOKUP_DELAY_SECONDS)
    raise TeamsAlertError("BDR Alerts is installed but Teams has not opened its chat yet")


class _ChatGone(Exception):
    """The Bot Connector refused the cached chat (403/404)."""


def _bot_token() -> str:
    global _bot_app
    if _bot_app is None:
        app_id, secret = _bot_credentials()
        _bot_app = msal.ConfidentialClientApplication(
            client_id=app_id,
            authority=f"https://login.microsoftonline.com/{get_settings().ms_tenant_id}",
            client_credential=secret,
        )
    result = _bot_app.acquire_token_for_client(scopes=_BOT_SCOPE)
    if "access_token" not in result:
        raise TeamsAlertError(
            f"The bot could not sign in: {result.get('error_description') or result.get('error')}"
        )
    return result["access_token"]


def _post_card(chat_id: str, card: dict, summary: str) -> None:
    base = get_settings().teams_bot_service_url.rstrip("/")
    try:
        resp = httpx.post(
            f"{base}/v3/conversations/{quote(chat_id, safe='')}/activities",
            headers={"Authorization": f"Bearer {_bot_token()}"},
            json={
                "type": "message",
                "summary": summary,
                "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
                ],
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise TeamsAlertError(f"Could not reach Teams ({exc.__class__.__name__})") from None
    if resp.status_code in (403, 404):
        raise _ChatGone()
    if resp.status_code == 401:
        raise TeamsAlertError("Teams rejected the bot's sign-in (check the Azure Bot app id)")
    if resp.status_code >= 400:
        raise TeamsAlertError(f"Teams returned HTTP {resp.status_code}")


# ── Per-user state (Settings) ────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_state(user_id: str, fields: dict) -> None:
    try:
        get_supabase().table(TABLE).upsert(
            {"user_id": user_id, **fields, "updated_at": _now()}, on_conflict="user_id"
        ).execute()
    except Exception:  # noqa: BLE001 - a lost cache row only costs a re-resolve
        logger.warning("Could not save Teams alert state for %s", user_id, exc_info=True)


def get_state(user_id: str) -> dict:
    rows = (
        get_supabase().table(TABLE).select("*").eq("user_id", user_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else {}


def status(user_id: str) -> dict:
    """What the Settings card shows: whether this server sends Teams alerts,
    the person's switch (absent row = on), and the last outcome."""
    state = get_state(user_id)
    return {
        "available": is_configured(),
        "enabled": state.get("enabled") is not False,
        "connected": bool(state.get("conversation_id")),
        "last_sent_at": state.get("last_sent_at"),
        "last_error": state.get("last_error"),
        "last_error_at": state.get("last_error_at"),
    }


def set_enabled(user_id: str, enabled: bool) -> None:
    get_supabase().table(TABLE).upsert(
        {"user_id": user_id, "enabled": enabled, "updated_at": _now()}, on_conflict="user_id"
    ).execute()


def send_test(user_id: str, email: str, full_name: str | None) -> None:
    """Send a test alert now, in the request (Settings > Send a test alert).
    Raises TeamsAlertError with what went wrong."""
    if graph_email._active_test_session():
        raise TeamsAlertError("Teams alerts are held while an RFP test session is active")
    base = get_settings().frontend_url.rstrip("/")
    name = (full_name or "").split(" ")[0] or "there"
    card = build_card(
        heading="Teams alerts are working",
        message=(
            f"Hi {name}, this is a test from BDR Alerts. Notifications that reach "
            "your inbox will show up here too."
        ),
        project_label=None,
        cta_label="Open Settings",
        cta_url=f"{base}/settings",
    )
    _deliver(user_id, email, card, "Teams alerts are working", get_state(user_id))
