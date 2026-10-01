"""The BuildingConnected branch of project creation and the project side
(app/services/rfp_create, app/services/files_needed, app/routers/projects,
app/routers/rfp_created; docs/RFP_BUILDINGCONNECTED.md 3.4, 3.7, 3.8 and
scratchpad BUILD_CONTRACT.md D14 to D20, D32, 3.5) against the in-memory
fakes, with agent E's `rfp_bc_portal.confirm_gc` stood in.

Pinned:

- `facts_for_bc` over the anonymised fixtures (tests/fixtures_bc): the
  full row, the minimal row (every optional null), the masked NDA row, the
  BUDGET request, the no-due-date row and a foreign row without
  clientValues; the deep link as the bidding link; the newlines kept;
- `gc_plan_for_bc` (alias/contact/domain -> resolved, provisional, none);
- the provisional plan: the GC linked like `resolved` but the lead NEVER
  filed as its contact (not inserted, not attached),
  `projects.gc_confirm_pending` true, `rfp_created_projects.gc_plan`
  'provisional' with the likely_gc_* pair filled;
- `ensure_lead_contact`: the case-insensitive literal lookup (oldest
  wins), the insert with "First Last", the company-name fallback, the
  email-less lead;
- the compensation taking back ONLY the contact this call inserted;
- the B suffix threaded through `_insert_project(budgetary=)`;
- `create_from_portal` dispatching by portal (NGEM untouched), the files
  flag raised at creation, the sender display, `_check_portal`'s
  BuildingConnected refusals and `portal_create_available`'s rule;
- `apply_project_dates` (PATCH semantics, Pacific dates, never
  internal_bid_at, the intake settlement, the audit) and
  `swap_project_gc` (removal by id, kept beside a sent proposal, needs_by
  carried, the lead attached, the pending flag cleared, the lead's stray
  contact under the old GC deleted only when the link went, the creation
  made it (created at or after the project) and nothing else refers to it);
- files_needed set / has / clear / dismiss, and the four insert-path hooks
  (upload, promotion, split, draft transfer) each best effort;
- the projects router: `_FIELD_EDITORS`, the dismiss route, the GC card's
  GET and POST (delegation, refusals, wiring), the portal sources gate,
  the per-role redaction, the created summary;
- the Created page: the BuildingConnected flags, missing_files suppressed,
  the gate opening on `rfp_portal_any_enabled`;
- the schemas: ProjectUpdate caps, ProjectOut defaults, RfpCreatedSummary,
  PortalSourceOut and the GC-confirm body.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core import features
from app.core.config import Settings
from app.core.deps import CurrentUser, require_writer
from app.core.roles import Role
from app.models.schemas import (
    PortalSourceOut,
    ProjectGcConfirmIn,
    ProjectOut,
    ProjectUpdate,
    RfpCreatedSummary,
)
from app.routers import projects as pr
from app.routers import rfp_created as rr
from app.services import bc_facts, files_needed, llm_queue, rfp_create as rc
from app.services import rfp_split
from app.services import rfp_create_files as rcf
from app.services.proposal_send import ProposalSendError
from tests.test_rfp_create import NEXT, CreateDB
from tests.test_rfp_email_ingest import FakeDB, _Query

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
FIXTURES = json.loads((Path(__file__).parent / "fixtures_bc" / "opportunities.json").read_text())
BY_ROLE = {row["_fixture_role"]: row for row in FIXTURES}
P1 = "5a000000-0000-4000-8000-000000000001"
INV = "9c000000-0000-4000-8000-000000000001"
TEXT_MAX = 20000


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _inv(role_key: str, **over) -> dict:
    """An rfp_portal_invitations row as the scan writes it for a fixture,
    plus the GC resolution columns the match step adds."""
    opp = BY_ROLE[role_key]
    row = {
        "id": INV,
        "portal": "buildingconnected",
        "status": "done",
        "flag_reason": None,
        "harvest_id": None,
        "created_project_id": None,
        "match_project_id": None,
        "first_seen_at": NOW.isoformat(),
        "gc_id": "gc-1",
        "gc_kind": "alias",
        "gc_candidates": [],
        "gc_confirmed_at": None,
        **bc_facts.invitation_fields(opp, NOW, text_max_chars=TEXT_MAX),
    }
    row.update(over)
    return row


# ── The creation fake and its seams ──────────────────────────────────────


@pytest.fixture
def db():
    fake = CreateDB({
        "rfp_emails": [],
        "rfp_portal_invitations": [],
        "rfp_harvests": [],
        "rfp_created_projects": [],
        "projects": [],
        "project_gcs": [],
        "project_gc_contacts": [],
        "project_files": [],
        "stage_events": [],
        "project_category_state": [],
        "general_contractors": [{"id": "gc-1", "name": "GC AA Builders"},
                                {"id": "gc-2", "name": "Rafael Companies"}],
        "gc_contacts": [{"id": "c1", "gc_id": "gc-1", "email": "PM@GC.example", "name": "GC PM",
                         "created_at": "2026-01-01T00:00:00+00:00"},
                        {"id": "c1b", "gc_id": "gc-1", "email": "pm@gc.example", "name": "Newer copy",
                         "created_at": "2026-02-01T00:00:00+00:00"},
                        {"id": "c2", "gc_id": "gc-2", "email": "pm@gc.example", "name": "Other GC's PM",
                         "created_at": "2026-01-01T00:00:00+00:00"}],
        "llm_jobs": [],
        "audit_log": [],
        "notifications": [],
        "proposal_sends": [],
    })
    fake.unique = {**FakeDB.unique, "rfp_created_projects": [("project_id",)],
                   "project_gc_contacts": [("project_gc_id", "gc_contact_id")]}
    fake.fk_cascade = {
        **FakeDB.fk_cascade,
        "projects": [("rfp_created_projects", "project_id"), ("project_gcs", "project_id"),
                     ("stage_events", "project_id"), ("project_category_state", "project_id")],
    }
    fake.defaults = {
        **FakeDB.defaults,
        "llm_jobs": {"status": lambda: "queued", "attempts": lambda: 0, "created_at": lambda: NOW.isoformat()},
        "rfp_created_projects": {"created_at": lambda: NOW.isoformat()},
        "projects": {"created_at": lambda: NOW.isoformat()},
        "gc_contacts": {"created_at": lambda: NOW.isoformat()},
    }
    fake.fail_insert = set()
    fake.inserts = []
    return fake


@pytest.fixture
def rec():
    return {"advance": [], "entry": [], "bells": [], "audits": [], "rescans": [], "numbers": [],
            "budgetary": [], "dismissed": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch, db, rec):
    settings = Settings(_env_file=None, rfp_ingest_enabled=True, llm_queue_enabled=True,
                        rfp_create_auto_enabled=True)
    monkeypatch.setattr(rc, "get_settings", lambda: settings)
    monkeypatch.setattr(rc, "_now", lambda: NOW)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: settings)
    monkeypatch.setattr(rc.workflow, "advance_category",
                        lambda pid, cat, actor, note=None: rec["advance"].append((pid, cat, actor, note)) or {})
    monkeypatch.setattr(rc.gono, "apply_entry_action",
                        lambda pid, actor, action: rec["entry"].append((pid, actor, action)) or (None, None))
    monkeypatch.setattr(
        rc, "notify_role",
        lambda role, pid, type_, message, **k: rec["bells"].append(
            {"role": role, "project_id": pid, "type": type_, "message": message, "metadata": k.get("metadata")}),
    )
    monkeypatch.setattr(
        rc, "audit",
        lambda actor, action, entity, entity_id, payload=None: rec["audits"].append(
            {"actor_id": actor, "action": action, "entity": entity, "entity_id": entity_id, "payload": payload}),
    )
    monkeypatch.setattr(rc, "dismiss_notifications", lambda **k: rec["dismissed"].append(k))
    monkeypatch.setattr(rc, "_start_rescan", lambda pid: rec["rescans"].append(pid))

    def insert_project(sb, payload, *, budgetary=False):
        NEXT["counter"] += 1
        payload["number"] = f"26.9.{NEXT['counter']:04d}{'B' if budgetary else ''}"
        rec["numbers"].append(payload["number"])
        rec["budgetary"].append(budgetary)
        return sb.table("projects").insert(payload).execute().data[0]

    monkeypatch.setattr(rc, "_insert_project", insert_project)
    NEXT["counter"] = 7203


def _projects(db):
    return db.tables["projects"]


def _created(db):
    return db.tables["rfp_created_projects"]


def _seed_inv(db, role_key: str, **over) -> dict:
    row = _inv(role_key, **over)
    db.tables["rfp_portal_invitations"].append(row)
    return db.tables["rfp_portal_invitations"][-1]


# ── facts_for_bc (D17, design 3.7) ───────────────────────────────────────


def test_facts_for_the_full_fixture_row():
    inv = _inv("open_undecided_full")
    facts = rc.facts_for_bc(inv, text_max_chars=TEXT_MAX, notes_max_chars=4000)
    opp = BY_ROLE["open_undecided_full"]
    link = f"https://app.buildingconnected.com/opportunities/{opp['id']}/info"
    assert facts.name == "Fixture Project 01"
    assert facts.actual_bid_at == "2026-10-16T23:00:00+00:00" and facts.bid_time_unknown is False
    assert facts.invitation_at == bc_facts.parse_ts(opp["invitedAt"]).isoformat()
    assert facts.job_walk_at == bc_facts.parse_ts(opp["jobWalkAt"]).isoformat()
    assert facts.est_start_date == bc_facts.pacific_date(opp["expectedStartAt"]).isoformat()
    assert facts.est_finish_date == bc_facts.pacific_date(opp["expectedFinishAt"]).isoformat()
    assert facts.address == inv["address"] == "100 Main St, Las Vegas, NV 89101, United States of America"
    assert facts.bidding_url == link and facts.no_bidding_url is False
    assert facts.is_ngem is False and facts.is_budgetary is False
    assert facts.project_information and facts.trade_instructions is None
    # The text blocks keep their lines (never through rfp_create._text), and
    # the bid notes are the first 400 characters of the information block
    # when the row has no trade instructions.
    assert "\n" in facts.project_information
    assert facts.bid_notes == facts.project_information[:400].rstrip() and len(facts.bid_notes) <= 400
    assert facts.notes.startswith("Created from BuildingConnected: GC AA Builders, package ")
    assert facts.notes.endswith(link)
    assert facts.files_needed_source == "buildingconnected" and facts.files_needed_url == link
    assert facts.sender_display == "Lead Person00 <lead00@gcaabuilders.example.com>"
    assert facts.gc_confirm_pending is False


def test_facts_prefer_the_trade_instructions_for_the_bid_notes():
    inv = _inv("with_trade_instructions")
    facts = rc.facts_for_bc(inv, text_max_chars=TEXT_MAX, notes_max_chars=4000)
    assert facts.trade_instructions and facts.project_information
    expected = facts.trade_instructions if len(facts.trade_instructions) <= 400 else facts.trade_instructions[:400].rstrip()
    assert facts.bid_notes == expected


def test_facts_for_the_minimal_row_leave_every_optional_null():
    inv = _inv("open_undecided_min")
    facts = rc.facts_for_bc(inv, text_max_chars=TEXT_MAX, notes_max_chars=4000)
    assert facts.name == "Fixture Project 03" and facts.actual_bid_at == "2026-09-30T22:00:00+00:00"
    assert facts.job_walk_at is None and facts.est_start_date is None and facts.est_finish_date is None
    assert facts.trade_instructions is None
    assert facts.bid_notes == (facts.project_information[:400].rstrip() if facts.project_information else None)
    assert facts.bidding_url and facts.no_bidding_url is False


def test_facts_for_the_masked_nda_row_are_all_null_but_the_link():
    inv = _inv("nda_masked", gc_id=None, gc_kind="none")
    facts = rc.facts_for_bc(inv, text_max_chars=TEXT_MAX, notes_max_chars=4000)
    assert facts.name == "Fixture Project 08"
    assert facts.address is None and facts.project_information is None and facts.trade_instructions is None
    assert facts.bid_notes is None and facts.sender_display is None
    assert facts.job_walk_at is None and facts.est_start_date is None
    # No invitedAt on the masked shape: the notes fall back to createdAt's day.
    assert facts.notes.startswith("Created from BuildingConnected: (NDA), package Electrical, invited 2026-09-02. ")
    assert facts.bidding_url and facts.no_bidding_url is False


def test_facts_for_the_budget_request_and_the_no_due_date_rows():
    budget = rc.facts_for_bc(_inv("budget_request"), text_max_chars=TEXT_MAX, notes_max_chars=4000)
    assert budget.is_budgetary is True and budget.actual_bid_at is None and budget.bid_time_unknown is False
    notice = rc.facts_for_bc(_inv("no_due_notice"), text_max_chars=TEXT_MAX, notes_max_chars=4000)
    assert notice.actual_bid_at is None and notice.is_budgetary is False


def test_facts_for_a_foreign_row_without_client_values_and_a_midnight_due_date():
    foreign = rc.facts_for_bc(_inv("foreign_manual"), text_max_chars=TEXT_MAX, notes_max_chars=4000)
    assert foreign.name == "Fixture Project 13" and foreign.actual_bid_at == "2025-02-12T20:00:00+00:00"
    assert foreign.sender_display is None and foreign.job_walk_at == "2025-01-22T19:00:00+00:00"
    assert "invited 2025-02-12. " in foreign.notes
    # A due date the GC typed without a time (midnight Pacific) is "time unknown".
    midnight = _inv("open_undecided_min", close_at="2026-10-01T07:00:00+00:00")
    facts = rc.facts_for_bc(midnight, text_max_chars=TEXT_MAX, notes_max_chars=4000)
    assert facts.bid_time_unknown is True


def test_facts_cap_the_notes_and_the_text_and_survive_a_bad_link():
    inv = _inv("open_undecided_full", external_url="javascript:alert(1)", view_url=None)
    facts = rc.facts_for_bc(inv, text_max_chars=1200, notes_max_chars=40)
    assert facts.bidding_url is None and facts.no_bidding_url is True and facts.files_needed_url is None
    assert len(facts.notes) <= 40
    assert len(facts.project_information or "") <= 1200


def test_facts_dataclass_defaults_keep_the_other_builders_untouched():
    email = rc.facts_for_email({"extracted_project_name": "X", "received_at": None}, None)
    assert email.job_walk_at is None and email.is_budgetary is False and email.files_needed_source is None
    assert email.gc_confirm_pending is False and email.sender_display is None
    portal = rc.facts_for_portal({"portal": "ngem", "title": "T", "agency": "A", "bid_number": "1"}, None)
    assert portal.is_ngem is True and portal.files_needed_source is None and portal.est_start_date is None


# ── gc_plan_for_bc and the provisional plan (D15) ───────────────────────


@pytest.mark.parametrize("kind", ["alias", "contact", "domain"])
def test_gc_plan_for_a_confirmed_kind_is_resolved(db, kind):
    plan = rc.gc_plan_for_bc(db, _inv("open_undecided_full", gc_kind=kind, gc_id="gc-1"))
    assert plan.kind == rc.GC_RESOLVED and plan.gc_id == "gc-1" and plan.name == "GC AA Builders"
    assert plan.lead == _inv("open_undecided_full")["lead"] and plan.contact_id is None


def test_gc_plan_for_provisional_and_none(db):
    plan = rc.gc_plan_for_bc(db, _inv("open_undecided_full", gc_kind="provisional", gc_id="gc-2"))
    assert plan.kind == rc.GC_PROVISIONAL and plan.gc_id == "gc-2" and plan.name == "Rafael Companies"
    assert rc.gc_plan_for_bc(db, _inv("open_undecided_full", gc_kind="none", gc_id=None)).kind == rc.GC_NONE
    assert rc.gc_plan_for_bc(db, _inv("open_undecided_full", gc_kind=None, gc_id=None)).kind == rc.GC_NONE
    # A kind with no id attaches nothing either.
    assert rc.gc_plan_for_bc(db, _inv("open_undecided_full", gc_kind="alias", gc_id=None)).kind == rc.GC_NONE


def test_provisional_plan_links_the_gc_without_the_lead_and_asks_on_the_project(db, rec):
    inv = _seed_inv(db, "open_undecided_full", gc_kind="provisional", gc_id="gc-2",
                    gc_candidates=[{"gc_id": "gc-2", "name": "Rafael Companies", "score": 0.61}])
    before = [dict(c) for c in db.tables["gc_contacts"]]
    made = rc.create_from_portal(db, inv, actor_id="u1", automatic=False)
    (project,) = _projects(db)
    assert made.project_id == project["id"] and made.linked is False and made.files_job is False
    assert project["gc_confirm_pending"] is True
    (link,) = db.tables["project_gcs"]
    assert link["gc_id"] == "gc-2" and link["needs_by"] == "2026-10-16"
    # The lead is never filed under a guessed GC: no gc_contacts row is made
    # (it would make gc_aliases resolve the company to gc-2 as CONFIRMED
    # through the contact address) and no bid contact is attached. The GC
    # card's answer (swap_project_gc) attaches it.
    lead = inv["lead"]
    assert db.tables["gc_contacts"] == before
    assert not any(c["email"] == lead["email"] for c in db.tables["gc_contacts"])
    assert db.tables["project_gc_contacts"] == []
    (crec,) = _created(db)
    assert crec["gc_plan"] == "provisional" and crec["likely_gc_id"] == "gc-2"
    assert crec["likely_gc_name"] == "Rafael Companies" and crec["invitation_method"] == "buildingconnected"
    assert crec["sender_display"] == f"{lead['first_name']} {lead['last_name']} <{lead['email']}>"
    assert crec["portal_invitation_id"] == INV and crec["harvest_id"] is None and crec["files_status"] == "none"
    assert inv["status"] == "created" and inv["created_project_id"] == project["id"]


def test_resolved_plan_leaves_gc_confirm_pending_false_and_reuses_the_oldest_contact(db, rec):
    inv = _seed_inv(db, "open_undecided_full", gc_kind="alias", gc_id="gc-1",
                    lead={"first_name": "Pat", "last_name": "Lee", "email": "pm@gc.example", "phone": None})
    rc.create_from_portal(db, inv, actor_id=None, automatic=True)
    (project,) = _projects(db)
    assert project["gc_confirm_pending"] is False
    (pgc,) = db.tables["project_gc_contacts"]
    assert pgc["gc_contact_id"] == "c1"   # the oldest row with that address, case folded
    assert len([c for c in db.tables["gc_contacts"] if c["gc_id"] == "gc-1"]) == 2   # nothing inserted
    assert _created(db)[0]["gc_plan"] == "resolved" and _created(db)[0]["likely_gc_id"] is None


def test_null_gc_kind_is_refused_like_none_on_both_create_paths(db, rec):
    """A row parked before its GC was resolved (no project name, the
    NDA-masked shape) never had gc_kind written. After a reviewer's "Not a
    match" it sits at `create` with resolution `human`, which skips the
    sweep's gate, so the create paths must read the NULL as `none`:
    otherwise the project is made with no GC although the platform named
    one and an alias may already map it."""
    inv = _seed_inv(db, "open_undecided_full", status="create", resolution="human",
                    flag_reason="no_project_name", gc_kind=None, gc_id=None, gc_candidates=None)
    # The sweep's inline create after "Not a match" (automatic) ...
    with pytest.raises(rc.CreateRefused) as exc:
        rc.create_from_portal(db, inv, actor_id=None, automatic=True)
    assert "No general contractor" in str(exc.value)
    # ... and the Create button (manual) both refuse, and the button is hidden.
    with pytest.raises(rc.CreateRefused):
        rc.create_from_portal(db, inv, actor_id="u1", automatic=False)
    assert rc.portal_create_available(inv) is False
    assert _projects(db) == [] and db.tables["project_gcs"] == [] and _created(db) == []


# ── ensure_lead_contact (D16) ────────────────────────────────────────────


def test_lead_contact_lookup_is_case_insensitive_literal_and_oldest_wins(db):
    assert rc.ensure_lead_contact(db, "gc-1", {"email": "Pm@Gc.Example"}) == ("c1", False)
    # The wildcard characters of LIKE are matched as literals.
    db.tables["gc_contacts"].append({"id": "c3", "gc_id": "gc-1", "email": "firstXlast@gc.example",
                                     "name": "Not the one", "created_at": "2025-01-01T00:00:00+00:00"})
    cid, inserted = rc.ensure_lead_contact(db, "gc-1", {"email": "first_last@gc.example", "first_name": "F",
                                                       "last_name": "L"})
    assert inserted is True and cid != "c3"
    made = next(c for c in db.tables["gc_contacts"] if c["id"] == cid)
    assert made == {"id": cid, "gc_id": "gc-1", "name": "F L", "email": "first_last@gc.example",
                    "phone": None, "created_at": NOW.isoformat()}


def test_lead_contact_insert_falls_back_to_the_company_name_and_keeps_the_phone(db):
    cid, inserted = rc.ensure_lead_contact(db, "gc-2", {"email": "New@Other.example", "phone": " 702 555 0100 "})
    assert inserted is True
    made = next(c for c in db.tables["gc_contacts"] if c["id"] == cid)
    assert made["name"] == "Rafael Companies" and made["email"] == "new@other.example"
    assert made["phone"] == "702 555 0100"


def test_lead_without_an_email_is_nobody(db):
    before = len(db.tables["gc_contacts"])
    assert rc.ensure_lead_contact(db, "gc-1", {"first_name": "Phone", "last_name": "Only", "phone": "1"}) == (None, False)
    assert rc.ensure_lead_contact(db, "gc-1", None) == (None, False)
    assert rc.ensure_lead_contact(db, "gc-1", {"email": "   "}) == (None, False)
    assert len(db.tables["gc_contacts"]) == before


# ── Compensation (D16 last sentence) ─────────────────────────────────────


def test_compensation_deletes_only_the_contact_this_call_inserted(db, rec):
    # A confirmed GC whose contacts do not hold the lead: the create inserts it.
    inv = _seed_inv(db, "open_undecided_full", gc_kind="alias", gc_id="gc-2")
    db.fail_insert = {"stage_events"}
    with pytest.raises(RuntimeError):
        rc.create_from_portal(db, inv, actor_id=None, automatic=True)
    assert _projects(db) == [] and _created(db) == [] and db.tables["project_gcs"] == []
    assert "gc_contacts" in db.inserts   # the lead contact was made, then taken back
    # The inserted lead contact is gone; every pre-existing contact stands.
    assert {c["id"] for c in db.tables["gc_contacts"]} == {"c1", "c1b", "c2"}
    assert [a["action"] for a in rec["audits"]] == ["rfp_create.create_failed"]
    assert inv["status"] == "done"


def test_provisional_compensation_has_no_contact_to_take_back(db, rec):
    inv = _seed_inv(db, "open_undecided_full", gc_kind="provisional", gc_id="gc-2")
    db.fail_insert = {"stage_events"}
    with pytest.raises(RuntimeError):
        rc.create_from_portal(db, inv, actor_id=None, automatic=True)
    assert "gc_contacts" not in db.inserts
    assert {c["id"] for c in db.tables["gc_contacts"]} == {"c1", "c1b", "c2"}
    assert _projects(db) == [] and db.tables["project_gcs"] == []


def test_provisional_then_confirmed_same_gc_files_the_lead_only_on_the_answer(db, rec, proposal_stub):
    """The dev case of 2026-09-28: a provisional create must leave nothing
    that later reads as a confirmed contact match; swap_project_gc (the GC
    card's swap, which is idempotent for "same GC") is where the lead
    becomes the GC's contact."""
    proposal_stub["db"] = db
    inv = _seed_inv(db, "open_undecided_full", gc_kind="provisional", gc_id="gc-2")
    rc.create_from_portal(db, inv, actor_id="u1", automatic=False)
    (project,) = _projects(db)
    lead = inv["lead"]
    assert not any(c["email"] == lead["email"] for c in db.tables["gc_contacts"])
    out = rc.swap_project_gc(db, project["id"], "gc-2", "gc-2", lead, "u1")
    (link,) = db.tables["project_gcs"]
    assert out["project_gc_id"] == link["id"] and out["removed_old"] is False
    (made,) = [c for c in db.tables["gc_contacts"] if c["email"] == lead["email"]]
    assert made["gc_id"] == "gc-2"
    (pgc,) = db.tables["project_gc_contacts"]
    assert pgc["project_gc_id"] == link["id"] and pgc["gc_contact_id"] == made["id"]
    assert db.tables["projects"][0]["gc_confirm_pending"] is False


def test_compensation_never_deletes_a_pre_existing_contact(db, rec):
    inv = _seed_inv(db, "open_undecided_full", gc_kind="alias", gc_id="gc-1",
                    lead={"first_name": "Pat", "last_name": "Lee", "email": "pm@gc.example", "phone": None})
    db.fail_insert = {"project_category_state"}
    with pytest.raises(RuntimeError):
        rc.create_from_portal(db, inv, actor_id=None, automatic=True)
    assert {c["id"] for c in db.tables["gc_contacts"]} == {"c1", "c1b", "c2"}
    assert _projects(db) == []


# ── The B suffix (D14) ───────────────────────────────────────────────────


def test_budget_request_threads_budgetary_into_the_numbered_insert(db, rec):
    inv = _seed_inv(db, "budget_request", gc_kind="alias", gc_id="gc-1")
    made = rc.create_from_portal(db, inv, actor_id=None, automatic=True)
    assert rec["budgetary"] == [True] and made.number.endswith("B")
    assert _projects(db)[0]["number"] == made.number
    # A proposal request is a plain number.
    inv2 = _seed_inv(db, "open_undecided_min", id="inv-2", gc_kind="alias", gc_id="gc-1")
    made2 = rc.create_from_portal(db, inv2, actor_id=None, automatic=True)
    assert rec["budgetary"] == [True, False] and not made2.number.endswith("B")


def test_insert_project_passes_budgetary_to_the_numbering_module(db, monkeypatch):
    from app.services import project_numbers
    from tests.test_rfp_create import _REAL_INSERT_PROJECT

    seen = []
    monkeypatch.setattr(project_numbers, "insert_with_assigned_number",
                        lambda sb, payload, *, budgetary: seen.append(budgetary) or {"id": "p", **payload})
    # The autouse seam stands the module's function in; the real one is the
    # test file's frozen import.
    assert rc._insert_project is not _REAL_INSERT_PROJECT
    _REAL_INSERT_PROJECT(db, {"name": "x"}, budgetary=True)
    _REAL_INSERT_PROJECT(db, {"name": "y"})
    assert seen == [True, False]


# ── create_from_portal dispatch, the payload and the files flag ──────────


def test_create_from_portal_bc_branch_writes_the_new_columns_and_raises_the_files_flag(db, rec):
    inv = _seed_inv(db, "open_undecided_full", gc_kind="alias", gc_id="gc-1")
    opp = BY_ROLE["open_undecided_full"]
    rc.create_from_portal(db, inv, actor_id="u1", automatic=False)
    (project,) = _projects(db)
    link = f"https://app.buildingconnected.com/opportunities/{opp['id']}/info"
    assert project["is_ngem"] is False and project["bidding_url"] == link and project["no_bidding_url"] is False
    assert project["job_walk_at"] == bc_facts.parse_ts(opp["jobWalkAt"]).isoformat()
    assert project["est_start_date"] == bc_facts.pacific_date(opp["expectedStartAt"]).isoformat()
    assert project["est_finish_date"] == bc_facts.pacific_date(opp["expectedFinishAt"]).isoformat()
    assert "\n" in project["project_information"] and project["trade_instructions"] is None
    assert project["files_needed_source"] == "buildingconnected" and project["files_needed_url"] == link
    assert project["files_needed_set_at"] == NOW.isoformat() and project.get("files_needed_cleared_at") is None
    assert project["gc_confirm_pending"] is False and project["is_rebid"] is False
    assert project["notes"].startswith("Created from BuildingConnected: GC AA Builders")
    # No harvest, no files job, no split: the record says so.
    assert db.tables["llm_jobs"] == [] and _created(db)[0]["split_status"] is None
    assert rec["bells"][0]["metadata"]["source_kind"] == "rfp_portal"
    assert rec["audits"][-1]["action"] == "rfp_create.create"


def test_create_from_portal_ngem_branch_is_untouched(db, rec):
    inv = {"id": "inv-n", "portal": "ngem", "agency": "UNLV", "bid_number": "5584-GS",
           "bid_number_raw": "5584-GS Addendum 1", "title": "Emergency Phone Tower Upgrades",
           "close_at": "2026-10-03T21:00:00+00:00", "first_seen_at": "2026-09-14T13:30:00+00:00",
           "status": "create", "harvest_id": None, "created_project_id": None}
    db.tables["rfp_portal_invitations"].append(inv)
    rc.create_from_portal(db, db.tables["rfp_portal_invitations"][0], actor_id=None, automatic=True)
    (project,) = _projects(db)
    assert project["is_ngem"] is True and project["no_bidding_url"] is True
    assert project["notes"] == "Created from NGEM UNLV bid 5584-GS Addendum 1"
    assert project.get("files_needed_source") is None and project["gc_confirm_pending"] is False
    assert project["job_walk_at"] is None and project["project_information"] is None
    assert _created(db)[0]["invitation_method"] == "ngem" and _created(db)[0]["gc_plan"] == "none"
    assert rec["budgetary"] == [False]


@pytest.mark.parametrize(
    "over, words",
    [
        ({"flag_reason": "nda_required"}, "NDA"),
        ({"gc_kind": "none", "gc_id": None}, "No general contractor"),
        ({"gc_kind": None, "gc_id": None}, "No general contractor"),
        ({"gc_kind": None, "gc_id": "gc-1"}, "No general contractor"),
        ({"status": "review_match"}, "not at a stage"),
        ({"status": "created", "created_project_id": "p"}, "already created"),
        ({"title": "  "}, "no project name"),
    ],
)
def test_check_portal_refuses_the_bc_shapes(db, over, words):
    inv = _seed_inv(db, "open_undecided_full", **over)
    with pytest.raises(rc.CreateRefused) as exc:
        rc.create_from_portal(db, inv, actor_id="u1", automatic=False)
    assert words in str(exc.value)
    assert _projects(db) == []


def test_portal_create_available_bc_rule_and_ngem_rule():
    base = _inv("open_undecided_full", status="create")
    assert rc.portal_create_available(base) is True
    assert rc.portal_create_available({**base, "status": "done"}) is True
    assert rc.portal_create_available({**base, "status": "review_match"}) is False
    assert rc.portal_create_available({**base, "status": "match"}) is False
    assert rc.portal_create_available({**base, "title": ""}) is False
    assert rc.portal_create_available({**base, "created_project_id": "p"}) is False
    assert rc.portal_create_available({**base, "gc_kind": "none"}) is False
    assert rc.portal_create_available({**base, "gc_kind": None}) is False
    assert rc.portal_create_available({**base, "gc_kind": None, "gc_id": None}) is False
    assert rc.portal_create_available({**base, "gc_kind": "provisional"}) is True
    assert rc.portal_create_available({**base, "flag_reason": "nda_required"}) is False
    assert rc.portal_create_available({**base, "flag_reason": "no_due_date"}) is True
    # NGEM keeps its rule: `done` only.
    ngem = {"portal": "ngem", "status": "create", "title": "T", "created_project_id": None}
    assert rc.portal_create_available(ngem) is False
    assert rc.portal_create_available({**ngem, "status": "done"}) is True


# ── apply_project_dates (D20) ────────────────────────────────────────────


def _full_project(pid=P1, **over):
    row = {
        "id": pid, "number": "26.9.7204", "name": "Fixture Project 01", "current_stage": "intake",
        "actual_bid_at": "2026-10-01T21:00:00+00:00", "internal_bid_at": "2026-09-30T00:00:00+00:00",
        "due_from_estimator_at": "2026-09-25T00:00:00+00:00", "due_from_vendors_at": "2026-09-26T00:00:00+00:00",
        "project_type": "ti", "owner_type": "private_commercial", "labor_needed": "union", "bid_method": "hard_bid",
        "competitor_known": "no_unknown", "gc_known": "yes_1_2", "subs_needed": "no", "est_value_band": "150k_500k",
        "scope_fit": "yes", "job_walk_at": None, "est_start_date": None, "est_finish_date": None,
        "gc_confirm_pending": False, "invitation_at": None, "labor_time": None, "wage_type": None,
        "labor_note": None, "notes": None, "created_by": None, "abandoned_at": None,
    }
    row.update(over)
    return row


def test_apply_project_dates_writes_the_ticked_fields_with_patch_semantics(db, rec):
    db.tables["projects"].append(_full_project())
    db.tables["rfp_created_projects"].append({"project_id": P1, "bid_time_unknown": True})
    out = rc.apply_project_dates(
        db, P1,
        {"actual_bid_at": "2026-10-16T23:00:00+00:00", "job_walk_at": "2026-10-05T17:00:00Z",
         "est_start_date": "2026-11-02T07:00:00+00:00", "est_finish_date": "2027-03-15",
         "internal_bid_at": "2026-10-15T00:00:00+00:00", "name": "hacked"},
        "u1", source="buildingconnected",
    )
    (project,) = db.tables["projects"]
    assert out == project
    assert project["actual_bid_at"] == "2026-10-16T23:00:00+00:00"
    assert project["job_walk_at"] == "2026-10-05T17:00:00+00:00"
    # DATE columns take the Pacific calendar day (07:00Z on Nov 2 is Nov 1 in Las Vegas).
    assert project["est_start_date"] == "2026-11-01" and project["est_finish_date"] == "2027-03-15"
    # Never the internal date, never anything else.
    assert project["internal_bid_at"] == "2026-09-30T00:00:00+00:00" and project["name"] == "Fixture Project 01"
    (a,) = rec["audits"]
    assert a["action"] == "project.apply_bc_dates" and a["entity_id"] == P1 and a["actor_id"] == "u1"
    assert a["payload"] == {"actual_bid_at": "2026-10-16T23:00:00+00:00", "job_walk_at": "2026-10-05T17:00:00+00:00",
                            "est_start_date": "2026-11-01", "est_finish_date": "2027-03-15",
                            "source": "buildingconnected"}
    # The intake settlement: the time marker settles (16:00 Pacific is a real
    # time) and, with nothing missing, the intake bell is dismissed.
    assert db.tables["rfp_created_projects"][0]["bid_time_unknown"] is False
    assert rec["dismissed"] == [{"project_id": P1, "types": ["rfp_create.intake_needed"]}]


def test_apply_project_dates_marks_a_midnight_due_date_unknown_and_keeps_the_bell_while_intake_is_open(db, rec):
    db.tables["projects"].append(_full_project(project_type=None))
    db.tables["rfp_created_projects"].append({"project_id": P1, "bid_time_unknown": False})
    rc.apply_project_dates(db, P1, {"actual_bid_at": "2026-10-17T07:00:00+00:00"}, None, source="buildingconnected")
    assert db.tables["rfp_created_projects"][0]["bid_time_unknown"] is True
    assert rec["dismissed"] == []


def test_apply_project_dates_refuses_nothing_and_a_missing_project(db, rec):
    db.tables["projects"].append(_full_project())
    with pytest.raises(ValueError):
        rc.apply_project_dates(db, P1, {"internal_bid_at": "2026-10-15T00:00:00+00:00"}, "u1", source="x")
    with pytest.raises(ValueError):
        rc.apply_project_dates(db, P1, {}, "u1", source="x")
    with pytest.raises(LookupError):
        rc.apply_project_dates(db, "nope", {"job_walk_at": "2026-10-05T17:00:00Z"}, "u1", source="x")
    assert rec["audits"] == []
    # A project the slice did not create has no record to settle; the write still lands.
    out = rc.apply_project_dates(db, P1, {"job_walk_at": None}, "u1", source="x")
    assert out["job_walk_at"] is None and rec["dismissed"] == []


# ── swap_project_gc (design 3.4, contract 3.5) ───────────────────────────


@pytest.fixture
def proposal_stub(monkeypatch):
    """proposal_send.remove_gc_link stood in: records the call, deletes the
    link through the fake's SQL function, or refuses like a sent proposal."""
    state = {"calls": [], "refuse": False}

    def remove_gc_link(project_id, gc_id, *, link_id=None, refuse_if_sent=False, via_unmerge=False):
        state["calls"].append({"project_id": project_id, "gc_id": gc_id, "link_id": link_id,
                               "refuse_if_sent": refuse_if_sent, "via_unmerge": via_unmerge})
        if state["refuse"]:
            raise ProposalSendError("This GC's proposal has already been sent.")
        deleted = state["db"].remove_project_gc_unless_sent(link_id, project_id, gc_id, refuse_if_sent)
        return bool(deleted)

    monkeypatch.setattr(rc, "_proposal_send",
                        lambda: SimpleNamespace(remove_gc_link=remove_gc_link, ProposalSendError=ProposalSendError))
    return state


def test_swap_project_gc_removes_the_old_link_by_id_and_attaches_the_new_gc_with_the_lead(db, rec, proposal_stub):
    proposal_stub["db"] = db
    # The project predates c2, so c2 reads as the contact its creation made.
    db.tables["projects"].append(_full_project(gc_confirm_pending=True, created_at="2025-12-01T00:00:00+00:00"))
    db.tables["project_gcs"].append({"id": "old-link", "project_id": P1, "gc_id": "gc-2", "needs_by": "2026-10-16"})
    db.tables["project_gc_contacts"].append({"id": "x", "project_gc_id": "old-link", "gc_contact_id": "c2"})
    lead = {"first_name": "Pat", "last_name": "Lee", "email": "PM@gc.example", "phone": None}
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", lead, "u1")
    (link,) = db.tables["project_gcs"]
    assert out == {"removed_old": True, "project_gc_id": link["id"], "removed_contact": True}
    # The lead's contact under the old GC went with its only link.
    assert {c["id"] for c in db.tables["gc_contacts"]} == {"c1", "c1b"}
    assert link["gc_id"] == "gc-1" and link["needs_by"] == "2026-10-16"   # carried over
    assert proposal_stub["calls"] == [{"project_id": P1, "gc_id": "gc-2", "link_id": "old-link",
                                       "refuse_if_sent": True, "via_unmerge": True}]
    (pgc,) = db.tables["project_gc_contacts"]   # the old link's contact cascaded away
    assert pgc["project_gc_id"] == link["id"] and pgc["gc_contact_id"] == "c1"
    assert db.tables["projects"][0]["gc_confirm_pending"] is False
    (a,) = rec["audits"]
    assert a["action"] == "project.gc_swap" and a["payload"]["removed_old"] is True
    assert a["payload"]["old_gc_id"] == "gc-2" and a["payload"]["new_gc_id"] == "gc-1"


def test_swap_project_gc_keeps_both_links_when_a_proposal_was_sent(db, rec, proposal_stub):
    proposal_stub["db"] = db
    proposal_stub["refuse"] = True
    db.tables["projects"].append(_full_project(gc_confirm_pending=True))
    db.tables["project_gcs"].append({"id": "old-link", "project_id": P1, "gc_id": "gc-2", "needs_by": None})
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", None, "u1")
    assert out["removed_old"] is False
    assert sorted(row["gc_id"] for row in db.tables["project_gcs"]) == ["gc-1", "gc-2"]
    assert db.tables["project_gc_contacts"] == []   # no lead, no contact
    assert db.tables["projects"][0]["gc_confirm_pending"] is False


def test_swap_project_gc_same_gc_and_an_existing_new_link(db, rec, proposal_stub):
    proposal_stub["db"] = db
    db.tables["projects"].append(_full_project(gc_confirm_pending=True))
    db.tables["project_gcs"].append({"id": "link-1", "project_id": P1, "gc_id": "gc-1", "needs_by": None})
    lead = {"first_name": "Pat", "last_name": "Lee", "email": "pm@gc.example", "phone": None}
    # "Same GC": nothing to remove, the contact attached, the question closed.
    out = rc.swap_project_gc(db, P1, "gc-1", "gc-1", lead, "u1")
    assert out == {"removed_old": False, "project_gc_id": "link-1", "removed_contact": False}
    assert proposal_stub["calls"] == []
    assert [c["gc_contact_id"] for c in db.tables["project_gc_contacts"]] == ["c1"]
    assert db.tables["projects"][0]["gc_confirm_pending"] is False
    # Twice is idempotent on the contact link.
    rc.swap_project_gc(db, P1, "gc-1", "gc-1", lead, "u1")
    assert len(db.tables["project_gc_contacts"]) == 1
    # Picking a GC already on the project (the old one removed) carries needs_by onto it.
    db.tables["project_gcs"].append({"id": "old-2", "project_id": P1, "gc_id": "gc-2", "needs_by": "2026-10-16"})
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", None, "u1")
    assert out["removed_old"] is True
    (link,) = db.tables["project_gcs"]
    assert link["id"] == "link-1" and link["needs_by"] == "2026-10-16"
    with pytest.raises(ValueError):
        rc.swap_project_gc(db, P1, "gc-2", "", None, "u1")


# The dev case: the create filed "Nancy Lopez <nlopez@aaafs.com>" under the
# provisional GC (gc-2) and linked her as the project's bid contact.
STRAY_LEAD = {"first_name": "Nancy", "last_name": "Lopez", "email": "NLopez@AAAFS.com", "phone": None}


def _provisional_project_with_stray(db):
    # The stray was inserted in the same instant as the project: "at or
    # after" the project's created_at, so the creation made it.
    db.tables["projects"].append(_full_project(gc_confirm_pending=True, created_at=NOW.isoformat()))
    db.tables["project_gcs"].append({"id": "old-link", "project_id": P1, "gc_id": "gc-2", "needs_by": None})
    db.tables["gc_contacts"].append({"id": "stray", "gc_id": "gc-2", "email": "nlopez@aaafs.com",
                                     "name": "Nancy Lopez", "created_at": NOW.isoformat()})
    db.tables["project_gc_contacts"].append({"id": "pgc-stray", "project_gc_id": "old-link",
                                             "gc_contact_id": "stray"})


def _contact_ids(db, gc_id):
    return {c["id"] for c in db.tables["gc_contacts"] if c["gc_id"] == gc_id}


def test_swap_project_gc_removes_the_unreferenced_stray_lead_contact(db, rec, proposal_stub):
    proposal_stub["db"] = db
    _provisional_project_with_stray(db)
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", STRAY_LEAD, "u1")
    (link,) = db.tables["project_gcs"]
    assert out == {"removed_old": True, "project_gc_id": link["id"], "removed_contact": True}
    assert _contact_ids(db, "gc-2") == {"c2"}               # the stray is gone, nothing else
    (made,) = [c for c in db.tables["gc_contacts"] if c["email"] == "nlopez@aaafs.com"]
    assert made["gc_id"] == "gc-1" and made["name"] == "Nancy Lopez"   # filed under the confirmed GC
    assert _contact_ids(db, "gc-1") == {"c1", "c1b", made["id"]}
    (pgc,) = db.tables["project_gc_contacts"]
    assert pgc == {"id": pgc["id"], "project_gc_id": link["id"], "gc_contact_id": made["id"]}
    (a,) = rec["audits"]
    assert a["payload"]["removed_contact_id"] == "stray"


def test_swap_project_gc_keeps_a_lead_contact_older_than_the_project(db, rec, proposal_stub):
    proposal_stub["db"] = db
    db.tables["projects"].append(_full_project(gc_confirm_pending=True, created_at=NOW.isoformat()))
    db.tables["project_gcs"].append({"id": "old-link", "project_id": P1, "gc_id": "gc-2", "needs_by": None})
    # Nancy was already on file under gc-2 before the project, so the create
    # reused her instead of inserting one; nothing else refers to her.
    db.tables["gc_contacts"].append({"id": "old-nancy", "gc_id": "gc-2", "email": "nlopez@aaafs.com",
                                     "name": "Nancy Lopez", "created_at": "2026-03-01T00:00:00+00:00"})
    db.tables["project_gc_contacts"].append({"id": "pgc-old", "project_gc_id": "old-link",
                                             "gc_contact_id": "old-nancy"})
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", STRAY_LEAD, "u1")
    assert out["removed_old"] is True and out["removed_contact"] is False
    assert _contact_ids(db, "gc-2") == {"c2", "old-nancy"}
    assert not any(r["gc_contact_id"] == "old-nancy" for r in db.tables["project_gc_contacts"])
    assert rec["audits"][0]["payload"]["removed_contact_id"] is None


def test_swap_project_gc_keeps_the_stray_contact_another_project_links(db, rec, proposal_stub):
    proposal_stub["db"] = db
    _provisional_project_with_stray(db)
    db.tables["project_gcs"].append({"id": "other-link", "project_id": "p-other", "gc_id": "gc-2", "needs_by": None})
    db.tables["project_gc_contacts"].append({"id": "pgc-other", "project_gc_id": "other-link",
                                             "gc_contact_id": "stray"})
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", STRAY_LEAD, "u1")
    assert out["removed_old"] is True and out["removed_contact"] is False
    assert _contact_ids(db, "gc-2") == {"c2", "stray"}
    assert [r["id"] for r in db.tables["project_gc_contacts"] if r["gc_contact_id"] == "stray"] == ["pgc-other"]
    assert rec["audits"][0]["payload"]["removed_contact_id"] is None


@pytest.mark.parametrize("table,row", [
    ("rfis", {"id": "rfi-1", "assigned_contact_id": "stray"}),
    ("rfp_emails", {"id": "e-1", "resolved_gc_contact_id": "stray"}),
    ("rfp_project_matches", {"id": "m-1", "contact_selected_id": "stray"}),
    ("proposal_sends", {"id": "ps-1", "project_id": "p-other", "gc_id": "gc-2", "status": "sent",
                        "gc_email": "pm@gc.example, NLopez@aaafs.com"}),
    ("proposal_send_events", {"id": "pse-1", "project_id": "p-other", "gc_id": "gc-2",
                              "recipients": "nlopez@aaafs.com", "cc_recipients": None}),
])
def test_swap_project_gc_keeps_the_stray_contact_any_other_row_refers_to(db, rec, proposal_stub, table, row):
    proposal_stub["db"] = db
    _provisional_project_with_stray(db)
    db.tables.setdefault(table, []).append(row)
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", STRAY_LEAD, "u1")
    assert out["removed_old"] is True and out["removed_contact"] is False
    assert "stray" in _contact_ids(db, "gc-2")


def test_swap_project_gc_keeps_the_stray_contact_when_the_old_link_stays(db, rec, proposal_stub):
    proposal_stub["db"] = db
    proposal_stub["refuse"] = True          # a proposal went to the provisional GC
    _provisional_project_with_stray(db)
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", STRAY_LEAD, "u1")
    assert out["removed_old"] is False and out["removed_contact"] is False
    assert _contact_ids(db, "gc-2") == {"c2", "stray"}
    assert any(r["id"] == "pgc-stray" for r in db.tables["project_gc_contacts"])


def test_swap_project_gc_never_touches_an_old_gc_contact_with_another_email(db, rec, proposal_stub):
    proposal_stub["db"] = db
    db.tables["projects"].append(_full_project(gc_confirm_pending=True))
    db.tables["project_gcs"].append({"id": "old-link", "project_id": P1, "gc_id": "gc-2", "needs_by": None})
    # Same domain, a LIKE lookalike of the lead's `_`, and an email-less
    # contact: none of them is the lead, so none is a candidate.
    db.tables["gc_contacts"] += [
        {"id": "colleague", "gc_id": "gc-2", "email": "jsmith@aaafs.com", "name": "J Smith",
         "created_at": NOW.isoformat()},
        {"id": "lookalike", "gc_id": "gc-2", "email": "nXlopez@aaafs.com", "name": "Look Alike",
         "created_at": NOW.isoformat()},
        {"id": "phone-only", "gc_id": "gc-2", "email": None, "name": "Phone Only",
         "created_at": NOW.isoformat()},
    ]
    lead = {**STRAY_LEAD, "email": "n_lopez@aaafs.com"}
    out = rc.swap_project_gc(db, P1, "gc-2", "gc-1", lead, "u1")
    assert out["removed_old"] is True and out["removed_contact"] is False
    assert _contact_ids(db, "gc-2") == {"c2", "colleague", "lookalike", "phone-only"}
    # The lead's own contact under the new GC is created and kept.
    assert any(c["gc_id"] == "gc-1" and c["email"] == "n_lopez@aaafs.com" for c in db.tables["gc_contacts"])


# ── files_needed (D19) ───────────────────────────────────────────────────


@pytest.fixture
def fn_audits(monkeypatch):
    calls = []
    monkeypatch.setattr(files_needed, "audit", lambda *a: calls.append(a))
    return calls


def _flagged(pid=P1, **over):
    row = {"id": pid, "files_needed_source": "buildingconnected", "files_needed_url": "https://app.buildingconnected.com/opportunities/x/info",
           "files_needed_set_at": "2026-09-26T10:00:00+00:00", "files_needed_cleared_at": None,
           "files_needed_cleared_by": None}
    row.update(over)
    return row


def test_clear_categories_are_every_drawing_set_plus_specification():
    from app.core.file_categories import DRAWING_CATEGORIES

    assert files_needed.CLEAR_CATEGORIES == frozenset(DRAWING_CATEGORIES | {"specification"})
    assert "other" not in files_needed.CLEAR_CATEGORIES and "rfp" not in files_needed.CLEAR_CATEGORIES
    assert "low_voltage_drawing" in files_needed.CLEAR_CATEGORIES


def test_set_needed_is_idempotent_and_re_raises_a_cleared_flag(db):
    db.tables["projects"].append({"id": P1, "files_needed_set_at": None, "files_needed_cleared_at": None})
    files_needed.set_needed(db, P1, source="buildingconnected", url="https://app.buildingconnected.com/opportunities/a/info")
    row = db.tables["projects"][0]
    assert row["files_needed_source"] == "buildingconnected" and row["files_needed_set_at"]
    first = row["files_needed_set_at"]
    files_needed.set_needed(db, P1, source="other", url="https://x")
    assert row["files_needed_source"] == "buildingconnected" and row["files_needed_set_at"] == first
    row["files_needed_cleared_at"] = "2026-09-26T11:00:00+00:00"
    row["files_needed_cleared_by"] = "u1"
    files_needed.set_needed(db, P1, source="buildingconnected", url="https://app.buildingconnected.com/opportunities/b/info")
    assert row["files_needed_cleared_at"] is None and row["files_needed_cleared_by"] is None
    assert row["files_needed_url"].endswith("/b/info")
    files_needed.set_needed(db, "missing", source="buildingconnected", url=None)   # a no-op
    assert files_needed.is_flagged(row) is True and files_needed.is_flagged(None) is False


def test_project_has_files_reads_only_the_clear_categories(db):
    db.tables["project_files"].append({"id": "f1", "project_id": P1, "category": "other"})
    assert files_needed.project_has_files(db, P1) is False
    db.tables["project_files"].append({"id": "f2", "project_id": P1, "category": "specification"})
    assert files_needed.project_has_files(db, P1) is True
    assert files_needed.project_has_files(db, "other-project") is False


def test_clear_if_satisfied_clears_once_audits_and_is_best_effort(db, fn_audits):
    db.tables["projects"].append(_flagged())
    assert files_needed.clear_if_satisfied(db, P1, "other", "u1") is False
    assert files_needed.clear_if_satisfied(db, None, "drawing", "u1") is False
    assert db.tables["projects"][0]["files_needed_cleared_at"] is None and fn_audits == []
    assert files_needed.clear_if_satisfied(db, P1, "electrical_drawing", "u1") is True
    row = db.tables["projects"][0]
    assert row["files_needed_cleared_at"] and row["files_needed_cleared_by"] == "u1"
    assert fn_audits == [("u1", "project.files_needed_cleared", "project", P1,
                          {"category": "electrical_drawing", "source": "buildingconnected"})]
    # Already cleared, or never flagged: nothing moves, nothing is audited.
    assert files_needed.clear_if_satisfied(db, P1, "specification", "u1") is False
    db.tables["projects"].append({"id": "p2", "files_needed_set_at": None, "files_needed_cleared_at": None})
    assert files_needed.clear_if_satisfied(db, "p2", "drawing", None) is False
    assert len(fn_audits) == 1

    class Broken:
        def table(self, name):
            raise RuntimeError("db down")

    assert files_needed.clear_if_satisfied(Broken(), P1, "drawing", "u1") is False


def test_dismiss_clears_by_hand_once(db, fn_audits):
    db.tables["projects"].append(_flagged())
    out = files_needed.dismiss(db, P1, "u1")
    assert out["files_needed_cleared_by"] == "u1" and out["files_needed_cleared_at"]
    assert fn_audits == [("u1", "project.files_needed_dismiss", "project", P1, {"source": "buildingconnected"})]
    assert files_needed.dismiss(db, P1, "u1") is None and len(fn_audits) == 1
    assert files_needed.dismiss(db, "nope", "u1") is None


# ── The four insert-path hooks ───────────────────────────────────────────


def test_split_insert_row_clears_the_flag_and_stays_best_effort(db, fn_audits, monkeypatch):
    monkeypatch.setattr(rfp_split.storage, "delete_file", lambda path: None)
    db.tables["projects"].append(_flagged())
    row = {"project_id": P1, "category": "drawing", "storage_path": "k", "filename": "E-1.pdf"}
    inserted = rfp_split._insert_row(db, row, "k")
    assert inserted["id"] and db.tables["projects"][0]["files_needed_cleared_at"]
    assert fn_audits[0][1] == "project.files_needed_cleared"

    class SplitDB(CreateDB):
        def table(self, name):
            if name == "projects":
                raise RuntimeError("projects unreachable")
            return super().table(name)

    broken = SplitDB({"project_files": []})
    inserted = rfp_split._insert_row(broken, {"project_id": P1, "category": "drawing", "storage_path": "k2"}, "k2")
    assert inserted["id"] and len(broken.tables["project_files"]) == 1


def test_promote_one_clears_the_flag(db, fn_audits, monkeypatch):
    monkeypatch.setattr(rcf.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(rcf.storage, "delete_file", lambda *a, **k: None)
    monkeypatch.setattr(rcf.office_preview, "is_convertible", lambda *a, **k: False)
    monkeypatch.setattr(rcf, "audit", lambda *a, **k: None)
    db.tables["projects"].append(_flagged())
    decision = rcf.Promote(bucket="b", path="p", filename="Specs.pdf", content_type="application/pdf",
                           check_sha=False)
    category = rcf._promote_one(db, P1, None, {"kind": "specification"}, {"id": "sf-1"}, decision, b"x")
    assert category == "specification" and db.tables["projects"][0]["files_needed_cleared_at"]
    assert db.tables["project_files"][0]["category"] == "specification"
    # An `other` promotion leaves a fresh flag alone.
    db.tables["projects"].append(_flagged("p2"))
    rcf._promote_one(db, "p2", None, {"kind": None}, {"id": "sf-2"}, decision, b"y")
    assert db.tables["projects"][1]["files_needed_cleared_at"] is None


async def test_upload_file_hook_runs_for_a_clear_category_only_and_never_fails_the_upload(monkeypatch):
    from app.routers import files as files_mod
    from tests.test_file_updates import _do_upload, _upload_env, _writer

    calls = []
    sb = _upload_env(monkeypatch)
    monkeypatch.setattr(files_mod, "handoff_locked", lambda pid: False)
    monkeypatch.setattr(files_mod, "_notify_drawing_changed", lambda *a, **k: None)
    monkeypatch.setattr(files_mod.files_needed, "clear_if_satisfied",
                        lambda db_, pid, category, actor: calls.append((db_ is sb, pid, category, actor)) or True)
    row = await _do_upload(project_id="p1", category="drawing", user=_writer())
    assert row["category"] == "drawing" and calls == [(True, "p1", "drawing", "w1")]
    row = await _do_upload(project_id="p1", category="other", user=_writer())
    assert row["category"] == "other" and len(calls) == 1
    # The real service against a client that cannot even update: the upload still answers.
    monkeypatch.setattr(files_mod.files_needed, "clear_if_satisfied", files_needed.clear_if_satisfied)
    row = await _do_upload(project_id="p1", category="specification", user=_writer())
    assert row["category"] == "specification"


async def test_draft_transfer_hook_runs_per_landed_file_and_is_best_effort(monkeypatch):
    from app.routers import bid_drafts as bd
    from tests.test_bid_draft_files import PROJECT_ID, FakeStore, _mk_draft, _seed_project, _transfer, _upload
    from tests.test_bid_drafts import FakeDB as DraftDB

    draft_db = DraftDB()
    store = FakeStore()
    monkeypatch.setattr(bd, "get_supabase", lambda: draft_db)
    monkeypatch.setattr(bd, "audit", lambda *a: None)
    for name in ("build_draft_object_path", "upload_file", "delete_file", "move_object", "object_exists",
                 "delete_draft_prefix"):
        monkeypatch.setattr(bd.storage, name, getattr(store, name))
    monkeypatch.setattr(bd.office_preview, "is_convertible", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(bd.files_needed, "clear_if_satisfied",
                        lambda db_, pid, category, actor: calls.append((db_ is draft_db, pid, category, actor)) or False)
    draft = _mk_draft()
    await _upload(draft["id"], category="drawing", filename="E-101.pdf", content=b"abc")
    await _upload(draft["id"], category="specification", filename="26.pdf", content=b"abcd")
    _seed_project(draft_db)
    assert _transfer(draft["id"], uid="admin-9") == {"moved": 2}
    assert calls == [(True, PROJECT_ID, "drawing", "admin-9"), (True, PROJECT_ID, "specification", "admin-9")]
    # The real service against the drafts fake (no `not_`/`is_`): the transfer still lands.
    monkeypatch.setattr(bd.files_needed, "clear_if_satisfied", files_needed.clear_if_satisfied)
    draft2 = _mk_draft("Second")
    await _upload(draft2["id"], category="drawing", filename="E-2.pdf", content=b"zz")
    assert _transfer(draft2["id"], uid="admin-9") == {"moved": 1}


# ── The projects router ──────────────────────────────────────────────────


class _RouterQuery(_Query):
    """The ingest fake's query plus `.single()` (the detail read uses it)."""

    def __init__(self, db, table):
        super().__init__(db, table)
        self._single = False

    def single(self):
        self._single = True
        return self

    def execute(self):
        out = super().execute()
        if self._single and self._op == "select":
            data = out.data
            return SimpleNamespace(data=(data[0] if data else None), count=out.count)
        return out


class RouterDB(FakeDB):
    def table(self, name):
        return _RouterQuery(self, name)


@pytest.fixture
def renv(monkeypatch):
    lead = {"first_name": "Pat", "last_name": "Lee", "email": "pat@gcaa.example", "phone": "702"}
    db = RouterDB({
        "projects": [_full_project(gc_confirm_pending=True, **_flagged())],
        "general_contractors": [{"id": "gc-1", "name": "GC AA Builders"}, {"id": "gc-2", "name": "Rafael Companies"}],
        "rfp_portal_invitations": [
            {"id": INV, "portal": "buildingconnected", "status": "created", "gc_external_id": "ext-1",
             "gc_external_name": "GC AA Builders Inc", "gc_id": "gc-2", "gc_kind": "provisional",
             "gc_candidates": [{"gc_id": "gc-2", "name": "Rafael Companies", "score": 0.3}],
             "gc_confirmed_at": None, "lead": lead, "created_project_id": P1, "match_project_id": None,
             "resolved_at": None, "first_seen_at": NOW.isoformat()},
            {"id": "inv-ngem", "portal": "ngem", "status": "exists", "match_project_id": P1,
             "created_project_id": None},
        ],
        "rfp_created_projects": [],
        "audit_log": [],
    })
    audits = []
    confirm_calls = []
    settings = SimpleNamespace(rfp_ingest_enabled=True, rfp_bc_enabled=True, rfp_ngem_enabled=False,
                               default_rate_limit_per_min=600)
    monkeypatch.setattr(pr, "get_supabase", lambda: db)
    monkeypatch.setattr(pr, "get_settings", lambda: settings)
    monkeypatch.setattr(pr, "audit", lambda *a: audits.append(a))
    monkeypatch.setattr(pr.workflow, "load_category_state", lambda pid: {})
    monkeypatch.setattr(pr.workflow, "load_category_states", lambda ids: {i: {} for i in ids})
    monkeypatch.setattr(files_needed, "audit", lambda *a: audits.append(a))

    def confirm_gc(sb, invitation_id, actor_id, *, gc_id=None, create=None):
        confirm_calls.append({"sb": sb is db, "invitation_id": invitation_id, "actor_id": actor_id,
                              "gc_id": gc_id, "create": create})
        for inv in db.tables["rfp_portal_invitations"]:
            if inv["id"] == invitation_id:
                inv["gc_id"] = gc_id or "gc-new"
                inv["gc_kind"] = "alias"
                inv["gc_confirmed_at"] = NOW.isoformat()
        db.tables["projects"][0]["gc_confirm_pending"] = False
        return {"id": invitation_id}

    monkeypatch.setattr(pr, "_bc_portal", lambda: SimpleNamespace(confirm_gc=confirm_gc))
    return SimpleNamespace(db=db, audits=audits, confirm_calls=confirm_calls, settings=settings)


def _routes():
    return {(m, r.path): r for r in pr.router.routes for m in r.methods}


def test_new_project_routes_exist_with_the_documented_dependencies():
    routes = _routes()
    bidding = pr._BIDDING_ONLY[0].dependency
    dismiss = routes[("POST", "/projects/{project_id}/files-needed/dismiss")]
    calls = {d.call for d in dismiss.dependant.dependencies}
    assert require_writer in calls and bidding in calls and features.require_rfp_ingest not in calls
    for key in (("GET", "/projects/{project_id}/gc-confirm"), ("POST", "/projects/{project_id}/gc-confirm")):
        calls = {d.call for d in routes[key].dependant.dependencies}
        assert require_writer in calls and bidding in calls and features.require_rfp_ingest in calls, key


@pytest.mark.parametrize("role", sorted(Role, key=lambda r: r.value))
def test_writer_roles_pass_the_gc_confirm_and_dismiss_gate(role):
    from app.core.roles import WRITER_ROLES

    user = _user(role)
    if role in WRITER_ROLES:
        assert asyncio.run(require_writer(user=user)) is user
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_writer(user=user))
        assert exc.value.status_code == 403


def test_field_editors_open_the_three_new_fields_to_writers():
    for field in ("job_walk_at", "project_information", "trade_instructions"):
        assert pr._FIELD_EDITORS[field] == pr._OPEN
    patch = ProjectUpdate(job_walk_at="2026-10-05T17:00:00Z", project_information="A\n- b",
                          trade_instructions=None).model_dump(exclude_unset=True, mode="json")
    assert patch == {"job_walk_at": "2026-10-05T17:00:00Z", "project_information": "A\n- b",
                     "trade_instructions": None}
    assert ProjectUpdate(project_information="x" * 20000).project_information
    with pytest.raises(ValidationError):
        ProjectUpdate(project_information="x" * 20001)
    with pytest.raises(ValidationError):
        ProjectUpdate(trade_instructions="x" * 20001)


def test_dismiss_route_clears_once_audits_and_answers_the_project(renv):
    out = pr.dismiss_files_needed(P1, user=_user(Role.ESTIMATING_ENGINEER_LABOR, uid="eng"))
    assert out["files_needed_cleared_at"] and out["files_needed_set_at"]
    assert out["id"] == P1 and out["actual_bid_at"] is None   # redacted for the engineer
    assert renv.audits == [("eng", "project.files_needed_dismiss", "project", P1, {"source": "buildingconnected"})]
    validated = ProjectOut.model_validate({**out, "created_at": NOW, "updated_at": NOW,
                                           "current_owner_role": None})
    assert validated.files_needed_source == "buildingconnected" and validated.files_needed_cleared_at
    # A second click: nothing moves, no second audit row, the project comes back as is.
    again = pr.dismiss_files_needed(P1, user=_user())
    assert again["files_needed_cleared_at"] == out["files_needed_cleared_at"] and len(renv.audits) == 1
    with pytest.raises(HTTPException) as exc:
        pr.dismiss_files_needed("5a000000-0000-4000-8000-0000000000ff", user=_user())
    assert exc.value.status_code == 404


def test_gc_confirm_get_reads_the_bc_invitation_behind_the_project(renv):
    out = pr.get_gc_confirm(P1, user=_user())
    assert out == {
        "pending": True,
        "external_name": "GC AA Builders Inc",
        "provisional_gc": {"id": "gc-2", "name": "Rafael Companies"},
        "candidates": [{"gc_id": "gc-2", "name": "Rafael Companies", "score": 0.3}],
        "lead": {"name": "Pat Lee", "email": "pat@gcaa.example", "phone": "702"},
        "invitation_id": INV,
        "portal": "buildingconnected",
    }
    # A project with no BuildingConnected invitation: the empty card.
    renv.db.tables["projects"].append(_full_project("p-other"))
    assert pr.get_gc_confirm("p-other", user=_user()) == {
        "pending": False, "external_name": None, "provisional_gc": None, "candidates": [], "lead": None,
        "invitation_id": None, "portal": None,
    }
    with pytest.raises(HTTPException) as exc:
        pr.get_gc_confirm("missing", user=_user())
    assert exc.value.status_code == 404


def test_gc_confirm_404s_bare_while_the_bc_portal_is_not_served(renv, monkeypatch):
    """Contract D4: with RFP_BC_ENABLED off (or the master switch off, or
    the flag absent) the two gc-confirm routes answer the bare 404 before
    any read, so the BC lead's email and phone and the alias write never
    leak through the project page; dismiss is generic project state and
    keeps serving."""
    body = ProjectGcConfirmIn(decision="pick", gc_id="gc-1")
    for settings in (
        SimpleNamespace(rfp_ingest_enabled=True, rfp_bc_enabled=False, rfp_ngem_enabled=True, default_rate_limit_per_min=600),
        SimpleNamespace(rfp_ingest_enabled=False, rfp_bc_enabled=True, rfp_ngem_enabled=False, default_rate_limit_per_min=600),
        SimpleNamespace(rfp_ingest_enabled=True, default_rate_limit_per_min=600),   # absent flag: closed
    ):
        monkeypatch.setattr(pr, "get_settings", lambda s=settings: s)
        with pytest.raises(HTTPException) as exc:
            pr.get_gc_confirm(P1, user=_user())
        assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
        with pytest.raises(HTTPException) as exc:
            pr.post_gc_confirm(P1, body, user=_user())
        assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
        assert renv.confirm_calls == []
        assert pr.dismiss_files_needed(P1, user=_user())["id"] == P1
    # Served again: the card reads and the answer goes through.
    monkeypatch.setattr(pr, "get_settings", lambda: renv.settings)
    assert pr.get_gc_confirm(P1, user=_user())["pending"] is True
    pr.post_gc_confirm(P1, body, user=_user())
    assert len(renv.confirm_calls) == 1


def test_gc_confirm_prefers_the_creating_invitation_then_the_newest_merge(renv):
    renv.db.tables["rfp_portal_invitations"].append(
        {"id": "inv-older", "portal": "buildingconnected", "status": "exists", "match_project_id": P1,
         "created_project_id": None, "gc_id": "gc-1", "gc_external_name": "Older", "resolved_at": "2026-09-01T00:00:00+00:00"})
    assert pr.get_gc_confirm(P1, user=_user())["invitation_id"] == INV
    renv.db.tables["rfp_portal_invitations"][0]["created_project_id"] = None
    renv.db.tables["rfp_portal_invitations"][0]["match_project_id"] = P1
    renv.db.tables["rfp_portal_invitations"][0]["status"] = "exists"
    renv.db.tables["rfp_portal_invitations"][0]["resolved_at"] = "2026-09-20T00:00:00+00:00"
    assert pr.get_gc_confirm(P1, user=_user())["invitation_id"] == INV
    renv.db.tables["rfp_portal_invitations"][0]["resolved_at"] = "2026-08-01T00:00:00+00:00"
    assert pr.get_gc_confirm(P1, user=_user())["invitation_id"] == "inv-older"


def test_gc_confirm_post_delegates_each_decision_and_answers_the_card(renv):
    out = pr.post_gc_confirm(P1, ProjectGcConfirmIn(decision="same"), user=_user(uid="u7"))
    assert renv.confirm_calls == [{"sb": True, "invitation_id": INV, "actor_id": "u7", "gc_id": "gc-2", "create": None}]
    assert out["pending"] is False and out["provisional_gc"] == {"id": "gc-2", "name": "Rafael Companies"}
    pr.post_gc_confirm(P1, ProjectGcConfirmIn(decision="pick", gc_id=" gc-1 "), user=_user())
    assert renv.confirm_calls[-1]["gc_id"] == "gc-1" and renv.confirm_calls[-1]["create"] is None
    body = ProjectGcConfirmIn(decision="create", create={"name": "New GC", "contact_email": "a@b.co"})
    out = pr.post_gc_confirm(P1, body, user=_user())
    assert renv.confirm_calls[-1] == {"sb": True, "invitation_id": INV, "actor_id": "u1", "gc_id": None,
                                      "create": {"name": "New GC", "contact_name": None, "contact_email": "a@b.co",
                                                 "contact_phone": None}}
    assert out["provisional_gc"] == {"id": "gc-new", "name": None}


def test_gc_confirm_post_refusals(renv, monkeypatch):
    renv.db.tables["rfp_portal_invitations"][0]["gc_id"] = None
    with pytest.raises(HTTPException) as exc:
        pr.post_gc_confirm(P1, ProjectGcConfirmIn(decision="same"), user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_portal_gc_required"
    assert renv.confirm_calls == []
    renv.db.tables["projects"].append(_full_project("p-other"))
    with pytest.raises(HTTPException) as exc:
        pr.post_gc_confirm("p-other", ProjectGcConfirmIn(decision="pick", gc_id="gc-1"), user=_user())
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        pr.post_gc_confirm("missing", ProjectGcConfirmIn(decision="pick", gc_id="gc-1"), user=_user())
    assert exc.value.status_code == 404
    # The service's refusals map to HTTP: a missing GC 404, a bad argument 400, a sent proposal 409.

    def raising(exc_):
        def confirm_gc(*a, **k):
            raise exc_
        return SimpleNamespace(confirm_gc=confirm_gc)

    monkeypatch.setattr(pr, "_bc_portal", lambda: raising(LookupError("GC gone")))
    with pytest.raises(HTTPException) as exc:
        pr.post_gc_confirm(P1, ProjectGcConfirmIn(decision="pick", gc_id="gc-x"), user=_user())
    assert exc.value.status_code == 404
    monkeypatch.setattr(pr, "_bc_portal", lambda: raising(ValueError("A GC name is required")))
    with pytest.raises(HTTPException) as exc:
        pr.post_gc_confirm(P1, ProjectGcConfirmIn(decision="pick", gc_id="gc-x"), user=_user())
    assert exc.value.status_code == 400
    monkeypatch.setattr(pr, "_bc_portal", lambda: raising(ProposalSendError("sent", code="rfp_match_gc_already_sent")))
    with pytest.raises(HTTPException) as exc:
        pr.post_gc_confirm(P1, ProjectGcConfirmIn(decision="pick", gc_id="gc-x"), user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_match_gc_already_sent"


def test_gc_confirm_body_validation():
    with pytest.raises(ValidationError):
        ProjectGcConfirmIn(decision="pick")
    with pytest.raises(ValidationError):
        ProjectGcConfirmIn(decision="create")
    with pytest.raises(ValidationError):
        ProjectGcConfirmIn(decision="maybe")
    with pytest.raises(ValidationError):
        ProjectGcConfirmIn(decision="create", create={"name": ""})
    assert ProjectGcConfirmIn(decision="same").gc_id is None


# ── Portal sources (D32) and the created summary ────────────────────────


def _bc_source(**over):
    row = {
        "portal": "buildingconnected", "invitation_id": INV, "agency": "GC AA Builders", "bid_number": "abc",
        "bid_number_raw": "abc", "addendum_no": None, "close_at": "2026-10-16T23:00:00+00:00",
        "resolution": "system", "resolved_at": None, "external_url": "https://app.buildingconnected.com/opportunities/abc/info",
        "trade_name": "Electrical", "invited_at": "2026-09-20T00:00:00+00:00", "gc_external_name": "GC AA Builders",
        "gc_id": "gc-1", "gc_name": "GC AA Builders",
        "change_log": [{"at": "2026-09-25T00:00:00+00:00", "field": "close_at", "old": "2026-10-10T23:00:00+00:00",
                        "new": "2026-10-16T23:00:00+00:00", "run_id": "run-1"},
                       {"at": "2026-09-25T00:00:00+00:00", "field": "title", "old": "A", "new": "B", "run_id": "run-1"}],
    }
    row.update(over)
    return row


def test_portal_sources_gate_on_any_portal_and_filter_by_portal_flag(monkeypatch):
    from app.services import rfp_portal_ingest as portal

    calls = []
    rows = {P1: [_bc_source(), {"portal": "ngem", "invitation_id": "inv-ngem", "agency": "UNLV"}]}
    monkeypatch.setattr(portal, "portal_sources_for_projects", lambda sb, ids: calls.append(list(ids)) or rows)
    monkeypatch.setattr(pr, "get_supabase", lambda: "sb")
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True, rfp_bc_enabled=False,
                                                                     rfp_ngem_enabled=False))
    assert pr._portal_sources([P1]) == {} and calls == []
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=False, rfp_bc_enabled=True))
    assert pr._portal_sources([P1]) == {}
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True, rfp_bc_enabled=True,
                                                                     rfp_ngem_enabled=False))
    out = pr._portal_sources([P1])
    assert calls == [[P1]] and [r["portal"] for r in out[P1]] == ["buildingconnected"]
    (bc,) = out[P1]
    assert "change_log" not in bc
    assert bc["changes"] == [
        {"at": "2026-09-25T00:00:00+00:00", "field": "close_at", "old": "2026-10-10T23:00:00+00:00",
         "new": "2026-10-16T23:00:00+00:00"},
        {"at": "2026-09-25T00:00:00+00:00", "field": "title", "old": "A", "new": "B"},
    ]
    # The real Settings object: its computed property is what gates.
    on = Settings(_env_file=None, rfp_ingest_enabled=True, rfp_bc_enabled=True)
    monkeypatch.setattr(pr, "get_settings", lambda: on)
    assert on.rfp_portal_any_enabled is True and pr._portal_sources([P1]) != {}
    off = Settings(_env_file=None, rfp_ingest_enabled=True, rfp_bc_enabled=False, rfp_ngem_enabled=False)
    monkeypatch.setattr(pr, "get_settings", lambda: off)
    assert pr._portal_sources([P1]) == {}


