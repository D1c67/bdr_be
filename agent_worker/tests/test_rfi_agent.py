"""rfi_from_email end to end (O1, O2): scripted model turns drive a real
headless Chromium against the local Procore fixture through the W1 slot.

Covers: stored jar, fresh login, MFA via a helper secret, locate mode, the
capture data path (only the REST detail artifact reaches BDR), scope and
"captured needs a capture" rules, help with files and notes, parking,
stop replies, resume from each checkpoint phase, login spacing, REST
challenge, and that the password never lands in steps, spool, turns or the
model's observations.
"""

import json
from urllib.parse import quote_plus
from datetime import UTC, datetime, timedelta

import pytest

from bdr_agent.agents.rfi_from_email.runner import RfiFromEmailRunner
from bdr_agent.agents.canary.runner import CanaryRunner

from .agent_bdr import AgentBdr
from .agent_harness import SEED, make_provider, needs_browser, run_job
from .procore_site import PM_FIXTURES, ProcoreSite

pytestmark = needs_browser

HOST = "app.procore.com"
PID = "3167322"
CID = "8089"
RFI = "27914290"


@pytest.fixture
def site():
    s = ProcoreSite().start()
    yield s
    s.stop()


@pytest.fixture
async def provider(site):
    p = await make_provider(site)
    yield p
    await p.stop()


def runners():
    return {"rfi_from_email": RfiFromEmailRunner(help_poll_seconds=0.1), "canary": CanaryRunner()}


def capture_args(rfi: str = RFI) -> dict:
    return {"host": HOST, "project_id": PID, "rfi_id": rfi, "company_id": CID}


def happy_script() -> list:
    return [("ensure_login", {"host": HOST}), ("capture_rfi_detail", capture_args()),
            ("finish", {"outcome": "captured", "summary": "Captured Const-198"})]


def assert_read_only(site: ProcoreSite) -> None:
    """Nothing but GETs (and the login POSTs on login.procore.com) left the browser."""
    for r in site.state.requests:
        if r.method not in ("GET", "HEAD", "OPTIONS"):
            assert r.host == "login.procore.com" and r.path.startswith("/sessions/submit_login_"), r


def dump_everything(bdr: AgentBdr, run, paths) -> str:
    """Everything the worker wrote anywhere BDR or disk can see."""
    parts = [json.dumps(run.steps(bdr), default=str), json.dumps(bdr.turn_requests, default=str),
             json.dumps(bdr.login_results, default=str), json.dumps([c.body for c in bdr.calls], default=str)]
    for f in paths.spool_dir.glob("*"):
        parts.append(f.read_text(errors="replace"))
    return "\n".join(parts)


async def test_stored_jar_capture_and_submit(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)

    assert run.outcome.terminal == "handed_off"
    assert run.job.status == "applying"
    assert bdr.last_result("call_1") == {"ok": True, "outcome": "ok"}
    summary = bdr.last_result("call_2")
    assert summary == {"number": "Const-198", "subject": "EF3 Circuit Clarification", "status": "closed",
                       "revision": "0", "attachment_count": 2, "draft": False}
    # The capture BDR applies is the REST body, byte for byte as JSON data.
    [cap] = bdr.captures
    assert cap["external_rfi_id"] == RFI and cap["company_id"] == CID
    env = json.loads(bdr.artifacts[cap["detail_artifact_id"]]["data"])
    assert env["request_path"] == f"/rest/v1.0/projects/{PID}/rfis/{RFI}"
    assert env["body"] == json.loads((PM_FIXTURES / f"detail_{RFI}.json").read_text())
    assert run.job.checkpoint["phase"] == "detail_captured"
    assert not bdr.login_begins
    assert_read_only(site)
    # The REST call carried the company header from inside the job context.
    detail_req = [r for r in site.seen(host=HOST) if r.path.endswith(f"/rfis/{RFI}")][0]
    assert detail_req.headers.get("procore-company-id") == CID


