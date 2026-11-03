"""Phase 8 triage source pins (docs/ASSISTANT_PHASE8_PLAN.md 15.3, 16.1; WI-7).

What triage must never be able to do, pinned on the source:

  service   app/services/assistant_triage.py never names the model service,
            llm_messages, the turn loop, complete_messages, complete_json, a
            sender (graph_email, teams_alerts, send_mail, the router's
            delivery module) or a delegation helper (live_delegation,
            owner_check, ensure_delegation); it passes the no-model scan of
            tests/test_assistant_no_model_unattended.py (wave D adds it to that
            scan list, a bdr-94 file); every spend call it makes names the
            company spend id; it never calls the A4 retry helper directly;
  model     app/services/assistant_triage_model.py is the only module that
            calls a model function with the `assistant_triage` feature; it
            never names a sender, the turn loop, jobs, the executor or the
            prompts; it passes spend.COMPANY_SPEND_ID (as `company`), never a
            user_id, to spend.reserve, spend.settle and llm_messages.attributed;
  routers   no router module imports the triage service except to read its
            `health()` for the admin block (routers/assistant_inbox.py, plan
            8.1); none imports the model module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.services import assistant_triage as svc
from app.services import assistant_triage_model as model

from tests import test_assistant_no_model_unattended as no_model

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SERVICE = Path(svc.__file__)
MODEL = Path(model.__file__)

SENDERS = (r"\bgraph_email\b", r"\bteams_alerts\b", r"\bsend_mail\b", r"\bassistant_delivery\b",
           r"\bnotification_email\b")
SERVICE_FORBIDDEN = (
    r"\bservices\.llm\b(?!_)", r"\bllm_messages\b", r"\bapp\.assistant\.loop\b", r"\bcomplete_messages\b",
    r"\bcomplete_json\b", r"\blive_delegation\b", r"\bowner_check\b", r"\bensure_delegation\b",
    r"\bllm_gate\b", r"\bassistant_triage_item_retry\b", *SENDERS,
)
MODEL_FORBIDDEN = (*SENDERS, r"\bapp\.assistant\.(?:loop|jobs|executor|prompts)\b")
SPEND_CALLS = {("spend", "reserve"), ("spend", "settle"), ("llm_messages", "attributed")}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_text(path))


def _imports(tree: ast.Module) -> set[str]:
    """Every imported dotted name: `from a.b import c` gives a.b.c."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def _calls(tree: ast.Module, owner: str, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == name and isinstance(n.func.value, ast.Name) and n.func.value.id == owner]


# ── The service ──────────────────────────────────────────────────────────────


def test_the_service_names_no_model_call_sender_or_delegation_helper():
    text = _text(SERVICE)
    hits = [p for p in SERVICE_FORBIDDEN if re.search(p, text)]
    assert hits == []


def test_the_service_passes_the_no_model_scan():
    text = _text(SERVICE)
    assert [p.pattern for p in no_model.FORBIDDEN_TEXT if p.search(text)] == []
    imported = _imports(_tree(SERVICE))
    banned = {"app.assistant.loop", "app.services.llm", "app.assistant.jobs", "app.assistant.executor",
              "app.assistant.prompts", "app.assistant.llm_messages", "app.services.llm_gate",
              "app.services.graph_email", "app.services.teams_alerts", "app.services.assistant_delivery"}
    assert imported & banned == set()
    # The model module is reached only through its public call and exception set.
    used = {n.attr for n in ast.walk(_tree(SERVICE)) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "assistant_triage_model"}
    allowed = {"classify", "ALL", "BREAKER"} | {cls.__name__ for cls in model.ALL}
    assert used and used <= allowed


def test_every_spend_call_of_the_service_names_the_company_id():
    tree = _tree(SERVICE)
    assert not _calls(tree, "spend", "reserve")
    settles = _calls(tree, "spend", "settle")
    assert settles
    for call in settles:
        first = call.args[0]
        assert isinstance(first, ast.Attribute) and first.attr == "COMPANY_SPEND_ID"
        assert not any(k.arg == "user_id" for k in call.keywords)


