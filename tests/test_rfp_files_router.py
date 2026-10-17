"""Missing files on an RFP-created project: the routes
(app/routers/rfp_files, docs/RFP_MISSING_FILES.md section 7) and the drop
zone (app/services/rfp_file_drop, section 5), through the real app with the
in-memory fake Supabase from tests/test_rfp_email_ingest.

Pinned:

- the route table, literal paths before parameters, the gates on every
  route (the RFP Ingestion switch, the project's assignment gate, internal
  roles on the read, writers on the writes), the rate limits;
- permissions end to end: the read for every internal role (the
  accountant included), the writes refused to the accountant, the
  estimator refused (an unassigned one by the project gate);
- each route's happy path and refusals (404 on another project's item or
  an item in the wrong state, 409 on the dismissal toggles with their
  sentences);
- the drop's routing table: a PDF to the splitter on a `project` job, a
  PDF whole when the splitter is off, its model away, over the page cap
  or the package sent (then under Additional files), a .docx to
  `specification` by its name, an .xlsx to `other`, a .png and a renamed
  file 415, a PDF that will not open 415; `job_id` reuse and expiry; the
  up-front name match and its reopening when the store fails;
- `looks_like_spec`, `route_for`, `splitter_state`.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from app.core import deps
from app.core.deps import CurrentUser, get_current_user, require_internal, require_project_assignment, require_writer
from app.core.roles import Role
from app.routers import files as files_router
from app.routers import rfp_files as rf
from app.services import bid_split, files_needed, rfp_file_drop, rfp_split, storage
from app.services import rfp_file_issues as rfi
from tests.test_rfp_email_ingest import FakeDB
from tests.test_rfp_file_issues import EMAIL, HV, URL_A, _email_harvest, _record

P1 = "5b000000-0000-4000-8000-000000000001"
P2 = "5b000000-0000-4000-8000-000000000002"
USERS = {
    "admin": ("u-admin", Role.ESTIMATING_ADMIN),
    "exec": ("u-exec", Role.EXECUTIVE),
    "acct": ("u-acct", Role.ACCOUNTANT),
    "est": ("u-est", Role.ESTIMATOR),
}


def _pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


DOCX = b"PK\x03\x04" + b"\x00" * 64
XLS = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _user(key="admin") -> CurrentUser:
    uid, role = USERS[key]
    return CurrentUser(id=uid, email=f"{key}@g3.test", role=role, is_active=True)


class JobsDB(FakeDB):
    """The fake with uuid ids on new splitter jobs (the drop route only
    joins a `job_id` that is a uuid, as real ids are)."""

    def table(self, name):
        query = super().table(name)
        if name == "bid_split_jobs":
            plain = query.insert

            def insert(payload):
                if isinstance(payload, dict):
                    payload = {"id": str(uuid.uuid4()), **payload}
                return plain(payload)

            query.insert = insert
        return query


def _db():
    db = JobsDB({
        "projects": [{"id": P1, "number": "26.10.7300", "name": "Warehouse HVAC"},
                     {"id": P2, "number": "26.10.7301", "name": "Clinic TI"}],
        "rfp_created_projects": [_record(P1)],
        "rfp_harvests": [_email_harvest()],
        "rfp_emails": [dict(EMAIL)],
        "rfp_file_issues": [],
        "project_files": [],
        "profiles": [{"id": uid, "full_name": key.title()} for key, (uid, _r) in USERS.items()],
        "bid_split_jobs": [],
        "bid_split_files": [],
        "estimator_assignments": [],
        "audit_log": [],
    })
    rfi.sync_project(db, P1)
    # Real item ids are uuids (the routes validate them).
    for row in db.tables["rfp_file_issues"]:
        row["id"] = str(uuid.uuid4())
    return db


def _item(db, name):
    return next(r for r in db.tables["rfp_file_issues"] if r["name"] == name)


@pytest.fixture
def env(monkeypatch):
    import app.main

    db = _db()
    state = {"sent": False, "splitter": (True, None)}
    audits: list[tuple] = []
    dispatched: list[str] = []
    uploaded: list[str] = []
    for mod in (rf, files_router, deps):
        monkeypatch.setattr(mod, "get_supabase", lambda: db)
    monkeypatch.setattr(files_router, "handoff_locked", lambda pid: state["sent"])
    monkeypatch.setattr(rfp_file_drop, "splitter_state", lambda s=None: state["splitter"])
    for mod in (rfi, rfp_file_drop, files_router, files_needed):
        monkeypatch.setattr(mod, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(files_router, "_notify_drawing_changed", lambda *a, **k: None)
    monkeypatch.setattr(storage, "upload_file", lambda path, *a, **k: uploaded.append(path))
    monkeypatch.setattr(storage, "delete_file", lambda *a, **k: None)
    monkeypatch.setattr(files_router.office_preview, "is_convertible", lambda *a, **k: False)
    monkeypatch.setattr(bid_split, "dispatch", lambda bg, fid, uid, forced_kind=None: dispatched.append(fid))
    monkeypatch.setattr(rfp_file_drop.llm, "active_model", lambda *a, **k: "test-model")
    monkeypatch.setattr("app.services.security_alerts.record_denied_access", lambda *a, **k: None)
    # The per-account budgets are pinned by their own tests; here they would
    # only count this module's uploads against one fake account.
    from app.core import ratelimit

    unlimited = ratelimit.get_settings().model_copy(update={"rate_limit_enabled": False})
    monkeypatch.setattr(ratelimit, "get_settings", lambda: unlimited)
    who = {"user": "admin"}
    app.main.app.dependency_overrides[get_current_user] = lambda: _user(who["user"])
    client = TestClient(app.main.app, raise_server_exceptions=True)
    yield SimpleNamespace(db=db, client=client, state=state, audits=audits, dispatched=dispatched,
                          uploaded=uploaded, who=who)
    app.main.app.dependency_overrides.pop(get_current_user, None)


def _url(path="", project_id=P1):
    return f"/projects/{project_id}/rfp-files{path}"


# ── The route table and the gates ────────────────────────────────────────


def test_route_table_is_exactly_the_spec():
    pairs = {(m, r.path) for r in rf.router.routes for m in r.methods}
    base = "/projects/{project_id}/rfp-files"
    assert pairs == {
        ("GET", f"{base}/issue"),
        ("POST", f"{base}/dismiss"),
        ("DELETE", f"{base}/dismiss"),
        ("POST", f"{base}/drop"),
        ("POST", f"{base}/items/{{item_id}}/resolve"),
        ("DELETE", f"{base}/items/{{item_id}}/resolve"),
    }
    paths = [r.path for r in rf.router.routes]
    assert max(paths.index(f"{base}/{p}") for p in ("issue", "dismiss", "drop")) < paths.index(
        f"{base}/items/{{item_id}}/resolve")
    for route in rf.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert rf.require_rfp_files in calls and require_project_assignment in calls, route.path
        writes = route.methods & {"POST", "DELETE"}
        assert (require_writer if writes else require_internal) in calls, route.path
        budget = rf.upload_rate_limit if route.path.endswith("/drop") else rf.rfp_files_rate_limit
        assert budget in calls, route.path


def test_the_switch_fails_closed(monkeypatch):
    monkeypatch.setattr(rf, "get_settings", lambda: SimpleNamespace())
    assert rf.rfp_files_enabled() is False
    with pytest.raises(HTTPException) as exc:
        rf.require_rfp_files()
    assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
    monkeypatch.setattr(rf, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True))
    assert rf.require_rfp_files() is None


def test_permissions_end_to_end(env, monkeypatch):
    c = env.client
    # Reads: every internal role, the read-only accountant included.
    for who in ("admin", "exec", "acct"):
        env.who["user"] = who
        resp = c.get(_url("/issue"))
        assert resp.status_code == 200 and resp.json()["counts"]["open"] == 4, who
    # Writes: never the accountant.
    env.who["user"] = "acct"
    item = _item(env.db, "Plans.pdf")
    assert c.post(_url(f"/items/{item['id']}/resolve")).status_code == 403
    assert c.post(_url("/dismiss")).status_code == 403
    assert c.post(_url("/drop"), files={"file": ("Plans.pdf", _pdf(), "application/pdf")}).status_code == 403
    assert item.get("resolved_at") is None
    # The external estimator: refused the read and the writes.
    env.who["user"] = "est"
    assert c.get(_url("/issue")).status_code == 403
    assert c.post(_url("/dismiss")).status_code == 403


def test_an_unassigned_estimator_is_refused_by_the_project_gate(env):
    est = _user("est")
    with pytest.raises(HTTPException) as exc:
        require_project_assignment(P1, user=est)
    assert exc.value.status_code == 403 and exc.value.detail == "Not assigned to this project"
    assert require_project_assignment(P1, user=_user("admin")).id == "u-admin"


# ── The read and the toggles ─────────────────────────────────────────────


def test_issue_route(env):
    c = env.client
    body = c.get(_url("/issue")).json()
    assert body["state"] == "open" and [i["group"] for i in body["items"]].count("refused") == 1
    assert c.get(_url("/issue", P2)).json() is None
    assert c.get(_url("/issue", "not-a-uuid")).status_code == 404


def test_resolve_and_reopen_routes(env):
    c = env.client
    item = _item(env.db, "Electrical/E-1.pdf")
    resp = c.post(_url(f"/items/{item['id']}/resolve"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["item"]["resolved"]["how"] == "manual"
    assert body["item"]["resolved"]["by"] == {"id": "u-admin", "name": "Admin"}
    assert body["issue"]["counts"] == {"open": 3, "resolved": 1, "not_downloaded": 3, "refused": 0}
    assert c.post(_url(f"/items/{item['id']}/resolve")).status_code == 404       # already resolved
    assert c.post(_url(f"/items/{item['id']}/resolve", P2)).status_code == 404   # not this project's
    assert c.post(_url(f"/items/{uuid.uuid4()}/resolve")).status_code == 404
    assert c.post(_url("/items/nope/resolve")).status_code == 404
    resp = c.delete(_url(f"/items/{item['id']}/resolve"))
    assert resp.status_code == 200 and resp.json()["item"]["resolved"] is None
    assert c.delete(_url(f"/items/{item['id']}/resolve")).status_code == 404     # not resolved
    assert [a[1] for a in env.audits] == ["rfp_files.resolve", "rfp_files.reopen"]


def test_dismiss_routes(env):
    c = env.client
    resp = c.post(_url("/dismiss"))
    assert resp.status_code == 200
    issue = resp.json()["issue"]
    assert issue["state"] == "dismissed" and issue["dismissed"]["by"]["id"] == "u-admin"
    # Nothing open is the only 409 (a second dismissal after new items re-stamps).
    resp = c.post(_url("/dismiss"))
    assert resp.status_code == 409 and resp.json()["detail"] == "Nothing is open to dismiss."
    resp = c.delete(_url("/dismiss"))
    assert resp.status_code == 200 and resp.json()["issue"]["state"] == "open"
    resp = c.delete(_url("/dismiss"))
    assert resp.status_code == 409 and resp.json()["detail"] == "The missing files are not dismissed."
    for row in env.db.tables["rfp_file_issues"]:
        row["resolved_at"] = "2026-10-09T00:00:00+00:00"
    resp = c.post(_url("/dismiss"))
    assert resp.status_code == 409 and resp.json()["detail"] == "Nothing is open to dismiss."
    assert c.post(_url("/dismiss", P2)).status_code == 404
    # A reopened item while dismissed: the issue is open, and a dismissal applies again.
    env.db.tables["rfp_file_issues"][0]["resolved_at"] = None
    c.post(_url("/dismiss"))
    env.db.tables["rfp_file_issues"][1]["resolved_at"] = None
    assert c.get(_url("/issue")).json()["state"] == "open"
    resp = c.post(_url("/dismiss"))
    assert resp.status_code == 200 and resp.json()["issue"]["state"] == "dismissed"


# ── The drop zone ────────────────────────────────────────────────────────


def _drop(env, name, content, job_id=None, project_id=P1):
    data = {"job_id": job_id} if job_id else {}
    return env.client.post(_url("/drop", project_id), files={"file": (name, content, "application/octet-stream")},
                           data=data)


def test_a_pdf_goes_to_the_splitter_on_a_project_job_and_clears_its_item_at_drop_time(env):
    resp = _drop(env, "E-1.pdf", _pdf(3))
    assert resp.status_code == 201
    body = resp.json()
    assert body["route"] == "splitter" and body["category"] is None and body["project_file_id"] is None
    item = _item(env.db, "Electrical/E-1.pdf")
    assert body["resolved"] == [item["id"]] and item["resolved_how"] == "name_match"
    [job] = env.db.tables["bid_split_jobs"]
    assert job["source"] == rfp_split.SOURCE_PROJECT and job["project_id"] == P1
    assert job["created_by"] == "u-admin" and job["status"] == "processing" and job["file_count"] == 1
    assert body["job_id"] == job["id"]
    [frow] = env.db.tables["bid_split_files"]
    assert frow["filename"] == "E-1.pdf" and frow["page_count"] == 3 and frow["status"] == "pending"
    assert body["split_file_id"] == frow["id"] and env.dispatched == [frow["id"]]
    assert env.db.tables["project_files"] == []
    assert body["issue"]["drop"]["job_id"] == job["id"] and body["issue"]["drop"]["files_total"] == 1
    drop_audit = next(a for a in env.audits if a[1] == "rfp_files.drop")
    assert drop_audit[4] == {"route": "splitter", "filename": "E-1.pdf", "job_id": job["id"],
                             "project_file_id": None, "resolved": [item["id"]]}
    # The batch's next PDF joins the same job.
    body = _drop(env, "Second.pdf", _pdf(), job_id=job["id"]).json()
    assert body["job_id"] == job["id"] and len(env.db.tables["bid_split_jobs"]) == 1
    assert env.db.tables["bid_split_jobs"][0]["file_count"] == 2


def test_job_id_reuse_rules(env):
    first = _drop(env, "One.pdf", _pdf()).json()["job_id"]
    jobs = env.db.tables["bid_split_jobs"]
    # Settled a minute ago: still joins.
    jobs[0].update(status="done", completed_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert _drop(env, "Two.pdf", _pdf(), job_id=first).json()["job_id"] == first
    # Settled past the window: a new job.
    jobs[0].update(status="done", completed_at=(datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat())
    second = _drop(env, "Three.pdf", _pdf(), job_id=first).json()["job_id"]
    assert second != first and len(jobs) == 2
    # Another project's job, a manual job, a junk id: never joined.
    jobs[1]["project_id"] = P2
    assert _drop(env, "Four.pdf", _pdf(), job_id=second).json()["job_id"] not in (first, second)
    jobs[0].update(status="processing", source="manual")
    assert _drop(env, "Five.pdf", _pdf(), job_id=first).json()["job_id"] != first
    assert _drop(env, "Six.pdf", _pdf(), job_id="junk").status_code == 201


@pytest.mark.parametrize("case, expect_category, why", [
    ("off", "drawing", "the Bid File Splitter is turned off on this server"),
    ("away", "drawing", "the Bid File Splitter cannot run right now because the AI model server is not responding"),
    ("sent", "additional", rfp_file_drop.WHY_PACKAGE_SENT),
])
def test_a_pdf_lands_whole_when_the_splitter_cannot_take_it(env, case, expect_category, why):
    if case == "off":
        env.state["splitter"] = (False, rfp_split.MSG_SPLIT_OFF)
    elif case == "away":
        env.state["splitter"] = (False, rfp_file_drop.MSG_SPLITTER_AWAY.format(
            why="the AI model server is not responding"))
    else:
        env.state["sent"] = True
    resp = _drop(env, "Plans.pdf", _pdf())
    assert resp.status_code == 201
    body = resp.json()
    assert body["route"] == "whole" and body["category"] == expect_category and body["job_id"] is None
    [row] = env.db.tables["project_files"]
    assert row["id"] == body["project_file_id"] and row["category"] == expect_category
    assert row["uploaded_by"] == "u-admin" and row["filename"] == "Plans.pdf"
    assert row["note"] == f"Added whole from the missing-files drop zone: {why}."
    assert row["mime_type"] == "application/pdf" and env.db.tables["bid_split_jobs"] == []
    item = _item(env.db, "Plans.pdf")
    assert body["resolved"] == [item["id"]] and item["resolved_how"] == "name_match"
    assert any(a[1] == "file.upload" and a[4]["source"] == "rfp_files_drop" for a in env.audits)


def test_a_pdf_over_the_page_cap_lands_whole_by_its_name(env):
    sb = env.db
    settings = SimpleNamespace(bid_split_max_pages_per_file=2, bid_split_max_files_per_job=250)
    out = rfp_file_drop.drop(sb, P1, _user(), "Project Manual Vol 2.pdf", _pdf(3), background=BackgroundTasks(),
                             settings=settings)
    assert out["route"] == "whole" and out["category"] == "specification"
    [row] = sb.table("project_files").select("*").execute().data
    assert row["note"] == ("Added whole from the missing-files drop zone: the PDF has 3 pages, over the "
                           "splitter's limit of 2 pages per file.")


def test_word_and_excel_land_whole(env):
    body = _drop(env, "Division 26 Specs.docx", DOCX).json()
    assert body["route"] == "whole" and body["category"] == "specification"
    body = _drop(env, "Bid Form.xls", XLS).json()
    assert body["route"] == "whole" and body["category"] == "other"
    notes = {r["note"] for r in env.db.tables["project_files"]}
    assert notes == {"Added from the missing-files drop zone. Check the category."}
    # After the hand-off: under Additional files, note and all.
    env.state["sent"] = True
    body = _drop(env, "Specs.docx", DOCX).json()
    assert body["category"] == "additional"


@pytest.mark.parametrize("name, content", [
    ("site.png", PNG),
    ("notes.txt", b"hello"),
    ("fake.pdf", b"MZ not a pdf"),
    ("renamed.docx", _pdf()),
    ("old.doc", DOCX),
])
def test_anything_else_is_415_before_anything_is_stored(env, name, content):
    resp = _drop(env, name, content)
    assert resp.status_code == 415
    assert resp.json()["detail"] == f"{name}: the drop zone accepts PDF, Word and Excel files"
    assert env.uploaded == [] and env.db.tables["bid_split_jobs"] == [] and env.db.tables["project_files"] == []


def test_a_pdf_that_will_not_open_is_415_and_resolves_nothing(env):
    resp = _drop(env, "Plans.pdf", b"%PDF-1.7 broken")
    assert resp.status_code == 415 and resp.json()["detail"].startswith("Plans.pdf: ")
    assert _item(env.db, "Plans.pdf").get("resolved_at") is None


def test_the_drop_needs_an_existing_project(env):
    assert _drop(env, "Plans.pdf", _pdf(), project_id=str(uuid.uuid4())).status_code == 404


def test_a_store_that_fails_reopens_the_up_front_match(env, monkeypatch):
    def boom(*a, **k):
        raise bid_split.UploadRefused("This job already holds 250 files.", 400)

    monkeypatch.setattr(bid_split, "stage_upload", boom)
    resp = _drop(env, "E-1.pdf", _pdf())
    assert resp.status_code == 400 and resp.json()["detail"] == "This job already holds 250 files."
    assert _item(env.db, "Electrical/E-1.pdf").get("resolved_at") is None
    # The job this request opened is gone again: no empty job reads processing forever.
    assert env.db.tables["bid_split_jobs"] == []


def test_a_failed_staging_keeps_a_job_the_batch_already_uses(env, monkeypatch):
    first = _drop(env, "One.pdf", _pdf()).json()["job_id"]

    def boom(*a, **k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(bid_split, "stage_upload", boom)
    with pytest.raises(RuntimeError):
        _drop(env, "Two.pdf", _pdf(), job_id=first)
    assert [j["id"] for j in env.db.tables["bid_split_jobs"]] == [first]


def test_a_whole_drop_answers_201_when_a_hook_after_the_insert_fails(env, monkeypatch):
    env.state["splitter"] = (False, rfp_split.MSG_SPLIT_OFF)

    def boom(*a, **k):
        raise RuntimeError("bell down")

    monkeypatch.setattr(files_router, "audit", boom)
    monkeypatch.setattr(files_router, "_notify_drawing_changed", boom)
    resp = _drop(env, "Plans.pdf", _pdf())
    assert resp.status_code == 201 and resp.json()["route"] == "whole"
    assert len(env.db.tables["project_files"]) == 1
    # The file is stored, so its item stays resolved.
    assert _item(env.db, "Plans.pdf")["resolved_how"] == "name_match"


def test_the_drop_validates_pdfs_with_the_splitters_own_check(env, monkeypatch):
    """Review finding 16: `bid_split.check_upload` itself, never a copy. Its
    page-cap refusal routes the PDF whole; its other refusals answer."""
    calls = []
    real = bid_split.check_upload

    def spy(name, content, settings=None):
        calls.append(name)
        return real(name, content, settings)

    monkeypatch.setattr(bid_split, "check_upload", spy)
    settings = SimpleNamespace(bid_split_max_pages_per_file=1, bid_split_max_files_per_job=250)
    out = rfp_file_drop.drop(env.db, P1, _user(), "Big Set.pdf", _pdf(2), background=BackgroundTasks(),
                             settings=settings)
    assert calls == ["Big Set.pdf"] and out["route"] == "whole" and out["category"] == "drawing"
    assert env.db.tables["project_files"][0]["note"] == (
        "Added whole from the missing-files drop zone: the PDF has 2 pages, over the splitter's limit "
        "of 1 pages per file.")
    with pytest.raises(rfp_file_drop.DropRefused) as exc:
        rfp_file_drop.drop(env.db, P1, _user(), "Broken.pdf", b"%PDF-1.7 broken", settings=settings)
    assert exc.value.status_code == 415 and calls[-1] == "Broken.pdf"


def test_a_whole_drop_runs_the_first_file_rule_on_links_only(env):
    env.db.tables["rfp_created_projects"][0].update(files_issue_total=True, files_promoted=0)
    env.state["splitter"] = (False, rfp_split.MSG_SPLIT_OFF)
    body = _drop(env, "Unrelated Set.pdf", _pdf()).json()
    link = next(r for r in env.db.tables["rfp_file_issues"] if r["kind"] == "link")
    assert body["category"] == "drawing" and body["resolved"] == [link["id"]]
    assert link["resolved_how"] == "auto_first_file"
    # The named files stay on the list a person is working from.
    assert all(r.get("resolved_at") is None for r in env.db.tables["rfp_file_issues"] if r["kind"] == "file")


# ── The pure pieces ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name, spec", [
    ("Specs.pdf", True),
    ("Vol1_Specifications.pdf", True),
    ("SpecBook.pdf", True),
    ("Project Manual.pdf", True),
    ("project_manual_v2.docx", True),
    ("Division 26.pdf", True),
    ("26 05 19 Low-Voltage Conductors.pdf", True),
    ("E-101 Lighting Plan.pdf", False),
    ("Inspection Report.pdf", False),
    ("2026-05-19 Plans.pdf", False),
    ("Bid Form.xlsx", False),
    (None, False),
])
def test_looks_like_spec(name, spec):
    assert rfp_file_drop.looks_like_spec(name) is spec


def test_route_for_is_the_routing_table():
    s = SimpleNamespace(bid_split_max_pages_per_file=600)
    pdf = b"%PDF-1.7"

    def route(name, magic, pages=10, **kw):
        kw.setdefault("splitter_available", True)
        kw.setdefault("package_sent", False)
        return rfp_file_drop.route_for(name, "application/pdf", magic, pages, s, **kw)

    assert route("E.pdf", pdf).kind == "splitter"
    assert route("E.pdf", pdf, pages=601) == rfp_file_drop.Route(
        "whole", "drawing", "Added whole from the missing-files drop zone: the PDF has 601 pages, over the "
        "splitter's limit of 600 pages per file.", "the PDF has 601 pages, over the splitter's limit of 600 pages per file")
    assert route("Specs.pdf", pdf, splitter_available=False).category == "specification"
    assert route("E.pdf", pdf, package_sent=True).category == "additional"
    assert route("Bid Form.xlsx", b"PK\x03\x04").category == "other"
    assert route("Spec Book.doc", bytes.fromhex("D0CF11E0A1B11AE1")).category == "specification"
    with pytest.raises(rfp_file_drop.DropRefused) as exc:
        route("x.png", PNG)
    assert exc.value.status_code == 415


def test_splitter_state(monkeypatch):
    on = SimpleNamespace(bid_file_splitter_enabled=True)
    assert rfp_file_drop.splitter_state(SimpleNamespace(bid_file_splitter_enabled=False)) == (
        False, "The Bid File Splitter is turned off on this server.")
    monkeypatch.setattr(rfp_file_drop.llm, "is_configured", lambda *a, **k: False)
    assert rfp_file_drop.splitter_state(on) == (
        False, "The Bid File Splitter cannot run right now because no AI model is set up for bid file "
               "splitting on this server.")
    monkeypatch.setattr(rfp_file_drop.llm, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(rfp_split, "model_away", lambda s=None: ("provider_down", "down"))
    assert rfp_file_drop.splitter_state(on) == (
        False, "The Bid File Splitter cannot run right now because the AI model server is not responding.")
    monkeypatch.setattr(rfp_split, "model_away", lambda s=None: None)
    assert rfp_file_drop.splitter_state(on) == (True, None)


def test_the_issue_offers_the_share_link_and_the_email(env):
    body = env.client.get(_url("/issue")).json()
    refused = next(i for i in body["items"] if i["group"] == "refused")
    assert refused["url"] == URL_A
    attachment = next(i for i in body["items"] if i["name"] == "Plans.pdf")
    assert attachment["email"]["subject"] == "ITB: Warehouse HVAC" and attachment["url"] is None
    assert HV in {r["harvest_id"] for r in env.db.tables["rfp_file_issues"]}
