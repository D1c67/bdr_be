"""AI Estimating routes (routers/plans_reader.py) and its llm_queue job specs.

What these tests pin:
- the gates: every route carries require_plans_reader (PLANS_READER_ENABLED,
  404 before auth while off) and require_internal (the external estimator
  gets 403); every write also takes require_writer (the read-only
  accountant reads everything but gets 403 on writes, chat thread creation
  included); ai_rate_limit on run creation, start, resume, re-render, modify
  and chat send, upload_rate_limit on uploads and report_rate_limit on the
  cost preview;
- each route hands the right arguments to the section 7 service function and
  maps its errors: unknown or malformed ids 404, wrong-state actions 409
  with X-Error-Code ai_estimating_not_actionable, SpendCapReached 409 with
  ai_estimating_spend_cap, an unavailable OpenAI route or queue 503 with
  ai_estimating_unavailable (and nothing is called), and the real
  plans_reader_runs.RunRefused keeping the status it declares;
- every service call the router makes binds to the REAL function's
  signature (fakes are installed with raising=True, so a missing function
  fails too);
- the races: an upload that lands after start, two modify requests (the
  service's claim_change_slot), a delete while a job is still running;
- resume on an over_cap run and on a stalled working run (the service
  decides when a stalled run may be taken over);
- re-render queues a job (202, {group, queue, kind, version}) instead of
  drawing files in the request;
- the upload size cap (413 before the service sees a byte);
- the payload additions the frontend reads: /status's
  promax_max_pages_per_message, the run's `calls` (newest first, capped)
  and per-group `document_pages` (kept when the service already returns
  them), the brief's PDF download, the estimate's cap fields, and chat send
  leaving staleness to the service (the router fails no answer itself);
- audit rows for run_created, run_started, run_deleted, deliverable_modified
  and deliverable_rerendered;
- the four job types: their own claim pass and capacity (never the
  pipeline's LLM slots), claimable only while the flag is on, interactive
  first; each spec.run dispatching to the service; the modify runner's
  group marks, idempotency, base version and never paying twice; the domain
  marks' fences, the monitor retry guard and the advance after a failed
  pipeline group.

Supabase is faked in memory (house convention, see tests/test_llm_queue.py)
and no test reaches OpenAI, storage or the network.
"""

from __future__ import annotations

import importlib
import inspect
import io
import uuid
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import features
from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user, require_internal
from app.core.features import require_plans_reader
from app.core.ratelimit import ai_rate_limit, report_rate_limit, upload_rate_limit
from app.core.roles import Role
from app.routers import plans_reader as pr
from app.services import ai_spend, llm, llm_queue, storage

RUN_ID = "11111111-1111-4111-8111-111111111111"
OTHER_RUN = "22222222-2222-4222-8222-222222222222"
DOC_ID = "33333333-3333-4333-8333-333333333333"
THREAD_ID = "44444444-4444-4444-8444-444444444444"
PROJECT_ID = "55555555-5555-4555-8555-555555555555"
UNKNOWN = "99999999-9999-4999-8999-999999999999"


# ── Fake Supabase ────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db: "FakeDB", table: str):
        self.db, self.table = db, table
        self._op = "select"
        self._payload = None
        self._filters: list[tuple] = []
        self._orders: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def or_(self, expr):
        self._filters.append(("or", expr, None))
        return self

    def order(self, col, desc=False, **_k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **_k):
        self._limit = n
        return self

    def _match(self, row: dict) -> bool:
        for op, col, val in self._filters:
            have = row.get(col) if op != "or" else None
            if op == "eq" and have != val:
                return False
            if op == "neq" and have == val:
                return False
            if op == "in" and have not in val:
                return False
            if op == "gte" and (have is None or str(have) < str(val)):
                return False
            if op == "or":
                hits = False
                for clause in col.split(","):
                    field, _ilike, pattern = clause.split(".", 2)
                    needle = pattern.strip("%").lower()
                    if needle in str(row.get(field) or "").lower():
                        hits = True
                if not hits:
                    return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        self.db.ops.append((self.table, self._op, self._payload, list(self._filters)))
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", str(uuid.uuid4()))
                # The column default (now()): later inserts sort later.
                self.db.clock += 1
                row.setdefault("created_at", f"2026-10-17T12:00:00.{self.db.clock:06d}")
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        hits = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in hits:
                r.update(self._payload)
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "delete":
            self.db.tables[self.table] = [r for r in rows if r not in hits]
            return SimpleNamespace(data=[dict(r) for r in hits])
        for col, desc in reversed(self._orders):
            hits.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        if self._range is not None:
            hits = hits[self._range[0] : self._range[1] + 1]
        if self._limit is not None:
            hits = hits[: self._limit]
        return SimpleNamespace(data=[dict(r) for r in hits])


