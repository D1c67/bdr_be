"""The AI Estimating spine (docs/AI_ESTIMATING_BUILD_SPEC.md 4.1 and 4.2;
services/plans_reader_runs.py and plans_reader_agents.py, wave 3).

What these tests pin, with every stage faked (no model, no network):
- the 4.1 order on a synthetic run: per category the code merge, the
  pre-gates and the recount targets (pass 3) before the re-merge; the
  derive group (scope prepare, then the measure) inserted and run before
  the main group is queued; _finish_with_main in its fixed order (enforce,
  final gates persisted, evidence, render, pricing autotrigger, autonomy);
- the derive group is dispatched before the cap check and a failed derive
  never blocks main;
- the post-run chain: the pricing job holds the run's change slot, never
  changes the run status and recomputes the autonomy state; a modify
  recomputes it too;
- flags off: the T-INT-1 harness fingerprint, the legacy main request from
  a v2 stored output and the legacy single call (one _call_model with
  MAIN_AGENT_SCHEMA, no tool loop);
- review kinds stopped by a cap or a loop failure end partial (WS7's
  finalizer seam) with the run status untouched, while recount and main
  in tools mode pause the run over_cap;
- the 0165 job type CHECK equals llm_queue.ALL_JOB_TYPES.
"""

from __future__ import annotations

import re
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import ai_spend, llm, llm_queue
from app.services import plans_reader_agents as agents
from app.services import plans_reader_render as render
from app.services import plans_reader_runs as runs
from app.services.plans_reader_schemas import MAIN_AGENT_SCHEMA
from tests.test_plans_reader_agents import FakeModel
from tests.test_plans_reader_runs import (
    install_env,
    main_output_spec,
    make_settings,
    seed_document,
    set_status,
    sub_output,
    user,
)


@pytest.fixture
def renders(monkeypatch):
    """Every renderer faked (the runs suite's fixture, trimmed)."""
    monkeypatch.setattr(render, "render_boq_xlsx", lambda spec: b"PK-boq")
    monkeypatch.setattr(render, "render_estimate_xlsx", lambda boq, est, *, date: b"PK-estimate")
    monkeypatch.setattr(render, "render_markup_pdf", lambda sources, sheets, *, kind, **_k: b"%PDF-markup")
    monkeypatch.setattr(render, "render_marked_plans_pdf", lambda pages: list(pages) and b"%PDF-marked")
    monkeypatch.setattr(render, "render_brief_docx", lambda spec, *, project_name: b"PK-docx")
    monkeypatch.setattr(render, "brief_pdf_bytes", lambda docx: b"%PDF-brief")
    monkeypatch.setattr(render, "office_pdf_bytes", lambda content, filename: b"%PDF-copy-" + content)
    return SimpleNamespace()

SPINE_ON = dict(
    plans_reader_derive_enabled=True,
    plans_reader_recount_enabled=True,
    plans_reader_gates_blocking=True,
    ai_scope_policy_enforce=True,
    ai_pricing_enabled=True,
    ai_pricing_autorun=True,
    plans_reader_marked_plans_mode="code",
)


@pytest.fixture
def env(monkeypatch):
    return install_env(monkeypatch)


def _reading_run(env, **fields):
    run = runs.create_run(env.db, name="Spine run", source="upload", project_id=None, render_mode="adaptive", user=user())
    return set_status(env, run, "reading", ledger={"text_tokens": 100, "images": []}, **fields)


def _sub(env, run, doc, category="electrical_drawings", index=0, status="done", output=None):
    pages = [{"document_id": doc["id"], "page": 1, "ppi": 155, "detail": "original", "tiles": None,
              "kind": "image", "w_in": 42.0, "h_in": 30.0}]
    out = output if output is not None else agents.map_doc_index(sub_output(category, [(1, 1)]), {1: doc["id"]})
    return env.db.add("plans_reader_groups", run_id=run["id"], kind="sub", category=category, group_index=index,
                      **{"pass": 1}, pages=pages, status=status, est_input_tokens=1000, output=out)


def _groups(env, run, kind):
    return sorted(env.db.rows("plans_reader_groups", run_id=run["id"], kind=kind), key=lambda g: g["created_at"])


def _run(env, run):
    return env.db.rows("plans_reader_runs", id=run["id"])[0]


# ---------------------------------------------------------------------------
# The 4.1 order on a synthetic run
# ---------------------------------------------------------------------------


class _StageLog:
    """Every stage faked and logged in call order."""

    def __init__(self, monkeypatch, env):
        from app.services import plans_reader_autonomy, plans_reader_evidence, plans_reader_gates, plans_reader_measure
        from app.services import plans_reader_merge, plans_reader_recount, pricing_run, scope_engine
        from app.services.plans_reader_recount import RecountTarget

        self.calls: list[str] = []
        log = self.calls
        real_merge = plans_reader_merge.consolidate_category

        def merge(category, groups, **kw):
            log.append(f"merge:{category}:prior={'yes' if kw.get('prior') else 'no'}")
            return real_merge(category, groups, **kw)

        monkeypatch.setattr(plans_reader_merge, "consolidate_category", merge)
        monkeypatch.setattr(plans_reader_gates, "pre_consolidation", lambda outputs, **kw: log.append("pre_gates") or [])

        def select_targets(rows, **kw):
            log.append("select_targets")
            row = next(iter(rows))
            return [RecountTarget(
                target_id="C1@D1P1", row_id=row["row_id"], priority="P4", reason="top-dollar counted row",
                document_id=row["document_id"], page=1, sheet_key="D1P1", sheet_number="E-101",
                category="electrical_drawings", family="R", description="Duplex receptacle", symbol="R",
                builder_qty=4, unit="EA",
            )]

        monkeypatch.setattr(plans_reader_recount, "select_targets", select_targets)

        def request_recounts(sb, run, targets, *, enqueue=None, **kw):
            log.append("request_recounts")
            first = targets[0]
            return sb.table("plans_reader_groups").insert([{
                "run_id": run["id"], "kind": "recount", "category": first.category, "group_index": 0, "pass": 3,
                "pages": [{"document_id": first.document_id, "page": first.page, "ppi": 150, "detail": "original",
                           "tiles": None, "kind": "image"}],
                "status": "queued", "est_input_tokens": 500, "mode": "read", "route": {"provider": "openai"},
                "loop_state": {"recount": {"targets": [first.to_state()]}},
            }]).execute().data

        monkeypatch.setattr(plans_reader_recount, "request_recounts", request_recounts)

        def recount_group(sb, run, group, *, settings=None):
            log.append("recount_group")
            target = group["loop_state"]["recount"]["targets"][0]
            out = sub_output("electrical_drawings", [(1, 1)], summary="recount")
            out["counts"][0].update({"quantity": 5, "supersedes_row_id": target["row_id"], "coverage": "whole_sheet",
                                     "document_id": target["document_id"], "points": [[0.1, 0.2]]})
            out["pages"][0]["document_id"] = target["document_id"]
            return out

        monkeypatch.setattr(runs, "run_recount_group", recount_group)
        monkeypatch.setattr(scope_engine, "prepare", lambda sb, run, outputs=None, **kw: log.append("scope_prepare") or {"facts": []})
        monkeypatch.setattr(plans_reader_measure, "build_measure_safe", lambda inputs, conv=None: log.append("build_measure") or {"version": 1, "quantities": []})
        monkeypatch.setattr(plans_reader_measure.MeasureService, "register_run", lambda self, run_id, measure, *, source_group_ids=(): log.append("measure_write") or 1)
        monkeypatch.setattr(scope_engine, "enforce_main_output", lambda sb, run, output, **kw: log.append("enforce") or {**output, "excluded_lines": []})
        monkeypatch.setattr(scope_engine, "facts_for_run", lambda sb, run_id: [])

        def run_final(**kw):
            log.append("final_gates")
            return SimpleNamespace(findings=[], summary=lambda: {"blocking": False, "codes": [], "enforced": True})

        monkeypatch.setattr(plans_reader_gates, "run_final", run_final)
        monkeypatch.setattr(plans_reader_gates, "record_findings", lambda sb, run, findings, stage: log.append(f"record_findings:{stage}") or [])
        monkeypatch.setattr(plans_reader_evidence, "build", lambda sb, run, **kw: log.append("evidence") or {"sheets": [], "lines": []})
        monkeypatch.setattr(plans_reader_evidence, "evidence_to_marked_spec", lambda ev: {"sheets": []})
        monkeypatch.setattr(pricing_run, "autotrigger", lambda sb, run_id, **kw: log.append("pricing_autotrigger") or {"id": "job-pricing"})
        monkeypatch.setattr(pricing_run, "on_new_boq_version", lambda sb, run_id, v, **kw: log.append(f"on_new_boq_version:{v}") or None)
        monkeypatch.setattr(plans_reader_autonomy, "recompute", lambda sb, run_id, **kw: log.append(f"autonomy:{kw.get('chain_active')}") or "in_progress")
        real_render = runs.render_deliverable

        def render(sb, run, kind, spec, **kw):
            log.append(f"render:{kind}")
            return real_render(sb, run, kind, spec, **kw)

        monkeypatch.setattr(runs, "render_deliverable", render)


