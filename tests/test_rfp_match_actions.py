"""RFP Matching (docs/RFP_MATCHING.md section 9, the Pipeline bullet): the
extract and match steps walked to every outcome, the per-feature LLM gate,
lease renewal, merge ordering and its crash resumes, unmerge and its
refusals, the ordinary-Remove guard, acknowledge, reopen, the sibling
follower cases and the startup backfill. Runs against the in-memory fake in
tests/test_rfp_email_ingest.py (fakes first: not_ on every filter, nested
and(...) or-groups, 23505 on project_gcs, delete, and the
remove_project_gc_unless_sent rpc) with the LLM stubbed per feature.
"""

from datetime import timedelta
from itertools import permutations

import pytest

from app.services import (
    llm,
    proposal_send,
    rfp_create as rc,
    rfp_email_ingest as ingest,
    rfp_match,
)
from tests import test_rfp_email_ingest as base
from tests.test_rfp_email_ingest import (
    EXO_PASS,
    _calls_for,
    _email,
    _row,
    _seed,
    _settings,
    _Snapshot,
)

# The shared fixtures, registered under their own names in this module.
db = base.db
llm_answer = base.llm_answer
_defaults = base._defaults

NOW = ingest._now()
BID_AT = ingest._iso((NOW + timedelta(days=5)).replace(microsecond=0))
DUE_DATE = (NOW + timedelta(days=5)).astimezone(ingest._COMPANY_TZ).date().isoformat()
OLD_BID_AT = ingest._iso(NOW - timedelta(days=200))   # rebid band, outside the window


def _extract(name="Riverside Plaza", gc_name=None, due=None, notes=None):
    return {
        "project_name": name,
        "gc_name": gc_name,
        "bid_due": due or {"date": None, "time": None, "timezone": None},
        "bid_notes": notes,
        "reasoning": "from the subject",
    }


def _verdicts(*entries):
    return {"verdicts": [
        {"index": i, "verdict": v, "confidence": c, "reasoning": "r"} for i, v, c in entries
    ]}


SAME = _verdicts((0, "same", 0.95))


def _project(db, pid, name, *, number=None, bid_at=BID_AT, actual=None, stage="rfq",
             gcs=(), bid_notes=None, abandoned_at=None):
    """A projects row with its GC links embedded (the shape the bundle query
    returns) and mirrored in project_gcs (what _gc_link and the rpc read)."""
    row = {
        "id": pid, "name": name, "number": number, "current_stage": stage,
        "internal_bid_at": bid_at, "actual_bid_at": actual, "bid_notes": bid_notes,
        "abandoned_at": abandoned_at, "project_gcs": [],
    }
    for gc_id in gcs:
        link = {"id": f"link-{pid}-{gc_id}", "project_id": pid, "gc_id": gc_id,
                "needs_by": None, "rfp_match_id": None}
        db.tables["project_gcs"].append(dict(link))
        gc = next((g for g in db.tables["general_contractors"] if g["id"] == gc_id), {})
        row["project_gcs"].append({**link, "general_contractors": {"name": gc.get("name")}})
    db.tables["projects"].append(row)
    return row


def _at_extract(db, **over):
    base = dict(
        status="extract", body_text="Please bid Riverside Plaza. Bids due Friday.",
        keyword_hits=["bid"], auth_raw=EXO_PASS, auth_verdict="pass", auth_dmarc="pass",
        auth_compauth="pass", llm_answer="yes", llm_confidence=0.99,
        authorization_kind="gc_domain", authorization_rule_id=None,
        invitation_method="organic",
    )
    base.update(over)
    return _seed(db, _email(**base))


def _at_match(db, **over):
    base = dict(
        extracted_project_name="Riverside Plaza", extracted_gc_name=None,
        extracted_bid_due_at=None, extracted_bid_due_has_time=False,
        extracted_bid_notes=None, extracted_at="2026-09-09T10:01:00+00:00",
        # A row past GC resolution (what the human actions re-read).
        resolved_gc_id="gc-1", resolved_gc_contact_id="c1", gc_match_kind="contact",
        gc_match_score=None,
    )
    base["status"] = "match"
    base.update(over)
    return _at_extract(db, **base)


def _walk(db, email_id="e1", **kw):
    stats = kw.pop("stats", None) or ingest._TickStats()
    outcome = ingest._process_email(db, dict(_row(db, email_id)), stats=stats, **kw)
    return outcome, stats


def _links(db, project_id="p1"):
    return [link for link in db.tables["project_gcs"] if link["project_id"] == project_id]


def _matches(db):
    return db.tables["rfp_project_matches"]


def _audits(db, action):
    return [a for a in db.tables.get("audit_log", []) if a["action"] == action]


def _auto_merge(monkeypatch, **over):
    monkeypatch.setattr(ingest, "get_settings",
                        lambda: _settings(rfp_match_auto_merge_enabled=True, **over))


# ── Walks to each outcome ──────────────────────────────────────────────────────


def test_walk_to_merged(db, llm_answer, monkeypatch):
    _auto_merge(monkeypatch)
    calls = llm_answer(None, extract=_extract(due={"date": DUE_DATE, "time": "14:00",
                                                   "timezone": "PT"}), match=SAME)
    _project(db, "p1", "Riverside Plaza", number="26.9.7001")
    _at_extract(db)
    outcome, stats = _walk(db)
    row = _row(db)
    assert outcome is None
    assert row["status"] == "merged" and row["flag_reason"] is None
    assert row["decided_at_step"] == "match" and row["matched_at"]
    assert row["match_project_id"] == "p1" and row["match_score"] == 1.0
    assert row["match_llm_model"] == "test-model"
    assert row["match_llm_prompt_version"] == rfp_match.MATCH_PROMPT_VERSION
    assert row["match_candidates"][0]["verdict"] == "same"
    assert row["extracted_bid_due_has_time"] is True
    assert [c["feature"] for c in calls] == ["rfp_extract", "rfp_match"]
    # The link the merge inserted carries the match id and the Pacific due date.
    (link,) = _links(db)
    assert link["gc_id"] == "gc-1" and link["needs_by"] == DUE_DATE
    (match,) = _matches(db)
    assert link["rfp_match_id"] == match["id"]
    assert match["kind"] == "merged" and match["gc_added"] is True
    assert match["project_gc_id"] == link["id"] and match["decided_by"] is None
    assert match["candidate_rank"] == 1 and match["score"] == 1.0
    assert match["breakdown"]["verdict"] == "same" and "project_id" not in match["breakdown"]
    assert match["sender_address"] == "pm@gc.example" and match["gc_match_kind"] == "contact"
    assert match["authorization_kind"] == "gc_domain" and match["auth_dmarc"] == "pass"
    # The organic contact was selected on the link.
    (selected,) = db.tables["project_gc_contacts"]
    assert selected["project_gc_id"] == link["id"] and selected["gc_contact_id"] == "c1"
    assert match["contact_selected_id"] == "c1"
    assert stats.merged_new == 1 and stats.merged_project_ids == ["p1"]
    assert _audits(db, "rfp_match.merge")[0]["actor_id"] is None
    # One bell row per tick, deduped while unread; a duplicate tick adds none.
    ingest._notify_review_queue(db, stats)
    ingest._notify_review_queue(db, stats)
    rows = [n for n in db.tables["notifications"] if n["type"] == "rfp_match.merged"]
    assert len(rows) == 1 and rows[0]["metadata"] == {"merged": 1, "project_ids": ["p1"]}


def test_walk_to_duplicate_when_gc_already_on_project(db, llm_answer, monkeypatch):
    _auto_merge(monkeypatch)
    llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza", gcs=["gc-1"])
    _at_extract(db)
    _, stats = _walk(db)
    row = _row(db)
    assert row["status"] == "duplicate" and row["flag_reason"] is None
    assert row["match_project_id"] == "p1"
    assert len(_links(db)) == 1 and _links(db)[0]["rfp_match_id"] is None  # nothing added
    (match,) = _matches(db)
    assert match["kind"] == "duplicate" and match["gc_added"] is False
    assert stats.merged_new == 0
    assert _audits(db, "rfp_match.duplicate")
    ingest._notify_review_queue(db, stats)
    assert db.tables["notifications"] == []


def test_second_email_same_tick_sees_the_merged_gc(db, llm_answer, monkeypatch):
    """After a merge inside the sweep the bundle carries the new link, so a
    second email from the same GC routes to duplicate without a re-read."""
    _auto_merge(monkeypatch)
    llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, id="e1", subject="A")
    _at_match(db, id="e2", internet_message_id="m2", subject="B",
              received_at="2026-09-09T11:00:00+00:00")
    ingest.process_pending(db, lease_key=None)
    assert _row(db, "e1")["status"] == "merged"
    assert _row(db, "e2")["status"] == "duplicate"
    assert len(_links(db)) == 1