class FakeDB:
    def __init__(self, tables: dict | None = None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.ops: list[tuple] = []
        self.clock = 0

    def table(self, name: str) -> _Query:
        return _Query(self, name)


def _run(status: str = "created", run_id: str = RUN_ID, **extra) -> dict:
    return {"id": run_id, "name": "Edikted", "status": status, "render_mode": "adaptive", **extra}


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def services():
    """The three service modules the router and the job specs call. The
    real modules, always: a fake must replace a function that exists."""
    mods = {
        name: importlib.import_module(f"app.services.{name}")
        for name in ("plans_reader_runs", "plans_reader_agents", "plans_reader_chat")
    }
    return SimpleNamespace(
        runs=mods["plans_reader_runs"], agents=mods["plans_reader_agents"], chat=mods["plans_reader_chat"]
    )


def _settings(**over) -> Settings:
    base = {
        "plans_reader_enabled": True,
        "full_self_hosted_llms_enabled": False,
        "openai_api_key": "sk-test",
        "llm_queue_enabled": True,
    }
    base.update(over)
    if base["full_self_hosted_llms_enabled"]:
        # Self-hosted mode refuses to boot without a target URL.
        base.setdefault("self_hosted_llm_local_base_url", "http://localhost:11434/v1")
    return Settings(_env_file=None, **base)


def _cap(message: str = "AI Estimating reached its daily spend cap of $75.00.") -> ai_spend.SpendCapReached:
    from decimal import Decimal

    return ai_spend.SpendCapReached(message, cap_usd=Decimal("75"), spent_usd=Decimal("75"))


class Api:
    """TestClient over a bare app holding only this router, with auth and
    rate limits overridden, plus the fakes every test inspects."""

    def __init__(self, monkeypatch, services):
        self.monkeypatch = monkeypatch
        self.svc = services
        self.db = FakeDB()
        self.settings = _settings()
        self.role = Role.ESTIMATING_ADMIN
        self.audits: list[tuple] = []
        self.calls: dict[str, list] = {}
        self.enqueued: list[dict] = []
        monkeypatch.setattr(pr, "get_supabase", lambda: self.db)
        monkeypatch.setattr(pr, "get_settings", lambda: self.settings)
        monkeypatch.setattr(features, "get_settings", lambda: self.settings)
        monkeypatch.setattr(pr, "audit", lambda *a: self.audits.append(a))
        monkeypatch.setattr(
            storage, "signed_url", lambda path, ttl=None, **k: f"https://signed/{path}?dl={k.get('download')}"
        )
        monkeypatch.setattr(ai_spend, "status", lambda sb, now=None: {"cap_usd": 75.0, "spent_usd": 1.5, "blocked": False})
        self.cap_checks: list = []
        monkeypatch.setattr(ai_spend, "check_cap", lambda sb, **k: self.cap_checks.append(k) or 70)

        app = FastAPI()
        app.include_router(pr.router)
        app.dependency_overrides[get_current_user] = self._user
        app.dependency_overrides[ai_rate_limit] = lambda: None
        app.dependency_overrides[upload_rate_limit] = lambda: None
        app.dependency_overrides[report_rate_limit] = lambda: None
        self.client = TestClient(app, raise_server_exceptions=True)

    def _user(self) -> CurrentUser:
        return CurrentUser(id="user-1", email="u@g3.com", role=self.role, is_active=True)

    def fake(self, module: ModuleType, name: str, result=None, exc: Exception | None = None):
        """Patch `module.name` with a recorder returning `result` (or a
        callable of the call) or raising `exc`."""

        def _fn(*args, **kwargs):
            self.calls.setdefault(name, []).append((args, kwargs))
            if exc is not None:
                raise exc
            return result(*args, **kwargs) if callable(result) else result

        self.monkeypatch.setattr(module, name, _fn)  # raising: the function must exist
        return _fn

    def seed(self, table: str, *rows: dict) -> None:
        self.db.tables.setdefault(table, []).extend(dict(r) for r in rows)


@pytest.fixture
def api(monkeypatch, services):
    return Api(monkeypatch, services)


def _code(resp) -> str | None:
    return resp.headers.get("x-error-code")


# ── Gates ────────────────────────────────────────────────────────────────


def test_every_route_carries_the_flag_gate_and_require_internal():
    assert pr.router.routes, "router has no routes"
    for route in pr.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert require_plans_reader in calls, f"{route.path} missing the flag gate"
        assert require_internal in calls, f"{route.path} missing require_internal"


def test_rate_limits_sit_on_the_spending_routes_and_uploads():
    def deps(path: str, method: str) -> set:
        route = next(r for r in pr.router.routes if r.path == path and method in r.methods)
        return {d.call for d in route.dependant.dependencies}

    base = "/ai-estimating/runs"
    for path in (
        base,
        f"{base}/{{run_id}}/start",
        f"{base}/{{run_id}}/resume",
        f"{base}/{{run_id}}/deliverables/{{kind}}/rerender",
        f"{base}/{{run_id}}/deliverables/{{kind}}/modify",
        f"{base}/{{run_id}}/chat/threads/{{thread_id}}/messages",
    ):
        assert ai_rate_limit in deps(path, "POST"), path
    assert upload_rate_limit in deps(f"{base}/{{run_id}}/files", "POST")
    # The preview reads every file.
    assert report_rate_limit in deps(f"{base}/{{run_id}}/estimate", "GET")
    # Reads never spend a model call and are not AI-rate-limited.
    assert ai_rate_limit not in deps(f"{base}/{{run_id}}", "GET")


def test_the_full_route_list_matches_the_contract():
    got = {(sorted(r.methods)[0], r.path) for r in pr.router.routes}
    p = "/ai-estimating"
    want = {
        ("GET", f"{p}/status"),
        ("POST", f"{p}/runs"),
        ("POST", f"{p}/runs/{{run_id}}/files"),
        ("POST", f"{p}/runs/{{run_id}}/project-files"),
        ("GET", f"{p}/runs/{{run_id}}/estimate"),
        ("POST", f"{p}/runs/{{run_id}}/start"),
        ("POST", f"{p}/runs/{{run_id}}/cancel"),
        ("POST", f"{p}/runs/{{run_id}}/resume"),
        ("GET", f"{p}/runs"),
        ("GET", f"{p}/runs/{{run_id}}"),
        ("DELETE", f"{p}/runs/{{run_id}}"),
        ("GET", f"{p}/runs/{{run_id}}/outputs/{{category}}"),
        ("GET", f"{p}/runs/{{run_id}}/documents/{{doc_id}}/download"),
        ("GET", f"{p}/runs/{{run_id}}/deliverables/{{kind}}/{{version}}/download"),
        ("GET", f"{p}/runs/{{run_id}}/deliverables/{{kind}}/{{version}}/preview-url"),
        ("POST", f"{p}/runs/{{run_id}}/deliverables/{{kind}}/rerender"),
        ("POST", f"{p}/runs/{{run_id}}/deliverables/{{kind}}/modify"),
        ("GET", f"{p}/runs/{{run_id}}/chat/threads"),
        ("POST", f"{p}/runs/{{run_id}}/chat/threads"),
        ("GET", f"{p}/runs/{{run_id}}/chat/threads/{{thread_id}}"),
        ("POST", f"{p}/runs/{{run_id}}/chat/threads/{{thread_id}}/messages"),
        ("GET", f"{p}/project-search"),
    }
    assert got == want


def test_routes_404_while_the_flag_is_off(api):
    api.settings = _settings(plans_reader_enabled=False)
    resp = api.client.get("/ai-estimating/status")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"


def test_every_route_carries_the_state_gate():
    from app.core.deps import require_ai_estimating

    for route in pr.router.routes:
        assert require_ai_estimating in {d.call for d in route.dependant.dependencies}, route.path


@pytest.mark.parametrize(
    "state,role,code",
    [
        ("live", Role.ACCOUNTANT, 200),
        ("test", Role.ESTIMATING_ADMIN, 403),
        ("test", Role.ACCOUNTANT, 403),
        ("test", Role.EXECUTIVE, 200),
        ("test", Role.IT_ADMIN, 200),
        ("dev", Role.EXECUTIVE, 403),
        ("dev", Role.IT_ADMIN, 403),
    ],
)
def test_ai_estimating_state_decides_who_gets_in(api, state, role, code):
    api.settings = _settings(ai_estimating_state=state)
    api.role = role
    assert api.client.get("/ai-estimating/runs").status_code == code


def test_the_external_estimator_is_refused(api):
    api.role = Role.ESTIMATOR
    assert api.client.get("/ai-estimating/runs").status_code == 403


def test_the_app_mounts_the_router():
    import app.main

    paths = {getattr(r, "path", "") for r in app.main.app.routes}
    assert "/ai-estimating/runs/{run_id}/start" in paths


# ── Status ───────────────────────────────────────────────────────────────


def test_status_reports_model_limits_and_spend(api):
    resp = api.client.get("/ai-estimating/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["unavailable_reason"] is None
    assert body["model"] == api.settings.openai_plans_model
    assert body["reasoning_effort"] == "medium"
    assert body["render_modes"] == ["adaptive", "tiles_260"]
    assert body["promax_max_pages"] == api.settings.plans_reader_promax_max_pages_per_message
    assert body["promax_max_pages_per_message"] == api.settings.plans_reader_promax_max_pages_per_message
    assert body["upload_max_mb"] == api.settings.upload_max_bytes // (1024 * 1024)
    assert body["spend"]["cap_usd"] == 75.0 and body["spend"]["blocked"] is False


def test_status_stays_live_in_self_hosted_mode(api):
    # The self-hosted switch moves every other feature to the box; AI
    # Estimating keeps running on OpenAI (owner decision 2026-10-18).
    api.settings = _settings(full_self_hosted_llms_enabled=True)
    body = api.client.get("/ai-estimating/status").json()
    assert body["enabled"] is True and body["unavailable_reason"] is None
    assert body["model"] == api.settings.openai_plans_model


# ── Runs: create, files, estimate ────────────────────────────────────────


def test_create_run_passes_the_body_and_audits(api):
    api.fake(api.svc.runs, "create_run", result=lambda sb, **k: _run(render_mode=k["render_mode"]))
    resp = api.client.post("/ai-estimating/runs", json={"name": "  Edikted  ", "source": "upload"})
    assert resp.status_code == 201
    (args, kwargs), = api.calls["create_run"]
    assert args == (api.db,)
    assert kwargs["name"] == "Edikted" and kwargs["source"] == "upload"
    assert kwargs["project_id"] is None and kwargs["render_mode"] == "adaptive"
    assert kwargs["user"].id == "user-1"
    assert api.audits == [
        ("user-1", "ai_estimating.run_created", "plans_reader_run", RUN_ID,
         {"source": "upload", "project_id": None, "render_mode": "adaptive"})
    ]


def test_create_run_from_a_project_validates_the_project_id(api):
    api.fake(api.svc.runs, "create_run", result=_run())
    assert api.client.post("/ai-estimating/runs", json={"name": "x", "source": "project"}).status_code == 422
    bad = api.client.post("/ai-estimating/runs", json={"name": "x", "source": "project", "project_id": "nope"})
    assert bad.status_code == 404
    ok = api.client.post(
        "/ai-estimating/runs",
        json={"name": "x", "source": "project", "project_id": PROJECT_ID, "render_mode": "tiles_260"},
    )
    assert ok.status_code == 201
    kwargs = api.calls["create_run"][-1][1]
    assert kwargs["project_id"] == PROJECT_ID and kwargs["render_mode"] == "tiles_260"
    assert api.client.post("/ai-estimating/runs", json={"name": "   "}).status_code == 422


def test_create_run_service_value_error_is_a_400(api):
    api.fake(api.svc.runs, "create_run", exc=ValueError("That project cannot be read."))
    resp = api.client.post(
        "/ai-estimating/runs", json={"name": "x", "source": "project", "project_id": PROJECT_ID}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "That project cannot be read."
    assert api.audits == []


def _upload(api, content: bytes = b"%PDF-1.7 test", label: str | None = "electrical_drawings", run_id=RUN_ID):
    data = {"label": label} if label is not None else {}
    return api.client.post(
        f"/ai-estimating/runs/{run_id}/files",
        files={"file": ("../E-101 Power.pdf", io.BytesIO(content), "application/pdf")},
        data=data,
    )


def test_upload_hands_bytes_and_label_to_add_upload(api):
    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "add_upload", result=lambda sb, run, **k: {"id": "f1", "label": k["label"]})
    resp = _upload(api)
    assert resp.status_code == 201
    (args, kwargs), = api.calls["add_upload"]
    assert args[1]["id"] == RUN_ID
    assert kwargs["filename"] == "E-101 Power.pdf"  # path segments stripped
    assert kwargs["content"] == b"%PDF-1.7 test"
    assert kwargs["label"] == "electrical_drawings"
    # A blank label means unlabeled (goes to the splitter).
    assert _upload(api, label="").status_code == 201
    assert api.calls["add_upload"][-1][1]["label"] is None


def test_upload_refuses_unknown_labels_started_runs_and_unknown_runs(api):
    api.seed("plans_reader_runs", _run(), _run("reading", run_id=OTHER_RUN))
    api.fake(api.svc.runs, "add_upload", result={"id": "f1"})
    assert _upload(api, label="cables").status_code == 400
    started = _upload(api, run_id=OTHER_RUN)
    assert started.status_code == 409
    assert _code(started) == "ai_estimating_not_actionable"
    assert _upload(api, run_id=UNKNOWN).status_code == 404
    assert _upload(api, run_id="not-a-uuid").status_code == 404
    assert "add_upload" not in api.calls


def test_upload_size_cap_is_enforced_before_the_service(api):
    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "add_upload", result={"id": "f1"})
    api.settings = api.settings.model_copy(update={"upload_max_bytes": 1024})
    resp = _upload(api, content=b"x" * 2048)
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"]
    assert "add_upload" not in api.calls
    assert _upload(api, content=b"x" * 1024).status_code == 201


def test_upload_that_lands_after_start_is_failed_and_refused(api):
    """add_upload checked `created` before a long conversion; start ran in
    between, so the pending row would hold the run in splitting forever."""
    api.seed("plans_reader_runs", _run())

    def _slow_add(sb, run, **k):
        sb.tables["plans_reader_runs"][0]["status"] = "splitting"  # start won meanwhile
        row = {"id": "late", "run_id": run["id"], "status": "pending", "storage_path": "p"}
        sb.tables.setdefault("plans_reader_files", []).append(dict(row))
        return row

    api.fake(api.svc.runs, "add_upload", result=_slow_add)
    resp = _upload(api)
    assert resp.status_code == 409 and _code(resp) == "ai_estimating_not_actionable"
    assert "not added" in resp.json()["detail"]
    late = api.db.tables["plans_reader_files"][0]
    assert late["status"] == "failed" and late["error"] == pr.MSG_LATE_FILE


def test_upload_in_time_is_untouched_and_a_taken_file_is_never_failed(api):
    api.seed("plans_reader_runs", _run())

    def _add(sb, run, **k):
        row = {"id": "f1", "run_id": run["id"], "status": "pending"}
        sb.tables.setdefault("plans_reader_files", []).append(dict(row))
        return row

    api.fake(api.svc.runs, "add_upload", result=_add)
    assert _upload(api).status_code == 201
    assert api.db.tables["plans_reader_files"][0]["status"] == "pending"

    # Start listed the file before its compare-and-set and already marked it.
    def _taken(sb, run, **k):
        sb.tables["plans_reader_runs"][0]["status"] = "splitting"
        row = {"id": "f2", "run_id": run["id"], "status": "labeled"}
        sb.tables["plans_reader_files"].append(dict(row))
        return row

    api.db.tables["plans_reader_runs"][0]["status"] = "created"
    api.fake(api.svc.runs, "add_upload", result=_taken)
    assert _upload(api).status_code == 409
    assert api.db.tables["plans_reader_files"][1]["status"] == "labeled"


def test_project_files_that_land_after_start_are_failed_and_refused(api):
    api.seed("plans_reader_runs", _run())

    def _copy(sb, run, **k):
        sb.tables["plans_reader_runs"][0]["status"] = "splitting"
        rows = [{"id": "c1", "run_id": run["id"], "status": "pending"}]
        sb.tables.setdefault("plans_reader_files", []).extend(dict(r) for r in rows)
        return rows

    api.fake(api.svc.runs, "add_project_files", result=_copy)
    resp = api.client.post(
        f"/ai-estimating/runs/{RUN_ID}/project-files", json={"project_file_ids": [str(uuid.uuid4())]}
    )
    assert resp.status_code == 409
    assert api.db.tables["plans_reader_files"][0]["status"] == "failed"


def test_upload_service_refusal_is_a_400(api):
    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "add_upload", exc=ValueError("Only PDF, xlsx and docx files can be added."))
    resp = _upload(api)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only PDF, xlsx and docx files can be added."


def test_project_files_validates_and_dedupes_ids(api):
    api.seed("plans_reader_runs", _run(), _run("done", run_id=OTHER_RUN))
    api.fake(api.svc.runs, "add_project_files", result=[{"id": "f1"}])
    fid = str(uuid.uuid4())
    ok = api.client.post(f"/ai-estimating/runs/{RUN_ID}/project-files", json={"project_file_ids": [fid, fid.upper()]})
    assert ok.status_code == 201
    assert api.calls["add_project_files"][0][1]["project_file_ids"] == [fid]
    bad = api.client.post(f"/ai-estimating/runs/{RUN_ID}/project-files", json={"project_file_ids": ["x"]})
    assert bad.status_code == 400
    empty = api.client.post(f"/ai-estimating/runs/{RUN_ID}/project-files", json={"project_file_ids": []})
    assert empty.status_code == 422
    late = api.client.post(f"/ai-estimating/runs/{OTHER_RUN}/project-files", json={"project_file_ids": [fid]})
    assert late.status_code == 409


