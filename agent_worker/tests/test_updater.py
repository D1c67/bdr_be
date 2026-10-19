"""Worker-side self-update: the decision rules (signature, min_version, floor,
bad_versions), the download (sha256, size, no token to storage), the hand-off
to the launcher's `stage`, and the supervisor draining then exiting 5."""

import asyncio
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest

from bdr_agent.protocol import ReleaseInfo
from bdr_agent.release_key import load_public_key
from bdr_agent.updater import (
    EXIT_UPDATE,
    RETRY_AFTER_FAILURE_SECONDS,
    Updater,
    UpdateError,
    decide,
    parse_bad_versions,
    write_health_marker,
)
from bdr_launcher import cli as launcher_cli
from bdr_launcher import release as launcher_release
from bdr_launcher.state import read_pending

from .conftest import make_clients
from .release_helpers import FakeInstall, ReleaseKey, build_signed_bundle, manifest
from .test_supervisor import Counter, FakeRunner, make_supervisor, stop, until

STORAGE = "https://storage.test/agent-releases/1.1.0.zip?token=SIGNED-SECRET"


def offer(key: ReleaseKey, version="1.1.0", *, min_version="1.0.0", sha="ab" * 32, sig=None,
          **overrides) -> ReleaseInfo:
    m = manifest(version, sha, min_version)
    body = {"version": version, "sha256": sha, "min_version": min_version, "manifest": m,
            "signature": sig if sig is not None else key.sign(m)}
    body.update(overrides)
    return ReleaseInfo.model_validate(body)


def pubkey(tmp_path, key: ReleaseKey):
    return load_public_key(key.write(tmp_path / "release_key.pub"))


# decide() ---------------------------------------------------------------------------------


def test_decide_rules(tmp_path):
    key = ReleaseKey()
    pk = pubkey(tmp_path, key)

    def d(o, current="1.0.0", bad=(), floor=None):
        return decide(current, o, key=pk, bad_versions=list(bad), floor=floor)

    assert d(None).action == "none"
    assert d(offer(key, "1.0.0")).reason == "current"
    assert d(offer(key)).action == "stage"
    assert d(offer(key, "0.9.9", min_version="0.9.0"), current="1.0.0").reason == "below_floor"
    assert d(offer(key, "1.0.5"), current="1.1.0").action == "stage"       # admin-picked downgrade above floor
    assert d(offer(key, "1.0.5"), current="1.1.0", floor="1.1.0").reason == "below_floor"
    assert d(offer(key, sig=ReleaseKey().sign(manifest("1.1.0")))).reason == "bad_signature"
    assert d(offer(key, "1.1.0", min_version="1.2.0")).reason == "below_min_version"
    assert d(offer(key), bad=["1.1.0"]).reason == "bad_version"
    tampered = offer(key)
    tampered = ReleaseInfo.model_validate({**tampered.model_dump(mode="json"), "sha256": "cd" * 32})
    assert d(tampered).reason == "manifest_mismatch"


def test_parse_bad_versions():
    assert parse_bad_versions("1.1.0, nope,1.2.0,") == ["1.1.0", "1.2.0"]
    assert parse_bad_versions(None) == []


def test_check_backs_off_after_a_failure(tmp_path):
    key = ReleaseKey()
    now = [0.0]
    up = Updater(paths=_paths(tmp_path), key=pubkey(tmp_path, key), current="1.0.0", launcher=Path("l.py"),
                 runtime="py", clock=lambda: now[0])
    o = offer(key)
    assert up.check(o).action == "stage"
    up.mark_failed("1.1.0")
    assert up.check(o).reason == "recently_failed"
    now[0] = RETRY_AFTER_FAILURE_SECONDS + 1
    assert up.check(o).action == "stage"


def test_from_env_only_under_the_launcher(tmp_path, monkeypatch):
    key = ReleaseKey()
    paths = _paths(tmp_path)
    key.write(paths.release_pubkey)
    monkeypatch.delenv("BDR_AGENT_LAUNCHER", raising=False)
    assert Updater.from_env(paths, "1.0.0") is None
    monkeypatch.setenv("BDR_AGENT_LAUNCHER", "/x/launcher.py")
    monkeypatch.setenv("BDR_AGENT_RUNTIME", "/x/python.exe")
    monkeypatch.setenv("BDR_AGENT_BAD_VERSIONS", "1.2.0")
    monkeypatch.setenv("BDR_AGENT_MIN_VERSION", "1.1.0")
    up = Updater.from_env(paths, "1.1.0")
    assert up is not None and up.bad_versions == ["1.2.0"] and up.floor == "1.1.0"
    paths.release_pubkey.unlink()
    assert Updater.from_env(paths, "1.1.0") is None


def test_health_marker(tmp_path, monkeypatch):
    target = tmp_path / "health" / "1.1.0-x.ok"
    monkeypatch.delenv("BDR_AGENT_HEALTH_FILE", raising=False)
    write_health_marker()
    assert not target.exists()
    monkeypatch.setenv("BDR_AGENT_HEALTH_FILE", str(target))
    write_health_marker()
    assert target.exists()


