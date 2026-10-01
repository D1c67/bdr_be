"""The BuildingConnected portal (app/services/rfp_bc_portal) and its touch
points in app/services/rfp_portal_ingest, against the in-memory fake
Supabase from tests/test_rfp_email_ingest (extended by tests/test_rfp_portal_ingest)
and the Bid Board API faked through httpx.MockTransport over the anonymised
rows in tests/fixtures_bc (docs/RFP_BUILDINGCONNECTED.md; scratchpad
BUILD_CONTRACT.md D2, D3, D7, D10 to D13, D18, D21 to D25, D29, D32, D33).

Pinned, in the contract's order:

- the scheduler: the newest incremental slot only, the full slot inside its
  catch-up window, a DST day, the (portal, kind, slot) ledger, the tick's
  per-portal gates and the bench pause, Run now with a kind;
- the scan: entry status per fixture role, the masked row, the hash short
  circuit (no write of its own), the request budget per API page (one
  select, one chunked seen-stamp update for the page's unchanged rows, one
  write per new or changed row, the sibling index read once and only when
  an insert needs it, a mid-pull failure raised after the rows already
  pulled land), the change log and its bell (linked rows only), the scan-time
  sibling link, the system ignore, the NDA un-masking, missing / withdrawn /
  expired on a full sync only, the run counters and the high water mark
  written only by the winning completion, a 429 mid-pull as a transient
  with no high water advance, a disconnected connection parking the run,
  the scan-failed bell by portal;
- the match branch: alias + confident -> exists with the link, the contact
  and the files flag; domain + duplicate -> marker only; provisional ->
  review_match / match_gc_unresolved; masked -> nda_required; no due date;
  SUBMITTED unmatched; a confirmed GC left alone; a reference-only name;
  no candidate -> create and the create step in the same pass; the create
  gates; the merge refused by a closed project;
- the human actions on BuildingConnected rows: resolve_new, resolve_exists,
  reopen (the link removed by id), restore, confirm_gc, apply_dates;
- the reads: the lanes and nulls-last ordering, the state filter, the item
  fields, portal_sources_for_projects for exists and created rows, the
  status block and the detail.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core.config import Settings
from app.core.roles import Role
from app.services import bc_client, bc_facts, gc_aliases, llm, llm_health, llm_queue, rfp_create, rfp_email_ingest
from app.services import rfp_bc_portal as bcp
from app.services import rfp_portal_ingest as pi
from app.services import rfp_test
from tests.test_rfp_email_ingest import _Snapshot
from tests.test_rfp_portal_ingest import PortalDB, PortalQuery, _review_users

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)   # 05:00 PDT, the fixtures' frozen now
PT = ZoneInfo("America/Los_Angeles")
# The real bench check, taken before the autouse environment stubs it out.
_REAL_ACTIVE_SESSION = rfp_test.active_session
FIXTURES = Path(__file__).resolve().parent / "fixtures_bc"
OPPS = json.loads((FIXTURES / "opportunities.json").read_text(encoding="utf-8"))
ME = json.loads((FIXTURES / "users_me.json").read_text(encoding="utf-8"))
BY_ROLE = {r["_fixture_role"]: r for r in OPPS}
API_PREFIX = "/construction/buildingconnected/v2"
REDIRECT = "http://localhost:5051/rfp-portal/buildingconnected/callback"
ACCESS = "access-token-never-logged"
BC = "buildingconnected"
U1 = "u-1"
ADMIN_USER = f"u-{Role.ESTIMATING_ADMIN.value}"
G1 = "6a000000-0000-4000-8000-000000000001"
G2 = "6a000000-0000-4000-8000-000000000002"
P1 = "5a000000-0000-4000-8000-000000000001"
P2 = "5a000000-0000-4000-8000-000000000002"
INV = "inv-bc-1"
GC_AA = BY_ROLE["open_undecided_full"]["client"]["company"]["id"]
GC_AM = BY_ROLE["same_gc_two_packages_a"]["client"]["company"]["id"]
FULL_ROLES = ("open_undecided_full", "with_trade_instructions", "open_undecided_min", "open_accepted",
              "budget_request", "no_due_notice", "no_due_real")   # the entry rule admits these at NOW


# ── Fake Supabase additions ──────────────────────────────────────────────


class BcQuery(PortalQuery):
    """PortalQuery plus `order(..., nullsfirst=False)` (PostgREST's
    `.nullslast`), which the BuildingConnected list uses for close_at."""

    def order(self, col, desc=False, **k):
        self._order.append((col, bool(desc)))
        nulls = getattr(self, "_nulls", None) or {}
        nulls[col] = k.get("nullsfirst")
        self._nulls = nulls
        return self

    def _sorted(self, rows):
        nulls = getattr(self, "_nulls", None) or {}
        for col, desc in reversed(self._order):
            nf = nulls.get(col)
            if nf is None:
                rows.sort(key=lambda r: (r.get(col) is None, r.get(col) if r.get(col) is not None else ""), reverse=desc)
                continue
            rows.sort(key=lambda r: r.get(col) if r.get(col) is not None else "", reverse=desc)
            present = [r for r in rows if r.get(col) is not None]
            absent = [r for r in rows if r.get(col) is None]
            rows[:] = (absent + present) if nf else (present + absent)
        return rows


class BcDB(PortalDB):
    """PortalDB with the 0136 shape of the run slot index (unique per
    portal, kind and slot) and the new tables' uniques."""

    unique = {
        **PortalDB.unique,
        "gc_external_aliases": [("source", "external_id")],
        "rfp_oauth_connections": [("provider",)],
        "rfp_portal_state": [("portal",)],
    }

    def table(self, name):
        query = BcQuery(self, name)
        inherited = query._check_unique
        if name == "rfp_portal_runs":

            def check_runs(rows, payload):
                inherited(rows, payload)
                for r in rows:
                    if r.get("portal") != payload.get("portal"):
                        continue
                    same_slot = payload.get("scheduled_for") and r.get("scheduled_for") == payload.get("scheduled_for")
                    same_kind = (r.get("kind") or "incremental") == (payload.get("kind") or "incremental")
                    if same_slot and same_kind:
                        raise Exception('duplicate key value violates unique constraint "rfp_portal_runs_slot_uidx" (23505)')
                    if r.get("status", "queued") in ("queued", "running") and payload.get("status", "queued") in ("queued", "running"):
                        raise Exception('duplicate key value violates unique constraint "rfp_portal_runs_active_uidx" (23505)')

            query._check_unique = check_runs
        if name == "llm_jobs":

            def check_jobs(rows, payload):
                inherited(rows, payload)
                for r in rows:
                    if (
                        r.get("job_type") == payload.get("job_type")
                        and r.get("target_id") == payload.get("target_id")
                        and r.get("status", "queued") in ("queued", "running")
                    ):
                        raise Exception('duplicate key value violates unique constraint "llm_jobs_active_target_uq" (23505)')

            query._check_unique = check_jobs
        return query


# ── The faked Bid Board ──────────────────────────────────────────────────


class FakeBoard:
    """MockTransport handler for the Bid Board API: pages the rows it holds
    (cursorState), honours `filter[updatedAt]` the way the real API does
    (rows with a null updatedAt never match a filter), and answers each
    API call with the next of `modes` (the last one repeats)."""

    def __init__(self, rows=None, *, page_size=100):
        self.rows = [copy.deepcopy(r) for r in (OPPS if rows is None else rows)]
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.modes: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if not path.startswith(API_PREFIX):
            return httpx.Response(404, json={"message": "no such route"})
        mode = self.modes.pop(0) if len(self.modes) > 1 else (self.modes[0] if self.modes else "ok")
        if mode == 429:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"message": "Too Many Requests"})
        if mode == 500:
            return httpx.Response(500, json={"message": "Internal Server Error"})
        if mode == 401:
            return httpx.Response(401, json={"developerMessage": "bad token"})
        sub = path[len(API_PREFIX):]
        if sub == "/users/me":
            return httpx.Response(200, json=ME)
        if sub == "/opportunities":
            params = dict(request.url.params)
            rows = self.rows
            since = params.get("filter[updatedAt]")
            if since:
                lo = bc_facts.parse_ts(since.split("..", 1)[0])
                rows = [r for r in rows if r.get("updatedAt") and bc_facts.parse_ts(r["updatedAt"]) >= lo]
            size = min(int(params.get("limit", "100")), self.page_size)
            start = int(params.get("cursorState", "0"))
            page = rows[start:start + size]
            nxt = start + size
            pagination = {"limit": size, "cursorState": str(nxt)} if nxt < len(rows) else {"limit": size}
            return httpx.Response(200, json={"pagination": pagination, "results": page})
        return httpx.Response(404, json={"message": "no such path"})

    @property
    def filters(self) -> list[str | None]:
        return [dict(r.url.params).get("filter[updatedAt]") for r in self.requests if r.url.path.endswith("/opportunities")]

    @property
    def page_calls(self) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith("/opportunities"))


def _opp(role, **over):
    row = copy.deepcopy(BY_ROLE[role])
    row.update(over)
    return row


# ── Fixtures ─────────────────────────────────────────────────────────────


def _settings(**over) -> Settings:
    base = dict(
        rfp_ingest_enabled=True, rfp_bc_enabled=True, building_connected_client_id="client-id",
        building_connected_client_secret="s3cr3t-never-logged", rfp_bc_redirect_url=REDIRECT,
        llm_queue_enabled=True, rfp_ngem_enabled=False, rfp_match_auto_merge_enabled=False,
        environment="development",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def _config() -> bc_client.BcConfig:
    return bc_client.config_from_settings(_settings())


def _connection(**over):
    row = {
        "provider": BC, "status": "connected", "access_token": ACCESS, "refresh_token": "refresh-never-logged",
        "expires_at": pi._iso(NOW + timedelta(hours=1)), "scope": "data:read", "connected_by": U1,
        "connected_at": pi._iso(NOW - timedelta(days=3)), "external_user_id": ME["id"],
        "external_user_name": "Fixture Admin", "external_user_email": ME["email"],
        "external_company_id": ME["companyId"], "view_all": True, "last_refresh_at": None,
        "last_used_at": None, "last_error": None, "refresh_lock_until": None, "refresh_lock_owner": None,
        "disconnected_at": None,
    }
    row.update(over)
    return row


def _run(**over):
    row = {"id": "run-bc", "portal": BC, "trigger": "scheduled", "kind": "incremental",
           "scheduled_for": pi._iso(NOW - timedelta(minutes=15)), "requested_by": None, "status": "queued",
           "claimed_by": None, "started_at": None, "finished_at": None, "pages_scanned": 0,
           "invitations_seen": 0, "invitations_new": 0, "invitations_changed": 0,
           "invitations_skipped_closed": 0, "rows_pulled": 0, "rows_pipeline": 0, "rows_expired": 0,
           "rows_withdrawn": 0, "high_water_before": None, "last_error": None, "next_attempt_at": None,
           "created_at": pi._iso(NOW - timedelta(minutes=15))}
    row.update(over)
    return row


def _bc_row(role="open_undecided_full", **over):
    """A stored BuildingConnected invitation built the way the scan builds
    it from the fixture, at `match` with the pipeline bookkeeping."""
    fields = bc_facts.invitation_fields(BY_ROLE[role], NOW, text_max_chars=20000)
    row = {
        "id": INV, "portal": BC, **fields, "addendum_no": None, "status": "match", "flag_reason": None,
        "decided_at_step": None, "attempts": 0, "next_attempt_at": None, "last_error": None,
        "match_candidates": None, "match_project_id": None, "match_score": None, "match_llm_model": None,
        "match_llm_prompt_version": None, "match_weights": None, "matched_at": None,
        "possible_rebid_project_id": None, "possible_rebid_score": None, "excluded_project_ids": [],
        "resolution": None, "resolved_by": None, "resolved_at": None, "reopen_reason": None,
        "ignored_by": None, "ignored_at": None, "ignore_reason": None, "ignore_source": None,
        "harvest_id": None, "harvested_at": None, "change_log": [], "first_seen_at": pi._iso(NOW - timedelta(days=2)),
        "last_seen_at": pi._iso(NOW - timedelta(days=1)), "first_seen_run_id": "run-0", "last_seen_run_id": "run-0",
        "seen_count": 1, "missing_since": None, "created_at": pi._iso(NOW - timedelta(days=2)),
        "updated_at": pi._iso(NOW - timedelta(days=1)), "created_project_id": None, "gc_id": None,
        "gc_kind": None, "gc_candidates": None, "gc_confirmed_at": None, "gc_confirmed_by": None,
        "project_gc_id": None, "sibling_of": None,
    }
    row.update(over)
    return row


def _project(pid, name, *, bid_at, number="26.9.7300", stage="rfqs", gcs=(), notes=None):
    return {"id": pid, "name": name, "number": number, "current_stage": stage,
            "internal_bid_at": bid_at, "actual_bid_at": None, "bid_notes": notes,
            "project_gcs": [{"id": f"pg-{g}", "gc_id": g, "needs_by": None, "general_contractors": {"name": "x"}} for g in gcs],
            "abandoned_at": None}


DUE_01 = "2026-10-16T23:00:00+00:00"   # open_undecided_full's due instant (16:00 PT)


@pytest.fixture
def db():
    fake = BcDB({
        "rfp_portal_runs": [], "rfp_portal_invitations": [], "rfp_portal_state": [],
        "rfp_oauth_connections": [_connection()], "gc_external_aliases": [], "rfp_harvests": [],
        "llm_jobs": [], "notifications": [], "projects": [], "general_contractors": [], "gc_contacts": [],
        "project_gcs": [], "project_gc_contacts": [], "profiles": _review_users(), "graph_sync_state": [],
        "audit_log": [],
    })
    fake.defaults = {
        **PortalDB.defaults,
        "llm_jobs": {"status": lambda: "queued", "attempts": lambda: 0, "created_at": lambda: NOW.isoformat()},
        "rfp_portal_runs": {"status": lambda: "queued", "created_at": lambda: NOW.isoformat(),
                            "pages_scanned": lambda: 0, "kind": lambda: "incremental"},
        "rfp_portal_invitations": {"attempts": lambda: 0, "excluded_project_ids": lambda: [],
                                   "change_log": lambda: [], "created_at": lambda: NOW.isoformat()},
    }
    return fake


@pytest.fixture
def settings():
    return _settings()


@pytest.fixture
def files(monkeypatch):
    """The files flag service seam: records set_needed calls; `has` says
    whether the project already has files."""
    calls = []
    state = SimpleNamespace(has=False)
    stub = SimpleNamespace(
        project_has_files=lambda sb, project_id: state.has,
        set_needed=lambda sb, project_id, *, source, url: calls.append((project_id, source, url)),
    )
    monkeypatch.setattr(bcp, "_files_needed", lambda: stub)
    state.calls = calls
    return state


@pytest.fixture
def lead_contact(monkeypatch):
    calls = []

    def fake(sb, gc_id, lead):
        calls.append((gc_id, lead))
        return ("c-lead", True) if (lead or {}).get("email") else (None, False)

    monkeypatch.setattr(rfp_create, "ensure_lead_contact", fake, raising=False)
    return calls


@pytest.fixture(autouse=True)
def _env(monkeypatch, db, settings, files, lead_contact):
    monkeypatch.setattr(pi, "get_settings", lambda: settings)
    monkeypatch.setattr(pi, "get_supabase", lambda: db)
    monkeypatch.setattr(pi, "_now", lambda: NOW)
    monkeypatch.setattr(bcp, "get_settings", lambda: settings)
    monkeypatch.setattr(bc_client, "_now", lambda: NOW)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: settings)
    monkeypatch.setattr(rfp_email_ingest, "get_settings", lambda: settings)
    monkeypatch.setattr(rfp_email_ingest, "get_supabase", lambda: db)
    monkeypatch.setattr(pi.rfp_test, "active_session", lambda sb: None)
    monkeypatch.setattr(
        pi, "notify_role",
        lambda role, project_id, type_, message, **k: db.tables["notifications"].append(
            {"type": type_, "message": message, "role": role, "user_id": None, "read_at": None,
             "dismissed_at": None, "metadata": k.get("metadata"), "mirror_email": k.get("mirror_email", True)}),
    )

    def fake_notify_user(user_id, project_id, type_, message, **k):
        profile = next((p for p in db.tables["profiles"] if p.get("id") == user_id), {})
        db.tables["notifications"].append(
            {"type": type_, "message": message, "role": Role(profile["role"]) if profile.get("role") else None,
             "user_id": user_id, "read_at": None, "dismissed_at": None,
             "metadata": k.get("metadata"), "mirror_email": k.get("mirror_email", True)})
    monkeypatch.setattr(pi, "notify_user", fake_notify_user)
    monkeypatch.setattr(
        pi, "audit",
        lambda actor, action, entity, entity_id, payload=None: db.tables["audit_log"].append(
            {"actor_id": actor, "action": action, "entity": entity, "entity_id": entity_id, "payload": payload}),
    )
    monkeypatch.setattr(llm, "is_configured", lambda feature, settings=None: True)
    monkeypatch.setattr(llm, "active_model", lambda feature, settings=None: "test-model")
    monkeypatch.setattr(llm_health, "cached", lambda settings=None, force=False: _Snapshot())


@pytest.fixture
def llm_answer(monkeypatch):
    calls = []

    def _set(result):
        def fake(feature, **kwargs):
            calls.append({"feature": feature, **kwargs})
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        monkeypatch.setattr(llm, "complete_json", fake)
        return calls
    return _set


SAME = {"verdicts": [{"index": 0, "verdict": "same", "confidence": 0.95, "reasoning": "same job"}]}


@pytest.fixture
def board_transport(monkeypatch):
    """The execute_scan path: the adapter opens its own BcClient; the
    transport, the sleep and the clock are injected here, and the queue
    lease renewal is a no-op (no claimed job in these tests)."""
    holder = SimpleNamespace(board=FakeBoard())

    class TestClient(bc_client.BcClient):
        def __init__(self, sb, config, **kw):
            kw.setdefault("sleep", lambda s: None)
            kw.setdefault("now", lambda: NOW)
            super().__init__(sb, config, **kw)

    monkeypatch.setattr(bc_client, "BcClient", TestClient)
    monkeypatch.setattr(
        bc_client, "_client",
        lambda config, transport: httpx.Client(transport=httpx.MockTransport(holder.board), http2=False),
    )
    monkeypatch.setattr(pi, "_renew", lambda: None)
    return holder


def _session(db, board) -> bc_client.BcClient:
    return bc_client.BcClient(db, _config(), transport=httpx.MockTransport(board), sleep=lambda s: None, now=lambda: NOW)


def _portal() -> bcp.BuildingConnectedPortal:
    return bcp.BuildingConnectedPortal()


def _inv(db, inv_id=INV):
    return next(r for r in db.tables["rfp_portal_invitations"] if r["id"] == inv_id)


def _by_external(db, external_id):
    return next(r for r in db.tables["rfp_portal_invitations"] if r.get("external_id") == external_id)


def _bells(db, type_):
    return [n for n in db.tables["notifications"] if n["type"] == type_]


def _jobs(db):
    return [j for j in db.tables["llm_jobs"] if j["job_type"] == pi.JOB_SCAN]


def _state(db):
    rows = db.tables["rfp_portal_state"]
    return rows[0] if rows else None


def _scan(db, run, board=None, settings=None):
    board = board or FakeBoard()
    db.tables["rfp_portal_runs"].append(run) if run not in db.tables["rfp_portal_runs"] else None
    with _session(db, board) as session:
        return _portal().scan(db, settings or _settings(), run, session, lambda: None), board


# ── Registry, statuses, lanes ────────────────────────────────────────────