@pytest.mark.parametrize("case, expected", [
    ("auto_off", "match_confident"),
    ("unverified_dmarc", "match_sender_unverified"),
    ("override", "match_sender_unverified"),
    ("gc_unresolved", "match_gc_unresolved"),
])
def test_confident_but_parked_for_a_person(db, llm_answer, monkeypatch, case, expected):
    if case != "auto_off":
        _auto_merge(monkeypatch)
    gc_name = "Zzz Unknown Partners" if case == "gc_unresolved" else "GC Example Builders"
    llm_answer(None, extract=_extract(gc_name=gc_name), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    over = {}
    if case == "unverified_dmarc":
        over = dict(auth_dmarc="none", auth_compauth="fail")
    if case == "override":
        over = dict(authorization_kind="override", invitation_method="nonorganic")
    if case == "gc_unresolved":
        # A platform sender: no contact, and the extracted GC name matches nothing.
        over = dict(from_address="noreply@us02.procoretech.com", authorization_kind="domain",
                    authorization_rule_id="r-procore", invitation_method="procore")
    _at_extract(db, **over)
    _, stats = _walk(db)
    row = _row(db)
    assert row["status"] == "review_match" and row["flag_reason"] == expected
    assert row["decided_at_step"] == "match" and row["match_project_id"] == "p1"
    assert row["match_score"] == 1.0 and row["last_error"] is None
    assert stats.match_review_new == 1 and stats.merged_new == 0
    assert _links(db) == [] and _matches(db) == []
    if case == "gc_unresolved":
        assert row["resolved_gc_id"] is None and len(row["gc_candidates"]) == 2
    else:
        assert row["resolved_gc_id"] == "gc-1"
    ingest._notify_review_queue(db, stats)
    assert "1 matches to review" in db.tables["notifications"][0]["message"]
    assert db.tables["notifications"][0]["metadata"]["matches"] == 1


def test_ambiguous_pair_goes_to_review_never_merges(db, llm_answer, monkeypatch):
    _auto_merge(monkeypatch)
    llm_answer(None, extract=_extract(), match=_verdicts((0, "same", 0.95), (1, "same", 0.95)))
    _project(db, "p1", "Riverside Plaza North")
    _project(db, "p2", "Riverside Plaza South")
    _at_extract(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_ambiguous"
    assert {c["project_id"] for c in row["match_candidates"]} == {"p1", "p2"}
    assert _links(db) == [] and _matches(db) == []


def test_uncertain_verdict_goes_to_review(db, llm_answer, monkeypatch):
    _auto_merge(monkeypatch)
    llm_answer(None, extract=_extract(), match=_verdicts((0, "unsure", 0.5)))
    _project(db, "p1", "Riverside Plaza")
    _at_extract(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_uncertain"
    assert row["match_candidates"][0]["verdict"] == "unsure"


def test_all_different_lands_at_done_with_rebid_lookup(db, llm_answer):
    calls = llm_answer(None, extract=_extract(), match=_verdicts((0, "different", 0.9)))
    _project(db, "p1", "Riverside Plaza")
    _project(db, "p-old", "Riverside Plaza", bid_at=OLD_BID_AT)   # rebid band
    _at_extract(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "done" and row["flag_reason"] == "all_different"
    assert row["match_project_id"] is None and row["match_score"] is None
    assert row["match_candidates"][0]["verdict"] == "different"
    assert row["possible_rebid_project_id"] == "p-old" and row["possible_rebid_score"] == 1.0
    assert len(_calls_for(calls, "rfp_match")) == 1


def test_no_candidate_when_nothing_clears_review_threshold(db, llm_answer):
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Sunrise Elementary Modernization")
    _project(db, "p-closed", "Riverside Plaza", stage="declined")
    _project(db, "p-gone", "Riverside Plaza", abandoned_at="2026-01-01T00:00:00+00:00")
    _at_extract(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "done" and row["flag_reason"] == "no_candidate"
    assert [c["project_id"] for c in row["match_candidates"]] == ["p1"]  # near miss kept
    assert _calls_for(calls, "rfp_match") == []


def test_date_far_outside_tolerance_is_review_only(db, llm_answer, monkeypatch):
    """A closest date outside the tolerance blocks the confident rule even
    with a perfect name and a confident 'same'."""
    _auto_merge(monkeypatch)
    far = (NOW + timedelta(days=15)).date().isoformat()
    llm_answer(None, extract=_extract(due={"date": far, "time": None, "timezone": None}), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _at_extract(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_uncertain"
    assert row["match_candidates"][0]["breakdown"]["date"] == 0.5


# ── The no-name path ───────────────────────────────────────────────────────────


def test_null_name_lands_at_done_after_gc_resolution_without_a_match_call(db, llm_answer):
    calls = llm_answer(None, extract=_extract(name=None, gc_name="GC Example Builders"))
    _project(db, "p1", "Riverside Plaza")
    _at_extract(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "done" and row["flag_reason"] == "no_project_name"
    # Parked through the create step (docs/RFP_CREATE.md 3), which sees no
    # name and drains under its own stamp; the match's reason stays.
    assert row["decided_at_step"] == "create" and row["matched_at"]
    assert row["resolved_gc_id"] == "gc-1" and row["gc_match_kind"] == "contact"
    assert row["match_candidates"] == [] and row["match_project_id"] is None
    assert row["match_weights"]["scorer_version"] == rfp_match.SCORER_VERSION
    assert [c["feature"] for c in calls] == ["rfp_extract"]


def test_all_stop_token_name_is_treated_as_no_name(db, llm_answer):
    llm_answer(None, extract=_extract(name="Invitation to Bid"))
    _at_extract(db)
    _walk(db)
    assert _row(db)["flag_reason"] == "no_project_name"


# ── Crash resume at extract and match ──────────────────────────────────────────


def test_crash_resume_at_extract_and_at_match(db, llm_answer, monkeypatch):
    _auto_merge(monkeypatch)
    llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _at_extract(db, id="e1", subject="A")
    _at_match(db, id="e2", internet_message_id="m2", subject="B")
    ingest.process_pending(db, lease_key=None)
    assert _row(db, "e1")["status"] == "merged"
    assert _row(db, "e2")["status"] == "duplicate"   # e1's link was in the bundle by then


# ── Reference bundle paging ────────────────────────────────────────────────────


def test_bundle_queries_order_on_id_as_the_paging_tiebreaker(db, monkeypatch):
    """_page_all drains by range: an order that ends on a non-unique column
    (internal_bid_at, name, created_at) can repeat or skip a row that ties
    across a page boundary, so every factory ends its order on id."""
    seen = []
    real_execute = base._Query.execute

    def record(self):
        if self._op == "select" and self._range is not None:
            seen.append((self.table, list(self._order)))
        return real_execute(self)
    monkeypatch.setattr(base._Query, "execute", record)
    ingest._reference_bundle(db, ingest._SweepState(), _settings())
    orders = {table: order for table, order in seen}
    assert orders["general_contractors"] == [("name", False), ("id", False)]
    assert orders["gc_contacts"] == [("created_at", False), ("id", False)]
    assert orders["projects"] == [("internal_bid_at", True), ("id", False)]


def test_page_all_drains_ties_across_page_boundaries(db, monkeypatch):
    monkeypatch.setattr(ingest, "_PAGE", 2)
    for i in range(5):
        _project(db, f"p{i}", f"Project {i}", bid_at=BID_AT)   # all tie on the sort key
    bundle = ingest._reference_bundle(db, ingest._SweepState(), _settings())
    assert sorted(p["id"] for p in bundle["projects"]) == [f"p{i}" for i in range(5)]


# ── Per-feature gate ───────────────────────────────────────────────────────────


def test_match_model_missing_still_advances_a_classify_row(db, llm_answer, monkeypatch):
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""},
                       extract=_extract(), match=SAME)
    monkeypatch.setattr(ingest.llm_health, "cached", lambda settings=None, force=False: _Snapshot(
        per_feature={"rfp_match": ("model_missing", "no match model")}))
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, id="m1", subject="A", received_at="2026-09-09T09:00:00+00:00")
    _seed(db, _email(id="c1", internet_message_id="m-c1", subject="Invitation to Bid: B",
                     received_at="2026-09-09T09:01:00+00:00"))
    ingest.process_pending(db, lease_key=None)
    m1, c1 = _row(db, "m1"), _row(db, "c1")
    assert m1["status"] == "match" and m1["attempts"] == 0
    assert m1["next_attempt_at"] is not None and m1["last_error"] == "no match model"
    # The classify row walked classify and extract this tick and stopped in
    # front of match, stamped with the same wait (not left to read "stalled").
    assert c1["status"] == "match" and c1["attempts"] == 0
    assert c1["next_attempt_at"] is not None and "come back" in c1["last_error"]
    assert [c["feature"] for c in calls] == ["rfp_classify", "rfp_extract"]


def test_provider_down_at_match_gates_all_three(db, llm_answer, monkeypatch):
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""},
                       extract=_extract(), match=SAME)
    monkeypatch.setattr(ingest.llm_health, "cached", lambda settings=None, force=False: _Snapshot(
        per_feature={"rfp_match": ("provider_down", "box off")}))
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, id="m1", subject="A", received_at="2026-09-09T09:00:00+00:00")
    _at_extract(db, id="x1", internet_message_id="m-x1", subject="B",
                received_at="2026-09-09T09:01:00+00:00")
    _seed(db, _email(id="c1", internet_message_id="m-c1", subject="Invitation to Bid: C",
                     received_at="2026-09-09T09:02:00+00:00"))
    ingest.process_pending(db, lease_key=None)
    assert _row(db, "m1")["next_attempt_at"] is not None
    # Skipped behind the provider outage: stamped as waiting, no attempt spent.
    assert _row(db, "x1")["status"] == "extract" and _row(db, "x1")["next_attempt_at"] is not None
    assert _row(db, "c1")["status"] == "classify" and _row(db, "c1")["next_attempt_at"] is not None
    assert _row(db, "x1")["attempts"] == 0 and _row(db, "c1")["attempts"] == 0
    assert calls == []


def test_feature_scoped_outage_gates_only_that_feature(monkeypatch):
    s = _settings()
    assert ingest._gated_features("rfp_extract", "feature", s) == {"rfp_extract"}
    assert ingest._gated_features("rfp_extract", "provider", s) == set(ingest.LLM_FEATURES)
    routes = {"rfp_classify": "openai", "rfp_extract": "self_hosted", "rfp_match": "self_hosted"}
    monkeypatch.setattr(ingest.llm, "resolve", lambda f, settings=None: type(
        "R", (), {"provider": routes[f]})())
    assert ingest._gated_features("rfp_match", "provider", s) == {"rfp_extract", "rfp_match"}


@pytest.mark.parametrize("status", ["extract", "match"])
def test_wait_at_extract_and_match_pushes_next_attempt_without_spending(db, llm_answer, status):
    llm_answer(None, extract=llm.SelfHostedUnreachable("down"),
               match=llm.SelfHostedUnreachable("down"))
    _project(db, "p1", "Riverside Plaza")
    seed = _at_extract if status == "extract" else _at_match
    seed(db, attempts=4)
    outcome, _ = _walk(db)
    row = _row(db)
    assert outcome == (f"rfp_{status}", "provider")
    assert row["status"] == status and row["attempts"] == 4
    assert row["next_attempt_at"] is not None and row["last_error"]


@pytest.mark.parametrize("status", ["extract", "match"])
def test_snapshot_gate_at_extract_and_match(db, llm_answer, monkeypatch, status):
    calls = llm_answer(None, extract=_extract(), match=SAME)
    monkeypatch.setattr(ingest.llm_health, "cached", lambda settings=None, force=False: _Snapshot(
        per_feature={f"rfp_{status}": ("unconfigured", "no model")}))
    _project(db, "p1", "Riverside Plaza")
    (_at_extract if status == "extract" else _at_match)(db)
    outcome, _ = _walk(db)
    assert outcome == (f"rfp_{status}", "feature")
    assert _row(db)["status"] == status and _row(db)["attempts"] == 0
    assert _calls_for(calls, f"rfp_{status}") == []


def test_attempts_spent_at_classify_do_not_carry_into_extract(db, llm_answer):
    class ServerError(Exception):
        status_code = 500

    llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""}, extract=ServerError("boom"))
    _seed(db, _email(status="classify", body_text="please bid", keyword_hits=["bid"],
                     auth_raw=EXO_PASS, auth_verdict="pass", attempts=5))
    _walk(db)
    row = _row(db)
    assert row["status"] == "extract" and row["attempts"] == 1   # not 6
    assert row["next_attempt_at"] is not None


def test_unusable_extract_retries_once_then_advances_with_nulls(db, llm_answer):
    llm_answer(None, extract=llm.LlmBadOutput("Model response was not valid JSON."))
    _at_extract(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "extract" and row["attempts"] == 1
    assert row["next_attempt_at"] is not None and row.get("extracted_at") is None
    row["next_attempt_at"] = None
    _walk(db)
    row = _row(db)
    # Twice: all-null facts, extracted_at stamped, last_error KEPT, and the
    # no-name path at match parks it at done without clearing last_error.
    assert row["status"] == "done" and row["flag_reason"] == "no_project_name"
    assert row["extracted_project_name"] is None and row["extracted_at"]
    assert row["extract_model"] == "test-model" and row["attempts"] == 0
    assert row["last_error"] and "JSON" in row["last_error"]


def test_unusable_match_twice_routes_to_review_with_deterministic_ranking(db, llm_answer):
    llm_answer(None, extract=_extract(), match=llm.LlmBadOutput("not json"))
    _project(db, "p1", "Riverside Plaza")
    _at_match(db)
    _walk(db)
    row = _row(db)
    assert row["status"] == "match" and row["attempts"] == 1
    row["next_attempt_at"] = None
    _walk(db)
    row = _row(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_llm_unusable"
    assert row["match_project_id"] == "p1" and row["match_candidates"][0]["verdict"] is None
    assert row["match_llm_model"] == "test-model" and row["last_error"]


def test_transient_failure_at_match_fails_at_the_cap(db, llm_answer):
    class ServerError(Exception):
        status_code = 503

    llm_answer(None, extract=_extract(), match=ServerError("boom"))
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, attempts=7)
    _walk(db)
    row = _row(db)
    assert row["status"] == "failed" and row["flag_reason"] == "match_overloaded"
    assert row["decided_at_step"] == "match" and row["attempts"] == 8


def test_failure_after_the_match_call_spends_an_attempt_and_waits(db, llm_answer, monkeypatch):
    """A merge_email that raises something other than RfpMatchError (a
    ProposalSendError, a unique violation, a PostgREST error) after the LLM
    answered must not leave the row at match with attempts 0 and no
    next_attempt_at, or the model is called again every tick."""
    _auto_merge(monkeypatch)
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _at_match(db)

    def boom(*a, **k):
        raise RuntimeError("PostgREST: connection reset")
    monkeypatch.setattr(ingest, "merge_email", boom)
    outcome, stats = _walk(db)
    row = _row(db)
    assert outcome is None
    assert row["status"] == "match" and row["attempts"] == 1
    assert row["next_attempt_at"] is not None and "connection reset" in row["last_error"]
    assert stats.merged_new == 0 and _matches(db) == [] and _links(db) == []
    assert len(_calls_for(calls, "rfp_match")) == 1


def test_failure_after_the_extract_call_spends_an_attempt_and_waits(db, llm_answer, monkeypatch):
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _at_extract(db)

    def boom(*a, **k):
        raise RuntimeError("parse exploded")
    monkeypatch.setattr(ingest.rfp_match, "parse_extraction", boom)
    outcome, _ = _walk(db)
    row = _row(db)
    assert outcome is None
    assert row["status"] == "extract" and row["attempts"] == 1
    assert row["next_attempt_at"] is not None and "parse exploded" in row["last_error"]
    assert row.get("extracted_at") is None
    assert len(_calls_for(calls, "rfp_extract")) == 1


def test_failure_after_the_match_call_fails_at_the_cap(db, llm_answer, monkeypatch):
    _auto_merge(monkeypatch)
    llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, attempts=7)

    def boom(*a, **k):
        raise proposal_send.ProposalSendError("sending")
    monkeypatch.setattr(ingest, "merge_email", boom)
    _walk(db)
    row = _row(db)
    assert row["status"] == "failed" and row["flag_reason"] == "match_error"
    assert row["decided_at_step"] == "match" and row["attempts"] == 8


