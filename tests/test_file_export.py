"""Unit tests for the project-files ZIP export — pure assembly, storage mocked —
plus the estimator/internal /export endpoint scoping (F1 + the files_exported_at
stamp branch)."""

import inspect
import io
import zipfile
from types import SimpleNamespace

from app.core.deps import CurrentUser
from app.core.roles import Role
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
    assert "Gen Dwgs/E-101.pdf" in names
    assert "Estimate/Estimate.xlsx" in names
    assert "Proposals/GC.docx" in names  # proposal gets its own folder, not the fallback
    assert "MANIFEST.txt" in names
    assert all(m["status"] == "ok" for m in manifest)


def test_dedupes_duplicate_filenames_in_same_category(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [
        {"category": "drawing", "storage_path": "a", "filename": "plan.pdf", "size_bytes": 1},
        {"category": "drawing", "storage_path": "b", "filename": "plan.pdf", "size_bytes": 1},
    ]
    names = _names(file_export.build_export_zip(rows)[0])
    assert "Gen Dwgs/plan.pdf" in names
    assert "Gen Dwgs/plan (2).pdf" in names


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
    assert "Gen Dwgs/a.pdf" in names
    assert "Gen Dwgs/b.pdf" not in names
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
    assert fn == "24-118_files.zip"
    assert fn.endswith(".zip")


def test_addendum_gets_its_own_folder(monkeypatch):
    # Pins that addenda export into their own `Add/` folder (parallel to the
    # spec §8.1 note about ranking the new folder).
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [
        {"category": "addendum", "storage_path": "a", "filename": "add3.pdf", "size_bytes": 1},
        {"category": "drawing", "storage_path": "b", "filename": "E-101.pdf", "size_bytes": 1},
    ]
    names = _names(file_export.build_export_zip(rows)[0])
    assert "Add/add3.pdf" in names
    assert "Gen Dwgs/E-101.pdf" in names


# ── estimator query set + /export endpoint scoping ─────────────────────────


def test_estimator_query_categories_include_addendum():
    # §8.2 #36: the single estimator-scoped category filter must carry 'addendum'
    # or every sent addendum is silently dropped from list/export/ZIP.
    from app.routers.files import ESTIMATOR_QUERY_CATEGORIES

    assert "addendum" in ESTIMATOR_QUERY_CATEGORIES


def test_export_projection_selects_uploaded_by():
    # F1 regression (§8.2 #37): without uploaded_by in the projection every row
    # evaluates `None == user.id` in _estimator_visible and 100% of an estimator's
    # own deliverables drop from their ZIP (→ 404). Pin the projection tail.
    from app.routers import files as files_mod

    src = inspect.getsource(files_mod.export_files)
    assert "sent_to_estimators_at, uploaded_by" in src


class _EQ:
    def __init__(self, db, table):
        self.db, self.table_name, self.op = db, table, "select"
        self.filters: list[tuple] = []
        self.payload = None

    def select(self, *a, **k):
        return self

    def insert(self, p):
        self.op, self.payload = "insert", p
        return self

    def update(self, p):
        self.op, self.payload = "update", p
        return self

    def eq(self, c, v):
        self.filters.append(("eq", c, v))
        return self

    def in_(self, c, v):
        self.filters.append(("in", c, tuple(v)))
        return self

    def is_(self, c, v):
        self.filters.append(("is", c, v))
        return self

    def or_(self, e):
        self.filters.append(("or", e))
        return self

    def single(self):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def execute(self):
        self.db.calls.append(self)
        q = self.db.queues.get((self.table_name, self.op)) or []
        r = q.pop(0) if q else []
        return SimpleNamespace(data=r, count=len(r) if isinstance(r, list) else None)


class _EDB:
    def __init__(self):
        self.queues: dict[tuple[str, str], list] = {}
        self.calls: list[_EQ] = []

    def queue(self, t, op, *rs):
        self.queues.setdefault((t, op), []).extend(rs)

    def table(self, n):
        return _EQ(self, n)

    def ops(self, t, op):
        return [c for c in self.calls if c.table_name == t and c.op == op]


def _user(role, uid="me"):
    return CurrentUser(
        id=uid, email="e@x.com", role=role, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


async def test_estimator_export_keeps_own_estimate_and_never_stamps(monkeypatch):
    # F1 behavioural (§8.2 #37/#38): an estimator's no-body export includes their
    # OWN estimate deliverable, and never stamps projects.files_exported_at.
    from app.routers import files as files_mod

    captured: dict = {}

    def fake_spool(rows, **_kw):
        captured["rows"] = rows
        return io.BytesIO(b"zip"), [{"status": "ok"}], 3

    monkeypatch.setattr(files_mod.file_export, "build_export_spooled", fake_spool)
    monkeypatch.setattr(files_mod, "audit", lambda *a, **k: None)
    db = _EDB()
    db.queue(
        "project_files", "select",
        [{
            "id": "e1", "category": "estimate", "storage_path": "s", "filename": "est.xlsx",
            "size_bytes": 10, "sent_to_estimators_at": None, "uploaded_by": "me",
        }],
    )
    db.queue("projects", "select", {"number": "42", "name": "Acme"})
    monkeypatch.setattr(files_mod, "get_supabase", lambda: db)

    await files_mod.export_files("p1", None, _user(Role.ESTIMATOR))
    assert any(r["id"] == "e1" for r in captured["rows"])  # own estimate survived the filter
    assert db.ops("projects", "update") == []  # estimator NEVER stamps files_exported_at


async def test_internal_export_stamps_files_exported_at(monkeypatch):
    from app.routers import files as files_mod

    monkeypatch.setattr(
        files_mod.file_export, "build_export_spooled",
        lambda rows, **_kw: (io.BytesIO(b"z"), [{"status": "ok"}], 1),
    )
    monkeypatch.setattr(files_mod, "audit", lambda *a, **k: None)
    db = _EDB()
    db.queue(
        "project_files", "select",
        [{
            "id": "d1", "category": "drawing", "storage_path": "s", "filename": "d.pdf",
            "size_bytes": 5, "sent_to_estimators_at": None, "uploaded_by": "x",
        }],
    )
    db.queue("projects", "select", {"number": "42", "name": "Acme"})
    db.queue("projects", "update", [{}])
    monkeypatch.setattr(files_mod, "get_supabase", lambda: db)

    await files_mod.export_files("p1", None, _user(Role.ESTIMATING_ADMIN))
    stamps = db.ops("projects", "update")
    assert len(stamps) == 1
    assert "files_exported_at" in stamps[0].payload


# ── Tree export (nested folders — Bid File Splitter) ──────────────────────────


def _tree_names(rows, **kwargs) -> set[str]:
    spool, _manifest, _size = file_export.build_tree_export_spooled(rows, **kwargs)
    try:
        with zipfile.ZipFile(spool) as zf:
            return set(zf.namelist())
    finally:
        spool.close()


def _tree_manifest_text(rows, **kwargs) -> str:
    spool, _manifest, _size = file_export.build_tree_export_spooled(rows, **kwargs)
    try:
        with zipfile.ZipFile(spool) as zf:
            return zf.read("MANIFEST.txt").decode()
    finally:
        spool.close()


def test_tree_export_nests_every_folder_component(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: f"b:{p}".encode())
    rows = [
        {
            "folders": ["Bid Set A", "Electrical Drawings"],
            "filename": "E-Sheets (pages 12-40).pdf",
            "storage_path": "a",
        },
        {
            "folders": ["Bid Set A", "Other", "Geotechnical Report"],
            "filename": "Soils (pages 41-60).pdf",
            "storage_path": "b",
        },
    ]
    names = _tree_names(rows)
    assert "Bid Set A/Elec Dwgs/E- p12-40.pdf" in names
    assert "Bid Set A/Other/Geotech Rpt/Soils p41-60.pdf" in names
    assert "MANIFEST.txt" in names


def test_tree_export_keeps_caller_order_and_dedupes_collisions(monkeypatch):
    """Rows arrive pre-ordered (source file, then segment order) and must not be
    re-sorted; two identical paths still get distinct arcnames."""
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [
        {"folders": ["Set", "Specifications"], "filename": "b.pdf", "storage_path": "1"},
        {"folders": ["Set", "Specifications"], "filename": "a.pdf", "storage_path": "2"},
        {"folders": ["Set", "Specifications"], "filename": "a.pdf", "storage_path": "3"},
    ]
    spool, manifest, _size = file_export.build_tree_export_spooled(rows)
    spool.close()
    assert [m["file"] for m in manifest] == [
        "Set/Spec/b.pdf",
        "Set/Spec/a.pdf",
        "Set/Spec/a (2).pdf",
    ]


def test_tree_export_sanitises_folder_components(monkeypatch):
    """Folder components are the same zip-slip surface as filenames."""
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [
        {"folders": ["../..", "C:/Windows"], "filename": "../evil.pdf", "storage_path": "1"},
    ]
    for name in _tree_names(rows):
        assert ".." not in name
        assert not name.startswith("/")


def test_tree_export_manifest_carries_title_and_notes(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [{"folders": ["Set", "RFP"], "filename": "rfp.pdf", "storage_path": "1"}]
    text = _tree_manifest_text(
        rows, title="BDR Bid File Splitter export", notes=["huge.pdf: failed, no sections"]
    )
    assert text.startswith("BDR Bid File Splitter export")
    assert "huge.pdf: failed, no sections" in text


def test_zip_filename_sanitises_without_a_date_stamp():
    # No date: "Extract All" names a folder after the zip, lengthening every path.
    assert file_export.zip_filename('bid/set:"A"', "folders") == "bid_set_A__folders.zip"


def test_zip_filename_is_header_safe_ascii():
    # Uploaded names are stored verbatim; the zip name lands in a latin-1
    # encoded Content-Disposition header, so CR/LF, controls and non-latin-1
    # characters must never reach it.
    fn = file_export.zip_filename("24-118 Caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{EN DASH} \U0001f525\r\nX-Evil: 1\x00", "split")
    assert fn == "24-118 Cafe _ _X-Evil_ 1_split.zip"
    fn.encode("latin-1")
    assert all(0x20 <= ord(c) <= 0x7E for c in fn)
    assert file_export.zip_filename("\U0001f525\U0001f525", "split") == "__split.zip"
    assert file_export.zip_filename("\r\n\t", "split") == "export_split.zip"


def test_flat_export_puts_every_file_at_the_root(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [
        {"category": "low_voltage_drawing", "storage_path": "a", "filename": "T-1.pdf", "size_bytes": 1},
        {"category": "specification", "storage_path": "b", "filename": "Specifications.pdf", "size_bytes": 1},
    ]
    names = _names(file_export.build_export_zip(rows, flat=True)[0])
    assert names == {"LV Dwgs - T-1.pdf", "Spec - Spec.pdf", "MANIFEST.txt"}


def test_long_names_are_capped_for_windows_paths(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: b"x")
    rows = [
        {
            "folders": ["A very long original bid set upload name for 26.9.7126 " * 3, "Electrical Drawings"],
            "filename": "Electrical Power and Lighting Plans Level 1 through 12 " * 3 + ".pdf",
            "storage_path": "1",
        }
    ]
    spool, manifest, _size = file_export.build_tree_export_spooled(rows)
    spool.close()
    path = manifest[0]["file"]
    assert len(path) <= 120
    assert path.endswith(".pdf")
    assert "26.9.7126" not in path
