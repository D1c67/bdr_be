"""The email harvester (app/services/rfp_email_harvest and its seams in
rfp_harvest / rfp_email_ingest) against the fake DB, sandbox and queue from
tests/test_rfp_harvest, with `cloud_folders`, `rfp_zip` and the Graph calls
replaced by the recording fakes in tests/fixtures_email_harvest
(docs/RFP_HARVEST.md 2.5, 4, 9).

Pinned, in the doc's order:

- the registry seams: `harvester_for` / `can_harvest` / `reference_for` /
  `availability_for` / `session_provider_for` for the three methods and
  the off switch, the selects that carry `attachments_meta`, the step and
  the match exit (`_park_done`) routing an organic row with attachments to
  `harvest` and one with nothing to `done`;
- `execute` in pipeline mode: attachments re-listed live (primary sighting
  first, a 404 falls back, every copy gone is permanent, the mailbox down
  is transient), the image and attached-email policy applied to the live
  listing, share links from the text body, the HTML body and the reference
  attachments (deduped, body order, the resolve cap), every link recorded
  whatever happened (`needs_sign_in` and `unsupported` keep the URL), zips
  opened now (members entered, skipped members and images recorded, an
  unreadable or unfetchable zip recorded on its entry), per-link reuse of
  an earlier harvest's accepted files (skipped under `force`), the facts
  written before the first download, the caps over what still needs
  downloading, the download loop skipping reused and expanded entries,
  `files_accepted` and `documents`, and no locator on any stored entry;
- manual mode, the session adapter's error mapping, `prior_link_files`,
  `mark_from_queue` and `harvest_for_email` finding an email harvest by
  its key, `error_message`.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.services import graph_inbox, llm_queue, rfp_email_ingest as ingest, rfp_ingest
from app.services import rfp_email_harvest as eh
from app.services import rfp_harvest as h
from tests import fixtures_email_harvest as fx
from tests.test_rfp_harvest import (
    E1,
    NOW,
    PDF,
    FakeSandbox,
    HarvestDB,
    _email,
    _email_row,
    _harvest_row,
    _harvests,
    _seed,
    _settings,
    _StepRecorder,
    _the_harvest,
)

MAILBOX = "rfp@g3.example"
OTHER_MAILBOX = "other@g3.example"
BODY = (
    "Please find the bid documents for 26-080 UMC MLK Warehouse Remodel here:\n"
    f"{fx.SHAREPOINT_URL}\n\nBids are due Friday. Learn more at "
    "https://aka.ms/LearnAboutSenderIdentification"
)
SP_BYTES = sum(r.size for r in fx.SP_FILES)

CloudForbidden = eh.cloud_folders.CloudForbidden
CloudTransient = eh.cloud_folders.CloudTransient
CloudUnavailable = eh.cloud_folders.CloudUnavailable


def _att(id_, meta, *, inline=False, kind="#microsoft.graph.fileAttachment", content_id=None):
    """A live Graph listing row from a stored-meta shape."""
    return {
        "@odata.type": kind, "id": id_, "name": meta["name"], "contentType": meta.get("contentType"),
        "size": meta["size"], "isInline": inline, "contentId": content_id,
    }


ATT_PDF1 = _att("att-1", fx.OCTET_PDF)
ATT_PDF2 = _att("att-2", fx.REAL_PDF)
ATT_SIG = _att("att-3", fx.IMAGE001, inline=True, content_id="image001.jpg@01DC")
ATT_ZIP = _att("att-9", fx.ZIP_FILE)
ATT_REFERENCE = _att("att-7", {"name": "Plans", "size": 0, "contentType": None},
                     kind="#microsoft.graph.referenceAttachment")


# ── Fixtures ─────────────────────────────────────────────────────────────


class EmailHarvestDB(HarvestDB):
    """The harvest fake plus real jsonb containment for `.contains`, which
    the reuse query needs (`data @> {"links": [{"key": ...}]}`)."""

    def table(self, name):
        query = super().table(name)
        query.contains = lambda col, val: query._add(
            lambda row: fx.jsonb_contains(row.get(col), val)
        )
        return query


@pytest.fixture
def db():
    fake = EmailHarvestDB({
        "rfp_emails": [],
        "rfp_harvests": [],
        "rfp_harvest_sessions": [],
        "rfp_email_sightings": [],
        "rfp_ingest_runs": [],
        "llm_jobs": [],
        "notifications": [],
    })
    fake.unique = {**HarvestDB.unique, "rfp_harvests": [("method", "external_key")]}
    fake.defaults = {
        **HarvestDB.defaults,
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
    """The tests/test_rfp_harvest environment (its `_env`, redone here so
    the fixture is this module's own) plus the harvester's own clock."""
    monkeypatch.setattr(h, "get_settings", lambda: settings)
    monkeypatch.setattr(h, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: settings)
    monkeypatch.setattr(h, "_now", lambda: NOW)
    clock = SimpleNamespace(sleep=lambda s: sleeps.append(s), monotonic=time.monotonic)
    monkeypatch.setattr(h, "time", clock)
    monkeypatch.setattr(eh, "time", clock)
    monkeypatch.setattr(
        h, "notify_role",
        lambda role, project_id, type_, message, **k: db.tables["notifications"].append(
            {"type": type_, "message": message, "role": role, "read_at": None,
             "dismissed_at": None, "metadata": k.get("metadata")}),
    )


@pytest.fixture
def sandbox(monkeypatch, db):
    fake = FakeSandbox(db)
    for name in ("create_harvest_run", "add_upload_file", "start_run", "dispatch", "delete_run"):
        monkeypatch.setattr(rfp_ingest, name, getattr(fake, name))
    return fake


@pytest.fixture
def cloud(monkeypatch):
    fake = fx.FakeCloud()
    fake.links = [fx.SHAREPOINT_LINK, fx.DROPBOX_LINK, fx.SHAREFILE_LINK, fx.ONEDRIVE_LINK]
    fake.listings[fx.SHAREPOINT_KEY] = fx.listing("listed", list(fx.SP_FILES))
    for name in ("find_share_links", "link_from_url", "resolve", "download"):
        monkeypatch.setattr(eh.cloud_folders, name, getattr(fake, name))
    return fake


@pytest.fixture
def zips(monkeypatch):
    fake = fx.FakeZip()
    monkeypatch.setattr(eh.rfp_zip, "inspect", fake.inspect)
    return fake


@pytest.fixture
def graph(monkeypatch):
    fake = fx.FakeGraph([ATT_PDF1, ATT_PDF2, ATT_SIG])
    monkeypatch.setattr(eh, "graph_request", fake.graph_request)
    monkeypatch.setattr(graph_inbox, "list_reference_links", fake.list_reference_links)
    monkeypatch.setattr(graph_inbox, "download_attachment_to_file", fake.download_attachment_to_file)
    monkeypatch.setattr(graph_inbox, "get_message", fake.get_message)
    return fake


def _organic(**over):
    row = _email(
        invitation_method="organic",
        body_text=BODY,
        from_address="pm@eoc.example",
        primary_mailbox=MAILBOX,
        attachments_meta=[fx.OCTET_PDF, fx.REAL_PDF, fx.IMAGE001],
        subject="Invitation to Bid: 26-080 UMC MLK Warehouse Remodel",
    )
    row.update(over)
    return row


def _seed_email(db, row=None, *, sightings=True):
    row = _seed(db, row or _organic())
    if sightings:
        db.tables["rfp_email_sightings"].extend([
            {"id": "s-1", "rfp_email_id": row["id"], "mailbox": OTHER_MAILBOX,
             "graph_message_id": "msg-other", "created_at": "2026-09-16T01:00:00+00:00"},
            {"id": "s-2", "rfp_email_id": row["id"], "mailbox": MAILBOX,
             "graph_message_id": "msg-primary", "created_at": "2026-09-16T02:00:00+00:00"},
        ])
    return row


def _prior_harvest(**over):
    """An earlier complete harvest of another email that listed the same
    SharePoint folder and accepted its first two files."""
    row = _harvest_row(
        id="hv-0", rfp_email_id="e-0", method="organic", external_key="email:e-0",
        external_url=None,
        data={"platform": "email", "links": [{"key": fx.SHAREPOINT_KEY, "provider": "sharepoint",
                                                "status": "listed"}]},
        files=[
            {**eh.link_file_entry(r.path, r.size, provider="sharepoint", link_key=fx.SHAREPOINT_KEY),
             "status": "accepted", "sandbox_file_id": f"f-old-{i + 1}"}
            for i, r in enumerate(fx.SP_FILES[:2])
        ],
        files_accepted=2, sandbox_run_id="run-old",
        finished_at=(NOW - timedelta(days=2)).isoformat(),
    )
    row.update(over)
    return row


def _no_locator_on_entries(harvest):
    dumped = json.dumps(harvest["files"])
    for marker in ("http", "graph:", "zip:", "|", "msg-primary", "att-", MAILBOX, "rfp-harvest"):
        assert marker not in dumped, marker


# ── Registry seams ───────────────────────────────────────────────────────


def test_registry_answers_email_for_the_three_methods_under_the_switch(tmp_path, cloud):
    on = _settings(tmp_path)
    off = _settings(tmp_path, rfp_harvest_email_enabled=False)
    for method in ("organic", "general", "nonorganic"):
        row = _organic(invitation_method=method)
        assert h.harvester_for(row, on) == "email" == h.HARVESTER_EMAIL
        assert h.can_harvest(row, on) == (True, None)
        assert h.reference_for(row) == eh.EmailRef("email:e-1")
        assert h.availability_for(row, on) == (True, None, None)
        assert h.session_provider_for(row, on) is None
        assert h.harvester_for(row, off) is None
        assert h.can_harvest(row, off) == (False, h._MSG_NO_HARVESTER)
        assert h.can_harvest(row, _settings(tmp_path, rfp_harvest_enabled=False)) == (False, h._MSG_NO_HARVESTER)
    # Nothing to harvest: a harvester, but the row cannot be harvested.
    bare = _organic(attachments_meta=[fx.IMAGE001], body_text="Please bid.")
    assert h.harvester_for(bare, on) == "email"
    assert h.reference_for(bare) is None
    assert h.can_harvest(bare, on) == (False, h._MSG_NO_EMAIL_FILES)
    assert h._MSG_NO_EMAIL_FILES == "The email carries no attachments or share links to harvest."
    # The other methods still go through the body parsers.
    assert h.reference_for({"invitation_method": "procore", "body_text": "x"}) is None
    assert h.reference_for({"invitation_method": "gc_portal", "body_text": BODY}) is None
    assert h.harvester_for(_organic(invitation_method="gc_portal"), on) is None
    # SmartBid (0148) has its own harvester; the email harvester never claims it.
    assert h.reference_for({"invitation_method": "smartbid", "body_text": BODY}) is None
    assert h.harvester_for(_organic(invitation_method="smartbid"), on) == "smartbid"


def test_the_selects_carry_what_the_trigger_reads():
    for column in ("attachments_meta", "subject", "from_address", "primary_mailbox", "body_text"):
        assert column in h._EMAIL_SELECT, column
    for column in ("attachments_meta", "from_address", "body_text"):
        assert column in ingest._SWEEP_SELECT, column
    assert "isInline" in ingest._ATTACHMENT_SELECT
    assert h.FILE_REUSED == "reused" and h.FILE_EXPANDED == "expanded"
    assert h._EMAIL_ERRORS == (CloudUnavailable, CloudForbidden, eh.cloud_folders.CloudError)
    assert CloudTransient in h._TRANSIENT_ERRORS and CloudForbidden in h._FORBIDDEN_ERRORS


def test_list_attachment_meta_stores_inline(monkeypatch):
    seen = {}

    def request(method, path, *, params=None, **kwargs):
        seen["params"] = params
        return httpx.Response(200, json={"value": [
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a", "name": "image001.jpg",
             "contentType": "image/jpeg", "size": 5, "isInline": True},
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "b", "name": "plans.pdf",
             "contentType": "application/pdf", "size": 9},
        ]}, request=httpx.Request("GET", "https://graph.microsoft.com/x"))

    monkeypatch.setattr(ingest, "graph_request", request)
    assert ingest._list_attachment_meta(MAILBOX, "m") == [
        {"name": "image001.jpg", "contentType": "image/jpeg", "size": 5, "kind": "file", "inline": True},
        {"name": "plans.pdf", "contentType": "application/pdf", "size": 9, "kind": "file", "inline": False},
    ]
    assert seen["params"] == {"$select": "id,name,contentType,size,isInline"}


def test_step_and_park_done_route_an_organic_row_by_its_files(db, settings, cloud, monkeypatch, tmp_path):
    rec = _StepRecorder()
    h.step(db, _organic(status="harvest"), park=rec.park, finish=rec.finish)
    assert rec.finished == 0 and len(db.tables["llm_jobs"]) == 1
    assert rec.parks == [(settings.rfp_harvest_poll_seconds, None)]
    # Links only is enough; neither drains; the switch off drains.
    h.step(db, _organic(id="e-2", attachments_meta=[]), park=rec.park, finish=rec.finish)
    assert rec.finished == 0 and len(db.tables["llm_jobs"]) == 2
    h.step(db, _organic(id="e-3", attachments_meta=[fx.MSG_FILE], body_text="Please bid."),
           park=rec.park, finish=rec.finish)
    assert rec.finished == 1 and len(db.tables["llm_jobs"]) == 2
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_email_enabled=False))
    h.step(db, _organic(id="e-4"), park=rec.park, finish=rec.finish)
    assert rec.finished == 2 and len(db.tables["llm_jobs"]) == 2
    # The match step's exit (rfp_email_ingest._park_done) makes the same call.
    monkeypatch.setattr(h, "get_settings", lambda: settings)
    fields = {"match_candidates": [], "match_project_id": None, "attempts": 0}
    row = _seed(db, _organic(id="e-5", status="match"))
    assert ingest._park_done(db, row, "no_candidate", fields) == ("harvest", None)
    assert _email_row(db, "e-5")["status"] == "harvest" and row["status"] == "harvest"
    assert _email_row(db, "e-5")["flag_reason"] == "no_candidate"
    # Nothing to harvest: straight to the create step (docs/RFP_CREATE.md 3).
    row = _seed(db, _organic(id="e-6", status="match", attachments_meta=[], body_text="Please bid."))
    assert ingest._park_done(db, row, "no_project_name", fields) == ("create", None)
    assert _email_row(db, "e-6")["status"] == "create"
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_email_enabled=False))
    row = _seed(db, _organic(id="e-7", status="match"))
    assert ingest._park_done(db, row, "no_candidate", fields) == ("create", None)
    assert _email_row(db, "e-7")["status"] == "create"


# ── execute: the happy path ──────────────────────────────────────────────


def test_execute_pipeline_mode_harvests_attachments_and_a_folder_link(
    db, cloud, zips, graph, sandbox, settings, tmp_path
):
    _seed_email(db)
    assert h.execute(E1) is None
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["claim_token"] is None
    assert harvest["method"] == "organic" and harvest["external_key"] == "email:e-1"
    assert harvest["external_url"] is None and harvest["rfp_email_id"] == E1
    assert harvest["description_text"] is None and harvest["instructions_text"] is None
    assert harvest["facts_at"] == NOW.isoformat() and harvest["finished_at"] == NOW.isoformat()
    assert harvest["data"] == {
        "platform": "email",
        "attachments": {
            "count": 3,
            "files": 2,
            "images": [{"name": "image001.jpg", "size": 27784, "inline": True, "signature_like": True}],
            "skipped": [],
        },
        "links": [{
            "key": fx.SHAREPOINT_KEY, "provider": "sharepoint", "kind": "folder",
            "url": fx.SHAREPOINT_URL, "label": "26-080 UMC MLK Warehouse Remodel",
            "status": "listed", "file_count": 3, "bytes": SP_BYTES, "reused": 0, "error": None,
        }],
        "documents": {"count": 5, "bytes": 5 * len(PDF), "kinds": None, "disciplines": None},
    }
    assert harvest["file_count"] == 5 and harvest["files_accepted"] == 5
    assert harvest["bytes_downloaded"] == 5 * len(PDF) and harvest["sandbox_run_id"] == "run-1"
    expected = [
        {**eh.attachment_entry(fx.OCTET_PDF["name"], 1328229), "status": "accepted", "sandbox_file_id": "f-2"},
        {**eh.attachment_entry(fx.REAL_PDF["name"], 699554), "status": "accepted", "sandbox_file_id": "f-3"},
    ] + [
        {**eh.link_file_entry(r.path, r.size, provider="sharepoint", link_key=fx.SHAREPOINT_KEY),
         "status": "accepted", "sandbox_file_id": f"f-{i + 4}"}
        for i, r in enumerate(fx.SP_FILES)
    ]
    assert harvest["files"] == expected
    _no_locator_on_entries(harvest)
    # The only URL on the whole row is the GC's share link, once.
    assert json.dumps(harvest).count("http") == 1
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["harvested_at"] == NOW.isoformat() and email["last_error"] is None
    assert email["attempts"] == 0 and email["next_attempt_at"] is None
    assert email["flag_reason"] == "no_candidate"
    # Graph: the live listing from the primary mailbox's copy with the
    # select the policy needs, the HTML body, then the two documents
    # streamed under the cap; the signature image never.
    assert graph.calls[0] == ("request", "GET", f"/users/{MAILBOX}/messages/msg-primary/attachments",
                              {"$select": "id,name,contentType,size,isInline"})
    assert graph.calls[1] == ("message", "msg-primary", MAILBOX, "id,body", "html")
    assert [c for c in graph.calls if c[0] == "download"] == [
        ("download", MAILBOX, "msg-primary", "att-1", settings.rfp_ingest_max_file_bytes),
        ("download", MAILBOX, "msg-primary", "att-2", settings.rfp_ingest_max_file_bytes),
    ]
    assert not any(c[0] == "references" for c in graph.calls)
    # cloud_folders: the parser over the text body and the HTML (None here),
    # one resolve into the job's scratch, three downloads under the cap.
    assert cloud.calls[0] == ("find", BODY, None)
    resolve = cloud.calls[1]
    assert resolve[:2] == ("resolve", fx.SHAREPOINT_KEY) and resolve[3] is settings
    assert str(resolve[2]).startswith(str(tmp_path / "rfp-harvest" / "rfp-harvest-"))
    assert cloud.downloaded() == [r.locator for r in fx.SP_FILES]
    assert all(c[2] == settings.rfp_ingest_max_file_bytes for c in cloud.calls if c[0] == "download")
    # The sandbox: one run, one upload per file named by its basename with
    # the email source pointer, then start and dispatch.
    assert sandbox.calls[0] == ("create", E1, harvest["id"])
    adds = [c for c in sandbox.calls if c[0] == "add"]
    assert [c[2] for c in adds] == [
        fx.OCTET_PDF["name"], fx.REAL_PDF["name"],
        "Reference Material - Architectural - Drawing Index.pdf", "E1.01 Rev 1.pdf",
        "26 05 00 Common Work Results.pdf",
    ]
    assert adds[0][5] == {"kind": "email", "file_path": fx.OCTET_PDF["name"], "harvest_id": harvest["id"]}
    assert adds[2][5]["file_path"] == fx.SP_FILES[0].path
    assert sandbox.calls[-2:] == [("start", "run-1"), ("dispatch", "run-1", None, None)]
    assert zips.calls == []
    # The job's scratch tree is gone.
    assert list((tmp_path / "rfp-harvest").iterdir()) == []


def test_execute_writes_the_facts_before_the_first_download(db, cloud, zips, graph, sandbox):
    _seed_email(db)
    seen = {}
    graph.on_download = lambda: seen.update(copy.deepcopy(_the_harvest(db)))
    h.execute(E1)
    assert seen["status"] == "running" and seen["claim_token"]
    assert seen["facts_at"] == NOW.isoformat() and seen["external_url"] is None
    assert seen["data"]["platform"] == "email" and seen["data"]["links"][0]["status"] == "listed"
    assert seen["data"]["documents"] == {"count": 5, "bytes": 1328229 + 699554 + SP_BYTES,
                                         "kinds": None, "disciplines": None}
    assert seen["file_count"] == 5 and all(f["status"] is None for f in seen["files"])
    # Attachments lead the list, and the first fetch of any kind (a Graph
    # stream) came after the listing, the HTML and the resolve.
    assert seen["files"][0]["origin"] == "attachment"
    kinds = [c[0] for c in graph.calls]
    assert kinds[0] == "request" and kinds.index("message") < kinds.index("download")
    assert cloud.resolved() == [fx.SHAREPOINT_KEY]


def test_execute_manual_mode_links_a_done_row_with_links_only(db, cloud, zips, graph, sandbox):
    graph.attachments = []
    _seed_email(db, _organic(status="done", flag_reason="no_project_name", attachments_meta=[]))
    h.execute(E1, force=True)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 3
    assert harvest["data"]["attachments"] == {"count": 0, "files": 0, "images": [], "skipped": []}
    assert [f["origin"] for f in harvest["files"]] == ["link"] * 3
    email = _email_row(db)
    assert email["status"] == "done" and email["flag_reason"] == "no_project_name"
    assert email["harvest_id"] == harvest["id"] and email["harvested_at"] == NOW.isoformat()


def test_execute_reuses_its_own_young_complete_harvest_like_every_harvester(db, cloud, zips, graph, sandbox):
    _seed_email(db)
    db.tables["rfp_harvests"].append(_harvest_row(
        method="organic", external_key="email:e-1", external_url=None, data={"platform": "email"},
        finished_at=(NOW - timedelta(days=1)).isoformat(),
    ))
    h.execute(E1)
    assert graph.calls == [] and cloud.calls == [] and sandbox.calls == []
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == "hv-1"


# ── execute: attachments ─────────────────────────────────────────────────


def test_attachments_are_listed_from_the_next_sighting_when_the_primary_copy_is_gone(
    db, cloud, zips, graph, sandbox
):
    graph.gone.add(MAILBOX)
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 5
    listings = [c for c in graph.calls if c[0] == "request"]
    assert [c[2] for c in listings] == [
        f"/users/{MAILBOX}/messages/msg-primary/attachments",
        f"/users/{OTHER_MAILBOX}/messages/msg-other/attachments",
    ]
    assert [c[1:4] for c in graph.calls if c[0] == "download"] == [
        (OTHER_MAILBOX, "msg-other", "att-1"), (OTHER_MAILBOX, "msg-other", "att-2"),
    ]


def test_every_copy_gone_is_permanent(db, cloud, zips, graph, sandbox):
    graph.gone.update({MAILBOX, OTHER_MAILBOX})
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed"
    assert harvest["last_error"] == "The message is no longer in any watched mailbox."
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["last_error"] == harvest["last_error"]
    assert cloud.calls == [] and sandbox.calls == []
    # No sighting at all reads the same way.
    db.tables["rfp_emails"] = [_organic()]
    db.tables["rfp_harvests"] = []
    db.tables["rfp_email_sightings"] = []
    h.execute(E1)
    assert _the_harvest(db)["last_error"] == "The message is no longer in any watched mailbox."


@pytest.mark.parametrize("exc", [fx.http_error(503), fx.http_error(429),
                                 httpx.ConnectError("boom")])
def test_the_mailbox_not_answering_is_transient(db, cloud, zips, graph, sandbox, exc):
    graph.listing_error = exc
    _seed_email(db)
    with pytest.raises(h.RfpHarvestTransient) as raised:
        h.execute(E1)
    assert str(raised.value) == "The mailbox did not answer; the harvest will be retried."
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["claim_token"] is None
    assert harvest["last_error"] == str(raised.value)
    assert _email_row(db)["status"] == "harvest"


def test_the_mailbox_refusing_the_listing_is_permanent(db, cloud, zips, graph, sandbox):
    graph.listing_error = fx.http_error(403)
    _seed_email(db)
    h.execute(E1)
    assert _the_harvest(db)["status"] == "failed"
    assert _the_harvest(db)["last_error"] == "The mailbox refused the attachment listing (HTTP 403)."
    assert _email_row(db)["status"] == "split"


def test_the_live_listing_decides_the_policy_not_the_stored_meta(db, cloud, zips, graph, sandbox):
    """The stored meta said two PDFs; live, one was deleted, a site photo,
    an attached email and an inline PDF appeared, and the listing pages."""
    graph.attachments = [
        ATT_PDF1,
        _att("att-4", fx.SITE_PHOTO),
        _att("att-5", fx.MSG_FILE),
        _att("att-6", fx.INLINE_PDF, inline=True, content_id="scope@01DC"),
        _att("att-8", fx.OCTET_PNG),
        _att("item-1", fx.ITEM_ATTACHMENT, kind="#microsoft.graph.itemAttachment"),
    ]
    graph.pages = 2
    cloud.links = []
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["data"]["attachments"] == {
        "count": 6,
        "files": 2,
        "images": [
            {"name": "site-photo.jpg", "size": 2 * 1024 * 1024, "inline": False, "signature_like": False},
            {"name": fx.OCTET_PNG["name"], "size": 220783, "inline": False, "signature_like": False},
        ],
        "skipped": [
            {"name": "FW: Addendum 1.msg", "size": 45000, "reason": "attached_email"},
            {"name": "Original invitation", "size": 12000, "reason": "attached_email"},
        ],
    }
    assert [(f["file_path"], f["status"]) for f in harvest["files"]] == [
        (fx.OCTET_PDF["name"], "accepted"), ("Scope Letter.pdf", "accepted"),
    ]
    assert graph.downloaded() == ["att-1", "att-6"]
    listings = [c for c in graph.calls if c[0] == "request"]
    assert len(listings) == 2
    assert listings[0][3] == {"$select": "id,name,contentType,size,isInline"}
    assert listings[1][2].endswith("/attachments?$skip=1") and listings[1][3] is None
    assert harvest["data"]["links"] == [] and harvest["data"]["documents"]["count"] == 2


def test_per_attachment_download_outcomes(db, cloud, zips, graph, sandbox, sleeps):
    graph.attachments = [ATT_PDF1, ATT_PDF2, _att("att-3", {**fx.REAL_PDF, "name": "third.pdf"}),
                         _att("att-4", {**fx.REAL_PDF, "name": "fourth.pdf"})]
    graph.errors["att-1"] = graph_inbox.AttachmentTooLarge("The attachment is larger than the per-file limit.")
    graph.errors["att-2"] = graph_inbox.AttachmentNotStored("The attachment content is no longer available.")
    graph.errors["att-3"] = [fx.http_error(503), fx.http_error(502), httpx.ConnectError("x")]
    graph.errors["att-4"] = [fx.http_error(503)]
    cloud.links = []
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    # Three paced tries on transient trouble (the last error is the record),
    # one retry that recovers is accepted.
    assert [(f["status"], f["error"]) for f in harvest["files"]] == [
        ("too_large", "The attachment is larger than the per-file limit."),
        ("download_failed", "The attachment content is no longer available."),
        ("download_failed", "The mailbox did not answer; the harvest will be retried."),
        ("accepted", None),
    ]
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 1
    assert graph.downloaded() == ["att-1", "att-2", "att-3", "att-3", "att-3", "att-4", "att-4"]
    assert sleeps == [2.0, 4.0, 2.0]


# ── execute: zips ────────────────────────────────────────────────────────


def _zip_listing():
    return fx.zip_listing([
        fx.member(0, "Electrical/E1.01.pdf", 4096),
        fx.member(1, "more.zip", 10, "nested_zip"),
        fx.member(2, "Electrical/photo.jpg", 3000, "image"),
        fx.member(3, "secret.pdf", 50, "encrypted"),
        fx.member(4, "Specs/26 05 00.pdf", 2048),
    ])


def test_a_zip_attachment_is_opened_now_and_its_members_follow_it(
    db, cloud, zips, graph, sandbox, settings, tmp_path
):
    graph.attachments = [ATT_ZIP, ATT_PDF2]
    graph.bytes_for["att-9"] = fx.ZIP_BYTES
    zips.listings[fx.ZIP_BYTES] = _zip_listing()
    cloud.links = []
    seen = {}
    cloud.on_download = lambda: seen.update(copy.deepcopy(_the_harvest(db)))
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert [(f["file_path"], f["origin"], f["zip_of"], f["status"], f["sandbox_file_id"])
            for f in harvest["files"]] == [
        ("Drawings.zip", "attachment", None, "expanded", None),
        ("Drawings.zip/Electrical/E1.01.pdf", "zip", "Drawings.zip", "accepted", "f-2"),
        ("Drawings.zip/Specs/26 05 00.pdf", "zip", "Drawings.zip", "accepted", "f-3"),
        (fx.REAL_PDF["name"], "attachment", None, "accepted", "f-4"),
    ]
    assert all(f["provider"] == "graph" and f["link_key"] is None for f in harvest["files"])
    assert harvest["files"][0]["error"] is None
    assert harvest["data"]["attachments"] == {
        "count": 2,
        "files": 2,
        "images": [{"name": "photo.jpg", "size": 3000, "inline": False, "signature_like": True}],
        "skipped": [
            {"name": "Drawings.zip/more.zip", "size": 10, "reason": "nested_zip"},
            {"name": "Drawings.zip/secret.pdf", "size": 50, "reason": "encrypted"},
        ],
    }
    assert harvest["file_count"] == 4 and harvest["files_accepted"] == 3
    assert harvest["data"]["documents"] == {"count": 3, "bytes": 3 * len(PDF), "kinds": None, "disciplines": None}
    _no_locator_on_entries(harvest)
    # The zip itself was fetched through Graph before the facts write and
    # inspected with the image policy; the members through zip locators
    # into the job's scratch; the facts snapshot already showed it expanded.
    assert graph.downloaded() == ["att-9", "att-2"]
    assert len(zips.calls) == 1 and zips.calls[0]["name"] == "zip-0001.zip"
    assert zips.calls[0]["is_image_name"] is eh.is_image_name
    assert zips.calls[0]["max_members"] == settings.rfp_harvest_file_cap
    assert zips.calls[0]["max_total_bytes"] == settings.rfp_harvest_max_total_bytes
    assert zips.calls[0]["per_member_max_bytes"] == settings.rfp_ingest_max_file_bytes
    scratch = str(tmp_path / "rfp-harvest" / "rfp-harvest-")
    assert [loc.split("|")[1] for loc in cloud.downloaded()] == ["0", "4"]
    assert all(loc.startswith("zip:" + scratch) and loc.endswith("zip-0001.zip|" + i)
               for loc, i in zip(cloud.downloaded(), ("0", "4")))
    assert seen["files"][0]["status"] == "expanded" and seen["file_count"] == 4
    assert list((tmp_path / "rfp-harvest").iterdir()) == []
    # The sandbox saw the members by basename.
    assert [c[2] for c in sandbox.calls if c[0] == "add"] == ["E1.01.pdf", "26 05 00.pdf", fx.REAL_PDF["name"]]


@pytest.mark.parametrize(
    "arm", ["not_zip", "bomb", "declares_too_much", "too_large", "not_stored", "transient"]
)
def test_a_zip_that_cannot_be_fetched_or_opened_is_recorded_on_its_entry(
    db, cloud, zips, graph, sandbox, arm, sleeps
):
    graph.attachments = [ATT_ZIP]
    graph.bytes_for["att-9"] = fx.ZIP_BYTES
    cloud.links = []
    expected = {
        "not_zip": ("rejected", "The file is not a zip archive that can be opened."),
        "bomb": ("rejected", "The zip looks like a decompression bomb."),
        "declares_too_much": ("too_large", "The zip declares more bytes than the harvest accepts (900 MB)."),
        "too_large": ("too_large", "The attachment is larger than the per-file limit."),
        "not_stored": ("download_failed", "The attachment content is no longer available."),
        "transient": ("download_failed", "The mailbox did not answer; the harvest will be retried."),
    }[arm]
    if arm in ("not_zip", "bomb", "declares_too_much"):
        kind = {"not_zip": "not_zip", "bomb": "bomb", "declares_too_much": "too_large"}[arm]
        zips.listings[fx.ZIP_BYTES] = fx.zip_listing([], error=expected[1], error_kind=kind)
    elif arm == "too_large":
        graph.errors["att-9"] = graph_inbox.AttachmentTooLarge("The attachment is larger than the per-file limit.")
    elif arm == "not_stored":
        graph.errors["att-9"] = graph_inbox.AttachmentNotStored("The attachment content is no longer available.")
    else:
        graph.errors["att-9"] = [httpx.ConnectError("x")] * 3
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete"
    assert [(f["file_path"], f["status"], f["error"]) for f in harvest["files"]] == [
        ("Drawings.zip", expected[0], expected[1]),
    ]
    assert harvest["files_accepted"] == 0 and harvest["sandbox_run_id"] is None
    assert harvest["data"]["attachments"]["skipped"] == []
    assert sandbox.calls == []
    if arm == "transient":
        assert sleeps == [2.0, 4.0]


def test_a_zip_listed_in_a_folder_is_opened_and_a_dropbox_folder_zip_is_read_for_its_skips(
    db, cloud, zips, graph, sandbox, tmp_path
):
    graph.attachments = []
    zip_url = "https://eagle1lv.sharepoint.com/sites/EOCExternal/_api/web/GetFileByServerRelativeUrl('z')/$value"
    cloud.listings[fx.SHAREPOINT_KEY] = fx.listing("listed", [
        fx.remote("Addendum 2/Bid Set.zip", 5000, zip_url),
        fx.remote("Addendum 2/walk-photo.HEIC", 3 * 1024 * 1024, "https://eagle1lv.sharepoint.com/p"),
    ])
    cloud.bytes_for[zip_url] = fx.ZIP_BYTES
    zips.listings[fx.ZIP_BYTES] = fx.zip_listing([fx.member(0, "E2.01.pdf", 100)], truncated=True)
    # The Dropbox folder: cloud_folders serves the zip it fetched into
    # scratch, listed with no image policy (the photo is in `files`), and
    # names it so the skipped members can be read with ours.
    dropbox_zip = tmp_path / "dropbox-1.zip"
    dropbox_zip.write_bytes(fx.DROPBOX_ZIP_BYTES)
    zips.listings[fx.DROPBOX_ZIP_BYTES] = fx.zip_listing([
        fx.member(0, "Plans/A1.pdf", 300),
        fx.member(1, "Plans/inner.zip", 300, "nested_zip"),
        fx.member(2, "Plans/photo.jpg", 5000, "image"),
        fx.member(3, "Plans/empty.pdf", 0, "empty"),
    ])
    cloud.listings[fx.DROPBOX_KEY] = fx.listing("listed", [
        fx.remote("Plans/A1.pdf", 300, f"zip:{dropbox_zip}|0"),
        fx.remote("Plans/photo.jpg", 5000, f"zip:{dropbox_zip}|2"),
    ], zip_path=str(dropbox_zip))
    _seed_email(db, _organic(attachments_meta=[], body_text=f"{fx.SHAREPOINT_URL}\n{fx.DROPBOX_URL}"))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert [(f["file_path"], f["origin"], f["zip_of"], f["link_key"], f["provider"], f["status"])
            for f in harvest["files"]] == [
        ("Addendum 2/Bid Set.zip", "link", None, fx.SHAREPOINT_KEY, "sharepoint", "expanded"),
        ("Addendum 2/Bid Set.zip/E2.01.pdf", "zip", "Addendum 2/Bid Set.zip", fx.SHAREPOINT_KEY, "sharepoint", "accepted"),
        ("Plans/A1.pdf", "zip", None, fx.DROPBOX_KEY, "dropbox", "accepted"),
    ]
    assert harvest["files"][0]["error"] == "The zip holds more members than the harvest lists; the rest were left."
    assert harvest["data"]["attachments"]["images"] == [
        {"name": "walk-photo.HEIC", "size": 3 * 1024 * 1024, "inline": False, "signature_like": False},
        {"name": "photo.jpg", "size": 5000, "inline": False, "signature_like": True},
    ]
    assert harvest["data"]["attachments"]["skipped"] == [
        {"name": "Plans/inner.zip", "size": 300, "reason": "nested_zip"},
        {"name": "Plans/empty.pdf", "size": 0, "reason": "empty"},
    ]
    assert [c["name"] for c in zips.calls] == ["zip-0001.zip", "dropbox-1.zip"]
    assert cloud.downloaded()[0] == zip_url
    assert cloud.downloaded()[1].startswith("zip:") and cloud.downloaded()[1].endswith("zip-0001.zip|0")
    assert cloud.downloaded()[2:] == [f"zip:{dropbox_zip}|0"]
    assert [(r["key"], r["file_count"], r["bytes"]) for r in harvest["data"]["links"]] == [
        (fx.SHAREPOINT_KEY, 1, 5000), (fx.DROPBOX_KEY, 1, 300),
    ]
    _no_locator_on_entries(harvest)


# ── execute: links ───────────────────────────────────────────────────────


def test_links_come_from_the_body_the_html_and_the_reference_attachments_in_that_order(
    db, cloud, zips, graph, sandbox, settings
):
    graph.attachments = [ATT_PDF1, ATT_REFERENCE]
    graph.references = [{"name": "Plans folder", "sourceUrl": fx.ONEDRIVE_URL},
                        {"name": "again", "sourceUrl": fx.SHAREPOINT_URL}, {"name": "no url"}]
    graph.html = f'<p>Docs: <a href="{fx.DROPBOX_URL}">Dropbox</a> and <a href="{fx.SHAREPOINT_URL}">SharePoint</a></p>'
    cloud.listings[fx.DROPBOX_KEY] = fx.listing("listed", [fx.remote("A1.pdf", 10, "zip:/s/d.zip|0")])
    cloud.listings[fx.ONEDRIVE_KEY] = fx.listing("listed", [fx.remote("B1.pdf", 20, "https://g3electrical-my.sharepoint.com/x")])
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert [r["key"] for r in harvest["data"]["links"]] == [fx.SHAREPOINT_KEY, fx.DROPBOX_KEY, fx.ONEDRIVE_KEY]
    assert cloud.resolved() == [fx.SHAREPOINT_KEY, fx.DROPBOX_KEY, fx.ONEDRIVE_KEY]
    # The parser saw the text body with the HTML; each reference went
    # through link_from_url with the attachment's name as its label.
    assert [c for c in cloud.calls if c[0] == "find"] == [("find", BODY, graph.html)]
    assert [c for c in cloud.calls if c[0] == "link_from_url"] == [
        ("link_from_url", fx.ONEDRIVE_URL, "Plans folder"), ("link_from_url", fx.SHAREPOINT_URL, "again"),
    ]
    assert harvest["data"]["links"][2]["label"] == "Plans folder"
    assert ("references", "msg-primary", MAILBOX) in graph.calls
    assert harvest["data"]["attachments"]["count"] == 2 and harvest["data"]["attachments"]["files"] == 1
    assert harvest["files_accepted"] == 6


def test_reference_listing_trouble_never_fails_the_harvest(db, cloud, zips, graph, sandbox):
    graph.attachments = [ATT_PDF1, ATT_REFERENCE]
    graph.reference_error = fx.http_error(500)
    _seed_email(db)
    h.execute(E1)
    assert _the_harvest(db)["status"] == "complete"
    assert [r["key"] for r in _the_harvest(db)["data"]["links"]] == [fx.SHAREPOINT_KEY]


def test_a_sign_in_wall_and_an_unreachable_folder_are_recorded_with_the_url(
    db, cloud, zips, graph, sandbox
):
    cloud.listings[fx.SHAREPOINT_KEY] = fx.listing("needs_sign_in", [], "The share asks for a sign-in.")
    cloud.listings[fx.DROPBOX_KEY] = fx.listing("unreachable", [], "HTTP 502")
    _seed_email(db, _organic(body_text=f"{fx.SHAREPOINT_URL}\n{fx.DROPBOX_URL}"))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["last_error"] is None
    assert harvest["data"]["links"] == [
        {"key": fx.SHAREPOINT_KEY, "provider": "sharepoint", "kind": "folder", "url": fx.SHAREPOINT_URL,
         "label": "26-080 UMC MLK Warehouse Remodel", "status": "needs_sign_in", "file_count": 0,
         "bytes": 0, "reused": 0, "error": "The share asks for a sign-in."},
        {"key": fx.DROPBOX_KEY, "provider": "dropbox", "kind": "folder", "url": fx.DROPBOX_URL,
         "label": None, "status": "unreachable", "file_count": 0, "bytes": 0, "reused": 0,
         "error": "HTTP 502"},
    ]
    assert harvest["files_accepted"] == 2 and [f["origin"] for f in harvest["files"]] == ["attachment"] * 2
    assert _email_row(db)["status"] == "split"


def test_an_unsupported_link_alone_completes_with_no_files_and_the_sentence(db, cloud, zips, graph, sandbox):
    graph.attachments = []
    _seed_email(db, _organic(attachments_meta=[], body_text=f"Docs: {fx.SHAREFILE_URL}"))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["files"] == [] and harvest["file_count"] == 0
    assert harvest["files_accepted"] == 0 and harvest["sandbox_run_id"] is None
    assert harvest["last_error"] == "The email carries no files to harvest."
    assert harvest["data"]["links"] == [{
        "key": fx.SHAREFILE_KEY, "provider": "sharefile", "kind": "unknown", "url": fx.SHAREFILE_URL,
        "label": None, "status": "unsupported", "file_count": 0, "bytes": 0, "reused": 0,
        "error": "These links must be downloaded by hand.",
    }]
    assert harvest["data"]["documents"] == {"count": 0, "bytes": 0, "kinds": None, "disciplines": None}
    assert cloud.resolved() == [fx.SHAREFILE_KEY] and sandbox.calls == []
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"] and email["last_error"] is None


def test_the_resolve_cap_counts_supported_links_only(db, cloud, zips, graph, sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_link_max_count=1))
    cloud.listings[fx.DROPBOX_KEY] = fx.listing("listed", [fx.remote("A1.pdf", 10, "zip:/s/d.zip|0")])
    _seed_email(db, _organic(body_text=f"{fx.SHAREFILE_URL}\n{fx.SHAREPOINT_URL}\n{fx.DROPBOX_URL}"))
    h.execute(E1)
    rows = _the_harvest(db)["data"]["links"]
    assert [(r["key"], r["status"], r["file_count"]) for r in rows] == [
        (fx.SHAREFILE_KEY, "unsupported", 0), (fx.SHAREPOINT_KEY, "listed", 3),
        (fx.DROPBOX_KEY, "skipped_cap", 0),
    ]
    # The unsupported link is answered by the resolver without the network
    # and spends none of the cap.
    assert cloud.resolved() == [fx.SHAREFILE_KEY, fx.SHAREPOINT_KEY]


def test_a_refusal_from_the_resolver_is_that_links_record(db, cloud, zips, graph, sandbox):
    cloud.listings[fx.SHAREPOINT_KEY] = CloudForbidden("The share redirected off its host.")
    cloud.listings[fx.DROPBOX_KEY] = eh.cloud_folders.CloudError("The share answered with a page.")
    _seed_email(db, _organic(body_text=f"{fx.SHAREPOINT_URL}\n{fx.DROPBOX_URL}"))
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 2
    assert [(r["key"], r["status"], r["error"]) for r in harvest["data"]["links"]] == [
        (fx.SHAREPOINT_KEY, "unreachable", "The share redirected off its host."),
        (fx.DROPBOX_KEY, "unreachable", "The share answered with a page."),
    ]
    assert _email_row(db)["status"] == "split"


@pytest.mark.parametrize("exc", [CloudTransient("The share host did not answer."), RuntimeError("boom")])
def test_resolve_trouble_is_transient_for_the_queue(db, cloud, zips, graph, sandbox, exc):
    cloud.listings[fx.SHAREPOINT_KEY] = exc
    _seed_email(db)
    with pytest.raises(h.RfpHarvestTransient):
        h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and _email_row(db)["status"] == "harvest"
    assert harvest.get("facts_at") is None and sandbox.calls == []


def test_resolve_unavailable_parks_a_pipeline_row(db, cloud, zips, graph, sandbox, settings):
    until = NOW + timedelta(hours=1)
    cloud.listings[fx.SHAREPOINT_KEY] = CloudUnavailable("The share host is rate limiting.", locked_until=until)
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "pending" and harvest["last_error"] == "The share host is rate limiting."
    email = _email_row(db)
    assert email["status"] == "harvest" and email["last_error"] == harvest["last_error"]
    assert h._parse_ts(email["next_attempt_at"]) == until


def test_a_link_download_that_fails_is_recorded_per_file(db, cloud, zips, graph, sandbox, sleeps):
    cloud.errors[fx.SP_FILES[0].locator] = CloudForbidden("The share refused the file (HTTP 403).")
    cloud.errors[fx.SP_FILES[1].locator] = [CloudTransient("x"), CloudTransient("y"), CloudTransient("z")]
    cloud.bytes_for[fx.SP_FILES[2].locator] = b"x" * 10
    _seed_email(db, _organic(attachments_meta=[]))
    graph.attachments = []
    h.execute(E1)
    harvest = _the_harvest(db)
    assert [(f["status"], f["error"]) for f in harvest["files"]] == [
        ("download_failed", "The share refused the file (HTTP 403)."),
        ("download_failed", "z"),
        ("rejected", "The file is not a PDF."),
    ]
    assert harvest["status"] == "complete" and harvest["files_accepted"] == 0
    assert harvest["sandbox_run_id"] is None and ("delete", "run-1") in sandbox.calls
    assert sleeps == [2.0, 4.0]


# ── execute: reuse ───────────────────────────────────────────────────────


def test_a_links_files_accepted_by_an_earlier_harvest_are_reused_not_downloaded(
    db, cloud, zips, graph, sandbox
):
    graph.attachments = []
    db.tables["rfp_harvests"].append(_prior_harvest())
    _seed_email(db, _organic(attachments_meta=[]))
    h.execute(E1)
    harvest = next(r for r in _harvests(db) if r["id"] != "hv-0")
    assert harvest["status"] == "complete"
    assert [(f["status"], f["sandbox_file_id"], f["reused_harvest_id"]) for f in harvest["files"]] == [
        ("reused", "f-old-1", "hv-0"), ("reused", "f-old-2", "hv-0"), ("accepted", "f-2", None),
    ]
    assert harvest["files_accepted"] == 1 and harvest["bytes_downloaded"] == len(PDF)
    assert harvest["file_count"] == 3 and harvest["sandbox_run_id"] == "run-1"
    assert harvest["data"]["links"][0]["reused"] == 2 and harvest["data"]["links"][0]["file_count"] == 3
    assert harvest["data"]["documents"] == {
        "count": 3, "bytes": len(PDF) + fx.SP_FILES[0].size + fx.SP_FILES[1].size,
        "kinds": None, "disciplines": None,
    }
    assert cloud.downloaded() == [fx.SP_FILES[2].locator]
    assert [c[2] for c in sandbox.calls if c[0] == "add"] == ["26 05 00 Common Work Results.pdf"]
    # The earlier harvest is untouched.
    assert next(r for r in _harvests(db) if r["id"] == "hv-0")["files_accepted"] == 2


def test_force_downloads_everything_again(db, cloud, zips, graph, sandbox):
    graph.attachments = []
    db.tables["rfp_harvests"].append(_prior_harvest())
    _seed_email(db, _organic(status="done", attachments_meta=[]))
    h.execute(E1, force=True)
    harvest = next(r for r in _harvests(db) if r["id"] != "hv-0")
    assert [f["status"] for f in harvest["files"]] == ["accepted"] * 3
    assert all(f["reused_harvest_id"] is None for f in harvest["files"])
    assert harvest["data"]["links"][0]["reused"] == 0 and harvest["files_accepted"] == 3
    assert cloud.downloaded() == [r.locator for r in fx.SP_FILES]


def test_a_harvest_whose_only_files_were_reused_creates_no_run(db, cloud, zips, graph, sandbox):
    graph.attachments = []
    cloud.listings[fx.SHAREPOINT_KEY] = fx.listing("listed", list(fx.SP_FILES[:2]))
    db.tables["rfp_harvests"].append(_prior_harvest())
    _seed_email(db, _organic(attachments_meta=[]))
    h.execute(E1)
    harvest = next(r for r in _harvests(db) if r["id"] != "hv-0")
    assert harvest["status"] == "complete" and harvest["last_error"] is None
    assert [f["status"] for f in harvest["files"]] == ["reused", "reused"]
    assert harvest["files_accepted"] == 0 and harvest["bytes_downloaded"] == 0
    assert harvest["sandbox_run_id"] is None and sandbox.calls == []
    assert cloud.downloaded() == []
    assert harvest["data"]["documents"]["count"] == 2
    assert _email_row(db)["status"] == "split" and _email_row(db)["harvest_id"] == harvest["id"]


def test_prior_link_files_reads_the_window_the_status_the_key_and_the_exclusion(db, settings):
    old = _prior_harvest(id="hv-old", external_key="email:e-old",
                         finished_at=(NOW - timedelta(days=15)).isoformat())
    failed = _prior_harvest(id="hv-failed", external_key="email:e-failed", status="failed")
    other_key = _prior_harvest(id="hv-other", external_key="email:e-other",
                               data={"platform": "email", "links": [{"key": fx.DROPBOX_KEY}]})
    newest = _prior_harvest(
        id="hv-new", external_key="email:e-new", finished_at=(NOW - timedelta(hours=1)).isoformat(),
        files=[{**eh.link_file_entry(fx.SP_FILES[0].path, fx.SP_FILES[0].size, provider="sharepoint",
                                     link_key=fx.SHAREPOINT_KEY), "status": "accepted",
                "sandbox_file_id": "f-newest"},
               {**eh.link_file_entry(fx.SP_FILES[2].path, fx.SP_FILES[2].size, provider="sharepoint",
                                     link_key=fx.SHAREPOINT_KEY), "status": "rejected",
                "sandbox_file_id": "f-rejected"},
               {**eh.link_file_entry("orphan.pdf", 1, provider="sharepoint", link_key=fx.SHAREPOINT_KEY),
                "status": "accepted", "sandbox_file_id": None},
               {**eh.link_file_entry("wrong-key.pdf", 1, provider="dropbox", link_key=fx.DROPBOX_KEY),
                "status": "accepted", "sandbox_file_id": "f-wrong"},
               "junk"],
    )
    db.tables["rfp_harvests"].extend([_prior_harvest(), old, failed, other_key, newest])
    found = eh.prior_link_files(db, fx.SHAREPOINT_KEY, settings, exclude_harvest_id=None)
    assert found == {
        (fx.SP_FILES[0].path, fx.SP_FILES[0].size): ("hv-new", "f-newest"),
        (fx.SP_FILES[1].path, fx.SP_FILES[1].size): ("hv-0", "f-old-2"),
    }
    assert eh.prior_link_files(db, fx.SHAREPOINT_KEY, settings, exclude_harvest_id="hv-new") == {
        (fx.SP_FILES[0].path, fx.SP_FILES[0].size): ("hv-0", "f-old-1"),
        (fx.SP_FILES[1].path, fx.SP_FILES[1].size): ("hv-0", "f-old-2"),
    }
    assert eh.prior_link_files(db, fx.DROPBOX_KEY, settings, exclude_harvest_id=None) == {}
    assert eh.prior_link_files(db, "gdrive:none", settings, exclude_harvest_id=None) == {}


# ── execute: the caps ────────────────────────────────────────────────────


def test_file_cap_is_permanent_over_what_still_needs_downloading(
    db, cloud, zips, graph, sandbox, monkeypatch, tmp_path
):
    monkeypatch.setattr(h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_files=2))
    db.tables["rfp_harvests"].append(_prior_harvest())
    _seed_email(db)   # 2 attachments + 3 link files, 2 of them reused: 3 to download
    h.execute(E1)
    harvest = next(r for r in _harvests(db) if r["id"] != "hv-0")
    assert harvest["status"] == "failed"
    assert harvest["last_error"] == "The email's files are more than the harvest accepts (3 of 2)."
    assert harvest["data"]["platform"] == "email" and harvest["file_count"] == 5   # the facts are kept
    assert [f["status"] for f in harvest["files"]] == [None, None, "reused", "reused", None]
    assert sandbox.calls == [] and graph.downloaded() == [] and cloud.downloaded() == []
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == harvest["id"]
    assert email["last_error"] == harvest["last_error"]


def test_byte_cap_is_permanent_and_ignores_reused_and_expanded_entries(
    db, cloud, zips, graph, sandbox, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        h, "get_settings", lambda: _settings(tmp_path, rfp_harvest_max_total_bytes=1024 * 1024)
    )
    graph.attachments = [ATT_ZIP]
    graph.bytes_for["att-9"] = fx.ZIP_BYTES
    zips.listings[fx.ZIP_BYTES] = fx.zip_listing([fx.member(0, "big.pdf", 3 * 1024 * 1024)])
    cloud.links = []
    _seed_email(db)
    h.execute(E1)
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed"
    assert harvest["last_error"] == "The email's files are larger than the harvest accepts (3 MB of 1 MB)."
    assert [f["status"] for f in harvest["files"]] == ["expanded", None]
    # Reused entries do not count either: the same email with the folder only.
    db.tables["rfp_harvests"] = [_prior_harvest()]
    db.tables["rfp_emails"] = [_organic(attachments_meta=[])]
    graph.attachments = []
    cloud.links = [fx.SHAREPOINT_LINK]
    cloud.listings[fx.SHAREPOINT_KEY] = fx.listing("listed", [
        fx.remote(fx.SP_FILES[1].path, fx.SP_FILES[1].size, fx.SP_FILES[1].locator),   # reused (802000)
        fx.remote("small.pdf", 1000, "https://eagle1lv.sharepoint.com/x"),
    ])
    h.execute(E1)
    harvest = next(r for r in _harvests(db) if r["id"] != "hv-0")
    assert harvest["status"] == "complete"
    assert [f["status"] for f in harvest["files"]] == ["reused", "accepted"]


# ── The session adapter ──────────────────────────────────────────────────


def test_session_download_dispatches_on_the_locator_and_maps_the_errors(tmp_path, cloud, graph):
    session = eh.EmailFileSession(tmp_path)
    assert session.provider == "email"
    dest = tmp_path / "a.bin"
    assert session.download(eh.graph_locator(MAILBOX, "m-1", "att-1"), dest, max_bytes=10_000) == len(PDF)
    assert dest.read_bytes() == PDF
    assert graph.calls[-1] == ("download", MAILBOX, "m-1", "att-1", 10_000)
    # zip: and https:// go to cloud_folders.download with the cap.
    assert session.download("zip:/s/z.zip|3", tmp_path / "b.bin", max_bytes=9_999) == len(PDF)
    assert session.download("https://www.dropbox.com/scl/fi/x?dl=1", tmp_path / "c.bin", max_bytes=9_998) == len(PDF)
    assert cloud.calls[-2:] == [("download", "zip:/s/z.zip|3", 9_999),
                                ("download", "https://www.dropbox.com/scl/fi/x?dl=1", 9_998)]
    # Anything else, and a malformed graph locator, is refused without a request.
    for bad in ("http://insecure.example/x", "file:///etc/passwd", "graph:only|two", "graph:a||c", ""):
        with pytest.raises(CloudForbidden) as exc:
            session.download(bad, tmp_path / "d.bin", max_bytes=10)
        assert str(exc.value) == "The harvest does not know how to fetch this file."
    assert len([c for c in graph.calls if c[0] == "download"]) == 1
    # Graph's own failures map to the cloud pair the loop understands.
    cases = [
        (graph_inbox.AttachmentTooLarge("The attachment is larger than the per-file limit."),
         CloudForbidden, "The attachment is larger than the per-file limit."),
        (graph_inbox.AttachmentNotStored("The attachment content is no longer available."),
         CloudForbidden, "The attachment content is no longer available."),
        (fx.http_error(503), CloudTransient, "The mailbox answered HTTP 503."),
        (fx.http_error(429), CloudTransient, "The mailbox answered HTTP 429."),
        (fx.http_error(403), CloudForbidden, "The mailbox refused the attachment (HTTP 403)."),
        (httpx.ConnectError("x"), CloudTransient, "The mailbox did not answer; the harvest will be retried."),
    ]
    for exc_in, exc_type, message in cases:
        graph.errors["att-2"] = exc_in
        with pytest.raises(exc_type) as exc:
            session.download(eh.graph_locator(MAILBOX, "m-1", "att-2"), tmp_path / "e.bin", max_bytes=10)
        assert str(exc.value) == message
        assert isinstance(exc.value, h._FORBIDDEN_ERRORS if exc_type is CloudForbidden else h._TRANSIENT_ERRORS)
    assert "larger" in str(cases[0][2])


# ── The queue marks, the router helpers, error_message ───────────────────


def test_mark_from_queue_and_harvest_for_email_find_an_email_harvest_by_its_key(db, cloud):
    _seed_email(db)
    db.tables["rfp_harvests"].append(_harvest_row(
        id="hv-e1", method="organic", external_key="email:e-1", external_url=None,
        status="running", claim_token="t", data={"platform": "email"},
    ))
    h.mark_from_queue(E1, "failed", "The harvest was interrupted; it will be retried.")
    harvest = _the_harvest(db)
    assert harvest["status"] == "failed" and harvest["claim_token"] is None
    email = _email_row(db)
    assert email["status"] == "split" and email["harvest_id"] == "hv-e1"
    assert h.current_status(E1) == "done"
    # The detail helper falls back to the same key for an unlinked row.
    db.tables["rfp_emails"][0]["harvest_id"] = None
    shown = h.harvest_for_email(db, _email_row(db))
    assert shown["id"] == "hv-e1"
    assert "claim_token" not in h._PUBLIC_HARVEST_COLUMNS and "raw" not in h._PUBLIC_HARVEST_COLUMNS
    assert h.harvest_for_email(db, _organic(id="e-9", attachments_meta=[], body_text="x")) is None


def test_error_message_passes_cloud_sentences_through():
    assert h.error_message(eh.cloud_folders.CloudError("The share host refused."), "email") == "The share host refused."
    assert h.error_message(CloudTransient("The share host did not answer."), "email") == "The share host did not answer."
    assert h.error_message(RuntimeError("secret path"), "email") == h._MSG_INTERRUPTED
