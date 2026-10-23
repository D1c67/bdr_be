"""Teams alerts (services/teams_alerts, 0175): the Teams copy of every
notification mirrored to email. Graph, the Bot Connector and Supabase are
faked; the thread hand-off is replaced with a synchronous shim."""

from types import SimpleNamespace

import httpx
import pytest

from app.services import notification_email as ne
from app.services import teams_alerts as ta

FRONTEND = "https://bdr.example.com"


def _settings(**over):
    base = dict(
        teams_alerts_enabled=True,
        ms_tenant_id="tenant",
        ms_client_id="client",
        ms_client_secret="secret",
        teams_bot_app_id="",
        teams_bot_app_secret="",
        teams_app_manifest_id="manifest",
        teams_bot_service_url="https://smba.example/teams/",
        frontend_url=FRONTEND,
        rfp_testing_enabled=False,
        notification_emails_enabled=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _SyncThread:
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class _FakeQuery:
    def __init__(self, store, name):
        self._store, self._name = store, name
        self._update = None
        self._eq = None

    def select(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def update(self, fields):
        self._update = fields
        return self

    def upsert(self, row, **k):
        self._store.setdefault("upserts", []).append((self._name, row))
        return self

    def execute(self):
        if self._update is not None:
            self._store.setdefault("updates", []).append((self._name, self._eq, self._update))
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=self._store.get(self._name, []))


class _FakeSupabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _FakeQuery(self.store, name)


@pytest.fixture
def wired(monkeypatch):
    """Configured sender with every Microsoft call faked; returns the log."""
    log = {"posted": [], "resolved": 0}
    store = {
        "profiles": [
            {"id": "u1", "full_name": "Pat Lee", "email": "pat@g3.com",
             "role": "estimating_admin", "is_active": True},
            {"id": "est", "full_name": "Est", "email": "est@x.com",
             "role": "estimator", "is_active": True},
            {"id": "off", "full_name": "Off", "email": "off@g3.com",
             "role": "executive", "is_active": True},
        ],
        "projects": [{"id": "p1", "name": "Tower", "number": "42"}],
        ta.TABLE: [{"user_id": "off", "enabled": False}],
    }
    sb = _FakeSupabase(store)
    monkeypatch.setattr(ta, "get_settings", lambda: _settings())
    monkeypatch.setattr(ne, "get_settings", lambda: _settings())
    monkeypatch.setattr(ta, "get_supabase", lambda: sb)
    monkeypatch.setattr(ta.threading, "Thread", _SyncThread)
    monkeypatch.setattr(ta.graph_email, "_active_test_session", lambda sb=None: None)
    monkeypatch.setattr(ta, "_lookup_user", lambda email: f"aad-{email}")
    monkeypatch.setattr(ta, "_catalog_id", lambda: "cat")
    monkeypatch.setattr(ta, "_install", lambda aad, cat: None)

    def _chat(aad, cat):
        log["resolved"] += 1
        return f"chat-{aad}-{log['resolved']}"

    monkeypatch.setattr(ta, "_chat_id", _chat)
    monkeypatch.setattr(
        ta, "_post_card", lambda chat, card, summary: log["posted"].append((chat, card, summary))
    )
    log["store"] = store
    return log


def test_queue_noop_when_flag_off(monkeypatch):
    started = []
    monkeypatch.setattr(ta, "get_settings", lambda: _settings(teams_alerts_enabled=False))
    monkeypatch.setattr(ta.threading, "Thread",
                        lambda **k: SimpleNamespace(start=lambda: started.append(True)))
    ta.queue([{"user_id": "u1", "type": "assigned", "message": "x"}])
    assert started == []


def test_queue_noop_without_credentials(monkeypatch):
    started = []
    monkeypatch.setattr(ta, "get_settings", lambda: _settings(ms_client_secret=""))
    monkeypatch.setattr(ta.threading, "Thread",
                        lambda **k: SimpleNamespace(start=lambda: started.append(True)))
    ta.queue([{"user_id": "u1", "type": "assigned", "message": "x"}])
    assert started == []


def test_separate_bot_registration_is_used_when_set(monkeypatch):
    monkeypatch.setattr(
        ta, "get_settings", lambda: _settings(teams_bot_app_id="bot", teams_bot_app_secret="s2")
    )
    assert ta._bot_credentials() == ("bot", "s2")


def test_sends_card_to_internal_user_and_stamps_row(wired):
    ta.queue([
        {"id": "n1", "user_id": "u1", "project_id": "p1", "type": "quote.received",
         "message": "Quote in from Acme"},
    ])
    assert len(wired["posted"]) == 1
    chat, card, summary = wired["posted"][0]
    assert chat == "chat-aad-pat@g3.com-1"
    # The email subject without the prefix, no em dash.
    assert summary == "A vendor quote came in · #42 Tower"
    assert "—" not in summary
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "View quotes", "url": f"{FRONTEND}/projects/p1"}
    ]
    updates = wired["store"]["updates"]
    assert updates[0][0] == "notifications" and updates[0][1] == ("id", "n1")
    assert "teams_sent_at" in updates[0][2]
    # The chat is cached for the next alert.
    cached = [row for name, row in wired["store"]["upserts"] if row.get("conversation_id")]
    assert cached[0]["aad_object_id"] == "aad-pat@g3.com"


