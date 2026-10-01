"""RFP Ingestion sandbox storage layer (rfp_ingest_storage) plus the two Graph
streaming helpers it pairs with (graph_email.graph_stream and
graph_inbox.download_attachment_to_file). No DB, no network, no real files
outside pytest's tmp_path.

What these tests pin:

- path builders: the exact object keys (quarantine source.pdf, the derived
  prefix, thumb/full page names via protocol.page_file, text.json,
  manifest.json, 1-based images-NNN.pdf) and their refusal of anything that
  is not a plain segment / non-negative index / positive part;
- uploads: upsert always on, three attempts with the 2 s x attempt backoff on
  httpx.TransportError only, the LAST error raised after the third failure,
  non-transport errors raised at once, upload_file reopening the file per
  attempt, upload_many attempting every item and raising the first failure in
  item order only after all have completed, unknown buckets refused before
  the SDK is touched;
- download_to_file: the REST object URL and the service headers, 1 MB
  chunked streaming into an O_EXCL file, 404 (and the legacy 400/"404" body)
  as RfpStorageNotFound, the running cap as RfpStorageTooLarge with the
  partial file removed, an existing dest refused untouched with no request
  made, a dropped connection retried, other statuses as an app-authored
  RfpStorageError;
- delete_prefix: a RECURSIVE walk over a three/four-level fake listing
  (folders = entries with a null id), pagination, batching of removes, the
  count returned, an empty prefix tolerated and a blank one refused, and the
  signed-URL memo dropping the removed paths;
- signed_url: the quarantine bucket refused with no mint, memoization per
  (bucket, path, download) with the 60 s refresh margin;
- graph_stream: Authorization + Prefer IdType="ImmutableId" on the request,
  a 302 raising (redirects are refused), a 500 raising;
- download_attachment_to_file: the $value path on the given mailbox, bytes
  written and counted, 404 and 410 as AttachmentNotStored, the cap as
  AttachmentTooLarge with the response closed and the partial file removed,
  O_EXCL refusal, mailbox required.

The token getter (graph_email._acquire_token) is monkeypatched, as the rest
of the suite does; HTTP is httpx.MockTransport behind the modules' own
client factories (rfp_ingest_storage._download_client, graph_email._graph_client).
upload_many genuinely runs a ThreadPoolExecutor: the test waits on it through
the function itself, so it stays deterministic.

The last section is the exception to "no real files": it reads the text of
migration 0121 (and the runner's own `_int` bounds) to pin the schema this
layer's paths and the runner's numbers land in, since none of that can be
exercised without a database.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.sandbox import protocol
from app.services import graph_email, graph_inbox
from app.services import rfp_ingest_storage as rs

SUPABASE_URL = "https://sb.test"
SERVICE_KEY = "svc-role-key"
MAILBOX = "pm@g3.test"


def _settings(**over):
    return Settings(
        _env_file=None,
        supabase_url=SUPABASE_URL,
        supabase_service_role_key=SERVICE_KEY,
        **over,
    )


# ── Fake storage SDK ─────────────────────────────────────────────────────


class _FakeBucketProxy:
    """The slice of storage3's SyncBucketProxy this module uses."""

    def __init__(self, store, bucket):
        self.store = store
        self.bucket = bucket

    def upload(self, path, body, options):
        self.store.upload_attempts.append((self.bucket, path))
        failures = self.store.upload_failures.get(path)
        if failures:
            exc = failures.pop(0)
            raise exc
        if hasattr(body, "read"):
            data = body.read()
            self.store.upload_body_kinds.append("file")
        else:
            data = bytes(body)
            self.store.upload_body_kinds.append("bytes")
        self.store.uploads.append((self.bucket, path, data, dict(options)))

    def list(self, path, options):
        self.store.lists.append((self.bucket, path, dict(options)))
        node = self.store.tree.get(self.bucket, {})
        for seg in path.split("/"):
            if not isinstance(node, dict) or seg not in node:
                return []
            node = node[seg]
        if not isinstance(node, dict):
            return []
        entries = []
        for name in sorted(node):
            if isinstance(node[name], dict):
                # Folder placeholder, exactly as storage-api returns it.
                entries.append({"name": name, "id": None, "metadata": None})
            else:
                entries.append({"name": name, "id": f"id-{name}", "metadata": {"size": 1}})
        start = options["offset"]
        return entries[start : start + options["limit"]]

    def remove(self, paths):
        self.store.removes.append((self.bucket, list(paths)))
        return [{"name": p} for p in paths]

    def create_signed_url(self, path, ttl, options=None):
        self.store.mints.append((self.bucket, path, ttl, dict(options or {})))
        suffix = f"&download={options['download']}" if options and options.get("download") else ""
        n = len(self.store.mints)
        return {"signedURL": f"{SUPABASE_URL}/sign/{self.bucket}/{path}?t={n}{suffix}"}


class _FakeStorage:
    def __init__(self, tree=None):
        self.tree = tree or {}
        self.uploads = []
        self.upload_attempts = []
        self.upload_failures = {}  # path -> [exceptions to raise, in order]
        self.upload_body_kinds = []
        self.lists = []
        self.removes = []
        self.mints = []

    def from_(self, bucket):
        return _FakeBucketProxy(self, bucket)


class _SB:
    def __init__(self, storage):
        self.storage = storage


