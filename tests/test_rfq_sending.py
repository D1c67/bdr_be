"""Unit tests for RFQ sending — pure parts only (no DB / no Graph / no OpenAI)."""

import pytest

from app.services import openai_text
from app.services import rfq_sending as rs


# ── Date formatting ────────────────────────────────────────────────────────


def test_format_bid_datetime_matches_spec_example():
    # 15:00 UTC on 2026-06-24 is 11:00 AM in America/New_York (EDT).
    assert rs.format_bid_datetime("2026-06-24T15:00:00+00:00") == "Wednesday, June 24th 11:00 AM"


def test_format_bid_datetime_none_is_tbd():
    assert rs.format_bid_datetime(None) == "TBD"


def test_format_bid_datetime_ordinals():
    cases = {
        "2026-06-01T16:00:00+00:00": "1st",
        "2026-06-02T16:00:00+00:00": "2nd",
        "2026-06-03T16:00:00+00:00": "3rd",
        "2026-06-04T16:00:00+00:00": "4th",
        "2026-06-11T16:00:00+00:00": "11th",
        "2026-06-12T16:00:00+00:00": "12th",
        "2026-06-13T16:00:00+00:00": "13th",
        "2026-06-21T16:00:00+00:00": "21st",
        "2026-06-22T16:00:00+00:00": "22nd",
        "2026-06-23T16:00:00+00:00": "23rd",
    }
    for iso, suffix in cases.items():
        assert suffix in rs.format_bid_datetime(iso)


def test_format_bid_datetime_pm_and_noon():
    assert rs.format_bid_datetime("2026-06-24T16:00:00+00:00").endswith("12:00 PM")
    assert rs.format_bid_datetime("2026-06-24T04:00:00+00:00").endswith("12:00 AM")


# ── Subject / body templates ───────────────────────────────────────────────


def test_build_subject_format():
    proj = {
        "number": "26-104",
        "name": "Riverside Plaza",
        "actual_bid_at": "2026-06-24T15:00:00+00:00",
    }
    assert rs.build_subject(proj) == "26-104 - Riverside Plaza - BOM - Wednesday, June 24th 11:00 AM"


def test_build_subject_without_bid_date():
    assert rs.build_subject({"number": "1", "name": "X", "actual_bid_at": None}).endswith("- TBD")


def test_build_base_body_template():
    body = rs.build_base_body("Jane Smith", "Friday, June 19th 2:00 PM", None)
    assert body.startswith("Hello Jane Smith,")
    assert "we need them by Friday, June 19th 2:00 PM?" in body
    assert "Please also let me know what you are not able to quote." in body
    assert body.endswith("Thank you,\nThe G3 Estimating Team")
    assert "—" not in body and body.isascii()


def test_build_base_body_with_drawings_link():
    body = rs.build_base_body("Jane", "Friday, June 19th 2:00 PM", "https://1drv.ms/x")
    assert "The drawings are available here: https://1drv.ms/x" in body


def test_is_trenching():
    assert rs._is_trenching("Trenching")
    assert rs._is_trenching("trenching")
    assert not rs._is_trenching("Switchgear")
    assert not rs._is_trenching(None)


# ── PE-edited body ─────────────────────────────────────────────────────────


def test_build_custom_body_substitutes_contact_name():
    out = rs.build_custom_body("Hello <Contact Name>,\n\nPlease quote.", "Jane Smith", None)
    assert out == "Hello Jane Smith,\n\nPlease quote."


def test_build_custom_body_appends_missing_drawings_link():
    out = rs.build_custom_body("Hi <Contact Name>", "Jane", "https://1drv.ms/x")
    assert out.endswith("The drawings are available here: https://1drv.ms/x")


def test_build_custom_body_keeps_existing_drawings_link():
    template = "Drawings: https://1drv.ms/x\n\nThanks, <Contact Name>"
    out = rs.build_custom_body(template, "Jane", "https://1drv.ms/x")
    assert out.count("https://1drv.ms/x") == 1


def test_email_preview_template_contains_placeholder():
    # The editable template the UI shows must carry the exact token the
    # backend substitutes per recipient.
    body = rs.build_base_body(rs.CONTACT_NAME_PLACEHOLDER, "Friday, June 19th 2:00 PM", None)
    assert rs.CONTACT_NAME_PLACEHOLDER in body


# ── Explicit attachment overrides ──────────────────────────────────────────

_PROJECT = {"id": "p1", "number": "26-104"}
_FILES = {
    "a": {"filename": "bom.xlsx", "content": b"x", "category": "rfq_split"},
    "b": {"filename": "plan.pdf", "content": b"y", "category": "drawing"},
}


