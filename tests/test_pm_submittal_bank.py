"""Project ↔ Submittal Bank links (migration 0074).

Covers the service that backs the "fill from the bank" section: pull links every
fuzzy match that brings a NEW file, up to a per-material cap (skipping
already-linked / no-match / fileless ones, and siblings that share a cut-sheet),
upload archives a pm_documents row + link and rejects non-PDF at the
router, add-to-bank creates a bank material + file + M:N link and records
bank_material_id, delete cleans up an uploaded doc/object, and every id is
project-scoped (a link/material from another project is a 404).

Supabase is faked with a tiny in-memory store (the pattern used across the PM
tests), extended with `.rpc` and PostgREST-embed resolution for the one nested
select the resolver uses. Storage + the alias LLM are monkeypatched to no-ops.
"""

import io
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import PmAddToBankIn, PmBankPullIn
from app.routers import pm_submittals
from app.services import pm as pm_service
from app.services import pm_submittal_bank as bank
from app.services import storage
from pydantic import ValidationError

MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/0074_project_submittal_bank_links.sql"


# ── Fake Supabase (auto-id insert, .rpc, submittal_files embed) ───────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._single = False
        self._sel = "*"

    def select(self, sel="*", *a, **k):
        self._op, self._sel = "select", sel
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

    def _matches(self, row):
        return all(
            row.get(c) in v if isinstance(v, list) else row.get(c) == v
            for c, v in self._filters
        )

    def _embed(self, row):
        # Resolve `submittal_files(...)` embeds the way PostgREST would: attach
        # the submittal_files row referenced by this link's file_id.
        if "submittal_files(" in self._sel and self.table == "submittal_material_files":
            row = dict(row)
            files = self.db.tables.get("submittal_files", [])
            match = next((f for f in files if f["id"] == row.get("file_id")), None)
            row["submittal_files"] = dict(match) if match else None
        return row

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [self._embed(r) for r in rows if self._matches(r)]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", uuid.uuid4().hex)
                row.setdefault("created_at", "2026-07-22T00:00:00Z")
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
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
        self.rpc_rows = []  # what search_submittals returns, in rank order

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        assert name == "search_submittals"
        rows = [dict(r) for r in self.rpc_rows]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=rows))


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True)


def _install(monkeypatch, db):
    audits = []
    monkeypatch.setattr(bank, "get_supabase", lambda: db)
    monkeypatch.setattr(bank, "audit", lambda *a, **k: audits.append(a))
    # Storage is inert; add-to-bank downloads by returning PDF-ish bytes.
    monkeypatch.setattr(storage, "build_object_path", lambda pid, cat, fn: f"{pid}/{cat}/x-{fn}")
    monkeypatch.setattr(storage, "build_submittal_object_path", lambda fn: f"submittal-bank/x-{fn}")
    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(storage, "download_file", lambda path: b"%PDF-fake")
    deleted = []
    monkeypatch.setattr(storage, "delete_file", lambda path: deleted.append(path))
    monkeypatch.setattr(bank.openai_text, "alt_material_names", lambda *a, **k: ["alt"])
    return audits, deleted


def _mat(mid="m1", pid="p1", desc="EMT Conduit 3/4in"):
    return {"id": mid, "project_id": pid, "description": desc}


# ── Schemas ───────────────────────────────────────────────────────────────────


def test_pull_schema_requires_ids():
    with pytest.raises(ValidationError):
        PmBankPullIn(material_ids=[])
    assert PmBankPullIn(material_ids=["m1"]).material_ids == ["m1"]


def test_add_to_bank_defaults():
    body = PmAddToBankIn()
    assert body.category == "general_material"
    assert body.name is None and body.generate_aliases is True
    # Unset fields are excluded so the service can fall back to the description.
    assert body.model_dump(exclude_unset=True) == {}


# ── Migration parity ──────────────────────────────────────────────────────────


def test_migration_declares_table_and_sources():
    sql = MIGRATION.read_text()
    assert "create table pm_material_submittals" in sql
    assert "source in ('bank', 'uploaded')" in sql
    assert "enable row level security" in sql and "force  row level security" in sql


# ── Pull ──────────────────────────────────────────────────────────────────────


