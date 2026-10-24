"""scripts/plans_reader_import_run.py: the minimal import harness (WS4 step 0).
The planning is pure and runs on the committed 7154 fixtures with a fake
backup of blank PDFs; execution runs against a fake Supabase client and a
fake uploader. Nothing here touches a database, storage or the network. The
golden test plans the real bundle against the real production backup (read
only) when both are on this machine."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from tests.plans_reader_fixtures import EVAL_BUNDLE_7154, FIXTURE_7154, load_json

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_import_run.py"
REAL_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")


def _load_script():
    if "plans_reader_import_run" in sys.modules:
        return sys.modules["plans_reader_import_run"]
    spec = importlib.util.spec_from_file_location("plans_reader_import_run", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plans_reader_import_run"] = mod
    spec.loader.exec_module(mod)
    return mod


imp = _load_script()


def _pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture
def fake_backup(tmp_path: Path) -> Path:
    """A backup with data/project_files.json rows for the six fixture files
    and blank PDFs of the right page counts at their storage paths. The
    folder names deliberately disagree with the category (map by the row)."""
    files = load_json("files.json")
    rows = []
    for i, f in enumerate(files):
        storage_path = f"proj/folder{i}/{i:032x}-{f['filename']}"
        rows.append({"id": f["source_project_file_id"], "category": "specification" if i % 2 else "electrical_drawing",
                     "storage_path": storage_path, "filename": f["filename"], "size_bytes": 10})
        path = tmp_path / "storage" / "project-files" / storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_pdf(int(f["page_count"] or 1)))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "project_files.json").write_text(json.dumps(rows))
    return tmp_path


def _ids():
    n = iter(range(1, 10_000))
    return lambda: f"00000000-0000-4000-8000-{next(n):012d}"


def _path_for(run_id: str, kind: str, filename: str) -> str:
    return f"plans-reader/{run_id}/{kind}/{filename}"


def _plan(fake_backup: Path, **kw):
    bundle = imp.load_bundle(FIXTURE_7154)
    sources = imp.locate_sources(bundle, fake_backup)
    return bundle, imp.plan_import(bundle, sources, new_id=_ids(), path_for=_path_for, now=lambda: "2026-10-21T00:00:00+00:00", **kw)


# Bundle and sources --------------------------------------------------------------------------


def test_load_bundle_resolves_every_reading_group_output():
    bundle = imp.load_bundle(FIXTURE_7154)
    reading = [g for g in bundle.groups if g["kind"] in ("sub", "rollup", "escalation")]
    assert len(reading) == 9
    assert all(str(g["id"]) in bundle.group_outputs for g in reading)
    rollup = next(g for g in bundle.groups if g["kind"] == "rollup")
    assert len(bundle.group_outputs[rollup["id"]]["counts"]) == 54
    assert "escalation_patches" not in bundle.group_outputs[rollup["id"]]
    assert set(bundle.category_outputs) == {"electrical_drawings", "rfp", "specifications"}
    assert bundle.main_output is not None


def test_locate_sources_maps_by_project_file_row(fake_backup):
    bundle = imp.load_bundle(FIXTURE_7154)
    sources = imp.locate_sources(bundle, fake_backup)
    assert len(sources) == 6
    for f in bundle.files:
        src = sources[f["id"]]
        assert src.project_file_id == f["source_project_file_id"]
        assert src.path.is_file() and src.filename == f["filename"]


def test_missing_source_pdf_is_refused(fake_backup):
    bundle = imp.load_bundle(FIXTURE_7154)
    victim = next(fake_backup.joinpath("storage").rglob("*.pdf"))
    victim.unlink()
    with pytest.raises(imp.ImportRefused, match="not found in the backup"):
        imp.locate_sources(bundle, fake_backup)
    with pytest.raises(imp.ImportRefused):
        imp.locate_sources(bundle, fake_backup / "nowhere")


# Plan ------------------------------------------------------------------------------------------


def test_plan_starts_at_consolidating_with_remapped_ids(fake_backup):
    bundle, plan = _plan(fake_backup, rows_version=1)  # the v1 outputs verbatim; v2 rows are the default (tests below)
    run = plan.run_row
    assert run["status"] == "consolidating" and run["source"] == "upload" and run["project_id"] is None
    assert not [k for k in run if "cost" in k or "tokens" in k]
    assert run["groups_total"] == 9 and run["groups_done"] == 9 and run["documents_total"] == 6
    assert run["settings_snapshot"]["plans_reader_escalations_enabled"] is True
    assert run["settings_snapshot"]["plans_reader_recount_enabled"] is False
    assert run["settings_snapshot"]["import"]["source_run_id"] == bundle.run["id"]
    assert len(plan.file_rows) == 6 and len(plan.document_rows) == 6
    assert {k: sum(1 for g in plan.group_rows if g["kind"] == k) for k in ("sub", "rollup", "escalation")} == {
        "sub": 5, "rollup": 1, "escalation": 3}
    assert all(g["status"] == "done" and g["output"] for g in plan.group_rows)
    assert [o["category"] for o in plan.output_rows] == ["electrical_drawings", "rfp", "specifications"]
    # no bundle id survives in what gets inserted (the import note names the source run on purpose)
    run_wo_note = dict(run, settings_snapshot={k: v for k, v in run["settings_snapshot"].items() if k != "import"})
    blob = json.dumps([run_wo_note, plan.file_rows, plan.document_rows, plan.group_rows, plan.output_rows])
    old_ids = [bundle.run["id"]] + [f["id"] for f in bundle.files] + [d["id"] for d in bundle.documents] + [
        g["id"] for g in bundle.groups if g["kind"] in ("sub", "rollup", "escalation")]
    assert not [i for i in old_ids if i in blob]
    # the D-numbering is kept (same created_at per document)
    from app.services.plans_reader_rows import doc_order

    old = doc_order(bundle.documents)
    new = doc_order(plan.document_rows)
    assert {plan.id_map[k]: v for k, v in old.items()} == new
    # storage paths are the run's own; every object comes from the backup
    assert all(f["storage_path"].startswith(f"plans-reader/{plan.run_id}/source/") for f in plan.file_rows)
    assert len(plan.uploads) == 6 and all(u.pages is None for u in plan.uploads)
    assert {d["storage_path"] for d in plan.document_rows} <= {f["storage_path"] for f in plan.file_rows}
    # the stored category output is kept verbatim (ids remapped) with escalations on
    elec = next(o for o in plan.output_rows if o["category"] == "electrical_drawings")
    assert len(elec["output"]["escalation_patches"]) == 3
    assert set(elec["source_group_ids"]) <= {g["id"] for g in plan.group_rows}


def test_plan_escalations_off_rebuilds_outputs(fake_backup):
    bundle, plan = _plan(fake_backup, escalations=False, recounts=True, rows_version=1)
    assert not [g for g in plan.group_rows if g["kind"] == "escalation"]
    elec = next(o for o in plan.output_rows if o["category"] == "electrical_drawings")["output"]
    assert elec["escalation_patches"] == []
    assert len(elec["counts"]) == 54
    stored = bundle.category_outputs["electrical_drawings"]
    assert len(elec["unresolved_flags"]) >= len(stored["unresolved_flags"])
    snap = plan.run_row["settings_snapshot"]
    assert snap["plans_reader_escalations_enabled"] is False and snap["plans_reader_recount_enabled"] is True
    assert any("escalations off" in n for n in plan.notes) and any("recounts on" in n for n in plan.notes)


def test_plan_project_and_name(fake_backup):
    _bundle, plan = _plan(fake_backup, name="7154 import", project_id="p-1", created_by="u-1")
    assert plan.run_row["name"] == "7154 import"
    assert plan.run_row["source"] == "project" and plan.run_row["project_id"] == "p-1"
    assert plan.run_row["created_by"] == "u-1"


def test_segment_documents_are_cut_to_their_pages(fake_backup):
    bundle = imp.load_bundle(FIXTURE_7154)
    rfp = next(d for d in bundle.documents if d["category"] == "rfp")
    rfp["page_meta"] = [{"page": 2, "pdf_page": 1}, {"page": 3, "pdf_page": 2}]
    rfp["page_count"], rfp["page_start"], rfp["page_end"] = 2, 2, 3
    sources = imp.locate_sources(bundle, fake_backup)
    plan = imp.plan_import(bundle, sources, new_id=_ids(), path_for=_path_for)
    cut = [u for u in plan.uploads if u.pages is not None]
    assert len(cut) == 1 and cut[0].pages == [2, 3]
    doc = next(d for d in plan.document_rows if d["category"] == "rfp")
    assert doc["storage_path"] == cut[0].storage_path and "/segments/" in doc["storage_path"]
    out = imp.cut_pages(_pdf(6), cut[0].pages)
    assert len(PdfReader(io.BytesIO(out)).pages) == 2


# Execute ---------------------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, sb: _FakeSB, name: str) -> None:
        self.sb, self.name, self.op, self.payload, self.filters = sb, name, None, None, []

    def insert(self, rows):
        self.op, self.payload = "insert", rows
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def execute(self):
        if self.op == "insert" and self.name == self.sb.fail_on:
            raise RuntimeError(f"insert into {self.name} failed")
        self.sb.calls.append((self.op, self.name, self.payload if self.op == "insert" else self.filters))
        data = self.payload if isinstance(self.payload, list) else [self.payload]
        return SimpleNamespace(data=data)


class _FakeSB:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_on = fail_on

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self, name)


def test_execute_uploads_then_inserts_in_order(fake_backup):
    _bundle, plan = _plan(fake_backup)
    sb = _FakeSB()
    uploaded: list[tuple[str, int, str]] = []
    run_id = imp.execute_plan(sb, plan, upload=lambda p, b, m: uploaded.append((p, len(b), m)), delete_object=lambda p: None)
    assert run_id == plan.run_id
    assert [u[0] for u in uploaded] == [u.storage_path for u in plan.uploads]
    assert all(m == "application/pdf" for _p, _n, m in uploaded)
    assert [c[1] for c in sb.calls] == [
        "plans_reader_runs", "plans_reader_files", "plans_reader_documents", "plans_reader_groups", "plans_reader_outputs"]
    assert sb.calls[0][2]["status"] == "consolidating"


def test_execute_failure_cleans_up(fake_backup):
    _bundle, plan = _plan(fake_backup)
    sb = _FakeSB(fail_on="plans_reader_groups")
    deleted: list[str] = []
    with pytest.raises(RuntimeError, match="plans_reader_groups"):
        imp.execute_plan(sb, plan, upload=lambda p, b, m: None, delete_object=deleted.append)
    assert ("delete", "plans_reader_runs", [("id", plan.run_id)]) in sb.calls
    assert deleted == [u.storage_path for u in plan.uploads]


def test_dev_target_preflight():
    imp.check_dev_target("https://bpidntbyvoooqvaispup.supabase.co")
    for url in ("https://zxqkqotlcgbalwoqeeca.supabase.co", "https://qpfuugcwsdfpxvdgwvwx.supabase.co", "", None):
        with pytest.raises(imp.ImportRefused):
            imp.check_dev_target(url)


def test_main_dry_run_touches_nothing(fake_backup, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry run must not reach the database")

    monkeypatch.setattr(imp, "execute_plan", boom)
    assert imp.main(["--bundle", str(FIXTURE_7154), "--backup", str(fake_backup), "--escalations", "off", "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "consolidating" and summary["groups"]["escalation"] == 0
    with pytest.raises(SystemExit):
        imp.build_parser().parse_args(["--bundle", "x", "--escalations", "maybe"])


@pytest.mark.golden
@pytest.mark.skipif(
    not (EVAL_BUNDLE_7154.is_dir() and (REAL_BACKUP / "data" / "project_files.json").is_file()),
    reason="EVAL_ROOT bundle or the production backup is not on this machine",
)
def test_plan_real_bundle_against_real_backup():
    bundle = imp.load_bundle(EVAL_BUNDLE_7154)
    sources = imp.locate_sources(bundle, REAL_BACKUP)
    assert len(sources) == 6 and all(s.path.is_file() for s in sources.values())
    plan = imp.plan_import(bundle, sources, new_id=_ids(), path_for=_path_for)
    assert plan.run_row["status"] == "consolidating"
    assert len(plan.uploads) == 6 and len(plan.group_rows) == 9


# WS4 wave 3: v2 rows, --consolidator, --page-facts -------------------------------------


def _v2_plan(fake_backup, **kw):
    bundle = imp.load_bundle(FIXTURE_7154)
    sources = imp.locate_sources(bundle, fake_backup)
    return bundle, imp.plan_import(bundle, sources, path_for=lambda run_id, kind, name: f"plans-reader/{run_id}/{kind}/{name}", **kw)


def test_plan_stores_v2_rows_by_default_with_floors_from_page_facts(fake_backup):
    bundle = imp.load_bundle(FIXTURE_7154)
    facts = imp.load_page_facts(FIXTURE_7154 / "page_facts_elec.json", bundle.documents)
    elec = next(d["id"] for d in bundle.documents if d["name"].startswith("Reference Material - Architectural - ELEC"))
    mech = next(d["id"] for d in bundle.documents if d["name"].startswith("Reference Material - Architectural - MECH"))
    assert facts and {doc for doc, _page in facts} == {elec, mech}  # matched by name to the bundle's documents
    bundle, plan = _v2_plan(fake_backup, page_facts=facts)
    outputs = {o["category"]: o["output"] for o in plan.output_rows}
    elec_out = outputs["electrical_drawings"]
    assert elec_out.get("rows_version") == 2 and plan.run_row["settings_snapshot"]["import"]["rows_version"] == 2
    counts = [r for r in elec_out["counts"] if r["status"] == "active"]
    assert len(counts) >= 54 and all(r["row_id"].startswith("C") for r in counts)
    new_ids = set(plan.id_map.values())
    assert all(r["sheet"]["document_id"] in new_ids for r in counts)  # remapped before the rebuild
    assert elec_out["floors"] and any(f["family"] == "demo_x" and f["floor"] == 39 for f in elec_out["floors"])
    assert outputs["rfp"].get("rows_version") == 2 and outputs["specifications"].get("rows_version") == 2
    assert any("v2 rows" in n for n in plan.notes)
    assert plan.summary()["rows_version"] == 2 and plan.summary()["consolidator"] is None
    assert "plans_reader_consolidator_mode" not in plan.run_row["settings_snapshot"]
    assert plan.run_row["settings_snapshot"]["import"]["page_facts_pages"] == len(facts)


def test_plan_v1_rows_and_consolidator_flag(fake_backup):
    bundle, plan = _v2_plan(fake_backup, rows_version=1, consolidator="tools")
    elec_out = next(o["output"] for o in plan.output_rows if o["category"] == "electrical_drawings")
    assert "rows_version" not in elec_out and elec_out["counts"][0].get("row_id") is None
    assert plan.run_row["settings_snapshot"]["plans_reader_consolidator_mode"] == "tools"
    assert plan.run_row["settings_snapshot"]["import"]["consolidator"] == "tools"
    assert any("consolidator tools" in n and "PLANS_READER_CONSOLIDATOR_MODE" in n for n in plan.notes)
    _bundle, legacy = _v2_plan(fake_backup, consolidator="legacy", escalations=False)
    assert legacy.run_row["settings_snapshot"]["plans_reader_consolidator_mode"] == "legacy"
    elec_v2 = next(o["output"] for o in legacy.output_rows if o["category"] == "electrical_drawings")
    assert elec_v2["rows_version"] == 2 and not elec_v2.get("escalation_patches")
    with pytest.raises(imp.ImportRefused):
        _v2_plan(fake_backup, rows_version=3)
    with pytest.raises(imp.ImportRefused):
        _v2_plan(fake_backup, consolidator="auto")


def test_cli_flags_for_rows_consolidator_and_page_facts(fake_backup, capsys, monkeypatch):
    monkeypatch.setattr(imp, "DEFAULT_BACKUP", fake_backup)
    code = imp.main(["--bundle", str(FIXTURE_7154), "--backup", str(fake_backup), "--rows", "v2", "--consolidator", "tools",
                     "--page-facts", str(FIXTURE_7154 / "page_facts_elec.json"), "--dry-run"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows_version"] == 2 and out["consolidator"] == "tools" and out["status"] == "consolidating"
    assert any("floors" in n and "electrical_drawings" in n for n in out["notes"])
    parser = imp.build_parser()
    # wave 3 fix WB (mandated): the default is auto, stored when the bundle has stored v2 outputs, else v2
    assert parser.parse_args(["--bundle", "x"]).rows == "auto" and parser.parse_args(["--bundle", "x"]).consolidator is None


# X1: page facts of every imported document in the facts store ------------------------------


def test_persist_page_facts_extracts_every_imported_document_drawings_first(fake_backup):
    bundle = imp.load_bundle(FIXTURE_7154)
    rfp = next(d for d in bundle.documents if d["category"] == "rfp")
    rfp["page_meta"] = [{"page": 2, "pdf_page": 1}, {"page": 3, "pdf_page": 2}]
    rfp["page_count"], rfp["page_start"], rfp["page_end"] = 2, 2, 3
    plan = imp.plan_import(bundle, imp.locate_sources(bundle, fake_backup), new_id=_ids(), path_for=_path_for)
    seen: list[tuple[str, int]] = []

    def store_facts(sb, document_id, *, pdf_bytes=None, settings=None):
        seen.append((document_id, len(PdfReader(io.BytesIO(pdf_bytes)).pages)))
        if document_id == plan.document_rows[-1]["id"]:
            raise RuntimeError("pdfium exploded")
        return {"status": "done", "cache": "miss"}

    lines = imp.persist_page_facts(object(), plan, store_facts=store_facts)
    by_id = {d["id"]: d for d in plan.document_rows}
    assert {doc for doc, _n in seen} == set(by_id)  # every imported document, by its NEW id
    cats = [by_id[doc]["category"] for doc, _n in seen]
    assert cats[:2] == ["electrical_drawings", "electrical_drawings"] and "electrical_drawings" not in cats[2:]
    assert dict(seen)[next(d["id"] for d in plan.document_rows if d["category"] == "rfp")] == 2  # the cut segment's bytes
    assert all(dict(seen)[d["id"]] == d["page_count"] for d in plan.document_rows if d["category"] != "rfp")
    failed = [line for line in lines if line["status"] == "failed"]
    assert len(failed) == 1 and "pdfium exploded" in failed[0]["error"] and len(lines) == len(plan.document_rows)


def test_persist_page_facts_writes_the_store_the_derive_reads(fake_backup, monkeypatch):
    """Real extraction ($0) of the blank fake PDFs through extract_and_store
    into a fake client: every imported document ends facts done with the
    cache rows the measure's page facts reader loads."""
    from app.services.pagefacts import api
    from tests.pagefacts_db import FakeDB, install_storage

    _bundle, plan = _plan(fake_backup)
    db = FakeDB()
    blobs = install_storage(monkeypatch)
    imp.execute_plan(db, plan, upload=lambda p, b, m: blobs.__setitem__(p, b), delete_object=lambda p: None)
    api.clear_caches()
    s = SimpleNamespace(plans_reader_page_facts_enabled=False, plans_reader_facts_relabel=True,
                        plans_reader_facts_page_budget_s=4.0, plans_reader_facts_doc_budget_s=60.0)
    lines = imp.persist_page_facts(db, plan, settings=s)
    assert [line["status"] for line in lines] == ["done"] * len(plan.document_rows)
    for d in plan.document_rows:
        row = db.rows("plans_reader_documents", id=d["id"])[0]
        assert row["facts_status"] == "done" and row["sha256"] and row["category"] == d["category"]  # never relabeled
        assert api.doc_facts(db, d["id"]).page_count == d["page_count"]
    api.clear_caches()


