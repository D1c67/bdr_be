"""Unit tests for proposal send orchestration (pure parts, no DB / no Graph).

The isolation matrix is the heart: every way a document could reach the wrong
GC must raise before the email call.
"""

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.services import proposal_docx as pdx
from app.services import proposal_send as psend
from app.services.proposal_send import ProposalSendError, assert_send_isolation

HAS_TEMPLATE = pdx.TEMPLATE_PATH.exists()
needs_template = pytest.mark.skipif(not HAS_TEMPLATE, reason="template asset not present")


# ── amounts / formatting ───────────────────────────────────────────────────


def test_proposal_amounts_uncommitted_override_wins_with_fallback():
    originals = {
        "labor_amount": Decimal("40000"),
        "materials_amount": Decimal("40000"),
        "labor_markup_amount": Decimal("950"),
        "materials_markup_amount": Decimal("1188"),
    }
    verification = {
        "labor_amount": "41000",  # Executive draft override
        "materials_amount": None,  # falls back to the live original
        "labor_markup_amount": None,
        "materials_markup_amount": None,
        "committed_at": None,
    }
    amounts = psend.proposal_amounts(originals, verification)
    assert amounts["material"] == Decimal("41188")
    assert amounts["labor"] == Decimal("41950")
    assert amounts["total"] == Decimal("83138")
    # No sections on this project: their figures stay None (no rows rendered).
    assert amounts["gear"] is None
    assert amounts["underground"] is None
    assert amounts["low_voltage"] is None


def test_proposal_amounts_committed_snapshot_is_read_as_is():
    # The commit stored every resolved number; the live originals never feed a
    # committed bid again (they can move after the Executive signed off).
    originals = {"materials_amount": Decimal("99999")}
    verification = {
        "labor_amount": "41000",
        "materials_amount": "40000",
        "gear_amount": "5000",
        "underground_amount": None,  # section not on the project
        "low_voltage_amount": None,
        "labor_markup_amount": "950",
        "materials_markup_amount": "1188",
        "gear_markup_amount": "500",
        "underground_markup_amount": None,
        "low_voltage_markup_amount": None,
        "committed_at": "2026-06-10T00:00:00Z",
    }
    amounts = psend.proposal_amounts(originals, verification)
    assert amounts["material"] == Decimal("41188")
    assert amounts["gear"] == Decimal("5500")
    assert amounts["underground"] is None
    assert amounts["low_voltage"] is None
    assert amounts["labor"] == Decimal("41950")
    assert amounts["total"] == Decimal("88638")


def test_proposal_amounts_legacy_committed_snapshot_totals_unchanged():
    # A pre-sections committed snapshot: the four legacy numbers are set, all
    # six section columns are NULL, and materials_amount carries the FULL
    # materials figure. It must compute exactly as before the release:
    # Material carries everything, section figures are None (no rows), and the
    # live section partition (which WOULD split the figure) is ignored.
    originals = {
        "labor_amount": Decimal("40000"),
        "materials_amount": Decimal("30000"),  # live residual after the split
        "gear_amount": Decimal("10000"),  # live breakout, must not double count
        "labor_markup_amount": Decimal("950"),
        "materials_markup_amount": Decimal("1188"),
        "gear_markup_amount": Decimal("100"),
    }
    verification = {
        "labor_amount": "40000",
        "materials_amount": "40000",  # the full pre-release figure
        "labor_markup_amount": "950",
        "materials_markup_amount": "1188",
        "gear_amount": None,
        "underground_amount": None,
        "low_voltage_amount": None,
        "gear_markup_amount": None,
        "underground_markup_amount": None,
        "low_voltage_markup_amount": None,
        "committed_at": "2025-12-01T00:00:00Z",
    }
    final = psend.resolved_verify_numbers(originals, verification)
    assert final["materials_amount"] == Decimal("40000")
    assert final["gear_amount"] is None
    assert final["gear_markup_amount"] is None
    amounts = psend.amounts_from_final(final)
    assert amounts["material"] == Decimal("41188")
    assert amounts["gear"] is None
    assert amounts["underground"] is None
    assert amounts["low_voltage"] is None
    assert amounts["labor"] == Decimal("40950")
    assert amounts["total"] == Decimal("82138")


def test_proposal_amounts_missing_values_are_zero():
    amounts = psend.proposal_amounts({}, {"committed_at": "x"})
    assert amounts == {
        "material": Decimal(0),
        "gear": None,
        "underground": None,
        "low_voltage": None,
        "labor": Decimal(0),
        "total": Decimal(0),
    }


def test_present_section_without_markup_carries_its_cost():
    # A present section whose markup was never entered: the section figure is
    # the cost as-is (markup treated as 0), not None and not an error.
    originals = {
        "labor_amount": Decimal("100"),
        "materials_amount": Decimal("200"),
        "underground_amount": Decimal("50"),
        "labor_markup_amount": Decimal("10"),
        "materials_markup_amount": Decimal("20"),
        "underground_markup_amount": None,
    }
    amounts = psend.proposal_amounts(originals, {})
    assert amounts["underground"] == Decimal("50")
    assert amounts["total"] == Decimal("380")


