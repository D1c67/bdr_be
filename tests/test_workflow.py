"""Unit tests for the category workflow state machine.

The pipeline is no longer one linear pointer: the 12 tasks are grouped into four
CATEGORIES (intake / material_numbers / labor_numbers / send_out) that progress
under a DAG. material_numbers + labor_numbers run in parallel once intake
completes; send_out is locked until all three finish. `declined` is a global
kill. The source of truth is the `project_category_state` table (4 rows/project);
`projects.current_stage` is a recomputed headline.

The pure helpers (category_of / next_task_in_category / category_reached / … /
is_category_complete) are tested against hand-built state dicts. The mutations
(advance_category / decline_project) run against a tiny in-memory fake Supabase
that supports the composite (project_id, category) upsert those functions use.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.roles import Role
from app.services import workflow
from app.services.workflow import (
    STAGES,
    category_before,
    category_of,
    category_past,
    category_reached,
    internal_owner_role_for,
    is_category_complete,
    next_task_in_category,
    owner_role_for,
)


# ── Fake Supabase (composite-key aware) ───────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = "select"
        self._payload = None
        self._conflict = None
        self._filters: list[tuple] = []
        self._single = False

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op, self._payload, self._conflict = "upsert", payload, on_conflict
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def _matches(self, row):
        for op, col, val in self._filters:
            if op == "eq":
                if row.get(col) != val:
                    return False
            elif op == "in":
                if row.get(col) not in val:
                    return False
            elif op == "is":
                if val == "null" and row.get(col) is not None:
                    return False
        return True

    def _conflict_match(self, existing, payload):
        keys = [k.strip() for k in self._conflict.split(",")]
        return all(existing.get(k) == payload.get(k) for k in keys)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._single:
                return SimpleNamespace(data=(dict(hits[0]) if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payloads:
                rows.append(dict(p))
            return SimpleNamespace(data=[dict(p) for p in payloads])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "upsert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                existing = None
                if self._conflict:
                    existing = next(
                        (r for r in rows if self._conflict_match(r, p)), None
                    )
                if existing is not None:
                    existing.update(p)
                    out.append(dict(existing))
                else:
                    rows.append(dict(p))
                    out.append(dict(p))
            return SimpleNamespace(data=out)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self, name)


def _install(monkeypatch, db):
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)
    monkeypatch.setattr(workflow.notifications, "dismiss_notifications", lambda **kw: None)


# State-row / state-dict builders ------------------------------------------------

_DEFAULTS = {
    "intake": ("intake", "active"),
    "material_numbers": ("estimate_received", "locked"),
    "labor_numbers": ("labor_numbers", "locked"),
    "send_out": ("gc_pricing", "locked"),
}


def _rows(pid="p1", **overrides):
    """4 project_category_state rows; override a category with (task, status)."""
    spec = {**_DEFAULTS, **overrides}
    out = []
    for cat, (task, status) in spec.items():
        out.append(
            {
                "project_id": pid,
                "category": cat,
                "current_task": task,
                "status": status,
                "owner_role": None,
                "completed_at": "2026-07-01T00:00:00Z" if status == "complete" else None,
            }
        )
    return out


def _state(**overrides):
    """The in-memory {category: {current_task, status}} dict the helpers consume."""
    spec = {**_DEFAULTS, **overrides}
    return {cat: {"current_task": t, "status": s} for cat, (t, s) in spec.items()}


def _cat(db, category, pid="p1"):
    return next(
        r for r in db.tables["project_category_state"]
        if r["category"] == category and r["project_id"] == pid
    )


# ── Constants / owner roles (kept from the old suite) ─────────────────────────


def test_owner_roles():
    assert owner_role_for("intake") == Role.ESTIMATING_ADMIN
    assert owner_role_for("rfqs") == Role.ESTIMATING_ENGINEER
    assert owner_role_for("verify") == Role.EXECUTIVE
    # submitted is Estimating-Admin-owned (records the Win/Loss outcome);
    # declined is the only ownerless terminal.
    assert owner_role_for("submitted") == Role.ESTIMATING_ADMIN
    assert owner_role_for("declined") is None


def test_internal_owner_skips_estimator_for_handoff():
    assert owner_role_for("estimate_received") == Role.ESTIMATOR  # access owner
    assert internal_owner_role_for("estimate_received") == Role.ESTIMATING_ENGINEER
    for stage in STAGES:
        internal = internal_owner_role_for(stage)
        if stage != "estimate_received":
            assert internal == owner_role_for(stage)
        if internal is not None:
            assert internal != Role.ESTIMATOR


def test_every_stage_defined():
    for key, defn in STAGES.items():
        assert defn.key == key
        assert defn.label


def test_category_task_partition_is_total_and_disjoint():
    # Every non-terminal task belongs to exactly one category, in order.
    seen: list[str] = []
    for cat in workflow.CATEGORY_ORDER:
        for task in workflow.CATEGORY_TASKS[cat]:
            assert workflow.STAGE_TO_CATEGORY[task] == cat
            seen.append(task)
    # declined is the project-global kill; pm_only/cp_only are terminal
    # placeholders for never-bid projects — none belongs to a category.
    assert set(seen) == set(STAGES) - {"declined", "pm_only", "cp_only"}
    assert len(seen) == len(set(seen))  # no task in two categories


# ── Pure category helpers ─────────────────────────────────────────────────────


def test_category_of_and_next_task():
    assert category_of("intake") == "intake"
    assert category_of("estimate_received") == "material_numbers"
    assert category_of("markup") == "labor_numbers"
    assert category_of("bid_outcome") == "send_out"
    assert next_task_in_category("intake") == "go_no_go"
    assert next_task_in_category("go_no_go") == "to_estimator"
    assert next_task_in_category("to_estimator") is None  # last in intake
    assert next_task_in_category("markup") is None  # last in labor
    assert next_task_in_category("gc_pricing") == "verify"


def test_is_category_complete():
    st = _state(intake=("to_estimator", "complete"))
    assert is_category_complete(st, "intake") is True
    assert is_category_complete(st, "material_numbers") is False
    assert is_category_complete(st, "send_out") is False


def test_category_reached():
    st = _state(material_numbers=("rfqs", "active"))
    # head at rfqs → reached estimate_received and rfqs, not receive_quotes.
    assert category_reached(st, "material_numbers", "estimate_received") is True
    assert category_reached(st, "material_numbers", "rfqs") is True
    assert category_reached(st, "material_numbers", "receive_quotes") is False
    # A locked category has reached nothing; a complete one has reached everything.
    assert category_reached(st, "send_out", "gc_pricing") is False
    st2 = _state(material_numbers=("receive_quotes", "complete"))
    assert category_reached(st2, "material_numbers", "receive_quotes") is True


def test_category_past():
    st = _state(material_numbers=("receive_quotes", "active"))
    # head AT receive_quotes → past rfqs, not past receive_quotes.
    assert category_past(st, "rfqs") is True
    assert category_past(st, "receive_quotes") is False
    # locked → never past; complete → past its last task.
    assert category_past(st, "gc_pricing") is False  # send_out locked
    st2 = _state(material_numbers=("receive_quotes", "complete"))
    assert category_past(st2, "receive_quotes") is True
    assert category_past(st2, "rfqs") is True


def test_category_before():
    st = _state(material_numbers=("rfqs", "active"))
    assert category_before(st, "receive_quotes") is True  # active, head rfqs < receive_quotes
    assert category_before(st, "rfqs") is False  # head == task, not before
    assert category_before(st, "estimate_received") is False  # head is past it
    # locked / complete categories are never "before" (they aren't actively working).
    assert category_before(_state(), "verify") is False  # send_out locked
    assert category_before(_state(material_numbers=("receive_quotes", "complete")),
                           "receive_quotes") is False


# ── advance_category ──────────────────────────────────────────────────────────


def test_advance_within_category_moves_head(monkeypatch):
    db = FakeDB({
        "project_category_state": _rows(),
        "projects": [{"id": "p1", "current_stage": "intake"}],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    workflow.advance_category("p1", "intake", "u1", "starting")

    intake = _cat(db, "intake")
    assert intake["current_task"] == "go_no_go" and intake["status"] == "active"
    ev = db.tables["stage_events"][-1]
    assert (ev["from_stage"], ev["to_stage"], ev["category"]) == ("intake", "go_no_go", "intake")
    assert db.tables["projects"][0]["current_stage"] == "go_no_go"


def test_intake_completion_fans_out_material_and_labor(monkeypatch):
    # intake parked on its last task; completing it must auto-activate BOTH lanes.
    db = FakeDB({
        "project_category_state": _rows(intake=("to_estimator", "active")),
        "projects": [{"id": "p1", "current_stage": "to_estimator"}],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    workflow.advance_category("p1", "intake", "u1")

    assert _cat(db, "intake")["status"] == "complete"
    assert _cat(db, "intake")["completed_at"] is not None
    mat, lab = _cat(db, "material_numbers"), _cat(db, "labor_numbers")
    assert (mat["status"], mat["current_task"]) == ("active", "estimate_received")
    assert (lab["status"], lab["current_task"]) == ("active", "labor_numbers")
    assert _cat(db, "send_out")["status"] == "locked"  # still gated on the two lanes
    # An activation event was emitted for each unlocked lane (from=None).
    acts = [e for e in db.tables["stage_events"] if e["from_stage"] is None]
    assert {e["category"] for e in acts} == {"material_numbers", "labor_numbers"}
    # Headline = furthest-along active head = labor_numbers (order 7 > estimate_received 4).
    assert db.tables["projects"][0]["current_stage"] == "labor_numbers"


def test_send_out_locked_until_material_and_labor_complete(monkeypatch):
    # intake done, labor still mid-flight; completing material must NOT open send_out.
    db = FakeDB({
        "project_category_state": _rows(
            intake=("to_estimator", "complete"),
            material_numbers=("receive_quotes", "active"),
            labor_numbers=("labor_numbers", "active"),
        ),
        "projects": [{"id": "p1", "current_stage": "receive_quotes"}],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    workflow.advance_category("p1", "material_numbers", "u1")

    assert _cat(db, "material_numbers")["status"] == "complete"
    assert _cat(db, "send_out")["status"] == "locked"  # labor not done yet
    # Headline falls to the one remaining active head.
    assert db.tables["projects"][0]["current_stage"] == "labor_numbers"


def test_completing_both_lanes_opens_send_out(monkeypatch):
    # material already done, labor on its last task; finishing labor opens send_out.
    db = FakeDB({
        "project_category_state": _rows(
            intake=("to_estimator", "complete"),
            material_numbers=("receive_quotes", "complete"),
            labor_numbers=("markup", "active"),
        ),
        "projects": [{"id": "p1", "current_stage": "markup"}],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    workflow.advance_category("p1", "labor_numbers", "u1")

    assert _cat(db, "labor_numbers")["status"] == "complete"
    so = _cat(db, "send_out")
    assert (so["status"], so["current_task"]) == ("active", "gc_pricing")
    # send_out active → it owns the headline.
    assert db.tables["projects"][0]["current_stage"] == "gc_pricing"


def test_advance_inactive_category_is_409(monkeypatch):
    db = FakeDB({
        "project_category_state": _rows(),  # material_numbers is locked
        "projects": [{"id": "p1", "current_stage": "intake"}],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        workflow.advance_category("p1", "material_numbers", "u1")
    assert exc.value.status_code == 409


def test_advance_unknown_category_is_400(monkeypatch):
    db = FakeDB({"project_category_state": _rows(), "projects": [{"id": "p1"}]})
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        workflow.advance_category("p1", "nonsense", "u1")
    assert exc.value.status_code == 400


# ── decline_project (global kill) ─────────────────────────────────────────────


def test_decline_project_kills_globally(monkeypatch):
    db = FakeDB({
        "project_category_state": _rows(intake=("go_no_go", "active")),
        "projects": [{"id": "p1", "current_stage": "go_no_go", "current_owner_role": "executive"}],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    row = workflow.decline_project("p1", "exec1", "not worth bidding")

    assert row["current_stage"] == "declined"
    assert row["current_owner_role"] is None
    ev = db.tables["stage_events"][-1]
    assert (ev["from_stage"], ev["to_stage"], ev["category"]) == ("go_no_go", "declined", "intake")
    # The other lanes never unlock — a declined project is frozen.
    assert _cat(db, "material_numbers")["status"] == "locked"
    assert _cat(db, "labor_numbers")["status"] == "locked"
    assert _cat(db, "send_out")["status"] == "locked"


# ── load helpers against the fake ─────────────────────────────────────────────


def test_load_category_state_fills_missing_with_defaults(monkeypatch):
    db = FakeDB({"project_category_state": [
        {"project_id": "p1", "category": "intake", "current_task": "to_estimator",
         "status": "complete", "owner_role": None, "completed_at": "x"},
    ]})
    _install(monkeypatch, db)
    st = workflow.load_category_state("p1")
    assert st["intake"]["status"] == "complete"
    # Categories with no row fall back to the seed defaults (locked, first task).
    assert st["material_numbers"]["status"] == "locked"
    assert st["send_out"]["current_task"] == "gc_pricing"


def test_load_category_states_is_batched(monkeypatch):
    db = FakeDB({"project_category_state": [
        {"project_id": "p1", "category": "send_out", "current_task": "verify",
         "status": "active", "owner_role": None, "completed_at": None},
        {"project_id": "p2", "category": "intake", "current_task": "intake",
         "status": "active", "owner_role": None, "completed_at": None},
    ]})
    _install(monkeypatch, db)
    out = workflow.load_category_states(["p1", "p2"])
    assert out["p1"]["send_out"]["current_task"] == "verify"
    assert out["p2"]["intake"]["status"] == "active"
    assert workflow.load_category_states([]) == {}


# ── _dismiss_stale_notifications (NEW signature: category + loaded state) ──────


def _sweep(monkeypatch, advanced_category, state):
    captured: dict = {}
    monkeypatch.setattr(
        workflow.notifications, "dismiss_notifications", lambda **kw: captured.update(kw)
    )
    workflow._dismiss_stale_notifications("p1", advanced_category, state)
    return captured


def test_dismiss_material_advance_clears_vendor_reminders(monkeypatch):
    st = _state(material_numbers=("receive_quotes", "complete"))
    cap = _sweep(monkeypatch, "material_numbers", st)
    assert "due.due_from_vendors." in cap["type_prefixes"]
    assert "stage_handoff" in cap["types"]
    # estimate_received notifications (same category) also clear once past them.
    assert "assigned" in cap["types"]


def test_dismiss_labor_advance_does_not_touch_vendor_reminders(monkeypatch):
    # A labor-lane advance must not silence a still-open material-side reminder.
    st = _state(
        material_numbers=("receive_quotes", "complete"),
        labor_numbers=("markup", "complete"),
    )
    cap = _sweep(monkeypatch, "labor_numbers", st)
    assert "due.due_from_vendors." not in (cap.get("type_prefixes") or [])
    # No labor task drives a dismiss type; only the ephemeral handoff clears.
    assert cap["types"] == ["stage_handoff"]


def test_dismiss_send_out_advance_to_submitted(monkeypatch):
    st = _state(
        intake=("to_estimator", "complete"),
        material_numbers=("receive_quotes", "complete"),
        labor_numbers=("markup", "complete"),
        send_out=("submitted", "active"),
    )
    cap = _sweep(monkeypatch, "send_out", st)
    # Bid-due reminders and "pricing committed" are done once the bid is out…
    assert "due.internal_bid." in cap["type_prefixes"]
    assert "due.actual_bid." in cap["type_prefixes"]
    assert "verified" in cap["types"]
    # …but "submitted" (created this same transition) must survive to Win/Loss.
    assert "submitted" not in cap["types"]
    assert "stage_handoff" in cap["types"]


def test_dismiss_reverify_required_after_leaving_verify(monkeypatch):
    past = _state(
        intake=("to_estimator", "complete"),
        material_numbers=("receive_quotes", "complete"),
        labor_numbers=("markup", "complete"),
        send_out=("send_out", "active"),  # past verify
    )
    assert "reverify_required" in _sweep(monkeypatch, "send_out", past)["types"]
    at_verify = _state(
        intake=("to_estimator", "complete"),
        material_numbers=("receive_quotes", "complete"),
        labor_numbers=("markup", "complete"),
        send_out=("verify", "active"),  # still pending at verify
    )
    assert "reverify_required" not in _sweep(monkeypatch, "send_out", at_verify).get("types", [])


def test_dismiss_estimate_reminders_on_intake_completion(monkeypatch):
    st = _state(intake=("to_estimator", "complete"))
    cap = _sweep(monkeypatch, "intake", st)
    assert "due.due_from_estimator." in cap["type_prefixes"]
    assert "gono_go" in cap["types"]  # pending-through to_estimator, now past
    # Material-side estimate notifications are NOT this category's to dismiss.
    assert "assigned" not in cap["types"]
    assert "due.due_from_vendors." not in (cap.get("type_prefixes") or [])


def test_dismiss_quote_types_never_swept(monkeypatch):
    # quote.received / rfq.reply_received are dismissed per-RFQ on pricing, so a
    # late quote after advancing still notifies — they must never be in the sweep.
    for cat, st in (
        ("material_numbers", _state(material_numbers=("receive_quotes", "complete"))),
        ("labor_numbers", _state(labor_numbers=("markup", "complete"))),
        ("send_out", _state(
            intake=("to_estimator", "complete"),
            material_numbers=("receive_quotes", "complete"),
            labor_numbers=("markup", "complete"),
            send_out=("bid_outcome", "active"),
        )),
    ):
        cap = _sweep(monkeypatch, cat, st)
        assert "quote.received" not in cap["types"]
        assert "rfq.reply_received" not in cap["types"]
        assert "estimator_note" not in cap["types"]
