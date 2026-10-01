"""The RFP email-intake router (app/routers/rfp_emails) called as plain
functions against an in-memory fake Supabase, with app/services/rfp_email_ingest
and app/services/rfp_match stubbed (they own every state transition and the
scorer, and are built alongside this router).

What is pinned (docs/RFP_EMAIL_INGESTION.md sections 3.6, 3.7, 7 and 9, and
docs/RFP_MATCHING.md sections 3.8, 7, 8 and 9):

- The route table: exactly the (method, path) pairs of the two section 7
  tables plus the harvest and creation routes (docs/RFP_HARVEST.md 5,
  docs/RFP_CREATE.md 8), every one carrying the env-flag gate (router-level, so it
  resolves before auth) and exactly one role dependency plus the catch-all
  rate limiter.
- Role gating: the review queue admits the Estimating Admin, the IT Admin,
  the Executive and both engineer focuses and nobody else; rule management
  and unmerge admit the Executive, the Estimating Admin and the IT Admin. The
  read-only accountant and the external estimator are 403 on all three.
- Locked rules are IT Admin only, on BOTH sides: POST {locked: true} and
  DELETE of a locked row, each a 403 tagged rfp_rule_locked_it_admin_only.
- A `domain` rule naming a public mailbox provider is a 400 carrying the
  service's descriptive sentence (and the sharpened public-domain code), and
  nothing is written.
- Tabs map to the right statuses (Matches = review_match; Processed = done,
  merged, duplicate), newest first, with the sighting mailboxes, the match
  project and the resolved GC attached; counts answer all five buckets.
- The detail route selects an explicit column list (never `*`), attaches the
  candidates (names refreshed), the latest match record, the excluded
  projects and the possible rebid, and runs the whole row through
  `rfp_match.redact_candidates` for roles outside ACTUAL_BID_VIEWER_ROLES
  only: an engineer's response carries no date sub-score whose closest_kind
  was `actual` and no `actual` date kind, an admin's is untouched.
- The human actions 404 a missing row, 409 a row that moved on (LookupError
  from the service) and never double-audit: the service writes those rows
  inside the conditional update that wins the race. The match actions map
  an RfpMatchError's own code (409, or 400 for rfp_match_gc_required), an
  excluded project is 409 rfp_match_project_excluded on merge and duplicate,
  and unmerge maps the removal helper's ProposalSendError to 409
  rfp_match_gc_already_sent. The two rule writes, which nothing else
  records, ARE audited here.
- GC contacts at gmail.com are SAVED, not refused, and come back with the
  `rfp_notice` advisory; a company-domain contact's response is unchanged.
"""

from __future__ import annotations

import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.error_codes import ErrorCode
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, RFP_REVIEW_ROLES, Role
from app.models.schemas import (
    RfpMatchDuplicateIn,
    RfpMatchGcIn,
    RfpMatchMergeIn,
    RfpMatchReopenIn,
    RfpMatchUnmergeIn,
)
from app.routers import reference as ref
from app.routers import rfp_emails as rr
from app.services import llm_queue, notifications
from app.services import rfp_harvest as harvest_svc
from app.services import rfp_email_auth as auth_svc
from app.services.proposal_send import ProposalSendError

E1 = "3f1c0b00-0000-4000-8000-000000000001"
E2 = "3f1c0b00-0000-4000-8000-000000000002"
E3 = "3f1c0b00-0000-4000-8000-000000000003"
E4 = "3f1c0b00-0000-4000-8000-000000000004"
E5 = "3f1c0b00-0000-4000-8000-000000000005"  # waiting at review_match
E6 = "3f1c0b00-0000-4000-8000-000000000006"  # merged
MISSING = "3f1c0b00-0000-4000-8000-0000000000ff"
RULE_LOCKED = "3f1c0b00-0000-4000-8000-000000000101"
RULE_OPEN = "3f1c0b00-0000-4000-8000-000000000102"
BLOCK_LOCKED = "3f1c0b00-0000-4000-8000-000000000201"   # platform seed (0133)
BLOCK_OPEN = "3f1c0b00-0000-4000-8000-000000000202"     # added by a person
P1 = "5a000000-0000-4000-8000-000000000001"
P2 = "5a000000-0000-4000-8000-000000000002"
P3 = "5a000000-0000-4000-8000-000000000003"
G1 = "6b000000-0000-4000-8000-000000000001"
M1 = "7c000000-0000-4000-8000-000000000001"
M2 = "7c000000-0000-4000-8000-000000000002"
M3 = "7c000000-0000-4000-8000-000000000003"
NOT_A_UUID = "not-a-uuid"


