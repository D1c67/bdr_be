"""Private browser help with encrypted messages and one-time MFA delivery."""
from __future__ import annotations

import re
import hashlib
import hmac
from datetime import datetime, timezone

from app.assistant import repo
from app.services import assistant_browser_records as records
from app.services import assistant_browser_vault as vault

REASONS = {"login_required", "mfa_required", "login_failed", "task_blocked", "read_failed",
           "mfa", "captcha", "sso", "unexpected_page", "login_locked", "logged_out", "access_denied",
           "not_found", "ambiguous", "cap_reached", "needs_info", "provider_config", "other"}
CODE = re.compile(r"^[0-9]{4,12}$")
CAPPED = "This browser task reached its limit. Stop it and start a new read."


class BrowserTaskCapped(records.BrowserContentUnavailable):
    pass


def _capped(job: dict) -> bool:
    from app.services.assistant_browser_runtime import job_capped

    return job_capped(job)


def _future(value: str | None) -> bool:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.tzinfo is not None and stamp > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def open_help(job: dict, *, reason: str, title: str, message: str, is_login_scope: bool,
              help_id: str, record_id: str, seq: int, worker_id: str, boot_id: str,
              lease_epoch: int, login_attempt_id: str | None = None) -> dict:
    if (job.get("principal_kind") != "user" or reason not in REASONS or not isinstance(title, str)
            or not 1 <= len(title) <= 160 or not isinstance(message, str) or not 1 <= len(message) <= 4000
            or is_login_scope is not bool(login_attempt_id)):
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    owner, job_id = records._uuid(job.get("principal_user_id")), records._uuid(job.get("id"))
    help_id = records._uuid(help_id)
    from app.services.assistant_browser_runtime import sanitize

    row = records.pack_record(owner, job_id, "help_request", sanitize(job, {
        "reason": reason, "title": title, "body": message, "author_kind": "worker",
    }), record_id=record_id, seq=seq, help_id=help_id)
    result = repo.browser_help_open(owner, job_id, worker_id, boot_id, lease_epoch, help_id, row, login_attempt_id)
    if result.get("ok") is not True or not isinstance(result.get("help"), dict):
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    return {"id": result["help"]["id"], "status": result["help"]["status"]}


def _read_state(user_id: str, job: dict) -> dict:
    job_id = job["id"]
    state = repo.browser_help_state(user_id, job_id)
    if not state:
        return {"id": None, "status": "none", "reason": None, "title": None, "messages": [],
                "is_login_scope": False, "secret_available": False, "retry_allowed": False}
    rows = repo.browser_help_records(user_id, job_id, state["id"])
    title, reason, messages = None, None, []
    has_secret = False
    for row in rows:
        if row.get("kind") == "help_secret":
            has_secret = True
            messages.append({"id": row["id"], "author_kind": "owner", "body": None, "is_secret": True,
                             "consumed": bool(row.get("consumed_at")), "created_at": row.get("created_at")})
            continue
        if row.get("kind") not in {"help_request", "help_message"}:
            continue
        payload = records.unpack_record(user_id, job_id, row)
        if row["kind"] == "help_request":
            title, reason = payload.get("title"), payload.get("reason")
        body = payload.get("body")
        messages.append({"id": row["id"], "author_kind": "worker" if row["kind"] == "help_request" else "owner",
                         "body": body if isinstance(body, str) else None,
                         "action": payload.get("action"), "is_secret": False, "created_at": row.get("created_at")})
    credential = repo.browser_credential_by_id(user_id, str(job.get("user_credential_id") or ""))
    secret_available = bool(state.get("status") == "open" and state.get("is_login_scope") is True
        and _future(state.get("expires_at")) and not has_secret and credential
        and credential.get("status") == "active" and credential.get("version") == job.get("user_credential_version")
        and credential.get("login_attempt_id") == state.get("login_attempt_id")
        and _future(credential.get("login_attempt_expires_at"))
        and job.get("status") in {"running", "waiting_help"})
    return {"id": state["id"], "status": state["status"], "reason": reason, "title": title,
            "messages": messages, "is_login_scope": state.get("is_login_scope") is True,
            "secret_available": secret_available,
            "retry_allowed": state.get("status") == "open" and not _capped(job)}


