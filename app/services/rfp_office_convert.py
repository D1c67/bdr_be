"""Office -> PDF for the RFP Ingestion Sandbox, through Gotenberg.

The conversion boundary of docs/RFP_INGESTION_SANDBOX.md section 2.1. The API
process never opens a Word or Excel file: the quarantined bytes go, as they
are, to the LibreOffice route of the Gotenberg service that already serves
the in-app previews and the RFQ sends (`office_preview.py`, setting
`gotenberg_url`), and what comes back is written to the scratch file the
sandbox child is then run over. LibreOffice runs inside Gotenberg's own
container, so the parser that touches the attacker-controlled document is
neither this process nor the sandbox child; the PDF it returns is treated as
attacker-influenced and gets the same parent sniff and the same child
verification as an uploaded PDF (the caller does both).

What this module guarantees, and what it does not:

- Before a `.docx` / `.xlsx` goes anywhere, its zip container gets a
  bounded look (`external_content_verdict`, on the `rfp_zip` helpers: the
  end record is checked before zipfile parses the directory, members are
  inflated in chunks under a cap, and the inspected parts are parsed as
  XML so entity encoding, element prefixes, UTF-16 and a field split
  across runs hide nothing): an external relationship in any `.rels`
  other than a hyperlink (an image, an attached template, an OLE object, a
  frame, an external workbook, a package; LibreOffice may fetch those
  while importing; a URL or UNC target counts as external whatever its
  TargetMode says), an embedded object (`embeddings/`, `oleObject*`), an
  external workbook link part, a workbook data connection or query table,
  or a DDE / INCLUDE field in any Word XML part refuses the conversion as
  `ConversionRejected` with `reason = "external_content"` and the finding
  under `scan` in the fragment. The original stays in quarantine and never
  reaches the converter. A container the scan cannot open, and a part that
  is not well-formed XML or declares a DTD, are refused the same way (the
  scan could not run). The legacy `.doc` / `.xls` binaries cannot be
  scanned this way and go as they are; deploy the converter without
  network egress (docs section 6).
- One streamed multipart POST per file. The part is named `source.<ext>`
  from the sniffed `source_format`, never from the declared filename, since
  the part name is what LibreOffice picks its import filter by and a
  user-chosen name is not something to hand a converter. No spreadsheet
  option is set (`singlePageSheets` in particular): the sandbox's page-side
  cap would fail a one-page sheet the size of a poster, while ordinary
  pagination renders every page.
- The response is streamed to `dest` (opened O_EXCL, never following a
  symlink) in 1 MB chunks under a running byte cap, hashed as it lands. A
  body over the cap, an empty body and any non-200 status leave nothing
  behind.
- Outcomes are three exception classes the caller maps to file verdicts:
  `ConversionUnavailable` (unreachable, a timeout, or a 5xx; retryable,
  `failed/conversion_unavailable`), `ConversionRejected` (a 4xx, which is
  how Gotenberg reports an unsupported or corrupt document, or an empty
  body; permanent, `rejected/conversion_rejected`) and `ConversionTooLarge`
  (`rejected/too_large`). Each carries `info`, the manifest fragment of the
  attempt (engine, route, source format, duration, HTTP status, reason), so a
  failed conversion is recorded as fully as a successful one.
- Timeouts: Gotenberg sends no byte until LibreOffice has finished, so the
  read timeout IS the conversion bound; the write timeout bounds the upload
  of the source and the connect timeout is short. The caller sizes the bound
  from `rfp_ingest_office_convert_timeout_seconds`, which the settings keep
  under half the queue lease because the lease is not renewed during the
  call. Gotenberg's own `--api-timeout` must be at least as long.
- Every message raised here is app-authored (a status code at most, never a
  response body).

`transport` (or the module-level `_transport` seam) lets tests drive the
whole exchange through `httpx.MockTransport`, the same way the Procore client
is tested. Sync module: the runner calls it from the queue worker thread.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import zipfile
import zlib
from pathlib import Path

import httpx

from app.sandbox import protocol
from app.services import rfp_zip

logger = logging.getLogger(__name__)

ENGINE = "gotenberg"
ROUTE = "/forms/libreoffice/convert"
PART_NAME_TEMPLATE = "source.{ext}"

_CHUNK = 1024 * 1024
_CONNECT_TIMEOUT = 10.0
_POOL_TIMEOUT = 10.0

# The pre-conversion container scan (module docstring). Formats it applies
# to, its bounds, and what it looks for.
SCANNED_FORMATS = frozenset({protocol.SOURCE_FORMAT_DOCX, protocol.SOURCE_FORMAT_XLSX})
SCAN_MAX_MEMBERS = 5000
SCAN_MAX_CENTRAL_DIRECTORY_BYTES = 4 * 1024 * 1024
SCAN_MAX_RELS_BYTES = 2 * 1024 * 1024
SCAN_MAX_PART_BYTES = 32 * 1024 * 1024
SCAN_MAX_TOTAL_BYTES = 128 * 1024 * 1024
REASON_EXTERNAL_CONTENT = "external_content"
# The one relationship type an external target may carry without reaching
# out of the document: a hyperlink is opened by the reader, never by the
# importer. Matched on the type's last path segment, exactly.
_EXTERNAL_REL_ALLOWED = ("hyperlink",)
# Field instructions that make Word (and LibreOffice on import) reach out.
_FIELD_WORDS = ("ddeauto", "ddelink", "includepicture", "includetext")
# The Word parts whose field instructions are read: every XML part under
# word/ (the body, headers and footers, notes, settings, the glossary,
# diagrams and charts included).
_WORD_PART_RE = re.compile(r"^word/.*\.xml$")
# Workbook parts that name a data connection or a web query (refreshed by
# the importer, potentially).
_XLSX_CONNECTION_PREFIXES = ("xl/connections.xml", "xl/querytables/")


def _scan_rels(raw: bytes) -> str | None:
    """The first external relationship in the parsed `.rels` part that is
    not a hyperlink, as `external_rel:<type>`; `bad_xml` when the part
    does not parse (LibreOffice's reading of it cannot be predicted, so
    the file is not converted)."""
    rels = rfp_zip.relationships(raw)
    if rels is None:
        return "bad_xml"
    for rel_type, external in rels:
        if not external:
            continue
        kind = rel_type.rsplit("/", 1)[-1]
        if kind in _EXTERNAL_REL_ALLOWED:
            continue
        kind = re.sub(r"[^a-z0-9]", "", kind) or "unknown"
        return f"external_rel:{kind[:40]}"
    return None


def _scan_word_part(raw: bytes) -> str | None:
    """`dde` / `include_field` when the parsed Word part carries a DDE or
    INCLUDE field instruction anywhere (entity-encoded, split across runs
    or in another encoding included); `bad_xml` when it does not parse."""
    flat = rfp_zip.flattened_xml(raw)
    if flat is None:
        return "bad_xml"
    lowered = raw.lower()
    for word in _FIELD_WORDS:
        if word in flat or word.encode() in lowered:
            return "dde" if word.startswith("dde") else "include_field"
    return None


def external_content_verdict(src: Path) -> str | None:
    """A bounded look inside the `.docx` / `.xlsx` container at `src`
    before it is handed to LibreOffice: None when nothing in it reaches
    outside the document, else the app-authored reason. Refusals:
    `too_many_members` / `central_directory_too_large` (from the end record,
    before zipfile parses the directory), `embedding` (a member under
    `embeddings/` or named `oleObject*`), `external_link` (an
    `xl/externalLinks/` part), `data_connection` (`xl/connections.xml` or
    an `xl/queryTables/` part), `external_rel:<type>` (a `.rels`
    relationship whose TargetMode reads External, or whose Target is a URL
    or UNC path, of any type but a hyperlink), `dde` / `include_field` (a
    DDE or INCLUDE field instruction in a Word part), `bad_xml` (an
    inspected part that is not well-formed XML or declares a DTD),
    `member_too_large` (an inspected part over its cap, or the inspected
    parts over SCAN_MAX_TOTAL_BYTES together) and `bad_zip` (a container
    zipfile cannot open: the scan did not run, so the file is not
    converted). The parts are read as XML (`rfp_zip.relationships`,
    `rfp_zip.flattened_xml`), never matched as raw text alone."""
    bound = rfp_zip.precheck(
        src, max_entries=SCAN_MAX_MEMBERS, max_central_directory_bytes=SCAN_MAX_CENTRAL_DIRECTORY_BYTES,
    )
    if bound is not None:
        return "too_many_members" if bound == "too_many_entries" else "central_directory_too_large"
    try:
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            if len(infos) > SCAN_MAX_MEMBERS:
                return "too_many_members"
            rels: list[zipfile.ZipInfo] = []
            parts: list[zipfile.ZipInfo] = []
            for info in infos:
                name = info.filename.replace("\\", "/").lower()
                base = name.rsplit("/", 1)[-1]
                if "/embeddings/" in f"/{name}" or base.startswith("oleobject"):
                    return "embedding"
                if name.startswith("xl/externallinks/"):
                    return "external_link"
                if name.startswith(_XLSX_CONNECTION_PREFIXES):
                    return "data_connection"
                if name.endswith(".rels"):
                    rels.append(info)
                elif _WORD_PART_RE.match(name):
                    parts.append(info)
            total = 0
            for info in rels:
                raw = rfp_zip.read_member_bounded(zf, info, SCAN_MAX_RELS_BYTES)
                if raw is None:
                    return "member_too_large"
                total += len(raw)
                if total > SCAN_MAX_TOTAL_BYTES:
                    return "member_too_large"
                why = _scan_rels(raw)
                if why:
                    return why
            for info in parts:
                raw = rfp_zip.read_member_bounded(zf, info, SCAN_MAX_PART_BYTES)
                if raw is None:
                    return "member_too_large"
                total += len(raw)
                if total > SCAN_MAX_TOTAL_BYTES:
                    return "member_too_large"
                why = _scan_word_part(raw)
                if why:
                    return why
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError, EOFError, zlib.error):
        return "bad_zip"
    return None

# Test seam: when set, every client is built on this transport instead of the
# network (httpx.MockTransport in the tests).
_transport: httpx.BaseTransport | None = None

_MSG_UNREACHABLE = "The PDF converter could not be reached."
_MSG_TIMEOUT = "The PDF converter did not answer in time."
_MSG_SERVER_ERROR = "The PDF converter answered HTTP {status}."
_MSG_REFUSED = "The PDF converter refused the document (HTTP {status})."
_MSG_EMPTY = "The PDF converter returned an empty document."
_MSG_TOO_LARGE = "The converted PDF is larger than the per-file limit."
_MSG_EXTERNAL = "The document reaches outside itself ({why}) and was not converted."


class OfficeConversionError(RuntimeError):
    """A conversion attempt that produced no usable PDF. `info` is the
    manifest fragment of the attempt (JSON-safe, app-authored)."""

    def __init__(self, message: str, *, info: dict) -> None:
        super().__init__(message)
        self.info = info


class ConversionUnavailable(OfficeConversionError):
    """Retryable: the converter was unreachable, timed out or answered 5xx."""


class ConversionRejected(OfficeConversionError):
    """Permanent: the converter refused the document (4xx) or returned
    nothing."""


class ConversionTooLarge(OfficeConversionError):
    """Permanent: the returned PDF passed the per-file byte cap."""


def _open_excl(dest: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o644)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _client(timeout_seconds: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        transport=transport if transport is not None else _transport,
        timeout=httpx.Timeout(
            connect=_CONNECT_TIMEOUT,
            read=float(timeout_seconds),
            write=float(timeout_seconds),
            pool=_POOL_TIMEOUT,
        ),
        follow_redirects=False,
    )


def convert_to_pdf(
    src: Path,
    dest: Path,
    *,
    source_format: str,
    max_bytes: int,
    timeout_seconds: float,
    base_url: str,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Send the office file at `src` to Gotenberg and write the PDF it returns
    to `dest` (created O_EXCL). Returns the manifest `conversion` fragment:
    `{engine, route, source_format, duration_ms, pdf_sha256, pdf_bytes,
    http_status}`. Raises ConversionUnavailable, ConversionRejected or
    ConversionTooLarge (see the module docstring); `dest` never exists after
    a raise. OSError from the scratch disk propagates unchanged.

    `src` and `dest` must be different paths; the caller keeps the raw file
    for the quarantine upload and for a retry."""
    if source_format not in protocol.OFFICE_FORMATS:
        raise ValueError("convert_to_pdf only takes an office source_format")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    src, dest = Path(src), Path(dest)
    if src == dest:
        raise ValueError("convert_to_pdf needs distinct src and dest paths")
    url = f"{base_url.rstrip('/')}{ROUTE}"
    part_name = PART_NAME_TEMPLATE.format(ext=source_format)
    info: dict = {
        "engine": ENGINE,
        "route": ROUTE,
        "source_format": source_format,
        "duration_ms": 0,
        "http_status": None,
    }
    started = time.monotonic()

    def _elapsed() -> None:
        info["duration_ms"] = int((time.monotonic() - started) * 1000)

    if source_format in SCANNED_FORMATS:
        why = external_content_verdict(src)
        if why is not None:
            _elapsed()
            info["reason"] = REASON_EXTERNAL_CONTENT
            info["scan"] = why
            raise ConversionRejected(_MSG_EXTERNAL.format(why=why), info=info)

    digest = hashlib.sha256()
    written = 0
    out = None
    try:
        with open(src, "rb") as fh, _client(timeout_seconds, transport) as client:
            with client.stream(
                "POST",
                url,
                files={"files": (part_name, fh, "application/octet-stream")},
            ) as resp:
                info["http_status"] = resp.status_code
                if resp.status_code >= 500:
                    _elapsed()
                    info["reason"] = "server_error"
                    raise ConversionUnavailable(
                        _MSG_SERVER_ERROR.format(status=resp.status_code), info=info
                    )
                if resp.status_code != 200:
                    _elapsed()
                    info["reason"] = "refused"
                    raise ConversionRejected(
                        _MSG_REFUSED.format(status=resp.status_code), info=info
                    )
                out = _open_excl(dest)
                with out:
                    for chunk in resp.iter_bytes(_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            _elapsed()
                            info["reason"] = "too_large"
                            raise ConversionTooLarge(_MSG_TOO_LARGE, info=info)
                        digest.update(chunk)
                        out.write(chunk)
    except httpx.TimeoutException as exc:
        _elapsed()
        info["reason"] = "timeout"
        if out is not None:
            _unlink_quietly(dest)
        raise ConversionUnavailable(_MSG_TIMEOUT, info=info) from exc
    except httpx.HTTPError as exc:
        # TransportError (unreachable, dropped mid-stream) and any other
        # httpx-level failure: the converter gave us no PDF, retry later.
        _elapsed()
        info["reason"] = "unreachable"
        if out is not None:
            _unlink_quietly(dest)
        raise ConversionUnavailable(_MSG_UNREACHABLE, info=info) from exc
    except BaseException:
        if out is not None:
            _unlink_quietly(dest)
        raise
    _elapsed()
    if written == 0:
        _unlink_quietly(dest)
        info["reason"] = "empty"
        raise ConversionRejected(_MSG_EMPTY, info=info)
    info["pdf_sha256"] = digest.hexdigest()
    info["pdf_bytes"] = written
    return info
