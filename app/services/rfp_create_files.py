"""RFP Ingestion: document promotion (docs/RFP_CREATE.md section 5, job
`rfp_create_files`).

When the creation step makes a project from a harvest that put files into
the Ingestion Sandbox, this job moves the verified ones into the project's
own files. The bytes promoted are exactly what the sandbox verified: the
ORIGINAL object from quarantine when the verdict is `verified`, every
PDFium hazard counter except `uri_links` is zero AND every byte marker the
sandbox's sniff counted except `/URI` is zero (estimators take off from
vector PDFs; the RFQs and the splitter need them too), its sha256
re-checked after the download against the digest the sandbox recorded (no
digest, no promotion). A hazard block that is missing or unreadable is
UNKNOWN, never clean. Legacy `.doc` / `.xls` promote the sandbox's
converted PDF instead (a legacy binary can carry macros, the converted PDF
cannot), checked against the conversion digest the manifest recorded. A
`.docx` / `.xlsx` original is promoted only after a bounded scan of its
OOXML container (`ooxml_container_verdict`: no VBA project, no embedded
objects, no external links or templates, no DDE, not macro-enabled); a
container the scan refuses lands as the converted PDF instead, with the
reason on the file's note. Anything else is listed on the "Created from
RFPs" card with the reason and not ingested; there is no rasterized
fallback. Images never reach the sandbox, attached emails and zip
containers are never entries with a sandbox file, so none of those is ever
promoted.

One job per PROJECT (payload `{project_id}`), claimed in the queue's third
pass beside the harvests (a 100-file set is minutes of streaming), fenced
on `rfp_created_projects.files_claim_token` like a harvest. A retry is
idempotent: the partial unique index on `project_files (project_id,
rfp_sandbox_file_id)` makes a second promotion of the same sandbox file a
unique violation, read here as "already promoted" (the uploaded object is
deleted). A transient failure (storage) releases the claim and rides the
queue's retry ladder; ladder exhaustion writes `files_status = failed`
and the "Retry documents" button on the page re-enqueues.

`promotion_for`, `category_for` and `ooxml_container_verdict` are pure so
the decision table is tested exhaustively.
"""

from __future__ import annotations

import bisect
import hashlib
import io
import logging
import os
import re
import tempfile
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any

import httpx

from app.core.config import Settings, get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.sandbox import protocol
from app.services import files_needed, llm_queue, office_preview, rfp_split, rfp_zip, storage
from app.services import rfp_ingest_storage as rs
from app.services.notifications import audit, notify_role, notify_user

logger = logging.getLogger(__name__)

TABLE = "rfp_created_projects"
HARVESTS_TABLE = "rfp_harvests"
FILES_TABLE = "rfp_ingest_files"
PROJECT_FILES_TABLE = "project_files"

JOB_TYPE = "rfp_create_files"
MODEL_LABEL = "storage"

STATUS_NONE = "none"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
# The statuses "Retry documents" (and a fresh enqueue) may start from.
RETRYABLE_STATUSES = (STATUS_NONE, STATUS_FAILED)

# Harvest entry statuses that put bytes into the sandbox.
ENTRY_WITH_FILE = ("accepted", "reused")
# Hazard counters that never block a promotion: a plain web link inside a
# PDF is what every spec book carries.
ALLOWED_HAZARDS = frozenset({"uri_links"})
# The sniff's byte markers (protocol.BYTE_MARKERS, decoded) that never block
# a promotion: `/URI` is the same web link seen by the byte scan.
ALLOWED_MARKERS = frozenset({"/URI"})
# Source formats promoted as the original bytes, and those promoted as the
# sandbox's converted PDF. The OOXML originals get the container scan first.
ORIGINAL_FORMATS = (protocol.SOURCE_FORMAT_PDF, protocol.SOURCE_FORMAT_DOCX, protocol.SOURCE_FORMAT_XLSX)
OOXML_FORMATS = (protocol.SOURCE_FORMAT_DOCX, protocol.SOURCE_FORMAT_XLSX)
CONVERTED_FORMATS = (protocol.SOURCE_FORMAT_DOC, protocol.SOURCE_FORMAT_XLS)

CATEGORY_DRAWING = "drawing"
CATEGORY_ELECTRICAL = "electrical_drawing"
CATEGORY_SPECIFICATION = "specification"
CATEGORY_OTHER = "other"
DRAWING_CATEGORIES = (CATEGORY_DRAWING, CATEGORY_ELECTRICAL) + tuple(
    sorted(rfp_split.DRAWING_FILE_CATEGORIES - {CATEGORY_DRAWING, CATEGORY_ELECTRICAL})
)

REASON_NO_SANDBOX_FILE = "not_in_sandbox"
REASON_PAGES_UNVERIFIED = "pages_unverified"
REASON_HAZARDS_UNKNOWN = "hazards_unknown"
REASON_SHA_MISMATCH = "changed_since_verification"
REASON_CONVERTED_SHA_MISMATCH = "converted_sha_mismatch"
REASON_NO_DIGEST = "no_digest"
REASON_MISSING_IN_STORAGE = "missing_in_storage"
REASON_TOO_LARGE = "too_large"
REASON_NO_SOURCE = "no_source_object"
REASON_UNKNOWN_FORMAT = "unsupported_format"
# Skip prefix when the byte-marker scan could not see through the PDF
# (`unscannable:<reason>`, reasons in PdfUnscannable): never promoted.
REASON_PDF_UNSCANNABLE_PREFIX = "unscannable:"
# Note tokens on a promoted converted PDF: why the original was not promoted
# (`converted:<why>`) and a conversion the manifest recorded no digest for.
NOTE_CONVERTED_PREFIX = "converted:"
NOTE_CONVERTED_UNVERIFIED = "converted_unverified"

# The OOXML container scan's bounds (section 5): more members than this, or
# an inspected member larger than this, refuses the original outright. The
# member count and the central directory size are read from the archive's
# end record (`rfp_zip.precheck`) BEFORE zipfile parses the directory, and
# inspected members are inflated in chunks under a running counter
# (`rfp_zip.read_member_bounded`), so neither bound is checked after the
# memory it guards has already been spent.
OOXML_MAX_MEMBERS = 5000
OOXML_MAX_MEMBER_BYTES = 2 * 1024 * 1024
OOXML_MAX_CENTRAL_DIRECTORY_BYTES = 4 * 1024 * 1024
# The byte-marker scan's bounds over a PDF's FlateDecode streams: one stream
# inflating past the first, or all of them past the second, makes the file
# unscannable (never promoted as an original). Memory stays one chunk at a
# time; the caps bound CPU time on a decompression bomb.
PDF_INFLATE_STREAM_CAP = 256 * 1024 * 1024
PDF_INFLATE_TOTAL_CAP = 4 * 1024 * 1024 * 1024
_PDF_INFLATE_IN_CHUNK = 256 * 1024
_PDF_INFLATE_OUT_CHUNK = 1024 * 1024
# Bytes carried from one inflated chunk to the next so a name token split
# across the boundary is still seen whole (the longest marker, fully
# `#xx`-escaped, is 37 bytes).
_PDF_SCAN_CARRY = 128
# Relationship types whose external target is a way out of the document.
# The parts are read as XML (`rfp_zip.relationships`, `rfp_zip.flattened_xml`)
# so an entity-encoded attribute, a prefixed element or a UTF-16 part reads
# the way Word reads it; a part that does not parse refuses the original.
_OOXML_EXTERNAL_REL_WORDS = ("attachedtemplate", "oleobject", "frame", "externallink", "package")

