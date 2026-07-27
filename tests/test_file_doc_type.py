"""The plans-vs-specs axis on post-hand-off files (0077).

`doc_type` is a SECOND axis alongside `category`: category says what kind of
thing a file is (revision / addendum / …), doc_type says WHICH DOCUMENT SET it
belongs to (the plans or the specs). These tests pin the three places that
axis has to hold together:

  * upload_file — the domain, the category pairing, and the one place it is
    REQUIRED (revisions) vs merely allowed (addenda);
  * _clean_section_notes — the per-send "what changed in the plans / in the
    specs" notes, which must match the batch's actual contents;
  * estimator_email.render_sections — the estimator's copy, where the split has
    to become two separate sections or the whole feature is invisible to them.

The two `section_key` implementations (app.core.file_categories owns it,
estimator_email keeps a leaf copy) are pinned against each other here, because a
silent drift between them would file notes under keys the renderer never reads.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core.file_categories import (
    DOC_TYPE_CATEGORIES,
    DOC_TYPE_REQUIRED_CATEGORIES,
    DOC_TYPES,
    SECTION_NOTE_KEYS,
    SECTION_NOTE_REQUIRED_KEYS,
    section_key,
)
from app.routers.estimator import _clean_section_notes
from app.services import estimator_email as ee
from tests.test_file_updates import _do_upload, _estimator, _upload_env, _writer

# ── The vocabulary itself ──────────────────────────────────────────────────


def test_doc_type_categories_are_the_split_ones():
    # Orthogonality is the design: doc_type applies to the post-hand-off
    # categories only. 'drawing'/'specification' ARE the document set, and
    # 'additional' is by definition neither, so none of them may carry one.
    assert DOC_TYPE_CATEGORIES == {"revision", "addendum"}
    assert not DOC_TYPE_CATEGORIES & {"drawing", "specification", "additional"}
    # Required only where the modal always knows the answer.
    assert DOC_TYPE_REQUIRED_CATEGORIES == {"revision"}
    assert DOC_TYPE_REQUIRED_CATEGORIES < DOC_TYPE_CATEGORIES


def test_section_key_suffixes_only_split_categories():
    assert section_key("revision", "drawing") == "revision:drawing"
    assert section_key("addendum", "specification") == "addendum:specification"
    # A legacy revision with no recorded document set keys to the bare category,
    # so it shares the untitled group it already rendered in.
    assert section_key("revision", None) == "revision"
    # doc_type on a category that cannot carry one is ignored, never suffixed.
    assert section_key("additional", "drawing") == "additional"


def test_section_note_keys_cover_every_renderable_section():
    for category, doc_type, _title in ee.SECTION_TITLES:
        if category in ("drawing", "specification"):
            continue  # the initial package is not a Revisions-modal section
        assert section_key(category, doc_type) in SECTION_NOTE_KEYS


def test_email_section_key_matches_the_core_one():
    # Two implementations, one meaning — a drift here files notes under keys the
    # renderer never looks up, so they'd silently vanish from the estimator's copy.
    for category in ("revision", "addendum", "additional", "drawing"):
        for doc_type in (None, "drawing", "specification"):
            assert ee.section_key(category, doc_type) == section_key(category, doc_type)


def test_required_section_notes_are_the_revision_ones():
    assert SECTION_NOTE_REQUIRED_KEYS == {"revision:drawing", "revision:specification"}
    assert SECTION_NOTE_REQUIRED_KEYS <= SECTION_NOTE_KEYS


# ── upload_file ────────────────────────────────────────────────────────────


async def test_revision_upload_records_doc_type(monkeypatch):
    sb = _upload_env(monkeypatch)
    monkeypatch.setattr("app.routers.files.handoff_locked", lambda _pid: True)
    await _do_upload(
        project_id="p1",
        category="revision",
        note="sheet E-301 reissued",
        doc_type="drawing",
        user=_writer(),
    )
    assert sb.inserted["doc_type"] == "drawing"


async def test_revision_upload_without_doc_type_refused(monkeypatch):
    # The Revisions modal always knows which section the file came from, so a
    # revision with no document set is a bug in the caller, not a legacy row.
    _upload_env(monkeypatch)
    monkeypatch.setattr("app.routers.files.handoff_locked", lambda _pid: True)
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1", category="revision", note="what changed", user=_writer()
        )
    assert exc.value.status_code == 400


async def test_unknown_doc_type_refused(monkeypatch):
    _upload_env(monkeypatch)
    monkeypatch.setattr("app.routers.files.handoff_locked", lambda _pid: True)
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1",
            category="revision",
            note="what changed",
            doc_type="plans",  # the UI's word, not the stored vocabulary
            user=_writer(),
        )
    assert exc.value.status_code == 400
    assert DOC_TYPES == {"drawing", "specification"}


async def test_doc_type_on_a_drawing_refused(monkeypatch):
    # Mirrors project_files_doc_type_ck: the initial package's category IS the
    # document set, so a doc_type there would be a second, conflicting answer.
    _upload_env(monkeypatch)
    monkeypatch.setattr("app.routers.files.handoff_locked", lambda _pid: False)
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1", category="drawing", doc_type="drawing", user=_writer()
        )
    assert exc.value.status_code == 400


async def test_addendum_doc_type_is_optional(monkeypatch):
    # The initial "Upload plans and specs" modal uploads addenda and never asks,
    # so an addendum without one must still be accepted — and stored as NULL.
    sb = _upload_env(monkeypatch, forbid_lock=True)
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    await _do_upload(
        project_id="p1",
        category="addendum",
        addendum_number="3",
        addendum_issued_on=tomorrow,
        user=_writer(),
    )
    assert sb.inserted["doc_type"] is None


async def test_addendum_accepts_doc_type(monkeypatch):
    sb = _upload_env(monkeypatch, forbid_lock=True)
    await _do_upload(
        project_id="p1",
        category="addendum",
        addendum_number="3",
        addendum_issued_on="2026-03-08",
        doc_type="specification",
        user=_writer(),
    )
    assert sb.inserted["doc_type"] == "specification"


async def test_estimator_still_cannot_smuggle_a_revision(monkeypatch):
    # doc_type must not become a second path into the update categories: the
    # ESTIMATOR_WRITE gate fires first, before any doc_type validation.
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1",
            category="revision",
            note="n",
            doc_type="drawing",
            user=_estimator(),
        )
    assert exc.value.status_code == 403


# ── _clean_section_notes ───────────────────────────────────────────────────


def _f(category, doc_type=None):
    return {"id": f"{category}-{doc_type}", "category": category, "doc_type": doc_type}


def test_section_notes_kept_for_present_sections():
    files = [_f("revision", "drawing"), _f("revision", "specification"), _f("additional")]
    out = _clean_section_notes(
        {
            "revision:drawing": "  Panel schedule reissued  ",
            "revision:specification": "16123 conductor sizes updated",
            "additional": "FYI only",
        },
        files,
    )
    assert out == {
        "revision:drawing": "Panel schedule reissued",
        "revision:specification": "16123 conductor sizes updated",
        "additional": "FYI only",
    }


def test_blank_section_note_is_not_an_answer():
    # "" must not satisfy the requirement — it renders as nothing, so accepting
    # it would deliver a revision section with no explanation at all.
    with pytest.raises(HTTPException) as exc:
        _clean_section_notes({"revision:drawing": "   \n "}, [_f("revision", "drawing")])
    assert exc.value.status_code == 400


def test_missing_required_section_note_refused():
    files = [_f("revision", "drawing"), _f("revision", "specification")]
    with pytest.raises(HTTPException) as exc:
        _clean_section_notes({"revision:drawing": "plans changed"}, files)
    assert exc.value.status_code == 400


def test_unknown_section_note_key_refused():
    with pytest.raises(HTTPException) as exc:
        _clean_section_notes({"revision:plans": "oops"}, [_f("revision", "drawing")])
    assert exc.value.status_code == 400


def test_section_note_for_an_absent_section_refused():
    # A note nothing renders is a note the author believes was delivered.
    with pytest.raises(HTTPException) as exc:
        _clean_section_notes(
            {
                "revision:drawing": "plans changed",
                "revision:specification": "…but no spec file is in this batch",
            },
            [_f("revision", "drawing")],
        )
    assert exc.value.status_code == 400


def test_addenda_note_keyed_by_bare_category_is_accepted():
    # The modal keeps addenda in ONE box whose files are tagged plans/specs per
    # row, so its single note arrives keyed "addendum" while its files key to
    # "addendum:drawing"/"addendum:specification".
    out = _clean_section_notes(
        {"addendum": "Addendum 3 — narrative plus two reissued sheets"},
        [_f("addendum", "drawing"), _f("addendum", "specification")],
    )
    assert out == {"addendum": "Addendum 3 — narrative plus two reissued sheets"}


def test_addenda_note_with_no_addenda_still_refused():
    with pytest.raises(HTTPException) as exc:
        _clean_section_notes({"addendum": "orphan"}, [_f("additional")])
    assert exc.value.status_code == 400


def test_addenda_only_batch_needs_no_section_note():
    # Addenda identify themselves by number + issue date; 'additional' already
    # requires a per-file note. Neither is forced to repeat itself.
    assert _clean_section_notes(None, [_f("addendum", "drawing"), _f("additional")]) == {}


def test_legacy_revision_without_doc_type_needs_no_section_note():
    # A colleague's pre-0077 unsent draft keys to plain "revision", which is not
    # in SECTION_NOTE_REQUIRED_KEYS — so seeding it into a batch cannot make the
    # send unsatisfiable.
    assert _clean_section_notes(None, [_f("revision", None)]) == {}


def test_oversize_section_note_refused():
    with pytest.raises(HTTPException) as exc:
        _clean_section_notes({"revision:drawing": "x" * 2001}, [_f("revision", "drawing")])
    assert exc.value.status_code == 400


# ── The estimator's copy: render_sections ──────────────────────────────────


def _ef(category, doc_type, filename, note=None):
    return {
        "category": category,
        "doc_type": doc_type,
        "filename": filename,
        "storage_path": f"p1/{filename}",
        "note": note,
    }


def _render(files, section_notes=None):
    return ee.render_sections(files, lambda p: f"https://x/{p}", section_notes)


def test_revisions_render_as_two_sections():
    html = _render(
        [
            _ef("revision", "drawing", "E-301-revA.pdf"),
            _ef("revision", "specification", "16123-revA.pdf"),
        ]
    )
    assert "Revised plans/drawings" in html
    assert "Revised specifications" in html
    # Plans first, then specs — the same order the modal collects them in.
    assert html.index("Revised plans/drawings") < html.index("Revised specifications")
    # Each file sits under its own heading, not in one merged list.
    assert html.index("E-301-revA.pdf") < html.index("Revised specifications")


def test_addenda_split_by_doc_type():
    html = _render(
        [
            _ef("addendum", "drawing", "add3-sheets.pdf"),
            _ef("addendum", "specification", "add3-specs.pdf"),
        ]
    )
    assert "Addenda — plans/drawings" in html
    assert "Addenda — specifications" in html


def test_legacy_files_keep_their_undivided_section():
    # Pre-0077 rows have no doc_type and must still render — under the original
    # title, never dropped because they match no split section.
    html = _render([_ef("revision", None, "old-rev.pdf")])
    assert "Changes/Revisions" in html
    assert "old-rev.pdf" in html
    assert "Revised plans/drawings" not in html


def test_section_notes_render_under_their_heading():
    html = _render(
        [_ef("revision", "drawing", "E-301-revA.pdf", note="sheet E-301 reissued")],
        {"revision:drawing": "Panel schedule revised throughout"},
    )
    # Section note between the heading and the file; the per-file note survives.
    assert html.index("Revised plans/drawings") < html.index("Panel schedule revised throughout")
    assert html.index("Panel schedule revised throughout") < html.index("E-301-revA.pdf")
    assert "sheet E-301 reissued" in html


def test_section_note_is_escaped():
    html = _render(
        [_ef("revision", "drawing", "a.pdf")],
        {"revision:drawing": "<script>alert(1)</script>"},
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_bare_addendum_note_heads_both_addenda_sections():
    html = _render(
        [
            _ef("addendum", "drawing", "add3-sheets.pdf"),
            _ef("addendum", "specification", "add3-specs.pdf"),
        ],
        {"addendum": "Addendum 3 — two reissued sheets and one spec section"},
    )
    assert html.count("Addendum 3 — two reissued sheets and one spec section") == 2


def test_a_note_for_an_unrendered_section_shows_nowhere():
    html = _render([_ef("revision", "drawing", "a.pdf")], {"additional": "orphan"})
    assert "orphan" not in html


def test_every_file_lands_in_exactly_one_section():
    files = [
        _ef("drawing", None, "one.pdf"),
        _ef("specification", None, "two.pdf"),
        _ef("addendum", "drawing", "three.pdf"),
        _ef("addendum", "specification", "four.pdf"),
        _ef("addendum", None, "five.pdf"),
        _ef("revision", "drawing", "six.pdf"),
        _ef("revision", "specification", "seven.pdf"),
        _ef("revision", None, "eight.pdf"),
        _ef("additional", None, "nine.pdf"),
    ]
    html = _render(files)
    for f in files:
        # Twice: once in the signed href, once as the link text. Never three
        # times (two sections claiming it) and never zero (dropped on the floor).
        assert html.count(f["filename"]) == 2, f["filename"]
