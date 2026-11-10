"""The personal browser agent type `user_browser` (docs/ASSISTANT_BROWSER_V2_PLAN.md 3.4).

Owner: WI-6. Registered in browser_agents/registry.py next to the retired
`user_browser_read`. enabled() reads ASSISTANT_BROWSER_ENABLED,
ASSISTANT_BROWSER_V2_ENABLED and the type row; prepare() validates the job
input as protocol.UserBrowserInput and checks it names the job's own
admission. Grants, credentials and caps are not in the input: every fence
rechecks them live (assistant_browser_user_fence_v2) and the worker gets
them from the job policy (GET /jobs/{job_id}/user/policy).
"""
from __future__ import annotations

import logging
from typing import Any, get_args

from app.core.config import get_settings
from app.services.browser_agents import protocol
from app.services.browser_agents.registry import AgentTypeSpec, PrepareResult

logger = logging.getLogger(__name__)

KEY = protocol.USER_BROWSER_TYPE
PROMPT_VERSION = "user_browser.v1"
EXECUTION_UNAVAILABLE = "user_browser_execution_unavailable"


def flags_on() -> bool:
    """The two settings flags only (no database read)."""
    settings = get_settings()
    return bool(getattr(settings, "assistant_browser_enabled", False)
                and getattr(settings, "assistant_browser_v2_enabled", False))


def enabled() -> bool:
    """Both flags on and the `user_browser` type row enabled for the user
    principal. A read failure counts as disabled."""
    if not flags_on():
        return False
    try:
        from app.assistant import repo

        kind, _ = repo.browser_platform_config_v2("")
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("user_browser type row unreadable (%s)", type(exc).__name__)
        return False
    return bool(kind.get("key") == KEY and kind.get("enabled") is True and kind.get("principal_kind") == "user")


def prepare(job: dict[str, Any]) -> PrepareResult:
    """Claim-time check (the claim RPC already ran the live authority
    check): the flags, the principal, the input shape and the admission."""
    if not flags_on():
        return PrepareResult.fail("unsupported", EXECUTION_UNAVAILABLE, notify=False)
    try:
        if (job.get("principal_kind") != "user" or not job.get("principal_user_id")
                or job.get("agent_type") != KEY or job.get("user_credential_id") is not None):
            raise ValueError("principal")
        seed = protocol.UserBrowserInput.model_validate(job.get("input") or {})
        if str(seed.admission_id) != str(job.get("assistant_admission_id") or ""):
            raise ValueError("admission")
        if str(seed.authority.conversation_id) != str(job.get("assistant_conversation_id") or ""):
            raise ValueError("conversation")
    except (ValueError, TypeError):
        return PrepareResult.fail("unsupported", "user_browser_authority_unavailable", notify=False)
    return PrepareResult.run()


def claimed_input(job: dict[str, Any]) -> dict[str, Any]:
    """The input a claimed v2 job carries to the worker: exactly
    UserBrowserInput (5.3). Raises ValueError on a malformed row."""
    return protocol.UserBrowserInput.model_validate(job.get("input") or {}).model_dump(mode="json")


def build_spec() -> AgentTypeSpec:
    return AgentTypeSpec(
        key=KEY, label="Personal browser", input_model=protocol.UserBrowserInput, checkpoint_model=None,
        identity=None, prompt_version=PROMPT_VERSION, succeed_outcomes=("nothing_to_do",), prepare=prepare,
        principal_kind="user", help_reasons=tuple(get_args(protocol.UserHelpReasonCode)),
    )


SPEC = build_spec()
