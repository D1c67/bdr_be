"""Code-fixed personal browser sites and narrow read destinations.

Adding a site requires a signed worker with the same ceiling. Configuration
cannot supply origins, login routines or arbitrary URLs.
"""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SiteSpec:
    key: str
    label: str
    origins: tuple[str, ...]
    read_origins: tuple[str, ...]
    agent_type: str
    available: bool = False


SITES = {
    "procore": SiteSpec("procore", "Procore", ("https://app.procore.com", "https://login.procore.com",
                                               "https://us02.procore.com"),
                        ("https://app.procore.com", "https://us02.procore.com"), "user_browser_read", True),
    "buildingconnected": SiteSpec("buildingconnected", "BuildingConnected", (), (), "", False),
    "smartbid": SiteSpec("smartbid", "SmartBid", (), (), "", False),
}
READ_PATHS = (
    re.compile(r"^/\d{1,20}/project/(home|rfi(/list)?|rfi/show/\d{1,20})/?$"),
    re.compile(r"^/webclients/host/companies/\d{1,20}/projects/\d{1,20}/tools/(projecthome|hubs/project-hub/views/overview|rfis(/\d{1,20})?)/?$"),
)


def site(service: str) -> SiteSpec:
    value = SITES.get(service)
    if value is None or not value.available:
        raise ValueError("Browser site is unavailable.")
    return value


def validate_url(service: str, url: str, *, login: bool = False) -> str:
    spec = site(service)
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise ValueError("Browser destination is unavailable.")
    try:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query or parsed.fragment
                or origin not in (spec.origins if login else spec.read_origins)):
            raise ValueError
        if not login and not any(pattern.fullmatch(parsed.path) for pattern in READ_PATHS):
            raise ValueError
    except ValueError:
        raise ValueError("Browser destination is unavailable.") from None
    return url


def read_origin(service: str, url: str) -> str:
    """Return an exact code-allowed origin, never normalize a wider URL."""
    parsed = urlsplit(validate_url(service, url))
    return f"{parsed.scheme}://{parsed.netloc}"


def credential_allows_url(credential: dict, service: str, url: str) -> bool:
    origins = credential.get("origins")
    return isinstance(origins, list) and read_origin(service, url) in origins
