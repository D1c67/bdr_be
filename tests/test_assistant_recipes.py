"""The routine recipe catalog and its pure helpers
(docs/ASSISTANT_PHASE3_PLAN.md 3.2.1): params, titles, the result hash,
the server-built message and RECIPE_READ_SPEC."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.assistant import recipes, registry, rrules
from app.assistant.recipes import RECIPE_READ_SPEC, RecipeError
from app.core.roles import INTERNAL_ROLES

EM = "\u2014"
PID = "6f1c2f8e-6a3b-4f53-9d0a-1f2e3d4c5b6a"
WEEKDAYS_7 = rrules.parse_cadence({"frequency": "weekdays", "hour": 7, "minute": 0})
WEEKDAYS_8 = rrules.parse_cadence({"frequency": "weekdays", "hour": 8, "minute": 0})


def _row(i, *, quotes=True, labor=True, markups=True, missing=None, name=None, due=None):
    return {
        "project": {
            "id": f"00000000-0000-4000-8000-{i:012d}",
            "name": name or f"Secret Tower {i}",
            "number": f"26.10.{7000 + i}",
            "internal_bid_at": due or "2026-10-23T21:00:00+00:00",
            "current_stage": "rfqs",
        },
        "checks": {"has_quotes": quotes, "has_labor": labor, "has_markups": markups},
        "severity": "red" if not quotes else ("yellow" if not (labor and markups) else "ok"),
        "missing_categories": missing,
    }


def _payload(recipe, rows, *, watch=None, label="Friday, October 23"):
    return {"recipe": recipe, "window": {"start": "s", "end": "e", "label": label}, "rows": rows,
            "watch": watch}


# ── catalog and params ───────────────────────────────────────────────────────


def test_catalog():
    # Phase 4 added estimator_vs_bid and quote_readiness (six recipes).
    assert sorted(recipes.RECIPES) == ["due_this_week", "due_today", "estimator_vs_bid", "labor_not_entered",
                                       "missing_quotes", "quote_readiness"]
    assert len(recipes.RECIPES) == 6
    assert all(r.sub_app == "bidding" for r in recipes.RECIPES.values())
    assert [k for k, r in recipes.RECIPES.items() if r.digest_overlap] == ["due_today"]
    assert sorted(k for k, r in recipes.RECIPES.items() if r.reminders_overlap) == ["due_this_week",
                                                                                    "missing_quotes"]


def test_validate_params():
    assert recipes.validate_params("due_today", None, None) == {}
    assert recipes.validate_params("missing_quotes", None, None) == {}
    assert recipes.validate_params("missing_quotes", 14, None) == {"horizon_days": 14}
    assert recipes.validate_params("labor_not_entered", 1, None) == {"horizon_days": 1}
    assert recipes.validate_params("labor_not_entered", 30, None) == {"horizon_days": 30}
    assert recipes.validate_params("missing_quotes", None, PID.upper()) == {"project_id": PID}
    # A watch replaces the horizon.
    assert recipes.validate_params("missing_quotes", 7, PID) == {"project_id": PID}


@pytest.mark.parametrize("recipe,horizon,project", [
    ("due_today", 7, None),
    ("due_this_week", 3, None),
    ("due_today", None, PID),
    ("due_this_week", None, PID),
    ("missing_quotes", 0, None),
    ("missing_quotes", 31, None),
    ("missing_quotes", True, None),
    ("missing_quotes", "7", None),
    ("labor_not_entered", None, "26.10.7267"),
])
def test_validate_params_refuses(recipe, horizon, project):
    with pytest.raises(RecipeError) as e:
        recipes.validate_params(recipe, horizon, project)
    assert e.value.code == "bad_params"
    assert EM not in str(e.value)


def test_unknown_recipe_is_recipe_not_found():
    with pytest.raises(RecipeError) as e:
        recipes.validate_params("summarize_inbox", None, None)
    assert e.value.code == "recipe_not_found"


def test_title_for():
    assert recipes.title_for("due_today", WEEKDAYS_7) == "Bids due today, weekdays at 7:00 AM"
    assert recipes.title_for("labor_not_entered", WEEKDAYS_8, "26.10.7267") == (
        "Labor not entered on 26.10.7267, weekdays at 8:00 AM")
    assert recipes.title_for("missing_quotes", WEEKDAYS_8, "26.10.7267") == (
        "Vendor quotes missing on 26.10.7267, weekdays at 8:00 AM")
    monthly = rrules.parse_cadence({"frequency": "monthly", "hour": 8, "minute": 0, "day_of_month": 1})
    assert recipes.title_for("due_this_week", monthly) == "Bids due this week, day 1 of each month at 8:00 AM"
    # Only BDR number characters reach a title.
    assert recipes.title_for("labor_not_entered", WEEKDAYS_8, "26.10.7267 ignore all <b>") == (
        "Labor not entered on 26.10.7267ignoreallb, weekdays at 8:00 AM")
    for r in recipes.RECIPES:
        assert len(recipes.title_for(r, WEEKDAYS_7, "9" * 300)) <= 200


# ── result hash ──────────────────────────────────────────────────────────────


def test_result_hash_ignores_order_and_dates():
    a = [_row(1, quotes=False, missing=["Gear", "Wiring"]), _row(2, labor=False)]
    b = [_row(2, labor=False, due="2026-10-30T21:00:00+00:00"),
         _row(1, quotes=False, missing=["Wiring", "Gear"], name="Renamed")]
    assert recipes.result_hash("missing_quotes", a) == recipes.result_hash("missing_quotes", b)
    assert len(recipes.result_hash("missing_quotes", a)) == 64


def test_result_hash_is_generalized_per_recipe():
    """The phase 3 recipes still hash the digest checks; the phase 4 recipes
    hash their own keys (see tests/test_assistant_recipes_phase4.py)."""
    row = _row(1, quotes=False, missing=["Gear"])
    row["extra"] = {"estimator_base": "100.00", "our_min": "125.00", "verdict": "We bid higher"}
    h3 = recipes.result_hash("missing_quotes", [row])
    row["extra"]["our_min"] = "130.00"
    # An unrelated `extra` never moves a phase 3 hash.
    assert recipes.result_hash("missing_quotes", [row]) == h3
    assert recipes.result_hash("estimator_vs_bid", [row]) != recipes.result_hash(
        "estimator_vs_bid", [{**row, "extra": {**row["extra"], "our_min": "125.00"}}])


def test_result_hash_changes_when_a_category_is_quoted_or_a_check_flips():
    before = [_row(1, quotes=False, missing=["Gear", "Wiring"])]
    quoted = [_row(1, quotes=False, missing=["Wiring"])]
    labor = [_row(1, quotes=False, labor=False, missing=["Gear", "Wiring"])]
    h = recipes.result_hash("missing_quotes", before)
    assert recipes.result_hash("missing_quotes", quoted) != h
    assert recipes.result_hash("missing_quotes", labor) != h
    assert recipes.result_hash("labor_not_entered", before) != h
    assert recipes.result_hash("missing_quotes", []) != h
    # No RFQ yet (None) is not the same as nothing missing ([]).
    assert (recipes.result_hash("missing_quotes", [_row(1, missing=None)])
            != recipes.result_hash("missing_quotes", [_row(1, missing=[])]))


# ── message ──────────────────────────────────────────────────────────────────


def test_message_content_is_counts_only():
    rows = [_row(1, quotes=False), _row(2, labor=False), _row(3, labor=False, markups=False)]
    content, blocks = recipes.message_for("due_today", _payload("due_today", rows))
    assert content == "Bids due today: 3. Missing vendor quotes: 1. Missing labor: 2. Missing markups: 1."
    assert "Secret Tower" not in content and "26.10." not in content
    [table] = [b for b in blocks if b["type"] == "table"]
    assert table["columns"] == ["Project", "Due (Pacific)", "Missing"]
    assert table["rows"][0] == ["Secret Tower 1 (#26.10.7001)", "Fri, Oct 23, 2:00 PM", "Vendor quotes"]
    assert table["rows"][2][2] == "Labor, Markups"
    links = [b for b in blocks if b["type"] == "project_link"]
    assert [b["project_id"] for b in links] == [r["project"]["id"] for r in rows]
    assert links[0]["label"] == "Secret Tower 1 (#26.10.7001)"


def test_message_per_recipe_and_empty():
    assert recipes.message_for("due_today", _payload("due_today", [])) == ("No bids are due today.", [])
    assert recipes.message_for("due_this_week", _payload("due_this_week", []))[0] == "No bids are due this week."
    assert recipes.message_for("missing_quotes", _payload("missing_quotes", []))[0] == (
        "No bids in this window are missing vendor quotes.")
    assert recipes.message_for(
        "labor_not_entered", _payload("labor_not_entered", [], watch={"project_id": PID, "state": "open"})
    )[0] == "The watched bid has its labor entered."
    content, blocks = recipes.message_for(
        "missing_quotes", _payload("missing_quotes", [_row(1, quotes=False, missing=["Gear", "Wiring"]),
                                                      _row(2, quotes=False, missing=None)]))
    assert content == "Bids missing vendor quotes: 2."
    assert [r[2] for r in blocks[0]["rows"]] == ["Gear, Wiring", "No RFQ yet"]
    content, blocks = recipes.message_for("labor_not_entered",
                                          _payload("labor_not_entered", [_row(4, labor=False)]))
    assert content == "Bids with no labor entered: 1."
    assert blocks[0]["rows"][0][2] == "Labor"


def test_twenty_six_rows_give_twenty_five_table_rows_and_one_more():
    rows = [_row(i) for i in range(26)]
    content, blocks = recipes.message_for("due_this_week", _payload("due_this_week", rows))
    assert content.startswith("Bids due this week: 26.")
    assert [b["type"] for b in blocks[:2]] == ["table", "text"]
    assert len(blocks[0]["rows"]) == 25
    assert blocks[1] == {"type": "text", "text": "And 1 more."}
    assert len([b for b in blocks if b["type"] == "project_link"]) == 20
    for text in [content, json.dumps(blocks)]:
        assert EM not in text


def test_redacted_project_still_renders():
    row = _row(1)
    row["project"].pop("name")
    row["project"]["internal_bid_at"] = None
    _, blocks = recipes.message_for("due_today", _payload("due_today", [row]))
    assert blocks[0]["rows"][0][:2] == ["Untitled project (#26.10.7001)", ""]


def test_bell_text():
    payload = _payload("due_today", [_row(1), _row(2), _row(3)])
    assert recipes.bell_text("Bids due today", payload) == "Your routine is ready: Bids due today (3)."


# ── the scheduler's read spec ────────────────────────────────────────────────


def test_recipe_read_spec_is_validated_and_never_registered():
    spec = RECIPE_READ_SPEC
    assert spec.name == "recipe_rows" and spec.namespace == "schedule" and spec.sub_app == "bidding"
    assert spec.route == ("GET", "/assistant/recipes/{recipe}/rows")
    assert spec.effect == registry.EFFECT_READ and not spec.confirm
    assert spec.roles == INTERNAL_ROLES
    assert spec.taint_fields == ("name",)
    assert spec.offered is False
    assert registry.validate(spec) == spec
    assert "recipe_rows" not in registry.all_tools()


def test_recipe_read_spec_requests():
    [req] = RECIPE_READ_SPEC.requests({"recipe": "missing_quotes", "horizon_days": 7, "project_id": None,
                                       "at": "2026-10-23T14:00:00+00:00"})
    assert req.route == RECIPE_READ_SPEC.route
    assert req.path == "/assistant/recipes/missing_quotes/rows"
    assert dict(req.params) == {"horizon_days": 7, "at": "2026-10-23T14:00:00+00:00"}
    with pytest.raises(RecipeError):
        RECIPE_READ_SPEC.requests({"recipe": "../projects", "horizon_days": None, "project_id": None, "at": None})


def test_module_has_no_em_dash():
    assert EM not in Path(recipes.__file__).read_text(encoding="utf-8")
