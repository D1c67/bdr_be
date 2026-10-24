"""WS8 anchors on WS1 words (spec section 6 WS8 step "anchors", section 5.1
coordinates) and the evidence inputs adapter.

Synthetic PDFs from tests/pagefacts_fixtures.py run through the real WS1
extraction (`LocalPdfInputs`): a /Rotate 90 page, an SHX-only page and a
two-scale sheet with a detail view and a schedule. The golden-local test
reads the 7154 backup E3.01 sheet (marked `golden`; skipped when the backup
is absent). No network, no database, no model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import plans_reader_anchors as anchors
from app.services import plans_reader_evidence_inputs as inputs_mod
from app.services.pagefacts.contracts import F_SHX, FactsUnavailable, Region, View, Word
from app.services.plans_reader_evidence_inputs import (
    FakeEvidenceInputs,
    LocalPdfInputs,
    SheetInputs,
    UnavailableEvidenceInputs,
)
from tests.pagefacts_fixtures import Shx, make_pdf, sheet, title_block_texts

DOC = "d0c5e7a1-1111-4222-8333-944455556666"

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
ELEC_PDF = (
    BACKUP
    / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011/drawing"
    / "c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(
    not ELEC_PDF.exists(), reason="7154 backup PDF not on this machine"
)


def _local(pdf: bytes) -> LocalPdfInputs:
    return LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=120.0)


def _near(a: float, b: float, tol: float = 0.004) -> bool:
    return abs(a - b) <= tol


def _word(t: str, x_in: float, y_in: float, *, w_in: float = 42.0, h_in: float = 30.0, f: int = 0):
    """A 7 pt word box from its left edge and baseline in sheet inches."""
    width = 0.1 * len(t)
    return Word(
        t=t,
        b=[x_in / w_in, (y_in - 0.1) / h_in, (x_in + width) / w_in, y_in / h_in],
        h=7.0,
        f=f,
    )


def _fake_sheet(words: list[Word], **kw) -> SheetInputs:
    base = {
        "document_id": DOC,
        "page": 3,
        "w_in": 42.0,
        "h_in": 30.0,
        "rotation": 0,
        "text_class": "cad_text",
        "words": words,
    }
    base.update(kw)
    return SheetInputs(**base)


# Pure helpers -------------------------------------------------------------------


def test_fold_token_and_normalize_tag():
    assert anchors.fold_token("L1L1") == "L1"
    assert anchors.fold_token("L5/1LH") == "L5"
    assert anchors.fold_token("L1/1HD") == "L1"
    assert anchors.fold_token("L1-EM") == "L1-EM"
    assert anchors.fold_token("l3\r\n") == "L3"
    assert anchors.fold_token("2X4") is None
    assert anchors.fold_token("L1 TROFFER") is None
    assert anchors.normalize_tag("l1-em") == "L1"
    assert anchors.normalize_tag("L3EM") == "L3"
    assert anchors.normalize_tag("L2/NL") == "L2"
    assert anchors.normalize_tag("EM") == "EM"
    tags = frozenset({"L1", "X1"})
    assert anchors.word_tag("L1-EM", tags) == "L1"
    assert anchors.word_tag("X1X1", tags) == "X1"
    assert anchors.word_tag("AND", tags) is None


def test_distance_helpers_convert_fractions_to_feet():
    a, b = (0.1, 0.1), (0.2, 0.1)
    assert _near(anchors.distance_in(a, b, 42.0, 30.0), 4.2, 1e-9)
    assert _near(anchors.distance_ft(a, b, 42.0, 30.0, 4.0), 16.8, 1e-9)
    assert anchors.distance_ft(a, b, 42.0, 30.0, None) is None
    assert anchors.ft_per_frac(42.0, 30.0, 4.0) == (168.0, 120.0)
    assert anchors.ft_per_frac(42.0, 30.0, None) is None


def test_doubled_words_fold_into_one_anchor_with_reading_order_refs():
    words = [
        _word("L1", 20.0, 12.0),
        _word("L1", 20.0, 12.0),  # CAD draws the tag twice
        _word("L1L1", 5.0, 12.0),
        _word("L5/1LH", 8.0, 6.0),
        _word("L1", 20.1, 12.02),  # within 0.15 in: still the same anchor
        _word("L1", 30.0, 12.0),
    ]
    out = anchors.extract_anchors(_fake_sheet(words), ["L1", "L5"])
    assert out.method == "text_layer" and out.complete
    assert out.counts() == {"L1": 3, "L5": 1}
    assert [(a.tag, round(a.x * 42)) for a in out.anchors] == [
        ("L5", 8),
        ("L1", 5),
        ("L1", 20),
        ("L1", 30),
    ]
    mid = out.by_tag("L1")[1]
    assert mid.folded == 3 and mid.ref == f"t:{DOC[:8]}:3:0" and mid.word_index == 0
    assert out.by_tag("L1-EM") == out.by_tag("L1")


def test_non_plan_regions_and_non_plan_sheets_are_excluded():
    regions = [
        Region("title_block", [0.9, 0.0, 1.0, 1.0]),
        Region("schedule", [0.7, 0.02, 0.89, 0.2], "LUMINAIRE SCHEDULE"),
        Region("plan_area", [0.0, 0.0, 0.9, 1.0]),
    ]
    words = [
        _word("L1", 10.0, 12.0),
        _word("L1", 31.0, 3.0),  # the schedule copy
        _word("L1", 39.0, 20.0),  # inside the title block
    ]
    out = anchors.extract_anchors(_fake_sheet(words, regions=regions, roles=["plan"]), ["L1"])
    assert out.counts() == {"L1": 1}
    assert out.excluded_counts() == {"non_plan_region": 2}
    assert sorted(e.where for e in out.excluded) == ["schedule", "title_block"]

    sched = anchors.extract_anchors(
        _fake_sheet(words, regions=regions, roles=["schedule"], page_role="schedule"), ["L1"]
    )
    assert sched.counts() == {"L1": 0} and sched.excluded_counts() == {"non_plan_sheet": 3}


def test_unusable_text_layer_and_missing_facts_answer_unknown_not_zero():
    scan = anchors.extract_anchors(_fake_sheet([], text_class="scanned"), ["L1", "L2"])
    assert scan.method == "unavailable" and not scan.available
    assert scan.counts() == {"L1": None, "L2": None}
    vec = anchors.extract_anchors(
        _fake_sheet([_word("L1", 1, 1)], text_class="vector_text"), ["L1"]
    )
    assert vec.counts() == {"L1": None}
    sparse = anchors.extract_anchors(_fake_sheet([_word("L1", 1, 1)], text_class="sparse"), ["L1"])
    assert sparse.available and not sparse.complete and sparse.counts() == {"L1": 1}

    missing = anchors.anchors_for_sheet(UnavailableEvidenceInputs(), DOC, 4, ["L1"])
    assert missing.method == "unavailable" and missing.counts() == {"L1": None}
    assert missing.notes and "not available" in missing.notes[0]


def test_demo_x_family_spreads_runs_and_reports_unknown_families():
    words = [_word("(X)(X)", 10.0, 5.0), _word("X)", 20.0, 5.0), _word("(X)L1", 30.0, 5.0)]
    out = anchors.extract_anchors(_fake_sheet(words), [], families=["demo_x", "circuit"])
    assert out.counts() == {"(X)": 3}
    xs = sorted(round(a.x * 42, 2) for a in out.anchors)
    assert xs[0] < xs[1] < 20.0 < xs[2]
    assert any("circuit" in n for n in out.notes)


def test_view_assignment_by_title_column():
    def view(vid: str, x_in: float, kind: str, ft: float | None, example: bool = False) -> View:
        box = [x_in / 42, 28.0 / 30, (x_in + 1.5) / 42, 28.2 / 30]
        return View(vid, f"T {vid}", None, "s", ft, kind=kind, anchor=box, example=example)

    views = [
        view("v1", 2.0, "plan", 4.0),
        view("v2", 17.0, "enlarged_plan", 2.0),
        view("v3", 29.0, "detail", 1.3333),
        view("v4", 10.0, "other", 8.0, example=True),
    ]
    at = anchors.view_at
    assert at(14 / 42, 0.5, views, 42, 30).view_id == "v1"
    assert at(19 / 42, 0.5, views, 42, 30).view_id == "v2"
    assert at(31 / 42, 0.5, views, 42, 30).view_id == "v3"
    assert at(1 / 42, 0.5, views, 42, 30).view_id == "v1"  # left of every title
    assert at(0.5, 0.5, [], 42, 30) is None
    assert at(0.5, 0.5, views[:1] + views[3:], 42, 30).view_id == "v1"  # examples skipped


# Adapter ------------------------------------------------------------------------


def test_fake_inputs_and_census_passthrough():
    fake = FakeEvidenceInputs(
        {(DOC, 3): _fake_sheet([_word("L2", 3, 3)])},
        {(DOC, 3, "L2"): {"count": 1, "method": "text_layer"}},
    )
    out = anchors.anchors_for_sheet(fake, DOC, 3, ["L2"])
    assert out.counts() == {"L2": 1}
    assert fake.census(DOC, 3, {"kind": "tag", "value": "L2"})["count"] == 1
    assert fake.census(DOC, 3, {"kind": "tag", "value": "L9"})["error"] == "unavailable"
    with pytest.raises(FactsUnavailable):
        fake.sheet(DOC, 9)
    assert UnavailableEvidenceInputs().census(DOC, 1, {"value": "L1"})["error"] == "unavailable"


def test_inputs_for_picks_a_provider():
    assert isinstance(inputs_mod.inputs_for(None), UnavailableEvidenceInputs)
    assert isinstance(inputs_mod.inputs_for(None, pdfs={DOC: b"%PDF"}), LocalPdfInputs)
    with pytest.raises(FactsUnavailable):
        inputs_mod.PagefactsApiInputs(None).sheet(DOC, 1)
    with pytest.raises(FactsUnavailable):
        LocalPdfInputs({DOC: b"not a pdf"}).sheet(DOC, 1)
    with pytest.raises(FactsUnavailable):
        LocalPdfInputs({}).sheet(DOC, 1)


# Synthetic PDF fixtures through WS1 extraction -------------------------------------

TAG_TEXTS = [
    (6.0, 8.0, 7, "L1"),
    (12.0, 9.0, 7, "L1L1"),
    (18.0, 14.0, 7, "L5/1LH"),
    (24.0, 20.0, 7, "X1"),
    (9.0, 22.0, 7, "L2"),
]


def _tag_sheet(rotate: int):
    texts = list(TAG_TEXTS) + title_block_texts("E3.01", "LIGHTING PLAN")
    texts += [(3.0, 27.6, 18, "LIGHTING PLAN - NEW WORK"), (3.0, 28.0, 7, '1/4" = 1\'-0"')]
    return sheet(*texts, rotate=rotate)


def test_rotate_90_page_anchors_land_in_the_displayed_frame():
    tags = ["L1", "L2", "L5", "X1"]
    flat = anchors.anchors_for_sheet(_local(make_pdf([_tag_sheet(0)])), DOC, 1, tags)
    rot_inputs = _local(make_pdf([_tag_sheet(90)]))
    rot = anchors.anchors_for_sheet(rot_inputs, DOC, 1, tags)
    assert rot_inputs.sheet(DOC, 1).rotation == 90
    assert rot.w_in == 42.0 and rot.h_in == 30.0
    assert flat.counts() == rot.counts() == {"L1": 2, "L2": 1, "L5": 1, "X1": 1}
    for a, b in zip(flat.anchors, rot.anchors, strict=True):
        assert a.tag == b.tag and _near(a.x, b.x) and _near(a.y, b.y)
    # Each anchor sits on its drawn tag (left edge and baseline in displayed inches).
    for x_in, y_in, _size, text in TAG_TEXTS:
        tag = anchors.normalize_tag(anchors.fold_token(text) or "")
        hits = [a for a in rot.by_tag(tag) if abs(a.bbox[0] * 42 - x_in) < 0.1]
        assert hits and abs(hits[0].bbox[3] * 30 - y_in) < 0.1
    assert {a.ft_per_in for a in rot.anchors} == {4.0}


def test_shx_only_page_gives_anchors_from_shx_words():
    shx = [
        Shx(6.0, 8.0, 0.25, 0.12, "L1"),
        Shx(12.0, 9.0, 0.25, 0.12, "L1"),
        Shx(18.0, 14.0, 0.25, 0.12, "L3"),
        Shx(18.02, 14.0, 0.25, 0.12, "L3"),  # duplicate within 0.15 in
        Shx(2.0, 1.0, 30.0, 0.2, "GENERAL NOTE " * 45),  # enough text for cad_text
    ]
    inputs = _local(make_pdf([sheet(shx=shx)]))
    sh = inputs.sheet(DOC, 1)
    assert all(w.f & F_SHX for w in sh.words) and sh.text_class == "cad_text"
    out = anchors.anchors_for_sheet(inputs, DOC, 1, ["L1", "L3"])
    assert out.counts() == {"L1": 2, "L3": 1}
    assert all(a.shx for a in out.anchors)
    l1 = out.by_tag("L1")[0]
    assert _near(l1.x, 6.125 / 42) and _near(l1.y, 8.06 / 30)

    # A page whose only text is a few SHX tags has no usable text layer.
    thin = _local(make_pdf([sheet(shx=shx[:2])]))
    assert anchors.anchors_for_sheet(thin, DOC, 1, ["L1"]).counts() == {"L1": None}


def _two_scale_sheet():
    texts = [
        (2.0, 27.6, 18, "FIRST FLOOR LIGHTING PLAN"),
        (2.0, 28.0, 7, '1/4" = 1\'-0"'),
        (17.0, 27.6, 18, "ENLARGED ELECTRICAL ROOM PLAN"),
        (17.0, 28.0, 7, '1/2" = 1\'-0"'),
        (29.0, 27.6, 18, "LIGHT POLE BASE DETAIL"),
        (29.0, 28.0, 7, '3/4" = 1\'-0"'),
        # first floor plan (1/4")
        (4.0, 10.0, 7, "L1"),
        (10.0, 12.0, 7, "L1"),
        (14.0, 20.0, 7, "L1"),
        # enlarged plan (1/2")
        (19.0, 10.0, 7, "L1"),
        (24.0, 15.0, 7, "L2"),
        # detail: never an anchor
        (31.0, 12.0, 7, "L1"),
        # luminaire schedule on the plan sheet: never an anchor
        (29.0, 2.0, 14, "LUMINAIRE SCHEDULE"),
        (29.0, 2.5, 7, "L1 2X4 LED TROFFER"),
        (29.0, 2.8, 7, "L2 6IN DOWNLIGHT"),
    ]
    texts += title_block_texts("E3.02", "LIGHTING PLANS")
    return sheet(*texts)


def test_two_scale_sheet_uses_each_views_scale_and_skips_detail_and_schedule():
    inputs = _local(make_pdf([_two_scale_sheet()]))
    sh = inputs.sheet(DOC, 1)
    kinds = {v.title: (v.kind, v.ft_per_in) for v in sh.views}
    assert kinds["FIRST FLOOR LIGHTING PLAN"] == ("plan", 4.0)
    assert kinds["ENLARGED ELECTRICAL ROOM PLAN"] == ("enlarged_plan", 2.0)
    assert kinds["LIGHT POLE BASE DETAIL"][0] == "detail"

    out = anchors.anchors_for_sheet(inputs, DOC, 1, ["L1", "L2"])
    assert out.counts() == {"L1": 4, "L2": 1}
    assert out.excluded_counts() == {"non_plan_region": 2, "non_plan_view": 1}
    by_x = {round(a.bbox[0] * 42): a for a in out.anchors}
    assert {by_x[4].ft_per_in, by_x[10].ft_per_in, by_x[14].ft_per_in} == {4.0}
    assert by_x[19].ft_per_in == 2.0 and by_x[24].ft_per_in == 2.0
    assert by_x[4].view_id != by_x[19].view_id

    # Paper inches times each view's own scale.
    d_plan = anchors.anchor_distance_ft(by_x[4], by_x[10], out.w_in, out.h_in)
    assert d_plan is not None and abs(d_plan - (6.0**2 + 2.0**2) ** 0.5 * 4.0) < 0.3
    d_enl = anchors.anchor_distance_ft(by_x[19], by_x[24], out.w_in, out.h_in)
    assert d_enl is not None and abs(d_enl - (5.0**2 + 5.0**2) ** 0.5 * 2.0) < 0.3
    assert anchors.anchor_distance_ft(by_x[4], by_x[19], out.w_in, out.h_in) is None
    payload = out.to_dict()
    assert payload["counts"] == {"L1": 4, "L2": 1} and len(payload["anchors"]) == 5


# Golden-local 7154 E3.01 ($0, the backup PDF) --------------------------------------


@golden
@needs_backup
def test_7154_e301_anchor_counts_match_estimator():
    """Spec WS8 acceptance: E3.01 anchors L1 21, L2 1, L3 16, L4 3, X1 2, L5 7
    (L1 21 = L1 16 + L1-EM 5; L3 16 = 13 + 3 L3-EM). Page 10 of the ELEC set,
    one 1/4" plan view."""
    elec = "c85a4720-0000-4000-8000-000000000010"
    inputs = LocalPdfInputs({elec: ELEC_PDF.read_bytes()})
    out = anchors.anchors_for_sheet(inputs, elec, 10, ["L1", "L2", "L3", "L4", "L5", "X1"])
    assert out.method == "text_layer" and out.complete
    assert out.counts() == {"L1": 21, "L2": 1, "L3": 16, "L4": 3, "L5": 7, "X1": 2}
    assert {a.ft_per_in for a in out.anchors} == {4.0}
    assert all(0.0 <= a.x <= 0.9 and 0.0 <= a.y <= 1.0 for a in out.anchors)
    # Demolition (X) tags on ED2.01 and ED3.01 (WS4 measured 34 and 39).
    demo = {
        p: anchors.anchors_for_sheet(inputs, elec, p, [], families=["demo_x"]).counts()["(X)"]
        for p in (7, 8)
    }
    assert demo == {7: 34, 8: 39}
    # The luminaire schedule sheet E0.04 never yields anchors.
    sched = anchors.anchors_for_sheet(inputs, elec, 5, ["L1", "L2", "L3"])
    assert sched.counts() == {"L1": 0, "L2": 0, "L3": 0} and sched.excluded
