"""Enrollment: one-time code + release key fingerprint check, token and worker.json written."""

import base64
import json
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bdr_agent.__main__ import build_parser, cmd_enroll, cmd_fingerprint, normalize_code
from bdr_agent.local_config import load_local_config
from bdr_agent.protocol import ReleaseManifest, canonical_manifest_bytes
from bdr_agent.release_key import fingerprint, fingerprint_matches, load_public_key, verify_manifest
from bdr_agent.tokenstore import TokenStore

from .conftest import BASE

CODE = "ABCDEFGHJKMNPQRS2345"


def _write_key(paths):
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    paths.release_pubkey.write_text(base64.b64encode(raw).decode())
    return priv, fingerprint(load_public_key(paths.release_pubkey))


def _args(paths, *extra):
    return build_parser().parse_args(["--home", str(paths.home), "enroll", "--api", BASE, *extra])


def test_enroll_with_matching_fingerprint(bdr, paths, capsys):
    _priv, fp = _write_key(paths)
    bdr.add_enroll_code(CODE)
    code = cmd_enroll(_args(paths, "--code", "abcd-efgh-jkmn-pqrs-2345", "--fingerprint", fp.lower()),
                      transport=bdr.transport())
    assert code == 0
    token = TokenStore(paths.token_file).load()
    worker = bdr.workers[token]
    assert worker.status == "pending"
    cfg = load_local_config(paths)
    assert cfg.api_base_url == BASE and cfg.worker_id == worker.worker_id
    assert token not in capsys.readouterr().out
    call = bdr.calls_to("enroll")[0]
    assert "authorization" not in call.headers and call.body["code"] == CODE
    assert set(json.loads(paths.worker_json.read_text())) == {"api_base_url", "worker_id"}


def test_enroll_refuses_fingerprint_mismatch_without_calling_bdr(bdr, paths):
    _write_key(paths)
    bdr.add_enroll_code(CODE)
    code = cmd_enroll(_args(paths, "--code", CODE, "--fingerprint", "00" * 32), transport=bdr.transport())
    assert code == 2
    assert not bdr.calls_to("enroll")
    assert TokenStore(paths.token_file).load() is None


def test_enroll_yes_needs_fingerprint_and_prompt_needs_yes(bdr, paths, monkeypatch):
    _write_key(paths)
    bdr.add_enroll_code(CODE)
    assert cmd_enroll(_args(paths, "--code", CODE, "--yes"), transport=bdr.transport()) == 2
    monkeypatch.setattr("builtins.input", lambda _p: "no")
    assert cmd_enroll(_args(paths, "--code", CODE), transport=bdr.transport()) == 2
    monkeypatch.setattr("builtins.input", lambda _p: "yes")
    assert cmd_enroll(_args(paths, "--code", CODE), transport=bdr.transport()) == 0


def test_enroll_bad_code_missing_key_and_already_enrolled(bdr, paths):
    assert cmd_enroll(_args(paths, "--code", "short"), transport=bdr.transport()) == 2
    assert cmd_enroll(_args(paths, "--code", CODE, "--fingerprint", "00"), transport=bdr.transport()) == 2
    _priv, fp = _write_key(paths)
    # Unknown code: BDR answers enroll_invalid.
    assert cmd_enroll(_args(paths, "--code", CODE, "--fingerprint", fp), transport=bdr.transport()) == 2
    bdr.add_enroll_code(CODE)
    assert cmd_enroll(_args(paths, "--code", CODE, "--fingerprint", fp), transport=bdr.transport()) == 0
    bdr.add_enroll_code(CODE)
    assert cmd_enroll(_args(paths, "--code", CODE, "--fingerprint", fp), transport=bdr.transport()) == 2
    assert cmd_enroll(_args(paths, "--code", CODE, "--fingerprint", fp, "--force"),
                      transport=bdr.transport()) == 0


def test_fingerprint_command_and_manifest_signature(paths, capsys):
    priv, fp = _write_key(paths)
    args = build_parser().parse_args(["--home", str(paths.home), "fingerprint"])
    assert cmd_fingerprint(args) == 0
    assert capsys.readouterr().out.strip() == fp
    key = load_public_key(paths.release_pubkey)
    assert fingerprint_matches(key, fp.replace(" ", ":"))
    m = ReleaseManifest(version="1.2.3", sha256="a" * 64, min_version="1.0.0",
                        created_at=datetime(2026, 10, 19, tzinfo=UTC))
    sig = base64.b64encode(priv.sign(canonical_manifest_bytes(m))).decode()
    assert verify_manifest(key, m, sig)
    tampered = m.model_copy(update={"min_version": "0.9.0"})
    assert not verify_manifest(key, tampered, sig)
    assert not verify_manifest(key, m, "not base64!")


def test_normalize_code():
    assert normalize_code(" abcd-efgh jkmn pqrs 2345 ") == CODE