def test_registry_lazily_builds_the_adapter_and_the_vocabulary_gains_the_parked_statuses():
    adapter = pi._portal(BC)
    assert isinstance(adapter, bcp.BuildingConnectedPortal) and adapter.key == BC
    assert pi.STATUS_PARKED == ("historical", "expired", "withdrawn")
    assert set(pi.STATUS_PARKED) <= set(pi.ALL_STATUSES)
    assert not set(pi.STATUS_PARKED) & set(pi.STATUS_PENDING)      # the sweep never touches them
    assert not set(pi.STATUS_PARKED) & set(pi.IGNORABLE_STATUSES)
    # NGEM's lanes are untouched; the BC lanes are the contract's.
    assert list(pi.VIEWS) == ["review", "new", "existing", "ignored", "all"]
    assert pi.VIEWS_BY_PORTAL["ngem"] is pi.VIEWS
    assert pi.VIEWS_BY_PORTAL[BC] == {
        "needs_action": ("review_match",), "open": ("match", "create", "done"),
        "existing": ("exists", "created"), "historical": ("historical",),
        "expired_withdrawn": ("expired", "withdrawn"), "ignored": ("ignored",), "all": pi.ALL_STATUSES,
    }
    assert pi._map_route_status("done") == "harvest" and pi._map_route_status("done", no_match="create") == "create"
    assert pi._map_route_status("merged", no_match="create") == "exists"
    assert "payload" not in pi._LIST_SELECT and "payload" in pi._DETAIL_SELECT
    for col in ("gc_kind", "gc_external_id", "submission_state", "is_nda_required", "lead", "project_gc_id"):
        assert col in pi._SWEEP_SELECT


def test_adapter_surface_classify_availability_and_priority(db):
    adapter = _portal()
    s = _settings()
    assert adapter.configured(s) is True and adapter.entry_url(s) is None and adapter.is_stale_link(Exception()) is False
    assert adapter.parse_bid_number(" 45dcb ") == ("45dcb", None) and adapter.parse_bid_number("") == (None, None)
    assert adapter.account(s) == "Fixture Admin"
    assert adapter.availability(s) == (True, None, None)
    db.tables["rfp_oauth_connections"][0].update(status="disconnected", last_error="refresh rejected")
    usable, reason, until = adapter.availability(s)
    assert usable is False and reason == "refresh rejected" and until is None
    assert adapter.availability(_settings(building_connected_client_secret=""))[0] is False
    assert adapter.classify(bc_client.BcDisconnected("x")) == pi.KIND_UNAVAILABLE
    assert adapter.classify(bc_client.BcRateLimited("x")) == pi.KIND_TRANSIENT
    assert adapter.classify(bc_client.BcTransient("x")) == pi.KIND_TRANSIENT
    assert adapter.classify(bc_client.BcPermanent("x")) == pi.KIND_PERMANENT
    assert adapter.classify(httpx.ConnectError("x")) == pi.KIND_TRANSIENT
    assert adapter.classify(ValueError("x")) == pi.KIND_UNKNOWN
    assert adapter.is_client_error(bc_client.BcPermanent("x")) and not adapter.is_client_error(ValueError())
    err = adapter.unavailable_error(None, None)
    assert err.code == bcp.CODE_NOT_CONNECTED and err.http_status == 409
    assert pi._scan_priority(s, BC, "incremental") == 140 == s.rfp_bc_scan_queue_priority
    assert pi._scan_priority(s, BC, "full") == 150 == s.rfp_bc_full_sync_queue_priority
    assert pi._scan_priority(s, "ngem", None) == s.rfp_ngem_scan_queue_priority
    assert pi._scan_priority(_settings(rfp_bc_scan_queue_priority=133), BC, None) == 133


# ── Scheduler (D3) ───────────────────────────────────────────────────────


def test_due_slots_claim_the_newest_incremental_slot_only():
    s = _settings(rfp_bc_poll_minutes=15)
    at = datetime(2026, 9, 26, 12, 7, 30, tzinfo=timezone.utc)
    slots = bcp.due_slots(at, s)
    inc = [slot for slot, kind in slots if kind == "incremental"]
    assert inc == [datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)]
    # Two hours later after downtime: still exactly one incremental slot, the newest.
    later = at + timedelta(hours=2, minutes=8)
    assert [slot for slot, kind in bcp.due_slots(later, s) if kind == "incremental"] == [
        datetime(2026, 9, 26, 14, 15, tzinfo=timezone.utc)
    ]
    assert bcp.incremental_slot(datetime(2026, 9, 26, 12, 14, 59, tzinfo=timezone.utc), s) == datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert bcp.incremental_slot(datetime(2026, 9, 26, 12, 15, tzinfo=timezone.utc), s) == datetime(2026, 9, 26, 12, 15, tzinfo=timezone.utc)
    # The grid is the floor of the instant, so any zone gives the same slot.
    local = datetime(2026, 9, 26, 5, 7, tzinfo=PT)   # 12:07 UTC
    assert bcp.incremental_slot(local, s) == datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert bcp.incremental_slot(at, _settings(rfp_bc_poll_minutes=60)) == datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert bcp.incremental_slot(datetime(2026, 9, 26, 12, 47, tzinfo=timezone.utc), _settings(rfp_bc_poll_minutes=20)) == datetime(2026, 9, 26, 12, 40, tzinfo=timezone.utc)


def test_due_slots_full_sync_inside_its_catch_up_window():
    s = _settings(rfp_bc_full_sync_time="02:30", rfp_bc_full_sync_catchup_hours=4)
    full = datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc)    # 02:30 PDT

    def fulls(at):
        return [slot for slot, kind in bcp.due_slots(at, s) if kind == "full"]

    assert fulls(full - timedelta(seconds=1)) == []
    assert fulls(full) == [full]
    assert fulls(full + timedelta(hours=3, minutes=59)) == [full]
    assert fulls(full + timedelta(hours=4)) == []
    # The full slot comes first in the list, so it wins the active index when both are due.
    assert [kind for _slot, kind in bcp.due_slots(full + timedelta(minutes=5), s)] == ["full", "incremental"]
    # A late slot's window survives midnight Pacific (yesterday's slot rides along).
    late = _settings(rfp_bc_full_sync_time="23:00", rfp_bc_full_sync_catchup_hours=4)
    after_midnight = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)   # 01:00 PDT on the 27th
    assert [slot for slot, kind in bcp.due_slots(after_midnight, late) if kind == "full"] == [
        datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)
    ]
    nxt_inc, nxt_full = bcp.next_slots(full + timedelta(minutes=5), s)
    assert nxt_inc == datetime(2026, 9, 26, 9, 45, tzinfo=timezone.utc)
    assert nxt_full == datetime(2026, 9, 27, 9, 30, tzinfo=timezone.utc)


def test_full_sync_slots_are_pacific_wall_clock_across_the_dst_change():
    s = _settings(rfp_bc_full_sync_time="06:30")
    # 2026-03-08 is the spring-forward day in America/Los_Angeles.
    on_the_day = datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)   # 13:00 PDT
    slots = bcp.full_sync_slots(on_the_day, s)
    assert [d.strftime("%Y-%m-%d %H:%M") for d in slots] == ["2026-03-07 14:30", "2026-03-08 13:30"]   # PST then PDT
    # The incremental grid does not care about the zone change.
    assert bcp.incremental_slot(on_the_day, s) == on_the_day


def test_claim_slot_keys_on_portal_kind_and_slot(db):
    slot = datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc)
    full = pi.claim_slot(db, BC, slot, kind="full")
    assert full and full["kind"] == "full" and full["trigger"] == "scheduled"
    assert pi.claim_slot(db, BC, slot, kind="full") is None                 # someone else's
    assert pi.claim_slot(db, BC, slot, kind="incremental") is None          # a run is active
    db.tables["rfp_portal_runs"][0]["status"] = "complete"
    inc = pi.claim_slot(db, BC, slot, kind="incremental")                   # same instant, other kind
    assert inc and inc["kind"] == "incremental"
    assert pi.claim_slot(db, "ngem", slot) is not None                      # NGEM's default kind
    assert db.tables["rfp_portal_runs"][-1]["kind"] == "incremental"


def test_poll_once_claims_the_bc_slots_at_their_priorities_and_honours_the_gates(db, settings, monkeypatch):
    # At NOW (05:00 PDT) the 02:30 full slot is inside its window and the
    # 12:00 UTC incremental slot is due; the full one claims first, the
    # incremental yields to the active index this tick.
    pi.poll_once()
    runs = db.tables["rfp_portal_runs"]
    assert [(r["portal"], r["kind"]) for r in runs] == [(BC, "full")]
    assert runs[0]["scheduled_for"] == pi._iso(datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc))
    jobs = _jobs(db)
    assert len(jobs) == 1 and jobs[0]["target_id"] == runs[0]["id"] and jobs[0]["priority"] == 150
    runs[0]["status"] = "complete"
    jobs[0]["status"] = "complete"
    pi.poll_once()
    runs = db.tables["rfp_portal_runs"]
    assert [(r["portal"], r["kind"]) for r in runs] == [(BC, "full"), (BC, "incremental")]
    assert runs[1]["scheduled_for"] == pi._iso(NOW)
    assert _jobs(db)[-1]["priority"] == 140
    # No NGEM run: its slice is off in these settings.
    assert not [r for r in runs if r["portal"] == "ngem"]
    # The bench pause applies to BuildingConnected too (D10).
    db.tables["rfp_portal_runs"].clear()
    db.tables["llm_jobs"].clear()
    monkeypatch.setattr(pi.rfp_test, "active_session", lambda sb: {"id": "bench"})
    pi.poll_once()
    assert db.tables["rfp_portal_runs"] == []
    # Neither portal active: the tick returns before touching the DB.
    monkeypatch.setattr(pi.rfp_test, "active_session", lambda sb: None)
    monkeypatch.setattr(pi, "get_settings", lambda: _settings(rfp_bc_enabled=False))
    pi.poll_once()
    assert db.tables["rfp_portal_runs"] == []


def test_run_now_takes_a_kind_and_answers_not_connected(db):
    run = pi.run_now(db, U1, BC, kind="full")
    assert run["portal"] == BC and run["kind"] == "full" and run["trigger"] == "manual"
    assert _jobs(db)[0]["priority"] == 150
    assert db.tables["audit_log"][-1]["payload"]["kind"] == "full"
    with pytest.raises(pi.RfpPortalError) as active:
        pi.run_now(db, U1, BC)
    assert active.value.code == pi.CODE_RUN_ACTIVE
    db.tables["rfp_portal_runs"].clear()
    with pytest.raises(pi.RfpPortalError) as bad:
        pi.run_now(db, U1, BC, kind="weekly")
    assert bad.value.code == pi.CODE_REASON_INVALID
    db.tables["rfp_oauth_connections"][0]["status"] = "disconnected"
    with pytest.raises(pi.RfpPortalError) as off:
        pi.run_now(db, U1, BC)
    assert off.value.code == bcp.CODE_NOT_CONNECTED and off.value.http_status == 409
    assert db.tables["rfp_portal_runs"] == []


def test_run_now_answers_503_while_the_bc_slice_is_inactive(db, monkeypatch):
    monkeypatch.setattr(pi, "get_settings", lambda: _settings(rfp_bc_enabled=False))
    with pytest.raises(pi.RfpPortalError) as exc:
        pi.run_now(db, U1, BC)
    assert exc.value.code == pi.CODE_NOT_AVAILABLE and exc.value.http_status == 503
    assert "BuildingConnected" in str(exc.value)


# ── The scan (3.2, 3.3) ──────────────────────────────────────────────────


def test_full_scan_inserts_every_row_by_the_entry_rule(db):
    (pages, counts), board = _scan(db, _run(kind="full"))
    rows = db.tables["rfp_portal_invitations"]
    assert len(rows) == 24 and pages == 1 and board.filters == [None]
    by_role = {BY_ROLE[role]["id"]: role for role in BY_ROLE}
    statuses = {by_role[r["external_id"]]: r["status"] for r in rows}
    assert {role for role, status in statuses.items() if status == "match"} == set(FULL_ROLES)
    assert all(status == "historical" for role, status in statuses.items() if role not in FULL_ROLES)
    assert counts["rows_pulled"] == 24 == counts["seen"]
    assert counts["new"] == 7 == counts["rows_pipeline"] and counts["skipped_closed"] == 17
    assert counts["changed"] == 0 and counts["rows_expired"] == 0 and counts["rows_withdrawn"] == 0
    assert counts["high_water_before"] is None and counts["high_water_at"] == NOW and counts["kind"] == "full"
    first = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert first["agency_key"] == "bc" and first["bid_number"] == first["external_id"] == first["bid_number_raw"]
    assert first["title"] == "Fixture Project 01" and first["agency"] == "GC AA Builders"
    assert first["view_url"] == first["external_url"] == bc_facts.deep_link(first["external_id"])
    assert first["close_at"] == DUE_01 and first["issued_on"] == "2026-09-16"
    assert first["gc_external_id"] == GC_AA and first["lead"]["email"] == "lead00@gcaabuilders.example.com"
    assert first["first_seen_run_id"] == "run-bc" and first["seen_count"] == 1 and first["payload"]["id"] == first["external_id"]
    assert "_fixture_role" not in first["payload"]
    masked = _by_external(db, BY_ROLE["nda_masked"]["id"])
    assert masked["agency"] == "(NDA)" and masked["is_nda_required"] is True and masked["gc_external_id"] is None
    assert masked["status"] == "historical"        # past due at NOW
    # The scan itself never writes the state row: only the winning completion does.
    assert _state(db) is None and db.tables["notifications"] == []


def test_incremental_scan_filters_on_the_high_water_minus_the_overlap_and_short_circuits_on_the_hash(db):
    db.tables["rfp_portal_state"].append({"portal": BC, "high_water_at": pi._iso(NOW - timedelta(days=1)),
                                          "last_full_sync_at": None, "last_incremental_at": None})
    settings = _settings(rfp_bc_overlap_minutes=30)
    (_pages, counts), board = _scan(db, _run(), settings=settings)
    assert board.filters == ["2026-09-25T11:30:00.000Z.."]
    assert counts["high_water_before"] == NOW - timedelta(days=1)
    rows = db.tables["rfp_portal_invitations"]
    assert [r["external_id"] for r in rows] == [BY_ROLE["no_due_real"]["id"]]   # the one row updated since
    assert counts["rows_pulled"] == 1 and counts["new"] == 1
    # The same pull again: the hash matches, only the seen stamps move (in
    # the page's one chunked write); seen_count counts the pulls that WROTE
    # the row, so it stays at 1.
    (_pages, counts), _board = _scan(db, _run(id="run-bc-2"), settings=settings)
    row = rows[0]
    assert counts["new"] == 0 and counts["changed"] == 0
    assert row["last_seen_run_id"] == "run-bc-2" and row["last_seen_at"] == pi._iso(NOW)
    assert row["seen_count"] == 1 and row["change_log"] == []
    # Without a high water mark an incremental pull is unfiltered (the first run).
    db.tables["rfp_portal_state"].clear()
    (_pages, counts), board = _scan(db, _run(id="run-bc-3"), settings=settings)
    assert board.filters == [None] and counts["rows_pulled"] == 24


# ── The request budget ───────────────────────────────────────────────────


class CountingDB(BcDB):
    """BcDB that records every request (one execute() is one PostgREST
    round trip) as (table, verb). Built over an existing fake so it shares
    that fake's tables and id counter: the autouse environment, the client
    and the scan all read and write the same rows."""

    def __init__(self, base: BcDB) -> None:
        super().__init__()
        self._base = base
        self.tables = base.tables
        self.defaults = base.defaults
        self.requests: list[tuple[str, str]] = []

    def next_id(self):
        return self._base.next_id()

    def table(self, name):
        query = super().table(name)
        original = query.execute

        def execute():
            self.requests.append((name, query._op))
            return original()

        query.execute = execute
        return query

    def count(self, table=None, op=None) -> int:
        return sum(1 for t, o in self.requests if (table is None or t == table) and (op is None or o == op))


def _counted_scan(db, counting, run, board, settings=None):
    """The scan against the counting fake; the client keeps the plain fake
    so its own token reads are not counted."""
    with _session(db, board) as session:
        return _portal().scan(counting, settings or _settings(), run, session, lambda: None)


def test_full_scan_request_budget_is_a_constant_plus_one_write_per_new_or_changed_row(db):
    """569 s for 2,543 rows came from one round trip per stored row. The
    budget now: a handful of selects per scan plus, per API page, one
    select and one chunked seen-stamp update for its unchanged rows, then
    one insert per new row and one update per changed row."""
    inv = pi.INVITATIONS_TABLE
    counting = CountingDB(db)
    board = FakeBoard()
    pages, counts = _counted_scan(db, counting, _run(id="run-1", kind="full"), board)
    assert pages == 1 and counts["new"] == 7 and counts["skipped_closed"] == 17
    assert counting.count(inv, "insert") == 24 and counting.count(inv, "update") == 0
    # The state read; the page lookup; the sibling index (two indexed
    # selects, once, on the first insert at match); the missing-marking
    # select; the nightly pass's select. Before this change the same scan
    # was 1 + 24 inserts + 24 per-row seen updates on a second pass.
    assert counting.count("rfp_portal_state") == 1 and counting.count(inv, "select") == 5
    assert len(counting.requests) == 6 + 24
    # The same board again: every row is unchanged, the page's 24 rows share
    # one seen-stamp update and the sibling index is never read.
    counting.requests.clear()
    _pages, counts = _counted_scan(db, counting, _run(id="run-2", kind="full"), board)
    assert counts["new"] == 0 and counts["changed"] == 0
    assert [(t, o) for t, o in counting.requests] == [
        ("rfp_portal_state", "select"), (inv, "select"), (inv, "update"), (inv, "select"), (inv, "select"),
    ]                                                                       # state, page, touch, unseen, sweep
    assert counting.count(inv, "update") == 1 and counting.count(inv, "insert") == 0
    stored = db.tables["rfp_portal_invitations"]
    assert all(r["last_seen_run_id"] == "run-2" and r["last_seen_at"] == pi._iso(NOW) and r["seen_count"] == 1 for r in stored)
    # One changed row and one new row at match: one update and one insert
    # more (plus the sibling index the insert needs); the other 23 rows
    # still share one touch.
    moved = _opp("open_undecided_min", dueAt="2026-10-01T22:00:00.000Z", updatedAt="2026-09-26T11:00:00.000Z")
    moved["clientValues"]["dueAt"] = "2026-10-01T22:00:00.000Z"
    fresh = _opp("open_undecided_full", id="ffff0000ffff0000ffff0001", name="Fixture Project 99")
    fresh["clientValues"]["name"] = "Fixture Project 99"
    rows = [moved if r["_fixture_role"] == "open_undecided_min" else r for r in OPPS] + [fresh]
    counting.requests.clear()
    _pages, counts = _counted_scan(db, counting, _run(id="run-3", kind="full"), FakeBoard(rows))
    assert counts["changed"] == 1 and counts["new"] == 1
    assert counting.count(inv, "update") == 2 and counting.count(inv, "insert") == 1
    assert len(counting.requests) == 7 + 2
    assert len(stored) == 25
    changed = _by_external(db, BY_ROLE["open_undecided_min"]["id"])
    assert changed["seen_count"] == 2 and changed["last_seen_run_id"] == "run-3"      # the per-row write increments
    assert all(r["last_seen_run_id"] == "run-3" for r in stored)


def test_a_page_of_100_known_unchanged_rows_costs_one_select_and_one_chunked_update(db):
    inv = pi.INVITATIONS_TABLE
    rows = []
    for i in range(100):
        row = copy.deepcopy(BY_ROLE["open_undecided_full"])
        row["id"] = f"{i:024x}"
        rows.append(row)
    board = FakeBoard(rows)
    _counted_scan(db, CountingDB(db), _run(id="run-1"), board)
    stored = db.tables["rfp_portal_invitations"]
    assert len(stored) == 100 and all(r["status"] == "match" for r in stored)
    counting = CountingDB(db)
    _pages, counts = _counted_scan(db, counting, _run(id="run-2"), board)
    assert board.page_calls == 2 and counts["rows_pulled"] == 100
    assert counts["new"] == 0 and counts["changed"] == 0
    # The state read, the page's one select, the page's one seen-stamp
    # update (100 ids, under the 200 chunk), nothing else.
    assert counting.requests == [("rfp_portal_state", "select"), (inv, "select"), (inv, "update")]
    assert all(r["last_seen_run_id"] == "run-2" and r["last_seen_at"] == pi._iso(NOW) for r in stored)
    assert all(r["seen_count"] == 1 and r.get("missing_since") is None and r["change_log"] == [] for r in stored)


