"""The six assistant splitter routes on the projects router
(docs/ASSISTANT_PHASE2_PLAN.md 3.4 and 5.3, WI-3).

Invariants pinned here:

  principal  both write routes answer only an approval-bound assistant
             principal whose idempotency header equals the token's approval
             id: a human, a turn principal and a differing header get 404
             and no task; a missing or non-uuid header is 400
             idempotency_key_required; both recovery routes 404 a human;
  bodies     201 split_drop_out, a replay 200 with X-Idempotent-Replay and
             no second job; the by-key route answers the same body and 404s
             a task still `pending`;
  errors     SplitRefused travels as {code, message, **detail} with
             X-Error-Code;
  previews   the CSV is parsed and capped, humans may read both previews;
  firewall   resolve_path_format maps both preview paths (and the siblings)
             to their own templates on app.main.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import provenance, supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import projects as projects_router
from app.services import llm, llm_queue, rfp_file_drop, split_handoff, storage

from tests.assistant_fakes import APPROVAL_ID, PROJECT, RUN, STAGED, STAGED2, USER
from tests.test_split_handoff import NUMBER, _db

HEADER = projects_router.ASSISTANT_IDEMPOTENCY_HEADER
OTHER_APPROVAL = "99999999-9999-4999-8999-999999999990"
JOB_ID = "dddddddd-dddd-4ddd-8ddd-ddddddddddd9"


class _Actor:
    """The shim's part: enter the assistant provenance around a request."""

    def __init__(self, app):
        self.app = app
        self.actor: tuple | None = None

    async def __call__(self, scope, receive, send):
        if self.actor is not None and scope["type"] == "http":
            with provenance.acting_as_assistant(*self.actor):
                return await self.app(scope, receive, send)
        return await self.app(scope, receive, send)


@pytest.fixture
def api(monkeypatch):
    db = _db()
    from tests.assistant_fakes import FakeStorage

    store = FakeStorage().install(monkeypatch)
    for row in db.tables["assistant_files"]:
        store.objects[row["storage_path"]] = b"%PDF-x"
    monkeypatch.setattr(get_settings(), "bidding_enabled", True)
    from app.core import ratelimit

    ratelimit._buckets.clear()
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(projects_router, "get_supabase", lambda: db)
    monkeypatch.setattr(split_handoff, "_handoff_locked", lambda pid: False)
    monkeypatch.setattr(rfp_file_drop, "splitter_state", lambda s=None: (True, None))
    monkeypatch.setattr(split_handoff, "estimate_split_usd",
                        lambda pages, s=None: split_handoff.SplitEstimate(Decimal("0.68"), "claude-opus-5", "anthropic"))
    monkeypatch.setattr(split_handoff, "audit", lambda *a, **k: None)
    monkeypatch.setattr(llm, "active_model", lambda f, s=None: "claude-opus-5")
    monkeypatch.setattr(storage, "delete_bid_split_prefix", lambda job_id: None)
    enqueued: list[dict] = []
    monkeypatch.setattr(llm_queue, "enqueue", lambda jt, **kw: enqueued.append({"job_type": jt, **kw}) or {"id": "j"})
    app = FastAPI()
    app.include_router(projects_router.router)
    state = SimpleNamespace(user=CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True,
                                             via_assistant_run_id=RUN))
    app.dependency_overrides[get_current_user] = lambda: state.user
    actor = _Actor(app)
    actor.actor = (USER, RUN, APPROVAL_ID)
    return SimpleNamespace(client=TestClient(actor), db=db, actor=actor, state=state, enqueued=enqueued)


def _post(api, headers=None, body=None):
    return api.client.post(f"/projects/{PROJECT}/split-drop",
                           json=body or {"file_ids": [STAGED, STAGED2], "max_usd": "0.68"},
                           headers={HEADER: APPROVAL_ID} if headers is None else headers)


def _human(api):
    api.actor.actor = None
    api.state.user = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)


