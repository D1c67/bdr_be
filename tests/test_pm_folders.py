"""Unified documents hub — folder mapping + the read-side union.

Covers the source→folder map (completeness + spot checks), the certified-payroll
path (latest-revision-per-report, project tagging, graceful degrade when the CP
tagging table is absent), and that `list_project_documents` sorts by folder then
filename. The bidding path uses PostgREST `.or_()` (exclude_unsent) which the tiny
fake below doesn't model, so the full union is exercised by stubbing the three
source helpers; the CP helper is tested directly against the fake.
"""

from types import SimpleNamespace

import pytest

from app.routers.pm_documents import PM_DOC_CATEGORIES
from app.services import pm_folders


# ── Tiny Supabase fake (select + eq + in_) ────────────────────────────────────


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self._filters, self._raise = db, table, [], db.raise_on == table

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    def _match(self, row):
        return all(
            row.get(c) in v if isinstance(v, list) else row.get(c) == v
            for c, v in self._filters
        )

    def execute(self):
        if self._raise:
            raise RuntimeError("relation does not exist")
        rows = self.db.tables.get(self.table, [])
        return SimpleNamespace(data=[dict(r) for r in rows if self._match(r)])


class FakeDB:
    def __init__(self, tables, raise_on=None):
        self.tables = tables
        self.raise_on = raise_on

    def table(self, name):
        return _Q(self, name)


def _install(monkeypatch, db):
    monkeypatch.setattr(pm_folders, "get_supabase", lambda: db)


# ── Folder mapping ────────────────────────────────────────────────────────────


def test_every_folder_has_a_label_and_order_is_unique():
    assert set(pm_folders.FOLDER_ORDER) == set(pm_folders.FOLDER_LABELS)
    assert len(pm_folders.FOLDER_ORDER) == len(set(pm_folders.FOLDER_ORDER))


def test_every_source_category_maps_into_an_ordered_folder():
    mapped = (
        set(pm_folders._PM_CATEGORY_FOLDER.values())
        | set(pm_folders._BID_CATEGORY_FOLDER.values())
        | {pm_folders.CP_FOLDER}
    )
    assert mapped <= set(pm_folders.FOLDER_ORDER)


def test_writable_folders_cover_every_pm_category():
    # Every uploadable pm category resolves to a real folder (the FE upload
    # picker mirrors this).
    for cat in PM_DOC_CATEGORIES:
        assert cat in pm_folders._PM_CATEGORY_FOLDER


@pytest.mark.parametrize(
    "source,category,folder",
    [
        ("pm", "drawing", "plans"),
        ("pm", "billing", "billing"),
        ("pm", "contract", "contracts"),
        ("pm", "rfi", "rfis"),
        ("bid", "quote", "quotes"),
        ("bid", "rfq_split", "quotes"),
        ("bid", "boq", "estimates"),
        ("bid", "proposal", "proposals"),
        ("bid", "revision", "revisions"),
        ("cp", None, "certified_payroll"),
        ("bid", "totally_unknown", "other"),
        ("pm", None, "other"),
    ],
)
def test_folder_for(source, category, folder):
    assert pm_folders.folder_for(source, category) == folder


def test_rfi_attachments_get_their_own_folder():
    # 0067: RFI files must not be buried under 'correspondence'.
    assert pm_folders._PM_CATEGORY_FOLDER["rfi"] == "rfis"
    assert pm_folders.FOLDER_LABELS["rfis"] == "RFIs"
    # Filed with the other formal-correspondence deliverables, after submittals.
    order = pm_folders.FOLDER_ORDER
    assert order.index("rfis") == order.index("submittals") + 1


def test_folder_rank_orders_known_before_unknown():
    assert pm_folders.folder_rank("plans") < pm_folders.folder_rank("other")
    assert pm_folders.folder_rank("nope") == len(pm_folders.FOLDER_ORDER)


# ── Certified-payroll path ────────────────────────────────────────────────────