class _ReadSB:
    """A read-only fake for backfill_page_facts (select/eq/limit/execute)."""

    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.tables = tables

    def table(self, name):
        rows = list(self.tables.get(name, []))
        q = SimpleNamespace()
        q.select = lambda *_a, **_k: q
        q.limit = lambda _n: q

        def eq(key, value):
            rows[:] = [r for r in rows if str(r.get(key)) == str(value)]
            return q

        q.eq = eq
        q.execute = lambda: SimpleNamespace(data=rows)
        return q


def test_backfill_page_facts_for_an_imported_run_only():
    run_id = "r-1"
    sb = _ReadSB({
        "plans_reader_runs": [{"id": run_id, "settings_snapshot": {"import": {"bundle": "x"}}}, {"id": "r-live", "settings_snapshot": {}}],
        "plans_reader_documents": [
            {"id": "d-spec", "run_id": run_id, "category": "specifications", "name": "SPECS", "status": "done"},
            {"id": "d-elec", "run_id": run_id, "category": "electrical_drawings", "name": "ELEC", "status": "done"},
            {"id": "d-gone", "run_id": run_id, "category": "electrical_drawings", "name": "BAD", "status": "failed"},
        ],
    })
    calls: list = []
    lines = imp.backfill_page_facts(sb, run_id, store_facts=lambda sb, doc_id, *, pdf_bytes=None, settings=None: calls.append((doc_id, pdf_bytes)) or {"status": "done"})
    assert calls == [("d-elec", None), ("d-spec", None)]  # drawings first, stored PDFs, failed documents skipped
    assert [line["status"] for line in lines] == ["done", "done"]
    with pytest.raises(imp.ImportRefused, match="not made by this import harness"):
        imp.backfill_page_facts(sb, "r-live", store_facts=lambda *a, **k: {})
    with pytest.raises(imp.ImportRefused, match="does not exist"):
        imp.backfill_page_facts(sb, "r-none", store_facts=lambda *a, **k: {})


