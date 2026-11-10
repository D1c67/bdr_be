"""No model runs unattended in phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.13,
deviation D4).

Recipes are code over the due digest, the scheduler and the maintenance
riders call no model, and offboarding calls none either; the phase 7 router,
its sender and its rules (docs/ASSISTANT_PHASE7_PLAN.md 12.3) call none. A source scan pins
it: none of these modules names the model message builder, the turn loop,
the model service or its completion call, in any import form.

Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.5, owner answer 1 of
2026-11-02, the D4 change in docs/ASSISTANT.md decisions log 22): the one
model call not started by a user message is the automatic follow-up turn
after a browser task finishes, the third named turn mode browser_result.
The browser chat writer that enqueues it is on the scan list too: it only
inserts the pending message and the assistant_turn job; the turn loop runs
it as one model call with no tool offered, at most once per user message
chain.

Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md D1, decisions log entry 24): triage
makes one unattended model call per batch of items, not per item. assistant_triage is scanned so it
never names the model service, the turn loop or the message builder itself;
its one model call goes only through assistant_triage_model.classify.
tests/test_assistant_triage_pins.py pins that only assistant_triage_model
makes that call."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.assistant import proactive, recipes, registry
from app.services import (
    assistant_browser_chat,
    assistant_delivery,
    assistant_maintenance,
    assistant_offboarding,
    assistant_proactive,
    assistant_recipes,
    assistant_scheduler,
    assistant_triage,
)

MODULES = (
    assistant_scheduler, assistant_maintenance, assistant_recipes, recipes, assistant_offboarding,
    assistant_proactive, assistant_delivery, proactive, assistant_browser_chat,
    assistant_triage,
)
FORBIDDEN_TEXT = (
    re.compile(r"\bllm_messages\b"),
    re.compile(r"\bapp\.assistant\.loop\b"),
    re.compile(r"\bservices\.llm\b(?!_)"),
    re.compile(r"\bcomplete_messages\b"),
)


def _source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def test_no_unattended_module_names_a_model_call():
    offenders = {}
    for module in MODULES:
        text = _source(module)
        hits = [p.pattern for p in FORBIDDEN_TEXT if p.search(text)]
        if hits:
            offenders[module.__name__] = hits
    assert offenders == {}


def test_no_unattended_module_imports_the_loop_or_the_model_service():
    for module in MODULES:
        tree = ast.parse(_source(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names}
                if node.module == "app.assistant":
                    assert "loop" not in names, module.__name__
                if node.module == "app.services":
                    assert "llm" not in names, module.__name__
                assert node.module not in ("app.assistant.loop", "app.services.llm"), module.__name__
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in ("app.assistant.loop", "app.services.llm"), module.__name__


def test_the_registry_has_exactly_three_turn_modes():
    modes = {v for k, v in vars(registry).items() if k.startswith("MODE_") and isinstance(v, str)}
    assert modes == {"normal", "mailbox_review", "browser_result"}


def test_the_third_mode_is_the_browser_follow_up_and_is_text_only():
    from app.assistant import loop

    assert registry.MODE_BROWSER_RESULT == assistant_browser_chat.FOLLOW_UP_ORIGIN == loop.FOLLOW_UP_ORIGIN
    # The writer enqueues the ordinary turn job; it never runs a turn itself.
    assert assistant_browser_chat.TURN_JOB_TYPE == "assistant_turn"
    assert loop.FOLLOW_UP_MAX_ROUNDS == 1   # owner answer 1: one text-only call
