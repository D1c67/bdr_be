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

import pytest

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
    # The phase 8 triage eval runner (docs/ASSISTANT_PHASE8_PLAN.md 16.4, WI-14a):
    # a dev-only runner like run_evals.py, which reads its own triage runs'
    # llm_call_log rows and refuses to run beside a claimed triage run.
    APP / "assistant" / "evals" / "run_triage_evals.py",
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


# ── Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 17.5, WI-18) ───────────────
#
# Every repo function of the browser v2 block, and the WI-18 functions beside
# the browser record retention, filters by owner: a table read or write carries
# the person's id, an RPC wrapper passes p_user_id, a system sweep refuses any
# user. The shared reads (the site registry, the platform config, the BDR
# record lookups) hold no per-user data and are named here on purpose.

_BV2_MARKER = "# ── Browser v2: sites, grants, admissions, credentials"
_BV2_WI18_MARKER = "# WI-18 (docs/ASSISTANT_BROWSER_V2_PLAN.md 17.5): browser v2 retention sweeps"
_P7_MARKER = "# ── Phase 7: the proactive router"
BV2_SHARED_READS = {
    "browser_platform_config_v2", "browser_sites", "browser_site", "browser_sites_by_keys",
    "browser_sites_for_url", "browser_url_email_sources", "browser_url_bdr_records", "browser_project_exists",
}
# A worker RPC fenced by the job lease (job, worker, boot, epoch), not by a user argument.
BV2_LEASE_FENCED = {"browser_download_record"}
BV2_OFFBOARD_WRAPPERS = ("browser_offboard_pins_delete", "browser_offboard_staged_downloads",
                         "browser_offboard_open_state")
# Callers of the WI-18 sweeps with a None user: the retention rider in the records
# service, and the orphan sweep for the open-state reader.
BV2_RETENTION_SWEEP_CALLERS = {
    "browser_holds_retention_delete": {("services/assistant_browser_records.py", "purge_v2_retention")},
    "browser_downloads_retention_delete": {("services/assistant_browser_records.py", "purge_v2_retention")},
    "browser_v2_owners_with_open_state": {(_MAINTENANCE, "run_orphans")},
}
_OWNER_FILTERS = ('.eq("user_id", user_id)', '.eq("principal_user_id", user_id)', "_scope(", "_bv2_delete_by_owner(")


def _v2_repo_functions() -> dict[str, ast.FunctionDef]:
    source = (APP / "assistant" / "repo.py").read_text(encoding="utf-8")
    spans = [(source.index(_BV2_MARKER), source.index(_P7_MARKER)),
             (source.index(_BV2_WI18_MARKER), source.index(_BV2_MARKER))]
    out: dict[str, ast.FunctionDef] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        offset = sum(len(line) + 1 for line in source.splitlines()[: node.lineno - 1])
        in_block = spans[0][0] <= offset < spans[0][1]
        # Between the WI-18 sweeps and the v2 marker sit the phase 6 functions: not pinned here.
        if in_block or (spans[1][0] <= offset < spans[1][1] and node.name in BV2_RETENTION_SWEEP_CALLERS):
            out[node.name] = node
    return out


def test_every_browser_v2_repo_function_filters_by_owner():
    source = (APP / "assistant" / "repo.py").read_text(encoding="utf-8")
    functions = _v2_repo_functions()
    assert len(functions) >= 55
    offenders = []
    for name, node in functions.items():
        if name in BV2_SHARED_READS or name in BV2_LEASE_FENCED:
            continue
        text = ast.get_source_segment(source, node)
        params = [a.arg for a in node.args.args]
        assert params and params[0] == "user_id", name
        optional = "None" in ast.unparse(node.args.args[0].annotation or ast.Constant(value=""))
        if optional:
            # A system sweep: it refuses a person's id (or, for the readers, scopes by it).
            if not ("raise ValueError" in text or "_bv2_system_only(" in text or "_scope(" in text):
                offenders.append((name, "system sweep without a user guard"))
            continue
        if ".table(" in text and not any(f in text for f in _OWNER_FILTERS):
            offenders.append((name, "table access without an owner filter"))
        if ".rpc(" in text or "_bv2_rpc(" in text or "browser_rpc(" in text:
            if not ('"p_user_id": user_id' in text or "user_id" in "".join(text.split("(", 1)[1:2])):
                offenders.append((name, "rpc without the owner"))
    assert offenders == [], offenders


