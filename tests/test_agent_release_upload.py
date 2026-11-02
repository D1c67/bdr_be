"""Offline TUS protocol/recovery tests with real httpx request construction."""
import base64
import fcntl
import hashlib
import json
import os
import stat

import httpx
import pytest

from scripts import agent_release_upload as upload

PROJECT = "https://bpidntbyvoooqvaispup.supabase.co"
ENDPOINT = "https://bpidntbyvoooqvaispup.storage.supabase.co/storage/v1/upload/resumable"


class Server:
    def __init__(self):
        self.data = bytearray()
        self.requests = []
        self.metadata = ""
        self.length = 0
        self.location = ENDPOINT + "/fixture"
        self.patch_hook = None
        self.head_hook = None
        self.post_hook = None

    def respond(self, status, **headers):
        return httpx.Response(status, headers={"Tus-Resumable": "1.0.0", **headers})

    def __call__(self, request):
        self.requests.append(request)
        assert request.headers["x-upsert"] == "false"
        assert request.headers["authorization"] == "Bearer secret-token"
        assert str(request.url).startswith(ENDPOINT)
        if request.method == "POST":
            assert request.content == b""  # lost creation response never loses bundle bytes
            self.metadata = request.headers["upload-metadata"]
            self.length = int(request.headers["upload-length"])
            if self.post_hook:
                response = self.post_hook(request)
                if response is not None:
                    return response
            return self.respond(201, Location=self.location)
        if request.method == "HEAD":
            if self.head_hook:
                response = self.head_hook(request)
                if response is not None:
                    return response
            return self.respond(200, **{"Upload-Offset": str(len(self.data)),
                "Upload-Length": str(self.length), "Upload-Metadata": self.metadata})
        assert request.method == "PATCH"
        assert request.headers["content-type"] == "application/offset+octet-stream"
        assert int(request.headers["upload-offset"]) == len(self.data)
        assert len(request.content) <= upload.CHUNK_SIZE
        if self.patch_hook:
            response = self.patch_hook(request)
            if response is not None:
                return response
        self.data.extend(request.content)
        return self.respond(204, **{"Upload-Offset": str(len(self.data))})


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(upload.time, "sleep", lambda _: None)

    def go(server, data=b"signed worker", **overrides):
        bundle = tmp_path / "worker.zip"
        bundle.write_bytes(data)
        args = {"supabase_url": PROJECT, "token": "secret-token", "bucket": "browser-agent",
                "object_name": "releases/1.0.2/bdr-agent-1.0.2.zip", "bundle": bundle,
                "expected_sha": hashlib.sha256(data).hexdigest(), "state_path": tmp_path / "resume.json"}
        args.update(overrides)
        with httpx.Client(transport=httpx.MockTransport(server), follow_redirects=True) as client:
            upload.upload_resumable(**args, client=client)
        return args["state_path"]
    return go


def test_chunks_state_permissions_metadata_and_completion(run):
    server = Server()
    data = b"a" * upload.CHUNK_SIZE + b"tail"
    events = []
    state_path = run(server, data, progress=lambda offset, size: events.append((offset, size)))
    assert bytes(server.data) == data
    assert [len(r.content) for r in server.requests if r.method == "PATCH"] == [upload.CHUNK_SIZE, 4]
    state = json.loads(state_path.read_text())
    assert state["size"] == len(data) and state["sha256"] == hashlib.sha256(data).hexdigest()
    assert "secret-token" not in state_path.read_text()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert events[-1] == (len(data), len(data))


def test_lost_patch_response_reconciles_without_resending(run):
    server = Server()

    def accept_then_disconnect(request):
        server.data.extend(request.content)
        raise httpx.ReadError("secret-token SSL bad record mac", request=request)

    server.patch_hook = accept_then_disconnect
    run(server)
    assert len([r for r in server.requests if r.method == "PATCH"]) == 1
    assert len([r for r in server.requests if r.method == "HEAD"]) >= 2


def test_process_rerun_resumes_after_committed_first_chunk(run):
    server = Server()
    data = b"b" * upload.CHUNK_SIZE + b"last chunk"

    def fail_after_first(request):
        if len(server.data) >= upload.CHUNK_SIZE:
            raise httpx.ReadError("secret-token", request=request)
        return None

    server.patch_hook = fail_after_first
    with pytest.raises(upload.UploadError, match="saved progress") as error:
        run(server, data)
    assert "secret-token" not in str(error.value)
    assert len(server.data) == upload.CHUNK_SIZE
    request_count = len(server.requests)
    server.patch_hook = None
    run(server, data)
    resumed = server.requests[request_count:]
    assert not any(r.method == "POST" for r in resumed)
    patches = [r for r in resumed if r.method == "PATCH"]
    assert [r.content for r in patches] == [b"last chunk"]
    assert bytes(server.data) == data


def test_partial_patch_is_resumed_from_authoritative_offset(run):
    server = Server()

    def partial(request):
        server.patch_hook = None
        server.data.extend(request.content[:3])
        raise httpx.WriteError("lost response", request=request)

    server.patch_hook = partial
    run(server)
    patches = [r for r in server.requests if r.method == "PATCH"]
    assert patches[1].headers["upload-offset"] == "3"
    assert bytes(server.data) == b"signed worker"