def test_format_money():
    assert psend.format_money(Decimal("82138")) == "$82,138"
    assert psend.format_money(Decimal("1234.56")) == "$1,234.56"
    assert psend.format_money(Decimal("0")) == "$0"


# A two-bucket project (no sections) as resolved_verify_numbers returns it.
FINAL_NO_SECTIONS = {
    "labor_amount": Decimal("40000"),
    "materials_amount": Decimal("40000"),
    "gear_amount": None,
    "underground_amount": None,
    "low_voltage_amount": None,
    "labor_markup_amount": Decimal("950"),
    "materials_markup_amount": Decimal("1188"),
    "gear_markup_amount": None,
    "underground_markup_amount": None,
    "low_voltage_markup_amount": None,
}

# The same project with a gear breakout carved out of materials.
FINAL_WITH_GEAR = {
    **FINAL_NO_SECTIONS,
    "materials_amount": Decimal("30000"),
    "gear_amount": Decimal("10000"),
    "gear_markup_amount": Decimal("500"),
    "materials_markup_amount": Decimal("688"),
}


def test_resolve_gc_amounts_override_wins_per_figure():
    defaults = psend.amounts_from_final(FINAL_NO_SECTIONS)
    assert defaults["material"] == Decimal("41188")
    assert defaults["labor"] == Decimal("40950")
    assert defaults["total"] == Decimal("82138")
    assert psend.resolve_gc_amounts(defaults, {}) == defaults
    assert psend.resolve_gc_amounts(
        defaults, {"material_override": Decimal("50000"), "labor_override": None}
    ) == {
        "material": Decimal("50000"),
        "gear": None,
        "underground": None,
        "low_voltage": None,
        "labor": Decimal("40950"),
        "total": Decimal("90950"),
    }
    assert psend.resolve_gc_amounts(
        defaults, {"material_override": Decimal("100"), "labor_override": Decimal("200")}
    )["total"] == Decimal("300")


def test_resolve_gc_amounts_section_override_wins_and_resums():
    defaults = psend.amounts_from_final(FINAL_WITH_GEAR)
    assert defaults["gear"] == Decimal("10500")
    assert defaults["total"] == Decimal("82138")
    resolved = psend.resolve_gc_amounts(defaults, {"gear_override": Decimal("12000")})
    assert resolved["gear"] == Decimal("12000")
    assert resolved["material"] == Decimal("30688")
    assert resolved["total"] == Decimal("83638")


def test_gc_markups_price_change_moves_only_that_section():
    # Costs 40k/40k with default markups 1188/950: base prices 41188/40950.
    basis = psend.pricing_basis(FINAL_NO_SECTIONS)
    assert basis == {
        "material_cost": Decimal("40000"),
        "gear_cost": None,
        "underground_cost": None,
        "low_voltage_cost": None,
        "labor_cost": Decimal("40000"),
        "material_markup": Decimal("1188"),
        "gear_markup": None,
        "underground_markup": None,
        "low_voltage_markup": None,
        "labor_markup": Decimal("950"),
    }
    defaults = psend.amounts_from_final(FINAL_NO_SECTIONS)

    # No override: each markup is exactly the project default; absent sections
    # produce None, never 0.
    no_override = psend.resolve_gc_amounts(defaults, {})
    assert psend.gc_markups(basis, no_override) == {
        "material_markup": Decimal("1188"),
        "gear_markup": None,
        "underground_markup": None,
        "low_voltage_markup": None,
        "labor_markup": Decimal("950"),
    }

    # A material price change lands entirely in the material markup; the labor
    # markup (and both costs) are untouched.
    material_up = psend.resolve_gc_amounts(
        defaults, {"material_override": Decimal("50000"), "labor_override": None}
    )
    markups = psend.gc_markups(basis, material_up)
    assert markups["material_markup"] == Decimal("10000")
    assert markups["labor_markup"] == Decimal("950")

    # A gear price change on a sectioned project moves only the gear markup.
    gear_basis = psend.pricing_basis(FINAL_WITH_GEAR)
    gear_up = psend.resolve_gc_amounts(
        psend.amounts_from_final(FINAL_WITH_GEAR), {"gear_override": Decimal("12000")}
    )
    gear_markups = psend.gc_markups(gear_basis, gear_up)
    assert gear_markups["gear_markup"] == Decimal("2000")
    assert gear_markups["material_markup"] == Decimal("688")
    assert gear_markups["labor_markup"] == Decimal("950")


def test_gc_markups_below_cost_goes_negative_unclamped():
    # A price far below cost must come back as a deep negative markup, not a
    # clamped zero and not a shifted cost basis.
    basis = psend.pricing_basis(FINAL_NO_SECTIONS)
    resolved = psend.resolve_gc_amounts(
        psend.amounts_from_final(FINAL_NO_SECTIONS),
        {"material_override": Decimal("100"), "labor_override": Decimal("200")},
    )
    markups = psend.gc_markups(basis, resolved)
    assert markups["material_markup"] == Decimal("-39900")
    assert markups["labor_markup"] == Decimal("-39800")
    # The basis is a pure read of the shared figures; deriving markups from it
    # must not have mutated the costs.
    assert basis["material_cost"] == Decimal("40000")
    assert basis["labor_cost"] == Decimal("40000")


