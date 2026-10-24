"""WS3 B11 measure assist and B12 scales draft (docs/AI_ESTIMATING_BUILD_SPEC.md
6 WS3). Every model call is scripted (llm.complete_turn patched); no
network, no database. Synthetic geometry and synthetic spend numbers."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services import llm, llm_parts
from app.services import plans_reader_measure as M
from app.services import plans_reader_measure_assist as A
from app.services import plans_reader_model_routes as model_routes
from app.services.pagefacts.contracts import PageFacts
from app.services.plans_reader_schemas import validate_strict
from tests.test_plans_reader_measure import CONV, _page, _single_sheet_inputs

W_IN, H_IN = 42.0, 30.0
TAG_POINT = [round(10.75 / W_IN, 4), round(9.61 / H_IN, 4)]
RUN = "run-assist"


# Synthetic sheet: one 3 in x 0.1 in fixture outline (12 ft at 1/4"), the tag's own frame, a tiny box.


def _rect_ops(x0: float, y0: float, x1: float, y1: float) -> str:
    """Four stroked lines of a rectangle given in inches from the top left."""

    def pt(x: float, y: float) -> str:
        return f"{x * 72:.2f} {(H_IN - y) * 72:.2f}"

    return f"{pt(x0, y0)} m {pt(x1, y0)} l S {pt(x1, y0)} m {pt(x1, y1)} l S {pt(x1, y1)} m {pt(x0, y1)} l S {pt(x0, y1)} m {pt(x0, y0)} l S"


def _sheet_pdf() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    ops = " ".join(
        [
            "0.5 w",
            _rect_ops(10.0, 10.0, 13.0, 10.1),  # the fixture (12.0 ft)
            _rect_ops(10.5, 9.55, 11.0, 9.67),  # the tag's own frame (2.0 ft)
            _rect_ops(11.5, 10.5, 11.72, 10.6),  # an implausible box (0.88 ft)
        ]
    )
    writer = PdfWriter()
    page = writer.add_blank_page(width=W_IN * 72, height=H_IN * 72)
    stream = DecodedStreamObject()
    stream.set_data(ops.encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


PDF = _sheet_pdf()


def _linear_item(n: int, tag: str, *, flags=None, instances=None, overridden_by=None) -> dict:
    return {
        "measure_id": f"lin{n}", "ref": f"m:lin{n}", "kind": "linear_fixture", "tag": tag, "description": f"{tag} LED UNDERCABINET",
        "document_id": "doc1", "page": 1, "sheet_key": "E2.01", "tag_points": [TAG_POINT], "area": "NEW WORK", "group": "LIGHTING FIXTURES",
        "subgroup": "FIXTURES", "material_category": "Lighting", "count": 1, "flags": list(flags if flags is not None else ["unmatched_tags:1"]),
        "instances": list(instances or []), "total_ft": None, "basis": "measured_route", "overridden_by": overridden_by,
        "unmatched": [TAG_POINT] if flags is None else [],
    }


def _service(items: list[dict], *, judgments=None, stats_assist=None, overrides=None) -> M.MeasureService:
    m = M.build_measure(_single_sheet_inputs(), CONV)
    m["linear_items"] = items
    m["judgments"] = list(judgments or [])
    m["overrides"] = list(overrides or [])
    if stats_assist is not None:
        m["stats"] = {**m["stats"], "assist": stats_assist}
    svc = M.MeasureService(M.MemoryMeasureStore(), conv=CONV)
    svc.register_run(RUN, m)
    return svc


def _settings(**kw):
    base = {"plans_reader_measure_assist_enabled": True, "plans_reader_measure_assist_max_items": 8, "plans_reader_measure_assist_max_usd": 0.75}
    base.update(kw)
    return get_settings().model_copy(update=base)


class Script:
    """A scripted complete_turn: answers in order, records every request."""

    def __init__(self, answers, *, cost: str | None = "0.01"):
        self.answers = list(answers)
        self.requests: list[tuple[str, llm_parts.ModelRequest]] = []
        self.cost = cost

    def __call__(self, feature, req, *, settings=None):
        self.requests.append((feature, req))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(req)
        model = model_routes.resolve_step("derive", settings).model
        return llm_parts.ModelTurn(text=json.dumps(answer), parsed=answer, tool_calls=[], usage=None, cost_usd=Decimal(self.cost) if self.cost else None, response_id=f"resp{len(self.requests)}", stop_reason="final", provider="openai", model=model)


def _candidate_ids(req: llm_parts.ModelRequest) -> dict[str, float | None]:
    text = next(p.text for p in req.messages[0].parts if isinstance(p, llm_parts.TextPart))
    out: dict[str, float | None] = {}
    for line in text.splitlines():
        if line.startswith("- c"):
            cid, rest = line[2:].split(":", 1)
            length = rest.rsplit(",", 1)[1].strip().split(" ")[0]
            out[cid] = None if length == "unknown" else float(length)
    return out


def _fail_if_called(*_a, **_k):
    raise AssertionError("no model call may be made here")


# Contract ---------------------------------------------------------------------------------------


def test_schema_is_strict_and_holds_no_number_field():
    validate_strict(A.ASSIST_SCHEMA)
    props = A.ASSIST_SCHEMA["properties"]
    assert set(props) == {"picks", "none_fit", "note"}
    assert props["picks"]["items"]["type"] == "string"
    assert "number" not in json.dumps(A.ASSIST_SCHEMA) and "integer" not in json.dumps(A.ASSIST_SCHEMA)
    assert "\u2014" not in A.ASSIST_SYSTEM and "\u2013" not in A.ASSIST_SYSTEM


def test_disabled_flag_makes_no_call_and_writes_nothing(monkeypatch):
    svc = _service([_linear_item(1, "L5")])
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings(plans_reader_measure_assist_enabled=False))
    assert res.status == "disabled" and res.calls == 0 and svc.latest(RUN)[0] == 1


def test_picks_only_among_candidates_and_code_measures_the_length(monkeypatch):
    svc = _service([_linear_item(1, "L5")], judgments=[{"id": "J1", "kind": "linear_candidates", "refs": {"tag": "L5", "document_id": "doc1", "page": 1, "measure_id": "lin1"}, "default_applied": "vector_rect nearest", "resolved_by": None}])
    seen: dict[str, dict] = {}

    def answer(req):
        cands = _candidate_ids(req)
        seen["cands"] = cands
        fixture = next(c for c, ft in cands.items() if ft == 12.0)
        tiny = next(c for c, ft in cands.items() if ft is not None and ft < 1.0)
        # The model names the fixture, an id that does not exist, the tiny box and the fixture again.
        return {"picks": [fixture, "c99", tiny, fixture], "none_fit": False, "note": "the outline under the tag"}

    script = Script([answer])
    monkeypatch.setattr(llm, "complete_turn", script)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings())
    assert res.status == "done" and res.calls == 1 and res.overrides == 1 and res.version == 2
    feature, req = script.requests[0]
    assert feature == "plans_derive" and req.step == "derive" and not req.tools_enabled and req.response_schema == A.ASSIST_SCHEMA
    images = [p for p in req.messages[0].parts if isinstance(p, llm_parts.ImagePart)]
    assert len(images) == 1 and images[0].png.startswith(b"\x89PNG") and images[0].view_id == "lin:doc1:1"
    assert len(seen["cands"]) == 3
    fixture = next(c for c, ft in seen["cands"].items() if ft == 12.0)
    item = res.items[0]
    assert item["outcome"] == "picked" and item["picks"] == [fixture]
    assert {r["why"] for r in item["rejected"]} == {"not_a_candidate", "implausible_length"} and "c99" in {r["id"] for r in item["rejected"]}
    latest = svc.latest(RUN)[1]
    ov = latest["overrides"][-1]
    assert ov["tool"] == "set_linear_item" and ov["by"] == "model" and ov["source"] == "measure_assist" and ov["args"]["candidate_ids"] == [fixture]
    # The endpoints are the code's centerline of the picked rectangle; the length is measured at the sheet scale.
    (x0, y0), (x1, y1) = ov["args"]["endpoints"][0]
    assert y0 == y1 and x0 == pytest.approx(10.0 / W_IN, abs=1e-4) and x1 == pytest.approx(13.0 / W_IN, abs=1e-4)
    row = next(it for it in latest["linear_items"] if it["measure_id"] == "lin1")
    assert row["total_ft"] == pytest.approx(12.0, abs=0.05) and row["overridden_by"] == "model" and row["basis"] == "measured_route"
    assert any(q["description"].startswith("L5") and q["unit"] == "FT" and q["quantity"] == pytest.approx(12.0, abs=0.05) for q in latest["quantities"])
    assert latest["judgments"][0]["resolved_by"] == "measure_assist" and latest["judgments"][0]["resolution"]["outcome"] == "picked"
    assert latest["stats"]["assist"]["calls"] == 1 and latest["stats"]["assist"]["spent_usd"] == pytest.approx(0.01)
    # A second pass never re-asks for an assisted item.
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    again = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings())
    assert again.status == "nothing_to_assist" and svc.latest(RUN)[0] == 2


def test_none_fit_and_unknown_ids_only_write_no_override(monkeypatch):
    svc = _service([_linear_item(1, "L5"), _linear_item(2, "L6")])
    script = Script([{"picks": ["c1"], "none_fit": True, "note": ""}, {"picks": ["zz", "c400"], "none_fit": False, "note": ""}])
    monkeypatch.setattr(llm, "complete_turn", script)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings())
    assert res.calls == 2 and res.overrides == 0 and [i["outcome"] for i in res.items] == ["none_fit", "none_fit"]
    latest = svc.latest(RUN)[1]
    assert not [o for o in latest["overrides"] if o.get("source") == "measure_assist"]
    assert all(it["total_ft"] is None for it in latest["linear_items"])


def test_budget_cap_is_respected_before_each_call(monkeypatch):
    svc = _service([_linear_item(1, "L5"), _linear_item(2, "L6"), _linear_item(3, "L7")])
    monkeypatch.setattr(A, "estimate_call_usd", lambda *a, **k: Decimal("0.30"))
    script = Script([{"picks": [], "none_fit": True, "note": ""}] * 3, cost="0.30")
    monkeypatch.setattr(llm, "complete_turn", script)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings(plans_reader_measure_assist_max_usd=0.75))
    # 0.30 + 0.30 fits 0.75; a third reserve of 0.30 would pass it, so it is never sent.
    assert res.status == "budget" and res.calls == 2 and len(script.requests) == 2
    assert res.items[-1]["outcome"] == "budget" and res.items[-1]["reserve_usd"] == pytest.approx(0.30)
    assert res.spent_usd == pytest.approx(0.60)
    # Spend already recorded on the run counts toward the per-run cap on a later pass.
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    later = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings(plans_reader_measure_assist_max_usd=0.75))
    assert later.status == "budget" and later.calls == 0


def test_a_tiny_cap_sends_nothing(monkeypatch):
    svc = _service([_linear_item(1, "L5")])
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings(plans_reader_measure_assist_max_usd=0.0))
    assert res.status == "budget" and res.calls == 0 and svc.latest(RUN)[0] == 1


def test_max_items_cap_counts_items_already_assisted(monkeypatch):
    svc = _service([_linear_item(1, "L5"), _linear_item(2, "L6"), _linear_item(3, "L7")])
    script = Script([{"picks": [], "none_fit": True, "note": ""}] * 3)
    monkeypatch.setattr(llm, "complete_turn", script)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings(plans_reader_measure_assist_max_items=2))
    assert res.calls == 2 and [i["tag"] for i in res.items] == ["L5", "L6"]
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    assert A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings(plans_reader_measure_assist_max_items=2)).status == "nothing_to_assist"


def test_selection_skips_human_set_items_and_sheets_without_scale_or_vectors():
    human_override = {"tool": "set_linear_item", "args": {"document_id": "doc1", "page": 1, "tag": "L8"}, "by": "human"}
    items = [
        _linear_item(1, "L5", flags=[], instances=[{"bbox": [0.1, 0.1, 0.2, 0.11], "length_ft": 6.0}]),  # confident: no assist
        _linear_item(2, "L6", flags=["scale_unknown"]),
        _linear_item(3, "L7", flags=["no_vectors"]),
        _linear_item(4, "L8"),
        _linear_item(5, "L9", overridden_by="human"),
        _linear_item(6, "LA", flags=["implausible_instance"], instances=[{"bbox": [0.1, 0.1, 0.2, 0.11], "length_ft": 60.0}]),
        _linear_item(7, "LB", flags=[]),
        {**_linear_item(8, "TRK"), "kind": "linear_demolition"},
    ]
    m = {"linear_items": items, "overrides": [human_override], "judgments": [{"id": "J1", "kind": "linear_candidates", "refs": {"measure_id": "lin7"}, "resolved_by": None}]}
    got = A.select_items(m, max_items=8)
    assert [(i.tag, i.reason) for i in got] == [("LB", "judgment"), ("LA", "implausible")]
    assert [i.tag for i in A.select_items(m, max_items=1)] == ["LB"]


def test_unpriced_model_refuses_before_any_call(monkeypatch):
    svc = _service([_linear_item(1, "L5")])
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    monkeypatch.setattr(A.llm_pricing, "price_for", lambda _m: None)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings())
    assert res.status == "unpriced_model" and res.calls == 0


def test_a_failed_call_is_booked_and_the_next_item_still_runs(monkeypatch):
    svc = _service([_linear_item(1, "L5"), _linear_item(2, "L6")])
    booked: list[dict] = []
    monkeypatch.setattr(A, "estimate_call_usd", lambda *a, **k: Decimal("0.05"))
    monkeypatch.setattr(A.llm_errors, "classify", lambda exc: A.llm_errors.KIND_TIMEOUT)
    monkeypatch.setattr(llm, "last_cost_usd", lambda: None)
    script = Script([TimeoutError("synthetic timeout"), lambda req: {"picks": [next(c for c, ft in _candidate_ids(req).items() if ft == 12.0)], "none_fit": False, "note": ""}])
    monkeypatch.setattr(llm, "complete_turn", script)
    res = A.run_assist(RUN, svc=svc, pdfs={"doc1": PDF}, settings=_settings(), record_call=lambda **kw: booked.append(kw))
    assert [i["outcome"] for i in res.items] == ["error:TimeoutError", "picked"]
    # The timeout is billed at its reserve; the answered call at its own cost.
    assert res.spent_usd == pytest.approx(0.05 + 0.01) and len(booked) == 2 and booked[0]["error"] is not None and booked[1]["turn"] is not None


def test_no_measure_output_and_missing_pdf(monkeypatch):
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    empty = M.MeasureService(M.MemoryMeasureStore(), conv=CONV)
    assert A.run_assist(RUN, svc=empty, pdfs={}, settings=_settings()).status == "no_measure"
    svc = _service([_linear_item(1, "L5")])
    res = A.run_assist(RUN, svc=svc, pdfs={}, settings=_settings())
    # Without the PDF the tool finds no vectors, so nothing is offered to a model.
    assert res.calls == 0 and res.items[0]["outcome"] in ("no_candidates", "no_overlay")


def test_centerline_and_fit_image():
    assert A.centerline({"bbox": [0.1, 0.2, 0.3, 0.22], "orientation": "h"}) == [[0.1, 0.21], [0.3, 0.21]]
    assert A.centerline({"bbox": [0.1, 0.2, 0.12, 0.5], "orientation": "v"}) == [[0.11, 0.2], [0.11, 0.5]]
    from PIL import Image

    big = io.BytesIO()
    Image.new("RGB", (7000, 5000), "white").save(big, format="PNG")
    png, w, h = A.fit_image(big.getvalue(), "openai")
    assert llm_parts.image_spec_for("openai").fits(w, h) and (w, h) != (7000, 5000) and abs(w / h - 1.4) < 0.01
    small = io.BytesIO()
    Image.new("RGB", (100, 50), "white").save(small, format="PNG")
    assert A.fit_image(small.getvalue(), "openai")[1:] == (100, 50)


# B12 scales draft ---------------------------------------------------------------------------------


def _draft_module():
    from scripts import plans_reader_scales_draft as D

    return D


def test_scales_draft_selection_is_deterministic_stratified_and_holdout_free():
    D = _draft_module()
    from app.services import eval_manifest

    pool = D.pool_ids()
    assert not set(pool) & set(eval_manifest.HOLDOUT_IDS) and not set(pool) & set(eval_manifest.TIER_IDS["core"])
    classes = {pid: ("tagged", "untagged_text", "vector_text", "scanned")[i % 4] for i, pid in enumerate(pool)}
    a = D.select_projects(pool, classes, 20)
    assert a == D.select_projects(list(reversed(pool)), classes, 20) and len(a) == len(set(a)) == 20
    first_round = [classes[p] for p in a[:4]]
    assert sorted(first_round) == ["scanned", "tagged", "untagged_text", "vector_text"]
    assert D.select_projects(["p1", "p2"], {}, 20) == sorted(["p1", "p2"], key=D._order_key)


def test_scales_draft_parser_labels_and_vision_reasons():
    D = _draft_module()
    labeled = PageFacts.from_dict(_page(1, "E2.01", "POWER PLAN", 4.0))
    lab = D.label_page(labeled)
    assert lab["status"] == "labeled" and lab["primary_ft_per_in"] == 4.0 and lab["vision_reason"] is None and lab["source"] == "parser"
    no_scale = _page(2, "E2.02", "LIGHTING PLAN", None)
    no_scale["views"][0].update(scale_text=None, nts=False)
    lab2 = D.label_page(PageFacts.from_dict(no_scale))
    assert lab2["status"] == "needs_vision" and lab2["vision_reason"] == "no_scale"
    scanned = {**_page(3, None, None, None), "text_class": "scanned", "views": []}
    assert D.label_page(PageFacts.from_dict(scanned))["vision_reason"] == "no_text_layer"
    nts = D.label_page(PageFacts.from_dict(_page(4, "E0.02", "RISER DIAGRAM", None)))
    assert nts["status"] == "nts" and nts["nts"] is True


def test_scales_draft_vision_answer_is_converted_by_code_only():
    D = _draft_module()
    validate_strict(D.VISION_SCHEMA)
    assert "number" not in json.dumps(D.VISION_SCHEMA)
    got = D.vision_label_from_answer({"views": [{"title": "POWER PLAN", "scale_text": '1/4" = 1\'-0"'}, {"title": "DETAIL 2", "scale_text": "NTS"}], "sheet_scale_text": ""})
    assert got["status"] == "labeled" and got["primary_ft_per_in"] == 4.0 and got["source"] == "vision"
    two = D.vision_label_from_answer({"views": [{"title": "A", "scale_text": '1/8" = 1\'-0"'}, {"title": "B", "scale_text": '1/4" = 1\'-0"'}], "sheet_scale_text": ""})
    assert two["status"] == "conflict" and two["primary_ft_per_in"] is None
    sheet = D.vision_label_from_answer({"views": [{"title": "PLAN", "scale_text": ""}], "sheet_scale_text": 'SCALE: 1" = 20\''})
    assert sheet["primary_ft_per_in"] == 20.0
    assert D.vision_label_from_answer({"views": [{"title": "RISER", "scale_text": "NOT TO SCALE"}], "sheet_scale_text": ""})["status"] == "nts"
    assert D.vision_label_from_answer({"views": [], "sheet_scale_text": "about forty"})["status"] == "unresolved"
    assert D.vision_label_from_answer("garbage")["status"] == "unresolved"


def test_scales_draft_vision_respects_the_cap_and_scripted_model(monkeypatch):
    D = _draft_module()
    monkeypatch.setattr(D, "estimate_page_usd", lambda *a, **k: Decimal("0.40"))
    monkeypatch.setattr(D, "render_vision_png", lambda pdf, page, provider, ppi=150: (b"\x89PNG", 10, 10))
    calls: list[llm_parts.ModelRequest] = []

    def turn(feature, req):
        assert feature == "plans_derive" and req.step == "derive" and not req.tools_enabled
        calls.append(req)
        return llm_parts.ModelTurn(text="", parsed={"views": [{"title": "PLAN", "scale_text": '1/8" = 1\'-0"'}], "sheet_scale_text": ""}, tool_calls=[], usage=None, cost_usd=Decimal("0.40"), response_id="r", stop_reason="final", provider="openai", model="m")

    labels = [({"page": 1}, b"%PDF", 42.0, 30.0), ({"page": 2}, b"%PDF", 42.0, 30.0), ({"page": 3}, b"%PDF", 42.0, 30.0)]
    budget = D.VisionBudget(Decimal("1.00"))
    D.run_vision(labels, settings=get_settings(), budget=budget, turn_fn=turn)
    assert len(calls) == 2 and budget.calls == 2 and budget.spent == Decimal("0.80")
    assert labels[0][0]["vision"]["primary_ft_per_in"] == 8.0 and labels[2][0]["vision"]["status"] == "budget"


def test_scales_draft_dry_run_makes_no_call(monkeypatch, tmp_path):
    D = _draft_module()
    monkeypatch.setattr(llm, "complete_turn", _fail_if_called)
    queued = {"page": 5, "status": "needs_vision", "vision_reason": "no_scale", "primary_ft_per_in": None, "reduced_print_suspected": False}
    rows = [{"project_id": "p" * 36, "documents": [{"plan_pages": [queued, {"page": 1, "status": "labeled", "vision_reason": None, "primary_ft_per_in": 4.0, "reduced_print_suspected": False}]}]}]
    monkeypatch.setattr(D, "build_draft", lambda *a, **k: (rows, [(queued, b"%PDF", 42.0, 30.0)]))
    monkeypatch.setattr(D, "BackupData", lambda *_a, **_k: None)
    out = tmp_path / "scales_draft.json"
    assert D.main(["--out", str(out), "--projects", "zzzz", "--eval-root", str(tmp_path)]) == 0
    draft = json.loads(out.read_text())
    assert draft["mode"] == "dry_run" and draft["vision"]["calls"] == 0 and draft["vision"]["pages"] == 1
    assert draft["stats"]["final_status"] == {"labeled": 1, "pending": 1} and draft["stats"]["parser_labeled_share"] == 0.5
    # A dry run never overwrites a live draft.
    out.write_text(json.dumps({"mode": "live"}))
    with pytest.raises(SystemExit):
        D.main(["--out", str(out), "--projects", "zzzz", "--eval-root", str(tmp_path)])
    with pytest.raises(SystemExit):
        D.main(["--live", "--out", str(tmp_path / "x.json")])


SCALES_DRAFT = Path(get_settings().eval_root) / "manifest" / "scales_draft.json"


@pytest.mark.golden
@pytest.mark.skipif(not SCALES_DRAFT.exists(), reason="scales draft not built on this machine")
def test_golden_scales_draft_covers_20_holdout_free_projects():
    from app.services import eval_manifest

    draft = json.loads(SCALES_DRAFT.read_text())
    assert draft["schema"] == "bdr.ws3_scales_draft/1" and draft["stats"]["projects"] == 20
    ids = {p["project_id"] for p in draft["projects"]}
    assert not ids & set(eval_manifest.HOLDOUT_IDS)
    assert draft["stats"]["plan_pages"] > 0
