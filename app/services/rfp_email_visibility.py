"""Who may see one row of the RFP review queue (docs/RFP_EMAIL_VISIBILITY.md).

A message belongs to the MAILBOXES that received it, not to the addresses in
its To/CC headers: about a fifth of the queue arrives by BCC or through a
distribution list, so the headers name the wrong people. Migration 0134
denormalizes the sightings onto `rfp_emails.mailboxes` (a trigger keeps it in
step) so the whole rule is one array overlap, and maps the other side onto
`profiles.rfp_mailboxes`.

The rule, in full:

- A dev account WEARING the IT Admin role sees everything. That is the one
  unscoped view, and it is deliberately narrow: a dev switched into another
  role to reproduce something is scoped exactly like the person they are
  imitating, which is the only way the reproduction means anything.
- Everyone else sees a row whose mailboxes overlap their own mapped mailboxes
  or the shared list (`RFP_EMAIL_INGESTION_SHARED_MAILBOXES`, by default the
  team's bids@ address). Executives and the Estimating Admin included: the
  owner's words were "they are executives not micromanagers", and the
  Estimating Admin must not be able to read into an executive's mailbox.
- Nobody with an empty scope sees anything. That is reachable (an internal
  role with no mailbox mapped and an emptied shared list), so it short
  -circuits to zero rows rather than querying with an empty overlap, which
  PostgREST would answer as "overlaps nothing" anyway but which is clearer
  and one round trip cheaper.

A row outside the scope 404s with the SAME body an unknown id gets, so a
caller can never tell "not yours" from "does not exist" and ids never leak.

Kept out of the router so the ingest service and any later reader share one
definition of the rule, and so it can be unit-tested without a fake HTTP
stack.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.core.roles import Role

# The body every refusal carries. Identical to the router's `_EMAIL_NOT_FOUND`
# on purpose (see the module docstring); it lives here too so the helper can
# raise without importing the router.
EMAIL_NOT_FOUND = "Email not found"

# Characters that cannot appear in a value we hand to PostgREST as part of an
# array literal (`mailboxes=ov.{a,b}`): the client joins the values raw, so a
# comma, a quote, a brace or a backslash inside ONE value would be read as a
# separator and widen the query beyond what the viewer owns. Whitespace goes
# too, for the same reason and because no mailbox we own contains any.
# AdminUpdateUserIn refuses these on the way in; this is the second line of
# defence, for a profile row written before that validator existed or by hand.
_UNSAFE_IN_LITERAL = set(',"\'{}\\')


def _literal_safe(value: str) -> bool:
    return not (_UNSAFE_IN_LITERAL & set(value)) and not any(
        ch.isspace() for ch in value
    )


def shared_mailboxes() -> set[str]:
    """Mailboxes every internal role may see into, lowercased."""
    return set(get_settings().rfp_email_ingestion_shared_mailbox_set)


def sees_everything(user) -> bool:
    """The one unscoped view: a dev account wearing the IT Admin role."""
    return bool(getattr(user, "is_dev", False)) and user.role == Role.IT_ADMIN


def visible_mailboxes(user) -> set[str] | None:
    """The mailboxes this viewer may see, or None for "no limit at all".

    None and the empty set are opposites and both are real: None is the dev IT
    Admin, the empty set is someone with nothing mapped and no shared mailbox
    configured. Callers must not collapse them into one falsy check.
    """
    if sees_everything(user):
        return None
    own = {
        m.strip().lower()
        for m in (getattr(user, "rfp_mailboxes", ()) or ())
        if isinstance(m, str) and m.strip()
    }
    return own | shared_mailboxes()


def apply_scope(query, user):
    """Narrow a PostgREST query on rfp_emails to what this viewer may see.

    Returns the query unchanged for the dev IT Admin. The caller is expected to
    have checked `visible_mailboxes` for the empty set first and answered zero
    rows without a query: an empty overlap is not a filter anyone should send.
    """
    allowed = visible_mailboxes(user)
    if allowed is None:
        return query
    # Drop anything that would corrupt the array literal rather than sending
    # it: a value holding a comma would widen the scope, which is the one
    # failure mode this whole module exists to prevent. Dropping it narrows
    # instead, which is the safe direction, and `row_visible` keeps using the
    # unfiltered set so a dropped value can never make a row look visible.
    safe = sorted(m for m in allowed if _literal_safe(m))
    return query.overlaps("mailboxes", safe)


def row_visible(row, user) -> bool:
    """Whether this already-loaded row is inside the viewer's scope."""
    allowed = visible_mailboxes(user)
    if allowed is None:
        return True
    if not allowed:
        return False
    # Compared against the UNFILTERED scope: a value apply_scope had to drop
    # must not become a way past the row check either, and a row carrying one
    # is simply never matched by anything here.
    return any(
        isinstance(m, str) and _literal_safe(m.strip().lower())
        and m.strip().lower() in allowed
        for m in (row.get("mailboxes") or [])
    )


def assert_visible(row, user) -> None:
    """404 (the unknown-id refusal, byte for byte) when the row is not theirs."""
    if not row_visible(row, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, EMAIL_NOT_FOUND)
