"""Team email on an internal bid date change.

PATCH /projects that moves `internal_bid_at` emails every active internal user
except the accountant: subject 'Internal bid date changed to <date> for
#<number> <name>', a short body with the old and new dates, a button into the
project. Re-sending the same date is silent; patches that don't touch the date
never read it. Nothing here touches the network: Graph and Supabase are faked,
and the thread hand-off is asserted never to fire while emails are disabled.
"""

from types import SimpleNamespace

import pytest

from app.core.deps import CurrentUser
from app.core.roles import INTERNAL_ROLES, Role, WRITER_ROLES
from app.models.schemas import ProjectUpdate
from app.routers import projects as proj_mod
from app.services import bid_date_change_email as mod
from app.services import datetime_format

FRONTEND = "https://bdr.example.com"
OLD = "2026-08-01T19:00:00+00:00"  # Sat Aug 1st 12:00 PM PDT
NEW = "2026-08-05T18:30:00+00:00"  # Wed Aug 5th 11:30 AM PDT
PROJECT = {"id": "p1", "number": "26-014", "name": "Van Ness Tower"}


# ── chainable Supabase fake (serves queued rows per table+op, records calls) ─


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.op, self.filters, self.one, self.payload = None, [], False, None

    def select(self, *a, **k):
        self.op = self.op or "select"
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    def limit(self, *a):
        return self

    def single(self):
        self.one = True
        return self

    def execute(self):
        self.db.calls.append(self)
        queue = self.db.queues.get((self.table_name, self.op)) or []
        rows = queue.pop(0) if queue else []
        return SimpleNamespace(data=(rows[0] if rows else None) if self.one else rows)


class _FakeDB:
    def __init__(self):
        self.queues: dict[tuple[str, str], list] = {}
        self.calls: list[_Query] = []

    def queue(self, table, op, *responses):
        self.queues.setdefault((table, op), []).extend(responses)

    def table(self, name):
        return _Query(self, name)

    def ops(self, table, op):
        return [c for c in self.calls if c.table_name == table and c.op == op]


@pytest.fixture(autouse=True)
def _pacific(monkeypatch):
    monkeypatch.setattr(
        datetime_format, "get_settings",
        lambda: SimpleNamespace(display_timezone="America/Los_Angeles"),
    )


# ── did it move? ────────────────────────────────────────────────────────────


def test_same_instant_spelled_two_ways_is_not_a_change():
    assert mod.bid_date_changed("2026-08-01T19:00:00+00:00", "2026-08-01T19:00:00Z") is False
    assert mod.bid_date_changed("2026-08-01T12:00:00-07:00", "2026-08-01T19:00:00Z") is False
    assert mod.bid_date_changed(None, None) is False


def test_move_clear_and_set_are_changes():
    assert mod.bid_date_changed(OLD, NEW) is True
    assert mod.bid_date_changed(OLD, None) is True
    assert mod.bid_date_changed(None, NEW) is True


# ── subject + body ──────────────────────────────────────────────────────────


def test_subject_states_the_new_date_and_the_project():
    assert (
        mod.subject_for(PROJECT, NEW)
        == "Internal bid date changed to Wednesday, August 5th 11:30 AM PDT for #26-014 Van Ness Tower"
    )


def test_subject_when_the_date_is_removed():
    assert mod.subject_for(PROJECT, None) == "Internal bid date removed for #26-014 Van Ness Tower"


def test_subject_without_a_project_still_reads():
    assert mod.subject_for({}, NEW) == "Internal bid date changed to Wednesday, August 5th 11:30 AM PDT"


def test_recipients_are_every_internal_role_except_the_accountant():
    assert mod.RECIPIENT_ROLES == INTERNAL_ROLES - {Role.ACCOUNTANT}
    assert Role.ESTIMATOR not in mod.RECIPIENT_ROLES


def test_render_lists_both_dates_the_actor_and_escapes():
    html_out = mod.render_internal_bid_date_email(
        recipient_name="Pat Smith",
        project={**PROJECT, "name": "Van Ness <Tower>"},
        previous=OLD,
        current=NEW,
        changed_by="Alex <Chen>",
        cta_url=f"{FRONTEND}/projects/p1",
    )
    assert "Hi Pat," in html_out
    assert "Internal bid date changed" in html_out
    assert "Previous: Saturday, August 1st 12:00 PM PDT" in html_out
    assert "New: Wednesday, August 5th 11:30 AM PDT" in html_out
    assert "changed by Alex &lt;Chen&gt;" in html_out
    assert f'href="{FRONTEND}/projects/p1"' in html_out
    # Escaping: raw names must not inject markup.
    assert "<Tower>" not in html_out and "<Chen>" not in html_out


def test_render_when_removed_says_none():
    html_out = mod.render_internal_bid_date_email(
        recipient_name=None, project=PROJECT, previous=OLD, current=None,
        changed_by=None, cta_url=f"{FRONTEND}/projects/p1",
    )
    assert "Internal bid date removed" in html_out
    assert "New: none" in html_out
    assert " by " not in html_out.split("Previous:")[0].split("was removed")[1]