def test_spine_runs_in_the_4_1_order(env, monkeypatch, renders):
    env.settings = make_settings(**SPINE_ON)
    log = _StageLog(monkeypatch, env)
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    # Reading: the category settles -> merge, pre-gates, targets, one recount group queued (pass 3).
    runs.advance(env.db, run["id"])
    recounts = _groups(env, run, "recount")
    assert len(recounts) == 1 and recounts[0]["pass"] == 3 and _run(env, run)["status"] == "reading"
    assert [j["target_id"] for j in env.enqueued] == [recounts[0]["id"]]
    pre = env.db.rows("plans_reader_outputs", run_id=run["id"], scope="category")
    assert len(pre) == 1 and pre[0]["version"] == 1 and pre[0]["output"]["rows_version"] == 2
    # The recount group runs (the tool loop is WS7's runner here), then the run consolidates.
    agents.run_group(env.db, recounts[0]["id"])
    assert _groups(env, run, "recount")[0]["status"] == "done"
    assert _run(env, run)["status"] == "consolidating"
    cats = sorted(env.db.rows("plans_reader_outputs", run_id=run["id"], scope="category"), key=lambda o: o["version"])
    assert [o["version"] for o in cats] == [1, 2] and recounts[0]["id"] in cats[1]["source_group_ids"]
    # The derive group is inserted and enqueued before any main group exists.
    derives, mains = _groups(env, run, "derive"), _groups(env, run, "main")
    assert len(derives) == 1 and not mains and env.enqueued[-1]["target_id"] == derives[0]["id"]
    agents.run_group(env.db, derives[0]["id"])
    derive = _groups(env, run, "derive")[0]
    assert derive["status"] == "done" and derive["error"] is None and derive["output"]["measure_version"] == 1
    mains = _groups(env, run, "main")
    assert len(mains) == 1 and env.enqueued[-1]["target_id"] == mains[0]["id"]
    assert derive["created_at"] < mains[0]["created_at"]
    # The main group answers; the run closes in the fixed render order.
    env.db.table("plans_reader_groups").update({"status": "done", "output": main_output_spec(doc["id"])}).eq("id", mains[0]["id"]).execute()
    runs.advance(env.db, run["id"])
    fresh = _run(env, run)
    assert fresh["status"] == "done", fresh.get("error")
    assert fresh["gate_summary"] == {"blocking": False, "codes": [], "enforced": True}
    assert env.db.rows("plans_reader_outputs", run_id=run["id"], scope="main")[0]["output"]["excluded_lines"] == []
    renders_seen = [c for c in log.calls if c.startswith("render:")]
    assert renders_seen[0] == "render:boq" and "render:marked_plans" in renders_seen
    without_renders = [c for c in log.calls if not c.startswith("render:")]
    assert without_renders == [
        "merge:electrical_drawings:prior=no", "pre_gates", "select_targets", "request_recounts",
        "recount_group",
        "merge:electrical_drawings:prior=yes",
        "scope_prepare", "build_measure", "measure_write",
        "enforce", "final_gates", "record_findings:final", "evidence",
        "on_new_boq_version:1",
        "pricing_autotrigger", "autonomy:True",
    ]
    # groups_total counts the pipeline kinds only (sub, recount, derive, main).
    assert fresh["groups_total"] == 4 and fresh["groups_done"] == 4


def _json_decode_error():
    import json

    try:
        json.loads("")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("unreachable")


def test_a_failed_annotated_plans_render_never_becomes_a_run_error(monkeypatch):
    """The first live WS4 iteration ended done_with_errors with "Annotated
    plans: Expecting value: line 1 column 1 (char 0)" (storage3 parsing a
    non-JSON error page). The seam now logs, records the evidence note and
    returns no render error."""
    from app.services import plans_reader_annotated

    seen: list[tuple] = []

    def boom(sb, run, **kw):
        raise _json_decode_error()

    monkeypatch.setattr(plans_reader_annotated, "render_annotated_plans", boom)
    monkeypatch.setattr(plans_reader_annotated, "record_annotated_failure", lambda sb, run_id, exc: seen.append((run_id, type(exc).__name__)))
    assert runs._render_annotated_plans(object(), {"id": "r1"}, {"sheets": []}) == []
    assert seen == [("r1", "JSONDecodeError")]
    # A note writer that raises is swallowed too.
    monkeypatch.setattr(plans_reader_annotated, "record_annotated_failure", lambda *a: 1 / 0)
    assert runs._render_annotated_plans(object(), {"id": "r1"}, {"sheets": []}) == []
    # A renderer module that fails to import is a missing renderer.
    monkeypatch.setattr(runs, "ANNOTATED_PLANS_SEAM", ("app.services.no_such_annotated_module", "render_annotated_plans"))
    assert runs._render_annotated_plans(object(), {"id": "r1"}, {"sheets": []}) == []


def test_the_run_ends_done_when_the_annotated_plans_upload_fails(env, monkeypatch, renders):
    from app.services import plans_reader_annotated

    env.settings = make_settings(**SPINE_ON, plans_reader_annotated_plans_enabled=True)
    _StageLog(monkeypatch, env)
    attempts: list[str] = []
    notes: list[str] = []

    def failing_render(sb, run, **kw):
        attempts.append(str(run["id"]))
        raise _json_decode_error()

    monkeypatch.setattr(plans_reader_annotated, "render_annotated_plans", failing_render)
    monkeypatch.setattr(plans_reader_annotated, "record_annotated_failure", lambda sb, run_id, exc: notes.append(run_id))
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    runs.advance(env.db, run["id"])
    agents.run_group(env.db, _groups(env, run, "recount")[0]["id"])
    agents.run_group(env.db, _groups(env, run, "derive")[0]["id"])
    main = _groups(env, run, "main")[0]
    env.db.table("plans_reader_groups").update({"status": "done", "output": main_output_spec(doc["id"])}).eq("id", main["id"]).execute()
    runs.advance(env.db, run["id"])
    fresh = _run(env, run)
    assert attempts == [run["id"]] and notes == [run["id"]]
    assert fresh["status"] == "done" and not fresh.get("error")
    kinds = {r["kind"] for r in env.db.rows("plans_reader_deliverables", run_id=run["id"])}
    assert {"boq", "marked_plans"} <= kinds and "annotated_plans" not in kinds


def test_with_every_parent_flag_off_the_spine_is_the_legacy_close(env, monkeypatch, renders):
    from app.services import plans_reader_autonomy, plans_reader_gates, pricing_run, scope_engine

    assert not runs.spine_enabled(env.settings)
    touched: list[str] = []
    for mod, name in ((plans_reader_gates, "run_final"), (scope_engine, "enforce_main_output"),
                      (pricing_run, "autotrigger"), (plans_reader_autonomy, "recompute")):
        monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: touched.append(_n))
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    runs.advance(env.db, run["id"])
    assert _run(env, run)["status"] == "consolidating"
    assert not _groups(env, run, "recount") and not _groups(env, run, "derive")
    main = _groups(env, run, "main")[0]
    env.db.table("plans_reader_groups").update({"status": "done", "output": main_output_spec(doc["id"])}).eq("id", main["id"]).execute()
    runs.advance(env.db, run["id"])
    fresh = _run(env, run)
    assert fresh["status"] == "done" and touched == [] and "gate_summary" not in fresh
    assert "estimated_cost_usd_max" not in fresh


# ---------------------------------------------------------------------------
# The derive group
# ---------------------------------------------------------------------------


def _consolidating_with_derive(env):
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    run = set_status(env, run, "consolidating")
    env.db.add("plans_reader_outputs", run_id=run["id"], scope="category", category="electrical_drawings",
               version=1, output=sub_output("electrical_drawings", [(1, 1)]), source_group_ids=[])
    derive = env.db.add("plans_reader_groups", run_id=run["id"], kind="derive", status="queued", pages=[], est_input_tokens=0)
    return run, doc, derive


def test_derive_is_dispatched_before_the_cap_check(env, monkeypatch):
    env.settings = make_settings(**SPINE_ON)
    run, _doc, derive = _consolidating_with_derive(env)
    env.cap_error = "The AI Estimating daily spend cap is reached."
    monkeypatch.setattr(runs, "run_derive", lambda sb, run, group: {"policy_version": 1, "measure_version": 1, "warnings": []})
    agents.run_group(env.db, derive["id"])
    assert _groups(env, run, "derive")[0]["status"] == "done"
    assert _run(env, run)["status"] == "consolidating"  # never over_cap: a $0 group
    assert len(_groups(env, run, "main")) == 1  # the derive's advance queued main


def test_a_failed_derive_never_blocks_main(env, monkeypatch):
    env.settings = make_settings(**SPINE_ON)
    run, _doc, derive = _consolidating_with_derive(env)

    def boom(sb, run, group):
        raise RuntimeError("pdfium exploded")

    monkeypatch.setattr(runs, "run_derive", boom)
    agents.run_group(env.db, derive["id"])
    done = _groups(env, run, "derive")[0]
    assert done["status"] == "done" and "pdfium exploded" in done["error"]
    assert done["output"]["warnings"][0].startswith("derive_failed")
    assert len(_groups(env, run, "main")) == 1 and _run(env, run)["status"] == "consolidating"


def test_run_derive_runs_prepare_then_the_measure_and_tolerates_a_failed_prepare(env, monkeypatch):
    from app.services import plans_reader_measure, scope_engine

    env.settings = make_settings(**SPINE_ON)
    run, _doc, derive = _consolidating_with_derive(env)
    order: list[str] = []

    def prepare(sb, run, outputs=None, **kw):
        order.append("prepare")
        raise RuntimeError("no active policy")

    monkeypatch.setattr(scope_engine, "prepare", prepare)
    monkeypatch.setattr(plans_reader_measure, "build_measure_safe", lambda inputs, conv=None: order.append("measure") or {"version": 1, "quantities": []})
    monkeypatch.setattr(plans_reader_measure.MeasureService, "register_run", lambda self, run_id, measure, *, source_group_ids=(): order.append("write") or 3)
    out = runs.run_derive(env.db, run, derive)
    assert order == ["prepare", "measure", "write"]
    assert out["measure_version"] == 3 and out["policy_version"] is None
    assert out["warnings"][0].startswith("scope_prepare_failed")


# ---------------------------------------------------------------------------
# The post-run chain
# ---------------------------------------------------------------------------


def _done_run(env):
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    run = set_status(env, run, "done")
    env.db.add("plans_reader_outputs", run_id=run["id"], scope="main", category=None, version=1,
               output=main_output_spec(doc["id"]), source_group_ids=[])
    return run, doc


