"""Browser v2 chat secret guard (docs/ASSISTANT_BROWSER_V2_PLAN.md 7.7, 8.1,
8.2, 18.1 "Secret guard"). Fakes only: repo reads are monkeypatched and the
real Supabase client is never built (conftest guard)."""
from __future__ import annotations

import base64
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import assistant_browser_help as help_flow
from app.services import assistant_browser_secrets as secrets
from app.services import assistant_browser_vault as vault

USER = "11111111-1111-4111-8111-111111111111"
CONV = "44444444-4444-4444-8444-444444444444"
JOB = "13131313-1313-4131-8131-131313131313"
HELP = "15151515-1515-4151-8151-151515151515"
SAVED_PASSWORD = "Tr0ub4dor&3xyz"
OTHER_SITE_PASSWORD = "c0rrect-Horse-77"


class World:
    def __init__(self) -> None:
        self.credentials: list[dict] = []
        self.credential_card = False
        self.code_helps: list[dict] = []
        self.help_state: dict | None = None
        self.sites: dict[str, dict] = {}
        self.delivered: list[dict] = []
        self.deliver_ok = True


@pytest.fixture
def world(monkeypatch):
    w = World()
    config = SimpleNamespace(assistant_browser_enabled=True, assistant_browser_v2_enabled=True,
                             user_cred_kek=base64.b64encode(b"k" * 32).decode(), user_cred_kek_id="u1",
                             user_cred_old_keys="")
    monkeypatch.setattr(secrets, "get_settings", lambda: config)
    monkeypatch.setattr(vault, "get_settings", lambda: config)
    monkeypatch.setattr(secrets.repo, "browser_credentials_v2_all", lambda user_id: list(w.credentials))
    monkeypatch.setattr(secrets.repo, "browser_requests_open",
                        lambda user_id, **kw: [{"id": "r"}] if w.credential_card and kw.get("kind") == "credential" else [])
    monkeypatch.setattr(secrets.repo, "browser_open_code_helps", lambda user_id, cid: list(w.code_helps))
    monkeypatch.setattr(secrets.repo, "browser_site", lambda user_id, key: w.sites.get(key))
    monkeypatch.setattr(secrets.repo, "browser_help_state", lambda user_id, job_id: w.help_state)

    def owner_reply(user_id, job_id, **kw):
        if not w.deliver_ok:
            raise help_flow.records.BrowserContentUnavailable("Browser code is unavailable.")
        w.delivered.append({"user_id": user_id, "job_id": job_id, **kw})
        return {}

    monkeypatch.setattr(help_flow, "owner_reply", owner_reply)
    w.config = config
    return w


def _save(world: World, password: str, site_key: str = "procore", status: str = "active") -> None:
    world.credentials.append({"id": str(uuid4()), "site_key": site_key, "status": status,
                              "fingerprint": vault.password_fingerprint(USER, password)})


def _open_code_card(world: World, site_key: str = "procore") -> None:
    world.code_helps = [{"job_id": JOB, "help_id": HELP, "site_key": site_key}]


def _guard(text: str) -> secrets.GuardResult:
    return secrets.guard_message(USER, CONV, text)


# ── Passwords (7.7) ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    f"my password is {SAVED_PASSWORD}", SAVED_PASSWORD, f"try '{SAVED_PASSWORD}'.", f"({SAVED_PASSWORD})",
])
def test_a_saved_password_is_refused(world, text):
    _save(world, SAVED_PASSWORD)
    with pytest.raises(secrets.SecretInMessage) as exc:
        _guard(text)
    assert exc.value.code == "secret_in_message"
    assert str(exc.value) == secrets.SECRET_IN_MESSAGE_TEXT


# A password that itself ends in punctuation (review finding: strip() removed
# the "!" that belongs to it).
PUNCT_PASSWORD = "Summer2026!"


@pytest.mark.parametrize("text", [
    f'my login is "{PUNCT_PASSWORD}"', f"the pw is {PUNCT_PASSWORD}.", f"({PUNCT_PASSWORD})",
    f"try {PUNCT_PASSWORD}, thanks", PUNCT_PASSWORD,
])
def test_a_saved_password_ending_in_punctuation_is_refused(world, text):
    _save(world, PUNCT_PASSWORD)
    with pytest.raises(secrets.SecretInMessage):
        _guard(text)


def test_help_text_refuses_a_quoted_password_ending_in_punctuation(world):
    _save(world, PUNCT_PASSWORD)
    with pytest.raises(secrets.SecretInMessage):
        secrets.guard_help_text(USER, JOB, f'use "{PUNCT_PASSWORD}"')


