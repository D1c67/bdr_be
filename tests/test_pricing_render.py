"""app/services/pricing_render.py: the priced workbook. Synthetic BOQ,
synthetic library, code policy; the recap BASE BID must equal the Verify
draft's bid total to the cent in the WRITTEN cells (the row values sum to
it; the base bid cell carries it), with 3 and with 9 recap labels."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

from openpyxl import Workbook, load_workbook

from app.services import pricing_engine as pe
from app.services import pricing_render as pr
from app.services import pricing_run
from app.services.plans_reader_render import RECAP_SHEET, _write_recap
from app.services.pricing_policy import PricingPolicy
from app.services.pricing_types import PricingContext, QuoteRef
from tests import ai_pricing_fakes as fx

DATE = dt.date(2026, 10, 21)
POLICY = PricingPolicy(hour_uplift="1.2")


def _ctx(**kw) -> PricingContext:
    base = dict(wage_type="prevailing_wage", labor_rate_usd=Decimal("0"), labor_rate_source="policy", eval_case_id="t1")
    base.update(kw)
    return PricingContext(**base)


def _priced(spec: dict, quotes=()):
    rows = pricing_run.boq_rows(spec)
    return pe.compute_estimate(rows, fx.library(), POLICY, list(quotes), _ctx(), pricing_allowed=(True, []))


def _load(data: bytes):
    return load_workbook(io.BytesIO(data))


def _recap_sum(ws, layout) -> Decimal:
    total = Decimal(0)
    for r in range(layout["first_row"], layout["last_row"] + 1):
        for col in (4, 5, 6, 7, 9, 10):  # D E F G I J; H and K are formulas over them
            v = ws.cell(row=r, column=col).value
            total += Decimal(str(v or 0))
    return total.quantize(Decimal("0.01"))


def test_recap_base_bid_equals_bid_total_to_the_cent_with_a_quote_and_allowances():
    spec = fx.boq_spec()
    quote = QuoteRef(quote_id="q1", category="Lighting", vendor="Graybar", amount_usd=Decimal("4200"), tax_included=False)
    priced = _priced(spec, [quote])
    assert priced.verify is not None and priced.status in ("priced", "provisional")
    data, layout = pr.render_priced_estimate_xlsx(
        spec, fx.estimate_spec(), priced, date=DATE, policy=POLICY, version=1, quote_labels={"q1": "Graybar"},
    )
    wb = _load(data)
    assert wb.sheetnames == ["Sheet1", "Detailed Estimate", RECAP_SHEET, pr.VERIFY_SHEET, pr.BASIS_SHEET, pr.CHECKS_SHEET]
    recap = wb[RECAP_SHEET]
    assert _recap_sum(recap, layout) == priced.verify.bid_total
    base_cell = recap[layout["base_bid_cell"]]
    assert Decimal(str(base_cell.value)).quantize(Decimal("0.01")) == priced.verify.bid_total
    labels = list(layout["label_rows"])
    assert labels[:5] == ["DEMOLITION", "WIRING", "OTHER ITEMS", "LIGHTING FIXTURES", "LIGHTING CONTROLS"]
    assert any(lb.startswith("VENDOR QUOTE - LIGHTING (Graybar)") for lb in labels)
    assert "ALLOWANCES" in labels
    assert labels[-3].startswith(pr.LABEL_ROUNDUP) and labels[-2] == pr.LABEL_TAX_ADJ and labels[-1] == pr.LABEL_MARKUP_ADJ
    # The quote row carries the quote amount and its tax; the labor round-up
    # row carries reviewer minus basis hours at the rate.
    qrow = layout["addresses"][next(lb for lb in labels if lb.startswith("VENDOR QUOTE"))]
    assert Decimal(str(recap[qrow["material"]].value)) == Decimal("4200")
    assert Decimal(str(recap[qrow["material_tax"]].value)) == Decimal("351.75")  # 8.375% of 4200
    # Formulas over the value rows, the summary chain and the recalc check.
    totals = layout["totals_row"]
    assert recap.cell(row=totals, column=4).value == f"=SUM(D{layout['first_row']}:D{layout['last_row']})"
    assert recap.cell(row=layout["summary_rows"]["TOTAL COST WITH OVERHEADS + PROFIT"], column=5).value == f"=K{totals}"
    checks = wb[pr.CHECKS_SHEET]
    formulas = [c.value for row in checks.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=ROUND(")]
    assert len(formulas) == 1 and f"'{RECAP_SHEET}'!E{layout['base_bid_row']}" in formulas[0]


def test_estimate_sheet_holds_the_rate_the_unit_values_and_the_formulas():
    spec = fx.boq_spec()
    priced = _priced(spec)
    data, layout = pr.render_priced_estimate_xlsx(spec, fx.estimate_spec(), priced, date=DATE, policy=POLICY)
    ws = _load(data)["Detailed Estimate"]
    assert ws["A1"].value == pr.DRAFT_BANNER
    assert Decimal(str(ws["N4"].value)) == priced.verify.labor_rate
    assert ws.cell(row=7, column=pr.BASIS_COL).value == "AI PRICE BASIS"
    # Find the duplex receptacle row: H and I are values, J to O formulas.
    row = next(r for r in range(8, ws.max_row + 1) if ws.cell(row=r, column=3).value == "DUPLEX RECEPTACLE")
    line = next(ln for ln in priced.lines if ln.row.description == "DUPLEX RECEPTACLE")
    assert Decimal(str(ws.cell(row=row, column=8).value)) == line.labor.value
    assert Decimal(str(ws.cell(row=row, column=9).value)) == line.material.value
    assert ws.cell(row=row, column=10).value == f'=IF(H{row}="","",H{row}*$N$4)'
    assert ws.cell(row=row, column=15).value.startswith("=IF(")
    assert "exact" in ws.cell(row=row, column=pr.BASIS_COL).value
    # A subtotal row sums L and M over the group.
    sub = next(r for r in range(8, ws.max_row + 1) if ws.cell(row=r, column=16).value == "SUBTOTAL MATERIAL")
    assert str(ws.cell(row=sub + 1, column=16).value).startswith("=SUM(L")
    assert ws.cell(row=sub + 1, column=18).value == f"=P{sub + 1}+Q{sub + 1}"


def test_signed_banner_and_the_summary_sheet():
    spec = fx.boq_spec()
    priced = _priced(spec)
    data, _layout = pr.render_priced_estimate_xlsx(
        spec, fx.estimate_spec(), priced, date=DATE, policy=POLICY, signed_off="Executive 2026-10-21",
        basis_rows=[{"line": "B1", "basis": "counted"}], checks_rows=[("census_floor", "pass", "", "B1")],
    )
    wb = _load(data)
    assert wb["Detailed Estimate"]["A1"].value == "INTERNAL - AI PRICED - VERIFIED Executive 2026-10-21"
    verify = wb[pr.VERIFY_SHEET]
    pairs = {verify.cell(row=r, column=1).value: verify.cell(row=r, column=2).value for r in range(4, verify.max_row + 1)}
    assert Decimal(str(pairs["Bid total"])) == priced.verify.bid_total
    assert pairs["Status"] == priced.status
    basis = wb[pr.BASIS_SHEET]
    assert [basis.cell(row=4, column=c).value for c in (1, 2)] == ["line", "basis"]
    assert basis.cell(row=5, column=1).value == "B1"
    checks = wb[pr.CHECKS_SHEET]
    assert checks.cell(row=5, column=1).value == "census_floor"
    assert checks.cell(row=6, column=1).value == "recap_base_bid_equals_verify"


def test_blocked_lines_stay_blank_and_say_why():
    spec = fx.boq_spec(groups=[("WIRING", "CONDUCTORS (AWG)", [fx.item("#12 THHN/THWN Cu", 100, "EA", wastage_applies=False)])])
    priced = _priced(spec)
    assert priced.status == "blocked"
    data, layout = pr.render_priced_estimate_xlsx(spec, fx.estimate_spec(), priced, date=DATE, policy=POLICY)
    ws = _load(data)["Detailed Estimate"]
    row = next(r for r in range(8, ws.max_row + 1) if ws.cell(row=r, column=3).value == "#12 THHN/THWN Cu")
    assert ws.cell(row=row, column=8).value is None and ws.cell(row=row, column=9).value is None
    assert ws.cell(row=row, column=pr.BASIS_COL).value.startswith("BLOCKED")
    recap = _load(data)[RECAP_SHEET]
    assert _recap_sum(recap, layout) == priced.verify.bid_total


def test_write_recap_layout_addresses_3_and_9_labels():
    for n in (3, 9):
        labels = [f"GROUP {i}" for i in range(1, n + 1)]
        ws = Workbook().active
        layout = _write_recap(ws, labels)
        assert list(layout["label_rows"]) == labels
        assert layout["first_row"] == 4 and layout["last_row"] == 3 + n
        assert layout["totals_row"] == layout["last_row"] + 2
        assert ws.cell(row=layout["totals_row"], column=3).value == "TOTALS"
        assert layout["summary_title_row"] == layout["totals_row"] + 3
        assert ws.cell(row=layout["summary_rows"]["TOTAL MATERIAL COST"], column=3).value == "TOTAL MATERIAL COST"
        assert layout["base_bid_row"] == layout["summary_rows"]["BID SECURITY"] + 2
        assert ws.cell(row=layout["base_bid_row"], column=3).value == "BASE BID PRICE"
        assert layout["disclaimer_row"] == layout["base_bid_row"] + 2
    dup = _write_recap(Workbook().active, ["WIRING", "WIRING"])
    assert list(dup["label_rows"]) == ["WIRING", "WIRING#2"]


def test_priced_recap_with_three_and_nine_group_labels_closes_on_bid_total():
    three = fx.boq_spec(groups=[
        ("WIRING", "CONDUCTORS (AWG)", [fx.item("#12 THHN/THWN Cu", 300, "FT")]),
        ("OTHER ITEMS", "RECEPTACLES", [fx.item("DUPLEX RECEPTACLE", 5)]),
    ])
    nine_groups = ["WIRING", "OTHER ITEMS", "SWITCHGEAR", "LIGHTING FIXTURES", "LIGHTING CONTROLS",
                   "LOW VOLTAGE WORK", "SOLAR WORK", "LIGHTING POLE WORK", "NV ENERGY"]
    nine = fx.boq_spec(groups=[
        (g, "ITEMS", [fx.item("DUPLEX RECEPTACLE", i + 1), fx.item("ELECTRICAL CONNECTION", 1)]) for i, g in enumerate(nine_groups)
    ])
    for spec, expected_labels in ((three, 3), (nine, 10)):  # DEMOLITION joins the count
        priced = _priced(spec)
        est = fx.estimate_spec(include_standard_allowances=False)
        data, layout = pr.render_priced_estimate_xlsx(spec, est, priced, date=DATE, policy=POLICY)
        assert len(layout["labels"]) == expected_labels
        recap = _load(data)[RECAP_SHEET]
        assert _recap_sum(recap, layout) == priced.verify.bid_total
        assert Decimal(str(recap[layout["base_bid_cell"]].value)).quantize(Decimal("0.01")) == priced.verify.bid_total


def test_multi_area_recap_unions_labels_and_still_closes():
    spec = fx.boq_spec(areas=["NORTH", "SOUTH"])
    priced = _priced(spec)
    data, layout = pr.render_priced_estimate_xlsx(spec, fx.estimate_spec(), priced, date=DATE, policy=POLICY)
    wb = _load(data)
    assert wb.sheetnames[1:3] == ["NORTH", "SOUTH"]
    assert _recap_sum(wb[RECAP_SHEET], layout) == priced.verify.bid_total


def test_filename_and_version_label():
    assert pr.priced_estimate_filename("UMC Test", 3) == "UMC Test - AI PRICED ESTIMATE v3.xlsx"
    assert pr.priced_estimate_filename("", 1).startswith("Project - ")
