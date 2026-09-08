"""Executive approval of a per-GC price change for a GC added after send-out
(migration 0118).

Covers the request / approve / reject services, the locks they impose on
generate / send / mark-submitted / direct amount edits, the GC list wire the
side-menu panel reads, the notification metadata + deep link, the targeted
dismissal, and the dashboard's pending count.

The Supabase client is faked with a tiny in-memory store (the test_reverify
pattern) that additionally understands the PostgREST JSON path filter
`metadata->>gc_id` the targeted dismissal uses.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.roles import Role
from app.models.schemas import ProjectOut, ProposalAmountsRequestIn, ProposalGenerateIn
from app.services import notification_email as ne
from app.services import notifications as notif
from app.services import proposal_send as psend
from app.services import workflow
from app.services.proposal_send import PENDING_APPROVAL_MESSAGE, ProposalSendError

# ── Fake Supabase ─────────────────────────────────────────────────────────────


def _json_path(row, col):
    """metadata->>gc_id -> row["metadata"]["gc_id"]"""
    if "->>" not in col:
        return row.get(col)
    base, key = col.split("->>", 1)
    return (row.get(base) or {}).get(key)


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._in_filters = []
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

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def neq(self, col, val):
        self._filters.append((col, ("__neq__", val)))
        return self

    def in_(self, col, vals):
        self._in_filters.append((col, list(vals)))
        return self

    def is_(self, col, val):
        self._filters.append((col, None if val == "null" else val))
        return self

    def like(self, col, pattern):
        self._filters.append((col, ("__like__", pattern.rstrip("%"))))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def _matches(self, row):
        for c, v in self._filters:
            got = _json_path(row, c)
            if isinstance(v, tuple) and v and v[0] == "__neq__":
                if got == v[1]:
                    return False
            elif isinstance(v, tuple) and v and v[0] == "__like__":
                if not (got or "").startswith(v[1]):
                    return False
            elif got != v:
                return False
        return all(_json_path(row, c) in vals for c, vals in self._in_filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = {"id": f"{self.table}-{len(rows) + 1}", **p}
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            gone = [r for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=gone)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self, name)


# ── Fixtures ──────────────────────────────────────────────────────────────────

PROJECT = {"id": "p1", "name": "Sunset Plaza", "number": "26.9.7201", "current_stage": "submitted"}

_CAT_DEFAULTS = {
    "intake": ("to_estimator", "complete"),
    "material_numbers": ("receive_quotes", "complete"),
    "labor_numbers": ("markup", "complete"),
    "send_out": ("submitted", "active"),
}


def _cat_rows(head="submitted"):
    spec = {**_CAT_DEFAULTS, "send_out": (head, "active")}
    return [
        {"project_id": "p1", "category": c, "current_task": t, "status": s,
         "owner_role": None, "completed_at": ("x" if s == "complete" else None)}
        for c, (t, s) in spec.items()
    ]


def _gc(gc_id, name, *, status=None, requested_by=None, overrides=None, contacts=None):
    row = {
        "id": f"link-{gc_id}",
        "project_id": "p1",
        "gc_id": gc_id,
        "needs_by": None,
        "proposal_material_amount": None,
        "proposal_gear_amount": None,
        "proposal_underground_amount": None,
        "proposal_low_voltage_amount": None,
        "proposal_labor_amount": None,
        "pricing_approval_status": status,
        "pricing_requested_by": requested_by,
        "pricing_requested_at": "2026-09-04T10:00:00+00:00" if status else None,
        "pricing_request_note": None,
        "pricing_decided_by": None,
        "pricing_decided_at": None,
        "pricing_decision_note": None,
        "project_gc_contacts": [],
        "general_contractors": {
            "id": gc_id,
            "name": name,
            "gc_contacts": contacts
            or [{"id": f"c-{gc_id}", "name": "Pat", "email": f"pat@{gc_id}.com", "phone": None}],
        },
    }
    for key, val in (overrides or {}).items():
        row[f"proposal_{key}_amount"] = val
    return row


BASIS = {
    "material_cost": Decimal("40000"),
    "gear_cost": None,
    "underground_cost": None,
    "low_voltage_cost": None,
    "labor_cost": Decimal("30000"),
    "material_markup": Decimal("4000"),
    "gear_markup": None,
    "underground_markup": None,
    "low_voltage_markup": None,
    "labor_markup": Decimal("3000"),
}

AMOUNTS = {
    "material": Decimal("45000"),
    "gear": None,
    "underground": None,
    "low_voltage": None,
    "labor": Decimal("31000"),
}


def _db(head="submitted", gcs=None, sends=None, profiles=None):
    return FakeDB(
        {
            "projects": [dict(PROJECT, current_stage=head)],
            "project_category_state": _cat_rows(head),
            "project_gcs": gcs or [_gc("g1", "Alpha Builders"), _gc("g2", "Bravo GC")],
            "proposal_sends": sends or [],
            "profiles": profiles
            or [
                {"id": "u-admin", "full_name": "Dana Admin", "role": "estimating_admin"},
                {"id": "u-exec", "full_name": "Sam Exec", "role": "executive"},
            ],
            "notifications": [],
            "audit_log": [],
        }
    )


@pytest.fixture
def env(monkeypatch):
    """Install a fake DB into every module the services reach, record the
    notification / audit side effects, and stub the pricing basis."""

    class Env:
        def __init__(self):
            self.db = None
            self.role_notes = []
            self.user_notes = []
            self.dismissals = []
            self.audits = []

        def install(self, db):
            self.db = db
            monkeypatch.setattr(psend, "get_supabase", lambda: db)
            monkeypatch.setattr(workflow, "get_supabase", lambda: db)
            monkeypatch.setattr(psend, "_pricing_basis_for", lambda pid: BASIS)
            monkeypatch.setattr(
                psend, "amounts_overview", lambda pid: {"gcs": [], "basis": BASIS}
            )
            monkeypatch.setattr(
                psend, "notify_role",
                lambda role, pid, type_, msg, **kw: self.role_notes.append(
                    (role, type_, msg, kw.get("metadata"))
                ),
            )
            monkeypatch.setattr(
                psend, "notify_user",
                lambda uid, pid, type_, msg, **kw: self.user_notes.append(
                    (uid, type_, msg, kw.get("metadata"))
                ),
            )
            monkeypatch.setattr(
                psend, "dismiss_notifications", lambda **kw: self.dismissals.append(kw)
            )
            monkeypatch.setattr(
                psend, "audit",
                lambda actor, action, entity, entity_id, payload=None: self.audits.append(
                    (actor, action, payload)
                ),
            )
            return db

        def link(self, gc_id):
            return next(r for r in self.db.tables["project_gcs"] if r["gc_id"] == gc_id)

    return Env()


# ── request ───────────────────────────────────────────────────────────────────


def test_request_by_admin_locks_gc_and_notifies_executives(env):
    env.install(_db())
    rows = psend.request_gc_pricing_change(
        "p1", "g1", AMOUNTS, "Sharper number for Alpha", "u-admin", Role.ESTIMATING_ADMIN
    )
    link = env.link("g1")
    assert link["pricing_approval_status"] == "pending"
    assert link["proposal_material_amount"] == "45000"
    assert link["proposal_labor_amount"] == "31000"
    assert link["proposal_gear_amount"] is None
    assert link["pricing_requested_by"] == "u-admin"
    assert link["pricing_request_note"] == "Sharper number for Alpha"
    assert link["pricing_decided_by"] is None
    # Executives are told, with the GC id riding along for the deep link.
    assert len(env.role_notes) == 1
    role, type_, msg, meta = env.role_notes[0]
    assert role == Role.EXECUTIVE
    assert type_ == "gc_pricing.approval_requested"
    assert meta == {"gc_id": "g1"}
    assert "Dana Admin" in msg and "Alpha Builders" in msg and "#26.9.7201" in msg
    assert "$45,000" in msg and "Sharper number" in msg
    # Audited with the figures and the fact it was NOT auto-approved.
    action, payload = env.audits[0][1], env.audits[0][2]
    assert action == "proposal.amounts_change_requested"
    assert payload["auto_approved"] is False and payload["head"] == "submitted"
    assert payload["material_amount"] == "45000"
    # The returned rows are the GC list wire with the new status on the row.
    mine = next(r for r in rows if r["id"] == "g1")
    assert mine["pricing_approval_status"] == "pending"
    assert mine["pricing_requested_by_name"] == "Dana Admin"
    assert mine["proposal_status"] == "not_sent"


def test_request_by_executive_is_auto_approved_without_notification(env):
    env.install(_db())
    psend.request_gc_pricing_change("p1", "g1", AMOUNTS, None, "u-exec", Role.EXECUTIVE)
    link = env.link("g1")
    assert link["pricing_approval_status"] == "approved"
    assert link["pricing_requested_by"] == "u-exec"
    assert link["pricing_decided_by"] == "u-exec"
    assert link["pricing_decided_at"] is not None
    assert env.role_notes == [] and env.user_notes == []
    assert env.audits[0][2]["auto_approved"] is True


def test_request_by_it_admin_is_auto_approved(env):
    env.install(_db())
    psend.request_gc_pricing_change("p1", "g1", AMOUNTS, None, "u-it", Role.IT_ADMIN)
    assert env.link("g1")["pricing_approval_status"] == "approved"
    assert env.role_notes == []


def test_request_refused_on_the_active_send_out_step(env):
    # The approval flow is for GCs added after the bid went out; the active
    # step keeps its own Change GC pricing (no approval) as before.
    env.install(_db(head="send_out"))
    with pytest.raises(ProposalSendError, match="added after the bid went out"):
        psend.request_gc_pricing_change(
            "p1", "g1", AMOUNTS, None, "u-admin", Role.ESTIMATING_ADMIN
        )
    assert env.link("g1")["pricing_approval_status"] is None


def test_request_allowed_at_bid_outcome(env):
    env.install(_db(head="bid_outcome"))
    psend.request_gc_pricing_change("p1", "g1", AMOUNTS, None, "u-admin", Role.ESTIMATING_ADMIN)
    assert env.link("g1")["pricing_approval_status"] == "pending"


def test_request_refused_while_one_is_already_pending(env):
    env.install(_db(gcs=[_gc("g1", "Alpha", status="pending", requested_by="u-admin")]))
    with pytest.raises(ProposalSendError, match="already awaiting"):
        psend.request_gc_pricing_change(
            "p1", "g1", AMOUNTS, None, "u-admin", Role.ESTIMATING_ADMIN
        )
    assert env.role_notes == []


def test_request_refused_for_a_sent_gc(env):
    env.install(
        _db(sends=[{"id": "ps1", "project_id": "p1", "gc_id": "g1", "status": "sent",
                    "sent_at": "2026-09-01T00:00:00+00:00", "sent_via": "email"}])
    )
    with pytest.raises(ProposalSendError, match="locked"):
        psend.request_gc_pricing_change(
            "p1", "g1", AMOUNTS, None, "u-admin", Role.ESTIMATING_ADMIN
        )


def test_request_refused_for_a_gc_not_on_the_project(env):
    env.install(_db())
    with pytest.raises(ProposalSendError) as exc:
        psend.request_gc_pricing_change(
            "p1", "nope", AMOUNTS, None, "u-admin", Role.ESTIMATING_ADMIN
        )
    assert exc.value.status_code == 404


def test_request_refuses_a_section_not_on_the_project(env):
    env.install(_db())
    with pytest.raises(ProposalSendError, match="not on this project"):
        psend.request_gc_pricing_change(
            "p1", "g1", {**AMOUNTS, "gear": Decimal("5000")}, None,
            "u-admin", Role.ESTIMATING_ADMIN,
        )


def test_request_after_rejection_starts_a_fresh_round(env):
    env.install(_db(gcs=[_gc("g1", "Alpha", status="rejected", requested_by="u-admin")]))
    psend.request_gc_pricing_change("p1", "g1", AMOUNTS, None, "u-admin", Role.ESTIMATING_ADMIN)
    link = env.link("g1")
    assert link["pricing_approval_status"] == "pending"
    assert link["pricing_decided_by"] is None and link["pricing_decision_note"] is None


# ── approve ───────────────────────────────────────────────────────────────────


def _pending_db(**kw):
    return _db(
        gcs=[
            _gc("g1", "Alpha Builders", status="pending", requested_by="u-admin",
                overrides={"material": "45000", "labor": "31000"}),
            _gc("g2", "Bravo GC", status="pending", requested_by="u-admin",
                overrides={"material": "44000"}),
        ],
        **kw,
    )


def test_approve_keeps_requested_figures_and_notifies_requester(env):
    env.install(_pending_db())
    rows = psend.approve_gc_pricing_change("p1", "g1", AMOUNTS, "Looks right", "u-exec")
    link = env.link("g1")
    assert link["pricing_approval_status"] == "approved"
    assert link["proposal_material_amount"] == "45000"
    assert link["pricing_decided_by"] == "u-exec"
    assert link["pricing_decision_note"] == "Looks right"
    uid, type_, msg, meta = env.user_notes[0]
    assert uid == "u-admin" and type_ == "gc_pricing.approved" and meta == {"gc_id": "g1"}
    assert "Sam Exec" in msg and "adjusted" not in msg and "Looks right" in msg
    assert env.audits[0][1] == "proposal.amounts_change_approved"
    assert env.audits[0][2]["changed_by_executive"] is False
    # Only THIS GC's request notifications are dismissed; g2's stay.
    assert env.dismissals == [
        {"project_id": "p1", "types": ["gc_pricing.approval_requested"], "gc_id": "g1"}
    ]
    assert env.link("g2")["pricing_approval_status"] == "pending"
    mine = next(r for r in rows if r["id"] == "g1")
    assert mine["pricing_decided_by_name"] == "Sam Exec"


def test_approve_with_edited_figures_replaces_them_and_says_so(env):
    env.install(_pending_db())
    edited = {**AMOUNTS, "material": Decimal("46500")}
    psend.approve_gc_pricing_change("p1", "g1", edited, None, "u-exec")
    assert env.link("g1")["proposal_material_amount"] == "46500"
    assert env.audits[0][2]["changed_by_executive"] is True
    assert env.audits[0][2]["requested"]["material_amount"] == "45000"
    assert "adjusted during approval" in env.user_notes[0][2]


def test_approve_refused_when_nothing_is_pending(env):
    env.install(_db())
    with pytest.raises(ProposalSendError, match="No pricing change"):
        psend.approve_gc_pricing_change("p1", "g1", AMOUNTS, None, "u-exec")
    assert env.user_notes == [] and env.dismissals == []


def test_approve_does_not_notify_when_the_requester_is_the_approver(env):
    env.install(_db(gcs=[_gc("g1", "Alpha", status="pending", requested_by="u-exec")]))
    psend.approve_gc_pricing_change("p1", "g1", AMOUNTS, None, "u-exec")
    assert env.user_notes == []
    assert env.link("g1")["pricing_approval_status"] == "approved"


# ── reject ────────────────────────────────────────────────────────────────────


def test_reject_clears_overrides_and_notifies_with_note(env):
    env.install(_pending_db())
    psend.reject_gc_pricing_change("p1", "g1", "Too low for this scope", "u-exec")
    link = env.link("g1")
    assert link["pricing_approval_status"] == "rejected"
    assert link["proposal_material_amount"] is None
    assert link["proposal_labor_amount"] is None
    assert link["pricing_decision_note"] == "Too low for this scope"
    uid, type_, msg, meta = env.user_notes[0]
    assert uid == "u-admin" and type_ == "gc_pricing.rejected" and meta == {"gc_id": "g1"}
    assert "Too low for this scope" in msg and "project figures" in msg
    assert env.audits[0][1] == "proposal.amounts_change_rejected"
    assert env.audits[0][2]["requested"]["material_amount"] == "45000"
    assert env.dismissals[0]["gc_id"] == "g1"


def test_reject_refused_when_nothing_is_pending(env):
    env.install(_db(gcs=[_gc("g1", "Alpha", status="approved", requested_by="u-admin")]))
    with pytest.raises(ProposalSendError, match="No pricing change"):
        psend.reject_gc_pricing_change("p1", "g1", None, "u-exec")


# ── direct amount edits (PUT /proposals/amounts/{gc}) after send-out ──────────


def test_set_gc_amounts_after_send_out_refuses_non_executives(env):
    env.install(_db())
    with pytest.raises(ProposalSendError, match="needs executive approval"):
        psend.set_gc_amounts("p1", "g1", AMOUNTS, "u-admin", role=Role.ESTIMATING_ADMIN)
    assert env.link("g1")["proposal_material_amount"] is None


def test_set_gc_amounts_after_send_out_allows_executives(env):
    env.install(_db())
    psend.set_gc_amounts("p1", "g1", AMOUNTS, "u-exec", role=Role.EXECUTIVE)
    assert env.link("g1")["proposal_material_amount"] == "45000"


def test_set_gc_amounts_refuses_a_pending_gc_even_for_executives(env):
    env.install(_pending_db())
    with pytest.raises(ProposalSendError, match="awaiting executive approval"):
        psend.set_gc_amounts("p1", "g1", AMOUNTS, "u-exec", role=Role.EXECUTIVE)


def test_set_gc_amounts_on_the_active_step_is_unchanged(env):
    # Before the bid goes out every writer edits per-GC figures freely.
    env.install(_db(head="send_out"))
    psend.set_gc_amounts("p1", "g1", AMOUNTS, "u-admin", role=Role.ESTIMATING_ADMIN)
    assert env.link("g1")["proposal_material_amount"] == "45000"


# ── generation targets ────────────────────────────────────────────────────────

GCS = [
    {"id": "g1", "name": "Alpha", "pricing_approval_status": None},
    {"id": "g2", "name": "Bravo", "pricing_approval_status": "pending"},
    {"id": "g3", "name": "Charlie", "pricing_approval_status": "approved"},
]
BY_GC = {"g1": {"status": "sent"}}


def test_batch_generation_skips_sent_and_pending_gcs():
    targets = psend.generation_targets(GCS, BY_GC)
    assert [g["id"] for g in targets] == ["g3"]


def test_generation_by_id_targets_only_the_asked_gc():
    targets = psend.generation_targets(GCS, BY_GC, ["g3"])
    assert [g["id"] for g in targets] == ["g3"]


def test_generation_by_id_refuses_a_pending_gc():
    with pytest.raises(ProposalSendError, match="awaiting executive approval"):
        psend.generation_targets(GCS, BY_GC, ["g3", "g2"])


def test_generation_by_id_refuses_a_sent_gc():
    with pytest.raises(ProposalSendError, match="already been sent"):
        psend.generation_targets(GCS, BY_GC, ["g1"])


def test_generation_by_id_refuses_an_unknown_gc():
    with pytest.raises(ProposalSendError) as exc:
        psend.generation_targets(GCS, BY_GC, ["nope"])
    assert exc.value.status_code == 404


def test_batch_generation_with_only_pending_left_explains_why():
    gcs = [GCS[0], GCS[1]]
    with pytest.raises(ProposalSendError, match="awaiting executive approval"):
        psend.generation_targets(gcs, BY_GC)


def test_generate_documents_refuses_pending_gc_before_rendering(env, monkeypatch):
    # The by-id gate fires before the template is even loaded.
    db = env.install(
        _db(
            gcs=[_gc("g1", "Alpha", status="pending", requested_by="u-admin")],
        )
    )
    db.tables["verifications"] = [{"project_id": "p1", "committed_at": "2026-09-01T00:00:00Z"}]
    db.tables["proposal_drafts"] = [
        {"id": "d1", "project_id": "p1", "approved_at": "x", "lines_json": ["Scope"],
         "created_at": "2026-09-01T00:00:00Z"}
    ]
    import app.routers.pricing as pricing

    monkeypatch.setattr(pricing, "get_supabase", lambda: db)
    monkeypatch.setattr(psend, "load_template_bytes", lambda: pytest.fail("rendered"))
    with pytest.raises(ProposalSendError, match="awaiting executive approval"):
        psend.generate_documents("p1", "d1", "u-admin", gc_ids=["g1"])


# ── send / mark-submitted never claim a pending row ───────────────────────────


def _send_ready_db(monkeypatch, env):
    db = env.install(
        _db(
            gcs=[
                _gc("g1", "Alpha", status="pending", requested_by="u-admin"),
                _gc("g2", "Bravo"),
            ],
            sends=[
                {"id": "ps1", "project_id": "p1", "gc_id": "g1", "gc_name": "Alpha",
                 "status": "generated", "file_id": "f1", "draft_id": "d1",
                 "sent_at": None, "sent_via": None},
            ],
        )
    )
    db.tables["proposal_drafts"] = []
    db.tables["verifications"] = []
    import app.routers.pricing as pricing

    monkeypatch.setattr(pricing, "get_supabase", lambda: db)
    monkeypatch.setattr(pricing, "_materials_rows", lambda pid: [])
    monkeypatch.setattr(
        pricing, "section_summary", lambda rows: {"gear": {"includes_generator": False}}
    )
    monkeypatch.setattr(psend.time, "sleep", lambda s: None)
    return db


def test_send_skips_a_pending_gc_without_claiming_its_row(env, monkeypatch):
    db = _send_ready_db(monkeypatch, env)
    out = psend.send_proposals("p1", "u-admin", ["ps1"])
    assert out["results"] == [
        {"proposal_id": "ps1", "gc_id": "g1", "gc_name": "Alpha",
         "status": "skipped", "error": PENDING_APPROVAL_MESSAGE}
    ]
    assert db.tables["proposal_sends"][0]["status"] == "generated"
    # A skip is not a failure: no failure notice goes out.
    assert env.role_notes == []


def test_mark_submitted_skips_a_pending_gc(env, monkeypatch):
    db = _send_ready_db(monkeypatch, env)
    out = psend.mark_submitted("p1", "u-admin", ["ps1"])
    assert out["results"][0]["status"] == "skipped"
    assert out["results"][0]["error"] == PENDING_APPROVAL_MESSAGE
    assert db.tables["proposal_sends"][0]["status"] == "generated"


# ── GC list wire ──────────────────────────────────────────────────────────────


def test_project_gc_rows_carry_send_and_approval_state(env):
    env.install(
        _db(
            gcs=[
                _gc("g1", "Alpha", status="pending", requested_by="u-admin"),
                _gc("g2", "Bravo"),
                _gc("g3", "Charlie"),
                _gc("g4", "Delta"),
            ],
            sends=[
                {"id": "ps2", "project_id": "p1", "gc_id": "g2", "status": "sent",
                 "sent_at": "2026-09-01T18:00:00+00:00", "sent_via": "external"},
                {"id": "ps3", "project_id": "p1", "gc_id": "g3", "status": "superseded",
                 "sent_at": None, "sent_via": None},
                {"id": "ps4", "project_id": "p1", "gc_id": "g4", "status": "generated",
                 "sent_at": None, "sent_via": None},
            ],
        )
    )
    rows = {r["id"]: r for r in psend.project_gc_rows("p1")}
    assert rows["g1"]["proposal_status"] == "not_sent"
    assert rows["g1"]["pricing_approval_status"] == "pending"
    assert rows["g1"]["pricing_requested_by_name"] == "Dana Admin"
    assert rows["g1"]["pricing_decided_by_name"] is None
    assert rows["g2"]["proposal_status"] == "sent"
    assert rows["g2"]["sent_at"] == "2026-09-01T18:00:00+00:00"
    assert rows["g2"]["sent_via"] == "external"
    assert rows["g2"]["pricing_approval_status"] is None
    # superseded = the GC has no live document: reads as not sent.
    assert rows["g3"]["proposal_status"] == "not_sent" and rows["g3"]["sent_at"] is None
    assert rows["g4"]["proposal_status"] == "generated" and rows["g4"]["sent_at"] is None
    # The pre-0118 fields are intact.
    assert rows["g1"]["contacts"][0]["email"] == "pat@g1.com"
    assert rows["g1"]["selected_contact_ids"] == []
    assert [r for r in psend.project_gc_rows("p1")][0]["name"] == "Alpha"  # sorted


# ── notifications: metadata, deep link, targeted dismissal ────────────────────


class _Recorder:
    def __init__(self, profiles):
        self._profiles = profiles
        self._returning = profiles
        self.inserted = []
        self.updates = []
        self.filters = []

    def table(self, name):
        return self

    def select(self, *a, **k):
        self._returning = self._profiles
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def is_(self, *a, **k):
        return self

    def in_(self, col, vals):
        self.filters.append((col, list(vals)))
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        rows = payload if isinstance(payload, list) else [payload]
        self._returning = [{**r, "id": f"n{i}"} for i, r in enumerate(rows)]
        return self

    def update(self, payload):
        self.updates.append(payload)
        self._returning = []
        return self

    def execute(self):
        return SimpleNamespace(data=self._returning)


def test_notify_role_and_user_write_metadata(monkeypatch):
    rec = _Recorder([{"id": "e1"}, {"id": "e2"}])
    monkeypatch.setattr(notif, "get_supabase", lambda: rec)
    queued = []
    monkeypatch.setattr(notif.notification_email, "queue", lambda rows: queued.append(rows))
    notif.notify_role(Role.EXECUTIVE, "p1", "gc_pricing.approval_requested", "m",
                      metadata={"gc_id": "g1"})
    assert all(r["metadata"] == {"gc_id": "g1"} for r in rec.inserted[0])
    # The mirror email gets the inserted rows, metadata included.
    assert queued[0][0]["metadata"] == {"gc_id": "g1"}
    notif.notify_user("u1", "p1", "gc_pricing.approved", "m", metadata={"gc_id": "g1"})
    assert rec.inserted[1]["metadata"] == {"gc_id": "g1"}


def test_notify_without_metadata_writes_no_metadata_key(monkeypatch):
    # Pre-0118 callers keep their exact insert shape (a deployment whose
    # PostgREST cache predates the column must not start failing on them).
    rec = _Recorder([{"id": "e1"}])
    monkeypatch.setattr(notif, "get_supabase", lambda: rec)
    monkeypatch.setattr(notif.notification_email, "queue", lambda rows: None)
    notif.notify_user("u1", "p1", "verified", "m")
    assert "metadata" not in rec.inserted[0]


def test_dismiss_notifications_can_target_one_gc(monkeypatch):
    rec = _Recorder([])
    monkeypatch.setattr(notif, "get_supabase", lambda: rec)
    notif.dismiss_notifications(
        project_id="p1", types=["gc_pricing.approval_requested"], gc_id="g1"
    )
    assert ("metadata->>gc_id", "g1") in rec.filters
    assert ("project_id", "p1") in rec.filters
    assert rec.updates == [{"dismissed_at": "now()"}]


FRONTEND = "https://bdr.example.com"


def _patch_frontend(monkeypatch):
    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(frontend_url=FRONTEND + "/"))


def test_deep_link_for_gc_pricing_opens_the_gc_modal(monkeypatch):
    _patch_frontend(monkeypatch)
    link = ne._deep_link("p1", Role.EXECUTIVE.value, "gc_pricing.approval_requested",
                         {"gc_id": "g1"})
    assert link == f"{FRONTEND}/projects/p1?box=gcs&gc=g1"
    link = ne._deep_link("p1", Role.ESTIMATING_ADMIN.value, "gc_pricing.approved",
                         {"gc_id": "g1"})
    assert link == f"{FRONTEND}/projects/p1?box=gcs&gc=g1"


def test_deep_link_ignores_metadata_for_other_types_and_missing_gc(monkeypatch):
    _patch_frontend(monkeypatch)
    assert ne._deep_link("p1", Role.EXECUTIVE.value, "verified", {"gc_id": "g1"}) == (
        f"{FRONTEND}/projects/p1"
    )
    assert ne._deep_link("p1", Role.EXECUTIVE.value, "gc_pricing.rejected", None) == (
        f"{FRONTEND}/projects/p1"
    )
    assert ne._deep_link("p1", Role.EXECUTIVE.value, "gc_pricing.rejected", {}) == (
        f"{FRONTEND}/projects/p1"
    )


def test_gc_pricing_email_headings():
    assert ne._meta("gc_pricing.approval_requested") == (
        "GC pricing change needs your approval", "Review pricing"
    )
    assert ne._meta("gc_pricing.approved")[1] == "Send the proposal"
    assert ne.heading_for("gc_pricing.rejected") == "GC pricing change rejected"


# ── dashboard count + schemas ─────────────────────────────────────────────────


def test_pending_counts_per_project(monkeypatch):
    import app.routers.projects as projects

    db = FakeDB(
        {
            "project_gcs": [
                {"project_id": "p1", "gc_id": "g1", "pricing_approval_status": "pending"},
                {"project_id": "p1", "gc_id": "g2", "pricing_approval_status": "pending"},
                {"project_id": "p1", "gc_id": "g3", "pricing_approval_status": "approved"},
                {"project_id": "p2", "gc_id": "g4", "pricing_approval_status": None},
                {"project_id": "p9", "gc_id": "g5", "pricing_approval_status": "pending"},
            ]
        }
    )
    monkeypatch.setattr(projects, "get_supabase", lambda: db)
    assert projects._pending_gc_pricing_counts(["p1", "p2"]) == {"p1": 2}
    assert projects._pending_gc_pricing_counts([]) == {}


def test_present_attaches_the_pending_count(monkeypatch):
    import app.routers.projects as projects

    base = {"id": "p1", "current_stage": "submitted", "abandoned_at": None, "bid_outcomes": []}
    out = projects._present(dict(base), Role.EXECUTIVE, None, 2)
    assert out["gc_pricing_approvals_pending"] == 2
    # Callers that do not supply a count leave the schema default in charge.
    assert "gc_pricing_approvals_pending" not in projects._present(dict(base), Role.EXECUTIVE)


def test_project_out_defaults_pending_to_zero():
    assert ProjectOut.model_fields["gc_pricing_approvals_pending"].default == 0


def test_request_schema_carries_amounts_and_note():
    body = ProposalAmountsRequestIn(material_amount="45000", note="hi")
    assert body.material_amount == Decimal("45000") and body.note == "hi"
    assert body.labor_amount is None
    with pytest.raises(ValueError):
        ProposalAmountsRequestIn(note="x" * 1001)


def test_generate_schema_accepts_gc_ids():
    assert ProposalGenerateIn().gc_ids is None
    assert ProposalGenerateIn(gc_ids=["g1"]).gc_ids == ["g1"]
