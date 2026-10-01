"""The RFP Ingestion Sandbox child, exercised as a REAL subprocess.

Every test here spawns the child with the exact launch command the parent
uses (`protocol.INTERPRETER_FLAGS` + `-c protocol.BOOTSTRAP <repo_root>`),
cwd set to the out dir, the scrubbed three-variable environment, stdin and
stdout on DEVNULL and stderr on an O_EXCL file, then reads the progress log
back the way the parent does: complete lines only, strict JSON, NaN and
Infinity refused. Nothing is monkeypatched, because the point is the
contract between two processes, not the Python inside one of them.

What is pinned (docs/RFP_INGESTION_SANDBOX.md sections 2, 3.1 to 3.3, 5):

- the event sequence and the required keys of every event, in order, for a
  blank multi-page file, and that every line is one strictly valid JSON
  object ending in a newline;
- the render contract: thumb and full tiers at exactly the configured long
  side, RGB baseline JPEGs whose marker walk admits only what the parent's
  walk admits, sizes and sha256 that match the bytes on disk, the small
  reading tier for letter pages and the large one for big sheets;
- the text contract: a hand-written Helvetica page yields its text, the
  per-page cap sets `truncated`, and hidden characters (a ToUnicode CMap
  emitting U+200B, U+E000 and U+FFFD) are stripped and counted;
- every child reject code: encrypted, unreadable (truncated bytes), no_pages,
  too_many_pages; owner-only restrictions are allowed and recorded;
- page failures: page_size for a giant media box (nothing rendered), and
  render_error for a page PDFium cannot load, with no artifacts left behind;
- the hazard inventory on a pypdf-built file carrying document JavaScript,
  an embedded file, URI / Launch / GoToR / GoToE links, page open/close
  actions and a FileAttachment annotation;
- resource behavior: the Flate bomb under a 20 s parent deadline (with the
  memory assertion only on Linux, where RLIMIT_AS applies), the child's own
  wall-clock deadline and disk quota aborts, and the rlimit readback;
- that a terminal event outlives the document close: with every PDFium
  close turned into an instant SIGKILL, a fully rendered file still leaves a
  complete `end` and an open-phase reject still reaches the log;
- --skip-file resume on a later spawn, the O_EXCL progress file, verify
  mode (ok, corrupt, wrong bytes, refused names, a page index wider than
  PAGE_NAME_WIDTH), EXIT_BAD_ARGS for every
  argument or limits problem, and the import boundary (a fresh interpreter's
  sys.modules holds nothing from app.core, app.services, pydantic, supabase
  or httpx after a full run).

Fixtures are built in-process: pypdf for the ordinary files and generic
objects, hand-written bytes (with a correct xref) for the text page, the
giant media box, the broken page and the bombs. reportlab is not installed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.sandbox import protocol, textclean

ROOT = Path(__file__).resolve().parent.parent
SCRUBBED_KEYS = ("LANG", "TMPDIR", "HOME")
FORBIDDEN_MODULE_PREFIXES = (
    "app.core",
    "app.services",
    "app.routers",
    "pydantic",
    "supabase",
    "httpx",
    "fastapi",
    "anthropic",
    "openai",
)
BOOMB_TIMEOUT_SECONDS = 20


# ── Test PDFs ────────────────────────────────────────────────────────────


def _blank_pdf(pages: int, width: float = 612, height: float = 792) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_blank_pdf(1))))
    writer.encrypt("owner-and-user-pw")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _owner_only_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_blank_pdf(1))))
    writer.encrypt(user_password="", owner_password="owner-only", permissions_flag=0)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _zero_pages_pdf() -> bytes:
    from pypdf import PdfWriter

    buf = io.BytesIO()
    PdfWriter().write(buf)
    return buf.getvalue()


def _assemble(objs: list[bytes]) -> bytes:
    """Hand-written PDF: numbered objects (1-based) plus a correct xref."""
    data = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(data)
    size = len(objs) + 1
    data += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    data += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return data


def _stream(dictionary: bytes, payload: bytes) -> bytes:
    return (
        b"<< " + dictionary + b" /Length " + str(len(payload)).encode() + b" >>\nstream\n"
        + payload + b"\nendstream"
    )


def _one_page(page_extra: bytes, content: bytes, extra_objs: list[bytes] | None = None) -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] " + page_extra + b" >>",
        _stream(b"", content),
    ]
    return _assemble(objs + (extra_objs or []))


TEXT_PAGE_WORDS = "Hello RFP world"


def _text_pdf() -> bytes:
    content = f"BT /F1 24 Tf 72 700 Td ({TEXT_PAGE_WORDS}) Tj ET".encode()
    return _one_page(
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R",
        content,
        [b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"],
    )


def _hidden_text_pdf() -> bytes:
    """A: U+200B (Cf), B: U+E000 (Co), C: U+FFFD, via a ToUnicode CMap."""
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        b"/CMapName /Custom def\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        b"3 beginbfchar\n<41> <200B>\n<42> <E000>\n<43> <FFFD>\nendbfchar\n"
        b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    )
    content = b"BT /F1 24 Tf 72 700 Td (ABC Hi\\tthere) Tj ET"
    return _one_page(
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R",
        content,
        [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding"
            b" /ToUnicode 6 0 R >>",
            _stream(b"", cmap),
        ],
    )


def _giant_pdf() -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 20000 20000] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    return _assemble(objs)


def _broken_kid_pdf() -> bytes:
    """Page 1 is the integer 42, which FPDF_LoadPage refuses."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        b"42",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    return _assemble(objs)


def _image_bomb_pdf(side: int) -> bytes:
    """A Flate stream that inflates to side x side gray bytes (side=30000 is
    900 MB from under 1 MB of file)."""
    compressed = zlib.compress(b"\x00" * (side * side), 9)
    dictionary = (
        f"/Type /XObject /Subtype /Image /Width {side} /Height {side} /ColorSpace /DeviceGray"
        " /BitsPerComponent 8 /Filter /FlateDecode"
    ).encode()
    return _one_page(
        b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R",
        b"q 612 0 0 792 0 0 cm /Im0 Do Q",
        [_stream(dictionary, compressed)],
    )


