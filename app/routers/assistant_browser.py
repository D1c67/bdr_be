"""Human-only personal browser routes (docs/ASSISTANT_BROWSER_V2_PLAN.md
4.3, 5.2, 5.3, 5.4, 6.3, 6.5, 7.3, 16.2).

Every route here answers only the person's own session: `_human` refuses an
assistant principal, and the firewall denies the prefixes
/assistant/browser-sites, /assistant/browser-credentials and
/assistant/browser-holds to the assistant as well. The model reaches the
browser only through /assistant/browser-requests (assistant_browser_requests,
WI-5), which creates admissions, request rows and server-built cards and
never grants, fills or approves anything. The buttons on those cards call
the routes below.

Credentials never enter a model tool, an assistant transcript, a log or a
validation error: the PUT body is parsed by hand with
hide_input_in_errors, and the password stays a SecretStr until the vault
encrypts it.

The Phase 6 task routes (POST /browser-tasks, /browser-tasks/preview and
/browser-tasks/by-approval/{id}) are retired and answer 410.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.assistant import repo
from app.core.config import get_settings
from app.core.deps import CurrentUser, require_writer
from app.routers.assistant import _refuse_assistant_principal, _require_assistant
from app.services import assistant_browser as browser
from app.services import assistant_browser_credentials as credentials
from app.services import assistant_browser_grants as grants
from app.services import assistant_browser_sites as sites
from app.services.browser_agents.protocol import SITE_KEY_RE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant-browser"])
NO_STORE = {"Cache-Control": "no-store"}
ACCOUNT_LABEL = r"^[A-Za-z0-9 _.-]{1,40}$"
RETIRED_TEXT = "This browser route is retired. Ask the assistant to start a browser task."
# Path segments generalized in an owner safe rule (6.3): numbers and tokens.
_NUMERIC_SEGMENT = re.compile(r"^[0-9]+$")
_TOKEN_SEGMENT = re.compile(r"^(?:[A-Za-z0-9_-]{20,}|[0-9a-fA-F-]{16,})$")
_UUID_SEGMENT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIGIT = re.compile(r"[0-9]")
FINAL_VERBS = re.compile(r"(submit|send|accept|declin|delet|remov|pay|approv|answer|respond|sign|award|confirm)",
                         re.IGNORECASE)
_GRAPHQL_OPERATION = re.compile(r"^[A-Za-z0-9_]{1,100}$")


# ── Gates ────────────────────────────────────────────────────────────────────


def _human(response: Response, user: CurrentUser = Depends(require_writer)) -> CurrentUser:
    response.headers["Cache-Control"] = "no-store"
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return user


def _enabled() -> None:
    if not getattr(get_settings(), "assistant_browser_enabled", False):
        raise HTTPException(404, "Not Found", headers=NO_STORE)


def _v2() -> bool:
    settings = get_settings()
    return bool(getattr(settings, "assistant_browser_enabled", False)
                and getattr(settings, "assistant_browser_v2_enabled", False))


def _enabled_v2() -> None:
    if not _v2():
        raise HTTPException(404, "Not Found", headers=NO_STORE)


def require_browser_v2(user: CurrentUser = Depends(require_writer)) -> CurrentUser:
    """Router-level gate app/main.py puts on the v2 routers it mounts
    (assistant_browser_requests, assistant_browser_results): writer roles,
    and a bare 404 while ASSISTANT_BROWSER_V2_ENABLED is off."""
    _enabled_v2()
    return user


def _refused(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    # `msg` is what the frontend's error parser shows for an object detail.
    detail: Any = {"code": code, "message": message, "msg": message, **extra} if extra else message
    return HTTPException(status, detail, headers={**NO_STORE, "X-Error-Code": code})


def _unavailable() -> HTTPException:
    return _refused(409, "unavailable", "Personal browser sign-ins are unavailable. Reload and try again.")


_GRANT_STATUS = {"invalid": 422, "unknown_site": 404, "not_found": 404, "ineligible": 403}
_CREDENTIAL_STATUS = {"invalid": 422, "origin_invalid": 422, "identity_provider_refused": 422,
                      "personal_password_needs_ack": 422, "unknown_site": 404, "not_found": 404,
                      "ineligible": 403}


def _past(value: Any) -> bool:
    """True when an ISO timestamp (or datetime) is at or before now; an
    unreadable value counts as past (fail closed)."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return True
    else:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp <= datetime.now(timezone.utc)


def _grant_refused(exc: grants.GrantRefused) -> HTTPException:
    return _refused(_GRANT_STATUS.get(exc.code, 409), exc.code, str(exc))


