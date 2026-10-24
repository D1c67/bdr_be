"""app/services/plans_reader_replay.py: the stage spine, the registry, the
legacy pass-through entries, the render and score stages, and the guarantee
that a replay never calls a provider. Golden: the weekend bundle replays end
to end and reproduces its stored outputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import eval_bundle as B
from app.services import llm
from app.services import plans_reader_replay as RP
from app.services import plans_reader_render as render

from tests import eval_fixtures as F

WEEKEND = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25")
EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
golden = pytest.mark.skipif(
    not (WEEKEND / "run.json").exists() or not (EVAL_ROOT / "references" / "7154" / "reference.json").exists(),
    reason="the weekend bundle or the 7154 reference is not on disk",
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    RP.reset_stages()
    yield
    RP.reset_stages()


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    return F.make_backup(tmp_path / "backup")


@pytest.fixture
def bundle(tmp_path: Path) -> B.RunBundle:
    return B.load_bundle(F.make_bundle(tmp_path / "bundle", good=True))


def test_stage_order_mirrors_the_spine():
    assert RP.STAGE_ORDER == ("plan", "merge", "recount", "derive", "consolidate", "enforce", "gate", "evidence",
                              "render", "price", "review", "score")
    assert set(RP.STAGE_OWNERS) == set(RP.STAGE_ORDER)
    assert RP.STAGE_OWNERS["render"] == "WS0" and RP.STAGE_OWNERS["score"] == "WS0"
    assert RP.STAGE_OWNERS["recount"] == "WS7" and RP.STAGE_OWNERS["derive"] == "WS3+WS5"


def test_registry_has_a_legacy_entry_for_every_stage():
    stages = RP.registered_stages()
    assert list(stages) == list(RP.STAGE_ORDER)
    assert all(s.legacy for s in stages.values())
    assert RP.legacy_stages() == list(RP.STAGE_ORDER)
    assert not any(s.llm for s in stages.values())


def test_register_stage_validates_and_real_entries_replace_legacy():
    with pytest.raises(RP.ReplayError, match="unknown stage"):
        RP.register_stage("measure", lambda c, a: {}, version="1")
    with pytest.raises(RP.ReplayError, match="callable"):
        RP.register_stage("merge", "nope", version="1")  # type: ignore[arg-type]
    with pytest.raises(RP.ReplayError, match="version"):
        RP.register_stage("merge", lambda c, a: {}, version="")

    def ws4_merge(ctx, art):
        return {"category:electrical_drawings": {"counts": [], "rows_version": 2}}

    spec = RP.register_stage("merge", ws4_merge, version="ws4-1", reads=("groups",), writes=("category:*",), owner="WS4")
    assert spec.legacy is False and RP.stage("merge").fn is ws4_merge
    # a legacy entry never replaces a real one
    again = RP.register_stage("merge", lambda c, a: {}, version="legacy-9", legacy=True)
    assert again.fn is ws4_merge
    assert RP.legacy_stages() == [s for s in RP.STAGE_ORDER if s != "merge"]


def test_stored_artifacts_from_bundle(bundle):
    art = RP.stored_artifacts(bundle)
    assert set(art) >= {"documents", "files", "ledger", "groups", "calls", "chat", "category:electrical_drawings",
                        "category:specifications", "main", "boq_spec", "estimate_spec", "brief_spec", "marked_plans_spec", "chat_probe"}
    assert art["deliverable_versions"] == {"boq": 1, "estimate": 2, "brief": 1, "marked_plans": 1}


def test_replay_never_calls_a_provider(bundle, backup, tmp_path, monkeypatch):
    """Every llm entry point is patched to raise: a full replay still completes."""

    def boom(*_a, **_k):
        raise AssertionError("a replay called a model provider")

    for name in ("_guarded", "_build_client", "_client_for", "complete_text", "complete_json", "complete_pdf_json",
                 "complete_images_json", "complete_parts", "complete_turn"):
        monkeypatch.setattr(llm, name, boom)
    result = RP.replay(bundle, reference=F.synthetic_reference(), case_id="7154", out_dir=tmp_path / "out", backup_dir=backup)
    assert result.ok, [s.error for s in result.stages if s.error]
    assert [s.name for s in result.stages] == list(RP.STAGE_ORDER)
    assert {s.mode for s in result.stages} == {"legacy"}
    assert result.score is not None and result.score.count_lines_exact == result.score.count_lines_total == 23
    assert result.artifacts["score"]["count_lines_exact"] == 23
    assert "truth_cost" not in result.artifacts["score"]  # no dollars in the artifact
    assert result.matcher_version == 2 and result.case_id == "7154"


def test_registered_llm_stage_is_refused_a_live_call(bundle, backup, tmp_path):
    def consolidate(ctx, art):
        llm.complete_text("plans_main", system="x", messages=[])
        return {}

    RP.register_stage("consolidate", consolidate, version="ws4-1", llm=True, owner="WS4")
    with pytest.raises(RP.ReplayLiveCallRefused):
        RP.replay(bundle, out_dir=tmp_path / "out", backup_dir=backup, to_stage="consolidate")


def test_stage_modes_stored_rerun_skip_and_window(bundle, backup, tmp_path):
    result = RP.replay(bundle, reference=F.synthetic_reference(), out_dir=tmp_path / "out", backup_dir=backup,
                       from_stage="render", to_stage="score", stages={"score": "skip"})
    modes = {s.name: s.mode for s in result.stages}
    assert modes["plan"] == "stored" and modes["merge"] == "stored" and modes["render"] == "legacy" and modes["score"] == "skip"
    assert "score" not in result.artifacts and "rendered" in result.artifacts
    with pytest.raises(RP.ReplayError):
        RP.replay(bundle, out_dir=tmp_path / "o2", backup_dir=backup, from_stage="score", to_stage="plan")
    with pytest.raises(RP.ReplayError):
        RP.replay(bundle, out_dir=tmp_path / "o3", backup_dir=backup, stages={"render": "live"})
    with pytest.raises(RP.ReplayError, match="live replay"):
        RP.replay(bundle, out_dir=tmp_path / "o4", backup_dir=backup, allow_live=True)


def test_render_stage_writes_only_under_out_dir_with_office_patched_out(bundle, backup, tmp_path, monkeypatch):
    calls = []

    def office(*a, **k):
        calls.append(a)
        raise AssertionError("office converter must be patched out during replay")

    monkeypatch.setattr(render, "office_pdf_bytes", office)
    out = tmp_path / "out"
    result = RP.replay(bundle, out_dir=out, backup_dir=backup, from_stage="render", to_stage="render")
    assert result.stage("render").error is None
    rendered = result.artifacts["rendered"]
    assert rendered["office_pdf"] == "patched_out" and not calls
    files = {Path(p) for p in rendered["files"].values()}
    assert files and all(out in p.parents for p in files)
    assert {p.name for p in files} == {"BOQ - COUNTS.xlsx", "ELECTRICAL ESTIMATE.xlsx", "Deep Brief.docx"}
    rows = rendered["boq_rows"]
    assert any(r["description"].startswith("#12") and r["unit"] == "FT" and abs(r["quantity"] - 7390.15) < 0.5 for r in rows)
    assert any(r["is_accessory"] and "COUPLING" in r["description"].upper() for r in rows)
    assert rendered["marked_plans"].startswith("skipped")


def test_merge_stage_rebuilds_from_group_outputs_and_notes_differences(bundle, backup, tmp_path):
    result = RP.replay(bundle, out_dir=tmp_path / "out", backup_dir=backup, from_stage="merge", to_stage="merge")
    assert result.stage("merge").written == ["category:electrical_drawings", "category:specifications"]
    assert result.artifacts["category:electrical_drawings"]["counts"] == bundle.category_output("electrical_drawings")["counts"]
    assert not any("differs" in n for n in result.notes)


def test_replay_result_write_and_stage_hashes(bundle, backup, tmp_path):
    result = RP.replay(bundle, reference=F.synthetic_reference(), out_dir=tmp_path / "out", backup_dir=backup)
    path = result.write()
    data = json.loads((path / "replay.json").read_text())
    assert data["schema"] == RP.REPLAY_SCHEMA and data["run_id"] == F.RUN_ID and len(data["stages"]) == 12
    assert (path / "artifacts" / "score.json").is_file() and (path / "artifacts" / "category_electrical_drawings.json").is_file()
    score_stage = result.stage("score")
    assert score_stage.inputs_sha256 and score_stage.outputs_sha256 and score_stage.seconds >= 0
    assert "replayed_at" in data


def test_local_doc_bytes_implements_the_doc_bytes_interface(bundle, backup):
    docs = RP.LocalDocBytes(bundle, backup_dir=backup)
    assert set(docs.documents) == {F.ELEC_DOC, F.SPEC_DOC}
    assert docs.get(F.ELEC_DOC)[:4] == b"%PDF" and docs.reads == 1
    docs.get(F.ELEC_DOC)
    assert docs.reads == 1
    docs.evict(F.ELEC_DOC)
    docs.get(F.ELEC_DOC)
    assert docs.reads == 2
    with pytest.raises(ValueError):
        docs.get("missing")
    assert docs.available("missing") is False


@golden
@pytest.mark.golden
def test_weekend_bundle_replays_end_to_end(tmp_path):
    from app.services import eval_reference as R
    from app.services import plans_reader_score as S

    bundle = B.load_bundle(WEEKEND)
    reference = R.load_reference(EVAL_ROOT, "7154")
    result = RP.replay(bundle, reference=reference, case_id="7154", out_dir=tmp_path / "out")
    assert result.ok, [s.error for s in result.stages if s.error]
    # merge reruns reproduce the stored category outputs
    for cat in bundle.categories:
        assert json.dumps(result.artifacts[f"category:{cat}"], sort_keys=True) == json.dumps(bundle.category_output(cat), sort_keys=True)
    assert not any("differs" in n for n in result.notes)
    # render reproduces the stored BOQ rows (41 items) from the v1 spec
    rows = result.artifacts["rendered"]["boq_rows"]
    assert len(rows) == 41 == len(S.rows_from_spec(bundle.boq_spec()))
    assert result.score.count_lines_exact == 18 and result.score.count_lines_total == 23
    assert result.artifacts["score_raw"]["count_lines_exact"] == 17 and result.artifacts["score_raw"]["count_lines_total"] == 22
    assert result.artifacts["score_raw"]["dollar_coverage_partial"] == pytest.approx(0.319, abs=0.005)


def test_register_real_stages_includes_the_consolidator_and_the_evidence_stages():
    """reset_stages(real=True) (the eval and replay entry points) carries the
    real WS4/WS6 consolidate and the WS8 evidence stages, not the legacy
    pass-throughs."""
    from app.services import plans_reader_consolidator as C
    from app.services import plans_reader_evidence as E

    RP.reset_stages()
    assert RP.stage("consolidate").legacy is True and RP.stage("evidence").legacy is True
    RP.reset_stages(real=True)
    consolidate, evidence = RP.stage("consolidate"), RP.stage("evidence")
    assert consolidate.legacy is False and consolidate.fn is C.replay_consolidate
    assert consolidate.version == C.CONSOLIDATOR_VERSION
    assert evidence.legacy is False and evidence.owner == "WS8" and evidence.version == E.REPLAY_STAGE_VERSION
    assert "consolidate" not in RP.legacy_stages() and "evidence" not in RP.legacy_stages()
