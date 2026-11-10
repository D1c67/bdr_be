"""Assistant browser v2 protocol additions (docs/ASSISTANT_BROWSER_V2_PLAN.md
15.3, WI-2). The new user_browser type gets its own tools, argument models,
wire models and help vocabulary; nothing shared with the org agent
(rfi_from_email), the canary or the retired user_browser_read changes."""

import json
import re
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.browser_agents import protocol as p

FIXTURES = Path(__file__).resolve().parent / "fixtures_browser_agents"
V2_FILES = sorted(f for f in FIXTURES.glob("user_v2_*.json") if f.stem != "user_v2_tool_calls")

JOB = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
HOLD = "40000000-0000-4000-8000-000000000001"
CRED = "c0000000-0000-4000-8000-000000000001"
ATT = "d1d1d1d1-0000-4000-8000-000000000002"
A = "a" * 64
B = "b" * 64

NEW_MODELS = (
    "UserBrowserInput", "UserJobPolicy", "UserHoldRequest", "UserHoldRelease", "UserCredentialFillRequest",
    "UserSessionPoll", "UserDownloadMeta", "UserSiteHoldRequest", "LiveFrameMeta", "LiveInputEvent",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture(stem: str) -> dict:
    return _load(FIXTURES / f"{stem}.json")["body"]


# Fixtures ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", V2_FILES, ids=lambda f: f.stem)
def test_v2_fixture_validates_against_its_model_and_route(path):
    fx = _load(path)
    assert "endpoint" not in fx            # ENDPOINTS fixtures stay for the v1 table
    model = getattr(p, fx["model"])
    model.model_validate(fx["body"])
    if fx["v2_endpoint"] is None:
        return
    method, template, req, resp, _status = p.USER_V2_ENDPOINTS[fx["v2_endpoint"]]
    assert template.startswith("/jobs/{job_id}/")
    if fx["direction"] in ("request", "query"):
        assert req is model
    else:
        assert resp is model or (isinstance(resp, tuple) and model in resp)


def test_every_new_model_has_a_fixture():
    covered = {_load(f)["model"] for f in V2_FILES}
    for name in NEW_MODELS:
        if name == "LiveInputEvent":
            assert "LiveInputBatch" in covered
            continue
        assert name in covered, name
    covered_routes = {_load(f)["v2_endpoint"] for f in V2_FILES}
    assert set(p.USER_V2_ENDPOINTS) <= covered_routes, sorted(set(p.USER_V2_ENDPOINTS) - covered_routes)


def test_tool_call_fixture_is_valid_for_user_browser():
    fx = _load(FIXTURES / "user_v2_tool_calls.json")
    assert fx["agent_type"] == "user_browser"
    for call in fx["calls"]:
        p.validate_tool_call("user_browser", call["tool"], call["args"])


def test_secret_fixture_values_are_masked():
    resp = p.UserCredentialFillResponse.model_validate(_fixture("user_v2_login_begin_granted"))
    assert "<secret-in-fixture>" not in repr(resp)
    assert "estimator@g3electrical.example" not in repr(resp)
    assert "<secret-in-fixture>" not in resp.model_dump_json()
    wire = json.loads(resp.wire_json())
    assert wire["password"] == "<secret-in-fixture>"
    assert wire["username"] == "estimator@g3electrical.example"


def test_v2_routes_are_apart_from_the_v1_table():
    assert not set(p.USER_V2_ENDPOINTS) & set(p.ENDPOINTS)
    v1_paths = {(v[0], v[1]) for v in p.ENDPOINTS.values()}
    shared = {(v[0], v[1]) for v in p.USER_V2_ENDPOINTS.values()} & v1_paths
    # Only the user help route keeps its path; WI-6 swaps its body model.
    assert shared == {("POST", "/jobs/{job_id}/user/help")}


# Shared items untouched ---------------------------------------------------------------


def test_shared_regexes_unchanged():
    assert p.SECRET_INPUT_RE.pattern == r"(?i)otp|passcode|mfa|verification|2fa|code|password"
    assert p.TOKEN_QUERY_KEY_RE.pattern == r"(?i)token|sig|signature|key|auth|code|session|passport|x-amz-"
    assert p.SECRET_INPUT_RE.search("Zip code")    # the org agent still refuses it


def test_shared_help_vocabulary_unchanged():
    assert typing.get_args(p.HelpReasonCode) == (
        "login_failed", "login_locked", "captcha", "mfa", "sso", "logged_out", "access_denied",
        "not_found", "ambiguous", "unexpected_page", "cap_reached", "needs_info", "provider_config", "other",
    )
    assert p.LOGIN_HELP_REASONS == ("login_failed", "login_locked", "mfa", "captcha", "sso", "logged_out")
    assert p.MODEL_HELP_REASONS == ("access_denied", "not_found", "ambiguous", "unexpected_page", "needs_info", "other")
    with pytest.raises(ValidationError):
        p.RequestHelpArgs(reason_code="needs_credential", message="x")
    with pytest.raises(ValidationError):
        p.HelpOpenRequest(reason_code="push")


def test_old_tool_sets_and_models_unchanged():
    assert p.TOOLS_BY_TYPE["rfi_from_email"] == (
        p.SITE_TOOLS_PROCORE + p.BROWSER_TOOLS + p.HELP_TOOLS + p.FINISH_TOOLS)
    assert p.TOOLS_BY_TYPE["canary"] == ("navigate", "read_page", "find_text", "wait_for", "finish")
    assert p.TOOLS_BY_TYPE["user_browser_read"] == (
        "ensure_login", "navigate", "read_page", "find_text", "scroll", "go_back", "wait_for",
        "request_help", "reply_help", "finish")
    assert p.TOOL_ARG_MODELS["request_help"] is p.RequestHelpArgs
    assert p.TOOL_ARG_MODELS["finish"] is p.FinishArgs
    assert p.TOOL_ARG_MODELS["navigate"] is p.NavigateArgs
    assert p.TOOL_ARG_MODELS["type_text"] is p.TypeTextArgs
    assert p.FORBIDDEN_TOOL_NAMES == ("fill_secret", "evaluate", "upload_file", "download", "new_tab")


@pytest.mark.parametrize("agent_type", ["rfi_from_email", "canary", "user_browser_read"])
@pytest.mark.parametrize("tool,args", [
    ("switch_tab", {"tab_id": "tab_1"}),
    ("fill_credential", {"ref": "ref_1", "field": "password"}),
    ("user_request_help", {"reason_code": "needs_site", "message": "x"}),
    ("user_finish", {"outcome": "completed", "result": "x"}),
])
def test_new_tools_do_not_widen_old_types(agent_type, tool, args):
    with pytest.raises(p.ToolCallInvalid) as e:
        p.validate_tool_call(agent_type, tool, args)
    assert e.value.reason == "tool_not_allowed"


def test_old_ceilings_unchanged():
    assert p.CEILINGS["rfi_from_email"]["nav_hosts"] == ["app.procore.com", "us02.procore.com"]
    assert p.CEILINGS["user_browser_read"]["login_hosts"] == ["login.procore.com"]
    assert p.CEILINGS["canary"]["nav_hosts"] == ["$api_host"]
    assert p.INPUT_MODELS["user_browser_read"] is p.UserBrowserReadInput


def test_manifest_bytes_unchanged():
    m = {"min_version": "1.0.5", "created_at": "2026-11-02T20:00:00Z", "version": "1.1.0", "sha256": "c" * 64}
    assert p.canonical_manifest_bytes(m) == (
        b'{"created_at":"2026-11-02T20:00:00Z","min_version":"1.0.5","sha256":"' + b"c" * 64 + b'","version":"1.1.0"}'
    )


# Type plumbing ---------------------------------------------------------------------


def test_user_browser_tool_set():
    tools = p.TOOLS_BY_TYPE["user_browser"]
    assert tools == ("navigate", "read_page", "find_text", "scroll", "go_back", "wait_for", "click", "type_text",
                     "select_option", "press_key", "switch_tab", "fill_credential", "user_request_help",
                     "user_finish")
    for name in ("request_help", "reply_help", "finish", "ensure_login", "attach_helper_file") + p.FORBIDDEN_TOOL_NAMES:
        assert name not in tools
    assert [t["name"] for t in p.tools_for("user_browser")] == list(tools)


def test_user_browser_ceiling_is_policy_driven():
    c = p.CEILINGS["user_browser"]
    assert c["nav_hosts"] == [] and c["nav_paths"] == [] and c["resource_hosts"] == []
    assert c["identity_provider_hosts"] == list(p.IDENTITY_PROVIDER_HOSTS)
    assert c["multi_tenant_domains"] == list(p.MULTI_TENANT_DOMAINS)
    assert ".local" in c["blocked_host_suffixes"] and ".home.arpa" in c["blocked_host_suffixes"]
    assert all(isinstance(v, list) for v in c.values())


def _authority(admission_id: str = JOB, **over) -> dict:
    return {"role": "it_admin", "model": "gpt-5.4", "conversation_id": HOLD, "max_cost_usd": 5,
            "admission_id": admission_id, **over}


def test_parse_input_user_browser():
    seed = p.parse_input("user_browser", _fixture("user_v2_input"))
    assert isinstance(seed, p.UserBrowserInput)
    assert seed.authority.max_cost_usd == 5 and seed.project_id is None
    p.UserBrowserInput(admission_id=JOB, goal="Look up the bid due date.", start_site_key="smartbid",
                       authority=_authority())
    with pytest.raises(ValidationError):
        p.UserBrowserInput(admission_id=JOB, goal="x", authority=_authority())          # no start
    with pytest.raises(ValidationError):
        p.UserBrowserInput(admission_id=JOB, goal="x", start_url="javascript:alert(1)", authority=_authority())
    with pytest.raises(ValidationError):
        p.UserBrowserInput(admission_id=JOB, goal="x", start_site_key="procore", credential_id=CRED,
                           authority=_authority())
    with pytest.raises(ValidationError):
        p.UserBrowserInput(admission_id=JOB, goal="x" * (p.USER_GOAL_MAX + 1), start_site_key="procore",
                           authority=_authority())
    with pytest.raises(ValidationError):
        p.UserBrowserInput(admission_id=JOB, goal="x", start_site_key="procore")        # no authority
    with pytest.raises(ValidationError):
        p.UserBrowserInput(admission_id=JOB, goal="x", start_site_key="procore", authority=_authority(HOLD))
    with pytest.raises(ValidationError):
        p.UserBrowserInput(admission_id=JOB, goal="x", start_site_key="procore",
                           authority=_authority(max_cost_usd=5.01))


def test_input_matches_the_enqueue_v2_shape():
    """The keys assistant_browser_task_enqueue_v2 (0192 A2.5) writes into
    jobs.input with p_input = {} are exactly the model's fields."""
    sql = (Path(__file__).resolve().parents[1] / "supabase/migrations/0192_assistant_browser_v2.sql").read_text(encoding="utf-8")
    m = re.search(r"v_input := coalesce\(p_input, '\{\}'::jsonb\) \|\| jsonb_build_object\((.+?)\);\n", sql, re.S)
    assert m
    top, _, inner = m.group(1).partition("'authority', jsonb_build_object(")
    assert set(re.findall(r"'([a-z_]+)',", top)) == set(p.UserBrowserInput.model_fields) - {"authority"}
    assert set(re.findall(r"'([a-z_]+)',", inner)) == set(p.UserJobAuthority.model_fields)
    body = dict(_fixture("user_v2_input"), project_id="5e5e5e5e-0000-4000-8000-000000000001", start_url=None)
    assert p.UserBrowserInput.model_validate(body).start_url is None


# Secret field regex for the user type ------------------------------------------------


@pytest.mark.parametrize("label", [
    "Password", "Passcode", "One-time code", "Onetime password", "OTP", "MFA code", "2FA",
    "Verification code", "Security code", "new_password",
])
def test_user_secret_input_re_refuses_secret_fields(label):
    assert p.USER_SECRET_INPUT_RE.search(label)


@pytest.mark.parametrize("label", ["Zip code", "Cost code", "Project code", "Postal code", "Promo code", "Email"])
def test_user_secret_input_re_allows_ordinary_code_fields(label):
    assert not p.USER_SECRET_INPUT_RE.search(label)


# Help reasons ------------------------------------------------------------------------


def test_user_help_reason_code():
    values = typing.get_args(p.UserHelpReasonCode)
    assert set(typing.get_args(p.HelpReasonCode)) < set(values)
    assert set(values) - set(typing.get_args(p.HelpReasonCode)) == set(p.USER_HELP_REASONS_ADDED)
    assert len(values) == 19 and len(set(values)) == 19
    assert set(p.USER_MODEL_HELP_REASONS) <= set(values)
    assert set(typing.get_args(p.UserRequestHelpArgs.model_fields["reason_code"].annotation)) == set(
        p.USER_MODEL_HELP_REASONS)
    assert "push" not in p.USER_MODEL_HELP_REASONS and "hold" not in p.USER_MODEL_HELP_REASONS
    schema_enum = p.TOOL_SCHEMAS["user_request_help"]["parameters"]["properties"]["reason_code"]["enum"]
    assert schema_enum == list(p.USER_MODEL_HELP_REASONS)


def test_user_request_help_args():
    p.validate_tool_call("user_browser", "user_request_help", {"reason_code": "mfa_step_up", "message": "Code asked."})
    for reason in ("push", "hold", "login_failed", "cap_reached"):
        with pytest.raises(p.ToolCallInvalid):
            p.validate_tool_call("user_browser", "user_request_help", {"reason_code": reason, "message": "x"})


def test_help_open_v2_shapes():
    p.UserHelpOpenV2Request(reason_code="needs_site")
    p.UserHelpOpenV2Request(reason_code="hold", hold_id=HOLD)
    p.UserHelpOpenV2Request(reason_code="push", login_attempt_id=ATT, credential_id=CRED)
    with pytest.raises(ValidationError):
        p.UserHelpOpenV2Request(reason_code="hold")
    with pytest.raises(ValidationError):
        p.UserHelpOpenV2Request(reason_code="mfa")
    with pytest.raises(ValidationError):
        p.UserHelpOpenV2Request(reason_code="mfa_step_up")
    with pytest.raises(ValidationError):
        p.UserHelpOpenV2Request(reason_code="evil")


# Tools ------------------------------------------------------------------------------


def test_fill_credential_args():
    p.validate_tool_call("user_browser", "fill_credential", {"ref": "ref_3", "field": "username"})
    for bad in ({"ref": "ref_3", "field": "otp"}, {"ref": "#pw", "field": "password"},
                {"ref": "ref_3", "field": "password", "value": "hunter2"}):
        with pytest.raises(p.ToolCallInvalid):
            p.validate_tool_call("user_browser", "fill_credential", bad)


def test_switch_tab_args():
    p.validate_tool_call("user_browser", "switch_tab", {"tab_id": "tab_0"})
    for bad in ({"tab_id": "1"}, {"tab_id": "tab_x"}, {}):
        with pytest.raises(p.ToolCallInvalid):
            p.validate_tool_call("user_browser", "switch_tab", bad)


def test_user_finish_args():
    ok = p.validate_tool_call("user_browser", "user_finish", {
        "outcome": "completed", "result": "Done.",
        "facts": [{"text": "Bid due 11/6.", "source_url": "http://bids.example-owner.org/42"}]})
    assert ok.facts[0].source_url.startswith("http://")
    bad_calls = [
        {"outcome": "cannot_complete", "result": "x"},                                   # no error_kind
        {"outcome": "completed", "result": "x", "error_kind": "other"},
        {"outcome": "completed", "result": ""},
        {"outcome": "completed", "result": "x", "facts": [{"text": "t", "source_url": "javascript:alert(1)"}]},
        {"outcome": "completed", "result": "x", "facts": [{"text": "t", "source_url": "https://a.example", "x": 1}]},
        {"outcome": "completed", "result": "x",
         "facts": [{"text": "t", "source_url": "https://a.example"}] * (p.USER_FACTS_MAX + 1)},
        {"outcome": "captured", "result": "x"},
    ]
    for args in bad_calls:
        with pytest.raises(p.ToolCallInvalid):
            p.validate_tool_call("user_browser", "user_finish", args)


def test_user_type_navigate_stays_https_only():
    # The shared navigate model is unchanged (http top-level navigation is
    # reached through links and redirects under the guard, not this tool).
    with pytest.raises(p.ToolCallInvalid):
        p.validate_tool_call("user_browser", "navigate", {"url": "http://example.com/"})
    p.validate_tool_call("user_browser", "navigate", {"url": "https://acc.autodesk.com/projects"})


# Job policy --------------------------------------------------------------------------


def _policy(**over) -> dict:
    data = json.loads(json.dumps(_fixture("user_v2_policy")))
    data.update(over)
    return data


def test_policy_lookup_helpers():
    pol = p.UserJobPolicy.model_validate(_fixture("user_v2_policy"))
    assert pol.site("procore").profile.final_actions[0].label == "Submit RFI response"
    assert pol.credential(CRED).login_origins == ["https://login.procore.com"]
    assert pol.site("nope") is None and pol.credential(JOB) is None
    p.UserJobPolicy.model_validate({**_fixture("user_v2_policy"), "server_added_field": 1})


def test_policy_refuses_identity_provider_login_origin():
    data = _policy()
    data["credentials"][0]["login_origins"] = ["https://login.microsoftonline.com"]
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)
    data["credentials"][0]["login_origins"] = ["https://acme.okta.com"]
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)


