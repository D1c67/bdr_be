"""The shared-file integration of the BDR Assistant (phase 0): what
app/assistant/INTEGRATION_TODO.md asked the integration stage to pin.

  config     the boot validator refuses the combinations that could only fail
             at run time; the defaults boot
  llm        the `assistant` feature is registered without the plans switch,
             never joins PLANS_FEATURES, and is pinned to OpenAI under the
             self-hosted master switch when the owner's allow flag is set
  features   GET /features on app.main carries "assistant", per user
  queue      assistant_turn is a known job type with its own claim pass
             (capacity 2, skipped while ASSISTANT_ENABLED is off), enqueue
             honours max_attempts_override, _spec runs the assistant body
  ratelimit  the assistant's bucket is its own and half the size; a 429 on
             it leaves the human's bucket untouched
  gate       log_call stamps user_id and assistant_run_id inside
             llm_messages.attributed and retries without them on a database
             that predates 0170
  transport  three rounds through complete_messages against a fake OpenAI
             client: store False, include reasoning.encrypted_content, never
             previous_response_id, and every round replays the previous
             round's reasoning and function_call items before the output
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.assistant import access, llm_messages
from app.core import provenance, ratelimit
from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant as router_mod
from app.services import llm, llm_gate, llm_queue

from tests.assistant_fakes import RUN, USER, FakeDB, profile, settings_row

SECRET = "integration-test-assistant-secret-0123456789abcdef"


def _s(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", anthropic_api_key="a-key")
    base.update(over)
    return Settings(**base)


def _sh(**over) -> Settings:
    base = dict(
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url="http://localhost:11434/v1",
        self_hosted_llm_local_model="qwen",
    )
    base.update(over)
    return _s(**base)


def _on(**over) -> Settings:
    base = dict(assistant_enabled=True, assistant_token_secret=SECRET)
    base.update(over)
    return _s(**base)


# ── config ───────────────────────────────────────────────────────────────────


def test_assistant_defaults_boot_and_are_off():
    s = _s()
    assert s.assistant_enabled is False and s.assistant_scheduler_enabled is False
    assert s.assistant_model == "gpt-5.4" and s.assistant_reasoning_effort == "low"
    assert (s.assistant_daily_cap_usd, s.assistant_global_daily_cap_usd) == (15.0, 50.0)
    assert s.assistant_concurrency == 2
    assert _on().assistant_enabled is True


@pytest.mark.parametrize(
    "over, names",
    [
        (dict(assistant_scheduler_enabled=True, assistant_enabled=False), "ASSISTANT_SCHEDULER_ENABLED"),
        (dict(assistant_token_secret="short"), "ASSISTANT_TOKEN_SECRET"),
        (dict(assistant_token_secret=""), "ASSISTANT_TOKEN_SECRET"),
        (dict(assistant_daily_cap_usd=0), "ASSISTANT_DAILY_CAP_USD"),
        (dict(assistant_daily_cap_usd=60, assistant_global_daily_cap_usd=50), "ASSISTANT_DAILY_CAP_USD"),
        (dict(assistant_reasoning_effort="max"), "ASSISTANT_REASONING_EFFORT"),
        (dict(assistant_concurrency=0), "ASSISTANT_CONCURRENCY"),
        (dict(assistant_model="not-a-priced-model"), "ASSISTANT_MODEL"),
    ],
)
def test_validator_refuses_each_bad_combination(over, names):
    with pytest.raises(ValueError, match=names):
        _on(**over)


def test_validator_demands_the_allow_flag_under_the_self_hosted_switch():
    with pytest.raises(ValueError, match="ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED"):
        _sh(assistant_enabled=True, assistant_token_secret=SECRET)
    s = _sh(assistant_enabled=True, assistant_token_secret=SECRET, assistant_allow_openai_in_self_hosted=True)
    assert s.assistant_enabled is True


def test_validator_is_silent_while_the_assistant_is_off():
    # A dev .env with a stale short secret must not stop the rest of the app.
    s = _s(assistant_enabled=False, assistant_token_secret="short", assistant_model="nope")
    assert s.assistant_enabled is False


# ── llm routing ──────────────────────────────────────────────────────────────


def test_assistant_feature_is_independent_of_the_plans_switch():
    s = _s(plans_reader_enabled=False)
    assert llm.unavailable_reason("assistant", s) is None
    assert llm.unavailable_reason("assistant_extract", s) is None
    assert llm.is_configured("assistant", s) is True
    route = llm.resolve("assistant", s)
    assert (route.provider, route.model, route.api_key) == ("openai", "gpt-5.4", "o-key")
    assert llm.resolve("assistant", _s(assistant_model="gpt-5.4-mini")).model == "gpt-5.4-mini"
    assert llm.active_model("assistant", s) == "gpt-5.4"


def test_assistant_never_joins_the_plans_features():
    assert "assistant" not in llm.PLANS_FEATURES and "assistant_extract" not in llm.PLANS_FEATURES
    assert llm._FEATURES["assistant"].openai_only is False
    assert llm._FEATURES["assistant"].openai_pinned is True
    assert llm._FEATURES["assistant"].switch is None


def test_self_hosted_switch_keeps_the_assistant_on_openai_with_the_allow_flag():
    pinned = _sh(assistant_allow_openai_in_self_hosted=True)
    route = llm.resolve("assistant", pinned)
    assert (route.provider, route.model, route.api_key) == ("openai", "gpt-5.4", "o-key")
    assert llm.is_configured("assistant", pinned) is True
    # Without the allow flag the feature follows the box like every other
    # feature (the boot validator refuses that state while the assistant is
    # on, so only the health snapshot can observe it).
    assert llm.resolve("assistant", _sh()).provider == "self_hosted"
    # The extract feature follows the master switch either way.
    assert llm.resolve("assistant_extract", pinned).provider == "self_hosted"
    assert llm.resolve("assistant_extract", _s()).provider == "openai"


def test_health_labels_cover_both_features():
    from app.services import llm_health

    assert llm_health.FEATURE_LABELS["assistant"] == "BDR Assistant"
    assert "assistant_extract" in llm_health.FEATURE_LABELS


# ── GET /features ────────────────────────────────────────────────────────────


@pytest.fixture
def features_api(monkeypatch):
    import app.main as main
    from app.core import supabase_client

    db = FakeDB(profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    who = {"role": Role.EXECUTIVE}

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=who["role"], is_active=True)

    main.app.dependency_overrides[get_current_user] = _user
    client = TestClient(main.app, raise_server_exceptions=True)
    try:
        yield SimpleNamespace(client=client, db=db, who=who)
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)


def _enable_env(monkeypatch, on: bool):
    monkeypatch.setattr(router_mod, "get_settings", lambda: SimpleNamespace(assistant_enabled=on))
    monkeypatch.setattr(access, "env_enabled", lambda: on)


def test_features_carries_assistant_false_while_the_env_switch_is_off(features_api, monkeypatch):
    _enable_env(monkeypatch, False)
    body = features_api.client.get("/features").json()
    assert body["assistant"] is False
    assert body["bidding"] is True  # the rest of the map is untouched
    assert not any(t == "assistant_settings" for t, _ in features_api.db.calls)


def test_features_carries_assistant_per_user(features_api, monkeypatch):
    _enable_env(monkeypatch, True)
    assert features_api.client.get("/features").json()["assistant"] is True
    features_api.who["role"] = Role.ESTIMATOR
    assert features_api.client.get("/features").json()["assistant"] is False
    features_api.who["role"] = Role.ESTIMATING_ENGINEER_LABOR
    features_api.db.tables["assistant_profiles"] = [{"user_id": USER, "enabled": False}]
    assert features_api.client.get("/features").json()["assistant"] is False
    features_api.db.tables["assistant_profiles"] = []
    features_api.db.tables["assistant_settings"][0]["enabled"] = False
    assert features_api.client.get("/features").json()["assistant"] is False


def test_features_fails_closed_on_a_read_error(features_api, monkeypatch):
    _enable_env(monkeypatch, True)
    monkeypatch.setattr(router_mod, "_rows_for", lambda user: (_ for _ in ()).throw(RuntimeError("db down")))
    assert features_api.client.get("/features").json()["assistant"] is False


def test_main_mounts_the_router_without_importing_the_package():
    import inspect

    import app.main as main

    assert any(getattr(r, "path", "") == "/assistant/health" for r in main.app.routes)
    source = inspect.getsource(main)
    assert "app.assistant" not in source
    assert "from app.assistant" not in source


def test_a_fresh_app_main_never_loads_the_engine_modules():
    # Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md 7.1): the continuation, the
    # spend ledger and the executor load only when a job or a turn needs them.
    import subprocess
    import sys
    from pathlib import Path

    code = "import app.main, sys; print(','.join(sorted(m for m in sys.modules if m.startswith('app.assistant'))))"
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, timeout=120, check=True).stdout.strip().splitlines()[-1]
    loaded = set(out.split(","))
    for name in ("app.assistant.continue_job", "app.assistant.spend", "app.assistant.executor"):
        assert name not in loaded, name


# ── queue ────────────────────────────────────────────────────────────────────


class _JobsDB:
    """Just enough of the SDK for enqueue's insert."""

    def __init__(self):
        self.rows: list[dict] = []

    def table(self, name):
        assert name == "llm_jobs"
        return self

    def insert(self, row):
        self._row = dict(row)
        return self

    def execute(self):
        self._row.setdefault("id", f"job-{len(self.rows) + 1}")
        self.rows.append(self._row)
        return SimpleNamespace(data=[self._row])