# ── Lease renewal before every LLM call ────────────────────────────────────────


def test_lease_renewed_before_each_llm_step_on_one_row(db, llm_answer, monkeypatch):
    llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""}, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    renewals = []
    monkeypatch.setattr(ingest, "_renew_lease", lambda sb, key: renewals.append(key) or True)
    _seed(db, _email(status="classify", body_text="please bid", keyword_hits=["bid"],
                     auth_raw=EXO_PASS, auth_verdict="pass"))
    ingest._sweep(db, lease_key="rfp-mail:lease", stats=ingest._TickStats())
    assert renewals == ["rfp-mail:lease"] * 3
    assert _row(db)["status"] == "review_match"


def test_failed_renewal_stops_the_sweep_with_the_row_at_its_step(db, llm_answer, monkeypatch):
    calls = llm_answer({"answer": "yes", "confidence": 0.99, "reasoning": ""}, extract=_extract())
    answers = iter([True, False])
    monkeypatch.setattr(ingest, "_renew_lease", lambda sb, key: next(answers))
    _seed(db, _email(status="classify", body_text="please bid", keyword_hits=["bid"],
                     auth_raw=EXO_PASS, auth_verdict="pass", received_at="2026-09-09T09:00:00+00:00"))
    _seed(db, _email(id="e2", internet_message_id="m2", received_at="2026-09-09T09:05:00+00:00"))
    ingest._sweep(db, lease_key="rfp-mail:lease", stats=ingest._TickStats())
    assert _row(db)["status"] == "extract" and _row(db)["next_attempt_at"] is None
    assert [c["feature"] for c in calls] == ["rfp_classify"]
    assert _row(db, "e2")["status"] == "received"   # the sweep returned


# ── Merge ordering and resume (3.6) ────────────────────────────────────────────


def _open_merge_row(db, email_id="e1", project_id="p1", gc_id="gc-1", **over):
    row = {"id": "mr-1", "rfp_email_id": email_id, "project_id": project_id, "gc_id": gc_id,
           "kind": "merged", "gc_added": False, "project_gc_id": None, "unmerged_at": None,
           "acknowledged_at": None, "decided_by": None, "decided_at": "2026-09-09T10:05:00+00:00",
           "score": 1.0, "candidate_rank": 1, "breakdown": {}, "candidates": [], "weights": {}}
    row.update(over)
    db.tables["rfp_project_matches"].append(row)
    return row


def test_resume_after_link_insert_before_stamp_is_merged_not_duplicate(db):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db)
    match = _open_merge_row(db)
    db.tables["project_gcs"].append({"id": "l-sys", "project_id": "p1", "gc_id": "gc-1",
                                     "needs_by": None, "rfp_match_id": "mr-1"})
    out = ingest.merge_email(db, "e1", "p1", "gc-1", None)
    assert out["status"] == "merged" and out["match_project_id"] == "p1"
    (row,) = _matches(db)
    assert row["id"] == match["id"] and row["kind"] == "merged"
    assert row["gc_added"] is True and row["project_gc_id"] == "l-sys"
    assert len(_links(db)) == 1
    assert _audits(db, "rfp_match.merge")[0]["args"][2]["resumed"] is True


def test_step_match_finishes_an_open_merge_instead_of_rescoring(db, llm_answer, monkeypatch):
    """A crash between merge steps 5 and 6 on the system path: the open
    merged row (gc_added true, link on p1) wins over a re-score that would
    now prefer p2; no model call, the row is resumed, the email lands at
    merged on p1 and p2 gets nothing."""
    _auto_merge(monkeypatch)
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _project(db, "p2", "Riverside Plaza Phase 2")   # a competing candidate
    _at_match(db, extracted_project_name="Riverside Plaza Phase 2")
    _open_merge_row(db, gc_added=True, project_gc_id="l-sys")
    db.tables["project_gcs"].append({"id": "l-sys", "project_id": "p1", "gc_id": "gc-1",
                                     "needs_by": None, "rfp_match_id": "mr-1"})
    outcome, stats = _walk(db)
    row = _row(db)
    assert outcome is None and row["status"] == "merged"
    assert row["match_project_id"] == "p1" and row["decided_at_step"] == "match"
    assert calls == []
    (match,) = _matches(db)
    assert match["id"] == "mr-1" and match["unmerged_at"] is None
    assert _links(db, "p1") and _links(db, "p2") == []
    assert _audits(db, "rfp_match.merge")[0]["args"][2]["resumed"] is True
    assert stats.merged_new == 1


def test_step_match_resume_refusal_parks_the_row(db, llm_answer, monkeypatch):
    """The open merge's project closed meanwhile: the resume is refused like
    any other system decision and the row waits for a person."""
    _auto_merge(monkeypatch)
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza", stage="declined")
    _at_match(db)
    _open_merge_row(db, gc_added=True, project_gc_id="l-sys")
    _walk(db)
    row = _row(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_uncertain"
    assert calls == []


def test_reject_refuses_while_an_open_merge_exists(db):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match")
    _open_merge_row(db, gc_added=True, project_gc_id="l-sys")
    db.tables["project_gcs"].append({"id": "l-sys", "project_id": "p1", "gc_id": "gc-1",
                                     "needs_by": None, "rfp_match_id": "mr-1"})
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.reject_match(db, "e1", "u1")
    assert exc.value.code == ingest.CODE_NOT_ACTIONABLE
    assert _row(db)["status"] == "review_match" and not _audits(db, "rfp_match.reject")
    assert len(_links(db)) == 1 and _matches(db)[0]["unmerged_at"] is None


def test_resume_with_gc_added_true_skips_straight_to_the_email(db):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db)
    _open_merge_row(db, gc_added=True, project_gc_id="l-sys")
    db.tables["project_gcs"].append({"id": "l-sys", "project_id": "p1", "gc_id": "gc-1",
                                     "needs_by": None, "rfp_match_id": "mr-1"})
    assert ingest.merge_email(db, "e1", "p1", "gc-1", None)["status"] == "merged"
    assert len(_links(db)) == 1 and len(_matches(db)) == 1


def test_human_add_before_the_link_insert_gives_duplicate(db):
    _project(db, "p1", "Riverside Plaza", gcs=["gc-1"])   # a person added the GC
    _at_match(db, status="review_match")
    out = ingest.merge_email(db, "e1", "p1", "gc-1", "u1")
    assert out["status"] == "duplicate" and out["match_review_decision"] == "duplicate"
    assert out["match_review_by"] == "u1" and out["decided_at_step"] == "review_match"
    (row,) = _matches(db)
    assert row["kind"] == "duplicate" and row["gc_added"] is False
    assert row["candidate_rank"] is not None or row["score"] is not None
    assert len(_links(db)) == 1 and _links(db)[0]["rfp_match_id"] is None
    (audit,) = _audits(db, "rfp_match.duplicate")
    assert audit["args"][2]["requested"] == "merge"


def test_lost_step_6_race_compensates(db, monkeypatch):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match")
    real_insert = ingest._insert_link

    def insert_then_lose(sb, project_id, gc_id, needs_by, match_id):
        link_id = real_insert(sb, project_id, gc_id, needs_by, match_id)
        _row(db)["status"] = "done"   # another reviewer clicked "not a match" first
        return link_id
    monkeypatch.setattr(ingest, "_insert_link", insert_then_lose)
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.merge_email(db, "e1", "p1", "gc-1", "u1")
    assert exc.value.code == ingest.CODE_NOT_ACTIONABLE
    assert _links(db) == [] and _matches(db) == []
    assert db.tables["project_gc_contacts"] == []
    assert _audits(db, "rfp_match.lost_race") and not _audits(db, "rfp_match.merge")
    assert _row(db)["status"] == "done"


def test_human_merge_scores_a_project_outside_the_candidate_list(db):
    _project(db, "p1", "Riverside Plaza")
    _project(db, "p2", "Sunrise Elementary")
    _at_match(db, status="review_match", match_project_id="p1", match_score=1.0,
              match_candidates=[{"project_id": "p1", "name": "Riverside Plaza", "number": None,
                                 "breakdown": {"total": 1.0, "name": 1.0}, "verdict": "same",
                                 "confidence": 0.9, "reasoning": "r"}])
    out = ingest.merge_email(db, "e1", "p2", None, "u1")
    assert out["status"] == "merged" and out["match_project_id"] == "p2"
    assert out["match_review_agreed"] is False and out["match_review_decision"] == "merge"
    (row,) = _matches(db)
    assert row["candidate_rank"] is None and row["breakdown"]["name"] == 0.0
    assert row["candidates"][0]["project_id"] == "p1"   # the list at decision time
    assert row["decided_by"] == "u1"
    assert db.tables["notifications"] == []


def test_human_merge_with_body_gc_stores_kind_human(db):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match", resolved_gc_id=None, gc_match_kind=None)
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.merge_email(db, "e1", "p1", None, "u1")
    assert exc.value.code == ingest.CODE_GC_REQUIRED
    out = ingest.merge_email(db, "e1", "p1", "gc-2", "u1")
    assert out["resolved_gc_id"] == "gc-2" and out["gc_match_kind"] == "human"
    assert _matches(db)[0]["gc_match_kind"] == "human" and _links(db)[0]["gc_id"] == "gc-2"


def test_merge_with_an_unknown_body_gc_writes_nothing(db):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match", resolved_gc_id=None, gc_match_kind=None)
    with pytest.raises(LookupError) as exc:
        ingest.merge_email(db, "e1", "p1", "gc-nope", "u1")
    assert not isinstance(exc.value, ingest.RfpMatchError)
    assert _matches(db) == [] and _links(db) == []
    assert _row(db)["status"] == "review_match" and _row(db)["resolved_gc_id"] is None
    assert not _audits(db, "rfp_match.merge")


