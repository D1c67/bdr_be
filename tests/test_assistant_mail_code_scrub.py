"""Browser v2 8.3 (WI-14): the model's own mail tools never see an emailed
code or a sign-in link. Fake Graph and repo only."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from app.assistant import repo
from app.services import assistant_browser_mfa_mail as mfa
from app.services import assistant_mail as mail
from app.services import assistant_mail_review as review
from app.services import assistant_ms

OWNER = "11111111-1111-4111-8111-111111111111"
RUN = "33333333-3333-4333-8333-333333333333"
CONV = "55555555-5555-4555-8555-555555555555"
APPROVAL = "22222222-2222-4222-8222-222222222222"
EMAIL = "owner@example.com"
TARGET = "staff@example.com"
TARGET_ID = "44444444-4444-4444-8444-444444444444"
CODE = "482913"
LINK = "https://login.procore.com/magic?token=Zx81kq"
SITES = [
    {"site_key": "procore", "code_email_senders": ["procore.com"], "code_email_regex": None},
    {"site_key": "autodesk", "code_email_senders": ["autodesk.com"], "code_email_regex": r"\b[A-Z]{6}\b(?= is your)"},
    {"site_key": "domain:example.org", "code_email_senders": ["example.org"], "code_email_regex": None},
]


def code_message(**extra):
    return {"id": "m1", "subject": f"Your Procore verification code is {CODE}",
            "from": {"emailAddress": {"address": "no-reply@procore.com", "name": "Procore"}},
            "receivedDateTime": "2026-11-02T17:00:00Z", "hasAttachments": False,
            "bodyPreview": f"Your verification code is {CODE}. Or sign in with {LINK}",
            "body": {"contentType": "text",
                     "content": f"Hello,\nYour verification code is {CODE}.\nOr sign in with this link: {LINK}\nThanks"},
            **extra}


def plain_message():
    return {"id": "m2", "subject": "Plans update for 26.10.7250",
            "from": {"emailAddress": {"address": "pm@gc.example", "name": "PM"}},
            "receivedDateTime": "2026-11-02T17:00:00Z", "hasAttachments": False,
            "bodyPreview": "Revised plans attached.",
            "body": {"contentType": "text", "content": "Revised plans attached. Cost code 16100, 2023 NEC code. "
                     "Bid due 11/14 at 2 PM, total 125000. Portal: https://app.gc.example/projects/7250"}}


@pytest.fixture
def h(monkeypatch):
    state = SimpleNamespace(pages=[], raw=[], extract_inputs=[], summary="A bounded extracted summary.",
                            sites=list(SITES), mfa_open=False, sites_error=None, target_open=False,
                            target_id=TARGET_ID)

    def for_mailbox(user_id, mailbox):
        if mailbox not in {EMAIL}:
            raise mail.MailError()
        return mailbox, "token"

    class Graph:
        def __init__(self, token, **kwargs):
            pass
        def read_json(self, path, **kwargs):
            return state.pages.pop(0)

    def sites(user_id, **kwargs):
        if state.sites_error:
            raise state.sites_error
        assert kwargs.get("include_disabled") is True
        return state.sites

    def extract(user_id, run_id, messages):
        state.extract_inputs.append(json.loads(json.dumps(messages)))
        return state.summary

    monkeypatch.setattr(assistant_ms, "for_mailbox", for_mailbox)
    monkeypatch.setattr(assistant_ms, "application_token", lambda uid: "token")
    monkeypatch.setattr(assistant_ms, "configured", lambda: True)
    monkeypatch.setattr(mail, "GraphTransport", Graph)
    monkeypatch.setattr(review, "GraphTransport", Graph)
    monkeypatch.setattr(review, "extract", extract)
    monkeypatch.setattr(repo, "browser_sites", sites)
    monkeypatch.setattr(repo, "browser_owner_code_help_open",
                        lambda uid: state.mfa_open if uid == OWNER else (uid == TARGET_ID and state.target_open))
    monkeypatch.setattr(mfa, "_profile_id_for", lambda address: state.target_id if address == TARGET else None)
    monkeypatch.setattr(repo, "insert_tool_result", lambda uid, row: state.raw.append(row) or {"id": "raw"})
    monkeypatch.setattr(repo, "conversation_for_run", lambda uid, run: CONV)
    monkeypatch.setattr(repo, "approval_for_route", lambda uid, aid: state.approval)
    monkeypatch.setattr(repo, "mailbox_access_claim", lambda *a, **k: {"ok": True, "claimed": True, "access_id": "a"})
    monkeypatch.setattr(repo, "mailbox_access_outcome", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(repo, "mark_conversation_tainted", lambda *a, **k: None)
    profile = {"id": OWNER, "email": EMAIL, "is_active": True, "role": "executive", "rfp_mailboxes": []}
    monkeypatch.setattr(review.access, "owner_check", lambda uid: SimpleNamespace(problem=None, human=profile))
    monkeypatch.setattr(review, "get_settings", lambda: SimpleNamespace(mailbox_exclusion_set=set()))
    return state


def no_code(value) -> bool:
    text = json.dumps(value)
    return CODE not in text and "Zx81kq" not in text and "magic?token" not in text


# ── email_read ───────────────────────────────────────────────────────────────


def test_email_read_on_a_code_email_returns_no_code_digits(h):
    h.pages = [code_message()]
    result = mail.read(OWNER, EMAIL, "m1", conversation_id=CONV, run_id=RUN)
    assert no_code(result)
    assert "[code]" in result["subject"]


def test_the_tool_results_row_has_no_code_and_no_sign_in_link(h):
    h.pages = [code_message()]
    mail.read(OWNER, EMAIL, "m1", conversation_id=CONV, run_id=RUN)
    row = h.raw[0]
    assert row["tool_name"] == "email_read" and row["tainted"] is True
    assert no_code(row["payload"])
    body = row["payload"]["message"]["body"]["content"]
    assert "[code]" in body and "[sign-in link]" in body
    assert row["payload_sha256"] and row["size_bytes"] == len(json.dumps(row["payload"], ensure_ascii=True).encode())


def test_a_code_email_reaches_the_extractor_with_no_body(h):
    h.pages = [code_message()]
    mail.read(OWNER, EMAIL, "m1", conversation_id=CONV, run_id=RUN)
    (sent,) = h.extract_inputs
    assert "body" not in sent[0] and no_code(sent)


def test_a_summary_that_echoes_a_code_is_masked(h):
    h.pages = [code_message()]
    h.summary = f"Procore sent the verification code {CODE}."
    result = mail.read(OWNER, EMAIL, "m1", conversation_id=CONV, run_id=RUN)
    assert CODE not in result["summary"] and "[code]" in result["summary"]


def test_an_ordinary_email_is_untouched(h):
    message = plain_message()
    h.pages = [message]
    result = mail.read(OWNER, EMAIL, "m2", conversation_id=CONV, run_id=RUN)
    assert result["subject"] == message["subject"]
    assert h.raw[0]["payload"]["message"] == message
    assert h.extract_inputs[0][0]["body"] == message["body"]["content"]


def test_a_listed_sender_without_a_keyword_is_still_a_code_email(h):
    message = code_message(subject="Sign-in attempt", bodyPreview="",
                           body={"contentType": "text", "content": f"Use {CODE} to finish signing in."})
    h.pages = [message]
    result = mail.read(OWNER, EMAIL, "m1", conversation_id=CONV, run_id=RUN)
    assert no_code(result) and no_code(h.raw[0]["payload"]) and "body" not in h.extract_inputs[0][0]


def test_a_site_regex_masks_a_letters_only_code(h):
    message = code_message(subject="Autodesk sign in", bodyPreview="",
                           **{"from": {"emailAddress": {"address": "noreply@autodesk.com"}}},
                           body={"contentType": "text", "content": "QWERTZ is your Autodesk sign-in code."})
    h.pages = [message]
    mail.read(OWNER, EMAIL, "m1", conversation_id=CONV, run_id=RUN)
    assert "QWERTZ" not in json.dumps(h.raw[0]["payload"])


def test_an_unlisted_sender_with_the_generic_pattern_is_masked(h):
    message = plain_message()
    message["subject"] = "Your Acme one-time passcode"
    message["body"]["content"] = f"Your one-time passcode is {CODE}."
    h.pages = [message]
    result = mail.read(OWNER, EMAIL, "m2", conversation_id=CONV, run_id=RUN)
    assert no_code(result) and no_code(h.raw[0]["payload"]) and "body" not in h.extract_inputs[0][0]


def test_every_message_is_masked_while_the_owner_has_an_open_code_help(h):
    h.mfa_open = True
    message = plain_message()
    message["body"]["content"] = f"Use {CODE} to log in. Plans: https://app.gc.example/projects/7250"
    h.pages = [message]
    mail.read(OWNER, EMAIL, "m2", conversation_id=CONV, run_id=RUN)
    stored = json.dumps(h.raw[0]["payload"])
    assert CODE not in stored and "https://app.gc.example/projects/7250" in stored
    # Not a code email by itself, so the masked body still reaches the extractor.
    assert CODE not in h.extract_inputs[0][0]["body"] and "Plans" in h.extract_inputs[0][0]["body"]


def test_a_registry_read_failure_fails_closed(h):
    h.sites_error = RuntimeError("database down")
    message = plain_message()
    message["body"]["content"] = f"Your security PIN {CODE} for the portal."
    h.pages = [message]
    mail.read(OWNER, EMAIL, "m2", conversation_id=CONV, run_id=RUN)
    assert CODE not in json.dumps(h.raw[0]["payload"])


# ── email_search ─────────────────────────────────────────────────────────────


def test_email_search_masks_codes_in_subjects(h):
    h.pages = [{"value": [code_message(), plain_message()]}]
    result = mail.search(OWNER, EMAIL, limit=5)
    subjects = [m["subject"] for m in result["messages"]]
    assert CODE not in json.dumps(result)
    assert subjects[1] == "Plans update for 26.10.7250"


# ── read_mailbox (review) ────────────────────────────────────────────────────


def test_mailbox_review_raw_rows_and_extractor_input_have_no_code(h, monkeypatch):
    monkeypatch.setattr(review, "target", lambda uid, run, upn: {"target_upn": TARGET, "conversation_id": CONV,
                                                               "mode": "mailbox_review", "exclusions": []})
    body = {"target_upn": TARGET, "window_days": 30, "limit": 2}
    h.approval = {"status": "executing", "action_kind": "read_mailbox", "conversation_id": CONV,
                  "decision_reason": "Reviewing this mailbox for project status",
                  "payload": {"request": {"json": body}}}
    h.pages = [{"value": [{"id": "m1"}, {"id": "m2"}]}, code_message(), plain_message()]
    result = review.review(OWNER, RUN, APPROVAL, body)
    assert result["row_count"] == 2 and no_code(result)
    assert no_code(h.raw[0]["payload"]) and h.raw[0]["tool_name"] == "read_mailbox"
    (sent,) = h.extract_inputs
    assert "body" not in sent[0] and "body" in sent[1] and no_code(sent)


# ── Router level: what the model receives from email_read ────────────────────


def test_the_email_read_route_returns_no_code(h, monkeypatch):
    from app.routers import assistant_mail as route

    monkeypatch.setattr(repo, "get_conversation", lambda uid, cid: {"id": cid, "mode": "normal"})
    h.pages = [code_message()]
    user = SimpleNamespace(id=OWNER, via_assistant_run_id=RUN)
    result = route.email_read(EMAIL, "m1", user=user)
    assert no_code(result)
    assert not re.search(r"\b\d{6}\b", json.dumps(result))


def test_scrub_signature_from_the_stub_still_works():
    masked = mfa.scrub(code_message(), sites=SITES, owner_mfa_open=False)
    assert no_code(masked) and masked["id"] == "m1"
    original = plain_message()
    assert mfa.scrub(original, sites=SITES, owner_mfa_open=False) is original


# ── Review findings: separators, word boundaries, the reviewed owner ─────────


@pytest.mark.parametrize("subject", [
    "Your verification code: 4829-1357",
    "Your verification code is 482 913",
    "Your verification code is ABC-123",
    "Sign in with code=482913",
    "Verification code #482913",
])
def test_codes_with_separators_or_after_a_mark_are_masked(subject):
    message = {"id": "m9", "subject": subject,
               "from": {"emailAddress": {"address": "no-reply@procore.com"}},
               "body": {"contentType": "text", "content": subject}}
    masked, code_email = mfa.scrub_view(message, sites=SITES, owner_mfa_open=False)
    assert code_email
    for part in ("4829", "1357", "482", "913", "ABC", "123"):
        if part in subject:
            assert part not in masked["subject"], (subject, masked["subject"])
    # An unlisted sender is still detected as a code email by the shape.
    other = {**message, "from": {"emailAddress": {"address": "alerts@portal.example"}}}
    assert mfa.is_code_email(other, sites=SITES)


@pytest.mark.parametrize("subject", ["Your Bonfire code: 482913", "Resource code: 482913"])
def test_a_word_ending_in_a_non_sign_in_word_keeps_the_code_keyword(subject):
    message = {"id": "m9", "subject": subject, "from": {"emailAddress": {"address": "x@portal.example"}}}
    assert mfa.is_code_email(message, sites=SITES)
    masked, _ = mfa.scrub_view(message, sites=SITES, owner_mfa_open=False)
    assert "482913" not in masked["subject"]


def test_a_cost_code_is_still_not_a_sign_in_code():
    message = {"id": "m9", "subject": "cost code 26-0500 for the panel",
               "from": {"emailAddress": {"address": "pm@gc.example"}}}
    assert not mfa.is_code_email(message, sites=SITES)
    assert mfa.scrub_view(message, sites=SITES, owner_mfa_open=False)[0] is message


def test_mailbox_review_masks_while_the_reviewed_owner_waits_on_a_code(h, monkeypatch):
    monkeypatch.setattr(review, "target", lambda uid, run, upn: {"target_upn": TARGET, "conversation_id": CONV,
                                                               "mode": "mailbox_review", "exclusions": []})
    body = {"target_upn": TARGET, "window_days": 30, "limit": 1}
    h.approval = {"status": "executing", "action_kind": "read_mailbox", "conversation_id": CONV,
                  "decision_reason": "Reviewing this mailbox for project status",
                  "payload": {"request": {"json": body}}}
    h.target_open = True     # the reviewer has no code help open; the owner does
    odd = {"id": "m1", "subject": "Portal login 7Q4-82X", "from": {"emailAddress": {"address": "x@portal.example"}},
           "receivedDateTime": "2026-11-02T17:00:00Z",
           "body": {"contentType": "text", "content": "Use 7Q4-82X to log in to the portal."}}
    h.pages = [{"value": [{"id": "m1"}]}, odd]
    review.review(OWNER, RUN, APPROVAL, body)
    assert "7Q4" not in json.dumps(h.raw[0]["payload"]) and "7Q4" not in json.dumps(h.extract_inputs)


def test_an_unresolved_review_target_fails_closed(monkeypatch):
    monkeypatch.setattr(mfa, "_profile_id_for", lambda address: None)
    monkeypatch.setattr(repo, "browser_owner_code_help_open", lambda uid: False)
    monkeypatch.setattr(repo, "browser_sites", lambda uid, **k: [])
    assert mfa.review_scrubber_for(OWNER, "gone@example.com").owner_mfa_open is True
    monkeypatch.setattr(mfa, "_profile_id_for", lambda address: TARGET_ID)
    assert mfa.review_scrubber_for(OWNER, TARGET).owner_mfa_open is False
