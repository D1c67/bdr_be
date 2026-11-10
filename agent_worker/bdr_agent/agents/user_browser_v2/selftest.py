"""Boot self-test and crash cleanup for the user pool (docs/ASSISTANT_BROWSER_V2_PLAN.md
10.1 layer 3, 10.6, 11.1). Signed worker code.

Worker 1.1.0 refuses to claim `user_browser` jobs unless the egress floor is
proven on this PC:
  (a) firewall rules: for each program the user pool runs (the real worker
      python.exe image, the stable Playwright driver node.exe and the stable
      Chromium), an enabled Outbound Block rule for every profile in group
      FIREWALL_GROUP covers exactly REQUIRED_REMOTE_RANGES (plus floor.json's
      office-owned `extra_blocked` ranges), plus one covering the LocalSubnet
      keyword; program paths must equal the resolved paths;
  (b) from each of the three processes, without the proxy, a TCP connect to
      the default gateway on a port it really serves (recorded by
      apply_user_pool_firewall.ps1 in <home>\\firewall\\floor.json) fails with
      the policy error WSAEACCES (10013). A timeout, a refusal or a connect is
      a failure, not a pass;
  (c) the Chromium check runs the real per-job browser executable (the probe
      is supplied by browser.py, which owns Playwright).
Downloads (11.1) additionally need Microsoft Defender: no readable exclusion
may cover the quarantine root, and an EICAR test file written there must be
detected (real-time removal, or MpCmdRun reporting a threat). A Defender
failure refuses downloads only; a firewall failure refuses claims.

Crash cleanup (10.6): `cleanup()` runs at worker start, before /hello. It
kills every process whose command line points into the jobs root (per-job
Chromium and the egress proxies, which carry `--marker <job dir>`) and deletes
every directory under the jobs root and the quarantine root.

Interface for browser.py, the supervisor and WI-10 (downloads.py):
    layout = UserPoolLayout.for_home(paths.home)
    st = UserPoolSelfTest(layout)              # or UserPoolSelfTest.from_env(layout)
    st.cleanup() -> CleanupReport
    await st.run_boot(chromium_probe)          # chromium_probe(chrome_exe, url) -> error text
    await st.check_before_claim() -> bool      # cheap; call before each claim cycle
    st.claims_allowed -> bool ; st.downloads_allowed -> bool ; st.report
    st.require_claims()                        # raises EgressFloorUnavailable
    find_mpcmdrun() -> Path | None             # Defender command line scanner
Off Windows nothing can be proven, so claims and downloads are refused unless
the developer sets BDR_AGENT_DEV_UNENFORCED_EGRESS=1 (honored only when
sys.platform is not win32; downloads stay refused).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...slot import SetupUnavailable

log = logging.getLogger("bdr_agent.user_browser_v2.selftest")

FIREWALL_GROUP = "BDR user pool floor"
DEV_UNENFORCED_ENV = "BDR_AGENT_DEV_UNENFORCED_EGRESS"
WSAEACCES = 10013
RUNTIME_MANIFEST = "runtime.json"
FLOOR_FILE = "floor.json"

# Must match $BlockedRanges in install/apply_user_pool_firewall.ps1 (a test
# pins the two lists together). Loopback is left open on purpose: the
# processes reach their local egress proxy there, and the proxy refuses
# loopback targets itself.
REQUIRED_REMOTE_RANGES = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128", "::ffff:0:0/96", "64:ff9b:1::/48", "2001:db8::/32", "fc00::/7", "fe80::/10", "fec0::/10",
    "ff00::/8",
    # NAT64 and 6to4 forms of the private IPv4 ranges.
    "64:ff9b::a00:0/104", "64:ff9b::ac10:0/108", "64:ff9b::c0a8:0/112", "64:ff9b::a9fe:0/112",
    "64:ff9b::6440:0/106", "64:ff9b::/104",
    "2002:a00::/24", "2002:ac10::/28", "2002:c0a8::/32", "2002:a9fe::/32", "2002:6440::/26", "2002::/24",
)
REQUIRED_KEYWORDS = ("LocalSubnet",)


class EgressFloorUnavailable(SetupUnavailable):
    """The floor is not proven on this PC: no user_browser job may start."""


# Layout --------------------------------------------------------------------------------------


def _default_quarantine_root(home: Path) -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "BDRQuarantine"
    return home.parent / (home.name + "-quarantine")


@dataclass(frozen=True)
class UserPoolLayout:
    """Folders of the user pool. The quarantine root is outside every worker
    home on purpose (README A6 recommends a Defender exclusion for the worker
    folder, which must never cover downloads)."""
    home: Path
    jobs_root: Path
    quarantine_root: Path
    runtime_dir: Path
    firewall_dir: Path

    @classmethod
    def for_home(cls, home: Path | str, *, quarantine_root: Path | str | None = None) -> UserPoolLayout:
        h = Path(home)
        return cls(home=h, jobs_root=h / "jobs",
                   quarantine_root=Path(quarantine_root) if quarantine_root else _default_quarantine_root(h),
                   runtime_dir=h / "runtime" / "current", firewall_dir=h / "firewall")

    def job_dir(self, job_id: Any) -> Path:
        return self.jobs_root / str(uuid.UUID(str(job_id)))

    def quarantine_dir(self, job_id: Any) -> Path:
        return self.quarantine_root / str(uuid.UUID(str(job_id)))

    def ensure(self) -> None:
        for d in (self.jobs_root, self.quarantine_root):
            d.mkdir(parents=True, exist_ok=True)

    # Stable runtime written by the launcher (bdr_launcher.release.sync_stable_runtime).

    def runtime_manifest(self) -> dict[str, Any] | None:
        try:
            data = json.loads((self.runtime_dir / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _runtime_file(self, key: str) -> Path | None:
        data = self.runtime_manifest() or {}
        rel = data.get(key)
        if not isinstance(rel, str) or not rel:
            return None
        root = self.runtime_dir.resolve()
        path = (self.runtime_dir / rel).resolve()
        if root not in path.parents or not path.is_file():
            return None
        return path

    @property
    def chrome_exe(self) -> Path | None:
        return self._runtime_file("chrome_exe")

    @property
    def node_exe(self) -> Path | None:
        return self._runtime_file("node_exe")

    def floor(self) -> dict[str, Any] | None:
        try:
            data = json.loads((self.firewall_dir / FLOOR_FILE).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def extra_blocked(self) -> tuple[str, ...]:
        """Office-owned public networks (the office WAN IPv4 and the site's
        routed IPv6 prefixes) that floor.json lists as `extra_blocked`. They
        are global addresses, so only this list blocks them: the firewall
        rules must carry them and every egress proxy refuses them."""
        from .egress import parse_networks

        values = (self.floor() or {}).get("extra_blocked")
        if isinstance(values, str):
            values = [values]
        return tuple(str(n) for n in parse_networks(values if isinstance(values, list) else []))


# Results ------------------------------------------------------------------------------------


@dataclass
class CheckResult:
    ok: bool
    detail: str = ""


@dataclass
class CleanupReport:
    killed: int = 0
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class SelfTestReport:
    at: datetime
    platform: str
    checks: dict[str, CheckResult] = field(default_factory=dict)
    claims_allowed: bool = False
    downloads_allowed: bool = False
    dev_unenforced: bool = False

    def summary(self) -> str:
        failed = [f"{k}: {v.detail}" for k, v in self.checks.items() if not v.ok]
        return "ok" if not failed else "; ".join(failed)[:1000]


# Process helpers ------------------------------------------------------------------------------

PowerShell = Callable[[str, float], tuple[int, str]]


def run_powershell(script: str, timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", script], capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return proc.returncode, proc.stdout


def running_python_image() -> Path:
    """The real image of this process. On Windows a venv python.exe is a
    redirector that starts the base interpreter, which is the process the
    firewall sees; GetModuleFileNameW reports it."""
    if sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))  # type: ignore[attr-defined]
        if n:
            return Path(buf.value)
    return Path(getattr(sys, "_base_executable", None) or sys.executable)


def norm_path(p: str | Path) -> str:
    text = os.path.expandvars(str(p))
    try:
        text = str(Path(text).resolve())
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(text))


def _networks(values: Any) -> tuple[set[Any], set[str]]:
    nets: set[Any] = set()
    words: set[str] = set()
    for v in values if isinstance(values, list) else [values]:
        if not isinstance(v, str):
            continue
        try:
            nets.add(ipaddress.ip_network(v.strip(), strict=False))
        except ValueError:
            words.add(v.strip().lower())
    return nets, words


def find_mpcmdrun() -> Path | None:
    """Defender's command line scanner: the newest platform copy, else the
    one under Program Files."""
    if sys.platform != "win32":
        return None
    platform_root = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows Defender" / "Platform"
    try:
        candidates = sorted((d for d in platform_root.iterdir() if (d / "MpCmdRun.exe").is_file()),
                            key=lambda d: [int(x) if x.isdigit() else 0 for x in d.name.split("-")[0].split(".")],
                            reverse=True)
    except OSError:
        candidates = []
    if candidates:
        return candidates[0] / "MpCmdRun.exe"
    fallback = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
    return fallback if fallback.is_file() else None


def exclusion_covers(exclusion: str, root: Path | str) -> bool:
    """True when a Defender ExclusionPath entry could cover `root` or anything
    under it. Wildcards are treated conservatively: everything from the first
    wildcard on matches anything."""
    text = os.path.expandvars(exclusion.strip().strip('"'))
    if not text:
        return False
    cut = min([i for i in (text.find("*"), text.find("?")) if i >= 0], default=-1)
    root_n = os.path.normcase(os.path.normpath(os.path.expandvars(str(root))))
    if cut >= 0:
        prefix = os.path.normcase(text[:cut].replace("/", os.sep))
        return root_n.startswith(prefix) or prefix.startswith(root_n)
    ex_n = os.path.normcase(os.path.normpath(text))
    return root_n == ex_n or root_n.startswith(ex_n.rstrip("\\/") + os.sep)


def _eicar() -> bytes:
    # Built at run time so no worker source file carries the test signature
    # (Defender would quarantine the worker itself).
    left = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$"
    right = "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    return (left + right).encode("ascii")


ChromiumProbe = Callable[[Path | None, str], Awaitable[str]]


# The self-test ----------------------------------------------------------------------------------


_RULES_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($r in @(Get-NetFirewallRule -Group '%GROUP%' -ErrorAction SilentlyContinue)) {
  $app = $r | Get-NetFirewallApplicationFilter
  $addr = $r | Get-NetFirewallAddressFilter
  $out += [pscustomobject]@{ name = [string]$r.Name; enabled = [string]$r.Enabled;
    direction = [string]$r.Direction; action = [string]$r.Action; profile = [string]$r.Profile;
    program = [string]$app.Program; remote = @($addr.RemoteAddress | ForEach-Object { [string]$_ }) }
}
ConvertTo-Json -InputObject @($out) -Depth 4 -Compress
"""

