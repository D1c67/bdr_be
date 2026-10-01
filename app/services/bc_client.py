"""BuildingConnected (Autodesk Platform Services) client and OAuth token
store (docs/RFP_BUILDINGCONNECTED.md section 4; BUILD_CONTRACT D26, D27
and 3.1).

Three-legged OAuth: one user with `bidBoardPermissions.viewAll` connects
from Settings; the refresh token keeps the connection alive. The tokens
live in `rfp_oauth_connections` (one row per provider, RLS forced, service
role only) and are never returned by an endpoint or written to a log line.
`rfp_oauth_states` holds the single-use, ten-minute `state` that binds the
unauthenticated callback to the user who started the flow.

Facts the code is built on (verified 2026-09-26):

- Refresh tokens rotate on every refresh and the previous one dies at once
  (`invalid_grant`). Two workers refreshing concurrently would therefore
  kill the connection, so the refresh runs under a 30 second row lock
  (D27): a conditional UPDATE claims it, the loser polls every second for
  up to 30 seconds and then reads the token the winner wrote.
- The new refresh token is written in the same UPDATE that clears the lock,
  before any API call uses the new access token.
- Access tokens last 3,599 s; a token is treated as expired 120 s early.
- The API refuses a 2-legged token with a 401 whose detail names it; a 401
  triggers one refresh and one retry, then `BcDisconnected`.
- Rate limit 1,000 requests per minute per user; a 429 carries
  `Retry-After` (slept, capped at 120 s, up to `max_retries` times).
- Page size max is 100; paging follows `pagination.cursorState`.

Every request logs method, path, status and elapsed time only. The Settings
object, the client secret, the tokens and the request headers never reach a
log line, an exception message or a stored row other than the token row.
"""

from __future__ import annotations

import base64
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlencode

import httpx

from app.core.roles import Role
from app.services.notifications import notify_role

logger = logging.getLogger(__name__)

PROVIDER = "buildingconnected"
BC_BASE = "https://developer.api.autodesk.com/construction/buildingconnected/v2"
AUTH_BASE = "https://developer.api.autodesk.com/authentication/v2"
DEEP_LINK = "https://app.buildingconnected.com/opportunities/{id}/info"

CONNECTIONS_TABLE = "rfp_oauth_connections"
STATES_TABLE = "rfp_oauth_states"
NOTIFY_DISCONNECTED = "rfp_bc.disconnected"

DEFAULT_SCOPE = "data:read"
PAGE_LIMIT_MAX = 100
TEST_MODE_HEADER = "x-bc-mode"

# Per-process lock owner (D27): there is no global worker id, only per-process
# tokens (llm_queue._WORKER_TOKEN, rfp_email_ingest._RUNNER_TOKEN).
_WORKER_TOKEN = uuid.uuid4().hex

_EXPIRY_SKEW_SECONDS = 120
_REFRESH_LOCK_SECONDS = 30
_REFRESH_WAIT_SECONDS = 30
_REFRESH_POLL_SECONDS = 1.0
_RETRY_AFTER_CAP_SECONDS = 120
_RETRY_AFTER_DEFAULT_SECONDS = 5
_SERVER_ERROR_RETRIES = 2
_SERVER_ERROR_BACKOFF_SECONDS = (2.0, 5.0)
_ERROR_MAX_CHARS = 500
_STATE_BYTES = 32
# secrets.token_urlsafe(32): 43 URL-safe base64 characters, no padding.
_STATE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_REVOKE_TIMEOUT_SECONDS = 10.0
NOTIFY_ACCOUNT_CHANGED = "rfp_bc.account_changed"

_MSG_NOT_CONNECTED = "BuildingConnected is not connected. Connect it from RFP ingestion settings."
_MSG_REFRESH_REJECTED = "BuildingConnected rejected the refresh token; reconnect from RFP ingestion settings."
_MSG_REFRESH_WAIT = "Another worker is refreshing the BuildingConnected token; the wait timed out."
_MSG_UNAUTHORIZED = "BuildingConnected refused the access token"
_MSG_UNAUTHORIZED_AFTER_REFRESH = (
    "BuildingConnected refused a freshly issued access token (Bid Board access may have been "
    "lost); reconnect from RFP ingestion settings."
)
_MSG_RATE_LIMITED = "BuildingConnected rate limit reached; retry later."
_MSG_TRANSPORT = "The BuildingConnected request failed"
_MSG_VIEW_ALL = "This Autodesk user cannot see the whole Bid Board."