def test_estimate_before_and_after_start(api):
    api.seed("plans_reader_runs", _run(), _run("reading", run_id=OTHER_RUN))
    api.fake(api.svc.runs, "estimate", result=lambda sb, run: {"input_tokens": 1000, "status": run["status"]})
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/estimate").json()["input_tokens"] == 1000
    # After start the service answers with the estimate stored at start.
    after = api.client.get(f"/ai-estimating/runs/{OTHER_RUN}/estimate")
    assert after.status_code == 200 and after.json()["status"] == "reading"
    assert api.client.get(f"/ai-estimating/runs/{UNKNOWN}/estimate").status_code == 404


# ── Runs: start, cancel, resume, delete ──────────────────────────────────


def test_start_happy_path_audits(api):
    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "start", result=lambda sb, run, user: {**run, "status": "splitting"})
    resp = api.client.post(f"/ai-estimating/runs/{RUN_ID}/start")
    assert resp.status_code == 200 and resp.json()["status"] == "splitting"
    assert api.calls["start"][0][1]["user"].id == "user-1"
    assert api.audits[-1][:4] == ("user-1", "ai_estimating.run_started", "plans_reader_run", RUN_ID)


def test_start_refusals(api):
    api.seed("plans_reader_runs", _run(), _run("reading", run_id=OTHER_RUN))
    wrong = api.client.post(f"/ai-estimating/runs/{OTHER_RUN}/start")
    assert wrong.status_code == 409 and _code(wrong) == "ai_estimating_not_actionable"
    assert api.client.post(f"/ai-estimating/runs/{UNKNOWN}/start").status_code == 404

    api.fake(api.svc.runs, "start", exc=_cap())
    cap = api.client.post(f"/ai-estimating/runs/{RUN_ID}/start")
    assert cap.status_code == 409 and _code(cap) == "ai_estimating_spend_cap"
    assert "daily spend cap" in cap.json()["detail"]

    api.fake(api.svc.runs, "start", exc=ValueError("Add at least one file first."))
    empty = api.client.post(f"/ai-estimating/runs/{RUN_ID}/start")
    assert empty.status_code == 409 and _code(empty) == "ai_estimating_not_actionable"
    assert empty.json()["detail"] == "Add at least one file first."
    assert not [a for a in api.audits if a[1] == "ai_estimating.run_started"]


@pytest.mark.parametrize(
    "over,needle",
    [
        # The self-hosted switch alone does not block it; a missing OpenAI key does.
        ({"full_self_hosted_llms_enabled": True, "openai_api_key": ""}, "OPENAI_API_KEY"),
        ({"openai_api_key": ""}, "OPENAI_API_KEY"),
        ({"llm_queue_enabled": False}, "LLM_QUEUE_ENABLED"),
    ],
)
def test_start_is_inert_when_the_model_or_queue_is_unavailable(api, over, needle):
    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "start", result={})
    api.settings = _settings(**over)
    resp = api.client.post(f"/ai-estimating/runs/{RUN_ID}/start")
    assert resp.status_code == 503
    assert _code(resp) == "ai_estimating_unavailable"
    assert needle in resp.json()["detail"]
    assert "start" not in api.calls


def test_cancel_and_resume_state_checks(api):
    api.seed("plans_reader_runs", _run("reading"), _run("done", run_id=OTHER_RUN))
    api.fake(api.svc.runs, "cancel", result=lambda sb, run, user: {**run, "status": "canceled"})
    assert api.client.post(f"/ai-estimating/runs/{RUN_ID}/cancel").json()["status"] == "canceled"
    done = api.client.post(f"/ai-estimating/runs/{OTHER_RUN}/cancel")
    assert done.status_code == 409 and _code(done) == "ai_estimating_not_actionable"

    api.fake(api.svc.runs, "resume", result=lambda sb, run, **k: {**run, "status": "reading"})
    # A finished run is never resumable (the router refuses before the service).
    finished = api.client.post(f"/ai-estimating/runs/{OTHER_RUN}/resume")
    assert finished.status_code == 409 and _code(finished) == "ai_estimating_not_actionable"
    assert "resume" not in api.calls
    api.db.tables["plans_reader_runs"][0]["status"] = "over_cap"
    resumed = api.client.post(f"/ai-estimating/runs/{RUN_ID}/resume")
    assert resumed.status_code == 200 and resumed.json()["status"] == "reading"
    assert api.calls["resume"][-1][1]["user"].id == "user-1"
    api.fake(api.svc.runs, "resume", exc=_cap("still capped"))
    assert _code(api.client.post(f"/ai-estimating/runs/{RUN_ID}/resume")) == "ai_estimating_spend_cap"


def test_delete_refuses_a_working_run_then_audits(api):
    api.seed("plans_reader_runs", _run("reading"), _run("canceled", run_id=OTHER_RUN))
    api.fake(api.svc.runs, "delete", result=None)
    working = api.client.delete(f"/ai-estimating/runs/{RUN_ID}")
    assert working.status_code == 409 and _code(working) == "ai_estimating_not_actionable"
    assert "delete" not in api.calls
    gone = api.client.delete(f"/ai-estimating/runs/{OTHER_RUN}")
    assert gone.status_code == 204
    assert api.calls["delete"][0][0][1]["id"] == OTHER_RUN
    assert api.audits[-1][1] == "ai_estimating.run_deleted"
    assert api.client.delete(f"/ai-estimating/runs/{UNKNOWN}").status_code == 404


def test_delete_refuses_while_a_job_is_still_writing(api):
    api.seed("plans_reader_runs", _run("over_cap"), _run("done", run_id=OTHER_RUN))
    api.fake(api.svc.runs, "delete", result=None)
    # An over_cap run whose in-flight group is still running.
    api.seed("plans_reader_groups", {"id": "g1", "run_id": RUN_ID, "kind": "sub", "status": "running"})
    busy = api.client.delete(f"/ai-estimating/runs/{RUN_ID}")
    assert busy.status_code == 409 and _code(busy) == "ai_estimating_not_actionable"
    assert busy.json()["detail"] == pr.MSG_DELETE_BUSY
    # A finished run with a chat answer still pending.
    api.seed("plans_reader_chat_threads", {"id": THREAD_ID, "run_id": OTHER_RUN})
    api.seed("plans_reader_chat_messages", {"id": "m1", "thread_id": THREAD_ID, "role": "assistant", "status": "pending"})
    assert api.client.delete(f"/ai-estimating/runs/{OTHER_RUN}").status_code == 409
    assert "delete" not in api.calls
    # Once both settle, delete goes ahead.
    api.db.tables["plans_reader_groups"][0]["status"] = "failed"
    api.db.tables["plans_reader_chat_messages"][0]["status"] = "done"
    assert api.client.delete(f"/ai-estimating/runs/{RUN_ID}").status_code == 204
    assert api.client.delete(f"/ai-estimating/runs/{OTHER_RUN}").status_code == 204


def test_delete_storage_failure_keeps_its_503(api):
    api.seed("plans_reader_runs", _run("canceled"))
    api.fake(
        api.svc.runs,
        "delete",
        exc=api.svc.runs.RunRefused("The run's files could not be removed right now. Try again in a minute.", 503),
    )
    resp = api.client.delete(f"/ai-estimating/runs/{RUN_ID}")
    assert resp.status_code == 503 and _code(resp) is None
    assert not [a for a in api.audits if a[1] == "ai_estimating.run_deleted"]


def test_list_and_get_runs(api):
    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "list_runs", result=lambda sb, **k: {"runs": [], "total": 0, "args": k})
    body = api.client.get("/ai-estimating/runs?status=done&q=%20edik%20&limit=10&offset=5").json()
    assert body["args"] == {"status": "done", "q": "edik", "limit": 10, "offset": 5}
    assert api.client.get("/ai-estimating/runs?status=bogus").status_code == 400
    assert api.client.get("/ai-estimating/runs?limit=0").status_code == 422

    api.fake(api.svc.runs, "get_run_payload", result=lambda sb, rid: {"run": {"id": rid}, "files": []})
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}").json()["run"]["id"] == RUN_ID
    assert api.client.get(f"/ai-estimating/runs/{UNKNOWN}").status_code == 404
    assert api.client.get("/ai-estimating/runs/not-a-uuid").status_code == 404


# ── Outputs, documents, deliverables ─────────────────────────────────────


def test_outputs_return_the_latest_version(api):
    api.seed("plans_reader_runs", _run("done"))
    api.seed(
        "plans_reader_outputs",
        {"run_id": RUN_ID, "scope": "main", "category": None, "version": 1, "output": {"v": 1}},
        {"run_id": RUN_ID, "scope": "main", "category": None, "version": 2, "output": {"v": 2}},
        {"run_id": RUN_ID, "scope": "category", "category": "electrical_drawings", "version": 1, "output": {"e": 1}},
    )
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/outputs/main").json()["output"] == {"v": 2}
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/outputs/electrical_drawings").json()["output"] == {"e": 1}
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/outputs/specifications").status_code == 404
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/outputs/secrets").status_code == 404


def test_document_download_signs_the_run_s_own_document(api):
    api.seed("plans_reader_runs", _run("done"), _run("done", run_id=OTHER_RUN))
    api.seed(
        "plans_reader_documents",
        {"id": DOC_ID, "run_id": RUN_ID, "name": "Electrical Drawings", "storage_path": "plans-reader/r/seg.pdf"},
    )
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/documents/{DOC_ID}/download").json()
    assert body["url"] == "https://signed/plans-reader/r/seg.pdf?dl=Electrical Drawings.pdf"
    assert api.client.get(f"/ai-estimating/runs/{OTHER_RUN}/documents/{DOC_ID}/download").status_code == 404
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/documents/x/download").status_code == 404


def _deliverable(version: int = 1, kind: str = "boq") -> dict:
    return {
        "id": f"d{version}",
        "run_id": RUN_ID,
        "kind": kind,
        "version": version,
        "filename": "BOQ - COUNTS.xlsx",
        "storage_path": f"plans-reader/{RUN_ID}/deliverables/v{version}/boq.xlsx",
        "spec_json": {},
    }


def test_deliverable_download(api):
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_deliverables", _deliverable(1), _deliverable(2))
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/2/download").json()
    assert body["url"].endswith("/v2/boq.xlsx?dl=BOQ - COUNTS.xlsx")
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/3/download").status_code == 404
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/deliverables/pricing/1/download").status_code == 404


def _rerender_result(kind: str = "boq", version: int = 1) -> dict:
    """The shape plans_reader_runs.rerender_deliverable returns."""
    return {
        "group": {"id": "g-rr", "run_id": RUN_ID, "kind": "modify", "status": "queued", "llm_job_id": "job-rr"},
        "queue": {"state": "queued", "position": 1},
        "kind": kind,
        "version": version,
    }


def test_rerender_recovers_a_deliverable_that_never_rendered_and_audits(api):
    api.seed("plans_reader_runs", _run("done_with_errors"))
    api.fake(api.svc.runs, "rerender_deliverable", result=lambda sb, run, kind, **k: _rerender_result(kind, 1))
    # No deliverable row yet (the renderer failed the first time): the
    # service queues a render from the main output instead of the router
    # refusing. Nothing is drawn in the request: 202 with the queued group.
    resp = api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/rerender")
    assert resp.status_code == 202 and resp.json() == _rerender_result("boq", 1)
    (args, kwargs), = api.calls["rerender_deliverable"]
    assert args[1]["id"] == RUN_ID and args[2] == "boq" and kwargs["user"].id == "user-1"
    assert api.audits[-1] == (
        "user-1",
        "ai_estimating.deliverable_rerendered",
        "plans_reader_run",
        RUN_ID,
        {"kind": "boq", "version": 1, "group_id": "g-rr"},
    )
    assert api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/pricing/rerender").status_code == 404