def test_explicit_attachments_small_drawings_stay_inline():
    atts, link = rs._ExplicitAttachments(dict(_FILES), _PROJECT).for_group(["a", "b", "a"])
    assert [f["filename"] for f in atts] == ["bom.xlsx", "plan.pdf"]  # deduped, ordered
    assert link is None


def test_explicit_attachments_oversize_decision_is_per_group(monkeypatch):
    monkeypatch.setattr(
        rs, "get_settings", lambda: type("S", (), {"rfq_drawings_inline_limit_mb": 1})()
    )
    uploads: list[str] = []
    monkeypatch.setattr(rs.graph_email, "drive_upload", lambda p, c: uploads.append(p))
    monkeypatch.setattr(rs.graph_email, "drive_get_item_id", lambda folder: folder)
    monkeypatch.setattr(rs.graph_email, "drive_create_link", lambda item: f"link:{item}")
    files = {
        "a": _FILES["a"],
        "big": {"filename": "site/plan.pdf", "content": b"x" * (2 * 1024 * 1024), "category": "drawing"},
        "small": {"filename": "detail.pdf", "content": b"y", "category": "drawing"},
    }
    ex = rs._ExplicitAttachments(files, _PROJECT)

    # Over-limit group: its drawings move to a selection-specific folder link.
    atts, link = ex.for_group(["a", "big"])
    assert [f["filename"] for f in atts] == ["bom.xlsx"]
    assert link and link.startswith("link:BDR/26-104/rfq-drawings/")
    assert ex.used_link
    # Path components are sanitized (the "/" in the filename can't escape).
    assert all("site/plan" not in p for p in uploads)

    # Same selection again reuses the upload + link.
    n_uploads = len(uploads)
    _, link_again = ex.for_group(["big"])
    assert link_again == link and len(uploads) == n_uploads

    # A different group under the limit stays inline — never inherits the link.
    atts2, link2 = ex.for_group(["a", "small"])
    assert [f["filename"] for f in atts2] == ["bom.xlsx", "detail.pdf"]
    assert link2 is None

    # No drawings selected -> nothing to link.
    _, link3 = ex.for_group(["a"])
    assert link3 is None


def test_safe_component_strips_path_separators():
    assert "/" not in rs._safe_component("a/b\\c:d")
    assert rs._safe_component("  ") == "file"


# ── Default drawings set: electrical-first with general fallback (0099) ────


def test_prepare_drawings_prefers_electrical_set(monkeypatch):
    # When an electrical set exists, vendors get ONLY it — the general set
    # (full plans) stays home.
    by_cat = {
        "electrical_drawing": [{"filename": "E-201.pdf", "content": b"e"}],
        "drawing": [{"filename": "full-set.pdf", "content": b"g"}],
    }
    monkeypatch.setattr(rs, "_load_files", lambda sb, pid, cat: list(by_cat.get(cat, [])))
    atts, link = rs._prepare_drawings(None, _PROJECT)
    assert [f["filename"] for f in atts] == ["E-201.pdf"]
    assert link is None


def test_prepare_drawings_falls_back_to_general(monkeypatch):
    # Projects that predate the split have no electrical bucket; the general
    # set still goes out so a vendor email never ships without plans.
    by_cat = {"drawing": [{"filename": "full-set.pdf", "content": b"g"}]}
    monkeypatch.setattr(rs, "_load_files", lambda sb, pid, cat: list(by_cat.get(cat, [])))
    atts, link = rs._prepare_drawings(None, _PROJECT)
    assert [f["filename"] for f in atts] == ["full-set.pdf"]
    assert link is None


def test_bulk_send_schema_normalizes_blank_body_and_defaults():
    from app.models.schemas import RFQBulkSendIn

    m = RFQBulkSendIn(
        groups=[{"rfq_id": "r", "vendor_contact_ids": ["c"]}], email_body="  \n "
    )
    assert m.email_body is None
    assert m.groups[0].attachment_file_ids is None
    assert m.groups[0].cc is None

    m2 = RFQBulkSendIn(
        groups=[{"rfq_id": "r", "vendor_contact_ids": ["c"], "attachment_file_ids": []}],
        email_body="Hello <Contact Name>",
    )
    assert m2.email_body == "Hello <Contact Name>"
    assert m2.groups[0].attachment_file_ids == []


# ── CC (same-company copies) ───────────────────────────────────────────────


