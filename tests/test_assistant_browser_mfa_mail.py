"""Browser v2 8.3 (WI-14): the server reads emailed one-time codes from the
owner's own mailbox; the model never does. Fakes only: no Supabase, no Graph."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import assistant_browser_mfa_mail as mfa
from app.services import assistant_browser_records as records
from app.services import assistant_browser_vault as vault

OWNER = str(uuid4())
JOB = str(uuid4())
HELP = str(uuid4())
EMAIL = "owner@g3electrical.com"
BODY_SECRET = "PRIVATE-BODY-TEXT-do-not-log"
TENANT_OK = ("spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=procore.com; dkim=pass (signature was verified) "
             "header.d=procore.com;dmarc=pass action=none header.from=procore.com;compauth=pass reason=100")
SQL_0192 = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
            / "0192_assistant_browser_v2.sql").read_text(encoding="utf-8")


def _seeded_regex(site_key: str) -> str:
    """The code_email_regex 0192 seeds for one curated site."""
    for match in re.finditer(r"set code_email_regex =\s*'([^']+)'\s*where site_key (?:=|in) \(?([^\n]+)", SQL_0192):
        if f"'{site_key}'" in match.group(2):
            return match.group(1)
    raise AssertionError(site_key)


PROCORE = {"site_key": "procore", "kind": "curated", "code_email_senders": ["procore.com"],
           "code_email_regex": _seeded_regex("procore"), "login_origins": ["https://login.procore.com"]}
AD_HOC = {"site_key": "domain:procore.com", "kind": "ad_hoc", "code_email_senders": ["procore.com"],
          "code_email_regex": None, "login_origins": ["https://login.procore.com"]}


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.since = self.now - timedelta(minutes=2)
        self.messages: dict[str, dict] = {}
        self.order: list[str] = []
        self.calls: list[tuple] = []
        self.delivered: list[dict] = []
        self.rpc_results: list[dict] = []
        self.profile_email = EMAIL
        self.shared = {"bids@g3electrical.com", "office@g3electrical.com"}
        self.help = {"id": HELP, "status": "open", "reason_code": "mfa", "is_login_scope": True,
                     "expires_at": (self.now + timedelta(minutes=8)).isoformat(), "created_at": _iso(self.now)}
        self.clock = 0.0

    def add(self, *, sender="no-reply@procore.com", subject="Your Procore verification code",
            body="Your verification code is 482913. It expires in 10 minutes.", received=None,
            auth=(TENANT_OK,), from_header=None, imid=None, to=EMAIL, written=None) -> str:
        mid = "AAMk" + uuid4().hex
        stamp = received or (self.now - timedelta(seconds=30))
        headers = [{"name": "Received", "value": "from mx.example"}]
        headers += [{"name": "Authentication-Results", "value": v} for v in auth]
        headers.append({"name": "From", "value": from_header or f"Site <{sender}>"})
        headers.append({"name": "To", "value": to})
        headers.append({"name": "Date", "value": format_datetime(written or stamp)})
        self.messages[mid] = {
            "id": mid, "internetMessageId": imid or f"<{uuid4().hex}@mail.example>",
            "from": {"emailAddress": {"address": sender, "name": "Site"}},
            "toRecipients": [{"emailAddress": {"address": to}}],
            "receivedDateTime": _iso(stamp), "subject": subject,
            "body": {"contentType": "text", "content": body + "\n" + BODY_SECRET},
            "internetMessageHeaders": headers}
        self.order.insert(0, mid)
        return mid


@pytest.fixture
def w(monkeypatch):
    world = World()
    monkeypatch.setattr(vault, "get_settings", lambda: SimpleNamespace(
        user_cred_kek=base64.b64encode(b"k" * 32).decode(), user_cred_kek_id="u1", user_cred_old_keys=""))
    monkeypatch.setattr(mfa, "get_settings", lambda: SimpleNamespace(
        shared_mailbox_set=world.shared, mailbox_exclusion_set=set(),
        rfp_email_ingestion_shared_mailboxes="bids@g3electrical.com"))
    monkeypatch.setattr(mfa.assistant_ms, "sender_mailbox", lambda uid: world.profile_email)
    monkeypatch.setattr(mfa.assistant_ms, "configured", lambda: True)

    def for_mailbox(uid, mailbox):
        world.calls.append(("authorize", mailbox))
        if mailbox != world.profile_email:
            raise mfa.assistant_ms.OutlookError()
        return mailbox, "token"
    monkeypatch.setattr(mfa.assistant_ms, "for_mailbox", for_mailbox)

    class Graph:
        def __init__(self, token, **kwargs):
            assert kwargs.get("reader") is True
        def read_json(self, path, params=None, prefer=None):
            world.calls.append(("get", path, params))
            if path.endswith("/messages"):
                return {"value": [{"id": m, "from": world.messages[m]["from"],
                                   "receivedDateTime": world.messages[m]["receivedDateTime"]} for m in world.order]}
            mid = path.rsplit("/", 1)[1]
            return world.messages[mid]
    monkeypatch.setattr(mfa, "GraphTransport", Graph)
    monkeypatch.setattr(mfa.repo, "browser_help_state", lambda uid, jid: dict(world.help))
    monkeypatch.setattr(mfa.repo, "browser_platform_config_v2", lambda uid: ({}, {"user_mfa_mail_poll_seconds": 30}))

    def from_mail(uid, jid, hid, sha, record):
        world.calls.append(("rpc", uid, jid, hid, sha))
        world.delivered.append({**record, "user_id": uid, "job_id": jid, "sha": sha})
        return world.rpc_results.pop(0) if world.rpc_results else {"ok": True, "record_id": record["id"],
                                                                  "code_delivered": True}
    monkeypatch.setattr(mfa.repo, "browser_help_secret_from_mail", from_mail)
    return world


def poll(world, site=PROCORE, since=None, credential=None):
    def sleep(seconds):
        world.calls.append(("sleep", seconds))
        world.clock += seconds
    return mfa.poll_for_code(OWNER, JOB, HELP, site=dict(site), since=since or world.since, credential=credential,
                             sleep=sleep, monotonic=lambda: world.clock)


def delivered_code(world) -> str:
    row = world.delivered[-1]
    stored = {**row, "help_id": HELP}
    return records.unpack_record(OWNER, JOB, stored, allow_secret=True)["secret"]


def message_reads(world) -> list[str]:
    return [c[1] for c in world.calls if c[0] == "get" and not c[1].endswith("/messages")]


# ── The happy path and the sealed record ─────────────────────────────────────


def test_a_trusted_code_email_delivers_exactly_that_code_to_the_help_path(w):
    mid = w.add()
    assert poll(w) is True
    assert delivered_code(w) == "482913"
    row = w.delivered[-1]
    # The envelope binds its own client_msg_id, which 0192 now stores as is.
    assert row["kind"] == "help_secret" and row["client_msg_id"] == row["id"] and row["help_id"] == HELP
    assert row["secret_hash"] and row["secret_key_id"] == "u1"
    imid = w.messages[mid]["internetMessageId"].strip("<>").lower()
    assert row["sha"] == hashlib.sha256(("imid:" + imid).encode()).hexdigest()
    assert "482913" not in str({k: v for k, v in row.items() if k != "envelope"})


def test_only_the_owners_primary_mailbox_is_read(w):
    w.add()
    poll(w)
    assert {c[1] for c in w.calls if c[0] == "authorize"} == {EMAIL}
    assert all(c[1].startswith("/v1.0/users/owner@g3electrical.com/messages") for c in w.calls if c[0] == "get")


def test_the_listing_asks_only_for_messages_after_the_attempt_began(w):
    w.add()
    poll(w)
    listing = next(c for c in w.calls if c[0] == "get" and c[1].endswith("/messages"))
    assert listing[2]["$filter"] == "receivedDateTime ge " + _iso(w.since)
    assert "body" not in listing[2]["$select"]


# ── Sender trust ─────────────────────────────────────────────────────────────


def test_dkim_fail_is_not_trusted(w):
    w.add(auth=(TENANT_OK.replace("dkim=pass", "dkim=fail"),))
    assert poll(w) is False and w.delivered == []


def test_dmarc_or_compauth_fail_is_not_trusted_even_with_dkim_pass():
    for bad in ("dmarc=pass", "compauth=pass"):
        header = TENANT_OK.replace(bad, bad.replace("pass", "fail"))
        assert not mfa.sender_trusted([("Authentication-Results", header), ("From", "no-reply@procore.com")],
                                      allowed_domains=["procore.com"], tenant_authserv_id=mfa.TENANT_AUTHSERV_ID)


def test_a_forged_authentication_results_header_below_the_tenants_is_ignored(w):
    forged = ("spf=pass smtp.mailfrom=procore.com; dkim=pass header.d=procore.com; dmarc=pass header.from=procore.com;"
              "compauth=pass reason=100")
    w.add(auth=(TENANT_OK.replace("dkim=pass", "dkim=none"), forged))
    assert poll(w) is False and w.delivered == []


def test_a_top_header_that_is_not_the_tenants_fails_closed(w):
    w.add(auth=("mx.attacker.example; dkim=pass header.d=procore.com; dmarc=pass", TENANT_OK))
    assert poll(w) is False and w.delivered == []


def test_the_tenant_header_is_matched_by_its_authserv_id_or_compauth():
    rfc = "bn8nam11ft.mail.protection.outlook.com; dkim=pass header.d=procore.com; dmarc=pass header.from=procore.com"
    other = "mx.example.net; dkim=pass header.d=procore.com; dmarc=pass header.from=procore.com"
    args = dict(allowed_domains=["procore.com"], tenant_authserv_id=mfa.TENANT_AUTHSERV_ID,
                from_address="no-reply@procore.com")
    assert mfa.sender_trusted([("Authentication-Results", rfc)], **args)
    assert mfa.sender_trusted([("Authentication-Results", TENANT_OK)], **args)
    assert not mfa.sender_trusted([("Authentication-Results", other)], **args)
    assert not mfa.sender_trusted([], **args)


def test_comment_text_cannot_supply_a_dkim_domain():
    header = ("spf=fail smtp.mailfrom=x.example; dkim=pass (header.d=procore.com) header.d=attacker.example;"
              "dmarc=none;compauth=none reason=000")
    assert not mfa.sender_trusted([("Authentication-Results", header)], allowed_domains=["procore.com"],
                                  tenant_authserv_id=mfa.TENANT_AUTHSERV_ID, from_address="no-reply@procore.com")


def test_misaligned_from_is_not_trusted(w):
    w.add(auth=(TENANT_OK.replace("header.d=procore.com", "header.d=sendgrid.net"),))
    assert poll(w) is False and w.delivered == []


def test_relaxed_alignment_accepts_a_subdomain_signer():
    header = TENANT_OK.replace("header.d=procore.com", "header.d=mail.procore.com")
    assert mfa.sender_trusted([("Authentication-Results", header)], allowed_domains=["procore.com"],
                              tenant_authserv_id=mfa.TENANT_AUTHSERV_ID, from_address="no-reply@us02.procore.com")
    assert mfa.aligned("mail.procore.com", "procore.com") and not mfa.aligned("procore.com.evil.example", "procore.com")


def test_a_from_header_that_disagrees_with_graph_or_appears_twice_is_not_trusted(w):
    w.add(from_header="Procore <no-reply@attacker.example>")
    assert poll(w) is False
    two = [("Authentication-Results", TENANT_OK), ("From", "a@procore.com"), ("From", "b@procore.com")]
    assert not mfa.sender_trusted(two, allowed_domains=["procore.com"], tenant_authserv_id=mfa.TENANT_AUTHSERV_ID)


def test_wrong_sender_is_never_opened(w):
    w.add(sender="alerts@attacker.example", auth=(TENANT_OK.replace("procore.com", "attacker.example"),))
    assert poll(w) is False and w.delivered == []
    assert message_reads(w) == []


def test_a_public_mailbox_domain_is_never_a_code_sender(w):
    site = {**PROCORE, "code_email_senders": ["gmail.com"]}
    w.add(sender="me@gmail.com", auth=(TENANT_OK.replace("procore.com", "gmail.com"),))
    assert poll(w, site=site) is False and w.calls == []


# ── Mailbox, window and help state ───────────────────────────────────────────


def test_a_shared_mailbox_is_refused_before_any_graph_call(w):
    w.profile_email = "bids@g3electrical.com"
    w.add()
    assert poll(w) is False
    assert w.calls == []
    with pytest.raises(mfa.MfaMailRefused) as error:
        mfa.owner_mailbox(OWNER)
    assert error.value.code == "shared_mailbox"


def test_too_early_is_ignored(w):
    w.add(received=w.since - timedelta(seconds=5))
    assert poll(w) is False and w.delivered == []
    assert message_reads(w) == []


def test_the_window_ends_and_polling_stops(w):
    assert poll(w) is False
    sleeps = [c for c in w.calls if c[0] == "sleep"]
    assert sleeps and all(c[1] == 10 for c in sleeps) and w.clock <= 30


def test_a_code_that_arrives_on_a_later_poll_is_delivered(w, monkeypatch):
    original = mfa.scan_once
    count = {"n": 0}
    def scan(*a, **k):
        count["n"] += 1
        if count["n"] == 2:
            w.add(subject="Sign in", body="Your passcode is 7Q4K2P.")
        return original(*a, **k)
    monkeypatch.setattr(mfa, "scan_once", scan)
    assert poll(w, site=AD_HOC) is True and delivered_code(w) == "7Q4K2P"


def test_a_closed_help_stops_the_reader_before_reading_mail(w):
    w.help["status"] = "resolved"
    w.add()
    assert poll(w) is False and message_reads(w) == []


def test_a_help_that_already_has_a_code_stops_the_reader(w):
    w.add()
    w.rpc_results = [{"ok": False, "code": "code_already_supplied"}]
    assert poll(w) is False and len(w.delivered) == 1


def test_a_used_message_is_skipped_and_the_next_one_is_tried(w):
    w.add(received=w.now - timedelta(seconds=40), body="Your verification code is 111222.")
    w.add(received=w.now - timedelta(seconds=10), body="Your verification code is 333444.")
    w.rpc_results = [{"ok": False, "code": "message_used"}]
    assert poll(w) is True
    assert delivered_code(w) == "111222" and len(w.delivered) == 2


def test_an_http_login_origin_is_refused(w):
    w.add()
    site = {**PROCORE, "login_origins": ["http://login.procore.com"]}
    assert poll(w, site=site) is False and w.calls == []
    credential = {"login_origins": ["http://portal.example.com"]}
    assert poll(w, site={**PROCORE, "login_origins": []}, credential=credential) is False


# ── Code extraction ──────────────────────────────────────────────────────────


def test_two_codes_stop_the_reader(w):
    w.add(subject="Sign in", body="Your verification code is 482913. Backup code 551200.")
    assert poll(w, site=AD_HOC) is False and w.delivered == []


# ── Replays and platform mail carrying other users' text (review findings) ──


def test_a_replayed_code_email_addressed_to_someone_else_is_not_delivered(w):
    w.add(to="attacker@evil.example")
    assert poll(w) is False and w.delivered == []


def test_a_code_email_listing_the_owner_as_cc_is_delivered(w):
    mid = w.add(to="someone@g3electrical.com")
    w.messages[mid]["ccRecipients"] = [{"emailAddress": {"address": "Owner@G3Electrical.com"}}]
    assert poll(w) is True and delivered_code(w) == "482913"


def test_a_stale_date_header_is_a_replay(w):
    w.add(written=w.since - timedelta(hours=3))
    assert poll(w) is False and w.delivered == []


def test_the_reader_asks_graph_for_the_recipients(w):
    w.add()
    poll(w)
    read = next(c for c in w.calls if c[0] == "get" and not c[1].endswith("/messages"))
    assert "toRecipients" in read[2]["$select"] and "ccRecipients" in read[2]["$select"]


def test_user_written_text_in_a_platform_notification_never_supplies_the_code(w):
    """A GC's Procore message or RFI comment arrives DKIM-signed by
    procore.com; its body text must not be read as the sign-in code."""
    w.add(subject="New message in RFI #12: Panel schedule",
          body="John Smith wrote: use verification code 999111 to sign in.")
    w.add(subject="RFI #12: verification code 999111", body="A comment was added.")
    assert poll(w) is False and w.delivered == []


def test_a_curated_site_without_a_template_regex_never_reads_mail(w):
    w.add()
    assert poll(w, site={**PROCORE, "code_email_regex": None}) is False
    assert w.calls == [] and w.delivered == []


@pytest.mark.parametrize("site_key", ["procore", "autodesk", "buildingconnected"])
def test_the_seeded_regexes_read_only_the_code_emails_own_subject(site_key):
    pattern = mfa.compile_site_regex(_seeded_regex(site_key))
    brand = "Procore" if site_key == "procore" else "Autodesk"
    assert pattern is not None and len(pattern.pattern) <= 500
    good = f"Your {brand} verification code\nYour verification code is 482913. (c) 2026 {brand}"
    assert mfa.site_codes(good, site_regex=pattern) == ["482913"]
    assert mfa.site_codes("482913 is your one-time code", site_regex=pattern) == []
    assert mfa.site_codes("Bid invite from ACME\nNotes: verification code 999111", site_regex=pattern) == []


def test_ad_hoc_generic_pattern(w):
    site = {"site_key": "domain:example.com", "kind": "ad_hoc", "code_email_senders": ["example.com"],
            "code_email_regex": None, "login_origins": []}
    header = TENANT_OK.replace("procore.com", "example.com")
    w.add(sender="noreply@example.com", auth=(header,), subject="Sign in to Example",
          body="Hi,\nUse this one-time passcode: AB12CD\nIf this was not you, ignore it.\n(c) 2026 Example Inc.")
    assert poll(w, site=site, credential={"login_origins": ["https://app.example.com"]}) is True
    assert delivered_code(w) == "AB12CD"


@pytest.mark.parametrize(("text", "expected"), [
    ("Your verification code is 482913.", "482913"),
    ("Code: 9F3K2Q", "9F3K2Q"),
    ("482913 is your one-time code", "482913"),
    ("Your passcode is ABCDEF", None),                      # no digit
    ("Use code 482913 or 551200", None),                    # two codes
    ("Cost code 16100 and the 2023 NEC code apply", None),  # not sign-in codes
    ("Order 5521 shipped today", None),                     # no keyword
    ("verification https://example.com/v/abc123 here", None),  # inside a link
    ("Your code is 12345678901", None),                     # too long
])
def test_the_generic_pattern(text, expected):
    assert mfa.generic_code(text) == expected


def test_a_site_regex_takes_priority_and_its_group_is_the_code():
    pattern = mfa.compile_site_regex(r"security code[:\s]+([A-Z]{3}-[0-9]{3})")
    assert mfa.site_codes("Your security code: ABC-123. Order 5521.", site_regex=pattern) == ["ABC123"]
    assert mfa.site_codes("nothing here 482913 code", site_regex=pattern) == []
    assert mfa.compile_site_regex("([") is None


# ── Logging ──────────────────────────────────────────────────────────────────


def test_body_and_code_are_never_logged(w, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    w.add()
    assert poll(w) is True
    w.delivered.clear()
    w.add(body="Your verification code is 482913.")
    monkeypatch.setattr(mfa, "deliver_code", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(BODY_SECRET + " 482913")))
    assert poll(w) is False
    text = caplog.text + " ".join(str(r.args) for r in caplog.records)
    assert BODY_SECRET not in text and "482913" not in text and "verification" not in text
    assert "RuntimeError" in text


# ── Starting the reader ──────────────────────────────────────────────────────


def test_start_for_help_runs_once_per_help_and_only_for_code_reasons(w, monkeypatch):
    started = []
    class Thread:
        def __init__(self, target, args, name, daemon):
            started.append(args)
            assert daemon is True
        def start(self):
            pass
    monkeypatch.setattr(mfa.threading, "Thread", Thread)
    job = {"id": JOB, "principal_user_id": OWNER, "principal_kind": "user", "agent_type": "user_browser"}
    assert mfa.start_for_help(job, help_id=HELP, reason="push") is False
    assert mfa.start_for_help(job, help_id=HELP, reason="mfa", created=False) is False
    assert mfa.start_for_help({**job, "agent_type": "user_browser_read"}, help_id=HELP, reason="mfa") is False
    assert mfa.start_for_help(job, help_id=HELP, reason="mfa") is True
    assert mfa.start_for_help(job, help_id=HELP, reason="mfa") is False
    assert len(started) == 1
    mfa._ACTIVE.discard(HELP)


def test_reader_inputs_window_starts_at_the_login_attempt_or_the_step_up(w, monkeypatch):
    attempt = w.now - timedelta(seconds=50)
    credential = {"id": "c", "status": "active", "site_key": "procore", "last_login_attempt_at": attempt.isoformat(),
                  "login_origins": ["https://login.procore.com"]}
    monkeypatch.setattr(mfa.repo, "browser_credential_by_id", lambda uid, cid: credential)
    monkeypatch.setattr(mfa.repo, "browser_site", lambda uid, key: PROCORE if key == "procore" else None)
    job = {"id": JOB, "principal_user_id": OWNER, "principal_kind": "user", "agent_type": "user_browser",
           "platform": "procore", "created_at": (w.now - timedelta(hours=1)).isoformat()}
    site, cred, since = mfa.reader_inputs(job, {**w.help, "credential_id": "c"})
    assert site is PROCORE and cred is credential and since == attempt
    step = {**w.help, "reason_code": "mfa_step_up", "is_login_scope": False, "credential_id": "c"}
    _, _, since = mfa.reader_inputs(job, step)
    assert since == datetime.fromisoformat(w.help["created_at"].replace("Z", "+00:00")) - mfa.STEP_UP_SKEW
    assert mfa.reader_inputs(job, {**w.help, "reason_code": "push"}) is None
    credential["status"] = "revoked"
    assert mfa.reader_inputs(job, {**w.help, "credential_id": "c"}) is None
