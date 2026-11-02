"""Bounded Playwright driver headers; no browser launch, sockets or site calls."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bdr_agent.browser.browser import ChromiumProvider, DRIVER_NODE_OPTIONS


@pytest.mark.parametrize("inherited", [None, "--max-http-header-size=1048576 --require=untrusted.js"])
@pytest.mark.parametrize("failure", [None, "driver", "cancel", "chromium"])
async def test_header_option_is_driver_only_and_parent_is_restored(monkeypatch, inherited, failure):
    if inherited is None:
        monkeypatch.delenv("NODE_OPTIONS", raising=False)
    else:
        monkeypatch.setenv("NODE_OPTIONS", inherited)
    captured = []
    driver = SimpleNamespace()

    class Manager:
        async def __aenter__(self):
            captured.append(os.environ.get("NODE_OPTIONS"))
            await asyncio.sleep(0)
            assert os.environ["NODE_OPTIONS"] == "--max-http-header-size=65536"
            if failure == "driver":
                raise RuntimeError("driver failed")
            if failure == "cancel":
                raise asyncio.CancelledError()
            return driver

    package = ModuleType("playwright")
    api = ModuleType("playwright.async_api")
    api.async_playwright = Manager
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    provider = ChromiumProvider(ceilings=lambda *_: None)

    async def launch():
        assert os.environ.get("NODE_OPTIONS") == inherited
        assert provider._ignore_https_errors is False
        if failure == "chromium":
            raise RuntimeError("launch failed")

    provider._launch = AsyncMock(side_effect=launch)
    if failure:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await provider.start()
    else:
        await provider.start()
        assert provider._pw is driver
    assert captured == [DRIVER_NODE_OPTIONS]
    assert os.environ.get("NODE_OPTIONS") == inherited
    if inherited is None:
        assert "NODE_OPTIONS" not in os.environ
    assert provider._launch.await_count == (0 if failure in {"driver", "cancel"} else 1)


def pinned_node() -> Path:
    candidates = []
    installed = importlib.util.find_spec("playwright")
    if installed and installed.origin:
        candidates.append(Path(installed.origin).parent / "driver")
    worker = Path(__file__).parents[1]
    candidates.extend(worker.glob(".venv/lib/python*/site-packages/playwright/driver"))
    candidates.append(worker / ".venv/Lib/site-packages/playwright/driver")
    for driver in candidates:
        node = driver / ("node.exe" if os.name == "nt" else "node")
        package = driver / "package/package.json"
        if node.is_file() and package.is_file():
            # The pinned Python 1.48.0 wheel bundles the JS package 1.48.1.
            # Pin the Python distribution, not its internal JS patch number.
            assert "version = '1.48.0'" in (driver.parent / "_repo_version.py").read_text()
            return node
    pytest.skip("Installed pinned Playwright driver is unavailable for socket-free parser check")


PARSER = r"""
const {HTTPParser:P} = process.binding('http_parser');
const {AsyncResource} = require('node:async_hooks');
const p = new P();
// Zero means the process default, as used by Playwright's https.request.
p.initialize(P.RESPONSE, new AsyncResource('synthetic-response'), 0, P.kLenientNone);
p[P.kOnHeaders] = () => {};
p[P.kOnHeadersComplete] = () => 0;
p[P.kOnBody] = () => {};
p[P.kOnMessageComplete] = () => {};
const bytes = Number(process.argv[1]);
const wire = Buffer.from('HTTP/1.1 200 OK\r\nContent-Security-Policy: ' +
                        'x'.repeat(bytes) + '\r\nContent-Length: 2\r\n\r\nOK');
const value = p.execute(wire);
const result = {cap: require('node:http').maxHeaderSize,
                code: value instanceof Error ? value.code : null,
                reason: value instanceof Error ? value.reason : null,
                complete: !(value instanceof Error) && value === wire.length};
p.close();
console.log(JSON.stringify(result));
"""


@pytest.mark.parametrize("fixed,size,expected_cap,expected_code", [
    (False, 24 * 1024, 16384, "HPE_HEADER_OVERFLOW"),
    (True, 24 * 1024, 65536, None),
    (True, 96 * 1024, 65536, "HPE_HEADER_OVERFLOW"),
])
def test_installed_node_parser_cap_is_real_and_remains_bounded(fixed, size, expected_cap, expected_code):
    env = {key: value for key, value in os.environ.items() if not key.startswith("NODE_")}
    if fixed:
        env["NODE_OPTIONS"] = DRIVER_NODE_OPTIONS
    result = subprocess.run([str(pinned_node()), "-e", PARSER, str(size)],
                            env=env, capture_output=True, text=True, check=True, timeout=10)
    outcome = json.loads(result.stdout)
    assert outcome["cap"] == expected_cap
    assert outcome["code"] == expected_code
    assert outcome["complete"] is (expected_code is None)
    if expected_code:
        assert outcome["reason"] == "Header overflow"
