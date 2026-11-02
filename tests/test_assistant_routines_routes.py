"""The routines API (app/routers/assistant_routines.py) against
docs/ASSISTANT_PHASE3_PLAN.md 3.2.3, 3.3, 3.6 and 5.3.

Invariants pinned here:

  create     approval-bound only: a human, a turn principal and a header
             that differs from the token's approval id get the bare 404 and
             no row; a missing header is 400; a replay of the same card is
             200 with the row it made; the cap is 409 schedule_cap_reached;
             a bad cadence is 422; a closed watch is 409; both occurrence
             slots are filled from a minute-floored dtstart;
  draft      never writes; every refusal is ok false with its code; the
             notices of 3.4.7 (no digest notice for an Accountant) and
             cap_near at 9 active;
  fences     pause, resume and end move only from the status they read; a
             lost fence is 409 schedule_state_changed with the current
             row; resume recomputes both slots, clears the counts, may hit
             the cap (the row stays paused) and needs the sub-app back on;
  tools      a human gets 404; the turn context comes from the begun ledger
             row only (409 turn_context_unavailable otherwise); the tool
             end pauses in a tainted turn and says so, ends in an untainted
             one; resume and Run now are refused while tainted;
  run now    scheduler_off, schedule_not_active, schedule_busy and
             run_now_too_soon; the row is a claimed run_now with no worker
             and a non-zero microsecond, so a click at hh:00:00 never shares
             a scheduled slot's occurrence key;
  previews   write nothing, redact per role, report what would be sent and
             the digest stub;
  recipe     the rows route redacts per role, 404s an unknown recipe and
             404s with Bidding off; `at` is clamped to 24 hours;
  holidays   Executive and IT Admin only, ISO dates, distinct, at most 60,
             within 2 years, stored sorted, audited.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, repo, rrules
from app.core import provenance, supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant_routines as routines

from tests.assistant_fakes import (
    APPROVAL_ID, CONV, MSG_ASSISTANT, NOW, OTHER_USER, PROJECT, RUN, SCHEDULE, SCHEDULE2, USER, WEEKDAYS_7AM,
    FakeDB, conversation, message, pending_answer, profile, schedule, schedule_run, settings_row,
)

HEADER = "X-Assistant-Idempotency-Key"
OTHER_APPROVAL = "99999999-9999-4999-8999-999999999990"
KEY = "ledger-key-1"
EM_DASH = chr(0x2014)
WEEKDAYS_7 = {"frequency": "weekdays", "hour": 7, "minute": 0, "days": [], "day_of_month": None,
              "interval": 1, "window_start_hour": None, "window_end_hour": None}


class _Actor:
    """The shim's part: enter the assistant provenance around a request."""

    def __init__(self, app):
        self.app = app
        self.actor: tuple | None = None

    async def __call__(self, scope, receive, send):
        if self.actor is not None and scope["type"] == "http":
            with provenance.acting_as_assistant(*self.actor):
                return await self.app(scope, receive, send)
        return await self.app(scope, receive, send)


def _row(**over) -> dict:
    """One recipe row as assistant_recipes.rows_for returns it (raw)."""
    project = {"id": PROJECT, "name": "Alpha Clinic", "number": "26.10.7267",
               "internal_bid_at": NOW.isoformat(), "actual_bid_at": NOW.isoformat(), "current_stage": "rfqs"}
    base = {"project": project, "checks": {"has_quotes": False, "has_labor": True, "has_markups": True},
            "severity": "red", "missing_categories": None}
    base.update(over)
    return base


class FakeData:
    """app/services/assistant_recipes as the routes see it."""

    def __init__(self):
        self.rows: list[dict] = [_row()]
        self.watch = "open"
        self.digest = False
        self.calls: list[tuple] = []

    def rows_for(self, sb, recipe, *, at_utc, horizon_days=None, project_id=None, period=None, week=None,
                 role=None):
        from app.assistant import recipes

        self.calls.append(("rows_for", recipe, at_utc, horizon_days, project_id, period, week, role))
        recipes.validate_params(recipe, horizon_days, project_id, period=period, week=week)
        watch = {"project_id": project_id, "state": self.watch} if project_id else None
        out = {"recipe": recipe, "window": {"start": "s", "end": "e", "label": "Friday, October 23"},
               "rows": [dict(r) for r in self.rows], "watch": watch}
        out.update(getattr(self, "more", {}))
        return out

    def watch_state(self, sb, project_id):
        self.calls.append(("watch_state", project_id))
        return self.watch

    def digest_sent(self, sb, user_id, local_date):
        self.calls.append(("digest_sent", user_id, local_date))
        return self.digest


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[], assistant_conversations=[], assistant_messages=[], assistant_tool_calls=[],
        assistant_schedules=[], assistant_schedule_runs=[], audit_log=[],
        projects=[{"id": PROJECT, "number": "26.10.7267", "name": "Alpha Clinic"}],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(get_settings(), "bidding_enabled", True)
    monkeypatch.setattr(get_settings(), "assistant_scheduler_enabled", True)
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    data = FakeData()
    monkeypatch.setattr(routines, "_data", lambda: data)
    clock = {"now": NOW}
    monkeypatch.setattr(routines, "_now", lambda: clock["now"])
    from app.services import notifications

    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    state = SimpleNamespace(role=Role.EXECUTIVE, via=None)

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(routines.router)
    app.dependency_overrides[get_current_user] = _user
    actor = _Actor(app)
    return SimpleNamespace(client=TestClient(actor), db=db, data=data, state=state, actor=actor, clock=clock)


def _as_card(api, approval_id: str | None = APPROVAL_ID):
    """The execute job of an approved card (approval None: a turn)."""
    api.state.via = RUN
    api.actor.actor = (USER, RUN, approval_id)


def _human(api):
    api.state.via = None
    api.actor.actor = None


def _create(api, body=None, headers=None):
    payload = body or {"recipe": "due_today", "cadence": WEEKDAYS_7, "delivery": "chat_bell",
                       "only_if_changed": False, "horizon_days": None, "project_id": None}
    return api.client.post("/assistant/schedules", json=payload,
                           headers={HEADER: APPROVAL_ID} if headers is None else headers)


