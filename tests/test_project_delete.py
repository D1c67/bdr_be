"""Delete a project, recoverably (docs/PROJECT_DELETE.md): the service
(app/services/project_delete), the two routes on /projects and the
/deleted-projects router, called as plain functions against the in-memory
fake Supabase from tests/test_rfp_email_ingest. The database functions of
migration 0141 are stood in by a small rpc double here (their real behavior
is exercised live on the dev database, see the doc's build record).

Pinned:

- the route tables, the Bidding gate on every route, the role gates
  (delete: Estimating Admin, Executive, IT Admin; restore and the page: IT
  Admin only) and the mount in app/main.py;
- the pure rules: the confirmation text (name, trimmed; the number when
  there is no name), the PM / CP block, the reasons offered, the count
  buckets, the request checks (reason, `other` needs a note, the note cap,
  a confirmation typed at all, the RFP reason only on an RFP project);
- delete: 404, the PM / CP 409 without calling the database, every
  `project_delete:<code>` the function raises mapped to its status and
  sentence (name mismatch 422 included), the rpc arguments, the audit row;
- delete-info: the counts, the reasons, a blocked project, a RESTRICT
  referrer turned into a block;
- the list (filters, paging on the pair), the summary, the restore (the
  failure kept on the record and audited, the success audited);
- the RFP block at the create steps (email and portal) and the
  availability of the Create project button; the create-time tombstone
  check (name + date, or name + the same GC; excluded and out-of-window
  archives ignored);
- the PGRST116 handler (a `.single()` read of a deleted project is a 404).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError

from app.core.config import Settings
from app.core.deps import CurrentUser
from app.core.roles import PROJECT_DELETE_ROLES, PROJECT_RESTORE_ROLES, Role
from app.models.schemas import ProjectDeleteIn
from app.routers import deleted_projects as dr
from app.routers import projects as pr
from app.services import llm_queue
from app.services import project_delete as pd
from app.services import rfp_create
from tests.test_rfp_email_ingest import FakeDB, _Rpc

P1 = "5a000000-0000-4000-8000-000000000001"
P2 = "5a000000-0000-4000-8000-000000000002"
A1 = "7b000000-0000-4000-8000-000000000001"
A2 = "7b000000-0000-4000-8000-000000000002"
A3 = "7b000000-0000-4000-8000-000000000003"
NOW = datetime.now(timezone.utc)


def _api_error(code: str, details: str | None = None) -> APIError:
    return APIError({"message": f"project_delete:{code}", "code": "P0001", "details": details,
                     "hint": None})


class DeleteDB(FakeDB):
    """The ingest fake plus the three 0141 functions: the preview answers
    what `preview` holds, the delete and the restore answer `result` or
    raise `raise_` (an APIError shaped like PostgREST's)."""

    preview: dict = {}
    result: dict | None = None
    raise_: Exception | None = None

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, dict(params or {})))
        if name in ("project_delete_preview", "archive_and_delete_project", "restore_deleted_project"):
            db = self

            def execute():
                if db.raise_ is not None:
                    raise db.raise_
                data = db.preview if name == "project_delete_preview" else db.result
                return SimpleNamespace(data=data)

            return SimpleNamespace(execute=execute)
        return _Rpc(self, name, params)


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _project(pid=P1, **over):
    row = {"id": pid, "number": "26.9.7120", "name": "Cimarron Scoreboard", "pm_stage": None,
           "cp_enrolled_at": None}
    row.update(over)
    return row


def _archive(aid, *, minutes_ago=0, **over):
    row = {
        "id": aid, "project_id": P1, "project_number": "26.9.7120", "project_name": "Cimarron Scoreboard",
        "project_stage": "go_no_go", "gc_names": ["Acme GC"], "gc_ids": ["gc-1"], "gc_links": [],
        "reason": pd.REASON_RFP, "note": None, "from_rfp": True, "rfp_source_kind": "rfp_email",
        "rfp_source_id": "e-1", "rfp_source_label": "Invitation to Bid: Cimarron", "deleted_by": "u1",
        "deleted_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "row_counts": {"projects": 1, "rfqs": 2, "project_files": 5, "stage_events": 3},
        "row_total": 11, "restored_at": None, "restored_by": None, "restore_error": None,
        "test_session_id": None, "project_bid_at": (NOW + timedelta(days=5)).isoformat(),
        "project_row": {"name": "Cimarron Scoreboard", "number": "26.9.7120",
                        "actual_bid_at": "2026-10-01T21:00:00+00:00", "internal_bid_at": None,
                        "bid_notes": None},
    }
    row.update(over)
    return row


