"""The 0172 provenance read side (docs/ASSISTANT_PHASE1_PLAN.md 3.12).

  activity     analytics_metrics.activity_log selects actor_kind on both
               sources, every row carries it ("human" for a pre-0172 row) and
               `actor_kind` filters both queries; the route passes it through.
  file access  events and summary groups carry actor_kind; a person and their
               assistant are separate groups (actor_id, actor_kind), in the
               report and on one file; the filter applies.
  email log    notification_log's sent_by carries actor_kind.
  notes        the project notes and quote notes lists select actor_kind and
               never assistant_run_id.
  bell         GET /notifications strips metadata.assistant_run_id for the
               estimator; internal roles keep it.

No database: the recording fake from tests/test_provenance_writers.py.
"""

from __future__ import annotations

import pytest

from app.core.deps import CurrentUser
from app.core.roles import Role
from tests.test_provenance_writers import RUN, FakeSB

ALICE = "44444444-4444-4444-4444-444444444444"
BOB = "55555555-5555-4555-8555-555555555555"
PROJECT = "11111111-1111-4111-8111-111111111111"
FILE_A = "22222222-2222-4222-8222-222222222222"


def _user(role=Role.EXECUTIVE, uid=ALICE):
    return CurrentUser(id=uid, email="a@g3.test", role=role, is_active=True)


# ── Analytics > Activity ─────────────────────────────────────────────────────


@pytest.fixture
def activity_db(monkeypatch):
    from app.services import analytics_metrics

    db = FakeSB({
        "audit_log": [
            {"id": "a1", "actor_id": ALICE, "action": "labor.review", "entity": "project",
             "entity_id": PROJECT, "payload": {}, "created_at": "2026-10-20T10:00:00+00:00",
             "actor_kind": "assistant", "assistant_run_id": RUN},
            # A row as a pre-0172 database returns it: no actor_kind at all.
            {"id": "a2", "actor_id": ALICE, "action": "project.update", "entity": "project",
             "entity_id": PROJECT, "payload": {}, "created_at": "2026-10-20T09:00:00+00:00"},
        ],
        "stage_events": [
            {"id": "e1", "project_id": PROJECT, "from_stage": None, "to_stage": "intake",
             "note": None, "actor_id": BOB, "entered_at": "2026-10-20T08:00:00+00:00",
             "actor_kind": "human"},
            {"id": "e2", "project_id": PROJECT, "from_stage": "intake", "to_stage": "x",
             "note": None, "actor_id": ALICE, "entered_at": "2026-10-20T11:00:00+00:00",
             "actor_kind": "assistant", "assistant_run_id": RUN},
        ],
        "profiles": [{"id": ALICE, "full_name": "Alice", "role": "executive"},
                     {"id": BOB, "full_name": "Bob", "role": "estimating_admin"}],
        "projects": [{"id": PROJECT, "number": "26.1.1", "name": "Acme"}],
    })
    monkeypatch.setattr(analytics_metrics, "get_supabase", lambda: db)
    return db


def test_activity_rows_carry_actor_kind(activity_db):
    from app.services import analytics_metrics

    out = analytics_metrics.activity_log("year", None, None, None, None, None, 50, 0)
    kinds = {r["id"]: r["actor_kind"] for r in out["rows"]}
    assert kinds == {"a1": "assistant", "a2": "human", "stage:e1": "human",
                     "stage:e2": "assistant"}
    assert all("assistant_run_id" not in r for r in out["rows"])
    cols = {t: c for t, c in activity_db.selects if t in ("audit_log", "stage_events")}
    assert "actor_kind" in cols["audit_log"] and "actor_kind" in cols["stage_events"]
    # "all" applies no filter.
    assert not [e for e in activity_db.eqs if e[1] == "actor_kind"]


@pytest.mark.parametrize("kind, want", [
    ("assistant", {"a1", "stage:e2"}),
    ("human", {"stage:e1"}),
])
def test_activity_actor_kind_filters_both_sources(activity_db, kind, want):
    from app.services import analytics_metrics

    out = analytics_metrics.activity_log("year", None, None, None, None, None, 50, 0,
                                         actor_kind=kind)
    assert {r["id"] for r in out["rows"]} == want
    filtered = {e[0] for e in activity_db.eqs if e[1] == "actor_kind" and e[2] == kind}
    assert filtered == {"audit_log", "stage_events"}


