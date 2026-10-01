"""The NGEM portal service (app/services/rfp_portal_ingest) against the
in-memory fake Supabase from tests/test_rfp_email_ingest, with the portal
client stubbed through the `PORTALS` registry (a scripted session and an
adapter that classifies scripted exceptions) and recording fakes for the
sandbox calls (docs/RFP_NGEM_PORTAL.md sections 2, 4 and 9).

Pinned, in the doc's order:

- slot computation across a DST change and the catch-up window, the next
  run, the run ledger claim (23505 = someone else's, or an active run);
- the scan: new rows inserted at match, a known row refreshed unchanged, a
  changed title / close date / addendum / raw number logged and belled
  (one bell per invitation while an unread one exists, an ignored row's
  silence), past-close rows skipped, missing_since set and cleared, the
  change_log cap, the new-invitations bell and its dedupe, the run counts,
  every failure mapping of the scan job and the queue marks;
- the match step: exists / review_match / harvest routing, the auto-resolve
  switch off, the model down waiting without an attempt, the ladder to
  match_failed, unusable output twice, excluded ids, the rebid hint, the
  no-name title;
- the harvest step (park while locked, enqueue once) and the harvest job
  (facts before files, the caps, every per-file outcome, the run created
  and started, or deleted when empty, every failure mapping, the losing
  claim, the queue mark fencing, the stale link refresh);
- every human action's status guard and writes, run_now;
- portal_sources_for_projects, the detail, the list and the status block.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.roles import Role
from app.services import llm, llm_health, llm_queue, rfp_email_ingest, rfp_ingest
from app.services import ngem_client as ngc
from app.services import rfp_portal_ingest as m
from tests.test_rfp_email_ingest import FakeDB, _Query, _apply, _Snapshot

NOW = datetime(2026, 9, 16, 15, 0, 0, tzinfo=timezone.utc)   # 08:00 Pacific
PDF = b"%PDF-1.7\n" + b"y" * 200 + b"\n%%EOF\n"
NOT_PDF = b"MZ" + b"\x00" * 100
ENTRY = "https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=entry"
VIEW = "https://supplier.ionwave.net/VendorResponse/Bid/VResponseEvent.aspx?e=v1"
ATT = "https://supplier.ionwave.net/VendorResponse/Bid/VResponseBidAttachments.aspx?e=a1"
DL1 = "https://supplier.ionwave.net/Extract.aspx?e=d1"
DL2 = "https://supplier.ionwave.net/Extract.aspx?e=d2"
INV = "inv-1"
P1 = "5a000000-0000-4000-8000-000000000001"
P2 = "5a000000-0000-4000-8000-000000000002"
P3 = "5a000000-0000-4000-8000-000000000003"
U1 = "u-1"


def _review_users():
    """One active user per review role (docs/RFP_NGEM_PORTAL.md 1, "Who sees
    it") plus one inactive Estimating Admin and one accountant; the bells
    fan out one row per active user holding the role."""
    users = [
        {"id": f"u-{role.value}", "full_name": role.value.replace("_", " ").title(),
         "role": role.value, "is_active": True}
        for role in m.RFP_REVIEW_ROLES
    ]
    users.append({"id": "u-gone", "full_name": "Gone Admin", "role": Role.ESTIMATING_ADMIN.value,
                  "is_active": False})
    users.append({"id": "u-acct", "full_name": "Ann Accountant", "role": Role.ACCOUNTANT.value,
                  "is_active": True})
    return users


ADMIN_USER = f"u-{Role.ESTIMATING_ADMIN.value}"


# ── Fake Supabase additions ──────────────────────────────────────────────


class PortalQuery(_Query):
    """The ingest fake plus a JSON path filter (metadata->>invitation_id),
    an ilike or-group and a count that ignores the range."""

    def select(self, *a, **k):
        # PostgREST honours the column list; so does this fake, so a select
        # that names its columns can never leak one it did not ask for.
        self._columns = None
        if a and isinstance(a[0], str) and a[0].strip() != "*" and "(" not in a[0] and "->" not in a[0]:
            self._columns = [c.strip() for c in a[0].split(",") if c.strip()]
        return super().select(*a, **k)

    def _filter(self, op, col, val):
        if "->>" in col:
            base, key = col.split("->>", 1)
            return self._add(lambda row: _apply(op, ((row.get(base) or {}).get(key)), val))
        return super()._filter(op, col, val)

    def or_(self, expr):
        if ".ilike." in expr:
            terms = [t.strip() for t in expr.split(",")]

            def pred(row):
                for term in terms:
                    col, _op, pattern = term.split(".", 2)
                    if pattern.strip("*").lower() in str(row.get(col) or "").lower():
                        return True
                return False

            return self._add(pred)
        return super().or_(expr)

    def execute(self):
        if self._op == "select":
            rows = self.db.tables.setdefault(self.table, [])
            hits = self._sorted([copy.deepcopy(r) for r in rows if self._matches(r)])
            total = len(hits)
            if self._range is not None:
                hits = hits[self._range[0]: self._range[1] + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            if getattr(self, "_columns", None):
                hits = [{c: r.get(c) for c in self._columns} for r in hits]
            return SimpleNamespace(data=hits, count=total)
        return super().execute()


class PortalDB(FakeDB):
    unique = {
        **FakeDB.unique,
        "rfp_portal_invitations": [("portal", "agency_key", "bid_number")],
        "rfp_harvests": [("method", "external_key")],
    }

    def table(self, name):
        query = PortalQuery(self, name)
        inherited = query._check_unique
        if name == "rfp_portal_runs":

            def check_runs(rows, payload):
                inherited(rows, payload)
                for r in rows:
                    if r.get("portal") != payload.get("portal"):
                        continue
                    if payload.get("scheduled_for") and r.get("scheduled_for") == payload.get("scheduled_for"):
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


# ── Fake portal, session and sandbox ─────────────────────────────────────


class PortalExc(Exception):
    """A scripted client exception: `kind` is what the adapter answers."""

    def __init__(self, message, kind, *, locked_until=None, stale=False):
        super().__init__(message)
        self.kind = kind
        self.locked_until = locked_until
        self.stale = stale


def _grid_row(**over):
    row = dict(
        agency="UNLV (NSHE-Business Center South)", bid_number_raw="5584-GS Addendum 1",
        bid_number="5584-GS", addendum_no=1, title="Emergency Phone Tower Installations and Upgrades",
        issued_on=datetime(2026, 8, 25).date(), close_at=NOW + timedelta(days=1), time_left="1 day",
        bid_status="Open", response_status="Viewed", response_code="V", status_code="O", view_url=VIEW,
    )
    row.update(over)
    return SimpleNamespace(**row)


def _page(rows, *, total_items=None, pages=1):
    return SimpleNamespace(rows=list(rows), total_items=len(rows) if total_items is None else total_items,
                           pages_fetched=pages, total_pages=pages)


def _facts(**over):
    row = dict(
        bid_number_raw="5584-GS Addendum 1", bid_number="5584-GS", addendum_no=1,
        title="Emergency Phone Tower Installations and Upgrades", bid_type="IFB", status="Open",
        issued_at=NOW - timedelta(days=20), close_at=NOW + timedelta(days=1), question_cutoff_at=NOW,
        notes_html="<p>Pre-bid <b>walk</b> at <script>x()</script><a href='https://unlv.edu'>UNLV</a></p>",
        notes_text="Pre-bid walk at UNLV",
        contact={"workgroup": "Purchasing", "name": "Pat Buyer", "address": "4505 S Maryland Pkwy",
                 "phone": "702-555-0100", "email": "pat@unlv.edu"},
        attachments_url=ATT, event_url=VIEW,
    )
    row.update(over)
    return SimpleNamespace(**row)


def _attachment(index, name, size, url, description=None):
    return SimpleNamespace(index=index, file_name=name, size_bytes=size, description=description, download_url=url)


class FakeSession:
    def __init__(self):
        self.calls = []
        self.page = _page([_grid_row()])
        self.pages = None          # a list consumed one fetch_invitations at a time
        self.facts = _facts()
        self.attachments = [_attachment(1, "Plans.pdf", 300, DL1, "Drawings"), _attachment(2, "Spec.pdf", 200, DL2)]
        self.errors = {}
        self.bytes_for = {}
        self.downloads = 0
        self.closed = False

    def _raise(self, key):
        err = self.errors.get(key)
        if isinstance(err, list):
            if err:
                raise err.pop(0)
        elif err is not None:
            raise err

    def fetch_invitations(self, max_pages):
        self.calls.append(("invitations", max_pages))
        self._raise("invitations")
        if self.pages:
            return self.pages.pop(0)
        return self.page

    def fetch_event(self, view_url):
        self.calls.append(("event", view_url))
        self._raise(("event", view_url))
        self._raise("event")
        return self.facts

    def fetch_attachments(self, facts, max_pages):
        self.calls.append(("attachments", facts.attachments_url, max_pages))
        self._raise("attachments")
        return list(self.attachments)

    def download(self, url, dest, max_bytes, *, referer=None):
        self.calls.append(("download", url, max_bytes, referer))
        self.downloads += 1
        self._raise(url)
        data = self.bytes_for.get(url, PDF)
        if len(data) > max_bytes:
            raise PortalExc("The bid attachment is larger than the harvest accepts.", "permanent")
        dest.write_bytes(data)
        return SimpleNamespace(bytes_written=len(data), filename=f"{url.rsplit('=', 1)[-1]}.pdf")

    def close(self):
        self.closed = True


class FakePortal:
    key = "ngem"

    def __init__(self):
        self.session = FakeSession()
        self.available = (True, None, None)
        self.opened = 0
        self.is_configured = True

    def configured(self, settings):
        return self.is_configured

    def account(self, settings):
        return "acct"

    def entry_url(self, settings):
        return ENTRY

    def open_session(self, settings):
        self.opened += 1
        return self.session

    def availability(self, settings):
        return self.available

    def classify(self, exc):
        return getattr(exc, "kind", m.KIND_UNKNOWN)

    def is_stale_link(self, exc):
        return bool(getattr(exc, "stale", False))

    def parse_bid_number(self, raw):
        return ngc.parse_bid_number(raw)


class FakeSandbox:
    def __init__(self, db):
        self.db = db
        self.calls = []
        self.outcomes = {}
        self._n = 0

    def create_portal_run(self, *, portal_invitation_id, harvest_id):
        self._n += 1
        row = {"id": f"run-{self._n}", "status": "staging", "source_kind": "rfp_portal",
               "portal_invitation_id": portal_invitation_id, "harvest_id": harvest_id, "file_count": 0}
        self.db.tables.setdefault("rfp_ingest_runs", []).append(row)
        self.calls.append(("create", portal_invitation_id, harvest_id))
        return dict(row)

    def add_upload_file(self, run_id, *, filename, declared_mime, data, source):
        self.calls.append(("add", run_id, filename, declared_mime, len(data), source))
        outcome = self.outcomes.get(filename)
        if outcome == 413:
            raise rfp_ingest.RfpIngestPermanent("The run already holds 200 files, the per-run limit.", http_status=413)
        if outcome == 409:
            raise rfp_ingest.RfpIngestPermanent("Files can only be added while the run is staging.", http_status=409)
        if outcome == "duplicate":
            raise rfp_ingest.RfpIngestDuplicate("f-existing")
        self._n += 1
        fid = f"f-{self._n}"
        if outcome == "failed":
            return {"id": fid, "status": "failed", "error": "A storage operation failed.", "size_bytes": len(data)}
        if outcome == "rejected" or not data.startswith(b"%PDF"):
            return {"id": fid, "status": "rejected", "error": "The file is not a PDF.", "size_bytes": len(data)}
        return {"id": fid, "status": "pending", "size_bytes": len(data)}

    def start_run(self, run_id):
        self.calls.append(("start", run_id))
        for row in self.db.tables.get("rfp_ingest_runs", []):
            if row["id"] == run_id:
                row["status"] = "pending"
                return dict(row)
        raise rfp_ingest.RfpIngestPermanent("Run not found.", http_status=404)

    def dispatch(self, run_id, *, created_by, background, raise_on_active=False):
        self.calls.append(("dispatch", run_id, created_by, background))
        return {"id": f"job-{run_id}"}

    def delete_run(self, run_id):
        self.calls.append(("delete", run_id))
        self.db.tables["rfp_ingest_runs"] = [r for r in self.db.tables.get("rfp_ingest_runs", []) if r["id"] != run_id]

    def names(self):
        return [c[0] for c in self.calls]


# ── Fixtures ─────────────────────────────────────────────────────────────


def _settings(tmp_path=None, **over):
    base = dict(
        rfp_ingest_enabled=True, rfp_ngem_enabled=True, ngem_login_username="acct",
        ngem_login_password="pw", ngem_entry_url=ENTRY, llm_queue_enabled=True,
        rfp_match_auto_merge_enabled=False,
    )
    if tmp_path is not None:
        base["rfp_ingest_scratch_dir"] = str(tmp_path)
    base.update(over)
    return Settings(_env_file=None, **base)


def _run(**over):
    row = {"id": "run-a", "portal": "ngem", "trigger": "scheduled", "scheduled_for": m._iso(NOW - timedelta(hours=1)),
           "requested_by": None, "status": "queued", "claimed_by": None, "started_at": None, "finished_at": None,
           "pages_scanned": 0, "invitations_seen": 0, "invitations_new": 0, "invitations_changed": 0,
           "invitations_skipped_closed": 0, "last_error": None, "next_attempt_at": None,
           "created_at": m._iso(NOW - timedelta(hours=1))}
    row.update(over)
    return row


def _invitation(**over):
    row = {
        "id": INV, "portal": "ngem", "agency": "UNLV (NSHE-Business Center South)",
        "agency_key": "unlv-nshe-business-center-south", "bid_number": "5584-GS",
        "bid_number_raw": "5584-GS Addendum 1", "addendum_no": 1,
        "title": "Emergency Phone Tower Installations and Upgrades", "issued_on": "2026-08-25",
        "close_at": m._iso(NOW + timedelta(days=1)), "time_left": "1 day", "bid_status": "Open",
        "response_status": "Viewed", "response_code": "V", "status_code": "O", "view_url": VIEW,
        "status": "match", "flag_reason": None, "decided_at_step": None, "attempts": 0,
        "next_attempt_at": None, "last_error": None, "match_candidates": None, "match_project_id": None,
        "match_score": None, "match_llm_model": None, "match_llm_prompt_version": None,
        "match_weights": None, "matched_at": None, "possible_rebid_project_id": None,
        "possible_rebid_score": None, "excluded_project_ids": [], "resolution": None,
        "resolved_by": None, "resolved_at": None, "reopen_reason": None, "ignored_by": None,
        "ignored_at": None, "ignore_reason": None, "harvest_id": None, "harvested_at": None,
        "change_log": [], "first_seen_at": m._iso(NOW - timedelta(days=2)),
        "last_seen_at": m._iso(NOW - timedelta(days=1)), "first_seen_run_id": "run-0",
        "last_seen_run_id": "run-0", "seen_count": 1, "missing_since": None,
        "created_at": m._iso(NOW - timedelta(days=2)), "updated_at": m._iso(NOW - timedelta(days=1)),
    }
    row.update(over)
    return row


def _project(pid, name, *, days=5, number="26.9.7200", stage="rfqs", notes=None, actual=None):
    return {"id": pid, "name": name, "number": number, "current_stage": stage,
            "internal_bid_at": m._iso(NOW + timedelta(days=days)), "actual_bid_at": actual,
            "bid_notes": notes, "project_gcs": [], "abandoned_at": None}


def _harvest_row(**over):
    row = {"id": "hv-1", "rfp_email_id": None, "portal_invitation_id": INV, "method": "ngem",
           "external_key": "ngem:unlv-nshe-business-center-south:5584-GS", "external_url": ENTRY,
           "status": "complete", "claim_token": None, "attempts": 1, "last_error": None,
           "data": {"platform": "ngem"}, "files": [], "file_count": 0, "files_accepted": 0,
           "bytes_downloaded": 0, "sandbox_run_id": None, "facts_at": m._iso(NOW - timedelta(days=1)),
           "started_at": m._iso(NOW - timedelta(days=1)), "finished_at": m._iso(NOW - timedelta(days=1))}
    row.update(over)
    return row


@pytest.fixture
def db():
    fake = PortalDB({
        "rfp_portal_runs": [], "rfp_portal_invitations": [], "rfp_harvests": [],
        "rfp_harvest_sessions": [], "rfp_ingest_runs": [], "llm_jobs": [], "notifications": [],
        "projects": [], "general_contractors": [], "gc_contacts": [], "profiles": _review_users(),
        "graph_sync_state": [], "audit_log": [],
    })
    fake.defaults = {
        **FakeDB.defaults,
        "llm_jobs": {"status": lambda: "queued", "attempts": lambda: 0, "created_at": lambda: NOW.isoformat()},
        "rfp_portal_runs": {"status": lambda: "queued", "created_at": lambda: NOW.isoformat(),
                            "pages_scanned": lambda: 0},
        "rfp_portal_invitations": {"attempts": lambda: 0, "excluded_project_ids": lambda: [],
                                   "change_log": lambda: [], "created_at": lambda: NOW.isoformat()},
        "rfp_harvests": {"attempts": lambda: 0, "files": lambda: []},
    }
    return fake


@pytest.fixture
def settings(tmp_path):
    return _settings(tmp_path)


@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
    monkeypatch.setitem(m.PORTALS, "ngem", lambda: fake)
    return fake


@pytest.fixture
def sandbox(db, monkeypatch):
    fake = FakeSandbox(db)
    for name in ("create_portal_run", "add_upload_file", "start_run", "dispatch", "delete_run"):
        monkeypatch.setattr(m.rfp_ingest, name, getattr(fake, name))
    return fake


@pytest.fixture(autouse=True)
def _env(monkeypatch, db, settings, portal):
    monkeypatch.setattr(m, "get_settings", lambda: settings)
    monkeypatch.setattr(m, "get_supabase", lambda: db)
    monkeypatch.setattr(m, "_now", lambda: NOW)
    monkeypatch.setattr(ngc, "_now", lambda: NOW)
    monkeypatch.setattr(m, "time", SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic))
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: settings)
    monkeypatch.setattr(rfp_email_ingest, "get_settings", lambda: settings)
    monkeypatch.setattr(rfp_email_ingest, "get_supabase", lambda: db)
    # The per-type bells (notify_role: one row per user of the role, recorded
    # here as one row carrying the role) and the per-user bells (notify_user:
    # one row per user, the role looked up from the seeded profiles).
    monkeypatch.setattr(
        m, "notify_role",
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
    monkeypatch.setattr(m, "notify_user", fake_notify_user)
    monkeypatch.setattr(
        m, "audit",
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


def _inv(db, inv_id=INV):
    return next(r for r in db.tables["rfp_portal_invitations"] if r["id"] == inv_id)


def _jobs(db, job_type=m.JOB_HARVEST):
    return [j for j in db.tables["llm_jobs"] if j["job_type"] == job_type]


def _bells(db, type_):
    return [n for n in db.tables["notifications"] if n["type"] == type_]


# ── Scheduler: slots, catch-up, the ledger claim ─────────────────────────


def test_schedule_slots_are_pacific_wall_clock_across_the_dst_change():
    s = _settings()
    # 2026-03-08 is the spring-forward day in America/Los_Angeles.
    before = datetime(2026, 3, 7, 20, 0, tzinfo=timezone.utc)   # 12:00 PST
    after = datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)    # 13:00 PDT
    slots_before = [d for d in m.schedule_slots(before, s) if d.date() == before.date()]
    slots_after = [d for d in m.schedule_slots(after, s) if d.date() == after.date()]
    assert [d.strftime("%H:%M") for d in slots_before] == ["14:30", "20:00"]   # PST = UTC-8
    assert [d.strftime("%H:%M") for d in slots_after] == ["13:30", "19:00"]    # PDT = UTC-7
    # Yesterday's slots ride along so a late slot's window survives midnight.
    assert len(m.schedule_slots(after, s)) == 4


def test_due_slots_honour_the_catch_up_window():
    s = _settings(rfp_ngem_catchup_hours=4)
    slot = datetime(2026, 9, 16, 13, 30, tzinfo=timezone.utc)   # 06:30 PDT
    assert m.due_slots(slot - timedelta(seconds=1), s) == []
    assert m.due_slots(slot, s) == [slot]
    assert m.due_slots(slot + timedelta(hours=3, minutes=59), s) == [slot]
    assert m.due_slots(slot + timedelta(hours=4), s) == []
    noon = datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc)
    assert m.due_slots(noon + timedelta(minutes=5), s) == [noon]
    # A late slot's window crosses midnight Pacific.
    late = _settings(rfp_ngem_schedule_times="22:00", rfp_ngem_catchup_hours=4)
    after_midnight = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)   # 01:00 PDT on the 17th
    assert m.due_slots(after_midnight, late) == [datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)]


def test_next_run_at_is_the_first_slot_after_now():
    s = _settings()
    at_8 = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
    assert m.next_run_at(at_8, s) == datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc)
    at_20 = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    assert m.next_run_at(at_20, s) == datetime(2026, 9, 17, 13, 30, tzinfo=timezone.utc)


def test_claim_slot_is_idempotent_and_yields_to_an_active_run(db):
    slot = datetime(2026, 9, 16, 13, 30, tzinfo=timezone.utc)
    first = m.claim_slot(db, "ngem", slot)
    assert first and first["trigger"] == "scheduled" and first["status"] == "queued"
    assert m.claim_slot(db, "ngem", slot) is None                       # someone else's
    assert m.claim_slot(db, "ngem", slot + timedelta(hours=6)) is None  # a run is active
    db.tables["rfp_portal_runs"][0]["status"] = "complete"
    assert m.claim_slot(db, "ngem", slot + timedelta(hours=6)) is not None
    with pytest.raises(Exception, match="boom"):
        bad = PortalDB({"rfp_portal_runs": []})

        class Q(PortalQuery):
            def execute(self):
                raise Exception("boom")

        bad.table = lambda name: Q(bad, name)
        m.claim_slot(bad, "ngem", slot)


def test_poll_once_claims_the_due_slot_and_queues_the_scan(db, settings, monkeypatch):
    monkeypatch.setattr(m, "sweep", lambda sb, lease_key=None: db.tables.setdefault("swept", []).append(lease_key))
    m.poll_once()
    runs = db.tables["rfp_portal_runs"]
    assert len(runs) == 1 and runs[0]["scheduled_for"] == m._iso(datetime(2026, 9, 16, 13, 30, tzinfo=timezone.utc))
    jobs = _jobs(db, m.JOB_SCAN)
    # The scan rides at its own priority, ahead of the harvests (150).
    assert len(jobs) == 1 and jobs[0]["target_id"] == runs[0]["id"] and jobs[0]["priority"] == 140
    assert jobs[0]["priority"] == settings.rfp_ngem_scan_queue_priority < settings.rfp_ngem_queue_priority
    assert jobs[0]["payload"] == {"run_id": runs[0]["id"]}
    assert db.tables["swept"] == [m.LEASE_KEY]
    # A second tick: the slot is claimed, nothing new.
    m.poll_once()
    assert len(db.tables["rfp_portal_runs"]) == 1 and len(_jobs(db, m.JOB_SCAN)) == 1


def test_scan_priority_setting_is_its_own(monkeypatch):
    s = _settings(rfp_ngem_scan_queue_priority=120)
    monkeypatch.setattr(llm_queue, "enqueue", lambda job_type, **k: {"id": "j", **k})
    assert m.enqueue_scan("run-x", created_by=None, settings=s)["priority"] == 120
    assert m.enqueue_harvest("inv-x", created_by=None, settings=s)["priority"] == 150


def test_poll_once_does_nothing_while_the_slice_is_inactive(db, settings, monkeypatch):
    for over in ({"rfp_ngem_enabled": False}, {"rfp_ingest_enabled": False},
                 {"ngem_login_password": ""}, {"llm_queue_enabled": False}):
        monkeypatch.setattr(m, "get_settings", lambda o=over: _settings(**o))
        m.poll_once()
    assert db.tables["rfp_portal_runs"] == [] and db.tables["llm_jobs"] == []


def test_poll_once_parks_the_run_when_the_scan_cannot_be_queued_and_retries_it(db, settings, monkeypatch):
    """One transient PostgREST error must not cost the day's slot: the run
    keeps its slot, waits a poll interval, and the next tick queues it."""
    monkeypatch.setattr(m, "sweep", lambda sb, lease_key=None: None)
    real_enqueue = llm_queue.enqueue

    def boom(*a, **k):
        raise RuntimeError("queue is down")
    monkeypatch.setattr(llm_queue, "enqueue", boom)
    m.poll_once()
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "queued" and "queue is down" in run["last_error"]
    assert run["next_attempt_at"] == m._iso(NOW + timedelta(seconds=settings.rfp_ngem_poll_seconds))
    assert _jobs(db, m.JOB_SCAN) == [] and _bells(db, m.NOTIFY_SCAN_FAILED) == []
    # Still waiting: nothing happens; the wait passed: the scan is queued.
    m.poll_once()
    assert _jobs(db, m.JOB_SCAN) == []
    monkeypatch.setattr(llm_queue, "enqueue", real_enqueue)
    run["next_attempt_at"] = m._iso(NOW - timedelta(seconds=1))
    m.poll_once()
    assert [j["target_id"] for j in _jobs(db, m.JOB_SCAN)] == [run["id"]]
    assert len(db.tables["rfp_portal_runs"]) == 1


def test_poll_once_dispatches_a_queued_run_that_has_no_job_and_no_wait(db, monkeypatch):
    """A crash between the claim and the enqueue (or an insert that answered
    no row) leaves a queued run with next_attempt_at null; it held the active
    index, and Run now with it, until the stale cap. Now it gets its job on
    the next tick."""
    monkeypatch.setattr(m, "sweep", lambda sb, lease_key=None: None)
    stranded = _run(id="run-s", scheduled_for=m._iso(NOW - timedelta(minutes=5)), status="queued",
                    next_attempt_at=None, created_at=m._iso(NOW - timedelta(minutes=5)))
    db.tables["rfp_portal_runs"] = [stranded]
    m.poll_once()
    assert [j["target_id"] for j in _jobs(db, m.JOB_SCAN)] == ["run-s"]
    assert db.tables["rfp_portal_runs"][0]["status"] == "queued"
    # A queued run whose wait is still ahead is left alone until then.
    later = _run(id="run-w", scheduled_for=None, trigger="manual", status="queued",
                 next_attempt_at=m._iso(NOW + timedelta(hours=2)))
    db.tables["rfp_portal_runs"] = [later]
    db.tables["llm_jobs"] = []
    m.poll_once()
    assert _jobs(db, m.JOB_SCAN) == []


def test_poll_once_requeues_a_parked_run_and_fails_a_lost_one(db, monkeypatch):
    monkeypatch.setattr(m, "sweep", lambda sb, lease_key=None: None)
    parked = _run(id="run-p", scheduled_for=m._iso(NOW - timedelta(days=1)), status="queued",
                  next_attempt_at=m._iso(NOW - timedelta(seconds=1)), last_error="locked")
    lost = _run(id="run-l", scheduled_for=m._iso(NOW - timedelta(days=2)), status="running",
                started_at=m._iso(NOW - timedelta(hours=2)))
    db.tables["rfp_portal_runs"] = [parked, lost]
    m.poll_once()
    assert [j["target_id"] for j in _jobs(db, m.JOB_SCAN)] == ["run-p"]
    by_id = {r["id"]: r for r in db.tables["rfp_portal_runs"]}
    assert by_id["run-l"]["status"] == "failed" and by_id["run-l"]["last_error"] == m._MSG_RUN_LOST
    assert by_id["run-p"]["status"] == "queued"
    # A running run whose job is still active is left alone.
    db.tables["rfp_portal_runs"] = [_run(id="run-x", scheduled_for=None, trigger="manual", status="running",
                                         started_at=m._iso(NOW - timedelta(hours=2)))]
    db.tables["llm_jobs"].append({"id": "j-x", "job_type": m.JOB_SCAN, "target_id": "run-x", "status": "running",
                                  "attempts": 1, "created_at": NOW.isoformat()})
    m.poll_once()
    assert db.tables["rfp_portal_runs"][0]["status"] == "running"


# ── The scan ─────────────────────────────────────────────────────────────


def _seed_run(db, **over):
    row = _run(**over)
    db.tables["rfp_portal_runs"].append(row)
    return row


def test_apply_scan_inserts_new_rows_at_match_and_skips_closed_ones(db, settings):
    run = _seed_run(db)
    page = _page([
        _grid_row(),
        _grid_row(agency="City of Henderson", bid_number_raw="IFB 112-27 Fire Station 95 Addendum 2",
                  bid_number="IFB 112-27 Fire Station 95", addendum_no=2, title="Fire Station 95"),
        _grid_row(agency="Clark County", bid_number_raw="1790", bid_number="1790", addendum_no=None,
                  title="Closed one", close_at=NOW - timedelta(minutes=1)),
        _grid_row(agency="Clark County", bid_number_raw="1791", bid_number="1791", addendum_no=None,
                  title="No date", close_at=None),
    ], pages=2)
    counts = m.apply_scan(db, settings, run, page)
    assert counts == {"seen": 4, "new": 2, "changed": 0, "skipped_closed": 2}
    rows = db.tables["rfp_portal_invitations"]
    assert [r["bid_number"] for r in rows] == ["5584-GS", "IFB 112-27 Fire Station 95"]
    first = rows[0]
    assert first["status"] == "match" and first["agency_key"] == "unlv-nshe-business-center-south"
    assert first["addendum_no"] == 1 and first["bid_number_raw"] == "5584-GS Addendum 1"
    assert first["first_seen_run_id"] == run["id"] == first["last_seen_run_id"] and first["seen_count"] == 1
    assert first["view_url"] == VIEW and first["issued_on"] == "2026-08-25"
    assert first["close_at"] == m._iso(NOW + timedelta(days=1))


def test_apply_scan_refreshes_a_known_row_without_a_change(db, settings):
    run = _seed_run(db)
    db.tables["rfp_portal_invitations"].append(_invitation(status="done", missing_since=m._iso(NOW - timedelta(days=3)),
                                                           view_url="https://supplier.ionwave.net/old"))
    counts = m.apply_scan(db, settings, run, _page([_grid_row(response_status="Submitted", time_left="12 hours")]))
    assert counts == {"seen": 1, "new": 0, "changed": 0, "skipped_closed": 0}
    row = _inv(db)
    assert row["status"] == "done" and row["seen_count"] == 2 and row["missing_since"] is None
    assert row["last_seen_run_id"] == run["id"] and row["last_seen_at"] == m._iso(NOW)
    assert row["response_status"] == "Submitted" and row["time_left"] == "12 hours" and row["view_url"] == VIEW
    assert row["change_log"] == [] and _bells(db, m.NOTIFY_INVITATION_CHANGED) == []


@pytest.mark.parametrize(
    "field, grid_over, new_value",
    [
        ("title", {"title": "Emergency Phone Tower Installations and Upgrades (Rebid)"},
         "Emergency Phone Tower Installations and Upgrades (Rebid)"),
        ("close_at", {"close_at": NOW + timedelta(days=8)}, m._iso(NOW + timedelta(days=8))),
        ("addendum_no", {"bid_number_raw": "5584-GS Addendum 2", "addendum_no": 2}, 2),
    ],
)
def test_apply_scan_logs_and_bells_a_change(db, settings, field, grid_over, new_value):
    run = _seed_run(db)
    db.tables["rfp_portal_invitations"].append(_invitation(status="done"))
    counts = m.apply_scan(db, settings, run, _page([_grid_row(**grid_over)]))
    assert counts["changed"] == 1
    row = _inv(db)
    assert row[field] == new_value
    fields = {c["field"] for c in row["change_log"]}
    assert field in fields
    entry = next(c for c in row["change_log"] if c["field"] == field)
    assert entry["run_id"] == run["id"] and entry["at"] == m._iso(NOW) and entry["new"] == new_value
    if field == "addendum_no":
        assert "bid_number_raw" in fields and row["bid_number_raw"] == "5584-GS Addendum 2"
    bells = _bells(db, m.NOTIFY_INVITATION_CHANGED)
    assert len(bells) == 1 and bells[0]["role"] == Role.ESTIMATING_ADMIN and bells[0]["mirror_email"] is False
    assert bells[0]["metadata"]["invitation_id"] == INV and field in bells[0]["metadata"]["fields"]
    assert "UNLV" in bells[0]["message"] and "5584-GS" in bells[0]["message"]
    # The same change seen again: nothing new; a second change while the
    # first bell is unread: logged, not belled again.
    m.apply_scan(db, settings, run, _page([_grid_row(**grid_over)]))
    assert len(_inv(db)["change_log"]) == len(row["change_log"])
    m.apply_scan(db, settings, run, _page([_grid_row(**grid_over, bid_status="Amended")]))
    m.apply_scan(db, settings, run, _page([_grid_row(title="Third title", **{k: v for k, v in grid_over.items() if k != "title"})]))
    assert len(_bells(db, m.NOTIFY_INVITATION_CHANGED)) == 1
    # Once read, the next change rings again.
    _bells(db, m.NOTIFY_INVITATION_CHANGED)[0]["read_at"] = "x"
    m.apply_scan(db, settings, run, _page([_grid_row(title="Fourth title", **{k: v for k, v in grid_over.items() if k != "title"})]))
    assert len(_bells(db, m.NOTIFY_INVITATION_CHANGED)) == 2


def test_apply_scan_change_bell_is_silent_for_an_ignored_row_and_the_log_is_capped(db, settings):
    run = _seed_run(db)
    log = [{"at": m._iso(NOW - timedelta(days=i)), "field": "title", "old": f"t{i}", "new": f"t{i + 1}", "run_id": "r"}
           for i in range(50)]
    db.tables["rfp_portal_invitations"].append(_invitation(status="ignored", change_log=log))
    m.apply_scan(db, settings, run, _page([_grid_row(title="Changed while ignored")]))
    row = _inv(db)
    assert row["title"] == "Changed while ignored" and len(row["change_log"]) == 50
    assert row["change_log"][-1]["new"] == "Changed while ignored" and row["change_log"][0]["old"] == "t1"
    assert _bells(db, m.NOTIFY_INVITATION_CHANGED) == []


def test_apply_scan_stores_an_empty_title_empty_and_the_match_step_routes_it_to_harvest(db, settings, llm_answer):
    """No app-authored placeholder ever reaches the row: the matcher would
    score "(untitled)" and the bell would repeat it. The empty title takes
    the no-name exit; the frontend renders its own placeholder."""
    calls = llm_answer({"verdicts": []})
    run = _seed_run(db)
    db.tables["projects"] = [_project(P1, "Untitled")]
    m.apply_scan(db, settings, run, _page([_grid_row(title="   ")]))
    row = db.tables["rfp_portal_invitations"][0]
    assert row["title"] == "" and row["status"] == "match"
    _sweep(db)
    assert row["status"] == "harvest" and row["flag_reason"] == "no_project_name" and calls == []
    # Seen again with the same empty title: no change logged.
    m.apply_scan(db, settings, run, _page([_grid_row(title="")]))
    assert row["change_log"] == []
    # A title that appears later is a change, belled without a parenthetical
    # placeholder for the old value.
    m.apply_scan(db, settings, run, _page([_grid_row(title="Now titled")]))
    assert row["title"] == "Now titled"
    bell = _bells(db, m.NOTIFY_INVITATION_CHANGED)[0]["message"]
    assert "(untitled)" not in bell and "title none -> Now titled" in bell


def test_apply_scan_merges_rows_that_key_alike_within_one_scan(db, settings, caplog):
    """Two grid rows whose agencies differ only in punctuation or case key
    the same: the first row's values stand, the second is logged, and the
    unique index is never hit twice."""
    run = _seed_run(db)
    page = _page([
        _grid_row(agency="Clark County", bid_number_raw="1790", bid_number="1790", addendum_no=None, title="First"),
        _grid_row(agency="CLARK  COUNTY.", bid_number_raw="1790", bid_number="1790", addendum_no=None, title="Second"),
        _grid_row(agency="Clark County", bid_number_raw="1790 Addendum 1", bid_number="1790", addendum_no=1,
                  title="Third"),
    ])
    with caplog.at_level("INFO", logger="app.services.rfp_portal_ingest"):
        counts = m.apply_scan(db, settings, run, page)
    assert counts == {"seen": 3, "new": 1, "changed": 0, "skipped_closed": 0}
    rows = db.tables["rfp_portal_invitations"]
    assert len(rows) == 1 and rows[0]["title"] == "First" and rows[0]["agency"] == "Clark County"
    assert sum("twice" in r.getMessage() for r in caplog.records) == 2
    # The next scan sees the same collision against the stored row: still
    # one row, the first grid row compared, no change.
    m.apply_scan(db, settings, run, page)
    assert len(db.tables["rfp_portal_invitations"]) == 1 and rows[0]["change_log"] == []


def test_apply_scan_agency_fallback_keys_the_same_way_the_stale_link_refind_does(db, settings, portal, sandbox):
    run = _seed_run(db)
    m.apply_scan(db, settings, run, _page([_grid_row(agency="")]))
    row = db.tables["rfp_portal_invitations"][0]
    assert row["agency"] == m._UNKNOWN_AGENCY and row["agency_key"] == m.agency_key("") == "unknown-agency"
    assert m._grid_row_for(_page([_grid_row(agency="")]), row) is not None
    assert m._grid_row_for(_page([_grid_row(agency="  ")]), row) is not None
    assert m._grid_row_for(_page([_grid_row(agency="Someone")]), row) is None
    # The stale-link refresh finds it through the same key.
    row.update(status="harvest")
    fresh = "https://supplier.ionwave.net/VendorResponse/Bid/VResponseEvent.aspx?e=fresh"
    portal.session.errors[("event", VIEW)] = PortalExc("BadRequest", "permanent", stale=True)
    portal.session.page = _page([_grid_row(agency="", view_url=fresh)])
    m.execute_harvest(row["id"])
    assert row["view_url"] == fresh and row["status"] == "split"


def test_apply_scan_does_not_stamp_a_closed_bid_the_grid_still_lists_as_missing(db, settings):
    run = _seed_run(db)
    db.tables["rfp_portal_invitations"].append(_invitation(status="done"))
    counts = m.apply_scan(db, settings, run, _page([_grid_row(close_at=NOW - timedelta(hours=1))]))
    assert counts["skipped_closed"] == 1
    row = _inv(db)
    assert row["missing_since"] is None and row["seen_count"] == 1   # listed, closed: not refreshed, not missing


def test_apply_scan_marks_missing_rows_but_not_ignored_ones_nor_a_truncated_grid(db, settings):
    run = _seed_run(db)
    db.tables["rfp_portal_invitations"].extend([
        _invitation(),
        _invitation(id="inv-2", bid_number="1790", bid_number_raw="1790", agency="Clark County",
                    agency_key="clark-county", status="done"),
        _invitation(id="inv-3", bid_number="1791", bid_number_raw="1791", agency="Clark County",
                    agency_key="clark-county", status="ignored"),
        _invitation(id="inv-4", bid_number="1792", bid_number_raw="1792", agency="Clark County",
                    agency_key="clark-county", status="exists", missing_since=m._iso(NOW - timedelta(days=9))),
    ])
    m.apply_scan(db, settings, run, _page([_grid_row()]))
    by_id = {r["id"]: r for r in db.tables["rfp_portal_invitations"]}
    assert by_id[INV]["missing_since"] is None
    assert by_id["inv-2"]["missing_since"] == m._iso(NOW)
    assert by_id["inv-3"]["missing_since"] is None
    assert by_id["inv-4"]["missing_since"] == m._iso(NOW - timedelta(days=9))   # kept, not moved
    # A grid the page cap truncated says nothing about absence.
    by_id["inv-2"]["missing_since"] = None
    m.apply_scan(db, settings, run, _page([_grid_row()], total_items=40))
    assert _inv(db, "inv-2")["missing_since"] is None


def test_execute_scan_completes_the_run_and_rings_the_new_invitations_bell_once(db, settings, portal):
    run = _seed_run(db)
    portal.session.page = _page([_grid_row(), _grid_row(agency="Clark County", bid_number_raw="1790",
                                                          bid_number="1790", addendum_no=None, title="Other")], pages=3)
    m.execute_scan(run["id"])
    row = db.tables["rfp_portal_runs"][0]
    assert row["status"] == "complete" and row["pages_scanned"] == 3 and row["invitations_seen"] == 2
    assert row["invitations_new"] == 2 and row["claimed_by"] is None and row["finished_at"] == m._iso(NOW)
    assert portal.session.closed and portal.session.calls == [("invitations", settings.rfp_ngem_max_list_pages)]
    bells = _bells(db, m.NOTIFY_NEW_INVITATIONS)
    # One row per active review-role user (never the inactive admin, never
    # the accountant), each bound to the run.
    assert len(bells) == 5 and {b["role"] for b in bells} == set(m.RFP_REVIEW_ROLES)
    assert {b["user_id"] for b in bells} == {f"u-{r.value}" for r in m.RFP_REVIEW_ROLES}
    assert bells[0]["message"] == "2 new NGEM invitations" and bells[0]["metadata"] == {"run_id": run["id"], "count": 2}
    assert all(b["mirror_email"] is False for b in bells)
    # A retry of the same (already complete) run is idempotent: no second
    # bell for the run, nothing re-scanned.
    calls_before = list(portal.session.calls)
    m.execute_scan(run["id"])
    assert len(_bells(db, m.NOTIFY_NEW_INVITATIONS)) == 5 and portal.session.calls == calls_before
    assert db.tables["rfp_portal_runs"][0]["status"] == "complete"
    # A later run with new rows is a new event: it rings again for everyone,
    # whether or not the earlier bell was read (the dedupe is per run).
    run2 = _seed_run(db, id="run-b", scheduled_for=m._iso(NOW))
    portal.session.page = _page([_grid_row(agency="Clark County", bid_number_raw="1791", bid_number="1791",
                                           addendum_no=None, title="Third")])
    m.execute_scan("run-b")
    assert len(_bells(db, m.NOTIFY_NEW_INVITATIONS)) == 10
    assert m.scan_status(run["id"]) == "done" and m.scan_status(run2["id"]) == "done"
    assert m.scan_status("nope") is None


def test_new_invitations_bell_dedupes_per_user_and_per_run(db):
    """A user who still holds an unread bell for this run is not rung again;
    a user who read theirs is; another user's unread bell never silences
    anyone else's (the old cross-user dedupe)."""
    assert m._notify_new_invitations(db, "run-1", 3) == 5
    assert m._notify_new_invitations(db, "run-1", 3) == 0
    bells = _bells(db, m.NOTIFY_NEW_INVITATIONS)
    mine = next(b for b in bells if b["user_id"] == ADMIN_USER)
    mine["read_at"] = "read"
    assert m._notify_new_invitations(db, "run-1", 3) == 1
    assert [b["user_id"] for b in _bells(db, m.NOTIFY_NEW_INVITATIONS)][-1] == ADMIN_USER
    # Dismissed counts as handled too; a different run is a different event.
    for b in _bells(db, m.NOTIFY_NEW_INVITATIONS):
        b["dismissed_at"] = "x"
    assert m._notify_new_invitations(db, "run-1", 1) == 5
    assert m._notify_new_invitations(db, "run-2", 1) == 5
    assert m._notify_new_invitations(db, "run-3", 0) == 0


def test_change_bell_dedupes_per_user_and_per_invitation_and_caps_its_text(db, settings):
    run = _seed_run(db)
    db.tables["profiles"].append({"id": "u-admin-2", "full_name": "Second Admin",
                                  "role": Role.ESTIMATING_ADMIN.value, "is_active": True})
    db.tables["rfp_portal_invitations"].append(_invitation(status="done", title="T" * 400))
    long_title = "L" * 2_000
    m.apply_scan(db, settings, run, _page([_grid_row(title=long_title)]))
    bells = _bells(db, m.NOTIFY_INVITATION_CHANGED)
    assert {b["user_id"] for b in bells} == {ADMIN_USER, "u-admin-2"}
    message = bells[0]["message"]
    assert len(message) <= m._BELL_MESSAGE_MAX_CHARS and "LLLL..." in message and long_title not in message
    assert "TTTT..." in message
    assert bells[0]["metadata"]["invitation_id"] == INV
    # One admin reads theirs: the next change rings that admin only.
    next(b for b in bells if b["user_id"] == ADMIN_USER)["read_at"] = "read"
    m.apply_scan(db, settings, run, _page([_grid_row(title="Short title")]))
    assert [b["user_id"] for b in _bells(db, m.NOTIFY_INVITATION_CHANGED)][-1] == ADMIN_USER
    assert len(_bells(db, m.NOTIFY_INVITATION_CHANGED)) == 3
    # Another invitation's change is its own bell for everyone.
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", bid_number="1790", bid_number_raw="1790",
                                                           agency="Clark County", agency_key="clark-county",
                                                           status="done", title="Old"))
    m.apply_scan(db, settings, run, _page([
        _grid_row(title="Short title"),
        _grid_row(agency="Clark County", bid_number_raw="1790", bid_number="1790", addendum_no=None, title="New"),
    ]))
    assert len(_bells(db, m.NOTIFY_INVITATION_CHANGED)) == 5