async def test_fresh_login_password_never_leaks(site, provider, paths):
    bdr = AgentBdr()
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)

    assert run.outcome.terminal == "handed_off"
    assert len(bdr.login_begins) == 1
    assert [r["outcome"] for r in bdr.login_results] == ["ok"]
    assert any(c["name"] == "_session_id" for c in bdr.login_results[0]["cookies"])
    # Only the login POST carried the password, and only to login.procore.com.
    encoded = quote_plus(bdr.login_password).encode()
    carrying = [r for r in site.state.requests if encoded in r.body or bdr.login_password.encode() in r.body]
    assert [(r.host, r.path) for r in carrying] == [("login.procore.com", "/sessions/submit_login_password")]
    everything = dump_everything(bdr, run, paths)
    assert bdr.login_password not in everything
    assert bdr.login_email not in json.dumps(bdr.turn_requests)
    cookie_value = bdr.login_results[0]["cookies"][0]["value"]
    assert cookie_value not in json.dumps(run.steps(bdr))
    assert_read_only(site)
    # Job secrets are dropped when the job ends.
    assert len(run.secrets) == 0


async def test_dead_jar_is_rejected_then_fresh_login(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = [{"name": "_session_id", "value": "stale-token-0000", "domain": HOST, "path": "/",
                       "expires": -1, "secure": True, "httpOnly": True, "sameSite": "Lax"}]
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert run.outcome.terminal == "handed_off"
    assert bdr.login_begins[0]["rejected_jar_logged_in_at"].startswith("2026-10-19T01:00:00")


async def test_mfa_code_from_helper_secret(site, provider, paths):
    site.state.login_mode = "mfa"
    bdr = AgentBdr()
    bdr.help_answers["mfa"] = [{"secret": site.state.otp}]
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)

    assert run.outcome.terminal == "handed_off", bdr.tool_results()
    assert [r["outcome"] for r in bdr.login_results] == ["mfa", "ok"]
    [help_req] = bdr.helps.values()
    assert help_req["scope"] == "login:procore" and help_req["reason"] == "mfa"
    assert site.state.otp not in dump_everything(bdr, run, paths)
    assert bdr.login_password not in dump_everything(bdr, run, paths)
    assert bdr.captures


async def test_mfa_wrong_code_then_right_code(site, provider, paths):
    site.state.login_mode = "mfa"
    bdr = AgentBdr()
    bdr.help_answers["mfa"] = [{"secret": "000000"}]
    bdr.help_join_answers["mfa"] = [{"secret": site.state.otp}]
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert [r["outcome"] for r in bdr.login_results] == ["mfa", "mfa", "ok"]
    assert len({(r["attempt_id"]) for r in bdr.login_results}) == 1
    keys = [c.headers["idempotency-key"] for c in bdr.calls_to("login_result")]
    assert len(set(keys)) == 3
    assert run.outcome.terminal == "handed_off" and bdr.captures
    assert "000000" not in dump_everything(bdr, run, paths)


async def test_session_help_open_joins_and_parks(site, provider, paths):
    bdr = AgentBdr()
    bdr.hold_seconds = 0.3
    bdr.session_status_override = [{"status": "help_open", "help_id": "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8"}]
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert not bdr.login_begins
    [help_req] = bdr.helps.values()
    assert help_req["scope"] == "login:procore"
    assert run.outcome.terminal == "handed_off" and bdr.parks == [run.job.job_id]


async def test_joining_login_help_ignores_earlier_helper_messages(site, provider, paths):
    """A job that joins an open login:procore request must not read a helper's
    earlier note to the holder as its own answer: it parks instead of failing."""
    import uuid as _uuid

    bdr = AgentBdr()
    bdr.hold_seconds = 0.3
    hid = _uuid.uuid4()
    holder = _uuid.uuid4()
    bdr.helps[hid] = {"id": hid, "scope": "login:procore", "reason": "mfa", "status": "open", "holder": holder,
                      "messages": [], "waiters": {holder}, "opened_message": "code please"}
    bdr.deliver(hid, {"body": "Working on it, getting the code from Tom"})
    bdr.helps[hid]["status"] = "open"
    bdr.session_status_override = [{"status": "help_open", "help_id": str(hid)}]
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert not bdr.login_begins
    assert run.outcome.terminal == "handed_off" and bdr.parks == [run.job.job_id]
    assert not bdr.consumed   # the old note was not acted on


async def test_help_retry_after_password_fix_logs_in(site, provider, paths):
    bdr = AgentBdr()
    bdr.login_password = "old-password-xyz"
    bdr.help_answers["login_failed"] = [{"action": "retry", "body": "Password updated"}]

    def fix_password(b, job, body):
        return ("ensure_login", {"host": HOST})

    bdr.script = [fix_password, ("capture_rfi_detail", capture_args()),
                  ("finish", {"outcome": "captured", "summary": "ok"})]
    real_begin = bdr._ep_login_begin

    def begin(w, params, request, body):
        if bdr.login_begins:   # second attempt: the helper fixed the password
            bdr.login_password = site.state.password
        return real_begin(w, params, request, body)

    bdr._ep_login_begin = begin
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert [r["outcome"] for r in bdr.login_results] == ["password_rejected", "ok"]
    assert bdr.last_result("call_1") == {"ok": True, "outcome": "ok"}
    assert run.outcome.terminal == "handed_off"