def test_assistant_turn_is_a_known_job_type():
    assert llm_queue.JOB_ASSISTANT_TURN == "assistant_turn"
    assert llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE == "assistant_approval_execute"
    assert llm_queue.JOB_ASSISTANT_CONTINUE == "assistant_continue"
    assert llm_queue.ASSISTANT_JOB_TYPES == (
        llm_queue.JOB_ASSISTANT_TURN, llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE, llm_queue.JOB_ASSISTANT_CONTINUE,
    )
    for job_type in llm_queue.ASSISTANT_JOB_TYPES:
        assert job_type not in llm_queue.LLM_JOB_TYPES
        assert job_type not in llm_queue.PLANS_JOB_TYPES
        assert job_type not in llm_queue.ALL_JOB_TYPES
    assert llm_queue._FEATURE_BY_TYPE[llm_queue.JOB_ASSISTANT_TURN] == "assistant"
    assert llm_queue._FEATURE_BY_TYPE[llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE] == "assistant_execute"
    assert llm_queue._FEATURE_BY_TYPE[llm_queue.JOB_ASSISTANT_CONTINUE] == "assistant_continue"


def test_spec_runs_the_assistant_body():
    from app.assistant import jobs

    spec = llm_queue._spec(llm_queue.JOB_ASSISTANT_TURN)
    assert spec.run is jobs.run_assistant_turn
    assert spec.feature == "assistant"
    assert spec.mark is jobs.mark_turn and spec.current_status is jobs.turn_status


