"""Encrypted private browser content, owner reads and audited inspection.

Only ciphertext reaches persistence. An encrypted wrapper binds the job,
record kind and sequence as well as the vault's owner/id/version AAD.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.assistant import repo
from app.services import assistant_browser_vault as vault

TRACE_KINDS = ("step", "turn", "request", "response", "snapshot", "result", "screenshot")
KINDS = (*TRACE_KINDS, "help_request", "help_message", "help_secret")
MAX_TEXT_BYTES = 128 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 50


class BrowserContentUnavailable(ValueError):
    """A deliberately content-free public failure."""


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise BrowserContentUnavailable("Browser content is unavailable.") from None


def _purpose(kind: str) -> str:
    if kind == "help_secret":
        return "help_secret"
    return {"screenshot": "artifact", "result": "result"}.get(kind, "trace")


def pack_record(user_id: str, job_id: str, kind: str, payload: dict, *, record_id: str, seq: int,
                help_id: str | None = None, client_msg_id: str | None = None) -> dict:
    """Prepare an envelope; only a fenced RPC may persist it."""
    owner, job, identifier = _uuid(user_id), _uuid(job_id), _uuid(record_id)
    if kind not in KINDS or isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 1_000_000:
        raise BrowserContentUnavailable("Browser content is unavailable.")
    if not isinstance(payload, dict):
        raise BrowserContentUnavailable("Browser content is unavailable.")
    help_key = _uuid(help_id) if help_id is not None else None
    client_key = _uuid(client_msg_id) if client_msg_id is not None else None
    wrapper = {"v": 1, "job_id": job, "kind": kind, "seq": seq, "help_id": help_key,
               "client_msg_id": client_key, "content": payload}
    try:
        plain = json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise BrowserContentUnavailable("Browser content is unavailable.") from None
    maximum = (MAX_IMAGE_BYTES * 4 // 3 + 2048) if kind == "screenshot" else MAX_TEXT_BYTES
    if len(plain) > maximum:
        raise BrowserContentUnavailable("Browser content is unavailable.")
    return {"id": identifier, "kind": kind, "seq": seq, "help_id": help_key, "client_msg_id": client_key,
            "envelope": vault.encrypt_payload(owner, identifier, 1, _purpose(kind), plain),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()}


def unpack_record(user_id: str, job_id: str, row: dict, *, allow_secret: bool = False) -> dict:
    owner, job = _uuid(user_id), _uuid(job_id)
    kind = row.get("kind")
    if (row.get("user_id") != owner or row.get("job_id") != job or kind not in KINDS
            or (kind == "help_secret" and not allow_secret)):
        raise BrowserContentUnavailable("Browser content is unavailable.")
    try:
        plain = vault.decrypt_payload(owner, _uuid(row["id"]), 1, _purpose(kind), row["envelope"])
        maximum = (MAX_IMAGE_BYTES * 4 // 3 + 2048) if kind == "screenshot" else MAX_TEXT_BYTES
        if len(plain) > maximum:
            raise ValueError
        value = json.loads(plain)
        expected = {"v": 1, "job_id": job, "kind": kind, "seq": row["seq"],
                    "help_id": row.get("help_id"), "client_msg_id": row.get("client_msg_id")}
        if not isinstance(value, dict) or any(value.get(k) != v for k, v in expected.items()):
            raise ValueError
        if not isinstance(value.get("content"), dict):
            raise ValueError
        return value["content"]
    except Exception:
        raise BrowserContentUnavailable("Browser content is unavailable.") from None


def append_record(job: dict, kind: str, payload: dict, *, record_id: str, seq: int,
                  worker_id: str, boot_id: str, lease_epoch: int) -> dict:
    if job.get("principal_kind") != "user" or kind not in TRACE_KINDS:
        raise BrowserContentUnavailable("Browser content is unavailable.")
    from app.services.assistant_browser_runtime import sanitize

    owner, job_id = _uuid(job.get("principal_user_id")), _uuid(job.get("id"))
    if kind == "screenshot":
        # Pixel masking and login-screen refusal happen in the worker/API.
        # Text substitution inside encoded image bytes corrupts the image.
        _image_bytes(payload)
        content = payload
    else:
        content = sanitize(job, payload)
    row = pack_record(owner, job_id, kind, content, record_id=record_id, seq=seq)
    # Preserve ciphertext on replay: fresh AEAD nonces differ even when the
    # caller retries the exact same logical record. Still call the RPC so the
    # current lease is rechecked before acknowledging a replay.
    for attempt in range(2):
        existing = repo.browser_record(owner, job_id, row["id"])
        if existing:
            if existing.get("kind") != kind or existing.get("seq") != seq or unpack_record(owner, job_id, existing) != content:
                raise BrowserContentUnavailable("Browser record changed.")
            row["envelope"] = existing["envelope"]
        try:
            result = repo.browser_record_put(owner, job_id, worker_id, boot_id, lease_epoch, row)
            break
        except Exception:
            if attempt:
                raise BrowserContentUnavailable("Browser content is unavailable.") from None
    if result.get("ok") is not True or not isinstance(result.get("record"), dict):
        raise BrowserContentUnavailable("Browser content is unavailable.")
    stored = result["record"]
    return {"id": stored.get("id"), "kind": stored.get("kind"), "seq": stored.get("seq")}


def require_owned_job(user_id: str, job_id: str) -> dict:
    row = repo.browser_job(_uuid(user_id), _uuid(job_id))
    if not row or row.get("principal_kind") != "user" or row.get("principal_user_id") != user_id:
        raise BrowserContentUnavailable("Browser content is unavailable.")
    return row


def _display(row: dict, payload: dict) -> dict:
    """No executable HTML, image URLs, secret payloads or storage locators."""
    kind = row["kind"]
    if kind == "screenshot":
        text = None
    else:
        # Plain JSON is displayed as escaped text when a structured trace has
        # no summary. Never interpret worker content as a UI block or link.
        text = payload.get("text") or payload.get("summary")
        if not isinstance(text, str):
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        text = text[:MAX_TEXT_BYTES]
    return {"id": row["id"], "kind": kind, "seq": row["seq"], "created_at": row.get("created_at"), "text": text}


def _cursor(row: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps([row["created_at"], row["id"]], separators=(",", ":")).encode()).decode()


def parse_cursor(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    try:
        if len(value) > 500:
            raise ValueError
        timestamp, identifier = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.isoformat(), _uuid(identifier)
    except Exception:
        raise BrowserContentUnavailable("Browser content is unavailable.") from None


def owner_trace(user_id: str, job_id: str, *, before: str | None = None) -> dict:
    require_owned_job(user_id, job_id)
    cursor = parse_cursor(before)
    rows = repo.browser_records(user_id, job_id, kinds=TRACE_KINDS, before=cursor, limit=PAGE_SIZE + 1)
    page = rows[:PAGE_SIZE]
    items = [_display(row, unpack_record(user_id, job_id, row)) for row in page]
    return {"items": items, "next_before": _cursor(page[-1]) if len(rows) > PAGE_SIZE else None}


def screenshot(user_id: str, job_id: str, record_id: str) -> tuple[bytes, str]:
    require_owned_job(user_id, job_id)
    row = repo.browser_record(user_id, job_id, _uuid(record_id))
    if not row or row.get("kind") != "screenshot":
        raise BrowserContentUnavailable("Browser content is unavailable.")
    return _image_bytes(unpack_record(user_id, job_id, row))


def _image_bytes(payload: dict) -> tuple[bytes, str]:
    try:
        value = base64.b64decode(payload.get("data", ""), validate=True)
        mime = payload.get("mime_type")
        if not 0 < len(value) <= MAX_IMAGE_BYTES:
            raise ValueError
        if (mime == "image/png" and value.startswith(b"\x89PNG\r\n\x1a\n")) or (
                mime == "image/jpeg" and value.startswith(b"\xff\xd8\xff")):
            return value, mime
    except (ValueError, TypeError):
        pass
    raise BrowserContentUnavailable("Browser content is unavailable.")


def inspect(viewer_id: str, user_id: str, job_id: str, reason: str) -> dict:
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
        raise BrowserContentUnavailable("Browser inspection is unavailable.")
    # Authorization and append-only audit are one transaction. No decryption
    # occurs until the RPC confirms the audit write succeeded.
    result = repo.browser_inspect(user_id, viewer_id, job_id, reason.strip())
    if result.get("ok") is not True or not result.get("audit_id"):
        raise BrowserContentUnavailable("Browser inspection is unavailable.")
    rows = result.get("records")
    if not isinstance(rows, list) or len(rows) > 200:
        raise BrowserContentUnavailable("Browser inspection is unavailable.")
    items = [_display(row, unpack_record(user_id, job_id, row)) for row in rows if row.get("kind") in TRACE_KINDS]
    return {"audit_id": result["audit_id"], "items": items, "bounded": True}


def inspect_screenshot(viewer_id: str, user_id: str, job_id: str, record_id: str, reason: str) -> tuple[bytes, str]:
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
        raise BrowserContentUnavailable("Browser inspection is unavailable.")
    result = repo.browser_inspect(user_id, viewer_id, job_id, reason.strip(), kind="screenshot")
    if result.get("ok") is not True or not result.get("audit_id"):
        raise BrowserContentUnavailable("Browser inspection is unavailable.")
    row = next((r for r in result.get("records", []) if r.get("id") == record_id and r.get("kind") == "screenshot"), None)
    if not row:
        raise BrowserContentUnavailable("Browser content is unavailable.")
    return _image_bytes(unpack_record(user_id, job_id, row))


def owner_views(user_id: str, job_id: str) -> dict:
    require_owned_job(user_id, job_id)
    return {"items": repo.browser_views(user_id, job_id)}


def latest_result(user_id: str, job_id: str) -> str | None:
    require_owned_job(user_id, job_id)
    rows = repo.browser_records(user_id, job_id, kinds=("result",), before=None, limit=1)
    if not rows:
        return None
    value = unpack_record(user_id, job_id, rows[0])
    text = value.get("text") or value.get("summary")
    return text[:10_000] if isinstance(text, str) else None


def purge_expired_records(*, limit: int = 100) -> dict:
    """The SQL sweep rechecks legal holds under lock before each deletion."""
    return repo.browser_records_purge(None, limit=max(1, min(500, limit)))
