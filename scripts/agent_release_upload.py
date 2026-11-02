"""Immutable Supabase TUS upload, with protected resumable local state.

Protocol references:
https://supabase.com/docs/guides/storage/uploads/resumable-uploads
https://tus.io/protocols/resumable-upload

The caller must verify the completed object's full hash before registering a
release. State is retained until that verification; it never contains a token.
"""
from __future__ import annotations

import base64
from contextlib import nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit

import httpx

CHUNK_SIZE = 6 * 1024 * 1024
ATTEMPTS = 4
PREFIX = "/storage/v1/upload/resumable"


class UploadError(RuntimeError):
    """A redacted upload failure; keep the local resume state for recovery."""


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise UploadError(message)


def storage_endpoint(supabase_url: str) -> str:
    parsed = urlsplit(supabase_url)
    match = re.fullmatch(r"([a-z0-9]{20})\.supabase\.co", parsed.netloc)
    _require(parsed.scheme == "https" and bool(match) and parsed.path in {"", "/"}
             and not parsed.query and not parsed.fragment, "Invalid Supabase project URL.")
    return f"https://{match.group(1)}.storage.supabase.co{PREFIX}"


def checked_location(endpoint: str, location: str) -> str:
    _require(isinstance(location, str) and bool(location)
             and not any(ord(c) < 33 for c in location), "Invalid resumable upload location.")
    url = urljoin(endpoint + "/", location)
    target, expected = urlsplit(url), urlsplit(endpoint)
    decoded = unquote(target.path)
    _require(target.scheme == expected.scheme == "https" and target.netloc == expected.netloc
             and not target.query and not target.fragment
             and target.path.startswith(PREFIX + "/") and decoded.startswith(PREFIX + "/")
             and len(decoded) > len(PREFIX) + 1 and "\\" not in decoded
             and "%" not in decoded and not any(part in {".", ".."} for part in decoded.split("/")),
             "Refusing an untrusted resumable upload location.")
    return url


def _protected_open(path: Path, *, create: bool = False) -> int:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            os.close(fd)
            raise UploadError("Resume state must be a private, owner-owned regular file.")
        return fd
    except FileNotFoundError:
        raise
    except OSError:
        raise UploadError("Cannot safely access the resume state file.") from None


def _load_state(path: Path, binding: dict, endpoint: str) -> dict | None:
    try:
        fd = _protected_open(path)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            value = json.loads(stream.read(8193))
        _require(isinstance(value, dict) and set(value) == {*binding, "upload_url"}
                 and all(value.get(key) == expected for key, expected in binding.items()),
                 "Resume state does not match this project, object or signed bundle.")
        value["upload_url"] = checked_location(endpoint, value["upload_url"])
        return value
    except (ValueError, TypeError):
        raise UploadError("Resume state is invalid; it was not replaced.") from None


def _save_state(path: Path, value: dict) -> None:
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".release-upload-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise UploadError("Cannot persist upload progress safely; upload stopped.") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _metadata(bucket: str, object_name: str) -> str:
    return ",".join(key + " " + base64.b64encode(value.encode()).decode()
                    for key, value in {"bucketName": bucket, "objectName": object_name,
                                       "contentType": "application/zip"}.items())


def _metadata_values(value: str) -> dict[str, bytes]:
    result = {}
    try:
        for item in value.split(","):
            key, encoded = item.strip().split(" ", 1)
            _require(bool(key) and key not in result, "Invalid remote upload metadata.")
            result[key] = base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, TypeError):
        raise UploadError("Invalid remote upload metadata.") from None
    return result


def _number(response: httpx.Response, name: str) -> int:
    value = response.headers.get(name, "")
    _require(bool(re.fullmatch(r"[0-9]{1,20}", value)), "Upload response has an invalid offset or length.")
    return int(value)


def _version(response: httpx.Response) -> None:
    _require(response.headers.get("Tus-Resumable") == "1.0.0", "Unexpected resumable protocol response.")