def test_merge_refuses_closed_and_out_of_window_projects(db):
    _project(db, "p-declined", "Riverside Plaza", stage="declined")
    _project(db, "p-old", "Riverside Plaza", bid_at=OLD_BID_AT)
    _project(db, "p-nodate", "Riverside Plaza", bid_at=None)
    _at_match(db, status="review_match")
    for pid in ("p-declined", "p-old", "p-nodate", "p-missing"):
        with pytest.raises(ingest.RfpMatchError) as exc:
            ingest.merge_email(db, "e1", pid, None, "u1")
        assert exc.value.code == ingest.CODE_PROJECT_CLOSED, pid
    assert _matches(db) == [] and _row(db)["status"] == "review_match"


def test_merge_refuses_when_not_at_review_match_and_system_when_not_at_match(db):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="done")
    with pytest.raises(ingest.RfpMatchError):
        ingest.merge_email(db, "e1", "p1", None, "u1")
    with pytest.raises(ingest.RfpMatchError):
        ingest.merge_email(db, "e1", "p1", "gc-1", None)


def test_duplicate_action_requires_the_gc_on_the_project(db):
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match")
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.duplicate_email(db, "e1", "p1", "u1")
    assert exc.value.code == ingest.CODE_GC_NOT_ON_PROJECT
    _at_match(db, id="e2", internet_message_id="m2", status="review_match", resolved_gc_id=None)
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.duplicate_email(db, "e2", "p1", "u1")
    assert exc.value.code == ingest.CODE_GC_REQUIRED


@pytest.mark.parametrize("action", ["merge", "duplicate"])
def test_open_merge_into_another_project_is_refused_never_dismantled(db, action):
    """Two reviewers merged at once: the second decision, aimed at a different
    project, is refused (409 rfp_match_not_actionable, reload) and A's link
    and open row are untouched. Closing A's row and removing its link while
    the email then lands on B was the old behaviour."""
    _project(db, "p1", "Riverside Plaza")
    _project(db, "p2", "Riverside Plaza Phase 2", gcs=["gc-1"])
    _at_match(db, status="review_match")
    _open_merge_row(db, gc_added=True, project_gc_id="l-sys")
    db.tables["project_gcs"].append({"id": "l-sys", "project_id": "p1", "gc_id": "gc-1",
                                     "needs_by": None, "rfp_match_id": "mr-1"})
    with pytest.raises(ingest.RfpMatchError) as exc:
        if action == "merge":
            ingest.merge_email(db, "e1", "p2", None, "u2")
        else:
            ingest.duplicate_email(db, "e1", "p2", "u2")
    assert exc.value.code == ingest.CODE_NOT_ACTIONABLE
    (match,) = _matches(db)
    assert match["id"] == "mr-1" and match["unmerged_at"] is None
    assert [link["id"] for link in _links(db, "p1")] == ["l-sys"]
    assert _row(db)["status"] == "review_match"
    assert db.rpc_calls == [] and not _audits(db, "rfp_match.merge")


def test_duplicate_request_never_resumes_an_open_merge(db):
    """Same target, but the reviewer says "already on X": the open merged row
    is not flipped into a resumed merge; it is closed as superseded, the
    fresh row is a duplicate, and the link on the project stays."""
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match")
    _open_merge_row(db, gc_added=True, project_gc_id="l-sys")
    db.tables["project_gcs"].append({"id": "l-sys", "project_id": "p1", "gc_id": "gc-1",
                                     "needs_by": None, "rfp_match_id": "mr-1"})
    out = ingest.duplicate_email(db, "e1", "p1", "u1")
    assert out["status"] == "duplicate" and out["match_review_decision"] == "duplicate"
    rows = {r["id"]: r for r in _matches(db)}
    assert rows["mr-1"]["unmerged_at"] and rows["mr-1"]["unmerge_reason"] == "superseded"
    fresh = next(r for r in rows.values() if r["id"] != "mr-1")
    assert fresh["kind"] == "duplicate" and fresh["gc_added"] is False
    assert [link["id"] for link in _links(db)] == ["l-sys"]
    assert not _audits(db, "rfp_match.merge") and _audits(db, "rfp_match.duplicate")


def test_leftover_open_duplicate_row_is_closed_before_a_fresh_insert(db):
    """A reopen that crashed between its two writes leaves an open duplicate
    row; the next decision closes it as superseded and inserts its own."""
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match")
    _open_merge_row(db, kind="duplicate")
    ingest.merge_email(db, "e1", "p1", None, "u1")
    rows = {r["id"]: r for r in _matches(db)}
    assert rows["mr-1"]["unmerged_at"] and rows["mr-1"]["unmerge_reason"] == "superseded"
    fresh = next(r for r in rows.values() if r["id"] != "mr-1")
    assert fresh["kind"] == "merged" and fresh["gc_added"] is True


# ── Unmerge (3.7) ──────────────────────────────────────────────────────────────


def _merged_state(db, *, human=False, due=None):
    """A completed merge: email at merged, open match row, the system's link."""
    _project(db, "p1", "Riverside Plaza")
    _at_match(db, status="review_match" if human else "match",
              extracted_bid_due_at=due)
    return ingest.merge_email(db, "e1", "p1", None, "u1" if human else None)


def _send(db, status, project_id="p1", gc_id="gc-1"):
    row = {"id": f"ps-{status}", "project_id": project_id, "gc_id": gc_id, "status": status}
    db.tables["proposal_sends"].append(row)
    return row


def test_unmerge_reverses_the_merge_and_returns_the_email_to_match(db, llm_answer):
    _merged_state(db)
    (match,) = _matches(db)
    _send(db, "generated")
    closed = ingest.unmerge(db, match["id"], "wrong project", "u9")
    assert closed["unmerged_at"] and closed["unmerged_by"] == "u9"
    assert closed["unmerge_reason"] == "wrong project" and closed["project_gc_id"] is None
    assert _links(db) == [] and db.tables["project_gc_contacts"] == []
    assert db.tables["proposal_sends"][0]["status"] == "superseded"
    assert db.tables["dismissed"][0]["gc_id"] == "gc-1"
    row = _row(db)
    assert row["status"] == "match" and row["excluded_project_ids"] == ["p1"]
    assert row["match_project_id"] is None and row["match_score"] is None
    assert row["attempts"] == 0 and row["last_error"] is None and row["next_attempt_at"] is None
    assert row["match_review_decision"] is None and row["match_review_agreed"] is None
    (audit,) = _audits(db, "rfp_match.unmerge")
    assert audit["args"][0] == "project" and audit["args"][1] == "p1"
    assert audit["args"][2]["gc_removed"] is True and audit["args"][2]["project_gc_id"] is None
    # The re-run finds nothing else and lands at done; the project stays excluded.
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _walk(db)
    row = _row(db)
    assert row["status"] == "done" and row["flag_reason"] == "no_candidate"
    assert row["match_candidates"] == [] and _calls_for(calls, "rfp_match") == []
    assert row["excluded_project_ids"] == ["p1"]
    # A human merge back into the excluded project is refused after a reopen.
    ingest.reopen_match(db, "e1", None, "u1")
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.merge_email(db, "e1", "p1", None, "u1")
    assert exc.value.code == ingest.CODE_PROJECT_EXCLUDED
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.duplicate_email(db, "e1", "p1", "u1")
    assert exc.value.code == ingest.CODE_PROJECT_EXCLUDED


@pytest.mark.parametrize("status", ["sent", "sending"])
def test_unmerge_refused_after_sent_or_sending(db, status):
    _merged_state(db)
    (match,) = _matches(db)
    _send(db, status)
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.unmerge(db, match["id"], "too late", "u9")
    assert exc.value.code == ingest.CODE_GC_ALREADY_SENT
    assert len(_links(db)) == 1 and _matches(db)[0].get("unmerged_at") is None
    assert _row(db)["status"] == "merged" and db.rpc_calls == []


def test_unmerge_refused_when_a_sent_row_appears_after_the_pre_check(db, monkeypatch):
    _merged_state(db)
    (match,) = _matches(db)
    _send(db, "generated")

    def pre_check_then_sent(project_id, gc_id, *, include_sent=False):
        db.tables["proposal_sends"][0]["status"] = "sent"   # mark-submitted got in between
    monkeypatch.setattr(proposal_send, "block_if_sending", pre_check_then_sent)
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.unmerge(db, match["id"], "too late", "u9")
    assert exc.value.code == ingest.CODE_GC_ALREADY_SENT
    assert db.rpc_calls[0][1]["p_refuse_if_sent"] is True
    assert len(_links(db)) == 1 and _links(db)[0]["rfp_match_id"] == match["id"]
    assert _matches(db)[0].get("unmerged_at") is None and _row(db)["status"] == "merged"


def test_unmerge_removes_by_link_id_only(db):
    _merged_state(db)
    (match,) = _matches(db)
    # A person removed the system's link and re-added the GC under a new id.
    db.tables["project_gcs"] = [{"id": "l-new", "project_id": "p1", "gc_id": "gc-1",
                                 "needs_by": None, "rfp_match_id": None}]
    match["project_gc_id"] = None   # what the on-delete-set-null FK did
    closed = ingest.unmerge(db, match["id"], "not ours", "u9")
    assert closed["unmerged_at"] and db.rpc_calls == []
    assert [link["id"] for link in _links(db)] == ["l-new"]
    assert _row(db)["status"] == "match"
    assert _audits(db, "rfp_match.unmerge")[0]["args"][2]["gc_removed"] is True


def test_unmerge_rpc_is_pinned_to_the_system_link(db):
    _merged_state(db)
    (match,) = _matches(db)
    (link,) = _links(db)
    assert match["project_gc_id"] == link["id"]
    ingest.unmerge(db, match["id"], "reason", "u9")
    (call,) = db.rpc_calls
    assert call == ("remove_project_gc_unless_sent", {
        "p_link_id": link["id"], "p_project_id": "p1", "p_gc_id": "gc-1", "p_refuse_if_sent": True,
    })


def test_unmerge_retry_after_a_crash_after_link_removal(db):
    _merged_state(db)
    (match,) = _matches(db)
    db.remove_row("project_gcs", _links(db)[0])   # the link went; the row is still open
    assert match["project_gc_id"] is None
    closed = ingest.unmerge(db, match["id"], "retry", "u9")
    assert closed["unmerged_at"] and _row(db)["status"] == "match"
    assert _row(db)["excluded_project_ids"] == ["p1"]
    assert _audits(db, "rfp_match.unmerge")[0]["args"][2]["gc_removed"] is True


def test_unmerge_retry_after_a_crash_after_the_stamp(db):
    _merged_state(db)
    (match,) = _matches(db)
    db.remove_row("project_gcs", _links(db)[0])
    match.update({"unmerged_at": "2026-09-09T12:00:00+00:00", "unmerged_by": "u9",
                  "unmerge_reason": "first try"})
    assert _row(db)["status"] == "merged"
    closed = ingest.unmerge(db, match["id"], "first try", "u9")
    assert closed["unmerge_reason"] == "first try"
    assert _row(db)["status"] == "match" and _row(db)["excluded_project_ids"] == ["p1"]
    assert len(_audits(db, "rfp_match.unmerge")) == 1
    # Once the email has moved on, the closed row is not actionable.
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.unmerge(db, match["id"], "again", "u9")
    assert exc.value.code == ingest.CODE_NOT_ACTIONABLE


def test_unmerge_validation_and_kind(db):
    _merged_state(db)
    (match,) = _matches(db)
    for bad in (None, "", "ab", "x" * 501):
        with pytest.raises(ValueError):
            ingest.unmerge(db, match["id"], bad, "u9")
    with pytest.raises(LookupError):
        ingest.unmerge(db, "nope", "reason", "u9")
    dup = _open_merge_row(db, id="mr-dup", email_id="e-other", kind="duplicate")
    with pytest.raises(ingest.RfpMatchError):
        ingest.unmerge(db, dup["id"], "reason", "u9")
    assert _row(db)["status"] == "merged"


