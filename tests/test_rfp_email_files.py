"""The email harvester's pure parts (app/services/rfp_email_files) over the
attachment and link shapes seen on the dev database on 2026-09-16
(docs/RFP_HARVEST.md 2.5): the image policy (an octet-stream PDF is kept,
an octet-stream .png is an image, the declared type never keeps a file),
the signature test, attached emails, the trigger (`email_reference`) with
attachments only, links only, both, neither, images only and an
unsupported link alone, and the entry builders (never a locator).
"""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.services import rfp_email_files as ef
from tests import fixtures_email_harvest as fx


def _settings(**over):
    base = dict(rfp_ingest_enabled=True, rfp_harvest_enabled=True)
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings():
    return _settings()


@pytest.fixture
def links(monkeypatch):
    """`cloud_folders.find_share_links` as the trigger calls it: a link is
    found when its URL is in the text (the HTML argument must be None)."""
    cloud = fx.FakeCloud()
    cloud.links = [fx.SHAREPOINT_LINK, fx.DROPBOX_LINK, fx.SHAREFILE_LINK]
    monkeypatch.setattr(ef.cloud_folders, "find_share_links", cloud.find_share_links)
    return cloud


# ── Names and types ──────────────────────────────────────────────────────


def test_image_by_extension_or_declared_type_and_never_the_other_way():
    for name in ("a.png", "A.JPG", "x.jpeg", "b.gif", "c.bmp", "d.tif", "e.tiff", "f.webp",
                 "g.heic", "h.svg", "i.ico", fx.OCTET_PNG["name"]):
        assert ef.is_image_name(name), name
        assert ef.is_image(name, "application/octet-stream"), name
    for name in ("plans.pdf", "Drawings.zip", "spec.docx", "README", "", None, "png"):
        assert not ef.is_image_name(name), name
    # The declared type adds images; it never keeps a file.
    assert ef.is_image("weird-name", "image/png")
    assert ef.is_image("weird-name", "IMAGE/JPEG ")
    assert not ef.is_image(fx.OCTET_PDF["name"], "application/octet-stream")
    assert not ef.is_image("plans.pdf", "application/pdf")
    assert not ef.is_image("plans.pdf", None)
    assert ef.IMAGE_EXTENSIONS == frozenset(
        {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "heic", "svg", "ico"}
    )


def test_attached_email_by_kind_extension_or_type():
    assert ef.is_attached_email("Original invitation", None, "item")
    assert ef.is_attached_email("FW: Addendum 1.msg", "application/vnd.ms-outlook", "file")
    assert ef.is_attached_email("thread.EML", "application/octet-stream", "file")
    assert ef.is_attached_email("forwarded", "message/rfc822", "file")
    assert ef.is_attached_email("forwarded", "application/vnd.ms-outlook", "file")
    assert not ef.is_attached_email("plans.pdf", "application/pdf", "file")
    assert not ef.is_attached_email("plans.pdf", "application/octet-stream", "file")
    assert ef.is_zip_name("Drawings.ZIP") and ef.is_zip_name("a/b/c.zip")
    assert not ef.is_zip_name("Drawings.zip.pdf") and not ef.is_zip_name(None)


def test_signature_like_is_inline_or_an_outlook_name_or_small(settings):
    assert ef.signature_like("photo.jpg", 5 * 1024 * 1024, True, settings)
    assert ef.signature_like("image001.jpg", 27784, False, settings)
    assert ef.signature_like("IMAGE042.PNG", 500_000, False, settings)
    assert ef.signature_like("Outlook-3auvsbkr.jpg", 205845, False, settings)
    assert ef.signature_like("outlook-x.png", 500_000, False, settings)
    assert ef.signature_like("logo.png", 100 * 1024, False, settings)          # at the cap
    assert not ef.signature_like("logo.png", 100 * 1024 + 1, False, settings)  # over it
    assert not ef.signature_like("site-photo.jpg", 2 * 1024 * 1024, False, settings)
    assert not ef.signature_like("image1.jpg", 200_000, False, settings)       # two digits is not Outlook
    assert not ef.signature_like("my-outlook-notes.png", 200_000, False, settings)
    small = _settings(rfp_harvest_image_signature_max_bytes=10)
    assert not ef.signature_like("logo.png", 11, False, small)
    assert ef.signature_like(None, None, False, small)                         # no size is 0 bytes


# ── classify_attachments over the probe shapes ───────────────────────────


