"""PM documents router.

Covers the per-project guard (404 missing / 409 no PM life), category
validation on upload and the list filter, the cross-project download 404, the
upload cleanup-on-insert-failure path, and delete's best-effort storage
removal. The category tuple and object-path shape are pinned as pure pieces
(no UploadFile streaming through _read_capped beyond the happy path).

The Supabase client is faked with the tiny in-memory store from
test_reverify.py (select/insert/update/delete + eq/in_/single/order/limit).
"""

import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import pm_documents
from app.services import pm as pm_service
from app.services import storage

MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/0058_pm_documents.sql"


# ── Fake Supabase ─────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._single = False

    # builders
    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    # execution
    def _matches(self, row):
        return all(
            row.get(c) in v if isinstance(v, list) else row.get(c) == v
            for c, v in self._filters
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payloads:
                rows.append(dict(p))
            return SimpleNamespace(data=[dict(p) for p in payloads])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            hits = [r for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=[dict(r) for r in hits])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self, name)


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True)


def _pm_project(pid="p1", stage="precon"):
    return {"id": pid, "name": "Job", "pm_stage": stage, "pm_completed_at": None}


def _doc(doc_id="d1", pid="p1", category="contract"):
    return {
        "id": doc_id,
        "project_id": pid,
        "category": category,
        "storage_path": f"{pid}/pm/{category}/x-a.pdf",
        "filename": "a.pdf",
        "created_at": "2026-07-01T00:00:00Z",
    }


def _install(monkeypatch, db):
    """Point the router AND require_pm_project at the fake DB; capture audits."""
    audits = []
    monkeypatch.setattr(pm_documents, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_service, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_documents, "audit", lambda *a, **k: audits.append(a))
    return audits


# ── Pure pieces ───────────────────────────────────────────────────────────────


def test_category_tuple_matches_migration_enum():
    sql = MIGRATION.read_text()
    enum_body = re.search(r"pm_doc_category as enum \(([^)]*)\)", sql).group(1)
    assert tuple(re.findall(r"'(\w+)'", enum_body)) == pm_documents.PM_DOC_CATEGORIES


def test_object_path_lives_under_pm_namespace():
    path = storage.build_object_path("p1", "pm/contract", "site plan.pdf")
    project, pm, category, obj = path.split("/")
    assert (project, pm, category) == ("p1", "pm", "contract")
    assert obj.endswith("-site plan.pdf")


# ── require_pm_project guard ──────────────────────────────────────────────────


def test_missing_project_is_404(monkeypatch):
    _install(monkeypatch, FakeDB({"projects": []}))
    with pytest.raises(HTTPException) as ei:
        pm_documents.list_documents("p1", None, _user())
    assert ei.value.status_code == 404


def test_project_without_pm_life_is_409(monkeypatch):
    _install(monkeypatch, FakeDB({"projects": [_pm_project(stage=None)]}))
    with pytest.raises(HTTPException) as ei:
        pm_documents.list_documents("p1", None, _user())
    assert ei.value.status_code == 409


# ── Listing ───────────────────────────────────────────────────────────────────


def test_list_rejects_unknown_category_filter(monkeypatch):
    _install(monkeypatch, FakeDB({"projects": [_pm_project()]}))
    with pytest.raises(HTTPException) as ei:
        pm_documents.list_documents("p1", "blueprints", _user())
    assert ei.value.status_code == 400


def test_list_filters_by_category(monkeypatch):
    db = FakeDB({
        "projects": [_pm_project()],
        "pm_documents": [
            _doc("d1", category="contract"),
            _doc("d2", category="permit"),
            _doc("d3", pid="p2", category="contract"),  # other project — never leaks
        ],
    })
    _install(monkeypatch, db)
    assert {r["id"] for r in pm_documents.list_documents("p1", None, _user())} == {"d1", "d2"}
    only = pm_documents.list_documents("p1", "permit", _user())
    assert [r["id"] for r in only] == ["d2"]


# ── Upload ────────────────────────────────────────────────────────────────────


