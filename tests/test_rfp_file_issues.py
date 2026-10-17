"""Missing files on an RFP-created project: the service
(app/services/rfp_file_issues, docs/RFP_MISSING_FILES.md sections 3, 4, 7
and 9) against the in-memory fake Supabase from tests/test_rfp_email_ingest.

Pinned:

- `items_from_harvest` over an email harvest (attachment, zip member, folder
  file refused by the sandbox, a link behind a sign-in wall; a listed link,
  images, expanded zips and accepted files never items; `email_ref` only on
  attachment items), a Procore harvest (no URL on platform files), a failed
  harvest (one `harvest` item with the bid sheet URL), the promotion's
  `files_skipped` tokens (a harvest-level skip never repeats as a
  promotion item);
- the identity key, `safe_url`, `normalise_name` and the match rules;
- `sync_project`: idempotent, additive on a changed harvest, never
  reopening a resolved item, `files_issue_total` stamped once;
- `after_file_insert`: the name match, the first-file rule only on a total
  failure and only once, a splitter segment matched by its source name;
- the resolve / reopen / dismiss / undismiss state machine;
- `creation_note` wording; `issue_for_project` ordering and counts;
  `flags_for_records`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import files_needed, rfp_file_drop, rfp_file_issues as rfi, rfp_split
from tests.test_rfp_email_ingest import FakeDB

P1 = "5b000000-0000-4000-8000-000000000001"
P2 = "5b000000-0000-4000-8000-000000000002"
HV = "8e000000-0000-4000-8000-000000000001"
E1 = "3f2c0b00-0000-4000-8000-000000000001"
LINK_A = "sharepoint:gc.sharepoint.com:aaa"
LINK_B = "sharepoint:gc.sharepoint.com:bbb"
URL_A = "https://gc.sharepoint.com/:f:/s/bids/Eaaa"
URL_B = "https://gc.sharepoint.com/:f:/s/bids/Ebbb"
BID_SHEET = "https://app.procore.com/562949953/company/planroom/route_to_bid_sheet/77"

EMAIL = {
    "id": E1, "from_name": "Pat PM", "from_address": "pat@gcbuilders.example",
    "subject": "ITB: Warehouse HVAC", "received_at": "2026-10-08T16:00:00+00:00",
    "primary_mailbox": "bids@g3electrical.com",
}


def _email_harvest(**over):
    row = {
        "id": HV, "method": "organic", "status": "complete", "project_id": P1, "external_url": None,
        "rfp_email_id": E1,
        "last_error": None,
        "data": {"platform": "email", "links": [
            {"key": LINK_A, "provider": "sharepoint", "kind": "folder", "url": URL_A, "label": "Bid Documents",
             "status": "listed", "error": None},
            {"key": LINK_B, "provider": "sharepoint", "kind": "folder", "url": URL_B, "label": None,
             "status": "needs_sign_in", "error": "The share asks for a sign-in."},
        ]},
        "files": [
            {"file_path": "Plans.pdf", "origin": "attachment", "provider": "graph", "link_key": None,
             "status": "download_failed", "error": "The mailbox did not answer."},
            {"file_path": "Bid Set.zip", "origin": "attachment", "provider": "graph", "status": "expanded"},
            {"file_path": "Bid Set.zip/Specs/26 05 19 Wiring.pdf", "origin": "zip", "provider": "graph",
             "zip_of": "Bid Set.zip", "link_key": None, "status": "too_large",
             "error": "Larger than the sandbox accepts."},
            {"file_path": "Electrical/E-1.pdf", "origin": "link", "provider": "sharepoint", "link_key": LINK_A,
             "status": "rejected", "error": "The PDF carries JavaScript."},
            {"file_path": "Electrical/E-2.pdf", "origin": "link", "provider": "sharepoint", "link_key": LINK_A,
             "status": "accepted", "sandbox_file_id": "sf-2"},
            {"file_path": "logo.png", "origin": "attachment", "provider": "graph", "status": "skipped"},
        ],
    }
    row.update(over)
    return row


def _procore_harvest(**over):
    row = {
        "id": HV, "method": "procore", "status": "complete", "project_id": P1, "external_url": BID_SHEET,
        "last_error": None, "data": {"platform": "procore"},
        "files": [
            {"file_path": "Drawings/E-101.pdf", "kind": "drawing", "status": "download_failed",
             "error": "The platform gave no download link for this file."},
            {"file_path": "Specs/Div 26.pdf", "kind": "specification", "status": "skipped_cap",
             "error": "The sandbox run is full."},
            {"file_path": "Drawings/A-101.pdf", "kind": "drawing", "status": "accepted", "sandbox_file_id": "sf-9"},
        ],
    }
    row.update(over)
    return row


def _record(project_id=P1, **over):
    row = {
        "project_id": project_id, "source_kind": "rfp_email", "rfp_email_id": E1, "portal_invitation_id": None,
        "harvest_id": HV, "files_status": "complete", "files_skipped": [], "files_issue_total": False,
        "files_issue_dismissed_at": None, "files_issue_dismissed_by": None,
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def audits(monkeypatch):
    """Every audit row this module writes, captured: the real `audit` would
    reach for the live Supabase client."""
    calls = []
    monkeypatch.setattr(rfi, "audit", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(files_needed, "audit", lambda *a, **k: None)
    return calls


def _db(harvest=None, record=None, **tables):
    base = {
        "rfp_created_projects": [record or _record()],
        "rfp_harvests": [harvest or _email_harvest()],
        "rfp_emails": [dict(EMAIL)],
        "rfp_file_issues": [],
        "project_files": [],
        "profiles": [{"id": "u1", "full_name": "Tom Moore"}, {"id": "u2", "full_name": "Eve Exec"}],
        "bid_split_jobs": [],
        "bid_split_files": [],
    }
    base.update(tables)
    return FakeDB(base)


def _by_name(specs):
    return {s.name: s for s in specs}


# ── 3. The builders ──────────────────────────────────────────────────────────


def test_email_harvest_items():
    specs = rfi.items_from_harvest(_email_harvest(), EMAIL, [])
    assert [(s.kind, s.status) for s in specs] == [
        ("file", "download_failed"), ("file", "too_large"), ("file", "rejected"), ("link", "needs_sign_in"),
    ]
    by = _by_name(specs)
    attachment = by["Plans.pdf"]
    assert attachment.group == "not_downloaded" and attachment.origin == "attachment"
    assert attachment.provider == "graph" and attachment.url is None
    assert attachment.reason == "The mailbox did not answer."
    assert attachment.email_ref == {
        "from": "Pat PM", "from_address": "pat@gcbuilders.example", "subject": "ITB: Warehouse HVAC",
        "received_at": "2026-10-08T16:00:00+00:00", "mailbox": "bids@g3electrical.com",
    }
    member = by["Bid Set.zip/Specs/26 05 19 Wiring.pdf"]
    assert member.origin == "zip" and member.group == "not_downloaded" and member.email_ref is not None
    refused = by["Electrical/E-1.pdf"]
    assert refused.group == "refused" and refused.origin == "link" and refused.url == URL_A
    assert refused.email_ref is None and refused.reason == "The PDF carries JavaScript."
    # The link behind a sign-in wall: no label, so the provider's display name.
    link = by["SharePoint"]
    assert link.kind == "link" and link.url == URL_B and link.origin == "link" and link.provider == "sharepoint"
    assert link.item_key == f"link|{LINK_B}" and link.email_ref is None
    # The listed link, the expanded zip, the accepted file and the image are never items.
    assert not {"Bid Documents", "Bid Set.zip", "Electrical/E-2.pdf", "logo.png"} & set(by)


def test_procore_harvest_items_link_the_bid_sheet_and_carry_the_method():
    specs = rfi.items_from_harvest(_procore_harvest(), None, [])
    assert [(s.name, s.status, s.group) for s in specs] == [
        ("Drawings/E-101.pdf", "download_failed", "not_downloaded"),
        ("Specs/Div 26.pdf", "skipped_cap", "not_downloaded"),
    ]
    # A platform document links the bid sheet it is listed on (review finding 3).
    assert all(s.origin == "platform" and s.provider == "procore" and s.url == BID_SHEET for s in specs)
    assert all(s.email_ref is None for s in specs)
    # An unsafe or missing bid sheet URL stores null.
    for bad in (None, "http://app.procore.com/x", "https://evil.example/x"):
        specs = rfi.items_from_harvest(_procore_harvest(external_url=bad), None, [])
        assert all(s.url is None for s in specs)
    # The promotion's refusal of a platform file never links the bid sheet.
    harvest = _procore_harvest()
    specs = rfi.items_from_harvest(harvest, None, [{"file_path": "A-101.pdf", "reason": "hazard:javascript"}])
    assert next(s for s in specs if s.origin == "promotion").url is None


def test_a_failed_harvest_is_one_harvest_item_with_the_bid_sheet():
    harvest = _procore_harvest(status="failed", last_error="Procore refused the bid.", files=[])
    [spec] = rfi.items_from_harvest(harvest, None, [])
    assert (spec.kind, spec.group, spec.origin, spec.status) == ("harvest", "not_downloaded", "harvest",
                                                               "harvest_failed")
    assert spec.url == BID_SHEET and spec.reason == "Procore refused the bid." and spec.name is None
    assert spec.provider == "procore" and spec.email_ref is None
    # An email-method harvest that failed names the email.
    [spec] = rfi.items_from_harvest(
        _email_harvest(status="failed", last_error="The message is no longer in any watched mailbox.",
                       files=[], data={}), EMAIL, [])
    assert spec.email_ref["subject"] == "ITB: Warehouse HVAC" and spec.url is None
    assert spec.provider == "email"   # the email harvester's methods read as the email itself


def test_a_harvest_with_nothing_and_no_failure_yields_nothing():
    assert rfi.items_from_harvest({"id": HV, "status": "complete", "files": [], "data": {}}, EMAIL, []) == []
    assert rfi.items_from_harvest(None, EMAIL, []) == []


def test_promotion_skips_become_refused_items_and_never_repeat_the_harvest():
    harvest = _email_harvest()
    harvest["files"].append({"file_path": "Specs/Manual.docx", "origin": "link", "provider": "sharepoint",
                             "link_key": LINK_A, "status": "accepted", "sandbox_file_id": "sf-3"})
    skipped = [
        {"file_path": "Manual.docx", "reason": "unscannable:encrypted"},
        {"file_path": "E-2.pdf", "reason": "hazard:javascript"},
        # The promotion repeats every entry that never reached the sandbox;
        # those are the harvest builder's (or never items).
        {"file_path": "Plans.pdf", "reason": "download_failed"},
        {"file_path": "logo.png", "reason": "skipped"},
        {"file_path": "Bid Set.zip", "reason": "expanded"},
        # A name the harvest does not list (the split's source set) still counts.
        {"file_path": "Whole Set.pdf", "reason": "source_set:missing_in_storage"},
        {"file_path": "Ghost.pdf", "reason": "download_failed"},
    ]
    specs = [s for s in rfi.items_from_harvest(harvest, EMAIL, skipped) if s.origin == "promotion"]
    assert [(s.name, s.status) for s in specs] == [
        ("Manual.docx", "skipped:unscannable:encrypted"),
        ("E-2.pdf", "skipped:hazard:javascript"),
        ("Whole Set.pdf", "skipped:source_set:missing_in_storage"),
    ]
    assert all(s.group == "refused" and s.kind == "file" and s.reason is None and s.email_ref is None
               for s in specs)
    # The parent link's URL rides along when the harvest entry came from a share.
    assert specs[0].url == URL_A and specs[1].url == URL_A and specs[2].url is None


def test_the_identity_key_and_dedupe():
    specs = rfi.items_from_harvest(_email_harvest(), EMAIL, [])
    assert _by_name(specs)["Plans.pdf"].item_key == "file|attachment|download_failed|Plans.pdf"
    assert rfi._item_key("harvest", "harvest", "harvest_failed", None) == "harvest|harvest|harvest_failed|"
    long_key = rfi._item_key("file", "zip", "too_large", "x/" * 400)
    assert long_key.startswith("file|zip|too_large|sha256:") and len(long_key) < 400
    # The same file listed twice (a portal listing it under two folders) is one item.
    harvest = _procore_harvest()
    harvest["files"].append(dict(harvest["files"][0]))
    assert len(rfi.items_from_harvest(harvest, None, [])) == 2


@pytest.mark.parametrize("url, ok", [
    (URL_A, True),
    (BID_SHEET, True),
    ("https://bids.pipelinesuite.com/ehPipelineSubs/dspProject/projectID/9", True),
    ("https://gocc.smartbid.co/#/projectlist", True),
    ("https://www.dropbox.com/scl/fo/abc/xyz?rlkey=1", True),
    ("https://acme.sharefile.com/d-s1234", True),
    ("http://gc.sharepoint.com/:f:/s/bids/Eaaa", False),
    ("javascript:alert(1)", False),
    ("https://evil.example/phish", False),
    ("https://user:pw@gc.sharepoint.com/x", False),
    ("https://gc.sharepoint.com/a b", False),
    (None, False),
    ("", False),
])
def test_safe_url(url, ok):
    assert rfi.safe_url(url) == (url if ok else None)


def test_normalise_name_and_the_match_rules():
    assert rfi.normalise_name("  Set.zip/Plans/A-101.PDF ") == "a-101.pdf"
    assert rfi.normalise_name("Folder\\Sub\\E 1   rev.pdf") == "e 1 rev.pdf"
    assert rfi.normalise_name(None) == ""
    assert rfi.names_match("A-101.PDF", "a-101.pdf")
    assert rfi.names_match("Set.zip/Plans/A-101.pdf", "A-101.pdf")
    assert rfi.names_match("E-1.pdf", "E-1.PDF")
    assert not rfi.names_match("E-1.pdf", "E-10.pdf")
    # Stems: a refused .docx re-saved as a PDF still clears its item.
    assert rfi.names_match("Specs/Manual.docx", "Manual.pdf")
    assert not rfi.names_match("", "x.pdf") and not rfi.names_match(None, None)
    # NFC: a decomposed accent matches the composed one.
    assert rfi.names_match("Re\u0301sume\u0301.pdf", "R\u00e9sum\u00e9.pdf")
    # The stem fallback needs a recognised extension on BOTH names (review
    # finding 14): an all-digit suffix is part of the name.
    assert rfi.names_match("E-1.pdf", "E-1.dwg")
    assert not rfi.names_match("E-1.1", "E-1.pdf")
    assert not rfi.names_match("E-1.pdf", "E-1.1")
    assert not rfi.names_match("E-1.1", "E-1.2")
    assert rfi.names_match("E-1.1", "e-1.1")
    assert not rfi.names_match("Specs", "Specs.pdf")
    assert rfi.names_match("Model.7z", "Model.mp4")


def test_total_failure():
    assert rfi.total_failure(None) is True
    assert rfi.total_failure(_email_harvest()) is False
    assert rfi.total_failure(_email_harvest(files=[{"status": "download_failed"}])) is True
    assert rfi.total_failure(_procore_harvest(files=[{"status": "reused", "sandbox_file_id": "x"}])) is False


# ── 9. The creation note ─────────────────────────────────────────────────────


def test_creation_note_wording():
    specs = rfi.items_from_harvest(_email_harvest(), EMAIL, [])
    assert rfi.creation_note(specs) == (
        "4 of the invitation's files did not come in (3 could not be downloaded, 1 was refused); "
        "open the project to see which."
    )
    # A total failure reads by which groups its items fall in (section 9).
    assert rfi.creation_note(specs, total=True) == (
        "None of the invitation's files came in; open the project to see what happened."
    )
    refused = [x for x in specs if x.group == "refused"]
    not_downloaded = [x for x in specs if x.group == "not_downloaded"]
    assert rfi.creation_note(refused, total=True) == (
        "All of the invitation's files were downloaded and refused; open the project to see why."
    )
    assert rfi.creation_note(not_downloaded, total=True) == (
        "None of the invitation's files could be downloaded; open the project to get them by hand."
    )
    assert rfi.creation_note(refused) == (
        "1 of the invitation's files did not come in (1 was refused); open the project to see which."
    )
    assert rfi.creation_note(rfi.items_from_harvest(_procore_harvest(), None, [])) == (
        "2 of the invitation's files did not come in (2 could not be downloaded); open the project to see which."
    )
    assert rfi.creation_note([]) is None and rfi.creation_note([], total=True) is None
    assert rfi.note_for_harvest(_procore_harvest(files=[{"file_path": "x.pdf", "status": "too_large"}]), None) == (
        "None of the invitation's files could be downloaded; open the project to get them by hand."
    )


# ── 4. The sync ──────────────────────────────────────────────────────────────


def test_sync_is_idempotent_additive_and_never_reopens():
    db = _db()
    assert rfi.sync_project(db, P1) == 4
    rows = db.tables["rfp_file_issues"]
    assert {r["harvest_id"] for r in rows} == {HV} and all(r["project_id"] == P1 for r in rows)
    attachment = next(r for r in rows if r["name"] == "Plans.pdf")
    assert attachment["email_ref"]["mailbox"] == "bids@g3electrical.com"
    assert rfi.sync_project(db, P1) == 0 and len(rows) == 4
    # A resolved item stays resolved through a re-sync.
    rfi.resolve(db, P1, attachment["id"], "u1")
    # "Harvest again": one new failure joins; nothing duplicates.
    db.tables["rfp_harvests"][0]["files"].append(
        {"file_path": "Addendum 1.pdf", "origin": "attachment", "provider": "graph", "status": "download_failed"})
    assert rfi.sync_project(db, P1) == 1 and len(rows) == 5
    assert next(r for r in rows if r["name"] == "Plans.pdf")["resolved_how"] == "manual"
    # A partial harvest never marks the total failure.
    assert db.tables["rfp_created_projects"][0]["files_issue_total"] is False


def test_email_ref_names_the_harvests_own_message():
    """The recent-project link path: invitation B's harvest promoted into a
    project whose record names email A. B's failed attachments belong to B's
    message (review finding 6)."""
    other = {**EMAIL, "id": "3f2c0b00-0000-4000-8000-0000000000bb", "subject": "ITB: Warehouse HVAC (Addendum 1)",
             "primary_mailbox": "tmoore@g3electrical.com"}
    harvest = _email_harvest(rfp_email_id=other["id"])
    db = _db(harvest=harvest, rfp_emails=[dict(EMAIL), other])
    # An email handed in that is not the harvest's own is ignored.
    assert rfi.sync_project(db, P1, email=dict(EMAIL)) == 4
    attachment = _item(db, "Plans.pdf")
    assert attachment["email_ref"]["subject"] == "ITB: Warehouse HVAC (Addendum 1)"
    assert attachment["email_ref"]["mailbox"] == "tmoore@g3electrical.com"
    # A platform harvest never reads an email.
    db = _db(harvest=_procore_harvest(rfp_email_id=E1))
    rfi.sync_project(db, P1)
    assert all(r.get("email_ref") is None for r in db.tables["rfp_file_issues"])


def test_merge_moves_open_items_and_back():
    db = _synced()
    # Items stay with the source unless the target is an RFP-created project.
    assert rfi.move_open_items(db, P1, P2) == []
    assert all(r["project_id"] == P1 for r in db.tables["rfp_file_issues"])
    db.tables["rfp_created_projects"].append(_record(P2))
    a = _item(db, "Plans.pdf")
    rfi.resolve(db, P1, _item(db, "Electrical/E-1.pdf")["id"], "u1")
    # The target already holds the zip member's key: that row stays behind.
    member = _item(db, "Bid Set.zip/Specs/26 05 19 Wiring.pdf")
    db.tables["rfp_file_issues"].append({**member, "id": "t-1", "project_id": P2})
    moved = rfi.move_open_items(db, P1, P2)
    assert a["id"] in moved and member["id"] not in moved and len(moved) == 2
    assert a["project_id"] == P2 and member["project_id"] == P1
    assert _item(db, "Electrical/E-1.pdf")["project_id"] == P1      # resolved rows stay
    rfi.move_items_back(db, moved, P2, P1)
    assert a["project_id"] == P1
    assert rfi.move_open_items(db, P2, P1) == []   # the target's own row collides with the source's key


def test_the_drop_summary_never_cuts_the_newest_job(wire, monkeypatch):
    """Review N3: each job's files are read on their own, newest job first
    and paged, so a row cap can never cut the newest job's files or fall
    back to an older job."""
    monkeypatch.setattr(rfi, "_PAGE_ROWS", 2)
    db = _synced()
    db.tables["bid_split_jobs"] = [
        {"id": "j-old", "project_id": P1, "source": "project", "status": "done", "created_at": "2026-10-09T10:00:00+00:00"},
        {"id": "j-new", "project_id": P1, "source": "project", "status": "processing", "created_at": "2026-10-09T11:00:00+00:00"},
    ]
    db.tables["bid_split_files"] = (
        [{"id": f"o{n}", "job_id": "j-old", "filename": f"Old{n}.pdf", "status": "done", "error": None,
          "created_at": f"0{n}"} for n in range(3)]
        + [{"id": f"n{n}", "job_id": "j-new", "filename": f"New{n}.pdf",
            "status": "failed" if n == 4 else "done", "error": "Unreadable." if n == 4 else None,
            "created_at": f"1{n}"} for n in range(5)]
    )
    drop = rfi.issue_for_project(db, P1)["drop"]
    assert drop["job_id"] == "j-new" and drop["files_total"] == 5 and drop["files_done"] == 4
    assert drop["failed"] == [{"filename": "New4.pdf", "error": "Unreadable."}]


def test_sync_reads_the_promotion_skips_and_stamps_the_total_failure_once():
    harvest = _procore_harvest(files=[{"file_path": "Drawings/E-101.pdf", "status": "download_failed"}])
    db = _db(harvest=harvest)
    assert rfi.sync_project(db, P1) == 1
    assert db.tables["rfp_created_projects"][0]["files_issue_total"] is True
    # A later look that finds files never flips it back.
    db.tables["rfp_harvests"][0]["files"].append({"file_path": "x.pdf", "status": "accepted", "sandbox_file_id": "s"})
    db.tables["rfp_harvests"][0]["files"].append({"file_path": "y.pdf", "status": "too_large"})
    assert rfi.sync_project(db, P1) == 1
    assert db.tables["rfp_created_projects"][0]["files_issue_total"] is True
    # The promotion's refusals arrive through the record.
    db.tables["rfp_created_projects"][0]["files_skipped"] = [{"file_path": "x.pdf", "reason": "too_large"}]
    assert rfi.sync_project(db, P1) == 1
    assert any(r["status"] == "skipped:too_large" for r in db.tables["rfp_file_issues"])


def test_sync_is_best_effort_and_ignores_projects_the_slice_did_not_make():
    db = _db()
    assert rfi.sync_project(db, P2) == 0 and db.tables["rfp_file_issues"] == []

    class Broken:
        def table(self, name):
            raise RuntimeError("db down")

    assert rfi.sync_project(Broken(), P1) == 0


def test_sync_survives_a_concurrent_insert(monkeypatch):
    db = _db()
    db.unique = {**FakeDB.unique, "rfp_file_issues": [("project_id", "item_key")]}
    specs = rfi.items_from_harvest(_email_harvest(), EMAIL, [])
    # Another worker landed one of the four first.
    db.tables["rfp_file_issues"].append(specs[0].row(P1, HV))
    original = rfi._rows

    seen = {"n": 0}

    def stale_keys(result):
        rows = original(result)
        if rows and set(rows[0]) == {"item_key"} and seen["n"] == 0:
            seen["n"] += 1
            return []   # the sync read the keys before the other insert landed
        return rows

    monkeypatch.setattr(rfi, "_rows", stale_keys)
    assert rfi.sync_project(db, P1) == 3
    assert len(db.tables["rfp_file_issues"]) == 4


# ── Resolution ───────────────────────────────────────────────────────────────


def _synced(harvest=None, record=None, **tables):
    db = _db(harvest=harvest, record=record, **tables)
    rfi.sync_project(db, P1)
    return db


def _item(db, name):
    return next(r for r in db.tables["rfp_file_issues"] if r["name"] == name)


def test_after_file_insert_resolves_by_name(audits):
    db = _synced()
    resolved = rfi.after_file_insert(db, P1, category="other", filenames=["e-1.PDF"], actor_id="u1")
    assert resolved == [_item(db, "Electrical/E-1.pdf")["id"]]
    row = _item(db, "Electrical/E-1.pdf")
    assert row["resolved_how"] == "name_match" and row["resolved_by"] == "u1" and row["resolved_at"]
    assert audits[-1][1] == "rfp_files.resolve" and audits[-1][4]["how"] == "name_match"
    # Links and whole-harvest items never resolve by name; nothing else moved.
    assert rfi.after_file_insert(db, P1, category="other", filenames=["SharePoint"], actor_id="u1") == []
    assert sum(1 for r in db.tables["rfp_file_issues"] if r.get("resolved_at")) == 1
    # A project without items costs one read and answers [].
    assert rfi.after_file_insert(db, P2, category="drawing", filenames=["x.pdf"], actor_id=None) == []
    assert rfi.after_file_insert(db, None, category="drawing", filenames=["x.pdf"], actor_id=None) == []


def _total_harvest():
    """A total failure: nothing accepted; a link behind a sign-in wall, a
    whole-harvest failure and two named files."""
    harvest = _email_harvest(status="failed", last_error="The message is no longer in any watched mailbox.")
    harvest["files"] = [f for f in harvest["files"] if f["status"] in ("download_failed", "too_large")]
    return harvest


def test_the_first_file_rule_clears_only_links_and_harvests_by_a_person_once(audits):
    db = _synced(harvest=_total_harvest())
    assert db.tables["rfp_created_projects"][0]["files_issue_total"] is True
    kinds = sorted(r["kind"] for r in db.tables["rfp_file_issues"])
    assert kinds == ["file", "file", "harvest", "link"]
    # Before the insert: no drawing yet, so this is the first one.
    assert rfi.is_first_file(db, P1, "drawing") is True
    assert rfi.is_first_file(db, P1, "other") is False
    db.tables["project_files"].append({"id": "pf-1", "project_id": P1, "category": "drawing", "filename": "Set.pdf"})
    # The promotion's (or the split's) own insert never runs the rule.
    assert rfi.after_file_insert(db, P1, category="drawing", filenames=["Set.pdf"], actor_id=None,
                                 first_file=True, by_person=False) == []
    # A person's first drawing clears the unknown-content items only; the
    # named files stay on the list (review finding 2).
    resolved = rfi.after_file_insert(db, P1, category="drawing", filenames=["Set.pdf"], actor_id="u1",
                                     first_file=True, by_person=True)
    assert len(resolved) == 2
    hows = {r["kind"]: r.get("resolved_how") for r in db.tables["rfp_file_issues"]}
    assert hows == {"link": "auto_first_file", "harvest": "auto_first_file", "file": None}
    # Only once: a person reopens the link; the next drawing is not the first.
    link = next(r for r in db.tables["rfp_file_issues"] if r["kind"] == "link")
    rfi.reopen(db, P1, link["id"], "u1")
    assert rfi.is_first_file(db, P1, "drawing") is False
    assert rfi.after_file_insert(db, P1, category="drawing", filenames=["Set2.pdf"], actor_id="u1",
                                 first_file=rfi.is_first_file(db, P1, "drawing"), by_person=True) == []
    assert link.get("resolved_at") is None
    # A category that is not a drawing or specification never triggers it.
    assert rfi.after_file_insert(db, P1, category="other", filenames=["z.pdf"], actor_id="u1",
                                 first_file=True, by_person=True) == []


def test_the_first_file_rule_reads_the_total_failure_live():
    db = _synced(harvest=_total_harvest())
    # A re-harvest brought files in and the promotion filed them: no longer total.
    db.tables["rfp_created_projects"][0]["files_promoted"] = 2
    assert rfi.total_failure_live(db.tables["rfp_created_projects"][0]) is False
    assert rfi.after_file_insert(db, P1, category="drawing", filenames=["x.pdf"], actor_id="u1",
                                 first_file=True, by_person=True) == []


def test_the_first_file_rule_never_fires_on_a_partial_failure():
    db = _synced()
    assert db.tables["rfp_created_projects"][0]["files_issue_total"] is False
    assert rfi.after_file_insert(db, P1, category="specification", filenames=["nothing.pdf"], actor_id=None,
                                 first_file=True, by_person=True) == []
    assert not any(r.get("resolved_at") for r in db.tables["rfp_file_issues"])


def test_a_split_segment_resolves_by_its_source_name(monkeypatch):
    db = _synced()
    monkeypatch.setattr(rfp_split.storage, "delete_file", lambda *a: None)
    monkeypatch.setattr(files_needed, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfi, "audit", lambda *a, **k: None)
    db.tables["rfp_file_issues"][0]["name"] = "Bid Set.pdf"
    row = {"project_id": P1, "category": "electrical_drawing", "storage_path": "k",
           "filename": "Electrical Drawings.pdf"}
    rfp_split._insert_row(db, row, "k", source_name="Bid Set.pdf")
    assert db.tables["rfp_file_issues"][0]["resolved_how"] == "name_match"


def test_resolve_reopen_state_machine(audits):
    db = _synced()
    item = _item(db, "Plans.pdf")
    other = _item(db, "Electrical/E-1.pdf")
    assert rfi.resolve(db, P2, item["id"], "u1") is None         # not this project's
    out = rfi.resolve(db, P1, item["id"], "u1")
    assert out["resolved_how"] == "manual" and out["resolved_by"] == "u1"
    assert rfi.resolve(db, P1, item["id"], "u1") is None          # already resolved
    assert rfi.reopen(db, P1, other["id"], "u1") is None          # not resolved
    out = rfi.reopen(db, P1, item["id"], "u2")
    assert out["resolved_at"] is None and out["resolved_by"] is None and out["resolved_how"] is None
    assert [a[1] for a in audits] == ["rfp_files.resolve", "rfp_files.reopen"]
    assert all(a[2] == "project" and a[3] == P1 for a in audits)


def test_dismiss_all_and_undo(audits):
    db = _synced()
    manual = _item(db, "Plans.pdf")
    rfi.resolve(db, P1, manual["id"], "u1")
    rec = rfi.dismiss_all(db, P1, "u2")
    assert rec["files_issue_dismissed_by"] == "u2" and rec["files_issue_dismissed_at"]
    hows = {r["name"]: r["resolved_how"] for r in db.tables["rfp_file_issues"]}
    assert hows["Plans.pdf"] == "manual" and list(hows.values()).count("dismiss_all") == 3
    # Nothing open now: the only 409.
    with pytest.raises(rfi.IssueConflict) as exc:
        rfi.dismiss_all(db, P1, "u2")
    assert str(exc.value) == "Nothing is open to dismiss."
    # A later sync adds an item: the issue is open again (review finding 12)
    # and a second dismissal re-stamps and resolves only the new one.
    db.tables["rfp_harvests"][0]["files"].append(
        {"file_path": "Addendum 2.pdf", "origin": "attachment", "provider": "graph", "status": "download_failed"})
    assert rfi.sync_project(db, P1) == 1
    record = db.tables["rfp_created_projects"][0]
    assert rfi.issue_state(record, 1) == "open"
    first_stamp = record["files_issue_dismissed_at"]
    rec = rfi.dismiss_all(db, P1, "u1")
    assert rec["files_issue_dismissed_by"] == "u1" and rec["files_issue_dismissed_at"] >= first_stamp
    hows = {r["name"]: r["resolved_how"] for r in db.tables["rfp_file_issues"]}
    assert list(hows.values()).count("dismiss_all") == 4
    # Undo reopens exactly the dismiss_all rows; the manual resolution stays.
    rfi.undismiss(db, P1, "u1")
    assert db.tables["rfp_created_projects"][0]["files_issue_dismissed_at"] is None
    hows = {r["name"]: r["resolved_how"] for r in db.tables["rfp_file_issues"]}
    assert hows["Plans.pdf"] == "manual" and list(hows.values()).count(None) == 4
    with pytest.raises(rfi.IssueConflict) as exc:
        rfi.undismiss(db, P1, "u1")
    assert str(exc.value) == "The missing files are not dismissed."
    assert [a[1] for a in audits][-3:] == ["rfp_files.dismiss", "rfp_files.dismiss", "rfp_files.undismiss"]
    assert audits[-1][4] == {"items": 4}
    assert rfi.dismiss_all(db, P2, "u1") is None and rfi.undismiss(db, P2, "u1") is None


def test_unresolve_name_matches_only_moves_name_matches():
    db = _synced()
    a, b = _item(db, "Plans.pdf"), _item(db, "Electrical/E-1.pdf")
    rfi.resolve(db, P1, a["id"], "u1")
    rfi.resolve_by_names(db, P1, ["E-1.pdf"], "u1")
    rfi.unresolve_name_matches(db, P1, [a["id"], b["id"]])
    assert a["resolved_how"] == "manual" and b.get("resolved_at") is None


# ── 7. The wire shapes ───────────────────────────────────────────────────────


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setattr(rfp_file_drop, "splitter_state", lambda s=None: (False, "The Bid File Splitter is off."))
    import app.routers.files as files_router

    monkeypatch.setattr(files_router, "handoff_locked", lambda pid: False)
    monkeypatch.setattr(rfi, "get_settings", lambda: SimpleNamespace(bid_split_max_pages_per_file=600))


def test_issue_for_project_order_counts_and_shape(wire, audits):
    db = _synced()
    assert rfi.issue_for_project(db, P2) is None
    e1 = _item(db, "Electrical/E-1.pdf")
    rfi.resolve(db, P1, _item(db, "Plans.pdf")["id"], "u1")
    db.tables["bid_split_jobs"] = [
        {"id": "j-old", "project_id": P1, "source": "project", "status": "done", "created_at": "2026-10-09T10:00:00+00:00"},
        {"id": "j-new", "project_id": P1, "source": "project", "status": "processing", "created_at": "2026-10-09T11:00:00+00:00"},
        {"id": "j-rfp", "project_id": P1, "source": "rfp", "status": "done", "created_at": "2026-10-09T12:00:00+00:00"},
    ]
    db.tables["bid_split_files"] = [
        {"id": "f1", "job_id": "j-new", "filename": "Set.pdf", "status": "done", "error": None, "created_at": "1"},
        {"id": "f2", "job_id": "j-new", "filename": "Bad.pdf", "status": "failed", "error": "Unreadable.", "created_at": "2"},
        {"id": "f3", "job_id": "j-new", "filename": "Big.pdf", "status": "running", "error": None, "created_at": "3"},
    ]
    # A newer drop job with no file (its first file failed to stage) is never the drop shown.
    db.tables["bid_split_jobs"].append({"id": "j-empty", "project_id": P1, "source": "project",
                                        "status": "processing", "created_at": "2026-10-09T13:00:00+00:00"})
    issue = rfi.issue_for_project(db, P1)
    assert issue["state"] == "open" and issue["total_failure"] is False
    assert issue["counts"] == {"open": 3, "resolved": 1, "not_downloaded": 2, "refused": 1}
    # Open first (not downloaded, then refused; by name), then the resolved ones.
    assert [(i["group"], i["name"]) for i in issue["items"]] == [
        ("not_downloaded", "Bid Set.zip/Specs/26 05 19 Wiring.pdf"),
        ("not_downloaded", "SharePoint"),
        ("refused", "Electrical/E-1.pdf"),
        ("not_downloaded", "Plans.pdf"),
    ]
    resolved = issue["items"][-1]["resolved"]
    assert resolved["how"] == "manual" and resolved["by"] == {"id": "u1", "name": "Tom Moore"}
    first = issue["items"][2]
    assert first["id"] == e1["id"] and first["url"] == URL_A and first["email"] is None
    assert set(first) == {"id", "kind", "group", "origin", "provider", "status", "name", "url", "reason",
                          "email", "resolved"}
    attachment = issue["items"][-1]
    assert attachment["email"]["from"] == "Pat PM" and attachment["email"]["mailbox"] == "bids@g3electrical.com"
    assert issue["drop"] == {
        "job_id": "j-new", "status": "processing", "files_total": 3, "files_done": 1, "files_failed": 1,
        "failed": [{"filename": "Bad.pdf", "error": "Unreadable."}], "created_at": "2026-10-09T11:00:00+00:00",
    }
    assert issue["splitter_available"] is False and issue["splitter_unavailable_reason"] == "The Bid File Splitter is off."
    assert issue["package_sent"] is False and issue["page_cap"] == 600 and issue["dismissed"] is None
    # Dismissed: the state says so, with who and when.
    rfi.dismiss_all(db, P1, "u2")
    issue = rfi.issue_for_project(db, P1)
    assert issue["state"] == "dismissed" and issue["dismissed"]["by"] == {"id": "u2", "name": "Eve Exec"}
    assert issue["counts"]["open"] == 0
    # An item reopened while dismissed: the issue reads open again.
    rfi.reopen(db, P1, e1["id"], "u1")
    assert rfi.issue_for_project(db, P1)["state"] == "open"
    rfi.undismiss(db, P1, "u2")
    for row in db.tables["rfp_file_issues"]:
        rfi.resolve(db, P1, row["id"], "u1")
    assert rfi.issue_for_project(db, P1)["state"] == "resolved"
    # total_failure: the stamp AND nothing promoted, read live.
    db.tables["rfp_created_projects"][0].update(files_issue_total=True, files_promoted=0)
    assert rfi.issue_for_project(db, P1)["total_failure"] is True
    db.tables["rfp_created_projects"][0]["files_promoted"] = 3
    assert rfi.issue_for_project(db, P1)["total_failure"] is False
    # Only empty drop jobs: no drop.
    db.tables["bid_split_files"] = []
    assert rfi.issue_for_project(db, P1)["drop"] is None


def test_a_stored_url_that_is_not_safe_never_reaches_the_page(wire):
    db = _synced()
    db.tables["rfp_file_issues"][0]["url"] = "https://evil.example/x"
    issue = rfi.issue_for_project(db, P1)
    assert all(i["url"] in (None, URL_A, URL_B) for i in issue["items"])


def test_flags_for_records_is_one_read_grouped_by_project():
    db = _synced()
    rfi.resolve(db, P1, _item(db, "Plans.pdf")["id"], "u1")
    records = [_record(P1), _record(P2)]
    flags = rfi.flags_for_records(db, records)
    assert flags == {P1: {"state": "open", "open": 3, "total": 4}, P2: None}
    # Open items win over a dismissal stamp (review finding 12).
    stamped = _record(P1, files_issue_dismissed_at="2026-10-09T00:00:00+00:00")
    assert rfi.flags_for_records(db, [stamped])[P1]["state"] == "open"
    for row in db.tables["rfp_file_issues"]:
        row["resolved_at"] = row.get("resolved_at") or "2026-10-09T00:00:00+00:00"
    assert rfi.flags_for_records(db, [stamped])[P1] == {"state": "dismissed", "open": 0, "total": 4}
    assert rfi.flags_for_records(db, [_record(P1)])[P1]["state"] == "resolved"


def test_flags_and_items_page_past_the_row_cap(monkeypatch):
    """PostgREST answers at most max-rows per read: every unbounded item
    read pages (review finding 11)."""
    monkeypatch.setattr(rfi, "_PAGE_ROWS", 3)
    db = _db()
    db.tables["rfp_file_issues"] = [
        {"id": f"i-{n:02d}", "project_id": P1 if n < 7 else P2, "item_key": f"k{n}", "kind": "file",
         "group": "not_downloaded", "name": f"F{n}.pdf", "status": "download_failed",
         "resolved_at": "2026-10-09T00:00:00+00:00" if n % 2 else None}
        for n in range(10)
    ]
    flags = rfi.flags_for_records(db, [_record(P1), _record(P2)])
    assert flags[P1] == {"state": "open", "open": 4, "total": 7}
    assert flags[P2] == {"state": "open", "open": 1, "total": 3}
    assert len(rfi._items(db, P1)) == 7 and len(rfi._open_items(db, P1)) == 4