# ── Fake Supabase ────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._ins = []
        self._sel = "*"
        self._count = None
        self._orders = []
        self._limit = None
        self._range = None
        self._overlaps = []

    def select(self, sel="*", count=None, **k):
        self._op, self._sel, self._count = "select", sel or "*", count
        self.db.selects.append((self.table, self._sel))
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
        self._ins.append((col, list(vals)))
        return self

    def overlaps(self, col, vals):
        """PostgREST `ov` on a text[] column: the row's array must share at
        least one value with the one given (migration 0134's mailbox scope)."""
        self._overlaps.append((col, set(vals)))
        return self

    def order(self, col, desc=False, **k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **k):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def _matches(self, row):
        return (
            all(row.get(c) == v for c, v in self._filters)
            and all(row.get(c) in vals for c, vals in self._ins)
            and all(
                set(row.get(c) or []) & vals for c, vals in self._overlaps
            )
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            total = len(hits)
            for col, desc in reversed(self._orders):
                hits = sorted(hits, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._range is not None:
                start, end = self._range
                hits = hits[start : end + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            if self._sel.strip() == "*":
                out = [copy.deepcopy(r) for r in hits]
            else:
                cols = [c.strip() for c in self._sel.split(",") if c.strip()]
                out = [{c: copy.deepcopy(r.get(c)) for c in cols} for r in hits]
            return SimpleNamespace(data=out, count=total if self._count else None)
        if self._op == "insert":
            payload = self._payload
            items = payload if isinstance(payload, list) else [payload]
            made = []
            for item in items:
                row = dict(item)
                row.setdefault("id", f"{self.table}-{len(rows) + len(made) + 1}")
                row.setdefault("created_at", self.db.stamp())
                for cols in self.db.unique.get(self.table, []):
                    key = tuple(row.get(c) for c in cols)
                    if any(tuple(r.get(c) for c in cols) == key for r in rows):
                        raise RuntimeError(
                            'duplicate key value violates unique constraint (23505)'
                        )
                made.append(row)
            rows.extend(made)
            return SimpleNamespace(data=[dict(r) for r in made], count=None)
        if self._op == "delete":
            hits = [dict(r) for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=hits, count=None)
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out, count=None)
        return SimpleNamespace(data=[], count=None)


class FakeDB:
    #: unique indexes the fake enforces, mirroring migration 0120.
    unique = {
        "rfp_authorized_senders": [("kind", "value")],
        "rfp_blocked_senders": [("kind", "value")],   # 0133
    }

    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.selects: list[tuple[str, str]] = []
        self._tick = 0

    def table(self, name):
        return _Query(self, name)

    def stamp(self):
        self._tick += 1
        return f"2026-09-09T00:00:{self._tick:02d}+00:00"


class _BlockDuplicate(LookupError):
    """The shape of rfp_email_ingest.BlockDuplicate (doc 3.1)."""


class _MatchErr(LookupError):
    """The shape of rfp_email_ingest.RfpMatchError (doc section 12): a
    LookupError carrying the error code the router puts in the header."""

    def __init__(self, code, message="refused"):
        super().__init__(message)
        self.code = code


class _Ingest:
    """Stand-in for app/services/rfp_email_ingest: records the calls the router
    makes and raises whatever the test arms it with."""

    def __init__(self):
        self.calls = []
        self.raise_lookup = False
        self.raise_value = False
        self.raise_create = None     # an exception instance for the create action
        self.return_create = None    # a rfp_create.Created to answer with instead
        self.raise_match = None      # an exception instance for the match actions
        self.rescan = 0
        self.parked = 0              # what a block reports it ended
        self.raise_duplicate = False  # arm the 409 on a second block
        self.latest_match = None     # what latest_match_for_email answers
        self.excluded = []           # what excluded_projects_for_email answers
        self.stats = {"confident": {"pending": 2, "agreed": 5, "disagreed": 1}}

    def _act(self, name, sb, email_id, *rest):
        self.calls.append((name, email_id, *rest))
        if self.raise_lookup:
            raise LookupError("That message is no longer waiting for a decision.")
        return {"id": email_id, "status": "done"}

    def _match_act(self, name, sb, email_id, *rest):
        self.calls.append((name, email_id, *rest))
        if self.raise_match is not None:
            raise self.raise_match
        return {"id": email_id, "status": "merged"}

    # the intake slice
    def review(self, sb, email_id, decision, actor_id):
        return self._act("review", sb, email_id, decision, actor_id)

    def continue_unauthorized(self, sb, email_id, actor_id):
        return self._act("continue", sb, email_id, actor_id)

    def dismiss(self, sb, email_id, actor_id):
        return self._act("dismiss", sb, email_id, actor_id)

    def set_method(self, sb, email_id, method, actor_id):
        return self._act("set_method", sb, email_id, method, actor_id)

    # the creation slice (docs/RFP_CREATE.md 8)
    def create_project(self, sb, email_id, actor_id):
        from app.services import rfp_create

        self.calls.append(("create", email_id, actor_id))
        if self.raise_create is not None:
            raise self.raise_create
        if self.return_create is not None:
            return self.return_create
        return rfp_create.Created(project_id=P3, number="26.9.7204", linked=False, files_job=True)

    def set_project_name(self, sb, email_id, name, actor_id):
        self.calls.append(("set_name", email_id, name, actor_id))
        if self.raise_value:
            raise ValueError("A project name is required.")
        if self.raise_lookup:
            raise LookupError("This email is not waiting for a project name.")
        return {"id": email_id, "status": "match"}

    def rescan_after_rule_added(self, sb, rule):
        self.calls.append(("rescan", rule.get("kind"), rule.get("value")))
        return self.rescan

    # the blocked-sender slice (doc 3.1)
    BlockDuplicate = _BlockDuplicate

    def add_block(self, sb, *, kind, value, reason, actor_id, locked=False):
        self.calls.append(("add_block", kind, value, reason, actor_id))
        if self.raise_duplicate:
            raise _BlockDuplicate(f"{value} is already blocked.")
        row = {
            "id": "block-new", "kind": kind, "value": value, "reason": reason,
            "locked": locked, "created_by": actor_id,
            "created_at": "2026-09-22T00:00:09+00:00",
        }
        sb.tables.setdefault("rfp_blocked_senders", []).append(row)
        return dict(row), self.parked

    def remove_block(self, sb, block, actor_id):
        self.calls.append(("remove_block", block.get("id"), actor_id))
        sb.tables["rfp_blocked_senders"] = [
            r for r in sb.tables.get("rfp_blocked_senders", []) if r["id"] != block["id"]
        ]

    def block_email_sender(self, sb, email_id, *, kind, value, reason, actor_id):
        self.calls.append(("block_email_sender", email_id, kind, value, actor_id))
        if self.raise_duplicate:
            raise _BlockDuplicate(f"{value} is already blocked.")
        return self.add_block(sb, kind=kind, value=value, reason=reason, actor_id=actor_id)

    # the matching slice (docs/RFP_MATCHING.md section 12)
    def merge_email(self, sb, email_id, project_id, gc_id, actor_id, *, bundle=None):
        return self._match_act("merge", sb, email_id, project_id, gc_id, actor_id)

    def duplicate_email(self, sb, email_id, project_id, actor_id):
        return self._match_act("duplicate", sb, email_id, project_id, actor_id)

    def reject_match(self, sb, email_id, actor_id):
        return self._match_act("reject", sb, email_id, actor_id)

    def set_match_gc(self, sb, email_id, gc_id, actor_id):
        return self._match_act("set_gc", sb, email_id, gc_id, actor_id)

    def reopen_match(self, sb, email_id, reason, actor_id):
        return self._match_act("reopen", sb, email_id, reason, actor_id)

    def unmerge(self, sb, match_id, reason, actor_id):
        self.calls.append(("unmerge", match_id, reason, actor_id))
        if self.raise_match is not None:
            raise self.raise_match
        return {"id": match_id, "unmerged_at": "2026-09-11T00:00:00+00:00"}

    def latest_match_for_email(self, sb, email_id):
        self.calls.append(("latest_match", email_id))
        return copy.deepcopy(self.latest_match)

    def excluded_projects_for_email(self, sb, row):
        self.calls.append(("excluded", row.get("id")))
        return copy.deepcopy(self.excluded)

    def match_stats(self, sb, mailboxes=None):
        self.calls.append(("match_stats", mailboxes))
        return dict(self.stats)


def _redact(obj, role):
    """The documented behaviour of rfp_match.redact_candidates (doc section
    8): deep, drops the `actual` date kind, nulls a date sub-score taken from
    the actual date (its total falls back to name + notes_bonus and every
    enclosing score / match_score is nulled) and any actual_bid_at value.
    Viewer roles get the object back untouched."""
    if role in ACTUAL_BID_VIEWER_ROLES:
        return obj
    return _redact_walk(copy.deepcopy(obj))[0]


def _redact_walk(node):
    """(node, hit): hit is True when a breakdown under node came from the
    actual date."""
    if isinstance(node, list):
        hit = False
        for i, item in enumerate(node):
            node[i], h = _redact_walk(item)
            hit = hit or h
        return node, hit
    if isinstance(node, dict):
        hit = False
        if node.get("closest_kind") == "actual":
            node["date"] = None
            node["closest_kind"] = None
            node["exact_time"] = False
            if "total" in node:
                node["total"] = round(
                    min(float(node.get("name") or 0) + float(node.get("notes_bonus") or 0), 1.0), 4
                )
            hit = True
        if isinstance(node.get("dates_used"), list):
            node["dates_used"] = [k for k in node["dates_used"] if k != "actual"]
        for k, v in list(node.items()):
            if k == "actual_bid_at":
                node[k] = None
            else:
                node[k], h = _redact_walk(v)
                hit = hit or h
        if hit:
            for k in ("score", "match_score"):
                if k in node:
                    node[k] = None
        return node, hit
    return node, False


class _RfpMatch:
    """Stand-in for app/services/rfp_match: only what the router reaches."""

    def __init__(self):
        self.redact_calls = []

    def redact_candidates(self, obj, role):
        self.redact_calls.append(role)
        return _redact(obj, role)


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(rr, "get_supabase", lambda: fake)
    monkeypatch.setattr(ref, "get_supabase", lambda: fake)
    monkeypatch.setattr(notifications, "get_supabase", lambda: fake)
    # The detail route's harvest block reads the queue (poll_info) through
    # llm_queue's own client; without this the tests would query the real
    # llm_jobs table over the network.
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: fake)
    monkeypatch.setattr(harvest_svc, "get_supabase", lambda: fake)
    return fake


@pytest.fixture()
def ingest(monkeypatch):
    stub = _Ingest()
    monkeypatch.setattr(rr, "_ingest", lambda: stub)
    return stub


@pytest.fixture()
def match_mod(monkeypatch):
    stub = _RfpMatch()
    monkeypatch.setattr(rr, "_match", lambda: stub)
    return stub


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _email(eid, *, status="review_llm", received_at="2026-09-09T10:00:00+00:00", **over):
    row = {
        "id": eid,
        "internet_message_id": f"msg-{eid}",
        "primary_mailbox": "bids@g3electrical.com",
        # 0134: the denormalized sightings every read is scoped against.
        "mailboxes": ["bids@g3electrical.com"],
        "from_address": "pm@examplegc.com",
        "from_name": "Example GC",
        "subject": "Invitation to bid",
        "body_text": "Please bid this project.",
        "received_at": received_at,
        "has_attachments": True,
        "attachments_meta": [{"name": "plans.pdf", "contentType": "application/pdf", "size": 12}],
        "keyword_hits": ["bid", "project"],
        "llm_answer": "undetermined",
        "llm_confidence": 0.5,
        "llm_reasoning": "Unclear whether this is an invitation.",
        "status": status,
        "flag_reason": None,
        "invitation_method": None,
        "authorization_rule_id": None,
        "auth_verdict": "pass",
        "extracted_project_name": None,
        "extracted_gc_name": None,
        "match_candidates": [],
        "match_project_id": None,
        "match_score": None,
        "resolved_gc_id": None,
        "possible_rebid_project_id": None,
        "possible_rebid_score": None,
        "match_review_decision": None,
        "excluded_project_ids": [],
    }
    row.update(over)
    return row


def _breakdown(closest_kind="actual", dates_used=("actual", "needs_by")):
    return {
        "name": 0.91,
        "date": 1.0,
        "notes": None,
        "notes_bonus": 0.0,
        "exact_time": False,
        "total": 0.93,
        "conflict": None,
        "dates_used": list(dates_used),
        "closest_kind": closest_kind,
    }


def _candidates():
    return [
        {
            "project_id": P1,
            "name": "Sunrise Elem (stored name)",
            "number": "26.9.7301",
            "breakdown": _breakdown("actual"),
            "verdict": "same",
            "confidence": 0.9,
            "reasoning": "Same school.",
        },
        {
            "project_id": P2,
            "name": "Fire Station 7",
            "number": "26.9.7302",
            "breakdown": _breakdown("internal", ("internal",)),
            "verdict": None,
            "confidence": None,
            "reasoning": None,
        },
    ]


def _seed(db):
    db.tables["rfp_emails"] = [
        _email(E1, status="review_llm", received_at="2026-09-09T09:00:00+00:00"),
        _email(E2, status="flagged_unauthorized", received_at="2026-09-09T11:00:00+00:00"),
        _email(E3, status="flagged_llm_no", received_at="2026-09-09T08:00:00+00:00"),
        _email(E4, status="done", received_at="2026-09-09T07:00:00+00:00", invitation_method="organic"),
        _email(
            E5,
            status="review_match",
            received_at="2026-09-09T12:00:00+00:00",
            flag_reason="match_confident",
            extracted_project_name="Sunrise Elementary Modernization",
            extracted_gc_name="Example GC",
            match_candidates=_candidates(),
            match_project_id=P1,
            match_score=0.93,
            resolved_gc_id=G1,
            possible_rebid_project_id=P3,
            possible_rebid_score=0.88,
            excluded_project_ids=[P2],
        ),
        _email(E6, status="merged", received_at="2026-09-09T06:00:00+00:00", match_project_id=P1),
        _email(MISSING.replace("ff", "fe"), status="rejected_by_review"),
    ]
    db.tables["rfp_email_sightings"] = [
        {"rfp_email_id": E1, "mailbox": "tmoore@g3electrical.com"},
        {"rfp_email_id": E1, "mailbox": "bids@g3electrical.com"},
        {"rfp_email_id": E2, "mailbox": "bids@g3electrical.com"},
    ]
    db.tables["rfp_authorized_senders"] = [
        {
            "id": RULE_LOCKED,
            "kind": "domain",
            "value": "procoretech.com",
            "method": "procore",
            "locked": True,
            "created_by": None,
            "created_at": "2026-09-09T00:00:00+00:00",
        },
        {
            "id": RULE_OPEN,
            "kind": "address",
            "value": "pm@examplegc.com",
            "method": "general",
            "locked": False,
            "created_by": "u9",
            "created_at": "2026-09-09T00:00:01+00:00",
        },
    ]
    db.tables["rfp_blocked_senders"] = [
        {
            "id": BLOCK_LOCKED,
            "kind": "domain",
            "value": "buildingconnected.com",
            "reason": "Platform seed",
            "locked": True,
            "created_by": None,
            "created_at": "2026-09-22T00:00:00+00:00",
        },
        {
            "id": BLOCK_OPEN,
            "kind": "address",
            "value": "noreply@spam.example",
            "reason": "Newsletter",
            "locked": False,
            "created_by": "u9",
            "created_at": "2026-09-22T00:00:01+00:00",
        },
    ]
    db.tables["profiles"] = [
        {"id": "u9", "full_name": "Dana Ruiz"},
        {"id": "u7", "full_name": "Sam Exec"},
    ]
    db.tables["projects"] = [
        {"id": P1, "name": "Sunrise Elementary Modernization", "number": "26.9.7301",
         "actual_bid_at": "2026-09-30T21:00:00+00:00"},
        {"id": P2, "name": "Fire Station 7", "number": "26.9.7302",
         "actual_bid_at": "2026-10-02T21:00:00+00:00"},
        {"id": P3, "name": "Sunrise Elementary Modernization (2025)", "number": "25.9.6801",
         "actual_bid_at": "2025-09-30T21:00:00+00:00"},
    ]
    db.tables["general_contractors"] = [{"id": G1, "name": "Example GC"}]
    db.tables["project_gcs"] = [{"id": "pg1", "project_id": P1, "gc_id": G1}]
    db.tables["rfp_project_matches"] = [
        # E6: an older duplicate, then the merge (the latest merged row), then
        # a newer duplicate on the same email: unmerge-by-email must pick M2.
        {"id": M1, "rfp_email_id": E6, "project_id": P2, "gc_id": G1, "kind": "duplicate",
         "decided_at": "2026-09-09T06:10:00+00:00", "unmerged_at": "2026-09-09T06:20:00+00:00"},
        {"id": M2, "rfp_email_id": E6, "project_id": P1, "gc_id": G1, "kind": "merged",
         "decided_at": "2026-09-09T06:30:00+00:00", "unmerged_at": None},
        {"id": M3, "rfp_email_id": E6, "project_id": P1, "gc_id": G1, "kind": "duplicate",
         "decided_at": "2026-09-09T06:40:00+00:00", "unmerged_at": None},
    ]
    return db


# ── Route table, flag gate and role wiring ───────────────────────────────


def _route_pairs():
    pairs = set()
    for route in rr.router.routes:
        for method in route.methods:
            pairs.add((method, route.path))
    return pairs


def test_route_table_is_exactly_the_spec():
    assert _route_pairs() == {
        ("GET", "/rfp-emails"),
        ("GET", "/rfp-emails/counts"),
        ("GET", "/rfp-emails/match-stats"),
        ("GET", "/rfp-emails/harvest-status"),
        ("POST", "/rfp-emails/{email_id}/harvest"),
        ("POST", "/rfp-emails/{email_id}/create"),
        ("POST", "/rfp-emails/{email_id}/set-name"),
        ("GET", "/rfp-emails/authorized-senders"),
        ("POST", "/rfp-emails/authorized-senders"),
        ("DELETE", "/rfp-emails/authorized-senders/{rule_id}"),
        ("GET", "/rfp-emails/blocked-senders"),
        ("POST", "/rfp-emails/blocked-senders"),
        ("DELETE", "/rfp-emails/blocked-senders/{block_id}"),
        ("POST", "/rfp-emails/{email_id}/block-sender"),
        ("GET", "/rfp-emails/{email_id}"),
        ("POST", "/rfp-emails/{email_id}/review"),
        ("POST", "/rfp-emails/{email_id}/continue"),
        ("POST", "/rfp-emails/{email_id}/dismiss"),
        ("PATCH", "/rfp-emails/{email_id}"),
        ("POST", "/rfp-emails/{email_id}/match/merge"),
        ("POST", "/rfp-emails/{email_id}/match/duplicate"),
        ("POST", "/rfp-emails/{email_id}/match/reject"),
        ("POST", "/rfp-emails/{email_id}/match/gc"),
        ("POST", "/rfp-emails/{email_id}/match/reopen"),
        ("POST", "/rfp-emails/{email_id}/match/unmerge"),
    }


def test_literal_paths_are_registered_before_the_parameterised_one():
    paths = [r.path for r in rr.router.routes]
    param = paths.index("/rfp-emails/{email_id}")
    assert paths.index("/rfp-emails/counts") < param
    assert paths.index("/rfp-emails/match-stats") < param
    assert paths.index("/rfp-emails/authorized-senders") < param
    assert paths.index("/rfp-emails/blocked-senders") < param


def test_every_route_is_flag_gated_and_rate_limited():
    for route in rr.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert rr.require_rfp_emails in calls, route.path
        assert rr.rfp_emails_rate_limit in calls, route.path


def test_flag_gate_is_fail_closed_and_404s_when_off(monkeypatch):
    monkeypatch.setattr(rr, "get_settings", lambda: SimpleNamespace())
    # No such setting yet: the gate must fail CLOSED, never open.
    assert rr.rfp_emails_enabled() is False
    with pytest.raises(HTTPException) as exc:
        rr.require_rfp_emails()
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not Found"

    monkeypatch.setattr(
        rr, "get_settings", lambda: SimpleNamespace(rfp_email_ingestion_enabled=True)
    )
    assert rr.rfp_emails_enabled() is True
    assert rr.require_rfp_emails() is None


# The reads, open to RFP_VIEW_ROLES (the review roles plus the read-only
# accountant) because 0134 scopes every one of them to the viewer's own
# mailboxes; docs/RFP_EMAIL_VISIBILITY.md 3.3.
VIEW_ROUTES = {
    "/rfp-emails",
    "/rfp-emails/counts",
    "/rfp-emails/match-stats",
    "/rfp-emails/{email_id}",
}
REVIEW_ROUTES = {
    "/rfp-emails/{email_id}/harvest",
    "/rfp-emails/{email_id}/create",
    "/rfp-emails/{email_id}/set-name",
    "/rfp-emails/{email_id}/review",
    "/rfp-emails/{email_id}/continue",
    "/rfp-emails/{email_id}/dismiss",
    "/rfp-emails/{email_id}/match/merge",
    "/rfp-emails/{email_id}/match/duplicate",
    "/rfp-emails/{email_id}/match/reject",
    "/rfp-emails/{email_id}/match/gc",
    "/rfp-emails/{email_id}/match/reopen",
}
MANAGEMENT_ROUTES = {
    "/rfp-emails/authorized-senders",
    "/rfp-emails/authorized-senders/{rule_id}",
    "/rfp-emails/harvest-status",
}
UNMERGE_ROUTES = {"/rfp-emails/{email_id}/match/unmerge"}
# Blocking a sender: BLOCK_ADMIN_ROLES plus any dev account (doc 3.1).
BLOCK_ROUTES = {
    "/rfp-emails/blocked-senders",
    "/rfp-emails/blocked-senders/{block_id}",
    "/rfp-emails/{email_id}/block-sender",
}
ROLE_DEPS = {
    rr.require_review_queue,
    rr.require_view_queue,
    rr.require_sender_admin,
    rr.require_unmerge,
    rr.require_block_admin,
}


def test_each_route_carries_exactly_one_role_dependency():
    for route in rr.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        # PATCH /{email_id} shares its path with the GET detail route but is an
        # ACTION, so the read gate must not reach it: key on the method too.
        reads_only = route.path in VIEW_ROUTES and route.methods == {"GET"}
        if route.path in MANAGEMENT_ROUTES:
            expected = rr.require_sender_admin
        elif route.path in UNMERGE_ROUTES:
            expected = rr.require_unmerge
        elif route.path in BLOCK_ROUTES:
            expected = rr.require_block_admin
        elif reads_only:
            expected = rr.require_view_queue
        else:
            assert route.path in REVIEW_ROUTES or route.path in VIEW_ROUTES, route.path
            expected = rr.require_review_queue
        assert calls & ROLE_DEPS == {expected}, (route.methods, route.path)


def test_review_queue_roles_are_the_shared_tuple():
    """The router keeps REVIEW_QUEUE_ROLES as an alias of app.core.roles'
    RFP_REVIEW_ROLES, which the projects router gates its email block on."""
    assert rr.REVIEW_QUEUE_ROLES is RFP_REVIEW_ROLES
    assert set(RFP_REVIEW_ROLES) == {
        Role.ESTIMATING_ADMIN,
        Role.IT_ADMIN,
        Role.EXECUTIVE,
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
    }


@pytest.mark.parametrize(
    "role",
    [
        Role.ESTIMATING_ADMIN,
        Role.IT_ADMIN,
        Role.EXECUTIVE,
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
    ],
)
def test_review_queue_admits_the_five_queue_roles(role):
    user = _user(role)
    assert asyncio.run(rr.require_review_queue(user=user)) is user


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.ESTIMATOR])
def test_review_queue_blocks_accountant_and_estimator(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_review_queue(user=_user(role)))
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN]
)
def test_rule_management_admits_its_three_roles(role):
    user = _user(role)
    assert asyncio.run(rr.require_sender_admin(user=user)) is user


@pytest.mark.parametrize(
    "role",
    [
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
        Role.ACCOUNTANT,
        Role.ESTIMATOR,
    ],
)
def test_rule_management_blocks_engineers_accountant_and_estimator(role):
    """An engineer may work the queue but may not change who the pipeline
    trusts: the two role sets are deliberately different."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_sender_admin(user=_user(role)))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN])
def test_unmerge_admits_its_three_roles(role):
    user = _user(role)
    assert asyncio.run(rr.require_unmerge(user=user)) is user


@pytest.mark.parametrize(
    "role",
    [
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
        Role.ACCOUNTANT,
        Role.ESTIMATOR,
    ],
)
def test_unmerge_blocks_engineers_accountant_and_estimator(role):
    """Unmerge detaches a GC the system put on a project (doc 3.7): the
    engineers who work the queue are not in that set."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_unmerge(user=_user(role)))
    assert exc.value.status_code == 403


# ── Lists and counts ─────────────────────────────────────────────────────


def test_tabs_map_to_the_documented_statuses():
    assert list(rr.TAB_STATUSES) == ["review", "matches", "unauthorized", "flagged", "processed"]
    assert rr.TAB_STATUSES["review"] == ("review_llm",)
    assert rr.TAB_STATUSES["matches"] == ("review_match",)
    assert rr.TAB_STATUSES["unauthorized"] == ("flagged_unauthorized",)
    assert set(rr.TAB_STATUSES["flagged"]) == {
        "flagged_auth",
        "flagged_no_keywords",
        "flagged_llm_no",
        "rejected_by_review",
        "blocked_sender",
        "failed",
    }
    assert set(rr.TAB_STATUSES["processed"]) == {
        "done", "merged", "duplicate", "harvest", "split", "create", "created"
    }


def test_the_tab_literal_and_the_status_map_agree():
    from typing import get_args

    assert set(get_args(rr.Tab)) == set(rr.TAB_STATUSES)


def test_list_returns_the_tab_newest_first_with_mailboxes(db):
    _seed(db)
    out = rr.list_rfp_emails(tab="review", limit=50, offset=0, user=_user())
    assert out["total"] == 1 and out["offset"] == 0 and out["limit"] == 50
    row = out["items"][0]
    assert row["id"] == E1
    assert row["mailboxes"] == ["bids@g3electrical.com", "tmoore@g3electrical.com"]
    # The body, the attachment listing, the raw auth header and the candidate
    # list stay out of the list.
    assert "body_text" not in row and "attachments_meta" not in row
    assert "match_candidates" not in row
    assert set(row) >= {
        "id",
        "received_at",
        "from_address",
        "from_name",
        "subject",
        "keyword_hits",
        "llm_answer",
        "llm_confidence",
        "llm_reasoning",
        "status",
        "flag_reason",
        "invitation_method",
        "has_attachments",
        "primary_mailbox",
        "mailboxes",
        "extracted_project_name",
        "extracted_gc_name",
        "match_project_id",
        "match_score",
        "resolved_gc_id",
        "possible_rebid_project_id",
        "match_project",
        "resolved_gc",
    }
    # A row that never reached match carries nulls, not missing keys.
    assert row["match_project"] is None and row["resolved_gc"] is None


