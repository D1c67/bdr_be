"""WI-8: the user pool boot self-test, crash cleanup and stable runtime
(plan 10.1 layers 2 and 3, 10.6, 11.1, 15.5).

No Windows here: PowerShell, the socket probe, node and Defender are fakes
with the exact shapes the Windows calls return. The crash test kills a real
worker process mid-job (with a real egress child and a real per-job Chromium
when Playwright is installed), restarts the cleanup and checks that the job
folders and every process are gone.
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from bdr_agent.agents.user_browser_v2 import selftest as S
from bdr_agent.slot import SetupUnavailable

from .agent_harness import needs_browser

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "install" / "apply_user_pool_firewall.ps1"


@pytest.fixture
def layout(tmp_path):
    lay = S.UserPoolLayout.for_home(tmp_path / "BDRAgentUser", quarantine_root=tmp_path / "BDRQuarantine")
    lay.ensure()
    rt = lay.runtime_dir
    (rt / "driver").mkdir(parents=True)
    (rt / "chrome").mkdir(parents=True)
    (rt / "driver" / "node.exe").write_bytes(b"node")
    (rt / "chrome" / "chrome.exe").write_bytes(b"chrome")
    (rt / "runtime.json").write_text(json.dumps({"format": 1, "version": "1.1.0", "node_exe": "driver/node.exe",
                                                 "chrome_exe": "chrome/chrome.exe"}))
    lay.firewall_dir.mkdir(parents=True)
    (lay.firewall_dir / "floor.json").write_text("\ufeff" + json.dumps(
        {"format": 1, "gateway": "192.168.1.1", "gateway_ports": [80, 53]}), encoding="utf-8")
    return lay


PYTHON = Path("/opt/python/python.exe")


def rules_for(layout, *, drop=None, python=PYTHON, netmask=True, **override):
    out = []
    ranges = list(S.REQUIRED_REMOTE_RANGES)
    if netmask:   # Windows reads IPv4 CIDR back as address/netmask
        ranges = [{"10.0.0.0/8": "10.0.0.0/255.0.0.0", "192.168.0.0/16": "192.168.0.0/255.255.0.0"}.get(r, r)
                  for r in ranges]
    for key, prog in (("python", python), ("node", layout.node_exe), ("chrome", layout.chrome_exe)):
        if key == drop:
            continue
        base = {"enabled": "True", "direction": "Outbound", "action": "Block", "profile": "Any", "program": str(prog)}
        base.update(override)
        out.append(dict(base, name=f"r-{key}", remote=ranges))
        out.append(dict(base, name=f"s-{key}", remote=["LocalSubnet"]))
    return out


def fake_ps(rules=None, defender=None, code=0):
    def ps(script, timeout):
        if "Get-NetFirewallRule" in script:
            return code, json.dumps(rules if rules is not None else [])
        if "Get-MpPreference" in script:
            return 0, json.dumps(defender or {"exclusions": [], "realtime": True, "antivirus": True})
        raise AssertionError(script)
    return ps


def wsaeacces(*_):
    exc = PermissionError(13, "An attempt was made to access a socket in a way forbidden by its access permissions")
    exc.winerror = S.WSAEACCES
    raise exc


def node_run(word):
    def run(cmd, **kw):
        if cmd[0].endswith("node.exe"):
            return subprocess.CompletedProcess(cmd, 0, stdout=word + "\n", stderr="")
        if "MpCmdRun" in cmd[0]:
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="")
        raise AssertionError(cmd)
    return run


async def denied_probe(exe, url):
    return f"page.goto: net::ERR_NETWORK_ACCESS_DENIED at {url}"


def win_selftest(layout, **kw):
    args = dict(powershell=fake_ps(rules_for(layout)), platform="win32", python_image=lambda: PYTHON,
                connect=wsaeacces, run=node_run("EACCES"))
    args.update(kw)
    return S.UserPoolSelfTest(layout, **args)


# Layout ------------------------------------------------------------------------------------------


def test_layout_paths_and_runtime_manifest(layout, tmp_path):
    job = "9b2b0f8e-2c62-4d38-9d0e-0c5d2b0a1e11"
    assert layout.job_dir(job) == layout.jobs_root / job
    assert layout.quarantine_dir(job) == tmp_path / "BDRQuarantine" / job
    with pytest.raises(ValueError):
        layout.job_dir("../../evil")
    assert layout.node_exe.name == "node.exe" and layout.chrome_exe.name == "chrome.exe"
    (layout.runtime_dir / "runtime.json").write_text(json.dumps({"node_exe": "../../../etc/passwd",
                                                                 "chrome_exe": "chrome/missing.exe"}))
    assert layout.node_exe is None and layout.chrome_exe is None
    assert layout.floor()["gateway"] == "192.168.1.1"


def test_quarantine_is_outside_the_worker_home_by_default(tmp_path):
    lay = S.UserPoolLayout.for_home(tmp_path / "BDRAgentUser")
    assert lay.home not in lay.quarantine_root.parents and lay.quarantine_root != lay.home


# Firewall rules ----------------------------------------------------------------------------------


def test_rules_check_passes_with_netmask_forms(layout):
    assert win_selftest(layout).check_rules().ok


@pytest.mark.parametrize(("change", "needle"), [
    ({"drop": "node"}, "node: ranges incomplete"),
    ({"python": Path("/other/python.exe")}, "python: ranges incomplete"),
    ({"enabled": "False"}, "ranges incomplete"),
    ({"action": "Allow"}, "ranges incomplete"),
    ({"profile": "Domain"}, "ranges incomplete"),
    ({"direction": "Inbound"}, "ranges incomplete"),
])
def test_rules_check_fails_on_any_gap(layout, change, needle):
    st = win_selftest(layout, powershell=fake_ps(rules_for(layout, **change)))
    result = st.check_rules()
    assert not result.ok and needle in result.detail


def test_rules_check_needs_every_range_and_local_subnet(layout):
    rules = rules_for(layout)
    rules[0]["remote"] = rules[0]["remote"][:-1]
    assert "ranges incomplete (1 missing)" in win_selftest(layout, powershell=fake_ps(rules)).check_rules().detail
    rules = [r for r in rules_for(layout) if r["remote"] != ["LocalSubnet"]]
    assert "LocalSubnet" in win_selftest(layout, powershell=fake_ps(rules)).check_rules().detail
    assert not win_selftest(layout, powershell=fake_ps(code=1)).check_rules().ok
    (layout.runtime_dir / "runtime.json").unlink()
    assert "stable runtime missing" in win_selftest(layout).check_rules().detail


def test_rules_check_requires_the_office_ranges_from_floor_json(layout):
    """Review WI-8: the office WAN IPv4 and the site IPv6 prefix are global, so
    only floor.json extra_blocked covers them; the rules must carry them."""
    floor = layout.floor()
    floor["extra_blocked"] = ["203.0.113.7/32", "2001:db8:1200::/56", "not a range"]
    (layout.firewall_dir / "floor.json").write_text(json.dumps(floor), encoding="utf-8")
    assert layout.extra_blocked() == ("203.0.113.7/32", "2001:db8:1200::/56")
    result = win_selftest(layout).check_rules()
    assert not result.ok and "ranges incomplete (2 missing)" in result.detail
    rules = rules_for(layout)
    for r in rules:
        if r["remote"] != ["LocalSubnet"]:
            r["remote"] = r["remote"] + ["203.0.113.7", "2001:db8:1200::/56"]
    assert win_selftest(layout, powershell=fake_ps(rules)).check_rules().ok


# Connect probes -----------------------------------------------------------------------------------


def test_python_probe_requires_the_policy_error(layout):
    assert win_selftest(layout).check_python_blocked().ok

    def raises(exc):
        def f(*_):
            raise exc
        return f

    for exc, word in ((TimeoutError(), "timeout"), (ConnectionRefusedError(), "refused"),
                      (OSError(65, "unreachable"), "not a policy block")):
        result = win_selftest(layout, connect=raises(exc)).check_python_blocked()
        assert not result.ok and word in result.detail
    result = win_selftest(layout, connect=lambda *_: None).check_python_blocked()
    assert not result.ok and "not blocked" in result.detail
    (layout.firewall_dir / "floor.json").unlink()
    assert "floor.json" in win_selftest(layout).check_python_blocked().detail


def test_node_probe_requires_eacces(layout):
    assert win_selftest(layout).check_node_blocked().ok
    for word in ("ECONNREFUSED", "TIMEOUT", "CONNECTED", ""):
        assert not win_selftest(layout, run=node_run(word)).check_node_blocked().ok


async def test_chromium_probe_requires_network_access_denied_on_a_web_port(layout):
    seen = []

    async def probe(exe, url):
        seen.append((exe, url))
        return "net::ERR_NETWORK_ACCESS_DENIED"

    assert (await win_selftest(layout).check_chromium_blocked(probe)).ok
    assert seen == [(layout.chrome_exe, "http://192.168.1.1:80/")]

    async def refused(exe, url):
        return "net::ERR_CONNECTION_REFUSED"

    async def loaded(exe, url):
        return ""

    for p in (refused, loaded, None):
        assert not (await win_selftest(layout).check_chromium_blocked(p)).ok
    (layout.firewall_dir / "floor.json").write_text(json.dumps({"gateway": "192.168.1.1", "gateway_ports": [53]}))
    assert "no web port" in (await win_selftest(layout).check_chromium_blocked(probe)).detail


# Defender ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("exclusion", "covers"), [
    (r"C:\ProgramData\BDRQuarantine", True), (r"C:\ProgramData", True), (r"C:\ProgramData\BDR*", True),
    (r"C:\ProgramData\BDRQuarantine\sub*", True), (r"C:\ProgramData\BDRAgentUser", False),
    (r"D:\Other", False), ("", False),
])
def test_exclusion_cover_rules(exclusion, covers):
    root = r"C:\ProgramData\BDRQuarantine"
    if os.sep == "/":
        exclusion = exclusion.replace("C:\\", "/c/").replace("D:\\", "/d/").replace("\\", "/")
        root = "/c/ProgramData/BDRQuarantine"
    assert S.exclusion_covers(exclusion, root) is covers


def test_defender_check_refuses_downloads_when_an_exclusion_covers_the_quarantine(layout):
    st = win_selftest(layout, powershell=fake_ps(rules_for(layout), defender={
        "exclusions": [str(layout.quarantine_root.parent)]}))
    result = st.check_defender()
    assert not result.ok and "exclusion" in result.detail


def test_defender_check_uses_eicar_when_exclusions_are_hidden(layout, monkeypatch):
    monkeypatch.setattr(S, "find_mpcmdrun", lambda: Path("C:/Defender/MpCmdRun.exe"))
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)
    hidden = {"exclusions": ["N/A: Must be an administrator to view exclusions"]}
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        assert Path(cmd[5]).read_bytes().startswith(b"X5O!P%@AP")
        return subprocess.CompletedProcess(cmd, 2, "", "")

    st = win_selftest(layout, powershell=fake_ps(rules_for(layout), defender=hidden), run=run)
    assert st.check_defender().ok
    assert calls and calls[0][1:5] == ["-Scan", "-ScanType", "3", "-File"] and "-DisableRemediation" in calls[0]
    assert list(layout.quarantine_root.iterdir()) == []       # the test file is gone

    st = win_selftest(layout, powershell=fake_ps(rules_for(layout), defender=hidden),
                      run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    assert not st.check_defender().ok
    monkeypatch.setattr(S, "find_mpcmdrun", lambda: None)
    assert "not found" in win_selftest(layout, powershell=fake_ps(rules_for(layout), defender=hidden)
                                       ).check_defender().detail


def test_no_worker_source_carries_the_eicar_signature():
    whole = S._eicar().decode()
    for path in (ROOT / "bdr_agent").rglob("*.py"):
        assert whole not in path.read_text(encoding="utf-8"), path
    for path in (ROOT / "tests").rglob("*.py"):
        assert whole not in path.read_text(encoding="utf-8"), path


# Boot runs and the claim gate -----------------------------------------------------------------------


async def test_boot_self_test_on_windows_allows_claims_and_downloads(layout, monkeypatch):
    monkeypatch.setattr(S.UserPoolSelfTest, "check_defender", lambda self: S.CheckResult(True, "ok"))
    st = win_selftest(layout)
    report = await st.run_boot(denied_probe)
    assert report.claims_allowed and report.downloads_allowed and st.claims_allowed
    st.require_claims()
    assert set(report.checks) == {"rules", "python", "node", "chromium", "defender"}


async def test_firewall_failure_refuses_claims_and_downloads(layout, monkeypatch):
    monkeypatch.setattr(S.UserPoolSelfTest, "check_defender", lambda self: S.CheckResult(True, "ok"))
    st = win_selftest(layout, run=node_run("ECONNREFUSED"))
    report = await st.run_boot(denied_probe)
    assert not report.claims_allowed and not report.downloads_allowed
    with pytest.raises(S.EgressFloorUnavailable) as info:
        st.require_claims()
    assert isinstance(info.value, SetupUnavailable) and "node" in str(info.value)


async def test_defender_failure_refuses_downloads_only(layout, monkeypatch):
    monkeypatch.setattr(S.UserPoolSelfTest, "check_defender", lambda self: S.CheckResult(False, "off"))
    report = await win_selftest(layout).run_boot(denied_probe)
    assert report.claims_allowed and not report.downloads_allowed


async def test_off_windows_nothing_is_proven_unless_the_dev_switch_is_on(layout, monkeypatch):
    st = S.UserPoolSelfTest(layout, platform="darwin")
    report = await st.run_boot(None)
    assert not report.claims_allowed and not report.downloads_allowed
    with pytest.raises(S.EgressFloorUnavailable):
        st.require_claims()
    dev = S.UserPoolSelfTest(layout, platform="darwin", dev_unenforced=True)
    assert (await dev.run_boot(None)).claims_allowed and not dev.downloads_allowed
    assert await dev.check_before_claim()
    # The switch never counts on Windows.
    assert S.UserPoolSelfTest(layout, platform="win32", dev_unenforced=True).dev_unenforced is False
    monkeypatch.setenv(S.DEV_UNENFORCED_ENV, "1")
    assert S.UserPoolSelfTest.from_env(layout).dev_unenforced is (sys.platform != "win32")


async def test_check_before_claim_reprobes_and_rechecks_rules_on_a_timer(layout, monkeypatch):
    monkeypatch.setattr(S.UserPoolSelfTest, "check_defender", lambda self: S.CheckResult(True, "ok"))
    now = [0.0]
    ps_calls = []
    ps = fake_ps(rules_for(layout))

    def counting_ps(script, timeout):
        ps_calls.append(script)
        return ps(script, timeout)

    connects = []

    def connect(*args):
        connects.append(args)
        wsaeacces()

    st = win_selftest(layout, powershell=counting_ps, connect=connect, clock=lambda: now[0],
                      rules_recheck_seconds=60)
    assert st.claims_allowed is False
    assert await st.check_before_claim() is False           # never before the boot test
    await st.run_boot(denied_probe)
    rules_reads = len(ps_calls)
    assert await st.check_before_claim()
    assert len(ps_calls) == rules_reads and len(connects) == 2
    now[0] = 61
    assert await st.check_before_claim()
    assert len(ps_calls) == rules_reads + 1

    def open_connect(*_):
        return None

    st._connect = open_connect                               # someone deleted the rules
    assert await st.check_before_claim() is False
    assert not st.claims_allowed and not st.downloads_allowed and "claim_cycle" in st.report.checks


# Crash cleanup --------------------------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie child of ours still answers kill 0; check its state.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_gone(pids, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


FAKE_WORKER = textwrap.dedent("""
    import asyncio, json, subprocess, sys, uuid
    from pathlib import Path
    from bdr_agent.agents.user_browser_v2.egress import EgressProcess
    from bdr_agent.agents.user_browser_v2.selftest import UserPoolLayout

    home, quarantine, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    layout = UserPoolLayout.for_home(home, quarantine_root=quarantine)
    job = uuid.uuid4()

    async def main():
        jd = layout.job_dir(job)
        (jd / "profile").mkdir(parents=True)
        (jd / "profile" / "Cookies").write_text("session")
        layout.quarantine_dir(job).mkdir(parents=True)
        (layout.quarantine_dir(job) / "download.pdf").write_bytes(b"%PDF-1.7")
        egress = EgressProcess(job_id=str(job), marker=jd)
        await egress.start()
        chrome = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                   f"--user-data-dir={jd / 'profile'}"])
        out.write_text(json.dumps({"egress": egress.pid, "chrome": chrome.pid, "job": str(job)}))
        await asyncio.sleep(120)

    asyncio.run(main())