@pytest.fixture
def db(monkeypatch):
    fake = DeleteDB({
        "projects": [_project()],
        "rfp_created_projects": [{"project_id": P1}],
        "deleted_projects": [],
        "profiles": [{"id": "u1", "full_name": "Tom Moore"}, {"id": "u9", "full_name": "Ivy Admin"}],
        "audit_log": [],
        "llm_jobs": [],
    })
    fake.preview = {"counts": {"projects": 1, "rfqs": 3, "rfq_sends": 4, "quotes": 5,
                               "project_files": 12, "project_gcs": 2, "stage_events": 30},
                    "total": 57, "links": 3, "blockers": []}
    fake.result = None
    fake.raise_ = None
    monkeypatch.setattr(pr, "get_supabase", lambda: fake)
    monkeypatch.setattr(dr, "get_supabase", lambda: fake)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def audits(monkeypatch):
    calls = []
    monkeypatch.setattr(pd, "audit", lambda *a, **k: calls.append(a))
    return calls


# ── Route tables and gates ───────────────────────────────────────────────────


def _deps(route) -> set:
    return {d.call for d in route.dependant.dependencies}


def test_the_two_project_routes_are_bidding_only_and_role_gated():
    routes = {(m, r.path): r for r in pr.router.routes for m in r.methods}
    info = routes[("GET", "/projects/{project_id}/delete-info")]
    delete = routes[("POST", "/projects/{project_id}/delete")]
    bidding = pr._BIDDING_ONLY[0].dependency
    for route in (info, delete):
        calls = _deps(route)
        assert bidding in calls and pr.require_project_delete in calls
        assert pr.project_delete_rate_limit in calls
    # The creator-only discard route is untouched.
    assert ("DELETE", "/projects/{project_id}") in routes


def test_the_deleted_projects_router_is_it_admin_only_and_mounted_under_bidding():
    pairs = {(m, r.path) for r in dr.router.routes for m in r.methods}
    assert pairs == {
        ("GET", "/deleted-projects"),
        ("GET", "/deleted-projects/summary"),
        ("POST", "/deleted-projects/{archive_id}/restore"),
    }
    paths = [r.path for r in dr.router.routes]
    assert paths.index("/deleted-projects/summary") < paths.index("/deleted-projects/{archive_id}/restore")
    for route in dr.router.routes:
        assert dr.require_restore in _deps(route) and dr.deleted_projects_rate_limit in _deps(route)
    import app.main as main

    assert any(getattr(r, "path", "") == "/deleted-projects/summary" for r in main.app.routes)
    with open(main.__file__) as fh:
        assert "app.include_router(deleted_projects.router, dependencies=_BIDDING)" in fh.read()


@pytest.mark.parametrize("role", list(Role))
def test_role_gates(role):
    user = _user(role)
    allowed_delete = role in PROJECT_DELETE_ROLES
    allowed_restore = role in PROJECT_RESTORE_ROLES
    assert set(PROJECT_DELETE_ROLES) == {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}
    assert set(PROJECT_RESTORE_ROLES) == {Role.IT_ADMIN}
    for dep, allowed in ((pr.require_project_delete, allowed_delete), (dr.require_restore, allowed_restore)):
        if allowed:
            assert asyncio.run(dep(user)) is user
        else:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(dep(user))
            assert exc.value.status_code == 403


# ── Pure rules ───────────────────────────────────────────────────────────────


def test_confirm_text_is_the_trimmed_name_or_the_number():
    assert pd.confirm_text({"name": "  Warehouse HVAC ", "number": "26.9.1"}) == ("Warehouse HVAC", False)
    assert pd.confirm_text({"name": "   ", "number": " 26.9.1 "}) == ("26.9.1", True)
    assert pd.confirm_text({"name": None, "number": "26.9.1B"}) == ("26.9.1B", True)


def test_block_for_pm_and_cp():
    assert pd.block_for(_project()) is None
    assert pd.block_for(_project(pm_stage="precon"))["code"] == pd.BLOCK_PM
    assert pd.block_for(_project(cp_enrolled_at="2026-01-01T00:00:00Z"))["code"] == pd.BLOCK_CP