def _body(model: type[BaseModel], value: Any) -> Any:
    try:
        return model.model_validate(value)
    except ValidationError:
        # FastAPI's default 422 echoes raw input; these bodies can carry secrets.
        raise _refused(422, "invalid", "Browser request fields are invalid.") from None


def _uuid_text(value: Any) -> str | None:
    try:
        return str(UUID(str(value))) if value else None
    except (ValueError, TypeError):
        return None


def _continue(user_id: str, admission_ids: list[str] | tuple[str, ...]) -> list[dict]:
    """After a grant, a credential save, a link approval or a start click:
    WI-5's continue_admission enqueues each admission that needs nothing
    more, or posts its next card. A failure leaves the admission waiting
    (it expires on its own) and never undoes the human's decision."""
    out: list[dict] = []
    for admission_id in list(dict.fromkeys(str(a) for a in admission_ids or () if a))[:20]:
        try:
            result = browser.continue_admission(user_id, admission_id)
            out.append({"admission_id": admission_id, "status": (result or {}).get("status"),
                        "task_id": (result or {}).get("task_id")})
        except browser.AdmissionRefused as exc:
            # A cap (concurrency, day budget) refused the enqueue: the owner
            # sees why on the card; the admission stays ready and the next
            # browser_task_status retries it.
            out.append({"admission_id": admission_id, "status": "refused", "task_id": None,
                        "code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 - the decision stands; the admission stays waiting
            logger.warning("browser admission %s did not continue (%s)", admission_id, type(exc).__name__)
            out.append({"admission_id": admission_id, "status": "waiting", "task_id": None})
    return out


def _decide_hold(user_id: str, hold_id: str, decision: str) -> dict:
    """assistant_browser_holds.decide (WI-6): signs the release token for a
    final action and runs the CAS RPC. Its ValueError carries a code."""
    from app.services import assistant_browser_holds as holds

    return holds.decide(user_id, hold_id, decision) or {}


def _hold_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", None) or str(exc)
    if code not in {"hold_aborted", "hold_expired", "hold_changed", "not_found", "invalid", "forbidden"}:
        code = "hold_changed"
    status = {"not_found": 404, "invalid": 422, "forbidden": 403}.get(code, 409)
    message = {"hold_aborted": "The task stopped before you answered.",
               "hold_expired": "This request expired. Ask the task to try again.",
               "not_found": "This request no longer exists.", "invalid": "This decision is not allowed.",
               "forbidden": "Not permitted."}.get(code, "This request changed. Reload and try again.")
    return _refused(status, code, message)


# ── Site registry, grants, sessions, safe rules (Settings and site cards) ────


def _site_summary(row: dict) -> dict:
    return {"site_key": row.get("site_key"), "label": row.get("label"), "kind": row.get("kind"),
            "login_kind": row.get("login_kind"), "registrable_domains": list(row.get("registrable_domains") or ()),
            "exact_origins": list(row.get("exact_origins") or ()), "path_prefix": row.get("path_prefix"),
            "prefer_api": row.get("prefer_api"), "enabled": row.get("enabled", True)}


@router.get("/browser-sites")
def browser_sites(user: CurrentUser = Depends(_human)):
    """The curated registry and the ad hoc sites this person approved."""
    if not _v2():
        return {"enabled": False, "items": []}
    granted = {str(g.get("site_key")) for g in repo.browser_site_grants(user.id, status="active")}
    rows = [r for r in repo.browser_sites(user.id) if r.get("kind") == "curated" or str(r.get("site_key")) in granted]
    return {"enabled": True, "items": [_site_summary(r) for r in rows]}


def _session_out(row: dict) -> dict:
    return {"id": str(row.get("id")), "site_key": row.get("site_key"), "account_label": row.get("account_label"),
            "source": "live_signin" if row.get("source") == "live_signin" else "login",
            "saved_at": row.get("created_at"), "expires_at": row.get("expires_at")}


def _safe_rule_out(row: dict) -> dict:
    return {"id": str(row.get("id")), "site_key": row.get("site_key"), "method": row.get("method"),
            "host": row.get("host"), "path_pattern": row.get("path_pattern"),
            "graphql_operation": row.get("graphql_operation"), "created_at": row.get("created_at")}


@router.get("/browser-sites/signins")
def browser_signins(user: CurrentUser = Depends(_human)):
    """Settings > Browser sites and sign-ins (16.2) in one read."""
    _enabled_v2()
    active = [g for g in grants.list_grants(user.id) if g.get("status") == "active"]
    return {"grants": active, "accounts": credentials.list_accounts(user.id),
            "sessions": [_session_out(r) for r in repo.browser_sessions_v2(user.id)],
            "safe_rules": [_safe_rule_out(r) for r in repo.browser_safe_rules(user.id)]}


@router.get("/browser-sites/grants")
def browser_grants(user: CurrentUser = Depends(_human)):
    _enabled_v2()
    # Approved sites only (the frontend BrowserGrant has no status field);
    # a revoked row is history, not an approval.
    return {"items": [g for g in grants.list_grants(user.id) if g.get("status") == "active"]}


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    request_id: UUID | None = None
    site_key: str | None = Field(default=None, pattern=r"^[a-z0-9_.:-]{2,80}$")
    scope: Literal["domain", "origin"] | None = None
    # Ignored when request_id is set (the request row names the site); a
    # card may send an empty string.
    value: str | None = Field(default=None, max_length=300)
    path_prefix: str | None = Field(default=None, max_length=512)
    site_only: bool = False


def _open_site_hold(user_id: str, job_id: str, site_key: str | None) -> dict | None:
    rows = repo.browser_holds(user_id, job_id, status="open")
    for row in rows:
        if row.get("kind") == "site" and (site_key is None or row.get("target_site_key") in (None, site_key)):
            return row
    return None


def _grant_from_request(user: CurrentUser, request_id: str, site_only: bool) -> dict:
    """The site access card's Approve: the request row (written by the
    server when it posted the card) names the site; the body cannot widen
    it. A mid-task request (5.4) also extends the job's scope and releases
    the held navigation."""
    row = repo.browser_request(user.id, request_id)
    if not row or row.get("kind") != "site_access":
        raise _refused(404, "not_found", "This request no longer exists.")
    if row.get("status") != "requested" or _past(row.get("expires_at")):
        raise _refused(409, "request_changed", "This request changed. Reload and try again.")
    provenance = row.get("provenance") if row.get("provenance") in grants.PROVENANCES else "unknown"
    site, target = None, None
    if row.get("site_key"):
        site = repo.browser_site(user.id, str(row["site_key"]))
    # The card's URL, or for a `domain:` key written without one, that
    # domain's root: either way the server computes the scope.
    probe = str(row["url"]) if row.get("url") else None
    if probe is None and str(row.get("site_key") or "").startswith("domain:"):
        probe = "https://" + str(row["site_key"]).split(":", 1)[1] + "/"
    if site is None and probe:
        try:
            site, target = sites.resolve_site(user.id, probe)
        except sites.UrlRefused:
            raise _refused(422, "invalid", "This site cannot be approved.") from None
    if site is not None:
        try:
            target = sites.grant_target_for_site(site)
        except ValueError:
            raise _refused(422, "invalid", "This site cannot be approved.") from None
        result = grants.grant_site(user.id, str(site["site_key"]), scope=target.scope, value=target.value,
                                   path_prefix=target.path_prefix, granted_via="chat_card", provenance=provenance,
                                   request_id=request_id)
    elif target is not None:
        result = grants.grant_site(user.id, target.site_key, scope=target.scope, value=target.value,
                                   path_prefix=target.path_prefix, granted_via="chat_card", provenance=provenance,
                                   request_id=request_id, ad_hoc_label=target.label)
    else:
        raise _refused(404, "unknown_site", "This site is not known.")
    site_key = str(result["grant"].get("site_key"))
    job_id = row.get("job_id")
    if job_id:
        result["job"] = _release_site_hold(user.id, str(job_id), site_key, site_only)
    return result


def _release_site_hold(user_id: str, job_id: str, site_key: str, site_only: bool) -> dict:
    try:
        version = grants.extend_job_scope(user_id, job_id, site_key, allow_multi_tenant=True)
    except grants.GrantRefused as exc:
        return {"job_id": job_id, "status": exc.code}
    hold = _open_site_hold(user_id, job_id, site_key)
    if hold is None:
        return {"job_id": job_id, "status": "scope_extended", "policy_version": version}
    try:
        decided = _decide_hold(user_id, str(hold["id"]), "approve_site_only" if site_only else "approve")
    except Exception as exc:  # noqa: BLE001 - the grant stands; the hold card shows its own state
        logger.warning("browser site hold %s not released (%s)", hold.get("id"), type(exc).__name__)
        return {"job_id": job_id, "status": "scope_extended", "policy_version": version}
    return {"job_id": job_id, "status": (decided.get("hold") or decided).get("status", "approved"),
            "policy_version": version}


@router.post("/browser-sites/grants")
def create_grant(body: Any = Body(None), user: CurrentUser = Depends(_human)):
    """Approve a site once (D4). Only this human route grants; the card's
    button calls it with the request id, Settings without one."""
    _enabled_v2()
    payload: GrantIn = _body(GrantIn, body)
    try:
        if payload.request_id is not None:
            result = _grant_from_request(user, str(payload.request_id), payload.site_only)
        else:
            if not payload.site_key or not payload.scope or not payload.value:
                raise _refused(422, "invalid", "Choose the site to approve.")
            result = grants.grant_site(user.id, payload.site_key, scope=payload.scope, value=payload.value,
                                       path_prefix=payload.path_prefix, granted_via="settings",
                                       provenance="settings")
    except grants.GrantRefused as exc:
        raise _grant_refused(exc) from None
    result["continued"] = _continue(user.id, result.get("waiting_admissions") or ())
    return result


@router.delete("/browser-sites/grants/{grant_id}")
def revoke_grant(grant_id: UUID, expected_version: int = Query(ge=1), user: CurrentUser = Depends(_human)):
    """Revoke in one transaction: sessions for the site removed, queued tasks
    cancelled, running tasks fenced (5.2)."""
    _enabled_v2()
    try:
        return grants.revoke_grant(user.id, str(grant_id), expected_version)
    except grants.GrantRefused as exc:
        raise _grant_refused(exc) from None


@router.get("/browser-sites/sessions")
def browser_sessions(user: CurrentUser = Depends(_human)):
    _enabled_v2()
    return {"items": [_session_out(r) for r in repo.browser_sessions_v2(user.id)]}


@router.delete("/browser-sites/sessions/{session_id}", status_code=204)
def sign_out_session(session_id: UUID, user: CurrentUser = Depends(_human)):
    _enabled_v2()
    result = repo.browser_session_signout(user.id, str(session_id))
    if not result.get("ok"):
        raise _refused(404, "not_found", "This sign-in session no longer exists.")
    return Response(status_code=204, headers=NO_STORE)


@router.get("/browser-sites/safe-rules")
def browser_safe_rules(user: CurrentUser = Depends(_human)):
    _enabled_v2()
    return {"items": [_safe_rule_out(r) for r in repo.browser_safe_rules(user.id)]}


@router.delete("/browser-sites/safe-rules/{rule_id}", status_code=204)
def remove_safe_rule(rule_id: UUID, user: CurrentUser = Depends(_human)):
    _enabled_v2()
    result = repo.browser_safe_rule_remove(user.id, str(rule_id))
    if not result.get("ok"):
        raise _refused(404, "not_found", "This safe action no longer exists.")
    return Response(status_code=204, headers=NO_STORE)


# ── Card requests (site access, credential, email link, task start) ──────────


def _url_facts(url: str | None) -> dict:
    facts: dict[str, Any] = {"registrable_domain": None, "not_encrypted": False, "carries_data": False}
    if not url:
        return facts
    try:
        parsed = sites.parse_url(url)
    except sites.UrlRefused:
        return facts
    facts["registrable_domain"] = parsed.registrable_domain
    facts["not_encrypted"] = parsed.scheme != "https"
    facts["carries_data"] = len(urlsplit(parsed.url).query) > 64
    return facts


def _sender_domain(user_id: str, url: str | None) -> str | None:
    if not url:
        return None
    try:
        for row in repo.browser_url_email_sources(user_id, [url]):
            if row.get("sender_domain"):
                return str(row["sender_domain"])
    except Exception:  # noqa: BLE001 - the card still warns that the link is untrusted
        return None
    return None


def _email_source_domain(user_id: str, url: str | None, conversation_id: str | None) -> str | None:
    """The sender domain for an email link card (5.5): a link the model read
    through the mail tools (provenance_detail, mail_tool), else the RFP,
    portal and PM email rows."""
    if not url:
        return None
    try:
        found = sites.provenance_detail(user_id, None, str(url),
                                        conversation_id=str(conversation_id) if conversation_id else None)
        if found.kind == "email" and found.sender_domain:
            return str(found.sender_domain)
    except Exception as exc:  # noqa: BLE001 - the card still warns that the link is untrusted
        logger.info("browser link card: provenance read failed (%s)", type(exc).__name__)
    return _sender_domain(user_id, url)


def request_view(user_id: str, row: dict) -> dict:
    """Server-rendered card facts for one request row (16.1). Every label,
    origin and warning comes from the registry and the server's checks,
    never from the model or the page."""
    site = repo.browser_site(user_id, str(row["site_key"])) if row.get("site_key") else None
    url = row.get("url")
    target = None
    if site is None and url:
        try:
            site, target = sites.resolve_site(user_id, str(url))
        except sites.UrlRefused:
            site, target = None, None
    label = (site or {}).get("label") or (target.label if target else None)
    ad_hoc = (site or {}).get("kind") != "curated" if site else target is not None
    if site is not None:
        lookalike = credentials.site_lookalike(site)
    elif target is not None:
        lookalike = sites.lookalike_of(str(url)) or sites.lookalike_of(target.value, label=target.label)
    else:
        lookalike = None
    if url and lookalike is None and ad_hoc:
        lookalike = sites.lookalike_of(str(url))
    login_origins = list((site or {}).get("login_origins") or ())
    current_version = 0
    if row.get("kind") == "credential" and site is not None:
        account = repo.browser_credential_v2(user_id, str(site["site_key"]), row.get("account_label"))
        current_version = int((account or {}).get("version") or 0)
    admission = repo.browser_admission(user_id, str(row["admission_id"])) if row.get("admission_id") else None
    facts = _url_facts(url or (admission or {}).get("start_url"))
    provenance = row.get("provenance")
    return {
        "id": str(row.get("id")), "kind": row.get("kind"), "status": row.get("status"),
        "site_key": (site or {}).get("site_key") or row.get("site_key") or (target.site_key if target else None),
        "site_label": label, "registrable_domain": facts["registrable_domain"],
        "url": url or (admission or {}).get("start_url"), "provenance": provenance,
        "source_domain": _email_source_domain(user_id, url, row.get("conversation_id")) if provenance == "email" else None,
        "reason_code": row.get("reason_code"), "account_label": row.get("account_label"),
        "login_origins": login_origins, "ad_hoc": bool(ad_hoc), "lookalike_of": lookalike,
        "carries_data": facts["carries_data"], "not_encrypted": facts["not_encrypted"],
        "current_version": current_version,
        "goal": (admission or {}).get("goal") if row.get("kind") in ("task_start", "link_open") else None,
        "project_id": str(admission["project_id"]) if admission and admission.get("project_id") else None,
        "project_label": None,
        "max_cost_usd": float(admission["max_cost_usd"]) if admission and admission.get("max_cost_usd") is not None else None,
        "tainted": bool((admission or {}).get("tainted")),
        "expires_at": row.get("expires_at"),
    }


@router.get("/browser-sites/requests/{request_id}")
def browser_request(request_id: UUID, user: CurrentUser = Depends(_human)):
    _enabled_v2()
    row = repo.browser_request(user.id, str(request_id))
    if not row:
        raise _refused(404, "not_found", "This request no longer exists.")
    return request_view(user.id, row)


class RequestDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "decline", "cancel"]


@router.post("/browser-sites/requests/{request_id}/decision")
def decide_request(request_id: UUID, body: Any = Body(None), user: CurrentUser = Depends(_human)):
    """Decline or cancel any card; approve an email link card (5.5). A site
    is approved through the grant route, a credential through the PUT."""
    _enabled_v2()
    payload: RequestDecisionIn = _body(RequestDecisionIn, body)
    result = repo.browser_request_decide(user.id, str(request_id), payload.decision)
    if not result.get("ok"):
        code = str(result.get("code") or "request_changed")
        status = {"invalid": 422}.get(code, 409)
        raise _refused(status, code, "This request changed. Reload and try again."
                       if code != "invalid" else "This decision is not allowed.")
    row = result.get("request") or {}
    out: dict[str, Any] = {"status": row.get("status"), "continued": []}
    if payload.decision == "approve" and row.get("admission_id"):
        out["continued"] = _continue(user.id, [str(row["admission_id"])])
    if payload.decision != "approve" and row.get("kind") == "site_access" and row.get("job_id"):
        # Declining a mid-task site card aborts the held navigation (5.4 step 3).
        hold = _open_site_hold(user.id, str(row["job_id"]), row.get("site_key"))
        if hold is not None:
            try:
                _decide_hold(user.id, str(hold["id"]), "decline")
            except Exception as exc:  # noqa: BLE001 - the park limit ends the hold anyway
                logger.warning("browser site hold %s not declined (%s)", hold.get("id"), type(exc).__name__)
    return out


# ── Credentials v2 (7.2, 7.3) ────────────────────────────────────────────────


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    account_label: str = Field(default="default", pattern=ACCOUNT_LABEL)
    username: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)
    expected_version: int = Field(default=0, ge=0)
    acknowledge_personal_password: bool
    acknowledge_reused_password: bool = False
    confirmed_login_origin: str | None = Field(default=None, max_length=300)
    request_id: str | None = Field(default=None, max_length=36)


