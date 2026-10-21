"""Auto send internal emails: preflight and digest rendering, subjects and the
recipient query. Pure rendering plus a recording fake Supabase; no network."""

from types import SimpleNamespace

import pytest

from app.core import roles
from app.core.roles import Role
from app.services import auto_send_emails as ase

BASE = "https://bdr.example.com/"
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

EVIL = "<script>alert(1)</script> & Sons"
EVIL_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Sons"


def _project(pid="p1", name="Anita Gelato TI", number="7001"):
    return {"id": pid, "name": name, "number": number}


def _ready(**over):
    ev = {
        "outcome": "ready",
        "reasons": [],
        "markup_pct": "10.00",
        "amounts": {
            "material": "11000.00",
            "labor": "22000.5",
            "total": "33000.50",
            "base_material": "10000.00",
            "base_labor": "20000.45",
        },
        "gcs": [
            {"gc_id": "gc1", "gc_name": "Acme GC",
             "recipients": ["bids@acme.test", "pm@acme.test"], "reason": None},
            {"gc_id": "gc2", "gc_name": "Quiet GC", "recipients": [],
             "reason": {"code": "gc_no_email",
                        "message": "Quiet GC has no contact with an email address."}},
        ],
    }
    ev.update(over)
    return ev


def _preflight(items, name="Pat Admin"):
    return ase.render_preflight_email(
        recipient_name=name, date_label="Tuesday, October 20", send_label="5:05 PM",
        items=items, base_url=BASE,
    )


def _digest(items, name="Pat Admin"):
    return ase.render_digest_email(
        recipient_name=name, date_label="Tuesday, October 20", items=items, base_url=BASE,
    )


# ── Labels / subjects ─────────────────────────────────────────────────────


def test_time_label():
    assert ase.time_label(17, 5) == "5:05 PM"
    assert ase.time_label(15, 0) == "3:00 PM"
    assert ase.time_label(9, 30) == "9:30 AM"


def test_subjects():
    assert ase.preflight_subject("Tuesday, October 20", "5:05 PM") == (
        "G3 BDR · [HIGH IMPORTANCE] Auto send at 5:05 PM today: Tuesday, October 20"
    )
    assert ase.digest_subject("Tuesday, October 20") == (
        "G3 BDR · [HIGH IMPORTANCE] Auto send results: Tuesday, October 20"
    )


def test_project_url_opens_auto_send_panel():
    assert ase.project_url(BASE, "p1") == "https://bdr.example.com/projects/p1?box=auto_send"


# ── Preflight ─────────────────────────────────────────────────────────────


def test_preflight_sections_and_numbers():
    body = _preflight([
        {"project": _project("p1"), "evaluation": _ready()},
        {"project": _project("p2", "Blocked Job", "7002"), "evaluation": {
            "outcome": "blocked",
            "reasons": [{"code": "estimate_not_received",
                         "message": "The estimator has not submitted an estimate yet."}],
        }},
        {"project": _project("p3", "Dev Job", "7003"), "evaluation": {
            "outcome": "skipped",
            "reasons": [{"code": "dev_estimator", "message": "The estimator on this project "
                         "is the dev account, so it is not auto sent."}],
        }},
        {"project": _project("p4", "Quiet Job", "7004"),
         "evaluation": {"outcome": "silent", "reasons": []}},
    ])
    assert "HIGH IMPORTANCE: AUTO SEND AT 5:05 PM" in body
    assert "Hi Pat," in body
    assert "Will auto send at 5:05 PM (1)" in body
    assert "Will not auto send (1)" in body
    assert "Not auto sent by rule (1)" in body
    assert "$11,000.00" in body and "$22,000.50" in body and "$33,000.50" in body
    assert "materials $10,000.00" in body and "labor $20,000.45" in body
    assert "Markup 10%" in body
    assert "bids@acme.test, pm@acme.test" in body
    assert "Quiet GC has no contact with an email address." in body
    assert "The estimator has not submitted an estimate yet." in body
    assert "dev account" in body
    assert "https://bdr.example.com/projects/p1?box=auto_send" in body
    assert "https://bdr.example.com/projects/p2?box=auto_send" in body
    assert "Quiet Job" not in body  # silent projects are left out


