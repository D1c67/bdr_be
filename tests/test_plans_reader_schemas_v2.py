"""plans_reader_schemas: the 5.4 model-facing changes (WS4 wave 2). Every
change is nullable and strict-compatible; MAIN_AGENT_SCHEMA and BOQ_ITEM are
unchanged so the legacy main call keeps its shape."""

from __future__ import annotations

import json

import jsonschema
import pytest

from app.services import plans_reader_schemas as S

_PAGE = {"doc_index": 1, "page": 8, "sheet_number": "ED3.01"}


def _count(**kw) -> dict:
    row = {
        "description": "Remove existing duplex receptacle", "symbol_or_tag": "(X)", "quantity": 10, "unit": "EA",
        **_PAGE, "location": None, "basis": "counted_symbols", "confidence": 0.8,
        "material_category_hint": None, "notes": None, "points": None,
    }
    row.update(kw)
    return row


def _sub_output(**kw) -> dict:
    out = {
        "category": "electrical_drawings", "summary": "s",
        "pages": [{**_PAGE, "title": None, "page_role": "plan", "scale": None, "tags": [], "notes": [], "resolution_flags": [], "viewports": None}],
        "counts": [_count()], "runs": [], "scope": [], "specs": [], "schedules": [], "details": [], "questions": [],
    }
    out.update(kw)
    return out


@pytest.mark.parametrize("name", ["SUB_AGENT_SCHEMA", "ROLLUP_SCHEMA", "ESCALATION_SCHEMA", "MAIN_AGENT_SCHEMA", "COUNT_ITEM", "ESCALATION_COUNT_ITEM", "RUN_ITEM", "SUB_PAGE", "SCHEDULE_ITEM"])
def test_every_schema_is_strict_and_valid_json_schema(name):
    schema = getattr(S, name)
    S.validate_strict(schema)
    jsonschema.Draft202012Validator.check_schema(schema)


def test_count_item_gains_nullable_points():
    props = S.COUNT_ITEM["properties"]
    assert "points" in props and "points" in S.COUNT_ITEM["required"]
    assert props["points"]["anyOf"][1] == {"type": "null"}
    jsonschema.validate(_sub_output(), S.SUB_AGENT_SCHEMA)
    jsonschema.validate(_sub_output(counts=[_count(points=[[0.1234, 0.5], [0.2, 0.3]])]), S.SUB_AGENT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_sub_output(counts=[_count(points="0.1,0.2")]), S.SUB_AGENT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_sub_output(counts=[_count(points=[["a", "b"]])]), S.SUB_AGENT_SCHEMA)


def test_escalation_schema_uses_escalation_count_item():
    assert S.ESCALATION_SCHEMA is not S.SUB_AGENT_SCHEMA
    assert S.ROLLUP_SCHEMA is S.SUB_AGENT_SCHEMA
    assert S.ESCALATION_SCHEMA["properties"]["counts"]["items"] is S.ESCALATION_COUNT_ITEM
    extra = set(S.ESCALATION_COUNT_ITEM["properties"]) - set(S.COUNT_ITEM["properties"])
    assert extra == {"supersedes_row_id", "coverage"}
    assert S.ESCALATION_COUNT_ITEM["properties"]["coverage"]["enum"] == ["whole_sheet", "inside_tiles_only"]
    assert S.ESCALATION_COVERAGES == ("whole_sheet", "inside_tiles_only")
    for key in S.SUB_AGENT_SCHEMA["properties"]:
        if key != "counts":
            assert S.ESCALATION_SCHEMA["properties"][key] is S.SUB_AGENT_SCHEMA["properties"][key]
    patch = _sub_output(counts=[_count(supersedes_row_id="c3", coverage="inside_tiles_only")])
    jsonschema.validate(patch, S.ESCALATION_SCHEMA)
    jsonschema.validate(_sub_output(counts=[_count(supersedes_row_id=None, coverage="whole_sheet")]), S.ESCALATION_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_sub_output(counts=[_count()]), S.ESCALATION_SCHEMA)  # the two keys are required
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_sub_output(counts=[_count(supersedes_row_id=None, coverage="partial")]), S.ESCALATION_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(patch, S.SUB_AGENT_SCHEMA)  # a sub output never carries them


def test_run_item_gains_nullable_route():
    props = S.RUN_ITEM["properties"]
    assert "route" in props and props["route"]["anyOf"][1] == {"type": "null"}
    run = {
        "description": "Feeder", "conduit_size": None, "conduit_type": None, "conductors": None, "from_location": None,
        "to_location": None, "length_ft": 120, "basis": "stated", "underground": False, **_PAGE, "confidence": 0.5,
        "notes": None, "route": [[0.1, 0.1], [0.4, 0.1]],
    }
    jsonschema.validate(_sub_output(runs=[run]), S.SUB_AGENT_SCHEMA)
    jsonschema.validate(_sub_output(runs=[dict(run, route=None)]), S.SUB_AGENT_SCHEMA)


def test_sub_page_gains_nullable_viewports():
    props = S.SUB_PAGE["properties"]
    assert "viewports" in props and props["viewports"]["anyOf"][1] == {"type": "null"}
    assert set(S.VIEWPORT["properties"]) == {"title", "scale_text", "region"}
    page = {**_PAGE, "title": "t", "page_role": "plan", "scale": None, "tags": [], "notes": [], "resolution_flags": [],
            "viewports": [{"title": "ENLARGED PLAN", "scale_text": "1/4\" = 1'-0\"", "region": "0.5,0.5,1,1"}, {"title": "OVERALL", "scale_text": None, "region": "r1c1"}]}
    jsonschema.validate(_sub_output(pages=[page]), S.SUB_AGENT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_sub_output(pages=[dict(page, viewports=[{"title": "x"}])]), S.SUB_AGENT_SCHEMA)


def test_schedule_item_gains_schedule_id_and_source():
    props = S.SCHEDULE_ITEM["properties"]
    assert props["schedule_id"] == {"type": ["string", "null"]}
    assert props["source"]["enum"] == ["text_layer", "vision"]
    sched = {"schedule_type": "fixture", "name": "LUMINAIRE SCHEDULE", **_PAGE, "columns": ["TAG"], "rows": [["L1"]], "notes": None,
             "schedule_id": "538c95b5:s5", "source": "text_layer"}
    jsonschema.validate(_sub_output(schedules=[sched]), S.SUB_AGENT_SCHEMA)
    jsonschema.validate(_sub_output(schedules=[dict(sched, schedule_id=None, source="vision")]), S.SUB_AGENT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_sub_output(schedules=[dict(sched, source="model")]), S.SUB_AGENT_SCHEMA)


def test_main_agent_schema_and_boq_item_unchanged():
    assert set(S.BOQ_ITEM["properties"]) == {
        "description", "quantity", "unit", "sheet_refs", "caveat", "material_note", "material_category",
        "wastage_applies", "is_accessory",
    }
    assert set(S.MAIN_AGENT_SCHEMA["properties"]) == {"brief", "boq", "estimate", "markups", "marked_plans", "material_notes", "confidence_notes"}
    text = json.dumps(S.MAIN_AGENT_SCHEMA)
    for key in ("line_id", "source_refs", "combine", "supersedes_row_id", "route", "viewports", "schedule_id", "coverage"):
        assert f'"{key}"' not in text, key
    # `points` was already a MARKUP_LINE and PLAN_MARK field; COUNT_ITEM is not part of the main schema
    assert "symbol_or_tag" not in text


def test_no_dashes_in_the_schema_module():
    import inspect

    src = inspect.getsource(S)
    assert chr(0x2013) not in src and chr(0x2014) not in src