def _credential_refused(exc: credentials.CredentialRefused) -> HTTPException:
    extra = {"site_label": exc.site_label} if exc.site_label else {}
    return _refused(_CREDENTIAL_STATUS.get(exc.code, 409), exc.code, str(exc), **extra)


@router.get("/browser-credentials")
def browser_credentials(user: CurrentUser = Depends(_human)):
    """The person's saved accounts per site (no passwords, no envelopes)."""
    _enabled_v2()
    return {"items": credentials.list_accounts(user.id)}


@router.put("/browser-credentials/{site_key}")
def put_credential(site_key: str, body: Any = Body(None), user: CurrentUser = Depends(_human)):
    """The credential card's save (7.3 step 3). Only this human route ever
    receives a password; the chat model and its transcript never do."""
    _enabled_v2()
    if not SITE_KEY_RE.match(site_key or ""):
        raise _refused(404, "unknown_site", "This site is not available.")
    try:
        payload = CredentialIn.model_validate(body)
    except ValidationError:
        # FastAPI's default validation includes raw input, including passwords.
        raise _refused(422, "invalid", "Credential fields are invalid.") from None
    request_id = None
    if payload.request_id is not None:
        try:
            request_id = str(UUID(payload.request_id))
        except ValueError:
            raise _refused(422, "invalid", "Credential fields are invalid.") from None
        # The card's request must name this site and account: a stale or
        # mis-wired card never marks another request saved.
        row = repo.browser_request(user.id, request_id)
        if (not row or row.get("kind") != "credential" or row.get("site_key") != site_key
                or (row.get("account_label") or payload.account_label) != payload.account_label):
            raise _refused(409, "request_changed", "This request changed. Reload and try again.")
    from app.services import assistant_browser_vault as vault

    try:
        saved = credentials.save_credential(
            user.id, site_key, account_label=payload.account_label, username=payload.username,
            password=payload.password, expected_version=payload.expected_version,
            acknowledge_personal_password=payload.acknowledge_personal_password,
            acknowledge_reused_password=payload.acknowledge_reused_password,
            confirmed_login_origin=payload.confirmed_login_origin, request_id=request_id)
    except credentials.CredentialRefused as exc:
        raise _credential_refused(exc) from None
    except vault.VaultUnavailable:
        raise _unavailable() from None
    except Exception as exc:  # noqa: BLE001 - never echo anything about the body
        logger.warning("browser credential save failed (%s)", type(exc).__name__)
        raise _unavailable() from None
    return {"credential": saved.credential, "warnings": list(saved.warnings),
            "woken_jobs": list(saved.woken_jobs), "waiting_admissions": list(saved.waiting_admissions),
            "continued": _continue(user.id, saved.waiting_admissions)}