def _install(monkeypatch, tree=None):
    store = _FakeStorage(tree)
    sleeps = []
    monkeypatch.setattr(rs, "get_supabase", lambda: _SB(store))
    monkeypatch.setattr(rs, "get_settings", _settings)
    monkeypatch.setattr(rs, "_sleep", lambda s: sleeps.append(s))
    rs._signed_url_cache.clear()
    return store, sleeps


@pytest.fixture(autouse=True)
def _clean_memo():
    rs._signed_url_cache.clear()
    yield
    rs._signed_url_cache.clear()


# ── Path builders ────────────────────────────────────────────────────────


def test_path_builders_produce_the_documented_keys():
    assert rs.quarantine_path("r1", "f1") == "r1/f1/source.pdf"
    assert rs.derived_prefix("r1", "f1") == "r1/f1"
    assert rs.thumb_path("r1", "f1", 7) == "r1/f1/thumb/0007.jpg"
    assert rs.full_path("r1", "f1", 0) == "r1/f1/full/0000.jpg"
    assert rs.text_path("r1", "f1") == "r1/f1/text.json"
    assert rs.manifest_path("r1", "f1") == "r1/f1/manifest.json"
    assert rs.images_pdf_path("r1", "f1", 1) == "r1/f1/images-001.pdf"
    assert rs.images_pdf_path("r1", "f1", 12) == "r1/f1/images-012.pdf"


def test_quarantine_path_takes_its_extension_from_the_source_format_only():
    # Section 2.1: the quarantine object is named by the sniffed format,
    # never by the declared filename, and only the five formats exist.
    assert rs.quarantine_path("r1", "f1", "pdf") == "r1/f1/source.pdf"
    assert rs.quarantine_path("r1", "f1", "docx") == "r1/f1/source.docx"
    assert rs.quarantine_path("r1", "f1", "xlsx") == "r1/f1/source.xlsx"
    assert rs.quarantine_path("r1", "f1", "doc") == "r1/f1/source.doc"
    assert rs.quarantine_path("r1", "f1", "xls") == "r1/f1/source.xls"
    for bad in ("pptx", "PDF", "docx/../x", "", None):
        with pytest.raises(ValueError):
            rs.quarantine_path("r1", "f1", bad)
    assert rs.converted_path("r1", "f1") == "r1/f1/converted.pdf"
    assert rs.converted_path("r1", "f1").startswith(rs.derived_prefix("r1", "f1") + "/")
    with pytest.raises(ValueError):
        rs.converted_path("r1", "../f1")


def test_source_content_types_cover_every_format():
    assert set(rs.SOURCE_CONTENT_TYPES) == set(protocol.SOURCE_FORMATS)
    assert rs.source_content_type("pdf") == "application/pdf"
    assert rs.source_content_type("xlsx").startswith("application/vnd.openxmlformats")
    assert rs.source_content_type("doc") == "application/msword"
    with pytest.raises(ValueError):
        rs.source_content_type("pptx")


def test_page_names_come_from_the_protocol_helper():
    # The runner derives the child's output names with protocol.page_file;
    # the derived object keys must agree byte for byte.
    assert rs.thumb_path("r", "f", 42).endswith(protocol.page_file(protocol.THUMB_DIR, 42, "jpg"))
    assert rs.full_path("r", "f", 42).endswith(protocol.page_file(protocol.FULL_DIR, 42, "jpg"))


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a b", "x\\y", "../r1", "r1/"])
def test_path_builders_refuse_unsafe_segments(bad):
    with pytest.raises(ValueError):
        rs.quarantine_path(bad, "f1")
    with pytest.raises(ValueError):
        rs.derived_prefix("r1", bad)


def test_path_builders_refuse_bad_indexes_and_parts():
    with pytest.raises(ValueError):
        rs.thumb_path("r1", "f1", -1)
    with pytest.raises(ValueError):
        rs.full_path("r1", "f1", True)
    with pytest.raises(ValueError):
        rs.images_pdf_path("r1", "f1", 0)


# ── Uploads ──────────────────────────────────────────────────────────────


def test_upload_bytes_always_upserts(monkeypatch):
    store, sleeps = _install(monkeypatch)
    rs.upload_bytes(rs.DERIVED_BUCKET, "r1/f1/manifest.json", b"{}", "application/json")
    assert store.uploads == [
        (rs.DERIVED_BUCKET, "r1/f1/manifest.json", b"{}",
         {"content-type": "application/json", "upsert": "true"}),
    ]
    assert sleeps == []


