"""The document promotion job (app/services/rfp_create_files) against the
in-memory fake Supabase from tests/test_rfp_email_ingest, with the two
storage services, the office preview, the queue lease and the bells stood
in (docs/RFP_CREATE.md section 5 and section 11).

Pinned:

- the pure decision table (`promotion_for`): every skip reason, the
  original-bytes formats, the converted-PDF formats, the hazard rule with
  `uri_links` allowed, the byte-marker rule with `/URI` allowed, an
  unknown hazard block (never clean), a missing digest;
- the OOXML container scan (`ooxml_container_verdict`) over tiny in-memory
  zips, and the converted-PDF fallback it triggers in `execute`;
- the category mapping and the filename rule;
- `execute` end to end: the claim, the downloads, the sha256 re-check (the
  original against the verified digest, a converted PDF against the
  manifest's conversion digest), the project_files rows (uploaded_by null,
  the two rfp columns, the preview status), one audit row per file, one
  drawing bell at the end, the record's counts and skipped list;
- the idempotent retry (rows already promoted are not re-promoted; a unique
  violation deletes the fresh object and reads as promoted);
- the transient path (claim released, `pending`, the queue's ladder), the
  queue marks and `current_status`, the stale-claim takeover, a lost claim
  raised as transient, the pending-first fence helpers and `retryable`.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.roles import Role
from app.sandbox import protocol
from app.services import llm_queue, rfp_create_files as rcf
from app.services import rfp_ingest_storage as rs
from tests.test_rfp_email_ingest import FakeDB

NOW = datetime(2026, 9, 16, 17, 0, 0, tzinfo=timezone.utc)
P1 = "p-1"
HV = "hv-1"
PDF = b"%PDF-1.7\n" + b"y" * 300 + b"\n%%EOF\n"
DOC_PDF = b"%PDF-1.4\nconverted\n%%EOF\n"
# The module's own drawing bell, kept before the autouse fixture stands it in.
_REAL_NOTIFY_DRAWINGS = rcf._notify_drawings


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FilesDB(FakeDB):
    """The ingest fake plus the PARTIAL unique index on project_files
    (project_id, rfp_sandbox_file_id) where the sandbox id is set."""

    unique = {**FakeDB.unique}

    def table(self, name):
        query = super().table(name)
        inherited = query._check_unique

        def check(rows, payload):
            inherited(rows, payload)
            if name == "project_files":
                sid = payload.get("rfp_sandbox_file_id")
                if sid and any(
                    r.get("project_id") == payload.get("project_id") and r.get("rfp_sandbox_file_id") == sid
                    for r in rows
                ):
                    raise Exception(
                        'duplicate key value violates unique constraint '
                        '"project_files_rfp_sandbox_file_uidx" (23505)'
                    )
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


def _entry(**over):
    row = {"file_path": "Bid_Drawings/Electrical/E-1.pdf", "kind": "drawing", "discipline": "Electrical",
           "sandbox_file_id": "f-1", "status": "accepted"}
    row.update(over)
    return row


def _markers(**over):
    """The sniff's byte-marker block: every protocol marker at zero except
    the web links, which every spec book carries."""
    out = {m.decode("ascii"): 0 for m in protocol.BYTE_MARKERS}
    out["/URI"] = 3
    out.update(over)
    return out


def _manifest(markers=None, *, conversion_sha=None, **over):
    """A verified row's manifest: the sniff block (None drops it) and, for a
    converted file, the conversion digest the sandbox recorded."""
    out = {"identity": {"filename": "E-1.pdf"}, "verdict": {"status": "verified"}}
    if markers is not None:
        out["sniff"] = {"verdict": None, "source_format": "pdf", "byte_markers": markers}
    if conversion_sha is not None:
        out["conversion"] = {"engine": "gotenberg", "pdf_sha256": conversion_sha, "pdf_bytes": 1}
    out.update(over)
    return out


def _file(**over):
    row = {"id": "f-1", "run_id": "run-1", "status": "verified", "hazards": {"uri_links": 3},
           "quarantine_path": "run-1/f-1/source.pdf", "source_format": "pdf", "converted_path": None,
           "filename": "E-1.pdf", "size_bytes": len(PDF), "sha256": _sha(PDF),
           "manifest": _manifest(_markers())}
    row.update(over)
    return row


def _converted_file(fmt, **over):
    """A legacy office row whose converted PDF carries the manifest digest."""
    row = _file(source_format=fmt, quarantine_path=f"run-1/f-1/source.{fmt}",
                converted_path="run-1/f-1/converted.pdf",
                manifest=_manifest(_markers(), conversion_sha=_sha(DOC_PDF)))
    row.update(over)
    return row


def _record(**over):
    row = {"project_id": P1, "source_kind": "rfp_email", "harvest_id": HV, "files_status": "pending",
           "files_claim_token": None, "files_claimed_at": None, "files_promoted": 0, "files_skipped": [],
           "files_error": None, "last_error": None}
    row.update(over)
    return row


@pytest.fixture
def db():
    fake = FilesDB({
        "rfp_created_projects": [], "rfp_harvests": [], "rfp_ingest_files": [], "project_files": [],
        "projects": [{"id": P1, "number": "26.9.7204", "name": "Warehouse HVAC"}],
        "estimator_assignments": [], "notifications": [], "audit_log": [], "llm_jobs": [],
    })
    fake.defaults = {
        **FakeDB.defaults,
        "llm_jobs": {"status": lambda: "queued", "attempts": lambda: 0,
                     "created_at": lambda: NOW.isoformat()},
    }
    return fake


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, rfp_ingest_enabled=True, llm_queue_enabled=True,
                    rfp_ingest_scratch_dir=str(tmp_path))


@pytest.fixture
def store():
    """The bytes behind (bucket, path); an exception instance is raised
    instead. Records uploads and deletes."""
    return {"objects": {}, "uploads": [], "deletes": [], "previews": [], "bells": [], "audits": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch, db, settings, store):
    monkeypatch.setattr(rcf, "get_settings", lambda: settings)
    monkeypatch.setattr(rcf, "get_supabase", lambda: db)
    monkeypatch.setattr(rcf, "_now", lambda: NOW)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: settings)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)

    def download(bucket, path, dest, *, max_bytes):
        obj = store["objects"].get((bucket, path))
        if obj is None:
            raise rs.RfpStorageNotFound("The stored file was not found.")
        if isinstance(obj, Exception):
            raise obj
        if len(obj) > max_bytes:
            raise rs.RfpStorageTooLarge("The stored file is larger than the cap.")
        Path(dest).write_bytes(obj)
        return len(obj)

    monkeypatch.setattr(rcf.rs, "download_to_file", download)
    monkeypatch.setattr(rcf.storage, "upload_file",
                        lambda path, content, ctype, **k: store["uploads"].append((path, content, ctype)))
    monkeypatch.setattr(rcf.storage, "delete_file", lambda path: store["deletes"].append(path))
    monkeypatch.setattr(rcf.office_preview, "is_convertible",
                        lambda filename, category: str(filename).lower().endswith((".docx", ".xlsx")))
    monkeypatch.setattr(rcf.office_preview, "generate_preview", lambda file_id: store["previews"].append(file_id))
    monkeypatch.setattr(rcf, "audit",
                        lambda actor, action, entity, entity_id, payload=None: store["audits"].append(
                            (actor, action, entity, entity_id, payload)))
    monkeypatch.setattr(
        rcf, "_notify_drawings",
        lambda sb, pid, count: store["bells"].append((pid, count)) if count else None,
    )


def _seed(db, entries, files, record=None):
    db.tables["rfp_created_projects"].append(record or _record())
    db.tables["rfp_harvests"].append({"id": HV, "files": entries})
    db.tables["rfp_ingest_files"].extend(files)


def _rec(db):
    return db.tables["rfp_created_projects"][0]


# ── promotion_for (the decision table) ────────────────────────────────────


@pytest.mark.parametrize(
    "entry, file_row, reason",
    [
        (_entry(sandbox_file_id=None, status="too_large"), None, "too_large"),
        (_entry(sandbox_file_id=None, status="rejected"), None, "rejected"),
        (_entry(status="download_failed"), _file(), "download_failed"),
        (_entry(status="skipped_cap"), _file(), "skipped_cap"),
        (_entry(status="expanded"), _file(), "expanded"),
        (_entry(sandbox_file_id=None, status=None), None, "not_in_sandbox"),
        (_entry(), None, "not_in_sandbox"),
        (_entry(), _file(status="verified_with_gaps"), "pages_unverified"),
        (_entry(), _file(status="rejected"), "rejected"),
        (_entry(), _file(status="failed"), "failed"),
        (_entry(), _file(status="pending"), "pending"),
        (_entry(), _file(status="running"), "running"),
        (_entry(), _file(hazards={"uri_links": 9, "javascript_actions": 1, "launch_actions": 2}),
         "hazard:javascript_actions,launch_actions"),
        (_entry(), _file(hazards={"file_attachments": "1"}), "hazard:file_attachments"),
        (_entry(), _file(hazards={"xfa_packets": "many"}), "hazard:xfa_packets"),
        # The byte markers are no longer read from the manifest at decision
        # time: the promoted bytes themselves are scanned in _fetch_verified
        # (test_pdf_marker_keys / test_fetch_verified_scans_pdf_bytes).
        (_entry(), _file(hazards={"javascript_actions": 1}, manifest=_manifest(_markers(**{"/JS": 1}))),
         "hazard:javascript_actions"),
        # An unknown hazard block is never clean: a hazards column that is
        # not a dict. The manifest's marker block no longer matters here.
        (_entry(), _file(hazards=None), "hazards_unknown"),
        (_entry(), _file(hazards="x"), "hazards_unknown"),
        (_entry(), _file(hazards="none"), "hazards_unknown"),
        (_entry(), _file(hazards=None, manifest=None), "hazards_unknown"),
        # An original with no verified digest is never promoted.
        (_entry(), _file(sha256=None), "no_digest"),
        (_entry(), _file(sha256="  "), "no_digest"),
        (_entry(), _file(source_format="docx", sha256=""), "no_digest"),
        (_entry(), _file(quarantine_path=None), "no_source_object"),
        (_entry(), _file(source_format="doc", converted_path=None), "no_source_object"),
        (_entry(), _file(source_format="rtf"), "unsupported_format"),
    ],
)
def test_promotion_for_skips_with_the_documented_reason(entry, file_row, reason):
    out = rcf.promotion_for(entry, file_row)
    assert isinstance(out, rcf.Skip) and out.reason == reason


def test_promotion_for_promotes_originals_and_converted_pdfs():
    out = rcf.promotion_for(_entry(status="reused"), _file())
    assert out == rcf.Promote(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf", "E-1.pdf", "application/pdf", True,
                              expected_sha=_sha(PDF))
    assert out.ooxml_scan is False
    # The verified digest is compared lowercased.
    assert rcf.promotion_for(_entry(), _file(sha256=_sha(PDF).upper())).expected_sha == _sha(PDF)
    for fmt, ctype in (
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ):
        out = rcf.promotion_for(_entry(file_path=f"Specs/Div 26.{fmt}"), _file(source_format=fmt, quarantine_path=f"run-1/f-1/source.{fmt}"))
        assert isinstance(out, rcf.Promote)
        assert (out.bucket, out.path, out.filename, out.content_type, out.check_sha) == (
            rs.QUARANTINE_BUCKET, f"run-1/f-1/source.{fmt}", f"Div 26.{fmt}", ctype, True
        )
        assert out.note is None and out.expected_sha == _sha(PDF) and out.ooxml_scan is True
    for fmt in ("doc", "xls"):
        out = rcf.promotion_for(_entry(file_path=f"Old/Spec Book.{fmt}"), _converted_file(fmt))
        assert isinstance(out, rcf.Promote)
        assert (out.bucket, out.path, out.filename, out.content_type, out.check_sha) == (
            rs.DERIVED_BUCKET, "run-1/f-1/converted.pdf", "Spec Book.pdf", "application/pdf", False
        )
        assert out.note == f"Converted from the original .{fmt} by the ingestion sandbox"
        assert out.expected_sha == _sha(DOC_PDF) and out.ooxml_scan is False
    # A conversion the manifest recorded no digest for is promoted but noted.
    out = rcf.promotion_for(_entry(file_path="a.doc"), _converted_file("doc", manifest=_manifest(_markers())))
    assert out.expected_sha is None
    assert out.note == "Converted from the original .doc by the ingestion sandbox; converted_unverified"
    # A verified file with only web links is promoted, whichever scan saw them.
    assert isinstance(rcf.promotion_for(_entry(), _file(hazards={"uri_links": 40})), rcf.Promote)
    assert isinstance(rcf.promotion_for(_entry(), _file(manifest=_manifest(_markers(**{"/URI": 900})))), rcf.Promote)
    assert isinstance(rcf.promotion_for(_entry(), _file(hazards={})), rcf.Promote)
    assert rcf.hazard_keys({"uri_links": 1, "attachments": 0, "remote_goto": 2}) == ["remote_goto"]
    assert rcf.hazard_keys(None) is None and rcf.hazard_keys("x") is None
    assert isinstance(rcf.promotion_for(_entry(), _file(manifest=None)), rcf.Promote)
    assert rcf.conversion_sha(_manifest(_markers(), conversion_sha=" ABC ")) == "abc"
    assert rcf.conversion_sha(_manifest(_markers())) is None and rcf.conversion_sha("x") is None
    # The OOXML fallback decision: the converted PDF with the reason on the note.
    row = _converted_file("docx")
    out = rcf.converted_promotion(row, "Div 26.docx", "docx", why="vba_project")
    assert (out.bucket, out.path, out.filename, out.content_type, out.check_sha) == (
        rs.DERIVED_BUCKET, "run-1/f-1/converted.pdf", "Div 26.pdf", "application/pdf", False
    )
    assert out.note == "Converted from the original .docx by the ingestion sandbox; converted:vba_project"
    assert out.expected_sha == _sha(DOC_PDF)
    # Without a converted copy the container reason is the skip reason.
    assert rcf.converted_promotion(_file(source_format="docx"), "a.docx", "docx", why="dde") == rcf.Skip("ooxml:dde")


# ── The OOXML container scan ──────────────────────────────────────────────


def _zip(members: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


_CT_DOCX = (
    b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument'
    b'.wordprocessingml.document.main+xml"/></Types>'
)
_RELS_ROOT = (
    b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    b'officeDocument" Target="word/document.xml"/></Relationships>'
)
_RELS_DOC = (
    b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    b'hyperlink" Target="https://example.com/spec" TargetMode="External"/>'
    b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    b'styles" Target="styles.xml"/></Relationships>'
)


def _docx(**extra: bytes) -> io.BytesIO:
    members = {
        "[Content_Types].xml": _CT_DOCX,
        "_rels/.rels": _RELS_ROOT,
        "word/_rels/document.xml.rels": _RELS_DOC,
        "word/document.xml": b"<w:document/>",
        "word/styles.xml": b"<w:styles/>",
        "docProps/core.xml": b"<cp:coreProperties/>",
    }
    members.update(extra)
    return _zip(members)


def test_ooxml_container_verdict_passes_a_plain_docx_and_names_each_refusal():
    # A plain document: an ordinary external hyperlink is fine.
    assert rcf.ooxml_container_verdict(_docx()) is None
    # A VBA project, wherever it sits; a binary part under word/ or xl/.
    assert rcf.ooxml_container_verdict(_docx(**{"word/vbaProject.bin": b"\xd0\xcf"})) == "vba_project"
    assert rcf.ooxml_container_verdict(_docx(**{"xl/VBAPROJECT.BIN": b"x"})) == "vba_project"
    assert rcf.ooxml_container_verdict(_docx(**{"word/activeX/activeX1.bin": b"x"})) == "binary_part"
    assert rcf.ooxml_container_verdict(_docx(**{"xl/printerSettings/p1.bin": b"x"})) == "binary_part"
    # Embedded objects, by folder or by name.
    assert rcf.ooxml_container_verdict(_docx(**{"word/embeddings/oleObject1.bin": b"x"})) == "embedding"
    assert rcf.ooxml_container_verdict(_docx(**{"word/embeddings/Sheet1.xlsx": b"x"})) == "embedding"
    assert rcf.ooxml_container_verdict(_docx(**{"word/media/oleObject7.emf": b"x"})) == "embedding"
    # An external workbook link.
    assert rcf.ooxml_container_verdict(_docx(**{"xl/externalLinks/externalLink1.xml": b"<x/>"})) == "external_link"
    # An external relationship of a type that reaches out of the document.
    for word, rel_type in (
        ("attachedtemplate", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"),
        ("oleobject", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject"),
        ("frame", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/frame"),
        ("externallink", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink"),
        ("package", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"),
    ):
        rels = (
            b'<Relationships><Relationship Id="rId9" Type="' + rel_type.encode() +
            b'" Target="file:///C:/evil.dotm" TargetMode="External"/></Relationships>'
        )
        assert rcf.ooxml_container_verdict(_docx(**{"word/_rels/settings.xml.rels": rels})) == f"external_rel:{word}"
    # The same types with an internal target are the document's own parts.
    internal = (
        b'<Relationships><Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        b'relationships/oleObject" Target="embeddings/x.bin"/></Relationships>'
    )
    assert rcf.ooxml_container_verdict(_docx(**{"word/_rels/settings.xml.rels": internal})) is None
    # Single-quoted attributes and a case-shifted TargetMode still count.
    quoted = (
        b"<Relationships><Relationship Id='rId9' Type='.../relationships/attachedTemplate' "
        b"Target='http://x/t.dotm' targetmode='EXTERNAL'/></Relationships>"
    )
    assert rcf.ooxml_container_verdict(_docx(**{"word/_rels/settings.xml.rels": quoted})) == "external_rel:attachedtemplate"
    # DDE anywhere in an inspected member; a macro-enabled content type.
    dde = b'<Relationships><Relationship Id="r" Type="x" Target="ddeLink"/></Relationships>'
    assert rcf.ooxml_container_verdict(_docx(**{"word/_rels/document.xml.rels": dde})) == "dde"
    assert rcf.ooxml_container_verdict(_docx(**{"[Content_Types].xml": _CT_DOCX.replace(b"</Types>", b"<!-- DDEAUTO --></Types>")})) == "dde"
    macro_ct = _CT_DOCX.replace(b"wordprocessingml.document.main+xml", b"wordprocessingml.document.macroEnabled.main+xml")
    assert rcf.ooxml_container_verdict(_docx(**{"[Content_Types].xml": macro_ct})) == "macro_enabled"
    # The bounds: too many members, an inspected member over the cap.
    many = {f"word/media/image{i}.png": b"p" for i in range(rcf.OOXML_MAX_MEMBERS)}
    assert rcf.ooxml_container_verdict(_docx(**many)) == "too_many_members"
    fat = b"<Relationships>" + b" " * (rcf.OOXML_MAX_MEMBER_BYTES + 1) + b"</Relationships>"
    assert rcf.ooxml_container_verdict(_docx(**{"word/_rels/document.xml.rels": fat})) == "member_too_large"
    # Not a zip at all, or a truncated one.
    assert rcf.ooxml_container_verdict(io.BytesIO(b"%PDF-1.7 not a zip")) == "bad_zip"
    assert rcf.ooxml_container_verdict(io.BytesIO(_docx().getvalue()[:40])) == "bad_zip"


def test_ooxml_container_verdict_reads_a_path_too(tmp_path):
    path = tmp_path / "spec.docx"
    path.write_bytes(_docx().getvalue())
    assert rcf.ooxml_container_verdict(path) is None
    path.write_bytes(_docx(**{"word/vbaProject.bin": b"x"}).getvalue())
    assert rcf.ooxml_container_verdict(str(path)) == "vba_project"
    assert rcf.ooxml_container_verdict(tmp_path / "missing.docx") == "bad_zip"


def test_category_for_and_entry_name():
    assert rcf.category_for({"kind": "drawing", "discipline": "Electrical"}) == "electrical_drawing"
    assert rcf.category_for({"kind": "drawing", "discipline": "26-ELECTRICAL"}) == "electrical_drawing"
    assert rcf.category_for({"kind": "drawing", "discipline": "Architectural"}) == "drawing"
    assert rcf.category_for({"kind": "drawing", "discipline": None}) == "drawing"
    assert rcf.category_for({"kind": "specification", "discipline": "Electrical"}) == "specification"
    assert rcf.category_for({"kind": "other"}) == "other"
    assert rcf.category_for({"kind": None}) == "other" and rcf.category_for({}) == "other"
    assert rcf.entry_name({"file_path": "Bid_Drawings/Current/E-1.pdf"}) == "E-1.pdf"
    assert rcf.entry_name({"file_path": "C:\\docs\\Plan Set.pdf"}) == "Plan Set.pdf"
    assert rcf.entry_name({"file_name": "Plans.pdf"}) == "Plans.pdf"
    assert rcf.entry_name({"file_path": "", "sandbox_file_id": "f-9"}, {"filename": "stored.pdf"}) == "stored.pdf"
    assert rcf.entry_name({"sandbox_file_id": "f-9"}) == "f-9"
    assert len(rcf.entry_name({"file_path": "x" * 400})) == 200


# ── execute ───────────────────────────────────────────────────────────────


def test_execute_promotes_the_verified_files_records_the_rest_and_rings_once(db, store):
    entries = [
        _entry(),
        _entry(file_path="Bid_Drawings/Architectural/A-1.pdf", discipline="Architectural", sandbox_file_id="f-2"),
        _entry(file_path="Specs/26.pdf", kind="specification", discipline=None, sandbox_file_id="f-3"),
        _entry(file_path="Old/Spec Book.doc", kind="other", discipline=None, sandbox_file_id="f-4"),
        _entry(file_path="Bid_Drawings/big.pdf", sandbox_file_id=None, status="too_large"),
        _entry(file_path="Specs/gone.pdf", kind="specification", sandbox_file_id="f-5"),
        _entry(file_path="Specs/js.pdf", kind="specification", sandbox_file_id="f-6"),
        _entry(file_path="Specs/fat.pdf", kind="specification", sandbox_file_id="f-7"),
    ]
    files = [
        _file(),
        _file(id="f-2", quarantine_path="run-1/f-2/source.pdf"),
        _file(id="f-3", quarantine_path="run-1/f-3/source.pdf", status="verified_with_gaps"),
        _converted_file("doc", id="f-4", quarantine_path="run-1/f-4/source.doc",
                        converted_path="run-1/f-4/converted.pdf", sha256="ignored"),
        _file(id="f-5", quarantine_path="run-1/f-5/source.pdf"),
        _file(id="f-6", quarantine_path="run-1/f-6/source.pdf", hazards={"javascript_actions": 1}),
        _file(id="f-7", quarantine_path="run-1/f-7/source.pdf", size_bytes=10),
    ]
    _seed(db, entries, files)
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf")] = PDF
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-2/source.pdf")] = PDF
    store["objects"][(rs.DERIVED_BUCKET, "run-1/f-4/converted.pdf")] = DOC_PDF
    # One byte over the live upload cap, read from settings so the 0132 raise
    # (300 MB -> 450 MB) does not silently turn this into a passing file.
    from app.core.config import get_settings

    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-7/source.pdf")] = (
        b"x" * (get_settings().upload_max_bytes + 1)
    )
    assert rcf.execute(P1) is None
    rows = db.tables["project_files"]
    assert [(r["category"], r["filename"]) for r in rows] == [
        ("electrical_drawing", "E-1.pdf"), ("drawing", "A-1.pdf"), ("other", "Spec Book.pdf"),
    ]
    first = rows[0]
    assert first["project_id"] == P1 and first["uploaded_by"] is None and first["mime_type"] == "application/pdf"
    assert first["size_bytes"] == len(PDF) and first["preview_status"] == "none"
    assert first["rfp_harvest_id"] == HV and first["rfp_sandbox_file_id"] == "f-1"
    assert first["storage_path"].startswith(f"{P1}/electrical_drawing/") and first["storage_path"].endswith("-E-1.pdf")
    assert first["note"] is None and first["doc_type"] is None and first["estimator_deliverable"] is False
    assert rows[2]["note"] == "Converted from the original .doc by the ingestion sandbox"
    assert rows[2]["mime_type"] == "application/pdf" and rows[2]["size_bytes"] == len(DOC_PDF)
    # The bytes uploaded are the verified ones, under the row's own key.
    assert [(u[0], u[2]) for u in store["uploads"]] == [(r["storage_path"], r["mime_type"]) for r in rows]
    assert store["uploads"][0][1] == PDF and store["uploads"][2][1] == DOC_PDF
    assert store["deletes"] == [] and store["previews"] == []
    # One audit row per promoted file, source rfp.
    assert [(a[1], a[3], a[4]) for a in store["audits"]] == [
        ("file.upload", r["id"], {"category": r["category"], "source": "rfp"}) for r in rows
    ]
    # One bell for the two drawings, at the end.
    assert store["bells"] == [(P1, 2)]
    rec = _rec(db)
    assert rec["files_status"] == "complete" and rec["files_promoted"] == 3 and rec["files_error"] is None
    assert rec["files_claim_token"] is None and rec["files_claimed_at"] is None
    assert rec["files_skipped"] == [
        {"file_path": "26.pdf", "reason": "pages_unverified"},
        {"file_path": "big.pdf", "reason": "too_large"},
        {"file_path": "gone.pdf", "reason": "missing_in_storage"},
        {"file_path": "js.pdf", "reason": "hazard:javascript_actions"},
        {"file_path": "fat.pdf", "reason": "too_large"},
    ]
    assert rcf.current_status(P1) == "done"


def test_execute_skips_a_file_whose_bytes_changed_since_verification(db, store):
    _seed(db, [_entry()], [_file(sha256=_sha(b"other bytes"))])
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf")] = PDF
    rcf.execute(P1)
    assert db.tables["project_files"] == [] and store["uploads"] == []
    rec = _rec(db)
    assert rec["files_status"] == "complete" and rec["files_promoted"] == 0
    assert rec["files_skipped"] == [{"file_path": "E-1.pdf", "reason": "changed_since_verification"}]
    assert store["bells"] == []
    # A converted PDF is checked against the conversion digest the manifest
    # recorded: a mismatch skips; the original's own digest is not consulted.
    _rec(db).update(files_status="failed")
    db.tables["rfp_harvests"][0]["files"] = [
        _entry(file_path="a.doc", kind="other", sandbox_file_id="f-4"),
        _entry(file_path="b.xls", kind="other", sandbox_file_id="f-5"),
        _entry(file_path="c.doc", kind="other", sandbox_file_id="f-6"),
    ]
    db.tables["rfp_ingest_files"].extend([
        _converted_file("doc", id="f-4", quarantine_path="run-1/f-4/source.doc",
                        converted_path="run-1/f-4/converted.pdf", sha256="nope"),
        _converted_file("xls", id="f-5", quarantine_path="run-1/f-5/source.xls",
                        converted_path="run-1/f-5/converted.pdf",
                        manifest=_manifest(_markers(), conversion_sha=_sha(b"other"))),
        # No conversion digest in the manifest: promoted, noted unverified.
        _converted_file("doc", id="f-6", quarantine_path="run-1/f-6/source.doc",
                        converted_path="run-1/f-6/converted.pdf", manifest=_manifest(_markers())),
    ])
    for fid in ("f-4", "f-5", "f-6"):
        store["objects"][(rs.DERIVED_BUCKET, f"run-1/{fid}/converted.pdf")] = DOC_PDF
    rcf.execute(P1)
    assert [r["filename"] for r in db.tables["project_files"]] == ["a.pdf", "c.pdf"]
    assert db.tables["project_files"][0]["note"] == "Converted from the original .doc by the ingestion sandbox"
    assert db.tables["project_files"][1]["note"] == (
        "Converted from the original .doc by the ingestion sandbox; converted_unverified"
    )
    assert _rec(db)["files_skipped"] == [{"file_path": "b.xls", "reason": "converted_sha_mismatch"}]
    assert _rec(db)["files_promoted"] == 2


def test_execute_scans_an_ooxml_original_and_falls_back_to_the_converted_pdf(db, store):
    clean = _docx().getvalue()
    macro = _docx(**{"word/vbaProject.bin": b"x"}).getvalue()
    entries = [
        _entry(file_path="Specs/Div 26.docx", kind="specification", discipline=None, sandbox_file_id="f-1"),
        _entry(file_path="Specs/Div 27.docx", kind="specification", discipline=None, sandbox_file_id="f-2"),
        _entry(file_path="Specs/Div 28.docx", kind="specification", discipline=None, sandbox_file_id="f-3"),
        _entry(file_path="Specs/Div 29.docx", kind="specification", discipline=None, sandbox_file_id="f-4"),
    ]
    files = [
        _converted_file("docx", id="f-1", quarantine_path="run-1/f-1/source.docx", sha256=_sha(clean),
                        converted_path="run-1/f-1/converted.pdf"),
        # Refused by the container scan: the converted PDF lands instead, noted.
        _converted_file("docx", id="f-2", quarantine_path="run-1/f-2/source.docx", sha256=_sha(macro),
                        converted_path="run-1/f-2/converted.pdf"),
        # Refused, and the converted copy does not match its digest: skipped.
        _converted_file("docx", id="f-3", quarantine_path="run-1/f-3/source.docx", sha256=_sha(macro),
                        converted_path="run-1/f-3/converted.pdf",
                        manifest=_manifest(_markers(), conversion_sha=_sha(b"other"))),
        # Refused with no converted copy at all: the container reason is the skip.
        _converted_file("docx", id="f-4", quarantine_path="run-1/f-4/source.docx", sha256=_sha(macro),
                        converted_path=None),
    ]
    _seed(db, entries, files)
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.docx")] = clean
    for fid in ("f-2", "f-3", "f-4"):
        store["objects"][(rs.QUARANTINE_BUCKET, f"run-1/{fid}/source.docx")] = macro
        store["objects"][(rs.DERIVED_BUCKET, f"run-1/{fid}/converted.pdf")] = DOC_PDF
    rcf.execute(P1)
    rows = db.tables["project_files"]
    assert [(r["filename"], r["mime_type"], r["note"]) for r in rows] == [
        ("Div 26.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", None),
        ("Div 27.pdf", "application/pdf",
         "Converted from the original .docx by the ingestion sandbox; converted:vba_project"),
    ]
    assert rows[0]["rfp_sandbox_file_id"] == "f-1" and rows[1]["rfp_sandbox_file_id"] == "f-2"
    assert store["uploads"][0][1] == clean and store["uploads"][1][1] == DOC_PDF
    assert rows[0]["preview_status"] == "pending" and rows[1]["preview_status"] == "none"
    rec = _rec(db)
    assert rec["files_status"] == "complete" and rec["files_promoted"] == 2
    assert rec["files_skipped"] == [
        {"file_path": "Div 28.docx", "reason": "converted_sha_mismatch"},
        {"file_path": "Div 29.docx", "reason": "ooxml:vba_project"},
    ]


def test_execute_is_idempotent_on_a_retry(db, store):
    _seed(db, [_entry(), _entry(file_path="Specs/26.pdf", kind="specification", sandbox_file_id="f-2")],
          [_file(), _file(id="f-2", quarantine_path="run-1/f-2/source.pdf")],
          record=_record(files_status="failed", files_error="old"))
    db.tables["project_files"].append({"id": "pf-1", "project_id": P1, "category": "electrical_drawing",
                                       "filename": "E-1.pdf", "rfp_sandbox_file_id": "f-1"})
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf")] = PDF
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-2/source.pdf")] = PDF
    rcf.execute(P1)
    # f-1 was already in the project: not downloaded, not uploaded, still counted.
    assert [u[0].split("/")[1] for u in store["uploads"]] == ["specification"]
    assert len(db.tables["project_files"]) == 2
    rec = _rec(db)
    assert rec["files_status"] == "complete" and rec["files_promoted"] == 2 and rec["files_error"] is None
    assert store["bells"] == []   # the drawing was promoted by the earlier run
    # A unique violation on the insert itself (a concurrent run): the fresh
    # object is deleted and the file reads as promoted.
    db.tables["rfp_created_projects"][0].update(files_status="failed")
    db.tables["project_files"] = [db.tables["project_files"][0]]
    original_insert = FilesDB.table

    def racing_table(self, name):
        query = original_insert(self, name)
        if name == "project_files":
            inherited = query._check_unique

            def check(rows, payload):
                if payload.get("rfp_sandbox_file_id") == "f-2":
                    raise Exception('duplicate key value violates unique constraint (23505)')
                inherited(rows, payload)

            query._check_unique = check
        return query

    db.table = racing_table.__get__(db, FilesDB)
    store["uploads"].clear()
    rcf.execute(P1)
    assert len(store["uploads"]) == 1 and store["deletes"] == [store["uploads"][0][0]]
    assert _rec(db)["files_promoted"] == 2 and _rec(db)["files_status"] == "complete"


def test_execute_promotes_the_harvest_the_payload_names_not_the_records(db, store):
    """The recent-project link (docs/RFP_CREATE.md 4.6) enqueues THIS
    invitation's harvest against a project whose record belongs to another
    invitation's. The payload's harvest wins, and the promoted rows carry it,
    so the link never promotes the other invitation's documents instead."""
    _seed(db, [_entry()], [_file()], record=_record(harvest_id="hv-other"))
    db.tables["rfp_harvests"].append({
        "id": "hv-other", "split_job_id": None,
        "files": [_entry(file_path="Other/O-1.pdf", sandbox_file_id="f-other")],
    })
    db.tables["rfp_ingest_files"].append(_file(id="f-other", quarantine_path="run-1/f-other/source.pdf"))
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf")] = PDF
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-other/source.pdf")] = PDF
    rcf.execute(P1, harvest_id=HV)
    (row,) = db.tables["project_files"]
    assert row["rfp_sandbox_file_id"] == "f-1" and row["rfp_harvest_id"] == HV
    assert _rec(db)["files_status"] == "complete" and _rec(db)["harvest_id"] == "hv-other"
    # Without the payload id the job still falls back to the record's harvest.
    db.tables["project_files"].clear()
    db.tables["rfp_created_projects"][0].update(files_status="failed")
    rcf.execute(P1)
    (row,) = db.tables["project_files"]
    assert row["rfp_sandbox_file_id"] == "f-other" and row["rfp_harvest_id"] == "hv-other"


