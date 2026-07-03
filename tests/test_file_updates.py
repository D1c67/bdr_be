"""Pure-logic tests for the post-hand-off file-update rules (0048): the
initial-block lock, estimator visibility of updates, note validation, and the
branded package/updates email rendering."""

import pytest
from pydantic import ValidationError

from app.routers.files import (
    ESTIMATOR_READ,
    ESTIMATOR_WRITE,
    INITIAL_CATEGORIES,
    UPDATE_CATEGORIES,
    VALID_CATEGORIES,
    FileNoteIn,
    _estimator_visible,
    is_handoff_locked,
)
from app.services import estimator_email as ee


# ── is_handoff_locked ──────────────────────────────────────────────────────


def test_no_assignments_is_unlocked():
    assert is_handoff_locked([]) is False


def test_active_assignment_locks():
    assert is_handoff_locked([{"revoked_at": None, "sent_to_estimator_at": None}]) is True


def test_revoked_never_sent_unlocks_again():
    # Assign → revoke before anything was emailed: the package never left the
    # building, so the initial blocks reopen.
    assert is_handoff_locked([{"revoked_at": "2026-07-01T00:00:00Z", "sent_to_estimator_at": None}]) is False


def test_revoked_but_sent_stays_locked():
    # The package went out — what the estimator received must stay reconstructable.
    assert (
        is_handoff_locked(
            [{"revoked_at": "2026-07-01T00:00:00Z", "sent_to_estimator_at": "2026-06-30T00:00:00Z"}]
        )
        is True
    )


def test_mixed_rows_any_active_locks():
    rows = [
        {"revoked_at": "2026-07-01T00:00:00Z", "sent_to_estimator_at": None},
        {"revoked_at": None, "sent_to_estimator_at": None},
    ]
    assert is_handoff_locked(rows) is True


# ── category sets ──────────────────────────────────────────────────────────


def test_update_categories_are_uploadable_and_internal_only():
    assert UPDATE_CATEGORIES <= VALID_CATEGORIES
    # The estimator can never author updates — they only receive them.
    assert not (UPDATE_CATEGORIES & ESTIMATOR_WRITE)


def test_specifications_are_estimator_readable():
    # Decision reversal of migration 0037: specs are part of the package now.
    assert "specification" in ESTIMATOR_READ


def test_initial_and_update_sets_are_disjoint():
    assert not (INITIAL_CATEGORIES & UPDATE_CATEGORIES)


# ── _estimator_visible ─────────────────────────────────────────────────────


def test_estimator_sees_initial_and_own_deliverables():
    for cat in ["drawing", "specification", "estimate", "boq", "markup"]:
        assert _estimator_visible({"category": cat, "sent_to_estimators_at": None}) is True


def test_estimator_never_sees_unsent_updates():
    for cat in ["revision", "additional"]:
        assert _estimator_visible({"category": cat, "sent_to_estimators_at": None}) is False


def test_estimator_sees_sent_updates():
    for cat in ["revision", "additional"]:
        assert (
            _estimator_visible({"category": cat, "sent_to_estimators_at": "2026-07-01T00:00:00Z"})
            is True
        )


def test_estimator_never_sees_internal_categories():
    for cat in ["rfq_split", "quote", "proposal", "other"]:
        assert _estimator_visible({"category": cat, "sent_to_estimators_at": None}) is False


# ── FileNoteIn validation ──────────────────────────────────────────────────


def test_note_is_stripped():
    assert FileNoteIn(note="  sheet E-3 reissued  ").note == "sheet E-3 reissued"


def test_blank_note_rejected():
    with pytest.raises(ValidationError):
        FileNoteIn(note="   \n ")


def test_oversize_note_rejected():
    with pytest.raises(ValidationError):
        FileNoteIn(note="x" * 2001)


# ── updates_label ──────────────────────────────────────────────────────────


def test_label_revisions_only():
    assert ee.updates_label([{"category": "revision"}]) == "Changes/Revisions"


def test_label_additional_only():
    assert ee.updates_label([{"category": "additional"}]) == "Additional files"


def test_label_both():
    files = [{"category": "revision"}, {"category": "additional"}]
    assert ee.updates_label(files) == "Changes/Revisions & Additional files"


# ── email rendering ────────────────────────────────────────────────────────

PROJ = {"id": "p1", "name": "Van Ness <Tower>", "number": "26-014", "due_from_estimator_at": "2026-07-10"}

FILES = [
    {"category": "drawing", "filename": "E-101.pdf", "storage_path": "p1/drawing/a", "note": None},
    {"category": "specification", "filename": "spec.pdf", "storage_path": "p1/specification/b", "note": None},
    {
        "category": "revision",
        "filename": "E-101_rev2.pdf",
        "storage_path": "p1/revision/c",
        "note": "Panel schedule <changed> on sheet E-101",
    },
    {"category": "additional", "filename": "geotech.pdf", "storage_path": "p1/additional/d", "note": "FYI only"},
]

signer = lambda path: f"https://signed.example/{path}"  # noqa: E731


def test_sections_group_and_tag_initial_vs_updates():
    html = ee.render_sections(FILES, signer)
    assert "Electrical drawings" in html
    assert "Specifications" in html
    assert "Changes/Revisions" in html
    assert "Additional files" in html
    assert html.count(ee.INITIAL_TAG) == 2  # drawings + specs
    assert html.count(ee.UPDATE_TAG) == 2  # revision + additional


def test_sections_omit_empty_categories():
    html = ee.render_sections(FILES[:1], signer)
    assert "Electrical drawings" in html
    assert "Specifications" not in html
    assert ee.UPDATE_TAG not in html


def test_sections_link_via_signer_and_escape_notes():
    html = ee.render_sections(FILES, signer)
    assert "https://signed.example/p1/revision/c" in html
    # Notes render beside their file, HTML-escaped.
    assert "Panel schedule &lt;changed&gt; on sheet E-101" in html
    assert "<changed>" not in html


def test_package_email_greets_and_brands():
    html = ee.render_package_email(proj=PROJ, files=FILES, recipient_name="Jane Smith", signer=signer)
    assert "Hi Jane," in html
    assert "Van Ness &lt;Tower&gt;" in html  # project name escaped
    assert "26-014" in html
    assert "2026-07-10" in html  # due back
    assert ee.PORTAL_LINE_PACKAGE in html
    assert "G3 ELECTRICAL" in html  # branded shell
    assert "ESTIMATE FILES" in html  # header subtitle


def test_updates_email_includes_message_and_notes():
    html = ee.render_updates_email(proj=PROJ, files=FILES[2:], message="Addendum 2 dropped today", signer=signer)
    assert "Addendum 2 dropped today" in html
    assert "MESSAGE FROM THE G3 TEAM" in html
    assert "FYI only" in html
    assert "FILE UPDATES" in html


def test_updates_email_omits_empty_message_block():
    html = ee.render_updates_email(proj=PROJ, files=FILES[2:], message="  ", signer=signer)
    assert "MESSAGE FROM THE G3 TEAM" not in html