def test_an_unchanged_row_back_after_a_missing_mark_is_cleared_by_the_page_touch(db):
    """The withdrawal rule keys on missing_since, so a row back on the
    board must lose its mark even when its payload did not move: the same
    chunked seen-stamp write the page's other unchanged rows get, never a
    write of its own."""
    inv = pi.INVITATIONS_TABLE
    board = FakeBoard([_opp("open_undecided_full"), _opp("open_undecided_min"), _opp("with_trade_instructions")])
    _counted_scan(db, CountingDB(db), _run(id="run-1"), board)
    stored = db.tables["rfp_portal_invitations"]
    for r in stored[:2]:
        r["missing_since"] = pi._iso(NOW - timedelta(days=1))
    counting = CountingDB(db)
    _pages, counts = _counted_scan(db, counting, _run(id="run-2"), board)
    assert counts["changed"] == 0 and counts["new"] == 0
    assert counting.requests == [("rfp_portal_state", "select"), (inv, "select"), (inv, "update")]
    for r in stored:
        assert r.get("missing_since") is None and r["last_seen_run_id"] == "run-2" and r["last_seen_at"] == pi._iso(NOW)
        assert r["seen_count"] == 1 and r["change_log"] == []
    # A page with nothing unchanged issues no touch at all.
    counting.requests.clear()
    _counted_scan(db, counting, _run(id="run-3"), FakeBoard([_opp("open_accepted")]))
    assert counting.count(inv, "update") == 0 and counting.count(inv, "insert") == 1


def test_batched_hands_over_the_rows_already_pulled_before_a_mid_pull_failure():
    def stream():
        yield {"id": "a"}
        yield {"id": "b"}
        yield {"id": "c"}
        raise bc_client.BcTransient("rate limit outlived its retries", status=429)

    batches = []
    with pytest.raises(bc_client.BcTransient):
        for batch in bcp._batched(stream(), 2):
            batches.append([r["id"] for r in batch])
    assert batches == [["a", "b"], ["c"]]
    assert [len(b) for b in bcp._batched(iter([{"id": str(i)} for i in range(250)]), 100)] == [100, 100, 50]
    assert list(bcp._batched(iter([]), 100)) == []


def test_known_row_change_logs_the_tracked_fields_and_bells_only_when_linked(db):
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp("open_undecided_full"), _opp("open_undecided_min")]))
    linked = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    linked.update(status="exists", match_project_id=P1)
    moved = _opp("open_undecided_full", name="Fixture Project 01 Phase 2", dueAt="2026-10-20T23:00:00.000Z",
                 updatedAt="2026-09-26T11:00:00.000Z")
    moved["clientValues"]["dueAt"] = "2026-10-20T23:00:00.000Z"
    moved["clientValues"]["name"] = "Fixture Project 01 Phase 2"
    unlinked = _opp("open_undecided_min", dueAt="2026-10-01T22:00:00.000Z", updatedAt="2026-09-26T11:00:00.000Z")
    unlinked["clientValues"]["dueAt"] = "2026-10-01T22:00:00.000Z"
    (_pages, counts), _board = _scan(db, _run(id="run-bc-2"), FakeBoard([moved, unlinked]))
    assert counts["changed"] == 2 and counts["new"] == 0
    linked = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert [(c["field"], c["old"], c["new"], c["run_id"]) for c in linked["change_log"]] == [
        ("close_at", DUE_01, "2026-10-20T23:00:00+00:00", "run-bc-2"),
        ("title", "Fixture Project 01", "Fixture Project 01 Phase 2", "run-bc-2"),
    ]
    assert linked["title"] == "Fixture Project 01 Phase 2" and linked["close_at"] == "2026-10-20T23:00:00+00:00"
    assert linked["status"] == "exists" and linked["match_project_id"] == P1      # never touched by the pull
    bells = _bells(db, bcp.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 1 and bells[0]["user_id"] == ADMIN_USER and bells[0]["mirror_email"] is False
    # The bell names the date move only (design 3.6); the rename is in the log and on the block, not the bell.
    assert bells[0]["metadata"]["invitation_id"] == linked["id"] and bells[0]["metadata"]["fields"] == ["close_at"]
    assert bells[0]["metadata"]["project_id"] == P1
    assert "due date" in bells[0]["message"] and "name" not in bells[0]["message"] and "GC AA Builders" in bells[0]["message"]
    other = _by_external(db, BY_ROLE["open_undecided_min"]["id"])
    assert len(other["change_log"]) == 1 and other["change_log"][0]["field"] == "close_at"
    assert len(_bells(db, bcp.NOTIFY_INVITATION_CHANGED)) == 1                 # the unlinked row rings nobody
    # The same payload again: the hash short-circuits, nothing is logged twice.
    (_pages, counts), _board = _scan(db, _run(id="run-bc-3"), FakeBoard([moved, unlinked]))
    assert counts["changed"] == 0 and len(_by_external(db, BY_ROLE["open_undecided_full"]["id"])["change_log"]) == 2
    # The change log is capped at 50.
    linked["change_log"] = [{"at": "t", "field": "title", "old": str(i), "new": str(i + 1), "run_id": "r"} for i in range(50)]
    again = copy.deepcopy(moved)
    again["clientValues"]["dueAt"] = again["dueAt"] = "2026-10-21T23:00:00.000Z"
    _scan(db, _run(id="run-bc-4"), FakeBoard([again]))
    log = _by_external(db, BY_ROLE["open_undecided_full"]["id"])["change_log"]
    assert len(log) == 50 and log[-1]["field"] == "close_at" and log[0]["old"] == "1"
    # A name, state, archived or location change alone on a linked row: logged, counted, no bell.
    db.tables["notifications"].clear()
    renamed = copy.deepcopy(again)
    renamed["clientValues"]["name"] = renamed["name"] = "Fixture Project 01 Phase 3"
    renamed["submissionState"] = "WILL_SUBMIT"
    renamed["isArchived"] = True
    (_pages, counts), _board = _scan(db, _run(id="run-bc-5"), FakeBoard([renamed]))
    log = _by_external(db, BY_ROLE["open_undecided_full"]["id"])["change_log"]
    assert counts["changed"] == 1 and {c["field"] for c in log[-3:]} == {"title", "submission_state", "is_archived"}
    assert _bells(db, bcp.NOTIFY_INVITATION_CHANGED) == []
    assert bcp.BELL_FIELDS == ("close_at", "job_walk_at", "expected_start_at", "expected_finish_at", "gc_external_id")


def test_scan_time_sibling_link_lands_a_package_pair_on_one_project(db):
    a = _opp("same_gc_two_packages_a")
    b = _opp("same_gc_two_packages_b", isArchived=False, dueAt="2026-10-28T19:00:00.000Z")
    b["clientValues"]["dueAt"] = "2026-10-28T19:00:00.000Z"
    _scan(db, _run(id="run-bc-1"), FakeBoard([a]))
    first = _by_external(db, a["id"])
    first.update(status="created", created_project_id=P1)
    (_pages, counts), _board = _scan(db, _run(id="run-bc-2"), FakeBoard([a, b]))
    second = _by_external(db, b["id"])
    assert second["status"] == "exists" and second["match_project_id"] == P1
    assert second["flag_reason"] == bcp.FLAG_SIBLING_PACKAGE and second["resolution"] == "system"
    assert second["sibling_of"] == first["id"] and second["resolved_at"] == pi._iso(NOW)
    assert counts["new"] == 1 and counts["rows_pipeline"] == 0
    # A third package in the same scan links through the index too (no re-read).
    c = _opp("same_gc_two_packages_b", id="ffff0000ffff0000ffff0000", tradeName="Fire Alarm", isArchived=False,
             dueAt="2026-10-28T19:00:00.000Z")
    c["clientValues"]["dueAt"] = "2026-10-28T19:00:00.000Z"
    _scan(db, _run(id="run-bc-3"), FakeBoard([a, b, c]))
    assert _by_external(db, c["id"])["match_project_id"] == P1
    # A different GC with the same name is not a sibling: it enters the pipeline.
    d = _opp("cross_gc_b", name="Fixture Project 18", isArchived=False, dueAt="2026-10-28T19:00:00.000Z")
    d["clientValues"]["dueAt"] = "2026-10-28T19:00:00.000Z"
    d["clientValues"]["name"] = "Fixture Project 18"
    _scan(db, _run(id="run-bc-4"), FakeBoard([d]))
    assert _by_external(db, d["id"])["status"] == "match"


def test_a_review_match_candidate_is_never_a_sibling_leader(db, monkeypatch):
    """The match step stores the best candidate in match_project_id on a
    review_match row too (an unconfirmed GC parks every confident match,
    D11). That guess is not a project: a later package of the same GC and
    name enters the pipeline at `match` (scan time, D12a) or is created
    (create time, D12b) instead of being merged to the guess."""
    a = _opp("same_gc_two_packages_a")
    b = _opp("same_gc_two_packages_b", isArchived=False, dueAt="2026-10-28T19:00:00.000Z")
    b["clientValues"]["dueAt"] = "2026-10-28T19:00:00.000Z"
    _scan(db, _run(id="run-bc-1"), FakeBoard([a]))
    first = _by_external(db, a["id"])
    first.update(status="review_match", flag_reason="match_gc_unresolved", match_project_id=P1, gc_id=G1, gc_kind="provisional")
    (_pages, counts), _board = _scan(db, _run(id="run-bc-2"), FakeBoard([a, b]))
    second = _by_external(db, b["id"])
    assert second["status"] == "match" and not second.get("match_project_id") and not second.get("sibling_of")
    assert counts["new"] == 1 and counts["rows_pipeline"] == 1
    # An ignored row that kept its candidate leads nobody either.
    first.update(status="ignored")
    c = _opp("same_gc_two_packages_b", id="ffff0000ffff0000ffff0000", tradeName="Fire Alarm", isArchived=False,
             dueAt="2026-10-28T19:00:00.000Z")
    c["clientValues"]["dueAt"] = "2026-10-28T19:00:00.000Z"
    _scan(db, _run(id="run-bc-3"), FakeBoard([a, b, c]))
    assert _by_external(db, c["id"])["status"] == "match"
    # Create time: the guard finds no sibling with a project; the project is created.
    created = []
    monkeypatch.setattr(rfp_create, "create_from_portal", lambda sb, row, *, actor_id, automatic: created.append(row["id"]))
    first.update(status="review_match")
    second.update(status="create", gc_id=G1, gc_kind="alias")
    pi._step_create(db, dict(second), _settings(rfp_create_auto_enabled=True))
    assert created == [second["id"]] and _inv(db, second["id"])["status"] == "create"
    assert bcp.find_sibling(db, second) is None
    # Once the leader really has a project, the same package links to it.
    first.update(status="exists")
    assert bcp.find_sibling(db, second)["id"] == first["id"]
    second["status"] = "create"
    pi._step_create(db, dict(second), _settings(rfp_create_auto_enabled=True))
    assert _inv(db, second["id"])["status"] == "exists" and _inv(db, second["id"])["match_project_id"] == P1


def test_declined_or_archived_pipeline_rows_are_ignored_by_the_system_and_unignore_tolerates_it(db):
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp("open_undecided_full"), _opp("with_trade_instructions"), _opp("open_accepted")]))
    parked = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    parked.update(status="review_match", flag_reason="match_uncertain")
    linked = _by_external(db, BY_ROLE["open_accepted"]["id"])
    linked.update(status="exists", match_project_id=P1)
    board = FakeBoard([
        _opp("open_undecided_full", submissionState="DECLINED", updatedAt="2026-09-26T11:00:00.000Z"),
        _opp("with_trade_instructions", isArchived=True, updatedAt="2026-09-26T11:00:00.000Z"),
        _opp("open_accepted", isArchived=True, submissionState="DECLINED", updatedAt="2026-09-26T11:00:00.000Z"),
    ])
    _scan(db, _run(id="run-bc-2"), board)
    declined = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert declined["status"] == "ignored" and declined["ignore_source"] == "system" and declined["ignored_by"] is None
    assert declined["ignore_reason"] == "declined in BuildingConnected" and declined["decided_at_step"] == "match"
    archived = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    assert archived["status"] == "ignored" and archived["ignore_reason"] == "archived in BuildingConnected"
    assert archived["decided_at_step"] == "review_match"
    linked = _by_external(db, BY_ROLE["open_accepted"]["id"])
    assert linked["status"] == "exists" and linked["match_project_id"] == P1 and linked["is_archived"] is True
    out = pi.unignore(db, declined["id"], U1)
    assert out["status"] == "match" and out["ignore_source"] is None
    assert db.tables["audit_log"][-1]["payload"] == {"was_reason": "declined in BuildingConnected", "was_source": "system"}


def test_nda_row_unmasks_back_to_match_when_the_details_appear(db):
    masked = _opp("nda_masked", dueAt="2026-10-30T17:00:00.000Z")
    _scan(db, _run(id="run-bc-1"), FakeBoard([masked]))
    row = _by_external(db, masked["id"])
    assert row["status"] == "match" and row["gc_external_id"] is None
    row.update(status="review_match", flag_reason=bcp.FLAG_NDA_REQUIRED)
    lifted = copy.deepcopy(masked)
    lifted["client"] = copy.deepcopy(BY_ROLE["open_undecided_full"]["client"])
    lifted["updatedAt"] = "2026-09-26T11:00:00.000Z"
    _scan(db, _run(id="run-bc-2"), FakeBoard([lifted]))
    row = _by_external(db, masked["id"])
    assert row["status"] == "match" and row["flag_reason"] is None and row["gc_external_id"] == GC_AA
    assert row["agency"] == "GC AA Builders"


# ── Fix round 3: a due date appearing, a GC swapped on the board, the bell refreshed ──

GC_AO = BY_ROLE["cross_gc_b"]["client"]["company"]["id"]
LATER = "2026-09-26T11:00:00.000Z"


def _swapped(role, company_id=None, company_name="GC AO Builders", **over):
    """The fixture with its client company swapped (the lead kept), as the
    board shows it after the GC changed the opportunity's client."""
    opp = _opp(role, **{"updatedAt": LATER, **over})
    opp["client"] = copy.deepcopy(opp["client"])
    opp["client"]["company"] = {"id": company_id or GC_AO, "name": company_name}
    return opp


def _with_due(opp, due):
    opp["dueAt"] = due
    if isinstance(opp.get("clientValues"), dict):
        opp["clientValues"]["dueAt"] = due
    return opp


def test_a_no_due_date_row_goes_back_to_match_when_the_gc_adds_the_due_date(db):
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp("no_due_real"), _opp("open_undecided_min")]))
    parked = _by_external(db, BY_ROLE["no_due_real"]["id"])
    assert parked["close_at"] is None
    parked.update(status="review_match", flag_reason=bcp.FLAG_NO_DUE_DATE, decided_at_step="match",
                  attempts=2, last_error="x", next_attempt_at=pi._iso(NOW + timedelta(hours=1)))
    other = _by_external(db, BY_ROLE["open_undecided_min"]["id"])
    other.update(status="review_match", flag_reason=bcp.FLAG_SUBMITTED_UNMATCHED, decided_at_step="match")
    # A change that is not the due date leaves the parked row where it is.
    renamed = _opp("no_due_real", name="Fixture Project 07 Renamed", updatedAt=LATER)
    if isinstance(renamed.get("clientValues"), dict):
        renamed["clientValues"]["name"] = "Fixture Project 07 Renamed"
    _scan(db, _run(id="run-bc-2"), FakeBoard([renamed]))
    assert _by_external(db, BY_ROLE["no_due_real"]["id"])["status"] == "review_match"
    due = "2026-10-22T21:00:00.000Z"
    later_due = "2026-10-23T21:00:00.000Z"
    (_pages, counts), _board = _scan(db, _run(id="run-bc-3"), FakeBoard([
        _with_due(copy.deepcopy(renamed), due),
        _with_due(_opp("open_undecided_min", updatedAt=LATER), later_due),
    ]))
    row = _by_external(db, BY_ROLE["no_due_real"]["id"])
    assert row["status"] == "match" and row["flag_reason"] is None and row["decided_at_step"] is None
    assert row["attempts"] == 0 and row["last_error"] is None and row["next_attempt_at"] is None
    assert row["close_at"] == "2026-10-22T21:00:00+00:00"
    assert row["change_log"][-1]["field"] == "close_at" and row["change_log"][-1]["old"] is None
    # A review_match row parked for another reason stays parked when its date moves.
    assert _by_external(db, BY_ROLE["open_undecided_min"]["id"])["status"] == "review_match"
    assert counts["changed"] == 2
    assert _bells(db, bcp.NOTIFY_INVITATION_CHANGED) == []                    # unlinked rows ring nobody


def test_a_gc_swapped_on_the_board_resets_the_resolution_of_unlinked_rows_and_the_sweep_resolves_again(db):
    roles = ("open_undecided_full", "open_undecided_min", "with_trade_instructions", "open_accepted", "budget_request")
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp(r) for r in roles]))
    resolved = {"gc_id": G1, "gc_kind": "alias", "gc_confirmed_at": pi._iso(NOW - timedelta(days=1)),
                "gc_confirmed_by": U1, "gc_candidates": [{"gc_id": G1, "name": "Old GC", "score": 0.9}]}
    review = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    review.update(status="review_match", flag_reason="match_uncertain", decided_at_step="match",
                  match_project_id=P1, **resolved)
    at_match = _by_external(db, BY_ROLE["open_undecided_min"]["id"])
    at_match.update(**resolved)
    done = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    done.update(status="done", decided_at_step="create", flag_reason="no_candidate", **resolved)
    ignored = _by_external(db, BY_ROLE["open_accepted"]["id"])
    ignored.update(status="ignored", ignore_source="user", ignored_by=U1, **resolved)
    renamed_only = _by_external(db, BY_ROLE["budget_request"]["id"])
    renamed_only.update(status="done", decided_at_step="create", **resolved)
    renamed = _opp("budget_request", updatedAt=LATER)
    renamed["client"] = copy.deepcopy(renamed["client"])
    renamed["client"]["company"]["name"] = "GC AE Builders Inc"
    (_pages, counts), _board = _scan(db, _run(id="run-bc-2"), FakeBoard(
        [_swapped(r) for r in roles[:4]] + [renamed]
    ))
    assert counts["changed"] == 5
    cleared = {"gc_id": None, "gc_kind": None, "gc_candidates": None, "gc_confirmed_at": None, "gc_confirmed_by": None}
    for role in roles[:4]:
        row = _by_external(db, BY_ROLE[role]["id"])
        assert {k: row.get(k) for k in cleared} == cleared, role
        assert row["gc_external_id"] == GC_AO and row["gc_external_name"] == "GC AO Builders"
        gc_log = [(c["field"], c["old"], c["new"], c["run_id"]) for c in row["change_log"] if c["field"].startswith("gc_")]
        assert gc_log == [
            ("gc_external_id", BY_ROLE[role]["client"]["company"]["id"], GC_AO, "run-bc-2"),
            ("gc_external_name", BY_ROLE[role]["client"]["company"]["name"], "GC AO Builders", "run-bc-2"),
        ], role
    review = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert review["status"] == "match" and review["flag_reason"] is None and review["decided_at_step"] is None
    assert _by_external(db, BY_ROLE["open_undecided_min"]["id"])["status"] == "match"
    done = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    assert done["status"] == "match" and done["flag_reason"] is None
    # An ignored row keeps its status (restore sends it to match, where it resolves afresh).
    assert _by_external(db, BY_ROLE["open_accepted"]["id"])["status"] == "ignored"
    # The same company renamed: logged, the resolution and the status stand.
    row = _by_external(db, BY_ROLE["budget_request"]["id"])
    assert row["status"] == "done" and row["gc_id"] == G1 and row["gc_confirmed_at"] is not None
    assert [c["field"] for c in row["change_log"]] == ["gc_external_name"]
    assert _bells(db, bcp.NOTIFY_INVITATION_CHANGED) == []
    # The sweep resolves the new company through its own alias.
    db.tables["general_contractors"].extend([{"id": G1, "name": "Old GC"}, {"id": G2, "name": "New GC"}])
    db.tables["gc_external_aliases"].append({"id": "al-ao", "source": BC, "external_id": GC_AO,
                                             "external_name": "GC AO Builders", "gc_id": G2,
                                             "confirmed_by": U1, "confirmed_at": "t"})
    _process(db, inv_id=review["id"])
    row = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert row["gc_id"] == G2 and row["gc_kind"] == "alias" and row["decided_at_step"] in ("match", "create")
    # A declined swap is ignored by the system and still loses the old resolution; a done row that
    # was declined is not sent back to match.
    declined = _by_external(db, BY_ROLE["open_undecided_min"]["id"])
    declined.update(status="review_match", flag_reason="match_uncertain", **resolved)
    closed = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    closed.update(status="done", **resolved)
    _scan(db, _run(id="run-bc-3"), FakeBoard([
        _swapped("open_undecided_min", company_id=GC_AM, company_name="GC AM Builders", submissionState="DECLINED"),
        _swapped("with_trade_instructions", company_id=GC_AM, company_name="GC AM Builders", submissionState="DECLINED"),
    ]))
    declined = _by_external(db, BY_ROLE["open_undecided_min"]["id"])
    assert declined["status"] == "ignored" and declined["ignore_source"] == "system" and declined["gc_id"] is None
    assert declined["ignore_reason"] == "declined in BuildingConnected" and declined["decided_at_step"] == "review_match"
    closed = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    assert closed["status"] == "done" and closed["gc_id"] is None and closed["gc_confirmed_at"] is None


