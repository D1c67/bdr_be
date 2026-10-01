"""The sandbox child's entry point. Runs in a fresh, isolated interpreter
(`python -I -X utf8 -c protocol.BOOTSTRAP <repo_root> ...`) with a scrubbed
environment, the out dir as cwd, and nothing from the API process but the
path to one PDF and a limits document. See docs/RFP_INGESTION_SANDBOX.md
sections 3.1 to 3.3 and the protocol.py module docstring, which is the
authority on the launch command and on how the parent attributes a crash.

Two modes:

    --spawn N --input PDF --out DIR --limits JSON [--skip-file PATH]
    --verify --out DIR --limits JSON --list-file PATH

`--limits` is the path of the limits document written by the parent, or the
document itself when the argument begins with `{`. Either way it must pass
`protocol.validate_limits`. Any argument or limits problem, a progress file
that already exists, an out dir that is not a directory, an --input that is
not a file (stat only; the parent wrote it, so its absence is a parent bug),
or a PDFium build with V8 (JavaScript) compiled in, exits `EXIT_BAD_ARGS`
with one app-authored line on stderr; the parent records failed/spawn and
never blames the file.

Process mode, in this exact order:

1. Parse and validate arguments. Read the skip file (0-based page indices,
   one per line).
2. Open `progress_file(spawn)` in <out> with O_CREAT|O_EXCL|O_APPEND. Every
   later event is ONE os.write of the whole JSON line (compact, allow_nan
   False) plus newline, so a kill tears at most the final line.
3. Apply the rlimits (`limits.apply`) and write `start`.
4. Import pypdfium2 and Pillow (through render.py / hazards.py) and write
   `ready` with the versions, the PDFium build flags and the platform.
5. Open the PDF through the raw PDFium API so a zero-page file can be told
   from an unreadable one: a NULL handle with FPDF_ERR_PASSWORD is
   `reject encrypted`, any other NULL handle is `reject unreadable`, a page
   count of 0 is `reject no_pages`, over `max_pages` is `reject
   too_many_pages`; every one of those events is written BEFORE the document
   handle is closed. A file whose only encryption is owner restrictions opens
   without a password and is recorded (`owner_restricted`,
   `security_handler_revision`) but allowed. Heartbeats bracket the open and
   inventory calls; the child is single-threaded (max_processes is 1 and
   a thread would count against it on Linux), so the parent's open timeout
   is what really bounds a pathological open.
6. Write `document` (page_count, pdf_version as "1.7"-style text or null,
   owner_restricted, security_handler_revision, form_type, sanitized and
   capped metadata for protocol.METADATA_KEYS, document hazards).
7. For each index in order, skipping the skip list: stop with `end.aborted =
   "deadline"` once `deadline_seconds` of wall clock have passed, or
   `"disk_quota"` once this spawn's artifact bytes exceed
   `max_output_total_bytes` (the parent mirrors the quota over the whole
   out dir, which also covers earlier spawns). Otherwise write `page_start`
   BEFORE touching PDFium for that page, then the `page` event from
   render.process_page (ok or failed; a failed page leaves no artifacts).
8. Write `end` (pages_ok, pages_failed, elapsed_ms, peak_rss_kb,
   output_bytes, aborted), THEN close the document, then exit 0. Nothing in
   `end` depends on the close, and a death inside PDFium's close would leave
   a log with no terminal event, which costs the parent every page this spawn
   rendered. `output_bytes` counts artifact bytes written by THIS spawn
   (thumb + full + text), not the progress log.

Verify mode re-decodes the JPEGs named in the list file (relative names of
exactly the `thumb/NNNN.jpg` / `full/NNNN.jpg` shape that `protocol.page_file`
spells, zero-padded to protocol.PAGE_NAME_WIDTH and wider for indices that
need more digits; anything else, a non-canonical padding included, is refused
as ok=false) in a fresh process under the same rlimits, each opened
relative to <out> with O_NOFOLLOW on both the subdirectory and the file and
required to be a regular file, fully decoded with Pillow under a pixel bound
of full_long_side squared plus a margin, and reports one `{"event":
"verified", "file", "w", "h", "ok"}` line per name to VERIFY_FILE (O_EXCL).
A failure of any kind for one file is ok=false, never an exception; the
reason goes to stderr as an app-authored line. Exit 0.

The child opens nothing but --input, --out (and its subdirectories), the
limits/skip/list files the parent named, and its own module files. stderr is
whatever descriptor the parent supplied (a file, never a pipe). Nothing here
imports app.core, app.services, or any third-party package other than
pypdfium2 and Pillow; tests/test_rfp_sandbox_child.py spawns the real command
and asserts sys.modules.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import stat
import sys
import time
import warnings

from app.sandbox import limits as limits_mod
from app.sandbox import protocol, textclean

_T0 = time.monotonic()

_MAX_LIMITS_FILE_BYTES = 64 * 1024
_MAX_LIST_FILE_BYTES = 8 * 1024 * 1024
_MAX_METADATA_READ_BYTES = 256 * 1024
_VERIFY_PIXEL_MARGIN = 1024 * 1024
# The index is zero-padded to protocol.PAGE_NAME_WIDTH, which is a MINIMUM
# width: an index of 10000 or more spells more digits. The page_file()
# round-trip in _verify_one is what pins the one canonical spelling.
_PAGE_NAME_RE = re.compile(
    rf"({protocol.THUMB_DIR}|{protocol.FULL_DIR})/(\d{{{protocol.PAGE_NAME_WIDTH},}})\.jpg"
)
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_./-]{1,64}")

# PDFium metadata key -> protocol.METADATA_KEYS name.
_METADATA_MAP = (
    ("Title", "title"),
    ("Author", "author"),
    ("Subject", "subject"),
    ("Keywords", "keywords"),
    ("Creator", "creator"),
    ("Producer", "producer"),
    ("CreationDate", "creation_date"),
    ("ModDate", "mod_date"),
)


class _ArgError(Exception):
    """An argument or limits problem; always exits EXIT_BAD_ARGS."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ArgError(message)