# ── Errors ───────────────────────────────────────────────────────────────────


class BcError(Exception):
    """Base: `.status` is the HTTP status when one was seen."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BcDisconnected(BcError):
    """No usable connection: no row, disconnected, or the refresh failed."""


class BcRateLimited(BcError):
    """429 after the retries; `.retry_after` is the last Retry-After in seconds."""

    def __init__(self, message: str, *, status: int | None = 429, retry_after: float | None = None) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class BcTransient(BcError):
    """5xx or a transport failure after the retries."""


class BcPermanent(BcError):
    """Any other 4xx: a bad request, a missing resource, a refused scope."""


# ── Config ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BcConfig:
    client_id: str
    client_secret: str
    redirect_url: str
    timeout_seconds: float
    test_mode: bool
    max_retries: int = 3

    def __repr__(self) -> str:  # never the secret
        return (
            f"BcConfig(client_id={'set' if self.client_id else 'blank'}, "
            f"client_secret={'set' if self.client_secret else 'blank'}, "
            f"redirect_url={self.redirect_url!r}, timeout_seconds={self.timeout_seconds}, "
            f"test_mode={self.test_mode}, max_retries={self.max_retries})"
        )


def config_from_settings(settings) -> BcConfig:
    """The settings block of contract 3.7, read through attributes so a
    SimpleNamespace or a Settings-like stub works in tests."""
    return BcConfig(
        client_id=str(getattr(settings, "building_connected_client_id", "") or "").strip(),
        client_secret=str(getattr(settings, "building_connected_client_secret", "") or ""),
        redirect_url=str(getattr(settings, "rfp_bc_redirect_url", "") or "").strip(),
        timeout_seconds=float(getattr(settings, "rfp_bc_request_timeout_seconds", 60.0) or 60.0),
        test_mode=bool(getattr(settings, "rfp_bc_test_mode", False)),
        max_retries=int(getattr(settings, "rfp_bc_max_retries", 3) or 3),
    )


# ── Time ─────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def updated_filter(since: datetime) -> str:
    """The `filter[updatedAt]` value for "updated at or after `since`":
    `YYYY-MM-DDThh:mm:ss.SSSZ..` (an open-ended range)."""
    utc = since.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z.."


# ── OAuth: authorize URL and state ───────────────────────────────────────────


def authorize_url(config: BcConfig, state: str, scope: str = DEFAULT_SCOPE) -> str:
    query = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_url,
        "scope": scope,
        "state": state,
    }
    return f"{AUTH_BASE}/authorize?{urlencode(query)}"


def new_state(sb, actor_id: str, ttl_seconds: int = 600) -> str:
    """A random single-use state bound to the actor, stored with its expiry."""
    state = secrets.token_urlsafe(_STATE_BYTES)
    now = _now()
    sb.table(STATES_TABLE).insert(
        {
            "state": state,
            "provider": PROVIDER,
            "actor_id": actor_id,
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=ttl_seconds)),
        }
    ).execute()
    return state


def state_well_formed(state: Any) -> bool:
    """True when `state` has the shape new_state mints (token_urlsafe(32):
    43 characters of [A-Za-z0-9_-]). Anything else is refused before a
    query, so junk on the unauthenticated callback never reaches the DB."""
    return isinstance(state, str) and bool(_STATE_RE.fullmatch(state))


def consume_state(sb, state: str) -> dict | None:
    """Delete-on-use: the row when it existed and had not expired, else
    None. An expired row is deleted too (nothing lingers). A value that is
    not state-shaped answers None without a query."""
    if not state_well_formed(state):
        return None
    rows = (
        sb.table(STATES_TABLE)
        .delete()
        .eq("state", state)
        .eq("provider", PROVIDER)
        .execute()
    ).data or []
    if not rows:
        return None
    row = rows[0]
    expires_at = _parse_ts(row.get("expires_at"))
    if expires_at is None or expires_at < _now():
        return None
    return row


# ── HTTP plumbing ────────────────────────────────────────────────────────────


def _basic_auth(config: BcConfig) -> str:
    raw = f"{config.client_id}:{config.client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _api_headers(access_token: str, config: BcConfig) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if config.test_mode:
        headers[TEST_MODE_HEADER] = "test"
    return headers


def _client(config: BcConfig, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(config.timeout_seconds),
        http2=False,
        follow_redirects=False,
    )


def _detail(response: httpx.Response) -> str:
    """The error text a response carries, capped; never the headers."""
    text = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("detail", "message", "error_description", "developerMessage", "error", "title"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if isinstance(nested, str) and nested.strip():
                    text = nested.strip()
                    break
    if not text:
        text = (response.text or "").strip()
    return " ".join(text.split())[:_ERROR_MAX_CHARS]


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise BcTransient("BuildingConnected answered with something other than JSON.", status=response.status_code) from exc
    return body if isinstance(body, dict) else {"results": body}


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after")
    seconds: float | None = None
    if raw:
        try:
            seconds = float(raw.strip())
        except ValueError:
            seconds = None
    if seconds is None or seconds < 0:
        seconds = float(_RETRY_AFTER_DEFAULT_SECONDS)
    return min(seconds, float(_RETRY_AFTER_CAP_SECONDS))


def _log(method: str, path: str, status: int | str, started: float) -> None:
    logger.info("bc client: %s %s -> %s in %.0f ms", method, path, status, (time.monotonic() - started) * 1000)


def _classify(response: httpx.Response, *, what: str) -> BcError:
    """The exception for a non-2xx answer (no retries here)."""
    status = response.status_code
    detail = _detail(response)
    if status == 401:
        return BcDisconnected(f"{_MSG_UNAUTHORIZED} ({what}): {detail}", status=status)
    if status == 429:
        return BcRateLimited(_MSG_RATE_LIMITED, retry_after=_retry_after(response))
    if status >= 500:
        return BcTransient(f"BuildingConnected answered {status} ({what}): {detail}", status=status)
    return BcPermanent(f"BuildingConnected answered {status} ({what}): {detail}", status=status)


def _token_request(config: BcConfig, form: dict, *, transport, what: str) -> dict:
    """POST /token with basic auth. Returns the JSON body; raises BcPermanent
    on a 4xx (the caller reads `.status` and the message for invalid_grant),
    BcTransient on 5xx or transport failure."""
    started = time.monotonic()
    path = "/token"
    with _client(config, transport) as client:
        try:
            response = client.post(
                f"{AUTH_BASE}{path}",
                data=form,
                headers={"Authorization": _basic_auth(config), "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            _log("POST", path, "transport-error", started)
            raise BcTransient(f"{_MSG_TRANSPORT} ({what}): {type(exc).__name__}") from exc
    _log("POST", path, response.status_code, started)
    if response.status_code >= 500:
        raise BcTransient(
            f"BuildingConnected token endpoint answered {response.status_code} ({what}).",
            status=response.status_code,
        )
    if response.status_code >= 400:
        exc = BcPermanent(
            f"BuildingConnected token endpoint answered {response.status_code} ({what}): {_detail(response)}",
            status=response.status_code,
        )
        exc.code = _oauth_error_code(response)
        raise exc
    return _json(response)


def _oauth_error_code(response: httpx.Response) -> str | None:
    """The OAuth `error` code of a token-endpoint refusal (invalid_grant,
    invalid_client, ...)."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error") if isinstance(body, dict) else None
    return code.strip().lower() if isinstance(code, str) and code.strip() else None