def test_estimator_and_switched_off_users_get_nothing(wired):
    ta.queue([
        {"user_id": "est", "project_id": "p1", "type": "assigned", "message": "m"},
        {"user_id": "off", "project_id": "p1", "type": "assigned", "message": "m"},
    ])
    assert wired["posted"] == []


def test_cached_chat_is_reused(wired):
    wired["store"][ta.TABLE] = [
        {"user_id": "u1", "enabled": True, "aad_object_id": "aad-1", "conversation_id": "chat-old"}
    ]
    ta.queue([{"user_id": "u1", "project_id": None, "type": "nudge", "message": "m"}])
    assert wired["posted"][0][0] == "chat-old"
    assert wired["resolved"] == 0


def test_gone_chat_is_resolved_again_once(wired, monkeypatch):
    wired["store"][ta.TABLE] = [
        {"user_id": "u1", "enabled": True, "aad_object_id": "aad-1", "conversation_id": "chat-old"}
    ]

    def _post(chat, card, summary):
        if chat == "chat-old":
            raise ta._ChatGone()
        wired["posted"].append(chat)

    monkeypatch.setattr(ta, "_post_card", _post)
    ta.queue([{"user_id": "u1", "project_id": None, "type": "nudge", "message": "m"}])
    assert wired["posted"] == ["chat-aad-1-1"]


def test_failure_is_recorded_and_isolated(wired, monkeypatch):
    def _lookup(email):
        if email == "pat@g3.com":
            raise ta.TeamsAlertError("No Microsoft 365 account found for pat@g3.com")
        return "aad-x"

    monkeypatch.setattr(ta, "_lookup_user", _lookup)
    wired["store"]["profiles"].append(
        {"id": "u2", "full_name": "B", "email": "b@g3.com", "role": "executive", "is_active": True}
    )
    ta.queue([
        {"user_id": "u1", "project_id": None, "type": "nudge", "message": "m"},
        {"user_id": "u2", "project_id": None, "type": "nudge", "message": "m"},
    ])
    assert len(wired["posted"]) == 1
    errors = [row for _, row in wired["store"]["upserts"] if row.get("last_error")]
    assert errors[0]["user_id"] == "u1"
    assert "No Microsoft 365 account" in errors[0]["last_error"]


def test_test_session_holds_every_send(wired, monkeypatch):
    monkeypatch.setattr(ta.graph_email, "_active_test_session", lambda sb=None: {"id": "s"})
    ta.queue([{"user_id": "u1", "project_id": None, "type": "nudge", "message": "m"}])
    assert wired["posted"] == []


def test_card_keeps_outside_text_plain():
    card = ta.build_card(
        heading="A vendor quote came in",
        message="See [click](https://evil.example) **now**",
        project_label="#42 · Tower [x](https://evil.example)",
        cta_label="View quotes",
        cta_url=f"{FRONTEND}/projects/p1",
    )
    blocks = card["body"]
    # Outside text only ever sits in TextRuns (no markdown in Teams).
    outside = [b for b in blocks if b["type"] == "RichTextBlock"]
    assert [b["inlines"][0]["text"] for b in outside] == [
        "#42 · Tower [x](https://evil.example)",
        "See [click](https://evil.example) **now**",
    ]
    assert all(
        "evil" not in b.get("text", "") for b in blocks if b["type"] == "TextBlock"
    )


def test_card_truncates_long_messages():
    card = ta.build_card(
        heading="h", message="x" * 10000, project_label=None, cta_label="Open", cta_url=FRONTEND
    )
    assert len(card["body"][-1]["inlines"][0]["text"]) == ta._MESSAGE_MAX_CHARS