def test_reasons_offer_the_rfp_reason_only_for_an_rfp_project():
    assert pd.reasons_for(True) == ["rfp_should_not_exist", "duplicate", "created_by_mistake", "other"]
    assert pd.reasons_for(False) == ["duplicate", "created_by_mistake", "other"]


def test_summarize_counts_buckets_and_excludes_the_project_row_from_other():
    counts, total = pd.summarize_counts(
        {"projects": 1, "rfqs": 3, "rfq_sends": 4, "quotes": 5, "project_files": 12,
         "project_gcs": 2, "stage_events": 30})
    assert total == 57
    assert counts == {"rfqs": 3, "vendor_emails": 4, "quotes": 5, "files": 12, "gcs": 2, "other": 30}
    assert pd.summarize_counts(None) == ({"rfqs": 0, "vendor_emails": 0, "quotes": 0, "files": 0,
                                          "gcs": 0, "other": 0}, 0)


@pytest.mark.parametrize(
    "reason, note, confirm, from_rfp, code",
    [
        (None, None, "x", None, "bad_reason"),
        ("bogus", None, "x", None, "bad_reason"),
        ("other", "   ", "x", None, "note_required"),
        ("duplicate", "n" * 2001, "x", None, "note_too_long"),
        ("duplicate", None, "   ", None, "name_mismatch"),
        ("rfp_should_not_exist", None, "x", False, "not_from_rfp"),
    ],
)
def test_validate_request_refusals_are_422_sentences(reason, note, confirm, from_rfp, code):
    with pytest.raises(pd.ProjectDeleteError) as exc:
        pd.validate_request(reason, note, confirm, from_rfp=from_rfp)
    assert exc.value.code == code and exc.value.status_code == 422 and str(exc.value)


def test_validate_request_cleans_the_note():
    assert pd.validate_request("other", "  because  ", "x") == "because"
    assert pd.validate_request("duplicate", "   ", "x") is None


def test_error_code_of_reads_the_database_marker():
    assert pd.error_code_of(_api_error("number_taken", "26.9.7120 Other")) == "number_taken"
    assert pd.error_code_of(RuntimeError("boom")) is None


# ── Delete ───────────────────────────────────────────────────────────────────


def test_delete_route_calls_the_function_with_the_typed_text_and_audits(db, audits):
    db.result = {"archive_id": A1, "project_id": P1, "number": "26.9.7120", "name": "Cimarron Scoreboard",
                 "from_rfp": True, "reason": "rfp_should_not_exist", "deleted_at": NOW.isoformat(),
                 "row_total": 57}
    body = ProjectDeleteIn(reason="rfp_should_not_exist", note="  junk invite ",
                           confirm_name=" Cimarron Scoreboard ")
    out = pr.archive_delete_project(P1, body, _user(Role.EXECUTIVE, "u9"))
    assert out == {"archive_id": A1, "project_id": P1, "number": "26.9.7120",
                   "name": "Cimarron Scoreboard", "from_rfp": True, "reason": "rfp_should_not_exist",
                   "deleted_at": NOW.isoformat()}
    ((name, params),) = [c for c in db.rpc_calls if c[0] == "archive_and_delete_project"]
    assert params == {"p_project_id": P1, "p_actor": "u9", "p_reason": "rfp_should_not_exist",
                      "p_note": "junk invite", "p_confirm_name": " Cimarron Scoreboard "}
    ((actor, action, entity, entity_id, payload),) = audits
    assert (actor, action, entity, entity_id) == ("u9", "project.delete", "project", P1)
    assert payload["archive_id"] == A1 and payload["reason"] == "rfp_should_not_exist"
    assert payload["note"] == "junk invite"


@pytest.mark.parametrize(
    "code, status",
    [("name_mismatch", 422), ("not_from_rfp", 422), ("note_required", 422), ("pm_enrolled", 409),
     ("cp_enrolled", 409), ("referenced", 409), ("not_found", 404)],
)
def test_delete_maps_every_database_refusal(db, audits, code, status):
    db.raise_ = _api_error(code, "quotes (2)")
    body = ProjectDeleteIn(reason="duplicate", note=None, confirm_name="Cimarron Scoreboard")
    with pytest.raises(HTTPException) as exc:
        pr.archive_delete_project(P1, body, _user())
    assert exc.value.status_code == status
    assert exc.value.headers == {"X-Error-Code": f"project_delete_{code}"}
    assert "project_delete:" not in exc.value.detail
    assert audits == []


