"""Unit tests for the office → PDF preview pipeline (no DB / network)."""

import httpx
import pytest

from app.core.config import get_settings
from app.services import office_preview


@pytest.fixture(autouse=True)
def _engine_gotenberg(monkeypatch):
    """Default every test to the gotenberg engine via env (Settings is cached)."""
    monkeypatch.setattr(
        office_preview,
        "get_settings",
        lambda: get_settings().model_copy(update={"preview_engine": "gotenberg"}),
    )


def _set_engine(monkeypatch, engine: str):
    monkeypatch.setattr(
        office_preview,
        "get_settings",
        lambda: get_settings().model_copy(update={"preview_engine": engine}),
    )


# ── is_convertible ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["a.xlsx", "B.XLSM", "c.docx", "d.doc"])
def test_office_extensions_are_convertible(name):
    assert office_preview.is_convertible(name, "estimate")


@pytest.mark.parametrize("name", ["a.pdf", "b.png", "noext", None, "x.csv"])
def test_non_office_files_are_not_convertible(name):
    assert not office_preview.is_convertible(name, "estimate")


def test_drawings_never_convert_even_office_ext():
    assert not office_preview.is_convertible("plan.docx", "drawing")


def test_engine_off_disables_conversion(monkeypatch):
    _set_engine(monkeypatch, "off")
    assert not office_preview.is_convertible("a.xlsx", "estimate")
    assert office_preview.get_converter() is None


def test_preview_object_path():
    assert office_preview.preview_object_path("p1", "f1") == "p1/previews/f1.pdf"


# ── Gotenberg adapter ────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code=200, content=b"%PDF", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def test_gotenberg_spreadsheet_uses_single_page_sheets(monkeypatch):
    calls = {}

    def fake_post(url, *, files, data, timeout):
        calls.update(url=url, files=files, data=data, timeout=timeout)
        return _Resp()

    monkeypatch.setattr(office_preview.httpx, "post", fake_post)
    pdf = office_preview.GotenbergConverter().convert_to_pdf(b"x", "boq.xlsx")
    assert pdf == b"%PDF"
    assert calls["url"].endswith("/forms/libreoffice/convert")
    assert calls["files"]["files"][0] == "boq.xlsx"
    assert calls["data"] == {"singlePageSheets": "true"}


def test_gotenberg_docx_has_no_spreadsheet_flag(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        office_preview.httpx,
        "post",
        lambda url, *, files, data, timeout: calls.update(data=data) or _Resp(),
    )
    office_preview.GotenbergConverter().convert_to_pdf(b"x", "scope.docx")
    assert calls["data"] == {}


def test_gotenberg_error_raises_conversion_error(monkeypatch):
    monkeypatch.setattr(
        office_preview.httpx,
        "post",
        lambda *a, **k: _Resp(status_code=500, content=b"", text="boom"),
    )
    with pytest.raises(office_preview.ConversionError, match="500"):
        office_preview.GotenbergConverter().convert_to_pdf(b"x", "a.xlsx")


