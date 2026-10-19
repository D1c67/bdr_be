"""Shared wiring for the browser agent worker-API tests.

`Harness` installs the in-memory FakeDB (tests/browser_agents_fake.py) behind
every module the worker API touches, a Settings with agents switched on,
a frozen clock, and recorders for notifications and audit rows. Helpers mint
real worker tokens (core/agent_auth) and drive the routes through FastAPI's
TestClient, so auth, fencing and error mapping run exactly as in production.
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any

from fastapi.testclient import TestClient

import app.main
from app.core import agent_auth, features, ratelimit, supabase_client
from app.core.config import Settings
from app.routers import agent_worker
from app.services import notifications
from app.services.browser_agents import artifacts, protocol, queue

from tests.browser_agents_fake import FakeDB, iso

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()
API = protocol.API_PREFIX


def agent_settings(**over: Any) -> Settings:
    base = dict(
        browser_agents_enabled=True,
        supabase_url="https://example.supabase.co",
        browser_agent_secrets_key=KEY_A,
        browser_agent_fingerprint_key=KEY_B,
        procore_login_email="agent@g3.test",
        pm_enabled=True,
        pm_rfi_sync_enabled=True,
        rate_limit_enabled=True,
        environment="test",
    )
    return Settings(**{**base, **over})


class Harness:
    def __init__(self, monkeypatch, db: FakeDB | None = None, **settings_over: Any):
        self.db = db or FakeDB()
        self.settings = agent_settings(**settings_over)
        self.notices: list[dict[str, Any]] = []
        self.audits: list[dict[str, Any]] = []
        mp = monkeypatch
        mp.setattr(supabase_client, "get_supabase", lambda: self.db)
        for mod in (features, agent_auth, queue, agent_worker, ratelimit):
            mp.setattr(mod, "get_settings", lambda: self.settings)
        mp.setattr(queue, "now_utc", lambda: self.db.now)
        mp.setattr(agent_worker, "_now", lambda: self.db.now)
        mp.setattr(queue, "_started_monotonic", time.monotonic() - 100_000)

        def _notify(role, project_id, type_, message, rfq_id=None, mirror_email=True, metadata=None):
            self.notices.append({"role": role, "project_id": project_id, "type": type_, "message": message,
                                 "email": mirror_email, "metadata": metadata})

        def _audit(actor_id, action, entity=None, entity_id=None, payload=None):
            self.audits.append({"actor_id": actor_id, "action": action, "entity": entity,
                                "entity_id": entity_id, "payload": payload})

        mp.setattr(notifications, "notify_role", _notify)
        mp.setattr(notifications, "audit", _audit)
        ratelimit._buckets.clear()
        self.client = TestClient(app.main.app, raise_server_exceptions=True)

    # ── fixtures ──
    def worker(self, *, status: str = "active", name: str | None = None, allowed_types=("canary", "rfi_from_email"),
               max_slots: int = 3, allowed_ips=(), types=None) -> tuple[dict, str]:
        token, digest, prefix = agent_auth.new_worker_token()
        row = self.db.table("browser_agent_workers").insert({
            "name": name or f"PC {uuid.uuid4().hex[:6]}",
            "status": status,
            "token_hash": agent_auth.bytea_literal(digest),
            "token_prefix": prefix,
            "max_slots": max_slots,
            "allowed_types": list(allowed_types),
            "allowed_ips": list(allowed_ips),
            "capabilities": {"types": types or {k: "1.0.0" for k in allowed_types}, "slots": max_slots},
        }).execute().data[0]
        return self.db.one("browser_agent_workers", id=row["id"]), token

    def enable_type(self, key: str, **cols: Any) -> None:
        t = self.db.one("browser_agent_types", key=key)
        t.update({"enabled": True, "provider": "openai", "model": "gpt-test", "price_in_per_mtok": 1,
                  "price_out_per_mtok": 2, **cols})

    PROJECT_ID = "0b0b0b0b-0000-4000-8000-000000000123"

    def link_project(self, project_id: str | None = None, **over: Any) -> dict:
        """A PM project linked to Procore project 123 on app.procore.com (the
        default RFI seed's), so rfi_from_email's claim-time prepare runs it."""
        pid = project_id or self.PROJECT_ID
        if not self.db.rows("projects", id=pid):
            self.db.tables.setdefault("projects", []).append({"id": pid, "name": "Linked", "pm_stage": "construction"})
        links = self.db.rows("pm_external_links", project_id=pid)
        if links:
            links[0].update(over)
            return links[0]
        row = {"id": f"link-{pid}", "project_id": pid, "platform": "procore", "host": "app.procore.com",
               "external_project_id": "123", "external_company_id": "77", "sync_enabled": True,
               "linked_via": "manual", "backfilled_at": iso(self.db.now), **over}
        self.db.tables.setdefault("pm_external_links", []).append(row)
        return row

    def job(self, agent_type: str = "canary", **cols: Any) -> dict:
        seed = cols.pop("input", None) or (
            {"expect_text": "BDR canary ok"} if agent_type == "canary" else {
                "platform": "procore", "host": "app.procore.com", "external_project_id": "123",
                "rfi_id": "456", "email_message_at": iso(self.db.now)})
        if agent_type == "rfi_from_email" and "project_id" not in cols:
            cols["project_id"] = self.link_project()["project_id"]
        result = queue.enqueue(agent_type, cols.pop("dedupe_key", f"test:{uuid.uuid4()}"), seed,
                               **{k: cols.pop(k) for k in list(cols) if k in (
                                   "priority", "project_id", "source_email_id", "platform", "created_by",
                                   "ttl_hours", "next_attempt_at")})
        row = self.db.one("browser_agent_jobs", id=result.job["id"])
        row.update(cols)
        return row

    # ── calls ──
    @staticmethod
    def auth(token: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", **extra}

    def hello(self, token: str, boot: str | None = None, **over: Any):
        boot = boot or str(uuid.uuid4())
        body = {"boot_id": boot, "version": "1.0.0",
                "capabilities": {"types": {"canary": "1.0.0", "rfi_from_email": "1.0.0"}, "slots": 3}, **over}
        return self.client.post(f"{API}/hello", json=body, headers=self.auth(token)), boot

    def claim(self, token: str, boot: str, capacity: dict[str, int] | None = None):
        return self.client.post(f"{API}/claim", json={"boot_id": boot, "capacity": capacity or {"canary": 1}},
                                headers=self.auth(token, **{"X-Agent-Boot": boot}))

    def lease_headers(self, token: str, job_id: str, epoch: int, boot: str) -> dict[str, str]:
        return self.auth(token, **{"X-Agent-Lease": f"{job_id}:{epoch}", "X-Agent-Boot": boot,
                                   "Idempotency-Key": str(uuid.uuid4())})

    def active_with_job(self, agent_type: str = "canary") -> tuple[dict, str, str, dict]:
        """An active worker after /hello, holding one claimed job."""
        if agent_type != "canary":
            self.enable_type(agent_type)
        worker, token = self.worker()
        _, boot = self.hello(token)
        job = self.job(agent_type)
        resp = self.claim(token, boot, {agent_type: 1})
        assert resp.status_code == 200, resp.text
        assert [j["job_id"] for j in resp.json()["jobs"]] == [job["id"]]
        return worker, token, boot, self.db.one("browser_agent_jobs", id=job["id"])


def error_code(resp) -> str | None:
    return resp.headers.get(protocol.HEADER_ERROR)


__all__ = ["Harness", "agent_settings", "error_code", "API", "KEY_A", "KEY_B", "artifacts"]
