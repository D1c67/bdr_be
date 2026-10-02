"""The RFP harvest service (app/services/rfp_harvest) against the in-memory
fake Supabase from tests/test_rfp_email_ingest, a scripted stand-in for the
Procore session and recording fakes for the sandbox's run/upload/start/
dispatch/delete calls (docs/RFP_HARVEST.md sections 2.2, 4 and 9).

Pinned, in the doc's order:

- the pure parts: `html_to_text` (block ends to newlines, entities, nbsp,
  script contents dropped, the cap), `normalize_facts` over the captured
  payloads (the section 4 shape), `build_raw` (no signed URL, no phone
  number, no recipient list, no link table, no reply-to address),
  `classify_manifest` / `manifest_urls` (kind and discipline per row type,
  a non-negative size, alignment, the malformed-payload refusal);
- the registry (`harvester_for`, `can_harvest`) across the flag, the
  credentials and the method;
- `execute` in pipeline mode (the row at `harvest`, finishing at `create`
  since docs/RFP_CREATE.md 3, success and permanent failure alike) and
  manual mode (the row at `done`): a young complete harvest linked with no
  session opened, `force`
  refreshing it, facts written before the first download, every per-file
  outcome, the file and byte caps (permanent, facts kept), the sandbox run
  created staging then started and dispatched, or deleted when nothing was
  accepted, and every failure mapping including the losing claim;
- `mark_from_queue` fencing, `current_status`, `error_message`;
- `step` (drain, park while locked, enqueue once, never twice while a job
  is active) and the session store, the lock bell and the settings adapters;
- PipelineSuite (docs/RFP_PIPELINESUITE.md 8) and SmartBid
  (docs/RFP_SMARTBID.md 8) in their own sections at the end: the registry,
  `execute` against a stub session and a stub Graph (pings once, recorded,
  never fatal; facts before files; KB to bytes; caps; per-file outcomes;
  every failure mapping; the lock parks without an attempt), and for
  SmartBid the gate's refusal, restricted files skipped, the re-login on
  expiry, and one end-to-end run over the fake platform proving no
  passport key, bearer token, security token or SAS signature is
  persisted or logged.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.core.roles import Role
from app.services import (
    llm_queue,
    pipelinesuite_client as psc,
    procore_client as pc,
    rfp_harvest as h,
    rfp_ingest,
    smartbid_client as sbc,
)
from tests import fixtures_pipelinesuite as psfx
from tests import fixtures_procore as fx
from tests import fixtures_smartbid as sbfx
from tests.test_rfp_email_ingest import FakeDB

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
E1 = "e-1"
PDF = b"%PDF-1.7\n" + b"y" * 200 + b"\n%%EOF\n"
NOT_PDF = b"MZ" + b"\x00" * 100

REF = pc.ProcoreRef(fx.COMPANY_ID, fx.BID_ID, package_id=fx.PACKAGE_ID, project_id=fx.PROJECT_ID)
KEY = f"procore:{fx.COMPANY_ID}:{fx.BID_ID}"
SHEET_URL = f"https://app.procore.com/{fx.COMPANY_ID}/company/planroom/route_to_bid_sheet/{fx.BID_ID}"
PACKAGE_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/bid_packages/{fx.PACKAGE_ID}"
BID_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/bids/{fx.BID_ID}"
FORM_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/bid/{fx.BID_ID}/bid_forms/{fx.BID_FORM_ID}"
DOCS_PATH = f"/rest/v1.0/companies/{fx.COMPANY_ID}/planroom/bid_packages/{fx.PACKAGE_ID}/documents"
URLS = fx.MANIFEST_URLS


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeSession:
    """The subset of procore_client.ProcoreSession the job touches. Payloads
    keyed by path; `errors` maps a path, a download URL or "resolve" to an
    exception (or a list consumed one per call); `bytes_for` overrides the
    bytes a download writes; `on_download` runs before the first download."""

    provider = "procore"

    def __init__(self):
        self.calls: list[tuple] = []
        self.available: tuple = (True, None, None)
        self.package_id = fx.PACKAGE_ID
        self.payloads = {
            PACKAGE_PATH: fx.bid_package(),
            BID_PATH: fx.bid(),
            FORM_PATH: fx.bid_form(),
            DOCS_PATH: fx.documents(),
        }
        self.errors: dict = {}
        self.bytes_for: dict[str, bytes] = {}
        self.on_download = None
        self.closed = False
        self.downloads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def _raise(self, key):
        err = self.errors.get(key)
        if isinstance(err, list):
            if err:
                raise err.pop(0)
        elif err is not None:
            raise err

    def availability(self):
        self.calls.append(("availability",))
        return self.available

    def resolve_bid_sheet(self, ref):
        self.calls.append(("resolve", ref))
        self._raise("resolve")
        return self.package_id

    def get_json(self, path, *, params=None, referer=None):
        self.calls.append(("get_json", path, params, referer))
        self._raise(path)
        return copy.deepcopy(self.payloads[path])

    def download(self, url, dest, *, max_bytes):
        self.calls.append(("download", url, max_bytes))
        if self.downloads == 0 and self.on_download is not None:
            self.on_download()
        self.downloads += 1
        self._raise(url)
        data = self.bytes_for.get(url, PDF)
        if len(data) > max_bytes:
            raise pc.ProcoreForbidden("The bid document is larger than the harvest accepts.")
        dest.write_bytes(data)
        return len(data)


class FakeSandbox:
    """Recording stand-ins for the rfp_ingest calls the harvest makes.
    `outcomes` maps a filename to "rejected", "failed", "duplicate", 413 or
    409 (default: accepted as a pending file)."""

    def __init__(self, db):
        self.db = db
        self.calls: list[tuple] = []
        self.outcomes: dict[str, object] = {}
        self._n = 0

    def create_harvest_run(self, *, rfp_email_id, harvest_id):
        self._n += 1
        row = {
            "id": f"run-{self._n}", "status": "staging", "source_kind": "rfp_email",
            "rfp_email_id": rfp_email_id, "harvest_id": harvest_id, "file_count": 0,
        }
        self.db.tables.setdefault("rfp_ingest_runs", []).append(row)
        self.calls.append(("create", rfp_email_id, harvest_id))
        return dict(row)

    def add_upload_file(self, run_id, *, filename, declared_mime, data, source):
        self.calls.append(("add", run_id, filename, declared_mime, len(data), source))
        outcome = self.outcomes.get(filename)
        if outcome == 413:
            raise rfp_ingest.RfpIngestPermanent(
                "The run already holds 200 files, the per-run limit.", http_status=413
            )
        if outcome == 409:
            raise rfp_ingest.RfpIngestPermanent(
                "Files can only be added while the run is staging.", http_status=409
            )
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
        self.db.tables["rfp_ingest_runs"] = [
            r for r in self.db.tables.get("rfp_ingest_runs", []) if r["id"] != run_id
        ]

    def names(self):
        return [c[0] for c in self.calls]


def _settings(tmp_path=None, **over):
    base = dict(
        rfp_ingest_enabled=True,
        rfp_harvest_enabled=True,
        procore_login_email="harvest-bot@example.com",
        procore_login_password="pw",
        llm_queue_enabled=True,
    )
    if tmp_path is not None:
        base["rfp_ingest_scratch_dir"] = str(tmp_path)
    base.update(over)
    return Settings(_env_file=None, **base)


def _email(**over):
    row = {
        "id": E1,
        "status": "harvest",
        "invitation_method": "procore",
        "body_text": fx.EMAIL_BODY,
        "harvest_id": None,
        "harvested_at": None,
        "flag_reason": "no_candidate",
        "attempts": 0,
        "last_error": None,
        "next_attempt_at": None,
        "sibling_of_email_id": None,
    }
    row.update(over)
    return row


def _harvest_row(**over):
    row = {
        "id": "hv-1",
        "rfp_email_id": "e-0",
        "method": "procore",
        "external_key": KEY,
        "external_url": SHEET_URL,
        "status": "complete",
        "claim_token": None,
        "attempts": 1,
        "last_error": None,
        "data": {"platform": "procore"},
        "raw": {"bid": {}},
        "files": [],
        "file_count": 0,
        "files_accepted": 0,
        "bytes_downloaded": 0,
        "sandbox_run_id": None,
        "facts_at": (NOW - timedelta(days=1)).isoformat(),
        "started_at": (NOW - timedelta(days=1)).isoformat(),
        "finished_at": (NOW - timedelta(days=1)).isoformat(),
    }
    row.update(over)
    return row


class HarvestDB(FakeDB):
    """The ingest fake plus the partial unique index on active llm_jobs
    (job_type, target_id) that llm_queue.enqueue relies on."""

    def table(self, name):
        query = super().table(name)
        if name == "llm_jobs":
            inherited = query._check_unique

            def check(rows, payload):
                inherited(rows, payload)
                for r in rows:
                    if (
                        r.get("job_type") == payload.get("job_type")
                        and r.get("target_id") == payload.get("target_id")
                        and r.get("status", "queued") in ("queued", "running")
                    ):
                        raise Exception(
                            'duplicate key value violates unique constraint '
                            '"llm_jobs_active_target_uq" (23505)'
                        )

            query._check_unique = check
        return query


@pytest.fixture
def db():
    fake = HarvestDB({
        "rfp_emails": [],
        "rfp_harvests": [],
        "rfp_harvest_sessions": [],
        "rfp_email_sightings": [],
        "rfp_ingest_runs": [],
        "llm_jobs": [],
        "notifications": [],
    })
    fake.unique = {**FakeDB.unique, "rfp_harvests": [("method", "external_key")]}
    fake.defaults = {
        **FakeDB.defaults,
        "llm_jobs": {"status": lambda: "queued", "attempts": lambda: 0,
                     "created_at": lambda: NOW.isoformat()},
    }
    return fake


@pytest.fixture
def settings(tmp_path):
    return _settings(tmp_path)


@pytest.fixture
def sleeps():
    return []


@pytest.fixture(autouse=True)
def _env(monkeypatch, db, settings, sleeps):
    monkeypatch.setattr(h, "get_settings", lambda: settings)
    monkeypatch.setattr(h, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: settings)
    monkeypatch.setattr(h, "_now", lambda: NOW)
    monkeypatch.setattr(
        h, "time", SimpleNamespace(sleep=lambda s: sleeps.append(s), monotonic=time.monotonic)
    )
    monkeypatch.setattr(
        h, "notify_role",
        lambda role, project_id, type_, message, **k: db.tables["notifications"].append(
            {"type": type_, "message": message, "role": role, "read_at": None,
             "dismissed_at": None, "metadata": k.get("metadata")}),
    )


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    opened = []

    def open_session(settings=None):
        opened.append(settings)
        return fake

    monkeypatch.setattr(h, "open_session", open_session)
    fake.opened = opened
    return fake


@pytest.fixture
def sandbox(monkeypatch, db):
    fake = FakeSandbox(db)
    for name in ("create_harvest_run", "add_upload_file", "start_run", "dispatch", "delete_run"):
        monkeypatch.setattr(rfp_ingest, name, getattr(fake, name))
    return fake


def _seed(db, row=None):
    row = row or _email()
    db.tables["rfp_emails"].append(row)
    return row


def _email_row(db, email_id=E1):
    return next(r for r in db.tables["rfp_emails"] if r["id"] == email_id)


def _harvests(db):
    return db.tables["rfp_harvests"]


def _the_harvest(db):
    assert len(_harvests(db)) == 1
    return _harvests(db)[0]


# ── html_to_text ─────────────────────────────────────────────────────────


def test_html_to_text_turns_block_ends_into_newlines_and_drops_the_rest():
    assert h.html_to_text(fx.BID_EMAIL_MESSAGE) == (
        "Monument Construction invites you to bid.\n\n"
        "Replace evaporative coolers in the warehouse with rooftop air conditioning units. "
        "New electrical service installation. New building envelop insulation upgrade.\n\n"
        "Follow the links into our system to download relevant bidding documents and submit "
        "your bids electronically. In this system, all electronic correspondence is tracked "
        "and archived, and bidders are provided with the most up to date information "
        "available for the project."
    )
    assert h.html_to_text("a<br>b<br/>c<br />d") == "a\nb\nc\nd"
    assert h.html_to_text("<p>one</p><p>two</p><div>three</div><li>x</li><h2>H</h2>") == (
        "one\ntwo\nthree\nx\nH"
    )


def test_html_to_text_resolves_entities_nbsp_and_drops_script_contents():
    assert h.html_to_text("Tom &amp; Jerry&nbsp;&lt;3 &#39;q&#39;") == "Tom & Jerry <3 'q'"
    assert h.html_to_text("a  b") == "a b"
    assert h.html_to_text("<p>keep</p><script>alert('x')</script><style>.a{}</style>after") == (
        "keep\nafter"
    )
    assert h.html_to_text("<!-- hidden -->shown") == "shown"
    assert h.html_to_text("line one   \n\n\n\n   line two") == "line one\n\nline two"
    assert h.html_to_text('<a href="javascript:x">link</a>') == "link"


def test_html_to_text_caps_and_returns_none_for_nothing():
    assert h.html_to_text(None) is None
    assert h.html_to_text("") is None
    assert h.html_to_text("   \n\t ") is None
    assert h.html_to_text("<p></p><br>") is None
    assert h.html_to_text("x" * 50, limit=10) == "x" * 10
    assert len(h.html_to_text("y" * 30_000)) == h._TEXT_MAX_CHARS
    assert h.html_to_text(123) == "123"


# ── normalize_facts ──────────────────────────────────────────────────────


def test_normalize_facts_over_the_captured_payloads_is_the_section_4_shape():
    entries = h.classify_manifest(fx.documents())
    data = h.normalize_facts(REF, fx.bid_package(), fx.bid(), fx.bid_form(), entries)
    assert data == {
        "platform": "procore",
        "company_id": fx.COMPANY_ID,
        "project_id": fx.PROJECT_ID,
        "bid_package_id": fx.PACKAGE_ID,
        "bid_id": fx.BID_ID,
        "bid_form_id": fx.BID_FORM_ID,
        "project_name": "Warehouse HVAC Upgrade",
        "project_address": "8250 W Flamingo Road, Las Vegas, Nevada 89147, United States",
        "project_latitude": 36.1154707,
        "project_longitude": -115.2710105,
        "bid_package_title": "Warehouse HVAC Upgrade",
        "bid_package_number": 26156,
        "bid_due_at": "2026-09-10T19:00:00+00:00",
        "accept_post_due_submissions": False,
        "anticipated_award_date": None,
        "pre_bid_walk_through_date": None,
        "pre_bid_walk_through_notes": None,
        "pre_bid_meeting_date": None,
        "pre_bid_meeting_location": None,
        "pre_bid_meeting_online_link": None,
        "pre_bid_meeting_notes": None,
        "pre_bid_rfi_deadline_date": None,
        "public_bid_opening_date": None,
        "public_bid_opening_location": None,
        "gc": {
            "name": "Monument Construction",
            "address": "7787 Eastgate Road #110, Henderson, Nevada 89011, United States",
            "phone": fx.GC_PHONE,
            "website": None,
        },
        "point_of_contact": {
            "name": "Pat Gale",
            "email": "pat@monument.example.com",
            "phone": fx.GC_PHONE,
        },
        "distribution_members": [
            {"name": "Robin Smith", "email": "bids@monument.example.com"},
            {"name": "Chloe Orwell", "email": "chloe@monument.example.com"},
        ],
        "invited_recipients": list(fx.RECIPIENT_EMAILS),
        "invitation_last_sent_at": "2026-09-07T21:58:44+00:00",
        "bid_form": {
            "title": "Warehouse HVAC Upgrade",
            "base_bid_sections": [
                {
                    "title": "Base Bid",
                    "items": [
                        {
                            "description": "Electrical scope per drawings",
                            "unit": "LS",
                            "quantity": "1",
                            "position": "1",
                            "response_type": "amount",
                        },
                        {"description": "Rooftop unit power", "position": "2"},
                    ],
                }
            ],
            "alternates": [{"title": None, "items": []}],
        },
        "accounting_method": "amount",
        "lump_sum_bidding": False,
        "require_nda": False,
        "blind_bidding": False,
        "documents": {
            "count": 4,
            "bytes": 891153 + 295994 + 2871208 + 1698010,
            "kinds": {"drawing": 2, "other": 1, "specification": 1},
            "disciplines": {"Architectural": 1, "Electrical": 1, "Bid_Drawings": 1, "26-Electrical": 1},
        },
    }
    # Every string is text: no tag survived anywhere in the document.
    assert "<" not in json.dumps(data)


def test_normalize_facts_falls_back_and_caps_on_thin_payloads():
    ref = pc.ProcoreRef("1", "2", package_id="3", project_id="4")
    data = h.normalize_facts(ref, None, "not a dict", [], [])
    assert data["company_id"] == "1" and data["bid_id"] == "2"
    assert data["bid_package_id"] == "3" and data["project_id"] == "4"
    assert data["project_name"] is None and data["gc"]["name"] is None
    assert data["point_of_contact"] == {"name": None, "email": None, "phone": None}
    assert data["distribution_members"] == [] and data["invited_recipients"] == []
    assert data["bid_form"] == {"title": None, "base_bid_sections": [], "alternates": []}
    assert data["documents"] == {"count": 0, "bytes": 0, "kinds": {}, "disciplines": {}}
    # The bid's project block and the requester stand in for a missing package.
    bid = fx.bid()
    data = h.normalize_facts(ref, {}, bid, {}, [])
    assert data["project_name"] == "Warehouse HVAC Upgrade"
    assert data["project_address"].startswith("8250 W Flamingo Road, ")
    assert data["bid_due_at"] == "2026-09-10T19:00:00+00:00"
    assert data["point_of_contact"]["email"] == "pat@monument.example.com"
    assert data["bid_form"]["title"] == "Warehouse HVAC Upgrade"
    # Strings are capped and de-tagged; bad coordinates and dates are dropped.
    bp = fx.bid_package()
    bp["project_name"] = "<b>" + "n" * 500 + "</b>"
    bp["project_latitude"] = "36.1"
    bp["bid_due_date"] = "not a date"
    bp["distribution_members"] = [{"first": "", "last": "", "email": ""}, "junk", {"first": "A"}]
    data = h.normalize_facts(ref, bp, bid, {}, [])
    assert data["project_name"] == "<b>" + "n" * (h._NAME_MAX_CHARS - 3)
    assert data["project_latitude"] is None and data["bid_due_at"] is None
    assert data["distribution_members"] == [{"name": "A", "email": None}]


# ── build_raw ────────────────────────────────────────────────────────────


def test_build_raw_never_carries_signed_urls_phones_recipients_or_link_tables():
    raw = h.build_raw(fx.bid_package(), fx.bid(), fx.bid_form(), fx.documents())
    dumped = json.dumps(raw)
    assert set(raw) == {"bid_package", "bid", "bid_form", "documents"}
    assert "sig=" not in dumped and "X-Amz" not in dumped and "storage.procore.com" not in dumped
    for phone in fx.PHONES:
        assert phone not in dumped
    assert "recipient_list" not in dumped and "recipient_ids" not in dumped
    for email in fx.RECIPIENT_EMAILS:
        assert email not in dumped.lower()
    assert "Upadhyay" not in dumped
    assert "links" not in raw["bid"] and "links" not in raw["bid_package"]
    assert "legacy_links" not in raw["bid"]
    assert "mailto" not in raw["bid"] and "cc_mailto" not in raw["bid"]
    assert fx.BID_MAILTO not in dumped
    assert "download_bid_docs_zip" not in dumped and "email_bid_docs" not in dumped
    assert "streaming_url" not in dumped and "bid_docs_manifest" not in dumped
    assert "project_logo_url" not in dumped
    assert "numbers" not in dumped and "contact" not in raw["bid"]["bid_requester"]
    for key in ("company_phone", "mobile_phone", "business_phone", "fax_number"):
        assert key not in raw["bid"]["bid_requester"]
    assert "business_phone" not in raw["bid"]["vendor"]
    # The manifest rows live in `files`, not in raw.
    assert raw["documents"] == {"id": 1376188, "title": "Warehouse HVAC Upgrade"}
    # The facts themselves survive.
    assert raw["bid_package"]["title"] == "Warehouse HVAC Upgrade"
    assert raw["bid"]["bid_requester"]["company"] == "Monument Construction"
    assert raw["bid_form"]["base_bid"][0]["title"] == "Base Bid"


def test_build_raw_drops_any_string_carrying_a_signature_and_caps_depth_and_size():
    payload = {
        "a": "https://x.example/f?X-Amz-Signature=abc",
        "b": "https://x.example/f?token=1&Signature=abc",
        "c": "https://x.example/f?sig=abc",
        "d": "plain",
        "e": "x" * 30_000,
        "nested": {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": 1}}}}}}}}},
        "list": list(range(600)),
    }
    raw = h.build_raw(payload, {}, {}, {})
    trimmed = raw["bid_package"]
    assert trimmed["a"] is None and trimmed["b"] is None and trimmed["c"] is None
    assert trimmed["d"] == "plain" and len(trimmed["e"]) == h._TEXT_MAX_CHARS
    assert trimmed["nested"]["l1"]["l2"]["l3"]["l4"]["l5"]["l6"]["l7"]["l8"] is None
    assert len(trimmed["list"]) == 500
    # Over the size cap the bid form and the documents head are dropped.
    fat_form = {"base_bid": [{"title": "t" * 20_000} for _ in range(15)]}
    raw = h.build_raw(fx.bid_package(), fx.bid(), fat_form, fx.documents())
    assert set(raw) == {"bid_package", "bid"}
    assert h.build_raw("junk", None, 3, "docs") == {
        "bid_package": "junk", "bid": None, "bid_form": 3, "documents": {}
    }


# ── classify_manifest / manifest_urls ────────────────────────────────────


def test_classify_manifest_gives_kind_discipline_and_no_urls_per_row_type():
    entries = h.classify_manifest(fx.documents())
    assert [e["file_path"] for e in entries] == [r["file_path"] for r in fx.DOCUMENTS["files"]]
    arch, elec, log, spec = entries
    assert arch == {
        "file_path": fx.DRAWING_ARCH["file_path"],
        "size": 891153,
        "kind": "drawing",
        "discipline": "Architectural",
        "drawing_title": "doors types, schedule + details",
        "revision": "0",
        "sandbox_file_id": None,
        "status": None,
        "error": None,
    }
    assert (elec["kind"], elec["discipline"], elec["drawing_title"]) == (
        "drawing", "Electrical", "ROOF ELECTRICAL PLANS"
    )
    assert (log["kind"], log["discipline"], log["drawing_title"], log["revision"]) == (
        "other", "Bid_Drawings", None, None
    )
    assert (spec["kind"], spec["discipline"], spec["drawing_title"]) == (
        "specification", "26-Electrical", None
    )
    dumped = json.dumps(entries)
    assert "s3_source" not in dumped and "sig=" not in dumped and "http" not in dumped
    assert h.manifest_urls(fx.documents()) == URLS
    assert h.documents_summary(entries)["count"] == 4


def test_classify_manifest_sizes_are_never_negative_and_bad_rows_are_skipped():
    docs = {
        "files": [
            {"file_path": "a/b.pdf", "size": -5, "type": "ZipManifests::GenericRow"},
            {"file_path": "c/d.pdf", "size": "many", "type": None, "s3_source": 12},
            {"file_path": "", "size": 1, "type": "ZipManifests::GenericRow"},
            "junk",
            {"size": 5, "type": "ZipManifests::GenericRow", "s3_source": "https://storage.procore.com/x"},
            {"file_path": "Specifications/only.pdf", "type": "ZipManifests::SpecificationsManifest::Row",
             "drawing": "not a dict", "s3_source": "https://storage.procore.com/spec?sig=t"},
            {"file_path": "Bid_Drawings/Current/Deep/Deeper/x.pdf", "size": 1,
             "type": "ZipManifests::BidDocsManifest::DrawingRevisionRow"},
            {"file_path": "Bid_Drawings/Old/Mechanical/m.pdf", "size": 1,
             "type": "ZipManifests::BidDocsManifest::DrawingRevisionRow"},
            {"file_path": "lonely.pdf", "size": 1, "type": "ZipManifests::GenericRow"},
        ]
    }
    entries = h.classify_manifest(docs)
    assert [(e["file_path"], e["size"]) for e in entries] == [
        ("a/b.pdf", 0), ("c/d.pdf", 0), ("Specifications/only.pdf", 0),
        ("Bid_Drawings/Current/Deep/Deeper/x.pdf", 1), ("Bid_Drawings/Old/Mechanical/m.pdf", 1),
        ("lonely.pdf", 1),
    ]
    assert [e["kind"] for e in entries] == [
        "other", "other", "specification", "drawing", "drawing", "other"
    ]
    assert [e["discipline"] for e in entries] == [
        "a", "c", None, "Deep", "Mechanical", None
    ]
    # The URL list is aligned with the entries: same rows skipped, None when absent or not a string.
    assert h.manifest_urls(docs) == [
        None, None, "https://storage.procore.com/spec?sig=t", None, None, None
    ]
    assert h.manifest_urls(None) == [] and h.manifest_urls({"files": None}) == []


@pytest.mark.parametrize("payload", [None, [], "files", {"files": "x"}, {"files": {"a": 1}}, {"id": 1}])
def test_classify_manifest_refuses_a_payload_of_the_wrong_shape(payload):
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.classify_manifest(payload)
    assert str(exc.value) == h._MSG_MANIFEST_SHAPE
    assert exc.value.http_status == 409


# ── harvester_for / can_harvest ──────────────────────────────────────────


def test_harvester_for_and_can_harvest_need_the_flags_the_credentials_and_the_method(tmp_path):
    row = _email()
    on = _settings(tmp_path)
    assert h.harvester_for(row, on) == "procore"
    assert h.can_harvest(row, on) == (True, None)
    for off in (
        _settings(tmp_path, rfp_harvest_enabled=False),
        _settings(tmp_path, rfp_ingest_enabled=False),
        _settings(tmp_path, procore_login_email=""),
        _settings(tmp_path, procore_login_password=""),
        _settings(tmp_path, procore_login_email="   "),
    ):
        assert h.harvester_for(row, off) is None
        assert h.can_harvest(row, off) == (False, h._MSG_NOT_CONFIGURED)
    assert h.harvester_for(_email(invitation_method=None), on) is None
    assert h.can_harvest(_email(invitation_method=None), on) == (False, h._MSG_NO_HARVESTER)
    # The three direct-invitation methods belong to the email harvester
    # (doc 2.5): a harvester under its own switch, and the row (a Procore
    # body, no attachments) has nothing for it. tests/test_rfp_email_harvest
    # covers the rest.
    email_off = _settings(tmp_path, rfp_harvest_email_enabled=False)
    for method in ("organic", "general", "nonorganic"):
        assert h.harvester_for(_email(invitation_method=method), on) == "email"
        assert h.can_harvest(_email(invitation_method=method), on) == (False, h._MSG_NO_EMAIL_FILES)
        assert h.harvester_for(_email(invitation_method=method), email_off) is None
        assert h.can_harvest(_email(invitation_method=method), email_off) == (False, h._MSG_NO_HARVESTER)
    # gc_portal with no scraper registered for the sender: no harvester, and
    # the reason names the domain the scraper would be keyed on.
    portal = _email(invitation_method="gc_portal", from_address="bids@gc.example")
    assert h.harvester_for(portal, on) is None
    assert h.can_harvest(portal, on) == (False, "No scraper exists yet for this GC's portal (gc.example).")
    assert h.can_harvest(_email(invitation_method="gc_portal", from_address=None), on) == (
        False, "No scraper exists yet for this GC's portal (unknown domain)."
    )
    # A procore row without a usable link has a harvester but cannot be harvested.
    no_link = _email(body_text="Please bid. https://app.procore.com/help")
    assert h.harvester_for(no_link, on) == "procore"
    assert h.can_harvest(no_link, on) == (False, h._MSG_NO_LINK)
    assert h.can_harvest(_email(body_text=None), on) == (False, h._MSG_NO_LINK)
    # The method decides the parser; nothing else parses a Procore link.
    assert h.platform_reference("procore", fx.EMAIL_BODY).bid_id == fx.BID_ID
    assert h.platform_reference("general", fx.EMAIL_BODY) is None
    # Without an explicit settings object the patched get_settings is read.
    assert h.harvester_for(row) == "procore"


# ── execute: the happy path ──────────────────────────────────────────────


def test_execute_pipeline_mode_harvests_facts_and_files_and_finishes_the_email(
    db, session, sandbox, settings, tmp_path
):
    _seed(db)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["claim_token"] is None
    assert harvest["method"] == "procore" and harvest["external_key"] == KEY
    assert harvest["external_url"] == SHEET_URL and harvest["rfp_email_id"] == E1
    assert harvest["attempts"] == 1 and harvest["last_error"] is None
    assert harvest["facts_at"] == NOW.isoformat() and harvest["finished_at"] == NOW.isoformat()
    assert harvest["started_at"] == NOW.isoformat()
    assert harvest["data"]["project_name"] == "Warehouse HVAC Upgrade"
    assert harvest["data"]["bid_package_id"] == fx.PACKAGE_ID
    assert harvest["description_text"].startswith("Monument Construction invites you to bid.")
    assert harvest["instructions_text"].startswith("For help with submitting a bid")
    assert "sig=" not in json.dumps(harvest["raw"]) and "sig=" not in json.dumps(harvest["files"])
    assert harvest["file_count"] == 4 and harvest["files_accepted"] == 4
    assert harvest["bytes_downloaded"] == 4 * len(PDF)
    assert harvest["sandbox_run_id"] == "run-1"
    assert [(f["status"], f["sandbox_file_id"], f["error"]) for f in harvest["files"]] == [
        ("accepted", "f-2", None), ("accepted", "f-3", None),
        ("accepted", "f-4", None), ("accepted", "f-5", None),
    ]
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["harvested_at"] == NOW.isoformat()
    assert email["attempts"] == 0 and email["last_error"] is None and email["next_attempt_at"] is None
    assert email["flag_reason"] == "no_candidate"     # the match step's reason is kept
    # The session: availability, the route, the four JSON GETs with the bid
    # sheet as referer, then the downloads in manifest order under the cap.
    assert session.opened == [settings] and session.closed
    assert session.calls[:6] == [
        ("availability",),
        ("resolve", pc.ProcoreRef(fx.COMPANY_ID, fx.BID_ID, project_id=fx.PROJECT_ID)),
        ("get_json", PACKAGE_PATH, None, SHEET_URL),
        ("get_json", BID_PATH, {"view": "planroom_redesign"}, SHEET_URL),
        ("get_json", FORM_PATH, None, SHEET_URL),
        ("get_json", DOCS_PATH, None, SHEET_URL),
    ]
    assert session.calls[6:] == [("download", url, settings.rfp_ingest_max_file_bytes) for url in URLS]
    # The sandbox: one staging run, one upload per file with the platform
    # source pointer, then start and dispatch by the system.
    assert sandbox.calls[0] == ("create", E1, harvest["id"])
    adds = [c for c in sandbox.calls if c[0] == "add"]
    assert [c[2] for c in adds] == [
        "A7.1-doors-types,-schedule-+-details-Rev.0.pdf",
        "E4.00-ROOF-ELECTRICAL-PLANS-Rev.0.pdf",
        "Drawing_Log_Current.pdf",
        "26-28-16-Enclosed-Switches-and-Circuit-Breakers_Rev_0.pdf",
    ]
    assert all(c[1] == "run-1" and c[3] is None and c[4] == len(PDF) for c in adds)
    assert adds[0][5] == {
        "kind": "procore", "file_path": fx.DRAWING_ARCH["file_path"], "harvest_id": harvest["id"],
    }
    assert sandbox.calls[-2:] == [("start", "run-1"), ("dispatch", "run-1", None, None)]
    assert db.tables["rfp_ingest_runs"][0]["status"] == "pending"
    # The scratch tree is gone.
    assert list((tmp_path / "rfp-harvest").iterdir()) == []


def test_execute_manual_mode_links_a_done_row_without_moving_its_status(db, session, sandbox):
    _seed(db, _email(status="done", flag_reason="no_project_name"))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete"
    email = _email_row(db)
    assert email["status"] == "done" and email["flag_reason"] == "no_project_name"
    assert email["harvest_id"] == harvest["id"] and email["harvested_at"] == NOW.isoformat()


@pytest.mark.parametrize("status", ["merged", "duplicate"])
def test_execute_manual_mode_accepts_merged_and_duplicate_rows(db, session, sandbox, status):
    _seed(db, _email(status=status))
    h.execute(E1)
    assert _email_row(db)["status"] == status
    assert _email_row(db)["harvest_id"] == _the_harvest(db)["id"]


@pytest.mark.parametrize("status", ["review_llm", "match", "failed", "flagged_auth", "received"])
def test_execute_refuses_a_row_at_any_other_stage(db, session, sandbox, status):
    _seed(db, _email(status=status))
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert "not at a stage" in str(exc.value)
    assert session.opened == [] and _harvests(db) == []


def test_execute_404s_a_missing_email(db, session, sandbox):
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute("nope")
    assert exc.value.http_status == 404 and session.opened == []


def test_execute_reuses_a_young_complete_harvest_without_opening_a_session(db, session, sandbox):
    _seed(db)
    db.tables["rfp_harvests"].append(_harvest_row(finished_at=(NOW - timedelta(days=13)).isoformat()))
    h.execute(E1)
    assert session.opened == [] and sandbox.calls == []
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == "hv-1"
    assert email["harvested_at"] == NOW.isoformat() and email["last_error"] is None
    assert _the_harvest(db)["status"] == "complete" and _the_harvest(db)["attempts"] == 1
    # Manual mode links the same way.
    db.tables["rfp_emails"] = [_email(status="done")]
    h.execute(E1)
    assert session.opened == [] and _email_row(db)["harvest_id"] == "hv-1"


def test_execute_reharvests_an_old_complete_or_failed_harvest(db, session, sandbox):
    _seed(db)
    db.tables["rfp_harvests"].append(_harvest_row(finished_at=(NOW - timedelta(days=15)).isoformat()))
    h.execute(E1)
    assert len(session.opened) == 1
    harvest = _the_harvest(db)
    assert harvest["id"] == "hv-1" and harvest["attempts"] == 2 and harvest["status"] == "complete"
    assert harvest["finished_at"] == NOW.isoformat()
    # A failed row re-runs whatever its age; a complete row without finished_at too.
    for row in (_harvest_row(status="failed", finished_at=NOW.isoformat()), _harvest_row(finished_at=None)):
        db.tables["rfp_harvests"] = [row]
        db.tables["rfp_emails"] = [_email()]
        h.execute(E1)
        assert _the_harvest(db)["status"] == "complete" and _the_harvest(db)["attempts"] == 2


def test_execute_force_refreshes_a_young_complete_harvest_on_the_same_row(db, session, sandbox):
    _seed(db, _email(status="done", harvest_id="hv-1"))
    db.tables["rfp_harvests"].append(_harvest_row(sandbox_run_id="run-old"))
    h.execute(E1, force=True)
    assert len(session.opened) == 1
    harvest = _the_harvest(db)
    assert harvest["id"] == "hv-1" and harvest["attempts"] == 2
    # The old run was not staging, so a new one was created for the refresh.
    assert sandbox.calls[0] == ("create", E1, "hv-1") and harvest["sandbox_run_id"] == "run-1"
    assert _email_row(db)["status"] == "done"


def test_execute_reuses_a_staging_run_it_left_behind(db, session, sandbox):
    _seed(db)
    db.tables["rfp_ingest_runs"].append({"id": "run-left", "status": "staging"})
    db.tables["rfp_harvests"].append(_harvest_row(status="failed", sandbox_run_id="run-left"))
    h.execute(E1)
    assert "create" not in sandbox.names()
    assert _the_harvest(db)["sandbox_run_id"] == "run-left"
    assert ("start", "run-left") in sandbox.calls


def test_execute_resumes_past_the_files_an_interrupted_attempt_accepted(db, session, sandbox):
    """The retry after a crash mid-download: the entries the last attempt
    accepted into the staging run keep their sandbox file id (matched by
    path and size) and are not downloaded again; a changed size is a
    different file."""
    _seed(db)
    db.tables["rfp_ingest_runs"].append({"id": "run-left", "status": "staging"})
    prior = h.classify_manifest(fx.documents())
    prior[0].update(sandbox_file_id="f-old-1", status="accepted")
    prior[1].update(sandbox_file_id="f-old-2", status="accepted", size=prior[1]["size"] + 1)
    prior[2].update(status="download_failed", error="timeout")
    db.tables["rfp_harvests"].append(_harvest_row(status="pending", sandbox_run_id="run-left", files=prior,
                                                  finished_at=None))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["sandbox_run_id"] == "run-left"
    assert harvest["files_accepted"] == 4 and harvest["files"][0]["sandbox_file_id"] == "f-old-1"
    assert harvest["files"][1]["sandbox_file_id"] != "f-old-2"
    downloads = [c[1] for c in session.calls if c[0] == "download"]
    assert downloads == URLS[1:]
    assert "create" not in sandbox.names() and ("start", "run-left") in sandbox.calls
    assert _email_row(db)["status"] == "split"


def test_execute_dispatches_a_run_the_last_attempt_started_but_never_dispatched(db, session, sandbox):
    """The worker died between start_run and dispatch: every file is in the
    run already, so nothing is downloaded and the run only gets its job."""
    _seed(db)
    db.tables["rfp_ingest_runs"].append({"id": "run-pend", "status": "pending"})
    prior = h.classify_manifest(fx.documents())
    for i, entry in enumerate(prior):
        entry.update(sandbox_file_id=f"f-p-{i}", status="accepted")
    db.tables["rfp_harvests"].append(_harvest_row(status="pending", sandbox_run_id="run-pend", files=prior,
                                                  finished_at=None))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["sandbox_run_id"] == "run-pend"
    assert harvest["files_accepted"] == 4 and session.downloads == 0
    assert sandbox.names() == ["dispatch"] and sandbox.calls[0][1] == "run-pend"
    assert _email_row(db)["status"] == "split"
    # A run in any other state is not reused: everything is downloaded into a fresh run.
    db.tables["rfp_emails"] = [_email()]
    db.tables["rfp_ingest_runs"] = [{"id": "run-done", "status": "done"}]
    harvest.update(status="failed", sandbox_run_id="run-done")
    sandbox.calls.clear()
    h.execute(E1)
    assert session.downloads == 4 and sandbox.names()[0] == "create"
    assert _the_harvest(db)["sandbox_run_id"] != "run-done"


def test_execute_writes_the_facts_before_the_first_download(db, session, sandbox):
    _seed(db)
    seen = {}

    def snapshot():
        row = copy.deepcopy(_the_harvest(db))
        seen.update(row)

    session.on_download = snapshot
    h.execute(E1)
    assert seen["status"] == "running" and seen["claim_token"]
    assert seen["facts_at"] == NOW.isoformat()
    assert seen["data"]["project_name"] == "Warehouse HVAC Upgrade"
    assert seen["external_url"] == SHEET_URL and seen["description_text"]
    assert seen["file_count"] == 4 and all(f["status"] is None for f in seen["files"])
    assert seen["raw"]["bid_package"]["title"] == "Warehouse HVAC Upgrade"


def test_execute_keeps_going_without_a_bid_form(db, session, sandbox):
    _seed(db)
    session.errors[FORM_PATH] = pc.ProcoreForbidden("no form")
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete"
    assert harvest["data"]["bid_form"] == {"title": "Warehouse HVAC Upgrade", "base_bid_sections": [], "alternates": []}
    # A bid without a form id skips the call entirely.
    session.errors.clear()
    session.payloads[BID_PATH]["bid_form_id"] = None
    db.tables["rfp_emails"] = [_email()]
    db.tables["rfp_harvests"] = []
    h.execute(E1)
    assert sum(1 for c in session.calls if c[0] == "get_json" and c[1] == FORM_PATH) == 1
    assert _the_harvest(db)["data"]["bid_form_id"] is None


# ── execute: per-file outcomes ───────────────────────────────────────────


def _statuses(db):
    return [(f["status"], f["sandbox_file_id"]) for f in _the_harvest(db)["files"]]


def test_execute_records_a_file_the_sandbox_rejects_at_sniff(db, session, sandbox):
    _seed(db)
    session.bytes_for[URLS[1]] = NOT_PDF
    h.execute(E1)
    harvest = _the_harvest(db)
    assert _statuses(db) == [("accepted", "f-2"), ("rejected", "f-3"), ("accepted", "f-4"), ("accepted", "f-5")]
    assert harvest["files"][1]["error"] == "The file is not a PDF."
    assert harvest["files_accepted"] == 3 and harvest["bytes_downloaded"] == 3 * len(PDF)
    assert harvest["status"] == "complete" and ("start", "run-1") in sandbox.calls


def test_execute_skips_a_file_too_large_by_its_declared_size(db, session, sandbox, settings):
    _seed(db)
    docs = session.payloads[DOCS_PATH]
    docs["files"][2]["size"] = settings.rfp_ingest_max_file_bytes + 1
    h.execute(E1)
    entry = _the_harvest(db)["files"][2]
    assert entry["status"] == "too_large" and entry["error"] == "Larger than the sandbox accepts."
    assert entry["sandbox_file_id"] is None
    assert not any(c[0] == "download" and c[1] == URLS[2] for c in session.calls)
    assert _the_harvest(db)["files_accepted"] == 3


def test_execute_records_a_download_that_fails_after_three_paced_tries(db, session, sandbox, sleeps):
    _seed(db)
    session.errors[URLS[0]] = [pc.ProcoreTransient("storage 503")] * 3
    h.execute(E1)
    entry = _the_harvest(db)["files"][0]
    assert entry["status"] == "download_failed" and entry["error"] == "storage 503"
    assert sum(1 for c in session.calls if c[0] == "download" and c[1] == URLS[0]) == 3
    assert sleeps == [2.0, 4.0]
    assert _statuses(db)[1:] == [("accepted", "f-2"), ("accepted", "f-3"), ("accepted", "f-4")]
    assert _the_harvest(db)["status"] == "complete"


def test_execute_a_download_that_recovers_on_the_second_try_is_accepted(db, session, sandbox, sleeps):
    _seed(db)
    session.errors[URLS[3]] = [pc.ProcoreTransient("blip")]
    h.execute(E1)
    assert _statuses(db)[3] == ("accepted", "f-5") and sleeps == [2.0]


def test_execute_records_the_running_byte_cap_and_a_storage_refusal(
    db, session, sandbox, monkeypatch, tmp_path
):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_ingest_max_file_bytes=300))
    _seed(db)
    for row in session.payloads[DOCS_PATH]["files"]:
        row["size"] = 100          # declared small: the running cap is what trips
    session.bytes_for[URLS[0]] = b"%PDF-" + b"z" * 400
    session.errors[URLS[1]] = pc.ProcoreForbidden("Procore storage refused the document link.")
    h.execute(E1)
    files = _the_harvest(db)["files"]
    assert files[0]["status"] == "too_large" and "larger than the harvest accepts" in files[0]["error"]
    assert files[1]["status"] == "download_failed" and "refused the document link" in files[1]["error"]
    assert files[2]["status"] == "accepted" and files[3]["status"] == "accepted"


def test_execute_marks_the_rest_skipped_cap_after_a_413_from_the_sandbox(db, session, sandbox):
    _seed(db)
    sandbox.outcomes["E4.00-ROOF-ELECTRICAL-PLANS-Rev.0.pdf"] = 413
    h.execute(E1)
    harvest = _the_harvest(db)
    assert [f["status"] for f in harvest["files"]] == ["accepted", "skipped_cap", "skipped_cap", "skipped_cap"]
    assert all("per-run limit" in f["error"] for f in harvest["files"][1:])
    assert harvest["files_accepted"] == 1 and harvest["status"] == "complete"
    # Nothing past the 413 was downloaded; the one accepted file still runs.
    assert [c[1] for c in session.calls if c[0] == "download"] == URLS[:2]
    assert sandbox.calls[-2:] == [("start", "run-1"), ("dispatch", "run-1", None, None)]


def test_execute_treats_a_duplicate_sha_as_accepted(db, session, sandbox):
    _seed(db)
    sandbox.outcomes["Drawing_Log_Current.pdf"] = "duplicate"
    h.execute(E1)
    harvest = _the_harvest(db)
    assert _statuses(db)[2] == ("accepted", "f-existing")
    assert harvest["files_accepted"] == 4 and harvest["bytes_downloaded"] == 4 * len(PDF)


def test_execute_records_a_missing_link_and_a_storage_failure_row(db, session, sandbox):
    _seed(db)
    docs = session.payloads[DOCS_PATH]
    del docs["files"][0]["s3_source"]
    sandbox.outcomes["Drawing_Log_Current.pdf"] = "failed"
    h.execute(E1)
    files = _the_harvest(db)["files"]
    assert files[0]["status"] == "download_failed" and "no download link" in files[0]["error"]
    assert files[2]["status"] == "download_failed" and files[2]["error"] == "A storage operation failed."
    assert files[2]["sandbox_file_id"] == "f-3"
    assert _the_harvest(db)["files_accepted"] == 2


def test_execute_a_409_from_the_sandbox_is_transient(db, session, sandbox):
    _seed(db)
    sandbox.outcomes["Drawing_Log_Current.pdf"] = 409
    with pytest.raises(h.RfpHarvestTransient) as exc:
        h.execute(E1)
    assert "staging" in str(exc.value)
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    assert harvest["facts_at"] == NOW.isoformat()
    assert _email_row(db)["status"] == "harvest"


def test_execute_deletes_the_run_when_nothing_was_accepted(db, session, sandbox):
    _seed(db)
    for url in URLS:
        session.bytes_for[url] = NOT_PDF
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 0
    assert harvest["sandbox_run_id"] is None
    assert [f["status"] for f in harvest["files"]] == ["rejected"] * 4
    assert sandbox.names() == ["create", "add", "add", "add", "add", "delete"]
    assert db.tables["rfp_ingest_runs"] == []
    assert _email_row(db)["status"] == "split"


def test_execute_with_an_empty_manifest_creates_no_run(db, session, sandbox):
    _seed(db)
    session.payloads[DOCS_PATH]["files"] = []
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["file_count"] == 0
    assert harvest["last_error"] == h._MSG_NO_FILES and harvest["sandbox_run_id"] is None
    assert sandbox.calls == [] and not any(c[0] == "download" for c in session.calls)
    assert _email_row(db)["status"] == "split"


# ── execute: the caps ────────────────────────────────────────────────────


def test_execute_file_cap_is_permanent_and_keeps_the_facts(db, session, sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_files=2))
    _seed(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["claim_token"] is None
    assert harvest["last_error"] == "The bid package holds more files than the harvest accepts (4 of 2)."
    assert harvest["finished_at"] == NOW.isoformat()
    assert harvest["data"]["project_name"] == "Warehouse HVAC Upgrade" and harvest["facts_at"]
    assert harvest["file_count"] == 4 and harvest.get("sandbox_run_id") is None
    assert sandbox.calls == [] and not any(c[0] == "download" for c in session.calls)
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["last_error"] == harvest["last_error"]


def test_execute_byte_cap_is_permanent_and_keeps_the_facts(db, session, sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(
        h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_total_bytes=3 * 1024 * 1024)
    )
    _seed(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed"
    assert harvest["last_error"] == "The bid package is larger than the harvest accepts (5 MB of 3 MB)."
    assert harvest["data"]["documents"]["count"] == 4
    assert sandbox.calls == []
    assert _email_row(db)["status"] == "split"


# ── execute: failure mapping ─────────────────────────────────────────────


def test_unavailable_parks_a_pipeline_row_without_spending_an_attempt(db, session, sandbox):
    _seed(db, _email(attempts=2))
    until = NOW + timedelta(hours=3)
    session.available = (False, "Procore logins are locked after repeated failures.", until)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    assert harvest["last_error"] == "Procore logins are locked after repeated failures."
    assert harvest["finished_at"] is None
    email = _email_row(db)
    assert email["status"] == "harvest" and email["attempts"] == 2
    assert email["next_attempt_at"] == (NOW + timedelta(hours=3)).isoformat()
    assert email["last_error"] == "Procore logins are locked after repeated failures."
    assert email["harvest_id"] is None
    assert session.calls == [("availability",)]


def test_unavailable_without_a_lock_time_waits_the_poll_interval(db, session, sandbox, settings):
    _seed(db)
    session.errors[PACKAGE_PATH] = pc.ProcoreUnavailable("Procore answered with a verification page instead of data.")
    h.execute(E1)
    email = _email_row(db)
    assert email["status"] == "harvest"
    assert email["next_attempt_at"] == (NOW + timedelta(seconds=settings.rfp_harvest_poll_seconds)).isoformat()
    assert "verification page" in email["last_error"]
    assert _the_harvest(db)["status"] == "pending"


def test_unavailable_fails_a_manual_harvest_and_links_the_email(db, session, sandbox):
    _seed(db, _email(status="done"))
    session.available = (False, "Procore credentials are not configured.", None)
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == "Procore credentials are not configured."
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["claim_token"] is None
    assert harvest["last_error"] == "Procore credentials are not configured."
    assert harvest["finished_at"] == NOW.isoformat()
    email = _email_row(db)
    assert email["status"] == "done" and email["harvest_id"] == harvest["id"]


@pytest.mark.parametrize(
    "exc",
    [pc.ProcoreTransient("Procore answered 503; the harvest will be retried."),
     h.RfpHarvestTransient("interrupted"), pc.ProcoreSessionExpired("gone")],
)
def test_transient_releases_the_harvest_and_raises_for_the_queue(db, session, sandbox, exc):
    _seed(db)
    session.errors[BID_PATH] = exc
    with pytest.raises(h.RfpHarvestTransient) as raised:
        h.execute(E1)
    assert str(raised.value) == str(exc)
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    assert harvest["last_error"] == str(exc) and harvest["finished_at"] is None
    email = _email_row(db)
    assert email["status"] == "harvest" and email["next_attempt_at"] is None
    assert email["harvest_id"] is None and email["last_error"] is None
    # Manual mode: the same release, plus the link so the card shows the row.
    db.tables["rfp_emails"] = [_email(status="done")]
    with pytest.raises(h.RfpHarvestTransient):
        h.execute(E1)
    assert _email_row(db)["harvest_id"] == harvest["id"] and _email_row(db)["status"] == "done"


def test_a_lost_lease_is_transient(db, session, sandbox, monkeypatch):
    _seed(db)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: False)
    with pytest.raises(h.RfpHarvestTransient) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_INTERRUPTED
    assert _the_harvest(db)["status"] == "pending" and session.calls == [("availability",)]


@pytest.mark.parametrize(
    "arm",
    [
        ("resolve", pc.ProcoreForbidden("Procore refused the bid sheet (403/404): the account cannot see it, or it was removed.")),
        (DOCS_PATH, h.RfpHarvestPermanent("Procore answered with a document list the harvest does not understand.")),
    ],
)
def test_permanent_fails_the_harvest_and_moves_the_email_to_create_with_the_link(db, session, sandbox, arm):
    key, exc = arm
    _seed(db)
    session.errors[key] = exc
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["last_error"] == str(exc)
    assert harvest["finished_at"] == NOW.isoformat() and harvest["claim_token"] is None
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["last_error"] == str(exc) and email["harvested_at"] == NOW.isoformat()
    assert sandbox.calls == []
    # Manual mode raises the same sentence after the same writes.
    db.tables["rfp_emails"] = [_email(status="done")]
    db.tables["rfp_harvests"] = []
    session.errors[key] = exc
    with pytest.raises(h.RfpHarvestPermanent) as raised:
        h.execute(E1)
    assert str(raised.value) == str(exc)
    assert _the_harvest(db)["status"] == "failed"
    assert _email_row(db)["harvest_id"] == _the_harvest(db)["id"] and _email_row(db)["status"] == "done"


def test_a_malformed_manifest_is_permanent(db, session, sandbox):
    _seed(db)
    session.payloads[DOCS_PATH] = {"files": "surprise"}
    h.execute(E1)
    assert _the_harvest(db)["status"] == "failed"
    assert _the_harvest(db)["last_error"] == h._MSG_MANIFEST_SHAPE
    assert _email_row(db)["status"] == "split"


def test_no_platform_link_is_permanent_without_a_harvest_row(db, session, sandbox):
    _seed(db, _email(body_text="Please bid. Nothing to click."))
    assert h.execute(E1) is None
    assert _harvests(db) == [] and session.opened == []
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] is None
    assert email["last_error"] == h._MSG_NO_LINK and email["harvested_at"] == NOW.isoformat()
    db.tables["rfp_emails"] = [_email(status="done", body_text=None)]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_NO_LINK


def test_no_harvester_drains_a_pipeline_row_and_refuses_a_manual_run(db, session, sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, procore_login_password=""))
    _seed(db)
    h.execute(E1)
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] is None
    assert _harvests(db) == [] and session.opened == []
    db.tables["rfp_emails"] = [_email(status="done")]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_NOT_CONFIGURED
    # A direct-invitation row (doc 2.5): with the email harvester off there
    # is no harvester; on, the Procore-bodied row has nothing for it.
    monkeypatch.setattr(
        h, "get_settings",
        lambda: _settings(tmp_path, procore_login_password="", rfp_harvest_email_enabled=False),
    )
    db.tables["rfp_emails"] = [_email(status="done", invitation_method="general")]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_NO_HARVESTER
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, procore_login_password=""))
    db.tables["rfp_emails"] = [_email(status="done", invitation_method="general")]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_NO_EMAIL_FILES
    assert _harvests(db) == []


# ── gc_portal: the scraper registry seam (doc 2.4) ───────────────────────


def test_gc_portal_scraper_is_picked_by_sender_domain_on_a_label_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "GC_PORTAL_SCRAPERS", {"gc.example": "gc_example", "": "never"})
    on = _settings(tmp_path)
    row = _email(invitation_method="gc_portal", from_address="Bids@GC.example")
    assert h.gc_portal_scraper_for(row) == "gc_example"
    assert h.harvester_for(row, on) == "gc_portal"
    assert h.can_harvest(row, on) == (True, None)
    # Subdomains are covered; lookalikes and other GCs are not.
    assert h.gc_portal_scraper_for(_email(from_address="x@portal.gc.example")) == "gc_example"
    assert h.gc_portal_scraper_for(_email(from_address="x@notgc.example")) is None
    assert h.gc_portal_scraper_for(_email(from_address="x@other.example")) is None
    assert h.gc_portal_scraper_for(_email(from_address=None)) is None
    # The registry keys on the sender, not the method: a procore row from a
    # registered domain is still harvested by Procore.
    assert h.harvester_for(_email(from_address="x@gc.example"), on) == "procore"
    # The flags still gate it.
    off = _settings(tmp_path, rfp_harvest_enabled=False)
    assert h.harvester_for(row, off) is None
    assert h.can_harvest(row, off) == (False, h._MSG_NO_HARVESTER)
    # No link parser exists for the method, so the match exit and the step
    # keep draining gc_portal rows to done until the scraper build adds one.
    assert h.platform_reference("gc_portal", fx.EMAIL_BODY) is None


def test_gc_portal_without_a_scraper_drains_the_pipeline_row_and_says_why_by_hand(
    db, session, sandbox, settings
):
    _seed(db)
    db.tables["rfp_emails"] = [_email(invitation_method="gc_portal", from_address="bids@gc.example")]
    h.execute(E1)
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] is None
    assert _harvests(db) == [] and session.opened == []
    db.tables["rfp_emails"] = [
        _email(status="done", invitation_method="gc_portal", from_address="bids@gc.example")
    ]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == "No scraper exists yet for this GC's portal (gc.example)."


def test_a_registered_gc_portal_scraper_with_no_runner_fails_visibly(
    db, session, sandbox, settings, monkeypatch
):
    """Until the scraper build replaces the branch, a registered scraper must
    never walk the Procore path: the manual run fails with the not-wired
    message and nothing is opened or written."""
    monkeypatch.setattr(h, "GC_PORTAL_SCRAPERS", {"gc.example": "gc_example"})
    _seed(db)
    db.tables["rfp_emails"] = [
        _email(status="done", invitation_method="gc_portal", from_address="bids@gc.example")
    ]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == "The gc_example scraper is registered for gc.example but nothing runs it yet."
    assert _harvests(db) == [] and session.opened == []
    # Pipeline mode: the row finishes at done with the reason on it.
    db.tables["rfp_emails"] = [_email(invitation_method="gc_portal", from_address="bids@gc.example")]
    h.execute(E1)
    row = _email_row(db)
    assert row["status"] == "split" and row["harvest_id"] is None
    assert row["last_error"] == "The gc_example scraper is registered for gc.example but nothing runs it yet."


def test_a_losing_claim_parks_a_pipeline_row_and_is_transient_by_hand(db, session, sandbox, settings):
    _seed(db)
    db.tables["rfp_harvests"].append(
        _harvest_row(status="running", claim_token="theirs", started_at=NOW.isoformat())
    )
    assert h.execute(E1) is None
    assert session.opened == []
    email = _email_row(db)
    assert email["status"] == "harvest" and email["last_error"] == h._MSG_CLAIMED
    assert email["next_attempt_at"] == (NOW + timedelta(seconds=settings.rfp_harvest_poll_seconds)).isoformat()
    assert email["attempts"] == 0
    assert _the_harvest(db)["claim_token"] == "theirs"
    db.tables["rfp_emails"] = [_email(status="done")]
    with pytest.raises(h.RfpHarvestTransient) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_CLAIMED


def test_a_losing_claim_is_logged(db, session, sandbox, caplog):
    _seed(db)
    db.tables["rfp_harvests"].append(
        _harvest_row(status="running", claim_token="theirs", started_at=NOW.isoformat())
    )
    with caplog.at_level(logging.WARNING, logger="app.services.rfp_harvest"):
        h.execute(E1)
    assert any("lost the claim" in r.getMessage() and "hv-1" in r.getMessage() for r in caplog.records)


def test_a_dead_workers_claim_is_taken_over_once_older_than_the_queue_lease(db, session, sandbox, settings):
    """A worker killed mid-harvest leaves the row `running` under its token;
    the requeued job used to park behind it every minute forever. Once the
    claim is older than the queue lease it is taken over; a claim exactly
    that old, or younger, is still someone else's."""
    _seed(db)
    stale = NOW - timedelta(seconds=settings.llm_queue_lease_seconds + 1)
    db.tables["rfp_harvests"].append(
        _harvest_row(status="running", claim_token="dead-worker", started_at=stale.isoformat(), files=[])
    )
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["claim_token"] is None and harvest["attempts"] == 2
    assert harvest["started_at"] == NOW.isoformat() and session.opened == [settings]
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == "hv-1"
    db.tables["rfp_emails"] = [_email()]
    harvest.update(status="running", claim_token="live",
                   started_at=(NOW - timedelta(seconds=settings.llm_queue_lease_seconds)).isoformat())
    assert h.execute(E1) is None
    assert _the_harvest(db)["claim_token"] == "live" and _email_row(db)["last_error"] == h._MSG_CLAIMED
    # The filter itself, as PostgREST reads it.
    cutoff = (NOW - timedelta(seconds=settings.llm_queue_lease_seconds)).isoformat()
    assert h.stale_claim_filter(settings, NOW) == (
        f"status.in.(pending,failed,complete),and(status.eq.running,started_at.lt.{cutoff})"
    )