def test_section_override_for_absent_section_is_refused():
    # The 409 behind PUT /proposals/amounts/{gc_id}: a figure for a section
    # that is not on the project (its cost basis is None) is refused.
    basis = psend.pricing_basis(FINAL_NO_SECTIONS)
    with pytest.raises(ProposalSendError, match="not on this project") as exc:
        psend.assert_section_overrides_allowed(basis, {"gear": Decimal("5000")})
    assert exc.value.status_code == 409
    # Clearing (None) is always fine, as is a figure for a present section.
    psend.assert_section_overrides_allowed(basis, {"gear": None, "material": Decimal("1")})
    gear_basis = psend.pricing_basis(FINAL_WITH_GEAR)
    psend.assert_section_overrides_allowed(gear_basis, {"gear": Decimal("5000")})


def test_align_sections_to_basis_hides_legacy_committed_breakouts():
    # A legacy pre-release committed snapshot has no breakout decomposition
    # (basis cost None) even when the category is live on the project; the
    # editors key off `present`, which must mirror the basis or the UI offers
    # a column that every save 409s.
    sections = {
        "materials": {"present": True, "categories": ["General Material"]},
        "gear": {"present": True, "categories": ["Switchgear"], "includes_generator": False},
        "underground": {"present": True, "categories": ["Trenching"]},
        "low_voltage": {"present": False, "categories": []},
    }
    basis = {
        "gear_cost": None,
        "underground_cost": Decimal("100"),
        "low_voltage_cost": None,
    }
    out = psend.align_sections_to_basis(sections, basis)
    assert out["gear"]["present"] is False  # live RFQ, no committed decomposition
    assert out["underground"]["present"] is True  # decomposed: still editable
    assert out["low_voltage"]["present"] is False  # absent stays absent
    assert out["materials"]["present"] is True
    assert sections["gear"]["present"] is True  # input not mutated


def test_gc_amounts_editable_from_gc_pricing_onward():
    # Editable at every head from GC Pricing on, including after the bid is
    # submitted: a GC added late still needs its numbers set. What protects a
    # bid already delivered is the per-GC lock (set_gc_amounts refuses a row at
    # 'sent'/'sending'), not the stage — see test_gc_amounts_locked_once_sent.
    for head in ("gc_pricing", "verify", "send_out", "submitted", "bid_outcome"):
        psend.assert_gc_amounts_editable(head)  # must not raise
    # A lane that has not reached GC Pricing has no numbers to edit yet.
    with pytest.raises(ProposalSendError, match="before the bid reaches GC Pricing"):
        psend.assert_gc_amounts_editable(None)
    with pytest.raises(ProposalSendError, match="before the bid reaches GC Pricing"):
        psend.assert_gc_amounts_editable("estimate_received")


def test_send_window_covers_send_out_through_bid_outcome(monkeypatch):
    # generate / send / re-send / mark-submitted all key off this window. It
    # stays open past 'send_out' so a GC added after "Done sending" can still be
    # bid and any GC can be sent its document again.
    import app.services.workflow as wf

    def _head(head):
        monkeypatch.setattr(
            wf, "load_category_state", lambda _pid, h=head: {"send_out": {"current_task": h}}
        )

    for head in ("gc_pricing", "verify", "send_out", "submitted", "bid_outcome"):
        _head(head)
        assert psend.send_window_head("p1") == head
    for head in (None, "markup"):
        _head(head)
        with pytest.raises(ProposalSendError, match="has not reached the Send Out stage"):
            psend.send_window_head("p1")


def test_stamped_amounts_none_only_for_prefeature_rows():
    # Rows generated before per-GC amounts existed carry no stamp at all.
    assert psend.stamped_amounts({}) is None
    assert psend.stamped_amounts({"material_amount": None, "labor_amount": None}) is None
    assert psend.stamped_amounts({"material_amount": 41188, "labor_amount": "40950"}) == {
        "material": Decimal("41188"),
        "gear": None,
        "underground": None,
        "low_voltage": None,
        "labor": Decimal("40950"),
    }
    stamped = psend.stamped_amounts(
        {
            "material_amount": "30688",
            "gear_amount": "10500",
            "underground_amount": None,
            "low_voltage_amount": None,
            "labor_amount": "40950",
        }
    )
    assert stamped["gear"] == Decimal("10500")
    assert stamped["underground"] is None


def test_stamp_figures_lists_present_sections_and_resums_total():
    figures = psend.stamp_figures(
        {
            "material": Decimal("30688"),
            "gear": Decimal("10500"),
            "underground": None,
            "low_voltage": None,
            "labor": Decimal("40950"),
        }
    )
    assert figures == ("$30,688", "$10,500", "$40,950", "$82,138")


def test_lines_hash_is_order_sensitive():
    a = psend.lines_hash(["one", "two"])
    assert a == psend.lines_hash(["one", "two"])
    assert a != psend.lines_hash(["two", "one"])


def test_build_cover_email():
    subject, body = psend.build_cover_email({"name": "Red Rock", "number": "26.4.7080"})
    assert "Red Rock" in subject and "26.4.7080" in subject
    assert psend.GC_NAME_TOKEN in body
    assert "attached" in body
    # Body is plain text — the branded HTML shell is applied at send time.
    assert "<p>" not in body


# ── recipient resolution ───────────────────────────────────────────────────


