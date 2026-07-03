"""Unit tests for the project-files ZIP export — pure assembly, storage mocked."""

import io
import zipfile

from app.services import file_export


def _names(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return set(zf.namelist())


def test_groups_by_category_folder_including_proposal(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: f"b:{p}".encode())
    rows = [
        {"category": "estimate", "storage_path": "b", "filename": "Estimate.xlsx", "size_bytes": 20},
        {"category": "drawing", "storage_path": "a", "filename": "E-101.pdf", "size_bytes": 10},
        {"category": "proposal", "storage_path": "c", "filename": "GC.docx", "size_bytes": 30},
    ]
    data, manifest = file_export.build_export_zip(rows)
    names = _names(data)
    assert "drawing/E-101.pdf" in names
    assert "estimate/Estimate.xlsx" in names
    assert "proposal/GC.docx" in names  # proposal gets its own folder, not the fallback
    assert "MANIFEST.txt" in names
    assert all(m["status"] == "ok" for m in manifest)


def test_dedupes_duplicate_filenames_in_same_category(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [
        {"category": "drawing", "storage_path": "a", "filename": "plan.pdf", "size_bytes": 1},
        {"category": "drawing", "storage_path": "b", "filename": "plan.pdf", "size_bytes": 1},
    ]
    names = _names(file_export.build_export_zip(rows)[0])
    assert "drawing/plan.pdf" in names
    assert "drawing/plan (2).pdf" in names


def test_missing_object_is_skipped_not_fatal(monkeypatch):
    def fake_dl(path: str) -> bytes:
        if path == "gone":
            raise RuntimeError("object not found")
        return b"ok"

    monkeypatch.setattr(file_export.storage, "download_file", fake_dl)
    rows = [
        {"category": "drawing", "storage_path": "ok1", "filename": "a.pdf", "size_bytes": 1},
        {"category": "drawing", "storage_path": "gone", "filename": "b.pdf", "size_bytes": 1},
    ]
    data, manifest = file_export.build_export_zip(rows)
    names = _names(data)
    assert "drawing/a.pdf" in names
    assert "drawing/b.pdf" not in names
    assert sum(1 for m in manifest if m["status"] == "ok") == 1
    assert any(m["status"] == "missing" for m in manifest)


def test_safe_name_blocks_zip_slip():
    assert file_export._safe_name("../../etc/passwd") == "passwd"
    assert file_export._safe_name("a\\b\\c.pdf") == "c.pdf"
    assert file_export._safe_name("C:\\Users\\x\\f.pdf") == "f.pdf"
    assert file_export._safe_name(None) == "file"
    assert "/" not in file_export._safe_name("nested/evil.pdf")


def test_export_filename_prefers_number():
    fn = file_export.export_filename({"number": "24-118", "name": "Riverside"})
    assert fn.startswith("24-118_files_")
    assert fn.endswith(".zip")