@router.delete("/browser-credentials/{site_key}", status_code=204)
def delete_credential(site_key: str, expected_version: int = Query(ge=1),
                      account_label: str | None = Query(default=None, pattern=ACCOUNT_LABEL),
                      user: CurrentUser = Depends(_human)):
    """Remove one account (its sessions go with it; tasks using it stop)."""
    _enabled_v2()
    if not SITE_KEY_RE.match(site_key or ""):
        raise _refused(404, "not_found", "This sign-in no longer exists.")
    row = repo.browser_credential_v2(user.id, site_key, account_label)
    if not row or row.get("status") != "active":
        raise _refused(404, "not_found", "This sign-in no longer exists.")
    try:
        credentials.revoke_credential(user.id, str(row["id"]), expected_version)
    except credentials.CredentialRefused as exc:
        raise _credential_refused(exc) from None
    return Response(status_code=204, headers=NO_STORE)


# ── Holds and the start card (6.5, 4.3) ──────────────────────────────────────


def _hold_summary(user_id: str, hold: dict) -> dict:
    """The encrypted hold summary WI-6 stores (UserHoldRequest fields: url,
    body_summary, element_label, profile_label, model_description). An
    unreadable record gives empty fields; the card still shows method and
    origin."""
    record_id = hold.get("summary_record_id")
    if not record_id:
        return {}
    from app.services import assistant_browser_records as records

    try:
        row = repo.browser_record(user_id, str(hold["job_id"]), str(record_id))
        if not row:
            return {}
        content = records.unpack_record(user_id, str(hold["job_id"]), row)
    except Exception as exc:  # noqa: BLE001 - the card degrades to method and origin
        logger.warning("browser hold %s summary unreadable (%s)", hold.get("id"), type(exc).__name__)
        return {}
    inner = content.get("hold") if isinstance(content.get("hold"), dict) else content
    return inner if isinstance(inner, dict) else {}


