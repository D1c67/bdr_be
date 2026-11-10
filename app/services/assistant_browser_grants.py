"""Per-user standing site approvals (docs/ASSISTANT_BROWSER_V2_PLAN.md 5.2, 5.4).

Grants are created only by the human route (POST
/assistant/browser-sites/grants, under the denied prefix); the model has no
route that grants. Every database call goes through app.assistant.repo:
assistant_browser_site_grant, assistant_browser_site_revoke and
assistant_browser_scope_extend (0192).

What a grant covers is fixed here, never by the caller: a registered site
is granted on its own registry scope; a new site gets the ad hoc scope of
assistant_browser_sites.grant_target_for_url (registrable domain, or exact
origin plus path prefix on a multi-tenant domain, or exact origin under a
private-section suffix), and the ad hoc key must be the one that scope
produces.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from app.assistant import repo
from app.services import assistant_browser_sites as sites
from app.services.browser_agents.protocol import ORIGIN_RE, SITE_KEY_RE

GrantScope = Literal["domain", "origin"]
GrantedVia = Literal["chat_card", "settings"]
Provenance = Literal["typed", "bdr_record", "search", "email", "page", "unknown", "settings"]

GRANTED_VIA = ("chat_card", "settings")
PROVENANCES = ("typed", "bdr_record", "search", "email", "page", "unknown", "settings")
# Stable refusal codes (the RPC's plus the ones checked here first).
CODES = ("ineligible", "invalid", "unknown_site", "site_disabled", "not_found", "version_conflict",
         "job_changed", "no_grant", "multi_tenant")
GRANT_FIELDS = ("id", "site_key", "scope", "value", "path_prefix", "version", "status", "granted_at",
                "granted_via", "provenance", "revoked_at")
_LABEL_CLEAN = re.compile(r"[\x00-\x1f\x7f]")


class GrantRefused(ValueError):
    """A grant, revoke or scope change refused. `code` is one of CODES;
    the message is fixed text, never the caller's input."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or _MESSAGES.get(code, "The site approval could not be changed."))
        self.code = code


_MESSAGES = {
    "ineligible": "The browser is not available for this account.",
    "invalid": "This site cannot be approved.",
    "unknown_site": "This site is not known.",
    "site_disabled": "This site is turned off.",
    "not_found": "This site approval no longer exists.",
    "version_conflict": "This site approval changed. Reload and try again.",
    "job_changed": "The task is no longer running.",
    "no_grant": "This site is not approved.",
    "multi_tenant": "This site needs its own approval for this task.",
}


def _refusal(result: dict) -> GrantRefused:
    code = str(result.get("code") or "invalid")
    return GrantRefused(code if code in CODES else "invalid")


def grant_metadata(row: dict, site: dict | None = None) -> dict[str, Any]:
    """A grant as Settings and browser_sites_list show it (no usernames,
    no request internals)."""
    out = {key: row.get(key) for key in GRANT_FIELDS}
    out["id"] = str(row["id"]) if row.get("id") is not None else None
    if site is not None:
        out.update(label=site.get("label"), kind=site.get("kind"), enabled=site.get("enabled", True),
                   login_kind=site.get("login_kind"))
    return out


def _target_for(user_id: str, site_key: str, scope: GrantScope, value: str,
                path_prefix: str | None) -> tuple[dict | None, sites.GrantTarget]:
    """The registry row (or None) and the grant scope a request may take."""
    if not isinstance(site_key, str) or not SITE_KEY_RE.match(site_key):
        raise GrantRefused("invalid")
    row = repo.browser_site(user_id, site_key)
    if row is not None:
        if not row.get("enabled", True):
            raise GrantRefused("site_disabled")
        try:
            return row, sites.grant_target_for_site(row)
        except ValueError:
            raise GrantRefused("invalid") from None
    if not site_key.startswith(("domain:", "origin:")):
        raise GrantRefused("unknown_site")
    if scope not in ("domain", "origin") or not isinstance(value, str) or not value:
        raise GrantRefused("invalid")
    probe = value if scope == "origin" else f"https://{value}/"
    if scope == "origin":
        if not ORIGIN_RE.match(value.lower()):
            raise GrantRefused("invalid")
        probe = value.lower() + (path_prefix if path_prefix and path_prefix.startswith("/") else "/")
    try:
        target = sites.grant_target_for_url(probe)
        existing = sites.site_for_url(user_id, probe)
    except sites.UrlRefused:
        raise GrantRefused("invalid") from None
    if existing is not None and existing.get("kind") == "curated":
        # A registered curated site is never shadowed by an ad hoc row.
        raise GrantRefused("invalid")
    if (target.site_key != site_key or target.scope != scope or target.value != value.lower()
            or (target.path_prefix or None) != (path_prefix or None)):
        raise GrantRefused("invalid")
    return None, target