def test_rerender_refusals_keep_the_service_status(api):
    api.seed("plans_reader_runs", _run("done"))
    api.fake(
        api.svc.runs,
        "rerender_deliverable",
        exc=api.svc.runs.RunRefused("This run has nothing to render for that deliverable.", 404),
    )
    nothing = api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/rerender")
    assert nothing.status_code == 404
    assert nothing.json()["detail"] == "This run has nothing to render for that deliverable."
    api.db.tables["plans_reader_runs"][0]["status"] = "rendering"
    busy = api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/rerender")
    assert busy.status_code == 409 and _code(busy) == "ai_estimating_not_actionable"
    assert len(api.calls["rerender_deliverable"]) == 1
    assert not [a for a in api.audits if a[1] == "ai_estimating.deliverable_rerendered"]


def test_rerender_through_the_real_service_queues_a_change_group(api, monkeypatch):
    """The real rerender_deliverable behind the route: it claims the run's
    change slot (a `modify` group), queues a plans_reader_modify job with
    `rerender` true and answers {group, queue, kind, version}; a second
    request while that change is active is a 409."""
    runs = api.svc.runs
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_deliverables", {**_deliverable(2), "spec_json": {"rows": []}})
    monkeypatch.setattr(runs, "get_settings", lambda: api.settings)

    def _enqueue(job_type, **kw):
        api.enqueued.append({"job_type": job_type, **kw})
        return {"id": "job-rr", "status": "queued"}

    monkeypatch.setattr(llm_queue, "enqueue", _enqueue)
    monkeypatch.setattr(llm_queue, "poll_info", lambda jt, target: {"state": "queued", "position": 1})
    url = f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/rerender"
    resp = api.client.post(url)
    assert resp.status_code == 202
    body = resp.json()
    assert body["kind"] == "boq" and body["version"] == 3 and body["queue"]["state"] == "queued"
    (group,) = api.db.tables["plans_reader_groups"]
    assert group["kind"] == "modify" and group["llm_job_id"] == "job-rr" and body["group"]["id"] == group["id"]
    (job,) = api.enqueued
    assert job["job_type"] == runs.JOB_MODIFY
    assert job["payload"]["rerender"] is True and job["payload"]["instruction"] is None
    assert job["payload"]["base_version"] == 2
    # One change per run at a time: a second request is refused and leaves no row.
    again = api.client.post(url)
    assert again.status_code == 409 and _code(again) == "ai_estimating_not_actionable"
    assert len(api.db.tables["plans_reader_groups"]) == 1 and len(api.enqueued) == 1


def test_modify_queues_a_modify_group_and_audits(api, monkeypatch):
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_deliverables", _deliverable(1))

    def _enqueue(job_type, **kw):
        api.enqueued.append({"job_type": job_type, **kw})
        return {"id": "job-1", "status": "queued"}

    monkeypatch.setattr(llm_queue, "enqueue", _enqueue)
    monkeypatch.setattr(llm_queue, "poll_info", lambda jt, target: {"state": "queued", "position": 1})
    resp = api.client.post(
        f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify", json={"instruction": " Add 4 GFCI receptacles "}
    )
    assert resp.status_code == 202
    group = api.db.tables["plans_reader_groups"][0]
    assert group["kind"] == "modify" and group["run_id"] == RUN_ID and group["llm_job_id"] == "job-1"
    (job,) = api.enqueued
    assert job["job_type"] == "plans_reader_modify" and job["target_id"] == group["id"]
    assert job["payload"] == {
        "group_id": group["id"], "run_id": RUN_ID, "kind": "boq",
        "instruction": "Add 4 GFCI receptacles", "user_id": "user-1", "base_version": 1,
    }
    assert resp.json()["queue"]["state"] == "queued"
    assert api.audits[-1][1] == "ai_estimating.deliverable_modified"
    assert api.cap_checks == [{}]  # the cap is checked before anything is queued

    # A second modify while the first is queued is refused, and its own
    # group row is withdrawn.
    again = api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify", json={"instruction": "x"})
    assert again.status_code == 409 and _code(again) == "ai_estimating_not_actionable"
    assert [g["id"] for g in api.db.tables["plans_reader_groups"]] == [group["id"]]
    assert len(api.enqueued) == 1


def test_modify_race_keeps_only_the_oldest_request(api, monkeypatch):
    """Both racers insert; each reads the active modify groups back and only
    the oldest survives. Simulated by a rival row that committed first but
    that this request's (absent) pre-check could not have seen."""
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_deliverables", _deliverable(1))
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("the loser must not enqueue"))
    real_insert = _Query.insert

    def _racing_insert(self, payload):
        if self.table == "plans_reader_groups":
            self.db.tables.setdefault("plans_reader_groups", []).append(
                {"id": "rival", "run_id": RUN_ID, "kind": "modify", "status": "queued", "created_at": "2026-10-17T10:00:00"}
            )
            payload = {**payload, "created_at": "2026-10-17T10:00:01"}
        return real_insert(self, payload)

    monkeypatch.setattr(_Query, "insert", _racing_insert)
    resp = api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify", json={"instruction": "x"})
    assert resp.status_code == 409 and _code(resp) == "ai_estimating_not_actionable"
    assert [g["id"] for g in api.db.tables["plans_reader_groups"]] == ["rival"]


def test_modify_unique_violation_is_the_same_refusal(api, monkeypatch):
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_deliverables", _deliverable(1))
    real_execute = _Query.execute

    def _dup(self):
        if self.table == "plans_reader_groups" and self._op == "insert":
            raise RuntimeError('duplicate key value violates unique constraint "plans_reader_groups_modify_uq" (23505)')
        return real_execute(self)

    monkeypatch.setattr(_Query, "execute", _dup)
    resp = api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify", json={"instruction": "x"})
    assert resp.status_code == 409 and _code(resp) == "ai_estimating_not_actionable"


def test_modify_refuses_at_the_spend_cap_before_queueing(api, monkeypatch):
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_deliverables", _deliverable(1))
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("must not enqueue"))

    def _capped(sb, **k):
        raise _cap()

    monkeypatch.setattr(ai_spend, "check_cap", _capped)
    resp = api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify", json={"instruction": "x"})
    assert resp.status_code == 409 and _code(resp) == "ai_estimating_spend_cap"
    assert not api.db.tables.get("plans_reader_groups")


def test_modify_refusals(api, monkeypatch):
    api.seed("plans_reader_runs", _run("reading"))
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("must not enqueue"))
    url = f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify"
    assert api.client.post(url, json={"instruction": "x"}).status_code == 409
    api.db.tables["plans_reader_runs"][0]["status"] = "done"
    assert api.client.post(url, json={"instruction": "x"}).status_code == 404  # no deliverable yet
    api.seed("plans_reader_deliverables", _deliverable(1))
    assert api.client.post(url, json={"instruction": "   "}).status_code == 422
    assert api.client.post(url, json={"instruction": "x" * 4001}).status_code == 422
    api.settings = _settings(openai_api_key="")
    off = api.client.post(url, json={"instruction": "x"})
    assert off.status_code == 503 and _code(off) == "ai_estimating_unavailable"
    assert not api.db.tables.get("plans_reader_groups")