def test_gotenberg_unreachable_raises_conversion_error(monkeypatch):
    def fake_post(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(office_preview.httpx, "post", fake_post)
    with pytest.raises(office_preview.ConversionError, match="unreachable"):
        office_preview.GotenbergConverter().convert_to_pdf(b"x", "a.xlsx")


# ── Graph adapter ────────────────────────────────────────────────────────────


def test_graph_converts_and_deletes_scratch(monkeypatch):
    from app.services import graph_email

    requests = []
    monkeypatch.setattr(graph_email, "drive_upload", lambda path, content: "item-1")

    def fake_graph_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return _Resp(content=b"%PDF")

    monkeypatch.setattr(graph_email, "graph_request", fake_graph_request)
    pdf = office_preview.GraphConverter().convert_to_pdf(b"x", "boq.xlsx")
    assert pdf == b"%PDF"

    get = requests[0]
    assert get[0] == "GET"
    assert get[1].endswith("/items/item-1/content")
    assert get[2]["params"] == {"format": "pdf"}
    assert get[2]["follow_redirects"] is True
    assert requests[-1][:2] == ("DELETE", get[1].replace("/content", ""))


def test_graph_deletes_scratch_even_on_failure(monkeypatch):
    from app.services import graph_email

    requests = []
    monkeypatch.setattr(graph_email, "drive_upload", lambda path, content: "item-1")

    def fake_graph_request(method, path, **kwargs):
        requests.append((method, path))
        if method == "GET":
            raise httpx.HTTPStatusError(
                "406", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(406),
            )
        return _Resp()

    monkeypatch.setattr(graph_email, "graph_request", fake_graph_request)
    with pytest.raises(office_preview.ConversionError, match="406"):
        office_preview.GraphConverter().convert_to_pdf(b"x", "boq.xlsx")
    assert ("DELETE", "/users/bids@g3electrical.com/drive/items/item-1") in requests


def test_graph_rejects_non_office_files():
    with pytest.raises(office_preview.ConversionError, match="not an office file"):
        office_preview.GraphConverter().convert_to_pdf(b"x", "drawing.pdf")


def test_graph_scratch_path_is_sanitized(monkeypatch):
    """Untrusted filenames must not be able to break the Graph URL path."""
    from app.services import graph_email

    paths: list[str] = []
    monkeypatch.setattr(
        graph_email, "drive_upload", lambda path, content: paths.append(path) or "item-1"
    )
    monkeypatch.setattr(graph_email, "graph_request", lambda *a, **k: _Resp(content=b"%PDF"))
    office_preview.GraphConverter().convert_to_pdf(b"x", "Quote #12? (rev:3).xlsx")
    scratch = paths[0]
    assert scratch.startswith("BDR/preview-scratch/")
    name_part = scratch.rsplit("/", 1)[1]
    assert all(c.isalnum() or c in "._-" for c in name_part)


# ── generate_preview orchestration ───────────────────────────────────────────


class _StubConverter:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0

    def convert_to_pdf(self, content, filename):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise office_preview.ConversionError("flaky")
        return b"%PDF"


def _wire_generate(monkeypatch, rec, converter):
    """Stub out DB/storage around generate_preview; returns recorded marks."""
    marks = []

    class _Q:
        def select(self, *_): return self
        def eq(self, *_): return self
        def single(self): return self
        def execute(self): return type("R", (), {"data": rec})()

    class _SB:
        def table(self, _): return _Q()

    monkeypatch.setattr(office_preview, "get_supabase", lambda: _SB())
    monkeypatch.setattr(office_preview, "_mark", lambda fid, **f: marks.append(f))
    monkeypatch.setattr(office_preview, "get_converter", lambda: converter)
    monkeypatch.setattr(office_preview.storage, "download_file", lambda p: b"bytes")
    uploads = []
    monkeypatch.setattr(
        office_preview.storage,
        "upload_file",
        lambda path, content, ctype, **kw: uploads.append((path, ctype)),
    )
    monkeypatch.setattr(office_preview.time, "sleep", lambda s: None)
    return marks, uploads


REC = {
    "id": "f1",
    "project_id": "p1",
    "filename": "boq.xlsx",
    "storage_path": "p1/boq/x-boq.xlsx",
    "preview_status": "pending",
    "size_bytes": 1000,
}


def test_generate_preview_success(monkeypatch):
    conv = _StubConverter()
    marks, uploads = _wire_generate(monkeypatch, dict(REC), conv)
    office_preview.generate_preview("f1")
    assert uploads == [("p1/previews/f1.pdf", "application/pdf")]
    assert marks[-1]["preview_status"] == "ready"
    assert marks[-1]["preview_path"] == "p1/previews/f1.pdf"


def test_generate_preview_retries_once_then_succeeds(monkeypatch):
    conv = _StubConverter(fail_times=1)
    marks, _ = _wire_generate(monkeypatch, dict(REC), conv)
    office_preview.generate_preview("f1")
    assert conv.calls == 2
    assert marks[-1]["preview_status"] == "ready"


def test_generate_preview_fails_after_two_attempts(monkeypatch):
    conv = _StubConverter(fail_times=99)
    marks, uploads = _wire_generate(monkeypatch, dict(REC), conv)
    office_preview.generate_preview("f1")
    assert conv.calls == 2
    assert uploads == []
    assert marks[-1]["preview_status"] == "failed"
    assert "flaky" in marks[-1]["preview_error"]


def test_generate_preview_skips_ready_rows(monkeypatch):
    conv = _StubConverter()
    marks, _ = _wire_generate(
        monkeypatch, dict(REC, preview_status="ready"), conv
    )
    office_preview.generate_preview("f1")
    assert conv.calls == 0
    assert marks == []


def test_generate_preview_size_guard(monkeypatch):
    conv = _StubConverter()
    marks, _ = _wire_generate(
        monkeypatch, dict(REC, size_bytes=200 * 1024 * 1024), conv
    )
    office_preview.generate_preview("f1")
    assert conv.calls == 0
    assert marks[-1]["preview_status"] == "failed"
    assert "too large" in marks[-1]["preview_error"]


def test_generate_preview_engine_off_marks_none(monkeypatch):
    marks, _ = _wire_generate(monkeypatch, dict(REC), None)
    office_preview.generate_preview("f1")
    assert marks == [{"preview_status": "none"}]


def test_generate_preview_skips_upload_when_row_deleted_mid_conversion(monkeypatch):
    """A delete racing the conversion must not leave an orphan derivative."""
    conv = _StubConverter()
    marks, uploads = _wire_generate(monkeypatch, dict(REC), conv)

    class _GoneQ:
        def __init__(self):
            self._single = False

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def single(self):
            self._single = True
            return self

        def execute(self):
            # The initial .single() fetch sees the row; the post-conversion
            # existence re-check (no .single()) sees it deleted.
            return type("R", (), {"data": dict(REC) if self._single else []})()

    class _SB:
        def table(self, _):
            return _GoneQ()

    monkeypatch.setattr(office_preview, "get_supabase", lambda: _SB())
    office_preview.generate_preview("f1")
    assert conv.calls == 1
    assert uploads == []
    assert marks == []


# ── send-time conversion helpers (immutable outbound copies) ─────────────────


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Proposal 7080 - Acme.docx", "Proposal 7080 - Acme.pdf"),
        ("boq.xlsx", "boq.pdf"),
        ("a.DOCX", "a.pdf"),
        ("noext", "noext.pdf"),
        ("plan.pdf", "plan.pdf.pdf"),  # non-office: just append (drawings never reach here)
        (None, "file.pdf"),
    ],
)
def test_pdf_filename(name, expected):
    assert office_preview.pdf_filename(name) == expected


