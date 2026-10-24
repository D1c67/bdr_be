"""app/services/cost_library.py: the in-memory G3 cost library. Synthetic
observations only; pure and offline (the DB loader runs against a fake)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services import cost_library as cl
from app.services.electrical_items import parse_item


def _obs(project: str, desc: str, unit: str, hours: float, material: float | None = 0.0,
         when: dt.date = dt.date(2026, 6, 1)) -> cl.Observation:
    ident = parse_item(desc, unit)
    return cl.Observation(
        project_id=project, item_key=cl.library_key(ident), family=cl.library_family(ident),
        item_class=ident.item_class, uom=ident.uom, unit_labor_hours=hours,
        unit_material_cost=material, observed_on=when, description=desc,
    )


def _lib(observations, **kw) -> cl.CostLibrary:
    return cl.CostLibrary.from_observations(observations, **kw)


def test_exact_match_median_across_projects_one_value_per_project():
    obs = [
        _obs("p1", "DUPLEX RECEPTACLE", "EA", 0.40, 10),
        _obs("p1", "DUPLEX RECEPTACLE", "EA", 0.40, 10),
        _obs("p1", "DUPLEX RECEPTACLE", "EA", 0.40, 10),  # repeats never over-weight p1
        _obs("p2", "DUPLEX RECEPTACLE", "EA", 0.50, 12),
        _obs("p3", "DUPLEX RECEPTACLE", "EA", 0.60, 14),
    ]
    lib = _lib(obs)
    got = lib.lookup("DUPLEX RECEPTACLE", "EA")
    assert got.method == "exact"
    assert got.labor.value == Decimal("0.5") and got.labor.n_projects == 3 and got.labor.n_obs == 5
    assert got.material.value == Decimal("12")
    assert got.labor.latest_seen == "2026-06"
    assert got.candidates == ()  # nothing else in the family to offer
    assert lib.n_items == 1 and lib.n_projects == 3


def test_lopo_exclusion_drops_the_project_and_changes_the_fingerprint():
    obs = [
        _obs("p1", "DUPLEX RECEPTACLE", "EA", 9.0, 99),
        _obs("p2", "DUPLEX RECEPTACLE", "EA", 0.5, 12),
        _obs("p3", "DUPLEX RECEPTACLE", "EA", 0.6, 14),
    ]
    full = _lib(obs)
    lopo = _lib(obs, exclude_projects={"p1"})
    assert lopo.n_projects == 2 and "p1" in lopo.excluded_projects
    assert lopo.lookup("DUPLEX RECEPTACLE", "EA").labor.value == Decimal("0.55")
    assert full.fingerprint != lopo.fingerprint
    assert lopo.fingerprint == _lib(obs, exclude_projects={"p1"}).fingerprint


def test_strict_uom_never_crosses_dimensions_and_ls_is_never_priced():
    obs = [_obs(f"p{i}", "#12 THHN/THWN Cu", "FT", 0.01, 0.2) for i in range(1, 6)]
    lib = _lib(obs)
    assert lib.lookup("#12 THHN/THWN Cu", "FT").method == "exact"
    ea = lib.lookup("#12 THHN/THWN Cu", "EA")
    assert ea.method == "none" and ea.labor.value is None
    assert lib.lookup("#12 THHN/THWN Cu", "LS").method == "none"
    assert lib.fallback(parse_item("#10 THHN/THWN Cu", "EA"), "EA").method == "none"
    assert lib.lookup("#12 THHN/THWN Cu", "SQ FT").method == "none"


def test_length_units_convert_per_c_to_per_ft():
    obs = [_obs(f"p{i}", "#12 THHN/THWN Cu", "C", 1.0, 21.0) for i in range(1, 4)]
    lib = _lib(obs)
    got = lib.lookup("#12 THHN/THWN Cu", "FT")
    assert got.method == "exact" and "unit_converted" in got.flags
    assert got.labor.value == Decimal("0.01") and got.material.value == Decimal("0.21")
    assert got.conversion and "per C" in got.conversion


def test_compatible_needs_critical_attrs_and_rejects_modifier_asymmetry():
    obs = [
        _obs("p1", "DUPLEX RECEPTACLE, 20A", "EA", 0.45, 15),
        _obs("p2", "DUPLEX RECEPTACLE, 20A", "EA", 0.47, 16),
        _obs("p3", "DUPLEX RECEPTACLE, 20A", "EA", 0.49, 17),
        _obs("p4", "DUPLEX RECEPTACLE, GFCI", "EA", 0.9, 30),
        _obs("p5", "DUPLEX RECEPTACLE, GFCI", "EA", 0.9, 30),
        _obs("p6", "WIRE", "FT", 0.5, 0.1),
        _obs("p7", "WIRE", "FT", 0.5, 0.1),
    ]
    lib = _lib(obs)
    plain = lib.lookup("DUPLEX RECEPTACLE", "EA")
    assert plain.method == "compatible" and plain.labor.value == Decimal("0.47")
    assert plain.labor.n_projects == 3
    # A bare conductor key (no size) never prices 500 kcmil: critical attr.
    big = lib.lookup("500 KCMIL THHN Cu", "FT")
    assert big.method != "compatible"
    assert big.labor.value is None or big.method == "fallback"


def test_context_and_fallback_need_two_projects_else_none():
    obs = [
        _obs("p1", "OCCUPANCY SENSOR SWITCH", "EA", 0.6, 30),
        _obs("p2", "OCCUPANCY SENSOR SWITCH", "EA", 0.6, 30),
        _obs("p3", "DIMMER SWITCH", "EA", 0.5, 20),
    ]
    lib = _lib(obs)
    ctx = lib.lookup("OCCUPANCY SENSOR", "EA")
    assert ctx.method == "context" and "context_match" in ctx.flags and ctx.labor.quality == "weak"
    one = _lib(obs[:1])
    assert one.lookup("OCCUPANCY SENSOR", "EA").method == "none"
    assert one.fallback(parse_item("OCCUPANCY SENSOR", "EA"), "EA").method == "none"
    fb = lib.fallback(parse_item("PHOTOCELL", "EA"), "EA")
    assert fb.method in ("fallback", "none")


def test_material_recency_weighting_favors_newer_projects_labor_does_not():
    old = dt.date(2024, 1, 1)
    new = dt.date(2026, 6, 1)
    obs = [
        _obs("p1", "DUPLEX RECEPTACLE", "EA", 0.4, 10, when=old),
        _obs("p2", "DUPLEX RECEPTACLE", "EA", 0.4, 10, when=old),
        _obs("p3", "DUPLEX RECEPTACLE", "EA", 0.8, 20, when=new),
    ]
    got = _lib(obs).lookup("DUPLEX RECEPTACLE", "EA")
    assert got.labor.value == Decimal("0.4")  # plain median across projects
    assert got.material.value == Decimal("20")  # the recent project outweighs two stale ones
    assert got.material.p25 == Decimal("10") and got.material.p75 == Decimal("20")  # unweighted


def test_outliers_are_excluded_with_four_or_more_projects():
    obs = [_obs(f"p{i}", "DUPLEX RECEPTACLE", "EA", 0.5, 10) for i in range(1, 6)]
    obs.append(_obs("p9", "DUPLEX RECEPTACLE", "EA", 50.0, 10))
    st = _lib(obs).stats(cl.library_key(parse_item("DUPLEX RECEPTACLE", "EA")), "EA")
    assert st is not None and st.labor_median == 0.5 and st.outliers_labor == ("p9",)
    assert st.labor_projects == 5


def test_demolition_material_is_zero():
    obs = [_obs(f"p{i}", "REMOVAL OF LIGHT FIXTURE", "EA", 0.5, 7.0) for i in range(1, 4)]
    got = _lib(obs).lookup("REMOVAL OF LIGHT FIXTURE", "EA")
    assert got.method == "exact" and got.labor.value == Decimal("0.5")
    assert got.material.value == Decimal("0")


def test_quality_labels_follow_the_thresholds():
    thr = cl.QualityThresholds(strong_min_projects=4, strong_max_iqr_ratio=0.25,
                               good_min_projects=2, good_max_iqr_ratio=0.5,
                               compatible_good_min_projects=3)
    tight = [_obs(f"p{i}", "DUPLEX RECEPTACLE", "EA", 0.5 + i * 0.01, 10) for i in range(1, 6)]
    assert _lib(tight, thresholds=thr).lookup("DUPLEX RECEPTACLE", "EA").labor.quality == "strong"
    wide = [_obs(f"p{i}", "DUPLEX RECEPTACLE", "EA", 0.5 * i, 10) for i in range(1, 6)]
    assert _lib(wide, thresholds=thr).lookup("DUPLEX RECEPTACLE", "EA").labor.quality == "weak"
    two = tight[:2]
    assert _lib(two, thresholds=thr).lookup("DUPLEX RECEPTACLE", "EA").labor.quality == "good"
    assert _lib(tight[:1], thresholds=thr).lookup("DUPLEX RECEPTACLE", "EA").labor.quality == "weak"
    assert cl.QualityThresholds.from_dict(thr.to_dict()) == thr


def test_library_key_drops_fixture_tags_and_keeps_demolition_subjects():
    a = cl.library_key(parse_item("L1 - 2' X 4' RECESSED TROFFER - LED - 277V", "EA"))
    b = cl.library_key(parse_item("F2 - 2' X 4' RECESSED TROFFER - LED - 120V", "EA"))
    assert a == b and a.startswith("luminaire|") and "tag=" not in a and "form=2X4" in a
    assert "mod=EM" in cl.library_key(parse_item("L1-EM - 2' X 4' RECESSED TROFFER - LED", "EA"))
    lights = cl.library_key(parse_item("REMOVAL OF LIGHTING FIXTURE AND CONTROLS W/ WIRE", "EA"))
    power = cl.library_key(parse_item("REMOVAL OF POWER DEVICES W/ BACKBOX & WIRE", "EA"))
    assert lights != power and lights.startswith("demolition:")
    attrs, mods = cl.key_attrs("receptacle:duplex|amps=20|mod=DUPLEX|mod=GFCI")
    assert attrs == {"amps": "20"} and mods == frozenset({"DUPLEX", "GFCI"})


def test_observations_from_book_skips_allowances_zero_qty_and_unknown_units():
    from app.services import estimate_parse as ep

    def line(desc, qty, unit, hours, mat, allowance=False):
        return ep.ParsedLine(sheet="S", row=1, section="NEW WORK", group="OTHER ITEMS", subgroup="",
                             description=desc, qty_base=Decimal(qty), wastage=Decimal(0),
                             quantity=Decimal(qty), unit=unit, unit_labor_hours=hours,
                             unit_material_cost=mat, is_allowance=allowance)

    parsed = ep.ParsedEstimate(sheets=["S"], labor_rate=None, workbook_date=dt.date(2026, 2, 2), lines=[
        line("DUPLEX RECEPTACLE", 4, "EA", Decimal("0.4"), None),
        line("PERMIT FEES", 0, "LS", None, None, allowance=True),
        line("SPARE", 0, "EA", Decimal("1"), Decimal("1")),
        line("THING", 2, "", Decimal("1"), Decimal("1")),
        line("NO HOURS", 2, "EA", None, Decimal("1")),
    ])
    obs, skipped = cl.observations_from_book("p1", parsed)
    assert len(obs) == 1 and obs[0].unit_material_cost == 0.0 and obs[0].observed_on == dt.date(2026, 2, 2)
    assert sorted(s["reason"] for s in skipped) == ["allowance", "no_labor_value", "unparsed_unit", "zero_qty"]


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._range = (0, len(rows) - 1)
        self.ordered_by = None

    def select(self, _cols):
        return self

    def order(self, column, **_kw):
        self.ordered_by = column
        return self

    def range(self, start, end):
        assert self.ordered_by is not None, "range() without order(): unstable paging"
        self._range = (start, end)
        return self

    def execute(self):
        start, end = self._range
        return type("R", (), {"data": self._rows[start:end + 1]})()


class _FakeSb:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def table(self, name):
        assert name == "cost_observations"
        self.calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_load_pages_through_the_table_and_skips_excluded_rows(monkeypatch):
    monkeypatch.setattr(cl, "DB_PAGE_SIZE", 2)
    rows = [
        {"project_id": f"p{i}", "item_key": "receptacle:duplex|mod=DUPLEX", "family": "receptacle:duplex",
         "item_class": "receptacle", "uom": "EA", "unit_labor_hours": 0.5, "unit_material_cost": 10,
         "observed_on": "2026-01-01", "raw_description": "DUPLEX RECEPTACLE", "group_name": "OTHER ITEMS",
         "excluded": i == 3}
        for i in range(5)
    ]
    sb = _FakeSb(rows)
    lib = cl.CostLibrary.load(sb, exclude_projects={"p0"})
    assert sb.calls == 3  # 2 + 2 + 1 rows
    assert sb.last_query.ordered_by == "id"  # a stable order before every page
    assert lib.n_projects == 3  # p0 excluded by LOPO, p3 excluded in the table
    assert lib.lookup("DUPLEX RECEPTACLE", "EA").method == "exact"


def test_load_without_table_raises_unavailable():
    class Broken:
        def table(self, name):
            raise RuntimeError("relation cost_observations does not exist")

    with pytest.raises(cl.LibraryUnavailable):
        cl.CostLibrary.load(Broken())
    with pytest.raises(cl.LibraryUnavailable):
        cl.CostLibrary.load(None)
