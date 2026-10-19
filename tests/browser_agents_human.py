"""Shared wiring for the browser agent help and human API tests.

`HumanHarness` extends the worker Harness (tests/browser_agents_harness.py)
with: profiles for each role, a notify_role recorder that returns the
created count (so the IT Admin fallback is testable), a dismissal recorder,
the settings patched into every module the help and admin code reads, and
`as_user(role)` to drive /browser-agents with a user JWT stand-in.
"""

from __future__ import annotations

import uuid
from typing import Any

import app.main
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import browser_agents as browser_agents_router
from app.services import notifications, procore_creds
from app.services.browser_agents import admin, help as help_flow, redaction, secrets
from tests.browser_agents_harness import API, Harness

PROFILES = {
    "ea": ("00000000-0000-4000-8000-0000000000e1", Role.ESTIMATING_ADMIN, "Erin Admin"),
    "it": ("00000000-0000-4000-8000-0000000000e2", Role.IT_ADMIN, "Ian Tech"),
    "exec": ("00000000-0000-4000-8000-0000000000e3", Role.EXECUTIVE, "Eve Exec"),
    "acct": ("00000000-0000-4000-8000-0000000000e4", Role.ACCOUNTANT, "Al Count"),
    "dev": ("00000000-0000-4000-8000-0000000000e5", Role.ACCOUNTANT, "Dee Dev"),
}


class HumanHarness(Harness):
    def __init__(self, monkeypatch, *, with_ea: bool = True, **settings_over: Any):
        settings_over.setdefault("procore_login_password", "Procore-Pass-9!")
        super().__init__(monkeypatch, **settings_over)
        mp = monkeypatch
        for mod in (help_flow, admin, secrets, redaction, procore_creds, browser_agents_router):
            mp.setattr(mod, "get_settings", lambda: self.settings)
        procore_creds.reset_cache()
        redaction.reset_for_tests()
        self.dismissals: list[dict[str, Any]] = []
        self.db.tables["profiles"] = [
            {"id": pid, "role": role.value, "full_name": name, "email": f"{key}@g3.test", "is_active": True}
            for key, (pid, role, name) in PROFILES.items() if with_ea or key != "ea"
        ]

        def _notify(role, project_id, type_, message, rfq_id=None, mirror_email=True, metadata=None):
            count = len([p for p in self.db.rows("profiles", role=role.value) if p.get("is_active")])
            self.notices.append({"role": role, "project_id": project_id, "type": type_, "message": message,
                                 "email": mirror_email, "metadata": metadata, "count": count})
            return count

        def _dismiss(**kw):
            self.dismissals.append(kw)

        mp.setattr(notifications, "notify_role", _notify)
        mp.setattr(notifications, "dismiss_notifications", _dismiss)
        self.who = "ea"

        def _user():
            pid, role, _name = PROFILES[self.who]
            return CurrentUser(id=pid, email=f"{self.who}@g3.test", role=role, is_active=True,
                               is_dev=self.who == "dev")

        app.main.app.dependency_overrides[get_current_user] = _user

    def close(self) -> None:
        app.main.app.dependency_overrides.pop(get_current_user, None)

    def user(self, key: str = "ea") -> CurrentUser:
        pid, role, _ = PROFILES[key]
        return CurrentUser(id=pid, email=f"{key}@g3.test", role=role, is_active=True, is_dev=key == "dev")

    # ── human calls ──
    def get(self, path: str, who: str = "ea", **kw):
        self.who = who
        return self.client.get(f"/browser-agents{path}", **kw)

    def post(self, path: str, who: str = "ea", **kw):
        self.who = who
        return self.client.post(f"/browser-agents{path}", **kw)

    def patch(self, path: str, who: str = "it", **kw):
        self.who = who
        return self.client.patch(f"/browser-agents{path}", **kw)

    def put(self, path: str, who: str = "it", **kw):
        self.who = who
        return self.client.put(f"/browser-agents{path}", **kw)

    def delete(self, path: str, who: str = "it", **kw):
        self.who = who
        return self.client.delete(f"/browser-agents{path}", **kw)

    def say(self, help_id: str, who: str = "ea", **body):
        body.setdefault("client_msg_id", str(uuid.uuid4()))
        return self.post(f"/help/{help_id}/messages", who=who, json=body)

    # ── worker calls ──
    def open_help(self, token: str, boot: str, job: dict, **body):
        job = self.db.one("browser_agent_jobs", id=job["id"])
        body.setdefault("reason_code", "needs_info")
        return self.client.post(f"{API}/jobs/{job['id']}/help", json=body,
                                headers=self.lease_headers(token, job["id"], int(job["lease_epoch"]), boot))

    def wcall(self, method: str, token: str, boot: str, job: dict, path: str, **kw):
        job = self.db.one("browser_agent_jobs", id=job["id"])
        headers = self.lease_headers(token, job["id"], int(job["lease_epoch"]), boot)
        return getattr(self.client, method)(f"{API}/jobs/{job['id']}{path}", headers=headers, **kw)

    def second_job(self, token: str, boot: str, agent_type: str = "rfi_from_email") -> dict:
        """One more claimed job for the same worker."""
        job = self.job(agent_type)
        resp = self.claim(token, boot, {agent_type: 1})
        assert resp.status_code == 200, resp.text
        assert job["id"] in [j["job_id"] for j in resp.json()["jobs"]]
        return self.db.one("browser_agent_jobs", id=job["id"])


__all__ = ["HumanHarness", "PROFILES", "API"]
