"""Personal read type; activation requires the complete private execution path."""
from typing import Any, get_args
from app.core.config import get_settings
from app.services.browser_agents import protocol
from app.services.browser_agents.registry import AgentTypeSpec, PrepareResult

EXECUTION_UNAVAILABLE = "user_browser_execution_unavailable"


def enabled() -> bool:
    from app.services import assistant_browser
    return bool(assistant_browser.EXECUTION_READY and get_settings().assistant_browser_enabled)


def prepare(job: dict[str, Any]) -> PrepareResult:
    from app.services import assistant_browser_sites, assistant_browser_vault
    if not enabled():
        return PrepareResult.fail("unsupported", EXECUTION_UNAVAILABLE, notify=False)
    try:
        if job.get("principal_kind") != "user" or not job.get("principal_user_id"):
            raise ValueError("principal")
        data = job.get("input") or {}
        seed = protocol.UserBrowserReadInput.model_validate({key: data.get(key) for key in ("service", "url", "objective")})
        assistant_browser_sites.validate_url(seed.service, seed.url)
        assistant_browser_vault.job_credential(job)
    except (ValueError, TypeError):
        return PrepareResult.fail("unsupported", "user_browser_authority_unavailable", notify=False)
    return PrepareResult.run()


SPEC = AgentTypeSpec(
    key="user_browser_read", label="Personal browser read",
    input_model=protocol.UserBrowserReadInput, checkpoint_model=None,
    identity=None, prompt_version="user_browser_read.v1", succeed_outcomes=("nothing_to_do",),
    prepare=prepare, principal_kind="user", help_reasons=tuple(get_args(protocol.HelpReasonCode)),
)
