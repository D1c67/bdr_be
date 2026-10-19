"""Procore paths the worker may touch, vendored from
app/services/procore_client.py (tests/test_procore_paths_drift.py fails on
any difference). GET-only REST reads for the RFI tools, and the login chain
the login routine walks. Nothing here can write to Procore.
"""

import re

_LONG_ID = r"\d{1,20}"

# The read-only RFI endpoints (procore_client.RFI_ALLOWED_PATHS): RFI list
# and detail, the account's companies, one project.
RFI_ALLOWED_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^/rest/v1\.0/projects/{_LONG_ID}/rfis$"),
    re.compile(rf"^/rest/v1\.0/projects/{_LONG_ID}/rfis/{_LONG_ID}$"),
    re.compile(r"^/rest/v1\.0/companies$"),
    re.compile(rf"^/rest/v1\.0/projects/{_LONG_ID}$"),
)
# The project home page: requested only to read its redirect Location (the
# company id is in the webclient path), never followed
# (procore_client._PROJECT_HOME_RE).
PROJECT_HOME_RE = re.compile(rf"^/({_LONG_ID})/project/home$")
WEBCLIENT_RE = re.compile(rf"^/webclients/host/companies/({_LONG_ID})/projects/({_LONG_ID})(?:/|$)")
COMPANY_PROJECT_RE = re.compile(rf"^/companies/({_LONG_ID})/projects/({_LONG_ID})(?:/|$)")

# login.procore.com paths the login page object may load and POST to
# (procore_client._LOGIN_LOGIN_PATHS).
LOGIN_LOGIN_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/$"),
    re.compile(r"^/oauth/authorize$"),
    re.compile(r"^/login/password$"),
    re.compile(r"^/sessions/submit_login_email$"),
    re.compile(r"^/sessions/submit_login_password$"),
)
# App-host paths on the login chain: the OAuth entry and callback, and the
# account's company chooser (a subset of procore_client._LOGIN_APP_PATHS; the
# harvest-only bid sheet paths are left out).
LOGIN_APP_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/auth/procore$"),
    re.compile(r"^/auth/procore/callback$"),
    re.compile(r"^/account/select_company$"),
)

# The app session cookie the OAuth callback sets on the app host.
SESSION_COOKIE = "_session_id"
LOGIN_ENTRY_PATH = "/auth/procore"
PROBE_PATH = "/rest/v1.0/companies"
MAX_JSON_BYTES = 8 * 1024 * 1024


def rest_path_allowed(path: str) -> bool:
    return any(p.match(path) for p in RFI_ALLOWED_PATHS)