def test_upload_retries_transport_errors_with_backoff_then_succeeds(monkeypatch):
    store, sleeps = _install(monkeypatch)
    store.upload_failures["r1/f1/source.pdf"] = [httpx.ReadError("tls"), httpx.ConnectError("x")]
    rs.upload_bytes(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", b"%PDF-", "application/pdf")
    assert len(store.upload_attempts) == 3
    assert [u[1] for u in store.uploads] == ["r1/f1/source.pdf"]
    # 2 s x attempt: after attempt 1 and after attempt 2.
    assert sleeps == [2.0, 4.0]


def test_upload_raises_the_last_transport_error_after_three_attempts(monkeypatch):
    store, sleeps = _install(monkeypatch)
    store.upload_failures["p"] = [
        httpx.ReadError("first"), httpx.ReadError("second"), httpx.ReadError("third"),
    ]
    with pytest.raises(httpx.ReadError) as exc:
        rs.upload_bytes(rs.DERIVED_BUCKET, "p", b"x", "image/jpeg")
    assert str(exc.value) == "third"
    assert len(store.upload_attempts) == 3
    assert sleeps == [2.0, 4.0]
    assert store.uploads == []


def test_upload_does_not_retry_non_transport_errors(monkeypatch):
    # An HTTP-level failure (the SDK's StorageApiError, an InvalidKey 400) is
    # not transient; retrying it would only triple the wasted transfer.
    store, sleeps = _install(monkeypatch)
    store.upload_failures["p"] = [RuntimeError("400 InvalidKey")]
    with pytest.raises(RuntimeError):
        rs.upload_bytes(rs.DERIVED_BUCKET, "p", b"x", "image/jpeg")
    assert len(store.upload_attempts) == 1
    assert sleeps == []


def test_upload_file_streams_from_disk_and_reopens_on_retry(monkeypatch, tmp_path):
    store, sleeps = _install(monkeypatch)
    src = tmp_path / "images-001.pdf"
    src.write_bytes(b"%PDF-1.4 streamed body")
    store.upload_failures["r1/f1/images-001.pdf"] = [httpx.ReadError("drop")]
    rs.upload_file(rs.DERIVED_BUCKET, "r1/f1/images-001.pdf", src, "application/pdf")
    # The body handed to the SDK is a file object (streamed), and the retry
    # saw the whole content again (a fresh open, not a half-consumed handle).
    assert store.upload_body_kinds == ["file"]
    assert store.uploads[0][2] == b"%PDF-1.4 streamed body"
    assert store.uploads[0][3]["upsert"] == "true"
    assert sleeps == [2.0]


def test_upload_refuses_unknown_buckets_before_touching_the_sdk(monkeypatch):
    monkeypatch.setattr(
        rs, "get_supabase", lambda: pytest.fail("the SDK must not be touched for a bad bucket")
    )
    with pytest.raises(ValueError):
        rs.upload_bytes("project-files", "p", b"x", "image/jpeg")
    with pytest.raises(ValueError):
        rs.upload_many("project-files", [("p", b"x", "image/jpeg")])


def test_upload_many_attempts_every_item_and_raises_the_first_failure(monkeypatch):
    store, _ = _install(monkeypatch)
    # Items 1 and 3 fail permanently (non-transport, so no retries).
    store.upload_failures["b"] = [RuntimeError("b failed")]
    store.upload_failures["d"] = [RuntimeError("d failed")]
    items = [(p, p.encode(), "image/jpeg") for p in ("a", "b", "c", "d", "e")]
    with pytest.raises(RuntimeError) as exc:
        rs.upload_many(rs.DERIVED_BUCKET, items, max_workers=2)
    assert str(exc.value) == "b failed"  # first in item order, not completion order
    assert sorted(u[1] for u in store.uploads) == ["a", "c", "e"]
    assert sorted(p for _, p in store.upload_attempts) == ["a", "b", "c", "d", "e"]


def test_upload_many_with_no_items_is_a_no_op(monkeypatch):
    monkeypatch.setattr(rs, "get_supabase", lambda: pytest.fail("no items, no SDK"))
    rs.upload_many(rs.DERIVED_BUCKET, [])


def test_upload_many_each_item_keeps_its_own_transport_retry(monkeypatch):
    store, sleeps = _install(monkeypatch)
    store.upload_failures["a"] = [httpx.ReadError("drop")]
    rs.upload_many(rs.DERIVED_BUCKET, [("a", b"1", "image/jpeg"), ("b", b"2", "image/jpeg")])
    assert sorted(u[1] for u in store.uploads) == ["a", "b"]
    assert sleeps == [2.0]


# ── download_to_file ─────────────────────────────────────────────────────


def _mock_download(monkeypatch, handler):
    calls = []

    def _handler(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(
        rs, "_download_client",
        lambda: httpx.Client(transport=transport, follow_redirects=False),
    )
    return calls


def test_download_streams_the_object_with_service_headers(monkeypatch, tmp_path):
    _install(monkeypatch)
    body = b"%PDF-1.7 " + b"x" * 5000

    def handler(request):
        expected = f"{SUPABASE_URL}/storage/v1/object/rfp-quarantine/r1/f1/source.pdf"
        assert str(request.url) == expected
        assert request.headers["apikey"] == SERVICE_KEY
        assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
        return httpx.Response(200, content=body)

    calls = _mock_download(monkeypatch, handler)
    dest = tmp_path / "source.pdf"
    n = rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=10_000)
    assert n == len(body)
    assert dest.read_bytes() == body
    assert len(calls) == 1


def test_download_writes_in_chunks_up_to_the_cap_exactly(monkeypatch, tmp_path):
    # A body exactly at the cap is fine; the check is "over", not "at".
    _install(monkeypatch)
    body = b"y" * 4096
    _mock_download(monkeypatch, lambda request: httpx.Response(200, content=body))
    dest = tmp_path / "exact.pdf"
    n = rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=4096)
    assert n == 4096
    assert dest.read_bytes() == body


def test_download_404_is_not_found_and_leaves_no_file(monkeypatch, tmp_path):
    _install(monkeypatch)
    _mock_download(monkeypatch, lambda request: httpx.Response(404, json={"error": "not_found"}))
    dest = tmp_path / "gone.pdf"
    with pytest.raises(rs.RfpStorageNotFound):
        rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=100)
    assert not dest.exists()


