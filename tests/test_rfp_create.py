"""The RFP creation service (app/services/rfp_create) against the in-memory
fake Supabase from tests/test_rfp_email_ingest (docs/RFP_CREATE.md sections
3, 4 and 7), with the workflow advance, the Go/No-Go entry action, the
bells, the audit log and the learn-back rescan recorded rather than run, and
the numbered insert stood in (the numbering module is another build's; one
test exercises the real one through the fake's rpc hook when it imports).

Pinned, in the doc's order:

- the pure facts for a Procore harvest, a plain email and a portal
  invitation, including the date-only rule (midnight Pacific and
  `bid_time_unknown`), the bidding-link fallbacks and the notes cap;
- the GC plan's four kinds and the sender marker;
- the whole creation: the project payload, the stage event and the
  category seed, the created record, the GC links, the source row CAS, the
  harvest link and claim, the mates (harvest sharers, sibling followers,
  portal sharers), the files job, the two bells with the missing list, the
  audit row and the rescan;
- the claim: a held claim waits, a stale one is taken over, a harvest that
  already has a project links instead;
- the lost race and the core-failure compensation (the project deleted,
  the claim released, the audit rows);
- the preconditions (one emptiness rule for the name: "Invitation to Bid"
  is no name), the nonorganic GC creation, the reuse of an existing GC as
  a likely GC with no contact, the compensation taking back the GC and
  contact this call inserted, the number exhaustion;
- the order of the core writes (the created record right behind the
  project), the pending-first fence on the files job, the human-set name
  winning over a harvest's, the sender marker without the method shortcut,
  the server-side domain lookup for a likely GC;
- the mirror-email subject naming the missing fields in plain words.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.roles import Role
from app.services import llm_queue, notification_email, rfp_create as rc
from tests.test_rfp_email_ingest import FakeDB, _Rpc

NOW = datetime(2026, 9, 16, 17, 0, 0, tzinfo=timezone.utc)
E1 = "e-1"
HV = "hv-1"
NEXT = {"counter": 7203}
# The module's own numbered insert, kept before the autouse fixture stands it in.
_REAL_INSERT_PROJECT = rc._insert_project


class CreateDB(FakeDB):
    """The ingest fake plus the partial unique index on active llm_jobs (the
    files job enqueue relies on it), a `next_project_number` rpc for the
    real numbering module, and a per-table insert failure switch for the
    compensation tests."""

    fail_insert: set[str] = set()
    inserts: list[str] = []

    def table(self, name):
        query = super().table(name)
        inherited = query._check_unique
        db = self

        def check(rows, payload):
            if name in db.fail_insert:
                raise RuntimeError(f"insert into {name} refused (test)")
            db.inserts.append(name)
            inherited(rows, payload)
            if name == "llm_jobs":
                for r in rows:
                    if (
                        r.get("job_type") == payload.get("job_type")
                        and r.get("target_id") == payload.get("target_id")
                        and r.get("status", "queued") in ("queued", "running")
                    ):
                        raise Exception(
                            'duplicate key value violates unique constraint '
                            '"llm_jobs_active_target_uq" (23505)'
                        )

        query._check_unique = check
        return query

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, dict(params or {})))
        if name == "next_project_number":
            NEXT["counter"] += 1
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=NEXT["counter"]))
        return _Rpc(self, name, params)


def _settings(**over):
    base = dict(rfp_ingest_enabled=True, llm_queue_enabled=True, rfp_create_auto_enabled=True)
    base.update(over)
    return Settings(_env_file=None, **base)


def _email(**over):
    row = {
        "id": E1,
        "status": "create",
        "from_address": "pm@gc.example",
        "from_name": "GC PM",
        "subject": "Invitation to Bid: Warehouse HVAC",
        "received_at": "2026-09-15T18:30:00+00:00",
        "invitation_method": "organic",
        "flag_reason": "no_candidate",
        "extracted_project_name": "Warehouse HVAC",
        "extracted_gc_name": "GC Example Builders",
        "extracted_bid_due_at": "2026-10-01T21:00:00+00:00",
        "extracted_bid_due_has_time": True,
        "extracted_bid_notes": "Walk on Tuesday.",
        "resolved_gc_id": "gc-1",
        "resolved_gc_contact_id": "c1",
        "gc_candidates": [],
        "sibling_of_email_id": None,
        "harvest_id": None,
        "continued_by": None,
        "created_project_id": None,
        "excluded_project_ids": [],
        # Present and null on a bench deployment: the 4.6 guard and the
        # sibling read scope themselves by it exactly as the sweep does.
        "test_session_id": None,
    }
    row.update(over)
    return row


def _harvest(**over):
    row = {
        "id": HV,
        "rfp_email_id": E1,
        "method": "procore",
        "external_key": "procore:1:2",
        "external_url": "https://app.procore.com/1/company/planroom/route_to_bid_sheet/2",
        "status": "complete",
        "project_id": None,
        "create_claim_token": None,
        "create_claimed_at": None,
        "data": {
            "platform": "procore",
            "project_name": "Warehouse HVAC Replacement",
            "project_address": "1 Industrial Way, Las Vegas, NV",
            "bid_due_at": "2026-10-02T20:00:00+00:00",
            "gc": {"name": "Monument Construction"},
        },
        "description_text": "Replace the coolers.",
        "instructions_text": "Submit through Procore.",
        "files": [
            {"file_path": "Bid_Drawings/E-1.pdf", "kind": "drawing", "discipline": "Electrical",
             "sandbox_file_id": "f-1", "status": "accepted"},
            {"file_path": "Specs/26.pdf", "kind": "specification", "sandbox_file_id": "f-2",
             "status": "reused"},
            {"file_path": "big.pdf", "kind": "other", "sandbox_file_id": None, "status": "too_large"},
        ],
    }
    row.update(over)
    return row


def _invitation(**over):
    row = {
        "id": "inv-1",
        "portal": "ngem",
        "agency": "UNLV",
        "bid_number": "5584-GS",
        "bid_number_raw": "5584-GS Addendum 1",
        "title": "Emergency Phone Tower Upgrades",
        "close_at": "2026-10-03T21:00:00+00:00",
        "first_seen_at": "2026-09-14T13:30:00+00:00",
        "status": "create",
        "harvest_id": None,
        "created_project_id": None,
    }
    row.update(over)
    return row


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
        "stage_events": [],
        "project_category_state": [],
        "general_contractors": [{"id": "gc-1", "name": "GC Example Builders"},
                                {"id": "gc-2", "name": "Bob's Construction"}],
        "gc_contacts": [{"id": "c1", "gc_id": "gc-1", "email": "pm@gc.example", "name": "GC PM"},
                        {"id": "c2", "gc_id": "gc-2", "email": "bob@gmail.com", "name": "Bob"}],
        "llm_jobs": [],
        "audit_log": [],
        "notifications": [],
    })
    fake.unique = {**FakeDB.unique, "rfp_created_projects": [("project_id",)]}
    # The `on delete cascade` children a compensating project delete cleans.
    fake.fk_cascade = {
        **FakeDB.fk_cascade,
        "projects": [("rfp_created_projects", "project_id"), ("project_gcs", "project_id"),
                     ("stage_events", "project_id"), ("project_category_state", "project_id")],
    }
    fake.defaults = {
        **FakeDB.defaults,
        "llm_jobs": {"status": lambda: "queued", "attempts": lambda: 0,
                     "created_at": lambda: NOW.isoformat()},
        "rfp_created_projects": {"created_at": lambda: NOW.isoformat()},
        # The column the create-time duplicate guard reads (4.6).
        "projects": {"created_at": lambda: NOW.isoformat()},
    }
    fake.fail_insert = set()
    fake.inserts = []
    return fake


@pytest.fixture
def rec():
    """What the creation records instead of running: the advance, the entry
    action, the bells, the audit rows and the rescan threads."""
    return {"advance": [], "entry": [], "bells": [], "audits": [], "rescans": [], "numbers": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch, db, rec):
    settings = _settings()
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
            {"role": role, "project_id": pid, "type": type_, "message": message,
             "mirror_email": k.get("mirror_email"), "metadata": k.get("metadata")}),
    )
    monkeypatch.setattr(
        rc, "audit",
        lambda actor, action, entity, entity_id, payload=None: rec["audits"].append(
            {"actor_id": actor, "action": action, "entity": entity, "entity_id": entity_id,
             "payload": payload}),
    )
    monkeypatch.setattr(rc, "_start_rescan", lambda pid: rec["rescans"].append(pid))

    def insert_project(sb, payload, *, budgetary=False):
        # `budgetary` is the B suffix (BuildingConnected BUDGET requests);
        # every row here is a plain proposal, so the seam takes and ignores it.
        NEXT["counter"] += 1
        payload["number"] = f"26.9.{NEXT['counter']:04d}{'B' if budgetary else ''}"
        rec["numbers"].append(payload["number"])
        return sb.table("projects").insert(payload).execute().data[0]

    monkeypatch.setattr(rc, "_insert_project", insert_project)
    NEXT["counter"] = 7203


def _created(db):
    return db.tables["rfp_created_projects"]


def _projects(db):
    return db.tables["projects"]


def _email_row(db, eid=E1):
    return next(r for r in db.tables["rfp_emails"] if r["id"] == eid)


# ── Facts (4.3) ──────────────────────────────────────────────────────────


def test_facts_for_a_procore_harvest_prefer_the_platforms_facts():
    facts = rc.facts_for_email(_email(), _harvest())
    assert facts.name == "Warehouse HVAC Replacement"
    assert facts.actual_bid_at == "2026-10-02T20:00:00+00:00" and facts.bid_time_unknown is False
    assert facts.invitation_at == "2026-09-15T18:30:00+00:00"
    assert facts.address == "1 Industrial Way, Las Vegas, NV"
    assert facts.bidding_url == "https://app.procore.com/1/company/planroom/route_to_bid_sheet/2"
    assert facts.no_bidding_url is False
    assert facts.bid_notes == "Walk on Tuesday.\n\nSubmit through Procore.\n\nReplace the coolers."
    assert facts.notes == (
        "Created from RFP invitation Invitation to Bid: Warehouse HVAC received 2026-09-15 (organic)"
    )
    assert facts.is_ngem is False
    # The notes cap applies to the joined text.
    assert rc.facts_for_email(_email(), _harvest(), notes_max_chars=10).bid_notes == "Walk on Tu"
    # A name a person typed ("Set project name" stamps extract_model = human)
    # wins over the platform's; a model's extraction does not.
    human = _email(extracted_project_name="Warehouse HVAC (Phase 2)", extract_model="human")
    assert rc.facts_for_email(human, _harvest()).name == "Warehouse HVAC (Phase 2)"
    assert rc.facts_for_email(dict(human, extract_model="qwen3.5-4b"), _harvest()).name == "Warehouse HVAC Replacement"
    assert rc.facts_for_email(dict(human, extracted_project_name="  "), _harvest()).name == "Warehouse HVAC Replacement"
    assert rc.facts_for_email(human, None).name == "Warehouse HVAC (Phase 2)"


def test_facts_for_a_plain_email_fall_back_to_the_extracted_fields():
    row = _email(extracted_bid_due_at="2026-10-01T07:00:00+00:00", extracted_bid_due_has_time=False,
                 subject=None, invitation_method="general")
    facts = rc.facts_for_email(row, None)
    assert facts.name == "Warehouse HVAC" and facts.address is None
    # A date-only extraction: midnight Pacific of that day, time unknown.
    assert facts.actual_bid_at == "2026-10-01T07:00:00+00:00" and facts.bid_time_unknown is True
    assert facts.bidding_url is None and facts.no_bidding_url is True
    assert facts.bid_notes == "Walk on Tuesday."
    assert facts.notes == "Created from RFP invitation (no subject) received 2026-09-15 (general)"
    # No due date anywhere: nothing stored, nothing unknown.
    bare = rc.facts_for_email(_email(extracted_bid_due_at=None, extracted_bid_notes=None), None)
    assert bare.actual_bid_at is None and bare.bid_time_unknown is False and bare.bid_notes is None


def test_bid_due_from_covers_the_date_only_rule():
    # The harvest's date-only platform field: midnight Pacific (PDT in October).
    assert rc.bid_due_from("2026-10-01", None, None) == ("2026-10-01T07:00:00+00:00", True)
    # A harvest instant wins over the extraction.
    assert rc.bid_due_from("2026-10-02T20:00:00Z", "2026-10-01T21:00:00+00:00", True) == (
        "2026-10-02T20:00:00+00:00", False
    )
    # Garbage in the harvest falls back to the extraction.
    assert rc.bid_due_from("soon", "2026-10-01T21:00:00+00:00", True) == ("2026-10-01T21:00:00+00:00", False)
    # A date-only extraction stored at an odd instant is re-derived on its Pacific day.
    assert rc.bid_due_from(None, "2026-10-01T23:30:00+00:00", False) == ("2026-10-01T07:00:00+00:00", True)
    assert rc.bid_due_from(None, None, False) == (None, False)


def test_bidding_url_from_the_email_harvesters_links_with_the_cleaning_rule():
    harvest = _harvest(external_url=None, data={
        "platform": "email",
        "links": [
            {"url": "javascript:alert(1)", "status": "listed"},
            {"url": "https://x.example/f", "status": "unsupported"},
            {"url": "sharepoint.example/sites/bid", "status": "needs_sign_in"},
            {"url": "https://later.example/", "status": "listed"},
        ],
    })
    assert rc.bidding_url_from(harvest) == "https://sharepoint.example/sites/bid"
    assert rc.bidding_url_from(None) is None
    assert rc.bidding_url_from(_harvest(external_url="ftp://x", data={})) is None
    facts = rc.facts_for_email(_email(), _harvest(external_url="ftp://x", data={}))
    assert facts.bidding_url is None and facts.no_bidding_url is True


def test_facts_for_a_portal_invitation():
    facts = rc.facts_for_portal(_invitation(), _harvest(description_text="Notes from the portal."))
    assert facts.name == "Emergency Phone Tower Upgrades"
    assert facts.actual_bid_at == "2026-10-03T21:00:00+00:00" and facts.bid_time_unknown is False
    assert facts.invitation_at == "2026-09-14T13:30:00+00:00"
    assert facts.address is None and facts.bidding_url is None and facts.no_bidding_url is True
    assert facts.bid_notes == "Notes from the portal."
    assert facts.notes == "Created from NGEM UNLV bid 5584-GS Addendum 1"
    assert facts.is_ngem is True
    assert rc.facts_for_portal(_invitation(close_at=None), None).actual_bid_at is None


# ── GC plan (4.4) and the sender marker ──────────────────────────────────


def test_likely_by_domain_filters_server_side_and_caps_the_read(db, monkeypatch):
    from tests.test_rfp_email_ingest import _Query

    seen = []
    real_ilike, real_limit = _Query.ilike, _Query.limit
    monkeypatch.setattr(_Query, "ilike", lambda self, col, pat: seen.append(("ilike", col, pat)) or real_ilike(self, col, pat))
    monkeypatch.setattr(_Query, "limit", lambda self, n: seen.append(("limit", n)) or real_limit(self, n))
    db.tables["gc_contacts"].append({"id": "c4", "gc_id": "gc-2", "email": "x@notgc.example", "name": "X"})
    db.tables["gc_contacts"].append({"id": "c6", "gc_id": "gc-2", "email": "x@sub.gc.example", "name": "X"})
    assert rc._likely_by_domain(db, "new.person@gc.example") == ("gc-1", "GC Example Builders")
    assert seen == [("ilike", "email", "%@gc.example"), ("limit", rc._DOMAIN_CONTACTS_CAP)]
    # Two contacts on gc-2 now (case-insensitive): most contacts wins.
    db.tables["gc_contacts"].append({"id": "c3", "gc_id": "gc-2", "email": "Sub@GC.example", "name": "Sub"})
    db.tables["gc_contacts"].append({"id": "c5", "gc_id": "gc-2", "email": "two@gc.example", "name": "Two"})
    assert rc._likely_by_domain(db, "new.person@gc.example") == ("gc-2", "Bob's Construction")
    # The pattern is a literal: LIKE wildcards in the domain are escaped.
    assert rc._like_literal("g_c%.ex\\ample") == "g\\_c\\%.ex\\\\ample"
    seen.clear()
    assert rc._likely_by_domain(db, "a@g_c.example") == (None, None)
    assert seen[0] == ("ilike", "email", "%@g\\_c.example")
    assert rc._likely_by_domain(db, "a@gmail.com") == (None, None) and rc._likely_by_domain(db, None) == (None, None)


def test_gc_plan_resolved_likely_created_and_none(db):
    plan = rc.gc_plan_for(db, _email(), None)
    assert (plan.kind, plan.gc_id, plan.contact_id, plan.name) == ("resolved", "gc-1", "c1", "GC Example Builders")
    # Likely from the match step's candidates (the top entry).
    plan = rc.gc_plan_for(db, _email(resolved_gc_id=None, resolved_gc_contact_id=None,
                                     gc_candidates=[{"gc_id": "gc-2", "name": "Bob's Construction", "score": 0.6},
                                                    {"gc_id": "gc-1", "name": "GC Example Builders", "score": 0.4}]),
                          None)
    assert (plan.kind, plan.gc_id, plan.name) == ("likely", "gc-2", "Bob's Construction")
    # Likely from a GC contact on the sender's domain; a public domain never infers.
    plan = rc.gc_plan_for(db, _email(resolved_gc_id=None, resolved_gc_contact_id=None,
                                     from_address="new.person@gc.example"), None)
    assert (plan.kind, plan.gc_id, plan.name) == ("likely", "gc-1", "GC Example Builders")
    plan = rc.gc_plan_for(db, _email(resolved_gc_id=None, resolved_gc_contact_id=None,
                                     from_address="someone@gmail.com"), None)
    assert plan.kind == "none"
    # A nonorganic sender with no inference creates the GC from the best name.
    row = _email(resolved_gc_id=None, resolved_gc_contact_id=None, invitation_method="nonorganic",
                 from_address="eve@unknown.example", from_name="Eve Adams")
    plan = rc.gc_plan_for(db, row, _harvest(data={"gc": {"name": "  Monument   Construction "}}))
    assert plan.kind == "created" and plan.name == "Monument Construction"
    assert plan.contact_name == "Eve Adams" and plan.contact_email == "eve@unknown.example"
    plan = rc.gc_plan_for(db, row, None)
    assert plan.name == "GC Example Builders"   # extracted_gc_name next
    plan = rc.gc_plan_for(db, dict(row, extracted_gc_name=None), None)
    assert plan.name == "Eve Adams"             # then the sender's display name
    plan = rc.gc_plan_for(db, dict(row, extracted_gc_name=None, from_name=None), None)
    assert plan.name == "unknown.example"       # then the domain


def test_sender_marker_reads_the_row_and_the_audit_trail_never_the_method(db):
    assert rc.sender_marker(db, _email()) == (False, None)
    assert rc.sender_marker(db, _email(invitation_method="nonorganic", continued_by="u-2")) == (True, "u-2")
    # A `nonorganic` a person set by hand on a row that was never flagged is
    # a correction, not an override: no marker, no allowing user.
    assert rc.sender_marker(db, _email(invitation_method="nonorganic")) == (False, None)
    db.tables["audit_log"].append({"actor_id": "u-9", "action": "rfp_email.continue", "entity": "rfp_email",
                                   "entity_id": E1, "created_at": "2026-09-15T19:00:00+00:00"})
    assert rc.sender_marker(db, _email(invitation_method="nonorganic")) == (True, "u-9")
    # A method correction after the override: the audit trail still says so.
    assert rc.sender_marker(db, _email(invitation_method="organic")) == (True, "u-9")
    # Another row's audit trail is not this row's.
    assert rc.sender_marker(db, _email(id="e-other", invitation_method="nonorganic")) == (False, None)


# ── Creation (4.5) ───────────────────────────────────────────────────────


def test_create_from_email_builds_the_project_and_everything_around_it(db, rec):
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    (project,) = _projects(db)
    assert made == rc.Created(project_id=project["id"], number="26.9.7204", linked=False, files_job=False)
    assert project["number"] == "26.9.7204" and project["name"] == "Warehouse HVAC"
    assert project["actual_bid_at"] == "2026-10-01T21:00:00+00:00"
    assert project["invitation_at"] == "2026-09-15T18:30:00+00:00"
    assert project["bidding_url"] is None and project["no_bidding_url"] is True
    assert project["bid_notes"] == "Walk on Tuesday." and project["is_ngem"] is False
    assert project["is_rebid"] is False and project["created_by"] is None
    assert project["current_stage"] == "intake" and project["current_owner_role"] == "estimating_admin"
    for key in ("internal_bid_at", "due_from_estimator_at", "due_from_vendors_at", "project_type", "scope_fit"):
        assert project.get(key) is None
    # Step 2: the resolved GC with its contact, needs_by on the Pacific day.
    (link,) = db.tables["project_gcs"]
    assert link["project_id"] == project["id"] and link["gc_id"] == "gc-1" and link["needs_by"] == "2026-10-01"
    assert db.tables["project_gc_contacts"] == [{"id": db.tables["project_gc_contacts"][0]["id"],
                                                 "project_gc_id": link["id"], "gc_contact_id": "c1"}]
    # Step 3: the stage event and the category seed, as create_project writes them.
    (event,) = db.tables["stage_events"]
    assert event == {"id": event["id"], "project_id": project["id"], "from_stage": None,
                     "to_stage": "intake", "category": "intake", "actor_id": None}
    seeds = db.tables["project_category_state"]
    assert [s["category"] for s in seeds] == list(rc.workflow.CATEGORY_ORDER)
    assert seeds[0] == {"id": seeds[0]["id"], "project_id": project["id"], "category": "intake",
                        "current_task": "intake", "status": "active", "owner_role": Role.ESTIMATING_ADMIN}
    assert all(s["status"] == "locked" for s in seeds[1:])
    # Step 4: the created record.
    (crec,) = _created(db)
    assert crec["project_id"] == project["id"] and crec["source_kind"] == "rfp_email"
    assert crec["rfp_email_id"] == E1 and crec["portal_invitation_id"] is None and crec["harvest_id"] is None
    assert crec["created_by"] is None and crec["automatic"] is True and crec["invitation_method"] == "organic"
    assert crec["sender_display"] == "GC PM <pm@gc.example>"
    assert crec["sender_was_unauthorized"] is False and crec["sender_allowed_by"] is None
    assert crec["gc_plan"] == "resolved" and crec["likely_gc_id"] is None and crec["likely_gc_name"] is None
    assert crec["bid_time_unknown"] is False and crec["files_status"] == "none"
    # Step 5: into Go/No-Go, parked in review.
    assert rec["advance"] == [(project["id"], "intake", None, "Created from RFP")]
    assert rec["entry"] == [(project["id"], None, "review")]
    # Step 6: the source row.
    row = _email_row(db)
    assert row["status"] == "created" and row["created_project_id"] == project["id"]
    assert row["decided_at_step"] == "create" and row["next_attempt_at"] is None and row["last_error"] is None
    assert row["flag_reason"] == "no_candidate"
    # Step 8: no harvest, no files job.
    assert db.tables["llm_jobs"] == []
    # Step 9: the two bells with the missing list, and the audit row.
    exec_bell, admin_bell = rec["bells"]
    assert exec_bell["role"] == Role.EXECUTIVE and exec_bell["type"] == "rfp_create.created"
    assert exec_bell["message"] == (
        "Project 26.9.7204 Warehouse HVAC was created from an RFP invitation and is waiting in Go/No-Go"
    )
    assert exec_bell["mirror_email"] is True
    assert admin_bell["role"] == Role.ESTIMATING_ADMIN and admin_bell["type"] == "rfp_create.intake_needed"
    assert admin_bell["message"] == (
        "Project 26.9.7204 Warehouse HVAC was created from an RFP invitation; intake details needed: "
        "internal bid date, estimator due, vendor due, Go/No-Go answers"
    )
    missing = ["internal_bid_at", "due_from_estimator_at", "due_from_vendors_at", "project_type", "owner_type",
               "labor_needed", "bid_method", "competitor_known", "gc_known", "subs_needed", "est_value_band",
               "scope_fit"]
    assert admin_bell["metadata"] == {"project_id": project["id"], "number": "26.9.7204", "missing": missing,
                                      "source_kind": "rfp_email"}
    assert exec_bell["metadata"] == admin_bell["metadata"]
    (a,) = rec["audits"]
    assert a["action"] == "rfp_create.create" and a["entity"] == "project" and a["entity_id"] == project["id"]
    assert a["payload"] == {"source_kind": "rfp_email", "source_id": E1, "harvest_id": None,
                            "number": "26.9.7204", "automatic": True}
    # Step 10: the learn-back.
    assert rec["rescans"] == [project["id"]]
    # The order of the core writes (4.5): the created record right behind
    # the project, before the GC links and the state seed.
    assert db.inserts == ["projects", "rfp_created_projects", "project_gcs", "project_gc_contacts",
                          "stage_events", *(["project_category_state"] * len(rc.workflow.CATEGORY_ORDER))]


def test_create_with_a_harvest_claims_links_mates_and_enqueues_the_files_job(db, rec, monkeypatch):
    real_enqueue = rc.llm_queue.enqueue
    status_at_enqueue = []

    def enqueue(*a, **k):
        status_at_enqueue.append(_created(db)[0]["files_status"])
        return real_enqueue(*a, **k)

    monkeypatch.setattr(rc.llm_queue, "enqueue", enqueue)
    db.tables["rfp_emails"].append(_email(harvest_id=HV, extracted_bid_due_has_time=False))
    db.tables["rfp_emails"].append(_email(id="e-2", status="done", harvest_id=HV, flag_reason="no_candidate"))
    db.tables["rfp_emails"].append(_email(id="e-3", status="done", harvest_id=None, flag_reason="sibling",
                                          sibling_of_email_id=E1))
    db.tables["rfp_emails"].append(_email(id="e-4", status="review_match", harvest_id=HV))
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-9", status="done", harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    made = rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    (project,) = _projects(db)
    assert made.files_job is True and made.linked is False
    # The harvest's facts won: name, address, the instant bid due date, the link.
    assert project["name"] == "Warehouse HVAC Replacement" and project["address"].startswith("1 Industrial")
    assert project["actual_bid_at"] == "2026-10-02T20:00:00+00:00" and project["created_by"] == "u-1"
    assert project["bidding_url"].startswith("https://app.procore.com/") and project["no_bidding_url"] is False
    (crec,) = _created(db)
    assert crec["bid_time_unknown"] is False and crec["harvest_id"] == HV and crec["automatic"] is False
    assert crec["created_by"] == "u-1" and crec["files_status"] == "pending"
    # Step 7: the harvest carries the project, the claim is released.
    (hv,) = db.tables["rfp_harvests"]
    assert hv["project_id"] == project["id"] and hv["create_claim_token"] is None and hv["create_claimed_at"] is None
    # The mates: the harvest sharer at done, the sibling follower, the portal sharer; not the row at review_match.
    for eid in ("e-2", "e-3"):
        row = _email_row(db, eid)
        assert row["status"] == "created" and row["created_project_id"] == project["id"], eid
        assert row["decided_at_step"] == "create"
    assert _email_row(db, "e-3")["flag_reason"] == "sibling"
    assert _email_row(db, "e-4")["status"] == "review_match" and _email_row(db, "e-4").get("created_project_id") is None
    (inv,) = db.tables["rfp_portal_invitations"]
    assert inv["status"] == "created" and inv["created_project_id"] == project["id"]
    # Step 8: the files job at its priority, one per project.
    (job,) = db.tables["llm_jobs"]
    assert job["job_type"] == "rfp_create_files" and job["target_id"] == project["id"]
    # The payload names the harvest to promote (4.6): the job never has to
    # guess it off a record that may belong to another invitation.
    assert job["project_id"] == project["id"]
    assert job["payload"] == {"project_id": project["id"], "harvest_id": HV}
    assert job["priority"] == 160 and job["feature"] == "rfp_create" and job["created_by"] == "u-1"
    assert rec["audits"][-1]["payload"]["harvest_id"] == HV and rec["audits"][-1]["payload"]["automatic"] is False
    # The record was `pending` BEFORE the job existed (the fence of 4.5 step 8).
    assert status_at_enqueue == ["pending"]


def test_a_harvest_that_already_has_a_project_links_the_row_instead(db, rec):
    db.tables["projects"].append({"id": "p-old", "number": "26.9.7100", "name": "Old"})
    db.tables["rfp_harvests"].append(_harvest(project_id="p-old"))
    db.tables["rfp_emails"].append(_email(status="done", harvest_id=HV))
    made = rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    assert made == rc.Created(project_id="p-old", number="26.9.7100", linked=True, files_job=False)
    assert len(_projects(db)) == 1 and _created(db) == [] and rec["bells"] == [] and rec["advance"] == []
    row = _email_row(db)
    assert row["status"] == "created" and row["created_project_id"] == "p-old"
    (a,) = rec["audits"]
    assert a["action"] == "rfp_create.link" and a["entity"] == "rfp_email" and a["entity_id"] == E1
    assert a["payload"] == {"project_id": "p-old", "number": "26.9.7100", "harvest_id": HV}


def test_a_held_claim_waits_and_a_stale_one_is_taken_over(db, rec):
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest(create_claim_token="other", create_claimed_at=NOW.isoformat()))
    with pytest.raises(rc.CreateInProgress):
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert _projects(db) == [] and _email_row(db)["status"] == "create"
    assert db.tables["rfp_harvests"][0]["create_claim_token"] == "other"
    # The other worker finished meanwhile: the re-read links.
    db.tables["rfp_harvests"][0]["project_id"] = "p-other"
    db.tables["projects"].append({"id": "p-other", "number": "26.9.7150", "name": "Other"})
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-other"
    # A claim older than RFP_CREATE_CLAIM_SECONDS is stale and taken over. A
    # different invitation (its own subject and name), so the create-time
    # duplicate guard (4.6) has nothing to say about it.
    db.tables["rfp_emails"].append(_email(id="e-5", harvest_id="hv-2",
                                          subject="Invitation to Bid: Clinic Fit-Out",
                                          extracted_project_name="Clinic Fit-Out"))
    stale = (NOW - timedelta(seconds=601)).isoformat()
    db.tables["rfp_harvests"].append(_harvest(id="hv-2", external_key="procore:1:3", create_claim_token="dead",
                                              create_claimed_at=stale))
    made = rc.create_from_email(db, _email_row(db, "e-5"), actor_id=None, automatic=True)
    assert made.linked is False
    hv2 = next(h for h in db.tables["rfp_harvests"] if h["id"] == "hv-2")
    assert hv2["project_id"] == made.project_id and hv2["create_claim_token"] is None


def test_the_lost_race_deletes_the_project_and_releases_the_claim(db, rec):
    db.tables["rfp_emails"].append(_email(status="review_match", harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    stale_view = _email(status="create", harvest_id=HV)   # what this worker read before the row moved
    with pytest.raises(rc.CreateRefused):
        rc.create_from_email(db, stale_view, actor_id=None, automatic=True)
    assert _projects(db) == [] and _created(db) == []
    assert db.tables["project_gcs"] == [] and db.tables["stage_events"] == []
    (hv,) = db.tables["rfp_harvests"]
    assert hv["project_id"] is None and hv["create_claim_token"] is None
    assert _email_row(db)["status"] == "review_match" and rec["bells"] == [] and rec["rescans"] == []
    (a,) = rec["audits"]
    assert a["action"] == "rfp_create.lost_race" and a["entity"] == "project"
    assert a["payload"]["source_id"] == E1 and a["payload"]["harvest_id"] == HV


def test_a_core_failure_compensates_and_re_raises(db, rec):
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    db.fail_insert = {"rfp_created_projects"}
    with pytest.raises(RuntimeError):
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert _projects(db) == [] and _created(db) == [] and _email_row(db)["status"] == "create"
    assert db.tables["rfp_harvests"][0]["create_claim_token"] is None
    assert rec["advance"] == [] and rec["bells"] == []
    (a,) = rec["audits"]
    assert a["action"] == "rfp_create.create_failed" and "refused (test)" in a["payload"]["error"]
    # The record is the first write after the project: nothing else was tried.
    assert db.inserts == ["projects"]
    # A failure in a later core write (the state seed) cascades through the
    # record too: no project the page cannot see is ever left behind.
    db.inserts.clear()
    db.fail_insert = {"stage_events"}
    with pytest.raises(RuntimeError):
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert _projects(db) == [] and _created(db) == [] and db.tables["project_gcs"] == []
    assert db.inserts == ["projects", "rfp_created_projects", "project_gcs", "project_gc_contacts"]
    # A failure before the project exists (the insert itself) only releases the claim.
    db.fail_insert = {"projects"}
    with pytest.raises(RuntimeError):
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert db.tables["rfp_harvests"][0]["create_claim_token"] is None and len(rec["audits"]) == 2


def test_compensation_takes_back_the_gc_and_contact_this_call_inserted(db, rec):
    db.tables["rfp_emails"].append(_email(invitation_method="nonorganic", resolved_gc_id=None,
                                          resolved_gc_contact_id=None, from_address="eve@unknown.example",
                                          from_name="Eve Adams", extracted_gc_name="Monument Construction"))
    before = len(db.tables["general_contractors"]), len(db.tables["gc_contacts"])
    db.fail_insert = {"stage_events"}
    with pytest.raises(RuntimeError):
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert _projects(db) == [] and _created(db) == []
    assert (len(db.tables["general_contractors"]), len(db.tables["gc_contacts"])) == before
    assert not any(g["name"] == "Monument Construction" for g in db.tables["general_contractors"])
    assert not any(c["email"] == "eve@unknown.example" for c in db.tables["gc_contacts"])
    # The lost race compensates the same way.
    db.fail_insert = set()
    db.tables["rfp_emails"].append(_email(id="e-2", status="review_match", invitation_method="nonorganic",
                                          resolved_gc_id=None, resolved_gc_contact_id=None,
                                          from_address="eve@unknown.example", from_name="Eve Adams",
                                          extracted_gc_name="Monument Construction"))
    stale_view = dict(_email_row(db, "e-2"), status="create")
    with pytest.raises(rc.CreateRefused):
        rc.create_from_email(db, stale_view, actor_id=None, automatic=True)
    assert (len(db.tables["general_contractors"]), len(db.tables["gc_contacts"])) == before
    # A REUSED GC is never taken back: it was not this call's.
    db.tables["rfp_emails"].append(_email(id="e-3", invitation_method="nonorganic", resolved_gc_id=None,
                                          resolved_gc_contact_id=None, from_address="eve@unknown.example",
                                          from_name="Eve Adams", extracted_gc_name="  bob's construction "))
    db.fail_insert = {"stage_events"}
    with pytest.raises(RuntimeError):
        rc.create_from_email(db, _email_row(db, "e-3"), actor_id=None, automatic=True)
    assert any(g["id"] == "gc-2" for g in db.tables["general_contractors"])
    assert (len(db.tables["general_contractors"]), len(db.tables["gc_contacts"])) == before


def test_best_effort_steps_never_delete_the_project(db, rec, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("workflow down")

    monkeypatch.setattr(rc.workflow, "advance_category", boom)
    monkeypatch.setattr(rc.llm_queue, "enqueue", boom)
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    (project,) = _projects(db)
    assert made.project_id == project["id"] and made.files_job is False
    (crec,) = _created(db)
    assert crec["files_status"] == "none" and "Document promotion could not be queued" in crec["last_error"]
    assert _email_row(db)["status"] == "created" and len(rec["bells"]) == 2
    assert rec["audits"][-1]["action"] == "rfp_create.create"


@pytest.mark.parametrize(
    "over, words",
    [
        ({"status": "match"}, "not at a stage"),
        ({"status": "created", "created_project_id": "p"}, "already created"),
        ({"status": "done", "created_project_id": "p"}, "already created"),
        ({"flag_reason": "sibling"}, "another copy of the same message"),
        ({"extracted_project_name": "  "}, "no project name"),
        ({"extracted_project_name": None}, "no project name"),
        # The match step's emptiness rule: generic words are no name.
        ({"extracted_project_name": "Invitation to Bid"}, "no project name"),
        ({"extracted_project_name": "RFP 2026-01"}, "no project name"),
    ],
)
def test_email_preconditions(db, rec, over, words):
    with pytest.raises(rc.CreateRefused) as exc:
        rc.create_from_email(db, _email(**over), actor_id=None, automatic=True)
    assert words in str(exc.value)
    assert _projects(db) == [] and rec["audits"] == []


def test_portal_preconditions(db, rec):
    for over, words in (({"status": "harvest"}, "not at a stage"),
                        ({"status": "created", "created_project_id": "p"}, "already created"),
                        ({"title": ""}, "no project name")):
        with pytest.raises(rc.CreateRefused) as exc:
            rc.create_from_portal(db, _invitation(**over), actor_id=None, automatic=True)
        assert words in str(exc.value)
    assert _projects(db) == []


def test_nonorganic_sender_creates_the_gc_and_its_contact(db, rec):
    db.tables["audit_log"].append({"actor_id": "u-9", "action": "rfp_email.continue", "entity": "rfp_email",
                                   "entity_id": E1, "created_at": "x"})
    db.tables["rfp_emails"].append(_email(invitation_method="nonorganic", resolved_gc_id=None,
                                          resolved_gc_contact_id=None, from_address="eve@unknown.example",
                                          from_name="Eve Adams", extracted_gc_name="Monument Construction",
                                          continued_by="u-9"))
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    gc = next(g for g in db.tables["general_contractors"] if g["name"] == "Monument Construction")
    contact = next(c for c in db.tables["gc_contacts"] if c["email"] == "eve@unknown.example")
    assert contact["gc_id"] == gc["id"] and contact["name"] == "Eve Adams"
    (link,) = db.tables["project_gcs"]
    assert link["gc_id"] == gc["id"] and link["project_id"] == made.project_id
    assert db.tables["project_gc_contacts"][0]["gc_contact_id"] == contact["id"]
    (crec,) = _created(db)
    assert crec["gc_plan"] == "created" and crec["sender_was_unauthorized"] is True
    assert crec["sender_allowed_by"] == "u-9"
    assert crec["likely_gc_id"] is None and crec["likely_gc_name"] is None
    # A near-duplicate company name REUSES the existing GC: attached with no
    # contact (an outside address is never added to a real GC by the app)
    # and recorded as a likely GC, not a created one.
    db.tables["rfp_emails"].append(_email(id="e-2", invitation_method="nonorganic", resolved_gc_id=None,
                                          resolved_gc_contact_id=None, from_address="EVE@another.example",
                                          from_name="Eve", extracted_gc_name="  monument  construction"))
    made2 = rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None, automatic=True)
    assert sum(1 for g in db.tables["general_contractors"] if "onument" in g["name"]) == 1
    assert sum(1 for c in db.tables["gc_contacts"] if c["email"].lower() == "eve@unknown.example") == 1
    assert not any(c["email"].lower() == "eve@another.example" for c in db.tables["gc_contacts"])
    link2 = next(lk for lk in db.tables["project_gcs"] if lk["project_id"] == made2.project_id)
    assert link2["gc_id"] == gc["id"]
    assert not any(c["project_gc_id"] == link2["id"] for c in db.tables["project_gc_contacts"])
    crec2 = next(r for r in _created(db) if r["project_id"] == made2.project_id)
    assert crec2["gc_plan"] == "likely" and crec2["likely_gc_id"] == gc["id"]
    assert crec2["likely_gc_name"] == "Monument Construction"
    # Reusing a GC by another sender, whose address the GC never had: same rule.
    db.tables["rfp_emails"].append(_email(id="e-3", invitation_method="nonorganic", resolved_gc_id=None,
                                          resolved_gc_contact_id=None, from_address="pat@elsewhere.example",
                                          from_name="Pat", extracted_gc_name="Bob's Construction"))
    made3 = rc.create_from_email(db, _email_row(db, "e-3"), actor_id=None, automatic=True)
    assert not any(c["email"] == "pat@elsewhere.example" for c in db.tables["gc_contacts"])
    crec3 = next(r for r in _created(db) if r["project_id"] == made3.project_id)
    assert (crec3["gc_plan"], crec3["likely_gc_id"], crec3["likely_gc_name"]) == ("likely", "gc-2", "Bob's Construction")
    assert next(lk for lk in db.tables["project_gcs"] if lk["project_id"] == made3.project_id)["gc_id"] == "gc-2"


def test_likely_gc_candidate_needs_a_score_above_the_floor(db, rec):
    """A match-step candidate below LIKELY_GC_MIN_SCORE (or with no score)
    is noise from a short directory, never a "likely" GC."""
    for cand in ({"gc_id": "gc-2", "name": "Bob's Construction", "score": 0.16},
                 {"gc_id": "gc-2", "name": "Bob's Construction"},
                 {"gc_id": "gc-2", "name": "Bob's Construction", "score": "x"}):
        plan = rc.gc_plan_for(db, {"resolved_gc_id": None, "gc_candidates": [cand],
                                   "from_address": "x@nowhere.example", "invitation_method": "procore"}, None)
        assert plan.kind == "none", cand
    plan = rc.gc_plan_for(db, {"resolved_gc_id": None,
                               "gc_candidates": [{"gc_id": "gc-2", "name": "Bob's Construction", "score": 0.6}],
                               "from_address": "x@nowhere.example", "invitation_method": "procore"}, None)
    assert plan.kind == "likely" and plan.gc_id == "gc-2"


def test_likely_gc_attaches_nothing_and_none_flags_no_gc(db, rec):
    db.tables["rfp_emails"].append(_email(resolved_gc_id=None, resolved_gc_contact_id=None,
                                          gc_candidates=[{"gc_id": "gc-2", "name": "Bob's Construction",
                                                          "score": 0.91}]))
    rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert db.tables["project_gcs"] == []
    (crec,) = _created(db)
    assert crec["gc_plan"] == "likely" and crec["likely_gc_id"] == "gc-2"
    assert crec["likely_gc_name"] == "Bob's Construction"
    db.tables["rfp_emails"].append(_email(id="e-2", resolved_gc_id=None, resolved_gc_contact_id=None,
                                          from_address="x@nowhere.example"))
    rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None, automatic=True)
    assert db.tables["project_gcs"] == [] and _created(db)[-1]["gc_plan"] == "none"


def test_create_from_portal_builds_an_ngem_project_with_no_gc(db, rec):
    db.tables["rfp_portal_invitations"].append(_invitation(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest(method="ngem", external_key="ngem:unlv:5584-GS",
                                              external_url=None, description_text="Portal notes.",
                                              files=[{"file_name": "Plans.pdf", "sandbox_file_id": "f-1",
                                                      "status": "accepted"}]))
    made = rc.create_from_portal(db, db.tables["rfp_portal_invitations"][0], actor_id=None, automatic=True)
    (project,) = _projects(db)
    assert project["name"] == "Emergency Phone Tower Upgrades" and project["is_ngem"] is True
    assert project["no_bidding_url"] is True and project["bidding_url"] is None
    assert project["actual_bid_at"] == "2026-10-03T21:00:00+00:00"
    assert project["invitation_at"] == "2026-09-14T13:30:00+00:00" and project["bid_notes"] == "Portal notes."
    assert project["notes"] == "Created from NGEM UNLV bid 5584-GS Addendum 1"
    assert db.tables["project_gcs"] == []
    (crec,) = _created(db)
    assert crec["source_kind"] == "rfp_portal" and crec["portal_invitation_id"] == "inv-1"
    assert crec["rfp_email_id"] is None and crec["gc_plan"] == "none" and crec["invitation_method"] == "ngem"
    assert crec["sender_display"] is None and crec["sender_was_unauthorized"] is False
    assert crec["files_status"] == "pending" and made.files_job is True
    (inv,) = db.tables["rfp_portal_invitations"]
    assert inv["status"] == "created" and inv["created_project_id"] == project["id"]
    assert rec["bells"][1]["metadata"]["source_kind"] == "rfp_portal"
    assert rec["audits"][-1]["payload"]["source_kind"] == "rfp_portal"


def test_link_harvest_mates_counts_what_it_moved(db):
    db.tables["rfp_emails"].append(_email(id="a", status="done", harvest_id=HV))
    db.tables["rfp_emails"].append(_email(id="b", status="create", harvest_id=HV))
    db.tables["rfp_emails"].append(_email(id="c", status="merged", harvest_id=HV))
    db.tables["rfp_emails"].append(_email(id="d", status="done", sibling_of_email_id="a"))
    db.tables["rfp_portal_invitations"].append(_invitation(id="i", status="done", harvest_id=HV))
    assert rc.link_harvest_mates(db, HV, "p-1", sibling_of="a", exclude_id="a") == 3
    assert _email_row(db, "a")["status"] == "done"
    assert all(_email_row(db, x)["status"] == "created" for x in ("b", "d"))
    assert _email_row(db, "c")["status"] == "merged"
    assert db.tables["rfp_portal_invitations"][0]["status"] == "created"
    assert rc.link_harvest_mates(db, None, "p-1") == 0


def test_number_exhaustion_is_a_refusal_through_the_real_numbering(db, rec, monkeypatch):
    project_numbers = pytest.importorskip("app.services.project_numbers")
    monkeypatch.setattr(rc, "_insert_project", _REAL_INSERT_PROJECT)
    monkeypatch.setattr(project_numbers, "NUMBER_MAX_TRIES", 2)
    db.unique = {**db.unique, "projects": [("number",)]}
    # The real numbering prefixes with the current office month, so seed that month.
    yy, month = project_numbers.prefix_for(datetime.now(timezone.utc))
    prefix = f"{yy}.{month}"
    db.tables["projects"].extend([{"id": "x1", "number": f"{prefix}.7204"}, {"id": "x2", "number": f"{prefix}.7205"}])
    db.tables["rfp_emails"].append(_email())
    with pytest.raises(rc.CreateRefused) as exc:
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert "No free project number" in str(exc.value)
    assert [c[0] for c in db.rpc_calls] == ["next_project_number", "next_project_number"]
    assert len(_projects(db)) == 2 and _email_row(db)["status"] == "create"
    # And with a free number the real path assigns one.
    db.tables["rfp_emails"].append(_email(id="e-2"))
    made = rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None, automatic=True)
    assert made.number == f"{prefix}.7206"


# ── The mirror email subject (section 7) ─────────────────────────────────


def test_intake_missing_phrase_and_the_subject_name_the_fields_in_plain_words():
    missing = ["internal_bid_at", "due_from_estimator_at", "due_from_vendors_at", "project_type",
               "owner_type", "scope_fit", "bid_time"]
    assert notification_email.intake_missing_phrase(missing) == (
        "internal bid date, estimator due, vendor due, Go/No-Go answers, bid time"
    )
    assert notification_email.intake_missing_phrase([]) == ""
    assert notification_email.intake_missing_phrase(["something_new"]) == "something new"
    subject = notification_email._subject(
        notification_email.heading_for("rfp_create.intake_needed"),
        {"number": "26.9.7204", "name": "Warehouse HVAC"},
        "rfp_create.intake_needed",
        {"missing": missing[:4]},
    )
    assert subject == (
        "G3 BDR · Intake details needed for 26.9.7204 Warehouse HVAC: "
        "internal bid date, estimator due, vendor due, Go/No-Go answers"
    )
    # Without a project row the metadata's number stands in; nothing missing, no colon.
    assert notification_email._subject("Intake details needed", None, "rfp_create.intake_needed",
                                       {"number": "26.9.7204", "missing": []}) == (
        "G3 BDR · Intake details needed for 26.9.7204"
    )
    assert notification_email.heading_for("rfp_create.created") == "Project created from an RFP invitation"
    assert notification_email._meta("rfp_create.created")[1] == "Open project"
    # Every other type keeps the house subject.
    assert notification_email._subject("Quote received", {"number": "42", "name": "Acme"}, "quote.received", None) == (
        notification_email._subject("Quote received", {"number": "42", "name": "Acme"})
    )


def test_availability_helpers_and_created_project_ref(db):
    assert rc.email_create_available(_email(status="done")) is True
    assert rc.email_create_available(_email(status="create")) is False
    assert rc.email_create_available(_email(status="done", flag_reason="sibling")) is False
    assert rc.email_create_available(_email(status="done", extracted_project_name=None)) is False
    assert rc.email_create_available(_email(status="done", extracted_project_name="Bid Invitation")) is False
    assert rc.email_create_available(_email(status="done", created_project_id="p")) is False
    assert rc.has_project_name({"extracted_project_name": "Warehouse HVAC"}) is True
    for bad in (None, "", "   ", "Invitation to Bid", "RFP 2026-01", "Request for Proposal", 42):
        assert rc.has_project_name({"extracted_project_name": bad}) is False, bad
    assert rc.portal_create_available(_invitation(status="done")) is True
    assert rc.portal_create_available(_invitation(status="done", title=" ")) is False
    assert rc.created_project_ref(db, None) is None and rc.created_project_ref(db, "nope") is None
    db.tables["projects"].append({"id": "p1", "number": "26.9.7204", "name": "X", "actual_bid_at": "secret"})
    assert rc.created_project_ref(db, "p1") == {"id": "p1", "number": "26.9.7204", "name": "X"}


def test_facts_dataclasses_are_frozen_and_the_settings_exist():
    facts = rc.facts_for_email(_email(), None)
    with pytest.raises(Exception):
        facts.name = "x"   # type: ignore[misc]
    s = Settings(_env_file=None)
    assert s.rfp_create_auto_enabled is False and s.rfp_create_poll_seconds == 30
    assert s.rfp_create_claim_seconds == 600 and s.rfp_create_notes_max_chars == 4000
    assert s.rfp_create_files_queue_priority == 160
    assert s.rfp_create_duplicate_window_minutes == 60
    assert Settings(_env_file=None, rfp_create_duplicate_window_minutes=0
                    ).rfp_create_duplicate_window_minutes == 0
    with pytest.raises(ValueError):
        Settings(_env_file=None, rfp_create_claim_seconds=20)
    with pytest.raises(ValueError):
        Settings(_env_file=None, rfp_create_poll_seconds=1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, rfp_create_duplicate_window_minutes=-1)
    assert copy.deepcopy(facts) == facts


# ── The create-time duplicate guard (4.6) ──────────────────────────────────


@pytest.fixture
def no_insert(monkeypatch):
    """The insert seam, made loud: the guard must return before it."""
    def refuse(sb, payload, *, budgetary=False):
        raise AssertionError(f"a second project was inserted: {payload.get('name')!r}")
    monkeypatch.setattr(rc, "_insert_project", refuse)


def _existing(db, pid="p-first", name="Warehouse HVAC", gc_id="gc-1", minutes_ago=5,
              number="26.9.7150"):
    """A project made `minutes_ago` with a GC link, the way a creation
    leaves it."""
    db.tables["projects"].append({
        "id": pid, "number": number, "name": name,
        "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "abandoned_at": None,
    })
    if gc_id:
        db.tables["project_gcs"].append({"id": f"l-{pid}", "project_id": pid, "gc_id": gc_id,
                                         "relationship": "invited_us"})
    return pid


def _copy(db, eid, minutes_after=2, **over):
    """Another copy of the same invitation: same sender, same subject, same
    (absent) attachments, a couple of minutes apart."""
    row = _email(id=eid, received_at=(datetime.fromisoformat("2026-09-15T18:30:00+00:00")
                                      + timedelta(minutes=minutes_after)).isoformat(), **over)
    db.tables["rfp_emails"].append(row)
    return row


def test_a_sibling_that_already_created_the_project_is_joined_not_duplicated(db, rec, no_insert):
    """The copy that lost the race joins its twin's project. It never
    matters which of the two was received first: the guard reads the window
    in both directions."""
    _existing(db, "p-sib")
    _copy(db, "e-younger", minutes_after=2, status="created", created_project_id="p-sib")
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=True)
    assert made == rc.Created(project_id="p-sib", number="26.9.7150", linked=True, files_job=False,
                              why="sibling", duplicate_of="e-younger")
    assert [p["id"] for p in _projects(db)] == ["p-sib"]       # no second project
    row = _email_row(db)
    assert row["status"] == "created" and row["created_project_id"] == "p-sib"
    assert row["sibling_of_email_id"] == "e-younger" and row["flag_reason"] == "sibling"
    assert row["decided_at_step"] == "create" and row["last_error"] is None
    (a,) = rec["audits"]
    assert a["action"] == "rfp_create.link" and a["entity"] == "rfp_email" and a["entity_id"] == E1
    assert a["payload"]["duplicate_of"] == "e-younger" and a["payload"]["why"] == "sibling"
    assert a["payload"]["project_id"] == "p-sib"
    assert a["payload"]["sibling_of_email_id"] == "e-younger"
    # Nothing else ran: no created record, no bells, no rescan.
    assert _created(db) == [] and rec["bells"] == [] and rec["rescans"] == []


def test_the_sibling_half_of_the_guard_reads_an_older_copy_too(db, rec, no_insert):
    _existing(db, "p-sib")
    _copy(db, "e-older", minutes_after=-3, status="created", created_project_id="p-sib")
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-sib"
    assert _email_row(db)["sibling_of_email_id"] == "e-older"


def test_a_sibling_at_created_with_no_project_has_nothing_to_join(db, rec):
    _copy(db, "e-younger", status="created", created_project_id=None)
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and len(_projects(db)) == 1


def test_a_project_created_minutes_ago_for_the_same_gc_is_joined(db, rec, no_insert):
    """The second half of the guard: no sibling row survived (a different
    Message-ID, a different mailbox), but the project it made is right
    there, under the same GC, with the same name."""
    _existing(db, "p-recent", name="  warehouse   HVAC ")     # normalized: casefold + collapse
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-recent"
    row = _email_row(db)
    assert row["status"] == "created" and row["created_project_id"] == "p-recent"
    # No sibling row was involved, so the row keeps its own flag and link.
    assert row["sibling_of_email_id"] is None and row["flag_reason"] == "no_candidate"
    (a,) = rec["audits"]
    # `duplicate_of` names what was duplicated (here the project itself);
    # the rule that found it is `why`.
    assert a["payload"]["duplicate_of"] == "p-recent" and a["payload"]["why"] == "recent_project"
    assert a["payload"]["sibling_of_email_id"] is None


def test_the_same_name_under_another_gc_is_a_different_project(db, rec):
    _existing(db, "p-other-gc", gc_id="gc-2")
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and made.project_id != "p-other-gc"
    assert {p["id"] for p in _projects(db)} == {"p-other-gc", made.project_id}


def test_a_project_with_the_same_gc_but_another_name_is_a_different_project(db, rec):
    _existing(db, "p-other-name", name="Clinic Fit-Out")
    db.tables["rfp_emails"].append(_email())
    assert rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True).linked is False


def test_a_project_older_than_the_window_is_not_a_duplicate(db, rec):
    _existing(db, "p-old", minutes_ago=61)
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and made.project_id != "p-old"


def test_an_abandoned_project_is_not_joined(db, rec):
    _existing(db, "p-dead")
    db.tables["projects"][0]["abandoned_at"] = NOW.isoformat()
    db.tables["rfp_emails"].append(_email())
    assert rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True).linked is False


def test_the_window_at_zero_turns_the_project_half_of_the_guard_off(db, rec, monkeypatch):
    monkeypatch.setattr(rc, "get_settings",
                        lambda: _settings(rfp_create_duplicate_window_minutes=0))
    _existing(db, "p-recent")
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and made.project_id != "p-recent"
    # The sibling half is not a setting and still runs. Another invitation,
    # so the row just created is not itself a copy of this one.
    other = {"subject": "Invitation to Bid: Clinic Fit-Out",
             "extracted_project_name": "Clinic Fit-Out"}
    _existing(db, "p-sib", name="Clinic Fit-Out", number="26.9.7160")
    _copy(db, "e-younger", status="created", created_project_id="p-sib", **other)
    db.tables["rfp_emails"].append(_email(id="e-2", **other))
    assert rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None,
                                automatic=True).project_id == "p-sib"


def test_a_row_with_no_resolved_gc_gets_only_the_sibling_half(db, rec):
    """A name alone never links: naming two different jobs the same thing is
    common, and deciding they are one project is the matcher's job."""
    _existing(db, "p-recent")
    db.tables["rfp_emails"].append(_email(resolved_gc_id=None, resolved_gc_contact_id=None,
                                          gc_candidates=[]))
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and made.project_id != "p-recent"
    # The sibling check still applies to a row with no GC. Another
    # invitation, so the row just created is not itself a copy of this one.
    other = {"subject": "Invitation to Bid: Clinic Fit-Out",
             "extracted_project_name": "Clinic Fit-Out", "resolved_gc_id": None,
             "gc_candidates": []}
    _copy(db, "e-younger", status="created", created_project_id="p-recent", **other)
    db.tables["rfp_emails"].append(_email(id="e-2", **other))
    made = rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-recent"


