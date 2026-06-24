"""Office → PDF preview derivatives ("convert once on upload, preview forever").

Office files (.xlsx/.xlsm/.docx/.doc) can't be rendered natively by browsers, so
at upload time we convert them to PDF and store the derivative next to the
original in the same private bucket ({project_id}/previews/{file_id}.pdf). The
frontend then previews them through the existing PDF modal via the same
short-TTL signed-URL path — estimator hardening applies unchanged.

Engine-agnostic (settings.preview_engine):
  - "gotenberg": LibreOffice via a Gotenberg sidecar (default, fully in-house).
  - "graph":     Microsoft Graph `/content?format=pdf` via a scratch upload to
                 the bids@ drive. Requires the Files.ReadWrite.All application
                 permission (admin consent). Known caps: ~250 PDF pages, large
                 files rejected with 406.
  - "off":       pipeline disabled; uploads keep preview_status="none".

Drawings are never converted — they are already PDFs and are the estimator's
read surface; that path stays byte-identical to the upload.
"""

import io
import logging
import re
import time
import uuid
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services import storage

logger = logging.getLogger(__name__)

CONVERTIBLE_EXTS = {".xlsx", ".xlsm", ".docx", ".doc"}
_SPREADSHEET_EXTS = {".xlsx", ".xlsm"}


class ConversionError(Exception):
    """A converter failed or refused the file."""


def _ext(filename: str | None) -> str:
    name = (filename or "").lower()
    dot = name.rfind(".")
    return name[dot:] if dot != -1 else ""


def is_convertible(filename: str | None, category: str) -> bool:
    """Whether this file should get a PDF preview derivative."""
    if get_settings().preview_engine == "off":
        return False
    if category == "drawing":  # drawings are PDFs; never enter a converter
        return False
    return _ext(filename) in CONVERTIBLE_EXTS


def preview_object_path(project_id: str, file_id: str) -> str:
    return f"{project_id}/previews/{file_id}.pdf"


class Converter(Protocol):
    def convert_to_pdf(self, content: bytes, filename: str) -> bytes: ...


