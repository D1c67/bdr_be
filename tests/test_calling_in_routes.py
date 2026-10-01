"""Calling In routes (docs/CALLING_IN.md section 6) over the in-memory fake.

Pinned here:
  - every route answers the section 6 shape; reads admit the read-only
    Accountant, the external estimator gets 403;
  - writes are writer-only (the Accountant gets 403), edit is author-only,
    delete is Executive / IT Admin only;
  - log call: 409 when the round's window is closed for the GC or the round
    does not cover the GC, 422 for a contact of another GC or a missing
    note / contact / outcome; new contacts are created on the GC first;
  - the call that clears the last GC closes the list entry and dismisses its
    notifications inside the request; voicemail / no answer never do;
  - the actual bid date shows on Calling In to every internal role but the
    project call log keeps the normal redaction;
  - feature flag: every route 404s with Bidding off.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import calling_in as router_mod
from app.services import calling_in as ci
from tests.calling_in_fake import FakeSB

UTC = timezone.utc
PT = ci.PT

PID = "11111111-1111-1111-1111-111111111111"
OTHER_PID = "22222222-2222-2222-2222-222222222222"
GC_A = "aaaaaaaa-0000-0000-0000-000000000001"
GC_B = "aaaaaaaa-0000-0000-0000-000000000002"
GC_LATE = "aaaaaaaa-0000-0000-0000-000000000003"
GC_NEVER = "aaaaaaaa-0000-0000-0000-000000000004"
K_A1 = "cccccccc-0000-0000-0000-000000000001"
K_A2 = "cccccccc-0000-0000-0000-000000000002"
K_B1 = "cccccccc-0000-0000-0000-000000000003"
PG_A = "dddddddd-0000-0000-0000-000000000001"

USERS = {
    "exec": ("e0000000-0000-0000-0000-000000000001", Role.EXECUTIVE),
    "labor": ("e0000000-0000-0000-0000-000000000002", Role.ESTIMATING_ENGINEER_LABOR),
    "admin": ("e0000000-0000-0000-0000-000000000003", Role.ESTIMATING_ADMIN),
    "it": ("e0000000-0000-0000-0000-000000000004", Role.IT_ADMIN),
    "acct": ("e0000000-0000-0000-0000-000000000005", Role.ACCOUNTANT),
    "estimator": ("e0000000-0000-0000-0000-000000000006", Role.ESTIMATOR),
}


def pt(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=PT).astimezone(UTC)


T = pt(2026, 10, 6, 14)  # the actual bid time
SENT = pt(2026, 9, 28, 9)
NOW_PRE = pt(2026, 10, 1, 10)  # List 1
NOW_POST = pt(2026, 10, 8, 10)  # List 2, day 2


def _tables():
    return {
        "projects": [
            {"id": PID, "name": "ZZ TEST Main St", "number": "4412",
             "actual_bid_at": T.isoformat(), "abandoned_at": None,
             "current_stage": "submitted", "test_session_id": None},
            {"id": OTHER_PID, "name": "Unsent", "number": "4413",
             "actual_bid_at": T.isoformat(), "abandoned_at": None,
             "current_stage": "verify", "test_session_id": None},
        ],
        "proposal_sends": [
            {"id": "ps-a", "project_id": PID, "gc_id": GC_A, "gc_name": "Acme (old)",
             "status": "sent", "sent_at": SENT.isoformat(), "sent_via": "email",
             "material_amount": "1000.00", "gear_amount": None, "underground_amount": None,
             "low_voltage_amount": None, "labor_amount": "400.00"},
            {"id": "ps-b", "project_id": PID, "gc_id": GC_B, "gc_name": "Brite",
             "status": "sent", "sent_at": SENT.isoformat(), "sent_via": "external",
             "material_amount": "900.00", "gear_amount": "50.00", "underground_amount": None,
             "low_voltage_amount": None, "labor_amount": "400.00"},
            {"id": "ps-late", "project_id": PID, "gc_id": GC_LATE, "gc_name": "Late Co",
             "status": "sent", "sent_at": (T + timedelta(hours=3)).isoformat(),
             "sent_via": "email", "material_amount": "1", "labor_amount": "1"},
            {"id": "ps-gen", "project_id": PID, "gc_id": GC_NEVER, "gc_name": "Never",
             "status": "generated", "sent_at": None, "sent_via": "email"},
        ],
        "general_contractors": [
            {"id": GC_A, "name": "Acme"}, {"id": GC_B, "name": "Brite"},
            {"id": GC_LATE, "name": "Late Co"}, {"id": GC_NEVER, "name": "Never"},
        ],
        "gc_contacts": [
            {"id": K_A1, "gc_id": GC_A, "name": "Zed Estimator", "email": "zed@acme.test",
             "phone": "702-555-0101"},
            {"id": K_A2, "gc_id": GC_A, "name": "Amy PM", "email": None, "phone": "702-555-0102"},
            {"id": K_B1, "gc_id": GC_B, "name": "Bo", "email": "bo@brite.test", "phone": None},
        ],
        "project_gcs": [{"id": PG_A, "project_id": PID, "gc_id": GC_A}],
        "project_gc_contacts": [{"project_gc_id": PG_A, "gc_contact_id": K_A1}],
        "bid_gc_outcomes": [{"project_id": PID, "gc_id": GC_B, "gc_award_result": "won"}],
        "bid_outcomes": [],
        "call_in_calls": [],
        "call_in_entries": [],
        "profiles": [
            {"id": uid, "full_name": f"{key.title()} Person"} for key, (uid, _) in USERS.items()
        ],
    }


@pytest.fixture
def env(monkeypatch):
    import app.main

    db = FakeSB(_tables())
    audits: list[tuple] = []
    dismissed: list[dict] = []
    clock = {"now": NOW_PRE}
    monkeypatch.setattr(ci, "get_supabase", lambda: db)
    monkeypatch.setattr(router_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(router_mod, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(ci, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(ci, "dismiss_notifications", lambda **kw: dismissed.append(kw))
    monkeypatch.setattr(ci, "_now", lambda: clock["now"])
    who = {"user": "exec"}

    def _user():
        uid, role = USERS[who["user"]]
        return CurrentUser(id=uid, email=f"{who['user']}@g3.test", role=role, is_active=True)

    app.main.app.dependency_overrides[get_current_user] = _user
    client = TestClient(app.main.app, raise_server_exceptions=True)
    yield {"db": db, "client": client, "audits": audits, "dismissed": dismissed,
           "clock": clock, "who": who}
    app.main.app.dependency_overrides.pop(get_current_user, None)


def _log(env, **body):
    payload = {"gc_id": GC_A, "round": "pre_bid", "outcome": "voicemail", "note": "Left a message",
               "contact_ids": [K_A1], **body}
    return env["client"].post(f"/calling-in/projects/{PID}/calls", json=payload)


# ── reads ────────────────────────────────────────────────────────────────────


def test_summary_and_list(env):
    c = env["client"]
    assert c.get("/calling-in/summary").json() == {"open_count": 1, "pre_bid": 1, "post_bid": 0}
    body = c.get("/calling-in").json()
    assert body["now"] == NOW_PRE.isoformat()
    assert body["post_bid"] == []
    (entry,) = body["pre_bid"]
    assert entry["project_id"] == PID and entry["project_number"] == "4412"
    assert entry["band"] == "open" and entry["bid_at"] == T.isoformat()
    # GC_LATE was sent after T: not on List 1. The generated row is never eligible.
    assert (entry["gcs_total"], entry["gcs_done"]) == (2, 0)


def test_actual_bid_date_is_visible_on_calling_in_to_every_internal_role(env):
    for who in ("labor", "acct", "admin"):
        env["who"]["user"] = who
        entry = env["client"].get("/calling-in").json()["pre_bid"][0]
        assert entry["bid_at"] == T.isoformat(), who


def test_estimator_is_refused_everywhere(env):
    env["who"]["user"] = "estimator"
    c = env["client"]
    for path in ("/calling-in/summary", "/calling-in", f"/calling-in/projects/{PID}",
                 f"/projects/{PID}/call-log", "/analytics/calling-in"):
        assert c.get(path).status_code == 403, path


def test_project_detail_shape(env):
    body = env["client"].get(f"/calling-in/projects/{PID}?round=pre_bid").json()
    assert body["entry"]["round"] == "pre_bid" and body["entry"]["on_list"] is True
    gcs = {g["gc_id"]: g for g in body["gcs"]}
    assert set(gcs) == {GC_A, GC_B}
    a, b = gcs[GC_A], gcs[GC_B]
    assert a["gc_name"] == "Acme"  # the live name, not the send-time snapshot
    assert a["proposal_send_id"] == "ps-a" and a["sent_via"] == "email"
    assert a["sent_at"] == SENT.isoformat()
    assert a["amounts"] == {"total": 1400.0, "sections": [
        {"key": "material", "label": "Material", "amount": 1000.0},
        {"key": "labor", "label": "Labor", "amount": 400.0},
    ]}
    assert b["amounts"]["total"] == 1350.0 and b["sent_via"] == "external"
    assert b["gc_outcome"] == "won" and a["gc_outcome"] is None
    assert a["done"] is False and a["done_at"] is None and a["window_open"] is True
    assert [c["id"] for c in a["contacts"]] == [K_A1, K_A2]  # project contact first
    assert a["contacts"][0]["is_project_contact"] is True
    assert a["contacts"][1]["is_project_contact"] is False
    assert a["contacts"][0] == {"id": K_A1, "name": "Zed Estimator", "phone": "702-555-0101",
                                "email": "zed@acme.test", "is_project_contact": True}
    assert a["calls"] == []


def test_project_detail_other_round_and_default_round(env):
    c = env["client"]
    post = c.get(f"/calling-in/projects/{PID}?round=post_bid").json()
    assert {g["gc_id"] for g in post["gcs"]} == {GC_A, GC_B, GC_LATE}
    assert post["entry"]["on_list"] is False
    assert all(g["window_open"] is False for g in post["gcs"])
    assert c.get(f"/calling-in/projects/{PID}").json()["entry"]["round"] == "pre_bid"
    assert c.get(f"/calling-in/projects/{PID}?round=bogus").status_code == 422


def test_project_detail_404s(env):
    c = env["client"]
    assert c.get(f"/calling-in/projects/{OTHER_PID}").status_code == 404  # nothing sent
    assert c.get("/calling-in/projects/33333333-3333-3333-3333-333333333333").status_code == 404
    assert c.get("/calling-in/projects/not-a-uuid").status_code == 404


# ── log call ─────────────────────────────────────────────────────────────────


def test_log_voicemail_keeps_the_gc_open(env):
    resp = _log(env)
    assert resp.status_code == 201
    call = resp.json()
    assert call["outcome"] == "voicemail" and call["round"] == "pre_bid"
    assert call["contacts"] == [{"gc_contact_id": K_A1, "name": "Zed Estimator",
                                 "phone": "702-555-0101", "email": "zed@acme.test"}]
    assert call["created_by"] == {"id": USERS["exec"][0], "name": "Exec Person"}
    assert call["can_edit"] is True and call["can_delete"] is True
    assert env["audits"][-1][1] == "call_in.log"
    detail = env["client"].get(f"/calling-in/projects/{PID}?round=pre_bid").json()
    a = next(g for g in detail["gcs"] if g["gc_id"] == GC_A)
    assert a["done"] is False and len(a["calls"]) == 1


def test_spoke_on_the_last_gc_closes_the_entry_in_the_request(env):
    db = env["db"]
    ci.poll_once(NOW_PRE)  # the poller claimed the List 1 entry
    (entry,) = db.tables["call_in_entries"]
    assert _log(env, gc_id=GC_A, outcome="spoke", contact_ids=[K_A1]).status_code == 201
    assert entry["closed_at"] is None  # GC_B is still open
    assert env["dismissed"] == []
    assert _log(env, gc_id=GC_B, outcome="no_answer", contact_ids=[K_B1]).status_code == 201
    assert entry["closed_at"] is None
    assert _log(env, gc_id=GC_B, outcome="spoke", contact_ids=[K_B1]).status_code == 201
    assert entry["closed_at"] is not None and entry["close_reason"] == "cleared"
    assert env["dismissed"] == [{"project_id": PID, "types": ["call_in_pre_bid"],
                                 "metadata_eq": {"entry_id": entry["id"]}}]
    assert env["client"].get("/calling-in/summary").json()["open_count"] == 0


def test_log_creates_new_contacts_on_the_gc_first(env):
    db = env["db"]
    resp = _log(env, contact_ids=[], new_contacts=[
        {"name": "  New Person ", "phone": "702-555-0199", "email": ""},
    ])
    assert resp.status_code == 201
    created = [c for c in db.tables["gc_contacts"] if c["name"] == "New Person"]
    assert len(created) == 1 and created[0]["gc_id"] == GC_A and created[0]["email"] is None
    assert resp.json()["contacts"][0]["gc_contact_id"] == created[0]["id"]


def test_log_422_for_a_contact_of_another_gc(env):
    db = env["db"]
    resp = _log(env, contact_ids=[K_B1], new_contacts=[{"name": "Should Not Exist"}])
    assert resp.status_code == 422
    assert not any(c["name"] == "Should Not Exist" for c in db.tables["gc_contacts"])
    assert db.tables["call_in_calls"] == []
    unknown = "cccccccc-0000-0000-0000-00000000ffff"
    assert _log(env, contact_ids=[unknown]).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"note": "   "},
        {"note": ""},
        {"contact_ids": [], "new_contacts": []},
        {"outcome": "busy"},
        {"outcome": None},
        {"round": "later"},
        {"new_contacts": [{"name": " "}], "contact_ids": []},
        {"new_contacts": [{"name": "X", "email": "not-an-email"}]},
        {"note": "x" * 4001},
    ],
)
def test_log_validation_422(env, body):
    assert _log(env, **body).status_code == 422


def test_log_409_when_the_window_is_closed(env):
    env["clock"]["now"] = NOW_POST
    resp = _log(env, round="pre_bid")
    assert resp.status_code == 409
    assert "window is closed" in resp.json()["detail"]
    env["clock"]["now"] = NOW_PRE
    assert _log(env, round="post_bid").status_code == 409


def test_log_409_when_the_round_does_not_cover_the_gc(env):
    env["clock"]["now"] = NOW_PRE
    # Sent after T: never on List 1.
    assert _log(env, gc_id=GC_LATE, contact_ids=[]).status_code in (409, 422)
    resp = _log(env, gc_id=GC_LATE, contact_ids=[], new_contacts=[{"name": "Lou"}])
    assert resp.status_code == 409
    # Generated, never sent: not eligible at all.
    resp = _log(env, gc_id=GC_NEVER, contact_ids=[], new_contacts=[{"name": "Nia"}])
    assert resp.status_code == 409
    assert not any(c["name"] in ("Lou", "Nia") for c in env["db"].tables["gc_contacts"])
    # ...but the late GC is callable on List 2.
    env["clock"]["now"] = NOW_POST
    resp = _log(env, gc_id=GC_LATE, round="post_bid", contact_ids=[],
                new_contacts=[{"name": "Lou"}])
    assert resp.status_code == 201


def test_log_404_for_an_unknown_project(env):
    resp = env["client"].post(
        "/calling-in/projects/33333333-3333-3333-3333-333333333333/calls",
        json={"gc_id": GC_A, "round": "pre_bid", "outcome": "spoke", "note": "x",
              "contact_ids": [K_A1]},
    )
    assert resp.status_code == 404


def test_log_409_on_an_abandoned_project(env):
    env["db"].tables["projects"][0]["abandoned_at"] = NOW_PRE.isoformat()
    assert _log(env).status_code == 409


def test_accountant_reads_but_cannot_write(env):
    env["who"]["user"] = "acct"
    c = env["client"]
    assert c.get("/calling-in").status_code == 200
    assert c.get(f"/calling-in/projects/{PID}").status_code == 200
    assert c.get(f"/projects/{PID}/call-log").status_code == 200
    assert _log(env).status_code == 403
    env["who"]["user"] = "exec"
    call_id = _log(env).json()["id"]
    env["who"]["user"] = "acct"
    assert c.patch(f"/calling-in/calls/{call_id}", json={"note": "x"}).status_code == 403
    assert c.delete(f"/calling-in/calls/{call_id}").status_code == 403


# ── edit / delete ────────────────────────────────────────────────────────────


def test_edit_is_author_only(env):
    env["who"]["user"] = "labor"
    call_id = _log(env).json()["id"]
    c = env["client"]
    for other in ("exec", "admin", "it"):
        env["who"]["user"] = other
        assert c.patch(f"/calling-in/calls/{call_id}", json={"note": "mine"}).status_code == 403
    env["who"]["user"] = "labor"
    resp = c.patch(f"/calling-in/calls/{call_id}", json={"note": "  Corrected  "})
    assert resp.status_code == 200
    assert resp.json()["note"] == "Corrected"
    row = env["db"].tables["call_in_calls"][0]
    assert row["edited_by"] == USERS["labor"][0]
    assert env["audits"][-1][1] == "call_in.edit"
    assert env["audits"][-1][4]["fields"] == ["note"]


def test_edit_to_spoke_closes_the_entry(env):
    db = env["db"]
    ci.poll_once(NOW_PRE)
    pairs = ((GC_A, K_A1), (GC_B, K_B1))
    ids = [_log(env, gc_id=g, contact_ids=[k]).json()["id"] for g, k in pairs]
    c = env["client"]
    for call_id in ids:
        assert c.patch(f"/calling-in/calls/{call_id}", json={"outcome": "spoke"}).status_code == 200
    (entry,) = db.tables["call_in_entries"]
    assert entry["close_reason"] == "cleared"


def test_correcting_the_clearing_call_reopens_the_list_without_a_new_notice(env, monkeypatch):
    db, c, clock = env["db"], env["client"], env["clock"]
    notices: list[tuple] = []
    monkeypatch.setattr(ci, "notify_role", lambda *a, **k: notices.append(a))
    clock["now"] = SENT + timedelta(hours=1)
    ci.poll_once(clock["now"])  # List 1 lands inside the burst guard: notifies
    assert len(notices) == 2
    spoke_a = _log(env, gc_id=GC_A, outcome="spoke", contact_ids=[K_A1]).json()["id"]
    spoke_b = _log(env, gc_id=GC_B, outcome="spoke", contact_ids=[K_B1]).json()["id"]
    assert [e["close_reason"] for e in db.tables["call_in_entries"]] == ["cleared"]

    # The author corrects the Spoke call to a voicemail: the list re-opens on
    # the next tick, claimed (badge and page) but silent.
    assert c.patch(f"/calling-in/calls/{spoke_b}", json={"outcome": "voicemail"}).status_code == 200
    clock["now"] = SENT + timedelta(hours=2)
    assert ci.poll_once(clock["now"]) == {"claimed": 1, "notified": 0, "closed": 0}
    assert c.get("/calling-in/summary").json()["open_count"] == 1

    # Cleared again, then an Executive deletes the call that cleared it.
    _log(env, gc_id=GC_B, outcome="spoke", contact_ids=[K_B1])
    assert c.delete(f"/calling-in/calls/{spoke_a}").status_code == 204
    clock["now"] = SENT + timedelta(hours=3)
    assert ci.poll_once(clock["now"]) == {"claimed": 1, "notified": 0, "closed": 0}
    assert len(notices) == 2
    open_rows = [e for e in db.tables["call_in_entries"] if e["closed_at"] is None]
    assert len(open_rows) == 1 and open_rows[0]["notified_at"] is None


def test_edit_contacts(env):
    call_id = _log(env).json()["id"]
    c = env["client"]
    resp = c.patch(f"/calling-in/calls/{call_id}", json={"contact_ids": [K_A2]})
    assert [x["gc_contact_id"] for x in resp.json()["contacts"]] == [K_A2]
    resp = c.patch(f"/calling-in/calls/{call_id}", json={"new_contacts": [{"name": "Extra"}]})
    assert [x["name"] for x in resp.json()["contacts"]] == ["Amy PM", "Extra"]
    assert c.patch(f"/calling-in/calls/{call_id}", json={"contact_ids": [K_B1]}).status_code == 422
    assert c.patch(f"/calling-in/calls/{call_id}", json={"contact_ids": []}).status_code == 422
    assert c.patch(f"/calling-in/calls/{call_id}", json={"note": " "}).status_code == 422
    assert c.patch(f"/calling-in/calls/{call_id}", json={"outcome": "nope"}).status_code == 422


def test_edit_and_delete_404(env):
    missing = "99999999-9999-9999-9999-999999999999"
    c = env["client"]
    assert c.patch(f"/calling-in/calls/{missing}", json={"note": "x"}).status_code == 404
    assert c.delete(f"/calling-in/calls/{missing}").status_code == 404


@pytest.mark.parametrize("who,code", [
    ("labor", 403), ("admin", 403), ("acct", 403), ("exec", 204), ("it", 204),
])
def test_delete_is_executive_and_it_admin_only(env, who, code):
    env["who"]["user"] = "labor"
    call_id = _log(env).json()["id"]
    env["who"]["user"] = who
    resp = env["client"].delete(f"/calling-in/calls/{call_id}")
    assert resp.status_code == code
    assert (env["db"].tables["call_in_calls"] == []) is (code == 204)
    if code == 204:
        assert env["audits"][-1][1] == "call_in.delete"


# ── project call log ─────────────────────────────────────────────────────────


def test_call_log_keeps_the_normal_redaction(env):
    _log(env, outcome="spoke")
    c = env["client"]
    env["who"]["user"] = "exec"
    shown = c.get(f"/projects/{PID}/call-log").json()
    assert shown["bid_at"] == T.isoformat()
    assert shown["on_list"] == "pre_bid"
    pre = {g["gc_id"]: g for g in shown["rounds"]["pre_bid"]["gcs"]}
    assert pre[GC_A]["done"] is True and len(pre[GC_A]["calls"]) == 1
    assert pre[GC_A]["gc_name"] == "Acme"
    assert {g["gc_id"] for g in shown["rounds"]["post_bid"]["gcs"]} == {GC_A, GC_B, GC_LATE}
    for who in ("labor",):  # outside ACTUAL_BID_VIEWER_ROLES
        env["who"]["user"] = who
        hidden = c.get(f"/projects/{PID}/call-log").json()
        assert hidden["bid_at"] is None and hidden["bid_at_date_only"] is False
    env["who"]["user"] = "acct"
    assert c.get(f"/projects/{PID}/call-log").json()["bid_at"] == T.isoformat()


def test_call_log_for_a_project_without_sends(env):
    body = env["client"].get(f"/projects/{OTHER_PID}/call-log").json()
    assert body["on_list"] is None
    assert body["rounds"] == {"pre_bid": {"gcs": []}, "post_bid": {"gcs": []}}
    assert env["client"].get("/projects/not-a-uuid/call-log").status_code == 404


# ── feature flag ─────────────────────────────────────────────────────────────


def test_routes_404_with_bidding_off(monkeypatch):
    import os

    import app.main
    from app.core.config import get_settings

    client = TestClient(app.main.app, raise_server_exceptions=False)
    paths = ["/calling-in/summary", "/calling-in", f"/calling-in/projects/{PID}",
             f"/projects/{PID}/call-log", "/analytics/calling-in"]
    for path in paths:
        assert client.get(path).status_code == 401, path  # mounted, asking for a token
    monkeypatch.setitem(os.environ, "BIDDING_ENABLED", "false")
    get_settings.cache_clear()
    try:
        for path in paths:
            assert client.get(path).status_code == 404, path
    finally:
        monkeypatch.setitem(os.environ, "BIDDING_ENABLED", "true")
        get_settings.cache_clear()