def test_download_legacy_400_with_404_body_is_not_found(monkeypatch, tmp_path):
    # Older storage-api releases report a missing object as HTTP 400 with the
    # 404 in the JSON body; the SDK's download maps that too.
    _install(monkeypatch)
    _mock_download(
        monkeypatch,
        lambda request: httpx.Response(
            400, json={"statusCode": "404", "error": "not_found", "message": "Object not found"}
        ),
    )
    dest = tmp_path / "x.pdf"
    with pytest.raises(rs.RfpStorageNotFound):
        rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=100)
    assert not dest.exists()


def test_download_over_the_cap_is_too_large_and_the_partial_file_is_removed(monkeypatch, tmp_path):
    _install(monkeypatch)
    _mock_download(monkeypatch, lambda request: httpx.Response(200, content=b"z" * 3000))
    dest = tmp_path / "big.pdf"
    with pytest.raises(rs.RfpStorageTooLarge):
        rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=2999)
    assert not dest.exists()


def test_download_refuses_an_existing_dest_before_any_request(monkeypatch, tmp_path):
    _install(monkeypatch)
    calls = _mock_download(monkeypatch, lambda request: httpx.Response(200, content=b"new"))
    dest = tmp_path / "exists.pdf"
    dest.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=100)
    assert dest.read_bytes() == b"old"  # not ours, left untouched
    assert calls == []


def test_download_refuses_a_symlink_at_dest(monkeypatch, tmp_path):
    _install(monkeypatch)
    calls = _mock_download(monkeypatch, lambda request: httpx.Response(200, content=b"new"))
    target = tmp_path / "target.pdf"
    target.write_bytes(b"old")
    link = tmp_path / "link.pdf"
    os.symlink(target, link)
    with pytest.raises(FileExistsError):
        rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", link, max_bytes=100)
    assert target.read_bytes() == b"old"
    assert calls == []


def test_download_retries_a_dropped_connection_from_scratch(monkeypatch, tmp_path):
    _, sleeps = _install(monkeypatch)
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadError("mid-stream drop")
        return httpx.Response(200, content=b"ok-body")

    calls = _mock_download(monkeypatch, handler)
    dest = tmp_path / "retry.pdf"
    assert rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=100) == 7
    assert dest.read_bytes() == b"ok-body"
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_download_gives_up_after_three_dropped_connections(monkeypatch, tmp_path):
    _, sleeps = _install(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("down")

    calls = _mock_download(monkeypatch, handler)
    dest = tmp_path / "down.pdf"
    with pytest.raises(httpx.ConnectError):
        rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=100)
    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]
    assert not dest.exists()


