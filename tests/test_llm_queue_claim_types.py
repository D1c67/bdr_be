"""llm_queue._claim_tick honours LLM_QUEUE_CLAIM_TYPES (spec 0.2, WS0):
empty means every type exactly as before; a restricted worker (the eval
sweep backend) never claims another type, and a pass whose types are all
excluded makes no claim call. The two job types migration MC adds are
named in the module but never claimed before MC lands."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.core.config import Settings
from app.services import llm_queue

SWEEP_TYPES = (
    "plans_reader_agent,plans_reader_main,plans_reader_chat,plans_reader_modify,"
    "plans_reader_facts,ai_pricing,bid_split"
)
BIDDING_TYPES = {"boq_extraction", "general_material", "proposal_lines"}


class _Recorder:
    def __init__(self, backlog=None):
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


def _settings(**over) -> Settings:
    base = dict(
        _env_file=None, plans_reader_enabled=True, openai_api_key="k", llm_queue_enabled=True,
        rfp_ingest_enabled=True, rfp_harvest_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def _claimed_types(rec: _Recorder) -> set[str]:
    return {jt for _, types in rec.calls for jt in types}


def test_empty_claims_every_family_as_before(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(llm_queue, "_claim", rec)
    llm_queue._claim_tick(_settings(llm_queue_claim_types=""), [])
    got = _claimed_types(rec)
    assert BIDDING_TYPES <= got and "bid_split" in got and "rfp_ingest" in got
    assert set(llm_queue.PLANS_JOB_TYPES) <= got
    assert llm_queue.claim_types_allowed(_settings()) == ()


def test_sweep_backend_never_claims_bidding_or_rfp_jobs(monkeypatch):
    rec = _Recorder({"boq_extraction": 3, "bid_split": 1, "plans_reader_agent": 5, "rfp_ingest": 2})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    got = llm_queue._claim_tick(_settings(llm_queue_claim_types=SWEEP_TYPES), [])
    assert not _claimed_types(rec) & (BIDDING_TYPES | {"rfp_ingest", "rfp_harvest", "rfp_create_files"})
    # The pipeline pass is narrowed to the splitter; its capacity is unchanged.
    llm_pass = next(c for c in rec.calls if "bid_split" in c[1])
    assert llm_pass[1] == ["bid_split"]
    assert sorted(j["job_type"] for j in got) == ["bid_split", "plans_reader_agent"]
    assert rec.backlog["boq_extraction"] == 3 and rec.backlog["rfp_ingest"] == 2


def test_a_fully_excluded_pass_makes_no_claim_call(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(llm_queue, "_claim", rec)
    llm_queue._claim_tick(_settings(llm_queue_claim_types="plans_reader_chat"), [])
    assert rec.calls == [(2, ["plans_reader_chat"])]


def test_pending_mc_types_are_named_but_never_claimed(monkeypatch):
    """ai_pricing (WS6) stays pending until its spec lands; the page facts
    job (WS1, wave 2) is claimed in the interactive tier of the plans pass."""
    assert llm_queue.JOB_PLANS_FACTS == "plans_reader_facts"
    assert llm_queue.JOB_AI_PRICING == "ai_pricing"
    assert llm_queue.PENDING_MC_JOB_TYPES == ()
    assert llm_queue.JOB_AI_PRICING in llm_queue.ALL_JOB_TYPES
    assert llm_queue.JOB_PLANS_FACTS in llm_queue.PLANS_INTERACTIVE_JOB_TYPES
    assert llm_queue.JOB_PLANS_FACTS in llm_queue.ALL_JOB_TYPES
    rec = _Recorder({"plans_reader_facts": 2, "ai_pricing": 2})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    got = llm_queue._claim_tick(_settings(llm_queue_claim_types="ai_pricing"), [])
    assert got == [] and rec.calls == []
    rec = _Recorder({"plans_reader_facts": 2, "ai_pricing": 2})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    got = llm_queue._claim_tick(_settings(llm_queue_claim_types="plans_reader_facts,ai_pricing"), [])
    assert [j["job_type"] for j in got] == ["plans_reader_facts", "plans_reader_facts"]
    assert rec.calls == [(2, ["plans_reader_facts"])]


def test_facts_job_is_claimed_before_the_batch_types(monkeypatch):
    rec = _Recorder({"plans_reader_facts": 1, "plans_reader_agent": 5})
    monkeypatch.setattr(llm_queue, "_claim", rec)
    got = llm_queue._claim_tick(_settings(), [])
    plans_calls = [c for c in rec.calls if "plans_reader_facts" in c[1] or "plans_reader_agent" in c[1]]
    assert plans_calls[0][1] == list(llm_queue.PLANS_INTERACTIVE_JOB_TYPES)
    assert [j["job_type"] for j in got if j["job_type"].startswith("plans_")] == ["plans_reader_facts", "plans_reader_agent"]
    spec = llm_queue._spec("plans_reader_facts")
    assert spec.feature == "plans_facts" and spec.model_label(_settings()) == "pagefacts"
    assert "AI provider" not in spec.error_message(RuntimeError("boom"), "pagefacts")


def test_claim_types_allowed_reads_plain_settings_objects():
    assert llm_queue.claim_types_allowed(SimpleNamespace(llm_queue_claim_types=" bid_split , Ai_Pricing ")) == (
        "bid_split",
        "ai_pricing",
    )
    assert llm_queue.claim_types_allowed(SimpleNamespace()) == ()
    assert llm_queue.claim_types_allowed(_settings(llm_queue_claim_types="bid_split,bid_split")) == ("bid_split",)
