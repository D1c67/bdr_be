"""app/services/eval_bundle.py and plans_reader_replay.stored_artifacts: the
live-run members of a bundle (every output scope, pricing versions and lines,
quotes, reviews and findings, gate findings, recounts, sign-offs, the run's
autonomy columns), the re-export into an existing bundle directory, and the
weekend layout still loading with those members absent. Fake client and
synthetic numbers only."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import cost_library as cl
from app.services import eval_bundle as B
from app.services import plans_reader_acceptance as A
from app.services import plans_reader_replay as R
from app.services import pricing_engine as pe
from app.services import pricing_policy as pp
from app.services.electrical_items import parse_item
from app.services.pricing_types import Allowance, PriceRow, PricingContext

from tests import eval_fixtures as F

DEV = SimpleNamespace(is_production=False, supabase_url=f"https://{B.DEV_REF}.supabase.co")
OWN_PROJECT = "7154abcd-0000-4000-8000-0000000000aa"


class _Query:
    def __init__(self, name, rows, fail):
        self.name = name
        self.rows = rows
        self.fail = fail
        self.filters = {}

    def select(self, *_a):
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def execute(self):
        if self.name in self.fail:
            raise RuntimeError(f"relation {self.name} does not exist")
        rows = [r for r in self.rows if all(r.get(k) == v for k, v in self.filters.items())]
        return SimpleNamespace(data=[json.loads(json.dumps(r)) for r in rows])


class _FakeSb:
    def __init__(self, tables, fail=()):
        self.tables = tables
        self.fail = set(fail)

    def table(self, name):
        return _Query(name, self.tables.get(name, []), self.fail)


def _obs(project, desc, unit, hours, material):
    ident = parse_item(desc, unit)
    return cl.Observation(
        project_id=project, item_key=cl.library_key(ident), family=cl.library_family(ident),
        item_class=ident.item_class, uom=ident.uom, unit_labor_hours=hours,
        unit_material_cost=material, observed_on=dt.date(2026, 5, 1), description=desc,
    )


def _priced_version(version: int, *, boq_version: int, stale: bool) -> tuple[dict, list[dict]]:
    """A plans_reader_pricing row and its plans_reader_priced_lines rows, made
    the way pricing_run.store_version makes them (made-up numbers)."""
    obs = []
    for i in range(1, 13):
        obs += [_obs(f"p{i}", "DUPLEX RECEPTACLE", "EA", 0.40, 10.00), _obs(f"p{i}", '3/4"C EMT', "FT", 0.03, 1.00)]
    library = cl.CostLibrary.from_observations(obs, exclude_projects=())
    rows = [
        PriceRow(line_id="B1", description="DUPLEX RECEPTACLE", group="OTHER ITEMS", subgroup="RECEPTACLES",
                 section="NEW WORK", unit="EA", quantity=Decimal("20"), qty_base=Decimal("20"), wastage_applies=False),
        PriceRow(line_id="B2", description='3/4"C EMT', group="WIRING", subgroup="CONDUITS", section="NEW WORK",
                 unit="FT", quantity=Decimal("550"), qty_base=Decimal("500"), wastage_applies=True),
    ]
    ctx = PricingContext(wage_type="prevailing_wage", labor_rate_usd=Decimal("0"), labor_rate_source="policy",
                         allowances=(Allowance(name="ARC FLASH STUDY"),), exclude_project_ids=frozenset({OWN_PROJECT}),
                         eval_case_id="7154")
    priced = pe.compute_estimate(rows, library, pp.PricingPolicy(hour_uplift="1.0"), [], ctx)
    doc = priced.to_dict()
    pid = f"pr-{version}"
    row = {
        "id": pid, "run_id": F.RUN_ID, "version": version, "boq_version": boq_version, "status": priced.status,
        "policy_version": priced.policy_version, "library_fingerprint": priced.library_fingerprint,
        "excluded_project_ids_sha256": priced.excluded_project_ids_sha256, "context": json.loads(json.dumps(ctx.to_dict())),
        "categories": doc["categories"], "verify_draft": doc["verify"], "workbook_basis": doc["workbook_basis"],
        "range_low": doc["range_low"], "range_high": doc["range_high"], "quality_summary": doc["quality_summary"],
        "unpriced": doc["unpriced"], "blockers": doc["blockers"], "warnings": doc["warnings"], "stale": stale,
        "created_at": f"2026-10-22T10:0{version}:00+00:00",
    }
    lines = [{"pricing_id": pid, "line_id": ln.line_id, "line": ln.to_dict()} for ln in priced.lines]
    return json.loads(json.dumps(row, default=str)), json.loads(json.dumps(lines, default=str))


def _live_tables(bundle_dir: Path) -> dict:
    """The synthetic weekend-layout bundle as dev rows, plus every live-run
    table and output scope a run of the autonomy build has."""
    b = B.load_bundle(bundle_dir)
    run = dict(b.run, autonomy_state="needs_review", gate_summary={"enforced": False, "blocking": 0},
               bid_conditions={"wage_type": "prevailing_wage", "labor_time": "mixed"}, scope_policy_sha="s" * 64)
    groups = []
    for g in b.groups:
        row = dict(g)
        row["output"] = b.group_outputs.get(str(g["id"]))
        groups.append(row)
    groups.append({"id": "m1", "run_id": F.RUN_ID, "kind": "modify", "category": None, "group_index": 0, "pass": 1,
                   "status": "done", "created_at": "2026-10-22T10:01:00+00:00", "output": {"first": True}})
    groups.append({"id": "m2abcdef-x", "run_id": F.RUN_ID, "kind": "modify", "category": None, "group_index": 0, "pass": 1,
                   "status": "done", "created_at": "2026-10-22T10:02:00+00:00", "output": {"second": True}})
    outputs = [{"run_id": F.RUN_ID, "scope": "category", "category": cat, "version": 1, "output": b.category_output(cat)}
               for cat in b.categories]
    outputs.append({"run_id": F.RUN_ID, "scope": "main", "category": None, "version": 1, "output": b.main_output})
    outputs.append({"run_id": F.RUN_ID, "scope": "policy", "category": None, "version": 1,
                    "output": {"schema": "scope_context", "bid_conditions": {"wage_type": "prevailing_wage"}, "facts": []}})
    outputs.append({"run_id": F.RUN_ID, "scope": "measure", "category": None, "version": 1, "output": {"totals": {"x": 1}}})
    outputs.append({"run_id": F.RUN_ID, "scope": "measure", "category": None, "version": 2, "output": {"totals": {"x": 2}}})
    outputs.append({"run_id": F.RUN_ID, "scope": "evidence", "category": None, "version": 1, "output": {"sheets": []}})
    deliverables = [dict(d, spec_json=b.deliverables[(d["kind"], d["version"])]) for d in b.deliverable_rows]
    p1, l1 = _priced_version(1, boq_version=1, stale=True)
    p2, l2 = _priced_version(2, boq_version=1, stale=False)
    review = {"id": "rv1", "run_id": F.RUN_ID, "base_pricing_version": 1, "revised_pricing_version": 2, "mode": "apply",
              "stage": "done", "verdict": "advisory", "created_at": "2026-10-22T10:03:00+00:00"}
    findings = [{"id": f"f{i}", "review_id": "rv1", "run_id": F.RUN_ID, "finding_id": f"R{i}", "origin": "rule",
                 "kind": "k", "severity": "warning", "title": "t", "created_at": "2026-10-22T10:04:00+00:00"} for i in (2, 1)]
    gate = [{"id": "g1", "run_id": F.RUN_ID, "output_version": 1, "stage": "final", "gate_id": "census_floor",
             "severity": "warn", "route": "recount", "status": "open", "message": "census above the BOQ",
             "target": {"row_ids": ["C1"]}, "evidence": {}, "fingerprint": "fp1", "created_at": "2026-10-22T10:05:00+00:00"},
            {"id": "g2", "run_id": F.RUN_ID, "output_version": 1, "stage": "pre", "gate_id": "arithmetic",
             "severity": "block", "route": "none", "status": "resolved", "message": "fixed", "target": {}, "evidence": {},
             "fingerprint": "fp2", "created_at": "2026-10-22T10:06:00+00:00"}]
    recounts = [{"id": "rc1", "run_id": F.RUN_ID, "target_id": "T1", "row_id": "C1", "document_id": F.ELEC_DOC, "page": 1,
                 "priority": "P1", "outcome": "confirmed", "created_at": "2026-10-22T10:07:00+00:00"}]
    return {
        "plans_reader_runs": [run], "plans_reader_files": b.files, "plans_reader_documents": b.documents,
        "plans_reader_groups": groups, "plans_reader_outputs": outputs, "plans_reader_deliverables": deliverables,
        "plans_reader_calls": b.calls, "plans_reader_chat_threads": [], "plans_reader_chat_messages": [],
        "plans_reader_pricing": [p1, p2], "plans_reader_priced_lines": l1 + l2, "vendor_quote_extractions": [],
        "plans_reader_reviews": [review], "plans_reader_review_findings": findings, "plans_reader_gate_findings": gate,
        "plans_reader_recounts": recounts, "plans_reader_price_signoffs": [],
    }


@pytest.fixture
def weekend_dir(tmp_path: Path) -> Path:
    return F.make_bundle(tmp_path / "weekend", good=True)


@pytest.fixture
def exported(weekend_dir, tmp_path) -> Path:
    sb = _FakeSb(_live_tables(weekend_dir))
    return B.export_run(sb, F.RUN_ID, tmp_path / "live", case_id="7154", settings=DEV)


def test_export_writes_every_output_scope_and_the_loader_reads_them(exported):
    names = sorted(p.name for p in (exported / "outputs").glob("*.json"))
    assert "policy_none_v1.json" in names and "measure_none_v2.json" in names and "evidence_none_v1.json" in names
    b = B.load_bundle(exported, require_meta=True)
    assert b.notes == []
    assert sorted(b.outputs) == ["category:electrical_drawings", "category:specifications", "evidence", "main", "measure", "policy"]
    assert b.scope_output("measure") == {"totals": {"x": 2}} and b.output_versions["measure"] == 2
    assert b.scope_output("policy")["bid_conditions"]["wage_type"] == "prevailing_wage"
    assert b.categories == ["electrical_drawings", "specifications"]


def test_export_writes_the_live_run_members(exported):
    for name in B.ROW_MEMBERS:
        assert (exported / name).is_file(), name
    assert sorted(p.name for p in (exported / "pricing_lines").glob("*.json")) == ["v1.json", "v2.json"]
    b = B.load_bundle(exported)
    assert [r["version"] for r in b.pricing_rows] == [1, 2]
    assert len(b.pricing_version_lines(2)) == 2 and b.latest_pricing()["version"] == 2
    assert b.latest_review()["verdict"] == "advisory"
    assert [f["finding_id"] for f in b.latest_review()["findings"]] == ["R1", "R2"]
    assert len(b.gate_finding_rows) == 2 and len(b.recount_rows) == 1 and b.quote_rows == [] and b.signoff_rows == []
    assert b.run_columns["autonomy_state"] == "needs_review"
    assert b.run_columns["bid_conditions"]["wage_type"] == "prevailing_wage"
    meta = json.loads((exported / "bundle.json").read_text())
    assert meta["pricing_free"] is False and meta["unavailable_tables"] == []
    assert "pricing.json" in meta["members"] and "pricing_lines/v2.json" in meta["members"]
    assert B.verify_members(exported) == []


def test_repeated_group_names_keep_both_outputs(exported):
    b = B.load_bundle(exported)
    assert (exported / "groups" / "modify_none_0_p1.json").is_file()
    assert (exported / "groups" / "modify_none_0_p1_m2abcdef.json").is_file()
    assert b.group_outputs["m1"] == {"first": True} and b.group_outputs["m2abcdef-x"] == {"second": True}


def test_stored_artifacts_rebuild_the_priced_estimate_and_gate_report(exported):
    art = R.stored_artifacts(B.load_bundle(exported))
    priced = art["priced_estimate"]
    assert priced["schema"] == "bdr.priced_estimate/1" and priced["refused"] is False
    assert priced["wage_type"] == "prevailing_wage" and len(priced["lines"]) == 2
    assert priced["metadata"]["pricing_version"] == 2 and priced["metadata"]["boq_version"] == 1
    assert priced["metadata"]["excluded_project_ids"] == [OWN_PROJECT]
    assert [a["name"] for a in priced["allowances"]] == ["ARC FLASH STUDY"]
    assert art["pricing_version"] == 2 and len(art["pricing_versions"]) == 2
    # only the open finding is active; the resolved block is left out
    assert art["gate_report"] == {"schema": "bdr.gate_report/1", "blocking": False,
                                  "checks": [{"code": "census_floor", "severity": "warn", "detail": "census above the BOQ",
                                              "rows": art["gate_report"]["checks"][0]["rows"]}]}
    assert len(art["gate_findings"]) == 2 and art["review"]["revised_pricing_version"] == 2
    assert art["policy"]["schema"] == "scope_context" and art["measure"] == {"totals": {"x": 2}}
    assert art["evidence"] == {"sheets": []} and art["autonomy_state"] == "needs_review"
    assert art["bid_conditions"]["labor_time"] == "mixed" and len(art["recounts"]) == 1
    assert "quotes" not in art and art["quote_extractions"] == []


def test_acceptance_reads_pricing_from_the_bundle(exported):
    art = R.stored_artifacts(B.load_bundle(exported))
    inp = F.build_inputs(art)
    e1 = A.check_e1(inp)
    assert e1.measured["priced_estimate"]["present"] is True
    assert e1.measured["priced_estimate"]["pricing_version"] == 2
    assert e1.measured["priced_estimate"]["boq_version"] == 1 and e1.measured["priced_estimate"]["latest_boq_version"] == 1
    for fn in (A.check_e9, A.check_e13, A.check_gc1, A.check_gc4):
        r = fn(inp)
        first = next(iter(r.measured["parts"]))
        assert r.measured[first].get("present") is not False, (r.id, r.measured)
    gc4 = A.check_gc4(F.build_inputs(art, reference={**F.synthetic_reference(), "project_id": OWN_PROJECT}))
    assert gc4.measured["excluded_projects"]["includes_own_project"] is True
    assert gc4.measured["excluded_projects"]["sha256_matches"] is True


def test_reexport_replaces_members_and_keeps_the_directory(weekend_dir, exported):
    stale = [exported / "outputs" / "category_gone_v9.json", exported / "groups" / "sub_gone_3_p1.json",
             exported / "pricing_lines" / "v7.json", exported / "deliverables" / "boq_v9.json"]
    for p in stale:
        p.write_text("{}")
    keep = exported / "replays" / "post_run" / "replay.json"
    keep.parent.mkdir(parents=True)
    keep.write_text("{}")
    (exported / "INDEX.md").write_text("notes\n")
    tables = _live_tables(weekend_dir)
    tables["plans_reader_pricing"] = [r for r in tables["plans_reader_pricing"] if r["version"] == 2]
    out = B.export_run(_FakeSb(tables), F.RUN_ID, exported, case_id="7154", settings=DEV)
    assert out == exported
    assert not any(p.exists() for p in stale)
    assert keep.is_file() and (exported / "INDEX.md").is_file()
    b = B.load_bundle(exported, require_meta=True)
    assert [r["version"] for r in b.pricing_rows] == [2] and sorted(b.pricing_lines) == [2]
    assert b.deliverable_versions("boq") == [1]
    meta = json.loads((exported / "bundle.json").read_text())
    assert meta["reexport_replaced"] > 0 and B.verify_members(exported) == []


def test_missing_tables_are_absent_members_not_errors(weekend_dir, tmp_path):
    sb = _FakeSb(_live_tables(weekend_dir), fail=("plans_reader_pricing", "plans_reader_reviews"))
    out = B.export_run(sb, F.RUN_ID, tmp_path / "old_db", case_id="7154", settings=DEV)
    assert not (out / "pricing.json").exists() and not (out / "reviews.json").exists()
    meta = json.loads((out / "bundle.json").read_text())
    assert any(t.startswith("plans_reader_pricing:") for t in meta["unavailable_tables"])
    assert meta["pricing_free"] is True
    b = B.load_bundle(out)
    assert b.pricing_rows == [] and b.latest_pricing() is None and b.latest_review() is None
    art = R.stored_artifacts(b)
    assert "priced_estimate" not in art and "review" not in art and "gate_report" in art


def test_weekend_layout_has_none_of_the_live_artifacts(weekend_dir):
    b = B.load_bundle(weekend_dir)
    assert b.pricing_rows == [] and b.gate_finding_rows == [] and b.review_rows == [] and b.pricing_lines == {}
    assert b.members_present == {"calls.json", "chat.json", "deliverables.json"}
    art = R.stored_artifacts(b)
    for name in ("priced_estimate", "gate_report", "gate_findings", "pricing_versions", "review", "policy", "measure",
                 "evidence", "bid_conditions", "autonomy_state", "quotes", "recounts"):
        assert name not in art, name


def test_unexpected_member_shapes_are_noted_not_raised(weekend_dir):
    (weekend_dir / "pricing.json").write_text('{"not": "rows"}')
    (weekend_dir / "pricing_lines").mkdir()
    (weekend_dir / "pricing_lines" / "latest.json").write_text("[]")
    b = B.load_bundle(weekend_dir)
    assert b.pricing_rows == [] and any("pricing.json" in n for n in b.notes) and any("latest.json" in n for n in b.notes)


def test_a_broken_pricing_row_leaves_the_artifact_out_with_a_note(exported):
    b = B.load_bundle(exported)
    b.pricing_rows[-1]["verify_draft"] = {"broken": True}
    b.pricing_rows[-1]["status"] = "not-a-status"
    art = R.stored_artifacts(b)
    assert "priced_estimate" not in art and len(art["pricing_versions"]) == 2
    assert any("priced_estimate not rebuilt" in n for n in b.notes)


def test_output_key_and_names():
    assert B.output_key("category", "rfp") == "category:rfp"
    assert B.output_key("main", "none") == "main" and B.output_key("policy", None) == "policy"
    assert B.output_key("measure", "electrical_drawings") == "measure:electrical_drawings"
    assert B.output_name("policy", None, 1) == "policy_none_v1.json"
    assert B.pricing_lines_name(3) == "v3.json"