def _stderr(line: str) -> None:
    try:
        sys.stderr.write(line.rstrip("\n") + "\n")
        sys.stderr.flush()
    except OSError:
        pass


def _elapsed_ms() -> int:
    return int((time.monotonic() - _T0) * 1000)


# ── Progress log ─────────────────────────────────────────────────────────────


class _Log:
    """Append-only JSONL writer: one os.write per event."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def emit(self, event: str, **fields: object) -> None:
        line = json.dumps(
            {"event": event, **fields}, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
        view = memoryview(line)
        while len(view):
            n = os.write(self.fd, view)
            view = view[n:]

    def heartbeat(self, phase: str) -> None:
        self.emit(protocol.EVENT_HEARTBEAT, phase=phase, elapsed_ms=_elapsed_ms())


def _open_exclusive(path: str) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC, 0o600)


def _ensure_output_dirs(out: str) -> None:
    """thumb/, full/ and text/ under <out>; a parent-created one is fine."""
    for name in (protocol.THUMB_DIR, protocol.FULL_DIR, protocol.TEXT_DIR):
        os.makedirs(os.path.join(out, name), mode=0o700, exist_ok=True)


def _close_quietly(doc) -> None:
    try:
        doc.close()
    except Exception:  # noqa: BLE001 (a close failure must never lose an event)
        pass


# ── Arguments ────────────────────────────────────────────────────────────────


def _read_bounded(path: str, max_bytes: int, what: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise _ArgError(f"{what} is larger than {max_bytes} bytes")
    return data


def _load_limits(arg: str) -> dict:
    text = arg
    if not arg.lstrip().startswith("{"):
        try:
            text = _read_bounded(arg, _MAX_LIMITS_FILE_BYTES, "limits file").decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _ArgError(f"limits file unreadable ({type(exc).__name__})") from exc
    try:
        document = json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:
        raise _ArgError("limits document is not valid JSON") from exc
    try:
        return protocol.validate_limits(document)
    except ValueError as exc:
        raise _ArgError(str(exc)) from exc


def _refuse_constant(name: str) -> None:
    raise ValueError(f"refusing JSON constant {name}")


def _load_skip(path: str | None, max_pages: int) -> set[int]:
    if path is None:
        return set()
    try:
        data = _read_bounded(path, _MAX_LIST_FILE_BYTES, "skip file").decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise _ArgError(f"skip file unreadable ({type(exc).__name__})") from exc
    out: set[int] = set()
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.isdigit():
            raise _ArgError("skip file must hold one non-negative integer per line")
        value = int(line)
        if value >= max_pages:
            raise _ArgError("skip file index exceeds max_pages")
        out.add(value)
    return out


def _load_list(path: str) -> list[str]:
    try:
        data = _read_bounded(path, _MAX_LIST_FILE_BYTES, "list file").decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _ArgError(f"list file unreadable ({type(exc).__name__})") from exc
    return [line.strip() for line in data.splitlines() if line.strip()]


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = _Parser(prog=protocol.MODULE, add_help=False, allow_abbrev=False)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--spawn", type=int, default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limits", required=True)
    parser.add_argument("--skip-file", dest="skip_file", default=None)
    parser.add_argument("--list-file", dest="list_file", default=None)
    args = parser.parse_args(argv)
    if not os.path.isdir(args.out):
        raise _ArgError("--out is not a directory")
    args.out = os.path.abspath(args.out)
    if args.verify:
        if args.list_file is None:
            raise _ArgError("--verify requires --list-file")
        if args.spawn is not None or args.input is not None or args.skip_file is not None:
            raise _ArgError("--verify takes only --out, --limits and --list-file")
    else:
        if args.spawn is None or args.spawn < 0:
            raise _ArgError("--spawn must be a non-negative integer")
        if args.input is None:
            raise _ArgError("--input is required")
        if not os.path.isfile(args.input):
            raise _ArgError("--input is not a file")
        if args.list_file is not None:
            raise _ArgError("--list-file is only valid with --verify")
    return args


# ── Process mode ─────────────────────────────────────────────────────────────


def _pdf_version(doc) -> str | None:
    import ctypes

    import pypdfium2.raw as raw

    value = ctypes.c_int()
    if not raw.FPDF_GetFileVersion(doc, value):
        return None
    number = int(value.value)
    if number < 0:
        return None
    return f"{number // 10}.{number % 10}"


def _metadata(doc) -> dict[str, str]:
    import ctypes

    import pypdfium2.raw as raw

    out: dict[str, str] = {}
    for pdf_key, name in _METADATA_MAP:
        enc_key = (pdf_key + "\x00").encode("utf-8")
        needed = int(raw.FPDF_GetMetaText(doc, enc_key, None, 0))
        value = ""
        if 2 < needed <= _MAX_METADATA_READ_BYTES:
            buffer = ctypes.create_string_buffer(needed)
            raw.FPDF_GetMetaText(doc, enc_key, buffer, needed)
            value = bytes(memoryview(buffer)[: needed - 2]).decode("utf-16-le", errors="replace")
        out[name] = textclean.clip(value, protocol.METADATA_MAX_CHARS)
    return out


def _open_document(log: _Log, path: str, limits: dict):
    """Open through the raw API. Returns (PdfDocument, page_count, info) or
    (None, 0, None) after writing a reject event."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    log.heartbeat(protocol.PHASE_OPEN)
    handle = raw.FPDF_LoadDocument((path + "\x00").encode("utf-8", errors="surrogateescape"), None)
    if not handle:
        err = int(raw.FPDF_GetLastError())
        if err == raw.FPDF_ERR_PASSWORD:
            log.emit(
                protocol.EVENT_REJECT,
                code=protocol.REJECT_ENCRYPTED,
                detail="The PDF requires a password to open.",
            )
        else:
            reasons = {
                raw.FPDF_ERR_SECURITY: "PDFium does not support the file's security handler.",
                raw.FPDF_ERR_FORMAT: "PDFium reported a data format error.",
                raw.FPDF_ERR_FILE: "PDFium could not read the file.",
            }
            log.emit(
                protocol.EVENT_REJECT,
                code=protocol.REJECT_UNREADABLE,
                detail=reasons.get(err, "PDFium could not open the file.")[
                    : protocol.DETAIL_MAX_CHARS
                ],
            )
        return None, 0, None
    log.heartbeat(protocol.PHASE_OPEN)
    page_count = int(raw.FPDF_GetPageCount(handle))
    # The verdict is written BEFORE the handle is closed: closing a hostile
    # document is PDFium work that can crash or be killed, and an event the
    # child never wrote is an event the parent must attribute as a crash.
    if page_count <= 0:
        log.emit(
            protocol.EVENT_REJECT, code=protocol.REJECT_NO_PAGES, detail="The PDF has no pages."
        )
        raw.FPDF_CloseDocument(handle)
        return None, 0, None
    if page_count > limits["max_pages"]:
        log.emit(
            protocol.EVENT_REJECT,
            code=protocol.REJECT_TOO_MANY_PAGES,
            detail=f"The PDF has {page_count} pages; the limit is {limits['max_pages']}.",
        )
        raw.FPDF_CloseDocument(handle)
        return None, 0, None
    doc = pdfium.PdfDocument(handle)
    revision = int(raw.FPDF_GetSecurityHandlerRevision(doc))
    info = {
        "owner_restricted": revision != -1,
        "security_handler_revision": revision if revision != -1 else None,
    }
    return doc, page_count, info