def test_a_gc_swapped_on_a_linked_row_keeps_the_project_gc_asks_again_and_rings_the_bell(db):
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp("open_undecided_full")]))
    db.tables["projects"].append({**_project(P1, "Fixture Project 01", bid_at=DUE_01, gcs=[G1]), "gc_confirm_pending": False})
    db.tables["project_gcs"].append({"id": "pg-1", "project_id": P1, "gc_id": G1, "needs_by": "2026-10-16"})
    row = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    row.update(status="created", created_project_id=P1, gc_id=G1, gc_kind="alias", gc_confirmed_at=pi._iso(NOW),
               gc_confirmed_by=U1, gc_candidates=[{"gc_id": G1, "name": "Old GC", "score": 0.9}], project_gc_id="pg-1")
    # A rename of the same company first: logged only, no question, no bell.
    renamed = _opp("open_undecided_full", updatedAt=LATER)
    renamed["client"] = copy.deepcopy(renamed["client"])
    renamed["client"]["company"]["name"] = "GC AA Builders Inc"
    _scan(db, _run(id="run-bc-2"), FakeBoard([renamed]))
    row = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert [c["field"] for c in row["change_log"]] == ["gc_external_name"] and row["gc_kind"] == "alias"
    assert db.tables["projects"][0]["gc_confirm_pending"] is False
    assert _bells(db, bcp.NOTIFY_INVITATION_CHANGED) == []
    # The client swapped to another company.
    swapped = _swapped("open_undecided_full", updatedAt="2026-09-26T11:30:00.000Z")
    (_pages, counts), _board = _scan(db, _run(id="run-bc-3"), FakeBoard([swapped]))
    assert counts["changed"] == 1
    row = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    # The project's GC is never rewritten by the pull: the row keeps the GC the project holds and its link.
    assert row["status"] == "created" and row["created_project_id"] == P1
    assert row["gc_id"] == G1 and row["project_gc_id"] == "pg-1"
    assert db.tables["project_gcs"] == [{"id": "pg-1", "project_id": P1, "gc_id": G1, "needs_by": "2026-10-16"}]
    # ... but the question is open again, on the row and on the project.
    assert row["gc_kind"] == "provisional" and row["gc_confirmed_at"] is None and row["gc_confirmed_by"] is None
    assert row["gc_candidates"] == [] and row["gc_external_id"] == GC_AO
    assert db.tables["projects"][0]["gc_confirm_pending"] is True
    assert [(c["field"], c["old"], c["new"]) for c in row["change_log"][-2:]] == [
        ("gc_external_id", GC_AA, GC_AO), ("gc_external_name", "GC AA Builders Inc", "GC AO Builders"),
    ]
    bells = _bells(db, bcp.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 1 and bells[0]["user_id"] == ADMIN_USER
    assert bells[0]["metadata"]["fields"] == ["gc_external_id"] and bells[0]["metadata"]["project_id"] == P1
    assert "GC changed: GC AA Builders Inc to GC AO Builders" in bells[0]["message"]
    # Answering the card on the row that created the project closes the question ("same GC").
    db.tables["general_contractors"].append({"id": G1, "name": "Old GC"})
    bcp.confirm_gc(db, row["id"], U1, gc_id=G1)
    assert db.tables["projects"][0]["gc_confirm_pending"] is False
    assert db.tables["gc_external_aliases"][-1]["external_id"] == GC_AO


def test_a_gc_swap_on_a_merged_row_is_answered_by_that_row_unless_the_creator_still_asks(db):
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp("open_undecided_full")]))
    db.tables["general_contractors"].extend([{"id": G1, "name": "Old GC"}, {"id": G2, "name": "New GC"}])
    db.tables["projects"].append({**_project(P2, "Fixture Project 01", bid_at=DUE_01, gcs=[G1]), "gc_confirm_pending": False})
    merged = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    merged.update(status="exists", match_project_id=P2, gc_id=G1, gc_kind="domain", resolution="system")
    _scan(db, _run(id="run-bc-2"), FakeBoard([_swapped("open_undecided_full")]))
    project = db.tables["projects"][0]
    assert project["gc_confirm_pending"] is True
    merged = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert merged["status"] == "exists" and merged["gc_id"] == G1 and merged["gc_kind"] == "provisional"
    # A creator row whose own GC question is still open keeps the flag up when the merged row answers.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-creator", external_id="cr", bid_number="cr", bid_number_raw="cr",
                                                        status="created", created_project_id=P2, gc_id=G1, gc_kind="provisional"))
    bcp.confirm_gc(db, merged["id"], U1, gc_id=G1)
    assert project["gc_confirm_pending"] is True
    # That answer closed the merged row's own question (its confirmation is not older than the swap).
    assert bcp.open_gc_question_at(_inv(db, merged["id"])) is None
    # The board swaps it again later; the creator's question is answered by then: the merged row's
    # answer now clears the flag.
    _inv(db, "inv-creator").update(gc_kind="alias", gc_confirmed_at=pi._iso(NOW))
    _inv(db, merged["id"])["change_log"].append(
        {"at": pi._iso(NOW + timedelta(minutes=5)), "field": "gc_external_id", "old": GC_AA, "new": GC_AO, "run_id": "r"})
    assert bcp.open_gc_question_at(_inv(db, merged["id"])) == NOW + timedelta(minutes=5)
    bcp.confirm_gc(db, merged["id"], U1, gc_id=G1)
    assert project["gc_confirm_pending"] is False