@pytest.mark.parametrize("mode,outcome,reason", [
    ("captcha", "captcha", "captcha"),
    ("challenge", "challenge", "captcha"),
    ("sso", "sso", "sso"),
])
async def test_login_blockers_open_login_help_and_park(site, provider, paths, mode, outcome, reason):
    site.state.login_mode = mode
    bdr = AgentBdr()
    bdr.hold_seconds = 0.5
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert [r["outcome"] for r in bdr.login_results] == [outcome]
    [help_req] = bdr.helps.values()
    assert help_req["scope"] == "login:procore" and help_req["reason"] == reason
    assert run.outcome.terminal == "handed_off" and bdr.parks == [run.job.job_id]
    assert_read_only(site)


async def test_wrong_password_reports_password_rejected(site, provider, paths):
    bdr = AgentBdr()
    bdr.login_password = "not-the-password-1"
    bdr.hold_seconds = 0.3
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert [r["outcome"] for r in bdr.login_results] == ["password_rejected"]
    [help_req] = bdr.helps.values()
    assert help_req["reason"] == "login_failed"
    assert run.outcome.terminal == "handed_off"
    assert "not-the-password-1" not in dump_everything(bdr, run, paths)


async def test_locate_mode_lists_then_captures(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    seed = {**SEED, "rfi_id": None, "company_id": None}
    bdr.script = [
        ("ensure_login", {"host": HOST}),
        ("procore_resolve_company", {"host": HOST, "project_id": PID}),
        ("capture_rfi_detail", capture_args()),            # refused: not listed yet
        ("procore_list_rfis", {"host": HOST, "project_id": PID, "company_id": CID}),
        ("capture_rfi_detail", capture_args("25803224")),  # another listed RFI: allowed
        ("capture_rfi_detail", capture_args()),
        ("finish", {"outcome": "captured", "summary": "Found and captured"}),
    ]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=seed)
    assert bdr.last_result("call_2") == {"company_id": CID}
    assert bdr.last_result("call_3")["error"] == "not_in_list"
    listing = bdr.last_result("call_4")
    assert listing["count"] == len(json.loads((PM_FIXTURES / "list.json").read_text()))
    assert [m["id"] for m in listing["hint_matches"]] == [RFI]
    assert set(listing["rows"][0]) == {"id", "number", "revision", "subject", "status", "current_revision",
                                       "updated_at"}
    assert bdr.last_result("call_5")["error"] == "not_found"   # listed, but the fixture has no detail
    assert bdr.artifacts_of("rfi_list_json")
    assert run.outcome.terminal == "handed_off" and bdr.captures[0]["external_rfi_id"] == RFI


