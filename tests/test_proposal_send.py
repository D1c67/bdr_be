"""Unit tests for proposal send orchestration (pure parts, no DB / no Graph).

The isolation matrix is the heart: every way a document could reach the wrong
GC must raise before the email call.
"""

from decimal import Decimal

import pytest

from app.services import proposal_docx as pdx
from app.services import proposal_send as psend
from app.services.proposal_send import ProposalSendError, assert_send_isolation

HAS_TEMPLATE = pdx.TEMPLATE_PATH.exists()
needs_template = pytest.mark.skipif(not HAS_TEMPLATE, reason="template asset not present")


# ── amounts / formatting ───────────────────────────────────────────────────


def test_proposal_amounts_override_wins_with_fallback():
    originals = {
        "labor_amount": Decimal("40000"),
        "materials_amount": Decimal("40000"),
        "labor_markup_amount": Decimal("950"),
        "materials_markup_amount": Decimal("1188"),
    }
    verification = {
        "labor_amount": "41000",  # Executive override
        "materials_amount": None,  # falls back to original
        "labor_markup_amount": None,
        "materials_markup_amount": None,
        "committed_at": "2026-06-10T00:00:00Z",
    }
    amounts = psend.proposal_amounts(originals, verification)
    assert amounts["material"] == Decimal("41188")
    assert amounts["labor"] == Decimal("41950")
    assert amounts["total"] == Decimal("83138")


def test_proposal_amounts_missing_values_are_zero():
    amounts = psend.proposal_amounts({}, {"committed_at": "x"})
    assert amounts == {"material": Decimal(0), "labor": Decimal(0), "total": Decimal(0)}


def test_format_money():
    assert psend.format_money(Decimal("82138")) == "$82,138"
    assert psend.format_money(Decimal("1234.56")) == "$1,234.56"
    assert psend.format_money(Decimal("0")) == "$0"


def test_resolve_gc_amounts_override_wins_per_figure():
    defaults = {
        "material": Decimal("41188"),
        "labor": Decimal("40950"),
        "total": Decimal("82138"),
    }
    assert (
        psend.resolve_gc_amounts(defaults, {"material_override": None, "labor_override": None})
        == defaults
    )
    assert psend.resolve_gc_amounts(
        defaults, {"material_override": Decimal("50000"), "labor_override": None}
    ) == {"material": Decimal("50000"), "labor": Decimal("40950"), "total": Decimal("90950")}
    assert psend.resolve_gc_amounts(
        defaults, {"material_override": Decimal("100"), "labor_override": Decimal("200")}
    ) == {"material": Decimal("100"), "labor": Decimal("200"), "total": Decimal("300")}


def test_stamped_amounts_require_both_figures():
    assert psend.stamped_amounts({}) is None
    assert psend.stamped_amounts({"material_amount": 41188, "labor_amount": None}) is None
    assert psend.stamped_amounts({"material_amount": 41188, "labor_amount": "40950"}) == (
        Decimal("41188"),
        Decimal("40950"),
    )


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
        # moved) or the row stamp disagrees with the live settings.
        (lambda k: k["row"].update(material_amount="50000"), "Amounts changed"),
        (lambda k: k["expected_amounts"].update(labor=Decimal("45000")), "Amounts changed"),
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