def test_classify_keeps_octet_stream_pdfs_and_skips_every_image(settings):
    plan = ef.classify_attachments(
        [fx.OCTET_PDF, fx.OUTLOOK_PNG, fx.OUTLOOK_JPG, fx.IMAGE001, fx.REAL_PDF, fx.OCTET_PNG,
         fx.SITE_PHOTO],
        settings,
    )
    assert [f["name"] for f in plan.files] == [fx.OCTET_PDF["name"], fx.REAL_PDF["name"]]
    assert plan.files[0] == {
        "name": fx.OCTET_PDF["name"], "size": 1328229,
        "content_type": "application/octet-stream", "id": None, "inline": False,
    }
    assert plan.images == [
        {"name": "Outlook-Global.png", "size": 7102, "inline": False, "signature_like": True},
        {"name": "Outlook-3auvsbkr.jpg", "size": 205845, "inline": False, "signature_like": True},
        {"name": "image001.jpg", "size": 27784, "inline": False, "signature_like": True},
        {"name": fx.OCTET_PNG["name"], "size": 220783, "inline": False, "signature_like": False},
        {"name": "site-photo.jpg", "size": 2 * 1024 * 1024, "inline": False, "signature_like": False},
    ]
    assert plan.skipped == [] and plan.references == 0


def test_classify_skips_attached_emails_and_unknown_kinds_and_keeps_inline_pdfs(settings):
    plan = ef.classify_attachments(
        [fx.MSG_FILE, fx.ITEM_ATTACHMENT, fx.INLINE_PDF,
         {"kind": "reference", "name": "Plans.pdf", "size": 0, "contentType": None},
         {"kind": "unknown", "name": "mystery", "size": 5, "contentType": "application/x-thing"},
         {"kind": "file", "name": "image002.png", "size": 3000, "contentType": "image/png", "inline": True},
         "not a dict", None],
        settings,
    )
    assert plan.skipped == [
        {"name": "FW: Addendum 1.msg", "size": 45000, "reason": "attached_email"},
        {"name": "Original invitation", "size": 12000, "reason": "attached_email"},
        {"name": "mystery", "size": 5, "reason": "unknown"},
    ]
    # Inline only matters for images: the inline PDF is a document.
    assert [(f["name"], f["inline"]) for f in plan.files] == [("Scope Letter.pdf", True)]
    assert plan.images == [{"name": "image002.png", "size": 3000, "inline": True, "signature_like": True}]
    assert plan.references == 1
    # The live listing's ids ride along; a missing kind is a file; sizes never negative.
    plan = ef.classify_attachments(
        [{"id": "att-1", "name": "a.pdf", "size": -4, "contentType": None},
         {"name": None, "size": "12", "contentType": ""}],
        settings,
    )
    assert plan.files[0] == {"name": "a.pdf", "size": 0, "content_type": None, "id": "att-1", "inline": False}
    assert plan.files[1]["name"] == "attachment" and plan.files[1]["size"] == 12
    assert ef.classify_attachments(None, settings) == ef.AttachmentPlan()


def test_downloadable_is_the_trigger_half_about_attachments():
    assert ef.downloadable(fx.OCTET_PDF) and ef.downloadable(fx.REAL_PDF)
    assert ef.downloadable(fx.INLINE_PDF) and ef.downloadable(fx.ZIP_FILE)
    assert ef.downloadable({"name": "x.pdf"})   # no kind: a file
    for meta in (fx.OUTLOOK_PNG, fx.OCTET_PNG, fx.SITE_PHOTO, fx.MSG_FILE, fx.ITEM_ATTACHMENT,
                 {"kind": "reference", "name": "Plans.pdf"}, {"kind": "unknown", "name": "x.pdf"},
                 "nope", None):
        assert not ef.downloadable(meta), meta


# ── email_reference: the trigger ─────────────────────────────────────────


def _row(**over):
    row = {"id": "e-1", "invitation_method": "organic", "attachments_meta": [], "body_text": ""}
    row.update(over)
    return row


def test_email_reference_answers_for_attachments_links_or_both_and_nothing_else(links):
    ref = ef.email_reference(_row(attachments_meta=[fx.OCTET_PDF]))
    assert ref == ef.EmailRef("email:e-1")
    assert ref.external_key == "email:e-1" and ref.external_url is None
    assert ref.session_provider is None
    # The row helpers read these two off any reference.
    assert ef.external_key_for("abc") == "email:abc"
    # Links only.
    assert ef.email_reference(_row(body_text=f"See {fx.SHAREPOINT_URL}")) == ef.EmailRef("email:e-1")
    # Both.
    assert ef.email_reference(
        _row(attachments_meta=[fx.REAL_PDF], body_text=f"See {fx.DROPBOX_URL}")
    ) == ef.EmailRef("email:e-1")
    # Neither.
    assert ef.email_reference(_row()) is None
    assert ef.email_reference(_row(body_text=None, attachments_meta=None)) is None
    assert ef.email_reference(_row(body_text="Please bid. https://aka.ms/LearnAboutSenderIdentification")) is None
    # Images only, attached emails only, a reference attachment only (the
    # cloud link it stands for is found at harvest time, never here).
    assert ef.email_reference(_row(attachments_meta=[fx.OUTLOOK_PNG, fx.IMAGE001, fx.SITE_PHOTO])) is None
    assert ef.email_reference(_row(attachments_meta=[fx.MSG_FILE, fx.ITEM_ATTACHMENT])) is None
    assert ef.email_reference(_row(attachments_meta=[{"kind": "reference", "name": "Plans.pdf"}])) is None
    # An unsupported ShareFile link alone still earns a harvest (download by hand).
    assert ef.email_reference(_row(body_text=fx.SHAREFILE_URL)) == ef.EmailRef("email:e-1")
    # The parser is asked about the text body only: the HTML is a harvest-time affair.
    assert all(call[2] is None for call in links.calls if call[0] == "find")