def _slow_then_blank_pdf(ops: int) -> bytes:
    """Page 0 draws `ops` diagonal strokes (seconds of PDFium time), page 1
    is blank; used to trip the child's own wall-clock deadline."""
    content = zlib.compress(b"0 0 m 612 792 l S\n" * ops, 9)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        _stream(b"/Filter /FlateDecode", content),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    return _assemble(objs)


def _form_pdf(xfa: bool) -> bytes:
    """An AcroForm catalog, optionally with a three-packet XFA entry."""
    xfa_entry = b" /XFA [(preamble) 4 0 R (template) 5 0 R (postamble) 6 0 R]" if xfa else b""
    template = (
        b"<template xmlns='http://www.xfa.org/schema/xfa-template/3.3'>"
        b"<subform name='f'><pageSet/></subform></template>"
    )
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields []" + xfa_entry + b" >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        _stream(b"", b"<?xml version='1.0'?><xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'>"),
        _stream(b"", template),
        _stream(b"", b"</xdp:xdp>"),
    ]
    return _assemble(objs)


def _hazard_pdf() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    def name(value: str) -> NameObject:
        return NameObject(value)

    def rect(x0: float) -> ArrayObject:
        return ArrayObject(
            [FloatObject(x0), FloatObject(10), FloatObject(x0 + 80), FloatObject(50)]
        )

    def link(x0: float, action: DictionaryObject) -> DictionaryObject:
        return DictionaryObject(
            {
                name("/Type"): name("/Annot"),
                name("/Subtype"): name("/Link"),
                name("/Rect"): rect(x0),
                name("/A"): action,
            }
        )

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    uri = link(10, DictionaryObject({name("/S"): name("/URI"),
                                     name("/URI"): TextStringObject("http://example.com")}))
    launch = link(110, DictionaryObject({name("/S"): name("/Launch"),
                                         name("/F"): TextStringObject("calc.exe")}))
    remote = link(210, DictionaryObject({
        name("/S"): name("/GoToR"), name("/F"): TextStringObject("other.pdf"),
        name("/D"): ArrayObject([NumberObject(0), name("/Fit")]),
    }))
    embedded = link(310, DictionaryObject({
        name("/S"): name("/GoToE"), name("/D"): ArrayObject([NumberObject(0), name("/Fit")]),
    }))
    stream = DecodedStreamObject()
    stream.set_data(b"hello")
    stream[name("/Type")] = name("/EmbeddedFile")
    filespec = DictionaryObject({
        name("/Type"): name("/Filespec"), name("/F"): TextStringObject("a.txt"),
        name("/EF"): DictionaryObject({name("/F"): writer._add_object(stream)}),
    })
    attachment = DictionaryObject({
        name("/Type"): name("/Annot"), name("/Subtype"): name("/FileAttachment"),
        name("/Rect"): rect(410), name("/FS"): writer._add_object(filespec),
    })
    page[name("/Annots")] = ArrayObject(
        [writer._add_object(a) for a in (uri, launch, remote, embedded, attachment)]
    )
    script = writer._add_object(DictionaryObject({
        name("/S"): name("/JavaScript"), name("/JS"): TextStringObject("app.alert(1)"),
    }))
    page[name("/AA")] = DictionaryObject({name("/O"): script, name("/C"): script})
    writer.add_js("app.alert('x')")
    writer._root_object[name("/OpenAction")] = writer._add_object(DictionaryObject({
        name("/S"): name("/JavaScript"), name("/JS"): TextStringObject("this.print()"),
    }))
    writer.add_attachment("evil.txt", b"evil")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── Launch harness ───────────────────────────────────────────────────────


def _limits(**over) -> dict:
    base = dict(
        memory_bytes=1536 * 1024 * 1024,
        cpu_seconds=120,
        max_output_file_bytes=16 * 1024 * 1024,
        max_output_total_bytes=64 * 1024 * 1024,
        max_open_files=64,
        max_processes=1,
        max_pages=50,
        max_page_side_pt=14400,
        thumb_long_side=256,
        thumb_jpeg_quality=70,
        full_long_side=600,
        full_small_long_side=400,
        full_small_threshold_pt=1300,
        full_jpeg_quality=85,
        max_text_chars_per_page=200_000,
        deadline_seconds=60,
        heartbeat_seconds=10,
    )
    base.update(over)
    return protocol.validate_limits(base)


@dataclass
class Run:
    returncode: int | None
    killed: bool
    out: Path
    events: list[dict] = field(default_factory=list)
    torn: bool = False
    stderr: str = ""

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["event"] == kind]

    def one(self, kind: str) -> dict:
        found = self.of(kind)
        assert len(found) == 1, f"expected exactly one {kind}, got {len(found)}"
        return found[0]

    def files(self) -> set[str]:
        return {
            os.path.relpath(os.path.join(d, f), self.out)
            for d, _, names in os.walk(self.out)
            for f in names
        }


def _refuse_constant(name: str) -> None:
    raise ValueError(f"refused JSON constant {name}")


def _parse_log(data: bytes) -> tuple[list[dict], bool]:
    """The parent's reading: complete lines only, strict JSON."""
    torn = bool(data) and not data.endswith(b"\n")
    lines = data.split(b"\n")
    if torn:
        lines = lines[:-1]
    events = []
    for line in lines:
        if not line:
            continue
        assert len(line) <= protocol.MAX_PROGRESS_LINE_BYTES
        events.append(json.loads(line, parse_constant=_refuse_constant))
    return events, torn


