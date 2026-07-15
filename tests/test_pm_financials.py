"""PM financials — the computed G702/G703 layer and the router's contract rules.

Pure math (services/pm_financials) is tested directly; the router rules (CO /
SOV / pay-app lifecycle, the previous_completed snapshot, the summary) run
against an in-memory fake Supabase (a private copy of the test_reverify builder,
extended with delete/in_, auto-ids, and 23505/23503 emulation so the
unique-violation and RESTRICT-FK translations are exercised end-to-end).
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.schemas import (
    ChangeOrderIn,
    ChangeOrderUpdate,
    PayAppCreate,
    PayAppLineUpdate,
    PayAppUpdate,
    SovLineIn,
)
from app.routers import pm_financials as r
from app.services import pm as pm_service
from app.services import pm_financials as fin


# ── Fake Supabase ─────────────────────────────────────────────────────────────

# (index name, columns) — mirrors the 0059 composite uniques so the router's
# 23505→409 translations are hit by real inserts/updates.
_UNIQUE = {
    "change_orders": ("change_orders_project_id_co_number_key", ("project_id", "co_number")),
    "sov_lines": ("sov_lines_project_id_line_number_key", ("project_id", "line_number")),
    "pay_applications": (
        "pay_applications_project_id_app_number_key",
        ("project_id", "app_number"),
    ),
}


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._neq_filters = []
        self._in_filters = []
        self._order = []
        self._single = False

    # builders
    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def neq(self, col, val):
        self._neq_filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._in_filters.append((col, list(vals)))
        return self

    def order(self, col, desc=False):
        self._order.append((col, desc))
        return self

    def limit(self, *a, **k):
        return self

    def single(self):
        self._single = True
        return self

    # execution
    def _matches(self, row):
        return (
            all(row.get(c) == v for c, v in self._filters)
            and all(row.get(c) != v for c, v in self._neq_filters)
            and all(row.get(c) in v for c, v in self._in_filters)
        )

    def _check_unique(self, rows, candidate, ignore=None):
        if self.table not in _UNIQUE:
            return
        index_name, cols = _UNIQUE[self.table]
        for other in rows:
            if other is ignore:
                continue
            if all(other.get(c) == candidate.get(c) for c in cols):
                raise Exception(
                    f'23505 duplicate key value violates unique constraint "{index_name}"'
                )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r_ for r_ in rows if self._matches(r_)]
            for col, desc in reversed(self._order):
                hits.sort(key=lambda row: row.get(col), reverse=desc)
            if self._single:
                return SimpleNamespace(data=(dict(hits[0]) if hits else None))
            return SimpleNamespace(data=[dict(r_) for r_ in hits])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                p = dict(p)
                p.setdefault("id", self.db.next_id(self.table))
                self._check_unique(rows, p)
                rows.append(p)
                out.append(dict(p))
            return SimpleNamespace(data=out)
        if self._op == "update":
            hits = [r_ for r_ in rows if self._matches(r_)]
            for r_ in hits:  # pre-check so a violation leaves the table untouched
                self._check_unique(rows, {**r_, **self._payload}, ignore=r_)
            out = []
            for r_ in hits:
                r_.update(self._payload)
                out.append(dict(r_))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            hits = [r_ for r_ in rows if self._matches(r_)]
            if self.table == "sov_lines":
                # Emulate the pay_app_lines→sov_lines ON DELETE RESTRICT FK.
                referenced = {
                    ln["sov_line_id"] for ln in self.db.tables.get("pay_app_lines", [])
                }
                if any(r_["id"] in referenced for r_ in hits):
                    raise Exception(
                        '23503 update or delete on table "sov_lines" violates foreign '
                        'key constraint "pay_app_lines_sov_line_id_fkey" on table '
                        '"pay_app_lines"'
                    )
            self.db.tables[self.table] = [r_ for r_ in rows if r_ not in hits]
            return SimpleNamespace(data=[dict(r_) for r_ in hits])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r_) for r_ in v] for k, v in (tables or {}).items()}
        self._seq = 0

    def next_id(self, table):
        self._seq += 1
        return f"{table}-{self._seq}"

    def table(self, name):
        return _Query(self, name)


USER = SimpleNamespace(id="u1")


def _base_db():
    return FakeDB(
        {
            "projects": [
                {"id": "p1", "name": "Job", "pm_stage": "construction", "pm_completed_at": None}
            ],
            "pm_details": [
                {"project_id": "p1", "original_contract_value": "100000", "retainage_percent": "10"}
            ],
        }
    )


def _install(monkeypatch, db):
    audits = []
    monkeypatch.setattr(r, "get_supabase", lambda: db)
    monkeypatch.setattr(pm_service, "get_supabase", lambda: db)
    monkeypatch.setattr(r, "audit", lambda *a, **k: audits.append((a[1], a[4])))
    return audits


# ── Pure math ─────────────────────────────────────────────────────────────────


def test_line_totals():
    t = fin.line_totals(
        {"previous_completed": "100", "this_period": "50", "stored_materials": "25"}, "500"
    )
    assert t["total_completed_and_stored"] == "175.00"
    assert t["percent_complete"] == "35.00"  # 0-100 percent, 2dp
    assert t["balance_to_finish"] == "325.00"


def test_line_totals_zero_scheduled_value():
    t = fin.line_totals(
        {"previous_completed": "0", "this_period": "150", "stored_materials": "0"}, 0
    )
    assert t["percent_complete"] is None
    assert t["balance_to_finish"] == "-150.00"


def test_retainage_rounds_half_up_and_defaults_to_zero():
    assert fin.retainage_of(Decimal("0.05"), "10") == Decimal("0.01")  # not banker's 0.00
    assert fin.retainage_of(Decimal("333.33"), "10") == Decimal("33.33")
    assert fin.retainage_of(Decimal("1000"), None) == Decimal("0.00")


def test_negative_change_order_amounts_sum_signed():
    assert fin.total_of([{"amount": "1000"}, {"amount": "-250.50"}], "amount") == Decimal(
        "749.50"
    )


def test_summary_null_contract_value_stays_null():
    s = fin.financials_summary(None, Decimal("500"), Decimal(0), Decimal(0), Decimal(0), 1, 0)
    assert s["original_contract_value"] is None
    assert s["current_contract_value"] is None
    assert s["balance_to_finish"] is None
    assert s["approved_change_total"] == "500.00"


def test_app_series_chain_skips_rejected():
    apps = [
        {"id": "a1", "app_number": 1, "status": "approved", "retainage_percent": "10"},
        {"id": "a2", "app_number": 2, "status": "rejected", "retainage_percent": "10"},
        {"id": "a3", "app_number": 3, "status": "draft", "retainage_percent": "10"},
    ]
    lines = {
        "a1": [{"previous_completed": "0", "this_period": "1000", "stored_materials": "0"}],
        "a2": [{"previous_completed": "1000", "this_period": "500", "stored_materials": "0"}],
        "a3": [{"previous_completed": "1000", "this_period": "200", "stored_materials": "0"}],
    }
    totals = fin.app_series_totals(apps, lines)
    assert totals["a1"]["current_payment_due"] == "900.00"  # 1000 − 100 retainage
    assert totals["a2"]["previous_certificates"] == "900.00"
    # The rejected app never advances the chain — a3 also builds on a1.
    assert totals["a3"]["previous_certificates"] == "900.00"
    assert totals["a3"]["total_completed_and_stored"] == "1200.00"
    assert totals["a3"]["current_payment_due"] == "180.00"  # 1200 − 120 − 900


# ── Per-project guard ─────────────────────────────────────────────────────────


def test_guard_rejects_missing_or_bid_only_projects(monkeypatch):
    db = FakeDB({"projects": [{"id": "bid1", "name": "Bid", "pm_stage": None}]})
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as e:
        r.list_change_orders("bid1", USER)
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        r.list_change_orders("nope", USER)
    assert e.value.status_code == 404


# ── Change orders ─────────────────────────────────────────────────────────────


def test_co_duplicate_number_is_409(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    co = r.create_change_order("p1", ChangeOrderIn(co_number="CO-1", title="Add"), USER)
    with pytest.raises(HTTPException) as e:
        r.create_change_order("p1", ChangeOrderIn(co_number="CO-1", title="Dup"), USER)
    assert e.value.status_code == 409
    assert "CO number" in e.value.detail

    other = r.create_change_order("p1", ChangeOrderIn(co_number="CO-2", title="Other"), USER)
    with pytest.raises(HTTPException) as e:
        r.update_change_order("p1", other["id"], ChangeOrderUpdate(co_number="CO-1"), USER)
    assert e.value.status_code == 409
    assert db.tables["change_orders"][1]["co_number"] == "CO-2"  # unchanged
    assert co["status"] == "draft"


def test_co_delete_blocked_unless_draft(monkeypatch):
    db = _base_db()
    audits = _install(monkeypatch, db)
    approved = r.create_change_order(
        "p1", ChangeOrderIn(co_number="CO-1", title="Add", status="approved"), USER
    )
    with pytest.raises(HTTPException) as e:
        r.delete_change_order("p1", approved["id"], USER)
    assert e.value.status_code == 409
    assert "contract record" in e.value.detail

    draft = r.create_change_order("p1", ChangeOrderIn(co_number="CO-2", title="Oops"), USER)
    r.delete_change_order("p1", draft["id"], USER)
    assert [c["id"] for c in db.tables["change_orders"]] == [approved["id"]]
    assert ("co.delete", {"co_number": "CO-2"}) in audits


# ── SOV lines ─────────────────────────────────────────────────────────────────


def _sov(monkeypatch, db):
    a = r.create_sov_line(
        "p1", SovLineIn(line_number="1", description="Rough-in", scheduled_value=Decimal("1000")), USER
    )
    b = r.create_sov_line(
        "p1", SovLineIn(line_number="2", description="Trim", scheduled_value=Decimal("500")), USER
    )
    return a, b


def test_sov_change_order_must_belong_to_project(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    db.tables["change_orders"] = [{"id": "co-x", "project_id": "p2", "co_number": "1"}]
    with pytest.raises(HTTPException) as e:
        r.create_sov_line(
            "p1",
            SovLineIn(
                line_number="1", description="X", scheduled_value=Decimal("10"),
                change_order_id="co-x",
            ),
            USER,
        )
    assert e.value.status_code == 400


def test_sov_duplicate_line_number_is_409(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    _sov(monkeypatch, db)
    with pytest.raises(HTTPException) as e:
        r.create_sov_line(
            "p1", SovLineIn(line_number="1", description="Dup", scheduled_value=Decimal("1")), USER
        )
    assert e.value.status_code == 409
    assert "line number" in e.value.detail


def test_sov_delete_blocked_once_billed(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    line_a, line_b = _sov(monkeypatch, db)
    r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 7, 31)), USER)
    with pytest.raises(HTTPException) as e:
        r.delete_sov_line("p1", line_a["id"], USER)
    assert e.value.status_code == 409
    assert "billing history" in e.value.detail


# ── Pay applications ──────────────────────────────────────────────────────────


def _line_id(worksheet, sov_line_id):
    return next(ln["id"] for ln in worksheet["lines"] if ln["sov_line_id"] == sov_line_id)


def test_app_number_sequence_and_default_retainage(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    _sov(monkeypatch, db)
    app1 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 7, 31)), USER)
    app2 = r.create_pay_application(
        "p1", PayAppCreate(period_end=date(2026, 8, 31), retainage_percent=Decimal("5")), USER
    )
    assert (app1["app_number"], app2["app_number"]) == (1, 2)
    assert app1["retainage_percent"] == "10"  # pm_details default
    assert app2["retainage_percent"] == "5"  # explicit wins
    assert app1["status"] == "draft"


def test_previous_completed_snapshot_excludes_rejected(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    line_a, line_b = _sov(monkeypatch, db)

    app1 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 7, 31)), USER)
    assert all(ln["previous_completed"] == "0" for ln in app1["lines"])
    r.update_pay_app_line(
        "p1", app1["id"], _line_id(app1, line_a["id"]),
        PayAppLineUpdate(this_period=Decimal("400")), USER,
    )

    app2 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 8, 31)), USER)
    by_sov = {ln["sov_line_id"]: ln for ln in app2["lines"]}
    assert by_sov[line_a["id"]]["previous_completed"] == "400"
    assert by_sov[line_b["id"]]["previous_completed"] == "0"

    # Bill on app2, then reject it — a new app must NOT inherit its this_period.
    r.update_pay_app_line(
        "p1", app2["id"], _line_id(app2, line_a["id"]),
        PayAppLineUpdate(this_period=Decimal("100")), USER,
    )
    r.update_pay_application("p1", app2["id"], PayAppUpdate(status="rejected"), USER)
    app3 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 9, 30)), USER)
    by_sov = {ln["sov_line_id"]: ln for ln in app3["lines"]}
    assert by_sov[line_a["id"]]["previous_completed"] == "400"


def test_line_edits_only_while_draft(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    line_a, _ = _sov(monkeypatch, db)
    app = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 7, 31)), USER)
    r.update_pay_application("p1", app["id"], PayAppUpdate(status="submitted"), USER)
    with pytest.raises(HTTPException) as e:
        r.update_pay_app_line(
            "p1", app["id"], _line_id(app, line_a["id"]),
            PayAppLineUpdate(this_period=Decimal("1")), USER,
        )
    assert e.value.status_code == 409
    assert "draft" in e.value.detail


def test_pay_app_delete_rules(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    _sov(monkeypatch, db)
    app1 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 7, 31)), USER)
    app2 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 8, 31)), USER)

    with pytest.raises(HTTPException) as e:  # later app exists
        r.delete_pay_application("p1", app1["id"], USER)
    assert e.value.status_code == 409
    assert "Later pay applications" in e.value.detail

    r.delete_pay_application("p1", app2["id"], USER)  # newest draft: fine

    r.update_pay_application("p1", app1["id"], PayAppUpdate(status="submitted"), USER)
    with pytest.raises(HTTPException) as e:  # no longer draft
        r.delete_pay_application("p1", app1["id"], USER)
    assert e.value.status_code == 409


# ── Worksheet + summary, end to end ───────────────────────────────────────────


def test_worksheet_math_and_summary(monkeypatch):
    db = _base_db()
    _install(monkeypatch, db)
    line_a, line_b = _sov(monkeypatch, db)
    r.create_change_order(
        "p1", ChangeOrderIn(co_number="CO-1", title="Add", status="approved",
                            amount=Decimal("5000")), USER,
    )
    r.create_change_order(
        "p1", ChangeOrderIn(co_number="CO-2", title="Deduct", status="approved",
                            amount=Decimal("-1000")), USER,
    )
    r.create_change_order(
        "p1", ChangeOrderIn(co_number="CO-3", title="Pending", amount=Decimal("99")), USER
    )

    app1 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 7, 31)), USER)
    ws1 = r.update_pay_app_line(
        "p1", app1["id"], _line_id(app1, line_a["id"]),
        PayAppLineUpdate(this_period=Decimal("400")), USER,
    )
    assert ws1["work_this_period"] == "400.00"
    assert ws1["retainage_held"] == "40.00"
    assert ws1["previous_certificates"] == "0.00"
    assert ws1["current_payment_due"] == "360.00"
    line = next(ln for ln in ws1["lines"] if ln["sov_line_id"] == line_a["id"])
    assert line["scheduled_value"] == "1000"
    assert line["percent_complete"] == "40.00"
    assert line["balance_to_finish"] == "600.00"

    app2 = r.create_pay_application("p1", PayAppCreate(period_end=date(2026, 8, 31)), USER)
    ws2 = r.update_pay_app_line(
        "p1", app2["id"], _line_id(app2, line_a["id"]),
        PayAppLineUpdate(this_period=Decimal("100")), USER,
    )
    assert ws2["total_completed_and_stored"] == "500.00"
    assert ws2["retainage_held"] == "50.00"
    assert ws2["previous_certificates"] == "360.00"
    assert ws2["current_payment_due"] == "90.00"

    listed = r.list_pay_applications("p1", USER)
    assert [a["app_number"] for a in listed] == [1, 2]
    assert listed[1]["current_payment_due"] == "90.00"

    s = r.get_financials_summary("p1", USER)
    assert s["original_contract_value"] == "100000.00"
    assert s["approved_change_total"] == "4000.00"
    assert s["current_contract_value"] == "104000.00"
    assert s["sov_total"] == "1500.00"
    assert s["billed_to_date"] == "500.00"
    assert s["retainage_held"] == "50.00"
    assert s["balance_to_finish"] == "103500.00"
    assert s["open_change_order_count"] == 1
    assert s["pay_app_count"] == 2