# prepare(): download + hand-off ----------------------------------------------------------------


def _paths(tmp_path):
    from bdr_agent.local_config import Paths

    p = Paths(tmp_path / "home")
    p.ensure()
    return p


class Storage:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, content=self.body)


def _release_response(o: ReleaseInfo, url=STORAGE, sig=None):
    return httpx.Response(200, json={"manifest": o.manifest.model_dump(mode="json"),
                                     "signature": sig or o.signature, "url": url})


class StageRecorder:
    def __init__(self, result=None, code=0):
        self.calls = []
        self.result = result
        self.code = code

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        zip_path = Path(argv[argv.index("--zip") + 1])
        rel = json.loads(Path(argv[argv.index("--release-json") + 1]).read_text())
        self.seen = (zip_path.read_bytes(), rel)
        res = self.result or {"ok": True, "version": rel["manifest"]["version"], "pending": True}
        return self.code, "log line\n" + json.dumps(res) + "\n"


async def _prepare(tmp_path, bdr, worker, *, body=b"BUNDLE", stage=None, release_resp=None, o=None,
                   key=None, max_bytes=None):
    key = key or ReleaseKey()
    o = o or offer(key, sha=hashlib.sha256(body).hexdigest())
    storage = Storage(body)
    stage = stage or StageRecorder()
    kw = {"max_bytes": max_bytes} if max_bytes else {}
    up = Updater(paths=_paths(tmp_path), key=pubkey(tmp_path, key), current="1.0.0",
                 launcher=Path("/home/launcher/launcher.py"), runtime="/home/python/python.exe",
                 transport=storage.transport(), stage=stage, **kw)
    bdr.inject("release", release_resp or _release_response(o))
    ac, _sc = make_clients(bdr, worker.token)
    try:
        return await up.prepare(ac, o), storage, stage, up
    finally:
        await ac.aclose()


async def test_prepare_downloads_verifies_and_stages(tmp_path, bdr, worker):
    version, storage, stage, up = await _prepare(tmp_path, bdr, worker)
    assert version == "1.1.0"
    req = storage.requests[0]
    assert str(req.url) == STORAGE
    assert "authorization" not in {k.lower() for k in req.headers}          # no worker token to storage
    assert not any(k.lower().startswith("x-agent") for k in req.headers)
    argv = stage.calls[0]
    assert argv[:4] == ["/home/python/python.exe", "-I", "/home/launcher/launcher.py", "--home"]
    assert "stage" in argv
    data, rel = stage.seen
    assert data == b"BUNDLE" and set(rel) == {"manifest", "signature"}     # the URL is not passed on
    assert not list((up.paths.tmp_dir / "update").glob("*.zip"))             # cleaned after staging


async def test_prepare_refuses_sha_mismatch(tmp_path, bdr, worker):
    key = ReleaseKey()
    o = offer(key, sha="ef" * 32)
    with pytest.raises(UpdateError) as e:
        await _prepare(tmp_path, bdr, worker, key=key, o=o)
    assert e.value.reason == "sha_mismatch"


async def test_prepare_refuses_a_release_endpoint_that_disagrees(tmp_path, bdr, worker):
    key = ReleaseKey()
    body = b"BUNDLE"
    o = offer(key, sha=hashlib.sha256(body).hexdigest())
    other = offer(key, "1.1.0", min_version="1.1.0", sha=o.sha256)
    with pytest.raises(UpdateError) as e:
        await _prepare(tmp_path, bdr, worker, key=key, o=o, release_resp=_release_response(other))
    assert e.value.reason == "manifest_mismatch"
    with pytest.raises(UpdateError) as e:
        await _prepare(tmp_path, bdr, worker, key=key, o=o,
                       release_resp=_release_response(o, sig=ReleaseKey().sign(o.manifest.model_dump(mode="json"))))
    assert e.value.reason == "bad_signature"
    with pytest.raises(UpdateError) as e:
        await _prepare(tmp_path, bdr, worker, key=key, o=o,
                       release_resp=_release_response(o, url="http://storage.test/x.zip"))
    assert e.value.reason == "bad_url"


async def test_prepare_caps_the_download_and_reports_stage_rejections(tmp_path, bdr, worker):
    with pytest.raises(UpdateError) as e:
        await _prepare(tmp_path, bdr, worker, body=b"x" * 5000, max_bytes=100)
    assert e.value.reason == "download_failed"
    rec = StageRecorder(result={"ok": False, "reason": "below_floor", "detail": "1.1.0 < 1.2.0"}, code=10)
    with pytest.raises(UpdateError) as e:
        await _prepare(tmp_path / "b", bdr, worker, stage=rec)
    assert e.value.reason == "below_floor"