def test_resolve_recipients_default_is_every_contact_with_email():
    assert psend.resolve_recipients(GC, None) == RECIPIENTS


def test_resolve_recipients_chosen_subset_dedupes_and_sorts():
    assert psend.resolve_recipients(GC, ["c-2", "c-2", "c-1"]) == RECIPIENTS
    assert psend.resolve_recipients(GC, ["c-2"]) == ["pat@taylor.com"]


def test_resolve_recipients_stale_choice_fails_closed():
    with pytest.raises(ProposalSendError, match="no longer on file"):
        psend.resolve_recipients(GC, ["c-404"])  # deleted contact
    with pytest.raises(ProposalSendError, match="no longer on file"):
        psend.resolve_recipients(GC, ["c-3"])  # exists but has no email


def test_join_recipients_matches_email_log_to_addrs_format():
    # graph_email.send_mail logs to_addrs as ", ".join(to); crash recovery
    # proves delivery by exact equality with proposal_sends.gc_email.
    assert psend.join_recipients(["a@x.com", "b@y.com"]) == "a@x.com, b@y.com"


def _cc_settings(monkeypatch, addr):
    monkeypatch.setattr(psend, "get_settings", lambda: Settings(_env_file=None, proposal_cc=addr))


def test_cc_recipients_copies_the_bids_desk(monkeypatch):
    _cc_settings(monkeypatch, "bids@g3electrical.com")
    assert psend.cc_recipients(RECIPIENTS) == ["bids@g3electrical.com"]


def test_cc_recipients_empty_setting_disables_the_cc(monkeypatch):
    _cc_settings(monkeypatch, "   ")
    assert psend.cc_recipients(RECIPIENTS) == []


def test_cc_recipients_not_duplicated_when_already_on_the_to_line(monkeypatch):
    # A GC contact at the same address must not be both To and CC.
    _cc_settings(monkeypatch, "bids@g3electrical.com")
    assert psend.cc_recipients(["BIDS@G3Electrical.com"]) == []


def test_resolve_cc_none_or_empty_is_no_cc():
    assert psend.resolve_cc(GC, None, RECIPIENTS) == ([], None)
    assert psend.resolve_cc(GC, [], RECIPIENTS) == ([], None)


def test_resolve_cc_returns_emails_and_snapshot():
    emails, snapshot = psend.resolve_cc(GC, ["c-2"], ["bids@taylor.com"])
    assert emails == ["pat@taylor.com"]
    assert snapshot == [
        {"gc_contact_id": "c-2", "name": "Pat Estimator", "email": "pat@taylor.com"}
    ]


def test_resolve_cc_drops_contacts_already_on_the_to_line():
    # Nobody is addressed twice; a To pick sneaking into the CC list is dropped,
    # case-insensitively, and an all-dropped list means no CC at all.
    assert psend.resolve_cc(GC, ["c-2"], ["PAT@taylor.com"]) == ([], None)


def test_resolve_cc_dedupes_ids():
    emails, snapshot = psend.resolve_cc(GC, ["c-2", "c-2"], ["bids@taylor.com"])
    assert emails == ["pat@taylor.com"]
    assert len(snapshot) == 1


def test_resolve_cc_stale_or_emailless_choice_fails_closed():
    # Same contract as resolve_recipients: the sender confirmed a list that no
    # longer exists, so make them reopen the dialog. A contact of ANOTHER GC is
    # indistinguishable from a deleted one here, which is what scopes the CC to
    # the same company.
    with pytest.raises(ProposalSendError, match="CC contact"):
        psend.resolve_cc(GC, ["c-404"], RECIPIENTS)  # deleted / other company
    with pytest.raises(ProposalSendError, match="CC contact"):
        psend.resolve_cc(GC, ["c-3"], RECIPIENTS)  # exists but has no email


@needs_template
def test_cc_stays_out_of_the_recipient_isolation_contract(monkeypatch):
    # The CC must never leak into the To line: gc_email is compared for exact
    # equality against join_recipients, and every To address must be a live
    # contact of this GC, so a G3 address folded in would fail closed.
    _cc_settings(monkeypatch, "bids@g3electrical.com")
    kwargs = _good_kwargs(_fixture_bytes())
    kwargs["recipients"] = kwargs["recipients"] + psend.cc_recipients(kwargs["recipients"])
    with pytest.raises(ProposalSendError, match="no longer on file"):
        assert_send_isolation(**kwargs)


# ── isolation matrix ───────────────────────────────────────────────────────


GC = {
    "id": "gc-1",
    "name": "Taylor International Corp.",
    "contacts": [
        {"id": "c-1", "name": "Bid Desk", "email": "bids@taylor.com"},
        {"id": "c-2", "name": "Pat Estimator", "email": "pat@taylor.com"},
        {"id": "c-3", "name": "Front Office", "email": None},
    ],
}
PROJECT = {"id": "p-1", "number": "26.4.7080", "name": "Red Rock"}
RECIPIENTS = ["bids@taylor.com", "pat@taylor.com"]  # sorted, like resolve_recipients
LINES = [
    "Demolish existing lighting and electrical devices.",
    "Furnish and install conduit, wiring and boxes.",
]


