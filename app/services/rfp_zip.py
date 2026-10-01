"""Zip archives inside an RFP harvest (docs/RFP_HARVEST.md 2.5, "Zips").

A GC's email attaches a zip, a shared folder holds one, and Dropbox serves a
whole folder as one: every one of them is opened here and nowhere else, under
hard caps, so a hostile archive can never inflate past the harvest's limits.

`inspect` reads the central directory only and answers one ZipMember per
non-directory entry, in `infolist()` order, either downloadable or carrying
a `skipped_reason` the harvester records. Names are reduced to safe relative
paths (no absolute paths, no drive letters, no `..`); `__MACOSX/`, dotfiles,
`Thumbs.db` and `desktop.ini` are dropped without a trace, as are directory
entries. Encrypted members, nested archives and members declared larger than
the per-file cap are listed but never opened. An archive whose kept members
declare more bytes than the harvest accepts, or whose members show a
decompression-bomb ratio, answers an error and no members at all.

`extract_member` inflates one member into a fresh file (O_EXCL, removed on
any failure) with the cap enforced on the bytes actually inflated, never on
the declared size.

Before `zipfile` ever parses a central directory, `precheck` reads the
archive's end record (and the ZIP64 one when a locator is present, which
is what `zipfile` honours too) and refuses an archive whose declared entry
count or central directory size is past a small cap: `zipfile.ZipFile`
materialises one ZipInfo per entry and reads the whole directory into
memory in the API worker, so a many-entry archive was a memory bomb until
this check. The OOXML container scan (`rfp_create_files`) and the office
pre-conversion scan (`rfp_office_convert`) reuse `precheck` and
`read_member_bounded` for the same reason, and read the XML parts they
inspect through `relationships` and `flattened_xml`: a real XML parse
(expat, over bounded bytes), so an entity-encoded attribute, a prefixed
element, a UTF-16 part or a field instruction split across runs reads the
way Word and LibreOffice read it, never the way a regex over the raw bytes
would. A part that does not parse, or that declares a DTD, is None to both
and the callers fail closed.
"""

from __future__ import annotations

import os
import re
import struct
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from xml.parsers import expat

_MAGIC = b"PK\x03\x04"
_CHUNK = 1024 * 1024
# The end-record bounds: an archive declaring more entries than this, or a
# central directory larger than this, is refused before zipfile parses it.
# The harvest keeps at most rfp_harvest_file_cap (250) members plus as many
# skipped ones; the OOXML scan passes its own member cap.
MAX_ENTRIES = 2000
MAX_CENTRAL_DIRECTORY_BYTES = 4 * 1024 * 1024
# Members are read in chunks this size under a running counter, so a member
# whose declared size lies (or whose deflate stream is a bomb) never inflates
# in one call (`ZipExtFile.read()` with no size inflates the whole stream).
MEMBER_READ_CHUNK = 64 * 1024
_EOCD_SIG = b"PK\x05\x06"
_EOCD_SIZE = 22
_EOCD_MAX_COMMENT = 0xFFFF
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE = 20
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_ZIP64_EOCD_SIZE = 56
_TAIL_BYTES = _EOCD_SIZE + _EOCD_MAX_COMMENT + _ZIP64_LOCATOR_SIZE + _ZIP64_EOCD_SIZE
_BOMB_RATIO = 200
# A member has to be at least this large before its ratio counts: a tiny,
# highly repetitive text file (a CSV of one repeated row) compresses far
# past 200:1 and could never inflate into trouble.
_BOMB_MIN_BYTES = 1024 * 1024
_ARCHIVE_EXTENSIONS = (".zip", ".zipx", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".jar")
_ARCHIVE_MAGICS = (_MAGIC, b"PK\x05\x06", b"PK\x07\x08", b"7z\xbc\xaf", b"Rar!", b"\x1f\x8b")
# Office documents are zip containers (OOXML and OpenDocument both start
# with the PK magic), and the sandbox ingests Word and Excel, so a member
# with one of these extensions is a document, never a nested archive. Seen
# live 2026-09-16: a GC's "Q A.docx" inside a Dropbox folder zip.
_DOCUMENT_CONTAINER_EXTENSIONS = (
    ".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".ppsx", ".odt", ".ods", ".odp", ".vsdx",
)
_DROPPED_BASENAMES = {"thumbs.db", "desktop.ini"}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ZipError(Exception):
    """A member that cannot be inflated: bad index, a directory, encrypted,
    corrupt data. The message is app-authored and safe to store."""


class ZipTooLarge(ZipError):
    """The member inflated past the caller's byte cap; the partial file was
    removed."""


