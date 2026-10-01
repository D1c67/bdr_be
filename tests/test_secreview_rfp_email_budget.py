"""Security review 2026-09-30, group rfp-email-budget (finding 8, attempt 3,
and the finding 18 follow-up).

- The per-sender classify budget (docs/RFP_EMAIL_INGESTION.md 3.5) is keyed
  on the registrable domain, so rotating sibling subdomains shares one
  budget, and on the exact address at a public mail provider, so one free
  account cannot spend every Gmail user's budget.
- Adding a rule releases rows the budget deferred at `classify` (3.7).
- The sweep's second window keeps a reserved slice of the batch, so fresh
  mail above a batch per tick cannot starve retries and polls (5).
- The detail route exposes the alignment columns and `classified_at`.
- RFP_BC_CALLBACK_RATE_LIMIT_PER_MIN below 1 is refused at boot.

The pipeline fakes and fixtures are the ones tests/test_rfp_email_ingest.py
defines; importing them here makes its autouse defaults apply too."""

# ruff: noqa: F811 - pytest fixtures are injected by name, which ruff reads as a redefinition

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.routers import rfp_emails as rr
from app.services import rfp_email_auth as auth
from app.services import rfp_email_ingest as ingest
from tests import test_rfp_email_ingest as tri
from tests.test_rfp_email_ingest import (  # noqa: F401 - fixtures
    EXO_PASS,
    _defaults,
    _email,
    _row,
    _seed,
    _settings,
    db,
    llm_answer,
)


def _pending_classify(db, email_id, from_address, **over):
    return _seed(db, _email(
        id=email_id, internet_message_id=f"{email_id}@x", from_address=from_address,
        subject=f"Invitation to Bid {email_id}", status="classify", body_text="please bid",
        keyword_hits=["bid"], auth_raw=EXO_PASS, auth_verdict="pass", **over,
    ))


def _classified(db, email_id, from_address, classified_at):
    return _seed(db, _email(
        id=email_id, internet_message_id=f"{email_id}@x", from_address=from_address,
        status="flagged_unauthorized", llm_model="test-model",
        received_at=classified_at, classified_at=classified_at,
    ))


def _budget_settings(monkeypatch, **over):
    knobs = dict(
        rfp_email_ingestion_classify_budget_per_sender_per_day=20,
        rfp_email_ingestion_classify_budget_per_tick=25,
    )
    knobs.update(over)
    s = _settings(**knobs)
    monkeypatch.setattr(ingest, "get_settings", lambda: s)
    return s


def _classify_calls(calls):
    return [c for c in calls if c["feature"] == "rfp_classify"]


# ── Budget key ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("domain, expected", [
    ("evil.example", "evil.example"),
    ("a0.evil.example", "evil.example"),
    ("deep.a0.evil.example", "evil.example"),
    ("Bids.GC.co.uk", "gc.co.uk"),
    ("mail.sub.gc.com.au", "gc.com.au"),
    ("gc.co.uk", "gc.co.uk"),
    ("co.uk", "co.uk"),
    ("sub.example.com", "example.com"),
    ("example", "example"),
    ("", ""),
    (None, ""),
])
def test_registrable_domain(domain, expected):
    assert auth.registrable_domain(domain) == expected


def test_classify_budget_key_is_the_address_at_a_public_provider_and_the_org_domain_otherwise():
    assert auth.classify_budget_key("Some.One@Outlook.com") == ("address", "some.one@outlook.com")
    assert auth.classify_budget_key("x@a0.evil.example") == ("domain", "evil.example")
    assert auth.classify_budget_key("x@a1.evil.example") == ("domain", "evil.example")
    assert auth.classify_budget_key("x@bids.gc.co.uk") == ("domain", "gc.co.uk")
    assert auth.classify_budget_key("nodomain") == ("domain", "")
    assert auth.classify_budget_key(None) == ("domain", "")


def test_rotating_sibling_subdomains_shares_one_daily_budget(db, llm_answer, monkeypatch):
    """The retest-2 bypass: a0.evil.example, a1.evil.example ... each got a
    fresh budget. They all count against evil.example now."""
    _budget_settings(monkeypatch, rfp_email_ingestion_classify_budget_per_sender_per_day=2)
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""})
    recent = ingest._iso(ingest._now() - timedelta(hours=1))
    _classified(db, "s0", "x@a0.evil.example", recent)
    _classified(db, "s1", "x@a1.evil.example", recent)
    _pending_classify(db, "n", "x@a2.evil.example", received_at=recent)
    _pending_classify(db, "other", "pm@unrelated.example", received_at=recent)
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats())
    assert len(_classify_calls(calls)) == 1          # only the unrelated sender
    n = _row(db, "n")
    assert n["status"] == "classify" and n["next_attempt_at"] and n["attempts"] == 0
    assert "evil.example" in n["last_error"] and "budget" in n["last_error"]
    assert _row(db, "other")["status"] == "flagged_unauthorized"


