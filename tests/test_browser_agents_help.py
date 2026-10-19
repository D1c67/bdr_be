"""Browser agent help flow (docs/browser_agents/SPEC.md 6, O5): worker routes,
the human help routes, the sweeper steps and the notices. FakeDB, no network."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import timedelta

import pytest

from app.services.browser_agents import help as help_flow
from app.services.browser_agents import queue
from tests.browser_agents_fake import iso
from tests.browser_agents_harness import error_code
from tests.browser_agents_human import PROFILES, HumanHarness

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 100


@pytest.fixture
def make(monkeypatch):
    made: list[HumanHarness] = []

    def _make(**kw) -> HumanHarness:
        h = HumanHarness(monkeypatch, **kw)
        made.append(h)
        return h

    yield _make
    for h in made:
        h.close()


@pytest.fixture
def h(make):
    return make()


def rfi_job(h: HumanHarness):
    worker, token, boot, job = h.active_with_job("rfi_from_email")
    return worker, token, boot, job


def help_row(h, help_id):
    return h.db.one("browser_agent_help_requests", id=help_id)


def dump(h) -> str:
    return json.dumps(h.db.tables, default=str) + json.dumps(h.notices, default=str) + json.dumps(h.audits, default=str)


# ── Open, join, scope, notices ──────────────────────────────────────────────────


def test_open_job_scope_help_parks_the_job_in_waiting_help_and_notifies_estimating_admins_once(h):
    _, token, boot, job = rfi_job(h)
    resp = h.open_help(token, boot, job, reason_code="ambiguous", message="Two RFIs named 198")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["joined_existing"] is False and body["holder"] is True and body["suppressed"] is False
    row = help_row(h, body["help_id"])
    assert row["scope_key"] == f"job:{job['id']}"
    assert row["title"] == "Which RFI is it? (RFI 456)"
    assert row["agent_message"] == "Two RFIs named 198"
    assert row["notified_at"] is not None
    j = h.db.one("browser_agent_jobs", id=job["id"])
    assert j["status"] == "waiting_help" and j["waiting_help_id"] == body["help_id"]
    notices = [n for n in h.notices if n["type"] == "browser_agent.help_needed"]
    assert [n["role"].value for n in notices] == ["estimating_admin"]
    assert notices[0]["metadata"]["help_id"] == body["help_id"]
    assert notices[0]["metadata"]["job_id"] == job["id"]
    assert "Two RFIs" not in notices[0]["message"]          # never the agent's words
    assert notices[0]["email"] is True


def test_no_active_estimating_admin_falls_back_to_it_admin(make):
    h = make(with_ea=False)
    _, token, boot, job = rfi_job(h)
    assert h.open_help(token, boot, job).status_code == 200
    roles = [n["role"].value for n in h.notices if n["type"] == "browser_agent.help_needed"]
    assert roles == ["estimating_admin", "it_admin"]


def test_login_scope_is_shared_has_no_project_and_also_tells_it_admin(h):
    _, token, boot, job = rfi_job(h)
    first = h.open_help(token, boot, job, reason_code="mfa").json()
    row = help_row(h, first["help_id"])
    assert row["scope_key"] == "login:procore" and row["project_id"] is None
    assert row["title"] == "Procore code needed"
    roles = [n["role"].value for n in h.notices if n["type"] == "browser_agent.help_needed"]
    assert roles == ["estimating_admin", "it_admin"]
    other = h.second_job(token, boot)
    second = h.open_help(token, boot, other, reason_code="login_failed", message="me too").json()
    assert second["help_id"] == first["help_id"]
    assert second["joined_existing"] is True and second["holder"] is False
    assert len([n for n in h.notices if n["type"] == "browser_agent.help_needed"]) == 2   # joiners never notify
    msgs = h.db.rows("browser_agent_help_messages", help_id=first["help_id"])
    assert [(m["author_kind"], m["body"]) for m in msgs] == [("agent", "me too")]


def test_a_link_scope_given_up_in_the_last_day_is_suppressed(h):
    _, token, boot, job = rfi_job(h)
    opened = h.open_help(token, boot, job, reason_code="access_denied").json()
    assert help_row(h, opened["help_id"])["scope_key"] == f"link:{job['project_id']}"
    assert h.say(opened["help_id"], action="give_up").status_code == 200
    link = h.db.one("pm_external_links", project_id=job["project_id"])
    assert "gave up" in link["last_error"]
    other = h.second_job(token, boot)
    again = h.open_help(token, boot, other, reason_code="not_found")
    assert again.status_code == 200 and again.json()["suppressed"] is True and again.json()["help_id"] is None
    assert h.db.one("browser_agent_jobs", id=other["id"])["status"] == "running"


def test_open_help_checks_reason_screenshot_ownership_and_the_lease(h):
    _, token, boot, job = rfi_job(h)
    foreign = h.db.table("browser_agent_artifacts").insert({
        "job_id": str(uuid.uuid4()), "kind": "screenshot", "storage_path": "x/y.png", "mime_type": "image/png",
        "size_bytes": 1, "sha256": "0" * 64}).execute().data[0]
    resp = h.open_help(token, boot, job, screenshot_artifact_id=foreign["id"])
    assert resp.status_code == 403 and error_code(resp) == "idor"
    stale = h.client.post(f"/agent-worker/v1/jobs/{job['id']}/help", json={"reason_code": "other"},
                          headers=h.lease_headers(token, job["id"], int(job["lease_epoch"]) + 5, boot))
    assert stale.status_code == 409 and error_code(stale) == "lease_lost"


def test_the_canary_cannot_open_login_help(h):
    _, token, boot, job = h.active_with_job("canary")
    resp = h.open_help(token, boot, job, reason_code="mfa")
    assert resp.status_code == 422 and error_code(resp) == "invalid_request"


# ── Messages, consume, reply ───────────────────────────────────────────────────


def test_worker_messages_hide_secret_values_and_reject_foreign_help(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job, reason_code="mfa").json()["help_id"]
    assert h.say(hid, body="Trying a new code now").status_code == 200
    sid = str(uuid.uuid4())
    _in_login(h, job)
    assert h.say(hid, is_secret=True, secret_id=sid, secret_value="482913").status_code == 200
    resp = h.wcall("get", token, boot, job, f"/help/{hid}/messages", params={"after": 0})
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    assert msgs[0]["body"] == "Trying a new code now" and msgs[0]["author_label"] == "Erin Admin (Estimating Admin)"
    assert msgs[1]["body"] is None and msgs[1]["secret_ref"]["consumed"] is False
    assert "482913" not in resp.text
    bad = h.wcall("get", token, boot, job, f"/help/{uuid.uuid4()}/messages")
    assert bad.status_code == 403 and error_code(bad) == "idor"


def test_consume_records_reads_and_puts_the_holder_back_to_running(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    mid = h.say(hid, body="It is RFI 198, the second one").json()["id"]
    assert help_row(h, hid)["status"] == "answered"
    bad = h.wcall("post", token, boot, job, f"/help/{hid}/consume", json={"message_ids": [mid, 999]})
    assert bad.status_code == 403 and error_code(bad) == "idor"
    ok = h.wcall("post", token, boot, job, f"/help/{hid}/consume", json={"message_ids": [mid]})
    assert ok.status_code == 204
    again = h.wcall("post", token, boot, job, f"/help/{hid}/consume", json={"message_ids": [mid]})
    assert again.status_code == 204                                       # idempotent
    assert len(h.db.rows("browser_agent_help_reads", job_id=job["id"])) == 1
    j = h.db.one("browser_agent_jobs", id=job["id"])
    assert j["status"] == "running" and j["waiting_help_id"] is None
    assert [s["kind"] for s in h.db.rows("browser_agent_steps", job_id=job["id"])][-2:] == ["help.consumed",
                                                                                           "help.consumed"]


def test_agent_reply_is_scrubbed_and_a_password_drops_the_text(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    ok = h.wcall("post", token, boot, job, f"/help/{hid}/messages",
                 json={"body": "See https://x.procore.com/p?token=abc123&x=1"})
    assert ok.status_code == 200
    leaked = h.wcall("post", token, boot, job, f"/help/{hid}/messages",
                     json={"body": "the password is Procore-Pass-9!"})
    assert leaked.status_code == 200
    bodies = [m["body"] for m in h.db.rows("browser_agent_help_messages", help_id=hid)]
    assert "abc123" not in bodies[0]
    assert bodies[1] == "[agent text removed: it contained a secret]"
    assert "Procore-Pass-9!" not in dump(h)


# ── The one-time helper secret ─────────────────────────────────────────────────


def _in_login(h, job, outcome="mfa"):
    h.db.table("browser_agent_login_attempts").insert({
        "identity": "procore", "host": "app.procore.com", "kind": "browser", "job_id": job["id"],
        "epoch": job["lease_epoch"], "granted": True, "outcome": outcome}).execute()


def test_a_secret_is_encrypted_released_once_to_the_holder_in_a_login_then_nulled(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job, reason_code="mfa").json()["help_id"]
    sid = str(uuid.uuid4())
    _in_login(h, job)
    sent = h.say(hid, is_secret=True, secret_id=sid, secret_value="482913")
    assert sent.status_code == 200, sent.text
    view = sent.json()
    assert view["is_secret"] is True and view["body"] is None and view["secret_consumed"] is False
    assert "482913" not in dump(h)
    [msg] = h.db.rows("browser_agent_help_messages", help_id=hid)
    assert msg["secret_ciphertext"].startswith("\\x") and msg["secret_hmac"]
    path = f"/help/{hid}/secrets/{msg['id']}/use"
    h.db.rows("browser_agent_login_attempts", job_id=job["id"])[0]["outcome"] = "ok"
    early = h.wcall("post", token, boot, job, path)
    assert early.status_code == 422                                        # not inside a login
    h.db.advance(1)
    _in_login(h, job)
    used = h.wcall("post", token, boot, job, path)
    assert used.status_code == 200 and used.json() == {"value": "482913"}
    assert used.headers["cache-control"] == "no-store"
    msg = h.db.one("browser_agent_help_messages", id=msg["id"])
    assert msg["secret_ciphertext"] is None and msg["secret_nonce"] is None and msg["secret_hmac"]
    assert msg["secret_consumed_by_job"] == job["id"]
    # Redeeming the code ends the wait: a wrong code reopens help from running.
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["waiting_help_id"]) == ("running", None)
    again = h.open_help(token, boot, job, reason_code="mfa")
    assert again.status_code == 200 and again.json()["help_id"] == hid
    twice = h.wcall("post", token, boot, job, path)
    assert twice.status_code == 422
    assert h.get(f"/help/{hid}/messages").json()["items"][0]["secret_consumed"] is True


def test_only_the_holder_may_use_a_secret(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job, reason_code="mfa").json()["help_id"]
    other = h.second_job(token, boot)
    h.open_help(token, boot, other, reason_code="mfa")
    _in_login(h, job)
    h.say(hid, is_secret=True, secret_id=str(uuid.uuid4()), secret_value="111222")
    [msg] = h.db.rows("browser_agent_help_messages", help_id=hid)
    _in_login(h, other)
    resp = h.wcall("post", token, boot, other, f"/help/{hid}/secrets/{msg['id']}/use")
    assert resp.status_code == 403 and error_code(resp) == "idor"


# ── Human messages and buttons ─────────────────────────────────────────────────


def test_messages_are_idempotent_on_client_msg_id(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    cid = str(uuid.uuid4())
    a = h.say(hid, client_msg_id=cid, body="first")
    b = h.say(hid, client_msg_id=cid, body="first")
    assert a.status_code == b.status_code == 200 and a.json()["id"] == b.json()["id"]
    assert len(h.db.rows("browser_agent_help_messages", help_id=hid)) == 1
    assert [a["action"] for a in h.audits] == ["browser_agent.help_reply"]


def test_looks_like_secret_guard(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    pw = h.say(hid, body="try Procore-Pass-9! please")
    assert pw.status_code == 422 and error_code(pw) == "looks_like_secret"
    assert h.say(hid, body="482913").status_code == 200                   # digits are fine outside login
    other = h.second_job(token, boot)
    lid = h.open_help(token, boot, other, reason_code="mfa").json()["help_id"]
    code = h.say(lid, body=" 482 913 ")
    assert code.status_code == 422 and error_code(code) == "looks_like_secret"
    assert "Procore-Pass-9!" not in dump(h)


def test_a_secret_outside_a_login_scope_is_refused(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    resp = h.say(hid, is_secret=True, secret_id=str(uuid.uuid4()), secret_value="123456")
    assert resp.status_code == 422 and error_code(resp) == "secret_not_allowed"
    shape = h.say(hid, body="x", secret_value="123456")
    assert shape.status_code == 422 and error_code(shape) == "invalid_request"


def test_a_message_requeues_parked_waiters_ahead_of_the_queue(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    assert h.wcall("post", token, boot, job, "/park").status_code == 204
    parked = h.db.one("browser_agent_jobs", id=job["id"])
    assert parked["status"] == "waiting_help" and parked["lease_owner"] is None
    h.say(hid, body="Use the second RFI")
    j = h.db.one("browser_agent_jobs", id=job["id"])
    assert (j["status"], j["resume_reason"], j["priority"], j["waiting_help_id"]) == ("queued", "help_answered", 10,
                                                                                        None)
    claimed = h.claim(token, boot, {"rfi_from_email": 1}).json()["jobs"]
    [c] = claimed
    assert c["unconsumed_help"][0]["messages"][0]["body"] == "Use the second RFI"
    assert h.db.one("browser_agent_jobs", id=job["id"])["attempts"] == 1        # uncharged


def test_an_unread_answer_requeues_the_job_when_it_parks(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    h.say(hid, body="It is the second one")
    assert h.db.one("browser_agent_jobs", id=job["id"])["status"] == "waiting_help"   # held: not requeued
    assert h.wcall("post", token, boot, job, "/park").status_code == 204
    j = h.db.one("browser_agent_jobs", id=job["id"])
    assert j["status"] == "queued" and j["resume_reason"] == "help_answered"


def test_retry_now_resolves_and_requeues_every_waiter(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job, reason_code="login_failed").json()["help_id"]
    other = h.second_job(token, boot)
    h.open_help(token, boot, other, reason_code="login_locked")
    h.wcall("post", token, boot, other, "/park")
    resp = h.say(hid, action="retry", body="Password fixed")
    assert resp.status_code == 200
    row = help_row(h, hid)
    assert row["status"] == "resolved" and row["resolution"] == "retry"
    for jid in (job["id"], other["id"]):
        j = h.db.one("browser_agent_jobs", id=jid)
        assert j["status"] == "queued" and j["priority"] == 10 and j["lease_owner"] is None
    assert h.dismissals and h.dismissals[-1]["metadata_eq"] == {"help_id": hid}
    stale = h.open_help(token, boot, job)                                  # the held lease was released
    assert stale.status_code == 409 and error_code(stale) == "lease_lost"


def test_give_up_and_skip_close_the_waiters(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    h.say(hid, action="give_up")
    j = h.db.one("browser_agent_jobs", id=job["id"])
    assert (j["status"], j["error_kind"]) == ("failed", "help_declined")
    other = h.second_job(token, boot)
    hid2 = h.open_help(token, boot, other).json()["help_id"]
    h.say(hid2, action="skip_daily")
    j2 = h.db.one("browser_agent_jobs", id=other["id"])
    assert (j2["status"], j2["error_kind"]) == ("canceled", "skipped_by_helper")
    closed = h.say(hid2, body="late")
    assert closed.status_code == 409 and error_code(closed) == "help_closed"
    kinds = [s["kind"] for s in h.db.rows("browser_agent_steps", job_id=other["id"]) if s["actor"] == "helper"]
    assert kinds == ["help.skip"]
    step = [s for s in h.db.rows("browser_agent_steps", job_id=other["id"]) if s["actor"] == "helper"][0]
    assert step["actor_user_id"] == PROFILES["ea"][0] and step["actor_label"] == "Erin Admin (Estimating Admin)"


def test_a_cap_reached_retry_resets_counters_once_for_a_helper(h):
    _, token, boot, job = rfi_job(h)
    h.db.one("browser_agent_jobs", id=job["id"]).update(tokens_in=9000, tokens_out=100, cost_usd=2.5, turn_count=7)
    hid = h.open_help(token, boot, job, reason_code="cap_reached").json()["help_id"]
    assert h.say(hid, action="retry").status_code == 200
    j = h.db.one("browser_agent_jobs", id=job["id"])
    assert (j["tokens_in"], j["cost_usd"], j["helper_retry_used"]) == (0, 0, True)
    assert j["result"]["cap_reset"]["after_turn_no"] == 7 and j["result"]["cap_reset"]["cost_usd"] == 2.5
    h.claim(token, boot, {"rfi_from_email": 1})
    hid2 = h.open_help(token, boot, j, reason_code="cap_reached").json()["help_id"]
    again = h.say(hid2, action="retry")
    assert again.status_code == 403 and error_code(again) == "retry_admin_only"
    assert h.say(hid2, who="it", action="retry").status_code == 200


def test_roles_on_help_routes(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    assert h.get(f"/help/{hid}", who="exec").status_code == 200
    denied = h.say(hid, who="exec", body="hi")
    assert denied.status_code == 403
    assert h.get(f"/help/{hid}", who="acct").status_code == 403
    assert h.get(f"/help/{hid}", who="dev").status_code == 200           # dev accounts may view


def test_help_list_detail_claim_and_unclaim(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job, message="<b>look</b>").json()["help_id"]
    listing = h.get("/help", params={"status": "active"}).json()
    assert [r["id"] for r in listing["items"]] == [hid] and listing["next_cursor"] is None
    assert listing["items"][0]["waiting_jobs"] == 1 and listing["items"][0]["project_name"] == "Linked"
    claimed = h.post(f"/help/{hid}/claim").json()
    assert claimed["claimed_by_me"] is True and claimed["claimed_by_label"] == "Erin Admin (Estimating Admin)"
    assert claimed["is_login_scope"] is False and claimed["agent_message"] == "<b>look</b>"
    assert claimed["waiters"][0]["rfi_label"] == "RFI 456"
    assert h.get(f"/help/{hid}", who="it").json()["claimed_by_me"] is False
    assert h.post(f"/help/{hid}/unclaim").json()["claimed_by_label"] is None
    assert [a["action"] for a in h.audits] == ["browser_agent.help_claim", "browser_agent.help_claim"]
    msgs = h.get(f"/help/{hid}/messages", params={"after_id": 0}).json()
    assert msgs["status"] == "open" and msgs["items"] == []


# ── Uploads and contributions ────────────────────────────────────────────────


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w/>")
    return buf.getvalue()


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "x")
    return buf.getvalue()


def upload(h, hid, name, data, attachable=True, who="ea"):
    return h.post(f"/help/{hid}/files", who=who, files={"file": (name, data, "application/octet-stream")},
                  data={"attachable": "true" if attachable else "false"})


def test_uploads_are_sniffed_allowlisted_and_zip_is_never_attachable(h, monkeypatch):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    pdf = upload(h, hid, "answer.pdf", PDF)
    assert pdf.status_code == 200, pdf.text
    assert pdf.json()["attachable"] is True and pdf.json()["mime_type"] == "application/pdf"
    assert upload(h, hid, "answer.docx", _docx()).json()["attachable"] is True
    assert upload(h, hid, "bundle.zip", _zip()).json()["attachable"] is False
    lie = upload(h, hid, "fake.pdf", PNG)
    assert lie.status_code == 422 and error_code(lie) == "file_not_allowed"
    exe = upload(h, hid, "tool.exe", b"MZ")
    assert exe.status_code == 422 and error_code(exe) == "file_not_allowed"
    again = upload(h, hid, "answer.pdf", PDF)                            # same bytes: same artifact
    assert again.json()["artifact_id"] == pdf.json()["artifact_id"]
    monkeypatch.setattr(help_flow, "UPLOAD_MAX_BYTES", 50)
    big = upload(h, hid, "big.pdf", PDF)
    assert big.status_code == 413 and error_code(big) == "file_too_large"


def test_a_message_may_cite_only_this_requests_uploads(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    art = upload(h, hid, "a.pdf", PDF).json()["artifact_id"]
    ok = h.say(hid, body="see file", artifact_ids=[art])
    assert ok.status_code == 200 and ok.json()["files"][0]["filename"] == "a.pdf"
    bad = h.say(hid, body="x", artifact_ids=[str(uuid.uuid4())])
    assert bad.status_code == 422


def test_contributions_accept_attachable_files_and_verbatim_helper_notes(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    pdf = upload(h, hid, "answer.pdf", PDF).json()["artifact_id"]
    zipped = upload(h, hid, "bundle.zip", _zip()).json()["artifact_id"]
    note = h.say(hid, body="The GC answered by phone: use 3/4 conduit.").json()["id"]
    agent_mid = h.wcall("post", token, boot, job, f"/help/{hid}/messages", json={"body": "thanks"}).json()["id"]
    body = {"help_id": hid, "external_rfi_id": "456", "items": [
        {"kind": "file_answer", "artifact_id": pdf}, {"kind": "file_question", "artifact_id": zipped},
        {"kind": "note", "message_ids": [note]}, {"kind": "note", "message_ids": [agent_mid]}]}
    resp = h.wcall("post", token, boot, job, "/contributions", json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert [a["index"] for a in out["accepted"]] == [0, 2]
    assert [(r["index"], r["reason"]) for r in out["rejected"]] == [(1, "not_attachable"), (3, "not_helper_message")]
    rows = h.db.rows("browser_agent_rfi_contributions")
    assert {r["kind"] for r in rows} == {"file_answer", "note"}
    assert [r["note_text"] for r in rows if r["kind"] == "note"] == ["The GC answered by phone: use 3/4 conduit."]
    resend = h.wcall("post", token, boot, job, "/contributions", json={**body, "items": body["items"][:1]})
    assert resend.json()["accepted"][0]["contribution_id"] == out["accepted"][0]["contribution_id"]
    foreign = h.wcall("post", token, boot, job, "/contributions", json={**body, "help_id": str(uuid.uuid4())})
    assert foreign.status_code == 403 and error_code(foreign) == "idor"


# ── Sweeper steps ──────────────────────────────────────────────────────────────


def test_reminders_go_out_every_interval_while_unclaimed_at_most_three(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    sent = []
    for _ in range(5):
        h.db.advance(121 * 60)
        sent.append(help_flow.sweep_reminders(h.db.now, queue.load_settings()))
    assert sent == [1, 1, 1, 0, 0]
    assert help_row(h, hid)["reminder_count"] == 3
    reminders = [n for n in h.notices if n["type"] == "browser_agent.help_reminder"]
    assert len(reminders) == 3 and reminders[0]["message"].startswith("Still waiting for help")


def test_a_claim_lapses_after_inactivity_and_rearms_a_reminder(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    h.post(f"/help/{hid}/claim")
    h.db.advance(10 * 60)
    assert help_flow.sweep_claim_lapse(h.db.now, queue.load_settings()) == 0
    h.db.advance(25 * 60)
    assert help_flow.sweep_claim_lapse(h.db.now, queue.load_settings()) == 1
    row = help_row(h, hid)
    assert row["claimed_by"] is None and row["reminder_count"] == 1
    assert [n["type"] for n in h.notices][-1] == "browser_agent.help_reminder"


def test_a_request_resolves_once_its_waiters_finish(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    assert help_flow.sweep_resolve_finished(h.db.now, queue.load_settings()) == 0
    mid = h.say(hid, body="go on").json()["id"]
    h.wcall("post", token, boot, job, f"/help/{hid}/consume", json={"message_ids": [mid]})
    finish = h.wcall("post", token, boot, job, "/finish",
                     json={"status": "succeeded", "outcome": "nothing_to_do", "summary": "done"})
    assert finish.status_code == 204, finish.text
    assert help_flow.sweep_resolve_finished(h.db.now, queue.load_settings()) == 1
    row = help_row(h, hid)
    assert (row["status"], row["resolution"]) == ("resolved", "agent_continued")


def test_expiry_fails_job_waiters_and_keeps_a_locked_login_open(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    other = h.second_job(token, boot)
    lid = h.open_help(token, boot, other, reason_code="login_locked").json()["help_id"]
    h.db.table("rfp_harvest_sessions").insert({"provider": "procore",
                                               "locked_until": iso(h.db.now + timedelta(days=5))}).execute()
    h.db.advance(49 * 3600)
    assert help_flow.sweep_expire(h.db.now, queue.load_settings()) == 2
    assert help_row(h, hid)["status"] == "expired"
    j = h.db.one("browser_agent_jobs", id=job["id"])
    assert (j["status"], j["error_kind"]) == ("failed", "help_timeout")
    assert help_row(h, lid)["status"] == "open" and help_row(h, lid)["expiry_emailed_at"]
    assert help_flow.sweep_expire(h.db.now, queue.load_settings()) == 0          # emailed once
    types = [(n["type"], n["role"].value) for n in h.notices]
    assert ("browser_agent.help_expired", "estimating_admin") in types
    it_mail = [n for n in h.notices if n["role"].value == "it_admin" and n["email"]
               and n["metadata"].get("help_id") == lid]
    assert it_mail


def test_unconsumed_secrets_are_purged_after_thirty_minutes(h):
    _, token, boot, job = rfi_job(h)
    hid = h.open_help(token, boot, job, reason_code="mfa").json()["help_id"]
    _in_login(h, job)
    h.say(hid, is_secret=True, secret_id=str(uuid.uuid4()), secret_value="777888")
    h.db.advance(29 * 60)
    assert help_flow.sweep_secret_purge(h.db.now, {}) == 0
    h.db.advance(2 * 60)
    assert help_flow.sweep_secret_purge(h.db.now, {}) == 1
    [m] = h.db.rows("browser_agent_help_messages", help_id=hid)
    assert m["secret_ciphertext"] is None and m["secret_purged_at"] and m["secret_hmac"]
    h.db.advance(8 * 86400)
    help_flow.sweep_secret_purge(h.db.now, {})
    assert h.db.one("browser_agent_help_messages", id=m["id"])["secret_hmac"] is None


def test_help_steps_are_registered_with_the_sweeper(h):
    names = [n for n, _ in queue._EXTRA_STEPS]
    for step in ("help_reminders", "help_claim_lapse", "help_answered_parked", "help_resolve_finished",
                 "help_expire", "help_secret_purge"):
        assert step in names
