"""Approval-bound bdra_ tokens (app/assistant/principal.py, phase 1;
docs/ASSISTANT_PHASE1_PLAN.md 3.8 "Principal").

  executing  a token minted for an approval resolves only while that
             approval row (the token owner's) is executing;
  refused    a pending, approved, executed or missing row, or another
             user's row, answers 403 assistant_approval_not_executing
             before the profile or the delegation is read;
  unbound    a token without an approval never reads assistant_approvals.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.assistant import principal
from app.core import provenance, supabase_client

from tests.assistant_fakes import APPROVAL_ID, RUN, SECRET, USER, FakeDB, approval, delegation, profile

OTHER = "abababab-abab-4bab-8bab-abababababab"


@pytest.fixture
def settings(monkeypatch):
    s = SimpleNamespace(assistant_token_secret=SECRET, assistant_enabled=True)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    return s


def _db(monkeypatch, *rows) -> FakeDB:
    fake = FakeDB(assistant_delegations=[delegation()], assistant_approvals=list(rows))
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def _resolve(token):
    scope = {"type": "http", "method": "PUT", "path": "/projects/x/labor", "headers": [],
             principal.SCOPE_MARKER: True}
    row = profile()
    return principal.resolve_bdra_principal(scope, token, lambda uid: row if uid == row["id"] else None)


def test_executing_approval_resolves_the_owner(settings, monkeypatch):
    db = _db(monkeypatch, approval(status="executing"))
    token = principal.mint_token(USER, RUN, APPROVAL_ID)
    with provenance.acting_as_assistant(USER, RUN, APPROVAL_ID):
        user = _resolve(token)
    assert user.id == USER and getattr(user, "via_assistant_run_id", None) == RUN
    assert [t for _op, t in db.calls] == ["assistant_approvals", "assistant_delegations"]


@pytest.mark.parametrize("status", ["pending", "approved", "executed", "failed", "expired", "rejected"])
def test_approval_not_executing_is_refused(settings, monkeypatch, status):
    extra = {"expired_reason": "timed_out"} if status == "expired" else {}
    db = _db(monkeypatch, approval(status=status, **extra))
    token = principal.mint_token(USER, RUN, APPROVAL_ID)
    with provenance.acting_as_assistant(USER, RUN, APPROVAL_ID):
        with pytest.raises(HTTPException) as err:
            _resolve(token)
    assert err.value.status_code == 403 and err.value.detail == "assistant_approval_not_executing"
    # Refused before the delegation is read.
    assert [t for _op, t in db.calls] == ["assistant_approvals"]


def test_missing_or_foreign_approval_is_refused(settings, monkeypatch):
    _db(monkeypatch, approval(status="executing", user_id=OTHER))
    token = principal.mint_token(USER, RUN, APPROVAL_ID)
    with provenance.acting_as_assistant(USER, RUN, APPROVAL_ID):
        with pytest.raises(HTTPException) as err:
            _resolve(token)
    assert err.value.detail == "assistant_approval_not_executing"
    _db(monkeypatch)
    with provenance.acting_as_assistant(USER, RUN, APPROVAL_ID):
        with pytest.raises(HTTPException) as err:
            _resolve(token)
    assert err.value.status_code == 403


def test_unbound_token_never_reads_approvals(settings, monkeypatch):
    db = _db(monkeypatch, approval(status="pending"))
    token = principal.mint_token(USER, RUN)
    with provenance.acting_as_assistant(USER, RUN):
        user = _resolve(token)
    assert user.id == USER
    assert [t for _op, t in db.calls] == ["assistant_delegations"]