@pytest.mark.parametrize("address, key", [
    ("spammer+1@gmail.com", "spammer@gmail.com"),
    ("Spammer+anything+else@GMAIL.com", "spammer@gmail.com"),
    ("s.p.a.m.m.e.r@googlemail.com", "spammer@gmail.com"),
    ("spammer@gmail.com", "spammer@gmail.com"),
    ("some.one+tag@outlook.com", "some.one@outlook.com"),   # dots count outside Gmail
    ("rep+1@comcast.net", "rep@comcast.net"),               # consumer ISP mailbox
    ("rep+1@hotmail.co.uk", "rep@hotmail.co.uk"),
])
def test_classify_budget_key_folds_every_spelling_of_one_mailbox(address, key):
    """Retest-3 bypass (a): plus tags and Gmail dot variants minted a fresh
    budget per spelling of one free account."""
    assert auth.classify_budget_key(address) == ("address", key)


def test_registrable_domain_keeps_the_tenant_under_a_shared_host_suffix():
    assert auth.registrable_domain("t1.onmicrosoft.com") == "t1.onmicrosoft.com"
    assert auth.registrable_domain("mail.t1.onmicrosoft.com") == "t1.onmicrosoft.com"
    assert auth.classify_budget_key("x@t2.onmicrosoft.com") == ("domain", "t2.onmicrosoft.com")


@pytest.mark.parametrize("domain, suffix", [
    ("gc.co.uk", "co.uk"), ("a0.co.de", "co.de"), ("t1.onmicrosoft.com", "onmicrosoft.com"),
    ("evil.example", ""), ("gc.com", ""), ("deep.gc.co.uk", ""),
])
def test_budget_parent_suffix(domain, suffix):
    assert auth.budget_parent_suffix(domain) == suffix


def test_address_key_patterns_are_wide_enough_and_refuse_unsafe_keys():
    assert ingest._address_key_patterns("spammer@gmail.com") == [
        "s%p%a%m%m%e%r%@gmail.com", "s%p%a%m%m%e%r%@googlemail.com",
    ]
    assert ingest._address_key_patterns("some.one@outlook.com") == ["some.one%@outlook.com"]
    assert ingest._address_key_patterns("a%b@gmail.com") is None
    assert ingest._address_key_patterns("@gmail.com") is None
    assert ingest._address_key_patterns("a b@gmail.com") is None


def test_plus_tag_and_dot_variants_at_a_public_provider_share_one_daily_budget(db, llm_answer, monkeypatch):
    """Retest-3 bypass (a): spammer+0@gmail.com ... spammer+5@gmail.com each
    got a fresh budget (6 of 6 calls with per_day=2). One mailbox, one
    budget now, whatever the spelling; another Gmail user is unaffected."""
    _budget_settings(monkeypatch, rfp_email_ingestion_classify_budget_per_sender_per_day=2)
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""})
    recent = ingest._iso(ingest._now() - timedelta(hours=1))
    spellings = ["spammer+0@gmail.com", "spammer+1@gmail.com", "s.p.a.m.m.e.r@gmail.com",
                 "spam.mer+x@googlemail.com", "spammer@gmail.com", "SPAMMER+5@gmail.com".lower()]
    for i, address in enumerate(spellings):
        _pending_classify(db, f"p{i}", address, received_at=recent)
    _pending_classify(db, "gc", "newgc.pm@gmail.com", received_at=recent)
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats())
    assert len(_classify_calls(calls)) == 3            # two for the spammer, one for the GC
    deferred = [r for r in db.tables["rfp_emails"] if r["status"] == "classify"]
    assert len(deferred) == 4
    assert all("spammer@gmail.com" in r["last_error"] for r in deferred)
    assert _row(db, "gc")["status"] == "flagged_unauthorized"