def test_list_select_names_the_six_match_columns_and_never_the_candidates():
    cols = {c.strip() for c in rr._LIST_SELECT.split(",")}
    assert cols >= {
        "extracted_project_name",
        "extracted_gc_name",
        "match_project_id",
        "match_score",
        "resolved_gc_id",
        "possible_rebid_project_id",
    }
    assert "match_candidates" not in cols and "body_text" not in cols


def test_matches_tab_attaches_the_match_project_and_the_resolved_gc(db):
    _seed(db)
    out = rr.list_rfp_emails(tab="matches", limit=50, offset=0, user=_user())
    assert [r["id"] for r in out["items"]] == [E5]
    row = out["items"][0]
    assert row["match_project"] == {
        "id": P1, "name": "Sunrise Elementary Modernization", "number": "26.9.7301"
    }
    assert row["resolved_gc"] == {"id": G1, "name": "Example GC"}
    assert row["match_score"] == 0.93
    # The Processed tab renders the human decision under the badge, so every
    # list row carries the column (null until reviewed).
    assert "match_review_decision" in row and row["match_review_decision"] is None
    # Only the three reference columns are ever read for a list row's
    # project: no date can ride along.
    project_selects = [sel for table, sel in db.selects if table == "projects"]
    assert project_selects and all(
        {c.strip() for c in sel.split(",")} == {"id", "name", "number"} for sel in project_selects
    )


def test_processed_tab_holds_done_merged_and_duplicate(db):
    _seed(db)
    out = rr.list_rfp_emails(tab="processed", limit=50, offset=0, user=_user())
    assert {r["status"] for r in out["items"]} == {"done", "merged"}
    assert [r["id"] for r in out["items"]] == [E4, E6]  # newest received first


def test_flagged_tab_gathers_every_terminal_refusal(db):
    _seed(db)
    out = rr.list_rfp_emails(tab="flagged", limit=50, offset=0, user=_user())
    assert {r["status"] for r in out["items"]} == {"flagged_llm_no", "rejected_by_review"}


def test_counts_answer_all_five_buckets(db):
    _seed(db)
    assert rr.rfp_email_counts(user=_user()) == {
        "review": 1,
        "matches": 1,
        "unauthorized": 1,
        "flagged": 2,
        "processed": 2,
    }


def test_match_stats_answers_the_services_tally(db, ingest, monkeypatch):
    monkeypatch.setattr(
        rr, "get_settings", lambda: SimpleNamespace(rfp_match_auto_merge_enabled=False)
    )
    assert rr.rfp_match_stats(user=_user()) == {
        "confident": {"pending": 2, "agreed": 5, "disagreed": 1},
        "auto_merge_enabled": False,
    }
    assert ingest.calls == [("match_stats", {"bids@g3electrical.com"})]


def test_match_stats_reports_the_auto_merge_switch(db, ingest, monkeypatch):
    """The Matches tab header reads the switch's position off this route, so
    it follows the setting, never a constant."""
    monkeypatch.setattr(
        rr, "get_settings", lambda: SimpleNamespace(rfp_match_auto_merge_enabled=True)
    )
    assert rr.rfp_match_stats(user=_user())["auto_merge_enabled"] is True
    monkeypatch.setattr(
        rr, "get_settings", lambda: SimpleNamespace(rfp_match_auto_merge_enabled=None)
    )
    assert rr.rfp_match_stats(user=_user())["auto_merge_enabled"] is False


# ── Detail ───────────────────────────────────────────────────────────────


def test_detail_carries_body_mailboxes_and_the_rule(db, ingest):
    _seed(db)
    db.tables["rfp_emails"][3]["authorization_rule_id"] = RULE_OPEN
    out = rr.get_rfp_email(E4, user=_user())
    assert out["body_text"] == "Please bid this project."
    assert out["mailboxes"] == []
    assert out["authorization_rule"] == {
        "id": RULE_OPEN,
        "kind": "address",
        "value": "pm@examplegc.com",
        "method": "general",
        "locked": False,
    }
    # A row that never reached match still answers every match key, as null
    # or empty, so the drawer renders one shape.
    assert out["candidates"] == [] and out["match"] is None
    assert out["excluded_projects"] == [] and out["possible_rebid"] is None
    assert out["match_project"] is None and out["resolved_gc"] is None


def test_detail_rule_is_null_for_a_gc_domain_match(db, ingest):
    _seed(db)
    assert rr.get_rfp_email(E1, user=_user())["authorization_rule"] is None


def test_detail_404s_a_missing_row_and_a_malformed_id(db, ingest):
    _seed(db)
    for bad in (MISSING, NOT_A_UUID):
        with pytest.raises(HTTPException) as exc:
            rr.get_rfp_email(bad, user=_user())
        assert exc.value.status_code == 404
    assert ingest.calls == []


def test_detail_never_selects_star(db, ingest):
    """Doc section 7: the detail route reads an explicit column list, so a
    column a later migration adds is a deliberate addition, never a leak."""
    _seed(db)
    rr.get_rfp_email(E5, user=_user())
    # The queue's own poll read (llm_jobs) is not the router's: its row never
    # reaches the response, only poll_info's fixed keys do.
    assert all(sel.strip() != "*" for table, sel in db.selects if table != "llm_jobs"), db.selects
    email_selects = [sel for table, sel in db.selects if table == "rfp_emails"]
    cols = {c.strip() for c in email_selects[0].split(",")}
    # Every list column, the 0120 drawer columns and the 0122 match columns.
    assert cols >= {c.strip() for c in rr._LIST_SELECT.split(",")}
    assert cols >= {
        "body_text", "attachments_meta", "auth_raw", "auth_dmarc", "auth_compauth",
        "review_decision", "review_by", "review_at", "continued_by", "continued_at",
        "decided_at_step", "attempts", "next_attempt_at", "last_error",
        "extracted_bid_due_at", "extracted_bid_due_has_time", "extracted_bid_notes",
        "extract_model", "extract_prompt_version", "extracted_at", "sibling_of_email_id",
        "resolved_gc_contact_id", "gc_match_kind", "gc_match_score", "gc_candidates",
        "match_candidates", "match_llm_model", "match_llm_prompt_version",
        "match_weights", "matched_at", "match_review_decision", "match_review_by",
        "match_review_at", "match_review_agreed", "excluded_project_ids",
        "possible_rebid_score",
    }


def _latest_match():
    return {
        "id": M2,
        "rfp_email_id": E5,
        "project_id": P1,
        "gc_id": G1,
        "kind": "merged",
        "gc_added": True,
        "score": 0.93,
        "breakdown": {**_breakdown("actual"), "verdict": "same", "confidence": 0.9},
        "candidates": _candidates(),
        "weights": {"scorer_version": "rfp_match_scorer_v1"},
        "decided_by": "u7",
        "decided_at": "2026-09-09T13:00:00+00:00",
        "acknowledged_by": None,
        "unmerged_by": "u9",
        "unmerged_at": "2026-09-09T14:00:00+00:00",
        "unmerge_reason": "Wrong school",
    }


def test_detail_attaches_candidates_match_excluded_and_rebid_for_an_admin(db, ingest):
    _seed(db)
    ingest.latest_match = _latest_match()
    ingest.excluded = [
        {"id": P2, "name": "Fire Station 7", "number": "26.9.7302",
         "unmerge_reason": "Wrong school", "unmerged_by": "u9"}
    ]
    out = rr.get_rfp_email(E5, user=_user(Role.ESTIMATING_ADMIN))
    # Candidates: the stored list with names refreshed from the live project
    # row (the stored spelling is the fallback), breakdowns untouched.
    assert [c["project_id"] for c in out["candidates"]] == [P1, P2]
    assert out["candidates"][0]["name"] == "Sunrise Elementary Modernization"
    assert out["candidates"][0]["breakdown"]["closest_kind"] == "actual"
    assert out["candidates"][0]["breakdown"]["date"] == 1.0
    assert "actual" in out["candidates"][0]["breakdown"]["dates_used"]
    # The latest match record, with its project, GC and the people named.
    assert out["match"]["id"] == M2
    assert out["match"]["project"] == {
        "id": P1, "name": "Sunrise Elementary Modernization", "number": "26.9.7301"
    }
    assert out["match"]["gc"] == {"id": G1, "name": "Example GC"}
    assert out["match"]["decided_by_name"] == "Sam Exec"
    assert out["match"]["unmerged_by_name"] == "Dana Ruiz"
    assert out["match"]["acknowledged_by_name"] is None
    # Excluded projects carry who unmerged them: `unmerged_by` is the full
    # NAME (the drawer renders it as text), the id moves aside, and
    # `unmerged_at` is always present (null when the service had none).
    assert out["excluded_projects"] == [
        {"id": P2, "name": "Fire Station 7", "number": "26.9.7302",
         "unmerge_reason": "Wrong school", "unmerged_by": "Dana Ruiz",
         "unmerged_by_id": "u9", "unmerged_at": None}
    ]
    # The rebid hit is flat: {id, name, number, score}.
    assert out["possible_rebid"] == {
        "id": P3, "name": "Sunrise Elementary Modernization (2025)",
        "number": "25.9.6801", "score": 0.88,
    }
    assert out["match_project"]["id"] == P1 and out["resolved_gc"]["id"] == G1
    assert ("latest_match", E5) in ingest.calls and ("excluded", E5) in ingest.calls
    # Each candidate carries the GC ids on that project, from ONE project_gcs
    # query over the candidate ids (so the drawer can offer "Already on this
    # project" without a read per candidate).
    assert out["candidates"][0]["gc_ids"] == [G1]
    assert out["candidates"][1]["gc_ids"] == []
    assert [t for t, _ in db.selects].count("project_gcs") == 1
    # No candidate or project reference ever carries a date value.
    for cand in out["candidates"]:
        assert "actual_bid_at" not in cand and "internal_bid_at" not in cand
    assert "actual_bid_at" not in out["match"]["project"]


@pytest.mark.parametrize(
    "role", [Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR]
)
def test_an_engineers_detail_carries_no_actual_date_sub_score(db, ingest, match_mod, role):
    """Doc section 9 (router): for a row past match, an engineer's response
    contains no candidate actual_bid_at value and no date sub-score whose
    closest_kind was `actual`; the internal-date candidate keeps its score."""
    _seed(db)
    ingest.latest_match = _latest_match()
    out = rr.get_rfp_email(E5, user=_user(role))
    assert match_mod.redact_calls == [role]
    by_id = {c["project_id"]: c for c in out["candidates"]}
    assert by_id[P1]["breakdown"]["date"] is None
    assert by_id[P1]["breakdown"]["closest_kind"] is None
    assert "actual" not in by_id[P1]["breakdown"]["dates_used"]
    assert by_id[P2]["breakdown"]["date"] == 1.0
    assert by_id[P2]["breakdown"]["closest_kind"] == "internal"
    # The match record's own breakdown and candidate copy are redacted too.
    assert out["match"]["breakdown"]["date"] is None
    assert out["match"]["candidates"][0]["breakdown"]["date"] is None
    for cand in out["candidates"] + out["match"]["candidates"]:
        assert cand.get("actual_bid_at") is None
    # The row itself never carries an actual date either.
    assert out.get("actual_bid_at") is None


@pytest.mark.parametrize("role", sorted(ACTUAL_BID_VIEWER_ROLES & set(RFP_REVIEW_ROLES)))
def test_a_viewer_roles_detail_is_never_redacted(db, ingest, match_mod, role):
    _seed(db)
    ingest.latest_match = _latest_match()
    out = rr.get_rfp_email(E5, user=_user(role))
    assert match_mod.redact_calls == []
    assert out["candidates"][0]["breakdown"]["date"] == 1.0
    assert out["match"]["breakdown"]["closest_kind"] == "actual"


@pytest.mark.parametrize(
    "role", [Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR]
)
def test_an_engineer_never_receives_a_total_or_score_taken_from_the_actual_date(
    db, ingest, role
):
    """Doc section 8, through the REAL rfp_match module: the list row's
    match_score is null for a non-viewer role (the list cannot tell which
    candidate it came from), and in the detail the actual-date candidate's
    total is reduced to its name-only part while the row-level match_score
    and the match record's score are nulled. The internal-date candidate is
    untouched."""
    _seed(db)
    ingest.latest_match = _latest_match()
    listed = rr.list_rfp_emails(tab="matches", limit=50, offset=0, user=_user(role))
    (row,) = listed["items"]
    assert row["id"] == E5 and row["match_score"] is None
    assert row["match_project"]["id"] == P1   # the rest of the row is intact
    out = rr.get_rfp_email(E5, user=_user(role))
    by_id = {c["project_id"]: c for c in out["candidates"]}
    assert by_id[P1]["breakdown"]["total"] == 0.91   # name 0.91 + notes_bonus 0.0
    assert by_id[P1]["breakdown"]["date"] is None
    assert by_id[P2]["breakdown"]["total"] == 0.93
    assert out["match_score"] is None
    assert out["match"]["score"] is None
    assert out["match"]["breakdown"]["total"] == 0.91
    assert out["match"]["candidates"][0]["breakdown"]["total"] == 0.91


@pytest.mark.parametrize("role", sorted(ACTUAL_BID_VIEWER_ROLES & set(RFP_REVIEW_ROLES)))
def test_a_viewer_roles_list_and_detail_scores_are_unchanged(db, ingest, role):
    _seed(db)
    ingest.latest_match = _latest_match()
    listed = rr.list_rfp_emails(tab="matches", limit=50, offset=0, user=_user(role))
    assert listed["items"][0]["match_score"] == 0.93
    out = rr.get_rfp_email(E5, user=_user(role))
    assert out["match_score"] == 0.93 and out["match"]["score"] == 0.93
    assert out["candidates"][0]["breakdown"]["total"] == 0.93


# ── Actions ──────────────────────────────────────────────────────────────


def test_review_passes_the_decision_and_the_actor_to_the_service(db, ingest):
    _seed(db)
    out = rr.review_rfp_email(E1, rr.ReviewIn(decision="yes"), user=_user(uid="u7"))
    assert ("review", E1, "yes", "u7") in ingest.calls
    # The action answers the same shape as GET /rfp-emails/{id}.
    assert out["id"] == E1 and "mailboxes" in out and "authorization_rule" in out
    assert "candidates" in out and "match" in out


def test_continue_and_dismiss_reach_their_service_calls(db, ingest):
    _seed(db)
    rr.continue_rfp_email(E2, user=_user(uid="u7"))
    rr.dismiss_rfp_email(E2, user=_user(uid="u7"))
    acts = [c for c in ingest.calls if c[0] in ("continue", "dismiss")]
    assert acts == [("continue", E2, "u7"), ("dismiss", E2, "u7")]


def test_the_router_never_double_audits_an_action_the_service_records(db, ingest):
    """rfp_email_ingest audits review / continue / dismiss / set_method and
    every match action inside the conditional update that performs them. A
    second row from here would make one decision look like two in the log."""
    _seed(db)
    rr.review_rfp_email(E1, rr.ReviewIn(decision="yes"), user=_user())
    rr.continue_rfp_email(E2, user=_user())
    rr.dismiss_rfp_email(E2, user=_user())
    rr.patch_rfp_email(E4, rr.MethodPatchIn(invitation_method="procore"), user=_user())
    rr.merge_rfp_email_match(E5, RfpMatchMergeIn(project_id=P1), user=_user())
    rr.duplicate_rfp_email_match(E5, RfpMatchDuplicateIn(project_id=P1), user=_user())
    rr.reject_rfp_email_match(E5, user=_user())
    rr.set_rfp_email_match_gc(E5, RfpMatchGcIn(gc_id=G1), user=_user())
    rr.reopen_rfp_email_match(E4, RfpMatchReopenIn(reason="look again"), user=_user())
    rr.unmerge_rfp_email_match(E6, RfpMatchUnmergeIn(reason="wrong project"), user=_user())
    assert not db.tables.get("audit_log")


