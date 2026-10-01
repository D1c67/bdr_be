"""The per-mailbox ownership rule (docs/RFP_EMAIL_VISIBILITY.md sections 1 and
3.2), tested on the helper itself so the router tests can stay about routing.

What is pinned:

- The one unscoped view is a DEV account WEARING the IT Admin role. A dev in
  any other role, a plain IT Admin, the Executive and the Estimating Admin are
  all scoped like anybody else - deliberately, so nobody reads into the
  executives' mailboxes and a dev reproducing a bug sees what the person they
  are imitating sees.
- The scope is the viewer's own mapped mailboxes UNION the shared list, and
  nothing else.
- None (no limit) and the empty set (no access) are opposites; neither is
  allowed to collapse into the other.
- A row outside the scope raises the same 404 body an unknown id gets.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.services import rfp_email_visibility as vis

SHARED = "bids@g3electrical.com"
TOM = "tmoore@g3electrical.com"
TIESHA = "tiesha@g3electrical.com"


@pytest.fixture(autouse=True)
def shared_list(monkeypatch):
    """The default shared list, read through the settings property."""
    monkeypatch.setattr(
        vis,
        "get_settings",
        lambda: SimpleNamespace(rfp_email_ingestion_shared_mailbox_set={SHARED}),
    )


def _user(role=Role.ESTIMATING_ADMIN, *, is_dev=False, mailboxes=()):
    return CurrentUser(
        id="u1",
        email="someone@g3electrical.com",
        role=role,
        is_active=True,
        is_dev=is_dev,
        rfp_mailboxes=tuple(mailboxes),
    )


def _row(*mailboxes):
    return {"id": "e1", "mailboxes": list(mailboxes)}


# ── Who sees everything ──────────────────────────────────────────────────


def test_a_dev_wearing_it_admin_is_the_only_unscoped_view():
    dev_it = _user(Role.IT_ADMIN, is_dev=True)
    assert vis.sees_everything(dev_it) is True
    assert vis.visible_mailboxes(dev_it) is None
    assert vis.row_visible(_row("someone.elses@g3electrical.com"), dev_it) is True
    # Nothing raises, whatever the row holds - including a row with no
    # mailboxes at all (a pre-0134 row the backfill could not reach).
    vis.assert_visible(_row(), dev_it)


def test_a_dev_in_another_role_is_scoped_like_that_role():
    """The point of "view as": a dev reproducing an engineer's problem must
    see exactly what the engineer sees."""
    dev_engineer = _user(
        Role.ESTIMATING_ENGINEER_LABOR, is_dev=True, mailboxes=(TOM,)
    )
    assert vis.sees_everything(dev_engineer) is False
    assert vis.visible_mailboxes(dev_engineer) == {TOM, SHARED}
    assert vis.row_visible(_row(TIESHA), dev_engineer) is False


def test_a_plain_it_admin_is_scoped():
    """The flag and the role are both required; neither alone opens the door."""
    it_admin = _user(Role.IT_ADMIN)
    assert vis.sees_everything(it_admin) is False
    assert vis.visible_mailboxes(it_admin) == {SHARED}


# ── Everybody else ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [
        Role.EXECUTIVE,
        Role.ESTIMATING_ADMIN,
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
    ],
)
def test_every_other_internal_role_sees_its_own_mailbox_plus_the_shared_one(role):
    user = _user(role, mailboxes=(TOM,))
    assert vis.visible_mailboxes(user) == {TOM, SHARED}
    assert vis.row_visible(_row(TOM), user) is True
    assert vis.row_visible(_row(SHARED), user) is True
    # One message sighted in two mailboxes is one row, and either owner sees it.
    assert vis.row_visible(_row(TIESHA, TOM), user) is True
    assert vis.row_visible(_row(TIESHA), user) is False


def test_the_estimating_admin_cannot_read_into_an_executives_mailbox():
    """The rule the owner asked for by name: the Estimating Admin is scoped
    too, so the executives' mail stays with the executives."""
    admin = _user(Role.ESTIMATING_ADMIN)
    assert vis.row_visible(_row(TOM), admin) is False


