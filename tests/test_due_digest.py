"""Daily bids-due-today digest - window math, checks, severity, poll_once.

Pure-logic tests plus poll_once against a fake Supabase client with the Graph
send stubbed; nothing here touches the network.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import due_digest as dd

SETTINGS = SimpleNamespace(
    due_digest_enabled=True,
    due_digest_poll_interval_seconds=60,
    due_digest_send_hour=6,
    due_digest_send_minute=3,
    due_digest_catchup_hours=4,
    frontend_url="https://bdr.example.com/",
)


def _local(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=dd.DIGEST_TZ)


# ── in_send_window ────────────────────────────────────────────────────────


def test_send_window_weekday_bounds():
    tue = (2026, 8, 25)  # a Tuesday
    assert not dd.in_send_window(_local(*tue, 6, 2), SETTINGS)   # before 6:03
    assert dd.in_send_window(_local(*tue, 6, 3), SETTINGS)       # start inclusive
    assert dd.in_send_window(_local(*tue, 9, 30), SETTINGS)      # catch-up
    assert not dd.in_send_window(_local(*tue, 10, 3), SETTINGS)  # horizon exclusive
    assert not dd.in_send_window(_local(*tue, 18, 0), SETTINGS)  # evening


def test_send_window_skips_weekends():
    assert not dd.in_send_window(_local(2026, 8, 22, 6, 30), SETTINGS)  # Saturday
    assert not dd.in_send_window(_local(2026, 8, 23, 6, 30), SETTINGS)  # Sunday
    assert dd.in_send_window(_local(2026, 8, 24, 6, 30), SETTINGS)      # Monday


# ── compute_checks ────────────────────────────────────────────────────────


def _rfq(pid="p1", section="materials", quotes=1):
    return {
        "id": "r",
        "project_id": pid,
        "material_categories": {"pricing_section": section},
        "quotes": [{"id": f"q{i}"} for i in range(quotes)],
    }


_FULL_MARKUP = {
    "labor_markup_amount": 1,
    "materials_markup_amount": 2,
    "gear_markup_amount": 3,
    "underground_markup_amount": 4,
    "low_voltage_markup_amount": 5,
}


def test_checks_quotes():
    assert dd.compute_checks([_rfq()], None, None)["has_quotes"]
    assert not dd.compute_checks([_rfq(), _rfq(quotes=0)], None, None)["has_quotes"]
    # No RFQs at all = the project has no numbers in, not a vacuous pass.
    assert not dd.compute_checks([], None, None)["has_quotes"]


def test_checks_labor():
    assert not dd.compute_checks([], None, None)["has_labor"]
    assert not dd.compute_checks([], {"labor_amount": None}, None)["has_labor"]
    assert dd.compute_checks([], {"labor_amount": 0}, None)["has_labor"]  # 0 counts


def test_checks_markups_present_sections_only():
    # materials-only project: labor + materials markup suffice.
    partial = {**_FULL_MARKUP, "gear_markup_amount": None}
    assert dd.compute_checks([_rfq()], None, partial)["has_markups"]
    # a gear-section RFQ makes the gear markup required.
    assert not dd.compute_checks(
        [_rfq(), _rfq(section="gear")], None, partial
    )["has_markups"]
    # labor markup is always required; a null pricing_section folds into materials.
    assert not dd.compute_checks(
        [_rfq(section=None)], None, {**_FULL_MARKUP, "labor_markup_amount": None}
    )["has_markups"]
    assert not dd.compute_checks(
        [_rfq(section=None)], None, {**_FULL_MARKUP, "materials_markup_amount": None}
    )["has_markups"]
    # no markups row at all
    assert not dd.compute_checks([_rfq()], None, None)["has_markups"]


def test_severity_precedence():
    assert dd.severity({"has_quotes": False, "has_labor": False, "has_markups": True}) == "red"
    assert dd.severity({"has_quotes": True, "has_labor": False, "has_markups": True}) == "yellow"
    assert dd.severity({"has_quotes": True, "has_labor": True, "has_markups": False}) == "yellow"
    assert dd.severity({"has_quotes": True, "has_labor": True, "has_markups": True}) == "ok"


# ── poll_once against a fake Supabase ─────────────────────────────────────


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, sb, table):
        self._sb = sb
        self._table = table
        self._op = "select"
        self._payload = None

    def select(self, *a, **k):
        return self

    @property
    def not_(self):
        return self

    def in_(self, *a):
        return self

    def eq(self, *a):
        return self

    def is_(self, *a):
        return self

    def gte(self, *a):
        return self

    def lt(self, *a):
        return self

    def order(self, *a):
        return self

    def range(self, lo, hi):
        return self

    def upsert(self, payload, **kwargs):
        self._op = "upsert"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        self._sb.calls.append(
            SimpleNamespace(table=self._table, op=self._op, payload=self._payload)
        )
        responder = self._sb.responses.get((self._table, self._op), [])
        data = responder(self._payload) if callable(responder) else responder
        return _FakeResult(data)


class _FakeSupabase:
    def __init__(self, responses):
        self.calls = []
        self.responses = responses

    def table(self, name):
        return _FakeQuery(self, name)


def _calls(fake, table, op):
    return [c for c in fake.calls if c.table == table and c.op == op]


def _echo_claims(payload):
    return [{"id": f"D{i}", "user_id": row["user_id"]} for i, row in enumerate(payload)]


# Tuesday 2026-08-25 06:04 PDT == 13:04 UTC — inside the send window.
NOW = datetime(2026, 8, 25, 13, 4, tzinfo=timezone.utc)

PROJECT = {
    "id": "p1",
    "name": "Anita Gelato TI",
    "number": "7001",
    "internal_bid_at": "2026-08-25T19:00:00Z",
    "current_stage": "receive_quotes",
}
PROFILES = [
    {"id": "u-admin", "full_name": "Pat Admin", "email": "pat@g3.com", "role": "estimating_admin"},
    {"id": "u-exec", "full_name": "Alex Exec", "email": "alex@g3.com", "role": "executive"},
    {"id": "u-acct", "full_name": "Ash Books", "email": "ash@g3.com", "role": "accountant"},
    {"id": "u-est", "full_name": "External Est", "email": "est@x.com", "role": "estimator"},
    {"id": "u-noemail", "full_name": "No Email", "email": None, "role": "executive"},
]


def _setup(monkeypatch, responses, now=NOW):
    fake = _FakeSupabase(responses)
    monkeypatch.setattr(dd, "get_supabase", lambda: fake)
    monkeypatch.setattr(dd, "_now", lambda: now)
    monkeypatch.setattr(dd, "get_settings", lambda: SETTINGS)
    sent = []
    monkeypatch.setattr(
        dd.graph_email, "send_mail", lambda **kw: sent.append(kw) or {"id": "log"}
    )
    return fake, sent


def _happy_responses():
    return {
        ("projects", "select"): [PROJECT],
        ("rfqs", "select"): [_rfq(quotes=0)],
        ("labor_reviews", "select"): [],
        ("markups", "select"): [],
        ("profiles", "select"): PROFILES,
        ("due_digest_log", "upsert"): _echo_claims,
    }


def test_poll_once_sends_to_internal_non_accountants(monkeypatch):
    fake, sent = _setup(monkeypatch, _happy_responses())
    dd.poll_once()

    [up] = _calls(fake, "due_digest_log", "upsert")
    assert {r["user_id"] for r in up.payload} == {"u-admin", "u-exec"}
    assert all(r["digest_date"] == "2026-08-25" for r in up.payload)

    assert {kw["to"][0] for kw in sent} == {"pat@g3.com", "alex@g3.com"}
    for kw in sent:
        assert kw["importance"] == "high"
        assert "Bids due today" in kw["subject"]
        assert "Anita Gelato TI" in kw["body_html"]
        assert "https://bdr.example.com/projects/p1" in kw["body_html"]
        # missing quotes → the red row background
        assert dd._ROW_BG["red"] in kw["body_html"]


def test_poll_once_duplicate_tick_sends_nothing(monkeypatch):
    responses = {**_happy_responses(), ("due_digest_log", "upsert"): []}
    fake, sent = _setup(monkeypatch, responses)
    dd.poll_once()
    assert _calls(fake, "due_digest_log", "upsert")
    assert not sent


def test_poll_once_outside_window_touches_nothing(monkeypatch):
    fake, sent = _setup(
        monkeypatch,
        _happy_responses(),
        now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),  # 5:00 AM PDT
    )
    dd.poll_once()
    assert not fake.calls and not sent


def test_poll_once_weekend_touches_nothing(monkeypatch):
    fake, sent = _setup(
        monkeypatch,
        _happy_responses(),
        now=datetime(2026, 8, 29, 13, 30, tzinfo=timezone.utc),  # Saturday
    )
    dd.poll_once()
    assert not fake.calls and not sent


def test_poll_once_no_projects_sends_nothing(monkeypatch):
    responses = {**_happy_responses(), ("projects", "select"): []}
    fake, sent = _setup(monkeypatch, responses)
    dd.poll_once()
    assert not _calls(fake, "due_digest_log", "upsert")
    assert not sent


def test_poll_once_send_failure_releases_claim(monkeypatch):
    fake, _ = _setup(monkeypatch, _happy_responses())

    def _boom(**kw):
        raise RuntimeError("graph down")

    monkeypatch.setattr(dd.graph_email, "send_mail", _boom)
    dd.poll_once()
    # both claims rolled back so the next tick retries
    assert len(_calls(fake, "due_digest_log", "delete")) == 2


def test_render_all_green_row(monkeypatch):
    html_body = dd.render_digest_email(
        recipient_name="Pat Admin",
        date_label="Tuesday, August 25",
        rows=[
            {
                "project": PROJECT,
                "checks": {"has_quotes": True, "has_labor": True, "has_markups": True},
                "severity": "ok",
            }
        ],
        base_url="https://bdr.example.com",
    )
    assert "Hi Pat," in html_body
    assert dd._ROW_BG["ok"] in html_body
    assert "&#10003;" in html_body and "&#10007;" not in html_body
    assert "/bids-today" in html_body