def test_bell_text_caps():
    assert m._bell_text("a" * 100) == "a" * 77 + "..."
    assert m._bell_text("  a   b ") == "a b" and m._bell_text(None) == ""
    assert len(m._bell_message("x" * 1000)) == m._BELL_MESSAGE_MAX_CHARS
    assert m._bell_text("t" * 200, m._BELL_TITLE_MAX_CHARS).endswith("...")
    assert len(m._bell_text("t" * 200, m._BELL_TITLE_MAX_CHARS)) == m._BELL_TITLE_MAX_CHARS


def test_execute_scan_refuses_a_failed_run_and_returns_on_a_complete_one(db, portal):
    _seed_run(db, status="failed")
    with pytest.raises(m.RfpPortalPermanent):
        m.execute_scan("run-a")
    assert portal.opened == 0
    # A retry against a run that already completed is idempotent: no portal
    # request, no write, no exception for the queue to mark.
    db.tables["rfp_portal_runs"][0].update(status="complete", finished_at="then")
    assert m.execute_scan("run-a") is None
    assert portal.opened == 0 and db.tables["rfp_portal_runs"][0]["finished_at"] == "then"


def test_execute_scan_writes_are_fenced_on_the_claim_token(db, portal, monkeypatch):
    """A zombie attempt (its lease expired, the run reclaimed by a newer
    attempt) can neither complete, park nor fail the run the newer attempt
    owns; nor can its bell fire twice."""
    _seed_run(db)
    run = db.tables["rfp_portal_runs"][0]

    def steal(max_pages):
        # The newer attempt reclaimed the run while this one was on the portal.
        run["claimed_by"] = "newer-attempt"
        return _page([_grid_row()])
    portal.session.fetch_invitations = steal
    m.execute_scan("run-a")
    assert run["status"] == "running" and run["claimed_by"] == "newer-attempt"
    assert run["invitations_new"] == 0 and _bells(db, m.NOTIFY_NEW_INVITATIONS) == []
    # The same for a park and a permanent failure.
    for kind, expect in (("unavailable", None), ("permanent", m.RfpPortalPermanent)):
        run.update(status="running", claimed_by="newer-attempt", last_error=None)

        def raise_after_steal(max_pages, kind=kind):
            run["claimed_by"] = "newer-attempt"
            raise PortalExc("trouble", kind)
        portal.session.fetch_invitations = raise_after_steal
        if expect is None:
            m.execute_scan("run-a")
        else:
            with pytest.raises(expect):
                m.execute_scan("run-a")
        assert run["status"] == "running" and run["last_error"] is None
    assert _bells(db, m.NOTIFY_SCAN_FAILED) == []