def test_modify_drops_the_group_when_the_enqueue_fails(api, monkeypatch):
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_deliverables", _deliverable(1))

    def _boom(*a, **k):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(llm_queue, "enqueue", _boom)
    with pytest.raises(RuntimeError):
        api.client.post(f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify", json={"instruction": "x"})
    assert api.db.tables["plans_reader_groups"] == []


# ── Chat ─────────────────────────────────────────────────────────────────


def test_chat_threads_create_list_and_get(api):
    api.seed("plans_reader_runs", _run("done"), _run("done", run_id=OTHER_RUN))
    api.seed("plans_reader_chat_threads", {"id": THREAD_ID, "run_id": RUN_ID, "title": "Panels"})
    api.fake(api.svc.chat, "create_thread", result=lambda sb, run, **k: {"id": "t2", "title": k["title"]})
    created = api.client.post(f"/ai-estimating/runs/{RUN_ID}/chat/threads", json={"title": "  Feeders "})
    assert created.status_code == 201 and created.json()["title"] == "Feeders"
    assert api.calls["create_thread"][0][1]["user"].id == "user-1"
    assert api.client.post(f"/ai-estimating/runs/{RUN_ID}/chat/threads", json={}).status_code == 201

    api.fake(api.svc.chat, "list_threads", result=lambda sb, rid: [{"id": THREAD_ID, "run_id": rid}])
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/chat/threads").json()[0]["run_id"] == RUN_ID

    api.fake(api.svc.chat, "get_thread", result=lambda sb, tid: {"thread": {"id": tid}, "messages": []})
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/chat/threads/{THREAD_ID}").json()["thread"]["id"] == THREAD_ID
    # A thread of another run is not found through this run.
    assert api.client.get(f"/ai-estimating/runs/{OTHER_RUN}/chat/threads/{THREAD_ID}").status_code == 404
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/chat/threads/nope").status_code == 404


def test_chat_send_passes_content_and_pro_max(api):
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_chat_threads", {"id": THREAD_ID, "run_id": RUN_ID})
    api.fake(
        api.svc.chat,
        "send_message",
        result=lambda sb, run, thread, **k: {"user_message": {"content": k["content"]}, "assistant_message": {"status": "pending"}},
    )
    url = f"/ai-estimating/runs/{RUN_ID}/chat/threads/{THREAD_ID}/messages"
    resp = api.client.post(url, json={"content": " How many panels? ", "pro_max": True})
    assert resp.status_code == 202
    (args, kwargs), = api.calls["send_message"]
    assert args[1]["id"] == RUN_ID and args[2]["id"] == THREAD_ID
    assert kwargs["content"] == "How many panels?" and kwargs["pro_max"] is True
    assert api.client.post(url, json={"content": ""}).status_code == 422
    assert api.client.post(url, json={"content": "x" * 8001}).status_code == 422
    assert api.client.post(url, json={"content": "x" * 8000}).status_code == 202


def test_chat_send_refusals(api):
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_chat_threads", {"id": THREAD_ID, "run_id": RUN_ID})
    api.fake(api.svc.chat, "send_message", result={})
    url = f"/ai-estimating/runs/{RUN_ID}/chat/threads/{THREAD_ID}/messages"
    # The busy refusal is the service's (BUSY_MESSAGE), not a router check.
    busy_text = api.svc.chat.BUSY_MESSAGE
    api.fake(api.svc.chat, "send_message", exc=ValueError(busy_text))
    busy = api.client.post(url, json={"content": "next"})
    assert busy.status_code == 409 and _code(busy) == "ai_estimating_not_actionable"
    assert busy.json()["detail"] == busy_text
    api.fake(api.svc.chat, "send_message", result={})
    api.settings = _settings(openai_api_key="")
    assert api.client.post(url, json={"content": "next"}).status_code == 503
    # Chat answers on a thread when the queue is off, so the queue is not required.
    api.settings = _settings(llm_queue_enabled=False)
    assert api.client.post(url, json={"content": "next"}).status_code == 202
    api.fake(api.svc.chat, "send_message", exc=ValueError("This chat thread belongs to a different run."))
    refused = api.client.post(url, json={"content": "next"})
    assert refused.status_code == 409 and refused.json()["detail"] == "This chat thread belongs to a different run."


# ── Project picker ───────────────────────────────────────────────────────


def test_project_search_scopes_projects_and_files(api):
    api.seed(
        "projects",
        {"id": PROJECT_ID, "number": "26.10.7267", "name": "YMCA Torrey Pine", "current_stage": "bidding", "created_at": "2026-10-02"},
        {"id": "p2", "number": "26.10.7268", "name": "YMCA PM job", "current_stage": "pm_only", "created_at": "2026-10-03"},
        {"id": "p3", "number": "26.10.7269", "name": "Hard Rock EV", "current_stage": "intake", "created_at": "2026-10-04"},
    )
    api.seed(
        "project_files",
        {"id": "f1", "project_id": PROJECT_ID, "category": "electrical_drawing", "filename": "E.pdf", "size_bytes": 10, "created_at": "1"},
        {"id": "f2", "project_id": PROJECT_ID, "category": "boq", "filename": "BOQ.xlsx", "size_bytes": 5, "created_at": "2"},
        {"id": "f3", "project_id": PROJECT_ID, "category": "specification", "filename": "Spec.pdf", "size_bytes": 7, "created_at": "3"},
        # Categories the service has no label for, and unreadable types.
        {"id": "f4", "project_id": PROJECT_ID, "category": "additional", "filename": "A.pdf", "size_bytes": 1, "created_at": "4"},
        {"id": "f5", "project_id": PROJECT_ID, "category": "estimator_additional", "filename": "B.pdf", "size_bytes": 1, "created_at": "5"},
        {"id": "f6", "project_id": PROJECT_ID, "category": "drawing", "filename": "Site.dwg", "size_bytes": 1, "created_at": "6"},
    )
    body = api.client.get("/ai-estimating/project-search?q=ymca").json()
    assert [p["id"] for p in body] == [PROJECT_ID]
    assert body[0] == {
        "id": PROJECT_ID, "number": "26.10.7267", "name": "YMCA Torrey Pine", "stage": "bidding",
        "files": [
            {"id": "f1", "category": "electrical_drawing", "filename": "E.pdf", "size_bytes": 10},
            {"id": "f3", "category": "specification", "filename": "Spec.pdf", "size_bytes": 7},
        ],
    }
    # Number search, and a blank query lists newest first.
    assert [p["id"] for p in api.client.get("/ai-estimating/project-search?q=7269").json()] == ["p3"]
    assert [p["id"] for p in api.client.get("/ai-estimating/project-search").json()] == ["p3", PROJECT_ID]
    # PostgREST syntax and wildcards are stripped from the term.
    or_filters = [f for op in api.db.ops for f in op[3] if f[0] == "or"]
    api.client.get("/ai-estimating/project-search?q=a,b(%25)")
    last = [f for op in api.db.ops for f in op[3] if f[0] == "or"][len(or_filters):]
    assert last and "," not in last[-1][1].split(",")[0].split(".", 2)[2]
    assert "(" not in last[-1][1] and "%%" not in last[-1][1]
    # The category allow list is applied in SQL, from the service's own map.
    file_reads = [op for op in api.db.ops if op[0] == "project_files"]
    cats = next(f[2] for f in file_reads[0][3] if f[0] == "in" and f[1] == "category")
    assert set(cats) == set(api.svc.runs.PROJECT_CATEGORY_LABELS)


def test_project_search_pages_through_project_files(api, monkeypatch):
    monkeypatch.setattr(pr, "_PICKER_PAGE", 2)
    api.seed("projects", {"id": PROJECT_ID, "number": "1", "name": "Big", "current_stage": "bidding", "created_at": "1"})
    api.seed(
        "project_files",
        *[
            {"id": f"f{i}", "project_id": PROJECT_ID, "category": "drawing", "filename": f"{i}.pdf", "created_at": f"{i:02d}"}
            for i in range(5)
        ],
    )
    body = api.client.get("/ai-estimating/project-search").json()
    assert [f["id"] for f in body[0]["files"]] == ["f0", "f1", "f2", "f3", "f4"]
    assert len([op for op in api.db.ops if op[0] == "project_files"]) == 3


# ── llm_queue job specs ──────────────────────────────────────────────────


PLANS_TYPES = {
    "plans_reader_agent": "plans_sub",
    "plans_reader_main": "plans_main",
    "plans_reader_chat": "plans_chat",
    "plans_reader_modify": "plans_modify",
}


def test_plans_job_types_are_registered_outside_the_pipeline_llm_pass():
    for job_type, feature in PLANS_TYPES.items():
        assert job_type not in llm_queue.LLM_JOB_TYPES
        assert job_type not in llm_queue.NON_LLM_JOB_TYPES
        assert job_type in llm_queue.ALL_JOB_TYPES
        assert llm_queue._FEATURE_BY_TYPE[job_type] == feature
        assert llm_queue._spec(job_type).feature == feature
    assert set(llm_queue.PLANS_JOB_TYPES) == set(PLANS_TYPES)
    assert set(llm_queue.PLANS_INTERACTIVE_JOB_TYPES) == {"plans_reader_chat", "plans_reader_modify"}
    assert llm_queue.JOB_PLANS_READER_CHAT == llm_queue.JOB_PLANS_CHAT == "plans_reader_chat"


def test_the_0152_job_type_check_is_exactly_every_job_type():
    import re
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "supabase/migrations/0152_plans_reader.sql").read_text()
    block = re.search(r"llm_jobs_job_type_check\s+check \(job_type in \(([^)]*)\)\)", sql)
    assert block, "0152 must redefine the llm_jobs job_type CHECK"
    values = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert values == set(llm_queue.ALL_JOB_TYPES)


class _ClaimRecorder:
    """Stands in for llm_queue._claim: records (max_jobs, types) and hands
    back up to max_jobs fake jobs from a per-type backlog."""

    def __init__(self, backlog: dict[str, int] | None = None):
        self.calls: list[tuple[int, list[str]]] = []
        self.backlog = dict(backlog or {})

    def __call__(self, s, max_jobs, types=None):
        self.calls.append((max_jobs, list(types or [])))
        out = []
        for jt in types or []:
            while self.backlog.get(jt, 0) > 0 and len(out) < max_jobs:
                self.backlog[jt] -= 1
                out.append({"id": str(uuid.uuid4()), "job_type": jt})
        return out


def _plans_calls(rec: _ClaimRecorder) -> list[tuple[int, list[str]]]:
    return [c for c in rec.calls if set(c[1]) & set(PLANS_TYPES)]


def test_plans_jobs_never_ride_the_pipeline_llm_pass(monkeypatch):
    rec = _ClaimRecorder()
    monkeypatch.setattr(llm_queue, "_claim", rec)
    llm_queue._claim_tick(_settings(rfp_ingest_enabled=False), [])
    llm_call = next(c for c in rec.calls if "boq_extraction" in c[1])
    assert not set(llm_call[1]) & set(PLANS_TYPES)
    # Running plans jobs never use up the pipeline's slots.
    rec.calls.clear()
    llm_queue._claim_tick(
        _settings(rfp_ingest_enabled=False, llm_queue_worker_concurrency=3), ["plans_reader_agent"] * 2
    )
    assert next(c for c in rec.calls if "boq_extraction" in c[1])[0] == 3


def test_plans_pass_is_skipped_while_the_flag_is_off(monkeypatch):
    rec = _ClaimRecorder({"plans_reader_agent": 5})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    assert llm_queue._claim_tick(_settings(plans_reader_enabled=False, rfp_ingest_enabled=False), []) == []
    assert _plans_calls(rec) == []


def test_plans_pass_claims_interactive_first_and_keeps_a_slot_free(monkeypatch):
    s = _settings(rfp_ingest_enabled=False)
    total = llm_queue._plans_concurrency(s)
    assert total == llm_queue.PLANS_DEFAULT_CONCURRENCY == 2
    # A full backlog of groups: batch jobs take every slot but one.
    rec = _ClaimRecorder({"plans_reader_agent": 50})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    got = llm_queue._claim_tick(s, [])
    assert [j["job_type"] for j in got] == ["plans_reader_agent"]
    assert _plans_calls(rec) == [
        (2, list(llm_queue.PLANS_INTERACTIVE_JOB_TYPES)),
        (1, list(llm_queue.PLANS_BATCH_JOB_TYPES)),
    ]
    # With one group running, the batch pass is not even called; a chat
    # waiting in the backlog takes the free slot.
    rec = _ClaimRecorder({"plans_reader_agent": 50, "plans_reader_chat": 1})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    got = llm_queue._claim_tick(s, ["plans_reader_agent"])
    assert [j["job_type"] for j in got] == ["plans_reader_chat"]
    assert _plans_calls(rec) == [(1, list(llm_queue.PLANS_INTERACTIVE_JOB_TYPES))]
    # At capacity the pass makes no call at all.
    rec = _ClaimRecorder({"plans_reader_chat": 1})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    assert llm_queue._claim_tick(s, ["plans_reader_agent", "plans_reader_chat"]) == []
    assert _plans_calls(rec) == []


def test_plans_concurrency_setting_and_single_slot(monkeypatch):
    one = _settings(rfp_ingest_enabled=False).model_copy(update={"plans_reader_concurrency": 1})
    assert llm_queue._plans_concurrency(one) == 1
    rec = _ClaimRecorder({"plans_reader_main": 1})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    # One slot: batch work may use it (otherwise a run could never progress).
    assert [j["job_type"] for j in llm_queue._claim_tick(one, [])] == ["plans_reader_main"]
    bad = _settings().model_copy(update={"plans_reader_concurrency": "x"})
    assert llm_queue._plans_concurrency(bad) == llm_queue.PLANS_DEFAULT_CONCURRENCY


