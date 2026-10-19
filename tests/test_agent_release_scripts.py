"""Release tooling (O3): scripts/agent_release_keygen.py and
scripts/publish_agent_worker_release.py, plus GET /agent-worker/v1/installer.ps1.
No network: the publish runs against the FakeDB storage."""

from __future__ import annotations

import base64
import importlib.util
import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.browser_agents import artifacts
from tests.browser_agents_fake import FakeDB
from tests.browser_agents_harness import API, Harness

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


keygen = _load("agent_release_keygen")
publisher = _load("publish_agent_worker_release")


@pytest.fixture
def keys(tmp_path):
    out = keygen.generate(tmp_path / "k" / "release.pem", tmp_path / "release_key.pub", b"long passphrase 123")
    key = publisher.load_private_key(tmp_path / "k" / "release.pem", b"long passphrase 123")
    return out, key, tmp_path


def test_keygen_writes_a_private_file_and_prints_the_public_half(keys):
    out, key, tmp = keys
    pem = tmp / "k" / "release.pem"
    assert stat.S_IMODE(pem.stat().st_mode) == 0o600
    assert b"ENCRYPTED PRIVATE KEY" in pem.read_bytes()
    assert (tmp / "release_key.pub").read_text().strip() == out["public_b64"]
    assert base64.b64decode(out["public_b64"]) == publisher.public_raw(key)
    assert len(out["fingerprint"].split()) == 16
    with pytest.raises(SystemExit):
        keygen.generate(pem, None, None)                  # never overwrites


def test_publish_signs_verifies_uploads_and_registers(keys, tmp_path):
    out, key, _ = keys
    bundle = tmp_path / "bdr-agent-1.2.0.zip"
    bundle.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    db = FakeDB()
    created = datetime(2026, 10, 19, 17, 0, 0, 123, tzinfo=timezone.utc)
    row = publisher.publish(db, version="1.2.0", min_version="1.1.0", bundle=bundle, key=key,
                            env_pubkey=out["public_b64"], chromium_version="141.0", notes="first", created_at=created)
    assert row["manifest"]["created_at"] == "2026-10-19T17:00:00Z"
    [stored] = db.rows("browser_agent_worker_releases")
    assert stored["sha256"] == publisher.sha256_file(bundle)
    assert db.storage.store[("browser-agent", "releases/1.2.0/bdr-agent-1.2.0.zip")] == bundle.read_bytes()
    assert artifacts.verify_release(stored, out["public_b64"]) is True     # BDR accepts it
    assert "PRIVATE" not in json.dumps(db.tables, default=str)
    with pytest.raises(SystemExit):
        publisher.publish(db, version="1.2.0", min_version="1.1.0", bundle=bundle, key=key)   # immutable


def test_publish_refuses_a_key_that_is_not_the_configured_one_and_bad_versions(keys, tmp_path):
    _, key, _ = keys
    bundle = tmp_path / "b.zip"
    bundle.write_bytes(b"x")
    other = base64.b64encode(b"\x01" * 32).decode()
    with pytest.raises(SystemExit):
        publisher.publish(FakeDB(), version="1.0.0", min_version="1.0.0", bundle=bundle, key=key, env_pubkey=other)
    with pytest.raises(SystemExit):
        publisher.build_manifest("1.0.0", "a" * 64, "2.0.0")
    with pytest.raises(SystemExit):
        publisher.version_tuple("1.0")
    dry = publisher.publish(FakeDB(), version="1.0.0", min_version="1.0.0", bundle=bundle, key=key, dry_run=True)
    assert dry["signature"]


def test_publish_never_targets_prod_without_the_flag():
    with pytest.raises(SystemExit):
        publisher.guard_target("https://zxqkqotlcgbalwoqeeca.supabase.co", False)
    publisher.guard_target("https://bpidntbyvoooqvaispup.supabase.co", False)


def test_installer_template_keeps_only_the_release_placeholder(keys):
    _, key, _ = keys
    tpl = publisher.render_installer_template("https://api.g3.test", publisher.public_raw(key))
    assert tpl.startswith(b"\xef\xbb\xbf")
    text = tpl.decode("utf-8-sig")
    assert text.count("__RELEASE_JSON_B64__") == 1 and "__BDR_API_BASE__" not in text
    assert "https://api.g3.test" in text and base64.b64encode(publisher.public_raw(key)).decode() in text


def test_installer_route_fills_the_desired_release(monkeypatch, keys, tmp_path):
    out, key, _ = keys
    h = Harness(monkeypatch, browser_agent_release_pubkey=out["public_b64"])
    assert h.client.get(f"{API}/installer.ps1").status_code == 404           # nothing selected
    bundle = tmp_path / "bdr-agent-1.0.0.zip"
    bundle.write_bytes(b"zip")
    publisher.publish(h.db, version="1.0.0", min_version="1.0.0", bundle=bundle, key=key)
    h.db.tables["browser_agent_settings"][0]["desired_worker_version"] = "1.0.0"
    assert h.client.get(f"{API}/installer.ps1").status_code == 404           # no template yet
    publisher.upload_installer_template(h.db, publisher.render_installer_template(
        "https://api.g3.test", publisher.public_raw(key)))
    resp = h.client.get(f"{API}/installer.ps1")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    text = resp.content.decode("utf-8-sig")
    assert "__RELEASE_JSON_B64__" not in text
    encoded = text.split("$EmbeddedReleaseB64")[1].split("'")[1]
    payload = json.loads(base64.b64decode(encoded))
    assert payload["manifest"]["version"] == "1.0.0" and payload["url"].startswith("https://storage.test/")
