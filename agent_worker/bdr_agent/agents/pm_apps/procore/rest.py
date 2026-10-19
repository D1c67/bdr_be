"""Read-only Procore REST calls inside the job's browser context (spec 4.1).

The site tools fetch through `context.request` (Playwright's
APIRequestContext), which shares the job context's cookies and so the
logged-in session. It bypasses `context.route`, so this module is the
guard for it, and it is the only caller:

  - GET only; host must be the job's seed host (a Procore app host);
  - path must match the vendored RFI_ALLOWED_PATHS, or be the project home
    page (read for its redirect Location only, never followed);
  - no redirects are followed; a redirect to the login host is `unauthorized`;
  - no token-like query keys;
  - the answer must be JSON and at most 8 MB.

Every call writes one `http.get` step: path (no query), status, kind.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from ....browser.guard import token_query_keys
from ....protocol import PROCORE_HOSTS, PROCORE_LOGIN_HOST
from .paths import MAX_JSON_BYTES, PROJECT_HOME_RE, rest_path_allowed

log = logging.getLogger("bdr_agent.procore.rest")

REQUEST_TIMEOUT_MS = 30_000
_JSON_ACCEPT = "application/json, text/plain, */*"
_HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class RestRefused(ValueError):
    """A call this module will not make (a coding error, never a page's doing)."""


@dataclass
class RestResult:
    kind: str                      # ok | unauthorized | forbidden | not_found | challenge | not_json |
    #                                too_large | redirect | error
    status: int
    data: Any = None
    location: str | None = None
    body_bytes: bytes | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def _looks_like_challenge(headers: dict[str, str], body: bytes) -> bool:
    if headers.get("cf-mitigated"):
        return True
    if "text/html" not in headers.get("content-type", ""):
        return False
    head = body[:4000].decode("utf-8", "replace").lower()
    return "challenge-platform" in head and ("just a moment" in head or "cf-chl" in head)


class ProcoreRest:
    def __init__(self, request_context: Any, host: str, *, log_step: Any = None):
        if host not in PROCORE_HOSTS:
            raise RestRefused(f"not a Procore app host: {host}")
        self._rc = request_context
        self.host = host
        self.base = f"https://{host}"
        self._log_step = log_step

    def _step(self, path: str, res: RestResult, started: float) -> None:
        if self._log_step is None:
            return
        self._log_step("http.get", f"GET {path} -> {res.status} {res.kind}", ok=res.ok,
                       result={"host": self.host, "path": path, "status": res.status, "kind": res.kind},
                       duration_ms=int((time.monotonic() - started) * 1000))

    async def _get(self, path: str, *, params: dict[str, str] | None, headers: dict[str, str]) -> Any:
        url = self.base + path + (("?" + urlencode(params)) if params else "")
        if token_query_keys(url):
            raise RestRefused("token-like query key")
        return await self._rc.get(url, headers=headers, max_redirects=0, timeout=REQUEST_TIMEOUT_MS,
                                  fail_on_status_code=False)

    async def get_json(self, path: str, *, company_id: str | None = None,
                       params: dict[str, str] | None = None, referer: str | None = None) -> RestResult:
        if not rest_path_allowed(path):
            raise RestRefused(f"Procore path is not on the RFI allowlist: {path}")
        headers = {"Accept": _JSON_ACCEPT}
        if company_id:
            if not company_id.isdigit():
                raise RestRefused("company id must be digits")
            headers["Procore-Company-Id"] = company_id
        if referer:
            headers["Referer"] = referer
        started = time.monotonic()
        resp = await self._get(path, params=params, headers=headers)
        try:
            res = await self._classify(resp)
        finally:
            await _dispose(resp)
        self._step(path, res, started)
        return res

    async def home_redirect(self, project_id: str) -> RestResult:
        """GET the project home page for its redirect Location (company id)."""
        path = f"/{project_id}/project/home"
        if not PROJECT_HOME_RE.match(path):
            raise RestRefused("not a project home path")
        started = time.monotonic()
        resp = await self._get(path, params=None, headers={"Accept": _HTML_ACCEPT})
        try:
            status = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items()}
            if status in (301, 302, 303, 307, 308):
                target = urljoin(self.base + path, headers.get("location", ""))
                kind = "unauthorized" if _is_login_redirect(target) else "redirect"
                res = RestResult(kind, status, location=target)
            elif status == 200 and "html" in headers.get("content-type", ""):
                body = await resp.body()
                res = RestResult("ok", status, data=body[: 2 * 1024 * 1024].decode("utf-8", "replace"))
            elif status == 401:
                res = RestResult("unauthorized", status)
            elif status in (403, 404):
                res = RestResult("forbidden" if status == 403 else "not_found", status)
            else:
                res = RestResult("error", status)
        finally:
            await _dispose(resp)
        self._step(path, res, started)
        return res

    async def _classify(self, resp: Any) -> RestResult:
        status = resp.status
        headers = {k.lower(): v for k, v in resp.headers.items()}
        if status == 401:
            return RestResult("unauthorized", status)
        if status in (301, 302, 303, 307, 308):
            target = urljoin(self.base, headers.get("location", ""))
            return RestResult("unauthorized" if _is_login_redirect(target) else "redirect", status, location=target)
        length = headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_JSON_BYTES:
            return RestResult("too_large", status)
        body = await resp.body()
        if len(body) > MAX_JSON_BYTES:
            return RestResult("too_large", status)
        # Cloudflare answers its interstitial with 403/503: check it first.
        if _looks_like_challenge(headers, body):
            return RestResult("challenge", status)
        if status == 403:
            return RestResult("forbidden", status)
        if status == 404:
            return RestResult("not_found", status)
        if status != 200:
            return RestResult("error", status)
        if "json" not in headers.get("content-type", ""):
            return RestResult("not_json", status)
        try:
            data = json.loads(body)
        except ValueError:
            return RestResult("not_json", status)
        return RestResult("ok", status, data=data, body_bytes=body)


def _is_login_redirect(target: str) -> bool:
    parts = urlsplit(target)
    return (parts.hostname or "").lower() == PROCORE_LOGIN_HOST or (parts.path or "").startswith("/auth/procore")


async def _dispose(resp: Any) -> None:
    try:
        await resp.dispose()
    except Exception as exc:
        log.debug("response dispose failed: %s", exc)