def test_unmerge_cascades_to_the_duplicates_the_merge_created(db):
    _merged_state(db)
    (match,) = _matches(db)
    later = ingest._iso(ingest._parse_ts(match["decided_at"]) + timedelta(minutes=1))
    earlier = ingest._iso(ingest._parse_ts(match["decided_at"]) - timedelta(minutes=1))
    # Two sibling duplicates decided after the merge, one decided before it,
    # one for another GC, and one whose email a person already reopened.
    for eid, decided, gc, status in (
        ("d-after", later, "gc-1", "duplicate"),
        ("d-after2", later, "gc-1", "duplicate"),
        ("d-before", earlier, "gc-1", "duplicate"),
        ("d-other-gc", later, "gc-2", "duplicate"),
        ("d-reopened", later, "gc-1", "review_match"),
    ):
        _at_match(db, id=eid, internet_message_id=f"m-{eid}", status=status,
                  match_project_id="p1", resolved_gc_id=gc)
        _open_merge_row(db, id=f"mr-{eid}", email_id=eid, gc_id=gc, kind="duplicate",
                        decided_at=decided)
    ingest.unmerge(db, match["id"], "wrong project", "u9")
    rows = {r["id"]: r for r in _matches(db)}
    for closed in ("mr-d-after", "mr-d-after2"):
        assert rows[closed]["unmerged_at"] and rows[closed]["unmerged_by"] == "u9"
        assert rows[closed]["unmerge_reason"] == "cascade: wrong project"
    assert rows["mr-d-before"]["unmerged_at"] is None
    assert rows["mr-d-other-gc"]["unmerged_at"] is None
    # The reopened one is closed (its premise is gone) but its email, no
    # longer at duplicate, is left alone.
    assert rows["mr-d-reopened"]["unmerged_at"]
    for eid in ("d-after", "d-after2"):
        assert _row(db, eid)["status"] == "match"
        assert _row(db, eid)["excluded_project_ids"] == ["p1"]
        assert _row(db, eid)["match_project_id"] is None and _row(db, eid)["attempts"] == 0
    assert _row(db, "d-before")["status"] == "duplicate"
    assert _row(db, "d-other-gc")["status"] == "duplicate"
    assert _row(db, "d-reopened")["status"] == "review_match"
    cascades = _audits(db, "rfp_match.unmerge_cascade")
    assert {a["args"][2]["match_id"] for a in cascades} == {
        "mr-d-after", "mr-d-after2", "mr-d-reopened"}
    assert all(a["args"][2]["parent_match_id"] == match["id"] for a in cascades)


def test_no_cascade_when_the_system_link_was_not_removed(db):
    """A merge whose link a person removed and re-added is unmerged without
    touching the duplicates: the GC is still on the project."""
    _merged_state(db)
    (match,) = _matches(db)
    later = ingest._iso(ingest._parse_ts(match["decided_at"]) + timedelta(minutes=1))
    _at_match(db, id="d1", internet_message_id="m-d1", status="duplicate", match_project_id="p1")
    _open_merge_row(db, id="mr-d1", email_id="d1", kind="duplicate", decided_at=later)
    match["gc_added"] = False   # the merge never added the link
    ingest.unmerge(db, match["id"], "reason", "u9")
    assert next(r for r in _matches(db) if r["id"] == "mr-d1")["unmerged_at"] is None
    assert _row(db, "d1")["status"] == "duplicate"
    assert _audits(db, "rfp_match.unmerge")[0]["args"][2]["gc_removed"] is False
    assert len(_links(db)) == 1


def test_unmerge_by_email_resolves_the_latest_merged_row(db):
    _merged_state(db)
    (first,) = _matches(db)
    assert ingest.latest_merged_match_for_email(db, "e1")["id"] == first["id"]
    assert ingest.latest_match_for_email(db, "e1")["id"] == first["id"]
    assert ingest.latest_merged_match_for_email(db, "nope") is None
    ingest.unmerge(db, first["id"], "reason", "u9")
    listed = ingest.excluded_projects_for_email(db, _row(db))
    assert listed == [{"id": "p1", "name": "Riverside Plaza", "number": None,
                       "unmerge_reason": "reason", "unmerged_by": "u9",
                       "unmerged_by_name": None, "unmerged_at": listed[0]["unmerged_at"]}]
    assert ingest.match_rows_for_project(db, "p1")[0]["id"] == first["id"]


# ── The ordinary-Remove guard (remove_gc_link) ─────────────────────────────────


def test_delete_guard_refuses_a_system_merged_gc_even_after_a_send(db):
    _merged_state(db)
    for status in (None, "generated", "sent"):
        if status:
            db.tables["proposal_sends"] = [_send(db, status)]
        with pytest.raises(proposal_send.ProposalSendError) as exc:
            proposal_send.remove_gc_link("p1", "gc-1")
        assert exc.value.code == proposal_send.RFP_MATCH_UNMERGE_REQUIRED
        assert exc.value.status_code == 409
    assert len(_links(db)) == 1 and db.rpc_calls == []
    assert db.tables["proposal_sends"][0]["status"] == "sent"   # the claim never ran


def test_delete_guard_covers_a_crash_resume_row_without_a_link_yet(db):
    _project(db, "p1", "Riverside Plaza", gcs=["gc-1"])
    _open_merge_row(db, gc_added=False)
    with pytest.raises(proposal_send.ProposalSendError):
        proposal_send.remove_gc_link("p1", "gc-1")


def test_gc_never_merged_still_deletes_with_the_same_end_state(db):
    _project(db, "p1", "Riverside Plaza", gcs=["gc-1", "gc-2"])
    closed_row = _open_merge_row(db, gc_id="gc-2", gc_added=True, project_gc_id="link-p1-gc-2",
                                 unmerged_at="2026-09-09T12:00:00+00:00")   # closed: no guard
    _send(db, "generated")
    _send(db, "failed")
    assert proposal_send.remove_gc_link("p1", "gc-1") is True
    assert [link["gc_id"] for link in _links(db)] == ["gc-2"]
    assert {s["status"] for s in db.tables["proposal_sends"]} == {"superseded"}
    assert db.tables["dismissed"] == [{"project_id": "p1", "types": ["gc_pricing.approval_requested"],
                                       "gc_id": "gc-1"}]
    # Already removed: False, never a raise (the 404 belongs to the route).
    assert proposal_send.remove_gc_link("p1", "gc-1") is False
    # A GC whose only match row is closed deletes normally too, and a sent
    # proposal does not block the ordinary Remove.
    _send(db, "sent", gc_id="gc-2")
    assert proposal_send.remove_gc_link("p1", "gc-2") is True
    assert _links(db) == [] and closed_row["unmerged_at"]


def test_delete_refuses_while_a_send_is_in_progress(db):
    _project(db, "p1", "Riverside Plaza", gcs=["gc-1"])
    _send(db, "sending")
    with pytest.raises(proposal_send.ProposalSendError) as exc:
        proposal_send.remove_gc_link("p1", "gc-1")
    assert exc.value.code is None and "in progress" in str(exc.value)
    assert len(_links(db)) == 1


def test_block_if_sending(db):
    _send(db, "sent")
    proposal_send.block_if_sending("p1", "gc-1")   # sent alone does not block
    with pytest.raises(proposal_send.ProposalSendError, match="already been sent"):
        proposal_send.block_if_sending("p1", "gc-1", include_sent=True)
    db.tables["proposal_sends"][0]["status"] = "sending"
    with pytest.raises(proposal_send.ProposalSendError, match="in progress"):
        proposal_send.block_if_sending("p1", "gc-1")


def test_gone_out_rule():
    head = next(iter(proposal_send.PRICING_APPROVAL_HEADS))
    assert proposal_send.gone_out_rule(head, None)
    assert proposal_send.gone_out_rule("verify", head)
    assert not proposal_send.gone_out_rule("verify", None)
    assert not proposal_send.gone_out_rule("select_vendors", head)


# ── Acknowledge (3.7b) and the project counts (3.9) ────────────────────────────


def test_acknowledge_clears_new_keeps_merged_and_is_refused_after_send(db):
    _merged_state(db)
    (match,) = _matches(db)
    assert ingest.rfp_match_counts(db, ["p1", "p-none"], ["p1"]) == {
        "p1": {"merged": 1, "history": 0, "new": 1},
        "p-none": {"merged": 0, "history": 0, "new": 0},
    }
    assert ingest.rfp_match_counts(db, ["p1"], [])["p1"]["new"] == 0   # not post-send
    with pytest.raises(ValueError):
        ingest.acknowledge_match(db, match["id"], "x" * 501, "u1")
    out = ingest.acknowledge_match(db, match["id"], " no proposal needed ", "u1")
    assert out["acknowledged_by"] == "u1" and out["acknowledged_at"]
    assert out["acknowledge_reason"] == "no proposal needed"
    assert _row(db)["status"] == "merged" and len(_links(db)) == 1
    assert ingest.rfp_match_counts(db, ["p1"], ["p1"])["p1"] == {"merged": 1, "history": 0, "new": 0}
    assert _audits(db, "rfp_match.acknowledge")
    with pytest.raises(ingest.RfpMatchError):
        ingest.acknowledge_match(db, match["id"], None, "u1")   # twice
    # Unmerge is still allowed on an acknowledged row until sent.
    ingest.unmerge(db, match["id"], "changed our mind", "u9")
    assert ingest.rfp_match_counts(db, ["p1"], ["p1"])["p1"] == {"merged": 0, "history": 1, "new": 0}


def test_acknowledge_refused_after_send_and_on_non_merges(db):
    _merged_state(db)
    (match,) = _matches(db)
    _send(db, "sent")
    with pytest.raises(ingest.RfpMatchError) as exc:
        ingest.acknowledge_match(db, match["id"], None, "u1")
    assert exc.value.code == ingest.CODE_NOT_ACTIONABLE
    # A sent proposal also clears `new` by itself.
    assert ingest.rfp_match_counts(db, ["p1"], ["p1"])["p1"]["new"] == 0
    dup = _open_merge_row(db, id="mr-dup", email_id="e-other", kind="duplicate")
    with pytest.raises(ingest.RfpMatchError):
        ingest.acknowledge_match(db, dup["id"], None, "u1")
    with pytest.raises(LookupError):
        ingest.acknowledge_match(db, "nope", None, "u1")


def test_duplicate_rows_never_raise_the_counts(db):
    _project(db, "p1", "Riverside Plaza", gcs=["gc-1"])
    _open_merge_row(db, kind="duplicate")
    assert ingest.rfp_match_counts(db, ["p1"], ["p1"])["p1"] == {"merged": 0, "history": 1, "new": 0}
    # proposal_sends is not queried when there is no open merge.
    assert "proposal_sends" in db.tables   # seeded, but untouched by a select
    # An open merge whose link is gone is history too.
    _open_merge_row(db, id="mr-2", email_id="e2", gc_added=True, project_gc_id=None)
    assert ingest.rfp_match_counts(db, ["p1"], ["p1"])["p1"] == {"merged": 0, "history": 2, "new": 0}


def test_counts_are_empty_when_the_flag_is_off(db, monkeypatch):
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_ingest_enabled=False))
    assert ingest.rfp_match_counts(db, ["p1"], ["p1"]) == {}
    assert ingest.rfp_match_counts(db, [], []) == {}


def test_match_stats_counts_confident_rows(db):
    _at_match(db, id="a", status="review_match", flag_reason="match_confident")
    _at_match(db, id="b", internet_message_id="mb", status="merged", flag_reason="match_confident",
              match_review_agreed=True)
    _at_match(db, id="c", internet_message_id="mc", status="done", flag_reason="match_confident",
              match_review_agreed=False)
    _at_match(db, id="d", internet_message_id="md", status="done", flag_reason="match_uncertain",
              match_review_agreed=False)
    assert ingest.match_stats(db) == {"confident": {"pending": 1, "agreed": 1, "disagreed": 1}}


