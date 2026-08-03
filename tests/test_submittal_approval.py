"""Submittal approval packages — the GC-facing send (migration 0081).

Covers the two halves of services/submittal_approval:

  available()       gathers vendor-reply attachments, Submittal Bank files and
                    project uploads into category buckets — deduped, markup
                    categories dropped, General Material kept, Uncategorized
                    last, byte-less rows skipped, and (critically) previous
                    packages' own transmittals NOT offered back as files.

  create_and_send() validates every picked key against that index, resolves GC
                    contacts, writes the package + one item per file, and sends
                    ONE email with a To list and a CC list.

The security property under test is the key guard: a file key is opaque and is
only honored if `available` produced it for THAT project and THAT category, so a
key from another project or another bucket is rejected identically to a
fabricated one.

Supabase is faked with the in-memory store used across the PM tests, extended
with `.like`, real `.order`/`.limit` (the package numbering depends on them) and
the PostgREST embeds these queries use. Graph, Gotenberg and storage are
monkeypatched.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.schemas import SubmittalApprovalGroup, SubmittalApprovalIn
from app.services import graph_email, pdf_combine, storage, submittal_pdf
from app.services import submittal_approval as sa

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/0081_submittal_approval_packages.sql"
)


# ── Fake Supabase ────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._likes = []
        self._single = False
        self._sel = "*"
        self._order = None
        self._desc = False
        self._limit = None

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

    def like(self, col, pattern):
        self._likes.append((col, pattern.rstrip("%")))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, col, desc=False, **k):
        self._order, self._desc = col, desc
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    def _matches(self, row):
        ok = all(
            row.get(c) in v if isinstance(v, list) else row.get(c) == v
            for c, v in self._filters
        )
        return ok and all(
            str(row.get(c) or "").startswith(p) for c, p in self._likes
        )

    def _embed(self, row):
        """Resolve the nested selects these queries use, the way PostgREST would."""
        row = dict(row)
        t = self.db.tables
        if self.table == "submittal_request_sends":
            if "vendor_contacts(" in self._sel:
                vc = next(
                    (c for c in t.get("vendor_contacts", []) if c["id"] == row.get("vendor_contact_id")),
                    None,
                )
                if vc:
                    vc = dict(vc)
                    vc["vendors"] = next(
                        (dict(v) for v in t.get("vendors", []) if v["id"] == vc.get("vendor_id")), None
                    )
                row["vendor_contacts"] = vc
            if "submittal_response_emails(" in self._sel:
                row["submittal_response_emails"] = [
                    {"email_id": e["email_id"]}
                    for e in t.get("submittal_response_emails", [])
                    if e["send_id"] == row["id"]
                ]
        if self.table == "submittal_material_files" and "submittal_files(" in self._sel:
            row["submittal_files"] = next(
                (dict(f) for f in t.get("submittal_files", []) if f["id"] == row.get("file_id")), None
            )
        if self.table == "submittal_packages" and "submittal_package_items(" in self._sel:
            row["submittal_package_items"] = [
                dict(i) for i in t.get("submittal_package_items", []) if i["package_id"] == row["id"]
            ]
        return row

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._order:
                hits = sorted(
                    hits, key=lambda r: (r.get(self._order) is None, r.get(self._order)),
                    reverse=self._desc,
                )
            if self._limit is not None:
                hits = hits[: self._limit]
            hits = [self._embed(r) for r in hits]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
            return SimpleNamespace(data=hits)
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", uuid.uuid4().hex)
                row.setdefault("created_at", "2026-07-28T00:00:00Z")
                # Column defaults the send path relies on rather than writing:
                # both approval grains default to 'pending' (0081), which is what
                # makes a freshly sent package read as awaiting a response.
                if self.table in ("submittal_packages", "submittal_package_items"):
                    row.setdefault("approval_status", "pending")
                    row.setdefault("responded_at", None)
                    row.setdefault("responded_by", None)
                    row.setdefault("response_notes", None)
                if self.table == "submittal_packages":
                    row.setdefault("supersedes_package_id", None)
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
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self, name)


# ── Fixtures ─────────────────────────────────────────────────────────────────

# No customer_gc_id here on purpose: it lives on pm_details (0057/0069), and a
# fixture that carried it on the project would let a send that reads the wrong
# table pass here while 42703-ing against the real database.
PROJECT = {"id": "p1", "number": "26-104", "name": "Riverside Plaza"}

# Two real categories + one markup (never submittable) + General Material.
CATEGORIES = [
    {"id": "c1", "name": "Switchgear", "kind": "material", "is_general": False, "sort_order": 1},
    {"id": "c2", "name": "Lighting", "kind": "material", "is_general": False, "sort_order": 2},
    {"id": "cg", "name": "General Material", "kind": "material", "is_general": True, "sort_order": 3},
    {"id": "cm", "name": "Overhead", "kind": "markup", "is_general": False, "sort_order": 9},
]


def _base_tables(**over):
    t = {
        "projects": [dict(PROJECT)],
        "pm_details": [{"project_id": "p1", "customer_gc_id": "gc1"}],
        "material_categories": [dict(c) for c in CATEGORIES],
        "pm_materials": [
            {"id": "m1", "project_id": "p1", "description": "NQ Panelboard", "material_category_id": "c1"},
            {"id": "m2", "project_id": "p1", "description": "2x4 Troffer", "material_category_id": "c2"},
        ],
        "gc_contacts": [
            {"id": "gcc1", "gc_id": "gc1", "name": "Dana Reyes", "email": "dana@gc.com"},
            {"id": "gcc2", "gc_id": "gc1", "name": "Sam Ito", "email": "sam@gc.com"},
            {"id": "gcc3", "gc_id": "gc1", "name": "No Mail", "email": None},
            {"id": "other", "gc_id": "gc9", "name": "Rival PM", "email": "rival@other.com"},
        ],
        "submittal_requests": [{"id": "r1", "project_id": "p1"}],
        "submittal_request_sends": [
            {"id": "s1", "request_id": "r1", "material_category_id": "c1", "vendor_contact_id": "vc1"}
        ],
        "vendor_contacts": [{"id": "vc1", "name": "Pat", "vendor_id": "v1"}],
        "vendors": [{"id": "v1", "name": "Rexel"}],
        "submittal_response_emails": [{"send_id": "s1", "email_id": "e1"}],
        "ingested_email_attachments": [
            {
                "id": "a1", "email_id": "e1", "filename": "panelboard.pdf",
                "size_bytes": 100, "storage_path": "ing/a1.pdf",
            },
            # Metadata-only (too large to store) — nothing to attach, so hidden.
            {"id": "a2", "email_id": "e1", "filename": "huge.pdf", "storage_path": None},
        ],
        "pm_material_submittals": [
            {"id": "l1", "project_id": "p1", "pm_material_id": "m2", "source": "bank",
             "submittal_material_id": "bm1", "document_id": None},
        ],
        "submittal_material_files": [{"material_id": "bm1", "file_id": "f1"}],
        "submittal_files": [
            {"id": "f1", "file_name": "troffer.pdf", "file_path": "bank/f1.pdf", "size_bytes": 50}
        ],
        "pm_documents": [],
        "email_log": [],
        "submittal_packages": [],
        "submittal_package_items": [],
    }
    t.update(over)
    return t


def _fake_cover(project, *, number, category_name, files):
    """Stand-in for the Chromium cover render. Records the category and which
    files it was told are merged, so tests can assert on both without a
    Gotenberg round trip."""
    flags = ",".join(
        f"{f['filename']}{'' if f.get('merged', True) else '(separate)'}" for f in files
    )
    return f"%PDF-cover:{category_name}:{flags}".encode()


def _install(monkeypatch, db):
    """Point the service at the fake DB and neutralize every side effect."""
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    monkeypatch.setattr(sa, "audit", lambda *a, **k: None)
    monkeypatch.setattr(storage, "build_object_path", lambda pid, cat, fn: f"{pid}/{cat}/{fn}")
    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(storage, "download_file", lambda path: b"%PDF-" + path.encode())
    monkeypatch.setattr(storage, "delete_file", lambda path: None)
    monkeypatch.setattr(submittal_pdf, "render_package_pdf", lambda *a, **k: b"%PDF-transmittal")
    # The per-category combined PDF, stubbed at its two seams: the Chromium cover
    # render and the pypdf concatenation. The fake storage hands out bytes that
    # only look like PDFs, so the real pdf_combine would (correctly) refuse them —
    # it gets its own unit tests against genuine PDFs in test_pdf_combine.py.
    monkeypatch.setattr(submittal_pdf, "render_category_cover_pdf", _fake_cover)
    monkeypatch.setattr(pdf_combine, "to_pdf", lambda content, filename: content)
    monkeypatch.setattr(pdf_combine, "merge", lambda parts: b"|".join(parts))
    sent = {}

    def _draft(to, subject, body, *, html=False, sender=None, cc=None):
        sent["to"], sent["cc"], sent["subject"], sender_ = to, cc, subject, sender
        sent["sender"] = sender_
        sent["attachments"] = []
        return {"id": "msg1", "conversationId": "conv1", "internetMessageId": "<imid>"}

    monkeypatch.setattr(graph_email, "create_draft", _draft)
    monkeypatch.setattr(
        graph_email, "add_attachment",
        lambda mid, name, content, ctype, **k: sent["attachments"].append(name),
    )
    monkeypatch.setattr(graph_email, "send_draft", lambda mid, **k: sent.update(sent_=True))
    monkeypatch.setattr(sa, "get_settings", lambda: SimpleNamespace(
        submittal_sender="ingest@g3.com", email_ingest_mailbox="ingest@g3.com",
        rfq_drawings_inline_limit_mb=20,
    ))
    return sent


def _body(**over):
    b = {
        "groups": [{"material_category_id": "c1", "file_keys": ["att:a1"]}],
        "recipient_contact_ids": ["gcc1"],
        "cc_contact_ids": ["gcc2"],
        "message": None,
    }
    b.update(over)
    return b


# ── Schema ───────────────────────────────────────────────────────────────────


def test_schema_requires_a_group_and_a_recipient():
    with pytest.raises(ValidationError):
        SubmittalApprovalIn(groups=[], recipient_contact_ids=["x"])
    with pytest.raises(ValidationError):
        SubmittalApprovalIn(groups=[SubmittalApprovalGroup()], recipient_contact_ids=[])


def test_schema_blank_message_becomes_none():
    body = SubmittalApprovalIn(
        groups=[SubmittalApprovalGroup(file_keys=["pm:1"])],
        recipient_contact_ids=["gcc1"],
        message="   \n ",
    )
    assert body.message is None
    assert body.groups[0].material_category_id is None  # Uncategorized bucket


# ── Migration parity ─────────────────────────────────────────────────────────


def test_migration_declares_both_tables_and_approval_grains():
    sql = MIGRATION.read_text()
    assert "create table submittal_packages" in sql
    assert "create table submittal_package_items" in sql
    # Package-level 'partial' exists; per-file it deliberately does not.
    assert "approval_status in ('pending', 'approved', 'partial', 'denied')" in sql
    assert "approval_status in ('pending', 'approved', 'approved_as_noted', 'rejected')" in sql
    # Both grains default to pending — the send path writes neither, so the
    # column default is what makes a freshly sent package read as awaiting.
    assert sql.count("approval_status text not null default 'pending'") == 1
    assert sql.count("approval_status      text not null default 'pending'") == 1
    assert "unique (project_id, number)" in sql
    assert sql.count("enable row level security") == 2
    assert sql.count("force  row level security") == 2


# ── available() ──────────────────────────────────────────────────────────────


def test_available_groups_sources_by_category(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    cats = sa.available("p1")["categories"]
    by_name = {c["name"]: c for c in cats}

    assert [f["key"] for f in by_name["Switchgear"]["files"]] == ["att:a1"]
    assert by_name["Switchgear"]["files"][0]["origin"] == "Rexel"
    assert [f["key"] for f in by_name["Lighting"]["files"]] == ["bank:f1"]
    assert by_name["Lighting"]["files"][0]["description"] == "2x4 Troffer"


def test_available_keeps_general_material_and_drops_markups(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    names = [c["name"] for c in sa.available("p1")["categories"]]
    # General Material is submittable to the GC even though vendors never see it.
    assert "General Material" not in names  # no materials in it → no bucket
    assert "Overhead" not in names  # markup: never submittable

    db.tables["pm_materials"].append(
        {"id": "m3", "project_id": "p1", "description": "Misc", "material_category_id": "cg"}
    )
    names = [c["name"] for c in sa.available("p1")["categories"]]
    assert "General Material" in names
    assert "Overhead" not in names


def test_available_skips_attachments_with_no_stored_bytes(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    keys = {f["key"] for c in sa.available("p1")["categories"] for f in c["files"]}
    assert "att:a1" in keys
    assert "att:a2" not in keys  # storage_path is null — nothing to attach


def test_available_dedupes_a_shared_bank_file(monkeypatch):
    """Two materials in one category linking the same bank cut sheet must offer
    that file once, not twice."""
    t = _base_tables()
    t["pm_materials"].append(
        {"id": "m4", "project_id": "p1", "description": "2x2 Troffer", "material_category_id": "c2"}
    )
    t["pm_material_submittals"].append(
        {"id": "l2", "project_id": "p1", "pm_material_id": "m4", "source": "bank",
         "submittal_material_id": "bm1", "document_id": None}
    )
    db = FakeDB(t)
    _install(monkeypatch, db)
    lighting = next(c for c in sa.available("p1")["categories"] if c["name"] == "Lighting")
    assert [f["key"] for f in lighting["files"]] == ["bank:f1"]


def test_available_uncategorized_bucket_sorts_last(monkeypatch):
    t = _base_tables()
    t["pm_materials"].append(
        {"id": "m9", "project_id": "p1", "description": "Odds", "material_category_id": None}
    )
    db = FakeDB(t)
    _install(monkeypatch, db)
    cats = sa.available("p1")["categories"]
    assert cats[-1]["name"] == sa.UNCATEGORIZED_LABEL
    assert cats[-1]["material_category_id"] is None


def test_available_offers_staged_uploads_but_not_sent_transmittals(monkeypatch):
    """The 'submittal' folder also holds vendor request sheets and this feature's
    own transmittals — offering those back would let a package re-send its
    predecessor's cover sheet."""
    t = _base_tables()
    t["pm_documents"] = [
        {"id": "d1", "project_id": "p1", "category": "submittal", "filename": "mine.pdf",
         "storage_path": "pm/d1.pdf", "size_bytes": 10,
         "note": f"{sa.STAGED_NOTE_PREFIX} — Switchgear [cat:c1]"},
        {"id": "d2", "project_id": "p1", "category": "submittal", "filename": "xmit.pdf",
         "storage_path": "pm/d2.pdf", "size_bytes": 10, "note": "Submittal transmittal 001"},
        {"id": "d3", "project_id": "p1", "category": "submittal", "filename": "req.pdf",
         "storage_path": "pm/d3.pdf", "size_bytes": 10, "note": "Submittal request — Switchgear"},
    ]
    db = FakeDB(t)
    _install(monkeypatch, db)
    keys = {f["key"] for c in sa.available("p1")["categories"] for f in c["files"]}
    assert "pm:d1" in keys
    assert "pm:d2" not in keys and "pm:d3" not in keys

    switchgear = next(c for c in sa.available("p1")["categories"] if c["name"] == "Switchgear")
    assert {f["key"] for f in switchgear["files"]} == {"att:a1", "pm:d1"}


