"""Launcher release checks (stdlib Ed25519, signature, min_version, floor,
bad_versions, sha256) and side-by-side installs, without Windows or network."""

import base64
import json
import os
import zipfile
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bdr_agent.protocol import ReleaseManifest, canonical_manifest_bytes
from bdr_agent.release_key import fingerprint as worker_fingerprint
from bdr_agent.release_key import load_public_key as worker_load_key
from bdr_launcher import cli, ed25519, release
from bdr_launcher.release import ReleaseRejected, check_release, safe_extract, stage_bundle
from bdr_launcher.state import load_state, read_pending, save_state

from .release_helpers import FakeInstall, ReleaseKey, build_signed_bundle, launcher_paths, manifest

# Ed25519 ---------------------------------------------------------------------------


def test_stdlib_ed25519_agrees_with_cryptography():
    for i in range(6):
        priv = Ed25519PrivateKey.generate()
        raw = priv.public_key().public_bytes_raw()
        msg = os.urandom(i * 37)
        sig = priv.sign(msg)
        assert ed25519.verify(raw, msg, sig)
        assert not ed25519.verify(raw, msg + b"x", sig)
        bad = bytearray(sig)
        bad[i] ^= 1
        assert not ed25519.verify(raw, msg, bytes(bad))
        other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        assert not ed25519.verify(other, msg, sig)


def test_rfc8032_test_vector_1():
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert ed25519.verify(pub, b"", sig)


def test_ed25519_rejects_malformed_input():
    key = ReleaseKey()
    sig = key.priv.sign(b"m")
    assert not ed25519.verify(key.raw[:31], b"m", sig)
    assert not ed25519.verify(key.raw, b"m", sig[:63])
    assert not ed25519.verify(b"\xff" * 32, b"m", sig)
    # s >= L is malleable and refused.
    s = int.from_bytes(sig[32:], "little") + 2**252 + 27742317777372353535851937790883648493
    if s < 2**256:
        assert not ed25519.verify(key.raw, b"m", sig[:32] + s.to_bytes(32, "little"))
    assert not ed25519.verify("not bytes", b"m", sig)  # type: ignore[arg-type]


# Keys and manifests ------------------------------------------------------------------


def test_fingerprint_and_key_formats_match_the_worker(tmp_path):
    key = ReleaseKey()
    b64 = key.write(tmp_path / "k.pub")
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.pem())
    assert release.load_public_key(b64) == key.raw == release.load_public_key(pem)
    assert release.fingerprint(key.raw) == worker_fingerprint(worker_load_key(b64))
    (tmp_path / "bad.pub").write_text(base64.b64encode(b"short").decode())
    with pytest.raises(ReleaseRejected) as e:
        release.load_public_key(tmp_path / "bad.pub")
    assert e.value.reason == "bad_release_key"
    with pytest.raises(ReleaseRejected):
        release.load_public_key(tmp_path / "missing.pub")


def test_canonical_bytes_match_the_protocol():
    m = manifest("1.2.3", "cd" * 32, "1.1.0")
    assert release.canonical_manifest_bytes(m) == canonical_manifest_bytes(m)
    model = ReleaseManifest.model_validate(m)
    assert release.canonical_manifest_bytes(m) == canonical_manifest_bytes(model)


@pytest.mark.parametrize("created_at", ["2026-10-18T20:00:00+00:00", "2026-10-18T20:00:00.120000Z",
                                        "2026-10-18T22:00:00+02:00", "2026-10-18T20:00:00.000001+00:00"])
def test_created_at_spellings_cover_the_pydantic_form(created_at):
    m = manifest("1.0.0", created_at=created_at)
    dumped = ReleaseManifest.model_validate(m).model_dump(mode="json")
    spellings = release._created_at_spellings(created_at)
    assert spellings[0] == created_at and dumped["created_at"] in spellings
    assert canonical_manifest_bytes(dumped) in [release.canonical_manifest_bytes(m, created_at=c) for c in spellings]


@pytest.mark.parametrize("signed", ["2026-10-18T20:00:00+00:00", "2026-10-18T20:00:00Z"])
def test_signature_over_either_utc_spelling_is_accepted(signed):
    key = ReleaseKey()
    m = manifest("1.1.0", created_at=signed)
    sig = key.sign(m)
    assert release.signature_valid(key.raw, m, sig)
    # The worker hands over the pydantic dump ("Z"): still the same signed instant.
    dumped = ReleaseManifest.model_validate(m).model_dump(mode="json")
    assert release.signature_valid(key.raw, dumped, sig)
    assert release.signature_valid(key.raw, {**m, "created_at": "2026-10-18T20:00:00+00:00"}, sig)
    assert not release.signature_valid(key.raw, {**m, "created_at": "2026-10-18T20:00:01Z"}, sig)
    assert not release.signature_valid(key.raw, {**m, "min_version": "1.0.1"}, sig)
    assert not release.signature_valid(key.raw, m, "%%%")