def _begun(api, tool: str, *, tainted=False, key=KEY, conv_tainted=False):
    """The turn whose ledger row a tool route trusts."""
    api.state.via = RUN
    api.actor.actor = (USER, RUN, None)
    api.db.tables["assistant_conversations"] = [
        conversation(tainted_at=NOW.isoformat() if conv_tainted else None)]
    api.db.tables["assistant_messages"] = [message(content="Stop sending that report."),
                                           pending_answer(status="running")]
    api.db.tables["assistant_tool_calls"].append({
        "id": f"tc-{len(api.db.tables['assistant_tool_calls'])}", "user_id": USER, "run_id": RUN,
        "idempotency_key": key, "phase": "begun", "tool_name": tool, "conversation_id": CONV,
        "message_id": MSG_ASSISTANT, "tainted_context": tainted, "seq": 1,
    })
    return {HEADER: key}


def _sched(api, **over) -> dict:
    row = schedule(**over)
    api.db.tables["assistant_schedules"].append(row)
    return row


# ── create (approval-bound) ──────────────────────────────────────────────────


def test_create_fills_both_slots_and_a_replay_answers_the_same_row(api):
    _as_card(api)
    res = _create(api)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["title"] == "Bids due today, weekdays at 7:00 AM"
    assert body["cadence_text"] == "Weekdays at 7:00 AM Pacific, holidays skipped"
    assert body["rrule"] == WEEKDAYS_7AM and body["status"] == "active" and body["delivery"] == "chat_bell"
    assert body["conversation_id"] is None and len(body["next_runs"]) == 3
    row = api.db.tables["assistant_schedules"][0]
    assert row["approval_id"] == APPROVAL_ID and row["sub_app"] == "bidding" and row["params"] == {}
    assert row["timezone"] == "America/Los_Angeles"
    dtstart = datetime.fromisoformat(row["dtstart"])
    assert dtstart.second == 0 and dtstart.microsecond == 0 and dtstart <= NOW
    first, second = datetime.fromisoformat(row["next_run_at"]), datetime.fromisoformat(row["after_next_run_at"])
    assert NOW < first < second
    for slot in (first, second):
        local = slot.astimezone(rrules.PT)
        assert (local.hour, local.minute, local.weekday() < 5) == (7, 0, True)
    again = _create(api)
    assert again.status_code == 200 and again.json()["id"] == body["id"]
    assert len(api.db.tables["assistant_schedules"]) == 1


def test_create_is_404_for_a_human_a_turn_and_a_header_that_differs(api):
    _human(api)
    assert _create(api).status_code == 404
    _as_card(api, None)                                  # a turn principal: no approval
    assert _create(api).status_code == 404
    _as_card(api)
    assert _create(api, headers={HEADER: OTHER_APPROVAL}).status_code == 404
    res = _create(api, headers={})
    assert res.status_code == 400 and res.headers["X-Error-Code"] == "idempotency_key_required"
    assert api.db.tables["assistant_schedules"] == []


def test_create_refusals(api):
    _as_card(api)
    bad = {**WEEKDAYS_7, "hour": None}
    res = _create(api, {"recipe": "due_today", "cadence": bad})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_cadence"
    res = _create(api, {"recipe": "due_today", "cadence": {**WEEKDAYS_7, "days": ["MO"]}})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_cadence"
    res = _create(api, {"recipe": "due_today", "cadence": WEEKDAYS_7, "horizon_days": 5})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_params"
    res = _create(api, {"recipe": "nope", "cadence": WEEKDAYS_7})
    assert res.status_code == 404 and res.json()["detail"]["code"] == "recipe_not_found"
    api.data.watch = "closed"
    res = _create(api, {"recipe": "labor_not_entered", "cadence": WEEKDAYS_7, "project_id": PROJECT})
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "watch_project_closed"
    assert api.db.tables["assistant_schedules"] == []


def test_create_a_watch_names_the_project_number(api):
    _as_card(api)
    res = _create(api, {"recipe": "labor_not_entered", "cadence": {**WEEKDAYS_7, "hour": 8},
                        "project_id": PROJECT, "delivery": "chat_bell_email", "only_if_changed": True})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["title"] == "Labor not entered on 26.10.7267, weekdays at 8:00 AM"
    assert body["project"] == {"id": PROJECT, "number": "26.10.7267", "name": "Alpha Clinic"}
    row = api.db.tables["assistant_schedules"][0]
    assert row["project_id"] == PROJECT and row["params"] == {"project_id": PROJECT}
    assert row["only_if_changed"] is True and row["delivery"] == "chat_bell_email"


def test_create_at_the_cap_is_409(api):
    for i in range(10):
        _sched(api, id=f"ffffffff-ffff-4fff-8fff-{i:012d}", approval_id=None)
    _as_card(api)
    res = _create(api)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "schedule_cap_reached"
    assert len(api.db.tables["assistant_schedules"]) == 10


def test_create_with_bidding_off_for_the_role_is_409(api, monkeypatch):
    monkeypatch.setattr(routines, "enabled_map", lambda role: {"bidding": False})
    _as_card(api)
    res = _create(api)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"


# ── draft ────────────────────────────────────────────────────────────────────


def _draft(api, **params):
    base = {"recipe": "due_today", "frequency": "weekdays", "hour": "7", "minute": "0"}
    base.update(params)
    return api.client.get("/assistant/schedules/draft", params={k: v for k, v in base.items() if v is not None})


def test_draft_normalizes_and_never_writes(api):
    calls_before = len(api.db.calls)
    res = _draft(api)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["code"] is None
    assert body["body"] == {"recipe": "due_today", "cadence": WEEKDAYS_7, "delivery": "chat_bell",
                            "only_if_changed": False, "horizon_days": None, "project_id": None,
                            "anchor": None, "period": None, "week": None}
    assert body["title"] == "Bids due today, weekdays at 7:00 AM" and body["rrule"] == WEEKDAYS_7AM
    assert body["cadence_text"] == "Weekdays at 7:00 AM Pacific, holidays skipped"
    assert len(body["next_runs"]) == 3 and all(datetime.fromisoformat(t) > NOW for t in body["next_runs"])
    assert [w["code"] for w in body["warnings"]] == ["digest_overlap"]
    assert body["active_count"] == 0 and body["active_cap"] == 10 and body["project"] is None
    assert EM_DASH not in res.text
    writes = [c for c in api.db.calls[calls_before:] if c[0] != "select"]
    assert writes == [] and api.db.tables["assistant_schedules"] == []


