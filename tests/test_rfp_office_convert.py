"""rfp_office_convert: the office-to-PDF step of the RFP Ingestion Sandbox
(docs/RFP_INGESTION_SANDBOX.md, section 2.1) over httpx.MockTransport.

No Gotenberg, no network, no database. What is pinned:

* The request: one streamed multipart POST to `{base}/forms/libreoffice/
  convert` whose only part is named `source.<ext>` from the sniffed format
  (never the declared filename) and carries the raw bytes unchanged; no
  spreadsheet option rides along; the client never follows a redirect.
* The response: written to `dest` (O_EXCL, a symlink there is refused) in
  chunks, hashed as it lands, and bounded by `max_bytes` with a running
  count; the returned fragment has the digest, the size, the status and the
  duration; `dest` never exists after any failure.
* The three outcomes: 5xx, a timeout and a dropped connection are
  ConversionUnavailable (retryable); 4xx and an empty body are
  ConversionRejected; a body past the cap is ConversionTooLarge; each carries
  the attempt's fragment with a `reason`.
* Arguments: only an office source_format, a positive cap, distinct paths.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest

from app.sandbox import protocol
from app.services import rfp_office_convert as oc

BASE = "http://gotenberg.test:3000"


def _tiny_docx() -> bytes:
    """A real (minimal) OOXML container: the pre-conversion scan opens the
    zip, so the bytes have to be one."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("word/document.xml", "<w:document>" + "docx bytes " * 50 + "</w:document>")
    return buf.getvalue()


DOCX = _tiny_docx()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"


def _files(tmp_path: Path, data: bytes = DOCX) -> tuple[Path, Path]:
    src = tmp_path / "source.docx"
    src.write_bytes(data)
    return src, tmp_path / "source.pdf"


def _convert(src, dest, handler, **over):
    kwargs = dict(
        source_format=protocol.SOURCE_FORMAT_DOCX,
        max_bytes=1 << 20,
        timeout_seconds=30,
        base_url=BASE,
        transport=httpx.MockTransport(handler),
    )
    kwargs.update(over)
    return oc.convert_to_pdf(src, dest, **kwargs)