def test_pull_links_every_file_bearing_match(monkeypatch):
    """One product needs several submittals, so a pull links ALL file-bearing
    matches in rank order — only the fileless one is skipped."""
    db = FakeDB({
        "pm_materials": [_mat("m1")],
        "submittal_materials": [
            {"id": "s1", "name": "EMT 3/4"},
            {"id": "s2", "name": "EMT 1"},
            {"id": "s3", "name": "EMT 3/4 alt vendor"},
        ],
        "submittal_files": [{"id": "f1", "file_name": "emt.pdf"}, {"id": "f2", "file_name": "emt-alt.pdf"}],
        # s1 has no file → nothing to preview, so it's the only one skipped.
        "submittal_material_files": [
            {"material_id": "s2", "file_id": "f1"},
            {"material_id": "s3", "file_id": "f2"},
        ],
    })
    db.rpc_rows = [
        {"id": "s1", "name": "EMT 3/4"},
        {"id": "s2", "name": "EMT 1"},
        {"id": "s3", "name": "EMT 3/4 alt vendor"},
    ]
    audits, _ = _install(monkeypatch, db)

    out = bank.pull("p1", ["m1"], "u1")
    assert [link["submittal_material_id"] for link in out] == ["s2", "s3"]
    assert all(link["source"] == "bank" for link in out)
    assert out[0]["files"] == [{"file_id": "f1", "file_name": "emt.pdf"}]
    assert {r["submittal_material_id"] for r in db.tables["pm_material_submittals"]} == {"s2", "s3"}


def test_pull_skips_matches_sharing_a_file(monkeypatch):
    """A group cut-sheet is reachable from every size/color it covers — linking
    each of those matches would attach the SAME PDF repeatedly, so a match that
    brings no new file is dropped."""
    db = FakeDB({
        "pm_materials": [_mat("m1")],
        "submittal_materials": [
            {"id": "s1", "name": "EMT 3/4"},
            {"id": "s2", "name": "EMT 1"},      # same shared cut-sheet as s1
            {"id": "s3", "name": "EMT vendor B"},
        ],
        "submittal_files": [{"id": "f1", "file_name": "emt.pdf"}, {"id": "f2", "file_name": "emt-b.pdf"}],
        "submittal_material_files": [
            {"material_id": "s1", "file_id": "f1"},
            {"material_id": "s2", "file_id": "f1"},
            {"material_id": "s3", "file_id": "f2"},
        ],
    })
    db.rpc_rows = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
    _install(monkeypatch, db)

    out = bank.pull("p1", ["m1"], "u1")
    assert [link["submittal_material_id"] for link in out] == ["s1", "s3"]


def test_pull_skips_file_already_linked_from_a_sibling(monkeypatch):
    """The same dedup across pulls: the material already previews f1 through s1,
    so s2 (a different bank item, same PDF) adds nothing."""
    db = FakeDB({
        "pm_materials": [_mat("m1")],
        "submittal_materials": [{"id": "s1", "name": "EMT 3/4"}, {"id": "s2", "name": "EMT 1"}],
        "submittal_files": [{"id": "f1", "file_name": "emt.pdf"}],
        "submittal_material_files": [
            {"material_id": "s1", "file_id": "f1"},
            {"material_id": "s2", "file_id": "f1"},
        ],
        "pm_material_submittals": [
            {"id": "L1", "project_id": "p1", "pm_material_id": "m1", "source": "bank",
             "submittal_material_id": "s1", "created_at": "2026-07-01T00:00:00Z"},
        ],
    })
    db.rpc_rows = [{"id": "s1"}, {"id": "s2"}]
    _install(monkeypatch, db)
    assert bank.pull("p1", ["m1"], "u1") == []


