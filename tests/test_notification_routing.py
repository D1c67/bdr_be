"""Notification routing by role (services/notification_routing, 0177): the
defaults decided with G3 on 2026-10-23, event mapping, overrides and the
Settings grid writes."""

from types import SimpleNamespace

import pytest

from app.core.roles import Role
from app.services import due_digest, notification_email, notification_routing as nr

EXE, EA, LAB, MAT, IT, ACC = (
    Role.EXECUTIVE,
    Role.ESTIMATING_ADMIN,
    Role.ESTIMATING_ENGINEER_LABOR,
    Role.ESTIMATING_ENGINEER_MATERIALS,
    Role.IT_ADMIN,
    Role.ACCOUNTANT,
)


def _teams_events(role: Role) -> set[str]:
    return {e.key for e in nr.EVENTS if nr.wants(role, e.key, "teams")}


def test_teams_defaults_per_role():
    assert _teams_events(EXE) == {
        "digest.bids_due_today", "go_no_go", "verify", "call_in", "approval_requested",
        "revision_approval", "revision_round", "reverify_required", "personal",
    }
    assert _teams_events(EA) == {
        "digest.bids_due_today", "digest.auto_send", "intake_needed", "rfp_review",
        "send_to_estimator", "stage_handoff", "verified", "auto_send_sent",
        "auto_send_problem", "proposal_send_failed", "revision_round", "personal",
    }
    assert _teams_events(LAB) == {
        "digest.bids_due_today", "estimate_submitted", "quote_received", "late_quote",
        "verified", "revision_round", "drawing_changed", "reverify_required", "personal",
    }
    assert _teams_events(MAT) == {
        "estimate_submitted", "revision_round", "drawing_changed", "personal",
    }
    assert _teams_events(IT) == {
        "digest.bids_due_today", "go_no_go", "verify", "tech_outage", "security_alert",
        "personal",
    }
    assert _teams_events(ACC) == {"digest.auto_send", "personal"}


def test_email_keeps_todays_sends_except_the_new_audiences():
    # The audiences added on 2026-10-23 get bell + Teams, no email.
    assert not nr.wants(LAB, "quote_received", "email")
    assert not nr.wants(LAB, "verified", "email")
    assert not nr.wants(IT, "verify", "email")
    assert not nr.wants(IT, "go_no_go", "email")
    # Everything that emailed before still does.
    assert nr.wants(MAT, "quote_received", "email")
    assert nr.wants(LAB, "late_quote", "email")
    assert nr.wants(EA, "verified", "email")
    assert nr.wants(EXE, "verify", "email")
    assert nr.wants(EXE, "go_no_go", "email")
    assert nr.wants(MAT, "submitted", "email")
    assert nr.wants(EA, "digest.bids_due_today", "email")
    assert nr.wants(ACC, "digest.auto_send", "email")


@pytest.mark.parametrize(
    ("type_", "metadata", "event"),
    [
        ("stage_handoff", {"stage": "verify"}, "verify"),
        ("stage_handoff", {"stage": "go_no_go"}, "go_no_go"),
        ("stage_handoff", {"stage": "rfqs"}, "stage_handoff"),
        ("stage_handoff", None, "stage_handoff"),
        ("rfp_create.created", None, "go_no_go"),
        ("quote.received", None, "quote_received"),
        ("rfq.reply_received", None, "quote_received"),
        ("rfq.initial_send", None, "rfq_initial_send"),
        ("estimate_revised", None, "revision_round"),
        ("note_mention", None, "personal"),
        ("due.internal_bid.1d", None, "due"),
        ("rfp_processing.model_away", None, "tech_outage"),
        ("email_ingest.llm_outage", None, "tech_outage"),
        ("pm_stage_change", None, "pm"),
        ("something_new", None, "other"),
    ],
)
def test_event_for(type_, metadata, event):
    assert nr.event_for(type_, metadata) == event


def test_override_wins_and_estimator_is_not_routed(monkeypatch):
    monkeypatch.setattr(
        nr, "_overrides",
        lambda: {("executive", "call_in"): {"email": False, "teams": None}},
    )
    assert not nr.wants(EXE, "call_in", "email")
    assert nr.wants(EXE, "call_in", "teams")  # NULL = default
    assert nr.wants(Role.ESTIMATOR, "personal", "teams")  # teams_alerts refuses estimators


def test_role_outside_audience_never_gains_teams():
    assert not nr.wants(ACC, "quote_received", "teams")
    assert nr.wants(ACC, "quote_received", "email")  # code decides; keep today's mail