def test_delete_refuses_pm_and_cp_before_calling_the_database(db, audits):
    db.tables["projects"] = [_project(pm_stage="precon")]
    body = ProjectDeleteIn(reason="duplicate", confirm_name="Cimarron Scoreboard")
    with pytest.raises(HTTPException) as exc:
        pr.archive_delete_project(P1, body, _user())
    assert exc.value.status_code == 409 and "Project Management" in exc.value.detail
    db.tables["projects"] = [_project(cp_enrolled_at="2026-01-01T00:00:00Z")]
    with pytest.raises(HTTPException) as exc:
        pr.archive_delete_project(P1, body, _user())
    assert exc.value.status_code == 409 and "Certified Payroll" in exc.value.detail
    assert [c for c in db.rpc_calls if c[0] == "archive_and_delete_project"] == []


def test_delete_request_checks_run_before_the_database(db, audits):
    for body, code in (
        (ProjectDeleteIn(reason="other", note=" ", confirm_name="Cimarron Scoreboard"), 422),
        (ProjectDeleteIn(reason="nope", confirm_name="Cimarron Scoreboard"), 422),
        (ProjectDeleteIn(reason="duplicate", confirm_name=""), 422),
    ):
        with pytest.raises(HTTPException) as exc:
            pr.archive_delete_project(P1, body, _user())
        assert exc.value.status_code == code
    assert [c for c in db.rpc_calls if c[0] == "archive_and_delete_project"] == []


def test_delete_unknown_and_malformed_ids_are_404(db, audits):
    body = ProjectDeleteIn(reason="duplicate", confirm_name="x")
    for pid in ("not-a-uuid", P2):
        with pytest.raises(HTTPException) as exc:
            pr.archive_delete_project(pid, body, _user())
        assert exc.value.status_code == 404


# ── Delete info ──────────────────────────────────────────────────────────────


def test_delete_info_for_an_rfp_project(db):
    info = pr.project_delete_info(P1, _user())
    assert info == {
        "project_id": P1, "number": "26.9.7120", "name": "Cimarron Scoreboard",
        "confirm_text": "Cimarron Scoreboard", "confirm_uses_number": False, "from_rfp": True,
        "blocked": None,
        "counts": {"rfqs": 3, "vendor_emails": 4, "quotes": 5, "files": 12, "gcs": 2, "other": 30},
        "total_rows": 57, "reasons": list(pd.REASONS),
    }


def test_delete_info_for_a_nameless_manual_project_and_a_blocked_one(db):
    db.tables["projects"] = [_project(name="  "), _project(P2, pm_stage="precon")]
    db.tables["rfp_created_projects"] = []
    info = pr.project_delete_info(P1, _user())
    assert info["confirm_text"] == "26.9.7120" and info["confirm_uses_number"] is True
    assert info["from_rfp"] is False and "rfp_should_not_exist" not in info["reasons"]
    calls = len(db.rpc_calls)
    blocked = pr.project_delete_info(P2, _user())
    assert blocked["blocked"]["code"] == pd.BLOCK_PM and blocked["total_rows"] == 0
    assert len(db.rpc_calls) == calls   # no preview for a blocked project


def test_delete_info_turns_a_restrict_referrer_into_a_block(db):
    db.preview = {**db.preview, "blockers": [{"table": "pay_app_lines", "constraint": "x", "rows": 2}]}
    info = pr.project_delete_info(P1, _user())
    assert info["blocked"]["code"] == "referenced" and "pay_app_lines (2)" in info["blocked"]["message"]


def test_delete_info_404(db):
    with pytest.raises(HTTPException) as exc:
        pr.project_delete_info(P2, _user())
    assert exc.value.status_code == 404


# ── The Deleted Projects page ────────────────────────────────────────────────


def _seed_archives(db):
    db.tables["deleted_projects"] = [
        _archive(A1, minutes_ago=30),
        _archive(A2, minutes_ago=20, reason="duplicate", from_rfp=False, rfp_source_id=None,
                 restored_at=NOW.isoformat(), restored_by="u9"),
        _archive(A3, minutes_ago=10, reason="other", note="made twice"),
    ]