def test_portal_sources_redaction_by_role():
    rows = [{**_bc_source(), "changes": [
        {"at": None, "field": "close_at", "old": "x", "new": "y"}, {"at": None, "field": "title", "old": "A", "new": "B"}]}]
    rows[0].pop("change_log")
    kept = pr._redact_portal_sources(rows, Role.EXECUTIVE)
    assert kept == rows
    kept = pr._redact_portal_sources(rows, Role.ACCOUNTANT)
    assert kept[0]["close_at"] == "2026-10-16T23:00:00+00:00"
    redacted = pr._redact_portal_sources(rows, Role.ESTIMATING_ENGINEER_MATERIALS)
    assert redacted[0]["close_at"] is None
    assert redacted[0]["changes"] == [{"at": None, "field": "close_at", "old": None, "new": None},
                                      {"at": None, "field": "title", "old": "A", "new": "B"}]
    assert redacted[0]["external_url"] == rows[0]["external_url"] and redacted[0]["trade_name"] == "Electrical"
    # Rows without a change list (NGEM) are left as they are but for the date.
    plain = pr._redact_portal_sources([{"portal": "ngem", "invitation_id": "i", "close_at": "c"}], Role.ESTIMATING_ENGINEER_LABOR)
    assert plain == [{"portal": "ngem", "invitation_id": "i", "close_at": None}]