def test_the_guard_covers_the_button_path_and_releases_a_harvest_claim(db, rec, no_insert):
    """`automatic=False` is the "Create project" button; a claim taken on
    the way in is handed back, because no project was made under it."""
    _existing(db, "p-sib")
    _copy(db, "e-younger", status="created", created_project_id="p-sib")
    db.tables["rfp_emails"].append(_email(status="done", harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    made = rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    assert made.linked is True and made.project_id == "p-sib"
    (hv,) = db.tables["rfp_harvests"]
    assert hv["create_claim_token"] is None and hv["create_claimed_at"] is None
    assert hv["project_id"] is None          # the project is not this harvest's


def test_a_test_row_records_the_link_it_made_instead_of_a_creation(db, rec, no_insert):
    sid = "sess-1"
    db.tables["rfp_test_events"] = []
    _existing(db, "p-sib")
    _copy(db, "e-younger", status="created", created_project_id="p-sib", test_session_id=sid)
    db.tables["rfp_emails"].append(_email(test_session_id=sid))
    rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    (event,) = db.tables["rfp_test_events"]
    assert event["session_id"] == sid and event["source"] == "create" and event["kind"] == "linked"
    assert event["project_id"] == "p-sib" and event["rfp_email_id"] == E1
    assert event["detail"]["duplicate_of"] == "e-younger" and event["detail"]["why"] == "sibling"


def test_normalized_name_collapses_case_and_whitespace():
    assert rc.normalized_name("  Warehouse   HVAC\n") == "warehouse hvac"
    assert rc.normalized_name(None) == "" and rc.normalized_name("   ") == ""
    assert rc.normalized_name("Warehouse HVAC") == rc.normalized_name("WAREHOUSE hvac")


def test_neither_half_links_to_a_project_an_unmerge_excluded(db, rec):
    """`excluded_project_ids` is the unmerge's whole point: a person said
    this email is NOT that project. The guard never puts it back."""
    _existing(db, "p-recent")
    _copy(db, "e-younger", status="created", created_project_id="p-recent")
    db.tables["rfp_emails"].append(_email(excluded_project_ids=["p-recent"]))
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and made.project_id != "p-recent"
    row = _email_row(db)
    assert row["sibling_of_email_id"] is None and row["flag_reason"] == "no_candidate"


def test_the_sibling_half_skips_an_abandoned_or_deleted_project(db, rec):
    _existing(db, "p-dead")
    db.tables["projects"][0]["abandoned_at"] = NOW.isoformat()
    _copy(db, "e-a", minutes_after=1, status="created", created_project_id="p-dead")
    _copy(db, "e-b", minutes_after=-4, status="created", created_project_id="p-gone")
    db.tables["rfp_emails"].append(_email())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False
    assert made.project_id not in ("p-dead", "p-gone")


def test_the_guard_stays_inside_the_rows_test_bench_scope(db, rec):
    """A test session's project is invisible to a real row, and the other way
    round, for both halves (docs/RFP_TESTING.md 4)."""
    _existing(db, "p-test")
    db.tables["projects"][0]["test_session_id"] = "s1"
    _copy(db, "e-tagged", status="created", created_project_id="p-test", test_session_id="s1")
    db.tables["rfp_emails"].append(_email())          # a real row: sees neither
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is False and made.project_id != "p-test"
    # A row in the session joins the session's project through its own copy.
    db.tables["rfp_emails"].append(_email(id="e-2", test_session_id="s1"))
    made = rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-test"
    assert made.why == "sibling" and made.duplicate_of == "e-tagged"


def test_a_real_row_never_joins_a_test_sessions_recent_project(db, rec):
    _existing(db, "p-test")
    db.tables["projects"][0]["test_session_id"] = "s1"
    db.tables["rfp_emails"].append(_email())
    assert rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True).linked is False
    # And the session's own row joins it.
    db.tables["rfp_emails"].append(_email(id="e-2", test_session_id="s1",
                                          subject="Invitation to Bid: Warehouse HVAC"))
    made = rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-test" and made.why == "recent_project"


def test_the_manual_path_refuses_a_recent_project_instead_of_linking(db, rec, no_insert):
    """A person clicking Create is never silently attached to a project the
    app only inferred; they are told what exists and what to do."""
    _existing(db, "p-recent", minutes_ago=7, number="26.9.7124")
    db.tables["rfp_emails"].append(_email(status="done"))
    with pytest.raises(rc.CreateRefused) as exc:
        rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    assert str(exc.value) == (
        "A project with this name for this GC was created 7 minutes ago: 26.9.7124 Warehouse HVAC. "
        "Merge this email into it, or rename the project, before creating another."
    )
    assert _email_row(db)["status"] == "done" and _email_row(db)["created_project_id"] is None
    assert rec["audits"] == []
    # The automatic path still links.
    db.tables["rfp_emails"].append(_email(id="e-2"))
    made = rc.create_from_email(db, _email_row(db, "e-2"), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-recent" and made.why == "recent_project"


def test_the_manual_refusal_releases_the_harvest_claim(db, rec, no_insert):
    _existing(db, "p-recent")
    db.tables["rfp_emails"].append(_email(status="done", harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    with pytest.raises(rc.CreateRefused):
        rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    (hv,) = db.tables["rfp_harvests"]
    assert hv["create_claim_token"] is None and hv["project_id"] is None


def test_the_manual_path_still_joins_a_copys_project_and_says_why(db, rec, no_insert):
    """A copy of the same message is not an inference: it IS this email."""
    _existing(db, "p-sib")
    _copy(db, "e-younger", status="created", created_project_id="p-sib")
    db.tables["rfp_emails"].append(_email(status="done"))
    made = rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    assert made.linked is True and made.why == "sibling" and made.duplicate_of == "e-younger"
    assert made.project_id == "p-sib"


def _job(db):
    (job,) = db.tables["llm_jobs"]
    return job


def test_a_recent_project_link_adopts_this_rows_harvest_and_documents(db, rec):
    """The other invitation's harvest made the project; THIS row's harvest
    holds documents nobody has promoted, so steps 7 and 8 run against the
    project that exists. The target's record belongs to the OTHER harvest, so
    the payload names this one: the job resolves its work from
    `rfp_created_projects.harvest_id` otherwise and would promote the wrong
    invitation's documents (docs/RFP_CREATE.md 4.6)."""
    _existing(db, "p-recent")
    db.tables["rfp_created_projects"].append({
        "id": "crec-1", "project_id": "p-recent", "source_kind": "rfp_email",
        "files_status": "none", "harvest_id": "hv-other",
    })
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_emails"].append(_email(id="e-mate", status="done", harvest_id=HV,
                                          subject="Invitation to Bid: Warehouse HVAC (mate)"))
    db.tables["rfp_harvests"].append(_harvest(split_job_id="sj-1"))
    db.tables["bid_split_jobs"] = [{"id": "sj-1", "project_id": None}]
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-recent" and made.files_job is True
    (hv,) = db.tables["rfp_harvests"]
    assert hv["project_id"] == "p-recent" and hv["create_claim_token"] is None
    assert db.tables["bid_split_jobs"][0]["project_id"] == "p-recent"
    assert _email_row(db, "e-mate")["created_project_id"] == "p-recent"
    # The record is the other invitation's and is left exactly as it is,
    # apart from the pending fence the job claims through.
    (crec,) = db.tables["rfp_created_projects"]
    assert crec["files_status"] == "pending" and crec["harvest_id"] == "hv-other"
    assert _job(db)["job_type"] == "rfp_create_files" and _job(db)["target_id"] == "p-recent"
    assert _job(db)["payload"] == {"project_id": "p-recent", "harvest_id": HV}
    # No second project, no second created record, no bells.
    assert [p["id"] for p in _projects(db)] == ["p-recent"] and rec["bells"] == []


def test_a_recent_project_link_to_a_new_bid_project_gets_the_record_the_job_needs(db, rec):
    """A project made through New Bid has no `rfp_created_projects` row, and
    the promotion job claims, reports and retries through one: without it the
    `none -> pending` CAS matched nothing, the job was never enqueued, and
    this row's documents were dropped with `rfp_harvests.project_id` already
    set so nothing else would ever pick them up."""
    _existing(db, "p-newbid")
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is True and made.project_id == "p-newbid" and made.files_job is True
    (crec,) = db.tables["rfp_created_projects"]
    assert crec["project_id"] == "p-newbid" and crec["harvest_id"] == HV
    assert crec["files_status"] == "pending" and crec["rfp_email_id"] == E1
    # This call attached no GC; the invitation that made the project did.
    assert crec["gc_plan"] == "none" and crec["automatic"] is True
    assert _job(db)["payload"] == {"project_id": "p-newbid", "harvest_id": HV}
    assert [p["id"] for p in _projects(db)] == ["p-newbid"] and rec["bells"] == []


def test_a_recent_project_link_enqueues_over_a_finished_promotion(db, rec):
    """The target already promoted the other invitation's documents, so its
    record reads `complete`. The fence CASes from the status the record
    actually carries, not from a hardcoded `none`."""
    _existing(db, "p-recent")
    db.tables["rfp_created_projects"].append({
        "id": "crec-1", "project_id": "p-recent", "source_kind": "rfp_email",
        "files_status": "complete", "harvest_id": "hv-other", "files_error": None,
    })
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    assert rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True).files_job is True
    (crec,) = db.tables["rfp_created_projects"]
    assert crec["files_status"] == "pending" and crec["harvest_id"] == "hv-other"
    assert _job(db)["payload"] == {"project_id": "p-recent", "harvest_id": HV}


@pytest.mark.parametrize("status", ["pending", "running"])
def test_a_recent_project_link_says_so_when_a_promotion_is_already_running(db, rec, status):
    """One active job per project: this invitation's documents cannot go in
    behind the other's. The card says so instead of dropping them silently."""
    _existing(db, "p-recent")
    db.tables["rfp_created_projects"].append({
        "id": "crec-1", "project_id": "p-recent", "source_kind": "rfp_email",
        "files_status": status, "harvest_id": "hv-other",
    })
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is True and made.files_job is False
    (crec,) = db.tables["rfp_created_projects"]
    assert crec["files_status"] == status and "Retry documents" in crec["last_error"]
    assert db.tables["llm_jobs"] == []
    # The link itself stands: the harvest points at the project either way.
    assert db.tables["rfp_harvests"][0]["project_id"] == "p-recent"


def test_a_sibling_link_leaves_the_harvest_alone(db, rec, no_insert):
    """A copy of the same message shares the invitation, so its documents are
    already linked and promoted by the copy that made the project."""
    _existing(db, "p-sib")
    _copy(db, "e-younger", status="created", created_project_id="p-sib")
    db.tables["rfp_emails"].append(_email(harvest_id=HV))
    db.tables["rfp_harvests"].append(_harvest())
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert made.linked is True and made.files_job is False
    (hv,) = db.tables["rfp_harvests"]
    assert hv["project_id"] is None and hv["create_claim_token"] is None
    assert db.tables["llm_jobs"] == []


def test_link_harvest_mates_walks_a_sibling_chain(db):
    """A chain forms when a follower is itself the earliest decided copy on a
    later tick; one hop would strand the far end (docs/RFP_MATCHING.md 3.1)."""
    db.tables["rfp_emails"].append(_email(id="root", status="done"))
    db.tables["rfp_emails"].append(_email(id="mid", status="done", sibling_of_email_id="root"))
    db.tables["rfp_emails"].append(_email(id="leaf", status="done", sibling_of_email_id="mid"))
    db.tables["rfp_emails"].append(_email(id="twig", status="create", sibling_of_email_id="leaf"))
    db.tables["rfp_emails"].append(_email(id="elsewhere", status="done"))
    assert rc.link_harvest_mates(db, None, "p-1", sibling_of="root", exclude_id="root") == 3
    assert all(_email_row(db, x)["created_project_id"] == "p-1"
               for x in ("mid", "leaf", "twig"))
    assert _email_row(db, "elsewhere").get("created_project_id") is None
    # A level already at `created` still leads to its own followers.
    db.tables["rfp_emails"].append(_email(id="r2", status="created", created_project_id="p-1"))
    db.tables["rfp_emails"].append(_email(id="c2", status="done", sibling_of_email_id="r2"))
    assert rc.link_harvest_mates(db, None, "p-1", sibling_of="r2", exclude_id="r2") == 1
    assert _email_row(db, "c2")["created_project_id"] == "p-1"


def test_the_sibling_follower_read_is_capped_and_says_so(db, caplog):
    """The frontier read is capped like `rfp_match.read_siblings`: copies of
    one message are a handful, so a full page is logged rather than silently
    truncated."""
    cap = rc.rfp_match.SIBLING_READ_CAP
    db.tables["rfp_emails"].append(_email(id="root", status="done"))
    for i in range(cap):
        db.tables["rfp_emails"].append(
            _email(id=f"f{i}", status="done", sibling_of_email_id="root")
        )
    with caplog.at_level("WARNING"):
        moved = rc.link_harvest_mates(db, None, "p-1", sibling_of="root", exclude_id="root")
    assert moved == cap and "hit the" in caplog.text and str(cap) in caplog.text
    # A normal group is not capped and logs nothing.
    caplog.clear()
    db.tables["rfp_emails"].append(_email(id="r2", status="done"))
    db.tables["rfp_emails"].append(_email(id="c2", status="done", sibling_of_email_id="r2"))
    with caplog.at_level("WARNING"):
        assert rc.link_harvest_mates(db, None, "p-2", sibling_of="r2", exclude_id="r2") == 1
    assert caplog.text == ""


def test_recent_project_sentence_names_the_project_and_its_age():
    project = {"id": "p", "number": "26.9.7124", "name": "Warehouse HVAC",
               "created_at": (NOW - timedelta(minutes=12)).isoformat()}
    assert rc.recent_project_sentence(project) == (
        "A project with this name for this GC was created 12 minutes ago: 26.9.7124 Warehouse HVAC. "
        "Merge this email into it, or rename the project, before creating another."
    )
    # Missing pieces never break the sentence.
    assert "another project" in rc.recent_project_sentence(None)
    assert "0 minutes ago" in rc.recent_project_sentence({"number": "26.9.1", "created_at": None})


# ── The RFP test bench's tag and event (docs/RFP_TESTING.md 4.4) ───────────


def test_a_test_rows_creation_tags_every_row_it_inserts_and_records(db, rec):
    from app.services import rfp_test

    sid = "aaaaaaaa-0000-4000-8000-000000000001"
    db.tables["rfp_test_events"] = []
    db.tables["rfp_emails"].append(_email(invitation_method="nonorganic", resolved_gc_id=None,
                                          resolved_gc_contact_id=None, from_address="eve@unknown.example",
                                          from_name="Eve Adams", extracted_gc_name="Monument Construction",
                                          continued_by="u-9", test_session_id=sid))
    made = rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    (project,) = _projects(db)
    assert project["test_session_id"] == sid
    (crec,) = _created(db)
    assert crec["test_session_id"] == sid
    gc = next(g for g in db.tables["general_contractors"] if g["name"] == "Monument Construction")
    contact = next(c for c in db.tables["gc_contacts"] if c["email"] == "eve@unknown.example")
    assert gc["test_session_id"] == sid and contact["test_session_id"] == sid
    # The seed GCs were never tagged.
    assert all("test_session_id" not in g for g in db.tables["general_contractors"] if g["id"] in ("gc-1", "gc-2"))
    (event,) = db.tables["rfp_test_events"]
    assert event["session_id"] == sid and event["source"] == "create" and event["kind"] == "created"
    assert event["project_id"] == made.project_id and event["rfp_email_id"] == E1
    detail = event["detail"]
    assert detail["number"] == "26.9.7204" and detail["gc_plan"] == "created" and detail["gc_inserted"] is True
    assert detail["contact_inserted"] is True and detail["sender_was_unauthorized"] is True
    assert detail["notifications"] == ["executive", "estimating_admin"] and detail["automatic"] is True
    # A refusal on a test row is recorded too; a normal row records nothing.
    with pytest.raises(rc.CreateRefused):
        rc.create_from_email(db, _email_row(db), actor_id=None, automatic=True)
    assert [e["kind"] for e in db.tables["rfp_test_events"]] == ["created", "failed"]
    assert db.tables["rfp_test_events"][1]["level"] == "warn"
    db.tables["rfp_emails"].append(_email(id="e-plain", subject="Another"))
    rc.create_from_email(db, _email_row(db, "e-plain"), actor_id=None, automatic=True)
    assert len(db.tables["rfp_test_events"]) == 2
    assert "test_session_id" not in _projects(db)[1]
    assert rfp_test.session_for_email(_email_row(db, "e-plain")) is None


# ── A split still filing the documents (docs/RFP_SPLIT.md 10.5) ─────────────


def test_create_waits_while_a_mate_row_is_still_splitting(db, rec, monkeypatch):
    """Create on a `done` copy while the leader row (same harvest) is still
    at `split`: refused with the sentence (the routers answer 409), nothing
    made, the harvest claim never taken. Once no row sits at `split` the
    same press creates."""
    monkeypatch.setattr(rc, "get_settings", lambda: _settings(bid_file_splitter_enabled=True, rfp_split_enabled=True))
    db.tables["rfp_harvests"].append(_harvest(split_status="running", split_job_id="job-1"))
    db.tables["rfp_emails"].append(_email(status="done", harvest_id=HV))
    db.tables["rfp_emails"].append(_email(id="e-leader", status="split", harvest_id=HV, subject="Reminder"))
    with pytest.raises(rc.CreateWaitingForSplit) as exc:
        rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    assert isinstance(exc.value, rc.CreateRefused)
    assert str(exc.value) == (
        "The documents for this invitation are still being split. "
        "The project will be created when the split finishes."
    )
    assert _projects(db) == [] and db.tables["rfp_harvests"][0]["create_claim_token"] is None
    # The leader finished its split (terminal): the press creates.
    _email_row(db, "e-leader")["status"] = "create"
    db.tables["rfp_harvests"][0]["split_status"] = "complete"
    made = rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False)
    assert made.linked is False and len(_projects(db)) == 1


