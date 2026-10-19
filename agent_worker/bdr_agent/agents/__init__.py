"""Agent types this worker can run (spec 3.6 "Adding a type").

`REGISTRY` maps a type key to its JobRunner (advertised in /hello);
`ceiling_for` gives each type's code-fixed browser ceiling; and
`browser_provider(paths)` builds the one shared Chromium (__main__ calls it).
"""

from pathlib import Path
from typing import Any

from ..browser.browser import ChromiumProvider
from ..browser.guard import Ceiling
from ..local_config import Paths, load_local_config
from .canary.runner import CanaryRunner
from .canary.runner import ceiling as canary_ceiling
from .pm_apps.procore.ceiling import ceiling as procore_ceiling
from .rfi_from_email.runner import RfiFromEmailRunner

REGISTRY: dict[str, Any] = {
    "rfi_from_email": RfiFromEmailRunner(),
    "canary": CanaryRunner(),
}

_CEILINGS = {"rfi_from_email": procore_ceiling, "canary": canary_ceiling}


def ceiling_for(agent_type: str, api_host: str = "") -> Ceiling:
    try:
        return _CEILINGS[agent_type](agent_type, api_host)
    except KeyError:
        raise ValueError(f"no browser ceiling for agent type {agent_type}") from None


def browser_provider(paths: Paths) -> ChromiumProvider:
    cfg = load_local_config(paths)
    return ChromiumProvider(ceilings=ceiling_for, api_host=cfg.api_host if cfg else "",
                            tmp_dir=Path(paths.tmp_dir))