def test_execute_scan_bell_failure_never_fails_a_complete_run(db, portal, monkeypatch):
    _seed_run(db)

    def boom(*a, **k):
        raise RuntimeError("notifications down")
    monkeypatch.setattr(m, "notify_user", boom)
    m.execute_scan("run-a")
    run = db.tables["rfp_portal_runs"][0]
    assert run["status"] == "complete" and run["invitations_new"] == 1 and run["last_error"] is None


def test_failed_runs_ring_the_it_admins_once(db, monkeypatch):
    monkeypatch.setattr(m, "sweep", lambda sb, lease_key=None: None)
    db.tables["rfp_portal_runs"] = [
        _run(id="run-l", scheduled_for=m._iso(NOW - timedelta(days=2)), status="running",
             started_at=m._iso(NOW - timedelta(hours=2))),
    ]
    m.poll_once()
    bells = _bells(db, m.NOTIFY_SCAN_FAILED)
    assert len(bells) == 1 and bells[0]["role"] == Role.IT_ADMIN and bells[0]["mirror_email"] is False
    assert m._MSG_RUN_LOST in bells[0]["message"] and bells[0]["metadata"]["run_id"] == "run-l"
    assert bells[0]["metadata"]["error"] == m._MSG_RUN_LOST
    # Another failure while that bell is unread: no second bell; once read, it rings again.
    _seed_run(db, id="run-q", scheduled_for=None, trigger="manual", status="running", started_at=m._iso(NOW))
    m.mark_scan_from_queue("run-q", "failed", "The run was interrupted repeatedly.")
    assert len(_bells(db, m.NOTIFY_SCAN_FAILED)) == 1
    bells[0]["read_at"] = "read"
    _seed_run(db, id="run-r", scheduled_for=None, trigger="manual", status="running", started_at=m._iso(NOW))
    m.mark_scan_from_queue("run-r", "failed", "x" * 900)
    bells = _bells(db, m.NOTIFY_SCAN_FAILED)
    assert len(bells) == 2 and len(bells[1]["message"]) <= m._BELL_MESSAGE_MAX_CHARS
    # A fenced-out failure (the run already finished) rings nothing.
    m.mark_scan_from_queue("run-r", "failed", "again")
    assert len(_bells(db, m.NOTIFY_SCAN_FAILED)) == 2


