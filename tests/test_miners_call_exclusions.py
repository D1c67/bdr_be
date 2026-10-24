"""WS0 `test_miners_call_exclusions` (docs/AI_ESTIMATING_BUILD_SPEC.md WS0
Tests, 8.1): every miner, library builder, tuner and band builder calls
`eval_manifest.excluded_projects` so the holdout (and, under LOPO, the
run's own project) never feeds it. Covered here: the scope policy miner
(scripts/mine_scope_policy.py), the pricing LOPO backtest over the cost
library (scripts/pricing_eval.py and app/services/cost_library.py) and the
history band builder (scripts/build_history_bands.py over
app/services/plans_reader_bands.py).

Each test patches `eval_manifest.excluded_projects` with a recorder (that
still returns the real answer), asserts the call with its purpose, and
asserts no holdout id reaches what the miner builds. Synthetic data only;
no database, network or model."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import cost_library as cl
from app.services import eval_manifest as M
from app.services import plans_reader_bands as pb

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
POOL_PID = sorted(M.TIER_IDS["pool"])[0]
CASE_7154_PID = M.resolve_case_project("7154")
HOLDOUT = sorted(M.HOLDOUT_IDS)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, str | None]]:
    seen: list[tuple[str, str | None]] = []
    real = M.excluded_projects

    def recorder(purpose: str, case_id: str | None = None) -> frozenset[str]:
        seen.append((purpose, case_id))
        return real(purpose, case_id)

    monkeypatch.setattr(M, "excluded_projects", recorder)
    return seen


# Scope policy mining (WS5) -----------------------------------------------------------------


def test_scope_policy_mining_calls_excluded_projects(monkeypatch, tmp_path, calls):
    msp = _load("mine_scope_policy")
    passed: dict[str, frozenset[str]] = {}

    def capture(name: str):
        def fake(backup, excluded):
            passed[name] = frozenset(excluded)
            return {}
        return fake

    for name in ("mine_proposals", "mine_estimates", "mine_boqs", "mine_projects"):
        monkeypatch.setattr(msp, name, capture(name))
    result = msp.mine(tmp_path)
    assert ("scope_policy", None) in calls
    assert result["excluded_source"] == "eval_manifest"  # never the hard-coded fallback list
    assert set(passed) == {"mine_proposals", "mine_estimates", "mine_boqs", "mine_projects"}
    for name, excluded in passed.items():
        assert M.HOLDOUT_IDS <= excluded, name


def test_scope_policy_mining_output_never_includes_a_holdout_project(tmp_path, calls):
    msp = _load("mine_scope_policy")
    (tmp_path / "data").mkdir()
    rows = [{"id": pid, "owner_type": "public_other", "wage_type": "prevailing_wage", "labor_time": "night_work"} for pid in HOLDOUT]
    rows.append({"id": POOL_PID, "owner_type": "private", "wage_type": "non_prevailing_wage", "labor_time": "day_work"})
    (tmp_path / "data" / "projects.json").write_text(json.dumps(rows))
    excluded, source = msp.excluded_projects("scope_policy")
    assert source == "eval_manifest" and calls == [("scope_policy", None)]
    out = msp.mine_projects(tmp_path, excluded)
    assert out["owner_by_wage"] == [{"owner_type": "private", "wage_type": "non_prevailing_wage", "projects": 1}]
    assert out["labor_time"] == {"day_work": 1}


# Pricing library LOPO (WS6) ----------------------------------------------------------------


class _Stop(Exception):
    pass


def test_pricing_lopo_library_calls_excluded_projects(monkeypatch, tmp_path, calls):
    pricing_eval = _load("pricing_eval")
    built: list[frozenset[str]] = []

    def fake_init(self, observations, *, exclude_projects=(), thresholds=None, as_of=None):
        built.append(frozenset(str(p) for p in exclude_projects))
        raise _Stop

    monkeypatch.setattr(cl, "backup_books", lambda root: (SimpleNamespace(entries=[], parsed=0, projects=0), {POOL_PID: object()}))
    monkeypatch.setattr(cl, "backup_observations", lambda root: [])
    monkeypatch.setattr(pricing_eval, "lopo_population", lambda bk, chosen: ([POOL_PID], {}))
    monkeypatch.setattr(cl.CostLibrary, "__init__", fake_init)
    bk = SimpleNamespace(root=tmp_path, gc_override_ids=lambda overrides: {}, tier=lambda pid: "pool")
    with pytest.raises(_Stop):
        pricing_eval.run_lopo(bk)
    assert ("pricing_library", POOL_PID) in calls
    assert built and M.HOLDOUT_IDS <= built[0] and POOL_PID in built[0]


def test_cost_library_drops_what_excluded_projects_returns(calls):
    # The library honors the exclusion set it is given: with the
    # pricing_library answer for a case, neither the holdout nor the case
    # contributes an observation.
    def obs(pid: str) -> cl.Observation:
        return cl.Observation(project_id=pid, item_key="receptacle|duplex", family="receptacle", item_class="device",
                              uom="EA", unit_labor_hours=0.5, unit_material_cost=10.0, observed_on=dt.date(2026, 9, 1))

    excluded = M.excluded_projects("pricing_library", "7154")
    assert calls == [("pricing_library", "7154")]
    lib = cl.CostLibrary([obs(p) for p in [*HOLDOUT, CASE_7154_PID, POOL_PID]], exclude_projects=excluded)
    assert {o.project_id for o in lib.observations} == {POOL_PID}
    assert lib.excluded_projects == excluded


# History bands (WS7) -------------------------------------------------------------------------


def _summary(pid: str) -> pb.EstimateSummary:
    return pb.EstimateSummary(project_id=pid, project_type="ti", devices=100.0, hours=300.0, branch_wire_ft=4500.0,
                              emt_ft=1500.0, emt34_ft=1500.0, bid_usd=None, line_ids={})


def test_history_bands_call_excluded_projects_and_drop_the_holdout(monkeypatch, tmp_path, calls):
    bhb = _load("build_history_bands")
    summaries = [_summary(p) for p in [*HOLDOUT, CASE_7154_PID, POOL_PID]]
    monkeypatch.setattr(pb, "summaries_from_backup", lambda root: (summaries, {}, {}))
    bands, json_path, md_path = bhb.build(tmp_path, tmp_path, case="7154", write=False)
    assert ("bands", "7154") in calls
    assert json_path is None and md_path is None
    ids = {p["project_id"] for p in bands.projects}
    assert ids == {POOL_PID}
    assert M.HOLDOUT_IDS <= set(bands.excluded_project_ids) and CASE_7154_PID in bands.excluded_project_ids
    calls.clear()
    bands, _j, _m = bhb.build(tmp_path, tmp_path, write=False)
    assert calls == [("bands", None)]
    assert {p["project_id"] for p in bands.projects} == {CASE_7154_PID, POOL_PID}
