"""The pure BuildingConnected facts (app/services/bc_facts) over the 24
anonymised fixture rows in tests/fixtures_bc (docs/RFP_BUILDINGCONNECTED.md
sections 2, 3.3, 3.6, 3.7; BUILD_CONTRACT D17, D22, D28).

Pinned: the entry rule for every fixture role against the frozen now
2026-09-26T12:00Z; the masked NDA shape; `effective()` with clientValues
null; html_to_text (lists, links, nested divs, entities, scripts dropped,
the cap with its marker, None); the invitation column set (every scan
column, never status or a resolution column); payload hashing; tracked
changes; project facts with every null, Pacific dates, the midnight rule,
the 400-character bid notes and the notes sentence.
"""

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.services import bc_facts as f

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
TEXT_MAX = 20000
NOTES_MAX = 4000

FIXTURES = Path(__file__).resolve().parent / "fixtures_bc"


def _load() -> list[dict]:
    return json.loads((FIXTURES / "opportunities.json").read_text(encoding="utf-8"))


ROWS = _load()
BY_ROLE = {row["_fixture_role"]: row for row in ROWS}


def _row(role: str) -> dict:
    return json.loads(json.dumps(BY_ROLE[role]))


def _inv(role: str, **over) -> dict:
    """An rfp_portal_invitations row as the scan would insert it."""
    row = f.invitation_fields(_row(role), NOW, text_max_chars=TEXT_MAX)
    row.update(over)
    return row


# ── Fixture sanity ──────────────────────────────────────────────────────────


def test_fixture_set_is_the_documented_24():
    assert len(ROWS) == 24
    assert len(BY_ROLE) == 24
    assert all(row.get("id") for row in ROWS)


# ── Entry rule (3.3) ────────────────────────────────────────────────────────


EXPECTED_ENTRY = {
    "open_undecided_full": "match",
    "with_trade_instructions": "match",
    "open_undecided_min": "match",
    "open_accepted": "match",
    "budget_request": "match",            # no due date never fails the rule
    "no_due_notice": "match",             # the review park happens at the match step
    "no_due_real": "match",
    "nda_masked": "historical",           # masked AND due 2026-09-16, before now
    "past_due_accepted": "historical",
    "submitted_active": "historical",     # SUBMITTED but due 2026-09-16
    "declined_archived": "historical",
    "archived_undecided": "historical",
    "foreign_manual": "historical",
    "foreign_email": "historical",
    "group_parent": "historical",
    "group_child": "historical",
    "sealed": "historical",
    "same_gc_two_packages_a": "historical",
    "same_gc_two_packages_b": "historical",
    "rebid_old": "historical",
    "rebid_new": "historical",
    "cross_gc_a": "historical",           # due 2026-09-25T21:00Z, before now
    "cross_gc_b": "historical",
    "cross_gc_c": "historical",
}


@pytest.mark.parametrize("role", sorted(EXPECTED_ENTRY))
def test_entry_status_for_every_fixture_role(role):
    assert f.entry_status(BY_ROLE[role], NOW) == EXPECTED_ENTRY[role]


def test_entry_status_edge_rules():
    opp = _row("open_undecided_full")
    due = opp["dueAt"]
    # Due exactly now is still open (>= now).
    assert f.entry_status(opp, datetime.fromisoformat(due.replace("Z", "+00:00"))) == "match"
    # DECLINED never enters, archived never enters, SUBMITTED with a future due does.
    assert f.entry_status({**opp, "submissionState": "DECLINED"}, NOW) == "historical"
    assert f.entry_status({**opp, "isArchived": True}, NOW) == "historical"
    assert f.entry_status({**opp, "submissionState": "SUBMITTED"}, NOW) == "match"
    # clientValues.dueAt wins over the top-level date.
    moved = {**opp, "clientValues": {**opp["clientValues"], "dueAt": "2026-09-01T00:00:00.000Z"}}
    assert f.entry_status(moved, NOW) == "historical"
    assert f.ENTRY_STATES == ("UNDECIDED", "WILL_SUBMIT", "SUBMITTED")


# ── Masked rows and effective() ─────────────────────────────────────────────


