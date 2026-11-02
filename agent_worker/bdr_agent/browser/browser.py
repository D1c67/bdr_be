"""One headless Chromium per worker, one ephemeral context per job (spec D11,
3.6, 4.2, 4.3).

  - Chromium is launched once (`start`) with --password-store=basic and the
    background services off; nothing persistent: Playwright's temporary
    profile lives under <home>/tmp (wiped at every boot) and every job gets a
    brand new BrowserContext (incognito-like: no saved passwords, no cookies
    at rest) that is closed in the slot's `finally`.
  - Playwright tracing and HAR are never enabled; DEBUG / PWDEBUG are cleared
    before the driver starts so protocol logging cannot capture a password.
  - A crashed or disconnected Chromium is relaunched on the next
    `new_context`; the jobs that were running on it fail with a browser error
    and the slot requeues them as `crash` (charged).
  - Context options: service workers blocked, downloads refused, viewport
    1366x768, Pacific time, en-US. `Guard` (guard.py) is installed on every
    context before a page exists.

`proxy` and `ignore_https_errors` exist for the local Procore fixture in the
tests only; `browser_provider(paths)` in agents/__init__.py never sets them.
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..local_config import CHROMIUM_TMP_NAME
from ..protocol import ClaimedJob
from .guard import Ceiling, Guard

log = logging.getLogger("bdr_agent.browser")

VIEWPORT = {"width": 1366, "height": 768}
# Route.fetch uses Playwright's Node HTTP parser, not Chromium's parser. Keep
# its response-header allowance bounded for sites with large CSP/cookie sets.
# This affects only the driver child; inherited Node execution flags are not
# forwarded, and the parent environment is restored before Chromium launches.
DRIVER_NODE_OPTIONS = "--max-http-header-size=65536"
LAUNCH_ARGS = (
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-features=PasswordLeakDetection,AutofillServerCommunication,MediaRouter,OptimizationHints,Translate",
)
# Environment the Playwright driver must never see (protocol logs would
# include typed values).
_SCRUBBED_ENV = ("DEBUG", "PWDEBUG", "DEBUG_FILE", "PLAYWRIGHT_DEBUG", "PW_TEST_DEBUG")
CEILING_FACTORY = Callable[[str, str], Ceiling]   # (agent_type, api_host) -> Ceiling


def clear_debug_env() -> None:
    for name in _SCRUBBED_ENV:
        os.environ.pop(name, None)


def kill_stale_chromium(marker: Path) -> int:
    """Kill Chromium processes left by an earlier worker process: any process
    whose command line contains `marker` (our temp dir). Best effort."""
    text = str(marker)
    killed = 0
    try:
        if sys.platform == "win32":
            script = (
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                f"$_.CommandLine.Contains('{text}') -and $_.ProcessId -ne {os.getpid()} }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
            )
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=30, check=False).stdout
            killed = sum(1 for line in out.splitlines() if line.strip().isdigit())
        else:
            out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True,
                                 timeout=15, check=False).stdout
            for line in out.splitlines():
                pid_s, _, cmd = line.strip().partition(" ")
                if text in cmd and pid_s.isdigit() and int(pid_s) != os.getpid():
                    try:
                        os.kill(int(pid_s), signal.SIGKILL)
                        killed += 1
                    except OSError:
                        pass
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not look for leftover Chromium: %s", exc)
    if killed:
        log.warning("killed %d leftover Chromium process(es)", killed)
    return killed


class JobBrowser:
    """The browser side of one job: its context, its guard and the pages this
    code owns (one model page, at most one login page)."""

    def __init__(self, context: Any, guard: Guard, *, agent_type: str, asset_request: Any = None):
        self.context = context
        self.guard = guard
        self.agent_type = agent_type
        self.asset_request = asset_request
        self.page: Any = None
        self.login_page: Any = None
        self._closed = False

    @property
    def request(self) -> Any:
        """APIRequestContext sharing this job's cookies. It bypasses routing:
        only the REST site tools may use it, and they enforce their own
        allowlist."""
        return self.context.request

    def attach_steps(self, log_step: Callable[..., Any]) -> None:
        self.guard.log_step = log_step

    def apply_narrowing(self, host_narrowing: list[str] | None) -> None:
        self.guard.ceiling = self.guard.ceiling.narrowed(host_narrowing)

    async def model_page(self) -> Any:
        if self.page is None or self.page.is_closed():
            self.page = await self.guard.new_page(self.context)
        return self.page

    async def open_login_page(self) -> Any:
        await self.close_login_page()
        self.login_page = await self.guard.new_page(self.context, login=True)
        return self.login_page

    async def close_login_page(self) -> None:
        page, self.login_page = self.login_page, None
        self.guard.release_login_page()
        if page is not None:
            try:
                await page.close()
            except Exception as exc:
                log.debug("closing login page failed: %s", exc)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.login_page = None
        self.guard.release_login_page()
        try:
            await self.guard.settle()
        finally:
            try:
                await self.context.close()
            except Exception as exc:
                log.debug("context close failed: %s", exc)
            finally:
                if self.asset_request is not None:
                    try:
                        await self.asset_request.dispose()
                    finally:
                        self.asset_request = None


class ChromiumProvider:
    """BrowserProvider for the slot (slot.BrowserProvider protocol)."""

    def __init__(self, *, ceilings: CEILING_FACTORY, api_host: str = "", tmp_dir: Path | None = None,
                 headless: bool = True, proxy: dict[str, str] | None = None,
                 ignore_https_errors: bool = False, launch_timeout_ms: int = 60_000):
        self._ceilings = ceilings
        self.api_host = api_host
        self.tmp_dir = Path(tmp_dir) if tmp_dir else None
        self.headless = headless
        self._proxy = proxy
        self._ignore_https_errors = ignore_https_errors
        self._launch_timeout_ms = launch_timeout_ms
        self._pw: Any = None
        self._pw_cm: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self.chromium_version: str | None = None
        self.launches = 0

    # Lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        clear_debug_env()
        if self.tmp_dir is not None:
            chrome_tmp = self.tmp_dir / CHROMIUM_TMP_NAME
            kill_stale_chromium(chrome_tmp)
            # The driver creates Chromium's throwaway profile under the temp
            # dir. Wipe the previous process's leftovers here, before launch:
            # supervisor boot hygiene runs after the browser is up and so
            # leaves this live directory alone.
            shutil.rmtree(chrome_tmp, ignore_errors=True)
            chrome_tmp.mkdir(parents=True, exist_ok=True)
            self._chrome_tmp = chrome_tmp
            for var in ("TMPDIR", "TMP", "TEMP"):
                os.environ[var] = str(chrome_tmp)
        from playwright.async_api import async_playwright

        self._pw_cm = async_playwright()
        previous_node_options = os.environ.get("NODE_OPTIONS")
        os.environ["NODE_OPTIONS"] = DRIVER_NODE_OPTIONS
        try:
            self._pw = await self._pw_cm.__aenter__()
        finally:
            if previous_node_options is None:
                os.environ.pop("NODE_OPTIONS", None)
            else:
                os.environ["NODE_OPTIONS"] = previous_node_options
        await self._launch()

    async def _launch(self) -> None:
        chrome_tmp = getattr(self, "_chrome_tmp", None)
        if chrome_tmp is not None:
            chrome_tmp.mkdir(parents=True, exist_ok=True)   # a relaunch needs the temp dir to exist
        kwargs: dict[str, Any] = {"headless": self.headless, "args": list(LAUNCH_ARGS),
                                  "timeout": self._launch_timeout_ms, "handle_sigint": False,
                                  "handle_sigterm": False, "handle_sighup": False}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._browser = await self._pw.chromium.launch(**kwargs)
        self.launches += 1
        self.chromium_version = self._browser.version
        self._browser.on("disconnected", lambda *_: log.warning("Chromium disconnected"))
        log.info("Chromium %s started", self.chromium_version)

    async def stop(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            try:
                await browser.close()
            except Exception as exc:
                log.debug("browser close failed: %s", exc)
        if self._pw_cm is not None:
            try:
                await self._pw_cm.__aexit__(None, None, None)
            except Exception as exc:
                log.debug("playwright stop failed: %s", exc)
            self._pw_cm = self._pw = None

    async def _ensure_browser(self) -> Any:
        async with self._lock:
            if self._pw is None:
                raise RuntimeError("browser provider not started")
            if self._browser is None or not self._browser.is_connected():
                log.warning("relaunching Chromium")
                await self._launch()
            return self._browser

    # BrowserProvider ---------------------------------------------------------

    async def new_context(self, job: ClaimedJob) -> JobBrowser:
        browser = await self._ensure_browser()
        if job.principal_kind == "user":
            if job.agent_type != "user_browser_read" or not job.principal_user_id:
                raise ValueError("Personal job principal invalid")
            from ..agents.user_browser.guard import UserGuard
            guard = UserGuard(self._ceilings(job.agent_type, self.api_host))
            guard.bind_seed(job.input.get("url", ""))
        else:
            guard = Guard(self._ceilings(job.agent_type, self.api_host))
        context = await browser.new_context(
            viewport=VIEWPORT, accept_downloads=False, service_workers="block", locale="en-US",
            timezone_id="America/Los_Angeles", java_script_enabled=True,
            ignore_https_errors=self._ignore_https_errors)
        asset_request = None
        try:
            context.set_default_timeout(30_000)
            context.set_default_navigation_timeout(45_000)
            if job.principal_kind == "user":
                options = {"ignore_https_errors": self._ignore_https_errors}
                if self._proxy:
                    options["proxy"] = self._proxy
                asset_request = await self._pw.request.new_context(**options)
                guard.asset_request = asset_request
            await guard.install(context)
            jb = JobBrowser(context, guard, agent_type=job.agent_type, asset_request=asset_request)
            await jb.model_page()
            return jb
        except BaseException:
            try:
                await context.close()
            finally:
                if asset_request is not None:
                    await asset_request.dispose()
            raise

    async def close_context(self, context: Any) -> None:
        if isinstance(context, JobBrowser):
            await context.close()
        elif context is not None:
            await context.close()