# ── create_and_send: validation ──────────────────────────────────────────────


def test_send_rejects_a_key_not_in_this_project(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    with pytest.raises(ValueError, match="not found in this project"):
        sa.create_and_send("p1", _body(groups=[
            {"material_category_id": "c1", "file_keys": ["att:stolen"]}
        ]), "u1")


def test_send_rejects_a_key_picked_under_the_wrong_category(monkeypatch):
    """att:a1 is a real file for this project, but it belongs to Switchgear —
    claiming it under Lighting is rejected exactly like a fabricated key."""
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    with pytest.raises(ValueError, match="not found in this project"):
        sa.create_and_send("p1", _body(groups=[
            {"material_category_id": "c2", "file_keys": ["att:a1"]}
        ]), "u1")


def test_send_requires_at_least_one_file(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    with pytest.raises(ValueError, match="at least one submittal file"):
        sa.create_and_send("p1", _body(groups=[{"material_category_id": "c1", "file_keys": []}]), "u1")


@pytest.mark.parametrize(
    "details",
    [
        pytest.param([{"project_id": "p1", "customer_gc_id": None}], id="unset"),
        # A bidding project that never entered PM has no row at all.
        pytest.param([], id="no-pm-details-row"),
    ],
)
def test_send_requires_a_customer_gc(monkeypatch, details):
    t = _base_tables()
    t["pm_details"] = details
    db = FakeDB(t)
    _install(monkeypatch, db)
    with pytest.raises(ValueError, match="no customer"):
        sa.create_and_send("p1", _body(), "u1")


def test_send_rejects_a_contact_from_another_gc(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    with pytest.raises(ValueError, match="Contact not found"):
        sa.create_and_send("p1", _body(recipient_contact_ids=["other"]), "u1")


def test_send_rejects_a_to_contact_with_no_email(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    with pytest.raises(ValueError, match="no email address"):
        sa.create_and_send("p1", _body(recipient_contact_ids=["gcc3"]), "u1")


def test_send_silently_drops_a_cc_contact_with_no_email(monkeypatch):
    """A CC without an address is skipped, not an error — the send still has a
    valid To list, so failing it would be gratuitous."""
    db = FakeDB(_base_tables())
    sent = _install(monkeypatch, db)
    sa.create_and_send("p1", _body(cc_contact_ids=["gcc3"]), "u1")
    assert sent["cc"] is None
    pkg = db.tables["submittal_packages"][0]
    assert pkg["cc_recipients"] == []


def test_send_drops_a_contact_that_is_both_to_and_cc(monkeypatch):
    db = FakeDB(_base_tables())
    sent = _install(monkeypatch, db)
    sa.create_and_send("p1", _body(recipient_contact_ids=["gcc1"], cc_contact_ids=["gcc1"]), "u1")
    assert sent["to"] == ["dana@gc.com"]
    assert sent["cc"] is None


# ── create_and_send: the happy path ──────────────────────────────────────────


def test_send_writes_package_and_one_item_per_file(monkeypatch):
    db = FakeDB(_base_tables())
    sent = _install(monkeypatch, db)
    res = sa.create_and_send("p1", _body(groups=[
        {"material_category_id": "c1", "file_keys": ["att:a1"]},
        {"material_category_id": "c2", "file_keys": ["bank:f1"]},
    ], message="Please review."), "u1")

    assert res["send_status"] == "sent"
    assert res["number"] == 1 and res["file_count"] == 2
    assert res["files_delivery"] == "attached"

    pkg = db.tables["submittal_packages"][0]
    assert pkg["send_status"] == "sent"
    assert pkg["conversation_id"] == "conv1"          # the future reply-matching key
    # The send path writes no verdict of its own — a freshly sent package is
    # awaiting a response until someone records one (0082), and it supersedes
    # nothing unless it was sent as a resubmittal.
    assert pkg["approval_status"] == "pending"
    assert pkg["responded_at"] is None
    assert pkg["supersedes_package_id"] is None
    assert pkg["subject"] == "26-104 - Riverside Plaza - Submittal 001"
    assert [r["email"] for r in pkg["recipients"]] == ["dana@gc.com"]
    assert [r["email"] for r in pkg["cc_recipients"]] == ["sam@gc.com"]

    items = {i["filename"]: i for i in db.tables["submittal_package_items"]}
    assert set(items) == {"panelboard.pdf", "troffer.pdf"}
    # Exactly the pointer column matching `source` is set (the pairing the
    # migration documents but can't CHECK).
    assert items["panelboard.pdf"]["source"] == "vendor_reply"
    assert items["panelboard.pdf"]["attachment_id"] == "a1"
    assert "submittal_file_id" not in items["panelboard.pdf"]
    assert items["troffer.pdf"]["source"] == "bank"
    assert items["troffer.pdf"]["submittal_file_id"] == "f1"
    assert items["troffer.pdf"]["category_label"] == "Lighting"
    assert all(i["approval_status"] == "pending" for i in items.values())


def test_send_is_one_email_with_to_and_cc(monkeypatch):
    db = FakeDB(_base_tables())
    sent = _install(monkeypatch, db)
    sa.create_and_send("p1", _body(), "u1")
    assert sent["to"] == ["dana@gc.com"]
    assert sent["cc"] == ["sam@gc.com"]
    assert sent["sender"] == "ingest@g3.com"  # replies must reach the ingest mailbox
    assert sent["sent_"] is True
    # Logo + transmittal + ONE combined PDF for the selected file's category —
    # the source file is inside that PDF, not an attachment of its own.
    assert "Submittal Transmittal - 26-104 - 001.pdf" in sent["attachments"]
    assert "Requested Submittals - 26-104 - Switchgear.pdf" in sent["attachments"]
    assert "panelboard.pdf" not in sent["attachments"]


def test_send_archives_the_transmittal_in_the_documents_hub(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    sa.create_and_send("p1", _body(), "u1")
    docs = db.tables["pm_documents"]
    transmittal = next(d for d in docs if d["filename"].startswith("Submittal Transmittal"))
    assert transmittal["category"] == "submittal"
    assert db.tables["submittal_packages"][0]["pdf_doc_id"] == transmittal["id"]


# ── One combined PDF per category ────────────────────────────────────────────


def test_each_category_is_sent_as_one_combined_pdf(monkeypatch):
    """Two categories → two attachments, each a cover page plus that category's
    files — not four loose files."""
    db = FakeDB(_base_tables())
    sent = _install(monkeypatch, db)
    t = db.tables
    t["ingested_email_attachments"].append(
        {"id": "a3", "email_id": "e1", "filename": "breaker.pdf",
         "size_bytes": 100, "storage_path": "ing/a3.pdf"}
    )
    res = sa.create_and_send("p1", _body(groups=[
        {"material_category_id": "c1", "file_keys": ["att:a1", "att:a3"]},
        {"material_category_id": "c2", "file_keys": ["bank:f1"]},
    ]), "u1")

    assert res["file_count"] == 3 and res["category_count"] == 2
    combined = [n for n in sent["attachments"] if n.startswith("Requested Submittals")]
    assert combined == [
        "Requested Submittals - 26-104 - Switchgear.pdf",
        "Requested Submittals - 26-104 - Lighting.pdf",
    ]
    # Every source file is inside a combined PDF, never alongside it.
    assert not {"panelboard.pdf", "breaker.pdf", "troffer.pdf"} & set(sent["attachments"])


def test_combined_pdf_is_cover_page_then_the_category_files_in_order(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    t = db.tables
    t["ingested_email_attachments"].append(
        {"id": "a3", "email_id": "e1", "filename": "breaker.pdf",
         "size_bytes": 100, "storage_path": "ing/a3.pdf"}
    )
    captured = []
    monkeypatch.setattr(pdf_combine, "merge", lambda parts: captured.append(parts) or b"%PDF-out")
    sa.create_and_send("p1", _body(groups=[
        {"material_category_id": "c1", "file_keys": ["att:a1", "att:a3"]}
    ]), "u1")

    (parts,) = captured
    assert parts[0] == b"%PDF-cover:Switchgear:panelboard.pdf,breaker.pdf"
    # The fake storage echoes the path, so the tail is the two source files in
    # the order they were selected.
    assert parts[1:] == [b"%PDF-ing/a1.pdf", b"%PDF-ing/a3.pdf"]


def test_combined_pdfs_are_archived_under_the_name_the_gc_receives(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    sa.create_and_send("p1", _body(groups=[
        {"material_category_id": "c1", "file_keys": ["att:a1"]},
        {"material_category_id": "c2", "file_keys": ["bank:f1"]},
    ]), "u1")

    archived = {d["filename"]: d for d in db.tables["pm_documents"]}
    assert "Requested Submittals - 26-104 - Switchgear.pdf" in archived
    assert "Requested Submittals - 26-104 - Lighting.pdf" in archived
    gear = archived["Requested Submittals - 26-104 - Switchgear.pdf"]
    assert gear["category"] == "submittal"
    assert gear["note"] == "Requested submittals 001 — Switchgear"


def test_archived_combined_pdfs_are_not_offered_back_as_selectable_files(monkeypatch):
    """The archive note must stay clear of STAGED_NOTE_PREFIX, or the next
    package would offer this one's output as one of its inputs."""
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    sa.create_and_send("p1", _body(), "u1")
    offered = {f["filename"] for c in sa.available("p1")["categories"] for f in c["files"]}
    assert not any(n.startswith("Requested Submittals") for n in offered)


def test_uncategorized_files_get_their_own_combined_pdf(monkeypatch):
    t = _base_tables()
    t["pm_materials"].append(
        {"id": "m9", "project_id": "p1", "description": "Misc", "material_category_id": None}
    )
    t["pm_documents"].append(
        {"id": "d9", "project_id": "p1", "category": "submittal", "filename": "misc.pdf",
         "storage_path": "pm/d9.pdf", "size_bytes": 10,
         "note": f"{sa.STAGED_NOTE_PREFIX} — Uncategorized"}
    )
    db = FakeDB(t)
    sent = _install(monkeypatch, db)
    sa.create_and_send("p1", _body(groups=[
        {"material_category_id": None, "file_keys": ["pm:d9"]}
    ]), "u1")
    assert "Requested Submittals - 26-104 - Uncategorized.pdf" in sent["attachments"]


def test_a_file_that_cannot_be_merged_rides_along_and_is_flagged(monkeypatch):
    """An unmergeable file is never dropped: it attaches separately and the cover
    page says so, so the GC's index still lists everything in the category."""
    t = _base_tables()
    t["ingested_email_attachments"].append(
        {"id": "a4", "email_id": "e1", "filename": "shop-drawings.zip",
         "size_bytes": 100, "storage_path": "ing/a4.zip"}
    )
    db = FakeDB(t)
    sent = _install(monkeypatch, db)

    def _picky(content, filename):
        if filename.endswith(".zip"):
            raise pdf_combine.UnmergeableFile(f"{filename}: nope")
        return content

    monkeypatch.setattr(pdf_combine, "to_pdf", _picky)
    captured = []
    monkeypatch.setattr(pdf_combine, "merge", lambda parts: captured.append(parts) or b"%PDF-out")

    res = sa.create_and_send("p1", _body(groups=[
        {"material_category_id": "c1", "file_keys": ["att:a1", "att:a4"]}
    ]), "u1")

    assert res["file_count"] == 2 and res["category_count"] == 1
    assert "Requested Submittals - 26-104 - Switchgear.pdf" in sent["attachments"]
    assert "shop-drawings.zip" in sent["attachments"]
    # Listed on the cover page, marked as travelling separately.
    (parts,) = captured
    assert parts[0] == b"%PDF-cover:Switchgear:panelboard.pdf,shop-drawings.zip(separate)"
    # ...and not folded into the PDF itself.
    assert parts[1:] == [b"%PDF-ing/a1.pdf"]
    # The item row still records it as sent — an unmergeable file is delivered,
    # not skipped.
    assert {i["filename"] for i in db.tables["submittal_package_items"]} == {
        "panelboard.pdf", "shop-drawings.zip"
    }


def test_a_cover_render_failure_fails_the_send(monkeypatch):
    """Gotenberg dying between the transmittal and the cover pages fails the
    package rather than shipping unlabelled merges."""
    from app.services.office_preview import ConversionError

    db = FakeDB(_base_tables())
    _install(monkeypatch, db)

    def _boom(*a, **k):
        raise ConversionError("gotenberg unreachable")

    monkeypatch.setattr(submittal_pdf, "render_category_cover_pdf", _boom)
    with pytest.raises(ValueError, match="category submittal PDFs"):
        sa.create_and_send("p1", _body(), "u1")
    assert db.tables["submittal_packages"][0]["send_status"] == "failed"


def test_package_numbers_increment_per_project(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    assert sa.create_and_send("p1", _body(), "u1")["number"] == 1
    assert sa.create_and_send("p1", _body(), "u1")["number"] == 2


def test_send_dedupes_a_file_picked_under_two_categories(monkeypatch):
    """The same bytes are attached once even when ticked in two buckets."""
    t = _base_tables()
    t["pm_materials"].append(
        {"id": "m5", "project_id": "p1", "description": "Trim", "material_category_id": "c1"}
    )
    t["pm_material_submittals"].append(
        {"id": "l3", "project_id": "p1", "pm_material_id": "m5", "source": "bank",
         "submittal_material_id": "bm1", "document_id": None}
    )
    db = FakeDB(t)
    _install(monkeypatch, db)
    res = sa.create_and_send("p1", _body(groups=[
        {"material_category_id": "c1", "file_keys": ["bank:f1"]},
        {"material_category_id": "c2", "file_keys": ["bank:f1"]},
    ]), "u1")
    assert res["file_count"] == 1
    assert len(db.tables["submittal_package_items"]) == 1


def test_oversize_selection_goes_to_onedrive(monkeypatch):
    db = FakeDB(_base_tables())
    sent = _install(monkeypatch, db)
    monkeypatch.setattr(sa, "get_settings", lambda: SimpleNamespace(
        submittal_sender="ingest@g3.com", email_ingest_mailbox="ingest@g3.com",
        rfq_drawings_inline_limit_mb=0,  # everything is over the limit
    ))
    uploaded = []
    monkeypatch.setattr(graph_email, "drive_upload", lambda path, content, **k: uploaded.append(path))
    monkeypatch.setattr(graph_email, "drive_get_item_id", lambda folder, **k: "item1")
    monkeypatch.setattr(graph_email, "drive_create_link", lambda item, **k: "https://1drv.ms/x")

    res = sa.create_and_send("p1", _body(), "u1")
    assert res["files_delivery"] == "onedrive_link"
    assert db.tables["submittal_packages"][0]["files_delivery"] == "onedrive_link"
    # The combined PDF rides the link, not the message; the transmittal still
    # attaches so the GC has something to mark up without following it.
    assert not [n for n in sent["attachments"] if n.startswith("Requested Submittals")]
    assert "Submittal Transmittal - 26-104 - 001.pdf" in sent["attachments"]
    # Folder is namespaced per package so the anonymous folder link can't reach
    # another package's files (different GCs / verdicts).
    pkg_id = db.tables["submittal_packages"][0]["id"]
    assert uploaded == [
        f"BDR/26-104/submittals-for-approval/{pkg_id}/"
        "001-Requested Submittals - 26-104 - Switchgear.pdf"
    ]


def test_onedrive_upload_prefixes_index_so_duplicate_names_survive(monkeypatch):
    """Two vendors both send 'cutsheet.zip' — neither can be merged, so both go up
    as themselves, and without the index prefix the second would silently
    overwrite the first."""
    t = _base_tables()
    for aid in ("a3", "a4"):
        t["ingested_email_attachments"].append(
            {"id": aid, "email_id": "e1", "filename": "cutsheet.zip",
             "size_bytes": 100, "storage_path": f"ing/{aid}.zip"}
        )
    db = FakeDB(t)
    _install(monkeypatch, db)
    monkeypatch.setattr(sa, "get_settings", lambda: SimpleNamespace(
        submittal_sender="ingest@g3.com", email_ingest_mailbox="ingest@g3.com",
        rfq_drawings_inline_limit_mb=0,
    ))

    def _picky(content, filename):
        if filename.endswith(".zip"):
            raise pdf_combine.UnmergeableFile(f"{filename}: nope")
        return content

    monkeypatch.setattr(pdf_combine, "to_pdf", _picky)
    uploaded = []
    monkeypatch.setattr(graph_email, "drive_upload", lambda path, content, **k: uploaded.append(path))
    monkeypatch.setattr(graph_email, "drive_get_item_id", lambda folder, **k: "item1")
    monkeypatch.setattr(graph_email, "drive_create_link", lambda item, **k: "https://1drv.ms/x")

    sa.create_and_send("p1", _body(groups=[
        {"material_category_id": "c1", "file_keys": ["att:a3", "att:a4"]}
    ]), "u1")
    # The combined PDF plus both same-named loose files, all distinct paths.
    assert len(uploaded) == 3 and len(set(uploaded)) == 3


# ── create_and_send: failure is recorded, not raised ─────────────────────────


def test_send_failure_marks_the_package_failed_without_raising(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)

    def _boom(*a, **k):
        raise RuntimeError("Graph is down")

    monkeypatch.setattr(graph_email, "create_draft", _boom)
    res = sa.create_and_send("p1", _body(), "u1")
    assert res["send_status"] == "failed"
    assert "Graph is down" in res["error"]
    # The attempt stays visible in the log rather than vanishing.
    pkg = db.tables["submittal_packages"][0]
    assert pkg["send_status"] == "failed" and "Graph is down" in pkg["error"]
    assert db.tables["email_log"] == []


def test_missing_sender_config_is_rejected_before_any_row(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    monkeypatch.setattr(sa, "get_settings", lambda: SimpleNamespace(
        submittal_sender="", email_ingest_mailbox="", rfq_drawings_inline_limit_mb=20,
    ))
    with pytest.raises(ValueError, match="SUBMITTAL_SENDER"):
        sa.create_and_send("p1", _body(), "u1")
    assert db.tables["submittal_packages"] == []


def test_a_file_that_vanished_between_pick_and_send_fails_the_package(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    real_resolve = sa._resolve_paths

    def _drop(sb, entries):
        real_resolve(sb, entries)
        for e in entries:
            e["storage_path"] = None

    monkeypatch.setattr(sa, "_resolve_paths", _drop)
    with pytest.raises(ValueError, match="no longer available"):
        sa.create_and_send("p1", _body(), "u1")
    assert db.tables["submittal_packages"][0]["send_status"] == "failed"


# ── stage_upload / list_packages ─────────────────────────────────────────────


def test_stage_upload_archives_with_the_category_marker(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    out = sa.stage_upload("p1", "c1", "cutsheet.pdf", b"%PDF-x", "u1")
    assert out["key"].startswith("pm:") and out["source"] == "document"
    doc = db.tables["pm_documents"][0]
    assert doc["note"] == f"{sa.STAGED_NOTE_PREFIX} — Switchgear [cat:c1]"
    # available() must now offer it under that category.
    switchgear = next(c for c in sa.available("p1")["categories"] if c["name"] == "Switchgear")
    assert out["key"] in {f["key"] for f in switchgear["files"]}


def test_stage_upload_without_a_category_lands_uncategorized(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    out = sa.stage_upload("p1", None, "loose.pdf", b"%PDF-x", "u1")
    assert "[cat:" not in db.tables["pm_documents"][0]["note"]
    bucket = next(c for c in sa.available("p1")["categories"] if c["material_category_id"] is None)
    assert out["key"] in {f["key"] for f in bucket["files"]}


def test_list_packages_returns_items(monkeypatch):
    db = FakeDB(_base_tables())
    _install(monkeypatch, db)
    sa.create_and_send("p1", _body(), "u1")
    pkgs = sa.list_packages("p1")
    assert len(pkgs) == 1
    assert [i["filename"] for i in pkgs[0]["submittal_package_items"]] == ["panelboard.pdf"]


# ── Templates ────────────────────────────────────────────────────────────────


def test_subject_and_body_templates():
    assert sa.build_subject(PROJECT, 7) == "26-104 - Riverside Plaza - Submittal 007"
    body = sa.build_body(PROJECT, 7, None, None)
    assert "Submittal 007 for 26-104 Riverside Plaza" in body
    assert "attached as one combined PDF per category." in body
    assert body.endswith("Thank you,\nThe G3 Estimating Team")


def test_body_uses_the_link_when_files_are_oversize():
    body = sa.build_body(PROJECT, 1, "note here", "https://1drv.ms/x")
    assert "one combined PDF per category: https://1drv.ms/x" in body
    assert "note here" in body


def test_transmittal_escapes_user_content():
    """Filenames and the cover note are user-supplied — escaping is the
    injection boundary for the rendered HTML."""
    html = submittal_pdf.render_package_html(
        PROJECT,
        number=1,
        groups=[("Switchgear", [{"filename": "<script>x</script>.pdf", "description": None}])],
        message="<img src=x onerror=alert(1)>",
        recipients=[{"name": "A&B", "email": "a@b.com"}],
        cc_recipients=[],
    )
    # Escaped, so the markup is inert text rather than a tag. The payload string
    # still appears — that's the point; what must not appear is a live element.
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "A&amp;B" in html


def test_package_pdf_filename_is_zero_padded_and_safe():
    assert (
        submittal_pdf.package_pdf_filename({"number": "26-104"}, 3)
        == "Submittal Transmittal - 26-104 - 003.pdf"
    )
    assert submittal_pdf.package_pdf_filename({"number": None}, 1).startswith(
        "Submittal Transmittal - Project - 001"
    )


def test_category_pdf_filename_names_the_category_and_is_path_safe():
    assert (
        submittal_pdf.category_pdf_filename({"number": "26-104"}, "Switchgear")
        == "Requested Submittals - 26-104 - Switchgear.pdf"
    )
    # A category name is user-editable, so it is stripped to filename-safe
    # characters before it can become a storage path or an attachment name.
    assert (
        submittal_pdf.category_pdf_filename({"number": "26-104"}, "Gear/Low: 480V?")
        == "Requested Submittals - 26-104 - GearLow 480V.pdf"
    )
    assert submittal_pdf.category_pdf_filename({"number": None}, "") == (
        "Requested Submittals - Project - Materials.pdf"
    )


def test_category_cover_page_names_the_category_and_lists_its_files():
    html = submittal_pdf.render_category_cover_html(
        PROJECT,
        number=3,
        category_name="Switchgear",
        files=[
            {"filename": "panelboard.pdf", "description": "200A MLO", "merged": True},
            {"filename": "shop-drawings.zip", "description": None, "merged": False},
        ],
    )
    assert "Requested Submittals" in html
    assert "Switchgear" in html
    assert "003" in html and "Riverside Plaza" in html
    assert "panelboard.pdf" in html and "200A MLO" in html
    # The unmergeable file is still indexed, flagged as travelling separately.
    assert "shop-drawings.zip" in html
    assert "sent as a separate attachment" in html
    assert "1 of the files above could not be combined" in html


def test_category_cover_page_escapes_user_content():
    html = submittal_pdf.render_category_cover_html(
        PROJECT,
        number=1,
        category_name="<script>alert(1)</script>",
        files=[{"filename": "<img src=x onerror=alert(1)>.pdf", "description": None}],
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html


def test_category_cover_page_without_a_separate_file_has_no_footnote():
    html = submittal_pdf.render_category_cover_html(
        PROJECT, number=1, category_name="Lighting",
        files=[{"filename": "troffer.pdf", "description": None, "merged": True}],
    )
    assert "could not be combined" not in html
    assert "sent as a separate attachment" not in html


# ── Verdicts + resubmittals (migration 0082) ─────────────────────────────────
#
# The GC's answer is recorded by a HUMAN (no reply-reader exists), per file,
# because that is how GCs answer: most of a package approved, one cut sheet
# rejected. The rejected ones are then what a resubmittal carries — a NEW package
# that points back at the one it answers, leaving that package's verdicts frozen.

MIGRATION_0082 = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/0082_submittal_approval_verdicts.sql"
)


def test_migration_0082_adds_the_verdict_author_and_the_resubmittal_link():
    sql = MIGRATION_0082.read_text()
    assert "alter table submittal_packages" in sql
    assert "add column responded_by uuid references profiles(id) on delete set null" in sql
    assert (
        "add column supersedes_package_id uuid references submittal_packages(id) "
        "on delete set null" in sql
    )
    # Not CASCADE: deleting a superseded package must not take its live
    # resubmittals with it.
    assert "on delete cascade" not in sql
    # No new status values — 0081's CHECKs already cover both grains.
    assert "check (approval_status" not in sql


@pytest.mark.parametrize(
    "statuses, expected",
    [
        ([], "pending"),
        (["pending", "pending"], "pending"),
        (["approved", "approved"], "approved"),
        (["approved", "approved_as_noted"], "approved"),
        (["rejected", "rejected"], "denied"),
        (["approved", "rejected"], "partial"),
        (["approved", "pending"], "partial"),
        (["rejected", "pending"], "partial"),
    ],
)
def test_rollup_derives_the_package_headline_from_its_files(statuses, expected):
    assert sa._rollup(statuses) == expected


def _sent_package(monkeypatch, **over):
    """Send one package and return (db, package_id, sent)."""
    db = FakeDB(_base_tables())
    sent = _install(monkeypatch, db)
    body = _body(
        groups=[
            {"material_category_id": "c1", "file_keys": ["att:a1"]},
            {"material_category_id": "c2", "file_keys": ["bank:f1"]},
        ],
        **over,
    )
    res = sa.create_and_send("p1", body, "u1")
    assert res["send_status"] == "sent"
    return db, res["package_id"], sent


def test_verdicts_write_each_file_and_derive_the_package_status(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    items = db.tables["submittal_package_items"]
    assert len(items) == 2

    out = sa.record_verdicts(
        "p1",
        pkg_id,
        {
            "items": [
                {"id": items[0]["id"], "approval_status": "approved", "response_notes": None},
                {
                    "id": items[1]["id"],
                    "approval_status": "rejected",
                    "response_notes": "  Revise to 4000K  ",
                },
            ],
            "response_notes": "See markups.",
        },
        "u9",
    )

    assert out["approval_status"] == "partial"
    assert out["response_notes"] == "See markups."
    assert out["responded_by"] == "u9"
    assert out["responded_at"] is not None
    by_id = {i["id"]: i for i in db.tables["submittal_package_items"]}
    assert by_id[items[0]["id"]]["approval_status"] == "approved"
    assert by_id[items[1]["id"]]["approval_status"] == "rejected"
    # Notes are stripped; an empty note is stored as null, not "".
    assert by_id[items[1]["id"]]["response_notes"] == "Revise to 4000K"
    assert by_id[items[0]["id"]]["response_notes"] is None
    assert by_id[items[0]["id"]]["responded_by"] == "u9"


def test_all_files_approved_rolls_the_package_up_to_approved(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    items = db.tables["submittal_package_items"]
    out = sa.record_verdicts(
        "p1",
        pkg_id,
        {"items": [{"id": i["id"], "approval_status": "approved_as_noted"} for i in items]},
        "u9",
    )
    # Approved-with-comments is still an acceptance; the per-file badge keeps the
    # distinction the package headline drops.
    assert out["approval_status"] == "approved"


def test_reverting_a_file_to_pending_clears_its_response_stamp(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    items = db.tables["submittal_package_items"]
    sa.record_verdicts(
        "p1", pkg_id,
        {"items": [{"id": i["id"], "approval_status": "approved"} for i in items]}, "u9",
    )
    out = sa.record_verdicts(
        "p1", pkg_id,
        {"items": [{"id": i["id"], "approval_status": "pending"} for i in items]}, "u9",
    )
    # "Pending, responded at 3pm by Dana" would be a state the log can't explain.
    assert out["approval_status"] == "pending"
    assert out["responded_at"] is None and out["responded_by"] is None
    assert all(i["responded_at"] is None for i in db.tables["submittal_package_items"])


def test_a_verdict_on_another_projects_package_is_not_found(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    db.tables["projects"].append({"id": "p2", "number": "26-999", "name": "Other"})
    with pytest.raises(ValueError, match="not found"):
        sa.record_verdicts("p2", pkg_id, {"items": []}, "u9")


def test_a_verdict_on_a_file_outside_the_package_is_rejected(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    with pytest.raises(ValueError, match="not found in this package"):
        sa.record_verdicts(
            "p1", pkg_id,
            {"items": [{"id": "fabricated", "approval_status": "approved"}]}, "u9",
        )
    # Nothing was written — the whole call is refused, not partially applied.
    assert all(
        i["approval_status"] == "pending" for i in db.tables["submittal_package_items"]
    )


def test_a_package_that_never_sent_cannot_have_a_verdict(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    for p in db.tables["submittal_packages"]:
        p["send_status"] = "failed"
    with pytest.raises(ValueError, match="hasn't been sent"):
        sa.record_verdicts("p1", pkg_id, {"items": []}, "u9")


def test_resend_options_merge_the_packages_files_into_the_available_tree(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    items = db.tables["submittal_package_items"]
    sa.record_verdicts(
        "p1", pkg_id,
        {"items": [{"id": items[0]["id"], "approval_status": "rejected"}]}, "u9",
    )

    opts = sa.resend_options("p1", pkg_id)
    assert opts["package"]["number"] == 1
    assert [r["contact_id"] for r in opts["package"]["recipients"]] == ["gcc1"]

    files = {f["key"]: f for c in opts["categories"] for f in c["files"]}
    # One tree, not two: the sent file is the SAME row as its available entry,
    # annotated — not a duplicate checkbox.
    assert len([f for c in opts["categories"] for f in c["files"] if f["key"] == "att:a1"]) == 1
    assert files["att:a1"]["prior"] is True
    assert files["att:a1"]["prior_status"] == "rejected"
    assert files["bank:f1"]["prior_status"] == "pending"
    assert files["att:a1"]["available"] is True


def test_resend_options_show_a_deleted_source_as_unsendable(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    # ON DELETE SET NULL fired on the source row (0081's documented steady state).
    for it in db.tables["submittal_package_items"]:
        if it["source"] == "vendor_reply":
            it["attachment_id"] = None
    db.tables["ingested_email_attachments"] = []

    opts = sa.resend_options("p1", pkg_id)
    gone = [
        f
        for c in opts["categories"]
        for f in c["files"]
        if f["filename"] == "panelboard.pdf"
    ]
    assert len(gone) == 1
    # Listed for the record, with no key to send — never silently dropped.
    assert gone[0]["available"] is False and gone[0]["key"] is None
    assert gone[0]["prior"] is True


def test_resend_creates_a_new_linked_package_and_leaves_the_original_alone(monkeypatch):
    db, pkg_id, sent = _sent_package(monkeypatch)
    items = db.tables["submittal_package_items"]
    sa.record_verdicts(
        "p1", pkg_id,
        {"items": [{"id": items[0]["id"], "approval_status": "rejected"}]}, "u9",
    )

    res = sa.create_and_send(
        "p1",
        _body(groups=[{"material_category_id": "c1", "file_keys": ["att:a1"]}]),
        "u1",
        supersedes_package_id=pkg_id,
    )
    assert res["send_status"] == "sent"
    assert res["number"] == 2

    packages = {p["id"]: p for p in db.tables["submittal_packages"]}
    assert packages[res["package_id"]]["supersedes_package_id"] == pkg_id
    # The original's verdicts are the record of what the GC said the first time.
    assert packages[pkg_id]["approval_status"] == "partial"
    assert packages[pkg_id]["supersedes_package_id"] is None
    # The GC sees which review this answers, in the subject line and the body.
    assert "Submittal 002 (Resubmittal of 001)" in sent["subject"]
    assert "resubmitted in response to your review of Submittal 001" in packages[
        res["package_id"]
    ]["body"]
    # A fresh package, so its own files start pending.
    new_items = [
        i for i in db.tables["submittal_package_items"] if i["package_id"] == res["package_id"]
    ]
    assert [i["approval_status"] for i in new_items] == ["pending"]


def test_resend_accepts_a_file_that_has_since_left_the_available_set(monkeypatch):
    """The bank link was removed after the send. The GC is still holding the file
    and asking for it again, so the package it came from keeps it selectable."""
    db, pkg_id, _ = _sent_package(monkeypatch)
    db.tables["pm_material_submittals"] = []
    assert not any(
        f["key"] == "bank:f1"
        for c in sa.available("p1")["categories"]
        for f in c["files"]
    )

    res = sa.create_and_send(
        "p1",
        _body(groups=[{"material_category_id": "c2", "file_keys": ["bank:f1"]}]),
        "u1",
        supersedes_package_id=pkg_id,
    )
    assert res["send_status"] == "sent"
    assert res["file_count"] == 1


def test_a_plain_send_still_refuses_a_key_only_a_package_knows(monkeypatch):
    """The prior-package index is scoped to the resend it was opened for — it
    must not leak into an ordinary send."""
    db, pkg_id, _ = _sent_package(monkeypatch)
    db.tables["pm_material_submittals"] = []
    with pytest.raises(ValueError, match="not found in this project"):
        sa.create_and_send(
            "p1", _body(groups=[{"material_category_id": "c2", "file_keys": ["bank:f1"]}]), "u1"
        )


def test_resending_another_projects_package_is_not_found(monkeypatch):
    db, pkg_id, _ = _sent_package(monkeypatch)
    db.tables["projects"].append({"id": "p2", "number": "26-999", "name": "Other"})
    db.tables["pm_details"].append({"project_id": "p2", "customer_gc_id": "gc1"})
    with pytest.raises(ValueError, match="not found"):
        sa.create_and_send("p2", _body(), "u1", supersedes_package_id=pkg_id)


def test_the_transmittal_marks_a_resubmittal(monkeypatch):
    html = submittal_pdf.render_package_html(
        PROJECT,
        number=4,
        groups=[("Lighting", [{"filename": "troffer-r1.pdf", "description": "2x4"}])],
        message=None,
        recipients=[{"name": "Dana", "email": "dana@gc.com"}],
        cc_recipients=[],
        supersedes_number=3,
    )
    assert "Resubmittal Transmittal" in html
    assert "Resubmittal of" in html and "003" in html
    assert "in response to your review of Submittal 003" in html

    plain = submittal_pdf.render_package_html(
        PROJECT, number=4, groups=[], message=None, recipients=[], cc_recipients=[]
    )
    assert "Resubmittal" not in plain