def test_download_other_statuses_raise_an_app_authored_error(monkeypatch, tmp_path):
    _install(monkeypatch)
    _mock_download(
        monkeypatch,
        lambda request: httpx.Response(503, content=b"<html>upstream secret path /internal</html>"),
    )
    dest = tmp_path / "err.pdf"
    with pytest.raises(rs.RfpStorageError) as exc:
        rs.download_to_file(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf", dest, max_bytes=100)
    assert not isinstance(exc.value, (rs.RfpStorageNotFound, rs.RfpStorageTooLarge))
    assert "503" in str(exc.value)
    assert "internal" not in str(exc.value)  # never the response body
    assert not dest.exists()


def test_download_refuses_bad_arguments_before_any_request(monkeypatch, tmp_path):
    _install(monkeypatch)
    calls = _mock_download(monkeypatch, lambda request: httpx.Response(200, content=b"x"))
    with pytest.raises(ValueError):
        rs.download_to_file("project-files", "p", tmp_path / "a.pdf", max_bytes=10)
    with pytest.raises(ValueError):
        rs.download_to_file(rs.QUARANTINE_BUCKET, "p", tmp_path / "b.pdf", max_bytes=0)
    assert calls == []
    assert not (tmp_path / "a.pdf").exists() and not (tmp_path / "b.pdf").exists()


# ── delete_prefix ────────────────────────────────────────────────────────


def _derived_tree():
    return {
        rs.DERIVED_BUCKET: {
            "r1": {
                "f1": {
                    "thumb": {"0000.jpg": b"", "0001.jpg": b""},
                    "full": {"0000.jpg": b"", "0001.jpg": b""},
                    "text.json": b"",
                    "manifest.json": b"",
                    "images-001.pdf": b"",
                },
                "f2": {"manifest.json": b""},
            },
        },
    }


F1_OBJECTS = {
    "r1/f1/thumb/0000.jpg",
    "r1/f1/thumb/0001.jpg",
    "r1/f1/full/0000.jpg",
    "r1/f1/full/0001.jpg",
    "r1/f1/text.json",
    "r1/f1/manifest.json",
    "r1/f1/images-001.pdf",
}


def test_delete_prefix_recurses_into_folders_and_counts_objects(monkeypatch):
    store, _ = _install(monkeypatch, _derived_tree())
    removed = rs.delete_prefix(rs.DERIVED_BUCKET, "r1/f1")
    assert removed == 7
    assert len(store.removes) == 1
    bucket, paths = store.removes[0]
    assert bucket == rs.DERIVED_BUCKET
    assert set(paths) == F1_OBJECTS
    # The sibling file's objects are untouched.
    assert "r1/f2/manifest.json" not in paths
    # Folders were descended, not removed as if they were objects.
    assert "r1/f1/thumb" not in paths


def test_delete_prefix_keep_spares_named_objects_directly_under_the_prefix(monkeypatch):
    tree = _derived_tree()
    tree[rs.DERIVED_BUCKET]["r1"]["f1"][rs.CONVERTED_OBJECT] = b""
    tree[rs.DERIVED_BUCKET]["r1"]["f1"]["thumb"][rs.CONVERTED_OBJECT] = b""   # not "directly under"
    store, _ = _install(monkeypatch, tree)
    removed = rs.delete_prefix(rs.DERIVED_BUCKET, "r1/f1", keep=(rs.CONVERTED_OBJECT,))
    # Everything but r1/f1/converted.pdf goes, the look-alike under thumb/ included.
    assert removed == 8
    paths = set(store.removes[0][1])
    assert paths == F1_OBJECTS | {f"r1/f1/thumb/{rs.CONVERTED_OBJECT}"}
    assert f"r1/f1/{rs.CONVERTED_OBJECT}" not in paths


def test_delete_prefix_on_a_whole_run_ignores_keep_names_of_deeper_objects(monkeypatch):
    # delete_run and the prune sweep the RUN prefix: a keep name is relative
    # to that prefix, so a file's converted.pdf two levels down is removed.
    tree = _derived_tree()
    tree[rs.DERIVED_BUCKET]["r1"]["f1"][rs.CONVERTED_OBJECT] = b""
    store, _ = _install(monkeypatch, tree)
    assert rs.delete_prefix(rs.DERIVED_BUCKET, "r1") == 9
    assert f"r1/f1/{rs.CONVERTED_OBJECT}" in set(store.removes[0][1])
    store2, _ = _install(monkeypatch, tree)
    assert rs.delete_prefix(rs.DERIVED_BUCKET, "r1", keep=(rs.CONVERTED_OBJECT,)) == 9


def test_delete_prefix_keep_with_nothing_else_under_the_prefix_removes_nothing(monkeypatch):
    tree = {rs.DERIVED_BUCKET: {"r1": {"f1": {rs.CONVERTED_OBJECT: b""}}}}
    store, _ = _install(monkeypatch, tree)
    assert rs.delete_prefix(rs.DERIVED_BUCKET, "r1/f1", keep=(rs.CONVERTED_OBJECT,)) == 0
    assert store.removes == []


def test_delete_prefix_on_a_whole_run_reaches_four_levels(monkeypatch):
    store, _ = _install(monkeypatch, _derived_tree())
    assert rs.delete_prefix(rs.DERIVED_BUCKET, "r1") == 8
    assert set(store.removes[0][1]) == F1_OBJECTS | {"r1/f2/manifest.json"}


def test_delete_prefix_pages_through_long_listings(monkeypatch):
    store, _ = _install(monkeypatch, _derived_tree())
    monkeypatch.setattr(rs, "_LIST_PAGE", 2)
    assert rs.delete_prefix(rs.DERIVED_BUCKET, "r1/f1") == 7
    assert set(store.removes[0][1]) == F1_OBJECTS
    # r1/f1 has 5 entries -> 3 pages at 2 per page.
    assert [o["offset"] for b, p, o in store.lists if p == "r1/f1"] == [0, 2, 4]


def test_delete_prefix_removes_in_batches(monkeypatch):
    store, _ = _install(monkeypatch, _derived_tree())
    monkeypatch.setattr(rs, "_REMOVE_BATCH", 3)
    assert rs.delete_prefix(rs.DERIVED_BUCKET, "r1/f1") == 7
    assert [len(paths) for _, paths in store.removes] == [3, 3, 1]
    assert {p for _, paths in store.removes for p in paths} == F1_OBJECTS


def test_delete_prefix_tolerates_a_prefix_with_nothing_under_it(monkeypatch):
    store, _ = _install(monkeypatch, _derived_tree())
    assert rs.delete_prefix(rs.DERIVED_BUCKET, "r9/f9") == 0
    assert store.removes == []


def test_delete_prefix_refuses_a_blank_prefix(monkeypatch):
    store, _ = _install(monkeypatch, _derived_tree())
    for blank in ("", "  ", "/"):
        with pytest.raises(ValueError):
            rs.delete_prefix(rs.DERIVED_BUCKET, blank)
    assert store.lists == [] and store.removes == []


def test_delete_prefix_forgets_memoized_urls_for_removed_objects(monkeypatch):
    store, _ = _install(monkeypatch, _derived_tree())
    rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/thumb/0000.jpg")
    rs.signed_url(rs.DERIVED_BUCKET, "r1/f2/manifest.json", download="manifest.json")
    rs.delete_prefix(rs.DERIVED_BUCKET, "r1/f1")
    keys = set(rs._signed_url_cache)
    assert (rs.DERIVED_BUCKET, "r1/f1/thumb/0000.jpg", None) not in keys
    assert (rs.DERIVED_BUCKET, "r1/f2/manifest.json", "manifest.json") in keys


def test_delete_prefix_refuses_absurd_nesting(monkeypatch):
    # A listing that keeps returning folders is not our layout; stop rather
    # than recurse forever.
    deep = {}
    node = deep
    for _ in range(rs._MAX_LIST_DEPTH + 3):
        node["d"] = {}
        node = node["d"]
    store, _ = _install(monkeypatch, {rs.DERIVED_BUCKET: {"r1": deep}})
    with pytest.raises(rs.RfpStorageError):
        rs.delete_prefix(rs.DERIVED_BUCKET, "r1")
    assert store.removes == []


# ── signed_url ───────────────────────────────────────────────────────────


def test_signed_url_refuses_the_quarantine_bucket_without_minting(monkeypatch):
    store, _ = _install(monkeypatch)
    with pytest.raises(ValueError):
        rs.signed_url(rs.QUARANTINE_BUCKET, "r1/f1/source.pdf")
    with pytest.raises(ValueError):
        rs.signed_url("project-files", "anything")
    assert store.mints == []


def test_signed_url_memoizes_per_path_and_download(monkeypatch):
    store, _ = _install(monkeypatch)
    monkeypatch.setattr(rs, "_now", lambda: 1000.0)
    u1 = rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/thumb/0000.jpg")
    u2 = rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/thumb/0000.jpg")
    assert u1 == u2 and len(store.mints) == 1
    d1 = rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/thumb/0000.jpg", download="page.jpg")
    d2 = rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/thumb/0000.jpg", download="page.jpg")
    assert d1 == d2 and d1 != u1 and len(store.mints) == 2
    assert store.mints[1][3] == {"download": "page.jpg"}
    assert store.mints[0][2] == 900  # settings.signed_url_ttl_seconds default
    rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/thumb/0001.jpg")
    assert len(store.mints) == 3


def test_signed_url_remints_inside_the_refresh_margin(monkeypatch):
    store, _ = _install(monkeypatch)
    monkeypatch.setattr(rs, "_now", lambda: 1000.0)
    u1 = rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/full/0000.jpg")  # expires at 1900
    monkeypatch.setattr(rs, "_now", lambda: 1900.0 - 30)  # inside the 60 s margin
    u2 = rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/full/0000.jpg")
    assert u1 != u2 and len(store.mints) == 2


def test_signed_url_memo_is_separate_from_storage_py(monkeypatch):
    from app.services import storage

    _install(monkeypatch)
    rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/text.json")
    assert rs._signed_url_cache is not storage._signed_url_cache
    assert "r1/f1/text.json" not in storage._signed_url_cache


def test_signed_url_without_a_url_in_the_response_is_a_storage_error(monkeypatch):
    store, _ = _install(monkeypatch)
    monkeypatch.setattr(
        _FakeBucketProxy, "create_signed_url", lambda self, p, t, o=None: {"signedURL": None}
    )
    with pytest.raises(rs.RfpStorageError):
        rs.signed_url(rs.DERIVED_BUCKET, "r1/f1/text.json")
    assert rs._signed_url_cache == {}


# ── graph_stream ─────────────────────────────────────────────────────────


def _mock_graph(monkeypatch, handler):
    calls = []

    def _handler(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(graph_email, "_acquire_token", lambda: "tok")
    monkeypatch.setattr(
        graph_email, "_graph_client",
        lambda timeout: httpx.Client(transport=transport, timeout=timeout, follow_redirects=False),
    )
    return calls


def test_graph_stream_sends_the_token_and_immutable_id_preference(monkeypatch):
    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["prefer"] == 'IdType="ImmutableId"'
        assert str(request.url) == "https://graph.microsoft.com/v1.0/users/x/messages/m"
        return httpx.Response(200, content=b"abc")

    calls = _mock_graph(monkeypatch, handler)
    with graph_email.graph_stream("GET", "/users/x/messages/m") as resp:
        assert b"".join(resp.iter_bytes()) == b"abc"
    assert len(calls) == 1
    assert resp.is_closed


def test_graph_stream_appends_an_extra_prefer_value(monkeypatch):
    def handler(request):
        assert request.headers["prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'
        return httpx.Response(200, content=b"")

    _mock_graph(monkeypatch, handler)
    with graph_email.graph_stream("GET", "/x", prefer='outlook.body-content-type="text"'):
        pass


def test_graph_stream_refuses_redirects(monkeypatch):
    calls = _mock_graph(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "https://elsewhere.test/f"}),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        with graph_email.graph_stream("GET", "/users/x/messages/m/attachments/a/$value"):
            pytest.fail("a redirect must never yield a response")
    assert exc.value.response.status_code == 302
    # Exactly one request: the Location was not followed.
    assert len(calls) == 1


def test_graph_stream_raises_on_server_errors(monkeypatch):
    _mock_graph(monkeypatch, lambda request: httpx.Response(500, content=b"boom"))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        with graph_email.graph_stream("GET", "/x"):
            pytest.fail("unreachable")
    assert exc.value.response.status_code == 500


def test_graph_stream_passes_its_timeout_to_the_client(monkeypatch):
    seen = {}

    def factory(timeout):
        seen["timeout"] = timeout
        return httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"")),
            follow_redirects=False,
        )

    monkeypatch.setattr(graph_email, "_acquire_token", lambda: "tok")
    monkeypatch.setattr(graph_email, "_graph_client", factory)
    with graph_email.graph_stream("GET", "/x"):
        pass
    assert seen["timeout"] == httpx.Timeout(connect=10, read=60, write=60, pool=30)
    custom = httpx.Timeout(5.0)
    with graph_email.graph_stream("GET", "/x", timeout=custom):
        pass
    assert seen["timeout"] is custom