def test_policy_refuses_http_login_origin_and_bad_cookie_domain():
    data = _policy()
    data["credentials"][0]["login_origins"] = ["http://login.procore.com"]
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)
    data = _policy()
    data["credentials"][0]["cookie_domains"] = ["localhost"]
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)


@pytest.mark.parametrize("domain", ["google.com", "sites.google.com", "sharepoint.com", "forms.gle"])
def test_policy_refuses_whole_multi_tenant_domain(domain):
    data = _policy()
    data["sites"][1]["registrable_domains"] = [domain]
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)


def test_policy_scope_rules():
    data = _policy()
    data["credentials"][0]["site_key"] = "smartbid"
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)                   # credential outside scope
    data = _policy()
    data["credentials"].append(dict(data["credentials"][0], credential_id=ATT, account_label="second"))
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)                   # two credentials for one site in a job
    data = _policy()
    data["sites"].append(dict(data["sites"][1]))
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)                   # duplicate site
    data = _policy(sites=[])
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)
    data = _policy()
    data["sites"][2]["exact_origins"] = []
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)                   # origin grant without an origin
    data = _policy()
    data["safe_rules"][0]["site_key"] = "smartbid"
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)


@pytest.mark.parametrize("origin", [
    "https://router", "https://APP.procore.com", "https://app.procore.com/", "ftp://app.procore.com",
    "https://user@app.procore.com", "file:///c:/x",
])
def test_policy_origins_are_validated_strings(origin):
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(_policy(start_origin=origin))


