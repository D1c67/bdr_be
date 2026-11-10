"""Credentials v2: save and revoke by site and account label
(docs/ASSISTANT_BROWSER_V2_PLAN.md 7.2, 7.3, 7.5, 7.6).

The password arrives as a SecretStr from the human-only route
(PUT /assistant/browser-credentials/{site_key}, under a firewall-denied
prefix) and is never logged, echoed or put in an error. It is encrypted
with the unchanged vault envelope (AAD bound to owner, credential id,
version and purpose) and written by assistant_browser_credential_put_v2,
which takes a curated site's login origins and cookie domains from the
registry and an ad hoc site's from the one exact origin the user confirmed
on the card. Identity-provider origins are refused here and again in SQL.

Warnings never block a save: the org Procore account (7.6) is a warning
only. Two acknowledgements do block it: the personal-password statement,
and reusing a password already saved for another site (7.3 step 4).

Owner: WI-4. SQL: assistant_browser_credential_put_v2,
assistant_browser_credential_revoke_v2 (0192).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

from app.assistant import repo
from app.core.config import get_settings
from app.services import assistant_browser_sites as sites
from app.services.browser_agents.protocol import SITE_KEY_RE

logger = logging.getLogger(__name__)

ACCOUNT_LABEL_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")
DEFAULT_ACCOUNT_LABEL = "default"
USERNAME_MAX = 320
PASSWORD_MAX = 1024
ORG_ACCOUNT = "org_account"
ORG_ACCOUNT_TEXT = ("This is the company Procore account BDR uses for the daily RFI sync. "
                    "Saving it here can lock that account if logins fail.")
# Columns a person may see about their own account (no envelope, no fingerprint).
ACCOUNT_FIELDS = ("id", "site_key", "account_label", "username", "version", "is_default", "sso_required",
                  "login_origins", "status", "created_at", "updated_at")
_HTTPS_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(?::[0-9]{1,5})?$")

_MESSAGES = {
    "ineligible": "Personal browser sign-ins are not available for this account.",
    "invalid": "The sign-in fields are invalid.",
    "unknown_site": "This site is not available.",
    "origin_invalid": "Confirm the exact sign-in address of this site.",
    "identity_provider_refused": "Use this site's own username and password. Never your Microsoft 365 or Google password.",
    "version_conflict": "This sign-in changed. Reload and try again.",
    "reused_password_needs_ack": "You use this password for another site. A different password is safer.",
    "personal_password_needs_ack": "Confirm that this is your own personal password for this site.",
    "not_found": "This sign-in no longer exists.",
}


@dataclass(frozen=True)
class SaveResult:
    credential: dict[str, Any]          # metadata only: no username, no material
    woken_jobs: tuple[str, ...]         # jobs moved forward instead of cancelled (7.2)
    waiting_admissions: tuple[str, ...]  # admissions WI-5 continues
    warnings: tuple[str, ...]           # org_account (7.6); never blocks the save


class CredentialRefused(ValueError):
    """code: ineligible, invalid, unknown_site, origin_invalid,
    identity_provider_refused, version_conflict, reused_password_needs_ack,
    personal_password_needs_ack."""

    def __init__(self, code: str, message: str | None = None, *, site_label: str | None = None) -> None:
        super().__init__(message or _MESSAGES.get(code, _MESSAGES["invalid"]))
        self.code = code
        self.site_label = site_label


def _metadata(row: dict) -> dict[str, Any]:
    """What a save returns to the card: no username (the card stub and the
    model never see one), no envelope, no fingerprint."""
    out = {key: row.get(key) for key in ACCOUNT_FIELDS if key != "username"}
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    return out


def account_metadata(row: dict) -> dict[str, Any]:
    """One account as Settings shows it to its owner (username included)."""
    out = {key: row.get(key) for key in ACCOUNT_FIELDS}
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    out["login_origins"] = list(out.get("login_origins") or ())
    # 7.6: Settings shows the company Procore account warning too, not only
    # the save answer.
    out["warnings"] = [ORG_ACCOUNT] if org_account_warning(str(row.get("site_key") or ""), row.get("username")) else []
    return out


def _origin_in_site(site: dict, origin: str) -> bool:
    """An exact origin of the row, or a host equal to or under one of its
    registrable domains (the SQL twin is assistant_browser_v2_site_matches;
    the path prefix of a multi-tenant row does not apply to a login origin)."""
    host = origin.split("://", 1)[1].split(":", 1)[0]
    if origin in [str(o).lower() for o in site.get("exact_origins") or ()]:
        return True
    return any(host == d or host.endswith("." + d) for d in (str(x).lower() for x in site.get("registrable_domains") or ()))


def _confirmed_origin(site: dict, value: str | None) -> str:
    """The ad hoc login origin the user confirmed on the card: https, an
    exact origin (no path), on the approved site, never an identity provider."""
    if not isinstance(value, str) or not value:
        raise CredentialRefused("origin_invalid")
    try:
        parsed = sites.parse_url(value.strip().rstrip("/") + "/")
    except sites.UrlRefused:
        raise CredentialRefused("origin_invalid") from None
    if parsed.scheme != "https" or value.strip().rstrip("/").lower() != parsed.origin:
        raise CredentialRefused("origin_invalid")
    if sites.is_identity_provider(parsed.origin):
        raise CredentialRefused("identity_provider_refused")
    if not _HTTPS_ORIGIN.match(parsed.origin) or not _origin_in_site(site, parsed.origin):
        raise CredentialRefused("origin_invalid")
    return parsed.origin


def lookalike_of(site_key_or_domain: str) -> str | None:
    """The curated site label this one imitates (7.3 step 2), or None. A
    registry key reads the row's own domain and label; anything else is
    taken as a domain, host or URL."""
    if not isinstance(site_key_or_domain, str) or not site_key_or_domain.strip():
        return None
    value = site_key_or_domain.strip()
    if value.startswith("domain:"):
        return sites.lookalike_of(value.split(":", 1)[1])
    if value.startswith("origin:"):
        return sites.lookalike_of(value.split(":", 2)[1])
    if SITE_KEY_RE.match(value) and "." not in value:
        # A bare registry key such as "procore" names a curated site itself.
        return None
    return sites.lookalike_of(value)


def site_lookalike(site: dict | None) -> str | None:
    """lookalike_of for a registry row (ad hoc rows only: a curated row is
    the brand itself)."""
    if not site or site.get("kind") == "curated":
        return None
    for value in [*(site.get("registrable_domains") or ()), *(site.get("exact_origins") or ())]:
        found = sites.lookalike_of(str(value), label=site.get("label"))
        if found:
            return found
    return None


def reused_password_site(user_id: str, site_key: str, password: SecretStr) -> str | None:
    """Label of another site saved with the same password fingerprint, or None."""
    from app.services import assistant_browser_vault as vault

    secret = password.get_secret_value() if isinstance(password, SecretStr) else None
    if not secret:
        return None
    fingerprint = vault.password_fingerprint(user_id, secret)
    others = [r for r in repo.browser_credentials_v2_all(user_id)
              if isinstance(r, dict) and r.get("status") == "active" and r.get("fingerprint") == fingerprint
              and (r.get("site_key") or r.get("service")) != site_key]
    if not others:
        return None
    key = str(others[0].get("site_key") or others[0].get("service") or "")
    row = repo.browser_site(user_id, key) if key and SITE_KEY_RE.match(key) else None
    return str((row or {}).get("label") or key or "another site")


def org_account_warning(site_key: str, username: str) -> str | None:
    """The 7.6 warning when the username is PROCORE_LOGIN_EMAIL, else None."""
    org = str(getattr(get_settings(), "procore_login_email", "") or "").strip().lower()
    if site_key != "procore" or not org or not isinstance(username, str):
        return None
    return ORG_ACCOUNT_TEXT if username.strip().lower() == org else None


def save_credential(user_id: str, site_key: str, *, account_label: str, username: str, password: SecretStr,
                    expected_version: int, acknowledge_personal_password: bool,
                    acknowledge_reused_password: bool = False, confirmed_login_origin: str | None = None,
                    request_id: str | None = None) -> SaveResult:
    """Encrypt with the vault envelope and call credential_put_v2."""
    from app.services import assistant_browser_vault as vault

    if not isinstance(site_key, str) or not SITE_KEY_RE.match(site_key):
        raise CredentialRefused("invalid")
    label = (account_label or "").strip()
    user = (username or "").strip()
    secret = password.get_secret_value() if isinstance(password, SecretStr) else None
    if (not ACCOUNT_LABEL_RE.match(label) or not 1 <= len(user) <= USERNAME_MAX or not secret
            or len(secret) > PASSWORD_MAX or isinstance(expected_version, bool)
            or not isinstance(expected_version, int) or expected_version < 0):
        raise CredentialRefused("invalid")
    if acknowledge_personal_password is not True:
        raise CredentialRefused("personal_password_needs_ack")
    site = repo.browser_site(user_id, site_key)
    if not site or not site.get("enabled", True):
        raise CredentialRefused("unknown_site")
    if site.get("kind") == "curated":
        origins = [str(o) for o in site.get("login_origins") or ()]
        if not origins or any(sites.is_identity_provider(o) for o in origins):
            raise CredentialRefused("identity_provider_refused")
    else:
        _confirmed_origin(site, confirmed_login_origin)
    current = repo.browser_credential_v2(user_id, site_key, label)
    current_version = int(current.get("version") or 0) if current else 0
    if current_version != expected_version:
        raise CredentialRefused("version_conflict")
    other = reused_password_site(user_id, site_key, password)
    if other and acknowledge_reused_password is not True:
        raise CredentialRefused("reused_password_needs_ack",
                                f"You use this password for {other}. A different password is safer.",
                                site_label=other)
    credential_id = str(current["id"]) if current else str(uuid4())
    envelope = vault.encrypt_payload(user_id, credential_id, expected_version + 1, "credential", secret.encode())
    record = {"id": credential_id, "username": user, "fingerprint": vault.password_fingerprint(user_id, secret),
              **envelope}
    if site.get("kind") != "curated":
        record["confirmed_login_origin"] = _confirmed_origin(site, confirmed_login_origin)
    result = repo.browser_credential_put_v2(user_id, site_key, label, expected_version, record, request_id)
    if not result.get("ok") or not isinstance(result.get("credential"), dict):
        code = str(result.get("code") or "invalid")
        raise CredentialRefused(code if code in _MESSAGES else "invalid")
    warning = org_account_warning(site_key, user)
    return SaveResult(
        credential=_metadata(result["credential"]),
        woken_jobs=tuple(str(j) for j in result.get("woken_jobs") or () if j),
        waiting_admissions=tuple(str(a) for a in result.get("waiting_admissions") or () if a),
        warnings=(ORG_ACCOUNT,) if warning else (),
    )


def revoke_credential(user_id: str, credential_id: str, expected_version: int) -> int:
    """Revoke one account; returns the new version."""
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
        raise CredentialRefused("invalid")
    result = repo.browser_credential_revoke_v2(user_id, str(credential_id), expected_version)
    if not result.get("ok"):
        code = str(result.get("code") or "invalid")
        raise CredentialRefused(code if code in _MESSAGES else "invalid")
    return int(result.get("version") or 0)


def list_accounts(user_id: str) -> list[dict[str, Any]]:
    """Accounts per site for Settings (labels, username, version, default).
    Re-keyed rows only; active accounts first, then by site and label."""
    rows = [r for r in repo.browser_credentials_v2_all(user_id) if isinstance(r, dict) and r.get("site_key")]
    rows.sort(key=lambda r: (r.get("status") != "active", str(r.get("site_key")), not r.get("is_default"),
                             str(r.get("account_label") or "")))
    return [account_metadata(r) for r in rows]