def test_the_pricing_job_holds_the_change_slot_and_never_changes_the_run_status(env, monkeypatch):
    from app.services import plans_reader_autonomy, pricing_run

    env.settings = make_settings(**SPINE_ON)
    run, _doc = _done_run(env)
    seen: dict = {}

    def execute_job(payload):
        active = [g for g in env.db.rows("plans_reader_groups", run_id=run["id"], kind="modify") if g["status"] == "running"]
        seen["slot"] = active[0]["output"] if active else None
        seen["status_during"] = _run(env, run)["status"]
        # A second change while the job runs is refused (one change per run at a time).
        with pytest.raises(runs.RunRefused):
            runs.claim_change_slot(env.db, run["id"], output={"kind": "boq"})

    monkeypatch.setattr(pricing_run, "execute_job", execute_job)
    recomputed: list = []
    monkeypatch.setattr(plans_reader_autonomy, "recompute", lambda sb, run_id, **kw: recomputed.append((run_id, kw.get("chain_active"))) or "ready_for_verification")
    runs.run_pricing_job({"run_id": run["id"], "reason": "finish"})
    assert seen == {"slot": {"kind": "ai_pricing", "post_run": True}, "status_during": "done"}
    assert _run(env, run)["status"] == "done"
    slot = env.db.rows("plans_reader_groups", run_id=run["id"], kind="modify")[0]
    assert slot["status"] == "done" and slot["output"]["done"] is True
    assert recomputed == [(run["id"], False)]


def test_the_pricing_job_waits_for_a_busy_change_slot_and_records_a_failure(env, monkeypatch):
    from app.services import pricing_run

    env.settings = make_settings(**SPINE_ON)
    run, _doc = _done_run(env)
    held = runs.claim_change_slot(env.db, run["id"], output={"kind": "boq"})
    with pytest.raises(RuntimeError, match="waiting for another change"):
        runs.run_pricing_job({"run_id": run["id"]})
    env.db.table("plans_reader_groups").update({"status": "done"}).eq("id", held["id"]).execute()

    def boom(payload):
        raise pricing_run.PricingRefused("This run has no BOQ deliverable to price.")

    monkeypatch.setattr(pricing_run, "execute_job", boom)
    with pytest.raises(pricing_run.PricingRefused):
        runs.run_pricing_job({"run_id": run["id"]})
    slots = env.db.rows("plans_reader_groups", run_id=run["id"], kind="modify")
    assert [s["status"] for s in slots] == ["done", "failed"] and "no BOQ" in slots[1]["error"]
    assert _run(env, run)["status"] == "done"


def test_autonomy_is_recomputed_after_a_modify_and_a_new_boq_version_reprices(env, monkeypatch, renders):
    from app.services import plans_reader_autonomy, pricing_run

    env.settings = make_settings(**SPINE_ON)
    run, doc = _done_run(env)
    log: list = []
    monkeypatch.setattr(plans_reader_autonomy, "recompute", lambda sb, run_id, **kw: log.append("autonomy") or "needs_review")
    monkeypatch.setattr(pricing_run, "on_new_boq_version", lambda sb, run_id, v, **kw: log.append(f"reprice:{v}"))
    row = runs.rerender_now(env.db, run, "boq", user=user())
    assert row["kind"] == "boq" and row["version"] == 1
    assert log == ["reprice:1", "autonomy"]
    log.clear()
    runs.rerender_now(env.db, run, "brief", user=user())
    assert log == ["autonomy"]  # not a BOQ: no re-price


def test_a_modify_marks_its_spec_edits_only_with_the_policy_enforced(env, monkeypatch, renders):
    from app.services import scope_engine

    run, doc = _done_run(env)
    fake = FakeModel()
    monkeypatch.setattr(llm, "complete_parts", fake)
    spec = main_output_spec(doc["id"])["boq"]
    fake.answer = spec
    marked: list = []
    monkeypatch.setattr(scope_engine, "mark_spec_edits", lambda boq, prev: marked.append((boq is not None, prev is not None)) or 0)
    agents.modify_deliverable(env.db, run, kind="boq", instruction="Add a receptacle", user=user())
    assert marked == []  # flag off: specs stay byte-identical
    env.settings = make_settings(ai_scope_policy_enforce=True)
    agents.modify_deliverable(env.db, run, kind="boq", instruction="Add another receptacle", user=user())
    assert marked == [(True, True)]


# ---------------------------------------------------------------------------
# Flags off: byte-identical requests
# ---------------------------------------------------------------------------


def test_t_int_1_fingerprint_holds_with_the_spine_in_place(env, monkeypatch):
    from app.services.pagefacts import api

    from tests.test_pagefacts_integration import PRE_FACTS_FINGERPRINT, _fingerprint, _plan_and_build, _seed_pair

    assert env.settings.plans_reader_page_facts_enabled is False and not runs.spine_enabled(env.settings)
    api.clear_caches()
    try:
        run, _d, _s = _seed_pair(env)
        stored, requests = _plan_and_build(env, run)
        assert _fingerprint(stored, requests) == PRE_FACTS_FINGERPRINT
    finally:
        api.clear_caches()


def test_legacy_main_request_reads_the_legacy_view_of_the_v2_output(env, monkeypatch):
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    sub = _sub(env, run, doc)
    runs.advance(env.db, run["id"])  # stores the v2 category output
    stored = env.db.rows("plans_reader_outputs", run_id=run["id"], scope="category")[0]["output"]
    assert stored["rows_version"] == 2
    main = _groups(env, run, "main")[0]
    fresh = _run(env, run)
    builder, _doc_map = agents._build_main(env.db, fresh, main, env.settings)
    roles = [m.get("role") for m in builder.manifest]
    assert "scope_digest" not in roles and "measure_summary" not in roles
    v1, _sources = runs._category_output("electrical_drawings", [sub])
    _index_to_doc, doc_to_index = agents._category_doc_map([doc])
    expected = "CATEGORY OUTPUT: electrical_drawings (Electrical Drawings) (JSON, data):\n" + agents._json(
        agents.reindex_docs(v1, doc_to_index)
    )
    assert [p["text"] for p in builder.parts if p["type"] == "input_text"][3] == expected
    assert agents._system_for(main, env.settings) == agents.MAIN_SYSTEM
    assert agents.prompt_tokens("main", env.settings) == agents.prompt_tokens("main")


def test_main_prompt_variants_select_by_flag():
    assert agents.main_system_for(make_settings()) is agents.MAIN_SYSTEM
    scope = agents.main_system_for(make_settings(ai_scope_policy_enforce=True))
    assert "G3 SCOPE DECISIONS are binding" in scope and "never provisional breakers" in scope
    assert "Never output prices" in scope  # the legacy pricing clause stays on the scope-only variant
    measure = agents.main_system_for(make_settings(plans_reader_derive_enabled=True), has_measure=True)
    assert "come from the app's measure output (M rows)" in measure and "the engine prices from your quantities" in measure
    assert agents.main_system_for(make_settings(plans_reader_derive_enabled=True), has_measure=False) is agents.MAIN_SYSTEM
    both = agents.main_system_for(make_settings(ai_scope_policy_enforce=True, plans_reader_derive_enabled=True), has_measure=True)
    assert "G3 SCOPE DECISIONS are binding" in both and "(M rows)" in both
    for text in (scope, measure, both):
        assert all(ord(c) < 128 for c in text) and chr(0x2014) not in text and chr(0x2013) not in text


def test_legacy_main_makes_one_single_call_and_never_runs_the_loop(env, monkeypatch):
    from app.services import plans_reader_toolloop as toolloop

    fake = FakeModel()
    monkeypatch.setattr(llm, "complete_parts", fake)
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: None)
    monkeypatch.setattr(toolloop, "run_loop", lambda ctx: (_ for _ in ()).throw(AssertionError("the loop ran")))
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    run = set_status(env, run, "consolidating")
    env.db.add("plans_reader_outputs", run_id=run["id"], scope="category", category="electrical_drawings",
               version=1, output=sub_output("electrical_drawings", [(1, 1)]), source_group_ids=[])
    main = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="queued", pages=[], est_input_tokens=5000)
    fake.answer = main_output_spec(doc["id"])
    assert env.settings.plans_reader_consolidator_mode == "legacy"
    agents.run_group(env.db, main["id"])
    assert len(fake.calls) == 1
    assert fake.calls[0]["schema"] is MAIN_AGENT_SCHEMA and fake.calls[0]["system"] == agents.MAIN_SYSTEM
    assert fake.calls[0]["schema_name"] == "plans_main_agent"
    assert _groups(env, run, "main")[0]["status"] == "done"


# ---------------------------------------------------------------------------
# Review kinds end partial; recount and main in tools mode pause the run
# ---------------------------------------------------------------------------


def _review_group(env, run, kind="review"):
    return env.db.add("plans_reader_groups", run_id=run["id"], kind=kind, status="queued", pages=[],
                      est_input_tokens=0, mode="review", route={"provider": "openai"}, loop_state={})


@pytest.fixture
def review_seam(monkeypatch):
    """WS7's partial finalizer seam and the loop seams for review and revise."""
    module = types.ModuleType("app.services.plans_reader_review")
    module.partial = []
    module.finish_partial = lambda sb, run, *, group, reason: module.partial.append((group["id"], reason))
    monkeypatch.setitem(sys.modules, "app.services.plans_reader_review", module)
    from app.services import plans_reader_tools

    monkeypatch.setattr(runs, "_run_services", lambda sb, run_id, document_rows, *, inputs=None: plans_reader_tools.fake_services())
    for kind in ("review", "revise", "main"):
        monkeypatch.setitem(agents._LOOP_SYSTEMS, kind, f"{kind.upper()} SYSTEM")
        monkeypatch.setitem(agents._LOOP_BUILDERS, kind, lambda sb, run, group, s: ([], []))
        monkeypatch.setitem(agents._LOOP_FINALIZERS, kind, lambda parsed, result, **kw: {"parsed": parsed})
    return module