def test_execute_takes_the_split_job_from_the_harvest_it_promotes(db, store, monkeypatch):
    """`rfp_created_projects.split_job_id` is a copy of the RECORD's harvest's
    (docs/RFP_SPLIT.md 4), so a payload harvest brings its own."""
    seen: list = []
    monkeypatch.setattr(rcf.rfp_split, "split_rows_for_job",
                        lambda sb, job_id: seen.append(job_id) or {})
    _seed(db, [_entry()], [_file()], record=_record(harvest_id=HV, split_job_id="sj-record"))
    db.tables["rfp_harvests"][0]["split_job_id"] = "sj-harvest"
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf")] = PDF
    rcf.execute(P1, harvest_id=HV)
    assert seen == ["sj-harvest"]
    # A harvest with no split job keeps the record's value.
    db.tables["rfp_harvests"][0]["split_job_id"] = None
    db.tables["rfp_created_projects"][0].update(files_status="failed")
    rcf.execute(P1)
    assert seen == ["sj-harvest", "sj-record"]


def test_execute_transient_storage_trouble_releases_the_claim_and_rides_the_ladder(db, store):
    _seed(db, [_entry(), _entry(file_path="Specs/26.pdf", kind="specification", sandbox_file_id="f-2")],
          [_file(), _file(id="f-2", quarantine_path="run-1/f-2/source.pdf")])
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf")] = PDF
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-2/source.pdf")] = rs.RfpStorageError("Storage returned HTTP 503.")
    with pytest.raises(rcf.RfpCreateFilesTransient) as exc:
        rcf.execute(P1)
    assert str(exc.value) == rcf._MSG_STORAGE
    rec = _rec(db)
    assert rec["files_status"] == "pending" and rec["files_claim_token"] is None
    assert rec["files_error"] == rcf._MSG_STORAGE and rec["files_promoted"] == 1
    assert len(db.tables["project_files"]) == 1
    # The queue's terminal mark after the ladder: failed with the sentence, fenced.
    rcf.mark_from_queue(P1, "pending", None)
    assert _rec(db)["files_status"] == "pending"
    rcf.mark_from_queue(P1, "failed", "The document promotion was interrupted (failed after 4 attempts)")
    assert _rec(db)["files_status"] == "failed"
    assert _rec(db)["files_error"] == "The document promotion was interrupted (failed after 4 attempts)"
    assert rcf.current_status(P1) == "done"
    _rec(db).update(files_status="complete")
    rcf.mark_from_queue(P1, "failed", "late")
    assert _rec(db)["files_status"] == "complete"
    assert rcf.current_status("nope") is None
    # An upload failure is transient too; a lost lease as well.
    _rec(db).update(files_status="pending")
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-2/source.pdf")] = PDF
    db.tables["project_files"].clear()

    def bad_upload(path, content, ctype, **k):
        raise RuntimeError("TLS reset")

    import app.services.storage as storage_mod

    real = storage_mod.upload_file
    storage_mod.upload_file = bad_upload
    try:
        with pytest.raises(rcf.RfpCreateFilesTransient):
            rcf.execute(P1)
    finally:
        storage_mod.upload_file = real
    assert _rec(db)["files_status"] == "pending"