def test_activity_route_passes_the_filter(monkeypatch):
    from app.routers import analytics

    seen = {}

    def _log(*args, **kw):
        seen.update(kw)
        return {"rows": []}

    monkeypatch.setattr(analytics.metrics, "activity_log", _log)
    analytics.activity(range_="year", date_from=None, date_to=None, actor_id=None,
                       action=None, project_id=None, limit=50, offset=0,
                       actor_kind="assistant", user=_user())
    assert seen == {"actor_kind": "assistant"}
    # Called directly without it (the other router tests do): the default.
    analytics.activity(range_="year", date_from=None, date_to=None, actor_id=None,
                       action=None, project_id=None, limit=50, offset=0, user=_user())
    assert seen == {"actor_kind": "all"}


def test_analytics_routes_declare_the_actor_kind_literal():
    import inspect
    import typing

    from app.routers import analytics

    for fn in (analytics.activity, analytics.file_access_report):
        p = inspect.signature(fn).parameters["actor_kind"]
        assert p.default == "all"
        assert typing.get_args(p.annotation) == ("all", "human", "assistant")


# ── file access ──────────────────────────────────────────────────────────────


def _event(i, actor, kind, action="view", **extra):
    row = {"id": f"ev{i}", "created_at": f"2026-10-0{i}T10:00:00+00:00", "actor_id": actor,
           "actor_role": "executive", "project_id": PROJECT, "source": "project_file",
           "file_id": FILE_A, "filename": "E1.pdf", "category": "drawing",
           "action": action, "export_id": None, "detail": None, **extra}
    if kind is not None:
        row["actor_kind"] = kind
    return row


@pytest.fixture
def access_db(monkeypatch):
    from app.services import file_access

    db = FakeSB({
        "file_access_events": [
            _event(5, ALICE, "assistant", assistant_run_id=RUN),
            _event(4, ALICE, "human"),
            _event(3, ALICE, None),           # pre-0172 row
            _event(2, BOB, "human", action="download"),
        ],
        "profiles": [{"id": ALICE, "full_name": "Alice", "email": "a@g3.test",
                      "role": "executive"},
                     {"id": BOB, "full_name": "Bob", "email": "b@g3.test",
                      "role": "estimating_admin"}],
        "projects": [{"id": PROJECT, "number": "26.1.1", "name": "Acme"}],
    })
    monkeypatch.setattr(file_access, "get_supabase", lambda: db)
    return db


def test_access_report_splits_a_person_from_their_assistant(access_db):
    from app.services import file_access

    out = file_access.access_report(date_from=None, date_to=None)
    assert "actor_kind" in dict(access_db.selects)["file_access_events"]
    groups = {(g["actor_id"], g["actor_kind"]): g for g in out["summary"]}
    assert set(groups) == {(ALICE, "assistant"), (ALICE, "human"), (BOB, "human")}
    assert groups[(ALICE, "human")]["views"] == 2      # the pre-0172 row counts as human
    assert groups[(ALICE, "assistant")]["views"] == 1
    assert groups[(ALICE, "assistant")]["actor_name"] == "Alice"
    kinds = {e["id"]: e["actor_kind"] for e in out["events"]}
    assert kinds == {"ev5": "assistant", "ev4": "human", "ev3": "human", "ev2": "human"}
    assert all("assistant_run_id" not in e for e in out["events"])


@pytest.mark.parametrize("kind, want", [("assistant", {"ev5"}), ("human", {"ev4", "ev2"})])
def test_access_report_actor_kind_filter(access_db, kind, want):
    from app.services import file_access

    out = file_access.access_report(date_from=None, date_to=None, actor_kind=kind)
    assert {e["id"] for e in out["events"]} == want
    assert ("file_access_events", "actor_kind", kind) in access_db.eqs


def test_access_report_all_applies_no_filter(access_db):
    from app.services import file_access

    out = file_access.access_report(date_from=None, date_to=None, actor_kind="all")
    assert out["events_total"] == 4
    assert not [e for e in access_db.eqs if e[1] == "actor_kind"]


def test_file_access_users_split_by_actor_kind(access_db):
    from app.services import file_access

    out = file_access.file_access(PROJECT, FILE_A)
    users = {(u["actor_id"], u["actor_kind"]): u for u in out["users"]}
    assert set(users) == {(ALICE, "assistant"), (ALICE, "human"), (BOB, "human")}
    assert users[(ALICE, "human")]["views"] == 2
    assert users[(ALICE, "assistant")]["actor_name"] == "Alice"
    assert all("actor_kind" in e for e in out["events"])