@pytest.mark.parametrize("kind", ["review", "revise"])
def test_a_review_kind_over_the_daily_cap_ends_partial_and_leaves_the_run_done(env, monkeypatch, review_seam, kind):
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    advanced: list = []
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: advanced.append(run_id))
    run, _doc = _done_run(env)
    group = _review_group(env, run, kind)
    env.cap_error = "The AI Estimating daily spend cap is reached."
    agents.run_group(env.db, group["id"])
    settled = env.db.rows("plans_reader_groups", id=group["id"])[0]
    assert settled["status"] == "failed" and "spend cap" in settled["error"]
    assert review_seam.partial == [(group["id"], env.cap_error)]
    assert _run(env, run)["status"] == "done" and advanced == [run["id"]]


@pytest.mark.parametrize("kind", ["review", "revise"])
@pytest.mark.parametrize("failure", ["cap", "loop"])
def test_a_review_kind_stopped_inside_the_loop_ends_partial(env, monkeypatch, review_seam, kind, failure):
    from app.services import plans_reader_toolloop as toolloop

    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    advanced: list = []
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: advanced.append(run_id))
    run, _doc = _done_run(env)
    group = _review_group(env, run, kind)

    def run_loop(ctx):
        assert ctx.kind == kind and ctx.group_id == group["id"] and ctx.system == f"{kind.upper()} SYSTEM"
        if failure == "cap":
            raise ai_spend.SpendCapReached("The daily cap is reached.", cap_usd=Decimal("75"), spent_usd=Decimal("75"))
        raise toolloop.LoopFailed("The review ran out of budget before a final answer.", reason="budget")

    monkeypatch.setattr(toolloop, "run_loop", run_loop)
    agents.run_group(env.db, group["id"])
    settled = env.db.rows("plans_reader_groups", id=group["id"])[0]
    assert settled["status"] == "failed" and len(review_seam.partial) == 1
    assert _run(env, run)["status"] == "done" and advanced == [run["id"]]


def test_a_review_kind_dispatched_before_its_module_lands_ends_partial(env, monkeypatch):
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: None)
    monkeypatch.setitem(sys.modules, "app.services.plans_reader_reviewer", None)  # import fails
    monkeypatch.setitem(sys.modules, "app.services.plans_reader_review", None)
    run, _doc = _done_run(env)
    group = _review_group(env, run, "review")
    agents.run_group(env.db, group["id"])
    settled = env.db.rows("plans_reader_groups", id=group["id"])[0]
    assert settled["status"] == "failed" and "not available" in settled["error"]
    assert _run(env, run)["status"] == "done"


def test_recount_over_the_cap_pauses_the_run(env, monkeypatch, review_seam):
    env.settings = make_settings(**SPINE_ON)
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    group = env.db.add("plans_reader_groups", run_id=run["id"], kind="recount", category="electrical_drawings",
                       status="queued", **{"pass": 3}, pages=[{"document_id": doc["id"], "page": 1, "kind": "image"}],
                       est_input_tokens=500, loop_state={})

    def boom(sb, run, group, *, settings=None):
        raise ai_spend.SpendCapReached("The daily cap is reached.", cap_usd=Decimal("75"), spent_usd=Decimal("75"))

    monkeypatch.setattr(runs, "run_recount_group", boom)
    agents.run_group(env.db, group["id"])
    assert env.db.rows("plans_reader_groups", id=group["id"])[0]["status"] == "queued"
    assert _run(env, run)["status"] == "over_cap"


def test_main_in_tools_mode_over_the_cap_pauses_the_run(env, monkeypatch, review_seam):
    from app.services import plans_reader_toolloop as toolloop

    env.settings = make_settings(**SPINE_ON, plans_reader_consolidator_mode="tools")
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    run = set_status(env, run, "consolidating")
    main = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="queued", pages=[], est_input_tokens=5000)
    seen: dict = {}

    def run_loop(ctx):
        seen["schema_name"] = ctx.schema_name
        seen["budget"] = ctx.budget.max_usd
        raise ai_spend.SpendCapReached("The daily cap is reached.", cap_usd=Decimal("75"), spent_usd=Decimal("75"))

    monkeypatch.setattr(toolloop, "run_loop", run_loop)
    agents.run_group(env.db, main["id"])
    assert seen == {"schema_name": "plans_main_tools", "budget": Decimal("6.00")}
    assert env.db.rows("plans_reader_groups", id=main["id"])[0]["status"] == "queued"
    assert _run(env, run)["status"] == "over_cap"


def test_main_in_tools_mode_settles_the_finalizers_output(env, monkeypatch, review_seam):
    from app.services import plans_reader_toolloop as toolloop

    env.settings = make_settings(**SPINE_ON, plans_reader_consolidator_mode="tools")
    advanced: list = []
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: advanced.append(run_id))
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    run = set_status(env, run, "consolidating")
    main = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="queued", pages=[], est_input_tokens=5000)
    result = SimpleNamespace(parsed={"brief": {}}, as_dict=lambda: {"status": "done", "rounds": 3, "spent_usd": 1.25})
    monkeypatch.setattr(toolloop, "run_loop", lambda ctx: result)
    agents.run_group(env.db, main["id"])
    settled = env.db.rows("plans_reader_groups", id=main["id"])[0]
    assert settled["status"] == "done" and settled["output"] == {"parsed": {"brief": {}}}
    assert settled["loop_state"]["rounds"] == 3 and advanced == [run["id"]]


# ---------------------------------------------------------------------------
# Run creation: the cost ceiling and the eval case
# ---------------------------------------------------------------------------


def test_create_run_writes_the_loop_budgets_and_the_eval_case_only_when_relevant(env):
    run = runs.create_run(env.db, name="Flags off", source="upload", project_id=None, render_mode="adaptive", user=user())
    assert "estimated_cost_usd_max" not in run and "eval_case_id" not in run
    assert set(run["settings_snapshot"]["conventions"]) >= {"version", "branch_conduit", "route_factor"}
    assert run["settings_snapshot"]["plans_reader_page_facts_enabled"] is False
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply", plans_reader_consolidator_mode="tools")
    run = runs.create_run(env.db, name="Flags on", source="upload", project_id=None, render_mode="adaptive",
                          user=user(), eval_case_id="7154")
    expected = runs.loop_budget_usd_max(env.settings)
    assert expected > 0 and Decimal(str(run["estimated_cost_usd_max"])) == expected
    assert run["eval_case_id"] == "7154"
    # recount 0.80 x 6 sheets + main 6.00 + review 1.50 + revise 1.00
    assert expected == Decimal("1.50") * 6 + Decimal("6.00") + Decimal("1.50") + Decimal("1.00")


# ---------------------------------------------------------------------------
# The 0165 job type CHECK
# ---------------------------------------------------------------------------


def _migration(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "supabase/migrations" / name).read_text()


def test_the_0165_check_list_equals_all_job_types():
    base = re.search(r"llm_jobs_job_type_check\s+check \(job_type in \(([^)]*)\)\)", _migration("0152_plans_reader.sql"))
    added = re.search(r"v_add\s+constant text\[\] := array\[([^\]]*)\]", _migration("0165_plans_reader_autonomy_core.sql"))
    assert base and added
    values = set(re.findall(r"'([a-z_]+)'", base.group(1))) | set(re.findall(r"'([a-z_]+)'", added.group(1)))
    assert values == set(llm_queue.ALL_JOB_TYPES)
    assert llm_queue.JOB_AI_PRICING in llm_queue.ALL_JOB_TYPES and llm_queue.JOB_PLANS_FACTS in llm_queue.PLANS_INTERACTIVE_JOB_TYPES
    assert llm_queue._spec(llm_queue.JOB_AI_PRICING).feature == "pricing_quote"
    assert llm_queue.PENDING_MC_JOB_TYPES == ()


# ---------------------------------------------------------------------------
# Wave 3 fix pass (group A1): the post-run chain hooks (runs.advance on a
# done run, after_deliverable_change), the measure assist behind its flag,
# the evidence-backed marked plans spec, the consolidator stop listener,
# the code fallback of a failed main loop and the pricing-tools gate
# ---------------------------------------------------------------------------


def _loop_seams(monkeypatch, kinds=("review", "revise", "main"), finalizer=None):
    """The loop seams without the fake review module of `review_seam`, so
    the real plans_reader_review chain runs."""
    from app.services import plans_reader_tools

    monkeypatch.setattr(runs, "_run_services", lambda sb, run_id, document_rows, *, inputs=None: plans_reader_tools.fake_services())
    for kind in kinds:
        monkeypatch.setitem(agents._LOOP_SYSTEMS, kind, f"{kind.upper()} SYSTEM")
        monkeypatch.setitem(agents._LOOP_BUILDERS, kind, lambda sb, run, group, s: ([], []))
        monkeypatch.setitem(agents._LOOP_FINALIZERS, kind, finalizer or (lambda parsed, result, **kw: {"parsed": parsed}))


def _consolidating_main(env, **settings):
    env.settings = make_settings(**SPINE_ON, plans_reader_consolidator_mode="tools", **settings)
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    run = set_status(env, run, "consolidating")
    main = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="queued", pages=[], est_input_tokens=5000)
    return run, main


def test_advance_on_a_done_run_moves_the_review_chain_only_with_the_review_mode_on(env, monkeypatch):
    from app.services import plans_reader_review as chain

    run, _doc = _done_run(env)
    seen: list = []
    monkeypatch.setattr(chain, "advance", lambda sb, r, **kw: seen.append((sb is env.db, r["id"], r["status"])))
    runs.advance(env.db, run["id"])  # review mode off (the default): the chain is never asked
    assert seen == []
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    runs.advance(env.db, run["id"])
    assert seen == [(True, run["id"], "done")]
    set_status(env, run, "done_with_errors")
    runs.advance(env.db, run["id"])
    assert seen[-1] == (True, run["id"], "done_with_errors")

    def broken(sb, r, **kw):
        raise RuntimeError("chain broke")

    monkeypatch.setattr(chain, "advance", broken)
    runs.advance(env.db, run["id"])  # logged, never raised
    assert _run(env, run)["status"] == "done_with_errors"