def test_policy_regex_must_compile():
    data = _policy()
    data["sites"][0]["profile"]["safe_mutations"][0]["path_regex"] = "^/graphql("
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)
    data = _policy()
    data["sites"][0]["login_paths"] = ["[unclosed"]
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)


def test_policy_caps_cannot_pass_the_floor():
    data = _policy()
    data["caps"]["download_file_bytes"] = p.USER_DOWNLOAD_FILE_BYTES_FLOOR + 1
    with pytest.raises(ValidationError):
        p.UserJobPolicy.model_validate(data)


def test_websocket_ok_counts_only_on_curated_sites():
    data = _policy()
    data["sites"][0]["profile"]["websocket_ok"] = True
    data["sites"][1]["profile"] = {"websocket_ok": True}
    pol = p.UserJobPolicy.model_validate(data)
    assert pol.site("procore").websocket_allowed is True
    assert pol.site("domain:example-supplier.com").websocket_allowed is False


# Holds -------------------------------------------------------------------------------


def test_hold_request_shapes():
    base = _fixture("user_v2_hold_request")
    with pytest.raises(ValidationError):
        p.UserHoldRequest.model_validate({**base, "origin": "https://evil.example.com"})
    with pytest.raises(ValidationError):
        p.UserHoldRequest.model_validate({**_fixture("user_v2_hold_click"), "element_label": None})
    with pytest.raises(ValidationError):
        p.UserHoldRequest.model_validate({**base, "body_summary": [{"name": "passwd", "value": "x", "hidden": True}]})
    with pytest.raises(ValidationError):
        p.UserHoldRequest.model_validate({**base, "body_summary": [{"name": "q", "hidden": False}]})
    with pytest.raises(ValidationError):
        p.UserHoldRequest.model_validate({**base, "password": "x"})
    with pytest.raises(ValidationError):
        p.UserHoldRequest.model_validate({**base, "method": "post"})
    # Non-default ports keep the port in the origin.
    p.UserHoldRequest.model_validate({**base, "url": "https://app.procore.com:8443/x", "origin": "https://app.procore.com:8443"})