def test_an_every_two_weeks_card_runs_on_the_weeks_it_showed(api):
    """SCHED-5: drafted Sunday 23:30 PT, approved Monday 00:10 PT (inside the
    card's hour). The draft's week anchor sets the parity, so the routine runs
    on the dates the card listed, not the alternate weeks."""
    sunday = datetime(2026, 11, 1, 23, 30, tzinfo=rrules.PT).astimezone(timezone.utc)
    api.clock["now"] = sunday
    draft = _draft(api, frequency="weekly", days="MO", interval="2").json()
    assert draft["ok"] is True, draft
    shown = [datetime.fromisoformat(t).astimezone(rrules.PT).date().isoformat() for t in draft["next_runs"]]
    assert shown == ["2026-11-09", "2026-11-23", "2026-12-07"]
    anchor = datetime.fromisoformat(draft["body"]["anchor"]).astimezone(rrules.PT)
    assert (anchor.date().isoformat(), anchor.hour, anchor.minute) == ("2026-10-26", 0, 0)
    api.clock["now"] = datetime(2026, 11, 2, 0, 10, tzinfo=rrules.PT).astimezone(timezone.utc)
    _as_card(api)
    res = _create(api, body=draft["body"])
    assert res.status_code == 201, res.text
    row = api.db.tables["assistant_schedules"][0]
    stored = [datetime.fromisoformat(row[k]).astimezone(rrules.PT).date().isoformat()
              for k in ("next_run_at", "after_next_run_at")]
    assert stored == shown[:2]


def test_an_anchor_from_the_future_or_long_ago_is_refused(api):
    _as_card(api)
    body = {"recipe": "due_today", "delivery": "chat_bell", "only_if_changed": False,
            "horizon_days": None, "project_id": None,
            "cadence": {**WEEKDAYS_7, "frequency": "weekly", "days": ["MO"], "interval": 2}}
    for anchor in (NOW + timedelta(days=1), NOW - timedelta(days=30)):
        res = _create(api, body={**body, "anchor": anchor.isoformat()})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_cadence"
    assert api.db.tables["assistant_schedules"] == []


def test_draft_refusals_come_back_inside_ok_false(api, monkeypatch):
    cases = [
        ({"hour": None}, "bad_cadence"),
        ({"frequency": "every_minute"}, "bad_cadence"),
        ({"days": "MO,XX", "frequency": "weekly"}, "bad_cadence"),
        ({"recipe": "nope"}, "recipe_not_found"),
        ({"horizon_days": "5"}, "bad_params"),
        ({"horizon_days": "lots", "recipe": "missing_quotes"}, "bad_params"),
        ({"delivery": "sms"}, "bad_params"),
        ({"only_if_changed": "perhaps"}, "bad_params"),
    ]
    for params, code in cases:
        body = _draft(api, **params).json()
        assert body["ok"] is False and body["code"] == code and body["message"], (params, body)
        assert EM_DASH not in body["message"]
    api.data.watch = "missing"
    body = _draft(api, recipe="missing_quotes", project_id=PROJECT).json()
    assert body["ok"] is False and body["code"] == "watch_project_closed"
    monkeypatch.setattr(routines, "enabled_map", lambda role: {"bidding": False})
    body = _draft(api).json()
    assert body["ok"] is False and body["code"] == "recipe_unavailable"
    assert api.db.tables["assistant_schedules"] == []


def test_draft_warnings_per_recipe_and_role_and_the_watch_project(api):
    body = _draft(api, recipe="due_this_week").json()
    assert [w["code"] for w in body["warnings"]] == ["reminders_overlap"]
    body = _draft(api, recipe="missing_quotes", horizon_days="7").json()
    assert body["ok"] and body["body"]["horizon_days"] == 7
    assert [w["code"] for w in body["warnings"]] == ["reminders_overlap"]
    body = _draft(api, recipe="labor_not_entered", project_id=PROJECT).json()
    assert body["warnings"] == [] and body["project"] == {"id": PROJECT, "number": "26.10.7267",
                                                          "name": "Alpha Clinic"}
    assert body["title"].startswith("Labor not entered on 26.10.7267")
    api.state.role = Role.ACCOUNTANT                    # no digest, so no digest notice
    assert _draft(api).json()["warnings"] == []
    api.state.role = Role.EXECUTIVE
    for i in range(9):
        _sched(api, id=f"ffffffff-ffff-4fff-8fff-{i:012d}")
    body = _draft(api).json()
    assert body["active_count"] == 9 and [w["code"] for w in body["warnings"]] == ["digest_overlap", "cap_near"]


def test_draft_of_a_weekday_rule_skips_the_holidays(api):
    api.clock["now"] = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)       # Wed noon PT
    api.db.tables["assistant_settings"][0]["holidays"] = ["2026-11-26", "2026-11-27"]
    runs = [datetime.fromisoformat(t).astimezone(rrules.PT).date().isoformat()
            for t in _draft(api).json()["next_runs"]]
    assert runs == ["2026-11-30", "2026-12-01", "2026-12-02"]


# ── reads ────────────────────────────────────────────────────────────────────


