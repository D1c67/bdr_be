"""Personal browser vault management and safe task metadata.

Approved reads use the owner-bound encrypted identity, trace and help paths.
Runtime configuration separately gates vault management and execution.
"""
from __future__ import annotations

from uuid import uuid4
from decimal import Decimal

from app.assistant import repo
from app.core.config import get_settings
from app.services import assistant_browser_sites as sites
from app.services import assistant_browser_vault as vault

EXECUTION_READY = True
UNAVAILABLE_REASON = "Personal browser execution is unavailable. Check the browser configuration."
METADATA_FIELDS = ("id", "service", "label", "username", "origins", "status", "version", "fingerprint", "updated_at")


def configured() -> bool:
    try:
        vault._key()
        return True
    except vault.VaultUnavailable:
        return False


def site_status() -> dict:
    enabled = bool(getattr(get_settings(), "assistant_browser_enabled", False))
    return {"enabled": enabled, "execution_ready": EXECUTION_READY, "configured": configured(),
            "items": [{"service": spec.key, "label": spec.label, "origins": list(spec.origins),
                       "available": spec.available,
                       "reason": (None if enabled and EXECUTION_READY else UNAVAILABLE_REASON)
                                 if spec.available else "This site is not supported yet."}
                      for spec in sites.SITES.values()]}


def metadata(row: dict) -> dict:
    return {key: row.get(key) for key in METADATA_FIELDS}


def credentials(user_id: str) -> dict:
    return {"items": [metadata(row) for row in repo.browser_credentials(user_id)]}


def save_credential(user_id: str, service: str, *, label: str, username: str, password: str,
                    expected_version: int | None, acknowledge_personal_password: bool) -> dict:
    spec = sites.site(service)
    if not acknowledge_personal_password:
        raise ValueError("Only a personal site password may be stored.")
    if not label.strip() or not username.strip() or not password or len(password) > 1024:
        raise ValueError("Credential is unavailable.")
    current = repo.browser_credential(user_id, service)
    expected = expected_version or 0
    if (current is None and expected != 0) or (current and expected != current.get("version")):
        raise ValueError("Credential changed. Reload and try again.")
    credential_id = str(current["id"]) if current else str(uuid4())
    version = expected + 1
    envelope = vault.encrypt_payload(user_id, credential_id, version, "credential", password.encode())
    record = {"id": credential_id, "label": label.strip(), "username": username.strip(),
              "origins": list(spec.origins), "fingerprint": vault.password_fingerprint(user_id, password), **envelope}
    result = repo.browser_credential_put(user_id, service, expected, record)
    if not result.get("ok") or not isinstance(result.get("credential"), dict):
        raise ValueError("Credential changed. Reload and try again.")
    return metadata(result["credential"])


def revoke_credential(user_id: str, service: str, expected_version: int) -> None:
    sites.site(service)
    result = repo.browser_credential_revoke(user_id, service, expected_version)
    if not result.get("ok"):
        raise ValueError("Credential changed. Reload and try again.")


def revoke_before_mfa_reset(user_id: str) -> None:
    """Fence stored authority before factors are removed, even with flags off.

    Deployments without the phase 6 table cannot have personal credentials.
    Every other read/revoke failure propagates and blocks the reset.
    """
    try:
        rows = repo.browser_credentials(user_id)
    except Exception as exc:
        if str(getattr(exc, "code", "")) in {"42P01", "PGRST205"}:
            return
        raise
    if any(row.get("status") == "active" for row in rows):
        repo.browser_revoke_owner(user_id)
        if any(row.get("status") == "active" for row in repo.browser_credentials(user_id)):
            raise ValueError("Personal browser revocation failed.")


def task_metadata(row: dict) -> dict:
    # No checkpoint, tool output, storage locator or credential identifiers.
    seed = row.get("input") if isinstance(row.get("input"), dict) else {}
    def text(value, limit=100):
        return value[:limit] if isinstance(value, str) else None
    return {"id": text(row.get("id")), "service": text(seed.get("service")), "url": text(seed.get("url"), 2048),
            "objective": text(seed.get("objective") or seed.get("task"), 2000), "status": text(row.get("status")),
            "outcome": text(row.get("outcome")), "error_kind": text(row.get("error_kind")),
            "created_at": text(row.get("created_at")), "expires_at": text(row.get("expires_at")),
            "cost_usd": row.get("cost_usd", 0), "result": None, "help_id": None}


def tasks(user_id: str) -> dict:
    return {"items": [task_metadata(row) for row in repo.browser_jobs(user_id)]}