def _is_invalid_grant(exc: BcPermanent) -> bool:
    return getattr(exc, "code", None) == "invalid_grant" or "invalid_grant" in str(exc).lower()


def exchange_code(config: BcConfig, code: str, *, transport=None) -> dict:
    """The authorization code for tokens: {access_token, refresh_token,
    expires_in, token_type, ...}."""
    body = _token_request(
        config,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": config.redirect_url},
        transport=transport,
        what="code exchange",
    )
    if not body.get("access_token"):
        raise BcPermanent("BuildingConnected returned no access token for the code.")
    return body


def fetch_me(access_token: str, config: BcConfig, *, transport=None) -> dict:
    """GET /users/me with the fresh token (the callback checks viewAll)."""
    started = time.monotonic()
    path = "/users/me"
    with _client(config, transport) as client:
        try:
            response = client.get(f"{BC_BASE}{path}", headers=_api_headers(access_token, config))
        except httpx.HTTPError as exc:
            _log("GET", path, "transport-error", started)
            raise BcTransient(f"{_MSG_TRANSPORT} (users/me): {type(exc).__name__}") from exc
    _log("GET", path, response.status_code, started)
    if response.status_code >= 400:
        raise _classify(response, what="users/me")
    return _json(response)


def view_all(me: dict) -> bool:
    permissions = (me or {}).get("bidBoardPermissions")
    return bool(isinstance(permissions, dict) and permissions.get("viewAll"))