def test_execute_claim_rules(db, store, settings, monkeypatch):
    _seed(db, [_entry()], [_file()],
          record=_record(files_status="running", files_claim_token="live", files_claimed_at=NOW.isoformat()))
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.pdf")] = PDF
    # A live claim: nothing is promoted, and the loss is transient (the
    # ladder retries; its exhaustion marks the record failed, never stranded).
    with pytest.raises(rcf.RfpCreateFilesTransient):
        rcf.execute(P1)
    assert db.tables["project_files"] == [] and _rec(db)["files_claim_token"] == "live"
    assert _rec(db)["files_status"] == "running"
    rcf.mark_from_queue(P1, "failed", "gave up")
    assert _rec(db)["files_status"] == "failed" and _rec(db)["files_claim_token"] is None
    _rec(db).update(files_status="running", files_claim_token="live", files_claimed_at=NOW.isoformat())
    # A claim older than the queue lease: taken over.
    _rec(db)["files_claimed_at"] = (NOW - timedelta(seconds=settings.llm_queue_lease_seconds + 1)).isoformat()
    rcf.execute(P1)
    assert len(db.tables["project_files"]) == 1 and _rec(db)["files_status"] == "complete"
    # A missing record is permanent; a record with no harvest completes empty.
    with pytest.raises(rcf.RfpCreateFilesPermanent):
        rcf.execute("p-none")
    db.tables["rfp_created_projects"].append(_record(project_id="p-2", harvest_id=None))
    rcf.execute("p-2")
    rec2 = next(r for r in db.tables["rfp_created_projects"] if r["project_id"] == "p-2")
    assert rec2["files_status"] == "complete" and rec2["files_promoted"] == 0 and rec2["files_skipped"] == []
    # A lost lease mid-run is transient.
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: False)
    db.tables["rfp_created_projects"].append(_record(project_id="p-3"))
    db.tables["project_files"].clear()
    with pytest.raises(rcf.RfpCreateFilesTransient):
        rcf.execute("p-3")


