"""Quote notes (0137): per-quote commentary on Receive Quotes / Select Vendors.

Pinned here:
  - the per-category read lists every quote on the RFQ with its entry note
    (quotes.notes) and its notes thread, oldest first, internal users only;
  - adding needs a writer role (the accountant reads, never writes), trims the
    body, and 404s a quote that is not on the RFQ or an RFQ from another project;
  - removing is for the author or the Executive / IT Admin;
  - note_count rides on both quote lists (Receive Quotes and Select Vendors),
    entry note included, from ONE extra query per request;
  - no note operation ever bounces a verified bid (notes are words, not prices).

Handlers are called directly against the in-memory fake, so `Depends` never
runs; the role gate is asserted on the route's declared dependencies instead.
"""

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.deps import CurrentUser, require_writer
from app.core.roles import Role
from app.models.schemas import QuoteNoteIn
from app.routers import rfqs as rfqs_router
from tests.test_workflow import FakeDB

PID = "p1"


def _user(role=Role.ESTIMATING_ENGINEER_MATERIALS, uid="u1"):
    return CurrentUser(id=uid, email="mats@g3.com", role=role, is_active=True)


def _quote(qid, rfq_id, amount, *, notes=None, origin="vendor", vendor=None):
    return {
        "id": qid,
        "rfq_id": rfq_id,
        "amount": amount,
        "origin": origin,
        "source": "manual",
        "is_approved": True,
        "is_selected": False,
        "tax_included": True,
        "tax_rate": "8.375",
        "received_at": None,
        "notes": notes,
        "quote_file_id": None,
        "vendors": {"name": vendor} if vendor else None,
    }


def _note(nid, qid, body, author_id="u1", created_at="2026-09-01T00:00:00Z"):
    return {
        "id": nid,
        "quote_id": qid,
        "body": body,
        "author_id": author_id,
        "created_at": created_at,
        # What the PostgREST author join resolves to; the fake returns rows as-is.
        "author": {"full_name": "Mats Engineer", "role": "estimating_engineer_materials"},
    }


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(rfqs_router, "audit", lambda *a, **k: seen.append(a))
    return seen


@pytest.fixture
def bounces(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(rfqs_router, "dismiss_notifications", lambda **k: None)
    monkeypatch.setattr(
        rfqs_router.workflow,
        "maybe_reopen_verify_after_edit",
        lambda pid, uid, reason, stale: seen.append(reason),
    )
    return seen


@pytest.fixture
def db(monkeypatch, audits, bounces):
    database = FakeDB(
        {
            "rfqs": [
                {
                    "id": "r1",
                    "project_id": PID,
                    "status": "quotes_in",
                    "material_category_id": "mc1",
                    "material_categories": {
                        "name": "Fixtures",
                        "is_general": False,
                        "pricing_section": "materials",
                        "sort_order": 10,
                    },
                },
                {
                    "id": "r-other",
                    "project_id": "p2",
                    "status": "quotes_in",
                    "material_category_id": "mc1",
                    "material_categories": {"name": "Fixtures", "is_general": False},
                },
            ],
            "quotes": [
                _quote("q-hi", "r1", "1200", vendor="Acme"),
                _quote("q-lo", "r1", "900", vendor="Brite"),
                _quote("q-man", "r1", "1000", origin="manual", notes="  phone price  "),
                _quote("q-else", "r-other", "50", vendor="Elsewhere"),
            ],
            "quote_notes": [
                _note("n2", "q-hi", "lead time 12 weeks", created_at="2026-09-02T00:00:00Z"),
                _note("n1", "q-hi", "excludes fixtures", created_at="2026-09-01T00:00:00Z"),
            ],
        }
    )
    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: database)
    return database


def _post(body, quote_id="q-lo", rfq_id="r1", project_id=PID, user=None):
    return rfqs_router.add_quote_note(
        project_id, rfq_id, quote_id, QuoteNoteIn(body=body), user or _user()
    )


def _routes():
    return {(m, r.path): r for r in rfqs_router.router.routes for m in r.methods}


# ── Read ─────────────────────────────────────────────────────────────────────


def test_modal_read_lists_every_quote_with_entry_note_and_thread(db):
    out = rfqs_router.list_quote_notes(PID, "r1", _user())

    assert out["category_name"] == "Fixtures"
    # Cheapest first on the tax-inclusive total, like Select Vendors; the other
    # project's quote never appears.
    assert [q["id"] for q in out["quotes"]] == ["q-lo", "q-man", "q-hi"]
    by_id = {q["id"]: q for q in out["quotes"]}
    assert [n["id"] for n in by_id["q-hi"]["quote_notes"]] == ["n1", "n2"]  # oldest first
    assert by_id["q-hi"]["quote_notes"][0]["author"]["full_name"] == "Mats Engineer"
    assert by_id["q-hi"]["vendor_name"] == "Acme"
    assert by_id["q-hi"]["note_count"] == 2
    assert by_id["q-man"]["entry_note"] == "phone price"
    assert by_id["q-man"]["note_count"] == 1
    assert by_id["q-lo"]["quote_notes"] == [] and by_id["q-lo"]["note_count"] == 0