""")


def _run_fake_worker(tmp_path, script):
    home, quarantine, out = tmp_path / "BDRAgentUser", tmp_path / "BDRQuarantine", tmp_path / "pids.json"
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(script)
    worker = subprocess.Popen([sys.executable, str(worker_py), str(home), str(quarantine), str(out)],
                              cwd=str(ROOT))
    end = time.monotonic() + 60
    while not out.exists() and time.monotonic() < end and worker.poll() is None:
        time.sleep(0.1)
    assert out.exists(), "fake worker did not start"
    return worker, json.loads(out.read_text()), S.UserPoolLayout.for_home(home, quarantine_root=quarantine)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill; the Job Object covers this on Windows")
def test_killing_the_worker_mid_job_then_restarting_leaves_no_folder_or_process(tmp_path):
    worker, pids, layout = _run_fake_worker(tmp_path, FAKE_WORKER)
    try:
        assert layout.job_dir(pids["job"]).is_dir()
        os.kill(worker.pid, signal.SIGKILL)                  # power cut, WinSW kill, python crash
        worker.wait(timeout=10)
        assert _alive(pids["chrome"]), "the orphan must still be running before cleanup"
        report = S.UserPoolSelfTest(layout, platform="darwin").cleanup()   # the next worker start
        assert report.killed >= 1
        assert _wait_gone([pids["chrome"], pids["egress"]])
        assert list(layout.jobs_root.iterdir()) == [] and list(layout.quarantine_root.iterdir()) == []
        assert report.errors == []
    finally:
        for pid in (pids.get("chrome"), pids.get("egress")):
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)
        if worker.poll() is None:
            worker.kill()


REAL_WORKER = textwrap.dedent("""
    import asyncio, json, subprocess, sys, uuid
    from datetime import UTC, datetime, timedelta
    from pathlib import Path
    from bdr_agent.agents.user_browser_v2.selftest import UserPoolLayout, UserPoolSelfTest
    from bdr_agent.browser.browser import ChromiumProvider, UserPoolConfig
    from bdr_agent.protocol import ClaimedJob

    home, quarantine, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    layout = UserPoolLayout.for_home(home, quarantine_root=quarantine)

    async def main():
        st = UserPoolSelfTest(layout, dev_unenforced=True)
        provider = ChromiumProvider(ceilings=lambda *_: None, user_pool=UserPoolConfig(layout=layout, selftest=st))
        await provider.start()
        admission = str(uuid.uuid4())
        job = ClaimedJob(job_id=uuid.uuid4(), agent_type="user_browser", principal_kind="user",
                         principal_user_id=uuid.uuid4(), epoch=1, attempts=1,
                         lease_expires_at=datetime.now(UTC) + timedelta(minutes=5), input={})
        jb = await provider.new_context(job)
        ps = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
        chrome = [int(l.split()[0]) for l in ps.splitlines() if str(jb.job_dir) in l and "egress" not in l]
        out.write_text(json.dumps({"egress": jb.egress.pid, "chrome": chrome, "job": str(job.job_id)}))
        await asyncio.sleep(120)

    asyncio.run(main())
