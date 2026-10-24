"""WS5 bid conditions classifier (docs/AI_ESTIMATING_BUILD_SPEC.md 5.4; E4)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.services import bid_conditions as bc
from app.services.bid_conditions import BidConditions, DocumentText, classify, recheck_threshold
from app.services.scope_policy_seed import SEED_POLICY

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scope_policy_eval.py"

# 7154 Exhibit A page 2 wording (the quoted abbreviation defeats a naive NRS\s*338).
EXHIBIT_A = '3.1.1 Nevada Revised Statutes ("NRS"), Chapter 338 - Public Works'


def _doc(text: str, *, page: int = 1, role: str | None = None, document_id: str = "d1"):
    return {"document_id": document_id, "page": page, "text": text, "role": role}


def _project(**kw):
    base = {
        "name": "Synthetic Project",
        "wage_type": None,
        "owner_type": "unknown",
        "labor_time": "day_work",
        "labor_needed": "unknown",
    }
    base.update(kw)
    return base


# Cues -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cue,weight,text",
    [
        ("nrs338", 3, "Contractor shall comply with NRS 338 requirements."),
        ("nrs338", 3, "per N.R.S. 338.020"),
        ("nrs338", 3, EXHIBIT_A),
        ("nrs338", 3, "subject to the provisions of Chapter 338 of the statutes"),
        ("davis_bacon", 3, "The Davis-Bacon Act applies to this contract."),
        ("davis_bacon", 3, "Davis Bacon and related acts"),
        ("wage_determination", 3, "The attached wage determination is part of the contract."),
        ("wage_determination", 3, "General Decision Number NV20260001"),
        ("labor_commissioner", 2, "rates set by the Nevada Labor Commissioner"),
        ("public_works", 2, "This is a public works project."),
        ("public_works", 2, "under the public works contract"),
        ("public_works", 2, "PWP# CL-2026-123"),
        ("prevailing_wage", 2, "Contractor shall pay prevailing wages."),
        ("certified_payroll", 1, "Submit certified payroll weekly."),
        ("not_prevailing", -3, "This project is not subject to prevailing wage."),
        ("not_prevailing", -3, "Non-prevailing rates apply."),
    ],
)
def test_each_cue_fires_with_its_weight(cue, weight, text):
    score, hits = bc.score_cues([_doc(text)])
    names = [h.cue for h in hits]
    assert cue in names
    hit = next(h for h in hits if h.cue == cue)
    assert hit.weight == weight == bc.CUES[cue][0]
    assert hit.document_id == "d1" and hit.page == 1
    assert len(hit.quote) <= bc.QUOTE_MAX_CHARS


def test_7154_nrs338_quoted_abbreviation_matches():
    score, hits = bc.score_cues([_doc(EXHIBIT_A, page=2)])
    assert [h.cue for h in hits] == ["nrs338"] and score == 3
    nrs = next(h for h in hits if h.cue == "nrs338")
    assert nrs.page == 2 and "Chapter 338" in nrs.quote


def test_non_prevailing_wage_is_not_a_prevailing_cue():
    _score, hits = bc.score_cues([_doc("All labor is based off non-prevailing wage rates.")])
    assert [h.cue for h in hits] == ["not_prevailing"]


def test_each_cue_counts_once():
    text = "prevailing wage. " * 20
    score, hits = bc.score_cues([_doc(text), _doc(text, page=2)])
    assert score == 2 and len(hits) == 1


def test_negative_cue_cancels_generic_prevailing():
    # 7206 pattern: prevailing + certified payroll + not subject to prevailing -> 0.
    text = (
        "Prevailing wage rates do not apply. Certified payroll is not required. "
        "This project is not subject to prevailing wage law."
    )
    result = classify(_project(owner_type="private_residential"), [_doc(text)])
    assert result.pw_score == 0
    assert result.wage_type == "non_prevailing_wage"
    assert result.wage_basis == "owner_type"
    assert bc.REVIEW_PRIVATE_OWNER in result.needs_review


def test_negative_cue_alone_decides_non_prevailing():
    # "not subject to prevailing wage" also names the prevailing wage cue (+2),
    # netting -1 (undecided), exactly as the weights say; a bare negative decides.
    undecided = classify(_project(), [_doc("Not subject to prevailing wage.")])
    assert undecided.pw_score == -1 and undecided.wage_basis == "default"
    result = classify(_project(), [_doc("Non-prevailing rates apply to this work.")])
    assert result.wage_type == "non_prevailing_wage"
    assert result.wage_basis == "document_cue"
    assert result.rate_table_key == "open_shop"


def test_document_score_thresholds():
    pw = classify(_project(), [_doc("Contractor shall pay prevailing wages.")])
    assert (pw.wage_type, pw.wage_basis, pw.wage_confidence) == (
        "prevailing_wage",
        "document_cue",
        "high",
    )
    weak = classify(_project(), [_doc("Submit certified payroll.")])
    assert weak.pw_score == 1 and weak.wage_basis == "default"


def test_davis_bacon_sets_federal_wage():
    result = classify(_project(), [_doc("Davis-Bacon wage rates apply.")])
    assert result.federal_wage is True
    assert result.wage_type == "prevailing_wage"
    assert classify(_project(), [_doc("NRS 338")]).federal_wage is False


# Project record wins ----------------------------------------------------------------------


def test_project_wage_type_authoritative():
    result = classify(
        _project(wage_type="prevailing_wage"), [_doc("Non-prevailing rates apply.")]
    )
    assert (result.wage_type, result.wage_basis, result.wage_confidence) == (
        "prevailing_wage",
        "project_record",
        "high",
    )
    assert bc.REVIEW_WAGE_CONFLICT in result.needs_review  # score -3 points the other way


def test_project_record_beats_documents_and_flags_conflict():
    result = classify(_project(wage_type="non_prevailing_wage"), [_doc(EXHIBIT_A)])
    assert result.wage_type == "non_prevailing_wage"
    assert result.wage_basis == "project_record"
    assert result.rate_table_key == "open_shop"
    assert bc.REVIEW_WAGE_CONFLICT in result.needs_review


def test_project_record_without_conflict_has_no_review():
    result = classify(
        _project(wage_type="prevailing_wage", owner_type="public_other"), [_doc(EXHIBIT_A)]
    )
    assert result.needs_review == ()
    assert [c.cue for c in result.cues][0] == "nrs338"


def test_7154_like_project_is_pw_from_record_with_nrs338_cue():
    project = _project(
        wage_type="prevailing_wage",
        owner_type="public_other",
        labor_time="day_work",
        labor_needed="union",
    )
    exhibit = _doc(EXHIBIT_A, page=2, role="rfp", document_id="exA")
    hours = _doc("Work after business hours (6:00 am - 3:30 pm) only.", role="rfp")
    result = classify(project, [exhibit, hours])
    assert result.wage_type == "prevailing_wage"
    assert result.wage_basis == "project_record"
    assert result.rate_table_key == "pw"
    assert result.labor_time == "day"
    assert result.union == "union"
    nrs = next(c for c in result.cues if c.cue == "nrs338")
    assert (nrs.document_id, nrs.page) == ("exA", 2)
    assert result.needs_review == ()


def test_threshold_text_value_flags_review_and_recheck_resolves():
    project = _project(wage_type="If over $100,000 then PW if not then Non PW")
    result = classify(project, [])
    assert bc.REVIEW_WAGE_THRESHOLD_TEXT in result.needs_review
    over = recheck_threshold(result, 150_000)
    assert over.wage_type == "prevailing_wage" and over.wage_basis == "project_record"
    assert bc.REVIEW_WAGE_THRESHOLD_TEXT not in over.needs_review
    under = recheck_threshold(result, 50_000)
    assert under.wage_type == "non_prevailing_wage"
    assert under.rate_table_key == "open_shop"
    assert bc.REVIEW_BELOW_NRS338 in under.needs_review


# Owner type, lexicon, default -----------------------------------------------------------------


@pytest.mark.parametrize("owner", ["public_other", "rtc", "ccsd"])
def test_public_owner_type_is_pw(owner):
    result = classify(_project(owner_type=owner), [])
    assert (result.wage_type, result.wage_basis, result.wage_confidence) == (
        "prevailing_wage",
        "owner_type",
        "medium",
    )
    assert result.needs_review == ()


@pytest.mark.parametrize(
    "owner", ["private_commercial", "private_residential", "casino_strip", "casino_other"]
)
def test_private_owner_non_pw_needs_review(owner):
    result = classify(_project(owner_type=owner), [])
    assert result.wage_type == "non_prevailing_wage"
    assert result.wage_basis == "owner_type"
    assert bc.REVIEW_PRIVATE_OWNER in result.needs_review


@pytest.mark.parametrize(
    "name",
    [
        "UMC Outpatient Pharmacy Remodel",
        "CCSD Elementary School Lighting",
        "RTC Bus Shelter Power",
        "NDOT Maintenance Yard",
        "UNLV Student Union Theater",
    ],
)
def test_public_owner_lexicon_on_project_name(name):
    result = classify(_project(name=name, owner_type=None), [])
    assert result.wage_basis == "owner_lexicon"
    assert result.wage_type == "prevailing_wage"
    assert bc.REVIEW_OWNER_LEXICON in result.needs_review


def test_lexicon_ignores_drawing_text_but_reads_owner_documents():
    stamp = _doc("Registered professional engineer, State of Nevada", role="drawing")
    assert classify(_project(owner_type=None), [stamp]).wage_basis == "default"
    owner_doc = _doc("Owner: Clark County School District", role="rfp")
    assert classify(_project(owner_type=None), [owner_doc]).wage_basis == "owner_lexicon"
    late_page = _doc("Owner: Clark County School District", role="rfp", page=9)
    assert classify(_project(owner_type=None), [late_page]).wage_basis == "default"
    code = _doc("Comply with the UMC and UPC.", role="rfp")
    assert classify(_project(owner_type=None), [code]).wage_basis == "default"


def test_unknown_defaults_pw_low_with_needs_review():
    result = classify(_project(owner_type="unknown"), [])
    assert result.wage_type == "prevailing_wage"
    assert result.wage_basis == "default"
    assert result.wage_confidence == "low"
    assert result.rate_table_key == "pw"
    assert "wage_unknown" in result.needs_review
    none_project = classify(None, [])
    assert none_project.wage_type == "prevailing_wage" and "wage_unknown" in none_project.needs_review


def test_policy_default_and_rate_table_are_read_from_the_policy():
    policy = {
        "bid_conditions": {
            "rate_table": {"prevailing_wage": "pw", "non_prevailing_wage": "open_shop"},
            "default_wage_type": "non_prevailing_wage",
            "evidence": [],
        }
    }
    result = classify(_project(), [], policy)
    assert result.wage_type == "non_prevailing_wage" and result.rate_table_key == "open_shop"
    seeded = classify(_project(), [], SEED_POLICY)
    assert seeded.wage_type == "prevailing_wage" and seeded.rate_table_key == "pw"
    block_only = classify(_project(), [], SEED_POLICY["bid_conditions"])
    assert block_only.rate_table_key == "pw"


def test_below_nrs338_threshold_recheck():
    by_owner = classify(_project(owner_type="public_other"), [])
    flagged = recheck_threshold(by_owner, 40_000)
    assert bc.REVIEW_BELOW_NRS338 in flagged.needs_review
    assert flagged.wage_type == "prevailing_wage"
    assert recheck_threshold(by_owner, 400_000) is by_owner
    assert recheck_threshold(by_owner, None) is by_owner
    by_statute = classify(_project(owner_type="public_other"), [_doc("NRS 338")])
    assert recheck_threshold(by_statute, 40_000) is by_statute
    by_record = classify(_project(wage_type="prevailing_wage"), [])
    assert recheck_threshold(by_record, 40_000) is by_record
    again = recheck_threshold(flagged, 40_000)
    assert again.needs_review.count(bc.REVIEW_BELOW_NRS338) == 1


# Labor time -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected,basis",
    [("day_work", "day", "project_record"), ("night_work", "night", "project_record")],
)
def test_labor_time_from_project_record(raw, expected, basis):
    result = classify(_project(labor_time=raw), [])
    assert (result.labor_time, result.labor_time_basis) == (expected, basis)
    assert not [r for r in result.needs_review if r.startswith("labor_time")]


def test_labor_time_null_reads_documents():
    # spec 5.4: null -> from documents, else day with needs_review
    default = classify(_project(labor_time=None), [])
    assert default.labor_time == "day" and default.labor_time_basis == "default"
    assert bc.REVIEW_LABOR_TIME_UNKNOWN in default.needs_review
    night = classify(_project(labor_time=None), [_doc("All work is night work.")])
    assert night.labor_time == "night" and night.labor_time_basis == "document_cue"
    assert not [r for r in night.needs_review if r.startswith("labor_time")]
    window = classify(
        _project(labor_time=None), [_doc("Work after business hours (6:00 am - 3:30 pm).")]
    )
    assert window.labor_time == "day" and window.labor_time_basis == "document_cue"
    assert not [r for r in window.needs_review if r.startswith("labor_time")]


def test_mixed_language_next_to_a_day_window_is_never_dropped():
    text = "Normal work hours 7:00 am to 3:30 pm. All tie-ins shall be done after hours."
    result = classify(_project(labor_time=None), [_doc(text)])
    assert result.labor_time == "mixed" and result.labor_time_basis == "document_cue"
    assert bc.REVIEW_LABOR_TIME_MIXED in result.needs_review
    record = classify(_project(labor_time="day_work"), [_doc(text)])
    assert record.labor_time == "day" and bc.REVIEW_LABOR_TIME_CONFLICT in record.needs_review


@pytest.mark.parametrize(
    "text",
    [
        "Construction phasing plan: the building stays occupied.",
        "Phased construction is required.",
        "All tie-ins shall be done after hours.",
        "Weekend work is required for shutdowns.",
    ],
)
def test_mixed_on_phasing_or_after_hours_language(text):
    result = classify(_project(labor_time=None), [_doc(text)])
    assert result.labor_time == "mixed"
    assert bc.REVIEW_LABOR_TIME_MIXED in result.needs_review


def test_record_day_with_phasing_language_keeps_day_and_flags():
    result = classify(_project(labor_time="day_work"), [_doc("Phased construction is required.")])
    assert result.labor_time == "day"
    assert bc.REVIEW_LABOR_TIME_CONFLICT in result.needs_review


def test_unrecognized_labor_time_is_day_with_review_or_mixed_when_both():
    both = classify(_project(labor_time="Day and Loud work at Night"), [])
    assert both.labor_time == "mixed" and bc.REVIEW_LABOR_TIME_MIXED in both.needs_review
    odd = classify(_project(labor_time="swing"), [])
    assert odd.labor_time == "day" and bc.REVIEW_LABOR_TIME_UNRECOGNIZED in odd.needs_review


# Union, bond ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("union", "union"), ("non_union", "non_union"), ("ce_cw", "non_union"), ("unknown", "unknown")],
)
def test_union_from_project(raw, expected):
    assert classify(_project(labor_needed=raw), []).union == expected


def test_union_cue_when_record_unknown():
    assert classify(_project(), [_doc("Contractor signatory to IBEW Local 357.")]).union == "union"
    assert classify(_project(), [_doc("Federal Credit Union branch remodel.")]).union == "unknown"


def test_bond_required_from_bid_instructions():
    assert classify(_project(), [_doc("A payment and performance bond is required.")]).bond_required
    assert (
        classify(_project(), [_doc("No bond is required for this bid.", role="rfp")]).bond_required
        is False
    )
    assert classify(_project(), []).bond_required is None
    on_drawing = _doc("Performance bond required.", role="drawing")
    assert classify(_project(), [on_drawing]).bond_required is None


# Shapes ----------------------------------------------------------------------------------------


def test_round_trip_and_spec_fields():
    result = classify(
        _project(wage_type="prevailing_wage", owner_type="public_other"),
        [DocumentText(text=EXHIBIT_A, document_id="exA", page=2), "Davis-Bacon applies"],
    )
    data = result.to_dict()
    for key in (
        "wage_type",
        "wage_basis",
        "wage_confidence",
        "federal_wage",
        "pw_score",
        "cues",
        "labor_time",
        "union",
        "bond_required",
        "owner_type",
        "rate_table_key",
        "needs_review",
    ):
        assert key in data
    assert data["cues"][0] == {
        "cue": "nrs338",
        "weight": 3,
        "document_id": "exA",
        "page": 2,
        "quote": data["cues"][0]["quote"],
    }
    assert BidConditions.from_dict(data) == result
    assert result.wage_basis in bc.WAGE_BASES and result.labor_time in bc.LABOR_TIMES


class _FakeQuery:
    def __init__(self, rows, error=None):
        self._rows, self._error = rows, error

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        if self._error:
            raise self._error
        return type("Res", (), {"data": self._rows})()


class _FakeSb:
    def __init__(self, rows, error=None):
        self._q = _FakeQuery(rows, error)

    def table(self, _name):
        return self._q


def test_for_run_reads_stored_conditions_or_none():
    stored = classify(_project(owner_type="public_other"), []).to_dict()
    got = bc.for_run(_FakeSb([{"bid_conditions": stored}]), "run-1")
    assert isinstance(got, BidConditions) and got.wage_type == "prevailing_wage"
    assert bc.for_run(None, "run-1") is None
    assert bc.for_run(_FakeSb([]), "run-1") is None
    assert bc.for_run(_FakeSb([{"bid_conditions": None}]), "run-1") is None
    assert bc.for_run(_FakeSb([], error=RuntimeError("column missing")), "run-1") is None


# Golden: the corpus eval on the backup ---------------------------------------------------------


def _load_eval():
    name = "scope_policy_eval"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.golden
@pytest.mark.skipif(
    not (BACKUP / "data" / "projects.json").exists() or not EVAL_ROOT.exists(),
    reason="backup or EVAL_ROOT not on disk",
)
def test_golden_wage_eval_agreement_and_cue_precision():
    # Measured 2026-10-21 (holdout reported separately): wage_hidden agreement
    # 67 of 72 (93.1%) main, 4 of 4 holdout, 71 of 76 all; cue precision 35 of
    # 35 (100%); record variant 100%; owner_hidden 58 of 72 (80.6%, reported only).
    result = _load_eval().run_eval(BACKUP, EVAL_ROOT, workers=1)
    main = result["metrics"]["main"]
    assert result["population"] >= 76
    assert main["agreement"]["record"]["share"] == 1.0
    assert main["agreement"]["wage_hidden"]["share"] >= 0.93
    assert main["cue_precision"]["precision"] == 1.0
    assert main["cue_precision"]["pw_verdicts_on_non_pw"] == 0
