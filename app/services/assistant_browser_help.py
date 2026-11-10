"""Private browser help with encrypted messages and one-time MFA delivery.

Two job types (docs/ASSISTANT_BROWSER_V2_PLAN.md 8.1, 8.2, 8.4):

  user_browser_read (retired): digits-only codes, login scope only, the one
      pinned credential; SQL assistant_browser_help_open, _reply, _consume.
  user_browser (v2): alphanumeric codes (4 to 12 letters and digits after
      single spaces and hyphens are removed, at least one digit unless the
      site's code_email_regex matches), the `continue` action after a push
      approval, `mfa_step_up` bound to the job and an open help request
      instead of a login attempt, the credential named by id; SQL
      assistant_browser_help_open_v2, _reply_v2, _consume_v2. Every owner
      text reply goes through assistant_browser_secrets.guard_help_text
      (any saved password of the user, any code-shaped token while an mfa
      or mfa_step_up help is open), which raises SecretInMessage.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.assistant import repo
from app.services import assistant_browser_records as records
from app.services import assistant_browser_vault as vault
from app.services.assistant_browser_redaction import code_digest

REASONS = {"login_required", "mfa_required", "login_failed", "task_blocked", "read_failed",
           "mfa", "captcha", "sso", "unexpected_page", "login_locked", "logged_out", "access_denied",
           "not_found", "ambiguous", "cap_reached", "needs_info", "provider_config", "other"}
# v2 (8.1): letters and digits, 4 to 12, after separators are removed.
CODE = re.compile(r"^[A-Za-z0-9]{4,12}$")
# The retired read type keeps its digits-only codes.
V1_CODE = re.compile(r"^[0-9]{4,12}$")
_HAS_DIGIT = re.compile(r"[0-9]")
_CODE_SEPARATORS = re.compile(r"[ \-]")
V2_TYPE = "user_browser"
CODE_REASONS = ("mfa", "mfa_step_up")
CONTINUE_REASONS = ("push", "mfa", "mfa_step_up")
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


def is_v2(job: dict) -> bool:
    return isinstance(job, dict) and job.get("agent_type") == V2_TYPE


# v2 help titles: fixed text per reason; the worker's own message is the body.
V2_TITLES = {
    "mfa": "Enter the sign-in code", "mfa_step_up": "The site asks for a code", "push": "Approve the sign-in on your phone",
    "needs_credential": "Sign-in needed", "needs_site": "Another site is needed", "sso": "This site uses Microsoft or Google sign-in",
    "captcha": "The site shows a challenge", "login_failed": "Sign-in did not work", "login_locked": "Sign-in is locked",
}


def open_help_v2(job: dict, *, reason: str, message: str | None, help_id: str, record_id: str,
                 worker_id: str, boot_id: str, lease_epoch: int, credential_id: str | None = None,
                 login_attempt_id: str | None = None) -> dict:
    """POST /user/help for a v2 job (UserHelpOpenV2Request). `hold` is the
    server's own reason (hold_open opens that row) and is refused here. A
    credential named here must be in the job's credential set. Returns the
    open help row: {"id", "status", "created"}; `created` False means the
    job already had an open help and the worker joins it."""
    if not is_v2(job) or job.get("principal_kind") != "user" or reason == "hold":
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    if reason in ("mfa", "push") and not login_attempt_id:
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    if reason == "mfa_step_up" and not credential_id:
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    if credential_id is not None:
        vault.job_credential(job, str(credential_id))
    owner, job_id = records._uuid(job.get("principal_user_id")), records._uuid(job.get("id"))
    help_id = records._uuid(help_id)
    body = message if isinstance(message, str) and message.strip() else "Please review your browser task."
    from app.services.assistant_browser_runtime import sanitize

    row = records.pack_record(owner, job_id, "help_request", sanitize(job, {
        "reason": reason, "title": V2_TITLES.get(reason, "Your browser task needs help"), "body": body[:4000],
        "author_kind": "worker",
    }), record_id=record_id, seq=0, help_id=help_id)
    result = repo.browser_rpc(owner, "assistant_browser_help_open_v2", {
        "p_job": job_id, "p_worker": str(worker_id), "p_boot": str(boot_id), "p_epoch": int(lease_epoch),
        "p_help_id": help_id, "p_record": row, "p_reason_code": reason,
        "p_credential_id": str(credential_id) if credential_id else None,
        "p_login_attempt_id": str(login_attempt_id) if login_attempt_id else None,
    })
    if result.get("ok") is not True or not isinstance(result.get("help"), dict):
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    return {"id": result["help"]["id"], "status": result["help"]["status"], "created": result.get("created") is True}


def _v2_secret_available(user_id: str, job: dict, state: dict, has_secret: bool) -> bool:
    """A code may be sent (8.1): an open mfa or mfa_step_up help that has
    no code yet. Login scope also needs the credential's attempt to still
    be the help's attempt; step-up is bound to the job and the help."""
    if (state.get("status") != "open" or not _future(state.get("expires_at")) or has_secret
            or state.get("reason_code") not in CODE_REASONS or job.get("status") not in {"running", "waiting_help"}):
        return False
    if not state.get("is_login_scope"):
        return state.get("reason_code") == "mfa_step_up"
    credential = repo.browser_credential_by_id(user_id, str(state.get("credential_id") or ""))
    return bool(credential and credential.get("status") == "active"
                and credential.get("login_attempt_id") == state.get("login_attempt_id")
                and _future(credential.get("login_attempt_expires_at")))


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
    if is_v2(job):
        reason = state.get("reason_code") or reason
        secret_available = _v2_secret_available(user_id, job, state, has_secret)
    else:
        credential = repo.browser_credential_by_id(user_id, str(job.get("user_credential_id") or ""))
        secret_available = bool(state.get("status") == "open" and state.get("is_login_scope") is True
            and _future(state.get("expires_at")) and not has_secret and credential
            and credential.get("status") == "active" and credential.get("version") == job.get("user_credential_version")
            and credential.get("login_attempt_id") == state.get("login_attempt_id")
            and _future(credential.get("login_attempt_expires_at"))
            and job.get("status") in {"running", "waiting_help"})
    out = {"id": state["id"], "status": state["status"], "reason": reason, "title": title,
           "messages": messages, "is_login_scope": state.get("is_login_scope") is True,
           "secret_available": secret_available,
           "retry_allowed": state.get("status") == "open" and not _capped(job)}
    if is_v2(job):
        out["continue_allowed"] = bool(state.get("status") == "open" and state.get("reason_code") in CONTINUE_REASONS)
    return out