""")


@needs_browser
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill; the Job Object covers this on Windows")
def test_killing_a_worker_with_a_real_chromium_then_restarting_cleans_up(tmp_path):
    worker, pids, layout = _run_fake_worker(tmp_path, REAL_WORKER)
    every = [pids["egress"], *pids["chrome"]]
    try:
        assert pids["chrome"], "the per-job Chromium was not found by its profile path"
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=10)
        report = S.UserPoolSelfTest(layout, platform="darwin").cleanup()
        assert _wait_gone(every, timeout=20)
        assert list(layout.jobs_root.iterdir()) == [] and list(layout.quarantine_root.iterdir()) == []
        assert report.killed >= 1
    finally:
        for pid in every:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        if worker.poll() is None:
            worker.kill()


# The firewall script --------------------------------------------------------------------------------------


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def test_firewall_script_ranges_match_the_self_test():
    block = re.search(r"\$BlockedRanges = @\((.*?)\n\)", _ps1(), re.S).group(1)
    assert re.findall(r"'([^']+)'", block) == list(S.REQUIRED_REMOTE_RANGES)
    assert re.search(r"\$Keywords = @\('LocalSubnet'\)", _ps1())
    assert f"$Group = '{S.FIREWALL_GROUP}'" in _ps1()


def test_firewall_ranges_cover_every_private_network():
    import ipaddress

    nets = [ipaddress.ip_network(r) for r in S.REQUIRED_REMOTE_RANGES]
    for text in ("10.0.0.5", "172.31.255.1", "192.168.1.1", "100.64.0.1", "169.254.169.254", "224.0.0.251",
                 "255.255.255.255", "0.0.0.0", "fd00::1", "fe80::1", "ff02::1", "::ffff:10.0.0.5",
                 "64:ff9b::a00:5", "64:ff9b::c0a8:101", "2002:c0a8:101::1", "2002:a00:5::1"):
        ip = ipaddress.ip_address(text)
        assert any(ip in n for n in nets if n.version == ip.version), text
    for text in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "127.0.0.1", "::1"):
        ip = ipaddress.ip_address(text)
        assert not any(ip in n for n in nets if n.version == ip.version), text


def test_firewall_script_shape():
    text = _ps1()
    assert "\u2014" not in text and "\u2013" not in text
    for marker in ("Test-Admin", "-Direction Outbound -Action Block -Profile Any", "driver\\node.exe",
                   "chrome\\chrome.exe", "pyvenv.cfg", "floor.json", "gateway_ports", "BDRQuarantine",
                   "Register-ScheduledTask", "-UserId 'SYSTEM'", "-Reapply", "-Remove", "-LocalUser",
                   "Get-MpPreference"):
        assert marker in text, marker
    for line in text.splitlines():
        if "icacls" in line and "/inheritance:r" in line:
            assert "/T" not in line.split(), line
    assert "\ufeff" not in text


def _ps1_block(text: str, opener: str) -> str:
    start = text.index(opener)
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise AssertionError(opener)


def test_firewall_reapply_task_never_runs_icacls_or_follows_links():
    """Review WI-8: the SYSTEM re-apply task must not run icacls on folders the
    service account can swap for a junction, and must never Test-Path a
    pyvenv.cfg home that could be an SMB path."""
    text = _ps1()
    main = text[text.index("# Main ----"):]
    folders = _ps1_block(main, "if (-not $Reapply) {")
    outside = "\n".join(line for line in main.replace(folders, "").splitlines() if not line.lstrip().startswith("#"))
    assert "icacls" not in outside and "Set-FolderAcl" not in outside and "New-Item" not in outside
    for line in text.splitlines():
        if re.search(r"&\s*icacls\b", line):
            assert "/L" in line.split(), line
    acl = _ps1_block(text, "function Set-FolderAcl")
    assert acl.index("Assert-SafeFolder") < acl.index("icacls") and "/setowner" in acl
    assert "Assert-NoReparseInside" in acl
    assert "(OI)(CI)(IO)M" in folders and "(RX,WD,AD)" in folders
    programs = _ps1_block(text, "function Get-ProgramSet")
    assert "Test-Path" not in programs and "Resolve-VenvHome" in programs and "Test-ReparseFree" in programs
    venv = _ps1_block(text, "function Resolve-VenvHome")
    assert "'^[A-Za-z]:\\\\'" in venv and venv.index("-notmatch") < venv.index("Test-ReparseFree")
    assert "Test-Path" not in venv
    assert "$env:ProgramFiles" in venv and "'python'" in venv


def test_firewall_script_blocks_office_owned_public_addresses():
    text = _ps1()
    assert "[string[]]$OfficeBlockedRanges" in text and "RouterAdvertisement" in text
    assert "myip.opendns.com" in text and "-NoWanDetect" in text
    assert text.count("-RemoteAddress $RuleRanges") == 2 and "-RemoteAddress $BlockedRanges" not in text
    assert "extra_blocked = $ExtraRanges" in text and "Get-RangesTag" in _ps1_block(text, "function Test-RulesCurrent")


def test_readme_documents_the_user_pool_floor_and_quarantine():
    text = (ROOT / "README_WINDOWS.md").read_text(encoding="utf-8")
    assert "apply_user_pool_firewall.ps1" in text and "BDRQuarantine" in text
    assert "Never exclude" in text and "\u2014" not in text


# Launcher: stable runtime copy ----------------------------------------------------------------------------


def _launcher():
    from bdr_launcher import release
    from bdr_launcher.paths import LauncherPaths
    return release, LauncherPaths


def _fake_version(paths, version, node=b"node-1", revision="1140", chrome=b"chrome-1"):
    release, _ = _launcher()
    if sys.platform == "win32":
        site = paths.version_dir(version) / "venv" / "Lib" / "site-packages"
    else:
        site = paths.version_dir(version) / "venv" / "lib" / "python3.12" / "site-packages"
    driver = site / "playwright" / "driver"
    (driver / "package").mkdir(parents=True)
    (driver / ("node.exe" if sys.platform == "win32" else "node")).write_bytes(node)
    (driver / "package" / "browsers.json").write_text(json.dumps(
        {"browsers": [{"name": "chromium", "revision": revision}, {"name": "firefox", "revision": "1"}]}))
    chrome_dir = paths.browsers_dir / f"chromium-{revision}" / "chrome-win"
    chrome_dir.mkdir(parents=True, exist_ok=True)
    (chrome_dir / "chrome.exe").write_bytes(chrome)
    (chrome_dir / "chrome.dll").write_bytes(b"dll")
    (chrome_dir / "locales").mkdir(exist_ok=True)
    (chrome_dir / "locales" / "en-US.pak").write_bytes(b"pak")


def test_launcher_copies_driver_and_chromium_to_fixed_paths(tmp_path):
    release, LauncherPaths = _launcher()
    paths = LauncherPaths.for_home(tmp_path / "BDRAgentUser")
    assert release.wants_stable_runtime(paths)
    assert not release.wants_stable_runtime(LauncherPaths.for_home(tmp_path / "BDRAgent"))
    _fake_version(paths, "1.1.0")
    m = release.sync_stable_runtime(paths, "1.1.0")
    current = release.stable_runtime_dir(paths)
    assert (current / m["chrome_exe"]).read_bytes() == b"chrome-1"
    assert (current / "chrome" / "locales" / "en-US.pak").is_file()
    assert (current / m["node_exe"]).read_bytes() == b"node-1"
    assert m["chrome_exe"] == "chrome/chrome.exe" and m["chromium_revision"] == "1140"
    # The worker's layout reads the same manifest.
    lay = S.UserPoolLayout.for_home(paths.home, quarantine_root=tmp_path / "q")
    assert lay.chrome_exe == (current / "chrome" / "chrome.exe").resolve()
    # Same version again: nothing is copied.
    again = release.sync_stable_runtime(paths, "1.1.0")
    assert again["synced_at"] == m["synced_at"]
    # A new version with a new Chromium replaces it in place; no temp folders stay.
    _fake_version(paths, "1.1.1", node=b"node-2", revision="1150", chrome=b"chrome-2")
    m2 = release.sync_stable_runtime(paths, "1.1.1")
    assert (current / m2["chrome_exe"]).read_bytes() == b"chrome-2" and m2["version"] == "1.1.1"
    assert sorted(p.name for p in current.parent.iterdir()) == ["current"]


def test_launcher_sync_failure_keeps_the_previous_runtime(tmp_path):
    release, LauncherPaths = _launcher()
    paths = LauncherPaths.for_home(tmp_path / "BDRAgentUser")
    _fake_version(paths, "1.1.0")
    release.sync_stable_runtime(paths, "1.1.0")
    _fake_version(paths, "1.1.2", revision="1999")
    import shutil
    shutil.rmtree(paths.browsers_dir / "chromium-1999")
    with pytest.raises(release.ReleaseRejected) as info:
        release.sync_stable_runtime(paths, "1.1.2")
    assert info.value.reason == "runtime_sync_failed"
    assert (release.stable_runtime_dir(paths) / "runtime.json").is_file()
    assert json.loads((release.stable_runtime_dir(paths) / "runtime.json").read_text())["version"] == "1.1.0"
    assert release.sync_stable_runtime_if_wanted(paths, "1.1.2") is None
    assert release.sync_stable_runtime_if_wanted(LauncherPaths.for_home(tmp_path / "BDRAgent"), "1.1.0") is None
    with pytest.raises(release.ReleaseRejected):
        release.sync_stable_runtime(paths, "9.9.9")


def test_selftest_module_is_wired_for_the_provider():
    # The provider calls these names; keep them stable for WI-10.
    for name in ("cleanup", "run_boot", "check_before_claim", "require_claims", "claims_allowed",
                 "downloads_allowed"):
        assert hasattr(S.UserPoolSelfTest, name), name
    assert callable(S.find_mpcmdrun) and S.find_mpcmdrun() is None or sys.platform == "win32"
    assert asyncio.iscoroutinefunction(S.UserPoolSelfTest.run_boot)
    assert NS  # keep the import used