def test_convertible_files_get_a_preview_and_enqueue_shapes_the_job(db, store):
    docx = _docx().getvalue()
    _seed(db, [_entry(file_path="Specs/Div 26.docx", kind="specification", sandbox_file_id="f-1")],
          [_file(source_format="docx", quarantine_path="run-1/f-1/source.docx", filename="Div 26.docx",
                 sha256=_sha(docx))])
    store["objects"][(rs.QUARANTINE_BUCKET, "run-1/f-1/source.docx")] = docx
    rcf.execute(P1)
    (row,) = db.tables["project_files"]
    assert row["preview_status"] == "pending" and store["previews"] == [row["id"]]
    assert row["mime_type"].endswith("wordprocessingml.document")
    job = rcf.enqueue(P1, created_by="u-1")
    stored = db.tables["llm_jobs"][0]
    assert job["id"] == stored["id"] and stored["job_type"] == "rfp_create_files"
    assert stored["priority"] == 160 and stored["payload"] == {"project_id": P1}
    assert stored["project_id"] == P1 and stored["feature"] == "rfp_create"
    with pytest.raises(llm_queue.JobAlreadyActive):
        rcf.enqueue(P1, created_by="u-1")
    assert rcf.active_job(P1)["id"] == job["id"]
    assert rcf.error_message(rcf.RfpCreateFilesPermanent("gone"), "storage") == "gone"
    assert rcf.error_message(KeyError("x"), "storage") == rcf._MSG_INTERRUPTED