def _inventory(log: _Log, doc) -> dict:
    from app.sandbox import hazards

    log.heartbeat(protocol.PHASE_INVENTORY)
    out = {
        "pdf_version": _pdf_version(doc),
        "form_type": hazards.form_type(doc),
        "metadata": _metadata(doc),
    }
    log.heartbeat(protocol.PHASE_INVENTORY)
    out["hazards"] = hazards.document_hazards(doc)
    return out


def _ready_event(log: _Log) -> bool:
    """Import the parsers and write `ready`. Returns False (after a stderr
    line) when this PDFium build must not be used."""
    try:
        import PIL
        from pypdfium2.version import PDFIUM_INFO, PYPDFIUM_INFO

        from app.sandbox import render  # noqa: F401 (pulls in pypdfium2 and Pillow's JPEG plugin)
    except Exception as exc:  # noqa: BLE001 (any import failure is a deployment problem)
        _stderr(f"sandbox: cannot import the PDF/JPEG libraries ({type(exc).__name__})")
        return False
    raw_flags = getattr(PDFIUM_INFO, "flags", None)
    pdfium_flags = (
        None if raw_flags is None else {"v8": "V8" in raw_flags, "xfa": "XFA" in raw_flags}
    )
    log.emit(
        protocol.EVENT_READY,
        versions={
            "python": platform.python_version(),
            "pypdfium2": str(PYPDFIUM_INFO),
            "pdfium": str(getattr(PDFIUM_INFO, "version", "") or PDFIUM_INFO),
            "pillow": str(getattr(PIL, "__version__", "")),
        },
        pdfium_flags=pdfium_flags,
        platform=f"{platform.system()}-{platform.machine()}",
    )
    if pdfium_flags is not None and pdfium_flags["v8"]:
        _stderr("sandbox: refusing a PDFium build with V8 (JavaScript) compiled in")
        return False
    return True


