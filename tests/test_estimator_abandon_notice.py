"""The external estimator is TOLD when the bid under them dies — or comes back.

test_abandon_lockout pins the taking-away half (portal shut, bells swept, every
push path refused). This pins the telling half: whoever is actively assigned
gets a portal bell row plus a branded email, in both directions.

Three properties matter more than the wording:

- **Order.** The withdrawn bell is created AFTER the sweep that clears the
  estimator's other bells — it's the row that explains where they went, so a
  sweep running afterwards would erase the only explanation.
- **Best-effort.** The status flip is already committed when these run. A dead
  mailbox or a PostgREST blip may not turn a successful abandon into a 500.
- **Active assignees only**, and no deep link into the project: its portal
  routes 403 while the marker is set.
"""

from types import SimpleNamespace

import pytest

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import projects as proj_mod
from app.services import estimator_email
from app.services import estimator_lifecycle as lc
from app.services.notifications import ESTIMATOR_NOTIFICATION_TYPES

PROJECT = {
    "id": "p1",
    "name": "Acme Clinic",
    "number": "0042",
    "due_from_estimator_at": "2026-08-05",
}


# ── chainable fake serving one queued result per table ────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        queue = self.db.queues.get(self.table_name) or []
        return SimpleNamespace(data=queue.pop(0) if queue else [])


class _FakeDB:
    def __init__(self, **tables):
        self.queues = {name: list(rows) for name, rows in tables.items()}

    def table(self, name):
        return _Query(self, name)


def _assignee(est_id="e1", email="e1@vendor.com", name="Dana Reyes"):
    return {"estimator_id": est_id, "profiles": {"email": email, "full_name": name}}


@pytest.fixture
def notices(monkeypatch):
    """Wire the service to recorders: bells, dismissals and sends, in order."""
    log = SimpleNamespace(bells=[], dismissed=[], sent=[])
    monkeypatch.setattr(
        lc, "notify_user",
        lambda uid, pid, type_, msg, **kw: log.bells.append(
            {"user_id": uid, "project_id": pid, "type": type_, "message": msg, **kw}
        ),
    )
    monkeypatch.setattr(lc, "dismiss_notifications", lambda **kw: log.dismissed.append(kw))
    monkeypatch.setattr(estimator_email, "graph_configured", lambda: True)
    monkeypatch.setattr(
        estimator_email, "send_withdrawn",
        lambda **kw: log.sent.append({"kind": "withdrawn", **kw}) or {"id": "log1"},
    )
    monkeypatch.setattr(
        estimator_email, "send_reactivated",
        lambda **kw: log.sent.append({"kind": "reactivated", **kw}) or {"id": "log2"},
    )
    return log


# ── withdrawn ─────────────────────────────────────────────────────────────