def _fixture_bytes() -> bytes:
    ctx = pdx.ProposalContext(
        project_number=PROJECT["number"],
        project_name="Red Rock Slot Expansion",
        address="11011 W Charleston Blvd, Las Vegas, NV 89135",
        gc_name=GC["name"],
        date_str="06/10/2026",
        labor_time="DAY",
        wage_text="Prevailing Wage",
        material_amount="$41,188",
        labor_amount="$40,950",
        total_amount="$82,138",
        scope_lines=tuple(LINES),
    )
    return pdx.render_proposal(pdx.TEMPLATE_PATH.read_bytes(), ctx)


def _good_kwargs(docx_bytes: bytes) -> dict:
    digest = psend.lines_hash(LINES)
    return dict(
        row={
            "id": "ps-1",
            "project_id": "p-1",
            "gc_id": "gc-1",
            "gc_name": GC["name"],
            # the claim wrote the recipient list before isolation runs
            "gc_email": psend.join_recipients(RECIPIENTS),
            "draft_id": "d-1",
            "lines_hash": digest,
            # the figures generation stamped — must match the fixture bytes
            "material_amount": "41188",
            "labor_amount": "40950",
        },
        file_row={
            "id": "f-1",
            "project_id": "p-1",
            "gc_id": "gc-1",
            "category": "proposal",
            "filename": pdx.build_filename(PROJECT["number"], GC["name"]),
        },
        docx_bytes=docx_bytes,
        recipients=list(RECIPIENTS),
        live_gc=GC,
        project=PROJECT,
        draft={"id": "d-1", "approved_at": "2026-06-10T00:00:00Z", "lines_json": LINES},
        other_gc_names=("Turner Construction",),
        expected_amounts={
            "material": Decimal("41188"),
            "gear": None,
            "underground": None,
            "low_voltage": None,
            "labor": Decimal("40950"),
            "total": Decimal("82138"),
        },
    )


@needs_template
def test_isolation_happy_path_passes():
    assert_send_isolation(**_good_kwargs(_fixture_bytes()))


@needs_template
@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda k: k["file_row"].update(gc_id="gc-2"), "does not belong to this GC"),
        (lambda k: k["file_row"].update(project_id="p-2"), "different project"),
        (lambda k: k["file_row"].update(category="other"), "not a generated proposal"),
        (lambda k: k["file_row"].update(filename="Proposal 7080 - Turner Construction.docx"),
         "filename does not match"),
        (lambda k: k.update(recipients=[]), "no contact with an email"),
        (lambda k: k.update(recipients=["other@elsewhere.com"]), "no longer on file"),
        (lambda k: k["live_gc"].update(contacts=[{"id": "c-1", "email": "moved@taylor.com"}]),
         "no longer on file"),
        (lambda k: k["row"].update(gc_email="bids@taylor.com"),
         "does not match the claimed send row"),
        (lambda k: k.update(live_gc={}), "no longer on this project"),
        (lambda k: k.update(draft=None), "draft changed"),
        (lambda k: k["draft"].update(id="d-2"), "draft changed"),
        (lambda k: k["draft"].update(approved_at=None), "no longer approved"),
        (lambda k: k["draft"].update(lines_json=LINES + ["Furnish and install panels."]),
         "lines changed"),
        # Per-GC amounts: the override was edited after generation (expected
        # moved) or the row stamp disagrees with the live settings. All five
        # pairs are compared; a section appearing after generation (live gear
        # figure vs NULL stamp) fails closed exactly like a moved number.
        (lambda k: k["row"].update(material_amount="50000"), "Amounts changed"),
        (lambda k: k["expected_amounts"].update(labor=Decimal("45000")), "Amounts changed"),
        (lambda k: k["expected_amounts"].update(gear=Decimal("5000")), "Amounts changed"),
        (lambda k: k["row"].update(underground_amount="1500"), "Amounts changed"),
    ],
)
def test_isolation_violations_raise(mutate, match):
    kwargs = _good_kwargs(_fixture_bytes())
    kwargs["live_gc"] = dict(GC)
    kwargs["draft"] = dict(kwargs["draft"])
    mutate(kwargs)
    with pytest.raises(ProposalSendError, match=match):
        assert_send_isolation(**kwargs)


@needs_template
def test_isolation_rejects_other_gcs_document():
    """The swapped-attachment scenario: Turner's bytes offered for Taylor."""
    ctx_other_gc = "Turner Construction"
    kwargs = _good_kwargs(_fixture_bytes())
    other_ctx_bytes = pdx.render_proposal(
        pdx.TEMPLATE_PATH.read_bytes(),
        pdx.ProposalContext(
            project_number=PROJECT["number"],
            project_name="Red Rock Slot Expansion",
            address="11011 W Charleston Blvd, Las Vegas, NV 89135",
            gc_name=ctx_other_gc,
            date_str="06/10/2026",
            labor_time="DAY",
            wage_text="Prevailing Wage",
            material_amount="$41,188",
            labor_amount="$40,950",
            total_amount="$82,138",
            scope_lines=tuple(LINES),
        ),
    )
    kwargs["docx_bytes"] = other_ctx_bytes
    with pytest.raises(Exception, match="To: cell|ISOLATION"):
        assert_send_isolation(**kwargs)