# ── Human actions on review_match (3.8) ────────────────────────────────────────


def test_reject_sets_no_match_and_keeps_candidates(db, monkeypatch):
    """"Not a match" takes the match step's own no-project road
    (RFP_CREATE.md 3): `create` when nothing harvests, `harvest` when the
    method has a harvester and the email carries something for it; the
    review fields are written either way and the candidates kept."""
    monkeypatch.setattr(ingest.rfp_harvest, "harvester_for", lambda row, settings=None: None)
    cands = [{"project_id": "p1", "name": "Riverside Plaza", "breakdown": {"total": 0.7}}]
    _at_match(db, status="review_match", flag_reason="match_uncertain", match_candidates=cands,
              match_project_id="p1", attempts=3, last_error="old", next_attempt_at="soon")
    out = ingest.reject_match(db, "e1", "u1")
    assert out["status"] == "create" and out["match_review_decision"] == "no_match"
    assert out["match_review_agreed"] is False and out["match_review_by"] == "u1"
    assert out["decided_at_step"] == "review_match" and out["flag_reason"] == "match_uncertain"
    assert out["match_candidates"] == cands
    assert out["attempts"] == 0 and out["last_error"] is None and out["next_attempt_at"] is None
    assert _audits(db, "rfp_match.reject")[-1]["args"][2] == {"match_project_id": "p1", "to_status": "create"}
    with pytest.raises(ingest.RfpMatchError):
        ingest.reject_match(db, "e1", "u2")
    # With a harvester and something to harvest: the row goes to harvest.
    monkeypatch.setattr(ingest.rfp_harvest, "harvester_for", lambda row, settings=None: "procore")
    monkeypatch.setattr(ingest.rfp_harvest, "reference_for", lambda row: object())
    _at_match(db, id="e2", internet_message_id="m2", status="review_match", flag_reason="match_uncertain",
              match_candidates=cands, match_project_id="p1")
    out = ingest.reject_match(db, "e2", "u1")
    assert out["status"] == "harvest" and out["match_review_decision"] == "no_match"
    assert out["match_review_by"] == "u1" and out["decided_at_step"] == "review_match"
    # A harvester with nothing to harvest: create.
    monkeypatch.setattr(ingest.rfp_harvest, "reference_for", lambda row: None)
    _at_match(db, id="e3", internet_message_id="m3", status="review_match", flag_reason="match_uncertain")
    assert ingest.reject_match(db, "e3", "u1")["status"] == "create"


def test_set_gc_stays_in_review_and_survives_a_rerun(db, llm_answer):
    _at_match(db, status="review_match", resolved_gc_id=None, gc_match_kind=None)
    with pytest.raises(LookupError):
        ingest.set_match_gc(db, "e1", "gc-nope", "u1")
    out = ingest.set_match_gc(db, "e1", "gc-2", "u1")
    assert out["status"] == "review_match" and out["resolved_gc_id"] == "gc-2"
    assert out["gc_match_kind"] == "human" and out["gc_match_score"] is None
    assert _audits(db, "rfp_match.set_gc")[0]["args"][2]["to"] == "gc-2"
    # A re-run at match (after an unmerge, say) keeps the human's GC.
    llm_answer(None, extract=_extract(), match=SAME)
    _row(db)["status"] = "match"
    _walk(db)
    assert _row(db)["resolved_gc_id"] == "gc-2" and _row(db)["gc_match_kind"] == "human"
    _at_match(db, id="e2", internet_message_id="m2", status="done")
    with pytest.raises(ingest.RfpMatchError):
        ingest.set_match_gc(db, "e2", "gc-2", "u1")


def test_reopen_on_done_and_on_duplicate(db):
    _at_match(db, status="done", flag_reason="no_candidate", match_review_decision="no_match",
              match_review_by="u1", match_review_agreed=False, last_error="x")
    out = ingest.reopen_match(db, "e1", None, "u2")
    assert out["status"] == "review_match" and out["flag_reason"] == "no_candidate"
    assert out["match_review_decision"] is None and out["match_review_agreed"] is None
    assert out["last_error"] is None
    with pytest.raises(ingest.RfpMatchError):
        ingest.reopen_match(db, "e1", None, "u2")   # not on review_match

    _project(db, "p1", "Riverside Plaza", gcs=["gc-1"])
    _at_match(db, id="e2", internet_message_id="m2", status="review_match")
    ingest.duplicate_email(db, "e2", "p1", "u1")
    assert _row(db, "e2")["status"] == "duplicate"
    out = ingest.reopen_match(db, "e2", "wrong project", "u2")
    assert out["status"] == "review_match"
    (row,) = _matches(db)
    assert row["unmerged_at"] and row["unmerge_reason"] == "reopened: wrong project"
    assert row["unmerged_by"] == "u2" and len(_links(db)) == 1
    assert _audits(db, "rfp_match.reopen")[-1]["args"][2]["from_status"] == "duplicate"
    with pytest.raises(ValueError):
        ingest.reopen_match(db, "e2", "x" * 501, "u2")


def test_dismiss_on_review_match_is_refused_and_leaves_the_row(db):
    _at_match(db, status="review_match", flag_reason="match_uncertain")
    before = dict(_row(db))
    with pytest.raises(LookupError):
        ingest.dismiss(db, "e1", "u1")
    assert _row(db) == before


@pytest.mark.parametrize("status", ["extract", "match", "review_match", "merged", "duplicate"])
def test_set_method_succeeds_after_the_method_step(db, status):
    _at_match(db, status=status)
    assert ingest.set_method(db, "e1", "procore", "u1")["invitation_method"] == "procore"
    assert _row(db)["status"] == status


# ── Sibling followers (3.1) ────────────────────────────────────────────────────


def _sibling(db, eid, received, **over):
    fields = {"attachments_meta": [{"name": "plans.pdf", "size": 10}], **over}
    return _at_extract(db, id=eid, internet_message_id=f"m-{eid}", received_at=received, **fields)


def test_sibling_of_a_merged_leader_lands_at_duplicate_with_no_llm_call(db, llm_answer):
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    leader = _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="merged",
                      extracted_project_name="Riverside Plaza", extracted_gc_name="GC Example",
                      extracted_at="2026-09-09T10:01:00+00:00", resolved_gc_id="gc-1",
                      resolved_gc_contact_id="c1", gc_match_kind="contact", match_project_id="p1",
                      match_score=1.0, match_weights={"scorer_version": "v"},
                      match_candidates=[{"project_id": "p1", "name": "Riverside Plaza",
                                         "breakdown": {"total": 1.0}, "verdict": "same",
                                         "confidence": 0.9, "reasoning": "r"}])
    _sibling(db, "f", "2026-09-09T10:03:00+00:00")
    outcome, stats = _walk(db, "f")
    row = _row(db, "f")
    assert outcome is None and row["status"] == "duplicate"
    assert row["sibling_of_email_id"] == leader["id"] and row["gc_match_kind"] == "sibling"
    assert row["extracted_project_name"] == "Riverside Plaza" and row["extracted_at"]
    assert row["resolved_gc_id"] == "gc-1" and row["match_project_id"] == "p1"
    assert row["decided_at_step"] == "match" and row["flag_reason"] is None and row["matched_at"]
    (match,) = _matches(db)
    assert match["kind"] == "duplicate" and match["sibling_of_email_id"] == "lead"
    assert match["decided_by"] is None and match["candidate_rank"] == 1
    assert match["breakdown"]["verdict"] == "same" and match["gc_match_kind"] == "sibling"
    assert calls == [] and stats.merged_new == 0
    # Idempotent on a retry after a crash between the row insert and the CAS.
    row["status"] = "extract"
    _walk(db, "f")
    assert len(_matches(db)) == 1 and _row(db, "f")["status"] == "duplicate"


def test_sibling_of_a_done_leader_lands_at_done_with_flag_sibling(db, llm_answer):
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="done", flag_reason="no_candidate",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:01:00+00:00")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00")
    _walk(db, "f")
    row = _row(db, "f")
    assert row["status"] == "done" and row["flag_reason"] == "sibling"
    assert row["sibling_of_email_id"] == "lead" and row["decided_at_step"] == "match"
    assert row["extracted_project_name"] == "Riverside Plaza" and row["extracted_at"]
    assert calls == []


def test_sibling_of_a_created_leader_is_linked_to_its_project(db, llm_answer):
    """docs/RFP_CREATE.md 4.5 step 7 for the late copy: a leader already at
    `created` links the follower straight to its project, so the copy never
    re-matches against the project its own original just created."""
    calls = llm_answer(None, extract=_extract())
    _project(db, "p-created", "Riverside Plaza")
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="created", flag_reason="no_candidate",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:01:00+00:00",
             created_project_id="p-created")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00")
    _walk(db, "f")
    row = _row(db, "f")
    assert row["status"] == "created" and row["flag_reason"] == "sibling"
    assert row["created_project_id"] == "p-created" and row["sibling_of_email_id"] == "lead"
    assert row["decided_at_step"] == "match" and row["extracted_project_name"] == "Riverside Plaza"
    assert calls == []


def test_sibling_of_a_pending_leader_waits_without_spending(db, llm_answer):
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="review_match")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00", attempts=2)
    outcome, _ = _walk(db, "f")
    row = _row(db, "f")
    assert outcome is None and row["status"] == "extract" and row["attempts"] == 2
    assert row["next_attempt_at"] is not None and "lead" in row["last_error"]
    assert calls == []


@pytest.mark.parametrize("leader_status", ["failed", "rejected_by_review", "flagged_llm_no"])
def test_sibling_of_a_failed_leader_extracts_normally(db, llm_answer, leader_status):
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status=leader_status)
    _sibling(db, "f", "2026-09-09T10:03:00+00:00")
    _walk(db, "f")
    row = _row(db, "f")
    assert row["status"] == "done" and row.get("sibling_of_email_id") is None
    assert [c["feature"] for c in calls] == ["rfp_extract"]


def _merged_leader(db, eid="lead", received="2026-09-09T10:00:00+00:00", project_id="p1", **over):
    """A copy that already merged onto `project_id`, with everything a
    follower would inherit."""
    fields = dict(
        status="merged", extracted_project_name="Riverside Plaza",
        extracted_at="2026-09-09T10:01:00+00:00", resolved_gc_id="gc-1",
        resolved_gc_contact_id="c1", gc_match_kind="contact", gc_match_score=0.9,
        match_project_id=project_id, match_score=1.0, match_weights={"scorer_version": "v"},
        match_candidates=[{"project_id": project_id, "name": "Riverside Plaza",
                           "breakdown": {"total": 1.0}, "verdict": "same",
                           "confidence": 0.9, "reasoning": "r"}],
    )
    fields.update(over)
    return _sibling(db, eid, received, **fields)


@pytest.mark.parametrize("step", ["extract", "match"])
def test_the_follow_never_lands_a_row_on_a_project_its_unmerge_excluded(db, llm_answer, step):
    """3.8: a person said this email is NOT that project. Every other writer
    honours `excluded_project_ids` (the scorer, `_record_decision`,
    `rfp_create._project_joinable`); inheriting a copy's merge must not be
    the one way back in, at either call site."""
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _merged_leader(db)
    extra = ({} if step == "extract" else
             {"status": "match", "extracted_project_name": "Riverside Plaza",
              "extracted_at": "2026-09-09T10:03:30+00:00"})
    _sibling(db, "f", "2026-09-09T10:03:00+00:00", excluded_project_ids=["p1"], **extra)
    _walk(db, "f")
    row = _row(db, "f")
    # It walked its own steps instead: p1 was never a candidate, so no match.
    assert row["status"] == "done" and row["flag_reason"] == "no_candidate"
    assert row.get("sibling_of_email_id") is None and row.get("match_project_id") is None
    assert row["gc_match_kind"] != "sibling" and _matches(db) == []
    assert [c["feature"] for c in calls] == (["rfp_extract"] if step == "extract" else [])


