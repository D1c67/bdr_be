"""scripts/cost_library_backfill.py: the dev-only guard, the dry run (counts
only) and the idempotent write through a fake client. The backup parse is
replaced by synthetic workbooks (no estimator numbers in the repo)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import cost_library as cl
from app.services import estimate_parse as ep
from tests import ai_pricing_fakes as fx

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cost_library_backfill.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("cost_library_backfill", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cost_library_backfill"] = mod
    spec.loader.exec_module(mod)
    return mod


def _line(desc: str, unit: str, qty: float, hours: float, material: float, row: int) -> ep.ParsedLine:
    return ep.ParsedLine(
        sheet="Detailed Estimate", row=row, section="NEW WORK", group="OTHER ITEMS", subgroup="RECEPTACLES",
        description=desc, qty_base=Decimal(str(qty)), wastage=Decimal(0), quantity=Decimal(str(qty)), unit=unit,
        unit_labor_hours=Decimal(str(hours)), unit_material_cost=Decimal(str(material)), is_allowance=unit == "LS",
    )


def _book(labor_rate: float = 100) -> ep.ParsedEstimate:
    return ep.ParsedEstimate(
        sheets=["Detailed Estimate"], labor_rate=Decimal(str(labor_rate)), workbook_date=dt.date(2026, 6, 1),
        lines=[
            _line("DUPLEX RECEPTACLE", "EA", 10, 0.4, 10, 8),
            _line("DUPLEX RECEPTACLE, GFCI", "EA", 2, 0.45, 20, 9),
            _line("#12 THHN/THWN Cu", "FT", 500, 0.01, 0.2, 10),
            _line("ALLOWANCE ROW", "LS", 0, 0, 0, 11),
        ],
    )


@pytest.fixture
def fake_backup(monkeypatch, tmp_path):
    books = {"proj-a": _book(), "proj-b": _book(110), "holdout-1": _book()}
    entries = []
    for pid in books:
        f = tmp_path / f"{pid}.xlsx"
        f.write_bytes(b"synthetic")
        entries.append(ep.DryRunEntry(
            file=ep.EstimateFile(project_id=pid, file_id=f"file-{pid}", filename=f.name, path=f, created_at="2026-06-01T00:00:00",
                                 submission_round=None, estimator_deliverable=True),
            ok=True, error=None, chosen=True,
        ))
    report = ep.DryRunReport(entries=entries)
    monkeypatch.setattr(cl, "backup_books", lambda root: (report, books))
    return tmp_path


def test_refuses_any_database_but_dev():
    mod = _load_script()
    with pytest.raises(mod.RefusedError):
        mod.check_dev_url("https://zxqkqotlcgbalwoqeeca.supabase.co")
    with pytest.raises(mod.RefusedError):
        mod.check_dev_url(None)
    assert mod.check_dev_url("https://bpidntbyvoooqvaispup.supabase.co") == mod.DEV_REF
    with pytest.raises(mod.RefusedError):
        mod.run(["--dry-run"], supabase_url="https://qpfuugcwsdfpxvdgwvwx.supabase.co")


def test_backfill_plan_excludes_the_holdout_and_builds_items_with_search_text(fake_backup):
    plan = cl.backfill_plan(fake_backup, exclude_projects={"holdout-1"})
    assert plan.counts == {"sources": 2, "items": 3, "observations": 6, "projects": 2, "excluded_projects": 1, "skipped_projects": 1}
    assert plan.skipped_projects == ("holdout-1",)
    src = plan.sources[0]
    assert src["project_id"] == "proj-a" and src["file_id"] == "file-proj-a" and src["line_count"] == 3
    assert src["skipped"] == {"rows": 1, "reasons": {"allowance": 1}}
    assert src["workbook_date"] == "2026-06-01" and src["sha256"]
    keys = {(i["item_key"], i["uom"]) for i in plan.items}
    assert len(keys) == 3 and all(i["search_text"] for i in plan.items)
    assert all(o["source_id"] in {s["id"] for s in plan.sources} for o in plan.observations)
    assert cl.search_text_for(["duplex  receptacle", "DUPLEX RECEPTACLE", "x"]) == "DUPLEX RECEPTACLE | X"


def test_dry_run_prints_counts_and_writes_nothing(fake_backup, capsys, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod.eval_manifest, "excluded_projects", lambda purpose, case_id=None: frozenset({"holdout-1"}))
    out = mod.run(["--dry-run", "--backup", str(fake_backup), "--json"], supabase_url="https://bpidntbyvoooqvaispup.supabase.co", sb=None)
    assert out["dry_run"] is True and out["plan"]["observations"] == 6 and "written" not in out
    printed = capsys.readouterr().out
    assert '"observations": 6' in printed and "$" not in printed


def test_apply_plan_writes_the_three_tables_idempotently(fake_backup, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod.eval_manifest, "excluded_projects", lambda purpose, case_id=None: frozenset({"holdout-1"}))
    db = fx.FakeDB()
    out = mod.run(["--backup", str(fake_backup)], supabase_url="https://bpidntbyvoooqvaispup.supabase.co", sb=db)
    assert out["written"] == {"sources": 2, "items": 3, "observations": 6, "replaced_sources": 0}
    assert len(db.rows("cost_sources")) == 2 and len(db.rows("cost_items")) == 3 and len(db.rows("cost_observations")) == 6
    obs = db.rows("cost_observations")
    assert all(o.get("item_id") for o in obs) and all(o["source_id"] in {s["id"] for s in db.rows("cost_sources")} for o in obs)
    # A second run replaces each source's observations instead of duplicating them.
    out = mod.run(["--backup", str(fake_backup)], supabase_url="https://bpidntbyvoooqvaispup.supabase.co", sb=db)
    assert out["written"]["replaced_sources"] == 2
    assert len(db.rows("cost_sources")) == 2 and len(db.rows("cost_items")) == 3 and len(db.rows("cost_observations")) == 6
    # And the library loads back from the tables, ordered and paged, holdout-free.
    lib = cl.CostLibrary.load(db, exclude_projects={"holdout-1"})
    assert lib.n_projects == 2 and lib.n_observations == 6
    assert lib.lookup("DUPLEX RECEPTACLE", "EA").labor.value == Decimal("0.4")


def test_load_pages_in_order_and_raises_without_the_table():
    db = fx.FakeDB(missing={"cost_observations"})
    with pytest.raises(cl.LibraryUnavailable):
        cl.CostLibrary.load(db)
    with pytest.raises(cl.LibraryUnavailable):
        cl.CostLibrary.load(None)
    rows = [fx.obs(f"p{i % 3}", "DUPLEX RECEPTACLE", "EA", 0.4, 10).to_row() | {"id": i} for i in range(1, 1500)]
    db = fx.FakeDB({"cost_observations": rows})
    lib = cl.CostLibrary.load(db)
    assert lib.n_observations == 1499
    pages = [op for op in db.ops if op[0] == "cost_observations"]
    assert len(pages) == 2