@dataclass
class ZipMember:
    index: int                    # zipfile infolist() position
    path: str                     # sanitized relative path inside the zip
    size: int                     # declared uncompressed size
    skipped_reason: str | None    # None = downloadable


@dataclass
class ZipListing:
    members: list[ZipMember]      # every non-directory entry kept, in order, skipped ones included
    error: str | None             # a sentence when the zip cannot be used at all; members empty then
    truncated: bool               # the member cap dropped entries
    error_kind: str | None = None  # "not_zip" | "too_large" | "bomb" when `error` is set


def is_zip(path: Path) -> bool:
    """True when the file starts with the local-file-header magic."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == _MAGIC
    except OSError:
        return False


def _safe_path(name: str) -> str | None:
    """`name` as a "/"-separated relative path, or None when no safe path is
    left (absolute, traversal, empty)."""
    raw = name.replace("\\", "/")
    raw = _DRIVE_RE.sub("", raw)
    parts = []
    for seg in raw.split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    if not parts:
        return None
    return "/".join(parts)


def _dropped(parts: list[str]) -> bool:
    """Entries the listing never mentions: resource forks and OS litter."""
    if parts[0] == "__MACOSX":
        return True
    base = parts[-1]
    return base.startswith(".") or base.lower() in _DROPPED_BASENAMES


def _archive_by_extension(basename: str) -> bool:
    lower = basename.lower()
    return lower.endswith(_ARCHIVE_EXTENSIONS)


def _archive_by_magic(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    """Peek at the member's first bytes; any trouble reads as "not an
    archive" (the extraction step reports real corruption). A member named
    like an office document is exempt: it is a zip container by design."""
    if info.filename.lower().endswith(_DOCUMENT_CONTAINER_EXTENSIONS):
        return False
    try:
        with zf.open(info) as member:
            head = member.read(4)
    except (zipfile.BadZipFile, RuntimeError, zlib.error, EOFError, OSError, ValueError):
        return False
    return any(head.startswith(m) for m in _ARCHIVE_MAGICS)


def _tail(source: str | Path | IO[bytes], n: int) -> bytes | None:
    """The last `n` bytes of the archive (fewer when it is shorter); a
    file object is left at the position it was in. None on any I/O
    trouble."""
    try:
        if isinstance(source, (str, Path)):
            with open(source, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - n))
                return fh.read(n)
        pos = source.tell()
        try:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - n))
            return source.read(n)
        finally:
            source.seek(pos)
    except (OSError, ValueError, AttributeError):
        return None


def central_directory_bounds(source: str | Path | IO[bytes]) -> tuple[int, int] | None:
    """(entry count, central directory bytes) the archive's end record
    declares, without parsing the directory. A ZIP64 locator in front of
    the end record is honoured the way `zipfile` honours it (its record
    sits right before the locator) and the larger of the two declarations
    wins; a locator whose record is missing or malformed counts as the
    ZIP64 ceiling, never as the small number beside it. None when there is
    no end record at all (zipfile refuses such a file on its own)."""
    tail = _tail(source, _TAIL_BYTES)
    if tail is None:
        return None
    at = tail.rfind(_EOCD_SIG)
    if at < 0 or len(tail) - at < _EOCD_SIZE:
        return None
    entries = struct.unpack("<H", tail[at + 10:at + 12])[0]
    cd_size = struct.unpack("<I", tail[at + 12:at + 16])[0]
    loc_at = at - _ZIP64_LOCATOR_SIZE
    if loc_at >= 0 and tail[loc_at:loc_at + 4] == _ZIP64_LOCATOR_SIG:
        rec_at = loc_at - _ZIP64_EOCD_SIZE
        rec = tail[rec_at:loc_at] if rec_at >= 0 else b""
        if len(rec) == _ZIP64_EOCD_SIZE and rec.startswith(_ZIP64_EOCD_SIG):
            entries64 = struct.unpack("<Q", rec[32:40])[0]
            cd_size64 = struct.unpack("<Q", rec[40:48])[0]
        else:
            entries64, cd_size64 = 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF
        entries = max(entries, entries64)
        cd_size = max(cd_size, cd_size64)
    return entries, cd_size


def precheck(
    source: str | Path | IO[bytes],
    *,
    max_entries: int = MAX_ENTRIES,
    max_central_directory_bytes: int = MAX_CENTRAL_DIRECTORY_BYTES,
) -> str | None:
    """Refuse an archive before `zipfile` parses it: "too_many_entries" when
    the end record declares more entries than `max_entries`,
    "central_directory_too_large" when it declares a directory over
    `max_central_directory_bytes`, else None (zipfile may still refuse it)."""
    bounds = central_directory_bounds(source)
    if bounds is None:
        return None
    entries, cd_size = bounds
    if entries > max_entries:
        return "too_many_entries"
    if cd_size > max_central_directory_bytes:
        return "central_directory_too_large"
    return None


def read_member_bounded(zf: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int) -> bytes | None:
    """The member's bytes, inflated in MEMBER_READ_CHUNK pieces under a
    running counter: None once more than `max_bytes` have come out,
    whatever the entry declares. Corruption and a bad CRC raise as zipfile
    raises them (BadZipFile, zlib.error, EOFError, RuntimeError)."""
    if int(info.compress_size or 0) > max_bytes or int(info.file_size or 0) > max_bytes:
        return None
    parts: list[bytes] = []
    total = 0
    with zf.open(info) as member:
        while True:
            chunk = member.read(MEMBER_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None
            parts.append(chunk)
    return b"".join(parts)


# ── OOXML parts, read as XML ─────────────────────────────────────────────────

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
# A relationship target that is a URL, a scheme-prefixed path or a UNC
# path: a lenient importer may fetch it whatever TargetMode says.
_EXTERNAL_TARGET_RE = re.compile(r"^\s*(?:[a-z][a-z0-9+.\-]*:|//|\\\\)", re.IGNORECASE)
_XML_SPACE_RE = re.compile(r"\s+")


@dataclass
class ParsedXml:
    """An OOXML part as expat read it: every element in document order as
    (local name lowercased, {attribute local name lowercased: value}),
    and every character-data node joined in document order."""

    elements: list[tuple[str, dict[str, str]]]
    text: str


def _local(name: str) -> str:
    """The local part of a tag or attribute name (after any prefix),
    lowercased."""
    return name.rsplit(":", 1)[-1].lower()


def parse_xml_part(raw: bytes) -> ParsedXml | None:
    """The OOXML part `raw` parsed by expat (any encoding the BOM or the
    declaration names; entities decoded; prefixes tolerated whether bound
    or not, since a reader's tolerance cannot be assumed), or None when it
    is not well-formed or declares a DTD, which no real package part
    does."""
    if _DOCTYPE_RE.search(raw):
        return None
    elements: list[tuple[str, dict[str, str]]] = []
    texts: list[str] = []
    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = lambda name, attrs: elements.append(
        (_local(name), {_local(key): value for key, value in attrs.items()})
    )
    parser.CharacterDataHandler = texts.append
    try:
        parser.Parse(raw, True)
    except (expat.ExpatError, ValueError, TypeError):
        return None
    return ParsedXml(elements, "".join(texts))


def relationships(raw: bytes) -> list[tuple[str, bool]] | None:
    """Every `Relationship` element of the OPC relationships part `raw`,
    whatever its namespace or prefix, as (type lowercased, external):
    external when TargetMode reads External once the XML is decoded, or
    when the Target is a URL, a scheme-prefixed path or a UNC path. None
    when the part cannot be parsed (the caller fails closed)."""
    parsed = parse_xml_part(raw)
    if parsed is None:
        return None
    out: list[tuple[str, bool]] = []
    for name, attrs in parsed.elements:
        if name != "relationship":
            continue
        mode = attrs.get("targetmode", "").strip().lower()
        external = mode == "external" or _EXTERNAL_TARGET_RE.match(attrs.get("target", "")) is not None
        out.append((attrs.get("type", "").strip().lower(), external))
    return out


def flattened_xml(raw: bytes) -> str | None:
    """Everything the part `raw` says, for a substring check, lowercased
    with white space removed: every element and attribute name and every
    attribute value, each set off by `|`, then every character-data node
    joined with nothing between, so a field instruction split across runs
    (or entity-encoded, or in UTF-16) reads whole. None when the part
    cannot be parsed."""
    parsed = parse_xml_part(raw)
    if parsed is None:
        return None
    pieces: list[str] = []
    for name, attrs in parsed.elements:
        pieces.append(name)
        for key, value in attrs.items():
            pieces.append(key)
            pieces.append(value)
    pieces.append(parsed.text)
    return _XML_SPACE_RE.sub("", "|".join(pieces)).lower()


def inspect(
    path: Path,
    *,
    max_members: int,
    max_total_bytes: int,
    per_member_max_bytes: int,
    is_image_name: Callable[[str], bool],
) -> ZipListing:
    """List the archive's members under the caps (module docstring).

    `is_image_name` is the caller's image policy over a member's basename;
    members it accepts are listed with `skipped_reason = "image"`. Downloadable
    members count against `max_members`; skipped ones are kept up to the same
    number so a folder of photos can never crowd out the drawings. Either
    overflow sets `truncated` and drops the rest.
    """
    path = Path(path)
    if precheck(path) is not None:
        return ZipListing(
            [], "The zip has more entries than the harvest accepts.", False, "too_many_entries"
        )
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
        return ZipListing([], "The file is not a zip archive that can be opened.", False, "not_zip")
    with zf:
        try:
            infos = zf.infolist()
        except (zipfile.BadZipFile, ValueError, EOFError, OSError):
            return ZipListing([], "The zip is truncated or damaged.", False, "not_zip")
        members: list[ZipMember] = []
        kept = 0
        skipped = 0
        total = 0
        truncated = False
        for index, info in enumerate(infos):
            if info.is_dir() or info.filename.endswith("/"):
                continue
            safe = _safe_path(info.filename)
            if safe is not None and _dropped(safe.split("/")):
                continue
            size = int(info.file_size or 0)
            reason: str | None = None
            if safe is None:
                reason = "unsafe_name"
                safe = _sanitize_flat(info.filename)
            elif info.flag_bits & 0x1:
                reason = "encrypted"
            elif _archive_by_extension(safe.rsplit("/", 1)[-1]):
                reason = "nested_zip"
            elif size == 0:
                reason = "empty"
            elif size > per_member_max_bytes:
                reason = "too_large"
            elif is_image_name(safe.rsplit("/", 1)[-1]):
                reason = "image"
            elif kept < max_members and _archive_by_magic(zf, info):
                # The magic peek opens the member; past the kept cap the
                # entry is dropped anyway, so it is never opened.
                reason = "nested_zip"
            if reason is None:
                compressed = int(info.compress_size or 0)
                if size >= _BOMB_MIN_BYTES and (compressed == 0 or size / compressed > _BOMB_RATIO):
                    return ZipListing([], "The zip looks like a decompression bomb.", False, "bomb")
                total += size
                if total > max_total_bytes:
                    return ZipListing(
                        [],
                        "The zip declares more bytes than the harvest accepts "
                        f"({total // (1024 * 1024)} MB).",
                        False,
                        "too_large",
                    )
                if kept >= max_members:
                    truncated = True
                    continue
                kept += 1
            else:
                if skipped >= max_members:
                    truncated = True
                    continue
                skipped += 1
            members.append(ZipMember(index=index, path=safe, size=size, skipped_reason=reason))
        return ZipListing(members, None, truncated)


def _sanitize_flat(name: str) -> str:
    """A displayable name for an entry whose path was unsafe: the basename,
    control characters and separators replaced."""
    base = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f]", "_", base).strip() or "member"
    return base[:150]


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


def extract_member(path: Path, index: int, dest: Path, *, max_bytes: int) -> int:
    """Inflate member `index` of the zip at `path` into `dest` (created
    O_EXCL, removed on any failure) in 1 MB chunks under a running cap on the
    bytes inflated. Returns the bytes written. Raises ZipTooLarge past the
    cap and ZipError for a member that cannot be opened (bad index, a
    directory, encrypted, corrupt); an existing `dest` raises
    FileExistsError before the zip is touched."""
    path = Path(path)
    dest = Path(dest)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    out = _open_excl(dest)
    written = 0
    try:
        with out:
            try:
                if precheck(path) is not None:
                    raise ZipError("The zip has more entries than the harvest accepts.")
                with zipfile.ZipFile(path) as zf:
                    infos = zf.infolist()
                    if not isinstance(index, int) or index < 0 or index >= len(infos):
                        raise ZipError("The zip has no member at that position.")
                    info = infos[index]
                    if info.is_dir() or info.filename.endswith("/"):
                        raise ZipError("The zip member is a directory.")
                    if info.flag_bits & 0x1:
                        raise ZipError("The zip member is encrypted.")
                    with zf.open(info) as member:
                        while True:
                            chunk = member.read(_CHUNK)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > max_bytes:
                                raise ZipTooLarge(
                                    "The zip member is larger than the per-file limit."
                                )
                            out.write(chunk)
            except ZipError:
                raise
            except (
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
                RuntimeError,
                zlib.error,
                EOFError,
                ValueError,
                OSError,
            ) as exc:
                raise ZipError(f"The zip member could not be inflated ({type(exc).__name__}).") from exc
    except BaseException:
        _unlink_quietly(dest)
        raise
    return written
