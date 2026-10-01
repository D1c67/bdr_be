"""Parent-side byte checks and string sanitizers for RFP Ingestion.

This is the ONLY place the API process looks inside an inbound RFP file or at
anything the sandbox child produced, and it does so with pure byte scans and
stdlib Unicode tables. Nothing here parses a PDF, decodes a JPEG, or imports
pypdfium2 / Pillow; the module must stay importable with the standard library
alone so the trust boundary in docs/RFP_INGESTION_SANDBOX.md (section 1) holds:
the parent never runs a parser over attacker-controlled bytes.

What lives here, and who calls it:

* Type sniff (`sniff_bytes`, `sniff_file`): the verdict-or-flags decision of
  section 2. Two verdicts only: `not_pdf` when `%PDF-` does not start within
  the first 1024 bytes, `polyglot` when a foreign magic sits at offset 0 (after
  an optional UTF-8 BOM plus leading whitespace for the text formats). Both
  are checked in that order, so a plain ZIP or EXE with no PDF header is
  `not_pdf` and `polyglot` is reserved for files that really do carry a PDF
  behind another format's header. An empty input is `empty`. Everything else
  is a manifest FLAG: header offset, foreign magics found before the header,
  bytes after the last `%%EOF`, a ZIP end-of-central-directory record in the
  last 64 KB, a missing `%%EOF`, and occurrence counts of every
  `protocol.BYTE_MARKERS` entry over the whole file. The router sniffs the
  uploaded bytes in memory; the runner sniffs the materialized scratch file in
  1 MB chunks. Both go through one streaming scanner so their answers are
  identical, including markers that straddle a chunk boundary.
  With a `filename` (section 2.1), a file that has NO PDF header may instead
  be recognised as an office document, by magic AND declared extension
  together: OOXML (`PK\x03\x04` at offset 0, name ending `.docx` / `.xlsx`)
  or legacy OLE2 (`D0 CF 11 E0 A1 B1 1A E1` at offset 0, name ending `.doc`
  / `.xls`). The zip is never opened and the compound file is never walked;
  the parent hands the bytes to the converter as they are. `source_format`
  says what the file was taken for. The PDF path is unchanged by the
  filename: a `.docx` name over PDF bytes is a PDF, a `PK` file named `.pdf`
  is still `not_pdf`, and without a filename (the default) nothing but a
  PDF passes, which is also the flag-off behaviour.
* Identity (`sha256_bytes`, `sha256_file`): the content hash stored on every
  file row and used by the duplicate index.
* JPEG marker walk (`jpeg_dimensions`): section 3.4. Walks the segment
  structure of a page artifact the child wrote, allows only the markers a
  Pillow baseline or progressive encode can produce, pins the single JFIF
  APP0 of that encode field by field so no segment carries bytes the child
  chose, reads the dimensions and component count from the SOF, and refuses
  anything else. It never decodes a single pixel; the decode itself happens in
  the child's `--verify` spawn.
* Text contract (`sanitize_text`): section 2. Strips control characters and
  the hidden Unicode categories (Cf, Co, Cs, Cn, plus U+FFFD) and counts what
  it stripped under `protocol.TEXT_HAZARD_KEYS`, so the agent slice can treat
  pages with hidden content as suspicious. `app/sandbox/textclean.py` mirrors
  these semantics in the child; the parent re-runs this over the child's text
  and its answer is the one stored.
* Display strings (`sanitize_display`, `sanitize_stderr_tail`): filenames,
  metadata values, detail strings and the child's stderr are attacker- or
  child-authored and end up in JSON columns, logs and the UI. They are reduced
  to printable characters, whitespace-collapsed and capped, and the helpers
  never raise (a hostile `__str__` yields an empty string, not a 500).

Every ValueError raised here carries an app-authored sentence that names the
structural problem; callers may surface it as a detail string. No message ever
embeds file content, a filename, or child output verbatim.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass

from app.sandbox import protocol

# ── Sniff constants ──────────────────────────────────────────────────────────

PDF_HEADER = b"%PDF-"
# The header must START within this many bytes (Acrobat tolerates junk up to
# here; anything later is not a PDF as far as this pipeline is concerned).
PDF_HEADER_WINDOW = 1024
PDF_EOF = b"%%EOF"
ZIP_EOCD = b"PK\x05\x06"
ZIP_TAIL_WINDOW = 64 * 1024

_UTF8_BOM = b"\xef\xbb\xbf"
_ASCII_WHITESPACE = b" \t\r\n\x0b\x0c"
# Bytes that may legitimately trail the final %%EOF (an end-of-line marker).
_EOF_TRAILER_WHITESPACE = b"\r\n\t "

# Foreign magics. Binary ones must sit at offset 0 exactly; text ones are
# checked after an optional BOM and leading whitespace, and the four markup
# magics are matched case-insensitively (ASCII case only, via bytes.lower()).
_BINARY_MAGICS: tuple[tuple[str, bytes], ...] = (
    ("zip", b"PK\x03\x04"),
    ("mz", b"MZ"),
    ("elf", b"\x7fELF"),
    ("gif", b"GIF8"),
    ("jpeg", b"\xff\xd8\xff"),
    ("png", b"\x89PNG"),
)
_TEXT_MAGICS: tuple[tuple[str, bytes, bool], ...] = (
    ("rtf", b"{\\rtf", False),
    ("ps", b"%!PS", False),
    ("doctype", b"<!doctype", True),
    ("html", b"<html", True),
    ("script", b"<script", True),
    ("xml", b"<?xml", True),
)
_ALL_MAGICS: tuple[tuple[str, bytes, bool], ...] = tuple(
    (name, magic, False) for name, magic in _BINARY_MAGICS
) + _TEXT_MAGICS

# The head buffer must hold every byte a header search or an offset-0 magic
# check can touch: 1024 candidate header starts plus the header itself.
_HEAD_WINDOW = PDF_HEADER_WINDOW + len(PDF_HEADER) + 16
# Streaming carry: the longest pattern counted across chunk boundaries, minus
# one byte, is enough for any occurrence that straddles a boundary to appear
# whole in (carry + chunk) and nowhere else.
_CARRY_BYTES = max(len(m) for m in (*protocol.BYTE_MARKERS, PDF_EOF)) - 1

# Office magics (section 2.1): each is accepted only together with a declared
# extension that names the same family. Neither container is ever opened here.
_OOXML_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OFFICE_EXTENSIONS: dict[str, tuple[bytes, str]] = {
    ".docx": (_OOXML_MAGIC, protocol.SOURCE_FORMAT_DOCX),
    ".xlsx": (_OOXML_MAGIC, protocol.SOURCE_FORMAT_XLSX),
    ".doc": (_OLE2_MAGIC, protocol.SOURCE_FORMAT_DOC),
    ".xls": (_OLE2_MAGIC, protocol.SOURCE_FORMAT_XLS),
}

# Every key `SniffResult.flags` carries, in manifest order.
SNIFF_FLAG_KEYS = (
    "pdf_header_offset",
    "markers_in_head",
    "bytes_after_last_eof",
    "zip_eocd_in_tail",
    "missing_eof",
)


@dataclass(frozen=True)
class SniffResult:
    """The type-sniff outcome for one file.

    `verdict` is None (the file may proceed, `flags` and `byte_markers` go
    in the manifest) or one of `protocol.REJECT_NOT_PDF`,
    `protocol.REJECT_POLYGLOT`, `protocol.REJECT_EMPTY`. `source_format` is
    one of `protocol.SOURCE_FORMATS` when the verdict is None (`pdf` for a
    file with a PDF header, the office format otherwise) and None for a
    rejected file. `flags` always carries every key in `SNIFF_FLAG_KEYS`;
    `byte_markers` always carries every `protocol.BYTE_MARKERS` entry
    (decoded ASCII) with its count; both describe the bytes as a PDF would
    be read and are recorded for office files too.
    """

    verdict: str | None
    pdf_header_offset: int | None
    flags: dict
    byte_markers: dict[str, int]
    source_format: str | None = None


def declared_extension(filename: str | None) -> str:
    """The lower-cased extension of a display filename, dot included, or an
    empty string. Only the final component after the last dot counts."""
    name = str(filename or "")
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return name[dot:].lower()


def office_format(head: bytes, filename: str | None) -> str | None:
    """The office `source_format` for a file whose first bytes are `head`
    and whose declared name is `filename`, or None. Both must agree: the
    magic AT OFFSET 0 and an extension naming that container's family. No
    filename means no office format, ever."""
    if not filename:
        return None
    entry = _OFFICE_EXTENSIONS.get(declared_extension(filename))
    if entry is None:
        return None
    magic, fmt = entry
    return fmt if head.startswith(magic) else None


class _SniffScanner:
    """Incremental scanner shared by `sniff_bytes` and `sniff_file`.

    Feed chunks of any size (one chunk for in-memory data), then call
    `result()`. State is bounded regardless of file size: the first
    `_HEAD_WINDOW` bytes, the last `ZIP_TAIL_WINDOW` bytes, a
    `_CARRY_BYTES` overlap, the marker counters and the last `%%EOF` offset.
    `filename` enables the office-format branch of `result()`; None keeps
    the PDF-only table.
    """

    def __init__(self, *, filename: str | None = None) -> None:
        self.filename = filename
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()
        self.carry = b""
        self.counts: dict[bytes, int] = {m: 0 for m in protocol.BYTE_MARKERS}
        self.last_eof_end: int | None = None
        self.nonspace_after_eof = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(self.head) < _HEAD_WINDOW:
            self.head += chunk[: _HEAD_WINDOW - len(self.head)]
        # Every occurrence is counted exactly once: an occurrence is credited to
        # the chunk it ENDS in, and (len(m) - 1) carried bytes are exactly enough
        # for a straddling occurrence to appear whole without re-counting one
        # that ended in the previous chunk. No marker is a self-overlapping
        # pattern (each starts with its only "/"), so bytes.count is exact.
        for marker in protocol.BYTE_MARKERS:
            window = self.carry[len(self.carry) - (len(marker) - 1) :] + chunk
            self.counts[marker] += window.count(marker)
        eof_carry = self.carry[len(self.carry) - (len(PDF_EOF) - 1) :]
        window = eof_carry + chunk
        idx = window.rfind(PDF_EOF)
        if idx >= 0:
            rel_end = idx + len(PDF_EOF) - len(eof_carry)
            self.last_eof_end = self.total + rel_end
            self.nonspace_after_eof = _has_non_eol(chunk[rel_end:])
        elif self.last_eof_end is not None and not self.nonspace_after_eof:
            self.nonspace_after_eof = _has_non_eol(chunk)
        self.tail += chunk
        if len(self.tail) > ZIP_TAIL_WINDOW:
            del self.tail[: len(self.tail) - ZIP_TAIL_WINDOW]
        self.carry = (self.carry + chunk)[-_CARRY_BYTES:]
        self.total += len(chunk)

    def result(self) -> SniffResult:
        byte_markers = {m.decode("ascii"): n for m, n in self.counts.items()}
        if self.total == 0:
            flags = {
                "pdf_header_offset": None,
                "markers_in_head": [],
                "bytes_after_last_eof": 0,
                "zip_eocd_in_tail": False,
                "missing_eof": True,
            }
            return SniffResult(protocol.REJECT_EMPTY, None, flags, byte_markers)
        head = bytes(self.head)
        offset = head[: PDF_HEADER_WINDOW + len(PDF_HEADER) - 1].find(PDF_HEADER)
        header_offset = offset if offset >= 0 else None
        # Markers "before the header": with no header at all, the whole window
        # a header could have occupied is scanned instead.
        head_before = head[:header_offset] if header_offset is not None else head[:PDF_HEADER_WINDOW]
        if self.last_eof_end is None:
            missing_eof, after_eof = True, 0
        else:
            missing_eof = False
            after_eof = (self.total - self.last_eof_end) if self.nonspace_after_eof else 0
        flags = {
            "pdf_header_offset": header_offset,
            "markers_in_head": _magics_in(head_before),
            "bytes_after_last_eof": after_eof,
            "zip_eocd_in_tail": ZIP_EOCD in self.tail,
            "missing_eof": missing_eof,
        }
        source_format: str | None = None
        if header_offset is None:
            # No PDF anywhere in the window. The office branch is the only
            # way past this point, and only with a filename to check.
            source_format = office_format(head, self.filename)
            verdict: str | None = None if source_format else protocol.REJECT_NOT_PDF
        elif _foreign_magic_at_zero(head) is not None:
            verdict = protocol.REJECT_POLYGLOT
        else:
            verdict = None
            source_format = protocol.SOURCE_FORMAT_PDF
        return SniffResult(verdict, header_offset, flags, byte_markers, source_format)


def _has_non_eol(data: bytes) -> bool:
    """True when `data` holds anything other than end-of-line whitespace."""
    return bool(data.translate(None, _EOF_TRAILER_WHITESPACE))


def _foreign_magic_at_zero(head: bytes) -> str | None:
    """Short name of the foreign format whose magic opens the file, or None."""
    for name, magic in _BINARY_MAGICS:
        if head.startswith(magic):
            return name
    body = head[len(_UTF8_BOM) :] if head.startswith(_UTF8_BOM) else head
    body = body.lstrip(_ASCII_WHITESPACE)
    lowered = body[:16].lower()
    for name, magic, case_insensitive in _TEXT_MAGICS:
        if (lowered if case_insensitive else body).startswith(magic):
            return name
    return None


def _magics_in(region: bytes) -> list[str]:
    """Short names of every foreign magic found anywhere in `region`, ordered
    by first occurrence (a stable, dedup'd audit list for the manifest)."""
    lowered = region.lower()
    found = []
    for name, magic, case_insensitive in _ALL_MAGICS:
        idx = (lowered if case_insensitive else region).find(magic)
        if idx >= 0:
            found.append((idx, name))
    found.sort()
    return [name for _, name in found]


def sniff_bytes(data: bytes, *, filename: str | None = None) -> SniffResult:
    """Type-sniff a whole file held in memory (router uploads). `filename`
    (the declared name, display-sanitized) enables the office-format branch;
    without it the answer is the PDF-only table."""
    scanner = _SniffScanner(filename=filename)
    scanner.feed(bytes(data))
    return scanner.result()


def sniff_file(
    path: str | os.PathLike, *, chunk_bytes: int = 1048576, filename: str | None = None
) -> SniffResult:
    """Type-sniff a file on disk in `chunk_bytes` reads, with the exact
    semantics of `sniff_bytes` (a marker straddling a chunk boundary is
    counted once). Bounded memory regardless of file size. `filename` is
    the DECLARED name, never `path` (the scratch file is always named by
    the parent)."""
    if chunk_bytes < 1:
        raise ValueError("chunk_bytes must be at least 1")
    scanner = _SniffScanner(filename=filename)
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_bytes):
            scanner.feed(chunk)
    return scanner.result()