def test_the_guard_reads_every_saved_row_not_the_first_20(world, monkeypatch):
    for i in range(24):
        _save(world, f"Other-pass-{i:02d}x", site_key=f"domain:s{i}.example.com", status="revoked")
    _save(world, SAVED_PASSWORD, site_key="domain:zz.example.com")
    monkeypatch.setattr(secrets.repo, "browser_credentials", lambda user_id: list(world.credentials)[:20])
    with pytest.raises(secrets.SecretInMessage):
        _guard(f"use {SAVED_PASSWORD}")


def test_any_site_and_revoked_passwords_are_refused(world):
    _save(world, SAVED_PASSWORD, site_key="domain:example.com")
    _save(world, OTHER_SITE_PASSWORD, status="revoked")
    for text in (f"use {SAVED_PASSWORD}", f"use {OTHER_SITE_PASSWORD}"):
        with pytest.raises(secrets.SecretInMessage):
            _guard(text)


@pytest.mark.parametrize("text", ["password: hunter2", "Password=abc", "pw: x", "pwd = y", "the passcode is 1",
                                  "PASSWORDS: a, b"])
def test_password_phrases_are_refused(world, text):
    with pytest.raises(secrets.SecretInMessage):
        _guard(text)


@pytest.mark.parametrize("text", ["What is the password policy for Procore?", "reset my pw please",
                                  "Read the RFI list on Procore", "ok go ahead"])
def test_ordinary_text_passes_untouched(world, text):
    _save(world, SAVED_PASSWORD)
    result = _guard(text)
    assert result == secrets.GuardResult(content=text, intercepted_code=False, ask_for_code_card=False)


def test_guard_is_off_with_the_browser_off(world):
    world.config.assistant_browser_enabled = False
    world.config.assistant_browser_v2_enabled = False
    _save(world, SAVED_PASSWORD)
    assert _guard(f"password: {SAVED_PASSWORD}").content == f"password: {SAVED_PASSWORD}"


def test_a_vault_failure_still_applies_the_phrase_rules(world, monkeypatch):
    _save(world, SAVED_PASSWORD)
    world.config.user_cred_kek = ""
    assert _guard(SAVED_PASSWORD).content == SAVED_PASSWORD
    with pytest.raises(secrets.SecretInMessage):
        _guard("password: x")


# ── Credential card shapes (7.7 item 3) ──────────────────────────────────────


@pytest.mark.parametrize("text", ["me@example.com / Hunter22!", "jsmith:Winter2026!", "me@example.com pass9word",
                                  "Xk9#mQ2vLp", "aZ3kQ9vB7xT2mW8r"])
def test_credential_shapes_are_refused_while_a_credential_card_is_open(world, text):
    world.credential_card = True
    with pytest.raises(secrets.SecretInMessage):
        _guard(text)


@pytest.mark.parametrize("text", ["me@example.com / Hunter22!", "Xk9#mQ2vLp"])
def test_credential_shapes_pass_without_a_card(world, text):
    assert _guard(text).content == text


@pytest.mark.parametrize("text", ["I saved it on the card", "done", "use my work account please",
                                  "https://app.procore.com/1/rfis", "me@example.com"])
def test_plain_replies_pass_with_a_credential_card_open(world, text):
    world.credential_card = True
    assert _guard(text).content == text


# ── Code intercept (8.2) ─────────────────────────────────────────────────────


def test_a_lone_code_is_delivered_and_stored_as_the_stub(world):
    _open_code_card(world)
    result = _guard("482913")
    assert result == secrets.GuardResult(content=secrets.CODE_SENT_STUB, intercepted_code=True,
                                         ask_for_code_card=False)
    assert len(world.delivered) == 1
    sent = world.delivered[0]
    assert sent["job_id"] == JOB and sent["expected_help_id"] == HELP
    assert sent["secret_value"] == "482913" and sent["action"] == "message"


def test_code_in_a_sentence_is_masked_and_delivered(world):
    _open_code_card(world)
    result = _guard("here is the code AB12CD go")
    assert result.content == "here is the code [code] go"
    assert result.intercepted_code is True
    assert world.delivered[0]["secret_value"] == "AB12CD"


@pytest.mark.parametrize("text,code", [("123 456", "123456"), ("code is AB12CD", "AB12CD"),
                                       ("the code is 12-34-56", "123456"), ("my code 9F3K", "9F3K")])
