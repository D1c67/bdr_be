"""Pure finite mailbox policy for the assistant's existing application identity.

App-only Graph reach is broader than any individual assistant's authority.
Every mailbox operation must use this owner mapping before token acquisition.
This policy is separate from ingested-email visibility and Reader access.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.core.roles import INTERNAL_ROLES, Role

MAILBOX_UNAVAILABLE = "Mailbox unavailable."
_LOCAL = re.compile(r"[a-zA-Z0-9_+.-]+", re.ASCII)
_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", re.ASCII)


class MailScopeError(ValueError):
    """Uniform refusal without mailbox existence or identity information."""

    def __init__(self) -> None:
        super().__init__(MAILBOX_UNAVAILABLE)


def _get(record: Any, key: str, default: Any = None) -> Any:
    return record.get(key, default) if isinstance(record, Mapping) else getattr(record, key, default)


def normalize_address(value: Any) -> str | None:
    """A normalized literal corporate address, or None for unsafe input.

    Display names, quoted local parts, Unicode, controls, URL escapes and
    Graph path/query delimiters are refused. The accepted subset supports
    ordinary Microsoft UPNs and plus-addressing without accepting arbitrary
    RFC mailbox syntax as a Graph resource identifier.
    """
    if not isinstance(value, str) or not value.isascii():
        return None
    # Surrounding spaces are harmless; tabs and newlines are never accepted.
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    address = value.strip(" ")
    if len(address) > 254 or address.count("@") != 1:
        return None
    local, domain = address.split("@")
    if (not local or len(local) > 64 or not _LOCAL.fullmatch(local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels):
        return None
    return address.casefold()


def _addresses(values: Any) -> frozenset[str]:
    # A profile's mailbox array must not accidentally become a string list.
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(address for value in values if (address := normalize_address(value)))


def application_allow_set(profile: Any, settings: Any) -> frozenset[str]:
    """Bound app-only access to this active internal owner's mapped mailboxes.

    Executive and IT Admin have exactly the same finite scope as every other
    internal role. Invalid addresses are dropped, never passed into Graph
    resource paths. Exchange application permissions may further restrict it.
    """
    if _get(profile, "is_active", False) is not True:
        return frozenset()
    try:
        role = Role(_get(profile, "role"))
    except (ValueError, TypeError):
        return frozenset()
    if role not in INTERNAL_ROLES:
        return frozenset()
    own = set(_addresses(_get(profile, "rfp_mailboxes")))
    primary = normalize_address(_get(profile, "email"))
    if primary is not None:
        own.add(primary)
    configured = _get(settings, "shared_mailbox_set")
    if configured is None:
        raw = _get(settings, "shared_mailboxes", "")
        configured = raw.split(",") if isinstance(raw, str) else ()
    return frozenset(own) | _addresses(configured)


def require_application_mailbox(mailbox: Any, profile: Any, settings: Any) -> str:
    """Normalize an allowed mailbox, with one refusal for all other targets."""
    address = normalize_address(mailbox)
    if address is None or address not in application_allow_set(profile, settings):
        raise MailScopeError()
    return address
