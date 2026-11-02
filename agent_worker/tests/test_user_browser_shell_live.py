"""Real local Chromium: the modern hub shell renders only through the narrow
*.pages.procore.com allowance, and the census names a refused shell. 1.0.6:
a shell loading a remote's /remoteEntry.js from an allowed exact pages host
renders; an unlisted or merely listed pages host is refused, the shell shows
Procore's not-found sentence and the finish becomes page_code_blocked."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser.diagnostics import NOT_FOUND_TEXT
from bdr_agent.agents.user_browser.runner import FinishTool
from bdr_agent.browser.browser import ChromiumProvider
from bdr_agent.protocol import ClaimedJob

from .agent_harness import needs_browser
from .procore_site import ProcoreSite

pytestmark = needs_browser
HUB = "/webclients/host/companies/5/projects/123/tools/hubs/project-hub/views/overview"
SEED = "https://us02.procore.com" + HUB
SHELL_HOST = "ui-service-shell.pages.procore.com"
BUNDLE = "/static/js/index.bundle.33ab5944.js"
# Renders into #root, then leaves decoys in the console and as a page error.
BUNDLE_JS = (b'document.getElementById("root").textContent = "Project hub overview ready";'
             b'console.error("decoy-console https://decoy-host.test/?token=decoy-q");'
             b'setTimeout(() => { throw new TypeError("decoy-pageerror https://decoy-host.test/p"); }, 0);')


RFIS_HOST = "rfis-ui-service.pages.procore.com"
CHUNK = "/static/js/52086.d9903000.chunk.js"


def loader_bundle(remote_host):
    """A shell bundle that loads one remote's root remoteEntry.js by script tag,
    like the Procore shell, and shows the fixed not-found sentence on error."""
    return (
        'const root = document.getElementById("root");'
        'const s = document.createElement("script");'
        f's.src = "https://{remote_host}/remoteEntry.js";'
        f's.onerror = () => {{ root.textContent = "{NOT_FOUND_TEXT}"; '
        'console.error("ScriptExternalLoadError: Loading script failed. decoy-console"); };'
        'document.head.appendChild(s);').encode()


REMOTE_ENTRY_JS = (
    'const c = document.createElement("script");'
    f'c.src = "https://{RFIS_HOST}{CHUNK}";'
    'document.head.appendChild(c);').encode()
CHUNK_JS = b'document.getElementById("root").textContent = "RFIs list ready";'


class ShellSite(ProcoreSite):
    script = f'<script defer src="https://{SHELL_HOST}{BUNDLE}"></script>'
    acao = True
    bundle = BUNDLE_JS

    def respond(self, seen):
        if seen.host == "us02.procore.com" and seen.path == HUB:
            return self._html(200, "Procore", f'<div id="root"></div>{self.script}')
        js = [("Content-Type", "application/javascript"), ("Access-Control-Allow-Origin", "*"),
              ("Set-Cookie", "decoy_remote=1; Domain=.procore.com")]
        if seen.host == SHELL_HOST and seen.path == BUNDLE:
            headers = [("Content-Type", "application/javascript")]
            if self.acao:
                headers.append(("Access-Control-Allow-Origin", "*"))
            return 200, headers, self.bundle
        if seen.host.endswith(".pages.procore.com") and seen.path == "/remoteEntry.js":
            return 200, js, REMOTE_ENTRY_JS
        if seen.host == RFIS_HOST and seen.path == CHUNK:
            return 200, js, CHUNK_JS
        return super().respond(seen)


@pytest.fixture
def site():
    value = ShellSite().start()
    yield value
    value.stop()


def job():
    return ClaimedJob(job_id=uuid4(), agent_type="user_browser_read", principal_kind="user",
                      principal_user_id=uuid4(), epoch=1, attempts=1,
                      lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
                      input={"service": "procore", "url": SEED, "objective": "Read the hub"})


async def open_hub(site):
    provider = ChromiumProvider(ceilings=ceiling_for, proxy={"server": site.proxy_url}, ignore_https_errors=True)
    await provider.start()
    jb = await provider.new_context(job())
    logged = []
    jb.attach_steps(lambda kind, summary, **fields: logged.append((kind, summary, fields)))
    await jb.page.goto(SEED, wait_until="load", timeout=15_000)
    await asyncio.sleep(0.3)   # requestfinished, the deferred throw and console events
    return provider, jb, logged


def census_rows(logged):
    [census] = [fields["result"] for kind, _s, fields in logged if kind == "browser.resource_census"]
    return census


@pytest.mark.parametrize("variant", ["classic", "module_with_acao", "module_without_acao"])
async def test_allowed_shell_renders_and_is_counted(site, variant):
    if variant != "classic":
        site.script = f'<script type="module" src="https://{SHELL_HOST}{BUNDLE}"></script>'
        site.acao = variant == "module_with_acao"
    provider, jb, logged = await open_hub(site)
    try:
        diag = jb.guard.diagnostics
        probe = await diag.emit(jb.page, "navigate")
        assert probe is not None and probe["text_len"] > 0 and diag.readable(probe)
        assert probe["root_children"]["root"] == 0   # textContent: a text node, no element child
        census = census_rows(logged)
        [row] = [r for r in census["rows"] if r["hc"] == f"procore_pages_known:{SHELL_HOST.split('.')[0]}"]
        assert row["v"] == "fulfilled" and row["co"] == "finished" and row["sc"] == "2xx"
        assert census["console"]["error"] and census["pageerror"] == {"type_error": 1}
        seen = site.seen(host=SHELL_HOST)
        assert seen and all(not r.headers.get("cookie") for r in seen)
        assert "decoy" not in json.dumps(logged)
    finally:
        await jb.close()
        await provider.stop()


async def test_refused_shell_leaves_a_white_page_and_a_named_census_row(site):
    site.script = f'<script defer src="https://{SHELL_HOST}{BUNDLE}?v=decoy-cachebuster"></script>'
    provider, jb, logged = await open_hub(site)
    try:
        diag = jb.guard.diagnostics
        probe = await diag.emit(jb.page, "navigate")
        assert probe["text_len"] == 0 and not diag.readable(probe)
        assert probe["root_children"]["root"] == 0 and probe["scripts_external"] == 1
        census = census_rows(logged)
        [row] = [r for r in census["rows"] if r["hc"].startswith("procore_pages")]
        assert row["v"] == "refused:asset_query_not_allowed" and row["co"] == "failed:blocked_by_client"
        assert row["q"] == "plain"
        blocked = [fields["result"] for kind, _s, fields in logged if kind == "http.blocked"]
        assert {"method": "GET", "host": SHELL_HOST, "path": BUNDLE, "reason": "asset_query_not_allowed"} in blocked
        assert not site.seen(host=SHELL_HOST)
        assert "decoy" not in json.dumps(logged)
    finally:
        await jb.close()
        await provider.stop()


async def test_probe_counts_shadow_roots_iframes_and_display_contents(site):
    provider, jb, _logged = await open_hub(site)
    try:
        await jb.page.set_content(
            '<div id="root" style="display:contents"><span>visible text</span></div>'
            '<x-host id="host"></x-host><x-undefined></x-undefined>'
            '<iframe srcdoc="<p>frame text</p>"></iframe>'
            '<script>document.getElementById("host").attachShadow({mode: "open"}).innerHTML = "<b>shadow</b>";'
            '</script><script type="module"></script><noscript>n</noscript>')
        await asyncio.sleep(0.2)
        probe = await jb.guard.diagnostics.probe(jb.page)
        assert probe["display_contents"] >= 1 and probe["shadow_roots_open"] == 1
        assert probe["undefined_custom"] == 2 and probe["iframes"] == 1 and probe["iframes_same_origin"] == 1
        assert probe["iframe_text_len"] > 0 and probe["module_scripts"] == 1 and probe["noscript"] == 1
        assert probe["root_children"]["root"] == 1 and probe["text_len"] > 0 and probe["probe_error"] is False
        assert not any(isinstance(value, str) for value in probe.values())
    finally:
        await jb.close()
        await provider.stop()


async def wait_root_text(page):
    await page.wait_for_function('() => (document.getElementById("root") || {}).textContent', timeout=10_000)
    await asyncio.sleep(0.3)   # requestfinished / requestfailed and console events


async def test_allowed_remote_entry_from_an_exact_pages_host_renders(site):
    site.bundle = loader_bundle(RFIS_HOST)
    provider, jb, logged = await open_hub(site)
    try:
        await wait_root_text(jb.page)
        assert await jb.page.evaluate('() => document.getElementById("root").textContent') == "RFIs list ready"
        diag = jb.guard.diagnostics
        probe = await diag.emit(jb.page, "navigate")
        assert probe["spa_not_found"] is False and diag.readable(probe)
        assert diag.remote_entry_refused == 0 and not diag.page_code_blocked(probe)
        census = census_rows(logged)
        entry = one_row(census, hc="procore_pages_known:rfis-ui-service", pf="root_remote_entry")
        assert entry["v"] == "fulfilled" and entry["co"] == "finished" and entry["rt"] == "script"
        assert entry["dh"] == 1 and entry["du"] == 1
        chunk = one_row(census, hc="procore_pages_known:rfis-ui-service", pf="static_ok")
        assert chunk["v"] == "fulfilled" and chunk["co"] == "finished"
        seen = site.seen(host=RFIS_HOST)
        assert {r.path for r in seen} == {"/remoteEntry.js", CHUNK}
        assert all(not r.headers.get("cookie") and not r.headers.get("authorization") for r in seen)
        cookies = await jb.context.cookies()
        assert not any(c["name"] == "decoy_remote" for c in cookies)
        verdict = await FinishTool(lambda: page_code_blocked(diag, jb.page))(
            NS(outcome="cannot_complete", summary="model summary", error_kind="not_found"))
        assert verdict.terminal.error_kind == "not_found" and verdict.terminal.error is None
    finally:
        await jb.close()
        await provider.stop()


@pytest.mark.parametrize("remote,label", [
    ("unlisted-decoy-ui-service.pages.procore.com", "[procore_pages_other]"),
    ("custom-configurable-fields-ui-service.pages.procore.com", "custom-configurable-fields-ui-service.pages.procore.com"),
])
async def test_refused_remote_entry_shows_not_found_and_finishes_page_code_blocked(site, remote, label):
    site.bundle = loader_bundle(remote)
    provider, jb, logged = await open_hub(site)
    try:
        await wait_root_text(jb.page)
        diag = jb.guard.diagnostics
        probe = await diag.emit(jb.page, "navigate")
        assert probe["spa_not_found"] is True and diag.remote_entry_refused == 1
        assert diag.page_code_blocked(probe)
        census = census_rows(logged)
        hc = "procore_pages_other" if label.startswith("[") else "procore_pages_listed:" + remote.split(".")[0]
        row = one_row(census, hc=hc)
        assert row["pf"] == "root_remote_entry" and row["v"] == "refused:origin_not_allowed"
        assert row["co"] == "failed:blocked_by_client" and row["dh"] == 1 and row["du"] == 1
        assert census["remote_entry_refused"] == 1
        assert census["console"]["error"].get("script_external_load") == 1
        blocked = [fields["result"] for kind, _s, fields in logged if kind == "http.blocked"]
        assert {"method": "GET", "host": label, "path": "[refused_path]", "reason": "origin_not_allowed"} in blocked
        assert not site.seen(host=remote)
        verdict = await FinishTool(lambda: page_code_blocked(diag, jb.page))(
            NS(outcome="cannot_complete", summary="404 decoy https://decoy-host.test", error_kind="not_found"))
        assert verdict.terminal.status == "failed" and verdict.terminal.error_kind == "cannot_complete"
        assert verdict.terminal.error == "page_code_blocked" and "decoy" not in verdict.terminal.summary
        persisted = json.dumps(logged)
        assert "decoy" not in persisted and "unlisted" not in persisted and NOT_FOUND_TEXT not in persisted
    finally:
        await jb.close()
        await provider.stop()


async def page_code_blocked(diag, page):
    return diag.page_code_blocked(await diag.probe(page))


def one_row(census, **match):
    found = [r for r in census["rows"] if all(r[k] == v for k, v in match.items())]
    assert len(found) == 1, (match, census["rows"])
    return found[0]