@needs_template
def test_isolation_legacy_rows_without_stamps_skip_amounts_check():
    """Rows generated before per-GC amounts have nothing to prove — they keep
    the pre-feature behavior even when the live settings differ."""
    kwargs = _good_kwargs(_fixture_bytes())
    kwargs["row"].update(material_amount=None, labor_amount=None)
    kwargs["expected_amounts"] = {
        "material": Decimal("1"),
        "labor": Decimal("2"),
        "total": Decimal("3"),
    }
    assert_send_isolation(**kwargs)


@needs_template
def test_isolation_rejects_bytes_not_carrying_stamped_amounts():
    """Stamp and live settings agree, but the bytes say something else — the
    document text must carry the stamped figures."""
    kwargs = _good_kwargs(_fixture_bytes())  # bytes rendered with $41,188
    kwargs["row"].update(material_amount="50000")
    kwargs["expected_amounts"].update(material=Decimal("50000"), total=Decimal("90950"))
    with pytest.raises(Exception, match="missing from the document"):
        assert_send_isolation(**kwargs)


# ── section-aware validation wiring (generator caption / removed labels) ──


def test_section_validation_kwargs_derivation():
    """Pre-feature rows skip the section checks; stamped rows derive the
    removed labels from their NULL section stamps, and the caption is only
    allowed when the gear row itself was rendered."""
    assert psend.section_validation_kwargs(None, True) == {
        "includes_generator": False,
        "removed_sections": (),
    }
    stamped = {
        "material": Decimal("41188"),
        "gear": Decimal("10500"),
        "underground": None,
        "low_voltage": None,
        "labor": Decimal("40950"),
    }
    assert psend.section_validation_kwargs(stamped, True) == {
        "includes_generator": True,
        "removed_sections": ("underground", "low_voltage"),
    }
    # Live generator flag but no rendered gear row: no caption possible.
    assert psend.section_validation_kwargs({**stamped, "gear": None}, True) == {
        "includes_generator": False,
        "removed_sections": ("gear", "underground", "low_voltage"),
    }


def _gear_fixture_bytes(
    includes_generator: bool, gear_amount: str = "$10,500", total_amount: str = "$92,638"
) -> bytes:
    ctx = pdx.ProposalContext(
        project_number=PROJECT["number"],
        project_name="Red Rock Slot Expansion",
        address="11011 W Charleston Blvd, Las Vegas, NV 89135",
        gc_name=GC["name"],
        date_str="06/10/2026",
        labor_time="DAY",
        wage_text="Prevailing Wage",
        material_amount="$41,188",
        labor_amount="$40,950",
        total_amount=total_amount,
        gear_amount=gear_amount,
        includes_generator=includes_generator,
        scope_lines=tuple(LINES),
    )
    return pdx.render_proposal(pdx.TEMPLATE_PATH.read_bytes(), ctx)


def _gear_kwargs(docx_bytes: bytes) -> dict:
    kwargs = _good_kwargs(docx_bytes)
    kwargs["row"] = {**kwargs["row"], "gear_amount": "10500"}
    kwargs["expected_amounts"] = {
        **kwargs["expected_amounts"],
        "gear": Decimal("10500"),
        "total": Decimal("92638"),
    }
    return kwargs


@needs_template
def test_isolation_generator_caption_flows_through():
    """A doc rendered with the caption sends only while the live flag says the
    project has a generator; the mismatch fails closed in both directions."""
    with_caption = _gear_fixture_bytes(includes_generator=True)
    kwargs = _gear_kwargs(with_caption)
    assert_send_isolation(**kwargs, includes_generator=True)
    with pytest.raises(Exception, match="Generator caption"):
        assert_send_isolation(**kwargs)  # live flag says no generator

    without_caption = _gear_fixture_bytes(includes_generator=False)
    kwargs = _gear_kwargs(without_caption)
    assert_send_isolation(**kwargs)
    with pytest.raises(Exception, match="Generator caption"):
        # Generator added after generation: stale doc, must regenerate.
        assert_send_isolation(**kwargs, includes_generator=True)


@needs_template
def test_isolation_removed_section_labels_are_checked_at_send():
    """A row whose section stamps are NULL offered bytes that still carry a
    section row fails closed on the label check (stale or wrong bytes). The
    figures are made to agree so the label check itself is what fires."""
    docx_bytes = _gear_fixture_bytes(
        includes_generator=False, gear_amount="$0", total_amount="$82,138"
    )
    kwargs = _good_kwargs(docx_bytes)  # all section stamps NULL
    with pytest.raises(Exception, match="Removed section label"):
        assert_send_isolation(**kwargs)


# ── mark as submitted (third-party application, no email) ──────────────────


def _mark_ready_kwargs() -> dict:
    digest = psend.lines_hash(LINES)
    return dict(
        row={
            "id": "ps-1",
            "file_id": "f-1",
            "draft_id": "d-1",
            "lines_hash": digest,
            "material_amount": "41188",
            "labor_amount": "40950",
        },
        draft={"id": "d-1", "approved_at": "2026-06-10T00:00:00Z", "lines_json": LINES},
        latest_draft_id="d-1",
        expected_amounts={
            "material": Decimal("41188"),
            "gear": None,
            "underground": None,
            "low_voltage": None,
            "labor": Decimal("40950"),
            "total": Decimal("82138"),
        },
    )


