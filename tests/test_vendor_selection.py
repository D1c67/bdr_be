"""Select Vendors: approval, selection, the advance gate, and what any of it
prices.

Four rules are pinned here, because between them they replace the whole old
price-precedence chain (custom_amount > selected quote > lowest quote):

  1. SELECTION IS THE PRICE. A category is priced by the quote a human picked,
     or it has no price. The cheapest quote on the table prices nothing.
  2. ONLY AN APPROVED QUOTE CAN WIN, and a quote cannot be approved until its
     sales-tax question is answered. Withdrawing approval from the winner
     withdraws the price with it.
  3. General Material is a category like any other: its estimate figure is a
     candidate that has to be picked, and it can have a winner.
  4. The gate that used to sit on Receive Quotes now sits on Select Vendors, so
     a bid is gated on having chosen a price everywhere, NOT on every vendor
     having replied. Select Vendors completes with the material lane still live.

Everything runs against the composite-key-aware in-memory Supabase fake from
tests/test_workflow.py. The handlers are called directly, so `Depends` (auth,
rate limits) never runs and only the logic under test does.
"""

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import QuoteApprovalIn, TransitionIn
from app.routers import pricing as pricing_router
from app.routers import rfqs as rfqs_router
from app.routers import workflow as wf_router
from app.services import vendor_selection, workflow
from tests.test_workflow import FakeDB

PID = "p1"


def _user(role=Role.ESTIMATING_ENGINEER_MATERIALS, uid="u1"):
    return CurrentUser(id=uid, email="mats@g3.com", role=role, is_active=True)


# ── Fixture data ──────────────────────────────────────────────────────────────
# Three categories across two pricing sections, so a section can be ready while
# its neighbour is not:
#
#   General Material (materials)  r-gen   estimate candidate, approved
#   Fixtures         (materials)  r-fix   one hand-entered candidate, approved
#   Switchgear       (gear)       r-gear  two vendor quotes, one still unapproved


def _rfq(rfq_id, name, section, sort_order, *, is_general=False, status="sent"):
    return {
        "id": rfq_id,
        "project_id": PID,
        "status": status,
        "material_category_id": f"mc-{rfq_id}",
        "material_categories": {
            "name": name,
            "is_general": is_general,
            "pricing_section": section,
            "sort_order": sort_order,
        },
    }


def _quote(
    qid,
    rfq_id,
    amount,
    *,
    origin="vendor",
    approved=True,
    selected=False,
    tax_included=True,
    tax_rate="8.375",
    received_at=None,
):
    return {
        "id": qid,
        "rfq_id": rfq_id,
        "amount": amount,
        "origin": origin,
        "is_approved": approved,
        "is_selected": selected,
        "tax_included": tax_included,
        "tax_rate": tax_rate,
        "received_at": received_at,
        "notes": None,
        "quote_file_id": None,
        "source": "manual",
    }


RFQS = [
    _rfq("r-gen", "General Material", "materials", 10, is_general=True, status="draft"),
    _rfq("r-gear", "Switchgear", "gear", 20),
    _rfq("r-fix", "Fixtures", "materials", 30),
]

QUOTES = [
    # General Material's wiring figure off the estimate: a candidate, not an answer.
    _quote("q-gen-est", "r-gen", "500", origin="estimate"),
    # Switchgear: the cheaper of the two has not been signed off yet.
    _quote("q-gear-hi", "r-gear", "1000", tax_included=False, tax_rate="10",
           received_at="2026-07-01T00:00:00Z"),
    _quote("q-gear-lo", "r-gear", "900", approved=False,
           received_at="2026-07-02T00:00:00Z"),
    # Fixtures: a price someone was given over the phone.
    _quote("q-fix-man", "r-fix", "250", origin="manual"),
]

# The six lanes, defaulted to a bid whose material lane is still taking quotes
# while Select Vendors is soft-open (exactly the state the restructure exists for).
_LANES = {
    "intake": ("to_estimator", "complete"),
    "material_numbers": ("receive_quotes", "active"),
    "labor_numbers": ("labor_numbers", "active"),
    "select_vendors": ("select_vendors", "active"),
    "markup": ("markup", "active"),
    "send_out": ("gc_pricing", "locked"),
}


def _lane_rows(**overrides):
    spec = {**_LANES, **overrides}
    return [
        {
            "project_id": PID,
            "category": cat,
            "current_task": task,
            "status": st,
            "owner_role": None,
            "completed_at": "2026-07-01T00:00:00Z" if st == "complete" else None,
        }
        for cat, (task, st) in spec.items()
    ]