class Harness:
    """One scratch dir per test: source.pdf + limits + out/ next to each other."""

    def __init__(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="rfp-sandbox-test-"))
        self.out = self.work / "out"
        (self.out / protocol.TMP_DIR).mkdir(parents=True)
        self.out.chmod(0o700)

    def write_pdf(self, data: bytes) -> Path:
        path = self.work / "source.pdf"
        path.write_bytes(data)
        return path

    def write_limits(self, limits: dict, name: str = "limits.json") -> Path:
        path = self.work / name
        path.write_text(json.dumps(limits))
        return path

    def write_lines(self, name: str, lines: list[str]) -> Path:
        path = self.work / name
        path.write_text("".join(f"{line}\n" for line in lines))
        return path

    def command(self, *args: str, bootstrap: str = protocol.BOOTSTRAP,
                extra_bootstrap_args: tuple[str, ...] = ()) -> list[str]:
        return [
            sys.executable, *protocol.INTERPRETER_FLAGS, "-c", bootstrap, str(ROOT),
            *extra_bootstrap_args, *args,
        ]

    def spawn(self, cmd: list[str], *, stderr_name: str, timeout: float) -> tuple[int | None, bool, str]:
        env = {
            "LANG": "C.UTF-8",
            "TMPDIR": str(self.out / protocol.TMP_DIR),
            "HOME": str(self.out / protocol.TMP_DIR),
        }
        stderr_path = self.out / stderr_name
        fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.out, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=fd, start_new_session=True,
            )
        finally:
            os.close(fd)
        killed = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            killed = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        return proc.returncode, killed, stderr_path.read_text(errors="replace")

    def process(self, pdf: bytes | Path, limits: dict | None = None, *, spawn: int = 0,
                skip: list[int] | None = None, timeout: float = 60,
                limits_arg: str | None = None, extra: tuple[str, ...] = (),
                bootstrap: str = protocol.BOOTSTRAP) -> Run:
        source = self.write_pdf(pdf) if isinstance(pdf, bytes) else pdf
        limits_value = limits_arg or str(self.write_limits(limits or _limits()))
        args = ["--spawn", str(spawn), "--input", str(source), "--out", str(self.out),
                "--limits", limits_value, *extra]
        if skip is not None:
            args += ["--skip-file", str(self.write_lines(f"skip.{spawn}.txt", [str(i) for i in skip]))]
        code, killed, stderr = self.spawn(
            self.command(*args, bootstrap=bootstrap),
            stderr_name=protocol.stderr_file(spawn), timeout=timeout,
        )
        log_path = self.out / protocol.progress_file(spawn)
        events, torn = _parse_log(log_path.read_bytes()) if log_path.exists() else ([], False)
        return Run(code, killed, self.out, events, torn, stderr)

    def verify(self, names: list[str], limits: dict | None = None) -> Run:
        list_path = self.write_lines("verify-list.txt", names)
        args = ["--verify", "--out", str(self.out), "--limits",
                str(self.write_limits(limits or _limits(), "limits.verify.json")),
                "--list-file", str(list_path)]
        code, killed, stderr = self.spawn(
            self.command(*args), stderr_name="stderr.verify.log", timeout=60
        )
        log_path = self.out / protocol.VERIFY_FILE
        events, torn = _parse_log(log_path.read_bytes()) if log_path.exists() else ([], False)
        return Run(code, killed, self.out, events, torn, stderr)


@pytest.fixture
def box():
    harness = Harness()
    yield harness
    shutil.rmtree(harness.work, ignore_errors=True)


# ── JPEG marker walk (the parent's rules, mirrored for assertions) ───────

_ALLOWED_MARKERS = {0xE0, 0xDB, 0xC0, 0xC2, 0xC4, 0xDD, 0xDA}


def _jpeg_walk(data: bytes) -> tuple[int, int, int]:
    """(w, h, components) after admitting only what the parent admits."""
    assert data[:2] == b"\xff\xd8", "missing SOI"
    i = 2
    dims = None
    while True:
        assert data[i] == 0xFF, f"expected a marker at {i}"
        marker = data[i + 1]
        if marker == 0xD9:
            assert i + 2 == len(data), "bytes after EOI"
            break
        assert marker in _ALLOWED_MARKERS, f"disallowed marker {marker:#x}"
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC2):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            dims = (width, height, data[i + 9])
        if marker == 0xDA:
            j = i + 2 + length
            while True:
                assert j < len(data) - 1, "no EOI after SOS"
                if data[j] == 0xFF and data[j + 1] != 0 and not 0xD0 <= data[j + 1] <= 0xD7:
                    break
                j += 1
            i = j
            continue
        i += 2 + length
    assert dims is not None, "no SOF"
    return dims


REQUIRED_KEYS = {
    protocol.EVENT_START: {"sandbox_version", "protocol_version", "spawn", "pid", "uid", "gid",
                           "limits", "limits_applied", "skip_count"},
    protocol.EVENT_READY: {"versions", "pdfium_flags", "platform"},
    protocol.EVENT_HEARTBEAT: {"phase", "elapsed_ms"},
    protocol.EVENT_DOCUMENT: {"page_count", "pdf_version", "owner_restricted",
                              "security_handler_revision", "form_type", "metadata", "hazards"},
    protocol.EVENT_REJECT: {"code", "detail"},
    protocol.EVENT_PAGE_START: {"index"},
    protocol.EVENT_PAGE: {"status", "index"},
    protocol.EVENT_END: {"pages_ok", "pages_failed", "elapsed_ms", "peak_rss_kb",
                         "output_bytes", "aborted"},
}
PAGE_OK_KEYS = {"width_pt", "height_pt", "rotation", "tier", "thumb", "full", "text", "hazards",
                "render_ms"}
ARTIFACT_KEYS = {"file", "w", "h", "bytes", "sha256"}
TEXT_KEYS = {"file", "chars", "truncated", "sha256", "hazards"}


def _assert_well_formed(run: Run) -> None:
    """Every event carries its required keys; page_start precedes page."""
    for event in run.events:
        assert event["event"] in protocol.EVENTS, event
        assert REQUIRED_KEYS[event["event"]] <= set(event), event
    assert run.events[0]["event"] == protocol.EVENT_START
    assert run.events[1]["event"] == protocol.EVENT_READY
    open_index = None
    for i, event in enumerate(run.events):
        if event["event"] == protocol.EVENT_PAGE_START:
            assert run.events[i + 1]["event"] == protocol.EVENT_PAGE
            assert run.events[i + 1]["index"] == event["index"]
            assert open_index is None or event["index"] > open_index
            open_index = event["index"]
        if event["event"] == protocol.EVENT_PAGE:
            if event["status"] == protocol.PAGE_OK:
                assert PAGE_OK_KEYS <= set(event), event
                assert ARTIFACT_KEYS == set(event["thumb"]) == set(event["full"])
                assert TEXT_KEYS == set(event["text"])
                assert set(event["hazards"]) == set(protocol.PAGE_HAZARD_KEYS)
                assert set(event["text"]["hazards"]) == set(protocol.TEXT_HAZARD_KEYS)
            else:
                assert event["status"] == protocol.PAGE_FAILED
                assert event["code"] in protocol.CHILD_PAGE_FAIL_CODES
                assert isinstance(event["detail"], str)
                assert len(event["detail"]) <= protocol.DETAIL_MAX_CHARS


