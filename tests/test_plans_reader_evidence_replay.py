"""Evidence replay on the 7154 weekend bundle (WS8 wave 3, spec 6 WS8
"evidence replay on 7154"; E17 "marks at text anchors"): the replay runs the
real evidence stage at $0 over the stored bundle artifacts with page facts
extracted from the backup PDFs, and the E17 acceptance check is computed on
the evidence marks (plans_reader_acceptance.check_e17 reads the `evidence`
artifact first). The measured values are printed and pinned as floors so a
regression shows. Golden (needs the weekend bundle, the 7154 references
and the backup on disk); a cheap registration test runs everywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import eval_bundle as B
from app.services import plans_reader_acceptance as AC
from app.services import plans_reader_evidence as E
from app.services import plans_reader_replay as RP

WEEKEND = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25")
EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
golden = pytest.mark.golden
needs_bundle = pytest.mark.skipif(
    not (WEEKEND / "run.json").exists()
    or not (EVAL_ROOT / "references" / "7154" / "reference.json").exists()
    or not BACKUP.exists(),
    reason="7154 weekend bundle, references or backup not on this machine",
)


@pytest.fixture
def stages():
    # real=True keeps the stages siblings register at import (WS3 measure,
    # WS4 merge and gates, WS6 price) in place for the rest of the session;
    # the WS8 evidence and the consolidate stages are part of
    # register_real_stages, so they are real again after the test.
    RP.reset_stages(real=True)
    yield
    RP.reset_stages(real=True)


def test_evidence_stage_registers_as_the_real_ws8_stage(stages):
    # reset_stages(real=True) registers the real evidence stage; the legacy
    # entry comes back only with a plain reset.
    assert RP.stage("evidence").legacy is False and RP.stage("evidence").owner == "WS8"
    RP.reset_stages()
    assert RP.stage("evidence").legacy is True
    spec = E.register_replay_stage()
    assert spec.owner == "WS8" and spec.legacy is False and RP.stage("evidence") is spec
    assert "measure" in spec.reads and spec.writes == ("evidence",)
    # register_real_stages keeps a real entry (never replaced by legacy);
    # it re-registers an equal spec, not the same object.
    RP.register_real_stages()
    after = RP.stage("evidence")
    assert after.owner == "WS8" and after.legacy is False and after.version == spec.version


@golden
@needs_bundle
def test_7154_evidence_replay_computes_e17_on_the_evidence_marks(stages, tmp_path):
    from app.services import eval_reference as R

    bundle = B.load_bundle(WEEKEND)
    reference = R.load_reference(EVAL_ROOT, "7154")
    expected = json.loads((EVAL_ROOT / "references" / "7154" / "expected.json").read_text())
    RP.register_real_stages()
    E.register_replay_stage()
    result = RP.replay(bundle, reference=reference, case_id="7154", out_dir=tmp_path / "out", to_stage="evidence")
    assert result.ok, [s.error for s in result.stages if s.error]
    evidence = result.artifacts["evidence"]
    assert evidence["schema"] == E.EVIDENCE_SCHEMA and evidence["run_id"] == bundle.run_id
    stats = evidence["stats"]
    assert stats["sheets"] >= 5 and stats["ea_marked"] > 0 and stats["lines_with_qty_chip"] == stats["lines"]
    assert E.money_keys(evidence) == []
    e301 = next(s for s in evidence["sheets"] if s["sheet_number"] == "E3.01")
    assert {k: e301["anchor_counts"][k] for k in ("L1", "L2", "L3", "L4", "L5", "X1")} == {
        "L1": 21, "L2": 1, "L3": 16, "L4": 3, "L5": 7, "X1": 2}

    inp = AC.inputs_from_replay(result, reference=reference, expected=expected, case_id="7154",
                                docs=RP.LocalDocBytes(bundle))
    e17 = AC.check_e17(inp)
    print("E17 on the 7154 evidence marks:", json.dumps(e17.measured, indent=1, default=str))
    m = e17.measured
    assert m["parts"]["median_anchor_distance"] in ("pass", "fail")
    median = m["median_anchor_distance"]["median_ft"]
    marks = m["median_anchor_distance"]["marks"]
    over = m["over_5ft_share"]["over_5ft"]
    stacked = m["stacked_duplicates"]["stacked"]
    # Measured 2026-10-22 on the stored v1 run (no measure output, so no
    # polylines): median 0.0 ft (median_any 0.0) over 165 marks on four
    # electrical sheets (ED2.01 34, ED3.01 39, E2.01 30 at median 1.85 ft,
    # E3.01 62), 2 of 165 over 5 ft (share 0.0121, both on E3.01), 0
    # stacked, MD2.01 skipped as a mechanical sheet. Pinned as floors against
    # the weekend baseline of the build log (median 4.95 ft, 115 of 207 over
    # 5 ft, 26 stacked).
    assert marks >= 150
    assert median <= 1.0
    assert over / marks <= 0.05
    assert stacked == 0
    assert m["parts"]["stacked_duplicates"] == "pass"
    assert m["parts"]["ft_lines_have_polylines"] == "skip"  # the stored v1 BOQ has no measured FT line