def test_the_wi18_offboarding_wrappers_take_a_person_and_have_one_caller():
    functions = _v2_repo_functions()
    for name in BV2_OFFBOARD_WRAPPERS:
        fn = getattr(repo, name)
        param = next(iter(inspect.signature(fn).parameters.values()))
        assert param.name == "user_id" and "None" not in str(param.annotation), name
        assert name in functions, name
        callers = set()
        for mod, top, call in _calls(name):
            assert not _passes_none_user(call), name
            callers.add((mod, top))
        assert callers <= {("services/assistant_offboarding.py", "browser_v2_offboard"),
                           ("services/assistant_offboarding.py", "_purge_staged_downloads")}, (name, callers)
        assert callers, name


def test_the_wi18_retention_sweeps_take_an_optional_user_first_and_have_planned_callers():
    functions = _v2_repo_functions()
    found = 0
    for name, allowed in BV2_RETENTION_SWEEP_CALLERS.items():
        fn = getattr(repo, name)
        assert name in functions, name
        param = next(iter(inspect.signature(fn).parameters.values()))
        assert param.name == "user_id" and "None" in str(param.annotation), name
        assert "system sweep" in (fn.__doc__ or "").lower(), name
        system = {(mod, top) for mod, top, call in _calls(name) if _passes_none_user(call)}
        found += len(system)
        assert system <= allowed, (name, sorted(system - allowed))
        # Called with a person's id, never.
        assert {(m, t) for m, t, c in _calls(name) if not _passes_none_user(c)} == set(), name
    assert found >= 2


def test_the_browser_v2_block_adds_no_other_optional_user_function():
    """The optional-user functions of the whole v2 area are the four RPC sweeps
    of tests/test_assistant_repo_browser_v2.py plus the three WI-18 sweeps."""
    optional = {n for n, node in _v2_repo_functions().items()
                if "None" in ast.unparse(node.args.args[0].annotation or ast.Constant(value=""))}
    assert optional == {"browser_sweep_v2", "browser_purge_v2", "browser_outbox_drain", "browser_outbox_ack",
                        *BV2_RETENTION_SWEEP_CALLERS}, optional


# Behavior: with two people's rows in every table, each WI-18 function reads or
# changes only what its arguments allow.
_OTHER = "12121212-1212-4121-8121-121212121212"


def _two_people(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.core import supabase_client
    from tests.assistant_fakes import USER, FakeDB

    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(days=200)).isoformat()
    fresh = (now - timedelta(days=1)).isoformat()
    db = FakeDB(now=now)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    t = db.tables
    t["assistant_browser_job_credentials"] = [{"id": f"p{i}", "user_id": u} for i, u in enumerate((USER, USER, _OTHER))]
    t["assistant_browser_downloads"] = [
        {"id": "d-mine", "user_id": USER, "status": "staged", "purged_at": None, "storage_path": "a", "created_at": old},
        {"id": "d-other", "user_id": _OTHER, "status": "staged", "purged_at": None, "storage_path": "b", "created_at": old},
    ]
    t["assistant_browser_holds"] = [
        {"id": "h-mine", "user_id": USER, "status": "open", "created_at": old},
        {"id": "h-other", "user_id": _OTHER, "status": "open", "created_at": old},
    ]
    t["assistant_browser_site_grants"] = [{"id": "g1", "user_id": USER, "status": "active"},
                                          {"id": "g2", "user_id": _OTHER, "status": "active"}]
    t["assistant_browser_admissions"] = [{"id": "a2", "user_id": _OTHER, "status": "ready"}]
    t["assistant_browser_user_safe_rules"] = [{"id": "s2", "user_id": _OTHER}]
    return db, USER, old, fresh


