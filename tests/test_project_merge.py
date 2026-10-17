"""Merge Projects (docs/PROJECT_MERGE.md): the service
(app/services/project_merge), the hashing helper (app/services/file_hashes)
and the three routes on /projects, called as plain functions against the
in-memory fake Supabase from tests/test_rfp_email_ingest. The database
function of migration 0149 (merge_rfp_project) is stood in by a small rpc
double here; its real behavior is exercised live on the dev database (the
doc's build record).

Pinned:

- the route table, the Bidding gate, the delete roles and the delete rate
  limit on all three routes; the request schemas;
- the pure rules: the confirm texts, the merge note, where a moved document
  lands (the hand-off lock), the work sentence, the target refusals, the
  search pattern, the visible bid date and the nearest-first order, which
  rows need a hash (sizes), the file plan by hash, the GC plan, the bell
  message and its audience (never the estimator), the error-code reader;
- hashing: the budget checked between files, a missing and an unreadable
  object, the conditional write, the streamed hash and its not-found
  readings;
- the source refusals (2.1), the merge targets (blocked, search by number,
  name and GC, the eligibility filters, likely targets by name, the bid
  date by role), the preview (progress, skipped by hash, in-use documents,
  GCs, RFP links, a blocked answer), the merge (the rpc payload, the copies,
  the retry on files_changed, every refusal mapped, copies removed on a
  failure, the audits and the bell);
- the `merged` reason: refused by the public delete, listed with
  `merged_into` on Deleted Projects, ignored by the tombstone check.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError
from pydantic import ValidationError
from storage3.exceptions import StorageApiError

from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.core.roles import PROJECT_DELETE_ROLES, Role
from app.models.schemas import ProjectMergeIn, ProjectMergePreviewIn
from app.routers import deleted_projects as dr
from app.routers import projects as pr
from app.services import file_hashes, files_needed, project_delete, storage
from app.services import project_merge as pm
from tests.test_rfp_email_ingest import FakeDB, _Rpc

S = "6c000000-0000-4000-8000-000000000001"   # the RFP-created project (source)
T = "6c000000-0000-4000-8000-000000000002"   # the project we are bidding (target)
OPEN = "6c000000-0000-4000-8000-000000000003"   # another open project
X = "6c000000-0000-4000-8000-000000000004"   # a closed project
G1 = "6d000000-0000-4000-8000-000000000001"
G2 = "6d000000-0000-4000-8000-000000000002"
G3 = "6d000000-0000-4000-8000-000000000003"
F1 = "6e000000-0000-4000-8000-000000000001"
F2 = "6e000000-0000-4000-8000-000000000002"
F3 = "6e000000-0000-4000-8000-000000000003"
F4 = "6e000000-0000-4000-8000-000000000004"
TF1 = "6e000000-0000-4000-8000-0000000000a1"
TF2 = "6e000000-0000-4000-8000-0000000000a2"
ARCHIVE = "6f000000-0000-4000-8000-000000000001"

NOW = datetime.now(timezone.utc)
BID = (NOW + timedelta(days=5)).replace(hour=21, minute=0, second=0, microsecond=0)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _api_error(code: str, details: str | None = None, marker: str = "project_merge") -> APIError:
    return APIError({"message": f"{marker}:{code}", "code": "P0001", "details": details, "hint": None})


class MergeDB(FakeDB):
    """The ingest fake plus the two 0149 functions the service calls:
    `project_merge_file_refs` answers the ids in `refs`, `merge_rfp_project`
    answers `result` or raises the next error queued in `raises`."""

    refs: set = set()
    result: dict | None = None
    raises: list = []

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, dict(params or {})))
        db = self
        if name == "project_merge_file_refs":
            ids = list((params or {}).get("p_file_ids") or [])
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[i for i in ids if i in db.refs]))
        if name == "merge_rfp_project":
            def execute():
                if db.raises:
                    raise db.raises.pop(0)
                return SimpleNamespace(data=db.result)

            return SimpleNamespace(execute=execute)
        return _Rpc(self, name, params)


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _project(pid, number, name, **over):
    row = {
        "id": pid, "number": number, "name": name, "current_stage": "intake", "abandoned_at": None,
        "pm_stage": None, "cp_enrolled_at": None, "test_session_id": None,
        "internal_bid_at": _iso(BID - timedelta(days=1)), "actual_bid_at": _iso(BID),
    }
    row.update(over)
    return row


def _file(fid, project_id, category, size, sha=None, **over):
    row = {
        "id": fid, "project_id": project_id, "category": category,
        "storage_path": f"{project_id}/{category}/{fid}.pdf", "preview_path": None,
        "preview_status": "none", "filename": f"{fid[-2:]}-{category}.pdf", "size_bytes": size,
        "sha256": sha, "note": None, "doc_type": None,
        "created_at": _iso(NOW - timedelta(minutes=int(fid[-2:], 16))),
    }
    row.update(over)
    return row


# Object bytes by storage path, for the fake hasher.
CONTENT = {
    f"{S}/drawing/{F2}.pdf": b"same bytes",
    f"{T}/drawing/{TF1}.pdf": b"same bytes",
}


def _seed():
    return {
        "projects": [
            _project(S, "26.10.7301", "ZZ Cimarron Scoreboard"),
            _project(T, "26.9.7120", "Cimarron Scoreboard", current_stage="rfqs"),
            _project(OPEN, "26.9.7200", "Desert Pines Clubhouse", current_stage="go_no_go",
                     actual_bid_at=_iso(BID + timedelta(days=30))),
            _project(X, "26.8.7001", "Cimarron Pool", current_stage="declined"),
        ],
        "rfp_created_projects": [{"project_id": S, "harvest_id": None, "files_status": "complete",
                                  "files_claimed_at": None}],
        "general_contractors": [{"id": G1, "name": "Acme Builders"}, {"id": G2, "name": "Beta GC"},
                                {"id": G3, "name": "Cimarron Partners"}],
        "project_gcs": [
            {"id": "pg-s1", "project_id": S, "gc_id": G1, "needs_by": None},
            {"id": "pg-s2", "project_id": S, "gc_id": G2, "needs_by": "2026-10-18"},
            {"id": "pg-t2", "project_id": T, "gc_id": G2, "needs_by": None},
            {"id": "pg-o3", "project_id": OPEN, "gc_id": G3, "needs_by": None},
        ],
        "project_files": [
            _file(F1, S, "drawing", 10, HASH_A),
            _file(F2, S, "drawing", 20),
            _file(F3, S, "rfq_split", 30),
            _file(F4, S, "specification", 40, note="Bid set"),
            _file(TF1, T, "drawing", 20),
            _file(TF2, T, "specification", 99, HASH_C),
        ],
        "rfqs": [], "rfq_sends": [], "quotes": [], "estimator_assignments": [], "proposal_sends": [],
        "external_submissions": [], "bid_outcomes": [],
        "rfp_project_matches": [{"id": "m-1", "project_id": S, "gc_id": G2}],
        "rfp_emails": [
            {"id": "e-1", "created_project_id": S, "match_project_id": None, "possible_rebid_project_id": None},
            {"id": "e-2", "created_project_id": None, "match_project_id": T, "possible_rebid_project_id": None},
        ],
        "rfp_portal_invitations": [],
        "rfp_harvests": [{"id": "h-1", "project_id": S}],
        "profiles": [{"id": "u1", "full_name": "Tom Moore"}],
        "deleted_projects": [],
    }


@pytest.fixture
def db(monkeypatch):
    fake = MergeDB(_seed())
    fake.refs = set()
    fake.result = None
    fake.raises = []
    monkeypatch.setattr(pr, "get_supabase", lambda: fake)
    monkeypatch.setattr(dr, "get_supabase", lambda: fake)
    monkeypatch.setattr(pm, "_split_running", lambda sb, record: False)
    monkeypatch.setattr(pm, "_handoff_locked", lambda project_id: False)

    def fake_hash(path):
        if path not in CONTENT:
            raise file_hashes.ObjectMissing(path)
        return hashlib.sha256(CONTENT[path]).hexdigest()

    monkeypatch.setattr(file_hashes, "sha256_of_object", fake_hash)
    return fake


@pytest.fixture
def effects(monkeypatch):
    """Every outward call the merge makes: copies, removals, audits, bells
    and the files-flag check."""
    out = SimpleNamespace(copies=[], removed=[], audits=[], bells=[], cleared=[], copy_error=None)

    def copy(src, dst):
        if out.copy_error is not None:
            raise out.copy_error
        out.copies.append((src, dst))

    monkeypatch.setattr(storage, "copy_file", copy)
    monkeypatch.setattr(storage, "delete_file", lambda path: out.removed.append(path))
    monkeypatch.setattr(pm, "audit", lambda *a, **k: out.audits.append(a))
    monkeypatch.setattr(pm, "notify_role", lambda *a, **k: out.bells.append((a, k)))
    monkeypatch.setattr(files_needed, "clear_if_satisfied",
                        lambda sb, pid, cat, actor: out.cleared.append((pid, cat)))
    return out


# ── Route table and gates ────────────────────────────────────────────────────


def _deps(route) -> set:
    return {d.call for d in route.dependant.dependencies}


def test_the_three_routes_are_bidding_only_delete_roles_and_rate_limited():
    routes = {(m, r.path): r for r in pr.router.routes for m in r.methods}
    bidding = pr._BIDDING_ONLY[0].dependency
    for key in (("GET", "/projects/{project_id}/merge-targets"),
                ("POST", "/projects/{project_id}/merge-preview"),
                ("POST", "/projects/{project_id}/merge")):
        calls = _deps(routes[key])
        assert bidding in calls and pr.require_project_delete in calls
        assert pr.project_delete_rate_limit in calls


@pytest.mark.parametrize("role", list(Role))
def test_only_the_delete_roles_may_merge(role):
    assert set(PROJECT_DELETE_ROLES) == {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}
    if role in PROJECT_DELETE_ROLES:
        assert asyncio.run(pr.require_project_delete(_user(role))).role == role
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(pr.require_project_delete(_user(role)))
        assert exc.value.status_code == 403


def test_request_schemas():
    assert ProjectMergePreviewIn(target_project_id=T).target_project_id == T
    body = ProjectMergeIn(target_project_id=T, confirm_source_name=" a ", confirm_target_name="b")
    assert body.confirm_source_name == " a "
    with pytest.raises(ValidationError):
        ProjectMergePreviewIn(target_project_id="")
    with pytest.raises(ValidationError):
        ProjectMergeIn(target_project_id=T, confirm_source_name="x" * 1001)


# ── Pure rules ───────────────────────────────────────────────────────────────


def test_confirm_text_is_the_delete_rule():
    assert pm.confirm_text({"name": "  Warehouse ", "number": "26.9.1"}) == "Warehouse"
    assert pm.confirm_text({"name": " ", "number": " 26.9.1B "}) == "26.9.1B"


def test_merge_note_names_the_old_project_and_keeps_the_old_note():
    source = {"number": "26.10.7301", "name": "ZZ Cimarron"}
    assert pm.merge_note(source) == "Moved from 26.10.7301 ZZ Cimarron (merged)"
    assert pm.merge_note(source, " Bid set ") == "Moved from 26.10.7301 ZZ Cimarron (merged). Bid set"
    assert pm.merge_note({"number": "26.1.1", "name": None}) == "Moved from 26.1.1 (merged)"
    assert len(pm.merge_note(source, "x" * 5000)) == 2000


@pytest.mark.parametrize(
    "row, locked, expected",
    [
        ({"category": "electrical_drawing"}, True, ("revision", "drawing")),
        ({"category": "drawing"}, True, ("revision", "drawing")),
        ({"category": "specification"}, True, ("revision", "specification")),
        ({"category": "rfp"}, True, ("additional", None)),
        ({"category": "addendum", "doc_type": "drawing"}, True, ("addendum", "drawing")),
        ({"category": "other"}, True, ("other", None)),
        ({"category": "electrical_drawing"}, False, ("electrical_drawing", None)),
        ({"category": "revision", "doc_type": "drawing"}, False, ("drawing", None)),
        ({"category": "revision", "doc_type": "specification"}, False, ("specification", None)),
        ({"category": "revision", "doc_type": None}, False, ("other", None)),
        ({"category": "additional"}, False, ("other", None)),
        ({"category": "addendum", "doc_type": None}, False, ("addendum", None)),
        ({"category": "marked_plans"}, False, ("marked_plans", None)),
    ],
)
def test_a_moved_document_lands_like_an_internal_upload(row, locked, expected):
    assert pm.landing(row, locked) == expected


def test_work_sentence_lists_what_was_found_in_order():
    text = pm.work_sentence(["quotes", "rfq_sends"])
    assert "RFQs sent to vendors, vendor quotes" in text and "bid it instead" in text
    err = pm._error("work_started", "estimator_assignments,bid_outcomes")
    assert err.status_code == 409 and "an estimator assignment, a bid outcome" in str(err)


def test_target_block_codes():
    source = {"id": S, "test_session_id": None}
    open_target = {"id": T, "current_stage": "rfqs", "abandoned_at": None, "pm_stage": None,
                   "test_session_id": None}
    assert pm.target_block(source, open_target, has_outcome=False) is None
    assert pm.target_block(source, None, has_outcome=False)["code"] == "target_not_found"
    assert pm.target_block(source, {**open_target, "id": S}, has_outcome=False)["code"] == "same_project"
    for over in ({"abandoned_at": "2026-01-01"}, {"current_stage": "declined"},
                 {"current_stage": "pm_only"}, {"current_stage": "cp_only"}, {"pm_stage": "precon"}):
        assert pm.target_block(source, {**open_target, **over}, has_outcome=False)["code"] == "target_closed"
    assert pm.target_block(source, open_target, has_outcome=True)["code"] == "target_closed"
    scoped = {**open_target, "test_session_id": "sess-1"}
    assert pm.target_block(source, scoped, has_outcome=False)["code"] == "target_scope"
    # A project at the Win / Loss head without an outcome is still being bid.
    assert pm.target_block(source, {**open_target, "current_stage": "bid_outcome"}, has_outcome=False) is None


def test_search_pattern_escapes_and_caps():
    assert pm.search_pattern(None) is None
    assert pm.search_pattern("   ") is None
    assert pm.search_pattern(" 50%_off\\x ") == "%50\\%\\_off\\\\x%"
    assert pm.search_pattern("a*b") == "%a_b%"
    assert pm.search_pattern("Smith, Jones (Phase 2)") == "%Smith, Jones (Phase 2)%"
    assert pm.search_pattern("x" * 300) == "%" + "x" * 100 + "%"
    assert pm.search_pattern("a\x00b\x7f") == "%ab%"


def test_visible_bid_at_follows_the_actual_bid_rule_and_orders_nearest_first():
    row = {"actual_bid_at": "2026-10-20T21:00:00+00:00", "internal_bid_at": "2026-10-19T21:00:00+00:00"}
    assert pm.visible_bid_at(row, Role.EXECUTIVE) == row["actual_bid_at"]
    assert pm.visible_bid_at(row, Role.ESTIMATING_ENGINEER_LABOR) == row["internal_bid_at"]
    assert pm.visible_bid_at({"actual_bid_at": None, "internal_bid_at": "x"}, Role.IT_ADMIN) == "x"
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    rows = [
        {"id": "far", "name": "a", "actual_bid_at": "2026-12-01T00:00:00+00:00"},
        {"id": "none", "name": "b", "actual_bid_at": None},
        {"id": "past", "name": "c", "actual_bid_at": "2026-10-05T00:00:00+00:00"},
        {"id": "soon", "name": "d", "actual_bid_at": "2026-10-10T00:00:00+00:00"},
    ]
    assert [r["id"] for r in pm.nearest_first(rows, Role.EXECUTIVE, now)] == ["past", "soon", "far", "none"]


def test_comparison_set_hashes_only_what_could_match():
    cands = [{"id": "c1", "size_bytes": 10}, {"id": "c2", "size_bytes": 20}]
    targets = [{"id": "t1", "size_bytes": 20}, {"id": "t2", "size_bytes": 30}]
    rel_c, rel_t = pm.comparison_set(cands, targets)
    assert [r["id"] for r in rel_c] == ["c2"] and [r["id"] for r in rel_t] == ["t1"]
    # An unknown size could match anything, so everything facing it is hashed.
    rel_c, rel_t = pm.comparison_set(cands + [{"id": "c3", "size_bytes": None}], targets)
    assert [r["id"] for r in rel_c] == ["c2", "c3"] and [r["id"] for r in rel_t] == ["t1", "t2"]
    assert pm.comparison_set([], targets) == ([], [])
    assert pm.comparison_set(cands, []) == ([], [])


def test_classify_skips_by_hash_and_moves_the_rest():
    cands = [
        {"id": "c1", "sha256": HASH_A, "filename": "a.pdf"},
        {"id": "c2", "sha256": HASH_B, "filename": "b.pdf"},
        {"id": "c3", "sha256": None, "filename": "c.pdf"},
        {"id": "c4", "sha256": None, "filename": "d.pdf"},
    ]
    targets = [
        {"id": "t2", "sha256": HASH_A, "filename": "newer.pdf", "created_at": "2026-10-02"},
        {"id": "t1", "sha256": HASH_A, "filename": "older.pdf", "created_at": "2026-10-01"},
        {"id": "t3", "sha256": None, "filename": "x.pdf", "created_at": "2026-10-01"},
    ]
    kept = [{"id": "k1"}]
    relevant = [cands[0], cands[2], targets[0], targets[2]]
    plan = pm.classify(cands, kept, targets, relevant, failed_ids={"c3"})
    assert [(c["id"], t["id"]) for c, t in plan.skipped] == [("c1", "t1")]
    assert [r["id"] for r in plan.moving] == ["c2", "c3", "c4"]
    assert [r["id"] for r in plan.unreadable] == ["c3"]
    assert plan.kept == kept
    # c1 and t2 hashed, c3 unreadable (done), t3 still waiting.
    assert (plan.hashed, plan.total, plan.ready) == (3, 4, False)
    assert pm.classify(cands, [], targets, [], set()).ready is True


def test_gc_plan_moves_new_gcs_with_a_due_date_and_lists_the_rest():
    source = {"actual_bid_at": "2026-10-21T05:00:00+00:00"}   # 10/20 in Pacific time
    links = [{"gc_id": G1, "needs_by": None}, {"gc_id": G2, "needs_by": "2026-10-18"},
             {"gc_id": G3, "needs_by": "2026-10-19"}]
    names = {G1: "Acme Builders", G2: "Beta GC", G3: "Cimarron Partners"}
    moving, already = pm.gc_plan(source, links, {G2}, names)
    assert moving == [
        {"gc_id": G1, "name": "Acme Builders", "needs_by": "2026-10-20"},
        {"gc_id": G3, "name": "Cimarron Partners", "needs_by": "2026-10-19"},
    ]
    assert already == [{"gc_id": G2, "name": "Beta GC"}]
    assert pm.gc_plan({"actual_bid_at": None}, links[:1], set(), names)[0][0]["needs_by"] is None


def test_notify_message():
    source = {"number": "26.10.7301", "name": "ZZ Cimarron"}
    one = pm.notify_message("Tom Moore", source, [{"name": "Acme", "needs_by": "2026-10-20"}], 0)
    assert one == "Tom Moore merged 26.10.7301 ZZ Cimarron into this project: GC added: Acme (due 10/20/2026)"
    two = pm.notify_message(None, source, [{"name": "Acme", "needs_by": None}, {"name": "Beta"}], 3)
    assert two.startswith("Someone merged") and "GCs added: Acme, Beta; 3 documents added" in two
    assert "1 document added" in pm.notify_message("A", source, [], 1)
    assert "nothing new was added" in pm.notify_message("A", source, [], 0)
    assert chr(0x2014) not in one + two


def test_stage_owner_roles_never_reach_the_estimator():
    assert pm.stage_owner_roles("intake") == [Role.ESTIMATING_ADMIN]
    assert pm.stage_owner_roles("estimate_received") == [Role.ESTIMATING_ENGINEER_MATERIALS]
    assert pm.stage_owner_roles("send_out") == [Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER_LABOR]
    assert pm.stage_owner_roles("nonsense") == [Role.ESTIMATING_ADMIN]
    for stage in ("intake", "go_no_go", "to_estimator", "estimate_received", "rfqs", "markup"):
        assert Role.ESTIMATOR not in pm.stage_owner_roles(stage)


def test_error_code_of_reads_both_markers():
    assert pm.error_code_of(_api_error("files_changed")) == "files_changed"
    assert pm.error_code_of(_api_error("referenced", marker="project_delete")) == "referenced"
    assert pm.error_code_of(RuntimeError("boom")) is None
    gone = pm._db_error(_api_error("not_found"))
    assert gone.code == "gone" and gone.status_code == 409
    with pytest.raises(RuntimeError):
        pm._db_error(RuntimeError("boom"))


# ── Hashing ──────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def test_fill_hashes_within_the_budget_and_records_failures():
    db = MergeDB({"project_files": [{"id": i, "sha256": None} for i in ("f1", "f2", "f3", "f4", "f5")]})
    rows = [
        {"id": "f1", "storage_path": "p/1", "sha256": None},
        {"id": "f2", "storage_path": "p/missing", "sha256": None},
        {"id": "f3", "storage_path": "p/broken", "sha256": None},
        {"id": "f0", "storage_path": "p/0", "sha256": HASH_C},
        {"id": "f4", "storage_path": "p/4", "sha256": None},
        {"id": "f5", "storage_path": "p/5", "sha256": None},
    ]

    def hasher(path):
        if path == "p/missing":
            raise file_hashes.ObjectMissing(path)
        if path == "p/broken":
            raise file_hashes.ObjectUnreadable("HTTP 500")
        return hashlib.sha256(path.encode()).hexdigest()

    # Each clock read advances 1 s; the first file is always attempted, and a
    # 3 s budget reaches f2 and f3 after it, never f4 or f5.
    run = file_hashes.fill(db, rows, budget_s=3, clock=_Clock(1.0), hasher=hasher)
    assert list(run.hashed) == ["f1"] and run.missing == {"f2"} and run.unreadable == {"f3"}
    assert run.remaining == ["f4", "f5"] and run.failed == {"f2", "f3"}
    assert rows[0]["sha256"] == hashlib.sha256(b"p/1").hexdigest()
    stored = {r["id"]: r["sha256"] for r in db.tables["project_files"]}
    assert stored["f1"] == rows[0]["sha256"] and stored["f4"] is None
    # The next call resumes where this one stopped.
    run2 = file_hashes.fill(db, rows, budget_s=100, clock=_Clock(1.0), hasher=hasher)
    assert set(run2.hashed) == {"f4", "f5"} and run2.missing == {"f2"}


def test_fill_never_overwrites_a_stored_hash():
    db = MergeDB({"project_files": [{"id": "f1", "sha256": HASH_A}]})
    row = {"id": "f1", "storage_path": "p", "sha256": None}
    file_hashes.fill(db, [row], budget_s=10, hasher=lambda p: HASH_B)
    assert db.tables["project_files"][0]["sha256"] == HASH_A   # conditional on sha256 is null


def _client_factory(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_sha256_of_object_streams_the_signed_url(monkeypatch):
    body = b"x" * (file_hashes.HASH_CHUNK * 2 + 7)
    monkeypatch.setattr(storage, "signed_url", lambda path, ttl: f"https://storage.test/{path}?token=t")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/gone"):
            return httpx.Response(404, json={"error": "not_found"})
        if request.url.path.endswith("/old-gone"):
            return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
        if request.url.path.endswith("/boom"):
            return httpx.Response(500, text="nope")
        return httpx.Response(200, content=body)

    factory = _client_factory(handler)
    assert file_hashes.sha256_of_object("p/file", client_factory=factory) == hashlib.sha256(body).hexdigest()
    with pytest.raises(file_hashes.ObjectMissing):
        file_hashes.sha256_of_object("p/gone", client_factory=factory)
    with pytest.raises(file_hashes.ObjectMissing):
        file_hashes.sha256_of_object("p/old-gone", client_factory=factory)
    with pytest.raises(file_hashes.ObjectUnreadable):
        file_hashes.sha256_of_object("p/boom", client_factory=factory)
    assert seen[0] == "https://storage.test/p/file?token=t"


def test_sha256_of_object_reads_a_signing_refusal(monkeypatch):
    def refuse(path, ttl):
        if path == "missing":
            raise StorageApiError("Object not found", "not_found", 400)
        raise StorageApiError("Internal", "internal", 500)

    monkeypatch.setattr(storage, "signed_url", refuse)
    with pytest.raises(file_hashes.ObjectMissing):
        file_hashes.sha256_of_object("missing")
    with pytest.raises(file_hashes.ObjectUnreadable):
        file_hashes.sha256_of_object("other")
    with pytest.raises(file_hashes.ObjectMissing):
        file_hashes.sha256_of_object("")


def test_a_dropped_stream_is_retried_once(monkeypatch):
    monkeypatch.setattr(storage, "signed_url", lambda path, ttl: "https://storage.test/p")
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("dropped")
        return httpx.Response(200, content=b"ok")

    assert file_hashes.sha256_of_object("p", client_factory=_client_factory(handler)) == hashlib.sha256(b"ok").hexdigest()
    assert len(calls) == 2


# ── The source refusals (2.1) ────────────────────────────────────────────────


def test_a_project_not_made_by_rfp_ingestion_is_blocked(db):
    db.tables["rfp_created_projects"] = []
    out = pr.project_merge_targets(S, q=None, limit=20, user=_user())
    assert out["items"] == [] and out["blocked"]["code"] == "not_from_rfp"


def test_pm_and_cp_are_blocked(db):
    db.tables["projects"][0]["pm_stage"] = "precon"
    assert pm.source_block(db, db.tables["projects"][0])["code"] == "pm_enrolled"
    db.tables["projects"][0].update({"pm_stage": None, "cp_enrolled_at": "2026-01-01T00:00:00Z"})
    assert pm.source_block(db, db.tables["projects"][0])["code"] == "cp_enrolled"


def test_work_started_names_what_was_found(db):
    db.tables["rfqs"] = [{"id": "r-1", "project_id": S}]
    db.tables["rfq_sends"] = [{"id": "rs-1", "rfq_id": "r-1"}]
    db.tables["estimator_assignments"] = [{"id": "ea-1", "project_id": S}]
    blocked = pm.source_block(db, db.tables["projects"][0])
    assert blocked["code"] == "work_started"
    assert "RFQs sent to vendors, an estimator assignment" in blocked["message"]
    assert pm.work_found(db, S) == ["rfq_sends", "estimator_assignments"]
    db.tables["rfq_sends"] = []
    db.tables["estimator_assignments"] = []
    db.tables["quotes"] = [{"id": "q-1", "rfq_id": "r-1"}]
    db.tables["bid_outcomes"] = [{"id": "bo-1", "project_id": S}]
    assert pm.work_found(db, S) == ["quotes", "bid_outcomes"]


def test_files_arriving_and_split_running(db, monkeypatch):
    record = db.tables["rfp_created_projects"][0]
    record["files_status"] = "pending"
    assert pm.source_block(db, db.tables["projects"][0])["code"] == "files_arriving"
    record.update({"files_status": "running", "files_claimed_at": _iso(NOW)})
    assert pm.source_block(db, db.tables["projects"][0])["code"] == "files_arriving"
    # A claim older than the queue lease is a dead worker's.
    record["files_claimed_at"] = _iso(NOW - timedelta(days=2))
    assert pm.source_block(db, db.tables["projects"][0]) is None
    record["files_status"] = "complete"
    monkeypatch.setattr(pm, "_split_running", lambda sb, rec: True)
    assert pm.source_block(db, db.tables["projects"][0])["code"] == "split_running"


def test_a_failed_split_check_reads_as_running(monkeypatch):
    from app.services import rfp_split

    def boom(sb, record):
        raise RuntimeError("db down")

    monkeypatch.setattr(rfp_split, "split_running", boom)
    assert pm._split_running(None, {"project_id": S}) is True


# ── Merge targets ────────────────────────────────────────────────────────────


def test_search_matches_number_name_and_gc_and_keeps_only_open_projects(db):
    by_name = pr.project_merge_targets(S, q="cimarron", limit=20, user=_user())
    assert by_name["blocked"] is None
    ids = [i["id"] for i in by_name["items"]]
    # The declined Cimarron Pool and the source itself never show; the GC
    # "Cimarron Partners" brings in Desert Pines.
    assert ids == [T, OPEN]
    item = by_name["items"][0]
    assert item == {"id": T, "number": "26.9.7120", "name": "Cimarron Scoreboard", "stage": "rfqs",
                    "bid_at": _iso(BID), "gc_names": ["Beta GC"], "rfp_created": False}
    by_number = pr.project_merge_targets(S, q=" 26.9.72 ", limit=20, user=_user())
    assert [i["id"] for i in by_number["items"]] == [OPEN]
    assert pr.project_merge_targets(S, q="cimarron", limit=1, user=_user())["items"][0]["id"] == T


def test_search_drops_abandoned_decided_and_other_scope_projects(db):
    db.tables["bid_outcomes"] = [{"id": "bo", "project_id": T}]
    db.tables["projects"][2]["test_session_id"] = "sess-1"
    assert pr.project_merge_targets(S, q="cimarron", limit=20, user=_user())["items"] == []
    db.tables["bid_outcomes"] = []
    db.tables["projects"][1]["abandoned_at"] = _iso(NOW)
    assert pr.project_merge_targets(S, q="cimarron", limit=20, user=_user())["items"] == []


def test_likely_targets_by_name_before_the_user_types(db):
    out = pr.project_merge_targets(S, q=None, limit=20, user=_user())
    assert [i["id"] for i in out["items"]] == [T]   # Desert Pines scores below the floor
    assert pr.project_merge_targets(S, q="   ", limit=20, user=_user())["items"][0]["id"] == T


def test_bid_at_is_the_internal_date_for_a_role_that_may_not_see_the_actual_one(db):
    out = pm.merge_targets(db, S, q="cimarron", limit=20, role=Role.ESTIMATING_ENGINEER_LABOR)
    assert out["items"][0]["bid_at"] == _iso(BID - timedelta(days=1))


def test_merge_targets_unknown_and_malformed_source_are_404(db):
    for pid in ("not-a-uuid", X.replace("4", "5")):
        with pytest.raises(HTTPException) as exc:
            pr.project_merge_targets(pid, q=None, limit=20, user=_user())
        assert exc.value.status_code == 404


# ── Preview ──────────────────────────────────────────────────────────────────


def test_preview_compares_documents_and_lists_what_moves(db):
    db.tables["rfp_created_projects"].append({"project_id": T})
    out = pr.project_merge_preview(S, ProjectMergePreviewIn(target_project_id=T), _user())
    assert out["blocked"] is None and out["ready"] is True
    # F2 and TF1 share a size, so both were hashed (and match); F1, F4 and
    # TF2 have sizes the other side lacks.
    assert (out["hashed"], out["total"]) == (2, 2)
    assert out["source"] == {"id": S, "number": "26.10.7301", "name": "ZZ Cimarron Scoreboard",
                             "confirm_text": "ZZ Cimarron Scoreboard"}
    assert out["target"] == {"id": T, "number": "26.9.7120", "name": "Cimarron Scoreboard",
                             "confirm_text": "Cimarron Scoreboard", "stage": "rfqs"}
    assert sorted(f["id"] for f in out["files_moving"]) == [F1, F4] and out["files_moving_total"] == 2
    skipped = {f["id"]: f for f in out["files_skipped"]}
    assert skipped[F2]["same_as_filename"] == db.tables["project_files"][4]["filename"]
    assert skipped[F2]["reason"] == "duplicate"
    assert skipped[F3] == {"id": F3, "filename": "03-rfq_split.pdf", "category": "rfq_split",
                           "same_as_filename": None, "reason": "in_use"}
    assert out["files_skipped_total"] == 2
    assert out["files_unreadable"] == [] and out["files_unreadable_total"] == 0
    assert out["gcs_moving"] == [{"gc_id": G1, "name": "Acme Builders",
                                  "needs_by": pm.pacific_date(_iso(BID))}]
    assert out["gcs_already"] == [{"gc_id": G2, "name": "Beta GC"}]
    # One match record, one email, one harvest.
    assert out["rfp_links"] == 3
    stored = {r["id"]: r["sha256"] for r in db.tables["project_files"]}
    assert stored[F2] == stored[TF1] == hashlib.sha256(b"same bytes").hexdigest()


def test_preview_reports_progress_until_everything_is_hashed(db, monkeypatch):
    monkeypatch.setattr(pm, "HASH_BUDGET_S", 0.0)
    out = pm.preview(db, S, T, budget_s=0.0)
    # The budget is checked between files: the first file always finishes.
    assert (out["ready"], out["hashed"], out["total"]) == (False, 1, 2)
    out = pm.preview(db, S, T, budget_s=0.0)
    assert (out["ready"], out["hashed"], out["total"]) == (True, 2, 2)


def test_a_missing_object_is_reported_and_moves(db):
    db.tables["project_files"][1]["storage_path"] = f"{S}/drawing/lost.pdf"
    out = pm.preview(db, S, T)
    assert out["ready"] is True
    assert out["files_unreadable"] == [{"id": F2, "filename": "02-drawing.pdf"}]
    assert F2 in [f["id"] for f in out["files_moving"]]


def test_documents_another_row_points_at_stay(db):
    db.refs = {F4}
    out = pm.preview(db, S, T)
    assert [f["id"] for f in out["files_moving"]] == [F1]
    assert {f["id"]: f["reason"] for f in out["files_skipped"]}[F4] == "in_use"


def test_preview_lists_are_capped_with_totals(db, monkeypatch):
    monkeypatch.setattr(pm, "LIST_CAP", 1)
    out = pm.preview(db, S, T)
    assert len(out["files_moving"]) == 1 and out["files_moving_total"] == 2
    assert len(out["files_skipped"]) == 1 and out["files_skipped_total"] == 2


@pytest.mark.parametrize(
    "target, setup, code",
    [
        (S, None, "same_project"),
        ("not-a-uuid", None, "target_not_found"),
        (OPEN.replace("3", "9"), None, "target_not_found"),
        (X, None, "target_closed"),
        (T, lambda d: d.tables["bid_outcomes"].append({"id": "bo", "project_id": T}), "target_closed"),
        (T, lambda d: d.tables["projects"][1].update({"test_session_id": "s"}), "target_scope"),
        (T, lambda d: d.tables["rfp_created_projects"].clear(), "not_from_rfp"),
    ],
)
def test_a_blocked_preview_is_final_and_hashes_nothing(db, target, setup, code):
    if setup:
        setup(db)
    out = pr.project_merge_preview(S, ProjectMergePreviewIn(target_project_id=target), _user())
    assert out["blocked"]["code"] == code and out["ready"] is False
    assert out["files_moving"] == [] and out["hashed"] == 0
    assert all(r["sha256"] in (None, HASH_A, HASH_C) for r in db.tables["project_files"])


# ── Merge ────────────────────────────────────────────────────────────────────


def _result(**over):
    out = {
        "archive_id": ARCHIVE,
        "source": {"id": S, "number": "26.10.7301", "name": "ZZ Cimarron Scoreboard"},
        "target": {"id": T, "number": "26.9.7120", "name": "Cimarron Scoreboard"},
        "gcs_moved": [{"gc_id": G1, "name": "Acme Builders", "needs_by": "2026-10-12"}],
        "gcs_already": [{"gc_id": G2, "name": "Beta GC"}],
        "files_moved": 2, "files_skipped": 2, "rfp_links_moved": 3, "row_total": 14,
    }
    out.update(over)
    return out


def _body(source="ZZ Cimarron Scoreboard", target="Cimarron Scoreboard"):
    return ProjectMergeIn(target_project_id=T, confirm_source_name=source, confirm_target_name=target)


def test_merge_copies_the_moving_documents_and_calls_the_function(db, effects):
    db.result = _result()
    out = pr.merge_project(S, _body(" ZZ Cimarron Scoreboard "), _user(Role.EXECUTIVE, "u1"))
    assert out == {k: v for k, v in _result().items() if k != "row_total"}
    ((name, params),) = [c for c in db.rpc_calls if c[0] == "merge_rfp_project"]
    assert params["p_source"] == S and params["p_target"] == T and params["p_actor"] == "u1"
    assert params["p_confirm_source"] == " ZZ Cimarron Scoreboard "
    files = {f["file_id"]: f for f in params["p_files"]}
    assert set(files) == {F1, F4}   # never the duplicate (F2) nor the work file (F3)
    for f in files.values():
        assert f["storage_path"].startswith(f"{T}/") and f["preview_path"] is None
    assert files[F1]["category"] == "drawing" and files[F1]["doc_type"] is None
    assert files[F1]["note"] == "Moved from 26.10.7301 ZZ Cimarron Scoreboard (merged)"
    assert files[F4]["note"] == "Moved from 26.10.7301 ZZ Cimarron Scoreboard (merged). Bid set"
    assert sorted(src for src, _ in effects.copies) == sorted([f"{S}/drawing/{F1}.pdf",
                                                               f"{S}/specification/{F4}.pdf"])
    assert effects.removed == []
    actions = [(a[1], a[3]) for a in effects.audits]
    assert actions == [("project.delete", S), ("project.merge", T)]
    delete_payload = effects.audits[0][4]
    assert delete_payload["reason"] == "merged" and delete_payload["merged_into"] == T
    merge_payload = effects.audits[1][4]
    assert merge_payload["source_project_id"] == S and merge_payload["files_moved"] == 2
    # The target is at RFQs: the Materials engineer owns it. Bell only.
    ((args, kwargs),) = effects.bells
    assert args[0] == Role.ESTIMATING_ENGINEER_MATERIALS and args[1] == T and args[2] == "project_merged"
    assert args[3] == ("Tom Moore merged 26.10.7301 ZZ Cimarron Scoreboard into this project: "
                       "GC added: Acme Builders (due 10/12/2026); 2 documents added")
    assert kwargs["mirror_email"] is False
    assert sorted(effects.cleared) == [(T, "drawing"), (T, "specification")]


def test_merge_into_a_sent_package_lands_documents_as_revisions(db, effects, monkeypatch):
    monkeypatch.setattr(pm, "_handoff_locked", lambda project_id: True)
    db.tables["project_files"][0]["preview_path"] = f"{S}/previews/{F1}.pdf"
    db.result = _result()
    pr.merge_project(S, _body(), _user())
    files = {f["file_id"]: f for f in db.rpc_calls[-1][1]["p_files"]}
    assert (files[F1]["category"], files[F1]["doc_type"]) == ("revision", "drawing")
    assert (files[F4]["category"], files[F4]["doc_type"]) == ("revision", "specification")
    assert files[F1]["storage_path"].startswith(f"{T}/revision/")
    assert files[F1]["preview_path"] == f"{T}/previews/{F1}.pdf"
    # A revision upload does not satisfy the BuildingConnected files flag.
    assert effects.cleared == []


def test_a_missing_object_moves_without_bytes(db, effects, monkeypatch):
    def copy(src, dst):
        if src.endswith(f"{F4}.pdf"):
            raise StorageApiError("Object not found", "not_found", 400)
        effects.copies.append((src, dst))

    monkeypatch.setattr(storage, "copy_file", copy)
    db.result = _result()
    pr.merge_project(S, _body(), _user())
    files = {f["file_id"]: f for f in db.rpc_calls[-1][1]["p_files"]}
    assert files[F4]["storage_path"].startswith(f"{T}/") and len(effects.copies) == 1


def test_a_copy_failure_removes_the_copies_and_merges_nothing(db, effects, monkeypatch):
    calls = []

    def copy(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise StorageApiError("Internal", "internal", 500)

    monkeypatch.setattr(storage, "copy_file", copy)
    with pytest.raises(HTTPException) as exc:
        pr.merge_project(S, _body(), _user())
    assert exc.value.status_code == 409
    assert exc.value.headers == {"X-Error-Code": "project_merge_copy_failed"}
    assert effects.removed == calls[:1]
    assert [c for c in db.rpc_calls if c[0] == "merge_rfp_project"] == []


def test_files_changed_replans_and_retries_once(db, effects):
    db.raises = [_api_error("files_changed", "the target has the same document")]
    db.result = _result()
    out = pr.merge_project(S, _body(), _user())
    assert out["archive_id"] == ARCHIVE
    assert len([c for c in db.rpc_calls if c[0] == "merge_rfp_project"]) == 2
    # The first attempt's copies were removed; the second's were not.
    assert len(effects.removed) == 2 and len(effects.copies) == 4


def test_files_changed_twice_is_a_409_for_the_modal(db, effects):
    db.raises = [_api_error("files_changed"), _api_error("files_changed")]
    with pytest.raises(HTTPException) as exc:
        pr.merge_project(S, _body(), _user())
    assert exc.value.status_code == 409
    assert exc.value.headers == {"X-Error-Code": "project_merge_files_changed"}
    assert len(effects.removed) == 4 and effects.audits == [] and effects.bells == []


def test_files_not_ready_when_hashing_does_not_finish(db, effects, monkeypatch):
    real_fill = file_hashes.fill
    monkeypatch.setattr(file_hashes, "fill", lambda sb, rows, budget_s: real_fill(sb, rows[:1], budget_s=budget_s))
    with pytest.raises(HTTPException) as exc:
        pr.merge_project(S, _body(), _user())
    assert exc.value.status_code == 409
    assert exc.value.headers == {"X-Error-Code": "project_merge_files_not_ready"}
    assert effects.copies == []


@pytest.mark.parametrize(
    "code, details, status",
    [
        ("source_name_mismatch", None, 422),
        ("target_name_mismatch", None, 422),
        ("work_started", "rfq_sends,quotes", 409),
        ("target_closed", None, 409),
        ("target_scope", None, 409),
        ("not_from_rfp", None, 409),
        ("not_found", None, 409),          # the second of two merges of the pair
        ("target_not_found", None, 409),
        ("bad_files", None, 409),
    ],
)
def test_every_database_refusal_is_mapped(db, effects, code, details, status):
    db.raises = [_api_error(code, details)]
    with pytest.raises(HTTPException) as exc:
        pr.merge_project(S, _body(), _user())
    expected = "gone" if code == "not_found" else code
    assert exc.value.status_code == status
    assert exc.value.headers == {"X-Error-Code": f"project_merge_{expected}"}
    assert "project_merge:" not in exc.value.detail
    if code == "work_started":
        assert "RFQs sent to vendors, vendor quotes" in exc.value.detail
    assert len(effects.removed) == len(effects.copies)   # every copy removed
    assert effects.audits == []


def test_the_archive_codes_pass_through(db, effects):
    db.raises = [_api_error("referenced", "pay_app_lines (2)", marker="project_delete")]
    with pytest.raises(HTTPException) as exc:
        pr.merge_project(S, _body(), _user())
    assert exc.value.headers == {"X-Error-Code": "project_merge_referenced"}
    assert "pay_app_lines (2)" in exc.value.detail


def test_name_mismatches_are_422_before_anything_is_copied(db, effects):
    for body, code in ((_body(source="ZZ Cimarron"), "source_name_mismatch"),
                       (_body(target="cimarron scoreboard"), "target_name_mismatch"),
                       (_body(source=""), "source_name_mismatch")):
        with pytest.raises(HTTPException) as exc:
            pr.merge_project(S, body, _user())
        assert exc.value.status_code == 422
        assert exc.value.headers == {"X-Error-Code": f"project_merge_{code}"}
    assert effects.copies == [] and db.rpc_calls == []


def test_a_blocked_merge_is_refused_before_anything_is_copied(db, effects):
    db.tables["rfqs"] = [{"id": "r-1", "project_id": S}]
    db.tables["quotes"] = [{"id": "q-1", "rfq_id": "r-1"}]
    with pytest.raises(HTTPException) as exc:
        pr.merge_project(S, _body(), _user())
    assert exc.value.status_code == 409
    assert exc.value.headers == {"X-Error-Code": "project_merge_work_started"}
    assert "vendor quotes" in exc.value.detail and effects.copies == []


def test_merge_unknown_and_malformed_source_are_404(db, effects):
    for pid in ("nope", X.replace("4", "5")):
        with pytest.raises(HTTPException) as exc:
            pr.merge_project(pid, _body(), _user())
        assert exc.value.status_code == 404


def test_a_failed_bell_or_audit_never_fails_the_merge(db, effects, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(pm, "audit", boom)
    monkeypatch.setattr(pm, "notify_role", boom)
    db.result = _result()
    assert pr.merge_project(S, _body(), _user())["archive_id"] == ARCHIVE


def _issue_rows():
    """Missing-files items on the source (docs/RFP_MISSING_FILES.md 10): two
    open, one handled, and one whose key the target already holds."""
    base = {"kind": "file", "group": "not_downloaded", "status": "download_failed", "resolved_at": None}
    return [
        {**base, "id": "i-1", "project_id": S, "item_key": "k-plans", "name": "Plans.pdf"},
        {**base, "id": "i-2", "project_id": S, "item_key": "k-link", "kind": "link", "name": "SharePoint"},
        {**base, "id": "i-3", "project_id": S, "item_key": "k-done", "name": "Done.pdf",
         "resolved_at": "2026-10-09T00:00:00+00:00", "resolved_how": "manual"},
        {**base, "id": "i-4", "project_id": S, "item_key": "k-shared", "name": "Shared.pdf"},
        {**base, "id": "t-4", "project_id": T, "item_key": "k-shared", "name": "Shared.pdf"},
    ]


def test_a_merge_moves_the_sources_open_missing_files_items_to_the_target(db, effects):
    db.tables["rfp_file_issues"] = _issue_rows()
    db.tables["rfp_created_projects"].append({"project_id": T, "harvest_id": None, "files_status": "complete"})
    db.result = _result()
    pr.merge_project(S, _body(), _user())
    where = {r["id"]: r["project_id"] for r in db.tables["rfp_file_issues"]}
    assert where == {"i-1": T, "i-2": T, "i-3": S, "i-4": S, "t-4": T}


def test_a_target_not_made_by_rfp_ingestion_never_takes_the_items(db, effects):
    """Its page has no callout and its record could not carry a dismissal:
    the items stay with the archived source (docs/RFP_MISSING_FILES.md 10)."""
    db.tables["rfp_file_issues"] = _issue_rows()
    db.result = _result()
    pr.merge_project(S, _body(), _user())
    assert all(r["project_id"] == S for r in db.tables["rfp_file_issues"] if r["id"].startswith("i-"))


def test_a_failed_merge_moves_the_items_back(db, effects, monkeypatch):
    from app.services import rfp_file_issues

    db.tables["rfp_file_issues"] = _issue_rows()
    db.tables["rfp_created_projects"].append({"project_id": T, "harvest_id": None, "files_status": "complete"})
    db.raises = [_api_error("work_started", "rfq_sends")]
    moved = []
    real_move = rfp_file_issues.move_open_items
    monkeypatch.setattr(rfp_file_issues, "move_open_items",
                        lambda sb, source_id, target_id: moved.extend(real_move(sb, source_id, target_id)) or moved)
    with pytest.raises(HTTPException):
        pr.merge_project(S, _body(), _user())
    assert sorted(moved) == ["i-1", "i-2"]       # they did move, then came back
    assert all(r["project_id"] == S for r in db.tables["rfp_file_issues"] if r["id"].startswith("i-"))


# ── The merged reason on the delete side ─────────────────────────────────────


def test_the_public_delete_refuses_the_merged_reason():
    assert "merged" not in project_delete.REASONS
    with pytest.raises(project_delete.ProjectDeleteError) as exc:
        project_delete.validate_request("merged", None, "x")
    assert exc.value.code == "bad_reason"
    assert project_delete.reasons_for(True) == list(project_delete.REASONS)


def _archive(aid, reason, **over):
    row = {
        "id": aid, "project_id": S, "project_number": "26.10.7301", "project_name": "ZZ Cimarron",
        "project_stage": "intake", "gc_names": [], "gc_ids": [], "gc_links": [], "reason": reason,
        "note": None, "from_rfp": True, "rfp_source_kind": "rfp_email", "rfp_source_id": "e-1",
        "rfp_source_label": "ITB", "deleted_by": "u1", "deleted_at": _iso(NOW), "row_counts": {},
        "row_total": 3, "restored_at": None, "restored_by": None, "restore_error": None,
        "merged_into_project_id": None, "merged_into_number": None, "merged_into_name": None,
        "test_session_id": None, "project_bid_at": _iso(BID),
        "project_row": {"name": "Cimarron Scoreboard", "number": "26.10.7301",
                        "actual_bid_at": _iso(BID), "internal_bid_at": None, "bid_notes": None},
    }
    row.update(over)
    return row


def test_deleted_projects_list_and_summary_carry_merged(db):
    db.tables["deleted_projects"] = [
        _archive(ARCHIVE, "merged", merged_into_project_id=T, merged_into_number="26.9.7120",
                 merged_into_name="Cimarron Scoreboard"),
        _archive("6f000000-0000-4000-8000-000000000002", "duplicate", deleted_at=_iso(NOW - timedelta(hours=1))),
    ]
    rows = dr.list_deleted_projects(limit=50, before=None, before_id=None, reason="merged",
                                    restored="false", _=_user(Role.IT_ADMIN))
    assert [r["id"] for r in rows] == [ARCHIVE]
    assert rows[0]["merged_into"] == {"project_id": T, "number": "26.9.7120", "name": "Cimarron Scoreboard"}
    every = dr.list_deleted_projects(limit=50, before=None, before_id=None, reason=None,
                                     restored="all", _=_user(Role.IT_ADMIN))
    assert every[1]["merged_into"] is None
    # The project merged into was deleted since: the label stays, the link goes.
    db.tables["deleted_projects"][0]["merged_into_project_id"] = None
    rows = dr.list_deleted_projects(limit=50, before=None, before_id=None, reason="merged",
                                    restored="all", _=_user(Role.IT_ADMIN))
    assert rows[0]["merged_into"]["project_id"] is None and rows[0]["merged_into"]["number"] == "26.9.7120"
    summary = dr.deleted_projects_summary(_user(Role.IT_ADMIN))
    assert summary["by_reason"]["merged"] == 1 and summary["by_reason"]["duplicate"] == 1


def test_the_tombstone_check_ignores_merged_archives(db):
    settings = get_settings()
    row = {"id": "e-9", "extracted_project_name": "Cimarron Scoreboard", "extracted_bid_due_at": _iso(BID),
           "resolved_gc_id": None, "excluded_project_ids": [], "test_session_id": None,
           "match_review_decision": None}
    db.tables["deleted_projects"] = [_archive(ARCHIVE, "merged", merged_into_number="26.9.7120")]
    assert project_delete.tombstone_for_email(db, row, settings) is None
    db.tables["deleted_projects"] = [_archive(ARCHIVE, "rfp_should_not_exist")]
    assert project_delete.tombstone_for_email(db, row, settings) == ARCHIVE
