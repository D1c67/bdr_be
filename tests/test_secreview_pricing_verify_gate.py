"""Security review, group pricing-verify-gate (finding 30).

POST /projects/{id}/pricing/verify must refuse (409, nothing written) until the
send_out lane head has reached Verify, so an Executive can never freeze a
committed snapshot of partial upstream figures. A commit at Verify still
advances, and a re-commit past Verify is still the silent re-stamp.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.roles import Role
from tests.test_reverify import FakeDB, _cat_rows, _patch_commit, _project, _verification

EXEC = SimpleNamespace(id="exec1", role=Role.EXECUTIVE)


def _db(send_out):
    return FakeDB({
        "verifications": [_verification(committed=False)],
        "projects": [_project("verify")],
        "project_category_state": _cat_rows(send_out=send_out),
    })


@pytest.mark.parametrize(
    "send_out",
    [
        ("gc_pricing", "active"),  # still pricing GCs: upstream figures partial
        ("gc_pricing", "locked"),  # lane not even opened yet
        ("verify", "locked"),  # head names verify but the lane is locked
    ],
)
def test_commit_before_verify_is_refused_and_writes_nothing(monkeypatch, send_out):
    db = _db(send_out)
    pricing, calls = _patch_commit(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        pricing.commit_verify("p1", None, EXEC)
    assert exc.value.status_code == 409
    # The snapshot row is untouched (no committed_at stamped) and nothing moved.
    assert db.tables["verifications"][0]["committed_at"] is None
    assert calls == []


def test_commit_at_verify_still_commits_and_advances(monkeypatch):
    db = _db(("verify", "active"))
    pricing, calls = _patch_commit(monkeypatch, db)
    row = pricing.commit_verify("p1", None, EXEC)
    assert row["committed_at"] is not None
    assert ("advance", "p1", "send_out") in calls


@pytest.mark.parametrize("head", ["send_out", "submitted", "bid_outcome"])
def test_recommit_past_verify_is_silent_restamp(monkeypatch, head):
    db = _db((head, "active"))
    pricing, calls = _patch_commit(monkeypatch, db)
    row = pricing.commit_verify("p1", None, EXEC)
    assert row["committed_at"] is not None
    assert calls == []