def test_confirmed_progress_resets_interruption_budget(run, monkeypatch):
    # Keep the simulation small; the production constant remains fixed at 6 MiB.
    monkeypatch.setattr(upload, "CHUNK_SIZE", 3)
    server = Server()

    def accept_then_disconnect(request):
        server.data.extend(request.content)
        raise httpx.ReadError("lost acknowledgement", request=request)

    server.patch_hook = accept_then_disconnect
    run(server, b"more than four chunks")
    assert bytes(server.data) == b"more than four chunks"
    assert len([r for r in server.requests if r.method == "PATCH"]) > upload.ATTEMPTS


@pytest.mark.parametrize("location", [
    "https://evil.invalid/storage/v1/upload/resumable/id",
    "http://bpidntbyvoooqvaispup.storage.supabase.co/storage/v1/upload/resumable/id",
    "https://bpidntbyvoooqvaispup.storage.supabase.co.evil.invalid/storage/v1/upload/resumable/id",
    ENDPOINT + "/%2e%2e/other", ENDPOINT + "/%252e%252e/other",
    ENDPOINT + "/id?token=secret", ENDPOINT + "/id#frag", "/auth/v1/admin/users",
])
def test_foreign_or_unsafe_location_never_receives_credentials(run, location):
    server = Server()
    server.location = location
    with pytest.raises(upload.UploadError, match="location"):
        run(server)
    assert [r.method for r in server.requests] == ["POST"]


def test_relative_location_and_normalized_metadata(run):
    server = Server()
    server.location = "/storage/v1/upload/resumable/fixture"

    def reordered(_):
        server.metadata = ", ".join(reversed(server.metadata.split(",")))
        server.head_hook = None
        return None

    server.head_hook = reordered
    run(server)
    assert server.data == b"signed worker"


def test_redirect_never_followed_even_when_client_default_follows(run):
    server = Server()
    server.post_hook = lambda _: server.respond(307, Location="https://evil.invalid/")
    with pytest.raises(upload.UploadError, match="HTTP 307"):
        run(server)
    assert len(server.requests) == 1


@pytest.mark.parametrize("field,value", [
    ("Upload-Offset", "999999"), ("Upload-Offset", "-1"), ("Upload-Length", "999999"),
    ("Upload-Metadata", "bucketName " + base64.b64encode(b"wrong-bucket").decode()),
])
def test_wrong_remote_binding_refused_before_patch(run, field, value):
    server = Server()

    def bad(_):
        headers = {"Upload-Offset": "0", "Upload-Length": str(server.length),
                   "Upload-Metadata": server.metadata, field: value}
        return server.respond(200, **headers)

    server.head_hook = bad
    with pytest.raises(upload.UploadError):
        run(server)
    assert not any(r.method == "PATCH" for r in server.requests)


def test_incorrect_patch_acknowledgement_stops(run):
    server = Server()
    server.patch_hook = lambda _: server.respond(204, **{"Upload-Offset": "1"})
    with pytest.raises(upload.UploadError, match="unexpected offset"):
        run(server)


def test_resume_state_cannot_be_reused_for_another_bundle(run):
    server = Server()
    run(server)
    count = len(server.requests)
    with pytest.raises(upload.UploadError, match="does not match"):
        run(server, b"different signed bundle")
    assert len(server.requests) == count


def test_bad_local_hash_refused_before_network(run):
    server = Server()
    with pytest.raises(upload.UploadError, match="signed hash"):
        run(server, expected_sha="0" * 64)
    assert server.requests == []


def test_expired_state_is_retained_without_new_post(run):
    server = Server()
    path = run(server)
    server.head_hook = lambda _: server.respond(410)
    count = len(server.requests)
    with pytest.raises(upload.UploadError, match="expired"):
        run(server)
    assert path.exists()
    assert [r.method for r in server.requests[count:]] == ["HEAD"]


def test_public_state_or_symlink_is_refused(run, tmp_path):
    server = Server()
    path = run(server)
    path.chmod(0o644)
    with pytest.raises(upload.UploadError, match="private"):
        run(server)
    path.unlink()
    target = tmp_path / "target"
    target.write_text("untouched")
    path.symlink_to(target)
    with pytest.raises(upload.UploadError):
        run(server)
    assert target.read_text() == "untouched"


def test_concurrent_publisher_refused(run, tmp_path):
    lock = tmp_path / "resume.json.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(upload.UploadError, match="Another publisher"):
            run(Server())
    finally:
        os.close(fd)


def test_lost_creation_retries_empty_posts_only(run):
    server = Server()

    def interrupted(request):
        server.post_hook = None
        raise httpx.ReadError("connection", request=request)

    server.post_hook = interrupted
    run(server)
    assert len([r for r in server.requests if r.method == "POST"]) == 2
    assert server.data == b"signed worker"


@pytest.mark.parametrize("status", [400, 401, 403, 307])
def test_nonretryable_patch_refused_without_leaking_body(run, status):
    server = Server()
    server.patch_hook = lambda _: httpx.Response(status, text="secret-token")
    with pytest.raises(upload.UploadError, match=f"HTTP {status}") as error:
        run(server)
    assert "secret-token" not in str(error.value)
    assert len([r for r in server.requests if r.method == "PATCH"]) == 1