def owner_help(user_id: str, job_id: str) -> dict:
    return _read_state(user_id, records.require_owned_job(user_id, job_id))


def worker_state(job: dict) -> dict:
    """Caller must have just validated the current worker/boot/lease fence.

    Codes remain absent. Identity delivery must use consume_secret separately.
    """
    if job.get("principal_kind") != "user":
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    return _read_state(records._uuid(job.get("principal_user_id")), job)


def _site_code_regex(user_id: str, job: dict, state: dict) -> re.Pattern[str] | None:
    """The site's code_email_regex for the help's credential (or the task's
    start site), compiled, or None."""
    site_key = None
    if state.get("credential_id"):
        credential = repo.browser_credential_by_id(user_id, str(state["credential_id"]))
        site_key = (credential or {}).get("site_key")
    site_key = site_key or (job.get("input") or {}).get("start_site_key")
    if not isinstance(site_key, str) or not site_key:
        return None
    try:
        value = (repo.browser_site(user_id, site_key) or {}).get("code_email_regex")
        return re.compile(value) if isinstance(value, str) and 0 < len(value) <= 500 else None
    except (re.error, TypeError, ValueError):
        return None


def code_value(value: str, *, site_regex: re.Pattern[str] | None = None) -> str | None:
    """The code as it is typed into the site (single spaces and hyphens
    removed, letter case kept), or None when it is not code-shaped (8.1)."""
    if not isinstance(value, str) or len(value) > 64 or value != value.strip() or "  " in value:
        return None
    code = _CODE_SEPARATORS.sub("", value)
    if not CODE.fullmatch(code):
        return None
    if not _HAS_DIGIT.search(code) and not (site_regex is not None and site_regex.fullmatch(code)):
        return None
    return code