def _assert_artifact(run: Run, meta: dict, long_side: int) -> None:
    import hashlib

    data = (run.out / meta["file"]).read_bytes()
    assert len(data) == meta["bytes"]
    assert hashlib.sha256(data).hexdigest() == meta["sha256"]
    width, height, components = _jpeg_walk(data)
    assert (width, height) == (meta["w"], meta["h"])
    assert components == protocol.JPEG_COMPONENTS
    assert max(width, height) == long_side


# ── Blank multi-page: the whole sequence ─────────────────────────────────


def test_blank_multipage_writes_the_full_event_sequence_and_valid_artifacts(box):
    limits = _limits()
    run = box.process(_blank_pdf(3), limits)
    assert run.returncode == protocol.EXIT_OK and not run.killed and not run.torn
    _assert_well_formed(run)

    start = run.one(protocol.EVENT_START)
    assert start["sandbox_version"] == protocol.SANDBOX_VERSION
    assert start["protocol_version"] == protocol.PROTOCOL_VERSION
    assert start["spawn"] == 0 and start["skip_count"] == 0
    assert start["uid"] == os.getuid() and start["gid"] == os.getgid()
    assert start["limits"] == limits
    applied = start["limits_applied"]
    assert set(applied) == {"memory", "cpu", "fsize", "nofile", "nproc", "core", "readback"}
    assert applied["core"] and applied["cpu"] and applied["fsize"] and applied["nofile"]
    assert applied["readback"]["fsize"] == [limits["max_output_file_bytes"]] * 2
    assert applied["readback"]["nofile"] == [64, 64]
    if sys.platform == "darwin":
        assert applied["memory"] is False  # RLIMIT_AS raises ValueError on macOS
    else:
        assert applied["memory"] is True

    ready = run.one(protocol.EVENT_READY)
    assert set(ready["versions"]) == {"python", "pypdfium2", "pdfium", "pillow"}
    assert ready["pdfium_flags"] == {"v8": False, "xfa": False}
    assert isinstance(ready["platform"], str) and ready["platform"]

    beats = run.of(protocol.EVENT_HEARTBEAT)
    assert {b["phase"] for b in beats} == {protocol.PHASE_OPEN, protocol.PHASE_INVENTORY}
    assert all(isinstance(b["elapsed_ms"], int) for b in beats)

    document = run.one(protocol.EVENT_DOCUMENT)
    assert document["page_count"] == 3
    assert document["pdf_version"] == "1.3"
    assert document["owner_restricted"] is False
    assert document["security_handler_revision"] is None
    assert document["form_type"] == protocol.FORM_NONE
    assert set(document["metadata"]) == set(protocol.METADATA_KEYS)
    assert document["metadata"]["producer"] == "pypdf"
    assert document["hazards"] == dict.fromkeys(protocol.DOC_HAZARD_KEYS, 0)

    pages = run.of(protocol.EVENT_PAGE)
    assert [p["index"] for p in pages] == [0, 1, 2]
    for page in pages:
        assert page["status"] == protocol.PAGE_OK
        assert (page["width_pt"], page["height_pt"], page["rotation"]) == (612.0, 792.0, 0)
        assert page["tier"] == "full_small"
        _assert_artifact(run, page["thumb"], limits["thumb_long_side"])
        _assert_artifact(run, page["full"], limits["full_small_long_side"])
        assert page["thumb"]["file"] == protocol.page_file(protocol.THUMB_DIR, page["index"], "jpg")
        assert page["full"]["file"] == protocol.page_file(protocol.FULL_DIR, page["index"], "jpg")
        text = page["text"]
        assert text["file"] == protocol.page_file(protocol.TEXT_DIR, page["index"], "txt")
        assert (run.out / text["file"]).read_bytes() == b""
        assert text["chars"] == 0 and text["truncated"] is False
        assert text["hazards"] == dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0)
        assert page["hazards"] == dict.fromkeys(protocol.PAGE_HAZARD_KEYS, 0)
        assert isinstance(page["render_ms"], int) and page["render_ms"] >= 0

    end = run.events[-1]
    assert end["event"] == protocol.EVENT_END
    assert (end["pages_ok"], end["pages_failed"], end["aborted"]) == (3, 0, None)
    assert end["output_bytes"] == sum(p["thumb"]["bytes"] + p["full"]["bytes"] for p in pages)
    assert end["peak_rss_kb"] > 1024  # kilobytes on every platform, never bytes
    assert end["elapsed_ms"] >= 0

    expected = {protocol.progress_file(0), protocol.stderr_file(0)}
    for i in range(3):
        expected |= {protocol.page_file(d, i, e) for d, e in (
            (protocol.THUMB_DIR, "jpg"), (protocol.FULL_DIR, "jpg"), (protocol.TEXT_DIR, "txt"))}
    assert run.files() == expected
    assert run.stderr == ""


def test_every_progress_line_is_one_strict_json_object_with_a_newline(box):
    run = box.process(_blank_pdf(2))
    raw = (run.out / protocol.progress_file(0)).read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.rstrip(b"\n").split(b"\n")
    assert len(lines) == len(run.events)
    for line in lines:
        assert b"\n" not in line and line.strip() == line
        obj = json.loads(line, parse_constant=_refuse_constant)
        assert isinstance(obj, dict) and obj["event"] in protocol.EVENTS
        # compact separators: no pretty-printing whitespace
        assert b": " not in line and b", " not in line


def test_large_sheet_uses_the_full_tier_and_a_letter_page_the_small_one(box):
    limits = _limits()
    run = box.process(_blank_pdf(1, width=2592, height=1728))  # 36 x 24 in sheet
    page = run.one(protocol.EVENT_PAGE)
    assert page["tier"] == "full"
    _assert_artifact(run, page["full"], limits["full_long_side"])
    assert page["full"]["w"] == limits["full_long_side"]
    _assert_artifact(run, page["thumb"], limits["thumb_long_side"])


# ── Text contract ────────────────────────────────────────────────────────


