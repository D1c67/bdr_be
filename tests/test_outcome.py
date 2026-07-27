"""Unit tests for the Win/Loss (bid outcome) pure logic — no DB.

Covers the snapshot of what we bid, the grid merge of recorded outcomes onto the
GCs we bid to, the "did we actually win the work" derivation, and the send_out
category gate that only lets an outcome be recorded once the bid is Submitted.
"""

from decimal import Decimal

from app.services import outcome
from app.services.workflow import STAGES, owner_role_for


# ── our_amount_of ────────────────────────────────────────────────────────────


def test_our_amount_sums_material_and_labor():
    assert outcome.our_amount_of("40000", "41950") == Decimal("81950")


def test_our_amount_treats_missing_half_as_zero():
    assert outcome.our_amount_of("40000", None) == Decimal("40000")
    assert outcome.our_amount_of(None, "950") == Decimal("950")


def test_our_amount_is_none_only_when_both_absent():
    # Legacy proposal_sends rows generated before per-GC amounts existed.
    assert outcome.our_amount_of(None, None) is None


# ── won_via_us ───────────────────────────────────────────────────────────────


def test_won_via_us_true_when_a_gc_won_and_used_us():
    rows = [
        {"gc_award_result": "lost", "our_bid_selection": "used_us"},
        {"gc_award_result": "won", "our_bid_selection": "used_us"},
    ]
    assert outcome.won_via_us(rows) is True


def test_won_via_us_false_when_winner_used_a_competitor():
    # The user's scenario: a GC we bid to won the job but went with someone else.
    rows = [{"gc_award_result": "won", "our_bid_selection": "used_other"}]
    assert outcome.won_via_us(rows) is False


def test_won_via_us_false_when_only_a_loser_chose_us():
    # The other scenario: a GC chose us but lost the job — no work, not a win.
    rows = [{"gc_award_result": "lost", "our_bid_selection": "used_us"}]
    assert outcome.won_via_us(rows) is False


# ── merge_gc_outcomes ────────────────────────────────────────────────────────


def test_merge_defaults_unrecorded_gcs_to_unknown():
    sent = [{"gc_id": "g1", "gc_name": "Acme", "our_amount": Decimal("81950"), "emails": ["a@x.com"]}]
    merged = outcome.merge_gc_outcomes(sent, [])
    assert merged == [
        {
            "gc_id": "g1",
            "gc_name": "Acme",
            "emails": ["a@x.com"],
            "our_amount": "81950",  # Decimals serialize as strings over the wire
            "gc_award_result": "unknown",
            "our_bid_selection": "unknown",
            "winning_amount": None,
        }
    ]


def test_merge_overlays_recorded_outcome_onto_the_bid_to_gc():
    sent = [{"gc_id": "g1", "gc_name": "Acme", "our_amount": Decimal("81950"), "emails": []}]
    recorded = [
        {
            "gc_id": "g1",
            "gc_award_result": "won",
            "our_bid_selection": "used_other",
            "winning_amount": "79000.00",
        }
    ]
    [row] = outcome.merge_gc_outcomes(sent, recorded)
    assert row["gc_award_result"] == "won"
    assert row["our_bid_selection"] == "used_other"
    assert row["winning_amount"] == "79000.00"
    assert row["our_amount"] == "81950"  # still the snapshot of what we bid


def test_merge_ignores_recorded_rows_for_gcs_we_did_not_bid_to():
    sent = [{"gc_id": "g1", "gc_name": "Acme", "our_amount": None, "emails": []}]
    recorded = [{"gc_id": "ghost", "gc_award_result": "won", "our_bid_selection": "used_us"}]
    merged = outcome.merge_gc_outcomes(sent, recorded)
    assert [r["gc_id"] for r in merged] == ["g1"]
    assert merged[0]["our_amount"] is None


# ── ownership of the closeout tasks ──────────────────────────────────────────


def test_bid_outcome_owned_by_estimating_admin():
    assert STAGES["bid_outcome"].owner_roles  # Estimating Admin owns it (can correct)
    assert owner_role_for("bid_outcome").value == "estimating_admin"


def test_submitted_now_owned_by_estimating_admin():
    # The outstanding task at Submitted is recording the outcome — the Estimating
    # Admin's job.
    assert owner_role_for("submitted").value == "estimating_admin"


# ── record_outcome: the send_out-head gate ────────────────────────────────────
# record_outcome is only reachable once the send_out category head is Submitted
# (advance to bid_outcome) or already at bid_outcome (in-place correction).

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from app.services import workflow  # noqa: E402
from app.services.outcome import OutcomeError, record_outcome  # noqa: E402
from app.models.schemas import BidOutcomeIn  # noqa: E402


class _OutcomeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._op = "select"
        self._payload = None
        self._filters = []
        self._single = False

    def select(self, *a, **k):
        self._op = "select"
        return self

    def upsert(self, payload, **k):
        self._op, self._payload = "upsert", payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        rows = self.db.tables.get(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if all(r.get(c) == v for c, v in self._filters)]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        self.db.upserts.append((self.table, self._payload))
        return SimpleNamespace(data=[])


class _OutcomeDB:
    def __init__(self, tables):
        self.tables = tables
        self.upserts = []

    def table(self, name):
        return _OutcomeQuery(self, name)


def _outcome_env(monkeypatch, send_head):
    db = _OutcomeDB({
        "projects": [{"id": "p1", "name": "Acme", "current_stage": send_head}],
        "proposal_sends": [],  # no GCs → empty grid, keeps the merge trivial
        "bid_outcomes": [],
        "bid_gc_outcomes": [],
    })
    monkeypatch.setattr(outcome, "get_supabase", lambda: db)
    monkeypatch.setattr(
        workflow, "load_category_state",
        lambda pid: {"send_out": {"current_task": send_head, "status": "active"}},
    )
    advanced = []
    monkeypatch.setattr(
        workflow, "advance_category",
        lambda pid, cat, uid, note=None: advanced.append((pid, cat, note)),
    )
    monkeypatch.setattr(outcome, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(outcome, "audit", lambda *a, **k: None)
    return db, advanced


def test_record_outcome_rejected_before_submitted(monkeypatch):
    _outcome_env(monkeypatch, "verify")
    with pytest.raises(OutcomeError) as exc:
        record_outcome("p1", "pa1", BidOutcomeIn(result="won"))
    assert "Submitted" in str(exc.value)


def test_record_outcome_from_submitted_advances_send_out(monkeypatch):
    _db, advanced = _outcome_env(monkeypatch, "submitted")
    record_outcome("p1", "pa1", BidOutcomeIn(result="lost"))
    # Advances the send_out head submitted → bid_outcome exactly once.
    assert advanced == [("p1", "send_out", "Outcome recorded: lost")]


def test_record_outcome_at_bid_outcome_is_in_place(monkeypatch):
    _db, advanced = _outcome_env(monkeypatch, "bid_outcome")
    # 'lost' — a win would also demand winning_gc_id (0069) and PM activation.
    record_outcome("p1", "pa1", BidOutcomeIn(result="lost"))
    # Already terminal: re-recording just updates in place, no further advance.
    assert advanced == []