def task(user_id: str, job_id: str) -> dict | None:
    row = repo.browser_job(user_id, job_id)
    if not row:
        return None
    from app.services import assistant_browser_records
    result = task_metadata(row)
    result["result"] = assistant_browser_records.latest_result(user_id, job_id)
    help_state = repo.browser_help_state(user_id, job_id)
    result["help_id"] = help_state.get("id") if help_state else None
    return result


def prepare(user_id: str, run_id: str, args: dict) -> dict:
    from app.assistant import access, mail_binding, spend
    if (not EXECUTION_READY or not getattr(get_settings(), "assistant_browser_enabled", False)
            or access.owner_check(user_id, require_writer=True).problem):
        raise ValueError(UNAVAILABLE_REASON)
    if not isinstance(args, dict) or set(args) - {"service", "url", "objective", "response_format", "limit"}:
        raise ValueError("Invalid browser request.")
    spec = sites.site(args.get("service"))
    url = sites.validate_url(spec.key, args.get("url"))
    texts = repo.mail_user_texts(user_id, run_id)
    if not mail_binding.url_is_bound(url, user_texts=texts):
        raise ValueError("Type the exact Procore page URL in your request.")
    objective = args.get("objective")
    if not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 2000:
        raise ValueError("Describe what to read in your own words.")
    cid = repo.conversation_for_run(user_id, run_id)
    conversation = repo.get_conversation(user_id, cid) if cid else None
    if not conversation or conversation.get("mode") != "normal" or conversation.get("mailbox_tainted_at"):
        raise ValueError("Start a normal conversation for browser work.")
    credential = repo.browser_credential(user_id, spec.key)
    if not credential or credential.get("status") != "active":
        raise ValueError("Save your personal Procore account in Assistant settings first.")
    if not sites.credential_allows_url(credential, spec.key, url):
        raise ValueError("Save your Procore sign-in again in Assistant settings to authorize this regional host.")
    kind, settings = repo.browser_platform_config(user_id)
    if (kind.get("principal_kind") != "user" or kind.get("enabled") is not True
            or settings.get("enabled") is not True or kind.get("provider") != "openai"
            or not kind.get("model") or not kind.get("model_probe_ok_at")):
        raise ValueError(UNAVAILABLE_REASON)
    for field in ("price_in_per_mtok", "price_out_per_mtok"):
        if kind.get(field) is None or not Decimal(str(kind[field])).is_finite() or Decimal(str(kind[field])) <= 0:
            raise ValueError("Browser model pricing is unavailable.")
    maximum = min(Decimal("2"), Decimal(str(settings.get("per_job_cost_cap_usd") or 0)))
    budget = spend.state(user_id)
    if maximum <= 0 or maximum >= min(budget.user_remaining, budget.global_remaining):
        raise ValueError("The browser read exceeds the remaining assistant budget.")
    human = repo.human_profile(user_id) or {}
    authority = {"credential_id":credential["id"],"credential_version":credential["version"],
                 "role":human.get("role"),"conversation_id":cid,"model":kind["model"],
                 "max_cost_usd":str(maximum),"url":url,"objective":objective.strip()}
    return {"available":True,"service":spec.key,"url":url,"objective":objective.strip(),
            "credential_id":credential["id"],"credential_version":credential["version"],
            "credential_label":credential.get("label"),"max_cost_usd":str(maximum),"authority":authority}


def enqueue(user_id: str, run_id: str, approval_id: str, body: dict) -> dict:
    from app.assistant.approvals import sha256_hex
    from app.assistant.spend import cap_defaults
    approval = repo.get_approval(user_id, approval_id)
    if (not approval or approval.get("status") != "executing" or approval.get("action_kind") != "user_browser_task"
            or approval.get("payload", {}).get("request", {}).get("json") != body):
        raise ValueError("Browser approval changed.")
    existing = repo.browser_job_by_approval(user_id, approval_id)
    if existing:
        return task_metadata(existing)
    fresh = prepare(user_id, run_id, approval["payload"]["args"])
    if any(body.get(key) != fresh[key] for key in ("service","url","objective","credential_id","credential_version","max_cost_usd","authority")):
        raise ValueError("Browser authority changed. Prepare a new approval.")
    user_cap, global_cap = cap_defaults()
    result = repo.browser_rpc(user_id, "assistant_browser_task_enqueue", {
        "p_user_id":user_id,"p_approval_id":approval_id,"p_run_id":run_id,
        "p_payload_sha256":sha256_hex(approval["payload"]),"p_input":body,
        "p_user_cap_default":str(user_cap),"p_global_cap_default":str(global_cap)})
    if not result.get("ok") or not result.get("job"):
        raise ValueError("Browser work is unavailable or already running.")
    return task_metadata(result["job"])