def test_list_get_and_status_filter(api):
    _sched(api)
    _sched(api, id=SCHEDULE2, status="paused", paused_reason="user", created_at=NOW.isoformat())
    _sched(api, id="ffffffff-ffff-4fff-8fff-fffffffffff3", status="ended", ended_at=NOW.isoformat(),
           ended_reason="user")
    _sched(api, id="ffffffff-ffff-4fff-8fff-fffffffffff4", user_id=OTHER_USER)
    items = api.client.get("/assistant/schedules").json()["items"]
    assert {i["id"] for i in items} == {SCHEDULE, SCHEDULE2, "ffffffff-ffff-4fff-8fff-fffffffffff3"}
    assert [i["id"] for i in api.client.get("/assistant/schedules?status=paused").json()["items"]] == [SCHEDULE2]
    assert [i["id"] for i in api.client.get("/assistant/schedules?status=active").json()["items"]] == [SCHEDULE]
    assert api.client.get("/assistant/schedules?status=weird").status_code == 422
    one = api.client.get(f"/assistant/schedules/{SCHEDULE}").json()
    assert one["cadence"] == WEEKDAYS_7 and len(one["next_runs"]) == 3 and one["last_run"] is None
    paused = api.client.get(f"/assistant/schedules/{SCHEDULE2}").json()
    assert paused["next_runs"] == [] and paused["paused_reason"] == "user"
    assert api.client.get("/assistant/schedules/nope").status_code == 404
    res = api.client.get("/assistant/schedules/ffffffff-ffff-4fff-8fff-fffffffffff4")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "schedule_not_found"


def test_runs_page_with_a_cursor_and_the_last_run(api):
    _sched(api)
    for i in range(3):
        api.db.tables["assistant_schedule_runs"].append(schedule_run(
            id=f"abababab-abab-4bab-8bab-{i:012d}", status="delivered",
            scheduled_for=(NOW - timedelta(days=i)).isoformat(), created_at=(NOW - timedelta(days=i)).isoformat()))
    page = api.client.get(f"/assistant/schedules/{SCHEDULE}/runs?limit=2").json()
    assert [r["id"][-1] for r in page["items"]] == ["0", "1"] and page["next_before"]
    rest = api.client.get(f"/assistant/schedules/{SCHEDULE}/runs",
                          params={"limit": 2, "before": page["next_before"]}).json()
    assert [r["id"][-1] for r in rest["items"]] == ["2"] and rest["next_before"] is None
    last = api.client.get(f"/assistant/schedules/{SCHEDULE}").json()["last_run"]
    assert last["id"].endswith("0") and last["status"] == "delivered"


def test_by_approval_is_the_assistants_own(api):
    _sched(api, approval_id=APPROVAL_ID)
    assert api.client.get(f"/assistant/schedules/by-approval/{APPROVAL_ID}").status_code == 404   # human
    _as_card(api)
    res = api.client.get(f"/assistant/schedules/by-approval/{APPROVAL_ID}")
    assert res.status_code == 200 and res.json()["id"] == SCHEDULE
    assert api.client.get(f"/assistant/schedules/by-approval/{OTHER_APPROVAL}").status_code == 404
    assert api.client.get("/assistant/schedules/by-approval/nope").status_code == 404


def test_seen_clears_the_callers_rows_only(api):
    _sched(api, unread_deliveries=4)
    _sched(api, id=SCHEDULE2, user_id=OTHER_USER, unread_deliveries=2)
    res = api.client.post("/assistant/schedules/seen", json={"ids": [SCHEDULE, SCHEDULE2]})
    assert res.status_code == 200 and res.json() == {"updated": 1}
    rows = {r["id"]: r for r in api.db.tables["assistant_schedules"]}
    assert rows[SCHEDULE]["unread_deliveries"] == 0 and rows[SCHEDULE]["last_seen_at"]
    assert rows[SCHEDULE2]["unread_deliveries"] == 2
    assert api.client.post("/assistant/schedules/seen", json={"ids": ["nope"]}).status_code == 422
    assert api.client.post("/assistant/schedules/seen", json={"ids": []}).status_code == 422


# ── the human transitions and their fences ───────────────────────────────────


def test_pause_resume_end(api):
    _sched(api, consecutive_failures=2, unread_deliveries=5)
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/pause")
    assert res.status_code == 200 and res.json()["status"] == "paused" and res.json()["paused_reason"] == "user"
    row = api.db.tables["assistant_schedules"][0]
    assert row["paused_at"] is not None
    # Pausing a paused row answers it as it is.
    assert api.client.post(f"/assistant/schedules/{SCHEDULE}/pause").json()["status"] == "paused"
    api.clock["now"] = NOW + timedelta(days=3)
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/resume")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "active" and res.json()["paused_reason"] is None
    assert row["paused_at"] is None and row["consecutive_failures"] == 0 and row["unread_deliveries"] == 0
    assert datetime.fromisoformat(row["next_run_at"]) > NOW + timedelta(days=3)
    assert datetime.fromisoformat(row["after_next_run_at"]) > datetime.fromisoformat(row["next_run_at"])
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/end")
    assert res.status_code == 200 and res.json()["status"] == "ended" and res.json()["ended_reason"] == "user"
    assert row["ended_at"] is not None
    assert api.client.post(f"/assistant/schedules/{SCHEDULE}/end").status_code == 200        # idempotent
    for action in ("pause", "resume"):
        res = api.client.post(f"/assistant/schedules/{SCHEDULE}/{action}")
        assert res.status_code == 409 and res.json()["detail"]["code"] == "schedule_state_changed"
        assert res.json()["detail"]["schedule"]["status"] == "ended"


