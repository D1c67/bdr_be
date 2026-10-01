"""The project side of RFP matching (docs/RFP_MATCHING.md 3.7, 3.7b, 3.9 and
section 9's router bullet), called as plain functions on app/routers/projects
against an in-memory fake Supabase, with app/services/rfp_email_ingest (the
match-row reads and writes) and app/services/rfp_match (the scorer and the
query builders) stubbed.

Pinned:

- The route table: GET /projects/{id}/rfp-matches, POST .../unmerge, POST
  .../acknowledge and POST /projects/similar exist; the three rfp-matches
  routes carry require_rfp_ingest AND the bidding feature gate, /similar the
  bidding gate only (New Bid code is never gated on the RFP flag) plus the
  catch-all rate limiter; /similar is registered before the /{project_id}
  routes.
- Roles: the match list is any internal role; unmerge is the Estimating
  Admin, the Executive and the IT Admin; acknowledge and /similar are writer
  roles.
- The flag-off contract: require_rfp_ingest 404s with the bare body, and
  GET /projects issues no rfp_project_matches query (and never reaches the
  service) while RFP_INGESTION_ENABLED is off; on, the counts are attached
  and the post-send set is derived inline over the loaded category states
  and reverify_return_stage, never by a per-project query.
- The accountant receives every match row with `email = null`; a
  review-queue role receives the email block; an engineer's breakdown is
  redacted (no date sub-score whose closest_kind was `actual`).
- can_unmerge and its reason (closed, role, sent); gc_on_project; the GC's
  proposal status; names for the deciding, acknowledging and unmerging users.
- unmerge and acknowledge: 404 for a match id from another project or a
  malformed one, the service call with the reason and the actor, 409 with the
  service's code, unmerge's 409 rfp_match_gc_already_sent on a
  ProposalSendError, no router audit (the service audits once).
- POST /projects/similar: dates in the body are ignored (byte-identical
  results), no date-derived field for a non-viewer role, a project whose
  internal_bid_at is older than the window is omitted even when its actual
  date is inside it, a year-old name match comes back as a possible rebid,
  the precreate threshold and the score ordering hold.
- DELETE /projects/{id}/gcs/{gc_id}: the 404 pre-check still runs first,
  the shared helper is called with refuse_if_sent=False, a ProposalSendError
  carrying rfp_match_unmerge_required is a 409 with that X-Error-Code, one
  without a code keeps its own status and no header, and the audit row is
  written exactly once on success.
- bid_notes and is_rebid are writer-editable fields; the ProjectOut counts
  default to 0.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import features
from app.core.deps import CurrentUser, require_internal, require_writer
from app.core.error_codes import ErrorCode
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES, RFP_REVIEW_ROLES, Role
from app.models.schemas import (
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    RfpMatchAcknowledgeIn,
    RfpMatchUnmergeIn,
    SimilarProjectsIn,
)
from app.routers import projects as pr
from app.services import notifications
from app.services import proposal_send as psend
from app.services.proposal_send import ProposalSendError

P1 = "5a000000-0000-4000-8000-000000000001"
P2 = "5a000000-0000-4000-8000-000000000002"
P3 = "5a000000-0000-4000-8000-000000000003"
P4 = "5a000000-0000-4000-8000-000000000004"
G1 = "6b000000-0000-4000-8000-000000000001"
G2 = "6b000000-0000-4000-8000-000000000002"
M1 = "7c000000-0000-4000-8000-000000000001"
M2 = "7c000000-0000-4000-8000-000000000002"
M3 = "7c000000-0000-4000-8000-000000000003"
M_OTHER = "7c000000-0000-4000-8000-0000000000aa"
MISSING = "7c000000-0000-4000-8000-0000000000ff"
E1 = "3f1c0b00-0000-4000-8000-000000000001"
E2 = "3f1c0b00-0000-4000-8000-000000000002"
NOW = datetime(2026, 9, 11, 17, 0, tzinfo=timezone.utc)


# ── Fake Supabase ──────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._preds = []
        self._negate = False
        self._single = False
        self._orders = []
        self._limit = None

    # PostgREST's `.not_` is a property returning the same builder with the
    # next filter negated; the fake mirrors that.
    @property
    def not_(self):
        self._negate = True
        return self

    def _add(self, pred):
        if self._negate:
            self._negate = False
            self._preds.append(lambda r, p=pred: not p(r))
        else:
            self._preds.append(pred)
        return self

    def select(self, sel="*", **k):
        self._op = "select"
        self.db.selects.append((self.table, sel))
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
        return self._add(lambda r: r.get(col) == val)

    def neq(self, col, val):
        return self._add(lambda r: r.get(col) != val)

    def in_(self, col, vals):
        vals = list(vals)
        return self._add(lambda r: r.get(col) in vals)

    def is_(self, col, val):
        want = None if val == "null" else val
        return self._add(lambda r: r.get(col) == want)

    def gte(self, col, val):
        return self._add(lambda r: r.get(col) is not None and r.get(col) >= val)

    def lt(self, col, val):
        return self._add(lambda r: r.get(col) is not None and r.get(col) < val)

    def single(self):
        self._single = True
        return self

    def order(self, col, desc=False, **k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **k):
        self._limit = n
        return self

    def _matches(self, row):
        return all(p(row) for p in self._preds)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            for col, desc in reversed(self._orders):
                hits = sorted(hits, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._limit is not None:
                hits = hits[: self._limit]
            out = [copy.deepcopy(r) for r in hits]
            if self._single:
                return SimpleNamespace(data=(out[0] if out else None))
            return SimpleNamespace(data=out)
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            made = []
            for item in items:
                row = {"id": f"{self.table}-{len(rows) + len(made) + 1}", **item}
                made.append(row)
            rows.extend(made)
            return SimpleNamespace(data=[dict(r) for r in made])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            gone = [dict(r) for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=gone)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [copy.deepcopy(r) for r in v] for k, v in (tables or {}).items()}
        self.selects: list[tuple[str, str]] = []

    def table(self, name):
        return _Query(self, name)


# ── Stubs for the two services ─────────────────────────────────────────────


class _MatchErr(LookupError):
    """The shape of rfp_email_ingest.RfpMatchError (doc section 12)."""

    def __init__(self, code, message="refused"):
        super().__init__(message)
        self.code = code


class _Ingest:
    def __init__(self):
        self.calls = []
        self.counts = {}
        self.match_rows = []
        self.raise_on_action = None

    def rfp_match_counts(self, sb, project_ids, post_send_ids):
        self.calls.append(("counts", list(project_ids), list(post_send_ids)))
        return copy.deepcopy(self.counts)

    def match_rows_for_project(self, sb, project_id):
        self.calls.append(("rows", project_id))
        return copy.deepcopy(self.match_rows)

    def unmerge(self, sb, match_id, reason, actor_id):
        self.calls.append(("unmerge", match_id, reason, actor_id))
        if self.raise_on_action is not None:
            raise self.raise_on_action
        return {"id": match_id}

    def acknowledge_match(self, sb, match_id, reason, actor_id):
        self.calls.append(("acknowledge", match_id, reason, actor_id))
        if self.raise_on_action is not None:
            raise self.raise_on_action
        return {"id": match_id}


@dataclass
class _NameScore:
    score: float
    dice: float
    containment: float
    conflict: dict | None


_STOP = {"project", "bid", "the", "of", "for", "and", "a", "an", "new"}


def _tokens(text):
    return {t for t in "".join(ch.lower() if ch.isalnum() else " " for ch in text or "").split()
            if t not in _STOP and not t.isdigit()}


def _redact(obj, role):
    """rfp_match.redact_candidates as documented (doc section 8): deep,
    untouched for viewer roles."""
    if role in ACTUAL_BID_VIEWER_ROLES:
        return obj
    if isinstance(obj, list):
        return [_redact(o, role) for o in obj]
    if isinstance(obj, dict):
        out = {k: (None if k == "actual_bid_at" else _redact(v, role)) for k, v in obj.items()}
        if isinstance(out.get("dates_used"), list):
            out["dates_used"] = [k for k in out["dates_used"] if k != "actual"]
        if out.get("closest_kind") == "actual":
            out["date"] = None
            out["closest_kind"] = None
        return out
    return obj


class _RfpMatch:
    """Stand-in for app/services/rfp_match: a Jaccard token scorer in place
    of the trigram/containment one (the router only needs a deterministic
    NameScore), plus the query builders and redaction as documented."""

    def __init__(self):
        self.calls = []
        self.redact_calls = []

    def pg_ts(self, dt):
        return dt.astimezone(timezone.utc).isoformat()

    def precreate_query(self, sb, lo_iso, *, select):
        self.calls.append(("precreate_query", lo_iso, select))
        return (
            sb.table("projects")
            .select(select)
            .is_("abandoned_at", "null")
            .not_.in_("current_stage", ["declined", "pm_only", "cp_only"])
            .gte("internal_bid_at", lo_iso)
        )

    def name_score(self, a, b, *, a_number=None, b_number=None, settings):
        ta, tb = _tokens(a), _tokens(b)
        score = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
        return _NameScore(score=round(score, 3), dice=score, containment=score, conflict=None)

    def rebid_lookup(self, name, projects, settings):
        self.calls.append(("rebid_lookup", [p["id"] for p in projects]))
        best = None
        for p in projects:
            ns = self.name_score(name, p.get("name"), b_number=p.get("number"), settings=settings)
            if ns.score >= settings.rfp_match_rebid_name_threshold and (
                best is None or ns.score > best[1]
            ):
                best = (p["id"], ns.score)
        return best

    def redact_candidates(self, obj, role):
        self.redact_calls.append(role)
        return _redact(obj, role)


# ── Fixtures ───────────────────────────────────────────────────────────────


def _settings(**over):
    base = dict(
        rfp_ingest_enabled=True,
        rfp_match_precreate_window_days=60,
        rfp_match_rebid_lookback_days=365,
        rfp_match_precreate_threshold=0.5,
        rfp_match_rebid_name_threshold=0.85,
        default_rate_limit_per_min=600,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _project(pid, name, number, *, internal_days_ago, actual_days_ago=None, stage="send_out",
             abandoned=None, gcs=(), outcome=None):
    return {
        "id": pid,
        "name": name,
        "number": number,
        "current_stage": stage,
        "abandoned_at": abandoned,
        "reverify_return_stage": None,
        "internal_bid_at": _iso(internal_days_ago),
        "actual_bid_at": _iso(actual_days_ago) if actual_days_ago is not None else None,
        "project_gcs": [{"general_contractors": {"name": g}} for g in gcs],
        "bid_outcomes": [{"result": outcome}] if outcome else [],
        "created_at": _iso(internal_days_ago + 30),
        "updated_at": _iso(internal_days_ago + 30),
        # The columns ProjectOut requires (nullable, no default): a list row
        # must validate as a whole, not only its RFP counts.
        "est_start_date": None,
        "est_finish_date": None,
        "invitation_at": None,
        "labor_time": None,
        "wage_type": None,
        "labor_note": None,
        "due_from_estimator_at": None,
        "notes": None,
        "current_owner_role": None,
        "created_by": None,
    }


def _match_row(mid, *, kind="merged", gc_id=G1, email_id=E1, gc_added=True, project_gc_id="link-1",
               unmerged=None, acknowledged=None, decided_by=None, closest_kind="actual",
               decided_at="2026-09-10T10:00:00+00:00"):
    return {
        "id": mid,
        "rfp_email_id": email_id,
        "project_id": P1,
        "gc_id": gc_id,
        "project_gc_id": project_gc_id,
        "kind": kind,
        "gc_added": gc_added,
        "score": 0.93,
        "candidate_rank": 1,
        "breakdown": {
            "name": 0.91, "date": 1.0, "notes": None, "notes_bonus": 0.0, "exact_time": False,
            "total": 0.93, "conflict": None, "dates_used": [closest_kind, "needs_by"],
            "closest_kind": closest_kind, "verdict": "same", "confidence": 0.9,
        },
        "candidates": [
            {"project_id": P1, "name": "Sunrise", "number": "26.9.7301",
             "breakdown": {"name": 0.91, "date": 1.0, "total": 0.93, "dates_used": [closest_kind],
                           "closest_kind": closest_kind}}
        ],
        "weights": {"scorer_version": "rfp_match_scorer_v1", "weight_name": 0.6},
        "gc_match_kind": "name",
        "sender_address": "pm@examplegc.com",
        "invitation_method": "procore",
        "decided_by": decided_by,
        "decided_at": decided_at,
        "acknowledged_by": "u-ack" if acknowledged else None,
        "acknowledged_at": acknowledged,
        "acknowledge_reason": "No bid" if acknowledged else None,
        "unmerged_by": "u-un" if unmerged else None,
        "unmerged_at": unmerged,
        "unmerge_reason": "Wrong school" if unmerged else None,
    }


@pytest.fixture()
def env(monkeypatch):
    db = FakeDB(
        {
            "projects": [
                _project(P1, "Sunrise Elementary Modernization", "26.9.7301",
                         internal_days_ago=-10, actual_days_ago=-8, stage="submitted",
                         gcs=("Example GC",)),
            ],
            "project_gcs": [
                {"id": "link-1", "project_id": P1, "gc_id": G1},
            ],
            "proposal_sends": [],
            "general_contractors": [
                {"id": G1, "name": "Example GC"},
                {"id": G2, "name": "Other Builders"},
            ],
            "profiles": [
                {"id": "u7", "full_name": "Sam Exec"},
                {"id": "u-ack", "full_name": "Dana Admin"},
                {"id": "u-un", "full_name": "Ira IT"},
            ],
            "rfp_emails": [
                {"id": E1, "subject": "ITB: Sunrise Elementary", "from_address": "pm@examplegc.com",
                 "from_name": "Pat", "received_at": "2026-09-10T09:00:00+00:00",
                 "extracted_project_name": "Sunrise Elementary Modernization",
                 "extracted_gc_name": "Example GC",
                 "extracted_bid_due_at": "2026-09-30T21:00:00+00:00",
                 "extracted_bid_due_has_time": True, "extracted_bid_notes": "Walk 9/15",
                 "body_text": "never returned here"},
            ],
            "rfp_project_matches": [
                {"id": M1, "project_id": P1}, {"id": M2, "project_id": P1},
                {"id": M3, "project_id": P1}, {"id": M_OTHER, "project_id": P2},
            ],
            "audit_log": [],
        }
    )
    ingest = _Ingest()
    match = _RfpMatch()
    settings = _settings()
    monkeypatch.setattr(pr, "get_supabase", lambda: db)
    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    monkeypatch.setattr(pr, "_ingest", lambda: ingest)
    monkeypatch.setattr(pr, "_match", lambda: match)
    monkeypatch.setattr(pr, "get_settings", lambda: settings)
    monkeypatch.setattr(pr.workflow, "load_category_states", lambda ids: {i: {} for i in ids})
    monkeypatch.setattr(pr.workflow, "load_category_state", lambda pid: {})
    monkeypatch.setattr(psend, "project_gc_rows", lambda pid: [{"id": "rows-of", "project": pid}])
    return SimpleNamespace(db=db, ingest=ingest, match=match, settings=settings)


# ── Route table, gates and roles ────────────────────────────────────────────


def _routes():
    return {(m, r.path): r for r in pr.router.routes for m in r.methods}


RFP_ROUTES = {
    ("GET", "/projects/{project_id}/rfp-matches"),
    ("POST", "/projects/{project_id}/rfp-matches/{match_id}/unmerge"),
    ("POST", "/projects/{project_id}/rfp-matches/{match_id}/acknowledge"),
}


def test_the_four_routes_exist():
    assert RFP_ROUTES | {("POST", "/projects/similar")} <= set(_routes())


def test_the_rfp_match_routes_carry_the_rfp_flag_and_the_bidding_gate():
    bidding = pr._BIDDING_ONLY[0].dependency
    routes = _routes()
    for key in RFP_ROUTES:
        calls = {d.call for d in routes[key].dependant.dependencies}
        assert features.require_rfp_ingest in calls, key
        assert bidding in calls, key


def test_similar_is_bidding_only_rate_limited_and_never_on_the_rfp_flag():
    """New Bid code: the similar-projects check runs whether or not RFP
    ingestion is switched on (doc 3.9)."""
    route = _routes()[("POST", "/projects/similar")]
    calls = {d.call for d in route.dependant.dependencies}
    assert pr._BIDDING_ONLY[0].dependency in calls
    assert pr.similar_projects_rate_limit in calls
    assert features.require_rfp_ingest not in calls
    assert require_writer in calls


def test_similar_is_registered_before_the_parameterised_routes():
    paths = [r.path for r in pr.router.routes]
    assert paths.index("/projects/similar") < paths.index("/projects/{project_id}")


def test_each_rfp_route_carries_the_documented_role_dependency():
    routes = _routes()
    deps = {
        ("GET", "/projects/{project_id}/rfp-matches"): require_internal,
        ("POST", "/projects/{project_id}/rfp-matches/{match_id}/unmerge"): pr.require_rfp_unmerge,
        ("POST", "/projects/{project_id}/rfp-matches/{match_id}/acknowledge"): require_writer,
    }
    for key, expected in deps.items():
        calls = {d.call for d in routes[key].dependant.dependencies}
        assert expected in calls, key
        others = {require_internal, require_writer, pr.require_rfp_unmerge} - {expected}
        assert not (calls & others), key


@pytest.mark.parametrize("role", [Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN])
def test_unmerge_admits_its_three_roles(role):
    user = _user(role)
    assert asyncio.run(pr.require_rfp_unmerge(user=user)) is user


@pytest.mark.parametrize(
    "role",
    [Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR, Role.ACCOUNTANT,
     Role.ESTIMATOR],
)
def test_unmerge_blocks_everyone_else(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(pr.require_rfp_unmerge(user=_user(role)))
    assert exc.value.status_code == 403


def test_require_rfp_ingest_404s_with_the_bare_body_while_off(monkeypatch):
    """The three rfp-matches routes 404 (never 403) while RFP_INGESTION_ENABLED
    is off: a deployment without the feature looks like one where it was
    never implemented."""
    monkeypatch.setattr(features, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=False))
    with pytest.raises(HTTPException) as exc:
        features.require_rfp_ingest()
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not Found"
    monkeypatch.setattr(features, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True))
    assert features.require_rfp_ingest() is None


# ── Counts on the list and detail routes (doc 3.9) ─────────────────────────


def _dashboard_db(env):
    env.db.tables["projects"] = [
        {**_project(P1, "Sunrise", "26.9.7301", internal_days_ago=-10, stage="submitted"),
         "reverify_return_stage": None},
        {**_project(P2, "Fire Station 7", "26.9.7302", internal_days_ago=-5, stage="verify"),
         "reverify_return_stage": "submitted"},
        {**_project(P3, "Lab Fit-Out", "26.9.7303", internal_days_ago=-3, stage="verify"),
         "reverify_return_stage": None},
        {**_project(P4, "Clinic", "26.9.7304", internal_days_ago=-1, stage="send_out"),
         "reverify_return_stage": None},
    ]
    states = {
        P1: {"send_out": {"current_task": "submitted", "status": "active"}},
        P2: {"send_out": {"current_task": "verify", "status": "active"}},
        P3: {"send_out": {"current_task": "verify", "status": "active"}},
        P4: {"send_out": {"current_task": "send_out", "status": "active"}},
    }
    return states


def test_list_projects_attaches_the_counts_and_derives_post_send_inline(env, monkeypatch):
    states = _dashboard_db(env)
    monkeypatch.setattr(pr.workflow, "load_category_states", lambda ids: states)
    env.ingest.counts = {P1: {"merged": 2, "history": 1, "new": 1}, P2: {"merged": 0, "history": 3, "new": 0}}
    out = pr.list_projects(stage=None, user=_user(Role.EXECUTIVE))
    by_id = {p["id"]: p for p in out}
    assert (by_id[P1]["rfp_merged_count"], by_id[P1]["rfp_history_count"], by_id[P1]["rfp_new_count"]) == (2, 1, 1)
    assert (by_id[P2]["rfp_merged_count"], by_id[P2]["rfp_history_count"], by_id[P2]["rfp_new_count"]) == (0, 3, 0)
    # Projects the service did not mention keep the schema defaults.
    assert "rfp_merged_count" not in by_id[P3] and "rfp_merged_count" not in by_id[P4]
    assert ProjectOut.model_validate(by_id[P3]).rfp_merged_count == 0
    # The post-send set: submitted, and verify with a post-send return stage;
    # never plain verify or send_out. Derived from the loaded rows, no query.
    (call,) = [c for c in env.ingest.calls if c[0] == "counts"]
    assert call[1] == [P1, P2, P3, P4]
    assert call[2] == [P1, P2]
    assert not any(t == "proposal_sends" for t, _ in env.db.selects)


def test_list_projects_issues_no_match_query_while_the_flag_is_off(env, monkeypatch):
    states = _dashboard_db(env)
    monkeypatch.setattr(pr.workflow, "load_category_states", lambda ids: states)
    monkeypatch.setattr(pr, "get_settings", lambda: _settings(rfp_ingest_enabled=False))
    env.ingest.counts = {P1: {"merged": 9, "history": 9, "new": 9}}
    out = pr.list_projects(stage=None, user=_user(Role.EXECUTIVE))
    assert env.ingest.calls == []
    assert not any(t == "rfp_project_matches" for t, _ in env.db.selects)
    assert all("rfp_merged_count" not in p for p in out)
    assert all(ProjectOut.model_validate(p).rfp_new_count == 0 for p in out)


def test_get_project_attaches_the_counts(env, monkeypatch):
    monkeypatch.setattr(
        pr.workflow, "load_category_state",
        lambda pid: {"send_out": {"current_task": "submitted", "status": "active"}},
    )
    env.ingest.counts = {P1: {"merged": 1, "history": 0, "new": 1}}
    out = pr.get_project(P1, user=_user(Role.ACCOUNTANT))
    assert out["rfp_merged_count"] == 1 and out["rfp_new_count"] == 1 and out["rfp_history_count"] == 0
    assert env.ingest.calls == [("counts", [P1], [P1])]


def test_get_project_attaches_portal_sources_only_with_the_ngem_flag(env, monkeypatch):
    """docs/RFP_NGEM_PORTAL.md section 5: the detail route carries the NGEM
    invitations resolved onto the project, read only while both flags are
    on; the list route never queries them."""
    from app.services import rfp_portal_ingest as portal

    calls = []
    source = {"portal": "ngem", "invitation_id": E1, "agency": "UNLV", "bid_number": "5584-GS",
              "bid_number_raw": "5584-GS Addendum 1", "addendum_no": 1,
              "close_at": "2026-09-17T21:00:00+00:00", "resolution": "system",
              "resolved_at": "2026-09-16T15:00:00+00:00"}
    monkeypatch.setattr(portal, "portal_sources_for_projects",
                        lambda sb, ids: calls.append(list(ids)) or {P1: [source]})
    out = pr.get_project(P1, user=_user(Role.ACCOUNTANT))
    assert out["portal_sources"] == [] and calls == []
    assert ProjectOut.model_validate(out).portal_sources == []
    monkeypatch.setattr(pr, "get_settings", lambda: _settings(rfp_ngem_enabled=True))
    out = pr.get_project(P1, user=_user(Role.ACCOUNTANT))
    assert calls == [[P1]] and out["portal_sources"] == [source]
    validated = ProjectOut.model_validate(out)
    assert validated.portal_sources[0].bid_number == "5584-GS" and validated.portal_sources[0].addendum_no == 1
    listed = pr.list_projects(stage=None, user=_user(Role.EXECUTIVE))
    assert calls == [[P1]] and all("portal_sources" not in p for p in listed)


def test_rfp_match_counts_returns_empty_without_ids_or_flag(env, monkeypatch):
    assert pr._rfp_match_counts([], {}, {}) == {}
    monkeypatch.setattr(pr, "get_settings", lambda: _settings(rfp_ingest_enabled=False))
    assert pr._rfp_match_counts([P1], {P1: {}}, {P1: {}}) == {}
    assert env.ingest.calls == []


@pytest.mark.parametrize(
    "head, ret, expected",
    [
        ("submitted", None, True),
        ("bid_outcome", None, True),
        ("verify", "submitted", True),
        ("verify", "bid_outcome", True),
        ("verify", None, False),
        ("verify", "send_out", False),
        ("send_out", None, False),
        ("generate_docs", "submitted", False),
        (None, None, False),
    ],
)
def test_gone_out_rule_mirrors_bid_has_gone_out(head, ret, expected):
    assert pr._gone_out(head, ret) is expected


def test_present_attaches_the_three_counts_only_when_supplied():
    base = {"id": P1, "current_stage": "submitted", "abandoned_at": None, "bid_outcomes": []}
    out = pr._present(dict(base), Role.EXECUTIVE, None, None, {"merged": 2, "history": 1, "new": 1})
    assert (out["rfp_merged_count"], out["rfp_history_count"], out["rfp_new_count"]) == (2, 1, 1)
    plain = pr._present(dict(base), Role.EXECUTIVE)
    assert not {"rfp_merged_count", "rfp_history_count", "rfp_new_count"} & set(plain)
    partial = pr._present(dict(base), Role.EXECUTIVE, None, None, {"merged": 1})
    assert (partial["rfp_merged_count"], partial["rfp_history_count"], partial["rfp_new_count"]) == (1, 0, 0)


def test_project_out_defaults_the_counts_and_the_new_fields():
    for field in ("rfp_merged_count", "rfp_history_count", "rfp_new_count"):
        assert ProjectOut.model_fields[field].default == 0
    assert ProjectOut.model_fields["is_rebid"].default is False
    assert ProjectOut.model_fields["bid_notes"].default is None


# ── bid_notes and is_rebid (doc 3.9) ───────────────────────────────────────


def test_bid_notes_and_is_rebid_are_writer_editable():
    for field in ("bid_notes", "is_rebid"):
        assert pr._FIELD_EDITORS[field] == pr._OPEN
    patch = ProjectUpdate(bid_notes="Walk 9/15 at 8", is_rebid=True).model_dump(exclude_unset=True)
    assert patch == {"bid_notes": "Walk 9/15 at 8", "is_rebid": True}
    # An explicit null clears the notes (the details modal sends null).
    assert ProjectUpdate(bid_notes=None).model_dump(exclude_unset=True) == {"bid_notes": None}
    assert ProjectCreate.model_fields["is_rebid"].default is False
    assert ProjectCreate.model_fields["bid_notes"].default is None


# ── GET /projects/{id}/rfp-matches ─────────────────────────────────────────


def _seed_matches(env, *, sends=()):
    env.ingest.match_rows = [
        _match_row(M3, kind="duplicate", gc_id=G1, email_id=E2, gc_added=False, project_gc_id=None,
                   decided_at="2026-09-10T12:00:00+00:00"),
        _match_row(M2, kind="merged", gc_id=G2, project_gc_id=None, unmerged="2026-09-10T11:30:00+00:00",
                   decided_by="u7", decided_at="2026-09-10T11:00:00+00:00"),
        _match_row(M1, kind="merged", gc_id=G1, acknowledged=None, decided_at="2026-09-10T10:00:00+00:00"),
    ]
    env.db.tables["proposal_sends"] = list(sends)


def test_accountant_gets_every_row_with_a_null_email_block(env):
    _seed_matches(env)
    out = pr.list_project_rfp_matches(P1, user=_user(Role.ACCOUNTANT))
    assert [r["id"] for r in out] == [M3, M2, M1]
    assert all(r["email"] is None for r in out)
    # The metadata is all there.
    open_row = out[2]
    assert open_row["kind"] == "merged" and open_row["gc_name"] == "Example GC"
    assert open_row["gc_added"] is True and open_row["gc_on_project"] is True
    assert open_row["proposal_status"] == "not_sent"
    assert open_row["score"] == 0.93 and open_row["scorer_version"] == "rfp_match_scorer_v1"
    assert open_row["decided_by_name"] is None  # the system; the FE renders "System"
    assert open_row["gc_match_kind"] == "name"
    assert open_row["sender_address"] == "pm@examplegc.com"
    assert open_row["invitation_method"] == "procore"
    assert out[1]["decided_by_name"] == "Sam Exec"
    assert out[1]["unmerged_by_name"] == "Ira IT" and out[1]["unmerge_reason"] == "Wrong school"
    assert out[1]["gc_on_project"] is False  # G2's link is gone
    # No email content leaked anywhere in the response.
    dumped = json.dumps(out)
    assert "ITB: Sunrise" not in dumped and "Walk 9/15" not in dumped
    assert not any(t == "rfp_emails" for t, _ in env.db.selects)


# Every key components/RfpMatchesModal.tsx (interface RfpProjectMatch) reads.
# The response is a BARE array: the modal swaps its whole list for it.
_MODAL_KEYS = {
    "id", "rfp_email_id", "kind", "gc_id", "gc_name", "gc_added", "gc_on_project",
    "proposal_status", "score", "candidate_rank", "scorer_version", "decided_by",
    "decided_by_name", "decided_at", "acknowledged_by_name", "acknowledged_at",
    "acknowledge_reason", "unmerged_by_name", "unmerged_at", "unmerge_reason",
    "can_unmerge", "can_unmerge_reason", "gc_match_kind", "sender_address",
    "invitation_method", "email",
}


def test_every_row_carries_every_key_the_modal_reads_as_a_bare_array(env):
    _seed_matches(env)
    for role in (Role.ACCOUNTANT, Role.ESTIMATING_ADMIN):
        out = pr.list_project_rfp_matches(P1, user=_user(role))
        assert isinstance(out, list) and len(out) == 3
        for row in out:
            assert _MODAL_KEYS <= set(row), _MODAL_KEYS - set(row)
    # Ordered decided_at desc, and the service was asked for exactly that.
    assert [r["decided_at"] for r in out] == sorted((r["decided_at"] for r in out), reverse=True)


def test_x_error_code_is_exposed_through_cors():
    """The modal branches on the X-Error-Code header of a 409 (a stale page's
    DELETE opens the modal on rfp_match_unmerge_required); a browser only
    hands a response header to the page when CORS exposes it."""
    from starlette.middleware.cors import CORSMiddleware

    from app.main import app

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "CORS middleware not mounted"
    assert "X-Error-Code" in cors[0].kwargs["expose_headers"]


@pytest.mark.parametrize("role", sorted(RFP_REVIEW_ROLES))
def test_review_queue_roles_get_the_email_block(env, role):
    _seed_matches(env)
    out = pr.list_project_rfp_matches(P1, user=_user(role))
    email = out[2]["email"]
    assert email["id"] == E1 and email["subject"] == "ITB: Sunrise Elementary"
    assert email["from_address"] == "pm@examplegc.com"
    assert email["extracted_project_name"] == "Sunrise Elementary Modernization"
    assert email["extracted_bid_notes"] == "Walk 9/15"
    assert email["weights"]["scorer_version"] == "rfp_match_scorer_v1"
    assert email["candidates"][0]["project_id"] == P1
    assert "body_text" not in email
    # The rfp_emails read names its columns: the body is never selected.
    (sel,) = [s for t, s in env.db.selects if t == "rfp_emails"]
    assert "body_text" not in sel and sel.strip() != "*"


def test_the_email_block_is_gated_on_the_shared_review_tuple():
    assert set(INTERNAL_ROLES) - set(RFP_REVIEW_ROLES) == {Role.ACCOUNTANT}


@pytest.mark.parametrize(
    "role", [Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR]
)
def test_an_engineers_breakdown_is_redacted(env, role):
    _seed_matches(env)
    out = pr.list_project_rfp_matches(P1, user=_user(role))
    assert env.match.redact_calls == [role]
    email = out[2]["email"]
    assert email["breakdown"]["date"] is None and email["breakdown"]["closest_kind"] is None
    assert "actual" not in email["breakdown"]["dates_used"]
    assert email["candidates"][0]["breakdown"]["date"] is None


def test_viewer_roles_are_never_redacted(env):
    _seed_matches(env)
    out = pr.list_project_rfp_matches(P1, user=_user(Role.EXECUTIVE))
    assert env.match.redact_calls == []
    assert out[2]["email"]["breakdown"]["closest_kind"] == "actual"


def test_can_unmerge_and_its_reason(env):
    _seed_matches(env, sends=[{"project_id": P1, "gc_id": G1, "status": "sent"}])
    admin = {r["id"]: r for r in pr.list_project_rfp_matches(P1, user=_user(Role.ESTIMATING_ADMIN))}
    assert (admin[M1]["can_unmerge"], admin[M1]["can_unmerge_reason"]) == (False, "sent")
    assert admin[M1]["proposal_status"] == "sent"
    assert (admin[M2]["can_unmerge"], admin[M2]["can_unmerge_reason"]) == (False, "closed")
    assert (admin[M3]["can_unmerge"], admin[M3]["can_unmerge_reason"]) == (False, "closed")

    env.db.tables["proposal_sends"] = [{"project_id": P1, "gc_id": G1, "status": "superseded"}]
    admin = {r["id"]: r for r in pr.list_project_rfp_matches(P1, user=_user(Role.EXECUTIVE))}
    assert (admin[M1]["can_unmerge"], admin[M1]["can_unmerge_reason"]) == (True, None)
    assert admin[M1]["proposal_status"] == "not_sent"  # superseded reads as not sent

    engineer = {r["id"]: r for r in pr.list_project_rfp_matches(P1, user=_user(Role.ESTIMATING_ENGINEER_LABOR))}
    assert (engineer[M1]["can_unmerge"], engineer[M1]["can_unmerge_reason"]) == (False, "role")

    env.db.tables["proposal_sends"] = [{"project_id": P1, "gc_id": G1, "status": "sending"}]
    admin = {r["id"]: r for r in pr.list_project_rfp_matches(P1, user=_user(Role.IT_ADMIN))}
    assert (admin[M1]["can_unmerge"], admin[M1]["can_unmerge_reason"]) == (False, "sent")


def test_match_list_404s_a_missing_project_and_answers_empty(env):
    with pytest.raises(HTTPException) as exc:
        pr.list_project_rfp_matches(P2, user=_user())
    assert exc.value.status_code == 404
    assert pr.list_project_rfp_matches(P1, user=_user()) == []


# ── unmerge and acknowledge ────────────────────────────────────────────────


def test_unmerge_calls_the_service_and_answers_the_list(env):
    _seed_matches(env)
    out = pr.unmerge_project_rfp_match(
        P1, M1, RfpMatchUnmergeIn(reason="Wrong school"), user=_user(Role.EXECUTIVE, uid="u7")
    )
    assert ("unmerge", M1, "Wrong school", "u7") in env.ingest.calls
    assert [r["id"] for r in out] == [M3, M2, M1]
    assert env.db.tables["audit_log"] == []  # the service audits, once


def test_acknowledge_calls_the_service_with_an_optional_reason(env):
    _seed_matches(env)
    pr.acknowledge_project_rfp_match(P1, M1, None, user=_user(uid="u7"))
    pr.acknowledge_project_rfp_match(
        P1, M1, RfpMatchAcknowledgeIn(reason=" No bid "), user=_user(Role.ESTIMATING_ENGINEER_MATERIALS, uid="u8")
    )
    acks = [c for c in env.ingest.calls if c[0] == "acknowledge"]
    assert acks == [("acknowledge", M1, None, "u7"), ("acknowledge", M1, "No bid", "u8")]
    assert env.db.tables["audit_log"] == []


@pytest.mark.parametrize("bad", [M_OTHER, MISSING, "not-a-uuid"])
def test_unmerge_and_acknowledge_404_a_match_that_is_not_this_projects(env, bad):
    """A match id from another project's modal (or a malformed one) is a 404
    here, and the service is never reached."""
    for call in (
        lambda: pr.unmerge_project_rfp_match(P1, bad, RfpMatchUnmergeIn(reason="Wrong"), user=_user()),
        lambda: pr.acknowledge_project_rfp_match(P1, bad, None, user=_user()),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404
    assert not [c for c in env.ingest.calls if c[0] in ("unmerge", "acknowledge")]


def test_unmerge_maps_a_send_in_flight_to_409_gc_already_sent(env):
    env.ingest.raise_on_action = ProposalSendError("A proposal has already been sent to this GC.")
    with pytest.raises(HTTPException) as exc:
        pr.unmerge_project_rfp_match(P1, M1, RfpMatchUnmergeIn(reason="Wrong"), user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_GC_ALREADY_SENT
    assert exc.value.detail == "A proposal has already been sent to this GC."


@pytest.mark.parametrize(
    "code", [ErrorCode.RFP_MATCH_GC_ALREADY_SENT, ErrorCode.RFP_MATCH_NOT_ACTIONABLE]
)
def test_unmerge_and_acknowledge_map_the_services_own_code(env, code):
    env.ingest.raise_on_action = _MatchErr(code, "Refused.")
    for call in (
        lambda: pr.unmerge_project_rfp_match(P1, M1, RfpMatchUnmergeIn(reason="Wrong"), user=_user()),
        lambda: pr.acknowledge_project_rfp_match(P1, M1, None, user=_user()),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 409
        assert exc.value.headers["X-Error-Code"] == code
        assert exc.value.detail == "Refused."


def test_a_bare_lookup_error_is_409_not_actionable(env):
    env.ingest.raise_on_action = LookupError("Already acknowledged.")
    with pytest.raises(HTTPException) as exc:
        pr.acknowledge_project_rfp_match(P1, M1, None, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_NOT_ACTIONABLE


def test_the_acknowledge_reason_is_optional_and_capped():
    assert RfpMatchAcknowledgeIn().reason is None
    assert RfpMatchAcknowledgeIn(reason="  ").reason is None
    with pytest.raises(Exception):
        RfpMatchAcknowledgeIn(reason="x" * 501)


# ── POST /projects/similar ─────────────────────────────────────────────────


def _similar_db(env):
    env.db.tables["projects"] = [
        # inside the 60-day precreate window (internal date), similar name
        _project(P1, "Sunrise Elementary Modernization", "26.9.7301", internal_days_ago=10,
                 actual_days_ago=5, gcs=("Example GC", "Other Builders")),
        # internal date OLDER than the window, actual date inside it: omitted
        _project(P2, "Sunrise Elementary Modernization Phase 2", "26.7.7101", internal_days_ago=90,
                 actual_days_ago=3),
        # a year-old exact name: a possible rebid, not a similar project
        _project(P3, "Sunrise Elementary Modernization", "25.9.6801", internal_days_ago=300,
                 actual_days_ago=298, stage="bid_outcome", outcome="lost"),
        # unrelated, inside the window: below the threshold
        _project(P4, "Downtown Parking Garage", "26.9.7304", internal_days_ago=2),
        # excluded stages and abandoned rows never come back
        _project("p-declined", "Sunrise Elementary Modernization", "26.9.7305", internal_days_ago=4,
                 stage="declined"),
        _project("p-abandoned", "Sunrise Elementary Modernization", "26.9.7306", internal_days_ago=4,
                 abandoned="2026-09-01T00:00:00+00:00"),
        _project("p-pm", "Sunrise Elementary Modernization", "26.9.7307", internal_days_ago=4,
                 stage="pm_only"),
        # beyond the rebid lookback entirely
        _project("p-ancient", "Sunrise Elementary Modernization", "24.9.6001", internal_days_ago=400),
    ]


def test_similar_windows_on_the_internal_date_and_finds_a_year_old_rebid(env, monkeypatch):
    _similar_db(env)
    monkeypatch.setattr(pr, "datetime", _FrozenDatetime)
    out = pr.similar_projects(SimilarProjectsIn(name="Sunrise Elementary Modernization"), user=_user())
    assert [p["id"] for p in out["similar"]] == [P1]
    item = out["similar"][0]
    assert item["number"] == "26.9.7301" and item["status"] == "active"
    assert item["current_stage"] == "send_out"
    assert item["gc_names"] == ["Example GC", "Other Builders"]
    assert item["breakdown"] == {"name": 1.0, "total": 1.0, "conflict": None}
    assert item["actual_bid_at"] is not None  # the admin may see it
    assert [p["id"] for p in out["possible_rebids"]] == [P3]
    assert out["possible_rebids"][0]["status"] == "lost"
    assert out["possible_rebids"][0]["breakdown"]["total"] == 1.0
    # The rebid band the router handed the lookup: the older slice only, so
    # P2 (old internal date) is in it and P1 is not.
    (lookup,) = [c for c in env.match.calls if c[0] == "rebid_lookup"]
    assert set(lookup[1]) == {P2, P3}
    # One query, via precreate_query, on the lookback bound.
    (query,) = [c for c in env.match.calls if c[0] == "precreate_query"]
    assert query[1] == (NOW - timedelta(days=365)).isoformat()
    assert "internal_bid_at" in query[2] and "bid_outcomes(result)" in query[2]


def test_similar_omits_a_project_whose_internal_date_is_old_even_if_its_actual_date_is_recent(env, monkeypatch):
    _similar_db(env)
    monkeypatch.setattr(pr, "datetime", _FrozenDatetime)
    out = pr.similar_projects(SimilarProjectsIn(name="Sunrise Elementary Modernization Phase 2"), user=_user())
    assert P2 not in [p["id"] for p in out["similar"]]
    # It is in the rebid band instead, where the name-only lookup may find it.
    assert [p["id"] for p in out["possible_rebids"]] == [P2]


def test_similar_ignores_dates_in_the_body_byte_for_byte(env, monkeypatch):
    _similar_db(env)
    monkeypatch.setattr(pr, "datetime", _FrozenDatetime)
    plain = SimilarProjectsIn.model_validate({"name": "Sunrise Elementary Modernization"})
    dated = SimilarProjectsIn.model_validate(
        {"name": "Sunrise Elementary Modernization", "internal_bid_at": "2026-09-30T21:00:00Z",
         "actual_bid_at": "2026-09-28T21:00:00Z", "number": "26.9.9999"}
    )
    assert plain.model_dump() == dated.model_dump() == {"name": "Sunrise Elementary Modernization"}
    a = json.dumps(pr.similar_projects(plain, user=_user()), sort_keys=True)
    b = json.dumps(pr.similar_projects(dated, user=_user()), sort_keys=True)
    assert a == b


@pytest.mark.parametrize("role", sorted(set(Role) - ACTUAL_BID_VIEWER_ROLES - {Role.ESTIMATOR}))
def test_similar_carries_no_date_derived_field_for_a_non_viewer(env, monkeypatch, role):
    _similar_db(env)
    monkeypatch.setattr(pr, "datetime", _FrozenDatetime)
    out = pr.similar_projects(SimilarProjectsIn(name="Sunrise Elementary Modernization"), user=_user(role))
    for item in out["similar"] + out["possible_rebids"]:
        assert item["actual_bid_at"] is None
        assert set(item["breakdown"]) == {"name", "total", "conflict"}
        assert item["breakdown"]["total"] == item["breakdown"]["name"]
    # Same membership and order as a viewer role sees.
    viewer = pr.similar_projects(SimilarProjectsIn(name="Sunrise Elementary Modernization"), user=_user())
    assert [p["id"] for p in out["similar"]] == [p["id"] for p in viewer["similar"]]


def test_similar_applies_the_threshold_and_orders_by_score(env, monkeypatch):
    _similar_db(env)
    env.db.tables["projects"].append(
        _project("p-partial", "Sunrise Elementary Roof", "26.9.7399", internal_days_ago=1)
    )
    monkeypatch.setattr(pr, "datetime", _FrozenDatetime)
    out = pr.similar_projects(SimilarProjectsIn(name="Sunrise Elementary Modernization"), user=_user())
    ids = [p["id"] for p in out["similar"]]
    assert ids == [P1, "p-partial"]  # 1.0 then 0.5 (at the threshold); the garage at 0 is out
    assert out["similar"][1]["breakdown"]["total"] == 0.5
    monkeypatch.setattr(pr, "get_settings", lambda: _settings(rfp_match_precreate_threshold=0.6))
    out = pr.similar_projects(SimilarProjectsIn(name="Sunrise Elementary Modernization"), user=_user())
    assert [p["id"] for p in out["similar"]] == [P1]


def test_similar_answers_empty_lists_when_nothing_is_close(env, monkeypatch):
    _similar_db(env)
    monkeypatch.setattr(pr, "datetime", _FrozenDatetime)
    out = pr.similar_projects(SimilarProjectsIn(name="Airport Runway Lighting"), user=_user())
    assert out == {"similar": [], "possible_rebids": []}


def test_similar_schema_requires_a_name_and_drops_everything_else():
    with pytest.raises(Exception):
        SimilarProjectsIn(name="")
    with pytest.raises(Exception):
        SimilarProjectsIn.model_validate({"internal_bid_at": "2026-09-30"})
    assert SimilarProjectsIn.model_validate({"name": "x", "gcs": [1]}).model_dump() == {"name": "x"}


class _FrozenDatetime(datetime):
    """datetime with now() pinned, so the window arithmetic is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