@pytest.mark.parametrize(
    "call",
    [
        lambda: rr.review_rfp_email(E1, rr.ReviewIn(decision="no"), user=_user()),
        lambda: rr.continue_rfp_email(E2, user=_user()),
        lambda: rr.dismiss_rfp_email(E2, user=_user()),
    ],
)
def test_a_row_that_moved_on_is_a_409(db, ingest, call):
    _seed(db)
    ingest.raise_lookup = True
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_EMAIL_NOT_ACTIONABLE
    assert not db.tables.get("audit_log")


def test_actions_404_a_missing_row_before_the_service_is_touched(db, ingest):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.dismiss_rfp_email(MISSING, user=_user())
    assert exc.value.status_code == 404
    assert ingest.calls == []


def test_patch_sets_the_invitation_method(db, ingest):
    _seed(db)
    out = rr.patch_rfp_email(
        E4, rr.MethodPatchIn(invitation_method="procore"), user=_user(uid="u7")
    )
    assert ("set_method", E4, "procore", "u7") in ingest.calls
    assert out["id"] == E4


def test_patch_refuses_a_method_outside_the_check_constraint():
    with pytest.raises(Exception):
        rr.MethodPatchIn(invitation_method="carrier_pigeon")


def test_the_patch_literal_matches_the_migrations_method_list():
    """The body Literal, the rule-form Literal, INVITATION_METHODS and the
    service vocabulary must not drift apart, or the router would let through
    a value the check constraint (0120, narrowed by 0124 and 0128, widened by
    0127 and 0129) refuses."""
    from typing import get_args

    field = rr.MethodPatchIn.model_fields["invitation_method"]
    assert set(get_args(field.annotation)) == set(rr.INVITATION_METHODS)
    assert set(rr.INVITATION_METHODS) == {
        "organic",
        "procore",
        "pipelinesuite",
        "gc_portal",
        "general",
        "nonorganic",
    }
    assert set(rr.INVITATION_METHODS) == set(auth_svc.INVITATION_METHODS)
    rule_field = rr.AuthorizedSenderIn.model_fields["method"]
    rule_literals = {a for a in get_args(get_args(rule_field.annotation)[0])}
    assert rule_literals == set(auth_svc.RULE_METHODS) == {
        "procore", "pipelinesuite", "gc_portal", "general"
    }
    assert rr.MethodPatchIn(invitation_method="gc_portal").invitation_method == "gc_portal"
    assert rr.MethodPatchIn(invitation_method="pipelinesuite").invitation_method == "pipelinesuite"
    assert (
        rr.AuthorizedSenderIn(kind="domain", value="cgandbinc.com", locked=True, method="pipelinesuite").method
        == "pipelinesuite"
    )
    for gone in ("buildingconnected", "ngem", "planhub"):
        with pytest.raises(Exception):
            rr.MethodPatchIn(invitation_method=gone)
        with pytest.raises(Exception):
            rr.AuthorizedSenderIn(kind="address", value="x@gc.example", locked=True, method=gone)