# ── Identity ─────────────────────────────────────────────────────────────────


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike, *, chunk_bytes: int = 1048576) -> tuple[str, int]:
    """(hex digest, byte size) of a file, read in `chunk_bytes` pieces. The
    size is what was actually read, never a value copied from elsewhere."""
    if chunk_bytes < 1:
        raise ValueError("chunk_bytes must be at least 1")
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_bytes):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ── JPEG marker walk ─────────────────────────────────────────────────────────

_JPEG_SOI = 0xD8
_JPEG_EOI = 0xD9
_JPEG_SOS = 0xDA
_JPEG_SOF0 = 0xC0
_JPEG_SOF2 = 0xC2
_JPEG_DHT = 0xC4
_JPEG_DQT = 0xDB
_JPEG_DRI = 0xDD
_JPEG_APP0 = 0xE0
_JPEG_SOF_MARKERS = frozenset({_JPEG_SOF0, _JPEG_SOF2})
# Segments allowed before the first scan, and between scans of a progressive
# file (a second SOF or an APP0 after the first scan is structurally wrong).
_JPEG_BEFORE_SCAN = frozenset(
    {_JPEG_APP0, _JPEG_DQT, _JPEG_SOF0, _JPEG_SOF2, _JPEG_DHT, _JPEG_DRI, _JPEG_SOS}
)
_JPEG_BETWEEN_SCANS = frozenset({_JPEG_DQT, _JPEG_DHT, _JPEG_DRI, _JPEG_SOS})
_JPEG_PRECISION = 8
# The one APP0 a Pillow encode writes: a 16-byte JFIF segment (2 length bytes
# plus 14 of payload) whose only free fields are the version, the density unit
# and the two density values. Anything else in an APP0 is a byte channel the
# child would control, so the walk pins the whole shape.
_JPEG_APP0_LENGTH = 16
_JPEG_JFIF_ID = b"JFIF\x00"
_JPEG_JFIF_MAJOR = 1
_JPEG_JFIF_MAX_MINOR = 2
_JPEG_JFIF_MAX_UNITS = 2


