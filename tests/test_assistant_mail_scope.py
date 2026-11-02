"""Finite app-only mailbox policy, including adversarial resource names."""

from types import SimpleNamespace

import pytest

from app.assistant import mail_scope as scope

SELF = "owner@example.com"
ALIAS = "alias@example.com"
SHARED = {"bids@example.com", "office@example.com", "assistant@example.com"}


@pytest.fixture
def profile():
    return {"email": SELF, "rfp_mailboxes": [ALIAS], "is_active": True, "role": "executive"}


@pytest.fixture
def settings():
    return SimpleNamespace(shared_mailbox_set=SHARED)


@pytest.mark.parametrize("raw", [None, 1, [], {}, "", "a", "a@localhost", "a@@example.com",
    "Name <a@example.com>", '"a"@example.com', "a/b@example.com", "a%2fb@example.com",
    "a?x@example.com", "a#x@example.com", "a@example.com/path", "a@example.com?x=y",
    "a@example.com\r\nBcc: victim@example.com", "a\t@example.com", "a@example.com\n",
    "a@éxample.com", "é@example.com", "a@ｅxample.com", "a@-example.com", "a@example-.com",
    "a@example..com", "a@example.com.", ".a@example.com", "a.@example.com", "a..b@example.com",
    "a b@example.com", "a@example.com,other@example.com", "a@example.com;other@example.com",
    "a" * 65 + "@example.com", "a@" + "b" * 64 + ".com", "a\x7f@example.com"])
def test_unsafe_addresses_are_not_resource_identifiers(raw):
    assert scope.normalize_address(raw) is None


@pytest.mark.parametrize("raw, expected", [
    (" Owner@Example.COM ", SELF),
    ("first.last+quotes@sub.example.com", "first.last+quotes@sub.example.com"),
    ("a_b-c@tenant.onmicrosoft.com", "a_b-c@tenant.onmicrosoft.com"),
])
def test_normalize_corporate_addresses(raw, expected):
    assert scope.normalize_address(raw) == expected



@pytest.mark.parametrize("role", ["executive", "it_admin", "accountant", "estimating_admin",
                                  "estimating_engineer_labor", "estimating_engineer_materials"])
def test_all_internal_roles_have_the_same_finite_scope(profile, settings, role):
    profile.update(role=role, is_dev=True)
    allowed = scope.application_allow_set(profile, settings)
    assert isinstance(allowed, frozenset)
    assert allowed == {SELF, ALIAS, *SHARED}
    for value in [SELF, ALIAS, *SHARED]:
        assert scope.require_application_mailbox(value.upper(), profile, settings) == value
    failures = []
    for value in ["colleague@example.com", "unknown@example.com", "hr@example.com", "bad/path"]:
        with pytest.raises(scope.MailScopeError) as error:
            scope.require_application_mailbox(value, profile, settings)
        failures.append((type(error.value), str(error.value), error.value.args))
    assert failures == [failures[0]] * len(failures)


@pytest.mark.parametrize("patch", [{"is_active": False}, {"is_active": None}, {"is_active": 1},
                                   {"role": "estimator"}, {"role": None}, {"role": "unknown"}])
def test_inactive_or_noninternal_owner_has_no_shared_access(profile, settings, patch):
    profile.update(patch)
    assert scope.application_allow_set(profile, settings) == frozenset()
    with pytest.raises(scope.MailScopeError):
        scope.require_application_mailbox("bids@example.com", profile, settings)


def test_missing_active_or_profile_fails_closed(profile, settings):
    profile.pop("is_active")
    assert scope.application_allow_set(profile, settings) == frozenset()
    assert scope.application_allow_set(None, settings) == frozenset()


def test_malformed_mapping_and_config_never_become_graph_paths(profile):
    profile["email"] = "Name <owner@example.com>"
    profile["rfp_mailboxes"] = [ALIAS, "bad/path@example.com", "a@example.com?x=1"]
    settings = {"shared_mailboxes": " BIDS@EXAMPLE.COM , bad/path, other@example.com?x=1"}
    assert scope.application_allow_set(profile, settings) == {ALIAS, "bids@example.com"}
    profile["rfp_mailboxes"] = ALIAS
    assert scope.application_allow_set(profile, {}) == frozenset()


def test_live_profile_remapping_changes_scope_immediately(profile, settings):
    profile["email"] = "new@example.com"
    profile["rfp_mailboxes"] = []
    assert scope.application_allow_set(profile, settings) == {"new@example.com", *SHARED}
    with pytest.raises(scope.MailScopeError):
        scope.require_application_mailbox(SELF, profile, settings)


def test_object_profile_and_settings_supported(profile):
    assert scope.application_allow_set(SimpleNamespace(**profile), {
        "shared_mailbox_set": ["BIDS@example.com"]}) == {SELF, ALIAS, "bids@example.com"}