_SKIPPED_CAP = 200
_ERROR_MAX_CHARS = 500
_FILENAME_MAX_CHARS = 200
_IN_CHUNK = 200
_FILE_SELECT = (
    "id, run_id, status, hazards, quarantine_path, source_format, converted_path, filename, "
    "size_bytes, sha256, manifest"
)

_MSG_INTERRUPTED = "The document promotion was interrupted; it will be retried."
_MSG_NOT_FOUND = "The created project record was not found."
_MSG_STORAGE = "Storage did not answer while promoting the documents."


# ── Errors ───────────────────────────────────────────────────────────────────


class RfpCreateFilesTransient(RuntimeError):
    """Worth retrying: storage, network, a lost lease. The queue's ladder applies."""

    llm_error_kind = "infrastructure"


class RfpCreateFilesPermanent(ValueError):
    """A permanent, app-authored refusal (the record is gone)."""

    llm_error_kind = "bad_input"


class _ClaimLost(Exception):
    """Another worker owns the promotion (or a person cleared the record)."""


# ── Pure decisions ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Promote:
    bucket: str
    path: str
    filename: str          # what project_files.filename shows
    content_type: str
    check_sha: bool        # True: the original bytes, whose verified digest is required
    note: str | None = None
    # The digest the downloaded bytes must match: the verified sha256 of the
    # original (always set: no digest is a Skip), or the conversion digest
    # the manifest recorded for a converted PDF (None = unverified, noted).
    expected_sha: str | None = None
    # True for a .docx / .xlsx original: the OOXML container scan runs on
    # the downloaded bytes and a refusal falls back to the converted PDF.
    ooxml_scan: bool = False


@dataclass(frozen=True)
class Skip:
    reason: str


