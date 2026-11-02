"""Existing application Graph identity with live BDR authorization, no OAuth."""

from types import SimpleNamespace

import pytest

from app.assistant import repo
from app.services import assistant_ms as service

OWNER = "11111111-1111-4111-8111-111111111111"
EMAIL = "owner@example.com"
ALIAS = "alias@example.com"
TOKEN = "fake-application-token"


@pytest.fixture
def h(monkeypatch):
    state = SimpleNamespace(
        settings=SimpleNamespace(assistant_enabled=True, assistant_outlook_enabled=True,
                                 ms_tenant_id="existing-tenant", ms_client_id="existing-client",
                                 ms_client_secret="existing-secret",
                                 shared_mailbox_set={"bids@example.com", "office@example.com"}),
        profile={"id": OWNER, "email": EMAIL, "rfp_mailboxes": [ALIAS],
                 "is_active": True, "role": "executive"},
        global_row={"enabled": True}, profile_row={"enabled": True},
        problem=None, calls=[], token=TOKEN, provider_error=None,
    )
    monkeypatch.setattr(service, "get_settings", lambda: state.settings)
    def profile(user_id):
        assert user_id == OWNER
        state.calls.append("profile")
        return state.profile
    def owner(user_id):
        assert user_id == OWNER
        state.calls.append("owner_check")
        return SimpleNamespace(problem=state.problem, human=state.profile)
    def token():
        state.calls.append("token")
        if state.provider_error:
            raise state.provider_error
        return state.token
    monkeypatch.setattr(repo, "human_profile", profile)
    monkeypatch.setattr(repo, "settings_row", lambda user_id: state.global_row)
    monkeypatch.setattr(repo, "profile_row", lambda user_id: state.profile_row)
    monkeypatch.setattr(service.access, "owner_check", owner)
    monkeypatch.setattr(service.graph_email, "_acquire_token", token)
    return state


@pytest.mark.parametrize("operation", ["status", "application_token", "for_mailbox", "sender_mailbox"])
def test_disabled_integration_does_no_database_or_token_work(h, operation):
    h.settings.assistant_outlook_enabled = False
    if operation == "status":
        assert service.status(OWNER) == {"available": False, "authentication": "application",
                                         "mailbox": None, "shared_mailboxes": []}
    else:
        with pytest.raises(service.OutlookError):
            service.for_mailbox(OWNER, EMAIL) if operation == "for_mailbox" else getattr(service, operation)(OWNER)
    assert h.calls == []


@pytest.mark.parametrize("field", ["ms_tenant_id", "ms_client_id", "ms_client_secret"])
@pytest.mark.parametrize("value", [None, "", " ", 1])
def test_existing_application_credentials_are_required(h, field, value):
    setattr(h.settings, field, value)
    assert not service.configured()


def test_no_new_registration_or_oauth_settings_are_required(h):
    assert service.configured()
    assert not hasattr(h.settings, "assistant_ms_client_id")
    assert service.application_token(OWNER) == TOKEN
    assert h.calls == ["owner_check", "token"]