def jpeg_dimensions(buf: bytes) -> tuple[int, int]:
    """Return (width, height) of a page JPEG after walking its marker structure.

    Accepts exactly what a Pillow baseline or progressive RGB encode produces:
    SOI, one JFIF APP0 as the first segment, then any of DQT / SOF0 / SOF2 /
    DHT / DRI / SOS, entropy data in which 0xFF is followed only by 0x00
    (stuffing) or an RSTn marker, DHT / DQT / DRI / SOS between the scans of a
    progressive file, and EOI as the final two bytes. The APP0 must be the
    16-byte JFIF segment of that encode (see `_check_app0`), so it cannot
    carry a payload of the child's choosing, and a second APP0 is refused. The
    SOF must be 8-bit with exactly `protocol.JPEG_COMPONENTS` components and
    non-zero dimensions. Any other marker (APP1/EXIF, COM, APP14/Adobe, a
    second SOF, ...), a truncated segment, a missing SOF or SOS, entropy data
    that runs off the end, or any byte after EOI raises ValueError. Nothing is
    decoded.
    """
    if not isinstance(buf, (bytes, bytearray)):
        buf = bytes(buf)
    n = len(buf)
    if n < 4 or buf[0] != 0xFF or buf[1] != _JPEG_SOI:
        raise ValueError("JPEG does not start with an SOI marker")
    pos = 2
    sof: tuple[int, int] | None = None
    scans = 0
    while True:
        if pos + 2 > n:
            raise ValueError("JPEG ends without an EOI marker")
        if buf[pos] != 0xFF:
            raise ValueError(f"expected a JPEG marker at offset {pos}")
        marker = buf[pos + 1]
        if marker == _JPEG_EOI:
            if scans == 0 or sof is None:
                raise ValueError("JPEG has an EOI marker before any scan")
            if pos + 2 != n:
                raise ValueError("JPEG has trailing bytes after the EOI marker")
            height, width = sof
            return width, height
        allowed = _JPEG_BEFORE_SCAN if scans == 0 else _JPEG_BETWEEN_SCANS
        if marker not in allowed:
            raise ValueError(f"JPEG marker 0x{marker:02X} is not allowed at offset {pos}")
        if pos + 4 > n:
            raise ValueError("JPEG segment header is truncated")
        length = (buf[pos + 2] << 8) | buf[pos + 3]
        if length < 2:
            raise ValueError("JPEG segment length is below the two-byte minimum")
        seg_end = pos + 2 + length
        if seg_end > n:
            raise ValueError("JPEG segment runs past the end of the data")
        payload = buf[pos + 4 : seg_end]
        if marker == _JPEG_APP0:
            if pos != 2:
                raise ValueError("JPEG has an APP0 segment that is not the first segment")
            _check_app0(length, payload)
        elif marker in _JPEG_SOF_MARKERS:
            if sof is not None:
                raise ValueError("JPEG has more than one SOF segment")
            sof = _parse_sof(payload)
        elif marker == _JPEG_DRI:
            if length != 4:
                raise ValueError("JPEG DRI segment has the wrong length")
        elif marker == _JPEG_SOS:
            if sof is None:
                raise ValueError("JPEG has an SOS segment before any SOF")
            components = payload[0] if payload else 0
            if components < 1 or len(payload) != 4 + 2 * components:
                raise ValueError("JPEG SOS segment has the wrong length")
            scans += 1
            pos = _skip_entropy_data(buf, seg_end)
            continue
        pos = seg_end