# ── Connection row ───────────────────────────────────────────────────────────


def _expires_at(tokens: dict, now: datetime) -> str | None:
    try:
        seconds = int(tokens.get("expires_in") or 0)
    except (TypeError, ValueError):
        seconds = 0
    return _iso(now + timedelta(seconds=seconds)) if seconds > 0 else None


def _display_name(me: dict) -> str | None:
    parts = [str(me.get(k) or "").strip() for k in ("firstName", "lastName")]
    name = " ".join(p for p in parts if p)
    return name or (str(me.get("email") or "").strip() or None)


def _account_ids(row: dict | None) -> tuple[str | None, str | None]:
    def _s(value) -> str | None:
        return str(value).strip() or None if value is not None else None

    row = row or {}
    return _s(row.get("external_user_id")), _s(row.get("external_company_id"))


def _notify_account_changed(sb, previous: dict, me: dict) -> None:
    """One bell to IT Admins when a connect replaces a different Autodesk
    user or company (a wrong account would feed another board into the
    pipeline). Bell row only. Best effort: never blocks the connect."""
    old_user, old_company = _account_ids(previous)
    new_user, new_company = _account_ids({"external_user_id": (me or {}).get("id"),
                                          "external_company_id": (me or {}).get("companyId")})
    message = (
        "The BuildingConnected connection now uses a different Autodesk account "
        f"({previous.get('external_user_email') or old_user or 'unknown'} -> "
        f"{(me or {}).get('email') or new_user or 'unknown'}). "
        "Check RFP ingestion settings if this was not expected."
    )
    metadata = {
        "provider": PROVIDER,
        "previous_user_id": old_user, "previous_company_id": old_company,
        "user_id": new_user, "company_id": new_company,
    }
    try:
        notify_role(Role.IT_ADMIN, None, NOTIFY_ACCOUNT_CHANGED, message, mirror_email=False, metadata=metadata)
    except Exception:  # noqa: BLE001 - the bell never masks the connect
        logger.exception("bc client: account changed bell failed")


def store_connection(sb, tokens: dict, me: dict, actor_id: str | None) -> dict:
    """Upsert the provider row as connected. Returns the row. When the row
    it replaces belonged to a different Autodesk user or company, IT Admins
    get a bell (`rfp_bc.account_changed`)."""
    try:
        previous = connection(sb)
    except Exception:  # noqa: BLE001 - the comparison is advisory
        logger.warning("bc client: could not read the previous connection", exc_info=True)
        previous = None
    now = _now()
    payload = {
        "provider": PROVIDER,
        "status": "connected",
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": _expires_at(tokens, now),
        "scope": tokens.get("scope") or DEFAULT_SCOPE,
        "connected_by": actor_id,
        "connected_at": _iso(now),
        "external_user_id": (me or {}).get("id"),
        "external_user_name": _display_name(me or {}),
        "external_user_email": (me or {}).get("email"),
        "external_company_id": (me or {}).get("companyId"),
        "view_all": view_all(me or {}),
        "last_refresh_at": None,
        "last_used_at": None,
        "last_error": None,
        "refresh_lock_until": None,
        "refresh_lock_owner": None,
        "disconnected_at": None,
        "updated_at": _iso(now),
    }
    rows = (sb.table(CONNECTIONS_TABLE).upsert(payload, on_conflict="provider").execute()).data or []
    logger.info("bc client: connection stored for %s (view_all=%s)", payload["external_user_email"], payload["view_all"])
    old_user, old_company = _account_ids(previous)
    new_user, new_company = _account_ids(payload)
    if (old_user or old_company) and (old_user, old_company) != (new_user, new_company):
        _notify_account_changed(sb, previous or {}, me or {})
    return rows[0] if rows else connection(sb) or payload


