"""The staging routes and the splitter status route
(app/routers/assistant.py; docs/ASSISTANT_PHASE2_PLAN.md 3.1.2, 3.1.3,
3.10.3 and 5.3).

Invariants pinned here:

  roles     POST and DELETE /assistant/files need a writer (the Accountant
            gets 403); GET lists for any internal role;
  human     an assistant principal is refused by every staging handler and
            by GET /assistant/tasks/{id}, and the firewall denies the POST;
  loop      POST /assistant/files never runs the sync assistant gate on the
            event loop;
  codes     refusals carry their code in detail.code and X-Error-Code;
  tray      GET lists the caller's unlinked, unclaimed live rows and sweeps
            the caller's own expired objects first;
  delete    204, idempotent, 404 file_not_found, 409 file_consumed;
  status    GET /assistant/split/status hands the service's shape through,
            maps SplitRefused, 404s a bad project id, and admits the
            assistant principal (splitter_status reads it).
"""

from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.assistant import access, firewall, repo
from app.core import supabase_client
from app.core.deps import CurrentUser, get_current_user
from app.core.ratelimit import upload_rate_limit
from app.core.roles import Role
from app.routers import assistant as router_mod
from app.services import notifications, split_handoff

from tests.assistant_fakes import (
    CONV, MSG_USER, NOW, OTHER_USER, RUN, STAGED, STAGED2, TASK, USER, FakeDB, FakeStorage, conversation, profile,
    settings_row, staged_file, task,
)


def _pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[], assistant_conversations=[conversation()], assistant_messages=[],
        assistant_files=[], assistant_tasks=[], llm_call_log=[], audit_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(notifications, "audit", lambda *a, **k: None)
    store = FakeStorage().install(monkeypatch)
    who = {"role": Role.EXECUTIVE, "via": None}

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=who["role"], is_active=True, aal="aal2",
                           mfa_enrolled=True, via_assistant_run_id=who["via"])

    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[upload_rate_limit] = lambda: None
    return SimpleNamespace(client=TestClient(app), db=db, who=who, store=store)


def _upload(api, name="A-101.pdf", content=None):
    content = _pdf(2) if content is None else content
    return api.client.post("/assistant/files", files={"file": (name, content, "application/pdf")})


# ── POST /assistant/files ────────────────────────────────────────────────────


def test_upload_stages_then_reuses(api):
    res = _upload(api)
    assert res.status_code == 201, res.text
    body = res.json()
    assert set(body) == {"id", "filename", "size_bytes", "pages", "sha256", "expires_at", "reused"}
    assert body["filename"] == "A-101.pdf" and body["pages"] == 2 and body["reused"] is False
    assert len(body["sha256"]) == 64 and body["expires_at"]
    again = _upload(api, name="copy.pdf")
    assert again.status_code == 200 and again.json()["reused"] is True and again.json()["id"] == body["id"]
    assert len(api.store.objects) == 1 and len(api.db.tables["assistant_files"]) == 1


def test_upload_refusals_carry_their_codes(api, monkeypatch):
    res = _upload(api, name="notes.docx", content=b"PK\x03\x04")
    assert res.status_code == 415 and res.headers["X-Error-Code"] == "not_pdf"
    assert res.json()["detail"]["code"] == "not_pdf"
    res = _upload(api, content=b"%PDF-1.4 broken")
    assert res.status_code == 415 and res.json()["detail"]["code"] == "unreadable_pdf"
    res = _upload(api, content=_pdf(601))
    assert res.status_code == 413 and res.json()["detail"]["code"] == "too_many_pages"
    assert res.json()["detail"]["pages"] == 601
    res = _upload(api, name="‮")
    assert res.status_code == 400 and res.json()["detail"]["code"] == "bad_filename"
    api.db.tables["assistant_files"] = [staged_file(size_bytes=2147483600, storage_path="o/big")]
    res = _upload(api)
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "staging_full"
    assert list(api.store.objects) == []

    from app.routers import files as files_router

    async def too_big(upload, max_bytes):
        raise HTTPException(413, "File is too large (limit 450 MB).")

    monkeypatch.setattr(files_router, "_read_capped", too_big)
    res = _upload(api)
    assert res.status_code == 413 and res.headers["X-Error-Code"] == "file_too_large"


def test_upload_and_delete_need_a_writer(api):
    api.db.tables["assistant_files"] = [staged_file()]
    api.who["role"] = Role.ACCOUNTANT
    assert _upload(api).status_code == 403
    assert api.client.delete(f"/assistant/files/{STAGED}").status_code == 403
    assert api.client.get("/assistant/files").status_code == 200
    api.who["role"] = Role.ESTIMATOR
    assert api.client.get("/assistant/files").status_code == 403


def test_assistant_principal_is_refused_by_each_handler_and_the_firewall(api, monkeypatch):
    api.db.tables["assistant_files"] = [staged_file()]
    api.db.tables["assistant_tasks"] = [task()]
    monkeypatch.setattr(split_handoff, "task_progress_for", lambda t: {})
    api.who["via"] = RUN
    for res in (_upload(api), api.client.get("/assistant/files"),
                api.client.delete(f"/assistant/files/{STAGED}"), api.client.get(f"/assistant/tasks/{TASK}")):
        assert res.status_code == 403, res.text
        assert res.headers["X-Error-Code"] == "assistant_cannot_approve"
    assert api.db.tables["assistant_files"][0]["expires_at"] > NOW.isoformat()
    verdict = firewall.check("POST", "/assistant/files", {("POST", "/assistant/files")})
    assert not verdict.allowed and verdict.reason == "deny_listed"
    assert not firewall.check("DELETE", "/assistant/files/{file_id}", {("DELETE", "/assistant/files/{file_id}")}).allowed
    assert ("POST", "/assistant/files") in firewall.DENY_LIST


