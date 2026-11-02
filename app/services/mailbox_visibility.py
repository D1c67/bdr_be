"""Who may see one ingested_emails row (docs/ASSISTANT.md, "PM mailbox scoping
fix"). Pure: no IO, no Supabase client, so it can be unit-tested bare and the
sync SDK never runs here.

An ingested_emails row belongs to the ONE mailbox that received it
(ingested_emails.mailbox, lowercased at ingest). The rule, in full:

- Executive and IT Admin, dev account or not, see every mailbox except the
  exclusion list (MAILBOX_EXCLUSIONS), their own mailbox always included.
  Every detail or attachment open outside their own plus the shared set is
  audited by the caller as email.viewed_other_mailbox (is_other_mailbox says
  when). The owner decided this on purpose and employees were given notice.
- Every other internal role (the Estimating Admin, both engineer focuses and
  the Accountant) sees rows whose mailbox is in their own set, profiles.email
  plus profiles.rfp_mailboxes, plus the shared list (SHARED_MAILBOXES, by
  default bids@, office@ and adminassistant@).
- A dev account wearing any other role is scoped exactly like that role, as
  in 0134: a dev reproducing an engineer's problem sees what the engineer sees.
- The external estimator never reaches the router (require_internal).

A row outside the scope 404s with the SAME body an unknown id gets, so a
caller can never tell "not yours" from "does not exist".

This module scopes ingested_emails rows only. The assistant's delegated mail
tools do not call it: they carry their own allow-set, which is never
unfiltered, because the full view here must never reach a Graph call.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.core.roles import Role
from app.services.rfp_email_visibility import _literal_safe

# Byte-identical to the router's unknown-id refusal (routers/emails.py
# _fetch_email) on purpose: ids never leak through a differing body.
EMAIL_NOT_FOUND = "Email not found"

FULL_VIEW_ROLES = frozenset({Role.EXECUTIVE, Role.IT_ADMIN})
_OWN_MAILBOX_ROLES = frozenset(
    {
        Role.ESTIMATING_ADMIN,
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
        Role.ACCOUNTANT,
    }
)


@dataclass(frozen=True)
class Scope:
    #: None means every mailbox except `excluded` (full-view roles only). The
    #: empty frozenset means no access at all; the two must never collapse
    #: into one falsy check.
    allowed: frozenset[str] | None
    #: Always applied, full-view roles included.
    excluded: frozenset[str]
    #: Own plus shared: what a full-view role may read WITHOUT an audit row.
    own: frozenset[str]


def shared_mailboxes() -> set[str]:
    """Mailboxes every internal role may see into, lowercased."""
    return set(get_settings().shared_mailbox_set)


def excluded_mailboxes() -> set[str]:
    """Mailboxes nobody sees through PM Emails unless it is their own."""
    return set(get_settings().mailbox_exclusion_set)


def _own_mailboxes(user) -> set[str]:
    return {
        m.strip().lower()
        for m in (getattr(user, "email", None), *(getattr(user, "rfp_mailboxes", ()) or ()))
        if isinstance(m, str) and m.strip()
    }


def visible_mailboxes(user) -> Scope:
    """The viewer's scope. See the module docstring for the rule."""
    own: set[str] = set()
    if user.role in _OWN_MAILBOX_ROLES or user.role in FULL_VIEW_ROLES:
        own = _own_mailboxes(user)
    own_and_shared = frozenset(own | shared_mailboxes())
    # A person always sees their own mailbox, excluded or not.
    excluded = frozenset(m for m in excluded_mailboxes() if m not in own)
    if user.role in FULL_VIEW_ROLES:
        return Scope(allowed=None, excluded=excluded, own=own_and_shared)
    return Scope(
        allowed=frozenset(own_and_shared - excluded),
        excluded=excluded,
        own=own_and_shared,
    )


def apply_scope(query, user):
    """Narrow a PostgREST query on ingested_emails to what this viewer may see.

    Scoped roles get one sorted `in_` on the single-valued mailbox column;
    full-view roles get one `neq` per excluded address and nothing else. A
    value that would corrupt a literal is dropped rather than sent, which
    narrows instead of widening; `row_visible` never lets a dropped value make
    a row visible either. Callers answer the empty `allowed` set with zero
    rows and no query.
    """
    scope = visible_mailboxes(user)
    if scope.allowed is not None:
        return query.in_("mailbox", sorted(m for m in scope.allowed if _literal_safe(m)))
    for m in sorted(scope.excluded):
        if _literal_safe(m):
            query = query.neq("mailbox", m)
    return query


def row_visible(row, user) -> bool:
    """Whether this already-loaded row is inside the viewer's scope."""
    scope = visible_mailboxes(user)
    mb = (row.get("mailbox") or "").strip().lower()
    if not mb or not _literal_safe(mb) or mb in scope.excluded:
        return False
    return scope.allowed is None or mb in scope.allowed


def is_other_mailbox(row, user) -> bool:
    """True when a full-view role reads outside its own and shared set. The
    caller writes the mandatory email.viewed_other_mailbox audit row."""
    mb = (row.get("mailbox") or "").strip().lower()
    return user.role in FULL_VIEW_ROLES and mb not in visible_mailboxes(user).own


def assert_visible(row, user) -> None:
    """404 (the unknown-id refusal, byte for byte) when the row is not theirs."""
    if not row_visible(row, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, EMAIL_NOT_FOUND)