def test_execute_scan_404_is_permanent(db):
    with pytest.raises(m.RfpPortalPermanent) as exc:
        m.execute_scan("missing")
    assert exc.value.http_status == 404


def test_execute_scan_parks_the_run_while_locked_or_unavailable(db, settings, portal):
    until = NOW + timedelta(hours=3)
    _seed_run(db)
    portal.available = (False, "NGEM logins are locked after repeated failures.", until)
    m.execute_scan("run-a")
    row = db.tables["rfp_portal_runs"][0]
    assert row["status"] == "queued" and row["next_attempt_at"] == m._iso(until)
    assert "locked" in row["last_error"] and portal.opened == 0
    # Unavailable raised by the session itself: the poll interval.
    portal.available = (True, None, None)
    portal.session.errors["invitations"] = PortalExc("login attempted recently", "unavailable")
    row["status"] = "queued"
    m.execute_scan("run-a")
    row = db.tables["rfp_portal_runs"][0]
    assert row["status"] == "queued" and row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=settings.rfp_ngem_poll_seconds))
    assert _jobs(db, m.JOB_SCAN) == []


def test_execute_scan_transient_keeps_the_run_running_for_the_ladder(db, portal):
    _seed_run(db)
    portal.session.errors["invitations"] = PortalExc("NGEM answered 503.", "transient")
    with pytest.raises(m.RfpPortalTransient, match="503"):
        m.execute_scan("run-a")
    row = db.tables["rfp_portal_runs"][0]
    assert row["status"] == "running" and row["last_error"] == "NGEM answered 503."
    # The retry runs against the running row.
    portal.session.errors.clear()
    m.execute_scan("run-a")
    assert db.tables["rfp_portal_runs"][0]["status"] == "complete"


def test_execute_scan_permanent_fails_the_run_and_raises(db, portal):
    _seed_run(db)
    portal.session.errors["invitations"] = PortalExc("The page is not the invitations grid.", "permanent")
    with pytest.raises(m.RfpPortalPermanent, match="grid"):
        m.execute_scan("run-a")
    row = db.tables["rfp_portal_runs"][0]
    assert row["status"] == "failed" and row["last_error"] == "The page is not the invitations grid."
    # The queue's own mark afterwards is fenced out.
    m.mark_scan_from_queue("run-a", "failed", "later")
    assert db.tables["rfp_portal_runs"][0]["last_error"] == "The page is not the invitations grid."


def test_execute_scan_unexpected_error_is_transient_with_an_app_sentence(db, portal):
    _seed_run(db)
    portal.session.errors["invitations"] = KeyError("boom")
    with pytest.raises(m.RfpPortalTransient) as exc:
        m.execute_scan("run-a")
    assert str(exc.value) == m._MSG_INTERRUPTED
    assert db.tables["rfp_portal_runs"][0]["last_error"] == m._MSG_INTERRUPTED


def test_mark_scan_from_queue_fails_only_active_runs(db):
    _seed_run(db, status="running")
    m.mark_scan_from_queue("run-a", "pending", None)
    assert db.tables["rfp_portal_runs"][0]["status"] == "running"
    m.mark_scan_from_queue("run-a", "failed", "The run was interrupted repeatedly.")
    row = db.tables["rfp_portal_runs"][0]
    assert row["status"] == "failed" and row["last_error"] == "The run was interrupted repeatedly."


# ── The match step ───────────────────────────────────────────────────────


def _seed_match(db, projects, **over):
    db.tables["projects"] = projects
    row = _invitation(**over)
    db.tables["rfp_portal_invitations"].append(row)
    return row


def _sweep(db):
    m.sweep(db, lease_key=None)


def test_match_no_candidate_goes_to_harvest_and_enqueues_the_job_in_the_same_pass(db, llm_answer):
    calls = llm_answer({"verdicts": []})
    _seed_match(db, [_project(P1, "Warehouse HVAC Upgrade")])
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "harvest" and row["flag_reason"] == "no_candidate"
    assert row["match_candidates"][0]["project_id"] == P1 and row["match_project_id"] is None
    assert row["match_weights"]["scorer_version"] and row["matched_at"] == m._iso(NOW)
    assert row["attempts"] == 0 and row["last_error"] is None
    assert row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=60))
    assert calls == []
    jobs = _jobs(db)
    assert len(jobs) == 1 and jobs[0]["target_id"] == INV and jobs[0]["payload"] == {"invitation_id": INV, "force": False}
    assert jobs[0]["priority"] == 150


def test_match_confident_same_resolves_to_exists_with_auto_resolve_on(db, llm_answer):
    calls = llm_answer({"verdicts": [{"index": 0, "verdict": "same", "confidence": 0.95, "reasoning": "same"}]})
    _seed_match(db, [_project(P1, "Emergency Phone Tower Installations and Upgrades", days=1)])
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "exists" and row["match_project_id"] == P1 and row["flag_reason"] is None
    assert row["resolution"] == "system" and row["resolved_by"] is None and row["resolved_at"] == m._iso(NOW)
    assert row["match_llm_model"] == "test-model" and row["match_llm_prompt_version"]
    assert row["match_score"] and row["match_score"] >= 0.85
    assert len(calls) == 1 and calls[0]["feature"] == "rfp_match"
    assert _jobs(db) == []
    assert m.portal_sources_for_projects(db, [P1]) == {P1: [{
        "portal": "ngem", "invitation_id": INV, "agency": row["agency"], "bid_number": "5584-GS",
        "bid_number_raw": "5584-GS Addendum 1", "addendum_no": 1, "close_at": row["close_at"],
        "resolution": "system", "resolved_at": m._iso(NOW),
    }]}


def test_match_confident_waits_for_a_click_with_auto_resolve_off(db, llm_answer, monkeypatch):
    monkeypatch.setattr(m, "get_settings", lambda: _settings(rfp_ngem_auto_resolve_enabled=False))
    llm_answer({"verdicts": [{"index": 0, "verdict": "same", "confidence": 0.95, "reasoning": "same"}]})
    _seed_match(db, [_project(P1, "Emergency Phone Tower Installations and Upgrades", days=1)])
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_confident"
    assert row["match_project_id"] == P1 and row["resolution"] is None
    # The snapshot records the switch THIS step routed by, next to the
    # shared one (which is the email side's).
    assert row["match_weights"]["auto_resolve_enabled"] is False
    assert row["match_weights"]["auto_merge_enabled"] is False and row["match_weights"]["scorer_version"]


def test_match_snapshot_records_the_auto_resolve_switch_when_on(db, llm_answer):
    llm_answer({"verdicts": []})
    _seed_match(db, [_project(P1, "Warehouse HVAC Upgrade")])
    _sweep(db)
    weights = _inv(db)["match_weights"]
    assert weights["auto_resolve_enabled"] is True
    assert weights == {**m.rfp_match.settings_snapshot(_settings()), "auto_resolve_enabled": True}


def test_sweep_queries_each_portal_by_name_so_the_index_applies(db, llm_answer, monkeypatch):
    llm_answer({"verdicts": []})
    seen = []
    real_table = db.table

    class Spy(PortalQuery):
        def eq(self, col, val):
            if self.table == "rfp_portal_invitations" and col == "portal":
                seen.append(val)
            return super().eq(col, val)

    monkeypatch.setattr(db, "table", lambda name: Spy(db, name) if name == "rfp_portal_invitations" else real_table(name))
    _seed_match(db, [_project(P1, "Warehouse HVAC Upgrade")])
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-other", portal="other", bid_number="x"))
    _sweep(db)
    assert seen[0] == "ngem" and _inv(db)["status"] == "harvest"
    assert _inv(db, "inv-other")["status"] == "match"   # not swept: no client for it, never selected


def test_match_uncertain_and_ambiguous_wait_at_review_match_with_the_rebid_hint(db, llm_answer):
    llm_answer({"verdicts": [{"index": 0, "verdict": "unsure", "confidence": 0.4, "reasoning": "?"}]})
    _seed_match(db, [
        _project(P1, "Emergency Phone Tower Installations and Upgrades", days=20),
        _project(P2, "Emergency Phone Tower Installation and Upgrade", days=-200, number="25.1.7000"),
    ])
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_uncertain"
    assert row["match_project_id"] == P1 and row["decided_at_step"] == "match"
    assert row["possible_rebid_project_id"] == P2 and row["possible_rebid_score"] >= 0.85


def test_match_excluded_ids_are_never_candidates(db, llm_answer):
    calls = llm_answer({"verdicts": [{"index": 0, "verdict": "same", "confidence": 0.95, "reasoning": "same"}]})
    _seed_match(db, [_project(P1, "Emergency Phone Tower Installations and Upgrades", days=1)],
                excluded_project_ids=[P1])
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "harvest" and row["flag_reason"] == "no_candidate" and row["match_candidates"] == []
    assert calls == []


def test_match_no_name_title_goes_straight_to_harvest(db, llm_answer):
    calls = llm_answer({"verdicts": []})
    _seed_match(db, [_project(P1, "Anything")], title="Project of the")
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "harvest" and row["flag_reason"] == "no_project_name"
    assert row["match_candidates"] == [] and calls == [] and len(_jobs(db)) == 1


def test_match_model_down_waits_without_spending_and_stops_the_tick(db, llm_answer, monkeypatch):
    calls = llm_answer({"verdicts": []})
    monkeypatch.setattr(llm_health, "cached", lambda settings=None, force=False: _Snapshot("provider_down", "box off"))
    _seed_match(db, [_project(P1, "Emergency Phone Tower Installations and Upgrades", days=1)])
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", bid_number="1790", bid_number_raw="1790",
                                                           agency="Clark County", agency_key="clark-county",
                                                           title="Emergency Phone Tower Installations and Upgrades",
                                                           first_seen_at=m._iso(NOW - timedelta(days=1))))
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "match" and row["attempts"] == 0 and row["last_error"] == "box off"
    assert row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=300))
    assert _inv(db, "inv-2")["next_attempt_at"] is None   # the tick stopped calling
    assert calls == []
    # A connection error on the live call is the same wait.
    monkeypatch.setattr(llm_health, "cached", lambda settings=None, force=False: _Snapshot())
    import httpx
    llm_answer(httpx.ConnectError("refused"))
    row["next_attempt_at"] = None
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "match" and row["attempts"] == 0 and row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=300))


def test_match_transient_failures_climb_the_ladder_to_match_failed(db, llm_answer, monkeypatch):
    monkeypatch.setattr(m, "get_settings", lambda: _settings(rfp_email_ingestion_classify_max_attempts=3))
    llm_answer(RuntimeError("the provider answered 500"))
    _seed_match(db, [_project(P1, "Emergency Phone Tower Installations and Upgrades", days=1)])
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "match" and row["attempts"] == 1 and row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=60))
    row["next_attempt_at"] = None
    _sweep(db)
    row = _inv(db)
    assert row["attempts"] == 2 and row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=300))
    row["next_attempt_at"] = None
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_failed"
    assert row["attempts"] == 3 and row["last_error"] and row["next_attempt_at"] is None


def test_match_unusable_output_twice_parks_at_review_match(db, llm_answer):
    llm_answer(llm.LlmBadOutput("Model response was not valid JSON."))
    _seed_match(db, [_project(P1, "Emergency Phone Tower Installations and Upgrades", days=1)])
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "match" and row["attempts"] == 1
    row["next_attempt_at"] = None
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "review_match" and row["flag_reason"] == "match_llm_unusable"
    assert row["match_project_id"] == P1 and row["last_error"]


def test_map_route_status_covers_every_route_outcome():
    assert m._map_route_status("duplicate") == "exists" and m._map_route_status("merged") == "exists"
    assert m._map_route_status("review_match") == "review_match" and m._map_route_status("done") == "harvest"
    assert m._map_route_status("something_new") == "review_match"


# ── The harvest step ─────────────────────────────────────────────────────


def test_harvest_step_parks_while_locked_and_enqueues_once(db, portal, settings):
    until = NOW + timedelta(hours=2)
    portal.available = (False, "locked", until)
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    _sweep(db)
    row = _inv(db)
    assert row["status"] == "harvest" and row["next_attempt_at"] == m._iso(until) and row["last_error"] == "locked"
    assert _jobs(db) == [] and row["attempts"] == 0
    portal.available = (True, None, None)
    row["next_attempt_at"] = None
    _sweep(db)
    assert len(_jobs(db)) == 1 and _inv(db)["next_attempt_at"] == m._iso(NOW + timedelta(seconds=60))
    _inv(db)["next_attempt_at"] = None
    _sweep(db)
    assert len(_jobs(db)) == 1
    assert portal.opened == 0


def test_harvest_step_enqueue_failure_rides_the_ladder_and_never_loses_the_row(db, monkeypatch):
    monkeypatch.setattr(m, "get_settings", lambda: _settings(rfp_email_ingestion_classify_max_attempts=2))

    def boom(*a, **k):
        raise RuntimeError("queue down")
    monkeypatch.setattr(llm_queue, "enqueue", boom)
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    _sweep(db)
    row = _inv(db)
    assert row["attempts"] == 1 and "queue down" in row["last_error"]
    assert row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=60))
    row["next_attempt_at"] = None
    _sweep(db)
    row = _inv(db)
    # Held at the cap: attempts pinned at it (the page reads "2 of 2").
    assert row["status"] == "harvest" and row["attempts"] == 2
    assert row["next_attempt_at"] == m._iso(NOW + timedelta(hours=1))