@pytest.fixture
def qdb(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    return db


def test_agent_and_main_specs_run_the_group(services, qdb, monkeypatch):
    seen = []
    monkeypatch.setattr(services.agents, "run_group", lambda sb, gid: seen.append((sb, gid)))
    llm_queue._spec("plans_reader_agent").run({"group_id": "g1", "run_id": RUN_ID})
    llm_queue._spec("plans_reader_main").run({"group_id": "g2", "run_id": RUN_ID})
    assert seen == [(qdb, "g1"), (qdb, "g2")]
    with pytest.raises(ValueError):
        llm_queue._spec("plans_reader_agent").run({})


def test_chat_spec_runs_the_message(services, qdb, monkeypatch):
    seen = []
    monkeypatch.setattr(services.chat, "run_message", lambda sb, mid: seen.append((sb, mid)))
    llm_queue._spec("plans_reader_chat").run({"message_id": "m1"})
    assert seen == [(qdb, "m1")]


def _modify_payload(**over) -> dict:
    payload = {
        "group_id": "g1", "run_id": RUN_ID, "kind": "boq", "instruction": "Add 4 GFCI",
        "user_id": "user-1", "base_version": 1,
    }
    payload.update(over)
    return payload


def _modify_env(qdb, *, group: dict | None = None, versions: tuple[int, ...] = (1,)):
    qdb.tables["plans_reader_runs"] = [_run("done")]
    qdb.tables["plans_reader_groups"] = [
        {"id": "g1", "run_id": RUN_ID, "kind": "modify", "status": "queued", "attempts": 0, **(group or {})}
    ]
    qdb.tables["plans_reader_deliverables"] = [_deliverable(v) for v in versions]
    qdb.tables["plans_reader_calls"] = []


def _paid_call(qdb, ok: bool = True) -> None:
    qdb.tables["plans_reader_calls"].append({"id": str(uuid.uuid4()), "group_id": "g1", "ok": ok})


def test_modify_spec_writes_a_version_and_marks_the_group(services, qdb, monkeypatch):
    _modify_env(qdb)
    seen = []

    def _modify(sb, run, *, kind, instruction, user, group_id=None, thread_id=None):
        seen.append((run["id"], kind, instruction, user.id, str(user), group_id))
        _paid_call(qdb)
        return {"id": "d2", "version": 2}

    monkeypatch.setattr(services.agents, "modify_deliverable", _modify)
    llm_queue._spec("plans_reader_modify").run(_modify_payload())
    assert seen == [(RUN_ID, "boq", "Add 4 GFCI", "user-1", "user-1", "g1")]
    group = qdb.tables["plans_reader_groups"][0]
    assert group["status"] == "done" and group["attempts"] == 1
    assert group["output"] == {"kind": "boq", "deliverable_id": "d2", "version": 2}
    assert group["started_at"] and group["finished_at"]

    # A requeued job after success writes nothing new (idempotent).
    llm_queue._spec("plans_reader_modify").run(_modify_payload())
    assert len(seen) == 1


def test_modify_spec_refuses_when_another_version_landed_first(services, qdb, monkeypatch):
    _modify_env(qdb, versions=(1, 2))
    monkeypatch.setattr(services.agents, "modify_deliverable", lambda *a, **k: pytest.fail("must not call the model"))
    with pytest.raises(ValueError) as exc:
        llm_queue._spec("plans_reader_modify").run(_modify_payload(base_version=1))
    assert str(exc.value) == llm_queue.MSG_MODIFY_STALE
    assert qdb.tables["plans_reader_groups"][0]["status"] == "queued"


def test_modify_spec_never_pays_twice_after_a_failure_past_the_model(services, qdb, monkeypatch):
    from app.services import llm_errors

    _modify_env(qdb)

    def _upload_fails(sb, run, **k):
        _paid_call(qdb)  # the model answered and was billed
        raise RuntimeError("storage upload timed out")

    monkeypatch.setattr(services.agents, "modify_deliverable", _upload_fails)
    with pytest.raises(ValueError) as exc:
        llm_queue._spec("plans_reader_modify").run(_modify_payload())
    assert str(exc.value) == llm_queue.MSG_MODIFY_UNSAVED
    assert not llm_errors.is_transient_kind(llm_errors.classify(exc.value))


def test_modify_spec_lets_the_queue_retry_when_the_model_was_never_reached(services, qdb, monkeypatch):
    _modify_env(qdb)

    def _down(sb, run, **k):
        _paid_call(qdb, ok=False)
        raise TimeoutError("connect timeout")

    monkeypatch.setattr(services.agents, "modify_deliverable", _down)
    with pytest.raises(TimeoutError):
        llm_queue._spec("plans_reader_modify").run(_modify_payload())


def test_modify_spec_retries_an_unusable_answer_once_accounted(services, qdb, monkeypatch):
    _modify_env(qdb)
    answers = iter([llm.LlmBadOutput("The AI model returned an unusable deliverable."), {"id": "d2", "version": 2}])

    def _modify(sb, run, **k):
        _paid_call(qdb)
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(services.agents, "modify_deliverable", _modify)
    with pytest.raises(llm.LlmBadOutput):
        llm_queue._spec("plans_reader_modify").run(_modify_payload())
    group = qdb.tables["plans_reader_groups"][0]
    assert group["output"] == {"retry_ok_calls": 1}
    group["status"] = "queued"  # the queue's pending mark
    llm_queue._spec("plans_reader_modify").run(_modify_payload())
    assert qdb.tables["plans_reader_groups"][0]["status"] == "done"
    assert qdb.tables["plans_reader_groups"][0]["output"]["version"] == 2


def test_modify_spec_retry_after_a_lost_attempt_finishes_or_refuses(services, qdb, monkeypatch):
    monkeypatch.setattr(services.agents, "modify_deliverable", lambda *a, **k: pytest.fail("must not pay again"))
    # The earlier attempt saved v2 but its done mark never landed.
    _modify_env(qdb, group={"status": "running", "started_at": "2026-10-17T10:00:00"}, versions=(1,))
    qdb.tables["plans_reader_deliverables"].append(
        {**_deliverable(2), "instruction": "Add 4 GFCI", "created_at": "2026-10-17T10:01:00"}
    )
    _paid_call(qdb)
    llm_queue._spec("plans_reader_modify").run(_modify_payload())
    group = qdb.tables["plans_reader_groups"][0]
    assert group["status"] == "done" and group["output"]["version"] == 2

    # The earlier attempt was paid and saved nothing: refuse, never re-pay.
    _modify_env(qdb, group={"status": "queued", "started_at": "2026-10-17T10:00:00"})
    _paid_call(qdb)
    with pytest.raises(ValueError) as exc:
        llm_queue._spec("plans_reader_modify").run(_modify_payload())
    assert str(exc.value) == llm_queue.MSG_MODIFY_UNSAVED


def test_modify_spec_turns_the_spend_cap_into_a_permanent_error(services, qdb, monkeypatch):
    from app.services import llm_errors

    _modify_env(qdb)

    def _capped(*a, **k):
        raise _cap()

    monkeypatch.setattr(services.agents, "modify_deliverable", _capped)
    with pytest.raises(ValueError) as exc:
        llm_queue._spec("plans_reader_modify").run(_modify_payload())
    assert llm_errors.classify(exc.value) == "bad_input"
    assert "daily spend cap" in llm_errors.user_message(exc.value, "gpt-6-astra")
    with pytest.raises(ValueError):
        llm_queue._spec("plans_reader_modify").run(_modify_payload(instruction=""))


def test_group_mark_maps_pending_and_advances_after_a_terminal_failure(services, qdb, monkeypatch):
    qdb.tables["plans_reader_groups"] = [
        {"id": "g1", "run_id": RUN_ID, "kind": "sub", "status": "running"},
        {"id": "g2", "run_id": RUN_ID, "kind": "sub", "status": "canceled"},
        {"id": "g3", "run_id": RUN_ID, "kind": "modify", "status": "running"},
    ]
    advanced = []
    monkeypatch.setattr(services.runs, "advance", lambda sb, rid: advanced.append(rid))
    spec = llm_queue._spec("plans_reader_agent")
    spec.mark("g1", {"status": "pending", "error": None})
    assert qdb.tables["plans_reader_groups"][0]["status"] == "queued"
    assert spec.current_status("g1") == "queued"
    assert advanced == []
    spec.mark("g1", {"status": "failed", "error": "The AI provider refused the request."})
    g1 = qdb.tables["plans_reader_groups"][0]
    assert g1["status"] == "failed" and g1["error"].startswith("The AI provider") and g1["finished_at"]
    assert advanced == [RUN_ID]
    # A canceled group never takes a mark. Terminal groups read as done so
    # the monitor's Retry refuses (it could not revive them anyway).
    spec.mark("g2", {"status": "failed", "error": "x"})
    assert qdb.tables["plans_reader_groups"][1]["status"] == "canceled"
    assert spec.current_status("g2") == "done"
    assert spec.current_status("g1") == "done"
    # A modify group failing does not move the run's pipeline.
    llm_queue._spec("plans_reader_modify").mark("g3", {"status": "failed", "error": "x"})
    assert advanced == [RUN_ID]


def test_monitor_retry_refuses_a_failed_group_and_a_failed_answer(services, qdb, monkeypatch):
    qdb.tables["plans_reader_groups"] = [{"id": "g1", "run_id": RUN_ID, "kind": "sub", "status": "failed"}]
    qdb.tables["plans_reader_chat_messages"] = [{"id": "m1", "status": "failed"}]
    monkeypatch.setattr(llm_queue, "latest_job", lambda jt, target: None)
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("must not enqueue"))
    for job_type, target in (("plans_reader_agent", "g1"), ("plans_reader_chat", "m1")):
        with pytest.raises(ValueError, match="since completed"):
            llm_queue.requeue_terminal({"id": "j1", "status": "failed", "job_type": job_type, "target_id": target}, "u")


def test_group_mark_survives_an_advance_error(services, qdb, monkeypatch):
    qdb.tables["plans_reader_groups"] = [{"id": "g1", "run_id": RUN_ID, "kind": "main", "status": "running"}]

    def _boom(sb, rid):
        raise RuntimeError("db down")

    monkeypatch.setattr(services.runs, "advance", _boom)
    llm_queue._spec("plans_reader_main").mark("g1", {"status": "failed", "error": "x"})
    assert qdb.tables["plans_reader_groups"][0]["status"] == "failed"


def test_message_mark_is_fenced_on_active_messages(qdb):
    qdb.tables["plans_reader_chat_messages"] = [
        {"id": "m1", "status": "running"},
        {"id": "m2", "status": "done", "error": None},
    ]
    spec = llm_queue._spec("plans_reader_chat")
    spec.mark("m1", {"status": "failed", "error": "The AI provider is overloaded."})
    spec.mark("m2", {"status": "failed", "error": "late"})
    assert qdb.tables["plans_reader_chat_messages"][0]["status"] == "failed"
    assert qdb.tables["plans_reader_chat_messages"][1] == {"id": "m2", "status": "done", "error": None}
    assert spec.current_status("m1") == "done"


# ── Error mapping ────────────────────────────────────────────────────────


def test_service_lookup_error_is_a_404_but_a_key_error_is_not(api):
    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "estimate", exc=LookupError("gone"))
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/estimate").status_code == 404
    api.fake(api.svc.runs, "estimate", exc=KeyError("id"))
    with pytest.raises(KeyError):
        api.client.get(f"/ai-estimating/runs/{RUN_ID}/estimate")


def test_service_status_code_exceptions_keep_their_status(api):
    class Refused(Exception):
        status_code = 415

    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "add_upload", exc=Refused("That file type cannot be read."))
    resp = _upload(api)
    assert resp.status_code == 415 and resp.json()["detail"] == "That file type cannot be read."


@pytest.mark.parametrize("code", [403, 404, 413, 415, 503])
@pytest.mark.parametrize("conflict", [True, False])
def test_real_run_refused_keeps_its_status_on_every_route_kind(services, code, conflict):
    from fastapi import HTTPException

    def _refuse():
        raise services.runs.RunRefused("app-authored sentence", code)

    with pytest.raises(HTTPException) as exc:
        pr._service(_refuse, conflict=conflict)
    assert exc.value.status_code == code
    assert exc.value.detail == "app-authored sentence"


def test_real_run_refused_409_carries_the_not_actionable_code(services):
    from fastapi import HTTPException

    def _refuse():
        raise services.runs.RunRefused("This run has already started.", 409)

    with pytest.raises(HTTPException) as exc:
        pr._service(_refuse, conflict=False)
    assert exc.value.status_code == 409
    assert exc.value.headers == {"X-Error-Code": "ai_estimating_not_actionable"}
    # An undeclared RunRefused (default 400) on an action route still answers 400.
    with pytest.raises(HTTPException) as plain:
        pr._service(lambda: (_ for _ in ()).throw(services.runs.RunRefused("bad input")), conflict=True)
    assert plain.value.status_code == 400


def test_a_provider_5xx_is_never_passed_through(api):
    class ProviderError(Exception):
        status_code = 502

    api.seed("plans_reader_runs", _run())
    api.fake(api.svc.runs, "add_upload", exc=ProviderError("upstream said something internal"))
    with pytest.raises(ProviderError):
        _upload(api)