# ── split-drop ───────────────────────────────────────────────────────────────


def test_split_drop_201_then_a_replay(api):
    res = _post(api)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["project_id"] == PROJECT and len(body["files"]) == 2 and body["estimate_usd"] == "0.68"
    assert "X-Idempotent-Replay" not in res.headers
    again = _post(api)
    assert again.status_code == 200 and again.headers["X-Idempotent-Replay"] == "true"
    assert again.json() == body
    assert len(api.db.tables["bid_split_jobs"]) == 1 and len(api.enqueued) == 2
    by_key = api.client.get(f"/projects/{PROJECT}/split-drop/by-key/{APPROVAL_ID}")
    assert by_key.status_code == 200 and by_key.json() == body


def test_the_by_key_route_404s_a_pending_task_and_a_human(api):
    _post(api)
    api.db.tables["assistant_tasks"][0]["status"] = "pending"
    assert api.client.get(f"/projects/{PROJECT}/split-drop/by-key/{APPROVAL_ID}").status_code == 404
    api.db.tables["assistant_tasks"][0]["status"] = "done"
    assert api.client.get(f"/projects/{PROJECT}/split-drop/by-key/nope").status_code == 404
    _human(api)
    assert api.client.get(f"/projects/{PROJECT}/split-drop/by-key/{APPROVAL_ID}").status_code == 404


def test_a_human_and_a_turn_principal_get_404_and_no_task(api):
    _human(api)
    assert _post(api).status_code == 404
    api.actor.actor = (USER, RUN, None)          # a turn principal: no approval
    api.state.user = CurrentUser(id=USER, email="t", role=Role.EXECUTIVE, is_active=True, via_assistant_run_id=RUN)
    assert _post(api).status_code == 404
    assert api.db.tables["assistant_tasks"] == [] and api.db.tables["bid_split_jobs"] == []


def test_the_header_must_equal_the_tokens_approval(api):
    assert _post(api, headers={HEADER: OTHER_APPROVAL}).status_code == 404
    for headers in ({}, {HEADER: "not-a-uuid"}):
        res = _post(api, headers=headers)
        assert res.status_code == 400 and res.headers["X-Error-Code"] == "idempotency_key_required"
    assert api.db.tables["assistant_tasks"] == []


def test_refusals_carry_the_code_and_the_detail(api, monkeypatch):
    monkeypatch.setattr(split_handoff, "estimate_split_usd",
                        lambda pages, s=None: split_handoff.SplitEstimate(Decimal("0.90"), "m", "anthropic"))
    res = _post(api)
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "price_changed"
    assert res.json()["detail"] == {"code": "price_changed", "message": res.json()["detail"]["message"],
                                    "estimate_usd": "0.90", "max_usd": "0.68"}
    res = _post(api, body={"file_ids": [STAGED, STAGED], "max_usd": "0.68"})
    assert res.status_code == 400 and res.headers["X-Error-Code"] == "bad_file_ids"
    assert _post(api, body={"file_ids": [STAGED], "max_usd": "0.5e3"}).status_code == 422
    res = api.client.post("/projects/77777777-7777-4777-8777-000000000000/split-drop",
                          json={"file_ids": [STAGED], "max_usd": "1"}, headers={HEADER: APPROVAL_ID})
    assert res.status_code == 404 and res.headers["X-Error-Code"] == "project_not_found"
    assert api.client.post("/projects/nope/split-drop", json={"file_ids": [STAGED], "max_usd": "1"},
                           headers={HEADER: APPROVAL_ID}).status_code == 404