def test_mark_ready_happy_path_passes():
    psend.assert_mark_ready(**_mark_ready_kwargs())


def test_mark_ready_matching_section_stamps_pass():
    kwargs = _mark_ready_kwargs()
    kwargs["row"].update(material_amount="30688", gear_amount="10500")
    kwargs["expected_amounts"].update(material=Decimal("30688"), gear=Decimal("10500"))
    psend.assert_mark_ready(**kwargs)


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda k: k["row"].update(file_id=None), "document is missing"),
        (lambda k: k.update(draft=None), "draft changed"),
        (lambda k: k["draft"].update(id="d-2"), "draft changed"),
        (lambda k: k.update(latest_draft_id="d-9"), "newer draft exists"),
        (lambda k: k["draft"].update(approved_at=None), "no longer approved"),
        (lambda k: k["draft"].update(lines_json=LINES + ["Furnish and install panels."]),
         "lines changed"),
        (lambda k: k["row"].update(material_amount="50000"), "Amounts changed"),
        (lambda k: k["expected_amounts"].update(labor=Decimal("45000")), "Amounts changed"),
        # Five-pair staleness: a section stamped into the document but no
        # longer expected (or vice versa) is a stale document, not a match.
        (lambda k: k["row"].update(gear_amount="5000"), "Amounts changed"),
        (lambda k: k["expected_amounts"].update(low_voltage=Decimal("1200")),
         "Amounts changed"),
        # A pre-sections document on a project whose live pricing now carries a
        # section split: every pair must agree, so it demands regeneration.
        (lambda k: (
            k["row"].update(material_amount="41188"),
            k["expected_amounts"].update(material=Decimal("30688"), gear=Decimal("10500")),
        ), "Amounts changed"),
    ],
)
def test_mark_ready_staleness_raises(mutate, match):
    kwargs = _mark_ready_kwargs()
    mutate(kwargs)
    with pytest.raises(ProposalSendError, match=match):
        psend.assert_mark_ready(**kwargs)


def test_mark_ready_legacy_rows_without_stamps_skip_amounts_check():
    kwargs = _mark_ready_kwargs()
    kwargs["row"].update(material_amount=None, labor_amount=None)
    kwargs["expected_amounts"] = {
        "material": Decimal("1"),
        "labor": Decimal("2"),
        "total": Decimal("3"),
    }
    psend.assert_mark_ready(**kwargs)


def test_mark_ready_without_live_amounts_skips_amounts_check():
    # Defensive: verification uncommitted (shouldn't happen at send_out) —
    # the metadata checks still run; only the amounts comparison is skipped.
    kwargs = _mark_ready_kwargs()
    kwargs["expected_amounts"] = None
    psend.assert_mark_ready(**kwargs)


# ── re-send isolation ──────────────────────────────────────────────────────
#
# A re-send emails the document that ALREADY went out. The isolation contract
# (right GC, no other GC's name, only this GC's live contacts) is unchanged;
# the freshness contract is deliberately dropped, because the document is
# history and drift since delivery is the normal case, not a fault.


def _resend_kwargs(docx_bytes: bytes) -> dict:
    send = _good_kwargs(docx_bytes)
    return dict(
        row=send["row"],
        file_row=send["file_row"],
        docx_bytes=docx_bytes,
        recipients=send["recipients"],
        live_gc=send["live_gc"],
        project=send["project"],
        scope_lines=tuple(LINES),
        other_gc_names=send["other_gc_names"],
    )


@needs_template
def test_resend_isolation_happy_path_passes():
    psend.assert_resend_isolation(**_resend_kwargs(_fixture_bytes()))


@needs_template
@pytest.mark.parametrize(
    "mutate",
    [
        # The live draft moved on after the bid went out.
        lambda k: k["row"].update(draft_id="d-9", lines_hash="stale-hash"),
        # The originating draft row is gone, so there are no lines to re-check.
        lambda k: k.update(scope_lines=()),
    ],
)
def test_resend_allows_a_stale_document(mutate):
    """The recovery path must not be blocked by drift. assert_send_isolation
    would refuse these (and should, for a FIRST send); a re-send of an
    already-delivered document must go through."""
    kwargs = _resend_kwargs(_fixture_bytes())
    kwargs["row"] = dict(kwargs["row"])
    mutate(kwargs)
    psend.assert_resend_isolation(**kwargs)


def test_resend_isolation_takes_no_freshness_inputs():
    """Structural guard on the distinction this whole path rests on. If someone
    later wires the live draft or today's per-GC amounts into the re-send gate,
    every re-send of an older bid starts failing closed the moment pricing is
    re-verified — the exact breakage this test exists to catch."""
    import inspect

    params = set(inspect.signature(psend.assert_resend_isolation).parameters)
    assert not params & {"draft", "expected_amounts", "latest_draft_id"}
    # ...while the first-send gate must keep every one of them.
    send_params = set(inspect.signature(psend.assert_send_isolation).parameters)
    assert {"draft", "expected_amounts"} <= send_params


