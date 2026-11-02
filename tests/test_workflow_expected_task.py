"""POST /projects/{id}/advance: the `expected_task` compare-and-set and the
assistant principal's route guard (docs/ASSISTANT_PHASE4_PLAN.md 3.12).

The route is mounted alone with the Supabase client, the lane state and the
notifier faked. A human caller keeps today's behaviour exactly; an assistant
principal (via_assistant_run_id set) must send gono_action 'review' plus
expected_task and may not move a closed project."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.models.schemas import TransitionIn
from app.routers import workflow as rt

PID = "77777777-7777-4777-8777-777777777777"


class _Q:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self._single = False

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        hits = [dict(r) for r in self.rows if all(r.get(c) == v for c, v in self.filters)]
        if self._single:
            return SimpleNamespace(data=hits[0] if hits else None)
        return SimpleNamespace(data=hits)


class _SB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


class Api:
    def __init__(self, monkeypatch, *, project=None, lane=None):
        self.project = {
            "id": PID, "current_stage": "labor_numbers", "abandoned_at": None, "failed_to_bid_at": None,
            **(project or {}),
        }
        self.lane = lane or {"labor_numbers": {"current_task": "labor_numbers", "status": "active"}}
        self.advanced: list[tuple] = []
        self.notified: list[tuple] = []
        self.user = CurrentUser(id="user-1", email="u@g3.com", role=Role.EXECUTIVE, is_active=True)
        monkeypatch.setattr(rt, "get_supabase", lambda: _SB({"projects": [self.project], "project_files": []}))
        monkeypatch.setattr(rt.workflow, "load_category_state", lambda pid: self.lane)

        def advance(pid, category, uid, note):
            self.advanced.append((pid, category, uid, note))
            return {"id": pid, "current_stage": "labor_numbers", "actual_bid_at": None}

        monkeypatch.setattr(rt.workflow, "advance_category", advance)
        monkeypatch.setattr(rt.gono, "apply_entry_action", lambda *a, **k: (None, None))
        monkeypatch.setattr(rt, "notify_roles", lambda *a, **k: self.notified.append((a, k)))
        app = FastAPI()
        app.include_router(rt.router)
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app, raise_server_exceptions=True)

    def assistant(self):
        self.user = CurrentUser(
            id="user-1", email="u@g3.com", role=Role.EXECUTIVE, is_active=True,
            via_assistant_run_id="22222222-2222-4222-8222-222222222222",
        )

    def post(self, **body):
        return self.client.post(f"/projects/{PID}/advance", json={"category": "labor_numbers", **body})


@pytest.fixture
def api(monkeypatch):
    return Api(monkeypatch)


def test_schema_field_is_optional_and_capped():
    assert TransitionIn(category="intake").expected_task is None
    assert TransitionIn(category="intake", expected_task="rfqs").expected_task == "rfqs"
    with pytest.raises(ValueError):
        TransitionIn(category="intake", expected_task="x" * 61)


def test_mismatch_is_409_lane_moved_with_no_write_and_no_handoff(api):
    r = api.post(expected_task="markup")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "lane_moved", "head": "labor_numbers"}
    assert api.advanced == [] and api.notified == []


def test_matching_expected_task_advances(api):
    r = api.post(expected_task="labor_numbers")
    assert r.status_code == 200
    assert len(api.advanced) == 1


def test_absent_expected_task_is_unchanged_behaviour(api):
    r = api.post()
    assert r.status_code == 200
    assert len(api.advanced) == 1


def test_human_may_send_any_gono_action_without_expected_task(api):
    for action in ("score", "go", "no_go", "review"):
        assert api.post(gono_action=action).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"expected_task": "labor_numbers"},
        {"gono_action": "score", "expected_task": "labor_numbers"},
        {"gono_action": "go", "expected_task": "labor_numbers"},
        {"gono_action": "review"},
        {"gono_action": "review", "expected_task": ""},
        {},
    ],
)
def test_assistant_without_review_and_expected_task_is_refused(api, body):
    api.assistant()
    r = api.post(**body)
    assert r.status_code == 403
    assert r.json()["detail"] == {"code": "assistant_advance_refused"}
    assert api.advanced == []


def test_assistant_with_review_and_matching_expected_task_advances(api):
    api.assistant()
    r = api.post(gono_action="review", expected_task="labor_numbers")
    assert r.status_code == 200
    assert len(api.advanced) == 1


def test_assistant_mismatch_is_lane_moved(api):
    api.assistant()
    r = api.post(gono_action="review", expected_task="markup")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "lane_moved"
    assert api.advanced == []


CLOSED = [
    {"failed_to_bid_at": "2026-10-01T00:00:00+00:00"},
    {"current_stage": "pm_only"},
    {"current_stage": "cp_only"},
]


@pytest.mark.parametrize("project", CLOSED)
def test_assistant_on_a_closed_project_gets_project_closed(monkeypatch, project):
    api = Api(monkeypatch, project=project)
    api.assistant()
    r = api.post(gono_action="review", expected_task="labor_numbers")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "project_closed"}
    assert api.advanced == []


@pytest.mark.parametrize("project", CLOSED)
def test_human_on_the_same_projects_keeps_todays_behaviour(monkeypatch, project):
    api = Api(monkeypatch, project=project)
    r = api.post()
    assert r.status_code == 200
    assert len(api.advanced) == 1


def test_declined_and_abandoned_still_refuse_everyone(monkeypatch):
    for project in ({"current_stage": "declined"}, {"abandoned_at": "2026-10-01T00:00:00+00:00"}):
        api = Api(monkeypatch, project=project)
        assert api.post().status_code == 409
        api.assistant()
        assert api.post(gono_action="review", expected_task="labor_numbers").status_code == 409
        assert api.advanced == []


def test_lane_not_active_is_unchanged_for_a_human(monkeypatch):
    api = Api(monkeypatch, lane={"labor_numbers": {"current_task": "labor_numbers", "status": "locked"}})
    r = api.post(expected_task="labor_numbers")
    assert r.status_code == 409
    assert "not active" in r.json()["detail"]
