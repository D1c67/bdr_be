"""Agent types this worker can run (spec 3.6 "Adding a type").

`REGISTRY` maps a type key to its JobRunner (advertised in /hello);
`ceiling_for` gives each type's code-fixed browser ceiling; and
`browser_provider(paths)` builds the browser provider (__main__ calls it).

User pool (docs/ASSISTANT_BROWSER_V2_PLAN.md 10.1, 10.6, 15.1): the personal
service (home C:\\ProgramData\\BDRAgentUser, or any home with a stable
runtime folder, the same rule as the launcher's `wants_stable_runtime`) gets a
provider with the user pool configuration: crash cleanup, the boot self-test
and one Chromium process per `user_browser` job. The org service is unchanged.
`user_browser_read` (v1) is retired: it is not registered, has no ceiling, and
the slot refuses a job of that name (`retired_type`).

Imports are lazy (PEP 562 module __getattr__): importing a submodule such as
the per-job egress proxy (`python -I -m bdr_agent.agents.user_browser_v2.egress`)
must not build REGISTRY, which loads every runner, the RFI agent and the guard.
`REGISTRY` is built on first access and then cached on the module.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..browser.browser import ChromiumProvider, UserPoolConfig
    from ..browser.guard import Ceiling
    from ..local_config import Paths

USER_POOL_HOME_NAME = "bdragentuser"


def _build_registry() -> dict[str, Any]:
    from .canary.runner import CanaryRunner
    from .rfi_from_email.runner import RfiFromEmailRunner
    from .user_browser_v2.runner import UserBrowserV2Runner

    return {
        "rfi_from_email": RfiFromEmailRunner(),
        "canary": CanaryRunner(),
        "user_browser": UserBrowserV2Runner(),
    }


def __getattr__(name: str) -> Any:
    if name == "REGISTRY":
        registry = _build_registry()
        globals()["REGISTRY"] = registry
        return registry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def user_browser_ceiling(agent_type: str = "user_browser", api_host: str = "") -> Ceiling:
    """The user type has no code-fixed navigation hosts: its scope comes per
    job from the policy, enforced by UserGuardV2. This empty ceiling only
    satisfies callers that ask every type for one."""
    from ..browser.guard import Ceiling

    return Ceiling.from_protocol(agent_type, api_host=api_host)


def _ceilings() -> dict[str, Any]:
    from .canary.runner import ceiling as canary_ceiling
    from .pm_apps.procore.ceiling import ceiling as procore_ceiling

    return {"rfi_from_email": procore_ceiling, "canary": canary_ceiling,
            "user_browser": user_browser_ceiling}


def ceiling_for(agent_type: str, api_host: str = "") -> Ceiling:
    try:
        factory = _ceilings()[agent_type]
    except KeyError:
        raise ValueError(f"no browser ceiling for agent type {agent_type}") from None
    return factory(agent_type, api_host)


def is_user_pool_home(home: Path) -> bool:
    return home.name.lower() == USER_POOL_HOME_NAME or (home / "runtime").is_dir()


def user_pool_config(paths: Paths) -> UserPoolConfig:
    from ..browser.browser import UserPoolConfig
    from .user_browser_v2.selftest import UserPoolLayout, UserPoolSelfTest

    layout = UserPoolLayout.for_home(paths.home)
    return UserPoolConfig(layout=layout, selftest=UserPoolSelfTest.from_env(layout))


def browser_provider(paths: Paths) -> ChromiumProvider:
    from ..browser.browser import ChromiumProvider
    from ..local_config import load_local_config

    cfg = load_local_config(paths)
    user_pool = user_pool_config(paths) if is_user_pool_home(Path(paths.home)) else None
    return ChromiumProvider(ceilings=ceiling_for, api_host=cfg.api_host if cfg else "",
                            tmp_dir=Path(paths.tmp_dir), user_pool=user_pool)
