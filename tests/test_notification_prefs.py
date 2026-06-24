"""Due-date reminder preferences — palettes, defaults, validation, endpoints.

Pure-logic tests against app.services.due_reminder_prefs plus the
/users/me/notification-prefs handlers with a fake Supabase client.
"""

import asyncio

import pytest
from pydantic import ValidationError

from app.core.deps import CurrentUser
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import users
from app.services.due_reminder_prefs import (
    ACTUAL_BID_OFFSETS,
    TASK_OFFSETS,
    ActualBidPref,
    NotificationPrefsDoc,
    TaskKindPref,
    default_prefs,
    effective_prefs,
)


def _doc(**overrides) -> dict:
    base = {
        "internal_bid": {"enabled": True, "offsets": ["2w", "1d"]},
        "due_from_estimator": {"enabled": False, "offsets": ["1h"]},
        "due_from_vendors": {"enabled": True, "offsets": ["expired"]},
    }
    base.update(overrides)
    return base


# ── Palettes (ledger-key contract with due_reminders + migration CHECKs) ──


def test_palettes_are_pinned():
    assert TASK_OFFSETS == ("2w", "1w", "2d", "1d", "1h", "expired")
    assert ACTUAL_BID_OFFSETS == ("24h", "8h", "1h")


# ── Role defaults ─────────────────────────────────────────────────────────


def test_defaults_internal_bid_audience():
    for role in (Role.PM, Role.PE, Role.PA):
        assert default_prefs(role).internal_bid.enabled
    for role in (Role.EXECUTIVE, Role.ACCOUNTANT, Role.IT_ADMIN, Role.ESTIMATOR):
        assert not default_prefs(role).internal_bid.enabled


def test_defaults_due_from_estimator_audience():
    for role in (Role.PM, Role.PE, Role.PA):
        assert default_prefs(role).due_from_estimator.enabled
    assert not default_prefs(Role.EXECUTIVE).due_from_estimator.enabled


def test_defaults_due_from_vendors_only_pe():
    assert default_prefs(Role.PE).due_from_vendors.enabled
    for role in (Role.PM, Role.PA, Role.EXECUTIVE, Role.ACCOUNTANT, Role.IT_ADMIN):
        assert not default_prefs(role).due_from_vendors.enabled


def test_defaults_actual_bid_present_only_for_pa():
    pa = default_prefs(Role.PA).actual_bid
    assert pa is not None and pa.offsets == list(ACTUAL_BID_OFFSETS)
    for role in Role:
        if role != Role.PA:
            assert default_prefs(role).actual_bid is None


def test_defaults_disabled_kinds_still_carry_full_palette():
    doc = default_prefs(Role.ACCOUNTANT)
    assert doc.due_from_vendors.enabled is False
    assert doc.due_from_vendors.offsets == list(TASK_OFFSETS)


# ── Document validation ───────────────────────────────────────────────────


def test_unknown_top_level_key_rejected():
    with pytest.raises(ValidationError):
        NotificationPrefsDoc.model_validate(_doc(bogus={"enabled": True}))


def test_unknown_field_inside_kind_rejected():
    with pytest.raises(ValidationError):
        TaskKindPref.model_validate({"enabled": True, "offsets": [], "color": "red"})


def test_wrong_palette_offset_on_task_kind_rejected():
    with pytest.raises(ValidationError):
        TaskKindPref.model_validate({"enabled": True, "offsets": ["8h"]})


def test_expired_rejected_for_actual_bid():
    with pytest.raises(ValidationError):
        ActualBidPref.model_validate({"offsets": ["expired"]})


def test_actual_bid_has_no_enabled_field():
    # Mandatory-for-PA: presence = on; an enabled flag must not sneak in.
    with pytest.raises(ValidationError):
        ActualBidPref.model_validate({"enabled": False, "offsets": ["1h"]})


def test_empty_actual_bid_offsets_rejected():
    with pytest.raises(ValidationError):
        ActualBidPref.model_validate({"offsets": []})


def test_empty_task_offsets_accepted():
    assert TaskKindPref.model_validate({"enabled": True, "offsets": []}).offsets == []


def test_offsets_deduped_and_canonically_ordered():
    pref = TaskKindPref.model_validate({"enabled": True, "offsets": ["1h", "2w", "1h"]})
    assert pref.offsets == ["2w", "1h"]
    bid = ActualBidPref.model_validate({"offsets": ["1h", "24h", "1h"]})
    assert bid.offsets == ["24h", "1h"]


# ── effective_prefs ───────────────────────────────────────────────────────


def test_effective_none_stored_equals_defaults():
    for role in INTERNAL_ROLES:
        assert effective_prefs(role, None) == default_prefs(role)


def test_effective_missing_kind_falls_back_to_default_for_that_kind_only():
    stored = {"due_from_vendors": {"enabled": True, "offsets": ["1d"]}}
    eff = effective_prefs(Role.PM, stored)
    assert eff.due_from_vendors.enabled and eff.due_from_vendors.offsets == ["1d"]
    assert eff.internal_bid == default_prefs(Role.PM).internal_bid