# ── DELETE /projects/{id}/gcs/{gc_id} (doc 3.7, ordinary removal) ──────────


def test_delete_gc_goes_through_the_shared_helper_with_refuse_if_sent_false(env, monkeypatch):
    calls = []

    def remove_gc_link(project_id, gc_id, *, link_id=None, refuse_if_sent=False, via_unmerge=False):
        calls.append((project_id, gc_id, link_id, refuse_if_sent, via_unmerge))
        env.db.tables["project_gcs"] = [
            r for r in env.db.tables["project_gcs"] if r["gc_id"] != gc_id
        ]
        return True

    monkeypatch.setattr(psend, "remove_gc_link", remove_gc_link, raising=False)
    out = pr.remove_project_gc(P1, G1, user=_user(uid="u7"))
    assert calls == [(P1, G1, None, False, False)]
    assert out == [{"id": "rows-of", "project": P1}]
    audits = [a for a in env.db.tables["audit_log"] if a["action"] == "project.gc_remove"]
    assert len(audits) == 1
    assert audits[0]["actor_id"] == "u7" and audits[0]["payload"] == {"gc_id": G1}


def test_delete_gc_still_404s_before_the_helper_is_touched(env, monkeypatch):
    called = []
    monkeypatch.setattr(psend, "remove_gc_link", lambda *a, **k: called.append(a), raising=False)
    with pytest.raises(HTTPException) as exc:
        pr.remove_project_gc(P1, G2, user=_user())
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        pr.remove_project_gc(P2, G1, user=_user())
    assert exc.value.status_code == 404
    assert called == [] and env.db.tables["audit_log"] == []