def test_a_second_date_move_refreshes_the_unread_bell_instead_of_going_silent(db):
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp("open_undecided_full")]))
    row = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    row.update(status="exists", match_project_id=P1)
    first, second, third = "2026-10-20T23:00:00.000Z", "2026-10-22T23:00:00.000Z", "2026-10-23T23:00:00.000Z"
    _scan(db, _run(id="run-bc-2"), FakeBoard([_with_due(_opp("open_undecided_full", updatedAt=LATER), first)]))
    bells = _bells(db, bcp.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 1 and bells[0]["metadata"]["run_id"] == "run-bc-2"
    assert bcp._describe_value("close_at", first) in bells[0]["message"]
    # The second move while the first bell is unread: the same bell now says the latest move.
    _scan(db, _run(id="run-bc-3"), FakeBoard([_with_due(_opp("open_undecided_full", updatedAt="2026-09-26T11:10:00.000Z"), second)]))
    bells = _bells(db, bcp.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 1 and bells[0]["user_id"] == ADMIN_USER
    assert bells[0]["metadata"]["run_id"] == "run-bc-3" and bells[0]["metadata"]["fields"] == ["close_at"]
    assert bells[0]["metadata"]["invitation_id"] == row["id"] and bells[0]["metadata"]["project_id"] == P1
    assert bcp._describe_value("close_at", second) in bells[0]["message"]
    assert f"{bcp._describe_value('close_at', first)} -> {bcp._describe_value('close_at', second)}" in bells[0]["message"]
    assert bells[0]["created_at"] == pi._iso(NOW)
    # A job walk move rewrites it again (the fields follow the latest change).
    moved = _with_due(_opp("open_undecided_full", updatedAt="2026-09-26T11:20:00.000Z", jobWalkAt="2026-10-01T16:00:00.000Z"), second)
    if isinstance(moved.get("clientValues"), dict):
        moved["clientValues"]["jobWalkAt"] = "2026-10-01T16:00:00.000Z"
    _scan(db, _run(id="run-bc-4"), FakeBoard([moved]))
    bells = _bells(db, bcp.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 1 and bells[0]["metadata"]["fields"] == ["job_walk_at"] and "job walk" in bells[0]["message"]
    # Read: the next move is a new bell and the read one is left as it was.
    bells[0]["read_at"] = pi._iso(NOW)
    read_message = bells[0]["message"]
    _scan(db, _run(id="run-bc-5"), FakeBoard([_with_due(copy.deepcopy(moved) | {"updatedAt": "2026-09-26T11:30:00.000Z"}, third)]))
    bells = _bells(db, bcp.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 2 and bells[0]["message"] == read_message and bells[0]["metadata"]["run_id"] == "run-bc-4"
    assert bells[1]["metadata"]["run_id"] == "run-bc-5" and bcp._describe_value("close_at", third) in bells[1]["message"]
    # Dismissed counts as gone too; another invitation's unread bell is never rewritten.
    bells[1]["dismissed_at"] = pi._iso(NOW)
    other = {"type": bcp.NOTIFY_INVITATION_CHANGED, "message": "other", "user_id": ADMIN_USER, "read_at": None,
             "dismissed_at": None, "metadata": {"invitation_id": "inv-other", "run_id": "r"}}
    db.tables["notifications"].append(other)
    assert bcp.notify_changed(db, {**_inv(db, row["id"])}, [{"field": "close_at", "old": second, "new": third}], "run-bc-6") == 1
    assert other["message"] == "other" and other["metadata"]["run_id"] == "r"
    assert bcp.notify_changed(db, {**_inv(db, row["id"])}, [{"field": "close_at", "old": third, "new": first}], "run-bc-7") == 0
    unread = [n for n in _bells(db, bcp.NOTIFY_INVITATION_CHANGED) if n["read_at"] is None and n["dismissed_at"] is None
              and n["metadata"]["invitation_id"] == row["id"]]
    assert len(unread) == 1 and unread[0]["metadata"]["run_id"] == "run-bc-7"


def test_describe_change_names_the_gc_companies():
    assert bcp._describe_change({"field": "gc_external_id", "old": "a1", "new": "b2", "old_name": "Old Co",
                                 "new_name": "New Co"}) == "GC changed: Old Co to New Co"
    assert bcp._describe_change({"field": "gc_external_id", "old": None, "new": "b2", "old_name": None,
                                 "new_name": "New Co"}) == "GC changed: none to New Co"
    assert bcp._describe_change({"field": "gc_external_id", "old": "a1", "new": "b2"}) == "GC changed: a1 to b2"
    assert bcp._describe_change({"field": "gc_external_name", "old": "Old Co", "new": "Old Co Inc"}) == (
        "GC name changed: Old Co to Old Co Inc"
    )
    assert bcp._describe_change({"field": "gc_alias", "old": G1, "new": None}) == f"GC alias changed: {G1} to none"
    assert (bcp._FIELD_LABELS["gc_external_id"], bcp._FIELD_LABELS["gc_external_name"], bcp._FIELD_LABELS["gc_alias"]) == (
        "GC company", "GC company name", "GC alias",
    )


def test_full_sync_marks_missing_then_withdraws_and_expires_unlinked_pipeline_rows_only(db):
    settings = _settings(rfp_bc_missing_days=3, rfp_bc_expire_days=7)
    rows = [
        _bc_row(id="a", external_id="a", bid_number="a", bid_number_raw="a", status="match"),
        _bc_row(id="b", external_id="b", bid_number="b", bid_number_raw="b", status="review_match",
                missing_since=pi._iso(NOW - timedelta(days=4))),
        _bc_row(id="c", external_id="c", bid_number="c", bid_number_raw="c", status="create",
                close_at=pi._iso(NOW - timedelta(days=8))),
        _bc_row(id="d", external_id="d", bid_number="d", bid_number_raw="d", status="exists", match_project_id=P1,
                close_at=pi._iso(NOW - timedelta(days=30)), missing_since=pi._iso(NOW - timedelta(days=30))),
        _bc_row(id="e", external_id="e", bid_number="e", bid_number_raw="e", status="match", created_project_id=P2,
                close_at=pi._iso(NOW - timedelta(days=8))),
        _bc_row(id="f", external_id="f", bid_number="f", bid_number_raw="f", status="done",
                close_at=pi._iso(NOW - timedelta(days=8))),
        _bc_row(id="g", external_id="g", bid_number="g", bid_number_raw="g", status="ignored",
                missing_since=None),
        _bc_row(id="h", external_id="h", bid_number="h", bid_number_raw="h", status="match",
                close_at=pi._iso(NOW - timedelta(days=6))),
    ]
    db.tables["rfp_portal_invitations"].extend(rows)
    # An incremental scan never marks, withdraws or expires.
    (_pages, counts), _board = _scan(db, _run(id="run-inc"), FakeBoard([]), settings=settings)
    assert counts["rows_expired"] == 0 and counts["rows_withdrawn"] == 0
    assert all(r["missing_since"] is None for r in db.tables["rfp_portal_invitations"] if r["id"] in ("a", "c", "f"))
    (_pages, counts), _board = _scan(db, _run(id="run-full", kind="full"), FakeBoard([]), settings=settings)
    by_id = {r["id"]: r for r in db.tables["rfp_portal_invitations"]}
    assert by_id["a"]["status"] == "match" and by_id["a"]["missing_since"] == pi._iso(NOW)
    assert by_id["b"]["status"] == "withdrawn" and by_id["b"]["last_error"] == "absent from the Bid Board since 2026-09-22"
    assert by_id["b"]["decided_at_step"] == "review_match"
    assert by_id["c"]["status"] == "expired" and by_id["c"]["last_error"] == "due date passed on 2026-09-18"
    assert by_id["f"]["status"] == "expired"
    assert by_id["d"]["status"] == "exists" and by_id["e"]["status"] == "match"    # linked rows: never
    assert by_id["g"]["missing_since"] is None                                   # ignored rows: not marked
    assert by_id["h"]["status"] == "match"                                       # inside the expiry window
    assert counts["rows_expired"] == 2 and counts["rows_withdrawn"] == 1
    # Restore brings a parked row back to match with the reason cleared.
    out = pi.restore(db, "c", U1)
    assert out["status"] == "match" and out["last_error"] is None and out["attempts"] == 0
    assert db.tables["audit_log"][-1]["action"] == "rfp_portal.restore"
    assert db.tables["audit_log"][-1]["payload"]["from_status"] == "expired"


def test_a_review_match_candidate_does_not_exempt_the_row_from_the_system(db):
    """A review_match row carrying the matcher's best candidate is not
    linked to it: the system ignore (D24), the expiry and the withdrawal
    (3.9) apply to it, and a change on it rings nobody (the bell names a
    project only for a row that sits on one)."""
    settings = _settings(rfp_bc_missing_days=3, rfp_bc_expire_days=7)
    _scan(db, _run(id="run-bc-1"), FakeBoard([_opp("open_undecided_full"), _opp("with_trade_instructions"), _opp("open_accepted")]))
    declined = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    declined.update(status="review_match", flag_reason="match_gc_unresolved", match_project_id=P1, gc_id=G1, gc_kind="provisional")
    moved = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    moved.update(status="review_match", flag_reason="match_uncertain", match_project_id=P1)
    linked = _by_external(db, BY_ROLE["open_accepted"]["id"])
    linked.update(status="exists", match_project_id=P1)
    changed = _opp("with_trade_instructions", dueAt="2026-10-20T23:00:00.000Z", updatedAt="2026-09-26T11:00:00.000Z")
    changed["clientValues"]["dueAt"] = "2026-10-20T23:00:00.000Z"
    really = _opp("open_accepted", dueAt="2026-10-21T23:00:00.000Z", updatedAt="2026-09-26T11:00:00.000Z")
    really["clientValues"]["dueAt"] = "2026-10-21T23:00:00.000Z"
    _scan(db, _run(id="run-bc-2"), FakeBoard([
        _opp("open_undecided_full", submissionState="DECLINED", updatedAt="2026-09-26T11:00:00.000Z"), changed, really,
    ]), settings=settings)
    declined = _by_external(db, BY_ROLE["open_undecided_full"]["id"])
    assert declined["status"] == "ignored" and declined["ignore_source"] == "system"
    moved = _by_external(db, BY_ROLE["with_trade_instructions"]["id"])
    assert moved["status"] == "review_match" and len(moved["change_log"]) == 1
    bells = _bells(db, bcp.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 1 and bells[0]["metadata"]["invitation_id"] == linked["id"]
    assert bells[0]["metadata"]["project_id"] == P1
    # The nightly pass: past due by more than the window -> expired; gone from the board -> withdrawn.
    moved.update(close_at=pi._iso(NOW - timedelta(days=8)))
    db.tables["rfp_portal_invitations"].append(_bc_row(id="gone", external_id="gone", bid_number="gone", bid_number_raw="gone",
                                                        status="review_match", flag_reason="match_uncertain", match_project_id=P2,
                                                        missing_since=pi._iso(NOW - timedelta(days=4))))
    (_pages, counts), _board = _scan(db, _run(id="run-full", kind="full"), FakeBoard([really]), settings=settings)
    assert _by_external(db, BY_ROLE["with_trade_instructions"]["id"])["status"] == "expired"
    assert _inv(db, "gone")["status"] == "withdrawn"
    assert _by_external(db, BY_ROLE["open_accepted"]["id"])["status"] == "exists"
    assert counts["rows_expired"] == 1 and counts["rows_withdrawn"] == 1
    assert bcp._linked({"status": "review_match", "match_project_id": P1}) is False
    assert bcp._linked({"status": "exists", "match_project_id": P1}) is True
    assert bcp._linked({"status": "match", "created_project_id": P1}) is True


def test_restore_covers_the_three_parked_statuses_and_refuses_the_rest(db):
    for status in ("historical", "withdrawn"):
        db.tables["rfp_portal_invitations"].append(_bc_row(id=f"inv-{status}", external_id=status, bid_number=status,
                                                            bid_number_raw=status, status=status,
                                                            last_error="x", missing_since=pi._iso(NOW)))
        out = pi.restore(db, f"inv-{status}", U1)
        assert out["status"] == "match" and out["missing_since"] is None
    db.tables["rfp_portal_invitations"].append(_bc_row(status="match"))
    with pytest.raises(pi.RfpPortalError) as exc:
        pi.restore(db, INV, U1)
    assert exc.value.code == pi.CODE_NOT_ACTIONABLE
    # A restored row is at match: restoring it again is refused too.
    with pytest.raises(pi.RfpPortalError):
        pi.restore(db, "inv-historical", U1)
    # The portal-side name delegates to the same action.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-exp", external_id="exp", bid_number="exp", bid_number_raw="exp",
                                                        status="expired", last_error="due date passed on 2026-09-01"))
    out = bcp.restore(db, "inv-exp", U1)
    assert out["status"] == "match" and out["last_error"] is None and out["flag_reason"] is None


def test_execute_scan_completes_the_run_writes_the_state_and_rings_the_bc_bell(db, board_transport):
    db.tables["rfp_portal_runs"].append(_run(kind="full"))
    pi.execute_scan("run-bc")
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "complete" and run["claimed_by"] is None and run["finished_at"] == pi._iso(NOW)
    assert run["pages_scanned"] == 1 and run["invitations_seen"] == 24 == run["rows_pulled"]
    assert run["invitations_new"] == 7 == run["rows_pipeline"] and run["invitations_skipped_closed"] == 17
    assert run["high_water_before"] is None and run["rows_expired"] == 0 and run["rows_withdrawn"] == 0
    state = _state(db)
    assert state["high_water_at"] == pi._iso(NOW) and state["last_full_sync_at"] == pi._iso(NOW)
    assert state.get("last_incremental_at") is None
    bells = _bells(db, bcp.NOTIFY_NEW_INVITATIONS)
    assert len(bells) == 5 and {b["role"] for b in bells} == set(pi.RFP_REVIEW_ROLES)
    assert bells[0]["message"] == "7 new BuildingConnected invitations" and bells[0]["metadata"]["run_id"] == "run-bc"
    assert bells[0]["mirror_email"] is False and not _bells(db, pi.NOTIFY_NEW_INVITATIONS)
    # An incremental run afterwards stamps last_incremental_at and keeps the full stamp.
    db.tables["rfp_portal_runs"].append(_run(id="run-bc-2", kind="incremental"))
    pi.execute_scan("run-bc-2")
    state = _state(db)
    assert state["last_incremental_at"] == pi._iso(NOW) and state["last_full_sync_at"] == pi._iso(NOW)
    assert db.tables["rfp_portal_runs"][1]["high_water_before"] == pi._iso(NOW)
    assert len(_bells(db, bcp.NOTIFY_NEW_INVITATIONS)) == 5     # nothing new, no second bell
    # The token never reaches a bell, a run row or the state row.
    assert ACCESS not in str(db.tables["notifications"]) + str(db.tables["rfp_portal_runs"]) + str(state)


def test_execute_scan_pages_and_counts_pages(db, board_transport):
    board_transport.board = FakeBoard(page_size=10)
    db.tables["rfp_portal_runs"].append(_run(kind="full"))
    pi.execute_scan("run-bc")
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "complete" and run["pages_scanned"] == 3 and board_transport.board.page_calls == 3
    assert len(db.tables["rfp_portal_invitations"]) == 24


def test_execute_scan_429_mid_pull_is_transient_and_never_advances_the_high_water(db, board_transport):
    board = FakeBoard(page_size=10)
    board.modes = ["ok", 429]      # the second page answers 429 until the retries run out
    board_transport.board = board
    db.tables["rfp_portal_state"].append({"portal": BC, "high_water_at": pi._iso(NOW - timedelta(days=1))})
    db.tables["rfp_portal_runs"].append(_run(kind="full"))
    with pytest.raises(pi.RfpPortalTransient):
        pi.execute_scan("run-bc")
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "running" and "rate limit" in run["last_error"]
    assert _state(db)["high_water_at"] == pi._iso(NOW - timedelta(days=1))      # untouched
    assert len(db.tables["rfp_portal_invitations"]) == 10                       # the first page's progress stands
    assert not _bells(db, bcp.NOTIFY_NEW_INVITATIONS)


def test_a_pull_cut_short_by_the_page_cap_marks_nothing_missing_and_holds_the_high_water(db):
    """An incomplete view of the board (the pull stopped at RFP_BC_MAX_PAGES
    with pages pending) upserts what it pulled but marks no unseen row
    missing, expires or withdraws nothing, keeps the high water mark where
    the run started and records the truncation on the run."""
    settings = _settings(rfp_bc_max_pages=1, rfp_bc_missing_days=3, rfp_bc_expire_days=7)
    before = pi._iso(NOW - timedelta(days=1))
    db.tables["rfp_portal_state"].append({"portal": BC, "high_water_at": before, "last_full_sync_at": "earlier"})
    db.tables["rfp_portal_invitations"].extend([
        _bc_row(id="gone", external_id="gone", bid_number="gone", bid_number_raw="gone", status="match",
                missing_since=pi._iso(NOW - timedelta(days=4))),
        _bc_row(id="late", external_id="late", bid_number="late", bid_number_raw="late", status="create",
                close_at=pi._iso(NOW - timedelta(days=8))),
        _bc_row(id="fresh", external_id="fresh", bid_number="fresh", bid_number_raw="fresh", status="match"),
    ])
    (pages, counts), board = _scan(db, _run(id="run-full", kind="full"), FakeBoard(page_size=10), settings=settings)
    assert pages == 1 and board.page_calls == 1 and counts["rows_pulled"] == 10
    assert counts["truncated"] is True and "RFP_BC_MAX_PAGES cap (1 pages)" in counts["last_error"]
    assert counts["high_water_at"] == _parse(before)                             # never advanced
    assert counts["rows_expired"] == 0 and counts["rows_withdrawn"] == 0
    by_id = {r["id"]: r for r in db.tables["rfp_portal_invitations"]}
    assert len(by_id) == 13                                                       # the first page landed
    assert by_id["fresh"]["missing_since"] is None and by_id["gone"]["status"] == "match" and by_id["late"]["status"] == "create"
    assert all(r.get("missing_since") is None for r in by_id.values() if r["id"] != "gone")
    # after_complete on a truncated pull leaves the state row as it was.
    _portal().after_complete(db, settings, _run(id="run-full", kind="full"), counts)
    assert _state(db) == {"portal": BC, "high_water_at": before, "last_full_sync_at": "earlier"}
    # The same board under a cap it fits: the nightly pass runs and the state moves.
    (pages, counts), board = _scan(db, _run(id="run-full-2", kind="full"), FakeBoard(page_size=10), settings=_settings(rfp_bc_max_pages=3, rfp_bc_missing_days=3, rfp_bc_expire_days=7))
    assert counts["truncated"] is False and "last_error" not in counts and board.page_calls == 3
    by_id = {r["id"]: r for r in db.tables["rfp_portal_invitations"]}
    assert by_id["gone"]["status"] == "withdrawn" and by_id["late"]["status"] == "expired" and by_id["fresh"]["missing_since"] == pi._iso(NOW)


def _parse(value):
    return bc_facts.parse_ts(value)


def test_execute_scan_completes_a_truncated_pull_with_the_note_and_writes_no_state(db, board_transport, monkeypatch):
    capped = _settings(rfp_bc_max_pages=2)
    monkeypatch.setattr(pi, "get_settings", lambda: capped)
    monkeypatch.setattr(bcp, "get_settings", lambda: capped)
    before = pi._iso(NOW - timedelta(days=1))
    db.tables["rfp_portal_state"].append({"portal": BC, "high_water_at": before})
    board_transport.board = FakeBoard(page_size=10)
    db.tables["rfp_portal_runs"].append(_run(kind="full"))
    pi.execute_scan("run-bc")
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "complete" and run["pages_scanned"] == 2 and board_transport.board.page_calls == 2
    assert "RFP_BC_MAX_PAGES cap (2 pages)" in run["last_error"] and run["rows_pulled"] == 20
    assert len(db.tables["rfp_portal_invitations"]) == 20
    assert _state(db) == {"portal": BC, "high_water_at": before}                 # untouched
    assert _bells(db, bcp.NOTIFY_NEW_INVITATIONS)                                 # what landed is still announced
    # A clean run afterwards clears the note and stamps the state.
    monkeypatch.setattr(pi, "get_settings", lambda: _settings())
    monkeypatch.setattr(bcp, "get_settings", lambda: _settings())
    db.tables["rfp_portal_runs"].append(_run(id="run-bc-2", kind="full", scheduled_for=pi._iso(NOW)))
    pi.execute_scan("run-bc-2")
    run = db.tables["rfp_portal_runs"][1]
    assert run["status"] == "complete" and run["last_error"] is None and run["pages_scanned"] == 3
    assert _state(db)["high_water_at"] == pi._iso(NOW) and _state(db)["last_full_sync_at"]


def test_execute_scan_parks_the_run_while_disconnected_and_fails_it_on_a_permanent_answer(db, board_transport):
    db.tables["rfp_oauth_connections"][0].update(status="disconnected", last_error="refresh rejected")
    db.tables["rfp_portal_runs"].append(_run())
    pi.execute_scan("run-bc")
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "queued" and run["last_error"] == "refresh rejected" and run["next_attempt_at"]
    assert board_transport.board.requests == []
    # A permanent refusal fails the run and rings the BC scan-failed bell to IT Admins.
    db.tables["rfp_oauth_connections"][0].update(status="connected", last_error=None)
    board_transport.board.modes = [401]     # 401 -> refresh -> the refresh needs the token endpoint: 404 here -> permanent
    with pytest.raises(pi.RfpPortalPermanent):
        pi.execute_scan("run-bc")
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "failed" and run["last_error"]
    bells = _bells(db, bcp.NOTIFY_SCAN_FAILED)
    assert len(bells) == 1 and bells[0]["role"] == Role.IT_ADMIN and bells[0]["mirror_email"] is False
    assert "BuildingConnected scan failed" in bells[0]["message"] and bells[0]["metadata"]["run_id"] == "run-bc"
    assert not _bells(db, pi.NOTIFY_SCAN_FAILED)


def test_fail_run_picks_the_bell_by_portal_and_dedupes_it(db):
    db.tables["rfp_portal_runs"].extend([_run(id="r-bc", status="running"), _run(id="r-ngem", portal="ngem", status="running")])
    assert pi._fail_run(db, "r-bc", "boom") is True
    assert pi._fail_run(db, "r-bc", "boom") is False
    bells = _bells(db, bcp.NOTIFY_SCAN_FAILED)
    assert len(bells) == 1 and bells[0]["metadata"]["kind"] == "incremental" and bells[0]["metadata"]["portal"] == BC
    db.tables["rfp_portal_runs"].append(_run(id="r-bc-2", status="running"))
    pi._fail_run(db, "r-bc-2", "again")
    assert len(_bells(db, bcp.NOTIFY_SCAN_FAILED)) == 1          # deduped while one is unread
    pi._fail_run(db, "r-ngem", "ngem boom")
    assert len(_bells(db, pi.NOTIFY_SCAN_FAILED)) == 1           # NGEM keeps its own type


# ── The match branch (3.5, D11, D13) ─────────────────────────────────────


def _seed_match(db, *, row=None, projects=(), gcs=(), contacts=(), aliases=()):
    db.tables["general_contractors"].extend([{"id": g, "name": n} for g, n in gcs])
    db.tables["gc_contacts"].extend(list(contacts))
    db.tables["gc_external_aliases"].extend(list(aliases))
    db.tables["projects"].extend(list(projects))
    db.tables["rfp_portal_invitations"].append(row or _bc_row())


def _process(db, settings=None, inv_id=INV):
    settings = settings or _settings()
    row = next(r for r in db.tables["rfp_portal_invitations"] if r["id"] == inv_id)
    return pi._process_invitation(db, dict(row), pi._SweepState(), settings, llm_down=False, renew=lambda: True)


def test_alias_gc_and_a_confident_match_resolve_to_exists_with_the_link_contact_and_files_flag(db, files, lead_contact, llm_answer):
    calls = llm_answer(SAME)
    _seed_match(
        db, gcs=[(G1, "Rafael Companies")], projects=[_project(P1, "Fixture Project 01", bid_at=DUE_01)],
        aliases=[{"id": "al-1", "source": BC, "external_id": GC_AA, "external_name": "GC AA Builders", "gc_id": G1,
                  "confirmed_by": U1, "confirmed_at": "t"}],
    )
    _process(db)
    row = _inv(db)
    assert row["status"] == "exists" and row["match_project_id"] == P1 and row["resolution"] == "system"
    assert row["gc_id"] == G1 and row["gc_kind"] == "alias" and row["gc_candidates"] == [{"gc_id": G1, "name": "Rafael Companies", "score": 0.0}]
    assert row["match_score"] == 1.0 and row["flag_reason"] is None and row["decided_at_step"] == "match"
    assert row["match_weights"]["auto_resolve_enabled"] is True
    links = db.tables["project_gcs"]
    assert len(links) == 1 and links[0]["project_id"] == P1 and links[0]["gc_id"] == G1
    assert links[0]["needs_by"] == "2026-10-16" and links[0]["rfp_match_id"] is None
    assert row["project_gc_id"] == links[0]["id"]
    assert lead_contact == [(G1, row["lead"])]
    assert db.tables["project_gc_contacts"] == [{"id": db.tables["project_gc_contacts"][0]["id"], "project_gc_id": links[0]["id"], "gc_contact_id": "c-lead"}]
    assert files.calls == [(P1, BC, bc_facts.deep_link(row["external_id"]))]
    # The matcher was told the GC and the notes; the LLM saw one candidate.
    assert len(calls) == 1 and "GC AA Builders" in str(calls[0]["messages"])
    # The project's GC name is what the sweep bundle had; nothing NGEM-shaped was written.
    assert row["harvest_id"] is None


def test_domain_gc_already_on_the_project_is_a_duplicate_marker_only(db, files, lead_contact, llm_answer):
    llm_answer(SAME)
    _seed_match(
        db, gcs=[(G1, "Rafael Companies")],
        contacts=[{"id": "c-9", "gc_id": G1, "email": "estimating@gcaabuilders.example.com", "created_at": "t"}],
        projects=[_project(P1, "Fixture Project 01", bid_at=DUE_01, gcs=[G1])],
    )
    _process(db)
    row = _inv(db)
    assert row["status"] == "exists" and row["gc_kind"] == "domain" and row["gc_id"] == G1
    assert db.tables["project_gcs"] == [] and row["project_gc_id"] is None
    assert files.calls == [] and lead_contact == []


def test_provisional_gc_parks_every_confident_match_for_the_gc_card(db, llm_answer):
    llm_answer(SAME)
    _seed_match(db, gcs=[(G1, "GC AA Builders Inc"), (G2, "Other Corp")],
                projects=[_project(P1, "Fixture Project 01", bid_at=DUE_01)])
    _process(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_gc_unresolved"
    assert row["gc_kind"] == "provisional" and row["gc_id"] == G1
    assert [c["gc_id"] for c in row["gc_candidates"]] == [G1, G2] and row["gc_candidates"][0]["score"] > 0.5
    assert row["match_project_id"] == P1 and db.tables["project_gcs"] == []


def test_masked_row_parks_as_nda_required_before_any_scoring(db, llm_answer):
    calls = llm_answer(SAME)
    _seed_match(db, row=_bc_row("nda_masked", close_at=pi._iso(NOW + timedelta(days=10))),
                projects=[_project(P1, "Fixture Project 08", bid_at=pi._iso(NOW + timedelta(days=10)))])
    _process(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "nda_required"
    assert row["match_candidates"] == [] and row["gc_kind"] is None and calls == []


def test_no_due_date_and_submitted_rows_are_withheld_from_create(db, llm_answer):
    llm_answer(SAME)
    _seed_match(db, row=_bc_row("no_due_real"), gcs=[(G1, "GC AF Builders")],
                aliases=[{"id": "al", "source": BC, "external_id": BY_ROLE["no_due_real"]["client"]["company"]["id"],
                          "external_name": "GC AF Builders", "gc_id": G1, "confirmed_by": None, "confirmed_at": "t"}])
    _process(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "no_due_date"
    assert row["match_candidates"] == [] and row["gc_kind"] == "alias"
    submitted = _bc_row("open_undecided_full", id="inv-sub", external_id="sub", bid_number="sub", bid_number_raw="sub",
                        submission_state="SUBMITTED")
    db.tables["rfp_portal_invitations"].append(submitted)
    _process(db, inv_id="inv-sub")
    row = _inv(db, "inv-sub")
    assert row["status"] == "review_match" and row["flag_reason"] == "submitted_unmatched"
    # An exists outcome is never overridden by the refinements.
    db.tables["projects"].append(_project(P1, "Fixture Project 01", bid_at=DUE_01))
    db.tables["gc_external_aliases"].append({"id": "al-2", "source": BC, "external_id": GC_AA, "external_name": "GC AA Builders",
                                             "gc_id": G1, "confirmed_by": None, "confirmed_at": "t"})
    both = _bc_row("open_undecided_full", id="inv-both", external_id="both", bid_number="both", bid_number_raw="both",
                   submission_state="SUBMITTED")
    db.tables["rfp_portal_invitations"].append(both)
    _process(db, inv_id="inv-both")
    assert _inv(db, "inv-both")["status"] == "exists"


def test_a_confirmed_gc_is_never_rewritten_by_the_sweep(db):
    db.tables["general_contractors"].extend([{"id": G1, "name": "Rafael Companies"}, {"id": G2, "name": "Chosen GC"}])
    db.tables["gc_external_aliases"].append({"id": "al", "source": BC, "external_id": GC_AA, "external_name": "GC AA Builders",
                                             "gc_id": G1, "confirmed_by": None, "confirmed_at": "t"})
    db.tables["rfp_portal_invitations"].append(_bc_row(gc_id=G2, gc_kind="alias", gc_confirmed_at=pi._iso(NOW), gc_confirmed_by=U1,
                                                        gc_candidates=[{"gc_id": G2, "name": "Chosen GC", "score": 0.4}]))
    _process(db)
    row = _inv(db)
    assert row["gc_id"] == G2 and row["gc_kind"] == "alias" and row["gc_candidates"] == [{"gc_id": G2, "name": "Chosen GC", "score": 0.4}]
    # No candidate: create, and the create step drained it to done in the same pass (auto creation off).
    assert row["status"] == "done" and row["decided_at_step"] == "create" and row["flag_reason"] == "no_candidate"


def test_the_sweep_never_overwrites_a_gc_confirmed_while_it_held_a_stale_row(db, monkeypatch):
    """The match branch's write is fenced on gc_confirmed_at still being
    null: a reviewer who answers the GC card while the sweep is inside the
    LLM call wins. The row keeps the reviewer's GC, stays at `match` and is
    routed again next tick with the confirmed GC."""
    _seed_match(db, gcs=[(G1, "GC AA Builders Inc"), (G2, "Other Corp")],
                projects=[_project(P1, "Fixture Project 01", bid_at=DUE_01)])
    calls = []

    def answer(feature, **kwargs):
        calls.append(feature)
        if len(calls) == 1:
            # Mid-call: the reviewer picks G2 on the GC card (the alias is written, the row's GC columns too).
            bcp.confirm_gc(db, INV, U1, gc_id=G2)
        return copy.deepcopy(SAME)

    monkeypatch.setattr(llm, "complete_json", answer)
    _process(db)
    row = _inv(db)
    assert row["status"] == "match" and row["match_project_id"] is None          # the routing write missed
    assert row["gc_id"] == G2 and row["gc_kind"] == "alias" and row["gc_confirmed_at"] and row["gc_confirmed_by"] == U1
    assert row["gc_candidates"] is None                                           # the sweep's guess never landed
    # Next tick: the confirmed GC routes the confident match, and the GC columns are left alone.
    _process(db)
    row = _inv(db)
    assert row["status"] == "exists" and row["match_project_id"] == P1
    assert row["gc_id"] == G2 and row["gc_kind"] == "alias" and row["gc_confirmed_by"] == U1
    assert db.tables["project_gcs"][0]["gc_id"] == G2
    # The fence itself: a null-fenced column that is set makes the write miss; a value fence matches on equality.
    fresh = _bc_row(id="inv-f", external_id="f", bid_number="f", bid_number_raw="f", status="match", gc_confirmed_at="t")
    db.tables["rfp_portal_invitations"].append(fresh)
    assert pi._cas(db, "inv-f", "match", {"flag_reason": "x"}, extra={"gc_confirmed_at": None}) is False
    assert pi._cas(db, "inv-f", "match", {"flag_reason": "x"}, extra={"gc_confirmed_at": "t"}) is True
    assert _inv(db, "inv-f")["flag_reason"] == "x"


def test_a_reference_only_name_parks_at_review_match_not_harvest(db):
    db.tables["rfp_portal_invitations"].append(_bc_row(title="RFP 2026-01"))
    _process(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "no_project_name"
    assert [j for j in db.tables["llm_jobs"] if j["job_type"] == pi.JOB_HARVEST] == []


def test_no_candidate_creates_in_the_same_pass_when_automatic_creation_is_on(db, monkeypatch):
    created = []
    monkeypatch.setattr(rfp_create, "create_from_portal", lambda sb, row, *, actor_id, automatic: created.append((row["id"], actor_id, automatic)))
    db.tables["general_contractors"].append({"id": G1, "name": "GC AA Builders"})
    db.tables["gc_external_aliases"].append({"id": "al", "source": BC, "external_id": GC_AA, "external_name": "GC AA Builders",
                                             "gc_id": G1, "confirmed_by": None, "confirmed_at": "t"})
    db.tables["rfp_portal_invitations"].append(_bc_row())
    _process(db, _settings(rfp_create_auto_enabled=True))
    row = _inv(db)
    assert row["status"] == "create" and row["gc_kind"] == "alias" and created == [(INV, None, True)]
    assert row["possible_rebid_project_id"] is None


def test_the_sweep_row_carries_every_fact_the_automatic_creation_copies(db, monkeypatch):
    """The sweep hands its own select to create_from_portal without a
    re-read, and bc_facts.project_facts reads the dated facts, the address
    and the request type from the row (not the payload): the select must
    carry them or every automatic creation loses them."""
    seen = []
    monkeypatch.setattr(rfp_create, "create_from_portal", lambda sb, row, *, actor_id, automatic: seen.append(row))
    monkeypatch.setattr(pi, "get_settings", lambda: _settings(rfp_create_auto_enabled=True))
    stored = _bc_row(status="create", gc_id=G1, gc_kind="alias")
    db.tables["rfp_portal_invitations"].append(stored)
    pi.sweep(db)
    assert len(seen) == 1
    row = seen[0]
    for col in ("job_walk_at", "expected_start_at", "expected_finish_at", "address", "request_type"):
        assert row.get(col) == stored[col] and stored[col], col
    facts = bc_facts.project_facts(row, text_max_chars=20000, notes_max_chars=2000)
    assert facts["job_walk_at"] == stored["job_walk_at"] and facts["address"] == stored["address"]
    assert facts["est_start_date"] == "2027-01-04" and facts["est_finish_date"] == "2027-03-15"


def test_the_create_gate_parks_rows_that_must_not_auto_create_but_never_a_human_decision(db, monkeypatch):
    created = []
    monkeypatch.setattr(rfp_create, "create_from_portal", lambda sb, row, *, actor_id, automatic: created.append(row["id"]))
    auto = _settings(rfp_create_auto_enabled=True)
    cases = {
        "inv-nodue": (dict(close_at=None, gc_kind="alias", gc_id=G1), "no_due_date"),
        "inv-nogc": (dict(gc_kind="none", gc_id=None), "match_gc_unresolved"),
        "inv-nda": (dict(flag_reason="nda_required", gc_kind="alias", gc_id=G1), "nda_required"),
    }
    for inv_id, (over, flag) in cases.items():
        db.tables["rfp_portal_invitations"].append(_bc_row(id=inv_id, external_id=inv_id, bid_number=inv_id, bid_number_raw=inv_id,
                                                            status="create", **over))
        pi._step_create(db, dict(_inv(db, inv_id)), auto)
        row = _inv(db, inv_id)
        assert (row["status"], row["flag_reason"], row["decided_at_step"]) == ("review_match", flag, "create"), inv_id
    assert created == []
    # "Not a match" sent this no-due-date row here: the gate stands aside and the project is made.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-human", external_id="h", bid_number="h", bid_number_raw="h",
                                                        status="create", close_at=None, gc_kind="provisional", gc_id=G1,
                                                        resolution="human"))
    pi._step_create(db, dict(_inv(db, "inv-human")), auto)
    assert created == ["inv-human"]
    # A provisional GC with a due date auto-creates (GC_PROVISIONAL is the plan's job).
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-prov", external_id="p", bid_number="p", bid_number_raw="p",
                                                        status="create", gc_kind="provisional", gc_id=G1))
    pi._step_create(db, dict(_inv(db, "inv-prov")), auto)
    assert created == ["inv-human", "inv-prov"]
    # Automatic creation off: the gate never fires; the row drains to done for the button.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-off", external_id="o", bid_number="o", bid_number_raw="o",
                                                        status="create", close_at=None, gc_kind="none"))
    pi._step_create(db, dict(_inv(db, "inv-off")), _settings())
    assert _inv(db, "inv-off")["status"] == "done"


def test_the_sibling_guard_links_a_package_pair_at_create_time_and_on_the_button(db, monkeypatch):
    created = []
    monkeypatch.setattr(rfp_create, "create_from_portal", lambda sb, row, *, actor_id, automatic: created.append(row["id"]))
    first = _bc_row("same_gc_two_packages_a", id="inv-a", status="created", created_project_id=P1, gc_id=G1, gc_kind="alias")
    second = _bc_row("same_gc_two_packages_b", id="inv-b", status="create", gc_id=G1, gc_kind="alias")
    other_gc = _bc_row("cross_gc_b", id="inv-c", status="create", title="Fixture Project 18", gc_id=G2, gc_kind="alias")
    db.tables["rfp_portal_invitations"].extend([first, second, other_gc])
    pi._step_create(db, dict(second), _settings(rfp_create_auto_enabled=True))
    row = _inv(db, "inv-b")
    assert row["status"] == "exists" and row["match_project_id"] == P1 and row["flag_reason"] == "sibling_package"
    assert row["sibling_of"] == "inv-a" and row["resolution"] == "system" and row["decided_at_step"] == "create"
    assert created == []
    # Another GC with the same name is not a sibling.
    pi._step_create(db, dict(other_gc), _settings(rfp_create_auto_enabled=True))
    assert created == ["inv-c"]
    # The Create button path on a done sibling: linked, then 409 not actionable.
    third = _bc_row("same_gc_two_packages_b", id="inv-d", external_id="d", bid_number="d", bid_number_raw="d",
                    status="done", gc_id=G1, gc_kind="alias")
    db.tables["rfp_portal_invitations"].append(third)
    with pytest.raises(pi.RfpPortalError) as exc:
        pi.create_project(db, "inv-d", U1)
    assert exc.value.code == pi.CODE_NOT_ACTIONABLE
    assert _inv(db, "inv-d")["status"] == "exists" and _inv(db, "inv-d")["sibling_of"] == "inv-a"
    assert db.tables["audit_log"][-1]["payload"]["linked_sibling"] is True


def test_find_sibling_keys_on_gc_id_or_the_company_id(db):
    """D12b: two BuildingConnected company records aliased to one GC are
    the same GC, so a twin with the same gc_id and name counts when the
    company ids differ, and the reverse (same company id, gc_id not yet
    resolved on one side) still counts; a different GC with the same name
    never does."""
    leader = _bc_row("same_gc_two_packages_a", id="inv-a", external_id="a", bid_number="a", bid_number_raw="a",
                     status="created", created_project_id=P1, gc_id=G1, gc_kind="alias",
                     gc_external_id="aaaaaaaaaaaaaaaaaaaaaaaa")
    db.tables["rfp_portal_invitations"].append(leader)
    # Same name, same resolved GC, another company record: found by gc_id.
    twin = _bc_row("same_gc_two_packages_b", id="inv-b", external_id="b", bid_number="b", bid_number_raw="b",
                   status="create", gc_id=G1, gc_kind="alias", gc_external_id="bbbbbbbbbbbbbbbbbbbbbbbb")
    assert bcp.find_sibling(db, twin)["id"] == "inv-a"
    # Same company record, gc_id not resolved on the new row: found by the company id.
    unresolved = _bc_row("same_gc_two_packages_b", id="inv-c", external_id="c", bid_number="c", bid_number_raw="c",
                         status="create", gc_id=None, gc_kind=None, gc_external_id="aaaaaaaaaaaaaaaaaaaaaaaa")
    assert bcp.find_sibling(db, unresolved)["id"] == "inv-a"
    # Same company record on the row, the leader's gc_id different (re-aliased): still the company id.
    other_gc = _bc_row("same_gc_two_packages_b", id="inv-d", external_id="d", bid_number="d", bid_number_raw="d",
                       status="create", gc_id=G2, gc_kind="alias", gc_external_id="aaaaaaaaaaaaaaaaaaaaaaaa")
    assert bcp.find_sibling(db, other_gc)["id"] == "inv-a"
    # A different GC and a different company record with the same name is not a sibling.
    stranger = _bc_row("same_gc_two_packages_b", id="inv-e", external_id="e", bid_number="e", bid_number_raw="e",
                       status="create", gc_id=G2, gc_kind="alias", gc_external_id="eeeeeeeeeeeeeeeeeeeeeeee")
    assert bcp.find_sibling(db, stranger) is None
    # A value that cannot ride inside the or-filter falls back to the company id alone.
    odd = dict(twin, gc_id="not a uuid, with commas")
    assert bcp.find_sibling(db, odd) is None
    # The create step links the gc_id twin instead of creating a second project.
    db.tables["rfp_portal_invitations"].append(twin)
    pi._step_create(db, dict(twin), _settings(rfp_create_auto_enabled=True))
    assert _inv(db, "inv-b")["status"] == "exists" and _inv(db, "inv-b")["sibling_of"] == "inv-a"


def test_a_merge_refused_by_a_closed_project_parks_the_row_for_a_person(db, llm_answer, monkeypatch):
    llm_answer(SAME)
    _seed_match(db, gcs=[(G1, "Rafael Companies")], projects=[_project(P1, "Fixture Project 01", bid_at=DUE_01)],
                aliases=[{"id": "al", "source": BC, "external_id": GC_AA, "external_name": "GC AA Builders", "gc_id": G1,
                          "confirmed_by": None, "confirmed_at": "t"}])

    def refuse(sb, project_id, settings):
        raise rfp_email_ingest.RfpMatchError(rfp_email_ingest.CODE_PROJECT_CLOSED, "That project is closed to new GCs.")

    monkeypatch.setattr(rfp_email_ingest, "_open_project", refuse)
    _process(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_uncertain"
    assert row["last_error"] == "That project is closed to new GCs." and row["resolution"] is None
    assert row["match_project_id"] == P1 and db.tables["project_gcs"] == []


def test_facts_for_and_route_params(db):
    adapter = _portal()
    row = _bc_row("with_trade_instructions")
    facts = adapter.facts_for(row)
    assert facts.project_name == "Fixture Project 02" and facts.gc_name == "GC AB Builders"
    assert facts.bid_due_at == bc_facts.parse_ts(row["close_at"]) and facts.has_time is True
    assert facts.bid_notes and len(facts.bid_notes) <= 400 and "<" not in facts.bid_notes
    midnight = _bc_row(close_at="2026-10-17T07:00:00+00:00")   # 00:00 PDT
    assert adapter.facts_for(midnight).has_time is False
    assert adapter.facts_for(_bc_row(close_at=None, payload={})).bid_notes is None
    gc = gc_aliases.GcResolve(kind="alias", gc_id=G1, gc_name="x", contact_id=None)
    project = _project(P1, "x", bid_at=DUE_01, gcs=[G1])
    params = adapter.route_params(gc, {"project_id": P1}, {P1: project}, _settings())
    assert params == {"gc_resolved": True, "gc_on_project": True, "sender_verified": True, "auto_merge": True}
    prov = gc_aliases.GcResolve(kind="provisional", gc_id=G1, gc_name="x", contact_id=None)
    assert adapter.route_params(prov, None, {}, _settings(rfp_bc_auto_resolve_enabled=False)) == {
        "gc_resolved": False, "gc_on_project": False, "sender_verified": True, "auto_merge": False,
    }
    assert adapter.route_params(None, {"project_id": P1}, {P1: project}, _settings())["gc_resolved"] is False
    assert adapter.refine(_bc_row(), "exists", None) == ("exists", None)
    assert adapter.refine(_bc_row(close_at=None), "review_match", "match_uncertain") == ("review_match", "match_uncertain")
    assert adapter.pre_match_park(_bc_row("nda_masked")) == ("review_match", "nda_required")
    assert adapter.pre_match_park(_bc_row()) is None
    assert adapter.gc_from_row(_bc_row(gc_id=G1, gc_kind="provisional")).kind == "provisional"
    assert adapter.gc_from_row(_bc_row(gc_id=G1, gc_kind="provisional", gc_confirmed_at="t")).kind == "alias"
    assert adapter.gc_from_row(_bc_row()).kind == "none"


# ── Human actions on BuildingConnected rows ──────────────────────────────


def test_resolve_new_sends_a_bc_row_to_create_and_runs_the_create_step_inline(db):
    db.tables["rfp_portal_invitations"].append(_bc_row(status="review_match", flag_reason="match_uncertain", match_project_id=P1,
                                                        match_candidates=[{"project_id": P2}]))
    out = pi.resolve_new(db, INV, U1)
    assert out["status"] == "done" and out["decided_at_step"] == "create"          # create, drained for the button
    assert out["excluded_project_ids"] == [P1, P2] and out["resolution"] == "human"
    assert db.tables["audit_log"][0]["payload"]["to_status"] == "create"
    assert [j for j in db.tables["llm_jobs"] if j["job_type"] == pi.JOB_HARVEST] == []


def test_resolve_exists_runs_the_merge_side_effects_for_a_bc_row(db, files, lead_contact):
    db.tables["projects"].append(_project(P1, "Fixture Project 01", bid_at=DUE_01))
    db.tables["general_contractors"].append({"id": G1, "name": "Rafael Companies"})
    db.tables["rfp_portal_invitations"].append(_bc_row(status="review_match", flag_reason="match_gc_unresolved",
                                                        gc_id=G1, gc_kind="provisional",
                                                        match_candidates=[{"project_id": P1}]))
    # "Same as project" while the GC is a similarity guess is refused (D11): the
    # GC card answers first, so nothing is linked or filed under the guess.
    with pytest.raises(pi.RfpPortalError) as refused:
        pi.resolve_exists(db, INV, P1, U1)
    assert refused.value.code == bcp.CODE_GC_REQUIRED and "GC card" in str(refused.value)
    assert _inv(db)["status"] == "review_match" and db.tables["project_gcs"] == [] and lead_contact == []
    assert db.tables["gc_contacts"] == [] and files.calls == []
    # The card answered (the alias written, the row rematched): the decision goes through.
    bcp.confirm_gc(db, INV, U1, gc_id=G1)
    _inv(db).update(status="review_match", flag_reason="match_uncertain")
    out = pi.resolve_exists(db, INV, P1, U1)
    assert out["status"] == "exists" and out["resolution"] == "human"
    links = db.tables["project_gcs"]
    assert len(links) == 1 and links[0]["gc_id"] == G1 and links[0]["needs_by"] == "2026-10-16"
    assert out["project_gc_id"] == links[0]["id"] and lead_contact[0][0] == G1
    assert files.calls == [(P1, BC, out["external_url"])]
    assert db.tables["project_gc_contacts"][0]["gc_contact_id"] == "c-lead"
    # An NDA-masked row has no company to confirm: the marker is set, nothing is linked.
    db.tables["rfp_portal_invitations"].append(_bc_row("nda_masked", id="inv-m", external_id="m", bid_number="m", bid_number_raw="m",
                                                        status="review_match", flag_reason="nda_required",
                                                        match_candidates=[{"project_id": P1}]))
    out = pi.resolve_exists(db, "inv-m", P1, U1)
    assert out["status"] == "exists" and out["project_gc_id"] is None and len(links) == 1


def test_on_exists_never_links_a_provisional_or_absent_gc(db, files, lead_contact):
    """The shared helper itself (the system path and the human path both
    reach it): only a confirmed GC (alias, contact, domain) becomes a
    project_gcs link and a bid contact; the files flag is set either way."""
    db.tables["projects"].append(_project(P1, "Fixture Project 01", bid_at=DUE_01))
    adapter = _portal()
    row = _bc_row(status="exists", match_project_id=P1, gc_id=G1, gc_kind="provisional")
    db.tables["rfp_portal_invitations"].append(row)
    out = adapter.on_exists(db, row, P1, gc_aliases.GcResolve(kind="provisional", gc_id=G1, gc_name="x", contact_id=None), _settings(), merged=True)
    assert out == {"project_gc_id": None, "contact_id": None, "files_needed": True}
    assert db.tables["project_gcs"] == [] and lead_contact == [] and files.calls == [(P1, BC, row["external_url"])]
    # gc None falls back to the row: still provisional, still no link; confirmed on the row, linked.
    assert adapter.on_exists(db, row, P1, None, _settings(), merged=True)["project_gc_id"] is None
    row.update(gc_confirmed_at=pi._iso(NOW))
    out = adapter.on_exists(db, row, P1, None, _settings(), merged=True)
    assert out["project_gc_id"] and db.tables["project_gcs"][0]["gc_id"] == G1 and lead_contact[0][0] == G1


def test_on_exists_stores_the_link_id_before_the_contact_and_unwinds_a_failed_store(db, files, monkeypatch):
    """The project_gcs link id lands on the invitation right after the
    insert: a contact failure later leaves a link reopen can still take
    back (best effort, no raise), and a failed store of the id removes the
    fresh link so nothing is stranded."""
    db.tables["projects"].append(_project(P1, "Fixture Project 01", bid_at=DUE_01))
    adapter = _portal()
    row = _bc_row(status="exists", match_project_id=P1, gc_id=G1, gc_kind="alias", gc_confirmed_at=pi._iso(NOW))
    db.tables["rfp_portal_invitations"].append(row)

    def contact_down(sb, gc_id, lead):
        raise RuntimeError("gc_contacts insert failed")

    monkeypatch.setattr(rfp_create, "ensure_lead_contact", contact_down, raising=False)
    out = adapter.on_exists(db, row, P1, None, _settings(), merged=True)
    links = db.tables["project_gcs"]
    assert len(links) == 1 and out["project_gc_id"] == links[0]["id"] and out["contact_id"] is None
    assert _inv(db)["project_gc_id"] == links[0]["id"] and db.tables["project_gc_contacts"] == []
    assert out["files_needed"] is True
    # The store of project_gc_id itself fails: the link just inserted is removed and the failure propagates.
    db.tables["project_gcs"].clear()
    _inv(db)["project_gc_id"] = None
    original_table = db.table

    def table(name):
        query = original_table(name)
        if name == bcp.INVITATIONS_TABLE:
            real_update = query.update

            def update(payload):
                if "project_gc_id" in payload:
                    raise RuntimeError("postgrest down")
                return real_update(payload)

            query.update = update
        return query

    monkeypatch.setattr(db, "table", table)
    with pytest.raises(RuntimeError, match="postgrest down"):
        adapter.on_exists(db, row, P1, None, _settings(), merged=True)
    assert db.tables["project_gcs"] == [] and _inv(db)["project_gc_id"] is None


def test_reopen_removes_the_link_by_id_and_refuses_when_a_proposal_was_sent(db, monkeypatch):
    calls = []

    def fake_remove(project_id, gc_id, *, link_id, refuse_if_sent, via_unmerge):
        calls.append((project_id, gc_id, link_id, refuse_if_sent, via_unmerge))
        if link_id == "pg-sent":
            raise pi.RfpPortalError(pi.CODE_NOT_ACTIONABLE, "A proposal went to this GC.")
        return True

    monkeypatch.setattr(bcp, "_remove_gc_link", fake_remove)
    db.tables["rfp_portal_invitations"].append(_bc_row(status="exists", match_project_id=P1, gc_id=G1, project_gc_id="pg-1"))
    out = pi.reopen(db, INV, "wrong project", U1)
    assert out["status"] == "match" and out["project_gc_id"] is None and out["match_project_id"] is None
    assert calls == [(P1, G1, "pg-1", True, True)]
    assert db.tables["audit_log"][-1]["payload"]["removed_project_gc_id"] == "pg-1"
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-2", external_id="2", bid_number="2", bid_number_raw="2",
                                                        status="exists", match_project_id=P1, gc_id=G1, project_gc_id="pg-sent"))
    with pytest.raises(pi.RfpPortalError):
        pi.reopen(db, "inv-2", None, U1)
    assert _inv(db, "inv-2")["status"] == "exists" and _inv(db, "inv-2")["project_gc_id"] == "pg-sent"
    # A marker-only row (no link) reopens without touching the proposal service.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-3", external_id="3", bid_number="3", bid_number_raw="3",
                                                        status="exists", match_project_id=P1))
    assert pi.reopen(db, "inv-3", None, U1)["status"] == "match" and len(calls) == 2