def test_address_key_count_pages_past_the_row_cap(db, monkeypatch):
    """The loose pattern hits are read in pages: with the page size at 2 and
    five classified rows, the count is still five."""
    monkeypatch.setattr(ingest, "_PAGE", 2)
    recent = ingest._iso(ingest._now() - timedelta(hours=1))
    for i in range(5):
        _classified(db, f"c{i}", f"spammer+{i}@gmail.com", recent)
    _classified(db, "other", "spammer.other@gmail.com", recent)   # loose hit, exact miss
    since = ingest._iso(ingest._now() - timedelta(days=1))
    assert ingest._classify_calls_for_key(db, "address", "spammer@gmail.com", since) == (5, recent)


def test_an_address_key_the_pages_cannot_cover_is_over_budget(db, monkeypatch):
    monkeypatch.setattr(ingest, "_PAGE", 1)
    monkeypatch.setattr(ingest, "_BUDGET_MAX_PAGES", 2)
    recent = ingest._iso(ingest._now() - timedelta(hours=1))
    for i in range(3):
        _classified(db, f"c{i}", f"spammer+{i}@gmail.com", recent)
    since = ingest._iso(ingest._now() - timedelta(days=1))
    assert ingest._classify_calls_for_key(db, "address", "spammer@gmail.com", since) is None


def test_an_unsafe_address_key_waits_without_a_call(db, llm_answer, monkeypatch):
    _budget_settings(monkeypatch, rfp_email_ingestion_classify_budget_per_sender_per_day=2)
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""})
    recent = ingest._iso(ingest._now() - timedelta(hours=1))
    _pending_classify(db, "odd", "a%b@gmail.com", received_at=recent)
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats())
    assert _classify_calls(calls) == []
    assert _row(db, "odd")["status"] == "classify" and _row(db, "odd")["next_attempt_at"]


def test_a_public_suffix_carries_one_larger_budget_as_a_whole(db, llm_answer, monkeypatch):
    """Retest-3 bypass (b): the owner of a suffix-shaped domain (co.de) got
    a fresh budget per a<i>.co.de (6 of 6 calls with per_day=2). The suffix
    as a whole is now capped at the multiplier times the per-key budget."""
    _budget_settings(monkeypatch, rfp_email_ingestion_classify_budget_per_sender_per_day=2)
    monkeypatch.setattr(ingest, "_SUFFIX_BUDGET_MULTIPLIER", 2)
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""})
    recent = ingest._iso(ingest._now() - timedelta(hours=1))
    for i in range(6):
        _pending_classify(db, f"c{i}", f"x@a{i}.co.de", received_at=recent)
    _pending_classify(db, "other", "pm@unrelated.example", received_at=recent)
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats())
    assert len(_classify_calls(calls)) == 5            # 2 x 2 under co.de, plus the unrelated sender
    deferred = [r for r in db.tables["rfp_emails"] if r["status"] == "classify"]
    assert len(deferred) == 2
    assert all("co.de reached its classify budget (4 calls" in r["last_error"] for r in deferred)


def test_public_provider_senders_are_budgeted_per_address_not_per_provider(db, llm_answer, monkeypatch):
    """One free Gmail account over budget must not defer a legitimate new
    GC contact who also writes from Gmail."""
    _budget_settings(monkeypatch, rfp_email_ingestion_classify_budget_per_sender_per_day=1)
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""})
    recent = ingest._iso(ingest._now() - timedelta(hours=1))
    _classified(db, "spent", "spammer@gmail.com", recent)
    _pending_classify(db, "spam", "spammer@gmail.com", received_at=recent)
    _pending_classify(db, "gc", "newgc.pm@gmail.com", received_at=recent)
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats())
    assert len(_classify_calls(calls)) == 1
    spam = _row(db, "spam")
    assert spam["status"] == "classify" and "spammer@gmail.com" in spam["last_error"]
    assert _row(db, "gc")["status"] == "flagged_unauthorized"      # judged, not deferred
    assert _row(db, "gc")["classified_at"]


# ── Rule release ──────────────────────────────────────────────────────────────


