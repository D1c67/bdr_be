"""Hazard inventory over the PDFium raw API: what active content a document
and each of its pages carry. Runs inside the sandbox child only.

Hazards never reject a file. They are audit flags the parent stores on the
file row (document level) and sums across page events (page level) so a
reviewer, and later the agent slice, can see that a drawing set arrived with
JavaScript, launch actions or embedded files even though rasterizing to JPEG
neutralized all of it. Every counter is an int and every key comes from
`protocol.DOC_HAZARD_KEYS` / `protocol.PAGE_HAZARD_KEYS`; nothing from the
file (no URIs, no file names, no script text) is ever copied into an event.

Document level (`document_hazards`):
  javascript_actions  FPDFDoc_GetJavaScriptActionCount: entries of the
                      document's /Names /JavaScript tree. An /OpenAction that
                      runs script is NOT visible through the public PDFium
                      API; the parent's byte-marker scan (`/OpenAction`,
                      `/JS`) covers that side.
  attachments         FPDFDoc_GetAttachmentCount: the /EmbeddedFiles tree.
  xfa_packets         FPDF_GetXFAPacketCount: the number of packets in the
                      /AcroForm /XFA entry as this (non-XFA) PDFium build
                      exposes it; negative (no XFA, or unreadable) is 0.

Page level (`page_hazards`), all via a loaded FPDF_PAGE:
  uri_links / launch_actions / remote_goto / embedded_goto
                      FPDFLink_Enumerate over the page's /Link annotations,
                      classified by FPDFAction_GetType. Links that only carry
                      a destination (an in-document GoTo) count nowhere.
  page_actions        FPDF_GetPageAAction for the open and close triggers
                      (the only two PDFium exposes): 0, 1 or 2. PDFium
                      reports the action type of a script action as
                      "unsupported", so presence is what is counted.
  file_attachments    FPDFPage_GetAnnot + FPDFAnnot_GetSubtype over every
                      annotation: /FileAttachment annotations.

Limits: a widget (form field) with its own action is not a /Link and is not
counted; the parent's byte markers see its `/Launch` or `/JS` anyway.
Enumeration stops after MAX_ENUMERATED entries so a page with millions of
annotations costs bounded time; the counts are then a floor.

Imports: standard library and pypdfium2 only. The functions accept the
pypdfium2 helper objects (PdfDocument, PdfPage) or raw handles; both convert
to the C handle through ctypes' `_as_parameter_` protocol.
"""

from __future__ import annotations

import ctypes

import pypdfium2.raw as raw

from app.sandbox.protocol import (
    DOC_HAZARD_KEYS,
    FORM_ACROFORM,
    FORM_NONE,
    FORM_XFA_FOREGROUND,
    FORM_XFA_FULL,
    PAGE_HAZARD_KEYS,
)

MAX_ENUMERATED = 100_000

_ACTION_KEYS: dict[int, str] = {
    raw.PDFACTION_URI: "uri_links",
    raw.PDFACTION_LAUNCH: "launch_actions",
    raw.PDFACTION_REMOTEGOTO: "remote_goto",
    raw.PDFACTION_EMBEDDEDGOTO: "embedded_goto",
}
_FORM_TYPES: dict[int, str] = {
    raw.FORMTYPE_NONE: FORM_NONE,
    raw.FORMTYPE_ACRO_FORM: FORM_ACROFORM,
    raw.FORMTYPE_XFA_FULL: FORM_XFA_FULL,
    raw.FORMTYPE_XFA_FOREGROUND: FORM_XFA_FOREGROUND,
}


def empty_document_hazards() -> dict[str, int]:
    return {key: 0 for key in DOC_HAZARD_KEYS}


def empty_page_hazards() -> dict[str, int]:
    return {key: 0 for key in PAGE_HAZARD_KEYS}


def form_type(doc) -> str:
    """`protocol.FORM_*` for the document; an unknown PDFium value reads as
    `FORM_NONE` rather than leaking a raw int."""
    return _FORM_TYPES.get(int(raw.FPDF_GetFormType(doc)), FORM_NONE)


def document_hazards(doc) -> dict[str, int]:
    """Counts for every key in `protocol.DOC_HAZARD_KEYS`."""
    out = empty_document_hazards()
    out["javascript_actions"] = max(0, int(raw.FPDFDoc_GetJavaScriptActionCount(doc)))
    out["attachments"] = max(0, int(raw.FPDFDoc_GetAttachmentCount(doc)))
    out["xfa_packets"] = max(0, int(raw.FPDF_GetXFAPacketCount(doc)))
    return out


def _count_link_actions(page, out: dict[str, int]) -> None:
    position = ctypes.c_int(0)
    link = raw.FPDF_LINK()
    seen = 0
    while seen < MAX_ENUMERATED and raw.FPDFLink_Enumerate(page, position, link):
        seen += 1
        action = raw.FPDFLink_GetAction(link)
        if not action:
            continue
        key = _ACTION_KEYS.get(int(raw.FPDFAction_GetType(action)))
        if key is not None:
            out[key] += 1


def _count_page_actions(page, out: dict[str, int]) -> None:
    for trigger in (raw.FPDFPAGE_AACTION_OPEN, raw.FPDFPAGE_AACTION_CLOSE):
        if raw.FPDF_GetPageAAction(page, trigger):
            out["page_actions"] += 1


def _count_file_attachments(page, out: dict[str, int]) -> None:
    total = int(raw.FPDFPage_GetAnnotCount(page))
    for i in range(min(max(total, 0), MAX_ENUMERATED)):
        annot = raw.FPDFPage_GetAnnot(page, i)
        if not annot:
            continue
        try:
            if int(raw.FPDFAnnot_GetSubtype(annot)) == raw.FPDF_ANNOT_FILEATTACHMENT:
                out["file_attachments"] += 1
        finally:
            raw.FPDFPage_CloseAnnot(annot)


def page_hazards(page) -> dict[str, int]:
    """Counts for every key in `protocol.PAGE_HAZARD_KEYS` for one loaded
    page."""
    out = empty_page_hazards()
    _count_link_actions(page, out)
    _count_page_actions(page, out)
    _count_file_attachments(page, out)
    return out