def test_site_hold_request_shapes():
    base = _fixture("user_v2_site_hold")
    with pytest.raises(ValidationError):
        p.UserSiteHoldRequest.model_validate({**base, "target_origin": "https://app.procore.com"})
    with pytest.raises(ValidationError):
        p.UserSiteHoldRequest.model_validate({**base, "url": "data:text/html,hi"})


def _token(**over) -> dict:
    t = dict(_fixture("user_v2_hold_released")["token"])
    t.update(over)
    return t


def test_hold_release_shapes():
    p.UserHoldRelease(hold_id=HOLD, status="released", token=_token())
    with pytest.raises(ValidationError):
        p.UserHoldRelease(hold_id=HOLD, status="released")
    with pytest.raises(ValidationError):
        p.UserHoldRelease(hold_id=HOLD, status="released", token=_token(hold_id=ATT))
    with pytest.raises(ValidationError):
        p.UserHoldRelease(hold_id=HOLD, status="declined", token=_token())
    with pytest.raises(ValidationError):
        p.UserHoldRelease(hold_id=HOLD, status="released", token=_token(), site_release="full")
    with pytest.raises(ValidationError):
        p.UserHoldRelease(hold_id=HOLD, kind="site", status="released")
    with pytest.raises(ValidationError):
        p.UserHoldRelease(hold_id=HOLD, kind="site", status="released", site_release="full", token=_token())
    with pytest.raises(ValidationError):
        p.UserHoldRelease(hold_id=HOLD, kind="site", status="declined", site_release="site_only")
    p.UserHoldRelease(hold_id=HOLD, kind="site", status="released", site_release="site_only", policy_version=5)
    p.UserHoldRelease(hold_id=HOLD, status="aborted")