# ── download_attachment_to_file ──────────────────────────────────────────

VALUE_URL = f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/messages/M1/attachments/A1/$value"


def test_attachment_download_streams_the_value_endpoint_into_the_file(monkeypatch, tmp_path):
    body = b"%PDF-1.5 " + b"q" * 2500

    def handler(request):
        assert str(request.url) == VALUE_URL
        assert request.headers["prefer"] == 'IdType="ImmutableId"'
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, content=body)

    calls = _mock_graph(monkeypatch, handler)
    dest = tmp_path / "source.pdf"
    n = graph_inbox.download_attachment_to_file(
        "M1", "A1", mailbox=MAILBOX, max_bytes=10_000, dest=dest
    )
    assert n == len(body)
    assert dest.read_bytes() == body
    assert len(calls) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_attachment_download_404_and_410_are_not_stored(monkeypatch, tmp_path, status):
    _mock_graph(monkeypatch, lambda request: httpx.Response(status, json={"error": "gone"}))
    dest = tmp_path / "gone.pdf"
    with pytest.raises(graph_inbox.AttachmentNotStored):
        graph_inbox.download_attachment_to_file(
            "M1", "A1", mailbox=MAILBOX, max_bytes=100, dest=dest
        )
    assert not dest.exists()
    assert issubclass(graph_inbox.AttachmentNotStored, RuntimeError)


