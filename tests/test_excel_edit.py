"""Edit in Excel (docs/EXCEL_EDIT.md, 0193): app/services/excel_edit.py.

An in-memory Supabase covering the builder surface the service uses, a fake
OneDrive, and a fake storage bucket. Covers eligibility, start and join, the
version rules (one session = one version, the original kept as version 1),
idle and explicit close, a deleted file, a non-workbook download and the
per-session lease."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from postgrest.exceptions import APIError

from app.services import excel_edit

UTC = timezone.utc


# ── fakes ────────────────────────────────────────────────────────────────────


def _ts(v):
    if isinstance(v, str) and len(v) >= 19 and v[4] == "-":
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters = db, table, []
        self.op, self.payload, self._order, self._limit = "select", None, None, None

    def select(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def or_(self, expr):
        terms = expr.split(",")

        def match(r):
            for t in terms:
                col, op, val = t.split(".", 2)
                have = r.get(col)
                if op == "is" and val == "null" and have is None:
                    return True
                if op == "lt" and have is not None and _ts(have) < _ts(val):
                    return True
            return False

        self.filters.append(match)
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            row = {"id": str(uuid.uuid4()), "created_at": datetime.now(UTC).isoformat(), **self.payload}
            row.setdefault("superseded_paths", []) if self.table == "file_edit_sessions" else None
            row.setdefault("status", "open") if self.table == "file_edit_sessions" else None
            if self.table == "file_edit_sessions" and any(
                r["file_id"] == row["file_id"] and r["status"] == "open" for r in rows
            ):
                raise APIError({"code": "23505", "message": "duplicate"})
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in hit])
        if self._order:
            col, desc = self._order
            hit = sorted(hit, key=lambda r: r.get(col), reverse=desc)
        if self._limit is not None:
            hit = hit[: self._limit]
        return SimpleNamespace(data=[dict(r) for r in hit])


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def table(self, name):
        return _Query(self, name)


class FakeDrive:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.grants: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.refuse_grant = False

    def upload(self, path, content):
        item_id = f"item-{len(self.items) + 1}"
        self.items[item_id] = {
            "id": item_id, "webUrl": f"https://excel.example/{item_id}", "cTag": "c1",
            "size": len(content), "content": content,
            "lastModifiedDateTime": datetime.now(UTC).isoformat(), "lastModifiedBy": {},
        }
        return item_id

    def edit(self, item_id, content, who="Jane Doe", at=None):
        item = self.items[item_id]
        item["content"] = content
        item["size"] = len(content)
        item["cTag"] = f"c{int(item['cTag'][1:]) + 1}"
        item["lastModifiedDateTime"] = (at or datetime.now(UTC)).isoformat()
        item["lastModifiedBy"] = {"user": {"displayName": who}}


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload_file(self, path, content, _mime, upsert=False):
        self.objects[path] = content

    def download_file(self, path):
        return self.objects[path]

    def delete_file(self, path):
        self.deleted.append(path)
        self.objects.pop(path, None)

    def build_object_path(self, project_id, category, filename):
        return f"{project_id}/{category}/{uuid.uuid4().hex}-{filename}"


@pytest.fixture
def env(monkeypatch):
    db, drive, store = FakeDB(), FakeDrive(), FakeStorage()
    previews: list[tuple[str, bool]] = []
    audits: list[str] = []

    monkeypatch.setattr(excel_edit, "get_supabase", lambda: db)
    monkeypatch.setattr(excel_edit, "storage", store)
    monkeypatch.setattr(excel_edit, "audit", lambda _u, action, *_a, **_k: audits.append(action))
    monkeypatch.setattr(
        excel_edit.office_preview, "generate_preview",
        lambda fid, fresh_path=False: previews.append((fid, fresh_path)),
    )
    monkeypatch.setattr(excel_edit.office_preview, "is_convertible", lambda name, cat: True)

    def drive_item(item_id):
        item = drive.items.get(item_id)
        return None if item is None else {k: v for k, v in item.items() if k != "content"}

    def grant(item_id, email):
        if drive.refuse_grant:
            import httpx

            req = httpx.Request("POST", "https://graph")
            raise httpx.HTTPStatusError("no", request=req, response=httpx.Response(400, request=req))
        drive.grants.append((item_id, email))

    def delete(item_id):
        drive.deleted.append(item_id)
        drive.items.pop(item_id, None)

    monkeypatch.setattr(excel_edit, "_drive_item", drive_item)
    monkeypatch.setattr(excel_edit, "_drive_content", lambda item_id: drive.items[item_id]["content"])
    monkeypatch.setattr(excel_edit, "_drive_delete", delete)
    monkeypatch.setattr(excel_edit, "_drive_grant", grant)
    from app.services import graph_email

    monkeypatch.setattr(graph_email, "drive_upload", drive.upload)

    settings = SimpleNamespace(
        excel_edit_enabled=True, ms_client_id="x", ms_drive_owner="owner@g3", ms_sender="bids@g3",
        excel_edit_drive_folder="BDR/excel-edit", excel_edit_idle_minutes=60,
        excel_edit_poll_seconds=30, upload_max_bytes=100 * 1024 * 1024,
    )
    monkeypatch.setattr(excel_edit, "get_settings", lambda: settings)

    store.objects["p1/boq/orig-book.xlsx"] = b"PK\x03\x04original"
    db.tables["project_files"] = [{
        "id": "f1", "project_id": "p1", "category": "boq", "filename": "book.xlsx",
        "storage_path": "p1/boq/orig-book.xlsx", "size_bytes": 12, "sha256": "aaa",
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "uploaded_by": "estimator-1", "created_at": "2026-10-01T00:00:00+00:00",
        "preview_status": "ready",
    }]
    db.tables["profiles"] = [{"id": "u1", "full_name": "Pat Writer"}, {"id": "u2", "full_name": "Sam Two"}]
    return SimpleNamespace(db=db, drive=drive, store=store, previews=previews, audits=audits, settings=settings)


U1 = SimpleNamespace(id="u1", email="pat@g3electrical.com")
U2 = SimpleNamespace(id="u2", email="sam@g3electrical.com")


def _file(env):
    return env.db.tables["project_files"][0]


def _versions(env):
    return sorted(env.db.tables.get("project_file_versions", []), key=lambda v: v["version_no"])


# ── eligibility ──────────────────────────────────────────────────────────────


def test_block_reason_rules(env):
    rec = _file(env)
    assert excel_edit.block_reason(rec, writer=True, locked=False, sent=False) is None
    assert "team members" in excel_edit.block_reason(rec, writer=False, locked=False, sent=False)
    assert ".xlsx" in excel_edit.block_reason({**rec, "filename": "a.pdf"}, writer=True, locked=False, sent=False)
    assert ".xlsx" in excel_edit.block_reason({**rec, "filename": "a.xls"}, writer=True, locked=False, sent=False)
    assert excel_edit.block_reason({**rec, "filename": "A.XLSM"}, writer=True, locked=False, sent=False) is None
    assert "revision" in excel_edit.block_reason(rec, writer=True, locked=True, sent=False)
    assert "already sent" in excel_edit.block_reason(rec, writer=True, locked=False, sent=True)
    assert "Proposals" in excel_edit.block_reason({**rec, "category": "proposal"}, writer=True, locked=False, sent=False)
    env.settings.excel_edit_enabled = False
    assert "turned off" in excel_edit.block_reason(rec, writer=True, locked=False, sent=False)


# ── start / join ─────────────────────────────────────────────────────────────


def test_start_copies_grants_and_second_user_joins(env):
    row = excel_edit.start(_file(env), U1)
    assert row["edit_url"] == "https://excel.example/item-1"
    assert row["drive_path"].startswith("BDR/excel-edit/") and row["drive_path"].endswith("/book.xlsx")
    assert env.drive.items["item-1"]["content"] == b"PK\x03\x04original"
    assert env.drive.grants == [("item-1", "pat@g3electrical.com")]

    again = excel_edit.start(_file(env), U2)
    assert again["id"] == row["id"]
    assert len(env.drive.items) == 1  # joined, no second copy
    assert ("item-1", "sam@g3electrical.com") in env.drive.grants
    # Joining twice never re-grants.
    excel_edit.start(_file(env), U2)
    assert env.drive.grants.count(("item-1", "sam@g3electrical.com")) == 1
    assert env.audits.count("file.excel_edit_started") == 1


def test_start_refused_share_removes_the_copy(env):
    env.drive.refuse_grant = True
    with pytest.raises(excel_edit.ExcelEditError) as exc:
        excel_edit.start(_file(env), U1)
    assert "G3 Microsoft 365" in str(exc.value)
    assert env.drive.items == {} and env.drive.deleted == ["item-1"]
    assert not env.db.tables.get("file_edit_sessions")


# ── sync and versions ────────────────────────────────────────────────────────


def test_session_writes_one_version_and_keeps_the_original(env):
    row = excel_edit.start(_file(env), U1)
    original_path = _file(env)["storage_path"]

    # Nothing changed: nothing written.
    assert excel_edit.tick_session(row["id"]).changed is False
    assert not _versions(env)

    env.drive.edit("item-1", b"PK\x03\x04edit-one")
    assert excel_edit.tick_session(row["id"]).changed is True
    v = _versions(env)
    assert [(x["version_no"], x["source"]) for x in v] == [(1, "original"), (2, "excel_edit")]
    assert v[0]["storage_path"] == original_path and v[0]["created_by"] == "estimator-1"
    f = _file(env)
    assert f["storage_path"] == v[1]["storage_path"] != original_path
    assert env.store.objects[f["storage_path"]] == b"PK\x03\x04edit-one"
    assert f["preview_status"] == "pending" and env.previews == [("f1", True)]
    first_edit_path = f["storage_path"]

    # A later autosave in the same session updates version 2, never adds v3.
    env.drive.edit("item-1", b"PK\x03\x04edit-two", who="Sam Two")
    assert excel_edit.tick_session(row["id"]).changed is True
    v = _versions(env)
    assert [x["version_no"] for x in v] == [1, 2]
    assert v[1]["last_modified_by"] == "Sam Two"
    assert env.store.objects[_file(env)["storage_path"]] == b"PK\x03\x04edit-two"
    # The superseded session object survives until the session closes.
    assert first_edit_path in env.store.objects
    assert original_path in env.store.objects

    result = excel_edit.finish(excel_edit.open_session("f1"), U1)
    assert result.closed
    assert first_edit_path not in env.store.objects
    assert original_path in env.store.objects  # v1 kept forever
    assert env.drive.deleted == ["item-1"]
    assert excel_edit.open_session("f1") is None

    # The next session is the next version.
    row2 = excel_edit.start(_file(env), U2)
    env.drive.edit(row2["drive_item_id"], b"PK\x03\x04edit-three")
    excel_edit.tick_session(row2["id"])
    assert [x["version_no"] for x in _versions(env)] == [1, 2, 3]
    assert excel_edit.current_version_no("f1") == 3
    listed = excel_edit.list_versions(_file(env))
    assert [x["version_no"] for x in listed] == [3, 2, 1]
    assert [x["current"] for x in listed] == [True, False, False]


def test_same_bytes_new_ctag_writes_nothing(env):
    row = excel_edit.start(_file(env), U1)
    env.drive.edit("item-1", b"PK\x03\x04edit")
    excel_edit.tick_session(row["id"])
    env.drive.edit("item-1", b"PK\x03\x04edit")  # Excel touched it, same bytes
    assert excel_edit.tick_session(row["id"]).changed is False
    assert len(env.previews) == 1


def test_non_workbook_download_is_refused_and_recorded(env):
    row = excel_edit.start(_file(env), U1)
    env.drive.edit("item-1", b"<html>sign in</html>")
    assert excel_edit.tick_session(row["id"]) is None
    assert _file(env)["storage_path"] == "p1/boq/orig-book.xlsx"
    session = excel_edit.open_session("f1")
    assert "not a workbook" in session["last_error"]
    assert session["lease_until"] is None


def test_idle_session_closes(env):
    row = excel_edit.start(_file(env), U1)
    env.drive.edit("item-1", b"PK\x03\x04x", at=datetime.now(UTC) - timedelta(minutes=61))
    result = excel_edit.tick_session(row["id"])
    assert result.changed and result.closed
    closed = env.db.tables["file_edit_sessions"][0]
    assert closed["status"] == "closed" and closed["close_reason"] == "idle"
    assert env.drive.deleted == ["item-1"]


def test_deleted_file_closes_the_session(env):
    row = excel_edit.start(_file(env), U1)
    env.db.tables["file_edit_sessions"][0]["file_id"] = None  # on delete set null
    assert excel_edit.tick_session(row["id"]).closed
    assert env.db.tables["file_edit_sessions"][0]["close_reason"] == "file_deleted"
    assert env.drive.deleted == ["item-1"]


def test_lease_keeps_a_second_worker_off(env):
    row = excel_edit.start(_file(env), U1)
    assert excel_edit._claim(row["id"])
    assert excel_edit.tick_session(row["id"]) is None  # held elsewhere
    excel_edit._release(row["id"])
    assert excel_edit.tick_session(row["id"]) is not None


def test_session_out_shape(env):
    row = excel_edit.start(_file(env), U1)
    out = excel_edit.session_out(excel_edit.open_session("f1"))
    assert out["started_by_name"] == "Pat Writer"
    assert out["version_no"] is None and out["edit_url"] == row["edit_url"]
    assert out["idle_close_at"] is not None


# ── routes (called directly, like the other router unit tests) ───────────────


@pytest.fixture
def routes(env, monkeypatch):
    from app.routers import file_edit

    monkeypatch.setattr(file_edit, "_get_file_checked", lambda _p, _f, _u: dict(_file(env)))
    monkeypatch.setattr(file_edit, "_record_access", lambda *a, **k: None)
    monkeypatch.setattr(file_edit, "handoff_locked", lambda _p: False)
    return file_edit


def _user(role, uid="u1", email="pat@g3electrical.com"):
    from app.core.roles import Role

    return SimpleNamespace(id=uid, email=email, role=Role(role))


def test_routes_role_gates(routes):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        routes.excel_edit_status("p1", "f1", _user("estimator"))
    assert exc.value.status_code == 403

    status = routes.excel_edit_status("p1", "f1", _user("accountant"))
    assert status["editable"] is False and "team members" in status["reason"]
    with pytest.raises(HTTPException) as exc:
        routes.excel_edit_start("p1", "f1", _user("accountant"))
    assert exc.value.status_code == 403


def test_routes_start_status_finish_versions(env, routes):
    writer = _user("executive")
    started = routes.excel_edit_start("p1", "f1", writer)
    assert started["edit_url"] == "https://excel.example/item-1"
    assert started["session"]["started_by_me"] and started["session"]["granted_to_me"]

    other = _user("executive", uid="u2", email="sam@g3electrical.com")
    seen = routes.excel_edit_status("p1", "f1", other)["session"]
    assert seen["started_by_me"] is False and seen["granted_to_me"] is False

    env.drive.edit("item-1", b"PK\x03\x04new")
    done = routes.excel_edit_finish("p1", "f1", writer)
    assert done["changed"] and done["version_no"] == 2 and done["session"]["status"] == "closed"
    status = routes.excel_edit_status("p1", "f1", writer)
    assert status["session"] is None and status["current_version_no"] == 2

    versions = routes.file_versions("p1", "f1", _user("accountant"))["versions"]
    assert [v["version_no"] for v in versions] == [2, 1]


def test_routes_feature_off_is_503_and_locked_is_409(env, routes, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(routes, "handoff_locked", lambda _p: True)
    env.db.tables["project_files"][0]["category"] = "specification"
    with pytest.raises(HTTPException) as exc:
        routes.excel_edit_start("p1", "f1", _user("executive"))
    assert exc.value.status_code == 409
    env.settings.excel_edit_enabled = False
    with pytest.raises(HTTPException) as exc:
        routes.excel_edit_start("p1", "f1", _user("executive"))
    assert exc.value.status_code == 503