def test_holding_at_the_cap_alerts_it_admin_once_per_step(db, monkeypatch):
    monkeypatch.setattr(m, "get_settings", lambda: _settings(rfp_email_ingestion_classify_max_attempts=2))
    db.tables.setdefault("notifications", [])
    db.tables["profiles"] = [{"id": "it-1", "role": "it_admin", "is_active": True,
                              "is_dev": False, "rfp_mailboxes": []}]
    monkeypatch.setattr(
        m.rfp_email_ingest, "notify_user",
        lambda user_id, project_id, type_, message, **k: db.tables["notifications"].append(
            {"type": type_, "message": message, "user_id": user_id, "metadata": k.get("metadata")}),
    )

    def boom(*a, **k):
        raise RuntimeError("queue down")
    monkeypatch.setattr(llm_queue, "enqueue", boom)
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    for _ in range(4):  # attempt 1, the cap, then hourly re-tries at the cap
        _sweep(db)
        _inv(db)["next_attempt_at"] = None
    alerts = [n for n in db.tables["notifications"] if n["type"] == "rfp_processing.step_failed"]
    assert len(alerts) == 1 and alerts[0]["user_id"] == "it-1"
    assert alerts[0]["metadata"] == {"source": "portal", "row_id": _inv(db)["id"], "step": "harvest"}
    assert "queue down" in alerts[0]["message"]
    # The row recovers, moves on, and later holds at the same step again: a new alert.
    _inv(db)["attempts"] = 0
    for _ in range(2):
        _sweep(db)
        _inv(db)["next_attempt_at"] = None
    assert len([n for n in db.tables["notifications"] if n["type"] == "rfp_processing.step_failed"]) == 2


# ── The harvest job ──────────────────────────────────────────────────────


def _hv(db):
    return db.tables["rfp_harvests"][0]


def test_execute_harvest_pipeline_writes_facts_then_files_then_create(db, portal, sandbox, settings):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest", flag_reason="no_candidate"))
    facts_seen = []
    real_add = sandbox.add_upload_file

    def add(run_id, **k):
        facts_seen.append(copy.deepcopy(_hv(db)))
        return real_add(run_id, **k)
    sandbox.add_upload_file = add
    m.rfp_ingest.add_upload_file = add
    m.execute_harvest(INV)
    hv = _hv(db)
    assert hv["status"] == "complete" and hv["method"] == "ngem" and hv["rfp_email_id"] is None
    assert hv["portal_invitation_id"] == INV and hv["external_key"] == "ngem:unlv-nshe-business-center-south:5584-GS"
    assert hv["external_url"] == ENTRY and hv["claim_token"] is None and hv["attempts"] == 1
    assert hv["files_accepted"] == 2 and hv["file_count"] == 2 and hv["bytes_downloaded"] == 2 * len(PDF)
    assert hv["sandbox_run_id"] == "run-1"
    data = hv["data"]
    assert data["platform"] == "ngem" and data["bid_number"] == "5584-GS" and data["addendum_no"] == 1
    assert data["title"] == "Emergency Phone Tower Installations and Upgrades" and data["bid_type"] == "IFB"
    assert data["contact"]["email"] == "pat@unlv.edu" and data["documents"] == {"count": 2, "bytes": 500}
    assert data["close_at"] == m._iso(NOW + timedelta(days=1)) and data["issued_at"] == m._iso(NOW - timedelta(days=20))
    assert "<script>" not in data["notes_html"] and "<b>walk</b>" in data["notes_html"]
    assert 'href="https://unlv.edu"' in data["notes_html"]
    assert hv["description_text"] == "Pre-bid walk at UNLV"
    assert "view_url" not in data and VIEW not in str(hv)
    # Facts landed before the first download.
    assert facts_seen[0]["facts_at"] == m._iso(NOW) and facts_seen[0]["data"]["title"] and facts_seen[0]["status"] == "running"
    assert [f["status"] for f in hv["files"]] == ["accepted", "accepted"]
    assert hv["files"][0] == {"index": 1, "file_name": "Plans.pdf", "description": "Drawings", "size": 300,
                              "sandbox_file_id": "f-2", "status": "accepted", "error": None}
    assert all("url" not in json_key for f in hv["files"] for json_key in f)
    assert sandbox.names() == ["create", "add", "add", "start", "dispatch"]
    assert sandbox.calls[0] == ("create", INV, hv["id"])
    assert sandbox.calls[1][5] == {"kind": "ngem", "file_name": "Plans.pdf", "harvest_id": hv["id"]}
    assert sandbox.calls[1][2] == "d1.pdf"   # the portal's declared filename
    downloads = [c for c in portal.session.calls if c[0] == "download"]
    assert downloads == [("download", DL1, settings.rfp_ingest_max_file_bytes, ATT),
                         ("download", DL2, settings.rfp_ingest_max_file_bytes, ATT)]
    row = _inv(db)
    assert row["status"] == "split" and row["harvest_id"] == hv["id"] and row["harvested_at"] == m._iso(NOW)
    assert row["attempts"] == 0 and row["last_error"] is None and row["next_attempt_at"] is None
    assert row["flag_reason"] == "no_candidate" and row["decided_at_step"] == "harvest"
    assert portal.session.closed and m.current_status(INV) == "done"


def test_execute_harvest_reuses_a_young_complete_harvest_without_the_portal(db, portal, sandbox):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    db.tables["rfp_harvests"].append(_harvest_row())
    m.execute_harvest(INV)
    assert portal.opened == 0 and sandbox.calls == []
    row = _inv(db)
    assert row["status"] == "split" and row["harvest_id"] == "hv-1"
    # `force` refreshes it (manual mode from create: the status stays).
    m.execute_harvest(INV, force=True)
    assert portal.opened == 1 and _hv(db)["status"] == "complete" and _hv(db)["attempts"] == 2
    assert _inv(db)["status"] == "split"


def test_execute_harvest_per_file_outcomes(db, portal, sandbox, settings):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    s = portal.session
    d3, d4, d5, d6, d7 = (f"https://supplier.ionwave.net/Extract.aspx?e=d{i}" for i in range(3, 8))
    s.attachments = [
        _attachment(1, "Plans.pdf", 300, DL1),
        _attachment(2, "NotPdf.exe", 100, DL2),
        _attachment(3, "NoLink.pdf", 100, None),
        _attachment(4, "Huge.pdf", settings.rfp_ingest_max_file_bytes + 1, d3),
        _attachment(5, "Flaky.pdf", 100, d4),
        _attachment(6, "Forbidden.pdf", 100, d5),
        _attachment(7, "Dupe.pdf", 100, d6),
        _attachment(8, "../evil/../Weird:name?.pdf", 100, d7),
    ]
    s.bytes_for[DL2] = NOT_PDF
    s.errors[d4] = [PortalExc("timeout", "transient"), PortalExc("timeout", "transient"), PortalExc("timeout", "transient")]
    s.errors[d5] = PortalExc("The download link is not one the client may follow.", "permanent")
    sandbox.outcomes["d6.pdf"] = "duplicate"
    m.execute_harvest(INV)
    hv = _hv(db)
    by_name = {f["file_name"]: f for f in hv["files"]}
    assert by_name["Plans.pdf"]["status"] == "accepted"
    assert by_name["NotPdf.exe"]["status"] == "rejected" and "not a PDF" in by_name["NotPdf.exe"]["error"]
    assert by_name["NoLink.pdf"]["status"] == "download_failed"
    assert by_name["Huge.pdf"]["status"] == "too_large"
    assert by_name["Flaky.pdf"]["status"] == "download_failed" and by_name["Flaky.pdf"]["error"] == "timeout"
    assert by_name["Forbidden.pdf"]["status"] == "download_failed"
    assert by_name["Dupe.pdf"]["status"] == "accepted" and by_name["Dupe.pdf"]["sandbox_file_id"] == "f-existing"
    assert "Weirdname.pdf" in by_name and by_name["Weirdname.pdf"]["status"] == "accepted"
    assert hv["status"] == "complete" and hv["files_accepted"] == 3
    assert s.calls.count(("download", d4, settings.rfp_ingest_max_file_bytes, ATT)) == 3
    assert _inv(db)["status"] == "split"


def test_execute_harvest_empty_grid_and_all_rejected_runs(db, portal, sandbox):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    portal.session.attachments = []
    m.execute_harvest(INV)
    hv = _hv(db)
    assert hv["status"] == "complete" and hv["last_error"] == m._MSG_NO_FILES and hv["file_count"] == 0
    assert sandbox.calls == [] and _inv(db)["status"] == "split"
    # All rejected: the run is created then deleted, the outcomes kept.
    db.tables["rfp_portal_invitations"][0].update(status="harvest", harvest_id=None)
    portal.session.attachments = [_attachment(1, "Bad.exe", 10, DL1)]
    portal.session.bytes_for[DL1] = NOT_PDF
    m.execute_harvest(INV, force=True)
    hv = _hv(db)
    assert hv["status"] == "complete" and hv["sandbox_run_id"] is None and hv["files"][0]["status"] == "rejected"
    assert sandbox.names() == ["create", "add", "delete"] and db.tables["rfp_ingest_runs"] == []


def test_execute_harvest_caps_are_permanent_and_keep_the_facts(db, portal, sandbox, monkeypatch):
    monkeypatch.setattr(m, "get_settings", lambda: _settings(rfp_harvest_max_files=1))
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    m.execute_harvest(INV)
    hv = _hv(db)
    assert hv["status"] == "failed" and "more attachments" in hv["last_error"] and hv["data"]["title"]
    assert hv["file_count"] == 2 and sandbox.calls == [] and portal.session.downloads == 0
    row = _inv(db)
    assert row["status"] == "split" and row["harvest_id"] == hv["id"] and "more attachments" in row["last_error"]
    monkeypatch.setattr(m, "get_settings", lambda: _settings(rfp_harvest_max_total_bytes=100))
    row.update(status="harvest")
    m.execute_harvest(INV, force=True)
    assert "larger than the harvest accepts" in _hv(db)["last_error"]


def test_execute_harvest_sandbox_cap_and_staging_conflict(db, portal, sandbox):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    sandbox.outcomes["d1.pdf"] = 413
    m.execute_harvest(INV)
    hv = _hv(db)
    assert [f["status"] for f in hv["files"]] == ["skipped_cap", "skipped_cap"] and hv["status"] == "complete"
    assert sandbox.names() == ["create", "add", "delete"]
    db.tables["rfp_portal_invitations"][0].update(status="harvest")
    sandbox.outcomes = {"d1.pdf": 409}
    with pytest.raises(m.RfpPortalTransient):
        m.execute_harvest(INV, force=True)
    assert _hv(db)["status"] == "pending" and _hv(db)["claim_token"] is None


def test_execute_harvest_failure_mappings(db, portal, sandbox, settings):
    until = NOW + timedelta(hours=5)
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    # Locked: the harvest row back to pending, the row parked by the lock remainder, no attempt spent.
    portal.session.errors["event"] = PortalExc("NGEM logins are locked.", "locked", locked_until=until)
    m.execute_harvest(INV)
    hv, row = _hv(db), _inv(db)
    assert hv["status"] == "pending" and hv["last_error"] == "NGEM logins are locked."
    assert row["status"] == "harvest" and row["next_attempt_at"] == m._iso(until) and row["attempts"] == 0
    # Transient: pending for the queue's ladder, the row untouched.
    portal.session.errors["event"] = PortalExc("NGEM answered 503.", "transient")
    row["next_attempt_at"] = None
    with pytest.raises(m.RfpPortalTransient, match="503"):
        m.execute_harvest(INV)
    assert _hv(db)["status"] == "pending" and _inv(db)["status"] == "harvest" and _inv(db)["harvest_id"] is None
    # Permanent: the harvest fails, the row ends at done with the link.
    portal.session.errors["event"] = PortalExc("The account cannot see this bid.", "permanent")
    m.execute_harvest(INV)
    hv, row = _hv(db), _inv(db)
    assert hv["status"] == "failed" and hv["last_error"] == "The account cannot see this bid."
    assert row["status"] == "split" and row["harvest_id"] == hv["id"] and row["last_error"] == hv["last_error"]
    # Manual mode reports the failure instead of moving the status.
    portal.session.errors["event"] = PortalExc("The account cannot see this bid.", "permanent")
    with pytest.raises(m.RfpPortalPermanent):
        m.execute_harvest(INV, force=True)
    assert _inv(db)["status"] == "split"
    # Unexpected: transient with the app sentence.
    portal.session.errors["event"] = KeyError("boom")
    row.update(status="harvest")
    with pytest.raises(m.RfpPortalTransient) as exc:
        m.execute_harvest(INV)
    assert str(exc.value) == m._MSG_INTERRUPTED and _hv(db)["status"] == "pending"
    # An unavailable answer in manual mode is a permanent report.
    portal.session.errors["event"] = PortalExc("not configured", "unavailable")
    row.update(status="done")
    with pytest.raises(m.RfpPortalPermanent, match="not configured"):
        m.execute_harvest(INV, force=True)
    assert _hv(db)["status"] == "failed"


def test_execute_harvest_losing_claim_parks_the_row(db, portal, sandbox, caplog):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    # A live claim: started inside the queue lease.
    db.tables["rfp_harvests"].append(_harvest_row(status="running", claim_token="other",
                                                  started_at=m._iso(NOW - timedelta(seconds=30))))
    with caplog.at_level("WARNING", logger="app.services.rfp_portal_ingest"):
        m.execute_harvest(INV)
    assert any("lost the claim" in r.getMessage() for r in caplog.records)
    row = _inv(db)
    assert row["status"] == "harvest" and row["last_error"] == m._MSG_CLAIMED
    assert row["next_attempt_at"] == m._iso(NOW + timedelta(seconds=60)) and portal.opened == 0
    assert _hv(db)["claim_token"] == "other"
    row.update(status="done")
    with pytest.raises(m.RfpPortalTransient):
        m.execute_harvest(INV, force=True)
    # A claim lost mid-run (another worker took the row) parks the same way.
    db.tables["rfp_harvests"][0].update(status="pending", claim_token=None)
    row.update(status="harvest", next_attempt_at=None)
    portal.session.errors["attachments"] = None

    def steal(facts, max_pages):
        db.tables["rfp_harvests"][0]["claim_token"] = "stolen"
        return []
    portal.session.fetch_attachments = steal
    m.execute_harvest(INV)
    assert _inv(db)["status"] == "harvest" and _inv(db)["last_error"] == m._MSG_CLAIMED


def test_execute_harvest_refreshes_a_stale_view_link_once(db, portal, sandbox):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    fresh = "https://supplier.ionwave.net/VendorResponse/Bid/VResponseEvent.aspx?e=fresh"
    portal.session.errors[("event", VIEW)] = PortalExc("BadRequest", "permanent", stale=True)
    portal.session.page = _page([_grid_row(view_url=fresh)])
    m.execute_harvest(INV)
    assert [c for c in portal.session.calls if c[0] != "download"] == [
        ("event", VIEW), ("invitations", 20), ("event", fresh), ("attachments", ATT, 20),
    ]
    assert _inv(db)["view_url"] == fresh and _inv(db)["status"] == "split" and _hv(db)["status"] == "complete"
    # Not on the grid any more: permanent stale_link.
    db.tables["rfp_portal_invitations"][0].update(status="harvest", view_url=VIEW, harvest_id=None)
    portal.session.calls.clear()
    portal.session.errors[("event", VIEW)] = PortalExc("BadRequest", "permanent", stale=True)
    portal.session.page = _page([])
    m.execute_harvest(INV, force=True)
    row = _inv(db)
    assert row["status"] == "split" and row["flag_reason"] == "stale_link" and _hv(db)["status"] == "failed"
    assert _hv(db)["last_error"] == m._MSG_STALE_LINK
    # The refreshed link answers BadRequest again: still permanent, still
    # flagged stale_link (the flag used to be lost on the second answer).
    db.tables["rfp_portal_invitations"][0].update(status="harvest", view_url=VIEW, harvest_id=None, flag_reason=None)
    portal.session.calls.clear()
    portal.session.errors[("event", VIEW)] = PortalExc("BadRequest", "permanent", stale=True)
    portal.session.errors[("event", fresh)] = PortalExc("BadRequest again", "permanent", stale=True)
    portal.session.page = _page([_grid_row(view_url=fresh)])
    m.execute_harvest(INV)
    row = _inv(db)
    assert row["status"] == "split" and row["flag_reason"] == "stale_link" and row["view_url"] == fresh
    assert _hv(db)["status"] == "failed" and _hv(db)["last_error"] == m._MSG_STALE_LINK_AGAIN
    assert [c[0] for c in portal.session.calls] == ["event", "invitations", "event"]