def _run_process(args: argparse.Namespace, limits: dict) -> int:
    skip = _load_skip(args.skip_file, limits["max_pages"])
    try:
        fd = _open_exclusive(os.path.join(args.out, protocol.progress_file(args.spawn)))
    except FileExistsError:
        raise _ArgError("progress file for this spawn already exists") from None
    except OSError as exc:
        raise _ArgError(f"cannot create the progress file ({type(exc).__name__})") from exc
    log = _Log(fd)
    try:
        _ensure_output_dirs(args.out)
    except OSError as exc:
        raise _ArgError(f"cannot create the output directories ({type(exc).__name__})") from exc

    applied = limits_mod.apply(limits)
    log.emit(
        protocol.EVENT_START,
        sandbox_version=protocol.SANDBOX_VERSION,
        protocol_version=protocol.PROTOCOL_VERSION,
        spawn=args.spawn,
        pid=os.getpid(),
        uid=os.getuid(),
        gid=os.getgid(),
        limits=limits,
        limits_applied=applied,
        skip_count=len(skip),
    )
    if not _ready_event(log):
        return protocol.EXIT_BAD_ARGS

    from app.sandbox import render

    doc = None
    try:
        doc, page_count, info = _open_document(log, args.input, limits)
        if doc is None:
            return protocol.EXIT_OK
        inventory = _inventory(log, doc)
    except MemoryError:
        log.emit(
            protocol.EVENT_REJECT,
            code=protocol.REJECT_UNREADABLE,
            detail="The sandbox ran out of memory while opening the PDF.",
        )
        if doc is not None:
            _close_quietly(doc)
        return protocol.EXIT_OK
    except Exception as exc:  # noqa: BLE001 (an open-phase fault is a verdict, not a crash)
        log.emit(
            protocol.EVENT_REJECT,
            code=protocol.REJECT_UNREADABLE,
            detail=f"The sandbox failed while inspecting the PDF ({type(exc).__name__}).",
        )
        if doc is not None:
            _close_quietly(doc)
        return protocol.EXIT_OK

    log.emit(
        protocol.EVENT_DOCUMENT,
        page_count=page_count,
        pdf_version=inventory["pdf_version"],
        owner_restricted=info["owner_restricted"],
        security_handler_revision=info["security_handler_revision"],
        form_type=inventory["form_type"],
        metadata=inventory["metadata"],
        hazards=inventory["hazards"],
    )

    pages_ok = pages_failed = 0
    output_bytes = 0
    aborted: str | None = None
    deadline = float(limits["deadline_seconds"])
    quota = int(limits["max_output_total_bytes"])
    for index in range(page_count):
        if index in skip:
            continue
        if time.monotonic() - _T0 > deadline:
            aborted = protocol.ABORT_DEADLINE
            break
        if output_bytes > quota:
            aborted = protocol.ABORT_DISK_QUOTA
            break
        log.emit(protocol.EVENT_PAGE_START, index=index)
        event, written = render.process_page(doc, index, out_dir=args.out, limits=limits)
        output_bytes += written
        if event["status"] == protocol.PAGE_OK:
            pages_ok += 1
        else:
            pages_failed += 1
        log.emit(protocol.EVENT_PAGE, **event)

    # `end` first, then the close. Nothing in the event depends on the
    # document being closed, and a death inside FPDF_CloseDocument (a crash in
    # the destructor chain, SIGXCPU, an OOM kill) would otherwise leave a log
    # with no terminal event: the parent would find nothing left to blame,
    # call it crash_loop and throw away every page this spawn rendered.
    log.emit(
        protocol.EVENT_END,
        pages_ok=pages_ok,
        pages_failed=pages_failed,
        elapsed_ms=_elapsed_ms(),
        peak_rss_kb=limits_mod.peak_rss_kb(),
        output_bytes=output_bytes,
        aborted=aborted,
    )
    _close_quietly(doc)
    return protocol.EXIT_OK