def test_text_page_yields_its_text_with_matching_hash_and_no_hazards(box):
    import hashlib

    run = box.process(_text_pdf())
    assert run.returncode == 0
    page = run.one(protocol.EVENT_PAGE)
    text = page["text"]
    data = (run.out / text["file"]).read_bytes()
    assert data.decode("utf-8") == TEXT_PAGE_WORDS
    assert text["chars"] == len(TEXT_PAGE_WORDS)
    assert text["truncated"] is False
    assert text["sha256"] == hashlib.sha256(data).hexdigest()
    assert text["hazards"] == dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0)
    assert run.one(protocol.EVENT_END)["output_bytes"] == (
        page["thumb"]["bytes"] + page["full"]["bytes"] + len(data)
    )


def test_text_cap_truncates_and_flags_the_page(box):
    run = box.process(_text_pdf(), _limits(max_text_chars_per_page=5))
    text = run.one(protocol.EVENT_PAGE)["text"]
    assert (run.out / text["file"]).read_text() == TEXT_PAGE_WORDS[:5]
    assert text["chars"] == 5 and text["truncated"] is True


def test_hidden_characters_are_stripped_from_text_and_counted(box):
    # The CMap makes PDFium emit a zero-width space, a private-use code point
    # and a replacement character ahead of the visible words; only the
    # visible words (tab included) may reach disk, and each hidden one is
    # counted so the agent slice can flag the page.
    run = box.process(_hidden_text_pdf())
    text = run.one(protocol.EVENT_PAGE)["text"]
    assert (run.out / text["file"]).read_text() == " Hi\tthere"
    assert text["chars"] == len(" Hi\tthere")
    assert text["hazards"] == {
        "format_chars": 1, "private_use": 1, "unassigned": 0, "replacement_chars": 1,
    }


@pytest.mark.parametrize(
    ("raw", "clean", "counts"),
    [
        ("plain\ttext\nhere", "plain\ttext\nhere", {}),
        ("a\x00b\rc\x07d\x7fe\x85f", "abcdef", {}),
        ("x\u200by\ufeffz\u00ad", "xyz", {"format_chars": 3}),
        ("a\ue000b\U000f0000", "ab", {"private_use": 2}),
        ("a\U000e0000b\ud800c", "abc", {"unassigned": 2}),
        ("bad\ufffdbyte", "badbyte", {"replacement_chars": 1}),
        ("café 中文  ok", "café 中文  ok", {}),
    ],
)
def test_sanitizer_rules_match_the_text_contract(raw, clean, counts):
    out, hazards = textclean.sanitize(raw)
    assert out == clean
    assert hazards == {**dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0), **counts}


def test_metadata_is_sanitized_and_capped(box):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_blank_pdf(1))))
    writer.add_metadata({"/Title": "T\u200bitle\x07 " + "x" * 2000, "/Author": "A\ufffdB"})
    buf = io.BytesIO()
    writer.write(buf)
    run = box.process(buf.getvalue())
    metadata = run.one(protocol.EVENT_DOCUMENT)["metadata"]
    assert metadata["title"].startswith("Title ")
    assert len(metadata["title"]) == protocol.METADATA_MAX_CHARS
    assert metadata["author"] == "AB"


# ── Document verdicts ────────────────────────────────────────────────────


def test_encrypted_file_is_rejected_with_no_pages_touched(box):
    run = box.process(_encrypted_pdf())
    assert run.returncode == protocol.EXIT_OK and not run.torn
    reject = run.one(protocol.EVENT_REJECT)
    assert reject["code"] == protocol.REJECT_ENCRYPTED
    assert run.events[-1] is reject
    assert not run.of(protocol.EVENT_PAGE_START) and not run.of(protocol.EVENT_DOCUMENT)
    assert not (run.files() - {protocol.progress_file(0), protocol.stderr_file(0)})


def test_owner_only_restrictions_are_recorded_and_allowed(box):
    run = box.process(_owner_only_pdf())
    document = run.one(protocol.EVENT_DOCUMENT)
    assert document["owner_restricted"] is True
    assert isinstance(document["security_handler_revision"], int)
    assert document["security_handler_revision"] >= 2
    assert run.one(protocol.EVENT_PAGE)["status"] == protocol.PAGE_OK
    assert run.one(protocol.EVENT_END)["pages_ok"] == 1


def test_zero_page_file_is_rejected_as_no_pages_not_unreadable(box):
    run = box.process(_zero_pages_pdf())
    assert run.returncode == protocol.EXIT_OK
    assert run.one(protocol.EVENT_REJECT)["code"] == protocol.REJECT_NO_PAGES


def test_truncated_bytes_are_rejected_as_unreadable(box):
    run = box.process(_blank_pdf(2)[:200])
    assert run.returncode == protocol.EXIT_OK
    reject = run.one(protocol.EVENT_REJECT)
    assert reject["code"] == protocol.REJECT_UNREADABLE
    assert len(reject["detail"]) <= protocol.DETAIL_MAX_CHARS
    assert "source.pdf" not in reject["detail"]


def test_over_the_page_cap_is_rejected_before_any_page(box):
    run = box.process(_blank_pdf(3), _limits(max_pages=2))
    assert run.one(protocol.EVENT_REJECT)["code"] == protocol.REJECT_TOO_MANY_PAGES
    assert not run.of(protocol.EVENT_PAGE_START)


# ── Page failures ────────────────────────────────────────────────────────


def test_giant_media_box_fails_page_size_without_rendering(box):
    run = box.process(_giant_pdf())
    assert run.returncode == protocol.EXIT_OK
    _assert_well_formed(run)
    failed, ok = run.of(protocol.EVENT_PAGE)
    assert failed["index"] == 0 and failed["status"] == protocol.PAGE_FAILED
    assert failed["code"] == protocol.PAGE_FAIL_SIZE
    assert ok["index"] == 1 and ok["status"] == protocol.PAGE_OK
    assert not any(name.endswith("0000.jpg") or name.endswith("0000.txt") for name in run.files())
    end = run.one(protocol.EVENT_END)
    assert (end["pages_ok"], end["pages_failed"]) == (1, 1)


