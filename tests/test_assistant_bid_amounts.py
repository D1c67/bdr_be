"""The bid-amount rule (app/assistant/tools/_common.py BID_AMOUNT_KEYS and
strip_bid_amounts; docs/ASSISTANT_PHASE4_PLAN.md 3.18).

Invariants pinned here:

  keys     BID_AMOUNT_KEYS is the plan's set (value_trend, the markup keys
           and the PM contract values included; a vendor quote's `amount`
           is not in it);
  helper   for every role outside ACTUAL_BID_VIEWER_ROLES (and an unknown
           or missing role) no key of the set survives at any depth, other
           keys stay, and the input is not mutated; a viewer gets the object
           back unchanged;
  cases    every list named *_CASES in tests/bid_amount_cases.py (the
           helper's own, then WI-13a's and WI-13b's trimmer fixtures) leaves
           no amount key for any non-viewer role;
  window   pt_date_range defaults, validates and caps Pacific date windows.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import pytest

import tests.bid_amount_cases as cases_module
from app.assistant.registry import TrimContext
from app.assistant.tools._common import (
    BID_AMOUNT_KEYS,
    ToolArgumentError,
    pt_date_range,
    pt_range_label,
    strip_bid_amounts,
)
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, Role

NON_VIEWERS = sorted((set(Role) - set(ACTUAL_BID_VIEWER_ROLES)), key=lambda r: r.value)
VIEWERS = sorted(ACTUAL_BID_VIEWER_ROLES, key=lambda r: r.value)


def _keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(str(key))
            found |= _keys(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            found |= _keys(inner)
    return found


def _all_cases() -> list:
    out = []
    for name in sorted(vars(cases_module)):
        value = getattr(cases_module, name)
        if name.endswith("_CASES") and isinstance(value, list):
            out.extend(value)
    return out


ALL_CASES = _all_cases()


def test_the_key_set_is_the_plans():
    assert BID_AMOUNT_KEYS == {
        "bid_price", "total_bid_amount", "total_amount", "our_amount", "our_min", "winning_amount",
        "material_amount", "gear_amount", "underground_amount", "low_voltage_amount", "labor_amount",
        "markup_amount", "materials_amount", "labor_markup_amount", "materials_markup_amount",
        "gear_markup_amount", "underground_markup_amount", "low_voltage_markup_amount", "markups",
        "value_trend", "original_contract_value", "approved_change_total", "current_contract_value",
        "sov_total", "balance_to_finish", "difference", "difference_pct", "actual_bid_at",
    }
    assert "amount" not in BID_AMOUNT_KEYS   # vendor quote amounts stay (owner question 17)
    assert ACTUAL_BID_VIEWER_ROLES == {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN, Role.ACCOUNTANT}


def _every_key_payload() -> dict:
    leaf = {k: "1" for k in BID_AMOUNT_KEYS}
    return {**leaf, "keep": "yes", "amount": "9", "rows": [{**leaf, "id": "x", "nested": [dict(leaf)]}]}


@pytest.mark.parametrize("role", [*NON_VIEWERS, None, "nobody"], ids=lambda r: getattr(r, "value", str(r)))
def test_non_viewers_lose_every_key_at_any_depth(role):
    payload = _every_key_payload()
    before = copy.deepcopy(payload)
    out = strip_bid_amounts(payload, role)
    assert not (_keys(out) & BID_AMOUNT_KEYS)
    assert out["keep"] == "yes" and out["amount"] == "9" and out["rows"][0]["id"] == "x"
    assert payload == before   # never mutated


@pytest.mark.parametrize("role", VIEWERS, ids=lambda r: r.value)
def test_viewers_keep_every_key(role):
    payload = _every_key_payload()
    assert strip_bid_amounts(payload, role) is payload
    assert strip_bid_amounts(payload, role.value) is payload


def test_scalars_and_none_pass_through():
    assert strip_bid_amounts(None, Role.ESTIMATOR) is None
    assert strip_bid_amounts("bid_price", Role.ESTIMATOR) == "bid_price"
    assert strip_bid_amounts([1, {"bid_price": 2}], Role.ESTIMATOR) == [1, {}]


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
@pytest.mark.parametrize("role", NON_VIEWERS, ids=lambda r: r.value)
def test_every_trimmer_case_strips_amounts_for_non_viewers(case, role):
    ctx = TrimContext(response_format=str(case.args.get("response_format") or "detailed"),
                      limit=int(case.args.get("limit") or 50), short=lambda v: str(v)[:8], role=role)
    out = case.trimmer(copy.deepcopy(case.payloads), dict(case.args), ctx)
    leaked = _keys(out) & BID_AMOUNT_KEYS
    assert not leaked, f"{case.id} leaks {sorted(leaked)} to {role.value}"


def test_every_case_feeds_at_least_one_amount_key():
    for case in ALL_CASES:
        assert _keys(case.payloads) & BID_AMOUNT_KEYS, f"{case.id} has no amount key to strip"
    assert len({c.id for c in ALL_CASES}) == len(ALL_CASES)


# ── Pacific date windows ─────────────────────────────────────────────────────


def test_pt_date_range_defaults_and_validates():
    today = date(2026, 10, 24)
    assert pt_date_range(today=today) == ("2026-07-26", "2026-10-24")
    assert pt_date_range("2026-10-01", "2026-10-21", today=today) == ("2026-10-01", "2026-10-21")
    assert pt_date_range(None, "2026-10-21", default_days=7) == ("2026-10-14", "2026-10-21")
    assert pt_date_range("2026-10-21", "2026-10-21") == ("2026-10-21", "2026-10-21")
    with pytest.raises(ToolArgumentError, match="on or before"):
        pt_date_range("2026-10-22", "2026-10-21")
    with pytest.raises(ToolArgumentError, match="at most 400 days"):
        pt_date_range("2025-01-01", "2026-10-21")
    with pytest.raises(ToolArgumentError, match="YYYY-MM-DD"):
        pt_date_range("10/21/2026", None)
    with pytest.raises(ToolArgumentError, match="not a real date"):
        pt_date_range("2026-02-30", None)
    assert pt_range_label("2026-10-01", "2026-10-21") == "2026-10-01 to 2026-10-21 (Pacific)"
    assert pt_range_label(None, None) == "start to today (Pacific)"