def test_the_wi18_offboarding_wrappers_touch_only_the_named_person(monkeypatch):
    db, user, _old, _fresh = _two_people(monkeypatch)
    assert repo.browser_offboard_pins_delete(user) == 2
    assert [r["id"] for r in db.tables["assistant_browser_job_credentials"]] == ["p2"]
    assert [r["id"] for r in repo.browser_offboard_staged_downloads(user)] == ["d-mine"]
    state = repo.browser_offboard_open_state(user)
    assert state == {"grants": 1, "admissions": 0, "requests": 0, "holds": 1, "live": 0, "safe_rules": 0,
                     "pins": 0, "staged": 1}
    assert repo.browser_offboard_open_state(_OTHER)["safe_rules"] == 1
    assert repo.browser_offboard_open_state(_OTHER)["admissions"] == 1


def test_the_wi18_retention_sweeps_refuse_a_person_and_delete_only_ended_old_rows(monkeypatch):
    db, user, old, fresh = _two_people(monkeypatch)
    for fn in (repo.browser_holds_retention_delete, repo.browser_downloads_retention_delete):
        with pytest.raises(ValueError, match="system sweep"):
            fn(user)
    t = db.tables
    t["assistant_browser_holds"] += [
        {"id": "h-old-done", "user_id": user, "status": "consumed", "created_at": old},
        {"id": "h-new-done", "user_id": user, "status": "declined", "created_at": fresh},
        {"id": "h-old-approved", "user_id": _OTHER, "status": "approved", "created_at": old},
        {"id": "h-old-aborted", "user_id": _OTHER, "status": "aborted", "created_at": old},
    ]
    assert repo.browser_holds_retention_delete(None) == 2
    assert sorted(r["id"] for r in t["assistant_browser_holds"]) == ["h-mine", "h-new-done", "h-old-approved", "h-other"]
    t["assistant_browser_downloads"] += [
        {"id": "d-stored", "user_id": user, "status": "stored", "purged_at": None, "created_at": old},
        {"id": "d-rejected", "user_id": _OTHER, "status": "rejected", "purged_at": None, "created_at": old},
        {"id": "d-purged", "user_id": user, "status": "staged", "purged_at": old, "created_at": old},
        {"id": "d-new", "user_id": user, "status": "stored", "purged_at": None, "created_at": fresh},
    ]
    assert repo.browser_downloads_retention_delete(None) == 3
    assert sorted(r["id"] for r in t["assistant_browser_downloads"]) == ["d-mine", "d-new", "d-other"]
    # Legal hold: nothing of that person goes.
    t["assistant_profiles"] = [{"user_id": user, "legal_hold": True}]
    t["assistant_browser_holds"].append({"id": "h-held", "user_id": user, "status": "consumed", "created_at": old})
    t["assistant_browser_downloads"].append({"id": "d-held", "user_id": user, "status": "stored", "created_at": old})
    assert repo.browser_holds_retention_delete(None) == 0 and repo.browser_downloads_retention_delete(None) == 0


def test_the_open_state_reader_names_each_owner_once_and_scopes_by_a_given_id(monkeypatch):
    db, user, _old, _fresh = _two_people(monkeypatch)
    assert repo.browser_v2_owners_with_open_state(None) == sorted({user, _OTHER})
    assert repo.browser_v2_owners_with_open_state(_OTHER) == [_OTHER]


# ── Phase 8: model triage (docs/ASSISTANT_PHASE8_PLAN.md 16.1, WI-5) ─────────
#
# The triage system sweeps take user_id None as "every user" and refuse a
# person's id (the _p7_system_only guard). Their callers are pinned by module,
# like the phase 4 riders, because the work loop's helpers are named by WI-7:
# the triage service (the loop and its purge step), the model module (the
# live-test cap's company spend read) and the Inbox routes (the admin health
# block). repo.py itself reads the counts for the fuse inputs. Every other
# phase 8 function takes a real person's id and refuses the company spend id.

