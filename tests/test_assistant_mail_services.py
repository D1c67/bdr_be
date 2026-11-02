"""Mail operation boundaries with fake Graph and atomic persistence adapters."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant import repo
from app.assistant.tools import mail as tools
from app.services import assistant_mail as mail
from app.services import assistant_mail_review as review
from app.services import assistant_mail_send as send
from app.services import assistant_ms
from app.services.assistant_graph import GraphSendUncertain

OWNER = "11111111-1111-4111-8111-111111111111"
APPROVAL = "22222222-2222-4222-8222-222222222222"
RUN = "33333333-3333-4333-8333-333333333333"
FILE = "44444444-4444-4444-8444-444444444444"
CONV = "55555555-5555-4555-8555-555555555555"
EMAIL = "owner@example.com"
TARGET = "staff@example.com"


class Query:
    def __init__(self, h, table):
        self.h, self.table, self.op, self.values = h, table, "select", None
    def select(self, *args):
        return self
    def eq(self, *args):
        return self
    def in_(self, *args):
        return self
    def limit(self, *args):
        return self
    def insert(self, values):
        self.op, self.values = "insert", values
        return self
    def update(self, values):
        self.op, self.values = "update", values
        return self
    def execute(self):
        if self.table == "profiles":
            return SimpleNamespace(data=[self.h.profile, self.h.recipient])
        if self.table == "email_log":
            self.h.logs.append((self.op, copy.deepcopy(self.values)))
            return SimpleNamespace(data=[{"id": "log"}])
        return SimpleNamespace(data=[])


@pytest.fixture
def h(monkeypatch):
    state = SimpleNamespace(
        profile={"id": OWNER, "email": EMAIL, "full_name": "Owner User", "is_active": True,
                 "role": "executive", "rfp_mailboxes": []},
        recipient={"id": "recipient", "email": TARGET, "full_name": "Staff", "is_active": True, "role": "executive"},
        calls=[], logs=[], raw=[], pages=[], files=[], mode="normal", token_error=None,
        send_error=None, claim={"ok": True, "claimed": True, "status": "claimed"},
        access_claim={"ok": True, "claimed": True, "access_id": "access"}, approval=None, sensitive=False,
    )
    def for_mailbox(user_id, mailbox):
        state.calls.append(("authorize", mailbox))
        if mailbox not in {EMAIL, "bids@example.com"}:
            raise mail.MailError()
        return mailbox, "token"
    def token(user_id):
        state.calls.append(("token", None))
        if state.token_error:
            raise state.token_error
        return "token"
    class Graph:
        def __init__(self, token, **kwargs):
            state.calls.append(("graph", kwargs))
        def read_json(self, path, **kwargs):
            state.calls.append(("get", (path, kwargs)))
            value = state.pages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        def read_bytes(self, path, **kwargs):
            state.calls.append(("bytes", path))
            return b"%PDF-1.4 fake"
        def send_mail(self, body):
            state.calls.append(("send", copy.deepcopy(body)))
            if state.send_error:
                raise state.send_error
    def raw(user_id, row):
        state.raw.append(row)
        return {"id": "raw"}
    def claim(*args, **kwargs):
        state.calls.append(("claim", (args, kwargs)))
        return state.claim
    def finish(*args, **kwargs):
        state.calls.append(("finish", (args, kwargs)))
        return {"ok": True}
    monkeypatch.setattr(assistant_ms, "for_mailbox", for_mailbox)
    monkeypatch.setattr(assistant_ms, "sender_mailbox", lambda uid: state.profile["email"])
    monkeypatch.setattr(assistant_ms, "application_token", token)
    monkeypatch.setattr(assistant_ms, "configured", lambda: True)
    monkeypatch.setattr(mail, "GraphTransport", Graph)
    monkeypatch.setattr(send, "GraphTransport", Graph)
    monkeypatch.setattr(review, "GraphTransport", Graph)
    monkeypatch.setattr(send.supabase_client, "get_supabase", lambda: SimpleNamespace(table=lambda table: Query(state, table)))
    monkeypatch.setattr(repo, "human_profile", lambda uid: state.profile)
    monkeypatch.setattr(repo, "conversation_for_run", lambda uid, run: CONV)
    monkeypatch.setattr(repo, "get_conversation", lambda uid, cid: {"id": cid, "mode": state.mode})
    monkeypatch.setattr(repo, "mail_user_texts", lambda uid, run: ["Send to staff@example.com; use https://safe.example.com/X"])
    monkeypatch.setattr(repo, "mail_sensitive_context", lambda uid, run: state.sensitive)
    monkeypatch.setattr(repo, "tainted_results", lambda *a, **k: [])
    monkeypatch.setattr(repo, "staged_files_by_ids", lambda uid, ids: state.files)
    monkeypatch.setattr(repo, "insert_tool_result", raw)
    monkeypatch.setattr(repo, "approval_for_route", lambda uid, aid: state.approval)
    monkeypatch.setattr(repo, "get_approval", lambda uid, aid: state.approval)
    monkeypatch.setattr(repo, "mail_send_claim", claim)
    monkeypatch.setattr(repo, "mail_send_finish", finish)
    monkeypatch.setattr(repo, "mailbox_access_claim", lambda *a, **k: state.access_claim)
    monkeypatch.setattr(repo, "mailbox_access_outcome", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(repo, "mark_conversation_tainted", lambda *a, **k: state.calls.append(("taint", k)))
    monkeypatch.setattr(send.graph_email, "_active_test_session", lambda sb: None)
    monkeypatch.setattr(review.access, "owner_check", lambda uid: SimpleNamespace(problem=None, human=state.profile))
    monkeypatch.setattr(review, "get_settings", lambda: SimpleNamespace(mailbox_exclusion_set=set()))
    monkeypatch.setattr(review, "extract", lambda *a: "A bounded extracted summary.")
    from app.services import notifications
    monkeypatch.setattr(notifications, "audit", lambda *a, **k: None)
    state.Graph = Graph
    return state


def args():
    return {"to": [TARGET], "cc": [], "gc_contact_ids": [], "vendor_contact_ids": [],
            "subject": "Plans update", "body_text": "Please review the plans.", "attachment_ids": []}


def approved_send(h):
    original = args()
    prepared = send.prepare(OWNER, RUN, original)
    ctx = SimpleNamespace(approval_id=APPROVAL, now=datetime.now(timezone.utc))
    proposal = tools._draft_build(original, [prepared], ctx)
    body = proposal.request.json
    h.approval = {"status": "executing", "action_kind": "email_draft", "decided_at": datetime.now(timezone.utc).isoformat(),
                  "payload": {"args": original, "request": {"json": body}}}
    return body, proposal


def test_search_scopes_before_graph_and_bounds_window(h):
    with pytest.raises(mail.MailError):
        mail.search(OWNER, TARGET)
    assert not any(event == "get" for event, _ in h.calls)
    for days in [0, 91, True]:
        with pytest.raises(mail.MailError):
            mail.search(OWNER, EMAIL, window_days=days)


def test_search_escapes_odata_literal_and_reauthorizes_pages(h):
    path = mail.mailbox_path(EMAIL)
    h.pages = [{"value": [{"id": "one", "subject": "Title"}],
                "@odata.nextLink": "https://graph.microsoft.com" + path + "?$skiptoken=abc"},
               {"value": [{"id": "two"}]}]
    result = mail.search(OWNER, EMAIL, query="x' or true", limit=3)
    assert len(result["messages"]) == 2
    gets = [value for event, value in h.calls if event == "get"]
    assert "contains(subject,'x'' or true')" in gets[0][1]["params"]["$filter"]
    assert len([1 for event, _ in h.calls if event == "authorize"]) >= 3


@pytest.mark.parametrize("next_link", ["https://evil.example.com/v1.0/users/a/messages",
    "https://graph.microsoft.com/v1.0/users/staff@example.com/messages?$skiptoken=x",
    "https://graph.microsoft.com/v1.0/users/owner@example.com/contacts?$skiptoken=x"])
def test_pagination_cannot_change_origin_mailbox_or_operation(h, next_link):
    h.pages = [{"value": [], "@odata.nextLink": next_link}]
    with pytest.raises((mail.MailError, ValueError)):
        mail.search(OWNER, EMAIL)
    assert len([1 for event, _ in h.calls if event == "get"]) == 1


def test_read_stores_raw_out_of_line_and_never_returns_body(h):
    secret = "Long confidential raw email body"
    h.pages = [{"id": "message", "subject": "Plans", "body": {"contentType": "text", "content": secret}}]
    result = mail.read(OWNER, EMAIL, "message/id", conversation_id=CONV, run_id=RUN)
    assert result["summary"] == "A bounded extracted summary."
    assert secret not in str(result)
    assert secret in str(h.raw[0]["payload"])
    assert h.raw[0]["retention_class"] == "standard"
    assert "message%2Fid" in [value[0] for event, value in h.calls if event == "get"][0]


def test_retention_failure_releases_no_mail_result(h, monkeypatch):
    h.pages = [{"body": {"contentType": "text", "content": "secret"}}]
    monkeypatch.setattr(repo, "insert_tool_result", lambda *a: {})
    with pytest.raises(mail.MailError):
        mail.read(OWNER, EMAIL, "message", run_id=RUN)


def test_attachment_is_parent_checked_pdf_only_and_storage_path_not_model_controlled(h, monkeypatch):
    h.pages = [{"id": "message"}, {"id": "attachment", "name": "plans.pdf", "contentType": "application/pdf",
               "size": 12, "@odata.type": "#microsoft.graph.fileAttachment"}]
    staged = []
    monkeypatch.setattr(mail.assistant_staging, "stage", lambda user, name, content: (
        staged.append((user.id, name, content)) or {"id": FILE, "filename": name, "size_bytes": len(content)}, False))
    result = mail.attachment_to_staging(SimpleNamespace(id=OWNER), EMAIL, "message", "attachment")
    assert result["id"] == FILE and staged[0][0] == OWNER
    assert [value for event, value in h.calls if event == "get"][0][0].endswith("/messages/message")


@pytest.mark.parametrize("kind", ["#microsoft.graph.referenceAttachment", "#microsoft.graph.itemAttachment"])
def test_reference_or_item_attachment_never_downloaded(h, kind):
    h.pages = [{"id": "message"}, {"name": "plans.pdf", "contentType": "application/pdf", "size": 12, "@odata.type": kind}]
    with pytest.raises(mail.MailError):
        mail.attachment_to_staging(SimpleNamespace(id=OWNER), EMAIL, "message", "attachment")
    assert not any(event == "bytes" for event, _ in h.calls)


def test_draft_preview_freezes_all_markers_and_safe_recipient_roles(h):
    body, proposal = approved_send(h)
    preview = proposal.preview["email"]
    assert preview["sender"] == EMAIL and preview["reply_to"] == EMAIL
    assert preview["to"][0]["role"] == "executive"
    assert preview["header"]["name"] == "X-BDR-Assistant"
    assert preview["header"]["value"] == body["send_id"]
    assert preview["category"] == "Sent by assistant" and preview["footer"] == body["footer"]
    assert not any(event in {"send", "claim"} for event, _ in h.calls)


def test_injected_sender_or_hidden_recipient_fields_refused(h):
    for field in ["sender", "from", "bcc", "reply_to", "headers"]:
        with pytest.raises(mail.MailError):
            send.prepare(OWNER, RUN, {**args(), field: "attacker@example.com"})


def test_external_recipient_must_be_typed_or_contact_and_bid_amounts_fail_closed(h):
    external = {**args(), "to": ["attacker@example.com"]}
    with pytest.raises(mail.MailError):
        send.prepare(OWNER, RUN, external)
    h.recipient["role"] = "estimating_engineer_labor"
    with pytest.raises(mail.MailError):
        send.prepare(OWNER, RUN, {**args(), "body_text": "Actual bid total: $150,000"})


def test_href_destinations_require_exact_provenance_and_untrusted_html_escaped():
    rendered, shown, hrefs = send.render_body(
        '[Open](https://evil.example.com/leak) <img src=x onerror=alert(1)> https://safe.example.com/X',
        ["use https://safe.example.com/X"])
    assert "evil.example.com/leak" not in rendered
    assert "<img" not in rendered and "&lt;img" in rendered
    assert hrefs == [{"anchor": "https://safe.example.com/X", "href": "https://safe.example.com/X"}]
    assert "Open" in shown


def test_send_acceptance_claims_once_and_uses_only_primary_sender(h):
    body, _ = approved_send(h)
    result = send.dispatch(OWNER, RUN, APPROVAL, body)
    assert result["outcome"] == "accepted"
    events = [event for event, _ in h.calls]
    assert events.index("claim") < events.index("send")
    payload = next(value for event, value in h.calls if event == "send")
    assert payload["saveToSentItems"] is True
    assert payload["message"]["replyTo"][0]["emailAddress"]["address"] == EMAIL
    assert h.logs[0][1]["actor_kind"] == "assistant"


def test_timeout_is_uncertain_and_claim_replay_never_resends(h):
    body, _ = approved_send(h)
    h.send_error = GraphSendUncertain()
    assert send.dispatch(OWNER, RUN, APPROVAL, body)["outcome"] == "uncertain"
    h.claim = {"ok": True, "claimed": False, "status": "claimed"}
    assert send.dispatch(OWNER, RUN, APPROVAL, body)["outcome"] == "uncertain"
    assert len([1 for event, _ in h.calls if event == "send"]) == 1


def test_sender_remap_or_attachment_change_never_claims_dispatch(h):
    body, _ = approved_send(h)
    h.profile["email"] = "new@example.com"
    with pytest.raises(mail.MailError):
        send.dispatch(OWNER, RUN, APPROVAL, body)
    assert not any(event == "claim" for event, _ in h.calls)


def test_quota_refusal_never_dispatches(h):
    body, _ = approved_send(h)
    h.claim = {"ok": False, "code": "daily_cap"}
    with pytest.raises(mail.MailError):
        send.dispatch(OWNER, RUN, APPROVAL, body)
    assert not any(event == "send" for event, _ in h.calls)


def review_body(h):
    h.mode = "mailbox_review"
    body = {"target_upn": TARGET, "window_days": 30, "limit": 2}
    h.approval = {"status": "executing", "action_kind": "read_mailbox", "conversation_id": CONV,
        "decision_reason": "Reviewing this mailbox for project status", "payload": {"request": {"json": body}}}
    return body


def test_review_requires_role_mode_reason_and_exclusion_before_graph(h, monkeypatch):
    body = review_body(h)
    for change in ["mode", "role", "reason", "excluded"]:
        h.mode, h.profile["role"] = "mailbox_review", "executive"
        h.approval["decision_reason"] = "Review mailbox for project status"
        if change == "mode":
            h.mode = "normal"
        elif change == "role":
            h.profile["role"] = "accountant"
        elif change == "reason":
            h.approval["decision_reason"] = "short"
        else:
            monkeypatch.setattr(review, "get_settings", lambda: SimpleNamespace(mailbox_exclusion_set={TARGET}))
        with pytest.raises(mail.MailError):
            review.review(OWNER, RUN, APPROVAL, body)
    assert not any(event == "get" for event, _ in h.calls)


def test_review_retains_raw_for_seven_days_and_releases_only_extracted_summary(h):
    body = review_body(h)
    h.pages = [{"value": [{"id": "message"}]}, {"id": "message", "body": {"content": "secret raw body"}}]
    result = review.review(OWNER, RUN, APPROVAL, body)
    assert "secret raw body" not in str(result)
    assert "secret raw body" in str(h.raw[0]["payload"])
    assert h.raw[0]["retention_class"] == "cross_mailbox"
    assert result["row_count"] == 1
    assert ("taint", {"mailbox": True}) in h.calls


def test_review_extraction_or_audit_failure_never_returns_raw(h, monkeypatch):
    body = review_body(h)
    h.pages = [{"value": [{"id": "message"}]}, {"id": "message", "body": {"content": "secret raw body"}}]
    def failed(*a):
        raise RuntimeError("extraction secret")
    monkeypatch.setattr(review, "extract", failed)
    with pytest.raises(mail.MailError) as error:
        review.review(OWNER, RUN, APPROVAL, body)
    assert "secret" not in str(error.value)


def test_review_claim_replay_does_not_repeat_read(h):
    body = review_body(h)
    h.access_claim = {"ok": True, "claimed": False}
    with pytest.raises(mail.MailError):
        review.review(OWNER, RUN, APPROVAL, body)
    assert not any(event == "get" for event, _ in h.calls)


def test_after_dispatch_persistence_failure_is_uncertain_not_a_refused_send(h, monkeypatch):
    body, _ = approved_send(h)
    def failed(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(repo, "mail_send_finish", failed)
    assert send.dispatch(OWNER, RUN, APPROVAL, body)["outcome"] == "uncertain"
    assert len([1 for event, _ in h.calls if event == "send"]) == 1


def test_visible_footer_uses_actual_pacific_approval_date(h):
    body, _ = approved_send(h)
    assert "approval date" in body["footer"]
    h.approval["decided_at"] = "2026-10-28T01:00:00+00:00"
    send.dispatch(OWNER, RUN, APPROVAL, body)
    payload = next(value for event, value in h.calls if event == "send")
    content = payload["message"]["body"]["content"]
    assert "approved by Owner User on 2026-10-27" in content
    assert "on approval date" not in content
    assert content.index("approved by Owner User") > content.index("Office")


def test_testbench_redirect_is_applied_before_claim_and_records_effective_recipient(h, monkeypatch):
    from app.services import rfp_test
    body, _ = approved_send(h)
    monkeypatch.setattr(send.graph_email, "_active_test_session", lambda sb: {"id": "test"})
    def redirect(message, **kwargs):
        message["toRecipients"] = [{"emailAddress": {"address": "designated-test@example.com"}}]
        message["ccRecipients"] = []
        return SimpleNamespace()
    monkeypatch.setattr(rfp_test, "redirect", redirect)
    monkeypatch.setattr(rfp_test, "record_redirected", lambda *args, **kwargs: None)
    assert send.dispatch(OWNER, RUN, APPROVAL, body)["outcome"] == "accepted"
    claim = next(value for event, value in h.calls if event == "claim")
    assert claim[1]["effective_recipients"] == ["designated-test@example.com"]


def test_large_attachments_freeze_server_links_in_approval_preview(h, monkeypatch):
    from datetime import timedelta
    row = {"id": FILE, "filename": "plans.pdf", "size_bytes": 4 * 1024 * 1024,
           "sha256": "a" * 64, "mime": "application/pdf", "storage_path": "owned/path",
           "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    h.files = [row]
    monkeypatch.setattr(send, "has_bid_amounts", lambda *args: False)
    calls = []
    def signed(path, **kwargs):
        calls.append((path, kwargs))
        return "https://storage.example.com/owned.pdf?token=frozen"
    monkeypatch.setattr(send.storage, "signed_url", signed)
    original = {**args(), "attachment_ids": [FILE]}
    prepared = send.prepare(OWNER, RUN, original)
    proposal = tools._draft_build(original, [prepared], SimpleNamespace(approval_id=APPROVAL, now=datetime.now(timezone.utc)))
    assert proposal.preview["email"]["hrefs"] == [{"anchor": "plans.pdf", "href": "https://storage.example.com/owned.pdf?token=frozen"}]
    assert calls[0][1]["ttl_seconds"] == 21600
    # Recheck uses the already frozen server link instead of minting a different destination.
    rechecked = send.prepare(OWNER, RUN, original, frozen_attachment_links=prepared["attachment_links"])
    assert rechecked == prepared and len(calls) == 1


def test_model_cannot_supply_artifact_links(h):
    with pytest.raises(mail.MailError):
        send.prepare(OWNER, RUN, {**args(), "attachment_links": [{"href": "https://evil.example.com"}]})


@pytest.mark.parametrize("body", ["The value is 0x1e848.", "Use b25lIGh1bmRyZWQgdGhvdXNhbmQ=.",
                                  "One hundred twenty five thousand."])
def test_tainted_source_never_uses_amount_heuristic_to_allow_nonviewer(h, body):
    h.sensitive = True
    h.recipient["role"] = "estimating_engineer_labor"
    with pytest.raises(mail.MailError, match="Mail is unavailable"):
        send.prepare(OWNER, RUN, {**args(), "body_text": body})
    assert not any(event == "claim" for event, _ in h.calls)


def test_every_pdf_requires_authorized_recipients_even_raster_or_misleading_text(h, monkeypatch):
    from datetime import timedelta
    h.recipient["role"] = "estimating_engineer_labor"
    h.files = [{"id": FILE, "filename": "raster-plans.pdf", "size_bytes": 100,
        "sha256": "a" * 64, "mime": "application/pdf", "storage_path": "owned/file",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}]
    monkeypatch.setattr(send, "has_bid_amounts", lambda *a: pytest.fail("Attachment policy must not rely on text detection"))
    with pytest.raises(mail.MailError):
        send.prepare(OWNER, RUN, {**args(), "attachment_ids": [FILE]})


def test_authority_snapshot_records_source_taint_and_rechecks_it_at_dispatch(h):
    body, _ = approved_send(h)
    assert body["authority"]["source_tainted"] is False
    h.sensitive = True
    with pytest.raises(mail.MailError):
        send.dispatch(OWNER, RUN, APPROVAL, body)
    assert not any(event == "claim" for event, _ in h.calls)


def test_tainted_internal_viewer_send_remains_available_and_clearly_snapshotted(h):
    h.sensitive = True
    prepared = send.prepare(OWNER, RUN, args())
    assert prepared["authority"]["source_tainted"] is True
    assert prepared["to"][0]["role"] == "executive"
