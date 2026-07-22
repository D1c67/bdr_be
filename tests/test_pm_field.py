"""PM field router — milestones, daily logs, RFIs, manpower.

Covers the per-project rfi_number sequencing (including the recompute-once race
path), the answer→answered status convenience, manpower↔daily-log project
integrity, project-scoped 404s (no cross-project id probing), and the date
window filters. The Supabase client is faked with the in-memory builder from
test_reverify, extended with delete/gte/lte/in_/order/limit and the rfis
(project_id, rfi_number) unique constraint.

The expanded RFI log (0068) adds: question sanitization on write, the
company/contact pairing rule, attachment keys resolved against the Documents
hub (unknown key → 400, never a silent drop), attachment_keys replace-on-PATCH
semantics, and the bulk read-side enrichment. `pm_folders.list_project_documents`
is stubbed — the hub's own union is covered in test_pm_folders.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.schemas import (
    DailyLogIn,
    DailyLogUpdate,
    ManpowerIn,
    MilestoneIn,
    MilestoneUpdate,
    RFIClose,
    RFIIn,
    RFIMarkSentIn,
    RFISendIn,
    RFIUpdate,
)
from app.routers import pm_field
from app.services import pm as pm_svc
from app.services import pm_folders
from app.services.sanitize import sanitize_rich_text


# ── Fake Supabase ─────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._in_filters = []  # (col, [vals]) for in_
        self._cmp_filters = []  # (col, op, val) for gte/lte
        self._single = False
        self._orders = []
        self._limit = None

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

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._in_filters.append((col, list(vals)))
        return self

    def gte(self, col, val):
        self._cmp_filters.append((col, "ge", val))
        return self

    def lte(self, col, val):
        self._cmp_filters.append((col, "le", val))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, col, desc=False, **k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    # execution
    def _matches(self, row):
        if not all(row.get(c) == v for c, v in self._filters):
            return False
        if not all(row.get(c) in vals for c, vals in self._in_filters):
            return False
        for col, op, val in self._cmp_filters:
            rv = row.get(col)
            if rv is None:
                return False
            if op == "ge" and not rv >= val:
                return False
            if op == "le" and not rv <= val:
                return False
        return True

    def _check_rfi_unique(self, payload):
        for r in self.db.tables.get("rfis", []):
            if (
                r.get("project_id") == payload.get("project_id")
                and r.get("rfi_number") == payload.get("rfi_number")
            ):
                raise Exception(
                    'duplicate key value violates unique constraint '
                    '"rfis_project_id_rfi_number_key" (23505)'
                )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            # PostgREST semantics: the first .order() is the primary key, so
            # apply keys last-to-first with stable sorts.
            for col, desc in reversed(self._orders):
                hits.sort(key=lambda r: r.get(col), reverse=desc)
            if self._limit is not None:
                hits = hits[: self._limit]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None))
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                if self.table == "rfis":
                    self._check_rfi_unique(p)
                # Postgres fills the pk and returns it; the fake mimics that so
                # callers that use the returned id to write child rows (RFI →
                # rfi_attachments) exercise the same path they do in prod.
                row = {"id": self.db.next_id(), **p}
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
            keep, removed = [], []
            for r in rows:
                (removed if self._matches(r) else keep).append(r)
            self.db.tables[self.table] = keep
            return SimpleNamespace(data=[dict(r) for r in removed])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self._seq = 0

    def next_id(self):
        self._seq += 1
        return f"gen{self._seq}"

    def table(self, name):
        return _Query(self, name)


# ── Setup ─────────────────────────────────────────────────────────────────────

USER = SimpleNamespace(id="u1")

_PROJECTS = [
    {"id": "p1", "name": "Job One", "pm_stage": "construction", "pm_completed_at": None},
    {"id": "p2", "name": "Job Two", "pm_stage": "precon", "pm_completed_at": None},
    {"id": "bid", "name": "Bid Only", "pm_stage": None, "pm_completed_at": None},
]


def _db(**tables):
    return FakeDB({"projects": _PROJECTS, **tables})


def _install(monkeypatch, db, details=None):
    """Point the router AND the require_pm_project guard at the fake; capture audits.

    Pass `details` (a list) to also capture each audit's detail payload.
    """
    audits = []
    monkeypatch.setattr(pm_field, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_svc, "get_supabase", lambda: db)

    def _audit(actor, action, *a, **k):
        audits.append(action)
        if details is not None:
            details.append(a[-1] if a and isinstance(a[-1], dict) else {})

    monkeypatch.setattr(pm_field, "audit", _audit)
    return audits


def _today():
    return datetime.now(timezone.utc).date().isoformat()


# ── The per-project guard ─────────────────────────────────────────────────────


def test_unknown_project_404s(monkeypatch):
    _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as exc:
        pm_field.list_milestones("nope", USER)
    assert exc.value.status_code == 404


def test_bid_only_project_409s(monkeypatch):
    _install(monkeypatch, _db())
    with pytest.raises(HTTPException) as exc:
        pm_field.list_rfis("bid", USER)
    assert exc.value.status_code == 409


# ── Milestones ────────────────────────────────────────────────────────────────


def test_milestones_ordered_sort_order_then_date_nulls_last(monkeypatch):
    _install(monkeypatch, _db(pm_milestones=[
        {"id": "m1", "project_id": "p1", "sort_order": 1, "planned_date": None},
        {"id": "m2", "project_id": "p1", "sort_order": 0, "planned_date": "2026-08-01"},
        {"id": "m3", "project_id": "p1", "sort_order": 0, "planned_date": "2026-07-01"},
        {"id": "m4", "project_id": "p1", "sort_order": 0, "planned_date": None},
        {"id": "other", "project_id": "p2", "sort_order": 0, "planned_date": "2026-01-01"},
    ]))
    assert [m["id"] for m in pm_field.list_milestones("p1", USER)] == [
        "m3", "m2", "m4", "m1"
    ]


def test_milestone_create_and_patch(monkeypatch):
    db = _db(pm_milestones=[])
    audits = _install(monkeypatch, db)
    created = pm_field.create_milestone(
        "p1", MilestoneIn(name="Rough-in complete", sort_order=2), USER
    )
    assert created["project_id"] == "p1" and created["created_by"] == "u1"
    db.tables["pm_milestones"][0]["id"] = "m1"

    updated = pm_field.update_milestone(
        "p1", "m1", MilestoneUpdate(actual_date=date(2026, 7, 10)), USER
    )
    assert updated["actual_date"] == "2026-07-10"
    assert audits == ["milestone.create", "milestone.update"]


def test_milestone_patch_cannot_clear_name(monkeypatch):
    _install(monkeypatch, _db(pm_milestones=[
        {"id": "m1", "project_id": "p1", "name": "Rough-in"},
    ]))
    with pytest.raises(HTTPException) as exc:
        pm_field.update_milestone(
            "p1", "m1", MilestoneUpdate.model_validate({"name": None}), USER
        )
    assert exc.value.status_code == 400


def test_milestone_empty_patch_400s(monkeypatch):
    _install(monkeypatch, _db(pm_milestones=[
        {"id": "m1", "project_id": "p1", "name": "Rough-in"},
    ]))
    with pytest.raises(HTTPException) as exc:
        pm_field.update_milestone("p1", "m1", MilestoneUpdate(), USER)
    assert exc.value.status_code == 400


def test_milestone_delete_removes_row(monkeypatch):
    db = _db(pm_milestones=[{"id": "m1", "project_id": "p1", "name": "Rough-in"}])
    audits = _install(monkeypatch, db)
    pm_field.delete_milestone("p1", "m1", USER)
    assert db.tables["pm_milestones"] == []
    assert audits == ["milestone.delete"]


# ── Cross-project id probing ──────────────────────────────────────────────────


def test_patch_from_another_project_404s_and_leaves_row(monkeypatch):
    db = _db(pm_milestones=[{"id": "m1", "project_id": "p1", "name": "Rough-in"}])
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        pm_field.update_milestone("p2", "m1", MilestoneUpdate(name="Hijack"), USER)
    assert exc.value.status_code == 404
    assert db.tables["pm_milestones"][0]["name"] == "Rough-in"


def test_delete_from_another_project_404s_and_leaves_row(monkeypatch):
    db = _db(rfis=[{"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"}])
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        pm_field.delete_rfi("p2", "r1", USER)
    assert exc.value.status_code == 404
    assert len(db.tables["rfis"]) == 1


# ── Daily logs: date window + ordering ───────────────────────────────────────


_LOGS = [
    {"id": "d1", "project_id": "p1", "log_date": "2026-07-01", "created_at": "2026-07-01T08:00:00Z"},
    {"id": "d2", "project_id": "p1", "log_date": "2026-07-02", "created_at": "2026-07-02T08:00:00Z"},
    {"id": "d3", "project_id": "p1", "log_date": "2026-07-02", "created_at": "2026-07-02T17:00:00Z"},
    {"id": "d4", "project_id": "p1", "log_date": "2026-07-05", "created_at": "2026-07-05T08:00:00Z"},
    {"id": "other", "project_id": "p2", "log_date": "2026-07-02", "created_at": "2026-07-02T09:00:00Z"},
]


def test_daily_logs_ordered_desc_then_created_desc(monkeypatch):
    _install(monkeypatch, _db(daily_logs=_LOGS))
    assert [r["id"] for r in pm_field.list_daily_logs("p1", None, None, USER)] == [
        "d4", "d3", "d2", "d1"
    ]


def test_daily_logs_date_window(monkeypatch):
    _install(monkeypatch, _db(daily_logs=_LOGS))
    rows = pm_field.list_daily_logs("p1", date(2026, 7, 2), date(2026, 7, 4), USER)
    assert [r["id"] for r in rows] == ["d3", "d2"]
    rows = pm_field.list_daily_logs("p1", date(2026, 7, 3), None, USER)
    assert [r["id"] for r in rows] == ["d4"]


def test_daily_log_create_and_required_field_guard(monkeypatch):
    db = _db(daily_logs=[])
    audits = _install(monkeypatch, db)
    pm_field.create_daily_log(
        "p1", DailyLogIn(log_date=date(2026, 7, 10), work_performed="Pulled feeders"), USER
    )
    assert db.tables["daily_logs"][0]["work_performed"] == "Pulled feeders"
    db.tables["daily_logs"][0]["id"] = "d1"
    with pytest.raises(HTTPException) as exc:
        pm_field.update_daily_log(
            "p1", "d1", DailyLogUpdate.model_validate({"work_performed": None}), USER
        )
    assert exc.value.status_code == 400
    assert audits == ["dailylog.create"]


# ── RFIs: numbering ───────────────────────────────────────────────────────────


def test_rfi_numbers_sequence_per_project(monkeypatch):
    db = _db(rfis=[])
    _install(monkeypatch, db)
    a = pm_field.create_rfi("p1", RFIIn(subject="Panel schedule", question="Q1"), USER)
    b = pm_field.create_rfi("p1", RFIIn(subject="Feeder size", question="Q2"), USER)
    c = pm_field.create_rfi("p2", RFIIn(subject="Trench depth", question="Q3"), USER)
    assert (a["rfi_number"], b["rfi_number"], c["rfi_number"]) == (1, 2, 1)


def test_rfi_number_fills_from_max_not_count(monkeypatch):
    # A deleted RFI leaves a gap — the next number continues from the max.
    _install(monkeypatch, _db(rfis=[
        {"id": "r5", "project_id": "p1", "rfi_number": 5, "status": "open"},
    ]))
    created = pm_field.create_rfi("p1", RFIIn(subject="s", question="q"), USER)
    assert created["rfi_number"] == 6


def test_rfi_number_race_recomputes_once(monkeypatch):
    db = _db(rfis=[{"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"}])
    _install(monkeypatch, db)
    real = pm_field._next_rfi_number
    calls = {"n": 0}

    def stale_then_real(pid):
        calls["n"] += 1
        return 1 if calls["n"] == 1 else real(pid)  # stale number → 23505 → retry

    monkeypatch.setattr(pm_field, "_next_rfi_number", stale_then_real)
    created = pm_field.create_rfi("p1", RFIIn(subject="s", question="q"), USER)
    assert created["rfi_number"] == 2 and calls["n"] == 2


def test_rfi_number_double_conflict_is_409(monkeypatch):
    _install(monkeypatch, _db(rfis=[
        {"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"},
    ]))
    monkeypatch.setattr(pm_field, "_next_rfi_number", lambda pid: 1)
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi("p1", RFIIn(subject="s", question="q"), USER)
    assert exc.value.status_code == 409


def test_rfis_listed_by_number(monkeypatch):
    _install(monkeypatch, _db(rfis=[
        {"id": "r2", "project_id": "p1", "rfi_number": 2, "status": "open"},
        {"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open"},
        {"id": "rx", "project_id": "p2", "rfi_number": 1, "status": "open"},
    ]))
    assert [r["id"] for r in pm_field.list_rfis("p1", USER)] == ["r1", "r2"]


# ── RFIs: the answer → answered convenience ───────────────────────────────────


def _open_rfi():
    return {"id": "r1", "project_id": "p1", "rfi_number": 1, "status": "open",
            "answer": None, "answered_at": None}


def test_answering_open_rfi_flips_status_and_stamps_date(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(answer="Use 3/4 EMT"), USER)
    assert updated["status"] == "answered"
    assert updated["answered_at"] == _today()


def test_explicit_answered_at_is_kept(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi(
        "p1", "r1", RFIUpdate(answer="Yes", answered_at=date(2026, 7, 1)), USER
    )
    assert updated["status"] == "answered"
    assert updated["answered_at"] == "2026-07-01"


def test_explicit_status_wins_over_convenience(monkeypatch):
    # An explicit status in the patch skips the answer→answered convenience —
    # here the author records an answer but deliberately keeps the RFI open.
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi(
        "p1", "r1", RFIUpdate(answer="Partial — see follow-up", status="open"), USER
    )
    assert updated["status"] == "open"
    assert updated["answered_at"] is None  # only the convenience path stamps it


def test_patch_cannot_close_an_rfi(monkeypatch):
    # Closing is gated (POST .../close); the generic edit refuses status=closed
    # so the terminal state always carries a responder and a response document.
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        pm_field.update_rfi("p1", "r1", RFIUpdate(status="closed"), USER)
    assert exc.value.status_code == 400
    assert "Close action" in exc.value.detail
    assert db.tables["rfis"][0]["status"] == "open"  # untouched


def test_editing_answer_on_closed_rfi_keeps_status(monkeypatch):
    db = _db(rfis=[{**_open_rfi(), "status": "closed"}])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(answer="typo fix"), USER)
    assert updated["status"] == "closed"


def test_clearing_answer_on_closed_rfi_is_rejected(monkeypatch):
    # The close-gate invariant (closed ⇒ answer recorded) must survive a plain
    # PATCH: nulling the answer while the RFI stays closed is refused so a closed
    # RFI can never end up with no recorded answer.
    db = _db(rfis=[{**_open_rfi(), "status": "closed", "answer": "Use 3/4 EMT"}])
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        pm_field.update_rfi("p1", "r1", RFIUpdate(answer=None), USER)
    assert exc.value.status_code == 400
    assert db.tables["rfis"][0]["answer"] == "Use 3/4 EMT"  # untouched


def test_empty_answer_does_not_flip_status(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(subject="Re-worded"), USER)
    assert updated["status"] == "open"


def test_answer_transition_survives_an_attachment_edit(monkeypatch):
    # The attachments path pops attachment_keys out of the patch before the
    # `rfis` update — the status convenience must be unaffected by that.
    db = _db(rfis=[_open_rfi()], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    updated = pm_field.update_rfi(
        "p1", "r1", RFIUpdate(answer="Use 3/4 EMT", attachment_keys=[_DOC_A]), USER
    )
    assert updated["status"] == "answered"
    assert updated["answered_at"] == _today()
    assert [a["key"] for a in updated["attachments"]] == [_DOC_A]


# ── RFIs: question sanitization ───────────────────────────────────────────────
#
# The frontend renders `question` with dangerouslySetInnerHTML and has no
# sanitizer, so these assert the only line of defense there is.


def test_create_strips_script_and_img_but_keeps_text(monkeypatch):
    db = _db(rfis=[])
    _install(monkeypatch, db)
    created = pm_field.create_rfi(
        "p1",
        RFIIn(
            subject="Panel schedule",
            question=(
                '<p>Which <strong>panel</strong>?<script>alert("xss")</script>'
                '<img src=x onerror=alert(1)> See detail.</p>'
            ),
        ),
        USER,
    )
    q = created["question"]
    assert "<script" not in q and "alert" not in q  # script content dropped, not exposed
    assert "<img" not in q and "onerror" not in q
    assert "Which" in q and "See detail." in q  # the prose survives
    assert "<strong>panel</strong>" in q  # allowlisted markup survives


def test_patch_sanitizes_question_too(monkeypatch):
    db = _db(rfis=[_open_rfi()])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi(
        "p1", "r1", RFIUpdate(question='<p>edit<iframe src="//evil"></iframe>ed</p>'), USER
    )
    assert updated["question"] == "<p>edited</p>"


def test_question_link_is_forced_safe(monkeypatch):
    db = _db(rfis=[])
    _install(monkeypatch, db)
    created = pm_field.create_rfi(
        "p1",
        RFIIn(subject="s", question='<p><a href="https://ex.com" target="_self">spec</a></p>'),
        USER,
    )
    q = created["question"]
    assert 'rel="noopener noreferrer"' in q and 'target="_blank"' in q


def test_question_javascript_url_is_dropped(monkeypatch):
    db = _db(rfis=[])
    _install(monkeypatch, db)
    created = pm_field.create_rfi(
        "p1", RFIIn(subject="s", question='<p><a href="javascript:alert(1)">x</a></p>'), USER
    )
    assert "javascript" not in created["question"]


@pytest.mark.parametrize(
    "vector",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<svg/onload=alert(1)>",
        '<iframe src="javascript:alert(1)">',
        "<body onload=alert(1)>",
        '<a href="javascript:alert(1)">x</a>',
        '<a href="jAvAsCrIpT:alert(1)">x</a>',
        '<a href="java&#115;cript:alert(1)">x</a>',           # entity-encoded scheme
        '<a href="  javascript:alert(1)">x</a>',              # leading whitespace
        '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
        '<p style="background:url(javascript:alert(1))">x</p>',
        '<div onclick="alert(1)">x</div>',
        '<p onmouseover="alert(1)">x</p>',
        '<form action="/steal"><input name="p"></form>',
        '<object data="evil.swf">',
        '<embed src="evil.swf">',
        '<link rel="stylesheet" href="//evil">',
        '<meta http-equiv="refresh" content="0;url=//evil">',
        '<base href="//evil/">',
        "<math><mtext><script>alert(1)</script></mtext></math>",
        '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',  # mutation XSS
        "<template><script>alert(1)</script></template>",
        "<style>@import '//evil';</style>",
        "<!--<script>alert(1)</script>-->",
    ],
)
def test_sanitizer_neutralizes_xss_vectors(vector):
    # This is the ONLY sanitizer in the stack — the frontend renders `question`
    # with dangerouslySetInnerHTML. Anything that survives here reaches the DOM.
    out = sanitize_rich_text(vector).lower()
    for surface in (
        "script", "onerror", "onload", "onclick", "onmouseover", "javascript",
        "data:text/html", "<iframe", "<style", "<form", "<object", "<embed",
        "<meta", "<base", "<link", "<svg", "<img",
    ):
        assert surface not in out, f"{surface!r} survived {vector!r} → {out!r}"


@pytest.mark.parametrize("markup", ["<p></p>", "<p><br></p>", "<p>&nbsp;</p>", "<script>x</script>"])
def test_question_that_renders_to_nothing_is_400(monkeypatch, markup):
    # min_length on the raw HTML can't catch these — `<p></p>` is 7 characters.
    _install(monkeypatch, _db(rfis=[]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi("p1", RFIIn(subject="s", question=markup), USER)
    assert exc.value.status_code == 400
    assert "empty" in exc.value.detail


# ── RFIs: the assignee pair ───────────────────────────────────────────────────


def _gc_db(**tables):
    return _db(
        general_contractors=[{"id": "gc1", "name": "Acme Builders"},
                             {"id": "gc2", "name": "Other GC"}],
        gc_contacts=[{"id": "c1", "gc_id": "gc1", "name": "Dana Ruiz"},
                     {"id": "c2", "gc_id": "gc2", "name": "Sam Vale"},
                     {"id": "orphan", "gc_id": None, "name": "No Company"}],
        profiles=[{"id": "u1", "full_name": "Tom Moore"}],
        **tables,
    )


def test_create_rejects_contact_from_another_company(monkeypatch):
    _install(monkeypatch, _gc_db(rfis=[]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi(
            "p1",
            RFIIn(subject="s", question="<p>q</p>", assigned_gc_id="gc1",
                  assigned_contact_id="c2"),
            USER,
        )
    assert exc.value.status_code == 400
    assert "different company" in exc.value.detail


def test_create_accepts_matching_company_and_contact(monkeypatch):
    db = _gc_db(rfis=[])
    _install(monkeypatch, db)
    created = pm_field.create_rfi(
        "p1",
        RFIIn(subject="s", question="<p>q</p>", assigned_gc_id="gc1",
              assigned_contact_id="c1"),
        USER,
    )
    assert created["assigned_gc_id"] == "gc1"
    assert created["assigned_gc_name"] == "Acme Builders"
    assert created["assigned_contact_name"] == "Dana Ruiz"


def test_unknown_contact_is_400(monkeypatch):
    _install(monkeypatch, _gc_db(rfis=[]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi(
            "p1",
            RFIIn(subject="s", question="<p>q</p>", assigned_gc_id="gc1",
                  assigned_contact_id="ghost"),
            USER,
        )
    assert exc.value.status_code == 400


def test_contact_without_a_company_is_400(monkeypatch):
    _install(monkeypatch, _gc_db(rfis=[]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi(
            "p1",
            RFIIn(subject="s", question="<p>q</p>", assigned_gc_id="gc1",
                  assigned_contact_id="orphan"),
            USER,
        )
    assert exc.value.status_code == 400
    assert "not linked to a company" in exc.value.detail


def test_company_only_assignment_is_allowed(monkeypatch):
    db = _gc_db(rfis=[])
    _install(monkeypatch, db)
    created = pm_field.create_rfi(
        "p1", RFIIn(subject="s", question="<p>q</p>", assigned_gc_id="gc1"), USER
    )
    assert created["assigned_gc_id"] == "gc1" and created["assigned_contact_id"] is None


def test_patching_only_the_company_revalidates_the_existing_contact(monkeypatch):
    # gc2 + the contact already on the row (c1, who works for gc1) is a mismatch
    # even though the patch never mentions the contact.
    _install(monkeypatch, _gc_db(rfis=[
        {**_open_rfi(), "assigned_gc_id": "gc1", "assigned_contact_id": "c1"},
    ]))
    with pytest.raises(HTTPException) as exc:
        pm_field.update_rfi("p1", "r1", RFIUpdate(assigned_gc_id="gc2"), USER)
    assert exc.value.status_code == 400
    assert "different company" in exc.value.detail


def test_clearing_the_contact_is_allowed(monkeypatch):
    db = _gc_db(rfis=[{**_open_rfi(), "assigned_gc_id": "gc1", "assigned_contact_id": "c1"}])
    _install(monkeypatch, db)
    updated = pm_field.update_rfi(
        "p1", "r1", RFIUpdate.model_validate({"assigned_contact_id": None}), USER
    )
    assert updated["assigned_contact_id"] is None
    assert updated["assigned_contact_name"] is None


# ── RFIs: attachments ─────────────────────────────────────────────────────────

_DOC_A = "pm:11111111-1111-1111-1111-111111111111"
_DOC_B = "bid:22222222-2222-2222-2222-222222222222"
_DOC_P2 = "pm:33333333-3333-3333-3333-333333333333"  # belongs to project p2
_DOC_GONE = "pm:44444444-4444-4444-4444-444444444444"  # deleted from the hub

_HUB = {
    "p1": [
        {"key": _DOC_A, "source": "pm", "folder": "plans", "filename": "panel.pdf",
         "size_bytes": 10, "storage_path": "p1/pm/drawing/panel.pdf"},
        {"key": _DOC_B, "source": "bid", "folder": "specifications", "filename": "spec.pdf",
         "size_bytes": 20, "storage_path": "p1/bid/spec.pdf"},
    ],
    "p2": [
        {"key": _DOC_P2, "source": "pm", "folder": "plans", "filename": "other.pdf",
         "size_bytes": 30, "storage_path": "p2/pm/drawing/other.pdf"},
    ],
}


def _stub_hub(monkeypatch):
    """Stub the hub listing and count the resolves (the read must be bulk)."""
    calls = {"n": 0}

    def _list(project_id):
        calls["n"] += 1
        return [dict(d) for d in _HUB.get(project_id, [])]

    monkeypatch.setattr(pm_folders, "list_project_documents", _list)
    return calls


def test_create_with_attachments_writes_the_link_rows(monkeypatch):
    db = _gc_db(rfis=[], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    created = pm_field.create_rfi(
        "p1", RFIIn(subject="s", question="<p>q</p>", attachment_keys=[_DOC_A, _DOC_B]), USER
    )
    assert all(a["rfi_id"] == created["id"] for a in db.tables["rfi_attachments"])
    assert {a["doc_key"] for a in db.tables["rfi_attachments"]} == {_DOC_A, _DOC_B}
    assert all(a["created_by"] == "u1" for a in db.tables["rfi_attachments"])
    assert [a["key"] for a in created["attachments"]] == [_DOC_A, _DOC_B]


def test_attachment_keys_never_reach_the_rfis_table(monkeypatch):
    # attachment_keys is not a column on `rfis` — it must be popped, or the
    # insert blows up against the real schema.
    db = _gc_db(rfis=[], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    pm_field.create_rfi(
        "p1", RFIIn(subject="s", question="<p>q</p>", attachment_keys=[_DOC_A]), USER
    )
    assert "attachment_keys" not in db.tables["rfis"][0]


def test_create_rejects_an_attachment_from_another_project(monkeypatch):
    # The whole point: p2's document is not in p1's hub listing.
    db = _gc_db(rfis=[], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi(
            "p1", RFIIn(subject="s", question="<p>q</p>", attachment_keys=[_DOC_P2]), USER
        )
    assert exc.value.status_code == 400
    assert "not in this project" in exc.value.detail
    # and nothing was created: no orphan RFI, no burned number
    assert db.tables["rfis"] == [] and db.tables["rfi_attachments"] == []


def test_create_rejects_an_unknown_attachment_key(monkeypatch):
    db = _gc_db(rfis=[], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.create_rfi(
            "p1", RFIIn(subject="s", question="<p>q</p>", attachment_keys=[_DOC_GONE]), USER
        )
    assert exc.value.status_code == 400


def test_patch_attachment_keys_replaces_the_set(monkeypatch):
    db = _gc_db(
        rfis=[_open_rfi()],
        rfi_attachments=[{"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "u1"}],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(attachment_keys=[_DOC_B]), USER)
    assert {a["doc_key"] for a in db.tables["rfi_attachments"]} == {_DOC_B}
    assert [a["key"] for a in updated["attachments"]] == [_DOC_B]


def test_patch_attachments_diffs_instead_of_churning(monkeypatch):
    # _DOC_A survives the edit, so its row must be left exactly as it was —
    # re-inserting it would lose who attached it and when.
    keep = {"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "someone_else"}
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[dict(keep)])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    pm_field.update_rfi("p1", "r1", RFIUpdate(attachment_keys=[_DOC_A, _DOC_B]), USER)
    rows = {a["doc_key"]: a for a in db.tables["rfi_attachments"]}
    assert set(rows) == {_DOC_A, _DOC_B}
    assert rows[_DOC_A] == keep  # untouched
    assert rows[_DOC_B]["created_by"] == "u1"


def test_patch_with_empty_attachment_keys_detaches_all(monkeypatch):
    db = _gc_db(
        rfis=[_open_rfi()],
        rfi_attachments=[{"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "u1"}],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(attachment_keys=[]), USER)
    assert db.tables["rfi_attachments"] == []
    assert updated["attachments"] == []


def test_patch_without_attachment_keys_leaves_them(monkeypatch):
    db = _gc_db(
        rfis=[_open_rfi()],
        rfi_attachments=[{"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "u1"}],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(subject="Re-worded"), USER)
    assert [a["doc_key"] for a in db.tables["rfi_attachments"]] == [_DOC_A]
    assert [a["key"] for a in updated["attachments"]] == [_DOC_A]  # still enriched


def test_patch_attachments_only_does_not_404(monkeypatch):
    # A body of just attachment_keys leaves no `rfis` column to update; the
    # write must be skipped rather than issued as an empty UPDATE.
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    updated = pm_field.update_rfi("p1", "r1", RFIUpdate(attachment_keys=[_DOC_A]), USER)
    assert [a["key"] for a in updated["attachments"]] == [_DOC_A]
    assert updated["status"] == "open"  # untouched


def test_patch_rejects_a_cross_project_attachment_and_writes_nothing(monkeypatch):
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.update_rfi(
            "p1", "r1", RFIUpdate(subject="new", attachment_keys=[_DOC_P2]), USER
        )
    assert exc.value.status_code == 400
    assert db.tables["rfis"][0].get("subject") != "new"  # the whole patch is rejected
    assert db.tables["rfi_attachments"] == []


def test_attach_and_detach_ride_on_the_existing_audit_events(monkeypatch):
    # No new event types: rfi.create / rfi.update carry the attachment detail so
    # the Activity feed needs no new labels (analytics_metrics ACTIVITY_LABELS).
    db = _gc_db(
        rfis=[_open_rfi()],
        rfi_attachments=[{"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "u1"}],
    )
    details = []
    audits = _install(monkeypatch, db, details)
    _stub_hub(monkeypatch)
    pm_field.update_rfi("p1", "r1", RFIUpdate(attachment_keys=[_DOC_B]), USER)
    assert audits == ["rfi.update"]
    assert details[0]["attached"] == [_DOC_B]
    assert details[0]["detached"] == [_DOC_A]


def test_untouched_attachments_are_absent_from_the_audit(monkeypatch):
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[])
    details = []
    _install(monkeypatch, db, details)
    _stub_hub(monkeypatch)
    pm_field.update_rfi("p1", "r1", RFIUpdate(subject="Re-worded"), USER)
    assert "attached" not in details[0] and "detached" not in details[0]


# ── RFIs: closing ─────────────────────────────────────────────────────────────


def test_close_records_responder_answer_and_answer_docs(monkeypatch):
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[])
    details = []
    audits = _install(monkeypatch, db, details)
    _stub_hub(monkeypatch)
    closed = pm_field.close_rfi(
        "p1", "r1",
        RFIClose(answer="Use 3/4 EMT", answered_by="Dana Ruiz — Acme PM",
                 attachment_keys=[_DOC_A]),
        USER,
    )
    assert closed["status"] == "closed"
    assert closed["answer"] == "Use 3/4 EMT"
    assert closed["answered_by"] == "Dana Ruiz — Acme PM"
    assert closed["answered_at"] == _today()
    # The answer document is its own kind, never mixed into the request's exhibits.
    assert [a["key"] for a in closed["answer_attachments"]] == [_DOC_A]
    assert closed["attachments"] == []
    rows = db.tables["rfi_attachments"]
    assert len(rows) == 1 and rows[0]["kind"] == "answer" and rows[0]["created_by"] == "u1"
    assert audits == ["rfi.close"] and details[0]["answer_attached"] == [_DOC_A]


def test_close_keeps_an_explicit_answered_at(monkeypatch):
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    closed = pm_field.close_rfi(
        "p1", "r1",
        RFIClose(answer="Yes", answered_by="EOR", answered_at=date(2026, 7, 1),
                 attachment_keys=[_DOC_A]),
        USER,
    )
    assert closed["answered_at"] == "2026-07-01"


def test_close_adds_answer_docs_without_touching_request_exhibits(monkeypatch):
    # A request exhibit (kind defaults to question) survives the close; the
    # answer doc is added alongside it, not in place of it.
    db = _gc_db(
        rfis=[_open_rfi()],
        rfi_attachments=[{"id": "q1", "rfi_id": "r1", "doc_key": _DOC_A,
                          "created_by": "u1", "kind": "question"}],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    closed = pm_field.close_rfi(
        "p1", "r1", RFIClose(answer="ok", answered_by="X", attachment_keys=[_DOC_B]), USER
    )
    assert [a["key"] for a in closed["attachments"]] == [_DOC_A]         # request exhibit kept
    assert [a["key"] for a in closed["answer_attachments"]] == [_DOC_B]  # answer doc added
    assert len(db.tables["rfi_attachments"]) == 2


def test_close_rejects_a_cross_project_answer_doc_and_writes_nothing(monkeypatch):
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.close_rfi(
            "p1", "r1", RFIClose(answer="x", answered_by="y", attachment_keys=[_DOC_P2]), USER
        )
    assert exc.value.status_code == 400
    assert db.tables["rfis"][0]["status"] == "open"  # validated before the update
    assert db.tables["rfi_attachments"] == []


def test_close_on_an_already_closed_rfi_is_409(monkeypatch):
    db = _gc_db(rfis=[{**_open_rfi(), "status": "closed"}], rfi_attachments=[])
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.close_rfi(
            "p1", "r1", RFIClose(answer="x", answered_by="y", attachment_keys=[_DOC_A]), USER
        )
    assert exc.value.status_code == 409


def test_close_requires_an_answer_document():
    with pytest.raises(ValidationError):
        RFIClose(answer="x", answered_by="y", attachment_keys=[])


@pytest.mark.parametrize("field", ["answer", "answered_by"])
def test_close_requires_nonblank_text(field):
    kw = {"answer": "x", "answered_by": "y", "attachment_keys": [_DOC_A]}
    kw[field] = "   "
    with pytest.raises(ValidationError):
        RFIClose(**kw)


# ── RFIs: the enriched read ───────────────────────────────────────────────────


def test_list_rfis_enriches_names_and_attachments(monkeypatch):
    db = _gc_db(
        rfis=[{**_open_rfi(), "assigned_gc_id": "gc1", "assigned_contact_id": "c1",
               "created_by": "u1"}],
        rfi_attachments=[{"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "u1"}],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    [row] = pm_field.list_rfis("p1", USER)
    assert row["assigned_gc_name"] == "Acme Builders"
    assert row["assigned_contact_name"] == "Dana Ruiz"
    assert row["created_by_name"] == "Tom Moore"
    assert row["attachments"] == [
        {"id": "a1", "key": _DOC_A, "filename": "panel.pdf", "folder": "plans",
         "source": "pm", "size_bytes": 10}
    ]


def test_enriched_attachments_never_leak_storage_path(monkeypatch):
    db = _gc_db(
        rfis=[_open_rfi()],
        rfi_attachments=[{"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "u1"}],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    [row] = pm_field.list_rfis("p1", USER)
    assert "storage_path" not in row["attachments"][0]


def test_list_rfis_resolves_the_hub_once_for_the_whole_page(monkeypatch):
    db = _gc_db(
        rfis=[
            {"id": f"r{n}", "project_id": "p1", "rfi_number": n, "status": "open",
             "assigned_gc_id": "gc1", "created_by": "u1"}
            for n in range(1, 6)
        ],
        rfi_attachments=[
            {"id": f"a{n}", "rfi_id": f"r{n}", "doc_key": _DOC_A, "created_by": "u1"}
            for n in range(1, 6)
        ],
    )
    _install(monkeypatch, db)
    calls = _stub_hub(monkeypatch)
    rows = pm_field.list_rfis("p1", USER)
    assert len(rows) == 5
    assert all(r["attachments"] and r["assigned_gc_name"] == "Acme Builders" for r in rows)
    assert calls["n"] == 1  # not one per RFI


def test_list_rfis_without_attachments_skips_the_hub(monkeypatch):
    db = _gc_db(rfis=[_open_rfi()], rfi_attachments=[])
    _install(monkeypatch, db)
    calls = _stub_hub(monkeypatch)
    [row] = pm_field.list_rfis("p1", USER)
    assert row["attachments"] == []
    assert calls["n"] == 0


def test_attachment_for_a_deleted_document_drops_out(monkeypatch):
    # Soft reference (0068): the hub no longer lists it, so it disappears from
    # the RFI instead of dangling.
    db = _gc_db(
        rfis=[_open_rfi()],
        rfi_attachments=[
            {"id": "a1", "rfi_id": "r1", "doc_key": _DOC_A, "created_by": "u1"},
            {"id": "a2", "rfi_id": "r1", "doc_key": _DOC_GONE, "created_by": "u1"},
        ],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    [row] = pm_field.list_rfis("p1", USER)
    assert [a["key"] for a in row["attachments"]] == [_DOC_A]


def test_enrichment_tolerates_missing_names(monkeypatch):
    db = _db(  # no general_contractors / profiles rows at all
        rfis=[{**_open_rfi(), "assigned_gc_id": "ghost", "created_by": "ghost"}],
        rfi_attachments=[],
        general_contractors=[],
        gc_contacts=[],
        profiles=[],
    )
    _install(monkeypatch, db)
    _stub_hub(monkeypatch)
    [row] = pm_field.list_rfis("p1", USER)
    assert row["assigned_gc_name"] is None and row["created_by_name"] is None


# ── RFIs: the new list fields ─────────────────────────────────────────────────


def test_drawing_numbers_are_cleaned_and_ordered():
    body = RFIIn(
        subject="s",
        question="<p>q</p>",
        drawing_numbers=["E-101", "  ", "E-102", "E-101", " E-103 "],
        applicable_references=["Spec 26 05 19"],
    )
    assert body.drawing_numbers == ["E-101", "E-102", "E-103"]  # blanks/dupes out, order kept
    assert body.applicable_references == ["Spec 26 05 19"]


@pytest.mark.parametrize(
    "field", ["drawing_numbers", "applicable_references"]
)
def test_chip_lists_are_capped(field):
    with pytest.raises(ValidationError):
        RFIIn(**{"subject": "s", "question": "<p>q</p>", field: [f"D-{n}" for n in range(51)]})
    with pytest.raises(ValidationError):
        RFIIn(**{"subject": "s", "question": "<p>q</p>", field: ["x" * 101]})


def test_priority_defaults_to_standard_and_rejects_junk():
    assert RFIIn(subject="s", question="<p>q</p>").priority == "standard"
    assert RFIIn(subject="s", question="<p>q</p>", priority="urgent").priority == "urgent"
    with pytest.raises(ValidationError):
        RFIIn(subject="s", question="<p>q</p>", priority="whenever")
    # The log offers exactly two levels; the retired 0068 draft had four.
    with pytest.raises(ValidationError):
        RFIIn(subject="s", question="<p>q</p>", priority="normal")


def test_priority_cannot_be_cleared(monkeypatch):
    _install(monkeypatch, _db(rfis=[_open_rfi()]))
    with pytest.raises(HTTPException) as exc:
        pm_field.update_rfi("p1", "r1", RFIUpdate.model_validate({"priority": None}), USER)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "key",
    [
        "pm:not-a-uuid",
        "ftp:11111111-1111-1111-1111-111111111111",
        "11111111-1111-1111-1111-111111111111",
        "pm:11111111-1111-1111-1111-11111111111",  # 35 chars
        "pm:ZZZZZZZZ-1111-1111-1111-111111111111",
    ],
)
def test_malformed_attachment_keys_are_rejected_by_the_schema(key):
    with pytest.raises(ValidationError):
        RFIIn(subject="s", question="<p>q</p>", attachment_keys=[key])


def test_attachment_keys_are_deduped():
    # (rfi_id, doc_key) is unique — a duplicate would be a 23505 on insert.
    body = RFIIn(subject="s", question="<p>q</p>", attachment_keys=[_DOC_A, _DOC_A, _DOC_B])
    assert body.attachment_keys == [_DOC_A, _DOC_B]


def test_attachment_keys_are_capped():
    with pytest.raises(ValidationError):
        RFIIn(
            subject="s",
            question="<p>q</p>",
            attachment_keys=[f"pm:{n:08d}-1111-1111-1111-111111111111" for n in range(51)],
        )


# ── Manpower ──────────────────────────────────────────────────────────────────


def _manpower_in(**over):
    base = dict(work_date=date(2026, 7, 10), classification="journeyman", workers=4)
    base.update(over)
    return ManpowerIn(**base)


def test_manpower_rejects_daily_log_from_other_project(monkeypatch):
    _install(monkeypatch, _db(daily_logs=[{"id": "d2", "project_id": "p2"}]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_manpower("p1", _manpower_in(daily_log_id="d2"), USER)
    assert exc.value.status_code == 400
    assert "different project" in exc.value.detail


def test_manpower_rejects_missing_daily_log(monkeypatch):
    _install(monkeypatch, _db(daily_logs=[]))
    with pytest.raises(HTTPException) as exc:
        pm_field.create_manpower("p1", _manpower_in(daily_log_id="ghost"), USER)
    assert exc.value.status_code == 404


def test_manpower_links_same_project_daily_log(monkeypatch):
    db = _db(daily_logs=[{"id": "d1", "project_id": "p1"}], manpower_entries=[])
    audits = _install(monkeypatch, db)
    created = pm_field.create_manpower("p1", _manpower_in(daily_log_id="d1"), USER)
    assert created["daily_log_id"] == "d1" and created["project_id"] == "p1"
    assert audits == ["manpower.create"]


def test_manpower_date_window_and_order(monkeypatch):
    _install(monkeypatch, _db(manpower_entries=[
        {"id": "e1", "project_id": "p1", "work_date": "2026-07-01"},
        {"id": "e2", "project_id": "p1", "work_date": "2026-07-03"},
        {"id": "e3", "project_id": "p1", "work_date": "2026-07-05"},
        {"id": "ex", "project_id": "p2", "work_date": "2026-07-03"},
    ]))
    assert [r["id"] for r in pm_field.list_manpower("p1", None, None, USER)] == [
        "e3", "e2", "e1"
    ]
    rows = pm_field.list_manpower("p1", date(2026, 7, 2), date(2026, 7, 4), USER)
    assert [r["id"] for r in rows] == ["e2"]


# ── RFIs: send / mark-sent (0071) ─────────────────────────────────────────────


def _send_db(**tables):
    """Like _gc_db, but contacts carry emails (required to be a valid recipient)."""
    return _db(
        general_contractors=[{"id": "gc1", "name": "Acme Builders"},
                             {"id": "gc2", "name": "Other GC"}],
        gc_contacts=[
            {"id": "c1", "gc_id": "gc1", "name": "Dana Ruiz", "email": "dana@acme.test"},
            {"id": "c2", "gc_id": "gc2", "name": "Sam Vale", "email": "sam@other.test"},
            {"id": "cn", "gc_id": "gc1", "name": "No Email", "email": None},
        ],
        profiles=[{"id": "u1", "full_name": "Tom Moore"}],
        **tables,
    )


def _rfi_for_send(**over):
    return {"id": "r1", "project_id": "p1", "rfi_number": 3, "subject": "Panel",
            "question": "<p>q</p>", "status": "open", "priority": "standard",
            "drawing_numbers": [], "applicable_references": [],
            "assigned_gc_id": "gc1", "assigned_contact_id": "c1",
            "asked_of": None, "sent_at": None, "due_at": None,
            "answer": None, "answered_by": None, "answered_at": None,
            "send_status": "not_sent", "sent_via": None,
            "last_sent_at": None, "last_sent_by": None, "created_by": "u1", **over}


def _stub_send_pipeline(monkeypatch, *, configured=True):
    """Stub the outbound side (PDF render, email, storage) so the send path runs
    without Gotenberg or Graph. Returns the captured send_rfi_email kwargs."""
    sent = []
    monkeypatch.setattr(pm_field.rfi_email, "graph_configured", lambda: configured)
    monkeypatch.setattr(pm_field.rfi_email, "signed_link", lambda p: f"https://dl/{p}")
    monkeypatch.setattr(
        pm_field.rfi_email, "send_rfi_email",
        lambda **k: (sent.append(k), {"id": "log1"})[1],
    )
    monkeypatch.setattr(pm_field.rfi_pdf, "render_pdf", lambda proj, rfi: b"%PDF-1.4 fake")
    monkeypatch.setattr(pm_field.storage, "build_object_path", lambda *a, **k: "p1/pm/rfi/x.pdf")
    monkeypatch.setattr(pm_field.storage, "upload_file", lambda *a, **k: None)
    return sent


def test_mark_sent_records_platform_and_stamps_sent_at(monkeypatch):
    db = _send_db(rfis=[_rfi_for_send()], rfi_sends=[])
    audits = _install(monkeypatch, db)
    out = pm_field.mark_rfi_sent("p1", "r1", RFIMarkSentIn(platform="procore"), USER)
    assert out["send_status"] == "sent_external"
    assert out["sent_via"] == "procore"
    assert out["last_sent_by"] == "u1"
    assert out["sent_at"] is not None  # filled because it was blank
    row = db.tables["rfi_sends"][0]
    assert row["method"] == "procore" and row["rfi_id"] == "r1" and row["recipients"] == []
    assert audits == ["rfi.mark_sent"]


def test_mark_sent_unknown_rfi_404s(monkeypatch):
    _install(monkeypatch, _send_db(rfis=[]))
    with pytest.raises(HTTPException) as exc:
        pm_field.mark_rfi_sent("p1", "nope", RFIMarkSentIn(platform="autodesk"), USER)
    assert exc.value.status_code == 404


def test_send_requires_assigned_company(monkeypatch):
    _install(monkeypatch, _send_db(
        rfis=[_rfi_for_send(assigned_gc_id=None, assigned_contact_id=None)]))
    _stub_send_pipeline(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.send_rfi("p1", "r1", RFISendIn(contact_ids=["c1"]), USER)
    assert exc.value.status_code == 400
    assert "company" in exc.value.detail.lower()


def test_send_rejects_contact_from_another_company(monkeypatch):
    _install(monkeypatch, _send_db(rfis=[_rfi_for_send()]))
    _stub_send_pipeline(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.send_rfi("p1", "r1", RFISendIn(contact_ids=["c2"]), USER)
    assert exc.value.status_code == 400
    assert "different company" in exc.value.detail


def test_send_rejects_contact_without_email(monkeypatch):
    _install(monkeypatch, _send_db(rfis=[_rfi_for_send()]))
    _stub_send_pipeline(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        pm_field.send_rfi("p1", "r1", RFISendIn(contact_ids=["cn"]), USER)
    assert exc.value.status_code == 400
    assert "email" in exc.value.detail.lower()


def test_send_requires_graph_configured(monkeypatch):
    _install(monkeypatch, _send_db(rfis=[_rfi_for_send()]))
    _stub_send_pipeline(monkeypatch, configured=False)
    with pytest.raises(HTTPException) as exc:
        pm_field.send_rfi("p1", "r1", RFISendIn(contact_ids=["c1"]), USER)
    assert exc.value.status_code == 503


def test_send_app_records_pdf_recipients_and_marks_sent(monkeypatch):
    db = _send_db(rfis=[_rfi_for_send()], rfi_sends=[], rfi_attachments=[], pm_documents=[])
    _stub_hub(monkeypatch)
    sent = _stub_send_pipeline(monkeypatch)
    audits = _install(monkeypatch, db)

    out = pm_field.send_rfi(
        "p1", "r1", RFISendIn(contact_ids=["c1"], message="please review"), USER
    )

    # the email fired to the contact's address, with the note
    assert len(sent) == 1
    assert sent[0]["to"] == ["dana@acme.test"]
    assert sent[0]["message"] == "please review"

    # the sent PDF was archived in Documents under the 'rfi' category
    doc = db.tables["pm_documents"][0]
    assert doc["category"] == "rfi" and doc["mime_type"] == "application/pdf"

    # the send was logged with the denormalized recipient + a pointer to the PDF
    log = db.tables["rfi_sends"][0]
    assert log["method"] == "app" and log["pdf_doc_id"] == doc["id"]
    assert log["recipients"] == [
        {"contact_id": "c1", "name": "Dana Ruiz", "email": "dana@acme.test"}
    ]

    # and the RFI now reads as sent-via-app
    assert out["send_status"] == "sent_app" and out["sent_via"] is None
    assert out["last_sent_by"] == "u1" and out["sent_at"] is not None
    assert audits == ["rfi.send"]