def test_preflight_fractional_markup():
    body = _preflight([{"project": _project(), "evaluation": _ready(markup_pct="12.50")}])
    assert "Markup 12.5%" in body


def test_preflight_escapes_user_content():
    ev = _ready(gcs=[{"gc_id": "g", "gc_name": EVIL, "recipients": [f"{EVIL}@x.test"],
                      "reason": None}])
    body = _preflight(
        [
            {"project": _project("p1", EVIL, "<7001>"), "evaluation": ev},
            {"project": _project("p2", "Blocked"), "evaluation": {
                "outcome": "blocked", "reasons": [{"code": "x", "message": EVIL}]}},
        ],
        name="<b>Pat</b> Admin",
    )
    assert "<script>" not in body and "<7001>" not in body and "<b>Pat</b>" not in body
    assert EVIL_ESCAPED in body
    assert "&lt;7001&gt;" in body


# ── Digest ────────────────────────────────────────────────────────────────


def _run(status, **over):
    run = {
        "id": f"run-{status}",
        "project_id": "p1",
        "bid_date": "2026-10-20",
        "status": status,
        "reasons": [],
        "markup_pct": "10.00",
        "estimate": {"ok": True, "materials": "10000.00", "labor": "20000.00"},
        "material_amount": "11000.00",
        "labor_amount": "22000.00",
        "total_amount": "33000.00",
        "redirected_to": None,
    }
    run.update(over)
    return run


def _gc(status, name="Acme GC", gc_id="gc1", error=None, recipients="bids@acme.test"):
    return {"gc_id": gc_id, "gc_name": name, "status": status, "error": error,
            "recipients": recipients}


def test_digest_sent_section():
    body = _digest([{
        "project": _project(),
        "run": _run("sent", redirected_to="qa@g3.test"),
        "gcs": [_gc("sent"), _gc("sent", "Beta GC", "gc2", recipients="a@b.test, c@d.test")],
    }])
    assert "HIGH IMPORTANCE: AUTO SEND RESULTS" in body
    assert "Sent (1)" in body
    assert "Could not send" not in body
    assert "7001 · Anita Gelato TI" in body
    assert "$11,000.00" in body and "$22,000.00" in body and "$33,000.00" in body
    assert "materials $10,000.00" in body and "labor $20,000.00" in body
    assert "Markup 10%" in body
    assert "bids@acme.test" in body and "a@b.test, c@d.test" in body
    assert "Test mode: every GC email went only to qa@g3.test." in body
    assert "https://bdr.example.com/projects/p1?box=auto_send" in body


def test_digest_partial_lists_sent_and_failed_gcs():
    body = _digest([{
        "project": _project(),
        "run": _run("partial", reasons=[
            {"code": "send_failed", "gc_id": "gc2",
             "message": "The email to Beta GC failed: mailbox full"},
        ]),
        "gcs": [
            _gc("sent"),
            _gc("failed", "Beta GC", "gc2", error="mailbox full"),
            _gc("unknown", "Gamma GC", "gc3"),
        ],
    }])
    assert "Sent (1)" in body and "Could not send (1)" in body
    assert "Partly sent" in body
    assert "The email to Beta GC failed: mailbox full" in body
    assert body.count("mailbox full") == 1  # covered by the per-GC reason, not repeated
    assert "Gamma GC: The email timed out" in body


