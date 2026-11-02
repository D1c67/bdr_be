"""The PM mailbox ownership rule (docs/ASSISTANT.md, "PM mailbox scoping fix"),
tested on the pure helper itself so the router tests can stay about routing.
Mirrors tests/test_rfp_email_visibility.py.

What is pinned:

- Executive and IT Admin, dev or not, get allowed=None with the exclusion set
  applied; an excluded mailbox is invisible to an Executive unless it is their
  own; apply_scope emits one neq per excluded address for them and nothing
  when nothing is excluded.
- A dev wearing the Estimating Admin role is scoped like the Estimating Admin.
- Every other internal role sees exactly own email plus rfp_mailboxes plus the
  shared list, and cannot see an executive's mailbox. The Accountant is own
  plus shared (the recommended default, open decision).
- Values are normalized for case and whitespace; an unsafe value is dropped
  from the query and does not make a row visible; a row with a blank mailbox
  is invisible to a scoped viewer.
- The refusal body is byte-identical to the unknown-id 404.
- apply_scope sends one sorted in_ filter for scoped roles.
- is_other_mailbox is true only for a full-view role outside own plus shared.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.services import mailbox_visibility as vis
from app.services import rfp_email_visibility as rfp_vis

BIDS = "bids@g3electrical.com"
OFFICE = "office@g3electrical.com"
ADMIN_ASSISTANT = "adminassistant@g3electrical.com"
SHARED = {BIDS, OFFICE, ADMIN_ASSISTANT}
TOM = "tmoore@g3electrical.com"       # an executive's mailbox
TIESHA = "tiesha@g3electrical.com"    # an engineer's mailbox
HR = "hr@g3electrical.com"            # on the exclusion list


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    """Default shared list, nothing excluded. Tests that need an exclusion set
    call `exclude`."""
    state = SimpleNamespace(shared_mailbox_set=set(SHARED), mailbox_exclusion_set=set())
    monkeypatch.setattr(vis, "get_settings", lambda: state)
    return state


def _user(role=Role.ESTIMATING_ADMIN, *, email="someone@g3electrical.com", is_dev=False, mailboxes=()):
    return CurrentUser(
        id="u1",
        email=email,
        role=role,
        is_active=True,
        is_dev=is_dev,
        rfp_mailboxes=tuple(mailboxes),
    )


def _row(mailbox):
    return {"id": "e1", "mailbox": mailbox}


class _Query:
    """Records what apply_scope puts on the query."""

    def __init__(self):
        self.ins = []
        self.neqs = []

    def in_(self, col, vals):
        self.ins.append((col, list(vals)))
        return self

    def neq(self, col, val):
        self.neqs.append((col, val))
        return self


# ── Full view: Executive and IT Admin ────────────────────────────────────


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.IT_ADMIN])
@pytest.mark.parametrize("is_dev", [False, True])
def test_full_view_roles_see_every_mailbox_except_the_exclusions(settings, role, is_dev):
    settings.mailbox_exclusion_set = {HR}
    viewer = _user(role, email=TOM, is_dev=is_dev)
    scope = vis.visible_mailboxes(viewer)
    assert scope.allowed is None
    assert scope.excluded == frozenset({HR})
    assert scope.own == frozenset({TOM, *SHARED})
    assert vis.row_visible(_row(TIESHA), viewer) is True
    assert vis.row_visible(_row(BIDS), viewer) is True
    assert vis.row_visible(_row(HR), viewer) is False
    with pytest.raises(HTTPException):
        vis.assert_visible(_row(HR), viewer)


def test_an_excluded_mailbox_stays_visible_to_its_own_executive(settings):
    settings.mailbox_exclusion_set = {TOM, HR}
    tom = _user(Role.EXECUTIVE, email=TOM)
    scope = vis.visible_mailboxes(tom)
    assert scope.excluded == frozenset({HR})
    assert vis.row_visible(_row(TOM), tom) is True
    assert vis.row_visible(_row(HR), tom) is False
    # Another executive still cannot see Tom's excluded mailbox.
    other = _user(Role.EXECUTIVE, email="other@g3electrical.com")
    assert vis.row_visible(_row(TOM), other) is False


def test_apply_scope_emits_one_neq_per_exclusion_and_no_in_for_full_view(settings):
    settings.mailbox_exclusion_set = {HR, "legal@g3electrical.com"}
    q = _Query()
    out = vis.apply_scope(q, _user(Role.IT_ADMIN, email=TOM))
    assert out is q
    assert q.ins == []
    assert q.neqs == [("mailbox", HR), ("mailbox", "legal@g3electrical.com")]


def test_apply_scope_leaves_the_query_alone_for_full_view_without_exclusions():
    q = _Query()
    vis.apply_scope(q, _user(Role.EXECUTIVE, email=TOM))
    assert q.ins == [] and q.neqs == []


# ── Scoped roles ─────────────────────────────────────────────────────────


def test_a_dev_wearing_estimating_admin_is_scoped_like_the_estimating_admin():
    dev_admin = _user(Role.ESTIMATING_ADMIN, email=TIESHA, is_dev=True)
    scope = vis.visible_mailboxes(dev_admin)
    assert scope.allowed == frozenset({TIESHA, *SHARED})
    assert vis.row_visible(_row(TOM), dev_admin) is False


@pytest.mark.parametrize(
    "role",
    [
        Role.ESTIMATING_ADMIN,
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
        Role.ACCOUNTANT,
    ],
)
def test_other_internal_roles_see_exactly_own_email_plus_rfp_mailboxes_plus_shared(role):
    viewer = _user(role, email="login@g3electrical.com", mailboxes=(TIESHA,))
    scope = vis.visible_mailboxes(viewer)
    assert scope.allowed == frozenset({"login@g3electrical.com", TIESHA, *SHARED})
    assert scope.own == scope.allowed
    assert vis.row_visible(_row(TIESHA), viewer) is True
    assert vis.row_visible(_row("login@g3electrical.com"), viewer) is True
    assert vis.row_visible(_row(OFFICE), viewer) is True
    assert vis.row_visible(_row(TOM), viewer) is False
    with pytest.raises(HTTPException):
        vis.assert_visible(_row(TOM), viewer)


def test_the_accountant_sees_own_plus_shared():
    """The recommended default (open decision): the Accountant's own mailbox
    is their own mail. Dropping ACCOUNTANT from _OWN_MAILBOX_ROLES is the one
    line that would make it shared only."""
    acct = _user(Role.ACCOUNTANT, email="books@g3electrical.com")
    assert vis.visible_mailboxes(acct).allowed == frozenset({"books@g3electrical.com", *SHARED})
    assert Role.ACCOUNTANT in vis._OWN_MAILBOX_ROLES


def test_an_exclusion_also_narrows_a_scoped_role(settings):
    settings.mailbox_exclusion_set = {BIDS}
    viewer = _user(Role.ESTIMATING_ENGINEER_LABOR, email=TIESHA)
    scope = vis.visible_mailboxes(viewer)
    assert BIDS not in scope.allowed
    assert vis.row_visible(_row(BIDS), viewer) is False


def test_empty_scope_is_the_empty_set_not_none(settings):
    settings.shared_mailbox_set = set()
    viewer = _user(Role.ESTIMATING_ADMIN, email="  ")
    scope = vis.visible_mailboxes(viewer)
    assert scope.allowed == frozenset()
    assert scope.allowed is not None


# ── Normalization and the literal guard ──────────────────────────────────


def test_values_are_normalized_for_case_and_whitespace(settings):
    settings.shared_mailbox_set = {BIDS}
    viewer = _user(Role.ESTIMATING_ADMIN, email="  Login@G3Electrical.com ", mailboxes=(" Tiesha@G3electrical.COM ",))
    assert vis.visible_mailboxes(viewer).allowed == frozenset({"login@g3electrical.com", TIESHA, BIDS})
    assert vis.row_visible(_row(" TIESHA@g3electrical.com "), viewer) is True


def test_an_unsafe_value_is_dropped_from_the_query_and_never_makes_a_row_visible(settings):
    settings.shared_mailbox_set = {BIDS}
    bad = 'x,"y@g3electrical.com'
    viewer = _user(Role.ESTIMATING_ADMIN, email=TIESHA, mailboxes=(bad,))
    q = _Query()
    vis.apply_scope(q, viewer)
    assert q.ins == [("mailbox", sorted([BIDS, TIESHA]))]
    assert vis.row_visible(_row(bad), viewer) is False
    # The same for a full-view role's exclusion list: an unsafe exclusion is
    # never sent, and the row carrying it is invisible regardless.
    settings.mailbox_exclusion_set = {bad}
    exec_q = _Query()
    vis.apply_scope(exec_q, _user(Role.EXECUTIVE, email=TOM))
    assert exec_q.neqs == []
    assert vis.row_visible(_row(bad), _user(Role.EXECUTIVE, email=TOM)) is False


@pytest.mark.parametrize("mailbox", ["", "   ", None])
def test_a_row_with_a_blank_mailbox_is_invisible(mailbox):
    scoped = _user(Role.ESTIMATING_ADMIN, email=TIESHA)
    assert vis.row_visible(_row(mailbox), scoped) is False
    assert vis.row_visible({"id": "e1"}, scoped) is False
    # And to a full-view role too: a blank mailbox belongs to nobody.
    assert vis.row_visible(_row(mailbox), _user(Role.EXECUTIVE, email=TOM)) is False


# ── The refusal and the query shape ──────────────────────────────────────


def test_refusal_body_is_byte_identical_to_the_unknown_id_404():
    viewer = _user(Role.ESTIMATING_ADMIN, email=TIESHA)
    with pytest.raises(HTTPException) as exc:
        vis.assert_visible(_row(TOM), viewer)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Email not found"
    assert vis.EMAIL_NOT_FOUND == "Email not found" == rfp_vis.EMAIL_NOT_FOUND


def test_apply_scope_sends_one_sorted_in_filter_for_scoped_roles():
    viewer = _user(Role.ESTIMATING_ENGINEER_MATERIALS, email="zed@g3electrical.com", mailboxes=(TIESHA,))
    q = _Query()
    out = vis.apply_scope(q, viewer)
    assert out is q
    assert q.neqs == []
    assert q.ins == [("mailbox", sorted({"zed@g3electrical.com", TIESHA, *SHARED}))]


# ── The audit decision ───────────────────────────────────────────────────


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.IT_ADMIN])
def test_is_other_mailbox_is_true_only_outside_own_plus_shared_for_full_view(role):
    viewer = _user(role, email=TOM, mailboxes=("alias@g3electrical.com",))
    assert vis.is_other_mailbox(_row(TIESHA), viewer) is True
    assert vis.is_other_mailbox(_row(TOM), viewer) is False
    assert vis.is_other_mailbox(_row("alias@g3electrical.com"), viewer) is False
    assert vis.is_other_mailbox(_row(BIDS), viewer) is False
    assert vis.is_other_mailbox(_row(" TIESHA@g3electrical.com "), viewer) is True


def test_is_other_mailbox_is_never_true_for_a_scoped_role():
    viewer = _user(Role.ESTIMATING_ADMIN, email=TIESHA)
    assert vis.is_other_mailbox(_row(TOM), viewer) is False
    assert vis.is_other_mailbox(_row(TIESHA), viewer) is False