def test_the_accountant_sees_the_shared_mailbox_only_until_one_is_mapped():
    accountant = _user(Role.ACCOUNTANT)
    assert vis.visible_mailboxes(accountant) == {SHARED}
    assert vis.row_visible(_row(SHARED), accountant) is True
    assert vis.row_visible(_row(TOM), accountant) is False
    # A mailbox mapped onto them widens it by exactly that mailbox.
    mapped = _user(Role.ACCOUNTANT, mailboxes=(TIESHA,))
    assert vis.visible_mailboxes(mapped) == {TIESHA, SHARED}


def test_mapped_mailboxes_are_normalized_before_they_are_compared():
    user = _user(Role.EXECUTIVE, mailboxes=("  TMoore@G3Electrical.com  ", ""))
    assert vis.visible_mailboxes(user) == {TOM, SHARED}
    assert vis.row_visible({"mailboxes": ["TMoore@G3Electrical.COM"]}, user) is True


# ── The empty scope and the refusal ──────────────────────────────────────


def test_an_empty_scope_sees_nothing_and_is_not_the_same_as_no_limit(monkeypatch):
    monkeypatch.setattr(
        vis, "get_settings", lambda: SimpleNamespace(rfp_email_ingestion_shared_mailbox_set=set())
    )
    nobody = _user(Role.EXECUTIVE)
    scope = vis.visible_mailboxes(nobody)
    assert scope == set() and scope is not None
    assert vis.row_visible(_row(SHARED), nobody) is False
    assert vis.row_visible(_row(), nobody) is False


def test_a_row_with_no_mailboxes_is_invisible_to_a_scoped_viewer():
    """Fail closed: a row the trigger never stamped belongs to nobody, so only
    the dev IT Admin can reach it and fix it."""
    assert vis.row_visible(_row(), _user(Role.EXECUTIVE)) is False


def test_the_refusal_is_the_unknown_id_404_byte_for_byte():
    from app.routers import rfp_emails as rr

    with pytest.raises(HTTPException) as exc:
        vis.assert_visible(_row(TIESHA), _user(Role.EXECUTIVE))
    assert exc.value.status_code == 404
    assert exc.value.detail == vis.EMAIL_NOT_FOUND == rr._EMAIL_NOT_FOUND


def test_apply_scope_sends_one_sorted_overlap_and_nothing_for_the_dev():
    calls = []

    class _Q:
        def overlaps(self, col, vals):
            calls.append((col, vals))
            return self

    query = _Q()
    assert vis.apply_scope(query, _user(Role.IT_ADMIN, is_dev=True)) is query
    assert calls == []
    vis.apply_scope(query, _user(Role.EXECUTIVE, mailboxes=(TOM,)))
    assert calls == [("mailboxes", sorted({TOM, SHARED}))]


# ── The array literal is built by joining values raw ─────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "a,tiesha@g3electrical.com",
        'a"b@g3electrical.com',
        "a'b@g3electrical.com",
        "a{b@g3electrical.com",
        "a}b@g3electrical.com",
        "a\\b@g3electrical.com",
        "a b@g3electrical.com",
    ],
)
def test_apply_scope_drops_a_value_that_would_corrupt_the_literal(bad):
    """AdminUpdateUserIn refuses these, but a profile row written by hand or
    before that validator existed must not be able to widen the query: the
    client joins the values into `ov.{a,b}` raw, so a comma inside one value
    becomes a separator."""
    calls = []

    class _Q:
        def overlaps(self, col, vals):
            calls.append(vals)
            return self

    user = _user(Role.EXECUTIVE, mailboxes=(bad, TOM))
    vis.apply_scope(_Q(), user)
    assert calls == [sorted({TOM, SHARED})]   # the bad value never reaches PostgREST


def test_a_dropped_value_is_not_a_way_past_the_row_check_either():
    """Narrowing the query must not be compensated for by a looser row test."""
    user = _user(Role.EXECUTIVE, mailboxes=("a,tiesha@g3electrical.com",))
    assert vis.row_visible({"mailboxes": ["a,tiesha@g3electrical.com"]}, user) is False
    assert vis.row_visible({"mailboxes": [TIESHA]}, user) is False
    with pytest.raises(HTTPException):
        vis.assert_visible({"mailboxes": [TIESHA]}, user)


def test_an_ordinary_address_is_untouched():
    assert vis._literal_safe(TOM) is True
    assert vis._literal_safe("first.last+tag@g3electrical.com") is True