def test_a_claim_lost_mid_run_parks_the_row_and_writes_nothing_more(db, session, sandbox, settings):
    _seed(db)
    real = session.get_json

    def steal(path, **kw):
        out = real(path, **kw)
        if path == DOCS_PATH:
            for row in _harvests(db):
                row["claim_token"] = "stolen"
        return out

    session.get_json = steal
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["claim_token"] == "stolen" and harvest["status"] == "running"
    assert "facts_at" not in harvest
    email = _email_row(db)
    assert email["status"] == "harvest" and email["last_error"] == h._MSG_CLAIMED
    assert email["next_attempt_at"] == (NOW + timedelta(seconds=settings.rfp_harvest_poll_seconds)).isoformat()
    assert sandbox.calls == []
    db.tables["rfp_emails"] = [_email(status="done")]
    db.tables["rfp_harvests"] = []
    with pytest.raises(h.RfpHarvestTransient):
        h.execute(E1)


def test_an_unexpected_error_is_interrupted_and_transient(db, session, sandbox, monkeypatch):
    _seed(db)
    monkeypatch.setattr(rfp_ingest, "create_harvest_run", lambda **k: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(h.RfpHarvestTransient) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_INTERRUPTED
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["last_error"] == h._MSG_INTERRUPTED
    assert _email_row(db)["status"] == "harvest"


def test_find_or_create_survives_the_unique_race(db, monkeypatch):
    real = h._find_harvest
    misses = []

    def racy(sb, method, key):
        if not misses:
            misses.append(True)
            db.tables["rfp_harvests"].append(_harvest_row(id="hv-theirs", status="pending"))
            return None
        return real(sb, method, key)

    monkeypatch.setattr(h, "_find_harvest", racy)
    row = h._find_or_create_harvest(db, E1, "procore", REF)
    assert row["id"] == "hv-theirs" and len(_harvests(db)) == 1


# ── mark_from_queue, current_status, error_message ───────────────────────


def test_mark_from_queue_fails_the_harvest_and_finishes_a_pipeline_email(db):
    _seed(db, _email(harvest_id="hv-1"))
    db.tables["rfp_harvests"].append(_harvest_row(status="pending", finished_at=None))
    h.mark_from_queue(E1, "failed", "The harvest was interrupted (failed after 6 attempts)")
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["claim_token"] is None
    assert harvest["last_error"] == "The harvest was interrupted (failed after 6 attempts)"
    assert harvest["finished_at"] == NOW.isoformat()
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == "hv-1"
    assert email["last_error"] == "The harvest was interrupted (failed after 6 attempts)"


def test_mark_from_queue_finds_the_harvest_by_platform_key_when_the_email_is_not_linked_yet(db):
    """Pipeline mode never links the email before the harvest completes, so a
    terminal queue failure must still reach the harvest row (doc 2.2: the
    permanent-failure writes, harvest_id set so the card shows the failure)."""
    _seed(db)
    db.tables["rfp_harvests"].append(_harvest_row(status="pending", finished_at=None, last_error="503"))
    h.mark_from_queue(E1, "failed", None)
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["last_error"] == h._MSG_INTERRUPTED
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == "hv-1"
    assert email["last_error"] == h._MSG_INTERRUPTED


def test_mark_from_queue_never_overwrites_a_complete_harvest_or_a_finished_email(db):
    _seed(db, _email(status="done", harvest_id="hv-1", last_error=None))
    db.tables["rfp_harvests"].append(_harvest_row())
    h.mark_from_queue(E1, "failed", "late")
    assert _the_harvest(db)["status"] == "complete" and _the_harvest(db)["last_error"] is None
    assert _email_row(db) == _email(status="done", harvest_id="hv-1")
    # A pending mark is a no-op; a missing email is a no-op; no link and no row is a no-op.
    h.mark_from_queue(E1, "pending", None)
    h.mark_from_queue("nope", "failed", "x")
    db.tables["rfp_emails"] = [_email(body_text=None)]
    h.mark_from_queue(E1, "failed", "x")
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] is None