# ── Verify mode ──────────────────────────────────────────────────────────────


def _verify_one(out_fd: int, name: str, max_pixels: int) -> tuple[bool, int | None, int | None]:
    from PIL import Image

    match = _PAGE_NAME_RE.fullmatch(name)
    if match is None or protocol.page_file(match[1], int(match[2]), "jpg") != name:
        _stderr("sandbox verify: refused a name outside the page artifact shape")
        return False, None, None
    kind_dir, base = match[1], f"{match[2]}.jpg"
    try:
        dir_fd = os.open(
            kind_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=out_fd
        )
        try:
            fd = os.open(base, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        _stderr(f"sandbox verify: {name} could not be opened ({type(exc).__name__})")
        return False, None, None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            _stderr(f"sandbox verify: {name} is not a regular file")
            return False, None, None
        with os.fdopen(fd, "rb") as fh, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(fh) as image:
                width, height = image.size
                if width * height > max_pixels or image.format != "JPEG":
                    _stderr(f"sandbox verify: {name} is not a JPEG within the pixel bound")
                    return False, width, height
                image.load()
                if image.mode != "RGB":
                    _stderr(f"sandbox verify: {name} is not an RGB JPEG")
                    return False, width, height
                return True, width, height
    except Exception as exc:  # noqa: BLE001 (a decode failure of any kind is ok=false)
        _stderr(f"sandbox verify: {name} failed to decode ({type(exc).__name__})")
        return False, None, None


def _run_verify(args: argparse.Namespace, limits: dict) -> int:
    names = _load_list(args.list_file)
    try:
        fd = _open_exclusive(os.path.join(args.out, protocol.VERIFY_FILE))
    except FileExistsError:
        raise _ArgError("verify file already exists") from None
    except OSError as exc:
        raise _ArgError(f"cannot create the verify file ({type(exc).__name__})") from exc
    log = _Log(fd)
    applied = limits_mod.apply(limits)
    _stderr(
        "sandbox verify: limits applied "
        + ",".join(f"{label}={int(bool(applied[label]))}" for label in limits_mod.LABELS)
    )
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 (a deployment problem, exit bad-args)
        _stderr(f"sandbox verify: cannot import Pillow ({type(exc).__name__})")
        return protocol.EXIT_BAD_ARGS
    max_pixels = int(limits["full_long_side"]) ** 2 + _VERIFY_PIXEL_MARGIN
    Image.MAX_IMAGE_PIXELS = max_pixels
    out_fd = os.open(args.out, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in names:
            ok, width, height = _verify_one(out_fd, name, max_pixels)
            safe = name if _SAFE_NAME_RE.fullmatch(name) else "invalid"
            log.emit(protocol.EVENT_VERIFIED, file=safe, w=width, h=height, ok=ok)
    finally:
        os.close(out_fd)
    return protocol.EXIT_OK


# ── Entry ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parse(argv)
        limits = _load_limits(args.limits)
        if args.verify:
            return _run_verify(args, limits)
        return _run_process(args, limits)
    except _ArgError as exc:
        _stderr(f"sandbox: bad arguments: {exc}")
        return protocol.EXIT_BAD_ARGS


if __name__ == "__main__":
    sys.exit(main())