def test_list_defaults_to_not_restored_newest_first_with_people_and_counts(db):
    _seed_archives(db)
    rows = dr.list_deleted_projects(limit=50, before=None, before_id=None, reason=None,
                                    restored="false", _=_user(Role.IT_ADMIN))
    assert [r["id"] for r in rows] == [A3, A1]
    first = rows[1]
    assert first["deleted_by"] == {"id": "u1", "full_name": "Tom Moore"}
    assert first["counts"] == {"rfqs": 2, "vendor_emails": 0, "quotes": 0, "files": 5, "gcs": 0, "other": 3}
    assert first["rfp_source"] == {"kind": "rfp_email", "id": "e-1", "label": "Invitation to Bid: Cimarron"}
    assert first["gc_names"] == ["Acme GC"] and first["row_total"] == 11
    restored = dr.list_deleted_projects(limit=50, before=None, before_id=None, reason=None,
                                        restored="true", _=_user(Role.IT_ADMIN))
    assert [r["id"] for r in restored] == [A2]
    assert restored[0]["restored_by"] == {"id": "u9", "full_name": "Ivy Admin"}
    assert restored[0]["rfp_source"] is None


def test_list_filters_by_reason_pages_on_the_pair_and_refuses_junk(db):
    _seed_archives(db)
    everything = dr.list_deleted_projects(limit=50, before=None, before_id=None, reason=None,
                                          restored="all", _=_user(Role.IT_ADMIN))
    assert [r["id"] for r in everything] == [A3, A2, A1]
    only = dr.list_deleted_projects(limit=50, before=None, before_id=None, reason="duplicate",
                                    restored="all", _=_user(Role.IT_ADMIN))
    assert [r["id"] for r in only] == [A2]
    page2 = dr.list_deleted_projects(limit=50, before=everything[0]["deleted_at"], before_id=A3,
                                     reason=None, restored="all", _=_user(Role.IT_ADMIN))
    assert [r["id"] for r in page2] == [A2, A1]
    with pytest.raises(HTTPException) as exc:
        dr.list_deleted_projects(limit=50, before=None, before_id=None, reason="nope", restored="all",
                                 _=_user(Role.IT_ADMIN))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        dr.list_deleted_projects(limit=50, before="yesterday", before_id=None, reason=None,
                                 restored="all", _=_user(Role.IT_ADMIN))


def test_summary_counts_by_reason(db):
    _seed_archives(db)
    out = dr.deleted_projects_summary(_user(Role.IT_ADMIN))
    assert out == {"total": 3, "not_restored": 2, "restored": 1,
                   "by_reason": {"rfp_should_not_exist": 1, "duplicate": 1, "created_by_mistake": 0,
                                 "other": 1}}


def test_restore_success_is_audited(db, audits):
    _seed_archives(db)
    db.result = {"archive_id": A1, "project_id": P1, "number": "26.9.7120", "name": "Cimarron Scoreboard",
                 "restored_at": NOW.isoformat(), "rows_restored": 11, "links_relinked": 3,
                 "links_skipped": 0, "references_cleared": []}
    out = dr.restore_deleted_project(A1, _user(Role.IT_ADMIN, "u9"))
    assert out["project_id"] == P1 and out["rows_restored"] == 11 and out["links_relinked"] == 3
    assert out["restored_by"] == {"id": "u9", "full_name": "Ivy Admin"}
    assert db.rpc_calls[-1] == ("restore_deleted_project", {"p_archive_id": A1, "p_actor": "u9"})
    ((actor, action, entity, entity_id, payload),) = audits
    assert (actor, action, entity, entity_id) == ("u9", "project.restore", "project", P1)
    assert payload["archive_id"] == A1 and payload["rows_restored"] == 11