async def test_scope_and_capture_rules(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    bdr.script = [
        ("finish", {"outcome": "captured", "summary": "too early"}),
        ("ensure_login", {"host": "us02.procore.com"}),
        ("capture_rfi_detail", {**capture_args(), "project_id": "999"}),
        ("capture_rfi_detail", capture_args("27699412")),
        ("navigate", {"url": "https://evil.example/steal"}),
        ("navigate", {"url": f"https://{HOST}/{PID}/project/rfi/list?token=abc"}),
        ("finish", {"outcome": "nothing_to_do", "summary": "Draft RFI"}),
    ]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert bdr.last_result("call_1")["error"] == "no_capture"
    assert bdr.last_result("call_2")["error"] == "out_of_scope"
    assert bdr.last_result("call_3")["error"] == "out_of_scope"
    assert bdr.last_result("call_4")["error"] == "out_of_scope"
    assert bdr.last_result("call_5") == {"error": "navigation_refused", "detail": "host_not_allowed"}
    assert bdr.last_result("call_6") == {"error": "navigation_refused", "detail": "token_query"}
    assert not site.seen(host="evil.example")
    assert run.outcome.terminal == "finish:succeeded"
    assert run.job.finish["outcome"] == "nothing_to_do"
    assert not bdr.captures


async def test_rest_challenge_and_unauthorized_are_observations(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)

    def expire_then_capture(b, job, body):
        site.expire_sessions()
        return ("capture_rfi_detail", capture_args())

    def challenge_mode(b, job, body):
        site.state.detail_mode = "challenge"
        return ("capture_rfi_detail", capture_args())

    def ok_mode(b, job, body):
        site.state.detail_mode = "ok"
        return ("capture_rfi_detail", capture_args())

    bdr.script = [("ensure_login", {"host": HOST}), expire_then_capture, ("ensure_login", {"host": HOST}),
                  challenge_mode, ("navigate", {"url": f"https://{HOST}/{PID}/project/rfi/show/{RFI}"}),
                  ("wait_for", {"seconds": 1}), ok_mode,
                  ("finish", {"outcome": "captured", "summary": "ok"})]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert bdr.last_result("call_2")["error"] == "unauthorized"
    assert bdr.last_result("call_3") == {"ok": True, "outcome": "ok"}
    # The dead jar was rejected by its logged_in_at before the fresh login.
    assert bdr.login_begins and bdr.login_begins[0]["rejected_jar_logged_in_at"]
    assert bdr.last_result("call_4")["error"] == "challenge"
    assert run.outcome.terminal == "handed_off"
    # The RFI page POSTs on load and opens a websocket: both blocked, logged once.
    assert_read_only(site)
    blocked = [s for s in run.steps(bdr) if s["kind"] == "http.blocked"]
    assert any(s["result"]["method"] == "POST" for s in blocked)
    assert any(s["result"]["method"] == "WEBSOCKET" for s in blocked)
    assert len({(s["result"]["host"], s["result"]["method"]) for s in blocked}) == len(blocked)


async def test_help_answer_with_file_and_note_becomes_contributions(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    file_id = "c4c4c4c4-0000-4000-8000-000000000004"
    zip_id = "c5c5c5c5-0000-4000-8000-000000000005"
    bdr.help_answers["ambiguous"] = [{"body": "Use revision 1. The answer PDF is attached.", "files": [
        {"artifact_id": file_id, "filename": "answer.pdf", "size_bytes": 1000, "mime_type": "application/pdf",
         "attachable": True},
        {"artifact_id": zip_id, "filename": "drawings.zip", "size_bytes": 9000, "mime_type": "application/zip",
         "attachable": False}]}]

    def note_ids(b, job, body):
        res = b.last_result("call_2")
        return ("add_helper_note", {"message_ids": [res["messages"][0]["id"]]})

    bdr.script = [
        ("ensure_login", {"host": HOST}),
        ("request_help", {"reason_code": "ambiguous", "message": "Which revision of Const-198?"}),
        ("attach_helper_file", {"artifact_id": zip_id, "kind": "answer"}),
        ("attach_helper_file", {"artifact_id": file_id, "kind": "answer"}),
        note_ids,
        ("attach_helper_file", {"artifact_id": file_id, "kind": "answer"}),
        ("reply_help", {"text": "Thanks, attaching it."}),
        ("capture_rfi_detail", capture_args()),
        ("finish", {"outcome": "captured", "summary": "ok"}),
    ]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    answer = bdr.last_result("call_2")
    assert answer["messages"][0]["text"].startswith("Use revision 1")
    assert {f["filename"] for f in answer["messages"][0]["files"]} == {"answer.pdf", "drawings.zip"}
    # The helper messages shown to the model are reported and consumed.
    assert bdr.turn_requests[2]["helper_message_ids_seen"] == [answer["messages"][0]["id"]]
    assert answer["messages"][0]["id"] in bdr.consumed
    assert bdr.last_result("call_3")["error"] == "not_attachable"
    assert bdr.last_result("call_4")["added"] is True
    assert bdr.last_result("call_5")["added"] is True
    assert bdr.last_result("call_6") == {"added": True, "already": True}
    assert [c["items"][0]["kind"] for c in bdr.contribution_calls] == ["file_answer", "note"]
    assert all(c["external_rfi_id"] == RFI for c in bdr.contribution_calls)
    assert bdr.replies == ["Thanks, attaching it."]
    assert run.outcome.terminal == "handed_off"
    assert any(k.startswith("file:") for k in run.job.checkpoint["contributions_sent"])


async def test_unanswered_help_parks(site, provider, paths):
    bdr = AgentBdr()
    bdr.hold_seconds = 0.5
    bdr.script = [("request_help", {"reason_code": "unexpected_page", "message": "I see an unknown page"})]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert run.outcome.terminal == "handed_off"
    assert bdr.parks == [run.job.job_id]
    assert run.job.status == "waiting_help" and run.job.lease_owner is None


@pytest.mark.parametrize("stop,terminal", [
    ({"stop": "provider_outage", "retry_at": "2026-10-19T15:10:00Z"}, "requeue:provider_outage"),
    ({"stop": "disabled"}, "finish:canceled"),
    ({"stop": "cap_reached"}, "handed_off"),
    ({"stop": "provider_config"}, "handed_off"),
])
async def test_stop_replies(site, provider, paths, stop, terminal):
    bdr = AgentBdr()
    bdr.script = [stop]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert run.outcome.terminal == terminal
    if terminal == "requeue:provider_outage":
        assert run.job.requeues[0]["retry_at"].startswith("2026-10-19T15:10:00")
    if terminal == "handed_off":
        [help_req] = bdr.helps.values()
        assert help_req["reason"] == ("provider_config" if stop["stop"] == "provider_config" else "cap_reached")
        assert bdr.parks == [run.job.job_id]


async def test_pending_turn_is_retried_with_same_key(site, provider, paths):
    bdr = AgentBdr()
    bdr.script = ["pending", ("finish", {"outcome": "nothing_to_do", "summary": "draft"})]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert run.outcome.terminal == "finish:succeeded"
    turns = bdr.calls_to("turns")
    assert len(turns) == 2 and turns[0].headers["idempotency-key"] == turns[1].headers["idempotency-key"]


async def test_resume_with_fresh_capture_submits_without_turns(site, provider, paths):
    """Spec 4.5: detail_captured less than 30 min old goes straight to /rfi-capture."""
    bdr = AgentBdr()
    detail = json.loads((PM_FIXTURES / f"detail_{RFI}.json").read_text())
    captured_at = datetime.now(UTC) - timedelta(minutes=5)
    envelope = json.dumps({"request_path": f"/rest/v1.0/projects/{PID}/rfis/{RFI}", "status": 200,
                           "captured_at": captured_at.isoformat(), "company_id": CID, "body": detail}).encode()
    aid = "c2c2c2c2-0000-4000-8000-000000000002"
    ckpt = {"phase": "detail_captured", "company_id": CID, "rfi_id": RFI, "detail_artifact_id": aid,
            "captured_at": captured_at.isoformat(), "last_url": None, "contributions_sent": []}

    def seed_artifact(b, job, body):   # never called: no turn may happen
        raise AssertionError("a fresh capture must not run the model")

    bdr.script = [seed_artifact]
    real_claim = bdr._ep_claim

    def claim_and_seed(w, params, request, body):
        resp = real_claim(w, params, request, body)
        for job in bdr.jobs.values():
            bdr.artifacts[aid] = {"job_id": str(job.job_id), "kind": "rfi_detail_json", "filename": "x",
                                  "data": envelope}
        return resp

    bdr._ep_claim = claim_and_seed
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED, checkpoint=ckpt,
                        checkpoint_seq=4)
    assert run.outcome.terminal == "handed_off"
    assert not bdr.turn_requests
    assert [c["detail_artifact_id"] for c in bdr.captures] == [aid]


async def test_resume_with_stale_capture_runs_the_model(site, provider, paths):
    bdr = AgentBdr()
    old = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
    ckpt = {"phase": "detail_captured", "company_id": CID, "rfi_id": RFI,
            "detail_artifact_id": "c2c2c2c2-0000-4000-8000-000000000002", "captured_at": old,
            "last_url": None, "contributions_sent": []}
    bdr.script = [("finish", {"outcome": "captured", "summary": "old capture"}),
                  ("finish", {"outcome": "nothing_to_do", "summary": "draft"})]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED, checkpoint=ckpt,
                        checkpoint_seq=4)
    assert bdr.last_result("call_1")["error"] == "no_capture"
    assert bdr.turn_requests and not bdr.captures
    assert run.outcome.terminal == "finish:succeeded"