def test_file_access_route_passes_the_filter(monkeypatch):
    from app.routers import analytics

    seen = {}

    def _report(**kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(analytics.file_access, "access_report", _report)
    analytics.file_access_report(
        range_="month", date_from=None, date_to=None, project_id=None, file_id=None,
        actor_id=None, source=None, action=None, include_prefetch=False, limit=100,
        offset=0, actor_kind="assistant", user=_user(),
    )
    assert seen["actor_kind"] == "assistant"


# ── notification log ─────────────────────────────────────────────────────────


def test_notification_log_sent_by_carries_actor_kind():
    from app.services import notification_log as nl

    assert "actor_kind" in nl._EMAIL_SELECT
    assert "assistant_run_id" not in nl._EMAIL_SELECT
    people = nl._People({ALICE: {"full_name": "Alice", "role": "executive"}}, {})

    def _email(i, kind):
        row = {"id": f"m{i}", "to_addrs": "gc@example.com", "subject": f"Subject {i}",
               "status": "sent", "error": None, "created_at": f"2026-10-0{i}T10:00:00+00:00",
               "rfq_id": None, "sent_by": ALICE}
        if kind is not None:
            row["actor_kind"] = kind
        return row

    entries = nl.assemble([], [_email(1, "assistant"), _email(3, None)], {}, people)
    by_subject = {e["subject"]: e["sent_by"] for e in entries}
    assert by_subject["Subject 1"] == {"name": "Alice", "role": "executive",
                                       "actor_kind": "assistant"}
    assert by_subject["Subject 3"]["actor_kind"] == "human"


# ── notes lists ──────────────────────────────────────────────────────────────


def test_notes_list_selects_actor_kind_never_the_run_id(monkeypatch):
    from app.routers import notes

    db = FakeSB({"estimator_notes": [
        {"id": "n1", "project_id": PROJECT, "body": "b", "created_at": "x", "author_id": ALICE,
         "source": None, "mentioned_user_ids": [], "actor_kind": "assistant"},
    ]})
    monkeypatch.setattr(notes, "get_supabase", lambda: db)
    out = notes.list_notes(PROJECT, _user())
    cols = dict(db.selects)["estimator_notes"]
    assert "actor_kind" in cols and "assistant_run_id" not in cols
    assert out[0]["actor_kind"] == "assistant"


def test_quote_notes_list_selects_actor_kind_never_the_run_id(monkeypatch):
    from app.routers import rfqs

    db = FakeSB({
        "quotes": [{"id": "q1", "rfq_id": "r1", "amount": "100.00", "tax_included": True,
                    "tax_rate": None, "is_approved": False, "is_selected": False,
                    "origin": None, "source": None, "notes": None, "quote_file_id": None,
                    "received_at": None, "received_after_submission": False}],
        "quote_notes": [{"id": "qn1", "quote_id": "q1", "body": "b", "author_id": ALICE,
                         "created_at": "x", "actor_kind": "assistant"}],
    })
    monkeypatch.setattr(rfqs, "get_supabase", lambda: db)
    monkeypatch.setattr(rfqs, "_rfq_in_project", lambda sb, pid, rid: {"id": rid})
    rfqs.list_quote_notes(PROJECT, "r1", _user())
    cols = dict(db.selects)["quote_notes"]
    assert "actor_kind" in cols and "assistant_run_id" not in cols


# ── the bell ─────────────────────────────────────────────────────────────────


def _bell_db(monkeypatch, uid):
    from app.routers import notifications

    db = FakeSB({"notifications": [
        {"id": "b1", "user_id": uid, "type": "estimator_note", "message": "m",
         "dismissed_at": None,
         "metadata": {"actor_kind": "assistant", "assistant_run_id": RUN, "gc_id": "g1"}},
        {"id": "b2", "user_id": uid, "type": "assigned", "message": "m",
         "dismissed_at": None, "metadata": None},
    ]})
    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    return notifications, db


def test_estimator_bell_never_sees_the_run_id(monkeypatch):
    notifications, db = _bell_db(monkeypatch, BOB)
    rows = notifications.my_notifications(_user(Role.ESTIMATOR, BOB))
    by_id = {r["id"]: r for r in rows}
    assert by_id["b1"]["metadata"] == {"actor_kind": "assistant", "gc_id": "g1"}
    assert by_id["b2"]["metadata"] is None
    # The stored row is untouched.
    assert db.tables["notifications"][0]["metadata"]["assistant_run_id"] == RUN


def test_internal_bell_keeps_the_metadata(monkeypatch):
    notifications, _ = _bell_db(monkeypatch, ALICE)
    rows = notifications.my_notifications(_user(Role.EXECUTIVE, ALICE))
    assert rows[0]["metadata"]["assistant_run_id"] == RUN