def test_migration_0124_drops_the_two_platforms_and_names_both_constraints():
    """0124 deletes the rows and seed rules for BuildingConnected and NGEM,
    replaces the unnamed 0120 checks with named ones carrying exactly the
    router's vocabulary, and is idempotent in the house style."""
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    sql = (migrations / "0124_rfp_drop_platform_methods.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0124 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "\u2014" not in sql and "\u2013" not in sql
    # The row purge matches the two domains on a label boundary.
    purge = re.search(r"delete from rfp_emails\s+where lower\(from_address\) ~ '([^']+)'", sql)
    assert purge
    pattern = re.compile(purge.group(1))
    for hit in ("team@buildingconnected.com", "nevada@customer.ionwave.net",
                "noreply@customer.ionwave.net"):
        assert pattern.search(hit), hit
    for miss in ("x@fakebuildingconnected.com", "x@ionwave.net.example.com", "pm@gc.example"):
        assert not pattern.search(miss), miss
    assert (
        "delete from rfp_authorized_senders where method in ('buildingconnected', 'ngem');"
        in sql
    )
    # Named constraints carrying the router's vocabulary exactly.
    email_check = re.search(
        r"add constraint rfp_emails_invitation_method_check\s+"
        r"check \(invitation_method in \(([^)]+)\)\)",
        sql,
    )
    assert email_check
    email_values = {v.strip().strip("'") for v in email_check.group(1).split(",")}
    # Frozen history: the sets 0124 wrote at the time, not the live vocabulary
    # (0127 added gc_portal, 0128 removed planhub).
    assert email_values == {"organic", "planhub", "procore", "general", "nonorganic"}
    rule_check = re.search(
        r"add constraint rfp_authorized_senders_method_check\s+check \(method in \(([^)]+)\)\)",
        sql,
    )
    assert rule_check
    rule_values = {v.strip().strip("'") for v in rule_check.group(1).split(",")}
    assert rule_values == {"planhub", "procore", "general"}
    # Both DO blocks look the old inline constraint up by column, so a re-run
    # drops the named one and re-adds it.
    assert sql.count("from pg_constraint c") == 2
    assert "a.attname = 'invitation_method'" in sql and "a.attname = 'method'" in sql


def test_migration_0127_adds_gc_portal_to_both_constraints():
    """0127 widens the two named checks by exactly gc_portal, touches no
    rows, and reuses the 0124 lookup-by-column pattern so it is idempotent
    and also lands on a database still carrying the inline 0120 check."""
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    sql = (migrations / "0127_rfp_gc_portal_method.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0127 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "\u2014" not in sql and "\u2013" not in sql
    assert "delete from" not in sql and "update " not in sql
    email_check = re.search(
        r"add constraint rfp_emails_invitation_method_check\s+"
        r"check \(invitation_method in \(([^)]+)\)\)",
        sql,
    )
    assert email_check
    email_values = {v.strip().strip("'") for v in email_check.group(1).split(",")}
    # Frozen history: the sets 0127 wrote at the time, not the live vocabulary
    # (0128 removed planhub afterwards).
    assert email_values == {"organic", "planhub", "procore", "gc_portal", "general", "nonorganic"}
    rule_check = re.search(
        r"add constraint rfp_authorized_senders_method_check\s+check \(method in \(([^)]+)\)\)",
        sql,
    )
    assert rule_check
    rule_values = {v.strip().strip("'") for v in rule_check.group(1).split(",")}
    assert rule_values == {"planhub", "procore", "gc_portal", "general"}
    assert sql.count("from pg_constraint c") == 2
    assert "a.attname = 'invitation_method'" in sql and "a.attname = 'method'" in sql
    # Only one 0127 file: prefixes must stay unique.
    assert len(list(migrations.glob("0127_*.sql"))) == 1


def test_migration_0128_drops_planhub_and_narrows_both_constraints():
    """0128 deletes the rows and seed rule for PlanHub the way 0124 did for
    BuildingConnected and NGEM, re-adds the two named checks carrying exactly
    the router's live vocabulary, and is idempotent in the house style."""
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    sql = (migrations / "0128_rfp_drop_planhub_method.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0128 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "\u2014" not in sql and "\u2013" not in sql
    # The row purge matches both PlanHub domains on a label boundary, and
    # also sweeps any row a person labelled planhub by hand from elsewhere.
    purge = re.search(r"delete from rfp_emails\s+where lower\(from_address\) ~ '([^']+)'", sql)
    assert purge
    pattern = re.compile(purge.group(1))
    for hit in ("noreply@message.planhub.com", "x@planhub.com", "bids@planhubprojects.com"):
        assert pattern.search(hit), hit
    for miss in ("x@fakeplanhub.com", "x@planhub.com.example.com", "pm@gc.example"):
        assert not pattern.search(miss), miss
    assert re.search(
        r"delete from rfp_emails\s+where lower\(from_address\) ~ '[^']+'\s+"
        r"or invitation_method = 'planhub';",
        sql,
    )
    assert "delete from rfp_authorized_senders where method in ('planhub');" in sql
    # Named constraints carrying the vocabulary as it stood after 0128.
    email_check = re.search(
        r"add constraint rfp_emails_invitation_method_check\s+"
        r"check \(invitation_method in \(([^)]+)\)\)",
        sql,
    )
    assert email_check
    email_values = {v.strip().strip("'") for v in email_check.group(1).split(",")}
    # Frozen history: the sets 0128 wrote at the time, not the live vocabulary
    # (0129 added pipelinesuite afterwards).
    assert email_values == {"organic", "procore", "gc_portal", "general", "nonorganic"}
    rule_check = re.search(
        r"add constraint rfp_authorized_senders_method_check\s+check \(method in \(([^)]+)\)\)",
        sql,
    )
    assert rule_check
    rule_values = {v.strip().strip("'") for v in rule_check.group(1).split(",")}
    assert rule_values == {"procore", "gc_portal", "general"}
    # Both DO blocks look the old constraint up by column, so a re-run drops
    # the named one and re-adds it.
    assert sql.count("from pg_constraint c") == 2
    assert "a.attname = 'invitation_method'" in sql and "a.attname = 'method'" in sql
    # Only one 0128 file: prefixes must stay unique.
    assert len(list(migrations.glob("0128_*.sql"))) == 1


def test_migration_0129_adds_pipelinesuite_and_seeds_two_locked_rules():
    """0129 widens the two named checks by exactly pipelinesuite (to the
    router's LIVE vocabulary), seeds the first two PipelineSuite portals as
    locked domain rules with an upsert on (kind, value), deletes nothing,
    and reuses the lookup-by-column pattern so it is idempotent
    (docs/RFP_PIPELINESUITE.md section 5)."""
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    sql = (migrations / "0129_rfp_pipelinesuite_method.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0129 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "—" not in sql and "–" not in sql
    # No row purge and no email-row rewrite: the only write is the seed.
    assert "delete from" not in sql
    assert not re.search(r"update rfp_emails", sql)
    # Named constraints carrying the router's LIVE vocabulary exactly.
    email_check = re.search(
        r"add constraint rfp_emails_invitation_method_check\s+"
        r"check \(invitation_method in \(([^)]+)\)\)",
        sql,
    )
    assert email_check
    email_values = {v.strip().strip("'") for v in email_check.group(1).split(",")}
    assert email_values == set(rr.INVITATION_METHODS)
    assert email_values == {"organic", "procore", "pipelinesuite", "gc_portal", "general", "nonorganic"}
    rule_check = re.search(
        r"add constraint rfp_authorized_senders_method_check\s+check \(method in \(([^)]+)\)\)",
        sql,
    )
    assert rule_check
    rule_values = {v.strip().strip("'") for v in rule_check.group(1).split(",")}
    assert rule_values == set(auth_svc.RULE_METHODS)
    assert rule_values == {"procore", "pipelinesuite", "gc_portal", "general"}
    # The two seeded portals: locked domain rules carrying the method, upserted
    # on the 0120 unique index so a hand-added rule is upgraded, not duplicated.
    seed = re.search(
        r"insert into rfp_authorized_senders \(kind, value, method, locked\) values\s+"
        r"(\(.+?\)(?:,\s*\(.+?\))*)\s+on conflict \(kind, value\) do update set "
        r"method = 'pipelinesuite', locked = true;",
        sql,
        re.S,
    )
    assert seed, "seed insert with the on conflict clause"
    rows = {
        tuple(v.strip().strip("'") for v in row.split(","))
        for row in re.findall(r"\(([^()]+)\)", seed.group(1))
    }
    assert rows == {
        ("domain", "cgandbinc.com", "pipelinesuite", "true"),
        ("domain", "shfcontracting.com", "pipelinesuite", "true"),
    }
    assert sql.count("on conflict (kind, value) do update") == 1
    # Both DO blocks look the old constraint up by column, so a re-run drops
    # the named one and re-adds it.
    assert sql.count("from pg_constraint c") == 2
    assert "a.attname = 'invitation_method'" in sql and "a.attname = 'method'" in sql
    assert sql.count("do $$") == 2 and sql.count("end\n$$;") == 2
    # No DDL on the two harvest columns that have no check constraint (0123).
    assert "alter table rfp_harvests" not in sql
    assert "alter table rfp_harvest_sessions" not in sql
    # The header names the release step (rescan the parked rows the seeded
    # domains now cover) and that function really exists.
    assert "rescan_after_rule_added" in sql
    from app.services import rfp_email_ingest

    assert callable(rfp_email_ingest.rescan_after_rule_added)
    # Only one 0129 file: prefixes must stay unique.
    assert len(list(migrations.glob("0129_*.sql"))) == 1


# ── Match actions (docs/RFP_MATCHING.md 3.8) ─────────────────────────────


def test_merge_passes_project_gc_and_actor_to_the_service(db, ingest):
    _seed(db)
    out = rr.merge_rfp_email_match(
        E5, RfpMatchMergeIn(project_id=P1, gc_id=G1), user=_user(uid="u7")
    )
    assert ("merge", E5, P1, G1, "u7") in ingest.calls
    assert out["id"] == E5 and "candidates" in out and "match" in out


def test_merge_without_a_gc_in_the_body_passes_none(db, ingest):
    _seed(db)
    rr.merge_rfp_email_match(E5, RfpMatchMergeIn(project_id=P1), user=_user(uid="u7"))
    assert ("merge", E5, P1, None, "u7") in ingest.calls


def test_duplicate_reject_gc_and_reopen_reach_their_service_calls(db, ingest):
    _seed(db)
    rr.duplicate_rfp_email_match(E5, RfpMatchDuplicateIn(project_id=P1), user=_user(uid="u7"))
    rr.reject_rfp_email_match(E5, user=_user(uid="u7"))
    rr.set_rfp_email_match_gc(E5, RfpMatchGcIn(gc_id=G1), user=_user(uid="u7"))
    rr.reopen_rfp_email_match(E4, RfpMatchReopenIn(reason="  look again "), user=_user(uid="u7"))
    rr.reopen_rfp_email_match(E4, None, user=_user(uid="u7"))
    acts = [c for c in ingest.calls if c[0] in ("duplicate", "reject", "set_gc", "reopen")]
    assert acts == [
        ("duplicate", E5, P1, "u7"),
        ("reject", E5, "u7"),
        ("set_gc", E5, G1, "u7"),
        ("reopen", E4, "look again", "u7"),
        ("reopen", E4, None, "u7"),
    ]


@pytest.mark.parametrize(
    "call",
    [
        lambda: rr.merge_rfp_email_match(E5, RfpMatchMergeIn(project_id=P2), user=_user()),
        lambda: rr.duplicate_rfp_email_match(E5, RfpMatchDuplicateIn(project_id=P2), user=_user()),
    ],
)
def test_merge_and_duplicate_refuse_an_excluded_project_with_409(db, ingest, call):
    """An earlier unmerge took the email away from P2; the service refuses
    both merge and duplicate into it (doc 3.6 step 1) and the router carries
    the service's code."""
    _seed(db)
    ingest.raise_match = _MatchErr(ErrorCode.RFP_MATCH_PROJECT_EXCLUDED, "That project was excluded.")
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_PROJECT_EXCLUDED
    assert exc.value.detail == "That project was excluded."
    assert not db.tables.get("audit_log")


@pytest.mark.parametrize(
    "code, http",
    [
        (ErrorCode.RFP_MATCH_GC_REQUIRED, 400),
        (ErrorCode.RFP_MATCH_GC_NOT_ON_PROJECT, 409),
        (ErrorCode.RFP_MATCH_PROJECT_CLOSED, 409),
        (ErrorCode.RFP_MATCH_NOT_ACTIONABLE, 409),
    ],
)
def test_match_refusals_map_their_code_to_the_documented_status(db, ingest, code, http):
    _seed(db)
    ingest.raise_match = _MatchErr(code, "no")
    with pytest.raises(HTTPException) as exc:
        rr.merge_rfp_email_match(E5, RfpMatchMergeIn(project_id=P1), user=_user())
    assert exc.value.status_code == http
    assert exc.value.headers["X-Error-Code"] == code


@pytest.mark.parametrize(
    "call",
    [
        lambda: rr.merge_rfp_email_match(E5, RfpMatchMergeIn(project_id=P1), user=_user()),
        lambda: rr.duplicate_rfp_email_match(E5, RfpMatchDuplicateIn(project_id=P1), user=_user()),
    ],
)
def test_merge_and_duplicate_map_a_send_in_flight_to_409_gc_already_sent(db, ingest, call):
    """The removal helper's ProposalSendError out of a merge or duplicate is
    the same 409 rfp_match_gc_already_sent the unmerge route answers, never
    an unhandled 500."""
    _seed(db)
    ingest.raise_match = ProposalSendError("A proposal has already been sent to this GC.")
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_GC_ALREADY_SENT
    assert exc.value.detail == "A proposal has already been sent to this GC."


def test_malformed_body_ids_are_404_before_the_service_is_touched(db, ingest):
    """A project_id or gc_id PostgREST could not cast would surface as a 500
    without CORS headers; the router refuses it first."""
    _seed(db)
    for call in (
        lambda: rr.merge_rfp_email_match(
            E5, RfpMatchMergeIn(project_id=NOT_A_UUID), user=_user()
        ),
        lambda: rr.merge_rfp_email_match(
            E5, RfpMatchMergeIn(project_id=P1, gc_id=NOT_A_UUID), user=_user()
        ),
        lambda: rr.duplicate_rfp_email_match(
            E5, RfpMatchDuplicateIn(project_id=NOT_A_UUID), user=_user()
        ),
        lambda: rr.set_rfp_email_match_gc(E5, RfpMatchGcIn(gc_id=NOT_A_UUID), user=_user()),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404
    assert ingest.calls == []


def test_an_unknown_body_gc_is_404_before_the_service_is_touched(db, ingest):
    _seed(db)
    for call in (
        lambda: rr.merge_rfp_email_match(
            E5, RfpMatchMergeIn(project_id=P1, gc_id=MISSING), user=_user()
        ),
        lambda: rr.set_rfp_email_match_gc(E5, RfpMatchGcIn(gc_id=MISSING), user=_user()),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404 and exc.value.detail == rr._GC_NOT_FOUND
    assert ingest.calls == []
    # A known GC goes through; a merge without a body gc_id never reads GCs.
    rr.merge_rfp_email_match(E5, RfpMatchMergeIn(project_id=P1, gc_id=G1), user=_user())
    rr.set_rfp_email_match_gc(E5, RfpMatchGcIn(gc_id=G1), user=_user())
    assert [c[0] for c in ingest.calls if c[0] in ("merge", "set_gc")] == ["merge", "set_gc"]


def test_a_bare_lookup_error_on_a_match_action_is_409_not_actionable(db, ingest):
    _seed(db)
    ingest.raise_match = LookupError("This email is not waiting for a match decision.")
    with pytest.raises(HTTPException) as exc:
        rr.reject_rfp_email_match(E5, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_NOT_ACTIONABLE
    assert exc.value.detail == "This email is not waiting for a match decision."


def test_match_actions_404_a_missing_row_before_the_service_is_touched(db, ingest):
    _seed(db)
    for call in (
        lambda: rr.merge_rfp_email_match(MISSING, RfpMatchMergeIn(project_id=P1), user=_user()),
        lambda: rr.duplicate_rfp_email_match(
            MISSING, RfpMatchDuplicateIn(project_id=P1), user=_user()
        ),
        lambda: rr.reject_rfp_email_match(NOT_A_UUID, user=_user()),
        lambda: rr.set_rfp_email_match_gc(MISSING, RfpMatchGcIn(gc_id=G1), user=_user()),
        lambda: rr.reopen_rfp_email_match(MISSING, None, user=_user()),
        lambda: rr.unmerge_rfp_email_match(
            MISSING, RfpMatchUnmergeIn(reason="wrong"), user=_user()
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404
    assert ingest.calls == []


def test_unmerge_by_email_resolves_the_latest_merged_row(db, ingest):
    """Doc 3.8: the email's latest kind = merged row, open or closed, via the
    (rfp_email_id, decided_at desc) order: duplicates on either side of it
    are skipped."""
    _seed(db)
    out = rr.unmerge_rfp_email_match(
        E6, RfpMatchUnmergeIn(reason="Wrong project"), user=_user(Role.EXECUTIVE, uid="u7")
    )
    assert ("unmerge", M2, "Wrong project", "u7") in ingest.calls
    assert out["id"] == E6


def test_unmerge_by_email_404s_when_the_email_was_never_merged(db, ingest):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.unmerge_rfp_email_match(E5, RfpMatchUnmergeIn(reason="Wrong project"), user=_user())
    assert exc.value.status_code == 404
    assert exc.value.detail == rr._NO_MERGE_TO_UNDO
    assert not [c for c in ingest.calls if c[0] == "unmerge"]


def test_unmerge_maps_a_send_in_flight_to_409_gc_already_sent(db, ingest):
    """The removal helper's ProposalSendError (a proposal to that GC is sent
    or sending): 409 rfp_match_gc_already_sent, nothing detached."""
    _seed(db)
    ingest.raise_match = ProposalSendError("A proposal has already been sent to this GC.")
    with pytest.raises(HTTPException) as exc:
        rr.unmerge_rfp_email_match(E6, RfpMatchUnmergeIn(reason="Wrong project"), user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_GC_ALREADY_SENT
    assert exc.value.detail == "A proposal has already been sent to this GC."


def test_unmerge_maps_the_services_own_code(db, ingest):
    _seed(db)
    ingest.raise_match = _MatchErr(ErrorCode.RFP_MATCH_NOT_ACTIONABLE, "Already unmerged.")
    with pytest.raises(HTTPException) as exc:
        rr.unmerge_rfp_email_match(E6, RfpMatchUnmergeIn(reason="Wrong project"), user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_NOT_ACTIONABLE


def test_the_unmerge_reason_is_required_and_trimmed():
    assert RfpMatchUnmergeIn(reason="  Wrong project ").reason == "Wrong project"
    for bad in ("", "  ", "ab", "  a  ", "x" * 501):
        with pytest.raises(Exception):
            RfpMatchUnmergeIn(reason=bad)


def test_the_reopen_reason_is_optional_and_blank_reads_as_none():
    assert RfpMatchReopenIn().reason is None
    assert RfpMatchReopenIn(reason="   ").reason is None
    assert RfpMatchReopenIn(reason=" why ").reason == "why"
    with pytest.raises(Exception):
        RfpMatchReopenIn(reason="x" * 501)


# ── Authorized senders ───────────────────────────────────────────────────


def test_rules_list_puts_locked_first_and_names_the_author(db):
    _seed(db)
    rows = rr.list_authorized_senders(_=_user(Role.EXECUTIVE))["items"]
    assert [r["value"] for r in rows] == ["procoretech.com", "pm@examplegc.com"]
    assert rows[0]["locked"] is True and rows[0]["created_by_name"] is None
    assert rows[1]["created_by_name"] == "Dana Ruiz"


def test_add_rule_normalizes_forces_general_and_reports_the_rescan(db, ingest):
    _seed(db)
    ingest.rescan = 3
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(kind="domain", value="  ExampleGC.COM "),
        user=_user(Role.ESTIMATING_ADMIN, uid="u7"),
    )
    out = {**res["item"], "rescanned": res["rescanned"]}
    assert out["kind"] == "domain" and out["value"] == "examplegc.com"
    assert out["method"] == "general" and out["locked"] is False
    assert out["created_by"] == "u7"
    assert out["rescanned"] == 3
    assert ("rescan", "domain", "examplegc.com") in ingest.calls
    entry = db.tables["audit_log"][0]
    assert entry["action"] == "rfp_authorized_sender.create"
    assert entry["payload"]["rescanned"] == 3


def test_a_non_locked_rule_can_never_carry_a_platform_method(db, ingest):
    """`method` is only meaningful on a locked platform rule (doc 3.8): a
    user-added rule is always 'general', whatever the body asks for."""
    _seed(db)
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(kind="domain", value="examplegc.com", method="procore"),
        user=_user(Role.EXECUTIVE),
    )
    out = {**res["item"], "rescanned": res["rescanned"]}
    assert out["method"] == "general"


def test_duplicate_rule_is_a_409(db, ingest):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.create_authorized_sender(
            rr.AuthorizedSenderIn(kind="address", value="PM@examplegc.com"),
            user=_user(Role.EXECUTIVE),
        )
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_RULE_DUPLICATE


def test_an_invalid_value_is_a_400_carrying_the_services_sentence(db, ingest):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.create_authorized_sender(
            rr.AuthorizedSenderIn(kind="domain", value="https://examplegc.com/bids"),
            user=_user(Role.EXECUTIVE),
        )
    assert exc.value.status_code == 400
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_RULE_INVALID
    assert "bare domain" in exc.value.detail
    assert len(db.tables["rfp_authorized_senders"]) == 2


@pytest.mark.parametrize("provider", ["gmail.com", "Outlook.com", "yahoo.com"])
def test_a_public_provider_domain_rule_is_a_400_that_says_why(db, ingest, provider):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.create_authorized_sender(
            rr.AuthorizedSenderIn(kind="domain", value=provider),
            user=_user(Role.EXECUTIVE),
        )
    assert exc.value.status_code == 400
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_RULE_PUBLIC_DOMAIN
    detail = exc.value.detail.lower()
    assert "public email provider" in detail or "public mailbox provider" in detail
    assert "address" in detail  # names the way out: add the exact address
    assert len(db.tables["rfp_authorized_senders"]) == 2
    assert not db.tables.get("audit_log")


def test_the_same_public_provider_address_is_allowed(db, ingest):
    """Only the DOMAIN is refused: an individual at gmail.com is a legitimate
    GC contact and their exact address may be authorized."""
    _seed(db)
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(kind="address", value="joe@gmail.com"),
        user=_user(Role.EXECUTIVE),
    )
    out = {**res["item"], "rescanned": res["rescanned"]}
    assert out["value"] == "joe@gmail.com" and out["kind"] == "address"


# ── Locked rules: IT Admin only ──────────────────────────────────────────


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN])
def test_only_the_it_admin_may_add_a_locked_rule(db, ingest, role):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.create_authorized_sender(
            rr.AuthorizedSenderIn(kind="domain", value="newplatform.com", locked=True),
            user=_user(role),
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY
    assert len(db.tables["rfp_authorized_senders"]) == 2


def test_the_it_admin_may_add_a_locked_rule_with_its_platform_method(db, ingest):
    _seed(db)
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(
            kind="domain", value="newplatform.com", locked=True, method="procore"
        ),
        user=_user(Role.IT_ADMIN),
    )
    out = {**res["item"], "rescanned": res["rescanned"]}
    assert out["locked"] is True and out["method"] == "procore"
    assert len(db.tables["rfp_authorized_senders"]) == 3


def test_the_it_admin_may_add_a_locked_gc_portal_rule_on_a_gc_domain(db, ingest):
    """A GC with its own bidding portal: a locked domain rule carrying
    gc_portal, the method the harvest step keys its scraper on."""
    _seed(db)
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(kind="domain", value="Portal.GC.example", locked=True, method="gc_portal"),
        user=_user(Role.IT_ADMIN),
    )
    assert res["item"]["locked"] is True
    assert res["item"]["method"] == "gc_portal"
    assert res["item"]["value"] == "portal.gc.example"
    # Not locked: the method is forced back to general like any user rule.
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(kind="domain", value="other.example", locked=False, method="gc_portal"),
        user=_user(Role.IT_ADMIN),
    )
    assert res["item"]["locked"] is False and res["item"]["method"] == "general"


def test_the_it_admin_may_add_a_locked_pipelinesuite_rule_on_a_gc_domain(db, ingest):
    """A GC whose plan room is a PipelineSuite portal: a locked domain rule
    on the GC's own domain carrying pipelinesuite, the method the harvest
    step keys the PipelineSuite harvester on (docs/RFP_PIPELINESUITE.md
    section 1; 0129 seeds the first two). The learn-back runs like for any
    rule."""
    _seed(db)
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(kind="domain", value="Amrize.COM", locked=True, method="pipelinesuite"),
        user=_user(Role.IT_ADMIN),
    )
    assert res["item"]["locked"] is True
    assert res["item"]["method"] == "pipelinesuite"
    assert res["item"]["value"] == "amrize.com"
    assert ("rescan", "domain", "amrize.com") in ingest.calls
    # Not locked: the method is forced back to general like any user rule.
    res = rr.create_authorized_sender(
        rr.AuthorizedSenderIn(kind="domain", value="amesco.com", locked=False, method="pipelinesuite"),
        user=_user(Role.IT_ADMIN),
    )
    assert res["item"]["locked"] is False and res["item"]["method"] == "general"
    # Locked rules are IT Admin only, pipelinesuite included.
    with pytest.raises(HTTPException) as exc:
        rr.create_authorized_sender(
            rr.AuthorizedSenderIn(kind="domain", value="another.example", locked=True, method="pipelinesuite"),
            user=_user(Role.ESTIMATING_ADMIN),
        )
    assert exc.value.status_code == 403
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN])
def test_only_the_it_admin_may_delete_a_locked_rule(db, role):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.delete_authorized_sender(RULE_LOCKED, user=_user(role))
    assert exc.value.status_code == 403
    assert exc.value.detail == ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY
    assert len(db.tables["rfp_authorized_senders"]) == 2


def test_the_it_admin_may_delete_a_locked_rule(db):
    _seed(db)
    assert rr.delete_authorized_sender(RULE_LOCKED, user=_user(Role.IT_ADMIN)) == {
        "id": RULE_LOCKED,
        "deleted": True,
    }
    assert [r["id"] for r in db.tables["rfp_authorized_senders"]] == [RULE_OPEN]
    assert db.tables["audit_log"][0]["action"] == "rfp_authorized_sender.delete"


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN])
def test_any_management_role_may_delete_an_unlocked_rule(db, role):
    _seed(db)
    rr.delete_authorized_sender(RULE_OPEN, user=_user(role))
    assert [r["id"] for r in db.tables["rfp_authorized_senders"]] == [RULE_LOCKED]


def test_delete_404s_a_missing_or_malformed_rule_id(db):
    _seed(db)
    for bad in (MISSING, NOT_A_UUID):
        with pytest.raises(HTTPException) as exc:
            rr.delete_authorized_sender(bad, user=_user(Role.IT_ADMIN))
        assert exc.value.status_code == 404


# ── GC contacts at a public mailbox provider ─────────────────────────────


def _contact_in(email):
    from app.models.schemas import GCContactIn

    return GCContactIn(gc_id="gc-1", name="Pat Lee", email=email)


def test_public_provider_gc_contact_is_saved_with_the_rfp_notice(db):
    out = ref.create_gc_contact(_contact_in("pat@gmail.com"), _=_user())
    # Saved, not refused: the row is in the table and the response is the contact.
    assert len(db.tables["gc_contacts"]) == 1
    assert out["email"] == "pat@gmail.com" and out["name"] == "Pat Lee"
    notice = out["rfp_notice"]
    assert "gmail.com" in notice
    assert "full email address" in notice
    assert "colleague" in notice


def test_company_domain_gc_contact_has_no_notice(db):
    out = ref.create_gc_contact(_contact_in("pat@examplegc.com"), _=_user())
    assert out["rfp_notice"] is None
    # Response shape otherwise unchanged.
    assert set(out) == set(db.tables["gc_contacts"][0]) | {"rfp_notice"}


def test_a_contact_without_an_email_has_no_notice(db):
    out = ref.create_gc_contact(_contact_in(None), _=_user())
    assert out["rfp_notice"] is None


def test_the_notice_is_declared_on_the_response_model():
    assert "rfp_notice" in ref.GCContactSaved.model_fields
    assert ref.GCContactSaved.model_fields["rfp_notice"].default is None