def test_a_review_group_settled_done_on_a_done_run_queues_the_revise_group(env, monkeypatch):
    """End to end through the real chain: run_group settles the review
    group done and calls runs.advance on the done run, which hands
    plans_reader_review.advance its step: the reviewer's output is
    recorded and the revise group is inserted and enqueued."""
    from app.services import plans_reader_review as chain
    from app.services import plans_reader_toolloop as toolloop
    from app.services.plans_reader_review_rules import ReviewFinding, ReviewInputs

    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    run, _doc = _done_run(env)
    env.db.add("plans_reader_pricing", run_id=run["id"], version=1, boq_version=1, status="priced",
               instruction="finish", stale=False, context={})
    review = env.db.add(chain.REVIEWS_TABLE, run_id=run["id"], stage="review", mode="apply", base_pricing_version=1,
                        verdict=None, reasons=[], result={}, models={}, cost_usd=0)
    finding = ReviewFinding(origin="rule", rule_id="R1", kind="missing_scope", severity="critical",
                            title="New devices with no branch wire", line_ids=["B1"], finding_id="RF1")
    env.db.add(chain.FINDINGS_TABLE, **finding.to_row(review_id=review["id"], run_id=run["id"]))
    group = env.db.add("plans_reader_groups", run_id=run["id"], kind="review", status="queued", pages=[], est_input_tokens=0,
                       mode="review", route={"provider": "openai"}, loop_state={"review": {"review_id": review["id"]}})
    env.db.table(chain.REVIEWS_TABLE).update(
        {"result": {"stages": {"review": {"status": "queued", "group_id": group["id"]}}}}
    ).eq("id", review["id"]).execute()
    monkeypatch.setattr(chain, "load_inputs", lambda sb, r, pricing_row, *, settings=None: chain.LoadedInputs(
        inputs=ReviewInputs(run_id=str(r["id"])), boq_version=1))
    answer = {"summary": "Checked.", "systems_checked": [], "findings": []}
    _loop_seams(monkeypatch, finalizer=lambda parsed, result, **kw: {**answer, "review_id": review["id"], "cost_usd": 0.3, "model": "scripted"})
    result = SimpleNamespace(parsed=answer, text="", as_dict=lambda: {"status": "done", "rounds": 2, "spent_usd": 0.3})
    monkeypatch.setattr(toolloop, "run_loop", lambda ctx: result)
    agents.run_group(env.db, group["id"])
    assert env.db.rows("plans_reader_groups", id=group["id"])[0]["status"] == "done"
    assert _run(env, run)["status"] == "done"
    revises = _groups(env, run, "revise")
    assert len(revises) == 1 and revises[0]["status"] == "queued"
    assert revises[0]["loop_state"]["revise"]["review_id"] == review["id"]
    assert env.enqueued[-1]["target_id"] == revises[0]["id"]
    row = env.db.rows(chain.REVIEWS_TABLE, id=review["id"])[0]
    assert row["stage"] == "revise" and row["result"]["stages"]["review"]["status"] == "done"


def test_a_new_boq_version_tells_the_review_chain_and_skips_the_reprice_of_its_own_revision(env, monkeypatch):
    from app.services import plans_reader_autonomy, pricing_run
    from app.services import plans_reader_review as chain

    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    run, _doc = _done_run(env)
    log: list = []
    monkeypatch.setattr(plans_reader_autonomy, "recompute", lambda sb, run_id, **kw: log.append("autonomy") or "needs_review")
    monkeypatch.setattr(pricing_run, "on_new_boq_version", lambda sb, run_id, v, **kw: log.append(f"reprice:{v}"))
    monkeypatch.setattr(chain, "on_boq_changed", lambda sb, run_id, *, instruction=None, user=None: log.append(f"stale:{instruction}:{user}") or 1)
    runs.after_deliverable_change(env.db, run, {"kind": "boq", "version": 2, "instruction": "Add a receptacle", "created_by": "u1"})
    assert log == ["stale:Add a receptacle:u1", "reprice:2", "autonomy"]
    log.clear()
    # The chain's own revision: the stale hook sees it (and ignores it), the
    # generic re-price is skipped (the chain queued its own with reason review_revision).
    runs.after_deliverable_change(env.db, run, {"kind": "boq", "version": 3, "instruction": chain.REVISED_INSTRUCTION, "created_by": None})
    assert log == [f"stale:{chain.REVISED_INSTRUCTION}:None", "autonomy"]
    log.clear()
    runs.after_deliverable_change(env.db, run, {"kind": "brief", "version": 2})
    assert log == ["autonomy"]
    log.clear()

    def broken(sb, run_id, *, instruction=None, user=None):
        raise RuntimeError("reviews table missing")

    monkeypatch.setattr(chain, "on_boq_changed", broken)
    runs.after_deliverable_change(env.db, run, {"kind": "boq", "version": 4, "instruction": "edit"})
    assert log == ["reprice:4", "autonomy"]  # the hook never blocks the re-price
    env.settings = make_settings(**SPINE_ON)  # review mode off: the hook is not called
    log.clear()
    monkeypatch.setattr(chain, "on_boq_changed", lambda *a, **k: log.append("stale"))
    runs.after_deliverable_change(env.db, run, {"kind": "boq", "version": 5, "instruction": "edit"})
    assert log == ["reprice:5", "autonomy"]


def test_the_measure_assist_runs_after_the_code_measure_only_with_its_flag(env, monkeypatch):
    from app.services import plans_reader_measure, scope_engine
    from app.services import plans_reader_measure_assist as assist_mod

    env.settings = make_settings(**SPINE_ON)
    run, _doc, derive = _consolidating_with_derive(env)
    monkeypatch.setattr(scope_engine, "prepare", lambda sb, run, outputs=None, **kw: None)
    monkeypatch.setattr(plans_reader_measure, "build_measure_safe", lambda inputs, conv=None: {"version": 1, "quantities": []})
    monkeypatch.setattr(plans_reader_measure.MeasureService, "register_run", lambda self, run_id, measure, *, source_group_ids=(): 3)
    seen: list = []

    def run_assist(run_id, **kw):
        seen.append((run_id, kw))
        return assist_mod.AssistResult(status="done", calls=2, spent_usd=0.12, overrides=1, version=4)

    monkeypatch.setattr(assist_mod, "run_assist", run_assist)
    with monkeypatch.context() as m:
        # Flag off: the module is never imported (an import here would fail).
        m.setitem(sys.modules, "app.services.plans_reader_measure_assist", None)
        out = runs.run_derive(env.db, run, derive)
    assert seen == [] and out["measure_version"] == 3 and "assist" not in out and out["warnings"] == []
    env.settings = make_settings(**SPINE_ON, plans_reader_measure_assist_enabled=True)
    out = runs.run_derive(env.db, run, derive)
    assert out["measure_version"] == 4 and out["assist"]["status"] == "done" and out["assist"]["version"] == 4
    run_id, kw = seen[0]
    assert run_id == run["id"] and kw["source_group_ids"] == [derive["id"]] and kw["settings"] is env.settings
    assert isinstance(kw["svc"], plans_reader_measure.MeasureService) and callable(kw["record_call"]) and callable(kw["heartbeat"])
    assert kw["route_snapshot"] == run["settings_snapshot"]["routes"]["derive"] and isinstance(kw["pdfs"], dict)

    def failing(run_id, **kw):
        raise RuntimeError("vision down")

    monkeypatch.setattr(assist_mod, "run_assist", failing)
    out = runs.run_derive(env.db, run, derive)  # the code version stands
    assert out["measure_version"] == 3 and out["assist"]["status"] == "error" and "vision down" in out["assist"]["message"]
    assert out["warnings"] == []

    def lease_lost(run_id, **kw):
        raise llm.LlmLeaseLost("The job lost its lease.")

    monkeypatch.setattr(assist_mod, "run_assist", lease_lost)
    with pytest.raises(llm.LlmLeaseLost):
        runs.run_derive(env.db, run, derive)
    with pytest.raises(llm.LlmLeaseLost):
        agents._run_derive(env.db, run, derive)  # the group wrapper lets the queue decide too


def test_marked_plans_spec_comes_from_the_stored_evidence_when_a_client_is_given(env, monkeypatch):
    from app.services import plans_reader_render_evidence as render_evidence

    env.settings = make_settings(**SPINE_ON)
    run, doc = _done_run(env)
    main = runs.main_output(env.db, run["id"])
    from_main = runs.deliverable_spec("marked_plans", main)
    assert from_main == {"sheets": main["marked_plans"]["sheets"]}
    evidence_spec = {"sheets": [{"document_id": doc["id"], "page": 1, "marks": [], "from_evidence": True}]}
    calls: list = []

    def from_evidence(kind, sb, r):
        calls.append((kind, sb is env.db, r["id"]))
        return evidence_spec if kind == "marked_plans" else None

    monkeypatch.setattr(render_evidence, "deliverable_spec_from_evidence", from_evidence)
    assert runs.deliverable_spec("marked_plans", main) == from_main and calls == []  # no client: the main part
    assert runs.deliverable_spec("marked_plans", main, env.db, run) == evidence_spec
    assert runs.deliverable_spec("boq", main, env.db, run) == main["boq"]
    assert calls == [("marked_plans", True, run["id"])]
    spec, version, current = runs._rerender_source(env.db, run, "marked_plans")
    assert spec == evidence_spec and (version, current) == (1, 0)
    monkeypatch.setattr(render_evidence, "deliverable_spec_from_evidence", lambda kind, sb, r: None)
    assert runs.deliverable_spec("marked_plans", main, env.db, run) == from_main  # evidence silent: the main part

    def broken(kind, sb, r):
        raise RuntimeError("no evidence table")

    monkeypatch.setattr(render_evidence, "deliverable_spec_from_evidence", broken)
    assert runs.deliverable_spec("marked_plans", main, env.db, run) == from_main


