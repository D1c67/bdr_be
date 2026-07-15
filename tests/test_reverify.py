"""Re-verification on a post-verify pricing edit.

When a pricing-affecting edit lands on a project that has ALREADY passed Verify,
the project is bounced back to `verify` (the Executive must re-commit) and then
resumes at the stage it was on. These tests cover the workflow helpers
(reopen_verify / return_from_reverify / the maybe_* hook), the commit-time return
logic in the pricing router, the preserved forward-only invariant, and the
analytics first-submitted-wins guard that keeps the bid date stable across a
re-verify round-trip.

The Supabase client is faked with a tiny in-memory store supporting the chained
builder the code uses (select/insert/update/upsert + eq/single/order).
"""

from types import SimpleNamespace

from app.core.roles import Role
from app.services import analytics_metrics as m
from app.services import general_material as gm
from app.services import workflow


# ── Fake Supabase ─────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._conflict = None
        self._filters = []
        self._neq_filters = []
        self._single = False

    # builders
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
        self._filters.append((col, val))
        return self

    def neq(self, col, val):
        self._neq_filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    # execution
    def _matches(self, row):
        return all(row.get(c) == v for c, v in self._filters) and all(
            row.get(c) != v for c, v in self._neq_filters
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
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
                        (r for r in rows if r.get(self._conflict) == p.get(self._conflict)), None
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


def _project(stage, *, return_stage=None, abandoned_at=None):
    return {
        "id": "p1",
        "current_stage": stage,
        "reverify_return_stage": return_stage,
        "abandoned_at": abandoned_at,
        "current_owner_role": "estimating_admin",
    }


def _verification(committed=True):
    return {
        "project_id": "p1",
        "committed_at": "2026-06-01T00:00:00Z" if committed else None,
        "verified_by": "exec1" if committed else None,
    }


def _install(monkeypatch, db):
    """Point workflow at the fake DB; record notify_role calls; stub dismissal."""
    notes = []
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)
    monkeypatch.setattr(
        workflow.notifications, "notify_role",
        lambda role, pid, type_, msg, **k: notes.append((role, type_, msg)),
    )
    monkeypatch.setattr(workflow.notifications, "dismiss_notifications", lambda **kw: None)
    return notes


# ── Forward-only invariant is preserved (no new backward edges) ────────────────


def test_reverify_does_not_add_backward_transitions():
    assert not workflow.can_transition("send_out", "verify")
    assert not workflow.can_transition("submitted", "verify")
    assert not workflow.can_transition("bid_outcome", "verify")
    # The return move is also not a legal forward edge — it's a deliberate bypass.
    assert not workflow.can_transition("verify", "submitted")
    assert not workflow.can_transition("verify", "bid_outcome")
    assert workflow.can_transition("verify", "send_out")  # the one real edge


def test_reverify_required_is_stage_dismissed_after_leaving_verify(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        workflow.notifications, "dismiss_notifications", lambda **kw: captured.update(kw)
    )
    workflow._dismiss_stale_notifications("p1", "send_out")  # order 11 > verify 10
    assert "reverify_required" in captured["types"]
    captured.clear()
    workflow._dismiss_stale_notifications("p1", "verify")  # still pending at verify
    assert "reverify_required" not in captured.get("types", [])


# ── The hook: when does an edit bounce the project? ────────────────────────────


def test_no_bounce_before_verify(monkeypatch):
    db = FakeDB({"projects": [_project("markup")], "verifications": [_verification()]})
    notes = _install(monkeypatch, db)
    assert workflow.maybe_reopen_verify_after_edit("p1", "u1", "Markup edited") is False
    assert db.tables["projects"][0]["current_stage"] == "markup"
    assert notes == []


def test_no_bounce_at_verify(monkeypatch):
    db = FakeDB({"projects": [_project("verify")], "verifications": [_verification(committed=False)]})
    notes = _install(monkeypatch, db)
    assert workflow.maybe_reopen_verify_after_edit("p1", "u1", "Markup edited") is False
    assert notes == []


def test_skip_abandoned(monkeypatch):
    db = FakeDB({
        "projects": [_project("send_out", abandoned_at="2026-06-01T00:00:00Z")],
        "verifications": [_verification()],
    })
    notes = _install(monkeypatch, db)
    assert workflow.maybe_reopen_verify_after_edit("p1", "u1", "Markup edited") is False
    assert db.tables["projects"][0]["current_stage"] == "send_out"
    assert notes == []


def test_bounce_from_send_out(monkeypatch):
    db = FakeDB({
        "projects": [_project("send_out")],
        "verifications": [_verification()],
        "stage_events": [],
    })
    notes = _install(monkeypatch, db)
    assert workflow.maybe_reopen_verify_after_edit("p1", "u1", "Labor numbers edited") is True

    proj = db.tables["projects"][0]
    assert proj["current_stage"] == "verify"
    assert proj["reverify_return_stage"] == "send_out"
    assert proj["current_owner_role"] == Role.EXECUTIVE

    v = db.tables["verifications"][0]
    assert v["committed_at"] is None and v["verified_by"] is None  # snapshot re-opened

    back = [e for e in db.tables["stage_events"] if e["to_stage"] == "verify"]
    assert back and back[0]["from_stage"] == "send_out"

    assert {n[0] for n in notes} == {Role.EXECUTIVE, Role.ESTIMATING_ENGINEER}
    assert all(n[1] == "reverify_required" for n in notes)
    assert "already sent" not in notes[0][2]  # not yet dispatched at send_out


def test_bounce_from_submitted_preserves_return_and_warns_sent(monkeypatch):
    db = FakeDB({
        "projects": [_project("submitted")],
        "verifications": [_verification()],
        "stage_events": [],
    })
    notes = _install(monkeypatch, db)
    assert workflow.maybe_reopen_verify_after_edit("p1", "u1", "Vendor quote amount changed") is True
    assert db.tables["projects"][0]["reverify_return_stage"] == "submitted"
    assert "already sent" in notes[0][2]


