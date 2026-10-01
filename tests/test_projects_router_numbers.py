"""The router half of app-assigned project numbers (docs/RFP_CREATE.md
sections 2, 7 and 8, section 12's second bullet), called as plain functions
on app/routers/projects against an in-memory fake Supabase that simulates the
0052 unique index on lower(btrim(number)) and the next_project_number() rpc.

Pinned:

- POST /projects ignores a client-supplied `number` (ProjectCreate drops the
  key), assigns the server's, and answers with it; two sequential POSTs get
  consecutive numbers; `budgetary: true` yields the B suffix; a number a
  PM-created row already holds is skipped; NUMBER_MAX_TRIES collisions are a
  409 with the sentence; the compensating delete and the rescan task are
  untouched.
- PATCH /projects/{id} `{budgetary: true|false}` renames the number by its
  marker, is a no-op (no update statement) when the marker is already as
  asked, 409s with the sentence on a legacy number, 409s on a rename onto a
  taken number, ignores an old client's `number` key, and keeps refusing to
  clear the name.
- GET /projects/next-number: registered before /{project_id}, writer roles,
  the bidding gate and its own rate limiter; answers `{number,
  budgetary_number}` from the counter row and never advances it.
- _present on the list and detail routes: `rfp_created` and
  `rfp_intake_missing` from one rfp_created_projects `in_()` query per
  request (plus one profiles read for `sender_allowed_by_name`), null when
  the project was not created by the slice, an empty list when nothing is
  missing; the list is computed before the actual-date redaction.
- After a PATCH on a created project: the intake bell is dismissed once
  nothing is missing (and not before), and an actual_bid_at edit clears
  bid_time_unknown.
- missing_intake_fields: the three dates first, then the rubric keys in
  form order, `bid_time` last and only with the flag and an actual date.
- GET /projects/{id}/files marks `from_rfp` off rfp_sandbox_file_id.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.core.deps import CurrentUser, require_writer
from app.core.roles import Role
from app.models.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from app.routers import files as files_mod
from app.routers import projects as pr
from app.services import notifications, project_intake
from app.services import project_numbers as pn

P1 = "5a000000-0000-4000-8000-000000000001"
P2 = "5a000000-0000-4000-8000-000000000002"
LEGACY = "5a000000-0000-4000-8000-00000000000a"

LEGACY_SENTENCE = "This project's number predates automatic numbering and cannot be changed"
NO_FREE_SENTENCE = "No free project number could be assigned; try again"

BASE = {
    "name": "Acme Tower",
    "internal_bid_at": "2026-09-20T19:00:00Z",
    "invitation_at": "2026-09-01T12:00:00Z",
    "due_from_estimator_at": "2026-09-15T12:00:00Z",
    "due_from_vendors_at": "2026-09-18T12:00:00Z",
    "no_bidding_url": True,
    "project_type": "other",
    "owner_type": "other",
    "labor_needed": "other",
    "bid_method": "other",
    "competitor_known": "other",
    "gc_known": "other",
    "subs_needed": "other",
    "est_value_band": "other",
    "scope_fit": "other",
}


# ── Fake Supabase ──────────────────────────────────────────────────────────


def _dup_error():
    return Exception(
        "{'code': '23505', 'message': 'duplicate key value violates unique "
        'constraint "projects_number_unique_idx"\'}'
    )


def _key(number):
    return (number or "").strip().lower()


class _Rpc:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.rpc_calls.append((self.name, self.params))
        row = self.db.tables["project_number_counter"][0]
        row["last"] = pn.next_after(row["last"])
        return SimpleNamespace(data=row["last"])


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._preds = []
        self._negate = False
        self._single = False
        self._orders = []
        self._limit = None
        self._select = None

    @property
    def not_(self):
        self._negate = True
        return self

    def _add(self, pred):
        if self._negate:
            self._negate = False
            self._preds.append(lambda r, p=pred: not p(r))
        else:
            self._preds.append(pred)
        return self

    def select(self, sel="*", **k):
        self._op = "select"
        self._select = sel
        self.db.selects.append((self.table, sel))
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        return self._add(lambda r: r.get(col) == val)

    def neq(self, col, val):
        return self._add(lambda r: r.get(col) != val)

    def in_(self, col, vals):
        vals = list(vals)
        self.db.in_calls.append((self.table, col, vals))
        return self._add(lambda r: r.get(col) in vals)

    def is_(self, col, val):
        want = None if val == "null" else val
        return self._add(lambda r: r.get(col) == want)

    def single(self):
        self._single = True
        return self

    def order(self, col, desc=False, **k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **k):
        self._limit = n
        return self

    def _matches(self, row):
        return all(p(row) for p in self._preds)

    def _check_number_unique(self, number, *, except_id=None):
        if self.table != "projects" or number is None:
            return
        for r in self.db.tables.get("projects", []):
            if r.get("id") != except_id and _key(r.get("number")) == _key(number):
                raise _dup_error()

    def execute(self):
        self.db.calls.append((self.table, self._op, copy.deepcopy(self._payload)))
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            for col, desc in reversed(self._orders):
                hits = sorted(hits, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._limit is not None:
                hits = hits[: self._limit]
            out = [copy.deepcopy(r) for r in hits]
            if self._single:
                return SimpleNamespace(data=(out[0] if out else None))
            return SimpleNamespace(data=out)
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            made = []
            for item in items:
                self._check_number_unique(item.get("number"))
                row = {"id": f"{self.table}-{len(rows) + len(made) + 1}", **item}
                if self.table == "projects":
                    # The database's default timestamps.
                    row.setdefault("created_at", "2026-09-16T17:00:00+00:00")
                    row.setdefault("updated_at", "2026-09-16T17:00:00+00:00")
                made.append(row)
            rows.extend(made)
            return SimpleNamespace(data=[copy.deepcopy(r) for r in made])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    self._check_number_unique(self._payload.get("number"), except_id=r.get("id"))
                    r.update(self._payload)
                    out.append(copy.deepcopy(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            gone = [copy.deepcopy(r) for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=gone)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None, *, last=7203):
        self.tables = {k: [copy.deepcopy(r) for r in v] for k, v in (tables or {}).items()}
        self.tables.setdefault("project_number_counter", [{"id": 1, "last": last}])
        self.selects: list = []
        self.in_calls: list = []
        self.calls: list = []
        self.rpc_calls: list = []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params=None):
        return _Rpc(self, name, params)

    @property
    def last(self):
        return self.tables["project_number_counter"][0]["last"]

    def ops(self, table, op):
        return [c for c in self.calls if c[0] == table and c[1] == op]


# ── Fixtures ───────────────────────────────────────────────────────────────


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _project(pid, number, **over):
    row = {
        "id": pid,
        "name": "Sunrise Elementary Modernization",
        "number": number,
        "current_stage": "go_no_go",
        "current_owner_role": "executive",
        "abandoned_at": None,
        "reverify_return_stage": None,
        "internal_bid_at": None,
        "actual_bid_at": "2026-09-30T07:00:00+00:00",
        "est_start_date": None,
        "est_finish_date": None,
        "invitation_at": "2026-09-10T09:00:00+00:00",
        "labor_time": None,
        "wage_type": None,
        "labor_note": None,
        "due_from_estimator_at": None,
        "due_from_vendors_at": None,
        "notes": None,
        "project_type": None,
        "owner_type": None,
        "labor_needed": None,
        "bid_method": None,
        "competitor_known": None,
        "gc_known": None,
        "subs_needed": None,
        "est_value_band": None,
        "scope_fit": None,
        "created_by": None,
        "created_at": "2026-09-16T10:00:00+00:00",
        "updated_at": "2026-09-16T10:00:00+00:00",
        "bid_outcomes": [],
    }
    row.update(over)
    return row


def _filled(pid, number, **over):
    """A project with every intake field answered (overrides win)."""
    answered = {
        "internal_bid_at": "2026-09-25T19:00:00+00:00",
        "due_from_estimator_at": "2026-09-20T19:00:00+00:00",
        "due_from_vendors_at": "2026-09-22T19:00:00+00:00",
        **{k: "other" for k in project_intake.RUBRIC_KEYS},
    }
    return _project(pid, number, **{**answered, **over})


def _created_row(pid, **over):
    row = {
        "project_id": pid,
        "automatic": True,
        "sender_was_unauthorized": False,
        "sender_display": "Pat <pm@examplegc.com>",
        "sender_allowed_by": None,
        "files_status": "complete",
        "files_promoted": 12,
        "bid_time_unknown": False,
    }
    row.update(over)
    return row


class _Ingest:
    def __init__(self):
        self.calls = []

    def rfp_match_counts(self, sb, project_ids, post_send_ids):
        self.calls.append(("counts", list(project_ids)))
        return {}


@pytest.fixture()
def env(monkeypatch):
    db = FakeDB(
        {
            "projects": [],
            "project_gcs": [],
            "profiles": [{"id": "u-allow", "full_name": "Dana Admin"}],
            "rfp_created_projects": [],
            "audit_log": [],
            "notifications": [],
        }
    )
    settings = SimpleNamespace(
        rfp_ingest_enabled=True, rfp_ngem_enabled=False, default_rate_limit_per_min=600
    )
    dismissed: list = []
    monkeypatch.setattr(pr, "get_supabase", lambda: db)
    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    monkeypatch.setattr(pr, "get_settings", lambda: settings)
    monkeypatch.setattr(pr, "_ingest", lambda: _Ingest())
    monkeypatch.setattr(pr.workflow, "load_category_states", lambda ids: {i: {} for i in ids})
    monkeypatch.setattr(pr.workflow, "load_category_state", lambda pid: {})
    monkeypatch.setattr(pr, "dismiss_notifications", lambda **kw: dismissed.append(kw))

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(pn, "datetime", _Now)
    return SimpleNamespace(db=db, settings=settings, dismissed=dismissed)


def _create(env, **over):
    body = ProjectCreate(**{**BASE, **over})
    background = BackgroundTasks()
    out = pr.create_project(body, background, user=_user())
    return out, background


# ── Schema ─────────────────────────────────────────────────────────────────


def test_project_create_drops_a_client_number_and_defaults_budgetary_off():
    body = ProjectCreate(**{**BASE, "number": "G3-2026-001"})
    assert "number" not in ProjectCreate.model_fields
    assert "number" not in body.model_dump()
    assert body.budgetary is False
    assert ProjectCreate(**{**BASE, "budgetary": True}).budgetary is True


def test_project_update_drops_a_client_number_and_carries_budgetary():
    assert "number" not in ProjectUpdate.model_fields
    assert ProjectUpdate(**{"number": "x", "name": "y"}).model_dump(exclude_unset=True) == {
        "name": "y"
    }
    assert ProjectUpdate(budgetary=True).model_dump(exclude_unset=True) == {"budgetary": True}
    assert ProjectUpdate().model_dump(exclude_unset=True) == {}


def test_project_out_carries_the_rfp_created_summary_with_null_defaults():
    out = ProjectOut(**_project(P1, "26.9.7204"))
    assert out.rfp_created is None
    assert out.rfp_intake_missing is None
    out = ProjectOut(
        **_project(P1, "26.9.7204"),
        rfp_created={"automatic": True, "files_status": "pending"},
        rfp_intake_missing=[],
    )
    assert out.rfp_created.automatic is True
    assert out.rfp_created.sender_allowed_by_name is None
    assert out.rfp_created.files_promoted == 0
    assert out.rfp_intake_missing == []


# ── POST /projects ─────────────────────────────────────────────────────────


def test_post_ignores_the_client_number_and_answers_with_the_assigned_one(env):
    out, background = _create(env, number="G3-2026-001")
    assert out["number"] == "26.9.7204"
    [row] = env.db.tables["projects"]
    assert row["number"] == "26.9.7204"
    assert "budgetary" not in row
    assert row["created_by"] == "u1" and row["current_stage"] == "intake"
    assert env.db.rpc_calls == [("next_project_number", None)]
    assert env.db.last == 7204
    assert len(background.tasks) == 1
    [logged] = env.db.tables["audit_log"]
    assert logged["action"] == "project.create" and logged["payload"]["number"] == "26.9.7204"
    ProjectOut(**out)


def test_two_sequential_posts_get_consecutive_numbers(env):
    first, _ = _create(env, name="First")
    second, _ = _create(env, name="Second")
    assert (first["number"], second["number"]) == ("26.9.7204", "26.9.7205")
    assert first["id"] != second["id"]


def test_budgetary_true_yields_the_b_suffix(env):
    out, _ = _create(env, budgetary=True)
    assert out["number"] == "26.9.7204B"
    plain, _ = _create(env, name="Second", budgetary=False)
    assert plain["number"] == "26.9.7205"


def test_post_skips_a_number_a_pm_row_already_holds(env):
    env.db.tables["projects"].append(_project(P2, "26.9.7204", current_stage="pm_only"))
    out, _ = _create(env)
    assert out["number"] == "26.9.7205"
    assert len(env.db.rpc_calls) == 2
    # No compensating delete: the first insert never made a row.
    assert env.db.ops("projects", "delete") == []


def test_post_409s_with_the_sentence_after_the_cap(env):
    for i in range(pn.NUMBER_MAX_TRIES):
        env.db.tables["projects"].append(
            _project(f"pm-{i}", f"26.9.{7204 + i:04d}", current_stage="pm_only")
        )
    with pytest.raises(HTTPException) as exc:
        _create(env)
    assert exc.value.status_code == 409
    assert exc.value.detail == NO_FREE_SENTENCE
    assert len(env.db.rpc_calls) == pn.NUMBER_MAX_TRIES
    assert len(env.db.tables["projects"]) == pn.NUMBER_MAX_TRIES
    assert env.db.tables["audit_log"] == []


def test_post_failure_after_the_insert_still_deletes_the_project(env, monkeypatch):
    def _boom(_pid):
        raise RuntimeError("state load failed")

    monkeypatch.setattr(pr.workflow, "load_category_state", _boom)
    with pytest.raises(RuntimeError, match="state load failed"):
        _create(env)
    assert env.db.tables["projects"] == []
    # The counter is spent regardless: numbers are never rewound.
    assert env.db.last == 7204


# ── PATCH /projects/{id} budgetary ────────────────────────────────────────


def _seed(env, number=None, pid=P1, **over):
    row = _filled(pid, number or "26.9.7204", **over)
    env.db.tables["projects"].append(row)
    return row


def _patch(env, body: dict, pid=P1, role=Role.ESTIMATING_ADMIN):
    return pr.update_project(pid, ProjectUpdate(**body), user=_user(role))


def test_patch_budgetary_true_adds_the_marker(env):
    _seed(env, "26.9.7204")
    out = _patch(env, {"budgetary": True})
    assert out["number"] == "26.9.7204B"
    assert env.db.tables["projects"][0]["number"] == "26.9.7204B"
    [(_, _, payload)] = env.db.ops("projects", "update")
    assert payload == {"number": "26.9.7204B"}
    [logged] = env.db.tables["audit_log"]
    assert logged["action"] == "project.update" and logged["payload"] == {"number": "26.9.7204B"}


def test_patch_budgetary_false_strips_the_marker(env):
    _seed(env, "26.9.7204B")
    out = _patch(env, {"budgetary": False})
    assert out["number"] == "26.9.7204"


def test_patch_budgetary_unchanged_is_a_no_op(env):
    _seed(env, "26.9.7204B")
    out = _patch(env, {"budgetary": True})
    assert out["number"] == "26.9.7204B"
    assert env.db.ops("projects", "update") == []
    assert env.db.tables["audit_log"] == []


def test_patch_budgetary_unchanged_still_applies_the_other_fields(env):
    _seed(env, "26.9.7204")
    out = _patch(env, {"budgetary": False, "notes": "hi"})
    assert out["notes"] == "hi" and out["number"] == "26.9.7204"
    [(_, _, payload)] = env.db.ops("projects", "update")
    assert payload == {"notes": "hi"}


def test_patch_budgetary_and_other_fields_travel_in_one_update(env):
    _seed(env, "26.9.7204")
    out = _patch(env, {"budgetary": True, "notes": "hi"})
    assert out["number"] == "26.9.7204B" and out["notes"] == "hi"
    [(_, _, payload)] = env.db.ops("projects", "update")
    assert payload == {"notes": "hi", "number": "26.9.7204B"}


@pytest.mark.parametrize("legacy", ["G3-2026-001", "PM-101", "26.9.720"])
def test_patch_budgetary_on_a_legacy_number_409s_with_the_sentence(env, legacy):
    _seed(env, legacy)
    with pytest.raises(HTTPException) as exc:
        _patch(env, {"budgetary": True})
    assert exc.value.status_code == 409
    assert exc.value.detail == LEGACY_SENTENCE
    assert env.db.ops("projects", "update") == []
    assert env.db.tables["projects"][0]["number"] == legacy


def test_patch_budgetary_onto_a_taken_number_409s(env):
    _seed(env, "26.9.7204")
    env.db.tables["projects"].append(_project(P2, "26.9.7204B", current_stage="pm_only"))
    with pytest.raises(HTTPException) as exc:
        _patch(env, {"budgetary": True})
    assert exc.value.status_code == 409
    assert exc.value.detail == pr._NUMBER_TAKEN


def test_patch_budgetary_on_a_missing_project_404s(env):
    with pytest.raises(HTTPException) as exc:
        _patch(env, {"budgetary": True}, pid=LEGACY)
    assert exc.value.status_code == 404


def test_patch_ignores_an_old_clients_number_key(env):
    _seed(env, "26.9.7204")
    out = pr.update_project(
        P1, ProjectUpdate(**{"number": "27.1.0001", "notes": "hi"}), user=_user()
    )
    assert out["number"] == "26.9.7204" and out["notes"] == "hi"


def test_patch_with_only_a_number_key_is_no_fields(env):
    _seed(env, "26.9.7204")
    with pytest.raises(HTTPException) as exc:
        pr.update_project(P1, ProjectUpdate(**{"number": "27.1.0001"}), user=_user())
    assert exc.value.status_code == 400


def test_patch_still_refuses_to_clear_the_name(env):
    _seed(env, "26.9.7204")
    with pytest.raises(HTTPException) as exc:
        _patch(env, {"name": None})
    assert exc.value.status_code == 400
    assert "name" in exc.value.detail


def test_budgetary_is_an_open_writer_field():
    assert pr._FIELD_EDITORS["budgetary"] == pr._OPEN
    assert "number" not in pr._FIELD_EDITORS


# ── GET /projects/next-number ──────────────────────────────────────────────


def _routes():
    return {(m, r.path): r for r in pr.router.routes for m in r.methods}


def test_next_number_route_is_registered_before_the_parameterised_routes():
    paths = [r.path for r in pr.router.routes]
    assert paths.index("/projects/next-number") < paths.index("/projects/{project_id}")


def test_next_number_route_carries_the_bidding_gate_the_limiter_and_writer():
    route = _routes()[("GET", "/projects/next-number")]
    calls = {d.call for d in route.dependant.dependencies}
    assert pr._BIDDING_ONLY[0].dependency in calls
    assert pr.next_number_rate_limit in calls
    assert require_writer in calls


def test_next_number_previews_without_advancing(env):
    out = pr.next_project_number(_user())
    assert out == {"number": "26.9.7204", "budgetary_number": "26.9.7204B"}
    assert pr.next_project_number(_user()) == out
    assert env.db.rpc_calls == []
    assert env.db.last == 7203
    # A save after the preview gets exactly the previewed number.
    created, _ = _create(env)
    assert created["number"] == out["number"]


def test_next_number_wraps_at_9999(env):
    env.db.tables["project_number_counter"][0]["last"] = 9999
    assert pr.next_project_number(_user()) == {
        "number": "26.9.0001",
        "budgetary_number": "26.9.0001B",
    }


# ── rfp_created / rfp_intake_missing on the list and detail routes ─────────


def test_list_attaches_rfp_created_from_one_query_per_page(env):
    env.db.tables["projects"] = [
        _project(P1, "26.9.7204"),
        _filled(P2, "26.9.7205"),
        _project(LEGACY, "G3-2025-011"),
    ]
    env.db.tables["rfp_created_projects"] = [
        _created_row(P1, sender_was_unauthorized=True, sender_allowed_by="u-allow"),
        _created_row(P2, automatic=False, files_status="pending", files_promoted=0),
    ]
    out = {p["id"]: p for p in pr.list_projects(user=_user())}

    assert out[P1]["rfp_created"] == {
        "automatic": True,
        "sender_was_unauthorized": True,
        "sender_display": "Pat <pm@examplegc.com>",
        "sender_allowed_by_name": "Dana Admin",
        "files_status": "complete",
        "files_promoted": 12,
        "bid_time_unknown": False,
        # The source method and the GC plan (BuildingConnected slice): null
        # on a record that predates them.
        "invitation_method": None,
        "gc_plan": None,
        # The intake modal's source facts: null on a record without them.
        "source_kind": None,
        "mailbox": None,
        "likely_gc_id": None,
        "likely_gc_name": None,
        # The splitter flag is computed by the detail route only.
        "split_issue": None,
    }
    assert out[P1]["rfp_intake_missing"] == [
        "internal_bid_at",
        "due_from_estimator_at",
        "due_from_vendors_at",
        *project_intake.RUBRIC_KEYS,
    ]
    assert out[P2]["rfp_created"]["automatic"] is False
    assert out[P2]["rfp_created"]["files_status"] == "pending"
    assert out[P2]["rfp_intake_missing"] == []
    # Not created by the slice: both null.
    assert "rfp_created" not in out[LEGACY] or out[LEGACY]["rfp_created"] is None
    assert out[LEGACY].get("rfp_intake_missing") is None
    for p in out.values():
        ProjectOut(**p)

    created_queries = [c for c in env.db.in_calls if c[0] == "rfp_created_projects"]
    assert len(created_queries) == 1
    assert set(created_queries[0][2]) == {P1, P2, LEGACY}
    profile_queries = [c for c in env.db.in_calls if c[0] == "profiles"]
    assert profile_queries == [("profiles", "id", ["u-allow"])]


def test_list_skips_the_created_table_while_the_rfp_flag_is_off(env):
    env.settings.rfp_ingest_enabled = False
    env.db.tables["projects"] = [_project(P1, "26.9.7204")]
    env.db.tables["rfp_created_projects"] = [_created_row(P1)]
    [row] = pr.list_projects(user=_user())
    assert row.get("rfp_created") is None
    assert not [s for s in env.db.selects if s[0] == "rfp_created_projects"]


def test_list_issues_no_profiles_read_when_nothing_was_created(env):
    env.db.tables["projects"] = [_project(P1, "26.9.7204")]
    pr.list_projects(user=_user())
    assert not [c for c in env.db.in_calls if c[0] == "profiles"]


def test_detail_attaches_rfp_created_and_the_bid_time_marker(env):
    env.db.tables["projects"] = [_filled(P1, "26.9.7204")]
    env.db.tables["rfp_created_projects"] = [_created_row(P1, bid_time_unknown=True)]
    out = pr.get_project(P1, user=_user())
    assert out["rfp_created"]["bid_time_unknown"] is True
    assert out["rfp_intake_missing"] == ["bid_time"]
    ProjectOut(**out)


def test_detail_drops_bid_time_from_the_missing_list_for_a_role_that_cannot_see_the_date(env):
    """An engineer never sees actual_bid_at, so the list says nothing about
    it either (that its time is unknown would be a fact about a redacted
    field); the other missing fields still show. The viewer roles keep it."""
    env.db.tables["projects"] = [_filled(P1, "26.9.7204", internal_bid_at=None)]
    env.db.tables["rfp_created_projects"] = [_created_row(P1, bid_time_unknown=True)]
    out = pr.get_project(P1, user=_user(Role.ESTIMATING_ENGINEER_MATERIALS))
    assert out["actual_bid_at"] is None
    assert out["rfp_intake_missing"] == ["internal_bid_at"]
    for role in (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN, Role.ACCOUNTANT):
        out = pr.get_project(P1, user=_user(role))
        assert out["rfp_intake_missing"] == ["internal_bid_at", "bid_time"], role
    listed = {p["id"]: p for p in pr.list_projects(user=_user(Role.ESTIMATING_ENGINEER_LABOR))}
    assert listed[P1]["rfp_intake_missing"] == ["internal_bid_at"]


def test_detail_without_a_created_row_leaves_both_null(env):
    env.db.tables["projects"] = [_filled(P1, "26.9.7204")]
    out = pr.get_project(P1, user=_user())
    assert out.get("rfp_created") is None and out.get("rfp_intake_missing") is None


# ── PATCH settles the intake task ──────────────────────────────────────────


def test_patch_that_fills_the_last_field_dismisses_the_intake_bell(env):
    _seed(env, "26.9.7204", internal_bid_at=None)
    env.db.tables["rfp_created_projects"] = [_created_row(P1)]
    _patch(env, {"internal_bid_at": "2026-09-25T19:00:00Z"})
    assert env.dismissed == [{"project_id": P1, "types": ["rfp_create.intake_needed"]}]


def test_patch_that_leaves_something_missing_keeps_the_bell(env):
    _seed(env, "26.9.7204", internal_bid_at=None, scope_fit=None)
    env.db.tables["rfp_created_projects"] = [_created_row(P1)]
    _patch(env, {"internal_bid_at": "2026-09-25T19:00:00Z"})
    assert env.dismissed == []


def test_patch_on_an_ordinary_project_never_dismisses(env):
    _seed(env, "26.9.7204")
    _patch(env, {"notes": "hi"})
    assert env.dismissed == []
    assert env.db.ops("rfp_created_projects", "update") == []


def test_patch_of_the_actual_date_clears_bid_time_unknown_and_settles(env):
    _seed(env, "26.9.7204")
    env.db.tables["rfp_created_projects"] = [_created_row(P1, bid_time_unknown=True)]
    _patch(env, {"actual_bid_at": "2026-09-30T21:00:00Z"}, role=Role.EXECUTIVE)
    assert env.db.tables["rfp_created_projects"][0]["bid_time_unknown"] is False
    assert env.dismissed == [{"project_id": P1, "types": ["rfp_create.intake_needed"]}]


def test_patch_of_another_field_leaves_bid_time_unknown_and_the_bell(env):
    _seed(env, "26.9.7204")
    env.db.tables["rfp_created_projects"] = [_created_row(P1, bid_time_unknown=True)]
    _patch(env, {"notes": "hi"})
    assert env.db.tables["rfp_created_projects"][0]["bid_time_unknown"] is True
    assert env.dismissed == []


def test_intake_settlement_failure_never_fails_the_patch(env, monkeypatch):
    _seed(env, "26.9.7204", internal_bid_at=None)
    env.db.tables["rfp_created_projects"] = [_created_row(P1)]

    def _boom(**kw):
        raise RuntimeError("notifications down")

    monkeypatch.setattr(pr, "dismiss_notifications", _boom)
    out = _patch(env, {"internal_bid_at": "2026-09-25T19:00:00Z"})
    assert out["internal_bid_at"] == "2026-09-25T19:00:00Z"


# ── missing_intake_fields ──────────────────────────────────────────────────


def test_missing_intake_fields_order_dates_then_rubric():
    assert project_intake.missing_intake_fields({}) == [
        "internal_bid_at",
        "due_from_estimator_at",
        "due_from_vendors_at",
        "project_type",
        "owner_type",
        "labor_needed",
        "bid_method",
        "competitor_known",
        "gc_known",
        "subs_needed",
        "est_value_band",
        "scope_fit",
    ]
    assert project_intake.DATE_KEYS == (
        "internal_bid_at",
        "due_from_estimator_at",
        "due_from_vendors_at",
    )
    assert len(project_intake.RUBRIC_KEYS) == 9


def test_missing_intake_fields_keeps_the_fixed_order_whatever_the_dict_order():
    project = {"scope_fit": None, "internal_bid_at": None, "owner_type": "other"}
    project.update({k: "other" for k in project_intake.RUBRIC_KEYS if k != "scope_fit"})
    project.update(due_from_estimator_at="x", due_from_vendors_at="y")
    assert project_intake.missing_intake_fields(project) == ["internal_bid_at", "scope_fit"]


def test_missing_intake_fields_bid_time_is_last_and_needs_the_flag_and_a_date():
    filled = {k: "x" for k in project_intake.DATE_KEYS + project_intake.RUBRIC_KEYS}
    assert project_intake.missing_intake_fields(filled) == []
    assert project_intake.missing_intake_fields(
        {**filled, "actual_bid_at": "2026-09-30T07:00:00+00:00"}, bid_time_unknown=True
    ) == ["bid_time"]
    # A cleared actual date has nothing to time.
    assert project_intake.missing_intake_fields(
        {**filled, "actual_bid_at": None}, bid_time_unknown=True
    ) == []
    assert project_intake.missing_intake_fields(
        {**filled, "actual_bid_at": "2026-09-30T07:00:00+00:00"}, bid_time_unknown=False
    ) == []
    partial = {**filled, "scope_fit": None, "actual_bid_at": "2026-09-30T07:00:00+00:00"}
    assert project_intake.missing_intake_fields(partial, bid_time_unknown=True) == [
        "scope_fit",
        "bid_time",
    ]


def test_intake_complete_mirrors_the_list():
    filled = {k: "x" for k in project_intake.DATE_KEYS + project_intake.RUBRIC_KEYS}
    assert project_intake.intake_complete(filled) is True
    assert project_intake.intake_complete({}) is False
    assert (
        project_intake.intake_complete(
            {**filled, "actual_bid_at": "2026-09-30"}, bid_time_unknown=True
        )
        is False
    )


# ── GET /projects/{id}/files: from_rfp ─────────────────────────────────────


class _ListSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=copy.deepcopy(self._rows))


def test_files_list_marks_from_rfp(monkeypatch):
    monkeypatch.setattr(
        files_mod,
        "get_supabase",
        lambda: _ListSB(
            [
                {"id": "f1", "category": "drawing", "uploaded_by": None,
                 "rfp_sandbox_file_id": "9c000000-0000-4000-8000-000000000001",
                 "sent_to_estimators_at": None},
                {"id": "f2", "category": "drawing", "uploaded_by": "u1",
                 "rfp_sandbox_file_id": None, "sent_to_estimators_at": None},
                {"id": "f3", "category": "other", "uploaded_by": "u1",
                 "sent_to_estimators_at": None},
            ]
        ),
    )
    out = files_mod.list_files("p1", _user())
    assert [(r["id"], r["from_rfp"]) for r in out] == [("f1", True), ("f2", False), ("f3", False)]