def test_restore_fails_ai_jobs_the_snapshot_caught_in_flight(db, audits, monkeypatch):
    _seed_archives(db)
    db.result = {"archive_id": A1, "project_id": P1, "number": "26.9.7120", "name": "Cimarron Scoreboard",
                 "restored_at": NOW.isoformat(), "rows_restored": 11, "links_relinked": 0,
                 "links_skipped": 0, "references_cleared": []}
    db.tables["llm_jobs"] = [
        {"id": "j1", "project_id": P1, "job_type": "boq_extraction", "target_id": "a1", "status": "queued"},
        {"id": "j2", "project_id": P1, "job_type": "boq_extraction", "target_id": "a2", "status": "running",
         "claimed_by": "w-gone", "lease_expires_at": NOW.isoformat()},
        {"id": "j3", "project_id": P1, "job_type": "boq_extraction", "target_id": "a3", "status": "succeeded"},
        {"id": "j4", "project_id": P2, "job_type": "boq_extraction", "target_id": "a4", "status": "queued"},
    ]
    marks = []
    monkeypatch.setattr(llm_queue, "_spec", lambda job_type: SimpleNamespace(
        mark=lambda target, fields: marks.append((target, fields["status"]))))
    dr.restore_deleted_project(A1, _user(Role.IT_ADMIN, "u9"))
    jobs = {j["id"]: j for j in db.tables["llm_jobs"]}
    assert [jobs[i]["status"] for i in ("j1", "j2", "j3", "j4")] == ["failed", "failed", "succeeded", "queued"]
    assert jobs["j2"]["claimed_by"] is None and jobs["j2"]["lease_expires_at"] is None
    assert jobs["j1"]["last_error"].startswith("The project was deleted while this AI job was in flight")
    assert sorted(marks) == [("a1", "failed"), ("a2", "failed")]


def test_restore_failure_is_kept_on_the_record_and_audited(db, audits):
    _seed_archives(db)
    db.raise_ = _api_error("number_taken", "26.9.7120 Someone Else's Job")
    with pytest.raises(HTTPException) as exc:
        dr.restore_deleted_project(A1, _user(Role.IT_ADMIN))
    assert exc.value.status_code == 409 and "26.9.7120 Someone Else's Job" in exc.value.detail
    rec = next(r for r in db.tables["deleted_projects"] if r["id"] == A1)
    assert rec["restore_error"] == exc.value.detail
    assert [a[1] for a in audits] == ["project.restore_failed"]


def test_restore_already_restored_and_unknown(db, audits):
    db.raise_ = _api_error("already_restored")
    with pytest.raises(HTTPException) as exc:
        dr.restore_deleted_project(A2, _user(Role.IT_ADMIN))
    assert exc.value.status_code == 409 and audits == []
    db.raise_ = _api_error("archive_not_found")
    with pytest.raises(HTTPException) as exc:
        dr.restore_deleted_project(A2, _user(Role.IT_ADMIN))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        dr.restore_deleted_project("junk", _user(Role.IT_ADMIN))
    assert exc.value.status_code == 404


# ── The RFP screens ──────────────────────────────────────────────────────────


def test_archive_ref_and_the_blocked_sentence(db):
    _seed_archives(db)
    ref = pd.archive_ref(db, A3)
    assert ref["number"] == "26.9.7120" and ref["reason"] == "other" and ref["note"] == "made twice"
    assert ref["deleted_by"] == {"id": "u1", "full_name": "Tom Moore"} and ref["restored_at"] is None
    assert pd.archive_ref(db, None) is None and pd.archive_ref(db, "missing") is None
    assert "26.9.7120 Cimarron Scoreboard" in pd.blocked_sentence(ref)
    assert "Deleted Projects" in pd.blocked_sentence(None)


def test_create_available_is_off_for_a_blocked_source():
    email = {"status": "done", "extracted_project_name": "Warehouse HVAC", "flag_reason": None,
             "created_project_id": None}
    assert rfp_create.email_create_available(email)
    assert not rfp_create.email_create_available({**email, "create_blocked_archive_id": A1})
    ngem = {"status": "done", "title": "Phone Towers", "created_project_id": None, "portal": "ngem"}
    assert rfp_create.portal_create_available(ngem)
    assert not rfp_create.portal_create_available({**ngem, "create_blocked_archive_id": A1})


# ── The tombstone check ──────────────────────────────────────────────────────


def _settings(**over):
    base = dict(rfp_ingest_enabled=True, llm_queue_enabled=True, rfp_create_auto_enabled=True)
    base.update(over)
    return Settings(_env_file=None, **base)


def _email_row(**over):
    row = {"id": "e-9", "extracted_project_name": "Cimarron Scoreboard",
           "extracted_bid_due_at": "2026-10-01T21:00:00+00:00", "extracted_bid_due_has_time": True,
           "resolved_gc_id": None, "excluded_project_ids": [], "test_session_id": None}
    row.update(over)
    return row


