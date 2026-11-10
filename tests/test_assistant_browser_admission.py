"""Browser v2 task admission and the model-facing routes
(docs/ASSISTANT_BROWSER_V2_PLAN.md 4.3, 5.3, 5.5, 12; WI-5).

Everything runs on an in-memory twin of the 0192 admission RPCs (needs,
status priority, enqueue recheck, per-owner queued limit) installed over
app.assistant.repo; no database is touched (tests/conftest.py guard).
Cards are captured at assistant_browser_chat.post_card.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, repo, spend
from app.core.deps import get_current_user, require_writer
from app.routers import assistant_browser_requests as routes
from app.services import assistant_browser as service
from app.services import assistant_browser_chat as chat
from app.services import assistant_browser_sites as sites

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
CONV = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
MSG = "55555555-5555-4555-8555-555555555555"
PROJECT = "66666666-6666-4666-8666-666666666666"
MIGRATION = Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "0192_assistant_browser_v2.sql"

GOAL = "Read the open RFIs on the Henderson job"
PROCORE_URL = "https://app.procore.com/123/project/rfis"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _site(key: str, label: str, domains: list[str], *, kind: str = "curated", login_kind: str = "password_mfa",
          enabled: bool = True, origins: list[str] | None = None) -> dict:
    return {"site_key": key, "label": label, "kind": kind, "registrable_domains": domains,
            "exact_origins": origins or [], "path_prefix": None, "login_origins": [], "login_kind": login_kind,
            "enabled": enabled, "prefer_api": None}


class Twin:
    """The 0192 admission, request and enqueue RPCs in Python."""

    def __init__(self) -> None:
        self.sites = {
            "procore": _site("procore", "Procore", ["procore.com"]),
            "smartbid": _site("smartbid", "SmartBid", ["smartbidnet.com"], login_kind="password"),
            "openpages": _site("openpages", "Open pages", ["openpages.net"], login_kind="none"),
            "ssoonly": _site("ssoonly", "SSO site", ["ssoonly.net"], login_kind="sso_only"),
        }
        self.grants: dict[str, dict] = {}
        self.credentials: list[dict] = []
        self.sessions: list[dict] = []
        self.admissions: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.cards: list[dict] = []
        self.enqueue_calls: list[dict] = []
        self.projects = {PROJECT}
        self.project_context: set[tuple[str, str]] = set()
        self.conversation = {"id": CONV, "user_id": USER, "mode": "normal", "tainted_at": None,
                             "mailbox_tainted_at": None}
        self.queued_limit = 5
        self.email_urls: dict[str, dict] = {}
        self.user_texts: list[str] = []
        self.tainted_rows: list[dict] = []
        self.message = {"id": MSG, "role": "assistant", "origin": None}
        self.cancelled: list[str] = []

    # registry and grants
    def grant(self, key: str) -> dict:
        row = {"id": str(uuid.uuid4()), "user_id": USER, "site_key": key, "status": "active", "version": 1}
        self.grants[key] = row
        return row

    def credential(self, key: str, label: str = "default", **over) -> dict:
        row = {"id": str(uuid.uuid4()), "user_id": USER, "site_key": key, "account_label": label,
               "status": "active", "version": 1, "is_default": not any(c["site_key"] == key for c in self.credentials),
               "sso_required": False, "username": "owner@example.test", **over}
        self.credentials.append(row)
        return row

    def _cred(self, key: str, label: str | None) -> dict | None:
        for c in self.credentials:
            if c["site_key"] == key and c["status"] == "active" and not c.get("sso_required") and (
                    c["account_label"] == label or (label is None and c.get("is_default"))):
                return c
        return None

    # admission_needs + admission_status (0192 A1.20)
    def needs(self, a: dict) -> list[str]:
        out: list[str] = []
        keys = [a["start_site_key"]] + [e["site_key"] for e in a["site_scope"] if e["site_key"] != a["start_site_key"]]
        for key in keys:
            site = self.sites.get(key)
            if site is None or key not in self.grants:
                out.append("site_access")
                continue
            if not site["enabled"]:
                out.append("site_disabled")
                continue
            if site["login_kind"] == "none":
                continue
            label = a.get("account_label") if key == a["start_site_key"] else None
            if self._cred(key, label) or any(s["site_key"] == key for s in self.sessions):
                continue
            sso = any(c["site_key"] == key and c.get("sso_required") for c in self.credentials)
            out.append("live_signin" if sso or site["login_kind"] == "sso_only" else "credential")
        if a["start_provenance"] == "email" and not a.get("link_approved_at"):
            out.append("link_open")
        if (a["tainted"] or not a["fresh_user_message"]) and not a.get("start_approved_at"):
            out.append("task_start")
        return sorted(set(out))

    @staticmethod
    def status(needs: list[str]) -> str:
        if "site_access" in needs or "site_disabled" in needs:
            return "waiting_grant"
        if "credential" in needs or "live_signin" in needs:
            return "waiting_credential"
        if "link_open" in needs:
            return "waiting_link"
        if "task_start" in needs:
            return "waiting_start"
        return "ready"

    def admission_create(self, user_id: str, record: dict) -> dict:
        assert user_id == USER
        if self.conversation["mode"] != "normal" or self.conversation["mailbox_tainted_at"]:
            return {"ok": False, "code": "conversation_changed"}
        aid = str(uuid.uuid4())
        a = {"id": aid, "user_id": user_id, "conversation_id": record["conversation_id"], "goal": record["goal"],
             "start_url": record.get("start_url"), "start_provenance": record.get("start_provenance", "unknown"),
             "start_site_key": record["start_site_key"], "site_scope": [], "account_label": record.get("account_label"),
             "project_id": record.get("project_id"), "max_cost_usd": "5.00", "model": "gpt-5.4",
             "tainted": bool(record.get("tainted")) or bool(self.conversation["tainted_at"]),
             "fresh_user_message": bool(record.get("fresh_user_message")), "start_approved_at": None,
             "link_approved_at": None, "job_id": None, "expires_at": _iso(_now() + timedelta(hours=24)),
             "record": dict(record)}
        a["site_scope"] = [{"site_key": record["start_site_key"]}]
        needs = self.needs(a)
        a.update(needs=needs, status=self.status(needs))
        self.admissions[aid] = a
        return {"ok": True, "admission": dict(a), "needs": needs}

    def enqueue(self, user_id: str, admission_id: str, job_input: dict, *, user_cap_default, global_cap_default):
        self.enqueue_calls.append({"admission_id": admission_id, "input": dict(job_input),
                                   "user_cap": user_cap_default, "global_cap": global_cap_default})
        a = self.admissions.get(admission_id)
        if a is None or a["user_id"] != user_id:
            return {"ok": False, "code": "admission_changed"}
        existing = next((j for j in self.jobs.values() if j["assistant_admission_id"] == admission_id), None)
        if existing:
            return {"ok": True, "created": False, "job": existing}
        if a["status"] in ("declined", "expired", "enqueued"):
            return {"ok": False, "code": "admission_changed"}
        needs = self.needs(a)
        if needs:
            a.update(needs=needs, status=self.status(needs))
            return {"ok": False, "code": "needs", "needs": needs}
        active = [j for j in self.jobs.values() if j["status"] in ("queued", "running", "waiting_help", "applying")]
        if len(active) >= self.queued_limit:
            return {"ok": False, "code": "concurrency_cap"}
        jid = str(uuid.uuid4())
        job = {"id": jid, "agent_type": "user_browser", "principal_kind": "user", "principal_user_id": user_id,
               "assistant_admission_id": admission_id, "status": "queued", "step_count": 0, "cost_usd": 0,
               "input": {"admission_id": admission_id, "goal": a["goal"], "start_url": a["start_url"],
                         "start_site_key": a["start_site_key"]},
               "cancel_requested_at": None}
        self.jobs[jid] = job
        a.update(status="enqueued", job_id=jid, needs=[])
        return {"ok": True, "created": True, "job": job}

    def request_create(self, user_id: str, record: dict) -> dict:
        if record.get("admission_id") and record["admission_id"] not in self.admissions:
            return {"ok": False, "code": "invalid"}
        rid = str(uuid.uuid4())
        row = {"id": rid, "user_id": user_id, "status": "requested", "admission_id": None,
               "expires_at": _iso(_now() + timedelta(hours=24)), **record}
        self.requests[rid] = row
        return {"ok": True, "request": dict(row)}

    def requests_open(self, user_id: str, *, conversation_id=None, job_id=None, admission_id=None, kind=None,
                      limit: int = 50) -> list[dict]:
        out = []
        for r in self.requests.values():
            if r["user_id"] != user_id or r["status"] != "requested":
                continue
            if conversation_id is not None and r.get("conversation_id") != conversation_id:
                continue
            if admission_id is not None and r.get("admission_id") != admission_id:
                continue
            if kind is not None and r["kind"] != kind:
                continue
            out.append(dict(r))
        return out

    def request_decide(self, user_id: str, request_id: str, decision: str) -> dict:
        r = self.requests.get(request_id)
        if r is None or r["status"] != "requested":
            return {"ok": False, "code": "request_changed"}
        r["status"] = {"approve": "approved", "decline": "declined", "cancel": "cancelled"}[decision]
        a = self.admissions.get(r.get("admission_id") or "")
        if a is not None:
            if decision == "approve":
                a["link_approved_at"] = _iso(_now())
            elif a["status"].startswith("waiting_"):
                a["status"] = "declined"
        return {"ok": True, "request": dict(r)}

    def start_decide(self, admission_id: str) -> None:
        a = self.admissions[admission_id]
        a["start_approved_at"] = _iso(_now())
        for r in self.requests.values():
            if r.get("admission_id") == admission_id and r["kind"] == "task_start" and r["status"] == "requested":
                r["status"] = "approved"
        a["needs"] = self.needs(a)
        a["status"] = self.status(a["needs"])

    def install(self, monkeypatch) -> None:
        cfg = SimpleNamespace(assistant_browser_enabled=True, assistant_browser_v2_enabled=True,
                              assistant_daily_cap_usd=15.0, assistant_global_daily_cap_usd=50.0)
        monkeypatch.setattr(service, "get_settings", lambda: cfg)
        monkeypatch.setattr(spend, "get_settings", lambda: cfg)
        self.config = cfg
        self.owner_problem = None
        monkeypatch.setattr(access, "owner_check", lambda user_id, **kw: SimpleNamespace(problem=self.owner_problem))
        self.type_row = {"key": "user_browser", "principal_kind": "user", "enabled": True, "provider": "openai",
                         "model": "gpt-5.4", "model_probe_ok_at": "2026-11-02T00:00:00Z",
                         "price_in_per_mtok": 2.5, "price_out_per_mtok": 15}
        self.settings_row = {"id": 1, "enabled": True, "user_per_job_cost_cap_usd": 5,
                             "user_task_reserve_step_usd": 1}
        patches = {
            "browser_platform_config_v2": lambda user_id: (self.type_row, self.settings_row),
            "get_conversation": lambda user_id, cid: dict(self.conversation) if cid == CONV and user_id == USER else None,
            "browser_site": lambda user_id, key: self.sites.get(key),
            "browser_sites": lambda user_id, include_disabled=False, kind=None, limit=200: [
                s for s in self.sites.values() if (kind is None or s["kind"] == kind)
                and (include_disabled or s["enabled"])],
            "browser_sites_by_keys": lambda user_id, keys: {k: self.sites[k] for k in keys if k in self.sites},
            "browser_sites_for_url": lambda user_id, origin, registrable_domain: list(self.sites.values()),
            "browser_site_grant": lambda user_id, key: self.grants.get(key) if user_id == USER else None,
            "browser_site_grants": lambda user_id, status="active", limit=200: list(self.grants.values()),
            "browser_credential_v2": lambda user_id, key, label=None, active_only=False: (
                next((c for c in self.credentials if c["site_key"] == key and c["account_label"] == label
                      and (not active_only or c["status"] == "active")), None)
                if label is not None else next((c for c in self.credentials if c["site_key"] == key
                                                and c["status"] == "active"), None)),
            "browser_credentials_for_site": lambda user_id, key: [c for c in self.credentials if c["site_key"] == key],
            "browser_sessions_v2": lambda user_id, site_key=None, limit=100: list(self.sessions),
            "browser_admission_create": self.admission_create,
            "browser_admission": lambda user_id, aid: (dict(self.admissions[aid])
                                                       if aid in self.admissions and user_id == USER else None),
            "browser_task_enqueue_v2": self.enqueue,
            "browser_job_by_admission": lambda user_id, aid: next(
                (j for j in self.jobs.values() if j["assistant_admission_id"] == aid), None),
            "browser_job": lambda user_id, jid: (self.jobs.get(jid)
                                                 if self.jobs.get(jid, {}).get("principal_user_id") == user_id else None),
            "browser_request": lambda user_id, rid: self.requests.get(rid) if user_id == USER else None,
            "browser_request_create": self.request_create,
            "browser_requests_open": self.requests_open,
            "browser_request_decide": self.request_decide,
            "browser_holds": lambda user_id, jid, status=None, limit=50: [],
            "browser_records": lambda *a, **k: [],
            "browser_project_exists": lambda user_id, pid: pid in self.projects,
            "browser_project_in_context": lambda user_id, cid, pid: (cid, pid) in self.project_context,
            "browser_job_cancel_request": self._cancel,
            "browser_message_turn": lambda user_id, mid: dict(self.message) if mid == MSG else None,
            "tainted_results": lambda user_id, cid, limit=50: list(self.tainted_rows),
            "mail_user_texts": lambda user_id, run_id: list(self.user_texts),
            "conversation_for_run": lambda user_id, run_id: CONV,
            "browser_url_email_sources": lambda user_id, urls: [self.email_urls[u] for u in urls if u in self.email_urls],
            "browser_url_bdr_records": lambda user_id, urls: [],
        }
        for name, fn in patches.items():
            assert hasattr(repo, name), name
            monkeypatch.setattr(repo, name, fn)

        def post_card(user_id, conversation_id, block, *, event_key, scope_id):
            self.cards.append({"user_id": user_id, "conversation_id": conversation_id, "block": dict(block),
                               "event_key": event_key, "scope_id": scope_id})
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope_id}:{event_key}"))

        monkeypatch.setattr(chat, "post_card", post_card)

    def _cancel(self, user_id: str, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if (job is None or job["principal_user_id"] != user_id or job["cancel_requested_at"]
                or job["status"] not in ("queued", "running", "waiting_help")):
            return None
        job["cancel_requested_at"] = _iso(_now())
        self.cancelled.append(job_id)
        return job


@pytest.fixture
def twin(monkeypatch) -> Twin:
    t = Twin()
    t.install(monkeypatch)
    return t


def _admit(args: dict | None = None, *, tainted: bool = False, fresh: bool = True) -> dict:
    return service.admit(USER, RUN, CONV, args or {"site_key": "procore", "goal": GOAL},
                         tainted=tainted, fresh_user_message=fresh)


def _ready_procore(t: Twin) -> None:
    t.grant("procore")
    t.credential("procore")


# ── Default caps and the stepped reservation ─────────────────────────────────


def test_untainted_fresh_turn_on_an_approved_site_enqueues_at_the_default_caps(twin):
    _ready_procore(twin)
    out = _admit()
    assert out["status"] == "queued" and out["task_id"] in twin.jobs and out["needs"] == []
    assert out["sites_in_scope"] == ["procore"]
    # D4: no approval card; only the server-built task card (4.5), keyed to the job.
    assert [c["block"]["type"] for c in twin.cards] == ["browser_task"]
    assert out["cards"] == [{"kind": "task", "request_id": None, "posted": True}]
    call = twin.enqueue_calls[-1]
    # The configured day caps (owner answer 5: 15 USD per user), never a cost
    # from the model, and p_input = {} (protocol.UserBrowserInput).
    assert call["user_cap"] == Decimal("15") and call["global_cap"] == Decimal("50")
    assert call["input"] == {}
    record = twin.admissions[out["admission_id"]]["record"]
    assert "max_cost_usd" not in record and "model" not in record


def test_enqueue_reserves_one_step_not_the_whole_task_cap():
    """The SQL twin of 12.1: the first reserve is the step setting (1 USD),
    one expiry for job and reservation, no least(2, ...) anywhere in v2."""
    text = MIGRATION.read_text(encoding="utf-8")
    body = text[text.index("function public.assistant_browser_task_enqueue_v2"):]
    body = body[:body.index("end $$;")]
    assert "v_step := least(cfg.user_task_reserve_step_usd, a.max_cost_usd)" in body
    assert "assistant_spend_reserve(p_user_id, 'browser_task', v_step" in body
    assert "v_exp" in body and "least(2" not in body
    assert "p_user_cap_default numeric default 15" in body


def test_money_never_comes_from_the_model(twin):
    _ready_procore(twin)
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit({"site_key": "procore", "goal": GOAL, "max_cost_usd": "5"})
    assert exc.value.code == "invalid"
    with pytest.raises(service.AdmissionRefused):
        _admit({"site_key": "procore", "goal": GOAL, "credential_id": str(uuid.uuid4())})


def test_budget_refusals_carry_fixed_messages(twin, monkeypatch):
    _ready_procore(twin)
    monkeypatch.setattr(repo, "browser_task_enqueue_v2", lambda *a, **k: {"ok": False, "code": "user_cap"})
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit()
    assert exc.value.code == "user_cap" and str(exc.value) == "Your daily assistant budget is used up."
    for code in ("type_unavailable", "runtime_unavailable"):
        monkeypatch.setattr(repo, "browser_task_enqueue_v2", lambda *a, code=code, **k: {"ok": False, "code": code})
        with pytest.raises(service.AdmissionRefused) as exc:
            _admit()
        assert exc.value.code == "unavailable"


# ── Two concurrent tasks ─────────────────────────────────────────────────────


def test_two_concurrent_tasks_for_one_owner_both_enqueue(twin):
    _ready_procore(twin)
    first = _admit()
    second = _admit({"site_key": "procore", "goal": "Read the submittal log"})
    assert first["status"] == second["status"] == "queued"
    assert first["task_id"] != second["task_id"]
    assert len(twin.jobs) == 2 and len(twin.enqueue_calls) == 2


def test_the_per_owner_queued_limit_refuses_the_next_task(twin):
    _ready_procore(twin)
    twin.queued_limit = 2
    _admit()
    _admit()
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit()
    assert exc.value.code == "concurrency_cap"


def test_a_ready_admission_refused_by_a_cap_is_retried_by_the_status_read(twin):
    """Review finding: a cap refusal after the start click left the
    admission 'ready' with nothing retrying it and a status that promised a
    start."""
    _ready_procore(twin)
    out = _admit(tainted=True)
    aid = out["admission_id"]
    assert out["status"] == "waiting_start"
    twin.queued_limit = 0
    twin.start_decide(aid)
    with pytest.raises(service.AdmissionRefused) as exc:
        service.continue_admission(USER, aid)
    assert exc.value.code == "concurrency_cap"
    status = service.task_status(USER, aid)
    assert status["status"] == "ready" and status["refused"] == "concurrency_cap"
    assert "will start" not in status["message"] and "Too many browser tasks" in status["message"]
    assert twin.jobs == {}
    # Once a slot frees up, the next status read enqueues it.
    twin.queued_limit = 5
    status = service.task_status(USER, aid)
    assert status["status"] == "queued" and status["task_id"] in twin.jobs
    assert status["admission_id"] == aid


def test_ready_status_never_promises_a_start():
    assert "will start" not in service.STATUS_MESSAGES["ready"]


# ── Start card under taint or without a fresh user message ───────────────────


@pytest.mark.parametrize(("tainted", "fresh", "reason"), [
    (True, True, "tainted_conversation"),
    (False, False, "unattended_turn"),
])
def test_start_card_and_no_job_under_taint_or_without_a_fresh_message(twin, tainted, fresh, reason):
    _ready_procore(twin)
    out = _admit(tainted=tainted, fresh=fresh)
    assert out["status"] == "waiting_start" and out["task_id"] is None
    assert out["needs"] == ["task_start"] and twin.jobs == {} and twin.enqueue_calls == []
    [card] = twin.cards
    assert card["block"]["type"] == "browser_task_start"
    assert card["block"]["admission_id"] == out["admission_id"]
    assert card["scope_id"] == out["admission_id"] and card["conversation_id"] == CONV
    request = twin.requests[card["block"]["request_id"]]
    assert request["kind"] == "task_start" and request["reason_code"] == reason
    # Only the owner's click (WI-4 start route) admits it; then continue enqueues.
    twin.start_decide(out["admission_id"])
    after = service.continue_admission(USER, out["admission_id"])
    assert after["status"] == "queued" and after["task_id"] in twin.jobs


def test_conversation_taint_marks_the_admission_tainted(twin):
    _ready_procore(twin)
    twin.conversation["tainted_at"] = "2026-11-02T10:00:00Z"
    out = _admit(tainted=False)
    assert out["status"] == "waiting_start"


def test_turn_flags_read_the_tool_results_and_the_message_origin(twin):
    turn = {"conversation_id": CONV, "message_id": MSG, "tainted": False, "mailbox_tainted": False,
            "user_texts": ["read the RFIs"]}
    assert service.turn_flags(USER, turn) == (False, True)
    twin.tainted_rows = [{"tool_name": "email_read", "payload": {}}]
    assert service.turn_flags(USER, turn) == (True, True)
    twin.tainted_rows = []
    twin.message["origin"] = "browser_result"
    assert service.turn_flags(USER, turn) == (False, False)
    twin.message["origin"] = None
    assert service.turn_flags(USER, {**turn, "user_texts": []}) == (False, False)


def test_turn_flags_fail_closed_on_a_read_error(twin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(repo, "tainted_results", boom)
    monkeypatch.setattr(repo, "browser_message_turn", boom)
    turn = {"conversation_id": CONV, "message_id": MSG, "user_texts": ["x"]}
    assert service.turn_flags(USER, turn) == (True, False)


def test_mailbox_review_and_mailbox_taint_are_refused(twin):
    _ready_procore(twin)
    twin.conversation["mailbox_tainted_at"] = "2026-11-02T10:00:00Z"
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit()
    assert exc.value.code == "conversation"
    twin.conversation.update(mailbox_tainted_at=None, mode="mailbox_review")
    with pytest.raises(service.AdmissionRefused):
        _admit()
    assert twin.admissions == {}


# ── Site access and credential cards ─────────────────────────────────────────


def test_site_without_a_grant_posts_one_site_access_card_then_a_credential_card(twin):
    out = _admit()
    assert out["status"] == "waiting_grant" and out["needs"] == ["site_access"]
    [card] = twin.cards
    assert card["block"] == {"type": "browser_site_access", "request_id": card["block"]["request_id"],
                             "status": "requested", "hold_id": None}
    request = twin.requests[card["block"]["request_id"]]
    assert request["kind"] == "site_access" and request["site_key"] == "procore"
    assert request["admission_id"] == out["admission_id"] and request["reason_code"] == "task_needs_site"
    # Continue again before the click: no duplicate card.
    again = service.continue_admission(USER, out["admission_id"])
    assert again["status"] == "waiting_grant" and len(twin.cards) == 1
    # The human grants (WI-4); the next card is the credential card.
    twin.grant("procore")
    after = service.continue_admission(USER, out["admission_id"])
    assert after["status"] == "waiting_credential" and after["needs"] == ["credential"]
    block = twin.cards[-1]["block"]
    assert block["type"] == "browser_credential_request" and block["site_key"] == "procore"
    assert "username" not in block and "password" not in block
    twin.credential("procore")
    done = service.continue_admission(USER, out["admission_id"])
    assert done["status"] == "queued" and done["task_id"]
    # The model sees the job from now on.
    assert service.continue_admission(USER, out["admission_id"])["task_id"] == done["task_id"]


def test_a_site_without_a_login_runs_without_a_credential(twin):
    twin.grant("openpages")
    out = _admit({"start_url": "https://www.openpages.net/plans", "goal": "Read the plan list"})
    assert out["status"] == "queued" and out["sites_in_scope"] == ["openpages"]


def test_an_unknown_site_by_url_waits_for_an_ad_hoc_grant(twin):
    out = _admit({"start_url": "https://bids.examplegc.com/project/7", "goal": "Read the bid form"})
    assert out["status"] == "waiting_grant"
    record = twin.admissions[out["admission_id"]]["record"]
    assert record["start_site_key"] == "domain:examplegc.com"
    assert record["start_url"] == "https://bids.examplegc.com/project/7"
    request = twin.requests[twin.cards[0]["block"]["request_id"]]
    assert request["site_key"] == "domain:examplegc.com" and request["url"] == record["start_url"]


def test_unknown_site_key_disabled_site_mismatch_and_identity_providers_are_refused(twin):
    for args, code in (
        ({"site_key": "nosuchsite", "goal": GOAL}, "unknown_site"),
        ({"site_key": "smartbid", "start_url": PROCORE_URL, "goal": GOAL}, "site_mismatch"),
        ({"start_url": "https://accounts.google.com/signin", "goal": GOAL}, "identity_provider"),
        ({"start_url": "http://192.168.1.10/admin", "goal": GOAL}, "url_refused"),
        ({"start_url": "https://user:pw@app.procore.com/", "goal": GOAL}, "url_refused"),
        ({"site_key": "procore", "goal": "   "}, "invalid"),
        ({"site_key": "procore", "goal": GOAL, "account_label": "bad/label"}, "invalid"),
    ):
        with pytest.raises(service.AdmissionRefused) as exc:
            _admit(args)
        assert exc.value.code == code, args
    twin.sites["smartbid"]["enabled"] = False
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit({"site_key": "smartbid", "goal": GOAL})
    assert exc.value.code == "site_disabled"
    assert twin.admissions == {}


def test_sso_sites_and_sso_accounts_are_refused_before_wave_e(twin):
    twin.grant("ssoonly")
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit({"site_key": "ssoonly", "goal": GOAL})
    assert exc.value.code == "sso_required"
    twin.grant("procore")
    twin.credential("procore", "work", sso_required=True)
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit({"site_key": "procore", "goal": GOAL, "account_label": "work"})
    assert exc.value.code == "sso_required"


def test_a_named_account_label_needs_that_account(twin):
    _ready_procore(twin)
    out = _admit({"site_key": "procore", "goal": GOAL, "account_label": "second"})
    assert out["status"] == "waiting_credential"
    block = twin.cards[-1]["block"]
    assert block["account_label"] == "second"


# ── Email link card (5.5) ────────────────────────────────────────────────────


def test_an_rfp_harvest_link_needs_the_link_card_even_on_an_approved_site(twin):
    _ready_procore(twin)
    twin.email_urls[PROCORE_URL] = {"source": "rfp_harvest", "sender_domain": "gc.example"}
    out = _admit({"start_url": PROCORE_URL, "goal": GOAL})
    assert out["status"] == "waiting_link" and out["needs"] == ["link_open"]
    [card] = twin.cards
    assert card["block"]["type"] == "browser_link_open"
    request = twin.requests[card["block"]["request_id"]]
    assert request["kind"] == "link_open" and request["provenance"] == "email" and request["url"] == PROCORE_URL
    assert twin.admissions[out["admission_id"]]["record"]["start_provenance"] == "email"
    # The owner approves the link (request_decide approve, WI-4); then it runs.
    twin.request_decide(USER, request["id"], "approve")
    assert service.continue_admission(USER, out["admission_id"])["status"] == "queued"


def test_an_email_link_inside_the_goal_is_refused_until_it_is_the_start_url(twin):
    """D6 (review finding): the link card may not be skipped by writing the
    email link into the free-text goal with no start_url."""
    _ready_procore(twin)
    twin.email_urls[PROCORE_URL] = {"source": "rfp_harvest", "sender_domain": "gc.example"}
    for goal in (f"Open {PROCORE_URL} and read the RFIs", f"Read the RFIs ({PROCORE_URL})."):
        with pytest.raises(service.AdmissionRefused) as exc:
            _admit({"site_key": "procore", "goal": goal})
        assert exc.value.code == "link_in_goal" and exc.value.message == "Pass the link as start_url."
    assert twin.jobs == {}
    # Passed as start_url, the same link gets its card.
    out = _admit({"start_url": PROCORE_URL, "goal": f"Open {PROCORE_URL} and read the RFIs"})
    assert out["status"] == "waiting_link"


def test_a_goal_link_that_is_not_from_email_is_allowed(twin):
    _ready_procore(twin)
    out = _admit({"site_key": "procore", "goal": f"Open {PROCORE_URL} and read the RFIs"})
    assert out["status"] == "queued"


def test_a_typed_link_starts_without_a_card(twin):
    _ready_procore(twin)
    twin.email_urls[PROCORE_URL] = {"source": "rfp_harvest", "sender_domain": "gc.example"}
    twin.user_texts = [f"please open {PROCORE_URL} and read the RFIs"]
    out = _admit({"start_url": PROCORE_URL, "goal": GOAL})
    assert out["status"] == "queued" and [c["block"]["type"] for c in twin.cards] == ["browser_task"]
    assert twin.admissions[out["admission_id"]]["record"]["start_provenance"] == "typed"


def test_a_link_from_a_mail_tool_result_needs_the_link_card(twin):
    _ready_procore(twin)
    twin.tainted_rows = [{"tool_name": "email_read", "payload": {"body": f"see {PROCORE_URL}"}, "taint_corpus": []}]
    out = _admit({"start_url": PROCORE_URL, "goal": GOAL}, tainted=True)
    # Link card first (5.5), then the start card (4.3).
    assert out["status"] == "waiting_link" and set(out["needs"]) == {"link_open", "task_start"}
    assert [c["block"]["type"] for c in twin.cards] == ["browser_link_open"]


def test_an_unapproved_site_gets_the_site_card_whatever_the_provenance(twin):
    twin.email_urls[PROCORE_URL] = {"source": "rfp_harvest", "sender_domain": "gc.example"}
    out = _admit({"start_url": PROCORE_URL, "goal": GOAL})
    assert out["status"] == "waiting_grant"
    assert twin.cards[0]["block"]["type"] == "browser_site_access"
    assert twin.requests[twin.cards[0]["block"]["request_id"]]["provenance"] == "email"


# ── Project right (5.3 step 6) ───────────────────────────────────────────────


def test_project_must_exist(twin):
    _ready_procore(twin)
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        with pytest.raises(service.AdmissionRefused) as exc:
            _admit({"site_key": "procore", "goal": GOAL, "project_id": bad})
        assert exc.value.code == "project_invalid"
    out = _admit({"site_key": "procore", "goal": GOAL, "project_id": PROJECT})
    assert out["status"] == "queued"
    assert twin.admissions[out["admission_id"]]["record"]["project_id"] == PROJECT


def test_tainted_project_must_be_bound_to_the_user(twin):
    _ready_procore(twin)
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit({"site_key": "procore", "goal": GOAL, "project_id": PROJECT}, tainted=True)
    assert exc.value.code == "project_not_bound"
    twin.project_context.add((CONV, PROJECT))
    out = _admit({"site_key": "procore", "goal": GOAL, "project_id": PROJECT}, tainted=True)
    assert out["status"] == "waiting_start"
    twin.project_context.clear()
    twin.user_texts = [f"use project {PROJECT}"]
    assert _admit({"site_key": "procore", "goal": GOAL, "project_id": PROJECT}, tainted=True)["admission_id"]


def test_project_right_needs_a_writer_owner(twin):
    _ready_procore(twin)
    twin.owner_problem = "owner_changed"
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit({"site_key": "procore", "goal": GOAL, "project_id": PROJECT})
    assert exc.value.code == "ineligible"


# ── Gates ────────────────────────────────────────────────────────────────────


def test_flags_and_type_row_gate_admission(twin):
    _ready_procore(twin)
    twin.config.assistant_browser_v2_enabled = False
    with pytest.raises(service.AdmissionRefused) as exc:
        _admit()
    assert exc.value.code == "unavailable"
    twin.config.assistant_browser_v2_enabled = True
    for field, value in (("enabled", False), ("model_probe_ok_at", None), ("provider", "other"),
                         ("price_in_per_mtok", 0)):
        saved = twin.type_row[field]
        twin.type_row[field] = value
        with pytest.raises(service.AdmissionRefused) as exc:
            _admit()
        assert exc.value.code == "unavailable", field
        twin.type_row[field] = saved
    assert twin.admissions == {}


def test_continue_admission_of_a_declined_or_missing_admission(twin):
    out = _admit()
    twin.admissions[out["admission_id"]]["status"] = "declined"
    assert service.continue_admission(USER, out["admission_id"])["status"] == "declined"
    assert twin.enqueue_calls == []
    with pytest.raises(service.AdmissionRefused) as exc:
        service.continue_admission(USER, str(uuid.uuid4()))
    assert exc.value.code == "not_found"


def test_a_card_that_cannot_be_posted_is_reported_not_raised(twin, monkeypatch):
    def not_built(*a, **k):
        raise NotImplementedError("WI-13")
    monkeypatch.setattr(chat, "post_card", not_built)
    out = _admit()
    assert out["status"] == "waiting_grant"
    assert out["cards"][0]["posted"] is False and out["cards"][0]["request_id"] in twin.requests


# ── Status, cancel, sites, model card requests ───────────────────────────────


def test_task_status_for_job_admission_and_credential_request(twin):
    _ready_procore(twin)
    job = _admit()
    status = service.task_status(USER, job["task_id"])
    assert status["status"] == "queued" and status["steps"] == 0 and status["holds_pending"] == 0
    assert status["site_label"] == "Procore" and status["goal"] == GOAL and status["result"] is None
    waiting = _admit(tainted=True)
    assert service.task_status(USER, waiting["admission_id"])["status"] == "waiting_start"
    request = twin.request_create(USER, {"kind": "credential", "site_key": "procore", "account_label": "default"})
    rid = request["request"]["id"]
    twin.requests[rid].update(status="saved", result={"version": 3})
    assert service.task_status(USER, rid) == {"request_id": rid, "kind": "credential", "status": "saved",
                                              "saved": True, "site_key": "procore", "account_label": "default",
                                              "version": 3}
    with pytest.raises(service.AdmissionRefused):
        service.task_status(USER, str(uuid.uuid4()))


def test_cancel_own_job_and_waiting_admission(twin):
    _ready_procore(twin)
    job = _admit()
    assert service.cancel_task(USER, job["task_id"]) == {"status": "cancel_requested"}
    assert twin.cancelled == [job["task_id"]]
    assert service.cancel_task(USER, job["task_id"]) == {"status": "cancel_requested"}
    waiting = _admit(tainted=True)
    assert service.cancel_task(USER, waiting["admission_id"]) == {"status": "declined"}
    assert twin.admissions[waiting["admission_id"]]["status"] == "declined"
    twin.jobs[job["task_id"]]["principal_user_id"] = OTHER
    with pytest.raises(service.AdmissionRefused):
        service.cancel_task(USER, job["task_id"])


def test_the_task_card_is_posted_once_per_job_with_fixed_fields(twin):
    _ready_procore(twin)
    out = _admit()
    [card] = twin.cards
    block = card["block"]
    assert block == {"type": "browser_task", "job_id": out["task_id"], "status": "queued", "steps": 0,
                     "cost_usd": 0.0, "holds_pending": 0, "site_label": "Procore", "goal": GOAL}
    # uuid5(job id, "task"): the writer's id, so a retry never duplicates.
    assert card["scope_id"] == out["task_id"] and card["event_key"] == "task" and card["conversation_id"] == CONV
    # A replayed enqueue (created: false) posts nothing more.
    service.continue_admission(USER, out["admission_id"])
    service.enqueue_admission(USER, out["admission_id"])
    assert len(twin.cards) == 1


def test_a_task_card_that_cannot_be_posted_never_fails_the_enqueue(twin, monkeypatch):
    _ready_procore(twin)
    def down(*a, **k):
        raise RuntimeError("chat down")
    monkeypatch.setattr(chat, "post_card", down)
    out = _admit()
    assert out["status"] == "queued" and out["task_id"] in twin.jobs
    assert out["cards"] == [{"kind": "task", "request_id": None, "posted": False}]


def test_cancel_declines_a_waiting_admission_whose_card_is_gone(twin, monkeypatch):
    _ready_procore(twin)
    waiting = _admit(tainted=True)
    for r in twin.requests.values():
        r["status"] = "expired"
    decided: list[tuple[str, str]] = []

    def start_decide(user_id, admission_id, decision):
        decided.append((admission_id, decision))
        twin.admissions[admission_id]["status"] = "declined"
        return {"ok": True}
    monkeypatch.setattr(repo, "browser_start_decide", start_decide)
    assert service.cancel_task(USER, waiting["admission_id"]) == {"status": "declined"}
    assert decided == [(waiting["admission_id"], "decline")]
    # Already declined: nothing more to stop.
    assert service.cancel_task(USER, waiting["admission_id"]) == {"status": "declined"}
    assert len(decided) == 1


def test_sites_list_shows_labels_and_sessions_never_usernames(twin):
    twin.grant("procore")
    twin.credential("procore", "default")
    twin.credential("procore", "second")
    twin.sessions.append({"site_key": "procore", "account_label": "default",
                          "expires_at": _iso(_now() + timedelta(hours=2))})
    items = {i["site_key"]: i for i in service.sites_list(USER)["items"]}
    assert items["procore"]["approved"] is True and items["procore"]["account_labels"] == ["default", "second"]
    assert items["procore"]["has_session"] is True
    assert items["smartbid"]["approved"] is False and items["smartbid"]["account_labels"] == []
    assert "owner@example.test" not in repr(items)


def test_request_site_access_and_credential_post_cards_once(twin):
    out = service.request_site_access(USER, RUN, CONV, {"site_key": "smartbid", "reason_code": "user_asked"})
    assert out["status"] == "card_posted" and out["request_id"] in twin.requests
    again = service.request_site_access(USER, RUN, CONV, {"site_key": "smartbid", "reason_code": "user_asked"})
    assert again["request_id"] == out["request_id"] and len(twin.cards) == 1
    twin.grant("smartbid")
    assert service.request_site_access(USER, RUN, CONV, {"site_key": "smartbid", "reason_code": "user_asked"})[
        "status"] == "already_approved"
    cred = service.request_site_credential(USER, RUN, CONV, {"site_key": "smartbid", "account_label": "office",
                                                             "reason_code": "no_saved_login"})
    assert cred["status"] == "card_posted"
    block = twin.cards[-1]["block"]
    assert block["type"] == "browser_credential_request" and block["account_label"] == "office"
    for args, code in (({"site_key": "openpages", "reason_code": "user_asked"}, "no_login"),
                       ({"site_key": "ssoonly", "reason_code": "user_asked"}, "sso_required"),
                       ({"site_key": "nosuchsite", "reason_code": "user_asked"}, "unknown_site"),
                       ({"site_key": "smartbid", "reason_code": "please"}, "invalid")):
        with pytest.raises(service.AdmissionRefused) as exc:
            service.request_site_credential(USER, RUN, CONV, args)
        assert exc.value.code == code


def test_another_users_ad_hoc_site_is_unknown_by_key_until_this_owner_approves_it(twin):
    """Review finding: ad hoc registry rows are global; a key alone must not
    reach a site this owner never approved, nor reveal that it exists."""
    twin.sites["domain:procore-bids.net"] = _site("domain:procore-bids.net", "procore-bids.net",
                                                  ["procore-bids.net"], kind="ad_hoc", login_kind="password")
    for call in (
        lambda: service.request_site_credential(USER, RUN, CONV, {"site_key": "domain:procore-bids.net",
                                                                  "reason_code": "no_saved_login"}),
        lambda: service.request_site_access(USER, RUN, CONV, {"site_key": "domain:procore-bids.net",
                                                              "reason_code": "user_asked"}),
        lambda: _admit({"site_key": "domain:procore-bids.net", "goal": GOAL}),
    ):
        with pytest.raises(service.AdmissionRefused) as exc:
            call()
        assert exc.value.code == "unknown_site"
    assert twin.cards == []
    twin.grant("domain:procore-bids.net")
    cred = service.request_site_credential(USER, RUN, CONV, {"site_key": "domain:procore-bids.net",
                                                             "reason_code": "no_saved_login"})
    assert cred["status"] == "card_posted"


# ── Routes ───────────────────────────────────────────────────────────────────


@pytest.fixture
def http(twin, monkeypatch):
    principal = SimpleNamespace(id=USER, via_assistant_run_id=RUN, role="executive")
    state = {"user": principal}
    monkeypatch.setattr(routes, "_require_assistant", lambda user: (None, None))

    def turn_context(user_id, run_id, key, *, tool_name=None):
        if not key or user_id != USER or run_id != RUN:
            return None
        return {"conversation_id": CONV, "message_id": MSG, "tool_name": tool_name, "tainted": False,
                "mailbox_tainted": False, "mode": twin.conversation["mode"], "user_texts": ["read the RFIs"]}

    monkeypatch.setattr(repo, "turn_context", turn_context)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_writer] = lambda: state["user"]
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    return TestClient(app), state


HEADERS = {"X-Assistant-Idempotency-Key": "run:1:browser_task_start"}


def test_route_start_task_admits_inside_a_turn(http, twin):
    client, _ = http
    _ready_procore(twin)
    res = client.post("/assistant/browser-requests/tasks", json={"site_key": "procore", "goal": GOAL},
                      headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "queued" and res.headers["cache-control"] == "no-store"
    assert twin.admissions[res.json()["admission_id"]]["record"]["fresh_user_message"] is True


def test_route_start_task_refusals(http, twin):
    client, state = http
    res = client.post("/assistant/browser-requests/tasks", json={"site_key": "procore", "goal": GOAL})
    assert res.status_code == 409  # no trusted turn
    res = client.post("/assistant/browser-requests/tasks", json={"site_key": "nope", "goal": GOAL}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "unknown_site"
    res = client.post("/assistant/browser-requests/tasks", json={"goal": GOAL, "max_cost_usd": 5}, headers=HEADERS)
    assert res.status_code == 422
    twin.conversation["mode"] = "mailbox_review"
    res = client.post("/assistant/browser-requests/tasks", json={"site_key": "procore", "goal": GOAL},
                      headers=HEADERS)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "conversation"
    twin.conversation["mode"] = "normal"
    state["user"] = SimpleNamespace(id=USER, via_assistant_run_id=None, role="executive")
    res = client.post("/assistant/browser-requests/tasks", json={"site_key": "procore", "goal": GOAL},
                      headers=HEADERS)
    assert res.status_code == 404  # a human never starts a task here
    twin.config.assistant_browser_v2_enabled = False
    assert client.get("/assistant/browser-requests/sites").status_code == 404


def test_route_status_cancel_and_sites_for_the_owner(http, twin):
    client, state = http
    _ready_procore(twin)
    job = _admit()
    state["user"] = SimpleNamespace(id=USER, via_assistant_run_id=None, role="executive")
    res = client.get(f"/assistant/browser-requests/tasks/{job['task_id']}")
    assert res.status_code == 200 and res.json()["status"] == "queued"
    res = client.post(f"/assistant/browser-requests/tasks/{job['task_id']}/cancel", json={})
    assert res.status_code == 200 and res.json() == {"status": "cancel_requested"}
    assert client.get(f"/assistant/browser-requests/tasks/{uuid.uuid4()}").status_code == 404
    res = client.get("/assistant/browser-requests/sites")
    assert res.status_code == 200 and {i["site_key"] for i in res.json()["items"]} >= {"procore", "smartbid"}


def test_route_card_requests_need_a_turn(http, twin):
    client, state = http
    res = client.post("/assistant/browser-requests/site-access",
                      json={"site_key": "smartbid", "reason_code": "user_asked"},
                      headers={"X-Assistant-Idempotency-Key": "k1"})
    assert res.status_code == 200 and res.json()["status"] == "card_posted"
    res = client.post("/assistant/browser-requests/credential",
                      json={"site_key": "smartbid", "reason_code": "user_asked", "password": "x"},
                      headers={"X-Assistant-Idempotency-Key": "k2"})
    assert res.status_code == 422 and "password" not in res.text.split("loc")[0]
    res = client.post("/assistant/browser-requests/credential",
                      json={"site_key": "smartbid", "reason_code": "user_asked"},
                      headers={"X-Assistant-Idempotency-Key": "k2"})
    assert res.status_code == 200 and res.json()["status"] == "card_posted"
    state["user"] = SimpleNamespace(id=USER, via_assistant_run_id=None, role="executive")
    res = client.post("/assistant/browser-requests/credential",
                      json={"site_key": "smartbid", "reason_code": "user_asked"})
    assert res.status_code == 404


def test_old_phase6_entry_points_are_gone():
    for name in ("prepare", "enqueue", "save_credential", "revoke_credential"):
        assert not hasattr(service, name), name
    # The MFA reset fence and the retired type's readiness flag stay.
    assert callable(service.revoke_before_mfa_reset) and service.EXECUTION_READY is True


def test_no_em_dash_in_the_wi5_files():
    root = Path(__file__).resolve().parents[1]
    for rel in ("app/services/assistant_browser.py", "app/routers/assistant_browser_requests.py",
                "tests/test_assistant_browser_admission.py"):
        assert chr(0x2014) not in (root / rel).read_text(encoding="utf-8"), rel


def test_sites_module_is_the_url_authority():
    # Admission never parses URLs itself.
    assert service.sites is sites


def test_status_reads_the_runtime_v2_result_record_shape(monkeypatch):
    """runtime.finish stores {summary, result, facts[{text, source_url}]}:
    the status read must hand the model's result and every fact's source
    page through, and the tool output must be tainted (review finding)."""
    from app.assistant import registry
    from app.assistant.executor import contains_tainted
    from app.assistant.tools import browser as browser_tools  # noqa: F401 - registers the tools
    from app.services import assistant_browser_records as records

    job = str(uuid.uuid4())
    monkeypatch.setattr(repo, "browser_records", lambda *a, **k: [{"id": "r1"}])
    monkeypatch.setattr(records, "unpack_record", lambda *a, **k: {
        "summary": "", "result": "The RFI 45 answer is due 11-04.",
        "facts": [{"text": "RFI 45 due 11-04", "source_url": "https://app.procore.com/p/1/rfis/45"}]})
    text, facts = service._result_payload(USER, job)
    assert text == "The RFI 45 answer is due 11-04."
    assert facts == [{"id": "f1", "text": "RFI 45 due 11-04", "url": "https://app.procore.com/p/1/rfis/45"}]
    spec = registry.get("browser_task_status")
    out = spec.response_trimmer([{"task_id": job, "status": "succeeded", "result": text, "facts": facts}],
                                {"task_id": job, "response_format": "concise", "limit": None},
                                registry.TrimContext(response_format="concise", limit=10, short=str))
    assert out["result"] == text and out["facts"][0]["url"] == "https://app.procore.com/p/1/rfis/45"
    assert contains_tainted(out, spec.taint_fields)
    # Older shapes still read.
    monkeypatch.setattr(records, "unpack_record", lambda *a, **k: {
        "text": "Old text", "facts": [{"text": "x", "url": "https://a.example/b"}]})
    assert service._result_payload(USER, job) == ("Old text", [{"id": "f1", "text": "x", "url": "https://a.example/b"}])
