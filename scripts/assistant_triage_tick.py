"""Phase 8 dev tick: one triage tick at a chosen time, dev only
(docs/ASSISTANT_PHASE8_PLAN.md 17.4 WI-16, live test 16.5).

    .venv/bin/python scripts/assistant_triage_tick.py --now <ISO> [--debounce SECONDS] [--canned-answer FILE]
                                                       [--forbid-text FILE] [--worker ID]

It runs assistant_triage.poll_once(now, worker=...) once, so the real claim,
pack, validate and fenced finish code runs. It never starts the router; the
router tick is scripts/assistant_proactive_tick.py. It sends nothing.

--now       the tick's clock, an ISO 8601 time. A naive value is read as
            America/Los_Angeles; a Z or offset value is used as given. It must
            lie in [real now - 30 days, real now + 8 days], the Phase 7 bounds
            (the purge step runs at this clock). Leases, retirement and the
            finish fence run on the real clock whatever --now says.
--debounce  seconds a person's oldest pending row waits before a claim
            (default 0, range 0 to 3600): the override of
            ASSISTANT_TRIAGE_DEBOUNCE_SECONDS for this process only.
--canned-answer FILE
            no model call and no spend reservation: every batch is answered
            from FILE and the run is stamped model = 'canned'. FILE is JSON in
            one of two shapes. (1) The model's answer object, used as is for
            every batch: {"items": [{"key": "t1", "decision": ..., "reason":
            ..., "action": ..., "severity": ...}, ...]} (keys t1 to t10 follow
            the row order; this shape may be malformed on purpose, to test the
            validator). (2) Rules: {"default": {...}, "rules": [{"kind":
            "bell.due", "project_number": "26.10.7301", "key": "t1",
            "decision": "escalate", "reason": "due_soon_part_missing",
            "severity": "high"}]}; a rule matches on any of kind,
            project_number and key it names (all of them must match), the first
            match wins, an unmatched item takes "default" (else none /
            no_action_needed). Decisions are the model's choice, still
            format-checked by triage.validate like a real answer.
--forbid-text FILE
            a JSON list of strings (or one string per line) that must never
            appear in a built pack. Each pack is hashed (sha256 of its
            canonical JSON, printed per run) and scanned before any model call
            or canned answer; a hit fails that run closed (bad_answer, no
            call) and the script exits 3. The scan runs on the raw text (every
            string value and key of the pack, and the rendered text the model
            would receive), so quotes, backslashes and newlines match. A run
            that hit is stored without a model stamp (it never reached the
            model). Only counts are printed, never the strings (live test
            step 7).
--worker    the worker id the tick claims under (default: a fresh script id).
            It must start with "triage-tick:" so it can never equal a
            server's worker id (the finish fence is keyed on the worker id).

The env comes from the command line only (never .env). A run needs
ASSISTANT_ENABLED=true ASSISTANT_PROACTIVE_ENABLED=true
ASSISTANT_TRIAGE_ENABLED=true and ASSISTANT_TRIAGE_USER_ALLOWLIST=<ids or
emails>. ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD passes through to the real
call's spend check (the boot validator already refuses it off the dev project
or without the allowlist).

Refuses (exit 2, nothing written, no model call) unless: the Supabase URL is
the dev project bpidntbyvoooqvaispup; the three flags are on; the allowlist is
set and holds only the two e2e accounts (owner question 22); --now is inside
the travel window; without --canned-answer, ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD
is set to at most 1 USD (the whole-test budget, owner question 22); and no
assistant_triage_runs row is claimed under a live lease by another worker
(the dev servers must run with ASSISTANT_TRIAGE_LOOP_ENABLED=false); and no
non-script worker claimed any run within the last max(2 x poll seconds, lease
seconds) of the real clock (a running server loop is idle between batches, so
a claimed row alone would almost always be missing). A claimed run whose lease
already expired does not block: the claim itself retires it.

Prints one JSON object: the poll_once counts, the runs this worker claimed
(status, model, error kind, cost) with their items' decisions, and one pack
hash per classify call. Exit 3 when a forbidden string reached a pack.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assistant_proactive_tick import (  # noqa: E402
    E2E_EMAILS,
    Refused,
    parse_when,
    require_dev_ref,
    require_travel_window,
)

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
CANNED_MODEL = "canned"
# The whole-test model budget (owner question 22 (a)): a real-model run needs
# the test cap set, and no higher than this.
LIVE_CAP_MAX = 1.0
DEBOUNCE_MAX = 3600
CANNED_FILE_MAX = 65536
FORBID_FILE_MAX = 65536
RULE_FIELDS = ("kind", "project_number", "key")
DECISION_FIELDS = ("decision", "reason", "action", "severity")
DEFAULT_DECISION = {"decision": "none", "reason": "no_action_needed", "action": None, "severity": None}
EXIT_FORBIDDEN = 3
WORKER_PREFIX = "triage-tick:"


def _real_now() -> datetime:
    """The wall clock (tests pin it)."""
    return datetime.now(UTC)


def _settings() -> Any:
    from app.core.config import get_settings

    return get_settings()


def _db() -> Any:
    from app.core import supabase_client

    return supabase_client.get_supabase()


def _rows(resp: Any) -> list[dict]:
    return [r for r in (getattr(resp, "data", None) or []) if isinstance(r, dict)]


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC)


# ── Guards (pure over a settings object, or one read-only query) ─────────────


def require_flags(settings: Any) -> None:
    missing = [name for name, attr in (
        ("ASSISTANT_ENABLED", "assistant_enabled"),
        ("ASSISTANT_PROACTIVE_ENABLED", "assistant_proactive_enabled"),
        ("ASSISTANT_TRIAGE_ENABLED", "assistant_triage_enabled"),
    ) if not getattr(settings, attr, False)]
    if missing:
        raise Refused(f"{', '.join(missing)} must be true on the command line, or poll_once returns without "
                      "doing anything.")


def allowlist_entries(settings: Any) -> list[str]:
    raw = str(getattr(settings, "assistant_triage_user_allowlist", "") or "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def require_allowlist(settings: Any) -> list[str]:
    entries = allowlist_entries(settings)
    if not entries:
        raise Refused("ASSISTANT_TRIAGE_USER_ALLOWLIST is not set. Without it the tick would triage every dev "
                      "user. Set it on the command line to the two e2e accounts.")
    return entries


def require_e2e_allowlist(entries: list[str], lookup: Callable[[str], dict | None]) -> None:
    """Every entry is one of the two e2e accounts: an email in the e2e set or
    a profile id whose row holds one (owner question 22). `lookup` returns a
    profiles row (or None) for an id."""
    for entry in entries:
        if "@" in entry:
            email = entry
        else:
            try:
                uuid.UUID(entry)
            except ValueError:
                raise Refused("Every allowlist entry must be a profile id or an email.") from None
            email = str((lookup(entry) or {}).get("email") or "").strip().lower()
        if email not in E2E_EMAILS:
            raise Refused("Every allowlisted person must be one of the two e2e accounts; at least one entry is "
                          "not.")


def require_debounce(value: int) -> int:
    if not 0 <= value <= DEBOUNCE_MAX:
        raise Refused(f"--debounce must be between 0 and {DEBOUNCE_MAX} seconds.")
    return value


def require_live_cap(settings: Any) -> float:
    """A run that can call the model needs the test cap, at most 1 USD."""
    cap = getattr(settings, "assistant_triage_test_spend_cap_usd", None)
    if cap is None:
        raise Refused("Without --canned-answer this run calls the model for real, so "
                      "ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD must be set on the command line (at most 1).")
    value = float(cap)
    if not 0 <= value <= LIVE_CAP_MAX:
        raise Refused(f"ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD must be between 0 and {LIVE_CAP_MAX:g} for this "
                      "script (the whole live test shares one 1 USD budget).")
    return value


def claimed_by_others(client: Any, worker: str, real_now: datetime) -> tuple[list[dict], int]:
    """(runs claimed under a live lease by another worker, count of claimed
    runs whose lease already expired). Read only."""
    claimed = _rows(client.table("assistant_triage_runs").select("id, claimed_by, lease_until")
                    .eq("status", "claimed").limit(200).execute())
    live: list[dict] = []
    expired = 0
    for row in claimed:
        lease = _ts(row.get("lease_until"))
        if lease is not None and lease <= real_now:
            expired += 1
        elif row.get("claimed_by") != worker:
            live.append(row)
    return live, expired


def recent_window_seconds(settings: Any) -> int:
    """How far back a non-script claim still counts as a running server: twice
    the poll interval, or the lease if that is longer."""
    def seconds(name: str, default: int) -> int:
        try:
            return max(int(getattr(settings, name, default)), 0)
        except (TypeError, ValueError):
            return default

    return max(2 * seconds("assistant_triage_poll_seconds", 60), seconds("assistant_triage_lease_seconds", 420))


def recent_server_claims(client: Any, worker: str, real_now: datetime, window: int) -> list[dict]:
    """Runs of any status claimed within `window` seconds of the real clock by
    a worker that is neither this one nor another script tick. claimed_at is
    the real clock (created_at holds the travelled p_now, so it is not used)."""
    since = real_now - timedelta(seconds=window)
    rows = _rows(client.table("assistant_triage_runs").select("id, claimed_by, claimed_at")
                 .gte("claimed_at", since.isoformat()).limit(200).execute())
    out: list[dict] = []
    for row in rows:
        at = _ts(row.get("claimed_at"))
        by = str(row.get("claimed_by") or "")
        if at is not None and at >= since and by != worker and not by.startswith(WORKER_PREFIX):
            out.append(row)
    return out


def require_no_other_worker(client: Any, worker: str, real_now: datetime, settings: Any = None) -> int:
    live, expired = claimed_by_others(client, worker, real_now)
    if live:
        raise Refused(f"{len(live)} triage run(s) are claimed by another worker under a live lease. This script "
                      "must be the only triage process on dev (run the dev servers with "
                      "ASSISTANT_TRIAGE_LOOP_ENABLED=false). Wait for the lease to end, then retry.")
    recent = recent_server_claims(client, worker, real_now, recent_window_seconds(settings))
    if recent:
        raise Refused(f"{len(recent)} triage run(s) were claimed by a server worker moments ago, so a triage loop "
                      "looks active. This script must be the only triage process on dev (run the dev servers with "
                      "ASSISTANT_TRIAGE_LOOP_ENABLED=false). Wait a few minutes after stopping the loop, then retry.")
    return expired


def require_worker(value: str | None) -> str:
    """The worker id the tick claims under: the default, or an id with the
    script prefix. A server's own id would let two processes settle each
    other's runs (the finish fence is keyed on the worker id)."""
    if value is None:
        return default_worker()
    if not value.startswith(WORKER_PREFIX) or len(value) > 200:
        raise Refused(f"--worker must start with {WORKER_PREFIX} (at most 200 characters) so it can never equal "
                      "a server worker id.")
    return value