async def test_upload_rejects_unknown_category(monkeypatch):
    _install(monkeypatch, FakeDB({"projects": [_pm_project()]}))
    up = UploadFile(filename="a.pdf", file=io.BytesIO(b"x"))
    with pytest.raises(HTTPException) as ei:
        await pm_documents.upload_document("p1", "blueprints", None, up, _user())
    assert ei.value.status_code == 400


async def test_upload_stores_object_then_row(monkeypatch):
    db = FakeDB({"projects": [_pm_project()], "pm_documents": []})
    audits = _install(monkeypatch, db)
    uploaded = {}
    monkeypatch.setattr(
        storage, "upload_file",
        lambda path, content, mime, **k: uploaded.update(path=path, mime=mime),
    )
    up = UploadFile(filename="contract.pdf", file=io.BytesIO(b"pdfbytes"))
    row = await pm_documents.upload_document("p1", "contract", "  signed copy  ", up, _user())
    assert uploaded["path"].startswith("p1/pm/contract/")
    assert uploaded["mime"] == "application/pdf"
    assert row["storage_path"] == uploaded["path"]
    assert row["size_bytes"] == 8
    assert row["note"] == "signed copy"
    assert db.tables["pm_documents"][0]["uploaded_by"] == "u1"
    assert audits and audits[0][1] == "pm_doc.upload"


async def test_upload_row_failure_cleans_up_object(monkeypatch):
    class _ExplodingInsertDB(FakeDB):
        def table(self, name):
            q = super().table(name)
            if name == "pm_documents":
                orig = q.execute

                def boom():
                    if q._op == "insert":
                        raise RuntimeError("insert failed")
                    return orig()

                q.execute = boom
            return q

    db = _ExplodingInsertDB({"projects": [_pm_project()]})
    _install(monkeypatch, db)
    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    deleted = []
    monkeypatch.setattr(storage, "delete_file", lambda path: deleted.append(path))
    up = UploadFile(filename="a.pdf", file=io.BytesIO(b"x"))
    with pytest.raises(RuntimeError):
        await pm_documents.upload_document("p1", "contract", None, up, _user())
    assert len(deleted) == 1 and deleted[0].startswith("p1/pm/contract/")


# ── Download ──────────────────────────────────────────────────────────────────


def test_download_cross_project_doc_is_404(monkeypatch):
    db = FakeDB({
        "projects": [_pm_project("p1"), _pm_project("p2")],
        "pm_documents": [_doc("d1", pid="p2")],
    })
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as ei:
        pm_documents.download_document("p1", "d1", _user())
    assert ei.value.status_code == 404


def test_download_returns_signed_url_as_attachment(monkeypatch):
    db = FakeDB({"projects": [_pm_project()], "pm_documents": [_doc("d1")]})
    audits = _install(monkeypatch, db)
    calls = {}
    monkeypatch.setattr(
        storage, "signed_url",
        lambda path, **k: calls.update(path=path, **k) or "https://signed",
    )
    out = pm_documents.download_document("p1", "d1", _user(role=Role.ACCOUNTANT))
    assert out == {"url": "https://signed", "filename": "a.pdf"}
    assert calls["download"] == "a.pdf"  # attachment disposition, named
    assert audits and audits[0][1] == "pm_doc.download"


# ── Delete ────────────────────────────────────────────────────────────────────


def test_delete_removes_row_even_when_storage_fails(monkeypatch):
    db = FakeDB({"projects": [_pm_project()], "pm_documents": [_doc("d1")]})
    audits = _install(monkeypatch, db)

    def _explode(path):
        raise RuntimeError("object already gone")

    monkeypatch.setattr(storage, "delete_file", _explode)
    pm_documents.delete_document("p1", "d1", _user())
    assert db.tables["pm_documents"] == []
    assert audits and audits[0][1] == "pm_doc.delete"


def test_delete_unknown_doc_is_404(monkeypatch):
    _install(monkeypatch, FakeDB({"projects": [_pm_project()], "pm_documents": []}))
    with pytest.raises(HTTPException) as ei:
        pm_documents.delete_document("p1", "d1", _user())
    assert ei.value.status_code == 404