def test_get_project_carries_the_bc_source_redacted_for_an_engineer(renv, monkeypatch):
    from app.services import rfp_portal_ingest as portal

    monkeypatch.setattr(portal, "portal_sources_for_projects", lambda sb, ids: {P1: [_bc_source()]})
    monkeypatch.setattr(pr, "_rfp_match_counts", lambda ids, by_id, states: {})
    monkeypatch.setattr(pr, "_pending_gc_pricing_counts", lambda ids: {})
    renv.db.tables["projects"][0].update({"created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
                                          "current_owner_role": None})
    out = pr.get_project(P1, user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    (src,) = out["portal_sources"]
    assert src["close_at"] is None and src["changes"][0]["new"] is None and src["changes"][1]["new"] == "B"
    validated = ProjectOut.model_validate(out)
    assert validated.portal_sources[0].trade_name == "Electrical" and validated.portal_sources[0].gc_name == "GC AA Builders"
    assert validated.gc_confirm_pending is True and validated.files_needed_source == "buildingconnected"
    out = pr.get_project(P1, user=_user(Role.EXECUTIVE))
    assert out["portal_sources"][0]["close_at"] == "2026-10-16T23:00:00+00:00"
    assert out["portal_sources"][0]["changes"][0]["old"] == "2026-10-10T23:00:00+00:00"


def test_created_summary_carries_method_and_gc_plan(renv):
    renv.db.tables["rfp_created_projects"].append({
        "project_id": P1, "automatic": False, "sender_was_unauthorized": False, "sender_display": "Pat Lee <pat@gcaa.example>",
        "sender_allowed_by": None, "files_status": "none", "files_promoted": 0, "bid_time_unknown": False,
        "invitation_method": "buildingconnected", "gc_plan": "provisional",
    })
    rows = pr._rfp_created_rows([P1])
    summary = pr._rfp_created_summary(rows[P1])
    assert summary["invitation_method"] == "buildingconnected" and summary["gc_plan"] == "provisional"
    assert RfpCreatedSummary(**summary).gc_plan == "provisional"
    assert RfpCreatedSummary().invitation_method is None and RfpCreatedSummary().gc_plan is None
    assert "invitation_method" in pr._RFP_CREATED_SELECT and "gc_plan" in pr._RFP_CREATED_SELECT


def test_schema_defaults_for_the_new_project_fields():
    for field, default in (("job_walk_at", None), ("project_information", None), ("trade_instructions", None),
                           ("gc_confirm_pending", False), ("files_needed_source", None), ("files_needed_url", None),
                           ("files_needed_set_at", None), ("files_needed_cleared_at", None)):
        assert ProjectOut.model_fields[field].default == default, field
    src = PortalSourceOut(portal="buildingconnected", invitation_id=INV)
    assert src.changes == [] and src.external_url is None and src.gc_name is None
    src = PortalSourceOut(**{k: v for k, v in _bc_source().items() if k != "change_log"},
                          changes=[{"at": None, "field": "close_at", "old": None, "new": None}])
    assert src.changes[0].field == "close_at" and src.trade_name == "Electrical"


# ── The Created page (rfp_created) ───────────────────────────────────────


def test_rfp_created_gate_opens_on_any_portal(monkeypatch):
    monkeypatch.setattr(rr, "get_settings", lambda: SimpleNamespace(rfp_email_ingestion_enabled=False,
                                                                     rfp_ngem_active=False, rfp_portal_any_enabled=True))
    assert rr.rfp_created_enabled() is True
    monkeypatch.setattr(rr, "get_settings", lambda: SimpleNamespace(rfp_email_ingestion_enabled=False,
                                                                     rfp_ngem_active=False, rfp_portal_any_enabled=False))
    assert rr.rfp_created_enabled() is False
    on = Settings(_env_file=None, rfp_ingest_enabled=True, rfp_bc_enabled=True, rfp_ngem_enabled=False,
                  rfp_email_ingestion_inboxes_allowed="")
    assert on.rfp_email_ingestion_enabled is False and on.rfp_ngem_active is False
    monkeypatch.setattr(rr, "get_settings", lambda: on)
    assert rr.rfp_created_enabled() is True
    off = Settings(_env_file=None, rfp_ingest_enabled=True, rfp_bc_enabled=False, rfp_ngem_enabled=False,
                   rfp_email_ingestion_inboxes_allowed="")
    monkeypatch.setattr(rr, "get_settings", lambda: off)
    assert rr.rfp_created_enabled() is False


def test_rfp_created_flags_for_a_bc_row(monkeypatch):
    from tests.test_rfp_created_router import CreatedDB, _project, _rec

    db = CreatedDB({
        "rfp_created_projects": [
            _rec(P1, source_kind="rfp_portal", rfp_email_id=None, portal_invitation_id=INV, harvest_id=None,
                 invitation_method="buildingconnected", gc_plan="provisional", likely_gc_id="gc-2",
                 likely_gc_name="Rafael Companies", files_status="none", files_promoted=0, sender_display="Pat Lee <pat@gcaa.example>"),
        ],
        "projects": [_project(P1, "26.9.7210", "Fixture Project 01", gc_confirm_pending=True,
                              files_needed_source="buildingconnected",
                              files_needed_url="https://app.buildingconnected.com/opportunities/abc/info",
                              files_needed_set_at=NOW.isoformat(), files_needed_cleared_at=None)],
        "project_gcs": [{"id": "l1", "project_id": P1, "gc_id": "gc-2"}],
        "general_contractors": [{"id": "gc-2", "name": "Rafael Companies"}],
        "profiles": [],
        "rfp_emails": [],
        "rfp_portal_invitations": [{"id": INV, "title": "Fixture Project 01", "first_seen_at": NOW.isoformat(),
                                    "portal": "buildingconnected", "external_url": "https://app.buildingconnected.com/opportunities/abc/info"}],
        "rfp_harvests": [],
        "llm_jobs": [],
        "audit_log": [],
    })
    monkeypatch.setattr(rr, "get_supabase", lambda: db)
    monkeypatch.setattr(rr, "get_settings", lambda: SimpleNamespace(rfp_email_ingestion_enabled=False,
                                                                     rfp_portal_any_enabled=True,
                                                                     default_rate_limit_per_min=600))
    (item,) = rr.list_rfp_created(cleared="all", limit=100, before=None, before_id=None, _=_user())
    flags = item["flags"]
    assert flags["missing_files"] is False and flags["files_status"] == "none"
    assert flags["files_needed"] is True
    assert flags["files_needed_url"] == "https://app.buildingconnected.com/opportunities/abc/info"
    assert flags["confirm_gc"] is True and flags["likely_gc"] is None
    assert flags["provisional_gc"] == {"id": "gc-2", "name": "Rafael Companies"}
    assert flags["no_gc"] is False and item["created"]["gc_plan"] == "provisional"
    assert item["created"]["source"] == {"id": INV, "title": "Fixture Project 01", "first_seen_at": NOW.isoformat(),
                                         "invitation_method": "buildingconnected"}
    # Dismissed and confirmed: both flags fall off the row, read from the project.
    db.tables["projects"][0]["files_needed_cleared_at"] = NOW.isoformat()
    db.tables["projects"][0]["gc_confirm_pending"] = False
    (item,) = rr.list_rfp_created(cleared="all", limit=100, before=None, before_id=None, _=_user())
    assert item["flags"]["files_needed"] is False and item["flags"]["files_needed_url"] is None
    assert item["flags"]["confirm_gc"] is False and item["flags"]["missing_files"] is False
    assert "gc_confirm_pending" in rr._PROJECT_SELECT and "files_needed_cleared_at" in rr._PROJECT_SELECT