def _check_app0(length: int, payload: bytes) -> None:
    """Pin an APP0 to the JFIF segment a Pillow encode writes.

    The length is fixed at 16, the payload starts with the JFIF identifier,
    the version is 1.00 to 1.02, the density unit is one of the three JFIF
    values with non-zero densities, and the embedded thumbnail is 0 x 0 (a
    thumbnail would need 3 * w * h more bytes than this length allows). That
    leaves an APP0 no room for arbitrary bytes. Only the first segment of the
    file is ever passed here.
    """
    if length != _JPEG_APP0_LENGTH:
        raise ValueError("JPEG APP0 segment has the wrong length")
    if not payload.startswith(_JPEG_JFIF_ID):
        raise ValueError("JPEG APP0 segment is not a JFIF segment")
    major, minor, units = payload[5], payload[6], payload[7]
    if major != _JPEG_JFIF_MAJOR or minor > _JPEG_JFIF_MAX_MINOR:
        raise ValueError("JPEG APP0 segment declares an unexpected JFIF version")
    if units > _JPEG_JFIF_MAX_UNITS:
        raise ValueError("JPEG APP0 segment declares an unknown density unit")
    x_density = (payload[8] << 8) | payload[9]
    y_density = (payload[10] << 8) | payload[11]
    if x_density < 1 or y_density < 1:
        raise ValueError("JPEG APP0 segment declares a zero pixel density")
    if payload[12] != 0 or payload[13] != 0:
        raise ValueError("JPEG APP0 segment declares an embedded thumbnail")