def test_retryable_and_the_pending_first_fence(db, settings):
    lease = settings.llm_queue_lease_seconds
    stale = (NOW - timedelta(seconds=lease + 1)).isoformat()
    fresh = (NOW - timedelta(seconds=lease - 5)).isoformat()
    for status in ("none", "failed"):
        assert rcf.retryable(_record(files_status=status), settings, NOW) is True
    for status in ("pending", "complete"):
        assert rcf.retryable(_record(files_status=status), settings, NOW) is False
    # `running` only under a claim older than the queue lease (or no claim at all).
    assert rcf.retryable(_record(files_status="running", files_claimed_at=fresh), settings, NOW) is False
    assert rcf.retryable(_record(files_status="running", files_claimed_at=stale), settings, NOW) is True
    assert rcf.retryable(_record(files_status="running", files_claimed_at=None), settings, NOW) is True
    assert rcf.retryable(_record(files_status="running", files_claimed_at="garbage"), settings, NOW) is True
    assert rcf.claim_is_stale(_record(files_status="pending", files_claimed_at=stale), settings, NOW) is False
    # mark_pending is a CAS from the exact status read; unmark puts it back.
    db.tables["rfp_created_projects"].append(
        _record(files_status="failed", files_error="old", files_claim_token="t", files_claimed_at=stale)
    )
    assert rcf.mark_pending(db, P1, "none") is False
    assert _rec(db)["files_status"] == "failed"
    assert rcf.mark_pending(db, P1, "failed") is True
    rec = _rec(db)
    assert rec["files_status"] == "pending" and rec["files_error"] is None
    assert rec["files_claim_token"] is None and rec["files_claimed_at"] is None
    assert rcf.mark_pending(db, P1, "failed") is False
    rcf.unmark_pending(db, P1, "failed")
    assert _rec(db)["files_status"] == "failed"
    rcf.unmark_pending(db, P1, "none")   # fenced on pending: nothing to undo
    assert _rec(db)["files_status"] == "failed"