@pytest.mark.parametrize("role", sorted(set(Role) - {Role.ACCOUNTANT, Role.ESTIMATOR}))
def test_delete_of_a_system_added_gc_is_409_unmerge_required_for_every_writer(env, monkeypatch, role):
    def refuse(project_id, gc_id, **k):
        exc = ProposalSendError("This GC was added by RFP ingestion; use Unmerge.", 409)
        exc.code = ErrorCode.RFP_MATCH_UNMERGE_REQUIRED
        raise exc

    monkeypatch.setattr(psend, "remove_gc_link", refuse, raising=False)
    with pytest.raises(HTTPException) as exc:
        pr.remove_project_gc(P1, G1, user=_user(role))
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == ErrorCode.RFP_MATCH_UNMERGE_REQUIRED
    assert "Unmerge" in exc.value.detail
    assert env.db.tables["project_gcs"] == [{"id": "link-1", "project_id": P1, "gc_id": G1}]
    assert env.db.tables["audit_log"] == []


def test_delete_gc_keeps_the_helpers_own_status_for_an_uncoded_refusal(env, monkeypatch):
    """A send in progress (the helper's plain 409) surfaces as before: its
    status, its sentence, no code header."""

    def refuse(project_id, gc_id, **k):
        raise ProposalSendError("A proposal send to this GC is in progress.", 409)

    monkeypatch.setattr(psend, "remove_gc_link", refuse, raising=False)
    with pytest.raises(HTTPException) as exc:
        pr.remove_project_gc(P1, G1, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.detail == "A proposal send to this GC is in progress."
    assert not exc.value.headers
    assert env.db.tables["audit_log"] == []
