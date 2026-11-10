"""Every fixture in tests/fixtures_browser_agents/ (spec 12.0 C1) validates
against the protocol models, every endpoint has at least one fixture, and the
tool schemas agree with the pydantic args models both policy checks use."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.services.browser_agents import protocol as p

FIXTURES = Path(__file__).resolve().parent / "fixtures_browser_agents"
FILES = sorted(FIXTURES.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_folder_has_the_named_examples():
    names = {f.stem for f in FILES}
    for required in ("claim_rfi", "turn_action_navigate", "session_ok", "session_no_jar", "help_messages"):
        assert required in names


def test_every_endpoint_has_a_fixture():
    covered = {_load(f).get("endpoint") for f in FILES}
    assert set(p.ENDPOINTS) <= covered, sorted(set(p.ENDPOINTS) - covered)


@pytest.mark.parametrize("path", [f for f in FILES if "endpoint" in _load(f)], ids=lambda f: f.stem)
def test_endpoint_fixture_validates(path):
    fx = _load(path)
    method, template, req_model, resp_model, ok_status = p.ENDPOINTS[fx["endpoint"]]
    assert fx["method"] == method
    assert fx["path"].startswith(p.API_PREFIX)
    if fx["status"] >= 400:
        code = fx["response_headers"][p.HEADER_ERROR]
        assert p.ERROR_HTTP_STATUS[p.ErrorCode(code)] == fx["status"]
        return
    if fx["request"] is not None:
        assert req_model is not None
        req_model.model_validate(fx["request"])
    elif req_model is not None and method != "GET":
        pytest.fail("request body missing")
    if fx["status"] == 204 or resp_model is None:
        assert fx["response"] is None
        return
    resp = fx["response"]
    if fx["endpoint"] in ("turns", "user_turns"):
        parsed = p.parse_turn_response(fx["status"], resp)
        assert type(parsed) in resp_model
    elif fx["endpoint"] == "config" and resp.get("pending"):
        p.PendingResponse.model_validate(resp)
    else:
        assert fx["status"] == ok_status
        resp_model.model_validate(resp)


def test_claim_fixture_input_and_checkpoint_parse():
    job = _load(FIXTURES / "claim_rfi.json")["response"]["jobs"][0]
    seed = p.parse_input(job["agent_type"], job["input"])
    assert isinstance(seed, p.RfiFromEmailInput)
    p.RfiCheckpoint.model_validate(job["checkpoint"])


def test_artifact_envelopes_validate():
    p.RfiDetailEnvelope.model_validate(_load(FIXTURES / "artifact_rfi_detail_envelope.json")["body"])
    p.RfiListEnvelope.model_validate(_load(FIXTURES / "artifact_rfi_list_envelope.json")["body"])


def test_turn_actions_in_fixtures_are_valid_tool_calls():
    for name in ("turn_first", "turn_action_navigate"):
        action = _load(FIXTURES / f"{name}.json")["response"]["action"]
        p.validate_tool_call("rfi_from_email", action["tool"], action["args"])


def test_no_dash_characters_in_fixtures():
    for f in FILES:
        text = f.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, f.name


# Secrets ---------------------------------------------------------------------------


def test_secrets_are_masked_except_on_the_wire():
    fx = _load(FIXTURES / "login_begin_granted.json")["response"]
    resp = p.LoginBeginResponse.model_validate(fx)
    assert "<secret-in-fixture>" not in repr(resp)
    assert "<secret-in-fixture>" not in resp.model_dump_json()
    assert json.loads(resp.wire_json())["password"] == "<secret-in-fixture>"
    use = p.HelpSecretUseResponse.model_validate({"value": "123456"})
    assert "123456" not in repr(use) and json.loads(use.wire_json())["value"] == "123456"


def test_cookie_repr_hides_value():
    c = p.Cookie(name="s", value="topsecret", domain=".procore.com")
    assert "topsecret" not in repr(c) and "topsecret" not in str(c)


def test_login_shapes_enforced():
    with pytest.raises(ValidationError):
        p.LoginResultRequest(attempt_id="00000000-0000-4000-8000-000000000001", outcome="ok")
    with pytest.raises(ValidationError):
        p.LoginBeginResponse(granted=False, password="x", reason="spacing")
    with pytest.raises(ValidationError):
        p.SessionResponse(status="ok")


def test_finish_shapes_enforced():
    with pytest.raises(ValidationError):
        p.FinishRequest(status="succeeded", summary="x")
    with pytest.raises(ValidationError):
        p.FinishRequest(status="failed", summary="x")
    p.FinishRequest(status="canceled", summary="stop")


def test_requests_forbid_unknown_fields_and_responses_ignore_them():
    with pytest.raises(ValidationError):
        p.ClaimRequest(boot_id="00000000-0000-4000-8000-000000000001", capacity={}, extra=1)
    p.ClaimResponse.model_validate({"jobs": [], "new_server_field": 1})


def test_worker_step_kinds():
    assert p.worker_step_kind_allowed("tool.navigate")
    assert p.worker_step_kind_allowed("http.blocked")
    assert p.worker_step_kind_allowed("nav")
    assert not p.worker_step_kind_allowed("claim")
    assert not p.worker_step_kind_allowed("llm.turn")
    assert not p.worker_step_kind_allowed("Tool.x")
    with pytest.raises(ValidationError):
        p.StepIn(idem_key="k", kind="help.opened", actor="worker", summary="x")


# Tools ------------------------------------------------------------------------------


def test_every_tool_has_schema_and_model():
    assert set(p.TOOL_SCHEMAS) == set(p.TOOL_ARG_MODELS)
    for tools in p.TOOLS_BY_TYPE.values():
        assert set(tools) <= set(p.TOOL_SCHEMAS)
    assert not set(p.FORBIDDEN_TOOL_NAMES) & set(p.TOOL_SCHEMAS)


def test_o1_tool_set_for_rfi_type():
    tools = set(p.TOOLS_BY_TYPE["rfi_from_email"])
    for name in ("ensure_login", "procore_resolve_company", "procore_list_rfis", "capture_rfi_detail",
                 "navigate", "read_page", "request_help", "attach_helper_file", "add_helper_note", "finish"):
        assert name in tools
    assert "ensure_login" not in p.TOOLS_BY_TYPE["canary"]


@pytest.mark.parametrize("name", sorted(p.TOOL_SCHEMAS))
def test_schema_matches_args_model(name):
    schema = p.TOOL_SCHEMAS[name]
    assert schema["name"] == name
    params = schema["parameters"]
    assert params["type"] == "object" and params["additionalProperties"] is False
    model = p.TOOL_ARG_MODELS[name]
    assert set(params["properties"]) == set(model.model_fields)
    required = {k for k, f in model.model_fields.items() if f.is_required()}
    assert set(params["required"]) == required
    json.dumps(schema)   # serializable as-is


def test_validate_tool_call():
    p.validate_tool_call("rfi_from_email", "capture_rfi_detail",
                         {"host": "app.procore.com", "project_id": "1", "rfi_id": "2", "company_id": "3"})
    with pytest.raises(p.ToolCallInvalid) as e:
        p.validate_tool_call("canary", "ensure_login", {"host": "app.procore.com"})
    assert e.value.reason == "tool_not_allowed"
    with pytest.raises(p.ToolCallInvalid):
        p.validate_tool_call("rfi_from_email", "navigate", {"url": "http://app.procore.com/"})
    with pytest.raises(p.ToolCallInvalid):
        p.validate_tool_call("rfi_from_email", "click", {"ref": "ref_1", "extra": 1})
    with pytest.raises(p.ToolCallInvalid):
        p.validate_tool_call("rfi_from_email", "finish", {"outcome": "cannot_complete", "summary": "x"})


def test_finish_to_job():
    args = p.FinishArgs(outcome="captured", summary="ok")
    assert p.finish_to_job("rfi_from_email", args) == {"action": "rfi_capture"}
    with pytest.raises(p.ToolCallInvalid):
        p.finish_to_job("canary", args)
    assert p.finish_to_job("canary", p.FinishArgs(outcome="nothing_to_do", summary="ok"))["outcome"] == "self_test_ok"
    failed = p.finish_to_job("rfi_from_email", p.FinishArgs(outcome="cannot_complete", summary="x", error_kind="not_found"))
    assert failed == {"action": "finish", "status": "failed", "error_kind": "not_found"}


def test_help_scope_key():
    j = "00000000-0000-4000-8000-000000000001"
    assert p.help_scope_key("mfa", identity="procore", project_id=None, job_id=j) == "login:procore"
    assert p.help_scope_key("access_denied", identity="procore", project_id="pid", job_id=j) == "link:pid"
    assert p.help_scope_key("ambiguous", identity="procore", project_id="pid", job_id=j) == f"job:{j}"


def test_manifest_canonical_bytes_are_stable():
    m = {"min_version": "1.0.0", "created_at": "2026-10-18T20:00:00Z", "version": "1.0.0", "sha256": "a" * 64}
    assert p.canonical_manifest_bytes(m) == (
        b'{"created_at":"2026-10-18T20:00:00Z","min_version":"1.0.0","sha256":"' + b"a" * 64 + b'","version":"1.0.0"}'
    )


def test_models_are_pydantic():
    for _, (_, _, req, resp, _) in p.ENDPOINTS.items():
        for m in (req,) + (resp if isinstance(resp, tuple) else (resp,)):
            assert m is None or issubclass(m, BaseModel)