def test_notify_drawings_rings_once_after_intake_and_never_during(db, monkeypatch):
    from app.services import workflow

    bells = []
    monkeypatch.setattr(rcf, "notify_role", lambda role, pid, type_, msg, **k: bells.append((role, type_, msg)))
    monkeypatch.setattr(rcf, "notify_user", lambda uid, pid, type_, msg, **k: bells.append((uid, type_, msg)))
    state = {"intake": {"status": "active", "current_task": "go_no_go"}}
    monkeypatch.setattr(workflow, "load_category_state", lambda pid: state)
    real = _REAL_NOTIFY_DRAWINGS
    real(db, P1, 2)
    assert bells == []
    state["intake"] = {"status": "complete", "current_task": "to_estimator"}
    db.tables["estimator_assignments"].append({"estimator_id": "est-1", "project_id": P1, "revoked_at": None,
                                               "expires_at": None})
    real(db, P1, 0)
    assert bells == []
    real(db, P1, 2)
    msg = "2 drawings added for 26.9.7204 Warehouse HVAC from the RFP invitation; re-check anything priced off them."
    assert bells == [
        (Role.ESTIMATING_ENGINEER_MATERIALS, "drawing_changed", msg),
        (Role.ESTIMATING_ENGINEER_LABOR, "drawing_changed", msg),
        ("est-1", "drawing_changed", msg),
    ]