def test_canonical_hold_token_bytes():
    tok = p.UserHoldToken.model_validate(_token(job_id=JOB.upper(), exp=1793664120))
    raw = p.canonical_hold_token_bytes(tok)
    assert raw == (
        b'{"body_sha256":"' + B.encode() + b'","exp":1793664120,"hold_id":"' + HOLD.encode()
        + b'","job_id":"' + JOB.encode() + b'","lease_epoch":3,"method":"POST",'
        b'"origin":"https://app.procore.com","url_sha256":"' + A.encode() + b'"}'
    )
    assert b"signature" not in raw and b"key_id" not in raw
    assert p.canonical_hold_token_bytes(tok.model_dump(mode="json")) == raw
    assert p.canonical_hold_token_bytes(_token(body_sha256="c" * 64)) != raw


def test_hold_token_signs_and_verifies_with_ed25519():
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    import base64
    from cryptography.exceptions import InvalidSignature

    key = ed.Ed25519PrivateKey.generate()
    tok = p.UserHoldToken.model_validate(_token())
    sig = base64.b64encode(key.sign(p.canonical_hold_token_bytes(tok))).decode()
    signed = p.UserHoldToken.model_validate({**tok.model_dump(mode="json"), "signature": sig})
    key.public_key().verify(base64.b64decode(signed.signature), p.canonical_hold_token_bytes(signed))
    other = p.UserHoldToken.model_validate({**signed.model_dump(mode="json"), "url_sha256": "c" * 64})
    with pytest.raises(InvalidSignature):
        key.public_key().verify(base64.b64decode(other.signature), p.canonical_hold_token_bytes(other))


