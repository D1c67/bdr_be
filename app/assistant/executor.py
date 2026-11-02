"""The tool executor (docs/ASSISTANT.md, Agent loop, Permissions items 7
and 12, Prompt injection and taint).

One Executor serves one run (a chat turn). `call(name, args)` runs one tool:

  1. the per-turn cap (25 calls) and the spec lookup;
  2. short ids in the arguments are expanded back to the full ids the
     trimmers registered earlier in the run (names plus short ids);
  3. the phase ledger: repo.tool_call_begin (the begin RPC of 0170) inserts
     the begun row, or returns what an earlier attempt recorded. A read that already begun or
     completed is re-run under a fresh key; a write that begun without
     completing is never replayed (WriteMayHavePartiallyRun, the loop parks
     the turn);
  4. the queue lease is renewed through the hook before anything is sent;
  5. a bdra_ token is minted for (user_id, run_id, approval_id) and every
     request of the tool goes through the shim over httpx.ASGITransport
     with raise_app_exceptions=False, on a dedicated event loop per call
     (asyncio.run inside the worker thread, never the server loop);
  6. the trimmer shapes the payloads, the result is capped at about 4k
     tokens with a narrowing hint, and taint is detected on the fields the
     spec names;
  7. the terminal ledger row (completed or failed) and the out-of-line
     result are written.

Confirm-tier tools (sensitive_read, write_internal, external_send,
expensive) never execute through `call`: it raises ConfirmRequired and the
loop turns the call into an approval card (app/assistant/approvals.py).

Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.7.2) adds the housekeeping tier:
inline writes to the user's own assistant rows. They run through `call`
like a read, but only under a begun ledger row (the route reads it back as
its trusted turn context, keyed by the X-Assistant-Idempotency-Key header
the executor sets), and a begun row from an earlier attempt is re-run under
a fresh key because the route dedupes by the step key. Every ledger row
stores `_ledger_args` (the spec's redact_args form when it has one), a
spec's `precheck` refuses before anything is recorded or sent, and a memory
tool's stored result names the full ids of the memories it shows.

Phase 1 (docs/ASSISTANT_PHASE1_PLAN.md 3.3) adds three entry points:
`read(requests, spec)` sends a write spec's preview requests (no ledger
row), `write(spec, request, ...)` sends the one approved write under the
begun ledger row, and `finish_write(...)` records its terminal row. A turn's
shim allowlist holds read routes and preview routes only; an executor bound
to an approval holds exactly that tool's routes.

Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.2, 3.8, 3.9.2, 3.14): `shape` is
the public trimmer path the evidence delivery uses; a sensitive_read write
whose begun row is already there re-runs under a fresh key like a read (its
route is idempotent on the approval id), never WriteMayHavePartiallyRun;
the evidence and report namespaces (and any spec naming TAINT_ANY_TEXT)
taint on any free text outside a whitelist of id, enum, date and number
keys; `report_ids` collects the workbooks a report tool built; a stored
result takes its spec's retention class (payroll aggregates expire after 7
days); and no request of a non-sensitive spec may name one person on a
per-person route (registry.per_person_violation).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.assistant import principal, shim
from app.assistant.registry import (
    EFFECT_HOUSEKEEPING,
    EFFECT_READ,
    EFFECT_SENSITIVE_READ,
    RETENTION_STANDARD,
    ToolRequest,
    ToolSpec,
    TrimContext,
    common_args,
    execute_allowlist,
    per_person_violation,
    turn_allowlist,
)
from app.assistant.tools._common import ToolArgumentError, is_uuid

logger = logging.getLogger(__name__)

MAX_CALLS_PER_TURN = 25
# About 4k tokens at four characters per token.
MAX_OUTPUT_CHARS = 16_000
ARGS_REDACTED_MAX_BYTES = 8_000
REQUEST_TIMEOUT_SECONDS = 60.0
TOOL_RESULT_RETENTION = RETENTION_STANDARD
# Days a stored tool result lives, per retention class (3.14). "standard"
# keeps the column default (30 days); the others set expires_at.
RETENTION_DAYS = {"mail": 30, "cross_mailbox": 7, "standard": 30, "payroll_aggregate": 7}
# Ledger attempts of a re-runnable step (a read, housekeeping, a sensitive read).
MAX_RERUN_ATTEMPTS = 4

# The whitelist taint rule (3.8): in these namespaces, and for any spec whose
# taint_fields include TAINT_ANY_TEXT, a non-empty string under a key that is
# not an id, enum, date or number key taints the result, on top of the named
# taint_fields.
TAINT_ANY_TEXT = "*"
WHITELIST_TAINT_NAMESPACES = frozenset({"evidence", "report"})
TAINT_WHITELIST_KEYS = frozenset({
    "id", "at", "at_pt", "source", "kind", "status", "action", "field", "view", "role", "before_known",
    "count", "total", "amount", "cost", "number", "channel", "type", "spec", "title", "filename",
    "model", "feature", "group_by", "since", "until", "day", "date", "week", "period", "cursor",
    "next_cursor", "truncated", "why", "hint", "caveats", "retention_note", "response_format",
})
TAINT_WHITELIST_SUFFIXES = (
    "_id", "_ids", "_at", "_pt", "_count", "_total", "_usd", "_amount", "_pct", "_date", "_days",
    "_hours", "_tokens", "_status", "_kind", "_role",
)

# The header a housekeeping route reads its ledger key from (the same name
# as tools/writes.py ASSISTANT_IDEMPOTENCY_HEADER, which carries the
# approval id on an approved write).
IDEMPOTENCY_HEADER = "X-Assistant-Idempotency-Key"
# Output keys whose strings name a memory (3.7.2): recorded in full on the
# stored result of a memory tool, so a delete finds every result to redact.
MEMORY_ID_KEYS = ("id", "memory_id")
LEDGER_UNAVAILABLE_TEXT = "The change could not be recorded, so it was not made. Try again."
PRECHECK_FAILED_TEXT = "The tool could not check its arguments, so nothing was sent."

NARROWING_HINT = (
    "Result truncated at the size limit. Ask again with a smaller limit, "
    "response_format concise, or a narrower query."
)

Sender = Callable[[ToolRequest, str], tuple[int, Any]]


class TurnBudgetExceeded(RuntimeError):
    """More than MAX_CALLS_PER_TURN tool calls in one turn."""


class LeaseLost(RuntimeError):
    """The queue lease could not be renewed: another worker may own the job."""

    llm_queue_lease_lost = True


class WriteMayHavePartiallyRun(RuntimeError):
    """A begun-without-completed write step: never auto-replayed."""


class ConfirmRequired(RuntimeError):
    """A confirm-tier tool: executes only through an approval card (phase 1)."""


class LedgerUnavailable(RuntimeError):
    """The begun row of a write could not be written: nothing was sent
    (writes fail closed)."""


# The most taint corpus entries kept per tool result (3.3).
MAX_TAINT_ENTRIES = 200


@dataclass(frozen=True)
class WriteResult:
    """What `write` sent and got back. `status` 0 is a transport failure:
    the request may have reached the route, so the caller treats it like a
    5xx."""

    status: int
    payload: Any
    key: str
    ledger_id: str
    duration_ms: int
    seq: int = 1


@dataclass(frozen=True)
class ToolOutcome:
    seq: int
    tool_name: str
    progress_label: str
    ok: bool
    duration_ms: int
    output: Any
    output_text: str
    tainted: bool = False
    error_kind: str | None = None
    error: str | None = None
    project_id: str | None = None
    ledger_id: str | None = None
    truncated: bool = False

    def progress(self) -> dict:
        """The assistant_messages.tool_calls entry (the API contract shape)."""
        return {
            "seq": self.seq,
            "tool_name": self.tool_name,
            "progress_label": self.progress_label,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
        }


@dataclass
class Executor:
    user_id: str
    user_label: str
    run_id: str
    specs: Mapping[str, ToolSpec]
    lease_owner: str
    conversation_id: str | None = None
    message_id: str | None = None
    approval_id: str | None = None
    tainted_context: bool = False
    renew_lease: Callable[[], bool] | None = None
    sender: Sender | None = None
    role: Any = None
    ids: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    tainted: bool = False
    project_ids: dict[str, str] = field(default_factory=dict)
    # {project number: full project id} for every project a result named,
    # so the loop can attach citation blocks to the numbers the answer cites.
    projects: dict[str, str] = field(default_factory=dict)
    # (field, text) for every taint-field string of every tainted result of
    # this run, taken before truncation: the binding corpus (3.4).
    tainted_texts: list[tuple[str, str]] = field(default_factory=list)
    # Full memory ids a housekeeping result asked to show under the answer
    # (output show_note true), in call order: the loop's memory_note blocks.
    memory_notes: list[str] = field(default_factory=list)
    # Full assistant_reports ids a report-namespace result named under
    # `report_id`, in call order: the loop's report_file blocks (3.9.2).
    report_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.approval_id:
            if len(self.specs) != 1:
                raise ValueError("an executor bound to an approval serves exactly one tool")
            allowlist = execute_allowlist(next(iter(self.specs.values())))
        else:
            allowlist = turn_allowlist(self.specs.values())
        self._shim = shim.make_shim(
            allowlist, user_id=self.user_id, run_id=self.run_id, approval_id=self.approval_id,
        )

    # ── ids ──────────────────────────────────────────────────────────────

    def short(self, value: Any) -> str:
        """Register a full id and return the short form the model sees."""
        full = str(value or "")
        if not is_uuid(full):
            return full
        for n in (8, 12, 36):
            candidate = full[:n]
            if self.ids.get(candidate, full) == full:
                self.ids[candidate] = full
                return candidate
        return full

    def expand(self, args: Mapping[str, Any]) -> dict:
        """Short ids back to full ids: any string under a key ending `_id`,
        and every string element of a list under a key ending `_ids`, at any
        depth (dicts and lists of dicts)."""
        return self._expand_value(dict(args))

    def _expand_value(self, value: Any, key: str = "") -> Any:
        if isinstance(value, Mapping):
            return {k: self._expand_value(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            if key.endswith("_ids"):
                return [self.ids.get(v, v) if isinstance(v, str) else self._expand_value(v) for v in value]
            return [self._expand_value(v) for v in value]
        if isinstance(value, str) and key.endswith("_id"):
            return self.ids.get(value, value)
        return value

    def _harvest_projects(self, value: Any) -> None:
        """Record {number: full id} for every dict in a result that carries
        a project number next to a short id this run registered."""
        if isinstance(value, dict):
            number, short = value.get("number"), value.get("id")
            if isinstance(number, str) and number.strip() and isinstance(short, str) and short in self.ids:
                self.projects.setdefault(number.strip(), self.ids[short])
            for inner in value.values():
                self._harvest_projects(inner)
        elif isinstance(value, list):
            for inner in value:
                self._harvest_projects(inner)

    # ── one call ─────────────────────────────────────────────────────────

    def call(self, name: str, args: Mapping[str, Any] | None) -> ToolOutcome:
        self.calls += 1
        if self.calls > MAX_CALLS_PER_TURN:
            raise TurnBudgetExceeded(f"more than {MAX_CALLS_PER_TURN} tool calls in one turn")
        seq = self.calls
        spec = self.specs.get(name)
        if spec is None:
            return self._unknown(seq, name)
        if spec.effect not in (EFFECT_READ, EFFECT_HOUSEKEEPING):
            raise ConfirmRequired(f"{spec.name} needs an approval card.")
        args = self.expand(args or {})
        refused = self._precheck(spec, seq, args)
        if refused is not None:
            return refused
        project_id = args.get("project_id") if is_uuid(args.get("project_id")) else None
        if spec.effect == EFFECT_HOUSEKEEPING:
            return self._housekeeping(spec, seq, args, project_id)
        started = time.monotonic()
        key, begun_id = self._begin(spec, seq, args, project_id)
        return self._run(spec, seq, key, begun_id, args, project_id, started, ledger_key=key)

    def establish_preview_context(self, spec: ToolSpec) -> None:
        """Persist trusted run/message provenance before a mail preview reads it.

        This ledger step grants no write authority. It records only that the
        server entered a preview, with no recipient or body arguments.
        """
        if not self.conversation_id or not self.message_id:
            raise LedgerUnavailable("Preview context is unavailable.")
        self.calls += 1
        if self.calls > MAX_CALLS_PER_TURN:
            raise TurnBudgetExceeded("Tool limit reached.")
        preview = replace(spec, effect=EFFECT_READ, route=spec.extra_routes[0], write=None, confirm=False)
        args = {"preview_context": True}
        key, begun = self._begin(preview, self.calls, args, None)
        if not begun:
            raise LedgerUnavailable("Preview context is unavailable.")
        self._finish(preview, self.calls, key, args, None, phase="completed", ok=True,
                     error_kind=None, duration_ms=0, result_summary={"preview_context": True})

    def _housekeeping(self, spec: ToolSpec, seq: int, args: Mapping[str, Any],
                      project_id: str | None) -> ToolOutcome:
        """A housekeeping step (3.7.2): the begun row must be written, since
        the route reads it back as its trusted turn context; without it
        nothing is sent. Every request carries the ledger key."""
        started = time.monotonic()
        try:
            key, begun_id = self._begin(spec, seq, args, project_id)
        except Exception:  # noqa: BLE001 - no begun row means nothing is sent
            logger.exception("assistant executor: no begun row for housekeeping %s", spec.name)
            begun_id = None
        if not begun_id:
            return self._refusal(spec, seq, "ledger_unavailable", LEDGER_UNAVAILABLE_TEXT,
                                 duration_ms=int((time.monotonic() - started) * 1000))
        return self._run(spec, seq, key, begun_id, args, project_id, started, ledger_key=key)

    def _run(self, spec: ToolSpec, seq: int, key: str, begun_id: str | None, args: Mapping[str, Any],
             project_id: str | None, started: float, *, ledger_key: str | None = None) -> ToolOutcome:
        """Send, shape, record: the shared tail of a read and a housekeeping
        step. `ledger_key` (housekeeping only) goes on every request."""
        try:
            requests = spec.requests(args)
            if ledger_key is not None:
                requests = [_with_ledger_key(req, ledger_key) for req in requests]
            payloads = self._send_all(spec, requests)
            output, tainted, truncated, corpus, trimmed = self._shape(spec, args, payloads)
        except ToolArgumentError as exc:
            return self._fail(spec, seq, key, args, started, project_id, "bad_args", str(exc), begun_id)
        except RequestFailed as exc:
            return self._fail(spec, seq, key, args, started, project_id, exc.kind, exc.message, begun_id)
        except (LeaseLost, TurnBudgetExceeded):
            raise
        except Exception:  # noqa: BLE001 - a trimmer bug is a failed step, not a dead turn
            logger.exception("assistant executor: %s failed", spec.name)
            return self._fail(spec, seq, key, args, started, project_id, "tool_error",
                              "The tool failed while shaping its result.", begun_id)
        duration = int((time.monotonic() - started) * 1000)
        if tainted:
            self.tainted = True
            self.tainted_texts.extend(corpus)
        if project_id:
            self.project_ids[project_id] = spec.name
        self._harvest_projects(output)
        text = _dumps(output)
        summary = {
            "requests": len(spec.routes),
            "chars": len(text),
            "truncated": truncated,
            "tainted": tainted,
        }
        self._finish(spec, seq, key, args, project_id, phase="completed", ok=True,
                     error_kind=None, duration_ms=duration, result_summary=summary)
        self._store_result(spec, begun_id, output, tainted, corpus, trimmed=trimmed)
        if spec.effect == EFFECT_HOUSEKEEPING and spec.namespace == "memory":
            self._collect_note(trimmed)
        if spec.namespace == "report":
            self._collect_reports(trimmed)
        return ToolOutcome(
            seq=seq, tool_name=spec.name, progress_label=spec.progress_label, ok=True,
            duration_ms=duration, output=output, output_text=text, tainted=tainted,
            project_id=project_id, ledger_id=begun_id, truncated=truncated,
        )

    # ── pieces ───────────────────────────────────────────────────────────

    def _precheck(self, spec: ToolSpec, seq: int, args: Mapping[str, Any]) -> ToolOutcome | None:
        """The spec's precheck (3.7.2): a refusal is a failed step with no
        ledger row, no stored result and no request. A precheck that raises
        refuses too (it guards what may be recorded at all)."""
        if spec.precheck is None:
            return None
        try:
            refusal = spec.precheck(args)
        except Exception:  # noqa: BLE001 - fail closed: nothing is recorded or sent
            logger.exception("assistant executor: precheck of %s failed", spec.name)
            return self._refusal(spec, seq, "tool_error", PRECHECK_FAILED_TEXT)
        if not refusal:
            return None
        kind, message = refusal
        return self._refusal(spec, seq, str(kind), str(message))

    def _refusal(self, spec: ToolSpec, seq: int, kind: str, message: str, *,
                 duration_ms: int = 0) -> ToolOutcome:
        """A failed step that recorded nothing (no ledger row, no result)."""
        output = {"error": message}
        return ToolOutcome(
            seq=seq, tool_name=spec.name, progress_label=spec.progress_label, ok=False,
            duration_ms=duration_ms, output=output, output_text=_dumps(output), error_kind=kind,
            error=message,
        )

    def _ledger_args(self, spec: ToolSpec, args: Mapping[str, Any]) -> dict:
        """The arguments as every ledger row of this spec stores them: its
        redact_args form when it has one, else the 8 KB cap (which is not a
        redaction). A redact_args that raises stores nothing of the args."""
        if spec.redact_args is None:
            return _redact_args(args)
        try:
            return dict(spec.redact_args(args))
        except Exception:  # noqa: BLE001 - never fall back to the raw arguments
            logger.exception("assistant executor: redact_args of %s failed", spec.name)
            return {"redacted": True}

    def _collect_note(self, output: Any) -> None:
        """A memory housekeeping result with show_note true names the
        memory the loop shows a memory_note block for (full id, once)."""
        if not isinstance(output, Mapping) or output.get("show_note") is not True:
            return
        memory_id = output.get("memory_id")
        full = self.ids.get(memory_id, memory_id) if isinstance(memory_id, str) else None
        if is_uuid(full) and full not in self.memory_notes:
            self.memory_notes.append(full)

    def _collect_reports(self, output: Any) -> None:
        """Every string under a `report_id` key at any depth, expanded to
        the full uuid, once each (3.9.2: never a URL, only the id)."""
        def walk(node: Any) -> None:
            if isinstance(node, Mapping):
                for key, inner in node.items():
                    if key == "report_id" and isinstance(inner, str):
                        full = self.ids.get(inner, inner)
                        if is_uuid(full) and full not in self.report_ids:
                            self.report_ids.append(full)
                    else:
                        walk(inner)
            elif isinstance(node, list):
                for inner in node:
                    walk(inner)

        walk(output)

    def _memory_ids(self, output: Any) -> list[str]:
        """Every string under an `id` or `memory_id` key at any depth,
        expanded through the short ids to the full uuid, deduped."""
        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, Mapping):
                for key, inner in node.items():
                    if key in MEMORY_ID_KEYS and isinstance(inner, str):
                        full = self.ids.get(inner, inner)
                        if is_uuid(full) and full not in found:
                            found.append(full)
                    else:
                        walk(inner)
            elif isinstance(node, list):
                for inner in node:
                    walk(inner)

        walk(output)
        return found

    def _unknown(self, seq: int, name: str) -> ToolOutcome:
        message = f"Unknown tool {name!r}. Use one of: {', '.join(sorted(self.specs))}."
        output = {"error": message}
        return ToolOutcome(
            seq=seq, tool_name=str(name)[:80], progress_label="Unknown tool", ok=False,
            duration_ms=0, output=output, output_text=_dumps(output), error_kind="unknown_tool",
            error=message,
        )

    def _idempotency_key(self, spec: ToolSpec, seq: int, args: Mapping[str, Any], attempt: int) -> str:
        """Stable across attempts of the same turn (Permissions item 7): the
        message id, not the per-attempt run_id, anchors the key, so a
        requeued turn collides with its own begun row for the same step."""
        digest = hashlib.sha256(_dumps(args).encode("utf-8")).hexdigest()[:16]
        anchor = self.message_id or self.approval_id or self.run_id
        key = f"{anchor}:{seq}:{spec.name}:{digest}"
        if attempt:
            key += f":r{attempt}"
        return key[:200]

    def _begin(self, spec: ToolSpec, seq: int, args: Mapping[str, Any],
               project_id: str | None) -> tuple[str, str | None]:
        """The begun ledger row for this step. Returns (key, begun row id)."""
        from app.assistant import repo

        redacted = self._ledger_args(spec, args)
        payload_sha = hashlib.sha256(_dumps(args).encode("utf-8")).hexdigest()
        for attempt in range(MAX_RERUN_ATTEMPTS):
            key = self._idempotency_key(spec, seq, args, attempt)
            try:
                result = repo.tool_call_begin(
                    self.user_id,
                    idempotency_key=key,
                    lease_owner=self.lease_owner,
                    user_label=self.user_label,
                    run_id=self.run_id,
                    seq=seq,
                    tool_name=spec.name,
                    effect=spec.effect,
                    conversation_id=self.conversation_id,
                    message_id=self.message_id,
                    tool_version=spec.version,
                    sub_app=spec.sub_app,
                    method=spec.route[0],
                    path_format=spec.route[1],
                    args_redacted=redacted,
                    payload_sha256=payload_sha,
                    project_id=project_id,
                    approval_id=self.approval_id,
                    tainted_context=self.tainted_context or self.tainted,
                )
            except Exception:  # noqa: BLE001 - the ledger must not kill a read turn
                logger.exception("assistant executor: tool_call_begin failed for %s", spec.name)
                return key, None
            rows = result.get("rows") or []
            if result.get("inserted"):
                begun = next((r for r in rows if r.get("phase") == "begun"), rows[0] if rows else {})
                return key, begun.get("id")
            if spec.effect not in (EFFECT_READ, EFFECT_HOUSEKEEPING):
                raise WriteMayHavePartiallyRun(
                    f"{spec.name} step {seq} began in an earlier attempt and did not complete"
                )
            # A read is re-run under a fresh key whatever the earlier attempt
            # did; so is a housekeeping step, whose route dedupes by the step
            # key (the ledger key without its :rN suffix).
        raise RuntimeError(f"could not open a ledger row for {spec.name} step {seq}")

    def _send_all(self, spec: ToolSpec, requests: list[ToolRequest]) -> list[Any]:
        allowed = set(spec.routes)
        for req in requests:
            if req.route not in allowed:
                raise RequestFailed("route_mismatch",
                                    f"{spec.name} built a request for a route it does not declare")
            refused = per_person_violation(spec.effect, req)
            if refused is not None:
                raise RequestFailed("per_person", f"{spec.name} may not read one person's activity: {refused}. "
                                                  "Use person_evidence, which asks for approval.")
        if self.renew_lease is not None and not self.renew_lease():
            raise LeaseLost("the queue lease could not be renewed; the request was not sent")
        token = principal.mint_token(self.user_id, self.run_id, self.approval_id)
        send = self.sender or self._send_via_shim
        payloads: list[Any] = []
        for req in requests:
            status, payload = send(req, token)
            if _is_firewall(status, payload):
                raise RequestFailed("firewall", f"{req.method} {req.path} refused by the assistant firewall "
                                                f"({payload.get('reason')})")
            if status >= 400:
                if req.optional and status in (403, 404):
                    payloads.append(None)
                    continue
                raise RequestFailed(f"http_{status}", _detail(payload, status))
            payloads.append(payload)
        return payloads

    # ── phase 1: previews and the one approved write ─────────────────────

    def read(self, requests: list[ToolRequest], spec: ToolSpec) -> list[Any]:
        """Send a write spec's preview requests (or the execute job's
        re-read): route check against spec.routes, lease renewal, one token.
        No ledger row. Raises RequestFailed like a read tool's step."""
        return self._send_all(spec, list(requests))

    def write(self, spec: ToolSpec, request: ToolRequest, *, args: Mapping[str, Any],
              before_snapshot: dict | None, project_id: str | None) -> WriteResult:
        """Send the one approved write under its begun ledger row.

        The begun row goes first; any failure to write it raises
        LedgerUnavailable and nothing is sent. A begun row already there
        for the key raises WriteMayHavePartiallyRun (writes have no retry
        key). A firewall 403 raises RequestFailed("firewall"); every other
        status comes back in the WriteResult and the caller records the
        terminal row through `finish_write`. A transport exception comes
        back as status 0."""
        from app.assistant import repo

        self.calls += 1
        seq = self.calls
        if not self.approval_id:
            raise RequestFailed("not_approved", f"{spec.name} writes only under an approval")
        if request.route != spec.route:
            raise RequestFailed("route_mismatch", f"{spec.name} built a write for a route it does not declare")
        refused = per_person_violation(spec.effect, request)
        if refused is not None:
            raise RequestFailed("per_person", f"{spec.name} may not read one person's activity: {refused}")
        args = dict(args)
        started = time.monotonic()
        # A sensitive read only reads and logs the access, and its route is
        # idempotent on the approval id: a begun row from an earlier attempt
        # is re-run under a fresh key like a read (3.2.3). Any other write
        # that already began is never replayed.
        attempts = MAX_RERUN_ATTEMPTS if spec.effect == EFFECT_SENSITIVE_READ else 1
        begun: dict = {}
        key = ""
        for attempt in range(attempts):
            key = self._idempotency_key(spec, seq, args, attempt)
            try:
                begun = repo.tool_call_begin(
                    self.user_id,
                    idempotency_key=key,
                    lease_owner=self.lease_owner,
                    user_label=self.user_label,
                    run_id=self.run_id,
                    seq=seq,
                    tool_name=spec.name,
                    effect=spec.effect,
                    conversation_id=self.conversation_id,
                    message_id=self.message_id,
                    tool_version=spec.version,
                    sub_app=spec.sub_app,
                    method=spec.route[0],
                    path_format=spec.route[1],
                    args_redacted=self._ledger_args(spec, args),
                    payload_sha256=hashlib.sha256(_dumps(args).encode("utf-8")).hexdigest(),
                    before_snapshot=before_snapshot,
                    project_id=project_id,
                    approval_id=self.approval_id,
                    tainted_context=self.tainted_context or self.tainted,
                )
            except Exception as exc:  # noqa: BLE001 - any ledger failure means nothing is sent
                logger.exception("assistant executor: begun row failed for write %s", spec.name)
                raise LedgerUnavailable(f"the ledger could not record {spec.name}") from exc
            if begun.get("inserted"):
                break
        rows = begun.get("rows") or []
        if not begun.get("inserted"):
            raise WriteMayHavePartiallyRun(
                f"{spec.name} step {seq} began in an earlier attempt and did not complete"
            )
        begun_row = next((r for r in rows if r.get("phase") == "begun"), rows[0] if rows else {})
        ledger_id = str(begun_row.get("id") or "")
        if self.renew_lease is not None and not self.renew_lease():
            raise LeaseLost("the queue lease could not be renewed; the write was not sent")
        token = principal.mint_token(self.user_id, self.run_id, self.approval_id)
        send = self.sender or self._send_via_shim
        try:
            status, payload = send(request, token)
        except Exception:  # noqa: BLE001 - the request may have reached the route: treated like a 5xx
            logger.exception("assistant executor: transport failure on write %s", spec.name)
            status, payload = 0, {"error": "transport"}
        if _is_firewall(status, payload):
            raise RequestFailed("firewall", f"{request.method} {request.path} refused by the assistant firewall "
                                            f"({payload.get('reason')})")
        return WriteResult(
            status=int(status), payload=payload, key=key, ledger_id=ledger_id,
            duration_ms=int((time.monotonic() - started) * 1000), seq=seq,
        )

    def finish_write(self, spec: ToolSpec, result: WriteResult, *, ok: bool, error_kind: str | None,
                     result_summary: dict | None, after_snapshot: dict | None,
                     args: Mapping[str, Any], project_id: str | None) -> dict:
        """The terminal ledger row of a write (completed when ok, else
        failed) with its after snapshot. Unlike `_finish`, any exception
        propagates; a fence refusal for a lost lease raises LeaseLost."""
        from app.assistant import repo

        row = self._ledger_row(
            spec, result.seq, result.key, args, project_id,
            phase="completed" if ok else "failed", ok=ok, error_kind=error_kind,
            duration_ms=result.duration_ms, result_summary=result_summary or {},
            after_snapshot=after_snapshot,
        )
        try:
            return repo.tool_call_finish(self.user_id, row)
        except Exception as exc:
            if "lease_lost" in str(exc):
                raise LeaseLost("the ledger fence refused the terminal row: lease lost") from exc
            raise

    def _send_via_shim(self, req: ToolRequest, token: str) -> tuple[int, Any]:
        # The tool's own headers first; Authorization and Accept always win.
        headers = {str(k): str(v) for k, v in dict(req.headers or {}).items()
                   if str(k).lower() not in ("authorization", "accept")}
        headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

        async def go() -> tuple[int, Any]:
            transport = httpx.ASGITransport(app=self._shim, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://assistant", timeout=REQUEST_TIMEOUT_SECONDS,
            ) as client:
                resp = await client.request(
                    req.method, req.path, params=dict(req.params) or None, json=req.json, headers=headers,
                )
                try:
                    payload: Any = resp.json()
                except ValueError:
                    payload = resp.text
                return resp.status_code, payload

        # A dedicated loop per call: the worker thread has none running.
        return asyncio.run(go())

    def _shape(self, spec: ToolSpec, args: Mapping[str, Any],
               payloads: list[Any]) -> tuple[Any, bool, bool, list[tuple[str, str]], Any]:
        """(output, tainted, truncated, taint corpus, trimmed). The corpus is
        taken from the full trimmer output before any truncation; `trimmed`
        is that full output."""
        fmt, limit = common_args(args)
        ctx = TrimContext(response_format=fmt, limit=limit, short=self.short, role=self.role)
        output = trimmed = spec.response_trimmer(payloads, args, ctx)
        tainted = contains_tainted(output, spec.taint_fields)
        corpus = taint_entries(output, spec.taint_fields) if tainted else []
        if whitelist_taint_applies(spec):
            free = free_text_entries(output)
            if free:
                tainted = True
                seen = set(corpus)
                corpus.extend(e for e in free if e not in seen)
                corpus = corpus[:MAX_TAINT_ENTRIES]
        text = _dumps(output)
        truncated = False
        if len(text) > MAX_OUTPUT_CHARS:
            truncated = True
            output = {
                "truncated": True,
                "hint": NARROWING_HINT,
                "partial": text[: MAX_OUTPUT_CHARS - len(NARROWING_HINT) - 60],
            }
        return output, tainted, truncated, corpus, trimmed

    def shape(self, spec: ToolSpec, payloads: list[Any], args: Mapping[str, Any]) -> tuple[Any, bool]:
        """(output, tainted) for `payloads` through the spec's trimmer: the
        same path as a read step (TrimContext with this executor's role and
        short ids, named and whitelist taint, the MAX_OUTPUT_CHARS cap with
        its narrowing form). No ledger row, no request, no stored result,
        and the run's own taint state is left alone; the project numbers
        the output names are recorded in `projects` (citations). Used by
        app/assistant/evidence_delivery.py (3.2.4)."""
        output, tainted, _truncated, _corpus, trimmed = self._shape(spec, args, payloads)
        self._harvest_projects(trimmed)
        return output, tainted

    def _fail(self, spec: ToolSpec, seq: int, key: str, args: Mapping[str, Any], started: float,
              project_id: str | None, kind: str, message: str, begun_id: str | None) -> ToolOutcome:
        duration = int((time.monotonic() - started) * 1000)
        # A route's error text may echo what it was sent: a spec whose
        # arguments are redacted records only the kind.
        summary = {"error": message[:300]} if spec.redact_args is None else {"error": kind, "redacted": True}
        self._finish(spec, seq, key, args, project_id, phase="failed", ok=False, error_kind=kind,
                     duration_ms=duration, result_summary=summary)
        output = {"error": message}
        return ToolOutcome(
            seq=seq, tool_name=spec.name, progress_label=spec.progress_label, ok=False,
            duration_ms=duration, output=output, output_text=_dumps(output), error_kind=kind,
            error=message, project_id=project_id, ledger_id=begun_id,
        )

    def _finish(self, spec: ToolSpec, seq: int, key: str, args: Mapping[str, Any],
                project_id: str | None, *, phase: str, ok: bool, error_kind: str | None,
                duration_ms: int, result_summary: dict, after_snapshot: dict | None = None) -> None:
        from app.assistant import repo

        row = self._ledger_row(spec, seq, key, args, project_id, phase=phase, ok=ok,
                               error_kind=error_kind, duration_ms=duration_ms,
                               result_summary=result_summary, after_snapshot=after_snapshot)
        try:
            repo.tool_call_finish(self.user_id, row)
        except Exception:  # noqa: BLE001 - a fenced-out or failed insert is logged, not fatal to a read
            logger.exception("assistant executor: ledger %s row failed for %s", phase, spec.name)

    def _ledger_row(self, spec: ToolSpec, seq: int, key: str, args: Mapping[str, Any],
                    project_id: str | None, *, phase: str, ok: bool, error_kind: str | None,
                    duration_ms: int, result_summary: dict, after_snapshot: dict | None) -> dict:
        row = {
            "user_label": self.user_label,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "seq": seq,
            "idempotency_key": key,
            "phase": phase,
            "lease_owner": self.lease_owner,
            "tool_name": spec.name,
            "tool_version": spec.version,
            "effect": spec.effect,
            "sub_app": spec.sub_app,
            "method": spec.route[0],
            "path_format": spec.route[1],
            "args_redacted": self._ledger_args(spec, args),
            "payload_sha256": hashlib.sha256(_dumps(args).encode("utf-8")).hexdigest(),
            "result_summary": result_summary,
            "ok": ok,
            "error_kind": error_kind,
            "project_id": project_id,
            "approval_id": self.approval_id,
            "tainted_context": self.tainted_context or self.tainted,
            "duration_ms": duration_ms,
        }
        if after_snapshot is not None:
            row["after_snapshot"] = after_snapshot
        return row

    def _store_result(self, spec: ToolSpec, begun_id: str | None, output: Any, tainted: bool,
                      corpus: list[tuple[str, str]] | None = None, *, trimmed: Any = None) -> None:
        from app.assistant import repo

        text = _dumps(output)
        extra: dict[str, Any] = {}
        if tainted:
            # The binding corpus, extracted before truncation (0172).
            extra["taint_corpus"] = [{"f": f, "t": t} for f, t in (corpus or [])]
        if spec.namespace == "memory":
            # The full ids behind the short ones the model saw, taken before
            # truncation (0176 B6: a delete redacts the results naming it).
            memory_ids = self._memory_ids(output if trimmed is None else trimmed)
            if memory_ids:
                extra["memory_ids"] = memory_ids
        retention = spec.retention_class or TOOL_RESULT_RETENTION
        if retention != TOOL_RESULT_RETENTION:
            # 3.14: payroll aggregates live 7 days; standard keeps the default.
            days = RETENTION_DAYS.get(retention, RETENTION_DAYS["payroll_aggregate"])
            extra["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        try:
            repo.insert_tool_result(self.user_id, {
                **extra,
                "conversation_id": self.conversation_id,
                "message_id": self.message_id,
                "tool_call_id": begun_id,
                "tool_name": spec.name,
                "retention_class": retention,
                "tainted": tainted,
                "payload": output if isinstance(output, (dict, list)) else {"value": output},
                "payload_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "size_bytes": len(text.encode("utf-8")),
            })
        except Exception:  # noqa: BLE001
            logger.exception("assistant executor: tool result store failed for %s", spec.name)


class RequestFailed(RuntimeError):
    """A request the tool sent was refused (firewall, route mismatch, HTTP
    status): a failed step, never a crash."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


_RequestFailed = RequestFailed


def _is_firewall(status: int, payload: Any) -> bool:
    return status == 403 and isinstance(payload, dict) and payload.get("code") == shim.FIREWALL_CODE


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _detail(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return f"HTTP {status}: {detail[:200]}"
        if isinstance(detail, dict) and detail.get("code"):
            return f"HTTP {status}: {detail['code']}"
    return f"HTTP {status}"


def _with_ledger_key(req: ToolRequest, key: str) -> ToolRequest:
    """The request with IDEMPOTENCY_HEADER set to the ledger key, over any
    spelling of it the builder set."""
    headers = {str(k): str(v) for k, v in dict(req.headers or {}).items()
               if str(k).lower() != IDEMPOTENCY_HEADER.lower()}
    headers[IDEMPOTENCY_HEADER] = key
    return replace(req, headers=headers)


def _redact_args(args: Mapping[str, Any]) -> dict:
    """The arguments as the ledger stores them: capped at 8 KB."""
    out = dict(args)
    text = _dumps(out)
    if len(text.encode("utf-8")) <= ARGS_REDACTED_MAX_BYTES:
        return out
    return {"truncated": True, "partial": text[: ARGS_REDACTED_MAX_BYTES // 2]}


def taint_entries(value: Any, fields: tuple[str, ...], *, limit: int = MAX_TAINT_ENTRIES) -> list[tuple[str, str]]:
    """(field, text) for every non-empty string under a key in `fields`, at
    any depth, whitespace-collapsed, each capped at MAX_OUTPUT_CHARS (the
    model never sees more of one result than that), at most `limit`."""
    out: list[tuple[str, str]] = []
    if not fields:
        return out

    def walk(node: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, dict):
            for key, inner in node.items():
                if len(out) >= limit:
                    return
                if key in fields and isinstance(inner, str):
                    text = " ".join(inner.split())
                    if text:
                        out.append((str(key), text[:MAX_OUTPUT_CHARS]))
                    continue
                walk(inner)
        elif isinstance(node, list):
            for inner in node:
                walk(inner)

    walk(value)
    return out


def contains_tainted(value: Any, fields: tuple[str, ...]) -> bool:
    """True when any key in `fields` holds non-empty text at any depth."""
    if not fields:
        return False
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in fields and isinstance(inner, str) and inner.strip():
                return True
            if contains_tainted(inner, fields):
                return True
        return False
    if isinstance(value, list):
        return any(contains_tainted(v, fields) for v in value)
    return False


def whitelist_taint_applies(spec: ToolSpec) -> bool:
    """The whitelist taint rule of 3.8 covers the evidence and report
    namespaces and any spec naming TAINT_ANY_TEXT in its taint_fields
    (run_report)."""
    return spec.namespace in WHITELIST_TAINT_NAMESPACES or TAINT_ANY_TEXT in tuple(spec.taint_fields or ())


def _whitelisted_key(key: str) -> bool:
    return key in TAINT_WHITELIST_KEYS or key.endswith(TAINT_WHITELIST_SUFFIXES)


def free_text_entries(value: Any, *, limit: int = MAX_TAINT_ENTRIES) -> list[tuple[str, str]]:
    """(key, text) for every non-empty string whose nearest key is not an
    id, enum, date or number key (TAINT_WHITELIST_KEYS and the suffixes),
    at any depth, whitespace-collapsed and capped like taint_entries. A
    string in a list belongs to the list's key; a string with no key at all
    counts as free text."""
    out: list[tuple[str, str]] = []

    def walk(node: Any, key: str | None) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, Mapping):
            for inner_key, inner in node.items():
                walk(inner, str(inner_key))
        elif isinstance(node, list):
            for inner in node:
                walk(inner, key)
        elif isinstance(node, str):
            if key is not None and _whitelisted_key(key):
                return
            text = " ".join(node.split())
            if text:
                out.append((key or "value", text[:MAX_OUTPUT_CHARS]))

    walk(value, None)
    return out