@needs_template
@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda k: k["file_row"].update(gc_id="gc-2"), "does not belong to this GC"),
        (lambda k: k["file_row"].update(project_id="p-2"), "different project"),
        (lambda k: k["file_row"].update(category="other"), "not a generated proposal"),
        (
            lambda k: k["file_row"].update(
                filename="Proposal 7080 - Turner Construction.docx"
            ),
            "filename does not match",
        ),
        (lambda k: k.update(recipients=[]), "no contact with an email"),
        (lambda k: k.update(recipients=["other@elsewhere.com"]), "no longer on file"),
        (lambda k: k.update(live_gc={}), "no longer on this project"),
        # The stamped figures must still be the ones these bytes carry — a row
        # whose stamp disagrees with its own document is not re-sendable.
        (lambda k: k["row"].update(material_amount="50000"), "Amount"),
    ],
)
def test_resend_isolation_violations_still_raise(mutate, match):
    kwargs = _resend_kwargs(_fixture_bytes())
    kwargs["live_gc"] = dict(GC)
    kwargs["row"] = dict(kwargs["row"])
    kwargs["file_row"] = dict(kwargs["file_row"])
    mutate(kwargs)
    with pytest.raises(Exception, match=match):
        psend.assert_resend_isolation(**kwargs)


@needs_template
def test_resend_rejects_another_gcs_document():
    """The swapped-attachment scenario survives into the re-send path."""
    kwargs = _resend_kwargs(_fixture_bytes())
    kwargs["docx_bytes"] = pdx.render_proposal(
        pdx.TEMPLATE_PATH.read_bytes(),
        pdx.ProposalContext(
            project_number=PROJECT["number"],
            project_name="Red Rock Slot Expansion",
            address="11011 W Charleston Blvd, Las Vegas, NV 89135",
            gc_name="Turner Construction",
            date_str="06/10/2026",
            labor_time="DAY",
            wage_text="Prevailing Wage",
            material_amount="$41,188",
            labor_amount="$40,950",
            total_amount="$82,138",
            scope_lines=tuple(LINES),
        ),
    )
    with pytest.raises(Exception, match="To: cell|ISOLATION"):
        psend.assert_resend_isolation(**kwargs)


# ── extra attachments (Modify Files) ───────────────────────────────────────


class _ExtraExec:
    def __init__(self, data):
        self.data = data


class _ExtraQuery:
    def __init__(self, rows):
        self._rows = rows
        self._ids = None

    def __getattr__(self, name):
        def method(*args, **kwargs):
            if name == "in_" and args and args[0] == "id":
                self._ids = list(args[1])
            return self

        return method

    def execute(self):
        rows = self._rows if self._ids is None else [r for r in self._rows if r["id"] in self._ids]
        return _ExtraExec(rows)


class _ExtraSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "project_files"
        return _ExtraQuery(self._rows)


_EXTRA_ROWS = [
    {"id": "f-spec", "filename": "spec.pdf", "storage_path": "s/spec.pdf", "category": "specification"},
    {"id": "f-bond", "filename": "bond.docx", "storage_path": "s/bond.docx", "category": "other"},
    {"id": "f-prop", "filename": "Proposal 0104 - Acme.docx", "storage_path": "s/prop.docx", "category": "proposal"},
]


def test_prepare_extra_attachments_downloads_converts_and_dedupes(monkeypatch):
    monkeypatch.setattr(psend.storage, "download_file", lambda p: b"raw:" + p.encode())
    monkeypatch.setattr(psend.office_preview, "convert_for_send", lambda b, n: b"pdf:" + n.encode())
    out = psend._prepare_extra_attachments(
        _ExtraSB(_EXTRA_ROWS), "p1", ["f-spec", "f-bond", "f-spec"]
    )
    # Deduped, order kept; the PDF passes through, the docx is converted and renamed.
    assert [n for n, _ in out] == ["spec.pdf", "bond.pdf"]
    assert out[0][1] == b"raw:s/spec.pdf"
    assert out[1][1] == b"pdf:bond.docx"


def test_prepare_extra_attachments_none_or_empty_is_noop():
    assert psend._prepare_extra_attachments(_ExtraSB([]), "p1", None) == []
    assert psend._prepare_extra_attachments(_ExtraSB([]), "p1", []) == []


def test_prepare_extra_attachments_rejects_foreign_file():
    with pytest.raises(ProposalSendError) as err:
        psend._prepare_extra_attachments(_ExtraSB(_EXTRA_ROWS), "p1", ["f-nope"])
    assert err.value.status_code == 400
    assert "not available in this project" in str(err.value)


def test_prepare_extra_attachments_rejects_generated_proposal_docs():
    with pytest.raises(ProposalSendError) as err:
        psend._prepare_extra_attachments(_ExtraSB(_EXTRA_ROWS), "p1", ["f-prop"])
    assert err.value.status_code == 400
    assert "proposal document" in str(err.value)


def test_prepare_extra_attachments_conversion_failure_is_actionable(monkeypatch):
    monkeypatch.setattr(psend.storage, "download_file", lambda p: b"raw")

    def boom(b, n):
        raise psend.office_preview.ConversionError("gotenberg down")

    monkeypatch.setattr(psend.office_preview, "convert_for_send", boom)
    with pytest.raises(ProposalSendError) as err:
        psend._prepare_extra_attachments(_ExtraSB(_EXTRA_ROWS), "p1", ["f-bond"])
    assert "bond.docx" in str(err.value)