def grant_site(user_id: str, site_key: str, *, scope: GrantScope, value: str, path_prefix: str | None,
               granted_via: GrantedVia, provenance: Provenance, request_id: str | None = None,
               ad_hoc_label: str | None = None) -> dict[str, Any]:
    """Create (or return) the user's active grant; inserts an ad hoc registry
    row for an unknown `domain:` or `origin:` site. Returns
    {"grant": {...}, "waiting_admissions": [admission ids]}. Raises
    GrantRefused (a ValueError) with a stable code (ineligible, invalid,
    unknown_site, site_disabled).

    For a registered site the scope, value and path prefix come from the
    registry row (the caller's are ignored). For a new site they must be
    exactly what grant_target_for_url gives for that scope, so a card can
    never widen a multi-tenant or private-suffix approval to a domain."""
    if granted_via not in GRANTED_VIA or provenance not in PROVENANCES:
        raise GrantRefused("invalid")
    row, target = _target_for(user_id, site_key, scope, value, path_prefix)
    ad_hoc = None
    if row is None:
        label = _LABEL_CLEAN.sub("", (ad_hoc_label or target.label or "").strip())[:100] or target.label
        ad_hoc = {"label": label}
    result = repo.browser_site_grant_put(
        user_id, target.site_key, scope=target.scope, value=target.value, path_prefix=target.path_prefix,
        granted_via=granted_via, provenance=provenance, request_id=request_id, ad_hoc=ad_hoc,
    )
    if not result.get("ok") or not isinstance(result.get("grant"), dict):
        raise _refusal(result)
    site = row or repo.browser_site(user_id, target.site_key)
    waiting = [str(a) for a in result.get("waiting_admissions") or () if a]
    return {"grant": grant_metadata(result["grant"], site), "waiting_admissions": waiting}


def revoke_grant(user_id: str, grant_id: str, expected_version: int) -> dict[str, Any]:
    """Revoke in one transaction (sessions removed, queued jobs cancelled,
    running jobs fenced). Returns {"version", "canceled_jobs", "sessions_removed"}.
    Raises GrantRefused (not_found, version_conflict)."""
    try:
        expected = int(expected_version)
    except (TypeError, ValueError):
        raise GrantRefused("invalid") from None
    result = repo.browser_site_revoke(user_id, str(grant_id), expected)
    if not result.get("ok"):
        raise _refusal(result)
    return {"version": int(result.get("version") or 0), "canceled_jobs": int(result.get("canceled_jobs") or 0),
            "sessions_removed": int(result.get("sessions_removed") or 0)}


def list_grants(user_id: str) -> list[dict[str, Any]]:
    """The user's grants for Settings and browser_sites_list (no usernames).
    Active first, then the most recent revoked ones."""
    rows = repo.browser_site_grants(user_id, status=None, limit=200)
    registry = repo.browser_sites_by_keys(user_id, sorted({str(r.get("site_key")) for r in rows}))
    out = [grant_metadata(r, registry.get(str(r.get("site_key")))) for r in rows]
    return sorted(out, key=lambda g: (g.get("status") != "active",))


def active_grant(user_id: str, site_key: str) -> dict[str, Any] | None:
    """The active grant for one site, or None."""
    if not isinstance(site_key, str) or not SITE_KEY_RE.match(site_key):
        return None
    row = repo.browser_site_grant(user_id, site_key)
    return grant_metadata(row) if row else None


def active_grant_for_url(user_id: str, url: str) -> tuple[dict | None, dict[str, Any] | None]:
    """(registry row or None, the user's active grant for it or None)."""
    site = sites.site_for_url(user_id, url)
    if site is None:
        return None, None
    return site, active_grant(user_id, str(site["site_key"]))


def extend_job_scope(user_id: str, job_id: str, site_key: str, *, allow_multi_tenant: bool = False) -> int:
    """Add an already granted site to a running job's scope (5.4 step 2).
    Returns the job's new policy_version. A multi-tenant site is never added
    automatically (raises GrantRefused("multi_tenant")): the caller posts a
    site access card, and its approval passes `allow_multi_tenant=True`."""
    site = None
    if isinstance(site_key, str) and SITE_KEY_RE.match(site_key):
        site = repo.browser_site(user_id, site_key)
    if site is None:
        raise GrantRefused("unknown_site")
    if not site.get("enabled", True):
        raise GrantRefused("site_disabled")
    if not allow_multi_tenant and _site_is_multi_tenant(site):
        raise GrantRefused("multi_tenant")
    result = repo.browser_scope_extend(user_id, str(job_id), site_key)
    if not result.get("ok"):
        raise _refusal(result)
    return int(result.get("policy_version") or 0)


def _site_is_multi_tenant(site: dict) -> bool:
    values = [*(site.get("registrable_domains") or ()), *(site.get("exact_origins") or ())]
    return any(sites.is_multi_tenant(str(v)) for v in values)
