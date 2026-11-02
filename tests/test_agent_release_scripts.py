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
from types import SimpleNamespace

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


def test_publish_expected_project_rejects_staging_and_deceptive_hosts():
    for url in ("https://staging.supabase.co", "https://bpidntbyvoooqvaispup.supabase.co.attacker.test"):
        with pytest.raises(SystemExit, match="expected-project"):
            publisher.guard_target(url, False, "bpidntbyvoooqvaispup")
    publisher.guard_target("https://bpidntbyvoooqvaispup.supabase.co", False, "bpidntbyvoooqvaispup")


def test_cli_retains_verified_dry_run_artifacts_without_network(keys, tmp_path, monkeypatch):
    from app.core import config, supabase_client

    out, key, _ = keys
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
        supabase_url="https://bpidntbyvoooqvaispup.supabase.co",
        browser_agent_api_base_url="https://api.g3.test",
        browser_agent_release_pubkey=out["public_b64"]))
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: pytest.fail("dry-run requested network"))
    monkeypatch.setattr(publisher, "load_private_key", lambda *_: key)
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test bundle")
    dest = tmp_path / "prepared"
    assert publisher.main([
        "--version", "1.0.1", "--min-version", "1.0.0", "--key", "unused",
        "--bundle", str(bundle), "--dry-run", "--output-dir", str(dest),
        "--expected-project", "bpidntbyvoooqvaispup", "--installer-from-settings",
    ]) == 0
    row = json.loads((dest / "release-1.0.1.json").read_text())
    assert row["sha256"] == publisher.sha256_file(bundle)
    assert publisher.verify(publisher.public_raw(key), row["manifest"], row["signature"])
    assert "__RELEASE_JSON_B64__" in (dest / "bdr-install-template.ps1").read_text(encoding="utf-8-sig")


def test_relative_build_paths_survive_worker_subprocess_cwd(tmp_path, monkeypatch):
    caller = tmp_path / "backend"
    worker = caller / "agent_worker"
    worker.mkdir(parents=True)
    wheels = caller / "input-wheels"
    wheels.mkdir()
    (wheels / "marker").write_text("relative input")
    builder = worker / "builder.py"
    builder.write_text('''import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--version')
p.add_argument('--out')
p.add_argument('--wheels-dir')
a = p.parse_args()
assert (Path(a.wheels_dir) / 'marker').read_text() == 'relative input'
dest = Path(a.out) / ('bdr-agent-' + a.version + '.zip')
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_bytes(b'fixture bundle')
print(json.dumps({'path': str(dest)}))
''')
    monkeypatch.chdir(caller)
    monkeypatch.setattr(publisher, "WORKER", worker)
    monkeypatch.setattr(publisher, "BUNDLE_TOOL", builder)
    bundle = publisher.build_bundle("1.0.1", Path("dist/release"), Path("input-wheels"))
    assert bundle == caller / "dist/release/bdr-agent-1.0.1.zip"
    assert bundle.read_bytes() == b"fixture bundle"
    assert not (worker / "dist").exists()


