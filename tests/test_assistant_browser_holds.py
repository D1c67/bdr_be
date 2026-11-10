"""Final-action and site holds (docs/ASSISTANT_BROWSER_V2_PLAN.md 5.4, 6.5, 6.6):
Ed25519 release tokens over protocol.canonical_hold_token_bytes, the human
decision, the release long poll (deliver once), site holds and the worker
routes. No database: repo calls are monkeypatched per test."""
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core import config as app_config
from app.services import assistant_browser_grants as grants
from app.services import assistant_browser_holds as holds
from app.services import assistant_browser_records as records
from app.services import assistant_browser_runtime as runtime
from app.services import assistant_browser_vault as vault
from app.services.browser_agents import protocol

SEED = b"s" * 32
KEK = base64.b64encode(b"k" * 32).decode()
URL = "https://app.procore.com/rest/v1/rfis/12/responses?draft=0"
SHA_URL = hashlib.sha256(URL.encode()).hexdigest()
SHA_BODY = hashlib.sha256(b"answer=yes").hexdigest()


@pytest.fixture
def keys(monkeypatch):
    settings = SimpleNamespace(browser_hold_signing_key=base64.b64encode(SEED).decode(),
                               browser_hold_signing_key_id="h1", user_cred_kek=KEK, user_cred_kek_id="u1",
                               user_cred_old_keys="", assistant_browser_enabled=True,
                               assistant_browser_v2_enabled=True)
    monkeypatch.setattr(app_config, "get_settings", lambda: settings)
    monkeypatch.setattr(vault, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    return settings


def _public() -> bytes:
    from cryptography.hazmat.primitives import serialization
    return Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _claims(**over):
    data = dict(job_id=str(uuid4()), lease_epoch=3, hold_id=str(uuid4()), method="POST",
                origin="https://app.procore.com", url_sha256=SHA_URL, body_sha256=SHA_BODY,
                exp=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=120))
    data.update(over)
    return holds.ReleaseClaims(**data)


# ── Tokens ───────────────────────────────────────────────────────────────────


def test_token_signs_the_canonical_bytes_and_verifies(keys):
    claims = _claims()
    token = holds.sign_release(claims)
    assert token.key_id == "h1" and token.exp == int(claims.exp.timestamp())
    assert holds.public_key_bytes() == _public()
    back = holds.verify_release(token, _public())
    assert back == claims
    # The signature is over exactly protocol.canonical_hold_token_bytes.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(_public()).verify(base64.b64decode(token.signature),
                                                         protocol.canonical_hold_token_bytes(token))
    assert holds.canonical_claims(claims) == protocol.canonical_hold_token_bytes(token)


@pytest.mark.parametrize("field,value", [("body_sha256", "0" * 64), ("url_sha256", "1" * 64),
                                         ("hold_id", str(uuid4())), ("lease_epoch", 4), ("method", "PUT"),
                                         ("origin", "https://evil.example.com"), ("job_id", str(uuid4()))])
def test_a_token_for_anything_else_is_refused(keys, field, value):
    token = holds.sign_release(_claims())
    forged = protocol.UserHoldToken.model_validate({**token.model_dump(mode="json"), field: value})
    with pytest.raises(ValueError, match="bad_signature"):
        holds.verify_release(forged, _public())


def test_a_token_arriving_after_exp_is_refused(keys):
    claims = _claims()
    token = holds.sign_release(claims)
    with pytest.raises(ValueError, match="expired"):
        holds.verify_release(token, _public(), now=claims.exp + timedelta(seconds=1))
    with pytest.raises(ValueError, match="expired"):
        holds.verify_release(token, _public(), now=claims.exp)
    holds.verify_release(token, _public(), now=claims.exp - timedelta(seconds=1))


def test_a_token_living_longer_than_the_ttl_is_refused(keys):
    token = holds.sign_release(_claims(exp=datetime.now(timezone.utc) + timedelta(hours=1)))
    with pytest.raises(ValueError, match="exp_too_far"):
        holds.verify_release(token, _public())


def test_another_key_is_refused(keys):
    token = holds.sign_release(_claims())
    from cryptography.hazmat.primitives import serialization
    other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw,
                                                                   serialization.PublicFormat.Raw)
    with pytest.raises(ValueError, match="bad_signature"):
        holds.verify_release(token, other)