_P8_MARKER = "# ── Phase 8: model triage"
_P4_MARKER = "# ── Phase 4 (0180)"
_TRIAGE = "services/assistant_triage.py"
_TRIAGE_MODEL = "services/assistant_triage_model.py"
PHASE8_SYSTEM_SWEEP_MODULES = {
    "triage_enqueue": {_TRIAGE},
    "triage_claim": {_TRIAGE},
    "triage_finish": {_TRIAGE},
    "triage_skip_all": {_TRIAGE},
    "triage_purge": {_TRIAGE},
    "triage_expired_company_holds": {_TRIAGE},
    "triage_fuse_inputs": {_TRIAGE, _INBOX_ROUTES},
    "triage_counts": {_TRIAGE, _INBOX_ROUTES, _REPO},
    "triage_company_spend": {_TRIAGE_MODEL, _TRIAGE, _INBOX_ROUTES},
}


def _p8_repo_functions() -> dict[str, ast.FunctionDef]:
    source = (APP / "assistant" / "repo.py").read_text(encoding="utf-8")
    # The block sits between the phase 3 offboarding reads and phase 4.
    start, end = source.index(_P8_MARKER), source.index(_P4_MARKER)
    assert start < end
    out: dict[str, ast.FunctionDef] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        offset = sum(len(line) + 1 for line in source.splitlines()[: node.lineno - 1])
        if start <= offset < end:
            out[node.name] = node
    return out


def test_the_phase8_system_sweeps_take_an_optional_user_id_first_and_refuse_a_person():
    functions = _p8_repo_functions()
    optional = {n for n, node in functions.items()
                if "None" in ast.unparse(node.args.args[0].annotation or ast.Constant(value=""))}
    assert optional == set(PHASE8_SYSTEM_SWEEP_MODULES), optional
    source = (APP / "assistant" / "repo.py").read_text(encoding="utf-8")
    for name in PHASE8_SYSTEM_SWEEP_MODULES:
        fn = getattr(repo, name)
        param = next(iter(inspect.signature(fn).parameters.values()))
        assert param.name == "user_id" and "None" in str(param.annotation), name
        assert "system sweep" in (fn.__doc__ or "").lower(), name
        assert "_p7_system_only(user_id," in ast.get_source_segment(source, functions[name]), name


def test_the_phase8_system_sweeps_have_only_the_planned_caller_modules():
    offenders = {}
    for name, modules in PHASE8_SYSTEM_SWEEP_MODULES.items():
        system = {mod for mod, _fn, call in _calls(name) if _passes_none_user(call)}
        if not system <= modules:
            offenders[name] = sorted(system - modules)
        # A person's id never reaches a system sweep.
        assert {mod for mod, _fn, call in _calls(name) if not _passes_none_user(call)} == set(), name
    assert offenders == {}, offenders


def test_every_phase8_per_person_function_filters_by_owner_and_refuses_the_company_id():
    source = (APP / "assistant" / "repo.py").read_text(encoding="utf-8")
    functions = _p8_repo_functions()
    people = {n: node for n, node in functions.items() if n not in PHASE8_SYSTEM_SWEEP_MODULES}
    assert len(people) >= 9, sorted(people)
    offenders = []
    for name, node in people.items():
        text = ast.get_source_segment(source, node)
        assert [a.arg for a in node.args.args][0] == "user_id", name
        if "_refuse_company(user_id," not in text:
            offenders.append((name, "no company id refusal"))
        if ".table(" in text and '.eq("user_id", user_id)' not in text and "SETTINGS_TABLE" not in text:
            offenders.append((name, "table access without an owner filter"))
        if ".rpc(" in text and '"p_user_id"' not in text:
            offenders.append((name, "rpc without the owner"))
    assert offenders == [], offenders
