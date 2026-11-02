"""The assistant's AI Estimating routes (app/routers/plans_reader_assistant.py;
docs/ASSISTANT_PHASE4_PLAN.md 3.4, 3.5.1, 3.5.3, 3.6.1, 3.6.3, WI-9a).

Invariants pinned here:

  gates      every route carries require_plans_reader, require_internal,
             require_ai_estimating and auth; 404 with PLANS_READER_ENABLED
             off, 403 for a role AI_ESTIMATING_STATE refuses; bad ids 400;
  reads      the runs list carries staleness and the sheet index fallback;
             the brief prefers the deliverable; the digest drops positions
             and every pricing key; the notes search syncs at most 2 runs
             lazily and searches at most 3;
  page read  approval-bound (human 404, turn principal 404), idempotent by
             approval, reuses an orphan hold of the same approval, reserves
             max(approved, re-estimated), refuses estimate_changed and
             question_asks_for_change, releases the hold and fails the task
             on each failure path, one pending answer per thread;
  run        the composite route resumes from its recorded phase after a
             crash at each phase (the recover read finds the task), honours
             the 15-file and 400-page caps, estimate_changed cancels the run
             and releases the hold, a second start's 409 counts as started;
  AE names   every AE function the router calls by name still exists.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.assistant import repo
from app.core import provenance, supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_ai_estimating, require_internal
from app.core.features import require_plans_reader
from app.core.ratelimit import ai_rate_limit, report_rate_limit
from app.core.roles import Role
from app.routers import plans_reader as pr
from app.routers import plans_reader_assistant as pra
from app.services import assistant_ai_estimate as aie
from app.services import file_access
from app.services import plans_reader_chat as chat
from app.services import plans_reader_notes as notes
from app.services import plans_reader_runs as runs

from tests.assistant_fakes import (
    APPROVAL_ID, CONV, MSG_ASSISTANT, NOW, PLANS_RUN, PROJECT, RUN, USER, FakeDB, approval, plans_note,
    profile, settings_row,
)

HEADER = "X-Assistant-Idempotency-Key"
DOC = "16161616-1616-4616-8616-161616161611"
PF1 = "17171717-1717-4717-8717-171717171711"
PF2 = "17171717-1717-4717-8717-171717171712"
RUN_B = "14141414-1414-4414-8414-141414141412"
RUN_C = "14141414-1414-4414-8414-141414141413"
OTHER_APPROVAL = "99999999-9999-4999-8999-999999999990"
NUMBER = "26.10.7212"
QUESTION = "What does sheet E-201 show for panel 1HD-21?"


class _Crash(BaseException):
    """A process dying mid-request: nothing after it runs, no cleanup."""


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


def _run(**over) -> dict:
    base = {"id": PLANS_RUN, "name": "Monument set", "status": "done", "source": "project", "project_id": PROJECT,
            "created_at": (NOW - timedelta(days=2)).isoformat(), "started_at": (NOW - timedelta(days=2)).isoformat(),
            "finished_at": (NOW - timedelta(days=1)).isoformat(), "documents_total": 3, "cost_usd": 4.123,
            "error": None, "created_by": USER}
    base.update(over)
    return base


def _estimate(**over) -> dict:
    base = {"run": {"id": PLANS_RUN, "name": "Monument set"}, "document": {"id": DOC, "name": "E-201.pdf"},
            "pages": [{"page": 14, "sheet_number": "E-201", "title": "Panels", "w_in": 36, "h_in": 24,
                       "ppi": 188, "tokens": 35900}],
            "input_tokens": 479200, "output_tokens": 8000, "rounds": 8, "estimate_usd": "1.42", "max_usd": "2.13",
            "model": "gpt-6-astra", "sizes_approx": False,
            "ai_estimating": {"cap_usd": "75.00", "remaining_usd": "70.00", "blocked": False},
            "assistant": {"user_remaining_usd": "40.00", "global_remaining_usd": "45.00"}, "problems": []}
    base.update(over)
    return base


@pytest.fixture
def api(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "plans_reader_enabled", True)
    monkeypatch.setattr(s, "ai_estimating_state", "live")
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()],
        assistant_profiles=[{"user_id": USER, "enabled": True, "daily_cost_cap_usd": 40, "legal_hold": False}],
        assistant_tasks=[], assistant_spend_reservations=[], llm_call_log=[], assistant_messages=[],
        assistant_approvals=[approval(action_kind="plans_read_pages", status="executing",
                                      payload={"v": 1, "args": {"run_id": PLANS_RUN, "document_id": DOC,
                                                                "pages": [14], "question": QUESTION}})],
        projects=[{"id": PROJECT, "number": NUMBER, "name": "Monument"}],
        plans_reader_runs=[_run()], plans_reader_documents=[], plans_reader_outputs=[], plans_reader_deliverables=[],
        plans_reader_files=[], plans_reader_chat_threads=[], plans_reader_chat_messages=[], plans_reader_notes=[],
        project_files=[], bid_split_jobs=[], bid_split_files=[], bid_split_segments=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    state = SimpleNamespace(
        role=Role.EXECUTIVE, via=RUN, calls={}, audits=[], accesses=[], estimate=_estimate(), available=None,
        behind=[], synced=[], stale={}, crash_after=None, start_exc=None, ae_estimate={
            "cost_usd": 1.6, "cost_usd_max": 2.0, "over_cap": False, "over_run_limit": False},
        file_pages=10,
    )

    def record(step, /, *args, **kwargs):
        state.calls.setdefault(step, []).append((args, kwargs))
        if state.crash_after == step:
            state.crash_after = None
            raise _Crash(step)

    # AE chat ---------------------------------------------------------------
    def create_thread(sb, run, *, title, user):
        row = db._insert("plans_reader_chat_threads", {"run_id": run["id"], "title": title,
                                                        "created_by": str(user.id)})
        state.calls.setdefault("create_thread", []).append(title)
        return row

    def send_message(sb, run, thread, *, content, pro_max, user):
        live = [m for m in db.tables["plans_reader_chat_messages"]
                if m["thread_id"] == thread["id"] and m["role"] == "assistant" and m["status"] in ("pending", "running")]
        if live:
            raise ValueError(chat.BUSY_MESSAGE)
        u = db._insert("plans_reader_chat_messages", {"thread_id": thread["id"], "role": "user", "content": content,
                                                       "status": "done", "created_by": str(user.id), "cost_usd": 0})
        a = db._insert("plans_reader_chat_messages", {"thread_id": thread["id"], "role": "assistant", "content": "",
                                                       "status": "pending", "created_by": str(user.id),
                                                       "pro_max": pro_max, "cost_usd": 0})
        record("send_message", content=content, pro_max=pro_max)
        return {"user_message": u, "assistant_message": a}

    monkeypatch.setattr(chat, "create_thread", create_thread)
    monkeypatch.setattr(chat, "send_message", send_message)

    # AE runs ---------------------------------------------------------------
    def create_run(sb, *, name, source="upload", project_id=None, render_mode="adaptive", user, eval_case_id=None):
        row = db._insert("plans_reader_runs", {"name": name, "status": "created", "source": source,
                                                "project_id": project_id, "render_mode": render_mode,
                                                "created_by": str(user.id), "cost_usd": 0, "documents_total": 0})
        record("create_run", name=name, source=source, project_id=project_id, render_mode=render_mode)
        return row

    def add_project_files(sb, run, *, project_file_ids, user):
        have = {f["source_project_file_id"] for f in db.tables["plans_reader_files"] if f["run_id"] == run["id"]}
        out = []
        for fid in project_file_ids:
            if fid in have:
                continue
            out.append(db._insert("plans_reader_files", {"run_id": run["id"], "source_project_file_id": fid,
                                                          "label": "electrical_drawings", "page_count": state.file_pages,
                                                          "status": "pending", "filename": f"{fid}.pdf"}))
        record("add_project_files", ids=list(project_file_ids))
        return out

    def estimate(sb, run):
        record("estimate", run_id=run["id"])
        return dict(state.ae_estimate)

    def start(sb, run, *, user):
        row = next(r for r in db.tables["plans_reader_runs"] if r["id"] == run["id"])
        if state.start_exc is not None:
            exc, state.start_exc = state.start_exc, None
            if getattr(exc, "status_code", None) == 409:
                row["status"] = "splitting"
            raise exc
        if row["status"] != "created":
            raise runs.RunRefused("This run has already started.", 409)
        row["status"] = "splitting"
        record("start", run_id=run["id"])
        return row

    def cancel(sb, run, *, user):
        row = next(r for r in db.tables["plans_reader_runs"] if r["id"] == run["id"])
        row["status"] = "canceled"
        state.calls.setdefault("cancel", []).append(run["id"])
        return row

    monkeypatch.setattr(runs, "create_run", create_run)
    monkeypatch.setattr(runs, "add_project_files", add_project_files)
    monkeypatch.setattr(runs, "estimate", estimate)
    monkeypatch.setattr(runs, "start", start)
    monkeypatch.setattr(runs, "cancel", cancel)
    monkeypatch.setattr(runs, "spine_enabled", lambda s=None: False)

    # Estimates, availability, notes, audit ---------------------------------
    monkeypatch.setattr(aie, "page_read_estimate", lambda sb, run, did, pages, uid, **k: dict(state.estimate))
    monkeypatch.setattr(aie, "unlabeled_split_usd", lambda pages, settings=None: Decimal("0.00"))

    def available(*, queue=True):
        state.calls.setdefault("available", []).append(queue)
        if state.available is not None:
            raise state.available

    monkeypatch.setattr(pr, "_require_available", available)
    monkeypatch.setattr(notes, "notes_behind", lambda sb, ids: [i for i in ids if i in state.behind])

    def sync_run(sb, run_id, *, max_rows=notes.DEFAULT_MAX_ROWS):
        state.synced.append(run_id)
        return {"run_id": run_id, "written": 1, "removed": 0, "versions": {}, "partial": False}

    monkeypatch.setattr(notes, "sync_run", sync_run)
    monkeypatch.setattr(notes, "stale_files", lambda sb, pid, run: list(state.stale.get(str(run["id"]), [])))
    monkeypatch.setattr(pra, "audit", lambda *a: state.audits.append(a))
    monkeypatch.setattr(file_access, "record", lambda user, **kw: state.accesses.append(kw))

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(pra.router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[ai_rate_limit] = lambda: None
    app.dependency_overrides[report_rate_limit] = lambda: None
    actor = _Actor(app)
    actor.actor = (USER, RUN, APPROVAL_ID)
    return SimpleNamespace(client=TestClient(actor), db=db, state=state, actor=actor)


def _human(api):
    api.actor.actor = None
    api.state.via = None


def _code(res) -> str | None:
    return res.headers.get("X-Error-Code")


def _holds(api, kind=None):
    return [h for h in api.db.tables["assistant_spend_reservations"] if kind is None or h["kind"] == kind]


# ── gates ────────────────────────────────────────────────────────────────────


def _dep_calls(route: APIRoute) -> set:
    calls = set()

    def walk(dep):
        calls.add(dep.call)
        for d in dep.dependencies:
            walk(d)

    walk(route.dependant)
    return calls


def test_every_route_carries_the_ae_trio_and_auth():
    assert len(pra.router.routes) == 9
    for route in pra.router.routes:
        direct = {d.call for d in route.dependant.dependencies}
        assert {require_plans_reader, require_internal, require_ai_estimating} <= direct, route.path
        assert get_current_user in _dep_calls(route), route.path


def test_gets_carry_the_report_limit_and_posts_the_ai_limit_and_approval_bound():
    from app.core.approval_bound import require_approval_bound
    from app.core.deps import require_writer

    for route in pra.router.routes:
        direct = {d.call for d in route.dependant.dependencies}
        if "GET" in route.methods:
            assert report_rate_limit in direct, route.path
        else:
            assert {ai_rate_limit, require_writer, require_approval_bound} <= direct, route.path
        assert not getattr(route.endpoint, "__code__").co_flags & 0x80, f"{route.path} must be plain def"


def _every_path() -> list[tuple[str, str]]:
    out = []
    for route in pra.router.routes:
        path = (route.path.replace("{project_id}", PROJECT).replace("{run_id}", PLANS_RUN)
                .replace("{category}", "main"))
        out.append((sorted(route.methods)[0], path))
    return out


def test_every_route_404s_while_the_flag_is_off(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "plans_reader_enabled", False)
    for method, path in _every_path():
        res = api.client.request(method, path, json={} if method == "POST" else None, headers={HEADER: APPROVAL_ID})
        assert res.status_code == 404 and res.json() == {"detail": "Not Found"}, (method, path)


@pytest.mark.parametrize("state,role,code", [
    ("test", Role.ESTIMATING_ADMIN, 403), ("dev", Role.EXECUTIVE, 403), ("test", Role.EXECUTIVE, 200),
    ("live", Role.ACCOUNTANT, 200),
])
def test_the_ae_state_decides_who_gets_in(api, monkeypatch, state, role, code):
    monkeypatch.setattr(get_settings(), "ai_estimating_state", state)
    api.state.role = role
    _human(api)
    assert api.client.get(f"/ai-estimating/projects/{PROJECT}/runs").status_code == code


def test_a_role_the_state_refuses_gets_403_on_every_route(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_estimating_state", "test")
    api.state.role = Role.ESTIMATING_ADMIN
    for method, path in _every_path():
        res = api.client.request(method, path, json={} if method == "POST" else None, headers={HEADER: APPROVAL_ID})
        assert res.status_code == 403, (method, path)


@pytest.mark.parametrize("path", [
    "/ai-estimating/projects/nope/runs",
    "/ai-estimating/runs/nope/brief",
    "/ai-estimating/runs/nope/documents",
    "/ai-estimating/runs/nope/outputs/main/digest?section=brief",
    "/ai-estimating/notes/search?q=panel&run_id=nope",
    "/ai-estimating/notes/search?q=panel&project_id=nope",
    f"/ai-estimating/runs/nope/read-pages/estimate?document_id={DOC}&pages=1",
    f"/ai-estimating/runs/{PLANS_RUN}/read-pages/estimate?document_id=nope&pages=1",
    "/ai-estimating/projects/nope/estimate-preview",
])
def test_bad_ids_are_400(api, path):
    _human(api)
    res = api.client.get(path)
    assert res.status_code == 400 and _code(res) == "id_invalid", res.text


def test_bad_ids_on_the_posts_are_400_before_anything_is_written(api):
    res = api.client.post("/ai-estimating/runs/nope/assistant-page-reads", headers={HEADER: APPROVAL_ID},
                          json={"document_id": DOC, "pages": [14], "question": QUESTION, "max_usd": "2.13"})
    assert res.status_code == 400 and _code(res) == "id_invalid"
    res = api.client.post("/ai-estimating/projects/nope/assistant-runs", headers={HEADER: APPROVAL_ID},
                          json={"file_ids": [PF1], "hold_usd": "3.00"})
    assert res.status_code == 400 and _code(res) == "id_invalid"
    assert api.db.tables["assistant_tasks"] == [] and _holds(api) == []


# ── reads ────────────────────────────────────────────────────────────────────


def test_the_runs_list_carries_staleness(api):
    _human(api)
    added = [{"id": PF1, "filename": "Addendum 2.pdf", "category": "addenda", "created_at": NOW.isoformat()}]
    api.state.stale = {PLANS_RUN: added * 25}
    api.db.tables["plans_reader_runs"].append(_run(id=RUN_B, name="Older", created_at=(NOW - timedelta(days=9)).isoformat()))
    res = api.client.get(f"/ai-estimating/projects/{PROJECT}/runs")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project"] == {"id": PROJECT, "number": NUMBER, "name": "Monument"}
    assert [r["id"] for r in body["runs"]] == [PLANS_RUN, RUN_B]
    newest = body["runs"][0]
    assert newest["stale"] is True and newest["new_files_count"] == 25 and len(newest["new_files_since"]) == 20
    assert newest["cost_usd"] == "4.13" and body["runs"][1]["stale"] is False
    assert body["sheet_index_fallback"] is None


def test_a_project_without_a_run_gets_the_splitter_sheet_index(api):
    _human(api)
    api.db.tables["plans_reader_runs"] = []
    api.db.tables["bid_split_jobs"] = [{"id": "job-1", "project_id": PROJECT}]
    api.db.tables["bid_split_files"] = [{"id": "file-1", "job_id": "job-1"}]
    api.db.tables["bid_split_segments"] = [{"id": "seg-1", "file_id": "file-1", "category": "electrical_drawings",
                                            "page_meta": [{"page": i, "title": f"E-{i}", "page_role": "plan"}
                                                          for i in range(1, 260)]}]
    body = api.client.get(f"/ai-estimating/projects/{PROJECT}/runs").json()
    assert body["runs"] == [] and len(body["sheet_index_fallback"]) == 200
    assert body["sheet_index_fallback"][0] == {"page": 1, "title": "E-1", "page_role": "plan"}


def _brief(**over):
    base = {"project_summary": "Main " + "x" * 900, "electrical_scope_summary": "Scope",
            "risks": [{"risk": f"r{i}", "severity": "low", "source": None} for i in range(60)]}
    base.update(over)
    return base


def test_the_brief_prefers_the_deliverable_and_caps_lists_and_strings(api):
    _human(api)
    api.db.tables["plans_reader_outputs"] = [
        {"id": "o1", "run_id": PLANS_RUN, "scope": "main", "category": None, "version": 2,
         "output": {"brief": _brief(), "material_notes": [{"material_category": "gear", "notes": ["n"]}],
                    "confidence_notes": ["c1"]}},
    ]
    api.db.tables["plans_reader_deliverables"] = [
        {"id": "d1", "run_id": PLANS_RUN, "kind": "brief", "version": 1, "spec_json": _brief(project_summary="Edited v1")},
        {"id": "d2", "run_id": PLANS_RUN, "kind": "brief", "version": 3,
         "spec_json": {"brief": _brief(project_summary="Edited v3")}},
    ]
    api.state.behind = [PLANS_RUN]
    res = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/brief")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "brief_deliverable" and body["version"] == 3
    assert body["brief"]["project_summary"] == "Edited v3"
    assert len(body["brief"]["risks"]) == 40 and set(body["brief"]) >= {"systems", "rfis", "spec_highlights"}
    assert body["material_notes"] == [{"material_category": "gear", "notes": ["n"]}]
    assert body["confidence_notes"] == ["c1"] and body["notes_partial"] is False
    assert api.state.synced == [PLANS_RUN]
    api.db.tables["plans_reader_deliverables"] = []
    body = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/brief").json()
    assert body["source"] == "main" and body["version"] == 2 and len(body["brief"]["project_summary"]) == 600
    api.db.tables["plans_reader_outputs"] = []
    res = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/brief")
    assert res.status_code == 404 and _code(res) == "no_brief"


def test_documents_list_sheets_up_to_400(api):
    _human(api)
    meta = [{"page": i, "pdf_page": i, "title": f"E-{i}", "page_role": "plan" if i % 2 else "schedule",
             "w_in": 36, "h_in": 24} for i in range(1, 301)]
    api.db.tables["plans_reader_documents"] = [
        {"id": DOC, "run_id": PLANS_RUN, "name": "E set", "category": "electrical_drawings", "other_type": None,
         "page_count": 300, "status": "done", "page_meta": meta, "created_at": NOW.isoformat()},
        {"id": "doc-2", "run_id": PLANS_RUN, "name": "M set", "category": "mechanical_drawings", "other_type": None,
         "page_count": 300, "status": "done", "page_meta": meta, "created_at": (NOW + timedelta(seconds=1)).isoformat()},
    ]
    body = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/documents").json()
    assert body["truncated"] is True
    assert [len(d["sheets"]) for d in body["documents"]] == [300, 100]
    assert body["documents"][0]["page_roles"] == {"plan": 150, "schedule": 150}
    assert "storage_path" not in body["documents"][0]


def test_the_digest_drops_positions_and_pricing_keys(api):
    _human(api)
    counts = [
        {"row_id": "C1", "description": "Duplex receptacle", "quantity": 12, "positions": [[0.1, 0.2]],
         "points": [[0.1, 0.2]], "unit_price": 4.5, "status": "active", "sheet": {"page": 3, "cost_usd": 1}},
        {"row_id": "C2", "description": "GFCI receptacle", "quantity": 3, "status": "superseded", "extended_cost": 9},
        {"row_id": "C3", "description": "Panel 1HD-21", "quantity": 1, "status": "active", "pricing": {"x": 1}},
    ]
    api.db.tables["plans_reader_outputs"] = [
        {"id": "o1", "run_id": PLANS_RUN, "scope": "category", "category": "electrical_drawings", "version": 1,
         "output": {"counts": []}},
        {"id": "o2", "run_id": PLANS_RUN, "scope": "category", "category": "electrical_drawings", "version": 2,
         "output": {"rows_version": 2, "counts": counts, "summary": "A summary"}},
    ]
    base = f"/ai-estimating/runs/{PLANS_RUN}/outputs/electrical_drawings/digest"
    body = api.client.get(f"{base}?section=counts").json()
    assert body["version"] == 2 and body["rows_version"] == 2 and body["total"] == 2
    text = repr(body["rows"])
    for banned in ("positions", "points", "unit_price", "cost_usd", "pricing", "extended_cost"):
        assert banned not in text, banned
    assert body["rows"][0]["sheet"] == {"page": 3}
    assert api.client.get(f"{base}?section=counts&include_superseded=true").json()["total"] == 3
    body = api.client.get(f"{base}?section=counts&q=1hd-21").json()
    assert [r["row_id"] for r in body["rows"]] == ["C3"]
    body = api.client.get(f"{base}?section=counts&limit=1").json()
    assert len(body["rows"]) == 1 and body["truncated"] is True
    assert api.client.get(f"{base}?section=summary").json()["rows"] == [{"summary": "A summary"}]
    res = api.client.get(f"{base}?section=boq")
    assert res.status_code == 400 and _code(res) == "section_invalid"
    res = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/outputs/low_voltage_drawings/digest?section=counts")
    assert res.status_code == 404 and _code(res) == "no_output"
    res = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/outputs/bogus/digest?section=counts")
    assert res.status_code == 404 and _code(res) == "no_output"


def test_the_main_digest_flattens_the_boq(api):
    _human(api)
    boq = {"areas": [{"area": "Level 1", "sections": [{"heading": "GEAR", "groups": [{"name": "Panels", "subgroups": [
        {"name": "LV", "items": [{"description": "Panel A", "quantity": 1, "unit": "EA", "material_cost": 900}]}]}]}]}]}
    api.db.tables["plans_reader_outputs"] = [
        {"id": "o1", "run_id": PLANS_RUN, "scope": "main", "category": None, "version": 1, "output": {"boq": boq}}]
    body = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/outputs/main/digest?section=boq").json()
    assert body["rows"] == [{"area": "Level 1", "section": "GEAR", "group": "Panels", "subgroup": "LV",
                             "description": "Panel A", "quantity": 1, "unit": "EA"}]


def _seed_runs_with_notes(api):
    api.db.tables["plans_reader_runs"] = [
        _run(), _run(id=RUN_B, name="B", created_at=(NOW - timedelta(days=5)).isoformat()),
        _run(id=RUN_C, name="C", created_at=(NOW - timedelta(days=8)).isoformat()),
        _run(id="14141414-1414-4414-8414-141414141414", name="D", created_at=(NOW - timedelta(days=9)).isoformat()),
    ]
    api.db.tables["plans_reader_notes"] = [
        plans_note(), plans_note(id="n2", run_id=RUN_B, body="Older run says 1HD-21 is 225A", title="t" * 400),
        plans_note(id="n3", run_id=RUN_C, body="Third run 1HD-21"),
        plans_note(id="n4", run_id="14141414-1414-4414-8414-141414141414", body="Fourth run 1HD-21"),
    ]


def test_the_notes_search_syncs_at_most_two_runs_and_searches_three(api):
    _human(api)
    _seed_runs_with_notes(api)
    api.state.behind = [PLANS_RUN, RUN_B, RUN_C]
    api.state.stale = {PLANS_RUN: [{"id": PF1}]}
    res = api.client.get(f"/ai-estimating/notes/search?q=1HD-21&project_id={PROJECT}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert api.state.synced == [PLANS_RUN, RUN_B] and body["notes_partial"] is True
    assert [r["id"] for r in body["runs"]] == [PLANS_RUN, RUN_B, RUN_C]
    assert {r["run_id"] for r in body["results"]} == {PLANS_RUN, RUN_B, RUN_C}
    newest = next(r for r in body["results"] if r["run_id"] == PLANS_RUN)
    older = next(r for r in body["results"] if r["run_id"] == RUN_B)
    assert newest["older_run"] is False and older["older_run"] is True and len(older["title"]) == 200
    assert newest["sheet_number"] == "E-201" and newest["run_name"] == "Monument set"
    assert body["stale"] is True
    rpc = [p for name, p in api.db.rpc_calls if name == "search_plans_notes"][-1]
    assert rpc["p_q"] == "1HD-21" and len(rpc["p_run_ids"]) == 3 and rpc["p_kinds"] is None


def test_the_notes_search_by_run_and_its_refusals(api):
    _human(api)
    _seed_runs_with_notes(api)
    body = api.client.get(f"/ai-estimating/notes/search?q=1HD-21&run_id={RUN_B}&kind=page_note,risk").json()
    assert [r["run_id"] for r in body["results"]] == [RUN_B] and body["notes_partial"] is False
    for query, code in (("q=x&run_id=" + RUN_B, "q_invalid"), ("q=" + "a" * 201 + "&run_id=" + RUN_B, "q_invalid"),
                        ("q=panel", "scope_required"), (f"q=panel&run_id={RUN_B}&project_id={PROJECT}", "scope_required"),
                        (f"q=panel&run_id={RUN_B}&kind=bogus", "kind_invalid")):
        res = api.client.get(f"/ai-estimating/notes/search?{query}")
        assert res.status_code == 400 and _code(res) == code, query


def test_the_page_estimate_route_passes_through(api):
    _human(api)
    res = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/read-pages/estimate?document_id={DOC}&pages=14,15")
    assert res.status_code == 200 and res.json()["max_usd"] == "2.13"
    res = api.client.get(f"/ai-estimating/runs/{PLANS_RUN}/read-pages/estimate?document_id={DOC}&pages=a,b")
    assert res.status_code == 400 and _code(res) == "pages_invalid"


def test_the_run_preview_maps_its_refusals(api, monkeypatch):
    _human(api)
    seen = []

    def preview(sb, pid, ids, uid, **k):
        seen.append(ids)
        if ids == ["bad"]:
            raise aie.PreviewRefused("One of the file ids is not valid.", code="file_ids_invalid")
        return {"project": {"id": pid}, "files": [], "hold_usd": "3.00", "problems": []}

    monkeypatch.setattr(aie, "run_preview", preview)
    assert api.client.get(f"/ai-estimating/projects/{PROJECT}/estimate-preview").status_code == 200
    assert api.client.get(f"/ai-estimating/projects/{PROJECT}/estimate-preview?file_ids=all").status_code == 200
    api.client.get(f"/ai-estimating/projects/{PROJECT}/estimate-preview?file_ids={PF1},{PF2}")
    res = api.client.get(f"/ai-estimating/projects/{PROJECT}/estimate-preview?file_ids=bad")
    assert res.status_code == 400 and _code(res) == "file_ids_invalid"
    assert seen == [None, None, [PF1, PF2], ["bad"]]


# ── page read ────────────────────────────────────────────────────────────────


def _read(api, *, approval_id=APPROVAL_ID, max_usd="2.13", question=QUESTION, pages=(14,)):
    return api.client.post(f"/ai-estimating/runs/{PLANS_RUN}/assistant-page-reads", headers={HEADER: approval_id},
                           json={"document_id": DOC, "pages": list(pages), "question": question, "max_usd": max_usd})


def test_a_page_read_holds_opens_a_task_and_sends_the_template(api):
    res = _read(api)
    assert res.status_code == 200, res.text
    body = res.json()
    task = api.db.tables["assistant_tasks"][0]
    hold = _holds(api)[0]
    assert body["task_id"] == task["id"] and body["run_id"] == PLANS_RUN and body["message_id"]
    assert task["kind"] == "plans_read_pages" and task["status"] == "running" and task["approval_id"] == APPROVAL_ID
    assert task["spend_reservation_id"] == hold["id"] and task["conversation_id"] == CONV
    assert task["message_id"] == MSG_ASSISTANT
    assert task["payload"]["plans_run_id"] == PLANS_RUN and task["payload"]["pages"] == [14]
    assert task["payload"]["plans_message_id"] == body["message_id"] and task["payload"]["thread_id"] == body["thread_id"]
    assert hold["kind"] == "plans_pages" and hold["status"] == "active" and hold["approval_id"] == APPROVAL_ID
    assert hold["task_id"] == task["id"] and Decimal(str(hold["amount_usd"])) == Decimal("2.13")
    assert api.state.calls["create_thread"] == ["Assistant page reads"]
    (_, kwargs), = api.state.calls["send_message"]
    assert kwargs["pro_max"] is True
    assert kwargs["content"] == (f"Read document {DOC} pages 14 with read_pages and answer this question from what the "
                                 f"pages show. Cite sheet numbers. Question: {QUESTION}")
    assert api.state.accesses == [{"action": "view", "source": "ai_estimating_document", "file_id": DOC,
                                   "project_id": PROJECT, "filename": "E-201.pdf",
                                   "detail": {"assistant": True, "pages": [14], "run_id": PLANS_RUN}}]
    assert api.state.calls["available"] == [False]


def test_a_page_read_replay_answers_the_same_and_spends_nothing_more(api):
    first = _read(api).json()
    again = _read(api)
    assert again.status_code == 200 and again.headers["X-Idempotent-Replay"] == "true"
    assert again.json() == first
    assert len(_holds(api)) == 1 and len(api.state.calls["send_message"]) == 1
    assert len(api.db.tables["assistant_tasks"]) == 1 and len(api.db.tables["plans_reader_chat_threads"]) == 1


def test_an_orphan_hold_of_the_same_approval_is_reused(api):
    api.db.tables["assistant_spend_reservations"].append({
        "id": "hold-orphan", "user_id": USER, "kind": "plans_pages", "run_id": RUN, "task_id": "task-orphan-id",
        "approval_id": APPROVAL_ID, "amount_usd": 2.13, "status": "active", "actual_usd": None,
        "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=4)).isoformat(), "settled_at": None,
    })
    res = _read(api)
    assert res.status_code == 200, res.text
    assert len(_holds(api)) == 1
    task = api.db.tables["assistant_tasks"][0]
    assert task["id"] == "task-orphan-id" and task["spend_reservation_id"] == "hold-orphan"


@pytest.mark.parametrize("approved,expected", [("2.00", "2.13"), ("3.00", "3.00")])
def test_the_hold_is_the_larger_of_approved_and_re_estimated(api, approved, expected):
    assert _read(api, max_usd=approved).status_code == 200
    assert Decimal(str(_holds(api)[0]["amount_usd"])) == Decimal(expected)


def test_a_re_estimate_past_ten_percent_is_estimate_changed(api):
    res = _read(api, max_usd="1.90")
    assert res.status_code == 409 and _code(res) == "estimate_changed"
    assert res.json()["detail"]["max_usd"] == "2.13" and res.json()["detail"]["approved_usd"] == "1.90"
    assert _holds(api) == [] and api.db.tables["assistant_tasks"] == []


def test_a_question_asking_for_a_change_is_refused_and_an_orphan_hold_released(api):
    api.db.tables["assistant_spend_reservations"].append({
        "id": "hold-orphan", "user_id": USER, "kind": "plans_pages", "run_id": RUN, "task_id": None,
        "approval_id": APPROVAL_ID, "amount_usd": 2.13, "status": "active", "actual_usd": None,
        "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=4)).isoformat(), "settled_at": None,
    })
    res = _read(api, question="Please update the BOQ to add two more panels")
    assert res.status_code == 400 and _code(res) == "question_asks_for_change"
    assert api.db.tables["assistant_tasks"] == [] and "send_message" not in api.state.calls
    assert _holds(api)[0]["status"] == "released" and Decimal(str(_holds(api)[0]["actual_usd"])) == 0


@pytest.mark.parametrize("problem,status,code", [
    ("pages_invalid", 400, "pages_invalid"), ("too_many_pages", 400, "pages_invalid"),
    ("long_context", 400, "long_context"), ("run_not_finished", 409, "run_not_finished"),
])
def test_estimate_problems_refuse_before_any_hold(api, problem, status, code):
    api.state.estimate = _estimate(problems=[{"code": problem, "message": "no"}])
    res = _read(api)
    assert res.status_code == status and _code(res) == code
    assert _holds(api) == [] and api.db.tables["assistant_tasks"] == []


def test_the_caps_refuse_with_their_codes(api):
    api.state.estimate = _estimate(ai_estimating={"cap_usd": "75.00", "remaining_usd": "2.13", "blocked": False})
    res = _read(api)
    assert res.status_code == 409 and _code(res) == "ai_estimating_spend_cap"
    api.state.estimate = _estimate()
    api.db.tables["assistant_profiles"][0]["daily_cost_cap_usd"] = 2
    res = _read(api)
    assert res.status_code == 409 and _code(res) == "assistant_spend_cap" and res.json()["detail"]["scope"] == "user"
    assert _holds(api) == [] and api.db.tables["assistant_tasks"] == []


def test_an_unavailable_ae_is_503_before_any_hold(api):
    api.state.available = HTTPException(503, "AI Estimating is not configured on this server.")
    res = _read(api)
    assert res.status_code == 503 and _code(res) == "ae_unavailable"
    assert _holds(api) == [] and api.db.tables["assistant_tasks"] == []


def _assert_released_and_failed(api, code):
    hold = _holds(api)[0]
    task = api.db.tables["assistant_tasks"][0]
    assert hold["status"] == "released" and hold["settled_at"] is not None
    assert task["status"] == "failed" and task["failure_reason"] == code and task["payload"]["error"]["code"] == code


def test_a_failed_send_releases_the_hold_and_fails_the_task(api, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(chat, "send_message", boom)
    res = _read(api)
    assert res.status_code == 500 and _code(res) == "route_failed"
    _assert_released_and_failed(api, "route_failed")
    again = _read(api)
    assert again.status_code == 409 and _code(again) == "route_failed"
    assert len(_holds(api)) == 1


def test_a_failed_thread_create_releases_the_hold(api, monkeypatch):
    monkeypatch.setattr(chat, "create_thread", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert _read(api).status_code == 500
    _assert_released_and_failed(api, "route_failed")


def test_an_ae_refusal_of_the_send_releases_the_hold(api, monkeypatch):
    monkeypatch.setattr(chat, "send_message", lambda *a, **k: (_ for _ in ()).throw(ValueError("Type a message first.")))
    res = _read(api)
    assert res.status_code == 409 and _code(res) == "ae_refused"
    _assert_released_and_failed(api, "ae_refused")


def test_one_pending_answer_per_thread(api):
    assert _read(api).status_code == 200
    api.db.tables["assistant_approvals"].append(approval(
        id=OTHER_APPROVAL, action_kind="plans_read_pages", status="executing",
        payload={"v": 1, "args": {"run_id": PLANS_RUN, "document_id": DOC}}))
    api.actor.actor = (USER, RUN, OTHER_APPROVAL)
    res = _read(api, approval_id=OTHER_APPROVAL)
    assert res.status_code == 409 and _code(res) == "page_read_busy"
    assert len(api.db.tables["plans_reader_chat_threads"]) == 1
    second = next(t for t in api.db.tables["assistant_tasks"] if t["approval_id"] == OTHER_APPROVAL)
    second_hold = next(h for h in _holds(api) if h["approval_id"] == OTHER_APPROVAL)
    assert second["status"] == "failed" and second["failure_reason"] == "page_read_busy"
    assert second_hold["status"] == "released"
    first_hold = next(h for h in _holds(api) if h["approval_id"] == APPROVAL_ID)
    assert first_hold["status"] == "active"
    # The first answer finishes: the thread takes the next card.
    for m in api.db.tables["plans_reader_chat_messages"]:
        m["status"] = "done"
    api.db.tables["assistant_approvals"].append(approval(
        id="99999999-9999-4999-8999-999999999991", action_kind="plans_read_pages", status="executing",
        payload={"v": 1, "args": {"run_id": PLANS_RUN}}))
    api.actor.actor = (USER, RUN, "99999999-9999-4999-8999-999999999991")
    assert _read(api, approval_id="99999999-9999-4999-8999-999999999991").status_code == 200
    assert len(api.db.tables["plans_reader_chat_threads"]) == 1


def test_a_crash_after_the_send_is_resumed_without_a_second_send(api, monkeypatch):
    real = repo.update_task
    calls = {"n": 0}

    def crash_once(user_id, task_id, patch, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Crash("after send")
        return real(user_id, task_id, patch, **kw)

    monkeypatch.setattr(repo, "update_task", crash_once)
    with pytest.raises(_Crash):
        _read(api)
    task = repo.task_by_approval(USER, APPROVAL_ID)
    assert task is not None and task["status"] == "running" and task["payload"]["plans_message_id"] is None
    res = _read(api)
    assert res.status_code == 200, res.text
    assert len(api.state.calls["send_message"]) == 1 and len(_holds(api)) == 1
    sent = [m for m in api.db.tables["plans_reader_chat_messages"] if m["role"] == "assistant"]
    assert res.json()["message_id"] == sent[0]["id"]


@pytest.fixture
def jobs(api, monkeypatch):
    """The plans_reader_chat job of each sent answer (llm_queue binds its own
    client, so its two calls are faked over the FakeDB)."""
    from app.services import llm_queue

    api.db.tables["llm_jobs"] = []
    real_send = chat.send_message

    def send_message(sb, run, thread, **kw):
        out = real_send(sb, run, thread, **kw)
        api.db.tables["llm_jobs"].append({"id": f"job-{out['assistant_message']['id']}", "status": api.state.job,
                                          "job_type": llm_queue.JOB_PLANS_READER_CHAT,
                                          "target_id": out["assistant_message"]["id"]})
        return out

    def active_job(job_type, target_id):
        return next((j for j in api.db.tables["llm_jobs"] if j["job_type"] == job_type
                     and j["target_id"] == target_id and j["status"] in ("queued", "running")), None)

    def cancel(job_id):
        job = next(j for j in api.db.tables["llm_jobs"] if j["id"] == job_id)
        if job["status"] != "queued":
            return None
        job["status"] = "canceled"
        return dict(job)

    api.state.job = "queued"
    monkeypatch.setattr(chat, "send_message", send_message)
    monkeypatch.setattr(llm_queue, "active_job", active_job)
    monkeypatch.setattr(llm_queue, "cancel", cancel)
    return api


def _fail_update_after_send(monkeypatch, *, times=1):
    real = repo.update_task
    calls = {"n": 0}

    def flaky(user_id, task_id, patch, **kw):
        if "plans_message_id" in (patch.get("payload") or {}) and patch["payload"]["plans_message_id"] \
                and "status" not in patch and calls["n"] < times:
            calls["n"] += 1
            raise RuntimeError("PostgREST 503")
        return real(user_id, task_id, patch, **kw)

    monkeypatch.setattr(repo, "update_task", flaky)


def test_a_failure_after_the_send_cancels_the_queued_answer_and_closes_at_its_cost(jobs, monkeypatch):
    # SPEND-1 / ROUTES-2: the answer job is still queued, so it is canceled.
    api = jobs
    _fail_update_after_send(monkeypatch)
    res = _read(api)
    assert res.status_code == 500 and _code(res) == "route_failed"
    assert "Nothing was charged" not in res.text
    assert api.db.tables["llm_jobs"][0]["status"] == "canceled"
    _assert_released_and_failed(api, "route_failed")
    # The failed task records the message it sent.
    assert api.db.tables["assistant_tasks"][0]["payload"]["plans_message_id"]


def test_a_failure_after_the_send_with_a_claimed_answer_leaves_the_task_open(jobs, monkeypatch):
    # The answer is already running and will bill: finalize settles it.
    api = jobs
    api.state.job = "running"
    _fail_update_after_send(monkeypatch)
    res = _read(api)
    assert res.status_code == 500 and _code(res) == "route_failed"
    assert "Nothing was charged" not in res.text
    task = api.db.tables["assistant_tasks"][0]
    hold = _holds(api)[0]
    assert task["status"] == "running" and hold["status"] == "active"
    sent = next(m for m in api.db.tables["plans_reader_chat_messages"] if m["role"] == "assistant")
    # The second attempt recorded the message on the open task.
    assert task["payload"]["plans_message_id"] == sent["id"]
    assert api.db.tables["llm_jobs"][0]["status"] == "running"


def test_the_page_read_task_stores_its_question_for_the_crash_adoption(api):
    assert _read(api).status_code == 200
    task = api.db.tables["assistant_tasks"][0]
    assert task["payload"]["question"] == QUESTION
    sent = next(m for m in api.db.tables["plans_reader_chat_messages"] if m["role"] == "assistant")
    adopted = aie.adopt_page_read_message(api.db, USER, {**task, "payload": {**task["payload"],
                                                                            "plans_message_id": None}})
    assert adopted == sent["id"]


def test_a_human_and_a_turn_principal_get_404_and_nothing_is_written(api):
    _human(api)
    assert _read(api).status_code == 404
    api.state.via = RUN
    api.actor.actor = (USER, RUN, None)
    assert _read(api).status_code == 404
    assert api.db.tables["assistant_tasks"] == [] and _holds(api) == []


def test_the_approval_must_be_this_tools_executing_card_for_this_run(api):
    api.db.tables["assistant_approvals"][0]["action_kind"] = "run_ai_estimating"
    res = _read(api)
    assert res.status_code == 409 and _code(res) == "approval_mismatch"
    api.db.tables["assistant_approvals"][0].update(action_kind="plans_read_pages",
                                                   payload={"v": 1, "args": {"run_id": RUN_B}})
    assert _code(_read(api)) == "approval_mismatch"
    api.db.tables["assistant_approvals"][0].update(payload={"v": 1, "args": {"run_id": PLANS_RUN}}, status="executed")
    assert _code(_read(api)) == "approval_mismatch"
    assert _holds(api) == []


# ── composite run ────────────────────────────────────────────────────────────


@pytest.fixture
def run_api(api):
    api.db.tables["plans_reader_runs"] = []
    api.db.tables["assistant_approvals"] = [approval(action_kind="run_ai_estimating", status="executing",
                                                     payload={"v": 1, "args": {"project_id": PROJECT}})]
    return api


def _start(api, *, file_ids=(PF1, PF2), hold="3.00", name=None, approval_id=APPROVAL_ID):
    body = {"file_ids": list(file_ids) if file_ids is not None else None, "hold_usd": hold}
    if name is not None:
        body["name"] = name
    return api.client.post(f"/ai-estimating/projects/{PROJECT}/assistant-runs", headers={HEADER: approval_id},
                           json=body)


def _ae_runs(api):
    return api.db.tables["plans_reader_runs"]


def test_a_run_card_creates_fills_estimates_and_starts(run_api):
    api = run_api
    res = _start(api)
    assert res.status_code == 200, res.text
    body = res.json()
    task = api.db.tables["assistant_tasks"][0]
    hold = _holds(api)[0]
    (run,) = _ae_runs(api)
    assert body == {"task_id": task["id"], "run_id": run["id"], "status": "waiting_external", "phase": "started",
                    "run_status": "splitting"}
    assert task["kind"] == "ai_estimating_run" and task["run_id"] == run["id"]
    assert task["payload"]["plans_run_id"] == run["id"] and task["payload"]["phase"] == "started"
    assert hold["kind"] == "ai_estimate" and hold["status"] == "active" and hold["task_id"] == task["id"]
    assert Decimal(str(hold["amount_usd"])) == Decimal("3.00")
    assert run["name"].startswith(f"{NUMBER} assistant run ") and run["source"] == "project"
    assert api.state.calls["add_project_files"][0][1]["ids"] == [PF1, PF2]
    actions = [a[1] for a in api.state.audits]
    assert actions == ["ai_estimating.run_created", "ai_estimating.run_started"]
    assert api.state.audits[0][4]["via"] == "assistant"
    again = _start(api)
    assert again.status_code == 200 and again.json() == body and len(_ae_runs(api)) == 1
    assert len(api.state.calls["start"]) == 1


@pytest.mark.parametrize("crash_at,phase", [
    ("create_run", "reserved"), ("add_project_files", "created"), ("estimate", "filled"), ("start", "estimated"),
])
def test_a_crash_or_timeout_at_each_phase_resumes(run_api, crash_at, phase):
    api = run_api
    api.state.crash_after = crash_at
    with pytest.raises(_Crash):
        _start(api)
    # What the recover read (GET /assistant/tasks/by-approval) finds.
    task = repo.task_by_approval(USER, APPROVAL_ID)
    assert task is not None and task["payload"]["phase"] == phase and task["status"] == "running"
    assert _holds(api)[0]["status"] == "active"
    res = _start(api)
    assert res.status_code == 200, res.text
    assert res.json()["phase"] == "started" and res.json()["status"] == "waiting_external"
    assert len(_ae_runs(api)) == 1 and len(_holds(api)) == 1 and _ae_runs(api)[0]["status"] == "splitting"
    assert len([f for f in api.db.tables["plans_reader_files"]]) == 2
    assert len([a for a in api.state.audits if a[1] == "ai_estimating.run_created"]) == 1


def test_a_second_starts_409_counts_as_started(run_api):
    api = run_api
    api.state.start_exc = runs.RunRefused("This run has already started.", 409)
    res = _start(api)
    assert res.status_code == 200, res.text
    assert res.json()["phase"] == "started" and _holds(api)[0]["status"] == "active"


def test_the_file_cap_refuses_before_any_hold(run_api):
    api = run_api
    ids = [f"17171717-1717-4717-8717-1717171717{i:02d}" for i in range(16)]
    res = _start(api, file_ids=ids)
    assert res.status_code == 400 and _code(res) == "too_many_files"
    assert _holds(api) == [] and api.db.tables["assistant_tasks"] == [] and _ae_runs(api) == []


def test_the_page_cap_cancels_the_run_and_releases_the_hold(run_api):
    api = run_api
    api.state.file_pages = 201
    res = _start(api)
    assert res.status_code == 409 and _code(res) == "too_many_pages"
    assert _ae_runs(api)[0]["status"] == "canceled"
    _assert_released_and_failed(api, "too_many_pages")


def test_estimate_changed_cancels_the_run_and_releases_the_hold(run_api):
    api = run_api
    api.state.ae_estimate = {"cost_usd": 8.0, "cost_usd_max": 10.0, "over_cap": False, "over_run_limit": False}
    res = _start(api)
    assert res.status_code == 409 and _code(res) == "estimate_changed"
    assert res.json()["detail"]["ceiling_usd"] == "12.50" and res.json()["detail"]["hold_usd"] == "3.00"
    assert _ae_runs(api)[0]["status"] == "canceled" and api.state.calls["cancel"] == [_ae_runs(api)[0]["id"]]
    _assert_released_and_failed(api, "estimate_changed")
    assert Decimal(str(_holds(api)[0]["actual_usd"])) == 0
    again = _start(api)
    assert again.status_code == 409 and _code(again) == "estimate_changed" and len(_ae_runs(api)) == 1


def test_within_ten_percent_starts(run_api):
    api = run_api
    api.state.ae_estimate = {"cost_usd": 2.0, "cost_usd_max": 2.64, "over_cap": False, "over_run_limit": False}
    assert _start(api).status_code == 200   # 3.30 is exactly 110 percent of 3.00


def test_the_ae_daily_cap_refuses_at_estimate_and_at_start(run_api):
    from app.services import ai_spend

    api = run_api
    api.state.ae_estimate = {**api.state.ae_estimate, "over_cap": True}
    res = _start(api)
    assert res.status_code == 409 and _code(res) == "ai_estimating_spend_cap"
    _assert_released_and_failed(api, "ai_estimating_spend_cap")
    api.db.tables.update(assistant_tasks=[], assistant_spend_reservations=[], plans_reader_runs=[],
                         plans_reader_files=[])
    api.state.ae_estimate = {**api.state.ae_estimate, "over_cap": False}
    api.state.start_exc = ai_spend.SpendCapReached("cap", cap_usd=Decimal("75"), spent_usd=Decimal("75"))
    res = _start(api)
    assert res.status_code == 409 and _code(res) == "ai_estimating_spend_cap"
    assert _ae_runs(api)[0]["status"] == "canceled"
    _assert_released_and_failed(api, "ai_estimating_spend_cap")


def test_a_run_in_progress_refuses_before_any_hold(run_api):
    api = run_api
    api.db.tables["plans_reader_runs"] = [_run(status="reading", finished_at=None)]
    res = _start(api)
    assert res.status_code == 409 and _code(res) == "run_in_progress"
    assert _holds(api) == [] and api.db.tables["assistant_tasks"] == []


def test_an_unavailable_ae_refuses_the_run_before_any_hold(run_api):
    api = run_api
    api.state.available = HTTPException(503, "The AI job queue is off.")
    res = _start(api)
    assert res.status_code == 503 and _code(res) == "ae_unavailable"
    assert _holds(api) == [] and _ae_runs(api) == []


def test_the_assistant_cap_refuses_the_hold(run_api):
    api = run_api
    res = _start(api, hold="45.00")
    assert res.status_code == 409 and _code(res) == "assistant_spend_cap"
    assert _holds(api) == [] and _ae_runs(api) == []


def test_an_orphan_run_hold_is_reused(run_api):
    api = run_api
    api.db.tables["assistant_spend_reservations"].append({
        "id": "hold-orphan", "user_id": USER, "kind": "ai_estimate", "run_id": RUN, "task_id": "task-orphan",
        "approval_id": APPROVAL_ID, "amount_usd": 3.0, "status": "active", "actual_usd": None,
        "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=12)).isoformat(), "settled_at": None,
    })
    assert _start(api).status_code == 200
    assert len(_holds(api)) == 1 and api.db.tables["assistant_tasks"][0]["id"] == "task-orphan"


def test_null_file_ids_take_the_previews_readable_files(run_api, monkeypatch):
    api = run_api
    monkeypatch.setattr(aie, "run_preview", lambda sb, pid, ids, uid, **k: {"files": [
        {"id": PF1, "problem": None}, {"id": PF2, "problem": "file_unreadable_type"}]})
    assert _start(api, file_ids=None).status_code == 200
    assert api.state.calls["add_project_files"][0][1]["ids"] == [PF1]


def test_a_human_and_a_turn_principal_get_404_on_the_run_route(run_api):
    api = run_api
    _human(api)
    assert _start(api).status_code == 404
    api.state.via = RUN
    api.actor.actor = (USER, RUN, None)
    assert _start(api).status_code == 404
    assert _ae_runs(api) == [] and _holds(api) == []


def test_an_unexpected_failure_cancels_and_releases(run_api, monkeypatch):
    api = run_api
    monkeypatch.setattr(runs, "add_project_files", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("storage")))
    res = _start(api)
    assert res.status_code == 500 and _code(res) == "route_failed"
    assert _ae_runs(api)[0]["status"] == "canceled"
    _assert_released_and_failed(api, "route_failed")


def test_a_failed_cancel_leaves_the_hold_and_the_task_for_the_stranded_sweep(run_api, monkeypatch):
    # SPEND-2: start succeeded, the phase update and the cancel both fail.
    api = run_api
    real = pra._set_phase

    def set_phase(user, task, phase, **kw):
        if phase == "started":
            raise RuntimeError("PostgREST 503")
        return real(user, task, phase, **kw)

    monkeypatch.setattr(pra, "_set_phase", set_phase)
    monkeypatch.setattr(runs, "cancel", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PostgREST 503")))
    res = _start(api)
    assert res.status_code == 500 and _code(res) == "route_failed"
    assert "The hold was released" not in res.text
    task = api.db.tables["assistant_tasks"][0]
    assert task["status"] in ("pending", "running", "waiting_external")
    assert task["payload"]["phase"] == "estimated"
    assert _holds(api)[0]["status"] == "active"
    assert _ae_runs(api)[0]["status"] == "splitting"
    # The stranded-route sweep cancels the run and closes the hold later.
    from app.services import assistant_maintenance, assistant_offboarding

    monkeypatch.setattr(runs, "cancel", lambda sb, run, *, user: _ae_runs(api)[0].update(status="canceled"))
    monkeypatch.setattr(assistant_offboarding, "cancel_ae_run",
                        lambda run_id, user_id: _ae_runs(api)[0].update(status="canceled"))
    monkeypatch.setattr(aie, "run_state", lambda sb, run_id: {"status": _ae_runs(api)[0]["status"],
                                                              "cost_usd": "0.40", "finished_at": None, "error": None})
    task["updated_at"] = (NOW - timedelta(minutes=30)).isoformat()
    assert assistant_maintenance._stranded_route(task, NOW)
    assert assistant_maintenance._clean_stranded_route(USER, task) is True
    assert _holds(api)[0]["status"] == "released" and Decimal(str(_holds(api)[0]["actual_usd"])) == Decimal("0.40")
    assert api.db.tables["assistant_tasks"][0]["status"] == "failed"


# ── AE names called by name ──────────────────────────────────────────────────


def test_the_ae_names_the_router_calls_still_exist():
    import importlib

    fresh_runs = importlib.import_module("app.services.plans_reader_runs")
    for name in ("create_run", "add_project_files", "estimate", "start", "cancel", "TERMINAL_STATUSES",
                 "spine_enabled", "loop_budget_usd_max", "RunRefused"):
        assert hasattr(fresh_runs, name), name
    for name in ("create_thread", "send_message", "asks_for_change", "BUSY_MESSAGE"):
        assert hasattr(chat, name), name
    assert callable(pr._require_available)
    for name in ("page_read_estimate", "run_preview", "unlabeled_split_usd", "run_state", "chat_message_state",
                 "PreviewRefused"):
        assert hasattr(aie, name), name
    for name in ("notes_behind", "sync_run", "stale_files", "KINDS", "TABLE"):
        assert hasattr(notes, name), name
    assert chat.asks_for_change(pra._page_read_content(DOC, [14], QUESTION)) is False
    from app.assistant import spend

    assert (pra.HOLD_PLANS_PAGES, pra.HOLD_AI_ESTIMATE) == (spend.KIND_PLANS_PAGES, spend.KIND_AI_ESTIMATE)