@pytest.mark.parametrize("name", ["a.xlsx", "B.XLSM", "c.docx", "d.doc"])
def test_is_office_file_true(name):
    assert office_preview.is_office_file(name)


@pytest.mark.parametrize("name", ["a.pdf", "b.png", "noext", None])
def test_is_office_file_false(name):
    assert not office_preview.is_office_file(name)


def test_is_office_file_ignores_engine_off(monkeypatch):
    """Unlike is_convertible, send paths must convert (or fail) regardless of
    the preview engine — so this is not gated on preview_engine."""
    _set_engine(monkeypatch, "off")
    assert office_preview.is_office_file("bom.xlsx")


class _NonPdfConverter:
    def __init__(self):
        self.calls = 0

    def convert_to_pdf(self, content, filename):
        self.calls += 1
        return b"<html>error</html>"


def test_convert_for_send_success(monkeypatch):
    monkeypatch.setattr(office_preview, "get_converter", lambda: _StubConverter())
    assert office_preview.convert_for_send(b"x", "a.docx") == b"%PDF"


def test_convert_for_send_engine_off_raises(monkeypatch):
    monkeypatch.setattr(office_preview, "get_converter", lambda: None)
    with pytest.raises(office_preview.ConversionError, match="disabled"):
        office_preview.convert_for_send(b"x", "a.docx")


def test_convert_for_send_oversize_raises(monkeypatch):
    conv = _StubConverter()
    monkeypatch.setattr(office_preview, "get_converter", lambda: conv)
    monkeypatch.setattr(
        office_preview,
        "get_settings",
        lambda: get_settings().model_copy(update={"preview_max_convert_mb": 0}),
    )
    with pytest.raises(office_preview.ConversionError, match="too large"):
        office_preview.convert_for_send(b"x", "a.docx")
    assert conv.calls == 0  # a doomed round-trip is skipped


def test_convert_for_send_retries_once_then_succeeds(monkeypatch):
    conv = _StubConverter(fail_times=1)
    monkeypatch.setattr(office_preview, "get_converter", lambda: conv)
    monkeypatch.setattr(office_preview.time, "sleep", lambda s: None)
    assert office_preview.convert_for_send(b"x", "a.docx") == b"%PDF"
    assert conv.calls == 2


def test_convert_for_send_raises_after_two_failures(monkeypatch):
    conv = _StubConverter(fail_times=99)
    monkeypatch.setattr(office_preview, "get_converter", lambda: conv)
    monkeypatch.setattr(office_preview.time, "sleep", lambda s: None)
    with pytest.raises(office_preview.ConversionError, match="flaky"):
        office_preview.convert_for_send(b"x", "a.docx")
    assert conv.calls == 2


def test_convert_for_send_rejects_non_pdf_output(monkeypatch):
    conv = _NonPdfConverter()
    monkeypatch.setattr(office_preview, "get_converter", lambda: conv)
    monkeypatch.setattr(office_preview.time, "sleep", lambda s: None)
    with pytest.raises(office_preview.ConversionError, match="non-PDF"):
        office_preview.convert_for_send(b"x", "a.docx")
    assert conv.calls == 2  # retried once before giving up


def test_extract_pdf_text_normalizes_whitespace(monkeypatch):
    class _Page:
        def __init__(self, t):
            self._t = t

        def extract_text(self):
            return self._t

    class _Reader:
        def __init__(self, _stream):
            self.pages = [_Page("Taylor   International\nCorp."), _Page("$82,138")]

    monkeypatch.setattr("pypdf.PdfReader", _Reader)
    assert office_preview.extract_pdf_text(b"%PDF-x") == "Taylor International Corp. $82,138"