def test_remove_gc_link_seam_maps_the_proposal_refusal(monkeypatch):
    from app.services import proposal_send

    monkeypatch.setattr(proposal_send, "remove_gc_link", lambda *a, **k: (_ for _ in ()).throw(proposal_send.ProposalSendError("sent already")))
    with pytest.raises(pi.RfpPortalError) as exc:
        bcp._remove_gc_link(P1, G1, link_id="pg", refuse_if_sent=True, via_unmerge=True)
    assert exc.value.code == pi.CODE_NOT_ACTIONABLE and "sent already" in str(exc.value)
    monkeypatch.setattr(proposal_send, "remove_gc_link", lambda *a, **k: False)
    assert bcp._remove_gc_link(P1, G1, link_id="pg", refuse_if_sent=True, via_unmerge=True) is False


def test_confirm_gc_pick_writes_the_alias_updates_the_row_and_rematches(db):
    db.tables["general_contractors"].extend([{"id": G1, "name": "Guess Corp"}, {"id": G2, "name": "Chosen GC"}])
    db.tables["rfp_portal_invitations"].append(_bc_row(status="review_match", flag_reason="match_gc_unresolved",
                                                        gc_id=G1, gc_kind="provisional", attempts=3, last_error="x"))
    out = bcp.confirm_gc(db, INV, U1, gc_id=G2)
    alias = db.tables["gc_external_aliases"][0]
    assert (alias["source"], alias["external_id"], alias["external_name"], alias["gc_id"], alias["confirmed_by"]) == (BC, GC_AA, "GC AA Builders", G2, U1)
    assert out["gc"] == {"external_id": GC_AA, "external_name": "GC AA Builders", "gc_id": G2, "gc_name": "Chosen GC",
                         "kind": "alias", "confirmed_at": pi._iso(NOW), "candidates": []}
    assert out["gc_confirmed_by"] == U1 and out["gc_confirmed_by_name"] == "Estimating Admin" if U1 == ADMIN_USER else True
    assert out["status"] == "match" and out["flag_reason"] is None and out["attempts"] == 0 and out["last_error"] is None
    audit = db.tables["audit_log"][-1]
    assert audit["action"] == "rfp_portal.confirm_gc" and audit["payload"]["rematch"] is True and audit["payload"]["was_gc_id"] == G1
    # A row not parked for the GC keeps its status.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-2", external_id="2", bid_number="2", bid_number_raw="2",
                                                        status="review_match", flag_reason="match_uncertain", gc_id=G1, gc_kind="provisional"))
    assert bcp.confirm_gc(db, "inv-2", U1, gc_id=G2)["status"] == "review_match"
    # Refusals: a masked row, a bad id, a vanished GC, an NGEM row.
    db.tables["rfp_portal_invitations"].append(_bc_row("nda_masked", id="inv-m", external_id="m", bid_number="m", bid_number_raw="m"))
    with pytest.raises(pi.RfpPortalError) as masked:
        bcp.confirm_gc(db, "inv-m", U1, gc_id=G2)
    assert masked.value.code == bcp.CODE_GC_REQUIRED
    with pytest.raises(pi.RfpPortalError) as bad:
        bcp.confirm_gc(db, "inv-2", U1, gc_id="not-a-uuid")
    assert bad.value.code == bcp.CODE_GC_REQUIRED
    with pytest.raises(pi.RfpPortalError) as gone:
        bcp.confirm_gc(db, "inv-2", U1, gc_id="6a000000-0000-4000-8000-00000000dead")
    assert gone.value.code == bcp.CODE_GC_REQUIRED
    db.tables["rfp_portal_invitations"].append({**_bc_row(id="inv-ngem", external_id="n", bid_number="n", bid_number_raw="n"), "portal": "ngem"})
    with pytest.raises(pi.RfpPortalError) as ngem:
        bcp.confirm_gc(db, "inv-ngem", U1, gc_id=G2)
    assert ngem.value.code == pi.CODE_NOT_ACTIONABLE