def test_a_lost_fence_is_409_with_the_current_row(api, monkeypatch):
    _sched(api)
    stale = dict(api.db.tables["assistant_schedules"][0])
    real_get = repo.get_schedule
    calls = {"n": 0}

    def get(user_id, schedule_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # The route read the row as active; a worker ended it since.
            api.db.tables["assistant_schedules"][0].update(status="ended", ended_at=NOW.isoformat())
            return stale
        return real_get(user_id, schedule_id)

    monkeypatch.setattr(repo, "get_schedule", get)
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/pause")
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "schedule_state_changed"
    assert res.json()["detail"]["schedule"]["status"] == "ended"
    assert api.db.tables["assistant_schedules"][0]["status"] == "ended"


def test_resume_needs_the_sub_app_and_room_under_the_cap(api, monkeypatch):
    _sched(api, status="paused_feature_off", paused_at=NOW.isoformat())
    monkeypatch.setattr(routines, "enabled_map", lambda role: {"bidding": False})
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/resume")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"
    assert api.db.tables["assistant_schedules"][0]["status"] == "paused_feature_off"
    monkeypatch.setattr(routines, "enabled_map", lambda role: {"bidding": True})
    for i in range(10):
        _sched(api, id=f"ffffffff-ffff-4fff-8fff-{i:012d}")
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/resume")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "schedule_cap_reached"
    assert api.db.tables["assistant_schedules"][0]["status"] == "paused_feature_off"


def test_human_routes_refuse_an_assistant_principal(api):
    _sched(api)
    _as_card(api, None)
    for action in ("pause", "resume", "end", "run-now"):
        res = api.client.post(f"/assistant/schedules/{SCHEDULE}/{action}")
        assert res.status_code == 403, action
    assert api.client.post("/assistant/schedules/seen", json={"ids": [SCHEDULE]}).status_code == 403
    assert api.db.tables["assistant_schedules"][0]["status"] == "active"
    assert api.db.tables["assistant_schedule_runs"] == []


# ── Run now ──────────────────────────────────────────────────────────────────


def test_run_now_inserts_a_claimed_row_with_no_worker(api):
    _sched(api)
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now")
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["kind"] == "run_now" and body["status"] == "claimed" and body["schedule_id"] == SCHEDULE
    run = api.db.tables["assistant_schedule_runs"][0]
    assert run["claimed_by"] is None and run["user_label"] == "Tom Moore"
    assert datetime.fromisoformat(run["scheduled_for"]).microsecond != 0
    # next_run_at never moves.
    assert api.db.tables["assistant_schedules"][0]["next_run_at"] == schedule()["next_run_at"]


def test_run_now_refusals(api, monkeypatch):
    _sched(api, status="paused", paused_reason="failures")
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "schedule_not_active"
    api.db.tables["assistant_schedules"][0].update(paused_reason="user")
    # A run this schedule made a minute ago.
    api.db.tables["assistant_schedule_runs"].append(schedule_run(
        status="delivered", created_at=(NOW - timedelta(minutes=1)).isoformat()))
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now")
    assert res.status_code == 429 and res.json()["detail"]["code"] == "run_now_too_soon"
    # A run a worker holds, older than the spacing but still open.
    api.db.tables["assistant_schedule_runs"][0].update(status="running",
                                                       created_at=(NOW - timedelta(minutes=12)).isoformat())
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "schedule_busy"
    api.db.tables["assistant_schedule_runs"][0].update(status="delivered")
    assert api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now").status_code == 202
    monkeypatch.setattr(get_settings(), "assistant_scheduler_enabled", False)
    res = api.client.post(f"/assistant/schedules/{SCHEDULE2}/run-now")
    assert res.status_code == 404
    _sched(api, id=SCHEDULE2)
    res = api.client.post(f"/assistant/schedules/{SCHEDULE2}/run-now")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "scheduler_off"


def test_a_click_at_the_top_of_the_minute_never_takes_a_slots_key(api):
    top = NOW.replace(minute=0, second=0, microsecond=0)
    api.clock["now"] = top
    api.db.now = top
    _sched(api, next_run_at=top.isoformat(), after_next_run_at=(top + timedelta(days=1)).isoformat())
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now")
    assert res.status_code == 202, res.text
    assert res.json()["scheduled_for"] == (top + timedelta(microseconds=1)).isoformat()
    # The scheduled slot for hh:00:00 claims without a 23505; it is skipped
    # as an overlap because the Run now row is open.
    api.db.now = top + timedelta(seconds=30)
    claimed = repo.claim_due_schedules(None, worker="w", limit=5, catchup_seconds=10800)
    scheduled = [r for r in claimed if r["kind"] == "scheduled"]
    assert len(scheduled) == 1 and scheduled[0]["scheduled_for"] == top.isoformat()
    assert scheduled[0]["status"] == "skipped" and scheduled[0]["error_kind"] == "overlap"
    assert len(api.db.tables["assistant_schedule_runs"]) == 2


def test_two_clicks_in_the_same_microsecond_are_429(api, monkeypatch):
    _sched(api)

    def collide(user_id, row):
        raise repo.RunExists("assistant_schedule_runs_occurrence_uq")

    monkeypatch.setattr(repo, "insert_run", collide)
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now")
    assert res.status_code == 429 and res.json()["detail"]["code"] == "run_now_too_soon"


def test_an_overlapping_run_now_is_409_schedule_busy(api, monkeypatch):
    """SCHED-1: two requests that both passed open_run and the spacing check;
    the pending Run now index (0176 A3b) refuses the second insert."""
    _sched(api)
    monkeypatch.setattr(repo, "open_run", lambda user_id, schedule_id: None)
    monkeypatch.setattr(repo, "last_run", lambda user_id, schedule_id: None)
    assert api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now").status_code == 202
    api.clock["now"] = api.clock["now"] + timedelta(milliseconds=3)      # another occurrence key
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/run-now")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "schedule_busy"
    assert len(api.db.tables["assistant_schedule_runs"]) == 1


# ── the housekeeping tool routes ─────────────────────────────────────────────


@pytest.mark.parametrize("action", ["pause", "resume", "end", "run-now"])
def test_tool_routes_404_a_human(api, action):
    _sched(api)
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/{action}", headers={HEADER: KEY})
    assert res.status_code == 404
    assert api.db.tables["assistant_schedules"][0]["status"] == "active"


def test_tool_routes_need_the_begun_row_of_their_own_tool(api):
    _sched(api)
    headers = _begun(api, "schedule_pause")
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/end", headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "turn_context_unavailable"
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/pause")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "turn_context_unavailable"
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/pause", headers={HEADER: "another-key"})
    assert res.status_code == 409
    assert api.db.tables["assistant_schedules"][0]["status"] == "active"
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/pause", headers=headers)
    assert res.status_code == 200 and res.json()["status"] == "paused" and res.json()["paused_reason"] == "user"


def test_tool_end_ends_in_an_untainted_turn(api):
    _sched(api)
    headers = _begun(api, "schedule_end")
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/end", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "ended" and res.json()["ended_reason"] == "user" and res.json()["note"] is None
    # The same step again answers the ended row.
    again = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/end", headers=headers)
    assert again.status_code == 200 and again.json()["status"] == "ended"


@pytest.mark.parametrize("taint", ["begun", "conversation"])
def test_tool_end_in_a_tainted_turn_pauses_instead(api, taint):
    _sched(api)
    headers = _begun(api, "schedule_end", tainted=taint == "begun", conv_tainted=taint == "conversation")
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/end", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "paused" and body["paused_reason"] == "user"
    assert body["note"] == "Paused. End it from Routines to stop it for good."
    row = api.db.tables["assistant_schedules"][0]
    assert row["status"] == "paused" and row["ended_at"] is None


def test_tool_resume_and_run_now_are_refused_while_tainted(api):
    _sched(api, status="paused", paused_reason="user")
    headers = _begun(api, "schedule_resume", tainted=True)
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/resume", headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "tainted_turn"
    headers = _begun(api, "schedule_run_now", tainted=True, key="ledger-key-2")
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/run-now", headers=headers)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "tainted_turn"
    assert api.db.tables["assistant_schedules"][0]["status"] == "paused"
    assert api.db.tables["assistant_schedule_runs"] == []
    # Untainted, both work.
    headers = _begun(api, "schedule_run_now", key="ledger-key-3")
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/run-now", headers=headers)
    assert res.status_code == 202 and res.json()["kind"] == "run_now"
    headers = _begun(api, "schedule_resume", key="ledger-key-4")
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/resume", headers=headers)
    assert res.status_code == 200 and res.json()["status"] == "active"


def test_tool_pause_is_allowed_in_a_tainted_turn(api):
    _sched(api)
    headers = _begun(api, "schedule_pause", tainted=True)
    res = api.client.post(f"/assistant/tools/schedules/{SCHEDULE}/pause", headers=headers)
    assert res.status_code == 200 and res.json()["status"] == "paused"


# ── previews ─────────────────────────────────────────────────────────────────


def test_previews_write_nothing_and_redact_per_role(api):
    _sched(api, delivery="chat_bell_email")
    before = {k: [dict(r) for r in v] for k, v in api.db.tables.items()}
    res = api.client.get(f"/assistant/schedules/{SCHEDULE}/preview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schedule_id"] == SCHEDULE and body["recipe"] == "due_today" and body["rows"] == 1
    assert body["note"] == "Preview only. Nothing was sent or saved." and body["suppressed"] is None
    assert body["content"] == "Bids due today: 1. Missing vendor quotes: 1. Missing labor: 0. Missing markups: 0."
    assert "Alpha Clinic" not in body["content"]
    assert body["would_deliver"]["chat"] is True and body["would_deliver"]["bell"] is True
    assert {k: [dict(r) for r in v] for k, v in api.db.tables.items()} == before
    res = api.client.get("/assistant/schedules/preview", params={"recipe": "missing_quotes", "horizon_days": "3"})
    assert res.status_code == 200 and res.json()["schedule_id"] is None and res.json()["rows"] == 1
    assert api.data.calls[-1][0] == "rows_for" and api.data.calls[-1][3] == 3
    assert api.client.get("/assistant/schedules/preview", params={"recipe": "nope"}).status_code == 404
    res = api.client.get("/assistant/schedules/preview", params={"recipe": "due_today", "horizon_days": "3"})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_params"


def test_preview_reports_the_digest_stub_and_an_empty_result(api):
    _sched(api)
    api.data.digest = True
    body = api.client.get(f"/assistant/schedules/{SCHEDULE}/preview").json()
    assert body["suppressed"] == "digest_sent" and not any(body["would_deliver"].values())
    api.state.role = Role.ACCOUNTANT                     # never gets the digest
    assert api.client.get(f"/assistant/schedules/{SCHEDULE}/preview").json()["suppressed"] is None
    api.state.role = Role.EXECUTIVE
    api.data.digest = False
    api.data.rows = []
    body = api.client.get(f"/assistant/schedules/{SCHEDULE}/preview").json()
    assert body["content"] == "No bids are due today." and body["would_deliver"] == {
        "chat": True, "bell": False, "email": False}


def test_preview_with_bidding_off_is_409(api, monkeypatch):
    _sched(api)
    monkeypatch.setattr(routines, "enabled_map", lambda role: {"bidding": False})
    res = api.client.get(f"/assistant/schedules/{SCHEDULE}/preview")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"


# ── the recipe rows route ────────────────────────────────────────────────────


def test_recipe_rows_redact_per_role_and_report_the_digest(api):
    res = api.client.get("/assistant/recipes/due_today/rows")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["recipe"] == "due_today" and body["digest_sent_today"] is False
    assert body["rows"][0]["project"]["actual_bid_at"] == NOW.isoformat()      # an Executive sees it
    api.state.role = Role.ESTIMATING_ENGINEER_LABOR
    body = api.client.get("/assistant/recipes/due_today/rows").json()
    assert body["rows"][0]["project"]["actual_bid_at"] is None
    api.data.digest = True
    assert api.client.get("/assistant/recipes/due_today/rows").json()["digest_sent_today"] is True
    assert api.client.get("/assistant/recipes/missing_quotes/rows").json()["digest_sent_today"] is False


def test_recipe_rows_errors_and_the_at_clamp(api, monkeypatch):
    res = api.client.get("/assistant/recipes/nope/rows")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "recipe_not_found"
    res = api.client.get("/assistant/recipes/due_today/rows", params={"horizon_days": "4"})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "bad_params"
    res = api.client.get("/assistant/recipes/missing_quotes/rows", params={"horizon_days": "x"})
    assert res.status_code == 422
    far = (NOW + timedelta(days=5)).isoformat()
    api.client.get("/assistant/recipes/missing_quotes/rows", params={"at": far, "horizon_days": "3"})
    assert api.data.calls[-1][2] == NOW + timedelta(hours=24)
    api.client.get("/assistant/recipes/missing_quotes/rows", params={"at": (NOW - timedelta(days=2)).isoformat()})
    assert api.data.calls[-1][2] == NOW - timedelta(hours=24)
    assert api.client.get("/assistant/recipes/due_today/rows", params={"at": "soon"}).status_code == 422
    monkeypatch.setattr(get_settings(), "bidding_enabled", False)
    assert api.client.get("/assistant/recipes/due_today/rows").status_code == 404


def test_the_scheduler_reads_the_rows_as_the_owners_assistant(api):
    _as_card(api, None)
    res = api.client.get("/assistant/recipes/labor_not_entered/rows", params={"project_id": PROJECT})
    assert res.status_code == 200 and res.json()["watch"] == {"project_id": PROJECT, "state": "open"}


# ── holidays ─────────────────────────────────────────────────────────────────


def test_holidays_role_gate_and_assistant_refusal(api):
    api.state.role = Role.ESTIMATING_ADMIN
    assert api.client.get("/assistant/admin/holidays").status_code == 403
    assert api.client.put("/assistant/admin/holidays", json={"holidays": []}).status_code == 403
    api.state.role = Role.IT_ADMIN
    assert api.client.get("/assistant/admin/holidays").status_code == 200
    _as_card(api, None)
    assert api.client.get("/assistant/admin/holidays").status_code == 403
    assert api.client.put("/assistant/admin/holidays", json={"holidays": []}).status_code == 403


def test_holidays_put_validates_sorts_and_audits(api):
    api.clock["now"] = datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc)
    api.db.tables["assistant_settings"][0]["holidays"] = ["2026-12-25"]
    res = api.client.put("/assistant/admin/holidays", json={"holidays": ["2027-01-01", "2026-11-26"]})
    assert res.status_code == 200, res.text
    assert res.json()["holidays"] == ["2026-11-26", "2027-01-01"]
    assert api.db.tables["assistant_settings"][0]["holidays"] == ["2026-11-26", "2027-01-01"]
    assert api.db.tables["assistant_settings"][0]["updated_by"] == USER
    audit = api.db.tables["audit_log"][-1]
    assert audit["action"] == "assistant.holidays_updated" and audit["entity_id"] == "1"
    assert audit["payload"] == {"added": ["2026-11-26", "2027-01-01"], "removed": ["2026-12-25"], "count": 2}
    assert api.client.get("/assistant/admin/holidays").json()["holidays"] == ["2026-11-26", "2027-01-01"]
    for bad in (["2026-11-26", "2026-11-26"], ["11/26/2026"], ["2026-02-30"], ["2030-01-01"], ["2024-01-01"],
                [f"2027-01-{d:02d}" for d in range(1, 32)] + [f"2027-03-{d:02d}" for d in range(1, 31)]):
        res = api.client.put("/assistant/admin/holidays", json={"holidays": bad})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "holidays_invalid", bad
    assert api.db.tables["assistant_settings"][0]["holidays"] == ["2026-11-26", "2027-01-01"]
    assert api.client.put("/assistant/admin/holidays", json={"holidays": []}).json()["holidays"] == []


