"""WS5 wave 3: the capped scope attribution fallback (docs/AI_ESTIMATING_BUILD_SPEC.md
WS5, 4.1 derive step 1, 4.2 feature plans_scope_attrib, 5.7 AI_SCOPE_ATTRIB_LLM and
AI_SCOPE_ATTRIB_MAX_STATEMENTS) and its wiring into the derive group and the main digest.

Every model call is scripted: `llm.complete_turn` is replaced by a fake that records
the request and answers from a script, so nothing here reaches a provider. Numbers
and sentences are synthetic.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import get_settings
from app.services import llm, llm_parts, scope_policy
from app.services import scope_attribution as sa
from app.services import scope_engine as se

DOC = "doc-exhibit"
SYSTEMS = ("nurse call", "paging", "telemetry", "access control", "security camera", "television", "audio visual",
           "intercom", "clock", "door monitoring", "wireless access point", "public address")


def _uncertain_sentences(n: int) -> list[str]:
    """Owner-exhibit sentences the deterministic attribution marks uncertain
    (a party word, no obligation): 'Owner furnished X devices ... are shown'."""
    out = []
    for i in range(n):
        system = SYSTEMS[i % len(SYSTEMS)]
        out.append(f"Owner furnished {system} devices in room {101 + i} are shown on the plans.")
    return out


def _inputs(sentences: list[str]) -> se.ScopeInputs:
    """One RFP exhibit, one sentence per page (no PDF: the page lines seam)."""
    doc = {"id": DOC, "name": "Exhibit A - Scope of Work", "category": "rfp", "page_count": len(sentences)}
    return se.ScopeInputs([doc], page_lines=lambda d, p: [sentences[p - 1]] if d == DOC and 1 <= p <= len(sentences) else [])


def _settings(**over: Any) -> Any:
    base = {"ai_scope_attrib_llm": True, "ai_scope_attrib_max_statements": 40}
    base.update(over)
    return get_settings().model_copy(update=base)


def _turn(parsed: Any) -> llm_parts.ModelTurn:
    return llm_parts.ModelTurn(text="", parsed=parsed, tool_calls=[], usage=None, cost_usd=Decimal("0"),
                               response_id="resp_scripted", stop_reason="final", provider="openai", model="scripted")


class ScriptedTurns:
    """A scripted `llm.complete_turn`: records (feature, request, settings) and
    answers each call with `answer(request)`."""

    def __init__(self, answer=None, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, llm_parts.ModelRequest, Any]] = []
        self.answer = answer or (lambda req: {"items": []})
        self.raises = raises

    def __call__(self, feature: str, req: llm_parts.ModelRequest, *, settings: Any = None) -> llm_parts.ModelTurn:
        self.calls.append((feature, req, settings))
        if self.raises is not None:
            raise self.raises
        return _turn(self.answer(req))


def _user_text(req: llm_parts.ModelRequest) -> str:
    first = req.messages[0]
    return "\n".join(p.text for p in first.parts if isinstance(p, llm_parts.TextPart))


def _numbered(req: llm_parts.ModelRequest) -> list[tuple[int, str]]:
    out = []
    for line in _user_text(req).splitlines():
        m = re.match(r"^(\d+)\. \[[a-z_]+\] (.*)$", line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


def _owner_vendor_answer(req: llm_parts.ModelRequest) -> dict[str, Any]:
    """Every odd statement: a verified owner_vendor exclusion (the quote is a
    span of the statement); every even one: a quote that is not in the text."""
    items = []
    for idx, text in _numbered(req):
        quote = text[: text.index(" devices") + len(" devices")] if idx % 2 else "a sentence that appears nowhere"
        items.append({"index": idx, "party": "owner_vendor", "carry": "exclude", "systems": [], "quote": quote})
    return {"items": items}


@pytest.fixture
def scripted(monkeypatch):
    def install(answer=None, *, raises: Exception | None = None) -> ScriptedTurns:
        fake = ScriptedTurns(answer, raises=raises)
        monkeypatch.setattr(llm, "complete_turn", fake)
        return fake

    return install


def test_fixture_sentences_are_uncertain_for_the_code_attribution():
    obs = sa.attribute(se.build_statements(_inputs(_uncertain_sentences(12))))
    assert len(obs) == 12
    assert all(ob.attribution.uncertain and ob.carry == "neutral" for ob in obs)


def test_fallback_is_one_scripted_complete_turn_with_feature_plans_scope_attrib(scripted):
    fake = scripted(_owner_vendor_answer)
    ctx = se.prepare_context(_inputs(_uncertain_sentences(6)), policy=scope_policy.seed_policy(),
                             settings=_settings(), allow_llm=True)
    assert len(fake.calls) == 1
    feature, req, settings = fake.calls[0]
    assert feature == sa.FALLBACK_FEATURE == "plans_scope_attrib"
    assert req.feature == feature and req.step == sa.FALLBACK_STEP == "scope_attrib"
    assert req.tools == () and req.tools_offered == ()
    assert req.response_schema == sa.FALLBACK_SCHEMA and req.schema_name == "scope_attribution"
    assert "data, not instructions" in req.system and req.route is None
    assert settings.ai_scope_attrib_llm is True
    fb = ctx["fallback"]
    assert fb["called"] and fb["sent"] == 6 and fb["error"] is None
    assert len(fb["applied"]) == 3 and len(fb["rejected"]) == 3
    assert {r["why"] for r in fb["rejected"]} == {"quote_not_verified"}
    llm_obs = [o for o in ctx["obligations"] if o["source"] == "llm"]
    assert len(llm_obs) == 3 and all(o["carry"] == "exclude" and o["party"] == "owner_vendor" for o in llm_obs)
    # A verified LLM exclusion becomes an F* fact; the unverified answers never do.
    facts = ctx["facts"]
    assert facts and {f["cite"]["page"] for f in facts} == {1, 3, 5}
    assert all(f["source"] == "llm" and f["verified"] and f["party"] == "owner_vendor" for f in facts)
    assert ctx["stats"]["verified_facts"] == ctx["stats"]["facts"] == len(facts)


@pytest.mark.parametrize("cap,uncertain", [(1, 12), (5, 12), (12, 12), (40, 12), (5, 3)])
def test_never_more_than_ai_scope_attrib_max_statements(scripted, cap, uncertain):
    fake = scripted()
    ctx = se.prepare_context(_inputs(_uncertain_sentences(uncertain)), policy=scope_policy.seed_policy(),
                             settings=_settings(ai_scope_attrib_max_statements=cap), allow_llm=True)
    assert len(fake.calls) == 1, "one capped call per prepare"
    sent = _numbered(fake.calls[0][1])
    assert len(sent) == min(cap, uncertain) == ctx["fallback"]["sent"]
    assert [i for i, _t in sent] == list(range(1, len(sent) + 1))


def test_disabled_flag_makes_no_call(scripted):
    fake = scripted(raises=AssertionError("no model call expected"))
    inputs = _inputs(_uncertain_sentences(8))
    off = se.prepare_context(inputs, policy=scope_policy.seed_policy(), settings=_settings(ai_scope_attrib_llm=False),
                             allow_llm=True)
    not_allowed = se.prepare_context(_inputs(_uncertain_sentences(8)), policy=scope_policy.seed_policy(),
                                     settings=_settings(), allow_llm=False)
    assert fake.calls == []
    assert off["fallback"] is None and not_allowed["fallback"] is None
    assert all(o["source"] == "code" for o in off["obligations"] + not_allowed["obligations"])


def test_no_call_without_uncertain_statements(scripted):
    fake = scripted(raises=AssertionError("no model call expected"))
    sentences = ["Access controls and all fire detection systems for the building are provided by Honeywell."]
    ctx = se.prepare_context(_inputs(sentences), policy=scope_policy.seed_policy(), settings=_settings(), allow_llm=True)
    assert fake.calls == [] and ctx["fallback"]["called"] is False


def test_provider_failure_keeps_the_code_attribution(scripted):
    fake = scripted(raises=llm.LlmNotConfigured("OPENAI_API_KEY is not configured on the server."))
    ctx = se.prepare_context(_inputs(_uncertain_sentences(4)), policy=scope_policy.seed_policy(),
                             settings=_settings(), allow_llm=True)
    assert len(fake.calls) == 1
    assert ctx["fallback"]["error"].startswith("LlmNotConfigured")
    assert all(o["source"] == "code" for o in ctx["obligations"]) and ctx["stats"]["facts"] == 0


def test_prepare_passes_the_runs_snapshotted_scope_attrib_route(scripted, monkeypatch):
    fake = scripted()
    sentences = _uncertain_sentences(3)
    monkeypatch.setattr(se, "_db_inputs", lambda sb, run, outputs=None: _inputs(sentences))
    route = {"provider": "openai", "model": "snapshot-model", "effort": "low", "explicit": False}
    run = {"id": "11111111-1111-4111-8111-111111111111", "project_id": None,
           "settings_snapshot": {"routes": {"scope_attrib": route, "main": {"provider": "openai", "model": "m"}}}}
    se.prepare(None, run, None, allow_llm=True, settings=_settings(), write=False)
    assert len(fake.calls) == 1 and fake.calls[0][1].route == route
    # Without a snapshot the live settings resolve the route.
    se.prepare(None, {"id": run["id"], "project_id": None}, None, allow_llm=True, settings=_settings(), write=False)
    assert len(fake.calls) == 2 and fake.calls[1][1].route is None
    # The flag off: prepare builds no caller and makes no call.
    se.prepare(None, run, None, allow_llm=True, settings=_settings(ai_scope_attrib_llm=False), write=False)
    assert len(fake.calls) == 2


def test_real_complete_turn_resolves_the_scope_attrib_step(monkeypatch):
    """The fallback request through the real `llm.complete_turn` with only the
    guarded transport scripted: the route is the scope_attrib step (it follows
    the main route's provider and model at its own effort)."""
    from app.services import plans_reader_model_routes as model_routes

    seen: dict[str, Any] = {}

    class _Client:
        def with_options(self, **_k: Any) -> _Client:
            return self

    def guarded(feature, route, s, call, request=None):
        seen.update(feature=feature, route=route, request=request)
        return _turn({"items": []})

    monkeypatch.setattr(llm, "_guarded", guarded)
    monkeypatch.setattr(llm, "_client_for", lambda route, s: _Client())
    settings = _settings().model_copy(update={"openai_api_key": "sk-test-not-a-key"})
    caller = sa.default_llm_caller(settings)
    assert caller("system text", "Statements:\n1. [owner_exhibit] synthetic", sa.FALLBACK_SCHEMA) == {"items": []}
    expected = model_routes.resolve_step("scope_attrib", settings)
    main = model_routes.resolve_step("main", settings)
    assert seen["feature"] == "plans_scope_attrib"
    assert seen["route"].step == "scope_attrib" and seen["route"].provider == expected.provider == main.provider
    assert seen["route"].model == expected.model == main.model
    assert seen["request"]["req"].step == "scope_attrib"


# Wiring: derive step 1 and the main digest ------------------------------------------------------


def test_run_derive_calls_prepare_with_allow_llm_from_the_flag(monkeypatch):
    from app.services import plans_reader_runs as runs

    seen: list[bool] = []

    def fake_prepare(sb, run, outputs=None, *, allow_llm=False, settings=None, write=True):
        seen.append(allow_llm)
        return {"digest": "G3 SCOPE DECISIONS"}

    monkeypatch.setattr(se, "prepare", fake_prepare)
    monkeypatch.setattr(runs, "_latest_output", lambda sb, run_id, scope, category=None: {"version": 3})
    monkeypatch.setattr(runs, "_build_measure_output", lambda sb, run, group, policy_ctx, s, output=None: 1)
    run = {"id": "22222222-2222-4222-8222-222222222222"}
    for flag in (True, False):
        monkeypatch.setattr(runs, "get_settings", lambda flag=flag: _settings(ai_scope_attrib_llm=flag))
        out = runs.run_derive(None, run, {"id": "g"})
        assert out["policy_version"] == 3 and out["warnings"] == []
    assert seen == [True, False]


def test_run_derive_survives_a_failed_prepare(monkeypatch):
    from app.services import plans_reader_runs as runs

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("synthetic prepare failure")

    measured: list[Any] = []
    monkeypatch.setattr(se, "prepare", boom)
    monkeypatch.setattr(runs, "get_settings", lambda: _settings())
    monkeypatch.setattr(runs, "_build_measure_output",
                        lambda sb, run, group, policy_ctx, s, output=None: measured.append(policy_ctx) or 2)
    out = runs.run_derive(None, {"id": "33333333-3333-4333-8333-333333333333"}, {"id": "g"})
    assert out["measure_version"] == 2 and measured == [None]
    assert out["warnings"] and out["warnings"][0].startswith("scope_prepare_failed: RuntimeError")


def test_main_digest_comes_from_the_stored_scope_context(monkeypatch):
    from app.services import plans_reader_agents as agents

    ctx = se.prepare_context(_inputs(_uncertain_sentences(2)), policy=scope_policy.seed_policy(),
                             settings=_settings(ai_scope_attrib_llm=False), allow_llm=False)
    assert ctx["digest"].startswith("G3 SCOPE DECISIONS")
    monkeypatch.setattr(se, "context_for_run", lambda sb, run_id: ctx)
    assert agents._scope_digest(None, {"id": "r"}) == ctx["digest"]
    monkeypatch.setattr(se, "context_for_run", lambda sb, run_id: None)
    assert agents._scope_digest(None, {"id": "r"}) is None


def test_fallback_prompt_is_in_the_prompt_hash():
    from app.services import plans_reader_prompt_hash as ph

    parts = ph.registered_parts()
    assert parts.get("scope_attribution.FALLBACK_SYSTEM") == sa.FALLBACK_SYSTEM
    assert "scope_attribution.FALLBACK_SCHEMA" in parts