def test_migration_0130_carries_the_status_checks_and_the_job_type():
    """docs/RFP_CREATE.md section 6: both status CHECKs gain create and
    created, the job type list gains rfp_create_files, the counter and the
    created table exist, and nothing rewrites a project number."""
    import re

    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    sql = (migrations / "0130_rfp_project_creation.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0130 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "—" not in sql and "–" not in sql
    email_check = re.search(r"add constraint rfp_emails_status_check check \(status in \(([^)]+)\)\)", sql, re.S)
    assert email_check
    email_values = {v.strip().strip("'") for v in email_check.group(1).replace("\n", ",").split(",") if v.strip()}
    from app.services import rfp_email_ingest as ingest

    # Frozen history: `split` joined the vocabulary in 0132 (docs/RFP_SPLIT.md
    # 2) and `blocked_sender` in 0133 (blocked senders), not here.
    assert email_values | {"split", "blocked_sender"} == (
        set(ingest.STATUS_PENDING) | set(ingest.STATUS_HUMAN) | set(ingest.STATUS_TERMINAL)
    )
    assert {"create", "created"} <= email_values
    portal_check = re.search(
        r"add constraint rfp_portal_invitations_status_check\s+check \(status in \(([^)]+)\)\)", sql, re.S
    )
    assert portal_check
    portal_values = {v.strip().strip("'") for v in portal_check.group(1).split(",")}
    from app.services import rfp_portal_ingest as portal

    # Frozen history: `split` joined in 0132 and the parked `historical`,
    # `expired`, `withdrawn` in 0136 (docs/RFP_BUILDINGCONNECTED.md 5).
    assert portal_values | {"split"} == set(portal.ALL_STATUSES) - {"historical", "expired", "withdrawn"}
    assert {"create", "created"} <= portal_values
    jobs = re.search(r"check \(job_type in \(([^)]+)\)\)", sql, re.S)
    assert jobs
    job_values = {v.strip().strip("'") for v in jobs.group(1).replace("\n", ",").split(",") if v.strip()}
    assert job_values == set(llm_queue.LLM_JOB_TYPES) | set(llm_queue.NON_LLM_JOB_TYPES)
    assert "rfp_create_files" in job_values
    assert "create table if not exists project_number_counter" in sql
    assert "create table if not exists rfp_created_projects" in sql
    assert "create or replace function public.next_project_number()" in sql
    assert not re.search(r"update projects\b", sql)
    assert "project_files_rfp_sandbox_file_uidx" in sql
    assert protocol.STATUS_VERIFIED == "verified"   # the promotion rule's verdict
    assert len(list(migrations.glob("0130_*.sql"))) == 1


def test_pdf_marker_keys_matches_name_tokens_only():
    """Every marker counts as a PDF name token, never as a prefix of a longer
    name: `/AAPL:Keywords` (Mac-made PDFs) is not `/AA`, `/URI` is allowed,
    and each marker is reported once, sorted."""
    assert rcf.pdf_marker_keys(b"%PDF-1.4\n<< /AAPL:Keywords [(x)] /Type /Catalog >>") == []
    assert rcf.pdf_marker_keys(b"/AA << /O 5 0 R >>") == ["/AA"]
    assert rcf.pdf_marker_keys(b"<</S/JavaScript/JS(app.alert(1))>>") == ["/JS", "/JavaScript"]
    assert rcf.pdf_marker_keys(b"/OpenAction 3 0 R /OpenAction<<>> /OpenActions 1") == ["/OpenAction"]
    assert rcf.pdf_marker_keys(b"/URI (https://example.com) /URI(x)") == []
    assert rcf.pdf_marker_keys(b"/Launch\n/EmbeddedFile]/XFA%/RichMedia{/GoToR") == sorted(
        ["/Launch", "/EmbeddedFile", "/XFA", "/RichMedia", "/GoToR"]
    )
    assert rcf.pdf_marker_keys(b"/JSX /JSON /Launcher /XFAB /GoToRemote") == []
    assert rcf.pdf_marker_keys(b"") == []
    assert rcf.pdf_marker_keys(b"/JS") == ["/JS"]


def test_fetch_verified_scans_pdf_bytes(monkeypatch, tmp_path):
    """After the digest check the PDF bytes are scanned: a scripted PDF is a
    marker Skip, a Mac-made PDF with /AAPL names is promoted, and a
    converted (non-original) PDF is scanned the same way."""
    import hashlib as _h

    def _serve(data: bytes):
        def fake_download(bucket, path, dest, *, max_bytes):
            dest.write_bytes(data)
            return len(data)
        monkeypatch.setattr(rcf.rs, "download_to_file", fake_download)

    scripted = b"%PDF-1.7\n1 0 obj << /OpenAction << /S /JavaScript /JS (x) >> >> endobj"
    _serve(scripted)
    decision = rcf.Promote("rfp-quarantine", "r/f/source.pdf", "a.pdf", "application/pdf", True,
                           expected_sha=_h.sha256(scripted).hexdigest())
    out = rcf._fetch_verified(decision, tmp_path / "a", 10_000)
    assert isinstance(out, rcf.Skip) and out.reason == "marker:/JS,/JavaScript,/OpenAction"

    mac = b"%PDF-1.4\n<< /AAPL:Keywords [(x)] /URI (https://g3electrical.com) >>"
    _serve(mac)
    decision = rcf.Promote("rfp-quarantine", "r/f/source.pdf", "b.pdf", "application/pdf", True,
                           expected_sha=_h.sha256(mac).hexdigest())
    assert rcf._fetch_verified(decision, tmp_path / "b", 10_000) == mac

    _serve(scripted)
    converted = rcf.Promote("rfp-derived", "r/f/converted.pdf", "c.pdf", "application/pdf", False)
    out = rcf._fetch_verified(converted, tmp_path / "c", 10_000)
    assert isinstance(out, rcf.Skip) and out.reason.startswith("marker:")

    # A non-PDF original (docx) is not byte-scanned here; the container scan owns it.
    _serve(b"PK\x03\x04 /JS")
    docx = rcf.Promote("rfp-quarantine", "r/f/source.docx", "d.docx",
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True,
                       expected_sha=_h.sha256(b"PK\x03\x04 /JS").hexdigest(), ooxml_scan=True)
    assert rcf._fetch_verified(docx, tmp_path / "d", 10_000) == b"PK\x03\x04 /JS"


def test_retryable_after_a_complete_run_with_skips():
    # These statuses answer before the lease is consulted, so no settings.
    skipped = [{"file_path": "a", "reason": "marker:/AA"}]
    assert rcf.retryable({"files_status": "complete", "files_skipped": skipped}, settings=None)
    assert not rcf.retryable({"files_status": "complete", "files_skipped": []}, settings=None)
    assert rcf.retryable({"files_status": "failed", "files_skipped": []}, settings=None)