def test_status_is_safe_metadata_and_needs_no_delegation_or_graph(h, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Metadata cannot acquire a token or require a delegation")
    monkeypatch.setattr(service.access, "owner_check", unexpected)
    monkeypatch.setattr(service.graph_email, "_acquire_token", unexpected)
    monkeypatch.setattr(repo, "ensure_delegation", unexpected)
    monkeypatch.setattr(repo, "live_delegation", unexpected)
    assert service.status(OWNER) == {"available": True, "authentication": "application",
                                     "mailbox": EMAIL,
                                     "shared_mailboxes": ["bids@example.com", "office@example.com"]}
    assert h.calls == ["profile"]


@pytest.mark.parametrize("change", ["inactive", "external", "global_off", "profile_off", "missing_profile"])
def test_status_still_enforces_live_profile_and_assistant_switches(h, change):
    if change == "inactive":
        h.profile["is_active"] = False
    elif change == "external":
        h.profile["role"] = "estimator"
    elif change == "global_off":
        h.global_row["enabled"] = False
    elif change == "profile_off":
        h.profile_row["enabled"] = False
    else:
        h.profile = None
    with pytest.raises(service.OutlookError):
        service.status(OWNER)
    assert "token" not in h.calls


def test_status_has_no_alias_sender_fallback(h):
    h.profile["email"] = "Name <owner@example.com>"
    assert service.status(OWNER)["mailbox"] is None
    assert "token" not in h.calls


@pytest.mark.parametrize("problem", ["no_delegation", "owner_changed", "feature_off"])
@pytest.mark.parametrize("operation", ["application_token", "for_mailbox", "sender_mailbox"])
def test_live_authority_failure_precedes_token_work(h, problem, operation):
    h.problem = problem
    with pytest.raises(service.OutlookError):
        service.for_mailbox(OWNER, EMAIL) if operation == "for_mailbox" else getattr(service, operation)(OWNER)
    assert h.calls == ["owner_check"]


@pytest.mark.parametrize("role", ["executive", "it_admin", "accountant", "estimating_admin"])
def test_elevated_roles_cannot_bypass_finite_target_authorization(h, role):
    h.profile["role"] = role
    for mailbox in ["colleague@example.com", "unknown@example.com", "hr@example.com", "bad/path"]:
        with pytest.raises(service.OutlookError):
            service.for_mailbox(OWNER, mailbox)
    assert "token" not in h.calls
    assert service.for_mailbox(OWNER, " OWNER@EXAMPLE.COM ") == (EMAIL, TOKEN)
    assert service.for_mailbox(OWNER, ALIAS) == (ALIAS, TOKEN)
    assert service.for_mailbox(OWNER, "bids@example.com") == ("bids@example.com", TOKEN)


def test_profile_remapping_applies_before_token_acquisition(h):
    h.profile["email"] = "new@example.com"
    h.profile["rfp_mailboxes"] = []
    with pytest.raises(service.OutlookError):
        service.for_mailbox(OWNER, EMAIL)
    with pytest.raises(service.OutlookError):
        service.for_mailbox(OWNER, ALIAS)
    assert "token" not in h.calls
    assert service.for_mailbox(OWNER, "new@example.com") == ("new@example.com", TOKEN)


def test_sender_is_live_primary_email_only_and_cannot_be_selected(h):
    assert service.sender_mailbox(OWNER) == EMAIL
    assert "token" not in h.calls
    with pytest.raises(service.OutlookError):
        service.sender_mailbox(OWNER, ALIAS)
    h.profile["email"] = "new@example.com"
    assert service.sender_mailbox(OWNER) == "new@example.com"


@pytest.mark.parametrize("value", [None, "", "Name <owner@example.com>", "bad/path@example.com"])
def test_malformed_primary_email_never_falls_back_to_alias_or_shared_sender(h, value):
    h.profile["email"] = value
    with pytest.raises(service.OutlookError):
        service.sender_mailbox(OWNER)
    assert "token" not in h.calls


def test_shared_or_alias_send_selector_does_not_exist():
    for name in ["begin", "complete", "disconnect", "delegated_token"]:
        assert not hasattr(service, name)


@pytest.mark.parametrize("operation", ["application_token", "for_mailbox"])
def test_existing_provider_failure_is_sanitized(h, operation, caplog):
    h.provider_error = RuntimeError("secret-token-provider-response")
    with pytest.raises(service.OutlookError) as error:
        service.for_mailbox(OWNER, EMAIL) if operation == "for_mailbox" else service.application_token(OWNER)
    assert str(error.value) == "Outlook is unavailable."
    assert error.value.__suppress_context__
    assert caplog.text == ""


@pytest.mark.parametrize("token", [None, "", "token\r\nheader", "token value", "tokené"])
def test_bad_provider_token_is_never_returned(h, token):
    h.token = token
    with pytest.raises(service.OutlookError):
        service.application_token(OWNER)


def test_database_failure_is_sanitized(h, monkeypatch):
    def broken(user_id):
        raise RuntimeError("secret-database-payload")
    monkeypatch.setattr(repo, "human_profile", broken)
    with pytest.raises(service.OutlookError) as error:
        service.status(OWNER)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("profile", [None, {}, {"is_active": False, "role": "executive"},
                                     {"is_active": True, "role": "estimator"},
                                     {"is_active": True, "role": None}])
def test_malformed_or_inactive_live_profile_never_acquires_credentials(h, profile):
    h.profile = profile
    with pytest.raises(service.OutlookError):
        service.application_token(OWNER)
    assert "token" not in h.calls


def test_no_graph_mail_operation_is_executed(h, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("These helpers must not execute any Graph mail request")
    monkeypatch.setattr(service.graph_email, "graph_request", unexpected)
    monkeypatch.setattr(service.graph_email, "send_mail", unexpected)
    assert service.for_mailbox(OWNER, EMAIL) == (EMAIL, TOKEN)