def test_a_modify_asks_for_the_current_spec_without_the_client_or_the_run(env, monkeypatch, renders):
    """K2 (spec WS8): the modify path never passes a client, so the
    evidence-derived marked plans spec can never reach a model."""
    run, doc = _done_run(env)
    fake = FakeModel()
    monkeypatch.setattr(llm, "complete_parts", fake)
    fake.answer = main_output_spec(doc["id"])["boq"]
    seen: list = []
    real = runs.deliverable_spec
    monkeypatch.setattr(runs, "deliverable_spec", lambda kind, main, sb=None, run=None: seen.append(
        (kind, sb is env.db, (run or {}).get("id"))) or real(kind, main, sb, run))
    agents.modify_deliverable(env.db, run, kind="boq", instruction="Add a receptacle", user=user())
    assert seen == [("boq", False, None)]


def _marked_version(env, run, spec, version=1):
    return env.db.add("plans_reader_deliverables", run_id=run["id"], kind="marked_plans", version=version,
                      filename="marked.pdf", spec_json=spec)


def test_a_rerender_of_marked_plans_after_a_version_redraws_from_the_evidence_in_code_mode(env, monkeypatch):
    from app.services import plans_reader_render_evidence as render_evidence

    env.settings = make_settings(**SPINE_ON)
    run, doc = _done_run(env)
    old_spec = {"sheets": [{"document_id": doc["id"], "page": 1, "marks": [], "old": True}]}
    _marked_version(env, run, old_spec)
    evidence_spec = {"sheets": [{"document_id": doc["id"], "page": 1, "marks": [], "from_evidence": True}]}
    calls: list = []

    def from_evidence(kind, sb, r):
        calls.append(kind)
        return evidence_spec

    monkeypatch.setattr(render_evidence, "deliverable_spec_from_evidence", from_evidence)
    rendered: list = []
    monkeypatch.setattr(runs, "render_deliverable", lambda sb, r, kind, spec, *, version, user=None, **_k: rendered.append(
        (kind, spec, version)) or {"kind": kind, "version": version})
    monkeypatch.setattr(runs, "after_deliverable_change", lambda sb, r, row, *, kind: None)
    runs.rerender_now(env.db, run, "marked_plans", user=user())
    assert rendered == [("marked_plans", evidence_spec, 2)] and calls == ["marked_plans"]
    # The evidence has nothing: the latest version's spec is the fallback.
    monkeypatch.setattr(render_evidence, "deliverable_spec_from_evidence", lambda kind, sb, r: None)
    spec, version, current = runs._rerender_source(env.db, run, "marked_plans")
    assert spec == old_spec and (version, current) == (2, 1)


def test_a_rerender_of_marked_plans_in_model_mode_keeps_the_latest_spec_and_never_asks_the_evidence(env, monkeypatch):
    from app.services import plans_reader_render_evidence as render_evidence

    env.settings = make_settings(**{**SPINE_ON, "plans_reader_marked_plans_mode": "model"})
    run, doc = _done_run(env)
    old_spec = {"sheets": [{"document_id": doc["id"], "page": 1, "marks": [], "old": True}]}
    _marked_version(env, run, old_spec)
    calls: list = []
    monkeypatch.setattr(render_evidence, "deliverable_spec_from_evidence", lambda kind, sb, r: calls.append(kind))
    spec, version, current = runs._rerender_source(env.db, run, "marked_plans")
    assert spec == old_spec and (version, current) == (2, 1) and calls == []
    # Other kinds in code mode never ask the evidence either.
    env.settings = make_settings(**SPINE_ON)
    spec, version, current = runs._rerender_source(env.db, run, "boq")
    assert spec == runs.main_output(env.db, run["id"])["boq"] and (version, current) == (1, 0) and calls == []


def test_a_modify_of_marked_plans_in_code_mode_refuses_without_a_model_call(env, monkeypatch, renders):
    env.settings = make_settings(**SPINE_ON)
    run, _doc = _done_run(env)
    _marked_version(env, run, {"sheets": []})
    fake = FakeModel()
    monkeypatch.setattr(llm, "complete_parts", fake)
    seen: list = []
    monkeypatch.setattr(runs, "deliverable_spec", lambda *a, **k: seen.append(a))
    with pytest.raises(ValueError, match="Marked plans are drawn from the evidence; re-render instead"):
        agents.modify_deliverable(env.db, run, kind="marked_plans", instruction="Circle the panels", user=user())
    assert fake.calls == [] and seen == []
    assert env.db.rows("plans_reader_calls", run_id=run["id"]) == []
    assert [d["version"] for d in env.db.rows("plans_reader_deliverables", run_id=run["id"])] == [1]


def test_a_modify_of_marked_plans_in_model_mode_still_calls_the_model_with_the_latest_spec(env, monkeypatch, renders):
    env.settings = make_settings(**{**SPINE_ON, "plans_reader_marked_plans_mode": "model"})
    run, doc = _done_run(env)
    latest = {"sheets": [{"document_id": doc["id"], "page": 1, "marks": [], "old": True}]}
    _marked_version(env, run, latest)
    fake = FakeModel()
    monkeypatch.setattr(llm, "complete_parts", fake)
    fake.answer = {"sheets": []}
    rendered: list = []
    monkeypatch.setattr(runs, "render_deliverable", lambda sb, r, kind, spec, *, version, **_k: rendered.append(
        (kind, version)) or {"kind": kind, "version": version})
    monkeypatch.setattr(runs, "after_deliverable_change", lambda sb, r, row, *, kind: None)
    agents.modify_deliverable(env.db, run, kind="marked_plans", instruction="Circle the panels", user=user())
    assert len(fake.calls) == 1 and fake.calls[0]["feature"] == "plans_modify"
    current = [p["text"] for p in fake.calls[0]["parts"] if "CURRENT MARKED_PLANS SPEC" in p.get("text", "")]
    assert len(current) == 1 and '"old":true' in current[0]
    assert rendered == [("marked_plans", 2)]


# ---------------------------------------------------------------------------
# K3: the poll's safety net for the post-run review chain
# ---------------------------------------------------------------------------


def _age(env, group, minutes=10):
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    for row in env.db.tables["plans_reader_groups"]:
        if row["id"] == group["id"]:
            row["created_at"] = old


@pytest.mark.parametrize("status", ["done", "done_with_errors"])
def test_the_poll_requeues_an_orphan_review_group_on_a_done_run(env, monkeypatch, status):
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    run, _doc = _done_run(env)
    run = set_status(env, run, status)
    kicks: list = []
    monkeypatch.setattr(runs, "_kick_advance", lambda run_id, force=False: kicks.append(run_id))
    lost = _review_group(env, run, "review")
    _age(env, lost)
    runs._REQUEUE_CHECKED.clear()
    runs.get_run_payload(env.db, run["id"])
    assert [j["target_id"] for j in env.enqueued] == [lost["id"]]
    assert env.enqueued[0]["job_type"] == runs.JOB_AGENT
    assert env.db.rows("plans_reader_groups", id=lost["id"])[0]["llm_job_id"] == env.enqueued[0]["id"]
    assert kicks == []  # a review group is active: the chain waits for it
    env.db.add("llm_jobs", id=env.enqueued[0]["id"], status="queued")
    runs._REQUEUE_CHECKED.clear()
    runs.get_run_payload(env.db, run["id"])  # its job is live now: nothing more
    assert len(env.enqueued) == 1 and _run(env, run)["status"] == status


def test_the_poll_leaves_a_live_or_young_review_group_alone(env, monkeypatch):
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    run, _doc = _done_run(env)
    kicks: list = []
    monkeypatch.setattr(runs, "_kick_advance", lambda run_id, force=False: kicks.append(run_id))
    live = _review_group(env, run, "revise")
    _age(env, live)
    env.db.table("plans_reader_groups").update({"llm_job_id": "job-live"}).eq("id", live["id"]).execute()
    env.db.add("llm_jobs", id="job-live", status="running")
    _review_group(env, run, "review")  # young: its stage winner is enqueuing it
    runs._REQUEUE_CHECKED.clear()
    runs.get_run_payload(env.db, run["id"])
    assert env.enqueued == [] and kicks == []


def test_the_poll_kicks_the_review_chain_when_no_review_group_is_active(env, monkeypatch):
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    run, _doc = _done_run(env)
    kicks: list = []
    monkeypatch.setattr(runs, "_kick_advance", lambda run_id, force=False: kicks.append(run_id))
    runs.get_run_payload(env.db, run["id"])  # no review group yet
    assert kicks == [run["id"]]
    settled = _review_group(env, run, "review")
    env.db.table("plans_reader_groups").update({"status": "done"}).eq("id", settled["id"]).execute()
    runs.get_run_payload(env.db, run["id"])
    assert kicks == [run["id"], run["id"]] and env.enqueued == []


def test_the_kick_on_a_done_run_reaches_the_review_chain(env, monkeypatch):
    """The kick runs advance, which hands the done run to
    plans_reader_review.advance (install_env's inline kick)."""
    from app.services import plans_reader_review as chain

    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    run, _doc = _done_run(env)
    seen: list = []
    monkeypatch.setattr(chain, "advance", lambda sb, r, **kw: seen.append(r["id"]))
    runs.get_run_payload(env.db, run["id"])
    assert seen == [run["id"]]


def test_with_the_review_mode_off_the_poll_does_nothing_for_a_done_run(env, monkeypatch):
    run, _doc = _done_run(env)  # review mode off (the default)
    kicks: list = []
    monkeypatch.setattr(runs, "_kick_advance", lambda run_id, force=False: kicks.append(run_id))
    lost = _review_group(env, run, "review")
    _age(env, lost)
    runs._REQUEUE_CHECKED.clear()
    runs.get_run_payload(env.db, run["id"])
    assert env.enqueued == [] and kicks == []
    env.db.table("plans_reader_groups").update({"status": "done"}).eq("id", lost["id"]).execute()
    runs.get_run_payload(env.db, run["id"])
    assert env.enqueued == [] and kicks == []


