"""atomicfile, local_config and tokenstore: crash-safe files and what may live on the PC."""

import json
import os
import stat
import sys
import uuid

import pytest

from bdr_agent.atomicfile import atomic_write_bytes, atomic_write_json, bak_path, read_json_with_bak, remove_stray_temps
from bdr_agent.local_config import (
    LocalConfig,
    LocalConfigError,
    load_local_config,
    normalize_base_url,
    save_local_config,
)
from bdr_agent.tokenstore import TAG_DPAPI, TokenStore, TokenStoreError

TOKEN = "bdrw_" + "A" * 43
TOKEN2 = "bdrw_" + "Z" * 43


def test_atomic_write_keeps_bak_and_reader_falls_back(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    assert json.loads(bak_path(p).read_text()) == {"v": 1}
    p.write_bytes(b"{torn")            # power loss mid-write of a non-atomic writer
    assert read_json_with_bak(p) == {"v": 1}
    p.unlink()                          # crash between the two os.replace calls
    assert read_json_with_bak(p) == {"v": 1}


def test_stray_temp_files_are_removed(tmp_path):
    (tmp_path / ".worker.json.123.tmp").write_bytes(b"x")
    (tmp_path / "keep.json").write_bytes(b"{}")
    assert remove_stray_temps(tmp_path) == 1
    assert (tmp_path / "keep.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_atomic_write_mode(tmp_path):
    p = tmp_path / "secret.bin"
    atomic_write_bytes(p, b"x", mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_normalize_base_url():
    assert normalize_base_url("https://api.example.com/") == "https://api.example.com"
    assert normalize_base_url("https://api.example.com/agent-worker/v1") == "https://api.example.com"
    assert normalize_base_url("http://localhost:8000") == "http://localhost:8000"
    for bad in ("http://api.example.com", "ftp://x", "https://u:p@x.com", "https://x.com/?a=1", ""):
        with pytest.raises(LocalConfigError):
            normalize_base_url(bad)


def test_local_config_roundtrip_and_only_allowed_keys(paths):
    assert load_local_config(paths) is None
    wid = uuid.uuid4()
    save_local_config(paths, LocalConfig("https://api.example.com", wid))
    cfg = load_local_config(paths)
    assert cfg == LocalConfig("https://api.example.com", wid)
    assert cfg.api_host == "api.example.com"
    assert set(json.loads(paths.worker_json.read_text())) == {"api_base_url", "worker_id"}
    with pytest.raises(LocalConfigError):
        LocalConfig.from_json({"api_base_url": "https://x.com", "max_slots": 8})


def test_local_config_corrupt_primary_uses_bak(paths):
    save_local_config(paths, LocalConfig("https://one.example.com"))
    save_local_config(paths, LocalConfig("https://two.example.com"))
    paths.worker_json.write_text(json.dumps({"api_base_url": "http://evil.example.com"}))
    assert load_local_config(paths).api_base_url == "https://one.example.com"


def test_tokenstore_roundtrip_and_validation(paths):
    store = TokenStore(paths.token_file)
    assert store.load() is None
    store.save(TOKEN)
    assert store.load() == TOKEN
    store.save(TOKEN2)
    assert store.load() == TOKEN2
    paths.token_file.write_bytes(b"PLAIN1:garbage")
    assert store.load() == TOKEN      # .bak holds the previous good token
    with pytest.raises(TokenStoreError):
        store.save("not-a-token")
    store.clear()
    assert store.load() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_tokenstore_refuses_group_readable_file(paths):
    store = TokenStore(paths.token_file)
    store.save(TOKEN)
    assert stat.S_IMODE(paths.token_file.stat().st_mode) == 0o600
    os.chmod(paths.token_file, 0o644)
    with pytest.raises(TokenStoreError):
        store.load()


class XorProtector:
    """Stands in for DPAPI: same tag, reversible transform, never plaintext on disk."""
    tag = TAG_DPAPI

    def protect(self, data: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in data)

    def unprotect(self, blob: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in blob)


def test_tokenstore_protector_seam(paths):
    store = TokenStore(paths.token_file, protector=XorProtector())
    store.save(TOKEN)
    raw = paths.token_file.read_bytes()
    assert raw.startswith(TAG_DPAPI) and TOKEN.encode() not in raw
    assert store.load() == TOKEN
    with pytest.raises(TokenStoreError):
        TokenStore(paths.token_file).load()   # plain store cannot read a DPAPI file