def _transient(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _request(client, method: str, url: str, headers: dict, *, content: bytes = b""):
    try:
        return client.request(method, url, headers=headers, content=content, follow_redirects=False)
    except httpx.TransportError:
        return None
    except Exception as exc:
        raise UploadError(f"TUS {method} failed ({type(exc).__name__}); saved progress is retained.") from None


def _retry_request(client, method: str, url: str, headers: dict):
    for attempt in range(ATTEMPTS):
        response = _request(client, method, url, headers)
        if response is not None and not _transient(response.status_code):
            return response
        if attempt + 1 < ATTEMPTS:
            time.sleep(min(2 ** attempt, 4))
    raise UploadError(f"TUS {method} unavailable after {ATTEMPTS} attempts; rerun with the same resume state.")


def _head(client, url: str, headers: dict, size: int, metadata: str) -> int:
    response = _retry_request(client, "HEAD", url, headers)
    if response.status_code in {404, 410}:
        raise UploadError("Upload session expired or missing; verify the object before clearing resume state.")
    _require(response.status_code in {200, 204},
             f"TUS HEAD refused (HTTP {response.status_code}); progress is retained.")
    _version(response)
    _require(_number(response, "Upload-Length") == size, "Remote upload length differs from signed bundle.")
    # Metadata must match the intended object, not merely a locally saved URL.
    actual = _metadata_values(response.headers.get("Upload-Metadata", ""))
    _require(all(actual.get(k) == v for k, v in _metadata_values(metadata).items()),
             "Remote upload metadata differs from this object.")
    offset = _number(response, "Upload-Offset")
    _require(offset <= size, "Remote upload offset exceeds signed bundle size.")
    return offset


def upload_resumable(*, supabase_url: str, token: str, bucket: str, object_name: str,
                     bundle: Path, expected_sha: str, state_path: Path,
                     client: httpx.Client | None = None,
                     progress: Callable[[int, int], None] | None = None) -> None:
    """Upload at most 6 MiB per PATCH; recover ambiguous writes with HEAD.

    No overwrite, redirects, registration, key access or automatic state deletion.
    An injected client is intended only for tests; production creates a verified
    TLS client. The caller owns feature flags and release selection.
    """
    endpoint = storage_endpoint(supabase_url)
    _require(bool(token) and bool(re.fullmatch(r"[a-f0-9]{64}", expected_sha)), "Upload credentials or hash are missing.")
    _require(bool(re.fullmatch(r"[a-zA-Z0-9_-]+", bucket)) and bool(object_name)
             and not object_name.startswith("/") and "\\" not in object_name
             and not any(part in {"", ".", ".."} for part in object_name.split("/"))
             and not any(ord(c) < 32 for c in object_name), "Invalid upload object path.")
    bundle, state_path = Path(bundle), Path(state_path)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = _protected_open(state_path.with_name(state_path.name + ".lock"), create=True)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UploadError("Another publisher is using this resume state.") from None
        with bundle.open("rb") as source:
            initial = os.fstat(source.fileno())
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            _require(digest == expected_sha and initial.st_size > 0, "Local bundle does not match its signed hash.")
            binding = {"schema": 1, "endpoint": endpoint, "bucket": bucket,
                       "object_name": object_name, "sha256": expected_sha, "size": initial.st_size}
            state = _load_state(state_path, binding, endpoint)
            headers = {"Authorization": "Bearer " + token, "apikey": token,
                       "Tus-Resumable": "1.0.0", "x-upsert": "false"}
            metadata = _metadata(bucket, object_name)
            context = nullcontext(client) if client is not None else httpx.Client(
                verify=True, follow_redirects=False,
                timeout=httpx.Timeout(connect=15, read=120, write=120, pool=30))
            with context as http:
                if state is None:
                    response = _retry_request(http, "POST", endpoint, {
                        **headers, "Upload-Length": str(initial.st_size), "Upload-Metadata": metadata})
                    _require(response.status_code == 201,
                             f"TUS POST refused (HTTP {response.status_code}); existing objects were not overwritten.")
                    _version(response)
                    url = checked_location(endpoint, response.headers.get("Location", ""))
                    state = {**binding, "upload_url": url}
                    _save_state(state_path, state)
                url = checked_location(endpoint, state["upload_url"])
                offset = _head(http, url, headers, initial.st_size, metadata)
                failures = 0
                if progress:
                    progress(offset, initial.st_size)
                while offset < initial.st_size:
                    current = os.fstat(source.fileno())
                    _require((current.st_size, current.st_mtime_ns) == (initial.st_size, initial.st_mtime_ns),
                             "Bundle changed during upload; publication must stop.")
                    source.seek(offset)
                    chunk = source.read(min(CHUNK_SIZE, initial.st_size - offset))
                    _require(bool(chunk), "Bundle ended before its signed length.")
                    end = offset + len(chunk)
                    response = _request(http, "PATCH", url, {**headers,
                        "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"},
                        content=chunk)
                    if response is not None and response.status_code == 204:
                        _version(response)
                        _require(_number(response, "Upload-Offset") == end, "Upload acknowledged an unexpected offset.")
                        offset, failures = end, 0
                    elif response is None or response.status_code == 409 or _transient(response.status_code):
                        failures += 1
                        confirmed = _head(http, url, headers, initial.st_size, metadata)
                        _require(offset <= confirmed <= end, "Upload offset changed outside the pending chunk.")
                        if confirmed > offset:
                            failures = 0
                        offset = confirmed
                        if offset < initial.st_size:
                            _require(failures < ATTEMPTS, "Upload interrupted repeatedly; saved progress is available on rerun.")
                            if failures:
                                time.sleep(min(2 ** (failures - 1), 4))
                    else:
                        raise UploadError(f"TUS PATCH refused (HTTP {response.status_code}); saved progress is retained.")
                    if progress:
                        progress(offset, initial.st_size)
                _require(_head(http, url, headers, initial.st_size, metadata) == initial.st_size,
                         "Upload completion could not be confirmed.")
                source.seek(0)
                _require(hashlib.file_digest(source, "sha256").hexdigest() == expected_sha,
                         "Bundle changed during upload; verify the remote object before proceeding.")
    except OSError:
        raise UploadError("Cannot safely read bundle or resume state; upload stopped.") from None
    finally:
        os.close(lock_fd)
