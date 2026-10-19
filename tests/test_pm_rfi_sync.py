"""PM RFI sync from the GC's Procore (services/pm_rfi_sync, docs/PM_RFI_SYNC.md).

Driven by trimmed real captures (tests/fixtures_pm_rfi): the RFI list of
Procore project 3167322 (company 6333) cut to five rows (Const-198 closed,
Const-189.1 open, Const-97 r0 closed_with_revision and r1 closed, Int-07
open), two real details (Const-198, Const-189.1) and three real Procore
notification emails in the Graph text form the ingestion stores. Nothing
reaches Procore or Supabase: the session is a fake that serves the fixtures
and records every call, the database an in-memory fake with the 0156 unique
indexes.

Pinned: project URL parsing (app + us02, safelinks, webclient), email RFI
link extraction (the Const-198 email triggers, the Boyd Martin bid emails do
not), list -> upsert dedupe (a second identical run fetches nothing and
creates nothing), closed rows never re-fetched or reopened, revisions as
separate rows, auto-link to G3's RFI by attachment filename and by subject,
the backfill's single in-app notice and the post-backfill summary, the
read-only 409s, the 06:30 Pacific slot across DST and its ledger, the email
scan (auto-link + one run per email), leases, attachments (oversize
skipped and recorded) and manual link / unlink.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.routers import pm_field
from app.services import pm as pm_svc
from app.services import pm_rfi_sync as svc
from app.services import procore_client as pc

FIX = Path(__file__).parent / "fixtures_pm_rfi"
PROJECT = "p1"
PROCORE_PROJECT = "3167322"
COMPANY = "6333"


def _json(name: str):
    return json.loads((FIX / name).read_text())


def _text(name: str) -> str:
    return (FIX / name).read_text()


LIST = _json("list.json")
DETAILS = {
    "27914290": _json("detail_27914290.json"),
    "27699412": _json("detail_27699412.json"),
}


# ── Fake Supabase ─────────────────────────────────────────────────────────────


def _ts(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _cmp_val(value):
    return _ts(value) or value


def _like_re(pattern: str, flags=0):
    out = ""
    for ch in pattern:
        out += ".*" if ch in "*%" else re.escape(ch)
    return re.compile(f"^{out}$", flags | re.DOTALL)


class _Q:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.filters, self.orders = None, None, [], []
        self.lim, self.on_conflict = None, None

    def select(self, *a, **k):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, **k):
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, c, v):
        self.filters.append(lambda r: r.get(c) == v if not isinstance(v, str) or _ts(v) is None
                            else (_ts(r.get(c)) == _ts(v) if _ts(r.get(c)) else r.get(c) == v))
        return self

    def in_(self, c, vals):
        vals = list(vals)
        self.filters.append(lambda r: r.get(c) in vals)
        return self

    def gte(self, c, v):
        self.filters.append(lambda r: r.get(c) is not None and _cmp_val(r.get(c)) >= _cmp_val(v))
        return self

    def lte(self, c, v):
        self.filters.append(lambda r: r.get(c) is not None and _cmp_val(r.get(c)) <= _cmp_val(v))
        return self

    def is_(self, c, v):
        self.filters.append(lambda r: r.get(c) is None)
        return self

    @property
    def not_(self):
        q = self

        class _Not:
            def is_(self, c, v):
                q.filters.append(lambda r: r.get(c) is not None)
                return q

        return _Not()

    def ilike(self, c, pat):
        rx = _like_re(pat, re.IGNORECASE)
        self.filters.append(lambda r: bool(rx.match(str(r.get(c) or ""))))
        return self

    def like(self, c, pat):
        rx = _like_re(pat)
        self.filters.append(lambda r: bool(rx.match(str(r.get(c) or ""))))
        return self

    def order(self, c, desc=False, **k):
        self.orders.append((c, desc))
        return self

    def limit(self, n, *a, **k):
        self.lim = n
        return self

    def _match(self, r):
        return all(f(r) for f in self.filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "select":
            hits = [dict(r) for r in rows if self._match(r)]
            for c, desc in reversed(self.orders):
                hits.sort(key=lambda r: (r.get(c) is None, str(r.get(c) or "")), reverse=desc)
            if self.lim is not None:
                hits = hits[: self.lim]
            return SimpleNamespace(data=hits)
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for p in items:
                row = {"id": self.db.next_id(), "created_at": self.db.stamp(), **p}
                self.db.check_unique(self.table, row)
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self.op == "upsert":
            key = self.on_conflict
            for r in rows:
                if r.get(key) == self.payload.get(key):
                    merged = {**r, **self.payload}
                    self.db.check_unique(self.table, merged, ignore=r)
                    r.update(self.payload)
                    return SimpleNamespace(data=[dict(r)])
            row = {"id": self.db.next_id(), "created_at": self.db.stamp(), **self.payload}
            self.db.check_unique(self.table, row)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self.op == "update":
            out = []
            for r in rows:
                if self._match(r):
                    self.db.check_unique(self.table, {**r, **self.payload}, ignore=r)
                    r.update(self.payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self.op == "delete":
            keep = [r for r in rows if not self._match(r)]
            removed = [r for r in rows if self._match(r)]
            self.db.tables[self.table] = keep
            if self.table == "rfis":
                ids = {r["id"] for r in removed}
                self.db.tables["rfi_attachments"] = [
                    a for a in self.db.tables.get("rfi_attachments", []) if a["rfi_id"] not in ids
                ]
            return SimpleNamespace(data=removed)
        raise AssertionError(self.op)


# (table, index name, predicate, key columns): the 0156 unique indexes.
_UNIQUE = [
    ("rfis", "rfis_project_rfi_number_uidx", lambda r: r.get("rfi_number") is not None,
     ("project_id", "rfi_number")),
    ("rfis", "rfis_project_external_id_uidx", lambda r: r.get("external_id") is not None,
     ("project_id", "external_id")),
    ("rfis", "rfis_project_external_number_rev_uidx", lambda r: r.get("source") == "procore",
     ("project_id", "external_number", "external_revision")),
    ("rfi_attachments", "rfi_attachments_external_file_uidx",
     lambda r: r.get("external_file_id") is not None, ("rfi_id", "external_file_id")),
    ("pm_external_links", "pm_external_links_project_id_key", lambda r: True, ("project_id",)),
    ("pm_external_links", "pm_external_links_procore_project_uidx",
     lambda r: r.get("platform") == "procore", ("platform", "host", "external_project_id")),
    ("pm_rfi_sync_runs", "pm_rfi_sync_runs_daily_uidx", lambda r: r.get("trigger") == "daily",
     ("project_id", "trigger", "scheduled_for")),
    ("pm_rfi_sync_runs", "pm_rfi_sync_runs_email_uidx",
     lambda r: r.get("trigger") == "email" and r.get("source_email_id"), ("source_email_id", "trigger")),
    ("pm_rfi_email_scan_state", "pm_rfi_email_scan_state_pkey", lambda r: True, ("id",)),
]


class FakeDB:
    def __init__(self, **tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self._seq = 0
        self._clock = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

    def next_id(self):
        self._seq += 1
        return f"gen{self._seq}"

    def stamp(self):
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat()

    def table(self, name):
        return _Q(self, name)

    def check_unique(self, table, row, ignore=None):
        for t, name, pred, cols in _UNIQUE:
            if t != table or not pred(row):
                continue
            key = tuple(str(_ts(row.get(c)) or row.get(c)) for c in cols)
            for other in self.tables.get(table, []):
                if other is ignore or other.get("id") == row.get("id"):
                    continue
                if pred(other) and tuple(str(_ts(other.get(c)) or other.get(c)) for c in cols) == key:
                    raise Exception(f'duplicate key value violates unique constraint "{name}" (23505)')

    def rows(self, table, **eq):
        return [r for r in self.tables.get(table, []) if all(r.get(k) == v for k, v in eq.items())]


# ── Fake Procore session ──────────────────────────────────────────────────────


def _detail_from_list(row: dict) -> dict:
    """A detail for list rows with no captured detail: the list row plus one
    plain question (no attachments)."""
    d = copy.deepcopy(row)
    d["questions"] = [{"id": 1, "plain_text_body": f"Question for {row['full_number']}",
                       "rich_text_body": None, "attachments": [], "answers": []}]
    d["draft"] = False
    return d


class FakeSession:
    def __init__(self, rows=None, details=None, *, file_bytes=b"%PDF-1.7 test"):
        self.rows = copy.deepcopy(rows if rows is not None else LIST)
        self.details = copy.deepcopy(details if details is not None else DETAILS)
        self.calls: list[tuple] = []
        self.file_bytes = file_bytes
        self.oversize: set[str] = set()
        self.closed = False

    def list_rfis(self, project_id, company_id):
        self.calls.append(("list", project_id, company_id))
        return copy.deepcopy(self.rows)

    def get_rfi(self, project_id, rfi_id, company_id):
        self.calls.append(("detail", rfi_id))
        if rfi_id in self.details:
            return copy.deepcopy(self.details[rfi_id])
        for r in self.rows:
            if str(r["id"]) == rfi_id:
                return _detail_from_list(r)
        raise pc.ProcoreForbidden("gone")

    def resolve_company_id(self, project_id):
        self.calls.append(("resolve", project_id))
        return COMPANY

    def download(self, url, dest, *, max_bytes):
        self.calls.append(("download", url))
        if any(key in url for key in self.oversize):
            raise pc.ProcoreForbidden("The bid document is larger than the harvest accepts.")
        dest.write_bytes(self.file_bytes)
        return len(self.file_bytes)

    def close(self):
        self.closed = True

    def details_fetched(self):
        return [c[1] for c in self.calls if c[0] == "detail"]


# ── Setup ─────────────────────────────────────────────────────────────────────


def _settings(**over) -> Settings:
    base = dict(
        supabase_url="", pm_enabled=True, pm_rfi_sync_enabled=True,
        procore_login_email="bot@example.com", procore_login_password="pw",
        notification_emails_enabled=False,
    )
    base.update(over)
    return Settings(**base)


SETTINGS = None


@pytest.fixture(autouse=True)
def _storage(monkeypatch):
    from app.services import storage

    uploads = []
    monkeypatch.setattr(storage, "upload_file", lambda path, content, ctype, **k: uploads.append(path))
    monkeypatch.setattr(storage, "build_object_path",
                        lambda pid, cat, name: f"{pid}/{cat}/x-{name}")
    return uploads


def _db(**tables) -> FakeDB:
    base = {
        "projects": [
            {"id": PROJECT, "name": "Lake Mead", "pm_stage": "construction"},
            {"id": "p2", "name": "Other", "pm_stage": "construction"},
            {"id": "bid", "name": "Bid only", "pm_stage": None},
        ],
        "pm_external_links": [_link()],
        "rfis": [],
        "rfi_attachments": [],
        "pm_documents": [],
        "pm_rfi_sync_runs": [],
    }
    base.update(tables)
    return FakeDB(**base)


def _link(**over) -> dict:
    row = {
        "id": "l1", "project_id": PROJECT, "platform": "procore", "host": "app.procore.com",
        "external_project_id": PROCORE_PROJECT, "external_company_id": COMPANY,
        "external_url": f"https://app.procore.com/{PROCORE_PROJECT}/project/home",
        "sync_enabled": True, "linked_via": "manual", "backfilled_at": None,
    }
    row.update(over)
    return row


def _sync(db, session, settings=None, **kw):
    link = svc.get_link(db, PROJECT)
    stats = svc.sync_project(db, link, session, settings or _settings(), **kw)
    if stats.get("backfill"):
        db.table("pm_external_links").update({"backfilled_at": "2026-10-18T12:00:00+00:00"}).eq(
            "project_id", PROJECT).execute()
    return stats


def _procore_rows(db):
    return sorted(
        (r for r in db.rows("rfis", project_id=PROJECT) if r.get("source") == "procore"),
        key=lambda r: (r["external_number"], r["external_revision"]),
    )


# ── URL parsing ───────────────────────────────────────────────────────────────


def test_parse_project_url_app_home():
    ref = pc.parse_project_url("https://app.procore.com/3167322/project/home")
    assert ref == pc.ProcoreProjectRef("app.procore.com", "3167322", None)


def test_parse_project_url_us02_rfi_link():
    ref = pc.parse_project_url("https://us02.procore.com/598134326762832/project/rfi/show/99")
    assert ref.host == "us02.procore.com" and ref.project_id == "598134326762832"


def test_parse_project_url_unwraps_safelinks_and_reads_company():
    url = (
        "https://nam09.safelinks.protection.outlook.com/?url=https%3A%2F%2Fapp.procore.com%2F"
        "3167322%2Fproject%2Frfi%2Fshow%2F27914290%3FcompanyId%3D6333&amp;data=05%7C02"
    )
    assert pc.parse_project_url(url) == pc.ProcoreProjectRef("app.procore.com", "3167322", "6333")


def test_parse_project_url_webclient_shell_carries_company():
    url = "https://app.procore.com/webclients/host/companies/6333/projects/3167322/tools/rfis"
    assert pc.parse_project_url(url) == pc.ProcoreProjectRef("app.procore.com", "3167322", "6333")


@pytest.mark.parametrize("url", [
    "https://evil.example.com/3167322/project/home",
    "https://app.procore.com/6333/company/planroom/route_to_bid_sheet/1",
    "not a url",
    "",
    "https://storage.procore.com/api/v5/files/x?companyId=6333&projectId=3167322",
])
def test_parse_project_url_refuses_non_project_urls(url):
    assert pc.parse_project_url(url) is None


# ── Email link extraction ─────────────────────────────────────────────────────


def test_const198_email_yields_its_rfi_link_and_company():
    body = _text("email_closed_const198.txt")
    assert pc.extract_rfi_links(body) == [
        pc.ProcoreRfiLink("app.procore.com", "3167322", "27914290")
    ]
    assert pc.extract_company_id(body, "3167322") == "6333"


def test_const189_1_response_email_yields_its_rfi_link():
    links = pc.extract_rfi_links(_text("email_response_const189_1.txt"))
    assert [lk.rfi_id for lk in links] == ["27699412"]


def test_boyd_bid_phase_rfi_email_does_not_trigger():
    body = _text("email_boyd_rfi20.txt")
    assert "RFI #20" in body
    assert pc.extract_rfi_links(body) == []


def test_company_id_ignores_storage_links_for_another_project():
    body = "https://storage.procore.com/api/v5/files/a?companyId=1&projectId=2&sig=x"
    assert pc.extract_company_id(body, "3167322") is None


# ── RFI allowlist (the client itself) ─────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "/rest/v1.0/projects/3167322/rfis",
    "/rest/v1.0/projects/3167322/rfis/27914290",
])
def test_rfi_allowlist_accepts_list_and_detail(path):
    assert any(p.match(path) for p in pc.RFI_ALLOWED_PATHS)


@pytest.mark.parametrize("path", [
    "/rest/v1.0/projects/3167322/rfis/27914290/replies",
    "/rest/v1.0/projects/3167322/rfis/27914290/close",
    "/rest/v1.1/projects/3167322/rfis",
    "/3167322/project/rfi/list",
])
def test_rfi_allowlist_refuses_everything_else(path):
    session = pc.ProcoreSession(pc.ProcoreConfig("a@example.com", "pw"), pc.MemorySessionStore())
    with pytest.raises(ValueError):
        session.get_rfi_json(path, company_id=COMPANY)
    session.close()


def test_session_refuses_an_unknown_app_host():
    with pytest.raises(ValueError):
        pc.ProcoreSession(pc.ProcoreConfig("a@example.com", "pw"), pc.MemorySessionStore(),
                          app_host="evil.procore.com.example.com")


def test_session_provider_shares_the_harvest_row_on_app_host_only():
    assert svc.session_provider("app.procore.com") == "procore"
    assert svc.session_provider("us02.procore.com") == "procore:us02.procore.com"


# ── Mapping ───────────────────────────────────────────────────────────────────


def test_map_detail_const198():
    m = svc.map_detail(DETAILS["27914290"], "app.procore.com", PROCORE_PROJECT)
    assert m["status"] == "closed"
    assert m["external_number"] == "Const-198" and m["external_revision"] == "0"
    assert m["external_status"] == "Closed"
    assert m["responsible_contractor"] == "G3 Electrical"
    assert m["received_from"] == "Abhishek Upadhyay"
    assert m["external_due_date"] == "2026-10-12"
    assert m["external_url"] == "https://app.procore.com/3167322/project/rfi/show/27914290"
    assert "circuit 2B-19 is meant to serve both EF-2 and EF-3" in m["answer"]
    assert m["answered_at"] == "2026-10-15"
    assert m["question"].startswith("<p>The panel schedule")
    assert "distribution_list" not in m["external_raw"]


def test_map_status_open_with_answer_is_answered():
    m = svc.map_detail(DETAILS["27699412"], "app.procore.com", PROCORE_PROJECT)
    assert m["status"] == "answered"
    assert m["ball_in_court"] == "Nenette Inovero (Cobblestone Construction)"
    # No official answer: every answer is joined, each with its author line.
    assert m["answer"].count(":\n") == 2


def test_question_is_sanitized():
    d = copy.deepcopy(DETAILS["27914290"])
    d["questions"][0]["rich_text_body"] = '<p>hi<script>alert(1)</script><img src=x onerror=y></p>'
    m = svc.map_detail(d, "app.procore.com", PROCORE_PROJECT)
    assert "<script" not in m["question"] and "<img" not in m["question"]


# ── The sync ──────────────────────────────────────────────────────────────────


def test_backfill_imports_everything_and_a_second_run_is_a_no_op(_storage):
    db = _db()
    session = FakeSession()
    stats = _sync(db, session)
    assert stats["backfill"] is True
    assert stats["created"] == 5 and stats["new"] == []
    rows = _procore_rows(db)
    assert {r["external_number"] for r in rows} == {"Const-198", "Const-189.1", "Const-97", "Int-07"}
    assert all(r["rfi_number"] is None and r["source"] == "procore" for r in rows)
    fetched_first = len(session.details_fetched())
    assert fetched_first == 5

    again = FakeSession()
    stats2 = _sync(db, again)
    assert stats2["backfill"] is False
    assert again.details_fetched() == []
    assert stats2["created"] == 0 and stats2["updated"] == 0
    assert stats2["skipped_closed"] == 3 and stats2["unchanged"] == 2
    assert len(_procore_rows(db)) == 5


def test_drafts_are_skipped():
    rows = copy.deepcopy(LIST) + [{**copy.deepcopy(LIST[1]), "id": 999, "full_number": "Const-300",
                                    "number": "Const-300", "draft": True}]
    db = _db()
    stats = _sync(db, FakeSession(rows=rows))
    assert stats["skipped_drafts"] == 1
    assert not [r for r in _procore_rows(db) if r["external_number"] == "Const-300"]


def test_revisions_are_separate_rows():
    db = _db()
    _sync(db, FakeSession())
    revs = [r for r in _procore_rows(db) if r["external_number"] == "Const-97"]
    assert [(r["external_revision"], r["external_id"], r["status"]) for r in revs] == [
        ("0", "25721337", "closed"),
        ("1", "25803224", "closed"),
    ]
    assert revs[0]["external_status"] == "Closed - Revised" or revs[0]["external_status"]


def test_closed_rows_are_never_refetched_or_reopened():
    db = _db()
    _sync(db, FakeSession())
    rows = copy.deepcopy(LIST)
    for r in rows:
        r["updated_at"] = "2026-10-20T00:00:00Z"
        if r["id"] == 27914290:
            r["status"] = "open"
    session = FakeSession(rows=rows)
    session.details["27914290"]["status"] = "open"
    _sync(db, session)
    assert "27914290" not in session.details_fetched()
    assert "25721337" not in session.details_fetched()
    [row] = [r for r in _procore_rows(db) if r["external_id"] == "27914290"]
    assert row["status"] == "closed"


def test_targeted_run_fetches_only_that_rfi_after_backfill():
    db = _db()
    _sync(db, FakeSession())
    session = FakeSession()
    _sync(db, session, target_external_rfi_id="27699412")
    assert session.calls[0][0] != "list"
    assert session.details_fetched() == ["27699412"]


def test_first_targeted_run_is_still_a_full_backfill():
    db = _db()
    session = FakeSession()
    stats = _sync(db, session, target_external_rfi_id="27914290")
    assert stats["backfill"] and stats["created"] == 5


def test_company_id_is_resolved_and_saved_when_missing():
    db = _db(pm_external_links=[_link(external_company_id=None)])
    session = FakeSession()
    _sync(db, session)
    assert ("resolve", PROCORE_PROJECT) in session.calls
    assert svc.get_link(db, PROJECT)["external_company_id"] == COMPANY


# ── Auto-link ─────────────────────────────────────────────────────────────────


def _internal(n, subject, **over):
    row = {"id": f"int{n}", "project_id": PROJECT, "rfi_number": n, "subject": subject,
           "question": "<p>G3 question</p>", "status": "open", "source": "internal",
           "send_status": "not_sent", "external_id": None, "external_autolink_blocked": False}
    row.update(over)
    return row


def test_auto_link_by_attachment_filename(_storage):
    db = _db(rfis=[_internal(40, "Exhaust fan circuits")])
    stats = _sync(db, FakeSession())
    assert stats["auto_linked"] == 1
    [row] = db.rows("rfis", id="int40")
    assert row["external_id"] == "27914290" and row["external_number"] == "Const-198"
    assert row["source"] == "internal" and row["question"] == "<p>G3 question</p>"
    assert row["status"] == "closed" and "2B-19" in row["answer"]
    assert row["send_status"] == "sent_external" and row["sent_via"] == "procore"
    assert not [r for r in _procore_rows(db) if r["external_id"] == "27914290"]
    # Answer files only: G3's own question file ("RFI 040 - ...") is not re-imported.
    kinds = {a["kind"] for a in db.rows("rfi_attachments", rfi_id="int40")}
    assert kinds == {"answer"}


def test_auto_link_by_subject():
    db = _db(rfis=[_internal(7, "Floor Closers for 206 and 207!")])
    stats = _sync(db, FakeSession())
    assert stats["auto_linked"] == 1
    assert db.rows("rfis", id="int7")[0]["external_number"] == "Const-189.1"


def test_ambiguous_subject_links_nothing():
    db = _db(rfis=[_internal(7, "Floor closers for 206 and 207"),
                   _internal(8, "floor closers for 206 and 207")])
    stats = _sync(db, FakeSession())
    assert stats["auto_linked"] == 0
    assert db.rows("rfis", id="int7")[0]["external_id"] is None


def test_blocked_internal_rows_are_not_auto_linked():
    db = _db(rfis=[_internal(40, "x", external_autolink_blocked=True)])
    stats = _sync(db, FakeSession())
    assert stats["auto_linked"] == 0


def test_linked_internal_row_answer_updates_never_overwrite_the_question():
    db = _db(rfis=[_internal(7, "Floor closers for 206 and 207")])
    _sync(db, FakeSession())
    rows = copy.deepcopy(LIST)
    for r in rows:
        if r["id"] == 27699412:
            r["updated_at"] = "2026-10-20T00:00:00Z"
            r["status"] = "closed"
    session = FakeSession(rows=rows)
    session.details["27699412"]["status"] = "closed"
    session.details["27699412"]["questions"][0]["rich_text_body"] = "<p>GC rewrote it</p>"
    stats = _sync(db, session)
    row = db.rows("rfis", id="int7")[0]
    assert row["question"] == "<p>G3 question</p>" and row["status"] == "closed"
    assert stats["closed"] == ["Const-189.1 Floor closers for 206 and 207"]


# ── Attachments ───────────────────────────────────────────────────────────────


def test_attachments_downloaded_once_into_documents(_storage):
    db = _db()
    _sync(db, FakeSession())
    [row] = [r for r in _procore_rows(db) if r["external_id"] == "27914290"]
    atts = db.rows("rfi_attachments", rfi_id=row["id"])
    assert {a["kind"] for a in atts} == {"question", "answer"}
    assert all(a["doc_key"].startswith("pm:") and a["external_file_id"] for a in atts)
    docs = db.rows("pm_documents")
    assert all(d["category"] == "rfi" and "[procore-file:" in d["note"] for d in docs)
    assert len(_storage) == len(docs)


def test_oversize_attachment_is_skipped_and_recorded():
    db = _db()
    session = FakeSession()
    session.oversize = {"01M4C5FMTMT4Z0S1WBYQ0TZRXM"}
    stats = _sync(db, session)
    skipped = stats["skipped_files"]
    assert [s["filename"] for s in skipped] == ["RFI 040 - EF3 Circuit Clarification.pdf"]
    assert skipped[0]["reason"] == "oversize" and skipped[0]["limit_bytes"] == 150 * 1024 * 1024


def test_unlink_then_resync_reuses_the_documents(_storage):
    db = _db(rfis=[_internal(40, "Exhaust fan circuits")])
    _sync(db, FakeSession())
    uploaded = len(_storage)
    svc.unlink_row(db, PROJECT, "int40")
    row = db.rows("rfis", id="int40")[0]
    assert row["external_id"] is None and row["external_autolink_blocked"] is True
    assert db.rows("rfi_attachments", rfi_id="int40") == []
    _sync(db, FakeSession())
    assert [r for r in _procore_rows(db) if r["external_id"] == "27914290"]
    # The re-imported row's answer file came back from Documents, not Procore.
    assert db.rows("rfis", id="int40")[0]["external_id"] is None
    answer_docs = [d for d in db.rows("pm_documents") if "Response RFI" in d["filename"]]
    assert len(answer_docs) == 1
    assert len(_storage) == uploaded + 1  # only the question file is new


# ── Manual link ───────────────────────────────────────────────────────────────


def test_manual_link_merges_the_imported_row():
    db = _db(rfis=[_internal(12, "Something else")])
    _sync(db, FakeSession())
    [imported] = [r for r in _procore_rows(db) if r["external_id"] == "27699412"]
    att_count = len(db.rows("rfi_attachments", rfi_id=imported["id"]))
    row = svc.link_rows(db, PROJECT, imported["id"], "int12")
    assert row["external_id"] == "27699412" and row["source"] == "internal"
    assert db.rows("rfis", id=imported["id"]) == []
    assert len(db.rows("rfi_attachments", rfi_id="int12")) == att_count
    with pytest.raises(svc.LinkActionError) as exc:
        svc.link_rows(db, PROJECT, "int12", "int12")
    assert exc.value.status_code == 409


def test_manual_link_refuses_an_already_linked_internal_row():
    db = _db(rfis=[_internal(40, "Exhaust fan circuits")])
    _sync(db, FakeSession())
    [imported] = [r for r in _procore_rows(db) if r["external_id"] == "27699412"]
    with pytest.raises(svc.LinkActionError) as exc:
        svc.link_rows(db, PROJECT, imported["id"], "int40")
    assert exc.value.status_code == 409


# ── Notifications ─────────────────────────────────────────────────────────────


class _Bells:
    def __init__(self):
        self.calls = []

    def __call__(self, role, project_id, type_, message, **kw):
        self.calls.append((role, project_id, type_, message, kw))


def test_backfill_sends_one_in_app_notice_per_role_and_no_email():
    db = _db()
    stats = _sync(db, FakeSession())
    bells = _Bells()
    svc.notify_after_run(PROJECT, stats, notify=bells)
    assert [(c[0].value, c[2]) for c in bells.calls] == [
        ("estimating_engineer_labor", "pm_rfi.linked"),
        ("estimating_admin", "pm_rfi.linked"),
    ]
    assert all(c[4]["mirror_email"] is False for c in bells.calls)
    assert "imported 5 RFIs" in bells.calls[0][3]


def test_summary_after_backfill_names_new_answered_and_closed():
    db = _db()
    _sync(db, FakeSession())
    rows = copy.deepcopy(LIST)
    new_row = {**copy.deepcopy(LIST[4]), "id": 30000001, "full_number": "Const-201",
               "number": "Const-201", "subject": "Panel feed size", "status": "open"}
    rows.append(new_row)
    for r in rows:
        if r["id"] == 28008229:  # Int-07: open, now answered
            r["updated_at"] = "2026-10-20T00:00:00Z"
        if r["id"] == 27699412:  # Const-189.1: answered, now closed
            r["updated_at"] = "2026-10-20T00:00:00Z"
            r["status"] = "closed"
    session = FakeSession(rows=rows)
    int07 = _detail_from_list([r for r in rows if r["id"] == 28008229][0])
    int07["questions"][0]["answers"] = [{"id": 5, "official": True, "plain_text_body": "Use W8x10",
                                         "answer_date": "2026-10-19T17:00:00Z", "created_by": "GC PM",
                                         "attachments": []}]
    session.details["28008229"] = int07
    session.details["27699412"]["status"] = "closed"
    stats = _sync(db, session)
    assert stats["new"] == ["Const-201 Panel feed size"]
    assert [s.split(" ")[0] for s in stats["answered"]] == ["Int-07"]
    assert [s.split(" ")[0] for s in stats["closed"]] == ["Const-189.1"]
    bells = _Bells()
    assert svc.notify_after_run(PROJECT, stats, notify=bells) == 2
    assert {c[2] for c in bells.calls} == {"pm_rfi.sync_summary"}
    assert "mirror_email" not in bells.calls[0][4]  # the branded mirror email goes out
    message = bells.calls[0][3]
    assert "1 new: Const-201 Panel feed size" in message
    assert "1 answered: Int-07" in message and "1 closed: Const-189.1" in message


def test_no_changes_no_summary():
    bells = _Bells()
    assert svc.notify_after_run(PROJECT, {"backfill": False, "new": [], "answered": [], "closed": []},
                                notify=bells) == 0


# ── Slots and the ledger ──────────────────────────────────────────────────────


def test_daily_slot_is_0630_pacific_in_summer_and_winter():
    s = _settings()
    summer = datetime(2026, 7, 1, 13, 35, tzinfo=timezone.utc)   # 06:35 PDT
    winter = datetime(2026, 12, 1, 14, 35, tzinfo=timezone.utc)  # 06:35 PST
    assert svc.due_daily_slot(summer, s) == datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    assert svc.due_daily_slot(winter, s) == datetime(2026, 12, 1, 14, 30, tzinfo=timezone.utc)
    # 13:35 UTC in December is 05:35 PST: before today's slot, and
    # yesterday's catch-up window is long gone.
    assert svc.due_daily_slot(datetime(2026, 12, 1, 13, 35, tzinfo=timezone.utc), s) is None


def test_daily_slot_on_the_dst_change_days():
    s = _settings()
    spring = datetime(2026, 3, 8, 13, 31, tzinfo=timezone.utc)   # 06:31 PDT, the day clocks moved
    fall = datetime(2026, 11, 1, 14, 31, tzinfo=timezone.utc)    # 06:31 PST
    assert svc.due_daily_slot(spring, s) == datetime(2026, 3, 8, 13, 30, tzinfo=timezone.utc)
    assert svc.due_daily_slot(fall, s) == datetime(2026, 11, 1, 14, 30, tzinfo=timezone.utc)


def test_catch_up_window_then_skip():
    s = _settings(pm_rfi_sync_catchup_hours=4)
    slot = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    assert svc.due_daily_slot(slot + timedelta(hours=3, minutes=59), s) == slot
    assert svc.due_daily_slot(slot + timedelta(hours=4), s) is None


def test_schedule_daily_ledger_never_double_queues():
    db = _db(pm_external_links=[_link(), _link(id="l2", project_id="p2", external_project_id="42"),
                                _link(id="l3", project_id="bid", platform="none", host=None,
                                      external_project_id=None)])
    s = _settings()
    now = datetime(2026, 7, 1, 13, 40, tzinfo=timezone.utc)
    assert svc.schedule_daily(db, s, now) == 2
    assert svc.schedule_daily(db, s, now) == 0          # the second worker's tick
    assert svc.schedule_daily(db, s, now + timedelta(minutes=30)) == 0
    assert len(db.rows("pm_rfi_sync_runs", trigger="daily")) == 2
    # The racing insert itself is refused by the unique index.
    assert svc.enqueue_run(db, PROJECT, "daily",
                           scheduled_for=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)) == (None, False)


def test_bad_daily_time_refuses_to_boot():
    with pytest.raises(ValueError):
        _settings(pm_rfi_sync_daily_time="6:30am")


# ── Queue and leases ──────────────────────────────────────────────────────────


def test_manual_runs_dedupe_while_one_is_open():
    db = _db()
    run, created = svc.enqueue_run(db, PROJECT, "manual", requested_by="u1")
    again, created2 = svc.enqueue_run(db, PROJECT, "manual", requested_by="u2")
    assert created and not created2 and again["id"] == run["id"]


def test_claim_is_exclusive_per_project_and_reclaims_expired_leases():
    db = _db()
    svc.enqueue_run(db, PROJECT, "manual")
    svc.enqueue_run(db, PROJECT, "email", target_external_rfi_id="1", source_email_id="e1")
    s = _settings()
    now = datetime.now(timezone.utc)
    first = svc.claim_next(db, s, now, worker_id="w1")
    assert first and first["lease_owner"] == "w1"
    assert svc.claim_next(db, s, now, worker_id="w2") is None   # project busy
    later = now + timedelta(seconds=s.pm_rfi_sync_lease_seconds + 1)
    retaken = svc.claim_next(db, s, later, worker_id="w2")
    assert retaken["id"] == first["id"] and retaken["attempts"] == 2


def test_process_run_end_to_end_marks_link_backfilled(monkeypatch):
    db = _db()
    run, _ = svc.enqueue_run(db, PROJECT, "manual")
    s = _settings()
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    bells = _Bells()
    status = svc.process_run(db, claimed, s, session_factory=lambda host, st: FakeSession(), notify=bells)
    assert status == "done"
    run = db.rows("pm_rfi_sync_runs", id=run["id"])[0]
    assert run["status"] == "done" and run["stats"]["created"] == 5
    link = svc.get_link(db, PROJECT)
    assert link["backfilled_at"] and link["last_sync_status"] == "ok"
    assert {c[2] for c in bells.calls} == {"pm_rfi.linked"}


def test_process_run_retries_a_transient_failure_then_fails():
    db = _db()
    svc.enqueue_run(db, PROJECT, "manual")
    s = _settings(pm_rfi_sync_max_attempts=2)

    class Down(FakeSession):
        def list_rfis(self, *a):
            raise pc.ProcoreTransient("Procore answered 503.")

    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: Down()) == "queued"
    run = db.rows("pm_rfi_sync_runs")[0]
    assert run["status"] == "queued" and run["next_attempt_at"]
    later = datetime.now(timezone.utc) + timedelta(seconds=s.pm_rfi_sync_retry_seconds + 5)
    claimed = svc.claim_next(db, s, later)
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: Down()) == "failed"
    assert svc.get_link(db, PROJECT)["last_sync_status"] == "failed"


def test_process_run_without_credentials_fails_clearly():
    db = _db()
    svc.enqueue_run(db, PROJECT, "manual")
    s = _settings(procore_login_email="", procore_login_password="")
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: FakeSession()) == "failed"
    assert "credentials" in db.rows("pm_rfi_sync_runs")[0]["error"]


# ── Email scan ────────────────────────────────────────────────────────────────


def _email(eid, body, project_id=PROJECT, updated_at="2026-10-18T11:00:00+00:00", direction="inbound"):
    return {"id": eid, "project_id": project_id, "direction": direction, "body_text": body,
            "updated_at": updated_at}


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def test_email_scan_auto_links_an_unlinked_pm_project_and_queues_a_targeted_run():
    db = _db(pm_external_links=[], ingested_emails=[
        _email("e1", _text("email_closed_const198.txt")),
        _email("e2", _text("email_boyd_rfi20.txt")),
    ])
    stats = svc.scan_emails(db, _settings(), NOW)
    assert stats["auto_linked_projects"] == 1 and stats["runs_queued"] == 1
    link = svc.get_link(db, PROJECT)
    assert (link["platform"], link["host"], link["external_project_id"], link["external_company_id"],
            link["linked_via"]) == ("procore", "app.procore.com", PROCORE_PROJECT, COMPANY, "email")
    [run] = db.rows("pm_rfi_sync_runs")
    assert (run["trigger"], run["target_external_rfi_id"], run["source_email_id"]) == (
        "email", "27914290", "e1")
    # A re-read (updated_at moved, or the next tick) queues nothing new.
    db.table("ingested_emails").update({"updated_at": "2026-10-18T11:30:00+00:00"}).eq("id", "e1").execute()
    db.table("pm_rfi_sync_runs").update({"status": "done"}).eq("id", run["id"]).execute()
    assert svc.scan_emails(db, _settings(), NOW)["runs_queued"] == 0


def test_email_scan_does_not_steal_a_procore_project_linked_elsewhere():
    db = _db(pm_external_links=[_link(project_id="p2", id="l2")], ingested_emails=[
        _email("e1", _text("email_closed_const198.txt")),
    ])
    stats = svc.scan_emails(db, _settings(), NOW)
    assert stats["auto_linked_projects"] == 0 and svc.get_link(db, PROJECT) is None
    assert db.rows("pm_rfi_sync_runs") == []


def test_email_scan_ignores_bid_projects_outbound_mail_and_none_links():
    db = _db(pm_external_links=[_link(project_id="p2", id="l2", platform="none", host=None,
                                      external_project_id=None)],
             ingested_emails=[
                 _email("e1", _text("email_closed_const198.txt"), project_id="bid"),
                 _email("e2", _text("email_closed_const198.txt"), direction="outbound"),
                 _email("e3", _text("email_closed_const198.txt"), project_id="p2"),
             ])
    stats = svc.scan_emails(db, _settings(), NOW)
    assert stats["runs_queued"] == 0 and db.rows("pm_rfi_sync_runs") == []


def test_email_scan_reads_rfis_classifications_when_the_table_exists():
    db = _db(ingested_emails=[_email("e9", "Please see the RFI response attached.")],
             email_classifications=[{
                 "email_id": "e9", "project_id": PROJECT, "status": "done",
                 "ai_labels": [{"category": "rfis"}], "user_labels": None,
                 "updated_at": "2026-10-18T11:00:00+00:00",
             }])
    stats = svc.scan_emails(db, _settings(), NOW)
    assert stats["classified"] == 1 and stats["runs_queued"] == 1
    [run] = db.rows("pm_rfi_sync_runs")
    assert run["target_external_rfi_id"] is None and run["source_email_id"] == "e9"


def test_email_scan_tolerates_a_missing_classifications_table():
    db = _db(ingested_emails=[])
    real = db.table

    def table(name):
        if name == "email_classifications":
            raise Exception("{'code': 'PGRST205', 'message': \"Could not find the table\"}")
        return real(name)

    db.table = table
    assert svc.scan_emails(db, _settings(), NOW)["runs_queued"] == 0


# ── Link settings ─────────────────────────────────────────────────────────────


def test_save_link_parses_url_and_refuses_conflicts():
    db = _db(pm_external_links=[_link(project_id="p2", id="l2")])
    with pytest.raises(svc.LinkError) as exc:
        svc.save_link(db, PROJECT, platform="procore",
                      url="https://app.procore.com/3167322/project/home", sync_enabled=None, actor_id="u1")
    assert exc.value.status_code == 409
    with pytest.raises(svc.LinkError) as exc:
        svc.save_link(db, PROJECT, platform="procore", url="https://example.com/x",
                      sync_enabled=None, actor_id="u1")
    assert exc.value.status_code == 422
    row, changed = svc.save_link(
        db, PROJECT, platform="procore",
        url="https://us02.procore.com/598134326762832/project/rfi/show/1",
        sync_enabled=None, actor_id="u1", resolve_company=lambda ref: "598134325511223",
    )
    assert changed and row["host"] == "us02.procore.com"
    assert row["external_company_id"] == "598134325511223" and row["backfilled_at"] is None


def test_save_link_other_and_none_disable_sync():
    db = _db()
    row, _ = svc.save_link(db, PROJECT, platform="other", url="https://acc.autodesk.com/p/1",
                           sync_enabled=True, actor_id="u1")
    assert row["platform"] == "other" and row["sync_enabled"] is False
    assert row["external_url"] == "https://acc.autodesk.com/p/1"


def test_link_view_shape():
    view = svc.link_view(_link(), running=True)
    assert set(view) == {
        "platform", "host", "external_company_id", "external_project_id", "external_url",
        "sync_enabled", "linked_via", "last_synced_at", "last_sync_status", "last_error",
        "backfilled_at", "running",
    }
    assert svc.link_view(None, False)["platform"] is None


# ── The RFI routes: read-only rows and ordering ───────────────────────────────


USER = SimpleNamespace(id="u1")


def _install_routes(monkeypatch, db):
    monkeypatch.setattr(pm_field, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_svc, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_field, "audit", lambda *a, **k: None)
    monkeypatch.setattr(pm_field.pm_folders, "list_project_documents", lambda pid: [])


def _imported_db():
    db = _db(rfis=[_internal(2, "Second"), _internal(1, "First")])
    _sync(db, FakeSession())
    return db


@pytest.mark.parametrize("call", ["update", "close", "send", "mark_sent", "delete"])
def test_imported_rows_are_read_only(monkeypatch, call):
    from app.models.schemas import RFIClose, RFIMarkSentIn, RFISendIn, RFIUpdate

    db = _imported_db()
    _install_routes(monkeypatch, db)
    rid = _procore_rows(db)[0]["id"]
    actions = {
        "update": lambda: pm_field.update_rfi(PROJECT, rid, RFIUpdate(subject="x"), USER),
        "close": lambda: pm_field.close_rfi(
            PROJECT, rid, RFIClose(answer="a", answered_by="b", attachment_keys=["pm:" + "0" * 36]), USER),
        "send": lambda: pm_field.send_rfi(PROJECT, rid, RFISendIn(contact_ids=["c1"]), USER),
        "mark_sent": lambda: pm_field.mark_rfi_sent(PROJECT, rid, RFIMarkSentIn(platform="procore"), USER),
        "delete": lambda: pm_field.delete_rfi(PROJECT, rid, USER),
    }
    with pytest.raises(HTTPException) as exc:
        actions[call]()
    assert exc.value.status_code == 409
    assert exc.value.detail == "This RFI is imported from Procore and is read-only."


def test_list_rfis_orders_internal_then_procore_and_flags_read_only(monkeypatch):
    db = _imported_db()
    _install_routes(monkeypatch, db)
    rows = pm_field.list_rfis(PROJECT, USER)
    labels = [r["rfi_number"] if r["source"] == "internal"
              else (r["external_number"], r["external_revision"]) for r in rows]
    assert labels == [1, 2, ("Const-97", "0"), ("Const-97", "1"), ("Const-189.1", "0"),
                      ("Const-198", "0"), ("Int-07", "0")]
    assert [r["read_only"] for r in rows] == [False, False, True, True, True, True, True]
    assert all("external_raw" not in r for r in rows)


def test_natural_sort_keys():
    nums = ["Const-189.1", "Const-9", "Const-189", "Const-97", "Const-189.0"]
    assert sorted(nums, key=svc.natural_key) == [
        "Const-9", "Const-97", "Const-189", "Const-189.0", "Const-189.1"
    ]


def test_rfi_number_conflict_recognizes_the_0156_index():
    assert pm_field._is_rfi_number_conflict(
        Exception('duplicate key value violates unique constraint "rfis_project_rfi_number_uidx"')
    )


# ── The client's RFI reads over a mock transport ──────────────────────────────


def _mock_session(handler, host="app.procore.com"):
    import httpx

    store = pc.MemorySessionStore()
    store.save_cookies("a@example.com", [
        {"name": "_session_id", "value": "s", "domain": host, "path": "/", "expires": None},
    ])
    return pc.ProcoreSession(
        pc.ProcoreConfig("a@example.com", "pw", min_request_interval=0),
        store, transport=httpx.MockTransport(handler), app_host=host,
    )


def test_list_rfis_sends_company_header_and_only_gets():
    import httpx

    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=LIST, headers={"content-type": "application/json"})

    with _mock_session(handler, host="us02.procore.com") as session:
        rows = session.list_rfis("598134326762832", "598134325511223")
    assert len(rows) == len(LIST)
    [req] = seen
    assert req.method == "GET" and req.url.host == "us02.procore.com"
    assert req.url.path == "/rest/v1.0/projects/598134326762832/rfis"
    assert req.headers["Procore-Company-Id"] == "598134325511223"
    assert req.headers["X-Requested-With"] == "XMLHttpRequest"


def test_resolve_company_id_reads_the_home_redirect_without_following():
    import httpx

    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={
            "location": "https://app.procore.com/webclients/host/companies/6333/projects/3167322/tools/projecthome"
        })

    with _mock_session(handler) as session:
        assert session.resolve_company_id("3167322") == "6333"
    assert seen == ["/3167322/project/home"]


# ── The pm-link / sync routes ─────────────────────────────────────────────────


def _install_sync_routes(monkeypatch, db):
    from app.routers import pm_rfi_sync as routes

    _install_routes(monkeypatch, db)
    monkeypatch.setattr(routes, "get_supabase", lambda: db)
    monkeypatch.setattr(routes, "audit", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_resolve_company", lambda ref: None)
    return routes


def test_routes_404_while_the_flag_is_off(monkeypatch):
    from app.core import features

    monkeypatch.setattr(features, "get_settings", lambda: _settings(pm_rfi_sync_enabled=False))
    with pytest.raises(HTTPException) as exc:
        features.require_pm_rfi_sync()
    assert exc.value.status_code == 404
    monkeypatch.setattr(features, "get_settings", lambda: _settings())
    features.require_pm_rfi_sync()
    assert features.enabled_map()["pm_rfi_sync"] is True


def test_put_pm_link_saves_and_queues_the_backfill(monkeypatch):
    from app.models.schemas import PmExternalLinkIn

    db = _db(pm_external_links=[])
    routes = _install_sync_routes(monkeypatch, db)
    view = routes.put_pm_link(
        PROJECT,
        PmExternalLinkIn(platform="procore",
                         url="https://app.procore.com/webclients/host/companies/6333/projects/3167322/tools/rfis"),
        USER,
    )
    assert view["platform"] == "procore" and view["external_company_id"] == COMPANY
    assert view["running"] is True
    [run] = db.rows("pm_rfi_sync_runs")
    assert run["trigger"] == "manual" and run["requested_by"] == "u1"
    assert routes.get_pm_link(PROJECT, USER)["external_project_id"] == PROCORE_PROJECT
    with pytest.raises(HTTPException) as exc:
        routes.put_pm_link(PROJECT, PmExternalLinkIn(platform="procore", url="https://x.example.com"), USER)
    assert exc.value.status_code == 422


def test_sync_now_needs_a_procore_link_and_dedupes(monkeypatch):
    db = _db(pm_external_links=[])
    routes = _install_sync_routes(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        routes.sync_rfis(PROJECT, USER)
    assert exc.value.status_code == 409
    db.tables["pm_external_links"] = [_link()]
    first = routes.sync_rfis(PROJECT, USER)
    second = routes.sync_rfis(PROJECT, USER)
    assert first["status"] == "queued" and first["run_id"] == second["run_id"]


def test_link_and_unlink_routes(monkeypatch):
    from app.models.schemas import RFILinkIn

    db = _db(rfis=[_internal(12, "Something else")])
    _sync(db, FakeSession())
    routes = _install_sync_routes(monkeypatch, db)
    [imported] = [r for r in _procore_rows(db) if r["external_id"] == "27699412"]
    row = routes.link_rfi(PROJECT, imported["id"], RFILinkIn(internal_rfi_id="int12"), USER)
    assert row["external_number"] == "Const-189.1" and row["read_only"] is False
    row = routes.unlink_rfi(PROJECT, "int12", USER)
    assert row["external_id"] is None
    with pytest.raises(HTTPException) as exc:
        routes.unlink_rfi(PROJECT, "int12", USER)
    assert exc.value.status_code == 409


# ── Review fixes ──────────────────────────────────────────────────────────────


class _FlakyFiles(FakeSession):
    """Storage answers 503 for files whose url holds one of `flaky`."""

    def __init__(self, *a, flaky=(), **k):
        super().__init__(*a, **k)
        self.flaky = set(flaky)

    def download(self, url, dest, *, max_bytes):
        if any(key in url for key in self.flaky):
            self.calls.append(("download", url))
            raise pc.ProcoreTransient("Procore storage answered 503.")
        return super().download(url, dest, max_bytes=max_bytes)


def test_a_file_that_failed_for_now_is_fetched_again_even_on_a_closed_row(_storage):
    db = _db()
    stats = _sync(db, _FlakyFiles(flaky={"01M4C5FMTMT4Z0S1WBYQ0TZRXM"}))
    assert stats["attachments_failed"] == 1
    assert stats["skipped_files"][0]["reason"] == "retry"
    [row] = [r for r in _procore_rows(db) if r["external_id"] == "27914290"]
    assert row["status"] == "closed" and row["external_updated_at"] is None
    assert {a["kind"] for a in db.rows("rfi_attachments", rfi_id=row["id"])} == {"answer"}
    again = FakeSession()
    _sync(db, again)
    assert again.details_fetched() == ["27914290"]
    [row] = [r for r in _procore_rows(db) if r["external_id"] == "27914290"]
    assert row["external_updated_at"] and row["status"] == "closed"
    assert {a["kind"] for a in db.rows("rfi_attachments", rfi_id=row["id"])} == {"question", "answer"}
    # Complete now: never fetched again.
    third = FakeSession()
    _sync(db, third)
    assert third.details_fetched() == []


def test_transitions_saved_before_a_failure_are_announced_by_the_retry():
    db = _db()
    _sync(db, FakeSession())
    rows = copy.deepcopy(LIST)
    for r in rows:
        if r["id"] in (28008229, 27699412):
            r["updated_at"] = "2026-10-20T00:00:00Z"
        if r["id"] == 27699412:
            r["status"] = "closed"
    s = _settings()

    class DiesSecond(FakeSession):
        def get_rfi(self, project_id, rfi_id, company_id):
            if rfi_id == "28008229":
                raise pc.ProcoreTransient("Procore answered 503.")
            return super().get_rfi(project_id, rfi_id, company_id)

    first = DiesSecond(rows=rows)
    first.details["27699412"]["status"] = "closed"
    svc.enqueue_run(db, PROJECT, "manual")
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    bells = _Bells()
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: first, notify=bells) == "queued"
    assert bells.calls == []
    assert db.rows("pm_rfi_sync_runs")[0]["stats"]["closed"] == ["Const-189.1 Floor closers for 206 and 207"]
    later = datetime.now(timezone.utc) + timedelta(seconds=s.pm_rfi_sync_retry_seconds + 5)
    claimed = svc.claim_next(db, s, later)
    second = FakeSession(rows=rows)
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: second, notify=bells) == "done"
    assert "27699412" not in second.details_fetched()
    assert bells.calls and "1 closed: Const-189.1" in bells.calls[0][3]


def test_failed_run_still_announces_what_it_saved():
    db = _db()
    _sync(db, FakeSession())
    rows = copy.deepcopy(LIST)
    for r in rows:
        if r["id"] in (28008229, 27699412):
            r["updated_at"] = "2026-10-20T00:00:00Z"
        if r["id"] == 27699412:
            r["status"] = "closed"

    class Refused(FakeSession):
        def get_rfi(self, project_id, rfi_id, company_id):
            if rfi_id == "28008229":
                raise RuntimeError("boom")
            return super().get_rfi(project_id, rfi_id, company_id)

    session = Refused(rows=rows)
    session.details["27699412"]["status"] = "closed"
    svc.enqueue_run(db, PROJECT, "manual")
    s = _settings()
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    bells = _Bells()
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: session, notify=bells) == "failed"
    assert bells.calls and "1 closed: Const-189.1" in bells.calls[0][3]


def test_relinking_to_another_procore_project_drops_the_old_import(_storage):
    db = _db(rfis=[_internal(40, "Exhaust fan circuits")])
    _sync(db, FakeSession())
    assert _procore_rows(db) and db.rows("rfis", id="int40")[0]["external_id"]
    row, changed = svc.save_link(db, PROJECT, platform="procore",
                                 url="https://app.procore.com/999/project/home?companyId=6333",
                                 sync_enabled=None, actor_id="u1")
    assert changed and row["backfilled_at"] is None
    assert _procore_rows(db) == []
    internal = db.rows("rfis", id="int40")[0]
    assert internal["external_id"] is None and internal["external_autolink_blocked"] is False
    assert db.rows("rfi_attachments", rfi_id="int40") == []
    assert internal["question"] == "<p>G3 question</p>"


def test_switching_a_procore_link_to_none_drops_the_import():
    db = _db()
    _sync(db, FakeSession())
    svc.save_link(db, PROJECT, platform="none", url=None, sync_enabled=None, actor_id="u1")
    assert _procore_rows(db) == []


def test_a_number_reused_after_a_procore_delete_replaces_the_stale_row():
    db = _db()
    _sync(db, FakeSession())
    rows = [r for r in copy.deepcopy(LIST) if r["id"] != 28008229]  # Int-07 deleted in Procore
    reborn = {**copy.deepcopy([r for r in LIST if r["id"] == 28008229][0]), "id": 30000002}
    rows.append(reborn)
    stats = _sync(db, FakeSession(rows=rows))
    assert stats["retired"] == 1 and stats["conflicts"] == 0
    [row] = [r for r in _procore_rows(db) if r["external_number"] == "Int-07"]
    assert row["external_id"] == "30000002"


def test_yield_to_rival_keeps_only_the_oldest_running_run():
    db = _db()
    s = _settings()
    now = datetime.now(timezone.utc)
    live = svc._iso(now + timedelta(seconds=300))
    db.tables["pm_rfi_sync_runs"] = [
        {"id": "r1", "project_id": PROJECT, "trigger": "manual", "status": "running", "lease_owner": "w1",
         "lease_until": live, "attempts": 1, "created_at": "2026-10-18T10:00:00+00:00"},
        {"id": "r2", "project_id": PROJECT, "trigger": "daily", "status": "running", "lease_owner": "w2",
         "lease_until": live, "attempts": 1, "created_at": "2026-10-18T10:00:05+00:00"},
    ]
    r2 = db.rows("pm_rfi_sync_runs", id="r2")[0]
    assert svc._yield_to_rival(db, r2, {"attempts": 0, "started_at": None}, now) is True
    after = db.rows("pm_rfi_sync_runs", id="r2")[0]
    assert after["status"] == "queued" and after["lease_owner"] is None and after["attempts"] == 0
    r1 = db.rows("pm_rfi_sync_runs", id="r1")[0]
    assert svc._yield_to_rival(db, r1, {"attempts": 0}, now) is False
    assert s  # settings unused; keeps the signature honest


def test_claim_skips_a_project_whose_run_another_worker_just_took():
    db = _db()
    svc.enqueue_run(db, PROJECT, "manual")
    svc.enqueue_run(db, PROJECT, "daily", scheduled_for=datetime(2026, 10, 18, 13, 30, tzinfo=timezone.utc))
    s = _settings()
    now = datetime.now(timezone.utc)
    real = db.table
    state = {"stolen": False}

    def table(name):
        q = real(name)
        if name == "pm_rfi_sync_runs" and not state["stolen"]:
            orig_execute = q.execute

            def execute():
                if q.op == "update" and q.payload.get("status") == "running":
                    # Worker A wins the first compare-and-set an instant earlier.
                    state["stolen"] = True
                    first = db.rows("pm_rfi_sync_runs")[0]
                    first.update(status="running", lease_owner="wA",
                                 lease_until=svc._iso(now + timedelta(seconds=300)))
                    return SimpleNamespace(data=[])
                return orig_execute()

            q.execute = execute
        return q

    db.table = table
    assert svc.claim_next(db, s, now, worker_id="wB") is None
    assert [r["status"] for r in db.rows("pm_rfi_sync_runs")] == ["running", "queued"]


def test_expired_run_past_the_attempt_cap_is_failed_not_reclaimed():
    db = _db()
    s = _settings(pm_rfi_sync_max_attempts=2)
    past = svc._iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    db.tables["pm_rfi_sync_runs"] = [
        {"id": "r1", "project_id": PROJECT, "trigger": "manual", "status": "running", "lease_owner": "w1",
         "lease_until": past, "attempts": 2, "created_at": "2026-10-18T10:00:00+00:00"},
    ]
    assert svc.claim_next(db, s, datetime.now(timezone.utc)) is None
    assert db.rows("pm_rfi_sync_runs")[0]["status"] == "failed"


def _plain_numbered(number, *, question_files=(), answer_files=()):
    def files(names):
        return [{"id": f"f-{n}", "filename": n, "url": f"https://storage.procore.com/x/{n}"} for n in names]

    return {
        "id": 40000001, "number": number, "full_number": number, "revision": "0", "subject": "Something",
        "status": "open", "draft": False, "updated_at": "2026-10-18T10:00:00Z",
        "questions": [{"id": 1, "plain_text_body": "q", "attachments": files(question_files),
                       "answers": [{"id": 2, "official": True, "plain_text_body": "a",
                                    "attachments": files(answer_files)}]}],
    }


def test_filename_auto_link_ignores_answer_files_and_the_rfis_own_number():
    cands = [_internal(12, "G3 twelve"), _internal(40, "G3 forty")]
    # A GC response named after the GC's own RFI 12: no link.
    assert svc.match_internal(_plain_numbered("12", answer_files=["RFI 12 Response.pdf"]), cands) is None
    # The GC RFI 12's own question file "RFI 12 ...": no link.
    assert svc.match_internal(_plain_numbered("12", question_files=["RFI 12 - sketch.pdf"]), cands) is None
    # G3's file on someone else's number still links.
    hit = svc.match_internal(_plain_numbered("12", question_files=["RFI 040 - EF3.pdf"]), cands)
    assert hit["rfi_number"] == 40


def test_relink_while_a_run_works_queues_a_fresh_run_and_the_old_run_drops_its_import():
    db = _db()
    s = _settings()
    svc.enqueue_run(db, PROJECT, "manual")
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    # A writer re-points the link while the run is in flight.
    _, created = svc.enqueue_run(db, PROJECT, "manual", fresh=True)
    assert created
    _, joined = svc.enqueue_run(db, PROJECT, "manual", fresh=True)
    assert not joined

    class Repoint(FakeSession):
        def list_rfis(self, project_id, company_id):
            db.table("pm_external_links").update(
                {"external_project_id": "999", "backfilled_at": None}).eq("project_id", PROJECT).execute()
            return super().list_rfis(project_id, company_id)

    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: Repoint()) == "done"
    link = svc.get_link(db, PROJECT)
    assert link["backfilled_at"] is None and link.get("last_sync_status") is None
    assert _procore_rows(db) == []


def test_manual_sync_does_not_join_a_targeted_email_run():
    db = _db()
    email_run, _ = svc.enqueue_run(db, PROJECT, "email", target_external_rfi_id="1", source_email_id="e1")
    run, created = svc.enqueue_run(db, PROJECT, "manual")
    assert created and run["id"] != email_run["id"] and run["target_external_rfi_id"] is None


def test_rfis_the_backfill_could_not_read_are_not_announced_as_new_later():
    db = _db()
    s = _settings()

    class Refuses(FakeSession):
        def get_rfi(self, project_id, rfi_id, company_id):
            if rfi_id == "28008229":
                self.calls.append(("detail", rfi_id))
                raise pc.ProcoreForbidden("no")
            return super().get_rfi(project_id, rfi_id, company_id)

    svc.enqueue_run(db, PROJECT, "manual")
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: Refuses(), notify=_Bells()) == "done"
    link = svc.get_link(db, PROJECT)
    assert link["backfilled_at"] and link["backfill_skipped_ids"] == ["28008229"]
    stats = _sync(db, FakeSession())
    assert stats["created"] == 1 and stats["new"] == []


def test_a_backfill_cut_by_the_detail_cap_is_not_marked_done():
    db = _db()
    s = _settings(pm_rfi_sync_max_details_per_run=2)
    svc.enqueue_run(db, PROJECT, "manual")
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    bells = _Bells()
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: FakeSession(), notify=bells) == "done"
    assert svc.get_link(db, PROJECT)["backfilled_at"] is None and bells.calls == []
    stats = svc.sync_project(db, svc.get_link(db, PROJECT), FakeSession(), s)
    assert stats["backfill"] is True and stats["new"] == []


def test_email_checkpoint_moves_past_unfiled_emails():
    body = _text("email_closed_const198.txt")
    emails = [_email(f"u{i}", body, project_id=None, updated_at=f"2026-10-18T11:{i:02d}:00+00:00")
              for i in range(3)]
    db = _db(ingested_emails=emails)
    svc.scan_emails(db, _settings(), NOW, limit=3)
    state = db.rows("pm_rfi_email_scan_state")[0]
    assert _ts(state["emails_checkpoint"]) == _ts("2026-10-18T11:02:00+00:00")


def test_queued_runs_stop_once_sync_is_turned_off():
    db = _db(pm_external_links=[_link(sync_enabled=False)])
    s = _settings()
    svc.enqueue_run(db, PROJECT, "daily", scheduled_for=datetime(2026, 10, 18, 13, 30, tzinfo=timezone.utc))
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    session = FakeSession()
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: session) == "failed"
    assert session.calls == [] and "turned off" in db.rows("pm_rfi_sync_runs")[0]["error"]


class _RowStore:
    def __init__(self, row=None):
        self.row = dict(row) if row else None
        self.logins = []

    def load(self):
        return dict(self.row) if self.row else None

    def save_cookies(self, account, cookies):
        self.row = {**(self.row or {}), "account": account, "cookies": cookies}

    def record_login(self, *, ok, error):
        self.logins.append(ok)
        self.row = {**(self.row or {}), "last_login_attempt_at": datetime.now(timezone.utc).isoformat()}
        return dict(self.row)

    def touch(self):
        pass


def test_us02_logins_honor_the_shared_procore_lock():
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    host, shared = _RowStore({"cookies": []}), _RowStore({"locked_until": until})
    store = svc._SharedLockStore(host, shared)
    session = pc.ProcoreSession(pc.ProcoreConfig("a@example.com", "pw"), store, app_host="us02.procore.com")
    with pytest.raises(pc.ProcoreLoginLocked):
        session.ensure_session()
    assert host.logins == [] and shared.logins == []
    # A recent attempt on the shared row spaces a us02 login too.
    shared.row = {"last_login_attempt_at": datetime.now(timezone.utc).isoformat()}
    with pytest.raises(pc.ProcoreUnavailable):
        session.ensure_session()
    store.record_login(ok=False, error="x")
    assert host.logins == [False] and shared.logins == [False]


def test_a_second_session_expiry_on_rfi_reads_is_not_retried():
    import httpx

    def handler(request: httpx.Request):
        return httpx.Response(401)

    session = _mock_session(handler)
    session.ensure_session = lambda: None  # the login "worked"
    with pytest.raises(pc.ProcoreSessionRejected):
        session.list_rfis("3167322", "6333")


def test_session_rejected_fails_the_run_without_a_retry():
    db = _db()
    s = _settings()

    class Rejected(FakeSession):
        def list_rfis(self, *a):
            raise pc.ProcoreSessionRejected("rejected")

    svc.enqueue_run(db, PROJECT, "manual")
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: Rejected()) == "failed"


def test_procore_files_get_safe_content_types(monkeypatch):
    from app.services import storage

    types = []
    monkeypatch.setattr(storage, "upload_file", lambda path, content, ctype, **k: types.append(ctype))
    db = _db()
    svc._download_to_documents(db, PROJECT, "Const-1", "f1", "evil.html", "https://storage.procore.com/x",
                               FakeSession(), _settings(), {})
    svc._download_to_documents(db, PROJECT, "Const-1", "f2", "plan.pdf", "https://storage.procore.com/y",
                               FakeSession(), _settings(), {})
    assert types == ["application/octet-stream", "application/pdf"]


def test_view_in_procore_link_is_always_rebuilt():
    detail = {"id": 2, "link": "javascript://app.procore.com/1/project/rfi/show/2%0aalert(1)"}
    assert svc._rfi_url(detail, "app.procore.com", "1") == "https://app.procore.com/1/project/rfi/show/2"
    assert pc.parse_project_url("javascript://app.procore.com/1/project/rfi/show/2%0aalert(1)") is None
    assert pc.parse_project_url("http://app.procore.com/1/project/home") is None


def test_email_auto_link_needs_a_procore_sender_or_a_matching_storage_link():
    plain = (
        "Fwd: see https://app.procore.com/3167322/project/rfi/show/27914290 thanks"
    )
    db = _db(pm_external_links=[], ingested_emails=[_email("e1", plain)])
    stats = svc.scan_emails(db, _settings(), NOW)
    assert stats["auto_linked_projects"] == 0 and svc.get_link(db, PROJECT) is None
    db = _db(pm_external_links=[], ingested_emails=[
        {**_email("e1", plain), "from_address": "Cobblestone_Construction_notifications@procoretech.com"},
    ])
    assert svc.scan_emails(db, _settings(), NOW)["auto_linked_projects"] == 1
    # The real Const-198 notification carries a matching storage link.
    db = _db(pm_external_links=[], ingested_emails=[_email("e1", _text("email_closed_const198.txt"))])
    assert svc.scan_emails(db, _settings(), NOW)["auto_linked_projects"] == 1


# ── Live dev test follow-ups (2026-10-19) ─────────────────────────────────────


def test_a_backfill_finished_on_a_later_attempt_announces_every_imported_rfi():
    # The live backfill crashed twice (laptop asleep) and finished on attempt 3:
    # the notice must count the rows earlier attempts saved, not just the last.
    earlier = {"id": "old1", "project_id": PROJECT, "source": "procore", "rfi_number": None,
               "external_id": "999", "external_number": "Const-1", "external_revision": "0",
               "status": "closed", "external_updated_at": "2026-10-01T00:00:00+00:00"}
    linked = {"id": "g40", "project_id": PROJECT, "source": "internal", "rfi_number": 40,
              "external_id": "998", "external_number": "Const-2", "status": "closed",
              "external_updated_at": "2026-10-01T00:00:00+00:00"}
    db = _db(rfis=[earlier, linked])
    svc.enqueue_run(db, PROJECT, "manual")
    s = _settings()
    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    bells = _Bells()
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: FakeSession(), notify=bells) == "done"
    run = db.rows("pm_rfi_sync_runs")[0]
    assert run["stats"]["created"] == 5
    assert run["stats"]["imported_total"] == 7 and run["stats"]["linked_total"] == 1
    assert "imported 7 RFIs, 1 matched to G3's own RFIs" in bells.calls[0][3]


def test_a_dropped_database_connection_is_retried_not_failed():
    db = _db()
    svc.enqueue_run(db, PROJECT, "manual")
    s = _settings()

    class Offline(FakeSession):
        def list_rfis(self, *a):
            raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")

    claimed = svc.claim_next(db, s, datetime.now(timezone.utc))
    assert svc.process_run(db, claimed, s, session_factory=lambda h, st: Offline()) == "queued"
    run = db.rows("pm_rfi_sync_runs")[0]
    assert run["status"] == "queued" and run["next_attempt_at"]
    assert "connection dropped" in run["error"]


def test_an_abandoned_run_marks_the_link_failed():
    db = _db()
    s = _settings(pm_rfi_sync_max_attempts=2)
    past = svc._iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    db.tables["pm_rfi_sync_runs"] = [
        {"id": "r1", "project_id": PROJECT, "trigger": "manual", "status": "running", "lease_owner": "w1",
         "lease_until": past, "attempts": 2, "created_at": "2026-10-18T10:00:00+00:00"},
    ]
    assert svc.claim_next(db, s, datetime.now(timezone.utc)) is None
    link = svc.get_link(db, PROJECT)
    assert link["last_sync_status"] == "failed" and "stopped repeatedly" in link["last_error"]
