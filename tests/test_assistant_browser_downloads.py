"""Browser downloads, server side (docs/ASSISTANT_BROWSER_V2_PLAN.md 11.2 to 11.4, WI-15).

Invariants pinned here:

  sniff      the type comes from magic bytes only (never the extension); an
             executable, a script with NUL bytes, a corrupt zip, a macro
             project and non-UTF-8 "text" are refused; a name that lies gets
             the extension of the sniffed type;
  project    a download for a task with a project is written as project_files
             category other through files.add_team_file inside the assistant
             provenance scope (the row is stamped assistant), the file-add
             hooks (_after_file_added) run, the note names the origin only,
             and the record points at the file;
  staging    no project, or an owner who may not upload, keeps the file as a
             staged `browser_download` object for 7 days under 2 GB;
  refusals   size, hash and type mismatches, the per-file cap, the task caps
             and a full staging area answer status rejected (200) with a
             reason, store nothing, and are recorded; a retry of the same bytes
             answers the first answer and writes nothing twice;
  failure    a record that fails removes the project file or the object;
  multipart  the worker route is a known multipart upload route, so the
             upload cap (not the JSON cap) applies to it;
  add        "Add to project" claims once, re-verifies the bytes, writes as the
             person (human provenance) and deletes the staged object;
  purge      expired staged objects go; a legal-hold owner's never do.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.assistant import access, repo
from app.core import provenance, supabase_client
from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import files
from app.services import assistant_browser_downloads as downloads
from app.services import assistant_staging as staging
from app.services import notifications, storage
from app.services.browser_agents import protocol
from tests.assistant_fakes import BV2_JOB, FakeDB, FakeStorage, NOW, PROJECT, PROJECT2, USER, bv2_job_row

OTHER_USER = "99999999-9999-4999-8999-999999999999"
ORIGIN = "https://acc.autodesk.com"
WORKER, BOOT, EPOCH = str(uuid4()), str(uuid4()), 3
EM_DASH = chr(0x2014)

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _zip(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name in names:
            archive.writestr(name, b"<x/>")
    return buf.getvalue()


XLSX = _zip(["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"])
XLSM = _zip(["[Content_Types].xml", "xl/workbook.xml", "xl/vbaProject.bin"])
PLAIN_ZIP = _zip(["a.txt", "b/c.txt"])


def _meta(data: bytes, filename: str, kind: str, **over) -> dict:
    meta = {"sha256": hashlib.sha256(data).hexdigest(), "filename": filename, "size_bytes": len(data),
            "sniffed_kind": kind, "source_origin": ORIGIN, "scan": "clean"}
    meta.update(over)
    return meta


# ── sniff and classify ───────────────────────────────────────────────────────


@pytest.mark.parametrize("head,kind", [
    (PDF, "pdf"),
    (PNG, "png"),
    (JPEG, "jpeg"),
    (b"II*\x00" + b"\x00" * 8, "tiff"),
    (b"MM\x00*" + b"\x00" * 8, "tiff"),
    (b"GIF89a" + b"\x00" * 8, "gif"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8, "ole"),
    (b"{\\rtf1\\ansi hello}", "rtf"),
    (b"AC1032\x00\x00\x00\x00", "dwg"),
    (b"0\nSECTION\n2\nHEADER\n9\n$ACADVER\n", "dxf"),
    (b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 8, "dxf"),
    (XLSX, "ooxml"),
    (PLAIN_ZIP, "zip"),
    (b"a,b,c\n1,2,3\n", "text"),
    ("Café notes\n".encode(), "text"),
])
def test_sniff_by_magic_bytes(head, kind):
    assert kind in downloads.ALLOWED_TYPES
    assert downloads.sniff(head) == kind


def test_sniff_csv_needs_the_name_but_never_widens_the_allowlist():
    assert downloads.sniff(b"a,b\n1,2\n", filename="bid.CSV") == "csv"
    assert downloads.sniff(b"a,b\n1,2\n", filename="bid.txt") == "text"
    # An extension never makes a binary file allowed.
    assert downloads.sniff(b"MZ\x90\x00\x03\x00\x00\x00", filename="report.pdf") is None


@pytest.mark.parametrize("head", [
    b"MZ\x90\x00\x03\x00\x00\x00\x04\x00",             # Windows executable
    b"\x7fELF\x02\x01\x01\x00",                         # ELF
    b"\xca\xfe\xba\xbe\x00\x00\x00\x34",                # Mach-O universal / Java class
    b"#!/bin/sh\n\x00rm -rf /\n",                       # script with a NUL byte
    b"\x00\x01\x02\x03\x04\x05",
    b"",
    b"PK\x05\x06" + b"\x00" * 18,                       # an empty zip
    b"\xff\xfe\x61\x00\x62\x00",                        # UTF-16 text is not UTF-8
])
def test_sniff_refuses_everything_else(head):
    assert downloads.sniff(head) is None


def test_classify_confirms_office_files_and_refuses_macros():
    assert downloads.classify(XLSX, "a.xlsx") == "ooxml"
    assert downloads.classify(PLAIN_ZIP, "a.zip") == "zip"
    with pytest.raises(downloads._Reject) as macro:
        downloads.classify(XLSM, "a.xlsm")
    assert macro.value.code == downloads.MACRO_ENABLED
    # A zip whose first entry looks like Office but has no Office parts is a plain zip.
    fake = _zip(["docProps/core.xml", "readme.txt"])
    assert downloads.sniff(fake) == "ooxml" and downloads.classify(fake, "x.zip") == "zip"


def _ooxml_with(content_types: bytes, parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        for name, body in parts.items():
            archive.writestr(name, body)
    return buf.getvalue()


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _ole(entry_names: list[str], text: bytes = b"") -> bytes:
    """A compound file stand-in: the 512-byte header, a body, then one
    directory sector whose 128-byte entries carry the given names."""
    header = OLE_MAGIC + b"\x00" * (512 - len(OLE_MAGIC))
    body = text + b"\x00" * ((-len(text)) % 512)
    entries = b"".join((n.encode("utf-16-le") + b"\x00\x00").ljust(128, b"\x00") for n in entry_names)
    return header + body + entries.ljust(512, b"\x00")


def test_classify_refuses_a_renamed_vba_part_declared_in_the_content_types():
    renamed = _ooxml_with(
        b'<Types><Override PartName="/word/m.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>',
        {"word/document.xml": b"<x/>", "word/m.bin": b"\x00" * 16})
    with pytest.raises(downloads._Reject) as macro:
        downloads.classify(renamed, "x.docx")
    assert macro.value.code == downloads.MACRO_ENABLED
    enabled = _ooxml_with(
        b'<Types><Default Extension="xml" ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/></Types>',
        {"xl/workbook.xml": b"<x/>"})
    with pytest.raises(downloads._Reject):
        downloads.classify(enabled, "x.xlsx")
    plain = _ooxml_with(b'<Types><Default Extension="xml" ContentType="application/xml"/></Types>',
                        {"word/document.xml": b"<x/>"})
    assert downloads.classify(plain, "x.docx") == "ooxml"


def test_classify_refuses_a_legacy_office_file_with_a_vba_project():
    with pytest.raises(downloads._Reject) as macro:
        downloads.classify(_ole(["Root Entry", "Workbook", "_VBA_PROJECT_CUR", "VBA"]), "budget.xls")
    assert macro.value.code == downloads.MACRO_ENABLED
    with pytest.raises(downloads._Reject):
        downloads.classify(_ole(["Root Entry", "WordDocument", "Macros"]), "spec.doc")
    # Document text that merely says "Macros" or "VBA" is not a VBA storage.
    text = "See the Macros and VBA notes.".encode("utf-16-le")
    assert downloads.classify(_ole(["Root Entry", "WordDocument", "1Table"], text=text), "spec.doc") == "ole"


def test_classify_refuses_a_corrupt_zip_and_bad_text():
    broken = XLSX[:-40]
    with pytest.raises(downloads._Reject) as corrupt:
        downloads.classify(broken, "x.xlsx")
    assert corrupt.value.code == downloads.TYPE_NOT_ALLOWED
    late_nul = b"hello world\n" * 6000 + b"\x00" + b"tail"
    assert len(late_nul) > downloads.HEAD_BYTES
    with pytest.raises(downloads._Reject):
        downloads.classify(late_nul, "x.txt")
    late_bad = b"hello world\n" * 6000 + b"\xff\xfe"
    with pytest.raises(downloads._Reject):
        downloads.classify(late_bad, "x.txt")


@pytest.mark.parametrize("raw,kind,expected", [
    ("Spec Book.pdf", "pdf", "Spec Book.pdf"),
    ("Spec Book.PDF", "pdf", "Spec Book.pdf"),
    ("report", "pdf", "report.pdf"),
    ("run.exe", "pdf", "run.exe.pdf"),
    ("a/b\\c.xlsx", "ooxml", "a-b-c.xlsx"),
    ("evil‮gnp.exe\n", "png", "evilgnp.exe.png"),
    ("photo.jpeg", "jpeg", "photo.jpeg"),
    ("script.bat", "text", "script.bat.txt"),
    ("page.html", "text", "page.html.txt"),
    ("..", "pdf", "download.pdf"),
    ("", None, "download"),
])
def test_clean_filename(raw, kind, expected):
    assert downloads.clean_filename(raw, kind) == expected


def test_clean_filename_is_bounded():
    name = downloads.clean_filename("x" * 600 + ".pdf", "pdf")
    assert len(name) == downloads.NAME_MAX and name.endswith(".pdf")


# ── accept: fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch):
    """A FakeDB, storage, a scripted record RPC that inserts the audit row, an
    eligible writer owner, and the file-add hooks spied on."""
    db = FakeDB(now=NOW, assistant_browser_downloads=[], assistant_profiles=[], project_files=[], projects=[
        {"id": PROJECT}, {"id": PROJECT2}], audit_log=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(files, "get_supabase", lambda: db)
    store = FakeStorage()
    store.downloads = {}
    store.install(monkeypatch)
    monkeypatch.setattr(storage, "download_file", lambda path: store.objects[path])
    audits: list[tuple] = []
    hooks: list[dict] = []
    monkeypatch.setattr(files, "audit", lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(notifications, "audit", lambda *a, **k: None)
    monkeypatch.setattr(files.rfp_file_issues, "is_first_file", lambda *a, **k: False)
    monkeypatch.setattr(files, "handoff_locked", lambda project_id: False)

    def after_added(project_id, row, category, user, first_file):
        hooks.append({"project_id": project_id, "row": row, "category": category, "user": user,
                      "actor": provenance.current()})
        return []

    monkeypatch.setattr(files, "_after_file_added", after_added)
    human = {"id": USER, "email": "tom@g3.test", "role": Role.EXECUTIVE.value, "is_active": True}
    state = SimpleNamespace(check=access.OwnerCheck(None, Role.EXECUTIVE, human, {}, {}))
    monkeypatch.setattr(access, "owner_check", lambda *a, **k: state.check)
    spawned: list = []
    monkeypatch.setattr(downloads, "_spawn", lambda later: spawned.append(list(later.tasks if later else [])))

    def record(params: dict) -> dict:
        rec = dict(params["p_record"])
        assert params["p_job"] == BV2_JOB and params["p_epoch"] == EPOCH
        if any(r["job_id"] == params["p_job"] and r["sha256"] == rec["sha256"] for r in db.tables["assistant_browser_downloads"]):
            return {"ok": True, "created": False, "download": None}
        db._insert("assistant_browser_downloads", {"user_id": USER, "job_id": params["p_job"], **rec})
        row = db.tables["assistant_browser_downloads"][-1]
        for col in ("project_id", "file_id", "staged_id", "reason", "storage_path", "expires_at", "purged_at"):
            row.setdefault(col, None)
        return {"ok": True, "created": True, "download": row}

    db.bv2_rpc_results[repo.BV2_DOWNLOAD_RECORD_RPC] = record
    return SimpleNamespace(db=db, store=store, audits=audits, hooks=hooks, state=state, spawned=spawned, human=human)


def _job(project=PROJECT, **over) -> dict:
    return bv2_job_row(project_id=project, **over)


def _accept(env, data: bytes, filename: str, kind: str, job=None, **over):
    return downloads.accept(job or _job(), WORKER, BOOT, EPOCH, stream=io.BytesIO(data),
                            meta=_meta(data, filename, kind, **over))


def _rows(env) -> list[dict]:
    return env.db.tables["assistant_browser_downloads"]


# ── accept: into the project ─────────────────────────────────────────────────


def test_a_download_with_a_project_becomes_an_assistant_project_file(env):
    out = _accept(env, PDF, "Spec Book.pdf", "pdf")
    assert out["status"] == "saved_to_project" and out["file_id"] and out["reason"] is None
    (pf,) = env.db.tables["project_files"]
    assert pf["project_id"] == PROJECT and pf["category"] == "other" and pf["filename"] == "Spec Book.pdf"
    assert pf["uploaded_by"] == USER and pf["size_bytes"] == len(PDF)
    assert pf["note"] == f"Downloaded by the assistant browser from {ORIGIN}"
    assert pf["actor_kind"] == "assistant" and pf["assistant_run_id"] == BV2_JOB
    assert env.store.objects[pf["storage_path"]] == PDF
    assert out["file_id"] == pf["id"]
    (rec,) = _rows(env)
    assert rec["status"] == "stored" and rec["project_id"] == PROJECT and rec["file_id"] == pf["id"]
    assert rec["sniffed_type"] == "pdf" and rec["scan_verdict"] == "clean" and rec["storage_path"] is None
    assert rec["origin"] == ORIGIN and rec["sha256"] == hashlib.sha256(PDF).hexdigest()


def test_the_file_add_hooks_run_and_the_audit_row_names_the_job(env):
    _accept(env, PDF, "a.pdf", "pdf")
    (hook,) = env.hooks
    assert hook["project_id"] == PROJECT and hook["category"] == "other" and hook["user"].id == USER
    assert hook["actor"] is not None and hook["actor"].user_id == USER and hook["actor"].run_id == BV2_JOB
    ((args, _kwargs),) = env.audits
    assert args[1] == "file.upload" and args[4]["via"] == "assistant_browser" and args[4]["job_id"] == BV2_JOB
    # The scope is closed again after the call.
    assert provenance.current() is None


def test_the_download_is_recorded_with_the_lease_fence_arguments(env):
    _accept(env, PNG, "plan.png", "png")
    params = env.db.rpc_calls[-1][1]
    assert env.db.rpc_calls[-1][0] == repo.BV2_DOWNLOAD_RECORD_RPC
    assert params["p_worker"] == WORKER and params["p_boot"] == BOOT and params["p_epoch"] == EPOCH


def test_the_name_gets_the_extension_of_the_sniffed_type(env):
    out = _accept(env, PDF, "report.exe", "pdf")
    assert out["status"] == "saved_to_project"
    assert env.db.tables["project_files"][0]["filename"] == "report.exe.pdf"


def test_an_office_file_is_stored_and_queues_its_preview(env):
    _accept(env, XLSX, "takeoff.xlsx", "ooxml")
    (pf,) = env.db.tables["project_files"]
    assert pf["preview_status"] in ("pending", "none")
    assert len(env.spawned) == 1


def test_declared_zip_for_an_office_file_is_one_family(env):
    out = _accept(env, XLSX, "takeoff.xlsx", "zip")
    assert out["status"] == "saved_to_project"
    assert _rows(env)[0]["sniffed_type"] == "ooxml"


def test_a_retry_of_the_same_bytes_answers_the_first_answer(env):
    first = _accept(env, PDF, "a.pdf", "pdf")
    again = _accept(env, PDF, "a.pdf", "pdf")
    assert again == first
    assert len(env.db.tables["project_files"]) == 1 and len(_rows(env)) == 1
    assert len(env.hooks) == 1


def test_a_lost_race_on_the_record_removes_the_second_project_file(env, monkeypatch):
    first = _accept(env, PDF, "a.pdf", "pdf")
    real = repo.browser_download_by_sha
    calls = {"n": 0}

    def first_lookup_misses(user_id, job_id, sha):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(user_id, job_id, sha)

    monkeypatch.setattr(repo, "browser_download_by_sha", first_lookup_misses)
    again = _accept(env, PDF, "a.pdf", "pdf")
    assert again == first
    assert len(env.db.tables["project_files"]) == 1
    assert len([p for p in env.store.objects if p.startswith(f"{PROJECT}/")]) == 1


def test_a_failed_record_removes_the_project_file_and_the_object(env):
    env.db.bv2_rpc_results[repo.BV2_DOWNLOAD_RECORD_RPC] = lambda p: {"ok": False, "code": "invalid"}
    with pytest.raises(RuntimeError):
        _accept(env, PDF, "a.pdf", "pdf")
    assert env.db.tables["project_files"] == []
    assert env.store.objects == {}


def test_a_record_that_raises_cleans_up_staged_objects(env):
    def boom(params):
        raise RuntimeError("lease_lost")

    env.db.bv2_rpc_results[repo.BV2_DOWNLOAD_RECORD_RPC] = boom
    with pytest.raises(RuntimeError):
        _accept(env, PDF, "a.pdf", "pdf", job=_job(project=None))
    assert env.store.objects == {}


# ── accept: staging ──────────────────────────────────────────────────────────


def test_no_project_stages_the_file_for_seven_days(env):
    out = _accept(env, PDF, "a.pdf", "pdf", job=_job(project=None))
    assert out["status"] == "staged" and out["file_id"] is None
    (rec,) = _rows(env)
    assert rec["status"] == "staged" and rec["project_id"] is None and rec["file_id"] is None
    assert rec["storage_path"].startswith(f"assistant-files/{USER}/browser/") and rec["storage_path"].endswith("-a.pdf")
    assert env.store.objects[rec["storage_path"]] == PDF
    expires = datetime.fromisoformat(rec["expires_at"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(timezone.utc) <= timedelta(days=7)
    assert env.db.tables["project_files"] == [] and env.hooks == []


def test_an_owner_who_may_not_upload_keeps_the_file_in_staging(env):
    human = {**env.human, "role": Role.ACCOUNTANT.value}
    env.state.check = access.OwnerCheck(access.OWNER_CHANGED, Role.ACCOUNTANT, human, None, None, access.DETAIL_NOT_WRITER)
    out = _accept(env, PDF, "a.pdf", "pdf")
    assert out["status"] == "staged" and out["reason"] == downloads.NO_PROJECT_RIGHT
    assert env.db.tables["project_files"] == []
    assert _rows(env)[0]["storage_path"]


def test_a_changed_owner_is_refused(env):
    env.state.check = access.OwnerCheck(access.OWNER_CHANGED, None, None, None, None, access.DETAIL_INACTIVE)
    out = _accept(env, PDF, "a.pdf", "pdf")
    assert out["status"] == "rejected" and out["reason"] == downloads.OWNER_UNAVAILABLE
    assert env.store.objects == {}


def test_a_project_write_that_fails_falls_back_to_staging(env, monkeypatch):
    def refuse(*a, **k):
        raise HTTPException(409, "locked")

    monkeypatch.setattr(files, "add_team_file", refuse)
    out = _accept(env, PDF, "a.pdf", "pdf")
    assert out["status"] == "staged" and out["reason"] == downloads.PROJECT_WRITE_FAILED
    assert provenance.current() is None


def test_a_full_staging_area_refuses(env, monkeypatch):
    monkeypatch.setattr(staging, "BROWSER_DOWNLOAD_CAP_BYTES", len(PDF) + 10)
    first = _accept(env, PDF, "a.pdf", "pdf", job=_job(project=None))
    assert first["status"] == "staged"
    other = PDF + b"x"
    out = _accept(env, other, "b.pdf", "pdf", job=_job(project=None))
    assert out["status"] == "rejected" and out["reason"] == downloads.STAGING_FULL
    assert len(env.store.objects) == 1


# ── accept: refusals ─────────────────────────────────────────────────────────


def _refused(env, out, reason):
    assert out["status"] == "rejected" and out["reason"] == reason and out["file_id"] is None
    assert env.store.objects == {} and env.db.tables["project_files"] == [] and env.hooks == []
    rec = _rows(env)[-1]
    assert rec["status"] == "rejected" and rec["reason"] == reason and rec["storage_path"] is None
    assert rec["file_id"] is None and rec["scan_verdict"] == "clean"


def test_a_wrong_hash_is_refused(env):
    _refused(env, _accept(env, PDF, "a.pdf", "pdf", sha256="0" * 64), downloads.SHA_MISMATCH)


def test_a_wrong_size_is_refused(env):
    _refused(env, _accept(env, PDF, "a.pdf", "pdf", size_bytes=len(PDF) + 1), downloads.SIZE_MISMATCH)


def test_an_empty_part_is_refused(env):
    out = downloads.accept(_job(), WORKER, BOOT, EPOCH, stream=io.BytesIO(b""),
                           meta=_meta(b"x", "a.pdf", "pdf"))
    _refused(env, out, downloads.EMPTY)


@pytest.mark.parametrize("data,name,kind", [
    (b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 32, "installer.pdf", "pdf"),
    (b"#!/bin/sh\n\x00evil", "run.txt", "text"),
    (b"\x7fELF\x02\x01\x01\x00", "tool.zip", "zip"),
])
def test_a_type_off_the_allowlist_is_refused_whatever_the_name_says(env, data, name, kind):
    _refused(env, _accept(env, data, name, kind), downloads.TYPE_NOT_ALLOWED)


def test_a_macro_enabled_file_is_refused(env):
    _refused(env, _accept(env, XLSM, "book.xlsm", "ooxml"), downloads.MACRO_ENABLED)


def test_a_declared_type_that_disagrees_with_the_bytes_is_refused(env):
    _refused(env, _accept(env, PNG, "a.png", "pdf"), downloads.TYPE_MISMATCH)


def test_a_file_past_the_per_file_cap_is_refused_without_reading_it_all(env, monkeypatch):
    monkeypatch.setattr(protocol, "USER_DOWNLOAD_FILE_BYTES_FLOOR", 1000)
    big = b"%PDF-" + b"a" * 2995

    class Counting(io.BytesIO):
        read_total = 0

        def read(self, n=-1):
            chunk = super().read(n)
            Counting.read_total += len(chunk)
            return chunk

    out = downloads.accept(_job(), WORKER, BOOT, EPOCH, stream=Counting(big), meta=_meta(big, "big.pdf", "pdf"))
    _refused(env, out, downloads.TOO_LARGE)
    assert Counting.read_total == 1001


def test_the_file_count_cap_is_enforced(env):
    caps = protocol.UserPolicyCaps()
    for n in range(caps.download_files):
        env.db.tables["assistant_browser_downloads"].append({
            "id": str(uuid4()), "user_id": USER, "job_id": BV2_JOB, "sha256": f"{n:064x}", "size_bytes": 10,
            "status": "stored", "created_at": NOW.isoformat()})
    _refused(env, _accept(env, PDF, "a.pdf", "pdf"), downloads.TOO_MANY_FILES)


def test_rejected_rows_do_not_count_against_the_caps(env):
    caps = protocol.UserPolicyCaps()
    for n in range(caps.download_files):
        env.db.tables["assistant_browser_downloads"].append({
            "id": str(uuid4()), "user_id": USER, "job_id": BV2_JOB, "sha256": f"{n:064x}", "size_bytes": 10,
            "status": "rejected", "created_at": NOW.isoformat()})
    assert _accept(env, PDF, "a.pdf", "pdf")["status"] == "saved_to_project"


def test_the_task_byte_cap_is_enforced(env):
    caps = protocol.UserPolicyCaps()
    env.db.tables["assistant_browser_downloads"].append({
        "id": str(uuid4()), "user_id": USER, "job_id": BV2_JOB, "sha256": "1" * 64,
        "size_bytes": caps.download_task_bytes - 5, "status": "stored", "created_at": NOW.isoformat()})
    _refused(env, _accept(env, PDF, "a.pdf", "pdf"), downloads.TASK_QUOTA)


def test_a_refusal_is_answered_the_same_on_retry(env):
    first = _accept(env, PNG, "a.png", "pdf")
    again = _accept(env, PNG, "a.png", "pdf")
    assert first == again and first["status"] == "rejected" and len(_rows(env)) == 1


# ── the worker route is a multipart upload route ─────────────────────────────


def test_the_download_route_gets_the_upload_cap():
    from app.core.middleware import form_routes_matcher
    from app.main import app

    match = form_routes_matcher(app)
    path = f"/agent-worker/v1/jobs/{uuid4()}/user/downloads"
    assert match("POST", path)
    assert not match("GET", path)


def test_the_route_hands_the_stream_and_meta_to_accept():
    import inspect

    from app.routers import agent_worker_user

    source = inspect.getsource(agent_worker_user.user_download)
    assert "downloads.accept(job, lease.worker.id, lease.boot_id, lease.epoch" in source
    assert "stream=file.file" in source and "meta=meta.model_dump" in source


# ── the result block ─────────────────────────────────────────────────────────


def test_file_entries_list_metadata_only(env):
    _accept(env, PDF, "a.pdf", "pdf")
    _accept(env, PNG, "b.png", "png", job=_job(project=None))
    entries = downloads.file_entries(USER, BV2_JOB)
    assert [e["status"] for e in entries] == ["saved_to_project", "staged"]
    assert entries[0]["file_id"] and entries[0]["can_add_to_project"] is False
    assert entries[1]["can_add_to_project"] is True and entries[1]["project_id"] is None
    assert all("storage_path" not in e and "sha256" not in e for e in entries)
    assert downloads.file_entries(OTHER_USER, BV2_JOB) == []


# ── add to project ───────────────────────────────────────────────────────────


@pytest.fixture
def staged(env):
    out = _accept(env, PDF, "Spec Book.pdf", "pdf", job=_job(project=None))
    row = _rows(env)[0]
    user = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    return SimpleNamespace(env=env, id=out["download_id"], row=row, user=user)


def test_add_to_project_writes_as_the_person_and_removes_the_staged_object(staged):
    env = staged.env
    path = staged.row["storage_path"]
    out = downloads.add_to_project(staged.user, staged.id, PROJECT2)
    (pf,) = env.db.tables["project_files"]
    assert out == {"file_id": pf["id"], "project_id": PROJECT2, "filename": "Spec Book.pdf"}
    assert pf["project_id"] == PROJECT2 and pf["category"] == "other" and pf["uploaded_by"] == USER
    assert pf["note"] == f"Downloaded by the assistant browser from {ORIGIN}"
    assert "actor_kind" not in pf or pf["actor_kind"] != "assistant"      # the person chose it: human
    assert env.store.objects[pf["storage_path"]] == PDF
    assert path not in env.store.objects
    row = _rows(env)[0]
    assert row["status"] == "stored" and row["project_id"] == PROJECT2 and row["file_id"] == pf["id"]
    assert row["purged_at"] is not None
    assert len(env.hooks) == 1 and env.hooks[0]["actor"] is None


def test_add_to_project_works_once(staged):
    downloads.add_to_project(staged.user, staged.id, PROJECT2)
    with pytest.raises(downloads.DownloadRefused) as second:
        downloads.add_to_project(staged.user, staged.id, PROJECT2)
    assert second.value.code == downloads.NOT_STAGED and second.value.status == 409
    assert len(staged.env.db.tables["project_files"]) == 1


def test_add_to_project_is_owner_scoped(staged):
    stranger = CurrentUser(id=OTHER_USER, email="x@g3.test", role=Role.EXECUTIVE, is_active=True)
    with pytest.raises(downloads.DownloadRefused) as err:
        downloads.add_to_project(stranger, staged.id, PROJECT2)
    assert err.value.code == downloads.NOT_FOUND and err.value.status == 404
    assert staged.env.db.tables["project_files"] == []


def test_add_to_project_checks_the_ids_and_the_project(staged):
    with pytest.raises(downloads.DownloadRefused) as bad:
        downloads.add_to_project(staged.user, "not-a-uuid", PROJECT2)
    assert bad.value.code == downloads.BAD_REQUEST and bad.value.status == 400
    with pytest.raises(downloads.DownloadRefused) as missing:
        downloads.add_to_project(staged.user, staged.id, str(uuid4()))
    assert missing.value.code == downloads.PROJECT_UNAVAILABLE and missing.value.status == 404
    assert _rows(staged.env)[0]["status"] == "staged"


def test_add_to_project_refuses_an_expired_download(staged):
    staged.row["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with pytest.raises(downloads.DownloadRefused) as err:
        downloads.add_to_project(staged.user, staged.id, PROJECT2)
    assert err.value.code == downloads.EXPIRED and err.value.status == 410


def test_add_to_project_refuses_a_rejected_row(env):
    out = _accept(env, PNG, "a.png", "pdf")
    user = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    with pytest.raises(downloads.DownloadRefused) as err:
        downloads.add_to_project(user, out["download_id"], PROJECT2)
    assert err.value.code == downloads.NOT_STAGED


def test_add_to_project_reverifies_the_bytes(staged):
    staged.env.store.objects[staged.row["storage_path"]] = PDF + b"tampered"
    with pytest.raises(downloads.DownloadRefused) as err:
        downloads.add_to_project(staged.user, staged.id, PROJECT2)
    assert err.value.code == downloads.CHANGED and err.value.status == 409
    assert staged.env.db.tables["project_files"] == []
    assert _rows(staged.env)[0]["status"] == "staged"


def test_add_to_project_undoes_the_claim_when_the_upload_policy_refuses(staged, monkeypatch):
    def refuse(*a, **k):
        raise HTTPException(403, "Not permitted")

    monkeypatch.setattr(files, "add_team_file", refuse)
    with pytest.raises(HTTPException) as err:
        downloads.add_to_project(staged.user, staged.id, PROJECT2)
    assert err.value.status_code == 403
    row = _rows(staged.env)[0]
    assert row["status"] == "staged" and row["project_id"] is None and row["purged_at"] is None
    assert staged.row["storage_path"] in staged.env.store.objects


# ── purge ────────────────────────────────────────────────────────────────────


def _age(env, row, days):
    row["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_purge_deletes_expired_staged_objects_and_keeps_fresh_ones(env):
    _accept(env, PDF, "old.pdf", "pdf", job=_job(project=None))
    _accept(env, PNG, "new.png", "png", job=_job(project=None))
    old, new = _rows(env)
    _age(env, old, 1)
    assert staging.purge_browser_downloads(None, 10) == 1
    assert old["storage_path"] not in env.store.objects and new["storage_path"] in env.store.objects
    assert old["purged_at"] is not None and new["purged_at"] is None
    assert staging.purge_browser_downloads(None, 10) == 0


def test_purge_skips_legal_hold_owners(env):
    _accept(env, PDF, "old.pdf", "pdf", job=_job(project=None))
    (old,) = _rows(env)
    _age(env, old, 1)
    env.db.tables["assistant_profiles"].append({"user_id": USER, "legal_hold": True})
    assert staging.purge_browser_downloads(None, 10) == 0
    assert staging.purge_browser_downloads(USER, 10) == 0
    assert old["storage_path"] in env.store.objects and old["purged_at"] is None


def test_the_own_sweep_touches_only_that_users_rows(env):
    _accept(env, PDF, "old.pdf", "pdf", job=_job(project=None))
    (old,) = _rows(env)
    _age(env, old, 1)
    assert staging.purge_browser_downloads(OTHER_USER, 10) == 0
    assert old["purged_at"] is None
    assert staging.purge_browser_downloads(USER, 10) == 1


def test_a_failed_object_delete_leaves_the_row_for_the_next_sweep(env):
    _accept(env, PDF, "old.pdf", "pdf", job=_job(project=None))
    (old,) = _rows(env)
    _age(env, old, 1)
    env.store.fail_delete = True
    assert staging.purge_browser_downloads(None, 10) == 0
    assert old["purged_at"] is None
    env.store.fail_delete = False
    assert staging.purge_browser_downloads(None, 10) == 1


def test_a_stored_row_is_never_swept(env):
    _accept(env, PDF, "a.pdf", "pdf")
    (rec,) = _rows(env)
    rec["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    assert staging.purge_browser_downloads(None, 10) == 0


# ── conventions ──────────────────────────────────────────────────────────────


def test_no_em_dash_in_the_files_of_this_item():
    from pathlib import Path

    for path in (Path(downloads.__file__), Path(staging.__file__), Path(__file__)):
        assert EM_DASH not in path.read_text(encoding="utf-8"), path.name


def test_the_new_repo_functions_take_user_id_first():
    import inspect

    for name in ("browser_download_record", "browser_download_get", "browser_download_by_sha",
                 "browser_downloads_staged_bytes", "browser_download_claim", "browser_download_unclaim",
                 "browser_download_finish", "browser_download_mark_purged", "staged_downloads_to_purge"):
        assert list(inspect.signature(getattr(repo, name)).parameters)[0] == "user_id", name


def test_the_record_rpc_wrapper_passes_exactly_the_declared_arguments(env):
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "supabase/migrations/0192_assistant_browser_v2.sql").read_text(
        encoding="utf-8")
    sig = re.search(r"function public\.assistant_browser_download_record\((.*?)\)\s*returns", sql, re.S).group(1)
    declared = {chunk.split()[0] for chunk in re.split(r",\s*(?=p_)", " ".join(sig.split()))}
    env.db.bv2_rpc_results[repo.BV2_DOWNLOAD_RECORD_RPC] = {"ok": True, "download": None}
    repo.browser_download_record(USER, BV2_JOB, WORKER, BOOT, EPOCH, {"sha256": "a" * 64})
    assert set(env.db.rpc_calls[-1][1]) == declared


def test_the_sql_records_the_staged_columns():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "supabase/migrations/0192_assistant_browser_v2.sql").read_text(
        encoding="utf-8")
    body = sql[sql.index("function public.assistant_browser_download_record"):]
    body = body[: body.index("end $$;")]
    for column in ("reason", "storage_path", "expires_at"):
        assert column in body
    assert "add column if not exists storage_path" in sql and "add column if not exists purged_at" in sql