def test_spec_runs_the_approval_execute_body():
    from app.assistant import approvals_job

    spec = llm_queue._spec(llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE)
    assert spec.run is approvals_job.run_approval_execute
    assert spec.feature == "assistant_execute"
    assert spec.mark is approvals_job.mark_approval and spec.current_status is approvals_job.approval_status
    assert spec.model_label(_s()) == approvals_job.MODEL_LABEL


def test_spec_runs_the_continue_body():
    from app.assistant import continue_job

    spec = llm_queue._spec(llm_queue.JOB_ASSISTANT_CONTINUE)
    assert spec.run is continue_job.run_continue
    assert spec.feature == "assistant_continue" == continue_job.FEATURE
    assert spec.mark is continue_job.mark_task and spec.current_status is continue_job.task_status
    assert spec.model_label(_s()) == continue_job.MODEL_LABEL == "assistant-continue"
    # A non-LLM job: its failure sentence never talks about an AI provider.
    assert spec.error_message(RuntimeError("PGRST boom"), "x") == continue_job.RETRY_MESSAGE


def test_enqueue_honours_max_attempts_override(monkeypatch):
    db = _JobsDB()
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "get_settings", lambda: _s())
    job = llm_queue.enqueue(llm_queue.JOB_ASSISTANT_TURN, target_id="conv-1", max_attempts_override=1)
    assert job["max_attempts"] == 1 and job["feature"] == "assistant"
    assert llm_queue.enqueue(llm_queue.JOB_ASSISTANT_TURN, target_id="conv-2", max_attempts_override=0)["max_attempts"] == 1
    # Without the override the retry schedule still decides.
    assert llm_queue.enqueue(llm_queue.JOB_BOQ, target_id="a")["max_attempts"] == 1 + len(_s().llm_retry_delay_list)


