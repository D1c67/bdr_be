"""Revised proposals after submission (migration 0178, docs/PROPOSAL_REVISIONS.md).

Covers the start guards, the locks a revision opens (scope lines, GC amounts,
quote selection) and closes again, the re-verify bounce on a cost edit, the
revised document generation and send (the live proposal_sends row swap, the
'revision' event, the prior snapshot, the custom subject and the crash
reclaim against it), mark revision submitted, auto-complete, cancel, the
outcome refusal, a GC added during the revision (normal initial proposal),
and a re-send after a revision.

The Supabase client is an in-memory fake (the test_late_gc_pricing_approval
pattern) that also runs apply_proposal_revision_send in Python, mirroring the
SQL function. Documents are rendered for real from the template asset, so
the isolation checks run against real bytes; only the PDF conversion, the
storage bucket and Graph are faked.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError

from app.core.roles import Role
from app.models.schemas import ProposalLinesIn
from app.routers import pricing as pricing_router
from app.routers import proposals as proposals_router
from app.routers import rfqs as rfqs_router
from app.services import auto_send, email_branding, graph_email, office_preview, outcome, storage
from app.services import proposal_revisions as pr
from app.services import proposal_send as psend
from app.services import workflow
from app.services.proposal_docx import build_filename, extract_document_text
from app.services.proposal_send import ProposalSendError

PID = "p1"
NOW = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
_ids = itertools.count(1)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── Fake Supabase ─────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._op = None
        self._payload = None
        self._filters: list = []
        self._order: list = []
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

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def is_(self, col, val):
        self._filters.append(("eq", col, None if val == "null" else val))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def or_(self, expr):
        arms = []
        for part in expr.split(","):
            col, op, val = part.split(".", 2)
            arms.append((col, op, None if val == "null" else val))
        self._filters.append(("or", arms, None))
        return self

    def order(self, col, desc=False):
        self._order.append((col, desc))
        return self

    def limit(self, *a, **k):
        return self

    def single(self):
        self._single = True
        return self

    def _match(self, row):
        for op, col, val in self._filters:
            if op == "or":
                if not any(
                    (row.get(c) == v) if o in ("is", "eq") else (row.get(c) != v)
                    for c, o, v in col
                ):
                    return False
                continue
            got = row.get(col)
            if op == "eq" and got != val:
                return False
            if op == "neq" and got == val:
                return False
            if op == "in" and got not in val:
                return False
            if op == "gte" and not (got is not None and str(got) >= str(val)):
                return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        now = _iso(self.db.now)
        if self._op == "select":
            hits = [dict(r) for r in rows if self._match(r)]
            for col, desc in reversed(self._order):
                hits.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._single:
                return SimpleNamespace(data=hits[0] if hits else None)
            return SimpleNamespace(data=hits)
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = {"id": f"{self.table}-{next(_ids)}", "created_at": now,
                       "updated_at": now, **_DEFAULTS.get(self.table, {}), **p}
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "upsert":
            keys = (self._conflict or "id").split(",")
            p = self._payload
            hit = next((r for r in rows if all(r.get(k) == p.get(k) for k in keys)), None)
            if hit:
                hit.update(p)
                return SimpleNamespace(data=[dict(hit)])
            rows.append({"id": f"{self.table}-{next(_ids)}", **p})
            return SimpleNamespace(data=[dict(rows[-1])])
        if self._op == "update":
            out = []
            for r in rows:
                if self._match(r):
                    r.update(self._payload)
                    r["updated_at"] = now
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            gone = [r for r in rows if self._match(r)]
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            return SimpleNamespace(data=gone)
        return SimpleNamespace(data=[])


_DEFAULTS = {
    "proposal_revisions": {"status": "open"},
    "proposal_revision_sends": {"status": "pending", "included": True},
}


class FakeDB:
    def __init__(self, tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.now = NOW
        self.rpc_calls: list = []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        fn = {
            "apply_proposal_revision_send": self._apply,
            "claim_proposal_revision_send": self._claim,
            "finish_proposal_revision": self._finish,
        }[name]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=fn(params)))

    # Python mirrors of the 0178 SQL functions. A refusal raises the same
    # 'proposal_revision:<code>' APIError PostgREST would hand back.

    @staticmethod
    def _refuse(code):
        raise APIError({"message": f"proposal_revision:{code}", "code": "P0001",
                        "hint": None, "details": None})

    def _send_row(self, rid):
        r = next((x for x in self.tables["proposal_revision_sends"] if x["id"] == rid), None)
        if r is None:
            self._refuse("not_found")
        return r

    def _rev_of(self, r):
        rev = next(x for x in self.tables["proposal_revisions"] if x["id"] == r["revision_id"])
        if rev["status"] != "open":
            self._refuse("not_open")
        return rev

    def _claim(self, p):
        r = self._send_row(p["p_revision_send_id"])
        self._rev_of(r)
        if not r["included"]:
            self._refuse("excluded")
        if r["status"] not in ("generated", "failed"):
            return None
        if (r.get("error") or "").startswith("email delivered but not recorded"):
            self._refuse("delivered")
        ps_ids = {x["id"] for x in self.tables["proposal_sends"]
                  if x["project_id"] == r["project_id"] and x["gc_id"] == r["gc_id"]}
        if any(e.get("proposal_send_id") in ps_ids and e.get("status") == "sending"
               for e in self.tables["proposal_send_events"]):
            self._refuse("resend_in_flight")
        r.update(status="sending", gc_email=p["p_gc_email"], subject=p["p_subject"],
                 body=p["p_body"], cc_recipients=p["p_cc"], claimed_at=_iso(self.now),
                 claimed_by=p["p_user_id"])
        return dict(r)

    def _apply(self, p):
        r = self._send_row(p["p_revision_send_id"])
        rev = self._rev_of(r)
        via = p["p_via"]
        if via == "email" and r["status"] != "sending":
            return None
        if via == "external" and r["status"] not in ("generated", "failed"):
            return None
        if via == "external" and (r.get("error") or "").startswith("email delivered"):
            self._refuse("delivered")
        if not r["included"]:
            self._refuse("excluded")
        if not r.get("file_id"):
            self._refuse("no_document")
        ps = next((x for x in self.tables["proposal_sends"]
                   if x["project_id"] == r["project_id"] and x["gc_id"] == r["gc_id"]), None)
        if ps is None or ps["status"] != "sent":
            self._refuse("not_sent")
        now = p["p_sent_at"] or _iso(self.now)
        event = {
            "id": f"proposal_send_events-{next(_ids)}", "proposal_send_id": ps["id"],
            "project_id": r["project_id"], "gc_id": r["gc_id"], "kind": "revision",
            "via": via, "file_id": r["file_id"],
            "recipients": p["p_recipients"] if via == "email" else None,
            "cc_recipients": p["p_cc"] if via == "email" else None, "status": "sent",
            "email_log_id": p["p_email_log_id"], "sent_at": now, "sent_by": p["p_user_id"],
            "proposal_revision_id": rev["id"], "created_at": now,
        }
        self.tables["proposal_send_events"].append(event)
        subject = p["p_subject"] or r.get("subject")
        prior = {f"prior_{k}": ps.get(k) for k in (
            "file_id", "draft_id", "lines_hash", "sent_via", "email_log_id", "gc_email",
            "subject", "material_amount", "gear_amount", "underground_amount",
            "low_voltage_amount", "labor_amount", "revision_number", "origin",
            "auto_send_run_id")}
        prior["prior_sent_at"] = ps.get("revised_at") or ps.get("sent_at")
        prior["prior_sent_by"] = ps.get("revised_by") or ps.get("sent_by")
        r.update({
            "status": "sent", "error": None, "sent_via": via, "sent_at": now,
            "sent_by": p["p_user_id"], "email_log_id": p["p_email_log_id"],
            "subject": subject, "gc_email": r.get("gc_email") if via == "email" else None,
            "send_event_id": event["id"], "proposal_send_id": ps["id"], **prior,
        })
        # The submission record (sent_at, sent_by, sent_via, email_log_id,
        # gc_email) is never touched.
        ps.update({
            "file_id": r["file_id"], "draft_id": r["draft_id"], "lines_hash": r["lines_hash"],
            "gc_name": r["gc_name"], **{f"{k}_amount": r.get(f"{k}_amount") for k in
                                        ("material", "gear", "underground", "low_voltage", "labor")},
            "subject": subject, "revision_number": rev["revision_number"],
            "revised_at": now, "revised_by": p["p_user_id"],
            "origin": "send_out", "auto_send_run_id": None, "error": None,
        })
        return dict(r)

    def _finish(self, p):
        rev = next((x for x in self.tables["proposal_revisions"]
                    if x["id"] == p["p_revision_id"]), None)
        if rev is None:
            self._refuse("not_found")
        if rev["status"] != "open":
            self._refuse("not_open")
        rows = [x for x in self.tables["proposal_revision_sends"] if x["revision_id"] == rev["id"]]
        if any(x["status"] == "sending" for x in rows):
            self._refuse("sending")
        reached = any(
            x["status"] == "sent" or (x["status"] == "failed" and (
                (x.get("error") or "").startswith("outcome unknown")
                or (x.get("error") or "").startswith("email delivered but not recorded")))
            for x in rows
        ) or any(e.get("proposal_revision_id") == rev["id"] and e.get("kind") == "initial"
                 and e.get("status") == "sent" for e in self.tables["proposal_send_events"])
        mode = p["p_mode"]
        if mode == "cancel" and reached:
            self._refuse("went_out")
        if mode == "done" and not reached:
            self._refuse("nothing_sent")
        now = _iso(self.now)
        if mode == "cancel":
            rev.update(status="cancelled", cancelled_by=p["p_user_id"], cancelled_at=now)
        else:
            rev.update(status="completed", completion="auto" if mode == "auto" else "manual",
                       completed_by=p["p_user_id"], completed_at=now)
        return dict(rev)


# ── World ────────────────────────────────────────────────────────────────────

LINES = ["Furnish and install site lighting"]
PROJECT = {
    "id": PID, "name": "Sunset Plaza", "number": "26.9.7201", "address": "1 Main St",
    "labor_time": "day_work", "wage_type": "prevailing_wage", "current_stage": "submitted",
    "reverify_return_stage": None, "abandoned_at": None, "failed_to_bid_at": None,
    "internal_bid_at": "2026-10-20T21:00:00+00:00", "actual_bid_at": None,
}
AMOUNTS = {
    "material": Decimal("45000"), "gear": None, "underground": None, "low_voltage": None,
    "labor": Decimal("31000"), "total": Decimal("76000"),
}


def _gc_link(gc_id, name, overrides=None):
    row = {
        "id": f"link-{gc_id}", "project_id": PID, "gc_id": gc_id, "needs_by": None,
        "pricing_approval_status": None, "project_gc_contacts": [],
        "general_contractors": {
            "id": gc_id, "name": name,
            "gc_contacts": [{"id": f"c-{gc_id}", "name": "Pat", "email": f"pat@{gc_id}.com"}],
        },
    }
    for key in ("material", "gear", "underground", "low_voltage", "labor"):
        row[f"proposal_{key}_amount"] = (overrides or {}).get(key)
    return row


def _cat_rows(head):
    spec = {
        "intake": ("to_estimator", "complete"),
        "material_numbers": ("receive_quotes", "complete"),
        "labor_numbers": ("labor_numbers", "complete"),
        "select_vendors": ("select_vendors", "complete"),
        "markup": ("markup", "complete"),
        "send_out": (head, "active"),
    }
    return [
        {"project_id": PID, "category": c, "current_task": t, "status": s,
         "owner_role": None, "completed_at": "x" if s == "complete" else None}
        for c, (t, s) in spec.items()
    ]


def _sent_row(sid, gc_id, name, file_id):
    return {
        "id": sid, "project_id": PID, "gc_id": gc_id, "gc_name": name, "status": "sent",
        "draft_id": "d1", "file_id": file_id, "lines_hash": psend.lines_hash(LINES),
        "material_amount": "45000", "gear_amount": None, "underground_amount": None,
        "low_voltage_amount": None, "labor_amount": "31000", "sent_at": "2026-10-20T20:00:00+00:00",
        "sent_by": "u-admin", "sent_via": "email", "email_log_id": "log-orig",
        "gc_email": f"pat@{gc_id}.com", "origin": "send_out", "revision_number": 0,
        "subject": None, "updated_at": "2026-10-20T20:00:00+00:00",
    }


def _world(head="submitted", **extra):
    tables = {
        "projects": [dict(PROJECT, current_stage=head)],
        "project_category_state": _cat_rows(head),
        "project_gcs": [_gc_link("g1", "Alpha Builders"), _gc_link("g2", "Bravo GC")],
        "proposal_sends": [
            _sent_row("ps1", "g1", "Alpha Builders", "f-orig1"),
            _sent_row("ps2", "g2", "Bravo GC", "f-orig2"),
        ],
        "project_files": [
            {"id": "f-orig1", "project_id": PID, "gc_id": "g1", "category": "proposal",
             "filename": "Proposal 7201 - Alpha Builders.docx", "storage_path": "orig1"},
            {"id": "f-orig2", "project_id": PID, "gc_id": "g2", "category": "proposal",
             "filename": "Proposal 7201 - Bravo GC.docx", "storage_path": "orig2"},
        ],
        "proposal_drafts": [
            {"id": "d1", "project_id": PID, "status": "done", "model": "manual",
             "lines_json": LINES, "approved_at": "2026-10-19T00:00:00+00:00",
             "created_at": "2026-10-19T00:00:00+00:00", "boq_file_id": None, "result_json": {}},
        ],
        "verifications": [{"project_id": PID, "committed_at": "2026-10-19T00:00:00+00:00"}],
        "proposal_send_events": [],
        "proposal_revisions": [],
        "proposal_revision_sends": [],
        "profiles": [
            {"id": "u-admin", "full_name": "Dana Admin"},
            {"id": "u-exec", "full_name": "Sam Exec"},
        ],
        "email_log": [],
        "bid_outcomes": [],
    }
    tables.update(extra)
    return FakeDB(tables)


class Env:
    def __init__(self, monkeypatch, db):
        self.db = db
        self.audits: list = []
        self.mails: list = []
        self.blobs: dict[str, bytes] = {}
        mp = monkeypatch
        for mod in (psend, pr, workflow, pricing_router, proposals_router, rfqs_router, outcome):
            mp.setattr(mod, "get_supabase", lambda: db)
        record = lambda actor, action, entity, entity_id, payload=None: self.audits.append(  # noqa: E731
            (action, payload)
        )
        for mod in (psend, pr, proposals_router, rfqs_router):
            mp.setattr(mod, "audit", record)
        for mod in (psend, pr, rfqs_router):
            mp.setattr(mod, "dismiss_notifications", lambda **kw: None)
        for mod in (psend, pr):
            mp.setattr(mod, "notify_role", lambda *a, **k: None)
        mp.setattr(workflow.notifications, "notify_role", lambda *a, **k: None)
        mp.setattr(workflow.notifications, "dismiss_notifications", lambda **kw: None)
        mp.setattr(pr.time, "sleep", lambda s: None)
        from app.core import revision_context

        mp.setattr(revision_context, "_open_revision",
                   lambda project_id: pr.open_revision(project_id, db))
        mp.setattr(psend.time, "sleep", lambda s: None)
        # Pricing: one committed snapshot whose figures are AMOUNTS.
        mp.setattr(pricing_router, "_verify_originals", lambda pid: {})
        mp.setattr(pricing_router, "_materials_rows", lambda pid: [])
        mp.setattr(pricing_router, "section_summary",
                   lambda rows: {"gear": {"includes_generator": False}})
        mp.setattr(pricing_router, "_sections_wire", lambda sections: {})
        mp.setattr(psend, "proposal_amounts", lambda o, v: dict(AMOUNTS))
        mp.setattr(psend, "_pricing_basis_for", lambda pid: {
            "material_cost": Decimal("40000"), "gear_cost": None, "underground_cost": None,
            "low_voltage_cost": None, "labor_cost": Decimal("30000"),
        })
        mp.setattr(psend, "auto_sent_gc_ids", lambda pid: set())
        # Storage, PDF, Graph.
        mp.setattr(storage, "build_object_path", lambda pid, cat, fn: f"{pid}/{cat}/{fn}")
        mp.setattr(storage, "upload_file", lambda path, data, mime, **k: self.blobs.__setitem__(path, data))
        mp.setattr(storage, "download_file", lambda path: self.blobs[path])
        mp.setattr(storage, "delete_file", lambda path: self.blobs.pop(path, None))
        mp.setattr(office_preview, "convert_for_send", lambda data, fn: b"%PDF-" + data[:10])
        mp.setattr(office_preview, "extract_pdf_text", lambda pdf: "")
        mp.setattr(psend, "validate_pdf_isolation", lambda *a, **k: None)
        mp.setattr(email_branding, "render_proposal_email", lambda body: body)
        mp.setattr(email_branding, "logo_bytes", lambda: b"")

        def send_mail(**kw):
            log = {"id": f"log-{next(_ids)}", "project_id": PID, "status": "sent",
                   "to_addrs": psend.join_recipients(kw["to"]), "subject": kw["subject"],
                   "created_at": _iso(self.db.now)}
            self.db.tables["email_log"].append(log)
            self.mails.append(kw)
            return log

        mp.setattr(graph_email, "send_mail", send_mail)

    def rows(self, table, **match):
        return [r for r in self.db.tables[table] if all(r.get(k) == v for k, v in match.items())]

    def one(self, table, **match):
        hits = self.rows(table, **match)
        assert len(hits) == 1, hits
        return hits[0]


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch, _world())


def _start(env, reason="GC moved the bid date"):
    return pr.start_revision(PID, "u-admin", reason)


def _rev_row(env, gc_id):
    return env.one("proposal_revision_sends", gc_id=gc_id)


# ── start guards ──────────────────────────────────────────────────────────────


def test_start_opens_rev_1_with_every_sent_gc_checked(env):
    rev = _start(env)
    assert rev["revision_number"] == 1 and rev["status"] == "open"
    rows = env.rows("proposal_revision_sends", revision_id=rev["id"])
    assert sorted(r["gc_id"] for r in rows) == ["g1", "g2"]
    assert all(r["included"] and r["status"] == "pending" for r in rows)
    action, payload = env.audits[-1]
    assert action == "proposal.revision_start" and payload["revision_number"] == 1
    assert payload["gcs"] == ["Alpha Builders", "Bravo GC"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda db: db.tables["project_category_state"][-1].update(current_task="send_out"),
         "not been submitted"),
        (lambda db: db.tables["project_category_state"][-1].update(current_task="bid_outcome"),
         "outcome is recorded"),
        (lambda db: db.tables["bid_outcomes"].append({"project_id": PID, "result": "lost"}),
         "outcome is recorded"),
        (lambda db: db.tables["projects"][0].update(abandoned_at="x"), "abandoned"),
        (lambda db: db.tables["projects"][0].update(failed_to_bid_at="x", abandoned_at="x"),
         "failed to bid"),
        (lambda db: db.tables["projects"][0].update(current_stage="declined"), "declined"),
        (lambda db: db.tables["proposal_send_events"].append(
            {"id": "e1", "project_id": PID, "status": "sending"}), "being sent"),
        (lambda db: db.tables["proposal_sends"][0].update(status="sending"), "being sent"),
        (lambda db: [r.update(status="generated") for r in db.tables["proposal_sends"]],
         "nothing to revise"),
    ],
)
def test_start_refusals(env, mutate, message):
    mutate(env.db)
    with pytest.raises(ProposalSendError, match=message):
        _start(env)
    assert env.db.tables["proposal_revisions"] == []


def test_start_refuses_a_second_open_revision(env):
    _start(env)
    with pytest.raises(ProposalSendError, match="Revision 1 is already open"):
        _start(env)


def test_start_allowed_while_bounced_to_verify_from_submitted(env):
    env.db.tables["project_category_state"][-1]["current_task"] = "verify"
    env.db.tables["projects"][0]["reverify_return_stage"] = "submitted"
    assert _start(env)["revision_number"] == 1


def test_start_requires_a_reason(env):
    with pytest.raises(ProposalSendError, match="what changed"):
        _start(env, reason="   ")


def test_numbers_continue_after_a_finished_revision(env):
    rev = _start(env)
    pr.cancel_revision(PID, rev["id"], "u-admin")
    assert _start(env)["revision_number"] == 2


# ── locks open only while a revision is open ─────────────────────────────────


def test_lines_lock_opens_and_closes(env):
    sb = env.db
    with pytest.raises(HTTPException) as exc:
        proposals_router._assert_lines_unlocked(sb, PID)
    assert exc.value.status_code == 409
    rev = _start(env)
    proposals_router._assert_lines_unlocked(sb, PID)  # open: no raise
    pr.cancel_revision(PID, rev["id"], "u-admin")
    with pytest.raises(HTTPException):
        proposals_router._assert_lines_unlocked(sb, PID)


def test_saving_lines_a_sent_document_was_built_from_forks_the_draft(env):
    _start(env)
    user = SimpleNamespace(id="u-admin", role=Role.ESTIMATING_ADMIN)
    out = proposals_router.save_lines(PID, "d1", ProposalLinesIn(lines=["New scope"]), user)
    assert out["id"] != "d1" and out["lines"] == ["New scope"] and not out.get("approved_at")
    # The draft the GCs' documents were rendered from is untouched.
    assert env.one("proposal_drafts", id="d1")["lines_json"] == LINES
    # The fork itself is edited in place (nothing sent was built from it).
    again = proposals_router.save_lines(PID, out["id"], ProposalLinesIn(lines=["Newer"]), user)
    assert again["id"] == out["id"]


def test_gc_amounts_open_for_revision_gcs_only(env):
    with pytest.raises(ProposalSendError, match="locked"):
        psend.set_gc_amounts(PID, "g1", {"material": Decimal("50000")}, "u-admin",
                             role=Role.ESTIMATING_ADMIN)
    rev = _start(env)
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1"])
    # A writer edits directly (no 0118 approval) for a GC in the revision.
    psend.set_gc_amounts(PID, "g1", {"material": Decimal("50000")}, "u-admin",
                         role=Role.ESTIMATING_ADMIN)
    assert env.one("project_gcs", gc_id="g1")["proposal_material_amount"] == "50000"
    assert env.one("project_gcs", gc_id="g1")["pricing_approval_status"] is None
    # The unchecked GC keeps its proposal, so its amounts stay locked.
    with pytest.raises(ProposalSendError, match="locked"):
        psend.set_gc_amounts(PID, "g2", {"material": Decimal("1")}, "u-admin",
                             role=Role.ESTIMATING_ADMIN)
    pr.cancel_revision(PID, rev["id"], "u-admin")
    with pytest.raises(ProposalSendError, match="locked"):
        psend.set_gc_amounts(PID, "g1", {"material": Decimal("1")}, "u-admin",
                             role=Role.ESTIMATING_ADMIN)


def test_added_gc_is_priced_directly_during_a_revision(env):
    env.db.tables["project_gcs"].append(_gc_link("g3", "Charlie Co"))
    with pytest.raises(ProposalSendError, match="executive approval"):
        psend.set_gc_amounts(PID, "g3", {"material": Decimal("47000")}, "u-admin",
                             role=Role.ESTIMATING_ADMIN)
    _start(env)
    psend.set_gc_amounts(PID, "g3", {"material": Decimal("47000")}, "u-admin",
                         role=Role.ESTIMATING_ADMIN)
    assert env.one("project_gcs", gc_id="g3")["pricing_approval_status"] is None


def _quote_world(env):
    env.db.tables["rfqs"] = [{"id": "r1", "project_id": PID, "status": "quotes_in",
                              "material_category_id": "mc1",
                              "material_categories": {"name": "Lighting", "is_general": False}}]
    env.db.tables["quotes"] = [
        {"id": "q1", "rfq_id": "r1", "is_approved": True, "is_selected": True, "amount": "10"},
        {"id": "q2", "rfq_id": "r1", "is_approved": True, "is_selected": False, "amount": "9"},
    ]


def test_quote_selection_locked_after_submission_but_open_during_a_revision(env):
    _quote_world(env)
    user = SimpleNamespace(id="u-admin", role=Role.ESTIMATING_ENGINEER_MATERIALS)
    assert workflow.in_post_submission_window(PID, env.db) is True
    with pytest.raises(HTTPException) as exc:
        rfqs_router.select_quote(PID, "r1", "q2", user)
    assert exc.value.status_code == 409
    rev = _start(env)
    assert workflow.in_post_submission_window(PID, env.db) is False
    events_before = len(env.db.tables.get("stage_events", []))
    rfqs_router.select_quote(PID, "r1", "q2", user)
    assert env.one("quotes", id="q2")["is_selected"] is True
    # 0179: no bounce. The bid stays at Submitted with no stage event; the
    # revision records the cost change instead.
    assert env.one("project_category_state", category="send_out")["current_task"] == "submitted"
    assert env.one("projects", id=PID)["reverify_return_stage"] is None
    assert len(env.db.tables.get("stage_events", [])) == events_before
    assert env.one("verifications", project_id=PID)["committed_at"]  # snapshot kept
    assert pr.costs_pending(env.one("proposal_revisions", id=rev["id"]))
    pr.cancel_revision(PID, rev["id"], "u-admin")
    assert workflow.in_post_submission_window(PID, env.db) is True


# ── cost changes: approved inside the revision (0179) ────────────────────────


def _commit_patches(monkeypatch):
    """What the Verify commit (pricing.commit_verify) reaches, faked."""
    monkeypatch.setattr(pricing_router, "section_summary", lambda rows: {
        "materials": {"present": True}, "gear": {"present": False, "includes_generator": False},
        "underground": {"present": False}, "low_voltage": {"present": False},
    })
    monkeypatch.setattr(pricing_router, "audit", lambda *a, **k: None)
    monkeypatch.setattr(pricing_router, "notify_roles", lambda *a, **k: None)
    monkeypatch.setattr(pricing_router, "notify_role", lambda *a, **k: None)


EXEC = SimpleNamespace(id="u-exec", role=Role.EXECUTIVE)


def test_cost_edit_blocks_generation_until_executive_approval(env, monkeypatch):
    _quote_world(env)
    _commit_patches(monkeypatch)
    rev = _start(env)
    rfqs_router.select_quote(PID, "r1", "q2", SimpleNamespace(id="u-admin", role=None))
    for blocked in (
        lambda: pr.generate_revision_documents(PID, rev["id"], "u-admin"),
        lambda: psend.set_gc_amounts(PID, "g1", {"material": Decimal("1")}, "u-admin",
                                     role=Role.ESTIMATING_ADMIN),
    ):
        with pytest.raises(ProposalSendError, match="Executive must approve"):
            blocked()
    pr.approve_costs(PID, rev["id"], EXEC)
    after = env.one("verifications", project_id=PID)
    assert after["committed_at"] and after["verified_by"] == "u-exec"
    approved = env.one("proposal_revisions", id=rev["id"])
    assert not pr.costs_pending(approved) and approved["costs_approved_by"] == "u-exec"
    assert env.one("project_category_state", category="send_out")["current_task"] == "submitted"
    assert len(pr.generate_revision_documents(PID, rev["id"], "u-admin")) == 2
    # Nothing left to approve.
    with pytest.raises(ProposalSendError, match="Nothing is waiting"):
        pr.approve_costs(PID, rev["id"], EXEC)


def test_approval_keeps_dispatch_blocked_until_commit_finishes(env, monkeypatch):
    _quote_world(env)
    _commit_patches(monkeypatch)
    rev = _start(env)
    pr.record_cost_change(PID, "u-admin", "Labor numbers edited", env.db)
    commit = pricing_router.commit_verify
    regenerated = []
    monkeypatch.setattr(pricing_router, "_regenerate_stale_proposals_after_commit",
                        lambda *args: regenerated.append(
                            pr.costs_pending(env.one("proposal_revisions", id=rev["id"]))
                        ))

    def paused_commit(*args):
        # A second request arriving before the new snapshot is persisted must
        # still be refused, even though the Executive clicked Approve.
        with pytest.raises(ProposalSendError, match="Executive must approve"):
            pr._require_dispatch_head(env.db, PID)
        return commit(*args)

    monkeypatch.setattr(pricing_router, "commit_verify", paused_commit)
    pr.approve_costs(PID, rev["id"], EXEC)
    assert regenerated == [True, False]
    assert pr._require_dispatch_head(env.db, PID) == "submitted"


def test_failed_approval_keeps_dispatch_blocked(env, monkeypatch):
    rev = _start(env)
    pr.record_cost_change(PID, "u-admin", "Labor numbers edited", env.db)

    def failed_commit(*args):
        raise RuntimeError("Snapshot write failed")

    monkeypatch.setattr(pricing_router, "commit_verify", failed_commit)
    with pytest.raises(RuntimeError, match="Snapshot write failed"):
        pr.approve_costs(PID, rev["id"], EXEC)
    assert pr.costs_pending(env.one("proposal_revisions", id=rev["id"]))
    assert not env.one("proposal_revisions", id=rev["id"]).get("costs_approved_at")


def test_cost_edit_during_approval_stays_pending(env, monkeypatch):
    _commit_patches(monkeypatch)
    rev = _start(env)
    pr.record_cost_change(PID, "u-admin", "Labor numbers edited", env.db)
    commit = pricing_router.commit_verify
    dismissed = []
    monkeypatch.setattr(pr, "dismiss_notifications", lambda **kw: dismissed.append(kw))

    def concurrent_edit(*args):
        row = commit(*args)
        pr.record_cost_change(PID, "u-admin", "Markup edited", env.db)
        return row

    monkeypatch.setattr(pricing_router, "commit_verify", concurrent_edit)
    pr.approve_costs(PID, rev["id"], EXEC)
    assert pr.costs_pending(env.one("proposal_revisions", id=rev["id"]))
    assert not dismissed
    with pytest.raises(ProposalSendError, match="Executive must approve"):
        pr._require_dispatch_head(env.db, PID)


def test_failed_pre_edit_stamp_prevents_quote_selection_change(env, monkeypatch):
    _quote_world(env)
    _start(env)

    def failed_stamp(*args):
        raise RuntimeError("Approval state write failed")

    monkeypatch.setattr(pr, "prepare_cost_change", failed_stamp)
    with pytest.raises(RuntimeError, match="Approval state write failed"):
        rfqs_router.select_quote(PID, "r1", "q2", SimpleNamespace(id="u-admin"))
    assert env.one("quotes", id="q1")["is_selected"]
    assert not env.one("quotes", id="q2")["is_selected"]


def test_pre_edit_stamp_refuses_revision_closed_after_lookup(env, monkeypatch):
    rev = _start(env)
    snapshot = dict(rev)
    env.one("proposal_revisions", id=rev["id"])["status"] = "cancelled"
    monkeypatch.setattr(pr, "open_revision", lambda *args: snapshot)
    with pytest.raises(ProposalSendError, match="no longer open"):
        pr.prepare_cost_change(PID, "u-admin", "Labor numbers edited", env.db)
    assert not env.one("proposal_revisions", id=rev["id"]).get("costs_changed_at")


def test_failed_after_edit_stamp_keeps_prepared_approval_pending(env, monkeypatch):
    _quote_world(env)
    rev = _start(env)

    def failed_stamp(*args):
        raise RuntimeError("Approval state write failed")

    monkeypatch.setattr(pr, "record_cost_change", failed_stamp)
    rfqs_router.select_quote(PID, "r1", "q2", SimpleNamespace(id="u-admin"))
    assert env.one("quotes", id="q2")["is_selected"]
    assert pr.costs_pending(env.one("proposal_revisions", id=rev["id"]))
    with pytest.raises(ProposalSendError, match="Executive must approve"):
        pr._require_dispatch_head(env.db, PID)


def test_cancel_retains_pending_costs_when_next_revision_starts(env, monkeypatch):
    _commit_patches(monkeypatch)
    rev = _start(env)
    pr.record_cost_change(PID, "u-admin", "Labor numbers edited", env.db)
    pr.cancel_revision(PID, rev["id"], "u-admin")
    next_rev = _start(env)
    assert next_rev["revision_number"] == rev["revision_number"] + 1
    assert pr.costs_pending(next_rev)
    assert next_rev["costs_change_reason"] == "Labor numbers edited"
    assert next_rev["costs_changed_by"] == "u-admin"
    with pytest.raises(ProposalSendError, match="Executive must approve"):
        pr.generate_revision_documents(PID, next_rev["id"], "u-admin")
    pr.approve_costs(PID, next_rev["id"], EXEC)
    assert not pr.costs_pending(env.one("proposal_revisions", id=next_rev["id"]))


def test_a_cost_change_after_approval_needs_approval_again(env, monkeypatch):
    _quote_world(env)
    _commit_patches(monkeypatch)
    rev = _start(env)
    user = SimpleNamespace(id="u-admin", role=None)
    rfqs_router.select_quote(PID, "r1", "q2", user)
    pr.approve_costs(PID, rev["id"], EXEC)
    env.db.now = NOW + timedelta(minutes=5)
    # The approval stamp is the wall clock; push the next change past it.
    env.one("proposal_revisions", id=rev["id"])["costs_approved_at"] = _iso(
        datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    rfqs_router.select_quote(PID, "r1", "q1", user)
    assert pr.costs_pending(env.one("proposal_revisions", id=rev["id"]))
    with pytest.raises(ProposalSendError, match="Executive must approve"):
        pr.generate_revision_documents(PID, rev["id"], "u-admin")


def test_cost_change_asks_the_executives_once(env, monkeypatch):
    _quote_world(env)
    asked = []
    monkeypatch.setattr(pr, "notify_role", lambda role, pid, type_, msg, **kw: asked.append(
        (role, type_, kw.get("metadata"))))
    rev = _start(env)
    user = SimpleNamespace(id="u-admin", role=None)
    rfqs_router.select_quote(PID, "r1", "q2", user)
    rfqs_router.select_quote(PID, "r1", "q1", user)  # still pending: no second bell
    assert asked == [(Role.EXECUTIVE, pr.APPROVAL_TYPE, {"revision_id": rev["id"]})]
    from app.services import notification_email, notification_routing

    assert notification_routing.event_for(pr.APPROVAL_TYPE) == "revision_approval"
    assert notification_routing.default(
        notification_routing.EVENT_BY_KEY["revision_approval"], Role.EXECUTIVE, "teams"
    )
    link = notification_email._deep_link(PID, "executive", pr.APPROVAL_TYPE)
    assert link.endswith(f"/projects/{PID}?box=revise_proposal")
    # A ping is throttled.
    with pytest.raises(ProposalSendError, match="minutes ago"):
        pr.request_approval(PID, rev["id"], "u-admin")


def test_generate_targets_only_checked_gcs_with_revision_markers(env):
    rev = _start(env)
    env.db.tables["project_gcs"][0]["proposal_material_amount"] = "50000"
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1"])
    created = pr.generate_revision_documents(PID, rev["id"], "u-admin")
    assert [c["gc_id"] for c in created] == ["g1"]
    row = _rev_row(env, "g1")
    assert row["status"] == "generated" and row["material_amount"] == "50000"
    file = env.one("project_files", id=row["file_id"])
    assert file["filename"] == "Proposal 7201 - Alpha Builders Rev 1.docx"
    text = extract_document_text(env.blobs[file["storage_path"]])
    assert "Revised Bid Proposal" in text and "Revision 1" in text and "$50,000" in text
    assert "Bravo GC" not in text
    assert _rev_row(env, "g2")["status"] == "pending"
    # The live rows are untouched by generation.
    assert env.one("proposal_sends", id="ps1")["file_id"] == "f-orig1"


def test_gc_prices_start_from_current_figures_not_prior_overrides(env):
    """The Cactus Ridge case: a GC bid at its own figures must not carry
    them into the revision; its revised price starts from today's figures and
    its held figures are only a reference."""
    env.db.tables["project_gcs"][1].update(proposal_material_amount="123456.78",
                                         proposal_labor_amount="9999")
    rev = _start(env)
    g2 = env.one("project_gcs", gc_id="g2")
    assert g2["proposal_material_amount"] is None and g2["proposal_labor_amount"] is None
    assert _rev_row(env, "g2")["start_overrides"]["proposal_material_amount"] == "123456.78"
    pr.generate_revision_documents(PID, rev["id"], "u-admin")
    assert _rev_row(env, "g2")["material_amount"] == str(AMOUNTS["material"])
    # The held document's figures are still shown, as a reference only.
    g2_wire = next(g for g in pr.overview(PID)["open"]["gcs"] if g["gc_id"] == "g2")
    assert g2_wire["current"]["material_amount"] == "45000"
    # Unchecking gives the GC its own figures back (it keeps its proposal).
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1"])
    assert env.one("project_gcs", gc_id="g2")["proposal_material_amount"] == "123456.78"
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1", "g2"])
    assert env.one("project_gcs", gc_id="g2")["proposal_material_amount"] is None


def test_generate_refuses_an_unchecked_gc_by_id(env):
    rev = _start(env)
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1"])
    with pytest.raises(ProposalSendError, match="unchecked"):
        pr.generate_revision_documents(PID, rev["id"], "u-admin", gc_ids=["g2"])


# ── send ──────────────────────────────────────────────────────────────────────


def _generated(env, gcs=("g1", "g2")):
    rev = _start(env)
    pr.set_included_gcs(PID, rev["id"], "u-admin", list(gcs))
    pr.generate_revision_documents(PID, rev["id"], "u-admin")
    return rev


def test_send_swaps_the_live_row_and_keeps_the_prior_snapshot(env):
    rev = _generated(env, ("g1",))
    row = _rev_row(env, "g1")
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [row["id"]],
                                     subject="Sunset Plaza: revised numbers")
    assert out["results"][0]["status"] == "sent"
    mail = env.mails[0]
    assert mail["subject"] == "Sunset Plaza: revised numbers"
    assert mail["to"] == ["pat@g1.com"]
    assert mail["attachments"][0][0] == "Proposal 7201 - Alpha Builders Rev 1.pdf"
    assert "Revision 1" in mail["body_html"] and "Alpha Builders" in mail["body_html"]
    live = env.one("proposal_sends", id="ps1")
    assert live["status"] == "sent" and live["file_id"] == row["file_id"]
    assert live["revision_number"] == 1 and live["subject"] == "Sunset Plaza: revised numbers"
    # H3: the submission record stays the ORIGINAL submission.
    assert live["sent_at"] == "2026-10-20T20:00:00+00:00" and live["sent_by"] == "u-admin"
    assert live["email_log_id"] == "log-orig" and live["sent_via"] == "email"
    assert live["gc_email"] == "pat@g1.com"
    assert live["revised_at"] == _iso(NOW) and live["revised_by"] == "u-admin"
    done = _rev_row(env, "g1")
    assert done["email_log_id"] == env.db.tables["email_log"][-1]["id"]
    assert done["prior_origin"] == "send_out"
    assert done["status"] == "sent" and done["prior_file_id"] == "f-orig1"
    assert done["prior_email_log_id"] == "log-orig" and done["prior_revision_number"] == 0
    assert done["prior_material_amount"] == "45000"
    event = env.one("proposal_send_events", kind="revision")
    assert event["proposal_send_id"] == "ps1" and event["proposal_revision_id"] == rev["id"]
    assert event["recipients"] == "pat@g1.com"
    # The unchecked GC keeps its original bid untouched.
    other = env.one("proposal_sends", id="ps2")
    assert other["file_id"] == "f-orig2" and other["revision_number"] == 0
    # Every checked GC is sent: the revision completes on its own.
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "completed"
    assert env.one("proposal_revisions", id=rev["id"])["completion"] == "auto"


def test_default_subject_and_body_are_the_revised_ones(env):
    rev = _generated(env, ("g1",))
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    mail = env.mails[0]
    assert mail["subject"] == "[G3 Electrical] Revised Proposal: Sunset Plaza (26.9.7201)"
    assert "supersedes" in mail["body_html"]


def test_send_refused_while_waiting_on_verify(env):
    rev = _generated(env, ("g1",))
    env.db.tables["project_category_state"][-1]["current_task"] = "verify"
    env.db.tables["projects"][0]["reverify_return_stage"] = "submitted"
    with pytest.raises(ProposalSendError, match="re-verify"):
        pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    assert env.mails == []


def test_isolation_checks_still_apply_to_revised_documents(env):
    rev = _generated(env, ("g1",))
    row = _rev_row(env, "g1")
    # The figures moved after generation: a stale document never goes out.
    env.db.tables["project_gcs"][0]["proposal_material_amount"] = "51000"
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [row["id"]])
    assert out["results"][0]["status"] == "failed"
    assert "Amounts changed" in out["results"][0]["error"]
    assert env.mails == [] and env.one("proposal_sends", id="ps1")["file_id"] == "f-orig1"
    # A document whose filename is not this GC's revision is refused too.
    env.db.tables["project_gcs"][0]["proposal_material_amount"] = None
    env.one("project_files", id=row["file_id"])["filename"] = build_filename(
        "26.9.7201", "Alpha Builders"
    )
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [row["id"]])
    assert "filename" in out["results"][0]["error"] and env.mails == []


def test_reclaim_proves_a_crashed_send_by_the_stored_custom_subject(env):
    rev = _generated(env, ("g1", "g2"))
    row = _rev_row(env, "g1")
    claimed = _iso(NOW - timedelta(minutes=20))
    row.update(status="sending", gc_email="pat@g1.com", subject="Custom subj",
               claimed_at=claimed, claimed_by="u-admin")
    # An email with the default subject is NOT proof of a custom-subject send.
    env.db.tables["email_log"].append(
        {"id": "log-other", "project_id": PID, "status": "sent", "to_addrs": "pat@g1.com",
         "subject": psend.revised_cover_subject(PROJECT),
         "created_at": _iso(NOW - timedelta(minutes=19))})
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [row["id"]])
    assert out["results"][0]["status"] == "failed"
    assert _rev_row(env, "g1")["error"].startswith("outcome unknown")
    assert env.mails == []
    # The stored subject is the proof: the swap is finished, nothing re-sent.
    row = _rev_row(env, "g1")
    row.update(status="sending", error=None)
    env.db.tables["email_log"].append(
        {"id": "log-proof", "project_id": PID, "status": "sent", "to_addrs": "pat@g1.com",
         "subject": "Custom subj", "created_at": _iso(NOW - timedelta(minutes=19))})
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [row["id"]])
    assert out["results"][0]["status"] == "sent" and env.mails == []
    live = env.one("proposal_sends", id="ps1")
    assert live["email_log_id"] == "log-orig" and live["subject"] == "Custom subj"
    assert _rev_row(env, "g1")["email_log_id"] == "log-proof"


def test_mark_revision_submitted(env):
    rev = _generated(env, ("g1", "g2"))
    row = _rev_row(env, "g2")
    out = pr.mark_revision_submitted(PID, rev["id"], "u-admin", [row["id"]])
    assert out["results"][0]["status"] == "marked"
    live = env.one("proposal_sends", id="ps2")
    # The submission record stays the original emailed one.
    assert live["sent_via"] == "email" and live["email_log_id"] == "log-orig"
    assert live["revision_number"] == 1 and live["file_id"] == row["file_id"]
    assert _rev_row(env, "g2")["sent_via"] == "external"
    assert env.one("proposal_send_events", kind="revision")["via"] == "external"
    assert env.mails == []
    # g1 still owed: the revision stays open.
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "open"
    assert any(a[0] == "proposal.revision_mark_submitted" for a in env.audits)


def test_cancel_only_while_nothing_went_out(env):
    rev = _generated(env, ("g1", "g2"))
    pr.mark_revision_submitted(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    with pytest.raises(ProposalSendError, match="cannot be cancelled"):
        pr.cancel_revision(PID, rev["id"], "u-admin")
    pr.complete_revision(PID, rev["id"], "u-admin")
    done = env.one("proposal_revisions", id=rev["id"])
    assert done["status"] == "completed" and done["completion"] == "manual"
    # The never-sent revised document for g2 is cleaned up.
    assert _rev_row(env, "g2")["file_id"] is None


def test_cancel_removes_unsent_documents(env):
    rev = _generated(env)
    files = [r["file_id"] for r in env.rows("proposal_revision_sends")]
    pr.cancel_revision(PID, rev["id"], "u-admin")
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "cancelled"
    assert not [f for f in env.db.tables["project_files"] if f["id"] in files]


def test_done_needs_something_sent(env):
    rev = _start(env)
    with pytest.raises(ProposalSendError, match="cancel the revision"):
        pr.complete_revision(PID, rev["id"], "u-admin")


def test_stale_revision_id_is_refused(env):
    rev = _start(env)
    pr.cancel_revision(PID, rev["id"], "u-admin")
    with pytest.raises(ProposalSendError, match="no longer open"):
        pr.generate_revision_documents(PID, rev["id"], "u-admin")


def test_outcome_refused_while_a_revision_is_open(env, monkeypatch):
    _start(env)
    body = SimpleNamespace(result="lost", winning_gc_id=None, gcs=[], notes=None)
    with pytest.raises(outcome.OutcomeError, match="Finish or cancel the revision"):
        outcome.record_outcome(PID, "u-admin", body)


def test_auto_send_stays_silent_on_a_project_under_revision(env, monkeypatch):
    rev = _start(env)
    assert pr.revision_open(PID, env.db) and rev["status"] == "open"
    project = env.one("projects", id=PID)
    # Take the "a human already sent it" arm out, so the gone-out arm alone
    # is what silences auto send at both heads a revision lives at.
    monkeypatch.setattr(auto_send, "_human_sent", lambda sb, pid: None)
    for head, back in (("submitted", None), ("verify", "submitted")):
        state = {"send_out": {"current_task": head}}
        # As stored: silent (the submitted headline already silences it).
        assert auto_send._silent_reason(
            env.db, {**project, "reverify_return_stage": back}, state, []
        ) is not None
        # Even with a headline that would not, the gone-out arm holds.
        reason = auto_send._silent_reason(
            env.db,
            {**project, "current_stage": "verify", "reverify_return_stage": back},
            state,
            [],
        )
        assert reason == "gone_out", (head, reason)


# ── a GC added during the revision ───────────────────────────────────────────


def test_added_gc_gets_a_normal_initial_proposal(env):
    env.db.tables["project_gcs"].append(_gc_link("g3", "Charlie Co"))
    rev = _start(env)
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1"])
    psend.set_gc_amounts(PID, "g3", {"material": Decimal("47000")}, "u-admin",
                         role=Role.ESTIMATING_ADMIN)
    created = psend.generate_documents(PID, "d1", "u-admin", gc_ids=["g3"])
    row = created[0]
    assert row["gc_id"] == "g3"
    file = env.one("project_files", id=row["file_id"])
    assert file["filename"] == "Proposal 7201 - Charlie Co.docx"
    assert "Revised Bid Proposal" not in extract_document_text(env.blobs[file["storage_path"]])
    out = psend.send_proposals(PID, "u-admin", [row["id"]])
    assert out["results"][0]["status"] == "sent"
    assert env.mails[-1]["subject"] == psend.build_cover_email(PROJECT)[0]
    event = env.one("proposal_send_events", gc_id="g3")
    assert event["kind"] == "initial" and event["proposal_revision_id"] == rev["id"]
    # g1 still owed its revision: open; and now Cancel is refused.
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "open"
    with pytest.raises(ProposalSendError, match="cannot be cancelled"):
        pr.cancel_revision(PID, rev["id"], "u-admin")


def test_readded_gc_can_regenerate_a_retired_initial_proposal(env):
    _start(env)
    env.db.tables["project_gcs"].append(_gc_link("g3", "Charlie Co"))
    original = psend.generate_documents(PID, "d1", "u-admin", gc_ids=["g3"])[0]
    psend.retire_unsent_proposals(PID, "g3")
    assert env.one("proposal_sends", id=original["id"])["status"] == "superseded"
    regenerated = psend.generate_documents(PID, "d1", "u-admin", gc_ids=["g3"])[0]
    assert regenerated["id"] == original["id"]
    assert regenerated["status"] == "generated"
    assert regenerated["file_id"] != original["file_id"]
    assert env.one("proposal_sends", gc_id="g1")["status"] == "sent"


def test_retirement_racing_initial_generation_is_not_overwritten(env, monkeypatch):
    _start(env)
    env.db.tables["project_gcs"].append(_gc_link("g3", "Charlie Co"))
    original = psend.generate_documents(PID, "d1", "u-admin", gc_ids=["g3"])[0]
    upload = storage.upload_file

    def retire_during_render(*args, **kwargs):
        psend.retire_unsent_proposals(PID, "g3")
        return upload(*args, **kwargs)

    monkeypatch.setattr(storage, "upload_file", retire_during_render)
    with pytest.raises(ProposalSendError, match="changed while generating"):
        psend.generate_documents(PID, "d1", "u-admin", gc_ids=["g3"])
    retired = env.one("proposal_sends", id=original["id"])
    assert retired["status"] == "superseded"
    assert retired["file_id"] == original["file_id"]


def test_auto_complete_waits_for_an_added_gc_generated_during_the_revision(env):
    env.db.tables["project_gcs"].append(_gc_link("g3", "Charlie Co"))
    rev = _generated(env, ("g1",))
    env.db.now = NOW + timedelta(minutes=1)
    psend.generate_documents(PID, "d1", "u-admin", gc_ids=["g3"])
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "open"
    initial = env.one("proposal_sends", gc_id="g3")
    psend.send_proposals(PID, "u-admin", [initial["id"]])
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "completed"


# ── re-send after a revision ─────────────────────────────────────────────────


def test_resend_after_a_revision_resends_the_revised_document(env):
    rev = _generated(env, ("g1", "g2"))
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]],
                               subject="Revised: Sunset Plaza")
    pr.complete_revision(PID, rev["id"], "u-admin")
    out = psend.resend_proposals(PID, "u-admin", ["ps1"])
    assert out["results"][0]["status"] == "sent", out
    mail = env.mails[-1]
    assert mail["subject"] == "Revised: Sunset Plaza"
    assert mail["attachments"][0][0] == "Proposal 7201 - Alpha Builders Rev 1.pdf"
    assert "Revision 1" in mail["body_html"]
    assert env.rows("proposal_send_events", kind="resend")[0]["status"] == "sent"


# ── overview ─────────────────────────────────────────────────────────────────


def test_overview_open_and_history(env):
    rev = _generated(env, ("g1", "g2"))
    view = pr.overview(PID)
    assert view["open"]["id"] == rev["id"] and view["can_start"] is False
    assert {g["gc_id"]: g["status"] for g in view["open"]["gcs"]} == {
        "g1": "generated", "g2": "generated"
    }
    assert view["open"]["gcs"][0]["stale_reason"] is None
    assert view["open"]["default_subject"].startswith("[G3 Electrical] Revised Proposal")
    pr.send_revision_documents(PID, rev["id"], "u-admin",
                               [_rev_row(env, "g1")["id"], _rev_row(env, "g2")["id"]])
    view = pr.overview(PID)
    assert view["open"] is None and view["can_start"] is True
    assert view["next_revision_number"] == 2
    past = view["history"][0]
    assert past["revision_number"] == 1 and past["status"] == "completed"
    g1 = next(g for g in past["gcs"] if g["gc_id"] == "g1")
    assert g1["prior"]["filename"] == "Proposal 7201 - Alpha Builders.docx"
    assert g1["filename"] == "Proposal 7201 - Alpha Builders Rev 1.docx"


def test_overview_flags_a_stale_revised_document(env):
    _generated(env, ("g1",))
    env.db.tables["project_gcs"][0]["proposal_material_amount"] = "52000"
    g1 = next(g for g in pr.overview(PID)["open"]["gcs"] if g["gc_id"] == "g1")
    assert g1["stale_reason"] == "amounts_changed"


def test_external_submission_undo_is_blocked_by_a_revision(env):
    from app.services import external_submission as es

    active = {"submitted_at": "2026-10-01T00:00:00+00:00"}
    assert es._revised_since(env.db, PID, active) is False
    rev = _generated(env, ("g1",))
    assert es._revised_since(env.db, PID, active) is True  # open
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "completed"
    assert es._revised_since(env.db, PID, active) is True  # a revised proposal went out


# ── review follow-ups (H1-H3, M1-M6, low) ────────────────────────────────────


def _claimed(env, gc_id, minutes_ago, subject="Custom subj"):
    """A claim made `minutes_ago` by the wall clock (the reclaim's staleness
    reads the real time)."""
    row = _rev_row(env, gc_id)
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    row.update(status="sending", gc_email=f"pat@{gc_id}.com", subject=subject,
               claimed_at=_iso(at), claimed_by="u-admin")
    return row


def test_h1_stale_sending_row_is_resolved_by_get(env):
    rev = _generated(env, ("g1", "g2"))
    _claimed(env, "g1", 20)
    view = pr.overview(PID)
    g1 = next(g for g in view["open"]["gcs"] if g["gc_id"] == "g1")
    assert g1["status"] == "failed" and g1["state"] == "outcome_unknown"
    assert view["open"]["any_sending"] is False
    # A fresh claim is left alone and tells the UI when it will be checked.
    _claimed(env, "g2", 1)
    g2 = next(g for g in pr.overview(PID)["open"]["gcs"] if g["gc_id"] == "g2")
    assert g2["status"] == "sending" and g2["reclaim_after"]
    assert rev["status"] == "open"


def test_h1_email_left_but_swap_refused_is_terminal(env, monkeypatch):
    rev = _generated(env, ("g1", "g2"))
    real = env.db._apply

    def refuse(p):
        env.db._refuse("not_sent")

    monkeypatch.setattr(env.db, "_apply", refuse)
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    assert len(env.mails) == 1  # the email did go out
    row = _rev_row(env, "g1")
    assert row["status"] == "failed" and row["error"].startswith(pr.DELIVERED_PREFIX)
    assert out["results"][0]["status"] == "failed"
    assert env.one("proposal_sends", id="ps1")["file_id"] == "f-orig1"
    monkeypatch.setattr(env.db, "_apply", real)
    # Never re-sent, not even with force; not markable either.
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [row["id"]], force=True)
    assert out["results"][0]["status"] == "failed" and len(env.mails) == 1
    out = pr.mark_revision_submitted(PID, rev["id"], "u-admin", [row["id"]])
    assert out["results"][0]["status"] == "failed"
    # Cancel is refused (it may have reached the GC); Done closes it and keeps
    # its document.
    with pytest.raises(ProposalSendError, match="cannot be"):
        pr.cancel_revision(PID, rev["id"], "u-admin")
    pr.complete_revision(PID, rev["id"], "u-admin")
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "completed"
    assert _rev_row(env, "g1")["file_id"] is not None
    assert _rev_row(env, "g2")["file_id"] is None  # never sent: cleaned up


def test_h1_reclaim_with_proof_but_refused_swap_is_delivered_unrecorded(env, monkeypatch):
    _generated(env, ("g1",))
    _claimed(env, "g1", 20)
    env.db.tables["email_log"].append(
        {"id": "log-proof", "project_id": PID, "status": "sent", "to_addrs": "pat@g1.com",
         "subject": "Custom subj",
         "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=19))})
    monkeypatch.setattr(env.db, "_apply", lambda p: env.db._refuse("not_sent"))
    g1 = next(g for g in pr.overview(PID)["open"]["gcs"] if g["gc_id"] == "g1")
    assert g1["state"] == "delivered_unrecorded"


def test_h2_done_and_cancel_refused_while_a_row_is_sending(env):
    rev = _generated(env, ("g1", "g2"))
    pr.mark_revision_submitted(PID, rev["id"], "u-admin", [_rev_row(env, "g2")["id"]])
    sending = _claimed(env, "g1", 1)
    for action in (pr.complete_revision, pr.cancel_revision):
        with pytest.raises(ProposalSendError, match="being sent"):
            action(PID, rev["id"], "u-admin")
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "open"
    # The in-flight document was not touched.
    assert _rev_row(env, "g1")["file_id"] == sending["file_id"]


def test_h2_claim_requires_the_revision_open(env):
    rev = _generated(env, ("g1",))
    row = _rev_row(env, "g1")
    env.one("proposal_revisions", id=rev["id"])["status"] = "cancelled"
    with pytest.raises(APIError, match="not_open"):
        env.db._claim({"p_revision_send_id": row["id"], "p_user_id": "u", "p_gc_email": "x",
                       "p_subject": "s", "p_body": "b", "p_cc": None})


def test_rpc_error_codes_map_to_messages(env):
    with pytest.raises(ProposalSendError) as exc:
        pr._rpc(env.db, "finish_proposal_revision",
                {"p_revision_id": "nope", "p_user_id": "u", "p_mode": "done"})
    assert exc.value.code == "not_found"
    err = APIError({"message": "proposal_revision:went_out", "code": "P0001"})
    assert pr.rpc_error_code(err) == "went_out"
    assert pr.rpc_error_code(APIError({"message": "boom", "code": "500"})) is None


def test_m1_resend_refused_for_a_gc_being_revised(env):
    rev = _generated(env, ("g1",))
    out = psend.resend_proposals(PID, "u-admin", ["ps1", "ps2"])
    by = {r["proposal_id"]: r for r in out["results"]}
    assert by["ps1"]["status"] == "skipped" and "Revise proposal" in by["ps1"]["error"]
    # The unchecked GC keeps its own proposal and is not held back by the
    # revision (the fake store has no bytes for its original document).
    assert "Revise proposal" not in (by["ps2"]["error"] or "")
    assert by["ps2"]["status"] != "skipped"
    # A re-send in flight blocks the revision claim for that GC.
    env.db.tables["proposal_send_events"].append(
        {"id": "ev-inflight", "proposal_send_id": "ps1", "project_id": PID, "gc_id": "g1",
         "kind": "resend", "status": "sending"})
    out = pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    assert out["results"][0]["status"] == "failed"
    assert "re-send" in out["results"][0]["error"]


def test_m2_figures_restored_when_unchecked_or_closed_unsent(env):
    env.db.tables["project_gcs"][0]["proposal_material_amount"] = "44000"
    rev = _start(env)
    psend.set_gc_amounts(PID, "g1", {"material": Decimal("50000")}, "u-admin",
                         role=Role.ESTIMATING_ADMIN)
    psend.set_gc_amounts(PID, "g2", {"material": Decimal("51000")}, "u-admin",
                         role=Role.ESTIMATING_ADMIN)
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g2"])
    assert env.one("project_gcs", gc_id="g1")["proposal_material_amount"] == "44000"
    pr.cancel_revision(PID, rev["id"], "u-admin")
    assert env.one("project_gcs", gc_id="g2")["proposal_material_amount"] is None


def test_m2_sent_gc_keeps_its_new_figures_on_done(env):
    rev = _start(env)
    env.db.tables["project_gcs"][0]["proposal_material_amount"] = "52000"
    pr.generate_revision_documents(PID, rev["id"], "u-admin")
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    env.db.tables["project_gcs"][1]["proposal_material_amount"] = "53000"
    pr.complete_revision(PID, rev["id"], "u-admin")
    # g1 now holds the 52k document: its figures stay. g2's revision did not
    # go out: back to its start values (none).
    assert env.one("project_gcs", gc_id="g1")["proposal_material_amount"] == "52000"
    assert env.one("project_gcs", gc_id="g2")["proposal_material_amount"] is None


def test_m3_cancel_refused_with_an_outcome_unknown_row(env):
    rev = _generated(env, ("g1",))
    row = _rev_row(env, "g1")
    row.update(status="failed", error="outcome unknown (ReadTimeout): check Sent Items")
    with pytest.raises(ProposalSendError, match="cannot be cancelled"):
        pr.cancel_revision(PID, rev["id"], "u-admin")
    pr.complete_revision(PID, rev["id"], "u-admin")
    assert _rev_row(env, "g1")["file_id"] == row["file_id"]  # never deleted


def test_m4_auto_complete_waits_for_an_added_gc_with_nothing_generated(env):
    rev = _generated(env, ("g1",))
    env.db.tables["project_gcs"].append(_gc_link("g3", "Charlie Co"))
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "open"
    view = pr.overview(PID)
    g3 = next(g for g in view["open"]["added_gcs"] if g["gc_id"] == "g3")
    assert g3["status"] == "not_generated" and g3["added_during_revision"] is True


def test_m6_head_rechecked_before_each_gc_email(env, monkeypatch):
    rev = _generated(env, ("g1", "g2"))
    real_send = graph_email.send_mail

    def send_then_bounce(**kw):
        log = real_send(**kw)
        # A cost edit lands while the first GC is being emailed.
        env.db.tables["project_category_state"][-1]["current_task"] = "verify"
        env.db.tables["projects"][0]["reverify_return_stage"] = "submitted"
        return log

    monkeypatch.setattr(graph_email, "send_mail", send_then_bounce)
    out = pr.send_revision_documents(
        PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"], _rev_row(env, "g2")["id"]]
    )
    by = {r["gc_id"]: r for r in out["results"]}
    assert by["g1"]["status"] == "sent"
    assert by["g2"]["status"] == "failed" and "re-verify" in by["g2"]["error"]
    assert len(env.mails) == 1


def test_low_uncheck_audits_only_real_changes(env):
    rev = _start(env)
    before = len(env.audits)
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1", "g2"])  # nothing changes
    assert len(env.audits) == before
    pr.set_included_gcs(PID, rev["id"], "u-admin", ["g1"])
    assert env.audits[-1][0] == "proposal.revision_gcs"
    assert env.audits[-1][1]["changes"] == [
        {"gc_id": "g2", "gc_name": "Bravo GC", "included": False}
    ]


def test_low_swap_snapshots_origin(env):
    env.db.tables["proposal_sends"][0].update(origin="auto_send", auto_send_run_id="run-1")
    rev = _generated(env, ("g1",))
    pr.mark_revision_submitted(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    row = _rev_row(env, "g1")
    assert row["prior_origin"] == "auto_send" and row["prior_auto_send_run_id"] == "run-1"
    assert env.one("proposal_sends", id="ps1")["origin"] == "send_out"


def test_low_late_quote_selected_during_revision_counts_again(env):
    _quote_world(env)
    env.one("quotes", id="q2")["received_after_submission"] = True
    _start(env)
    rfqs_router.select_quote(PID, "r1", "q2", SimpleNamespace(id="u-admin", role=None))
    assert env.one("quotes", id="q2")["received_after_submission"] is False


def test_low_removed_gc_does_not_block_generate_or_auto_complete(env):
    rev = _start(env)
    env.db.tables["project_gcs"] = [g for g in env.db.tables["project_gcs"] if g["gc_id"] != "g2"]
    created = pr.generate_revision_documents(PID, rev["id"], "u-admin")
    assert [c["gc_id"] for c in created] == ["g1"]
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "completed"


def test_proposal_history_files_cannot_be_deleted(env, monkeypatch):
    from app.routers import files as files_router

    monkeypatch.setattr(files_router, "get_supabase", lambda: env.db)
    rev = _generated(env, ("g1",))
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    # The original document is no longer the held one, but it went out.
    assert files_router._is_proposal_history("f-orig1") is True
    assert files_router._is_proposal_history(_rev_row(env, "g1")["file_id"]) is True
    assert files_router._is_proposal_history("f-unrelated") is False


def test_notification_log_classifies_revision_emails(env, monkeypatch):
    from app.services import notification_log

    rev = _generated(env, ("g1",))
    pr.send_revision_documents(PID, rev["id"], "u-admin", [_rev_row(env, "g1")["id"]])
    env.db.tables.setdefault("rfqs", [])
    env.db.tables.setdefault("file_send_batches", [])
    classes = notification_log._classify_emails(env.db, PID)
    log_id = _rev_row(env, "g1")["email_log_id"]
    assert classes[log_id]["title"] == "Revised proposal sent to the GC"
    assert classes["log-orig"]["title"] == "Proposal sent to the GC"


# ── router: roles and the start ordering ─────────────────────────────────────


@pytest.fixture
def client(env, monkeypatch):
    import app.main
    from fastapi.testclient import TestClient

    from app.core.deps import CurrentUser, get_current_user

    who = {"role": Role.ESTIMATING_ADMIN}
    app.main.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id="u-admin", email="a@g3.test", role=who["role"], is_active=True
    )
    yield TestClient(app.main.app), who
    app.main.app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("role", [Role.ESTIMATOR, Role.ACCOUNTANT])
def test_router_writes_refused_for_estimator_and_accountant(env, client, role):
    c, who = client
    rev = _start(env)
    who["role"] = role
    base = f"/projects/{PID}/proposal-revisions"
    for method, path, body in (
        ("post", base, {"reason": "x"}),
        ("put", f"{base}/{rev['id']}/gcs", {"gc_ids": []}),
        ("post", f"{base}/{rev['id']}/generate", {}),
        ("post", f"{base}/{rev['id']}/send", {"proposal_ids": ["x"]}),
        ("post", f"{base}/{rev['id']}/mark-submitted", {"proposal_ids": ["x"]}),
        ("post", f"{base}/{rev['id']}/complete", None),
        ("post", f"{base}/{rev['id']}/cancel", None),
    ):
        resp = getattr(c, method)(path, json=body) if body is not None else getattr(c, method)(path)
        assert resp.status_code == 403, (role, path, resp.status_code)
    read = c.get(base)
    assert read.status_code == (200 if role == Role.ACCOUNTANT else 403)


def test_router_refused_start_never_touches_the_bid_date(env, client, monkeypatch):
    from app.routers import projects as projects_router

    c, _who = client
    patched = []
    monkeypatch.setattr(projects_router, "update_project", lambda *a, **k: patched.append(a))
    env.db.tables["project_category_state"][-1]["current_task"] = "send_out"
    resp = c.post(f"/projects/{PID}/proposal-revisions",
                  json={"reason": "moved", "internal_bid_at": "2026-11-01T21:00:00Z"})
    assert resp.status_code == 409 and patched == []


def test_router_refused_date_change_keeps_the_revision_open(env, client, monkeypatch):
    from app.routers import projects as projects_router

    c, _who = client

    def refuse(*a, **k):
        raise HTTPException(403, "Your role may not edit: actual_bid_at")

    monkeypatch.setattr(projects_router, "update_project", refuse)
    resp = c.post(f"/projects/{PID}/proposal-revisions",
                  json={"reason": "moved", "actual_bid_at": "2026-11-01T21:00:00Z"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["open"]["revision_number"] == 1
    assert "bid date was not changed" in body["bid_date_error"]
    assert body["open"]["new_actual_bid_at"] is None


# ── the lock: writes outside the modal are refused (0179) ────────────────────

LOCKED_ROUTES = {
    ("PUT", "/projects/{project_id}/labor"),
    ("PUT", "/projects/{project_id}/markup"),
    ("PUT", "/projects/{project_id}/verify"),
    ("POST", "/projects/{project_id}/verify"),
    ("POST", "/projects/{project_id}/rfqs"),
    ("POST", "/projects/{project_id}/rfqs/bulk-send"),
    ("POST", "/projects/{project_id}/rfqs/{rfq_id}/quotes"),
    ("POST", "/projects/{project_id}/rfqs/{rfq_id}/quotes/manual"),
    ("DELETE", "/projects/{project_id}/rfqs/{rfq_id}/quotes/{quote_id}"),
    ("PATCH", "/projects/{project_id}/rfqs/{rfq_id}/quotes/{quote_id}"),
    ("POST", "/projects/{project_id}/rfqs/{rfq_id}/quotes/{quote_id}/approval"),
    ("POST", "/projects/{project_id}/rfqs/{rfq_id}/quotes/{quote_id}/select"),
    ("DELETE", "/projects/{project_id}/rfqs/{rfq_id}/select"),
    ("PUT", "/projects/{project_id}/rfqs/{rfq_id}/quotes-confirmed"),
    ("PUT", "/projects/{project_id}/rfqs/{rfq_id}/quotes/{quote_id}/tax"),
    ("PUT", "/projects/{project_id}/general-material"),
    ("PUT", "/projects/{project_id}/general-material/tax"),
    ("POST", "/projects/{project_id}/general-material/extract"),
    ("POST", "/projects/{project_id}/proposal-lines"),
    ("POST", "/projects/{project_id}/proposal-lines/manual"),
    ("PUT", "/projects/{project_id}/proposal-lines/{draft_id}/lines"),
    ("POST", "/projects/{project_id}/proposal-lines/{draft_id}/approve"),
    ("PUT", "/projects/{project_id}/proposals/amounts/{gc_id}"),
    ("POST", "/projects/{project_id}/proposals/amounts/{gc_id}/request"),
    ("POST", "/projects/{project_id}/proposals/generate"),
    ("POST", "/projects/{project_id}/proposals/send"),
    ("POST", "/projects/{project_id}/proposals/resend"),
    ("POST", "/projects/{project_id}/proposals/mark-submitted"),
    ("POST", "/projects/{project_id}/proposals/complete-send-out"),
    ("POST", "/projects/{project_id}/external-submission"),
    ("POST", "/projects/{project_id}/external-submission/undo"),
    ("POST", "/projects/{project_id}/auto-send/accept"),
    ("POST", "/projects/{project_id}/boq-analysis/{analysis_id}/confirm"),
}


def _locked_routes():
    import app.main
    from app.core.revision_context import require_revision_context

    def deps(dependant):
        for d in dependant.dependencies:
            yield d.call
            yield from deps(d)

    out = set()
    for route in app.main.app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant and require_revision_context in set(deps(dependant)):
            for method in route.methods:
                out.add((method, route.path))
    return out


def test_every_cost_pricing_and_proposal_write_is_behind_the_lock():
    locked = _locked_routes()
    missing = {r for r in LOCKED_ROUTES if r not in locked}
    assert not missing, missing
    # Uploads, the estimator file updates, notes and the revision routes
    # themselves stay open (the modal and the rest of the page use them).
    for open_route in (
        ("POST", "/projects/{project_id}/files"),
        ("POST", "/projects/{project_id}/send-file-updates"),
        ("POST", "/projects/{project_id}/proposal-revisions/{revision_id}/generate"),
        ("POST", "/projects/{project_id}/proposals/amounts/{gc_id}/approve"),
    ):
        assert open_route not in locked, open_route


def test_lock_refuses_without_the_header_and_passes_with_it(env):
    from app.core.revision_context import require_revision_context

    assert require_revision_context(PID, None) is None  # no revision: unchanged
    rev = _start(env)
    for header in (None, "some-other-revision"):
        with pytest.raises(HTTPException) as exc:
            require_revision_context(PID, header)
        assert exc.value.status_code == 409
        assert exc.value.headers["X-Error-Code"] == "revision_open"
        assert "Revision 1 is open" in exc.value.detail
    assert require_revision_context(PID, rev["id"]) == rev["id"]
    pr.cancel_revision(PID, rev["id"], "u-admin")
    assert require_revision_context(PID, None) is None


def test_locked_route_over_http(env, client, monkeypatch):
    c, _who = client
    _start(env)
    for method, path in (("put", "markup"), ("put", "labor"), ("post", "proposals/generate")):
        resp = getattr(c, method)(f"/projects/{PID}/{path}", json={})
        assert resp.status_code == 409, path
        assert resp.headers["x-error-code"] == "revision_open"
        assert "happen in Revise proposal" in resp.json()["detail"]


def test_revision_lock_authenticates_before_exposing_open_revision(env, client):
    from app.core.deps import get_current_user
    from app.main import app

    c, _who = client
    _start(env)
    override = app.dependency_overrides.pop(get_current_user)
    try:
        response = c.put(f"/projects/{PID}/labor", json={})
    finally:
        app.dependency_overrides[get_current_user] = override
    assert response.status_code == 401
    assert "revision_open" not in response.headers.get("x-error-code", "")


def test_router_only_executives_approve_costs(env, client):
    c, who = client
    _quote_world(env)
    rev = _start(env)
    rfqs_router.select_quote(PID, "r1", "q2", SimpleNamespace(id="u-admin", role=None))
    for role in (Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER_LABOR, Role.ACCOUNTANT):
        who["role"] = role
        resp = c.post(f"/projects/{PID}/proposal-revisions/{rev['id']}/approve-costs")
        assert resp.status_code == 403, role
    assert pr.costs_pending(env.one("proposal_revisions", id=rev["id"]))


def test_files_uploaded_from_the_modal_join_the_revision(env):
    rev = _start(env)
    env.db.tables["project_files"].append({"id": "f-plan", "project_id": PID, "filename": "A1 rev.pdf"})
    pr.tag_revision_file(PID, "not-the-open-one", "f-plan")
    assert env.one("project_files", id="f-plan").get("proposal_revision_id") is None
    pr.tag_revision_file(PID, rev["id"], "f-plan")
    assert env.one("project_files", id="f-plan")["proposal_revision_id"] == rev["id"]
    files = pr.overview(PID)["open"]["files"]
    assert [f["id"] for f in files] == ["f-plan"]


def test_quotes_added_during_a_revision_are_candidates_not_late(env):
    from app.models.schemas import ManualQuoteIn

    _quote_world(env)
    _start(env)
    user = SimpleNamespace(id="u-admin", role=Role.ESTIMATING_ENGINEER_MATERIALS)
    row = rfqs_router.add_manual_quote(
        PID, "r1",
        ManualQuoteIn(amount=Decimal("8"), tax_included=True, tax_rate=Decimal("0")),
        user,
    )
    assert not row.get("received_after_submission")
    assert row["is_approved"] is True


def test_cancel_keeps_cost_changes(env, monkeypatch):
    _quote_world(env)
    rev = _start(env)
    rfqs_router.select_quote(PID, "r1", "q2", SimpleNamespace(id="u-admin", role=None))
    pr.cancel_revision(PID, rev["id"], "u-admin")
    assert env.one("quotes", id="q2")["is_selected"] is True  # not rolled back
    assert env.one("proposal_revisions", id=rev["id"])["status"] == "cancelled"


def test_costs_wire_shows_held_and_current_figures(env, monkeypatch):
    rev = _start(env)
    start = env.one("proposal_revisions", id=rev["id"])["start_verification"]
    assert start and start["committed_at"]
    costs = pr.overview(PID)["open"]["costs"]
    assert costs["pending"] is False and costs["start"] and costs["current"]
    assert set(costs["start"]["prices"]) >= {"material", "labor", "total"}


def test_held_figures_are_what_each_gc_holds(env):
    """'Held' is each GC's live proposal (its per-GC price), not the
    project-level committed figure (the SND-05 step 5 report)."""
    env.db.tables["proposal_sends"][0].update(material_amount="340000", labor_amount="31791.63")
    _start(env)
    costs = pr.overview(PID)["open"]["costs"]
    held = {g["gc_id"]: g for g in costs["held_by_gc"]}
    assert held["g1"]["total"] == "371791.63" and held["g2"]["total"] == "76000"
    assert costs["held_total_range"] == {"min": "76000", "max": "371791.63"}
    assert costs["current"]["prices"]["total"] != held["g1"]["total"]