def entry_name(entry: dict, file_row: dict | None = None) -> str:
    """The readable filename: the basename of the entry's `file_path`
    (Procore, the email harvester), else its `file_name` (NGEM), else the
    sandbox's stored filename, else the file id."""
    for key in ("file_path", "file_name"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            base = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
            if base:
                return base[:_FILENAME_MAX_CHARS]
    stored = (file_row or {}).get("filename")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()[:_FILENAME_MAX_CHARS]
    return str(entry.get("sandbox_file_id") or "document")


def _counter_keys(counters: dict, allowed: frozenset[str]) -> list[str]:
    """The counters above zero, the allowed keys excepted, sorted. An
    unreadable counter is not a zero."""
    out = []
    for key, value in counters.items():
        if key in allowed:
            continue
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            count = 1
        if count > 0:
            out.append(str(key))
    return sorted(out)


def hazard_keys(hazards: Any) -> list[str] | None:
    """The PDFium hazard counters above zero, `uri_links` excepted, sorted.
    None when the block is not a dict: unknown, never clean."""
    if not isinstance(hazards, dict):
        return None
    return _counter_keys(hazards, ALLOWED_HAZARDS)


# The sniff's byte markers, matched as PDF NAME TOKENS on the bytes about to
# be promoted: a marker counts only when the next byte ends the name
# (whitespace, a delimiter, or the end of the file), so `/AA` never matches
# the `/AAPL:Keywords` every Mac-made PDF carries. The sandbox's own sniff is
# a substring count kept for the reviewer; this scan is what decides. It is
# the authoritative net behind PDFium's hazard counters, which cannot see a
# catalog /OpenAction or a widget action: a name written with `#xx` escapes
# is decoded before matching, and every stream is found the way a PDF
# reader finds one, from its `N G obj` header, with the dictionary read by a
# small PDF tokenizer (strings, hex strings, comments, nesting and `#xx`
# escapes honoured; never a regex window over the bytes before the keyword).
# A plain FlateDecode stream is inflated (bounded) and scanned the same way
# so a catalog hidden in a compressed object stream is seen. The walk fails
# closed, raising PdfUnscannable so the original is not promoted, on: a
# `stream` keyword (any line ending) that no parsed object accounts for; an
# object stream (/Type /ObjStm, a /Type given indirectly, or any stream
# carrying /N, the key a reader that never checks /Type loads one by) behind
# another filter, a filter chain, an indirect filter, a predictor, or with a
# length that cannot be settled, or truncated; a corrupt or over-cap stream;
# a keyword whose line ending is ambiguous; and a file past the tokenizing
# budget. An /ObjStm whose dictionary or filter a reader would reject is
# still refused rather than trusted: nothing here guesses.
_MARKER_RE = re.compile(
    rb"(" + b"|".join(re.escape(m) for m in protocol.BYTE_MARKERS) + rb")(?=[\s()<>\[\]{}/%]|\Z)"
)
_MARKER_SET = frozenset(protocol.BYTE_MARKERS)
# A name token carrying at least one `#`: decoded (`#xx` -> the byte) and
# compared whole against the markers. NUL is white space in PDF syntax.
_ESCAPED_NAME_RE = re.compile(rb"/[^\s\x00()<>\[\]{}/%]*#[^\s\x00()<>\[\]{}/%]*")
_NAME_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")
_PDF_WS = b"\r\n\t \x00\x0c"
# Every `stream` keyword in the file whatever follows it (`\r\n`, `\n`, a
# bare `\r`, trailing blanks: lenient readers take all of them); the
# lookbehind leaves `endstream` out.
_STREAM_KW_RE = re.compile(rb"(?<![A-Za-z])stream(?![A-Za-z])")
# Every `N G obj` header, the only place a reader starts an object from.
_OBJ_HEADER_RE = re.compile(rb"(?<![0-9])(\d+)[\s\x00]+(\d+)[\s\x00]+obj(?![A-Za-z0-9])")
_INT_OBJ_RE = re.compile(rb"(?<![0-9])(\d+)[\s\x00]+0[\s\x00]+obj[\s\x00]*(\d+)[\s\x00]*endobj")
# One PDF token after any white space and comments. The groups, in order:
# `<<`, `>>`, `[`, `]`, `(` (a literal string follows), `<` (a hex string),
# a name, a number, a keyword, any other delimiter (`)`, `>`, `{`, `}`).
_TOKEN_RE = re.compile(
    rb"(?:[\s\x00]|%[^\r\n]*)*"
    rb"(?:(<<)|(>>)|(\[)|(\])|(\()|(<)|(/[^\s\x00()<>\[\]{}/%]*)"
    rb"|([+-]?(?:\d+\.?\d*|\.\d+))(?![^\s\x00()<>\[\]{}/%])|([^\s\x00()<>\[\]{}/%]+)|([^\s\x00]))",
    re.DOTALL,
)
_TOKEN_KINDS = ("<<", ">>", "[", "]", "str", "hex", "name", "num", "kw", "other")
_STRING_SPECIAL_RE = re.compile(rb"[()\\]")
_INT_RE = re.compile(rb"[+-]?\d+\Z")
_NAME_FLATE = b"/FlateDecode"
_NAME_OBJSTM = b"/ObjStm"
# Bounds on the tokenizer: one object's source (its dictionary, never a
# stream body) and the file's total, so a file built to make the walk
# quadratic ends as `scan_budget`, unscannable.
PDF_PARSE_OBJECT_CAP = 4 * 1024 * 1024
PDF_PARSE_BUDGET = 128 * 1024 * 1024
_PDF_MAX_NESTING = 64


class PdfUnscannable(Exception):
    """The byte-marker scan could not see through the file (a stream it
    cannot inflate or that is past a cap, an object stream it cannot
    decode). `reason` is an app-authored token for the skip list."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _marker_hits(buf: bytes, found: set[str], *, final: bool) -> None:
    """Add the markers `buf` carries as name tokens (plain or `#xx`-escaped)
    to `found`. A token that runs to the very end of `buf` counts only when
    `final`: otherwise the next carried chunk decides how it ends."""
    end = len(buf)
    for match in _MARKER_RE.finditer(buf):
        if match.end() == end and not final:
            continue
        key = match.group(1).decode("ascii")
        if key not in ALLOWED_MARKERS:
            found.add(key)
    for match in _ESCAPED_NAME_RE.finditer(buf):
        if match.end() == end and not final:
            continue
        decoded = _NAME_ESCAPE_RE.sub(lambda e: bytes([int(e.group(1), 16)]), match.group())
        if decoded in _MARKER_SET:
            key = decoded.decode("ascii")
            if key not in ALLOWED_MARKERS:
                found.add(key)


def _inflate_scan(
    data: bytes, start: int, end: int, found: set[str], budget: list[int], *, objstm: bool = False,
) -> None:
    """Inflate the FlateDecode stream body `data[start:end]` in chunks and
    scan every chunk (with a carry) for the markers. Raises PdfUnscannable
    on a zlib error, on a stream past PDF_INFLATE_STREAM_CAP, once the
    file's inflated total passes PDF_INFLATE_TOTAL_CAP, and for an object
    stream whose deflate data ends before its end marker (what a reader
    would load from it cannot be settled)."""
    view = memoryview(data)[start:end]
    dec = zlib.decompressobj()
    fed = 0
    pending: bytes | memoryview = b""
    carry = b""
    inflated = 0
    while True:
        if not pending and fed < len(view):
            pending = view[fed:fed + _PDF_INFLATE_IN_CHUNK]
            fed += len(pending)
        try:
            chunk = dec.decompress(pending, _PDF_INFLATE_OUT_CHUNK)
        except zlib.error as exc:
            raise PdfUnscannable("stream_corrupt") from exc
        pending = dec.unconsumed_tail
        if chunk:
            inflated += len(chunk)
            budget[0] += len(chunk)
            if inflated > PDF_INFLATE_STREAM_CAP:
                raise PdfUnscannable("stream_too_large")
            if budget[0] > PDF_INFLATE_TOTAL_CAP:
                raise PdfUnscannable("streams_too_large")
            buf = carry + chunk
            _marker_hits(buf, found, final=False)
            carry = buf[-_PDF_SCAN_CARRY:]
        if dec.eof or (not chunk and not pending and fed >= len(view)):
            break
    if objstm and not dec.eof:
        raise PdfUnscannable("objstm_truncated")
    _marker_hits(carry, found, final=True)


class _ParseFailed(Exception):
    """The forward parse from an object header did not yield one object a
    reader would load from there (so nothing is claimed for it)."""


class _Tokens:
    """A PDF tokenizer over `data` from `pos`, bounded by `limit` (the
    per-object cap) and the shared `budget` (index 0: bytes tokenized)."""

    __slots__ = ("data", "pos", "limit", "budget")

    def __init__(self, data: bytes, pos: int, limit: int, budget: list[int]) -> None:
        self.data = data
        self.pos = pos
        self.limit = limit
        self.budget = budget

    def _spend(self, upto: int) -> None:
        if upto > self.limit:
            raise _ParseFailed
        self.budget[0] += upto - self.pos
        if self.budget[0] > PDF_PARSE_BUDGET:
            raise PdfUnscannable("scan_budget")
        self.pos = upto

    def next(self) -> tuple[str, Any, int]:
        """(kind, value, start of the token): kind from `_TOKEN_KINDS`; a
        name comes back with its `#xx` escapes decoded, a number as int or
        float, a keyword as bytes, a string as None (its contents never
        matter here)."""
        match = _TOKEN_RE.match(self.data, self.pos)
        if match is None:
            raise _ParseFailed
        index = match.lastindex or 0
        kind = _TOKEN_KINDS[index - 1]
        start = match.start(index)
        self._spend(match.end())
        if kind == "str":
            self._literal_string()
            return kind, None, start
        if kind == "hex":
            close = self.data.find(b">", self.pos, self.limit + 1)
            if close < 0:
                raise _ParseFailed
            self._spend(close + 1)
            return kind, None, start
        raw = match.group(index)
        if kind == "name":
            return kind, _NAME_ESCAPE_RE.sub(lambda e: bytes([int(e.group(1), 16)]), raw), start
        if kind == "num":
            return kind, (int(raw) if _INT_RE.match(raw) else float(raw)), start
        return kind, raw, start

    def _literal_string(self) -> None:
        depth = 1
        pos = self.pos
        while depth:
            special = _STRING_SPECIAL_RE.search(self.data, pos, self.limit + 1)
            if special is None:
                raise _ParseFailed
            char = special.group()
            pos = special.end() + (1 if char == b"\\" else 0)
            if char == b"(":
                depth += 1
            elif char == b")":
                depth -= 1
        self._spend(pos)


def _parse_value(tokens: _Tokens, kind: str, value: Any, depth: int) -> Any:
    """One PDF object from its first token: a dict as {name: value}, an
    array as a list, an indirect reference as ("ref", num, gen), a name as
    decoded bytes, a number, None for a string, and ("kw", bytes) for
    true/false/null. Anything else where a value belongs fails the parse."""
    if depth > _PDF_MAX_NESTING:
        raise _ParseFailed
    if kind in ("<<", "["):
        closer = ">>" if kind == "<<" else "]"
        items: list[Any] = []
        while True:
            k, v, _ = tokens.next()
            if k == closer:
                break
            if k == "kw" and v == b"R":
                if len(items) < 2 or not isinstance(items[-1], int) or not isinstance(items[-2], int):
                    raise _ParseFailed
                items[-2:] = [("ref", items[-2], items[-1])]
                continue
            items.append(_parse_value(tokens, k, v, depth + 1))
        if kind == "[":
            return items
        if len(items) % 2 or any(not isinstance(key, bytes) for key in items[::2]):
            raise _ParseFailed
        return dict(zip(items[::2], items[1::2]))
    if kind in ("name", "num", "str", "hex"):
        return value
    if kind == "kw" and value in (b"true", b"false", b"null"):
        return ("kw", value)
    raise _ParseFailed


def _is_object_stream(head: dict) -> bool:
    """/Type /ObjStm, a /Type given indirectly (PDFium and pypdf resolve
    it), or any /N key (MuPDF and pdf.js load an object stream by /N and
    /First without checking /Type)."""
    kind = head.get(b"/Type")
    return kind == _NAME_OBJSTM or isinstance(kind, tuple) or b"/N" in head


def _filters(head: dict) -> list[bytes] | None:
    """The stream's filter names in order; [] for none; None when they
    cannot be read from the dictionary (indirect, or not names)."""
    value = head.get(b"/Filter")
    if value is None or value == ("kw", b"null"):
        return []
    if isinstance(value, bytes):
        return [value]
    if isinstance(value, list) and all(isinstance(item, bytes) for item in value):
        return value
    return None


def _has_predictor(head: dict) -> bool:
    """Whether /DecodeParms (or /DP) applies a predictor, or cannot be
    read: anything but a direct dict (or array of dicts / null) whose
    /Predictor is absent or 1 counts as one."""
    parms = head.get(b"/DecodeParms", head.get(b"/DP"))
    if parms is None or parms == ("kw", b"null"):
        return False
    entries = parms if isinstance(parms, list) else [parms]
    for entry in entries:
        if entry == ("kw", b"null"):
            continue
        if not isinstance(entry, dict):
            return True
        predictor = entry.get(b"/Predictor")
        if predictor is not None and (not isinstance(predictor, int) or predictor > 1):
            return True
    return False


def _stream_body_start(data: bytes, keyword_end: int) -> int:
    """Where the body starts: after the keyword, optional blanks and ONE
    line ending (`\\r\\n`, `\\n` or `\\r`). Anything else on the keyword's
    line makes the start ambiguous between readers, so the stream is
    unscannable."""
    pos = keyword_end
    while pos < len(data) and data[pos:pos + 1] in (b" ", b"\t", b"\x00", b"\x0c"):
        pos += 1
    if data[pos:pos + 2] == b"\r\n":
        return pos + 2
    if data[pos:pos + 1] in (b"\n", b"\r"):
        return pos + 1
    raise PdfUnscannable("stream_eol")


def _stream_body_end(
    data: bytes, length: Any, start: int, *, objstm: bool, int_objects: dict[int, int] | None,
) -> tuple[int, dict[int, int] | None]:
    """Where the stream body that starts at `start` ends: by a direct (or
    resolvable indirect) /Length that is followed by `endstream`, else by
    the next `endstream` keyword, the way PDFium falls back. An object
    stream whose length cannot be settled is unscannable."""
    if isinstance(length, tuple) and length[0] == "ref":
        if int_objects is None:
            int_objects = {int(num): int(val) for num, val in _INT_OBJ_RE.findall(data)}
        length = int_objects.get(length[1])
        if length is None and objstm:
            raise PdfUnscannable("objstm_length")
    if isinstance(length, int) and 0 <= length <= len(data) - start:
        after = data[start + length:start + length + 16].lstrip(_PDF_WS)
        if after.startswith(b"endstream"):
            return start + length, int_objects
    end = data.find(b"endstream", start)
    if end < 0:
        if objstm:
            raise PdfUnscannable("objstm_length")
        return len(data), int_objects
    return end, int_objects


def _scan_streams(data: bytes, found: set[str]) -> None:
    """Walk every object header and parse the object after it. One that is
    a dictionary followed by the `stream` keyword is a stream: a plain
    /FlateDecode one is inflated and scanned; an object stream behind any
    other filter, a filter chain, an indirect filter or a predictor is
    unscannable. Every parsed object accounts for the bytes it spans; a
    `stream` keyword left over in the file (one no reader would reach from
    a header, or one this walk could not parse the dictionary of) makes the
    file unscannable rather than trusted."""
    budget = [0]
    inflate_budget = [0]
    int_objects: dict[int, int] | None = None
    spans: list[tuple[int, int]] = []
    claimed: set[int] = set()
    done: set[tuple[int, int, bool]] = set()
    for header in _OBJ_HEADER_RE.finditer(data):
        tokens = _Tokens(data, header.end(), min(len(data), header.end() + PDF_PARSE_OBJECT_CAP), budget)
        try:
            kind, value, _ = tokens.next()
            head = _parse_value(tokens, kind, value, 0)
            if not isinstance(head, dict):
                spans.append((header.start(), tokens.pos))
                continue
            kind, value, at = tokens.next()
        except _ParseFailed:
            continue
        if kind != "kw" or value != b"stream":
            spans.append((header.start(), tokens.pos))
            continue
        claimed.add(at)
        objstm = _is_object_stream(head)
        filters = _filters(head)
        plain_flate = filters == [_NAME_FLATE]
        if objstm:
            if filters is None or (filters and not plain_flate):
                raise PdfUnscannable("objstm_filter")
            if _has_predictor(head):
                raise PdfUnscannable("objstm_decode_parms")
        start = _stream_body_start(data, at + 6)
        end, int_objects = _stream_body_end(
            data, head.get(b"/Length"), start, objstm=objstm, int_objects=int_objects,
        )
        spans.append((header.start(), end))
        if plain_flate and (start, end, objstm) not in done:
            done.add((start, end, objstm))
            _inflate_scan(data, start, end, found, inflate_budget, objstm=objstm)
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    starts = [lo for lo, _ in merged]
    for match in _STREAM_KW_RE.finditer(data):
        if match.start() in claimed:
            continue
        index = bisect.bisect_right(starts, match.start()) - 1
        if index < 0 or match.start() >= merged[index][1]:
            raise PdfUnscannable("stream_unparsed")


def pdf_marker_keys(data: bytes) -> list[str]:
    """The byte markers present in `data` as name tokens, `/URI` excepted,
    sorted: plain, `#xx`-escaped, and inside every plain FlateDecode
    stream (object streams included). Empty when the bytes carry none.
    Raises PdfUnscannable when a stream cannot be seen through (module
    comment above `_MARKER_RE`); a file whose plain bytes already carry a
    marker is answered before its streams are walked."""
    found: set[str] = set()
    _marker_hits(data, found, final=True)
    if not found:
        _scan_streams(data, found)
    return sorted(found)


def conversion_sha(manifest: Any) -> str | None:
    """The digest the sandbox recorded for its converted PDF
    (`manifest.conversion.pdf_sha256`), lowercased, or None."""
    conversion = manifest.get("conversion") if isinstance(manifest, dict) else None
    sha = conversion.get("pdf_sha256") if isinstance(conversion, dict) else None
    return sha.strip().lower() if isinstance(sha, str) and sha.strip() else None


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def converted_promotion(file_row: dict, name: str, fmt: str, *, why: str | None = None) -> Promote | Skip:
    """The converted-PDF promotion (section 5): the sandbox's `converted_path`
    from the derived bucket as `<stem>.pdf`, checked against the conversion
    digest the manifest recorded when there is one (noted
    `converted_unverified` otherwise). `why` is the reason the original was
    not promoted (the OOXML fallback), recorded as `converted:<why>`."""
    path = file_row.get("converted_path")
    if not path:
        # An OOXML refusal with no converted copy to fall back on: the card
        # names the container reason, not a missing object.
        return Skip(f"ooxml:{why}" if why else REASON_NO_SOURCE)
    expected = conversion_sha(file_row.get("manifest"))
    parts = [f"Converted from the original .{fmt} by the ingestion sandbox"]
    if why:
        parts.append(f"{NOTE_CONVERTED_PREFIX}{why}")
    if expected is None:
        parts.append(NOTE_CONVERTED_UNVERIFIED)
    return Promote(
        rs.DERIVED_BUCKET, path, f"{_stem(name)}.pdf", "application/pdf", False,
        note="; ".join(parts), expected_sha=expected,
    )


def promotion_for(entry: dict, file_row: dict | None) -> Promote | Skip:
    """The per-entry decision (section 5 step 2). `entry` is one harvest
    `files[]` item, `file_row` the rfp_ingest_files row it names (None when
    the id resolves to nothing)."""
    status = entry.get("status")
    if not entry.get("sandbox_file_id") or status not in ENTRY_WITH_FILE:
        return Skip(str(status or REASON_NO_SANDBOX_FILE))
    if file_row is None:
        return Skip(REASON_NO_SANDBOX_FILE)
    file_status = file_row.get("status")
    if file_status == protocol.STATUS_VERIFIED_WITH_GAPS:
        return Skip(REASON_PAGES_UNVERIFIED)
    if file_status != protocol.STATUS_VERIFIED:
        return Skip(str(file_status or REASON_NO_SANDBOX_FILE))
    keys = hazard_keys(file_row.get("hazards"))
    if keys is None:
        return Skip(REASON_HAZARDS_UNKNOWN)
    if keys:
        return Skip("hazard:" + ",".join(keys))
    fmt = file_row.get("source_format") or protocol.SOURCE_FORMAT_PDF
    name = entry_name(entry, file_row)
    if fmt in ORIGINAL_FORMATS:
        path = file_row.get("quarantine_path")
        if not path:
            return Skip(REASON_NO_SOURCE)
        expected = str(file_row.get("sha256") or "").strip().lower()
        if not expected:
            return Skip(REASON_NO_DIGEST)
        return Promote(
            rs.QUARANTINE_BUCKET, path, name, rs.source_content_type(fmt), True,
            expected_sha=expected, ooxml_scan=fmt in OOXML_FORMATS,
        )
    if fmt in CONVERTED_FORMATS:
        return converted_promotion(file_row, name, fmt)
    return Skip(REASON_UNKNOWN_FORMAT)


# ── The OOXML container scan (section 5) ─────────────────────────────────────


def _rels_verdict(raw: bytes) -> str | None:
    """An external relationship (TargetMode External once the XML is
    decoded, or a URL / UNC target) of a type that reaches outside the
    document; `bad_xml` when the part does not parse."""
    rels = rfp_zip.relationships(raw)
    if rels is None:
        return "bad_xml"
    for rel_type, external in rels:
        if not external:
            continue
        for word in _OOXML_EXTERNAL_REL_WORDS:
            if word in rel_type:
                return f"external_rel:{word}"
    return None


def ooxml_container_verdict(source: str | Path | IO[bytes]) -> str | None:
    """A bounded look inside a .docx / .xlsx container before its original
    bytes are promoted: None when nothing in it reaches past the document,
    else the reason (recorded as `converted:<reason>` on the converted PDF
    promoted instead). Listing only, plus the content types and the
    relationship parts read: a VBA project or any binary part under
    `word/` or `xl/`, an embedded object, an external workbook link, an
    external relationship of an attached-template / OLE / frame / external
    link / package type, DDE, a macro-enabled content type. More than
    OOXML_MAX_MEMBERS members, an inspected member over
    OOXML_MAX_MEMBER_BYTES, an inspected member that is not well-formed
    XML (`bad_xml`), or a container zipfile cannot open are refusals too.
    Inspected parts are read as XML, so entity encoding, element prefixes
    and UTF-16 hide nothing. The member count and the central directory size are
    read from the end record before zipfile parses anything
    (`central_directory_too_large` for a directory over
    OOXML_MAX_CENTRAL_DIRECTORY_BYTES), and inspected members are inflated
    in chunks under a running counter, never trusted at their declared
    size."""
    bound = rfp_zip.precheck(
        source,
        max_entries=OOXML_MAX_MEMBERS,
        max_central_directory_bytes=OOXML_MAX_CENTRAL_DIRECTORY_BYTES,
    )
    if bound is not None:
        return "too_many_members" if bound == "too_many_entries" else "central_directory_too_large"
    try:
        with zipfile.ZipFile(source) as zf:
            infos = zf.infolist()
            if len(infos) > OOXML_MAX_MEMBERS:
                return "too_many_members"
            inspect: list[zipfile.ZipInfo] = []
            for info in infos:
                name = info.filename.replace("\\", "/").lower()
                base = name.rsplit("/", 1)[-1]
                if base == "vbaproject.bin":
                    return "vba_project"
                if "/embeddings/" in f"/{name}" or base.startswith("oleobject"):
                    return "embedding"
                if name.startswith(("word/", "xl/")) and name.endswith(".bin"):
                    return "binary_part"
                if name.startswith("xl/externallinks/"):
                    return "external_link"
                if name == "[content_types].xml" or name.endswith(".rels"):
                    if info.file_size > OOXML_MAX_MEMBER_BYTES or info.compress_size > OOXML_MAX_MEMBER_BYTES:
                        return "member_too_large"
                    inspect.append(info)
            for info in inspect:
                raw = rfp_zip.read_member_bounded(zf, info, OOXML_MAX_MEMBER_BYTES)
                if raw is None:
                    return "member_too_large"
                flat = rfp_zip.flattened_xml(raw)
                if flat is None:
                    return "bad_xml"
                lowered = raw.lower()
                if any(word in flat or word.encode() in lowered for word in ("ddelink", "ddeauto")):
                    return "dde"
                name = info.filename.replace("\\", "/").lower()
                if name == "[content_types].xml" and ("macroenabled" in flat or b"macroenabled" in lowered):
                    return "macro_enabled"
                if name.endswith(".rels"):
                    why = _rels_verdict(raw)
                    if why:
                        return why
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError, EOFError, zlib.error):
        return "bad_zip"
    return None


def category_for(entry: dict) -> str:
    """Procore `kind = drawing` with an electrical discipline ->
    electrical_drawing; drawing -> drawing; specification -> specification;
    anything else, and every email-harvester or NGEM entry (kind null) ->
    other."""
    kind = entry.get("kind")
    if kind == "drawing":
        discipline = str(entry.get("discipline") or "").lower()
        return CATEGORY_ELECTRICAL if "electrical" in discipline else CATEGORY_DRAWING
    if kind == "specification":
        return CATEGORY_SPECIFICATION
    return CATEGORY_OTHER


# ── Queue adapters ───────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def enqueue(project_id: str, *, created_by: str | None, settings: Settings | None = None) -> dict:
    """One job per project; `raise_on_active` so the page can answer 409."""
    s = settings or get_settings()
    return llm_queue.enqueue(
        JOB_TYPE,
        target_id=project_id,
        project_id=project_id,
        payload={"project_id": project_id},
        created_by=created_by,
        priority=s.rfp_create_files_queue_priority,
        settings=s,
        raise_on_active=True,
    )


def active_job(project_id: str) -> dict | None:
    return llm_queue.active_job(JOB_TYPE, project_id)


def _load_record(sb, project_id: str) -> dict | None:
    rows = sb.table(TABLE).select("*").eq("project_id", project_id).limit(1).execute().data or []
    return rows[0] if rows else None


def current_status(project_id: str) -> str | None:
    """For the queue: complete and failed both read as 'done' so the AI
    monitor's requeue_terminal refuses; "Retry documents" is the retry path."""
    row = _load_record(get_supabase(), project_id)
    if row is None:
        return None
    status = row.get("files_status")
    return "done" if status in (STATUS_COMPLETE, STATUS_FAILED) else status


def claim_is_stale(record: dict, settings: Settings, now: datetime | None = None) -> bool:
    """A `running` record whose claim is older than the queue lease: the
    worker that held it is gone (the job's own claim takes such a record
    over the same way)."""
    if record.get("files_status") != STATUS_RUNNING:
        return False
    claimed = record.get("files_claimed_at")
    if not isinstance(claimed, str) or not claimed:
        return True
    try:
        at = datetime.fromisoformat(claimed.replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at < (now or _now()) - timedelta(seconds=settings.llm_queue_lease_seconds)


def retryable(record: dict, settings: Settings, now: datetime | None = None) -> bool:
    """Whether "Retry documents" (and a fresh enqueue) may start from this
    record: `none` or `failed`, a `complete` run that skipped documents
    (a re-harvest or a policy change can admit them; promoted files are
    idempotent through the unique index), or a `running` whose claim is
    stale."""
    status = record.get("files_status")
    if status in RETRYABLE_STATUSES:
        return True
    if status == STATUS_COMPLETE and bool(record.get("files_skipped")):
        return True
    return claim_is_stale(record, settings, now)


def mark_pending(sb, project_id: str, prior_status: str) -> bool:
    """CAS `files_status` to `pending` from the exact status the caller
    read, BEFORE the job is enqueued (a crash between the two leaves a
    pending record the retry button can act on, never an enqueued job over
    a `none` record). Clears the error and any claim. False when the
    record moved meanwhile."""
    rows = (
        sb.table(TABLE)
        .update({
            "files_status": STATUS_PENDING,
            "files_error": None,
            "files_claim_token": None,
            "files_claimed_at": None,
        })
        .eq("project_id", project_id)
        .eq("files_status", prior_status)
        .execute()
    ).data or []
    return bool(rows)


def unmark_pending(sb, project_id: str, prior_status: str) -> None:
    """The enqueue failed after `mark_pending`: put the prior status back,
    fenced on `pending`. Never raises."""
    try:
        sb.table(TABLE).update({"files_status": prior_status}).eq("project_id", project_id).eq(
            "files_status", STATUS_PENDING
        ).execute()
    except Exception:  # noqa: BLE001 - the record reads pending; the retry button still works
        logger.exception("rfp create files: could not restore files_status on %s", project_id)


def mark_from_queue(project_id: str, status: str, error: str | None) -> None:
    """The queue's domain marks: 'pending' when a retry is scheduled (the
    claim was released on the way out; nothing to do), 'failed' when the
    ladder is exhausted (fenced so a job that finished normally is not
    overwritten)."""
    if status != STATUS_FAILED:
        return
    sb = get_supabase()
    sb.table(TABLE).update(
        {
            "files_status": STATUS_FAILED,
            "files_error": (error or _MSG_INTERRUPTED)[:_ERROR_MAX_CHARS],
            "files_claim_token": None,
            "files_claimed_at": None,
        }
    ).eq("project_id", project_id).in_("files_status", [STATUS_PENDING, STATUS_RUNNING]).execute()


def error_message(exc: Exception, _label: str) -> str:
    """The sentence the queue stores for a failed job: the service's own
    exceptions carry user sentences; anything else is app-authored."""
    if isinstance(exc, (RfpCreateFilesTransient, RfpCreateFilesPermanent)):
        return str(exc) or _MSG_INTERRUPTED
    return _MSG_INTERRUPTED


# ── The job ──────────────────────────────────────────────────────────────────


def _claim(sb, project_id: str, token: str, settings: Settings) -> bool:
    """CAS the record `running` under a fresh token: from none, pending or
    failed, or from a `running` whose claim is older than the queue lease
    (a worker that died mid-promotion; its writes are fenced on the token
    anyway)."""
    now = _now()
    cutoff = _iso(now - timedelta(seconds=settings.llm_queue_lease_seconds))
    rows = (
        sb.table(TABLE)
        .update({
            "files_status": STATUS_RUNNING,
            "files_claim_token": token,
            "files_claimed_at": _iso(now),
            "files_error": None,
        })
        .eq("project_id", project_id)
        .or_(
            f"files_status.in.({STATUS_NONE},{STATUS_PENDING},{STATUS_FAILED}),"
            f"and(files_status.eq.{STATUS_RUNNING},files_claimed_at.lt.{cutoff})"
        )
        .execute()
    ).data or []
    return bool(rows)


def _update_claimed(sb, project_id: str, token: str, fields: dict) -> None:
    rows = (
        sb.table(TABLE)
        .update(fields)
        .eq("project_id", project_id)
        .eq("files_claim_token", token)
        .execute()
    ).data or []
    if not rows:
        raise _ClaimLost()


def _release(sb, project_id: str, token: str, status: str, error: str | None) -> None:
    try:
        sb.table(TABLE).update({
            "files_status": status,
            "files_claim_token": None,
            "files_claimed_at": None,
            "files_error": error,
        }).eq("project_id", project_id).eq("files_claim_token", token).execute()
    except Exception:  # noqa: BLE001 - the queue records the failure regardless
        logger.exception("rfp create files: release failed for %s", project_id)


def _renew() -> None:
    if not llm_queue.renew_lease():
        raise RfpCreateFilesTransient(_MSG_INTERRUPTED)


def _load_harvest(sb, harvest_id: str | None) -> dict | None:
    if not harvest_id:
        return None
    rows = sb.table(HARVESTS_TABLE).select("*").eq("id", harvest_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _file_rows(sb, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    clean = sorted({str(i) for i in ids if i})
    for i in range(0, len(clean), _IN_CHUNK):
        rows = (
            sb.table(FILES_TABLE).select(_FILE_SELECT).in_("id", clean[i:i + _IN_CHUNK]).execute()
        ).data or []
        for row in rows:
            if row.get("id"):
                out[str(row["id"])] = row
    return out


def _already_promoted(sb, project_id: str) -> set[str]:
    rows = (
        sb.table(PROJECT_FILES_TABLE)
        .select("id, rfp_sandbox_file_id")
        .eq("project_id", project_id)
        .not_.is_("rfp_sandbox_file_id", "null")
        .execute()
    ).data or []
    return {str(r["rfp_sandbox_file_id"]) for r in rows if r.get("rfp_sandbox_file_id")}


def _scratch_dir(settings: Settings) -> Path:
    base = settings.rfp_ingest_scratch_dir or tempfile.gettempdir()
    root = Path(base) / "rfp-create"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="rfp-create-", dir=root))


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return "23505" in text or "duplicate key" in text


def _download(decision: Promote, dest: Path, max_bytes: int) -> bytes | Skip:
    """The verified object into scratch and back as bytes; a missing or
    over-cap object is a Skip, storage trouble is transient."""
    _unlink_quietly(dest)
    try:
        rs.download_to_file(decision.bucket, decision.path, dest, max_bytes=max_bytes)
        return dest.read_bytes()
    except rs.RfpStorageNotFound:
        return Skip(REASON_MISSING_IN_STORAGE)
    except rs.RfpStorageTooLarge:
        return Skip(REASON_TOO_LARGE)
    except (rs.RfpStorageError, httpx.TransportError, OSError) as exc:
        raise RfpCreateFilesTransient(_MSG_STORAGE) from exc
    finally:
        _unlink_quietly(dest)


def _fetch_verified(decision: Promote, dest: Path, max_bytes: int) -> bytes | Skip:
    """Download and re-check the digest (section 11): the original must
    match the verified sha256, a converted PDF must match the conversion
    digest when the manifest recorded one. A PDF is then scanned for the
    byte markers as name tokens (`pdf_marker_keys`); any marker but `/URI`
    keeps it out."""
    data = _download(decision, dest, max_bytes)
    if isinstance(data, Skip):
        return data
    if decision.expected_sha and hashlib.sha256(data).hexdigest() != decision.expected_sha:
        return Skip(REASON_SHA_MISMATCH if decision.check_sha else REASON_CONVERTED_SHA_MISMATCH)
    if decision.content_type == "application/pdf":
        try:
            markers = pdf_marker_keys(data)
        except PdfUnscannable as exc:
            return Skip(f"{REASON_PDF_UNSCANNABLE_PREFIX}{exc.reason}")
        if markers:
            return Skip("marker:" + ",".join(markers))
    return data


def _fetch_entry(
    decision: Promote, file_row: dict, name: str, dest: Path, max_bytes: int,
) -> tuple[Promote, bytes] | Skip:
    """The bytes to promote for one entry, with the decision that describes
    them: the original after its digest check and (for .docx / .xlsx) the
    OOXML container scan; a refused container falls back to the sandbox's
    converted PDF, downloaded and checked in turn."""
    data = _fetch_verified(decision, dest, max_bytes)
    if isinstance(data, Skip):
        return data
    if not decision.ooxml_scan:
        return decision, data
    why = ooxml_container_verdict(io.BytesIO(data))
    if why is None:
        return decision, data
    fmt = str(file_row.get("source_format") or "")
    logger.warning("rfp create files: %s refused as an original (%s); promoting the converted PDF", name, why)
    fallback = converted_promotion(file_row, name, fmt, why=why)
    if isinstance(fallback, Skip):
        return fallback
    data = _fetch_verified(fallback, dest, max_bytes)
    if isinstance(data, Skip):
        return data
    return fallback, data


def fetch_entry(
    decision: Promote, file_row: dict, name: str, dest: Path, max_bytes: int,
) -> tuple[Promote, bytes] | Skip:
    """Public name for `_fetch_entry`: the split step (services/rfp_split)
    stages exactly the bytes the promotion would have admitted."""
    return _fetch_entry(decision, file_row, name, dest, max_bytes)


def fetch_verified(decision: Promote, dest: Path, max_bytes: int) -> bytes | Skip:
    """Public name for `_fetch_verified` (the converted PDF a non-PDF file is
    identified from)."""
    return _fetch_verified(decision, dest, max_bytes)


def file_rows(sb, ids: list[str]) -> dict[str, dict]:
    return _file_rows(sb, ids)


def _promote_one(
    sb, project_id: str, harvest_id: str | None, entry: dict, file_row: dict, decision: Promote,
    data: bytes,
) -> str | None:
    """Upload, insert, audit, preview. Returns the category on a new row,
    None when the unique index said the file was already promoted."""
    category = category_for(entry)
    key = storage.build_object_path(project_id, category, decision.filename)
    try:
        storage.upload_file(key, data, decision.content_type)
    except Exception as exc:  # noqa: BLE001 - storage trouble is transient
        raise RfpCreateFilesTransient(_MSG_STORAGE) from exc
    convertible = office_preview.is_convertible(decision.filename, category)
    row = {
        "project_id": project_id,
        "category": category,
        "storage_path": key,
        "filename": decision.filename,
        "material_category_id": None,
        "uploaded_by": None,
        "mime_type": decision.content_type,
        "size_bytes": len(data),
        "preview_status": "pending" if convertible else "none",
        "note": decision.note,
        "doc_type": None,
        "addendum_number": None,
        "addendum_issued_on": None,
        "estimator_deliverable": False,
        "rfp_harvest_id": harvest_id,
        "rfp_sandbox_file_id": file_row["id"],
        # The pre-split mapping: no splitter provenance (docs/RFP_SPLIT.md 4).
        "bid_split_segment_id": None,
        "bid_split_file_id": None,
        "is_source_set": False,
    }
    try:
        inserted = sb.table(PROJECT_FILES_TABLE).insert(row).execute().data[0]
    except Exception as exc:  # noqa: BLE001 - the unique race is expected; the rest propagate
        try:
            storage.delete_file(key)
        except Exception:  # noqa: BLE001
            logger.exception("rfp create files: orphaned object %s", key)
        if _is_unique_violation(exc):
            return None
        raise RfpCreateFilesTransient(_MSG_STORAGE) from exc
    audit(None, "file.upload", "project_file", inserted.get("id"), {"category": category, "source": "rfp"})
    # The files flag (docs/RFP_BUILDINGCONNECTED.md 3.8): a promoted drawing
    # or specification satisfies it. Best effort inside the service.
    files_needed.clear_if_satisfied(sb, project_id, category, None)
    if convertible:
        try:
            office_preview.generate_preview(inserted["id"])
        except Exception:  # noqa: BLE001 - a preview is a convenience
            logger.exception("rfp create files: preview failed for %s", inserted.get("id"))
    return category


def _notify_drawings(sb, project_id: str, count: int) -> None:
    """One bell for the promoted drawings, the files router's rule: nothing
    while intake is still assembling the package (a freshly created project
    always is; a retried promotion after intake completed rings), then both
    engineer focuses and every active estimator on the project."""
    from app.services import workflow

    if count <= 0:
        return
    if not workflow.is_category_complete(workflow.load_category_state(project_id), "intake"):
        return
    rows = sb.table("projects").select("id, name, number").eq("id", project_id).limit(1).execute().data or []
    proj = rows[0] if rows else {}
    label = f"{proj.get('number') or ''} {proj.get('name') or ''}".strip() or "a project"
    noun = "drawing" if count == 1 else "drawings"
    msg = f"{count} {noun} added for {label} from the RFP invitation; re-check anything priced off them."
    notify_role(Role.ESTIMATING_ENGINEER_MATERIALS, project_id, "drawing_changed", msg)
    notify_role(Role.ESTIMATING_ENGINEER_LABOR, project_id, "drawing_changed", msg)
    assignments = (
        sb.table("estimator_assignments")
        .select("estimator_id")
        .eq("project_id", project_id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    ).data or []
    for est_id in {a["estimator_id"] for a in assignments if a.get("estimator_id")}:
        notify_user(est_id, project_id, "drawing_changed", msg)


def execute(project_id: str, harvest_id: str | None = None) -> None:
    """The rfp_create_files job (section 5). Claims the record, walks the
    harvest's entries in order, promotes what the sandbox verified, and
    ends the record `complete` with the counts and the skipped list.

    `harvest_id` is the harvest the job payload names, and it WINS over the
    record's: the create-time duplicate guard's recent-project link
    (docs/RFP_CREATE.md 4.6) enqueues THIS invitation's harvest against a
    project whose record belongs to another invitation's, and without the
    payload id the job would promote that other harvest again and drop this
    one's documents. The record is still what the job claims and reports
    through, so the link inserts one when the target project has none."""
    settings = get_settings()
    sb = get_supabase()
    record = _load_record(sb, project_id)
    if record is None:
        raise RfpCreateFilesPermanent(_MSG_NOT_FOUND)
    source_harvest_id = str(harvest_id) if harvest_id else record.get("harvest_id")
    token = uuid.uuid4().hex
    if not _claim(sb, project_id, token, settings):
        # Another worker holds a live claim (or a dead one not yet past the
        # lease): the queue's ladder retries, and its exhaustion marks the
        # record failed, so a `running` record is never stranded.
        logger.warning("rfp create files: lost the claim on %s; another worker owns it", project_id)
        raise RfpCreateFilesTransient(_MSG_INTERRUPTED)

    promoted = 0
    drawings = 0
    skipped: list[dict] = []
    scratch: Path | None = None
    split_rows: dict = {}
    try:
        harvest = _load_harvest(sb, source_harvest_id)
        entries = [e for e in ((harvest or {}).get("files") or []) if isinstance(e, dict)]
        wanted = [
            str(e["sandbox_file_id"]) for e in entries
            if e.get("sandbox_file_id") and e.get("status") in ENTRY_WITH_FILE
        ]
        file_rows = _file_rows(sb, wanted) if wanted else {}
        done_ids = _already_promoted(sb, project_id) if wanted else set()
        # The splitter's outcome per sandbox file (docs/RFP_SPLIT.md 4): a
        # `done` row with segments is promoted through rfp_split; a failed
        # row, or none (flags off, skipped, over the cap), takes the
        # pre-split mapping below. The split job is the promoted HARVEST's
        # (the record's copy of it is only a denormalization for the page,
        # and it names the record's harvest, not the payload's).
        split_job_id = (harvest or {}).get("split_job_id") or record.get("split_job_id")
        split_rows = rfp_split.split_rows_for_job(sb, split_job_id) if wanted else {}
        existing_rows = rfp_split.project_rows(sb, project_id) if split_rows else []
        promoted = len([sid for sid in done_ids if sid not in split_rows])
        scratch = _scratch_dir(settings) if wanted else None
        handled: set[str] = set()

        for i, entry in enumerate(entries):
            name = entry_name(entry, file_rows.get(str(entry.get("sandbox_file_id") or "")))
            file_row = file_rows.get(str(entry.get("sandbox_file_id") or ""))
            decision = promotion_for(entry, file_row)
            if isinstance(decision, Skip):
                skipped.append({"file_path": name, "reason": decision.reason})
                continue
            sid = str(file_row["id"])
            if sid in handled:
                continue  # the same bytes listed twice by the portal: one sandbox file, one promotion
            handled.add(sid)
            split_file, segments = split_rows.get(sid, (None, []))
            if split_file is not None and split_file.get("status") == "done" and segments:
                _renew()
                dest = scratch / f"{i}-{uuid.uuid4().hex}"
                result = rfp_split.promote_split_file(
                    sb, project_id=project_id, harvest_id=source_harvest_id, split_file=split_file,
                    segments=segments, existing=rfp_split.rows_for_file(existing_rows, split_file),
                    fetch_original=lambda: _fetch_entry(decision, file_row, name, dest, settings.upload_max_bytes),
                    name=name,
                )
                promoted += result.documents
                drawings += result.drawings
                skipped.extend(result.skipped)
                done_ids.add(sid)
                _update_claimed(sb, project_id, token, {"files_promoted": promoted})
                continue
            if sid in done_ids:
                continue  # a retry: already in the project
            _renew()
            fetched = _fetch_entry(
                decision, file_row, name, scratch / f"{i}-{uuid.uuid4().hex}", settings.upload_max_bytes
            )
            if isinstance(fetched, Skip):
                skipped.append({"file_path": name, "reason": fetched.reason})
                continue
            decision, data = fetched
            category = _promote_one(
                sb, project_id, source_harvest_id, entry, file_row, decision, data
            )
            promoted += 1
            if category in DRAWING_CATEGORIES:
                drawings += 1
            done_ids.add(str(file_row["id"]))
            # Progress survives a crash between files: the counts so far.
            _update_claimed(sb, project_id, token, {"files_promoted": promoted})
    except _ClaimLost:
        logger.warning("rfp create files: claim on %s lost mid-run; stopping", project_id)
        return
    except RfpCreateFilesTransient as exc:
        _release(sb, project_id, token, STATUS_PENDING, str(exc)[:_ERROR_MAX_CHARS])
        raise
    except Exception as exc:  # noqa: BLE001 - unexpected trouble is still worth a retry
        logger.exception("rfp create files: promotion failed for %s", project_id)
        _release(sb, project_id, token, STATUS_PENDING, _MSG_INTERRUPTED)
        raise RfpCreateFilesTransient(_MSG_INTERRUPTED) from exc
    finally:
        if scratch is not None:
            try:
                for leftover in scratch.iterdir():
                    _unlink_quietly(leftover)
                scratch.rmdir()
            except OSError:
                pass

    try:
        if split_rows:
            try:
                promoted = rfp_split.count_documents(sb, project_id)
            except Exception:  # noqa: BLE001 - the running count stands
                logger.exception("rfp create files: document count failed for %s", project_id)
        _update_claimed(sb, project_id, token, {
            "files_status": STATUS_COMPLETE,
            "files_promoted": promoted,
            "files_skipped": skipped[:_SKIPPED_CAP],
            "files_error": None,
            "files_claim_token": None,
            "files_claimed_at": None,
        })
    except _ClaimLost:
        logger.warning("rfp create files: claim on %s lost at the end; result not recorded", project_id)
        return
    try:
        _notify_drawings(sb, project_id, drawings)
    except Exception:  # noqa: BLE001 - the bell is a courtesy
        logger.exception("rfp create files: drawing bell failed for %s", project_id)