def test_the_notice_uses_the_services_public_provider_list():
    """The advisory and the authorization rule must agree on what counts as a
    public provider, or a contact could be told the wrong thing."""
    for domain in sorted(auth_svc.PUBLIC_MAILBOX_DOMAINS):
        assert ref._rfp_notice(f"pat@{domain}") is not None
    assert ref._rfp_notice("pat@g3electrical.com") is None


def test_the_services_are_reached_lazily():
    """The router must not import app/services/rfp_email_ingest or
    app/services/rfp_match at module import time: both are built alongside
    this router."""
    assert "app.services.rfp_email_ingest" not in sys.modules or callable(rr._ingest)
    assert "app.services.rfp_match" not in sys.modules or callable(rr._match)


# ── Project data harvest (docs/RFP_HARVEST.md section 5) ─────────────────

E7 = "3f1c0b00-0000-4000-8000-000000000007"  # done, procore, with a bid link
E8 = "3f1c0b00-0000-4000-8000-000000000008"  # done, pipelinesuite (added per test)
H1 = "8d000000-0000-4000-8000-000000000001"
SIGNED_URL = "https://storage.procore.com/api/v5/files/x.pdf?companyId=5662&sig=deadbeef"
PROCORE_BODY = (
    "Monument invites you to bid. View Bid Sheet "
    "<https://app.procore.com/5662/company/planroom/route_to_bid_sheet/64706611?from_email=1>"
)


class _Harvest:
    """Stand-in for app/services/rfp_harvest: only what the router reaches."""

    JOB_TYPE = "rfp_harvest"
    MANUAL_STATUSES = ("done", "merged", "duplicate", "harvest")

    def __init__(self):
        self.calls = []
        self.can = (True, None)
        self.avail = (True, None, None)
        self.row = None
        self.active = None

    def harvest_for_email(self, sb, row):
        self.calls.append(("for_email", row["id"]))
        return copy.deepcopy(self.row)

    def can_harvest(self, row):
        self.calls.append(("can", row["id"], row.get("status")))
        return self.can

    def availability(self):
        self.calls.append(("availability",))
        return self.avail

    def availability_for(self, row, settings=None):
        # The route asks for the row's own platform session (Procore's one,
        # or the PipelineSuite portal named in the body), never the global
        # Procore check.
        self.calls.append(("availability_for", row["id"], row.get("invitation_method")))
        return self.avail

    def enqueue(self, email_id, *, created_by, force=False):
        self.calls.append(("enqueue", email_id, created_by, force))
        if self.active is not None:
            raise llm_queue.JobAlreadyActive(self.active)
        return {"id": "job-1", "status": "queued", "payload": {"email_id": email_id, "force": force}}

    def session_status(self):
        self.calls.append(("session_status",))
        return {"configured": True, "account": "bot@example.com", "locked_until": None,
                "last_error": None, "active_jobs": 0}


@pytest.fixture()
def harvest_mod(monkeypatch):
    stub = _Harvest()
    monkeypatch.setattr(rr, "_harvest", lambda: stub)
    return stub


def _seed_harvest(db):
    _seed(db)
    db.tables["rfp_emails"].append(_email(
        E7, status="done", received_at="2026-09-09T05:00:00+00:00", invitation_method="procore",
        body_text=PROCORE_BODY, harvest_id=H1, harvested_at="2026-09-14T20:00:00+00:00",
    ))
    db.tables["rfp_harvests"] = [{
        "id": H1, "rfp_email_id": E7, "method": "procore", "external_key": "procore:5662:64706611",
        "external_url": "https://app.procore.com/5662/company/planroom/route_to_bid_sheet/64706611",
        "status": "complete", "claim_token": "secret-claim", "attempts": 1, "last_error": None,
        "data": {"platform": "procore", "project_name": "Warehouse HVAC Upgrade"},
        "raw": {"bid_package": {"project_logo_url": SIGNED_URL}},
        "description_text": "Replace evaporative coolers.", "instructions_text": None,
        "files": [{"file_path": "Bid_Drawings/Current/Electrical/E4.00.pdf", "size": 1, "kind": "drawing",
                   "discipline": "Electrical", "status": "accepted", "sandbox_file_id": "f1", "error": None}],
        "file_count": 1, "files_accepted": 1, "bytes_downloaded": 1, "sandbox_run_id": "run-1",
        "facts_at": "2026-09-14T19:58:00+00:00", "started_at": "2026-09-14T19:57:00+00:00",
        "finished_at": "2026-09-14T20:00:00+00:00", "created_at": "2026-09-14T19:57:00+00:00",
        "updated_at": "2026-09-14T20:00:00+00:00",
    }]
    return db


def test_harvest_route_answers_202_with_the_job_and_audits_the_run(db, harvest_mod):
    _seed_harvest(db)
    out = rr.harvest_rfp_email(E7, rr.HarvestIn(force=True), user=_user(uid="u3"))
    assert out == {"job": {"id": "job-1", "status": "queued"}}
    assert ("enqueue", E7, "u3", True) in harvest_mod.calls
    assert ("can", E7, "done") in harvest_mod.calls
    assert ("availability_for", E7, "procore") in harvest_mod.calls
    assert ("availability",) not in harvest_mod.calls
    audits = db.tables.get("audit_log") or []
    assert len(audits) == 1
    assert audits[0]["action"] == "rfp_harvest.run" and audits[0]["actor_id"] == "u3"
    assert audits[0]["entity"] == "rfp_email" and audits[0]["entity_id"] == E7
    assert audits[0]["payload"] == {"force": True}
    # No body means no refresh.
    rr.harvest_rfp_email(E7, None, user=_user())
    assert harvest_mod.calls[-1] == ("enqueue", E7, "u1", False)
    assert db.tables["audit_log"][-1]["payload"] == {"force": False}


def test_harvest_route_reads_only_what_it_needs(db, harvest_mod):
    _seed_harvest(db)
    rr.harvest_rfp_email(E7, None, user=_user())
    email_selects = [sel for table, sel in db.selects if table == "rfp_emails"]
    assert email_selects == [
        "id, status, invitation_method, body_text, harvest_id, attachments_meta, from_address, "
        "primary_mailbox, mailboxes"
    ]