# Credentials -------------------------------------------------------------------------


def test_credential_fill_request_floor():
    p.UserCredentialFillRequest.model_validate(_fixture("user_v2_login_begin"))
    for over in ({"frame_origin": "https://evil.example.com"}, {"origin": "http://login.procore.com",
                                                                "frame_origin": "http://login.procore.com"},
                 {"origin": "https://accounts.google.com", "frame_origin": "https://accounts.google.com"},
                 {"username": "x"}):
        with pytest.raises(ValidationError):
            p.UserCredentialFillRequest.model_validate({**_fixture("user_v2_login_begin"), **over})


def test_credential_fill_response_shapes():
    with pytest.raises(ValidationError):
        p.UserCredentialFillResponse(granted=True, attempt_id=ATT, password="x")
    with pytest.raises(ValidationError):
        p.UserCredentialFillResponse(granted=False, reason="spacing", password="x")
    with pytest.raises(ValidationError):
        p.UserCredentialFillResponse(granted=False, reason="spacing", username="x")
    with pytest.raises(ValidationError):
        p.UserCredentialFillResponse(granted=False)
    with pytest.raises(ValidationError):
        p.UserCredentialFillResponse(granted=False, reason="made_up")
    refused = p.UserCredentialFillResponse.model_validate(_fixture("user_v2_login_begin_busy"))
    assert refused.reason == "login_busy"


