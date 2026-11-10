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

User pool, `user_browser` v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 10.1, 10.3,
10.6, 11.1, 15.4). The org path above is unchanged. With `user_pool` set:
  - `start` runs the crash cleanup (selftest.cleanup) before anything else,
    puts this worker process in a Windows Job Object with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (every later child: the Playwright
    driver, each per-job Chromium and each egress proxy inherits it, so the OS
    kills them all when the worker dies), starts the driver from the stable
    node.exe (PLAYWRIGHT_NODEJS_PATH) and runs the boot self-test, whose
    Chromium check launches the real per-job executable without a proxy.
    No shared Chromium is launched for the user pool.
  - each `user_browser` job gets its own Chromium process
    (`launch_persistent_context`) from the stable executable, channel
    "chromium" (full Chromium, new headless; never the headless shell), the
    renderer sandbox on, a real Windows user agent, its own profile under
    <home>\\jobs\\<job id>\\profile, its own egress proxy process (Playwright's
    `proxy` option, which covers Chromium, route.fetch and the context's
    APIRequestContext), downloads accepted into
    C:\\ProgramData\\BDRQuarantine\\<job id>, service workers blocked, no
    permissions, WebRTC limited to proxied UDP, DNS prefetch and QUIC off.
    Until a guard is installed the context aborts every request (deny-all
    route) and `model_page()` refuses. The guard (UserGuard v2, WI-9) needs
    the job policy, which the runner fetches after claim, so the runner calls
    `await job_browser.install_guard(guard)`; `UserPoolConfig.guard_factory`
    may install one at launch instead.
  - `UserJobBrowser.close()` closes Chromium, stops the egress process, kills
    anything still running from the job folder and deletes the job folder and
    its quarantine folder.
  - a job is refused (SetupUnavailable: uncharged requeue) unless the self-test
    proved the floor.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
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