def _db(*, rfqs=None, quotes=None, lanes=None, **tables):
    return FakeDB(
        {
            "projects": [
                {
                    "id": PID,
                    "current_stage": "receive_quotes",
                    "abandoned_at": None,
                    "current_owner_role": None,
                }
            ],
            "rfqs": RFQS if rfqs is None else rfqs,
            "quotes": QUOTES if quotes is None else quotes,
            "project_category_state": _lane_rows() if lanes is None else lanes,
            "stage_events": [],
            "project_files": [],
            "general_material_estimates": [],
            "labor_reviews": [],
            "audit_log": [],
            **tables,
        }
    )


def _row(db, table, row_id):
    return next(r for r in db.tables[table] if r["id"] == row_id)


@pytest.fixture
def bounces(monkeypatch):
    """Point the RFQ router at nothing but the fake, and record the re-verify
    bounces so a test can assert which edits actually moved a price."""
    seen: list[str] = []
    monkeypatch.setattr(rfqs_router, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfqs_router, "dismiss_notifications", lambda **k: None)
    monkeypatch.setattr(
        rfqs_router.workflow,
        "maybe_reopen_verify_after_edit",
        lambda pid, uid, reason: seen.append(reason),
    )
    return seen


@pytest.fixture
def db(monkeypatch, bounces):
    database = _db()
    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: database)
    monkeypatch.setattr(vendor_selection, "get_supabase", lambda: database)
    monkeypatch.setattr(pricing_router, "get_supabase", lambda: database)
    return database


# ══ 1. Approval ═══════════════════════════════════════════════════════════════
# Approving a quote says two things at once: the amount on the row is the amount
# that was quoted, and the sales-tax question has been answered.


def _approve(rfq_id, quote_id, approved=True, project_id=PID):
    return rfqs_router.set_quote_approval(
        project_id, rfq_id, quote_id, QuoteApprovalIn(approved=approved), _user()
    )


def test_cannot_approve_a_quote_whose_tax_question_is_unanswered(db):
    db.tables["quotes"].append(
        _quote("q-untaxed", "r-gear", "700", approved=False, tax_included=None)
    )
    with pytest.raises(HTTPException) as exc:
        _approve("r-gear", "q-untaxed")

    assert exc.value.status_code == 409
    assert "sales tax" in exc.value.detail
    # Refused before mutating: approving is the attestation, so a half-done one
    # must not exist.
    assert _row(db, "quotes", "q-untaxed")["is_approved"] is False


def test_approval_records_who_signed_it_off(db):
    updated = _approve("r-gear", "q-gear-lo")

    assert updated["is_approved"] is True
    assert updated["approved_by"] == "u1"
    assert updated["approved_at"] is not None


def test_withdrawing_approval_from_the_winner_withdraws_the_price(db, bounces):
    _row(db, "quotes", "q-gear-hi")["is_selected"] = True

    updated = _approve("r-gear", "q-gear-hi", approved=False)

    # A number nobody stands behind cannot be left standing as the price.
    assert updated["is_approved"] is False
    assert updated["is_selected"] is False
    assert updated["approved_by"] is None
    assert "Switchgear" in vendor_selection.categories_without_a_price(PID)
    # And that DID move a price, so a committed bid has to be re-verified.
    assert bounces == ["Winning quote's approval withdrawn"]


def test_withdrawing_approval_from_a_loser_leaves_the_winner_standing(db, bounces):
    _row(db, "quotes", "q-gear-hi")["is_selected"] = True
    _approve("r-gear", "q-gear-lo")  # sign off the other candidate

    _approve("r-gear", "q-gear-lo", approved=False)

    assert _row(db, "quotes", "q-gear-hi")["is_selected"] is True
    assert bounces == []  # no price moved, so no re-verify bounce


def test_withdrawing_approval_is_never_blocked_by_the_tax_question(db):
    # The 409 guards the attestation, not its retraction: a quote whose tax
    # answer was cleared must still be un-approvable rather than stuck approved.
    db.tables["quotes"].append(
        _quote("q-odd", "r-gear", "700", approved=True, tax_included=None)
    )
    assert _approve("r-gear", "q-odd", approved=False)["is_approved"] is False