def _parse_sof(payload: bytes) -> tuple[int, int]:
    """(height, width) from an SOF payload, with the structural checks."""
    if len(payload) < 6:
        raise ValueError("JPEG SOF segment is truncated")
    precision = payload[0]
    height = (payload[1] << 8) | payload[2]
    width = (payload[3] << 8) | payload[4]
    components = payload[5]
    if precision != _JPEG_PRECISION:
        raise ValueError("JPEG sample precision is not 8 bits")
    if components != protocol.JPEG_COMPONENTS:
        raise ValueError(
            f"JPEG has {components} color components, expected {protocol.JPEG_COMPONENTS}"
        )
    if len(payload) != 6 + 3 * components:
        raise ValueError("JPEG SOF segment has the wrong length")
    if width < 1 or height < 1:
        raise ValueError("JPEG dimensions must be at least 1 x 1")
    return height, width


def _skip_entropy_data(buf: bytes, start: int) -> int:
    """Offset of the first real marker after the entropy-coded data that begins
    at `start`. Only 0xFF00 (stuffing) and RST0-7 may occur inside it."""
    n = len(buf)
    i = start
    while True:
        j = buf.find(b"\xff", i)
        if j < 0 or j + 1 >= n:
            raise ValueError("JPEG entropy data runs off the end without a marker")
        following = buf[j + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7:
            i = j + 2
            continue
        return j


# ── Text and display sanitizers ──────────────────────────────────────────────

# Characters that need a category lookup: anything outside printable ASCII
# plus the three line-structure controls we keep. Printable ASCII is passed
# straight through so English spec text costs one C-level regex pass.
_NEEDS_INSPECTION = re.compile(r"[^\x20-\x7e\t\n\r]")
# The text contract keeps only \n and \t of the C0 controls; \r is dropped like
# the rest (the child's textclean.py applies the same table), so page text
# never carries CRLF. The display helpers above keep their wider table because
# they collapse whitespace anyway.
_TEXT_INSPECTION = re.compile(r"[^\x20-\x7e\t\n]")
_REPLACEMENT_CHAR = "�"
_STDERR_TAIL_MAX_CHARS = 4096


def sanitize_text(text: str) -> tuple[str, dict[str, int]]:
    """Apply the text contract to one page of extracted text.

    Returns (clean, hazards). Dropped without counting: control characters
    (category Cc, including U+0000 and `\\r`) other than `\\n` and `\\t`, so
    line structure survives while PDFium's CRLF line endings collapse to LF
    (exactly what the child's `app.sandbox.textclean.sanitize` does; the two
    must agree, and tests/test_rfp_sanitize.py compares them on a generated
    corpus). Dropped AND counted under `protocol.TEXT_HAZARD_KEYS`:
    category Cf as `format_chars` (zero-width and bidi controls, BOM, soft
    hyphen, tags), Co as `private_use`, Cn and Cs as `unassigned`, and U+FFFD
    as `replacement_chars`. Every hazard key is always present. Everything
    else, including every printable script and all whitespace, is untouched.
    """
    if not isinstance(text, str):
        raise TypeError("sanitize_text expects a str")
    hazards = dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0)

    def _inspect(match: re.Match) -> str:
        ch = match.group(0)
        if ch == _REPLACEMENT_CHAR:
            hazards["replacement_chars"] += 1
            return ""
        category = unicodedata.category(ch)
        if category == "Cc":
            return ""
        if category == "Cf":
            hazards["format_chars"] += 1
            return ""
        if category == "Co":
            hazards["private_use"] += 1
            return ""
        if category in ("Cn", "Cs"):
            hazards["unassigned"] += 1
            return ""
        return ch

    return _TEXT_INSPECTION.sub(_inspect, text), hazards