def test_create_never_waits_on_a_split_nothing_will_move(db, rec, monkeypatch):
    """No deadlock: a `none` harvest with no row at `split` (the flags came
    on later, a harvest from before the step), a `running` harvest whose
    leader gave up and moved on, or the split step off: creation proceeds."""
    on = _settings(bid_file_splitter_enabled=True, rfp_split_enabled=True)
    monkeypatch.setattr(rc, "get_settings", lambda: on)
    db.tables["rfp_harvests"].append(_harvest(split_status="none"))
    db.tables["rfp_emails"].append(_email(status="done", harvest_id=HV))
    db.tables["rfp_emails"].append(_email(id="e-leader", status="create", harvest_id=HV))
    assert rc.create_from_email(db, _email_row(db), actor_id="u-1", automatic=False).linked is False
    # The split step off: a mate at `split` does not hold the press.
    monkeypatch.setattr(rc, "get_settings", lambda: _settings(rfp_split_enabled=False))
    db.tables["rfp_harvests"].append(_harvest(id="hv-2", split_status="none"))
    db.tables["rfp_emails"].append(_email(id="e-5", status="done", harvest_id="hv-2", subject="Other"))
    db.tables["rfp_emails"].append(_email(id="e-6", status="split", harvest_id="hv-2", subject="Other 2"))
    assert rc.create_from_email(db, _email_row(db, "e-5"), actor_id="u-1", automatic=False).linked is False