def test_pull_caps_links_per_material(monkeypatch):
    """A vague description that matches half the bank can't bury the page: the
    per-material cap counts links the material already carries."""
    n = bank._MAX_LINKS_PER_MATERIAL + 5
    db = FakeDB({
        "pm_materials": [_mat("m1")],
        "submittal_materials": [{"id": f"s{i}", "name": f"EMT {i}"} for i in range(n)],
        "submittal_files": [{"id": f"f{i}", "file_name": f"emt-{i}.pdf"} for i in range(n)],
        "submittal_material_files": [{"material_id": f"s{i}", "file_id": f"f{i}"} for i in range(n)],
        "pm_material_submittals": [
            {"id": "L1", "project_id": "p1", "pm_material_id": "m1", "source": "uploaded",
             "document_id": "d1", "created_at": "2026-07-01T00:00:00Z"},
        ],
    })
    db.rpc_rows = [{"id": f"s{i}", "name": f"EMT {i}"} for i in range(n)]
    _install(monkeypatch, db)

    out = bank.pull("p1", ["m1"], "u1")
    # One slot is already taken by the uploaded PDF.
    assert len(out) == bank._MAX_LINKS_PER_MATERIAL - 1
    assert [link["submittal_material_id"] for link in out] == [f"s{i}" for i in range(len(out))]


def test_pull_skips_already_linked_bank_item(monkeypatch):
    """Re-pulling adds only bank items the material doesn't already carry."""
    db = FakeDB({
        "pm_materials": [_mat("m1")],
        "submittal_materials": [{"id": "s1", "name": "EMT"}, {"id": "s2", "name": "EMT alt"}],
        "submittal_files": [{"id": "f1", "file_name": "emt.pdf"}, {"id": "f2", "file_name": "alt.pdf"}],
        "submittal_material_files": [
            {"material_id": "s1", "file_id": "f1"},
            {"material_id": "s2", "file_id": "f2"},
        ],
        "pm_material_submittals": [
            {"id": "L1", "project_id": "p1", "pm_material_id": "m1", "source": "bank",
             "submittal_material_id": "s1", "created_at": "2026-07-01T00:00:00Z"},
        ],
    })
    db.rpc_rows = [{"id": "s1", "name": "EMT"}, {"id": "s2", "name": "EMT alt"}]
    _install(monkeypatch, db)
    out = bank.pull("p1", ["m1"], "u1")
    assert [link["submittal_material_id"] for link in out] == ["s2"]


def test_pull_skips_material_with_no_match(monkeypatch):
    db = FakeDB({"pm_materials": [_mat("m2", desc="Mystery Widget")]})
    db.rpc_rows = []  # nothing matches
    _install(monkeypatch, db)
    assert bank.pull("p1", ["m2"], "u1") == []


def test_pull_skips_match_without_file(monkeypatch):
    db = FakeDB({
        "pm_materials": [_mat("m1")],
        "submittal_materials": [{"id": "s1", "name": "EMT"}],
        "submittal_files": [],
        "submittal_material_files": [],  # s1 has no file → nothing to preview
    })
    db.rpc_rows = [{"id": "s1", "name": "EMT"}]
    _install(monkeypatch, db)
    assert bank.pull("p1", ["m1"], "u1") == []
    assert db.tables.get("pm_material_submittals", []) == []


# ── Upload ────────────────────────────────────────────────────────────────────


def test_upload_archives_document_and_links(monkeypatch):
    db = FakeDB({"pm_materials": [_mat("m1")]})
    _install(monkeypatch, db)
    link = bank.upload("p1", "m1", "cut sheet.pdf", b"%PDF-xyz", "u1")

    assert link["source"] == "uploaded" and link["in_bank"] is False
    assert link["name"] == "cut sheet.pdf"
    doc = db.tables["pm_documents"][0]
    assert doc["category"] == "submittal" and doc["project_id"] == "p1"
    assert link["document_key"] == f"pm:{doc['id']}"
    assert db.tables["pm_material_submittals"][0]["document_id"] == doc["id"]


def test_upload_rejects_material_from_other_project(monkeypatch):
    db = FakeDB({"pm_materials": [_mat("m1", pid="p2")]})
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as ei:
        bank.upload("p1", "m1", "a.pdf", b"%PDF-", "u1")
    assert ei.value.status_code == 404


# ── Add to bank ───────────────────────────────────────────────────────────────


def _uploaded_link_db():
    return FakeDB({
        "pm_materials": [_mat("m1", desc="EMT Conduit")],
        "pm_documents": [{"id": "d1", "project_id": "p1", "storage_path": "p1/pm/submittal/x-a.pdf",
                          "filename": "a.pdf"}],
        "pm_material_submittals": [
            {"id": "L1", "project_id": "p1", "pm_material_id": "m1", "source": "uploaded",
             "document_id": "d1", "bank_material_id": None, "created_at": "2026-07-01T00:00:00Z"},
        ],
    })