def test_upload_never_runs_the_assistant_gate_on_the_event_loop(api, monkeypatch):
    seen: list[bool] = []
    real = repo.settings_row

    def off_loop(user_id):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append(True)
        else:
            raise AssertionError("repo.settings_row ran on the event loop")
        return real(user_id)

    monkeypatch.setattr(repo, "settings_row", off_loop)
    res = _upload(api)
    assert res.status_code == 201, res.text
    assert seen == [True]


def test_switched_off_assistant_is_a_bare_404(api):
    api.db.tables["assistant_settings"][0]["enabled"] = False
    assert _upload(api).status_code == 404
    assert api.client.get("/assistant/files").status_code == 404
    assert api.client.delete(f"/assistant/files/{STAGED}").status_code == 404
    assert api.db.tables["assistant_files"] == [] and api.store.objects == {}


# ── GET and DELETE /assistant/files ──────────────────────────────────────────


def test_tray_lists_unlinked_live_rows_and_sweeps_own_rows(api):
    past = (NOW - timedelta(hours=1)).isoformat()
    api.db.tables["assistant_files"] = [
        staged_file(created_at=(NOW - timedelta(minutes=5)).isoformat()),
        staged_file(id=STAGED2, storage_path="p2", created_at=NOW.isoformat()),
        staged_file(id="linked", storage_path="p3", message_id=MSG_USER, conversation_id=CONV),
        staged_file(id="claimed", storage_path="p4", consumed_task_id=TASK),
        staged_file(id="old", storage_path="p5", expires_at=past),
        staged_file(id="theirs", storage_path="p6", user_id=OTHER_USER, expires_at=past),
    ]
    api.store.objects.update({"p5": b"x", "p6": b"x"})
    res = api.client.get("/assistant/files")
    assert res.status_code == 200
    assert [r["id"] for r in res.json()] == [STAGED2, STAGED]
    assert all(r["reused"] is False for r in res.json())
    # The caller's own expired object went; another user's did not.
    assert "p5" not in api.store.objects and "p6" in api.store.objects


def test_delete_retires_and_is_idempotent(api):
    api.db.tables["assistant_files"] = [staged_file(storage_path="o/x"),
                                        staged_file(id=STAGED2, storage_path="o/y", consumed_task_id=TASK)]
    api.store.objects.update({"o/x": b"%PDF-", "o/y": b"%PDF-"})
    res = api.client.delete(f"/assistant/files/{STAGED}")
    assert res.status_code == 204 and res.content == b""
    assert "o/x" not in api.store.objects and api.db.tables["assistant_files"][0]["purged_at"]
    assert api.client.delete(f"/assistant/files/{STAGED}").status_code == 204
    res = api.client.delete(f"/assistant/files/{STAGED2}")
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "file_consumed"
    res = api.client.delete("/assistant/files/not-a-uuid")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "file_not_found"
    res = api.client.delete("/assistant/files/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa7")
    assert res.status_code == 404
    assert len(api.db.tables["assistant_files"]) == 2


# ── GET /assistant/split/status ──────────────────────────────────────────────


def _status_body(**over):
    base = {"splitter": {"available": True, "reason": None, "max_pages_per_file": 600},
            "limits": {"max_files_per_card": 10, "max_pages_per_card": 400},
            "staged": [{"file_id": STAGED, "staged_filename": "A-101.pdf", "pages": 3, "size_mb": 0.1}],
            "jobs": [], "tasks": [], "project": None,
            "spend": {"user_remaining_usd": "4.00", "global_remaining_usd": "49.00"}}
    base.update(over)
    return base


def test_split_status_hands_the_service_shape_through(api, monkeypatch):
    seen = []

    def status_for(user, project_id):
        seen.append((user.id, project_id))
        return _status_body(project={"number": "26.9.7130", "handoff_locked": False})

    monkeypatch.setattr(split_handoff, "status_for", status_for)
    pid = "77777777-7777-4777-8777-777777777777"
    res = api.client.get(f"/assistant/split/status?project_id={pid}")
    assert res.status_code == 200, res.text
    assert res.json() == _status_body(project={"number": "26.9.7130", "handoff_locked": False})
    assert seen == [(USER, pid)]
    assert api.client.get("/assistant/split/status").status_code == 200 and seen[-1] == (USER, None)
    res = api.client.get("/assistant/split/status?project_id=nope")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "project_not_found"
    # The assistant reads it through splitter_status; the Accountant may too.
    api.who["via"] = RUN
    assert api.client.get("/assistant/split/status").status_code == 200
    api.who["via"], api.who["role"] = None, Role.ACCOUNTANT
    assert api.client.get("/assistant/split/status").status_code == 200


def test_split_status_maps_refusals(api, monkeypatch):
    def refuse(user, project_id):
        raise split_handoff.SplitRefused("project_not_found", "Project not found.", status=404)

    monkeypatch.setattr(split_handoff, "status_for", refuse)
    res = api.client.get("/assistant/split/status?project_id=77777777-7777-4777-8777-777777777777")
    assert res.status_code == 404 and res.headers["X-Error-Code"] == "project_not_found"
    assert res.json()["detail"] == {"code": "project_not_found", "message": "Project not found."}