def test_is_masked_is_the_nda_shape_only():
    assert f.is_masked(BY_ROLE["nda_masked"]) is True
    for role, row in BY_ROLE.items():
        if role != "nda_masked":
            assert f.is_masked(row) is False, role
    # NDA required but the company is exposed (after the GC releases it): not masked.
    populated = _row("nda_masked")
    populated["client"]["company"] = {"id": "abc", "name": "GC Revealed"}
    assert f.is_masked(populated) is False
    assert f.is_masked({"isNdaRequired": True, "client": None}) is True
    assert f.is_masked({"isNdaRequired": False, "client": None}) is False


def test_effective_prefers_client_values_and_falls_back():
    foreign = BY_ROLE["foreign_manual"]
    assert foreign["clientValues"] is None
    assert f.effective(foreign, "dueAt") == foreign["dueAt"]
    assert f.effective(foreign, "name") == "Fixture Project 13"
    opp = _row("open_undecided_full")
    opp["clientValues"]["dueAt"] = "2026-11-01T00:00:00.000Z"
    assert f.effective(opp, "dueAt") == "2026-11-01T00:00:00.000Z"
    opp["clientValues"]["dueAt"] = None
    assert f.effective(opp, "dueAt") == opp["dueAt"]
    masked = BY_ROLE["nda_masked"]
    assert all(v is None for v in masked["clientValues"].values())
    assert f.effective(masked, "name") == "Fixture Project 08"
    assert f.effective(masked, "dueAt") == masked["dueAt"]


# ── Time helpers ────────────────────────────────────────────────────────────