# ── queue gating + the send loop ────────────────────────────────────────────


def test_queue_noops_when_emails_disabled(monkeypatch):
    # conftest forces notification_emails_enabled off: no thread may spawn.
    monkeypatch.setattr(
        mod.threading, "Thread",
        lambda *a, **k: pytest.fail("thread spawned while emails disabled"),
    )
    mod.queue_internal_bid_date_change("p1", OLD, NEW, "u1")


def test_run_emails_each_writer_and_skips_blank_addresses(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "select", [PROJECT])
    db.queue(
        "profiles", "select",
        [
            {"id": "a", "full_name": "Ann Admin", "email": "ann@g3.com"},
            {"id": "b", "full_name": "Ben Labor", "email": None},
            {"id": "c", "full_name": "Cy Exec", "email": "cy@g3.com"},
        ],
        [{"full_name": "Alex Chen", "email": "alex@g3.com"}],  # the actor lookup
    )
    monkeypatch.setattr(mod, "get_supabase", lambda: db)
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(frontend_url=FRONTEND + "/"))
    sent = []
    monkeypatch.setattr(mod.graph_email, "send_mail", lambda **kw: sent.append(kw) or {"id": "e1"})

    mod._run("p1", OLD, NEW, "u1")

    # The recipient query asked for exactly the writer roles, active only.
    recipients_q = db.ops("profiles", "select")[0]
    assert ("in", "role", sorted(r.value for r in WRITER_ROLES)) in [
        (k, c, sorted(v)) if k == "in" else (k, c, v) for k, c, v in recipients_q.filters
    ]
    assert ("eq", "is_active", True) in recipients_q.filters

    assert [s["to"] for s in sent] == [["ann@g3.com"], ["cy@g3.com"]]
    for s in sent:
        assert s["subject"] == (
            "Internal bid date changed to Wednesday, August 5th 11:30 AM PDT for #26-014 Van Ness Tower"
        )
        assert s["project_id"] == "p1" and s["sent_by"] == "u1"
        assert "changed by Alex Chen" in s["body_html"]
    assert "Hi Ann," in sent[0]["body_html"] and "Hi Cy," in sent[1]["body_html"]


def test_run_one_bad_recipient_never_stops_the_rest(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "select", [PROJECT])
    db.queue(
        "profiles", "select",
        [{"id": "a", "email": "boom@g3.com"}, {"id": "c", "email": "cy@g3.com"}],
        [],
    )
    monkeypatch.setattr(mod, "get_supabase", lambda: db)
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(frontend_url=FRONTEND))
    sent = []

    def _send(**kw):
        if kw["to"] == ["boom@g3.com"]:
            raise RuntimeError("graph down")
        sent.append(kw["to"])
        return {"id": "e1"}

    monkeypatch.setattr(mod.graph_email, "send_mail", _send)
    mod._run("p1", OLD, NEW, None)
    assert sent == [["cy@g3.com"]]


# ── the PATCH wiring ────────────────────────────────────────────────────────


def _writer():
    return CurrentUser(
        id="u1", email="pa@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True,
        aal="aal2", mfa_enrolled=True,
    )


def _patch_router(monkeypatch, db):
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    monkeypatch.setattr(proj_mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(proj_mod, "_present", lambda row, role, *a: row)
    queued = []
    monkeypatch.setattr(
        mod, "queue_internal_bid_date_change", lambda *a: queued.append(a)
    )
    return queued


def test_patch_that_moves_the_date_queues_the_email(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "select", [{"internal_bid_at": OLD}])
    db.queue("projects", "update", [{**PROJECT, "internal_bid_at": NEW}])
    queued = _patch_router(monkeypatch, db)

    proj_mod.update_project("p1", ProjectUpdate(internal_bid_at=NEW), _writer())

    assert queued == [("p1", OLD, NEW, "u1")]


def test_patch_that_resends_the_same_date_is_silent(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "select", [{"internal_bid_at": OLD}])
    db.queue("projects", "update", [{**PROJECT, "internal_bid_at": OLD}])
    queued = _patch_router(monkeypatch, db)

    proj_mod.update_project("p1", ProjectUpdate(internal_bid_at="2026-08-01T12:00:00-07:00"), _writer())

    assert queued == []


def test_patch_that_clears_the_date_queues_a_removed_email(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "select", [{"internal_bid_at": OLD}])
    db.queue("projects", "update", [{**PROJECT, "internal_bid_at": None}])
    queued = _patch_router(monkeypatch, db)

    proj_mod.update_project("p1", ProjectUpdate(internal_bid_at=None), _writer())

    assert queued == [("p1", OLD, None, "u1")]


def test_patch_without_the_date_never_reads_it_or_emails(monkeypatch):
    db = _FakeDB()
    db.queue("projects", "update", [{**PROJECT, "internal_bid_at": OLD, "notes": "hi"}])
    queued = _patch_router(monkeypatch, db)

    proj_mod.update_project("p1", ProjectUpdate(notes="hi"), _writer())

    assert db.ops("projects", "select") == []
    assert queued == []
