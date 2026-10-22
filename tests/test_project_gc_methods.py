"""Every GC's invitation method(s) on a project (migration 0173).

Pinned here:
  - the pure derivation: the creating RFP, live merges, portal invitations
    and the user-set method, unioned per GC with their sources, `by_hand`
    when there are none, GCs not on the project never credited;
  - the reads: unmerged rows excluded (via the query project_gc_rows makes),
    the creating GC found through the email, the portal invitation, the
    likely GC and the plan-`created` time window, the RFP reads skipped
    while RFP ingestion is off, portal rows dropped while their switch is
    off, a failed read degrading to the user-set method;
  - the wire: `methods` and `invitation_method` on GET /projects/{id}/gcs;
  - PATCH /projects/{id}/gcs/{gc_id} sets and clears the method, validates
    it (422), audits it, and keeps the writer + bidding gates (403 for the
    read-only accountant and the estimator).
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.deps import CurrentUser, get_current_user, require_writer
from app.core.roles import Role
from app.models.schemas import ProjectGCUpdate
from app.routers import projects as pr
from app.services import project_gc_methods as m
from app.services import proposal_send as psend
from app.services.rfp_email_auth import INVITATION_METHODS

P1 = "p1"
G1, G2, G3 = "g1", "g2", "g3"
BY_HAND = [{"key": "by_hand", "sources": ["default"]}]


# ── Fake Supabase ─────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._op, self._payload, self._filters = None, None, []

    def select(self, cols, *a, **k):
        self._op = "select"
        self.db.selects.append((self.table, cols))
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def eq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def is_(self, col, val):
        want = None if val == "null" else val
        self._filters.append(lambda r, c=col, v=want: r.get(c) == v)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self._filters.append(lambda r, c=col, v=vals: r.get(c) in v)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self.table in self.db.fail:
            raise RuntimeError(f"{self.table} read failed")
        rows = self.db.tables.setdefault(self.table, [])
        hits = [r for r in rows if all(f(r) for f in self._filters)]
        if self._op == "select":
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "update":
            for r in hits:
                r.update(self._payload)
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            return SimpleNamespace(data=[dict(i) for i in items])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None, fail=()):
        self.tables = tables or {}
        self.fail = set(fail)
        self.selects: list[tuple[str, str]] = []

    def table(self, name):
        return _Query(self, name)


def _settings(**over):
    base = dict(rfp_ingest_enabled=True, rfp_bc_enabled=True, rfp_ngem_enabled=True)
    base.update(over)
    return SimpleNamespace(**base)


def _link(gc_id, name, *, method=None, created_at="2026-01-01T00:00:00+00:00"):
    """A project_gcs row as the nested select returns it."""
    return {
        "id": f"link-{gc_id}",
        "project_id": P1,
        "gc_id": gc_id,
        "needs_by": None,
        "invitation_method": method,
        "pricing_approval_status": None,
        "project_gc_contacts": [],
        "general_contractors": {"id": gc_id, "name": name, "created_at": created_at, "gc_contacts": []},
    }


def _merge(gc_id, method, *, unmerged=None, kind="merged", decided_at="2026-10-01T00:00:00+00:00", mid=None):
    return {
        "id": mid or f"m-{gc_id}-{method}", "project_id": P1, "gc_id": gc_id, "kind": kind,
        "invitation_method": method, "unmerged_at": unmerged, "decided_at": decided_at,
        "gc_match_kind": "contact", "sender_address": "pm@gc.test",
    }


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(db=FakeDB(), settings=_settings(), audits=[])

    def install(tables, *, fail=(), **settings):
        state.db = FakeDB(tables, fail=fail)
        state.settings = _settings(**settings)
        return state.db

    state.install = install
    monkeypatch.setattr(psend, "get_supabase", lambda: state.db)
    monkeypatch.setattr(psend, "get_settings", lambda: state.settings)
    monkeypatch.setattr(pr, "get_supabase", lambda: state.db)
    monkeypatch.setattr(pr, "audit", lambda *a, **k: state.audits.append(a))
    return state


# ── The pure derivation ───────────────────────────────────────────────────


def test_a_gc_with_nothing_reads_by_hand():
    assert m.derive_methods([G1]) == {G1: BY_HAND}


def test_rfp_derived_methods_in_order_with_their_sources():
    out = m.derive_methods(
        [G1, G2],
        created={"source_kind": "rfp_email", "invitation_method": "procore"},
        created_gc_ids={G1},
        merges=[
            {"gc_id": G1, "invitation_method": "smartbid", "decided_at": "2026-10-03"},
            {"gc_id": G1, "invitation_method": "procore", "decided_at": "2026-10-02"},
            {"gc_id": G2, "invitation_method": "organic", "decided_at": "2026-10-01"},
        ],
        portal_rows=[{"gc_id": G2, "portal": "buildingconnected"}],
    )
    assert out[G1] == [
        {"key": "procore", "sources": ["rfp_created", "rfp_merge"]},
        {"key": "smartbid", "sources": ["rfp_merge"]},
    ]
    assert out[G2] == [
        {"key": "organic", "sources": ["rfp_merge"]},
        {"key": "buildingconnected", "sources": ["portal"]},
    ]


def test_user_set_alone_replaces_by_hand():
    out = m.derive_methods([G1, G2], user_set={G1: "other"})
    assert out[G1] == [{"key": "other", "sources": ["user"]}]
    assert out[G2] == BY_HAND


def test_user_set_unions_with_rfp_methods_without_duplicating_a_key():
    out = m.derive_methods(
        [G1],
        merges=[{"gc_id": G1, "invitation_method": "procore"}],
        user_set={G1: "procore"},
    )
    assert out[G1] == [{"key": "procore", "sources": ["rfp_merge", "user"]}]
    out = m.derive_methods(
        [G1],
        merges=[{"gc_id": G1, "invitation_method": "procore"}],
        user_set={G1: "buildingconnected"},
    )
    assert [x["key"] for x in out[G1]] == ["procore", "buildingconnected"]


def test_a_gc_not_on_the_project_is_never_credited():
    out = m.derive_methods(
        [G1],
        created={"source_kind": "rfp_portal", "invitation_method": "buildingconnected"},
        created_gc_ids={G3},
        merges=[{"gc_id": G3, "invitation_method": "procore"}],
        portal_rows=[{"gc_id": G3, "portal": "buildingconnected"}],
    )
    assert out == {G1: BY_HAND}


def test_a_merge_with_no_method_adds_nothing():
    assert m.derive_methods([G1], merges=[{"gc_id": G1, "invitation_method": None}]) == {G1: BY_HAND}


def test_created_method_reads_the_email_method_or_the_portal_key():
    assert m.created_method({"source_kind": "rfp_email", "invitation_method": "pipelinesuite"}) == "pipelinesuite"
    assert m.created_method({"source_kind": "rfp_email", "invitation_method": None}) is None
    assert m.created_method({"source_kind": "rfp_portal", "invitation_method": "buildingconnected"}) == "buildingconnected"
    # An old portal row with no key is NGEM, as the frontend labels it.
    assert m.created_method({"source_kind": "rfp_portal", "invitation_method": None}) == "ngem"
    assert m.created_method(None) is None


def test_plan_created_credits_only_the_gcs_inserted_while_the_creation_ran():
    created = {"gc_plan": "created", "created_at": "2026-10-20T12:00:00+00:00"}
    at = {
        G1: "2026-10-20T12:00:01.5+00:00",   # inserted right after the row
        G2: "2025-03-01T00:00:00+00:00",     # an old directory GC added by hand
        G3: None,
    }
    assert m.gcs_created_by_step(created, at) == {G1}
    assert m.gcs_created_by_step({**created, "gc_plan": "resolved"}, at) == set()


def test_user_settable_methods_cover_every_email_method_plus_portals_and_other():
    assert set(INVITATION_METHODS) <= set(m.USER_SETTABLE_METHODS)
    assert {"buildingconnected", "ngem", "other"} <= set(m.USER_SETTABLE_METHODS)
    assert "by_hand" not in m.USER_SETTABLE_METHODS


def test_normalize_user_method():
    assert m.normalize_user_method(" Procore ") == "procore"
    assert m.normalize_user_method("") is None
    assert m.normalize_user_method("by_hand") is None
    assert m.normalize_user_method(None) is None
    with pytest.raises(ValueError):
        m.normalize_user_method("carrier_pigeon")


# ── The reads, through project_gc_rows ────────────────────────────────────


def _rows():
    return {r["id"]: r for r in psend.project_gc_rows(P1)}


def test_wire_carries_methods_and_the_user_set_value(env):
    env.install({
        "project_gcs": [_link(G1, "Alpha", method="other"), _link(G2, "Bravo")],
        "rfp_project_matches": [_merge(G2, "procore")],
    })
    rows = _rows()
    assert rows[G1]["invitation_method"] == "other"
    assert rows[G1]["methods"] == [{"key": "other", "sources": ["user"]}]
    assert rows[G2]["invitation_method"] is None
    assert rows[G2]["methods"] == [{"key": "procore", "sources": ["rfp_merge"]}]
    # The pre-0173 fields are intact.
    assert rows[G2]["rfp_match_id"] == "m-g2-procore"
    assert rows[G2]["rfp_system_added"]["invitation_method"] == "procore"


def test_unmerged_and_duplicate_rows_are_excluded(env):
    env.install({
        "project_gcs": [_link(G1, "Alpha")],
        "rfp_project_matches": [
            _merge(G1, "procore", unmerged="2026-10-05T00:00:00+00:00"),
            _merge(G1, "smartbid", kind="duplicate"),
        ],
    })
    assert _rows()[G1]["methods"] == BY_HAND


def test_the_creating_email_credits_its_resolved_gc(env):
    env.install({
        "project_gcs": [_link(G1, "Alpha"), _link(G2, "Bravo")],
        "rfp_created_projects": [{
            "project_id": P1, "source_kind": "rfp_email", "invitation_method": "procore",
            "gc_plan": "resolved", "likely_gc_id": None, "rfp_email_id": "e1",
            "portal_invitation_id": None, "created_at": "2026-10-20T12:00:00+00:00",
        }],
        "rfp_emails": [{"id": "e1", "resolved_gc_id": G1}],
    })
    rows = _rows()
    assert rows[G1]["methods"] == [{"key": "procore", "sources": ["rfp_created"]}]
    assert rows[G2]["methods"] == BY_HAND


def test_the_creating_portal_invitation_and_merged_portal_rows(env):
    env.install({
        "project_gcs": [_link(G1, "Alpha"), _link(G2, "Bravo"), _link(G3, "Charlie")],
        "rfp_created_projects": [{
            "project_id": P1, "source_kind": "rfp_portal", "invitation_method": "buildingconnected",
            "gc_plan": "provisional", "likely_gc_id": G1, "rfp_email_id": None,
            "portal_invitation_id": "i1", "created_at": "2026-10-20T12:00:00+00:00",
        }],
        "rfp_portal_invitations": [
            {"id": "i1", "gc_id": G1, "portal": "buildingconnected", "status": "created",
             "created_project_id": P1, "match_project_id": None},
            {"id": "i2", "gc_id": G2, "portal": "buildingconnected", "status": "exists",
             "created_project_id": None, "match_project_id": P1},
            # Still waiting on review: not on the project yet.
            {"id": "i3", "gc_id": G3, "portal": "buildingconnected", "status": "review_match",
             "created_project_id": None, "match_project_id": P1},
        ],
    })
    rows = _rows()
    assert rows[G1]["methods"] == [{"key": "buildingconnected", "sources": ["rfp_created", "portal"]}]
    assert rows[G2]["methods"] == [{"key": "buildingconnected", "sources": ["portal"]}]
    assert rows[G3]["methods"] == BY_HAND


def test_portal_rows_are_dropped_while_their_switch_is_off(env):
    env.install({
        "project_gcs": [_link(G2, "Bravo")],
        "rfp_portal_invitations": [
            {"id": "i2", "gc_id": G2, "portal": "buildingconnected", "status": "exists",
             "match_project_id": P1},
        ],
    }, rfp_bc_enabled=False)
    assert _rows()[G2]["methods"] == BY_HAND


def test_plan_created_credits_the_gc_the_creation_inserted(env):
    env.install({
        "project_gcs": [
            _link(G1, "New GC Inc", created_at="2026-10-20T12:00:02+00:00"),
            _link(G2, "Old GC", created_at="2024-01-01T00:00:00+00:00"),
        ],
        "rfp_created_projects": [{
            "project_id": P1, "source_kind": "rfp_email", "invitation_method": "nonorganic",
            "gc_plan": "created", "likely_gc_id": None, "rfp_email_id": "e1",
            "portal_invitation_id": None, "created_at": "2026-10-20T12:00:00+00:00",
        }],
        "rfp_emails": [{"id": "e1", "resolved_gc_id": None}],
    })
    rows = _rows()
    assert rows[G1]["methods"] == [{"key": "nonorganic", "sources": ["rfp_created"]}]
    assert rows[G2]["methods"] == BY_HAND


def test_rfp_reads_are_skipped_while_rfp_ingestion_is_off(env):
    db = env.install({
        "project_gcs": [_link(G1, "Alpha", method="procore")],
        "rfp_project_matches": [_merge(G1, "smartbid")],
    }, rfp_ingest_enabled=False)
    assert _rows()[G1]["methods"] == [{"key": "procore", "sources": ["user"]}]
    read = {t for t, _ in db.selects}
    assert not read & {"rfp_project_matches", "rfp_created_projects", "rfp_portal_invitations"}


def test_a_failed_rfp_read_degrades_to_the_user_set_method(env):
    env.install({
        "project_gcs": [_link(G1, "Alpha", method="other"), _link(G2, "Bravo")],
    }, fail={"rfp_created_projects"})
    rows = _rows()
    assert rows[G1]["methods"] == [{"key": "other", "sources": ["user"]}]
    assert rows[G2]["methods"] == BY_HAND


# ── PATCH /projects/{id}/gcs/{gc_id} ──────────────────────────────────────


def _patch_env(env, **settings):
    return env.install({
        "projects": [{"id": P1, "name": "Job", "current_stage": "send_out", "abandoned_at": None}],
        "project_gcs": [_link(G1, "Alpha")],
        "rfp_project_matches": [_merge(G1, "procore")],
    }, **settings)


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="u@g3.test", role=role, is_active=True)


def test_patch_sets_then_clears_the_method(env):
    db = _patch_env(env)
    out = pr.update_project_gc(P1, G1, ProjectGCUpdate(invitation_method="buildingconnected"), user=_user())
    assert db.tables["project_gcs"][0]["invitation_method"] == "buildingconnected"
    assert out[0]["invitation_method"] == "buildingconnected"
    assert [x["key"] for x in out[0]["methods"]] == ["procore", "buildingconnected"]
    assert env.audits[-1][1] == "project.gc_update"
    assert env.audits[-1][4] == {"gc_id": G1, "invitation_method": "buildingconnected"}

    out = pr.update_project_gc(P1, G1, ProjectGCUpdate(invitation_method=None), user=_user())
    assert db.tables["project_gcs"][0]["invitation_method"] is None
    assert out[0]["invitation_method"] is None
    assert out[0]["methods"] == [{"key": "procore", "sources": ["rfp_merge"]}]
    assert env.audits[-1][4] == {"gc_id": G1, "invitation_method": None}


def test_patch_without_the_key_leaves_the_method_alone(env):
    db = _patch_env(env)
    db.tables["project_gcs"][0]["invitation_method"] = "other"
    pr.update_project_gc(P1, G1, ProjectGCUpdate(needs_by=None), user=_user())
    assert db.tables["project_gcs"][0]["invitation_method"] == "other"


def test_patch_404s_for_a_gc_not_on_the_project(env):
    _patch_env(env)
    with pytest.raises(pr.HTTPException) as exc:
        pr.update_project_gc(P1, G2, ProjectGCUpdate(invitation_method="procore"), user=_user())
    assert exc.value.status_code == 404


def test_the_body_rejects_an_unknown_method_and_normalizes_by_hand():
    with pytest.raises(ValidationError):
        ProjectGCUpdate.model_validate({"invitation_method": "carrier_pigeon"})
    body = ProjectGCUpdate.model_validate({"invitation_method": "by_hand"})
    assert body.invitation_method is None and "invitation_method" in body.model_fields_set


def test_the_patch_route_keeps_the_writer_and_bidding_gates():
    route = next(
        r for r in pr.router.routes
        if r.path == "/projects/{project_id}/gcs/{gc_id}" and "PATCH" in r.methods
    )
    calls = {d.call for d in route.dependant.dependencies}
    assert pr._BIDDING_ONLY[0].dependency in calls
    assert require_writer in {d.call for d in route.dependant.dependencies} | {
        sub.call for d in route.dependant.dependencies for sub in d.dependencies
    }


@pytest.fixture
def api(env):
    app = FastAPI()
    app.include_router(pr.router)
    who = {"role": Role.ESTIMATING_ADMIN}
    app.dependency_overrides[get_current_user] = lambda: _user(who["role"])
    client = TestClient(app, raise_server_exceptions=True)

    def as_(role):
        who["role"] = role
        return client

    return as_


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.ESTIMATOR])
def test_read_only_and_external_roles_get_403(env, api, role):
    db = _patch_env(env)
    res = api(role).patch(f"/projects/{P1}/gcs/{G1}", json={"invitation_method": "procore"})
    assert res.status_code == 403
    assert db.tables["project_gcs"][0]["invitation_method"] is None


@pytest.mark.parametrize("role", sorted(
    set(Role) - {Role.ACCOUNTANT, Role.ESTIMATOR}, key=lambda r: r.value
))
def test_every_writer_may_set_the_method_over_http(env, api, role):
    db = _patch_env(env)
    res = api(role).patch(f"/projects/{P1}/gcs/{G1}", json={"invitation_method": "smartbid"})
    assert res.status_code == 200, res.text
    assert db.tables["project_gcs"][0]["invitation_method"] == "smartbid"
    assert res.json()[0]["methods"][-1] == {"key": "smartbid", "sources": ["user"]}


def test_an_unknown_method_is_422_over_http(env, api):
    db = _patch_env(env)
    res = api(Role.EXECUTIVE).patch(f"/projects/{P1}/gcs/{G1}", json={"invitation_method": "fax"})
    assert res.status_code == 422
    assert db.tables["project_gcs"][0]["invitation_method"] is None