class GotenbergConverter:
    """LibreOffice conversion via a Gotenberg sidecar (https://gotenberg.dev)."""

    def convert_to_pdf(self, content: bytes, filename: str) -> bytes:
        s = get_settings()
        data: dict[str, str] = {}
        if _ext(filename) in _SPREADSHEET_EXTS:
            # One PDF page per sheet — wide BOQ sheets don't get chopped up by
            # Excel print-layout pagination.
            data["singlePageSheets"] = "true"
        try:
            resp = httpx.post(
                f"{s.gotenberg_base_url}/forms/libreoffice/convert",
                files={"files": (filename, content)},
                data=data,
                timeout=s.preview_convert_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ConversionError(f"gotenberg unreachable: {exc}") from exc
        if resp.status_code != 200 or not resp.content:
            raise ConversionError(
                f"gotenberg {resp.status_code}: {resp.text[:200]}"
            )
        return resp.content


class GraphConverter:
    """Microsoft Graph conversion: scratch upload to the bids@ drive, then
    `GET /content?format=pdf`. The scratch item is deleted afterwards."""

    def convert_to_pdf(self, content: bytes, filename: str) -> bytes:
        from app.services import graph_email

        if _ext(filename) not in CONVERTIBLE_EXTS:
            raise ConversionError(f"not an office file: {filename}")

        sender = get_settings().ms_sender
        # The scratch path is interpolated into a Graph URL — restrict the
        # untrusted filename to URL-safe characters (cf. rfq_sending's
        # _safe_component) so '#', '?' or ':' can't break the request path.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "file") or "file"
        scratch = f"BDR/preview-scratch/{uuid.uuid4().hex}-{safe}"
        item_id = graph_email.drive_upload(scratch, content)
        try:
            resp = graph_email.graph_request(
                "GET",
                f"/users/{sender}/drive/items/{item_id}/content",
                params={"format": "pdf"},
                follow_redirects=True,
                timeout=get_settings().preview_convert_timeout_seconds,
            )
            if not resp.content:
                raise ConversionError("graph returned an empty PDF")
            return resp.content
        except httpx.HTTPStatusError as exc:
            # 406/413: too large or not convertible; anything else is transient.
            raise ConversionError(
                f"graph convert failed ({exc.response.status_code})"
            ) from exc
        finally:
            try:
                graph_email.graph_request(
                    "DELETE", f"/users/{sender}/drive/items/{item_id}"
                )
            except Exception:  # noqa: BLE001 — scratch leaks are cosmetic
                logger.warning("Failed to delete Graph scratch item %s", item_id)


def get_converter() -> Converter | None:
    engine = get_settings().preview_engine
    if engine == "gotenberg":
        return GotenbergConverter()
    if engine == "graph":
        return GraphConverter()
    return None


# ── send-time conversion (immutable outbound copies) ─────────────────────────
#
# Distinct from generate_preview below: the preview pipeline deliberately
# swallows every failure into preview_status so a bad convert never breaks an
# upload. The send path is the opposite — a failed conversion MUST fail the send
# (never fall back to emailing the malleable office file), so these RAISE.


def is_office_file(filename: str | None) -> bool:
    """True for the editable Office formats we convert to PDF before emailing.
    Unlike is_convertible this is NOT gated on preview_engine or category — a
    send must convert (or fail), regardless of the in-app preview setting."""
    return _ext(filename) in CONVERTIBLE_EXTS


def pdf_filename(name: str | None) -> str:
    """The emailed PDF mirrors the stored office filename with a .pdf extension:
    'Proposal 1234 - Acme.docx' -> 'Proposal 1234 - Acme.pdf'."""
    ext = _ext(name)
    if ext in CONVERTIBLE_EXTS:
        return name[: -len(ext)] + ".pdf"
    return (name or "file") + ".pdf"


def convert_for_send(content: bytes, filename: str) -> bytes:
    """Office → PDF for EMAILING. Raises ConversionError on any failure so the
    caller fails the send rather than attaching the malleable original.

    - converter disabled (preview_engine='off') -> ConversionError
    - larger than preview_max_convert_mb -> ConversionError (skip a doomed call)
    - one retry on a transient converter failure (mirrors generate_preview)
    - verifies the result is non-empty and really is a PDF (%PDF magic), since
      GotenbergConverter only checks for HTTP 200 / non-empty, not PDF-ness.
    """
    converter = get_converter()
    if converter is None:  # preview_engine == "off"
        raise ConversionError("PDF conversion is disabled (preview_engine='off') — cannot send.")

    s = get_settings()
    if len(content) > s.preview_max_convert_mb * 1024 * 1024:
        raise ConversionError(
            f"document too large to convert to PDF (> {s.preview_max_convert_mb}MB)"
        )

    last_error: ConversionError | None = None
    for attempt in (1, 2):  # single retry
        try:
            pdf = converter.convert_to_pdf(content, filename)
            if not pdf or not pdf.startswith(b"%PDF"):
                raise ConversionError("converter returned a non-PDF response")
            return pdf
        except ConversionError as exc:
            last_error = exc
            if attempt == 1:
                time.sleep(2)
    raise last_error  # type: ignore[misc]  # loop always sets it before falling through


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Whitespace-normalized visible text of a PDF, for the proposal send-time
    leak re-scan (proposal_docx.validate_pdf_isolation). Best-effort per page."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = " ".join((page.extract_text() or "") for page in reader.pages)
    return " ".join(text.split())


def _mark(file_id: str, **fields) -> None:
    get_supabase().table("project_files").update(fields).eq("id", file_id).execute()


def generate_preview(file_id: str) -> None:
    """Convert one project_file to its PDF derivative and record the outcome.

    Safe everywhere it's called from (BackgroundTasks threadpool, the inbox
    poller thread, the backfill script): idempotent, single retry, and no
    exception ever escapes — failures land in preview_status/preview_error.
    """
    try:
        rec = (
            get_supabase()
            .table("project_files")
            .select("*")
            .eq("id", file_id)
            .single()
            .execute()
        ).data
        if not rec or rec.get("preview_status") == "ready":
            return

        converter = get_converter()
        if converter is None:
            _mark(file_id, preview_status="none")
            return

        s = get_settings()
        size = rec.get("size_bytes")
        if size and size > s.preview_max_convert_mb * 1024 * 1024:
            _mark(
                file_id,
                preview_status="failed",
                preview_error=f"too large to convert (> {s.preview_max_convert_mb}MB)",
            )
            return

        content = storage.download_file(rec["storage_path"])
        filename = rec.get("filename") or "file"

        pdf: bytes | None = None
        last_error: Exception | None = None
        for attempt in (1, 2):  # single retry
            try:
                pdf = converter.convert_to_pdf(content, filename)
                break
            except Exception as exc:  # noqa: BLE001 — recorded, retried once
                last_error = exc
                if attempt == 1:
                    time.sleep(2)
        if pdf is None:
            logger.warning("Preview conversion failed for %s: %s", filename, last_error)
            _mark(
                file_id,
                preview_status="failed",
                preview_error=str(last_error)[:500],
            )
            return

        # The file may have been deleted while we were converting — don't
        # upload an orphan derivative for a row that no longer exists.
        still_exists = (
            get_supabase()
            .table("project_files")
            .select("id")
            .eq("id", file_id)
            .execute()
        ).data
        if not still_exists:
            return

        path = preview_object_path(rec["project_id"], file_id)
        # upsert: a retried/backfilled conversion may overwrite a stale derivative
        storage.upload_file(path, pdf, "application/pdf", upsert=True)
        _mark(file_id, preview_path=path, preview_status="ready", preview_error=None)
    except Exception:  # noqa: BLE001 — never break the caller (poller/bg task)
        logger.exception("Preview generation crashed for file %s", file_id)
        try:
            _mark(file_id, preview_status="failed", preview_error="internal error")
        except Exception:  # noqa: BLE001
            pass