def _multipart_parts(request: httpx.Request) -> list[tuple[str, str, bytes]]:
    """(name, filename, body) of every part in a multipart request."""
    ctype = request.headers["content-type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1].encode()
    body = request.read()
    parts = []
    for raw in body.split(b"--" + boundary)[1:]:
        if raw.strip() == b"--":
            continue
        head, _, payload = raw.partition(b"\r\n\r\n")
        disposition = next(
            line for line in head.split(b"\r\n") if line.lower().startswith(b"content-disposition")
        ).decode()
        name = disposition.split('name="', 1)[1].split('"', 1)[0]
        filename = disposition.split('filename="', 1)[1].split('"', 1)[0]
        parts.append((name, filename, payload[: -len(b"\r\n")]))
    return parts


# ── The request ──────────────────────────────────────────────────────────


def test_success_streams_the_part_named_by_the_format_and_writes_the_pdf(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["parts"] = _multipart_parts(request)
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    src, dest = _files(tmp_path)
    info = _convert(src, dest, handler)
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/forms/libreoffice/convert"
    # One part, named source.docx from the sniffed format, the raw bytes as
    # they were. No spreadsheet or page option rides along.
    assert seen["parts"] == [("files", "source.docx", DOCX)]
    assert dest.read_bytes() == PDF
    assert src.read_bytes() == DOCX                       # the raw file is untouched
    assert info["engine"] == "gotenberg" and info["route"] == "/forms/libreoffice/convert"
    assert info["source_format"] == "docx"
    assert info["pdf_sha256"] == hashlib.sha256(PDF).hexdigest()
    assert info["pdf_bytes"] == len(PDF)
    assert info["http_status"] == 200
    assert isinstance(info["duration_ms"], int) and info["duration_ms"] >= 0
    assert "reason" not in info


@pytest.mark.parametrize("fmt", sorted(protocol.OFFICE_FORMATS))
def test_the_part_name_follows_every_office_format(tmp_path, fmt):
    names = []

    def handler(request):
        names.append(_multipart_parts(request)[0][1])
        return httpx.Response(200, content=PDF)

    src, dest = _files(tmp_path)
    _convert(src, dest, handler, source_format=fmt)
    assert names == [f"source.{fmt}"]


def test_the_base_url_trailing_slash_is_tolerated(tmp_path):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, content=PDF)

    src, dest = _files(tmp_path)
    _convert(src, dest, handler, base_url=BASE + "/")
    assert urls == [f"{BASE}/forms/libreoffice/convert"]


def test_a_redirect_is_not_followed_and_counts_as_a_refusal(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://elsewhere.test/x"})

    src, dest = _files(tmp_path)
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(src, dest, handler)
    assert calls == [f"{BASE}/forms/libreoffice/convert"]
    assert exc.value.info["http_status"] == 302 and exc.value.info["reason"] == "refused"
    assert not dest.exists()


def test_the_module_level_transport_seam_is_used_when_no_transport_is_passed(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, content=PDF)

    monkeypatch.setattr(oc, "_transport", httpx.MockTransport(handler))
    src, dest = _files(tmp_path)
    info = oc.convert_to_pdf(
        src, dest, source_format="xlsx", max_bytes=1 << 20, timeout_seconds=5, base_url=BASE
    )
    assert info["pdf_bytes"] == len(PDF) and dest.read_bytes() == PDF


def test_the_client_timeouts_come_from_the_caller(tmp_path, monkeypatch):
    built = []
    real = oc._client

    def spy(timeout_seconds, transport):
        client = real(timeout_seconds, transport)
        built.append(client.timeout)
        return client

    monkeypatch.setattr(oc, "_client", spy)
    src, dest = _files(tmp_path)
    _convert(src, dest, lambda r: httpx.Response(200, content=PDF), timeout_seconds=42)
    assert built[0].read == 42.0 and built[0].write == 42.0
    assert built[0].connect == oc._CONNECT_TIMEOUT


# ── The response ─────────────────────────────────────────────────────────


def test_the_body_is_streamed_in_chunks_under_a_running_cap(tmp_path):
    big = b"%PDF-" + bytes(range(256)) * 4096          # 1 MB + 5

    def handler(request):
        return httpx.Response(200, content=big)

    src, dest = _files(tmp_path)
    info = _convert(src, dest, handler, max_bytes=len(big))
    assert dest.read_bytes() == big and info["pdf_bytes"] == len(big)
    assert info["pdf_sha256"] == hashlib.sha256(big).hexdigest()

    (tmp_path / "two").mkdir()
    src2, dest2 = _files(tmp_path / "two")
    with pytest.raises(oc.ConversionTooLarge) as exc:
        _convert(src2, dest2, handler, max_bytes=len(big) - 1)
    assert exc.value.info["reason"] == "too_large" and exc.value.info["http_status"] == 200
    assert "pdf_sha256" not in exc.value.info
    assert not dest2.exists()                          # the partial file is removed


def test_an_empty_200_body_is_a_refusal_with_nothing_left_behind(tmp_path):
    src, dest = _files(tmp_path)
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(src, dest, lambda r: httpx.Response(200, content=b""))
    assert exc.value.info["reason"] == "empty"
    assert not dest.exists()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_unavailable_and_retryable(tmp_path, status):
    src, dest = _files(tmp_path)
    with pytest.raises(oc.ConversionUnavailable) as exc:
        _convert(src, dest, lambda r: httpx.Response(status, content=b"libreoffice failed"))
    assert exc.value.info["http_status"] == status
    assert exc.value.info["reason"] == "server_error"
    assert str(exc.value) == f"The PDF converter answered HTTP {status}."
    assert "libreoffice" not in str(exc.value)          # never the response body
    assert not dest.exists()


@pytest.mark.parametrize("status", [400, 404, 413, 415, 422])
def test_a_4xx_is_a_permanent_refusal(tmp_path, status):
    src, dest = _files(tmp_path)
    with pytest.raises(oc.ConversionRejected) as exc:
        _convert(src, dest, lambda r: httpx.Response(status, content=b"unsupported"))
    assert exc.value.info["http_status"] == status and exc.value.info["reason"] == "refused"
    assert "unsupported" not in str(exc.value)
    assert not dest.exists()


def test_a_timeout_is_unavailable(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    src, dest = _files(tmp_path)
    with pytest.raises(oc.ConversionUnavailable) as exc:
        _convert(src, dest, handler)
    assert exc.value.info["reason"] == "timeout" and exc.value.info["http_status"] is None
    assert str(exc.value) == "The PDF converter did not answer in time."
    assert not dest.exists()


def test_an_unreachable_converter_is_unavailable(tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    src, dest = _files(tmp_path)
    with pytest.raises(oc.ConversionUnavailable) as exc:
        _convert(src, dest, handler)
    assert exc.value.info["reason"] == "unreachable"
    assert not dest.exists()


def test_a_connection_dropped_mid_body_is_unavailable_and_the_partial_file_goes(tmp_path):
    def broken_body():
        yield b"%PDF-1.4 first chunk"
        raise httpx.ReadError("dropped")

    def handler(request):
        return httpx.Response(200, stream=_Gen(broken_body()))

    src, dest = _files(tmp_path)
    with pytest.raises(oc.ConversionUnavailable) as exc:
        _convert(src, dest, handler)
    assert exc.value.info["reason"] == "unreachable"
    assert not dest.exists()


class _Gen(httpx.SyncByteStream):
    def __init__(self, gen):
        self._gen = gen

    def __iter__(self):
        yield from self._gen


def test_dest_is_created_excl_and_never_through_a_symlink(tmp_path):
    src, dest = _files(tmp_path)
    dest.write_bytes(b"already here")
    with pytest.raises(FileExistsError):
        _convert(src, dest, lambda r: httpx.Response(200, content=PDF))
    assert dest.read_bytes() == b"already here"          # left untouched
    target = tmp_path / "victim.pdf"
    target.write_bytes(b"victim")
    link = tmp_path / "link.pdf"
    os.symlink(target, link)
    with pytest.raises(OSError):
        _convert(src, link, lambda r: httpx.Response(200, content=PDF))
    assert target.read_bytes() == b"victim"


# ── Arguments ────────────────────────────────────────────────────────────


def test_arguments_are_checked_before_any_request(tmp_path):
    src, dest = _files(tmp_path)

    def never(request):
        pytest.fail("no request expected")

    with pytest.raises(ValueError):
        _convert(src, dest, never, source_format="pdf")
    with pytest.raises(ValueError):
        _convert(src, dest, never, source_format="pptx")
    with pytest.raises(ValueError):
        _convert(src, dest, never, max_bytes=0)
    with pytest.raises(ValueError):
        _convert(src, dest, never, max_bytes=True)
    with pytest.raises(ValueError):
        _convert(src, src, never)
    assert not dest.exists()


def test_the_exceptions_form_one_family_with_the_fragment_attached():
    info = {"engine": "gotenberg"}
    for cls in (oc.ConversionUnavailable, oc.ConversionRejected, oc.ConversionTooLarge):
        exc = cls("x", info=info)
        assert isinstance(exc, oc.OfficeConversionError) and exc.info is info
