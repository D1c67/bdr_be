"""The approval-bound route helper (app/core/approval_bound.py;
docs/ASSISTANT_PHASE4_PLAN.md 3.2.3): only the assistant principal that
executes an approval passes, and the idempotency header must repeat that
approval id. A human, a chat turn principal and a header mismatch get a 404
byte-identical to an unknown route; a missing or malformed header gets 400
idempotency_key_required. The refusals match routers/projects.py
_approval_bound_key, which keeps its own copy."""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core import provenance
from app.core.approval_bound import (
    ASSISTANT_IDEMPOTENCY_HEADER,
    IDEMPOTENCY_KEY_REQUIRED,
    approval_bound_key,
    require_approval_bound,
)
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import projects

USER_ID = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_ID = "aaaaaaaa-0000-4000-8000-000000000002"
RUN_ID = "bbbbbbbb-0000-4000-8000-000000000001"
APPROVAL_ID = "cccccccc-0000-4000-8000-000000000001"


def _user(*, via_run: str | None = RUN_ID, user_id: str = USER_ID) -> CurrentUser:
    return CurrentUser(id=user_id, email="exec@g3test.dev", role=Role.EXECUTIVE, is_active=True,
                       via_assistant_run_id=via_run)


def _request(header: str | None) -> Request:
    headers = []
    if header is not None:
        headers.append((ASSISTANT_IDEMPOTENCY_HEADER.lower().encode(), header.encode()))
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": headers, "query_string": b""})


def _status(fn, request, user) -> tuple[int, object]:
    try:
        return 200, fn(request, user)
    except HTTPException as exc:
        return exc.status_code, exc.detail


def test_header_name_matches_the_split_drop_route():
    assert ASSISTANT_IDEMPOTENCY_HEADER == projects.ASSISTANT_IDEMPOTENCY_HEADER
    assert IDEMPOTENCY_KEY_REQUIRED == projects.IDEMPOTENCY_KEY_REQUIRED


def test_the_executing_principal_passes_and_the_key_is_normalized():
    with provenance.acting_as_assistant(USER_ID, RUN_ID, approval_id=APPROVAL_ID):
        assert approval_bound_key(_request(APPROVAL_ID), _user()) == APPROVAL_ID
        assert approval_bound_key(_request("  " + APPROVAL_ID.upper() + " "), _user()) == APPROVAL_ID
        assert require_approval_bound(_request(APPROVAL_ID), _user()) == APPROVAL_ID


def test_a_human_gets_404():
    code, detail = _status(approval_bound_key, _request(APPROVAL_ID), _user(via_run=None))
    assert (code, detail) == (404, "Not Found")
    # Even inside an assistant context, a request without the principal is a human's.
    with provenance.acting_as_assistant(USER_ID, RUN_ID, approval_id=APPROVAL_ID):
        assert _status(approval_bound_key, _request(APPROVAL_ID), _user(via_run=None)) == (404, "Not Found")


def test_a_turn_principal_gets_404():
    with provenance.acting_as_assistant(USER_ID, RUN_ID):
        assert _status(approval_bound_key, _request(APPROVAL_ID), _user()) == (404, "Not Found")


def test_a_principal_without_a_provenance_actor_gets_404():
    assert _status(approval_bound_key, _request(APPROVAL_ID), _user()) == (404, "Not Found")


def test_an_actor_of_another_user_gets_404():
    with provenance.acting_as_assistant(OTHER_ID, RUN_ID, approval_id=APPROVAL_ID):
        assert _status(approval_bound_key, _request(APPROVAL_ID), _user()) == (404, "Not Found")


def test_a_header_mismatch_gets_404():
    with provenance.acting_as_assistant(USER_ID, RUN_ID, approval_id=APPROVAL_ID):
        assert _status(approval_bound_key, _request(str(uuid.uuid4())), _user()) == (404, "Not Found")


@pytest.mark.parametrize("header", [None, "", "   ", "not-a-uuid", APPROVAL_ID[:-1]])
def test_a_missing_or_malformed_header_gets_400(header):
    with provenance.acting_as_assistant(USER_ID, RUN_ID, approval_id=APPROVAL_ID):
        code, detail = _status(approval_bound_key, _request(header), _user())
    assert code == 400
    assert detail["code"] == IDEMPOTENCY_KEY_REQUIRED
    assert ASSISTANT_IDEMPOTENCY_HEADER in detail["message"]


def test_the_refusals_match_the_projects_copy():
    """Same status and detail as routers/projects.py _approval_bound_key for
    every refusal and the pass (that module keeps its own copy)."""
    cases = [
        (None, _user(via_run=None), APPROVAL_ID),
        (None, _user(), APPROVAL_ID),
        ({"approval_id": None}, _user(), APPROVAL_ID),
        ({"approval_id": APPROVAL_ID}, _user(), str(uuid.uuid4())),
        ({"approval_id": APPROVAL_ID}, _user(), None),
        ({"approval_id": APPROVAL_ID}, _user(), "nope"),
        ({"approval_id": APPROVAL_ID}, _user(), APPROVAL_ID),
    ]
    for actor, user, header in cases:
        def both():
            ours = _status(approval_bound_key, _request(header), user)
            theirs = _status(projects._approval_bound_key, _request(header), user)
            return ours, theirs
        if actor is None:
            ours, theirs = both()
        else:
            with provenance.acting_as_assistant(USER_ID, RUN_ID, approval_id=actor["approval_id"]):
                ours, theirs = both()
        assert ours == theirs, (actor, header)


def test_the_dependency_refuses_a_human_through_fastapi():
    app = FastAPI()

    @app.post("/bound")
    def bound(key: str = Depends(require_approval_bound)) -> dict:
        return {"key": key}

    app.dependency_overrides[get_current_user] = lambda: _user(via_run=None)
    client = TestClient(app)
    res = client.post("/bound", headers={ASSISTANT_IDEMPOTENCY_HEADER: APPROVAL_ID})
    assert res.status_code == 404
    assert res.json() == {"detail": "Not Found"}