def test_unloadable_page_fails_render_error_and_leaves_no_artifacts(box):
    run = box.process(_broken_kid_pdf())
    assert run.returncode == protocol.EXIT_OK
    _assert_well_formed(run)
    pages = {p["index"]: p for p in run.of(protocol.EVENT_PAGE)}
    assert pages[1]["status"] == protocol.PAGE_FAILED
    assert pages[1]["code"] == protocol.PAGE_FAIL_RENDER
    assert "PdfiumError" in pages[1]["detail"]  # the class name, never the message
    assert pages[0]["status"] == pages[2]["status"] == protocol.PAGE_OK
    assert not any("0001" in name for name in run.files() - {protocol.progress_file(0)})
    assert run.one(protocol.EVENT_END)["pages_failed"] == 1


# ── Hazard inventory ─────────────────────────────────────────────────────


def test_hazard_inventory_counts_document_and_page_active_content(box):
    run = box.process(_hazard_pdf())
    assert run.returncode == protocol.EXIT_OK
    document = run.one(protocol.EVENT_DOCUMENT)
    assert document["hazards"]["javascript_actions"] >= 1
    assert document["hazards"]["attachments"] == 1
    assert document["hazards"]["xfa_packets"] == 0
    assert document["form_type"] == protocol.FORM_NONE
    page = run.one(protocol.EVENT_PAGE)
    assert page["status"] == protocol.PAGE_OK  # hazards never fail a page
    assert page["hazards"] == {
        "uri_links": 1,
        "launch_actions": 1,
        "remote_goto": 1,
        "embedded_goto": 1,
        "page_actions": 2,
        "file_attachments": 1,
    }
    # Nothing from the file leaks into the log: no URI, no file name, no script.
    raw = (run.out / protocol.progress_file(0)).read_text()
    for leak in ("example.com", "calc.exe", "other.pdf", "app.alert", "evil.txt"):
        assert leak not in raw


def test_form_type_and_xfa_packets_are_reported_and_the_page_still_renders(box):
    # No form environment is ever created, so an XFA or AcroForm file is
    # inventoried and rasterized like any other; nothing in it executes.
    run = box.process(_form_pdf(xfa=True))
    document = run.one(protocol.EVENT_DOCUMENT)
    assert document["form_type"] == protocol.FORM_XFA_FOREGROUND
    assert document["hazards"]["xfa_packets"] == 3
    assert run.one(protocol.EVENT_PAGE)["status"] == protocol.PAGE_OK

    second = Harness()
    try:
        plain = second.process(_form_pdf(xfa=False))
        document = plain.one(protocol.EVENT_DOCUMENT)
        assert document["form_type"] == protocol.FORM_ACROFORM
        assert document["hazards"]["xfa_packets"] == 0
    finally:
        shutil.rmtree(second.work, ignore_errors=True)


# ── Resource behavior ────────────────────────────────────────────────────


def test_flate_bomb_under_the_parent_deadline_leaves_a_parseable_log(box):
    # 900 MB of gray from under 1 MB of file. Whatever PDFium does with it,
    # the parent must see either a clean end or a dangling page_start after
    # every complete line parsed strictly; it never sees a half-page event.
    run = box.process(_image_bomb_pdf(30000), timeout=BOOMB_TIMEOUT_SECONDS)
    for event in run.events:
        assert event["event"] in protocol.EVENTS
    assert run.events[0]["event"] == protocol.EVENT_START
    if run.killed or run.returncode != protocol.EXIT_OK:
        assert run.of(protocol.EVENT_PAGE_START), "died before the page it was working on"
        assert run.events[-1]["event"] in (protocol.EVENT_PAGE_START, protocol.EVENT_HEARTBEAT)
        return
    assert not run.torn
    _assert_well_formed(run)
    page = run.one(protocol.EVENT_PAGE)
    assert page["status"] == protocol.PAGE_OK or page["code"] in (
        protocol.PAGE_FAIL_MEMORY, protocol.PAGE_FAIL_RENDER
    )
    assert run.events[-1]["event"] == protocol.EVENT_END


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS only applies on Linux")
def test_flate_bomb_hits_the_memory_limit_on_linux(box):
    run = box.process(
        _image_bomb_pdf(30000), _limits(memory_bytes=256 * 1024 * 1024),
        timeout=BOOMB_TIMEOUT_SECONDS,
    )
    assert run.one(protocol.EVENT_START)["limits_applied"]["memory"] is True
    pages = run.of(protocol.EVENT_PAGE)
    if pages:
        assert pages[0]["status"] == protocol.PAGE_FAILED
        assert pages[0]["code"] == protocol.PAGE_FAIL_MEMORY
    else:
        # PDFium's own allocator gave up: the child died with a dangling
        # page_start, which the parent blames on memory.
        assert run.returncode != protocol.EXIT_OK
        assert run.events[-1]["event"] == protocol.EVENT_PAGE_START


def test_child_deadline_aborts_before_the_next_page(box):
    # Page 0 takes seconds of PDFium time; with a one-second budget the check
    # that runs BEFORE page 1 fires, page 1 is never started, and the parent
    # reads end.aborted = deadline (failed/resource_limit) plus one aborted page.
    run = box.process(_slow_then_blank_pdf(200_000), _limits(deadline_seconds=1), timeout=120)
    assert run.returncode == protocol.EXIT_OK and not run.torn
    _assert_well_formed(run)
    end = run.one(protocol.EVENT_END)
    assert end["aborted"] == protocol.ABORT_DEADLINE
    assert [p["index"] for p in run.of(protocol.EVENT_PAGE_START)] == [0]
    assert end["pages_ok"] + end["pages_failed"] == 1


def test_disk_quota_aborts_once_this_spawns_output_exceeds_it(box):
    # Each blank page costs about 4 KB across both tiers at the test sizes;
    # a 16 KB quota stops the loop after a few pages, well short of ten.
    limits = _limits(max_output_file_bytes=16_000, max_output_total_bytes=16_000)
    run = box.process(_blank_pdf(10), limits)
    assert run.returncode == protocol.EXIT_OK and not run.torn
    _assert_well_formed(run)
    end = run.one(protocol.EVENT_END)
    assert end["aborted"] == protocol.ABORT_DISK_QUOTA
    assert end["output_bytes"] > limits["max_output_total_bytes"]
    assert 0 < end["pages_ok"] < 10
    assert len(run.of(protocol.EVENT_PAGE_START)) == end["pages_ok"]


# ── Terminal events outlive the document close ───────────────────────────