def test_approval_cannot_reach_another_project(db):
    db.tables["rfqs"].append(
        {**_rfq("r-other", "Switchgear", "gear", 20), "project_id": "p2"}
    )
    db.tables["quotes"].append(_quote("q-other", "r-other", "10"))

    with pytest.raises(HTTPException) as exc:
        _approve("r-other", "q-other")
    assert exc.value.status_code == 404


def test_approval_of_a_quote_on_a_different_rfq_404s(db):
    with pytest.raises(HTTPException) as exc:
        _approve("r-gear", "q-fix-man")  # right project, wrong category
    assert exc.value.status_code == 404


# ══ 2. Selection ══════════════════════════════════════════════════════════════


def _select(rfq_id, quote_id, project_id=PID):
    return rfqs_router.select_quote(project_id, rfq_id, quote_id, _user())


def test_only_an_approved_quote_can_be_selected(db, bounces):
    _row(db, "quotes", "q-gear-hi")["is_selected"] = True

    with pytest.raises(HTTPException) as exc:
        _select("r-gear", "q-gear-lo")  # cheaper, but nobody has checked it

    assert exc.value.status_code == 409
    assert "Approve this quote" in exc.value.detail
    # Validated before mutating: the refused pick must not have cleared the
    # standing winner on its way to the 409.
    assert _row(db, "quotes", "q-gear-hi")["is_selected"] is True
    assert bounces == []


def test_approving_then_selecting_is_the_two_step(db):
    _approve("r-gear", "q-gear-lo")
    assert _select("r-gear", "q-gear-lo")["is_selected"] is True


def test_general_material_can_have_a_winner(db, bounces):
    # The old rule refused every General Material selection outright: its price
    # came off the estimate and nothing could be picked. Now the estimate figure
    # is a candidate on the same footing as any other.
    updated = _select("r-gen", "q-gen-est")

    assert updated["is_selected"] is True
    assert updated["origin"] == "estimate"
    assert "General Material" not in vendor_selection.categories_without_a_price(PID)
    assert bounces == ["Quote selection changed"]


def test_a_hand_entered_candidate_wins_like_any_other(db):
    assert _select("r-fix", "q-fix-man")["is_selected"] is True


def test_selecting_is_exclusive_within_the_category(db):
    _approve("r-gear", "q-gear-lo")
    _select("r-gear", "q-gear-lo")
    _select("r-gear", "q-gear-hi")  # change of mind

    assert _row(db, "quotes", "q-gear-hi")["is_selected"] is True
    assert _row(db, "quotes", "q-gear-lo")["is_selected"] is False
    assert sum(1 for q in db.tables["quotes"] if q["is_selected"]) == 1


def test_selecting_does_not_disturb_another_category(db):
    _select("r-fix", "q-fix-man")
    _select("r-gear", "q-gear-hi")

    assert _row(db, "quotes", "q-fix-man")["is_selected"] is True


def _clear(rfq_id, project_id=PID):
    return rfqs_router.clear_selected_quote(project_id, rfq_id, _user())


def test_clearing_a_winner_un_prices_the_category(db, bounces):
    _select("r-gear", "q-gear-hi")
    assert "Switchgear" not in vendor_selection.categories_without_a_price(PID)

    _clear("r-gear")

    # Back to undecided: no winner, and therefore no price at all. There is no
    # fallback to the cheapest quote to catch it.
    assert _row(db, "quotes", "q-gear-hi")["is_selected"] is False
    assert "Switchgear" in vendor_selection.categories_without_a_price(PID)
    # Removing a price is a pricing edit, so a past-Verify bid must bounce back.
    assert bounces == ["Quote selection changed", "Winning quote cleared"]


def test_clearing_an_undecided_category_is_a_no_op(db, bounces):
    _clear("r-gear")  # nothing was ever picked

    assert not any(q["is_selected"] for q in db.tables["quotes"] if q["rfq_id"] == "r-gear")
    # Idempotent, and specifically it must NOT bounce a verified bid for a
    # clear that changed nothing.
    assert bounces == []


def test_clearing_leaves_other_categories_alone(db):
    _select("r-fix", "q-fix-man")
    _select("r-gear", "q-gear-hi")

    _clear("r-gear")

    assert _row(db, "quotes", "q-fix-man")["is_selected"] is True


def test_clearing_a_category_in_another_project_404s(db):
    with pytest.raises(HTTPException) as exc:
        _clear("r-other")
    assert exc.value.status_code == 404