def test_tombstone_matches_a_deleted_project_by_name_and_exact_date(db):
    db.tables["deleted_projects"] = [_archive(A1)]
    assert pd.tombstone_for_email(db, _email_row(), _settings()) == A1
    # A different job, an excluded project, a restored archive, another bench scope: no hit.
    assert pd.tombstone_for_email(db, _email_row(extracted_project_name="Durango Library"), _settings()) is None
    assert pd.tombstone_for_email(db, _email_row(excluded_project_ids=[P1]), _settings()) is None
    assert pd.tombstone_for_email(db, _email_row(test_session_id="ts-1"), _settings()) is None
    db.tables["deleted_projects"] = [_archive(A1, restored_at=NOW.isoformat())]
    assert pd.tombstone_for_email(db, _email_row(), _settings()) is None


def test_tombstone_without_a_date_needs_the_same_gc(db):
    db.tables["deleted_projects"] = [_archive(A1)]
    no_date = _email_row(extracted_bid_due_at=None, extracted_bid_due_has_time=None)
    assert pd.tombstone_for_email(db, no_date, _settings()) is None
    assert pd.tombstone_for_email(db, {**no_date, "resolved_gc_id": "gc-1"}, _settings()) == A1
    assert pd.tombstone_for_email(db, {**no_date, "resolved_gc_id": "gc-2"}, _settings()) is None


def test_tombstone_ignores_archives_outside_the_candidate_window(db):
    old = (NOW - timedelta(days=400)).isoformat()
    db.tables["deleted_projects"] = [_archive(A1, project_bid_at=old)]
    assert pd.tombstone_for_email(db, _email_row(), _settings()) is None


def test_tombstone_never_raises_on_a_failed_read(db, monkeypatch):
    def boom(name):
        raise RuntimeError("down")

    monkeypatch.setattr(db, "table", boom)
    assert pd.tombstone_for_email(db, _email_row(), _settings()) is None


# ── The pipeline's create steps ──────────────────────────────────────────────


def test_email_create_step_parks_a_blocked_row_whatever_the_switch(db, monkeypatch):
    from app.services import rfp_email_ingest as ingest

    calls = []
    monkeypatch.setattr(ingest.rfp_create, "create_from_email",
                        lambda *a, **k: calls.append(a) or None)
    for auto in (False, True):
        monkeypatch.setattr(ingest, "get_settings", lambda a=auto: _settings(rfp_create_auto_enabled=a))
        db.tables["rfp_harvests"] = [{"id": "hv-1", "project_id": None, "create_blocked_archive_id": A1}]
        db.tables["rfp_emails"] = [{"id": "e-1", "status": "create", "flag_reason": "no_candidate",
                                    "harvest_id": "hv-1", "extracted_project_name": "Cimarron",
                                    "create_blocked_archive_id": None, "test_session_id": None}]
        assert ingest._step_create(db, dict(db.tables["rfp_emails"][0])) is None
        row = db.tables["rfp_emails"][0]
        assert row["status"] == "done" and row["flag_reason"] == "project_deleted"
        assert row["create_blocked_archive_id"] == A1 and row["decided_at_step"] == "create"
    assert calls == []


def test_email_create_step_maps_a_block_the_service_found(db, monkeypatch):
    from app.services import rfp_email_ingest as ingest

    def blocked(*a, **k):
        raise rfp_create.CreateBlocked("deleted", A2)

    monkeypatch.setattr(ingest.rfp_create, "create_from_email", blocked)
    monkeypatch.setattr(ingest.rfp_create, "create_block_for", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_create_auto_enabled=True))
    db.tables["rfp_emails"] = [{"id": "e-1", "status": "create", "flag_reason": None, "harvest_id": None,
                                "extracted_project_name": "Cimarron Scoreboard", "test_session_id": None}]
    ingest._step_create(db, dict(db.tables["rfp_emails"][0]))
    row = db.tables["rfp_emails"][0]
    assert row["status"] == "done" and row["flag_reason"] == "project_deleted"
    assert row["create_blocked_archive_id"] == A2 and row["last_error"] == "deleted"


def test_portal_create_step_parks_a_blocked_invitation(db, monkeypatch):
    from app.services import rfp_portal_ingest as portal

    calls = []
    monkeypatch.setattr(portal.rfp_create, "create_from_portal", lambda *a, **k: calls.append(a))
    db.tables["rfp_portal_invitations"] = [{"id": "inv-1", "portal": "ngem", "status": "create",
                                            "title": "Phone Towers", "harvest_id": None,
                                            "create_blocked_archive_id": A1, "sibling_of": None}]
    portal._step_create(db, dict(db.tables["rfp_portal_invitations"][0]), _settings())
    row = db.tables["rfp_portal_invitations"][0]
    assert row["status"] == "done" and row["flag_reason"] == "project_deleted" and calls == []


