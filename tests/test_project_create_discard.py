"""Project creation atomicity + the discard cleanup endpoint.

The New Project modal creates the row first and uploads staged files after, so
two windows exist where "creation failed" could still leave a live project:
a mid-endpoint failure (the creation statements auto-commit one by one), and an
abandoned modal after a partial upload. The first is closed by a compensating
delete inside create_project; the second by DELETE /projects/{id}, which only
the creator may call and only while the project is still at the first intake
task. Everything here runs against a fake Supabase — no network.
"""

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import ProjectCreate
from app.routers import projects as pr
from app.services import notifications, workflow

BASE = {
    "name": "Acme Tower",
    "number": "G3-2026-001",
    "internal_bid_at": "2026-08-20T12:00:00Z",
    "invitation_at": "2026-08-01T12:00:00Z",
    "due_from_estimator_at": "2026-08-15T12:00:00Z",
    "due_from_vendors_at": "2026-08-18T12:00:00Z",
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

CREATED_ROW = {
    "id": "p1",
    "name": "Acme Tower",
    "number": "G3-2026-001",
    "current_stage": "intake",
    "abandoned_at": None,
    "actual_bid_at": None,
}


def _user(uid="u1", role=Role.ESTIMATING_ADMIN):
    return CurrentUser(id=uid, email="u@g3.com", role=role, is_active=True)


# ── fake Supabase ──────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, sb, table):
        self._sb = sb
        self._table = table
        self._op = "select"
        self._payload = None
        self._eq_args = []
        self._is_args = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq_args.append((col, val))
        return self

    def is_(self, col, val):
        self._is_args.append((col, val))
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        self._sb.calls.append(
            SimpleNamespace(
                table=self._table, op=self._op, payload=self._payload,
                eq_args=self._eq_args, is_args=self._is_args,
            )
        )
        responder = self._sb.responses.get((self._table, self._op), [])
        data = responder(self._payload) if callable(responder) else responder
        return _FakeResult(data)


class _FakeSupabase:
    def __init__(self, responses):
        self.calls = []
        self.responses = responses

    def table(self, name):
        return _FakeQuery(self, name)


def _setup(monkeypatch, responses):
    fake = _FakeSupabase(responses)
    # The router, the workflow state loader, and audit() each resolve their own
    # module-level get_supabase — point all three at the same fake.
    monkeypatch.setattr(pr, "get_supabase", lambda: fake)
    monkeypatch.setattr(workflow, "get_supabase", lambda: fake)
    monkeypatch.setattr(notifications, "get_supabase", lambda: fake)
    return fake


def _calls(fake, table, op):
    return [c for c in fake.calls if c.table == table and c.op == op]


def _boom(_payload):
    raise RuntimeError("statement failed")


# ── create_project atomicity ───────────────────────────────────────────────


def test_create_success_leaves_no_delete_and_schedules_rescan(monkeypatch):
    fake = _setup(monkeypatch, {
        ("projects", "insert"): [dict(CREATED_ROW)],
        ("stage_events", "insert"): [],
        ("project_category_state", "insert"): [],
        ("audit_log", "insert"): [],
        ("project_category_state", "select"): [],
    })
    background = BackgroundTasks()

    out = pr.create_project(ProjectCreate(**BASE), background, user=_user())

    assert out["id"] == "p1"
    assert _calls(fake, "projects", "delete") == []
    assert len(background.tasks) == 1  # the unknown-email rescan


@pytest.mark.parametrize("failing_table", ["stage_events", "project_category_state"])
def test_create_failure_after_insert_deletes_the_project(monkeypatch, failing_table):
    """A failure in any post-insert statement must not strand a live project:
    the client is told creation failed, so nothing may remain (the row would
    otherwise sit on every dashboard and be emailed about by the reminder
    poller, with its number permanently retired)."""
    fake = _setup(monkeypatch, {
        ("projects", "insert"): [dict(CREATED_ROW)],
        ("stage_events", "insert"): _boom if failing_table == "stage_events" else [],
        ("project_category_state", "insert"): (
            _boom if failing_table == "project_category_state" else []
        ),
        ("audit_log", "insert"): [],
        ("project_category_state", "select"): [],
    })

    with pytest.raises(RuntimeError, match="statement failed"):
        pr.create_project(ProjectCreate(**BASE), BackgroundTasks(), user=_user())

    [deleted] = _calls(fake, "projects", "delete")
    assert ("id", "p1") in deleted.eq_args


def test_create_gc_link_failure_deletes_the_project(monkeypatch):
    fake = _setup(monkeypatch, {
        ("projects", "insert"): [dict(CREATED_ROW)],
        ("project_gcs", "insert"): _boom,
    })
    body = ProjectCreate(**BASE, gcs=[{"gc_id": "gc1"}])

    with pytest.raises(RuntimeError):
        pr.create_project(body, BackgroundTasks(), user=_user())

    [deleted] = _calls(fake, "projects", "delete")
    assert ("id", "p1") in deleted.eq_args


def test_create_compensating_delete_failure_still_raises_original(monkeypatch):
    def _delete_boom(_payload):
        raise RuntimeError("delete also failed")

    _setup(monkeypatch, {
        ("projects", "insert"): [dict(CREATED_ROW)],
        ("stage_events", "insert"): _boom,
        ("projects", "delete"): _delete_boom,
    })

    with pytest.raises(RuntimeError, match="statement failed"):
        pr.create_project(ProjectCreate(**BASE), BackgroundTasks(), user=_user())


# ── discard endpoint ───────────────────────────────────────────────────────


FRESH_PROJECT = {
    "id": "p1",
    "number": "G3-2026-001",
    "created_by": "u1",
    "current_stage": "intake",
    "pm_stage": None,
    "cp_enrolled_at": None,
    "abandoned_at": None,
}


def _fresh_state_rows(task="intake", cat_status="active"):
    return [{
        "category": "intake", "current_task": task, "status": cat_status,
        "owner_role": None, "completed_at": None,
    }]


def test_discard_deletes_conditionally_and_sweeps_storage(monkeypatch):
    fake = _setup(monkeypatch, {
        ("projects", "select"): [dict(FRESH_PROJECT)],
        ("project_category_state", "select"): _fresh_state_rows(),
        ("projects", "delete"): [dict(FRESH_PROJECT)],
        ("audit_log", "insert"): [],
    })
    swept = []
    monkeypatch.setattr(pr.storage, "delete_project_prefix", swept.append)

    pr.discard_project("p1", user=_user())

    [deleted] = _calls(fake, "projects", "delete")
    # The delete re-asserts the guards SQL-side so a teammate's transition
    # landing after the checks makes it match nothing instead of cascading.
    assert ("id", "p1") in deleted.eq_args
    assert ("created_by", "u1") in deleted.eq_args
    assert ("current_stage", "intake") in deleted.eq_args
    assert {c for c, _ in deleted.is_args} == {"pm_stage", "cp_enrolled_at", "abandoned_at"}
    assert swept == ["p1"]
    [logged] = _calls(fake, "audit_log", "insert")
    assert logged.payload["action"] == "project.discard"


def test_discard_conditional_delete_miss_409s(monkeypatch):
    """Guards passed but the row changed before the delete landed (concurrent
    advance/abandon/enroll): the conditional delete matches nothing and the
    request must 409 without sweeping storage or logging an audit entry."""
    fake = _setup(monkeypatch, {
        ("projects", "select"): [dict(FRESH_PROJECT)],
        ("project_category_state", "select"): _fresh_state_rows(),
        ("projects", "delete"): [],
    })
    swept = []
    monkeypatch.setattr(pr.storage, "delete_project_prefix", swept.append)

    with pytest.raises(HTTPException) as exc:
        pr.discard_project("p1", user=_user())
    assert exc.value.status_code == 409
    assert swept == []
    assert _calls(fake, "audit_log", "insert") == []


def test_discard_rejects_non_creator(monkeypatch):
    _setup(monkeypatch, {
        ("projects", "select"): [dict(FRESH_PROJECT)],
    })

    with pytest.raises(HTTPException) as exc:
        pr.discard_project("p1", user=_user(uid="somebody-else"))
    assert exc.value.status_code == 403


def test_discard_rejects_project_past_first_intake_task(monkeypatch):
    fake = _setup(monkeypatch, {
        ("projects", "select"): [dict(FRESH_PROJECT)],
        ("project_category_state", "select"): _fresh_state_rows(task="go_no_go"),
    })

    with pytest.raises(HTTPException) as exc:
        pr.discard_project("p1", user=_user())
    assert exc.value.status_code == 409
    assert _calls(fake, "projects", "delete") == []


def test_discard_rejects_non_intake_stage(monkeypatch):
    _setup(monkeypatch, {
        ("projects", "select"): [dict(FRESH_PROJECT, current_stage="go_no_go")],
        ("project_category_state", "select"): _fresh_state_rows(),
    })

    with pytest.raises(HTTPException) as exc:
        pr.discard_project("p1", user=_user())
    assert exc.value.status_code == 409


def test_discard_rejects_abandoned_project(monkeypatch):
    """Abandon has no stage minimum, so a first-task project can be Withdrawn;
    that record must go through reactivate, never a hard delete."""
    _setup(monkeypatch, {
        ("projects", "select"): [
            dict(FRESH_PROJECT, abandoned_at="2026-08-12T00:00:00Z")
        ],
        ("project_category_state", "select"): _fresh_state_rows(),
    })

    with pytest.raises(HTTPException) as exc:
        pr.discard_project("p1", user=_user())
    assert exc.value.status_code == 409


def test_discard_unknown_project_404s(monkeypatch):
    _setup(monkeypatch, {("projects", "select"): []})

    with pytest.raises(HTTPException) as exc:
        pr.discard_project("nope", user=_user())
    assert exc.value.status_code == 404


def test_discard_storage_failure_never_blocks(monkeypatch):
    fake = _setup(monkeypatch, {
        ("projects", "select"): [dict(FRESH_PROJECT)],
        ("project_category_state", "select"): _fresh_state_rows(),
        ("projects", "delete"): [dict(FRESH_PROJECT)],
        ("audit_log", "insert"): [],
    })

    def _storage_boom(_project_id):
        raise RuntimeError("bucket unavailable")

    monkeypatch.setattr(pr.storage, "delete_project_prefix", _storage_boom)

    pr.discard_project("p1", user=_user())

    [deleted] = _calls(fake, "projects", "delete")
    assert ("id", "p1") in deleted.eq_args
    [logged] = _calls(fake, "audit_log", "insert")
    assert logged.payload["action"] == "project.discard"
