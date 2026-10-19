"""Artifact storage for the browser agents (private bucket `browser-agent`).

Worker uploads (POST /agent-worker/v1/jobs/{id}/artifacts) are checked here
before anything is stored (SPEC.md 3.2, protocol.ARTIFACT_RULES):
  - the per-kind byte cap (read by a capped synchronous reader, so an
    oversize body never sits in memory twice);
  - sha256 recomputed and compared with what the worker declared;
  - content sniffed: screenshots by PNG/JPEG magic bytes, the two RFI
    envelopes parsed as JSON and validated against their protocol models,
    page snapshots decoded as UTF-8 text or JSON;
  - idempotent on (job_id, kind, sha256): the same bytes twice return the
    first row with existed=True.

Objects live at jobs/<job_id>/<kind>/<sha256><ext>, so a retried upload
overwrites the same object with the same bytes. Release packages live under
releases/ and are only ever handed out as 900 s signed URLs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, BinaryIO

import httpx
from pydantic import ValidationError

from app.core import supabase_client
from app.services.browser_agents import protocol

logger = logging.getLogger(__name__)

BUCKET = "browser-agent"
RELEASE_URL_TTL_SECONDS = 900
_TRANSFER_ATTEMPTS = 3
_TRANSFER_BACKOFF_S = 1.0
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/json": ".json",
    "text/plain": ".txt",
}
_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._ ()-]+")


class ArtifactRejected(ValueError):
    """The upload breaks a rule; the reason is a fixed, short text."""


class ArtifactTooLarge(ArtifactRejected):
    pass


@dataclass(frozen=True)
class CheckedArtifact:
    kind: str
    data: bytes
    sha256: str
    mime_type: str
    filename: str


def read_capped(stream: BinaryIO, max_bytes: int) -> bytes:
    """Synchronous bounded read of an upload (the plain-def twin of
    routers/files._read_capped): one read of max_bytes + 1, completed for a
    short source, refused past the cap."""
    data = stream.read(max_bytes + 1) or b""
    while len(data) <= max_bytes:
        more = stream.read(max_bytes + 1 - len(data))
        if not more:
            break
        data += more
    if len(data) > max_bytes:
        raise ArtifactTooLarge(f"too large (limit {max_bytes} bytes)")
    return data


def safe_filename(name: str) -> str:
    """The last path component, reduced to a conservative character set."""
    base = re.split(r"[\\/]", name or "")[-1].strip()
    base = _FILENAME_BAD.sub("_", base).strip(" .") or "artifact"
    return base[:200]


def sniff(kind: str, data: bytes) -> str:
    """The MIME type the bytes really are, or ArtifactRejected."""
    if kind == "screenshot":
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        raise ArtifactRejected("screenshot is not a PNG or JPEG image")
    if kind in ("rfi_detail_json", "rfi_list_json"):
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ArtifactRejected(f"{kind} is not valid JSON") from None
        model = protocol.RfiDetailEnvelope if kind == "rfi_detail_json" else protocol.RfiListEnvelope
        try:
            model.model_validate(parsed)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            where = ".".join(str(p) for p in first.get("loc", ())) or "body"
            raise ArtifactRejected(f"{kind} does not match its envelope ({where})") from None
        return "application/json"
    if kind == "page_snapshot":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ArtifactRejected("page_snapshot is not UTF-8 text") from None
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                json.loads(stripped)
                return "application/json"
            except json.JSONDecodeError:
                pass
        return "text/plain"
    raise ArtifactRejected(f"kind {kind!r} cannot be uploaded by a worker")


def check_upload(kind: str, declared_sha256: str, filename: str, stream: BinaryIO) -> CheckedArtifact:
    rules = protocol.ARTIFACT_RULES.get(kind)
    if rules is None:
        raise ArtifactRejected(f"kind {kind!r} cannot be uploaded by a worker")
    data = read_capped(stream, int(rules["max_bytes"]))
    if not data:
        raise ArtifactRejected("empty file")
    digest = hashlib.sha256(data).hexdigest()
    if digest != declared_sha256:
        raise ArtifactRejected("sha256 does not match the bytes")
    mime = sniff(kind, data)
    if mime not in rules["mime_types"]:
        raise ArtifactRejected(f"{mime} is not allowed for {kind}")
    return CheckedArtifact(kind=kind, data=data, sha256=digest, mime_type=mime,
                           filename=safe_filename(filename))


def object_path(job_id: str, kind: str, sha256: str, mime_type: str) -> str:
    return f"jobs/{job_id}/{kind}/{sha256}{_EXT_BY_MIME.get(mime_type, '.bin')}"


def upload_object(path: str, data: bytes, content_type: str) -> None:
    """Upload with the same transport retry as services/storage.upload_file
    (always upsert: the path is content-addressed, same bytes)."""
    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        try:
            supabase_client.get_supabase().storage.from_(BUCKET).upload(
                path, data, {"content-type": content_type, "upsert": "true"}
            )
            return
        except httpx.TransportError:
            if attempt == _TRANSFER_ATTEMPTS:
                raise
            logger.warning("browser-agent upload of %s dropped (attempt %d); retrying", path, attempt)
            time.sleep(_TRANSFER_BACKOFF_S * attempt)


def download_object(path: str) -> bytes:
    """Read one stored artifact back (capture validation, the apply shim,
    helper contributions), with the same transport retry as uploads."""
    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        try:
            return bytes(supabase_client.get_supabase().storage.from_(BUCKET).download(path))
        except httpx.TransportError:
            if attempt == _TRANSFER_ATTEMPTS:
                raise
            logger.warning("browser-agent download of %s dropped (attempt %d); retrying", path, attempt)
            time.sleep(_TRANSFER_BACKOFF_S * attempt)
    raise AssertionError("unreachable")


def signed_url(path: str, ttl_seconds: int = RELEASE_URL_TTL_SECONDS) -> str:
    res = supabase_client.get_supabase().storage.from_(BUCKET).create_signed_url(path, ttl_seconds)
    return res["signedURL"] if isinstance(res, dict) and "signedURL" in res else res["signedUrl"]


def find_existing(job_id: str, kind: str, sha256: str) -> dict[str, Any] | None:
    rows = (
        supabase_client.get_supabase().table("browser_agent_artifacts").select("id")
        .eq("job_id", job_id).eq("kind", kind).eq("sha256", sha256).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def store_worker_artifact(job_id: str, worker_id: str, checked: CheckedArtifact) -> tuple[str, bool]:
    """(artifact_id, existed). Idempotent on (job_id, kind, sha256)."""
    existing = find_existing(job_id, checked.kind, checked.sha256)
    if existing:
        return str(existing["id"]), True
    path = object_path(job_id, checked.kind, checked.sha256, checked.mime_type)
    upload_object(path, checked.data, checked.mime_type)
    row = {
        "job_id": job_id,
        "kind": checked.kind,
        "storage_path": path,
        "filename": checked.filename,
        "mime_type": checked.mime_type,
        "sniffed_mime": checked.mime_type,
        "size_bytes": len(checked.data),
        "sha256": checked.sha256,
        "uploaded_by_worker": worker_id,
    }
    try:
        inserted = (supabase_client.get_supabase().table("browser_agent_artifacts").insert(row).execute()).data or []
    except Exception as exc:  # noqa: BLE001 - a concurrent identical upload won the insert
        if "23505" not in str(exc) and getattr(exc, "code", None) != "23505" and "duplicate key" not in str(exc):
            raise
        inserted = []
    if inserted:
        return str(inserted[0]["id"]), False
    again = find_existing(job_id, checked.kind, checked.sha256)
    if not again:
        raise RuntimeError("artifact insert neither stored nor found")
    return str(again["id"]), True


# ── Signed releases (O3) ──────────────────────────────────────────────────────


def verify_release(row: dict[str, Any], pubkey_b64: str) -> bool:
    """True when the stored manifest matches the row and its Ed25519
    signature verifies under BROWSER_AGENT_RELEASE_PUBKEY. Without a
    configured key BDR cannot check, so it answers True and relies on the
    worker's pinned key (the worker always verifies)."""
    manifest = row.get("manifest") or {}
    try:
        parsed = protocol.ReleaseManifest.model_validate(manifest)
    except ValidationError:
        return False
    if (parsed.version, parsed.sha256, parsed.min_version) != (
        row.get("version"), row.get("sha256"), row.get("min_version")
    ):
        return False
    if not (pubkey_b64 or "").strip():
        return True
    import base64

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        text = pubkey_b64.strip()
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode((text + "=" * (-len(text) % 4)).replace("-", "+").replace("_", "/"))
        )
        signature = base64.b64decode(str(row.get("signature") or ""), validate=True)
        key.verify(signature, protocol.canonical_manifest_bytes(parsed))
        return True
    except (InvalidSignature, ValueError):
        return False