def test_adding_a_rule_releases_rows_the_budget_deferred_at_classify(db):
    """Doc 3.5: a deferred row is picked up at once when a rule for its sender
    is added. The learn-back clears the wait (and the note); a waiting row
    from another sender keeps its wait."""
    recent = ingest._iso(ingest._now() - timedelta(hours=2))
    later = ingest._iso(ingest._now() + timedelta(hours=20))
    _pending_classify(db, "d1", "pm@newgc.example", received_at=recent,
                      next_attempt_at=later, last_error="Deferred: newgc.example reached its classify budget")
    _pending_classify(db, "d2", "bids@sub.newgc.example", received_at=recent,
                      next_attempt_at=later, last_error="Deferred: budget")
    _pending_classify(db, "keep", "who@other.example", received_at=recent,
                      next_attempt_at=later, last_error="Deferred: budget")
    rule = {"id": "r-new", "kind": "domain", "value": "newgc.example", "method": "general", "locked": False}
    db.tables["rfp_authorized_senders"].append(rule)
    ingest.rescan_after_rule_added(db, rule)
    for email_id in ("d1", "d2"):
        row = _row(db, email_id)
        assert row["status"] == "classify", email_id
        assert row["next_attempt_at"] is None and row["last_error"] is None, email_id
    keep = _row(db, "keep")
    assert keep["next_attempt_at"] == later and keep["last_error"] == "Deferred: budget"


def _cap_unpaged_selects(monkeypatch, cap):
    """PostgREST's max-rows: a select without a range gets `cap` rows at most."""
    original = tri._Query.execute

    def execute(self):
        res = original(self)
        if self._op == "select" and self._range is None and self._limit is None:
            res.data = res.data[:cap]
        return res
    monkeypatch.setattr(tri._Query, "execute", execute)
    monkeypatch.setattr(ingest, "_PAGE", cap)


def test_release_classify_waits_pages_past_the_row_cap_and_filters_by_sender(db, monkeypatch):
    """Retest-3 bypass (c): one unpaged select under a flood of other
    senders' waiting rows never reached a legitimate sender's rows. The
    rule is now applied server-side and the hits are paged."""
    _cap_unpaged_selects(monkeypatch, 2)
    later = ingest._iso(ingest._now() + timedelta(hours=20))
    for i in range(3):   # the flood, received first
        _pending_classify(db, f"flood{i}", f"x@a{i}.evil.example",
                          received_at=ingest._iso(ingest._now() - timedelta(hours=5)),
                          next_attempt_at=later, last_error="Deferred: budget")
    for i in range(5):   # the GC, received later: past the cap, and past the first page
        _pending_classify(db, f"gc{i}", f"pm{i}@sub.newgc.example",
                          received_at=ingest._iso(ingest._now() - timedelta(hours=1)),
                          next_attempt_at=later, last_error="Deferred: budget")
    rule = {"id": "r", "kind": "domain", "value": "newgc.example", "method": "general"}
    since = ingest._iso(ingest._now() - timedelta(days=ingest._RESCAN_DAYS))
    assert ingest.release_classify_waits_for_rule(db, rule, since) == 5
    assert all(_row(db, f"gc{i}")["next_attempt_at"] is None for i in range(5))
    assert all(_row(db, f"flood{i}")["next_attempt_at"] == later for i in range(3))


def test_rescan_after_rule_added_pages_past_the_row_cap(db, monkeypatch):
    _cap_unpaged_selects(monkeypatch, 2)
    old = ingest._iso(ingest._now() - timedelta(hours=5))
    for i in range(3):
        _seed(db, _email(id=f"flood{i}", internet_message_id=f"flood{i}@x",
                         from_address=f"x@a{i}.evil.example", status="flagged_unauthorized",
                         received_at=old))
    for i in range(5):
        _seed(db, _email(id=f"gc{i}", internet_message_id=f"gc{i}@x",
                         from_address="pm@newgc.example", status="flagged_unauthorized",
                         received_at=ingest._iso(ingest._now() - timedelta(hours=1))))
    rule = {"id": "r-new", "kind": "address", "value": "pm@newgc.example", "method": "general", "locked": False}
    db.tables["rfp_authorized_senders"].append(rule)
    assert ingest.rescan_after_rule_added(db, rule) == 5
    assert all(_row(db, f"gc{i}")["status"] != "flagged_unauthorized" for i in range(5))
    assert all(_row(db, f"flood{i}")["status"] == "flagged_unauthorized" for i in range(3))


def test_rows_for_rule_ignores_a_malformed_rule(db):
    since = ingest._iso(ingest._now() - timedelta(days=1))
    assert ingest._rows_for_rule(db, {"kind": "domain", "value": "x%"}, status="classify",
                                 since_iso=since, columns="id") == []
    assert ingest._rows_for_rule(db, {"kind": "other", "value": "a"}, status="classify",
                                 since_iso=since, columns="id") == []