def _turn_job(**over) -> dict:
    base = {
        "id": "j1", "job_type": llm_queue.JOB_ASSISTANT_TURN, "feature": "assistant",
        "target_id": "conv-1", "project_id": None, "status": "running", "attempts": 1,
        "max_attempts": 1, "claimed_by": "w1", "payload": {"message_id": "m1", "user_id": USER,
                                                           "conversation_id": "conv-1"},
    }
    base.update(over)
    return base


def test_transient_failure_never_requeues_a_turn_with_max_attempts_1(monkeypatch):
    """The failure handler honours the row's max_attempts, not only the
    retry ladder: a transient error that escapes run_turn fails the one
    attempt terminally."""
    from app.assistant import jobs

    cas: list[dict] = []
    marks: list[tuple[str, dict]] = []
    monkeypatch.setattr(llm_queue, "_cas_job", lambda sb, job, fields: cas.append(dict(fields)) or True)
    spec = llm_queue._JobSpec(**{**jobs.spec_kwargs(), "mark": lambda target, fields: marks.append((target, fields))})
    exc = llm.LlmBadOutput("bad")
    assert llm_queue.llm_errors.is_transient_kind(llm_queue.llm_errors.classify(exc))
    llm_queue._handle_failure_inner(None, _turn_job(), spec, exc, _s())
    assert cas[-1]["status"] == "failed" and cas[-1]["claimed_by"] is None
    assert marks == [("conv-1", {"status": "failed", "error": marks[0][1]["error"]})]
    assert "attempts" not in marks[0][1]["error"]
    # A row that allows more attempts still retries (the ladder is unchanged).
    cas.clear()
    marks.clear()
    llm_queue._handle_failure_inner(None, _turn_job(max_attempts=6), spec, exc, _s())
    assert cas[-1]["status"] == "queued"
    assert marks == [("conv-1", {"status": "pending", "error": None})]
    # A row without the column keeps the ladder length.
    cas.clear()
    llm_queue._handle_failure_inner(None, _turn_job(max_attempts=None), spec, exc, _s())
    assert cas[-1]["status"] == "queued"