def test_effective_corrupt_kind_falls_back_others_preserved():
    stored = {
        "internal_bid": {"enabled": "banana", "offsets": 7},
        "due_from_vendors": {"enabled": True, "offsets": ["2d"]},
    }
    eff = effective_prefs(Role.PM, stored)
    assert eff.internal_bid == default_prefs(Role.PM).internal_bid
    assert eff.due_from_vendors.offsets == ["2d"]


def test_effective_non_dict_stored_falls_back_entirely():
    eff = effective_prefs(Role.PE, ["not", "a", "dict"])
    assert eff == default_prefs(Role.PE)


def test_effective_strips_actual_bid_for_non_pa_even_if_stored():
    stored = {"actual_bid": {"offsets": ["8h"]}}
    assert effective_prefs(Role.EXECUTIVE, stored).actual_bid is None
    assert effective_prefs(Role.ESTIMATOR, stored).actual_bid is None


def test_effective_preserves_stored_actual_bid_for_pa():
    stored = {"actual_bid": {"offsets": ["8h"]}}
    assert effective_prefs(Role.PA, stored).actual_bid.offsets == ["8h"]


def test_effective_pa_corrupt_actual_bid_falls_back_to_all_offsets():
    stored = {"actual_bid": {"offsets": []}}  # min-1 violation
    assert effective_prefs(Role.PA, stored).actual_bid.offsets == list(ACTUAL_BID_OFFSETS)


# ── Endpoint handlers (fake Supabase; deps.require_internal guards roles) ──


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, sb, table):
        self._sb = sb
        self._table = table
        self._op = "select"
        self._payload = None

    def select(self, *a, **k):
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def upsert(self, payload, **kwargs):
        self._op = "upsert"
        self._payload = (payload, kwargs)
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        self._sb.calls.append((self._table, self._op, self._payload))
        return _FakeResult(self._sb.responses.get((self._table, self._op), []))


class _FakeSupabase:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def table(self, name):
        return _FakeQuery(self, name)


def _user(role: Role) -> CurrentUser:
    return CurrentUser(id="u1", email="u@x.com", role=role, is_active=True)


def test_put_strips_actual_bid_for_non_pa(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(users, "get_supabase", lambda: fake)
    monkeypatch.setattr(users, "audit", lambda *a, **k: None)
    body = NotificationPrefsDoc.model_validate(
        _doc(actual_bid={"offsets": ["8h"]})
    )
    out = asyncio.run(users.update_notification_prefs(body, _user(Role.PM)))
    (_, op, (payload, kwargs)) = fake.calls[0]
    assert op == "upsert" and kwargs == {"on_conflict": "user_id"}
    assert "actual_bid" not in payload["prefs"]
    assert out.is_customized is True and out.prefs.actual_bid is None


def test_put_preserves_actual_bid_for_pa(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(users, "get_supabase", lambda: fake)
    monkeypatch.setattr(users, "audit", lambda *a, **k: None)
    body = NotificationPrefsDoc.model_validate(
        _doc(actual_bid={"offsets": ["24h", "1h"]})
    )
    out = asyncio.run(users.update_notification_prefs(body, _user(Role.PA)))
    (_, _, (payload, _)) = fake.calls[0]
    assert payload["prefs"]["actual_bid"] == {"offsets": ["24h", "1h"]}
    assert out.prefs.actual_bid.offsets == ["24h", "1h"]


def test_get_without_row_returns_defaults_not_customized(monkeypatch):
    fake = _FakeSupabase({("notification_prefs", "select"): []})
    monkeypatch.setattr(users, "get_supabase", lambda: fake)
    out = asyncio.run(users.get_notification_prefs(_user(Role.PE)))
    assert out.is_customized is False
    assert out.prefs == default_prefs(Role.PE)


def test_get_with_row_merges_stored(monkeypatch):
    stored = {"due_from_vendors": {"enabled": False, "offsets": ["1h"]}}
    fake = _FakeSupabase({("notification_prefs", "select"): [{"prefs": stored}]})
    monkeypatch.setattr(users, "get_supabase", lambda: fake)
    out = asyncio.run(users.get_notification_prefs(_user(Role.PE)))
    assert out.is_customized is True
    assert out.prefs.due_from_vendors.enabled is False
    assert out.prefs.due_from_vendors.offsets == ["1h"]


def test_delete_removes_row_and_returns_defaults(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(users, "get_supabase", lambda: fake)
    monkeypatch.setattr(users, "audit", lambda *a, **k: None)
    out = asyncio.run(users.reset_notification_prefs(_user(Role.PA)))
    assert fake.calls[0][:2] == ("notification_prefs", "delete")
    assert out.is_customized is False
    assert out.prefs == default_prefs(Role.PA)