def _cp_fixture():
    # One weekly report, two generation records (rev 0 superseded by rev 1). The
    # aggregate file is tagged to both projects, the LCP file to one.
    return {
        "cp_record_file_projects": [
            {"record_file_id": "f_old", "project_id": "p1"},
            {"record_file_id": "f_agg", "project_id": "p1"},
            {"record_file_id": "f_agg", "project_id": "p2"},
            {"record_file_id": "f_lcp", "project_id": "p1"},
        ],
        "cp_record_files": [
            {"id": "f_old", "record_id": "rec0", "filename": "eComply CPR Upload.csv",
             "storage_path": "payroll/x/f_old", "size_bytes": 10, "created_at": "2026-06-01T00:00:00Z"},
            {"id": "f_agg", "record_id": "rec1", "filename": "Revised eComply CPR Upload.csv",
             "storage_path": "payroll/x/f_agg", "size_bytes": 20, "created_at": "2026-06-02T00:00:00Z"},
            {"id": "f_lcp", "record_id": "rec1", "filename": "24-118 LCP CPR Upload.csv",
             "storage_path": "payroll/x/f_lcp", "size_bytes": 30, "created_at": "2026-06-02T00:00:00Z"},
        ],
        "cp_records": [
            {"id": "rec0", "payroll_report_id": "rep1", "revision_number": 0},
            {"id": "rec1", "payroll_report_id": "rep1", "revision_number": 1},
        ],
        "cp_payroll_reports": [{"id": "rep1", "week_start_date": "2026-06-08"}],
    }


def test_cp_documents_latest_revision_and_tagging(monkeypatch):
    _install(monkeypatch, FakeDB(_cp_fixture()))
    items = pm_folders._cp_documents("p1")
    by_id = {i["id"]: i for i in items}
    # rev-0 file is superseded → excluded; both rev-1 files present.
    assert set(by_id) == {"f_agg", "f_lcp"}
    agg = by_id["f_agg"]
    assert agg["folder"] == "certified_payroll"
    assert agg["source"] == "cp" and agg["key"] == "cp:f_agg"
    assert agg["writable"] is False
    assert agg["cp_meta"] == {"week_start_date": "2026-06-08", "revision_number": 1}


def test_cp_documents_scoped_to_project(monkeypatch):
    _install(monkeypatch, FakeDB(_cp_fixture()))
    # p2 is only tagged on the aggregate file.
    items = pm_folders._cp_documents("p2")
    assert {i["id"] for i in items} == {"f_agg"}


def test_cp_documents_degrades_when_tagging_table_absent(monkeypatch):
    _install(monkeypatch, FakeDB(_cp_fixture(), raise_on="cp_record_file_projects"))
    assert pm_folders._cp_documents("p1") == []


# ── Union: composition + sort ─────────────────────────────────────────────────


def test_list_project_documents_sorts_by_folder_then_filename(monkeypatch):
    monkeypatch.setattr(
        pm_folders, "_pm_documents",
        lambda pid: [
            {"key": "pm:1", "folder": "contracts", "filename": "b.pdf"},
            {"key": "pm:2", "folder": "billing", "filename": "z.pdf"},
        ],
    )
    monkeypatch.setattr(
        pm_folders, "_bidding_documents",
        lambda pid: [
            {"key": "bid:1", "folder": "plans", "filename": "M.pdf"},
            {"key": "bid:2", "folder": "plans", "filename": "a.pdf"},
        ],
    )
    monkeypatch.setattr(
        pm_folders, "_cp_documents",
        lambda pid: [{"key": "cp:1", "folder": "certified_payroll", "filename": "cpr.csv"}],
    )
    order = [i["key"] for i in pm_folders.list_project_documents("p1")]
    # plans (rank 0, case-insensitive a<M) → contracts → billing → certified_payroll.
    assert order == ["bid:2", "bid:1", "pm:1", "pm:2", "cp:1"]