def _hold_fields(summary: dict) -> list[dict]:
    out = []
    for field in summary.get("body_summary") or summary.get("fields") or ():
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            continue
        shown = field.get("hidden") is False and isinstance(field.get("value"), str)
        out.append({"name": field["name"][:200], "value": field["value"][:500] if shown else None})
    return out[:50]


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


@router.get("/browser-holds/{hold_id}")
def browser_hold(hold_id: UUID, user: CurrentUser = Depends(_human)):
    """One hold's card facts: the full URL only here, to its owner."""
    _enabled_v2()
    hold = repo.browser_hold(user.id, str(hold_id))
    if not hold:
        raise _refused(404, "not_found", "This request no longer exists.")
    summary = _hold_summary(user.id, hold)
    url = summary.get("url") if isinstance(summary.get("url"), str) else None
    facts = _url_facts(url)
    site = repo.browser_site(user.id, str(hold["target_site_key"])) if hold.get("target_site_key") else None
    return {"id": str(hold["id"]), "job_id": str(hold.get("job_id")), "kind": hold.get("kind"),
            "status": hold.get("status"), "method": hold.get("method"), "url": url or hold.get("origin"),
            "origin": hold.get("origin"), "registrable_domain": facts["registrable_domain"]
            or sites.registrable_domain(str(hold.get("origin") or "")),
            "out_of_scope": bool(hold.get("out_of_scope")),
            "carries_data": bool(summary.get("carries_data")) or facts["carries_data"],
            "label": _text(summary.get("profile_label"), 120) or _text(hold.get("label"), 200)
            or _text(summary.get("element_label"), 200),
            "description": _text(summary.get("model_description"), 300),
            "fields": _hold_fields(summary),
            "screenshot_record_id": str(hold["screenshot_record_id"]) if hold.get("screenshot_record_id") else None,
            "expires_at": hold.get("expires_at"), "site_label": (site or {}).get("label"),
            # A site hold for a site never approved carries its site access
            # request (5.4 step 2); the card approves it through the grant route.
            "request_id": _uuid_text(summary.get("request_id")) if hold.get("kind") == "site" else None}


class HoldDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "approve_site_only", "decline"]


@router.post("/browser-holds/{hold_id}/decision")
def decide_hold(hold_id: UUID, body: Any = Body(None), user: CurrentUser = Depends(_human)):
    """Approve or decline a held request (6.5 step 3). Approving a final
    action signs a single-use release token; a site hold is released only
    for a site this person already approved (the site card's own Approve
    goes through the grant route)."""
    _enabled_v2()
    payload: HoldDecisionIn = _body(HoldDecisionIn, body)
    hold = repo.browser_hold(user.id, str(hold_id))
    if not hold:
        raise _refused(404, "not_found", "This request no longer exists.")
    if payload.decision != "decline" and hold.get("kind") not in ("final_action", "site"):
        raise _refused(422, "invalid", "This decision is not allowed.")
    if hold.get("kind") == "site" and payload.decision != "decline":
        site_key = hold.get("target_site_key")
        if not site_key or grants.active_grant(user.id, str(site_key)) is None:
            raise _refused(409, "no_grant", "Approve the site on its card first.")
        try:
            grants.extend_job_scope(user.id, str(hold["job_id"]), str(site_key), allow_multi_tenant=True)
        except grants.GrantRefused as exc:
            raise _grant_refused(exc) from None
    elif payload.decision == "approve_site_only":
        raise _refused(422, "invalid", "This decision is not allowed.")
    try:
        result = _decide_hold(user.id, str(hold_id), payload.decision)
    except NotImplementedError:
        raise _unavailable() from None
    except ValueError as exc:
        raise _hold_error(exc) from None
    decided = result.get("hold") if isinstance(result.get("hold"), dict) else result
    return {"status": decided.get("status") or ("declined" if payload.decision == "decline" else "approved")}