def test_credential_result_shapes():
    base = _fixture("user_v2_login_result_ok")
    p.UserCredentialResultRequest.model_validate(base)
    with pytest.raises(ValidationError):
        p.UserCredentialResultRequest.model_validate({**base, "cookies": None})
    with pytest.raises(ValidationError):
        p.UserCredentialResultRequest.model_validate({**base, "outcome": "push"})
    assert "push" in typing.get_args(p.UserLoginOutcome)
    assert "push" not in typing.get_args(p.LoginOutcome)        # shared vocabulary untouched
    ok = p.UserCredentialResultRequest.model_validate(base)
    assert "<secret-in-fixture>" not in repr(ok)
    resp = p.UserCredentialResultResponse.model_validate(_fixture("user_v2_login_result_response"))
    assert resp.attempt_open and resp.help_reason == "push"


def test_session_poll_shape():
    poll = p.UserSessionPoll.model_validate(_fixture("user_v2_session_poll"))
    assert poll.origin is None
    with_origin = p.UserSessionPoll.model_validate({**_fixture("user_v2_session_poll"), "origin": "https://app.procore.com"})
    assert with_origin.origin == "https://app.procore.com"
    with pytest.raises(ValidationError):
        p.UserSessionPoll(credential_id=CRED)
    with pytest.raises(ValidationError):
        p.UserSessionPoll.model_validate({**_fixture("user_v2_session_poll"), "origin": "http://app.procore.com"})


def test_policy_from_the_sql_shape():
    """assistant_browser_job_policy output (fixture built from the 0192 A3.4
    jsonb) parses, including a site-key-only start and an origin grant."""
    pol = p.UserJobPolicy.model_validate(_fixture("user_v2_policy_sql"))
    assert pol.start_origin == "https://procore.com"
    assert pol.site("origin:docs.google.com").scope == "origin"
    assert [str(c.credential_id) for c in pol.credentials] == [CRED]


def test_login_begin_v2_refusals_are_protocol_values():
    sql = (Path(__file__).resolve().parents[1] / "supabase/migrations/0192_assistant_browser_v2.sql").read_text(encoding="utf-8")
    m = re.search(r"create or replace function public\.assistant_browser_login_begin_v2\(.+?\nend \$\$;", sql, re.S)
    assert m
    codes = set(re.findall(r'"code":"([a-z_]+)"', m.group(0))) | set(re.findall(r"'code', '([a-z_]+)'", m.group(0)))
    allowed = set(typing.get_args(p.UserLoginRefusal))
    assert codes and codes <= allowed, sorted(codes - allowed)


# Downloads ---------------------------------------------------------------------------


def test_download_meta_shapes():
    base = _fixture("user_v2_download_meta")
    for over in ({"filename": "../../evil.pdf"}, {"filename": "a\\b.pdf"}, {"filename": ""},
                 {"size_bytes": p.USER_DOWNLOAD_FILE_BYTES_FLOOR + 1}, {"size_bytes": 0},
                 {"scan": "skipped"}, {"sniffed_kind": "exe"}, {"source_origin": "https://intranet"}):
        with pytest.raises(ValidationError):
            p.UserDownloadMeta.model_validate({**base, **over})


