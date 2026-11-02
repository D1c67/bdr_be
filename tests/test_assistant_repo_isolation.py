"""Isolation of the assistant's own tables (docs/ASSISTANT.md, Permissions
item 14, Summary):

  user_id   every function in app/assistant/repo.py takes user_id first;
  tables    no module outside repo.py (and the delegation check the design
            places in principal.py) queries an assistant_* table or calls
            the ledger RPC;
  tools     nothing under app/assistant/tools imports the Supabase client:
            a tool that touched the database directly would bypass every
            route-level authorization;
  client    deny by default (phase 2): no module under app/assistant names
            get_supabase or supabase_client except repo.py, principal.py and
            evals/run_evals.py (continue_job and spend included);
  sweep     the system-wide staging sweep (user_id None) has exactly the
            callers docs/ASSISTANT_PHASE2_PLAN.md 3.1.4 names, and every
            phase 3 system sweep (docs/ASSISTANT_PHASE3_PLAN.md, WI-3) is
            called with None only from the scheduler and the maintenance
            riders;
  main      app/main.py still never imports app.assistant.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from app.assistant import repo

APP = Path(repo.__file__).resolve().parents[1]
# The eval harness is a dev-only test runner, never imported by the app: the
# design has it take assistant_eval_lock and read the ledger rows of its own
# runs with the dev service key (docs/ASSISTANT.md, Eval harness). Its
# message, conversation and tool-call reads are scoped to the eval account's
# user_id.
ALLOWED_TABLE_USERS = {
    APP / "assistant" / "repo.py",
    APP / "assistant" / "principal.py",
    APP / "assistant" / "evals" / "run_evals.py",
}
_TABLE_RE = re.compile(r"""\.table\(\s*["']assistant_""")
_CONST_RE = re.compile(r"""["']assistant_[a-z_]+["']""")
_RPC_RE = re.compile(r"assistant_tool_call_begin")


def _py_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_every_repo_function_takes_user_id_first():
    """Every function that reaches the database (public or private: its
    source calls the client) takes user_id first. The handful of pure
    helpers (timestamp and Decimal formatting, the row unwrapper, the client
    accessor itself) touch no table."""
    offenders = []
    checked = 0
    for name, fn in inspect.getmembers(repo, inspect.isfunction):
        if fn.__module__ != repo.__name__:
            continue
        source = inspect.getsource(fn)
        if name == "_sb" or ("_sb()" not in source and ".table(" not in source and ".rpc(" not in source):
            continue
        checked += 1
        params = list(inspect.signature(fn).parameters)
        if not params or params[0] != "user_id":
            offenders.append(name)
    assert checked >= 20
    assert offenders == [], f"repo functions without user_id first: {offenders}"


def test_no_other_module_queries_assistant_tables():
    offenders = []
    for path in _py_files(APP):
        if path in ALLOWED_TABLE_USERS:
            continue
        text = path.read_text(encoding="utf-8")
        if _TABLE_RE.search(text) or _RPC_RE.search(text):
            offenders.append(str(path.relative_to(APP)))
            continue
        # A table-name constant next to a .table( call is the same thing.
        if ".table(" in text and _CONST_RE.search(text) and "assistant" in path.parts:
            offenders.append(str(path.relative_to(APP)))
    assert offenders == [], f"assistant_* tables touched outside repo.py: {offenders}"


def test_tools_never_import_the_database_client():
    offenders = []
    for path in _py_files(APP / "assistant" / "tools"):
        text = path.read_text(encoding="utf-8")
        if "supabase_client" in text or "get_supabase" in text or ".table(" in text:
            offenders.append(path.name)
    assert offenders == []


def test_no_assistant_module_but_the_allowed_three_names_the_client():
    """Deny by default: a new module under app/assistant (continue_job,
    spend, anything later) reaches the database only through repo.py."""
    offenders = []
    scanned = 0
    for path in _py_files(APP / "assistant"):
        if path in ALLOWED_TABLE_USERS:
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8")
        if "get_supabase" in text or "supabase_client" in text:
            offenders.append(str(path.relative_to(APP)))
    assert scanned >= 15
    assert offenders == [], f"modules under app/assistant that name the Supabase client: {offenders}"


def _calls(name: str):
    """(module path, enclosing top-level function, call node) for every call
    of a function or method named `name` under app/."""
    for path in _py_files(APP):
        text = path.read_text(encoding="utf-8")
        if name not in text:
            continue
        tree = ast.parse(text)
        for top in tree.body:
            for node in ast.walk(top):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if called == name:
                    yield str(path.relative_to(APP)), getattr(top, "name", "<module>"), node


def _passes_none_user(call: ast.Call) -> bool:
    if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value is None:
        return True
    return any(k.arg == "user_id" and isinstance(k.value, ast.Constant) and k.value.value is None
               for k in call.keywords)


def test_the_system_staging_sweep_has_only_the_planned_callers():
    """staged_objects_to_purge is called only from purge_expired (with the
    caller's user_id or None), and purge_expired(user_id=None, ...) only
    from assistant_staging.stage, continue_job.run_continue and the
    maintenance staging rider (phase 3, 3.5 item 3)."""
    to_purge = {(mod, fn) for mod, fn, _ in _calls("staged_objects_to_purge")}
    assert to_purge == {("services/assistant_staging.py", "purge_expired")}, to_purge
    system = {(mod, fn) for mod, fn, call in _calls("purge_expired") if _passes_none_user(call)}
    allowed = {("services/assistant_staging.py", "stage"), ("assistant/continue_job.py", "run_continue"),
               ("services/assistant_maintenance.py", "run_staging_sweep")}
    assert ("services/assistant_staging.py", "stage") in system
    assert system <= allowed, system - allowed


# Phase 3 system sweeps (docs/ASSISTANT_PHASE3_PLAN.md 3.4.2 and 3.5): the
# repo functions that take user_id None as "every user", and the only
# (module, top-level function) pairs allowed to pass that None.
_SCHEDULER = "services/assistant_scheduler.py"
_MAINTENANCE = "services/assistant_maintenance.py"
_PROACTIVE = "services/assistant_proactive.py"
_REPO = "assistant/repo.py"
_DELIVERY = "services/assistant_delivery.py"
_INBOX_ROUTES = "routers/assistant_inbox.py"
SYSTEM_SWEEP_CALLERS = {
    "schedules_to_refill": {(_SCHEDULER, "refill")},
    "claim_due_schedules": {(_SCHEDULER, "poll_once")},
    "stale_runs": {(_SCHEDULER, "reap")},
    "paused_to_check": {(_SCHEDULER, "resume_pass")},
    # Phase 7: the router's approval-expiry scan reads the same sweep (6.4).
    "pending_approvals_due": {(_MAINTENANCE, "run_approvals_sweep"), (_PROACTIVE, "scan_approvals")},
    "pending_approvals_with_project": {(_MAINTENANCE, "run_approvals_sweep")},
    "approved_stranded": {(_MAINTENANCE, "run_approvals_sweep")},
    # _claim_today is the claim both riders share (records the day only
    # once claim_sweep answered, OFB-8).
    "claim_sweep": {(_MAINTENANCE, "_claim_today")},
    "finish_sweep": {(_MAINTENANCE, "run_nightly"), (_MAINTENANCE, "run_orphans")},
    "memories_to_auto_archive": {(_MAINTENANCE, "run_nightly")},
    # Paged by run_nightly's helper (OFB-5).
    "project_notes_for_review": {(_MAINTENANCE, "_review_project_notes")},
    "memories_to_purge": {(_MAINTENANCE, "run_nightly")},
    "tool_results_to_purge": {(_MAINTENANCE, "run_nightly")},
    "owners_with_open_state": {(_MAINTENANCE, "run_orphans")},
    # Phase 7 state the orphan sweep also reads (6.8): same one caller.
    "phase7_owners_with_open_state": {(_MAINTENANCE, "run_orphans")},
    "open_split_tasks": {(_MAINTENANCE, "run_tasks_sweep")},
}


def test_every_system_sweep_takes_an_optional_user_id_first():
    for name in SYSTEM_SWEEP_CALLERS:
        fn = getattr(repo, name)
        param = next(iter(inspect.signature(fn).parameters.values()))
        assert param.name == "user_id" and "None" in str(param.annotation), name
        assert "system sweep" in (fn.__doc__ or "").lower() or "system-wide" in (fn.__doc__ or ""), name


def test_the_phase3_system_sweeps_have_only_the_planned_callers():
    """A None user_id reaches a phase 3 sweep only from the scheduler's
    refill, poll_once, reap and resume_pass and the maintenance riders'
    run_approvals_sweep, run_nightly and run_orphans (and their helpers
    _claim_today and _review_project_notes). Every other caller
    passes a person's id."""
    offenders = {}
    for name, allowed in SYSTEM_SWEEP_CALLERS.items():
        system = {(mod, fn) for mod, fn, call in _calls(name) if _passes_none_user(call)}
        if not system <= allowed:
            offenders[name] = sorted(system - allowed)
    assert offenders == {}, offenders


# Phase 7 system sweeps (docs/ASSISTANT_PHASE7_PLAN.md 6, 8.5, 9.3, 12.3): the
# repo functions of the proactive router that take user_id None as "every
# user", and the only (module, top-level function) pairs allowed to pass that
# None. The router's _Context class carries the cached role-subscription and
# project-mute reads. scan_approvals reaches pending_approvals_due: it is
# pinned in SYSTEM_SWEEP_CALLERS above. The feedback report is the one
# request handler allowed a None system read.
PHASE7_SYSTEM_SWEEP_CALLERS = {
    "insert_events": {(_PROACTIVE, "run_tail"), (_PROACTIVE, "run_slot_sweeper"), (_PROACTIVE, "scan_approvals")},
    "events_by_dedupe_keys": {(_PROACTIVE, "run_slot_sweeper")},
    "events_claim": {(_PROACTIVE, "run_slot_sweeper"), (_PROACTIVE, "_route_step"), (_PROACTIVE, "_skip_step")},
    "event_finish": {(_PROACTIVE, "_route_and_finish"), (_PROACTIVE, "_skip_step")},
    "router_cursors": {(_PROACTIVE, "run_tail"), (_PROACTIVE, "reseed_tails"), (_PROACTIVE, "_lag_guard")},
    "advance_router_cursor": {(_PROACTIVE, "run_tail")},
    "reseed_router_cursors": {(_PROACTIVE, "reseed_tails")},
    # The slot writers read the slot row as the system inside repo.py (they
    # are system-only themselves, so the read never widens a person's scope).
    "proactive_slot": {(_PROACTIVE, "run_slot_sweeper"), (_PROACTIVE, "deliver_digests"), (_PROACTIVE, "run_purge"),
                       (_REPO, "claim_proactive_slot"), (_REPO, "record_proactive_slot"),
                       (_REPO, "finish_proactive_slot")},
    "claim_proactive_slot": {(_PROACTIVE, "run_slot_sweeper"), (_PROACTIVE, "run_purge")},
    "record_proactive_slot": {(_PROACTIVE, "run_slot_sweeper")},
    "finish_proactive_slot": {(_PROACTIVE, "run_slot_sweeper"), (_PROACTIVE, "run_purge")},
    "role_subscriptions": {(_PROACTIVE, "_Context")},
    "project_muted_user_ids": {(_PROACTIVE, "_Context")},
    "inbox_due_immediate": {(_PROACTIVE, "release_held")},
    "inbox_folded_pending": {(_PROACTIVE, "release_held")},
    "inbox_pending_digest_users": {(_PROACTIVE, "deliver_digests")},
    "proactive_purge": {(_PROACTIVE, "run_purge")},
    "external_sends_claim": {(_DELIVERY, "send_due")},
    "external_send_settle": {(_DELIVERY, "send_due")},
    "inbox_feedback_report": {(_INBOX_ROUTES, "feedback_report")},
}
# The offboarding wrappers (6.8) take a person's id and have one caller.
PHASE7_OFFBOARD_WRAPPERS = (
    "offboard_dismiss_undelivered", "offboard_skip_pending_sends", "offboard_delete_subscriptions",
    "offboard_delete_project_mutes",
)


def test_the_phase7_system_sweeps_take_an_optional_user_id_first():
    for name in PHASE7_SYSTEM_SWEEP_CALLERS:
        fn = getattr(repo, name)
        param = next(iter(inspect.signature(fn).parameters.values()))
        assert param.name == "user_id" and "None" in str(param.annotation), name
        assert "system sweep" in (fn.__doc__ or "").lower() or "system-wide" in (fn.__doc__ or ""), name


def test_the_phase7_system_sweeps_have_only_the_planned_callers():
    """A None user_id reaches a phase 7 sweep only from the router service
    (assistant_proactive), the sender (assistant_delivery.send_due) and the
    feedback_report route, which is the one request handler allowed a None
    system read (9.3). Every route, tool and the offboarding hook pass a
    person's id."""
    offenders, found = {}, 0
    for name, allowed in PHASE7_SYSTEM_SWEEP_CALLERS.items():
        system = {(mod, fn) for mod, fn, call in _calls(name) if _passes_none_user(call)}
        found += len(system)
        if not system <= allowed:
            offenders[name] = sorted(system - allowed)
    assert found >= 20
    assert offenders == {}, offenders


def test_the_scan_approvals_sweep_is_the_router_reader_of_pending_approvals_due():
    system = {(mod, fn) for mod, fn, call in _calls("pending_approvals_due") if _passes_none_user(call)}
    assert (_PROACTIVE, "scan_approvals") in system


def test_the_offboarding_wrappers_are_called_only_by_the_phase7_hook():
    """repo.offboard_* take a person's id (never None) and are reached only
    from assistant_offboarding.phase7_offboard (6.8)."""
    for name in PHASE7_OFFBOARD_WRAPPERS:
        fn = getattr(repo, name)
        param = next(iter(inspect.signature(fn).parameters.values()))
        assert param.name == "user_id" and "None" not in str(param.annotation), name
        callers = set()
        for mod, top, call in _calls(name):
            callers.add((mod, top))
            assert not _passes_none_user(call), name
        # The hook names each wrapper as a value (a step table), not a call.
        assert callers == set(), (name, callers)
    text = (APP / "services" / "assistant_offboarding.py").read_text(encoding="utf-8")
    for name in PHASE7_OFFBOARD_WRAPPERS:
        assert f"repo.{name}" in text, name
    tree = ast.parse(text)
    hook = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "phase7_offboard")
    named = {n.attr for n in ast.walk(hook) if isinstance(n, ast.Attribute)}
    assert set(PHASE7_OFFBOARD_WRAPPERS) <= named
    elsewhere = [
        str(path.relative_to(APP)) for path in _py_files(APP)
        if path != APP / "services" / "assistant_offboarding.py" and path != APP / "assistant" / "repo.py"
        and any(f"repo.{name}" in path.read_text(encoding="utf-8") for name in PHASE7_OFFBOARD_WRAPPERS)
    ]
    assert elsewhere == [], elsewhere