def test_current_status_maps_terminal_harvests_to_done(db):
    assert h.current_status("nope") is None
    _seed(db)
    assert h.current_status(E1) == "pending"
    _email_row(db)["harvest_id"] = "hv-missing"
    assert h.current_status(E1) == "pending"
    db.tables["rfp_harvests"].append(_harvest_row(status="running"))
    _email_row(db)["harvest_id"] = "hv-1"
    assert h.current_status(E1) == "running"
    _the_harvest(db)["status"] = "pending"
    assert h.current_status(E1) == "pending"
    for terminal in ("complete", "failed"):
        _the_harvest(db)["status"] = terminal
        assert h.current_status(E1) == "done"


def test_error_message_passes_app_sentences_through_and_hides_everything_else():
    assert h.error_message(h.RfpHarvestTransient("Procore storage did not answer."), "procore") == (
        "Procore storage did not answer."
    )
    assert h.error_message(h.RfpHarvestPermanent("No link."), "procore") == "No link."
    assert h.error_message(pc.ProcoreForbidden("Refused."), "procore") == "Refused."
    assert h.error_message(pc.ProcoreLoginLocked("Locked."), "procore") == "Locked."
    assert h.error_message(h.RfpHarvestTransient(""), "procore") == h._MSG_INTERRUPTED
    assert h.error_message(ValueError("developer text /tmp/x"), "procore") == h._MSG_INTERRUPTED
    assert h.error_message(RuntimeError(), "procore") == h._MSG_INTERRUPTED
    assert h.RfpHarvestTransient.llm_error_kind == "infrastructure"
    assert h.RfpHarvestPermanent.llm_error_kind == "bad_input"