def test_email_reference_does_not_ask_the_parser_when_an_attachment_already_answers(links):
    ef.email_reference(_row(attachments_meta=[fx.OCTET_PDF], body_text=fx.SHAREPOINT_URL))
    assert links.calls == []


def test_email_reference_over_the_real_parser_and_the_probe_bodies():
    """The dev bodies: a SharePoint share, its Outlook-decorated twin, a
    Safe Links wrapped Dropbox folder and a ShareFile share all trigger; the
    sender-identification, GC website and Gmail image links never do."""
    for body in (
        fx.SHAREPOINT_URL,
        fx.SHAREPOINT_URL + "&xsdata=MDV8MDJ8&sdata=abc",
        "https://nam09.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.dropbox.com%2Fscl"
        "%2Ffo%2Fcv15gi89jjt514by214om%2FAHvlRf6u0-J2cRcsqe9n4R8%3Frlkey%3Dvt6nc04tsnd0b9b25kb3plckk"
        "%26dl%3D0&data=05%7C02%7C&reserved=0",
        fx.SHAREFILE_URL,
        "https://sletteninc.sharefile.com/public/share/web-s5030aec135b14046a3c2b01282109eb8",
    ):
        assert ef.email_reference(_row(body_text=f"Docs here: {body} thanks")) == ef.EmailRef("email:e-1"), body
    for body in (
        "https://aka.ms/LearnAboutSenderIdentification",
        "https://www.gc-website.example/projects https://www.linkedin.com/company/x",
        "https://linkprotect.cudasvc.com/url?a=https%3A%2F%2Fexample.com",
        "https://ci3.googleusercontent.com/proxy/abc",
        "",
        None,
    ):
        assert ef.email_reference(_row(body_text=body)) is None, body


# ── Entry builders ───────────────────────────────────────────────────────


_ENTRY_KEYS = {
    "file_path", "size", "kind", "discipline", "drawing_title", "revision", "sandbox_file_id",
    "status", "error", "origin", "provider", "link_key", "zip_of", "reused_harvest_id",
}


def test_entry_builders_carry_the_section_4_shape_and_never_a_locator():
    att = ef.attachment_entry("  Bid   Package.pdf ", 1328229)
    assert att == {
        "file_path": "Bid Package.pdf", "size": 1328229, "kind": None, "discipline": None,
        "drawing_title": None, "revision": None, "sandbox_file_id": None, "status": None,
        "error": None, "origin": "attachment", "provider": "graph", "link_key": None,
        "zip_of": None, "reused_harvest_id": None,
    }
    member = ef.zip_member_entry("Drawings.zip", "/Electrical//E1.01.pdf", "4096",
                                 provider="graph", link_key=None)
    assert member["file_path"] == "Drawings.zip/Electrical/E1.01.pdf"
    assert member["origin"] == "zip" and member["zip_of"] == "Drawings.zip"
    assert member["provider"] == "graph" and member["link_key"] is None and member["size"] == 4096
    link = ef.link_file_entry("Specs/26 05 00.pdf", 120500, provider="sharepoint",
                              link_key=fx.SHAREPOINT_KEY)
    assert link["file_path"] == "Specs/26 05 00.pdf" and link["origin"] == "link"
    assert link["provider"] == "sharepoint" and link["link_key"] == fx.SHAREPOINT_KEY
    assert link["zip_of"] is None
    dropbox_member = ef.link_file_entry("Folder/plan.pdf", 10, provider="dropbox",
                                        link_key=fx.DROPBOX_KEY, origin="zip")
    assert dropbox_member["origin"] == "zip" and dropbox_member["zip_of"] is None
    for entry in (att, member, link, dropbox_member):
        assert set(entry) == _ENTRY_KEYS
        dumped = json.dumps(entry)
        assert "graph:" not in dumped and "zip:" not in dumped and "http" not in dumped
        assert "|" not in dumped
    # Empty names fall back; paths are capped; sizes never negative.
    assert ef.attachment_entry(None, -1)["file_path"] == "attachment"
    assert ef.attachment_entry(None, -1)["size"] == 0
    assert ef.zip_member_entry("z.zip", "", 1, provider=None, link_key=None)["file_path"] == "z.zip/member"
    assert ef.link_file_entry("", 1, provider="gdrive", link_key="k")["file_path"] == "document"
    assert len(ef.link_file_entry("x" * 900, 1, provider="gdrive", link_key="k")["file_path"]) == 400
    assert ef.FILE_REUSED == "reused" and ef.FILE_EXPANDED == "expanded"
    assert ef.HARVESTER_EMAIL == "email" and ef.EMAIL_METHODS == ("organic", "general", "nonorganic")
