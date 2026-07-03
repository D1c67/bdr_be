"""Estimator submission rounds (0050): draft sealing, post-send locking rules,
per-type staleness, per-user review acks, and the high-importance revision
alert plumbing."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import CHANGE_REVIEW_ROLES, Role
from app.routers import change_review
from app.routers.estimator import _before_receive_quotes
from app.routers.files import (
    ESTIMATOR_WRITE,
    UPDATE_CATEGORIES,
    VALID_CATEGORIES,
    _estimator_visible,
)
from app.services import estimator_rounds as er
from app.services import notifications as n
from app.services import revision_email


# ── minimal chainable Supabase fake ────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name, self.op = db, table, "select"
        self.payload = None
        self.filters: list[tuple] = []
        self.ors: list[str] = []

    def select(self, *cols, count=None):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, **kw):
        self.op, self.payload = "upsert", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, tuple(vals)))
        return self

    def or_(self, expr):
        self.ors.append(expr)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def single(self):
        return self

    def execute(self):
        self.db.calls.append(self)
        queue = self.db.queues.get((self.table_name, self.op)) or []
        resp = queue.pop(0) if queue else []
        if isinstance(resp, Exception):
            raise resp
        count = len(resp) if isinstance(resp, list) else None
        return SimpleNamespace(data=resp, count=count)


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


def _patch_db(monkeypatch, module, db):
    monkeypatch.setattr(module, "get_supabase", lambda: db)


# ── category sets / read gate ──────────────────────────────────────────────


def test_estimator_additional_is_writable_and_valid():
    assert "estimator_additional" in ESTIMATOR_WRITE
    assert "estimator_additional" in VALID_CATEGORIES
    # It is the estimator's own box — never a team-side update category.
    assert "estimator_additional" not in UPDATE_CATEGORIES


def test_estimator_sees_own_additional_files():
    assert _estimator_visible({"category": "estimator_additional"}) is True


def test_exclude_unsent_applies_the_or_filter():
    q = _Query(_FakeDB(), "project_files")
    er.exclude_unsent(q)
    assert er.SENT_OR_INTERNAL in q.ors


# ── create_submission_round ────────────────────────────────────────────────


def test_first_send_seals_round_1(monkeypatch):
    db = _FakeDB()
    db.queue("estimator_submissions", "select", [])  # no prior rounds
    db.queue("estimator_submissions", "insert", [{"id": "s1", "round": 1}])
    db.queue(
        "project_files",
        "update",
        [
            {"id": "f1", "category": "estimate", "filename": "e.xlsx"},
            {"id": "f2", "category": "boq", "filename": "b.xlsx"},
        ],
    )
    db.queue("estimator_submissions", "update", [{"id": "s1"}])  # summary snapshot
    _patch_db(monkeypatch, er, db)

    sub, sealed = er.create_submission_round("p1", "est1")

    assert sub["round"] == 1
    assert sub["summary"] == {"estimate": 1, "boq": 1}
    assert {f["id"] for f in sealed} == {"f1", "f2"}
    # The seal is conditional: only unsent estimator drafts on this project.
    seal = db.ops("project_files", "update")[0]
    assert ("eq", "estimator_deliverable", True) in seal.filters
    assert ("is", "submission_round", "null") in seal.filters
    assert seal.payload == {"submission_round": 1}


def test_next_send_gets_the_next_round(monkeypatch):
    db = _FakeDB()
    db.queue("estimator_submissions", "select", [{"round": 2, "submitted_at": "x"}])
    db.queue("estimator_submissions", "insert", [{"id": "s3", "round": 3}])
    db.queue("project_files", "update", [{"id": "f9", "category": "markup", "filename": "m.pdf"}])
    db.queue("estimator_submissions", "update", [{"id": "s3"}])
    _patch_db(monkeypatch, er, db)

    sub, _ = er.create_submission_round("p1", "est1")
    assert sub["round"] == 3


def test_duplicate_round_is_a_409(monkeypatch):
    db = _FakeDB()
    db.queue("estimator_submissions", "select", [])
    db.queue(
        "estimator_submissions",
        "insert",
        Exception('duplicate key value violates unique constraint "estimator_submissions_project_id_round_key"'),
    )
    _patch_db(monkeypatch, er, db)

    with pytest.raises(HTTPException) as exc:
        er.create_submission_round("p1", "est1")
    assert exc.value.status_code == 409


def test_send_with_no_drafts_rolls_back_the_round(monkeypatch):
    db = _FakeDB()
    db.queue("estimator_submissions", "select", [])
    db.queue("estimator_submissions", "insert", [{"id": "s1", "round": 1}])
    db.queue("project_files", "update", [])  # nothing to seal
    _patch_db(monkeypatch, er, db)

    with pytest.raises(HTTPException) as exc:
        er.create_submission_round("p1", "est1")
    assert exc.value.status_code == 409
    # The empty submission row must not survive announcing a round that never was.
    deletes = db.ops("estimator_submissions", "delete")
    assert len(deletes) == 1
    assert ("eq", "id", "s1") in deletes[0].filters


def test_seal_failure_unseals_before_dropping_the_round(monkeypatch):
    db = _FakeDB()
    db.queue("estimator_submissions", "select", [])
    db.queue("estimator_submissions", "insert", [{"id": "s1", "round": 1}])
    db.queue("project_files", "update", Exception("response lost"))
    # Compensation: un-seal update, then submission delete.
    db.queue("project_files", "update", [])
    _patch_db(monkeypatch, er, db)

    with pytest.raises(Exception, match="response lost"):
        er.create_submission_round("p1", "est1")
    updates = db.ops("project_files", "update")
    # Second project_files update is the compensating un-seal of this round —
    # covers the "UPDATE applied but response lost" case.
    assert updates[-1].payload == {"submission_round": None}
    assert ("eq", "submission_round", 1) in updates[-1].filters
    assert len(db.ops("estimator_submissions", "delete")) == 1


def test_round_cap_blocks_runaway_sends(monkeypatch):
    db = _FakeDB()
    db.queue("estimator_submissions", "select", [{"round": er.MAX_ROUNDS, "submitted_at": "x"}])
    _patch_db(monkeypatch, er, db)
    with pytest.raises(HTTPException) as exc:
        er.create_submission_round("p1", "est1")
    assert exc.value.status_code == 409
    assert db.ops("estimator_submissions", "insert") == []


# ── staleness rules ────────────────────────────────────────────────────────


def test_estimate_not_stale_before_extraction_ran(monkeypatch):
    db = _FakeDB()
    db.queue("general_material_estimates", "select", [])
    _patch_db(monkeypatch, er, db)
    assert er._estimate_stale(db, "p1") is False


def test_estimate_not_stale_mid_run(monkeypatch):
    db = _FakeDB()
    db.queue("general_material_estimates", "select", [{"estimate_file_id": None, "status": "running"}])
    _patch_db(monkeypatch, er, db)
    assert er._estimate_stale(db, "p1") is False


def test_estimate_not_stale_without_a_consumed_file(monkeypatch):
    # Extraction ran but never anchored a file (not_found / manual override):
    # nothing was consumed, so nothing can be stale.
    db = _FakeDB()
    db.queue("general_material_estimates", "select", [{"estimate_file_id": None, "status": "done"}])
    _patch_db(monkeypatch, er, db)
    assert er._estimate_stale(db, "p1") is False


def test_estimate_stale_when_newer_file_than_anchor(monkeypatch):
    db = _FakeDB()
    db.queue("general_material_estimates", "select", [{"estimate_file_id": "old", "status": "done"}])
    db.queue("project_files", "select", [{"id": "new", "filename": "e2.xlsx", "created_at": "2026-07-02T01:00:00+00:00"}])
    _patch_db(monkeypatch, er, db)
    assert er._estimate_stale(db, "p1") is True


def test_estimate_fresh_when_anchor_is_newest(monkeypatch):
    db = _FakeDB()
    db.queue("general_material_estimates", "select", [{"estimate_file_id": "same", "status": "done"}])
    db.queue("project_files", "select", [{"id": "same", "filename": "e.xlsx", "created_at": "x"}])
    _patch_db(monkeypatch, er, db)
    assert er._estimate_stale(db, "p1") is False


def test_newest_visible_excludes_unsent_drafts(monkeypatch):
    db = _FakeDB()
    db.queue("project_files", "select", [])
    _patch_db(monkeypatch, er, db)
    er._newest_visible(db, "p1", "estimate")
    q = db.ops("project_files", "select")[0]
    assert er.SENT_OR_INTERNAL in q.ors


def test_boq_not_stale_without_analysis(monkeypatch):
    db = _FakeDB()
    db.queue("boq_analyses", "select", [])
    _patch_db(monkeypatch, er, db)
    assert er._boq_stale(db, "p1") is False


def test_boq_stale_when_newer_than_analyzed(monkeypatch):
    db = _FakeDB()
    db.queue("boq_analyses", "select", [{"boq_file_id": "old"}])
    db.queue("project_files", "select", [{"id": "new", "filename": "b2.xlsx", "created_at": "x"}])
    _patch_db(monkeypatch, er, db)
    assert er._boq_stale(db, "p1") is True


def _queue_markup_case(db, *, markup_at, rfq_category, send_at):
    db.queue("project_files", "select", [{"id": "m1", "filename": "m.pdf", "created_at": markup_at}])
    db.queue("rfqs", "select", [{"id": "r1", "material_categories": {"name": rfq_category}}])
    if rfq_category.lower().find("trench") != -1:
        db.queue("rfq_sends", "select", [{"sent_at": send_at, "created_at": send_at}])


def test_markup_not_stale_without_trenching_rfq(monkeypatch):
    db = _FakeDB()
    _queue_markup_case(db, markup_at="2026-07-02T02:00:00+00:00", rfq_category="Lighting", send_at="2026-07-02T03:00:00+00:00")
    _patch_db(monkeypatch, er, db)
    assert er._markup_stale(db, "p1") is False


def test_markup_not_stale_without_any_trenching_send(monkeypatch):
    db = _FakeDB()
    db.queue("project_files", "select", [{"id": "m1", "filename": "m.pdf", "created_at": "2026-07-02T02:00:00+00:00"}])
    db.queue("rfqs", "select", [{"id": "r1", "material_categories": {"name": "Trenching"}}])
    db.queue("rfq_sends", "select", [])  # RFQ exists but nothing sent yet
    _patch_db(monkeypatch, er, db)
    assert er._markup_stale(db, "p1") is False


def test_markup_stale_when_newer_than_trenching_send(monkeypatch):
    db = _FakeDB()
    _queue_markup_case(db, markup_at="2026-07-02T04:00:00+00:00", rfq_category="Trenching", send_at="2026-07-02T03:00:00+00:00")
    _patch_db(monkeypatch, er, db)
    assert er._markup_stale(db, "p1") is True


def test_markup_fresh_when_sent_after_upload(monkeypatch):
    db = _FakeDB()
    _queue_markup_case(db, markup_at="2026-07-02T02:00:00+00:00", rfq_category="Trenching", send_at="2026-07-02T03:00:00+00:00")
    _patch_db(monkeypatch, er, db)
    assert er._markup_stale(db, "p1") is False


# ── resolve_boq_file_id ────────────────────────────────────────────────────


def test_explicit_boq_id_must_be_a_sent_file(monkeypatch):
    db = _FakeDB()
    db.queue("project_files", "select", [])  # id doesn't match once drafts excluded
    _patch_db(monkeypatch, er, db)
    with pytest.raises(HTTPException) as exc:
        er.resolve_boq_file_id("p1", "draft-id")
    assert exc.value.status_code == 400
    q = db.ops("project_files", "select")[0]
    assert er.SENT_OR_INTERNAL in q.ors  # the draft screen is applied to explicit picks


def test_explicit_boq_id_passes_when_sent(monkeypatch):
    db = _FakeDB()
    db.queue("project_files", "select", [{"id": "b1"}])
    _patch_db(monkeypatch, er, db)
    assert er.resolve_boq_file_id("p1", "b1") == "b1"


def test_boq_fallback_picks_newest_sent(monkeypatch):
    db = _FakeDB()
    db.queue("project_files", "select", [{"id": "b2"}])
    _patch_db(monkeypatch, er, db)
    assert er.resolve_boq_file_id("p1", None) == "b2"
    q = db.ops("project_files", "select")[0]
    assert er.SENT_OR_INTERNAL in q.ors


# ── needs_review ───────────────────────────────────────────────────────────


def _latest(round_no, when="2026-07-02T05:00:00+00:00"):
    return {"id": "s", "round": round_no, "submitted_at": when, "summary": None}


def test_no_review_needed_for_roles_outside_the_set(monkeypatch):
    monkeypatch.setattr(er, "latest_submission", lambda pid: _latest(2))
    assert Role.ACCOUNTANT not in CHANGE_REVIEW_ROLES
    assert er.needs_review("p1", "u1", Role.ACCOUNTANT) is False


def test_no_review_needed_without_a_revision_round(monkeypatch):
    monkeypatch.setattr(er, "latest_submission", lambda pid: None)
    assert er.needs_review("p1", "u1", Role.EXECUTIVE) is False
    monkeypatch.setattr(er, "latest_submission", lambda pid: _latest(1))
    assert er.needs_review("p1", "u1", Role.EXECUTIVE) is False


def test_review_needed_with_no_ack_row(monkeypatch):
    db = _FakeDB()
    db.queue("change_review_acks", "select", [])
    _patch_db(monkeypatch, er, db)
    monkeypatch.setattr(er, "latest_submission", lambda pid: _latest(2))
    assert er.needs_review("p1", "u1", Role.ESTIMATING_ADMIN) is True


def test_review_cleared_by_newer_ack_and_retripped_by_next_round(monkeypatch):
    db = _FakeDB()
    _patch_db(monkeypatch, er, db)
    monkeypatch.setattr(er, "latest_submission", lambda pid: _latest(2, "2026-07-02T05:00:00+00:00"))
    db.queue("change_review_acks", "select", [{"last_reviewed_at": "2026-07-02T06:00:00+00:00"}])
    assert er.needs_review("p1", "u1", Role.ESTIMATING_ENGINEER) is False
    # Round 3 lands after the ack → needs review again.
    monkeypatch.setattr(er, "latest_submission", lambda pid: _latest(3, "2026-07-02T07:00:00+00:00"))
    db.queue("change_review_acks", "select", [{"last_reviewed_at": "2026-07-02T06:00:00+00:00"}])
    assert er.needs_review("p1", "u1", Role.ESTIMATING_ENGINEER) is True


# ── mark_changes_reviewed endpoint ─────────────────────────────────────────


def _user(role=Role.ESTIMATING_ADMIN):
    return CurrentUser(
        id="u1", email="u@g3.com", role=role, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


def _ack_env(monkeypatch, db, latest, needs_after=False):
    _patch_db(monkeypatch, change_review, db)
    monkeypatch.setattr(change_review.estimator_rounds, "latest_submission", lambda pid: latest)
    monkeypatch.setattr(
        change_review.estimator_rounds, "needs_review", lambda pid, uid, role: needs_after
    )
    monkeypatch.setattr(change_review, "dismiss_notifications", lambda **kw: None)
    monkeypatch.setattr(change_review, "audit", lambda *a, **k: None)


def test_mark_reviewed_stores_the_rounds_timestamp(monkeypatch):
    db = _FakeDB()
    db.queue("change_review_acks", "select", [])
    db.queue("change_review_acks", "upsert", [{}])
    _ack_env(monkeypatch, db, _latest(2))

    out = asyncio.run(change_review.mark_changes_reviewed("p1", None, _user()))
    assert out == {"needs_review": False}
    up = db.ops("change_review_acks", "upsert")[0]
    # The mark is the round's submitted_at (server data), never client "now".
    assert up.payload["last_reviewed_at"] == _latest(2)["submitted_at"]
    assert up.payload["user_id"] == "u1"


def test_mark_reviewed_never_moves_backwards(monkeypatch):
    newer = "2026-07-02T09:00:00+00:00"
    db = _FakeDB()
    db.queue("change_review_acks", "select", [{"last_reviewed_at": newer}])
    db.queue("change_review_acks", "upsert", [{}])
    _ack_env(monkeypatch, db, _latest(2))

    asyncio.run(change_review.mark_changes_reviewed("p1", None, _user()))
    up = db.ops("change_review_acks", "upsert")[0]
    assert up.payload["last_reviewed_at"] == newer


def test_mark_reviewed_noop_without_revision_round(monkeypatch):
    db = _FakeDB()
    _ack_env(monkeypatch, db, _latest(1))
    out = asyncio.run(change_review.mark_changes_reviewed("p1", None, _user()))
    assert out == {"needs_review": False}
    assert db.ops("change_review_acks", "upsert") == []


def test_mark_reviewed_noop_for_roles_outside_the_set(monkeypatch):
    db = _FakeDB()
    _ack_env(monkeypatch, db, _latest(2))
    out = asyncio.run(
        change_review.mark_changes_reviewed("p1", None, _user(Role.ACCOUNTANT))
    )
    assert out == {"needs_review": False}
    assert db.ops("change_review_acks", "upsert") == []


def test_mark_reviewed_acks_only_the_round_the_user_saw(monkeypatch):
    # Banner showed round 2; round 3 sealed before the click. Only round 2's
    # submitted_at is acked, so round 3 keeps the banner up.
    seen = {"round": 2, "submitted_at": "2026-07-02T05:00:00+00:00"}
    db = _FakeDB()
    db.queue("estimator_submissions", "select", [seen])  # lookup of the echoed round
    db.queue("change_review_acks", "select", [])
    db.queue("change_review_acks", "upsert", [{}])
    _ack_env(monkeypatch, db, _latest(3, "2026-07-02T07:00:00+00:00"), needs_after=True)

    out = asyncio.run(
        change_review.mark_changes_reviewed(
            "p1", change_review.ReviewedIn(round=2), _user()
        )
    )
    assert out == {"needs_review": True}
    up = db.ops("change_review_acks", "upsert")[0]
    assert up.payload["last_reviewed_at"] == seen["submitted_at"]


# ── extraction auto-rerun window ───────────────────────────────────────────


def test_before_receive_quotes_window():
    assert _before_receive_quotes("estimate_received") is True
    assert _before_receive_quotes("rfqs") is True
    assert _before_receive_quotes("receive_quotes") is False
    assert _before_receive_quotes("verify") is False
    assert _before_receive_quotes("not_a_stage") is False


# ── notifications: mirror_email flag ───────────────────────────────────────


class _Recorder:
    def __init__(self, profiles):
        self._profiles = profiles
        self.inserted = []

    def table(self, name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def insert(self, payload):
        self.inserted.append(payload)
        return self

    def execute(self):
        return SimpleNamespace(data=self._profiles)


def test_notify_role_without_mirror_email(monkeypatch):
    rec = _Recorder([{"id": "pe1"}])
    queued = []
    monkeypatch.setattr(n, "get_supabase", lambda: rec)
    monkeypatch.setattr(n.notification_email, "queue", lambda rows: queued.append(rows))
    n.notify_role(Role.ESTIMATING_ADMIN, "p1", "estimate_revised", "msg", mirror_email=False)
    assert len(rec.inserted) == 1  # bell rows still created
    assert queued == []  # but no generic mirror email


def test_notify_role_default_still_mirrors(monkeypatch):
    rec = _Recorder([{"id": "pe1"}])
    queued = []
    monkeypatch.setattr(n, "get_supabase", lambda: rec)
    monkeypatch.setattr(n.notification_email, "queue", lambda rows: queued.append(rows))
    n.notify_role(Role.ESTIMATING_ADMIN, "p1", "estimate_submitted", "msg")
    assert len(queued) == 1


# ── graph importance ───────────────────────────────────────────────────────


def _graph_fakes(monkeypatch, captured):
    from app.services import graph_email as ge

    db = _FakeDB()
    db.queue("email_log", "insert", [{"id": "log1"}])
    db.queue("email_log", "update", [{}])
    monkeypatch.setattr(ge, "get_supabase", lambda: db)
    monkeypatch.setattr(ge, "_acquire_token", lambda: "tok")

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return SimpleNamespace(raise_for_status=lambda: None, headers={})

    monkeypatch.setattr(ge.httpx, "post", fake_post)
    return ge


def test_send_mail_importance_high(monkeypatch):
    captured: dict = {}
    ge = _graph_fakes(monkeypatch, captured)
    ge.send_mail(to=["a@g3.com"], subject="s", body_html="<p>x</p>", importance="high")
    assert captured["message"]["importance"] == "high"


def test_send_mail_importance_defaults_off(monkeypatch):
    captured: dict = {}
    ge = _graph_fakes(monkeypatch, captured)
    ge.send_mail(to=["a@g3.com"], subject="s", body_html="<p>x</p>")
    assert "importance" not in captured["message"]


# ── revision alert email ───────────────────────────────────────────────────


def test_revision_alert_renders_marker_files_and_round():
    html_out = revision_email.render_revision_alert(
        recipient_name="Pat Smith",
        proj={"name": "Van Ness <Tower>", "number": "26-014"},
        round_no=3,
        files=[
            {"category": "estimate", "filename": "estimate_rev3.xlsx"},
            {"category": "estimator_additional", "filename": "geotech.pdf"},
        ],
        cta_url="https://bdr.example/projects/p1",
    )
    assert "HIGH IMPORTANCE" in html_out
    assert "estimate_rev3.xlsx" in html_out and "geotech.pdf" in html_out
    assert "round 3" in html_out
    assert "Revised estimate" in html_out and "Additional files" in html_out
    # No unsent-type sections appear.
    assert "Revised BOQ" not in html_out
    # Escaping: the raw project name must not inject markup.
    assert "Van Ness <Tower>" not in html_out


def test_queue_revision_alert_noops_when_emails_disabled(monkeypatch):
    # conftest forces notification_emails_enabled off — no thread may spawn.
    monkeypatch.setattr(
        revision_email.threading, "Thread",
        lambda *a, **k: pytest.fail("thread spawned while emails disabled"),
    )
    revision_email.queue_revision_alert("p1", 2, [])