def test_switched_off_assistant_gives_a_bare_404(api):
    _sched(api)
    api.db.tables["assistant_settings"][0]["enabled"] = False
    for path in ("/assistant/schedules", f"/assistant/schedules/{SCHEDULE}", "/assistant/schedules/draft",
                 "/assistant/recipes/due_today/rows"):
        res = api.client.get(path)
        assert res.status_code == 404 and res.json() == {"detail": "Not Found"}, path


# ── phase 4: the coded recipes (docs/ASSISTANT_PHASE4_PLAN.md 3.10.3) ──────

NON_VIEWER = Role.ESTIMATING_ENGINEER_LABOR


def _phase4(api):
    """The fake's recipe check as 0180 B widens it (the fake still lists
    the four phase 3 recipes)."""
    api.db.RECIPES = (*type(api.db).RECIPES, "estimator_vs_bid", "quote_readiness")


def _evb_row():
    return _row(extra={"estimator_base": "100000.00", "estimator_name": "Sam", "round": 2, "our_min": "120000.00",
                       "gc_count": 3, "difference": "20000.00", "difference_pct": "20.0",
                       "verdict": "We bid higher", "code": None, "message": None, "revised_after_send": False,
                       "first_sent_at": NOW.isoformat()})


def _qr_row():
    return _row(extra={"bid_date": "Oct 23, 10:00 AM", "internal_due": "Oct 22, 2:00 PM", "key_quotes": "3 of 4",
                       "complete": False, "estimator_returned_at": None,
                       "categories": [{"name": "Gear", "covered": True, "actual_bid_at": NOW.isoformat(),
                                       "our_amount": "5.00"}]})