def test_bulk_send_schema_cc_normalization():
    from app.models.schemas import RFQBulkSendIn

    m = RFQBulkSendIn(
        groups=[
            {
                "rfq_id": "r",
                "vendor_contact_ids": ["a", "b"],
                # duplicates deduped; To recipients ("a", "b") never double as
                # CC; an emptied list is pruned; an all-empty map becomes None.
                "cc": {"a": ["x", "x", "b", "a"], "b": ["a"]},
            }
        ]
    )
    assert m.groups[0].cc == {"a": ["x"]}

    m2 = RFQBulkSendIn(
        groups=[{"rfq_id": "r", "vendor_contact_ids": ["a"], "cc": {}}]
    )
    assert m2.groups[0].cc is None


def test_bulk_send_schema_cc_rejects_unknown_key_and_oversize_list():
    from pydantic import ValidationError

    from app.models.schemas import RFQBulkSendIn

    with pytest.raises(ValidationError, match="recipient contact ids"):
        RFQBulkSendIn(
            groups=[{"rfq_id": "r", "vendor_contact_ids": ["a"], "cc": {"z": ["x"]}}]
        )
    with pytest.raises(ValidationError, match="At most 10"):
        RFQBulkSendIn(
            groups=[
                {
                    "rfq_id": "r",
                    "vendor_contact_ids": ["a"],
                    "cc": {"a": [f"c{i}" for i in range(11)]},
                }
            ]
        )


_ANN = {"id": "t1", "name": "Ann", "email": "ann@acme.com", "vendor_id": "v1"}
_BOB = {"id": "c1", "name": "Bob", "email": "bob@acme.com", "vendor_id": "v1"}
_EVE = {"id": "c2", "name": "Eve", "email": "eve@other.com", "vendor_id": "v2"}


def test_resolve_cc_same_company_ok_and_missing_to_skipped():
    out = rs._resolve_cc({"t1": _ANN, "c1": _BOB}, {"t1": ["c1"], "gone": ["c1"]})
    assert out == {"t1": [_BOB]}
    assert rs._resolve_cc({"t1": _ANN}, None) == {}


def test_resolve_cc_rejects_cross_company():
    with pytest.raises(ValueError, match="same company"):
        rs._resolve_cc({"t1": _ANN, "c2": _EVE}, {"t1": ["c2"]})


def test_resolve_cc_rejects_unknown_cc_contact():
    with pytest.raises(ValueError, match="not found"):
        rs._resolve_cc({"t1": _ANN}, {"t1": ["nope"]})


class _FakeSB:
    """Captures inserts/upserts per table; every execute returns one row."""

    def __init__(self):
        self.inserts: dict[str, list[dict]] = {}
        self.upserts: dict[str, list[dict]] = {}

    def table(self, name):
        fake = self

        class _T:
            def insert(self, row):
                fake.inserts.setdefault(name, []).append(row)
                return self

            def upsert(self, row, on_conflict=None):
                fake.upserts.setdefault(name, []).append(row)
                return self

            def execute(self):
                return type("R", (), {"data": [{"id": f"{name}-1"}]})()

        return _T()


def test_send_one_ccs_same_company_contacts(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        rs.graph_email,
        "create_draft",
        lambda to, subject, body, html=False, cc=None, sender=None: (
            captured.update(to=to, cc=cc)
            or {"id": "d1", "conversationId": "cv1", "internetMessageId": "im1"}
        ),
    )
    monkeypatch.setattr(rs.graph_email, "add_attachment", lambda *a, **k: None)
    monkeypatch.setattr(rs.graph_email, "send_draft", lambda _id: None)
    monkeypatch.setattr(rs, "vary_email_body", lambda base, must_contain: base)
    monkeypatch.setattr(rs, "audit", lambda *a, **k: None)
    sb = _FakeSB()

    res = rs._send_one(
        sb,
        project={"id": "p1"},
        rfq={"id": "r1"},
        category_name="Switchgear",
        contact=_ANN,
        cc_contacts=[_BOB],
        subject="subj",
        due_str="Friday, June 19th 2:00 PM",
        attachments=[],
        drawings_link=None,
        custom_body=None,
        user_id="u1",
    )
    assert res["status"] == "sent"
    assert captured["to"] == "ann@acme.com"
    assert captured["cc"] == ["bob@acme.com"]
    # The ledger row carries both addresses; the send row snapshots the CC.
    assert sb.inserts["email_log"][0]["to_addrs"] == "ann@acme.com, bob@acme.com"
    assert sb.inserts["rfq_sends"][0]["cc_recipients"] == [
        {"vendor_contact_id": "c1", "name": "Bob", "email": "bob@acme.com"}
    ]