def test_selecting_an_unknown_quote_404s(db):
    with pytest.raises(HTTPException) as exc:
        _select("r-gear", "no-such-quote")
    assert exc.value.status_code == 404


# ══ 3. Which categories have a price (services/vendor_selection) ══════════════


def test_a_category_with_no_winner_has_no_price(db):
    # Nothing has been picked anywhere, so nothing on the project is priced.
    # (Sorted: the row order the database hands back is not part of the contract.)
    assert sorted(vendor_selection.categories_without_a_price(PID)) == [
        "Fixtures",
        "General Material",
        "Switchgear",
    ]


def test_the_cheapest_quote_on_the_table_prices_nothing(db):
    # Two approved candidates sitting on Switchgear, neither picked. Under the
    # old chain the lowest one silently became the price; now the category is
    # simply undecided, which is what holds the step open.
    _approve("r-gear", "q-gear-lo")
    assert "Switchgear" in vendor_selection.categories_without_a_price(PID)


def test_picking_a_winner_prices_the_category(db):
    _select("r-gear", "q-gear-hi")
    assert "Switchgear" not in vendor_selection.categories_without_a_price(PID)


def test_a_project_with_no_rfqs_has_nothing_outstanding(monkeypatch):
    empty = _db(rfqs=[], quotes=[])
    monkeypatch.setattr(vendor_selection, "get_supabase", lambda: empty)
    assert vendor_selection.categories_without_a_price(PID) == []
    assert vendor_selection.section_readiness(PID) == {}


def test_section_readiness_names_the_categories_it_is_waiting_on(db):
    _select("r-gen", "q-gen-est")  # materials: one of two decided
    _select("r-gear", "q-gear-hi")  # gear: the only one, decided

    readiness = vendor_selection.section_readiness(PID)
    assert readiness["gear"] == {"ready": True, "waiting_on": []}
    assert readiness["materials"] == {"ready": False, "waiting_on": ["Fixtures"]}


def test_a_section_is_ready_once_every_category_feeding_it_is_decided(db):
    for rfq_id, quote_id in (("r-gen", "q-gen-est"), ("r-fix", "q-fix-man")):
        _select(rfq_id, quote_id)

    readiness = vendor_selection.section_readiness(PID)
    assert readiness["materials"]["ready"] is True
    assert readiness["gear"] == {"ready": False, "waiting_on": ["Switchgear"]}


# ══ 4. The Select Vendors advance gate ════════════════════════════════════════


@pytest.fixture
def advance_env(monkeypatch, db):
    monkeypatch.setattr(wf_router, "get_supabase", lambda: db)
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)
    monkeypatch.setattr(workflow.notifications, "dismiss_notifications", lambda **k: None)
    monkeypatch.setattr(wf_router, "notify_role", lambda *a, **k: None)
    return db


def _advance(category, role=Role.ESTIMATING_ENGINEER_MATERIALS):
    return wf_router.advance(PID, TransitionIn(category=category), _user(role))


def _lane(db, category):
    return next(
        r for r in db.tables["project_category_state"] if r["category"] == category
    )


def test_select_vendors_refuses_while_a_category_has_no_winner(advance_env):
    _select("r-gen", "q-gen-est")
    _select("r-gear", "q-gear-hi")  # Fixtures still undecided

    with pytest.raises(HTTPException) as exc:
        _advance("select_vendors")

    assert exc.value.status_code == 409
    assert "Fixtures" in exc.value.detail
    assert _lane(advance_env, "select_vendors")["status"] == "active"


def test_select_vendors_completes_once_every_category_has_a_winner(advance_env):
    for rfq_id, quote_id in (
        ("r-gen", "q-gen-est"),
        ("r-gear", "q-gear-hi"),
        ("r-fix", "q-fix-man"),
    ):
        _select(rfq_id, quote_id)

    _advance("select_vendors")

    assert _lane(advance_env, "select_vendors")["status"] == "complete"


