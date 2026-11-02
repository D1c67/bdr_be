"""Verify commit refuses figures read from a stale page.

Prod 26.10.7274 (2026-11-02): the Verify panel had loaded before quotes were
picked and labor/markup entered, never reloaded, and Commit posted its $0
section figures as Executive overrides. The commit now refuses (409, nothing
written) any submitted figure matching neither the live upstream number nor
the saved verify draft.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.roles import Role
from app.models.schemas import VerifyOverrideIn
from app.routers import pricing
from tests.test_reverify import FakeDB, _cat_rows, _patch_commit, _project, _verification

EXEC = SimpleNamespace(id="exec1", role=Role.EXECUTIVE)

D = Decimal


def _sections(**present):
    base = {"materials": True, "gear": False, "underground": False, "low_voltage": False}
    base.update(present)
    return {k: {"present": v} for k, v in base.items()}


ORIGINALS = {
    "labor_amount": D("232000.00"),
    "materials_amount": D("240120.92"),
    "gear_amount": D("168604.85"),
    "underground_amount": D("99000.00"),
    "low_voltage_amount": D("121.21"),
    "labor_markup_amount": None,
    "materials_markup_amount": D("11734.43"),
    "gear_markup_amount": D("8430.24"),
    "underground_markup_amount": None,
    "low_voltage_markup_amount": None,
}
ALL_PRESENT = _sections(gear=True, underground=True, low_voltage=True)


def test_the_7274_body_is_stale():
    # Exactly what the stale panel posted: zeros for every section, rest blank.
    body = VerifyOverrideIn(
        materials_amount=D("0"), gear_amount=D("0"),
        underground_amount=D("0"), low_voltage_amount=D("0"),
    )
    stale = pricing.stale_verify_figures(ORIGINALS, None, body, ALL_PRESENT)
    assert [k for k, _, _ in stale] == [
        "materials_amount", "gear_amount", "underground_amount", "low_voltage_amount",
    ]
    msg = pricing._stale_message(stale)
    assert "Materials shows $0.00 but is now $240,120.92" in msg
    assert "Nothing was committed" in msg


def test_live_figures_pass_within_half_a_cent():
    body = VerifyOverrideIn(
        labor_amount=D("232000"), materials_amount=D("240120.924"),
        gear_amount=D("168604.85"), materials_markup_amount=D("11734.43"),
    )
    assert pricing.stale_verify_figures(ORIGINALS, None, body, ALL_PRESENT) == []


def test_saved_draft_value_passes():
    # A saved (PUT /verify) draft value is what the panel shows: legitimate.
    saved = {"labor_amount": 250000.0, "committed_at": None}
    body = VerifyOverrideIn(labor_amount=D("250000.00"))
    assert pricing.stale_verify_figures(ORIGINALS, saved, body, ALL_PRESENT) == []


def test_figure_for_absent_section_is_ignored():
    body = VerifyOverrideIn(gear_amount=D("0"))
    assert pricing.stale_verify_figures(ORIGINALS, None, body, _sections()) == []


def test_value_where_live_is_blank_is_stale():
    body = VerifyOverrideIn(labor_markup_amount=D("500"))
    stale = pricing.stale_verify_figures(ORIGINALS, None, body, ALL_PRESENT)
    assert stale == [("labor_markup_amount", D("500"), None)]
    assert "Labor markup shows $500.00 but is now blank" in pricing._stale_message(stale)


def test_empty_body_never_stale():
    assert pricing.stale_verify_figures(ORIGINALS, None, VerifyOverrideIn(), ALL_PRESENT) == []


# ── through commit_verify ─────────────────────────────────────────────────────


def _db(verification=None):
    return FakeDB({
        "verifications": [verification or _verification(committed=False)],
        "projects": [_project("verify")],
        "project_category_state": _cat_rows(send_out=("verify", "active")),
    })


def test_stale_commit_is_refused_and_writes_nothing(monkeypatch):
    # The fake has no quotes, so live materials is 0 and labor is blank.
    db = _db()
    _, calls = _patch_commit(monkeypatch, db)
    audits = []
    monkeypatch.setattr(pricing, "audit", lambda *a, **k: audits.append(a))
    with pytest.raises(HTTPException) as exc:
        pricing.commit_verify("p1", VerifyOverrideIn(materials_amount=D("500")), EXEC)
    assert exc.value.status_code == 409
    assert "Materials shows $500.00 but is now $0.00" in exc.value.detail
    assert db.tables["verifications"][0]["committed_at"] is None
    assert calls == []
    assert [a[1] for a in audits] == ["pricing.commit_refused_stale"]


def test_matching_commit_still_commits(monkeypatch):
    db = _db()
    _, calls = _patch_commit(monkeypatch, db)
    row = pricing.commit_verify("p1", VerifyOverrideIn(materials_amount=D("0")), EXEC)
    assert row["committed_at"] is not None
    assert ("advance", "p1", "send_out") in calls


def test_saved_draft_override_still_commits(monkeypatch):
    draft = {**_verification(committed=False), "labor_amount": 250000.0}
    db = _db(draft)
    _, calls = _patch_commit(monkeypatch, db)
    row = pricing.commit_verify("p1", VerifyOverrideIn(labor_amount=D("250000")), EXEC)
    assert row["committed_at"] is not None
    assert ("advance", "p1", "send_out") in calls
