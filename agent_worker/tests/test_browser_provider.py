"""ChromiumProvider: the profile under <home>/tmp never holds the password,
DEBUG is scrubbed, a dead Chromium is relaunched, leftovers are killed."""

import os
import subprocess
import sys
import time
from urllib.parse import quote_plus

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.rfi_from_email.runner import RfiFromEmailRunner
from bdr_agent.browser.browser import ChromiumProvider, kill_stale_chromium

from .agent_bdr import AgentBdr
from .agent_harness import SEED, needs_browser, run_job
from .procore_site import ProcoreSite

pytestmark = needs_browser


@pytest.fixture
def site():
    s = ProcoreSite().start()
    yield s
    s.stop()


@pytest.fixture
def scoped_env(monkeypatch):
    # start() points the driver's temp dir at <home>/tmp; restore after.
    for var in ("TMPDIR", "TMP", "TEMP", "DEBUG", "PWDEBUG"):
        monkeypatch.setenv(var, os.environ.get(var, ""))
    return monkeypatch


async def test_profile_never_holds_the_password(site, paths, scoped_env):
    scoped_env.setenv("DEBUG", "pw:api")
    provider = ChromiumProvider(ceilings=ceiling_for, api_host="bdr.test", tmp_dir=paths.tmp_dir,
                                proxy={"server": site.proxy_url}, ignore_https_errors=True)
    await provider.start()
    try:
        assert "DEBUG" not in os.environ
        bdr = AgentBdr()
        bdr.script = [("ensure_login", {"host": "app.procore.com"}),
                      ("finish", {"outcome": "nothing_to_do", "summary": "x"})]
        run = await run_job(bdr, provider, paths, "rfi_from_email",
                            runners={"rfi_from_email": RfiFromEmailRunner()}, input=SEED)
        assert run.outcome.terminal == "finish:succeeded"
        assert [r["outcome"] for r in bdr.login_results] == ["ok"]
        profile_root = paths.tmp_dir / "chromium"
        files = [p for p in profile_root.rglob("*") if p.is_file()]
        assert files, "Chromium's throwaway profile lives under <home>/tmp"
        needles = [bdr.login_password.encode(), quote_plus(bdr.login_password).encode()]
        for f in files:
            try:
                data = f.read_bytes()
            except OSError:
                continue
            assert not any(n in data for n in needles), f
    finally:
        await provider.stop()


async def test_start_wipes_the_previous_profile_then_keeps_its_temp_dir(site, paths, scoped_env):
    stale = paths.chromium_tmp_dir / "old-profile" / "Cookies"
    stale.parent.mkdir(parents=True)
    stale.write_text("leftover from a hard shutdown")
    provider = ChromiumProvider(ceilings=ceiling_for, api_host="bdr.test", tmp_dir=paths.tmp_dir,
                                proxy={"server": site.proxy_url}, ignore_https_errors=True)
    await provider.start()
    try:
        assert not stale.exists()
        assert paths.chromium_tmp_dir.is_dir() and os.environ["TMP"] == str(paths.chromium_tmp_dir)
    finally:
        await provider.stop()


async def test_dead_browser_is_relaunched(site, paths):
    provider = ChromiumProvider(ceilings=ceiling_for, api_host="bdr.test", proxy={"server": site.proxy_url},
                                ignore_https_errors=True)
    await provider.start()
    try:
        await provider._browser.close()
        from bdr_agent.protocol import ClaimedJob
        job = ClaimedJob(job_id="1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d", agent_type="canary", epoch=1,
                         lease_expires_at="2026-10-19T15:05:35Z", input={}, attempts=1)
        jb = await provider.new_context(job)
        assert provider.launches == 2 and provider.chromium_version
        await provider.close_context(jb)
    finally:
        await provider.stop()


def test_kill_stale_chromium(tmp_path):
    marker = tmp_path / "chromium"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(marker)])
    try:
        time.sleep(0.2)
        assert kill_stale_chromium(marker) >= 1
        assert proc.wait(timeout=10) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