def test_create_block_for_reads_row_harvest_copies_and_package_siblings(db):
    s = _settings(rfp_match_sibling_window_minutes=10)
    db.tables["rfp_harvests"] = [{"id": "hv-1", "create_blocked_archive_id": A1},
                                 {"id": "hv-2", "create_blocked_archive_id": None}]
    email = {"id": "e-2", "status": "create", "harvest_id": "hv-2", "create_blocked_archive_id": None,
             "from_address": "pm@gc.example", "subject": "Invitation to Bid: X",
             "received_at": "2026-09-09T10:00:00+00:00", "attachments_meta": [],
             "authorization_kind": "address", "authorization_rule_id": None,
             "extracted_project_name": None, "test_session_id": None}
    assert rfp_create.create_block_for(db, rfp_create.SOURCE_EMAIL, {**email, "create_blocked_archive_id": A3},
                                       settings=s) == A3
    assert rfp_create.create_block_for(db, rfp_create.SOURCE_EMAIL, {**email, "harvest_id": "hv-1"},
                                       settings=s) == A1
    assert rfp_create.create_block_for(db, rfp_create.SOURCE_EMAIL, email, settings=s) is None
    # A copy of the same message (another recipient) carries the mark.
    db.tables["rfp_emails"] = [{**email, "id": "e-1", "status": "created", "created_project_id": None,
                                "received_at": "2026-09-09T10:02:00+00:00",
                                "create_blocked_archive_id": A2}]
    assert rfp_create.create_block_for(db, rfp_create.SOURCE_EMAIL, email, settings=s) == A2
    # A BuildingConnected package sibling.
    db.tables["rfp_portal_invitations"] = [{"id": "inv-lead", "sibling_of": None, "create_blocked_archive_id": A1},
                                           {"id": "inv-2", "sibling_of": "inv-lead",
                                            "create_blocked_archive_id": None}]
    inv = {"id": "inv-2", "portal": "buildingconnected", "sibling_of": "inv-lead",
           "create_blocked_archive_id": None, "harvest_id": None}
    assert rfp_create.create_block_for(db, rfp_create.SOURCE_PORTAL, inv, settings=s) == A1


# ── A deleted project's stale link is a 404 ──────────────────────────────────


def test_a_single_read_of_a_missing_row_is_a_404_everything_else_re_raises():
    import app.main as main

    missing = APIError({"message": "JSON object requested, multiple (or no) rows returned",
                        "code": "PGRST116", "details": "The result contains 0 rows", "hint": None})
    resp = asyncio.run(main._postgrest_not_found_handler(None, missing))
    assert resp.status_code == 404
    other = APIError({"message": "boom", "code": "42P01", "details": None, "hint": None})
    with pytest.raises(APIError):
        asyncio.run(main._postgrest_not_found_handler(None, other))
    # A write into a project deleted from another tab trips the projects FK.
    gone = APIError({"message": 'insert or update on table "project_files" violates foreign key constraint',
                     "code": "23503", "hint": None,
                     "details": f'Key (project_id)=({P1}) is not present in table "projects".'})
    resp = asyncio.run(main._postgrest_not_found_handler(None, gone))
    assert resp.status_code == 404 and b"Project not found" in resp.body
    other_fk = APIError({"message": "fk", "code": "23503", "hint": None,
                         "details": 'Key (gc_id)=(x) is not present in table "general_contractors".'})
    with pytest.raises(APIError):
        asyncio.run(main._postgrest_not_found_handler(None, other_fk))


def test_get_project_on_a_deleted_project_is_a_404(monkeypatch):
    class Single:
        def __getattr__(self, name):
            return lambda *a, **k: self

        def execute(self):
            raise APIError({"message": "no rows", "code": "PGRST116", "details": None, "hint": None})

    monkeypatch.setattr(pr, "get_supabase", lambda: SimpleNamespace(table=lambda name: Single()))
    with pytest.raises(HTTPException) as exc:
        pr._fetch_project_with_outcome(P1)
    assert exc.value.status_code == 404
