"""GET /ai-estimating/runs/{run_id}/groups/{group_id}/loop (routers/plans_reader,
WS2 wave 2): the read-only view of a group's tool loop.

Pinned here:
  gates    the route carries require_plans_reader, require_internal and
           require_ai_estimating like every AI Estimating route; 404 with
           the bare body while PLANS_READER_ENABLED is off; 404 for an
           unknown run, a malformed group id or a group of another run.
  payload  rounds and tool calls of the group in order, timestamps in
           America/Los_Angeles, the group's loop_state.
  pricing  pricing tool calls, every later tool call (a record_finding
           claim may quote the price) and the assistant text from the
           round that saw a price are redacted for the Accountant (never a
           pricing viewer) and for an engineer while AI_PRICING_STATE is
           test; an Executive with pricing on and live sees them.
Supabase is faked in memory (the fake of tests/test_plans_reader_routes);
nothing reaches the network.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import features
from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user, require_ai_estimating, require_internal
from app.core.features import require_plans_reader
from app.core.roles import Role
from app.routers import plans_reader as pr
from tests.test_plans_reader_routes import FakeDB

RUN_ID = "11111111-1111-4111-8111-111111111111"
OTHER_RUN = "22222222-2222-4222-8222-222222222222"
GROUP_ID = "66666666-6666-4666-8666-666666666666"
OTHER_GROUP = "67666666-6666-4666-8666-666666666666"
PATH = f"/ai-estimating/runs/{RUN_ID}/groups/{GROUP_ID}/loop"


def _settings(**over) -> Settings:
    base = {"plans_reader_enabled": True, "openai_api_key": "sk-test", "ai_estimating_state": "live"}
    base.update(over)
    return Settings(_env_file=None, **base)


class Api:
    def __init__(self, monkeypatch, **settings):
        self.db = FakeDB()
        self.settings = _settings(**settings)
        self.role = Role.ESTIMATING_ADMIN
        monkeypatch.setattr(pr, "get_supabase", lambda: self.db)
        monkeypatch.setattr(pr, "get_settings", lambda: self.settings)
        monkeypatch.setattr(features, "get_settings", lambda: self.settings)
        app = FastAPI()
        app.include_router(pr.router)
        app.dependency_overrides[get_current_user] = self._user
        self.client = TestClient(app, raise_server_exceptions=True)

    def _user(self) -> CurrentUser:
        return CurrentUser(id="user-1", email="u@g3.com", role=self.role, is_active=True)

    def seed(self, table: str, *rows: dict) -> None:
        self.db.tables.setdefault(table, []).extend(dict(r) for r in rows)


def _seed_loop(api: Api) -> None:
    api.seed("plans_reader_runs", {"id": RUN_ID, "name": "Edikted", "status": "done"}, {"id": OTHER_RUN, "name": "Other", "status": "done"})
    api.seed(
        "plans_reader_groups",
        {"id": GROUP_ID, "run_id": RUN_ID, "kind": "review", "status": "done", "category": None, "mode": "review", "loop_state": {"round_no": 3, "status": "done"}},
        {"id": OTHER_GROUP, "run_id": OTHER_RUN, "kind": "review", "status": "done"},
    )
    api.seed(
        "plans_reader_loop_rounds",
        {"id": "r1", "run_id": RUN_ID, "group_id": GROUP_ID, "round_no": 1, "status": "done", "est_usd": 0.01, "cost_usd": 0.004, "response_id": "resp_1", "assistant_raw": {"text": "", "final": False, "stop_reason": "tool_use", "provider": "openai", "model": "gpt-6-astra"}, "tool_calls": [{"call_id": "c1", "name": "get_rows", "args": {"kind": "C"}}], "usage": {"input_tokens": 1000, "output_tokens": 50}, "created_at": "2026-10-21T19:00:00+00:00"},
        {"id": "r2", "run_id": RUN_ID, "group_id": GROUP_ID, "round_no": 2, "status": "done", "est_usd": 0.01, "cost_usd": 0.005, "response_id": "resp_2", "assistant_raw": {"text": "", "final": False, "stop_reason": "tool_use", "provider": "openai", "model": "gpt-6-astra"}, "tool_calls": [{"call_id": "c2", "name": "price_lookup", "args": {"description": "3/4 EMT", "uom": "FT"}}], "usage": {}, "created_at": "2026-10-21T19:01:00+00:00"},
        {"id": "r3", "run_id": RUN_ID, "group_id": GROUP_ID, "round_no": 3, "status": "done", "est_usd": 0.02, "cost_usd": 0.009, "response_id": "resp_3", "assistant_raw": {"text": "EMT runs about 4.44 per foot here", "final": True, "stop_reason": "final", "provider": "openai", "model": "gpt-6-astra"}, "tool_calls": [{"call_id": "c3", "name": "record_finding", "args": {"claim": "EMT at 4.44 per foot", "severity": "warn"}}], "usage": {}, "created_at": "2026-10-21T19:02:00+00:00"},
        {"id": "x1", "run_id": OTHER_RUN, "group_id": OTHER_GROUP, "round_no": 1, "status": "done", "assistant_raw": {"text": "other run"}, "tool_calls": [], "created_at": "2026-10-21T19:03:00+00:00"},
    )
    api.seed(
        "plans_reader_tool_calls",
        {"id": "t1", "run_id": RUN_ID, "group_id": GROUP_ID, "round_no": 1, "call_id": "c1", "tool_name": "get_rows", "args": {"kind": "C"}, "result_text": '{"rows":[]}', "result_data": {"rows": []}, "citations": ["C1"], "views": [], "refused": False, "created_at": "2026-10-21T19:00:30+00:00"},
        {"id": "t2", "run_id": RUN_ID, "group_id": GROUP_ID, "round_no": 2, "call_id": "c2", "tool_name": "price_lookup", "args": {"description": "3/4 EMT", "uom": "FT"}, "result_text": '{"status":"ok","material":{"value":4.44}}', "result_data": {"status": "ok", "material": {"value": 4.44}}, "citations": ["$:emt_34@v1"], "views": [], "refused": False, "created_at": "2026-10-21T19:01:30+00:00"},
        # A non-pricing tool after the price was seen: its claim quotes the price.
        {"id": "t3", "run_id": RUN_ID, "group_id": GROUP_ID, "round_no": 3, "call_id": "c3", "tool_name": "record_finding", "args": {"claim": "EMT at 4.44 per foot", "severity": "warn"}, "result_text": '{"finding_id":"f1","claim":"EMT at 4.44 per foot"}', "result_data": {"finding_id": "f1", "claim": "EMT at 4.44 per foot"}, "citations": ["f:f1"], "views": [], "refused": False, "created_at": "2026-10-21T19:02:30+00:00"},
    )


def test_route_carries_the_three_gates():
    route = next(r for r in pr.router.routes if r.path == "/ai-estimating/runs/{run_id}/groups/{group_id}/loop")
    assert "GET" in route.methods
    calls = {d.call for d in route.dependant.dependencies}
    assert {require_plans_reader, require_internal, require_ai_estimating} <= calls


def test_404_when_the_page_flag_is_off(monkeypatch):
    api = Api(monkeypatch, plans_reader_enabled=False)
    _seed_loop(api)
    resp = api.client.get(PATH)
    assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}


def test_404_for_unknown_run_malformed_group_and_foreign_group(monkeypatch):
    api = Api(monkeypatch)
    _seed_loop(api)
    assert api.client.get(f"/ai-estimating/runs/{OTHER_RUN}/groups/{GROUP_ID}/loop").status_code == 404
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/groups/not-a-uuid/loop").status_code == 404
    assert api.client.get(f"/ai-estimating/runs/99999999-9999-4999-8999-999999999999/groups/{GROUP_ID}/loop").status_code == 404


def test_estimator_is_refused(monkeypatch):
    api = Api(monkeypatch)
    _seed_loop(api)
    api.role = Role.ESTIMATOR
    assert api.client.get(PATH).status_code == 403


def test_accountant_sees_rounds_but_not_pricing(monkeypatch):
    api = Api(monkeypatch, ai_pricing_enabled=True, ai_pricing_state="live")
    _seed_loop(api)
    api.role = Role.ACCOUNTANT
    resp = api.client.get(PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pricing_visible"] is False
    assert body["group"]["id"] == GROUP_ID and body["group"]["loop_state"] == {"round_no": 3, "status": "done"}
    assert [r["round_no"] for r in body["rounds"]] == [1, 2, 3]
    assert body["rounds"][0]["created_at"] == "2026-10-21T12:00:00-07:00"
    assert [c["call_id"] for c in body["tool_calls"]] == ["c1", "c2", "c3"]
    assert body["tool_calls"][0]["result_text"] == '{"rows":[]}'
    priced = body["tool_calls"][1]
    assert priced["result_text"] == "pricing" and priced["result_data"] == {"redacted": "pricing"}
    assert priced["args"] == {"redacted": "pricing"} and priced["citations"] == [] and priced["pricing_restricted"]
    assert body["rounds"][1]["tool_calls"][0]["args"] == {"redacted": "pricing"}
    assert body["rounds"][2]["text"] == "pricing" and body["rounds"][2]["pricing_restricted"] is True
    assert body["rounds"][0]["pricing_restricted"] is False
    later = body["tool_calls"][2]
    assert later["tool_name"] == "record_finding" and later["args"] == {"redacted": "pricing"}
    assert later["result_text"] == "pricing" and later["result_data"] == {"redacted": "pricing"} and later["citations"] == []
    assert body["rounds"][2]["tool_calls"][0]["args"] == {"redacted": "pricing"}
    assert "4.44" not in resp.text and "EMT" not in resp.text
    assert "other run" not in resp.text


def test_engineer_in_test_state_is_redacted_and_executive_live_sees_prices(monkeypatch):
    api = Api(monkeypatch, ai_pricing_enabled=True, ai_pricing_state="test")
    _seed_loop(api)
    api.role = Role.ESTIMATING_ENGINEER_LABOR
    body = api.client.get(PATH).json()
    assert body["pricing_visible"] is False and "4.44" not in str(body)
    api.role = Role.EXECUTIVE
    body = api.client.get(PATH).json()
    assert body["pricing_visible"] is True
    assert body["tool_calls"][1]["result_data"] == {"status": "ok", "material": {"value": 4.44}}
    assert body["rounds"][2]["text"].startswith("EMT runs")
    assert body["tool_calls"][1]["pricing_restricted"] is True  # the flag stays so the page can mark it
    assert body["tool_calls"][2]["args"]["claim"] == "EMT at 4.44 per foot"


def test_pricing_switch_off_hides_prices_from_everyone(monkeypatch):
    api = Api(monkeypatch, ai_pricing_enabled=False)
    _seed_loop(api)
    api.role = Role.EXECUTIVE
    body = api.client.get(PATH).json()
    assert body["pricing_visible"] is False and "4.44" not in str(body)
