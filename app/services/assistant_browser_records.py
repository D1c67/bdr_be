"""Encrypted private browser content, owner reads and audited inspection.

Only ciphertext reaches persistence. An encrypted wrapper binds the job,
record kind and sequence as well as the vault's owner/id/version AAD.
"""
from __future__ import annotations

import base64
import json
import logging
import time
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
# One retention for the encrypted records (expires_at), the ended holds and the
# download metadata (docs/ASSISTANT_BROWSER_V2_PLAN.md 17.5): 90 days, with
# legal hold suspending every delete.
RETENTION_DAYS = 90
SWEEP_LIMIT = 500

logger = logging.getLogger(__name__)


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
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)).isoformat()}


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
    owner_only, no_screenshots = _owner_only_records(user_id, job_id)
    items = []
    for row in rows:
        if row.get("kind") not in TRACE_KINDS or row.get("help_id") is not None:
            continue
        if row.get("id") in owner_only or (no_screenshots and row.get("kind") == "screenshot"):
            continue
        content = unpack_record(user_id, job_id, row)
        if _is_hold_record(content):
            continue
        items.append(_display(row, content))
    return {"audit_id": result["audit_id"], "items": items, "bounded": True}


def _owner_only_records(user_id: str, job_id: str) -> tuple[set[str], bool]:
    """(ids of the records a hold points at, whether to withhold every
    screenshot of the job from an IT Admin). The SQL already skips the hold
    records; this is the second fence by id. The worker's own screenshot,
    the one a final-action hold copies, is an ordinary trace record the hold
    does not name, so a job that ever held a final action (or whose holds
    cannot be read) shows no screenshots to an IT Admin (10.7)."""
    try:
        holds = repo.browser_holds(user_id, job_id, limit=200)
    except Exception:  # noqa: BLE001 - unknown holds: withhold the screenshots, keep the text
        logger.warning("browser inspection: holds of job %s unreadable, screenshots withheld", job_id)
        return set(), True
    ids = {str(h[k]) for h in holds for k in ("summary_record_id", "screenshot_record_id") if h.get(k)}
    return ids, any(h.get("kind") == "final_action" for h in holds)


def _is_hold_record(content: dict) -> bool:
    """A hold's summary ({"hold": ...}) or screenshot copy ({"hold": true}):
    owner-only (10.7). The SQL already leaves them out; this is the second
    fence."""
    return isinstance(content, dict) and "hold" in content


def inspect_screenshot(viewer_id: str, user_id: str, job_id: str, record_id: str, reason: str) -> tuple[bytes, str]:
    if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
        raise BrowserContentUnavailable("Browser inspection is unavailable.")
    result = repo.browser_inspect(user_id, viewer_id, job_id, reason.strip(), kind="screenshot")
    if result.get("ok") is not True or not result.get("audit_id"):
        raise BrowserContentUnavailable("Browser inspection is unavailable.")
    row = next((r for r in result.get("records", []) if r.get("id") == record_id and r.get("kind") == "screenshot"
                and r.get("help_id") is None), None)
    owner_only, no_screenshots = _owner_only_records(user_id, job_id)
    if not row or no_screenshots or row.get("id") in owner_only:
        raise BrowserContentUnavailable("Browser content is unavailable.")
    content = unpack_record(user_id, job_id, row)
    if _is_hold_record(content):
        raise BrowserContentUnavailable("Browser content is unavailable.")
    return _image_bytes(content)


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
    """The SQL sweep rechecks legal holds under lock before each deletion.
    The browser v2 retention rides along and its counts come back under "v2"
    (purge_v2_retention): always while ASSISTANT_BROWSER_V2_ENABLED is on,
    and with the flag off whenever the 0192 schema answers, so turning the
    flag off as a kill switch never stops the 7 and 90 day promises. A
    database without 0192 is re-probed at most once an hour and the result
    is then unchanged."""
    result = repo.browser_records_purge(None, limit=max(1, min(500, limit)))
    if _v2_enabled():
        result = {**result, "v2": purge_v2_retention(limit=limit)}
    else:
        v2 = _v2_retention_flag_off(limit)
        if v2 is not None:
            result = {**result, "v2": v2}
    return result


# Monotonic time before which a flag-off tick skips the v2 retention: the
# last probe found no 0192 schema (or the first step failed).
V2_SCHEMA_RECHECK_SECONDS = 3600
_v2_absent_until = 0.0


def _v2_retention_flag_off(limit: int) -> dict | None:
    global _v2_absent_until
    if time.monotonic() < _v2_absent_until:
        return None
    out = purge_v2_retention(limit=limit, require_schema=True)
    if out is None:
        _v2_absent_until = time.monotonic() + V2_SCHEMA_RECHECK_SECONDS
    return out


def _v2_enabled() -> bool:
    try:
        from app.core.config import get_settings

        return bool(getattr(get_settings(), "assistant_browser_v2_enabled", False))
    except Exception:  # noqa: BLE001 - unreadable settings: the v1 sweep alone
        return False


def purge_v2_retention(*, limit: int = 100, require_schema: bool = False) -> dict | None:
    """Browser v2 retention (docs/ASSISTANT_BROWSER_V2_PLAN.md 17.5), run by
    the maintenance tick next to the records purge. In order: the SQL
    purge_v2 (expired cookie jars, live frames and input, idle live
    sessions); the objects of staged downloads past their 7 days; the ended
    holds and the download metadata past RETENTION_DAYS (the encrypted hold
    summaries and screenshots go with the records purge at the same age).
    Legal hold suspends the deletes. Never raises: a failed step is logged
    and counted under "errors", and the next tick retries it. With
    `require_schema` (the flag-off path) the first step doubles as the
    probe for 0192: when it fails nothing else runs and None comes back."""
    from app.services import assistant_staging

    size = max(1, min(SWEEP_LIMIT, int(limit)))
    out: dict[str, Any] = {"errors": 0}
    steps = (
        ("purge", lambda: repo.browser_purge_v2(None, limit=size * 10)),
        ("downloads_purged", lambda: assistant_staging.purge_browser_downloads(None, size)),
        ("holds_deleted", lambda: repo.browser_holds_retention_delete(None, days=RETENTION_DAYS, limit=size)),
        ("downloads_deleted", lambda: repo.browser_downloads_retention_delete(None, days=RETENTION_DAYS, limit=size)),
    )
    for key, step in steps:
        try:
            out[key] = step()
        except Exception:  # noqa: BLE001 - one step never stops the rest
            if require_schema and key == "purge":
                logger.info("browser v2 retention: skipped with the flag off (0192 schema did not answer)")
                return None
            logger.exception("browser v2 retention: %s failed", key)
            out["errors"] += 1
    return out