def test_check_release_rules():
    key = ReleaseKey()

    def ok(m, **kw):
        return check_release(m, key.sign(m), key.raw, **kw)

    def reason(m, sig=None, **kw):
        with pytest.raises(ReleaseRejected) as e:
            check_release(m, sig if sig is not None else key.sign(m), key.raw, **kw)
        return e.value.reason

    good = manifest("1.2.0", min_version="1.1.0")
    assert ok(good) == good
    assert ok(good, floor="1.2.0", bad_versions=["1.1.9"]) == good
    assert reason(good, sig=ReleaseKey().sign(good)) == "bad_signature"
    assert reason(good, sig=key.sign(manifest("1.2.0", min_version="1.0.0"))) == "bad_signature"
    assert reason(good, expected_version="1.3.0") == "version_mismatch"
    assert reason(manifest("1.0.5", min_version="1.1.0")) == "below_min_version"
    assert reason(good, floor="1.3.0") == "below_floor"
    assert reason(manifest("0.9.0", min_version="0.9.0")) == "below_floor"   # embedded floor 1.0.0
    assert reason(good, bad_versions=["1.2.0"]) == "bad_version"
    assert reason({"version": "1.2.0"}, sig="x") == "manifest_invalid"
    assert reason({**good, "sha256": "XYZ"}, sig="x") == "manifest_invalid"
    assert reason({**good, "version": "1.2"}, sig="x") == "manifest_invalid"


# Extraction --------------------------------------------------------------------------


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            if isinstance(data, zipfile.ZipInfo):
                zf.writestr(data, b"x")
            else:
                zf.writestr(name, data)
    return path


@pytest.mark.parametrize("name", ["../evil.py", "a/../../evil.py", "/abs.py", "C:/win.py", "\\\\srv\\x.py"])
def test_safe_extract_refuses_escaping_paths(tmp_path, name):
    z = _zip(tmp_path / "b.zip", [("ok.txt", b"1"), (name, b"2")])
    with pytest.raises(ReleaseRejected) as e:
        safe_extract(z, tmp_path / "out")
    assert e.value.reason == "bundle_invalid"
    assert not (tmp_path / "evil.py").exists()


def test_safe_extract_refuses_symlinks_and_bombs(tmp_path):
    info = zipfile.ZipInfo("link")
    info.external_attr = (0o120777 << 16)
    z = _zip(tmp_path / "s.zip", [("link", info)])
    with pytest.raises(ReleaseRejected):
        safe_extract(z, tmp_path / "out")
    z2 = _zip(tmp_path / "big.zip", [("a", b"0" * 1000)])
    with pytest.raises(ReleaseRejected):
        safe_extract(z2, tmp_path / "out2", max_bytes=100)
    (tmp_path / "nz.zip").write_bytes(b"not a zip")
    with pytest.raises(ReleaseRejected):
        safe_extract(tmp_path / "nz.zip", tmp_path / "out3")


# Staging ----------------------------------------------------------------------------


def test_stage_bundle_installs_side_by_side(tmp_path):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    zip_path, m, sig = build_signed_bundle(tmp_path, key, "1.1.0", min_version="1.0.5")
    run = FakeInstall()
    res = stage_bundle(paths, zip_path, m, sig, runtime="/rt/python", floor="1.0.0", bad_versions=[], run=run)
    assert (res.version, res.min_version, res.reused) == ("1.1.0", "1.0.5", False)
    assert run.steps() == ["venv", "pip", "chromium", "smoke"]
    assert run.calls[0][:3] == ["/rt/python", "-m", "venv"]
    pip = run.calls[1]
    for flag in ("--no-index", "--require-hashes", "--no-deps", "--only-binary"):
        assert flag in pip
    assert all(e["PLAYWRIGHT_BROWSERS_PATH"] == str(paths.browsers_dir) for e in run.envs)
    vdir = paths.version_dir("1.1.0")
    assert release.is_installed(paths, "1.1.0")
    info = release.read_installed(vdir)
    assert info["sha256"] == m["sha256"] and info["manifest"] == m and info["signature"] == sig
    assert (vdir / "app" / "bdr_agent" / "__init__.py").exists()
    assert not list((vdir / "app" / "bdr_agent").glob("__pycache__"))
    pth = next(vdir.glob("venv/**/bdr_agent_app.pth"))
    assert (pth.parent / pth.read_text().strip()).resolve() == (vdir / "app").resolve()
    assert not list(paths.versions_dir.glob("*.partial-*"))
    assert release.installed_min_version(paths, "1.1.0") == "1.0.5"
    # Second stage of the same bytes reuses the install.
    run2 = FakeInstall()
    assert stage_bundle(paths, zip_path, m, sig, runtime="/rt/python", floor=None, bad_versions=[],
                        run=run2).reused
    assert run2.calls == []