def test_parse_ts_and_pacific_helpers():
    parsed = f.parse_ts("2026-10-16T23:00:00.000Z")
    assert parsed == datetime(2026, 10, 16, 23, 0, tzinfo=timezone.utc)
    assert f.parse_ts(None) is None
    assert f.parse_ts("") is None
    assert f.parse_ts("not a date") is None
    assert f.parse_ts(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    # 02:00Z on the 17th is the evening of the 16th in Las Vegas.
    assert f.pacific_date("2026-10-17T02:00:00Z") == date(2026, 10, 16)
    assert f.pacific_date(None) is None
    # Midnight Pacific during DST is 07:00Z; during standard time 08:00Z.
    assert f.is_midnight_pacific("2026-10-16T07:00:00Z") is True
    assert f.is_midnight_pacific("2026-12-16T08:00:00Z") is True
    assert f.is_midnight_pacific("2026-10-16T23:00:00Z") is False
    assert f.is_midnight_pacific("2026-10-16T00:00:00Z") is False   # midnight UTC is 17:00 PT
    assert f.is_midnight_pacific(None) is False


def test_deep_link_and_normalize_name():
    assert f.deep_link("abc123") == "https://app.buildingconnected.com/opportunities/abc123/info"
    assert f.deep_link(" abc123 ") == "https://app.buildingconnected.com/opportunities/abc123/info"
    assert f.normalize_name("  Fixture   Project, Phase 2 (Electrical)! ") == "fixture project phase 2 electrical"
    assert f.normalize_name("Ｆixture") == "fixture"
    assert f.normalize_name(None) == ""
    assert f.normalize_name("Phase 2") != f.normalize_name("Phase 3")


# ── html_to_text (D28) ──────────────────────────────────────────────────────


def test_html_to_text_lists_links_divs_entities_scripts():
    html = (
        "<div>Scope of <b>work</b></div><div>Second line</div>"
        "<ul><li>Item one</li><li>Item &amp; two</li></ul>"
        "<script>alert('x')</script><style>.a{}</style>"
        '<div>See <a href="https://example.com/plans">the plans</a> and '
        '<a href="https://example.com/">https://example.com/</a></div>'
        "<div><div>Nested   inner</div></div>"
        "<br /><br /><br /><div>After &ldquo;blank&rdquo; &lt;tag&gt;</div>"
    )
    out = f.html_to_text(html, max_chars=TEXT_MAX)
    assert out == (
        "Scope of work\nSecond line\n- Item one\n- Item & two\n"
        "See the plans (https://example.com/plans) and https://example.com/\n"
        "Nested inner\n\nAfter “blank” <tag>"
    )
    assert "alert" not in out and ".a{}" not in out
    assert "\n\n\n" not in out


def test_html_to_text_none_and_empty_and_plain():
    assert f.html_to_text(None, max_chars=TEXT_MAX) is None
    assert f.html_to_text("", max_chars=TEXT_MAX) is None
    assert f.html_to_text("   \n  ", max_chars=TEXT_MAX) is None
    assert f.html_to_text("<br />", max_chars=TEXT_MAX) is None       # no_due_notice's tradeSpecificInstructions
    assert f.html_to_text("<div></div><p> </p>", max_chars=TEXT_MAX) is None
    assert f.html_to_text("plain text, no tags", max_chars=TEXT_MAX) == "plain text, no tags"


def test_html_to_text_cap_includes_the_marker():
    text = "<div>" + "word " * 100 + "</div>"
    out = f.html_to_text(text, max_chars=50)
    assert out.endswith(" [truncated]")
    assert len(out) <= 50
    assert out.startswith("word word")
    short = f.html_to_text("<div>short</div>", max_chars=50)
    assert short == "short"
    assert f.TRUNCATED_SUFFIX == " [truncated]"


def test_html_to_text_on_the_fixture_information_keeps_newlines():
    opp = BY_ROLE["submitted_active"]
    out = f.html_to_text(opp["projectInformation"], max_chars=TEXT_MAX)
    lines = out.split("\n")
    assert any(line.startswith("- ") for line in lines)
    assert "<div" not in out and "<li" not in out and "</" not in out and "<b>" not in out
    assert "&amp;" not in out
    assert "&lorem;" in out          # not a real entity: the parser leaves it alone
    assert all(line == " ".join(line.split()) for line in lines)
    assert "\n\n\n" not in out
    # "<div>a</div><br /><div>b</div>" is the board's blank line; "</div><div>" is not.
    assert f.html_to_text("<div>a</div><br /><div>b</div>", max_chars=100) == "a\n\nb"
    assert f.html_to_text("<div>a</div><div>b</div>", max_chars=100) == "a\nb"
    assert f.html_to_text("<div>a<br />b</div>", max_chars=100) == "a\nb"
    assert f.html_to_text("<ul><li>x</li><li>y</li></ul>", max_chars=100) == "- x\n- y"


# ── invitation_fields (D22, section 2 item 1) ───────────────────────────────


RESOLUTION_COLUMNS = {
    "status", "gc_id", "gc_kind", "gc_candidates", "gc_confirmed_at", "gc_confirmed_by",
    "ignore_source", "project_gc_id", "sibling_of", "created_project_id", "match_project_id",
}


def test_invitation_fields_key_set_is_every_scan_column_and_never_status():
    fields = f.invitation_fields(BY_ROLE["open_undecided_full"], NOW, text_max_chars=TEXT_MAX)
    assert set(fields) == f.INVITATION_COLUMNS
    assert set(fields) == {
        "title", "agency", "agency_key", "bid_number", "bid_number_raw", "close_at", "issued_on",
        "view_url", "external_id", "external_url", "payload", "payload_hash", "bc_updated_at",
        "invited_at", "job_walk_at", "expected_start_at", "expected_finish_at", "rfis_due_at",
        "address", "trade_name", "submission_state", "workflow_bucket", "source", "request_type",
        "is_archived", "is_nda_required", "is_sealed", "gc_external_id", "gc_external_name", "lead",
    }
    assert not (set(fields) & RESOLUTION_COLUMNS)
    for role in BY_ROLE:
        assert set(f.invitation_fields(BY_ROLE[role], NOW, text_max_chars=TEXT_MAX)) == f.INVITATION_COLUMNS


def test_invitation_fields_full_row():
    opp = BY_ROLE["open_undecided_full"]
    fields = f.invitation_fields(opp, NOW, text_max_chars=TEXT_MAX)
    link = "https://app.buildingconnected.com/opportunities/45dcb8142be69abb6affe312/info"
    assert fields["title"] == "Fixture Project 01"
    assert fields["agency"] == "GC AA Builders"
    assert fields["agency_key"] == "bc"
    assert fields["bid_number"] == fields["bid_number_raw"] == fields["external_id"] == opp["id"]
    assert fields["close_at"] == "2026-10-16T23:00:00+00:00"
    assert fields["issued_on"] == "2026-09-16"           # invitedAt 18:02Z is the same PT day
    assert fields["view_url"] == fields["external_url"] == link
    assert fields["bc_updated_at"] == "2026-09-21T14:48:35.291000+00:00"
    assert fields["invited_at"] == "2026-09-16T18:02:10.048000+00:00"
    assert fields["job_walk_at"] == "2026-09-24T14:00:00+00:00"
    assert fields["expected_start_at"] == "2027-01-04T20:00:00+00:00"
    assert fields["expected_finish_at"] == "2027-03-15T19:00:00+00:00"
    assert fields["rfis_due_at"] == "2026-09-25T19:00:00+00:00"
    assert fields["address"] == "100 Main St, Las Vegas, NV 89101, United States of America"
    assert fields["trade_name"] == "Electrical"
    assert fields["submission_state"] == "UNDECIDED"
    assert fields["workflow_bucket"] == "UNDECIDED_ACTIVE_ORPHAN"
    assert fields["source"] == "BUILDINGCONNECTED"
    assert fields["request_type"] == "PROPOSAL"
    assert fields["is_archived"] is False
    assert fields["is_nda_required"] is False
    assert fields["is_sealed"] is False
    assert fields["gc_external_id"] == "ab41ccb9140a2eb134ce6fa6"
    assert fields["gc_external_name"] == "GC AA Builders"
    assert fields["lead"] == {
        "first_name": "Lead",
        "last_name": "Person00",
        "email": "lead00@gcaabuilders.example.com",
        "phone": "702-555-0100",
    }
    assert "_fixture_role" not in fields["payload"]
    assert fields["payload"]["id"] == opp["id"]
    assert fields["payload_hash"] == f.payload_hash(opp)


def test_invitation_fields_masked_and_foreign_rows():
    masked = f.invitation_fields(BY_ROLE["nda_masked"], NOW, text_max_chars=TEXT_MAX)
    assert masked["agency"] == "(NDA)"
    assert masked["title"] == "Fixture Project 08"
    assert masked["gc_external_id"] is None and masked["gc_external_name"] is None
    assert masked["lead"] is None
    assert masked["bc_updated_at"] is None
    assert masked["invited_at"] is None
    assert masked["issued_on"] == "2026-09-02"      # createdAt is the fallback day
    assert masked["address"] is None
    assert masked["workflow_bucket"] is None
    assert masked["is_nda_required"] is True
    assert masked["close_at"] == "2026-09-16T17:00:00+00:00"
    foreign = f.invitation_fields(BY_ROLE["foreign_manual"], NOW, text_max_chars=TEXT_MAX)
    assert foreign["agency"] == "(No GC)"
    assert foreign["gc_external_id"] is None
    assert foreign["lead"] is None
    assert foreign["source"] == "MANUAL"
    assert foreign["is_archived"] is True
    assert foreign["job_walk_at"] == "2025-01-22T19:00:00+00:00"
    sealed = f.invitation_fields(BY_ROLE["sealed"], NOW, text_max_chars=TEXT_MAX)
    assert sealed["is_sealed"] is True
    budget = f.invitation_fields(BY_ROLE["budget_request"], NOW, text_max_chars=TEXT_MAX)
    assert budget["request_type"] == "BUDGET"
    assert budget["close_at"] is None


def test_invitation_fields_lead_email_lowercased_and_blank_phone_none():
    opp = _row("no_due_notice")
    opp["client"]["lead"]["email"] = "Lead05@GCAEBuilders.example.com"
    fields = f.invitation_fields(opp, NOW, text_max_chars=TEXT_MAX)
    assert fields["lead"]["email"] == "lead05@gcaebuilders.example.com"
    assert fields["lead"]["phone"] is None


def test_payload_hash_is_canonical_and_ignores_fixture_keys():
    opp = _row("open_undecided_min")
    stripped = {k: v for k, v in opp.items() if not k.startswith("_")}
    expected = hashlib.sha256(
        json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert f.payload_hash(opp) == expected
    assert f.payload_hash(stripped) == expected
    reordered = dict(reversed(list(opp.items())))
    assert f.payload_hash(reordered) == expected
    changed = {**opp, "name": "Renamed"}
    assert f.payload_hash(changed) != expected


# ── tracked_changes (3.6) ───────────────────────────────────────────────────


def test_tracked_changes_reports_moved_fields_only():
    old = _inv("open_undecided_full")
    assert f.tracked_changes(old, BY_ROLE["open_undecided_full"], text_max_chars=TEXT_MAX) == []
    opp = _row("open_undecided_full")
    opp["clientValues"]["dueAt"] = "2026-10-20T23:00:00.000Z"
    opp["clientValues"]["name"] = "Fixture Project 01 (Rev 2)"
    opp["isArchived"] = True
    opp["clientValues"]["location"]["complete"] = "101 Main St, Las Vegas, NV 89101"
    changes = f.tracked_changes(old, opp, text_max_chars=TEXT_MAX)
    assert [c["field"] for c in changes] == ["close_at", "title", "is_archived", "address"]
    by_field = {c["field"]: c for c in changes}
    assert by_field["close_at"] == {
        "field": "close_at", "old": "2026-10-16T23:00:00+00:00", "new": "2026-10-20T23:00:00+00:00",
    }
    assert by_field["title"]["new"] == "Fixture Project 01 (Rev 2)"
    assert by_field["is_archived"] == {"field": "is_archived", "old": False, "new": True}
    assert by_field["address"]["new"] == "101 Main St, Las Vegas, NV 89101"
    assert f.TRACKED_FIELDS == (
        "close_at", "job_walk_at", "expected_start_at", "expected_finish_at",
        "title", "submission_state", "is_archived", "address", "gc_external_id", "gc_external_name",
    )


def test_tracked_changes_compares_instants_not_spellings():
    old = _inv("open_undecided_full", close_at="2026-10-16T23:00:00.000Z", job_walk_at="2026-09-24T14:00:00Z")
    assert f.tracked_changes(old, BY_ROLE["open_undecided_full"], text_max_chars=TEXT_MAX) == []
    opp = _row("open_undecided_full")
    opp["jobWalkAt"] = None
    opp["clientValues"]["jobWalkAt"] = None
    opp["submissionState"] = "WILL_SUBMIT"
    changes = f.tracked_changes(old, opp, text_max_chars=TEXT_MAX)
    assert [c["field"] for c in changes] == ["job_walk_at", "submission_state"]
    assert changes[0]["new"] is None
    assert changes[1] == {"field": "submission_state", "old": "UNDECIDED", "new": "WILL_SUBMIT"}
    # A brand-new row (no stored values) reports every populated field once.
    fresh = f.tracked_changes({}, BY_ROLE["open_undecided_min"], text_max_chars=TEXT_MAX)
    assert {c["field"] for c in fresh} == {
        "close_at", "title", "submission_state", "address", "gc_external_id", "gc_external_name",
    }


def test_tracked_changes_report_a_gc_swap_and_a_gc_rename():
    """Fix round 3: the client company swapped on the board is two entries
    (the id and the name, as invitation_fields writes them); the same
    company renamed is the name alone; an NDA row that stays masked is
    none."""
    old = _inv("open_undecided_full")
    swapped = _row("open_undecided_full")
    swapped["client"]["company"] = {"id": "ccec8ba311ac03a51604bdd1", "name": "GC AO Builders"}
    changes = f.tracked_changes(old, swapped, text_max_chars=TEXT_MAX)
    assert changes == [
        {"field": "gc_external_id", "old": "ab41ccb9140a2eb134ce6fa6", "new": "ccec8ba311ac03a51604bdd1"},
        {"field": "gc_external_name", "old": "GC AA Builders", "new": "GC AO Builders"},
    ]
    renamed = _row("open_undecided_full")
    renamed["client"]["company"]["name"] = "  GC AA Builders   Inc "
    assert f.tracked_changes(old, renamed, text_max_chars=TEXT_MAX) == [
        {"field": "gc_external_name", "old": "GC AA Builders", "new": "GC AA Builders Inc"},
    ]
    masked = _inv("nda_masked")
    assert masked["gc_external_id"] is None and masked["gc_external_name"] is None
    assert f.tracked_changes(masked, _row("nda_masked"), text_max_chars=TEXT_MAX) == []
    lifted = _row("nda_masked")
    lifted["client"] = _row("open_undecided_full")["client"]
    assert [c["field"] for c in f.tracked_changes(masked, lifted, text_max_chars=TEXT_MAX)] == [
        "gc_external_id", "gc_external_name",
    ]


# ── project_facts (D17, 3.7) ────────────────────────────────────────────────


def test_project_facts_key_set_and_full_row():
    row = _inv("open_undecided_full")
    facts = f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert set(facts) == f.PROJECT_FACT_KEYS
    assert set(facts) == {
        "name", "actual_bid_at", "bid_time_unknown", "invitation_at", "job_walk_at",
        "est_start_date", "est_finish_date", "address", "bidding_url", "project_information",
        "trade_instructions", "bid_notes", "notes", "is_budgetary", "sender_display",
        "gc_external_name", "trade_name",
    }
    link = "https://app.buildingconnected.com/opportunities/45dcb8142be69abb6affe312/info"
    assert facts["name"] == "Fixture Project 01"
    assert facts["actual_bid_at"] == "2026-10-16T23:00:00+00:00"
    assert facts["bid_time_unknown"] is False
    assert facts["invitation_at"] == "2026-09-16T18:02:10.048000+00:00"
    assert facts["job_walk_at"] == "2026-09-24T14:00:00+00:00"
    assert facts["est_start_date"] == "2027-01-04"
    assert facts["est_finish_date"] == "2027-03-15"
    assert facts["address"] == "100 Main St, Las Vegas, NV 89101, United States of America"
    assert facts["bidding_url"] == link
    assert facts["project_information"].startswith("lorem lorem lorem lorem, lorem, lorem lorem lorem lorem lorem\n")
    assert "\n" in facts["project_information"]
    assert "<" not in facts["project_information"]
    assert facts["trade_instructions"] is None
    assert facts["bid_notes"] == facts["project_information"][:400].rstrip()
    assert len(facts["bid_notes"]) <= 400
    assert facts["notes"] == (
        f"Created from BuildingConnected: GC AA Builders, package Electrical, invited 2026-09-16. {link}"
    )
    assert facts["is_budgetary"] is False
    assert facts["sender_display"] == "Lead Person00 <lead00@gcaabuilders.example.com>"
    assert facts["gc_external_name"] == "GC AA Builders"
    assert facts["trade_name"] == "Electrical"


def test_project_facts_pacific_dates_cross_the_utc_day():
    row = _inv(
        "open_undecided_full",
        expected_start_at="2027-01-05T02:00:00Z",     # 18:00 PST on the 4th
        expected_finish_at="2027-03-16T05:30:00Z",    # 22:30 PDT on the 15th
        job_walk_at="2026-09-25T03:00:00Z",
    )
    facts = f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["est_start_date"] == "2027-01-04"
    assert facts["est_finish_date"] == "2027-03-15"
    assert facts["job_walk_at"] == "2026-09-25T03:00:00+00:00"   # timestamptz, not a date


def test_project_facts_midnight_rule_sets_bid_time_unknown():
    row = _inv("open_undecided_full", close_at="2026-10-16T07:00:00Z")   # 00:00 PDT
    facts = f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["bid_time_unknown"] is True
    assert facts["actual_bid_at"] == "2026-10-16T07:00:00+00:00"
    row = _inv("open_undecided_full", close_at="2026-12-16T08:00:00Z")   # 00:00 PST
    assert f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)["bid_time_unknown"] is True
    row = _inv("open_undecided_full", close_at="2026-10-16T00:00:00Z")   # 17:00 PDT the day before
    assert f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)["bid_time_unknown"] is False


