"""app/services/pricing_tools.py: price_lookup and get_price_summary as
registered internal_pricing tools. Results never carry a project id, a
project name, a GC name or an address; the tools are hidden from
non-pricing roles and from the estimator. Synthetic library only."""

from __future__ import annotations

from app.services import plans_reader_tools as tools
from app.services import pricing_tools as pt
from app.services.pricing_policy import PricingPolicy
from tests import ai_pricing_fakes as fx

REDACT = ("Acme Tower", "Big GC Inc", "1800 W Charleston")


def _service(**kw) -> pt.LibraryPriceService:
    return pt.LibraryPriceService(fx.library(), policy=PricingPolicy(), redact=REDACT, **kw)


def test_tools_are_registered_as_internal_pricing_read_tools():
    for name, kinds in ((pt.PRICE_LOOKUP, pt.LOOKUP_KINDS), (pt.GET_PRICE_SUMMARY, pt.SUMMARY_KINDS)):
        spec = tools.get_spec(name)
        assert spec is not None and spec.internal_pricing and spec.mode == "read" and spec.cost_class == "free"
        assert spec.kinds == kinds
    offered = {s.name for s in tools.offered_tools("chat", role="executive", pricing_allowed=True)}
    assert {pt.PRICE_LOOKUP, pt.GET_PRICE_SUMMARY} <= offered
    hidden = {s.name for s in tools.offered_tools("chat", role="accountant", pricing_allowed=False)}
    assert not {pt.PRICE_LOOKUP, pt.GET_PRICE_SUMMARY} & hidden
    estimator = {s.name for s in tools.offered_tools("chat", role="estimator", pricing_allowed=True)}
    assert not {pt.PRICE_LOOKUP, pt.GET_PRICE_SUMMARY} & estimator
    assert pt.PRICE_LOOKUP in {s.name for s in tools.offered_tools("main", pricing_allowed=True)}
    assert pt.GET_PRICE_SUMMARY not in {s.name for s in tools.offered_tools("main", pricing_allowed=True)}


def test_lookup_statuses_and_aggregates():
    svc = _service()
    ok = svc.lookup({"description": "DUPLEX RECEPTACLE", "uom": "EA", "quantity": 10, "spec_facts": []})
    assert ok["status"] == "ok" and ok["matched"]["method"] == "exact"
    assert ok["labor_hours"]["median"] == 0.4 and ok["labor_hours"]["n_projects"] == 12 and ok["labor_hours"]["quality"] == "strong"
    assert ok["material"]["median"] == 10.0 and ok["material"]["latest_seen"] == "2026-05"
    assert ok["extended"] == {"hours": 4.0, "material_usd": 100.0}
    assert ok["citation"].startswith("$:receptacle")
    assert svc.lookup({"description": "#12 THHN/THWN Cu", "uom": "EA"})["status"] == "uom_conflict"
    assert svc.lookup({"description": "MOBILIZATION", "uom": "LS"})["status"] == "ls_not_priced"
    none = svc.lookup({"description": "400A ATS WITH BYPASS", "uom": "EA"})
    assert none["status"] == "no_match" and none["labor_hours"] is None
    with_fact = svc.lookup({"description": "DUPLEX RECEPTACLE", "uom": "EA", "spec_facts": [{"key": "device_grade", "value": "hospital"}]})
    assert "device_grade=hospital" in with_fact["modifiers_applied"]


def test_results_never_name_a_project_a_gc_or_an_address():
    svc = _service()
    out = svc.lookup({"description": "DUPLEX RECEPTACLE AT ACME TOWER FOR BIG GC INC", "uom": "EA"})
    text = str(out).lower()
    assert "acme tower" not in text and "big gc" not in text
    assert not {"project_id", "project_name", "gc_name", "address"} & set(out)
    scrubbed = pt.scrub({"project_name": "Acme Tower", "note": "see Acme Tower at 1800 W Charleston",
                         "nested": {"gc_name": "x", "id": "7dfc64e6-36ef-4c67-9d02-0d7942015011"}}, REDACT)
    assert scrubbed == {"note": "see [redacted] at [redacted]", "nested": {"id": "[id]"}}


def test_summary_reads_the_latest_version_through_the_loader_without_names():
    row = {"id": "p1", "version": 2, "status": "provisional", "stale": False,
           "verify_draft": {"bid_total": "1000.00", "labor_hours_basis": "10", "labor_hours_reviewer": "20", "labor_rate": "125"},
           "quality_summary": {"hours_share_by_quality": {"strong": 0.5}, "fallback_share_hours": 0.1, "provisional_reasons": ["x"]},
           "range_low": "900", "range_high": "1100", "blockers": [], "warnings": [], "unpriced": {"lines": []},
           "project_name": "Acme Tower"}
    lines = [{"line": {"line_id": "B1", "row": {"description": "DUPLEX RECEPTACLE AT ACME TOWER", "quantity": "2"}, "uom": "EA",
                       "labor_hours": "2", "material_cost": "20", "labor": {"quality": "strong"}, "flags": []}},
             {"line": {"line_id": "B2", "row": {"description": "CHEAP", "quantity": "1"}, "uom": "EA",
                       "labor_hours": "0.1", "material_cost": "1", "labor": {"quality": "weak"}, "flags": []}}]
    svc = _service(summary_loader=lambda run_id: row, lines_loader=lambda pid: lines)
    out = svc.summary("run")
    assert out["status"] == "provisional" and out["version"] == 2 and out["hours_reviewer"] == "20"
    assert out["range"] == {"low": "900", "high": "1100"}
    assert "project_name" not in out
    assert out["top_lines"][0]["line_id"] == "B1" and "[redacted]" in out["top_lines"][0]["description"]
    empty = pt.LibraryPriceService(fx.library(), summary_loader=lambda run_id: None).summary("run")
    assert empty["status"] == "none"
    assert tools.is_unavailable(pt.LibraryPriceService(fx.library()).summary("run"))


def test_handlers_dispatch_through_the_registry_with_fake_services():
    services = tools.fake_services(price=_service(summary_loader=lambda run_id: {"id": "p", "version": 1, "status": "priced"}))
    ctx = tools.ToolContext(run_id="run-1", kind="chat", services=services, role="executive")
    spec = tools.get_spec(pt.PRICE_LOOKUP)
    result = tools.dispatch(spec, ctx, {"description": "DUPLEX RECEPTACLE", "uom": "EA", "quantity": None, "family_hint": None, "spec_facts": []})
    assert not result.refused and result.data["status"] == "ok" and result.citations and result.citations[0].startswith("$:")
    bad = tools.dispatch(spec, ctx, {"description": "x"})
    assert bad.refused
    summary = tools.dispatch(tools.get_spec(pt.GET_PRICE_SUMMARY), ctx, {})
    assert not summary.refused and summary.data["status"] == "priced"
    unavailable = tools.dispatch(spec, tools.ToolContext(run_id="r", kind="chat", services=tools.fake_services(price=tools.Unavailable("price"))), {
        "description": "DUPLEX RECEPTACLE", "uom": "EA", "quantity": None, "family_hint": None, "spec_facts": []})
    assert unavailable.refused and unavailable.code == "unavailable"
