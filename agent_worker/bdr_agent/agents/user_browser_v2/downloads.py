"""Downloads for user_browser v2 (plan 11.1; WI-10). Signed worker code.

Every file a page downloads is untrusted. The pipeline, per file:

  1. Playwright saves it into the job's quarantine folder
     (C:\\ProgramData\\BDRQuarantine\\<job id>, outside every worker home; WI-8
     sets `downloads_path`). Nothing here opens, executes or previews it.
  2. Refused at once, before any byte is kept, when the boot self-test did not
     prove Microsoft Defender (`downloads_allowed`), or the task already has
     its file count.
  3. Size: while the file arrives, a monitor cancels it once it passes the per
     file cap (job policy, never above the signed floor of 450 MB) or the
     task total; the finished size is checked again.
  4. Type by magic bytes, never by extension: PDF, PNG, JPEG, TIFF, GIF, ZIP
     (not extracted), Office Open XML (no macros), legacy Office (OLE), CSV
     and plain text (UTF-8, by .csv/.txt/.tsv name only), DWG, DXF, RTF.
     Everything else (executables, scripts, disk images, shortcuts, HTML) is
     deleted.
  5. Scan: `MpCmdRun.exe -Scan -ScanType 3 -File <path> -DisableRemediation`.
     Exit code 0 is clean; anything else, a timeout or a missing scanner
     deletes the file. Fail closed.
  6. Upload through BDR (multipart, USER_V2_ENDPOINTS["user_downloads"], with
     the sha256), never straight to storage.
  7. The local file is deleted in every case.

The model hears about each file through a notice on its next observation;
the runner adds the list to the finish step.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import subprocess
import struct
import unicodedata
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...client import (
    BdrApiError,
    BdrUnreachable,
    LeaseLost,
    StaleBoot,
    WorkerRevoked,
    WorkerUnauthorized,
)
from ...protocol import (
    SAFE_FILENAME_RE,
    USER_DOWNLOAD_FILE_BYTES_FLOOR,
    USER_DOWNLOAD_FILES_FLOOR,
    USER_DOWNLOAD_TASK_BYTES_FLOOR,
    UserDownloadMeta,
    UserPolicyCaps,
    url_origin,
)
from .exfil import trace_url
from .selftest import find_mpcmdrun

log = logging.getLogger("bdr_agent.user_browser_v2.downloads")

SCAN_TIMEOUT_SECONDS = 300.0
MONITOR_INTERVAL_SECONDS = 0.5
SNIFF_BYTES = 8192
TEXT_CHECK_BYTES = 1024 * 1024
FILENAME_MAX = 200
_FATAL = (LeaseLost, StaleBoot, WorkerRevoked, WorkerUnauthorized)

# kind -> the extensions a saved name may keep (the first is used when the
# page's name says something else).
KIND_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",), "png": (".png",), "jpeg": (".jpg", ".jpeg"), "tiff": (".tif", ".tiff"),
    "gif": (".gif",), "zip": (".zip",), "ooxml": (".docx", ".xlsx", ".pptx"),
    "ole": (".doc", ".xls", ".ppt", ".msg"), "csv": (".csv", ".tsv"), "text": (".txt",),
    "dwg": (".dwg",), "dxf": (".dxf",), "rtf": (".rtf",),
}
_OOXML_PARTS = {"word/": ".docx", "xl/": ".xlsx", "ppt/": ".pptx"}
_DWG_VERSIONS = re.compile(rb"^AC10(?:0[2-9]|1[0-9]|2[0-9]|3[0-9])")
_DXF_START = re.compile(rb"^\s*0\s*\r?\n\s*(?:SECTION|section)\b")
_TEXT_EXTENSIONS = {".csv": "csv", ".tsv": "csv", ".txt": "text"}
_BAD_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


# Type allowlist ------------------------------------------------------------------------


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def sniff_kind(path: Path | str, filename: str = "") -> tuple[str | None, str]:
    """(kind, detail) by magic bytes, or (None, reason) when the file is not
    on the allowlist. Never trusts the extension, except that plain text is
    accepted only under a .csv, .tsv or .txt name (a script is text too)."""
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return None, "unreadable"
    if not head:
        return None, "empty"
    if head.startswith(b"%PDF-"):
        return "pdf", "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg", "jpeg"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff", "tiff"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "gif"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _ole_kind(p)
    if head.startswith(b"{\\rtf"):
        return "rtf", "rtf"
    if _DWG_VERSIONS.match(head):
        return "dwg", "dwg"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _zip_kind(p)
    if head.startswith((b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe")):
        return None, "executable"
    if _DXF_START.match(head) and _ext(filename) == ".dxf":
        return ("dxf", "dxf") if _is_utf8_text(p) else (None, "not_text")
    kind = _TEXT_EXTENSIONS.get(_ext(filename))
    if kind is None:
        return None, "type_not_allowed"
    if not _is_utf8_text(p):
        return None, "not_text"
    return kind, kind


# Legacy Office (Compound File Binary) directory entries that hold a VBA
# project: Excel and PowerPoint "_VBA_PROJECT_CUR" / "VBA", Word "Macros".
_OLE_MACRO_NAMES = frozenset({"_vba_project_cur", "_vba_project", "vba", "macros"})
_CFB_FREE, _CFB_END = 0xFFFFFFFF, 0xFFFFFFFE
_CFB_MAX_SECTORS = 1 << 20


def ole_entry_names(p: Path) -> list[str] | None:
    """Every directory entry name of a Compound File Binary file (legacy
    .doc, .xls, .ppt, .msg), or None when the structure cannot be read.
    Reads the header, the FAT and the directory chain only."""
    try:
        with open(p, "rb") as fh:
            header = fh.read(512)
            if len(header) < 512 or header[28:30] != b"\xfe\xff":
                return None
            shift = struct.unpack_from("<H", header, 30)[0]
            if shift not in (9, 12):
                return None
            size = 1 << shift
            n_fat, first_dir = struct.unpack_from("<II", header, 44)
            first_difat, n_difat = struct.unpack_from("<II", header, 68)
            if n_fat > _CFB_MAX_SECTORS or n_difat > _CFB_MAX_SECTORS:
                return None

            def sector(n: int) -> bytes:
                fh.seek((n + 1) * size)
                data = fh.read(size)
                if len(data) != size:
                    raise ValueError("short sector")
                return data

            fat_sectors = [v for v in struct.unpack_from("<109I", header, 76) if v not in (_CFB_FREE, _CFB_END)]
            nxt, seen = first_difat, 0
            while nxt not in (_CFB_FREE, _CFB_END) and seen < n_difat:
                values = struct.unpack(f"<{size // 4}I", sector(nxt))
                fat_sectors += [v for v in values[:-1] if v not in (_CFB_FREE, _CFB_END)]
                nxt, seen = values[-1], seen + 1
            fat: list[int] = []
            for n in fat_sectors[:n_fat]:
                fat += struct.unpack(f"<{size // 4}I", sector(n))
            names: list[str] = []
            cur, steps = first_dir, 0
            while cur not in (_CFB_FREE, _CFB_END):
                steps += 1
                if steps > _CFB_MAX_SECTORS or cur >= len(fat):
                    return None
                data = sector(cur)
                for off in range(0, size, 128):
                    entry = data[off:off + 128]
                    length = struct.unpack_from("<H", entry, 64)[0]
                    if entry[66] == 0 or not 2 <= length <= 64:
                        continue          # unused entry
                    names.append(entry[: length - 2].decode("utf-16-le", errors="replace"))
                cur = fat[cur]
            return names
    except (OSError, ValueError, struct.error, IndexError):
        return None


def _ole_kind(p: Path) -> tuple[str | None, str]:
    """Legacy Office is allowed, but not with a VBA project inside (the
    classic macro carrier keeps its .doc or .xls name)."""
    names = ole_entry_names(p)
    if names is None:
        return None, "bad_ole"
    if any(n.lower() in _OLE_MACRO_NAMES for n in names):
        return None, "office_macros"
    return "ole", "ole"


def _zip_kind(p: Path) -> tuple[str | None, str]:
    """ZIP stays a ZIP (never extracted); Office Open XML is recognised from
    the central directory names only. A macro-enabled Office file is refused:
    a part named vbaProject.bin, or a content type that declares one (a
    macro project can be renamed through its relationships)."""
    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            types = b""
            for info in zf.infolist():
                if info.filename.lower() == "[content_types].xml" and info.file_size <= 4 * 1024 * 1024:
                    types = zf.read(info)
                    break
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError):
        return None, "bad_zip"
    lowered = [n.lower() for n in names]
    if "[content_types].xml" in lowered:
        declared = types.lower()
        if (any(n.endswith("vbaproject.bin") for n in lowered) or b"macroenabled" in declared
                or b"vbaproject" in declared):
            return None, "office_macros"
        for prefix, ext in _OOXML_PARTS.items():
            if any(n.startswith(prefix) for n in lowered):
                return "ooxml", ext
    return "zip", "zip"


def _is_utf8_text(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            data = fh.read(TEXT_CHECK_BYTES)
    except OSError:
        return False
    if b"\x00" in data:
        return False
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut by the read limit is fine.
        if exc.start < len(data) - 4:
            return False
    return True


def safe_filename(name: str | None, kind: str, detail: str = "") -> str:
    """The page's suggested name made safe for SAFE_FILENAME_RE, with an
    extension that matches the sniffed kind (a PDF named "x.exe" becomes
    "x.exe.pdf"; the server re-sniffs anyway)."""
    text = unicodedata.normalize("NFKC", name or "")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _BAD_NAME_CHARS.sub("_", text).strip(" .")
    if not text:
        text = "download"
    allowed = KIND_EXTENSIONS.get(kind, ())
    want = detail if detail.startswith(".") else (allowed[0] if allowed else "")
    if kind == "ooxml":
        allowed = (want,) if want else allowed
    if _ext(text) not in allowed and want:
        text = text + want
    if len(text) > FILENAME_MAX:
        stem, ext = os.path.splitext(text)
        text = stem[: FILENAME_MAX - len(ext)] + ext
    if not SAFE_FILENAME_RE.fullmatch(text):
        text = "download" + (want or "")
    return text


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Scanner -------------------------------------------------------------------------------


@dataclass
class ScanResult:
    verdict: str            # clean, threat, error, unavailable
    detail: str = ""


class DefenderScanner:
    """Microsoft Defender's command line scan of one file. Fail closed: no
    scanner, a timeout or any exit code but 0 is not clean."""

    def __init__(self, mpcmdrun: Callable[[], Path | None] = find_mpcmdrun, *,
                 timeout: float = SCAN_TIMEOUT_SECONDS,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._find = mpcmdrun
        self.timeout = timeout
        self._run = run

    async def scan(self, path: Path) -> ScanResult:
        exe = self._find()
        if exe is None:
            return ScanResult("unavailable", "Defender command line scanner not found")
        cmd = [str(exe), "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"]
        try:
            proc = await asyncio.to_thread(self._run, cmd, capture_output=True, timeout=self.timeout,
                                           check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return ScanResult("error", "scan timed out")
        except OSError as exc:
            return ScanResult("unavailable", type(exc).__name__)
        if proc.returncode == 0:
            return ScanResult("clean")
        if proc.returncode == 2:
            return ScanResult("threat", "threat found")
        return ScanResult("error", f"exit code {proc.returncode}")


# Manager -------------------------------------------------------------------------------


@dataclass
class DownloadRecord:
    filename: str
    origin: str | None
    status: str = "pending"          # saved_to_project, staged, rejected
    reason: str | None = None
    size_bytes: int = 0
    kind: str | None = None
    download_id: str | None = None
    file_id: str | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"filename": self.filename, "status": self.status}
        for key in ("reason", "kind", "download_id", "file_id"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.size_bytes:
            out["size_bytes"] = self.size_bytes
        return out


@dataclass
class _Caps:
    file_bytes: int
    task_bytes: int
    files: int


@dataclass
class DownloadManager:
    """One job's downloads. `attach(context)` once the guard is installed;
    `drain()` before finishing; `take_notices()` after every tool."""
    quarantine_dir: Path
    upload: Callable[[UserDownloadMeta, Path], Awaitable[Any]]
    caps: Callable[[], UserPolicyCaps]
    allowed: Callable[[], bool]
    scanner: Any = field(default_factory=DefenderScanner)
    log_step: Callable[..., Any] | None = None
    redact: Callable[[str], str] | None = None
    monitor_interval: float = MONITOR_INTERVAL_SECONDS
    records: list[DownloadRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.quarantine_dir = Path(self.quarantine_dir)
        self._notices: list[str] = []
        self._tasks: set[asyncio.Task] = set()
        self._started = 0
        # Bytes reserved by downloads that passed the task cap check (reserved
        # with no await in between, so parallel downloads cannot all pass it),
        # and the quarantine files those reservations already count.
        self._bytes = 0
        self._reserved_paths: set[str] = set()
        self._fatal: BaseException | None = None

    # Wiring --------------------------------------------------------------------------

    def attach(self, context: Any) -> None:
        context.on("page", self._adopt)
        for page in list(getattr(context, "pages", []) or []):
            self._adopt(page)

    def _adopt(self, page: Any) -> None:
        if getattr(page, "_bdr_downloads_v2", False):
            return
        with contextlib.suppress(Exception):
            page._bdr_downloads_v2 = True
        page.on("download", self.on_download)

    def on_download(self, download: Any) -> None:
        task = asyncio.ensure_future(self.handle(download))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout: float | None = None) -> bool:
        """Wait for downloads in progress (scan and upload included)."""
        if not self._tasks:
            return True
        _done, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._fatal is not None:
            fatal, self._fatal = self._fatal, None
            raise fatal
        return not pending

    def take_notices(self) -> list[str]:
        out, self._notices = self._notices, []
        return out

    def summary(self) -> list[dict[str, Any]]:
        return [r.public() for r in self.records]

    # One download ------------------------------------------------------------------

    def _caps(self) -> _Caps:
        c = self.caps()
        return _Caps(file_bytes=min(c.download_file_bytes, USER_DOWNLOAD_FILE_BYTES_FLOOR),
                     task_bytes=min(c.download_task_bytes, USER_DOWNLOAD_TASK_BYTES_FLOOR),
                     files=min(c.download_files, USER_DOWNLOAD_FILES_FLOOR))

    def _origin(self, download: Any) -> str | None:
        origin = url_origin(getattr(download, "url", "") or "")
        if origin is None:   # blob: and data: downloads come from the page that made them
            page = getattr(download, "page", None)
            origin = url_origin(getattr(page, "url", "") or "") if page is not None else None
        return origin

    async def handle(self, download: Any) -> DownloadRecord:
        raw_name = ""
        with contextlib.suppress(Exception):
            raw_name = str(download.suggested_filename or "")
        record = DownloadRecord(filename=safe_filename(raw_name, "", "") if raw_name else "download",
                                origin=self._origin(download))
        self.records.append(record)
        path: Path | None = None
        reserved = 0
        committed = False
        try:
            caps = self._caps()
            self._started += 1
            if not self.allowed():
                await _cancel(download)
                return self._reject(record, "downloads_unavailable")
            if self._started > caps.files:
                await _cancel(download)
                return self._reject(record, "file_count_cap")
            if record.origin is None:
                await _cancel(download)
                return self._reject(record, "bad_source")
            path, failure = await self._wait_complete(download, caps)
            if failure is not None:
                return self._reject(record, failure)
            assert path is not None
            size = path.stat().st_size
            record.size_bytes = size
            if size <= 0:
                return self._reject(record, "empty")
            if size > caps.file_bytes:
                return self._reject(record, "file_too_large")
            if self._bytes + size > caps.task_bytes:
                return self._reject(record, "task_bytes_cap")
            self._bytes += size                 # reserved now; released below unless uploaded
            reserved = size
            self._reserved_paths.add(_real(path))
            kind, detail = await asyncio.to_thread(sniff_kind, path, raw_name)
            if kind is None:
                return self._reject(record, detail if detail in ("executable", "office_macros") else "type_not_allowed")
            record.kind = kind
            record.filename = safe_filename(raw_name, kind, detail)
            scan = await self.scanner.scan(path)
            if scan.verdict != "clean":
                return self._reject(record, "scan_threat" if scan.verdict == "threat" else "scan_unavailable")
            digest = await asyncio.to_thread(sha256_file, path)
            meta = UserDownloadMeta(sha256=digest, filename=record.filename, size_bytes=size, sniffed_kind=kind,
                                    source_origin=record.origin, scan="clean")
            try:
                resp = await self.upload(meta, path)
            except _FATAL as exc:
                self._fatal = exc
                return self._reject(record, "upload_failed")
            except (BdrApiError, BdrUnreachable) as exc:
                log.warning("download upload failed: %s", type(exc).__name__)
                return self._reject(record, "upload_failed")
            committed = True                    # the bytes went to BDR: they count
            record.status = getattr(resp, "status", "rejected")
            record.reason = getattr(resp, "reason", None)
            record.download_id = str(resp.download_id) if getattr(resp, "download_id", None) else None
            record.file_id = str(resp.file_id) if getattr(resp, "file_id", None) else None
            self._log(record)
            self._notice_for(record)
            return record
        except asyncio.CancelledError:
            record.status, record.reason = "rejected", "canceled"
            raise
        except Exception as exc:   # never let a page's download break the job
            log.warning("download handling failed: %s", type(exc).__name__)
            return self._reject(record, "download_failed")
        finally:
            if reserved and not committed:
                self._bytes -= reserved
            await self._delete(download, path)
            if path is not None:
                self._reserved_paths.discard(_real(path))

    async def _wait_complete(self, download: Any, caps: _Caps) -> tuple[Path | None, str | None]:
        """Wait for the file while a monitor cancels it past the size caps."""
        exceeded: list[str] = []

        async def monitor() -> None:
            while True:
                await asyncio.sleep(self.monitor_interval)
                sizes = self._partial_sizes()
                if sizes and max(sizes) > caps.file_bytes:
                    exceeded.append("file_too_large")
                elif self._bytes + sum(sizes) > caps.task_bytes:
                    # Every download still being written counts, not only
                    # the largest one (parallel downloads share the cap).
                    exceeded.append("task_bytes_cap")
                if exceeded:
                    await _cancel(download)
                    return

        watcher = asyncio.ensure_future(monitor())
        try:
            try:
                raw = await download.path()
            except Exception:
                raw = None
            failure = None
            with contextlib.suppress(Exception):
                failure = await download.failure()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        if exceeded:
            return None, exceeded[0]
        if failure or raw is None:
            return None, "download_failed"
        path = Path(raw)
        if not _inside(path, self.quarantine_dir):
            return None, "outside_quarantine"
        return path, None

    def _partial_sizes(self) -> list[int]:
        """Sizes of the quarantine files not yet counted in _bytes (downloads
        still being written, or finished but not yet checked)."""
        sizes: list[int] = []
        with contextlib.suppress(OSError):
            for entry in os.scandir(self.quarantine_dir):
                with contextlib.suppress(OSError):
                    if entry.is_file(follow_symlinks=False) and _real(entry.path) not in self._reserved_paths:
                        sizes.append(entry.stat(follow_symlinks=False).st_size)
        return sizes

    async def _delete(self, download: Any, path: Path | None) -> None:
        with contextlib.suppress(Exception):
            await download.delete()
        if path is not None:
            with contextlib.suppress(OSError):
                if _inside(path, self.quarantine_dir):
                    path.unlink(missing_ok=True)

    def _reject(self, record: DownloadRecord, reason: str) -> DownloadRecord:
        record.status, record.reason = "rejected", reason
        self._log(record)
        self._notice_for(record)
        return record

    def _log(self, record: DownloadRecord) -> None:
        if self.log_step is None:
            return
        try:
            self.log_step("browser.download", f"Download {record.status}: {record.filename}",
                          ok=record.status != "rejected",
                          result={**record.public(), "origin": trace_url(record.origin or "")})
        except Exception as exc:
            log.debug("download step not logged: %s", exc)

    def _notice_for(self, record: DownloadRecord) -> None:
        name = record.filename
        if record.status == "saved_to_project":
            text = f"Downloaded \"{name}\": saved to the task's BDR project."
        elif record.status == "staged":
            text = f"Downloaded \"{name}\": kept for the owner to add to a project."
        else:
            text = f"Download \"{name}\" was not kept ({record.reason or 'refused'})."
        if self.redact is not None:
            text = self.redact(text)
        self._notices.append(text)


def _real(path: Path | str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return str(path)


def _inside(path: Path, root: Path) -> bool:
    try:
        p = Path(os.path.realpath(path))
        r = Path(os.path.realpath(root))
    except OSError:
        return False
    return p == r or r in p.parents


async def _cancel(download: Any) -> None:
    with contextlib.suppress(Exception):
        await download.cancel()


__all__ = [
    "KIND_EXTENSIONS",
    "DefenderScanner",
    "DownloadManager",
    "DownloadRecord",
    "ScanResult",
    "safe_filename",
    "sha256_file",
    "sniff_kind",
]