def test_add_to_bank_creates_material_file_and_records_link(monkeypatch):
    db = _uploaded_link_db()
    _install(monkeypatch, db)
    out = bank.add_to_bank("p1", "L1", PmAddToBankIn().model_dump(exclude_unset=True), "u1")

    mat = db.tables["submittal_materials"][0]
    assert mat["name"] == "EMT Conduit"  # defaulted from the material description
    assert mat["category"] == "general_material" and mat["aliases"] == ["alt"]
    frow = db.tables["submittal_files"][0]
    assert frow["file_name"] == "a.pdf" and frow["file_path"].startswith("submittal-bank/")
    linkrow = db.tables["submittal_material_files"][0]
    assert linkrow["material_id"] == mat["id"] and linkrow["file_id"] == frow["id"]
    assert db.tables["pm_material_submittals"][0]["bank_material_id"] == mat["id"]
    assert out["in_bank"] is True


def test_add_to_bank_rejects_already_in_bank(monkeypatch):
    db = _uploaded_link_db()
    db.tables["pm_material_submittals"][0]["bank_material_id"] = "existing"
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as ei:
        bank.add_to_bank("p1", "L1", {}, "u1")
    assert ei.value.status_code == 400


def test_add_to_bank_404_for_other_project(monkeypatch):
    db = _uploaded_link_db()
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as ei:
        bank.add_to_bank("pOTHER", "L1", {}, "u1")
    assert ei.value.status_code == 404


# ── Delete / unlink ───────────────────────────────────────────────────────────


def test_delete_uploaded_removes_document_and_object(monkeypatch):
    db = _uploaded_link_db()
    _, deleted = _install(monkeypatch, db)
    bank.delete_link("p1", "L1", "u1")
    # Deleting the pm_documents row cascades the link in Postgres; the fake has no
    # cascade, so assert the document row is gone and the object was cleaned up.
    assert db.tables["pm_documents"] == []
    assert deleted == ["p1/pm/submittal/x-a.pdf"]


def test_delete_bank_link_leaves_bank_untouched(monkeypatch):
    db = FakeDB({
        "submittal_materials": [{"id": "s1", "name": "EMT"}],
        "pm_material_submittals": [
            {"id": "L1", "project_id": "p1", "pm_material_id": "m1", "source": "bank",
             "submittal_material_id": "s1", "created_at": "2026-07-01T00:00:00Z"},
        ],
    })
    _, deleted = _install(monkeypatch, db)
    bank.delete_link("p1", "L1", "u1")
    assert db.tables["pm_material_submittals"] == []
    assert db.tables["submittal_materials"] == [{"id": "s1", "name": "EMT"}]  # bank kept
    assert deleted == []


def test_delete_404_for_other_project(monkeypatch):
    db = FakeDB({
        "pm_material_submittals": [
            {"id": "L1", "project_id": "p1", "pm_material_id": "m1", "source": "bank",
             "submittal_material_id": "s1", "created_at": "2026-07-01T00:00:00Z"},
        ],
    })
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as ei:
        bank.delete_link("pOTHER", "L1", "u1")
    assert ei.value.status_code == 404


# ── Router: PDF validation on upload ─────────────────────────────────────────


async def test_router_upload_rejects_non_pdf(monkeypatch):
    monkeypatch.setattr(pm_submittals, "require_pm_project", lambda pid: None)
    upload = UploadFile(
        filename="not.pdf",
        file=io.BytesIO(b"<html>nope"),
        headers=Headers({"content-type": "text/html"}),
    )
    with pytest.raises(HTTPException) as ei:
        await pm_submittals.bank_upload("p1", pm_material_id="m1", file=upload, user=_user())
    assert ei.value.status_code == 415


async def test_router_upload_rejects_bad_magic_bytes(monkeypatch):
    monkeypatch.setattr(pm_submittals, "require_pm_project", lambda pid: None)
    upload = UploadFile(
        filename="real.pdf",  # right extension, wrong bytes
        file=io.BytesIO(b"GIF89a"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    with pytest.raises(HTTPException) as ei:
        await pm_submittals.bank_upload("p1", pm_material_id="m1", file=upload, user=_user())
    assert ei.value.status_code == 415