def test_execute_harvest_takes_over_a_dead_workers_claim(db, portal, sandbox, settings):
    """A worker killed mid-harvest leaves the row `running` under its token;
    once that claim is older than the queue lease the next attempt takes it
    over (a fresh one is never touched), so the invitation no longer parks
    behind the dead claim every minute forever."""
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    stale_at = NOW - timedelta(seconds=settings.llm_queue_lease_seconds + 1)
    db.tables["rfp_harvests"].append(_harvest_row(status="running", claim_token="dead-worker",
                                                  started_at=m._iso(stale_at), files=[]))
    m.execute_harvest(INV)
    hv = _hv(db)
    assert hv["status"] == "complete" and hv["claim_token"] is None and hv["attempts"] == 2
    assert hv["started_at"] == m._iso(NOW) and portal.opened == 1
    assert _inv(db)["status"] == "split" and _inv(db)["harvest_id"] == hv["id"]
    # Exactly at the lease age the claim is still considered live.
    db.tables["rfp_portal_invitations"][0].update(status="harvest", harvest_id=None)
    hv.update(status="running", claim_token="live",
              started_at=m._iso(NOW - timedelta(seconds=settings.llm_queue_lease_seconds)))
    m.execute_harvest(INV)
    assert _hv(db)["claim_token"] == "live" and _inv(db)["last_error"] == m._MSG_CLAIMED


def test_execute_harvest_manual_mode_on_a_moved_row_only_links(db, portal, sandbox):
    """A row that moved to review_match while the job waited (a reopen path)
    is harvested in manual mode: the writes are field-scoped, the status
    never moves."""
    db.tables["rfp_portal_invitations"].append(_invitation(status="review_match"))
    m.execute_harvest(INV)
    row = _inv(db)
    assert row["status"] == "review_match" and row["harvest_id"] == _hv(db)["id"] and _hv(db)["status"] == "complete"


def test_execute_harvest_never_opens_an_ignored_bid_unless_forced(db, portal, sandbox):
    """Ignore means "do not open this bid on the portal": a queued pipeline
    job finding the row ignored does nothing at all; only a forced (manual)
    run may, and then it links field-scoped."""
    db.tables["rfp_portal_invitations"].append(_invitation(status="ignored"))
    m.execute_harvest(INV)
    assert portal.opened == 0 and db.tables["rfp_harvests"] == [] and _inv(db)["harvest_id"] is None
    m.execute_harvest(INV, force=True)
    row = _inv(db)
    assert portal.opened == 1 and row["status"] == "ignored" and row["harvest_id"] == _hv(db)["id"]


def test_execute_harvest_links_when_the_row_was_ignored_mid_job(db, portal, sandbox):
    """The done CAS loses (the row moved off harvest while the files were
    downloading): the harvest still happened, so harvest_id and harvested_at
    are written field-scoped and the new status is left alone."""
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))

    def ignore_meanwhile(facts, max_pages):
        _inv(db).update(status="ignored", ignored_by=U1)
        return list(portal.session.attachments)
    portal.session.fetch_attachments = ignore_meanwhile
    m.execute_harvest(INV)
    row = _inv(db)
    assert row["status"] == "ignored" and row["harvest_id"] == _hv(db)["id"] and row["harvested_at"] == m._iso(NOW)
    assert row["decided_at_step"] is None and _hv(db)["status"] == "complete"


def test_ignore_cancels_a_queued_harvest_job_but_not_a_running_one(db, portal):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    db.tables["llm_jobs"].append({"id": "j-q", "job_type": m.JOB_HARVEST, "target_id": INV, "status": "queued",
                                  "attempts": 0, "created_at": NOW.isoformat()})
    m.ignore(db, INV, None, U1)
    job = db.tables["llm_jobs"][0]
    assert job["status"] == "canceled" and _inv(db)["status"] == "ignored"
    assert db.tables["audit_log"][-1]["payload"]["canceled_job_id"] == "j-q"
    # A running job (a live lease) is left to finish.
    db.tables["rfp_portal_invitations"][0].update(status="harvest", ignored_by=None)
    db.tables["llm_jobs"] = [{"id": "j-r", "job_type": m.JOB_HARVEST, "target_id": INV, "status": "running",
                              "attempts": 1, "claimed_by": "w", "created_at": NOW.isoformat(),
                              "lease_expires_at": m._iso(NOW + timedelta(minutes=10))}]
    m.ignore(db, INV, None, U1)
    assert db.tables["llm_jobs"][0]["status"] == "running" and _inv(db)["status"] == "ignored"
    assert db.tables["audit_log"][-1]["payload"]["canceled_job_id"] is None
    # A cancel that errors never fails the ignore.
    db.tables["rfp_portal_invitations"][0].update(status="match")
    db.tables["llm_jobs"] = [{"id": "j-x", "job_type": m.JOB_HARVEST, "target_id": INV, "status": "queued",
                              "attempts": 0, "created_at": NOW.isoformat()}]
    real_cancel = llm_queue.cancel
    llm_queue.cancel = lambda job_id: (_ for _ in ()).throw(RuntimeError("queue down"))
    try:
        assert m.ignore(db, INV, None, U1)["status"] == "ignored"
    finally:
        llm_queue.cancel = real_cancel


def test_lost_lease_is_never_retried_per_file(db, portal, sandbox, monkeypatch):
    """The renew seam raising _LeaseLost mid-download: no second try, no
    sleep, the harvest row back to pending under the fence, the job told to
    stand down through the transient path."""
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    renewals = {"n": 0}

    def renew():
        renewals["n"] += 1
        if renewals["n"] == 4:   # inside the first download (after the event, the attachments, the loop top)
            raise m._LeaseLost(m._MSG_INTERRUPTED)
    monkeypatch.setattr(m, "_renew", renew)
    real_download = portal.session.download

    def download(url, dest, max_bytes, *, referer=None):
        m._renew()
        return real_download(url, dest, max_bytes, referer=referer)
    portal.session.download = download
    with pytest.raises(m.RfpPortalTransient) as exc:
        m.execute_harvest(INV)
    assert str(exc.value) == m._MSG_INTERRUPTED
    assert portal.session.downloads == 0 and renewals["n"] == 4
    assert _hv(db)["status"] == "pending" and _hv(db)["claim_token"] is None
    assert _inv(db)["status"] == "harvest"


def test_execute_harvest_resumes_past_accepted_files_and_dispatches_a_stranded_run(db, portal, sandbox):
    """An interrupted attempt left two of three files accepted in a staging
    run: the retry downloads only the third. An attempt that started the run
    but died before dispatching it: nothing to download, the run gets its job."""
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    d3 = "https://supplier.ionwave.net/Extract.aspx?e=d3"
    portal.session.attachments = [
        _attachment(1, "Plans.pdf", 300, DL1, "Drawings"), _attachment(2, "Spec.pdf", 200, DL2),
        _attachment(3, "Addendum.pdf", 100, d3),
    ]
    db.tables["rfp_ingest_runs"].append({"id": "run-left", "status": "staging"})
    prior = [
        {"index": 1, "file_name": "Plans.pdf", "description": "Drawings", "size": 300,
         "sandbox_file_id": "f-old-1", "status": "accepted", "error": None},
        {"index": 2, "file_name": "Spec.pdf", "description": None, "size": 999,     # size moved: not the same file
         "sandbox_file_id": "f-old-2", "status": "accepted", "error": None},
        {"index": 3, "file_name": "Addendum.pdf", "description": None, "size": 100,
         "sandbox_file_id": None, "status": "download_failed", "error": "timeout"},
    ]
    db.tables["rfp_harvests"].append(_harvest_row(status="pending", sandbox_run_id="run-left", files=prior,
                                                  finished_at=None))
    m.execute_harvest(INV)
    hv = _hv(db)
    assert hv["status"] == "complete" and hv["sandbox_run_id"] == "run-left" and hv["files_accepted"] == 3
    by_name = {f["file_name"]: f for f in hv["files"]}
    assert by_name["Plans.pdf"]["sandbox_file_id"] == "f-old-1"
    assert by_name["Spec.pdf"]["sandbox_file_id"] != "f-old-2" and by_name["Addendum.pdf"]["status"] == "accepted"
    downloads = [c[1] for c in portal.session.calls if c[0] == "download"]
    assert downloads == [DL2, d3]
    assert "create" not in sandbox.names() and ("start", "run-left") in sandbox.calls
    # A run the last attempt started but never dispatched.
    db.tables["rfp_portal_invitations"][0].update(status="harvest", harvest_id=None)
    db.tables["rfp_ingest_runs"] = [{"id": "run-pend", "status": "pending"}]
    hv.update(status="pending", sandbox_run_id="run-pend", files=[
        {"index": 1, "file_name": "Plans.pdf", "description": "Drawings", "size": 300,
         "sandbox_file_id": "f-p-1", "status": "accepted", "error": None},
        {"index": 2, "file_name": "Spec.pdf", "description": None, "size": 200,
         "sandbox_file_id": "f-p-2", "status": "accepted", "error": None},
        {"index": 3, "file_name": "Addendum.pdf", "description": None, "size": 100,
         "sandbox_file_id": "f-p-3", "status": "accepted", "error": None},
    ])
    portal.session.calls.clear()
    sandbox.calls.clear()
    m.execute_harvest(INV)
    hv = _hv(db)
    assert hv["status"] == "complete" and hv["sandbox_run_id"] == "run-pend" and hv["files_accepted"] == 3
    assert [c[0] for c in portal.session.calls if c[0] == "download"] == []
    assert sandbox.names() == ["dispatch"] and sandbox.calls[0][1] == "run-pend"
    assert _inv(db)["status"] == "split"
    # A run in any other state is not reused: a fresh run, everything downloaded.
    db.tables["rfp_portal_invitations"][0].update(status="harvest", harvest_id=None)
    db.tables["rfp_ingest_runs"] = [{"id": "run-done", "status": "done"}]
    hv.update(status="failed", sandbox_run_id="run-done")
    portal.session.calls.clear()
    sandbox.calls.clear()
    m.execute_harvest(INV)
    assert len([c for c in portal.session.calls if c[0] == "download"]) == 3
    assert sandbox.names()[0] == "create" and _hv(db)["sandbox_run_id"] != "run-done"


def test_execute_harvest_404_is_permanent(db):
    with pytest.raises(m.RfpPortalPermanent) as exc:
        m.execute_harvest("missing")
    assert exc.value.http_status == 404


def test_mark_harvest_from_queue_is_fenced(db):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest"))
    db.tables["rfp_harvests"].append(_harvest_row(status="running", claim_token="t"))
    m.mark_harvest_from_queue(INV, "pending", None)
    assert _hv(db)["status"] == "running" and _inv(db)["status"] == "harvest"
    m.mark_harvest_from_queue(INV, "failed", "The run was interrupted repeatedly.")
    assert _hv(db)["status"] == "failed" and _hv(db)["claim_token"] is None
    row = _inv(db)
    assert row["status"] == "split" and row["harvest_id"] == "hv-1" and row["last_error"] == "The run was interrupted repeatedly."
    # A harvest that finished normally is not overwritten.
    _hv(db).update(status="complete")
    m.mark_harvest_from_queue(INV, "failed", "late")
    assert _hv(db)["status"] == "complete"
    assert m.current_status(INV) == "done" and m.current_status("missing") is None
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", status="harvest"))
    assert m.current_status("inv-2") == "pending"


def test_error_message_uses_the_client_and_service_sentences_only():
    assert m.error_message(m.RfpPortalTransient("The portal did not answer."), "ngem") == "The portal did not answer."
    assert m.error_message(ngc.NgemTransient("NGEM answered 503."), "ngem") == "NGEM answered 503."
    assert m.error_message(ValueError("raw /tmp/x"), "ngem") == m._MSG_INTERRUPTED


def test_ngem_adapter_classifies_the_client_exceptions_and_the_stale_link():
    adapter = m.NgemPortal()
    assert adapter.classify(ngc.NgemLoginLocked("locked", locked_until=NOW)) == m.KIND_LOCKED
    assert adapter.classify(ngc.NgemUnavailable("nope")) == m.KIND_UNAVAILABLE
    assert adapter.classify(ngc.NgemForbidden("no")) == m.KIND_PERMANENT
    assert adapter.classify(ngc.NgemParseError("shape")) == m.KIND_PERMANENT
    assert adapter.classify(ngc.NgemTransient("503")) == m.KIND_TRANSIENT
    assert adapter.classify(ngc.NgemSessionExpired()) == m.KIND_TRANSIENT
    assert adapter.classify(KeyError("x")) == m.KIND_UNKNOWN
    assert adapter.is_stale_link(ngc.NgemForbidden("bad request", reason=ngc.STALE_LINK))
    assert not adapter.is_stale_link(ngc.NgemForbidden("forbidden"))
    s = _settings(ngem_entry_url="https://supplier.ionwave.net//VendorResponse/ResponseList.aspx?e=x")
    assert adapter.config(s).entry_url == "https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=x"
    assert adapter.config(s).password == "pw" and adapter.account(s) == "acct"


def test_availability_reads_the_store_row(db, settings):
    adapter = m.NgemPortal()
    assert adapter.availability(settings) == (True, None, None)
    db.tables["rfp_harvest_sessions"].append({"provider": "ngem", "locked_until": m._iso(NOW + timedelta(hours=1)),
                                              "login_failures": 3})
    usable, reason, until = adapter.availability(settings)
    assert not usable and "locked" in reason and until > NOW
    assert adapter.availability(_settings(ngem_login_password=""))[0] is False


# ── Human actions ────────────────────────────────────────────────────────


def _candidates(*pids):
    return [{"project_id": p, "name": f"P {p[-1]}", "number": "1", "breakdown": {"total": 0.7, "name": 0.7},
             "verdict": None, "confidence": None, "reasoning": None} for p in pids]


def test_resolve_exists_validates_the_project_and_writes_the_resolution(db):
    db.tables["projects"] = [_project(P1, "A"), _project(P3, "C", days=-100)]
    db.tables["rfp_portal_invitations"].append(_invitation(status="review_match", match_project_id=P1,
                                                           match_candidates=_candidates(P1, P2),
                                                           excluded_project_ids=[P2]))
    for bad, code in (("", m.CODE_PROJECT_REQUIRED), ("nope", m.CODE_PROJECT_REQUIRED),
                      (P2, m.CODE_PROJECT_REQUIRED), (P3, m.CODE_PROJECT_REQUIRED)):
        with pytest.raises(m.RfpPortalError) as exc:
            m.resolve_exists(db, INV, bad, U1)
        assert exc.value.code == code
    out = m.resolve_exists(db, INV, P1, U1)
    row = _inv(db)
    assert row["status"] == "exists" and row["resolution"] == "human" and row["resolved_by"] == U1
    assert row["resolved_at"] == m._iso(NOW) and row["match_project_id"] == P1
    assert out["status"] == "exists" and out["match_project"]["id"] == P1 and "view_url" not in out
    assert db.tables["audit_log"][-1]["action"] == "rfp_portal.resolve"
    with pytest.raises(m.RfpPortalError) as exc:
        m.resolve_exists(db, INV, P1, U1)
    assert exc.value.code == m.CODE_NOT_ACTIONABLE
    # Any project inside the window is accepted even when it was not a candidate.
    db.tables["projects"].append(_project(P2, "B"))
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", status="review_match", bid_number="x",
                                                           match_candidates=_candidates(P1)))
    assert m.resolve_exists(db, "inv-2", P2, U1)["match_project_id"] == P2


