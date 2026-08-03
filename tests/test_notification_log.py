"""The per-project notification log's event assembly.

Everything here exercises the pure half of services/notification_log — bell rows
+ email_log rows + classifications → events. No database and no network: the
identity lookups are handed in as a prebuilt `_People`.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import notification_log as route
from app.services import notification_log as nl

ADMIN_A = "11111111-1111-1111-1111-111111111111"
ADMIN_B = "22222222-2222-2222-2222-222222222222"
EST = "33333333-3333-3333-3333-333333333333"

PEOPLE = nl._People(
    by_id={
        ADMIN_A: {"id": ADMIN_A, "full_name": "A. Chen", "email": "achen@g3.com", "role": "estimating_admin"},
        ADMIN_B: {"id": ADMIN_B, "full_name": "B. Ortiz", "email": "bortiz@g3.com", "role": "estimating_admin"},
        EST: {"id": EST, "full_name": "M. Rivera", "email": "m@ext.com", "role": "estimator"},
    },
    by_email={
        "achen@g3.com": {"name": "A. Chen", "role": "estimating_admin", "audience": "internal"},
        "bortiz@g3.com": {"name": "B. Ortiz", "role": "estimating_admin", "audience": "internal"},
        "m@ext.com": {"name": "M. Rivera", "role": "estimator", "audience": "estimator"},
        "sales@codale.com": {"name": "Dana Q.", "role": None, "audience": "vendor", "org": "CODALE"},
        "bids@wt.com": {"name": "J. Doe", "role": None, "audience": "gc", "org": "Whiting-Turner"},
    },
)


def _notif(id_, user_id, type_, message, created_at, **extra):
    row = {
        "id": id_,
        "user_id": user_id,
        "type": type_,
        "message": message,
        "created_at": created_at,
        "read_at": None,
        "dismissed_at": None,
        "rfq_id": None,
        "email_log_id": None,
    }
    row.update(extra)
    return row


def _email(id_, to_addrs, subject, created_at, **extra):
    row = {
        "id": id_,
        "to_addrs": to_addrs,
        "subject": subject,
        "created_at": created_at,
        "status": "sent",
        "error": None,
        "rfq_id": None,
        "sent_by": None,
    }
    row.update(extra)
    return row


def _assemble(notifs=(), emails=(), classes=None):
    return nl.assemble(list(notifs), list(emails), classes or {}, PEOPLE)


# ── address parsing ─────────────────────────────────────────────────────────


def test_addresses_splits_dedupes_and_trims():
    assert nl._addresses("a@x.com, b@x.com , a@x.com") == ["a@x.com", "b@x.com"]
    assert nl._addresses("") == []
    assert nl._addresses(None) == []


# ── bell rows group into one event per broadcast ────────────────────────────


def test_role_broadcast_collapses_into_one_entry():
    """notify_role inserts every row in ONE statement, so they share an exact
    created_at — that is what makes them one event rather than three."""
    at = "2026-07-30T21:14:02.123456+00:00"
    entries = _assemble(
        notifs=[
            _notif("n1", ADMIN_A, "estimate_submitted", "Estimate submitted", at),
            _notif("n2", ADMIN_B, "estimate_submitted", "Estimate submitted", at),
        ]
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["title"] == "Estimate submitted"
    assert entry["counts"]["recipients"] == 2
    assert entry["channels"] == ["in_app"]
    assert {r["name"] for r in entry["recipients"]} == {"A. Chen", "B. Ortiz"}
    assert all(r["audience"] == "internal" for r in entry["recipients"])


def test_same_type_at_different_times_stays_two_entries():
    entries = _assemble(
        notifs=[
            _notif("n1", ADMIN_A, "verified", "Pricing committed", "2026-07-30T10:00:00+00:00"),
            _notif("n2", ADMIN_A, "verified", "Pricing committed", "2026-07-31T10:00:00+00:00"),
        ]
    )
    assert len(entries) == 2


def test_read_state_is_carried_per_recipient():
    at = "2026-07-30T21:14:02+00:00"
    entries = _assemble(
        notifs=[
            _notif("n1", ADMIN_A, "verified", "m", at, read_at="2026-07-30T22:00:00+00:00"),
            _notif("n2", ADMIN_B, "verified", "m", at),
        ]
    )
    assert entries[0]["counts"]["read"] == 1


# ── a bell row and its mirror email are ONE event ───────────────────────────


def test_explicit_link_folds_the_mirror_into_the_bell_entry():
    """The 0091 FK is exact: no time window, no subject sniffing."""
    entries = _assemble(
        notifs=[
            _notif(
                "n1", ADMIN_A, "verified", "Pricing committed",
                "2026-07-30T21:14:02+00:00", email_log_id="e1",
            )
        ],
        emails=[
            _email("e1", "achen@g3.com", "G3 BDR · Pricing committed", "2026-07-30T21:14:09+00:00")
        ],
    )
    assert len(entries) == 1, "the mirror email must not also be its own entry"
    entry = entries[0]
    assert entry["channels"] == ["in_app", "email"]
    assert entry["recipients"][0]["email_status"] == "sent"
    assert entry["counts"]["emailed"] == 1


def test_legacy_mirror_matches_on_prefix_recipient_and_window():
    """Rows written before 0091 have no link — the narrow fallback covers them."""
    entries = _assemble(
        notifs=[_notif("n1", ADMIN_A, "verified", "Pricing committed", "2026-07-30T21:14:02+00:00")],
        emails=[
            _email("e1", "achen@g3.com", "G3 BDR · Pricing committed", "2026-07-30T21:14:09+00:00")
        ],
    )
    assert len(entries) == 1
    assert entries[0]["channels"] == ["in_app", "email"]


def test_legacy_mirror_ignored_outside_the_window():
    entries = _assemble(
        notifs=[_notif("n1", ADMIN_A, "verified", "Pricing committed", "2026-07-30T21:14:02+00:00")],
        emails=[
            _email("e1", "achen@g3.com", "G3 BDR · Pricing committed", "2026-07-30T23:00:00+00:00")
        ],
    )
    assert len(entries) == 2, "an email an hour later is a different event"


def test_legacy_mirror_ignored_for_multi_recipient_mail():
    """A mirror goes to exactly one person; a multi-address send sharing the
    prefix is a different kind of mail and keeps its own entry."""
    entries = _assemble(
        notifs=[_notif("n1", ADMIN_A, "verified", "Pricing committed", "2026-07-30T21:14:02+00:00")],
        emails=[
            _email(
                "e1", "achen@g3.com, bortiz@g3.com",
                "G3 BDR · Pricing committed", "2026-07-30T21:14:09+00:00",
            )
        ],
    )
    assert len(entries) == 2


def test_one_mirror_cannot_claim_two_bell_rows():
    """Two rows, one email: the second must stay unmatched rather than double-count."""
    at = "2026-07-30T21:14:02+00:00"
    entries = _assemble(
        notifs=[
            _notif("n1", ADMIN_A, "verified", "first", at),
            _notif("n2", ADMIN_A, "assigned", "second", at),
        ],
        emails=[_email("e1", "achen@g3.com", "G3 BDR · Pricing committed", at)],
    )
    emailed = [e for e in entries if "email" in e["channels"]]
    assert len(emailed) == 1


# ── fan-out sends are one event with many recipients ────────────────────────


RFQ_CLASS = {
    "type": "rfq_send",
    "title": "RFQ sent to vendors",
    "group_key": "rfq:r1",
    "category": "Switchgear",
}


def test_rfq_fanout_is_one_entry_with_every_vendor():
    entries = _assemble(
        emails=[
            _email("e1", "sales@codale.com", "RFQ — Switchgear", "2026-07-30T09:00:00+00:00"),
            _email("e2", "bids@wt.com", "RFQ — Switchgear", "2026-07-30T09:00:02+00:00"),
        ],
        classes={"e1": dict(RFQ_CLASS), "e2": dict(RFQ_CLASS)},
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["type"] == "rfq_send"
    assert entry["category"] == "Switchgear"
    assert entry["counts"]["recipients"] == 2
    assert entry["channels"] == ["email"]
    codale = next(r for r in entry["recipients"] if r["email"] == "sales@codale.com")
    assert codale["audience"] == "vendor" and codale["org"] == "CODALE"


def test_rfq_resend_much_later_is_a_separate_entry():
    entries = _assemble(
        emails=[
            _email("e1", "sales@codale.com", "RFQ — Switchgear", "2026-07-30T09:00:00+00:00"),
            _email("e2", "sales@codale.com", "RFQ — Switchgear", "2026-07-31T09:00:00+00:00"),
        ],
        classes={"e1": dict(RFQ_CLASS), "e2": dict(RFQ_CLASS)},
    )
    assert len(entries) == 2


def test_to_and_cc_on_one_message_are_not_double_listed():
    entries = _assemble(
        emails=[
            _email(
                "e1", "bids@wt.com, bids@wt.com, achen@g3.com",
                "Requested Submittals", "2026-07-30T09:00:00+00:00",
            )
        ]
    )
    assert entries[0]["counts"]["recipients"] == 2


# ── unclassified mail, failures, ordering ───────────────────────────────────


def test_unclassified_email_is_titled_by_its_subject():
    entries = _assemble(
        emails=[_email("e1", "bids@wt.com", "[G3] RFI 004 — Panel schedule", "2026-07-30T09:00:00+00:00")]
    )
    assert entries[0]["type"] == "email"
    assert entries[0]["title"] == "[G3] RFI 004 — Panel schedule"


def test_failed_send_surfaces_status_and_error():
    entries = _assemble(
        emails=[
            _email(
                "e1", "sales@codale.com", "RFQ — Switchgear", "2026-07-30T09:00:00+00:00",
                status="failed", error="mailbox full",
            )
        ]
    )
    entry = entries[0]
    assert entry["counts"]["failed"] == 1 and entry["counts"]["emailed"] == 0
    assert entry["recipients"][0]["email_error"] == "mailbox full"


def test_unknown_address_is_shown_not_dropped():
    entries = _assemble(
        emails=[_email("e1", "someone@nowhere.com", "Hello", "2026-07-30T09:00:00+00:00")]
    )
    recipient = entries[0]["recipients"][0]
    assert recipient["email"] == "someone@nowhere.com"
    assert recipient["audience"] == "external" and recipient["name"] is None


def test_entries_are_newest_first_across_both_streams():
    entries = _assemble(
        notifs=[_notif("n1", ADMIN_A, "verified", "m", "2026-07-30T12:00:00+00:00")],
        emails=[
            _email("e1", "sales@codale.com", "RFQ", "2026-07-30T08:00:00+00:00"),
            _email("e2", "bids@wt.com", "Proposal", "2026-07-30T18:00:00+00:00"),
        ],
    )
    assert [e["at"] for e in entries] == [
        "2026-07-30T18:00:00+00:00",
        "2026-07-30T12:00:00+00:00",
        "2026-07-30T08:00:00+00:00",
    ]
    assert len({e["id"] for e in entries}) == 3


def test_empty_project_yields_no_entries():
    assert _assemble() == []


# ── the route ───────────────────────────────────────────────────────────────


class _Projects:
    """Supabase stand-in whose projects lookup returns `rows`."""

    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


def _call(project_id, *, exists=True, monkeypatch=None):
    monkeypatch.setattr(route, "get_supabase", lambda: _Projects([{"id": project_id}] if exists else []))
    monkeypatch.setattr(route.notification_log, "build", lambda pid: {"entries": [], "truncated": False, "pid": pid})
    return route.project_notification_log(project_id, user=SimpleNamespace(id="u1"))


def test_route_returns_the_log_for_a_real_project(monkeypatch):
    pid = "11111111-1111-1111-1111-111111111111"
    assert _call(pid, monkeypatch=monkeypatch)["pid"] == pid


def test_route_404s_on_a_malformed_id(monkeypatch):
    """A non-uuid would reach PostgREST as a 22P02 — an unhandled 500 that loses
    its CORS headers and surfaces in the browser as "Failed to fetch"."""
    with pytest.raises(HTTPException) as exc:
        _call("not-a-uuid", monkeypatch=monkeypatch)
    assert exc.value.status_code == 404


def test_route_404s_on_an_unknown_project(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _call("11111111-1111-1111-1111-111111111111", exists=False, monkeypatch=monkeypatch)
    assert exc.value.status_code == 404