def test_attachment_download_over_the_cap_closes_the_response_and_removes_the_file(
    monkeypatch, tmp_path
):
    _mock_graph(monkeypatch, lambda request: httpx.Response(200, content=b"w" * 5000))
    seen = {}
    real_stream = graph_inbox.graph_stream

    @contextlib.contextmanager
    def capturing_stream(*args, **kwargs):
        with real_stream(*args, **kwargs) as resp:
            seen["resp"] = resp
            yield resp

    monkeypatch.setattr(graph_inbox, "graph_stream", capturing_stream)
    dest = tmp_path / "big.pdf"
    with pytest.raises(graph_inbox.AttachmentTooLarge):
        graph_inbox.download_attachment_to_file(
            "M1", "A1", mailbox=MAILBOX, max_bytes=4999, dest=dest
        )
    assert seen["resp"].is_closed
    assert not dest.exists()
    assert issubclass(graph_inbox.AttachmentTooLarge, RuntimeError)


def test_attachment_download_at_the_cap_exactly_is_allowed(monkeypatch, tmp_path):
    _mock_graph(monkeypatch, lambda request: httpx.Response(200, content=b"w" * 5000))
    dest = tmp_path / "exact.pdf"
    assert graph_inbox.download_attachment_to_file(
        "M1", "A1", mailbox=MAILBOX, max_bytes=5000, dest=dest
    ) == 5000


def test_attachment_download_redirect_raises_and_removes_the_file(monkeypatch, tmp_path):
    calls = _mock_graph(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "https://cdn.test/blob"}),
    )
    dest = tmp_path / "redir.pdf"
    with pytest.raises(httpx.HTTPStatusError) as exc:
        graph_inbox.download_attachment_to_file(
            "M1", "A1", mailbox=MAILBOX, max_bytes=100, dest=dest
        )
    assert exc.value.response.status_code == 302
    assert len(calls) == 1
    assert not dest.exists()


def test_attachment_download_refuses_an_existing_dest_before_any_request(monkeypatch, tmp_path):
    calls = _mock_graph(monkeypatch, lambda request: httpx.Response(200, content=b"new"))
    dest = tmp_path / "exists.pdf"
    dest.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        graph_inbox.download_attachment_to_file(
            "M1", "A1", mailbox=MAILBOX, max_bytes=100, dest=dest
        )
    assert dest.read_bytes() == b"old"
    assert calls == []


def test_attachment_download_requires_a_mailbox_and_a_positive_cap(monkeypatch, tmp_path):
    # The default ms_sender mailbox is a different mailbox: stored ids do not
    # resolve there, so the helper never falls back to it.
    calls = _mock_graph(monkeypatch, lambda request: httpx.Response(200, content=b"x"))
    with pytest.raises(ValueError):
        graph_inbox.download_attachment_to_file(
            "M1", "A1", mailbox="", max_bytes=100, dest=tmp_path / "a.pdf"
        )
    with pytest.raises(ValueError):
        graph_inbox.download_attachment_to_file(
            "M1", "A1", mailbox=MAILBOX, max_bytes=0, dest=tmp_path / "b.pdf"
        )
    assert calls == []
    assert not (tmp_path / "a.pdf").exists() and not (tmp_path / "b.pdf").exists()


def test_attachment_download_dropped_connection_propagates_and_removes_the_file(
    monkeypatch, tmp_path
):
    def handler(request):
        raise httpx.ReadError("mid-stream drop")

    _mock_graph(monkeypatch, handler)
    dest = tmp_path / "drop.pdf"
    with pytest.raises(httpx.ReadError):
        graph_inbox.download_attachment_to_file(
            "M1", "A1", mailbox=MAILBOX, max_bytes=100, dest=dest
        )
    assert not dest.exists()