# Live view ---------------------------------------------------------------------------


def test_live_frame_meta_shapes():
    base = _fixture("user_v2_live_frame")
    with pytest.raises(ValidationError):
        p.LiveFrameMeta.model_validate({**base, "size_bytes": p.USER_LIVE_FRAME_MAX_BYTES + 1})
    with pytest.raises(ValidationError):
        p.LiveFrameMeta.model_validate({**base, "mime_type": "image/png"})


def test_live_input_event_shapes_and_repr():
    ev = p.LiveInputEvent(seq=9, type="char", text="s")
    assert "'s'" not in repr(ev) and "text" not in repr(ev) and "text" not in str(ev)
    key = p.LiveInputEvent(seq=10, type="keyDown", key="Enter", code="Enter")
    assert "Enter" not in repr(key)
    for bad in ({"seq": 1, "type": "mouseMoved"}, {"seq": 1, "type": "char"}, {"seq": 1, "type": "keyDown"},
                {"seq": 1, "type": "keyDown", "key": "a", "x": 1, "y": 1},
                {"seq": 1, "type": "mousePressed", "x": 1, "y": 1, "text": "a"},
                {"seq": 1, "type": "char", "text": "toolong"},
                {"seq": 1, "type": "evaluate", "x": 1, "y": 1}):
        with pytest.raises(ValidationError):
            p.LiveInputEvent.model_validate(bad)
    with pytest.raises(ValidationError):
        p.LiveInputBatch.model_validate({"events": [{"seq": i, "type": "mouseMoved", "x": 1, "y": 1}
                                                    for i in range(p.USER_LIVE_INPUT_BATCH_MAX + 1)]})


# Floor helpers -----------------------------------------------------------------------


def test_identity_provider_and_multi_tenant_helpers():
    assert p.is_identity_provider_host("login.microsoftonline.com")
    assert p.is_identity_provider_host("G3Electrical.okta.com")
    assert not p.is_identity_provider_host("okta.com")
    assert not p.is_identity_provider_host("okta.com.evil.example")
    assert not p.is_identity_provider_host("login.procore.com")
    assert p.is_multi_tenant_domain("docs.google.com")
    assert not p.is_multi_tenant_domain("notgoogle.com")
    assert not p.is_multi_tenant_domain("procore.com")


@pytest.mark.parametrize("url,origin", [
    ("https://App.Procore.com/x?y=1", "https://app.procore.com"),
    ("https://app.procore.com:443/x", "https://app.procore.com"),
    ("http://example.com:80/", "http://example.com"),
    ("http://example.com:8080/a", "http://example.com:8080"),
    ("javascript:alert(1)", None),
    ("https://[::1", None),
    ("ftp://example.com/", None),
])
def test_url_origin(url, origin):
    assert p.url_origin(url) == origin


def test_no_dash_characters_in_protocol_or_v2_fixtures():
    text = Path(p.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text
    for f in FIXTURES.glob("user_v2_*.json"):
        raw = f.read_text(encoding="utf-8")
        assert chr(0x2014) not in raw and chr(0x2013) not in raw, f.name


def test_hold_stub_signs_the_protocol_canonical_bytes():
    """assistant_browser_holds.canonical_claims delegates to
    protocol.canonical_hold_token_bytes, so server and worker cannot drift."""
    from datetime import datetime, timezone

    from app.services import assistant_browser_holds as holds

    claims = holds.ReleaseClaims(JOB.upper(), 3, HOLD, "POST", "https://app.procore.com", A, B,
                                 datetime(2026, 11, 2, 18, 0, 0, 900000, tzinfo=timezone.utc))
    token = {"job_id": JOB, "lease_epoch": 3, "hold_id": HOLD, "method": "POST", "origin": "https://app.procore.com",
             "url_sha256": A, "body_sha256": B, "exp": int(claims.exp.timestamp())}
    assert holds.canonical_claims(claims) == p.canonical_hold_token_bytes(token)