def test_main_in_tools_mode_gets_a_stop_listener_bound_to_the_consolidator_session(env, monkeypatch, review_seam):
    from app.services import plans_reader_consolidator as consolidator
    from app.services import plans_reader_toolloop as toolloop

    run, main = _consolidating_main(env)
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: None)
    seen: dict = {}

    def run_loop(ctx):
        seen["kind"] = ctx.kind
        seen["before"] = ctx.stop_when()
        monkeypatch.setitem(consolidator._SESSIONS, main["id"], SimpleNamespace(finished=True))
        seen["after"] = ctx.stop_when()
        return SimpleNamespace(parsed=None, text="", as_dict=lambda: {"status": "done", "rounds": 2, "stop_reason": "tool_stop"})

    monkeypatch.setattr(toolloop, "run_loop", run_loop)
    agents.run_group(env.db, main["id"])
    assert seen == {"kind": "main", "before": False, "after": True}
    settled = env.db.rows("plans_reader_groups", id=main["id"])[0]
    assert settled["status"] == "done" and settled["loop_state"]["stop_reason"] == "tool_stop"
    # Review kinds get no stop listener.
    env.settings = make_settings(**SPINE_ON, plans_reader_review_mode="apply")
    done_run, _doc = _done_run(env)
    group = _review_group(env, done_run, "review")
    monkeypatch.setattr(toolloop, "run_loop", lambda ctx: seen.update(review_stop=ctx.stop_when) or SimpleNamespace(
        parsed={}, text="", as_dict=lambda: {"status": "done"}))
    agents.run_group(env.db, group["id"])
    assert seen["review_stop"] is None


@pytest.mark.parametrize("failure", ["loop", "module"])
def test_a_failed_main_loop_falls_back_to_the_code_assembly_and_still_settles(env, monkeypatch, review_seam, failure):
    from app.services import plans_reader_consolidator as consolidator
    from app.services import plans_reader_toolloop as toolloop

    run, main = _consolidating_main(env)
    advanced: list = []
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: advanced.append(run_id))
    message = "The consolidator cannot afford its final answer within its budget."

    def run_loop(ctx):
        if failure == "loop":
            raise toolloop.LoopFailed(message, reason="budget")
        raise agents.LoopModuleMissing(message)

    monkeypatch.setattr(toolloop, "run_loop", run_loop)
    seen: dict = {}

    def fallback(sb, r, group, settings=None, *, reason="loop_failed", inputs=None):
        seen.update(reason=reason, group=group["id"], run=r["id"], settings=settings)
        return {"boq": {"areas": []}, "consolidator": {"mode": "fallback"}}

    monkeypatch.setattr(consolidator, "fallback_main_output", fallback)
    agents.run_group(env.db, main["id"])
    settled = env.db.rows("plans_reader_groups", id=main["id"])[0]
    assert settled["status"] == "done" and settled["output"]["consolidator"]["mode"] == "fallback"
    assert settled["loop_state"]["fallback"] == message and settled["error"] is None
    assert seen == {"reason": message, "group": main["id"], "run": run["id"], "settings": env.settings}
    assert advanced == [run["id"]] and _run(env, run)["status"] == "consolidating"


def test_a_failed_recount_loop_still_raises(env, monkeypatch, review_seam):
    from app.services import plans_reader_toolloop as toolloop

    env.settings = make_settings(**SPINE_ON)
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc)
    group = env.db.add("plans_reader_groups", run_id=run["id"], kind="recount", category="electrical_drawings",
                       status="queued", **{"pass": 3}, pages=[{"document_id": doc["id"], "page": 1, "kind": "image"}],
                       est_input_tokens=500, loop_state={})

    def boom(sb, run, group, *, settings=None):
        raise toolloop.LoopFailed("The recount answer was not the required JSON.", reason="bad_output")

    monkeypatch.setattr(runs, "run_recount_group", boom)
    with pytest.raises(toolloop.LoopFailed):
        agents.run_group(env.db, group["id"])


@pytest.mark.parametrize("pricing,agent_tools,expected", [(True, True, True), (False, True, False), (True, False, False)])
def test_pricing_tools_need_both_the_pricing_and_the_agent_tools_flags(env, monkeypatch, review_seam, pricing, agent_tools, expected):
    from app.services import plans_reader_tools
    from app.services import plans_reader_toolloop as toolloop

    flags = {**SPINE_ON, "ai_pricing_enabled": pricing, "ai_pricing_autorun": pricing}
    env.settings = make_settings(**flags, plans_reader_review_mode="apply", ai_pricing_agent_tools_enabled=agent_tools)
    monkeypatch.setattr(runs, "advance", lambda sb, run_id: None)
    seen: dict = {}
    real = plans_reader_tools.offered_tools
    monkeypatch.setattr(plans_reader_tools, "offered_tools", lambda kind, **kw: seen.update(kw) or real(kind, **kw))
    monkeypatch.setattr(toolloop, "run_loop", lambda ctx: SimpleNamespace(parsed={}, text="", as_dict=lambda: {"status": "done"}))
    run, _doc = _done_run(env)
    agents.run_group(env.db, _review_group(env, run, "review")["id"])
    assert seen["pricing_allowed"] is expected


# ---------------------------------------------------------------------------
# The derive measure needs page facts (X1: an imported run measured nothing)
# ---------------------------------------------------------------------------


def _consolidating_with_plan_sheets(env):
    """A consolidating run with one electrical drawing document whose
    stored PDF is a synthetic two-sheet plan set (E1.01, E1.02) and whose
    facts never ran (sha256 and facts_status null, as an imported run's)."""
    import uuid

    from tests.pagefacts_db import e_set_pdf

    run = _reading_run(env)
    path = f"plans-reader/{run['id']}/source/{uuid.uuid4().hex}-elec.pdf"
    env.store[path] = e_set_pdf()
    f = env.db.add("plans_reader_files", run_id=run["id"], filename="elec.pdf", storage_path=path, label="electrical_drawings", status="labeled")
    doc = env.db.add("plans_reader_documents", run_id=run["id"], file_id=f["id"], category="electrical_drawings",
                     name="ELEC set", storage_path=path, page_start=1, page_end=2, page_count=2, status="done")
    _sub(env, run, doc)
    run = set_status(env, run, "consolidating")
    env.db.add("plans_reader_outputs", run_id=run["id"], scope="category", category="electrical_drawings",
               version=1, output=sub_output("electrical_drawings", [(1, 1)]), source_group_ids=[])
    derive = env.db.add("plans_reader_groups", run_id=run["id"], kind="derive", status="queued", pages=[], est_input_tokens=0)
    return run, doc, derive


def test_a_derive_without_page_facts_records_measure_no_sheets_and_measures_once_facts_are_stored(env, monkeypatch, caplog):
    from app.services import scope_engine
    from app.services.pagefacts import api as pf_api
    from app.services.pagefacts import integration as pf_integration

    env.settings = make_settings(**SPINE_ON, plans_reader_page_facts_enabled=True)
    monkeypatch.setattr(scope_engine, "prepare", lambda sb, run, outputs=None, **kw: None)
    pf_api.clear_caches()
    run, doc, derive = _consolidating_with_plan_sheets(env)
    with caplog.at_level("WARNING", logger="app.services.plans_reader_runs"):
        out = runs.run_derive(env.db, run, derive)
    (warning,) = [w for w in out["warnings"] if w.startswith("measure_no_sheets")]
    assert "1 of 1 drawing documents have no finished page facts" in warning and "ELEC set (not started)" in warning
    assert any("framed no plan sheet" in r.getMessage() for r in caplog.records)
    stored = env.db.rows("plans_reader_outputs", run_id=run["id"], scope="measure")
    assert stored and max(stored, key=lambda r: r["version"])["output"]["stats"]["sheets"] == 0

    # The import harness's step: the WS1 facts written through the facts job core.
    summary = pf_integration.extract_and_store(env.db, doc["id"], settings=env.settings)
    assert summary["status"] == "done"
    pf_api.clear_caches()
    out = runs.run_derive(env.db, run, derive)
    assert not [w for w in out["warnings"] if w.startswith("measure_no_sheets")]
    latest = max(env.db.rows("plans_reader_outputs", run_id=run["id"], scope="measure"), key=lambda r: r["version"])
    assert latest["output"]["stats"]["sheets"] >= 1
    pf_api.clear_caches()


def test_measure_no_sheets_names_page_facts_off_and_a_run_without_drawings(env, monkeypatch):
    from app.services import scope_engine

    monkeypatch.setattr(scope_engine, "prepare", lambda sb, run, outputs=None, **kw: None)
    env.settings = make_settings(**SPINE_ON, plans_reader_page_facts_enabled=False)
    run, _doc, derive = _consolidating_with_plan_sheets(env)
    out = runs.run_derive(env.db, run, derive)
    assert any(w.startswith("measure_no_sheets") and "page facts are off" in w for w in out["warnings"])
    run2 = _reading_run(env)
    spec = seed_document(env, run2, category="specifications", pages=1)
    _sub(env, run2, spec, category="specifications")
    run2 = set_status(env, run2, "consolidating")
    derive2 = env.db.add("plans_reader_groups", run_id=run2["id"], kind="derive", status="queued", pages=[], est_input_tokens=0)
    out = runs.run_derive(env.db, run2, derive2)
    assert any(w.startswith("measure_no_sheets") and "no drawing document" in w for w in out["warnings"])


# ---------------------------------------------------------------------------
# Render step 2 then 3: enforce keeps provenance, so the final gates agree
# with what finish() saw (first live WS4 iteration, run 19344776)
# ---------------------------------------------------------------------------


def _spine_row(rid: str, desc: str, qty: float, number: str, *, key: str = "D1P7") -> dict:
    return {"row_id": rid, "description": desc, "quantity": qty, "unit": "EA", "status": "active", "coverage": "sheet_total",
            "basis": "counted_symbols", "sheet": {"document_id": "d1", "page": 7, "sheet_number": number, "sheet_key": key},
            "document_id": "d1", "page": 7, "flags": [], "superseded": [], "alternates": [], "family_tags": [], "positions": [],
            "origin": {"group_id": "g", "group_kind": "sub", "pass": 1, "local_id": "c1"}, "fingerprint": rid, "item_key": None}