def test_queue_adapters_take_the_owner_from_the_job(monkeypatch):
    """Permissions item 14: the two queue adapters filter assistant_messages
    by the job owner's user_id (payload), never by the conversation alone."""
    from app.assistant import jobs, repo

    seen: list[tuple] = []
    monkeypatch.setattr(repo, "queue_mark_turn", lambda user_id, cid, fields: seen.append(("mark", user_id, cid)))
    monkeypatch.setattr(repo, "queue_turn_status",
                        lambda user_id, cid: seen.append(("status", user_id, cid)) or "pending")
    monkeypatch.setattr(llm_queue, "latest_job", lambda jt, t: (_ for _ in ()).throw(AssertionError("not needed")))
    token = llm_queue.current_job.set(_turn_job())
    try:
        jobs.mark_turn("conv-1", {"status": "failed", "error": "x"})
        assert jobs.turn_status("conv-1") == "pending"
    finally:
        llm_queue.current_job.reset(token)
    assert seen == [("mark", USER, "conv-1"), ("status", USER, "conv-1")]
    # Outside a running job (the lease sweep, the retry path) the newest
    # assistant_turn job for the target names the owner.
    looked: list[tuple[str, str]] = []

    def latest(job_type, target):
        looked.append((job_type, target))
        return _turn_job(target_id=target, payload={"user_id": "owner-2"})

    monkeypatch.setattr(llm_queue, "latest_job", latest)
    seen.clear()
    jobs.mark_turn("conv-2", {"status": "failed"})
    assert seen == [("mark", "owner-2", "conv-2")] and looked == [("assistant_turn", "conv-2")]
    # No job names an owner: the mark is skipped, the status is None, and
    # the repo refuses an empty owner outright instead of widening the filter.
    monkeypatch.setattr(llm_queue, "latest_job", lambda jt, t: None)
    seen.clear()
    jobs.mark_turn("conv-3", {"status": "failed"})
    assert jobs.turn_status("conv-3") is None and seen == []
    monkeypatch.undo()
    with pytest.raises(ValueError):
        repo.queue_mark_turn("", "conv-3", {"status": "failed"})
    with pytest.raises(ValueError):
        repo.queue_turn_status(None, "conv-3")


@pytest.fixture
def claims(monkeypatch):
    calls: list[tuple[int, list[str]]] = []

    def _claim_filtered(s, max_jobs, job_types):
        calls.append((max_jobs, list(job_types)))
        return [{"id": f"j{len(calls)}-{i}", "job_type": job_types[0]} for i in range(max_jobs)]

    monkeypatch.setattr(llm_queue, "_claim_filtered", _claim_filtered)
    return calls


def _tick_settings(**over):
    base = dict(plans_reader_enabled=False, rfp_ingest_enabled=False, llm_queue_worker_concurrency=0)
    base.update(over)
    return _s(**base)


def test_claim_tick_skips_the_assistant_pass_while_the_switch_is_off(claims):
    assert llm_queue._claim_tick(_tick_settings(), []) == []
    assert claims == []


def test_claim_tick_claims_assistant_turns_with_their_own_capacity(claims):
    s = _tick_settings(assistant_enabled=True, assistant_token_secret=SECRET)
    claimed = llm_queue._claim_tick(s, [])
    assert claims == [(2, list(llm_queue.ASSISTANT_JOB_TYPES))]
    assert len(claimed) == 2
    # Two running turns fill the capacity: no third claim, no RPC call.
    claims.clear()
    assert llm_queue._claim_tick(s, [llm_queue.JOB_ASSISTANT_TURN, llm_queue.JOB_ASSISTANT_TURN]) == []
    assert claims == []
    # Pipeline and AI Estimating jobs never count against the assistant.
    claims.clear()
    llm_queue._claim_tick(s, [llm_queue.JOB_BOQ, llm_queue.JOB_PLANS_CHAT, llm_queue.JOB_ASSISTANT_TURN])
    assert claims == [(1, list(llm_queue.ASSISTANT_JOB_TYPES))]
    # A running approval execution shares the capacity of 2.
    claims.clear()
    llm_queue._claim_tick(s, [llm_queue.JOB_ASSISTANT_APPROVAL_EXECUTE])
    assert claims == [(1, list(llm_queue.ASSISTANT_JOB_TYPES))]