def test_confirm_gc_swaps_the_project_gc_or_clears_the_pending_flag(db, monkeypatch):
    swaps = []
    monkeypatch.setattr(rfp_create, "swap_project_gc",
                        lambda sb, project_id, old, new, lead, actor: swaps.append((project_id, old, new, lead, actor)) or {"removed_old": True, "project_gc_id": "pg-new"},
                        raising=False)
    db.tables["general_contractors"].extend([{"id": G1, "name": "Guess Corp"}, {"id": G2, "name": "Chosen GC"}])
    db.tables["projects"].append({**_project(P1, "Fixture Project 01", bid_at=DUE_01), "gc_confirm_pending": True})
    db.tables["rfp_portal_invitations"].append(_bc_row(status="created", created_project_id=P1, gc_id=G1, gc_kind="provisional"))
    out = bcp.confirm_gc(db, INV, U1, gc_id=G2)
    # The swap gets the stored lead (first/last names); the detail shows the contract's shape.
    assert swaps == [(P1, G1, G2, _inv(db)["lead"], U1)] and out["project_gc_id"] == "pg-new" and out["gc_id"] == G2
    assert out["lead"]["name"] == "Lead Person00"
    assert db.tables["projects"][0]["gc_confirm_pending"] is True      # the swap owns that write
    # "Same GC" on the creator row goes through the swap with the GC unchanged: the swap attaches the
    # lead the creation withheld from the provisional GC and clears the pending flag.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-same", external_id="s", bid_number="s", bid_number_raw="s",
                                                        status="created", created_project_id=P1, gc_id=G2, gc_kind="provisional"))
    out = bcp.confirm_gc(db, "inv-same", U1, gc_id=G2)
    assert swaps[-1] == (P1, G2, G2, _inv(db, "inv-same")["lead"], U1) and len(swaps) == 2
    assert out["project_gc_id"] == "pg-new"
    # An exists row whose link the merge did not add (marker only) never swaps the project's own GC,
    # and never answers the project's open question either (another package asked it).
    db.tables["projects"][0]["gc_confirm_pending"] = True
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-dup", external_id="d", bid_number="d", bid_number_raw="d",
                                                        status="exists", match_project_id=P1, gc_id=G1, gc_kind="domain"))
    bcp.confirm_gc(db, "inv-dup", U1, gc_id=G2)
    assert len(swaps) == 2 and db.tables["projects"][0]["gc_confirm_pending"] is True
    # A package sibling (exists through D12, no GC of its own, no link) picks the right GC:
    # the alias is written, the row follows, the creator's question on the project stays open.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-sib", external_id="sb", bid_number="sb", bid_number_raw="sb",
                                                        status="exists", match_project_id=P1, sibling_of=INV,
                                                        flag_reason="sibling_package", gc_id=None, gc_kind=None))
    out = bcp.confirm_gc(db, "inv-sib", U1, gc_id=G2)
    assert out["gc_id"] == G2 and out["gc_kind"] == "alias" and len(swaps) == 2
    assert db.tables["projects"][0]["gc_confirm_pending"] is True
    assert db.tables["audit_log"][-1]["payload"]["project_id"] == P1
    # A review_match row's best candidate is not a project: nothing on the project is written.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-cand", external_id="cd", bid_number="cd", bid_number_raw="cd",
                                                        status="review_match", flag_reason="match_gc_unresolved",
                                                        match_project_id=P1, gc_id=G1, gc_kind="provisional"))
    out = bcp.confirm_gc(db, "inv-cand", U1, gc_id=G1)
    assert out["status"] == "match" and len(swaps) == 2 and db.tables["projects"][0]["gc_confirm_pending"] is True
    assert db.tables["audit_log"][-1]["payload"]["project_id"] is None
    # "Add a GC": the alias service creates it; the row and the swap follow.
    monkeypatch.setattr(gc_aliases, "create_gc_and_confirm",
                        lambda sb, **k: {"alias": {}, "gc_id": G1, "gc_name": k["name"], "contact_id": "c-new", "reused": False})
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-new", external_id="w", bid_number="w", bid_number_raw="w",
                                                        status="created", created_project_id=P1, gc_id=G2, gc_kind="provisional", project_gc_id="pg-old"))
    out = bcp.confirm_gc(db, "inv-new", U1, create={"name": "Brand New GC", "contact_email": "a@b.c"})
    assert out["gc_id"] == G1 and swaps[-1][:3] == (P1, G2, G1)
    assert db.tables["audit_log"][-1]["payload"]["created"] is True and db.tables["audit_log"][-1]["payload"]["contact_id"] == "c-new"


def test_confirm_gc_same_gc_on_the_creator_attaches_the_lead_contact(db, lead_contact, monkeypatch):
    """Creation never files the lead under a provisional GC (rfp_create);
    "Yes, same GC" on the row that created the project is what attaches
    it, through the real swap_project_gc with the GC unchanged: the lead
    becomes the bid contact on the existing link, no second link, the
    pending flag clears."""
    monkeypatch.setattr(rfp_create, "audit", lambda *a, **k: None)
    db.tables["general_contractors"].append({"id": G1, "name": "Guess Corp"})
    db.tables["projects"].append({**_project(P1, "Fixture Project 01", bid_at=DUE_01), "gc_confirm_pending": True})
    db.tables["project_gcs"].append({"id": "pg-1", "project_id": P1, "gc_id": G1, "needs_by": "2026-10-16"})
    db.tables["rfp_portal_invitations"].append(_bc_row(status="created", created_project_id=P1, gc_id=G1, gc_kind="provisional"))
    out = bcp.confirm_gc(db, INV, U1, gc_id=G1)
    lead = _inv(db)["lead"]
    assert lead_contact == [(G1, lead)]
    assert [(c["project_gc_id"], c["gc_contact_id"]) for c in db.tables["project_gc_contacts"]] == [("pg-1", "c-lead")]
    assert [link["id"] for link in db.tables["project_gcs"]] == ["pg-1"]
    assert db.tables["projects"][0]["gc_confirm_pending"] is False
    assert out["gc_id"] == G1 and out["gc_kind"] == "alias" and out["project_gc_id"] == "pg-1"
    assert db.tables["gc_external_aliases"][0]["gc_id"] == G1


def test_an_alias_change_reopens_the_question_the_merged_row_answers(db):
    """gc_aliases.repoint / delete log a `gc_alias` entry on a linked row
    and raise the project's flag without touching the row's confirmation:
    the entry newer than the confirmation is an open question, and the
    row's answer on the card clears the flag."""
    db.tables["general_contractors"].extend([{"id": G1, "name": "Old GC"}, {"id": G2, "name": "New GC"}])
    db.tables["projects"].append({**_project(P2, "Fixture Project 01", bid_at=DUE_01, gcs=[G1]), "gc_confirm_pending": True})
    confirmed = pi._iso(NOW - timedelta(days=1))
    db.tables["rfp_portal_invitations"].append(_bc_row(
        status="exists", match_project_id=P2, gc_id=G1, gc_kind="alias", gc_confirmed_at=confirmed,
        change_log=[{"at": pi._iso(NOW - timedelta(hours=1)), "field": "gc_alias", "old": G1, "new": G2, "run_id": None}],
    ))
    assert bcp.open_gc_question_at(_inv(db)) == NOW - timedelta(hours=1)
    # An entry older than the confirmation, another field, or a masked row is no open question.
    assert bcp.open_gc_question_at({**_inv(db), "gc_confirmed_at": pi._iso(NOW)}) is None
    assert bcp.open_gc_question_at({**_inv(db), "change_log": [{"at": pi._iso(NOW), "field": "title"}]}) is None
    assert bcp.open_gc_question_at({**_inv(db), "gc_external_id": None}) is None
    bcp.confirm_gc(db, INV, U1, gc_id=G1)
    assert db.tables["projects"][0]["gc_confirm_pending"] is False
    assert bcp.open_gc_question_at(_inv(db)) is None


def test_the_sweep_never_writes_back_a_gc_resolved_for_a_company_the_board_swapped_meanwhile(db, monkeypatch):
    """_step_match fences its write on the platform company the tick read:
    a scan that swaps the GC on the board while the sweep is inside the
    LLM call (and clears the row's resolution) wins; the stale routing
    misses, the row stays at `match` and the next tick resolves the new
    company."""
    _seed_match(db, gcs=[(G1, "GC AA Builders Inc"), (G2, "GC AO Builders")],
                projects=[_project(P1, "Fixture Project 01", bid_at=DUE_01)],
                aliases=[{"id": "al-ao", "source": BC, "external_id": GC_AO, "external_name": "GC AO Builders",
                          "gc_id": G2, "confirmed_by": U1, "confirmed_at": "t"}])
    calls = []

    def answer(feature, **kwargs):
        calls.append(feature)
        if len(calls) == 1:
            # Mid-call: the board swaps the client company; the scan writes it and clears the resolution.
            _inv(db).update(gc_external_id=GC_AO, gc_external_name="GC AO Builders", **{
                "gc_id": None, "gc_kind": None, "gc_candidates": None, "gc_confirmed_at": None, "gc_confirmed_by": None})
        return copy.deepcopy(SAME)

    monkeypatch.setattr(llm, "complete_json", answer)
    _process(db)
    row = _inv(db)
    assert row["status"] == "match" and row["match_project_id"] is None           # the stale write missed
    assert row["gc_id"] is None and row["gc_kind"] is None and row["gc_candidates"] is None
    _process(db)
    row = _inv(db)
    assert row["gc_id"] == G2 and row["gc_kind"] == "alias"                        # the new company, resolved afresh
    # The fence itself: the company id is a value fence, matched on equality.
    fresh = _bc_row(id="inv-f2", external_id="f2", bid_number="f2", bid_number_raw="f2", status="match", gc_external_id="x")
    db.tables["rfp_portal_invitations"].append(fresh)
    assert pi._cas(db, "inv-f2", "match", {"flag_reason": "y"}, extra={"gc_external_id": "other"}) is False
    assert pi._cas(db, "inv-f2", "match", {"flag_reason": "y"}, extra={"gc_external_id": "x"}) is True


def test_the_project_gc_card_serves_the_row_whose_own_gc_question_is_open(db):
    """routers/projects._bc_invitation_for_project: a linked row whose board
    swap (or alias change) raised the flag is served before the creator
    row; the newest open question wins; a review_match candidate never
    counts; with nothing asking, the creator row as before."""
    from app.routers import projects as pr

    def row(inv_id, **over):
        return _bc_row(id=inv_id, external_id=inv_id, bid_number=inv_id, bid_number_raw=inv_id, **over)

    def swap_entry(minutes, field="gc_external_id"):
        return {"at": pi._iso(NOW + timedelta(minutes=minutes)), "field": field, "old": "a", "new": "b", "run_id": "r"}

    db.tables["rfp_portal_invitations"].extend([
        row("inv-creator", status="created", created_project_id=P1, gc_id=G1, gc_kind="alias", gc_confirmed_at=pi._iso(NOW)),
        row("inv-merged", status="exists", match_project_id=P1, gc_id=G1, gc_kind="provisional"),
        row("inv-cand", status="review_match", match_project_id=P1, gc_confirmed_at=None,
            change_log=[swap_entry(30)]),
    ])
    assert pr._bc_invitation_for_project(db, P1)["id"] == "inv-creator"
    _inv(db, "inv-merged")["change_log"] = [swap_entry(5)]
    assert pr._bc_invitation_for_project(db, P1)["id"] == "inv-merged"
    # An alias change on another merged row, raised later, is served first.
    db.tables["rfp_portal_invitations"].append(
        row("inv-alias", status="exists", match_project_id=P1, gc_id=G2, gc_kind="alias",
            gc_confirmed_at=pi._iso(NOW - timedelta(days=1)), change_log=[swap_entry(10, "gc_alias")]))
    assert pr._bc_invitation_for_project(db, P1)["id"] == "inv-alias"
    # Answered (confirmed after the entry): back to the next open one, then to the creator.
    _inv(db, "inv-alias")["gc_confirmed_at"] = pi._iso(NOW + timedelta(minutes=20))
    assert pr._bc_invitation_for_project(db, P1)["id"] == "inv-merged"
    _inv(db, "inv-merged")["gc_confirmed_at"] = pi._iso(NOW + timedelta(minutes=20))
    assert pr._bc_invitation_for_project(db, P1)["id"] == "inv-creator"
    assert bcp.card_invitation([], P1) is None


def test_apply_dates_patches_the_project_and_moves_needs_by(db, monkeypatch):
    applied = []
    monkeypatch.setattr(rfp_create, "apply_project_dates",
                        lambda sb, project_id, patch, actor, *, source: applied.append((project_id, patch, actor, source)) or {"id": project_id, **patch},
                        raising=False)
    db.tables["project_gcs"].append({"id": "pg-1", "project_id": P1, "gc_id": G1, "needs_by": "2026-09-01"})
    db.tables["rfp_portal_invitations"].append(_bc_row(status="created", created_project_id=P1, gc_id=G1, project_gc_id="pg-1"))
    out = bcp.apply_dates(db, INV, U1, ["actual_bid_at", "est_start_date", "job_walk_at", "bogus", "est_finish_date"])
    row = _inv(db)
    expected = {
        "actual_bid_at": DUE_01,
        "job_walk_at": row["job_walk_at"],
        "est_start_date": bc_facts.pacific_date(row["expected_start_at"]).isoformat(),
        "est_finish_date": bc_facts.pacific_date(row["expected_finish_at"]).isoformat(),
    }
    assert applied == [(P1, expected, U1, BC)]
    assert out["applied"] == list(expected) and out["project"]["id"] == P1
    assert db.tables["project_gcs"][0]["needs_by"] == "2026-10-16"
    assert db.tables["audit_log"][-1]["payload"] == {"project_id": P1, "applied": list(expected), "needs_by_moved": True}
    # Only the ticked fields with a value; needs_by untouched when the due date was not ticked.
    db.tables["project_gcs"][0]["needs_by"] = "2026-09-01"
    out = bcp.apply_dates(db, INV, U1, ["job_walk_at"])
    assert out["applied"] == ["job_walk_at"] and db.tables["project_gcs"][0]["needs_by"] == "2026-09-01"
    with pytest.raises(pi.RfpPortalError) as none:
        bcp.apply_dates(db, INV, U1, ["bogus"])
    assert none.value.code == bcp.CODE_DATES_UNCHANGED
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-nd", external_id="nd", bid_number="nd", bid_number_raw="nd",
                                                        status="created", created_project_id=P1, close_at=None))
    with pytest.raises(pi.RfpPortalError) as empty:
        bcp.apply_dates(db, "inv-nd", U1, ["actual_bid_at"])
    assert empty.value.code == bcp.CODE_DATES_UNCHANGED
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-np", external_id="np", bid_number="np", bid_number_raw="np", status="done"))
    with pytest.raises(pi.RfpPortalError) as no_project:
        bcp.apply_dates(db, "inv-np", U1, ["actual_bid_at"])
    assert no_project.value.code == pi.CODE_PROJECT_REQUIRED
    # A review_match row's best candidate is not a project: refused, nothing patched.
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-cand", external_id="cd", bid_number="cd", bid_number_raw="cd",
                                                        status="review_match", flag_reason="match_uncertain",
                                                        match_project_id=P1, gc_id=G1))
    with pytest.raises(pi.RfpPortalError) as candidate:
        bcp.apply_dates(db, "inv-cand", U1, ["actual_bid_at"])
    assert candidate.value.code == pi.CODE_PROJECT_REQUIRED and len(applied) == 2
    # An exists row without a stored link id moves the pair's link instead.
    db.tables["project_gcs"].append({"id": "pg-2", "project_id": P2, "gc_id": G1, "needs_by": None})
    db.tables["rfp_portal_invitations"].append(_bc_row(id="inv-ex", external_id="ex", bid_number="ex", bid_number_raw="ex",
                                                        status="exists", match_project_id=P2, gc_id=G1))
    bcp.apply_dates(db, "inv-ex", U1, ["actual_bid_at"])
    assert db.tables["project_gcs"][1]["needs_by"] == "2026-10-16"