@pytest.mark.parametrize("step", ["extract", "match"])
def test_the_follow_keeps_a_gc_a_person_picked(db, llm_answer, step):
    """3.2: a GC set on the review screen survives a re-run, and it survives
    a sibling follow too. The project still comes from the leader; only the
    GC identity is the person's."""
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _merged_leader(db)
    extra = ({} if step == "extract" else
             {"status": "match", "extracted_project_name": "Riverside Plaza",
              "extracted_at": "2026-09-09T10:03:30+00:00"})
    _sibling(db, "f", "2026-09-09T10:03:00+00:00", gc_match_kind="human",
             resolved_gc_id="gc-2", resolved_gc_contact_id="c2", gc_match_score=None, **extra)
    _walk(db, "f")
    row = _row(db, "f")
    assert row["status"] == "duplicate" and row["sibling_of_email_id"] == "lead"
    assert row["match_project_id"] == "p1"                 # the leader's project
    assert row["resolved_gc_id"] == "gc-2" and row["resolved_gc_contact_id"] == "c2"
    assert row["gc_match_kind"] == "human" and row["gc_match_score"] is None
    (match,) = _matches(db)
    assert match["gc_id"] == "gc-2" and match["gc_match_kind"] == "human"
    assert match["project_id"] == "p1" and match["sibling_of_email_id"] == "lead"
    assert calls == []


def test_a_follower_with_no_human_gc_still_inherits_the_leaders(db, llm_answer):
    """The other side of the same branch: without a person's pick the
    follower takes the leader's GC and records `sibling` provenance."""
    llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _merged_leader(db)
    _sibling(db, "f", "2026-09-09T10:03:00+00:00")
    _walk(db, "f")
    row = _row(db, "f")
    assert row["resolved_gc_id"] == "gc-1" and row["gc_match_kind"] == "sibling"
    assert row["gc_match_score"] == 0.9
    (match,) = _matches(db)
    assert match["gc_id"] == "gc-1" and match["gc_match_kind"] == "sibling"


@pytest.mark.parametrize("project", ["abandoned", "deleted", "excluded"])
def test_a_created_leader_with_no_live_project_is_not_followed(db, llm_answer, project):
    """`sibling_decided` is pure and calls a `created` copy decided; the
    follow re-reads the project the way `rfp_create._project_joinable` does,
    so a follower is never linked to a dead project."""
    calls = llm_answer(None, extract=_extract())
    if project != "deleted":
        _project(db, "p-created", "Riverside Plaza",
                 abandoned_at="2026-09-09T09:00:00+00:00" if project == "abandoned" else None)
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="created", flag_reason="no_candidate",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:01:00+00:00",
             created_project_id="p-created")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00",
             excluded_project_ids=["p-created"] if project == "excluded" else [])
    _walk(db, "f")
    row = _row(db, "f")
    assert row["status"] == "done" and row["flag_reason"] == "no_candidate"
    assert row.get("created_project_id") is None and row.get("sibling_of_email_id") is None
    assert [c["feature"] for c in calls] == ["rfp_extract"]   # it did its own work


def test_a_created_leader_on_an_abandoned_project_is_not_followed_at_match_either(db, llm_answer):
    calls = llm_answer(None, extract=_extract())
    _project(db, "p-created", "Riverside Plaza", abandoned_at="2026-09-09T09:00:00+00:00")
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="created", flag_reason="no_candidate",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:01:00+00:00",
             created_project_id="p-created")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00", status="match",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:03:30+00:00")
    _walk(db, "f")
    row = _row(db, "f")
    assert row["status"] == "done" and row.get("created_project_id") is None
    assert row.get("sibling_of_email_id") is None and calls == []


def test_sibling_with_a_different_authorization_kind_walks_the_steps(db, llm_answer):
    calls = llm_answer(None, extract=_extract(gc_name="GC Example Builders"), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="merged", match_project_id="p1",
             resolved_gc_id="gc-1", extracted_project_name="Riverside Plaza",
             extracted_at="2026-09-09T10:01:00+00:00")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00", authorization_kind="override",
             invitation_method="nonorganic")
    _walk(db, "f")
    row = _row(db, "f")
    # The verified-sender gate parks it for a person; nothing inherited.
    assert row["status"] == "review_match" and row["flag_reason"] == "match_sender_unverified"
    assert row.get("sibling_of_email_id") is None and _matches(db) == []
    assert [c["feature"] for c in calls] == ["rfp_extract", "rfp_match"]


def test_not_a_sibling_outside_the_window_or_with_other_attachments(db, llm_answer):
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="done", flag_reason="no_candidate",
             extracted_at="2026-09-09T10:01:00+00:00")
    _sibling(db, "late", "2026-09-10T10:00:00+00:00")
    _sibling(db, "other", "2026-09-09T10:03:00+00:00", attachments_meta=[{"name": "plans.pdf", "size": 99}])
    _walk(db, "late")
    _walk(db, "other")
    assert _row(db, "late")["flag_reason"] == "no_candidate"
    assert _row(db, "other")["flag_reason"] == "no_candidate"
    assert len(calls) == 2


# ── Arrival order: the younger copy can be swept first (3.1) ──────────────────


def test_a_late_older_copy_follows_the_younger_one_that_already_finished(db, llm_answer):
    """The whole point of the symmetric rule. Graph's delta can hand the
    younger copy over a tick before the older one; the younger finds no
    sibling and does the work, and the older one arriving later must follow
    it rather than pay for a second extract."""
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00")
    _walk(db, "younger")
    assert _row(db, "younger")["status"] == "done"
    assert [c["feature"] for c in calls] == ["rfp_extract"]
    # The older copy is pulled from its mailbox on a later tick.
    _sibling(db, "older", "2026-09-09T10:00:00+00:00")
    _walk(db, "older")
    row = _row(db, "older")
    assert row["status"] == "done" and row["flag_reason"] == "sibling"
    assert row["sibling_of_email_id"] == "younger" and row["decided_at_step"] == "match"
    assert row["extracted_project_name"] == "Riverside Plaza" and row["extracted_at"]
    assert [c["feature"] for c in calls] == ["rfp_extract"]   # the model was not called again


def test_a_late_older_copy_follows_a_younger_merged_one_to_duplicate(db, llm_answer):
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza")
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00", status="merged",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:04:00+00:00",
             resolved_gc_id="gc-1", resolved_gc_contact_id="c1", gc_match_kind="contact",
             match_project_id="p1", match_score=1.0,
             match_candidates=[{"project_id": "p1", "name": "Riverside Plaza",
                                "breakdown": {"total": 1.0}, "verdict": "same",
                                "confidence": 0.9, "reasoning": "r"}])
    _sibling(db, "older", "2026-09-09T10:00:00+00:00")
    _walk(db, "older")
    row = _row(db, "older")
    assert row["status"] == "duplicate" and row["sibling_of_email_id"] == "younger"
    assert row["match_project_id"] == "p1" and row["gc_match_kind"] == "sibling"
    (match,) = _matches(db)
    assert match["kind"] == "duplicate" and match["sibling_of_email_id"] == "younger"
    assert match["rfp_email_id"] == "older" and match["decided_by"] is None
    assert calls == []


def test_a_late_older_copy_follows_a_younger_created_one_to_its_project(db, llm_answer):
    calls = llm_answer(None, extract=_extract())
    _project(db, "p-created", "Riverside Plaza")
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00", status="created", flag_reason="no_candidate",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:04:00+00:00",
             created_project_id="p-created")
    _sibling(db, "older", "2026-09-09T10:00:00+00:00")
    _walk(db, "older")
    row = _row(db, "older")
    assert row["status"] == "created" and row["flag_reason"] == "sibling"
    assert row["created_project_id"] == "p-created" and row["sibling_of_email_id"] == "younger"
    assert calls == []


def test_an_undecided_younger_copy_is_never_waited_on(db, llm_answer):
    """Rule 2 only ever points backwards: waiting on a younger copy could
    have the pair waiting on each other forever."""
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00", status="extract")
    _sibling(db, "older", "2026-09-09T10:00:00+00:00")
    _walk(db, "older")
    row = _row(db, "older")
    assert row["status"] == "done" and row.get("sibling_of_email_id") is None
    assert _row(db, "younger")["status"] == "extract"       # untouched
    assert [c["feature"] for c in calls] == ["rfp_extract"]


def test_a_decided_copy_beats_an_older_undecided_one(db, llm_answer):
    """Rule 1 before rule 2: a decision is a fact, wherever it sits."""
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "oldest", "2026-09-09T09:58:00+00:00", status="classify")
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00", status="done", flag_reason="no_candidate",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:04:00+00:00")
    _sibling(db, "me", "2026-09-09T10:00:00+00:00")
    _walk(db, "me")
    row = _row(db, "me")
    assert row["status"] == "done" and row["sibling_of_email_id"] == "younger"
    assert calls == []


def test_a_copy_authorized_by_another_rule_is_a_different_invitation(db, llm_answer):
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00", status="done", flag_reason="no_candidate",
             authorization_rule_id="rule-b", extracted_at="2026-09-09T10:04:00+00:00")
    _sibling(db, "older", "2026-09-09T10:00:00+00:00", authorization_rule_id="rule-a")
    _walk(db, "older")
    row = _row(db, "older")
    assert row["status"] == "done" and row.get("sibling_of_email_id") is None
    assert [c["feature"] for c in calls] == ["rfp_extract"]


# ── Chains: whatever order the copies arrive in, one root (3.1) ───────────────


def _drain_in_order(db, order, times):
    """Each copy is listed out of its mailbox and swept on its own tick, in
    `order`. That IS the arrival order the bug rode in on."""
    for eid in order:
        _sibling(db, eid, times[eid])
        _walk(db, eid)
    return {eid: _row(db, eid) for eid in times}


def _assert_one_root(rows, calls):
    roots = [eid for eid, r in rows.items() if not r.get("sibling_of_email_id")]
    assert len(roots) == 1, {e: r.get("sibling_of_email_id") for e, r in rows.items()}
    root = roots[0]
    for eid, row in rows.items():
        if eid == root:
            continue
        # Every follower points AT THE ROOT, never at another follower: the
        # creation step links one generation from the row it creates from.
        assert row["sibling_of_email_id"] == root, (eid, row["sibling_of_email_id"])
        assert row["status"] == "done" and row["flag_reason"] == "sibling", eid
    assert [c["feature"] for c in calls] == ["rfp_extract"]   # one extract for the group
    return root


TIMES3 = {"a": "2026-09-09T10:00:00+00:00", "b": "2026-09-09T10:01:00+00:00",
          "c": "2026-09-09T10:03:00+00:00"}
TIMES4 = {**TIMES3, "d": "2026-09-09T10:04:00+00:00"}


@pytest.mark.parametrize("order", list(permutations("abc")), ids=lambda o: "".join(o))
def test_three_copies_in_every_arrival_order_share_one_root(db, llm_answer, order):
    calls = llm_answer(None, extract=_extract())
    rows = _drain_in_order(db, order, TIMES3)
    root = _assert_one_root(rows, calls)
    # And the root's creation collapses the whole group, chain or not.
    assert rc.link_harvest_mates(db, None, "p-new", sibling_of=root, exclude_id=root) == 2
    for eid in TIMES3:
        if eid == root:
            continue
        follower = _row(db, eid)
        assert follower["created_project_id"] == "p-new" and follower["status"] == "created"
        assert follower["flag_reason"] == "sibling"