# Closing a hostile document is PDFium work like any other: it can crash, and
# the parent kills a child that goes silent. This bootstrap turns every close
# into an immediate SIGKILL, which is the worst case the child must survive:
# the terminal event has to already be on disk. A log that ends without one
# costs the parent the whole spawn (rejected/crash_loop, no pages).
_CLOSE_CRASH_BOOTSTRAP = (
    "import sys\n"
    "root = sys.argv.pop(1)\n"
    "sys.path.insert(0, root)\n"
    "import os, signal\n"
    "import pypdfium2, pypdfium2.raw\n"
    "def _die(*args, **kwargs):\n"
    "    os.kill(os.getpid(), signal.SIGKILL)\n"
    "pypdfium2.PdfDocument.close = _die\n"
    "pypdfium2.raw.FPDF_CloseDocument = _die\n"
    "import runpy\n"
    "runpy.run_module('app.sandbox', run_name='__main__', alter_sys=True)\n"
)


def test_end_is_written_before_the_document_is_closed(box):
    run = box.process(_blank_pdf(2), bootstrap=_CLOSE_CRASH_BOOTSTRAP)
    assert run.returncode == -signal.SIGKILL and not run.killed
    assert not run.torn, "the end line must be one complete os.write"
    end = run.one(protocol.EVENT_END)
    assert (end["pages_ok"], end["pages_failed"], end["aborted"]) == (2, 0, None)
    assert [e["index"] for e in run.of(protocol.EVENT_PAGE)] == [0, 1]
    for page in run.of(protocol.EVENT_PAGE):
        assert (box.out / page["thumb"]["file"]).exists()


@pytest.mark.parametrize("pages,max_pages,code", [
    (0, 50, protocol.REJECT_NO_PAGES),
    (3, 2, protocol.REJECT_TOO_MANY_PAGES),
])
def test_an_open_phase_reject_is_written_before_the_handle_is_closed(box, pages, max_pages, code):
    pdf = _zero_pages_pdf() if pages == 0 else _blank_pdf(pages)
    run = box.process(pdf, _limits(max_pages=max_pages), bootstrap=_CLOSE_CRASH_BOOTSTRAP)
    assert run.returncode == -signal.SIGKILL and not run.torn
    assert run.one(protocol.EVENT_REJECT)["code"] == code
    assert run.of(protocol.EVENT_PAGE_START) == []


# ── Spawns, skip lists, arguments ────────────────────────────────────────


def test_skip_file_resume_processes_only_the_remaining_pages_on_spawn_one(box):
    first = box.process(_blank_pdf(3), spawn=0)
    assert first.returncode == protocol.EXIT_OK
    second = box.process(box.work / "source.pdf", spawn=1, skip=[0, 2])
    assert second.returncode == protocol.EXIT_OK and not second.torn
    _assert_well_formed(second)
    start = second.one(protocol.EVENT_START)
    assert start["spawn"] == 1 and start["skip_count"] == 2
    assert second.one(protocol.EVENT_DOCUMENT)["page_count"] == 3
    assert [p["index"] for p in second.of(protocol.EVENT_PAGE_START)] == [1]
    assert second.one(protocol.EVENT_PAGE)["status"] == protocol.PAGE_OK
    assert second.one(protocol.EVENT_END)["pages_ok"] == 1
    assert (box.out / protocol.progress_file(0)).exists()
    assert (box.out / protocol.progress_file(1)).exists()
    assert (box.out / protocol.stderr_file(1)).read_text() == ""


def test_an_existing_progress_file_for_the_spawn_exits_bad_args(box):
    (box.out / protocol.progress_file(0)).write_text("")
    run = box.process(_blank_pdf(1), spawn=0)
    assert run.returncode == protocol.EXIT_BAD_ARGS
    assert run.events == []
    assert "sandbox: bad arguments" in run.stderr
    assert (box.out / protocol.progress_file(0)).read_bytes() == b""


@pytest.mark.parametrize(
    "broken",
    [
        {k: v for k, v in _limits().items() if k != "max_pages"},      # missing key
        {**_limits(), "max_pages": "5"},                               # mistyped
        {**_limits(), "thumb_long_side": 5000},                        # thumb > full_small
        {**_limits(), "surprise": 1},                                  # unknown key
    ],
)
def test_a_bad_limits_document_exits_bad_args_without_a_progress_file(box, broken):
    path = box.write_limits(broken)
    run = box.process(_blank_pdf(1), limits_arg=str(path))
    assert run.returncode == protocol.EXIT_BAD_ARGS
    assert not (box.out / protocol.progress_file(0)).exists()
    assert "limits" in run.stderr


def test_inline_limits_json_is_accepted(box):
    run = box.process(_blank_pdf(1), limits_arg=json.dumps(_limits()))
    assert run.returncode == protocol.EXIT_OK
    assert run.one(protocol.EVENT_END)["pages_ok"] == 1


def test_an_unknown_argument_exits_bad_args(box):
    run = box.process(_blank_pdf(1), extra=("--network", "yes"))
    assert run.returncode == protocol.EXIT_BAD_ARGS
    assert run.events == []


def test_a_missing_input_file_is_a_parent_bug_not_a_verdict(box):
    # The parent wrote source.pdf; its absence must read as failed/spawn
    # (EXIT_BAD_ARGS, no progress file), never as rejected/unreadable.
    run = box.process(box.work / "never-written.pdf")
    assert run.returncode == protocol.EXIT_BAD_ARGS
    assert run.events == []
    assert not (box.out / protocol.progress_file(0)).exists()


def test_a_malformed_skip_file_exits_bad_args(box):
    path = box.write_lines("skip.txt", ["0", "zero"])
    run = box.process(_blank_pdf(2), extra=("--skip-file", str(path)))
    assert run.returncode == protocol.EXIT_BAD_ARGS
    assert not (box.out / protocol.progress_file(0)).exists()


# ── Verify mode ──────────────────────────────────────────────────────────


