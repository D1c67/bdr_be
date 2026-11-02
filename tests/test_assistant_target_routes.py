"""The human routes the assistant's phase 1 write tools call (docs/
ASSISTANT_PHASE1_PLAN.md 3.11 and 3.14), edited for the assistant:

  provenance   the labor upsert stamps both 0172 columns (assistant inside
               a run, human outside); project, project_gcs, stage_events and
               todos inserts carry them only inside a run;
  create key   POST /projects honors X-Assistant-Idempotency-Key only for a
               bdra_ principal: a replay answers the existing project with
               X-Idempotent-Replay, and so does a 23505 on the 0170 index;
  lookup       GET /projects/assistant-created/{key} serves the caller's own
               project to a bdra_ principal and 404s everyone else;
  PATCH        an assistant patch outside ASSISTANT_PROJECT_FIELDS is a 403
               assistant_field_refused, and it never settles RFP intake;
  labor CAS    X-Assistant-Expected-Updated-At makes the assistant's PUT a
               compare-and-set (409 labor_changed, no audit), a human save
               stays the plain upsert;
  read side    created_by_name on the detail route, no assistant_run_id for
               the estimator, the new todo columns;
  rate limit   inside a run POST /projects/similar spends the ":assistant"
               bucket at half the limit and leaves the human's alone.

Requests go through httpx.ASGITransport inside `acting_as_assistant`, the
way the shim sends them, so the contextvar reaches the sync routes. The
Supabase fake is this file's own (house convention: one per family).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.routing import APIRoute

import app.main
from app.core import provenance, ratelimit
from app.core.deps import CurrentUser, get_current_user
from app.core.error_codes import RateLimitScope
from app.core.roles import Role
from app.models import schemas
from app.models.schemas import ASSISTANT_PROJECT_FIELDS, AssistantProjectUpdate, ProjectUpdate
from app.routers import pricing as pricing_mod
from app.routers import projects as projects_mod
from app.routers import todos as todos_mod
from app.services import gono, project_intake, project_numbers, workflow

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
RUN = "22222222-2222-4222-8222-222222222222"
PROJECT = "77777777-7777-4777-8777-777777777777"
KEY = "33333333-3333-4333-8333-333333333333"
GC = "44444444-4444-4444-8444-444444444444"
STAMP = "2026-10-22T19:00:00.123456+00:00"

ASSISTANT = {"actor_kind": "assistant", "assistant_run_id": RUN}


# ── fake Supabase ─────────────────────────────────────────────────────────


class Unique(Exception):
    code = "23505"

    def __init__(self, index: str):
        super().__init__(f'duplicate key value violates unique constraint "{index}" (23505)')


class _Query:
    def __init__(self, db: "FakeDB", table: str):
        self.db, self.table = db, table
        self.op, self.payload, self.filters = "select", None, []
        self.limit_n, self.one, self.on_conflict = None, False, None
        self.in_filters: list[tuple[str, set]] = []
        self.order_by: tuple[str, bool] | None = None

    def select(self, *a, **k):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, **k):
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, col, vals):
        self.in_filters.append((col, {str(v) for v in vals}))
        return self

    def order(self, col=None, *a, desc=False, **k):
        if col is not None:
            self.order_by = (col, bool(desc))
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def single(self):
        self.one = True
        return self

    def _rows(self):
        rows = [r for r in self.db.tables.setdefault(self.table, [])
                if all(str(r.get(c)) == str(v) for c, v in self.filters)
                and all(str(r.get(c)) in vals for c, vals in self.in_filters)]
        if self.order_by is not None:
            col, desc = self.order_by
            rows = sorted(rows, key=lambda r: str(r.get(col) or ""), reverse=desc)
        return rows[: self.limit_n] if self.limit_n is not None else rows

    def execute(self):
        self.db.calls.append((self.op, self.table, self.payload))
        if self.op == "select":
            rows = [dict(r) for r in self._rows()]
            return SimpleNamespace(data=(rows[0] if rows else None) if self.one else rows)
        if self.op == "insert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            return SimpleNamespace(data=[self.db.insert(self.table, dict(p)) for p in payloads])
        if self.op == "upsert":
            key = self.on_conflict
            existing = next((r for r in self.db.tables.setdefault(self.table, [])
                             if str(r.get(key)) == str(self.payload.get(key))), None)
            if existing is not None:
                existing.update(self.payload)
                return SimpleNamespace(data=[dict(existing)])
            return SimpleNamespace(data=[self.db.insert(self.table, dict(self.payload))])
        if self.op == "update":
            out = []
            for r in self._rows():
                r.update(self.payload)
                out.append(dict(r))
            return SimpleNamespace(data=out)
        raise AssertionError(self.op)


class FakeDB:
    def __init__(self, **tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.calls: list[tuple[str, str, object]] = []
        self.before_insert: dict[str, object] = {}

    def table(self, name):
        return _Query(self, name)

    def insert(self, table: str, row: dict) -> dict:
        hook = self.before_insert.get(table)
        if hook is not None:
            hook(row)
        rows = self.tables.setdefault(table, [])
        key = row.get("assistant_idempotency_key")
        if table == "projects" and key is not None and any(
            r.get("assistant_idempotency_key") == key for r in rows
        ):
            raise Unique("projects_assistant_idempotency_key_uq")
        if table == "labor_reviews" and any(r.get("project_id") == row.get("project_id") for r in rows):
            raise Unique("labor_reviews_pkey")
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", "2026-10-22T18:00:00+00:00")
        if table in ("projects", "labor_reviews"):
            row.setdefault("updated_at", STAMP)
        rows.append(row)
        return dict(row)

    def written(self, op: str, table: str) -> list:
        return [p for o, t, p in self.calls if o == op and t == table]


def project_row(**over) -> dict:
    row = {
        "id": PROJECT, "name": "ZZ TEST Alpha", "number": "26.10.7300",
        "internal_bid_at": "2026-11-01T19:00:00+00:00", "actual_bid_at": None,
        "est_start_date": None, "est_finish_date": None,
        "invitation_at": "2026-10-01T19:00:00+00:00", "labor_time": None, "wage_type": None,
        "labor_note": None, "due_from_estimator_at": "2026-10-28T19:00:00+00:00",
        "due_from_vendors_at": "2026-10-29T19:00:00+00:00", "notes": None, "address": None,
        "current_stage": "intake", "current_owner_role": "estimating_admin",
        "abandoned_at": None, "failed_to_bid_at": None, "created_by": USER,
        "created_at": "2026-10-22T18:00:00+00:00", "updated_at": STAMP,
        "actor_kind": "human", "assistant_run_id": None, "assistant_idempotency_key": None,
    }
    row.update({k: "other" for k in project_intake.RUBRIC_KEYS})
    row.update(over)
    return row


CREATE_BODY = {
    "name": "ZZ TEST Assistant P1",
    "internal_bid_at": "2026-11-01T19:00:00Z",
    "invitation_at": "2026-10-01T19:00:00Z",
    "due_from_estimator_at": "2026-10-28T19:00:00Z",
    "due_from_vendors_at": "2026-10-29T19:00:00Z",
    "no_bidding_url": True,
    **{k: "other" for k in project_intake.RUBRIC_KEYS},
}


# ── harness ───────────────────────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        projects=[project_row()],
        profiles=[{"id": USER, "full_name": "Tom Moore"}],
        general_contractors=[{"id": GC, "name": "Alpha Builders"}],
    )
    for mod in (projects_mod, pricing_mod, todos_mod):
        monkeypatch.setattr(mod, "get_supabase", lambda: db)
    audits: list[tuple] = []
    monkeypatch.setattr(projects_mod, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(pricing_mod, "audit", lambda *a, **k: audits.append(a))
    reopened: list[str] = []
    monkeypatch.setattr(workflow, "maybe_reopen_verify_after_edit",
                        lambda pid, *a, **k: reopened.append(pid))
    monkeypatch.setattr(workflow, "load_category_state", lambda pid: {})
    monkeypatch.setattr(project_numbers, "assign", lambda sb, budgetary=False: "26.10.7301")
    monkeypatch.setattr(projects_mod.email_ingest, "rescan_unknown_for_project", lambda pid: None)
    monkeypatch.setattr(projects_mod, "_project_gc_rows", lambda pid: [])
    monkeypatch.setattr(projects_mod, "_rfp_created_rows", lambda ids, **k: {})
    monkeypatch.setattr(projects_mod, "_rfp_match_counts", lambda *a, **k: {})
    monkeypatch.setattr(projects_mod, "_portal_sources", lambda ids: {})
    monkeypatch.setattr(projects_mod, "_pending_gc_pricing_counts", lambda ids: {})
    monkeypatch.setattr(projects_mod, "_auto_send_pending_ids", lambda ids: set())
    who = {"role": Role.EXECUTIVE, "id": USER, "run": None}

    def _user():
        return CurrentUser(id=who["id"], email="tom@g3.test", role=who["role"], is_active=True,
                           via_assistant_run_id=who["run"])

    app.main.app.dependency_overrides[get_current_user] = _user

    def call(method: str, path: str, *, assistant: bool = False, **kw) -> httpx.Response:
        async def go():
            transport = httpx.ASGITransport(app=app.main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://bdr.test") as c:
                return await c.request(method, path, **kw)

        if not assistant:
            who["run"] = None
            return asyncio.run(go())
        who["run"] = RUN
        try:
            with provenance.acting_as_assistant(who["id"], RUN):
                return asyncio.run(go())
        finally:
            who["run"] = None

    yield SimpleNamespace(call=call, db=db, who=who, audits=audits, reopened=reopened)
    app.main.app.dependency_overrides.pop(get_current_user, None)


def _provenance(row: dict) -> dict:
    return {k: row[k] for k in ("actor_kind", "assistant_run_id") if k in row}


# ── the assistant allowlist ───────────────────────────────────────────────


def test_assistant_project_fields_are_exactly_the_seventeen():
    assert ASSISTANT_PROJECT_FIELDS == frozenset({
        "name", "address", "est_start_date", "est_finish_date", "invitation_at", "job_walk_at",
        "labor_time", "wage_type", "labor_note", "notes", "bid_notes", "bidding_url",
        "no_bidding_url", "is_ngem", "is_rebid", "project_information", "trade_instructions",
    })
    assert not ASSISTANT_PROJECT_FIELDS & set(project_intake.RUBRIC_KEYS)
    assert not ASSISTANT_PROJECT_FIELDS & set(project_intake.DATE_KEYS)
    assert "actual_bid_at" not in ASSISTANT_PROJECT_FIELDS
    assert "budgetary" not in ASSISTANT_PROJECT_FIELDS
    # Same types as ProjectUpdate, so the route's own parsing is the contract.
    for name in ASSISTANT_PROJECT_FIELDS:
        assert (AssistantProjectUpdate.model_fields[name].annotation
                == ProjectUpdate.model_fields[name].annotation), name


def test_assistant_project_update_validates_like_project_update():
    with pytest.raises(ValueError):
        AssistantProjectUpdate(internal_bid_at="2026-11-01T19:00:00Z")  # extra="forbid"
    with pytest.raises(ValueError):
        AssistantProjectUpdate(name="")
    with pytest.raises(ValueError):
        AssistantProjectUpdate(bidding_url="javascript:alert(1)")
    with pytest.raises(ValueError):
        AssistantProjectUpdate(project_information="x" * (schemas.PROJECT_TEXT_MAX_CHARS + 1))
    assert AssistantProjectUpdate(bidding_url="example.com/bid").bidding_url == "https://example.com/bid"


def test_project_out_carries_the_provenance_fields():
    fields = schemas.ProjectOut.model_fields
    assert fields["actor_kind"].default == "human"
    assert fields["assistant_run_id"].default is None
    assert fields["created_by_name"].default is None


# ── labor ─────────────────────────────────────────────────────────────────


LABOR = {"labor_amount": "12500.00", "labor_notes": "phase one", "verified": False}


def test_labor_upsert_stamps_assistant_inside_a_run_and_human_outside(api):
    res = api.call("PUT", f"/projects/{PROJECT}/labor", json=LABOR, assistant=True)
    assert res.status_code == 200, res.text
    assert _provenance(api.db.tables["labor_reviews"][0]) == ASSISTANT
    # No header: still the plain upsert, even for the assistant.
    assert [op for op, t, _ in api.db.calls if t == "labor_reviews"] == ["upsert"]
    res = api.call("PUT", f"/projects/{PROJECT}/labor", json=LABOR)
    assert res.status_code == 200, res.text
    assert _provenance(api.db.tables["labor_reviews"][0]) == {
        "actor_kind": "human", "assistant_run_id": None}
    assert len(api.audits) == 2 and api.reopened == [PROJECT, PROJECT]


def test_labor_compare_and_set_updates_on_a_matching_updated_at(api):
    api.db.tables["labor_reviews"] = [{"project_id": PROJECT, "labor_amount": 10000,
                                       "updated_at": STAMP, "actor_kind": "human"}]
    res = api.call("PUT", f"/projects/{PROJECT}/labor", json=LABOR, assistant=True,
                   headers={pricing_mod.LABOR_EXPECTED_HEADER: STAMP})
    assert res.status_code == 200, res.text
    [row] = api.db.tables["labor_reviews"]
    assert row["labor_amount"] == "12500.00" and _provenance(row) == ASSISTANT
    assert [op for op, t, _ in api.db.calls if t == "labor_reviews"] == ["update"]
    assert len(api.audits) == 1 and api.reopened == [PROJECT]


def test_labor_compare_and_set_refuses_a_stale_updated_at(api):
    api.db.tables["labor_reviews"] = [{"project_id": PROJECT, "labor_amount": 10000,
                                       "updated_at": "2026-10-22T20:00:00+00:00"}]
    res = api.call("PUT", f"/projects/{PROJECT}/labor", json=LABOR, assistant=True,
                   headers={pricing_mod.LABOR_EXPECTED_HEADER: STAMP})
    assert res.status_code == 409
    assert res.headers["x-error-code"] == "labor_changed"
    assert res.json()["detail"]["code"] == "labor_changed"
    assert api.db.tables["labor_reviews"][0]["labor_amount"] == 10000
    assert api.audits == [] and api.reopened == []


def test_labor_none_inserts_and_refuses_when_a_row_exists(api):
    res = api.call("PUT", f"/projects/{PROJECT}/labor", json=LABOR, assistant=True,
                   headers={pricing_mod.LABOR_EXPECTED_HEADER: "none"})
    assert res.status_code == 200, res.text
    assert [op for op, t, _ in api.db.calls if t == "labor_reviews"] == ["insert"]
    res = api.call("PUT", f"/projects/{PROJECT}/labor", json=LABOR, assistant=True,
                   headers={pricing_mod.LABOR_EXPECTED_HEADER: "none"})
    assert res.status_code == 409 and res.headers["x-error-code"] == "labor_changed"
    assert len(api.audits) == 1


def test_a_human_with_the_header_still_upserts(api):
    api.db.tables["labor_reviews"] = [{"project_id": PROJECT, "labor_amount": 10000,
                                       "updated_at": "2026-10-22T20:00:00+00:00"}]
    res = api.call("PUT", f"/projects/{PROJECT}/labor", json=LABOR,
                   headers={pricing_mod.LABOR_EXPECTED_HEADER: STAMP})
    assert res.status_code == 200, res.text
    assert [op for op, t, _ in api.db.calls if t == "labor_reviews"] == ["upsert"]
    assert api.db.tables["labor_reviews"][0]["labor_amount"] == "12500.00"


# ── POST /projects ────────────────────────────────────────────────────────


def _create(api, *, assistant: bool, key: str | None = KEY, gcs=None) -> httpx.Response:
    headers = {projects_mod.ASSISTANT_IDEMPOTENCY_HEADER: key} if key else {}
    body = {**CREATE_BODY, "gcs": gcs or []}
    return api.call("POST", "/projects", json=body, headers=headers, assistant=assistant)


def test_create_inside_a_run_stamps_every_row_and_stores_the_key(api):
    res = _create(api, assistant=True, gcs=[{"gc_id": GC}])
    assert res.status_code == 201, res.text
    assert "x-idempotent-replay" not in res.headers
    body = res.json()
    assert body["actor_kind"] == "assistant" and body["assistant_run_id"] == RUN
    [project] = api.db.written("insert", "projects")
    assert _provenance(project) == ASSISTANT and project["created_by"] == USER
    assert project["assistant_idempotency_key"] == KEY
    [[link]] = api.db.written("insert", "project_gcs")
    assert _provenance(link) == ASSISTANT
    [event] = api.db.written("insert", "stage_events")
    assert _provenance(event) == ASSISTANT and event["actor_id"] == USER


def test_create_outside_a_run_ignores_the_key_and_sends_no_provenance(api):
    for _ in range(2):
        res = _create(api, assistant=False, gcs=[{"gc_id": GC}])
        assert res.status_code == 201, res.text
    projects = api.db.written("insert", "projects")
    assert len(projects) == 2                       # the header did not dedupe
    for row in projects:
        assert "assistant_idempotency_key" not in row and _provenance(row) == {}
    assert all(_provenance(r) == {} for [r] in api.db.written("insert", "project_gcs"))
    assert all(_provenance(r) == {} for r in api.db.written("insert", "stage_events"))


def test_create_replay_answers_the_existing_project(api):
    first = _create(api, assistant=True)
    assert first.status_code == 201, first.text
    again = _create(api, assistant=True)
    assert again.status_code == 201, again.text
    assert again.headers["x-idempotent-replay"] == "true"
    assert again.json()["id"] == first.json()["id"]
    assert len(api.db.written("insert", "projects")) == 1
    assert len(api.db.written("insert", "stage_events")) == 1


def test_create_with_a_non_uuid_key_is_an_ordinary_create(api):
    for _ in range(2):
        assert _create(api, assistant=True, key="not-a-uuid").status_code == 201
    rows = api.db.written("insert", "projects")
    assert len(rows) == 2 and all("assistant_idempotency_key" not in r for r in rows)


def test_create_race_on_the_key_index_answers_the_winner(api):
    winner = project_row(id=str(uuid.uuid4()), number="26.10.7299", assistant_idempotency_key=KEY,
                         actor_kind="assistant", assistant_run_id=RUN)

    def _race(row):
        # The concurrent create commits between our lookup and our insert.
        api.db.before_insert.pop("projects")
        api.db.tables["projects"].append(dict(winner))

    api.db.before_insert["projects"] = _race
    res = _create(api, assistant=True)
    assert res.status_code == 201, res.text
    assert res.headers["x-idempotent-replay"] == "true"
    assert res.json()["id"] == winner["id"]
    assert api.db.written("insert", "stage_events") == []


def test_create_ignores_another_users_key(api):
    api.db.tables["projects"].append(project_row(
        id=str(uuid.uuid4()), created_by=OTHER, assistant_idempotency_key=KEY))
    api.db.before_insert["projects"] = lambda row: None
    # Same key under another creator: the lookup misses, the insert collides,
    # the re-lookup misses again, so the error propagates as a 500.
    with pytest.raises(Exception, match="projects_assistant_idempotency_key_uq"):
        _create(api, assistant=True)


# ── GET /projects/assistant-created/{key} ─────────────────────────────────


def test_assistant_created_lookup_serves_only_the_callers_own_project(api):
    api.db.tables["projects"].append(project_row(
        id="p-mine", number="26.10.7302", name="Mine", assistant_idempotency_key=KEY))
    other_key = str(uuid.uuid4())
    api.db.tables["projects"].append(project_row(
        id="p-theirs", created_by=OTHER, assistant_idempotency_key=other_key))
    res = api.call("GET", f"/projects/assistant-created/{KEY}", assistant=True)
    assert res.status_code == 200, res.text
    assert res.json() == {"id": "p-mine", "number": "26.10.7302", "name": "Mine"}
    assert api.call("GET", f"/projects/assistant-created/{KEY}").status_code == 404
    assert api.call("GET", f"/projects/assistant-created/{other_key}", assistant=True).status_code == 404
    assert api.call("GET", "/projects/assistant-created/nope", assistant=True).status_code == 404
    assert api.call("GET", f"/projects/assistant-created/{uuid.uuid4()}",
                    assistant=True).status_code == 404


def test_assistant_created_is_declared_above_the_project_id_routes():
    order = [r.path_format for r in app.main.app.routes
             if isinstance(r, APIRoute) and "GET" in (r.methods or ())]
    assert order.index("/projects/assistant-created/{key}") < order.index("/projects/{project_id}")


# ── PATCH /projects/{id} ──────────────────────────────────────────────────


@pytest.mark.parametrize("patch", [
    {"internal_bid_at": "2026-11-02T19:00:00Z"},
    {"project_type": "other"},
    {"budgetary": True},
    {"actual_bid_at": "2026-11-02T19:00:00Z"},
    {"notes": "fine", "due_from_vendors_at": "2026-11-02T19:00:00Z"},
])
def test_assistant_patch_outside_the_allowlist_is_refused(api, patch):
    res = api.call("PATCH", f"/projects/{PROJECT}", json=patch, assistant=True)
    assert res.status_code == 403
    assert res.headers["x-error-code"] == "assistant_field_refused"
    assert res.json()["detail"].startswith("The assistant may not edit: ")
    assert api.db.written("update", "projects") == [] and api.audits == []


def _rfp_created(api, monkeypatch) -> list:
    went = []
    monkeypatch.setattr(projects_mod, "_rfp_created_rows",
                        lambda ids, **k: {PROJECT: {"bid_time_unknown": False}})
    monkeypatch.setattr(projects_mod, "dismiss_notifications", lambda **k: None)

    def _go(pid, actor):
        went.append(pid)
        api.db.tables["projects"][0]["current_stage"] = "send_out"
        return "go"

    monkeypatch.setattr(gono, "auto_go_if_scored", _go)
    return went


def test_assistant_patch_never_settles_rfp_intake(api, monkeypatch):
    went = _rfp_created(api, monkeypatch)
    res = api.call("PATCH", f"/projects/{PROJECT}", assistant=True,
                   json={"address": "1 Main St", "job_walk_at": "2026-10-30T17:00:00Z"})
    assert res.status_code == 200, res.text
    assert went == [] and res.json()["current_stage"] == "intake"
    assert api.db.tables["projects"][0]["address"] == "1 Main St"
    assert len(api.audits) == 1


def test_a_human_patch_still_settles_rfp_intake(api, monkeypatch):
    went = _rfp_created(api, monkeypatch)
    res = api.call("PATCH", f"/projects/{PROJECT}", json={"address": "1 Main St"})
    assert res.status_code == 200, res.text
    assert went == [PROJECT] and res.json()["current_stage"] == "send_out"


def test_a_human_may_still_patch_the_go_no_go_answers(api):
    res = api.call("PATCH", f"/projects/{PROJECT}", json={"project_type": "other"})
    assert res.status_code == 200, res.text


# ── POST /projects/{id}/gcs ───────────────────────────────────────────────


def test_add_project_gc_stamps_the_link_only_inside_a_run(api):
    res = api.call("POST", f"/projects/{PROJECT}/gcs", json={"gc_id": GC}, assistant=True)
    assert res.status_code == 201, res.text
    [link] = api.db.written("insert", "project_gcs")
    assert _provenance(link) == ASSISTANT
    api.db.tables["project_gcs"] = []
    assert api.call("POST", f"/projects/{PROJECT}/gcs", json={"gc_id": GC}).status_code == 201
    assert _provenance(api.db.written("insert", "project_gcs")[1]) == {}


def test_add_project_gc_checks_the_contacts_before_linking(api):
    """A contact that is gone (or belongs to another GC) is refused before
    the link insert, so a 400 never leaves the GC linked with no contacts
    and no audit row."""
    gone = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c0c0"
    api.db.tables["project_gcs"] = []
    res = api.call("POST", f"/projects/{PROJECT}/gcs", json={"gc_id": GC, "contact_ids": [gone]}, assistant=True)
    assert res.status_code == 400, res.text
    assert api.db.written("insert", "project_gcs") == [] and api.db.tables["project_gcs"] == []


# ── GET /projects/{id} and the estimator ──────────────────────────────────


def test_get_project_fills_created_by_name_and_provenance(api):
    api.db.tables["projects"][0].update(actor_kind="assistant", assistant_run_id=RUN)
    body = api.call("GET", f"/projects/{PROJECT}").json()
    assert body["created_by_name"] == "Tom Moore"
    assert body["actor_kind"] == "assistant" and body["assistant_run_id"] == RUN
    assert "assistant_idempotency_key" not in body
    api.db.tables["profiles"] = []
    assert api.call("GET", f"/projects/{PROJECT}").json()["created_by_name"] is None


def test_the_estimator_never_sees_the_run_id():
    row = project_row(actor_kind="assistant", assistant_run_id=RUN)
    seen = projects_mod._present(dict(row), Role.ESTIMATOR)
    assert "assistant_run_id" not in seen and seen["actor_kind"] == "assistant"
    assert projects_mod._present(dict(row), Role.EXECUTIVE)["assistant_run_id"] == RUN


# ── todos ─────────────────────────────────────────────────────────────────


def test_todo_select_lists_the_new_columns():
    for col in ("project_id", "due_at", "source", "actor_kind"):
        assert col in todos_mod._SELECT
    assert "assistant_run_id" not in todos_mod._SELECT


def test_todo_list_narrows_by_id_and_orders_newest_first(api):
    """The assistant's to-do tools read one to-do by id and list newest
    first, so the route's 500-row window never hides a new to-do."""
    api.db.tables["todos"] = [
        {"id": f"00000000-0000-4000-8000-{i:012d}", "user_id": USER, "title": f"t{i}", "is_done": i < 600,
         "created_at": f"2026-01-01T00:00:00.{i:06d}+00:00"} for i in range(601)
    ]
    newest = "00000000-0000-4000-8000-000000000600"
    default = api.call("GET", "/todos").json()
    assert len(default) == 500 and newest not in {r["id"] for r in default}
    recent = api.call("GET", "/todos", params={"newest_first": "true"}).json()
    assert recent[0]["id"] == newest and len(recent) == 500
    one = api.call("GET", "/todos", params={"todo_id": newest}).json()
    assert [r["id"] for r in one] == [newest]