def _spine_item(lid: str, desc: str, qty: float, refs: list[str], sheet: str, *, material: str = "General Material") -> dict:
    return {"line_id": lid, "description": desc, "quantity": qty, "unit": "EA", "sheet_refs": [sheet], "caveat": None,
            "material_note": None, "material_category": material, "wastage_applies": False, "is_accessory": False,
            "source_refs": [{"ref": r, "multiplier": 1.0} for r in refs], "combine": "sum" if len(refs) > 1 else "single",
            "basis": "counted", "needs_recount": False}


def _spine_main(demo: list[dict], new_work: list[dict]) -> dict:
    return {
        "boq": {"areas": [{"area": "A", "sections": [
            {"heading": "DEMOLITION", "groups": [{"name": "DEMOLITION", "subgroups": [{"name": "", "items": demo}]}]},
            {"heading": "NEW WORK", "groups": [{"name": "OTHER ITEMS", "subgroups": [{"name": "MISCELLANEOUS", "items": new_work}]}]},
        ]}]},
        "brief": {"systems": [], "rfis": [], "exclusions": []}, "estimate": {"extra_allowances": []},
        "dispositions": [], "consolidator": {"policy_rows": []},
    }


def test_enforce_merges_keep_provenance_so_the_final_gates_see_no_orphans():
    """Run 19344776: enforce merged B2 into B1 (demolition canonical row)
    and B11 into B10 (IN-EQUIP-CONNECTION) keeping only the head's
    source_refs, so the final gates reported arithmetic (B10 stored 7,
    computed 6) and orphan rows C15 and C33 that the session had consumed,
    while finish() inside the loop had seen neither. A merged line cites
    every source row and the canonical power row is an EA device class."""
    from app.core.config import get_settings
    from app.services import plans_reader_gates as G
    from app.services import plans_reader_lines as lines_mod
    from app.services import scope_engine as se
    from app.services.scope_policy_seed import SEED_POLICY

    rows = [
        _spine_row("C13", "Existing duplex receptacle to be removed", 11, "ED2.01"),
        _spine_row("C14", "Existing duplex receptacle, filled symbol, to be removed", 8, "ED2.01"),
        _spine_row("C15", "Existing quad outlet to be removed", 8, "ED2.01"),
        _spine_row("C16", "Existing floor outlet assembly to be removed", 7, "ED2.01"),
        _spine_row("C32", "Electrical connection to new fire/smoke damper", 6, "E2.01", key="D1P9"),
        _spine_row("C33", "Electrical connection to new VAV", 1, "E2.01", key="D1P9"),
    ]
    cat = {"rows_version": 2, "category": "electrical_drawings", "counts": rows, "runs": [], "schedules": [], "specs": [],
           "scope": [], "details": [], "questions": [], "floors": [], "row_seq": {"C": 33}, "merge_log": {}}
    main = _spine_main(
        [_spine_item("B1", "REMOVE EXISTING DUPLEX RECEPTACLES, OPEN AND FILLED SYMBOLS", 19, ["C13", "C14"], "ED2.01", material="Demolition"),
         _spine_item("B2", "REMOVE EXISTING MULTIPLE RECEPTACLE/QUAD OUTLET", 8, ["C15"], "ED2.01", material="Demolition"),
         _spine_item("B3", "REMOVE EXISTING FLOOR OUTLET ASSEMBLY", 7, ["C16"], "ED2.01", material="Demolition")],
        [_spine_item("B10", "ELECTRICAL CONNECTION TO NEW FIRE/SMOKE DAMPER", 6, ["C32"], "E2.01"),
         _spine_item("B11", "ELECTRICAL CONNECTION TO NEW VAV", 1, ["C33"], "E2.01")],
    )
    settings = get_settings().model_copy(update={"plans_reader_demo_two_line_style": False, "plans_reader_control_cable_ft_per_device": 0,
                                                 "plans_reader_gates_blocking": True})
    incs = se.compute_inclusions(se.ScopeInputs([], category_outputs={"electrical_drawings": cat}), SEED_POLICY, settings=settings)
    ctx = {"inclusions": incs, "standards": {"demo_disposition": {"value": "REMOVE_TO_SOURCE", "cite": "ED3.01 KN1", "clarification": "Removed to source."}}}
    out = se.enforce(main, ctx, SEED_POLICY, (), settings=settings)
    lines = {ln["line_id"]: ln for ln in G.boq_lines_of(out)}
    power = lines["B1"]
    assert power["description"] == se.DEMO_POWER_LINE and power["quantity"] == 27 and power["combine"] == "sum"
    assert [r["ref"] for r in power["source_refs"]] == ["C13", "C14", "C15"] and power["sheet_refs"] == ["ED2.01"]
    assert [r["ref"] for r in lines["B3"]["source_refs"]] == ["C16"]
    conn = lines["B10"]
    assert conn["description"] == "ELECTRICAL CONNECTION" and conn["quantity"] == 7 and conn["combine"] == "sum"
    assert [r["ref"] for r in conn["source_refs"]] == ["C32", "C33"] and "B11" not in lines
    assert lines_mod.recheck(lines.values(), cat) == []
    rep = G.run_final(boq=out, main=out, category_outputs=[cat], settings=settings, mode="tools")
    assert not ({"orphan_rows", "arithmetic", "unsourced_line", "linear_in_ea", "double_consumption"} & set(rep.by_gate()))
    # idempotent: a second enforce changes nothing
    again = se.enforce(out, ctx, SEED_POLICY, (), settings=settings)
    assert G.boq_lines_of(again) == G.boq_lines_of(out)


def test_an_inserted_inclusion_cites_its_trigger_rows_and_a_fact_conflict_is_a_warning():
    """Run 19344776: with no thermostat line in the BOQ the engine inserted
    B31 RELOCATION OF THERMOSTAT TO NEW LOCATION (5 EA) with no source
    rows, and the final gates blocked it twice (unsourced_line and
    excluded_system by F4) although the binding inclusion carries it (Q14).
    The insert now cites its trigger rows and the gates, given the run's
    policy inclusions, record fact_conflict for a human."""
    from app.core.config import get_settings
    from app.services import plans_reader_gates as G
    from app.services import plans_reader_lines as lines_mod
    from app.services import scope_engine as se
    from app.services.estimating_contracts import Fact, FactCite
    from app.services.scope_policy_seed import SEED_POLICY

    rows = [
        _spine_row("C49", "Existing thermostat sensor relocation demolition phase: remove wiring and conduit back to VV", 5, "MD2.01", key="D2P5"),
        _spine_row("C53", "Relocate existing thermostat sensors and provide new conduit and wiring to VV", 5, "M2.01", key="D2P7"),
        _spine_row("C32", "Electrical connection to new fire/smoke damper", 6, "E2.01", key="D1P9"),
    ]
    cat = {"rows_version": 2, "category": "mechanical_drawings", "counts": rows, "runs": [], "schedules": [], "specs": [],
           "scope": [], "details": [], "questions": [], "floors": [], "row_seq": {"C": 53}, "merge_log": {}}
    settings = get_settings().model_copy(update={"plans_reader_control_cable_ft_per_device": 0, "plans_reader_gates_blocking": True})
    incs = se.compute_inclusions(se.ScopeInputs([], category_outputs={"mechanical_drawings": cat}), SEED_POLICY, settings=settings)
    reloc = next(i for i in incs if i["rule_id"] == "IN-THERMOSTAT-RELOC")
    assert reloc["quantity"] == 5 and [t["row_id"] for t in reloc["triggers"] if t["used"]] == ["C53"]
    main = _spine_main([], [_spine_item("B10", "ELECTRICAL CONNECTION TO NEW FIRE/SMOKE DAMPER", 6, ["C32"], "E2.01")])
    out = se.enforce(main, {"inclusions": incs}, SEED_POLICY, (), settings=settings)
    lines = {ln["line_id"]: ln for ln in G.boq_lines_of(out)}
    tstat = next(ln for ln in lines.values() if ln["description"] == "RELOCATION OF THERMOSTAT TO NEW LOCATION")
    assert tstat["quantity"] == 5 and tstat["scope_rule"] == "IN-THERMOSTAT-RELOC" and tstat["basis"] == "policy"
    assert tstat["source_refs"] == [{"ref": "C53", "multiplier": 1.0}] and tstat["combine"] == "single"
    assert lines_mod.recheck(lines.values(), cat) == []
    controls = Fact(id="F4", system="hvac_controls", statement="ATC wiring by the controls contractor.", party="other trade", verified=True,
                    cite=FactCite(document_id="m", page=7, quote="ALL ATC WIRING BY TEMPERATURE CONTROLS CONTRACTOR."), kind="provided_by_others")
    rep = G.run_final(boq=out, main=out, category_outputs=[cat], facts=[controls], settings=settings, mode="tools",
                      policy_includes=G.policy_include_matcher({"inclusions": incs}))
    by = rep.by_gate()
    assert "excluded_system" not in by and "unsourced_line" not in by and "arithmetic" not in by
    assert by["fact_conflict"][0].target["line_id"] == tstat["line_id"] and by["fact_conflict"][0].severity == "warn"
    # the demolition-phase row is still an orphan for the model to account (the gate is not loosened)
    assert by["orphan_rows"][0].target["row_ids"] == ["C49"]
    # a context stored before triggers carried row ids inserts the line without sources, as before
    bare = [{**i, "triggers": [{k: v for k, v in t.items() if k != "row_id"} for t in i["triggers"]]} for i in incs]
    out2 = se.enforce(main, {"inclusions": bare}, SEED_POLICY, (), settings=settings)
    tstat2 = next(ln for ln in G.boq_lines_of(out2) if ln["description"] == "RELOCATION OF THERMOSTAT TO NEW LOCATION")
    assert "source_refs" not in tstat2
