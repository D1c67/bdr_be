"""The "Created from RFP Ingestion" router (app/routers/rfp_created) called as plain
functions against the in-memory fake Supabase from
tests/test_rfp_email_ingest (docs/RFP_CREATE.md sections 7 and 8).

Pinned:

- the route table (five routes), the flag gate (fail closed; 404 while
  neither the email intake nor the NGEM slice is served) and the role gate
  (Estimating Admin, Executive, IT Admin; everyone else 403);
- the list: the section 8 shape, newest first, `before` + `before_id`
  paging on the (created_at, project_id) pair (rows created in the same
  instant never repeat or vanish), the cleared filter, the flags (missing
  files, no GC, likely GC, the intake list, the sender marker, skipped
  documents, a failed promotion, a best-effort step's `last_error`), one
  query per table over the page;
- clear / restore (409 when already there, audited) and retry-files (the
  record `pending` BEFORE the enqueue, the prior status restored when the
  enqueue fails, a stale `running` claim accepted, the refusals), and the
  counts;
- the filters (migration 0135): the creation window (`created_from`
  inclusive, `created_to` exclusive, 400 on junk or an empty window), the
  search box over the `rfp_created_search` view (every field, wildcards and
  PostgREST filter syntax as literal text, paging on the pair), the counts
  under the same filter, and the no-param counts unchanged for the badge.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import rfp_created as rr
from app.services import llm_queue, rfp_create_files
from tests.test_rfp_email_ingest import FakeDB

NOW = datetime(2026, 9, 16, 17, 0, 0, tzinfo=timezone.utc)
P1 = "5a000000-0000-4000-8000-000000000001"
P2 = "5a000000-0000-4000-8000-000000000002"
P3 = "5a000000-0000-4000-8000-000000000003"
MISSING = "5a000000-0000-4000-8000-0000000000ff"
E1 = "3f1c0b00-0000-4000-8000-000000000001"
I1 = "9c000000-0000-4000-8000-000000000001"
HV = "8d000000-0000-4000-8000-000000000001"


class CreatedDB(FakeDB):
    def table(self, name):
        query = super().table(name)
        if name == "llm_jobs":
            inherited = query._check_unique

            def check(rows, payload):
                inherited(rows, payload)
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


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _rec(project_id, *, minutes_ago=0, **over):
    row = {
        "project_id": project_id,
        "source_kind": "rfp_email",
        "rfp_email_id": E1,
        "portal_invitation_id": None,
        "harvest_id": HV,
        "created_by": None,
        "automatic": True,
        "invitation_method": "organic",
        "sender_display": "GC PM <pm@gc.example>",
        "sender_was_unauthorized": False,
        "sender_allowed_by": None,
        "gc_plan": "resolved",
        "likely_gc_id": None,
        "likely_gc_name": None,
        "bid_time_unknown": False,
        "files_status": "complete",
        "files_promoted": 3,
        "files_skipped": [],
        "files_error": None,
        "last_error": None,
        "cleared_at": None,
        "cleared_by": None,
        "restored_at": None,
        "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
    }
    row.update(over)
    return row


def _project(pid, number, name, **over):
    row = {
        "id": pid, "number": number, "name": name, "current_stage": "go_no_go",
        "actual_bid_at": "2026-10-01T21:00:00+00:00", "internal_bid_at": None,
        "due_from_estimator_at": None, "due_from_vendors_at": None, "project_type": None,
        "owner_type": None, "labor_needed": None, "bid_method": None, "competitor_known": None,
        "gc_known": None, "subs_needed": None, "est_value_band": None, "scope_fit": None,
    }
    row.update(over)
    return row


@pytest.fixture
def db(monkeypatch):
    fake = CreatedDB({
        "rfp_created_projects": [],
        "projects": [],
        "project_gcs": [],
        "general_contractors": [{"id": "gc-1", "name": "GC Example Builders"},
                                {"id": "gc-2", "name": "Bob's Construction"}],
        "profiles": [{"id": "u1", "full_name": "Tom Moore"}, {"id": "u9", "full_name": "Eve Exec"}],
        "rfp_emails": [{"id": E1, "subject": "Invitation to Bid: Warehouse HVAC",
                        "received_at": "2026-09-15T18:30:00+00:00", "invitation_method": "organic"}],
        "rfp_portal_invitations": [{"id": I1, "title": "Phone Towers", "first_seen_at": "2026-09-14T13:30:00+00:00",
                                    "portal": "ngem"}],
        "rfp_harvests": [{"id": HV, "files": [{"file_path": "a.pdf", "sandbox_file_id": "f-1", "status": "accepted"}]}],
        "llm_jobs": [],
        "audit_log": [],
    })
    fake.defaults = {
        **FakeDB.defaults,
        "llm_jobs": {"status": lambda: "queued", "attempts": lambda: 0, "created_at": lambda: NOW.isoformat()},
    }
    monkeypatch.setattr(rr, "get_supabase", lambda: fake)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: fake)
    monkeypatch.setattr(
        rr, "get_settings",
        lambda: SimpleNamespace(rfp_email_ingestion_enabled=True, rfp_ngem_active=False,
                                default_rate_limit_per_min=600, rfp_create_files_queue_priority=160,
                                llm_queue_lease_seconds=900),
    )
    queue_settings = SimpleNamespace(rfp_create_files_queue_priority=160, llm_retry_delay_list=[60, 300],
                                     llm_queue_lease_seconds=900)
    monkeypatch.setattr(rfp_create_files, "get_settings", lambda: queue_settings)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: queue_settings)
    return fake


@pytest.fixture
def audits(monkeypatch):
    calls = []
    monkeypatch.setattr(rr, "audit", lambda *a: calls.append(a))
    return calls


def _seed(db):
    db.tables["rfp_created_projects"] = [
        _rec(P1, minutes_ago=30),
        _rec(P2, minutes_ago=20, source_kind="rfp_portal", rfp_email_id=None, portal_invitation_id=I1,
             invitation_method="ngem", sender_display=None, gc_plan="none", files_status="none",
             files_promoted=0, created_by="u1", automatic=False, bid_time_unknown=True),
        _rec(P3, minutes_ago=10, gc_plan="likely", likely_gc_id="gc-2", likely_gc_name="Bob's Construction",
             sender_was_unauthorized=True, sender_allowed_by="u9", invitation_method="nonorganic",
             files_status="failed", files_error="Storage did not answer.", files_promoted=1,
             files_skipped=[{"file_path": "x.pdf", "reason": "hazard:javascript_actions"}],
             last_error="Go/No-Go advance failed: workflow down",
             cleared_at=NOW.isoformat(), cleared_by="u1"),
    ]
    db.tables["projects"] = [
        _project(P1, "26.9.7204", "Warehouse HVAC", internal_bid_at="2026-09-30T00:00:00+00:00",
                 project_type="ti", owner_type="private_commercial", labor_needed="union", bid_method="hard_bid",
                 competitor_known="no_unknown", gc_known="yes_1_2", subs_needed="no", est_value_band="150k_500k",
                 scope_fit="yes", due_from_estimator_at="2026-09-25T00:00:00+00:00",
                 due_from_vendors_at="2026-09-26T00:00:00+00:00"),
        _project(P2, "26.9.7205", "Phone Towers"),
        _project(P3, "26.9.7206", "Clinic TI"),
    ]
    db.tables["project_gcs"] = [{"id": "l1", "project_id": P1, "gc_id": "gc-1"}]


# ── Route table and gates ────────────────────────────────────────────────


def test_route_table_is_exactly_the_spec():
    pairs = {(m, r.path) for r in rr.router.routes for m in r.methods}
    assert pairs == {
        ("GET", "/rfp-created"),
        ("GET", "/rfp-created/counts"),
        ("POST", "/rfp-created/{project_id}/clear"),
        ("POST", "/rfp-created/{project_id}/restore"),
        ("POST", "/rfp-created/{project_id}/retry-files"),
        # The splitter flag's actions (docs/RFP_SPLIT.md 10).
        ("POST", "/rfp-created/{project_id}/split/run"),
        ("POST", "/rfp-created/{project_id}/split/outside"),
        ("DELETE", "/rfp-created/{project_id}/split/outside"),
    }
    paths = [r.path for r in rr.router.routes]
    assert paths.index("/rfp-created/counts") < paths.index("/rfp-created/{project_id}/clear")
    for route in rr.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert rr.require_rfp_created in calls and rr.require_page in calls, route.path
        # The manual splitter run rides the AI budget, like the splitter's own runs.
        budget = rr.ai_rate_limit if route.path.endswith("/split/run") else rr.rfp_created_rate_limit
        assert budget in calls, route.path


def test_flag_gate_is_fail_closed_and_either_slice_opens_it(monkeypatch):
    monkeypatch.setattr(rr, "get_settings", lambda: SimpleNamespace())
    assert rr.rfp_created_enabled() is False
    with pytest.raises(HTTPException) as exc:
        rr.require_rfp_created()
    assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
    monkeypatch.setattr(rr, "get_settings",
                        lambda: SimpleNamespace(rfp_email_ingestion_enabled=False, rfp_ngem_active=False))
    with pytest.raises(HTTPException):
        rr.require_rfp_created()
    monkeypatch.setattr(rr, "get_settings",
                        lambda: SimpleNamespace(rfp_email_ingestion_enabled=False, rfp_ngem_active=True))
    assert rr.require_rfp_created() is None
    monkeypatch.setattr(rr, "get_settings",
                        lambda: SimpleNamespace(rfp_email_ingestion_enabled=True, rfp_ngem_active=False))
    assert rr.require_rfp_created() is None


@pytest.mark.parametrize("role", [Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN])
def test_page_admits_the_three_roles(role):
    user = _user(role)
    assert asyncio.run(rr.require_page(user=user)) is user


@pytest.mark.parametrize(
    "role",
    [Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR, Role.ACCOUNTANT, Role.ESTIMATOR],
)
def test_page_refuses_everyone_else(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_page(user=_user(role)))
    assert exc.value.status_code == 403
    assert set(rr.PAGE_ROLES) == {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}


# ── The list ─────────────────────────────────────────────────────────────


def test_list_shape_order_flags_and_one_query_per_table(db):
    _seed(db)
    out = rr.list_rfp_created(cleared="all", limit=100, before=None, before_id=None, _=_user())
    assert [item["project"]["id"] for item in out] == [P3, P2, P1]   # newest first
    first, portal, resolved = out
    assert resolved == {
        "project": {"id": P1, "number": "26.9.7204", "name": "Warehouse HVAC", "current_stage": "go_no_go",
                    "actual_bid_at": "2026-10-01T21:00:00+00:00", "internal_bid_at": "2026-09-30T00:00:00+00:00",
                    "gcs": [{"id": "gc-1", "name": "GC Example Builders"}]},
        "created": {"created_at": (NOW - timedelta(minutes=30)).isoformat(), "created_by": None, "automatic": True,
                    "source_kind": "rfp_email",
                    "source": {"id": E1, "subject": "Invitation to Bid: Warehouse HVAC",
                               "received_at": "2026-09-15T18:30:00+00:00", "invitation_method": "organic"},
                    "harvest_id": HV, "gc_plan": "resolved"},
        # The BuildingConnected flags (docs/RFP_BUILDINGCONNECTED.md 3.4 and 3.8)
        # are off for every other source: no files flag, no open GC question.
        "flags": {"missing_files": False, "no_gc": False, "likely_gc": None,
                  "files_needed": False, "files_needed_url": None, "confirm_gc": False, "provisional_gc": None,
                  "intake_incomplete": [],
                  "sender_was_unauthorized": False, "sender_display": "GC PM <pm@gc.example>",
                  "sender_allowed_by": None, "documents_skipped": 0, "files_status": "complete",
                  "files_promoted": 3, "files_error": None, "files_failed": False, "last_error": None,
                  # The split step (docs/RFP_SPLIT.md 4): a record created before 0132 carries no status.
                  "split_status": None, "split_job_id": None, "split_files_total": 0, "split_files_done": 0,
                  "split_segments": 0, "split_failed": False,
                  # The splitter flag (docs/RFP_SPLIT.md 10): nothing to say.
                  "split_partial": False, "split_issue": None},
        "cleared_at": None,
        "cleared_by": None,
    }
    assert portal["created"]["source"] == {"id": I1, "title": "Phone Towers",
                                           "first_seen_at": "2026-09-14T13:30:00+00:00",
                                           "invitation_method": "ngem"}
    assert portal["created"]["created_by"] == {"id": "u1", "full_name": "Tom Moore"}
    assert portal["created"]["automatic"] is False and portal["created"]["source_kind"] == "rfp_portal"
    assert portal["flags"]["missing_files"] is True and portal["flags"]["no_gc"] is True
    assert portal["flags"]["intake_incomplete"] == [
        "internal_bid_at", "due_from_estimator_at", "due_from_vendors_at", "project_type", "owner_type",
        "labor_needed", "bid_method", "competitor_known", "gc_known", "subs_needed", "est_value_band",
        "scope_fit", "bid_time",
    ]
    assert portal["project"]["gcs"] == [] and portal["flags"]["sender_display"] is None
    assert first["flags"]["likely_gc"] == {"id": "gc-2", "name": "Bob's Construction"}
    assert first["flags"]["no_gc"] is True and first["flags"]["missing_files"] is False
    assert first["flags"]["sender_was_unauthorized"] is True
    assert first["flags"]["sender_allowed_by"] == {"id": "u9", "full_name": "Eve Exec"}
    assert first["flags"]["documents_skipped"] == 1 and first["flags"]["files_failed"] is True
    assert first["flags"]["files_status"] == "failed" and first["flags"]["files_error"] == "Storage did not answer."
    assert first["flags"]["last_error"] == "Go/No-Go advance failed: workflow down"
    assert first["cleared_at"] == NOW.isoformat() and first["cleared_by"] == {"id": "u1", "full_name": "Tom Moore"}
    # Nothing here carries a score or a body.
    assert "score" not in repr(out) and "body_text" not in repr(out)


def test_list_filters_cleared_and_pages_on_before(db):
    _seed(db)
    active = rr.list_rfp_created(cleared="false", limit=100, before=None, before_id=None, _=_user())
    assert [i["project"]["id"] for i in active] == [P2, P1]
    cleared = rr.list_rfp_created(cleared="true", limit=100, before=None, before_id=None, _=_user())
    assert [i["project"]["id"] for i in cleared] == [P3]
    page = rr.list_rfp_created(cleared="all", limit=1, before=None, before_id=None, _=_user())
    assert [i["project"]["id"] for i in page] == [P3]
    nxt = rr.list_rfp_created(cleared="all", limit=1, before=page[0]["created"]["created_at"], before_id=None, _=_user())
    assert [i["project"]["id"] for i in nxt] == [P2]
    last = rr.list_rfp_created(cleared="all", limit=5, before=nxt[0]["created"]["created_at"], before_id=None, _=_user())
    assert [i["project"]["id"] for i in last] == [P1]
    assert rr.list_rfp_created(cleared="all", limit=5, before="2020-01-01T00:00:00Z", before_id=None, _=_user()) == []
    with pytest.raises(HTTPException) as exc:
        rr.list_rfp_created(cleared="all", limit=5, before="yesterday", before_id=None, _=_user())
    assert exc.value.status_code == 400
    assert rr.list_rfp_created(cleared="false", limit=5, before=None, before_id=None, _=_user(Role.EXECUTIVE)) == active


def test_list_pages_on_the_created_at_project_id_pair(db):
    """Rows created in the same instant (a harvest's mates, a batch) sort
    by project_id desc after created_at desc; paging on the pair walks
    them without a repeat or a gap, where paging on the timestamp alone
    would skip every same-instant row after the first page's last."""
    _seed(db)
    same = (NOW - timedelta(minutes=20)).isoformat()
    for rec in db.tables["rfp_created_projects"]:
        rec["created_at"] = same
    db.tables["rfp_created_projects"].append(_rec("5a000000-0000-4000-8000-000000000000", minutes_ago=40))
    db.tables["projects"].append(_project("5a000000-0000-4000-8000-000000000000", "26.9.7200", "Oldest"))
    ids = []
    before = before_id = None
    for _ in range(10):
        page = rr.list_rfp_created(cleared="all", limit=1, before=before, before_id=before_id, _=_user())
        if not page:
            break
        ids.extend(i["project"]["id"] for i in page)
        before, before_id = page[-1]["created"]["created_at"], page[-1]["project"]["id"]
    assert ids == [P3, P2, P1, "5a000000-0000-4000-8000-000000000000"]
    # The timestamp alone (an older client) still pages, coarser.
    coarse = rr.list_rfp_created(cleared="all", limit=5, before=same, before_id=None, _=_user())
    assert [i["project"]["id"] for i in coarse] == ["5a000000-0000-4000-8000-000000000000"]
    with pytest.raises(HTTPException) as exc:
        rr.list_rfp_created(cleared="all", limit=5, before=same, before_id="not-a-uuid", _=_user())
    assert exc.value.status_code == 400
    # before_id without before is ignored (nothing to pair it with).
    assert len(rr.list_rfp_created(cleared="all", limit=5, before=None, before_id=P1, _=_user())) == 4


def test_list_survives_a_missing_project_or_source_row(db):
    _seed(db)
    db.tables["projects"] = [p for p in db.tables["projects"] if p["id"] != P2]
    db.tables["rfp_emails"] = []
    out = rr.list_rfp_created(cleared="all", limit=100, before=None, before_id=None, _=_user())
    gone = next(i for i in out if i["project"]["id"] == P2)
    assert gone["project"]["number"] is None and gone["flags"]["intake_incomplete"] == []
    email_item = next(i for i in out if i["project"]["id"] == P1)
    assert email_item["created"]["source"] == {"id": E1, "subject": None, "received_at": None,
                                               "invitation_method": "organic"}
    assert rr.list_rfp_created(cleared="all", limit=100, before=None, before_id=None, _=_user()) == out
    db.tables["rfp_created_projects"] = []
    assert rr.list_rfp_created(cleared="all", limit=100, before=None, before_id=None, _=_user()) == []


# ── Clear, restore, retry, counts ────────────────────────────────────────


def test_clear_and_restore_toggle_the_row_and_audit(db, audits):
    _seed(db)
    out = rr.clear_rfp_created(P1, user=_user(uid="u1"))
    row = next(r for r in db.tables["rfp_created_projects"] if r["project_id"] == P1)
    assert row["cleared_at"] and row["cleared_by"] == "u1" and row["restored_at"] is None
    assert out == {"project_id": P1, "cleared_at": row["cleared_at"], "cleared_by": "u1"}
    with pytest.raises(HTTPException) as exc:
        rr.clear_rfp_created(P1, user=_user())
    assert exc.value.status_code == 409
    out = rr.restore_rfp_created(P1, user=_user(uid="u9"))
    row = next(r for r in db.tables["rfp_created_projects"] if r["project_id"] == P1)
    assert row["cleared_at"] is None and row["cleared_by"] is None and row["restored_at"]
    assert out == {"project_id": P1, "cleared_at": None, "restored_at": row["restored_at"]}
    with pytest.raises(HTTPException) as exc:
        rr.restore_rfp_created(P1, user=_user())
    assert exc.value.status_code == 409
    for bad in (MISSING, "not-a-uuid"):
        for call in (rr.clear_rfp_created, rr.restore_rfp_created, rr.retry_rfp_created_files):
            with pytest.raises(HTTPException) as exc:
                call(bad, user=_user())
            assert exc.value.status_code == 404
    assert [(a[0], a[1], a[2], a[3]) for a in audits] == [
        ("u1", "rfp_created.clear", "rfp_created_project", P1),
        ("u9", "rfp_created.restore", "rfp_created_project", P1),
    ]


def test_retry_files_re_enqueues_only_when_there_is_something_to_promote(db, audits):
    _seed(db)
    # Complete: nothing to retry.
    with pytest.raises(HTTPException) as exc:
        rr.retry_rfp_created_files(P1, user=_user())
    assert exc.value.status_code == 409
    # Failed with entries: 202 with the job, the record `pending` BEFORE the
    # job exists (the fence: a crash between the two leaves a retryable
    # pending record, never a job over a record that still says failed).
    real_enqueue = rfp_create_files.enqueue
    status_at_enqueue = []

    def enqueue(project_id, *, created_by, settings=None):
        status_at_enqueue.append(
            next(r for r in db.tables["rfp_created_projects"] if r["project_id"] == project_id)["files_status"]
        )
        return real_enqueue(project_id, created_by=created_by, settings=settings)

    rfp_create_files.enqueue = enqueue
    try:
        out = rr.retry_rfp_created_files(P3, user=_user(uid="u1"))
    finally:
        rfp_create_files.enqueue = real_enqueue
    assert status_at_enqueue == ["pending"]
    (job,) = db.tables["llm_jobs"]
    assert out == {"job": {"id": job["id"], "status": "queued"}}
    assert job["job_type"] == "rfp_create_files" and job["target_id"] == P3 and job["priority"] == 160
    assert job["created_by"] == "u1" and job["payload"] == {"project_id": P3}
    row = next(r for r in db.tables["rfp_created_projects"] if r["project_id"] == P3)
    assert row["files_status"] == "pending" and row["files_error"] is None
    assert audits[-1][1] == "rfp_created.retry_files" and audits[-1][4] == {"from_status": "failed", "job_id": job["id"]}
    # Pending now: refused; a second enqueue would collide anyway.
    with pytest.raises(HTTPException) as exc:
        rr.retry_rfp_created_files(P3, user=_user())
    assert exc.value.status_code == 409
    # A collision on the enqueue puts the prior status back.
    row["files_status"] = "failed"
    with pytest.raises(HTTPException) as exc:
        rr.retry_rfp_created_files(P3, user=_user())
    assert exc.value.status_code == 409 and "already queued" in exc.value.detail
    assert row["files_status"] == "failed"
    # Any other enqueue failure restores it too, and propagates.
    db.tables["llm_jobs"].clear()

    def boom(project_id, *, created_by, settings=None):
        raise RuntimeError("PostgREST 502")

    rfp_create_files.enqueue = boom
    try:
        with pytest.raises(RuntimeError):
            rr.retry_rfp_created_files(P3, user=_user())
    finally:
        rfp_create_files.enqueue = real_enqueue
    assert row["files_status"] == "failed" and db.tables["llm_jobs"] == []
    # A `running` record whose claim is older than the queue lease is a dead
    # worker's: retryable. A fresh claim is not.
    row.update(files_status="running", files_claim_token="dead",
               files_claimed_at=(datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat())
    out = rr.retry_rfp_created_files(P3, user=_user(uid="u1"))
    assert out["job"]["status"] == "queued" and row["files_status"] == "pending"
    assert row["files_claim_token"] is None and row["files_claimed_at"] is None
    assert audits[-1][4]["from_status"] == "running"
    db.tables["llm_jobs"].clear()
    row.update(files_status="running", files_claim_token="live",
               files_claimed_at=datetime.now(timezone.utc).isoformat())
    with pytest.raises(HTTPException) as exc:
        rr.retry_rfp_created_files(P3, user=_user())
    assert exc.value.status_code == 409 and "nothing to retry" in exc.value.detail
    assert row["files_status"] == "running" and row["files_claim_token"] == "live"
    # `none` with a harvest that has no verified entries: refused.
    db.tables["rfp_harvests"][0]["files"] = [{"file_path": "x.pdf", "sandbox_file_id": None, "status": "too_large"}]
    with pytest.raises(HTTPException) as exc:
        rr.retry_rfp_created_files(P2, user=_user())
    assert exc.value.status_code == 409 and "no verified documents" in exc.value.detail


def test_counts_answer_active_and_cleared(db):
    _seed(db)
    assert rr.rfp_created_counts(_user()) == {"active": 2, "cleared": 1}
    db.tables["rfp_created_projects"] = []
    assert rr.rfp_created_counts(_user()) == {"active": 0, "cleared": 0}


# ── Filters: the creation window and the search box (migration 0135) ─────


def _search_rows(db):
    """The `rfp_created_search` view as the fake's table, built the way the
    0135 SQL builds it: lower(concat_ws(' ', number, name, GC names
    (distinct, sorted), subject, title, sender_display, invitation_method))."""
    projects = {p["id"]: p for p in db.tables["projects"]}
    gcs = {g["id"]: g["name"] for g in db.tables["general_contractors"]}
    emails = {e["id"]: e for e in db.tables["rfp_emails"]}
    invitations = {i["id"]: i for i in db.tables["rfp_portal_invitations"]}
    rows = []
    for rec in db.tables["rfp_created_projects"]:
        project = projects.get(rec["project_id"]) or {}
        names = sorted({gcs[link["gc_id"]] for link in db.tables["project_gcs"]
                        if link["project_id"] == rec["project_id"] and link["gc_id"] in gcs})
        parts = [project.get("number"), project.get("name"), " ".join(names) or None,
                 (emails.get(rec.get("rfp_email_id")) or {}).get("subject"),
                 (invitations.get(rec.get("portal_invitation_id")) or {}).get("title"),
                 rec.get("sender_display"), rec.get("invitation_method")]
        rows.append({"project_id": rec["project_id"], "created_at": rec["created_at"],
                     "cleared_at": rec["cleared_at"],
                     "search_text": " ".join(p for p in parts if p is not None).lower()})
    db.tables["rfp_created_search"] = rows


def _ids(out):
    return [i["project"]["id"] for i in out]


def _list(**kw):
    args = {"cleared": "all", "limit": 100, "before": None, "before_id": None, "_": _user()}
    args.update(kw)
    return rr.list_rfp_created(**args)


P4 = "5a000000-0000-4000-8000-000000000004"


def _seed_search(db):
    _seed(db)
    db.tables["rfp_created_projects"].append(
        _rec(P4, minutes_ago=5, rfp_email_id=None, sender_display=None, invitation_method="general"))
    db.tables["projects"].append(_project(P4, "26.9.7207", "100% Design_Set (Phase 1), Bldg A"))
    _search_rows(db)


def test_list_search_matches_each_field_case_insensitively(db):
    _seed_search(db)
    assert _ids(_list(q="7205")) == [P2]                                # number
    assert _ids(_list(q="CLINIC")) == [P3]                              # name
    assert _ids(_list(q="example builders")) == [P1]                    # linked GC name
    assert _ids(_list(q="invitation to bid")) == [P3, P1]               # email subject
    assert _ids(_list(q="Phone Towers")) == [P2]                        # portal title (and name)
    assert _ids(_list(q="pm@gc.example")) == [P3, P1]                   # sender_display
    assert _ids(_list(q="nonorganic")) == [P3]                          # invitation_method
    assert _ids(_list(q="  ngem  ")) == [P2]                            # trimmed
    assert _ids(_list(q="no such project")) == []
    # Blank is no search at all: the table path, every row.
    assert _ids(_list(q="   ")) == _ids(_list()) == [P4, P3, P2, P1]
    # The cleared filter still applies (P3 is cleared).
    assert _ids(_list(q="invitation to bid", cleared="false")) == [P1]
    assert _ids(_list(q="invitation to bid", cleared="true")) == [P3]
    # The rows come back whole, in the view's order.
    out = _list(q="26.9.72")
    assert _ids(out) == [P4, P3, P2, P1]
    assert out[1]["flags"]["likely_gc"] == {"id": "gc-2", "name": "Bob's Construction"}


def test_list_search_treats_wildcards_and_filter_syntax_as_literal_text(db):
    _seed_search(db)
    assert _ids(_list(q="100%")) == [P4]
    assert _ids(_list(q="%")) == [P4]            # only P4 carries a literal %
    assert _ids(_list(q="design_set")) == [P4]
    assert _ids(_list(q="_")) == [P4]            # only P4 carries a literal _
    assert _ids(_list(q="designxset")) == []     # `_` is not a wildcard
    assert _ids(_list(q="(phase 1), bldg")) == [P4]
    assert _ids(_list(q="\\")) == []
    assert rr._search_pattern("a%b_c\\d") == "%a\\%b\\_c\\\\d%"
    assert rr._search_pattern("A*B") == "%a_b%"  # PostgREST reads * as %; the loosest literal is _
    assert rr._search_pattern(" \t\x00 ") is None and rr._search_pattern(None) is None
    assert rr._search_pattern("x" * 150) == "%" + "x" * rr._Q_MAX + "%"


def test_search_pattern_travels_as_one_postgrest_filter_param():
    """Through the real postgrest-py builder the search is ONE query param,
    `search_text=ilike.<pattern>`, with commas and parentheses left as they
    are (they only mean something inside an `or=(...)` group)."""
    from postgrest import SyncPostgrestClient

    client = SyncPostgrestClient("http://localhost:1")
    pattern = rr._search_pattern("100% Design_Set (Phase 1), Bldg A")
    query = rr._scoped(client.from_(rr.SEARCH_VIEW).select("project_id"), "false",
                       "2026-09-01T07:00:00+00:00", "2026-09-02T07:00:00+00:00", pattern)
    params = query.request.params
    assert params.get_list("search_text") == [f"ilike.{pattern}"]
    assert pattern == "%100\\% design\\_set (phase 1), bldg a%"
    assert params.get_list("created_at") == ["gte.2026-09-01T07:00:00+00:00", "lt.2026-09-02T07:00:00+00:00"]
    assert params.get_list("cleared_at") == ["is.null"]
    assert "or" not in params


def test_list_search_pages_on_the_pair(db):
    _seed_search(db)
    ids, before, before_id = [], None, None
    for _ in range(10):
        page = _list(q="gc", limit=1, before=before, before_id=before_id)
        if not page:
            break
        ids.extend(_ids(page))
        before, before_id = page[-1]["created"]["created_at"], page[-1]["project"]["id"]
    # "gc" hits P1 (GC name, sender), P3 (sender), not P2 or P4.
    assert ids == [P3, P1]


def test_list_created_window_is_inclusive_from_exclusive_to(db):
    _seed(db)
    at = {P1: NOW - timedelta(minutes=30), P2: NOW - timedelta(minutes=20), P3: NOW - timedelta(minutes=10)}
    iso = lambda d: d.isoformat().replace("+00:00", "Z")   # noqa: E731 - the FE sends Z
    assert _ids(_list(created_from=iso(NOW - timedelta(minutes=25)))) == [P3, P2]
    assert _ids(_list(created_to=iso(NOW - timedelta(minutes=15)))) == [P2, P1]
    assert _ids(_list(created_from=iso(at[P2]), created_to=iso(at[P3]))) == [P2]   # [from, to)
    assert _ids(_list(created_from=iso(NOW), created_to=iso(NOW + timedelta(days=1)))) == []
    # With the cleared filter and paging.
    assert _ids(_list(created_from=iso(at[P1]), cleared="false")) == [P2, P1]
    page = _list(created_from=iso(at[P1]), limit=1)
    nxt = _list(created_from=iso(at[P1]), limit=5, before=page[0]["created"]["created_at"],
                before_id=page[0]["project"]["id"])
    assert _ids(page) == [P3] and _ids(nxt) == [P2, P1]
    # A naive timestamp reads as UTC; an offset is honoured.
    assert _ids(_list(created_from=(NOW - timedelta(minutes=25)).replace(tzinfo=None).isoformat())) == [P3, P2]
    assert _ids(_list(created_to="2026-09-16T09:45:00-07:00")) == [P2, P1]   # 16:45Z


def test_list_window_and_search_combine(db):
    _seed_search(db)
    assert _ids(_list(q="invitation to bid",
                      created_from=(NOW - timedelta(minutes=15)).isoformat())) == [P3]
    assert _ids(_list(q="26.9", created_to=(NOW - timedelta(minutes=15)).isoformat())) == [P2, P1]


@pytest.mark.parametrize(
    "kw, detail",
    [
        ({"created_from": "yesterday"}, "created_from must be an ISO timestamp"),
        ({"created_to": "2026-13-01"}, "created_to must be an ISO timestamp"),
        ({"created_from": "2026-09-16T00:00:00Z", "created_to": "2026-09-16T00:00:00Z"},
         "created_from must be before created_to"),
        ({"created_from": "2026-09-17T00:00:00Z", "created_to": "2026-09-16T00:00:00Z"},
         "created_from must be before created_to"),
    ],
)
def test_bad_window_is_400_on_list_and_counts(db, kw, detail):
    _seed(db)
    for call in (lambda: _list(**kw), lambda: rr.rfp_created_counts(_user(), **kw)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 400 and exc.value.detail == detail


def test_counts_follow_the_filter(db):
    _seed_search(db)
    assert rr.rfp_created_counts(_user(), q="invitation to bid") == {"active": 1, "cleared": 1}
    assert rr.rfp_created_counts(_user(), q="clinic") == {"active": 0, "cleared": 1}
    assert rr.rfp_created_counts(_user(), q="no such project") == {"active": 0, "cleared": 0}
    assert rr.rfp_created_counts(_user(), created_from=(NOW - timedelta(minutes=15)).isoformat()) == {
        "active": 1, "cleared": 1}   # P4 active, P3 cleared
    assert rr.rfp_created_counts(_user(), created_to=(NOW - timedelta(minutes=15)).isoformat()) == {
        "active": 2, "cleared": 0}
    assert rr.rfp_created_counts(_user(), q="26.9", created_from=(NOW - timedelta(minutes=25)).isoformat(),
                                 created_to=(NOW - timedelta(minutes=8)).isoformat()) == {"active": 1, "cleared": 1}
    # Blank filters are no filters.
    assert rr.rfp_created_counts(_user(), created_from="", created_to=" ", q="  ") == {"active": 3, "cleared": 1}


def test_counts_without_params_read_the_table_as_before(db):
    """The sidebar badge: no params, so the same two HEAD reads off
    rfp_created_projects, never the view."""
    _seed_search(db)
    seen = []
    real = db.table

    def spy(name):
        seen.append(name)
        return real(name)

    db.table = spy
    assert rr.rfp_created_counts(_user()) == {"active": 3, "cleared": 1}
    assert seen == ["rfp_created_projects", "rfp_created_projects"]
    seen.clear()
    _list(created_from=NOW.isoformat())
    assert rr.SEARCH_VIEW not in seen
    seen.clear()
    _list(q="clinic")
    assert seen[:2] == [rr.SEARCH_VIEW, "rfp_created_projects"]


def test_migration_0135_builds_the_search_view():
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
    (path,) = list(migrations.glob("0135_*.sql"))
    sql = path.read_text()
    assert "create or replace view public.rfp_created_search" in sql
    assert "with (security_invoker = true)" in sql
    assert "revoke all on public.rfp_created_search from anon, authenticated" in sql
    for part in ("p.number", "p.name", "string_agg(distinct gc.name", "e.subject", "i.title",
                 "r.sender_display", "r.invitation_method", "r.project_id", "r.created_at", "r.cleared_at"):
        assert part in sql, part
    assert re.search(r"lower\(concat_ws\(", sql) and "as search_text" in sql
    assert "notify pgrst, 'reload schema'" in sql
    assert not re.search(r"\b(update|delete from|insert into|drop table)\b", sql, re.I)
