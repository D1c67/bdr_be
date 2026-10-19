"""The rfi_from_email code ceiling (spec 4.3): protocol.CEILINGS plus the
vendored login paths. Settings host_narrowing can only shrink nav hosts."""

from ....browser.guard import Ceiling
from .paths import LOGIN_APP_PATHS, LOGIN_LOGIN_PATHS


def ceiling(agent_type: str = "rfi_from_email", api_host: str = "") -> Ceiling:
    return Ceiling.from_protocol(agent_type, api_host=api_host, login_paths=LOGIN_LOGIN_PATHS,
                                 login_app_paths=LOGIN_APP_PATHS)