def safe_path_pattern(path: str) -> str:
    """The owner's safe rule pattern (6.3): the exact path with numeric and
    token segments generalized, anchored. `/api/v1/projects/123/filters`
    gives `^/api/v1/projects/[0-9]+/filters$`. A long segment is a token only
    when it holds a digit, so a word such as `bulk_filter_preferences` stays
    exact and the rule never also matches a sibling like `replies`."""
    parts = []
    for segment in (path or "/").split("/"):
        if _NUMERIC_SEGMENT.match(segment):
            parts.append("[0-9]+")
        elif _UUID_SEGMENT.match(segment) or (_TOKEN_SEGMENT.match(segment) and _DIGIT.search(segment)):
            parts.append("[A-Za-z0-9_-]+")
        else:
            parts.append(re.escape(segment))
    return "^" + "/".join(parts) + "$"


@router.post("/browser-holds/{hold_id}/safe-rule")
def mark_hold_safe(hold_id: UUID, user: CurrentUser = Depends(_human)):
    """"This was not a final action" (6.3): a rule for this owner only,
    derived by the server from the held request, refused when the path or
    GraphQL operation names a final-action verb. It does not release the
    held request; the card's Approve does."""
    _enabled_v2()
    hold = repo.browser_hold(user.id, str(hold_id))
    if not hold:
        raise _refused(404, "not_found", "This request no longer exists.")
    if hold.get("kind") != "final_action" or hold.get("out_of_scope"):
        raise _refused(422, "invalid", "This request cannot be marked safe.")
    summary = _hold_summary(user.id, hold)
    url = summary.get("url") if isinstance(summary.get("url"), str) else None
    try:
        parsed = sites.parse_url(url) if url else None
    except sites.UrlRefused:
        parsed = None
    if parsed is None or parsed.origin != hold.get("origin"):
        raise _unavailable()
    operation = summary.get("graphql_operation")
    operation = operation if isinstance(operation, str) and _GRAPHQL_OPERATION.match(operation) else None
    pattern = safe_path_pattern(urlsplit(parsed.url).path or "/")
    if len(pattern) > 300 or FINAL_VERBS.search(urlsplit(parsed.url).path or "") or (
            operation and FINAL_VERBS.search(operation)):
        raise _refused(422, "final_verb", "This looks like a final action, so it cannot be marked safe.")
    host = parsed.host + (f":{parsed.port}" if parsed.port else "")
    result = repo.browser_safe_rule_add(user.id, str(hold_id), method=str(hold.get("method")), host=host,
                                        path_pattern=pattern, graphql_operation=operation)
    if not result.get("ok"):
        code = str(result.get("code") or "invalid")
        raise _refused(422, code if code in {"invalid", "final_verb", "out_of_scope"} else "invalid",
                       "This request cannot be marked safe.")
    rule = result.get("rule")
    return _safe_rule_out(rule) if isinstance(rule, dict) else None


class StartDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["start", "decline"]


@router.post("/browser-holds/start/{admission_id}/decision")
def decide_start(admission_id: UUID, body: Any = Body(None), user: CurrentUser = Depends(_human)):
    """The start card (4.3) and the email link card's admission: only the
    owner's click admits a task started from a conversation that holds
    outside content or from a turn without a fresh user message."""
    _enabled_v2()
    payload: StartDecisionIn = _body(StartDecisionIn, body)
    result = repo.browser_start_decide(user.id, str(admission_id), payload.decision)
    if not result.get("ok"):
        code = str(result.get("code") or "admission_changed")
        raise _refused(422 if code == "invalid" else 409, code, "This task changed. Reload and try again.")
    admission = result.get("admission") or {}
    out: dict[str, Any] = {"status": admission.get("status"), "needs": list(result.get("needs") or ()),
                           "continued": []}
    if payload.decision == "start":
        out["continued"] = _continue(user.id, [str(admission_id)])
    return out


# ── Task metadata (owner), Phase 6 routes retired ────────────────────────────


@router.get("/browser-tasks")
def browser_tasks(user: CurrentUser = Depends(_human)):
    _enabled()
    return browser.tasks(user.id)


def _reader(response: Response, user: CurrentUser = Depends(require_writer)) -> CurrentUser:
    response.headers["Cache-Control"] = "no-store"
    _require_assistant(user)
    if user.via_assistant_run_id:
        cid = repo.conversation_for_run(user.id, user.via_assistant_run_id)
        conversation = repo.get_conversation(user.id, cid) if cid else None
        if not conversation or conversation.get("mode") != "normal" or conversation.get("mailbox_tainted_at"):
            raise HTTPException(404, "Not Found")
    return user


def _retired() -> HTTPException:
    return _refused(410, "retired", RETIRED_TEXT)


@router.post("/browser-tasks/preview")
def preview_browser_task(user: CurrentUser = Depends(_reader)):
    """Retired (Phase 6 per-task approval card): 410."""
    raise _retired()


@router.get("/browser-tasks/by-approval/{approval_id}")
def recover_browser_task(approval_id: UUID, user: CurrentUser = Depends(_reader)):
    """Retired (Phase 6 per-task approval card): 410."""
    raise _retired()


@router.get("/browser-tasks/{job_id}")
def browser_task(job_id: UUID, user: CurrentUser = Depends(_reader)):
    _enabled()
    result = browser.task(user.id, str(job_id))
    if result is None:
        raise HTTPException(404, "Not Found", headers=NO_STORE)
    return result


@router.post("/browser-tasks")
def create_browser_task(user: CurrentUser = Depends(_reader)):
    """Retired (Phase 6 per-task approval card): 410. Tasks start through
    POST /assistant/browser-requests/tasks."""
    raise _retired()