def revoke_token(config: BcConfig, token: str, token_type_hint: str, *, transport=None) -> bool:
    """POST /revoke (APS authentication v2) with client basic auth. Best
    effort: True on a 2xx, False on anything else; never raises and never
    logs the token."""
    if not (token and config.client_id and config.client_secret):
        return False
    started = time.monotonic()
    path = "/revoke"
    try:
        with httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(min(config.timeout_seconds, _REVOKE_TIMEOUT_SECONDS)),
            http2=False,
            follow_redirects=False,
        ) as client:
            response = client.post(
                f"{AUTH_BASE}{path}",
                data={"token": token, "token_type_hint": token_type_hint},
                headers={"Authorization": _basic_auth(config), "Accept": "application/json"},
            )
    except Exception as exc:  # noqa: BLE001 - best effort
        _log("POST", path, "transport-error", started)
        logger.warning("bc client: revoke %s failed (%s)", token_type_hint, type(exc).__name__)
        return False
    _log("POST", path, response.status_code, started)
    if response.status_code >= 300:
        logger.warning("bc client: revoke %s answered %s", token_type_hint, response.status_code)
        return False
    return True


def disconnect(sb, *, error: str | None = None, config: BcConfig | None = None, transport=None) -> None:
    """Clear the tokens and mark the row disconnected (a row is created when
    none exists so the status endpoint always has one to read). With a
    `config` (a person pressed Disconnect) the stored refresh and access
    tokens are first revoked at Autodesk, best effort: a failed revoke is
    logged and the row is cleared all the same. The automatic disconnects
    (refresh rejected, token refused) pass no config: those tokens are
    already dead."""
    if config is not None:
        try:
            row = connection(sb) or {}
        except Exception:  # noqa: BLE001 - never blocks the disconnect
            logger.warning("bc client: could not read the connection to revoke", exc_info=True)
            row = {}
        if row.get("refresh_token"):
            revoke_token(config, row["refresh_token"], "refresh_token", transport=transport)
        if row.get("access_token"):
            revoke_token(config, row["access_token"], "access_token", transport=transport)
    now = _now()
    payload = {
        "provider": PROVIDER,
        "status": "disconnected",
        "access_token": None,
        "refresh_token": None,
        "expires_at": None,
        "last_error": (error or "")[:_ERROR_MAX_CHARS] or None,
        "refresh_lock_until": None,
        "refresh_lock_owner": None,
        "disconnected_at": _iso(now),
        "updated_at": _iso(now),
    }
    sb.table(CONNECTIONS_TABLE).upsert(payload, on_conflict="provider").execute()
    logger.warning("bc client: connection disconnected%s", " (refresh rejected)" if error else "")


def connection(sb) -> dict | None:
    """The full provider row (tokens included): callers redact."""
    rows = (sb.table(CONNECTIONS_TABLE).select("*").eq("provider", PROVIDER).limit(1).execute()).data or []
    return rows[0] if rows else None


_STATUS_KEYS = (
    "connected_by",
    "connected_at",
    "external_user_name",
    "external_user_email",
    "view_all",
    "last_refresh_at",
    "last_used_at",
    "last_error",
)


def connection_status(sb) -> dict:
    """What the router returns: never the tokens, never the lock."""
    row = connection(sb) or {}
    out = {"status": row.get("status") or "disconnected"}
    for key in _STATUS_KEYS:
        out[key] = row.get(key)
    return out


_BELL_REFRESH_REJECTED = (
    "BuildingConnected disconnected: the refresh token was rejected. "
    "Scans stop until someone reconnects from RFP ingestion settings."
)
_BELL_UNAUTHORIZED_AFTER_REFRESH = (
    "BuildingConnected disconnected: the Bid Board refused a freshly issued token (the "
    "connected user may have lost Bid Board access). Scans stop until someone reconnects "
    "from RFP ingestion settings."
)


