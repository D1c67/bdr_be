"""scripts/plans_reader_assist_probe.py: the WS3 measure assist probe
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS3 "Live $6 (assist picks on 6
golden pool projects)"; 0.4 ledger rules 2, 3 and 6).

Pinned here:
  selection   the tagged training split, golden pool only, holdout and
              7154 excluded, fixed sha256 order; `--projects` refuses an
              excluded project; the pick takes the first projects with an
              item a model would be asked about and lists the others;
  dry run     the plan (uncertain items per project, the tool outcome per
              item, the call reserve and the expected dollars under the
              per-run cap) with no sweep and no model call;
  refusals    a live run refuses without --sweep, with a sweep of another
              purpose or workstream, a closed sweep, an unpriced route, a
              database that is not dev, and a worst case past what the
              sweep has left; nothing is armed and nothing is called;
  live path   with a scripted model and the in-memory ledger: the process
              is armed (EVAL_SWEEP_ID, the ledger guard, cassette
              record_missing), run_assist picks among the code's candidates,
              the length change per item and the per-tag comparison with the
              estimator reference are recorded, results.json and table.md
              are written under the sweep.
No network, no database, no real model; synthetic geometry and spend.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings
from app.services import eval_ledger, eval_manifest, llm
from app.services import plans_reader_measure_assist as A
from scripts import plans_reader_assist_probe as P
from tests.test_plans_reader_measure_assist import PDF, RUN, Script, _candidate_ids, _linear_item, _service

DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
ARMED = ("EVAL_SWEEP_ID", "AI_EVAL_LEDGER_ENFORCE", "LLM_CASSETTE_MODE", "LLM_CASSETTE_DIR")
POOL = sorted(eval_manifest.TIER_IDS["pool"])
ID_7154 = eval_manifest.resolve_case_project("7154")
HOLDOUT = sorted(eval_manifest.HOLDOUT_IDS)


def _boom(*_a, **_k):  # pragma: no cover - reaching it fails the test
    raise AssertionError("a model call was attempted")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(llm, "complete_turn", _boom)
    monkeypatch.setattr(llm, "complete_parts", _boom)
    saved = {k: os.environ.get(k) for k in ARMED}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    eval_ledger.use_store(None)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, openai_api_key="test-key", supabase_url=DEV_URL, **over)


def _split(root: Path, ids: list[str]) -> None:
    path = root / "manifest" / "ws3_split.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "bdr.ws3_split/1", "tagged_training": ids, "classes": {i: "tagged" for i in ids}}))


def _prepared(project_id: str, items: list[dict] | None = None) -> P.Prepared:
    svc = _service(items if items is not None else [_linear_item(1, "L5")])
    measure = svc.latest(RUN)[1]
    prep = P.Prepared(project_id=project_id, name="Synthetic project", run_id=RUN, measure=dict(measure), svc=svc, pdfs={"doc1": PDF}, source="built")
    prep.items = A.select_items(prep.measure, max_items=8)
    return prep


def _prepare_factory(with_items: set[str]):
    def prepare(pid: str) -> P.Prepared:
        return _prepared(pid, None if pid in with_items else [])

    return prepare


def _pick_fixture(req):
    fixture = next(c for c, ft in _candidate_ids(req).items() if ft == 12.0)
    return {"picks": [fixture, "c99"], "none_fit": False, "note": "the outline under the tag"}


# -- selection ---------------------------------------------------------------------------


def test_candidates_are_pool_only_without_holdout_or_7154_in_a_fixed_order():
    split = {"tagged_training": [ID_7154, HOLDOUT[0], POOL[0], POOL[1], POOL[2]]}
    excluded = P.excluded_ids()
    assert ID_7154 in excluded and set(HOLDOUT) <= excluded
    got = P.candidate_ids(split, excluded=excluded, pool=eval_manifest.TIER_IDS["pool"])
    assert set(got) == {POOL[0], POOL[1], POOL[2]}
    assert got == sorted(got, key=P.order_key)
    assert got == P.candidate_ids({"tagged_training": list(reversed(split["tagged_training"]))}, excluded=excluded, pool=eval_manifest.TIER_IDS["pool"])
    with pytest.raises(P.ProbeError, match="excluded"):
        P.resolve_requested([ID_7154[:8]], got, excluded=excluded)
    with pytest.raises(P.ProbeError, match="exactly one"):
        P.resolve_requested(["zzzzzzzz"], got, excluded=excluded)
    assert P.resolve_requested([POOL[1][:8]], got, excluded=excluded) == [POOL[1]]


def test_the_pick_takes_the_first_projects_with_an_item_to_ask_and_lists_the_rest():
    ids = POOL[:5]
    s = P._settings_for_assist(_settings())
    route = P.derive_route(s, None)
    picked, skipped = P.pick_projects(ids, n=2, prepare=_prepare_factory({ids[1], ids[2], ids[3]}),
                                      preview=lambda p: P.preview_project(p, settings=s, route=route), log=lambda _m: None)
    assert [p.project_id for p, _pv in picked] == [ids[1], ids[2]]
    assert [r["project_id"] for r in skipped] == [ids[0]] and skipped[0]["why"] == "no uncertain linear item"


# -- dry run ---------------------------------------------------------------------------


def test_dry_run_lists_uncertain_items_and_the_estimate_with_no_sweep_and_no_call(tmp_path, capsys):
    _split(tmp_path, [ID_7154, *POOL[:3]])
    with_items = set(POOL[:3])
    out = tmp_path / "plan"
    code = P.main(["--dry-run", "--eval-root", str(tmp_path), "--out", str(out), "--n", "2"], settings=_settings(), prepare=_prepare_factory(with_items))
    assert code == 0
    printed = capsys.readouterr().out
    assert "2 of 2 projects picked from 3 candidates" in printed and "model calls made: 0" in printed
    assert "NOT PRICED" not in printed and "L5 on E2.01: unmatched, 3 candidates, ask" in printed
    plan = json.loads((out / "plan.json").read_text())
    assert plan["model_calls_made"] == 0 and len(plan["projects"]) == 2
    row = plan["projects"][0]
    assert row["uncertain"] == 1 and row["would_ask"] == 1 and row["outcomes"] == {"ask": 1}
    item = row["items"][0]
    assert item["candidates"] == 3 and item["input_tokens"] > 0 and 0 < item["expected_usd"] <= item["reserve_usd"]
    # Worst case per project: the smaller of the per-run cap and the summed reserves.
    assert row["worst_usd"] == pytest.approx(min(0.75, item["reserve_usd"]))
    assert plan["worst_usd"] == pytest.approx(round(2 * row["worst_usd"], 2), abs=0.011)
    assert ID_7154 not in {p["project_id"] for p in plan["projects"]}


# -- refusals ---------------------------------------------------------------------------


def _ledger(purpose: str = "dev_probe", workstream: str = "WS3", budget: str = "6", close: bool = False) -> str:
    eval_ledger.use_store(eval_ledger.MemoryLedgerStore())
    row = eval_ledger.open_sweep(label="assist probe test", workstream=workstream, purpose=purpose, budget_usd=Decimal(budget))
    if close:
        eval_ledger.close_sweep(sweep_id=row["id"])
    return row["id"]


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"purpose": "ab"}, "purpose ab"),
        ({"workstream": "WS2"}, "belongs to WS2"),
        ({"close": True}, "not open"),
        ({"budget": "0.01"}, "worst case"),
    ],
)
def test_a_live_run_refuses_the_wrong_sweep_and_an_oversized_plan(tmp_path, capsys, kwargs, why):
    _split(tmp_path, POOL[:2])
    sweep = _ledger(**kwargs)
    code = P.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--n", "2"], settings=_settings(), prepare=_prepare_factory(set(POOL[:2])))
    assert code == 2 and why in capsys.readouterr().err
    if why != "worst case":
        assert os.environ.get("EVAL_SWEEP_ID") != sweep


def test_a_live_run_refuses_without_a_sweep_unpriced_and_prod(tmp_path, capsys):
    _split(tmp_path, POOL[:1])
    prep = _prepare_factory({POOL[0]})
    assert P.main(["--eval-root", str(tmp_path)], settings=_settings(), prepare=prep) == 2
    assert "needs --sweep" in capsys.readouterr().err
    sweep = _ledger()
    assert P.main(["--eval-root", str(tmp_path), "--sweep", sweep, "--route", "openai:no-such-model"], settings=_settings(), prepare=prep) == 2
    err = capsys.readouterr().err
    assert "no verified llm_pricing row for no-such-model" in err
    prod = Settings(_env_file=None, openai_api_key="k", supabase_url=PROD_URL)
    assert P.main(["--eval-root", str(tmp_path), "--sweep", sweep], settings=prod, prepare=prep) == 2
    assert "dev project" in capsys.readouterr().err
    assert os.environ.get("EVAL_SWEEP_ID") != sweep


# -- live path (scripted model) ------------------------------------------------------------


def test_a_scripted_live_run_arms_the_sweep_records_picks_and_writes_results(tmp_path, capsys, monkeypatch):
    _split(tmp_path, POOL[:3])
    ref = tmp_path / "references" / "9999" / "reference.json"
    ref.parent.mkdir(parents=True)
    ref.write_text(json.dumps({"project_id": POOL[0], "quantity_lines_adjudicated": [
        {"section": "NEW WORK", "description": "L5 LED UNDERCABINET LIGHT", "unit": "FT", "qty": 12.5},
        {"section": "DEMOLITION", "description": "L5 LED UNDERCABINET LIGHT", "unit": "FT", "qty": 99.0},
    ]}))
    sweep = _ledger()
    script = Script([_pick_fixture, {"picks": [], "none_fit": True, "note": "no outline"}])
    monkeypatch.setattr(llm, "complete_turn", script)
    code = P.main(["--eval-root", str(tmp_path), "--sweep", "open", "--projects", f"{POOL[0][:8]},{POOL[1][:8]}"],
                  settings=_settings(), prepare=_prepare_factory(set(POOL[:3])))
    assert code == 0
    assert os.environ["EVAL_SWEEP_ID"] == sweep and os.environ["AI_EVAL_LEDGER_ENFORCE"] == "true"
    assert os.environ["LLM_CASSETTE_MODE"] == "record_missing" and os.environ["LLM_CASSETTE_DIR"] == str(tmp_path / "cassettes")
    assert len(script.requests) == 2 and all(f == "plans_derive" and r.step == "derive" for f, r in script.requests)
    runs = list((tmp_path / "sweeps" / sweep).glob("assist_probe_*"))
    assert len(runs) == 1
    payload = json.loads((runs[0] / "results.json").read_text())
    assert payload["schema"] == P.RESULTS_SCHEMA and payload["sweep_id"] == sweep
    first, second = payload["results"]
    assert first["project_id"] == POOL[0] and first["status"] == "done"
    assert (first["uncertain"], first["asked"], first["picked_items"], first["picks"], first["none_fit"], first["rejected_picks"]) == (1, 1, 1, 1, 0, 1)
    item = first["items"][0]
    assert item["outcome"] == "picked" and item["code_ft"] is None and item["assist_ft"] == pytest.approx(12.0, abs=0.05) and item["change"] == "measured"
    assert item["rejected"] == [{"id": "c99", "why": "not_a_candidate"}]
    tag = first["tags"][0]
    assert tag["tag"] == "L5" and tag["estimator_ft"] == 12.5 and first["estimator_source"] == "reference.json"
    assert tag["assist_err"] == pytest.approx((12.0 - 12.5) / 12.5, abs=0.01)
    assert first["cost_usd"] == pytest.approx(0.01) and first["calls"][0]["cost_usd"] == pytest.approx(0.01) and first["calls"][0]["error"] is None
    assert (second["picked_items"], second["none_fit"], second["items"][0]["delta_ft"]) == (0, 1, None)
    assert payload["spent_usd"] == pytest.approx(0.02)
    table = (runs[0] / "table.md").read_text()
    assert "| L5 |" in table and "none_fit" in table and chr(0x2014) not in table and chr(0x2013) not in table
    assert "results:" in capsys.readouterr().out


def test_run_live_skips_a_project_the_sweep_cannot_cover_and_survives_a_failure():
    a, b, c = _prepared(POOL[0]), _prepared(POOL[1]), _prepared(POOL[2])
    picked = [(a, {"worst_usd": Decimal("0.50")}), (b, {"worst_usd": Decimal("0.50")}), (c, {"worst_usd": Decimal("0.50")})]
    calls = []

    def fake_assist(run_id, **kw):
        calls.append(run_id)
        if len(calls) == 1:
            raise RuntimeError("synthetic failure")
        return A.AssistResult(status="done", calls=1, spent_usd=0.45, items=[])

    results, skipped = P.run_live(picked, settings=_settings(), route_snapshot=None, remaining_usd=Decimal("0.90"), data=None,
                                  root=Path("/nonexistent"), assist=fake_assist, log=lambda _m: None)
    assert [r["status"] for r in results] == ["error", "done"] and skipped == [POOL[2]]
    assert results[0]["message"].startswith("RuntimeError")


def test_estimator_reference_is_quantities_by_tag_without_demolition(tmp_path):
    ref = tmp_path / "references" / "1234" / "reference.json"
    ref.parent.mkdir(parents=True)
    ref.write_text(json.dumps({"project_id": "p1", "quantity_lines": [
        {"section": "NEW WORK", "description": "F3 LINEAR PENDANT 8'", "unit": "EA", "qty": 4, "unit_material": 1.0},
        {"section": "NEW WORK", "description": "F3 LINEAR PENDANT", "unit": "FT", "qty": 32},
        {"section": "NEW WORK", "description": "L1A 2X4 LED TROFFER", "unit": "EA", "qty": 10},
    ]}))
    got = P.estimator_linear(None, tmp_path, "p1", ["F3"])
    assert got == {"source": "reference.json", "tags": {"F3": {"ft": 32.0, "ea": 4.0, "rows": 2}}}
    assert P.estimator_linear(None, tmp_path, "p2", ["F3"]) == {"source": None, "tags": {}}


def test_route_parsing():
    assert P.parse_route(None) is None
    assert P.parse_route("openai:gpt-6-astra:medium") == {"provider": "openai", "model": "gpt-6-astra", "tools": False, "explicit": True, "effort": "medium"}
    for bad in ("gpt-6-astra", "x:y", "openai:"):
        with pytest.raises(P.ProbeError):
            P.parse_route(bad)