def owner_help(user_id: str, job_id: str) -> dict:
    return _read_state(user_id, records.require_owned_job(user_id, job_id))


def worker_state(job: dict) -> dict:
    """Caller must have just validated the current worker/boot/lease fence.

    Codes remain absent. Identity delivery must use consume_secret separately.
    """
    if job.get("principal_kind") != "user":
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    return _read_state(records._uuid(job.get("principal_user_id")), job)


def owner_reply(user_id: str, job_id: str, *, expected_help_id: str, action: str, client_msg_id: str,
                body: str | None = None, secret_value: str | None = None) -> dict:
    job = records.require_owned_job(user_id, job_id)
    help_id, client_id = records._uuid(expected_help_id), records._uuid(client_msg_id)
    state = repo.browser_help_state(user_id, job_id)
    if not state or state.get("id") != help_id:
        raise records.BrowserContentUnavailable("Browser help changed. Reload and try again.")
    if action not in {"message", "retry", "give_up"} or (body is not None and secret_value is not None):
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    # Retry and message both requeue the job without resetting its counters,
    # so a capped job could only burn attempts. Stop and private codes stay.
    if action in {"message", "retry"} and secret_value is None and _capped(job):
        raise BrowserTaskCapped(CAPPED)
    if secret_value is not None:
        if action != "message" or not isinstance(secret_value, str) or not CODE.fullmatch(secret_value):
            raise records.BrowserContentUnavailable("Browser code is unavailable.")
        kind, rpc_action, content = "help_secret", "code", {"secret": secret_value}
    else:
        if body is not None and (not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000):
            raise records.BrowserContentUnavailable("Browser message is unavailable.")
        if action == "message" and not body:
            raise records.BrowserContentUnavailable("Browser message is unavailable.")
        if action != "message" and body is not None:
            raise records.BrowserContentUnavailable("Browser message is unavailable.")
        if body:
            if state.get("is_login_scope") and re.fullmatch(r"[\s\d-]{4,64}", body):
                raise records.BrowserContentUnavailable("Use the private code field.")
            # A personal password must never become a message visible in the
            # help transcript, even though the transcript is encrypted at rest.
            credential = repo.browser_credential_by_id(user_id, str(job.get("user_credential_id") or ""))
            if credential and credential.get("status") == "active":
                password = vault.decrypt_credential(credential)
                if password and password in body:
                    raise records.BrowserContentUnavailable("Use the browser sign-in settings.")
        kind, rpc_action, content = "help_message", action, {"body": body, "action": action, "author_kind": "owner"}
        from app.services.assistant_browser_runtime import sanitize

        content = sanitize(job, content)
    row = records.pack_record(user_id, job_id, kind, content, record_id=client_id, seq=0,
                              help_id=help_id, client_msg_id=client_id)
    if secret_value is not None:
        key_id, key = vault._key()
        row["secret_hash"] = hmac.new(key, secret_value.encode(), hashlib.sha256).hexdigest()
        row["secret_key_id"] = key_id
    result = repo.browser_help_reply(user_id, job_id, help_id, client_id, rpc_action, row)
    if result.get("ok") is not True:
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    return owner_help(user_id, job_id)


def consume_secret(job: dict, *, help_id: str, record_id: str, login_attempt_id: str,
                   worker_id: str, boot_id: str, lease_epoch: int) -> str:
    if job.get("principal_kind") != "user":
        raise records.BrowserContentUnavailable("Browser code is unavailable.")
    owner, job_id = records._uuid(job.get("principal_user_id")), records._uuid(job.get("id"))
    result = repo.browser_help_consume(owner, job_id, worker_id, boot_id, lease_epoch,
                                      help_id, record_id, login_attempt_id)
    if result.get("ok") is not True or not isinstance(result.get("record"), dict):
        raise records.BrowserContentUnavailable("Browser code is unavailable.")
    value = records.unpack_record(owner, job_id, result["record"], allow_secret=True)
    code = value.get("secret")
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise records.BrowserContentUnavailable("Browser code is unavailable.")
    return code