def test_recipe_rows_forward_period_week_and_role_and_carry_extra(api):
    api.data.rows = [_evb_row()]
    res = api.client.get("/assistant/recipes/estimator_vs_bid/rows", params={"period": "2026-09"})
    assert res.status_code == 200, res.text
    call = api.data.calls[-1]
    assert call[1] == "estimator_vs_bid" and call[5:] == ("2026-09", None, Role.EXECUTIVE)
    extra = res.json()["rows"][0]["extra"]
    assert extra["our_min"] == "120000.00" and extra["difference"] == "20000.00"
    api.data.rows = [_qr_row()]
    res = api.client.get("/assistant/recipes/quote_readiness/rows", params={"week": "next_week"})
    assert res.status_code == 200 and api.data.calls[-1][5:] == (None, "next_week", Role.EXECUTIVE)
    assert res.json()["rows"][0]["extra"]["bid_date"] == "Oct 23, 10:00 AM"
    # The phase 3 recipes carry no extra.
    api.data.rows = [_row()]
    assert api.client.get("/assistant/recipes/due_today/rows").json()["rows"][0]["extra"] is None


def test_estimator_vs_bid_rows_serve_the_month_totals_and_sent_early(api):
    # DATA-5: the like-for-like totals and the sent-early bids reach the tool.
    api.data.rows = [_evb_row()]
    totals = {"like_for_like": {"count": 1, "estimator_total": "100000.00", "our_total": "120000.00",
                                "difference": "20000.00", "difference_pct": "20.0"},
              "all_rows": {"count": 1, "estimator_total": "100000.00", "our_total": "120000.00", "note": "n"}}
    early = {**_evb_row(), "project": {**_evb_row()["project"], "actual_bid_at": NOW.isoformat()}}
    api.data.more = {"totals": totals, "extra": {"sent_early": [early]}}
    body = api.client.get("/assistant/recipes/estimator_vs_bid/rows").json()
    assert body["totals"] == totals
    assert len(body["sent_early"]) == 1 and body["sent_early"][0]["extra"]["our_min"] == "120000.00"
    # A non-viewer (behind the 409 gate anyway) never gets an amount.
    from app.routers import assistant_routines as routines

    class _U:
        role = NON_VIEWER

    redacted = routines._redacted(_U(), {"rows": [], "extra": {"sent_early": [early]}})
    assert "our_min" not in redacted["sent_early"][0]["extra"]
    assert redacted["sent_early"][0]["project"]["actual_bid_at"] is None
    # The other recipes serve neither.
    api.data.more = {}
    plain = api.client.get("/assistant/recipes/due_today/rows").json()
    assert plain["totals"] is None and plain["sent_early"] is None