def _notify_disconnected(sb, error: str | None, *, message: str = _BELL_REFRESH_REJECTED) -> None:
    """One bell to IT Admins and Executives (D7), deduped while an unread one
    exists, mirrored to email. Best effort: a bell failure never changes
    what happened to the connection."""
    try:
        pending = (
            sb.table("notifications")
            .select("id")
            .eq("type", NOTIFY_DISCONNECTED)
            .is_("read_at", "null")
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        ).data
        if pending:
            return
        metadata = {"provider": PROVIDER, "error": (error or "")[:_ERROR_MAX_CHARS]}
        for role in (Role.IT_ADMIN, Role.EXECUTIVE):
            notify_role(role, None, NOTIFY_DISCONNECTED, message, mirror_email=True, metadata=metadata)
    except Exception:  # noqa: BLE001 - the bell never masks the disconnect
        logger.exception("bc client: disconnected bell failed")


# ── Access token with the refresh lock (D27) ─────────────────────────────────


def _fresh(row: dict, now: datetime) -> str | None:
    """The stored access token when it is connected and not about to expire."""
    if not row or row.get("status") != "connected":
        return None
    token = row.get("access_token")
    expires_at = _parse_ts(row.get("expires_at"))
    if not token or expires_at is None:
        return None
    return token if expires_at - timedelta(seconds=_EXPIRY_SKEW_SECONDS) > now else None


def _claim_refresh(sb, now: datetime) -> dict | None:
    """The conditional UPDATE: ours when the lock is free or expired. The
    ISO value is computed here (+00:00) so the row and the filter compare
    the same spelling; PostgREST cannot take now()."""
    now_iso = _iso(now)
    rows = (
        sb.table(CONNECTIONS_TABLE)
        .update(
            {
                "refresh_lock_until": _iso(now + timedelta(seconds=_REFRESH_LOCK_SECONDS)),
                "refresh_lock_owner": _WORKER_TOKEN,
                "updated_at": now_iso,
            }
        )
        .eq("provider", PROVIDER)
        .eq("status", "connected")
        .or_(f"refresh_lock_until.is.null,refresh_lock_until.lt.{now_iso}")
        .execute()
    ).data or []
    return rows[0] if rows else None


def _release_refresh(sb, now: datetime) -> None:
    sb.table(CONNECTIONS_TABLE).update(
        {"refresh_lock_until": None, "refresh_lock_owner": None, "updated_at": _iso(now)}
    ).eq("provider", PROVIDER).eq("refresh_lock_owner", _WORKER_TOKEN).execute()


def _refresh(sb, config: BcConfig, row: dict, *, transport, now_fn: Callable[[], datetime]) -> str:
    """Holding the lock: rotate the tokens. The new refresh token is written
    in the same UPDATE that clears the lock, before anything uses the new
    access token. `invalid_grant` disconnects the row and rings the bell."""
    refresh_token = row.get("refresh_token")
    if not refresh_token:
        _release_refresh(sb, now_fn())
        raise BcDisconnected(_MSG_NOT_CONNECTED)
    try:
        tokens = _token_request(
            config,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            transport=transport,
            what="refresh",
        )
    except BcPermanent as exc:
        if _is_invalid_grant(exc):
            disconnect(sb, error=_MSG_REFRESH_REJECTED)
            _notify_disconnected(sb, _MSG_REFRESH_REJECTED)
            raise BcDisconnected(_MSG_REFRESH_REJECTED, status=exc.status) from exc
        _release_refresh(sb, now_fn())
        raise
    except BcError:
        _release_refresh(sb, now_fn())
        raise
    access = tokens.get("access_token")
    if not access:
        _release_refresh(sb, now_fn())
        raise BcTransient("BuildingConnected returned no access token on refresh.")
    now = now_fn()
    written = (
        sb.table(CONNECTIONS_TABLE)
        .update(
            {
                "access_token": access,
                "refresh_token": tokens.get("refresh_token") or refresh_token,
                "expires_at": _expires_at(tokens, now),
                "last_refresh_at": _iso(now),
                "last_error": None,
                "refresh_lock_until": None,
                "refresh_lock_owner": None,
                "updated_at": _iso(now),
            }
        )
        .eq("provider", PROVIDER)
        .eq("status", "connected")
        .execute()
    ).data or []
    if not written:
        raise BcDisconnected(_MSG_NOT_CONNECTED)
    logger.info("bc client: token refreshed")
    return access


