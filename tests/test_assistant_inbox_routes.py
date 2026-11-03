"""The proactive router's Inbox, Alerts and admin routes
(app/routers/assistant_inbox.py) against docs/ASSISTANT_PHASE7_PLAN.md 9 and
10.1.

Invariants pinned here:

  gates      the six internal roles reach the user routes with the flags on;
             the external estimator is refused by require_internal (403, the
             spine of every assistant router); a switched-off flag (env,
             company switch, assistant) answers a coded 404 inbox_off; a turn
             principal gets 403 everywhere; with the pilot allowlist set a
             non-allowlisted internal user gets assistant_inbox false and 404
             on every user route; both feature functions fail closed;
  admin      Executive and IT Admin only (403 otherwise), reachable with the
             company switch off, so an Executive can turn it back on;
  firewall   every (method, path) pair of the router is in DENY_LIST and the
             two prefixes are listed;
  rows       lists hide a record row whose source bell was dismissed, count a
             row read through its own bell, page by keyset, and offer buttons
             per row kind; feedback, mute, mute project, snooze and its limit;
  alerts     role default under user override, urgent is admin only (and
             approval_expiring takes it), record kinds never push, reset;
  audit      every admin default write, reset and company switch change writes
             its audit row first (a failing audit means no write);
  report     counts only, the health block carries events_lease_exhausted_7d;
  hygiene    no em dash, no model import, no send helper named in the router,
             main.py never names the assistant package.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, firewall, proactive, repo
from app.core import supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import assistant_inbox as inbox_router

from tests.assistant_fakes import (
    BELL, BELL2, DIGEST, INBOX, INBOX2, P7_OTHER, P7_T0, PROJECT, USER,
    FakeDB, bell_row, cursor_row, digest_row, inbox_row, profile, settings_row, subscription_row,
)

EM_DASH = chr(0x2014)
INBOX3 = "b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3"
INBOX4 = "b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4"
UNKNOWN = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c0c0"
NOW = P7_T0 + timedelta(hours=2)          # Friday 9:00 AM Pacific, a workday
ROOT = Path(__file__).resolve().parents[1]

USER_ROUTES = [
    ("GET", "/assistant/inbox", None),
    ("GET", "/assistant/inbox/summary", None),
    ("GET", f"/assistant/inbox/digests/{DIGEST}", None),
    ("POST", "/assistant/inbox/read", {"ids": None}),
    ("POST", "/assistant/inbox/dismiss", {"ids": []}),
    ("POST", f"/assistant/inbox/{INBOX}/feedback", {"value": "useful"}),
    ("POST", f"/assistant/inbox/{INBOX}/snooze", None),
    ("GET", "/assistant/alerts", None),
    ("PUT", "/assistant/alerts/todo.due", {"mode": "inbox"}),
    ("POST", "/assistant/alerts/todo.due/reset", None),
    ("POST", f"/assistant/alerts/mutes/{PROJECT}/remove", None),
    ("PUT", "/assistant/alerts/me/digest-email", {"enabled": True}),
]
ADMIN_ROUTES = [
    ("GET", "/assistant/alerts/defaults", None),
    ("PUT", "/assistant/alerts/defaults/executive/todo.due", {"mode": "inbox"}),
    ("POST", "/assistant/alerts/defaults/executive/todo.due/reset", None),
    ("GET", "/assistant/alerts/company", None),
    ("PUT", "/assistant/alerts/company", {"enabled": True}),
    ("GET", "/assistant/alerts/company/triage", None),
    ("PUT", "/assistant/alerts/company/triage", {"enabled": True}),
    ("GET", "/assistant/inbox/feedback-report", None),
]


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()],
        assistant_settings=[settings_row(proactive_enabled=True)],
        assistant_profiles=[], assistant_inbox=[], assistant_subscriptions=[], assistant_digests=[],
        assistant_project_mutes=[], assistant_router_cursors=[cursor_row(), cursor_row(name="audit_log")],
        notifications=[], audit_log=[],
        projects=[{"id": PROJECT, "number": "26.10.7301", "name": "Alpha Clinic"}],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    settings = get_settings()
    monkeypatch.setattr(settings, "bidding_enabled", True)
    monkeypatch.setattr(settings, "assistant_proactive_enabled", True)
    monkeypatch.setattr(settings, "assistant_proactive_user_allowlist", "")
    monkeypatch.setattr(settings, "assistant_proactive_external_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    clock = {"now": NOW}
    monkeypatch.setattr(inbox_router, "_now", lambda: clock["now"])
    from app.services import assistant_delivery, notifications

    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    channel_state = {"teams": {"available": True, "enabled": True, "connected": True,
                               "last_sent_at": "2026-10-30T15:00:00+00:00", "last_error_at": None},
                     "email": {"available": True}}
    monkeypatch.setattr(assistant_delivery, "channels", lambda user_id: channel_state)
    state = SimpleNamespace(role=Role.EXECUTIVE, via=None, email="tom@g3.test", uid=USER)

    def _user():
        return CurrentUser(id=state.uid, email=state.email, role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(inbox_router.router)
    app.dependency_overrides[get_current_user] = _user
    return SimpleNamespace(client=TestClient(app), db=db, state=state, clock=clock, settings=settings,
                           channels=channel_state)


def _call(api, method, path, body=None):
    return api.client.request(method, path, json=body) if body is not None else api.client.request(method, path)


def _put(api, row: dict) -> dict:
    api.db.tables.setdefault("assistant_inbox", []).append(row)
    return row


def _router_row(**over) -> dict:
    base = {"delivered_at": P7_T0.isoformat(), "severity": "high", "event_type": "todo.due",
            "delivery": "immediate", "notification_id": BELL}
    base.update(over)
    return inbox_row(**base)


def _record_row(**over) -> dict:
    base = {"id": INBOX2, "dedupe_key": "p7:bell:b1", "delivery": "record", "severity": "low",
            "event_type": "bell.late_quote", "title": "A late quote", "event_id": None,
            "delivered_at": P7_T0.isoformat(), "deliver_after": None, "source_notification_id": BELL2,
            "notification_id": None, "project_id": PROJECT}
    base.update(over)
    return inbox_row(**base)


def _direct_row(**over) -> dict:
    base = {"id": INBOX3, "dedupe_key": "calendar:ev1", "delivery": "direct", "severity": "normal",
            "event_type": "assistant.calendar", "event_id": None, "delivered_at": P7_T0.isoformat(),
            "deliver_after": None}
    base.update(over)
    return inbox_row(**base)


def _audit_rows(api, action: str) -> list[dict]:
    return [r for r in api.db.tables["audit_log"] if r["action"] == action]


# -- Gates --------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES, key=lambda r: r.value))
def test_every_internal_role_reaches_the_user_routes(api, role):
    api.state.role = role
    assert _call(api, "GET", "/assistant/inbox/summary").json() == {"unread": 0, "high_unread": 0, "last_digest": None}
    assert _call(api, "GET", "/assistant/inbox").json() == {"items": [], "next": None}
    assert _call(api, "GET", "/assistant/alerts").status_code == 200
    # Every user route, with one router row and one digest so the id routes reach their handlers.
    _put(api, _router_row())
    api.db.tables["assistant_digests"].append(digest_row())
    for method, path, body in USER_ROUTES:
        res = _call(api, method, path, body)
        assert res.status_code not in (401, 403, 404, 500), (role, method, path, res.status_code, res.text)


def test_the_external_estimator_is_refused_by_the_spine(api):
    api.state.role = Role.ESTIMATOR
    for method, path, body in USER_ROUTES + ADMIN_ROUTES:
        assert _call(api, method, path, body).status_code == 403, (method, path)
    flag_user = CurrentUser(id=USER, email="e@g3.test", role=Role.ESTIMATOR, is_active=True)
    assert inbox_router.features_flag(flag_user) is False
    assert inbox_router.proactive_admin_flag(flag_user) is False


@pytest.mark.parametrize("off", ["env", "proactive_env", "company", "assistant", "profile"])
def test_a_switched_off_flag_gives_a_coded_404_on_every_user_route(api, monkeypatch, off):
    if off == "env":
        monkeypatch.setattr(access, "env_enabled", lambda: False)
    elif off == "proactive_env":
        monkeypatch.setattr(api.settings, "assistant_proactive_enabled", False)
    elif off == "company":
        api.db.tables["assistant_settings"][0]["proactive_enabled"] = False
    elif off == "assistant":
        api.db.tables["assistant_settings"][0]["enabled"] = False
    else:
        api.db.tables["assistant_profiles"].append({"user_id": USER, "enabled": False})
    for method, path, body in USER_ROUTES:
        res = _call(api, method, path, body)
        assert res.status_code == 404, (method, path, res.text)
        assert res.json()["detail"]["code"] == "inbox_off"


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES, key=lambda r: r.value))
def test_every_admin_route_is_a_coded_404_with_the_env_flag_off_for_every_role(api, monkeypatch, role):
    monkeypatch.setattr(api.settings, "assistant_proactive_enabled", False)
    api.state.role = role
    for method, path, body in ADMIN_ROUTES:
        res = _call(api, method, path, body)
        assert res.status_code == 404, (role, method, path, res.status_code)
        assert res.json()["detail"]["code"] == "inbox_off"
    assert api.db.tables["audit_log"] == []


def test_the_turn_principal_gets_403_on_every_route(api):
    api.state.via = "22222222-2222-4222-8222-222222222222"
    for method, path, body in USER_ROUTES + ADMIN_ROUTES:
        res = _call(api, method, path, body)
        assert res.status_code == 403, (method, path)
        assert res.json()["detail"]["code"] == "assistant_cannot_approve"
    assert api.db.tables["audit_log"] == []


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES, key=lambda r: r.value))
def test_admin_routes_refuse_every_role_but_executive_and_it_admin(api, role):
    api.state.role = role
    allowed = role in (Role.EXECUTIVE, Role.IT_ADMIN)
    for method, path, body in ADMIN_ROUTES:
        res = _call(api, method, path, body)
        assert (res.status_code != 403) is allowed, (role, method, path, res.status_code)
    if not allowed:
        assert api.db.tables["audit_log"] == []


def test_admin_routes_stay_reachable_with_the_company_switch_off(api):
    api.db.tables["assistant_settings"][0]["proactive_enabled"] = False
    assert _call(api, "GET", "/assistant/alerts").status_code == 404
    assert _call(api, "GET", "/assistant/alerts/defaults").status_code == 200
    assert _call(api, "GET", "/assistant/inbox/feedback-report").status_code == 200
    res = _call(api, "PUT", "/assistant/alerts/company", {"enabled": True})
    assert res.status_code == 200 and res.json() == {"enabled": True}
    assert api.db.tables["assistant_settings"][0]["proactive_enabled"] is True
    assert _call(api, "GET", "/assistant/alerts").status_code == 200
    # The admin gate does need the env flag and the assistant itself.
    api.settings.assistant_proactive_enabled = False
    assert _call(api, "GET", "/assistant/alerts/defaults").status_code == 404


def test_an_estimating_admin_cannot_flip_the_company_switch(api):
    api.db.tables["assistant_settings"][0]["proactive_enabled"] = False
    api.state.role = Role.ESTIMATING_ADMIN
    assert _call(api, "PUT", "/assistant/alerts/company", {"enabled": True}).status_code == 403
    assert api.db.tables["assistant_settings"][0]["proactive_enabled"] is False
    assert _audit_rows(api, "assistant.proactive_switch") == []


def test_the_allowlist_narrows_the_flag_and_every_user_route(api):
    api.settings.assistant_proactive_user_allowlist = f" {P7_OTHER}, someone.else@g3.test "
    me = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    assert inbox_router.features_flag(me) is False
    for method, path, body in USER_ROUTES:
        assert _call(api, method, path, body).status_code == 404, (method, path)
    # The admin block does not look at the allowlist.
    assert inbox_router.proactive_admin_flag(me) is True
    assert _call(api, "GET", "/assistant/alerts/defaults").status_code == 200
    # Listed by id or by email (case insensitive).
    api.settings.assistant_proactive_user_allowlist = f"{P7_OTHER},{USER.upper()}"
    assert inbox_router.features_flag(me) is True
    api.settings.assistant_proactive_user_allowlist = "TOM@g3.test"
    assert inbox_router.features_flag(me) is True
    assert _call(api, "GET", "/assistant/inbox/summary").status_code == 200


def test_both_feature_functions_fail_closed(api, monkeypatch):
    me = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    assert inbox_router.features_flag(me) is True and inbox_router.proactive_admin_flag(me) is True

    def boom(user_id):
        raise RuntimeError("database is down")

    monkeypatch.setattr(repo, "settings_row", boom)
    assert inbox_router.features_flag(me) is False
    assert inbox_router.proactive_admin_flag(me) is False
    assert _call(api, "GET", "/assistant/inbox/summary").status_code == 404
    assert inbox_router.features_flag(None) is False
    assert inbox_router.proactive_admin_flag(None) is False


def test_a_missing_settings_row_fails_closed(api):
    api.db.tables["assistant_settings"].clear()
    me = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    assert inbox_router.features_flag(me) is False
    assert inbox_router.proactive_admin_flag(me) is False


def test_main_features_carries_both_keys(api):
    from app.main import features

    me = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    flags = features(me)
    assert flags["assistant_inbox"] is True and flags["assistant_proactive_admin"] is True
    api.db.tables["assistant_settings"][0]["proactive_enabled"] = False
    flags = features(me)
    assert flags["assistant_inbox"] is False and flags["assistant_proactive_admin"] is True
    api.state.role = Role.ESTIMATING_ADMIN
    flags = features(CurrentUser(id=USER, email="tom@g3.test", role=Role.ESTIMATING_ADMIN, is_active=True))
    assert flags["assistant_proactive_admin"] is False


# -- Firewall -----------------------------------------------------------------


def test_every_route_pair_is_in_the_firewall_deny_list():
    pairs = {(method, route.path) for route in inbox_router.router.routes for method in route.methods}
    missing = pairs - firewall.DENY_LIST
    assert missing == set(), missing
    assert pairs == set(firewall._ASSISTANT_INBOX)
    assert "/assistant/inbox" in firewall.DENY_PATH_PREFIXES and "/assistant/alerts" in firewall.DENY_PATH_PREFIXES
    for _, path in pairs:
        assert path.startswith(("/assistant/inbox", "/assistant/alerts"))


def test_the_prefixes_catch_a_route_added_later():
    assert any("/assistant/inbox/new-thing".startswith(p) for p in firewall.DENY_PATH_PREFIXES)
    assert any("/assistant/alerts/new-thing".startswith(p) for p in firewall.DENY_PATH_PREFIXES)


# -- Listing ------------------------------------------------------------------


def test_the_list_shapes_a_router_row_with_its_project_and_buttons(api):
    api.db.tables["notifications"].append(bell_row(id=BELL))
    _put(api, _router_row(project_id=PROJECT, digest_id=None, why="You own it."))
    body = _call(api, "GET", "/assistant/inbox?segment=all").json()
    assert body["next"] is None
    (item,) = body["items"]
    assert item == {
        "id": INBOX, "event_type": "todo.due", "kind_label": "To-do due", "group": "Personal", "severity": "high",
        "title": "A to-do is due today", "why": "You own it.", "delivery": "immediate", "precedence": None,
        "project": {"id": PROJECT, "number": "26.10.7301", "name": "Alpha Clinic"}, "digest_id": None,
        "created_at": P7_T0.isoformat(), "delivered_at": P7_T0.isoformat(), "read": False, "feedback": None,
        "actions": ["useful", "not_useful", "mute", "mute_project", "snooze", "dismiss"],
        "triage": None,
    }


def test_buttons_follow_the_row_kind(api):
    api.db.tables["notifications"].extend([bell_row(id=BELL2)])
    _put(api, _record_row())
    _put(api, _direct_row())
    _put(api, _router_row(id=INBOX4, event_type="assistant.approval_expiring", snooze_count=3, project_id=None,
                          created_at=(P7_T0 + timedelta(minutes=1)).isoformat(), dedupe_key="p7:x"))
    items = {i["id"]: i for i in _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]}
    assert items[INBOX3]["actions"] == ["useful", "not_useful", "dismiss"]
    assert items[INBOX2]["actions"] == ["useful", "not_useful", "mute", "mute_project", "dismiss"]
    assert items[INBOX4]["actions"] == ["useful", "not_useful", "mute", "dismiss"]
    assert items[INBOX3]["kind_label"] == "Calendar reminder" and items[INBOX3]["group"] == "Assistant"
    assert items[INBOX2]["kind_label"] == "Late vendor quotes"
    assert items[INBOX3]["read"] is True


def test_read_state_per_row_kind_and_the_unread_segment(api):
    api.db.tables["notifications"].extend([
        bell_row(id=BELL, read_at=None), bell_row(id=BELL2, read_at=P7_T0.isoformat())])
    _put(api, _router_row())                                      # unread
    _put(api, _router_row(id=INBOX4, dedupe_key="p7:b", notification_id=BELL2))   # its own bell was read
    _put(api, _record_row())                                      # source bell read
    _put(api, _direct_row())
    items = {i["id"]: i for i in _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]}
    assert (items[INBOX]["read"], items[INBOX4]["read"], items[INBOX2]["read"], items[INBOX3]["read"]) == (False, True, True, True)
    unread = _call(api, "GET", "/assistant/inbox?segment=unread").json()["items"]
    assert [i["id"] for i in unread] == [INBOX]


def test_a_record_row_whose_source_bell_was_dismissed_is_left_out(api):
    api.db.tables["notifications"].append(bell_row(id=BELL2, dismissed_at=P7_T0.isoformat()))
    _put(api, _record_row())
    assert _call(api, "GET", "/assistant/inbox?segment=all").json()["items"] == []


def test_dismissed_and_undelivered_rows_are_not_listed(api):
    _put(api, _router_row(dismissed_at=P7_T0.isoformat(), dismissed_reason="user"))
    _put(api, _router_row(id=INBOX4, dedupe_key="p7:c", delivered_at=None))
    assert _call(api, "GET", "/assistant/inbox?segment=all").json()["items"] == []


def test_paging_by_keyset_cursor(api):
    for n in range(5):
        _put(api, _router_row(id=f"b{n}b{n}b{n}b{n}-b{n}b{n}-4b{n}b-8b{n}b-b{n}b{n}b{n}b{n}b{n}b{n}",
                              dedupe_key=f"p7:k{n}", notification_id=None,
                              created_at=(P7_T0 + timedelta(minutes=n)).isoformat()))
    first = _call(api, "GET", "/assistant/inbox?segment=all&limit=2").json()
    assert len(first["items"]) == 2 and first["next"]
    second = _call(api, "GET", f"/assistant/inbox?segment=all&limit=2&before={first['next']}")
    page2 = second.json()
    third = _call(api, "GET", f"/assistant/inbox?segment=all&limit=2&before={page2['next']}").json()
    seen = [i["id"] for i in first["items"] + page2["items"] + third["items"]]
    assert len(seen) == 5 and len(set(seen)) == 5
    created = [i["created_at"] for i in first["items"] + page2["items"] + third["items"]]
    assert created == sorted(created, reverse=True)
    assert third["next"] is None


def test_a_page_skips_hidden_rows_without_ending_early(api):
    api.db.tables["notifications"].append(bell_row(id=BELL2, dismissed_at=P7_T0.isoformat()))
    for n in range(4):
        _put(api, _record_row(id=f"c{n}c{n}c{n}c{n}-c{n}c{n}-4c{n}c-8c{n}c-c{n}c{n}c{n}c{n}c{n}c{n}", dedupe_key=f"p7:bell:h{n}",
                              created_at=(P7_T0 + timedelta(minutes=10 + n)).isoformat()))
    _put(api, _router_row(created_at=P7_T0.isoformat()))
    body = _call(api, "GET", "/assistant/inbox?segment=all&limit=1").json()
    assert [i["id"] for i in body["items"]] == [INBOX] and body["next"] is None


def test_bad_list_arguments(api):
    assert _call(api, "GET", "/assistant/inbox?segment=bogus").status_code == 422
    assert _call(api, "GET", "/assistant/inbox?limit=0").status_code == 422
    assert _call(api, "GET", "/assistant/inbox?limit=51").status_code == 422
    for bad in ("yesterday", "2026-10-30T14:00:00Z|nope"):
        res = _call(api, "GET", f"/assistant/inbox?before={bad}")
        assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_cursor"


def test_the_digests_segment_sends_the_digests_behind_the_items(api):
    api.db.tables["assistant_digests"].append(digest_row(item_count=1, record_count=4))
    _put(api, _router_row(delivery="digest", digest_id=DIGEST, severity="normal"))
    _put(api, _router_row(id=INBOX4, dedupe_key="p7:d", notification_id=None))
    body = _call(api, "GET", "/assistant/inbox?segment=digests").json()
    assert [i["id"] for i in body["items"]] == [INBOX]
    assert body["items"][0]["digest_id"] == DIGEST
    (digest,) = body["digests"]
    assert digest["id"] == DIGEST and digest["slot"] == "am" and digest["item_count"] == 1 and digest["record_count"] == 4
    assert digest["digest_date"] == "2026-10-30"


def test_another_persons_rows_never_appear(api):
    _put(api, _router_row(user_id=P7_OTHER))
    assert _call(api, "GET", "/assistant/inbox?segment=all").json()["items"] == []
    assert _call(api, "POST", f"/assistant/inbox/{INBOX}/feedback", {"value": "useful"}).status_code == 404
    assert _call(api, "POST", f"/assistant/inbox/{INBOX}/snooze").status_code == 404
    assert _call(api, "POST", "/assistant/inbox/dismiss", {"ids": [INBOX]}).json() == {"updated": 0}
    assert api.db.tables["assistant_inbox"][0]["dismissed_at"] is None


def test_summary_counts_router_rows_only_and_subtracts_a_read_bell(api):
    api.db.tables["notifications"].extend([bell_row(id=BELL), bell_row(id=BELL2, read_at=P7_T0.isoformat())])
    _put(api, _router_row(severity="high"))
    _put(api, _router_row(id=INBOX4, dedupe_key="p7:u", severity="urgent", notification_id=None))
    _put(api, _router_row(id=INBOX2, dedupe_key="p7:n", severity="normal", delivery="digest", notification_id=None))
    _put(api, _router_row(id=INBOX3, dedupe_key="p7:r", severity="high", notification_id=BELL2))   # read bell
    _put(api, _record_row(id="d1d1d1d1-aaaa-4aaa-8aaa-aaaaaaaaaaaa", dedupe_key="p7:bell:z"))
    _put(api, _direct_row(id="d2d2d2d2-aaaa-4aaa-8aaa-aaaaaaaaaaaa", dedupe_key="calendar:z"))
    api.db.tables["assistant_digests"].append(digest_row(item_count=3, record_count=9))
    body = _call(api, "GET", "/assistant/inbox/summary").json()
    assert body["unread"] == 3 and body["high_unread"] == 2
    assert body["last_digest"]["id"] == DIGEST and body["last_digest"]["item_count"] == 3


def test_digest_detail(api):
    api.db.tables["assistant_digests"].append(digest_row(item_count=2))
    _put(api, _router_row(delivery="digest", digest_id=DIGEST, severity="normal"))
    _put(api, _router_row(id=INBOX4, dedupe_key="p7:g", delivery="digest", digest_id=DIGEST, severity="normal",
                          dismissed_at=P7_T0.isoformat(), dismissed_reason="user"))
    body = _call(api, "GET", f"/assistant/inbox/digests/{DIGEST}").json()
    assert body["digest"]["id"] == DIGEST and [i["id"] for i in body["items"]] == [INBOX]
    assert _call(api, "GET", f"/assistant/inbox/digests/{UNKNOWN}").status_code == 404
    assert _call(api, "GET", "/assistant/inbox/digests/not-a-uuid").status_code == 404
    api.state.uid = P7_OTHER
    api.db.tables["profiles"].append(profile(id=P7_OTHER))
    assert _call(api, "GET", f"/assistant/inbox/digests/{DIGEST}").json()["detail"]["code"] == "not_found"


# -- Read and dismiss ---------------------------------------------------------


def test_mark_read_and_mark_all_read(api):
    api.db.tables["notifications"].extend([bell_row(id=BELL), bell_row(id=BELL2)])
    _put(api, _router_row())
    _put(api, _record_row())
    res = _call(api, "POST", "/assistant/inbox/read", {"ids": [INBOX]})
    assert res.status_code == 200 and res.json() == {"updated": 1}
    assert api.db.tables["assistant_inbox"][0]["read_at"] is not None
    assert next(n for n in api.db.tables["notifications"] if n["id"] == BELL)["read_at"] is not None
    # A record row's source bell is never written.
    assert _call(api, "POST", "/assistant/inbox/read", {"ids": None}).status_code == 200
    assert next(n for n in api.db.tables["notifications"] if n["id"] == BELL2)["read_at"] is None


def test_read_and_dismiss_bound_the_id_list(api):
    ids = [UNKNOWN] * 201
    assert _call(api, "POST", "/assistant/inbox/read", {"ids": ids}).status_code == 422
    assert _call(api, "POST", "/assistant/inbox/dismiss", {"ids": ids}).status_code == 422
    assert _call(api, "POST", "/assistant/inbox/read", {"ids": [UNKNOWN] * 200}).json() == {"updated": 0}
    assert _call(api, "POST", "/assistant/inbox/dismiss", {"ids": ["not-a-uuid"]}).json() == {"updated": 0}


def test_dismiss_reads_the_routers_own_bell_and_leaves_a_record_source_bell(api):
    api.db.tables["notifications"].extend([bell_row(id=BELL), bell_row(id=BELL2)])
    _put(api, _router_row())
    _put(api, _record_row())
    assert _call(api, "POST", "/assistant/inbox/dismiss", {"ids": [INBOX, INBOX2]}).json() == {"updated": 2}
    rows = {r["id"]: r for r in api.db.tables["assistant_inbox"]}
    assert rows[INBOX]["dismissed_reason"] == "user" and rows[INBOX2]["dismissed_reason"] == "user"
    bells = {n["id"]: n for n in api.db.tables["notifications"]}
    assert bells[BELL]["read_at"] is not None and bells[BELL2]["read_at"] is None
    assert _call(api, "GET", "/assistant/inbox?segment=all").json()["items"] == []


# -- Feedback and snooze ------------------------------------------------------


def test_feedback_useful_returns_the_item(api):
    _put(api, _router_row())
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/feedback", {"value": "useful"})
    assert res.status_code == 200
    body = res.json()
    assert body["auto_downgraded"] is False and body["item"]["feedback"] == "useful" and body["item"]["id"] == INBOX


def test_feedback_errors(api):
    _put(api, _router_row())
    _put(api, _direct_row())
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/feedback", {"value": "love"})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_feedback"
    res = _call(api, "POST", f"/assistant/inbox/{UNKNOWN}/feedback", {"value": "useful"})
    assert res.status_code == 404 and res.json()["detail"]["code"] == "not_found"
    assert _call(api, "POST", "/assistant/inbox/not-a-uuid/feedback", {"value": "useful"}).status_code == 404
    for value in ("mute", "mute_project"):
        res = _call(api, "POST", f"/assistant/inbox/{INBOX3}/feedback", {"value": value})
        assert res.status_code == 409 and res.json()["detail"]["code"] == "not_mutable", value
    # A router row without a project cannot mute a project.
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/feedback", {"value": "mute_project"})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "not_mutable"
    assert api.db.tables["assistant_project_mutes"] == []


def test_mute_kind_writes_the_user_row_and_clears_waiting_rows(api):
    _put(api, _router_row())
    _put(api, _router_row(id=INBOX4, dedupe_key="p7:w", delivered_at=None, notification_id=None))
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/feedback", {"value": "mute"})
    assert res.status_code == 200 and res.json()["item"]["feedback"] == "mute"
    (sub,) = api.db.tables["assistant_subscriptions"]
    assert (sub["user_id"], sub["event_type"], sub["enabled"], sub["source"]) == (USER, "todo.due", False, "user")
    waiting = next(r for r in api.db.tables["assistant_inbox"] if r["id"] == INBOX4)
    assert waiting["dismissed_reason"] == "muted"
    kind = next(k for k in _call(api, "GET", "/assistant/alerts").json()["kinds"] if k["event_type"] == "todo.due")
    assert kind["effective_mode"] == "off" and kind["override"] == {"mode": "off", "source": "user"}


def test_mute_project_writes_the_project_mute(api):
    _put(api, _router_row(project_id=PROJECT))
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/feedback", {"value": "mute_project"})
    assert res.status_code == 200
    assert [(m["user_id"], m["project_id"]) for m in api.db.tables["assistant_project_mutes"]] == [(USER, PROJECT)]
    mutes = _call(api, "GET", "/assistant/alerts").json()["project_mutes"]
    assert mutes == [{"project_id": PROJECT, "number": "26.10.7301"}]
    assert _call(api, "POST", f"/assistant/alerts/mutes/{PROJECT}/remove").json() == {"removed": True}
    assert _call(api, "POST", f"/assistant/alerts/mutes/{PROJECT}/remove").json() == {"removed": False}
    assert _call(api, "POST", "/assistant/alerts/mutes/not-a-uuid/remove").status_code == 404
    assert api.db.tables["assistant_project_mutes"] == []


def test_the_third_not_useful_mark_downgrades_the_kind_to_inbox_only(api):
    for n, rid in enumerate((INBOX, INBOX2, INBOX3)):
        _put(api, _router_row(id=rid, dedupe_key=f"p7:t{n}", notification_id=None))
    results = [_call(api, "POST", f"/assistant/inbox/{rid}/feedback", {"value": "not_useful"}).json()
               for rid in (INBOX, INBOX2, INBOX3)]
    assert [r["auto_downgraded"] for r in results] == [False, False, True]
    kind = next(k for k in _call(api, "GET", "/assistant/alerts").json()["kinds"] if k["event_type"] == "todo.due")
    assert kind["override"] == {"mode": "inbox", "source": "feedback_auto"} and kind["effective_mode"] == "inbox"


def test_the_next_digest_carries_the_downgrade_line_once(api):
    """S7-1 (9.1): after the third Not useful the next digest (in the Inbox,
    as in its email) carries the fixed line; the digest before it and the
    digest after it do not."""
    for n, rid in enumerate((INBOX, INBOX2, INBOX3)):
        _put(api, _router_row(id=rid, dedupe_key=f"p7:t{n}", notification_id=None))
        assert _call(api, "POST", f"/assistant/inbox/{rid}/feedback", {"value": "not_useful"}).status_code == 200
    am, pm, mon = DIGEST, "d6d6d6d6-d6d6-4d6d-8d6d-d6d6d6d6d6d6", "d7d7d7d7-d7d7-4d7d-8d7d-d7d7d7d7d7d7"
    api.db.tables["assistant_digests"] += [
        digest_row(id=am, item_count=1, notification_id=BELL, created_at=P7_T0.isoformat()),
        digest_row(id=pm, slot="pm", item_count=1, notification_id=BELL2,
                   created_at=(P7_T0 + timedelta(hours=6)).isoformat()),
        digest_row(id=mon, digest_date="2026-11-02", item_count=1, notification_id=BELL,
                   created_at=(P7_T0 + timedelta(days=3, hours=1)).isoformat()),
    ]
    line = proactive.auto_downgrade_line("todo.due")
    notices = {d: _call(api, "GET", f"/assistant/inbox/digests/{d}").json()["digest"]["notices"] for d in (am, pm, mon)}
    assert notices == {am: [], pm: [line], mon: []}
    _put(api, _router_row(id=INBOX4, dedupe_key="p7:pm", delivery="digest", digest_id=pm, severity="normal"))
    body = _call(api, "GET", "/assistant/inbox?segment=digests").json()
    assert [d["notices"] for d in body["digests"]] == [[line]]
    # The summary's last_digest is only the badge: it reads no notices.
    assert _call(api, "GET", "/assistant/inbox/summary").json()["last_digest"]["notices"] == []


def test_a_snoozed_todo_in_a_later_digest_reads_overdue_in_the_inbox(api):
    """S9-3: the Inbox shows a due line for the Pacific day it was delivered
    on, the same text as the digest email."""
    mon_7am = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    api.clock["now"] = mon_7am + timedelta(hours=1)
    api.db.tables["assistant_digests"].append(digest_row(digest_date="2026-11-02", item_count=1,
                                                         notification_id=BELL, created_at=mon_7am.isoformat()))
    _put(api, _router_row(delivery="digest", digest_id=DIGEST, severity="normal", notification_id=None,
                          title="A to-do is due today", why="Due Sat Oct 31.", precedence="snoozed", snooze_count=1,
                          payload={"todo_id": "t1", "due": "2026-10-31", "overdue": False},
                          created_at=datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc).isoformat(),
                          delivered_at=mon_7am.isoformat()))
    (item,) = _call(api, "GET", f"/assistant/inbox/digests/{DIGEST}").json()["items"]
    assert (item["title"], item["why"]) == ("A to-do is overdue", "Overdue since Sat Oct 31.")
    (listed,) = _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]
    assert (listed["title"], listed["why"]) == ("A to-do is overdue", "Overdue since Sat Oct 31.")
    # The stored row is never rewritten.
    assert api.db.tables["assistant_inbox"][0]["title"] == "A to-do is due today"


def test_snooze_returns_the_row_in_the_next_slot_and_stops_at_three(api):
    _put(api, _router_row())
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/snooze")
    assert res.status_code == 200, res.text
    body = res.json()
    # Friday 9:00 AM Pacific: the next slot is Friday 1:00 PM Pacific (20:00 UTC).
    assert body["next_slot"] == "2026-10-30T20:00:00+00:00"
    assert body["item"]["delivered_at"] is None and body["item"]["delivery"] == "digest" and body["item"]["read"] is False
    assert body["item"]["precedence"] == "snoozed"
    row = api.db.tables["assistant_inbox"][0]
    assert row["deliver_after"].startswith("2026-10-30T20:00:00") and row["snooze_count"] == 1
    # Not delivered any more, so it cannot be snoozed again until the digest delivers it.
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/snooze")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "not_mutable"
    row.update({"delivered_at": P7_T0.isoformat(), "snooze_count": 3})
    res = _call(api, "POST", f"/assistant/inbox/{INBOX}/snooze")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "snooze_limit"


def test_snooze_skips_weekends_and_holidays(api):
    api.clock["now"] = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc)      # Friday 3:00 PM Pacific
    api.db.tables["assistant_settings"][0]["holidays"] = ["2026-11-02"]
    _put(api, _router_row())
    body = _call(api, "POST", f"/assistant/inbox/{INBOX}/snooze").json()
    # Monday Nov 2 is a holiday: Tuesday 7:00 AM Pacific (PST, 15:00 UTC).
    assert body["next_slot"] == "2026-11-03T15:00:00+00:00"


def test_snooze_follows_the_configured_digest_times(api):
    api.settings.assistant_proactive_digest_times = "08:00,14:00"
    _put(api, _router_row())
    body = _call(api, "POST", f"/assistant/inbox/{INBOX}/snooze").json()
    assert body["next_slot"] == "2026-10-30T21:00:00+00:00"
    api.settings.assistant_proactive_digest_times = "07:00,13:00"


def test_snooze_refuses_record_and_direct_rows(api):
    _put(api, _record_row())
    _put(api, _direct_row())
    for rid in (INBOX2, INBOX3):
        res = _call(api, "POST", f"/assistant/inbox/{rid}/snooze")
        assert res.status_code == 409 and res.json()["detail"]["code"] == "not_mutable", rid
    assert _call(api, "POST", f"/assistant/inbox/{UNKNOWN}/snooze").status_code == 404


# -- Alerts: the person's own modes -------------------------------------------


def test_alerts_view_shape_and_channels(api):
    body = _call(api, "GET", "/assistant/alerts").json()
    assert body["quiet_hours"] == {"start": "18:00", "end": "06:00", "tz": "America/Los_Angeles"}
    assert body["digest_times"] == ["07:00", "13:00"]
    assert body["channels"] == {
        "teams": {"available": True, "enabled": True, "connected": True,
                  "last_sent_at": "2026-10-30T15:00:00+00:00", "last_error_at": None},
        "email": {"available": True, "enabled": True}}
    kinds = {k["event_type"]: k for k in body["kinds"]}
    assert kinds["bid.due_not_ready"] == {
        "event_type": "bid.due_not_ready", "group": "Bidding", "label": "Bid due and your part is missing",
        "description": "A bid is due soon and a part your role owns is still missing.",
        "kind": "router", "modes": ["alert", "digest", "inbox", "off"],
        "role_default": "digest", "override": None, "effective_mode": "digest"}
    assert kinds["assistant.approval_expiring"]["role_default"] == "alert"
    assert kinds["bell.due"]["role_default"] == "inbox" and kinds["bell.due"]["group"] == "Bidding"
    assert kinds["bell.due"]["kind"] == "record" and kinds["bell.due"]["modes"] == ["inbox", "off"]
    assert "bell.late_quote" not in kinds         # Executive is outside the late quote audience
    assert list(kinds)[:3] == ["bid.due_not_ready", "todo.due", "assistant.approval_expiring"]
    assert body["project_mutes"] == []


def test_channels_need_the_external_switch_and_never_leak_error_text(api, monkeypatch):
    api.settings.assistant_proactive_external_enabled = False
    teams = _call(api, "GET", "/assistant/alerts").json()["channels"]
    assert teams["teams"]["available"] is False and teams["email"]["available"] is False
    assert teams["teams"]["enabled"] is True
    api.settings.assistant_proactive_external_enabled = True
    api.channels["teams"]["last_error"] = "secret exception text"
    api.channels["teams"]["address"] = "someone@g3.test"
    flat = str(_call(api, "GET", "/assistant/alerts").json()["channels"])
    assert "secret" not in flat and "someone@" not in flat

    def boom(user_id):
        raise RuntimeError("graph is down")

    from app.services import assistant_delivery

    monkeypatch.setattr(assistant_delivery, "channels", boom)
    res = _call(api, "GET", "/assistant/alerts")
    assert res.status_code == 200
    assert res.json()["channels"]["teams"]["available"] is False


def test_kinds_follow_the_callers_role_and_sub_apps(api, monkeypatch):
    api.state.role = Role.ACCOUNTANT
    kinds = {k["event_type"] for k in _call(api, "GET", "/assistant/alerts").json()["kinds"]}
    assert "bid.due_not_ready" not in kinds and "todo.due" in kinds and "assistant.approval_expiring" in kinds
    api.state.role = Role.EXECUTIVE
    monkeypatch.setattr(api.settings, "bidding_enabled", False)
    kinds = {k["event_type"] for k in _call(api, "GET", "/assistant/alerts").json()["kinds"]}
    assert "bid.due_not_ready" not in kinds and "bell.due" not in kinds and "todo.due" in kinds
    assert _call(api, "PUT", "/assistant/alerts/bid.due_not_ready", {"mode": "alert"}).status_code == 404


def test_a_person_sets_and_resets_a_mode(api):
    res = _call(api, "PUT", "/assistant/alerts/todo.due", {"mode": "alert"})
    assert res.status_code == 200
    assert res.json() == {
        "event_type": "todo.due", "group": "Personal", "label": "To-do due",
        "description": "A to-do you own is due today or overdue.", "kind": "router",
        "modes": ["alert", "digest", "inbox", "off"], "role_default": "digest",
        "override": {"mode": "alert", "source": "user"}, "effective_mode": "alert"}
    (sub,) = api.db.tables["assistant_subscriptions"]
    assert (sub["enabled"], sub["severity"], sub["role"], sub["user_id"]) == (True, "high", None, USER)
    off = _call(api, "PUT", "/assistant/alerts/todo.due", {"mode": "off"}).json()
    assert off["effective_mode"] == "off" and off["override"]["mode"] == "off"
    res = _call(api, "POST", "/assistant/alerts/todo.due/reset")
    assert res.status_code == 200 and res.json()["override"] is None and res.json()["effective_mode"] == "digest"
    assert api.db.tables["assistant_subscriptions"] == []
    assert api.db.tables["audit_log"] == []        # a person's own change writes no admin audit row


def test_a_user_cannot_pick_urgent_or_an_unknown_mode(api):
    for mode in ("urgent", "loud", ""):
        res = _call(api, "PUT", "/assistant/alerts/todo.due", {"mode": mode})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_mode", mode
    res = _call(api, "PUT", "/assistant/alerts/assistant.approval_expiring", {"mode": "urgent"})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_mode"
    assert api.db.tables["assistant_subscriptions"] == []


def test_unknown_kinds_are_404(api):
    for path in ("/assistant/alerts/nope.kind", "/assistant/alerts/bell.not_a_kind"):
        for method, suffix, body in (("PUT", "", {"mode": "alert"}), ("POST", "/reset", None)):
            res = _call(api, method, path + suffix, body)
            assert res.status_code == 404 and res.json()["detail"]["code"] == "unknown_kind", (method, path)


def test_record_kinds_never_push_so_alert_is_stored_as_inbox(api):
    res = _call(api, "PUT", "/assistant/alerts/bell.due", {"mode": "alert"})
    assert res.status_code == 200
    assert res.json()["effective_mode"] == "inbox" and res.json()["override"]["mode"] == "inbox"
    assert api.db.tables["assistant_subscriptions"][0]["severity"] == "low"
    assert _call(api, "PUT", "/assistant/alerts/bell.due", {"mode": "off"}).json()["effective_mode"] == "off"


def test_an_admin_switch_off_for_a_role_is_final_for_its_people(api):
    api.db.tables["assistant_subscriptions"].append(subscription_row(
        role="executive", user_id=None, event_type="todo.due", enabled=False, severity="normal", source="admin"))
    kind = _call(api, "PUT", "/assistant/alerts/todo.due", {"mode": "alert"}).json()
    assert kind["role_default"] == "off" and kind["effective_mode"] == "off"


def test_an_admin_urgent_role_default_shows_as_the_role_default(api):
    api.db.tables["assistant_subscriptions"].append(subscription_row(
        role="executive", user_id=None, event_type="assistant.approval_expiring", enabled=True, severity="urgent",
        source="admin"))
    kinds = {k["event_type"]: k for k in _call(api, "GET", "/assistant/alerts").json()["kinds"]}
    assert kinds["assistant.approval_expiring"]["role_default"] == "urgent"
    assert kinds["assistant.approval_expiring"]["effective_mode"] == "urgent"
    # The person's own row can never be urgent; it clamps to alert.
    out = _call(api, "PUT", "/assistant/alerts/assistant.approval_expiring", {"mode": "alert"}).json()
    assert out["effective_mode"] == "alert" and out["role_default"] == "urgent"


# -- Admin: role defaults, company switch, report -----------------------------


def test_the_defaults_grid_lists_the_three_router_kinds_per_role(api):
    grid = _call(api, "GET", "/assistant/alerts/defaults").json()
    assert grid["roles"] == [r.value for r in Role if r in INTERNAL_ROLES]
    assert [k["event_type"] for k in grid["kinds"]] == ["bid.due_not_ready", "todo.due", "assistant.approval_expiring"]
    bid = grid["kinds"][0]
    assert bid["cells"]["executive"] == {"mode": "digest", "default_mode": "digest"}
    assert bid["cells"]["it_admin"] == {"mode": "inbox", "default_mode": "inbox"}
    assert "accountant" not in bid["cells"]
    assert grid["kinds"][2]["cells"]["accountant"]["mode"] == "alert"
    one = _call(api, "GET", "/assistant/alerts/defaults?role=estimating_admin").json()
    assert one["roles"] == ["estimating_admin"] and all(set(k["cells"]) <= {"estimating_admin"} for k in one["kinds"])
    res = _call(api, "GET", "/assistant/alerts/defaults?role=pe")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_role"
    assert _call(api, "GET", "/assistant/alerts/defaults?role=estimator").status_code == 422


def test_an_admin_can_make_approval_expiring_urgent_for_a_role(api):
    res = _call(api, "PUT", "/assistant/alerts/defaults/estimating_engineer_materials/assistant.approval_expiring",
                {"mode": "urgent"})
    assert res.status_code == 200, res.text
    cell = next(k for k in res.json()["kinds"] if k["event_type"] == "assistant.approval_expiring")["cells"]
    assert res.json()["roles"] == ["estimating_engineer_materials"]
    assert cell["estimating_engineer_materials"] == {"mode": "urgent", "default_mode": "alert"}
    (row,) = api.db.tables["assistant_subscriptions"]
    assert (row["role"], row["user_id"], row["event_type"], row["enabled"], row["severity"], row["source"], row["updated_by"]) == (
        "estimating_engineer_materials", None, "assistant.approval_expiring", True, "urgent", "admin", USER)
    (audit,) = _audit_rows(api, "assistant.alert_default.update")
    assert audit["actor_id"] == USER and audit["payload"] == {
        "role": "estimating_engineer_materials", "event_type": "assistant.approval_expiring", "mode": "urgent"}
    # The person's own PUT of urgent is still refused.
    assert _call(api, "PUT", "/assistant/alerts/assistant.approval_expiring", {"mode": "urgent"}).status_code == 422


@pytest.mark.parametrize("kind", ["bid.due_not_ready", "todo.due", "assistant.approval_expiring"])
def test_urgent_is_accepted_for_all_three_router_kinds(api, kind):
    assert _call(api, "PUT", f"/assistant/alerts/defaults/executive/{kind}", {"mode": "urgent"}).status_code == 200


@pytest.mark.parametrize("mode,enabled,severity", [
    ("alert", True, "high"), ("digest", True, "normal"), ("inbox", True, "low"), ("off", False, "high")])
def test_each_default_mode_maps_to_enabled_and_severity(api, mode, enabled, severity):
    res = _call(api, "PUT", "/assistant/alerts/defaults/executive/assistant.approval_expiring", {"mode": mode})
    assert res.status_code == 200
    (row,) = api.db.tables["assistant_subscriptions"]
    assert (row["enabled"], row["severity"]) == (enabled, severity)
    cell = next(k for k in res.json()["kinds"] if k["event_type"] == "assistant.approval_expiring")["cells"]["executive"]
    assert cell["mode"] == mode


def test_default_write_errors_write_nothing_and_no_audit(api):
    for path, body, code, status_code in (
        ("/assistant/alerts/defaults/executive/todo.due", {"mode": "loud"}, "invalid_mode", 422),
        ("/assistant/alerts/defaults/executive/bell.late_quote", {"mode": "inbox"}, "unknown_kind", 404),
        ("/assistant/alerts/defaults/executive/nope.kind", {"mode": "inbox"}, "unknown_kind", 404),
        ("/assistant/alerts/defaults/estimator/todo.due", {"mode": "inbox"}, "invalid_role", 422),
        ("/assistant/alerts/defaults/nobody/todo.due", {"mode": "inbox"}, "invalid_role", 422),
    ):
        res = _call(api, "PUT", path, body)
        assert res.status_code == status_code and res.json()["detail"]["code"] == code, path
    assert api.db.tables["assistant_subscriptions"] == [] and api.db.tables["audit_log"] == []


def test_a_default_reset_audits_first_then_removes_the_row(api, monkeypatch):
    api.db.tables["assistant_subscriptions"].append(subscription_row(
        role="executive", user_id=None, event_type="todo.due", enabled=True, severity="high", source="admin"))
    order: list[str] = []
    from app.services import notifications

    real_audit, real_reset = notifications.audit, repo.role_subscription_reset

    def audit_spy(*args, **kwargs):
        order.append("audit")
        assert len(api.db.tables["assistant_subscriptions"]) == 1       # the row still exists
        return real_audit(*args, **kwargs)

    def reset_spy(*args, **kwargs):
        order.append("reset")
        return real_reset(*args, **kwargs)

    monkeypatch.setattr(notifications, "audit", audit_spy)
    monkeypatch.setattr(repo, "role_subscription_reset", reset_spy)
    res = _call(api, "POST", "/assistant/alerts/defaults/executive/todo.due/reset")
    assert res.status_code == 200 and order == ["audit", "reset"]
    assert api.db.tables["assistant_subscriptions"] == []
    (audit,) = _audit_rows(api, "assistant.alert_default.reset")
    assert audit["payload"] == {"role": "executive", "event_type": "todo.due"}
    cell = next(k for k in res.json()["kinds"] if k["event_type"] == "todo.due")["cells"]["executive"]
    assert cell == {"mode": "digest", "default_mode": "digest"}


def test_a_failing_audit_means_no_write(api, monkeypatch):
    from app.services import notifications

    def boom(*args, **kwargs):
        raise RuntimeError("audit table is down")

    monkeypatch.setattr(notifications, "audit", boom)
    client = TestClient(api.client.app, raise_server_exceptions=False)
    api.db.tables["assistant_settings"][0]["proactive_enabled"] = False
    assert client.put("/assistant/alerts/defaults/executive/todo.due", json={"mode": "inbox"}).status_code == 500
    assert client.post("/assistant/alerts/defaults/executive/todo.due/reset").status_code == 500
    assert client.put("/assistant/alerts/company", json={"enabled": True}).status_code == 500
    assert api.db.tables["assistant_subscriptions"] == []
    assert api.db.tables["assistant_settings"][0]["proactive_enabled"] is False


def test_the_company_switch_audits_and_reseeds_on_an_off_to_on_flip(api):
    api.db.tables["assistant_settings"][0]["proactive_enabled"] = False
    old = (NOW - timedelta(hours=6)).isoformat()
    for row in api.db.tables["assistant_router_cursors"]:
        row["cursor_at"] = old
    res = _call(api, "PUT", "/assistant/alerts/company", {"enabled": True})
    assert res.status_code == 200 and res.json() == {"enabled": True}
    assert api.db.tables["assistant_settings"][0]["proactive_enabled"] is True
    (audit,) = _audit_rows(api, "assistant.proactive_switch")
    assert audit["actor_id"] == USER and audit["payload"] == {"enabled": True}
    for row in api.db.tables["assistant_router_cursors"]:
        assert datetime.fromisoformat(row["cursor_at"]) == NOW - timedelta(minutes=2)
        assert row["cursor_id"] is None
        assert row["updated_by"] == "admin_switch"
    # on to on does not reseed; on to off audits and does not reseed either.
    for row in api.db.tables["assistant_router_cursors"]:
        row["cursor_at"] = old
    assert _call(api, "PUT", "/assistant/alerts/company", {"enabled": True}).json() == {"enabled": True}
    assert _call(api, "PUT", "/assistant/alerts/company", {"enabled": False}).json() == {"enabled": False}
    assert _call(api, "GET", "/assistant/alerts/company").json() == {"enabled": False}
    assert all(row["cursor_at"] == old for row in api.db.tables["assistant_router_cursors"])
    assert [a["payload"] for a in _audit_rows(api, "assistant.proactive_switch")] == [
        {"enabled": True}, {"enabled": True}, {"enabled": False}]
    assert _call(api, "PUT", "/assistant/alerts/company", {"enabled": "maybe"}).status_code == 422


def test_a_failed_reseed_does_not_undo_the_switch(api, monkeypatch):
    api.db.tables["assistant_settings"][0]["proactive_enabled"] = False

    def boom(*args, **kwargs):
        raise RuntimeError("cursor table is down")

    monkeypatch.setattr(repo, "reseed_router_cursors", boom)
    assert _call(api, "PUT", "/assistant/alerts/company", {"enabled": True}).json() == {"enabled": True}
    assert api.db.tables["assistant_settings"][0]["proactive_enabled"] is True


def test_the_flip_reseeds_through_the_proactive_service_with_a_fixed_marker(api, monkeypatch):
    from app.services import assistant_proactive

    api.db.tables["assistant_settings"][0]["proactive_enabled"] = False
    seen = []
    monkeypatch.setattr(assistant_proactive, "reseed_tails", lambda now, **kw: seen.append((now, kw)) or {})
    assert _call(api, "PUT", "/assistant/alerts/company", {"enabled": True}).status_code == 200
    assert seen == [(NOW, {"worker": "admin_switch", "reason": "switch"})]


def test_the_feedback_report_is_counts_only_and_carries_the_health_names(api):
    body = _call(api, "GET", "/assistant/inbox/feedback-report?days=7").json()
    assert body["kinds"] == [] and body["floor"] == 3
    assert body["since"].startswith((NOW - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"))
    health = body["health"]
    for key in ("events_pending", "events_failed_7d", "events_lease_exhausted_7d", "slot_failures_7d",
                "workers", "cursors", "sends_7d"):
        assert key in health, key
    assert health["events_lease_exhausted_7d"] == 0
    assert _call(api, "GET", "/assistant/inbox/feedback-report?days=0").status_code == 422
    assert _call(api, "GET", "/assistant/inbox/feedback-report?days=91").status_code == 422
    assert _call(api, "GET", "/assistant/inbox/feedback-report").status_code == 200


def test_the_feedback_report_suppresses_small_cells_and_names_no_one(api):
    api.db.tables["profiles"].extend([profile(id=P7_OTHER, role="estimating_admin")])
    for n in range(3):
        uid = USER if n == 0 else P7_OTHER
        _put(api, _router_row(id=f"e{n}e{n}e{n}e{n}-e{n}e{n}-4e{n}e-8e{n}e-e{n}e{n}e{n}e{n}e{n}e{n}",
                              user_id=uid, dedupe_key=f"p7:f{n}", feedback="useful", notification_id=None))
    body = _call(api, "GET", "/assistant/inbox/feedback-report").json()
    assert body["kinds"] == [{"event_type": "todo.due", "suppressed": True, "users": None}]
    assert USER not in str(body) and P7_OTHER not in str(body)


# -- Source hygiene -----------------------------------------------------------


def test_the_router_file_keeps_to_the_phase_7_rules():
    text = (ROOT / "app/routers/assistant_inbox.py").read_text()
    assert EM_DASH not in text
    for banned in ("app.assistant.loop", "app.assistant.llm_messages", "app.services.llm", "complete_messages",
                   "notifications.deliver", "from app.services.notifications import", "notify_role", "notify_roles",
                   "notify_user", "notification_email.queue", "teams_alerts", "graph_email", "queue_report",
                   "get_supabase", "supabase_client"):
        assert banned not in text, banned
    assert not re.search(r"(?<![\w.])deliver\(", text)
    # Assistant tables only through repo.py.
    assert not re.search(r'table\(\s*["\']assistant_', text)


def test_the_one_system_read_is_the_feedback_report():
    text = (ROOT / "app/routers/assistant_inbox.py").read_text()
    calls = [(m.start(), m.group(0)) for m in re.finditer(r"repo\.\w+\(\s*None\b", text)]
    names = sorted(g.split("(")[0] for _, g in calls)
    # Phase 8 (plan 8.1): the admin triage block reads the counts RPC once, as
    # a system read (tests/test_assistant_repo_isolation.py pins the caller).
    assert names == ["repo.inbox_feedback_report", "repo.triage_counts"]
    triage_view = text[text.index("def _triage_company_view"):text.index("@router.get(\"/alerts/company/triage\")")]
    assert triage_view.count("repo.triage_counts(None") == 1
    report = text[text.index("def feedback_report"):]
    assert "repo.inbox_feedback_report(None" in report


def test_main_never_names_the_assistant_package_and_registers_the_router():
    text = (ROOT / "app/main.py").read_text()
    assert "app.assistant" not in text
    assert "assistant_inbox.router" in text and "assistant_proactive_task" in text
    assert EM_DASH not in (ROOT / "app/assistant/firewall.py").read_text()


def test_the_router_is_registered_in_the_app():
    from app.main import app

    paths = {(m, r.path) for r in app.routes if hasattr(r, "methods") for m in r.methods}
    assert {("GET", "/assistant/inbox"), ("PUT", "/assistant/alerts/company")} <= paths