def test_stage_bundle_skips_chromium_when_the_bundle_says_so(tmp_path):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    zip_path, m, sig = build_signed_bundle(tmp_path, key, "1.1.0", chromium=False)
    run = FakeInstall()
    stage_bundle(paths, zip_path, m, sig, runtime="py", floor=None, bad_versions=[], run=run)
    assert run.steps() == ["venv", "pip", "smoke"]


def test_stage_bundle_rejects_tampered_zip_bad_signature_and_floor(tmp_path):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    zip_path, m, sig = build_signed_bundle(tmp_path, key, "1.1.0", min_version="1.1.0")
    run = FakeInstall()

    def reason(**kw):
        args = dict(zip_path=zip_path, manifest=m, signature=sig, runtime="py", floor=None, bad_versions=[],
                    run=run)
        args.update(kw)
        with pytest.raises(ReleaseRejected) as e:
            stage_bundle(paths, args.pop("zip_path"), args.pop("manifest"), args.pop("signature"), **args)
        return e.value.reason

    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(zip_path.read_bytes() + b"\0")
    assert reason(zip_path=tampered) == "sha_mismatch"
    assert reason(signature=ReleaseKey().sign(m)) == "bad_signature"
    assert reason(floor="1.2.0") == "below_floor"
    assert reason(bad_versions=["1.1.0"]) == "bad_version"
    # A signed manifest whose sha belongs to another bundle.
    other_zip, _m2, _s2 = build_signed_bundle(tmp_path / "o", key, "1.1.0", chromium=False)
    assert reason(zip_path=other_zip) == "sha_mismatch"
    assert run.calls == []
    assert not paths.version_dir("1.1.0").exists()


def test_stage_bundle_validates_bundle_contents(tmp_path):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("bundle.json", json.dumps({"format": 1, "version": "1.2.0"}))
        zf.writestr("app/bdr_agent/__init__.py", "__version__ = '1.1.0'\n")
        zf.writestr("requirements.lock", "httpx==0.28.1\n")   # no hash
        zf.writestr("wheels/x.whl", "w")
    m = manifest("1.1.0", release.sha256_file(bad))
    with pytest.raises(ReleaseRejected) as e:
        stage_bundle(paths, bad, m, key.sign(m), runtime="py", floor=None, bad_versions=[], run=FakeInstall())
    assert e.value.reason == "bundle_invalid" and "version" in e.value.detail
    assert not list(paths.versions_dir.iterdir())


@pytest.mark.parametrize("step", ["venv", "pip", "chromium", "smoke"])
def test_failed_install_step_leaves_nothing_behind(tmp_path, step):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    zip_path, m, sig = build_signed_bundle(tmp_path, key, "1.1.0")
    with pytest.raises(ReleaseRejected) as e:
        stage_bundle(paths, zip_path, m, sig, runtime="py", floor=None, bad_versions=[],
                     run=FakeInstall(fail=step))
    assert e.value.reason == "install_failed"
    assert not list(paths.versions_dir.iterdir())


def test_remove_partials_and_prune(tmp_path):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    for v in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
        zip_path, m, sig = build_signed_bundle(tmp_path / v, key, v)
        stage_bundle(paths, zip_path, m, sig, runtime="py", floor=None, bad_versions=[], run=FakeInstall())
    (paths.versions_dir / "1.4.0.partial-99").mkdir()
    (paths.versions_dir / "1.5.0").mkdir()     # no INSTALLED.json: incomplete
    assert release.remove_partials(paths) == 2
    removed = release.prune_versions(paths, ["1.1.0", "1.0.0"])
    assert removed == ["1.2.0"]
    assert sorted(p.name for p in paths.versions_dir.iterdir()) == ["1.0.0", "1.1.0", "1.3.0"]


# CLI: stage (worker updater) and install-bundle (installer) --------------------------


def _release_json(tmp_path, m, sig):
    p = tmp_path / "release.json"
    p.write_text(json.dumps({"manifest": m, "signature": sig, "url": "https://x/y.zip?token=t"}))
    return p