def test_select_vendors_completes_with_the_material_lane_still_taking_quotes(advance_env):
    """The whole point of the restructure. Switchgear's second vendor has not
    replied and Receive Quotes is nowhere near finished, but every category has a
    chosen price, so the bid may move on. Waiting on the last vendor is optional;
    having picked a number everywhere is not."""
    for rfq_id, quote_id in (
        ("r-gen", "q-gen-est"),
        ("r-gear", "q-gear-hi"),
        ("r-fix", "q-fix-man"),
    ):
        _select(rfq_id, quote_id)
    assert _lane(advance_env, "material_numbers")["status"] == "active"

    _advance("select_vendors")

    assert _lane(advance_env, "select_vendors")["status"] == "complete"
    # Still open for the outstanding vendors, and still an unapproved quote on
    # the table: neither has any say in whether the bid proceeds.
    assert _lane(advance_env, "material_numbers")["current_task"] == "receive_quotes"
    assert _lane(advance_env, "material_numbers")["status"] == "active"
    assert _row(advance_env, "quotes", "q-gear-lo")["is_approved"] is False


def test_the_winner_gate_binds_select_vendors_only(advance_env):
    # Receive Quotes used to carry this gate. It must not carry it any more, or
    # the material lane would be stuck behind decisions that belong downstream.
    assert vendor_selection.categories_without_a_price(PID)  # nothing decided

    _advance("material_numbers")

    assert _lane(advance_env, "material_numbers")["status"] == "complete"


def test_a_locked_select_vendors_lane_is_refused_before_the_price_check(monkeypatch, db):
    monkeypatch.setattr(wf_router, "get_supabase", lambda: db)
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)
    db.tables["project_category_state"] = _lane_rows(
        intake=("intake", "active"),
        material_numbers=("estimate_received", "locked"),
        select_vendors=("select_vendors", "locked"),
    )
    with pytest.raises(HTTPException) as exc:
        _advance("select_vendors")
    assert exc.value.status_code == 409
    assert "not active" in exc.value.detail


# ══ 5. What the selection prices (routers/pricing) ════════════════════════════


def _rows_by_category(db):
    return {r["category_name"]: r for r in pricing_router._materials_rows(PID)}


def test_an_undecided_category_carries_no_amount(db):
    rows = _rows_by_category(db)
    assert rows["Switchgear"]["amount"] is None
    assert rows["Switchgear"]["source"] == "none"
    # Not "fall back to the cheapest": both Switchgear quotes are ignored.
    assert pricing_router._materials_total(PID) == Decimal(0)


def test_the_retired_custom_amount_column_prices_nothing(db):
    # rfqs.custom_amount is still in the database (0103 left it behind so the
    # migration stays auditable) and it used to OUTRANK every quote received.
    # Nothing may read it as a price again: the category is unpriced until a
    # human picks a candidate, and then it is the candidate's number that lands.
    _row(db, "rfqs", "r-gear")["custom_amount"] = "99999.00"

    assert _rows_by_category(db)["Switchgear"]["amount"] is None
    assert "Switchgear" in vendor_selection.categories_without_a_price(PID)

    _select("r-gear", "q-gear-hi")
    assert _rows_by_category(db)["Switchgear"]["amount"] == "1100.00"


def test_the_winner_prices_the_category_tax_inclusive(db):
    _select("r-gear", "q-gear-hi")  # 1000 pre-tax, tax NOT included, at 10%

    row = _rows_by_category(db)["Switchgear"]
    assert row["amount"] == "1100.00"
    assert row["pre_tax_amount"] == "1000"
    assert row["tax_amount"] == "100.00"
    assert row["source"] == "vendor"
    assert pricing_router._materials_total(PID) == Decimal("1100.00")


def test_a_tax_inclusive_winner_is_carried_as_quoted(db):
    _select("r-fix", "q-fix-man")

    row = _rows_by_category(db)["Fixtures"]
    assert (row["amount"], row["tax_amount"], row["source"]) == ("250", "0.00", "manual")


def test_the_estimate_candidate_prices_general_material_once_picked(db):
    assert _rows_by_category(db)["General Material"]["amount"] is None
    _select("r-gen", "q-gen-est")
    row = _rows_by_category(db)["General Material"]
    assert (row["amount"], row["source"]) == ("500", "estimate")


def test_price_basis_reports_per_section_readiness(db):
    _select("r-gear", "q-gear-hi")

    basis = pricing_router.get_price_basis(PID, _user())

    assert basis["sections"]["gear"]["ready"] is True
    assert basis["sections"]["gear"]["waiting_on"] == []
    assert basis["sections"]["materials"]["ready"] is False
    assert sorted(basis["sections"]["materials"]["waiting_on"]) == [
        "Fixtures",
        "General Material",
    ]
    # A section that is not on the project has no markup box to unlock.
    assert basis["sections"]["underground"]["present"] is False
    assert basis["sections"]["underground"]["ready"] is False
    assert basis["sections"]["underground"]["waiting_on"] == []