@pytest.mark.parametrize("order", list(permutations("abcd")), ids=lambda o: "".join(o))
def test_four_copies_in_every_arrival_order_share_one_root(db, llm_answer, order):
    calls = llm_answer(None, extract=_extract())
    rows = _drain_in_order(db, order, TIMES4)
    root = _assert_one_root(rows, calls)
    assert rc.link_harvest_mates(db, None, "p-new", sibling_of=root, exclude_id=root) == 3
    assert all(_row(db, eid)["created_project_id"] == "p-new"
               for eid in TIMES4 if eid != root)


def test_a_pre_existing_chain_is_still_linked_end_to_end(db):
    """Rows written before the root rule existed (or by a later reopen) can
    already sit in a chain; the creation step walks it."""
    _sibling(db, "root", "2026-09-09T10:00:00+00:00", status="done", flag_reason="no_candidate")
    _sibling(db, "mid", "2026-09-09T10:01:00+00:00", status="done", flag_reason="sibling",
             sibling_of_email_id="root")
    _sibling(db, "leaf", "2026-09-09T10:02:00+00:00", status="done", flag_reason="sibling",
             sibling_of_email_id="mid")
    _sibling(db, "twig", "2026-09-09T10:03:00+00:00", status="done", flag_reason="sibling",
             sibling_of_email_id="leaf")
    assert rc.link_harvest_mates(db, None, "p-new", sibling_of="root", exclude_id="root") == 3
    assert all(_row(db, eid)["created_project_id"] == "p-new"
               for eid in ("mid", "leaf", "twig"))


def test_a_sibling_cycle_does_not_hang_the_walk(db):
    """A cycle can only come from a hand edit or a partial write; it must
    stop the walk, not spin it."""
    _sibling(db, "x", "2026-09-09T10:00:00+00:00", status="done", sibling_of_email_id="y")
    _sibling(db, "y", "2026-09-09T10:01:00+00:00", status="done", sibling_of_email_id="x")
    assert rc.link_harvest_mates(db, None, "p-new", sibling_of="x", exclude_id="x") == 1
    assert _row(db, "y")["created_project_id"] == "p-new"
    # And the ingest side stops too, landing on one of the two.
    root = ingest._sibling_root(db, _row(db, "x"))
    assert root["id"] in ("x", "y")


def test_the_leader_walk_stops_at_the_hop_cap(db, caplog):
    chain = [f"n{i}" for i in range(ingest._SIBLING_CHAIN_MAX_HOPS + 3)]
    for i, eid in enumerate(chain):
        _sibling(db, eid, f"2026-09-09T10:{i:02d}:00+00:00", status="done",
                 sibling_of_email_id=chain[i + 1] if i + 1 < len(chain) else None)
    with caplog.at_level("WARNING"):
        root = ingest._sibling_root(db, _row(db, "n0"))
    assert root["id"] == chain[ingest._SIBLING_CHAIN_MAX_HOPS]
    assert "deeper than" in caplog.text


# ── The received_at tie needs created_at on the swept row (3.1) ───────────────


def test_two_copies_at_one_instant_break_the_tie_on_created_at(db, llm_answer):
    """Same received_at, different insert order. Without `created_at` the two
    rank only by id, and a swept row (whose select omitted the column) ranks
    itself with an empty one and leads against every copy: both copies lead
    and both pay for an extract."""
    assert "created_at" in [c.strip() for c in ingest._SWEEP_SELECT.split(",")]
    calls = llm_answer(None, extract=_extract())
    same = "2026-09-09T10:00:00+00:00"
    # `zz` sorts AFTER `aa` by id, so only created_at can make it the root.
    _sibling(db, "zz", same, created_at="2026-09-09T10:00:01+00:00")
    _sibling(db, "aa", same, created_at="2026-09-09T10:00:09+00:00")

    def swept(email_id):
        (row,) = db.table("rfp_emails").select(ingest._SWEEP_SELECT).eq("id", email_id).execute().data
        return dict(row)

    aa = swept("aa")
    assert aa["created_at"] == "2026-09-09T10:00:09+00:00"     # the select carries it
    # The younger of the two waits behind the other; no attempt, no call.
    ingest._process_email(db, aa, stats=ingest._TickStats())
    row = _row(db, "aa")
    assert row["status"] == "extract" and row["next_attempt_at"] and "zz" in row["last_error"]
    assert calls == []
    # The root does the work, and the waiter then follows it.
    ingest._process_email(db, swept("zz"), stats=ingest._TickStats())
    assert _row(db, "zz")["status"] == "done"
    ingest._process_email(db, swept("aa"), stats=ingest._TickStats())
    assert _row(db, "aa")["sibling_of_email_id"] == "zz"
    assert [c["feature"] for c in calls] == ["rfp_extract"]
    # The regression itself: strip created_at off the swept row and it
    # out-ranks the copy it should be following.
    bare = {k: v for k, v in swept("aa").items() if k != "created_at"}
    assert rfp_match.received_order(bare) < rfp_match.received_order(swept("zz"))
    assert rfp_match.received_order(swept("aa")) > rfp_match.received_order(swept("zz"))


# ── Decided copies short-circuit the match step too (3.1) ────────────────────


def test_a_copy_parked_at_match_follows_a_decided_copy_with_no_match_call(db, llm_answer):
    """The row already paid for its extract; it must not also pay for a match
    call once a copy of it has decided."""
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00", status="match",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:04:00+00:00")
    _sibling(db, "older", "2026-09-09T10:00:00+00:00")
    _walk(db, "older")
    assert _row(db, "older")["status"] == "done"
    _walk(db, "younger")
    row = _row(db, "younger")
    assert row["status"] == "done" and row["flag_reason"] == "sibling"
    assert row["sibling_of_email_id"] == "older" and row["decided_at_step"] == "match"
    assert [c["feature"] for c in calls] == ["rfp_extract"]   # never rfp_match for the copy
    assert _matches(db) == []


def test_a_copy_at_match_follows_a_merged_copy_to_duplicate_with_no_match_call(db, llm_answer,
                                                                               monkeypatch):
    _auto_merge(monkeypatch)
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza", number="26.9.7001")
    _sibling(db, "younger", "2026-09-09T10:03:00+00:00", status="match",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:04:00+00:00")
    _sibling(db, "older", "2026-09-09T10:00:00+00:00")
    _walk(db, "older")
    assert _row(db, "older")["status"] == "merged"
    before = [c["feature"] for c in calls]
    _walk(db, "younger")
    row = _row(db, "younger")
    assert row["status"] == "duplicate" and row["sibling_of_email_id"] == "older"
    assert [c["feature"] for c in calls] == before       # no second rfp_match call
    dup = [m for m in _matches(db) if m["rfp_email_id"] == "younger"]
    assert len(dup) == 1 and dup[0]["kind"] == "duplicate" and dup[0]["decided_by"] is None
    assert dup[0]["sibling_of_email_id"] == "older"


def test_the_match_step_never_waits_behind_an_undecided_copy(db, llm_answer):
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "older", "2026-09-09T10:00:00+00:00", status="classify")
    _sibling(db, "me", "2026-09-09T10:02:00+00:00", status="match",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:03:00+00:00")
    _walk(db, "me")
    row = _row(db, "me")
    assert row["status"] == "done" and row.get("sibling_of_email_id") is None
    assert calls == []      # no candidates to judge, so no match call either


def test_an_open_merge_of_this_rows_own_is_finished_before_the_sibling_check(db, llm_answer,
                                                                            monkeypatch):
    """A crash between merge steps 5 and 6 must still resume as a merge, not
    be turned into a sibling duplicate by a copy that decided meanwhile."""
    _auto_merge(monkeypatch)
    calls = llm_answer(None, extract=_extract(), match=SAME)
    _project(db, "p1", "Riverside Plaza", number="26.9.7001")
    _sibling(db, "copy", "2026-09-09T10:03:00+00:00", status="done", flag_reason="no_candidate",
             extracted_at="2026-09-09T10:04:00+00:00")
    _sibling(db, "me", "2026-09-09T10:00:00+00:00", status="match",
             extracted_project_name="Riverside Plaza", extracted_at="2026-09-09T10:01:00+00:00",
             resolved_gc_id="gc-1")
    db.tables["rfp_project_matches"].append({
        "id": "open-1", "rfp_email_id": "me", "project_id": "p1", "gc_id": "gc-1",
        "kind": "merged", "gc_added": False, "unmerged_at": None, "decided_at": "x",
    })
    _walk(db, "me")
    row = _row(db, "me")
    assert row["status"] == "merged" and row.get("sibling_of_email_id") is None
    (match,) = _matches(db)
    assert match["id"] == "open-1" and match["kind"] == "merged"   # the same row, finished
    assert calls == []


# ── The bench event reads the outcome, not the caller's stale row ────────────


def test_the_bench_event_names_the_copy_that_was_followed(db, llm_answer):
    db.tables["rfp_test_events"] = []
    calls = llm_answer(None, extract=_extract())
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="done", flag_reason="no_candidate",
             extracted_at="2026-09-09T10:01:00+00:00", test_session_id="s1")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00", test_session_id="s1")
    _walk(db, "f")
    events = [e for e in db.tables["rfp_test_events"] if e["kind"] == "extract"]
    assert len(events) == 1
    assert events[0]["detail"]["sibling_of_email_id"] == "lead"
    assert events[0]["detail"]["next_status"] == "done"
    assert "followed another copy (done)" in events[0]["title"]
    assert calls == []


def test_the_bench_event_records_a_wait_as_a_wait(db, llm_answer):
    db.tables["rfp_test_events"] = []
    llm_answer(None, extract=_extract())
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="review_match", test_session_id="s1")
    _sibling(db, "f", "2026-09-09T10:03:00+00:00", test_session_id="s1")
    _walk(db, "f")
    (event,) = [e for e in db.tables["rfp_test_events"] if e["kind"] == "extract"]
    assert event["detail"]["next_status"] == "wait"
    assert event["detail"]["sibling_of_email_id"] == "lead"
    assert "waited for another copy (wait)" in event["title"]


def test_a_lost_cas_in_the_follow_branch_is_not_a_success(db, monkeypatch):
    """The row moved under us between the read and the write: nothing was
    written, so the step must not report that it followed anything."""
    _sibling(db, "lead", "2026-09-09T10:00:00+00:00", status="done", flag_reason="no_candidate",
             extracted_at="2026-09-09T10:01:00+00:00")
    row = _sibling(db, "f", "2026-09-09T10:03:00+00:00")
    monkeypatch.setattr(ingest, "_terminal", lambda *a, **k: False)
    assert ingest._sibling_short_circuit(db, dict(row), ingest.get_settings()) is None


# ── Startup backfill ───────────────────────────────────────────────────────────


def test_startup_backfill_moves_parked_rows_only(db):
    _seed(db, _email(id="parked", status="done", extracted_at=None, attempts=3, last_error="x",
                     next_attempt_at="2026-09-09T10:00:00+00:00"))
    _seed(db, _email(id="decided", internet_message_id="m2", status="done",
                     extracted_at="2026-09-09T10:01:00+00:00", flag_reason="no_candidate"))
    _seed(db, _email(id="merged", internet_message_id="m3", status="merged", extracted_at=None))
    _seed(db, _email(id="flagged", internet_message_id="m4", status="flagged_llm_no",
                     extracted_at=None))
    assert ingest.backfill_parked_rows(db) == 1
    parked = _row(db, "parked")
    assert parked["status"] == "extract" and parked["attempts"] == 0
    assert parked["last_error"] is None and parked["next_attempt_at"] is None
    assert _row(db, "decided")["status"] == "done"
    assert _row(db, "merged")["status"] == "merged"
    assert _row(db, "flagged")["status"] == "flagged_llm_no"
    assert ingest.backfill_parked_rows(db) == 0   # idempotent