def test_second_edit_does_not_overwrite_return_stage(monkeypatch):
    db = FakeDB({
        "projects": [_project("send_out")],
        "verifications": [_verification()],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    assert workflow.maybe_reopen_verify_after_edit("p1", "u1", "first") is True
    events_after_first = len(db.tables["stage_events"])

    # A further edit while already bounced: no-op, return stage intact, no dupes.
    assert workflow.maybe_reopen_verify_after_edit("p1", "u1", "second") is False
    assert db.tables["projects"][0]["reverify_return_stage"] == "send_out"
    assert len(db.tables["stage_events"]) == events_after_first


# ── The return move ────────────────────────────────────────────────────────────


def test_return_from_reverify_restores_stage_and_clears_marker(monkeypatch):
    db = FakeDB({
        "projects": [_project("verify", return_stage="submitted")],
        "stage_events": [],
    })
    _install(monkeypatch, db)
    workflow.return_from_reverify("p1", "submitted", "exec1")
    proj = db.tables["projects"][0]
    assert proj["current_stage"] == "submitted"
    assert proj["reverify_return_stage"] is None
    fwd = [e for e in db.tables["stage_events"] if e["from_stage"] == "verify"]
    assert fwd and fwd[0]["to_stage"] == "submitted"


# ── commit_verify: route off Verify to the right place ─────────────────────────


def _commit_db(stage="verify", return_stage=None):
    return FakeDB({
        "verifications": [_verification(committed=False)],
        "projects": [_project(stage, return_stage=return_stage)],
    })


def _patch_commit(monkeypatch, db):
    from app.routers import pricing

    calls = []
    monkeypatch.setattr(pricing, "get_supabase", lambda: db)
    monkeypatch.setattr(pricing, "audit", lambda *a, **k: None)
    monkeypatch.setattr(pricing, "_deltas", lambda body, pid: {})
    monkeypatch.setattr(pricing, "notify_role", lambda *a, **k: calls.append(("notify", *a)))
    monkeypatch.setattr(
        pricing.workflow, "return_from_reverify",
        lambda pid, rs, uid: calls.append(("return", pid, rs)),
    )
    monkeypatch.setattr(
        pricing.workflow, "transition_project",
        lambda pid, to, uid, note=None: calls.append(("transition", pid, to)),
    )
    return pricing, calls


def test_commit_returns_to_stored_stage(monkeypatch):
    pricing, calls = _patch_commit(monkeypatch, _commit_db(return_stage="submitted"))
    user = SimpleNamespace(id="exec1", role=Role.EXECUTIVE)
    pricing.commit_verify("p1", None, user)
    assert ("return", "p1", "submitted") in calls
    assert not any(c[0] == "transition" for c in calls)


def test_commit_without_return_stage_advances_to_send_out(monkeypatch):
    pricing, calls = _patch_commit(monkeypatch, _commit_db(return_stage=None))
    user = SimpleNamespace(id="exec1", role=Role.EXECUTIVE)
    pricing.commit_verify("p1", None, user)
    assert ("transition", "p1", "send_out") in calls
    assert not any(c[0] == "return" for c in calls)


def test_redundant_commit_off_verify_is_silent(monkeypatch):
    # A re-commit when the project already advanced off Verify just re-stamps the
    # snapshot — no transition, no return, and no spurious "verified" notification.
    pricing, calls = _patch_commit(monkeypatch, _commit_db(stage="send_out", return_stage=None))
    user = SimpleNamespace(id="exec1", role=Role.EXECUTIVE)
    pricing.commit_verify("p1", None, user)
    assert calls == []


# ── Analytics: the bid date survives a re-verify round-trip ────────────────────


def test_submitted_at_is_pinned_to_first_submission(monkeypatch):
    # submitted → (post-verify edit bounces) verify → (re-commit) submitted.
    events = [
        {"project_id": "p1", "to_stage": "submitted", "entered_at": "2026-06-01T00:00:00Z"},
        {"project_id": "p1", "to_stage": "verify", "entered_at": "2026-06-02T00:00:00Z"},
        {"project_id": "p1", "to_stage": "submitted", "entered_at": "2026-06-03T00:00:00Z"},
    ]
    db = FakeDB({
        "projects": [{"id": "p1", "current_stage": "submitted", "abandoned_at": None}],
        "stage_events": events,
    })
    monkeypatch.setattr(m, "get_supabase", lambda: db)
    from datetime import datetime, timezone

    w = m.load_window(
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 12, 31, tzinfo=timezone.utc)
    )
    # Pinned to the ORIGINAL submission, not the re-commit.
    assert w.submitted_at["p1"] == m._parse("2026-06-01T00:00:00Z")


# ── General-material re-extraction only bounces on a real change ───────────────


def test_general_material_amount_changed_is_numeric_aware():
    assert gm._amount_changed("100", 100) is False
    assert gm._amount_changed("100.00", 100) is False
    assert gm._amount_changed(None, None) is False
    assert gm._amount_changed(None, 50) is True
    assert gm._amount_changed("100", 150) is True


def test_general_material_maybe_bounce_only_on_change(monkeypatch):
    called = []
    monkeypatch.setattr(
        workflow, "maybe_reopen_verify_after_edit",
        lambda pid, actor, reason: called.append((pid, actor, reason)),
    )
    gm._maybe_bounce("p1", "100", 100)  # unchanged → no bounce
    assert called == []
    gm._maybe_bounce("p1", "100", 250)  # changed → bounce (background, no actor)
    assert called == [("p1", None, "General material re-extracted")]