def test_release_classify_waits_returns_the_count_and_skips_rows_outside_the_window(db):
    recent = ingest._iso(ingest._now() - timedelta(days=1))
    old = ingest._iso(ingest._now() - timedelta(days=30))
    later = ingest._iso(ingest._now() + timedelta(hours=1))
    _pending_classify(db, "in", "pm@newgc.example", received_at=recent, next_attempt_at=later)
    _pending_classify(db, "out", "pm@newgc.example", received_at=old, next_attempt_at=later)
    _pending_classify(db, "fresh", "pm@newgc.example", received_at=recent)   # not waiting
    rule = {"id": "r", "kind": "domain", "value": "newgc.example", "method": "general"}
    since = ingest._iso(ingest._now() - timedelta(days=ingest._RESCAN_DAYS))
    assert ingest.release_classify_waits_for_rule(db, rule, since) == 1
    assert _row(db, "in")["next_attempt_at"] is None
    assert _row(db, "out")["next_attempt_at"] == later
    assert _row(db, "fresh")["next_attempt_at"] is None


# ── Window 2 reservation ──────────────────────────────────────────────────────


def test_fresh_mail_above_a_batch_per_tick_cannot_starve_waited_rows(db, llm_answer, monkeypatch):
    """More never-waited rows than the batch holds: the second window still
    gets its reserved slice, so a due retry is swept this tick."""
    _budget_settings(monkeypatch)
    monkeypatch.setattr(ingest, "_SWEEP_BATCH", 5)        # reserve = 5 // 5 = 1
    llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""})
    old = ingest._iso(ingest._now() - timedelta(hours=3))
    due = ingest._iso(ingest._now() - timedelta(minutes=1))
    for i in range(6):
        _pending_classify(db, f"f{i}", f"who{i}@stranger{i}.example", received_at=old)
    # A genuine GC retry that waited and is due now.
    _pending_classify(db, "retry", "pm@gc.example",
                      received_at=ingest._iso(ingest._now() - timedelta(minutes=30)),
                      next_attempt_at=due, last_error="Deferred: model away")
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats())
    assert _row(db, "retry")["status"] != "classify"
    # Four fresh rows went (the batch minus the reserved slot); two remain.
    still_fresh = [r for r in db.tables["rfp_emails"]
                   if r["status"] == "classify" and r["next_attempt_at"] is None]
    assert len(still_fresh) == 2


def test_the_reserve_is_not_held_when_nothing_waited_is_due(db, llm_answer, monkeypatch):
    _budget_settings(monkeypatch)
    monkeypatch.setattr(ingest, "_SWEEP_BATCH", 5)
    llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""})
    old = ingest._iso(ingest._now() - timedelta(hours=3))
    for i in range(6):
        _pending_classify(db, f"f{i}", f"who{i}@stranger{i}.example", received_at=old)
    _pending_classify(db, "future", "pm@gc.example", received_at=old,
                      next_attempt_at=ingest._iso(ingest._now() + timedelta(hours=1)))
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats())
    still_fresh = [r for r in db.tables["rfp_emails"]
                   if r["status"] == "classify" and r["next_attempt_at"] is None]
    assert len(still_fresh) == 1                          # the whole batch went to fresh mail
    assert _row(db, "future")["status"] == "classify"     # not due, untouched


def test_the_reserve_scales_with_the_batch():
    assert min(ingest._SWEEP_WINDOW2_RESERVE, ingest._SWEEP_BATCH // 5) == 40
    assert ingest._SWEEP_WINDOW2_RESERVE <= ingest._SWEEP_BATCH // 5


# ── Reviewer visibility ───────────────────────────────────────────────────────


def test_detail_select_exposes_the_alignment_domains_and_classified_at():
    for col in ("auth_spf_domain", "auth_dkim_domain", "auth_compauth", "classified_at"):
        assert col in rr._DETAIL_SELECT.replace(" ", "").split(","), col


# ── Finding 18 follow-up: callback limiter bounds ─────────────────────────────


@pytest.mark.parametrize("value", [0, -1, -30])
def test_bc_callback_rate_limit_below_one_is_refused_at_boot(value):
    with pytest.raises(ValidationError, match="RFP_BC_CALLBACK_RATE_LIMIT_PER_MIN"):
        Settings(_env_file=None, rfp_bc_callback_rate_limit_per_min=value)


def test_bc_callback_rate_limit_of_one_boots():
    assert Settings(_env_file=None, rfp_bc_callback_rate_limit_per_min=1).rfp_bc_callback_rate_limit_per_min == 1