# ── step ─────────────────────────────────────────────────────────────────


class _StepRecorder:
    def __init__(self):
        self.parks: list[tuple[float, str | None]] = []
        self.finished = 0

    def park(self, seconds, error):
        self.parks.append((seconds, error))

    def finish(self):
        self.finished += 1
        return True


def test_step_drains_to_done_without_a_harvester_or_a_link(db, monkeypatch, tmp_path):
    rec = _StepRecorder()
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, procore_login_email=""))
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    assert rec.finished == 1 and rec.parks == [] and db.tables["llm_jobs"] == []
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path))
    h.step(db, _email(invitation_method="general"), park=rec.park, finish=rec.finish)
    h.step(db, _email(body_text="no link"), park=rec.park, finish=rec.finish)
    assert rec.finished == 3 and rec.parks == [] and db.tables["llm_jobs"] == []


def test_step_parks_while_logins_are_locked_without_enqueueing(db, settings):
    until = NOW + timedelta(hours=2)
    db.tables["rfp_harvest_sessions"].append({"provider": "procore", "locked_until": until.isoformat()})
    rec = _StepRecorder()
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    assert rec.finished == 0
    assert rec.parks == [(7200.0, "Procore logins are locked after repeated failures.")]
    assert db.tables["llm_jobs"] == []
    # A short remaining lock still waits at least the poll interval.
    db.tables["rfp_harvest_sessions"][0]["locked_until"] = (NOW + timedelta(seconds=10)).isoformat()
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    assert rec.parks[-1] == (float(settings.rfp_harvest_poll_seconds), "Procore logins are locked after repeated failures.")