def test_todo_source_comes_from_the_context_never_the_client(api):
    res = api.call("POST", "/todos", json={"title": "call the GC", "source": "assistant",
                                           "project_id": PROJECT})
    assert res.status_code == 201, res.text
    human = api.db.tables["todos"][0]
    assert human["source"] == "user" and human["project_id"] == PROJECT
    assert _provenance(human) == {}
    res = api.call("POST", "/todos", json={"title": "phase one live check"}, assistant=True)
    assert res.status_code == 201, res.text
    mine = api.db.tables["todos"][1]
    assert mine["source"] == "assistant" and mine["project_id"] is None
    assert _provenance(mine) == ASSISTANT
    assert api.call("POST", "/todos", json={"title": "x", "project_id": "nope"}).status_code == 422


# ── rate limiter (3.14) ───────────────────────────────────────────────────


def test_similar_spends_the_assistant_bucket_at_half_the_limit(api, monkeypatch):
    monkeypatch.setattr(ratelimit, "get_settings", lambda: SimpleNamespace(rate_limit_enabled=True))
    monkeypatch.setattr(projects_mod, "get_settings", lambda: SimpleNamespace(
        default_rate_limit_per_min=4, rfp_match_rebid_lookback_days=365,
        rfp_match_precreate_window_days=60, rfp_match_precreate_threshold=0.5,
    ))
    empty = SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
    monkeypatch.setattr(projects_mod, "_match", lambda: SimpleNamespace(
        pg_ts=lambda dt: dt.isoformat(), precreate_query=lambda sb, lo, select: empty))
    ratelimit._buckets.clear()
    try:
        scope = RateLimitScope.DEFAULT
        for _ in range(2):
            res = api.call("POST", "/projects/similar", json={"name": "Alpha"}, assistant=True)
            assert res.status_code == 200, res.text
        res = api.call("POST", "/projects/similar", json={"name": "Alpha"}, assistant=True)
        assert res.status_code == 429
        assert (scope, f"{USER}:assistant") in ratelimit._buckets
        assert (scope, USER) not in ratelimit._buckets
        for _ in range(4):   # the human still has the whole quota
            assert api.call("POST", "/projects/similar", json={"name": "Alpha"}).status_code == 200
        assert api.call("POST", "/projects/similar", json={"name": "Alpha"}).status_code == 429
    finally:
        ratelimit._buckets.clear()