def test_recipe_rows_refuse_a_role_outside_the_recipe(api):
    api.state.role = NON_VIEWER
    calls = len(api.data.calls)
    res = api.client.get("/assistant/recipes/estimator_vs_bid/rows")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"
    assert len(api.data.calls) == calls
    api.state.role = Role.ACCOUNTANT                     # an amount viewer
    assert api.client.get("/assistant/recipes/estimator_vs_bid/rows").status_code == 200


def test_recipe_rows_bad_period_and_week(api):
    for path, params, code in (
        ("estimator_vs_bid", {"period": "2026-13"}, "period_invalid"),
        ("quote_readiness", {"week": "2026-10-21"}, "week_invalid"),           # a Wednesday
        ("quote_readiness", {"period": "last_month"}, "bad_params"),
        ("due_today", {"week": "next_week"}, "bad_params"),
    ):
        res = api.client.get(f"/assistant/recipes/{path}/rows", params=params)
        assert res.status_code == 422 and res.json()["detail"]["code"] == code, (path, params)
    assert api.client.get("/assistant/recipes/quote_readiness/rows",
                          params={"week": "x" * 11}).status_code == 422


def test_a_data_side_role_refusal_is_409(api, monkeypatch):
    from app.assistant import recipes

    def refuse(*a, **k):
        raise recipes.RecipeError("no", code="recipe_unavailable")

    monkeypatch.setattr(api.data, "rows_for", refuse)
    res = api.client.get("/assistant/recipes/estimator_vs_bid/rows")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"


def test_redacted_strips_extra_amounts_for_non_viewers(api):
    api.state.role = NON_VIEWER
    api.data.rows = [_qr_row(), {**_evb_row(), "extra": {**_evb_row()["extra"], "nested": {"our_amount": "9.00"}}}]
    rows = api.client.get("/assistant/recipes/quote_readiness/rows").json()["rows"]
    first, second = rows[0]["extra"], rows[1]["extra"]
    assert "bid_date" not in first and first["key_quotes"] == "3 of 4"
    assert first["categories"] == [{"name": "Gear", "covered": True}]
    for key in ("our_min", "difference", "difference_pct"):
        assert key not in second
    assert second["nested"] == {} and second["estimator_name"] == "Sam"
    assert rows[0]["project"]["actual_bid_at"] is None
    # A viewer keeps every key.
    api.state.role = Role.EXECUTIVE
    rows = api.client.get("/assistant/recipes/quote_readiness/rows").json()["rows"]
    assert rows[0]["extra"]["bid_date"] and rows[1]["extra"]["our_min"] == "120000.00"


def test_draft_and_create_carry_period_and_week(api):
    _phase4(api)
    res = _draft(api, recipe="estimator_vs_bid", frequency="monthly", day_of_month="2", hour="7", period="last_month")
    body = res.json()
    assert body["ok"] is True, body
    assert body["body"]["period"] == "last_month" and body["body"]["week"] is None
    res = _draft(api, recipe="quote_readiness", week="2026-10-26")
    assert res.json()["ok"] is True and res.json()["body"]["week"] == "2026-10-26"
    res = _draft(api, recipe="quote_readiness", week="someday")
    assert res.json()["ok"] is False and res.json()["code"] == "week_invalid"
    _as_card(api)
    res = _create(api, {"recipe": "quote_readiness", "cadence": WEEKDAYS_7, "week": "next_week"})
    assert res.status_code == 201, res.text
    assert api.db.tables["assistant_schedules"][0]["params"] == {"week": "next_week"}


def test_recipe_roles_are_enforced_in_plan_resume_and_preview(api):
    _phase4(api)
    api.state.role = NON_VIEWER
    res = _draft(api, recipe="estimator_vs_bid", period="last_month")
    assert res.json()["ok"] is False and res.json()["code"] == "recipe_unavailable"
    _as_card(api)
    res = _create(api, {"recipe": "estimator_vs_bid", "cadence": WEEKDAYS_7, "period": "last_month"})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"
    assert api.db.tables["assistant_schedules"] == []
    _human(api)
    calls = len(api.data.calls)
    res = api.client.get("/assistant/schedules/preview", params={"recipe": "estimator_vs_bid"})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"
    # A routine made while the owner was a viewer: preview and resume refuse now.
    _sched(api, recipe="estimator_vs_bid", params={"period": "last_month"}, status="paused", paused_reason="user")
    res = api.client.get(f"/assistant/schedules/{SCHEDULE}/preview")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"
    res = api.client.post(f"/assistant/schedules/{SCHEDULE}/resume")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "recipe_unavailable"
    assert api.db.tables["assistant_schedules"][0]["status"] == "paused"
    assert len(api.data.calls) == calls
    # The viewer previews it with its month.
    api.state.role = Role.EXECUTIVE
    api.data.rows = [_evb_row()]
    res = api.client.get(f"/assistant/schedules/{SCHEDULE}/preview")
    assert res.status_code == 200, res.text
    assert api.data.calls[-1][5:] == ("last_month", None, Role.EXECUTIVE)
    assert res.json()["content"] == "1 bid compared; we bid higher on 1."