async def test_prepare_with_the_real_launcher_stage(tmp_path, bdr, worker, monkeypatch):
    """Worker updater -> launcher `stage` (in process, fake pip) -> pending.txt."""
    key = ReleaseKey()
    from bdr_launcher.paths import LauncherPaths

    lpaths = LauncherPaths.for_home(tmp_path / "home", tmp_path / "home" / "launcher")
    lpaths.ensure()
    key.write(lpaths.release_pubkey)
    zip_path, m, sig = build_signed_bundle(tmp_path / "b", key, "1.1.0")
    body = zip_path.read_bytes()
    o = ReleaseInfo.model_validate({"version": "1.1.0", "sha256": m["sha256"], "min_version": "1.0.0",
                                    "manifest": m, "signature": sig})
    monkeypatch.setattr(launcher_release, "subprocess_runner", FakeInstall())

    def in_process_stage(argv, timeout):
        args = argv[argv.index("--home"):]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = launcher_cli.main([*args[:2], "--launcher-dir", str(lpaths.launcher_dir), *args[2:]])
        return code, buf.getvalue()

    version, *_ = await _prepare(tmp_path, bdr, worker, body=body, key=key, o=o, stage=in_process_stage)
    assert version == "1.1.0"
    assert read_pending(lpaths.pending_file) == "1.1.0"
    assert launcher_release.is_installed(lpaths, "1.1.0")


# Supervisor: drain, stage, exit 5 ------------------------------------------------------------


class FakeUpdater:
    def __init__(self, decision_action="stage", fail=False, gate=None):
        self.decision_action = decision_action
        self.fail = fail
        self.gate = gate
        self.prepared: list[str] = []
        self.failed: list[str] = []

    def check(self, o):
        from bdr_agent.updater import UpdateDecision

        if o is None or o.version == "1.0.0" or o.version in self.failed:
            return UpdateDecision("none", "current")
        return UpdateDecision(self.decision_action, "ok", o.version)

    async def prepare(self, client, o):
        if self.gate is not None:
            await self.gate.wait()
        self.prepared.append(o.version)
        if self.fail:
            raise UpdateError("download_failed", "boom")
        return o.version

    def mark_failed(self, v):
        self.failed.append(v)


def _offer_in_config(bdr, key, version="1.1.0"):
    o = offer(key, version)
    orig = bdr._config_body

    def body(w):
        b = orig(w)
        b["release"] = o.model_dump(mode="json")
        return b

    bdr._config_body = body
    bdr.bump_config()


async def test_supervisor_drains_then_exits_for_update(bdr, worker, paths):
    counter = Counter()
    rfi = FakeRunner(counter, hold=True)
    jid = bdr.add_job("rfi_from_email")
    gate = asyncio.Event()
    up = FakeUpdater(gate=gate)
    sup = make_supervisor(bdr, worker, paths, {"rfi_from_email": rfi}, updater=up)
    task = asyncio.create_task(sup.run())
    await until(lambda: rfi.started)
    _offer_in_config(bdr, ReleaseKey())
    await until(lambda: sup._update_task is not None)
    later = bdr.add_job("rfi_from_email")
    claims_before = len(bdr.claim_requests)
    gate.set()
    await until(lambda: sup._update_ready == "1.1.0")
    await asyncio.sleep(0.2)
    assert not task.done()                                # a job is still running
    assert len(bdr.claim_requests) == claims_before       # draining: no claims
    rfi.release()
    assert await asyncio.wait_for(task, 10) == EXIT_UPDATE
    assert bdr.jobs[jid].status == "succeeded"
    assert bdr.jobs[later].status == "queued"             # left for the new version
    assert up.prepared == ["1.1.0"]


async def test_supervisor_resumes_claiming_after_a_failed_update(bdr, worker, paths):
    counter = Counter()
    canary = FakeRunner(counter)
    up = FakeUpdater(fail=True)
    _offer_in_config(bdr, ReleaseKey())
    sup = make_supervisor(bdr, worker, paths, {"canary": canary}, updater=up)
    task = asyncio.create_task(sup.run())
    await until(lambda: up.failed == ["1.1.0"])
    jid = bdr.add_job("canary")
    await until(lambda: bdr.jobs[jid].status == "succeeded")
    assert sup._update_ready is None and sup.exit_code is None
    assert await stop(sup, task) == 0


async def test_supervisor_without_updater_ignores_releases(bdr, worker, paths):
    counter = Counter()
    canary = FakeRunner(counter)
    _offer_in_config(bdr, ReleaseKey())
    sup = make_supervisor(bdr, worker, paths, {"canary": canary})
    task = asyncio.create_task(sup.run())
    jid = bdr.add_job("canary")
    await until(lambda: bdr.jobs[jid].status == "succeeded")
    assert await stop(sup, task) == 0


def test_stage_subprocess_recreates_a_missing_temp_dir(tmp_path, monkeypatch):
    """playwright install (Node) fails on a missing TMP; the stage child must get one that exists."""
    import sys

    from bdr_agent.updater import subprocess_stage

    gone = tmp_path / "tmp" / "chromium"
    for var in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(var, str(gone))
    code, _out = subprocess_stage([sys.executable, "-c", "import os; assert os.path.isdir(os.environ['TMP'])"], 30)
    assert code == 0 and gone.is_dir()
