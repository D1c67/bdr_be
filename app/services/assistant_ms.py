"""Assistant access through the existing BDR application Graph identity.

No per-user Microsoft connection, OAuth flow, token cache or credential
registration. The BDR owner and delegation remain the authority boundary.
These helpers do not execute a mail operation or enable mail tools.
"""

from __future__ import annotations

from functools import wraps
from types import SimpleNamespace

from app.assistant import access, mail_scope, repo
from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, Role
from app.services import graph_email


class OutlookError(ValueError):
    def __init__(self, code="outlook_unavailable"):
        self.code = code
        super().__init__("Outlook is unavailable.")


def _sanitized(operation):
    """Never expose provider responses, credentials or database payloads."""
    @wraps(operation)
    def wrapped(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except OutlookError:
            raise
        except Exception:
            raise OutlookError() from None
    return wrapped


def configured(settings=None) -> bool:
    try:
        s = settings if settings is not None else get_settings()
        return bool(s.assistant_enabled and s.assistant_outlook_enabled
                    and all(isinstance(value, str) and value.strip() for value in (
                        s.ms_tenant_id, s.ms_client_id, s.ms_client_secret)))
    except (AttributeError, TypeError, ValueError):
        return False


def _eligible(user_id: str) -> dict:
    if not configured():
        raise OutlookError()
    check = access.owner_check(user_id)
    if check.problem or not check.human:
        raise OutlookError("outlook_owner_changed")
    if (check.human.get("is_active") is not True
            or Role(check.human.get("role")) not in INTERNAL_ROLES):
        raise OutlookError("outlook_owner_changed")
    return check.human


def _status_profile(user_id: str) -> dict:
    profile = repo.human_profile(user_id)
    if (not profile or profile.get("is_active") is not True
            or not access.assistant_enabled_for(
                SimpleNamespace(role=profile.get("role"), is_active=True),
                repo.settings_row(user_id), repo.profile_row(user_id), env=True)):
        raise OutlookError("outlook_owner_changed")
    return profile


def _token() -> str:
    # This existing provider owns the application's client-credentials cache.
    # Its exceptions can contain provider fields, so public callers sanitize.
    token = graph_email._acquire_token()
    if (not isinstance(token, str) or not token
            or any(ord(char) <= 32 or ord(char) >= 127 for char in token)):
        raise OutlookError()
    return token


@_sanitized
def status(user_id: str) -> dict:
    """Metadata only, available before the first BDR delegation is created."""
    result = {"available": configured(), "authentication": "application",
              "mailbox": None, "shared_mailboxes": []}
    if not result["available"]:
        return result
    profile = _status_profile(user_id)
    result["mailbox"] = mail_scope.normalize_address(profile.get("email"))
    shared = mail_scope.application_allow_set({**profile, "email": None, "rfp_mailboxes": []},
                                             get_settings())
    result["shared_mailboxes"] = sorted(shared)
    return result


@_sanitized
def application_token(user_id: str) -> str:
    """Require live BDR authority before acquiring the existing app token.

    Mailbox adapters should use for_mailbox so target authorization cannot be
    accidentally skipped. This function alone grants no mailbox selection.
    """
    _eligible(user_id)
    return _token()


@_sanitized
def for_mailbox(user_id: str, mailbox: str) -> tuple[str, str]:
    """Authorize a finite target before acquiring any Microsoft credential."""
    profile = _eligible(user_id)
    safe = mail_scope.require_application_mailbox(mailbox, profile, get_settings())
    return safe, _token()


@_sanitized
def sender_mailbox(user_id: str) -> str:
    """Only the live primary profile email can be the assistant's sender.

    Alias mappings and shared mailboxes are read targets, not sender choices.
    Future send dispatch must revalidate this immediately before its claim.
    """
    profile = _eligible(user_id)
    primary = mail_scope.normalize_address(profile.get("email"))
    if primary is None:
        raise OutlookError()
    return primary