# ── Schema: 0121 corrections to 0119 ─────────────────────────────────────

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase/migrations"
# 0119 creates the three tables and the two buckets; it is applied everywhere
# and is never edited, so the corrections below live in 0121.
MIGRATION_0121 = MIGRATIONS / "0121_rfp_ingest_indexes.sql"
RUNNER_SOURCE = Path(__file__).resolve().parents[1] / "app/services/rfp_sandbox_runner.py"

INT4_MAX = 2**31 - 1


def _migration_sql() -> str:
    return MIGRATION_0121.read_text(encoding="utf-8")


def _parent_bound(field: str) -> int:
    """The upper bound the parent accepts from the child for `field`, read out
    of the runner's own `_int(...)` call (read-only: this test never edits the
    runner, it only refuses to let the column be narrower than the bound)."""
    src = RUNNER_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf'_int\(\w+, "{field}", 0, ([0-9*]+)\)', src)
    assert match, f"no _int bound for {field} in rfp_sandbox_runner"
    literal = match.group(1)
    power = re.fullmatch(r"2\*\*(\d+)", literal)
    return 2 ** int(power.group(1)) if power else int(literal)


@pytest.mark.parametrize(
    ("table", "column"),
    [("rfp_ingest_pages", "render_ms"), ("rfp_ingest_files", "elapsed_ms")],
)
def test_millisecond_columns_are_widened_to_hold_what_the_parent_accepts(table, column):
    # The parent admits a duration far past the int4 ceiling, so an int4
    # column turned a fine render into a 22003 on the 500-row page upsert
    # (the file was then recorded failed/interrupted with no page rows).
    assert _parent_bound(column) > INT4_MAX
    sql = _migration_sql()
    assert re.search(
        rf"alter table {table}\s+alter column {column} type bigint;", sql
    ), f"0121 must widen {table}.{column} to bigint"
    # Guarded on the current type, so re-applying the migration is a no-op
    # rather than a second table rewrite.
    assert re.search(
        rf"column_name = '{column}' and data_type <> 'bigint'", sql
    ), f"the {column} widening must be guarded on its current type"


def test_the_unused_status_only_files_index_is_replaced_by_the_claim_path_index():
    sql = _migration_sql()
    # Nothing reads rfp_ingest_files by status without a run_id (_next_pending
    # and _run_files are both scoped to one run), so the status-only partial
    # index was write cost with no reader.
    assert "drop index if exists rfp_ingest_files_active_idx;" in sql
    assert re.search(
        r"create index if not exists rfp_ingest_files_pending_idx\s+"
        r"on rfp_ingest_files \(run_id, status, created_at\)\s+"
        r"where status in \('pending', 'running'\);",
        sql,
    )


def test_runs_indexes_cover_the_unfiltered_listing_and_the_retention_prune():
    sql = _migration_sql()
    # list_runs with no status filter orders by created_at desc, which a
    # leading-status index cannot serve.
    assert re.search(
        r"create index if not exists rfp_ingest_runs_created_idx\s+"
        r"on rfp_ingest_runs \(created_at desc\);",
        sql,
    )
    # prune_expired filters exactly the terminal statuses minus expired and
    # then orders by completed_at; the predicate must track that vocabulary.
    match = re.search(
        r"create index if not exists rfp_ingest_runs_prune_idx\s+"
        r"on rfp_ingest_runs \(completed_at\)\s+"
        r"where status in \(([^)]+)\);",
        sql,
    )
    assert match
    predicate = sorted(part.strip().strip("'") for part in match.group(1).split(","))
    assert predicate == sorted(protocol.RUN_TERMINAL_STATUSES - {protocol.RUN_EXPIRED})


def test_the_migration_is_idempotent_and_in_house_style():
    sql = _migration_sql()
    assert sql.startswith("-- 0121 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    # House rule: no em or en dashes in project files (escaped here so this
    # test file does not itself carry one).
    assert "\u2014" not in sql and "\u2013" not in sql
    for statement in re.findall(r"create index[^;]*;", sql):
        assert "if not exists" in statement
    for statement in re.findall(r"drop index[^;]*;", sql):
        assert "if exists" in statement


# ── Schema: 0125 office files ────────────────────────────────────────────

MIGRATION_0125 = MIGRATIONS / "0125_rfp_ingest_office_files.sql"


def _migration_0125() -> str:
    return MIGRATION_0125.read_text(encoding="utf-8")


def test_0125_adds_the_two_columns_with_the_format_vocabulary_of_protocol():
    sql = _migration_0125()
    assert re.search(
        r"add column if not exists source_format\s+text not null default 'pdf'", sql
    )
    assert re.search(r"add column if not exists converted_path text", sql)
    match = re.search(r"check \(source_format in \(([^)]+)\)\)", sql)
    assert match
    allowed = sorted(part.strip().strip("'") for part in match.group(1).split(","))
    assert allowed == sorted(protocol.SOURCE_FORMATS)
    # The constraint is added under an existence guard so a re-run is a no-op.
    assert "conname = 'rfp_ingest_files_source_format_check'" in sql
    assert "if not exists (" in sql


def test_0125_is_idempotent_and_in_house_style():
    sql = _migration_0125()
    assert sql.startswith("-- 0125 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "\u2014" not in sql and "\u2013" not in sql
    for statement in re.findall(r"add column[^,;]*", sql):
        assert "if not exists" in statement