def _strip_hidden(match: re.Match) -> str:
    """Drop every C* category code point; keep everything else."""
    ch = match.group(0)
    return "" if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn", "Cs") else ch


def _to_text(value: object) -> str:
    """str() for display purposes that cannot raise and never renders bytes
    as a Python literal."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        # A hostile or broken __str__ on untrusted metadata must not become a 500.
        return ""


def sanitize_display(value: object, *, max_chars: int = 200) -> str:
    """Reduce an untrusted value (filename, metadata field, detail string) to
    one line of printable text for JSON columns, logs and the UI.

    Coerces with str() (bytes are UTF-8 decoded with replacement, None is
    empty), drops every control / format / bidi / private-use / unassigned /
    surrogate code point, collapses any run of Unicode whitespace (including
    newlines and NBSP) to one space, trims the ends and caps the length.
    Never raises.
    """
    text = _NEEDS_INSPECTION.sub(_strip_hidden, _to_text(value))
    text = " ".join(text.split())
    return text[: max(0, int(max_chars))]


def sanitize_stderr_tail(
    data: bytes, *, max_bytes: int = protocol.MAX_STDERR_TAIL_BYTES
) -> str:
    """The last `max_bytes` of a child's stderr as safe text for the manifest.

    UTF-8 decoded with replacement (a cut inside a multibyte sequence yields
    one U+FFFD, which is kept as evidence), then cleaned like
    `sanitize_display` line by line: hidden code points dropped, whitespace
    within a line collapsed, blank lines removed, newlines kept between the
    surviving lines (CR and CRLF count as newlines). The result is capped at
    4096 characters from the end, since it is a tail. Never raises.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    raw = bytes(data)
    tail = raw[-max_bytes:] if max_bytes > 0 else b""
    text = tail.decode("utf-8", errors="replace")
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = " ".join(_NEEDS_INSPECTION.sub(_strip_hidden, line).split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)[-_STDERR_TAIL_MAX_CHARS:]