USER_BROWSER_TYPE = "user_browser"
RETIRED_USER_TYPE = "user_browser_read"       # v1 personal agent, deleted in 1.1.0
# Extra Chromium switches for user jobs (plan 10.3). Chromium reads only the
# last --disable-features, so the user list repeats the org one. Names checked
# against the shipped Chromium 156 (WI-11); unknown names are ignored.
USER_DISABLED_FEATURES = (
    "PasswordLeakDetection", "AutofillServerCommunication", "MediaRouter", "OptimizationHints", "Translate",
    "Prerender2", "NoStatePrefetch", "PreloadingHeuristics",
)
USER_LAUNCH_ARGS = tuple(a for a in LAUNCH_ARGS if not a.startswith("--disable-features=")) + (
    "--disable-features=" + ",".join(USER_DISABLED_FEATURES),
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--dns-prefetch-disable",
    "--disable-quic",
    # WI-9 (exfil.EXFIL_LAUNCH_ARGS): shared workers are off for user jobs by
    # design (plan 6.2 item 2).
    "--disable-blink-features=SharedWorker",
)
# Environment the user-pool driver must never see: the first would let
# localhost bypass the egress proxy.
_USER_SCRUBBED_ENV = ("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "PLAYWRIGHT_NODEJS_PATH",
                      "PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                      "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def windows_user_agent(chrome_version: str | None) -> str | None:
    """A real Windows Chrome user agent (reduced form) for the running major
    version. Never "HeadlessChrome"."""
    major = (chrome_version or "").split(".")[0]
    if not major.isdigit():
        return None
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


class WindowsJobObject:
    """A Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Only this process
    holds the handle (not inheritable), so when the worker dies for any reason
    the OS kills every process in the job. A no-op off Windows."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    def __init__(self) -> None:
        self.handle: Any = None
        self.supported = sys.platform == "win32"

    def create(self) -> bool:
        if not self.supported or self.handle is not None:
            return self.handle is not None
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            log.error("CreateJobObject failed: %s", ctypes.get_last_error())
            return False
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, self.JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            log.error("SetInformationJobObject failed: %s", ctypes.get_last_error())
            k32.CloseHandle(handle)
            return False
        self.handle = handle
        self._k32 = k32
        return True

    def assign_current_process(self) -> bool:
        if not self.create():
            return False
        import ctypes
        from ctypes import wintypes

        k32 = self._k32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        if not k32.AssignProcessToJobObject(self.handle, k32.GetCurrentProcess()):
            log.error("AssignProcessToJobObject failed: %s", ctypes.get_last_error())
            return False
        return True
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


class UserJobBrowser:
    """One user_browser job: its own Chromium process (a persistent context on
    a throwaway profile), its own egress proxy process and its folders.

    For WI-9 and WI-10: `context` (BrowserContext), `request` (the context's
    APIRequestContext, proxied), `pages()`, `install_guard(guard)` (await it
    before any page is used: the context aborts every request until then),
    `model_page()`, `guard`, `egress` (EgressProcess: `refusals`, `alive`),
    `downloads_dir` (Playwright saves downloads there), `job_dir`,
    `user_agent`, `headless`, `relaunch(headless=...)` (for `needs_headed`
    sites: same profile, same proxy, guard installed again), `close()`."""

    def __init__(self, provider: "ChromiumProvider", job: ClaimedJob, *, job_dir: Path, downloads_dir: Path,
                 egress: Any, headless: bool):
        self.provider = provider
        self.job = job
        self.agent_type = job.agent_type
        self.job_dir = job_dir
        self.profile_dir = job_dir / "profile"
        self.tmp_dir = job_dir / "tmp"
        self.downloads_dir = downloads_dir
        self.egress = egress
        self.headless = headless
        self.context: Any = None
        self.guard: Any = None
        self.page: Any = None
        self.user_agent: str | None = None
        self._closed = False
        self._close_failed = False

    @property
    def request(self) -> Any:
        return self.context.request

    def pages(self) -> list[Any]:
        return list(self.context.pages) if self.context is not None else []

    def attach_steps(self, log_step: Callable[..., Any]) -> None:
        if self.guard is not None and hasattr(self.guard, "log_step"):
            self.guard.log_step = log_step

    async def install_guard(self, guard: Any) -> None:
        """Install the job's guard (anything with `async install(context)`),
        then lift the deny-all route. The guard is installed first, so there is
        no moment without a route. Kept for relaunch."""
        install = getattr(guard, "install", None)
        if install is None:
            raise RuntimeError("user_browser guard has no install()")
        if self.context is None:
            raise RuntimeError("job browser closed")
        await install(self.context)
        self.guard = guard
        with contextlib.suppress(Exception):
            await self.context.unroute("**/*", _deny_all_route)

    async def model_page(self) -> Any:
        if self.guard is None:
            raise RuntimeError("no guard installed on this job's browser")
        if self.page is None or self.page.is_closed():
            open_pages = [p for p in self.pages() if not p.is_closed()]
            self.page = open_pages[0] if open_pages else await self.context.new_page()
        return self.page

    async def relaunch(self, *, headless: bool) -> None:
        await self.provider._relaunch_user(self, headless=headless)

    async def _close_context(self) -> None:
        context, self.context = self.context, None
        self.page = None
        if context is None:
            return
        try:
            if self.guard is not None and hasattr(self.guard, "settle"):
                await asyncio.wait_for(self.guard.settle(), 10)
        except Exception as exc:
            log.debug("guard settle failed: %s", exc)
        finally:
            try:
                await asyncio.wait_for(context.close(), 20)
            except Exception as exc:
                self._close_failed = True
                log.warning("closing the job's Chromium failed: %s", exc)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._close_context()
        finally:
            try:
                if self.egress is not None:
                    await self.egress.stop()
            finally:
                await asyncio.to_thread(self.provider._remove_job_dirs, self)


async def _deny_all_route(route: Any) -> None:
    with contextlib.suppress(Exception):
        await route.abort("blockedbyclient")


@dataclass
class UserPoolConfig:
    """What the user pool provider needs (agents/__init__.py builds it):
    `layout` and `selftest` from agents/user_browser_v2/selftest.py;
    optional `guard_factory(job, user_job_browser) -> guard` (sync or async;
    the guard must expose `async install(context)`, and `settle()` and
    `log_step` are optional), otherwise the runner calls
    `install_guard(guard)`; `egress_factory(job_id=..., marker=...,
    on_event=..., extra_blocked=...)` (EgressProcess by default)."""
    layout: Any
    selftest: Any
    guard_factory: Callable[[ClaimedJob, UserJobBrowser], Any] | None = None
    egress_factory: Callable[..., Any] | None = None
    headless: bool = True
    on_egress_event: Callable[[ClaimedJob, dict[str, Any]], None] | None = None


class ChromiumProvider:
    """BrowserProvider for the slot (slot.BrowserProvider protocol)."""

    def __init__(self, *, ceilings: CEILING_FACTORY, api_host: str = "", tmp_dir: Path | None = None,
                 headless: bool = True, proxy: dict[str, str] | None = None,
                 ignore_https_errors: bool = False, launch_timeout_ms: int = 60_000,
                 user_pool: UserPoolConfig | None = None):
        self.user_pool = user_pool
        self.job_object = WindowsJobObject()
        self._user_chrome_version: str | None = None
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
        if self.user_pool is not None:
            await self._start_user_pool()
            return
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

    async def _start_user_pool(self) -> None:
        up = self.user_pool
        assert up is not None
        # 1. Crash cleanup before any browser of this process exists.
        await asyncio.to_thread(up.selftest.cleanup)
        # 2. Everything started from here on dies with this process.
        if self.job_object.supported and not self.job_object.assign_current_process():
            log.error("could not put the worker in a kill-on-close Job Object")
        for name in _USER_SCRUBBED_ENV:
            os.environ.pop(name, None)
        node = up.layout.node_exe
        if node is not None:
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(node)
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
        # 3. Boot self-test (firewall, Defender, the real Chromium executable).
        report = await up.selftest.run_boot(self.chromium_probe)
        log.info("user pool self-test: claims %s, downloads %s", report.claims_allowed, report.downloads_allowed)

    def _user_executable(self) -> str | None:
        up = self.user_pool
        exe = up.layout.chrome_exe if up is not None else None
        if exe is None and sys.platform == "win32":
            # On the PC the stable path is mandatory (the firewall rules name it).
            raise RuntimeError("stable Chromium runtime missing")
        return str(exe) if exe is not None else None

    async def chromium_probe(self, executable: Path | None, url: str) -> str:
        """Launch the real per-job executable WITHOUT the proxy and load `url`.
        Returns the error text ("" when the page loaded). Also learns the
        Chromium version for the user agent."""
        # --no-proxy-server: the probe must connect directly (a system proxy
        # setting would otherwise hide what the firewall does).
        kwargs: dict[str, Any] = {"headless": True, "channel": "chromium",
                                  "args": [*USER_LAUNCH_ARGS, "--no-proxy-server"], "chromium_sandbox": True, "timeout": self._launch_timeout_ms,
                                  "handle_sigint": False, "handle_sigterm": False, "handle_sighup": False}
        if executable is not None:
            kwargs["executable_path"] = str(executable)
        browser = await self._pw.chromium.launch(**kwargs)
        try:
            self._user_chrome_version = browser.version
            self.chromium_version = browser.version
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=15_000)
            except Exception as exc:
                return str(exc)[:500]
            return ""
        finally:
            with contextlib.suppress(Exception):
                await browser.close()

    async def _user_version(self) -> str | None:
        if self._user_chrome_version is None:
            with contextlib.suppress(Exception):
                await self.chromium_probe(self.user_pool.layout.chrome_exe if self.user_pool else None,
                                          "about:blank")
        return self._user_chrome_version

    async def _new_user_context(self, job: ClaimedJob) -> UserJobBrowser:
        up = self.user_pool
        if up is None:
            raise RuntimeError("user_browser needs the user pool configuration")
        if job.principal_kind != "user" or not job.principal_user_id:
            raise ValueError("Personal job principal invalid")
        if self._pw is None:
            raise RuntimeError("browser provider not started")
        # Plan 10.1(3): the floor is proven at boot AND before every job, so a
        # rule deleted or disabled after boot stops new launches at once. The
        # python probe runs every time, the rules every rules_recheck_seconds;
        # a failure withdraws claims and require_claims raises
        # EgressFloorUnavailable (SetupUnavailable: uncharged requeue).
        check = getattr(up.selftest, "check_before_claim", None)
        if check is not None:
            await check()
        up.selftest.require_claims()
        job_dir = up.layout.job_dir(job.job_id)
        downloads = up.layout.quarantine_dir(job.job_id)
        for d in (job_dir, downloads):
            shutil.rmtree(d, ignore_errors=True)   # a requeued job claimed again here
        for d in (job_dir / "profile", job_dir / "tmp", downloads):
            d.mkdir(parents=True, exist_ok=True)
        egress_factory = up.egress_factory
        if egress_factory is None:
            from ..agents.user_browser_v2.egress import EgressProcess
            egress_factory = EgressProcess
        on_event = None
        if up.on_egress_event is not None:
            def on_event(event: dict[str, Any]) -> None:
                up.on_egress_event(job, event)
        # Office-owned public addresses (floor.json extra_blocked) are refused
        # by the proxy as well as by the firewall rules.
        extra = getattr(up.layout, "extra_blocked", None)
        egress = egress_factory(job_id=str(job.job_id), marker=str(job_dir), on_event=on_event,
                                extra_blocked=tuple(extra()) if extra is not None else ())
        ub = UserJobBrowser(self, job, job_dir=job_dir, downloads_dir=downloads, egress=egress,
                            headless=up.headless)
        try:
            await egress.start()
            await self._launch_user(ub)
            return ub
        except BaseException:
            await ub.close()
            raise

    def _chromium_env(self, ub: UserJobBrowser) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k not in _SCRUBBED_ENV and k not in _USER_SCRUBBED_ENV and not k.startswith("NODE_")}
        for var in ("TMPDIR", "TMP", "TEMP"):
            env[var] = str(ub.tmp_dir)
        return env

    async def _launch_user(self, ub: UserJobBrowser) -> None:
        up = self.user_pool
        assert up is not None
        ub.user_agent = windows_user_agent(await self._user_version())
        kwargs: dict[str, Any] = {
            "user_data_dir": str(ub.profile_dir), "channel": "chromium", "headless": ub.headless,
            "args": list(USER_LAUNCH_ARGS), "chromium_sandbox": True, "proxy": ub.egress.playwright_proxy,
            "downloads_path": str(ub.downloads_dir), "accept_downloads": True, "service_workers": "block",
            "viewport": VIEWPORT, "locale": "en-US", "timezone_id": "America/Los_Angeles",
            "java_script_enabled": True, "permissions": [], "ignore_https_errors": self._ignore_https_errors,
            "env": self._chromium_env(ub), "timeout": self._launch_timeout_ms,
            "handle_sigint": False, "handle_sigterm": False, "handle_sighup": False,
        }
        exe = self._user_executable()
        if exe is not None:
            kwargs["executable_path"] = exe
        if ub.user_agent:
            kwargs["user_agent"] = ub.user_agent
        context = await self._pw.chromium.launch_persistent_context(**kwargs)
        ub.context = context
        context.set_default_timeout(30_000)
        context.set_default_navigation_timeout(45_000)
        await context.route("**/*", _deny_all_route)
        guard = ub.guard
        if guard is None and up.guard_factory is not None:
            guard = up.guard_factory(ub.job, ub)
            if asyncio.iscoroutine(guard):
                guard = await guard
        if guard is not None:
            ub.guard = None
            await ub.install_guard(guard)

    async def _relaunch_user(self, ub: UserJobBrowser, *, headless: bool) -> None:
        if ub._closed:
            raise RuntimeError("job browser closed")
        if ub.egress is None or not ub.egress.alive:
            raise RuntimeError("egress proxy is not running")
        await ub._close_context()
        ub.headless = headless
        await self._launch_user(ub)

    def _remove_job_dirs(self, ub: UserJobBrowser) -> None:
        if self.user_pool is None:
            return
        if ub._close_failed:
            kill_stale_chromium(ub.job_dir)
        for d in (ub.job_dir, ub.downloads_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        if ub.job_dir.exists():
            # Something still holds the folder (a Chromium that did not exit).
            kill_stale_chromium(ub.job_dir)
            shutil.rmtree(ub.job_dir, ignore_errors=True)

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

    async def new_context(self, job: ClaimedJob) -> JobBrowser | UserJobBrowser:
        if job.agent_type == USER_BROWSER_TYPE:
            return await self._new_user_context(job)
        if job.principal_kind == "user" or job.agent_type == RETIRED_USER_TYPE:
            # The only personal type is user_browser (handled above); the v1
            # type is retired, so nothing else may build an org-style context
            # for a personal principal.
            raise ValueError("Personal job principal invalid")
        browser = await self._ensure_browser()
        guard = Guard(self._ceilings(job.agent_type, self.api_host))
        context = await browser.new_context(
            viewport=VIEWPORT, accept_downloads=False, service_workers="block", locale="en-US",
            timezone_id="America/Los_Angeles", java_script_enabled=True,
            ignore_https_errors=self._ignore_https_errors)
        try:
            context.set_default_timeout(30_000)
            context.set_default_navigation_timeout(45_000)
            await guard.install(context)
            jb = JobBrowser(context, guard, agent_type=job.agent_type)
            await jb.model_page()
            return jb
        except BaseException:
            await context.close()
            raise

    async def close_context(self, context: Any) -> None:
        if context is not None:     # JobBrowser, UserJobBrowser or a bare context
            await context.close()
