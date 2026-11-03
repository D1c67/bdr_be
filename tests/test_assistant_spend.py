"""Spend reservations (app/assistant/spend.py; docs/ASSISTANT_PHASE2_PLAN.md
3.8, 3.14) and the hold stamp on llm_call_log (app/services/llm_gate.py,
3.8.4).

Invariants pinned here:

  admission  a hold is admitted while spent + held + amount < cap, per user
             and company-wide (a zero amount passes while spent + held <
             cap); a refusal names its scope and carries the state;
  holds      a live hold counts until settled, released or expired; a split
             hold counts max(amount, logged) through the log rows that carry
             its id; settle and release record the logged cost, once;
  wire       money goes to the RPCs as strings (never a float or Decimal);
  threads    two reservations racing for the same user: the second is
             refused when the first holds enough (the advisory lock);
  gate       log_call stamps spend_reservation_id from the attribution; a
             PGRST204 naming it drops only that key, one naming user_id
             drops only the 0170 pair;
  prices     the splitter and assistant models have PRICES rows.

The 0174 functions run as FakeDB's Python twins (tests/assistant_fakes.py);
`install_spend_rpcs` readies a FakeDB for them, and the loop tests use it too.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.assistant import llm_messages, spend
from app.core import supabase_client
from app.services import llm_gate, llm_pricing

from tests.assistant_fakes import NOW, RUN, USER, FakeDB, settings_row

OTHER = "33333333-3333-4333-8333-333333333333"
SPEND_RPCS = ("assistant_spend_state", "assistant_spend_reserve")


# ── The fake functions (section 2, F) ────────────────────────────────────────


def install_spend_rpcs(db: FakeDB) -> threading.RLock:
    """Make `db` ready for the 0174 functions. assistant_spend_state and
    assistant_spend_reserve are FakeDB's own Python twins
    (tests/assistant_fakes.py: Pacific day, live holds reduced by their
    stamped log rows, money arguments asserted to be strings); this only
    adds the two tables they read. Returns the lock standing in for the
    advisory lock. Kept for the loop and continuation tests that call it."""
    db.tables.setdefault("assistant_spend_reservations", [])
    db.tables.setdefault("llm_call_log", [])
    return db._spend_lock


@pytest.fixture
def db(monkeypatch):
    db = FakeDB(assistant_settings=[settings_row()], assistant_profiles=[], llm_call_log=[],
                assistant_spend_reservations=[])
    install_spend_rpcs(db)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    s = SimpleNamespace(assistant_daily_cap_usd=5, assistant_global_daily_cap_usd=50)
    monkeypatch.setattr(spend, "get_settings", lambda: s)
    return db


def _log(db, cost, *, user=USER, feature="assistant", run=None, hold=None, at=None):
    db.tables["llm_call_log"].append({
        "id": f"l{len(db.tables['llm_call_log'])}", "created_at": (at or NOW).isoformat(), "cost_usd": cost,
        "user_id": user, "feature": feature, "assistant_run_id": run, "spend_reservation_id": hold,
    })


def _hold(db, hold_id) -> dict:
    return next(r for r in db.tables["assistant_spend_reservations"] if r["id"] == hold_id)


# ── Admission ────────────────────────────────────────────────────────────────


def test_reserve_settle_and_release_send_money_as_strings(db):
    hold = spend.reserve(USER, kind="turn_call", amount=Decimal("0.25"), ttl=600, run_id=RUN)
    assert _hold(db, hold)["status"] == "active" and _hold(db, hold)["run_id"] == RUN
    name, params = db.rpc_calls[-1]
    assert name == "assistant_spend_reserve"
    for key in ("p_amount", "p_user_cap_default", "p_global_cap_default"):
        assert isinstance(params[key], str), key
    assert Decimal(params["p_amount"]) == Decimal("0.25") and params["p_ttl_seconds"] == 600
    _log(db, 0.1, hold=hold)
    spend.settle(USER, hold)
    row = _hold(db, hold)
    assert row["status"] == "settled" and Decimal(str(row["actual_usd"])) == Decimal("0.1")
    assert isinstance(row["actual_usd"], str)
    spend.settle(USER, hold)          # fenced on active: a no-op
    assert _hold(db, hold)["status"] == "settled"

    other = spend.reserve(USER, kind="split_task", amount=Decimal("1"), ttl=3600, task_id="t1", approval_id="a1")
    spend.release(USER, other)
    assert _hold(db, other)["status"] == "released" and Decimal(str(_hold(db, other)["actual_usd"])) == 0
    spend.settle(USER, None)
    spend.release(USER, None)


def test_user_cap_refuses_with_its_scope_and_state(db):
    _log(db, 4.5)
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(USER, kind="turn_call", amount=Decimal("0.5"), ttl=600)
    assert exc.value.scope == "user"
    assert exc.value.state.user_spent == Decimal("4.5") and exc.value.state.user_cap == Decimal("5")
    assert db.tables["assistant_spend_reservations"] == []
    # Just under the cap still fits.
    assert spend.reserve(USER, kind="turn_call", amount=Decimal("0.49"), ttl=600)


def test_global_cap_counts_every_assistant_feature_and_attributed_split_calls(db):
    db.tables["assistant_settings"][0]["global_daily_cap_usd"] = 3
    _log(db, 1, user=OTHER)                                   # another user's turn
    _log(db, 1.5, user=OTHER, feature="bid_split", run=RUN)   # a split the assistant started
    _log(db, 10, user=OTHER, feature="boq")                   # not the assistant's
    st = spend.state(USER)
    assert st.global_spent == Decimal("2.5") and st.user_spent == Decimal("0")
    with pytest.raises(spend.SpendRefused) as exc:
        spend.reserve(USER, kind="turn_call", amount=Decimal("0.5"), ttl=600)
    assert exc.value.scope == "global"


def test_a_zero_amount_passes_while_spent_is_under_the_cap(db):
    _log(db, 4.99)
    assert spend.reserve(USER, kind="turn_call", amount=Decimal("0"), ttl=600)
    _log(db, 0.01)
    with pytest.raises(spend.SpendRefused):
        spend.reserve(USER, kind="turn_call", amount=Decimal("0"), ttl=600)


def test_a_hold_counts_until_settled_or_expired(db):
    hold = spend.reserve(USER, kind="split_task", amount=Decimal("3"), ttl=3600)
    st = spend.state(USER)
    assert st.user_held == Decimal("3") and st.user_remaining == Decimal("2") and st.global_held == Decimal("3")
    with pytest.raises(spend.SpendRefused):
        spend.reserve(USER, kind="turn_call", amount=Decimal("2"), ttl=600)
    spend.settle(USER, hold)
    assert spend.state(USER).user_held == 0
    # An expired hold stops counting by itself.
    late = spend.reserve(USER, kind="split_task", amount=Decimal("4"), ttl=60)
    db.now = NOW + timedelta(minutes=5)
    assert spend.state(USER).user_held == 0
    assert _hold(db, late)["status"] == "active"


def test_a_split_hold_counts_max_of_amount_and_logged(db):
    hold = spend.reserve(USER, kind="split_task", amount=Decimal("2"), ttl=3600, task_id="t1")
    _log(db, 0.75, feature="bid_split", run=RUN, hold=hold)
    st = spend.state(USER)
    # Logged 0.75 plus the unused 1.25 of the hold: 2 in all, never 2.75.
    assert st.user_spent == Decimal("0.75") and st.user_held == Decimal("1.25")
    assert st.user_spent + st.user_held == Decimal("2")
    _log(db, 2, feature="bid_split", run=RUN, hold=hold)      # over the hold: held floors at 0
    assert spend.state(USER).user_held == 0
    spend.settle(USER, hold)
    assert Decimal(str(_hold(db, hold)["actual_usd"])) == Decimal("2.75")


def test_two_threads_racing_for_one_user_admit_only_what_fits(db):
    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        barrier.wait()
        try:
            results.append(spend.reserve(USER, kind="split_task", amount=Decimal("3"), ttl=3600))
        except spend.SpendRefused as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, spend.SpendRefused) for r in results) == 1
    assert len(db.tables["assistant_spend_reservations"]) == 1


def test_the_env_default_applies_only_without_a_profile_row(db, monkeypatch):
    assert spend.state(USER).user_cap == Decimal("5")
    monkeypatch.setattr(spend, "get_settings", lambda: SimpleNamespace(
        assistant_daily_cap_usd=7, assistant_global_daily_cap_usd=50))
    assert spend.state(USER).user_cap == Decimal("7")
    db.tables["assistant_profiles"].append({"user_id": USER, "daily_cost_cap_usd": 12})
    assert spend.state(USER).user_cap == Decimal("12")


def test_reserve_refuses_bad_arguments_before_the_rpc(db):
    for kwargs in ({"kind": "nope", "amount": Decimal("1"), "ttl": 600},
                   {"kind": "turn_call", "amount": Decimal("-1"), "ttl": 600},
                   {"kind": "turn_call", "amount": Decimal("1"), "ttl": 5},
                   {"kind": "turn_call", "amount": Decimal("1"), "ttl": 50000}):
        with pytest.raises(ValueError):
            spend.reserve(USER, **kwargs)
    assert db.rpc_calls == []


def test_spend_state_properties():
    st = spend.SpendState(Decimal("5"), Decimal("4"), Decimal("2"), Decimal("50"), Decimal("1"), Decimal("0"))
    assert st.user_remaining == 0 and st.global_remaining == Decimal("49") and st.over_cap is True
    st = spend.SpendState.from_rpc({"user_cap": "5", "user_spent": "1.5"}, user_default=Decimal("9"),
                                   global_default=Decimal("40"))
    assert st.user_cap == Decimal("5") and st.global_cap == Decimal("40") and st.user_held == 0
    assert st.over_cap is False


# ── Attribution and the llm_call_log stamp ───────────────────────────────────


def test_attributed_carries_the_hold_and_restores_the_outer_scope():
    assert llm_messages.current_attribution() is None
    with llm_messages.attributed(USER, RUN):
        assert llm_messages.current_attribution().reservation_id is None
        with llm_messages.attributed(USER, RUN, reservation_id="h1"):
            att = llm_messages.current_attribution()
            assert (att.user_id, att.run_id, att.reservation_id) == (USER, RUN, "h1")
        assert llm_messages.current_attribution().reservation_id is None
    assert llm_messages.current_attribution() is None


class _LogDB:
    """llm_call_log inserts that refuse named columns once, like PostgREST
    on a database that predates a migration."""

    def __init__(self, missing: set[str]):
        self.missing = set(missing)
        self.rows: list[dict] = []
        self.attempts: list[dict] = []

    def table(self, name):
        assert name == "llm_call_log"
        outer = self

        class _Q:
            def insert(self, row):
                self.row = dict(row)
                return self

            def execute(self):
                outer.attempts.append(dict(self.row))
                for col in sorted(outer.missing):
                    if col in self.row:
                        err = Exception(f"Could not find the '{col}' column of 'llm_call_log' in the schema cache")
                        err.code = "PGRST204"
                        raise err
                outer.rows.append(dict(self.row))
                return SimpleNamespace(data=[self.row])

        return _Q()


def _log_one(monkeypatch, fake: _LogDB, reservation_id: str | None = "hold-1"):
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    monkeypatch.setattr(llm_gate, "_eval_sweep_id", lambda: None)
    with llm_messages.attributed(USER, RUN, reservation_id=reservation_id):
        llm_gate.log_call(feature="bid_split", provider="anthropic", model="claude-opus-5", tier_name="t",
                          ok=True, error=None, duration_ms=5, input_tokens=10, output_tokens=2, cost_usd=0.01)


def test_log_call_stamps_the_hold_from_the_attribution(monkeypatch):
    fake = _LogDB(set())
    _log_one(monkeypatch, fake)
    assert fake.rows[0]["spend_reservation_id"] == "hold-1"
    assert fake.rows[0]["user_id"] == USER and fake.rows[0]["assistant_run_id"] == RUN
    fake = _LogDB(set())
    _log_one(monkeypatch, fake, reservation_id=None)
    assert "spend_reservation_id" not in fake.rows[0] and fake.rows[0]["user_id"] == USER


def test_a_missing_hold_column_drops_only_the_hold_stamp(monkeypatch):
    monkeypatch.setattr(llm_gate, "_reservation_column_warned", False)
    fake = _LogDB({"spend_reservation_id"})
    _log_one(monkeypatch, fake)
    row = fake.rows[0]
    assert "spend_reservation_id" not in row
    assert row["user_id"] == USER and row["assistant_run_id"] == RUN and row["cost_usd"] == 0.01
    assert len(fake.attempts) == 2


def test_missing_attribution_columns_drop_only_the_0170_pair(monkeypatch):
    monkeypatch.setattr(llm_gate, "_attribution_columns_warned", False)
    fake = _LogDB({"user_id"})
    _log_one(monkeypatch, fake)
    row = fake.rows[0]
    assert "user_id" not in row and "assistant_run_id" not in row
    assert row["spend_reservation_id"] == "hold-1" and row["cost_usd"] == 0.01


# ── PRICES (3.14) and module hygiene ─────────────────────────────────────────


def test_splitter_and_planner_models_are_priced():
    from app.core.config import Settings

    s = Settings.model_construct()
    for model in (s.claude_bid_split_model, s.openai_bid_split_model, s.assistant_model):
        assert llm_pricing.price_for(model) is not None, model


def test_spend_and_continue_job_never_hold_a_client():
    root = Path(__file__).resolve().parents[1] / "app" / "assistant"
    for name in ("spend.py", "continue_job.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "get_supabase" not in text and "supabase_client" not in text, name
        assert "\u2014" not in text, name


# ── phase 4: AE holds (docs/ASSISTANT_PHASE4_PLAN.md 3.7) ────────────────────


def test_phase4_kinds():
    assert spend.KIND_PLANS_PAGES == "plans_pages" and spend.KIND_AI_ESTIMATE == "ai_estimate"
    # Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md 5.4, MC-9, F8): six kinds; triage
    # stays out of the external-actual branch (its call carries the hold).
    assert spend.KINDS == ("turn_call", "split_task", "plans_pages", "ai_estimate", "browser_task", "triage")
    assert spend.KIND_TRIAGE == "triage"
    assert spend.EXTERNAL_KINDS == ("plans_pages", "ai_estimate", "browser_task")


@pytest.mark.parametrize("kind", ["plans_pages", "ai_estimate", "browser_task"])
def test_reserve_accepts_the_ae_kinds(db, monkeypatch, kind):
    seen = []

    def fake_reserve(user_id, **kw):
        seen.append(kw)
        return {"ok": True, "id": "hold-ae", "state": {}}

    monkeypatch.setattr(spend.repo, "rpc_spend_reserve", fake_reserve)
    assert spend.reserve(USER, kind=kind, amount=Decimal("2.13"), ttl=14400, approval_id="a1") == "hold-ae"
    assert seen[0]["kind"] == kind and seen[0]["amount"] == Decimal("2.13")
    with pytest.raises(ValueError, match="unknown reservation kind"):
        spend.reserve(USER, kind="plans", amount=Decimal("1"), ttl=600)


def _ae_hold(db, hold_id, *, user=USER, kind="plans_pages", status="active"):
    from tests.assistant_fakes import reservation

    db.tables["assistant_spend_reservations"].append(
        reservation(id=hold_id, user_id=user, kind=kind, amount_usd="2.13", status=status))


def test_settle_external_closes_with_the_given_actual_once(db):
    _ae_hold(db, "h1")
    _log(db, 0.5, hold="h1")    # a stamped log row is NOT the actual for an AE hold
    assert spend.settle_external(USER, "h1", Decimal("1.42")) is True
    row = _hold(db, "h1")
    assert row["status"] == "settled" and Decimal(str(row["actual_usd"])) == Decimal("1.42")
    assert isinstance(row["actual_usd"], str)
    # Fenced on active: a second close (or a later release) changes nothing.
    assert spend.settle_external(USER, "h1", Decimal("9")) is False
    assert spend.settle_external(USER, "h1", Decimal("9"), release=True) is False
    assert Decimal(str(_hold(db, "h1")["actual_usd"])) == Decimal("1.42") and _hold(db, "h1")["status"] == "settled"


def test_settle_external_release_and_refusals(db):
    _ae_hold(db, "h2", kind="ai_estimate")
    _ae_hold(db, "h3", user=OTHER)
    assert spend.settle_external(USER, "h2", "3.10", release=True) is True
    assert _hold(db, "h2")["status"] == "released" and Decimal(str(_hold(db, "h2")["actual_usd"])) == Decimal("3.10")
    assert spend.settle_external(USER, "h3", Decimal("1")) is False         # not this person's hold
    assert _hold(db, "h3")["status"] == "active"
    assert spend.settle_external(USER, None, Decimal("1")) is False
    _ae_hold(db, "h4")
    for bad in (Decimal("-0.01"), "NaN", "abc", None):
        with pytest.raises(ValueError):
            spend.settle_external(USER, "h4", bad)
    assert _hold(db, "h4")["status"] == "active"
    assert spend.settle_external(USER, "h4", 0) is True


# ── phase 8: the company spend identity (docs/ASSISTANT_PHASE8_PLAN.md 4.1 A2a, 5.4) ──

COMPANY = "00000000-0000-0000-0000-000000000001"   # 0196 assistant_company_spend_id()


def test_the_company_spend_id_is_the_0196_value_and_not_a_v4_uuid():
    assert spend.COMPANY_SPEND_ID == COMPANY == spend.repo.COMPANY_SPEND_ID
    assert COMPANY[14] == "0"   # version nibble 0: gen_random_uuid() never yields it


@pytest.mark.parametrize("user_id, kind", [
    (USER, "triage"),                     # a person never holds a triage hold
    (None, "triage"),
    ("", "triage"),
    (COMPANY, "turn_call"),               # the company id holds nothing else
    (COMPANY, "split_task"),
    (COMPANY, "plans_pages"),
    (COMPANY, "ai_estimate"),
    (COMPANY, "browser_task"),
    (COMPANY.upper(), "turn_call"),       # any spelling of the company id
])
def test_reserve_refuses_a_bad_kind_and_id_pairing_before_the_rpc(db, monkeypatch, user_id, kind):
    calls = []
    monkeypatch.setattr(spend.repo, "rpc_spend_reserve", lambda *a, **kw: calls.append(kw) or {"ok": True, "id": "h"})
    with pytest.raises(ValueError, match="company spend identity"):
        spend.reserve(user_id, kind=kind, amount=Decimal("0.01"), ttl=330)
    assert calls == []


def test_reserve_takes_a_triage_hold_under_the_company_id(db):
    hold = spend.reserve(COMPANY, kind=spend.KIND_TRIAGE, amount=Decimal("0.0012"), ttl=330, run_id=RUN)
    row = _hold(db, hold)
    assert row["user_id"] == COMPANY and row["kind"] == "triage" and row["run_id"] == RUN


def test_a_triage_hold_skips_the_per_person_cap_and_counts_only_globally(db):
    # A person at their cap does not stop triage (owner question 12 (b)) ...
    db.tables["assistant_profiles"].append({"user_id": USER, "daily_cost_cap_usd": 0})
    with pytest.raises(spend.SpendRefused) as refused:
        spend.reserve(USER, kind="turn_call", amount=Decimal("0.01"), ttl=600)
    assert refused.value.scope == spend.SCOPE_USER
    hold = spend.reserve(COMPANY, kind="triage", amount=Decimal("1"), ttl=330, run_id=RUN)
    # ... its live hold is in the company's held and in no person's held.
    person = spend.state(USER)
    assert person.user_held == 0 and person.global_held == Decimal("1")
    # A settled triage call logged under the company id raises global_spent
    # and leaves the person's user_spent unchanged (A2a).
    _log(db, 0.25, user=COMPANY, feature="assistant_triage", run=RUN, hold=hold)
    spend.settle(COMPANY, hold)
    after = spend.state(USER)
    assert after.user_spent == 0 and after.global_spent == Decimal("0.25") and after.global_held == 0
    assert _hold(db, hold)["status"] == "settled" and Decimal(str(_hold(db, hold)["actual_usd"])) == Decimal("0.25")


def test_a_triage_hold_is_refused_at_the_company_cap_with_scope_global(db):
    # Pure sharing, no floor (owner question 11 (b)): spent + held + amount >= cap.
    _log(db, 49.5, user=OTHER, run=RUN)
    # A queued browser task's live hold counts in the company's held (2.1 item 11).
    from tests.assistant_fakes import reservation

    db.tables["assistant_spend_reservations"].append(reservation(
        id="b0b0b0b0-b0b0-4b0b-8b0b-b0b0b0b0b0b0", user_id=USER, kind="browser_task", amount_usd="0.3",
        expires_at=(NOW + timedelta(hours=8)).isoformat()))
    with pytest.raises(spend.SpendRefused) as refused:
        spend.reserve(COMPANY, kind="triage", amount=Decimal("0.2"), ttl=330)
    assert refused.value.scope == spend.SCOPE_GLOBAL
    assert spend.reserve(COMPANY, kind="triage", amount=Decimal("0.19"), ttl=330)


def test_the_fake_reserve_mirrors_0196_b2_refusals(db):
    from tests.assistant_fakes import CheckViolation

    for user_id, kind in ((USER, "triage"), (COMPANY, "turn_call"), (COMPANY, "browser_task")):
        with pytest.raises(CheckViolation):
            db.rpc("assistant_spend_reserve", {
                "p_user_id": user_id, "p_kind": kind, "p_amount": "0.01", "p_ttl_seconds": 330,
                "p_user_cap_default": "5", "p_global_cap_default": "50", "p_run_id": None, "p_task_id": None,
                "p_approval_id": None}).execute()


def test_the_per_person_spend_state_refuses_the_company_id(db):
    with pytest.raises(ValueError, match="company spend id"):
        spend.state(COMPANY)