# Phase 4 system sweeps (docs/ASSISTANT_PHASE4_PLAN.md 3.5.4 and 3.9.3): the
# repo functions that take user_id None as "every user", and the only
# modules allowed to pass that None. The riders are named by WI-11, so this
# pins the module (the maintenance riders) rather than a function name.
PHASE4_SYSTEM_SWEEP_MODULES = {
    "open_ae_tasks": {_MAINTENANCE},
    "reports_to_purge": {_MAINTENANCE},
}


def test_the_phase4_system_sweeps_take_an_optional_user_id_first():
    for name in PHASE4_SYSTEM_SWEEP_MODULES:
        fn = getattr(repo, name)
        param = next(iter(inspect.signature(fn).parameters.values()))
        assert param.name == "user_id" and "None" in str(param.annotation), name
        assert "system sweep" in (fn.__doc__ or "").lower(), name


def test_the_phase4_system_sweeps_have_only_the_maintenance_riders_as_callers():
    """A None user_id reaches open_ae_tasks and reports_to_purge only from
    the maintenance riders (ae_task_sweep and report_purge). The lazy task
    finalize, the offboarding hook and every route pass a person's id."""
    offenders = {}
    for name, modules in PHASE4_SYSTEM_SWEEP_MODULES.items():
        system = {mod for mod, _fn, call in _calls(name) if _passes_none_user(call)}
        if not system <= modules:
            offenders[name] = sorted(system - modules)
    assert offenders == {}, offenders


def test_the_evidence_log_is_only_ever_inserted():
    """assistant_evidence_access is append-only (0180 B4): repo.py names it
    for an upsert that ignores duplicates and for reads, and no repo
    function updates or deletes it."""
    checked = 0
    for name, fn in inspect.getmembers(repo, inspect.isfunction):
        if fn.__module__ != repo.__name__:
            continue
        text = inspect.getsource(fn)
        if "EVIDENCE_TABLE" not in text:
            continue
        checked += 1
        flat = re.sub(r"\s+", "", text)
        assert ".update(" not in flat and ".delete(" not in flat, name
    assert checked >= 2


def test_main_never_imports_the_assistant_package():
    text = (APP / "main.py").read_text(encoding="utf-8")
    assert "app.assistant" not in text
    assert "routers.assistant" not in text or "# " in text.split("routers.assistant")[0].splitlines()[-1]


def test_no_em_dash_in_the_package():
    em = chr(0x2014)
    offenders = []
    for path in _py_files(APP / "assistant"):
        if em in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    for path in (APP / "assistant").rglob("*.md"):
        if em in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    for path in (APP / "assistant").rglob("*.json"):
        if em in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert em not in (APP / "routers" / "assistant.py").read_text(encoding="utf-8")
    assert offenders == []