def test_verify_mode_redecodes_every_listed_jpeg_and_reports_dimensions(box):
    processed = box.process(_blank_pdf(2))
    pages = {p["index"]: p for p in processed.of(protocol.EVENT_PAGE)}
    names = [pages[i][tier]["file"] for i in (0, 1) for tier in ("thumb", "full")]
    run = box.verify(names)
    assert run.returncode == protocol.EXIT_OK and not run.torn
    assert [e["event"] for e in run.events] == [protocol.EVENT_VERIFIED] * 4
    for event, name in zip(run.events, names, strict=True):
        assert set(event) == {"event", "file", "w", "h", "ok"}
        assert event["file"] == name and event["ok"] is True
        index, tier = int(name[-8:-4]), name.split("/")[0]
        assert (event["w"], event["h"]) == (pages[index][tier]["w"], pages[index][tier]["h"])
    assert (box.out / protocol.VERIFY_FILE).exists()


def test_verify_mode_accepts_an_index_wider_than_the_padding_width(box):
    """PAGE_NAME_WIDTH is a minimum: page_file spells index 10000 with five
    digits, and an operator may raise max_pages above that."""
    processed = box.process(_blank_pdf(1))
    thumb = processed.one(protocol.EVENT_PAGE)["thumb"]
    data = (box.out / thumb["file"]).read_bytes()
    wide = protocol.page_file(protocol.THUMB_DIR, 10_000, "jpg")
    (box.out / wide).write_bytes(data)
    overpadded = f"{protocol.THUMB_DIR}/0{10_000:0{protocol.PAGE_NAME_WIDTH}d}.jpg"
    (box.out / overpadded).write_bytes(data)
    run = box.verify([wide, overpadded])
    assert run.returncode == protocol.EXIT_OK and not run.torn
    verdicts = {e["file"]: e for e in run.events}
    assert verdicts[wide]["ok"] is True
    assert (verdicts[wide]["w"], verdicts[wide]["h"]) == (thumb["w"], thumb["h"])
    assert verdicts[overpadded]["ok"] is False, "only the canonical spelling is accepted"


def test_verify_mode_marks_corrupt_foreign_missing_and_refused_names_not_ok(box):
    processed = box.process(_blank_pdf(2))
    good = processed.of(protocol.EVENT_PAGE)[0]["thumb"]["file"]
    corrupt = protocol.page_file(protocol.FULL_DIR, 1, "jpg")
    data = (box.out / corrupt).read_bytes()
    (box.out / corrupt).write_bytes(data[: len(data) // 2])            # torn JPEG
    foreign = protocol.page_file(protocol.THUMB_DIR, 1, "jpg")
    (box.out / foreign).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # not a JPEG
    missing = protocol.page_file(protocol.FULL_DIR, 7, "jpg")
    run = box.verify([good, corrupt, foreign, missing, "../source.pdf", "thumb/1.jpg",
                      protocol.progress_file(0)])
    assert run.returncode == protocol.EXIT_OK and not run.torn
    verdicts = {e["file"]: e["ok"] for e in run.events}
    assert verdicts[good] is True
    assert verdicts[corrupt] is False and verdicts[foreign] is False
    assert verdicts[missing] is False
    assert verdicts["../source.pdf"] is False and verdicts["thumb/1.jpg"] is False
    assert verdicts[protocol.progress_file(0)] is False
    assert len(run.events) == 7
    # A refused name is never opened: the source file is untouched and readable.
    assert (box.work / "source.pdf").read_bytes()[:5] == b"%PDF-"


def test_verify_mode_refuses_a_symlinked_artifact(box):
    processed = box.process(_blank_pdf(1))
    name = processed.one(protocol.EVENT_PAGE)["thumb"]["file"]
    target = box.out / name
    real = box.work / "elsewhere.jpg"
    real.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(real)
    run = box.verify([name])
    assert run.one(protocol.EVENT_VERIFIED)["ok"] is False


def test_verify_mode_refuses_an_existing_verify_file(box):
    box.process(_blank_pdf(1))
    (box.out / protocol.VERIFY_FILE).write_text("")
    run = box.verify([protocol.page_file(protocol.THUMB_DIR, 0, "jpg")])
    assert run.returncode == protocol.EXIT_BAD_ARGS
    assert run.events == []


# ── Import boundary ──────────────────────────────────────────────────────

_BOUNDARY_BOOTSTRAP = (
    "import sys, json\n"
    "root = sys.argv.pop(1); dump = sys.argv.pop(1)\n"
    "sys.path.insert(0, root)\n"
    "import runpy\n"
    "code = 0\n"
    "try:\n"
    "    runpy.run_module('app.sandbox', run_name='__main__', alter_sys=True)\n"
    "except SystemExit as exc:\n"
    "    code = exc.code\n"
    "with open(dump, 'w') as fh:\n"
    "    json.dump({'code': code, 'modules': sorted(sys.modules)}, fh)\n"
)


def test_child_imports_nothing_from_the_api_process(box):
    # Same interpreter flags and module entry as the real launch; only the
    # bootstrap gains a sys.modules dump after the child returns.
    source = box.write_pdf(_hazard_pdf())
    dump = box.work / "modules.json"
    cmd = box.command(
        "--spawn", "0", "--input", str(source), "--out", str(box.out),
        "--limits", str(box.write_limits(_limits())),
        bootstrap=_BOUNDARY_BOOTSTRAP, extra_bootstrap_args=(str(dump),),
    )
    code, killed, stderr = box.spawn(cmd, stderr_name=protocol.stderr_file(0), timeout=60)
    assert code == 0 and not killed, stderr
    report = json.loads(dump.read_text())
    assert report["code"] == protocol.EXIT_OK
    modules = report["modules"]
    offenders = [m for m in modules if m.startswith(FORBIDDEN_MODULE_PREFIXES)]
    assert offenders == []
    assert "app.sandbox.render" in modules and "app.sandbox.hazards" in modules
    assert "pypdfium2" in modules and "PIL.Image" in modules
    assert "app" in modules and "app.sandbox.protocol" in modules
    # The run really happened under that interpreter.
    events, _torn = _parse_log((box.out / protocol.progress_file(0)).read_bytes())
    assert events[-1]["event"] == protocol.EVENT_END


def test_sandbox_package_source_imports_only_stdlib_pypdfium2_and_pillow():
    import ast

    allowed_third_party = {"pypdfium2", "PIL"}
    for path in sorted((ROOT / "app" / "sandbox").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if name.startswith("app."):
                    assert name.startswith("app.sandbox"), f"{path.name} imports {name}"
                    continue
                if top in allowed_third_party:
                    continue
                assert top in sys.stdlib_module_names, f"{path.name} imports {name}"