def test_send_one_without_cc_keeps_single_recipient(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        rs.graph_email,
        "create_draft",
        lambda to, subject, body, html=False, cc=None, sender=None: (
            captured.update(to=to, cc=cc)
            or {"id": "d1", "conversationId": "cv1", "internetMessageId": "im1"}
        ),
    )
    monkeypatch.setattr(rs.graph_email, "add_attachment", lambda *a, **k: None)
    monkeypatch.setattr(rs.graph_email, "send_draft", lambda _id: None)
    monkeypatch.setattr(rs, "vary_email_body", lambda base, must_contain: base)
    monkeypatch.setattr(rs, "audit", lambda *a, **k: None)
    sb = _FakeSB()

    res = rs._send_one(
        sb,
        project={"id": "p1"},
        rfq={"id": "r1"},
        category_name="Switchgear",
        contact=_ANN,
        subject="subj",
        due_str="Friday, June 19th 2:00 PM",
        attachments=[],
        drawings_link=None,
        custom_body=None,
        user_id="u1",
    )
    assert res["status"] == "sent"
    assert captured["cc"] is None
    assert sb.inserts["email_log"][0]["to_addrs"] == "ann@acme.com"
    assert sb.inserts["rfq_sends"][0]["cc_recipients"] is None


# ── OpenAI rewrite guardrails ──────────────────────────────────────────────

BASE = rs.build_base_body("Jane Smith", "Friday, June 19th 2:00 PM", None)
TOKENS = ["Jane Smith", "Friday, June 19th 2:00 PM", rs.SIGNOFF]


def test_rewrite_rejected_when_token_dropped():
    assert not openai_text._rewrite_acceptable(BASE.replace("Jane Smith", "Jane"), BASE, TOKENS)


def test_rewrite_rejected_on_em_dash_or_non_ascii():
    assert not openai_text._rewrite_acceptable(BASE + " —", BASE, TOKENS)
    assert not openai_text._rewrite_acceptable(BASE + " ✉", BASE, TOKENS)


def test_rewrite_rejected_when_empty_or_bloated():
    assert not openai_text._rewrite_acceptable("", BASE, TOKENS)
    assert not openai_text._rewrite_acceptable(BASE + "x" * (2 * len(BASE)), BASE, TOKENS)


def test_rewrite_accepted_when_faithful():
    varied = BASE.replace("Can you please get me quotes", "Could you please send me quotes")
    assert openai_text._rewrite_acceptable(varied, BASE, TOKENS)


def test_vary_email_body_falls_back_without_api_key(monkeypatch):
    # No OPENAI_API_KEY configured -> the base template is used untouched.
    from app.core.config import Settings

    monkeypatch.setattr(
        openai_text, "get_settings", lambda: Settings(_env_file=None, openai_api_key="")
    )
    assert openai_text.vary_email_body(BASE, TOKENS) == BASE


# ── BOM → immutable PDF conversion ──────────────────────────────────────────


def test_as_immutable_pdf_converts_office_bom(monkeypatch):
    monkeypatch.setattr(rs.office_preview, "convert_for_send", lambda content, name: b"%PDF-data")
    out = rs._as_immutable_pdf(
        {"filename": "bom.xlsx", "content": b"xl", "category": "rfq_split"}
    )
    assert out["filename"] == "bom.pdf"
    assert out["content"] == b"%PDF-data"
    assert out["category"] == "rfq_split"  # other keys preserved


def test_as_immutable_pdf_passes_through_drawings(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("drawings (already PDFs) must not be converted")

    monkeypatch.setattr(rs.office_preview, "convert_for_send", _boom)
    f = {"filename": "plan.pdf", "content": b"pdfbytes"}
    assert rs._as_immutable_pdf(f) is f


def test_as_immutable_pdf_propagates_conversion_error(monkeypatch):
    def _fail(content, name):
        raise rs.office_preview.ConversionError("gotenberg down")

    monkeypatch.setattr(rs.office_preview, "convert_for_send", _fail)
    with pytest.raises(rs.office_preview.ConversionError, match="gotenberg down"):
        rs._as_immutable_pdf({"filename": "bom.xlsx", "content": b"x"})


def test_record_failed_send_inserts_and_returns_result():
    inserted: dict = {}

    class _Q:
        def insert(self, row):
            inserted.update(row)
            return self

        def execute(self):
            return type("R", (), {"data": [{}]})()

    class _SB:
        def table(self, _):
            return _Q()

    res = rs._record_failed_send(_SB(), {"id": "r1"}, {"id": "c1"}, "subj", "boom", "u1")
    assert res == {
        "rfq_id": "r1",
        "vendor_contact_id": "c1",
        "status": "failed",
        "error": "boom",
    }
    assert inserted["status"] == "failed"
    assert inserted["error"] == "boom"
    assert inserted["rfq_id"] == "r1"
    assert inserted["vendor_contact_id"] == "c1"
