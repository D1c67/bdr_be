"""Phase 7 no-model pins (docs/ASSISTANT_PHASE7_PLAN.md 6, 8.5, 11.1).

The proactive router and its sender are code over rows: they call no model.
An AST scan of every import (module level and lazy, `import x`, `from x
import y` and `from . import y`) pins that app/services/assistant_proactive.py,
app/services/assistant_delivery.py and app/assistant/proactive.py import
nothing from the model service (app.services.llm and its llm_* helpers), the
llm queue, the provider SDKs or the assistant turn modules (loop, llm_messages,
prompts, continue_job). The source scan of the sender names (notifications
deliver, notify_role(s), notify_user, notification_email.queue,
teams_alerts.queue, queue_report) lives in test_assistant_proactive_service.py
(test_scanned_files_never_call_a_bdr_sender) and is pinned here only for the
offboarding hook, the one router module that sits outside that list."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.services import assistant_offboarding

ROOT = Path(assistant_offboarding.__file__).resolve().parents[2]
ROUTER_MODULES = (
    "app/services/assistant_proactive.py",
    "app/services/assistant_delivery.py",
    "app/assistant/proactive.py",
)
# Dotted module prefixes a router module may never import.
FORBIDDEN_MODULES = (
    "app.services.llm",            # also llm_queue, llm_anthropic, llm_gate and the other llm_* helpers
    "openai",
    "anthropic",
    "app.assistant.loop",
    "app.assistant.llm_messages",
    "app.assistant.prompts",
    "app.assistant.continue_job",
)
# Names a `from app.services import ...` or `from app.assistant import ...` may not bring in.
FORBIDDEN_FROM_PACKAGE = {
    "app.services": ("llm", "llm_queue", "llm_anthropic", "llm_gate", "llm_target", "llm_parts", "llm_health",
                     "llm_cassette", "llm_errors", "llm_pricing", "plans_reader_toolloop"),
    "app.assistant": ("loop", "llm_messages", "prompts", "continue_job"),
}
SENDER_NAMES = re.compile(
    r"notifications\.deliver|from app\.services\.notifications import|\bnotify_roles?\b|\bnotify_user\b"
    r"|notification_email\.queue|teams_alerts\.queue|\bqueue_report\b"
)


def _imported_modules(rel: str) -> set[str]:
    """Every dotted module (and package.name for a from-import) the file imports."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            out.add(base)
            out.update(f"{base}.{alias.name}" for alias in node.names)
    return out


def _is_forbidden(dotted: str) -> bool:
    return any(dotted == m or dotted.startswith(m + ".") or dotted.startswith(m + "_") for m in FORBIDDEN_MODULES)


def test_the_router_modules_import_no_model_service_sdk_queue_or_turn_module():
    offenders = {}
    for rel in ROUTER_MODULES:
        imported = _imported_modules(rel)
        bad = sorted(d for d in imported if _is_forbidden(d))
        for package, names in FORBIDDEN_FROM_PACKAGE.items():
            bad += sorted(f"{package}.{n}" for n in names if f"{package}.{n}" in imported)
        if bad:
            offenders[rel] = sorted(set(bad))
    assert offenders == {}, offenders


def test_the_router_modules_import_no_relative_model_module():
    for rel in ROUTER_MODULES:
        for dotted in _imported_modules(rel):
            if dotted.startswith("."):
                assert not re.search(r"\b(llm\w*|loop|prompts|continue_job|openai|anthropic)\b", dotted), (rel, dotted)


def test_the_router_modules_name_no_model_call():
    """Text-level backstop for an import form the scan above cannot see
    (importlib, a string import)."""
    banned = ("llm_messages", "complete_messages", "llm_queue", "app.assistant.loop", "app.services.llm",
              "importlib", "__import__", "openai", "anthropic")
    for rel in ROUTER_MODULES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for name in banned:
            assert name not in text, (rel, name)


def test_the_scan_sees_the_imports_it_is_meant_to_see():
    """Guard against a scanner that finds nothing: the router service does
    import its delivery module, the pure rules module and repo."""
    imported = _imported_modules("app/services/assistant_proactive.py")
    assert "app.services.assistant_delivery" in imported and "app.assistant.repo" in imported
    assert "app.assistant.proactive" in imported
    assert _is_forbidden("app.services.llm_queue") and _is_forbidden("app.services.llm")
    assert not _is_forbidden("app.services.assistant_delivery")


def test_the_offboarding_hook_names_no_bdr_sender():
    """phase7_offboard (6.8) sends nothing: it only dismisses, skips and deletes."""
    text = (ROOT / "app/services/assistant_offboarding.py").read_text(encoding="utf-8")
    start = text.index("def phase7_offboard")
    hook = text[start:text.index("HOOKS.append(phase7_offboard)")]
    assert SENDER_NAMES.search(hook) is None
    assert "graph_email" not in hook and "teams_alerts" not in hook
