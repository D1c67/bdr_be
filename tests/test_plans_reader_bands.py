"""WS7 history bands (AI_ESTIMATING_BUILD_SPEC WS7, 8.7, 5.3 check_bands).

Synthetic projects only (no real pricing data); the golden test at the end
builds the bands from the production backup on disk and is skipped when the
backup is absent.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import plans_reader_bands as pb
from app.services.estimate_parse import ParsedEstimate, ParsedLine

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_history_bands.py"
BUILT = dt.datetime(2026, 10, 21, 9, 0, tzinfo=pb.COMPANY_TZ)


def _summary(pid: str, *, devices=100.0, hours=300.0, wire=4500.0, emt=1500.0, emt34=1500.0,
             bid=None, ptype="ti", line_ids=None) -> pb.EstimateSummary:
    return pb.EstimateSummary(
        project_id=pid, project_type=ptype, devices=devices, hours=hours, branch_wire_ft=wire,
        emt_ft=emt, emt34_ft=emt34, bid_usd=bid, line_ids=line_ids or {},
    )


def _history(n=30, *, ptype="other", hours_fn=None, wire_fn=None):
    """n building projects with 100 devices; hours per device 2.0 + 0.1 i,
    branch wire per device 30 + i, EMT per device 10 + 0.5 i."""
    out = []
    for i in range(n):
        h = hours_fn(i) if hours_fn else (2.0 + 0.1 * i) * 100
        w = wire_fn(i) if wire_fn else (30.0 + i) * 100
        out.append(_summary(f"p{i:02d}", hours=h, wire=w, emt=(10 + 0.5 * i) * 100,
                            emt34=w / 3.0, bid=(500 + 10 * i) * 100, ptype=ptype))
    return out


# Classifier and summaries ---------------------------------------------------------------


@pytest.mark.parametrize(
    "row, expected",
    [
        (("NEW WORK", "LIGHTING FIXTURES", "", "L1 TROFFER", "EA"), "device"),
        (("NEW WORK", "LIGHTING CONTROLS", "", "OCCUPANCY SENSOR", "EA"), "device"),
        (("NEW WORK", "OTHER ITEMS", "RECEPTACLES (RECEPTACLES INCLUDES 4\"S DEEP COMB BOX AND COVER PLATE)",
          "DUPLEX RECEPTACLE", "EA"), "device"),
        (("NEW WORK", "OTHER ITEMS", "MISCELLANEOUS", "ELECTRICAL CONNECTION", "EA"), "device"),
        (("NEW WORK", "OTHER ITEMS", "POLE W/ ACCESSORIES", "POLE", "EA"), "other"),
        (("NEW WORK", "LOW VOLTAGE WORK", "", "DATA OUTLET", "EA"), "device"),
        (("NEW WORK", "SWITCHGEAR", "CIRCUIT BREAKERS", "20A/1P", "EA"), "other"),
        (("DEMOLITION", "LIGHTING FIXTURES", "", "REMOVE FIXTURE", "EA"), "other"),
        (("NEW WORK", "LIGHTING FIXTURES", "", "L5 UNDERCABINET", "FT"), "other"),
        (("NEW WORK", "WIRING", "CONDUCTORS (AWG)", "#12 THHN/THWN Cu", "FT"), "branch_wire"),
        (("NEW WORK", "WIRING", "CONDUCTORS (AWG)", "# 10 THHN", "FT"), "branch_wire"),
        (("NEW WORK", "WIRING", "CONDUCTORS (AWG)", "#8 THHN", "FT"), "other"),
        (("NEW WORK", "WIRING", "CONDUITS", "3/4\"C EMT", "FT"), "emt34"),
        (("NEW WORK", "WIRING", "CONDUITS", "1\" EMT", "FT"), "emt_other"),
        (("NEW WORK", "WIRING", "CONDUITS", "3/4\" PVC", "FT"), "other"),
    ],
)
def test_classify_row(row, expected):
    assert pb.classify_row(*row) == expected


def test_metric_values_apply_rules():
    vals = _summary("a", devices=50, hours=150, wire=2500, emt=900, emt34=600, bid=40000).metric_values()
    assert vals["hours_per_device"] == pytest.approx(3.0)
    assert vals["branch_wire_ft_per_device"] == pytest.approx(50.0)
    assert vals["emt_ft_per_device"] == pytest.approx(18.0)
    assert vals["wire_per_emt34"] == pytest.approx(2500 / 600)
    assert vals["bid_per_device"] == pytest.approx(800.0)
    few = _summary("b", devices=19, wire=0, emt34=100).metric_values()
    assert all(few[m] is None for m in ("hours_per_device", "branch_wire_ft_per_device",
                                        "emt_ft_per_device", "bid_per_device"))
    assert few["wire_per_emt34"] is None  # needs both wire and 3/4 EMT
    zero = _summary("c", devices=40, wire=0).metric_values()
    assert zero["branch_wire_ft_per_device"] == 0.0


def _line(row, group, subgroup, desc, unit, qty, hours="0.5", mat="10", section="NEW WORK", wastage="0"):
    q = Decimal(qty)
    w = Decimal(wastage)
    return ParsedLine(sheet="E", row=row, section=section, group=group, subgroup=subgroup, description=desc,
                      qty_base=q, wastage=w, quantity=q * (1 + w), unit=unit,
                      unit_labor_hours=Decimal(hours) if hours is not None else None,
                      unit_material_cost=Decimal(mat))


def _parsed(lines, *, labor_cost="5000", hours="200", base_bid="25000"):
    return ParsedEstimate(
        sheets=["ESTIMATE"], labor_rate=Decimal("100"), workbook_date=None, lines=lines,
        summary={"total_labor_hours": Decimal(hours), "total_labor_cost": Decimal(labor_cost)},
        base_bid=Decimal(base_bid) if base_bid else None,
    )


def test_summary_from_parsed_uses_pre_waste_quantities():
    p = _parsed([
        _line(10, "LIGHTING FIXTURES", "", "L1", "EA", "30"),
        _line(11, "OTHER ITEMS", "RECEPTACLES", "DUPLEX", "EA", "20"),
        _line(12, "WIRING", "CONDUCTORS (AWG)", "#12 THHN", "FT", "1000", wastage="0.1"),
        _line(13, "WIRING", "CONDUITS", "3/4\"C EMT", "FT", "400", wastage="0.1"),
        _line(14, "DEMOLITION", "", "REMOVE", "EA", "9", section="DEMOLITION"),
    ])
    s = pb.summary_from_parsed(p, project_id="x", project_type="ti")
    assert s.devices == 50
    assert s.branch_wire_ft == 1000  # column D, not the wastage-inclusive F
    assert s.emt34_ft == 400 and s.emt_ft == 400
    assert s.hours == 200 and s.bid_usd == 25000
    assert s.line_ids["branch_wire"] == ("E!12",)


def test_summary_from_priced_counts_hours_and_skips_excluded_lines():
    lines = [
        {"line_id": "B1", "section": "NEW WORK", "group": "LIGHTING FIXTURES", "subgroup": "",
         "description": "L1", "unit": "EA", "qty_base": 25, "wastage": 0, "qty": 25, "unit_hours": 1.0,
         "excluded": False},
        {"line_id": "B2", "section": "NEW WORK", "group": "WIRING", "subgroup": "CONDUCTORS (AWG)",
         "description": "#12 THHN", "unit": "FT", "qty_base": None, "wastage": 0.1, "qty": 1100,
         "unit_hours": 0.01, "excluded": False},
        {"line_id": "B3", "section": "NEW WORK", "group": "LIGHTING FIXTURES", "subgroup": "",
         "description": "L9", "unit": "EA", "qty_base": 50, "wastage": 0, "qty": 50, "unit_hours": 1.0,
         "excluded": True},
    ]
    s = pb.summary_from_priced({"lines": lines}, project_id="r", project_type="other")
    assert s.devices == 25
    assert s.branch_wire_ft == pytest.approx(1000.0)
    assert s.hours == pytest.approx(25 + 11)
    assert s.bid_usd is None
    assert s.metric_values()["bid_per_device"] is None


def test_book_exclusion_filter():
    good = [_line(10 + i, "LIGHTING FIXTURES", "", f"L{i}", "EA", "10") for i in range(5)]
    assert pb.book_exclusion(_parsed(good)) is None
    assert pb.book_exclusion(_parsed(good), filename="X - BUDGET ESTIMATE.xlsx") == "budget_rom"
    assert pb.book_exclusion(_parsed(good), filename="ROM pricing.xlsx") == "budget_rom"
    assert pb.book_exclusion(_parsed(good[:2])) == "budget_rom"
    lump = good[:3] + [_line(30 + i, "OTHER ITEMS", "", "LUMP", "EA", "1", hours=None) for i in range(4)]
    assert pb.book_exclusion(_parsed(lump)) == "budget_rom"
    no_mat = [_line(10 + i, "LIGHTING FIXTURES", "", f"L{i}", "EA", "10", mat="0") for i in range(5)]
    assert pb.book_exclusion(_parsed(no_mat)) == "labor_only"


# Percentiles and build ------------------------------------------------------------------


def test_percentile_linear_interpolation():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert pb.percentile(vals, 0.5) == 3.0
    assert pb.percentile(vals, 0.1) == pytest.approx(1.4)
    assert pb.percentile(vals, 0.9) == pytest.approx(4.6)
    assert pb.percentile([7.0], 0.9) == 7.0
    with pytest.raises(ValueError):
        pb.percentile([], 0.5)


def test_build_bands_quantiles_strata_and_exclusions():
    hist = _history(30) + [_summary("hold", hours=99999, ptype="other"),
                           _summary("rom", hours=88888, ptype="other"),
                           _summary("road", ptype="roadway")]
    bands = pb.build_bands(hist, excluded_project_ids=["hold"], book_exclusions={"rom": "budget_rom"},
                           built_at=BUILT)
    b = bands.band("hours_per_device", "building")
    assert b.n == 30
    expected = sorted(2.0 + 0.1 * i for i in range(30))
    q = b.quantiles()
    assert q["p10"] == pytest.approx(pb.percentile(expected, 0.10))
    assert q["p50"] == pytest.approx(pb.percentile(expected, 0.50))
    assert q["p90"] == pytest.approx(pb.percentile(expected, 0.90))
    ids = {p for p, _v in b.values}
    assert "hold" not in ids and "rom" not in ids and "road" not in ids
    assert bands.band("hours_per_device", "all").n == 31  # roadway joins `all` only
    listed = {p["project_id"]: p for p in bands.projects}
    assert "hold" not in listed
    assert listed["rom"]["excluded_reason"] == "budget_rom"
    assert bands.excluded_project_ids == ("hold",)
    assert re.fullmatch(r"hb1-20261021-[0-9a-f]{8}", bands.band_version)


def test_building_stratum_types():
    for t in ("ti", "other", "new_construction", "multi_family", "casino_strip", "casino_other"):
        assert pb.is_building_type(t)
        assert "building" in pb.strata_for_type(t)
    for t in ("roadway", "lighting", "unknown", None):
        assert not pb.is_building_type(t)


# Severity ------------------------------------------------------------------------------------


def _by_metric(checks):
    return {c.metric: c for c in checks}


def test_inside_band_is_none_and_outside_is_advisory():
    bands = pb.build_bands(_history(30), built_at=BUILT)
    q = bands.band("branch_wire_ft_per_device", "building").quantiles()
    inside = _summary("run", wire=q["p50"] * 100, ptype="ti")
    c = _by_metric(pb.check_bands(inside, bands))["branch_wire_ft_per_device"]
    assert c.severity == "none" and c.position in ("in_band", "low", "high")
    above = _summary("run", wire=q["p90"] * 1.2 * 100, ptype="ti")
    c = _by_metric(pb.check_bands(above, bands))["branch_wire_ft_per_device"]
    assert c.position == "above" and c.severity == "warning" and c.advisory


def test_component_extreme_stays_advisory():
    bands = pb.build_bands(_history(30), built_at=BUILT)
    q = bands.band("branch_wire_ft_per_device", "building").quantiles()
    far = _summary("run", wire=q["p90"] * 3 * 100, ptype="other")
    c = _by_metric(pb.check_bands(far, bands))["branch_wire_ft_per_device"]
    assert c.position == "above_extreme"
    assert c.severity == "warning"


def test_structural_zero_is_critical_when_supported():
    bands = pb.build_bands(_history(30), built_at=BUILT)
    assert bands.band("branch_wire_ft_per_device", "building").lopo.critical_supported
    run = _summary("run", wire=0, ptype="other", line_ids={"branch_wire": ()})
    c = _by_metric(pb.check_bands(run, bands))["branch_wire_ft_per_device"]
    assert c.position == "below_extreme" and c.severity == "critical"
    assert pb.worst_severity(pb.check_bands(run, bands)) == "critical"


def test_structural_zero_not_supported_when_history_has_zeros():
    # 3 of 30 clean books carry zero branch wire: rate 0.10 > 0.05.
    hist = _history(30, wire_fn=lambda i: 0.0 if i < 3 else (30.0 + i) * 100)
    bands = pb.build_bands(hist, built_at=BUILT)
    band = bands.band("branch_wire_ft_per_device", "building")
    assert len(band.zeros) == 3 and band.n == 27
    assert not band.lopo.critical_supported
    c = _by_metric(pb.check_bands(_summary("run", wire=0, ptype="other"), bands))["branch_wire_ft_per_device"]
    assert c.position == "below_extreme" and c.severity == "warning"


def test_total_level_extreme_critical_only_when_lopo_supports_it():
    clean = pb.build_bands(_history(30), built_at=BUILT)
    lopo = clean.band("hours_per_device", "building").lopo
    assert lopo.critical_scope == "extreme" and lopo.n_extreme == 0 and lopo.critical_supported
    q = clean.band("hours_per_device", "building").quantiles()
    run = _summary("run", hours=q["p90"] * 3 * 100, ptype="other")
    assert _by_metric(pb.check_bands(run, clean))["hours_per_device"].severity == "critical"
    # Three clean books far above the rest: LOPO extreme rate 0.10 > 0.05.
    noisy = pb.build_bands(
        _history(30, hours_fn=lambda i: 5000.0 if i < 3 else (2.0 + 0.1 * i) * 100), built_at=BUILT
    )
    nlopo = noisy.band("hours_per_device", "building").lopo
    assert nlopo.n_extreme == 3 and not nlopo.critical_supported
    c = _by_metric(pb.check_bands(_summary("run", hours=100000, ptype="other"), noisy))["hours_per_device"]
    assert c.position == "above_extreme" and c.severity == "warning"


def test_critical_needs_twenty_values():
    bands = pb.build_bands(_history(15), built_at=BUILT)
    band = bands.band("branch_wire_ft_per_device", "building")
    assert band.n == 15 and not band.lopo.critical_supported
    c = _by_metric(pb.check_bands(_summary("run", wire=0, ptype="other"), bands))["branch_wire_ft_per_device"]
    assert c.severity == "warning"


def test_wire_per_emt34_never_critical():
    bands = pb.build_bands(_history(30), built_at=BUILT)
    assert bands.band("wire_per_emt34", "building").lopo.critical_scope == "none"
    run = _summary("run", wire=100000, emt34=1000, ptype="other")
    c = _by_metric(pb.check_bands(run, bands))["wire_per_emt34"]
    assert c.position == "above_extreme" and c.severity == "warning"


def test_not_applicable_under_twenty_devices_and_thin_band():
    bands = pb.build_bands(_history(30), built_at=BUILT)
    small = _by_metric(pb.check_bands(_summary("run", devices=10, ptype="other"), bands))
    assert small["hours_per_device"].position == "not_applicable"
    assert small["hours_per_device"].severity == "none"
    thin = pb.build_bands(_history(5), built_at=BUILT)
    c = _by_metric(pb.check_bands(_summary("run", ptype="other"), thin))["hours_per_device"]
    assert c.position == "not_applicable" and "band n" in c.reason


# LOPO ----------------------------------------------------------------------------------------


def test_check_bands_leaves_the_runs_own_project_out():
    # The run's own book sits in the history with a huge wire value: with it
    # in the band, p90 would rise; with LOPO it does not.
    hist = _history(30) + [_summary("own", wire=500 * 100, ptype="other")]
    bands = pb.build_bands(hist, built_at=BUILT)
    full = bands.band("branch_wire_ft_per_device", "building")
    assert full.n == 31
    own = _summary("own", wire=500 * 100, ptype="other")
    c = _by_metric(pb.check_bands(own, bands))["branch_wire_ft_per_device"]
    assert c.n == 30
    lopo_p90 = full.without(["own"]).quantiles()["p90"]
    assert c.p90 == pytest.approx(lopo_p90)
    assert c.p90 < full.quantiles()["p90"]
    assert c.position == "above_extreme"
    # A run without a project id sees the full band; extra exclusions apply.
    anon = pb.EstimateSummary(None, "other", 100, 300, 50000, 1500, 1500)
    assert _by_metric(pb.check_bands(anon, bands))["branch_wire_ft_per_device"].n == 31
    extra = _by_metric(pb.check_bands(anon, bands, exclude_project_ids=["own", "p00"]))
    assert extra["branch_wire_ft_per_device"].n == 29


def test_lopo_baseline_places_each_book_against_the_others():
    hist = _history(30, hours_fn=lambda i: 9000.0 if i == 0 else (2.0 + 0.1 * i) * 100)
    band = pb.build_bands(hist, built_at=BUILT).band("hours_per_device", "building")
    lopo = band.lopo
    assert lopo.n_evaluated == 30
    assert lopo.n_extreme == 1  # only the planted outlier
    assert lopo.extreme_rate == pytest.approx(1 / 30)
    assert lopo.critical_supported


# Files and contract ----------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    bands = pb.build_bands(_history(25), excluded_project_ids=["zz"], built_at=BUILT)
    path = pb.save_bands(bands, tmp_path)
    assert path == tmp_path / "bands" / f"{bands.band_version}.json"
    again = pb.load_bands(bands.band_version, tmp_path)
    assert again.band_version == bands.band_version
    assert again.excluded_project_ids == ("zz",)
    for key, band in bands.bands.items():
        other = again.band(*key)
        assert other.n == band.n and other.zeros == band.zeros
        assert other.lopo == pb.LopoBaseline.from_dict(band.lopo.to_dict())
        assert other.quantiles() == pytest.approx(band.quantiles())
    assert pb.load_bands(path).band_version == bands.band_version
    assert pb.latest_bands_path(tmp_path) == path
    run = _summary("run", wire=0, ptype="other")
    assert [c.to_dict() for c in pb.check_bands(run, again)] == [c.to_dict() for c in pb.check_bands(run, bands)]


def test_band_version_is_deterministic():
    a = pb.build_bands(_history(25), built_at=BUILT)
    b = pb.build_bands(_history(25), built_at=BUILT)
    c = pb.build_bands(_history(26), built_at=BUILT)
    assert a.band_version == b.band_version != c.band_version


def test_money_fields_stripped_without_pricing_view():
    bands = pb.build_bands(_history(30), built_at=BUILT)
    checks = _by_metric(pb.check_bands(_summary("run", bid=60000, ptype="other"), bands))
    bid = checks["bid_per_device"]
    assert bid.value is not None
    hidden = bid.to_dict(include_money=False)
    assert all(hidden[k] is None for k in ("value", "p10", "p50", "p90"))
    assert checks["hours_per_device"].to_dict(include_money=False)["value"] is not None


def test_persist_bands_writes_rows_after_mf():
    # Wave 2: MF landed; the fake-client upsert tests live in
    # tests/test_plans_reader_bands_persist.py.
    rows = pb.band_rows(pb.build_bands(_history(10), built_at=BUILT))
    assert rows and all(r["manifest_sha256"] for r in rows)


def test_report_lists_n_and_lopo_baseline():
    md = pb.report_markdown(pb.build_bands(_history(30), book_exclusions={"p01": "labor_only"}, built_at=BUILT))
    assert "## LOPO baseline (building stratum)" in md
    assert "| hours_per_device | building | 29 |" in md
    assert "p01" in md and "labor_only" in md


def _load_script():
    if "build_history_bands" in sys.modules:
        return sys.modules["build_history_bands"]
    spec = importlib.util.spec_from_file_location("build_history_bands", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_history_bands"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_script_exclusions_use_manifest_and_fall_back(monkeypatch):
    script = _load_script()
    ids = script.excluded_ids(None)
    assert set(script.SPEC_HOLDOUT_IDS) <= ids
    monkeypatch.setitem(sys.modules, "app.services.eval_manifest", None)
    import app.services as services_pkg

    monkeypatch.delattr(services_pkg, "eval_manifest", raising=False)
    fallback = script.excluded_ids("7dfc64e6-36ef-4c67-9d02-0d7942015011")
    assert fallback == frozenset(script.SPEC_HOLDOUT_IDS) | {"7dfc64e6-36ef-4c67-9d02-0d7942015011"}


# Golden ----------------------------------------------------------------------------------------


@pytest.mark.golden
@pytest.mark.skipif(not (BACKUP / "data" / "project_files.json").is_file(), reason="backup not present")
def test_golden_bands_from_backup():
    script = _load_script()
    bands, _j, _m = script.build(BACKUP, write=False)
    holdout = set(script.SPEC_HOLDOUT_IDS)
    for metric in ("hours_per_device", "branch_wire_ft_per_device", "emt_ft_per_device", "wire_per_emt34"):
        band = bands.band(metric, "building")
        assert band is not None and band.n >= 20, metric
        assert not holdout & {p for p, _v in band.values}
    summaries, _excl, _names = pb.summaries_from_backup(BACKUP)
    own = next(s for s in summaries if str(s.project_id).startswith("7dfc64e6"))
    checks = _by_metric(pb.check_bands(own, bands))
    wire = checks["branch_wire_ft_per_device"]
    assert wire.stratum == "building" and wire.n >= 20
    assert wire.value > wire.p90  # 7154 truth sits above p90 (spec WS7)
    assert wire.position in ("above", "above_extreme")
    assert wire.severity == "warning"  # never critical on the correct estimate
    assert wire.n == bands.band("branch_wire_ft_per_device", "building").n - 1  # LOPO