def test_spaced_and_prefixed_codes_are_intercepted(world, text, code):
    _open_code_card(world)
    result = _guard(text)
    assert result.content == secrets.CODE_SENT_STUB
    assert world.delivered[0]["secret_value"] == code


@pytest.mark.parametrize("text", ["ok go ahead", "yes thanks", "yes do it", "cancel that", "continue please"])
def test_plain_words_are_never_intercepted(world, text):
    _open_code_card(world)
    result = _guard(text)
    assert result.content == text and not result.intercepted_code and not result.ask_for_code_card
    assert world.delivered == []


def test_two_codes_are_not_sent_and_ask_for_the_card(world):
    _open_code_card(world)
    result = _guard("either 123456 or 654321")
    assert result.content == "either [code] or [code]"
    assert result.ask_for_code_card is True and result.intercepted_code is False
    assert world.delivered == []


def test_two_open_code_cards_send_nothing(world):
    world.code_helps = [{"job_id": JOB, "help_id": HELP, "site_key": "procore"},
                        {"job_id": str(uuid4()), "help_id": str(uuid4()), "site_key": "autodesk"}]
    result = _guard("482913")
    assert result.content == "[code]" and result.ask_for_code_card is True
    assert world.delivered == []


def test_a_refused_delivery_still_never_stores_the_code(world):
    _open_code_card(world)
    world.deliver_ok = False
    result = _guard("482913")
    assert result.content == "[code]" and result.ask_for_code_card is True and result.intercepted_code is False


def test_no_code_card_means_no_intercept(world):
    assert _guard("482913").content == "482913"


def test_a_saved_password_shaped_like_a_code_is_refused_not_typed(world):
    _save(world, "4829135")
    _open_code_card(world)
    with pytest.raises(secrets.SecretInMessage):
        _guard("4829135")
    assert world.delivered == []


def test_a_code_while_a_credential_card_is_open_is_still_the_code(world):
    world.credential_card = True
    _open_code_card(world)
    assert _guard("A1B2C3").content == secrets.CODE_SENT_STUB


def test_site_code_regex_allows_letter_codes(world):
    world.sites["autodesk"] = {"site_key": "autodesk", "code_email_regex": r"[A-Z]{6}"}
    _open_code_card(world, "autodesk")
    assert _guard("QWERTY").content == secrets.CODE_SENT_STUB
    assert world.delivered[0]["secret_value"] == "QWERTY"


def test_code_tokens_shapes():
    assert secrets.code_tokens("123 456") == ["123456"]
    assert secrets.code_tokens("AB12CD and 99-88") == ["AB12CD", "9988"]
    assert secrets.code_tokens("ok go ahead") == []
    assert secrets.code_tokens("abc") == []
    assert secrets.code_tokens("1234567890123") == []  # 13 characters
    assert secrets.code_tokens("2026 RFI") == ["2026"]


# ── Help-text guard (8.1) ────────────────────────────────────────────────────


def test_help_text_refuses_any_saved_password(world):
    _save(world, SAVED_PASSWORD, site_key="autodesk")
    world.help_state = {"status": "open", "reason_code": "needs_info"}
    with pytest.raises(secrets.SecretInMessage):
        secrets.guard_help_text(USER, JOB, f"try {SAVED_PASSWORD}")


@pytest.mark.parametrize("reason", ["mfa", "mfa_step_up"])
def test_help_text_refuses_codes_while_mfa_help_is_open(world, reason):
    world.help_state = {"status": "open", "reason_code": reason, "is_login_scope": False}
    with pytest.raises(secrets.SecretInMessage):
        secrets.guard_help_text(USER, JOB, "the code is AB12CD")


def test_help_text_allows_codes_outside_mfa_help(world):
    world.help_state = {"status": "open", "reason_code": "needs_info"}
    secrets.guard_help_text(USER, JOB, "open project 2026 instead")


def test_help_text_fails_closed_when_the_state_read_fails(world, monkeypatch):
    def boom(*a):
        raise RuntimeError("down")

    monkeypatch.setattr(secrets.repo, "browser_help_state", boom)
    with pytest.raises(secrets.SecretInMessage):
        secrets.guard_help_text(USER, JOB, "123456")


# ── Nothing reaches the logs ─────────────────────────────────────────────────


