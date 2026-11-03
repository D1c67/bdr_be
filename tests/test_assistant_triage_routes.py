"""Phase 8 routes (docs/ASSISTANT_PHASE8_PLAN.md 8.1, 6.1; WI-9):
triage on the Inbox items, the Alerts card's triage line, the person's digest
email switch, the company triage switch, the escalation kind's on or off only
Alerts handling and the assistant-side `GET /assistant/tools/inbox`.

Invariants pinned here:

  routes     the digest email path has two segments and is not captured by
             PUT /alerts/{event_type}; there is no per-person triage route
             (owner questions 14 and 15); both company triage routes need an
             Executive or IT Admin and answer the flag-off 404;
  switch     turning triage on writes triage_enabled_at and clears the fuse
             latch in one update, the GET reports fuse_tripped, an audit row
             leads every switch write and no write happens when the audit
             insert fails (company switch and digest email);
  kind       PUT /alerts/assistant.triage_escalation accepts only off
             (invalid_mode for inbox, digest, alert and on), POST .../reset
             turns it back on with no own row left (P8R-4);
  items      a record row carries its triage object with server rendered
             labels and the "Work on this" text, an escalated row links the
             escalation row; mute is offered for the new kind;
  tool       the model route refuses a human (bare 404), answers inbox_off,
             hides dismissed rows, filters by kind and project, never writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, firewall, proactive
from app.core import supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import assistant_inbox as inbox_router
from app.routers import assistant_inbox_tools as tools_router

from tests.assistant_fakes import (
    BELL, BELL2, DIGEST, INBOX, INBOX2, P7_T0, PROJECT, RUN, USER,
    FakeDB, bell_row, digest_row, inbox_row, profile, settings_row, triage_item_row,
)

KIND = proactive.TRIAGE_ESCALATION
NOW = P7_T0 + timedelta(hours=2)          # Friday 9:00 AM Pacific, a workday
ROOT = Path(__file__).resolve().parents[1]
INBOX3 = "b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3"
INBOX4 = "b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4"
PROJECT_B = "99999999-9999-4999-8999-999999999999"
PROJECT_C = "88888888-8888-4888-8888-888888888888"
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()],
        assistant_settings=[settings_row(proactive_enabled=True, triage_enabled=True,
                                         triage_enabled_at=(NOW - timedelta(days=1)).isoformat(),
                                         triage_fuse_tripped_at=None)],
        assistant_profiles=[], assistant_inbox=[], assistant_subscriptions=[], assistant_digests=[],
        assistant_project_mutes=[], assistant_triage_items=[], assistant_triage_runs=[],
        notifications=[], audit_log=[],
        projects=[{"id": PROJECT, "number": "26.10.7301", "name": "Alpha Clinic"},
                  {"id": PROJECT_B, "number": "26.10.7302", "name": "Beta Mall"}],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    settings = get_settings()
    monkeypatch.setattr(settings, "bidding_enabled", True)
    monkeypatch.setattr(settings, "assistant_proactive_enabled", True)
    monkeypatch.setattr(settings, "assistant_proactive_user_allowlist", "")
    monkeypatch.setattr(settings, "assistant_proactive_external_enabled", True)
    monkeypatch.setattr(settings, "assistant_triage_enabled", True)
    monkeypatch.setattr(settings, "assistant_triage_user_allowlist", "")
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(inbox_router, "_now", lambda: NOW)
    from app.services import assistant_delivery, notifications

    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    channel_state = {"teams": {"available": True, "enabled": True, "connected": True,
                               "last_sent_at": None, "last_error_at": None},
                     "email": {"available": True}}
    monkeypatch.setattr(assistant_delivery, "channels", lambda user_id: channel_state)
    state = SimpleNamespace(role=Role.EXECUTIVE, via=None, email="tom@g3.test", uid=USER)

    def _user():
        return CurrentUser(id=state.uid, email=state.email, role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(inbox_router.router)
    app.include_router(tools_router.router)
    app.dependency_overrides[get_current_user] = _user
    return SimpleNamespace(client=TestClient(app), db=db, state=state, settings=settings, channels=channel_state)


def _call(api, method, path, body=None):
    return api.client.request(method, path, json=body) if body is not None else api.client.request(method, path)


def _put(api, row: dict) -> dict:
    api.db.tables.setdefault("assistant_inbox", []).append(row)
    return row


def _record_row(**over) -> dict:
    base = {"id": INBOX2, "dedupe_key": "p7:bell:b1", "delivery": "record", "severity": "low",
            "event_type": "bell.late_quote", "title": "A late quote", "event_id": None,
            "delivered_at": P7_T0.isoformat(), "deliver_after": None, "source_notification_id": BELL2,
            "notification_id": None, "project_id": PROJECT}
    base.update(over)
    return inbox_row(**base)


def _router_row(**over) -> dict:
    base = {"delivered_at": P7_T0.isoformat(), "severity": "high", "event_type": "todo.due",
            "delivery": "immediate", "notification_id": BELL}
    base.update(over)
    return inbox_row(**base)


def _audit_rows(api, action: str) -> list[dict]:
    return [r for r in api.db.tables["audit_log"] if r["action"] == action]


def _settings(api) -> dict:
    return api.db.tables["assistant_settings"][0]


# -- Routes and gates ---------------------------------------------------------


def test_the_digest_email_path_has_two_segments_and_is_not_captured_by_the_kind_route(api):
    res = _call(api, "PUT", "/assistant/alerts/me/digest-email", {"enabled": False})
    assert res.status_code == 200 and res.json() == {"enabled": False}
    # The one-segment spelling would be a kind name and answers unknown_kind.
    res = _call(api, "PUT", "/assistant/alerts/digest-email", {"mode": "off"})
    assert res.status_code == 404 and res.json()["detail"]["code"] == "unknown_kind"
    routes = {(m, r.path) for r in inbox_router.router.routes for m in r.methods}
    assert ("PUT", "/assistant/alerts/me/digest-email") in routes


def test_there_is_no_per_person_triage_route(api):
    paths = {r.path for r in inbox_router.router.routes} | {r.path for r in tools_router.router.routes}
    assert [p for p in paths if "triage" in p] == ["/assistant/alerts/company/triage"] * 1
    for method in ("PUT", "POST", "GET", "DELETE"):
        res = _call(api, method, "/assistant/alerts/me/triage", {"enabled": True} if method != "GET" else None)
        assert res.status_code in (404, 405), method
    # The company triage path exists for GET and PUT only.
    assert _call(api, "POST", "/assistant/alerts/company/triage", {"enabled": True}).status_code == 405


def test_the_new_alerts_routes_are_in_the_firewall_deny_list():
    new = {("PUT", "/assistant/alerts/me/digest-email"), ("GET", "/assistant/alerts/company/triage"),
           ("PUT", "/assistant/alerts/company/triage")}
    assert new <= set(firewall._ASSISTANT_INBOX)
    assert new <= firewall.DENY_LIST
    # The tool route sits outside every denied prefix: the turn allowlist is its gate.
    assert not any(tools_router.ROUTE_PATH.startswith(p) for p in firewall.DENY_PATH_PREFIXES)
    assert ("GET", tools_router.ROUTE_PATH) not in firewall._ASSISTANT_INBOX


@pytest.mark.parametrize("role", [r for r in INTERNAL_ROLES if r not in (Role.EXECUTIVE, Role.IT_ADMIN)])
def test_both_company_triage_routes_need_an_executive_or_it_admin(api, role):
    api.state.role = role
    assert _call(api, "GET", "/assistant/alerts/company/triage").status_code == 403
    assert _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": False}).status_code == 403
    assert _settings(api)["triage_enabled"] is True


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.IT_ADMIN])
def test_executive_and_it_admin_reach_the_company_triage_routes(api, role):
    api.state.role = role
    assert _call(api, "GET", "/assistant/alerts/company/triage").status_code == 200
    assert _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": True}).status_code == 200


def test_the_external_estimator_and_the_turn_principal_are_refused(api):
    api.state.role = Role.ESTIMATOR
    assert _call(api, "GET", "/assistant/alerts/company/triage").status_code == 403
    api.state.role = Role.EXECUTIVE
    api.state.via = RUN
    for method, path, body in (("GET", "/assistant/alerts/company/triage", None),
                               ("PUT", "/assistant/alerts/company/triage", {"enabled": False}),
                               ("PUT", "/assistant/alerts/me/digest-email", {"enabled": False})):
        assert _call(api, method, path, body).status_code == 403, path
    assert _settings(api)["triage_enabled"] is True


def test_the_company_routes_answer_the_flag_off_404_for_every_role(api, monkeypatch):
    monkeypatch.setattr(api.settings, "assistant_proactive_enabled", False)
    assert _call(api, "GET", "/assistant/alerts/company/triage").status_code == 404
    assert _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": False}).status_code == 404
    # The digest email switch is a user route: the inbox gate.
    assert _call(api, "PUT", "/assistant/alerts/me/digest-email", {"enabled": False}).status_code == 404


def test_the_company_triage_switch_is_reachable_with_the_proactive_switch_off(api):
    _settings(api)["proactive_enabled"] = False
    assert _call(api, "GET", "/assistant/alerts/company/triage").status_code == 200
    assert _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": False}).status_code == 200


# -- The company switch -------------------------------------------------------


def test_the_company_triage_get_shape(api):
    api.db.tables["assistant_triage_items"].append(
        triage_item_row(status="decided", decision="escalate", severity_hint="urgent", reason="due_soon_unread",
                        created_at=NOW.isoformat(), decided_at=NOW.isoformat(), run_id=None))
    api.db.tables["assistant_triage_items"].append(
        triage_item_row(inbox_id=INBOX3, status="pending", cap_deferred_at=NOW.isoformat(), created_at=NOW.isoformat()))
    body = _call(api, "GET", "/assistant/alerts/company/triage").json()
    assert set(body) == {"enabled", "enabled_at", "fuse_tripped", "fuse_tripped_at", "breaker", "cap_paused",
                         "counts", "cap_deferred_pending", "spend"}
    assert body["enabled"] is True and body["fuse_tripped"] is False and body["fuse_tripped_at"] is None
    assert body["breaker"] == {"open": False, "next_probe_at": None}
    assert body["cap_paused"] == {"paused": False, "until": None}
    assert body["cap_deferred_pending"] == 1
    assert body["counts"]["decision"] == {"escalate": 1} and body["counts"]["severity_hint"] == {"urgent": 1}
    assert body["counts"]["status"] == {"decided": 1, "pending": 1}
    assert set(body["spend"]) == {"global_cap", "global_spent", "global_held", "browser_task_held",
                                  "triage_spent_today", "triage_held"}
    # Counts only: no user id leaves the block.
    assert USER not in str(body)


def test_the_get_reports_a_tripped_fuse(api):
    stamp = (NOW - timedelta(hours=1)).isoformat()
    _settings(api)["triage_fuse_tripped_at"] = stamp
    body = _call(api, "GET", "/assistant/alerts/company/triage").json()
    assert body["fuse_tripped"] is True and body["fuse_tripped_at"] == stamp


def test_the_process_health_block_reads_the_service_when_it_has_one(api, monkeypatch):
    from app.services import assistant_triage

    monkeypatch.setattr(assistant_triage, "health", lambda: {
        "breaker": {"open": True, "next_probe_at": "2026-10-30T16:00:00+00:00"},
        "cap_paused": {"paused": True, "until": "2026-10-30T16:15:00+00:00"}}, raising=False)
    body = _call(api, "GET", "/assistant/alerts/company/triage").json()
    assert body["breaker"] == {"open": True, "next_probe_at": "2026-10-30T16:00:00+00:00"}
    assert body["cap_paused"] == {"paused": True, "until": "2026-10-30T16:15:00+00:00"}
    monkeypatch.setattr(assistant_triage, "health", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                        raising=False)
    assert _call(api, "GET", "/assistant/alerts/company/triage").json()["breaker"]["open"] is False


def test_switching_on_writes_the_epoch_and_clears_the_latch_in_one_update(api):
    _settings(api).update(triage_enabled=False, triage_enabled_at=(NOW - timedelta(days=3)).isoformat(),
                          triage_fuse_tripped_at=(NOW - timedelta(hours=2)).isoformat())
    old_epoch = _settings(api)["triage_enabled_at"]
    before = len([c for c in api.db.calls if c == ("update", "assistant_settings")])
    rpcs = len(api.db.rpc_calls)
    res = _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": True})
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True and body["fuse_tripped"] is False and body["fuse_tripped_at"] is None
    row = _settings(api)
    assert row["triage_enabled"] is True and row["triage_fuse_tripped_at"] is None
    assert row["triage_enabled_at"] != old_epoch and body["enabled_at"] == row["triage_enabled_at"]
    # The enabling write is the one 0196 B6 statement (D-1): no table update from the app.
    assert len([c for c in api.db.calls if c == ("update", "assistant_settings")]) == before
    assert [n for n, _p in api.db.rpc_calls[rpcs:] if n == "assistant_triage_set_enabled"] == [
        "assistant_triage_set_enabled"]
    assert row["triage_enabled_at"] == api.db.now_iso()     # the database clock
    (audit,) = _audit_rows(api, "assistant.triage_switch")
    assert audit["actor_id"] == USER and audit["payload"] == {"enabled": True}


def test_switching_off_keeps_the_latch_and_the_epoch(api):
    stamp = (NOW - timedelta(hours=2)).isoformat()
    _settings(api)["triage_fuse_tripped_at"] = stamp
    epoch = _settings(api)["triage_enabled_at"]
    res = _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": False})
    assert res.status_code == 200 and res.json()["enabled"] is False
    assert _settings(api)["triage_enabled"] is False
    assert _settings(api)["triage_enabled_at"] == epoch and _settings(api)["triage_fuse_tripped_at"] == stamp
    (audit,) = _audit_rows(api, "assistant.triage_switch")
    assert audit["payload"] == {"enabled": False}


def test_turning_on_while_on_does_not_clear_a_tripped_fuse(api):
    stamp = (NOW - timedelta(hours=2)).isoformat()
    _settings(api)["triage_fuse_tripped_at"] = stamp
    assert _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": True}).status_code == 200
    assert _settings(api)["triage_fuse_tripped_at"] == stamp


def test_an_audit_row_leads_every_switch_write(api, monkeypatch):
    """One shared list records the audit call and every settings or profiles
    update from the start, so a write made before the audit would show."""
    from app.services import notifications

    events: list[str] = []
    real_audit = notifications.audit

    def audit_spy(*args, **kwargs):
        events.append("audit")
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(notifications, "audit", audit_spy)
    real_table = FakeDB.table

    class _Spy:
        def __init__(self, inner, name):
            self._inner, self._name = inner, name

        def __getattr__(self, attr):
            target = getattr(self._inner, attr)
            if attr in ("update", "upsert", "insert"):
                def wrapped(*a, **k):
                    events.append(f"{attr}:{self._name}")
                    return target(*a, **k)
                return wrapped
            return target

    def table_spy(self, name):
        inner = real_table(self, name)
        return _Spy(inner, name) if name in ("assistant_settings", "assistant_profiles") else inner

    monkeypatch.setattr(FakeDB, "table", table_spy)
    real_rpc = FakeDB.rpc

    def rpc_spy(self, name, params):
        if name == "assistant_triage_set_enabled":
            events.append("rpc:assistant_settings")     # the switch writes through 0196 B6
        return real_rpc(self, name, params)

    monkeypatch.setattr(FakeDB, "rpc", rpc_spy)
    assert _call(api, "PUT", "/assistant/alerts/company/triage", {"enabled": False}).status_code == 200
    writes = [i for i, e in enumerate(events) if e.endswith(":assistant_settings")]
    assert "audit" in events and writes
    assert events.index("audit") < min(writes)
    events.clear()
    assert _call(api, "PUT", "/assistant/alerts/me/digest-email", {"enabled": False}).status_code == 200
    writes = [i for i, e in enumerate(events) if e.endswith(":assistant_profiles")]
    assert "audit" in events and writes
    assert events.index("audit") < min(writes)
    (audit,) = _audit_rows(api, "assistant.digest_email")
    assert audit["actor_id"] == USER and audit["payload"] == {"enabled": False}


def test_a_failing_audit_means_no_write_on_both_puts(api, monkeypatch):
    from app.services import notifications

    def boom(*args, **kwargs):
        raise RuntimeError("audit table is down")

    monkeypatch.setattr(notifications, "audit", boom)
    client = TestClient(api.client.app, raise_server_exceptions=False)
    _settings(api)["triage_enabled"] = False
    assert client.put("/assistant/alerts/company/triage", json={"enabled": True}).status_code == 500
    assert _settings(api)["triage_enabled"] is False
    assert client.put("/assistant/alerts/me/digest-email", json={"enabled": False}).status_code == 500
    assert api.db.tables["assistant_profiles"] == []


# -- The digest email switch and the card -------------------------------------


def test_the_digest_email_switch_writes_the_profile_and_the_card_reads_it(api):
    card = _call(api, "GET", "/assistant/alerts").json()
    assert card["channels"]["email"] == {"available": True, "enabled": True}
    assert _call(api, "PUT", "/assistant/alerts/me/digest-email", {"enabled": False}).json() == {"enabled": False}
    (row,) = api.db.tables["assistant_profiles"]
    assert row["user_id"] == USER and row["digest_email_enabled"] is False
    assert _call(api, "GET", "/assistant/alerts").json()["channels"]["email"] == {"available": True, "enabled": False}
    assert _call(api, "PUT", "/assistant/alerts/me/digest-email", {"enabled": True}).json() == {"enabled": True}
    assert _call(api, "GET", "/assistant/alerts").json()["channels"]["email"]["enabled"] is True


def test_the_channel_email_enabled_comes_from_the_delivery_state_when_it_has_one(api):
    api.channels["email"] = {"available": True, "enabled": False}
    assert _call(api, "GET", "/assistant/alerts").json()["channels"]["email"] == {"available": True, "enabled": False}


def test_the_digest_email_body_must_be_a_boolean(api):
    assert _call(api, "PUT", "/assistant/alerts/me/digest-email", {"enabled": "maybe"}).status_code == 422
    assert _call(api, "PUT", "/assistant/alerts/me/digest-email", {}).status_code == 422


def test_the_alerts_card_triage_line(api):
    card = _call(api, "GET", "/assistant/alerts").json()
    assert card["triage"] == {"available": True, "on": True, "last_run_at": None,
                              "today": {"decided": 0, "escalations": 0}}
    api.db.tables["assistant_triage_runs"].append(
        {"id": "e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1", "user_id": USER, "status": "done",
         "claimed_at": NOW.isoformat(), "finished_at": NOW.isoformat(), "created_at": NOW.isoformat()})
    for n, decision in enumerate(("none", "suggest", "escalate")):
        api.db.tables["assistant_triage_items"].append(triage_item_row(
            inbox_id=f"c{n}c{n}c{n}c{n}-c{n}c{n}-4c{n}c-8c{n}c-c{n}c{n}c{n}c{n}c{n}c{n}c{n}c{n}c{n}c{n}c{n}c{n}",
            status="decided", decision=decision, decided_at=NOW.isoformat(), created_at=NOW.isoformat()))
    # Yesterday (Pacific) does not count today.
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        inbox_id=INBOX4, status="decided", decision="escalate", decided_at=(NOW - timedelta(days=1)).isoformat()))
    card = _call(api, "GET", "/assistant/alerts").json()["triage"]
    assert card["last_run_at"] == NOW.isoformat()
    assert card["today"] == {"decided": 3, "escalations": 1}


def test_the_card_triage_line_follows_the_env_flag_the_company_switch_and_the_allowlist(api):
    _settings(api)["triage_enabled"] = False
    card = _call(api, "GET", "/assistant/alerts").json()["triage"]
    assert card["available"] is True and card["on"] is False
    _settings(api)["triage_enabled"] = True
    api.settings.assistant_triage_user_allowlist = "someone-else@g3.test"
    assert _call(api, "GET", "/assistant/alerts").json()["triage"]["on"] is False
    api.settings.assistant_triage_user_allowlist = "TOM@g3.test"
    assert _call(api, "GET", "/assistant/alerts").json()["triage"]["on"] is True
    api.settings.assistant_triage_user_allowlist = ""
    api.settings.assistant_triage_enabled = False
    assert _call(api, "GET", "/assistant/alerts").json()["triage"] == {
        "available": False, "on": False, "last_run_at": None, "today": {"decided": 0, "escalations": 0}}


def test_features_assistant_triage_key(api):
    from app.main import features

    me = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    assert features(me)["assistant_triage"] is True
    _settings(api)["triage_enabled"] = False
    assert features(me)["assistant_triage"] is False
    _settings(api)["triage_enabled"] = True
    api.settings.assistant_triage_user_allowlist = "other@g3.test"
    assert features(me)["assistant_triage"] is False
    api.settings.assistant_triage_user_allowlist = USER
    assert features(me)["assistant_triage"] is True
    api.settings.assistant_triage_enabled = False
    assert features(me)["assistant_triage"] is False
    api.settings.assistant_triage_enabled = True
    _settings(api)["proactive_enabled"] = False            # the inbox flag false
    assert features(me)["assistant_triage"] is False
    ext = CurrentUser(id=USER, email="e@g3.test", role=Role.ESTIMATOR, is_active=True)
    assert features(ext)["assistant_triage"] is False


def test_the_features_flag_fails_closed_on_a_read_error(api, monkeypatch):
    from app.assistant import repo

    me = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(repo, "settings_row", boom)
    assert inbox_router.triage_flag(me) is False


def test_a_precomputed_inbox_flag_saves_the_second_flag_read(api, monkeypatch):
    me = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)

    def boom(_user):
        raise AssertionError("features_flag must not run again")

    monkeypatch.setattr(inbox_router, "features_flag", boom)
    assert inbox_router.triage_flag(me, inbox_on=True) is True
    assert inbox_router.triage_flag(me, inbox_on=False) is False


# -- The escalation kind: on or off only --------------------------------------


def test_the_kind_is_listed_with_on_and_off_only(api):
    kinds = {k["event_type"]: k for k in _call(api, "GET", "/assistant/alerts").json()["kinds"]}
    row = kinds[KIND]
    assert row["kind"] == "router" and row["modes"] == ["on", "off"]
    assert row["role_default"] == "on" and row["override"] is None and row["effective_mode"] == "on"


def test_the_kind_accepts_only_off(api):
    for mode in ("inbox", "digest", "alert", "on", "urgent", "nonsense"):
        res = _call(api, "PUT", f"/assistant/alerts/{KIND}", {"mode": mode})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_mode", mode
        assert api.db.tables["assistant_subscriptions"] == [], mode
    res = _call(api, "PUT", f"/assistant/alerts/{KIND}", {"mode": "off"})
    assert res.status_code == 200
    body = res.json()
    assert body["effective_mode"] == "off" and body["override"] == {"mode": "off", "source": "user"}
    (row,) = api.db.tables["assistant_subscriptions"]
    assert row["event_type"] == KIND and row["user_id"] == USER and row["enabled"] is False


def test_the_reset_route_turns_the_kind_back_on_with_no_own_row_left(api):
    assert _call(api, "PUT", f"/assistant/alerts/{KIND}", {"mode": "off"}).status_code == 200
    assert len(api.db.tables["assistant_subscriptions"]) == 1
    res = _call(api, "POST", f"/assistant/alerts/{KIND}/reset")
    assert res.status_code == 200
    body = res.json()
    assert body["effective_mode"] == "on" and body["override"] is None
    assert [r for r in api.db.tables["assistant_subscriptions"] if r["event_type"] == KIND] == []


def test_the_kind_stays_out_of_the_admin_grid_and_other_kinds_keep_their_modes(api):
    grid = _call(api, "GET", "/assistant/alerts/defaults").json()
    assert KIND not in [k["event_type"] for k in grid["kinds"]]
    # A Phase 7 router kind still takes digest as before.
    assert _call(api, "PUT", "/assistant/alerts/todo.due", {"mode": "digest"}).status_code == 200


# -- Triage on the Inbox items ------------------------------------------------


def test_actions_offer_mute_for_the_escalation_kind(api):
    _put(api, _router_row(event_type=KIND, id=INBOX3, notification_id=None, project_id=None))
    (item,) = _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]
    assert "mute" in item["actions"]
    assert item["kind_label"] == "Assistant flagged an update"
    # Other assistant kinds still do not.
    assert "mute" not in inbox_router._actions({"event_type": "assistant.calendar", "delivery": "immediate"})
    assert "mute" in inbox_router._actions({"event_type": "assistant.approval_expiring", "delivery": "immediate"})


def test_a_record_row_carries_its_triage_object(api):
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row())
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        inbox_id=INBOX2, status="decided", decision="suggest", reason="waiting_on_you",
        action="open_receive_quotes", decided_at=NOW.isoformat()))
    (item,) = _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]
    assert item["triage"] == {
        "status": "decided", "decision": "suggest", "reason": "waiting_on_you", "action": "open_receive_quotes",
        "reason_label": "Work is waiting on this person",
        "action_label": "Go through the vendor quotes on the bid",
        "work_prompt": ("Help me with the vendor quotes on bid 26.10.7301. "
                        "Which quotes are in and which are still missing?"),
        "decided_at": NOW.isoformat(), "severity_hint": None, "escalation_inbox_id": None,
    }


def test_an_accountant_is_not_shown_a_stored_write_action(api):
    """The role is checked again at read time: a person changed to Accountant
    after the decision keeps the reason but sees no write action or prompt,
    in the Inbox and through the tool route."""
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row())
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        inbox_id=INBOX2, status="decided", decision="suggest", reason="waiting_on_you",
        action="open_receive_quotes", decided_at=NOW.isoformat()))
    api.state.role = Role.ACCOUNTANT
    (item,) = _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]
    triage = item["triage"]
    assert triage["decision"] == "suggest" and triage["reason"] == "waiting_on_you"
    assert triage["reason_label"] == "Work is waiting on this person"
    assert triage["action"] is None and triage["action_label"] is None and triage["work_prompt"] is None
    _as_assistant(api)
    (row,) = _call(api, "GET", "/assistant/tools/inbox?segment=all").json()["items"]
    assert row["triage"]["action"] is None
    api.state.role = Role.EXECUTIVE
    api.state.via = None
    (item,) = _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]
    assert item["triage"]["action"] == "open_receive_quotes" and item["triage"]["work_prompt"]


def test_a_none_decision_and_a_pending_item_render_without_a_prompt(api):
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row())
    _put(api, _record_row(id=INBOX3, dedupe_key="p7:bell:b3", source_notification_id=BELL))
    api.db.tables["notifications"].append(bell_row(id=BELL))
    api.db.tables["assistant_triage_items"] += [
        triage_item_row(inbox_id=INBOX2, status="decided", decision="none", reason="covered_by_app",
                        decided_at=NOW.isoformat()),
        triage_item_row(inbox_id=INBOX3, status="pending"),
    ]
    items = {i["id"]: i for i in _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]}
    none = items[INBOX2]["triage"]
    assert none["decision"] == "none" and none["action"] is None and none["work_prompt"] is None
    assert none["reason_label"] == "BDR's own notification already says what matters"
    pending = items[INBOX3]["triage"]
    assert pending["status"] == "pending" and pending["decision"] is None and pending["reason_label"] is None


def test_an_escalated_row_links_the_escalation_inbox_row(api):
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row())
    _put(api, _router_row(event_type=KIND, id=INBOX3, dedupe_key=f"p8:esc:{INBOX2}", notification_id=None,
                          project_id=PROJECT))
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        inbox_id=INBOX2, status="decided", decision="escalate", reason="due_soon_unread", severity_hint="high",
        decided_at=NOW.isoformat()))
    items = {i["id"]: i for i in _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]}
    triage = items[INBOX2]["triage"]
    assert triage["decision"] == "escalate" and triage["severity_hint"] == "high"
    assert triage["escalation_inbox_id"] == INBOX3
    assert items[INBOX3]["triage"] is None            # only record rows are triaged


def test_triage_never_shows_another_persons_item(api):
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row())
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        user_id="33333333-3333-4333-8333-333333333333", inbox_id=INBOX2, status="decided", decision="escalate",
        reason="unread_long", decided_at=NOW.isoformat()))
    (item,) = _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]
    assert item["triage"] is None


def test_a_failed_triage_read_shows_the_items_without_triage(api, monkeypatch):
    from app.assistant import repo

    def boom(*_a, **_k):
        raise RuntimeError("triage table is down")

    monkeypatch.setattr(repo, "triage_items_for_inbox", boom)
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row())
    res = _call(api, "GET", "/assistant/inbox?segment=all")
    assert res.status_code == 200 and res.json()["items"][0]["triage"] is None


def test_the_digest_route_joins_triage_too(api):
    api.db.tables["assistant_digests"].append(digest_row())
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row(digest_id=DIGEST))
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        inbox_id=INBOX2, status="decided", decision="suggest", reason="needs_decision",
        action="ask_assistant_summary", decided_at=NOW.isoformat()))
    body = _call(api, "GET", f"/assistant/inbox/digests/{DIGEST}").json()
    (item,) = body["items"]
    assert item["triage"]["action"] == "ask_assistant_summary"
    assert item["triage"]["work_prompt"] == "Give me a short summary of where bid 26.10.7301 stands."


def test_an_unknown_stored_value_renders_no_label_instead_of_failing(api):
    api.db.tables["notifications"].append(bell_row(id=BELL2))
    _put(api, _record_row())
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        inbox_id=INBOX2, status="decided", decision="suggest", reason="from_the_future", action="new_action",
        decided_at=NOW.isoformat()))
    (item,) = _call(api, "GET", "/assistant/inbox?segment=all").json()["items"]
    assert item["triage"]["reason_label"] is None and item["triage"]["work_prompt"] is None


# -- GET /assistant/tools/inbox -----------------------------------------------


def _as_assistant(api):
    api.state.via = RUN


def test_the_tool_route_refuses_a_human_with_a_bare_404(api):
    res = _call(api, "GET", "/assistant/tools/inbox")
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}


def test_the_tool_route_answers_inbox_off(api, monkeypatch):
    _as_assistant(api)
    assert _call(api, "GET", "/assistant/tools/inbox").status_code == 200
    _settings(api)["proactive_enabled"] = False
    res = _call(api, "GET", "/assistant/tools/inbox")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "inbox_off"
    _settings(api)["proactive_enabled"] = True
    monkeypatch.setattr(api.settings, "assistant_proactive_enabled", False)
    assert _call(api, "GET", "/assistant/tools/inbox").status_code == 404
    monkeypatch.setattr(api.settings, "assistant_proactive_enabled", True)
    monkeypatch.setattr(api.settings, "assistant_proactive_user_allowlist", "someone@g3.test")
    assert _call(api, "GET", "/assistant/tools/inbox").json()["detail"]["code"] == "inbox_off"


def test_the_tool_route_is_a_plain_def_on_the_expected_path():
    import inspect

    routes = [r for r in tools_router.router.routes if r.path == "/assistant/tools/inbox"]
    assert len(routes) == 1 and routes[0].methods == {"GET"}
    assert not inspect.iscoroutinefunction(routes[0].endpoint)


def _seed_tool_rows(api):
    api.db.tables["notifications"] += [bell_row(id=BELL), bell_row(id=BELL2)]
    _put(api, _record_row())                                                  # newest record row
    _put(api, _router_row(id=INBOX, project_id=PROJECT_B, created_at=(P7_T0 - timedelta(hours=1)).isoformat()))
    _put(api, inbox_row(id=INBOX3, delivery="direct", severity="normal", event_type="assistant.calendar",
                        dedupe_key="calendar:ev1", delivered_at=P7_T0.isoformat(), deliver_after=None,
                        created_at=(P7_T0 - timedelta(hours=2)).isoformat(), title="A reminder", why="Soon."))
    api.db.tables["assistant_triage_items"].append(triage_item_row(
        inbox_id=INBOX2, status="decided", decision="escalate", reason="due_soon_part_missing",
        action=None, severity_hint="urgent", decided_at=NOW.isoformat()))


def test_the_tool_route_flattens_rows_to_the_documented_fields(api):
    _seed_tool_rows(api)
    _as_assistant(api)
    body = _call(api, "GET", "/assistant/tools/inbox?segment=all").json()
    assert body["next"] is None and "digests" not in body
    rows = {r["id"]: r for r in body["items"]}
    assert set(rows) == {INBOX2, INBOX, INBOX3}
    rec = rows[INBOX2]
    assert set(rec) == {"id", "kind", "kind_label", "group", "severity", "delivery", "from_bell", "project_number",
                        "project_id", "received_at", "read", "triage", "summary", "detail"}
    assert rec["kind"] == "bell.late_quote" and rec["from_bell"] is True and rec["delivery"] == "record"
    assert rec["project_number"] == "26.10.7301" and rec["project_id"] == PROJECT
    assert rec["received_at"] == "2026-10-30 7:00 AM PT"
    assert rec["triage"] == {"decision": "escalate", "reason": "due_soon_part_missing", "action": None}
    assert rec["summary"] == "A late quote"
    assert rows[INBOX]["from_bell"] is False and rows[INBOX]["triage"] is None
    assert rows[INBOX3]["delivery"] == "direct" and rows[INBOX3]["read"] is True
    # Newest first.
    assert [r["id"] for r in body["items"]] == [INBOX2, INBOX, INBOX3]


def test_the_tool_route_hides_dismissed_rows_and_shares_the_read_state(api):
    api.db.tables["notifications"] += [bell_row(id=BELL2, read_at=P7_T0.isoformat()),
                                       bell_row(id=BELL, dismissed_at=P7_T0.isoformat())]
    _put(api, _record_row())                                                   # source bell read
    _put(api, _record_row(id=INBOX3, dedupe_key="p7:bell:b3", source_notification_id=BELL))   # bell dismissed
    _put(api, _router_row(id=INBOX4, dedupe_key="x4", notification_id=None,
                          dismissed_at=P7_T0.isoformat(), dismissed_reason="user"))
    _as_assistant(api)
    body = _call(api, "GET", "/assistant/tools/inbox?segment=all").json()
    assert [r["id"] for r in body["items"]] == [INBOX2]
    assert body["items"][0]["read"] is True
    # Record rows are never in the unread segment (repo.inbox_list keeps only
    # immediate and digest rows there), even with the source bell unread, the
    # same as the human route.
    api.db.tables["notifications"][0]["read_at"] = None
    unread = _call(api, "GET", "/assistant/tools/inbox?segment=unread").json()
    assert unread["items"] == []
    api.state.via = None
    human = _call(api, "GET", "/assistant/inbox?segment=unread").json()
    assert human["items"] == []


def test_the_tool_route_filters_by_kind_and_project(api):
    _seed_tool_rows(api)
    _as_assistant(api)
    by_kind = _call(api, "GET", "/assistant/tools/inbox?segment=all&kind=todo.due").json()
    assert [r["id"] for r in by_kind["items"]] == [INBOX]
    by_id = _call(api, "GET", f"/assistant/tools/inbox?segment=all&project={PROJECT}").json()
    assert [r["id"] for r in by_id["items"]] == [INBOX2]
    by_number = _call(api, "GET", "/assistant/tools/inbox?segment=all&project=26.10.7302").json()
    assert [r["id"] for r in by_number["items"]] == [INBOX]
    # A real project with no rows is an empty page, not an error.
    empty = _call(api, "GET", f"/assistant/tools/inbox?segment=all&project={PROJECT_C}").json()
    assert empty == {"items": [], "next": None}
    both = _call(api, "GET", f"/assistant/tools/inbox?segment=all&kind=todo.due&project={PROJECT}").json()
    assert both["items"] == []


@pytest.mark.parametrize("value", ["26.10.9999", "hello", PROJECT[:8], PROJECT[:12]])
def test_the_tool_route_answers_a_coded_422_for_a_project_it_cannot_resolve(api, value):
    """A short id or a missing number is never an empty page, which would read
    as "no rows on that bid"."""
    _seed_tool_rows(api)
    _as_assistant(api)
    res = _call(api, "GET", f"/assistant/tools/inbox?segment=all&project={value}")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "unknown_project"


def test_an_unreadable_project_lookup_is_a_503_not_an_empty_page(api, monkeypatch):
    _as_assistant(api)

    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(tools_router, "supabase_client", SimpleNamespace(get_supabase=boom))
    res = _call(api, "GET", "/assistant/tools/inbox?segment=all&project=26.10.7301")
    assert res.status_code == 503 and res.json()["detail"]["code"] == "project_lookup_failed"


def test_the_tool_route_pages_by_keyset_and_rejects_a_bad_cursor(api):
    _seed_tool_rows(api)
    _as_assistant(api)
    first = _call(api, "GET", "/assistant/tools/inbox?segment=all&limit=2").json()
    assert [r["id"] for r in first["items"]] == [INBOX2, INBOX] and first["next"]
    nxt = _call(api, "GET", f"/assistant/tools/inbox?segment=all&limit=2&before={first['next']}").json()
    assert [r["id"] for r in nxt["items"]] == [INBOX3] and nxt["next"] is None
    res = _call(api, "GET", "/assistant/tools/inbox?before=garbage")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_cursor"
    assert _call(api, "GET", "/assistant/tools/inbox?limit=0").status_code == 422
    assert _call(api, "GET", "/assistant/tools/inbox?limit=51").status_code == 422
    assert _call(api, "GET", "/assistant/tools/inbox?segment=nope").status_code == 422


def test_the_tool_route_lists_digests_with_the_documented_fields(api):
    api.db.tables["assistant_digests"].append(digest_row(item_count=3, record_count=2))
    api.db.tables["notifications"].append(bell_row(id=BELL))
    _put(api, _router_row(delivery="digest", digest_id=DIGEST, notification_id=BELL))
    _as_assistant(api)
    body = _call(api, "GET", "/assistant/tools/inbox?segment=digests").json()
    assert body["digests"] == [{"id": DIGEST, "slot": "am", "date": "2026-10-30", "item_count": 3,
                                "record_count": 2}]
    assert [r["delivery"] for r in body["items"]] == ["digest"]


def test_the_tool_route_never_writes(api):
    _seed_tool_rows(api)
    _as_assistant(api)
    before = {k: [dict(r) for r in v] for k, v in api.db.tables.items()}
    n_calls = len(api.db.calls)
    assert _call(api, "GET", "/assistant/tools/inbox?segment=all").status_code == 200
    assert _call(api, "GET", "/assistant/tools/inbox?segment=unread").status_code == 200
    assert api.db.tables == before
    assert {op for op, _t in api.db.calls[n_calls:]} == {"select"}
    assert api.db.rpc_calls == [] or all(not n.startswith(("assistant_inbox_mark", "assistant_inbox_dismiss"))
                                         for n, _ in api.db.rpc_calls)


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES, key=lambda r: r.value))
def test_every_internal_role_reads_its_own_rows_through_the_tool(api, role):
    api.state.role = role
    _seed_tool_rows(api)
    _put(api, _router_row(id=INBOX4, user_id="33333333-3333-4333-8333-333333333333", dedupe_key="other",
                          notification_id=None))
    _as_assistant(api)
    ids = [r["id"] for r in _call(api, "GET", "/assistant/tools/inbox?segment=all").json()["items"]]
    assert INBOX4 not in ids and INBOX2 in ids


def test_a_tool_result_is_template_text_only(api):
    """summary and detail are the stored title and why templates; the route
    adds no free text of its own (they are the tainted fields of the tool)."""
    _seed_tool_rows(api)
    _as_assistant(api)
    body = _call(api, "GET", "/assistant/tools/inbox?segment=all").json()
    assert {r["summary"] for r in body["items"]} >= {"A late quote"}
    for row in body["items"]:
        assert "title" not in row and "why" not in row           # not whitelisted taint keys


# -- Source hygiene -----------------------------------------------------------


def test_the_phase_8_router_files_keep_to_the_rules():
    for name in ("app/routers/assistant_inbox.py", "app/routers/assistant_inbox_tools.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text, name
        for banned in ("app.assistant.loop", "app.assistant.llm_messages", "app.services.llm", "complete_messages",
                       "notify_role", "notify_user", "teams_alerts", "graph_email"):
            assert banned not in text, (name, banned)


def test_the_tool_router_names_the_database_client_only_for_the_project_number(api):
    text = (ROOT / "app/routers/assistant_inbox_tools.py").read_text(encoding="utf-8")
    import re

    assert not re.search(r'table\(\s*["\']assistant_', text)
    assert text.count("get_supabase") == 1


def test_main_mounts_the_tool_router_and_the_triage_loop_behind_its_flags():
    text = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "assistant_inbox_tools.router" in text
    assert "assistant_triage_task" in text and "assistant_triage_loop_enabled" in text
    assert "settings.assistant_triage_enabled" in text
    assert text.count("assistant_triage_task") >= 3          # declared, created, cancelled
    assert "app.assistant" not in text
    from app.main import app

    paths = {(m, r.path) for r in app.routes if hasattr(r, "methods") for m in r.methods}
    assert {("GET", "/assistant/tools/inbox"), ("GET", "/assistant/alerts/company/triage"),
            ("PUT", "/assistant/alerts/company/triage"), ("PUT", "/assistant/alerts/me/digest-email")} <= paths


def test_pacific_midnight_handles_both_offsets():
    assert inbox_router._pacific_midnight(datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)) == datetime(
        2026, 10, 30, 7, 0, tzinfo=timezone.utc)                     # PDT
    assert inbox_router._pacific_midnight(datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)) == datetime(
        2026, 11, 2, 8, 0, tzinfo=timezone.utc)                      # PST