def test_digest_failed_missed_and_skipped():
    body = _digest([
        {"project": _project("p1"), "run": _run("failed", reasons=[
            {"code": "no_scope_lines",
             "message": "There are no scope lines yet. Add them in the Auto send panel."}]),
         "gcs": []},
        {"project": _project("p2", "Down Job", "7002"), "run": _run("missed", reasons=[
            {"code": "missed", "message": "The server was not running at 5:05 PM, so this "
             "was not sent. Open the project to send it now."}]), "gcs": []},
        {"project": _project("p3", "Dev Job", "7003"), "run": _run("skipped", reasons=[
            {"code": "dev_estimator", "message": "Dev account estimator."}]), "gcs": []},
    ])
    assert "Sent (" not in body
    assert "Could not send (2)" in body
    assert "Not auto sent by rule (1)" in body
    assert "There are no scope lines yet." in body
    assert "Missed" in body and "The server was not running at 5:05 PM" in body
    assert "Dev account estimator." in body


def test_digest_escapes_user_content():
    body = _digest([{
        "project": _project("p1", EVIL, "7001"),
        "run": _run("partial", reasons=[{"code": "x", "message": EVIL}],
                    redirected_to=f"{EVIL}@x.test"),
        "gcs": [_gc("sent", EVIL, recipients=f"{EVIL}@x.test"),
                _gc("failed", "Other", "gc9", error=EVIL)],
    }])
    assert "<script>" not in body
    assert EVIL_ESCAPED in body


@pytest.mark.parametrize("which", ["preflight", "digest"])
def test_emails_have_no_em_or_en_dash(which):
    if which == "preflight":
        body = _preflight([
            {"project": _project(), "evaluation": _ready()},
            {"project": _project("p2"), "evaluation": {"outcome": "blocked", "reasons": []}},
            {"project": _project("p3"), "evaluation": {"outcome": "skipped", "reasons": []}},
        ])
        subject = ase.preflight_subject("Tuesday, October 20", "5:05 PM")
    else:
        body = _digest([
            {"project": _project(), "run": _run(s), "gcs": [_gc("sent"), _gc("unknown")]}
            for s in ("sent", "partial", "failed", "missed", "skipped", "sending")
        ])
        subject = ase.digest_subject("Tuesday, October 20")
    for text in (body, subject):
        assert EM_DASH not in text and EN_DASH not in text


def test_module_source_has_no_em_dash():
    with open(ase.__file__, encoding="utf-8") as fh:
        assert EM_DASH not in fh.read()


# ── Recipients ────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, rec, rows):
        self._rec = rec
        self._rows = rows

    def select(self, cols):
        self._rec["select"] = cols
        return self

    def in_(self, col, vals):
        self._rec["in"] = (col, sorted(vals))
        return self

    def eq(self, col, val):
        self._rec["eq"] = (col, val)
        return self

    def order(self, *a):
        return self

    def range(self, lo, hi):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


def test_notify_recipients(monkeypatch):
    monkeypatch.setattr(
        roles,
        "AUTO_SEND_NOTIFY_ROLES",
        frozenset({Role.ESTIMATING_ADMIN, Role.ACCOUNTANT, Role.EXECUTIVE, Role.IT_ADMIN}),
    )
    rows = [
        {"id": "u1", "full_name": "A", "email": "a@g3.test", "role": "estimating_admin"},
        {"id": "u2", "full_name": "B", "email": "b@g3.test", "role": "accountant"},
        {"id": "u3", "full_name": "C", "email": "", "role": "executive"},
        {"id": "u4", "full_name": "D", "email": None, "role": "it_admin"},
        # defense in depth: a row the role filter should have dropped
        {"id": "u5", "full_name": "E", "email": "e@g3.test", "role": "estimating_engineer_labor"},
    ]
    rec: dict = {}
    sb = SimpleNamespace(table=lambda name: rec.setdefault("table", name) and _Query(rec, rows))
    out = ase.notify_recipients(sb)
    assert [p["id"] for p in out] == ["u1", "u2"]
    assert rec["table"] == "profiles"
    assert rec["in"] == ("role", ["accountant", "estimating_admin", "executive", "it_admin"])
    assert rec["eq"] == ("is_active", True)