def _obtain(
    sb,
    config: BcConfig,
    *,
    transport,
    sleep: Callable[[float], None],
    now_fn: Callable[[], datetime],
    stale_token: str | None = None,
) -> str:
    """The stored token when fresh (and not the one that just failed), else a
    refresh under the lock, else the token another worker wrote while we
    waited."""
    row = connection(sb)
    if not row or row.get("status") != "connected":
        raise BcDisconnected(_MSG_NOT_CONNECTED)
    token = _fresh(row, now_fn())
    if token and token != stale_token:
        return token
    deadline = now_fn() + timedelta(seconds=_REFRESH_WAIT_SECONDS)
    while True:
        now = now_fn()
        claimed = _claim_refresh(sb, now)
        if claimed is not None:
            token = _fresh(claimed, now)
            if token and token != stale_token:
                # Someone refreshed between our read and our claim.
                _release_refresh(sb, now)
                return token
            return _refresh(sb, config, claimed, transport=transport, now_fn=now_fn)
        sleep(_REFRESH_POLL_SECONDS)
        row = connection(sb)
        if not row or row.get("status") != "connected":
            raise BcDisconnected(row.get("last_error") if row else _MSG_NOT_CONNECTED)
        token = _fresh(row, now_fn())
        if token and token != stale_token:
            return token
        if now_fn() >= deadline:
            raise BcTransient(_MSG_REFRESH_WAIT)


def access_token(sb, config: BcConfig, *, transport=None, sleep=time.sleep, now=None) -> str:
    """A usable bearer token (D27). Raises BcDisconnected when there is no
    connection or the refresh was rejected, BcTransient when the token
    endpoint is down or the lock wait timed out."""
    return _obtain(sb, config, transport=transport, sleep=sleep, now_fn=now or _now)


# ── The API client ───────────────────────────────────────────────────────────