def test_email_mirror_hands_rows_to_teams(monkeypatch):
    seen = []
    monkeypatch.setattr(ta, "queue", lambda rows: seen.append(rows))
    monkeypatch.setattr(ne, "get_settings", lambda: _settings())
    ne.queue([{"user_id": "u1", "type": "assigned", "message": "x"}, {"user_id": None}])
    assert seen == [[{"user_id": "u1", "type": "assigned", "message": "x"}]]


def test_graph_permission_error_reads_plainly(monkeypatch):
    def _raise(*a, **k):
        req = httpx.Request("GET", "https://graph.microsoft.com/v1.0/users/x")
        raise httpx.HTTPStatusError("no", request=req, response=httpx.Response(403, request=req))

    monkeypatch.setattr(ta.graph_email, "graph_request", _raise)
    with pytest.raises(ta.TeamsAlertError, match="missing a Graph permission"):
        ta._graph("GET", "/users/x", step="Find the Teams user")


def test_post_card_maps_bot_connector_statuses(monkeypatch):
    monkeypatch.setattr(ta, "get_settings", lambda: _settings())
    monkeypatch.setattr(ta, "_bot_token", lambda: "tok")
    calls = []

    def _post(url, **kw):
        calls.append(url)
        return httpx.Response(status_for[0])

    monkeypatch.setattr(ta.httpx, "post", _post)
    status_for = [201]
    ta._post_card("19:abc@unq.gbl.spaces", {}, "s")
    assert calls[0] == "https://smba.example/teams/v3/conversations/19%3Aabc%40unq.gbl.spaces/activities"
    status_for[0] = 404
    with pytest.raises(ta._ChatGone):
        ta._post_card("c", {}, "s")
    status_for[0] = 401
    with pytest.raises(ta.TeamsAlertError, match="sign-in"):
        ta._post_card("c", {}, "s")


# ── /users/me/teams-alerts ───────────────────────────────────────────────────


def _user():
    from app.core.deps import CurrentUser
    from app.core.roles import Role

    return CurrentUser(id="u1", email="pat@g3.com", role=Role.EXECUTIVE, is_active=True)


def test_status_defaults_to_on_without_a_row(monkeypatch):
    monkeypatch.setattr(ta, "get_settings", lambda: _settings())
    monkeypatch.setattr(ta, "get_supabase", lambda: _FakeSupabase({}))
    assert ta.status("u1") == {
        "available": True, "enabled": True, "connected": False,
        "last_sent_at": None, "last_error": None, "last_error_at": None,
    }


def test_switch_endpoint_saves_and_audits(monkeypatch):
    from app.routers import users

    store = {}
    monkeypatch.setattr(ta, "get_settings", lambda: _settings())
    monkeypatch.setattr(ta, "get_supabase", lambda: _FakeSupabase(store))
    audits = []
    monkeypatch.setattr(users, "audit", lambda *a: audits.append(a))
    users.update_teams_alerts(users.TeamsAlertsIn(enabled=False), _user())
    assert store["upserts"][0][1]["enabled"] is False
    assert audits[0][1] == "user.teams_alerts.update"


def test_test_endpoint_refuses_when_not_set_up(monkeypatch):
    from fastapi import HTTPException

    from app.routers import users

    monkeypatch.setattr(ta, "get_settings", lambda: _settings(teams_alerts_enabled=False))
    with pytest.raises(HTTPException) as exc:
        users.send_teams_test_alert(_user())
    assert exc.value.status_code == 409


def test_test_endpoint_surfaces_the_failure(monkeypatch):
    from fastapi import HTTPException

    from app.routers import users

    store = {"profiles": [{"email": "pat@g3.com", "full_name": "Pat Lee"}]}
    monkeypatch.setattr(ta, "get_settings", lambda: _settings())
    monkeypatch.setattr(ta, "get_supabase", lambda: _FakeSupabase(store))
    monkeypatch.setattr(users, "get_supabase", lambda: _FakeSupabase(store))

    def _boom(*a):
        raise ta.TeamsAlertError("The BDR Alerts app is not in the Teams app catalog yet")

    monkeypatch.setattr(ta, "send_test", _boom)
    with pytest.raises(HTTPException) as exc:
        users.send_teams_test_alert(_user())
    assert exc.value.status_code == 502
    assert "catalog" in exc.value.detail