def test_missing_bundle_fails_before_prompting_for_key(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(publisher, "load_private_key", lambda *_: pytest.fail("asked for key before bundle exists"))
    with pytest.raises(SystemExit, match="bundle was not found"):
        publisher.main(["--version", "1.0.1", "--key", "unused", "--dry-run",
                        "--bundle", str(tmp_path / "missing.zip")])


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


def test_runtime_setup_route_requires_prepared_public_script(monkeypatch):
    h = Harness(monkeypatch)
    path = f"{API}/python-runtime.ps1"
    assert h.client.get(path).status_code == 404
    key = ("browser-agent", "installer/setup-python-runtime.ps1")
    h.db.storage.store[key] = b"$ExpectedSha256 = '__PYTHON_NUGET_SHA256__'"
    assert h.client.get(path).status_code == 404
    prepared = b"$ExpectedSha256 = '" + b"a" * 64 + b"'"
    h.db.storage.store[key] = prepared
    response = h.client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.content == prepared


@pytest.mark.parametrize('accepted', [False, True])
def test_interrupted_upload_reconciles_before_retry(keys, tmp_path, monkeypatch, accepted):
    _, key, _ = keys
    bundle = tmp_path / 'bundle.zip'
    bundle.write_bytes(b'signed bundle')
    db = FakeDB()
    stored = {}
    calls = []

    class Bucket:
        def upload(self, path, data, options):
            assert options.pop('upsert') == 'false'
            calls.append(path)
            if len(calls) == 1:
                if accepted:
                    stored[path] = data
                raise publisher.httpx.ReadError('secret-bearing transport details')
            stored[path] = data

        def download(self, path):
            if path not in stored:
                raise publisher.StorageApiError('missing', 'NoSuchKey', '404')
            return stored[path]

    monkeypatch.setattr(db.storage, 'from_', lambda _: Bucket())
    monkeypatch.setattr(publisher.time, 'sleep', lambda _: None)
    publisher.publish(db, version='1.0.2', min_version='1.0.0', bundle=bundle, key=key)
    assert len(calls) == (1 if accepted else 2)
    assert len(db.rows('browser_agent_worker_releases')) == 1


@pytest.mark.parametrize('read_result', ['mismatch', 'unknown', 'denied', 'missing'])
def test_upload_recovery_fails_closed_and_bounds_retries(keys, tmp_path, monkeypatch, read_result):
    _, key, _ = keys
    bundle = tmp_path / 'bundle.zip'
    bundle.write_bytes(b'signed bundle')
    db = FakeDB()
    calls = []

    class Bucket:
        def upload(self, path, data, options):
            assert options['upsert'] == 'false'
            calls.append(path)
            raise publisher.httpx.ReadError('SECRET')

        def download(self, path):
            if read_result == 'mismatch':
                return b'other bundle'
            if read_result == 'unknown':
                raise publisher.httpx.ReadError('SECRET')
            raise publisher.StorageApiError('SECRET', 'error', 404 if read_result == 'missing' else 403)

    monkeypatch.setattr(db.storage, 'from_', lambda _: Bucket())
    monkeypatch.setattr(publisher.time, 'sleep', lambda _: None)
    with pytest.raises(publisher.PublishError) as caught:
        publisher.publish(db, version='1.0.2', min_version='1.0.0', bundle=bundle, key=key)
    assert 'SECRET' not in str(caught.value)
    assert len(calls) == (3 if read_result == 'missing' else 1)
    assert not db.rows('browser_agent_worker_releases')


def test_duplicate_orphan_matches_without_overwrite(monkeypatch):
    data = b'existing matching orphan'

    class Bucket:
        def upload(self, path, body, options):
            assert options['upsert'] == 'false'
            raise publisher.StorageApiError('already exists', 'Duplicate', 400)

        def download(self, path):
            return data

    publisher.upload_release_bundle(Bucket(), 'release.zip', data, publisher.hashlib.sha256(data).hexdigest())


def test_changed_bundle_and_permission_errors_are_not_retried():
    calls = []

    class Bucket:
        def upload(self, *_):
            calls.append(1)
            raise publisher.StorageApiError('SECRET', 'AccessDenied', 403)

        def download(self, *_):
            pytest.fail('must not reconcile access denial')

    with pytest.raises(publisher.PublishError, match='changed after signing'):
        publisher.upload_release_bundle(Bucket(), 'release.zip', b'data', '0' * 64)
    assert not calls
    with pytest.raises(publisher.PublishError, match='Storage refused'):
        publisher.upload_release_bundle(Bucket(), 'release.zip', b'data', publisher.hashlib.sha256(b'data').hexdigest())
    assert len(calls) == 1


@pytest.mark.parametrize('stage', ['upload', 'download'])
def test_malformed_storage_response_is_redacted_and_not_retried(stage):
    calls = []

    class Bucket:
        def upload(self, *_):
            calls.append(1)
            if stage == 'upload':
                raise ValueError('SECRET response body')
            raise publisher.httpx.ReadError('SECRET transport')

        def download(self, *_):
            raise AttributeError('SECRET response body')

    with pytest.raises(publisher.PublishError, match='Unexpected storage response') as caught:
        publisher.upload_release_bundle(Bucket(), 'release.zip', b'data', publisher.hashlib.sha256(b'data').hexdigest())
    assert 'SECRET' not in str(caught.value)
    assert len(calls) == 1


def test_signed_checkpoint_survives_transfer_failure_and_resumes_without_key(keys, tmp_path, monkeypatch):
    from app.core import config, supabase_client

    out, key, _ = keys
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(
        supabase_url='https://bpidntbyvoooqvaispup.supabase.co',
        browser_agent_release_pubkey=out['public_b64']))
    monkeypatch.setattr(supabase_client, 'get_supabase', FakeDB)
    monkeypatch.setattr(publisher, 'load_private_key', lambda *_: key)
    bundle = tmp_path / 'bundle.zip'
    bundle.write_bytes(b'fixture')
    dest = tmp_path / 'signed'
    args = ['--version', '1.0.2', '--key', 'unused', '--bundle', str(bundle), '--output-dir', str(dest)]

    def fail(*_args, **_kwargs):
        assert (dest / 'release-1.0.2.json').exists()
        raise publisher.PublishError('simulated disconnect')

    monkeypatch.setattr(publisher, 'register_release', fail)
    with pytest.raises(publisher.PublishError, match='simulated disconnect'):
        publisher.main(args)
    monkeypatch.setattr(publisher, 'load_private_key', lambda *_: pytest.fail('resume opened signing key'))
    assert publisher.main([*args, '--resumable', '--dry-run']) == 0
    bundle.write_bytes(b'changed')
    with pytest.raises(publisher.PublishError, match='does not match'):
        publisher.main([*args, '--resumable', '--dry-run'])