def test_project_facts_every_null_on_the_masked_row():
    row = _inv("nda_masked")
    facts = f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["name"] == "Fixture Project 08"
    assert facts["actual_bid_at"] == "2026-09-16T17:00:00+00:00"
    assert facts["bid_time_unknown"] is False
    assert facts["invitation_at"] == "2026-09-02T19:48:25.488000+00:00"   # createdAt fallback
    assert facts["job_walk_at"] is None
    assert facts["est_start_date"] is None and facts["est_finish_date"] is None
    assert facts["address"] is None
    assert facts["bidding_url"] == "https://app.buildingconnected.com/opportunities/b619a027455438b5c25df7da/info"
    assert facts["project_information"] is None
    assert facts["trade_instructions"] is None
    assert facts["bid_notes"] is None
    assert facts["notes"] == (
        "Created from BuildingConnected: (NDA), package Electrical, invited 2026-09-02. "
        "https://app.buildingconnected.com/opportunities/b619a027455438b5c25df7da/info"
    )
    assert facts["is_budgetary"] is False
    assert facts["sender_display"] is None
    assert facts["gc_external_name"] is None
    assert facts["trade_name"] == "Electrical"


def test_project_facts_no_due_date_and_budget_suffix():
    facts = f.project_facts(_inv("budget_request"), text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["actual_bid_at"] is None
    assert facts["bid_time_unknown"] is False
    assert facts["is_budgetary"] is True
    assert facts["invitation_at"] == "2026-08-28T19:28:55.874000+00:00"
    facts = f.project_facts(_inv("no_due_real"), text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["actual_bid_at"] is None and facts["is_budgetary"] is False


def test_project_facts_bid_notes_prefer_trade_instructions_and_cap_at_400():
    row = _inv("with_trade_instructions")
    facts = f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["trade_instructions"].startswith("lorem lorem lorem")
    assert facts["project_information"] is not None
    assert facts["bid_notes"] == facts["trade_instructions"][:400].rstrip()
    assert len(facts["bid_notes"]) <= 400
    assert facts["bid_notes"] != facts["project_information"][:400].rstrip()
    assert f.BID_NOTES_MAX_CHARS == 400
    # A short trade text is kept whole; whitespace-only trade text falls back.
    short = _inv("with_trade_instructions")
    short["payload"]["tradeSpecificInstructions"] = "<div>Bring boots.</div>"
    short["payload"]["clientValues"]["tradeSpecificInstructions"] = None
    facts = f.project_facts(short, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["trade_instructions"] == "Bring boots."
    assert facts["bid_notes"] == "Bring boots."
    notice = _inv("no_due_notice")     # tradeSpecificInstructions is "<br />"
    facts = f.project_facts(notice, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["trade_instructions"] is None
    assert facts["bid_notes"] == facts["project_information"][:400].rstrip()


def test_project_facts_text_cap_and_notes_cap():
    row = _inv("open_undecided_full")
    facts = f.project_facts(row, text_max_chars=120, notes_max_chars=40)
    assert facts["project_information"].endswith(" [truncated]")
    assert len(facts["project_information"]) <= 120
    assert len(facts["notes"]) <= 40
    assert facts["notes"].startswith("Created from BuildingConnected")


def test_project_facts_sender_display_without_a_name_and_without_email():
    row = _inv("open_undecided_full", lead={"first_name": None, "last_name": None, "email": "x@gc.example", "phone": None})
    assert f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)["sender_display"] == "x@gc.example"
    row = _inv("open_undecided_full", lead={"first_name": "Only", "last_name": "Phone", "email": None, "phone": "702"})
    assert f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)["sender_display"] is None


def test_project_facts_without_a_payload_still_maps_the_columns():
    row = _inv("open_undecided_full")
    row["payload"] = None
    facts = f.project_facts(row, text_max_chars=TEXT_MAX, notes_max_chars=NOTES_MAX)
    assert facts["name"] == "Fixture Project 01"
    assert facts["project_information"] is None
    assert facts["trade_instructions"] is None
    assert facts["bid_notes"] is None
    assert facts["sender_display"] == "Lead Person00 <lead00@gcaabuilders.example.com>"
    assert facts["bidding_url"].endswith("/45dcb8142be69abb6affe312/info")