def test_nothing_secret_is_logged(world, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _save(world, SAVED_PASSWORD)
    _open_code_card(world)
    with pytest.raises(secrets.SecretInMessage):
        _guard(f"pw {SAVED_PASSWORD}")
    _guard("code 482913")
    world.deliver_ok = False
    _guard("code 777111")

    def boom(*a, **k):
        raise RuntimeError(f"failed with {SAVED_PASSWORD} 482913")

    monkeypatch.setattr(secrets.repo, "browser_credentials_v2_all", boom)
    monkeypatch.setattr(secrets.repo, "browser_open_code_helps", boom)
    _guard("hello 555444")
    text = caplog.text
    for secret in (SAVED_PASSWORD, "482913", "777111", "555444"):
        assert secret not in text


# ── The send route (POST /assistant/conversations/{id}/messages) ─────────────


@pytest.fixture
def chat(world, monkeypatch):
    from app.core.deps import get_current_user, require_internal
    from app.routers import assistant as routes

    user = SimpleNamespace(id=USER, via_assistant_run_id=None, aal="aal2", mfa_enrolled=True, role="executive")
    inserted: list[dict] = []
    monkeypatch.setattr(routes, "_require_assistant", lambda user: None)
    monkeypatch.setattr(routes.repo, "get_conversation", lambda uid, cid: {"id": cid, "mode": "normal"})
    monkeypatch.setattr(routes.repo, "active_turn", lambda uid, cid: None)
    monkeypatch.setattr(routes, "_queue_available", lambda: True)
    monkeypatch.setattr(routes.repo, "ensure_delegation", lambda *a: None)
    monkeypatch.setattr(routes.repo, "ensure_profile", lambda *a: None)

    class Stop(Exception):
        pass

    def insert(uid, row):
        inserted.append(dict(row))
        raise Stop()

    monkeypatch.setattr(routes.repo, "insert_message", insert)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_internal] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False), inserted, Stop


def _send(http: TestClient, content: str):
    return http.post(f"/assistant/conversations/{CONV}/messages", json={"content": content})


def test_route_refuses_a_password_before_anything_is_stored(world, chat, caplog):
    http, inserted, _ = chat
    _save(world, SAVED_PASSWORD)
    result = _send(http, f"log in with {SAVED_PASSWORD}")
    assert result.status_code == 422
    assert result.headers["x-error-code"] == "secret_in_message"
    assert result.json()["detail"] == secrets.SECRET_IN_MESSAGE_TEXT
    assert SAVED_PASSWORD not in result.text and SAVED_PASSWORD not in caplog.text
    assert inserted == []


def test_route_stores_the_masked_text(world, chat):
    http, inserted, _ = chat
    _open_code_card(world)
    _send(http, "here is the code AB12CD go")
    assert inserted and inserted[0]["content"] == "here is the code [code] go"
    assert world.delivered[0]["secret_value"] == "AB12CD"


def test_route_stores_ordinary_text_unchanged(world, chat):
    http, inserted, _ = chat
    _send(http, "Read the RFI list on Procore")
    assert inserted[0]["content"] == "Read the RFI list on Procore"


def _accept_sends(monkeypatch, routes, inserted):
    """The send path past insert_message: rows come back, the turn enqueues."""
    def insert(uid, row):
        inserted.append(dict(row))
        return {**row, "id": str(uuid4()), "created_at": "2026-11-02T12:00:00+00:00", "blocks": [], "files": []}

    monkeypatch.setattr(routes.repo, "insert_message", insert)
    monkeypatch.setattr(routes, "_enqueue_turn", lambda *a: "job-1")
    monkeypatch.setattr(routes.repo, "update_message", lambda *a, **k: None)
    monkeypatch.setattr(routes.repo, "update_conversation", lambda *a, **k: None)


def test_route_asks_for_the_code_card_when_two_codes_are_masked(world, chat, monkeypatch):
    from app.routers import assistant as routes

    http, inserted, _ = chat
    _accept_sends(monkeypatch, routes, inserted)
    _open_code_card(world)
    result = _send(http, "it is 482913 or maybe 551234")
    assert result.status_code == 202, result.text
    assert result.json()["browser_code_card"] is True
    assert "482913" not in inserted[0]["content"] and world.delivered == []


def test_route_does_not_ask_for_the_code_card_for_ordinary_text(world, chat, monkeypatch):
    from app.routers import assistant as routes

    http, inserted, _ = chat
    _accept_sends(monkeypatch, routes, inserted)
    result = _send(http, "Read the RFI list on Procore")
    assert result.status_code == 202, result.text
    assert result.json()["browser_code_card"] is False