def test_every_active_assignee_gets_a_bell_and_an_email(monkeypatch, notices):
    db = _FakeDB(estimator_assignments=[[_assignee(), _assignee("e2", "e2@vendor.com", "Sam Ng")]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)

    lc.notify_withdrawn(PROJECT, note="The GC pulled the job.", actor_id="u1")

    assert {b["user_id"] for b in notices.bells} == {"e1", "e2"}
    for b in notices.bells:
        assert b["type"] == lc.WITHDRAWN_TYPE
        assert b["project_id"] == "p1"
        assert "0042" in b["message"] and "Acme Clinic" in b["message"]
        # The reason travels with the notice, not just into the audit log.
        assert "The GC pulled the job." in b["message"]
        # The generic mirror would deep-link into the project the estimator can
        # no longer open — this event sends its own branded email instead.
        assert b["mirror_email"] is False
    assert {s["to"][0] for s in notices.sent} == {"e1@vendor.com", "e2@vendor.com"}
    assert all(s["kind"] == "withdrawn" for s in notices.sent)
    assert notices.sent[0]["note"] == "The GC pulled the job."
    assert notices.sent[0]["sent_by"] == "u1"
    assert notices.sent[0]["recipient_name"] == "Dana Reyes"


def test_the_withdrawn_notice_survives_the_bell_sweep():
    """Abandon sweeps the estimator's other bells; this type is deliberately not
    in that list, or the only explanation would be swept with them."""
    assert lc.WITHDRAWN_TYPE not in ESTIMATOR_NOTIFICATION_TYPES
    assert lc.REACTIVATED_TYPE not in ESTIMATOR_NOTIFICATION_TYPES


def test_a_long_reason_is_clipped_for_the_bell_but_not_the_email(monkeypatch, notices):
    db = _FakeDB(estimator_assignments=[[_assignee()]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)
    reason = "x" * 900

    lc.notify_withdrawn(PROJECT, note=reason)

    assert notices.bells[0]["message"].endswith("…")
    assert len(notices.bells[0]["message"]) < 500
    assert notices.sent[0]["note"] == reason  # the email keeps the whole thing


def test_no_reason_means_no_reason_clause(monkeypatch, notices):
    db = _FakeDB(estimator_assignments=[[_assignee()]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)

    lc.notify_withdrawn(PROJECT, note="   ")

    assert "Reason" not in notices.bells[0]["message"]


def test_a_stale_reactivated_bell_is_dismissed(monkeypatch, notices):
    """The two notices contradict each other — the newer one wins."""
    db = _FakeDB(estimator_assignments=[[_assignee()]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)

    lc.notify_withdrawn(PROJECT)

    assert notices.dismissed == [{"project_id": "p1", "types": [lc.REACTIVATED_TYPE]}]


def test_bell_still_lands_when_graph_is_unconfigured(monkeypatch, notices):
    """Locally (and in tests) there is no mailbox — the portal half must still
    happen rather than the whole notice being skipped."""
    db = _FakeDB(estimator_assignments=[[_assignee()]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)
    monkeypatch.setattr(estimator_email, "graph_configured", lambda: False)

    lc.notify_withdrawn(PROJECT)

    assert len(notices.bells) == 1
    assert notices.sent == []


def test_an_assignee_with_no_email_still_gets_the_bell(monkeypatch, notices):
    db = _FakeDB(estimator_assignments=[[_assignee(email="")]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)

    lc.notify_withdrawn(PROJECT)

    assert len(notices.bells) == 1
    assert notices.sent == []


def test_one_bad_recipient_never_stops_the_rest(monkeypatch, notices):
    """The abandon is committed — a single failing send may not swallow the
    other estimator's notice, and may not raise."""
    db = _FakeDB(estimator_assignments=[[_assignee(), _assignee("e2", "e2@vendor.com")]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)

    def _boom(**kw):
        if kw["to"] == ["e1@vendor.com"]:
            raise RuntimeError("mailbox down")
        notices.sent.append({"kind": "withdrawn", **kw})

    monkeypatch.setattr(estimator_email, "send_withdrawn", _boom)

    lc.notify_withdrawn(PROJECT)  # no raise

    assert [s["to"][0] for s in notices.sent] == ["e2@vendor.com"]


def test_a_failed_lookup_never_breaks_the_abandon(monkeypatch, notices):
    class _Dead:
        def table(self, _name):
            raise RuntimeError("postgrest down")

    monkeypatch.setattr(lc, "get_supabase", lambda: _Dead())

    lc.notify_withdrawn(PROJECT)  # no raise

    assert notices.bells == [] and notices.sent == []


# ── reactivated ───────────────────────────────────────────────────────────


def test_reactivate_tells_them_it_is_back_on(monkeypatch, notices):
    db = _FakeDB(estimator_assignments=[[_assignee()]])
    monkeypatch.setattr(lc, "get_supabase", lambda: db)

    lc.notify_reactivated(PROJECT, actor_id="u1")

    assert notices.bells[0]["type"] == lc.REACTIVATED_TYPE
    assert "0042" in notices.bells[0]["message"]
    # And the withdrawn row, now false, is cleared.
    assert notices.dismissed == [{"project_id": "p1", "types": [lc.WITHDRAWN_TYPE]}]
    assert notices.sent[0]["kind"] == "reactivated"
    assert notices.sent[0]["to"] == ["e1@vendor.com"]


# ── the emails themselves ─────────────────────────────────────────────────


def test_withdrawn_email_says_stop_work_and_never_links_into_the_project():
    html = estimator_email.render_withdrawn_email(
        proj=PROJECT, recipient_name="Dana Reyes", note="The GC pulled the job."
    )
    assert "Hi Dana," in html
    assert "withdrawn" in html and "Acme Clinic" in html and "0042" in html
    assert "REASON" in html and "The GC pulled the job." in html
    assert "stop work" in html
    # The project's portal routes 403 while abandoned — the button lands on the
    # work list, which shows the row as Withdrawn.
    assert "/estimator/projects/p1" not in html


def test_withdrawn_email_omits_the_reason_block_when_there_is_none():
    html = estimator_email.render_withdrawn_email(proj=PROJECT, recipient_name=None, note=None)
    assert "REASON" not in html
    assert "Hi there," in html


def test_withdrawn_email_escapes_the_reason():
    html = estimator_email.render_withdrawn_email(
        proj=PROJECT, recipient_name=None, note="<script>alert(1)</script>"
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_reactivated_email_restates_the_deadline():
    html = estimator_email.render_reactivated_email(proj=PROJECT, recipient_name="Sam")
    assert "active again" in html
    assert "2026-08-05" in html
    assert "Acme Clinic" in html


# ── the endpoint wiring ───────────────────────────────────────────────────


def _writer():
    return CurrentUser(
        id="u1", email="pa@g3.com", role=Role.ESTIMATING_ADMIN, is_active=True,
        aal="aal2", mfa_enrolled=True,
    )


@pytest.fixture
def endpoint(monkeypatch):
    """abandon/reactivate with everything below the router stubbed to a call log."""
    from tests.test_reverify import FakeDB

    db = FakeDB({
        "projects": [{
            "id": "p1", "name": "Acme Clinic", "number": "0042", "current_stage": "estimating",
            "abandoned_at": None, "pm_stage": None, "due_from_estimator_at": "2026-08-05",
        }],
    })
    monkeypatch.setattr(proj_mod, "get_supabase", lambda: db)
    calls: list[tuple] = []
    monkeypatch.setattr(proj_mod, "audit", lambda *a, **k: calls.append(("audit", a[1], a[4])))
    monkeypatch.setattr(proj_mod, "notify_role", lambda *a, **k: calls.append(("notify_role",)))
    monkeypatch.setattr(
        proj_mod, "_sweep_estimator_notifications", lambda pid: calls.append(("sweep", pid))
    )
    monkeypatch.setattr(
        proj_mod.estimator_lifecycle, "notify_withdrawn",
        lambda proj, **kw: calls.append(("withdrawn", proj, kw)),
    )
    monkeypatch.setattr(
        proj_mod.estimator_lifecycle, "notify_reactivated",
        lambda proj, **kw: calls.append(("reactivated", proj, kw)),
    )
    monkeypatch.setattr(proj_mod.pm, "activate_pm_if_won", lambda *a, **k: None)
    return SimpleNamespace(db=db, calls=calls)


def test_abandon_notifies_after_the_sweep(endpoint):
    from app.models.schemas import AbandonIn

    proj_mod.abandon_project("p1", AbandonIn(note="The GC pulled the job."), _writer())

    order = [c[0] for c in endpoint.calls]
    # The sweep clears the estimator's bells; the withdrawn notice has to land
    # on the cleared bell, so it must come after.
    assert order.index("sweep") < order.index("withdrawn")
    withdrawn = next(c for c in endpoint.calls if c[0] == "withdrawn")
    assert withdrawn[1]["number"] == "0042"          # the estimator's project name
    assert withdrawn[2]["note"] == "The GC pulled the job."
    assert withdrawn[2]["actor_id"] == "u1"
    # And the same reason is still audited internally.
    assert ("audit", "project.abandon", {"stage": "estimating", "note": "The GC pulled the job."}) in endpoint.calls


def test_abandon_without_a_reason_sends_none(endpoint):
    proj_mod.abandon_project("p1", None, _writer())

    withdrawn = next(c for c in endpoint.calls if c[0] == "withdrawn")
    assert withdrawn[2]["note"] is None


def test_reactivate_tells_the_estimator_too(endpoint):
    endpoint.db.tables["projects"][0]["abandoned_at"] = "2026-07-20T17:00:00+00:00"

    proj_mod.reactivate_project("p1", _writer())

    assert [c[0] for c in endpoint.calls].count("reactivated") == 1