def test_cli_facts_flags(fake_backup, capsys):
    assert imp.main(["--bundle", str(FIXTURE_7154), "--backup", str(fake_backup), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["page_facts_store"].startswith("WS1 facts extracted and stored for 6 documents")
    assert imp.main(["--bundle", str(FIXTURE_7154), "--backup", str(fake_backup), "--facts", "off", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["page_facts_store"] == "off"
    parser = imp.build_parser()
    assert parser.parse_args(["--facts-for-run", "abc"]).facts_for_run == "abc" and parser.parse_args(["--bundle", "x"]).facts is True
    with pytest.raises(SystemExit):
        imp.main(["--dry-run"])  # neither --bundle nor --facts-for-run


# Wave 3 fix WB: --rows stored (a re-run from consolidating reuses the live merge) -------------


RECOUNT_ID = "7e57c0de-0000-4000-8000-00000000abcd"


@pytest.fixture
def stored_bundle(tmp_path: Path) -> Path:
    """The fixture bundle turned into a live-run bundle: every category
    output stored as a v2 row set (rows_version) under the BUNDLE ids, the
    electrical one with a sentinel row quantity, a floor and a merge_log
    marker the rebuild would never produce, plus an electrical version 2
    that a recount group (also added to the bundle) patched."""
    root = tmp_path / "bundle"
    shutil.copytree(FIXTURE_7154, root)
    bundle = imp.load_bundle(root)
    groups = [dict(g, output=bundle.group_outputs[str(g["id"])]) for g in bundle.groups
              if g["kind"] in ("sub", "rollup", "escalation") and str(g["id"]) in bundle.group_outputs]
    for cat in ("electrical_drawings", "rfp", "specifications"):
        out = imp.rebuild_v2_output(cat, groups, documents=bundle.documents, ledger=bundle.run.get("ledger") or {})
        assert out is not None and out.get("rows_version")
        if cat == "electrical_drawings":
            out["counts"][0]["quantity"] = 4242
            elec_doc = next(d["id"] for d in bundle.documents if d["category"] == "electrical_drawings")
            out["floors"] = [{"document_id": elec_doc, "page": 1, "family": "device_mod", "key": "device_mod", "floor": 7, "method": "text_layer"}]
            out["merge_log"] = dict(out.get("merge_log") or {}, marker="live")
            v2 = copy.deepcopy(out)
            v2["source"] = dict(v2.get("source") or {}, group_ids=list((v2.get("source") or {}).get("group_ids") or []) + [RECOUNT_ID])
            v2["counts"][1]["origin"] = {"group_id": RECOUNT_ID, "group_kind": "recount", "local_id": "p1.c1", "pass": 3}
            (root / "outputs" / f"category_{cat}_v2.json").write_text(json.dumps(v2))
        (root / "outputs" / f"category_{cat}_v1.json").write_text(json.dumps(out))
    all_groups = json.loads((root / "groups.json").read_text())
    elec_doc = next(d["id"] for d in bundle.documents if d["category"] == "electrical_drawings")
    recount = {"id": RECOUNT_ID, "run_id": bundle.run["id"], "kind": "recount", "category": "electrical_drawings",
               "group_index": 0, "pass": 3, "status": "done", "pages": [{"document_id": elec_doc, "page": 1}]}
    (root / "groups.json").write_text(json.dumps(all_groups + [recount]))
    (root / "groups" / imp.group_output_name(recount)).write_text(json.dumps({"counts": [], "recounts": [{"document_id": elec_doc}]}))
    return root


def _stored_plan(root: Path, fake_backup: Path, **kw):
    bundle = imp.load_bundle(root)
    sources = imp.locate_sources(bundle, fake_backup)
    return bundle, imp.plan_import(bundle, sources, new_id=_ids(), path_for=_path_for, now=lambda: "2026-10-22T00:00:00+00:00", **kw)


def test_load_bundle_keeps_every_category_output_version(stored_bundle):
    bundle = imp.load_bundle(stored_bundle)
    assert sorted(bundle.category_versions["electrical_drawings"]) == [1, 2]
    assert bundle.category_outputs["electrical_drawings"] is bundle.category_versions["electrical_drawings"][1]
    assert imp.stored_rows_problem(bundle) is None
    fixture = imp.load_bundle(FIXTURE_7154)
    assert "rows_version" in (imp.stored_rows_problem(fixture) or "")


def test_rows_mode_resolution(stored_bundle):
    live, fixture = imp.load_bundle(stored_bundle), imp.load_bundle(FIXTURE_7154)
    assert imp.resolve_rows_mode(live, None)[0] == "stored"
    assert imp.resolve_rows_mode(live, "auto")[0] == "stored"
    assert imp.resolve_rows_mode(live, None, escalations=False) == (
        "v2", "default: rebuilt from the groups (escalations off (the stored rows carry the escalation patches))")
    mode, why = imp.resolve_rows_mode(fixture, None)
    assert mode == "v2" and "carries no rows_version" in why
    assert imp.resolve_rows_mode(fixture, 1)[0] == "v1" and imp.resolve_rows_mode(fixture, "v2")[0] == "v2"
    with pytest.raises(imp.ImportRefused, match="rows_version"):
        imp.resolve_rows_mode(fixture, "stored")
    with pytest.raises(imp.ImportRefused, match="escalations on"):
        imp.resolve_rows_mode(live, "stored", escalations=False)
    with pytest.raises(imp.ImportRefused):
        imp.resolve_rows_mode(live, 3)


def test_plan_stored_rows_are_the_live_outputs_verbatim(stored_bundle, fake_backup):
    bundle, plan = _stored_plan(stored_bundle, fake_backup)  # the default on a live-run bundle
    snap = plan.run_row["settings_snapshot"]["import"]
    assert snap["rows_mode"] == "stored" and snap["rows_version"] == 2
    assert plan.summary()["rows_mode"] == "stored" and plan.notes[0].startswith("rows stored:")
    # every stored version at its own number, verbatim but for the remapped ids
    assert [(o["category"], o["version"]) for o in plan.output_rows] == [
        ("electrical_drawings", 1), ("electrical_drawings", 2), ("rfp", 1), ("specifications", 1)]
    back = {v: k for k, v in plan.id_map.items()}
    for o in plan.output_rows:
        assert o["output"] == imp.remap_ids(bundle.category_versions[o["category"]][o["version"]], plan.id_map)
        assert imp.remap_ids(o["output"], back) == bundle.category_versions[o["category"]][o["version"]]
    elec1, elec2 = plan.output_rows[0]["output"], plan.output_rows[1]["output"]
    assert elec1["counts"][0]["quantity"] == 4242 and elec1["merge_log"]["marker"] == "live"  # not rebuilt
    assert elec1["floors"][0]["document_id"] in set(plan.id_map.values())  # floors cite the new documents
    assert [r["row_id"] for r in elec1["counts"]] == [r["row_id"] for r in bundle.category_versions["electrical_drawings"][1]["counts"]]
    # the recount group comes along, so the patched origin and source ids name groups of the new run
    recount = [g for g in plan.group_rows if g["kind"] == "recount"]
    assert len(recount) == 1 and recount[0]["id"] == plan.id_map[RECOUNT_ID] and recount[0]["status"] == "done"
    assert elec2["counts"][1]["origin"]["group_id"] == recount[0]["id"]
    group_ids = {g["id"] for g in plan.group_rows}
    assert recount[0]["id"] in plan.output_rows[1]["source_group_ids"] and recount[0]["id"] not in plan.output_rows[0]["source_group_ids"]
    assert all(set(o["source_group_ids"]) <= group_ids for o in plan.output_rows)
    assert plan.run_row["groups_total"] == plan.run_row["groups_done"] == 10 and plan.summary()["groups"]["recount"] == 1
    # no imported bundle id survives in what gets inserted
    blob = json.dumps([plan.file_rows, plan.document_rows, plan.group_rows, plan.output_rows])
    assert not [old for old in plan.id_map if old in blob]


def test_plan_stored_rows_ignore_page_facts_and_v2_still_rebuilds(stored_bundle, fake_backup):
    bundle = imp.load_bundle(stored_bundle)
    facts = imp.load_page_facts(FIXTURE_7154 / "page_facts_elec.json", bundle.documents)
    _b, plan = _stored_plan(stored_bundle, fake_backup, page_facts=facts)
    assert any("--page-facts ignored" in n for n in plan.notes)
    assert plan.output_rows[0]["output"]["floors"] == imp.remap_ids(bundle.category_versions["electrical_drawings"][1]["floors"], plan.id_map)
    _b, rebuilt = _stored_plan(stored_bundle, fake_backup, rows_version="v2")
    assert rebuilt.run_row["settings_snapshot"]["import"]["rows_mode"] == "v2"
    assert not [g for g in rebuilt.group_rows if g["kind"] == "recount"]
    assert [(o["category"], o["version"]) for o in rebuilt.output_rows] == [("electrical_drawings", 1), ("rfp", 1), ("specifications", 1)]
    elec = rebuilt.output_rows[0]["output"]
    assert elec["counts"][0]["quantity"] != 4242 and "marker" not in (elec.get("merge_log") or {}) and not elec.get("floors")
    _b, v1 = _stored_plan(stored_bundle, fake_backup, rows_version=1)
    assert v1.run_row["settings_snapshot"]["import"]["rows_mode"] == "v1"


def test_cli_dry_run_prints_the_rows_mode(stored_bundle, fake_backup, capsys):
    assert imp.main(["--bundle", str(stored_bundle), "--backup", str(fake_backup), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows_mode"] == "stored" and "rows_version" in out["rows_mode_reason"]
    assert out["output_versions"] == ["electrical_drawings v1", "electrical_drawings v2", "rfp v1", "specifications v1"]
    assert imp.main(["--bundle", str(stored_bundle), "--backup", str(fake_backup), "--rows", "v2", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["rows_mode"] == "v2"
    assert imp.main(["--bundle", str(FIXTURE_7154), "--backup", str(fake_backup), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows_mode"] == "v2" and out["rows_mode_reason"].startswith("default: rebuilt from the groups")
    with pytest.raises(imp.ImportRefused):
        imp.main(["--bundle", str(FIXTURE_7154), "--backup", str(fake_backup), "--rows", "stored", "--dry-run"])
    with pytest.raises(imp.ImportRefused, match="escalations on"):
        imp.main(["--bundle", str(stored_bundle), "--backup", str(fake_backup), "--rows", "stored", "--escalations", "off", "--dry-run"])


LIVE_BUNDLE_7154_RUN2 = EVAL_BUNDLE_7154.parent / "2492ba7a"


@pytest.mark.golden
@pytest.mark.skipif(
    not (LIVE_BUNDLE_7154_RUN2.is_dir() and (REAL_BACKUP / "data" / "project_files.json").is_file()),
    reason="EVAL_ROOT bundle 7154/2492ba7a or the production backup is not on this machine",
)
def test_stored_rows_on_live_7154_run2_match_the_live_merge():
    """Integration run 2 (2492ba7a): the stored rows are the live merge's
    latest version (floors, recount patches); the v2 rebuild has no floors
    and none of the recount patches."""
    bundle = imp.load_bundle(LIVE_BUNDLE_7154_RUN2)
    sources = imp.locate_sources(bundle, REAL_BACKUP)
    new_id = lambda: str(uuid.uuid4())  # noqa: E731
    plan = imp.plan_import(bundle, sources, new_id=new_id, path_for=_path_for)
    assert plan.run_row["settings_snapshot"]["import"]["rows_mode"] == "stored"
    elec = [o for o in plan.output_rows if o["category"] == "electrical_drawings"]
    assert [o["version"] for o in elec] == [1, 2]
    back = {v: k for k, v in plan.id_map.items()}
    live = json.loads((LIVE_BUNDLE_7154_RUN2 / "outputs" / "category_electrical_drawings_v2.json").read_text())
    assert imp.remap_ids(elec[-1]["output"], back) == live
    assert len(elec[-1]["output"]["floors"]) == 11
    assert sum(1 for r in elec[-1]["output"]["counts"] if (r.get("origin") or {}).get("group_kind") == "recount") == 9
    rebuilt = imp.plan_import(bundle, sources, new_id=new_id, path_for=_path_for, rows_version=2)
    relec = next(o["output"] for o in rebuilt.output_rows if o["category"] == "electrical_drawings")
    assert not relec.get("floors")
    assert not [r for r in relec["counts"] if (r.get("origin") or {}).get("group_kind") == "recount"]