def test_resolve_new_excludes_every_candidate_and_moves_to_harvest(db, portal):
    db.tables["rfp_portal_invitations"].append(_invitation(status="review_match", match_project_id=P1,
                                                           match_candidates=_candidates(P1, P2), attempts=3,
                                                           last_error="x", next_attempt_at="y"))
    out = m.resolve_new(db, INV, U1)
    row = _inv(db)
    assert row["status"] == "harvest" and row["excluded_project_ids"] == [P1, P2] and row["match_project_id"] is None
    assert row["resolution"] == "human" and row["resolved_by"] == U1 and row["attempts"] == 0
    assert out["harvest_status"] if "harvest_status" in out else True
    assert len(_jobs(db)) == 1 and db.tables["audit_log"][-1]["action"] == "rfp_portal.new"
    with pytest.raises(m.RfpPortalError):
        m.resolve_new(db, INV, U1)


def test_reopen_returns_an_exists_row_to_match_with_the_project_excluded(db):
    db.tables["rfp_portal_invitations"].append(_invitation(status="exists", match_project_id=P1, resolution="system",
                                                           resolved_at="x", attempts=2))
    with pytest.raises(m.RfpPortalError) as exc:
        m.reopen(db, INV, "ab", U1)
    # A bad reason is the caller's input (400), never "the row moved" (409).
    assert exc.value.code == m.CODE_REASON_INVALID and _inv(db)["status"] == "exists"
    out = m.reopen(db, INV, "  wrong project  ", U1)
    row = _inv(db)
    assert row["status"] == "match" and row["excluded_project_ids"] == [P1] and row["match_project_id"] is None
    assert row["reopen_reason"] == "wrong project" and row["resolution"] is None and row["attempts"] == 0
    assert out["status"] == "match" and db.tables["audit_log"][-1]["payload"]["reason"] == "wrong project"
    assert m.portal_sources_for_projects(db, [P1]) == {}
    with pytest.raises(m.RfpPortalError):
        m.reopen(db, INV, None, U1)


@pytest.mark.parametrize("status", ["match", "review_match", "harvest", "create"])
def test_ignore_and_unignore(db, status):
    db.tables["rfp_portal_invitations"].append(_invitation(status=status, excluded_project_ids=[P2], attempts=2,
                                                           next_attempt_at="soon"))
    out = m.ignore(db, INV, None, U1)
    row = _inv(db)
    assert row["status"] == "ignored" and row["ignored_by"] == U1 and row["ignored_at"] == m._iso(NOW)
    assert row["ignore_reason"] is None and row["decided_at_step"] == status and row["next_attempt_at"] is None
    assert out["status"] == "ignored" and out["ignored_by_name"] is None
    with pytest.raises(m.RfpPortalError):
        m.ignore(db, INV, None, U1)
    m.unignore(db, INV, U1)
    row = _inv(db)
    assert row["status"] == "match" and row["ignored_by"] is None and row["excluded_project_ids"] == [P2]
    assert row["attempts"] == 0 and row["next_attempt_at"] is None
    assert [a["action"] for a in db.tables["audit_log"]] == ["rfp_portal.ignore", "rfp_portal.unignore"]


@pytest.mark.parametrize("status", ["exists", "done", "ignored", "created"])
def test_ignore_refuses_terminal_rows(db, status):
    db.tables["rfp_portal_invitations"].append(_invitation(status=status))
    with pytest.raises(m.RfpPortalError) as exc:
        m.ignore(db, INV, "meh", U1)
    assert exc.value.code == m.CODE_NOT_ACTIONABLE


def test_validate_reason_answers_its_own_code():
    for bad in ("a", " ab ", "\t"):
        with pytest.raises(m.RfpPortalError) as exc:
            m._validate_reason(bad, required=True)
        assert exc.value.code == m.CODE_REASON_INVALID and exc.value.http_status is None
    with pytest.raises(m.RfpPortalError) as exc:
        m._validate_reason(None, required=True)
    assert exc.value.code == m.CODE_REASON_INVALID and "required" in str(exc.value)
    assert m._validate_reason(None, required=False) is None and m._validate_reason("  ", required=False) is None
    assert m._validate_reason(" a  b  c ", required=False) == "a b c"
    assert len(m._validate_reason("x" * 900, required=False)) == m._REASON_MAX_CHARS
    assert m.CODE_REASON_INVALID == "rfp_portal_reason_invalid"


def test_harvest_again_guards(db, portal):
    db.tables["rfp_portal_invitations"].append(_invitation(status="exists"))
    with pytest.raises(m.RfpPortalError) as exc:
        m.harvest_again(db, INV, U1)
    assert exc.value.code == m.CODE_NOT_AVAILABLE
    _inv(db)["status"] = "done"
    portal.available = (False, "locked", NOW + timedelta(hours=1))
    with pytest.raises(m.RfpPortalError) as exc:
        m.harvest_again(db, INV, U1)
    assert exc.value.code == m.CODE_LOCKED and exc.value.locked_until == NOW + timedelta(hours=1)
    portal.available = (True, None, None)
    job = m.harvest_again(db, INV, U1)
    assert job["id"] and _jobs(db)[0]["payload"] == {"invitation_id": INV, "force": True}
    assert _jobs(db)[0]["created_by"] == U1 and db.tables["audit_log"][-1]["action"] == "rfp_portal.harvest_again"
    with pytest.raises(m.RfpPortalError) as exc:
        m.harvest_again(db, INV, U1)
    assert exc.value.code == m.CODE_HARVEST_ACTIVE
    _jobs(db)[0]["status"] = "succeeded"
    _inv(db)["status"] = "harvest"
    assert m.harvest_again(db, INV, U1, force=False)["status"] == "queued"
    assert _jobs(db)[-1]["payload"]["force"] is False


def test_run_now_inserts_a_manual_run_and_refuses_while_active_or_locked(db, portal):
    run = m.run_now(db, U1)
    assert run["trigger"] == "manual" and run["requested_by"] == U1 and run["status"] == "queued"
    assert _jobs(db, m.JOB_SCAN)[0]["target_id"] == run["id"] and _jobs(db, m.JOB_SCAN)[0]["created_by"] == U1
    assert db.tables["audit_log"][-1]["action"] == "rfp_portal.run_now"
    with pytest.raises(m.RfpPortalError) as exc:
        m.run_now(db, U1)
    assert exc.value.code == m.CODE_RUN_ACTIVE
    db.tables["rfp_portal_runs"][0]["status"] = "complete"
    portal.available = (False, "locked", NOW)
    with pytest.raises(m.RfpPortalError) as exc:
        m.run_now(db, U1)
    assert exc.value.code == m.CODE_LOCKED


def test_run_now_answers_503_not_available_while_the_slice_is_inactive(db, portal, monkeypatch):
    """The queue that runs the scan is off: nothing is inserted (a queued
    run with no worker would hold the active index for good)."""
    monkeypatch.setattr(m, "get_settings", lambda: _settings(llm_queue_enabled=False))
    with pytest.raises(m.RfpPortalError) as exc:
        m.run_now(db, U1)
    assert exc.value.code == m.CODE_NOT_AVAILABLE and exc.value.http_status == 503
    assert db.tables["rfp_portal_runs"] == [] and db.tables["llm_jobs"] == []
    assert portal.available == (True, None, None)   # never consulted after the gate


def test_run_now_survives_an_insert_that_answers_no_row_and_a_failed_enqueue(db, portal, monkeypatch):
    real_table = db.table

    class Silent(PortalQuery):
        def execute(self):
            out = super().execute()
            if self._op == "insert":
                return SimpleNamespace(data=[])   # a minimal-return PostgREST
            return out

    monkeypatch.setattr(db, "table", lambda name: Silent(db, name) if name == "rfp_portal_runs" else real_table(name))
    run = m.run_now(db, U1)
    assert run["trigger"] == "manual" and run["requested_by"] == U1
    assert _jobs(db, m.JOB_SCAN)[0]["target_id"] == run["id"]
    # An enqueue that fails: the run stays queued (parked one poll out) for
    # the scheduler, and Run now still answers it rather than a 500.
    db.tables["rfp_portal_runs"].clear()
    db.tables["llm_jobs"].clear()
    monkeypatch.setattr(db, "table", real_table)
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue down")))
    run = m.run_now(db, U1)
    assert run["status"] == "queued" and "queue down" in run["last_error"] and run["next_attempt_at"]
    assert db.tables["audit_log"][-1]["payload"]["job_id"] is None


# ── Reads ────────────────────────────────────────────────────────────────


def test_detail_shape_and_secrets(db, portal):
    db.tables["profiles"] = [{"id": U1, "full_name": "Ann Admin"}]
    db.tables["projects"] = [_project(P1, "Live name"), _project(P2, "Old")]
    db.tables["rfp_portal_invitations"].append(_invitation(
        status="exists", match_project_id=P1, match_candidates=_candidates(P1), excluded_project_ids=[P2],
        possible_rebid_project_id=P2, possible_rebid_score=0.9, resolution="human", resolved_by=U1,
        harvest_id="hv-1",
    ))
    db.tables["rfp_harvests"].append(_harvest_row(claim_token="secret", raw={"x": 1}))
    db.tables["llm_jobs"].append({"id": "j1", "job_type": m.JOB_HARVEST, "target_id": INV, "status": "queued",
                                  "attempts": 0, "max_attempts": 3, "priority": 150, "created_at": NOW.isoformat()})
    out = m.detail(db, INV)
    assert "view_url" not in out and VIEW not in str(out)
    assert out["match_candidates"][0]["name"] == "Live name" and out["match_project"] == {"id": P1, "name": "Live name", "number": "26.9.7200"}
    assert out["excluded_projects"] == [{"id": P2, "name": "Old", "number": "26.9.7200"}]
    assert out["possible_rebid"] == {"id": P2, "name": "Old", "number": "26.9.7200", "score": 0.9}
    assert out["resolved_by_name"] == "Ann Admin" and out["ignored_by_name"] is None
    assert out["harvest"]["id"] == "hv-1" and "claim_token" not in out["harvest"] and "raw" not in out["harvest"]
    assert out["harvest_job"]["state"] == "queued"
    assert out["harvest_available"] == {"ok": False, "reason": "The invitation is not at a stage that can be harvested."}
    assert m.detail(db, "missing") is None


def test_list_invitations_views_search_and_counts(db):
    db.tables["projects"] = [_project(P1, "Proj")]
    db.tables["rfp_harvests"].append(_harvest_row(status="failed"))
    log_recent = [{"at": m._iso(NOW - timedelta(days=1)), "field": "title", "old": "a", "new": "b", "run_id": "r"}]
    log_old = [{"at": m._iso(NOW - timedelta(days=30)), "field": "title", "old": "a", "new": "b", "run_id": "r"}]
    db.tables["rfp_portal_invitations"].extend([
        _invitation(status="review_match", match_project_id=P1, match_score=0.7, change_log=log_recent),
        _invitation(id="inv-2", bid_number="1790", bid_number_raw="1790", agency="Clark County", agency_key="clark-county",
                    title="Fire Station", status="harvest", close_at=m._iso(NOW + timedelta(days=3)), change_log=log_old),
        _invitation(id="inv-3", bid_number="1791", bid_number_raw="1791", agency="Clark County", agency_key="clark-county",
                    title="Library", status="done", harvest_id="hv-1", close_at=m._iso(NOW + timedelta(days=2))),
        _invitation(id="inv-4", bid_number="1792", bid_number_raw="1792", agency="Henderson", agency_key="henderson",
                    title="Park", status="exists", match_project_id=P1),
        _invitation(id="inv-5", bid_number="1793", bid_number_raw="1793", agency="Henderson", agency_key="henderson",
                    title="Pool", status="ignored"),
    ])
    out = m.list_invitations(db, portal="ngem", view="new", q=None, limit=50, offset=0)
    assert [r["id"] for r in out["items"]] == ["inv-2", "inv-3"] and out["total"] == 2
    assert out["counts"] == {"review": 1, "new": 2, "existing": 1, "ignored": 1, "all": 5}
    by_id = {r["id"]: r for r in out["items"]}
    assert by_id["inv-2"]["harvest_status"] == "pending" and by_id["inv-3"]["harvest_status"] == "failed"
    assert by_id["inv-2"]["changed_recently"] is False
    review = m.list_invitations(db, portal="ngem", view="review", q=None, limit=50, offset=0)["items"][0]
    assert review["match_project"] == {"id": P1, "name": "Proj", "number": "26.9.7200"} and review["match_score"] == 0.7
    assert review["changed_recently"] is True and "view_url" not in review
    hits = m.list_invitations(db, portal="ngem", view="all", q="fire", limit=50, offset=0)
    assert [r["id"] for r in hits["items"]] == ["inv-2"]
    hits = m.list_invitations(db, portal="ngem", view="all", q="Hender,(son)", limit=1, offset=1)
    assert hits["total"] == 2 and len(hits["items"]) == 1
    assert m.list_invitations(db, portal="ngem", view="all", q=None, limit=5000, offset=0)["limit"] == 200


def test_list_counts_stay_exact_under_a_max_rows_cap_from_one_paged_status_read(db, monkeypatch):
    """PostgREST caps the rows it transfers (max-rows). The counts read only
    the status column in pages that advance by the rows actually received,
    against the exact total the first page asks for, so a fake that hands
    back at most two rows per select still counts every row exactly (7 rows:
    pages of 2, 2, 2, 1). No HEAD count per lane any more."""
    db.tables["rfp_portal_invitations"].extend([
        _invitation(id=f"inv-{i}", bid_number=str(1800 + i), bid_number_raw=str(1800 + i),
                    status=["match", "harvest", "done", "exists", "ignored", "review_match", "match"][i])
        for i in range(7)
    ])
    real_table = db.table
    selects = []

    class Capped(PortalQuery):
        def select(self, *a, **k):
            selects.append((a[0] if a else None, k.get("head"), k.get("count")))
            return super().select(*a, **k)

        def execute(self):
            out = super().execute()
            if self._op == "select":
                out.data = out.data[:2]
            return out

    monkeypatch.setattr(db, "table", lambda name: Capped(db, name) if name == "rfp_portal_invitations" else real_table(name))
    out = m.list_invitations(db, portal="ngem", view="all", q=None, limit=50, offset=0)
    assert out["counts"] == {"review": 1, "new": 4, "existing": 1, "ignored": 1, "all": 7} and out["total"] == 7
    assert selects[0][1] is None and selects[1:] == [("status", None, "exact")] + [("status", None, None)] * 3
    assert m.view_counts(db, "ngem")["all"] == 7 and m.view_counts(db, "other") == {n: 0 for n in m.VIEWS}


def test_detail_read_path_never_hands_out_a_download_url_even_if_a_row_carried_one(db):
    """The real read path over a row that (against the doc) carries an
    Extract.aspx link inside harvest.files and a view_url: neither reaches
    the caller."""
    dl = "https://supplier.ionwave.net/Extract.aspx?e=SECRET-FILE"
    db.tables["rfp_portal_invitations"].append(_invitation(status="done", harvest_id="hv-1", view_url=VIEW))
    db.tables["rfp_harvests"].append(_harvest_row(files=[
        {"index": 1, "file_name": "Plans.pdf", "description": None, "size": 300, "sandbox_file_id": "f-1",
         "status": "accepted", "error": None, "download_url": dl, "url": dl},
        "junk",
    ], claim_token="secret", raw={"x": dl}))
    out = m.detail(db, INV)
    text = str(out)
    assert "Extract.aspx" not in text and "SECRET" not in text and VIEW not in text
    assert out["harvest"]["files"] == [{"index": 1, "file_name": "Plans.pdf", "description": None, "size": 300,
                                        "sandbox_file_id": "f-1", "status": "accepted", "error": None}]
    assert "claim_token" not in out["harvest"] and "raw" not in out["harvest"]