async def test_resume_submitted_is_a_no_op(site, provider, paths):
    bdr = AgentBdr()
    ckpt = {"phase": "submitted", "company_id": CID, "rfi_id": RFI, "contributions_sent": []}
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED, checkpoint=ckpt,
                        checkpoint_seq=5)
    assert run.outcome.terminal == "handed_off"
    assert not bdr.turn_requests and not bdr.captures


async def test_resume_after_help_reports_unconsumed_messages(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    help_id = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8"
    unconsumed = [{"help_id": help_id, "status": "answered", "messages": [
        {"id": 42, "author_kind": "helper", "author_label": "Dana", "action": "message",
         "body": "The RFI is Const-198 rev 1, id 27914290.", "secret_ref": None, "files": [],
         "created_at": "2026-10-19T15:04:05Z"}]}]
    # BDR's help request exists on the fake so consume passes the IDOR check.
    from uuid import UUID
    bdr.helps[UUID(help_id)] = {"id": UUID(help_id), "scope": "job:x", "reason": "ambiguous", "status": "answered",
                                "holder": None, "messages": [], "waiters": set()}

    def first_turn(b, job, body):
        b.helps[UUID(help_id)]["waiters"].add(job.job_id)
        return ("ensure_login", {"host": HOST})

    bdr.script = [first_turn, ("capture_rfi_detail", capture_args()),
                  ("finish", {"outcome": "captured", "summary": "ok"})]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED, turn_count=9,
                        checkpoint={"phase": "session_ready", "contributions_sent": []}, checkpoint_seq=3,
                        unconsumed=unconsumed)
    first = bdr.turn_requests[0]
    assert first["prev_turn_no"] == 9 and first["helper_message_ids_seen"] == [42] and first["tool_results"] == []
    assert 42 in bdr.consumed
    assert bdr.turn_requests[1]["prev_turn_no"] == 10
    assert run.outcome.terminal == "handed_off"