# ── The router's calls bind to the real functions ────────────────────────


def test_every_service_call_matches_the_real_signature(services):
    """The route tests fake these functions; this pins that each call shape
    the router (and the job specs) use binds to the real one, so a renamed
    function or a changed signature fails here instead of in production."""
    from app.services import notifications

    sb, run, user = object(), {"id": RUN_ID}, object()
    calls = [
        (services.runs.create_run, (sb,), dict(name="n", source="upload", project_id=None, render_mode="adaptive", user=user)),
        (services.runs.add_upload, (sb, run), dict(filename="a.pdf", content=b"", label=None, user=user)),
        (services.runs.add_project_files, (sb, run), dict(project_file_ids=[], user=user)),
        (services.runs.estimate, (sb, run), {}),
        (services.runs.start, (sb, run), dict(user=user)),
        (services.runs.cancel, (sb, run), dict(user=user)),
        (services.runs.resume, (sb, run), dict(user=user)),
        (services.runs.list_runs, (sb,), dict(status=None, q=None, limit=50, offset=0)),
        (services.runs.get_run_payload, (sb, RUN_ID), {}),
        (services.runs.delete, (sb, run), dict(user=user)),
        (services.runs.rerender_deliverable, (sb, run, "boq"), dict(user=user)),
        (services.runs.claim_change_slot, (sb, RUN_ID), {}),
        (services.runs.advance, (sb, RUN_ID), {}),
        (services.chat.list_threads, (sb, RUN_ID), {}),
        (services.chat.create_thread, (sb, run), dict(title=None, user=user)),
        (services.chat.get_thread, (sb, THREAD_ID), {}),
        (services.chat.send_message, (sb, run, {"id": THREAD_ID}), dict(content="x", pro_max=False, user=user)),
        (services.chat.run_message, (sb, "m1"), {}),
        (services.agents.run_group, (sb, "g1"), {}),
        (services.agents.modify_deliverable, (sb, run), dict(kind="boq", instruction="x", user=user, group_id="g1")),
        (llm_queue.enqueue, ("plans_reader_modify",), dict(target_id="g1", payload={}, created_by="u")),
        (llm_queue.poll_info, ("plans_reader_modify", "g1"), {}),
        (storage.signed_url, ("path",), dict(download="name.pdf")),
        (storage.object_exists, ("path.pdf",), {}),
        (services.runs.brief_pdf_path, ("a/Deep Brief.docx",), {}),
        (ai_spend.check_cap, (sb,), {}),
        (ai_spend.status, (sb,), {}),
        (notifications.audit, ("u", "ai_estimating.run_created", "plans_reader_run", RUN_ID, {}), {}),
    ]
    for fn, args, kwargs in calls:
        inspect.signature(fn).bind(*args, **kwargs)
    assert isinstance(services.runs.PROJECT_CATEGORY_LABELS, dict)
    assert issubclass(services.runs.RunRefused, ValueError)
    assert pr.audit is notifications.audit


# ── Reconciliation with the frontend (lib/aiEstimating.ts) ───────────────

_WRITE_ROUTES = {
    ("POST", "/ai-estimating/runs"),
    ("POST", "/ai-estimating/runs/{run_id}/files"),
    ("POST", "/ai-estimating/runs/{run_id}/project-files"),
    ("POST", "/ai-estimating/runs/{run_id}/start"),
    ("POST", "/ai-estimating/runs/{run_id}/cancel"),
    ("POST", "/ai-estimating/runs/{run_id}/resume"),
    ("DELETE", "/ai-estimating/runs/{run_id}"),
    ("POST", "/ai-estimating/runs/{run_id}/deliverables/{kind}/rerender"),
    ("POST", "/ai-estimating/runs/{run_id}/deliverables/{kind}/modify"),
    ("POST", "/ai-estimating/runs/{run_id}/chat/threads"),
    ("POST", "/ai-estimating/runs/{run_id}/chat/threads/{thread_id}/messages"),
}


def test_writes_need_require_writer_and_reads_stay_internal():
    from app.core.deps import require_writer

    seen = set()
    for route in pr.router.routes:
        key = (sorted(route.methods)[0], route.path)
        # The handler's `user` parameter is one of the route's dependencies.
        calls = {d.call for d in route.dependant.dependencies}
        if key in _WRITE_ROUTES:
            seen.add(key)
            assert require_writer in calls, f"{key} must take require_writer"
        else:
            assert require_writer not in calls, f"{key} is a read and must not take require_writer"
    assert seen == _WRITE_ROUTES


def test_the_read_only_accountant_reads_but_never_writes(api):
    api.role = Role.ACCOUNTANT
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_chat_threads", {"id": THREAD_ID, "run_id": RUN_ID})
    api.fake(api.svc.runs, "list_runs", result=[])
    api.fake(api.svc.runs, "get_run_payload", result=lambda sb, rid: {"run": {"id": rid}, "groups": []})
    assert api.client.get("/ai-estimating/status").status_code == 200
    assert api.client.get("/ai-estimating/runs").status_code == 200
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}").status_code == 200
    for method, path, body in (
        ("post", "/ai-estimating/runs", {"name": "x", "source": "upload"}),
        ("post", f"/ai-estimating/runs/{RUN_ID}/start", None),
        ("post", f"/ai-estimating/runs/{RUN_ID}/cancel", None),
        ("post", f"/ai-estimating/runs/{RUN_ID}/resume", None),
        ("delete", f"/ai-estimating/runs/{RUN_ID}", None),
        ("post", f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/rerender", None),
        ("post", f"/ai-estimating/runs/{RUN_ID}/deliverables/boq/modify", {"instruction": "x"}),
        ("post", f"/ai-estimating/runs/{RUN_ID}/project-files", {"project_file_ids": [DOC_ID]}),
        ("post", f"/ai-estimating/runs/{RUN_ID}/chat/threads", {"title": "x"}),
        ("post", f"/ai-estimating/runs/{RUN_ID}/chat/threads/{THREAD_ID}/messages", {"content": "x"}),
    ):
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(api.client, method)(path, **kwargs)
        assert resp.status_code == 403, (method, path, resp.status_code)
    assert not api.calls.get("start") and not api.audits


def _call_row(i: int, **extra) -> dict:
    return {
        "id": f"c{i:05d}",
        "run_id": RUN_ID,
        "group_id": "g1",
        "kind": "sub",
        "input_tokens": 10,
        "created_at": f"2026-10-17T12:{i // 60:02d}:{i % 60:02d}",
        "request_snapshot": {"big": "x"},
        **extra,
    }


def test_run_payload_carries_the_call_log_newest_first_and_capped(api):
    api.seed("plans_reader_runs", _run("reading"))
    api.seed("plans_reader_calls", *[_call_row(i) for i in range(5)], _call_row(9, run_id=OTHER_RUN))
    api.seed(
        "plans_reader_groups",
        {"id": "g1", "run_id": RUN_ID, "pages": [{"document_id": "d1"}, {"document_id": "d1"}, {"document_id": "d2"}]},
        {"id": "g2", "run_id": RUN_ID, "pages": []},
    )
    api.fake(
        api.svc.runs,
        "get_run_payload",
        result=lambda sb, rid: {"run": {"id": rid}, "groups": [{"id": "g1", "pages": 3}, {"id": "g2", "pages": 0}]},
    )
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}").json()
    assert [c["id"] for c in body["calls"]] == ["c00004", "c00003", "c00002", "c00001", "c00000"]
    reads = [op for op in api.db.ops if op[0] == "plans_reader_calls"]
    assert reads and ("eq", "run_id", RUN_ID) in reads[0][3]
    assert body["groups"][0]["document_pages"] == {"d1": 2, "d2": 1}
    assert body["groups"][1]["document_pages"] == {}
    # The request snapshot is never selected (the fake ignores columns, so
    # pin the column list instead).
    assert "request_snapshot" not in pr._CALL_COLUMNS
    for col in ("group_id", "thread_id", "kind", "cost_usd", "duration_ms", "ok", "error"):
        assert col in pr._CALL_COLUMNS


def test_run_call_log_stops_at_the_cap(api, monkeypatch):
    monkeypatch.setattr(pr, "_CALLS_MAX", 7)
    monkeypatch.setattr(pr, "_PICKER_PAGE", 3)
    api.seed("plans_reader_calls", *[_call_row(i) for i in range(20)])
    calls = pr._run_calls(api.db, RUN_ID)
    assert len(calls) == 7 and calls[0]["id"] == "c00019" and calls[-1]["id"] == "c00013"


def test_brief_pdf_download_and_its_404s(api, monkeypatch):
    api.seed("plans_reader_runs", _run("done"))
    api.seed(
        "plans_reader_deliverables",
        {**_deliverable(1, "brief"), "filename": "Deep Brief.docx",
         "storage_path": f"plans-reader/{RUN_ID}/deliverables/v1/Deep Brief.docx"},
        {**_deliverable(2, "brief"), "id": "b2", "filename": "Rev 2 - Deep Brief.docx",
         "storage_path": f"plans-reader/{RUN_ID}/deliverables/v2/Rev 2 - Deep Brief.docx"},
        _deliverable(1, "boq"),
    )
    present = {f"plans-reader/{RUN_ID}/deliverables/v2/Rev 2 - Deep Brief.pdf"}
    monkeypatch.setattr(storage, "object_exists", lambda path: path in present)
    base = f"/ai-estimating/runs/{RUN_ID}/deliverables"
    ok = api.client.get(f"{base}/brief/2/download?format=pdf")
    assert ok.status_code == 200
    assert ok.json()["url"] == (
        f"https://signed/plans-reader/{RUN_ID}/deliverables/v2/Rev 2 - Deep Brief.pdf?dl=Rev 2 - Deep Brief.pdf"
    )
    # The docx is still the default.
    assert api.client.get(f"{base}/brief/2/download").json()["url"].endswith(".docx?dl=Rev 2 - Deep Brief.docx")
    missing = api.client.get(f"{base}/brief/1/download?format=pdf")
    assert missing.status_code == 404 and missing.json()["detail"] == pr.MSG_NO_BRIEF_PDF
    assert api.client.get(f"{base}/boq/1/download?format=pdf").status_code == 404
    assert api.client.get(f"{base}/brief/3/download?format=pdf").status_code == 404
    assert api.client.get(f"{base}/brief/2/download?format=docx").status_code == 422


def test_office_deliverables_download_their_pdf_copy_too(api, monkeypatch):
    """`?format=pdf` works for every office kind (the BOQ and the estimate
    xlsx as well as the brief), named after the file with .pdf."""
    api.seed("plans_reader_runs", _run("done"))
    api.seed(
        "plans_reader_deliverables",
        _deliverable(1, "boq"),
        {**_deliverable(2, "estimate"), "id": "e2", "filename": "Rev 2 - EDIKTED - ELECTRICAL ESTIMATE.xlsx",
         "storage_path": f"plans-reader/{RUN_ID}/deliverables/v2/estimate.xlsx"},
        {**_deliverable(1, "trench_markup"), "id": "t1", "filename": "Trench Markup.pdf",
         "storage_path": f"plans-reader/{RUN_ID}/deliverables/v1/trench.pdf"},
    )
    present = {f"plans-reader/{RUN_ID}/deliverables/v1/boq.pdf"}
    monkeypatch.setattr(storage, "object_exists", lambda path: path in present)
    base = f"/ai-estimating/runs/{RUN_ID}/deliverables"
    ok = api.client.get(f"{base}/boq/1/download?format=pdf")
    assert ok.status_code == 200
    assert ok.json()["url"] == f"https://signed/plans-reader/{RUN_ID}/deliverables/v1/boq.pdf?dl=BOQ - COUNTS.pdf"
    missing = api.client.get(f"{base}/estimate/2/download?format=pdf")
    assert missing.status_code == 404 and missing.json()["detail"] == pr.MSG_NO_BRIEF_PDF
    pdf_kind = api.client.get(f"{base}/trench_markup/1/download?format=pdf")
    assert pdf_kind.status_code == 404 and pdf_kind.json()["detail"] == pr.MSG_NO_PDF_KIND