def _owner_reply_v2(user_id: str, job: dict, state: dict, *, help_id: str, client_id: str, action: str,
                    body: str | None, secret_value: str | None) -> dict:
    from app.services import assistant_browser_secrets as secrets
    from app.services.assistant_browser_runtime import sanitize

    job_id = str(job["id"])
    if action not in {"message", "retry", "give_up", "continue"} or (body is not None and secret_value is not None):
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    if action in {"message", "retry", "continue"} and secret_value is None and _capped(job):
        raise BrowserTaskCapped(CAPPED)
    if secret_value is not None:
        code = code_value(secret_value, site_regex=_site_code_regex(user_id, job, state)) if action == "message" else None
        if code is None or state.get("reason_code") not in CODE_REASONS:
            raise records.BrowserContentUnavailable("Browser code is unavailable.")
        kind, rpc_action, content = "help_secret", "code", {"secret": code}
    else:
        if action == "continue" and (body is not None or state.get("reason_code") not in CONTINUE_REASONS):
            raise records.BrowserContentUnavailable("Browser help is unavailable.")
        if body is not None and (not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000):
            raise records.BrowserContentUnavailable("Browser message is unavailable.")
        if action == "message" and not body:
            raise records.BrowserContentUnavailable("Browser message is unavailable.")
        if action not in {"message"} and body is not None:
            raise records.BrowserContentUnavailable("Browser message is unavailable.")
        if body:
            # 8.1: any saved password of this user (every site) or, while an
            # mfa or mfa_step_up help is open, any code-shaped token. Raises
            # SecretInMessage; the text is not stored, logged or sent.
            secrets.guard_help_text(user_id, job_id, body)
        kind, rpc_action = "help_message", action
        content = sanitize(job, {"body": body, "action": action, "author_kind": "owner"})
    row = records.pack_record(user_id, job_id, kind, content, record_id=client_id, seq=0,
                              help_id=help_id, client_msg_id=client_id)
    if kind == "help_secret":
        key_id, key = vault._key()
        row["secret_hash"] = code_digest(key, content["secret"])
        row["secret_key_id"] = key_id
    result = repo.browser_help_reply_v2(user_id, job_id, help_id, client_id, rpc_action, row)
    if result.get("ok") is not True:
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    return owner_help(user_id, job_id)


def owner_reply(user_id: str, job_id: str, *, expected_help_id: str, action: str, client_msg_id: str,
                body: str | None = None, secret_value: str | None = None) -> dict:
    job = records.require_owned_job(user_id, job_id)
    help_id, client_id = records._uuid(expected_help_id), records._uuid(client_msg_id)
    state = repo.browser_help_state(user_id, job_id)
    if not state or state.get("id") != help_id:
        raise records.BrowserContentUnavailable("Browser help changed. Reload and try again.")
    if is_v2(job):
        return _owner_reply_v2(user_id, job, state, help_id=help_id, client_id=client_id, action=action,
                               body=body, secret_value=secret_value)
    if action not in {"message", "retry", "give_up"} or (body is not None and secret_value is not None):
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    # Retry and message both requeue the job without resetting its counters,
    # so a capped job could only burn attempts. Stop and private codes stay.
    if action in {"message", "retry"} and secret_value is None and _capped(job):
        raise BrowserTaskCapped(CAPPED)
    if secret_value is not None:
        if action != "message" or not isinstance(secret_value, str) or not V1_CODE.fullmatch(secret_value):
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
        row["secret_hash"] = code_digest(key, secret_value)
        row["secret_key_id"] = key_id
    result = repo.browser_help_reply(user_id, job_id, help_id, client_id, rpc_action, row)
    if result.get("ok") is not True:
        raise records.BrowserContentUnavailable("Browser help is unavailable.")
    return owner_help(user_id, job_id)


def consume_secret(job: dict, *, help_id: str, record_id: str, login_attempt_id: str | None,
                   worker_id: str, boot_id: str, lease_epoch: int) -> str:
    """The one-time code for the worker. v2: assistant_browser_help_consume_v2
    (login scope with its attempt, or mfa_step_up with no attempt)."""
    if job.get("principal_kind") != "user":
        raise records.BrowserContentUnavailable("Browser code is unavailable.")
    owner, job_id = records._uuid(job.get("principal_user_id")), records._uuid(job.get("id"))
    if is_v2(job):
        result = repo.browser_rpc(owner, "assistant_browser_help_consume_v2", {
            "p_job": job_id, "p_worker": str(worker_id), "p_boot": str(boot_id), "p_epoch": int(lease_epoch),
            "p_help_id": records._uuid(help_id), "p_record_id": records._uuid(record_id),
            "p_login_attempt_id": records._uuid(login_attempt_id) if login_attempt_id else None})
        pattern = CODE
    else:
        if not login_attempt_id:
            raise records.BrowserContentUnavailable("Browser code is unavailable.")
        result = repo.browser_help_consume(owner, job_id, worker_id, boot_id, lease_epoch,
                                          help_id, record_id, login_attempt_id)
        pattern = V1_CODE
    if result.get("ok") is not True or not isinstance(result.get("record"), dict):
        raise records.BrowserContentUnavailable("Browser code is unavailable.")
    value = records.unpack_record(owner, job_id, result["record"], allow_secret=True)
    code = value.get("secret")
    if not isinstance(code, str) or not pattern.fullmatch(code):
        raise records.BrowserContentUnavailable("Browser code is unavailable.")
    return code
