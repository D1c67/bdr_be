"""Shared fixtures for tests/test_assistant_bid_amounts.py
(docs/ASSISTANT_PHASE4_PLAN.md 3.18).

The one sanctioned hand-over of phase 4: WI-6 created this file with the
helper's own cases; WI-13a and WI-13b each append ONE module-level list of
their own, named `<SOMETHING>_CASES`, and touch nothing else here. The test
collects every module-level list whose name ends in `_CASES`, so no shared
line is edited by two items.

A case is a BidAmountCase: a trimmer (the ToolSpec.response_trimmer
signature: payloads, args, ctx), the payloads it is fed (a real-shaped route
answer carrying amount keys) and the args. The test runs it with a
TrimContext for every role outside ACTUAL_BID_VIEWER_ROLES and asserts that
no key of BID_AMOUNT_KEYS survives at any depth of the output.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.assistant.tools._common import strip_bid_amounts


@dataclass(frozen=True)
class BidAmountCase:
    id: str
    trimmer: Callable[[list[Any], Mapping[str, Any], Any], Any]
    payloads: list[Any]
    args: Mapping[str, Any] = field(default_factory=lambda: {"response_format": "detailed", "limit": 50})


def _strip_only(payloads, args, ctx):
    """A trimmer that is only the helper: the second guard every phase 4
    trimmer runs after its own whitelist."""
    return strip_bid_amounts(payloads[0] if payloads else None, ctx.role)


# ── WI-6: the helper itself ─────────────────────────────────────────────────

HELPER_CASES: list[BidAmountCase] = [
    BidAmountCase("helper_flat", _strip_only, [{
        "project": {"id": "p1", "number": "26.10.7212"}, "bid_price": "125000.00", "total_bid_amount": "1",
        "total_amount": "2", "our_amount": "3", "our_min": "4", "winning_amount": "5", "actual_bid_at": "x",
        "status": "submitted",
    }]),
    BidAmountCase("helper_pricing_summary", _strip_only, [{
        "materials_amount": "10", "labor_amount": "20", "markup_amount": "5", "bid_price": "35",
        "material_amount": "1", "gear_amount": "2", "underground_amount": "3", "low_voltage_amount": "4",
        "labor_markup_amount": "1", "materials_markup_amount": "1", "gear_markup_amount": "1",
        "underground_markup_amount": "1", "low_voltage_markup_amount": "1",
        "markups": [{"category": "gear", "pct": "10"}],
    }]),
    BidAmountCase("helper_overview_value_trend", _strip_only, [{
        "kpis": {"win_rate": 0.31, "bids": 40},
        "value_trend": [{"bucket": "2026-10", "value": 1250000}],
        "rows": [{"project": {"number": "26.10.7212"}, "bid_price": 9, "our_min": 8}],
    }]),
    BidAmountCase("helper_pm_contract", _strip_only, [[{
        "project": {"number": "26.8.7001"}, "original_contract_value": "1", "approved_change_total": "2",
        "current_contract_value": "3", "sov_total": "4", "balance_to_finish": "5", "billed_to_date": "6",
    }]]),
    BidAmountCase("helper_recipe_extra", _strip_only, [{
        "rows": [{"extra": {"estimator_base": "100", "our_min": "120", "difference": "20",
                            "difference_pct": "20.0", "verdict": "We bid higher"}}],
    }]),
    BidAmountCase("helper_deep_nesting", _strip_only, [{
        "a": [{"b": [{"c": {"bid_price": "1", "keep": "yes"}}]}, ({"our_amount": "2"},)],
    }]),
]


# ── WI-13a: plans, PM and billing trimmers ──────────────────────────────────
# The trimmers are reached by name inside small wrappers so importing this
# file does not load the registry.


def _plans_output(payloads, args, ctx):
    from app.assistant.tools import plans

    return plans._output_trim(payloads, args, ctx)


def _plans_brief(payloads, args, ctx):
    from app.assistant.tools import plans

    return plans._brief_trim(payloads, args, ctx)


def _pm_status(payloads, args, ctx):
    from app.assistant.tools import pm

    return pm._status_trim(payloads, args, ctx)


def _billing(payloads, args, ctx):
    from app.assistant.tools import pm

    return pm._billing_trim(payloads, args, ctx)


WI13A_CASES: list[BidAmountCase] = [
    BidAmountCase("plans_output_rows", _plans_output, [{
        "version": 2, "section": "boq", "total": 1,
        "rows": [{"item": "LP-1 panel", "labor_amount": "5000", "bid_price": "9", "difference": "2",
                  "nested": [{"markups": [{"pct": "10"}], "keep": "yes"}]}],
    }]),
    BidAmountCase("plans_brief_with_a_priced_note", _plans_brief, [{
        "run": {"id": "r1", "name": "Run"}, "source": "main", "version": 1,
        "brief": {"project_summary": "Office build-out", "bid_price": "1", "risks": [{"text": "x", "total_amount": "2"}]},
        "material_notes": [{"note": "n", "materials_amount": "3"}], "confidence_notes": [],
    }]),
    BidAmountCase("pm_status_list", _pm_status, [[{
        "id": "p1", "number": "26.8.7001", "name": "ZZ PM Job", "pm_stage": "precon", "status": "won",
        "pm_details": {"customer_name": "GC", "original_contract_value": "250000.00"},
        "financials_headline": {"original_contract_value": "250000.00", "approved_change_total": "1",
                                "current_contract_value": "250001.00"},
    }]]),
    BidAmountCase("pm_status_single", _pm_status, [{
        "id": "p1", "number": "26.8.7001", "name": "ZZ PM Job", "pm_stage": "closeout",
        "pm_details": {"original_contract_value": "1"},
        "financials_headline": {"original_contract_value": "1", "approved_change_total": "2",
                                "current_contract_value": "3"},
    }, [{"from_stage": "precon", "to_stage": "closeout", "entered_at": "2026-09-03T17:00:00+00:00"}]]),
    BidAmountCase("billing_summary_rows", _billing, [{"projects": [{
        "project": {"id": "p1", "number": "26.8.7001", "name": "ZZ PM Job"},
        "original_contract_value": "1", "approved_change_total": "2", "current_contract_value": "3",
        "billed_to_date": "100.00", "retainage_held": "5.00", "balance_to_finish": "4", "open_change_order_count": 0,
        "pay_app_count": 2,
    }]}]),
]


# ── WI-13b: the report tools (run_report over the real service fixtures, ──────
# coded_report, quotes_summary, build_report) ─────────────────────────────────


def _report_tool_cases() -> list[BidAmountCase]:
    from tests.test_assistant_report_tools import bid_amount_cases

    return bid_amount_cases()


REPORT_TOOL_CASES: list[BidAmountCase] = _report_tool_cases()
