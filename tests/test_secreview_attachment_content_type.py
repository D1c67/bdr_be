"""Security review: ingested email attachments and vendor quote files must be
stored with a content type derived from the filename extension (allowlist,
octet-stream fallback), never the sender-declared Graph contentType."""

import base64
from types import SimpleNamespace

import pytest

from app.services import email_ingest, rfq_inbox


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.payload, self.op = db, table, None, "select"

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload, **k):
        self.op, self.payload = "upsert", payload
        return self

    def execute(self):
        if self.op == "select":
            return SimpleNamespace(data=[])
        rows = self.payload if isinstance(self.payload, list) else [self.payload]
        out = []
        for r in rows:
            r = dict(r)
            r.setdefault("id", f"{self.table}-{len(self.db.written) + 1}")
            self.db.written.append((self.table, r))
            out.append(r)
        return SimpleNamespace(data=out)


class _DB:
    def __init__(self):
        self.written = []

    def table(self, name):
        return _Q(self, name)


@pytest.fixture
def uploads(monkeypatch):
    calls = []

    def _upload(path, content, content_type, *a, **k):
        calls.append((path, content_type))

    monkeypatch.setattr(email_ingest.storage, "upload_file", _upload)
    monkeypatch.setattr(rfq_inbox.storage, "upload_file", _upload)
    return calls


@pytest.mark.parametrize(
    "name, declared, expected",
    [
        ("evil.html", "text/html", "application/octet-stream"),
        ("logo.svg", "image/svg+xml", "application/octet-stream"),
        ("quote.pdf", "text/html", "application/pdf"),
        ("photo.PNG", "image/png", "image/png"),
    ],
)
def test_email_attachment_stored_type_ignores_declared(monkeypatch, uploads, name, declared, expected):
    monkeypatch.setattr(
        email_ingest, "get_settings",
        lambda: SimpleNamespace(inbound_attachment_max_count=10, inbound_attachment_max_bytes=10**6),
    )
    att = {"id": "a1", "name": name, "contentType": declared,
           "contentBytes": base64.b64encode(b"<html>x</html>").decode()}
    monkeypatch.setattr(
        email_ingest.graph_inbox, "list_attachments", lambda *a, **k: ([att], [])
    )
    db = _DB()
    email_ingest._ingest_attachments(db, {"id": "e1", "graph_message_id": "g1", "mailbox": "m@x.com"})
    assert [ct for _, ct in uploads] == [expected]
    # The sender-declared value is kept only as metadata (sandbox reads it).
    (_, row), = [w for w in db.written if w[0] == "ingested_email_attachments"]
    assert row["mime_type"] == declared


def test_vendor_quote_file_stored_type_ignores_declared(monkeypatch, uploads):
    monkeypatch.setattr(rfq_inbox.office_preview, "is_convertible", lambda *a, **k: False)
    monkeypatch.setattr(rfq_inbox.storage, "build_object_path", lambda *a: "p1/quote/quote.html")
    send = {"rfqs": {"material_category_id": "mc1", "projects": {"id": "p1"}}}
    db = _DB()
    row = rfq_inbox._store_quote_file(db, send, "quote.html", b"<script>", "text/html")
    assert uploads == [("p1/quote/quote.html", "application/octet-stream")]
    assert row["mime_type"] == "application/octet-stream"

    uploads.clear()
    row = rfq_inbox._store_quote_file(db, send, "quote.pdf", b"%PDF", "text/html")
    assert uploads[0][1] == "application/pdf"
    assert row["mime_type"] == "application/pdf"