_DEFENDER_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-MpPreference
$s = Get-MpComputerStatus
ConvertTo-Json -Compress -InputObject ([pscustomobject]@{
  exclusions = @($p.ExclusionPath | ForEach-Object { [string]$_ });
  realtime = [bool]$s.RealTimeProtectionEnabled; antivirus = [bool]$s.AntivirusEnabled })
"""

_NODE_PROBE = (
    "const s=require('net').connect({host:process.argv[1],port:+process.argv[2]});"
    "s.on('connect',()=>{console.log('CONNECTED');process.exit(0)});"
    "s.on('error',e=>{console.log(e.code||'ERROR');process.exit(0)});"
    "setTimeout(()=>{console.log('TIMEOUT');process.exit(0)},5000);"
)


class UserPoolSelfTest:
    def __init__(self, layout: UserPoolLayout, *, powershell: PowerShell = run_powershell,
                 platform: str | None = None, dev_unenforced: bool = False,
                 python_image: Callable[[], Path] = running_python_image,
                 connect: Callable[[str, int, float], None] | None = None,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 rules_recheck_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.layout = layout
        self.platform = platform or sys.platform
        self._ps = powershell
        self.dev_unenforced = bool(dev_unenforced) and self.platform != "win32"
        self._python_image = python_image
        self._connect = connect or self._socket_connect
        self._run = run
        self.rules_recheck_seconds = rules_recheck_seconds
        self._clock = clock
        self._rules_checked_at: float | None = None
        self.report: SelfTestReport | None = None

    @classmethod
    def from_env(cls, layout: UserPoolLayout, **kw: Any) -> UserPoolSelfTest:
        dev = os.environ.get(DEV_UNENFORCED_ENV) == "1" and sys.platform != "win32"
        return cls(layout, dev_unenforced=dev, **kw)

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    @property
    def claims_allowed(self) -> bool:
        return bool(self.report and self.report.claims_allowed)

    @property
    def downloads_allowed(self) -> bool:
        return bool(self.report and self.report.downloads_allowed)

    def require_claims(self) -> None:
        if not self.claims_allowed:
            raise EgressFloorUnavailable(
                "user pool egress floor not proven: " + (self.report.summary() if self.report else "self-test not run"))

    # Crash cleanup ------------------------------------------------------------------------

    def cleanup(self) -> CleanupReport:
        """Kill leftovers of a dead worker and delete their folders. Call at
        worker start, before /hello, before any Chromium of this process."""
        from ...browser.browser import kill_stale_chromium

        report = CleanupReport()
        self.layout.ensure()
        report.killed = kill_stale_chromium(self.layout.jobs_root)
        for root in (self.layout.jobs_root, self.layout.quarantine_root):
            try:
                entries = list(root.iterdir())
            except OSError as exc:
                report.errors.append(f"{root}: {exc}")
                continue
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, onexc=_force_remove)
                    else:
                        entry.unlink()
                    report.removed.append(str(entry))
                except OSError as exc:
                    report.errors.append(f"{entry.name}: {exc}")
        if report.killed or report.removed:
            log.warning("user pool cleanup: killed %d process(es), removed %d folder(s)",
                        report.killed, len(report.removed))
        return report

    # Checks ---------------------------------------------------------------------------------

    def _required_programs(self) -> dict[str, Path | None]:
        return {"python": self._python_image(), "node": self.layout.node_exe, "chrome": self.layout.chrome_exe}

    def check_rules(self) -> CheckResult:
        programs = self._required_programs()
        missing = [k for k, v in programs.items() if v is None]
        if missing:
            return CheckResult(False, f"stable runtime missing: {', '.join(missing)} (launcher sync)")
        code, out = self._ps(_RULES_SCRIPT.replace("%GROUP%", FIREWALL_GROUP), 60.0)
        if code != 0:
            return CheckResult(False, f"cannot read firewall rules (exit {code})")
        try:
            rules = json.loads(out or "[]")
        except ValueError:
            return CheckResult(False, "firewall rules output unreadable")
        if isinstance(rules, dict):
            rules = [rules]
        required_nets, _ = _networks(list(REQUIRED_REMOTE_RANGES) + list(self.layout.extra_blocked()))
        problems: list[str] = []
        for key, prog in programs.items():
            want = norm_path(prog)  # type: ignore[arg-type]
            mine = [r for r in rules if isinstance(r, dict) and norm_path(r.get("program") or "") == want
                    and str(r.get("enabled")).lower() == "true" and str(r.get("direction")).lower() == "outbound"
                    and str(r.get("action")).lower() == "block" and str(r.get("profile")).lower() == "any"]
            nets: set[Any] = set()
            words: set[str] = set()
            for r in mine:
                n, w = _networks(r.get("remote"))
                nets |= n
                words |= w
            if not required_nets <= nets:
                problems.append(f"{key}: ranges incomplete ({len(required_nets - nets)} missing)")
            if not {w.lower() for w in REQUIRED_KEYWORDS} <= words:
                problems.append(f"{key}: LocalSubnet rule missing")
        return CheckResult(not problems, "; ".join(problems))

    @staticmethod
    def _socket_connect(host: str, port: int, timeout: float) -> None:
        with socket.create_connection((host, port), timeout=timeout):
            pass

    def _gateway(self) -> tuple[str, list[int]] | None:
        floor = self.layout.floor() or {}
        gw = floor.get("gateway")
        ports = [p for p in floor.get("gateway_ports") or [] if isinstance(p, int) and 0 < p < 65536]
        if not isinstance(gw, str) or not ports:
            return None
        try:
            ipaddress.ip_address(gw)
        except ValueError:
            return None
        return gw, ports

    def check_python_blocked(self) -> CheckResult:
        target = self._gateway()
        if target is None:
            return CheckResult(False, "floor.json missing or has no gateway (run apply_user_pool_firewall.ps1)")
        gw, ports = target
        try:
            self._connect(gw, ports[0], 5.0)
        except PermissionError as exc:
            if getattr(exc, "winerror", None) == WSAEACCES or exc.errno in (13, WSAEACCES):
                return CheckResult(True, "WSAEACCES")
            return CheckResult(False, f"permission error {exc.errno}")
        except TimeoutError:
            return CheckResult(False, "timeout (not a policy block)")
        except ConnectionRefusedError:
            return CheckResult(False, "refused (not a policy block)")
        except OSError as exc:
            if getattr(exc, "winerror", None) == WSAEACCES:
                return CheckResult(True, "WSAEACCES")
            return CheckResult(False, f"{type(exc).__name__} (not a policy block)")
        return CheckResult(False, "connected to the gateway: not blocked")

    def check_node_blocked(self) -> CheckResult:
        target = self._gateway()
        node = self.layout.node_exe
        if target is None or node is None:
            return CheckResult(False, "gateway or stable node.exe missing")
        gw, ports = target
        try:
            proc = self._run([str(node), "-e", _NODE_PROBE, gw, str(ports[0])], capture_output=True, text=True,
                             timeout=30, check=False, env={k: v for k, v in os.environ.items()
                                                           if not k.startswith("NODE_")})
        except (OSError, subprocess.SubprocessError) as exc:
            return CheckResult(False, f"node probe failed to run: {type(exc).__name__}")
        word = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        if word[0] == "EACCES":
            return CheckResult(True, "EACCES")
        return CheckResult(False, f"node: {word[0] or 'no output'} (not a policy block)")

    async def check_chromium_blocked(self, probe: ChromiumProbe | None) -> CheckResult:
        target = self._gateway()
        if target is None or probe is None:
            return CheckResult(False, "gateway or Chromium probe missing")
        gw, ports = target
        web = [p for p in ports if p in (80, 443)]
        if not web:
            return CheckResult(False, "the gateway serves no web port for the Chromium check")
        scheme = "https" if web[0] == 443 else "http"
        host = f"[{gw}]" if ":" in gw else gw
        try:
            error = await probe(self.layout.chrome_exe, f"{scheme}://{host}:{web[0]}/")
        except Exception as exc:
            return CheckResult(False, f"Chromium probe crashed: {type(exc).__name__}")
        if "ERR_NETWORK_ACCESS_DENIED" in error or "ERR_ACCESS_DENIED" in error:
            return CheckResult(True, "ERR_NETWORK_ACCESS_DENIED")
        return CheckResult(False, f"chromium: {error[:120] or 'loaded'} (not a policy block)")

    def check_defender(self) -> CheckResult:
        root = self.layout.quarantine_root
        code, out = self._ps(_DEFENDER_SCRIPT, 60.0)
        try:
            data = json.loads(out) if code == 0 and out.strip() else {}
        except ValueError:
            data = {}
        exclusions = [e for e in (data.get("exclusions") or []) if isinstance(e, str)]
        for ex in exclusions:
            if ex.startswith("N/A"):
                continue   # non-admins cannot read exclusions; the EICAR test decides
            if exclusion_covers(ex, root):
                return CheckResult(False, f"a Defender exclusion covers the quarantine root ({ex})")
        return self._eicar_detected()

    def _eicar_detected(self) -> CheckResult:
        work = self.layout.quarantine_root / f"selftest-{uuid.uuid4().hex}"
        try:
            work.mkdir(parents=True)
        except OSError as exc:
            return CheckResult(False, f"cannot write the quarantine root: {exc}")
        target = work / "eicar-test.txt"
        try:
            try:
                target.write_bytes(_eicar())
            except PermissionError:
                return CheckResult(True, "real-time protection blocked the test file")
            for _ in range(20):
                time.sleep(0.25)
                try:
                    if not target.exists() or target.read_bytes() != _eicar():
                        return CheckResult(True, "real-time protection removed the test file")
                except OSError:
                    return CheckResult(True, "real-time protection locked the test file")
            scanner = find_mpcmdrun()
            if scanner is None:
                return CheckResult(False, "Defender scanner MpCmdRun.exe not found")
            try:
                proc = self._run([str(scanner), "-Scan", "-ScanType", "3", "-File", str(target),
                                  "-DisableRemediation"], capture_output=True, text=True, timeout=120, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                return CheckResult(False, f"Defender scan did not run: {type(exc).__name__}")
            if proc.returncode == 2:
                return CheckResult(True, "MpCmdRun reported the test file")
            return CheckResult(False, f"Defender did not detect the test file (exit {proc.returncode})")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # Runs -----------------------------------------------------------------------------------------

    async def run_boot(self, chromium_probe: ChromiumProbe | None = None) -> SelfTestReport:
        report = SelfTestReport(at=datetime.now(UTC), platform=self.platform)
        if not self.windows:
            report.dev_unenforced = self.dev_unenforced
            report.checks["platform"] = CheckResult(
                self.dev_unenforced, "dev only: the floor is not enforced off Windows" if self.dev_unenforced
                else "the egress floor can only be proven on Windows")
            report.claims_allowed = self.dev_unenforced
            report.downloads_allowed = False
            self.report = report
            log.warning("user pool self-test: %s", report.summary())
            return report
        report.checks["rules"] = await asyncio.to_thread(self.check_rules)
        self._rules_checked_at = self._clock()
        report.checks["python"] = await asyncio.to_thread(self.check_python_blocked)
        report.checks["node"] = await asyncio.to_thread(self.check_node_blocked)
        report.checks["chromium"] = await self.check_chromium_blocked(chromium_probe)
        floor_ok = all(report.checks[k].ok for k in ("rules", "python", "node", "chromium"))
        report.checks["defender"] = await asyncio.to_thread(self.check_defender)
        report.claims_allowed = floor_ok
        report.downloads_allowed = floor_ok and report.checks["defender"].ok
        self.report = report
        (log.info if floor_ok else log.error)("user pool self-test: %s", report.summary())
        return report

    async def check_before_claim(self) -> bool:
        """Before each claim cycle: the python probe every time (one refused
        connect), the firewall rules again at most every rules_recheck_seconds.
        A failure withdraws claims until the next boot self-test."""
        if self.report is None or not self.report.claims_allowed:
            return False
        if not self.windows:
            return self.report.claims_allowed
        result = await asyncio.to_thread(self.check_python_blocked)
        if result.ok and (self._rules_checked_at is None
                          or self._clock() - self._rules_checked_at >= self.rules_recheck_seconds):
            result = await asyncio.to_thread(self.check_rules)
            self._rules_checked_at = self._clock()
        if not result.ok:
            self.report.checks["claim_cycle"] = result
            self.report.claims_allowed = False
            self.report.downloads_allowed = False
            log.error("user pool floor check failed before a claim: %s", result.detail)
        return self.report.claims_allowed


def _force_remove(func: Callable[..., Any], path: str, _exc: Any) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
        func(path)