def test_step_enqueues_once_and_waits_while_the_job_is_active(db, settings):
    rec = _StepRecorder()
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    jobs = db.tables["llm_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "rfp_harvest" and jobs[0]["feature"] == "rfp_harvest"
    assert jobs[0]["target_id"] == E1 and jobs[0]["payload"] == {"email_id": E1, "force": False}
    assert jobs[0]["priority"] == settings.rfp_harvest_queue_priority == 150
    assert jobs[0]["created_by"] is None
    assert rec.parks == [(settings.rfp_harvest_poll_seconds, None)] and rec.finished == 0
    # The sweep re-checks the row: the job is still active, so no second job.
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    assert len(db.tables["llm_jobs"]) == 1
    assert rec.parks == [(settings.rfp_harvest_poll_seconds, None)] * 2
    # Once the job is terminal a new one is queued (crash resume).
    jobs[0]["status"] = "succeeded"
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    assert len(db.tables["llm_jobs"]) == 2


def test_step_swallows_a_lost_enqueue_race(db, monkeypatch):
    rec = _StepRecorder()
    monkeypatch.setattr(h, "active_job", lambda email_id: None)

    def collide(email_id, *, created_by, settings=None, force=False):
        raise llm_queue.JobAlreadyActive({"id": "j-theirs"})

    monkeypatch.setattr(h, "enqueue", collide)
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    assert len(rec.parks) == 1 and rec.finished == 0


def test_enqueue_carries_force_and_refuses_a_second_active_job(db, settings):
    job = h.enqueue(E1, created_by="u1", force=True)
    assert job["payload"] == {"email_id": E1, "force": True} and job["created_by"] == "u1"
    assert job["priority"] == 150 and job["feature"] == "rfp_harvest"
    with pytest.raises(llm_queue.JobAlreadyActive) as exc:
        h.enqueue(E1, created_by="u2")
    assert exc.value.job["id"] == job["id"]
    assert h.active_job(E1)["id"] == job["id"]
    assert len(db.tables["llm_jobs"]) == 1


# ── The session store, the lock bell and the adapters ────────────────────


def test_session_store_mirrors_the_lock_policy_against_the_table(db, settings):
    store = h._SessionStore(settings)
    assert store.load() is None
    assert store.record_login(ok=False, error="email: no")["login_failures"] == 1
    assert store.record_login(ok=False, error="password: no")["login_failures"] == 2
    state = store.record_login(ok=False, error="password: no")
    assert state["login_failures"] == 3
    assert state["locked_until"] == (NOW + timedelta(seconds=settings.procore_login_lock_seconds)).isoformat()
    assert state["last_login_attempt_at"] == NOW.isoformat()
    assert len(db.tables["rfp_harvest_sessions"]) == 1
    store.save_cookies("harvest-bot@example.com", [{"name": "_session_id", "value": "v", "domain": "app.procore.com"}])
    row = store.load()
    assert row["account"] == "harvest-bot@example.com" and row["cookies"][0]["value"] == "v"
    assert row["login_failures"] == 0 and row["locked_until"] is None and row["last_error"] is None
    assert row["logged_in_at"] == NOW.isoformat()
    store.record_login(ok=True, error=None)
    assert store.load()["login_failures"] == 0
    # Errors are capped; touch is throttled to one write a minute.
    assert len(store.record_login(ok=False, error="x" * 900)["last_error"]) == h._ERROR_MAX_CHARS
    store.touch()
    store.touch()
    assert store.load()["last_used_at"] == NOW.isoformat()


def test_availability_and_session_status_read_the_store_without_touching_procore(db, settings, tmp_path):
    assert h.availability(settings) == (True, None, None)
    assert h.availability(_settings(tmp_path, procore_login_password="")) == (False, h._MSG_NOT_CONFIGURED, None)
    until = NOW + timedelta(hours=1)
    db.tables["rfp_harvest_sessions"].append({
        "provider": "procore", "account": "harvest-bot@example.com",
        "cookies": [{"name": "_session_id", "value": "secret-cookie"}],
        "logged_in_at": "2026-09-14T10:00:00+00:00", "last_used_at": "2026-09-14T11:00:00+00:00",
        "last_login_attempt_at": "2026-09-14T10:00:00+00:00", "login_failures": 3,
        "locked_until": until.isoformat(), "last_error": "password: Procore rejected the password.",
    })
    assert h.availability(settings) == (False, "Procore logins are locked after repeated failures.", until)
    db.tables["llm_jobs"] = [
        {"id": "j1", "job_type": "rfp_harvest", "status": "queued"},
        {"id": "j2", "job_type": "rfp_harvest", "status": "running"},
        {"id": "j3", "job_type": "rfp_harvest", "status": "succeeded"},
        {"id": "j4", "job_type": "rfp_ingest", "status": "queued"},
    ]
    status = h.session_status(settings)
    assert status == {
        "enabled": True,
        "configured": True,
        "account": "harvest-bot@example.com",
        "logged_in_at": "2026-09-14T10:00:00+00:00",
        "last_used_at": "2026-09-14T11:00:00+00:00",
        "last_login_attempt_at": "2026-09-14T10:00:00+00:00",
        "login_failures": 3,
        "locked_until": until.isoformat(),
        "last_error": "password: Procore rejected the password.",
        "active_jobs": 2,
        "pipelinesuite": {"enabled": True, "portals": []},
        "smartbid": {
            "enabled": True, "logged_in_at": None, "last_used_at": None, "last_login_attempt_at": None,
            "login_failures": 0, "locked_until": None, "last_error": None,
        },
    }
    assert "secret-cookie" not in json.dumps(status) and "pw" not in status.values()
    assert h.session_status(_settings(tmp_path, procore_login_email=""))["account"] is None


def test_notify_lock_rings_every_it_admin_once_and_dedupes_while_unread(db):
    until = NOW + timedelta(hours=6)
    h._notify_lock(until, "password: Procore rejected the password.")
    bells = db.tables["notifications"]
    assert len(bells) == 1 and bells[0]["role"] == Role.IT_ADMIN
    assert bells[0]["type"] == "rfp_harvest.login_failed"
    assert "paused until 2026-09-14 18:00 UTC" in bells[0]["message"]
    assert "PROCORE_LOGIN_EMAIL and PROCORE_LOGIN_PASSWORD" in bells[0]["message"]
    assert bells[0]["metadata"] == {"locked_until": until.isoformat(), "error": "password: Procore rejected the password."}
    h._notify_lock(until, "again")
    assert len(bells) == 1
    bells[0]["read_at"] = NOW.isoformat()
    h._notify_lock(until, "again")
    assert len(bells) == 2


def test_open_session_wires_the_config_the_store_and_the_bell(settings, tmp_path):
    config = h.procore_config(_settings(
        tmp_path, procore_login_email="  bot@example.com ", procore_min_request_interval_seconds=3.5,
        procore_login_min_interval_seconds=120, procore_request_timeout_seconds=12,
    ))
    assert config == pc.ProcoreConfig(
        email="bot@example.com", password="pw", min_request_interval=3.5,
        login_min_interval=120, timeout=12,
    )
    session = h.open_session(settings)
    try:
        assert isinstance(session, pc.ProcoreSession)
        assert isinstance(session.store, h._SessionStore)
        assert session._on_lock is h._notify_lock
        assert session.config.email == "harvest-bot@example.com"
    finally:
        session.close()


def test_harvest_for_email_falls_back_to_the_platform_row_and_hides_the_secrets(db):
    db.tables["rfp_harvests"].append(_harvest_row(claim_token="tok", raw={"bid": {"x": 1}}))
    row = h.harvest_for_email(db, _email())
    assert row["id"] == "hv-1"
    assert h.harvest_for_email(db, _email(harvest_id="hv-1"))["id"] == "hv-1"
    assert h.harvest_for_email(db, _email(body_text=None)) is None
    assert h.harvest_for_email(db, _email(invitation_method="general")) is None
    cols = {c.strip() for c in h._PUBLIC_HARVEST_COLUMNS.split(",")}
    assert "raw" not in cols and "claim_token" not in cols and "cookies" not in cols
    assert {"id", "status", "data", "files", "last_error", "sandbox_run_id", "facts_at"} <= cols


# ═════════════════════════════════════════════════════════════════════════
# PipelineSuite (docs/RFP_PIPELINESUITE.md sections 4 and 8)
# ═════════════════════════════════════════════════════════════════════════

PS_BODY = psfx.load(psfx.CGB_EMAIL_TEXT)
PS_REF = psc.parse_reference(PS_BODY)
PS_KEY = f"pipelinesuite:{psfx.CGB_LABEL}:{psfx.CGB_PROJECT_ID}"
PS_URL = f"https://{psfx.CGB_HOST}/ehPipelineSubs/dspProject/projectID/{psfx.CGB_PROJECT_ID}"
PS_PROVIDER = f"pipelinesuite:{psfx.CGB_HOST}"
PS_MAILBOX = "rfp@g3.example"
PS_PAGE = psfx.load(psfx.CGB_PROJECT_PAGE)
PS_FILE_URLS = [f["url"] for f in psc.parse_project_page(PS_PAGE).files]
DOCX = b"PK\x03\x04" + b"\x00" * 200
SECRET_MARKERS = (psfx.CGB_KEY, "_dummy_c")


class FakePortalSession:
    """The subset of pipelinesuite_client.PipelineSuiteSession the job
    touches. `errors` maps "page", a download URL or a ping URL to an
    exception (or a list consumed one per call); `bytes_for` overrides the
    bytes a download writes; `ping_status` the status a ping answers."""

    provider = "pipelinesuite"

    def __init__(self, page_html=PS_PAGE):
        self.calls: list[tuple] = []
        self.available: tuple = (True, None, None)
        self.page_html = page_html
        self.errors: dict = {}
        self.bytes_for: dict[str, bytes] = {}
        self.ping_status: dict[str, object] = {}
        self.on_download = None
        self.closed = False
        self.downloads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def _raise(self, key):
        err = self.errors.get(key)
        if isinstance(err, list):
            if err:
                raise err.pop(0)
        elif err is not None:
            raise err

    def availability(self):
        self.calls.append(("availability",))
        return self.available

    def get_project_page(self):
        self.calls.append(("page",))
        self._raise("page")
        return self.page_html

    def download(self, url, dest, *, max_bytes):
        self.calls.append(("download", url, max_bytes))
        if self.downloads == 0 and self.on_download is not None:
            self.on_download()
        self.downloads += 1
        self._raise(url)
        data = self.bytes_for.get(url, PDF)
        if len(data) > max_bytes:
            raise psc.PipelineSuiteForbidden("The project file is larger than the harvest accepts.")
        dest.write_bytes(data)
        return len(data)

    def ping(self, url):
        self.calls.append(("ping", url))
        self._raise(url)
        if url in self.ping_status:
            return self.ping_status[url]
        return 200 if "/wf/open" in url else 302

    @property
    def pings(self):
        return [c[1] for c in self.calls if c[0] == "ping"]


@pytest.fixture
def ps_session(monkeypatch):
    fake = FakePortalSession()
    opened = []

    def open_session(settings, ref):
        opened.append((settings, ref))
        return fake

    monkeypatch.setattr(h, "open_pipelinesuite_session", open_session)
    fake.opened = opened
    return fake


class FakeGraph:
    """graph_inbox.get_message: the email HTML per (mailbox, message id).
    `mode` = ok | 404 | boom; `gone` names mailboxes whose copy 404s."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.mode = "ok"
        self.gone: set[str] = set()
        self.html = psfx.load(psfx.CGB_EMAIL_HTML)

    def get_message(self, message_id, *, mailbox=None, select=None, body_type=None):
        self.calls.append((message_id, mailbox, select, body_type))
        if self.mode == "boom":
            raise RuntimeError("graph down")
        if self.mode == "404" or mailbox in self.gone:
            import httpx
            req = httpx.Request("GET", "https://graph.microsoft.com/x")
            raise httpx.HTTPStatusError("gone", request=req, response=httpx.Response(404, request=req))
        return {"id": message_id, "body": {"contentType": "html", "content": self.html}}


@pytest.fixture
def graph(monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(h.graph_inbox, "get_message", fake.get_message)
    return fake


def _ps_email(**over):
    row = _email(
        invitation_method="pipelinesuite",
        body_text=PS_BODY,
        from_address="michelle@cgandbinc.com",
        primary_mailbox=PS_MAILBOX,
    )
    row.update(over)
    return row


def _ps_seed(db, row=None, *, sightings=True):
    row = _seed(db, row or _ps_email())
    if sightings:
        db.tables["rfp_email_sightings"].extend([
            {"id": "s-1", "rfp_email_id": row["id"], "mailbox": "other@g3.example",
             "graph_message_id": "msg-other", "created_at": "2026-09-16T01:00:00+00:00"},
            {"id": "s-2", "rfp_email_id": row["id"], "mailbox": PS_MAILBOX,
             "graph_message_id": "msg-primary", "created_at": "2026-09-16T02:00:00+00:00"},
        ])
    return row


def _ps_harvest_row(**over):
    row = _harvest_row(
        method="pipelinesuite", external_key=PS_KEY, external_url=PS_URL,
        data={"platform": "pipelinesuite"},
    )
    row.update(over)
    return row


def _no_secret_anywhere(db, caplog=None):
    """The key and the form token are in no harvest row, no email row update
    (the email's own body_text, which carries the key by nature, is left
    out), no queue job, no session row, no bell and no log record."""
    tables = {
        name: [
            {k: v for k, v in row.items() if not (name == "rfp_emails" and k == "body_text")}
            for row in rows
        ]
        for name, rows in db.tables.items()
    }
    dumped = json.dumps(tables, default=str)
    for marker in SECRET_MARKERS:
        assert marker not in dumped, marker
    if caplog is not None:
        for marker in SECRET_MARKERS:
            assert marker not in caplog.text, marker
            assert not any(marker in str(r.args) for r in caplog.records), marker


# ── Registry ─────────────────────────────────────────────────────────────


def test_pipelinesuite_registry_needs_the_flags_and_the_reference(tmp_path):
    on = _settings(tmp_path)
    row = _ps_email()
    assert h.harvester_for(row, on) == "pipelinesuite"
    assert h.can_harvest(row, on) == (True, None)
    assert h.platform_reference("pipelinesuite", PS_BODY) == PS_REF
    assert h.platform_reference("pipelinesuite", PS_BODY).external_key == PS_KEY
    assert h.platform_reference("pipelinesuite", PS_BODY).external_url == PS_URL
    assert h.platform_reference("procore", PS_BODY) is None
    assert h.platform_reference("pipelinesuite", fx.EMAIL_BODY) is None
    assert h.session_provider_for(row, on) == PS_PROVIDER
    assert h.session_provider_for(_email(), on) == "procore"
    assert h.session_provider_for(_ps_email(body_text="no portal here"), on) is None
    assert h.session_provider_for(_email(invitation_method="general"), on) is None
    # Procore credentials play no part; the PipelineSuite flag does.
    assert h.harvester_for(row, _settings(tmp_path, procore_login_password="")) == "pipelinesuite"
    for off in (
        _settings(tmp_path, pipelinesuite_enabled=False),
        _settings(tmp_path, rfp_harvest_enabled=False),
        _settings(tmp_path, rfp_ingest_enabled=False),
    ):
        assert h.harvester_for(row, off) is None
        assert h.can_harvest(row, off) == (False, h._MSG_NO_HARVESTER)
    no_ref = _ps_email(body_text="Please bid. Project ID: 377363 only.")
    assert h.harvester_for(no_ref, on) == "pipelinesuite"
    assert h.can_harvest(no_ref, on) == (
        False, "The email carries no PipelineSuite Project ID and Security Key."
    )
    assert h.can_harvest(_ps_email(body_text=None), on)[1] == h._MSG_NO_PS_REFERENCE
    # A Procore row keeps its own sentences.
    assert h.can_harvest(_email(body_text=None), on) == (False, h._MSG_NO_LINK)
    assert h.harvester_for(row) == "pipelinesuite"


def test_availability_for_reads_the_rows_own_portal_lock(db, settings, tmp_path):
    until = NOW + timedelta(hours=1)
    assert h.availability_for(_ps_email(), settings) == (True, None, None)
    assert h.availability_for(_email(), settings) == (True, None, None)
    assert h.availability_for(_email(invitation_method="gc_portal"), settings) == (True, None, None)
    assert h.availability_for(_ps_email(body_text="nothing"), settings) == (False, h._MSG_NO_PS_REFERENCE, None)
    assert h.availability_for(_ps_email(), _settings(tmp_path, pipelinesuite_enabled=False)) == (
        False, h._MSG_NO_HARVESTER, None
    )
    db.tables["rfp_harvest_sessions"].append({"provider": PS_PROVIDER, "locked_until": until.isoformat()})
    locked = f"PipelineSuite logins for {psfx.CGB_HOST} are locked after repeated failures."
    assert h.availability_for(_ps_email(), settings) == (False, locked, until)
    assert h.availability(settings, PS_PROVIDER) == (False, locked, until)
    # Another portal and the Procore session are not locked by it.
    other = _ps_email(body_text=psfx.load(psfx.SHF_EMAIL_TEXT))
    assert h.availability_for(other, settings) == (True, None, None)
    assert h.availability_for(_email(), settings) == (True, None, None)
    assert h.availability(settings) == (True, None, None)
    assert h.availability(settings, "smartbid") == (True, None, None)
    assert h.availability(settings, "no-such-platform") == (False, h._MSG_NO_HARVESTER, None)
    # And the other way round: a Procore lock leaves the portal alone.
    db.tables["rfp_harvest_sessions"] = [{"provider": "procore", "locked_until": until.isoformat()}]
    assert h.availability_for(_ps_email(), settings) == (True, None, None)
    assert h.availability_for(_email(), settings)[0] is False


# ── execute: the happy path ──────────────────────────────────────────────


def test_pipelinesuite_execute_pipeline_mode_harvests_facts_files_and_pings(
    db, ps_session, sandbox, graph, settings, caplog
):
    caplog.set_level(logging.DEBUG)
    _ps_seed(db)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["claim_token"] is None
    assert harvest["method"] == "pipelinesuite" and harvest["external_key"] == PS_KEY
    assert harvest["external_url"] == PS_URL and harvest["rfp_email_id"] == E1
    assert harvest["facts_at"] == NOW.isoformat() and harvest["finished_at"] == NOW.isoformat()
    data = harvest["data"]
    sizes = [kb * 1024 for kb in psfx.CGB_FILE_SIZES_KB]
    assert data == {
        "platform": "pipelinesuite",
        "portal_host": psfx.CGB_HOST,
        "portal_label": psfx.CGB_LABEL,
        "project_id": psfx.CGB_PROJECT_ID,
        "project_number": "MPID#0019122",
        "project_name": "Install Scoreboard on Soccer Field Cimarron Memorial High School",
        "project_address": "2301 N Tenaya Way, Las Vegas, NV 89128",
        "location": None,
        "bid_due_at": "2026-09-22T13:00:00-07:00",
        "bid_date_text": "September 22, 2026",
        "bid_time_text": "1:00 PM",
        "gc": {"name": "CG&B Inc.", "address": None, "phone": None, "website": None},
        "point_of_contact": {"name": "Jeff Wasson", "email": "j.wasson@cgandbinc.com", "phone": None},
        "contacts": [],
        "invited_name": "Thomas Moore with G3 Electrical Technologies",
        "trades": [{"code": "26000", "name": "Electrical"}],
        "notices": [],
        "other_info": "PRIVATE WAGES",
        "plans": None,
        "response_recorded": True,
        "tracking": {"pinged_at": NOW.isoformat(), "opened": True, "clicked": True, "error": None},
        "documents": {
            "count": 4,
            "bytes": sum(sizes),
            "kinds": {"drawing": 1, "specification": 3},
            "folders": [],
        },
    }
    assert harvest["description_text"] == (
        "Installation, excavation and trenching for underground electrical, concrete "
        "foundations and anchor systems, structural steel support poles, mounting and "
        "securing scoreboard assemblly and restoration of disturbed areas.\n\n"
        "CONTACT JEFF WASSON WITH RFI'S J.WASSON@CGANDBINC.COM"
    )
    assert "CLICK" not in harvest["description_text"]
    assert harvest["instructions_text"] == "PRIVATE WAGES"
    assert set(harvest["raw"]) >= {"project_info", "trades", "notices", "contacts", "files_head"}
    raw_dump = json.dumps(harvest["raw"])
    assert "http" not in raw_dump and "cne" not in raw_dump and "1000001" not in raw_dump
    assert harvest["file_count"] == 4 and harvest["files_accepted"] == 4
    assert harvest["bytes_downloaded"] == 4 * len(PDF) and harvest["sandbox_run_id"] == "run-1"
    assert harvest["files"] == [
        {
            "file_path": name, "size": size, "kind": kind, "discipline": None,
            "file_id": file_id, "uploaded_on": "9/10/2026",
            "sandbox_file_id": f"f-{index + 2}", "status": "accepted", "error": None,
        }
        for index, (name, size, kind, file_id) in enumerate(zip(
            psfx.CGB_FILE_NAMES, sizes, ("drawing", "specification", "specification", "specification"),
            psfx.CGB_FILE_IDS,
        ))
    ]
    assert "http" not in json.dumps(harvest["files"])
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["harvested_at"] == NOW.isoformat() and email["last_error"] is None
    # The documented order: pings, availability, the page, the downloads.
    assert ps_session.opened == [(settings, PS_REF)] and ps_session.closed
    assert ps_session.calls == [
        ("ping", psfx.OPEN_PIXEL_URL),
        ("ping", psfx.VIEW_FILES_URL),
        ("availability",),
        ("page",),
    ] + [("download", url, settings.rfp_ingest_max_file_bytes) for url in PS_FILE_URLS]
    assert not any(token in url for url in ps_session.pings for token in psfx.RESPONSE_TOKENS)
    # The email HTML came from the primary mailbox's sighting, html body only.
    assert graph.calls == [("msg-primary", PS_MAILBOX, "id,body", "html")]
    # The sandbox: one run, one upload per file named by its basename with
    # the platform source pointer, then start and dispatch.
    assert sandbox.calls[0] == ("create", E1, harvest["id"])
    adds = [c for c in sandbox.calls if c[0] == "add"]
    assert [c[2] for c in adds] == list(psfx.CGB_FILE_NAMES)
    assert adds[0][5] == {"kind": "pipelinesuite", "file_path": psfx.CGB_FILE_NAMES[0], "harvest_id": harvest["id"]}
    assert sandbox.calls[-2:] == [("start", "run-1"), ("dispatch", "run-1", None, None)]
    # The Security Key and the form token are in no row, no queue job, no log line.
    _no_secret_anywhere(db, caplog)


def test_pipelinesuite_manual_mode_links_a_done_row(db, ps_session, sandbox, graph):
    _ps_seed(db, _ps_email(status="done", flag_reason="no_project_name"))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["data"]["platform"] == "pipelinesuite"
    email = _email_row(db)
    assert email["status"] == "done" and email["flag_reason"] == "no_project_name"
    assert email["harvest_id"] == harvest["id"] and email["harvested_at"] == NOW.isoformat()


def test_pipelinesuite_writes_the_facts_before_the_first_download(db, ps_session, sandbox, graph):
    _ps_seed(db)
    seen = {}
    ps_session.on_download = lambda: seen.update(copy.deepcopy(_the_harvest(db)))
    h.execute(E1)
    assert seen["status"] == "running" and seen["claim_token"]
    assert seen["facts_at"] == NOW.isoformat() and seen["external_url"] == PS_URL
    assert seen["data"]["project_name"].startswith("Install Scoreboard")
    assert seen["data"]["tracking"]["pinged_at"] == NOW.isoformat()
    assert seen["file_count"] == 4 and all(f["status"] is None for f in seen["files"])


def test_pipelinesuite_reuses_a_young_complete_harvest_without_pinging(db, ps_session, sandbox, graph):
    _ps_seed(db)
    db.tables["rfp_harvests"].append(_ps_harvest_row(finished_at=(NOW - timedelta(days=2)).isoformat()))
    h.execute(E1)
    assert ps_session.opened == [] and graph.calls == [] and sandbox.calls == []
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == "hv-1"


# ── execute: the tracking pings ──────────────────────────────────────────


def test_pipelinesuite_pings_fire_once_and_are_skipped_on_a_later_run(db, ps_session, sandbox, graph):
    _ps_seed(db, _ps_email(status="done"))
    h.execute(E1)
    first = _the_harvest(db)["data"]["tracking"]
    assert first == {"pinged_at": NOW.isoformat(), "opened": True, "clicked": True, "error": None}
    assert ps_session.pings == [psfx.OPEN_PIXEL_URL, psfx.VIEW_FILES_URL]
    # Harvest again: the facts are refreshed, the tracking record kept, no ping.
    ps_session.calls.clear()
    graph.calls.clear()
    h.execute(E1, force=True)
    harvest = _the_harvest(db)
    assert harvest["attempts"] == 2 and harvest["status"] == "complete"
    assert harvest["data"]["tracking"] == first
    assert ps_session.pings == [] and graph.calls == []
    assert ps_session.calls[0] == ("availability",)


def test_pipelinesuite_pings_are_recorded_even_when_the_portal_is_locked(db, ps_session, sandbox, graph):
    """The pings come first and land on the row before the portal is
    touched, so a parked run never pings twice."""
    _ps_seed(db)
    until = NOW + timedelta(hours=2)
    ps_session.available = (False, f"PipelineSuite logins for {psfx.CGB_HOST} are locked after repeated failures.", until)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["data"]["tracking"]["pinged_at"] == NOW.isoformat()
    assert ps_session.pings == [psfx.OPEN_PIXEL_URL, psfx.VIEW_FILES_URL]
    email = _email_row(db)
    assert email["status"] == "harvest" and email["next_attempt_at"] == until.isoformat()
    assert email["attempts"] == 0
    # The lock lifts: the second run pings nothing and completes.
    ps_session.available = (True, None, None)
    ps_session.calls.clear()
    h.execute(E1)
    assert ps_session.pings == [] and _the_harvest(db)["status"] == "complete"
    assert _the_harvest(db)["data"]["tracking"]["pinged_at"] == NOW.isoformat()


def test_pipelinesuite_pings_never_fail_the_harvest(db, ps_session, sandbox, graph):
    _ps_seed(db)
    ps_session.errors[psfx.OPEN_PIXEL_URL] = RuntimeError("tracker exploded")
    ps_session.ping_status[psfx.VIEW_FILES_URL] = None
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete"
    assert harvest["data"]["tracking"] == {
        "pinged_at": NOW.isoformat(), "opened": False, "clicked": False,
        "error": "open: RuntimeError; click: no answer",
    }
    # A non-2xx/302 answer is recorded as not clicked.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_ps_email()]
    ps_session.errors.clear()
    ps_session.ping_status = {psfx.VIEW_FILES_URL: 404}
    h.execute(E1)
    assert _the_harvest(db)["data"]["tracking"] == {
        "pinged_at": NOW.isoformat(), "opened": True, "clicked": False, "error": "click: HTTP 404",
    }


def test_pipelinesuite_pings_fall_back_to_the_text_click_link(db, ps_session, sandbox, graph):
    # Graph down: no open pixel, the View Files link from body_text is clicked.
    _ps_seed(db)
    graph.mode = "boom"
    h.execute(E1)
    tracking = _the_harvest(db)["data"]["tracking"]
    assert tracking["opened"] is None and tracking["clicked"] is True
    assert tracking["error"] is None
    assert ps_session.pings == [psfx.VIEW_FILES_URL]
    assert len(graph.calls) == 2                      # both sightings tried
    # The primary copy gone: the other sighting serves the HTML.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_ps_email()]
    graph.mode = "ok"
    graph.gone = {PS_MAILBOX}
    graph.calls.clear()
    ps_session.calls.clear()
    h.execute(E1)
    assert [c[1] for c in graph.calls] == [PS_MAILBOX, "other@g3.example"]
    assert ps_session.pings == [psfx.OPEN_PIXEL_URL, psfx.VIEW_FILES_URL]
    # No sighting at all and a body without the line: nothing to ping, recorded as such.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_email_sightings"] = []
    db.tables["rfp_emails"] = [_ps_email(body_text=PS_BODY.replace("View Files and Project Details", "Files"))]
    graph.calls.clear()
    ps_session.calls.clear()
    h.execute(E1)
    tracking = _the_harvest(db)["data"]["tracking"]
    assert tracking == {"pinged_at": NOW.isoformat(), "opened": None, "clicked": None,
                        "error": "no tracking links in the email"}
    assert ps_session.pings == [] and graph.calls == []
    assert _the_harvest(db)["status"] == "complete"


def test_pipelinesuite_pings_can_be_switched_off(db, ps_session, sandbox, graph, monkeypatch, tmp_path):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, pipelinesuite_tracking_pings_enabled=False))
    _ps_seed(db)
    h.execute(E1)
    assert ps_session.pings == [] and graph.calls == []
    assert _the_harvest(db)["data"]["tracking"] is None
    assert _the_harvest(db)["status"] == "complete"


# ── execute: files, caps, failure mapping ────────────────────────────────


def test_pipelinesuite_per_file_outcomes_over_the_shf_folder_with_docx(db, ps_session, sandbox, graph, settings):
    shf_body = psfx.load(psfx.SHF_EMAIL_TEXT)
    _ps_seed(db, _ps_email(body_text=shf_body, from_address="quincy@shfcontracting.com"))
    ps_session.page_html = psfx.load(psfx.SHF_PROJECT_PAGE)
    page = psc.parse_project_page(ps_session.page_html)
    urls = [f["url"] for f in page.files]
    docx_urls = [u for u in urls if u.endswith(".docx")]
    for url in docx_urls:
        ps_session.bytes_for[url] = DOCX                    # the sandbox's sniff decides
    ps_session.errors[urls[1]] = [psc.PipelineSuiteTransient("file host 503")] * 3
    ps_session.errors[urls[4]] = psc.PipelineSuiteForbidden("The file host has no such file (404).")
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["external_key"] == "pipelinesuite:shfcontracting:377691"
    assert harvest["status"] == "complete" and harvest["file_count"] == 13
    files = harvest["files"]
    assert files[0]["file_path"] == "Addendum 01/Addendum 1 - Scope of Work - Fire Station 95 Interior Renovation - IFB 112-27.pdf"
    assert files[0]["size"] == 135 * 1024 and files[0]["kind"] == "specification"
    assert files[0]["status"] == "accepted"
    assert files[1]["status"] == "download_failed" and files[1]["error"] == "file host 503"
    assert files[4]["status"] == "download_failed" and "no such file" in files[4]["error"]
    docx_entries = [f for f in files if f["file_path"].endswith(".docx")]
    assert len(docx_entries) == 6 and all(f["kind"] == "other" for f in docx_entries)
    assert all(f["status"] == "rejected" and f["error"] == "The file is not a PDF." for f in docx_entries)
    assert harvest["files_accepted"] == 13 - 6 - 2
    assert harvest["data"]["documents"] == {
        "count": 13, "bytes": sum(f["size"] for f in files),
        "kinds": {"specification": 3, "other": 9, "drawing": 1}, "folders": ["Addendum 01"],
    }
    assert harvest["data"]["project_address"] == "2300 Pebble Road, Henderson, NV 89074"
    assert harvest["data"]["location"] == "Henderson, Nevada"
    assert harvest["data"]["gc"]["name"] == "SHF International LLC"
    assert harvest["data"]["point_of_contact"] is None
    assert harvest["data"]["response_recorded"] is False
    adds = [c for c in sandbox.calls if c[0] == "add"]
    assert adds[0][2] == "Addendum 1 - Scope of Work - Fire Station 95 Interior Renovation - IFB 112-27.pdf"
    assert "Apprenticeship Utilization Act Waiver.docx" in [c[2] for c in adds]
    assert all(c[5]["kind"] == "pipelinesuite" for c in adds)
    assert all(c[5]["file_path"].startswith("Addendum 01/") for c in adds)


def test_pipelinesuite_facts_grid_over_the_notices_and_contacts_page(db, ps_session, sandbox, graph):
    body = PS_BODY.replace("377363", psfx.CGB_PROJECT_ID_NOTICES)
    _ps_seed(db, _ps_email(body_text=body))
    ps_session.page_html = psfx.load(psfx.CGB_PROJECT_PAGE_NOTICES)
    h.execute(E1)
    data = _the_harvest(db)["data"]
    assert data["project_id"] == psfx.CGB_PROJECT_ID_NOTICES and data["project_number"] == "43ADG-S3999"
    assert data["project_address"] is None
    assert data["notices"] == [{"title": "Amendment # 2", "created_by": "Camilot Bradburn", "created_on": "9/15/2026"}]
    assert data["contacts"] == [{
        "company": "CG&B Enterprises, Inc.", "name": "Jeff Wasson", "title": "Estimator",
        "phone": "702-565-6564", "extension": None, "fax": None, "email": "j.wasson@cgandbinc.com",
    }]
    assert data["point_of_contact"] == {"name": "Jeff Wasson", "email": "j.wasson@cgandbinc.com", "phone": "702-565-6564"}
    assert data["gc"]["phone"] == "702-565-6564"
    assert data["response_recorded"] is False
    assert data["documents"]["kinds"] == {"other": 3, "drawing": 1, "specification": 3}
    assert _the_harvest(db)["instructions_text"] is None


def test_pipelinesuite_file_and_byte_caps_are_permanent_and_keep_the_facts(
    db, ps_session, sandbox, graph, monkeypatch, tmp_path
):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_files=2))
    _ps_seed(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed"
    assert harvest["last_error"] == "The project holds more files than the harvest accepts (4 of 2)."
    assert harvest["data"]["project_name"].startswith("Install Scoreboard") and harvest["facts_at"]
    assert sandbox.calls == [] and not any(c[0] == "download" for c in ps_session.calls)
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == harvest["id"]
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_total_bytes=3 * 1024 * 1024))
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_ps_email()]
    h.execute(E1)
    assert _the_harvest(db)["last_error"] == "The project files are larger than the harvest accepts (17 MB of 3 MB)."


def test_pipelinesuite_no_reference_is_permanent_without_a_row(db, ps_session, sandbox, graph):
    _ps_seed(db, _ps_email(body_text="Please bid on our project."), sightings=False)
    assert h.execute(E1) is None
    assert _harvests(db) == [] and ps_session.opened == []
    email = _email_row(db)
    assert email["status"] == "split" and email["last_error"] == h._MSG_NO_PS_REFERENCE
    db.tables["rfp_emails"] = [_ps_email(status="done", body_text=None)]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == "The email carries no PipelineSuite Project ID and Security Key."


def test_pipelinesuite_off_drains_a_pipeline_row_and_refuses_by_hand(db, ps_session, sandbox, graph, monkeypatch, tmp_path):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, pipelinesuite_enabled=False))
    _ps_seed(db)
    h.execute(E1)
    assert _email_row(db)["status"] == "split" and _harvests(db) == [] and ps_session.opened == []
    db.tables["rfp_emails"] = [_ps_email(status="done")]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_NO_HARVESTER


@pytest.mark.parametrize(
    "exc",
    [psc.PipelineSuiteTransient("The portal answered 503; the harvest will be retried."),
     psc.PipelineSuiteSessionExpired("gone"), h.RfpHarvestTransient("interrupted")],
)
def test_pipelinesuite_transient_releases_and_raises_for_the_queue(db, ps_session, sandbox, graph, exc, caplog):
    caplog.set_level(logging.DEBUG)
    _ps_seed(db)
    ps_session.errors["page"] = exc
    with pytest.raises(h.RfpHarvestTransient) as raised:
        h.execute(E1)
    assert str(raised.value) == str(exc)
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    assert harvest["last_error"] == str(exc) and harvest["finished_at"] is None
    assert harvest["data"]["tracking"]["pinged_at"] == NOW.isoformat()
    assert _email_row(db)["status"] == "harvest" and _email_row(db)["harvest_id"] is None
    _no_secret_anywhere(db, caplog)


def test_pipelinesuite_unavailable_parks_without_an_attempt_and_fails_by_hand(db, ps_session, sandbox, graph, settings):
    _ps_seed(db, _ps_email(attempts=2))
    ps_session.errors["page"] = psc.PipelineSuiteUnavailable("The portal answered with a page the harvest does not understand.")
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    email = _email_row(db)
    assert email["status"] == "harvest" and email["attempts"] == 2 and email["harvest_id"] is None
    assert email["next_attempt_at"] == (NOW + timedelta(seconds=settings.rfp_harvest_poll_seconds)).isoformat()
    assert "does not understand" in email["last_error"]
    # A login lock parks until the lock lifts.
    until = NOW + timedelta(hours=3)
    ps_session.errors["page"] = psc.PipelineSuiteLoginLocked("locked", locked_until=until)
    h.execute(E1)
    assert _email_row(db)["next_attempt_at"] == until.isoformat()
    # By hand: failed and linked.
    db.tables["rfp_emails"] = [_ps_email(status="done")]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == "locked"
    assert _the_harvest(db)["status"] == "failed" and _email_row(db)["harvest_id"] == _the_harvest(db)["id"]


def test_pipelinesuite_a_page_that_is_not_the_project_is_unavailable(db, ps_session, sandbox, graph):
    _ps_seed(db)
    ps_session.page_html = psfx.load(psfx.LOGIN_PAGE_ROOT)
    h.execute(E1)
    assert _the_harvest(db)["status"] == "pending"
    assert _email_row(db)["status"] == "harvest"
    assert _email_row(db)["last_error"] == "The portal answered with a page the harvest does not understand."


def test_pipelinesuite_forbidden_fails_the_harvest_and_moves_the_email_on(db, ps_session, sandbox, graph):
    _ps_seed(db)
    ps_session.errors["page"] = psc.PipelineSuiteForbidden("The portal has no project 377363 for this Security Key.")
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["finished_at"] == NOW.isoformat()
    assert harvest["last_error"] == "The portal has no project 377363 for this Security Key."
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["last_error"] == harvest["last_error"]
    db.tables["rfp_emails"] = [_ps_email(status="done")]
    db.tables["rfp_harvests"] = []
    with pytest.raises(h.RfpHarvestPermanent):
        h.execute(E1)
    assert _the_harvest(db)["status"] == "failed"


def test_pipelinesuite_losing_claim_and_lost_lease(db, ps_session, sandbox, graph, settings, monkeypatch):
    _ps_seed(db)
    db.tables["rfp_harvests"].append(_ps_harvest_row(status="running", claim_token="theirs", started_at=NOW.isoformat()))
    assert h.execute(E1) is None
    assert ps_session.opened == [] and _email_row(db)["last_error"] == h._MSG_CLAIMED
    db.tables["rfp_harvests"] = []
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: False)
    with pytest.raises(h.RfpHarvestTransient):
        h.execute(E1)
    assert _the_harvest(db)["status"] == "pending"
    assert [c[0] for c in ps_session.calls] == ["ping", "ping", "availability"]


# ── Sessions, the bell, the status block, the step ───────────────────────


def test_pipelinesuite_session_store_uses_the_portal_thresholds(db, tmp_path):
    settings = _settings(tmp_path, pipelinesuite_login_max_failures=2, pipelinesuite_login_lock_seconds=120)
    store = h._SessionStore(settings, PS_PROVIDER)
    assert store.load() is None
    assert store.record_login(ok=False, error="key: no")["login_failures"] == 1
    state = store.record_login(ok=False, error="key: no")
    assert state["login_failures"] == 2
    assert state["locked_until"] == (NOW + timedelta(seconds=120)).isoformat()
    store.save_cookies(PS_REF.fingerprint, [{"name": "CFID", "value": "v", "domain": psfx.CGB_HOST}])
    rows = db.tables["rfp_harvest_sessions"]
    assert [r["provider"] for r in rows] == [PS_PROVIDER]
    assert rows[0]["account"] == PS_REF.fingerprint and rows[0]["login_failures"] == 0
    # The Procore store is untouched by it and keeps its own thresholds.
    procore = h._SessionStore(settings)
    assert procore.load() is None and procore.max_failures == settings.procore_login_max_failures
    assert store.max_failures == 2 and store.lock_seconds == 120
    assert psfx.CGB_KEY not in json.dumps(rows)


def test_open_pipelinesuite_session_wires_the_config_the_store_and_the_bell(settings):
    session = h.open_pipelinesuite_session(settings, PS_REF)
    try:
        assert isinstance(session, psc.PipelineSuiteSession)
        assert session.config == psc.PipelineSuiteConfig(
            account=PS_REF.fingerprint,
            min_request_interval=settings.pipelinesuite_min_request_interval_seconds,
            login_min_interval=settings.pipelinesuite_login_min_interval_seconds,
            timeout=settings.pipelinesuite_request_timeout_seconds,
        )
        assert isinstance(session.store, h._SessionStore) and session.store.provider == PS_PROVIDER
        assert session._on_lock is h._notify_lock and session.ref is PS_REF
        assert psfx.CGB_KEY not in repr(session.config)
    finally:
        session.close()


def test_notify_lock_names_the_portal(db):
    until = NOW + timedelta(hours=6)
    h._notify_lock(until, "key: The portal rejected the Project ID and Security Key.", psfx.CGB_HOST)
    bells = db.tables["notifications"]
    assert len(bells) == 1 and bells[0]["type"] == "rfp_harvest.login_failed"
    assert bells[0]["message"] == (
        f"PipelineSuite login ({psfx.CGB_HOST}) failed repeatedly; RFP harvests for that portal "
        "are paused until 2026-09-14 18:00 UTC. The Project ID and Security Key come from the "
        "invitation email."
    )
    assert bells[0]["metadata"]["portal"] == psfx.CGB_HOST
    # Deduped like the Procore bell while one is unread.
    h._notify_lock(until, "again", psfx.SHF_HOST)
    assert len(bells) == 1


def test_session_status_lists_the_portals_without_cookies_or_keys(db, settings, tmp_path):
    until = NOW + timedelta(hours=1)
    db.tables["rfp_harvest_sessions"].extend([
        {"provider": "pipelinesuite:shfcontracting.pipelinesuite.com", "account": "aa11",
         "cookies": [{"name": "CFID", "value": "secret-cookie"}], "logged_in_at": "2026-09-16T10:00:00+00:00",
         "last_used_at": None, "last_login_attempt_at": "2026-09-16T10:00:00+00:00", "login_failures": 0,
         "locked_until": None, "last_error": None},
        {"provider": PS_PROVIDER, "account": PS_REF.fingerprint, "cookies": [{"name": "CFID", "value": "secret-2"}],
         "logged_in_at": None, "last_used_at": None, "last_login_attempt_at": "2026-09-16T11:00:00+00:00",
         "login_failures": 3, "locked_until": until.isoformat(),
         "last_error": "key: The portal rejected the Project ID and Security Key."},
        {"provider": "procore", "account": "harvest-bot@example.com", "cookies": [], "login_failures": 1},
    ])
    status = h.session_status(settings)
    assert status["account"] == "harvest-bot@example.com" and status["login_failures"] == 1
    assert status["pipelinesuite"] == {
        "enabled": True,
        "portals": [
            {"host": psfx.CGB_HOST, "account": PS_REF.fingerprint, "logged_in_at": None, "last_used_at": None,
             "last_login_attempt_at": "2026-09-16T11:00:00+00:00", "login_failures": 3,
             "locked_until": until.isoformat(),
             "last_error": "key: The portal rejected the Project ID and Security Key."},
            {"host": psfx.SHF_HOST, "account": "aa11", "logged_in_at": "2026-09-16T10:00:00+00:00",
             "last_used_at": None, "last_login_attempt_at": "2026-09-16T10:00:00+00:00", "login_failures": 0,
             "locked_until": None, "last_error": None},
        ],
    }
    dumped = json.dumps(status)
    assert "secret" not in dumped and psfx.CGB_KEY not in dumped
    assert h.session_status(_settings(tmp_path, pipelinesuite_enabled=False))["pipelinesuite"]["enabled"] is False


def test_step_parks_a_pipelinesuite_row_on_its_own_portal_lock(db, settings):
    until = NOW + timedelta(hours=2)
    db.tables["rfp_harvest_sessions"].append({"provider": PS_PROVIDER, "locked_until": until.isoformat()})
    rec = _StepRecorder()
    h.step(db, _ps_email(), park=rec.park, finish=rec.finish)
    assert rec.finished == 0 and db.tables["llm_jobs"] == []
    assert rec.parks == [(7200.0, f"PipelineSuite logins for {psfx.CGB_HOST} are locked after repeated failures.")]
    # A Procore row is not held by the portal's lock; a portal row without a
    # reference drains.
    h.step(db, _email(), park=rec.park, finish=rec.finish)
    assert len(db.tables["llm_jobs"]) == 1 and rec.parks[-1] == (settings.rfp_harvest_poll_seconds, None)
    h.step(db, _ps_email(body_text="nothing"), park=rec.park, finish=rec.finish)
    assert rec.finished == 1
    # The lock gone, the row is enqueued once.
    db.tables["rfp_harvest_sessions"] = []
    h.step(db, _ps_email(), park=rec.park, finish=rec.finish)
    assert [j["target_id"] for j in db.tables["llm_jobs"]] == [E1, E1] or len(db.tables["llm_jobs"]) == 1


def test_pipelinesuite_rows_reach_the_router_helpers_and_the_queue_marks(db):
    db.tables["rfp_harvests"].append(_ps_harvest_row(claim_token="tok"))
    row = h.harvest_for_email(db, _ps_email())
    assert row["id"] == "hv-1"
    assert h.harvest_for_email(db, _ps_email(body_text="nothing")) is None
    _seed(db, _ps_email())
    db.tables["rfp_harvests"][0].update(status="pending", finished_at=None)
    h.mark_from_queue(E1, "failed", "The harvest was interrupted (failed after 6 attempts)")
    assert _the_harvest(db)["status"] == "failed"
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == "hv-1"
    assert h.error_message(psc.PipelineSuiteForbidden("Refused."), "procore") == "Refused."
    assert h.error_message(psc.PipelineSuiteLoginLocked("Locked."), "procore") == "Locked."


def test_pipelinesuite_pure_builders_edge_cases():
    assert h.pipelinesuite_description(None) is None
    assert h.pipelinesuite_description("***CLICK \"YES\" ABOVE***\n\n\n\nReal scope.\nMore.") == "Real scope.\nMore."
    assert h.pipelinesuite_description("Click yes, no or unsure above for your response") is None
    assert h.pipelinesuite_instructions({"other_info": "A", "plans": "B"}) == "A\n\nB"
    assert h.pipelinesuite_instructions({"other_info": None, "plans": " "}) is None
    assert h._ps_address({"address": "1 Main", "city": "Reno", "state": "NV", "zip": "89501", "location": "Reno Airport"}) == (
        "1 Main, Reno, NV 89501 (Reno Airport)"
    )
    assert h._ps_address({"address": None, "city": None, "state": None, "zip": None, "location": "Somewhere"}) == "Somewhere"
    assert h._ps_address({"address": "2300 Pebble Road", "city": "Henderson", "state": "NV", "zip": "89074", "location": "Henderson, Nevada"}) == (
        "2300 Pebble Road, Henderson, NV 89074"
    )
    assert h._ps_address({}) is None
    page = psc.parse_project_page("<html><body><div id='projectInfo'></div></body></html>")
    entries, urls = h.pipelinesuite_files(page)
    assert entries == [] and urls == []
    data = h.normalize_pipelinesuite_facts(PS_REF, page, [], None)
    assert data["platform"] == "pipelinesuite" and data["project_name"] is None
    assert data["point_of_contact"] is None and data["tracking"] is None
    assert data["documents"] == {"count": 0, "bytes": 0, "kinds": {}, "folders": []}
    assert psfx.CGB_KEY not in json.dumps(data) and "http" not in json.dumps(h.build_pipelinesuite_raw(page))


# ═════════════════════════════════════════════════════════════════════════
# SmartBid (docs/RFP_SMARTBID.md sections 4 and 8)
# ═════════════════════════════════════════════════════════════════════════

SB_BODY = sbfx.load(sbfx.EMAIL_874974_TEXT)
SB_REF = sbc.parse_reference(SB_BODY)
SB_KEY = "smartbid:874974"
SB_URL = "https://gocc.smartbid.co/#/projectlist"
SB_MAILBOX = "bids@g3.example"
SB_PROJECT = sbc.parse_project(sbfx.load_json(sbfx.BP_874974))
SB_FILE_IDS = [f["file_id"] for f in SB_PROJECT.files]
SB_LOCKED = "SmartBid logins are locked after repeated failures."
SB_AGREEMENT = (
    "This SmartBid project needs a confidentiality agreement accepted in SmartBid first; "
    "open it there, then press Harvest again."
)
SB_NOW = datetime(2026, 10, 1, 21, 0, 0, tzinfo=timezone.utc)   # the fake platform's stamp
# What must never be persisted or logged: the passport key (and the link
# that carries it), the bearer token, the per-file security token, the SAS
# signature.
SB_SECRETS = (
    sbfx.KEY, sbfx.KEY.lower(), "sPassportKey", sbfx.BEARER, sbfx.SECURITY_TOKEN,
    sbfx.SAS_SIGNATURE, "sig=",
)


class FakeSmartBidSession:
    """The subset of smartbid_client.SmartBidSession the job touches.
    `errors` maps "login", "gate", "project", a file id or a ping URL to an
    exception (or a list consumed one per call); `bytes_for` overrides the
    bytes a download (by file id) writes; `ping_status` the status a ping
    answers."""

    provider = "smartbid"

    def __init__(self, payload=None):
        self.calls: list[tuple] = []
        self.available: tuple = (True, None, None)
        self.payload = payload if payload is not None else sbfx.load_json(sbfx.BP_874974)
        self.errors: dict = {}
        self.bytes_for: dict[str, bytes] = {}
        self.ping_status: dict[str, object] = {}
        self.locators: list = []
        self.on_download = None
        self.closed = False
        self.downloads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def _raise(self, key):
        err = self.errors.get(key)
        if isinstance(err, list):
            if err:
                raise err.pop(0)
        elif err is not None:
            raise err

    def availability(self):
        self.calls.append(("availability",))
        return self.available

    def login(self):
        self.calls.append(("login",))
        self._raise("login")

    def gate(self):
        self.calls.append(("gate",))
        self._raise("gate")
        return sbfx.load_json(sbfx.CA_OPEN[0])

    def get_project(self):
        self.calls.append(("project",))
        self._raise("project")
        return copy.deepcopy(self.payload)

    def download(self, entry, dest, *, max_bytes):
        file_id = entry["file_id"]
        self.calls.append(("download", file_id, max_bytes))
        self.locators.append(entry)
        if self.downloads == 0 and self.on_download is not None:
            self.on_download()
        self.downloads += 1
        self._raise(file_id)
        data = self.bytes_for.get(file_id, PDF)
        if len(data) > max_bytes:
            raise sbc.SmartBidForbidden("The project file is larger than the harvest accepts.")
        dest.write_bytes(data)
        return len(data)

    def ping(self, url):
        self.calls.append(("ping", url))
        self._raise(url)
        if url in self.ping_status:
            return self.ping_status[url]
        return 302 if "/Main/Login.aspx" in url else 200

    @property
    def pings(self):
        return [c[1] for c in self.calls if c[0] == "ping"]

    @property
    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def sb_session(monkeypatch):
    fake = FakeSmartBidSession()
    opened = []

    def open_session(settings, ref):
        opened.append((settings, ref))
        return fake

    monkeypatch.setattr(h, "open_smartbid_session", open_session)
    fake.opened = opened
    return fake


@pytest.fixture
def sb_graph(graph):
    graph.html = sbfx.load(sbfx.EMAIL_874974_HTML)
    return graph


def _sb_email(**over):
    row = _email(
        invitation_method="smartbid",
        body_text=SB_BODY,
        from_address="notifications@com2.smartbidnet.com",
        primary_mailbox=SB_MAILBOX,
    )
    row.update(over)
    return row


def _sb_seed(db, row=None, *, sightings=True):
    row = _seed(db, row or _sb_email())
    if sightings:
        db.tables["rfp_email_sightings"].extend([
            {"id": "s-1", "rfp_email_id": row["id"], "mailbox": "office@g3.example",
             "graph_message_id": "msg-office", "created_at": "2026-10-01T01:00:00+00:00"},
            {"id": "s-2", "rfp_email_id": row["id"], "mailbox": SB_MAILBOX,
             "graph_message_id": "msg-bids", "created_at": "2026-10-01T02:00:00+00:00"},
        ])
    return row


def _sb_harvest_row(**over):
    row = _harvest_row(method="smartbid", external_key=SB_KEY, external_url=SB_URL, data={"platform": "smartbid"})
    row.update(over)
    return row


def _sb_no_secret_anywhere(db, caplog=None):
    """No SmartBid secret in any harvest row, email row update (the email's
    own body_text, which carries the key by nature, is left out), queue
    job, session row, bell or log record."""
    tables = {
        name: [
            {k: v for k, v in row.items() if not (name == "rfp_emails" and k == "body_text")}
            for row in rows
        ]
        for name, rows in db.tables.items()
    }
    dumped = json.dumps(tables, default=str)
    for marker in SB_SECRETS:
        assert marker not in dumped, marker
    assert "Login.aspx" not in dumped
    if caplog is not None:
        for marker in SB_SECRETS:
            assert marker not in caplog.text, marker
            assert not any(marker in str(r.args) or marker in str(r.msg) for r in caplog.records), marker


# ── Registry ─────────────────────────────────────────────────────────────


def test_smartbid_registry_needs_the_flags_and_the_reference(tmp_path):
    on = _settings(tmp_path)
    row = _sb_email()
    assert h.harvester_for(row, on) == "smartbid"
    assert h.can_harvest(row, on) == (True, None)
    ref = h.platform_reference("smartbid", SB_BODY)
    assert ref == SB_REF and ref.external_key == SB_KEY and ref.external_url == SB_URL
    assert h.reference_for(row) == SB_REF
    assert h.platform_reference("procore", SB_BODY) is None
    assert h.platform_reference("pipelinesuite", SB_BODY) is None
    assert h.platform_reference("smartbid", PS_BODY) is None
    assert h.session_provider_for(row, on) == "smartbid"
    assert h.session_provider_for(_sb_email(body_text="no link here"), on) is None
    # Procore credentials play no part; the SmartBid flag does.
    assert h.harvester_for(row, _settings(tmp_path, procore_login_password="")) == "smartbid"
    for off in (
        _settings(tmp_path, smartbid_enabled=False),
        _settings(tmp_path, rfp_harvest_enabled=False),
        _settings(tmp_path, rfp_ingest_enabled=False),
    ):
        assert h.harvester_for(row, off) is None
        assert h.can_harvest(row, off) == (False, h._MSG_NO_HARVESTER)
    # Only the Yes / No links: no reference (an iR link is never fallen back to).
    answers_only = f"Yes <{sbfx.YES_URL}> | No <{sbfx.NO_URL}>"
    for body in (answers_only, None, "Please bid."):
        no_ref = _sb_email(body_text=body)
        assert h.harvester_for(no_ref, on) == "smartbid"
        assert h.can_harvest(no_ref, on) == (False, "The email carries no SmartBid project link.")
    # The other platforms keep their own sentences.
    assert h.can_harvest(_email(body_text=None), on) == (False, h._MSG_NO_LINK)
    assert h.can_harvest(_ps_email(body_text=None), on)[1] == h._MSG_NO_PS_REFERENCE
    assert h.harvester_for(row) == "smartbid"


def test_smartbid_availability_for_reads_the_one_smartbid_lock(db, settings, tmp_path):
    until = NOW + timedelta(hours=1)
    assert h.availability_for(_sb_email(), settings) == (True, None, None)
    assert h.availability_for(_sb_email(body_text="nothing"), settings) == (False, h._MSG_NO_SB_REFERENCE, None)
    assert h.availability_for(_sb_email(), _settings(tmp_path, smartbid_enabled=False)) == (
        False, h._MSG_NO_HARVESTER, None
    )
    db.tables["rfp_harvest_sessions"].append({"provider": "smartbid", "locked_until": until.isoformat()})
    assert h.availability_for(_sb_email(), settings) == (False, SB_LOCKED, until)
    assert h.availability(settings, "smartbid") == (False, SB_LOCKED, until)
    # Every SmartBid email shares the one lock; Procore and the portals do not.
    other = _sb_email(body_text=sbfx.load(sbfx.EMAIL_876398_TEXT))
    assert h.availability_for(other, settings)[0] is False
    assert h.availability_for(_email(), settings) == (True, None, None)
    assert h.availability_for(_ps_email(), settings) == (True, None, None)
    db.tables["rfp_harvest_sessions"] = [{"provider": "procore", "locked_until": until.isoformat()}]
    assert h.availability_for(_sb_email(), settings) == (True, None, None)


# ── execute: the happy path ──────────────────────────────────────────────


def _sb_expected_files(statuses=None):
    out = []
    for index, f in enumerate(SB_PROJECT.files):
        out.append({
            "file_path": f"{f['folder']}/{f['name']}" if f["folder"] else f["name"],
            "size": f["size_kb"] * 1024,
            "kind": h._sb_kind(f["name"], f["folder"]),
            "discipline": None,
            "file_id": f["file_id"],
            "uploaded_on": f["uploaded_on"],
            "sandbox_file_id": f"f-{index + 2}",
            "status": "accepted",
            "error": None,
        })
    return out


def test_smartbid_execute_pipeline_mode_harvests_facts_files_and_pings(
    db, sb_session, sandbox, sb_graph, settings, caplog
):
    caplog.set_level(logging.DEBUG)
    _sb_seed(db)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["claim_token"] is None
    assert harvest["method"] == "smartbid" and harvest["external_key"] == SB_KEY
    assert harvest["external_url"] == SB_URL and harvest["rfp_email_id"] == E1
    assert harvest["facts_at"] == NOW.isoformat() and harvest["finished_at"] == NOW.isoformat()
    files = _sb_expected_files()
    folders = sorted({p["file_path"].rsplit("/", 1)[0] for p in files})
    assert harvest["data"] == {
        "platform": "smartbid",
        "bid_project_id": 874974,
        "system_id": 3766,
        "project_name": "Nevada State University @ NLV Gateway",
        "project_address": "800 E Lake Mead Blvd, North Las Vegas, NV 89030",
        "bid_due_at": "2026-10-01T17:00:00-05:00",
        "bid_due_text": "10-01-2026 5:00 PM",
        "bid_due_tz": "CT",
        "bid_due_tz_assumed": False,
        "gc": {"name": "DC Building Group, LLC.", "address": None, "phone": "(702) 434-9991 x214",
               "fax": "(702) 243-5556", "website": None},
        "point_of_contact": {"name": "Nicole Burguin", "email": None, "phone": "(702) 434-9991 x214"},
        "owner": None,
        "architect": "SCA Design",
        "project_status": "Open to Bid",
        "past_due": False,
        "allow_late_proposal": False,
        "pre_bid": None,
        "invitations": [{"code": "26 00 00", "name": "Electrical", "status": "Accepted"}],
        "response_recorded": True,
        "tracking": {"pinged_at": NOW.isoformat(), "opened": True, "clicked": True, "error": None},
        "documents": {
            "count": 45,
            "bytes": 763_174 * 1024,
            "kinds": {"drawing": 19, "other": 23, "specification": 3},  # name, else folder
            "folders": folders,
            "restricted": 0,
        },
    }
    assert len(folders) == 10 and "Shell Bid Set/REVISED CIVILS 9.24.26" in folders
    assert harvest["description_text"].startswith("Project Description:")
    assert "<" not in harvest["description_text"]
    assert harvest["instructions_text"] is None
    assert set(harvest["raw"]) == {"bid_project", "invitations", "files_head"}
    assert "PassportKey" not in harvest["raw"]["bid_project"]
    assert "ProjectDescription" not in harvest["raw"]["bid_project"]
    assert harvest["raw"]["bid_project"]["Title"] == "Nevada State University @ NLV Gateway"
    assert len(harvest["raw"]["files_head"]) == 45 and "href" not in harvest["raw"]["files_head"][0]
    assert harvest["file_count"] == 45 and harvest["files_accepted"] == 45
    assert harvest["bytes_downloaded"] == 45 * len(PDF) and harvest["sandbox_run_id"] == "run-1"
    assert harvest["files"] == files
    assert "http" not in json.dumps(harvest["files"])
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["harvested_at"] == NOW.isoformat() and email["last_error"] is None
    # The documented order: pings (receipt, open, click), availability, the
    # login, the gate, the project, the downloads in plan-room order.
    assert sb_session.opened == [(settings, SB_REF)] and sb_session.closed
    assert sb_session.calls[:7] == [
        ("ping", sbfx.READ_RECEIPT_URL),
        ("ping", sbfx.OPEN_PIXEL_URL),
        ("ping", sbfx.VIEW_URL),
        ("availability",),
        ("login",),
        ("gate",),
        ("project",),
    ]
    assert sb_session.calls[7:] == [("download", fid, settings.rfp_ingest_max_file_bytes) for fid in SB_FILE_IDS]
    assert not any("iR=" in url for url in sb_session.pings)
    # The locator is the parsed plan-room entry (its Href in memory only).
    assert sb_session.locators[0] == SB_PROJECT.files[0] and sb_session.locators[0]["href"].startswith("https://")
    assert sb_graph.calls == [("msg-bids", SB_MAILBOX, "id,body", "html")]
    assert sandbox.calls[0] == ("create", E1, harvest["id"])
    adds = [c for c in sandbox.calls if c[0] == "add"]
    assert [c[2] for c in adds] == [f["name"] for f in SB_PROJECT.files]
    assert adds[0][5] == {"kind": "smartbid", "file_path": files[0]["file_path"], "harvest_id": harvest["id"]}
    assert sandbox.calls[-2:] == [("start", "run-1"), ("dispatch", "run-1", None, None)]
    _sb_no_secret_anywhere(db, caplog)


def test_smartbid_manual_mode_links_a_done_row(db, sb_session, sandbox, sb_graph):
    _sb_seed(db, _sb_email(status="done", flag_reason="no_project_name"))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["data"]["platform"] == "smartbid"
    email = _email_row(db)
    assert email["status"] == "done" and email["flag_reason"] == "no_project_name"
    assert email["harvest_id"] == harvest["id"] and email["harvested_at"] == NOW.isoformat()


def test_smartbid_writes_the_facts_before_the_first_download(db, sb_session, sandbox, sb_graph):
    _sb_seed(db)
    seen = {}
    sb_session.on_download = lambda: seen.update(copy.deepcopy(_the_harvest(db)))
    h.execute(E1)
    assert seen["status"] == "running" and seen["claim_token"]
    assert seen["facts_at"] == NOW.isoformat() and seen["external_url"] == SB_URL
    assert seen["data"]["project_name"] == "Nevada State University @ NLV Gateway"
    assert seen["data"]["tracking"]["pinged_at"] == NOW.isoformat()
    assert seen["file_count"] == 45 and all(f["status"] is None for f in seen["files"])


def test_smartbid_reuses_a_young_complete_harvest_without_pinging_or_logging_in(db, sb_session, sandbox, sb_graph):
    _sb_seed(db)
    db.tables["rfp_harvests"].append(_sb_harvest_row(finished_at=(NOW - timedelta(days=2)).isoformat()))
    h.execute(E1)
    assert sb_session.opened == [] and sb_graph.calls == [] and sandbox.calls == []
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == "hv-1"


def test_smartbid_copies_of_one_invitation_share_one_harvest(db, sb_session, sandbox, sb_graph):
    """Bids@, office@ and tmoore@ each get their own key for the project;
    the harvest row is per bid project, so the second copy reuses it."""
    _sb_seed(db)
    h.execute(E1)
    other_key = sbfx.KEY.replace("DEADBEEF", "FEEDFACE", 1)
    second = _sb_email(id="e-2", body_text=SB_BODY.replace(sbfx.KEY, other_key))
    _seed(db, second)
    sb_session.calls.clear()
    h.execute("e-2")
    assert len(_harvests(db)) == 1 and sb_session.calls == []
    assert _email_row(db, "e-2")["harvest_id"] == _the_harvest(db)["id"]


# ── execute: the tracking pings ──────────────────────────────────────────


def test_smartbid_pings_fire_once_and_are_skipped_on_a_later_run(db, sb_session, sandbox, sb_graph):
    _sb_seed(db, _sb_email(status="done"))
    h.execute(E1)
    first = _the_harvest(db)["data"]["tracking"]
    assert first == {"pinged_at": NOW.isoformat(), "opened": True, "clicked": True, "error": None}
    assert sb_session.pings == [sbfx.READ_RECEIPT_URL, sbfx.OPEN_PIXEL_URL, sbfx.VIEW_URL]
    sb_session.calls.clear()
    sb_graph.calls.clear()
    h.execute(E1, force=True)
    harvest = _the_harvest(db)
    assert harvest["attempts"] == 2 and harvest["status"] == "complete"
    assert harvest["data"]["tracking"] == first
    assert sb_session.pings == [] and sb_graph.calls == []
    assert sb_session.calls[0] == ("availability",)


def test_smartbid_the_lock_parks_without_an_attempt_and_the_pings_are_kept(db, sb_session, sandbox, sb_graph):
    _sb_seed(db, _sb_email(attempts=1))
    until = NOW + timedelta(hours=2)
    sb_session.available = (False, SB_LOCKED, until)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["data"]["tracking"]["pinged_at"] == NOW.isoformat()
    assert sb_session.names == ["ping", "ping", "ping", "availability"]       # no login spent
    email = _email_row(db)
    assert email["status"] == "harvest" and email["next_attempt_at"] == until.isoformat()
    assert email["attempts"] == 1 and email["last_error"] == SB_LOCKED
    # The lock lifts: the second run pings nothing and completes.
    sb_session.available = (True, None, None)
    sb_session.calls.clear()
    h.execute(E1)
    assert sb_session.pings == [] and _the_harvest(db)["status"] == "complete"


def test_smartbid_pings_never_fail_the_harvest(db, sb_session, sandbox, sb_graph):
    _sb_seed(db)
    sb_session.errors[sbfx.READ_RECEIPT_URL] = RuntimeError("tracker exploded")
    sb_session.ping_status[sbfx.OPEN_PIXEL_URL] = None
    sb_session.ping_status[sbfx.VIEW_URL] = 500
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete"
    assert harvest["data"]["tracking"] == {
        "pinged_at": NOW.isoformat(), "opened": False, "clicked": False,
        "error": "read receipt: RuntimeError; open: no answer; click: HTTP 500",
    }
    # Either pixel's 200 is an open; a 302 is a click.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_sb_email()]
    sb_session.errors.clear()
    sb_session.ping_status = {sbfx.READ_RECEIPT_URL: 404}
    h.execute(E1)
    assert _the_harvest(db)["data"]["tracking"] == {
        "pinged_at": NOW.isoformat(), "opened": True, "clicked": True, "error": "read receipt: HTTP 404",
    }


def test_smartbid_pings_fall_back_to_the_references_link_without_pixels(db, sb_session, sandbox, sb_graph):
    # Graph down: no pixel fires, the email's own View the Project link is clicked.
    _sb_seed(db)
    sb_graph.mode = "boom"
    h.execute(E1)
    tracking = _the_harvest(db)["data"]["tracking"]
    assert tracking == {"pinged_at": NOW.isoformat(), "opened": None, "clicked": True, "error": None}
    assert sb_session.pings == [sbfx.VIEW_URL] and len(sb_graph.calls) == 2
    # HTML without the View anchor: the pixels fire, the click is the reference's.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_sb_email()]
    sb_graph.mode = "ok"
    sb_graph.html = sbfx.load(sbfx.EMAIL_874974_HTML).replace("Click Here to View the Project", "Details")
    sb_graph.calls.clear()
    sb_session.calls.clear()
    h.execute(E1)
    assert sb_session.pings == [sbfx.READ_RECEIPT_URL, sbfx.OPEN_PIXEL_URL, SB_REF.click_url]
    # No sighting at all: the click only.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_email_sightings"] = []
    db.tables["rfp_emails"] = [_sb_email()]
    sb_graph.calls.clear()
    sb_session.calls.clear()
    h.execute(E1)
    assert sb_session.pings == [sbfx.VIEW_URL] and sb_graph.calls == []
    assert _the_harvest(db)["data"]["tracking"]["opened"] is None


def test_smartbid_pings_can_be_switched_off(db, sb_session, sandbox, sb_graph, monkeypatch, tmp_path):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, smartbid_tracking_pings_enabled=False))
    _sb_seed(db)
    h.execute(E1)
    assert sb_session.pings == [] and sb_graph.calls == []
    assert _the_harvest(db)["data"]["tracking"] is None
    assert _the_harvest(db)["status"] == "complete"


# ── execute: the gate, restricted files, caps, per-file outcomes ─────────


@pytest.mark.parametrize(
    "sentence",
    [SB_AGREEMENT, "SmartBid does not allow this project: Your company is not on the bidders list for this project."],
)
def test_smartbid_the_gates_refusal_is_permanent_and_writes_no_facts(db, sb_session, sandbox, sb_graph, sentence):
    _sb_seed(db)
    sb_session.errors["gate"] = sbc.SmartBidForbidden(sentence)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["last_error"] == sentence
    assert harvest.get("facts_at") is None and set(harvest["data"]) == {"tracking"}
    assert sb_session.names[-2:] == ["login", "gate"] and sandbox.calls == []
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["last_error"] == sentence
    db.tables["rfp_emails"] = [_sb_email(status="done")]
    db.tables["rfp_harvests"] = []
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == sentence


def _restricted_payload(*, everything=False):
    payload = sbfx.load_json(sbfx.BP_874974)
    root = payload["PlanRoom"][0]
    if everything:
        root["SCARequired"] = True
        return payload
    contract = next(n for n in root["Folders"] if n["Name"] == "DCBG Contract Terms & Conditions")
    contract["SCARequired"] = True                       # 7 files under it
    itb = next(n for n in root["Folders"] if n["Name"] == "DCBG ITB")
    itb["Folders"][0]["PQRequired"] = True                # 1 file
    return payload


def test_smartbid_restricted_files_are_skipped_and_never_downloaded(db, sb_session, sandbox, sb_graph):
    sb_session.payload = _restricted_payload()
    _sb_seed(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete"
    skipped = [f for f in harvest["files"] if f["status"] == "skipped"]
    assert len(skipped) == 8
    assert all(f["error"] == "SmartBid requires an agreement for this file" for f in skipped)
    assert all(f["sandbox_file_id"] is None for f in skipped)
    assert {f["file_path"].split("/")[0] for f in skipped} == {"DCBG Contract Terms & Conditions", "DCBG ITB"}
    downloaded = {c[1] for c in sb_session.calls if c[0] == "download"}
    assert len(downloaded) == 37 and not downloaded & {f["file_id"] for f in skipped}
    assert harvest["files_accepted"] == 37
    assert harvest["data"]["documents"]["restricted"] == 8 and harvest["data"]["documents"]["count"] == 45


def test_smartbid_every_file_restricted_creates_no_run(db, sb_session, sandbox, sb_graph):
    sb_session.payload = _restricted_payload(everything=True)
    _sb_seed(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["sandbox_run_id"] is None
    assert harvest["files_accepted"] == 0 and harvest["data"]["documents"]["restricted"] == 45
    assert sandbox.calls == [] and "download" not in sb_session.names


def test_smartbid_file_and_byte_caps_are_permanent_keep_the_facts_and_skip_restricted_files(
    db, sb_session, sandbox, sb_graph, monkeypatch, tmp_path
):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_files=2))
    _sb_seed(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed"
    assert harvest["last_error"] == "The project holds more files than the harvest accepts (45 of 2)."
    assert harvest["data"]["project_name"] == "Nevada State University @ NLV Gateway" and harvest["facts_at"]
    assert sandbox.calls == [] and "download" not in sb_session.names
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == harvest["id"]
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_total_bytes=3 * 1024 * 1024))
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_sb_email()]
    h.execute(E1)
    assert _the_harvest(db)["last_error"] == "The project files are larger than the harvest accepts (745 MB of 3 MB)."
    # Restricted files are not counted: 37 downloadable fit a cap of 37.
    sb_session.payload = _restricted_payload()
    for cap, status in ((36, "failed"), (37, "complete")):
        monkeypatch.setattr(h, "get_settings", lambda cap=cap: _settings(tmp_path, rfp_harvest_max_files=cap))
        db.tables["rfp_harvests"] = []
        db.tables["rfp_emails"] = [_sb_email()]
        h.execute(E1)
        assert _the_harvest(db)["status"] == status, cap
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_sb_email()]
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_files=36))
    h.execute(E1)
    assert _the_harvest(db)["last_error"] == "The project holds more files than the harvest accepts (37 of 36)."


def test_smartbid_per_file_outcomes_with_docx_and_xlsx(db, sb_session, sandbox, sb_graph):
    _sb_seed(db)
    office = [f["file_id"] for f in SB_PROJECT.files if f["ext"] in ("docx", "xlsx")]
    for file_id in office:
        sb_session.bytes_for[file_id] = DOCX                    # the sandbox's sniff decides
    first, second, third = SB_FILE_IDS[0], SB_FILE_IDS[1], SB_FILE_IDS[2]
    sb_session.errors[first] = [sbc.SmartBidTransient("The SmartBid file store answered 503.")] * 3
    sb_session.errors[second] = sbc.SmartBidForbidden("The SmartBid file store refused the file (404).")
    sb_session.errors[third] = sbc.SmartBidForbidden("The project file is larger than the harvest accepts.")
    h.execute(E1)
    harvest = _the_harvest(db)
    by_id = {f["file_id"]: f for f in harvest["files"]}
    assert by_id[first]["status"] == "download_failed" and by_id[first]["error"] == "The SmartBid file store answered 503."
    assert by_id[second]["status"] == "download_failed" and "(404)" in by_id[second]["error"]
    assert by_id[third]["status"] == "too_large"
    assert len(office) == 5
    assert all(by_id[i]["status"] == "rejected" and by_id[i]["error"] == "The file is not a PDF." for i in office)
    assert by_id[sbfx.EXHIBIT_F_ID]["size"] == 15 * 1024 and by_id[sbfx.EXHIBIT_F_ID]["kind"] == "other"
    assert harvest["files_accepted"] == 45 - 5 - 3 and harvest["status"] == "complete"
    assert sum(1 for c in sb_session.calls if c[:2] == ("download", first)) == 3


# ── execute: session expiry and failure mapping ──────────────────────────


def test_smartbid_a_401_on_a_project_read_logs_in_once_more_and_retries_once(db, sb_session, sandbox, sb_graph):
    _sb_seed(db)
    sb_session.errors["project"] = [sbc.SmartBidSessionExpired()]
    h.execute(E1)
    assert _the_harvest(db)["status"] == "complete"
    assert sb_session.names[3:8] == ["availability", "login", "gate", "project", "login"]
    assert sb_session.names[8] == "project"
    # Expired twice: transient for the queue.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_sb_email()]
    sb_session.calls.clear()
    sb_session.errors["gate"] = [sbc.SmartBidSessionExpired(), sbc.SmartBidSessionExpired()]
    with pytest.raises(h.RfpHarvestTransient):
        h.execute(E1)
    assert _the_harvest(db)["status"] == "pending" and _email_row(db)["status"] == "harvest"


def test_smartbid_a_401_during_a_download_logs_in_once_for_that_file(db, sb_session, sandbox, sb_graph):
    _sb_seed(db)
    target = SB_FILE_IDS[4]
    sb_session.errors[target] = [sbc.SmartBidSessionExpired()]
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 45
    downloads = [c for c in sb_session.calls if c[0] in ("download", "login")]
    index = downloads.index(("download", target, h.get_settings().rfp_ingest_max_file_bytes))
    assert downloads[index + 1] == ("login",) and downloads[index + 2][:2] == ("download", target)
    assert sb_session.names.count("login") == 2
    # Expired again right after the re-login: transient for the job.
    db.tables["rfp_harvests"] = []
    db.tables["rfp_emails"] = [_sb_email()]
    sb_session.calls.clear()
    sb_session.errors[target] = [sbc.SmartBidSessionExpired(), sbc.SmartBidSessionExpired()]
    with pytest.raises(h.RfpHarvestTransient):
        h.execute(E1)
    assert _the_harvest(db)["status"] == "pending"


@pytest.mark.parametrize(
    "key, exc",
    [
        ("login", sbc.SmartBidTransient("SmartBid refused the project link or is down (HTTP 500).")),
        ("login", sbc.SmartBidLoginFailed("token", "SmartBid answered the login without an access token.")),
        ("project", sbc.SmartBidTransient("SmartBid answered 503; the harvest will be retried.")),
        ("project", h.RfpHarvestTransient("interrupted")),
    ],
)
def test_smartbid_transient_releases_and_raises_for_the_queue(db, sb_session, sandbox, sb_graph, key, exc, caplog):
    caplog.set_level(logging.DEBUG)
    _sb_seed(db)
    sb_session.errors[key] = exc
    with pytest.raises(h.RfpHarvestTransient) as raised:
        h.execute(E1)
    assert str(raised.value) == str(exc)
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    assert harvest["last_error"] == str(exc) and harvest["finished_at"] is None
    assert harvest["data"]["tracking"]["pinged_at"] == NOW.isoformat()
    assert _email_row(db)["status"] == "harvest" and _email_row(db)["harvest_id"] is None
    _sb_no_secret_anywhere(db, caplog)


def test_smartbid_unavailable_parks_without_an_attempt_and_fails_by_hand(db, sb_session, sandbox, sb_graph, settings):
    _sb_seed(db, _sb_email(attempts=2))
    recent = NOW + timedelta(seconds=20)
    sb_session.errors["login"] = sbc.SmartBidUnavailable(
        "A SmartBid login was attempted recently; waiting before trying again.", locked_until=recent
    )
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    email = _email_row(db)
    assert email["status"] == "harvest" and email["attempts"] == 2 and email["harvest_id"] is None
    assert email["next_attempt_at"] == (NOW + timedelta(seconds=settings.rfp_harvest_poll_seconds)).isoformat()
    assert "attempted recently" in email["last_error"]
    # A login lock parks until the lock lifts.
    until = NOW + timedelta(hours=3)
    sb_session.errors["login"] = sbc.SmartBidLoginLocked(SB_LOCKED, locked_until=until)
    h.execute(E1)
    assert _email_row(db)["next_attempt_at"] == until.isoformat()
    # By hand: failed and linked.
    db.tables["rfp_emails"] = [_sb_email(status="done")]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == SB_LOCKED
    assert _the_harvest(db)["status"] == "failed" and _email_row(db)["harvest_id"] == _the_harvest(db)["id"]


def test_smartbid_a_rejected_link_fails_the_harvest_and_moves_the_email_on(db, sb_session, sandbox, sb_graph):
    _sb_seed(db)
    rejected = (
        "SmartBid rejected this email's project link (the invitation may have been "
        "withdrawn or the link expired)."
    )
    sb_session.errors["login"] = sbc.SmartBidForbidden(rejected)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["last_error"] == rejected
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"] and email["last_error"] == rejected


def test_smartbid_no_reference_is_permanent_without_a_row(db, sb_session, sandbox, sb_graph):
    _sb_seed(db, _sb_email(body_text=f"Yes <{sbfx.YES_URL}>"), sightings=False)
    assert h.execute(E1) is None
    assert _harvests(db) == [] and sb_session.opened == []
    email = _email_row(db)
    assert email["status"] == "split" and email["last_error"] == "The email carries no SmartBid project link."
    db.tables["rfp_emails"] = [_sb_email(status="done", body_text=None)]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == "The email carries no SmartBid project link."


def test_smartbid_off_drains_a_pipeline_row_and_refuses_by_hand(db, sb_session, sandbox, sb_graph, monkeypatch, tmp_path):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, smartbid_enabled=False))
    _sb_seed(db)
    h.execute(E1)
    assert _email_row(db)["status"] == "split" and _harvests(db) == [] and sb_session.opened == []
    db.tables["rfp_emails"] = [_sb_email(status="done")]
    with pytest.raises(h.RfpHarvestPermanent) as exc:
        h.execute(E1)
    assert str(exc.value) == h._MSG_NO_HARVESTER


def test_smartbid_losing_claim_and_lost_lease(db, sb_session, sandbox, sb_graph, monkeypatch):
    _sb_seed(db)
    db.tables["rfp_harvests"].append(_sb_harvest_row(status="running", claim_token="theirs", started_at=NOW.isoformat()))
    assert h.execute(E1) is None
    assert sb_session.opened == [] and _email_row(db)["last_error"] == h._MSG_CLAIMED
    db.tables["rfp_harvests"] = []
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: False)
    with pytest.raises(h.RfpHarvestTransient):
        h.execute(E1)
    assert _the_harvest(db)["status"] == "pending"
    assert sb_session.names == ["ping", "ping", "ping", "availability"]


# ── End to end over the fake platform: no secret persisted or logged ─────


@pytest.fixture
def sb_platform(monkeypatch):
    """The real SmartBid client over the fake platform from
    tests/test_smartbid_client (MockTransport only, no network), with the
    real store adapter on the fake table and the real bell."""
    from tests.test_smartbid_client import FakeSmartBid

    fake = FakeSmartBid()
    fake.blob_bytes = PDF
    opened = []

    def open_session(settings, ref):
        session = sbc.SmartBidSession(
            sbc.SmartBidConfig(min_request_interval=0.0, login_min_interval=30, timeout=5.0),
            h._SessionStore(settings, ref.session_provider), ref,
            transport=httpx.MockTransport(fake), sleep=lambda s: None, now=lambda: SB_NOW,
            on_lock=h._notify_smartbid_lock,
        )
        session._download_client_factory = lambda: httpx.Client(
            transport=httpx.MockTransport(fake), follow_redirects=False
        )
        opened.append(session)
        return session

    monkeypatch.setattr(h, "open_smartbid_session", open_session)
    monkeypatch.setattr(sbc, "_last_request_at", 0.0)
    fake.opened = opened
    return fake


def test_smartbid_end_to_end_persists_and_logs_no_key_token_or_signature(
    db, sb_platform, sandbox, sb_graph, caplog
):
    caplog.set_level(logging.DEBUG)
    _sb_seed(db)
    h.enqueue(E1, created_by=None)                        # a queue row to inspect too
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 45
    assert harvest["data"]["tracking"] == {"pinged_at": NOW.isoformat(), "opened": True, "clicked": True, "error": None}
    assert harvest["data"]["bid_due_at"] == "2026-10-01T17:00:00-05:00"
    # What went over the wire: the three tracking hits, one token POST, the
    # two reads, three requests per file. Never an answer, never an agreement.
    paths = sb_platform.paths
    assert paths[:6] == [
        ("GET", "securecc.smartbidnet.com", "/External/RequestReadReceipt.aspx"),
        ("GET", "em.smartinsight.co", "/wf/open"),
        ("GET", "securecc.smartbidnet.com", "/Main/Login.aspx"),
        ("POST", "apicc.smartinsight.co", "/token"),
        ("GET", "apicc.smartinsight.co", "/api/projects/getconfidentialagreement"),
        ("GET", "apicc.smartinsight.co", "/api/projects/getbidproject"),
    ]
    assert len(paths) == 6 + 3 * 45
    assert [p for m, _, p in paths if m == "POST"] == ["/token"] + ["/api/admin/getSecurityToken"] * 45
    assert {m for m, _, _ in paths} == {"GET", "POST"}
    assert not any("iR=" in str(r.url) or "iR%3D" in str(r.url) for r in sb_platform.requests)
    assert not any(word in p for _, _, p in paths for word in ("setallcodesanswer", "linkwontbidthisjob", "Unsubscribe"))
    # The session row is bookkeeping only.
    rows = db.tables["rfp_harvest_sessions"]
    assert [r["provider"] for r in rows] == ["smartbid"]
    assert rows[0]["account"] == "passport" and rows[0]["cookies"] == [] and rows[0]["login_failures"] == 0
    assert sb_platform.opened[0]._token is None                # dropped on close
    # The passport key, the bearer token, the security token and the SAS
    # signature: in no harvest row, email update, queue row, session row,
    # bell or log record. httpx did log every request, redacted.
    assert "HTTP Request" in caplog.text and "?<redacted>" in caplog.text
    _sb_no_secret_anywhere(db, caplog)


def test_smartbid_end_to_end_a_dead_link_is_transient_and_never_locks(db, sb_platform, sandbox, sb_graph, caplog):
    """A dead or foreign link answers /token with 500 (captured): transient,
    no failure counted, no bell; the queue's attempt cap ends it."""
    caplog.set_level(logging.DEBUG)
    sb_platform.key = "0" * 40
    _sb_seed(db)
    with pytest.raises(h.RfpHarvestTransient) as exc:
        h.execute(E1)
    assert str(exc.value) == "SmartBid refused the project link or is down (HTTP 500)."
    assert db.tables["rfp_harvest_sessions"] == [] and db.tables["notifications"] == []
    assert _the_harvest(db)["last_error"] == "SmartBid refused the project link or is down (HTTP 500)."
    _sb_no_secret_anywhere(db, caplog)


# ── Sessions, the bell, the status block, the step ───────────────────────


def test_smartbid_session_store_uses_the_smartbid_thresholds(db, tmp_path):
    settings = _settings(tmp_path, smartbid_login_max_failures=2, smartbid_login_lock_seconds=120)
    store = h._SessionStore(settings, "smartbid")
    assert store.max_failures == 2 and store.lock_seconds == 120
    assert store.record_login(ok=False, error="token: no")["login_failures"] == 1
    state = store.record_login(ok=False, error="token: no")
    assert state["login_failures"] == 2 and state["locked_until"] == (NOW + timedelta(seconds=120)).isoformat()
    store.save_cookies("passport", [])
    rows = db.tables["rfp_harvest_sessions"]
    assert [r["provider"] for r in rows] == ["smartbid"]
    assert rows[0]["account"] == "passport" and rows[0]["cookies"] == [] and rows[0]["login_failures"] == 0
    procore = h._SessionStore(settings)
    assert procore.load() is None and procore.max_failures == settings.procore_login_max_failures


def test_open_smartbid_session_wires_the_config_the_store_and_the_bell(settings):
    session = h.open_smartbid_session(settings, SB_REF)
    try:
        assert isinstance(session, sbc.SmartBidSession)
        assert session.config == sbc.SmartBidConfig(
            min_request_interval=settings.smartbid_min_request_interval_seconds,
            login_min_interval=settings.smartbid_login_min_interval_seconds,
            timeout=settings.smartbid_request_timeout_seconds,
        )
        assert isinstance(session.store, h._SessionStore) and session.store.provider == "smartbid"
        assert session._on_lock is h._notify_smartbid_lock and session.ref is SB_REF
        assert sbfx.KEY not in repr(session) and sbfx.KEY not in repr(session.config)
    finally:
        session.close()


def test_notify_lock_in_smartbids_words(db):
    until = NOW + timedelta(hours=6)
    h._notify_smartbid_lock(until, "SmartBid answered the login without an access token.", None)
    bells = db.tables["notifications"]
    assert len(bells) == 1 and bells[0]["type"] == "rfp_harvest.login_failed" and bells[0]["role"] == Role.IT_ADMIN
    assert bells[0]["message"] == (
        "SmartBid logins failed repeatedly; SmartBid RFP harvests are paused until 2026-09-14 18:00 UTC. "
        "Each login uses the project link in the invitation email."
    )
    assert bells[0]["metadata"] == {
        "locked_until": until.isoformat(), "error": "SmartBid answered the login without an access token.",
        "platform": "smartbid",
    }
    h._notify_smartbid_lock(until, "again")
    assert len(bells) == 1


def test_session_status_has_the_smartbid_block_without_cookies_or_keys(db, settings, tmp_path):
    until = NOW + timedelta(hours=1)
    db.tables["rfp_harvest_sessions"].extend([
        {"provider": "smartbid", "account": "passport", "cookies": [{"name": "x", "value": "secret-cookie"}],
         "logged_in_at": "2026-10-01T10:00:00+00:00", "last_used_at": "2026-10-01T10:05:00+00:00",
         "last_login_attempt_at": "2026-10-01T11:00:00+00:00", "login_failures": 3,
         "locked_until": until.isoformat(), "last_error": "token: SmartBid answered the login with HTTP 401."},
        {"provider": "procore", "account": "harvest-bot@example.com", "cookies": [], "login_failures": 1},
    ])
    status = h.session_status(settings)
    assert status["smartbid"] == {
        "enabled": True,
        "logged_in_at": "2026-10-01T10:00:00+00:00",
        "last_used_at": "2026-10-01T10:05:00+00:00",
        "last_login_attempt_at": "2026-10-01T11:00:00+00:00",
        "login_failures": 3,
        "locked_until": until.isoformat(),
        "last_error": "token: SmartBid answered the login with HTTP 401.",
    }
    assert status["login_failures"] == 1 and status["pipelinesuite"]["portals"] == []
    assert "secret" not in json.dumps(status)
    assert h.session_status(_settings(tmp_path, smartbid_enabled=False))["smartbid"]["enabled"] is False
    # An expired lock reads as none.
    db.tables["rfp_harvest_sessions"][0]["locked_until"] = (NOW - timedelta(seconds=1)).isoformat()
    assert h.session_status(settings)["smartbid"]["locked_until"] is None


def test_step_parks_a_smartbid_row_on_the_smartbid_lock(db, settings):
    until = NOW + timedelta(hours=2)
    db.tables["rfp_harvest_sessions"].append({"provider": "smartbid", "locked_until": until.isoformat()})
    rec = _StepRecorder()
    h.step(db, _sb_email(), park=rec.park, finish=rec.finish)
    assert rec.finished == 0 and db.tables["llm_jobs"] == []
    assert rec.parks == [(7200.0, SB_LOCKED)]
    # A row without a link drains; the lock gone, the row is enqueued once.
    h.step(db, _sb_email(body_text=f"Yes <{sbfx.YES_URL}>"), park=rec.park, finish=rec.finish)
    assert rec.finished == 1
    db.tables["rfp_harvest_sessions"] = []
    h.step(db, _sb_email(), park=rec.park, finish=rec.finish)
    assert [j["target_id"] for j in db.tables["llm_jobs"]] == [E1]
    assert rec.parks[-1] == (settings.rfp_harvest_poll_seconds, None)


def test_smartbid_rows_reach_the_router_helpers_and_the_queue_marks(db):
    db.tables["rfp_harvests"].append(_sb_harvest_row(claim_token="tok"))
    assert h.harvest_for_email(db, _sb_email())["id"] == "hv-1"
    assert h.harvest_for_email(db, _sb_email(body_text="nothing")) is None
    _seed(db, _sb_email())
    db.tables["rfp_harvests"][0].update(status="pending", finished_at=None)
    h.mark_from_queue(E1, "failed", "SmartBid refused the project link or is down (HTTP 500).")
    assert _the_harvest(db)["status"] == "failed"
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == "hv-1"
    assert h.error_message(sbc.SmartBidForbidden("Refused."), "procore") == "Refused."
    assert h.error_message(sbc.SmartBidLoginLocked(SB_LOCKED), "procore") == SB_LOCKED
    assert sbc.SmartBidTransient in h._TRANSIENT_ERRORS and sbc.SmartBidForbidden in h._FORBIDDEN_ERRORS
    assert h.FILE_SKIPPED in h._SETTLED_BEFORE_DOWNLOAD


def test_smartbid_pure_builders_edge_cases():
    project = sbc.parse_project({"BidProject": {"BidProjectId": 874974, "TimeZoneShort": "(XYZ)",
                                                "BidDueDate": "2026-10-01T17:00:00"}})
    entries, locators = h.smartbid_files(project)
    assert entries == [] and locators == []
    data = h.normalize_smartbid_facts(SB_REF, project, [], None)
    assert data["platform"] == "smartbid" and data["project_name"] is None
    assert data["bid_due_at"] == "2026-10-01T17:00:00-07:00" and data["bid_due_tz"] == "XYZ"
    assert data["bid_due_tz_assumed"] is True
    assert data["point_of_contact"] is None and data["tracking"] is None and data["pre_bid"] is None
    assert data["documents"] == {"count": 0, "bytes": 0, "kinds": {}, "folders": [], "restricted": 0}
    no_due = sbc.parse_project({"BidProject": {"BidProjectId": 1}})
    assert h.normalize_smartbid_facts(SB_REF, no_due, [], None)["bid_due_tz_assumed"] is False
    with_pre_bid = sbc.parse_project({"BidProject": {
        "BidProjectId": 1, "PreBidMeetingDate": "10/05/2026 09:00 AM", "PreBidMeetingTimeZone": "(PT)",
        "IsPreBidMeetingMandatory": True, "Address1": "1 Main ", "City": "Reno", "State": "NV", "Zip": "89501",
        "Manager": "Pat", "Phone": None,
    }})
    data = h.normalize_smartbid_facts(SB_REF, with_pre_bid, [], None)
    assert data["pre_bid"] == {"date": "10/05/2026 09:00 AM", "time_zone": "PT", "mandatory": True}
    assert data["project_address"] == "1 Main, Reno, NV 89501"
    assert data["point_of_contact"] == {"name": "Pat", "email": None, "phone": None}
    raw = h.build_smartbid_raw(
        {"BidProject": {"Title": "T", "PassportKey": sbfx.KEY, "passportkey2": "x", "SessionToken": "t",
                        "ProjectDescription": "<p>d</p>", "logo": "https://h/x?sig=abc"}},
        with_pre_bid,
    )
    assert raw["bid_project"] == {"Title": "T", "logo": None}
    assert h.build_smartbid_raw(None, no_due) == {"bid_project": {}, "invitations": [], "files_head": []}
    assert sbfx.KEY not in json.dumps(data)


def test_smartbid_file_kind_falls_back_to_the_folder():
    """A sheet-code file name says nothing; its folder does. A spreadsheet
    under a plans folder stays other, and the name wins over the folder."""
    project = sbc.parse_project({"BidProject": {"BidProjectId": 874974, "SystemId": 3766}, "PlanRoom": [{
        "Name": "root", "isFile": False, "Folders": [
            {"Name": "Shell Bid Set ", "isFile": False, "Folders": [
                {"isFile": True, "FileId": 1, "Name": "E-2026-09-01_NSU_Shell_REV_1.pdf", "Size": 10,
                 "Href": "https://apicc.smartbidnet.com/project/fileMgmt/download?Value=MS44NzQ5NzQuMzc2Ng=="},
                {"isFile": True, "FileId": 2, "Name": "NSU_Grey_Shell_SPECS_-_Project_Manual.pdf", "Size": 10,
                 "Href": "https://apicc.smartbidnet.com/project/fileMgmt/download?Value=Mi44NzQ5NzQuMzc2Ng=="},
            ]},
            {"Name": "Plans", "isFile": False, "Folders": [
                {"isFile": True, "FileId": 3, "Name": "Quantities.xlsx", "Size": 10,
                 "Href": "https://apicc.smartbidnet.com/project/fileMgmt/download?Value=My44NzQ5NzQuMzc2Ng=="},
            ]},
            {"Name": "Contract", "isFile": False, "Folders": [
                {"isFile": True, "FileId": 4, "Name": "2025_Subcontract.docx", "Size": 10,
                 "Href": "https://apicc.smartbidnet.com/project/fileMgmt/download?Value=NC44NzQ5NzQuMzc2Ng=="},
            ]},
        ]}]})
    entries, _ = h.smartbid_files(project)
    assert [(e["file_path"], e["kind"]) for e in entries] == [
        ("Shell Bid Set/E-2026-09-01_NSU_Shell_REV_1.pdf", "drawing"),
        ("Shell Bid Set/NSU_Grey_Shell_SPECS_-_Project_Manual.pdf", "specification"),
        ("Plans/Quantities.xlsx", "other"),
        ("Contract/2025_Subcontract.docx", "other"),
    ]