def test_claim_tick_reads_assistant_concurrency(claims):
    s = _tick_settings(assistant_enabled=True, assistant_token_secret=SECRET, assistant_concurrency=3)
    llm_queue._claim_tick(s, [llm_queue.JOB_ASSISTANT_TURN])
    assert claims == [(2, list(llm_queue.ASSISTANT_JOB_TYPES))]


# ── ratelimit ────────────────────────────────────────────────────────────────


@pytest.fixture
def limiter(monkeypatch):
    monkeypatch.setattr(ratelimit, "get_settings", lambda: SimpleNamespace(
        rate_limit_enabled=True, outbound_email_rate_limit_per_hour=4,
    ))
    ratelimit._buckets.clear()
    yield ratelimit._buckets
    ratelimit._buckets.clear()


def test_assistant_hits_its_own_smaller_bucket(limiter):
    from fastapi import HTTPException

    with provenance.acting_as_assistant(USER, RUN):
        for _ in range(5):
            ratelimit._check("ai_jobs", USER, 10)
        with pytest.raises(HTTPException) as exc:
            ratelimit._check("ai_jobs", USER, 10)
    assert exc.value.status_code == 429
    assert ("ai_jobs", f"{USER}:assistant") in limiter
    assert ("ai_jobs", USER) not in limiter
    # The human still has the whole quota.
    for _ in range(10):
        ratelimit._check("ai_jobs", USER, 10)
    with pytest.raises(HTTPException):
        ratelimit._check("ai_jobs", USER, 10)
    assert limiter[("ai_jobs", USER)][1] == 10


def test_outbound_email_budget_is_split_the_same_way(limiter):
    from fastapi import HTTPException

    with provenance.acting_as_assistant(USER, RUN):
        ratelimit.check_outbound_email(USER)
        ratelimit.check_outbound_email(USER)
        with pytest.raises(HTTPException):
            ratelimit.check_outbound_email(USER)
    assert ("outbound_email", f"{USER}:assistant") in limiter
    ratelimit.check_outbound_email(USER)  # the human: untouched


def test_bucket_limit_never_drops_below_one():
    with provenance.acting_as_assistant(USER, RUN):
        assert ratelimit._bucket_limit(1) == 1
        assert ratelimit._bucket_limit(3) == 1
    assert ratelimit._bucket_limit(3) == 3


# ── llm_gate attribution ─────────────────────────────────────────────────────


class _LogDB:
    def __init__(self, reject_attribution: bool = False):
        self.rows: list[dict] = []
        self.attempts: list[dict] = []
        self.reject_attribution = reject_attribution

    def table(self, name):
        assert name == "llm_call_log"
        return self

    def insert(self, row):
        self._row = dict(row)
        return self

    def execute(self):
        self.attempts.append(dict(self._row))
        if self.reject_attribution and "user_id" in self._row:
            exc = Exception("Could not find the 'user_id' column of 'llm_call_log' in the schema cache")
            exc.code = "PGRST204"
            raise exc
        self.rows.append(self._row)
        return SimpleNamespace(data=[self._row])


def _log(**over):
    base = dict(feature="assistant", provider="openai", model="gpt-5.4", tier_name="job",
                ok=True, error=None, duration_ms=10)
    base.update(over)
    llm_gate.log_call(**base)


def test_log_call_stamps_the_attribution_inside_a_turn(monkeypatch):
    db = _LogDB()
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    with llm_messages.attributed(USER, RUN):
        _log()
    _log()
    stamped, plain = db.rows
    assert stamped["user_id"] == USER and stamped["assistant_run_id"] == RUN
    assert "user_id" not in plain and "assistant_run_id" not in plain