# ── Arguments and files ──────────────────────────────────────────────────────


def _when(value: str) -> datetime:
    try:
        return parse_when(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant_triage_tick.py", description="One triage tick at a chosen time (dev only).")
    parser.add_argument("--now", required=True, type=_when, help="tick time, ISO 8601; naive means America/Los_Angeles")
    parser.add_argument("--debounce", type=int, default=0, metavar="SECONDS",
                        help="debounce override in seconds (default 0)")
    parser.add_argument("--canned-answer", metavar="FILE",
                        help="answer every batch from this JSON file; no model call, no reserve")
    parser.add_argument("--forbid-text", metavar="FILE",
                        help="JSON list of strings (or one per line) that must never appear in a pack")
    parser.add_argument("--worker", metavar="ID", help=f"worker id, must start with {WORKER_PREFIX} (default: a fresh script id)")
    return parser


def default_worker() -> str:
    return f"{WORKER_PREFIX}{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _read_text(path: str, limit: int, what: str) -> str:
    file = Path(path)
    try:
        if file.stat().st_size > limit:
            raise Refused(f"The {what} file is larger than {limit} bytes.")
        return file.read_text(encoding="utf-8")
    except OSError:
        raise Refused(f"The {what} file could not be read.") from None


def _check_decision(spec: Any, where: str) -> dict[str, Any]:
    """One decision's fields, enum-checked (the per-kind and per-role action
    catalog is checked at run time by triage.validate, as for a real answer)."""
    from app.assistant import triage

    if not isinstance(spec, dict):
        raise Refused(f"{where} is not an object.")
    decision = spec.get("decision")
    if decision not in triage.DECISIONS:
        raise Refused(f"{where}: decision must be one of {', '.join(triage.DECISIONS)}.")
    reason = spec.get("reason")
    if reason not in triage.REASONS_BY_DECISION[decision]:
        raise Refused(f"{where}: reason must be one of {', '.join(triage.REASONS_BY_DECISION[decision])}.")
    action, severity = spec.get("action"), spec.get("severity")
    if decision == "suggest" and action not in triage.ACTIONS:
        raise Refused(f"{where}: a suggest needs an action from the action list.")
    if decision == "escalate" and severity not in triage.SEVERITIES:
        raise Refused(f"{where}: an escalate needs a severity of {', '.join(triage.SEVERITIES)}.")
    if decision != "suggest" and action is not None:
        raise Refused(f"{where}: only a suggest carries an action.")
    if decision != "escalate" and severity is not None:
        raise Refused(f"{where}: only an escalate carries a severity.")
    return {"decision": decision, "reason": reason, "action": action, "severity": severity}


def load_canned(path: str) -> dict[str, Any]:
    """Read and validate a canned-answer file. Returns {"raw": answer} for
    shape 1 and {"default": decision, "rules": [...]} for shape 2."""
    from app.assistant import triage

    try:
        data = json.loads(_read_text(path, CANNED_FILE_MAX, "canned answer"))
    except ValueError:
        raise Refused("The canned answer file is not valid JSON.") from None
    if not isinstance(data, dict):
        raise Refused("The canned answer file must hold a JSON object.")
    if "items" in data:
        if set(data) != {"items"}:
            raise Refused("A raw canned answer holds only \"items\".")
        return {"raw": data}
    unknown = sorted(set(data) - {"default", "rules"})
    if unknown or "rules" not in data:
        raise Refused("A canned answer file holds either \"items\" or \"rules\" (and an optional \"default\").")
    rules_in = data["rules"]
    if not isinstance(rules_in, list) or not rules_in:
        raise Refused("\"rules\" must be a non-empty list.")
    default = _check_decision(data["default"], "default") if "default" in data else dict(DEFAULT_DECISION)
    rules = []
    for index, rule in enumerate(rules_in):
        where = f"rule {index}"
        if not isinstance(rule, dict):
            raise Refused(f"{where} is not an object.")
        extra = sorted(set(rule) - set(RULE_FIELDS) - set(DECISION_FIELDS))
        if extra:
            raise Refused(f"{where} has unknown fields: {', '.join(extra)}.")
        match = {name: rule[name] for name in RULE_FIELDS if name in rule}
        if not match:
            raise Refused(f"{where} names none of kind, project_number or key.")
        if "kind" in match and not triage.is_triage_kind(match["kind"]):
            raise Refused(f"{where}: kind is not a triaged record kind.")
        rules.append({"match": match, **_check_decision({k: rule.get(k) for k in DECISION_FIELDS}, where)})
    return {"default": default, "rules": rules}


def canned_answer(spec: dict[str, Any], pack: dict[str, Any]) -> dict[str, Any]:
    """The answer for one pack: shape 1 as is, shape 2 one decision per item."""
    if "raw" in spec:
        return copy.deepcopy(spec["raw"])
    out = []
    for item in pack.get("items") or ():
        chosen = spec["default"]
        for rule in spec["rules"]:
            if all(item.get(name) == value for name, value in rule["match"].items()):
                chosen = rule
                break
        out.append({"key": item["key"], **{k: chosen[k] for k in DECISION_FIELDS}})
    return {"items": out}


def load_forbidden(path: str) -> list[str]:
    text = _read_text(path, FORBID_FILE_MAX, "forbidden text")
    try:
        data = json.loads(text)
    except ValueError:
        data = [line for line in text.splitlines() if line.strip()]
    if not isinstance(data, list) or not data or not all(isinstance(s, str) and s.strip() for s in data):
        raise Refused("The forbidden text file must hold a non-empty list of non-empty strings.")
    return [s.strip() for s in data]


# ── The classify wrapper: pack hash, forbidden-text assertion, canned mode ──


def pack_hash(pack: Any) -> str:
    text = json.dumps(pack, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _texts(value: Any):
    """Every string key and value of a nested structure (a scalar as its
    str), unescaped."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _texts(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _texts(item)
    elif value is not None:
        yield value if isinstance(value, str) else str(value)


def _haystacks(pack: Any) -> list[str]:
    """The raw (unescaped, lowercased) text a forbidden string is looked for
    in: each string of the pack, and the exact text the model would receive.
    json.dumps is never used: it escapes quotes, backslashes and newlines in
    the haystack but not in the needle."""
    texts = [t.lower() for t in _texts(pack)]
    try:
        from app.assistant import triage

        texts.append(triage.render_pack(pack).lower())
    except Exception:  # noqa: BLE001 - a pack that cannot render is scanned by its leaves alone
        pass
    return texts


def forbidden_hits(pack: Any, forbidden: list[str]) -> int:
    """How many forbidden strings appear in the pack (case-insensitive)."""
    texts = _haystacks(pack)
    return sum(1 for s in forbidden if any(s.lower() in t for t in texts))


def make_classify(inner: Callable[..., Any], seen: list[dict], forbidden: list[str]) -> Callable[..., Any]:
    """Wrap one classify function: record the pack hash, refuse a pack that
    holds a forbidden string (the run fails bad_answer, `inner` is not
    called), else delegate."""
    from app.services import assistant_triage_model as model

    def classify(run_id: str, pack: dict, *, settings: Any) -> Any:
        hits = forbidden_hits(pack, forbidden) if forbidden else 0
        seen.append({"run_id": str(run_id), "pack_sha256": pack_hash(pack),
                     "items": len(pack.get("items") or ()), "forbidden_hits": hits})
        if hits:
            raise model.TriageBadAnswer("A forbidden string reached the pack; no answer was requested.")
        return inner(run_id, pack, settings=settings)

    return classify


def make_canned_inner(spec: dict[str, Any]) -> Callable[..., Any]:
    """A classify twin answering from the canned spec, in classify's own
    return shape (a TriageResult; no tokens, no cost, no hold)."""
    from app.services import assistant_triage_model as model

    def inner(run_id: str, pack: dict, *, settings: Any) -> Any:
        return model.TriageResult(answer=canned_answer(spec, pack), usage={"model": CANNED_MODEL})

    return inner


def make_finish(real_finish: Callable[..., dict], seen: list[dict], canned: bool) -> Callable[..., dict]:
    """The service's _finish with the usage corrected: a run whose pack held a
    forbidden string never reached the model, so it is stored with no usage
    (the service counts a bad_answer as a call); in canned mode any other
    usage model is replaced by 'canned'."""

    def finish(run_id, worker, now, status, error_kind, entries, usage=None):
        blocked = any(p["forbidden_hits"] and p["run_id"] == str(run_id) for p in seen)
        if blocked:
            usage = None
        elif canned and usage is not None:
            usage = {**dict(usage), "model": CANNED_MODEL}
        return real_finish(run_id, worker, now, status, error_kind, entries, usage)

    return finish


# ── The report (read only) ───────────────────────────────────────────────────

RUN_FIELDS = ("id", "user_id", "status", "model", "item_count", "error_kind", "cost_usd", "input_tokens",
              "output_tokens", "reservation_id", "created_at", "finished_at")
ITEM_FIELDS = ("id", "run_id", "event_type", "status", "skip_reason", "decision", "reason", "action",
               "severity_hint", "escalation_event_id", "attempts", "cap_deferred_at", "next_attempt_at")


def report(client: Any, worker: str) -> dict[str, Any]:
    runs = _rows(client.table("assistant_triage_runs").select(", ".join(RUN_FIELDS))
                 .eq("claimed_by", worker).order("created_at").limit(200).execute())
    items: list[dict] = []
    if runs:
        items = _rows(client.table("assistant_triage_items").select(", ".join(ITEM_FIELDS))
                      .in_("run_id", [str(r["id"]) for r in runs]).limit(1000).execute())
    by_run: dict[str, list[dict]] = {}
    for item in items:
        by_run.setdefault(str(item.get("run_id")), []).append({k: item.get(k) for k in ITEM_FIELDS if k != "run_id"})
    return {"runs": [{**{k: r.get(k) for k in RUN_FIELDS}, "items": by_run.get(str(r["id"]), [])} for r in runs]}


# ── Main ─────────────────────────────────────────────────────────────────────


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    try:
        settings = _settings()
    except Exception as exc:  # noqa: BLE001 - an invalid env is a refusal, never a traceback with values
        raise Refused(f"The settings did not load ({exc.__class__.__name__}). Check the command line env.") from None
    clock = _real_now()
    canned = args.canned_answer is not None
    require_dev_ref(settings)
    require_flags(settings)
    entries = require_allowlist(settings)
    debounce = require_debounce(args.debounce)
    require_travel_window(args.now, clock)
    cap = None if canned else require_live_cap(settings)
    spec = load_canned(args.canned_answer) if canned else None
    forbidden = load_forbidden(args.forbid_text) if args.forbid_text else []

    from app.assistant import repo

    require_e2e_allowlist(entries, repo.human_profile)
    worker = require_worker(args.worker)
    client = _db()
    expired_claimed = require_no_other_worker(client, worker, clock, settings)

    from app.services import assistant_triage as service
    from app.services import assistant_triage_model as model

    effective = settings.model_copy(update={"assistant_triage_debounce_seconds": debounce})
    seen: list[dict] = []
    real_classify, real_get_settings, real_finish = model.classify, service.get_settings, service._finish
    try:
        service.get_settings = lambda: effective
        inner = make_canned_inner(spec) if canned and spec is not None else real_classify
        model.classify = make_classify(inner, seen, forbidden)
        service._finish = make_finish(real_finish, seen, canned)
        result = service.poll_once(args.now, worker=worker)
    finally:
        model.classify, service.get_settings, service._finish = real_classify, real_get_settings, real_finish

    summary = {
        "now": args.now.isoformat(), "now_pacific": args.now.astimezone(PT).isoformat(), "worker": worker,
        "mode": "canned" if canned else "model", "debounce_seconds": debounce, "allowlist": entries,
        "test_spend_cap_usd": cap, "expired_claimed_runs_seen": expired_claimed,
        "poll": result, "packs": seen, "forbidden_hits": sum(p["forbidden_hits"] for p in seen),
    }
    summary.update(report(client, worker))
    return summary


def main(argv: list[str] | None = None) -> int:
    try:
        summary = run(argv)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    if summary.get("forbidden_hits"):
        print("FAILED: a forbidden string reached a pack.", file=sys.stderr)
        return EXIT_FORBIDDEN
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