def test_price_basis_labor_readiness_tracks_the_labor_figure(db):
    assert pricing_router.get_price_basis(PID, _user())["labor_ready"] is False

    # A review row that exists but carries no figure is not a labor price.
    db.tables["labor_reviews"].append({"project_id": PID, "labor_amount": None})
    assert pricing_router.get_price_basis(PID, _user())["labor_ready"] is False

    db.tables["labor_reviews"][0]["labor_amount"] = "40000"
    basis = pricing_router.get_price_basis(PID, _user())
    assert basis["labor_ready"] is True
    assert basis["labor_amount"] == "40000"


def test_price_basis_readiness_matches_the_advance_gate(db):
    # The lock the Markup page draws and the gate /advance enforces come from one
    # function; if they ever diverge a user is locked out of work they may do, or
    # let into work they may not.
    for rfq_id, quote_id in (
        ("r-gen", "q-gen-est"),
        ("r-gear", "q-gear-hi"),
        ("r-fix", "q-fix-man"),
    ):
        _select(rfq_id, quote_id)

    basis = pricing_router.get_price_basis(PID, _user())
    assert vendor_selection.categories_without_a_price(PID) == []
    assert all(basis["sections"][k]["ready"] for k in ("materials", "gear"))


# ══ 6. GET /vendor-selection (the Select Vendors page's single read) ══════════


def test_vendor_selection_lists_every_category_in_display_order(db):
    entries = rfqs_router.get_vendor_selection(PID, _user())
    assert [e["category_name"] for e in entries] == [
        "General Material",
        "Switchgear",
        "Fixtures",
    ]
    assert [e["pricing_section"] for e in entries] == ["materials", "gear", "materials"]
    general = entries[0]
    assert general["is_general"] is True
    assert general["was_sent"] is False  # still a draft RFQ, nobody has answered
    assert entries[1]["was_sent"] is True


def test_vendor_selection_orders_candidates_by_tax_inclusive_total(db):
    # q-gear-hi is 1000 + 10% = 1100; q-gear-lo is 900 with tax already in. The
    # raw amounts would sort the same way here, but the comparison basis is the
    # true cost, and an approval flag never reorders the table.
    gear = next(e for e in rfqs_router.get_vendor_selection(PID, _user())
                if e["category_name"] == "Switchgear")
    assert [q["id"] for q in gear["quotes"]] == ["q-gear-lo", "q-gear-hi"]
    assert [q["total"] for q in gear["quotes"]] == ["900", "1100.00"]
    assert [q["is_approved"] for q in gear["quotes"]] == [False, True]
    assert gear["selected_quote_id"] is None


def test_vendor_selection_reports_the_winner(db):
    _select("r-gear", "q-gear-hi")
    gear = next(e for e in rfqs_router.get_vendor_selection(PID, _user())
                if e["category_name"] == "Switchgear")
    assert gear["selected_quote_id"] == "q-gear-hi"
    assert [q["is_selected"] for q in gear["quotes"]] == [False, True]


def test_vendor_selection_labels_candidates_with_no_vendor_behind_them(db):
    entries = {e["category_name"]: e for e in rfqs_router.get_vendor_selection(PID, _user())}
    [manual] = entries["Fixtures"]["quotes"]
    assert (manual["origin"], manual["vendor_name"]) == ("manual", None)
    [estimate] = entries["General Material"]["quotes"]
    assert (estimate["origin"], estimate["vendor_name"]) == ("estimate", None)


def test_vendor_selection_is_internal_only(db):
    with pytest.raises(HTTPException) as exc:
        rfqs_router.get_vendor_selection(PID, _user(role=Role.ESTIMATOR))
    assert exc.value.status_code == 403


def test_the_quotes_list_carries_no_category_level_price(db):
    # The receive-quotes panel used to be handed the category's hand-entered
    # override alongside the quotes. There is no such thing any more: a category
    # holds candidates and nothing else.
    payload = rfqs_router.list_quotes(PID, "r-gear", _user())
    assert set(payload) == {"quotes", "lowest_amount"}
    # Lowest is still computed, on the tax-inclusive basis, but it is a display
    # hint: it prices nothing until somebody picks it.
    assert payload["lowest_amount"] == "900"
    assert all("custom_amount" not in q for q in payload["quotes"])