class BcClient:
    """One httpx client over the Bid Board API. `transport`, `sleep` and
    `now` are test seams; `renew` runs between pages (the queue lease)."""

    def __init__(
        self,
        sb,
        config: BcConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        renew: Callable[[], None] | None = None,
    ) -> None:
        self._sb = sb
        self.config = config
        self._transport = transport
        self._sleep = sleep
        self._now = now or _now
        self._renew = renew
        self._client = _client(config, transport)
        # True after an `iter_opportunities` pull stopped at `max_pages`
        # before the board's last page: the caller must treat that pull as
        # an incomplete view (rfp_bc_portal.scan never marks unseen rows
        # missing or moves its high water mark on one).
        self.truncated = False
        self._touched = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BcClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- token ----------------------------------------------------------------

    def _token(self, *, stale: str | None = None) -> str:
        return _obtain(
            self._sb, self.config, transport=self._transport, sleep=self._sleep, now_fn=self._now, stale_token=stale
        )

    def _touch(self) -> None:
        """`last_used_at`, once per client instance, best effort."""
        if self._touched:
            return
        self._touched = True
        try:
            self._sb.table(CONNECTIONS_TABLE).update({"last_used_at": _iso(self._now())}).eq(
                "provider", PROVIDER
            ).execute()
        except Exception:  # noqa: BLE001 - bookkeeping only
            logger.debug("bc client: last_used_at touch failed", exc_info=True)

    # -- requests -------------------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET `path` under BC_BASE. 401 -> refresh once -> retry once (a
        second 401 disconnects the row and rings the disconnected bell);
        429 -> sleep Retry-After (cap 120 s) up to max_retries; 5xx or
        transport -> two retries with backoff; other 4xx -> BcPermanent."""
        path = "/" + path.lstrip("/")
        url = f"{BC_BASE}{path}"
        token = self._token()
        refreshed = False
        rate_limited = 0
        server_errors = 0
        while True:
            started = time.monotonic()
            try:
                response = self._client.get(url, params=params, headers=_api_headers(token, self.config))
            except httpx.HTTPError as exc:
                _log("GET", path, "transport-error", started)
                server_errors += 1
                if server_errors > _SERVER_ERROR_RETRIES:
                    raise BcTransient(f"{_MSG_TRANSPORT} ({path}): {type(exc).__name__}") from exc
                self._sleep(_SERVER_ERROR_BACKOFF_SECONDS[min(server_errors, len(_SERVER_ERROR_BACKOFF_SECONDS)) - 1])
                continue
            status = response.status_code
            _log("GET", path, status, started)
            if 200 <= status < 300:
                self._touch()
                return _json(response)
            if status == 401:
                if refreshed:
                    # A freshly issued token the Bid Board still refuses: the
                    # connected user lost Bid Board access, or the app its
                    # grant, while Autodesk auth still honours the refresh
                    # token. Left alone this would rotate the refresh token
                    # on every tick and park the run forever with the row
                    # still "connected" (no bell anywhere), so it is treated
                    # exactly like a rejected refresh: disconnect + bell.
                    detail = f"{_MSG_UNAUTHORIZED} after a refresh ({path}): {_detail(response)}"
                    disconnect(self._sb, error=_MSG_UNAUTHORIZED_AFTER_REFRESH)
                    _notify_disconnected(self._sb, detail, message=_BELL_UNAUTHORIZED_AFTER_REFRESH)
                    raise BcDisconnected(detail, status=401)
                refreshed = True
                token = self._token(stale=token)
                continue
            if status == 429:
                rate_limited += 1
                wait = _retry_after(response)
                if rate_limited > self.config.max_retries:
                    raise BcRateLimited(_MSG_RATE_LIMITED, retry_after=wait)
                self._sleep(wait)
                continue
            if status >= 500:
                server_errors += 1
                if server_errors > _SERVER_ERROR_RETRIES:
                    raise BcTransient(f"BuildingConnected answered {status} ({path}): {_detail(response)}", status=status)
                self._sleep(_SERVER_ERROR_BACKOFF_SECONDS[min(server_errors, len(_SERVER_ERROR_BACKOFF_SECONDS)) - 1])
                continue
            raise BcPermanent(f"BuildingConnected answered {status} ({path}): {_detail(response)}", status=status)

    def iter_opportunities(
        self,
        *,
        updated_since: datetime | None = None,
        limit: int = PAGE_LIMIT_MAX,
        max_pages: int | None = None,
    ) -> Iterator[dict]:
        """Every opportunity, paging on `pagination.cursorState` until it is
        absent. `renew()` runs before each page after the first. Keys with a
        leading underscore (fixture-only) are dropped. A pull stopped by
        `max_pages` with a cursor still pending sets `self.truncated`
        (reset at the start of every pull) so the caller knows its view of
        the board is incomplete."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit or PAGE_LIMIT_MAX), PAGE_LIMIT_MAX))}
        if updated_since is not None:
            params["filter[updatedAt]"] = updated_filter(updated_since)
        pages = 0
        cursor: str | None = None
        self.truncated = False
        while True:
            if max_pages is not None and pages >= max_pages:
                self.truncated = True
                logger.warning(
                    "bc client: page cap %s reached on /opportunities with more pages pending; "
                    "the pull is incomplete", max_pages,
                )
                return
            if pages and self._renew is not None:
                self._renew()
            page_params = dict(params)
            if cursor:
                page_params["cursorState"] = cursor
            body = self.get("/opportunities", page_params)
            pages += 1
            for item in body.get("results") or []:
                if isinstance(item, dict):
                    yield {k: v for k, v in item.items() if not str(k).startswith("_")}
            pagination = body.get("pagination")
            cursor = pagination.get("cursorState") if isinstance(pagination, dict) else None
            if not cursor:
                return

    def opportunity(self, opportunity_id) -> dict:
        body = self.get(f"/opportunities/{str(opportunity_id).strip()}")
        return {k: v for k, v in body.items() if not str(k).startswith("_")}

    def me(self) -> dict:
        return self.get("/users/me")