def test_missing_signing_key_refuses_to_sign(keys):
    keys.browser_hold_signing_key = ""
    with pytest.raises(holds.HoldRefused) as err:
        holds.sign_release(_claims())
    assert err.value.code == "signing_unavailable"


# ── Human decision ───────────────────────────────────────────────────────────


def _hold(**over):
    data = {"id": str(uuid4()), "user_id": "owner", "job_id": str(uuid4()), "kind": "final_action",
            "status": "open", "lease_epoch": 5, "method": "POST", "origin": "https://app.procore.com",
            "url_sha256": SHA_URL, "body_sha256": SHA_BODY, "label": "Submit RFI response",
            "help_id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat()}
    data.update(over)
    return data


def test_approve_signs_a_token_bound_to_the_hold(keys, monkeypatch):
    hold = _hold()
    seen = {}
    monkeypatch.setattr(records.repo, "browser_hold", lambda u, h: hold)
    def decide(user_id, hold_id, decision, token=None, token_exp=None):
        seen.update(decision=decision, token=token, exp=token_exp)
        return {"ok": True, "hold": {**hold, "status": "approved"}}
    monkeypatch.setattr(records.repo, "browser_hold_decide", decide)
    out = holds.decide("owner", hold["id"], "approve")
    assert out["hold"]["status"] == "approved" and seen["decision"] == "approve"
    token = protocol.UserHoldToken.model_validate_json(seen["token"])
    claims = holds.verify_release(token, _public())
    assert (claims.job_id, claims.lease_epoch, claims.hold_id) == (hold["job_id"], 5, hold["id"])
    assert (claims.url_sha256, claims.body_sha256, claims.method) == (SHA_URL, SHA_BODY, "POST")
    assert seen["exp"] == claims.exp and claims.exp <= datetime.now(timezone.utc) + timedelta(seconds=121)


@pytest.mark.parametrize("kind,decision", [("final_action", "decline"), ("site", "approve"),
                                           ("site", "approve_site_only")])
def test_decline_and_site_decisions_carry_no_token(keys, monkeypatch, kind, decision):
    hold = _hold(kind=kind)
    seen = {}
    monkeypatch.setattr(records.repo, "browser_hold", lambda u, h: hold)
    monkeypatch.setattr(records.repo, "browser_hold_decide",
                        lambda u, h, d, token=None, token_exp=None: seen.update(token=token) or {"ok": True})
    holds.decide("owner", hold["id"], decision)
    assert seen["token"] is None


@pytest.mark.parametrize("code", ["hold_aborted", "hold_expired", "hold_changed", "forbidden"])
def test_decision_refusals_carry_the_rpc_code(keys, monkeypatch, code):
    monkeypatch.setattr(records.repo, "browser_hold", lambda u, h: _hold())
    monkeypatch.setattr(records.repo, "browser_hold_decide", lambda *a, **kw: {"ok": False, "code": code})
    with pytest.raises(holds.HoldRefused) as err:
        holds.decide("owner", str(uuid4()), "approve")
    assert err.value.code == code and isinstance(err.value, ValueError)


def test_an_aborted_or_unknown_hold_is_refused_before_signing(keys, monkeypatch):
    monkeypatch.setattr(records.repo, "browser_hold_decide", lambda *a, **kw: pytest.fail("no RPC"))
    monkeypatch.setattr(records.repo, "browser_hold", lambda u, h: _hold(status="aborted"))
    with pytest.raises(holds.HoldRefused, match="hold_aborted"):
        holds.decide("owner", str(uuid4()), "approve")
    monkeypatch.setattr(records.repo, "browser_hold", lambda u, h: None)
    with pytest.raises(holds.HoldRefused, match="not_found"):
        holds.decide("owner", str(uuid4()), "decline")
    with pytest.raises(holds.HoldRefused, match="invalid"):
        holds.decide("owner", str(uuid4()), "maybe")


# ── Release long poll (deliver once) ─────────────────────────────────────────


@pytest.fixture
def poll(keys, monkeypatch):
    hold = _hold()
    state = {"status": "pending", "decision": None, "kind": "final_action", "token": None}
    calls = []
    def rpc(owner, name, params):
        calls.append(name)
        if name == "assistant_browser_hold_release_get":
            return {"ok": True, **state}
        if name == "assistant_browser_hold_consume":
            state["status"] = "consumed"
            return {"ok": True}
        if name == "assistant_browser_user_fence_v2":
            return {"id": hold["job_id"], "policy_version": 7}
        return pytest.fail(name)
    monkeypatch.setattr(records.repo, "browser_rpc", rpc)
    monkeypatch.setattr(records.repo, "browser_hold", lambda u, h: hold)
    monkeypatch.setattr(holds.time, "sleep", lambda s: None)
    return SimpleNamespace(hold=hold, state=state, calls=calls)


def _status(p, wait=0):
    return holds.release_status(p.hold["job_id"], "w", "b", 5, p.hold["id"], wait_seconds=wait, user_id="owner")


def test_pending_answers_retry_after(poll):
    out = _status(poll)
    assert out["status"] == "pending" and out["retry_after"] == 1 and out["token"] is None


def test_long_poll_waits_then_answers_pending(poll, monkeypatch):
    ticks = iter([0.0, 0.0, 1.0, 3.0, 26.0, 26.0])
    monkeypatch.setattr(holds.time, "monotonic", lambda: next(ticks))
    out = _status(poll, wait=25)
    assert out["status"] == "pending"
    assert poll.calls.count("assistant_browser_hold_release_get") >= 2


def test_released_token_is_answered_once_and_consumed(poll):
    token = holds.sign_release(holds.ReleaseClaims(
        job_id=poll.hold["job_id"], lease_epoch=5, hold_id=poll.hold["id"], method="POST",
        origin="https://app.procore.com", url_sha256=SHA_URL, body_sha256=SHA_BODY,
        exp=datetime.now(timezone.utc) + timedelta(seconds=100)))
    poll.state.update(status="approved", decision="approve", token=token.model_dump_json())
    out = _status(poll)
    assert out["status"] == "released" and out["token"]["signature"] == token.signature
    assert out["label"] == "Submit RFI response"
    assert poll.calls.count("assistant_browser_hold_consume") == 1
    again = _status(poll)
    assert again["status"] == "aborted" and again["token"] is None


@pytest.mark.parametrize("code,status", [("already_consumed", "aborted"), ("token_expired", "expired")])
def test_a_poll_that_loses_the_consume_never_hands_out_the_token(poll, monkeypatch, code, status):
    """Review finding: two concurrent polls both read the token; only the
    one whose consume succeeds may return it."""
    token = holds.sign_release(holds.ReleaseClaims(
        job_id=poll.hold["job_id"], lease_epoch=5, hold_id=poll.hold["id"], method="POST",
        origin="https://app.procore.com", url_sha256=SHA_URL, body_sha256=SHA_BODY,
        exp=datetime.now(timezone.utc) + timedelta(seconds=100)))
    poll.state.update(status="approved", decision="approve", token=token.model_dump_json())

    def rpc(owner, name, params):
        poll.calls.append(name)
        if name == "assistant_browser_hold_release_get":
            return {"ok": True, **poll.state}
        if name == "assistant_browser_hold_consume":
            return {"ok": False, "code": code}
        return pytest.fail(name)

    monkeypatch.setattr(records.repo, "browser_rpc", rpc)
    out = _status(poll)
    assert out["status"] == status and out["token"] is None
    assert token.signature not in json.dumps(out)


def test_a_token_for_another_epoch_is_never_handed_out(poll):
    token = holds.sign_release(holds.ReleaseClaims(
        job_id=poll.hold["job_id"], lease_epoch=4, hold_id=poll.hold["id"], method="POST",
        origin="https://app.procore.com", url_sha256=SHA_URL, body_sha256=SHA_BODY,
        exp=datetime.now(timezone.utc) + timedelta(seconds=100)))
    poll.state.update(status="approved", decision="approve", token=token.model_dump_json())
    with pytest.raises(holds.HoldRefused):
        _status(poll)
    assert "assistant_browser_hold_consume" not in poll.calls


def test_approved_but_token_lapsed_reads_as_expired(poll):
    poll.state.update(status="approved", decision="approve", token=None)
    assert _status(poll)["status"] == "expired"


@pytest.mark.parametrize("status", ["declined", "expired", "aborted"])
def test_final_states_pass_through(poll, status):
    poll.state.update(status=status)
    out = _status(poll)
    assert out["status"] == status and out["token"] is None


@pytest.mark.parametrize("decision,release", [("approve", "full"), ("approve_site_only", "site_only")])
def test_site_release_carries_the_new_policy_version(poll, decision, release):
    poll.state.update(status="approved", decision=decision, kind="site")
    out = _status(poll)
    assert out["kind"] == "site" and out["status"] == "released" and out["site_release"] == release
    assert out["policy_version"] == 7 and out["token"] is None
    assert "assistant_browser_hold_consume" not in poll.calls


# ── Opening holds (worker side) ──────────────────────────────────────────────


@pytest.fixture
def opener(keys, monkeypatch):
    owner, jid = str(uuid4()), str(uuid4())
    job = {"id": jid, "agent_type": "user_browser", "principal_kind": "user", "principal_user_id": owner,
           "status": "running", "control_mode": "agent", "input": {}}
    opened = []
    open_rows = []
    def rpc(o, name, params):
        if name == "assistant_browser_hold_open":
            opened.append(params)
            if open_rows:
                return {"ok": False, "code": "help_busy"}
            return {"ok": True, "created": True, "hold": {"id": params["p_hold_id"]}}
        if name == "assistant_browser_hold_release_get":
            return {"ok": True, "status": "pending", "kind": opened[-1]["p_kind"] if opened else "site"}
        return pytest.fail(name)
    monkeypatch.setattr(records.repo, "browser_rpc", rpc)
    monkeypatch.setattr(records.repo, "browser_holds", lambda *a, **kw: open_rows)
    monkeypatch.setattr(runtime, "sanitize", lambda job, payload, **kw: payload)
    monkeypatch.setattr(runtime, "login_open", lambda job: False)
    return SimpleNamespace(job=job, opened=opened, open_rows=open_rows, owner=owner)


def _hold_body(**over):
    body = {"trigger": "network", "method": "POST", "url": URL, "origin": "https://app.procore.com",
            "url_sha256": SHA_URL, "body_sha256": SHA_BODY,
            "body_summary": [{"name": "answer", "value": "yes", "hidden": False}, {"name": "passwd", "hidden": True}],
            "profile_label": "Submit RFI response", "model_description": "Send the answer"}
    body.update(over)
    return body


def test_open_hold_encrypts_the_full_url_and_answers_pending(opener):
    out = holds.open_hold(opener.job, "w", "b", 5, _hold_body())
    assert out["status"] == "pending" and out["kind"] == "final_action"
    params = opener.opened[0]
    assert params["p_kind"] == "final_action" and params["p_hold"]["label"] == "Submit RFI response"
    assert params["p_hold"]["url_sha256"] == SHA_URL and params["p_screenshot_record"] is None
    raw = json.dumps(params)
    assert "draft=0" not in raw and "rfis/12" not in raw
    record = params["p_summary_record"]
    # hold_open stores the record with no help id, as Python sealed it; it
    # is bound to the hold through summary_record_id.
    row = {**record, "user_id": opener.owner, "job_id": opener.job["id"], "help_id": None}
    content = records.unpack_record(opener.owner, opener.job["id"], row)
    assert content["hold"]["url"] == URL and content["hold"]["body_summary"][1] == {"name": "passwd", "value": None,
                                                                                   "hidden": True}


def test_a_hold_whose_url_hash_is_not_the_card_url_is_refused(opener):
    """Review finding: the owner approves the URL on the card, so the signed
    url_sha256 must be the hash of that exact URL."""
    with pytest.raises(holds.HoldRefused) as exc:
        holds.open_hold(opener.job, "w", "b", 5, _hold_body(url_sha256="b" * 64))
    assert exc.value.code == "invalid" and opener.opened == []


def test_hold_summary_reader(opener, monkeypatch):
    holds.open_hold(opener.job, "w", "b", 5, _hold_body())
    record = opener.opened[0]["p_summary_record"]
    row = {**record, "user_id": opener.owner, "job_id": opener.job["id"], "help_id": None}
    monkeypatch.setattr(records.repo, "browser_record", lambda *a: row)
    summary = holds.summary(opener.owner, {"job_id": opener.job["id"], "summary_record_id": record["id"]})
    assert summary["url"] == URL and summary["profile_label"] == "Submit RFI response"


def test_a_hold_record_stored_under_a_help_id_is_refused(opener):
    """The record binding is exact again (review finding): Python seals hold
    records with no help id and hold_open stores them that way, so a row
    that carries a help id no longer passes."""
    holds.open_hold(opener.job, "w", "b", 5, _hold_body())
    record = opener.opened[0]["p_summary_record"]
    row = {**record, "user_id": opener.owner, "job_id": opener.job["id"], "help_id": str(uuid4())}
    with pytest.raises(records.BrowserContentUnavailable):
        records.unpack_record(opener.owner, opener.job["id"], row)


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_it_admin_inspection_never_returns_a_hold_summary_or_screenshot(opener, monkeypatch):
    """10.7 (review finding): held request bodies and final-action
    screenshots are owner-only, even when a row reaches Python."""
    owner, jid = opener.owner, opener.job["id"]
    artifact = records.pack_record(owner, jid, "screenshot",
                                   {"data": base64.b64encode(_png()).decode(), "mime_type": "image/png"},
                                   record_id=str(uuid4()), seq=3)
    artifact_row = {**artifact, "user_id": owner, "job_id": jid, "help_id": None, "created_at": "2026-11-02T00:00:00Z"}
    monkeypatch.setattr(records.repo, "browser_record", lambda *a: artifact_row)
    holds.open_hold(opener.job, "w", "b", 5, _hold_body(
        url="https://app.procore.com/rfis/12?token=abc", url_sha256=hashlib.sha256(
            b"https://app.procore.com/rfis/12?token=abc").hexdigest(),
        body_summary=[{"name": "ssn", "value": "123-45-6789", "hidden": False}],
        screenshot_artifact_id=artifact["id"]))
    params = opener.opened[-1]
    rows = []
    for key in ("p_summary_record", "p_screenshot_record"):
        rows.append({**params[key], "user_id": owner, "job_id": jid, "help_id": None,
                     "created_at": "2026-11-02T00:00:00Z"})
    trace = records.pack_record(owner, jid, "step", {"summary": "Opened the RFI list"}, record_id=str(uuid4()), seq=1)
    rows.append({**trace, "user_id": owner, "job_id": jid, "help_id": None, "created_at": "2026-11-02T00:00:00Z"})
    rows.append({**rows[0], "help_id": str(uuid4())})
    monkeypatch.setattr(records.repo, "browser_inspect",
                        lambda *a, **k: {"ok": True, "audit_id": str(uuid4()), "records": list(rows)})
    out = records.inspect(str(uuid4()), owner, jid, "Checking a reported failure")
    text = json.dumps(out)
    assert [i["kind"] for i in out["items"]] == ["step"]
    assert "123-45-6789" not in text and "token=abc" not in text
    with pytest.raises(records.BrowserContentUnavailable):
        records.inspect_screenshot(str(uuid4()), owner, jid, rows[1]["id"], "Checking a reported failure")
    # The owner still reads the hold screenshot (the copy keeps its bytes).
    assert records._image_bytes(records.unpack_record(owner, jid, rows[1]))[1] == "image/png"


def test_no_screenshot_while_a_login_is_open_or_in_operator_mode(opener, monkeypatch):
    monkeypatch.setattr(records.repo, "browser_record", lambda *a: pytest.fail("screenshot read"))
    monkeypatch.setattr(runtime, "login_open", lambda job: True)
    holds.open_hold(opener.job, "w", "b", 5, _hold_body(screenshot_artifact_id=str(uuid4())))
    monkeypatch.setattr(runtime, "login_open", lambda job: False)
    opener.job["control_mode"] = "operator"
    holds.open_hold(opener.job, "w", "b", 5, _hold_body(screenshot_artifact_id=str(uuid4())))
    assert all(p["p_screenshot_record"] is None for p in opener.opened)


def test_a_retried_hold_joins_the_open_one(opener):
    existing = str(uuid4())
    opener.open_rows.append({"id": existing, "method": "POST", "origin": "https://app.procore.com",
                             "url_sha256": SHA_URL, "body_sha256": SHA_BODY})
    out = holds.open_hold(opener.job, "w", "b", 5, _hold_body())
    assert out["hold_id"] == existing
    opener.open_rows[0]["body_sha256"] = "f" * 64
    with pytest.raises(holds.HoldRefused, match="help_busy"):
        holds.open_hold(opener.job, "w", "b", 5, _hold_body())


def test_a_retired_job_cannot_open_a_hold(opener):
    opener.job["agent_type"] = "user_browser_read"
    with pytest.raises(holds.HoldRefused):
        holds.open_hold(opener.job, "w", "b", 5, _hold_body())


def _site_body(url="https://docs.example.org/spec?x=1"):
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return {"trigger": "link", "url": url, "target_origin": f"{parts.scheme}://{parts.netloc}",
            "from_origin": "https://app.procore.com", "carries_data": False}


def test_site_hold_for_an_approved_site_extends_scope_without_a_card(opener, monkeypatch):
    site = {"site_key": "domain:example.org", "label": "example.org", "registrable_domains": ["example.org"]}
    monkeypatch.setattr(grants, "active_grant_for_url", lambda owner, url: (site, {"id": "g"}))
    seen = []
    monkeypatch.setattr(grants, "extend_job_scope", lambda o, j, k, **kw: seen.append((j, k, kw)) or 9)
    out = holds.open_site_hold(opener.job, "w", "b", 5, _site_body())
    assert out["status"] == "released" and out["site_release"] == "full" and out["policy_version"] == 9
    assert seen == [(opener.job["id"], "domain:example.org", {})] and opener.opened == []


def test_site_hold_for_a_multi_tenant_or_unapproved_site_parks_for_a_card(opener, monkeypatch):
    site = {"site_key": "origin:docs.google.com", "label": "docs.google.com", "kind": "ad_hoc",
            "exact_origins": ["https://docs.google.com"], "registrable_domains": []}
    monkeypatch.setattr(grants, "active_grant_for_url", lambda owner, url: (site, {"id": "g"}))
    def refuse(*a, **kw):
        raise grants.GrantRefused("multi_tenant")
    monkeypatch.setattr(grants, "extend_job_scope", refuse)
    out = holds.open_site_hold(opener.job, "w", "b", 5, _site_body("https://docs.google.com/forms/d/x/viewform"))
    assert out["status"] == "pending" and out["kind"] == "site"
    params = opener.opened[0]
    assert params["p_kind"] == "site" and params["p_hold"]["method"] == "GET"
    assert params["p_hold"]["body_sha256"] == hashlib.sha256(b"").hexdigest()
    assert params["p_hold"]["target_site_key"] and params["p_screenshot_record"] is None
    monkeypatch.setattr(grants, "active_grant_for_url", lambda owner, url: (None, None))
    holds.open_site_hold(opener.job, "w", "b", 5, _site_body("https://new-site.example.net/a"))
    assert opener.opened[-1]["p_hold"]["target_site_key"] == "domain:example.net"


def test_site_hold_for_a_site_never_approved_creates_its_site_access_request(opener, monkeypatch):
    """5.4 step 2 (review finding): the hold card alone cannot approve a new
    site; a site_access request bound to the job lets the grant route do it."""
    opener.job["assistant_conversation_id"] = str(uuid4())
    created, cancelled = [], []

    def create(owner, record):
        created.append((owner, dict(record)))
        return {"ok": True, "request": {"id": str(uuid4()), **record}}

    open_requests = []
    monkeypatch.setattr(records.repo, "browser_request_create", create)
    monkeypatch.setattr(records.repo, "browser_requests_open", lambda owner, **kw: list(open_requests))
    monkeypatch.setattr(records.repo, "browser_request_decide",
                        lambda owner, rid, decision: cancelled.append((rid, decision)) or {"ok": True})
    monkeypatch.setattr(grants, "active_grant_for_url", lambda owner, url: (None, None))
    holds.open_site_hold(opener.job, "w", "b", 5, _site_body("https://new-site.example.net/a?b=1"))
    [(owner, record)] = created
    assert owner == opener.owner
    assert record == {"kind": "site_access", "job_id": opener.job["id"], "site_key": "domain:example.net",
                      "url": "https://new-site.example.net/a?b=1", "reason_code": "site_hold", "provenance": "page",
                      "conversation_id": opener.job["assistant_conversation_id"]}
    # The sealed summary carries the request id for the card.
    summary = opener.opened[-1]["p_summary_record"]
    row = {**summary, "user_id": opener.owner, "job_id": opener.job["id"], "help_id": None}
    request_id = records.unpack_record(opener.owner, opener.job["id"], row)["hold"]["request_id"]
    assert request_id
    # A retried hold joins the open request instead of creating another.
    open_requests.append({"id": request_id, "job_id": opener.job["id"], "site_key": "domain:example.net"})
    holds.open_site_hold(opener.job, "w", "b", 5, _site_body("https://new-site.example.net/a?b=1"))
    assert len(created) == 1
    # A hold that cannot open cancels a request it just created.
    open_requests.clear()
    opener.open_rows.append({"id": str(uuid4()), "method": "X"})
    with pytest.raises(holds.HoldRefused):
        holds.open_site_hold(opener.job, "w", "b", 5, _site_body("https://other.example.com/"))
    assert cancelled and cancelled[-1][1] == "cancel"


def test_site_hold_for_an_approved_multi_tenant_site_creates_no_request(opener, monkeypatch):
    site = {"site_key": "origin:docs.google.com", "label": "docs.google.com", "kind": "ad_hoc",
            "exact_origins": ["https://docs.google.com"], "registrable_domains": []}
    monkeypatch.setattr(grants, "active_grant_for_url", lambda owner, url: (site, {"id": "g"}))
    monkeypatch.setattr(grants, "extend_job_scope",
                        lambda *a, **kw: (_ for _ in ()).throw(grants.GrantRefused("multi_tenant")))
    monkeypatch.setattr(records.repo, "browser_request_create", lambda *a: pytest.fail("no request"))
    holds.open_site_hold(opener.job, "w", "b", 5, _site_body("https://docs.google.com/forms/d/x/viewform"))


def test_site_hold_to_a_private_name_is_declined_without_a_card(opener, monkeypatch):
    monkeypatch.setattr(grants, "active_grant_for_url", lambda *a: pytest.fail("no grant read"))
    out = holds.open_site_hold(opener.job, "w", "b", 5, _site_body("https://router.local/admin"))
    assert out["status"] == "declined" and opener.opened == []


# ── Worker routes ────────────────────────────────────────────────────────────


@pytest.fixture
def routes(monkeypatch):
    from app.core import agent_auth
    from app.routers import agent_worker_user  # noqa: F401 - mounts the routes
    from tests.browser_agents_harness import API, Harness

    h = Harness(monkeypatch)
    worker, token, boot, job = h.active_with_job()
    worker["principal_kind"] = "user"
    job.update(principal_kind="user", principal_user_id=str(uuid4()), agent_type="user_browser")

    def fence(lease, *a, **kw):
        if lease.job_id != job["id"] or lease.epoch != job["lease_epoch"] or lease.boot_id != boot:
            raise agent_auth.agent_error(protocol.ErrorCode.LEASE_LOST)
        return job
    monkeypatch.setattr(runtime, "fenced", fence)
    yield SimpleNamespace(h=h, job=job, headers=h.lease_headers(token, job["id"], job["lease_epoch"], boot),
                          path=f"{API}/jobs/{job['id']}", worker=worker)
    h.client.close()


def test_hold_routes_answer_the_wire_model(routes, monkeypatch):
    hid = str(uuid4())
    monkeypatch.setattr(holds, "open_hold", lambda job, w, b, e, body: {
        "hold_id": hid, "kind": "final_action", "status": "pending", "token": None, "site_release": None,
        "policy_version": None, "label": None, "retry_after": 1})
    out = routes.h.client.post(routes.path + "/user/holds", headers=routes.headers, json=_hold_body())
    assert out.status_code == 200 and out.json()["status"] == "pending"
    assert out.headers["cache-control"] == "no-store"
    seen = {}
    def status(job_id, w, b, e, hold_id, wait_seconds=25, user_id=None):
        seen.update(wait=wait_seconds, hold=hold_id, owner=user_id)
        return {"hold_id": hold_id, "kind": "final_action", "status": "declined"}
    monkeypatch.setattr(holds, "release_status", status)
    out = routes.h.client.get(routes.path + f"/user/holds/{hid}/release", headers=routes.headers)
    assert out.status_code == 200 and out.json()["status"] == "declined"
    # Each read is a single-shot step; the route waits between reads itself.
    assert seen == {"wait": 0, "hold": hid, "owner": routes.job["principal_user_id"]}


def test_the_release_long_poll_never_sleeps_in_a_threadpool_thread(routes, monkeypatch):
    """Review finding: the 25 s wait held a threadpool thread per call."""
    import inspect as pyinspect

    from app.routers import agent_worker_user as module

    assert pyinspect.iscoroutinefunction(module.user_hold_release)
    monkeypatch.setattr(holds.time, "sleep", lambda s: pytest.fail("time.sleep in the release poll"))
    monkeypatch.setattr(protocol, "USER_HOLD_RELEASE_POLL_SECONDS", 0.2)
    monkeypatch.setattr(module, "RELEASE_POLL_STEP_SECONDS", 0.05)
    reads = []

    def status(job_id, w, b, e, hold_id, wait_seconds=25, user_id=None):
        reads.append(wait_seconds)
        return {"hold_id": hold_id, "kind": "final_action", "status": "pending", "retry_after": 1}

    monkeypatch.setattr(holds, "release_status", status)
    hid = str(uuid4())
    out = routes.h.client.get(routes.path + f"/user/holds/{hid}/release", headers=routes.headers)
    assert out.status_code == 200 and out.json()["status"] == "pending"
    assert len(reads) >= 2 and set(reads) == {0}
    assert module._RELEASE_POLLS == set() and module._RELEASE_POLLS_PER_WORKER == {}


def test_a_second_poll_for_the_same_hold_or_past_the_worker_cap_answers_pending_at_once(routes, monkeypatch):
    from app.routers import agent_worker_user as module

    hid = str(uuid4())
    monkeypatch.setattr(holds, "release_status", lambda *a, **k: pytest.fail("no release read"))
    monkeypatch.setattr(module.repo, "browser_hold",
                        lambda owner, h: {"id": h, "job_id": routes.job["id"], "kind": "site"})
    worker_id = str(routes.worker["id"])
    monkeypatch.setattr(module, "_RELEASE_POLLS", {(worker_id, hid)})
    monkeypatch.setattr(module, "_RELEASE_POLLS_PER_WORKER", {worker_id: 1})
    out = routes.h.client.get(routes.path + f"/user/holds/{hid}/release", headers=routes.headers)
    assert out.status_code == 200
    assert out.json()["status"] == "pending" and out.json()["kind"] == "site" and out.json()["token"] is None
    # The in-flight poll still owns its slot.
    assert module._RELEASE_POLLS == {(worker_id, hid)}
    other = str(uuid4())
    monkeypatch.setattr(module, "_RELEASE_POLLS_PER_WORKER", {worker_id: module.RELEASE_POLLS_PER_WORKER_MAX})
    out = routes.h.client.get(routes.path + f"/user/holds/{other}/release", headers=routes.headers)
    assert out.status_code == 200 and out.json()["status"] == "pending"
    # A hold of another job is not answered.
    monkeypatch.setattr(module.repo, "browser_hold", lambda owner, h: {"id": h, "job_id": str(uuid4()), "kind": "site"})
    out = routes.h.client.get(routes.path + f"/user/holds/{other}/release", headers=routes.headers)
    assert out.status_code in (400, 422)


def test_hold_route_refuses_bad_bodies_and_retired_jobs(routes, monkeypatch):
    monkeypatch.setattr(holds, "open_hold", lambda *a: pytest.fail("opened"))
    bad = routes.h.client.post(routes.path + "/user/holds", headers=routes.headers,
                               json=_hold_body(origin="https://evil.example.com"))
    assert bad.status_code in (400, 422)
    routes.job["agent_type"] = "user_browser_read"
    monkeypatch.setattr(holds, "open_hold", lambda job, *a: holds._owner(job))
    out = routes.h.client.post(routes.path + "/user/holds", headers=routes.headers, json=_hold_body())
    assert out.status_code in (400, 422) and "token" not in out.text


def test_heartbeat_reports_a_decided_hold(monkeypatch):
    from app.routers import agent_worker_user as module

    job = {"id": str(uuid4()), "principal_user_id": "o"}
    decided = _hold(status="approved")
    monkeypatch.setattr(module.repo, "browser_holds", lambda *a, **kw: [decided])
    assert module._hold_updates(job, None) == [{"job_id": job["id"], "help_id": decided["help_id"],
                                                "status": "resolved"}]
    assert module._hold_updates(job, decided["help_id"]) == []
    decided["status"] = "open"
    assert module._hold_updates(job, None) == []
    decided.update(status="declined", created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert module._hold_updates(job, None) == []