def test_harvest_route_409s_when_a_job_is_already_active(db, harvest_mod):
    _seed_harvest(db)
    harvest_mod.active = {"id": "job-0", "status": "running"}
    with pytest.raises(HTTPException) as exc:
        rr.harvest_rfp_email(E7, None, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_HARVEST_ACTIVE == "rfp_harvest_active"
    assert not db.tables.get("audit_log")


@pytest.mark.parametrize(
    "arm",
    [
        ("no_link", None),
        ("method", None),
        ("status", E5),      # waiting at review_match: not a stage a harvest may run from
    ],
)
def test_harvest_route_409s_when_the_harvest_is_not_available(db, harvest_mod, arm):
    kind, target = arm
    _seed_harvest(db)
    if kind == "no_link":
        harvest_mod.can = (False, "The email carries no usable Procore bid link.")
    elif kind == "method":
        harvest_mod.can = (False, "No harvester exists for this invitation method.")
    with pytest.raises(HTTPException) as exc:
        rr.harvest_rfp_email(target or E7, None, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_HARVEST_NOT_AVAILABLE == "rfp_harvest_not_available"
    if kind == "status":
        assert exc.value.detail == "The email is not at a stage that can be harvested."
    else:
        assert exc.value.detail == harvest_mod.can[1]
    assert not any(c[0] == "enqueue" for c in harvest_mod.calls)
    assert not db.tables.get("audit_log")


def test_harvest_route_503s_while_logins_are_locked_with_the_unlock_time(db, harvest_mod):
    """The lock is the row's own platform session (`availability_for(row)`):
    Procore's one, or the PipelineSuite portal named in the body. The route
    passes the service's sentence through and appends the unlock time."""
    from datetime import datetime, timezone

    _seed_harvest(db)
    until = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc)
    harvest_mod.avail = (False, "Procore logins are locked after repeated failures.", until)
    with pytest.raises(HTTPException) as exc:
        rr.harvest_rfp_email(E7, None, user=_user())
    assert exc.value.status_code == 503
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_HARVEST_LOCKED == "rfp_harvest_locked"
    assert exc.value.detail == (
        "Procore logins are locked after repeated failures. Locked until 2026-09-15T02:00:00+00:00."
    )
    assert ("availability_for", E7, "procore") in harvest_mod.calls
    assert ("availability",) not in harvest_mod.calls
    # A PipelineSuite row: the same path, the portal's own sentence.
    db.tables["rfp_emails"].append(_email(
        E8, status="done", received_at="2026-09-16T05:00:00+00:00", invitation_method="pipelinesuite",
        body_text="Project ID: 377363\nSecurity Key: dummy-key\n",
    ))
    harvest_mod.avail = (
        False,
        "PipelineSuite login (cgandbinc.pipelinesuite.com) failed repeatedly; harvests for that portal are paused.",
        until,
    )
    with pytest.raises(HTTPException) as exc:
        rr.harvest_rfp_email(E8, None, user=_user())
    assert exc.value.status_code == 503
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_HARVEST_LOCKED
    assert exc.value.detail.startswith("PipelineSuite login (cgandbinc.pipelinesuite.com)")
    assert exc.value.detail.endswith("Locked until 2026-09-15T02:00:00+00:00.")
    assert ("availability_for", E8, "pipelinesuite") in harvest_mod.calls
    # No sentence from the service: a platform-neutral fallback.
    harvest_mod.avail = (False, None, None)
    with pytest.raises(HTTPException) as exc:
        rr.harvest_rfp_email(E7, None, user=_user())
    assert exc.value.detail == "The platform is not available."
    assert not any(c[0] == "enqueue" for c in harvest_mod.calls)


def test_harvest_route_404s_a_missing_or_malformed_id_before_the_service(db, harvest_mod):
    _seed_harvest(db)
    for bad in (MISSING, NOT_A_UUID):
        with pytest.raises(HTTPException) as exc:
            rr.harvest_rfp_email(bad, None, user=_user())
        assert exc.value.status_code == 404
    assert harvest_mod.calls == []


def test_harvest_in_defaults_force_off():
    assert rr.HarvestIn().force is False
    assert rr.HarvestIn(force=True).force is True


def test_the_detail_harvest_block_never_carries_raw_the_claim_token_or_a_signed_url(db, ingest):
    """Through the real rfp_harvest.harvest_for_email against a fake that
    honours column lists: the row comes back without raw and claim_token,
    and nothing in the response carries a signed storage URL."""
    _seed_harvest(db)
    db.tables["llm_jobs"] = [{
        "id": "job-9", "job_type": "rfp_harvest", "target_id": E7, "status": "succeeded",
        "attempts": 1, "max_attempts": 6, "created_at": "2026-09-14T19:56:00+00:00",
        "error_kind": None, "last_error": None, "next_attempt_at": None, "priority": 150,
    }]
    out = rr.get_rfp_email(E7, user=_user())
    block = out["harvest"]
    assert block["id"] == H1 and block["status"] == "complete"
    assert block["data"]["project_name"] == "Warehouse HVAC Upgrade"
    assert block["files"][0]["file_path"] == "Bid_Drawings/Current/Electrical/E4.00.pdf"
    assert "raw" not in block and "claim_token" not in block and "cookies" not in block
    dumped = repr(out)
    assert "sig=" not in dumped and "secret-claim" not in dumped and SIGNED_URL not in dumped
    assert out["harvest_job"] == {
        "state": "succeeded", "attempts": 1, "max_attempts": 6, "position": None,
        "next_attempt_at": None, "retrying": False, "error_kind": None, "last_error": None,
    }
    # The pinned test environment has no Procore credentials: the button is
    # off and the reason says why.
    assert out["harvest_available"] == {"ok": False, "reason": harvest_svc._MSG_NOT_CONFIGURED}
    harvest_selects = [sel for table, sel in db.selects if table == "rfp_harvests"]
    assert harvest_selects == [harvest_svc._PUBLIC_HARVEST_COLUMNS]
    assert all("raw" not in sel.split(", ") and "claim_token" not in sel for sel in harvest_selects)


def test_the_detail_answers_the_harvest_keys_for_a_row_without_a_harvest(db, ingest):
    _seed(db)
    out = rr.get_rfp_email(E4, user=_user())
    assert out["harvest"] is None and out["harvest_job"] is None
    assert out["harvest_available"] == {"ok": False, "reason": harvest_svc._MSG_NO_HARVESTER}


def test_the_detail_falls_back_to_the_platform_row_for_an_unlinked_email(db, ingest):
    _seed_harvest(db)
    db.tables["rfp_emails"].append(_email(
        "3f1c0b00-0000-4000-8000-000000000008", status="harvest", invitation_method="procore",
        body_text=PROCORE_BODY, harvest_id=None,
    ))
    out = rr.get_rfp_email("3f1c0b00-0000-4000-8000-000000000008", user=_user())
    assert out["harvest"]["id"] == H1 and "raw" not in out["harvest"]


def test_the_detail_harvest_context_survives_a_queue_read_failure(db, ingest, monkeypatch):
    _seed_harvest(db)
    monkeypatch.setattr(llm_queue, "poll_info", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    out = rr.get_rfp_email(E7, user=_user())
    assert out["harvest_job"] is None and out["harvest"]["id"] == H1


def test_the_detail_harvest_availability_follows_the_row_status(db, ingest, harvest_mod):
    _seed_harvest(db)
    assert rr.get_rfp_email(E7, user=_user())["harvest_available"] == {"ok": True, "reason": None}
    assert rr.get_rfp_email(E5, user=_user())["harvest_available"] == {
        "ok": False, "reason": "The email is not at a stage that can be harvested."
    }
    harvest_mod.can = (False, "No harvester exists for this invitation method.")
    assert rr.get_rfp_email(E7, user=_user())["harvest_available"] == {
        "ok": False, "reason": "No harvester exists for this invitation method."
    }


def test_processed_tab_rows_carry_harvest_status(db):
    _seed_harvest(db)
    db.tables["rfp_emails"].append(_email(
        "3f1c0b00-0000-4000-8000-000000000009", status="harvest", invitation_method="procore",
        received_at="2026-09-09T04:00:00+00:00", body_text=PROCORE_BODY, harvest_id=None,
    ))
    db.tables["rfp_emails"].append(_email(
        "3f1c0b00-0000-4000-8000-00000000000a", status="done", invitation_method="procore",
        received_at="2026-09-09T03:00:00+00:00", harvest_id="8d000000-0000-4000-8000-0000000000ff",
    ))
    out = rr.list_rfp_emails(tab="processed", limit=50, offset=0, user=_user())
    by_id = {r["id"]: r for r in out["items"]}
    assert by_id[E7]["harvest_status"] == "complete"
    assert by_id["3f1c0b00-0000-4000-8000-000000000009"]["harvest_status"] == "pending"
    assert by_id["3f1c0b00-0000-4000-8000-00000000000a"]["harvest_status"] is None
    assert by_id[E4]["harvest_status"] is None and by_id[E6]["harvest_status"] is None
    assert set(rr.TAB_STATUSES["processed"]) == {
        "done", "merged", "duplicate", "harvest", "split", "create", "created"
    }
    # One query for the page's harvests, by id, with the status column only.
    harvest_selects = [sel for table, sel in db.selects if table == "rfp_harvests"]
    assert harvest_selects == ["id, status"]
    assert "harvest_id" in rr._LIST_SELECT and "harvested_at" in rr._LIST_SELECT
    # List rows never carry the harvest row itself.
    assert all("harvest" not in r or r.get("harvest") is None for r in out["items"])


def test_harvest_status_is_manage_roles_only_and_never_returns_a_secret(db, harvest_mod):
    route = next(r for r in rr.router.routes if r.path == "/rfp-emails/harvest-status")
    calls = {d.call for d in route.dependant.dependencies}
    assert rr.require_sender_admin in calls and rr.require_review_queue not in calls
    for role in (Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR,
                 Role.ACCOUNTANT, Role.ESTIMATOR):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rr.require_sender_admin(user=_user(role)))
        assert exc.value.status_code == 403
    out = rr.rfp_harvest_status(_user(Role.IT_ADMIN))
    assert out["configured"] is True and out["account"] == "bot@example.com"
    assert harvest_mod.calls == [("session_status",)]
    dumped = repr(out).lower()
    assert "cookie" not in dumped and "password" not in dumped and "_session_id" not in dumped


def test_harvest_status_through_the_real_service_never_returns_cookies_or_a_password(db):
    db.tables["rfp_harvest_sessions"] = [
        {
            "provider": "procore", "account": "bot@example.com",
            "cookies": [{"name": "_session_id", "value": "very-secret-cookie"}],
            "logged_in_at": "2026-09-14T10:00:00+00:00", "last_used_at": None,
            "last_login_attempt_at": "2026-09-14T10:00:00+00:00", "login_failures": 0,
            "locked_until": None, "last_error": None,
        },
        # A PipelineSuite portal session (0129): its jar is just as secret.
        {
            "provider": "pipelinesuite:gc.pipelinesuite.com", "account": "0123456789abcdef",
            "cookies": [{"name": "JSESSIONID", "value": "very-secret-portal-cookie"}],
            "logged_in_at": "2026-09-16T10:00:00+00:00", "last_used_at": None,
            "last_login_attempt_at": "2026-09-16T10:00:00+00:00", "login_failures": 0,
            "locked_until": None, "last_error": None,
        },
    ]
    out = rr.rfp_harvest_status(_user(Role.EXECUTIVE))
    assert set(out) == {
        "enabled", "configured", "account", "logged_in_at", "last_used_at",
        "last_login_attempt_at", "login_failures", "locked_until", "last_error", "active_jobs",
        "pipelinesuite",
    }
    dumped = repr(out)
    assert "very-secret-cookie" not in dumped and "cookies" not in out
    assert "very-secret-portal-cookie" not in dumped
    assert out["logged_in_at"] == "2026-09-14T10:00:00+00:00"
    portals = out["pipelinesuite"]["portals"]
    assert [p["host"] for p in portals] == ["gc.pipelinesuite.com"]
    assert "cookies" not in portals[0]
    # The pinned test env has no credentials, so the block says unconfigured.
    assert out["configured"] is False and out["account"] is None


def test_harvest_status_is_registered_before_the_parameterised_route():
    paths = [r.path for r in rr.router.routes]
    assert paths.index("/rfp-emails/harvest-status") < paths.index("/rfp-emails/{email_id}")


def test_the_harvest_error_codes_are_cataloged():
    assert ErrorCode.RFP_HARVEST_ACTIVE == "rfp_harvest_active"
    assert ErrorCode.RFP_HARVEST_NOT_AVAILABLE == "rfp_harvest_not_available"
    assert ErrorCode.RFP_HARVEST_LOCKED == "rfp_harvest_locked"



# ── Project creation (docs/RFP_CREATE.md 8) ──────────────────────────────


def test_create_answers_the_created_project_and_maps_the_two_refusals(db, ingest):
    from app.services import rfp_create

    _seed(db)
    out = rr.create_rfp_email_project(E4, user=_user(uid="u7"))
    assert out == {"project_id": P3, "number": "26.9.7204", "linked": False,
                   "why": None, "duplicate_of": None}
    assert ("create", E4, "u7") in ingest.calls
    ingest.raise_create = rfp_create.CreateRefused("This invitation has no project name; set one first.")
    with pytest.raises(HTTPException) as exc:
        rr.create_rfp_email_project(E4, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.detail == "This invitation has no project name; set one first."
    assert exc.value.headers["X-Error-Code"] == rr.CODE_CREATE_REFUSED == "rfp_create_refused"
    ingest.raise_create = rfp_create.CreateInProgress("Another worker is creating it.")
    with pytest.raises(HTTPException) as exc:
        rr.create_rfp_email_project(E4, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == rr.CODE_CREATE_IN_PROGRESS == "rfp_create_in_progress"
    # A split still filing the documents (docs/RFP_SPLIT.md 10.5): the same
    # refusal, with the service's sentence.
    from app.services import rfp_split

    ingest.raise_create = rfp_create.CreateWaitingForSplit(rfp_split.MSG_CREATE_WAITS)
    with pytest.raises(HTTPException) as exc:
        rr.create_rfp_email_project(E4, user=_user())
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_CREATE_WAITS
    assert exc.value.headers["X-Error-Code"] == rr.CODE_CREATE_REFUSED
    # A link made by the 4.6 guard says which rule joined it and to which
    # copy, so the page can word the toast without guessing.
    ingest.raise_create = None
    ingest.return_create = rfp_create.Created(
        project_id=P3, number="26.9.7204", linked=True, files_job=False,
        why="sibling", duplicate_of="e-younger",
    )
    assert rr.create_rfp_email_project(E4, user=_user()) == {
        "project_id": P3, "number": "26.9.7204", "linked": True,
        "why": "sibling", "duplicate_of": "e-younger",
    }
    # The recent-project half never links on this path: it refuses, and the
    # router returns the service's sentence as the 409 it already returns.
    ingest.return_create = None
    ingest.raise_create = rfp_create.CreateRefused(
        "A project with this name for this GC was created 7 minutes ago: 26.9.7124 X. "
        "Merge this email into it, or rename the project, before creating another."
    )
    with pytest.raises(HTTPException) as exc:
        rr.create_rfp_email_project(E4, user=_user())
    assert exc.value.status_code == 409 and "7 minutes ago" in exc.value.detail
    assert exc.value.headers["X-Error-Code"] == rr.CODE_CREATE_REFUSED
    ingest.raise_create = None
    # A missing or malformed id is a 404 before the service runs.
    for bad in (MISSING, NOT_A_UUID):
        with pytest.raises(HTTPException) as exc:
            rr.create_rfp_email_project(bad, user=_user())
        assert exc.value.status_code == 404
    assert not db.tables.get("audit_log")   # the service audits, not the router


def test_set_name_answers_the_detail_and_maps_the_refusals(db, ingest):
    _seed(db)
    out = rr.set_rfp_email_project_name(E4, rr.SetNameIn(name="Warehouse HVAC"), user=_user(uid="u7"))
    assert ("set_name", E4, "Warehouse HVAC", "u7") in ingest.calls
    assert out["id"] == E4 and "mailboxes" in out and "created_project" in out
    ingest.raise_value = True
    with pytest.raises(HTTPException) as exc:
        rr.set_rfp_email_project_name(E4, rr.SetNameIn(name=" \x01 "), user=_user())
    assert exc.value.status_code == 400
    ingest.raise_value = False
    ingest.raise_lookup = True
    with pytest.raises(HTTPException) as exc:
        rr.set_rfp_email_project_name(E4, rr.SetNameIn(name="Warehouse HVAC"), user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_EMAIL_NOT_ACTIONABLE.value
    with pytest.raises(Exception):
        rr.SetNameIn(name="")


def test_the_create_and_set_name_routes_are_review_queue_gated():
    for path in ("/rfp-emails/{email_id}/create", "/rfp-emails/{email_id}/set-name"):
        route = next(r for r in rr.router.routes if r.path == path)
        calls = {d.call for d in route.dependant.dependencies}
        assert rr.require_review_queue in calls and rr.require_rfp_emails in calls
        assert rr.rfp_emails_rate_limit in calls


def test_detail_carries_the_created_project_and_create_available(db, ingest):
    _seed(db)
    made = "5a000000-0000-4000-8000-0000000000cc"
    db.tables["projects"].append({"id": made, "name": "Warehouse HVAC", "number": "26.9.7204"})
    row = next(r for r in db.tables["rfp_emails"] if r["id"] == E4)
    row["extracted_project_name"] = "Warehouse HVAC"
    out = rr.get_rfp_email(E4, user=_user())
    assert out["created_project"] is None and out["create_available"] is True
    row.update(status="created", created_project_id=made)
    out = rr.get_rfp_email(E4, user=_user())
    assert out["created_project"] == {"id": made, "number": "26.9.7204", "name": "Warehouse HVAC"}
    assert out["create_available"] is False
    # A sibling follower, a nameless row and a pending row never offer the button.
    row.update(status="done", created_project_id=None, flag_reason="sibling")
    assert rr.get_rfp_email(E4, user=_user())["create_available"] is False
    row.update(flag_reason=None, extracted_project_name=None)
    assert rr.get_rfp_email(E4, user=_user())["create_available"] is False
    row.update(extracted_project_name="X", status="create")
    assert rr.get_rfp_email(E4, user=_user())["create_available"] is False
    assert "created_project_id" in rr._DETAIL_SELECT and "created_project_id" in rr._LIST_SELECT
    listed = rr.list_rfp_emails(tab="processed", limit=50, offset=0, user=_user())
    assert any(r["id"] == E4 and r.get("created_project_id") is None for r in listed["items"])


# ── Blocked senders (doc 3.1, migration 0133) ────────────────────────────


def _block_user(role=Role.ESTIMATING_ADMIN, uid="u1", is_dev=False):
    return CurrentUser(
        id=uid, email="e@g3electrical.com", role=role, is_active=True, is_dev=is_dev
    )


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN])
def test_block_management_admits_its_three_roles(role):
    user = _block_user(role)
    assert asyncio.run(rr.require_block_admin(user=user)) is user


@pytest.mark.parametrize(
    "role",
    [
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
        Role.ACCOUNTANT,
        Role.ESTIMATOR,
    ],
)
def test_block_management_blocks_everyone_else(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_block_admin(user=_block_user(role)))
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "role", [Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR]
)
def test_a_dev_account_may_block_from_any_review_queue_role(role):
    """The one gate on this router that reads a profile flag as well as a
    role: a dev switched into an engineer focus still has to be able to stop
    a sender flooding the queue."""
    user = _block_user(role, is_dev=True)
    assert asyncio.run(rr.require_block_admin(user=user)) is user


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.ESTIMATOR])
def test_the_dev_flag_does_not_open_blocking_to_a_role_that_cannot_act(role):
    """Blocking is global and cross-user: it parks that sender's mail for
    everybody, and keeps parking it. The read-only accountant may never take
    it and the external estimator never reaches this router at all, so the
    dev flag only extends the gate across roles that could already act on the
    queue (docs/RFP_EMAIL_VISIBILITY.md 1, "Who acts")."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_block_admin(user=_block_user(role, is_dev=True)))
    assert exc.value.status_code == 403


def test_block_list_puts_locked_first_and_names_the_blocker(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(
        rr,
        "get_settings",
        lambda: SimpleNamespace(rfp_email_ingestion_blocked_domain_set={"pinned.example"}),
    )
    out = rr.list_blocked_senders(_=_block_user(Role.EXECUTIVE))
    assert [r["value"] for r in out["items"]] == ["buildingconnected.com", "noreply@spam.example"]
    assert out["items"][0]["locked"] is True and out["items"][0]["created_by_name"] is None
    assert out["items"][1]["created_by_name"] == "Dana Ruiz"
    # The environment list is reported separately and is not removable here.
    assert out["env_domains"] == ["pinned.example"]


def test_add_block_normalizes_and_reports_what_it_parked(db, ingest):
    _seed(db)
    ingest.parked = 4
    res = rr.create_blocked_sender(
        rr.BlockedSenderIn(kind="address", value="  NoReply@Spammy.Example ", reason=" junk "),
        user=_block_user(Role.ESTIMATING_ADMIN, uid="u7"),
    )
    assert res["item"]["kind"] == "address"
    assert res["item"]["value"] == "noreply@spammy.example"
    assert res["item"]["created_by"] == "u7" and res["item"]["created_by_name"] == "Sam Exec"
    assert res["parked"] == 4
    assert ("add_block", "address", "noreply@spammy.example", " junk ", "u7") in ingest.calls


def test_blocking_a_whole_domain_is_allowed(db, ingest):
    _seed(db)
    res = rr.create_blocked_sender(
        rr.BlockedSenderIn(kind="domain", value="SpamCo.example"),
        user=_block_user(Role.EXECUTIVE),
    )
    assert res["item"]["kind"] == "domain" and res["item"]["value"] == "spamco.example"


def test_blocking_a_public_provider_domain_is_a_400_with_its_own_code(db, ingest):
    """Blocking gmail.com would silently drop every GC contact who uses it,
    so the domain form is refused and the sentence names the way out."""
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.create_blocked_sender(
            rr.BlockedSenderIn(kind="domain", value="gmail.com"),
            user=_block_user(Role.EXECUTIVE),
        )
    assert exc.value.status_code == 400
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_BLOCK_PUBLIC_DOMAIN
    assert "individual email address" in exc.value.detail
    assert ("add_block",) not in {(c[0],) for c in ingest.calls}


def test_blocking_one_public_provider_address_is_allowed(db, ingest):
    _seed(db)
    res = rr.create_blocked_sender(
        rr.BlockedSenderIn(kind="address", value="nuisance@gmail.com"),
        user=_block_user(Role.EXECUTIVE),
    )
    assert res["item"]["value"] == "nuisance@gmail.com"


def test_blocking_our_own_domain_is_refused(db, ingest, monkeypatch):
    _seed(db)
    monkeypatch.setattr(
        rr,
        "get_settings",
        lambda: SimpleNamespace(
            rfp_email_ingestion_internal_domain_set={"g3electrical.com"},
            rfp_email_ingestion_blocked_domain_set=set(),
        ),
    )
    with pytest.raises(HTTPException) as exc:
        rr.create_blocked_sender(
            rr.BlockedSenderIn(kind="domain", value="g3electrical.com"),
            user=_block_user(Role.IT_ADMIN),
        )
    assert exc.value.status_code == 400
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_BLOCK_INTERNAL_DOMAIN


def test_a_malformed_block_value_is_a_400(db, ingest):
    _seed(db)
    for kind, value in (("address", "not-an-address"), ("domain", "http://x.example/y")):
        with pytest.raises(HTTPException) as exc:
            rr.create_blocked_sender(
                rr.BlockedSenderIn(kind=kind, value=value), user=_block_user(Role.EXECUTIVE)
            )
        assert exc.value.status_code == 400
        assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_BLOCK_INVALID


def test_a_duplicate_block_is_a_409(db, ingest):
    _seed(db)
    ingest.raise_duplicate = True
    with pytest.raises(HTTPException) as exc:
        rr.create_blocked_sender(
            rr.BlockedSenderIn(kind="address", value="noreply@spam.example"),
            user=_block_user(Role.EXECUTIVE),
        )
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_BLOCK_DUPLICATE


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN])
def test_only_the_it_admin_or_a_dev_may_unblock_a_platform_row(db, ingest, role):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rr.delete_blocked_sender(BLOCK_LOCKED, user=_block_user(role))
    assert exc.value.status_code == 403
    assert exc.value.detail == ErrorCode.RFP_BLOCK_LOCKED_IT_ADMIN_ONLY
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_BLOCK_LOCKED_IT_ADMIN_ONLY
    assert len(db.tables["rfp_blocked_senders"]) == 2


def test_the_it_admin_may_unblock_a_platform_row(db, ingest):
    _seed(db)
    assert rr.delete_blocked_sender(BLOCK_LOCKED, user=_block_user(Role.IT_ADMIN)) == {
        "id": BLOCK_LOCKED,
        "deleted": True,
    }
    assert [r["id"] for r in db.tables["rfp_blocked_senders"]] == [BLOCK_OPEN]


def test_a_dev_may_unblock_a_platform_row(db, ingest):
    _seed(db)
    rr.delete_blocked_sender(
        BLOCK_LOCKED, user=_block_user(Role.ESTIMATING_ADMIN, is_dev=True)
    )
    assert [r["id"] for r in db.tables["rfp_blocked_senders"]] == [BLOCK_OPEN]


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN])
def test_any_block_role_may_unblock_an_added_row(db, ingest, role):
    _seed(db)
    rr.delete_blocked_sender(BLOCK_OPEN, user=_block_user(role))
    assert [r["id"] for r in db.tables["rfp_blocked_senders"]] == [BLOCK_LOCKED]


def test_unblock_404s_a_missing_or_malformed_id(db, ingest):
    _seed(db)
    for bad in (MISSING, NOT_A_UUID):
        with pytest.raises(HTTPException) as exc:
            rr.delete_blocked_sender(bad, user=_block_user(Role.IT_ADMIN))
        assert exc.value.status_code == 404


# ── Block sender from the review queue ───────────────────────────────────


def test_block_sender_blocks_the_stored_address_and_returns_the_fresh_row(db, ingest):
    _seed(db)
    ingest.parked = 2
    out = rr.block_rfp_email_sender(
        E1,
        rr.BlockSenderIn(kind="address", confirm=" PM@ExampleGC.com "),
        user=_block_user(Role.ESTIMATING_ADMIN, uid="u7"),
    )
    assert out["item"]["value"] == "pm@examplegc.com"
    assert out["parked"] == 2
    assert out["email"]["id"] == E1
    assert ("block_email_sender", E1, "address", "pm@examplegc.com", "u7") in ingest.calls


def test_block_sender_can_block_the_whole_company(db, ingest):
    """The domain form is derived from the STORED address, so the request
    can never aim the block at somebody else's company."""
    _seed(db)
    out = rr.block_rfp_email_sender(
        E1,
        rr.BlockSenderIn(kind="domain", confirm="examplegc.com"),
        user=_block_user(Role.EXECUTIVE),
    )
    assert out["item"]["kind"] == "domain" and out["item"]["value"] == "examplegc.com"


