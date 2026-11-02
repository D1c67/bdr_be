"""Encrypted browser records bind all private dimensions and audit before decrypt."""
import base64
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import assistant_browser_records as records
from app.services import assistant_browser_vault as vault


@pytest.fixture
def context(monkeypatch):
    owner, job, record = [str(uuid4()) for _ in range(3)]
    monkeypatch.setattr(vault, "get_settings", lambda: SimpleNamespace(
        user_cred_kek=base64.b64encode(b"k" * 32).decode(), user_cred_kek_id="u1", user_cred_old_keys=""))
    monkeypatch.setattr(records.repo, "browser_job", lambda user_id, job_id: {
        "id": job, "principal_kind": "user", "principal_user_id": owner,
    } if (user_id, job_id) == (owner, job) else None)
    packed = records.pack_record(owner, job, "step", {"text": "PRIVATE_STEP"}, record_id=record, seq=2)
    row = {**packed, "user_id": owner, "job_id": job, "created_at": "2026-10-27T12:00:00+00:00"}
    return SimpleNamespace(owner=owner, job=job, record=record, row=row)


def test_only_ciphertext_is_persistable_and_round_trip(context):
    assert "PRIVATE_STEP" not in str(context.row)
    assert records.unpack_record(context.owner, context.job, context.row) == {"text": "PRIVATE_STEP"}


@pytest.mark.parametrize("field,value", [("job_id", "uuid"), ("user_id", "uuid"), ("id", "uuid"),
    ("kind", "turn"), ("seq", 3), ("help_id", "uuid"), ("client_msg_id", "uuid")])
def test_transplanted_metadata_cannot_decrypt(context, field, value):
    row = deepcopy(context.row)
    row[field] = str(uuid4()) if value == "uuid" else value
    with pytest.raises(records.BrowserContentUnavailable):
        records.unpack_record(context.owner, context.job, row)


def test_secrets_cannot_be_opened_as_normal_trace(context):
    row = records.pack_record(context.owner, context.job, "help_secret", {"secret": "123456"},
                              record_id=str(uuid4()), seq=0)
    row.update(user_id=context.owner, job_id=context.job)
    with pytest.raises(records.BrowserContentUnavailable):
        records.unpack_record(context.owner, context.job, row)
    assert records.unpack_record(context.owner, context.job, row, allow_secret=True)["secret"] == "123456"


def test_owner_mismatch_precedes_record_query(context, monkeypatch):
    monkeypatch.setattr(records.repo, "browser_records", lambda *a, **k: pytest.fail("must not query"))
    with pytest.raises(records.BrowserContentUnavailable):
        records.owner_trace(str(uuid4()), context.job)


def test_admin_audit_failure_never_decrypts(context, monkeypatch):
    monkeypatch.setattr(records.repo, "browser_inspect", lambda *a, **k: {"ok": False, "records": [context.row]})
    monkeypatch.setattr(records, "unpack_record", lambda *a, **k: pytest.fail("must not decrypt"))
    with pytest.raises(records.BrowserContentUnavailable):
        records.inspect(str(uuid4()), context.owner, context.job, "Investigate reported failure")


def test_admin_read_excludes_secret_and_requires_audit_id(context, monkeypatch):
    secret = {**context.row, "kind": "help_secret"}
    monkeypatch.setattr(records.repo, "browser_inspect", lambda *a, **k: {
        "ok": True, "audit_id": "audit", "records": [context.row, secret],
    })
    result = records.inspect(str(uuid4()), context.owner, context.job, "Investigate reported failure")
    assert result["items"] == [{"id": context.record, "kind": "step", "seq": 2,
                                "created_at": context.row["created_at"], "text": "PRIVATE_STEP"}]


@pytest.mark.parametrize("value", ["garbage", "a" * 501, base64.b64encode(b'["x", "id"]').decode(),
    base64.b64encode(b'["2026-10-27T12:00:00", "id"]').decode()])
def test_bad_cursor_fails_closed(value):
    with pytest.raises(records.BrowserContentUnavailable):
        records.parse_cursor(value)


def test_cursor_preserves_tied_timestamp_id(context):
    assert records.parse_cursor(records._cursor(context.row)) == (context.row["created_at"], context.record)


@pytest.mark.parametrize("mime,data,ok", [("image/png", b"\x89PNG\r\n\x1a\nimage", True),
    ("image/jpeg", b"\xff\xd8\xffimage", True), ("image/svg+xml", b"<svg/>", False),
    ("image/png", b"<html>secret</html>", False), ("image/png", b"", False)])
def test_images_are_bounded_and_sniffed(mime, data, ok):
    payload = {"mime_type": mime, "data": base64.b64encode(data).decode()}
    if ok:
        assert records._image_bytes(payload) == (data, mime)
    else:
        with pytest.raises(records.BrowserContentUnavailable):
            records._image_bytes(payload)


def test_record_replay_reuses_envelope_but_rechecks_lease(context, monkeypatch):
    from app.services import assistant_browser_runtime as runtime
    monkeypatch.setattr(runtime, "sanitize", lambda job, payload: payload)
    monkeypatch.setattr(records.repo, "browser_record", lambda *a: context.row)
    calls = []
    def put(*args):
        calls.append(args)
        assert args[-1]["envelope"] == context.row["envelope"]
        return {"ok": True, "record": context.row}
    monkeypatch.setattr(records.repo, "browser_record_put", put)
    records.append_record({"id": context.job, "principal_kind": "user", "principal_user_id": context.owner},
        "step", {"text": "PRIVATE_STEP"}, record_id=context.record, seq=2,
        worker_id=str(uuid4()), boot_id=str(uuid4()), lease_epoch=7)
    assert len(calls) == 1 and calls[0][4] == 7


def test_purge_delegates_to_atomic_legal_hold_rpc(monkeypatch):
    calls = []
    monkeypatch.setattr(records.repo, "browser_records_purge", lambda owner, **kw: calls.append((owner, kw)) or {"records": 2})
    assert records.purge_expired_records(limit=10000) == {"records": 2}
    assert calls == [(None, {"limit": 500})]