def test_log_call_retries_without_the_columns_before_0170(monkeypatch, caplog):
    db = _LogDB(reject_attribution=True)
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    monkeypatch.setattr(llm_gate, "_attribution_columns_warned", False)
    with llm_messages.attributed(USER, RUN):
        _log()
        _log()
    assert len(db.rows) == 2 and all("user_id" not in r for r in db.rows)
    assert len(db.attempts) == 4  # each call: one refusal, one retry
    warnings = [r for r in caplog.records if "0170" in r.getMessage()]
    assert len(warnings) == 1  # one WARNING per process


# ── transport ────────────────────────────────────────────────────────────────


class _FakeOpenAI:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)

    def with_options(self, **kw):
        return self


def _usage():
    return SimpleNamespace(
        input_tokens=100, input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens=20, output_tokens_details=SimpleNamespace(reasoning_tokens=5),
    )


def _resp(*, output, rid, text=""):
    return SimpleNamespace(id=rid, status="completed", incomplete_details=None, output_text=text,
                           output=list(output), usage=_usage())


def _reasoning(n):
    return {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"enc-{n}"}


def _call(n):
    return {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": "whats_due",
            "arguments": json.dumps({"limit": n})}


@pytest.fixture
def quiet(monkeypatch):
    llm_gate.reset_gates()
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: None)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    yield
    llm_gate.reset_gates()


def test_three_round_tool_loop_replays_reasoning_and_calls_without_previous_response_id(quiet, monkeypatch):
    client = _FakeOpenAI(
        _resp(output=[_reasoning(1), _call(1)], rid="resp_1"),
        _resp(output=[_reasoning(2), _call(2)], rid="resp_2"),
        _resp(output=[_reasoning(3), {"type": "message", "id": "msg_3", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "Two bids are due."}]}],
              rid="resp_3", text="Two bids are due."),
    )
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    tools = [{"type": "function", "name": "whats_due", "description": "d",
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True}]
    items = [llm_messages.user_message("What is due today?")]
    s = _s()

    for n in (1, 2):
        result = llm_messages.complete_messages(system="sys", messages=list(items), tools=tools, settings=s)
        assert result.tool_calls == [{"call_id": f"call_{n}", "name": "whats_due", "arguments": {"limit": n}}]
        items.extend(result.output_items)
        items.append(llm_messages.function_call_output(f"call_{n}", {"rows": n}))
    final = llm_messages.complete_messages(system="sys", messages=list(items), tools=None, settings=s)
    assert final.text == "Two bids are due." and final.tool_calls == []

    assert len(client.calls) == 3
    for kwargs in client.calls:
        assert kwargs["store"] is False
        assert kwargs["include"] == ["reasoning.encrypted_content"]
        assert "previous_response_id" not in kwargs
        assert kwargs["model"] == "gpt-5.4" and kwargs["instructions"] == "sys"
        assert kwargs["reasoning"] == {"effort": "low"}
    assert client.calls[0]["tools"] == tools and "tools" not in client.calls[2]

    # Round 2 replays round 1's reasoning and function_call, then the output.
    second = client.calls[1]["input"]
    assert [i.get("type") for i in second[1:]] == ["reasoning", "function_call", "function_call_output"]
    assert second[1] == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-1"}
    assert second[2] == {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "whats_due",
                         "arguments": json.dumps({"limit": 1})}
    assert second[3] == {"type": "function_call_output", "call_id": "call_1", "output": json.dumps({"rows": 1})}
    # Round 3 carries both earlier rounds in order.
    third = client.calls[2]["input"]
    assert [i.get("type") for i in third[1:]] == [
        "reasoning", "function_call", "function_call_output",
        "reasoning", "function_call", "function_call_output",
    ]
    assert third[4]["id"] == "rs_2" and third[5]["call_id"] == "call_2"


def test_complete_messages_refuses_a_non_openai_route(quiet):
    with pytest.raises(llm.LlmNotConfigured, match="OpenAI provider"):
        llm_messages.complete_messages(system="s", messages=[llm_messages.user_message("x")], settings=_sh())
