"""Auto send routes (docs/AUTO_SEND.md section 8): role gates, payload shapes
and the 409 mapping, over the in-memory fake of tests/test_auto_send.py.

Pinned here:
  - the settings read and the project panel admit every internal role (the
    read-only accountant included); the external estimator gets 403;
  - the on/off switch is IT Admin only; the markup % is Executive / IT Admin;
  - Send now and Accept are Estimating Admin / Executive / IT Admin;
  - refusals carry a plain message and the X-Error-Code header; unknown
    project 404s; the markup body is range checked (422).

The router is mounted on its own app so these tests do not depend on main.py.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import auto_send as router_mod
from tests.test_auto_send import P1, SENT_ROWS, _db, _sent_run
from tests.test_auto_send import env as _env  # noqa: F401, F811 (the shared `env` fixture)

USERS = {
    "it": ("u-it", Role.IT_ADMIN),
    "exec": ("u-exec", Role.EXECUTIVE),
    "admin": ("u-admin", Role.ESTIMATING_ADMIN),
    "labor": ("u-labor", Role.ESTIMATING_ENGINEER_LABOR),
    "acct": ("u-acct", Role.ACCOUNTANT),
    "estimator": ("u-est", Role.ESTIMATOR),
}


@pytest.fixture
def api(_env):  # noqa: F811
    app = FastAPI()
    app.include_router(router_mod.router)
    who = {"user": "it"}

    def _user():
        uid, role = USERS[who["user"]]
        return CurrentUser(id=uid, email=f"{who['user']}@g3.test", role=role, is_active=True)

    app.dependency_overrides[get_current_user] = _user

    class Api:
        def __init__(self):
            self.client = TestClient(app, raise_server_exceptions=True)
            self.env = _env

        def as_(self, key):
            who["user"] = key
            return self.client

    return Api()


# ── settings ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["it", "exec", "admin", "labor", "acct"])
def test_settings_read_is_open_to_internal_roles(api, key):
    api.env.install(_db())
    res = api.as_(key).get("/auto-send/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True and body["markup_pct"] == "10.00"
    assert body["can_toggle"] is (key == "it")
    assert body["can_edit_pct"] is (key in ("it", "exec"))
    assert body["send_time"] == "17:05" and body["preflight_time"] == "15:00"


def test_estimator_is_refused_everywhere(api):
    api.env.install(_db())
    c = api.as_("estimator")
    assert c.get("/auto-send/settings").status_code == 403
    assert c.get(f"/projects/{P1}/auto-send").status_code == 403
    assert c.patch("/auto-send/settings/enabled", json={"enabled": False}).status_code == 403
    assert c.post(f"/projects/{P1}/auto-send/send-now", json={}).status_code == 403


@pytest.mark.parametrize("key, code", [("it", 200), ("exec", 403), ("admin", 403),
                                       ("labor", 403), ("acct", 403)])
def test_toggle_is_it_admin_only(api, key, code):
    db = api.env.install(_db())
    res = api.as_(key).patch("/auto-send/settings/enabled", json={"enabled": False})
    assert res.status_code == code
    assert db.tables["auto_send_settings"][0]["enabled"] is (code != 200)
    if code == 200:
        assert res.json()["enabled"] is False
        assert db.tables["auto_send_settings"][0]["updated_by"] == "u-it"


@pytest.mark.parametrize("key, code", [("it", 200), ("exec", 200), ("admin", 403),
                                       ("labor", 403), ("acct", 403)])
def test_markup_is_exec_and_it_admin(api, key, code):
    api.env.install(_db())
    res = api.as_(key).patch("/auto-send/settings/markup", json={"markup_pct": "15"})
    assert res.status_code == code
    if code == 200:
        assert res.json()["markup_pct"] == "15.00"


@pytest.mark.parametrize("value", ["-1", "100.5", "abc"])
def test_markup_body_is_range_checked(api, value):
    api.env.install(_db())
    res = api.as_("exec").patch("/auto-send/settings/markup", json={"markup_pct": value})
    assert res.status_code == 422


# ── project panel ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["it", "exec", "admin", "labor", "acct"])
def test_panel_is_readable_by_every_internal_role(api, key):
    api.env.install(_db())
    res = api.as_(key).get(f"/projects/{P1}/auto-send")
    assert res.status_code == 200
    body = res.json()
    assert body["preview"]["outcome"] == "ready"
    assert body["latest_run"] is None
    for field in ("enabled", "markup_pct", "send_time", "bid_date", "pending",
                  "can_send_now", "can_accept", "lines_review"):
        assert field in body


def test_panel_unknown_project_is_404(api):
    api.env.install(_db())
    assert api.as_("exec").get("/projects/nope/auto-send").status_code == 404


# ── send now ────────────────────────────────────────────────────────────────


def _missed():
    return [{"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "missed",
             "created_at": "2026-10-20T03:00:00Z"}]


@pytest.mark.parametrize("key, code", [("admin", 200), ("exec", 200), ("it", 200),
                                       ("labor", 403), ("acct", 403)])
def test_send_now_roles(api, key, code):
    api.env.install(_db(runs=_missed()))
    res = api.as_(key).post(f"/projects/{P1}/auto-send/send-now", json={})
    assert res.status_code == code
    if code == 200:
        body = res.json()
        assert body["latest_run"]["status"] == "sent"
        assert body["pending"] is True and body["can_accept"] is True
        assert len(api.env.mails) == 2


def test_send_now_without_a_body(api):
    api.env.install(_db(runs=_missed()))
    assert api.as_("admin").post(f"/projects/{P1}/auto-send/send-now").status_code == 200


def test_send_now_refusal_is_a_plain_409(api):
    api.env.install(_db(runs=_missed(), enabled=False))
    res = api.as_("admin").post(f"/projects/{P1}/auto-send/send-now", json={})
    assert res.status_code == 409
    assert res.headers["X-Error-Code"] == "disabled"
    assert "turned off" in res.json()["detail"]


def test_send_now_nothing_to_send(api):
    api.env.install(_db())
    res = api.as_("exec").post(f"/projects/{P1}/auto-send/send-now", json={})
    assert res.status_code == 409
    assert res.headers["X-Error-Code"] == "nothing_to_send"


# ── accept ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key, code", [("admin", 200), ("exec", 200), ("it", 200),
                                       ("labor", 403), ("acct", 403)])
def test_accept_roles(api, key, code):
    db = api.env.install(_db(runs=[_sent_run()], run_gcs=SENT_ROWS))
    res = api.as_(key).post(f"/projects/{P1}/auto-send/accept", json={"run_id": "r1"})
    assert res.status_code == code
    assert len(db.rpcs) == (1 if code == 200 else 0)


def test_accept_already_accepted_is_409(api):
    api.env.install(_db(runs=[_sent_run(accepted_at="2026-10-20T01:00:00Z")],
                        run_gcs=SENT_ROWS))
    res = api.as_("admin").post(f"/projects/{P1}/auto-send/accept", json={"run_id": "r1"})
    assert res.status_code == 409
    assert res.headers["X-Error-Code"] == "already_accepted"


def test_accept_needs_a_run_id(api):
    api.env.install(_db())
    res = api.as_("admin").post(f"/projects/{P1}/auto-send/accept", json={})
    assert res.status_code == 422


def test_malformed_ids_are_404_not_a_raw_500(api):
    """Finding 2: PostgREST's 22P02 on a uuid column maps to not found."""
    from tests.test_auto_send import _BadUuidDB

    api.env.install(_BadUuidDB(_db(runs=[_sent_run()], run_gcs=SENT_ROWS).tables))
    c = api.as_("admin")
    assert c.get("/projects/not-a-uuid/auto-send").status_code == 404
    assert c.post("/projects/not-a-uuid/auto-send/send-now", json={}).status_code == 404
    res = c.post(f"/projects/{P1}/auto-send/accept", json={"run_id": "not-a-uuid"})
    assert res.status_code == 404
    assert res.headers["X-Error-Code"] == "run_not_for_project"