async def test_long_login_spacing_requeues(site, provider, paths):
    bdr = AgentBdr()
    retry_at = (datetime.now(UTC) + timedelta(minutes=9)).isoformat().replace("+00:00", "Z")
    bdr.login_begin_override = [{"granted": False, "reason": "spacing", "retry_at": retry_at}]
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert run.outcome.terminal == "requeue:login_spacing"
    assert run.job.requeues[0]["reason"] == "login_spacing"


async def test_not_configured_fails_login_failed(site, provider, paths):
    bdr = AgentBdr()
    bdr.session_status_override = [{"status": "not_configured"}]
    bdr.script = happy_script()
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert run.outcome.terminal == "finish:failed"
    assert run.job.finish["error_kind"] == "login_failed"


async def test_checkpoint_last_url_every_five_steps(site, provider, paths):
    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    show = f"https://{HOST}/{PID}/project/rfi/show/{RFI}"
    bdr.script = [("ensure_login", {"host": HOST}), ("navigate", {"url": show}), ("read_page", {}),
                  ("scroll", {"direction": "down", "amount": 1}), ("find_text", {"query": "Const-198"}),
                  ("finish", {"outcome": "nothing_to_do", "summary": "x"})]
    run = await run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED)
    assert run.job.checkpoint["last_url"] == show
    assert run.job.checkpoint["phase"] == "session_ready"
    page = bdr.last_result("call_3")
    assert "Const-198" in page["text"] and "IGNORE ALL PREVIOUS" in page["text"]
    # Secret-looking fields never show a value; the injection line is just text.
    assert 'textbox "Verification code"' in page["text"]


async def test_two_types_run_in_separate_contexts(site, provider, paths):
    import asyncio

    bdr = AgentBdr()
    bdr.stored_jar = site.mint_session()
    bdr.jar_logged_in_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)

    def by_type(rfi_items, canary_items):
        def pick(b, job, body):
            return (rfi_items if job.agent_type == "rfi_from_email" else canary_items).pop(0)
        return pick

    rfi_items = happy_script()
    canary_items = [("navigate", {"url": "https://bdr.test/agent-worker/v1/canary/page"}),
                    ("finish", {"outcome": "nothing_to_do", "summary": "ok"})]
    bdr.script = [by_type(rfi_items, canary_items) for _ in range(5)]
    a, b = await asyncio.gather(
        run_job(bdr, provider, paths, "rfi_from_email", runners=runners(), input=SEED),
        run_job(bdr, provider, paths, "canary", runners=runners()))
    assert a.outcome.terminal == "handed_off"
    assert b.outcome.terminal == "finish:succeeded"
    # The canary context never carried the Procore session.
    canary_reqs = site.seen(host="bdr.test")
    assert canary_reqs and all("_session_id" not in r.headers.get("cookie", "") for r in canary_reqs)