def test_session_status_never_carries_cookies_or_the_password(db, settings):
    db.tables["profiles"] = [{"id": U1, "full_name": "Ann Admin"}]
    db.tables["rfp_harvest_sessions"].append({"provider": "ngem", "account": "acct", "cookies": [{"name": "procData", "value": "SECRET"}],
                                              "logged_in_at": "t1", "last_used_at": "t2", "last_login_attempt_at": "t3",
                                              "login_failures": 0, "locked_until": None, "last_error": None})
    db.tables["rfp_portal_runs"].extend([_run(id="r-old", status="complete", created_at=m._iso(NOW - timedelta(days=1))),
                                         _run(id="r-new", status="queued", trigger="manual", scheduled_for=None,
                                              requested_by=U1, created_at=m._iso(NOW))])
    db.tables["llm_jobs"].append({"id": "j", "job_type": m.JOB_SCAN, "target_id": "r-new", "status": "queued", "attempts": 0})
    out = m.session_status()
    assert out["enabled"] is True and out["configured"] is True and out["account"] == "acct"
    assert out["logged_in_at"] == "t1" and out["login_failures"] == 0 and out["locked_until"] is None
    assert out["schedule_times"] == ["06:30", "12:00"] and out["next_run_at"] == m._iso(datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc))
    assert out["last_run"]["id"] == "r-new" and out["last_run"]["requested_by_name"] == "Ann Admin"
    assert out["active_run"]["id"] == "r-new" and out["active_jobs"] == 1
    text = str(out)
    assert "SECRET" not in text and "cookies" not in text and "pw" not in text.split()


def test_session_store_records_logins_and_locks(db, settings, monkeypatch):
    store = m.SessionStore(_settings(ngem_login_max_failures=2, ngem_login_lock_seconds=100))
    assert store.load() is None
    store.save_cookies("acct", [{"name": "procData", "value": "v"}])
    assert store.load()["account"] == "acct" and store.load()["login_failures"] == 0
    state = store.record_login(ok=False, error="bad password page")
    assert state["login_failures"] == 1 and state.get("locked_until") is None
    state = store.record_login(ok=False, error="again")
    assert state["login_failures"] == 2 and state["locked_until"] == m._iso(NOW + timedelta(seconds=100))
    state = store.record_login(ok=True, error=None)
    assert state["login_failures"] == 0 and state["locked_until"] is None
    store.touch()
    assert store.load()["last_used_at"] == m._iso(NOW)


def test_notify_lock_rings_it_admins_once(db, settings):
    m._notify_lock(NOW + timedelta(hours=6), "credentials rejected")
    m._notify_lock(NOW + timedelta(hours=6), "credentials rejected")
    bells = _bells(db, m.NOTIFY_LOGIN_FAILED)
    assert len(bells) == 1 and bells[0]["role"] == Role.IT_ADMIN and bells[0]["mirror_email"] is False
    assert "NGEM login failed 3 times" in bells[0]["message"] and "NGEM_LOGIN_USERNAME" in bells[0]["message"]
    assert "pw" not in bells[0]["message"] and bells[0]["metadata"]["error"] == "credentials rejected"


def test_normalize_entry_url_collapses_slashes_in_the_path_only():
    base = "https://supplier.ionwave.net"
    assert m.normalize_entry_url(f"{base}//VendorResponse/ResponseList.aspx?e=abc") == (
        f"{base}/VendorResponse/ResponseList.aspx?e=abc"
    )
    assert m.normalize_entry_url(f"  {base}///VendorResponse//ResponseList.aspx?e=abc  ") == (
        f"{base}/VendorResponse/ResponseList.aspx?e=abc"
    )
    # The entry token is opaque: slashes inside it are never touched.
    token = "aB//cd==//x"
    assert m.normalize_entry_url(f"{base}//VendorResponse/ResponseList.aspx?e={token}") == (
        f"{base}/VendorResponse/ResponseList.aspx?e={token}"
    )
    assert m.normalize_entry_url(f"{base}/VendorResponse/ResponseList.aspx?e=a//b&f=c//d#x//y") == (
        f"{base}/VendorResponse/ResponseList.aspx?e=a//b&f=c//d#x//y"
    )
    assert m.normalize_entry_url("not a url//x") == "not a url//x"
    assert m.normalize_entry_url("") is None and m.normalize_entry_url(None) is None


def test_pure_helpers():
    assert m.agency_key("UNLV (NSHE-Business Center South)") == "unlv-nshe-business-center-south"
    assert m.agency_key("") == "unknown-agency" == m.agency_key(m._UNKNOWN_AGENCY) == m.agency_key(None)
    assert m.agency_name("  Clark   County ") == "Clark County" and m.agency_name("") == m._UNKNOWN_AGENCY
    assert m.safe_filename("../../etc/passwd") == "passwd" and m.safe_filename("..") == "attachment"
    assert m.safe_filename("C:\\docs\\Plan Set.pdf") == "Plan Set.pdf"
    assert m.sanitize_notes("<p onclick='x'>Hi <a href='javascript:alert(1)'>j</a><a href='https://a.b'>ok</a></p>") == (
        '<p>Hi <a rel="noopener noreferrer">j</a><a href="https://a.b" rel="noopener noreferrer">ok</a></p>'
    )
    assert m.sanitize_notes("   ") is None and m.sanitize_notes("<b>" + "x" * 30_000) is not None
    assert m.normalize_entry_url(None) is None
    entries, urls = m.attachment_entries([_attachment(3, "a.pdf", None, DL1), SimpleNamespace(index=None, file_name="", size_bytes=-5, description=None, download_url="")])
    assert entries[0]["size"] == 0 and entries[1]["file_name"] == "attachment-2" and entries[1]["index"] == 2 and urls == [DL1, None]



# ── Create step (docs/RFP_CREATE.md 3) ───────────────────────────────────


def test_status_vocabulary_gains_create_and_created():
    assert m.STATUS_PENDING == ("match", "harvest", "split", "create")
    assert "created" in m.STATUS_TERMINAL and "done" in m.STATUS_TERMINAL
    # The 0132 nine are always there; the parked trio (0136,
    # docs/RFP_BUILDINGCONNECTED.md 5) are in the vocabulary only once the
    # module defines them, and then nothing else is.
    base = {"match", "harvest", "split", "create", "review_match", "exists", "done", "created", "ignored"}
    parked = {
        getattr(m, name, None)
        for name in ("STATUS_HISTORICAL", "STATUS_EXPIRED", "STATUS_WITHDRAWN")
    } - {None}
    assert base <= set(m.ALL_STATUSES)
    assert parked <= set(m.ALL_STATUSES)
    assert set(m.ALL_STATUSES) == base | parked
    assert parked <= {"historical", "expired", "withdrawn"}
    # `split` (docs/RFP_SPLIT.md 2) sits where `create` did: pending, ignorable, in the "new" view.
    assert "split" in m.IGNORABLE_STATUSES and "split" in m.VIEWS["new"] and "split" not in m.HARVEST_AGAIN_STATUSES
    assert "create" in m.VIEWS["new"] and "created" in m.VIEWS["existing"]
    assert "created" not in m.IGNORABLE_STATUSES and "created" not in m.HARVEST_AGAIN_STATUSES
    # A row waiting at `create` (for the flag, a claim or a retry) may be ignored.
    assert "create" in m.IGNORABLE_STATUSES and "create" not in m.HARVEST_AGAIN_STATUSES
    assert "created_project_id" in m._SWEEP_SELECT and "created_project_id" in m._DETAIL_SELECT
    assert "created_project_id" in m._LIST_SELECT


@pytest.fixture
def create_stub(monkeypatch):
    from app.services import rfp_create

    state = {"calls": [], "raise": None}

    def fake(sb, inv, *, actor_id, automatic):
        state["calls"].append((inv["id"], actor_id, automatic))
        if state["raise"] is not None:
            raise state["raise"]
        return rfp_create.Created(project_id="p-new", number="26.9.7204", linked=False, files_job=True)

    monkeypatch.setattr(m.rfp_create, "create_from_portal", fake)
    return state


def test_step_create_drains_to_done_while_automatic_creation_is_off(db, settings, create_stub):
    db.tables["rfp_portal_invitations"].append(_invitation(status="create", flag_reason="no_candidate",
                                                           attempts=2, last_error="kept"))
    m._process_invitation(db, dict(_inv(db)), m._SweepState(), settings, llm_down=False, renew=lambda: True)
    row = _inv(db)
    assert row["status"] == "done" and row["decided_at_step"] == "create"
    assert row["flag_reason"] == "no_candidate" and row["next_attempt_at"] is None
    assert row["attempts"] == 2 and row["last_error"] == "kept" and create_stub["calls"] == []
    # A row with no title drains the same way with the switch on.
    monkeypatch_settings = _settings(rfp_create_auto_enabled=True)
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", bid_number="X-2", status="create", title="  "))
    m._step_create(db, dict(_inv(db, "inv-2")), monkeypatch_settings)
    assert _inv(db, "inv-2")["status"] == "done" and create_stub["calls"] == []


def test_step_create_links_a_row_whose_harvest_already_has_a_project(db, settings, create_stub):
    """The link-only path (RFP_CREATE.md 3): an addendum notice or a second
    sighting whose harvest was already turned into a project joins it, with
    automatic creation off (no drain) and on (no service call)."""
    db.tables.setdefault("rfp_harvests", []).extend([{"id": "hv-1", "project_id": "p-old"},
                                                     {"id": "hv-2", "project_id": None}])
    db.tables["rfp_portal_invitations"].append(_invitation(status="create", harvest_id="hv-1", title="  "))
    m._step_create(db, dict(_inv(db)), settings)
    row = _inv(db)
    assert row["status"] == "created" and row["created_project_id"] == "p-old"
    assert row["decided_at_step"] == "create" and row["next_attempt_at"] is None and create_stub["calls"] == []
    on = _settings(rfp_create_auto_enabled=True)
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", bid_number="X-2", status="create",
                                                           harvest_id="hv-1"))
    m._step_create(db, dict(_inv(db, "inv-2")), on)
    assert _inv(db, "inv-2")["status"] == "created" and create_stub["calls"] == []
    # A harvest with no project yet, or none at all: the ordinary path.
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-3", bid_number="X-3", status="create",
                                                           harvest_id="hv-2"))
    m._step_create(db, dict(_inv(db, "inv-3")), on)
    assert create_stub["calls"] == [("inv-3", None, True)]
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-4", bid_number="X-4", status="create",
                                                           harvest_id="hv-2"))
    m._step_create(db, dict(_inv(db, "inv-4")), settings)
    assert _inv(db, "inv-4")["status"] == "done"


def test_step_create_hands_the_row_to_rfp_create_and_maps_its_outcomes(db, create_stub):
    from app.services import rfp_create

    on = _settings(rfp_create_auto_enabled=True)
    db.tables["rfp_portal_invitations"].append(_invitation(status="create", attempts=1))
    m._step_create(db, dict(_inv(db)), on)
    assert create_stub["calls"] == [(INV, None, True)] and _inv(db)["status"] == "create"
    # A losing claim waits without spending an attempt.
    create_stub["raise"] = rfp_create.CreateInProgress("busy")
    m._step_create(db, dict(_inv(db)), on)
    row = _inv(db)
    assert row["status"] == "create" and row["attempts"] == 1 and row["last_error"] == "busy"
    assert m._parse_ts(row["next_attempt_at"]) == NOW + timedelta(seconds=on.rfp_create_poll_seconds)
    # A refusal drains with the sentence.
    create_stub["raise"] = rfp_create.CreateRefused("Already created.")
    m._step_create(db, dict(_inv(db)), on)
    row = _inv(db)
    assert row["status"] == "done" and row["decided_at_step"] == "create" and row["last_error"] == "Already created."
    # Anything else walks the ladder, and at the cap parks for an hour, never lost.
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", bid_number="X-2", status="create"))
    create_stub["raise"] = RuntimeError("PostgREST 502")
    m._step_create(db, dict(_inv(db, "inv-2")), on)
    row = _inv(db, "inv-2")
    assert row["status"] == "create" and row["attempts"] == 1 and row["last_error"] == "PostgREST 502"
    assert m._parse_ts(row["next_attempt_at"]) == NOW + timedelta(seconds=rfp_email_ingest.backoff_seconds(1))
    row["attempts"] = on.rfp_email_ingestion_classify_max_attempts - 1
    m._step_create(db, dict(row), on)
    row = _inv(db, "inv-2")
    assert row["status"] == "create" and m._parse_ts(row["next_attempt_at"]) == NOW + timedelta(seconds=3600)


def test_sweep_picks_up_a_due_create_row(db, create_stub, monkeypatch):
    monkeypatch.setattr(m, "get_settings", lambda: _settings(rfp_create_auto_enabled=True))
    db.tables["rfp_portal_invitations"].append(_invitation(status="create"))
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", bid_number="X-2", status="create",
                                                           next_attempt_at=m._iso(NOW + timedelta(minutes=5))))
    m.sweep(db)
    assert [c[0] for c in create_stub["calls"]] == [INV]


def test_finish_invitation_targets_create_on_success_and_permanent_failure(db):
    db.tables["rfp_portal_invitations"].append(_invitation(status="harvest", attempts=2))
    assert m._finish_invitation(db, INV, "hv-1", error=None)
    row = _inv(db)
    assert row["status"] == "split" and row["harvest_id"] == "hv-1" and row["attempts"] == 0
    assert row["decided_at_step"] == "harvest" and row["next_attempt_at"] is None
    row.update(status="harvest")
    assert m._finish_invitation(db, INV, "hv-1", error="broken", flag_reason="stale_link")
    assert _inv(db)["status"] == "split" and _inv(db)["last_error"] == "broken"
    assert _inv(db)["flag_reason"] == "stale_link"
    # Off `harvest` the CAS loses.
    assert not m._finish_invitation(db, INV, "hv-1", error=None)


def test_created_rows_refuse_every_human_action(db):
    db.tables["rfp_portal_invitations"].append(_invitation(status="created", created_project_id="p1"))
    for action in (
        lambda: m.resolve_new(db, INV, U1),
        lambda: m.reopen(db, INV, None, U1),
        lambda: m.ignore(db, INV, None, U1),
        lambda: m.unignore(db, INV, U1),
        lambda: m.resolve_exists(db, INV, P1, U1),
        lambda: m.harvest_again(db, INV, U1),
    ):
        with pytest.raises(m.RfpPortalError):
            action()
    assert _inv(db)["status"] == "created"


def test_detail_carries_the_created_project_and_create_available(db, monkeypatch):
    monkeypatch.setattr(llm_queue, "poll_info", lambda *a, **k: None)
    db.tables["projects"].append(_project("p1", "Emergency Phone Tower", number="26.9.7204"))
    db.tables["rfp_portal_invitations"].append(_invitation(status="created", created_project_id="p1"))
    out = m.detail(db, INV)
    assert out["created_project"] == {"id": "p1", "name": "Emergency Phone Tower", "number": "26.9.7204"}
    assert out["create_available"] is False
    db.tables["rfp_portal_invitations"].append(_invitation(id="inv-2", bid_number="X-2", status="done"))
    out = m.detail(db, "inv-2")
    assert out["created_project"] is None and out["create_available"] is True
    listed = m.list_invitations(db, portal="ngem", view="existing", q=None, limit=10, offset=0)
    assert [r["id"] for r in listed["items"]] == [INV]
    assert listed["items"][0]["created_project"]["number"] == "26.9.7204"
    assert listed["counts"]["existing"] == 1 and listed["counts"]["new"] == 1


def test_create_project_by_hand_audits_and_returns_the_created(db, create_stub):
    db.tables["rfp_portal_invitations"].append(_invitation(status="done"))
    made = m.create_project(db, INV, U1)
    assert (made.project_id, made.number, made.linked, made.files_job) == ("p-new", "26.9.7204", False, True)
    assert create_stub["calls"] == [(INV, U1, False)]
    entry = db.tables["audit_log"][-1]
    assert entry["action"] == "rfp_portal.create" and entry["actor_id"] == U1
    assert entry["payload"] == {"project_id": "p-new", "number": "26.9.7204", "linked": False,
                                "from_status": "done"}