def test_the_queue_off_is_splitter_unavailable(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_queue_enabled", False)
    res = _post(api)
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "splitter_unavailable"
    assert res.json()["detail"]["reason"]


def test_the_accountant_is_refused(api):
    api.state.user = CurrentUser(id=USER, email="t", role=Role.ACCOUNTANT, is_active=True, via_assistant_run_id=RUN)
    assert _post(api).status_code == 403


# ── previews ─────────────────────────────────────────────────────────────────


def test_split_preview_parses_the_csv_and_answers_humans(api):
    _human(api)
    res = api.client.get(f"/projects/{PROJECT}/split-drop/preview", params={"file_ids": f"{STAGED}, {STAGED2},"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [f["file_id"] for f in body["files"]] == [STAGED, STAGED2]
    assert body["project"]["number"] == NUMBER and body["estimate"]["usd"] == "0.68"
    too_many = ",".join([STAGED] * 11)
    res = api.client.get(f"/projects/{PROJECT}/split-drop/preview", params={"file_ids": too_many})
    assert res.status_code == 400 and res.headers["X-Error-Code"] == "bad_file_ids"


def test_split_preview_has_its_own_budget_that_fits_a_cards_reads(api, monkeypatch):
    """Live test 2026-10-22: under ai_rate_limit the assistant got 2 reads a
    minute, so the execute job's after read answered 429 and a turn with two
    split reads followed by a prompt approve failed the card. The preview
    now has its own scope at four times the AI budget."""
    from app.core import ratelimit
    from app.core.error_codes import RateLimitScope

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_rate_limit_per_min", 5)
    route = next(r for r in projects_router.router.routes
                 if getattr(r, "path", "") == "/projects/{project_id}/split-drop/preview")
    calls = {d.call for d in route.dependant.dependencies}
    assert projects_router.split_preview_rate_limit in calls
    assert ratelimit.ai_rate_limit not in calls

    def read():
        return api.client.get(f"/projects/{PROJECT}/split-drop/preview", params={"file_ids": STAGED})

    # The assistant (half the human limit: 20 // 2): a refused read, the
    # proposal, the execute job's re-read and its after read, twice over.
    codes = [read().status_code for _ in range(10)]
    assert codes == [200] * 10
    over = read()
    assert over.status_code == 429 and over.headers["X-RateLimit-Scope"] == "split_preview"
    # It never spends the AI jobs budget (the Bid Splitter page's creates and retries).
    assert not any(key[0] == RateLimitScope.AI_JOBS for key in ratelimit._buckets)


def test_attach_routes(api, monkeypatch):
    api.db.tables["bid_split_jobs"] = [{"id": JOB_ID, "status": "done", "source": "manual", "project_id": None,
                                        "created_by": USER, "created_at": "2026-10-22T00:00:00+00:00"}]
    api.db.tables["bid_split_files"] = [{"id": "m1", "job_id": JOB_ID, "filename": "Set.pdf", "status": "done",
                                         "page_count": 3, "size_bytes": 10, "file_kind": "drawing_set",
                                         "created_at": "1", "finished_at": "2026-10-22T00:00:00+00:00"}]
    from app.services import rfp_split

    def resync(fid):
        # The re-file lands one whole row (the recovery read requires rows).
        rows = api.db.tables.setdefault("project_files", [])
        if not any(r.get("bid_split_file_id") == fid for r in rows):
            rows.append({"id": f"pf-{fid}", "project_id": PROJECT, "bid_split_file_id": fid,
                         "bid_split_segment_id": None, "is_source_set": False, "note": None,
                         "category": "drawing"})

    monkeypatch.setattr(rfp_split, "resync_after_run", resync)
    preview = api.client.get(f"/projects/{PROJECT}/attach-split-job/preview", params={"job_id": JOB_ID})
    assert preview.status_code == 200 and preview.json()["problem"] is None
    assert api.client.get(f"/projects/{PROJECT}/attach-split-job/{JOB_ID}").status_code == 404   # not linked yet
    res = api.client.post(f"/projects/{PROJECT}/attach-split-job", json={"job_id": JOB_ID},
                          headers={HEADER: APPROVAL_ID})
    assert res.status_code == 200, res.text
    assert res.json()["job_id"] == JOB_ID and "X-Idempotent-Replay" not in res.headers
    again = api.client.post(f"/projects/{PROJECT}/attach-split-job", json={"job_id": JOB_ID},
                            headers={HEADER: APPROVAL_ID})
    assert again.status_code == 200 and again.headers["X-Idempotent-Replay"] == "true"
    got = api.client.get(f"/projects/{PROJECT}/attach-split-job/{JOB_ID}")
    assert got.status_code == 200 and got.json()["job_id"] == JOB_ID
    assert api.client.post(f"/projects/{PROJECT}/attach-split-job", json={"job_id": "nope"},
                           headers={HEADER: APPROVAL_ID}).status_code == 404
    assert api.client.get(f"/projects/{PROJECT}/attach-split-job/preview",
                          params={"job_id": "nope"}).status_code == 404
    _human(api)
    assert api.client.post(f"/projects/{PROJECT}/attach-split-job", json={"job_id": JOB_ID},
                           headers={HEADER: APPROVAL_ID}).status_code == 404
    assert api.client.get(f"/projects/{PROJECT}/attach-split-job/{JOB_ID}").status_code == 404
    assert api.client.get(f"/projects/{PROJECT}/attach-split-job/preview",
                          params={"job_id": JOB_ID}).status_code == 200


def test_attach_refusals_travel_with_their_code(api, monkeypatch):
    api.db.tables["bid_split_jobs"] = [{"id": JOB_ID, "status": "processing", "source": "manual",
                                        "project_id": None, "created_by": USER}]
    res = api.client.post(f"/projects/{PROJECT}/attach-split-job", json={"job_id": JOB_ID},
                          headers={HEADER: APPROVAL_ID})
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "job_not_finished"
    assert res.json()["detail"]["code"] == "job_not_finished"


# ── route resolution (the firewall walks app.main's routes) ──────────────────


def test_resolve_path_format_maps_every_split_route_to_its_own_template():
    import app.main
    from app.assistant import firewall

    pid, other = PROJECT, "88888888-8888-4888-8888-888888888888"

    def resolve(method, path):
        return firewall.resolve_path_format(app.main.app, {"type": "http", "method": method, "path": path})

    assert resolve("GET", f"/projects/{pid}/split-drop/preview") == projects_router.SPLIT_DROP_PREVIEW_ROUTE
    assert resolve("GET", f"/projects/{pid}/attach-split-job/preview") == projects_router.ATTACH_SPLIT_JOB_PREVIEW_ROUTE
    assert resolve("GET", f"/projects/{pid}/attach-split-job/{other}") == projects_router.ATTACH_SPLIT_JOB_GET_ROUTE
    assert resolve("GET", f"/projects/{pid}/split-drop/by-key/{other}") == projects_router.SPLIT_DROP_BY_KEY_ROUTE
    assert resolve("POST", f"/projects/{pid}/split-drop") == projects_router.SPLIT_DROP_ROUTE
    assert resolve("POST", f"/projects/{pid}/attach-split-job") == projects_router.ATTACH_SPLIT_JOB_ROUTE


def test_the_route_literals_match_the_declared_paths():
    paths = {(m, r.path) for r in projects_router.router.routes for m in getattr(r, "methods", ())}
    for method, literal in (
        ("POST", projects_router.SPLIT_DROP_ROUTE), ("GET", projects_router.SPLIT_DROP_PREVIEW_ROUTE),
        ("GET", projects_router.SPLIT_DROP_BY_KEY_ROUTE), ("POST", projects_router.ATTACH_SPLIT_JOB_ROUTE),
        ("GET", projects_router.ATTACH_SPLIT_JOB_PREVIEW_ROUTE), ("GET", projects_router.ATTACH_SPLIT_JOB_GET_ROUTE),
    ):
        assert (method, literal) in paths, literal
    assert projects_router.ASSISTANT_IDEMPOTENCY_HEADER == "X-Assistant-Idempotency-Key"


def test_app_main_does_not_load_the_split_service():
    import subprocess
    import sys

    code = "import sys, app.main; print('app.services.split_handoff' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
