"""app/services/eval_bundle.py: bundle layout, loading, source PDF resolution,
the weekend import and the dev export (fake client). Golden: the real
weekend bundle and the production backup when both are on this machine."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from app.services import eval_bundle as B

from tests import eval_fixtures as F

WEEKEND = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25")
golden = pytest.mark.skipif(
    not (WEEKEND / "run.json").exists() or not (B.DEFAULT_BACKUP_DIR / "data" / "project_files.json").exists(),
    reason="the weekend bundle or the production backup is not on disk",
)


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    return F.make_backup(tmp_path / "backup")


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    return F.make_bundle(tmp_path / "bundle", good=True)


def test_load_bundle_typed_objects(bundle_dir):
    b = B.load_bundle(bundle_dir)
    assert b.run_id == F.RUN_ID and b.run_id8 == F.RUN_ID[:8]
    assert b.case_id is None  # no bundle.json yet
    assert [d["category"] for d in b.documents] == ["electrical_drawings", "specifications"]
    assert b.categories == ["electrical_drawings", "specifications"]
    assert b.main_output is not None and "boq" in b.main_output
    assert b.deliverable_versions("estimate") == [1, 2]
    assert b.latest_deliverable("boq")[0] == 1
    assert b.boq_spec()["areas"][0]["area"] == "Synthetic Pharmacy"
    assert len(b.calls) == 1 and b.chat_probe()["answer"].startswith("About 104")
    # group outputs: stored file for the subs, the main output for main
    kinds = {g["kind"]: str(g["id"]) in b.group_outputs for g in b.groups}
    assert kinds == {"sub": True, "main": True}
    assert b.group_outputs["g3"] is b.main_output


def test_group_output_name_matches_weekend_layout():
    g = {"kind": "escalation", "category": "electrical_drawings", "group_index": 2, "pass": 2}
    assert B.group_output_name(g) == "escalation_electrical_drawings_2_p2.json"
    assert B.group_output_name({"kind": "main", "category": None, "group_index": 0, "pass": 1}) == "main_none_0_p1.json"
    assert B.output_name("category", "rfp", 1) == "category_rfp_v1.json"
    assert B.output_name("main", None, 3) == "main_none_v3.json"


def test_missing_required_member_is_refused(tmp_path):
    (tmp_path / "run.json").write_text("{}")
    with pytest.raises(B.BundleError, match="files.json"):
        B.load_bundle(tmp_path)


def test_doc_bytes_resolves_through_backup_rows_not_folders(bundle_dir, backup):
    b = B.load_bundle(bundle_dir)
    pdf = b.doc_bytes(F.ELEC_DOC, backup_dir=backup)
    assert len(PdfReader(__import__("io").BytesIO(pdf)).pages) == 3
    # the spec document is 4 pages of a 4 page file: whole file, no cut
    spec = b.doc_bytes(F.SPEC_DOC, backup_dir=backup)
    assert len(PdfReader(__import__("io").BytesIO(spec)).pages) == 4


def test_doc_bytes_cuts_segments(bundle_dir, backup):
    b = B.load_bundle(bundle_dir)
    d = b.document(F.SPEC_DOC)
    d["page_meta"] = [{"page": 2, "pdf_page": 1}, {"page": 3, "pdf_page": 2}]
    d["segment_id"] = "seg"
    assert B.segment_pages(d, 4) == [2, 3]
    pdf = b.doc_bytes(F.SPEC_DOC, backup_dir=backup)
    assert len(PdfReader(__import__("io").BytesIO(pdf)).pages) == 2


def test_doc_bytes_falls_back_to_inputs_dir(bundle_dir, tmp_path):
    b = B.load_bundle(bundle_dir)
    with pytest.raises(B.BundleError, match="no source PDF"):
        b.doc_bytes(F.ELEC_DOC, backup_dir=tmp_path / "nowhere")
    (bundle_dir / "inputs").mkdir()
    (bundle_dir / "inputs" / f"{F.ELEC_DOC}.pdf").write_bytes(F.blank_pdf(3))
    assert b.doc_bytes(F.ELEC_DOC, backup_dir=tmp_path / "nowhere")


def test_import_weekend_export_writes_meta_in_place_and_copy(bundle_dir, tmp_path):
    out = B.import_weekend_export(bundle_dir, case_id="7154")
    assert out == bundle_dir
    meta = json.loads((bundle_dir / "bundle.json").read_text())
    assert meta["schema"] == B.BUNDLE_SCHEMA and meta["case_id"] == "7154" and meta["source"] == "prod_export"
    assert meta["run_id"] == F.RUN_ID and meta["pricing_free"] is True
    assert "run.json" in meta["members"] and "groups/sub_electrical_drawings_0_p1.json" in meta["members"]
    assert "bundle.json" not in meta["members"]
    assert "T" in meta["exported_at"] and ("-07:00" in meta["exported_at"] or "-08:00" in meta["exported_at"])
    assert B.load_bundle(bundle_dir, require_meta=True).case_id == "7154"
    assert B.verify_members(bundle_dir) == []
    copy_dir = B.import_weekend_export(bundle_dir, tmp_path / "copy", case_id="7154")
    assert (copy_dir / "outputs" / "main_none_v1.json").is_file()
    (copy_dir / "run.json").write_text("{}")
    assert B.verify_members(copy_dir) == ["run.json"]


def test_strip_images_removes_data_urls_and_long_image_fields():
    snap = {"parts": [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * 5000, "sha256": "abc"},
                      {"type": "input_text", "head": "RUN LEDGER"}], "image": "x" * 300, "note": "data:image/png;base64,AAAA"}
    out = B.strip_images(snap)
    assert out["parts"][0]["image_url"] == {"stripped": True, "chars": 5022}
    assert out["parts"][0]["sha256"] == "abc" and out["parts"][1]["head"] == "RUN LEDGER"
    assert out["image"]["stripped"] is True and out["note"]["stripped"] is True
    assert snap["parts"][0]["image_url"].startswith("data:")  # the input is untouched


def test_check_not_production_refuses_prod_and_staging():
    B.check_not_production(SimpleNamespace(is_production=False, supabase_url=f"https://{B.DEV_REF}.supabase.co"))
    with pytest.raises(B.BundleError):
        B.check_not_production(SimpleNamespace(is_production=True, supabase_url=""))
    with pytest.raises(B.BundleError):
        B.check_not_production(None, f"https://{B.PROD_REF}.supabase.co")
    with pytest.raises(B.BundleError):
        B.check_not_production(None, "https://qpfuugcwsdfpxvdgwvwx.supabase.co")


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}

    def select(self, *_a):
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def execute(self):
        rows = [r for r in self.rows if all(r.get(k) == v for k, v in self.filters.items())]
        return SimpleNamespace(data=[json.loads(json.dumps(r)) for r in rows])


class _FakeSb:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _dev_tables(bundle_dir: Path) -> dict:
    """The weekend layout turned back into table rows (what a dev export reads)."""
    b = B.load_bundle(bundle_dir)
    groups = []
    for g in b.groups:
        row = dict(g)
        row["output"] = b.group_outputs.get(str(g["id"]))
        groups.append(row)
    outputs = [{"run_id": F.RUN_ID, "scope": "category", "category": cat, "version": 1, "output": b.category_output(cat)} for cat in b.categories]
    outputs.append({"run_id": F.RUN_ID, "scope": "main", "category": None, "version": 1, "output": b.main_output})
    deliverables = [dict(d, spec_json=b.deliverables[(d["kind"], d["version"])]) for d in b.deliverable_rows]
    calls = [dict(c, request_snapshot={"parts": [{"type": "input_image", "image_url": "data:image/png;base64," + "B" * 400}]}) for c in b.calls]
    threads = [{k: v for k, v in t.items() if k != "messages"} for t in b.chat]
    messages = [dict(m, thread_id=t["id"], created_at=f"2026-10-21T10:0{i}:00+00:00") for t in b.chat for i, m in enumerate(t["messages"])]
    return {
        "plans_reader_runs": [b.run], "plans_reader_files": b.files, "plans_reader_documents": b.documents,
        "plans_reader_groups": groups, "plans_reader_outputs": outputs, "plans_reader_deliverables": deliverables,
        "plans_reader_calls": calls, "plans_reader_chat_threads": threads, "plans_reader_chat_messages": messages,
    }


def test_export_run_writes_the_bundle_layout(bundle_dir, tmp_path):
    sb = _FakeSb(_dev_tables(bundle_dir))
    out = B.export_run(sb, F.RUN_ID, tmp_path / "export", case_id="7154",
                       settings=SimpleNamespace(is_production=False, supabase_url=f"https://{B.DEV_REF}.supabase.co"))
    assert (out / "bundle.json").is_file()
    exported = B.load_bundle(out, require_meta=True)
    assert exported.case_id == "7154" and exported.source == "dev_db"
    assert exported.categories == ["electrical_drawings", "specifications"]
    assert exported.boq_spec() == B.load_bundle(bundle_dir).boq_spec()
    assert exported.deliverable_versions("estimate") == [1, 2]
    assert all("output" not in json.loads((out / "groups.json").read_text())[i] for i in range(3))
    assert (out / "groups" / "sub_electrical_drawings_0_p1.json").is_file()
    assert exported.calls[0]["request_snapshot"]["parts"][0]["image_url"]["stripped"] is True
    assert exported.chat[0]["messages"][0]["role"] == "user" and exported.chat_probe() is not None
    assert "spec_json" not in exported.deliverable_rows[0]


def test_export_run_refuses_production_and_unknown_run(bundle_dir, tmp_path):
    sb = _FakeSb(_dev_tables(bundle_dir))
    with pytest.raises(B.BundleError, match="production"):
        B.export_run(sb, F.RUN_ID, tmp_path / "x", settings=SimpleNamespace(is_production=True, supabase_url=""))
    with pytest.raises(B.BundleError, match="BDR_Prod"):
        B.export_run(sb, F.RUN_ID, tmp_path / "x", supabase_url=f"https://{B.PROD_REF}.supabase.co")
    with pytest.raises(B.BundleError, match="not found"):
        B.export_run(sb, "nope", tmp_path / "x", supabase_url=f"https://{B.DEV_REF}.supabase.co")
    assert not (tmp_path / "x").exists()


@golden
@pytest.mark.golden
def test_weekend_bundle_loads_and_resolves_backup_pdfs():
    b = B.load_bundle(WEEKEND)
    assert b.run_id.startswith("ff8fad25") and len(b.groups) == 11 and len(b.documents) == 6
    assert len(b.category_output("electrical_drawings")["counts"]) == 54
    assert len(b.group_outputs) == 11
    assert sorted(b.deliverables) == [("boq", 1), ("brief", 1), ("estimate", 1), ("estimate", 2), ("marked_plans", 1)]
    assert len(b.calls) == 12 and b.chat_probe() is not None and "not available" in b.chat_probe()["answer"]
    elec = next(d for d in b.documents if d["name"].startswith("Reference Material - Architectural - ELEC"))
    pdf = b.doc_bytes(elec["id"])
    assert len(pdf) == 3132314
    exhibit = next(d for d in b.documents if d["name"].startswith("Exhibit A"))
    assert len(PdfReader(__import__("io").BytesIO(b.doc_bytes(exhibit["id"]))).pages) == 6
    if (WEEKEND / "bundle.json").is_file():
        assert b.case_id == "7154" and B.verify_members(WEEKEND) == []