class _Table:
    def __init__(self, log, existing):
        self.log, self.existing, self.op = log, existing, None

    def select(self, *a):
        self.op = "select"
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, row, **k):
        self.op = ("upsert", row)
        return self

    def execute(self):
        if self.op == "select":
            return SimpleNamespace(data=self.existing)
        self.log.append(self.op)
        return SimpleNamespace(data=[])


@pytest.fixture
def table(monkeypatch):
    log: list = []
    state = {"existing": []}
    monkeypatch.setattr(
        nr, "get_supabase",
        lambda: SimpleNamespace(table=lambda name: _Table(log, state["existing"])),
    )
    return SimpleNamespace(log=log, state=state)


def test_set_cell_stores_only_differences(table):
    nr.set_cell("executive", "call_in", "teams", False, "actor")
    op, row = table.log[0]
    assert op == "upsert" and row["teams"] is False and row["email"] is None
    # Back to the default with nothing else overridden: the row goes away.
    table.state["existing"] = [{"email": None, "teams": False}]
    nr.set_cell("executive", "call_in", "teams", True, "actor")
    assert table.log[1] == "delete"


@pytest.mark.parametrize(
    ("role", "event", "channel", "match"),
    [
        ("accountant", "quote_received", "teams", "does not receive"),
        ("estimating_admin", "auto_send_sent", "email", "never sends an email"),
        ("estimator", "personal", "teams", "Unknown"),
        ("executive", "nope", "teams", "Unknown"),
    ],
)
def test_set_cell_refuses_impossible_cells(table, role, event, channel, match):
    with pytest.raises(nr.RoutingError, match=match):
        nr.set_cell(role, event, channel, True, "actor")
    assert table.log == []


def test_grid_lists_only_audience_cells():
    grid = nr.grid()
    assert grid["roles"] == [r.value for r in nr.ROUTED_ROLES]
    quotes = next(e for e in grid["events"] if e["key"] == "quote_received")
    assert set(quotes["cells"]) == {LAB.value, MAT.value}
    assert quotes["cells"][LAB.value] == {
        "email": False, "email_default": False, "teams": True, "teams_default": True,
    }


def test_email_mirror_skips_roles_routed_off(monkeypatch):
    sent = []
    monkeypatch.setattr(
        notification_email.graph_email, "send_mail", lambda **k: sent.append(k) or None
    )
    profile = {"id": "u", "full_name": "L", "email": "l@g3.com",
               "role": LAB.value, "is_active": True}
    row = {"user_id": "u", "project_id": None, "type": "quote.received", "message": "m"}
    notification_email._send_one(row, profile, None)
    assert sent == []
    notification_email._send_one({**row, "type": "late_quote.received"}, profile, None)
    assert len(sent) == 1


@pytest.mark.parametrize("role", [EA, EXE, IT])
def test_rfq_oversight_emails_only_the_initial_send(monkeypatch, role):
    # A stale queued row or a recipient whose role changed still must not
    # get replies, even if an obsolete override asks to turn their email on.
    monkeypatch.setattr(nr, "_overrides", lambda: {
        (role.value, "quote_received"): {"email": True},
        (role.value, "late_quote"): {"email": True},
    })
    sent = []
    monkeypatch.setattr(
        notification_email.graph_email, "send_mail", lambda **k: sent.append(k)
    )
    profile = {"role": role.value, "is_active": True, "email": "team@example.com"}
    for type_ in ("rfq.reply_received", "quote.received", "late_quote.received"):
        notification_email._send_one({"type": type_}, profile, None)
    assert sent == []
    notification_email._send_one({"type": "rfq.initial_send"}, profile, None)
    assert len(sent) == 1
    assert "RFQs sent to vendors" in sent[0]["subject"]


@pytest.mark.parametrize("role", [EA, EXE, IT])
def test_rfq_oversight_reply_email_cannot_be_enabled(table, role):
    for event in ("quote_received", "late_quote"):
        with pytest.raises(nr.RoutingError, match="does not receive"):
            nr.set_cell(role.value, event, "email", True, "actor")
    assert table.log == []


def test_digest_teams_lines_are_short():
    rows = [
        {
            "project": {"name": "Tower", "number": "42", "internal_bid_at": None},
            "checks": {"has_quotes": False, "has_labor": True, "has_markups": False},
        },
        {
            "project": {"name": "Depot", "number": None, "internal_bid_at": None},
            "checks": {"has_quotes": True, "has_labor": True, "has_markups": True},
        },
    ]
    assert due_digest.teams_lines(rows) == [
        "#42 Tower · missing quotes, markups",
        "Depot · ready",
    ]