def test_block_sender_refuses_a_confirmation_that_does_not_match(db, ingest):
    _seed(db)
    for kind, typed in (
        ("address", "pm@examplegc.co"),
        ("address", "examplegc.com"),     # the domain typed under the address form
        ("domain", "pm@examplegc.com"),   # the address typed under the domain form
    ):
        with pytest.raises(HTTPException) as exc:
            rr.block_rfp_email_sender(
                E1, rr.BlockSenderIn(kind=kind, confirm=typed), user=_block_user(Role.EXECUTIVE)
            )
        assert exc.value.status_code == 400
        assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_BLOCK_CONFIRM_MISMATCH
    assert not [c for c in ingest.calls if c[0] == "block_email_sender"]


def test_block_sender_404s_a_missing_or_malformed_email_id(db, ingest):
    _seed(db)
    for bad in (MISSING, NOT_A_UUID):
        with pytest.raises(HTTPException) as exc:
            rr.block_rfp_email_sender(
                bad, rr.BlockSenderIn(confirm="x@y.com"), user=_block_user(Role.EXECUTIVE)
            )
        assert exc.value.status_code == 404


def test_block_sender_409s_a_sender_already_blocked(db, ingest):
    _seed(db)
    ingest.raise_duplicate = True
    with pytest.raises(HTTPException) as exc:
        rr.block_rfp_email_sender(
            E1,
            rr.BlockSenderIn(kind="address", confirm="pm@examplegc.com"),
            user=_block_user(Role.EXECUTIVE),
        )
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_BLOCK_DUPLICATE


def test_the_block_reason_is_capped():
    from pydantic import ValidationError

    assert rr.BlockedSenderIn(kind="address", value="a@b.com", reason="why").reason == "why"
    with pytest.raises(ValidationError):
        rr.BlockedSenderIn(kind="address", value="a@b.com", reason="x" * 201)
    with pytest.raises(ValidationError):
        rr.BlockSenderIn(confirm="a@b.com", reason="x" * 201)


# ── Migration 0133 ───────────────────────────────────────────────────────


def test_migration_0133_seeds_the_platform_blocks_and_widens_the_status_check():
    """0133 creates rfp_blocked_senders, seeds the four platform domains as
    LOCKED rows (so the page shows them and only an IT Admin removes one),
    adds the terminal `blocked_sender` status and is idempotent."""
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    sql = (migrations / "0133_rfp_blocked_senders.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0133 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "—" not in sql and "–" not in sql
    # Nothing is deleted or rewritten: a block is additive.
    assert "delete from" not in sql
    assert not re.search(r"update rfp_emails", sql)
    # Idempotent creates plus the on-conflict seed.
    assert "create table if not exists rfp_blocked_senders" in sql
    assert "create unique index if not exists rfp_blocked_senders_uidx" in sql
    seed = re.search(
        r"insert into rfp_blocked_senders \(kind, value, reason, locked\) values\s+"
        r"(\(.+?\))\s+on conflict \(kind, value\) do nothing;",
        sql,
        re.S,
    )
    assert seed, "seed insert with the on conflict clause"
    values = {m.lower() for m in re.findall(r"'([a-z0-9.]+\.[a-z]{2,})'", seed.group(1))}
    assert values == {
        "buildingconnected.com", "ionwave.net", "planhub.com", "planhubprojects.com"
    }
    assert seed.group(1).count("true") == 4          # every seeded row is locked
    # The status check carries the router's live vocabulary exactly.
    check = re.search(
        r"add constraint rfp_emails_status_check check \(status in \(([^)]+)\)\)", sql, re.S
    )
    assert check
    statuses = {v.strip().strip("'") for v in check.group(1).split(",")}
    assert "blocked_sender" in statuses
    assert statuses == set().union(*[set(v) for v in rr.TAB_STATUSES.values()]) | {
        "received", "auth", "keywords", "classify", "authorize", "method", "extract",
        "match", "flagged_unauthorized", "review_llm", "review_match",
    }
    # Looked up by column so a re-run drops the named constraint and re-adds it.
    assert "a.attname = 'status'" in sql
    assert sql.count("do $$") == 1 and sql.count("end\n$$;") == 1
    # Deny-by-default RLS, like every other rfp_* table.
    assert "alter table rfp_blocked_senders enable row level security;" in sql
    assert "alter table rfp_blocked_senders force row level security;" in sql


# ── Per-mailbox visibility (docs/RFP_EMAIL_VISIBILITY.md 3.3) ────────────
# The router's half of the rule: every read is narrowed to the viewer's
# mailboxes and every /{email_id} route answers the unknown-id 404 for a row
# outside them, so a caller can never tell "not yours" from "no such row".
# The rule itself is pinned in tests/test_rfp_email_visibility.py.

TIESHA_MB = "tiesha@g3electrical.com"
TOM_MB = "tmoore@g3electrical.com"
FOREIGN = "3f1c0b00-0000-4000-8000-0000000000aa"   # sighted in tiesha@ only


def _scoped_user(role=Role.ESTIMATING_ADMIN, *, is_dev=False, mailboxes=()):
    return CurrentUser(
        id="u1", email="e@g3electrical.com", role=role, is_active=True,
        is_dev=is_dev, rfp_mailboxes=tuple(mailboxes),
    )


@pytest.fixture()
def foreign(db):
    """One row in a mailbox nobody in these tests owns, at review_match so
    every action family has something to aim at."""
    _seed(db)
    row = _email(
        FOREIGN,
        status="review_match",
        received_at="2026-09-09T13:00:00+00:00",
        mailboxes=[TIESHA_MB],
        primary_mailbox=TIESHA_MB,
    )
    db.tables["rfp_emails"].append(row)
    db.tables["rfp_email_sightings"].append(
        {"rfp_email_id": row["id"], "mailbox": TIESHA_MB}
    )
    return row["id"]


def test_the_list_only_shows_rows_from_the_viewers_own_mailboxes(db, foreign):
    # E1 is sighted in bids@ (shared) and tmoore@; the foreign row is not.
    out = rr.list_rfp_emails(tab="matches", limit=50, offset=0, user=_scoped_user())
    assert [r["id"] for r in out["items"]] == [E5]
    assert out["total"] == 1
    # The owner of that mailbox does see it, newest first.
    owner = _scoped_user(Role.EXECUTIVE, mailboxes=(TIESHA_MB,))
    out = rr.list_rfp_emails(tab="matches", limit=50, offset=0, user=owner)
    assert [r["id"] for r in out["items"]] == [foreign, E5]


def test_a_dev_wearing_it_admin_sees_every_row_and_gets_the_owners_column(db, foreign):
    db.tables["profiles"] = [
        {"id": "p1", "full_name": "Tiesha Vega", "rfp_mailboxes": [TIESHA_MB]},
    ]
    dev = _scoped_user(Role.IT_ADMIN, is_dev=True)
    out = rr.list_rfp_emails(tab="matches", limit=50, offset=0, user=dev)
    assert [r["id"] for r in out["items"]] == [foreign, E5]
    assert out["items"][0]["owners"] == [{"mailbox": TIESHA_MB, "name": "Tiesha Vega"}]
    # An unmapped mailbox answers a null name; the page falls back to the
    # address. E1 was sighted in two mailboxes, so it carries both.
    review = rr.list_rfp_emails(tab="review", limit=50, offset=0, user=dev)
    assert review["items"][0]["owners"] == [
        {"mailbox": "bids@g3electrical.com", "name": None},
        {"mailbox": TOM_MB, "name": None},
    ]


def test_owners_is_absent_for_everyone_but_the_dev_it_admin(db):
    _seed(db)
    out = rr.list_rfp_emails(tab="review", limit=50, offset=0, user=_scoped_user())
    assert "owners" not in out["items"][0]
    assert out["items"][0]["mailboxes"]  # the addresses themselves still ride along


def test_an_empty_scope_answers_an_empty_page_without_a_query(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(
        rr.visibility, "shared_mailboxes", lambda: set()
    )
    nobody = _scoped_user(Role.EXECUTIVE)
    before = len(db.selects)
    out = rr.list_rfp_emails(tab="review", limit=50, offset=0, user=nobody)
    assert out == {"items": [], "total": 0, "offset": 0, "limit": 50}
    assert rr.rfp_email_counts(user=nobody) == {tab: 0 for tab in rr.TAB_STATUSES}
    assert len(db.selects) == before  # neither read touched the table


def test_counts_are_scoped_so_a_badge_never_names_a_row_it_cannot_open(db, foreign):
    assert rr.rfp_email_counts(user=_scoped_user())["matches"] == 1
    owner = _scoped_user(Role.EXECUTIVE, mailboxes=(TIESHA_MB,))
    assert rr.rfp_email_counts(user=owner)["matches"] == 2
    dev = _scoped_user(Role.IT_ADMIN, is_dev=True)
    assert rr.rfp_email_counts(user=dev)["matches"] == 2


def test_match_stats_is_handed_the_viewers_scope(db, ingest, monkeypatch):
    monkeypatch.setattr(
        rr, "get_settings", lambda: SimpleNamespace(rfp_match_auto_merge_enabled=False)
    )
    rr.rfp_match_stats(user=_scoped_user(Role.EXECUTIVE, mailboxes=(TOM_MB,)))
    rr.rfp_match_stats(user=_scoped_user(Role.IT_ADMIN, is_dev=True))
    assert ingest.calls == [
        ("match_stats", {TOM_MB, "bids@g3electrical.com"}),
        ("match_stats", None),   # the dev IT Admin counts everything
    ]


def test_the_detail_404s_a_row_outside_the_scope_with_the_unknown_id_body(db, foreign):
    with pytest.raises(HTTPException) as exc:
        rr.get_rfp_email(foreign, user=_scoped_user())
    assert exc.value.status_code == 404 and exc.value.detail == rr._EMAIL_NOT_FOUND
    # Byte for byte what a genuinely missing id answers.
    with pytest.raises(HTTPException) as missing:
        rr.get_rfp_email(MISSING, user=_scoped_user())
    assert (missing.value.status_code, missing.value.detail) == (
        exc.value.status_code, exc.value.detail
    )
    # And the owner opens it.
    owner = _scoped_user(Role.EXECUTIVE, mailboxes=(TIESHA_MB,))
    assert rr.get_rfp_email(foreign, user=owner)["id"] == foreign


@pytest.mark.parametrize(
    "call",
    [
        lambda eid, user: rr.review_rfp_email(eid, rr.ReviewIn(decision="yes"), user=user),
        lambda eid, user: rr.dismiss_rfp_email(eid, user=user),
        lambda eid, user: rr.continue_rfp_email(eid, user=user),
        lambda eid, user: rr.create_rfp_email_project(eid, user=user),
        lambda eid, user: rr.set_rfp_email_project_name(
            eid, rr.SetNameIn(name="Anything"), user=user
        ),
        lambda eid, user: rr.patch_rfp_email(
            eid, rr.MethodPatchIn(invitation_method="organic"), user=user
        ),
        lambda eid, user: rr.harvest_rfp_email(eid, None, user=user),
        lambda eid, user: rr.merge_rfp_email_match(
            eid, RfpMatchMergeIn(project_id=P1), user=user
        ),
        lambda eid, user: rr.duplicate_rfp_email_match(
            eid, RfpMatchDuplicateIn(project_id=P1), user=user
        ),
        lambda eid, user: rr.reject_rfp_email_match(eid, user=user),
        lambda eid, user: rr.set_rfp_email_match_gc(eid, RfpMatchGcIn(gc_id=G1), user=user),
        lambda eid, user: rr.reopen_rfp_email_match(eid, None, user=user),
        lambda eid, user: rr.unmerge_rfp_email_match(
            eid, RfpMatchUnmergeIn(reason="wrong project"), user=user
        ),
        lambda eid, user: rr.block_rfp_email_sender(
            eid, rr.BlockSenderIn(kind="address", confirm="pm@examplegc.com"), user=user
        ),
    ],
)
def test_every_action_404s_a_row_outside_the_scope(db, ingest, foreign, call):
    """The check runs BEFORE the action is considered, so the service is never
    reached and nothing is written."""
    with pytest.raises(HTTPException) as exc:
        call(foreign, _scoped_user())
    assert exc.value.status_code == 404 and exc.value.detail == rr._EMAIL_NOT_FOUND
    assert ingest.calls == []


def test_the_accountant_may_read_the_queue_but_never_act(db):
    _seed(db)
    accountant = _scoped_user(Role.ACCOUNTANT)
    assert rr.VIEW_QUEUE_ROLES == RFP_REVIEW_ROLES + (Role.ACCOUNTANT,)
    assert asyncio.run(rr.require_view_queue(user=accountant)) is accountant
    out = rr.list_rfp_emails(tab="review", limit=50, offset=0, user=accountant)
    assert [r["id"] for r in out["items"]] == [E1]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_review_queue(user=accountant))
    assert exc.value.status_code == 403


def test_the_estimator_never_reaches_the_read_gate_either():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.require_view_queue(user=_scoped_user(Role.ESTIMATOR)))
    assert exc.value.status_code == 403


def test_a_select_that_already_names_mailboxes_is_not_asked_for_it_twice(db):
    """The token test trims, so a select written with the usual `, ` spacing
    matches (untrimmed it could never fire and every such read would ask
    PostgREST for the column twice)."""
    _seed(db)
    rr._email_row_or_404(db, E1, "id, mailboxes", user=_scoped_user())
    assert [sel for table, sel in db.selects if table == "rfp_emails"] == ["id, mailboxes"]


def test_owners_resolve_a_sighting_stored_in_another_case(db):
    """Sightings written before 0134 normalized them carry whatever case Graph
    reported; profiles.rfp_mailboxes is always lowercased, so the Recipient
    column has to compare on the lowercased value or an owned mailbox reads as
    unowned."""
    _seed(db)
    db.tables["rfp_email_sightings"] = [
        {"rfp_email_id": E1, "mailbox": " TMoore@G3Electrical.com "},
    ]
    db.tables["profiles"] = [
        {"id": "p1", "full_name": "Thomas Moore", "rfp_mailboxes": [TOM_MB]},
    ]
    out = rr.list_rfp_emails(
        tab="review", limit=50, offset=0, user=_scoped_user(Role.IT_ADMIN, is_dev=True)
    )
    row = out["items"][0]
    assert row["mailboxes"] == [TOM_MB]
    assert row["owners"] == [{"mailbox": TOM_MB, "name": "Thomas Moore"}]