def test_the_service_claims_with_the_allowlist_and_both_fuse_settings():
    calls = _calls(_tree(SERVICE), "repo", "triage_claim")
    assert len(calls) == 1
    call = calls[0]
    assert {k.arg for k in call.keywords} == {"user_ids"} and len(call.args) == 8
    assert [a.id for a in call.args[-2:] if isinstance(a, ast.Name)] == ["ratio", "per_hour"]
    # The enqueue runs through _step(out, "enqueued", repo.triage_enqueue, ..., user_ids=allowlist).
    steps = [n for n in ast.walk(_tree(SERVICE)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_step" and len(n.args) > 2 and isinstance(n.args[2], ast.Attribute)
             and n.args[2].attr == "triage_enqueue"]
    assert len(steps) == 1
    kw = {k.arg: k.value for k in steps[0].keywords}
    assert isinstance(kw.get("user_ids"), ast.Name) and kw["user_ids"].id == "allowlist"


# ── The model module ─────────────────────────────────────────────────────────


def test_only_the_model_module_calls_a_model_function_with_the_triage_feature():
    assert model.FEATURE == "assistant_triage"
    offenders = []
    for path in APP.rglob("*.py"):
        tree = _tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "assistant_triage":
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    feature_calls = [n for n in ast.walk(_tree(MODEL)) if isinstance(n, ast.Call) and n.args
                     and isinstance(n.args[0], ast.Name) and n.args[0].id == "FEATURE"]
    assert {n.func.attr for n in feature_calls if isinstance(n.func, ast.Attribute)} == {"resolve", "complete_json"}


def test_the_model_module_names_no_sender_loop_jobs_executor_or_prompts():
    text = _text(MODEL)
    assert [p for p in MODEL_FORBIDDEN if re.search(p, text)] == []
    imported = _imports(_tree(MODEL))
    for name in ("loop", "jobs", "executor", "prompts"):
        assert f"app.assistant.{name}" not in imported
    assert not any(i.startswith(("app.services.graph_email", "app.services.teams_alerts",
                                 "app.services.assistant_delivery")) for i in imported)


def test_the_model_module_pays_with_the_company_id_never_a_user_id():
    tree = _tree(MODEL)
    assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "company" for t in n.targets)]
    assert len(assigned) == 1
    value = assigned[0].value
    assert isinstance(value, ast.Attribute) and value.attr == "COMPANY_SPEND_ID"
    assert isinstance(value.value, ast.Name) and value.value.id == "spend"
    seen = set()
    for owner, name in SPEND_CALLS:
        for call in _calls(tree, owner, name):
            seen.add((owner, name))
            assert call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "company"
            assert not any(k.arg == "user_id" for k in call.keywords)
    assert seen == SPEND_CALLS
    settles = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_settle"]
    assert settles and all(isinstance(c.args[0], ast.Name) and c.args[0].id == "company" for c in settles)


def test_the_run_usage_is_read_in_the_model_module_only_and_passed_to_the_finish():
    """WI7-1: the tokens and cost come from llm.last_usage() / last_cost_usd()
    inside the model module's `_call_usage` only; the service never reads
    them itself (it never names services.llm) and hands classify's usage to
    every finish that follows a model call."""
    reads = {}
    for fn in ast.walk(_tree(MODEL)):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "llm"
                        and n.attr in ("last_usage", "last_cost_usd")):
                    reads.setdefault(n.attr, set()).add(fn.name)
    assert reads == {"last_usage": {"_call_usage"}, "last_cost_usd": {"_call_usage"}}
    service = _text(SERVICE)
    assert "last_usage" not in service and "last_cost_usd" not in service
    tree = _tree(SERVICE)
    run_batch = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_batch")
    attrs = {n.attr for n in ast.walk(run_batch) if isinstance(n, ast.Attribute)
             and isinstance(n.value, ast.Name) and n.value.id == "classified"}
    assert attrs == {"answer", "usage"}


# ── Routers ──────────────────────────────────────────────────────────────────


def test_routers_never_import_the_triage_service_except_for_its_health():
    for path in (APP / "routers").rglob("*.py"):
        tree = _tree(path)
        rel = str(path.relative_to(ROOT))
        imported = _imports(tree)
        assert not any(i.startswith("app.services.assistant_triage_model") for i in imported), rel
        assert not any(i.startswith("app.services.assistant_triage.") for i in imported), rel
        if "app.services.assistant_triage" not in imported:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "assistant_triage"):
                assert node.attr == "health", rel
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                    and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "assistant_triage"):
                assert isinstance(node.args[1], ast.Constant) and node.args[1].value == "health", rel


def test_nothing_in_app_calls_the_retry_helper_directly():
    offenders = [str(p.relative_to(ROOT)) for p in APP.rglob("*.py")
                 if "assistant_triage_item_retry" in _text(p)]
    assert offenders == []


def test_the_wi7_files_carry_no_em_or_en_dash():
    files = (SERVICE, Path(__file__), ROOT / "tests" / "test_assistant_triage_service.py",
             ROOT / "scripts" / "check_triage_claims.py")
    for path in files:
        if path.exists():
            assert chr(0x2014) not in _text(path) and chr(0x2013) not in _text(path), path.name