def _cli(paths, *args):
    return cli.main(["--home", str(paths.home), "--launcher-dir", str(paths.launcher_dir), *args])


def test_cli_stage_writes_pending_and_reports_json(tmp_path, monkeypatch, capsys):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    monkeypatch.setattr(release, "subprocess_runner", FakeInstall())
    zip_path, m, sig = build_signed_bundle(tmp_path, key, "1.1.0")
    code = _cli(paths, "stage", "--zip", str(zip_path), "--release-json", str(_release_json(tmp_path, m, sig)))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["ok"] and out["version"] == "1.1.0" and out["pending"]
    assert read_pending(paths.pending_file) == "1.1.0"


def test_cli_stage_refuses_bad_versions_and_floor_from_state(tmp_path, monkeypatch, capsys):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    monkeypatch.setattr(release, "subprocess_runner", FakeInstall())
    state = load_state(paths.state_file)
    state.bad_versions = ["1.1.0"]
    state.min_version_floor = "1.0.0"
    save_state(paths.state_file, state)
    zip_path, m, sig = build_signed_bundle(tmp_path, key, "1.1.0")
    rj = _release_json(tmp_path, m, sig)
    assert _cli(paths, "stage", "--zip", str(zip_path), "--release-json", str(rj)) == cli.EXIT_REJECTED
    assert json.loads(capsys.readouterr().out.strip())["reason"] == "bad_version"
    state.bad_versions = []
    state.min_version_floor = "1.2.0"
    save_state(paths.state_file, state)
    assert _cli(paths, "stage", "--zip", str(zip_path), "--release-json", str(rj)) == cli.EXIT_REJECTED
    assert json.loads(capsys.readouterr().out.strip())["reason"] == "below_floor"
    assert read_pending(paths.pending_file) is None


def test_cli_install_bundle_activates_only_when_newer(tmp_path, monkeypatch, capsys):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    monkeypatch.setattr(release, "subprocess_runner", FakeInstall())
    z1, m1, s1 = build_signed_bundle(tmp_path / "a", key, "1.1.0", min_version="1.1.0")
    assert _cli(paths, "install-bundle", "--zip", str(z1), "--release-json", str(_release_json(tmp_path, m1, s1))) == 0
    st = load_state(paths.state_file)
    assert (st.current, st.previous, st.min_version_floor) == ("1.1.0", None, "1.1.0")
    z2, m2, s2 = build_signed_bundle(tmp_path / "b", key, "1.2.0", min_version="1.1.0")
    assert _cli(paths, "install-bundle", "--zip", str(z2), "--release-json", str(_release_json(tmp_path, m2, s2))) == 0
    st = load_state(paths.state_file)
    assert (st.current, st.previous) == ("1.2.0", "1.1.0")
    # Re-running an older installer keeps the newer current.
    assert _cli(paths, "install-bundle", "--zip", str(z1), "--release-json", str(_release_json(tmp_path, m1, s1))) == 0
    assert load_state(paths.state_file).current == "1.2.0"
    capsys.readouterr()
    assert _cli(paths, "status", "--python-path") == 0
    assert "1.2.0" in capsys.readouterr().out
    assert _cli(paths, "fingerprint") == 0
    assert capsys.readouterr().out.strip() == release.fingerprint(key.raw)


def test_cli_install_bundle_rejects_a_forged_release(tmp_path, monkeypatch, capsys):
    key = ReleaseKey()
    paths = launcher_paths(tmp_path, key)
    run = FakeInstall()
    monkeypatch.setattr(release, "subprocess_runner", run)
    z, m, _sig = build_signed_bundle(tmp_path, key, "1.1.0")
    forged = ReleaseKey().sign(m)
    assert _cli(paths, "install-bundle", "--zip", str(z), "--release-json",
                str(_release_json(tmp_path, m, forged))) == cli.EXIT_REJECTED
    assert json.loads(capsys.readouterr().out.strip())["reason"] == "bad_signature"
    assert run.calls == [] and load_state(paths.state_file).current is None


def test_bundle_build_is_deterministic(tmp_path):
    key = ReleaseKey()
    z1, m1, _ = build_signed_bundle(tmp_path / "a", key, "1.1.0")
    z2, m2, _ = build_signed_bundle(tmp_path / "b", key, "1.1.0")
    assert m1["sha256"] == m2["sha256"] and z1.read_bytes() == z2.read_bytes()


def test_created_at_now_round_trip():
    key = ReleaseKey()
    m = manifest("1.0.0", created_at=datetime.now(UTC).isoformat())
    assert check_release(m, key.sign(m), key.raw)