# ── Reads: lanes, ordering, sources, status, detail ──────────────────────


def _seed_lanes(db):
    rows = [
        ("r1", "review_match", DUE_01, "Alpha Tower", "GC AA Builders", "Electrical", "UNDECIDED"),
        ("r2", "match", "2026-10-02T02:00:00+00:00", "Beta Plant", "GC AB Builders", "Low Voltage", "WILL_SUBMIT"),
        ("r3", "create", None, "Gamma Notice", "GC AC Builders", "Electrical", "UNDECIDED"),
        ("r4", "done", "2026-10-30T22:00:00+00:00", "Delta School", "GC AD Builders", "Electrical", "SUBMITTED"),
        ("r5", "exists", "2026-09-30T22:00:00+00:00", "Epsilon", "GC AE Builders", "Electrical", "UNDECIDED"),
        ("r6", "created", None, "Zeta", "GC AF Builders", "Electrical", "UNDECIDED"),
        ("r7", "historical", "2022-11-04T22:00:00+00:00", "Old", "GC AG Builders", "Electrical", "WILL_SUBMIT"),
        ("r8", "expired", "2026-09-10T22:00:00+00:00", "Expired", "GC AH Builders", "Electrical", "UNDECIDED"),
        ("r9", "withdrawn", "2026-09-12T22:00:00+00:00", "Gone", "GC AI Builders", "Electrical", "UNDECIDED"),
        ("r10", "ignored", "2026-09-14T22:00:00+00:00", "Nope", "GC AJ Builders", "Electrical", "DECLINED"),
    ]
    for rid, status, close_at, title, gc, trade, state in rows:
        db.tables["rfp_portal_invitations"].append(_bc_row(
            id=rid, external_id=rid, bid_number=rid, bid_number_raw=rid, status=status, close_at=close_at, title=title,
            external_url=bc_facts.deep_link(rid), view_url=bc_facts.deep_link(rid),
            gc_external_name=gc, agency=gc, trade_name=trade, submission_state=state, gc_id=G1 if rid == "r1" else None,
            gc_kind="provisional" if rid == "r1" else None, match_project_id=P1 if rid == "r5" else None,
            created_project_id=P2 if rid == "r6" else None,
            change_log=[{"at": pi._iso(NOW - timedelta(days=1)), "field": "close_at", "old": "a", "new": "b", "run_id": "r"},
                        {"at": pi._iso(NOW - timedelta(days=30)), "field": "title", "old": "a", "new": "b", "run_id": "r"}] if rid == "r2" else [],
        ))
    db.tables["general_contractors"].append({"id": G1, "name": "Rafael Companies"})
    db.tables["projects"].extend([_project(P1, "Epsilon", bid_at=DUE_01), _project(P2, "Zeta", bid_at=DUE_01, number="26.9.7301")])


def test_list_invitations_uses_the_bc_lanes_nulls_last_the_state_filter_and_the_item_fields(db):
    _seed_lanes(db)
    out = pi.list_invitations(db, portal=BC, view="open", q=None, limit=50, offset=0)
    assert [i["id"] for i in out["items"]] == ["r4", "r2", "r3"]           # close_at desc, the null last
    assert out["total"] == 3 and out["counts"] == {
        "needs_action": 1, "open": 3, "existing": 2, "historical": 1, "expired_withdrawn": 2, "ignored": 1, "all": 10,
    }
    item = next(i for i in out["items"] if i["id"] == "r2")
    assert item["gc"] == {"external_id": GC_AA, "external_name": "GC AB Builders", "gc_id": None, "gc_name": None,
                          "kind": None, "confirmed_at": None, "candidates": []}
    assert item["trade_name"] == "Low Voltage" and item["submission_state"] == "WILL_SUBMIT" and item["changes_recent"] == 1
    assert item["changed_recently"] is True and item["external_url"] == bc_facts.deep_link("r2")
    for key in ("invited_at", "job_walk_at", "expected_start_at", "expected_finish_at", "workflow_bucket", "is_archived",
                "is_nda_required", "address", "ignore_source"):
        assert key in item
    assert "payload" not in item and "lead" not in item
    needs = pi.list_invitations(db, portal=BC, view="needs_action", q=None, limit=50, offset=0)["items"]
    assert [i["id"] for i in needs] == ["r1"] and needs[0]["gc"]["gc_name"] == "Rafael Companies"
    existing = pi.list_invitations(db, portal=BC, view="existing", q=None, limit=50, offset=0)["items"]
    assert [(i["id"], (i["match_project"] or {}).get("name"), (i["created_project"] or {}).get("name")) for i in existing] == [
        ("r5", "Epsilon", None), ("r6", None, "Zeta"),
    ]
    assert [i["id"] for i in pi.list_invitations(db, portal=BC, view="expired_withdrawn", q=None, limit=50, offset=0)["items"]] == ["r9", "r8"]
    assert [i["id"] for i in pi.list_invitations(db, portal=BC, view="historical", q=None, limit=50, offset=0)["items"]] == ["r7"]
    # The state filter and the search over the name, the GC and the trade.
    assert [i["id"] for i in pi.list_invitations(db, portal=BC, view="all", q=None, limit=50, offset=0, state="will_submit")["items"]] == ["r2", "r7"]
    assert [i["id"] for i in pi.list_invitations(db, portal=BC, view="all", q="low volt", limit=50, offset=0)["items"]] == ["r2"]
    assert [i["id"] for i in pi.list_invitations(db, portal=BC, view="all", q="GC AH", limit=50, offset=0)["items"]] == ["r8"]
    assert [i["id"] for i in pi.list_invitations(db, portal=BC, view="all", q="Gamma", limit=50, offset=0)["items"]] == ["r3"]
    assert pi.list_invitations(db, portal=BC, view="all", q=None, limit=50, offset=0, state="drop table")["total"] == 10
    # An unknown lane falls back to all; NGEM keeps its own lanes and item shape.
    assert pi.list_invitations(db, portal=BC, view="review", q=None, limit=50, offset=0)["total"] == 10
    assert set(pi.view_counts(db, "ngem")) == {"review", "new", "existing", "ignored", "all"}
    assert set(pi.view_counts(db, BC)) == set(pi.BC_VIEWS)


def test_portal_sources_for_projects_returns_exists_and_created_bc_rows_with_the_d32_fields(db):
    _seed_lanes(db)
    created = _inv(db, "r6")
    created["change_log"] = [{"at": f"t{i}", "field": "close_at", "old": str(i), "new": str(i + 1), "run_id": "r"} for i in range(12)]
    created["gc_id"] = G1
    db.tables["rfp_portal_invitations"].append({
        "id": "ngem-1", "portal": "ngem", "agency": "UNLV", "agency_key": "unlv", "bid_number": "1", "bid_number_raw": "1",
        "addendum_no": None, "title": "x", "close_at": DUE_01, "status": "exists", "match_project_id": P1,
        "resolution": "system", "resolved_at": "t", "change_log": [],
    })
    touched = []
    original_table = db.table
    db.table = lambda name: touched.append(name) or original_table(name)
    out = pi.portal_sources_for_projects(db, [P1, P2, "5a000000-0000-4000-8000-000000000009"])
    db.table = original_table
    assert set(out) == {P1, P2}
    bc_exists = next(s for s in out[P1] if s["portal"] == BC)
    assert bc_exists["invitation_id"] == "r5" and bc_exists["status"] == "exists" and bc_exists["gc_external_name"] == "GC AE Builders"
    assert bc_exists["external_url"] == bc_facts.deep_link("r5") and bc_exists["trade_name"] == "Electrical" and bc_exists["change_log"] == []
    assert bc_exists["invited_at"] == _inv(db, "r5")["invited_at"] and bc_exists["close_at"] == _inv(db, "r5")["close_at"]
    ngem = next(s for s in out[P1] if s["portal"] == "ngem")
    assert set(ngem) == {"portal", "invitation_id", "agency", "bid_number", "bid_number_raw", "addendum_no", "close_at",
                         "resolution", "resolved_at"}
    assert [s["invitation_id"] for s in out[P2]] == ["r6"]
    assert out[P2][0]["status"] == "created" and out[P2][0]["gc_id"] == G1
    assert [c["old"] for c in out[P2][0]["change_log"]] == [str(i) for i in range(2, 12)]     # the last ten
    # D32: gc_name is the directory GC the row's gc_id points at (one in_() read); None without a gc_id.
    assert out[P2][0]["gc_name"] == "Rafael Companies" and bc_exists["gc_name"] is None and "gc_name" not in ngem
    assert touched.count("general_contractors") == 1


def test_status_block_carries_the_connection_schedule_state_runs_and_counts(db):
    _seed_lanes(db)
    db.tables["profiles"].append({"id": U1, "full_name": "Ann Admin", "role": "it_admin", "is_active": True})
    db.tables["rfp_portal_state"].append({"portal": BC, "high_water_at": "hw", "last_full_sync_at": "f", "last_incremental_at": "i"})
    db.tables["rfp_portal_runs"].extend([_run(id="r-old", status="complete", created_at=pi._iso(NOW - timedelta(days=1))),
                                         _run(id="r-new", status="queued", trigger="manual", kind="full", scheduled_for=None,
                                              requested_by=U1, created_at=pi._iso(NOW))])
    db.tables["projects"].extend([
        {"id": "pf-1", "files_needed_source": BC, "files_needed_set_at": "t", "files_needed_cleared_at": None},
        {"id": "pf-2", "files_needed_source": BC, "files_needed_set_at": "t", "files_needed_cleared_at": "t2"},
        {"id": "pf-3", "files_needed_source": "other", "files_needed_set_at": "t", "files_needed_cleared_at": None},
    ])
    out = bcp.status(db, _settings(rfp_bc_poll_minutes=15, rfp_bc_full_sync_time="02:30"))
    assert out["connection"]["status"] == "connected" and out["connection"]["connected_by"] == {"id": U1, "name": "Ann Admin"}
    assert out["connection"]["external_user_name"] == "Fixture Admin" and "access_token" not in out["connection"]
    assert out["configured"] is True and out["enabled"] is True and out["active"] is True
    assert out["redirect_url"] == REDIRECT and out["poll_minutes"] == 15 and out["full_sync_time"] == "02:30"
    assert out["next_incremental_at"] == pi._iso(NOW + timedelta(minutes=15))
    assert out["next_full_sync_at"] == pi._iso(datetime(2026, 9, 27, 9, 30, tzinfo=timezone.utc))
    assert (out["high_water_at"], out["last_full_sync_at"], out["last_incremental_at"]) == ("hw", "f", "i")
    assert out["last_run"]["id"] == "r-new" and out["last_run"]["requested_by_name"] == "Ann Admin" and out["active_run"]["kind"] == "full"
    assert out["counts"] == {"needs_action": 1, "open": 3, "existing": 2, "historical": 1, "expired_withdrawn": 2,
                             "ignored": 1, "all": 10, "files_needed_projects": 1}
    text = str(out)
    assert ACCESS not in text and "refresh-never-logged" not in text and "s3cr3t" not in text
    off = bcp.status(db, _settings(rfp_bc_enabled=False))
    assert off["active"] is False and off["enabled"] is False and off["next_incremental_at"] is None


def test_status_says_when_the_test_bench_pauses_the_scheduled_scans(db, monkeypatch):
    """poll_once returns before claiming any slot while an RFP test session
    is active, so the status block says the scheduled scans are paused. It
    is the same rfp_test.active_session call the tick makes: no read of the
    sessions table at all while the bench switch is off."""
    monkeypatch.setattr(rfp_test, "active_session", _REAL_ACTIVE_SESSION)
    db.tables["rfp_test_sessions"] = [{"id": "bench-1", "status": rfp_test.STATUS_ACTIVE}]
    monkeypatch.setattr(rfp_test, "get_settings", lambda: SimpleNamespace(rfp_testing_enabled=False))
    counting = CountingDB(db)
    out = bcp.status(counting, _settings())
    assert out["paused_by_test_session"] is False and counting.count("rfp_test_sessions") == 0
    # The switch on and a session active: paused, and poll_once agrees (no slot claimed).
    monkeypatch.setattr(rfp_test, "get_settings", lambda: SimpleNamespace(rfp_testing_enabled=True))
    counting = CountingDB(db)
    assert bcp.status(counting, _settings())["paused_by_test_session"] is True
    assert counting.count("rfp_test_sessions") == 1
    pi.poll_once()
    assert db.tables["rfp_portal_runs"] == []
    # The session ended: not paused, and the tick claims the due slot again.
    db.tables["rfp_test_sessions"][0]["status"] = rfp_test.STATUS_ENDED
    assert bcp.status(db, _settings())["paused_by_test_session"] is False
    pi.poll_once()
    assert [r["portal"] for r in db.tables["rfp_portal_runs"]] == [BC]


class _SelectSpyDB(CountingDB):
    """CountingDB that also records each select's column list and kwargs."""

    def __init__(self, base: BcDB) -> None:
        super().__init__(base)
        self.selects: list[tuple[str, tuple, dict]] = []

    def table(self, name):
        query = super().table(name)
        original = query.select

        def select(*a, **k):
            self.selects.append((name, a, k))
            return original(*a, **k)

        query.select = select
        return query


def test_view_counts_read_the_status_column_in_pages_not_one_head_count_per_lane(db):
    """GET /invitations recomputed the lane counts with one exact-count HEAD
    request per lane (7 on this tab, 1.6 to 3.3 s on dev at 2,543 rows).
    Now one paged read of the portal's status column (1,000 a page): 2,500
    rows cost 3 requests, only `status` is transferred, the counts equal the
    per-lane sets and another portal's rows never leak in. NGEM counts with
    the same function."""
    statuses = list(pi.ALL_STATUSES)
    rows = db.tables["rfp_portal_invitations"]
    rows.extend({"id": f"bc-{i:05d}", "portal": BC, "status": statuses[(i * 7) % len(statuses)]} for i in range(2500))
    ngem_statuses = ["review_match", "match", "done", "exists", "ignored", "created"]
    rows.extend({"id": f"ngem-{i:03d}", "portal": "ngem", "status": ngem_statuses[i % len(ngem_statuses)]} for i in range(60))

    def expected(portal):
        return {
            name: sum(1 for r in rows if r["portal"] == portal and r["status"] in members)
            for name, members in pi.VIEWS_BY_PORTAL[portal].items()
        }

    spy = _SelectSpyDB(db)
    counts = pi.view_counts(spy, BC)
    assert spy.requests == [("rfp_portal_invitations", "select")] * 3          # was 7 HEAD counts
    assert all(a == ("status",) and not k.get("head") for _t, a, k in spy.selects)
    assert counts == expected(BC) and set(counts) == set(pi.BC_VIEWS) and counts["all"] == 2500
    spy = _SelectSpyDB(db)
    assert pi.view_counts(spy, "ngem") == expected("ngem") and len(spy.requests) == 1
    assert pi.view_counts(db, "ngem")["all"] == 60
    # An empty portal is one request and every lane at zero.
    spy = _SelectSpyDB(db)
    assert pi.view_counts(spy, "other") == {n: 0 for n in pi.VIEWS} and len(spy.requests) == 1


def test_list_invitations_reads_the_lane_counts_once_per_call(db, monkeypatch):
    """The tab polls the list every 30 s and the counts ride in the list
    response: one view_counts per call, one status page for a small portal."""
    _seed_lanes(db)
    calls = []
    real = pi.view_counts
    monkeypatch.setattr(pi, "view_counts", lambda sb, portal: calls.append(portal) or real(sb, portal))
    spy = _SelectSpyDB(db)
    out = pi.list_invitations(spy, portal=BC, view="open", q=None, limit=50, offset=0)
    assert calls == [BC] and out["counts"]["all"] == 10 and out["counts"]["open"] == 3
    reads = [a for t, a, _k in spy.selects if t == "rfp_portal_invitations"]
    assert len(reads) == 2 and reads[1] == ("status",)                       # the page, then the counts
    assert spy.count("rfp_portal_invitations") == 2


def test_detail_carries_the_gc_block_lead_payload_and_no_harvest_for_a_bc_row(db):
    db.tables["general_contractors"].append({"id": G1, "name": "Rafael Companies"})
    db.tables["profiles"].append({"id": U1, "full_name": "Ann Admin", "role": "it_admin", "is_active": True})
    db.tables["rfp_portal_invitations"].append(_bc_row(status="done", gc_id=G1, gc_kind="alias", gc_confirmed_at="t", gc_confirmed_by=U1,
                                                        gc_candidates=[{"gc_id": G1, "name": "Rafael Companies", "score": 0.9}, "junk"],
                                                        project_gc_id="pg-1", sibling_of="inv-0"))
    out = pi.detail(db, INV)
    assert out["gc"] == {"external_id": GC_AA, "external_name": "GC AA Builders", "gc_id": G1, "gc_name": "Rafael Companies",
                         "kind": "alias", "confirmed_at": "t", "candidates": [{"gc_id": G1, "name": "Rafael Companies", "score": 0.9}]}
    assert out["gc_confirmed_by_name"] == "Ann Admin"
    # The lead in the contract's shape (name, email, phone), the way the tab and the GC card read it.
    stored = _inv(db)["lead"]
    assert out["lead"] == {"name": "Lead Person00", "email": "lead00@gcaabuilders.example.com", "phone": stored["phone"]}
    assert "first_name" not in out["lead"] and stored["first_name"] == "Lead"
    assert bc_facts.lead_view({"first_name": None, "last_name": None, "email": "x@y.z", "phone": None}) == {"name": None, "email": "x@y.z", "phone": None}
    assert bc_facts.lead_view(None) is None
    assert out["payload"]["id"] == out["external_id"] and out["project_gc_id"] == "pg-1" and out["sibling_of"] == "inv-0"
    assert out["harvest_available"] == {"ok": False, "reason": "BuildingConnected has no files to harvest."}
    assert out["create_available"] is True and out["changes_recent"] == 0 and "view_url" not in out
    assert out["external_url"] == bc_facts.deep_link(out["external_id"])


def test_harvest_again_refuses_a_bc_row_and_queues_nothing(db):
    """A BuildingConnected row at `done` (automatic creation off) has no
    documents to harvest and its client has no fetch_event: harvest_again
    answers rfp_portal_not_actionable and no harvest job or rfp_harvests
    row is written."""
    db.tables["rfp_portal_invitations"].append(_bc_row(status="done"))
    with pytest.raises(pi.RfpPortalError) as exc:
        pi.harvest_again(db, INV, U1, force=True)
    assert exc.value.code == pi.CODE_NOT_ACTIONABLE
    assert str(exc.value) == "BuildingConnected invitations carry no documents to harvest."
    assert db.tables["llm_jobs"] == [] and db.tables["rfp_harvests"] == []
    assert not any(a["action"] == "rfp_portal.harvest_again" for a in db.tables["audit_log"])
    assert pi.portal_has_harvest(BC) is False and pi.portal_has_harvest("ngem") is True
    # The status rule still comes first: a row at match answers not_available, as before.
    _inv(db)["status"] = "match"
    with pytest.raises(pi.RfpPortalError) as exc:
        pi.harvest_again(db, INV, U1)
    assert exc.value.code == pi.CODE_NOT_AVAILABLE


def test_ngem_rows_keep_their_item_and_detail_shape(db):
    db.tables["rfp_portal_invitations"].append({
        "id": "ngem-1", "portal": "ngem", "agency": "UNLV", "agency_key": "unlv", "bid_number": "1", "bid_number_raw": "1",
        "addendum_no": None, "title": "x", "close_at": DUE_01, "issued_on": None, "status": "done", "flag_reason": None,
        "response_status": None, "match_project_id": None, "match_score": None, "harvest_id": None, "last_seen_at": None,
        "missing_since": None, "change_log": [], "created_project_id": None, "excluded_project_ids": [],
    })
    item = pi.list_invitations(db, portal="ngem", view="new", q=None, limit=10, offset=0)["items"][0]
    assert "gc" not in item and "external_url" not in item and "changes_recent" not in item
    out = pi.detail(db, "ngem-1")
    assert "gc" not in out and out["harvest_available"]["ok"] is True