def test_resumable_registration_verifies_remote_bytes_before_insert(keys, tmp_path, monkeypatch):
    _, key, _ = keys
    bundle = tmp_path / 'bundle.zip'
    bundle.write_bytes(b'fixture')
    row = publisher.publish(None, version='1.0.2', min_version='1.0.0', bundle=bundle, key=key, dry_run=True)
    db = FakeDB()
    stored = {}

    class Bucket:
        def download(self, path):
            if path not in stored:
                raise publisher.StorageApiError('missing', 'NoSuchKey', 404)
            return stored[path]

    monkeypatch.setattr(db.storage, 'from_', lambda _: Bucket())

    def wrong_upload(path, sha):
        stored[path] = b'wrong bytes'

    with pytest.raises(publisher.PublishError, match='differs'):
        publisher.register_release(db, row, bundle, wrong_upload, resume=True)
    assert not db.rows('browser_agent_worker_releases')
    stored.clear()
    publisher.register_release(db, row, bundle, lambda path, sha: stored.update({path: bundle.read_bytes()}), resume=True)
    publisher.register_release(db, row, bundle, lambda *_: pytest.fail('reuploaded registered release'), resume=True)
    assert len(db.rows('browser_agent_worker_releases')) == 1
    changed = dict(row, notes='changed')
    with pytest.raises(publisher.PublishError, match='immutable'):
        publisher.register_release(db, changed, bundle, resume=True)


def test_cli_resumable_transfer_reconciles_lost_completion(keys, tmp_path, monkeypatch):
    from app.core import config, supabase_client
    from scripts import agent_release_upload

    out, key, _ = keys
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(
        supabase_url='https://bpidntbyvoooqvaispup.supabase.co',
        supabase_service_role_key='fixture-token', browser_agent_release_pubkey=out['public_b64']))
    db = FakeDB()
    objects = {}

    class Bucket:
        def download(self, path):
            if path not in objects:
                raise publisher.StorageApiError('missing', 'NoSuchKey', 404)
            return objects[path]

    monkeypatch.setattr(db.storage, 'from_', lambda _: Bucket())
    monkeypatch.setattr(supabase_client, 'get_supabase', lambda: db)
    monkeypatch.setattr(publisher, 'load_private_key', lambda *_: key)
    bundle = tmp_path / 'bundle.zip'
    bundle.write_bytes(b'fixture completed tus bundle')
    dest = tmp_path / 'release'

    def transfer(**kwargs):
        assert kwargs['token'] == 'fixture-token'
        assert (dest / 'release-1.0.2.json').exists()
        objects[kwargs['object_name']] = kwargs['bundle'].read_bytes()
        raise agent_release_upload.UploadError('completion HEAD failed')

    monkeypatch.setattr(agent_release_upload, 'upload_resumable', transfer)
    args = ['--version', '1.0.2', '--key', 'unused', '--bundle', str(bundle),
            '--output-dir', str(dest), '--resumable', '--expected-project', 'bpidntbyvoooqvaispup']
    assert publisher.main(args) == 0
    monkeypatch.setattr(publisher, 'load_private_key', lambda *_: pytest.fail('opened key on completed retry'))
    monkeypatch.setattr(agent_release_upload, 'upload_resumable', lambda **_: pytest.fail('reuploaded completed release'))
    assert publisher.main(args) == 0
    assert len(db.rows('browser_agent_worker_releases')) == 1