def test_accountant_may_read_estimator_may_not(db):
    assert rfqs_router.list_quote_notes(PID, "r1", _user(Role.ACCOUNTANT))["quotes"]
    with pytest.raises(HTTPException) as exc:
        rfqs_router.list_quote_notes(PID, "r1", _user(Role.ESTIMATOR))
    assert exc.value.status_code == 403


def test_read_of_another_projects_rfq_is_404(db):
    with pytest.raises(HTTPException) as exc:
        rfqs_router.list_quote_notes(PID, "r-other", _user())
    assert exc.value.status_code == 404


# ── Add ──────────────────────────────────────────────────────────────────────


def test_add_trims_records_author_and_audits(db, audits, bounces):
    row = _post("  excludes lamps  ")

    assert row["body"] == "excludes lamps"
    assert row["author_id"] == "u1" and row["quote_id"] == "q-lo"
    assert db.tables["quote_notes"][-1]["body"] == "excludes lamps"
    assert audits[-1][1] == "quote.note_add" and audits[-1][3] == "q-lo"
    assert bounces == []  # a note moves no price


@pytest.mark.parametrize("body", ["", "   ", "x" * 2001])
def test_add_rejects_blank_and_oversized_bodies(body):
    with pytest.raises(ValidationError):
        QuoteNoteIn(body=body)


def test_add_to_a_quote_not_on_the_rfq_is_404(db):
    with pytest.raises(HTTPException) as exc:
        _post("hi", quote_id="q-else")
    assert exc.value.status_code == 404
    assert len(db.tables["quote_notes"]) == 2


def test_add_through_another_projects_path_is_404(db):
    with pytest.raises(HTTPException) as exc:
        _post("hi", quote_id="q-else", rfq_id="r-other")
    assert exc.value.status_code == 404


def test_writes_are_writer_only_so_the_accountant_gets_403():
    routes = _routes()
    for key in (
        ("POST", "/projects/{project_id}/rfqs/{rfq_id}/quotes/{quote_id}/notes"),
        ("DELETE", "/projects/{project_id}/rfqs/{rfq_id}/quotes/{quote_id}/notes/{note_id}"),
    ):
        calls = {d.call for d in routes[key].dependant.dependencies}
        assert require_writer in calls, key
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_writer(_user(Role.ACCOUNTANT)))
    assert exc.value.status_code == 403


# ── Delete ───────────────────────────────────────────────────────────────────


def test_author_can_delete_own_note(db, audits, bounces):
    rfqs_router.delete_quote_note(PID, "r1", "q-hi", "n1", _user())

    assert [n["id"] for n in db.tables["quote_notes"]] == ["n2"]
    assert audits[-1][1] == "quote.note_delete"
    assert bounces == []


def test_someone_elses_note_is_403_for_an_engineer(db):
    with pytest.raises(HTTPException) as exc:
        rfqs_router.delete_quote_note(PID, "r1", "q-hi", "n1", _user(uid="u2"))
    assert exc.value.status_code == 403
    assert len(db.tables["quote_notes"]) == 2


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.IT_ADMIN])
def test_executive_and_it_admin_may_remove_any_note(db, role):
    rfqs_router.delete_quote_note(PID, "r1", "q-hi", "n1", _user(role, uid="boss"))
    assert [n["id"] for n in db.tables["quote_notes"]] == ["n2"]


def test_delete_of_a_note_on_another_quote_is_404(db):
    with pytest.raises(HTTPException) as exc:
        rfqs_router.delete_quote_note(PID, "r1", "q-lo", "n1", _user())
    assert exc.value.status_code == 404
    assert len(db.tables["quote_notes"]) == 2


# ── note_count on the quote lists ────────────────────────────────────────────


def test_receive_quotes_list_carries_note_count(db):
    quotes = rfqs_router.list_quotes(PID, "r1", _user())["quotes"]
    assert {q["id"]: q["note_count"] for q in quotes} == {"q-hi": 2, "q-lo": 0, "q-man": 1}


def test_vendor_selection_candidates_carry_note_count(db):
    _post("new one", quote_id="q-lo")
    cats = rfqs_router.get_vendor_selection(PID, _user())
    counts = {c["id"]: c["note_count"] for c in cats[0]["quotes"]}
    assert counts == {"q-hi": 2, "q-lo": 1, "q-man": 1}


def test_note_count_costs_one_query_per_request(db, monkeypatch):
    calls: list[str] = []
    real = db.table

    def spy(name):
        calls.append(name)
        return real(name)

    monkeypatch.setattr(db, "table", spy)
    rfqs_router.get_vendor_selection(PID, _user())
    assert calls.count("quote_notes") == 1
