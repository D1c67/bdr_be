"""Encrypted secret store and the shared Procore credentials
(services/browser_agents/secrets.py, services/procore_creds.py;
docs/browser_agents/SPEC.md 2.2, 5.5, 10.1, override O2).

Pinned: AES-GCM round trip; a ciphertext moved to another name or version
never decrypts; rotation through BROWSER_AGENT_SECRETS_OLD_KEYS and
`reencrypt_all`; the HMAC fingerprint; write-only views (no plaintext in any
row, change log or return value); a new password clears the shared login
lock; `procore_creds.password` reads the database fresh with the env as the
fallback; `configured` caches; the turn scrub learns the stored password;
and no `procore_configured` read survives in app/ outside config.py and
procore_creds.py.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from app.services import procore_creds
from app.services.browser_agents import redaction, secrets
from tests.browser_agents_harness import KEY_A, Harness

PASSWORD = "S3cret-Procore!"
KEY_C = base64.b64encode(b"C" * 32).decode()


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch)
    for mod in (secrets, procore_creds, redaction):
        monkeypatch.setattr(mod, "get_settings", lambda: harness.settings)
    procore_creds.reset_cache()
    return harness


def _dump(h) -> str:
    return json.dumps(h.db.tables, default=str) + json.dumps(h.audits, default=str)


# ── AES-GCM ───────────────────────────────────────────────────────────────────


def test_round_trip_and_fresh_nonces(h):
    ct1, nonce1, key_id = secrets.encrypt_with_aad(PASSWORD, b"aad", h.settings)
    ct2, nonce2, _ = secrets.encrypt_with_aad(PASSWORD, b"aad", h.settings)
    assert key_id == "k1" and len(nonce1) == 12 and nonce1 != nonce2 and ct1 != ct2
    assert PASSWORD.encode() not in ct1
    assert secrets.decrypt_with_aad(ct1, nonce1, b"aad", "k1", h.settings) == PASSWORD


def test_a_ciphertext_moved_to_another_name_or_version_never_decrypts(h):
    secrets.set_secret("procore_password", PASSWORD, actor_id=None, actor_label="IT Admin")
    row = dict(h.db.one("browser_agent_secrets", name="procore_password"))
    assert secrets.decrypt_row(row, h.settings) == PASSWORD
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt_row({**row, "name": "other_password"}, h.settings)
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt_row({**row, "version": 2}, h.settings)
    tampered = "\\x" + ("00" if row["ciphertext"][2:4] != "00" else "11") + row["ciphertext"][4:]
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt_row({**row, "ciphertext": tampered}, h.settings)


def test_rotation_reads_old_keys_and_reencrypts(h, monkeypatch):
    secrets.set_secret("procore_password", PASSWORD, actor_id=None, actor_label="IT Admin")
    assert h.db.one("browser_agent_secrets", name="procore_password")["key_id"] == "k1"
    rotated = h.settings.model_copy(update={
        "browser_agent_secrets_key": KEY_C, "browser_agent_secrets_key_id": "k2",
        "browser_agent_secrets_old_keys": f"k1:{KEY_A}",
    })
    assert secrets.get_secret("procore_password", rotated) == PASSWORD
    assert secrets.reencrypt_all(rotated) == 1
    row = h.db.one("browser_agent_secrets", name="procore_password")
    assert row["key_id"] == "k2" and row["version"] == 1
    no_old = rotated.model_copy(update={"browser_agent_secrets_old_keys": ""})
    assert secrets.get_secret("procore_password", no_old) == PASSWORD
    assert secrets.reencrypt_all(no_old) == 0
    with pytest.raises(secrets.SecretsKeyMissing):
        secrets.get_secret("procore_password", h.settings)    # k2 is unknown to the old config


def test_fingerprint_is_8_hex_and_keyed(h):
    fp = secrets.fingerprint(PASSWORD, h.settings)
    assert re.fullmatch(r"[0-9a-f]{8}", fp) and fp == secrets.fingerprint(PASSWORD, h.settings)
    other = h.settings.model_copy(update={"browser_agent_fingerprint_key": KEY_C})
    assert secrets.fingerprint(PASSWORD, other) != fp
    assert secrets.fingerprint("another", h.settings) != fp


# ── Write-only store ──────────────────────────────────────────────────────────


def test_set_secret_is_write_only_versioned_audited_and_resets_the_lock(h):
    h.db.tables["rfp_harvest_sessions"] = [{
        "provider": "procore", "login_failures": 3, "locked_until": "2026-10-20T00:00:00+00:00",
        "last_login_attempt_at": "2026-10-19T16:59:00+00:00", "last_error": "password: rejected",
    }]
    view = secrets.set_secret("procore_password", PASSWORD, actor_id="u-it", actor_label="Ina (IT Admin)")
    assert view == {"name": "procore_password", "set": True, "set_at": view["set_at"],
                    "set_by_label": "Ina (IT Admin)", "fingerprint": secrets.fingerprint(PASSWORD, h.settings)}
    view2 = secrets.set_secret("procore_password", PASSWORD + "2", actor_id="u-it", actor_label="Ina (IT Admin)")
    assert h.db.one("browser_agent_secrets", name="procore_password")["version"] == 2
    assert view2["fingerprint"] != view["fingerprint"]
    session = h.db.one("rfp_harvest_sessions", provider="procore")
    assert (session["login_failures"], session["locked_until"], session["last_login_attempt_at"]) == (0, None, None)
    changes = h.db.rows("browser_agent_settings_changes", scope="secret")
    assert [c["new_value"]["set"] for c in changes] == [True, True]
    assert changes[1]["old_value"] == {"set": True, "fingerprint": view["fingerprint"]}
    assert [a["action"] for a in h.audits] == ["browser_agent.secret_set", "browser_agent.secret_set"]
    dumped = _dump(h)
    assert PASSWORD not in dumped and (PASSWORD + "2") not in dumped
    assert PASSWORD.encode().hex() not in dumped


def test_secret_rows_never_carry_a_value(h):
    assert secrets.secret_rows() == [{"name": "procore_password", "set": False, "set_at": None,
                                      "set_by_label": None, "fingerprint": None}]
    h.db.tables["profiles"] = [{"id": "u-it", "full_name": "Ina", "email": "ina@g3.test"}]
    secrets.set_secret("procore_password", PASSWORD, actor_id="u-it", actor_label="Ina")
    [row] = secrets.secret_rows()
    assert row["set"] and row["set_by_label"] == "Ina" and PASSWORD not in json.dumps(row)
    assert secrets.delete_secret("procore_password", actor_id="u-it", actor_label="Ina")
    assert secrets.secret_rows()[0]["set"] is False
    assert not secrets.delete_secret("procore_password", actor_id="u-it", actor_label="Ina")
    assert h.audits[-1]["action"] == "browser_agent.secret_cleared"


@pytest.mark.parametrize("name,value", [
    ("procore", PASSWORD), ("Procore_password", PASSWORD), ("procore_password", ""),
    ("procore_password", "x" * 201), ("procore_password", " padded "), ("procore_password", "a\nb"),
])
def test_bad_names_and_values_are_refused(h, name, value):
    with pytest.raises(secrets.SecretInvalid):
        secrets.set_secret(name, value, actor_id=None, actor_label="x")
    assert h.db.rows("browser_agent_secrets") == []


def test_no_key_means_no_store(h):
    plain = h.settings.model_copy(update={"browser_agent_secrets_key": ""})
    assert not secrets.keys_configured(plain)
    with pytest.raises(secrets.SecretsKeyMissing):
        secrets.encrypt_with_aad("x", b"a", plain)


# ── procore_creds ─────────────────────────────────────────────────────────────


def test_password_reads_the_database_fresh_with_the_env_fallback(h, monkeypatch):
    with_env = h.settings.model_copy(update={"procore_login_password": "env-pw"})
    assert procore_creds.password(with_env) == "env-pw"                 # no row: the env
    assert procore_creds.password(h.settings) == ""                      # neither
    secrets.set_secret("procore_password", PASSWORD, actor_id=None, actor_label="x")
    assert procore_creds.password(with_env) == PASSWORD                  # the row wins
    secrets.set_secret("procore_password", "Changed-1", actor_id=None, actor_label="x")
    assert procore_creds.password(with_env) == "Changed-1"               # read fresh every call
    assert procore_creds.password_version(with_env) == 2
    # An unreadable row (key rotated away) falls back to the env, never raises.
    other_key = with_env.model_copy(update={"browser_agent_secrets_key": KEY_C})
    assert procore_creds.password(other_key) == "env-pw"
    # Without the store key nothing is read at all.
    no_key = with_env.model_copy(update={"browser_agent_secrets_key": ""})
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: pytest.fail("the store was read"))
    assert procore_creds.password(no_key) == "env-pw"


def test_configured_needs_an_email_and_a_password_somewhere_and_caches(h, monkeypatch):
    assert not procore_creds.configured(h.settings)                      # email, no password
    assert procore_creds.configured(h.settings.model_copy(update={"procore_login_password": "pw"}))
    assert not procore_creds.configured(h.settings.model_copy(update={
        "procore_login_email": " ", "procore_login_password": "pw"}))
    secrets.set_secret("procore_password", PASSWORD, actor_id=None, actor_label="x")
    assert not procore_creds.configured(h.settings)                      # cached "absent" for 60 s
    procore_creds.reset_cache()
    assert procore_creds.configured(h.settings)
    loads = []
    real = secrets.load_row
    monkeypatch.setattr(secrets, "load_row", lambda name: loads.append(name) or real(name))
    for _ in range(5):
        assert procore_creds.configured(h.settings)
    assert loads == []                                                   # all from the cache


def test_config_builds_the_session_config_with_the_stored_password(h):
    secrets.set_secret("procore_password", PASSWORD, actor_id=None, actor_label="x")
    cfg = procore_creds.config(h.settings, "us02.procore.com")
    assert cfg.email == "agent@g3.test" and cfg.password == PASSWORD and cfg.configured
    assert PASSWORD not in repr(h.audits)


def test_the_turn_scrub_learns_the_stored_password(h):
    redaction.reset_for_tests()
    procore_creds.register_redaction_source()
    secrets.set_secret("procore_password", PASSWORD, actor_id=None, actor_label="x")
    found = redaction.load_secrets("job-1", worker_token=None)
    report = redaction.ScrubReport()
    out = redaction.scrub(f"typed {PASSWORD} here", found, report)
    assert PASSWORD not in out
    redaction.reset_for_tests()


def test_no_procore_configured_read_outside_config_and_procore_creds():
    """SPEC.md 5.5: every `settings.procore_configured` read in app/ goes
    through procore_creds.configured (the property knows the env only)."""
    root = Path(__file__).resolve().parents[1] / "app"
    allowed = {root / "core" / "config.py", root / "services" / "procore_creds.py"}
    strays = [
        f"{path.relative_to(root.parent)}:{n}"
        for path in root.rglob("*.py") if path not in allowed
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "procore_configured" in line
    ]
    assert strays == []