def test_deliverable_preview_url_answers_the_viewer_s_states(api, monkeypatch):
    """The preview route: a PDF kind is ready as it is; an office kind is
    ready with its PDF copy, pending while the copy is built (started on a
    daemon thread, once per object), failed after a build without a PDF,
    and unavailable when no converter is configured. Wrong run, wrong
    version and unknown kind are 404."""
    from app.services import office_preview
    from app.services import plans_reader_runs as runs

    api.seed("plans_reader_runs", _run("done", name="Edikted"))
    api.seed(
        "plans_reader_deliverables",
        _deliverable(1, "boq"),
        {**_deliverable(1, "estimate"), "id": "e1", "filename": "EDIKTED - ELECTRICAL ESTIMATE.xlsx",
         "storage_path": f"plans-reader/{RUN_ID}/deliverables/v1/estimate.xlsx"},
        {**_deliverable(1, "trench_markup"), "id": "t1", "filename": "Trench Markup.pdf",
         "storage_path": f"plans-reader/{RUN_ID}/deliverables/v1/trench.pdf"},
    )
    store: dict[str, bytes] = {
        f"plans-reader/{RUN_ID}/deliverables/v1/boq.xlsx": b"PK-boq",
        f"plans-reader/{RUN_ID}/deliverables/v1/estimate.xlsx": b"PK-estimate",
        f"plans-reader/{RUN_ID}/deliverables/v1/estimate.pdf": b"%PDF-estimate",
    }
    monkeypatch.setattr(storage, "object_exists", lambda path: path in store)
    monkeypatch.setattr(storage, "download_file", lambda path: store[path])
    monkeypatch.setattr(storage, "upload_file", lambda path, content, ct, **k: store.__setitem__(path, content))
    monkeypatch.setattr(runs, "_PDF_COPY_BUILDING", set())
    monkeypatch.setattr(runs, "_PDF_COPY_FAILED", {})
    threads: list = []

    class _Thread:
        def __init__(self, *, target, args=(), name=None, daemon=None):
            self.target, self.args = target, args
            threads.append(self)

        def start(self):
            pass

        def run(self):
            self.target(*self.args)

    monkeypatch.setattr(runs.threading, "Thread", _Thread)
    base = f"/ai-estimating/runs/{RUN_ID}/deliverables"

    # A PDF kind: the file itself.
    pdf = api.client.get(f"{base}/trench_markup/1/preview-url").json()
    assert pdf == {
        "preview_status": "ready",
        "url": f"https://signed/plans-reader/{RUN_ID}/deliverables/v1/trench.pdf?dl=Trench Markup.pdf",
        "filename": "Trench Markup.pdf",
    }
    # An office kind whose copy exists: the copy, named after the file.
    est = api.client.get(f"{base}/estimate/1/preview-url").json()
    assert est == {
        "preview_status": "ready",
        "url": f"https://signed/plans-reader/{RUN_ID}/deliverables/v1/estimate.pdf?dl=EDIKTED - ELECTRICAL ESTIMATE.pdf",
        "filename": "EDIKTED - ELECTRICAL ESTIMATE.pdf",
    }
    assert threads == []

    # No converter: unavailable, nothing started.
    monkeypatch.setattr(office_preview, "get_converter", lambda: None)
    assert api.client.get(f"{base}/boq/1/preview-url").json() == {"preview_status": "unavailable", "filename": "BOQ - COUNTS.pdf"}
    assert threads == []

    # A converter: the copy is built on a thread; a second poll while it
    # runs starts nothing more; once it has written the copy, ready.
    monkeypatch.setattr(office_preview, "get_converter", lambda: object())
    converted: list = []

    def convert(content, filename):
        converted.append((content, filename))
        return b"%PDF-boq"

    monkeypatch.setattr(office_preview, "convert_for_send", convert)
    assert api.client.get(f"{base}/boq/1/preview-url").json() == {"preview_status": "pending", "filename": "BOQ - COUNTS.pdf"}
    assert api.client.get(f"{base}/boq/1/preview-url").json()["preview_status"] == "pending"
    assert len(threads) == 1
    threads[0].run()
    assert converted == [(b"PK-boq", "BOQ - COUNTS.xlsx")]
    assert store[f"plans-reader/{RUN_ID}/deliverables/v1/boq.pdf"] == b"%PDF-boq"
    ready = api.client.get(f"{base}/boq/1/preview-url").json()
    assert ready["preview_status"] == "ready" and ready["url"].endswith("/v1/boq.pdf?dl=BOQ - COUNTS.pdf")

    # A build that produced nothing reads failed until the retry window passes.
    del store[f"plans-reader/{RUN_ID}/deliverables/v1/boq.pdf"]
    monkeypatch.setattr(office_preview, "convert_for_send", lambda c, f: None)
    assert api.client.get(f"{base}/boq/1/preview-url").json()["preview_status"] == "pending"
    threads[-1].run()
    assert api.client.get(f"{base}/boq/1/preview-url").json()["preview_status"] == "failed"
    assert len(threads) == 2
    runs._PDF_COPY_FAILED[f"plans-reader/{RUN_ID}/deliverables/v1/boq.xlsx"] -= runs.PDF_COPY_RETRY_SECONDS + 1
    assert api.client.get(f"{base}/boq/1/preview-url").json()["preview_status"] == "pending"
    assert len(threads) == 3

    assert api.client.get(f"/ai-estimating/runs/{OTHER_RUN}/deliverables/boq/1/preview-url").status_code == 404
    assert api.client.get(f"{base}/boq/2/preview-url").status_code == 404
    assert api.client.get(f"{base}/pricing/1/preview-url").status_code == 404


def test_estimate_route_returns_the_cap_fields_from_the_real_service(api, monkeypatch):
    api.seed("plans_reader_runs", _run())
    monkeypatch.setattr(
        ai_spend, "status", lambda sb, now=None: {"cap_usd": 75.0, "remaining_usd": 12.5, "blocked": False}
    )
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}/estimate").json()
    assert body["cap_remaining_usd"] == 12.5 and body["over_cap"] is False
    for key in ("documents", "groups", "input_tokens", "output_tokens", "cost_usd", "cost_usd_max",
                "over_run_limit", "max_input_tokens", "problems", "priced"):
        assert key in body, key
    monkeypatch.setattr(
        ai_spend,
        "status",
        lambda sb, now=None: {"cap_usd": 75.0, "remaining_usd": 0.0, "cap_hit": True, "blocked": True},
    )
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/estimate").json()["over_cap"] is True
    # A refusal earlier in the window (blocked without cap_hit) is a Settings
    # banner, not a verdict on a run whose worst case fits in what is left.
    monkeypatch.setattr(
        ai_spend,
        "status",
        lambda sb, now=None: {"cap_usd": 75.0, "remaining_usd": 52.0, "cap_hit": False, "blocked": True},
    )
    assert api.client.get(f"/ai-estimating/runs/{RUN_ID}/estimate").json()["over_cap"] is False


def test_chat_send_leaves_staleness_to_the_service(api):
    """The router fails no answer itself: an old pending answer with no job
    may still be a live thread-fallback answer (its heartbeat is its newest
    call, plans_reader_chat._is_stale), so only the service judges it."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    api.seed("plans_reader_runs", _run("done"))
    api.seed("plans_reader_chat_threads", {"id": THREAD_ID, "run_id": RUN_ID})
    api.seed(
        "plans_reader_chat_messages",
        {"id": "m-old", "thread_id": THREAD_ID, "role": "assistant", "status": "running", "created_at": old},
    )
    api.fake(api.svc.chat, "send_message", result={"user_message": {}, "assistant_message": {}})
    url = f"/ai-estimating/runs/{RUN_ID}/chat/threads/{THREAD_ID}/messages"
    assert api.client.post(url, json={"content": "again"}).status_code == 202
    assert api.db.tables["plans_reader_chat_messages"][0]["status"] == "running"
    assert not [op for op in api.db.ops if op[0] == "plans_reader_chat_messages" and op[1] == "update"]
    assert len(api.calls["send_message"]) == 1
    assert not hasattr(pr, "_release_stale_answers")


def test_resume_reaches_the_service_for_a_stalled_working_run(api):
    """Resume is not limited to over_cap: a run stalled in reading,
    consolidating or rendering goes to plans_reader_runs.resume, which
    decides (a rendering run younger than RECOVERY_AFTER_SECONDS is a 409
    there). A rendering run makes no model call, so it is resumable while
    the OpenAI route is unavailable; the others are not."""
    assert set(api.svc.runs.RESUMABLE_STATUSES) >= {"over_cap", "reading", "consolidating", "rendering"}
    api.seed("plans_reader_runs", _run("rendering"))
    api.fake(api.svc.runs, "resume", result=lambda sb, run, **k: {**run, "status": "done"})
    url = f"/ai-estimating/runs/{RUN_ID}/resume"
    api.settings = _settings(openai_api_key="")
    assert api.client.post(url).json()["status"] == "done"
    api.db.tables["plans_reader_runs"][0]["status"] = "reading"
    off = api.client.post(url)
    assert off.status_code == 503 and _code(off) == "ai_estimating_unavailable"
    api.settings = _settings()
    assert api.client.post(url).status_code == 200
    assert len(api.calls["resume"]) == 2
    api.fake(
        api.svc.runs,
        "resume",
        exc=api.svc.runs.RunRefused("This run is still saving its files. Give it a few minutes.", 409),
    )
    api.db.tables["plans_reader_runs"][0]["status"] = "rendering"
    young = api.client.post(url)
    assert young.status_code == 409 and _code(young) == "ai_estimating_not_actionable"
    assert young.json()["detail"] == "This run is still saving its files. Give it a few minutes."
    for st in ("created", "splitting", "done", "canceled"):
        api.db.tables["plans_reader_runs"][0]["status"] = st
        assert api.client.post(url).status_code == 409, st


def test_run_payload_keeps_calls_and_document_pages_the_service_returns(api):
    """When get_run_payload already carries `calls` and per-group
    `document_pages`, the router does not scan those tables again."""
    api.seed("plans_reader_runs", _run("reading"))
    api.fake(
        api.svc.runs,
        "get_run_payload",
        result=lambda sb, rid: {
            "run": {"id": rid},
            "calls": [{"id": "c1"}],
            "groups": [{"id": "g1", "pages": 2, "document_pages": {"d1": 2}}],
        },
    )
    body = api.client.get(f"/ai-estimating/runs/{RUN_ID}").json()
    assert body["calls"] == [{"id": "c1"}] and body["groups"][0]["document_pages"] == {"d1": 2}
    assert not [op for op in api.db.ops if op[0] in ("plans_reader_calls", "plans_reader_groups")]
