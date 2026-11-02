"""POST /agent-worker/v1/jobs/{id}/turns: BDR runs the model (SPEC.md 1, D1, D2).

The worker never talks to a model provider. After each tool it ran, it posts
the observation here; BDR answers with exactly one validated tool call, a
202 "someone is thinking", or a stop. `handle_turn` is a plain `def` (sync
SDKs, sync Supabase client) and runs, in order:

 1. Fence. The job is `running`, leased to this worker at this epoch and
    boot (browser_agent_fence; lease_lost / stale_boot / job_not_running).
 2. Idempotency, keyed (job_id, turn_no = prev_turn_no + 1):
      issued (this epoch)       -> the stored action again (no model call);
      thinking, not expired     -> 202 {retry_after: 5};
      thinking, expired         -> takeover by compare-and-set; the lost
                                   call is billed as an ESTIMATE
                                   (billing_unknown, llm.takeover step);
      failed / abandoned        -> retaken for this epoch;
      absent                    -> inserted as thinking (owner = this process).
 3. Scrub before anything persists (services/browser_agents/redaction.py):
    known secrets in plain, URL-encoded and base64 forms, credential-like
    URL query values, invisible characters. A password, helper code or
    worker token in the observation fails the job secret_leak_blocked and
    only {"redacted": true} is stored.
 4. Caps (O4): kill switch, cancel, the global daily spend ($25 default,
    window = the later of Pacific midnight and the last IT Admin reset),
    then the job's model steps, tokens, cost ($2 default) and active time.
    Caps are read LIVE, so an IT Admin edit applies to the next turn.
 5. Context: versioned system prompt, the type's tools, the seed facts, all
    prior turns of every epoch, the last 2 screenshots as images (older ones
    become a one-line placeholder), read_page output older than 3 turns cut
    to 1 KB. Tool output is wrapped in <untrusted_page_content>; helper
    messages (read from the DB, never from the worker's text) in
    <helper_message>. A resumed attempt gets one fixed system note.
 6. Provider call through llm_gate (tier job), 90 s timeout, one retry on a
    transient error inside the 120 s thinking window. Stateless.
 7. Errors: transient -> stop provider_outage with retry_at (5, 15, 60 min);
    configuration (key, model, credits) -> the type is disabled and its
    probe cleared, stop provider_config. One IT Admin bell per provider per
    hour (compare-and-set in browser_agent_runtime_state.provider_alerts).
 8. Record: llm_request / llm_response artifacts (scrubbed), the turn row
    (usage, cost, request id, hashes), the job counters, an llm.turn step,
    and an llm_call_log mirror (feature browser_agent, tier job).
 9. Validate: exactly one tool call, the per-type tool set and schema
    (protocol.validate_tool_call), the code ceiling for URLs, the seed's
    project/company/RFI for site tools, help-file and message ownership,
    and finish(captured) only with a stored rfi_detail_json. A violation is
    sent back to the model as an error result (it counts as a model step);
    after two violations BDR issues a safe fallback action itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.core import supabase_client
from app.core.agent_auth import AgentLease, agent_error, rpc
from app.core.config import get_settings
from app.core.redact import redact_url
from app.services import llm_gate
from app.services.browser_agents import artifacts as artifact_store
from app.services.browser_agents import llm as providers
from app.services.browser_agents import prompts, protocol, queue, redaction
from app.services.browser_agents.llm.base import (
    CONFIG_KINDS,
    CompleteArgs,
    ImagePart,
    LlmResult,
    Message,
    ToolCall,
    classify,
    is_transient,
    safe_error_text,
)
from app.services.browser_agents.protocol import ErrorCode

logger = logging.getLogger(__name__)

UTC = timezone.utc
TURNS = "browser_agent_turns"
JOBS = queue.JOBS

# Who holds a thinking turn: this process. Survives nothing (a redeploy
# leaves the row to expire and be taken over), which is the point.
WORKER_ID = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

THINKING_SECONDS = protocol.TURN_THINKING_SECONDS          # 120
PENDING_RETRY_SECONDS = protocol.TURN_PENDING_RETRY_SECONDS
MAX_OUTPUT_TOKENS = 4096
PROVIDER_TIMEOUT_SECONDS = 90.0
GATE_WAIT_SECONDS = 20.0               # a turn waits at most this long for an llm_gate slot
CALL_BUDGET_SECONDS = 110.0            # every provider call of one turn ends inside the thinking window
MIN_RETRY_SECONDS = 15.0
MAX_INVALID_REPLIES = 2                # re-asks after a refused reply before BDR's fallback action
SCREENSHOTS_KEPT = 2
READ_PAGE_FULL_TURNS = 3
OLD_READ_PAGE_CHARS = 1024
OUTAGE_BACKOFF_MINUTES = (5, 15, 60)
PROVIDER_ALERT_SECONDS = 3600
DEFAULT_REQUEST_TOKENS_ESTIMATE = 20_000
STEP_TEXT_MAX = 16_384
ASSISTANT_TEXT_MAX = 4000
ERROR_MAX = 500
_MTOK = Decimal(1_000_000)
_QUANT = Decimal("0.000001")


@dataclass
class TurnReply:
    status_code: int
    body: dict[str, Any]


class _Stop(Exception):
    """Internal: end the turn with a TurnStopResponse."""

    def __init__(self, reason: str, *, detail: str | None = None, retry_at: datetime | None = None,
                 error_kind: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.retry_at = retry_at
        self.error_kind = error_kind or reason


class _ProviderFailed(Exception):
    def __init__(self, kind: str, exc: Exception | None):
        super().__init__(kind)
        self.kind = kind
        self.exc = exc


# ── small helpers ─────────────────────────────────────────────────────────────


def _sb():
    return supabase_client.get_supabase()


def _now() -> datetime:
    return queue.now_utc()


def _iso(dt: datetime) -> str:
    return queue.iso(dt)


def _one(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except Exception:  # noqa: BLE001 - a malformed numeric reads as zero
        return Decimal(0)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "23505" or "23505" in str(exc) or "duplicate key" in str(exc)


def public_action(action: dict[str, Any] | None) -> dict[str, Any] | None:
    """The worker/UI view of a stored action: {call_id, tool, args} only
    (provider replay state and refused calls stay on the server)."""
    if not action:
        return None
    return {"call_id": action.get("call_id"), "tool": action.get("tool"), "args": action.get("args") or {}}


def turn_cost(usage_in: int, usage_out: int, price_in: Any, price_out: Any) -> Decimal:
    """Dollar cost from the type's per-MTok prices (cached input is billed
    at the input price: no cached price is configured, and over-counting is
    the safe side of a cap)."""
    return _q((Decimal(max(0, usage_in)) * _dec(price_in) + Decimal(max(0, usage_out)) * _dec(price_out)) / _MTOK)


# ── step and artifact writes ──────────────────────────────────────────────────


def _step(job_id: str, epoch: int, kind: str, summary: str, *, actor: str = "bdr", worker_id: str | None = None,
          idem_key: str | None = None, tool_name: str | None = None, args: Any = None, result: Any = None,
          turn_no: int | None = None, ok: bool | None = None, duration_ms: int | None = None,
          llm_request_sha256: str | None = None, llm_response_sha256: str | None = None,
          url_after: str | None = None, screenshot_artifact_id: str | None = None) -> None:
    """One BDR-written ledger row (seq by trigger). Never raises; a resent
    idem_key is a quiet no-op."""
    row: dict[str, Any] = {
        "job_id": job_id, "epoch": int(epoch), "worker_id": worker_id, "kind": kind, "actor": actor,
        "summary": summary[: protocol.STEP_SUMMARY_MAX], "idem_key": idem_key, "tool_name": tool_name,
        "args": args, "result": result, "turn_no": turn_no, "ok": ok, "duration_ms": duration_ms,
        "llm_request_sha256": llm_request_sha256, "llm_response_sha256": llm_response_sha256,
        "url_after": url_after, "screenshot_artifact_id": screenshot_artifact_id,
    }
    if args is not None or result is not None:
        row["payload_sha256"] = _sha(_canonical(args) + b"||" + _canonical(result))
    try:
        _sb().table("browser_agent_steps").insert({k: v for k, v in row.items() if v is not None}).execute()
    except Exception as exc:  # noqa: BLE001 - the ledger never breaks the turn
        if not _is_unique_violation(exc):
            logger.exception("browser agents: step %s for job %s not written", kind, job_id)


def _store_json_artifact(job_id: str, kind: str, payload: Any, filename: str) -> tuple[str | None, str]:
    """(artifact_id, sha256) for an llm_request / llm_response body. The
    payload is already scrubbed. Idempotent on (job, kind, sha256)."""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    sha = _sha(data)
    try:
        existing = artifact_store.find_existing(job_id, kind, sha)
        if existing:
            return str(existing["id"]), sha
        path = artifact_store.object_path(job_id, kind, sha, "application/json")
        artifact_store.upload_object(path, data, "application/json")
        row = {"job_id": job_id, "kind": kind, "storage_path": path, "filename": filename,
               "mime_type": "application/json", "sniffed_mime": "application/json",
               "size_bytes": len(data), "sha256": sha}
        try:
            inserted = _sb().table("browser_agent_artifacts").insert(row).execute().data or []
        except Exception as exc:  # noqa: BLE001
            if not _is_unique_violation(exc):
                raise
            inserted = []
        if inserted:
            return str(inserted[0]["id"]), sha
        again = artifact_store.find_existing(job_id, kind, sha)
        return (str(again["id"]) if again else None), sha
    except Exception:  # noqa: BLE001 - the hash is still recorded on the turn and the step
        logger.exception("browser agents: %s artifact for job %s not stored", kind, job_id)
        return None, sha


# ── settings, caps, spend ─────────────────────────────────────────────────────


def _spend_today() -> dict[str, Any]:
    return _one(_sb().rpc("browser_agent_spend_today", {}).execute().data)


def _type_row(key: str) -> dict[str, Any]:
    rows = _sb().table("browser_agent_types").select("*").eq("key", key).limit(1).execute().data or []
    return rows[0] if rows else {}


def cap_reset_base(job: dict[str, Any]) -> int:
    """The turn number a helper's cap_reached retry reset the step count at
    (jobs.result.cap_reset.after_turn_no, written by help.py); 0 when the
    counters were never reset. Turns at or below it no longer count."""
    reset = (job.get("result") or {}).get("cap_reset") if isinstance(job.get("result"), dict) else None
    try:
        return int((reset or {}).get("after_turn_no") or 0)
    except (TypeError, ValueError):
        return 0


def _steps_used(turns: list[dict[str, Any]], base: int = 0) -> int:
    """Model steps charged so far: every issued turn plus every refused reply
    (turns at or below `base`, a cap reset, are not counted)."""
    used = 0
    for t in turns:
        if t.get("status") == "issued" and int(t.get("turn_no") or 0) > base:
            used += 1 + len((t.get("action") or {}).get("rejected") or [])
    return used


@dataclass
class Caps:
    max_steps: int
    max_tokens: int
    max_active_seconds: int
    per_job_cap: Decimal
    daily_cap: Decimal
    spend_today: Decimal
    window_started_at: str | None

    def left(self, job: dict[str, Any], steps_used: int) -> protocol.TurnCaps:
        tokens = int(job.get("tokens_in") or 0) + int(job.get("tokens_out") or 0)
        cost_left = min(self.per_job_cap - _dec(job.get("cost_usd")), self.daily_cap - self.spend_today)
        return protocol.TurnCaps(
            steps_left=max(0, self.max_steps - steps_used),
            tokens_left=max(0, self.max_tokens - tokens),
            cost_left_usd=float(max(Decimal(0), cost_left).quantize(Decimal("0.0001"))),
        )


def _load_caps(job: dict[str, Any], settings_row: dict[str, Any], type_row: dict[str, Any]) -> Caps:
    snap_type = (job.get("config_snapshot") or {}).get("type") or {}

    def pick(col: str, default: int) -> int:
        value = type_row.get(col, snap_type.get(col))
        return int(value) if value is not None else default

    spend = _spend_today()
    return Caps(
        max_steps=pick("max_steps", protocol.MAX_STEPS_DEFAULT),
        max_tokens=pick("max_total_tokens", protocol.MAX_TOTAL_TOKENS_DEFAULT),
        max_active_seconds=pick("max_active_seconds", protocol.MAX_ACTIVE_SECONDS_DEFAULT),
        per_job_cap=_dec(settings_row.get("per_job_cost_cap_usd", protocol.PER_JOB_COST_CAP_USD_DEFAULT)),
        daily_cap=_dec(spend.get("daily_cap_usd", settings_row.get("daily_cost_cap_usd",
                                                                    protocol.DAILY_COST_CAP_USD_DEFAULT))),
        spend_today=_dec(spend.get("spend_usd")),
        window_started_at=spend.get("window_started_at"),
    )


def _check_caps(job: dict[str, Any], settings_row: dict[str, Any], caps: Caps, steps_used: int) -> None:
    """Raise _Stop on the first cap that is hit (order: kill switch, cancel,
    global daily spend, then the job's own caps)."""
    if not settings_row.get("enabled"):
        raise _Stop("disabled", detail="Browser agents are switched off.")
    if job.get("cancel_requested_at"):
        raise _Stop("canceled", detail="The job was canceled.")
    if caps.spend_today >= caps.daily_cap:
        raise _Stop("daily_cap_reached",
                    detail=f"The ${caps.daily_cap} daily model spend cap is reached.")
    if steps_used >= caps.max_steps:
        raise _Stop("cap_reached", detail=f"The job used its {caps.max_steps} model steps.")
    tokens = int(job.get("tokens_in") or 0) + int(job.get("tokens_out") or 0)
    if tokens >= caps.max_tokens:
        raise _Stop("cap_reached", detail=f"The job used its {caps.max_tokens} model tokens.")
    if _dec(job.get("cost_usd")) >= caps.per_job_cap:
        raise _Stop("cap_reached", detail=f"The job reached its ${caps.per_job_cap} model spend cap.")
    if int(job.get("active_seconds") or 0) >= caps.max_active_seconds:
        raise _Stop("cap_reached", detail="The job used its active time.")


# ── the turn row ──────────────────────────────────────────────────────────────


def _get_turn(job_id: str, turn_no: int) -> dict[str, Any] | None:
    rows = _sb().table(TURNS).select("*").eq("job_id", job_id).eq("turn_no", turn_no).limit(1).execute().data or []
    return rows[0] if rows else None


def _all_turns(job_id: str) -> list[dict[str, Any]]:
    return _sb().table(TURNS).select("*").eq("job_id", job_id).order("turn_no").execute().data or []


def _charge_job(job_id: str, *, cost: Decimal = Decimal(0), tokens_in: int = 0, tokens_out: int = 0,
                turn_no: int | None = None) -> None:
    """jobs.cost_usd / tokens / turn_count, compare-and-set on the counters
    themselves (charged even if the lease moved on: the money is spent)."""
    sb = _sb()
    for _ in range(6):
        rows = sb.table(JOBS).select("turn_count, tokens_in, tokens_out, cost_usd").eq("id", job_id).limit(1) \
            .execute().data or []
        if not rows:
            return
        cur = rows[0]
        patch: dict[str, Any] = {
            "tokens_in": int(cur.get("tokens_in") or 0) + tokens_in,
            "tokens_out": int(cur.get("tokens_out") or 0) + tokens_out,
            "cost_usd": float(_q(_dec(cur.get("cost_usd")) + cost)),
        }
        if turn_no is not None:
            patch["turn_count"] = max(int(cur.get("turn_count") or 0), turn_no)
        q = (sb.table(JOBS).update(patch).eq("id", job_id)
             .eq("tokens_in", cur.get("tokens_in") or 0).eq("tokens_out", cur.get("tokens_out") or 0)
             .eq("turn_count", cur.get("turn_count") or 0).eq("cost_usd", cur.get("cost_usd") or 0))
        if q.execute().data:
            return
    logger.error("browser agents: job %s counters not updated after retries", job_id)


def _estimate_lost_call(turns: list[dict[str, Any]], price_in: Any, price_out: Any) -> Decimal:
    """Takeover estimate: max_output_tokens at the output price plus the
    last known request size (or a default) at the input price."""
    request_tokens = DEFAULT_REQUEST_TOKENS_ESTIMATE
    for t in reversed(turns):
        if t.get("input_tokens"):
            request_tokens = int(t["input_tokens"])
            break
    return turn_cost(request_tokens, MAX_OUTPUT_TOKENS, price_in, price_out)


def _thinking_patch(now: datetime) -> dict[str, Any]:
    return {"status": "thinking", "thinking_owner": WORKER_ID,
            "thinking_until": _iso(now + timedelta(seconds=THINKING_SECONDS))}


def _extend_thinking(job_id: str, turn_no: int) -> bool:
    data = (
        _sb().table(TURNS).update({"thinking_until": _iso(_now() + timedelta(seconds=THINKING_SECONDS))})
        .eq("job_id", job_id).eq("turn_no", turn_no).eq("status", "thinking").eq("thinking_owner", WORKER_ID)
        .execute()
    ).data
    return bool(data)


# ── observation: validation and scrub ─────────────────────────────────────────


def _job_help_ids(job_id: str) -> list[str]:
    rows = _sb().table("browser_agent_help_waiters").select("help_id").eq("job_id", job_id).execute().data or []
    return sorted({str(r["help_id"]) for r in rows if r.get("help_id")})


def _check_observation_refs(job_id: str, body: protocol.TurnRequest) -> None:
    """IDOR: the screenshot is this job's, and every helper message id the
    worker says it saw is a message on a help request this job waits on."""
    sb = _sb()
    if body.screenshot_artifact_id:
        rows = (
            sb.table("browser_agent_artifacts").select("id, kind").eq("id", str(body.screenshot_artifact_id))
            .eq("job_id", job_id).limit(1).execute()
        ).data or []
        if not rows or rows[0].get("kind") != "screenshot":
            raise agent_error(ErrorCode.IDOR, "screenshot does not belong to this job")
    ids = sorted(set(body.helper_message_ids_seen))
    if ids:
        help_ids = _job_help_ids(job_id)
        rows: list[dict[str, Any]] = []
        if help_ids:
            rows = (
                sb.table("browser_agent_help_messages").select("id").in_("help_id", help_ids).in_("id", ids)
                .execute()
            ).data or []
        if {int(r["id"]) for r in rows} != set(ids):
            raise agent_error(ErrorCode.IDOR, "helper message does not belong to this job")


def _scrubbed_observation(body: protocol.TurnRequest, secrets: redaction.SecretSet,
                          report: redaction.ScrubReport) -> dict[str, Any]:
    results = [
        {"call_id": r.call_id, "ok": r.ok, "content": redaction.scrub(r.content, secrets, report)}
        for r in body.tool_results
    ]
    return {
        "results": results,
        "url": redaction.scrub(body.url, secrets, report) if body.url else None,
        "title": redaction.scrub(body.title, secrets, report) if body.title else None,
        "helper_message_ids_seen": sorted(set(body.helper_message_ids_seen)),
        **({"redactions": list(report.hits)} if report.hits else {}),
    }


# ── context building ──────────────────────────────────────────────────────────


def _escape_untrusted(text: str) -> str:
    text = redaction.strip_invisible(text)
    for tag in ("untrusted_page_content", "helper_message"):
        text = text.replace(f"</{tag}", f"&lt;/{tag}").replace(f"<{tag}", f"&lt;{tag}")
    return text


def _attr(value: str | None) -> str:
    return _escape_untrusted(value or "").replace('"', "%22").replace("<", "%3C").replace(">", "%3E")[:2048]


def wrap_untrusted(content: str, url: str | None) -> str:
    return f'<untrusted_page_content url="{_attr(url)}">\n{_escape_untrusted(content)}\n</untrusted_page_content>'


def wrap_helper(body: str, label: str | None) -> str:
    return f'<helper_message from="{_attr(label or "G3 helper")}">\n{_escape_untrusted(body)}\n</helper_message>'


def _helper_messages(ids: set[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    rows = (
        _sb().table("browser_agent_help_messages")
        .select("id, author_kind, author_label, body, is_secret, action, artifact_ids")
        .in_("id", sorted(ids)).execute()
    ).data or []
    out = {int(r["id"]): r for r in rows}
    artifact_ids = sorted({str(a) for r in rows for a in (r.get("artifact_ids") or [])})
    files: dict[str, dict[str, Any]] = {}
    if artifact_ids:
        for a in (
            _sb().table("browser_agent_artifacts").select("id, filename, mime_type, size_bytes, attachable, kind")
            .in_("id", artifact_ids).execute()
        ).data or []:
            if a.get("kind") == "helper_upload":
                files[str(a["id"])] = a
    for r in out.values():
        r["files"] = [files[str(a)] for a in (r.get("artifact_ids") or []) if str(a) in files]
    return out


def helper_file_lines(files: list[dict[str, Any]]) -> list[str]:
    """One line per helper file: what attach_helper_file needs (the
    artifact id) and whether it may be attached. Names are untrusted."""
    return [
        f"Helper file artifact_id={a['id']} name=\"{_attr(a.get('filename') or 'file')}\" "
        f"type={_attr(a.get('mime_type') or 'application/octet-stream')} "
        f"size_bytes={int(a.get('size_bytes') or 0)} attachable={'true' if a.get('attachable') else 'false'}"
        for a in files
    ]


def _screenshot(artifact_id: str) -> ImagePart | None:
    try:
        rows = (
            _sb().table("browser_agent_artifacts").select("storage_path, mime_type, kind").eq("id", artifact_id)
            .limit(1).execute()
        ).data or []
        if not rows or rows[0].get("kind") != "screenshot":
            return None
        data = _sb().storage.from_(artifact_store.BUCKET).download(rows[0]["storage_path"])
        mime = rows[0].get("mime_type") or "image/png"
        return ImagePart(mime if mime in ("image/png", "image/jpeg") else "image/png", bytes(data))
    except Exception:  # noqa: BLE001 - a missing image degrades to the placeholder line
        logger.warning("browser agents: screenshot %s unreadable for the model", artifact_id)
        return None


def _seed_text(job: dict[str, Any], agent_type: str, api_host: str | None) -> str:
    seed = dict(job.get("input") or {})
    seed.pop("source_email_ids", None)            # ids only; never useful to the model
    facts: dict[str, Any] = {"agent_type": agent_type, "seed": seed}
    if agent_type == "canary" and api_host:
        facts["canary_url"] = f"https://{api_host}{protocol.API_PREFIX}/canary/page"
    checkpoint = job.get("checkpoint") or {}
    if checkpoint:
        facts["checkpoint"] = {k: checkpoint.get(k) for k in ("phase", "company_id", "rfi_id") if checkpoint.get(k)}
    return (
        "BDR job facts (trusted, written by BDR):\n"
        + json.dumps(facts, indent=1, sort_keys=True, default=str)
        + f"\nAttempt {int(job.get('attempts') or 1)}. You have these tools: "
        + ", ".join(protocol.TOOLS_BY_TYPE[agent_type]) + "."
    )


def _tool_text(tool: str, content: str, url: str | None, age: int) -> str:
    if tool == "read_page" and age > READ_PAGE_FULL_TURNS and len(content) > OLD_READ_PAGE_CHARS:
        content = content[:OLD_READ_PAGE_CHARS] + "\n[older read_page output cut to 1 KB]"
    return wrap_untrusted(content, url)


@dataclass
class _Context:
    messages: list[Message]
    screenshot_ids: list[str]


def build_context(job: dict[str, Any], agent_type: str, turns: list[dict[str, Any]], current_no: int,
                  current_obs: dict[str, Any], current_shot: str | None, *, api_host: str | None) -> _Context:
    """The neutral transcript for turn `current_no` (see the module docstring)."""
    epoch = int(job.get("lease_epoch") or 0)
    issued = [t for t in turns if t.get("status") == "issued" and int(t["turn_no"]) < current_no]
    # (turn_no, observation, screenshot id) for every observation, the current one last.
    observations = [(int(t["turn_no"]), t.get("tool_results") or {}, t.get("screenshot_artifact_id")) for t in issued]
    observations.append((current_no, current_obs, current_shot))
    shot_turns = [(n, str(s)) for n, _, s in observations if s]
    keep = {s for _, s in shot_turns[-SCREENSHOTS_KEPT:]}
    images = {s: _screenshot(s) for s in keep}
    helper_ids: set[int] = set()
    for _, obs, _ in observations:
        helper_ids.update(int(i) for i in (obs.get("helper_message_ids_seen") or []))
    helpers = _helper_messages(helper_ids)
    actions = {int(t["turn_no"]): t.get("action") or {} for t in issued}
    resumed = bool(issued) and not any(int(t.get("epoch") or 0) == epoch for t in issued)

    messages: list[Message] = [Message(role="user", text=_seed_text(job, agent_type, api_host))]
    last_url: str | None = None
    for n, obs, shot in observations:
        prev = actions.get(n - 1)
        results = obs.get("results") or []
        url = obs.get("url") or last_url
        if prev and prev.get("call_id"):
            hit = next((r for r in results if r.get("call_id") == prev.get("call_id")), None)
            if hit is not None:
                text = _tool_text(prev.get("tool") or "", str(hit.get("content") or ""), url, current_no - n)
                messages.append(Message(role="tool", call_id=prev["call_id"], tool_name=prev.get("tool"),
                                        text=text, ok=bool(hit.get("ok"))))
            else:
                messages.append(Message(role="tool", call_id=prev["call_id"], tool_name=prev.get("tool"),
                                        text="Not run: the attempt ended before this action ran.", ok=False))
        lines: list[str] = []
        if obs.get("redacted"):
            lines.append("[this observation was withheld by BDR]")
        if obs.get("url") or obs.get("title"):
            lines.append("Current page: " + wrap_untrusted(f"{obs.get('title') or ''}".strip(), obs.get("url")))
        elif n == 1:
            lines.append("The browser is on a blank page.")
        for mid in obs.get("helper_message_ids_seen") or []:
            msg = helpers.get(int(mid))
            files = helper_file_lines(msg.get("files") or []) if msg and msg.get("author_kind") == "helper" else []
            if msg and msg.get("author_kind") == "helper" and not msg.get("is_secret") and (msg.get("body") or files):
                body = "\n".join([str(msg.get("body") or "(no text, files only)"), *files])
                lines.append(f"Helper message id {int(mid)}:\n" + wrap_helper(body, msg.get("author_label")))
            elif msg and msg.get("action") in ("retry", "give_up", "skip_daily"):
                lines.append(f"Helper action on the help request: {msg['action']}.")
        if n == current_no and resumed:
            last_no = int(issued[-1]["turn_no"])
            lines.append(
                f"Attempt {int(job.get('attempts') or 1)}: the previous attempt was interrupted after step "
                f"{last_no} at URL {_attr(last_url) or 'unknown'}; the browser restarted on a blank page."
            )
        imgs: tuple[ImagePart, ...] = ()
        if shot:
            image = images.get(str(shot))
            if image is not None:
                imgs = (image,)
                lines.append("Screenshot of the current page attached.")
            else:
                lines.append(f"[screenshot {shot} omitted]")
        messages.append(Message(role="user", text="\n".join(lines) or "(no page details)", images=imgs))
        if n != current_no:
            action = actions.get(n) or {}
            call = ToolCall(call_id=str(action.get("call_id") or f"bdr_{n}"), name=str(action.get("tool") or ""),
                            args=action.get("args") or {}, replay=action.get("replay"))
            messages.append(Message(role="assistant", text=str(action.get("assistant_text") or ""), tool_call=call))
        last_url = obs.get("url") or last_url
    return _Context(messages=messages, screenshot_ids=sorted(keep))


# ── validation ────────────────────────────────────────────────────────────────


def allowed_nav_hosts(agent_type: str, type_row: dict[str, Any], api_host: str | None) -> set[str]:
    ceiling = protocol.CEILINGS.get(agent_type, {})
    hosts = {(api_host or "").lower() if h == "$api_host" else h.lower() for h in ceiling.get("nav_hosts", [])}
    hosts.discard("")
    narrowing = type_row.get("host_narrowing")
    if narrowing is not None:
        hosts &= {str(h).lower() for h in narrowing}
    return hosts


def check_url(agent_type: str, url: str, hosts: set[str]) -> str | None:
    """Reason the URL is refused, or None (spec 4.3 code ceiling)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "url_invalid"
    if parts.scheme != "https":
        return "url_not_https"
    if parts.username or parts.password:
        return "url_has_credentials"
    if port not in (None, 443):
        return "url_port_not_allowed"
    if (parts.hostname or "").lower() not in hosts:
        return "host_not_allowed"
    paths = protocol.CEILINGS.get(agent_type, {}).get("nav_paths", [])
    if not any(re.match(p, parts.path or "/") for p in paths):
        return "path_not_allowed"
    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        if protocol.TOKEN_QUERY_KEY_RE.search(key):
            return "query_not_allowed"
    return None


def _check_site_args(job: dict[str, Any], args: Any, hosts_allowed: set[str]) -> str | None:
    seed = job.get("input") or {}
    host = getattr(args, "host", None)
    if host is not None and str(host).lower() not in hosts_allowed:
        return "host_not_allowed"
    project_id = getattr(args, "project_id", None)
    if project_id is not None and project_id != str(seed.get("external_project_id")):
        return "project_not_this_job"
    company_id = getattr(args, "company_id", None)
    checkpoint = job.get("checkpoint") or {}
    known_company = seed.get("company_id") or checkpoint.get("company_id")
    if company_id is not None and known_company and company_id != str(known_company):
        return "company_not_this_job"
    rfi_id = getattr(args, "rfi_id", None)
    if rfi_id is not None and seed.get("rfi_id") and rfi_id != str(seed.get("rfi_id")):
        return "rfi_not_this_job"
    return None


def check_call(job: dict[str, Any], agent_type: str, call: ToolCall, type_row: dict[str, Any],
               api_host: str | None) -> str | None:
    """Policy check of one model tool call; the refusal reason or None."""
    if call.parse_error:
        return f"invalid_args: {call.parse_error}"
    try:
        args = protocol.validate_tool_call(agent_type, call.name, call.args)
    except protocol.ToolCallInvalid as exc:
        return exc.reason
    hosts = allowed_nav_hosts(agent_type, type_row, api_host)
    if call.name == "navigate":
        return check_url(agent_type, args.url, hosts)
    if call.name in protocol.SITE_TOOLS_PROCORE:
        return _check_site_args(job, args, hosts)
    sb = _sb()
    if call.name == "attach_helper_file":
        help_ids = _job_help_ids(str(job["id"]))
        rows = (
            sb.table("browser_agent_artifacts").select("id, kind, attachable, help_id")
            .eq("id", str(args.artifact_id)).limit(1).execute()
        ).data or []
        if not rows or str(rows[0].get("help_id")) not in help_ids or rows[0].get("kind") != "helper_upload":
            return "file_not_from_this_job_help"
        if not rows[0].get("attachable"):
            return "file_not_attachable"
        return None
    if call.name == "add_helper_note":
        help_ids = _job_help_ids(str(job["id"]))
        rows = (
            sb.table("browser_agent_help_messages").select("id, help_id, author_kind, is_secret")
            .in_("id", sorted(set(args.message_ids))).execute()
        ).data or []
        ok = {int(r["id"]) for r in rows if str(r.get("help_id")) in help_ids
              and r.get("author_kind") == "helper" and not r.get("is_secret")}
        if ok != set(args.message_ids):
            return "message_not_a_helper_message_of_this_job"
        return None
    if call.name == "finish":
        try:
            mapped = protocol.finish_to_job(agent_type, args)
        except protocol.ToolCallInvalid as exc:
            return exc.reason
        if mapped["action"] == "rfi_capture":
            rows = (
                sb.table("browser_agent_artifacts").select("id").eq("job_id", str(job["id"]))
                .eq("kind", "rfi_detail_json").limit(1).execute()
            ).data or []
            if not rows:
                return "captured_without_detail: call capture_rfi_detail first"
        return None
    return None


def _fallback_action(agent_type: str, turn_no: int, reason: str) -> dict[str, Any]:
    """BDR's own action after repeated refused replies: ask for help when the
    type can, else end the job."""
    note = f"The model did not produce a valid next step ({reason[:200]})."
    if "request_help" in protocol.TOOLS_BY_TYPE[agent_type]:
        return {"call_id": f"bdr_{turn_no}", "tool": "request_help",
                "args": {"reason_code": "other", "message": note}}
    return {"call_id": f"bdr_{turn_no}", "tool": "finish",
            "args": {"outcome": "cannot_complete", "summary": note[: protocol.FINISH_SUMMARY_MAX],
                     "error_kind": "other"}}


# ── provider call ─────────────────────────────────────────────────────────────


def _request_record(args: CompleteArgs, provider: str, prompt: prompts.Prompt, turn_no: int,
                    attempt: int) -> dict[str, Any]:
    """The llm_request artifact body: the transcript as sent, images as
    hashes (the screenshots are their own artifacts), tools by name."""
    msgs = []
    for m in args.messages:
        item: dict[str, Any] = {"role": m.role}
        if m.text:
            item["text"] = m.text
        if m.images:
            item["images"] = [{"media_type": i.media_type, "sha256": _sha(i.data), "bytes": len(i.data)}
                              for i in m.images]
        if m.tool_call is not None:
            item["tool_call"] = {"call_id": m.tool_call.call_id, "name": m.tool_call.name, "args": m.tool_call.args}
        if m.role == "tool":
            item.update({"call_id": m.call_id, "tool": m.tool_name, "ok": m.ok})
        msgs.append(item)
    return {
        "provider": provider, "model": args.model, "turn_no": turn_no, "attempt": attempt,
        "prompt_version": prompt.version, "prompt_sha256": prompt.sha256,
        "max_output_tokens": args.max_output_tokens, "tools": [t["name"] for t in args.tools],
        "messages": msgs,
    }


def _call_provider(adapter: providers.ProviderAdapter, args: CompleteArgs, provider: str, job_id: str,
                   turn_no: int, deadline: float) -> tuple[LlmResult, int]:
    """One logical provider call: up to two attempts (one retry on a
    transient error) inside the turn's time budget. Returns (result,
    duration_ms); raises _ProviderFailed(kind)."""
    settings = get_settings()
    gate_settings = settings.model_copy(update={"llm_background_wait_seconds": GATE_WAIT_SECONDS})
    last_kind, last_exc = "unknown", None
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining < MIN_RETRY_SECONDS:
            break
        _extend_thinking(job_id, turn_no)
        args.timeout_seconds = min(PROVIDER_TIMEOUT_SECONDS, remaining)
        started = time.monotonic()
        try:
            with llm_gate.slot(provider, llm_gate.TIER_JOB, gate_settings):
                result = adapter.complete(args)
            return result, int((time.monotonic() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - classified below
            last_kind, last_exc = classify(exc), exc
            llm_gate.log_call(feature="browser_agent", provider=provider, model=args.model,
                              tier_name=llm_gate.TIER_JOB, ok=False, error=exc,
                              duration_ms=int((time.monotonic() - started) * 1000))
            logger.warning("browser agents: %s call for job %s failed (%s, attempt %d)",
                           provider, job_id, last_kind, attempt + 1)
            if not is_transient(last_kind):
                break
    raise _ProviderFailed(last_kind, last_exc)


# ── alerts and failure paths ──────────────────────────────────────────────────


def _alert_provider(provider: str, category: str, kind: str, message: str, *, email: bool) -> None:
    """One IT Admin provider_problem notice per provider and category per
    hour, claimed by compare-and-set on runtime_state.provider_alerts."""
    sb = _sb()
    alert_key = f"{provider}:{category}"
    for _ in range(3):
        rows = sb.table("browser_agent_runtime_state").select("provider_alerts, updated_at").eq("id", 1).limit(1) \
            .execute().data or []
        if not rows:
            return
        alerts = dict(rows[0].get("provider_alerts") or {})
        last = queue.parse_ts(alerts.get(alert_key))
        now = _now()
        if last is not None and (now - last).total_seconds() < PROVIDER_ALERT_SECONDS:
            return
        alerts[alert_key] = _iso(now)
        q = sb.table("browser_agent_runtime_state").update({"provider_alerts": alerts, "updated_at": _iso(now)}) \
            .eq("id", 1)
        q = q.eq("updated_at", rows[0]["updated_at"]) if rows[0].get("updated_at") else q.is_("updated_at", "null")
        if q.execute().data:
            queue.notify_it_admin("browser_agent.provider_problem", message,
                                  {"provider": provider, "kind": kind}, email=email)
            return


def _disable_type_for_config(agent_type: str, provider: str, model: str, kind: str) -> None:
    """A key, model or credit problem: retrying cannot help and every job of
    the type would fail the same way. The type is switched off and its probe
    cleared (audited through the settings RPC) until IT Admin fixes it and a
    new Test model passes."""
    try:
        _sb().rpc("browser_agent_update_settings", {
            "p_scope": "agent_type", "p_target": agent_type,
            "p_patch": {"enabled": False, "model_probe_ok_at": None},
            "p_actor": None, "p_actor_label": "BDR (provider error)",
            "p_probe": {"ok": False, "kind": kind, "provider": provider, "model": model},
        }).execute()
    except Exception:  # noqa: BLE001 - the stop reply still goes out
        logger.exception("browser agents: disabling type %s after a provider config error failed", agent_type)


def _outage_retry_at(job_id: str) -> datetime:
    rows = (
        _sb().table("browser_agent_steps").select("id").eq("job_id", job_id).eq("kind", "llm.provider_outage")
        .execute()
    ).data or []
    minutes = OUTAGE_BACKOFF_MINUTES[min(len(rows), len(OUTAGE_BACKOFF_MINUTES) - 1)]
    return _now() + timedelta(minutes=minutes)


def _fail_turn(job_id: str, turn_no: int, error_kind: str, error: str | None, *,
               tool_results: dict[str, Any] | None = None) -> None:
    patch: dict[str, Any] = {"status": "failed", "error_kind": error_kind, "error": (error or "")[:ERROR_MAX] or None,
                             "thinking_owner": None, "thinking_until": None}
    if tool_results is not None:
        patch["tool_results"] = tool_results
    _sb().table(TURNS).update(patch).eq("job_id", job_id).eq("turn_no", turn_no).eq("status", "thinking") \
        .eq("thinking_owner", WORKER_ID).execute()


def _stop_reply(stop: _Stop) -> TurnReply:
    body = protocol.TurnStopResponse(stop=True, reason=stop.reason, retry_at=stop.retry_at, detail=stop.detail)
    return TurnReply(200, body.model_dump(mode="json"))


def _leak_blocked(lease: AgentLease, job: dict[str, Any], turn_no: int, existing: dict[str, Any] | None,
                  report: redaction.ScrubReport) -> TurnReply:
    """A known secret reached the worker's observation: store nothing of it,
    fail the job, tell IT Admin, stop the worker."""
    job_id, epoch = lease.job_id, lease.epoch
    row = {"tool_results": {"redacted": True}, "status": "failed", "epoch": epoch,
           "error_kind": "secret_leak_blocked", "error": "A known secret appeared in the observation.",
           "thinking_owner": None, "thinking_until": None}
    try:
        if existing is None:
            _sb().table(TURNS).insert({"job_id": job_id, "turn_no": turn_no, **row}).execute()
        else:
            _sb().table(TURNS).update(row).eq("job_id", job_id).eq("turn_no", turn_no) \
                .neq("status", "issued").execute()
    except Exception:  # noqa: BLE001 - the job is failed below either way
        logger.exception("browser agents: leak-blocked turn row for job %s not written", job_id)
    _step(job_id, epoch, "security.secret_leak_blocked", "A known secret appeared in the observation; withheld",
          worker_id=lease.worker.id, turn_no=turn_no, ok=False, result={"labels": report.hits})
    try:
        rpc("browser_agent_finish", {
            **lease.params(), "p_status": "failed", "p_outcome": None, "p_error_kind": "secret_leak_blocked",
            "p_error": "A known secret appeared in the worker's observation; the job was stopped.",
            "p_result": {"summary": "Stopped: a known secret appeared in the observation."},
        })
        queue.notify_job_failed(job, "secret_leak_blocked", "a known secret appeared in the worker's observation")
    except Exception:  # noqa: BLE001 - a lost lease means someone else owns the job now
        logger.exception("browser agents: failing job %s after a secret leak failed", job_id)
    return _stop_reply(_Stop("secret_leak_blocked", detail="A known secret appeared in the observation."))


# ── the handler ───────────────────────────────────────────────────────────────


def _action_reply(turn: dict[str, Any], job: dict[str, Any], caps: Caps, turns: list[dict[str, Any]]) -> TurnReply:
    action = public_action(turn.get("action")) or {}
    body = protocol.TurnActionResponse(
        turn_no=int(turn["turn_no"]),
        assistant_text=turn.get("assistant_text"),
        action=protocol.ToolCall(**action),
        caps=caps.left(job, _steps_used(turns, cap_reset_base(job))),
    )
    return TurnReply(200, body.model_dump(mode="json"))


def _fresh_job(job_id: str) -> dict[str, Any]:
    rows = _sb().table(JOBS).select("*").eq("id", job_id).limit(1).execute().data or []
    return rows[0] if rows else {}


def _claim_turn(lease: AgentLease, job: dict[str, Any], turn_no: int, existing: dict[str, Any] | None,
                obs: dict[str, Any], shot: str | None, turns_for_estimate: list[dict[str, Any]],
                snapshot_type: dict[str, Any]) -> str:
    """Make this process the thinker of turn `turn_no` for this epoch.
    Returns 'mine' or 'busy'."""
    sb = _sb()
    now = _now()
    base = {"epoch": lease.epoch, "tool_results": obs, "screenshot_artifact_id": shot, **_thinking_patch(now)}
    if existing is None:
        try:
            sb.table(TURNS).insert({"job_id": lease.job_id, "turn_no": turn_no, **base}).execute()
            return "mine"
        except Exception as exc:  # noqa: BLE001
            if not _is_unique_violation(exc):
                raise
            return "busy"
    status = existing.get("status")
    if status == "thinking":
        # A stale thinker (redeploy, crashed process): take over and bill the
        # lost call as an estimate. Same epoch only; an older epoch's
        # thinking row is retaken like an abandoned one.
        if int(existing.get("epoch") or 0) == lease.epoch:
            estimate = _estimate_lost_call(turns_for_estimate, snapshot_type.get("price_in_per_mtok"),
                                           snapshot_type.get("price_out_per_mtok"))
            q = (sb.table(TURNS).update({
                    "thinking_owner": WORKER_ID, "thinking_until": _iso(now + timedelta(seconds=THINKING_SECONDS)),
                    "billing_unknown": True,
                    "cost_usd": float(_q(_dec(existing.get("cost_usd")) + estimate)),
                 })
                 .eq("job_id", lease.job_id).eq("turn_no", turn_no).eq("status", "thinking")
                 .lte("thinking_until", _iso(now)))
            q = q.eq("thinking_owner", existing["thinking_owner"]) if existing.get("thinking_owner") \
                else q.is_("thinking_owner", "null")
            if not q.execute().data:
                return "busy"
            _charge_job(lease.job_id, cost=estimate)
            _step(lease.job_id, lease.epoch, "llm.takeover",
                  f"Turn {turn_no}: took over a stale model call (estimated ${estimate})",
                  worker_id=lease.worker.id, turn_no=turn_no,
                  result={"previous_owner": existing.get("thinking_owner"), "estimate_usd": float(estimate)})
            return "mine"
    if status in ("thinking", "failed", "abandoned"):
        patch = {**base, "action": None, "assistant_text": None, "error_kind": None, "error": None,
                 "created_at": _iso(now)}
        q = (sb.table(TURNS).update(patch).eq("job_id", lease.job_id).eq("turn_no", turn_no)
             .eq("status", status).eq("epoch", existing.get("epoch")))
        if status == "thinking":
            q = q.lte("thinking_until", _iso(now))
        return "mine" if q.execute().data else "busy"
    return "busy"


def handle_turn(lease: AgentLease, body: protocol.TurnRequest, *, worker_token: str | None,
                api_host: str | None) -> TurnReply:
    # waiting_help too: a job whose helper answered while the page was held
    # continues on the same lease (request_help returned the answer).
    job = _one(rpc("browser_agent_fence", {**lease.params(), "p_statuses": ["running", "waiting_help"]}))
    if not job:
        raise agent_error(ErrorCode.LEASE_LOST)
    if job.get("principal_kind", "org") != "org":
        raise agent_error(ErrorCode.IDOR, "user_browser_execution_unavailable")
    job_id = lease.job_id
    agent_type = str(job.get("agent_type"))
    turn_no = body.prev_turn_no + 1

    # 2a. The previous turn must be issued, and the result must answer it.
    if body.prev_turn_no > 0:
        prev = _get_turn(job_id, body.prev_turn_no)
        if not prev or prev.get("status") != "issued":
            raise agent_error(ErrorCode.INVALID_REQUEST, "prev_turn_no is not an issued turn")
        prev_call = (prev.get("action") or {}).get("call_id")
        if any(r.call_id != prev_call for r in body.tool_results):
            raise agent_error(ErrorCode.INVALID_REQUEST, "tool result call_id does not match the previous action")
    elif body.tool_results:
        raise agent_error(ErrorCode.INVALID_REQUEST, "the first turn carries no tool results")

    settings_row = queue.load_settings()
    type_row = _type_row(agent_type)

    # 2b. Idempotency: a stored action is returned as is; a live thinker gets 202.
    existing = _get_turn(job_id, turn_no)
    if existing and existing.get("status") == "issued":
        if int(existing.get("epoch") or 0) != lease.epoch:
            raise agent_error(ErrorCode.INVALID_REQUEST, "that turn was issued under an earlier lease")
        turns = _all_turns(job_id)
        return _action_reply(existing, job, _load_caps(job, settings_row, type_row), turns)
    if (existing and existing.get("status") == "thinking" and int(existing.get("epoch") or 0) == lease.epoch
            and (queue.parse_ts(existing.get("thinking_until")) or _now()) > _now()):
        return TurnReply(202, protocol.TurnPendingResponse(retry_after=PENDING_RETRY_SECONDS).model_dump(mode="json"))

    # 3. Scrub before anything persists.
    _check_observation_refs(job_id, body)
    secrets = redaction.load_secrets(job_id, worker_token=worker_token)
    report = redaction.ScrubReport()
    obs = _scrubbed_observation(body, secrets, report)
    if report.severe:
        return _leak_blocked(lease, job, turn_no, existing, report)
    shot = str(body.screenshot_artifact_id) if body.screenshot_artifact_id else None

    snapshot = job.get("config_snapshot") or {}
    snap_type = snapshot.get("type") or {}
    turns = _all_turns(job_id)
    if _claim_turn(lease, job, turn_no, existing, obs, shot, turns, snap_type) != "mine":
        return TurnReply(202, protocol.TurnPendingResponse(retry_after=PENDING_RETRY_SECONDS).model_dump(mode="json"))

    result_payload = {"results": [{**r, "content": str(r["content"])[:STEP_TEXT_MAX]} for r in obs["results"]],
                      **({"redactions": obs["redactions"]} if obs.get("redactions") else {})}
    _step(job_id, lease.epoch, "tool.result",
          f"Turn {turn_no}: observation" + (f" ({len(obs['results'])} result)" if obs["results"] else ""),
          actor="worker", worker_id=lease.worker.id, idem_key=f"bdr:turn:{turn_no}:e{lease.epoch}:result",
          turn_no=turn_no, result=result_payload,
          url_after=redact_url(obs["url"])[: protocol.URL_MAX] if obs.get("url") else None,
          screenshot_artifact_id=shot)

    try:
        return _think(lease, job, agent_type, turn_no, obs, shot, settings_row, type_row, secrets, api_host)
    except _Stop as stop:
        _fail_turn(job_id, turn_no, stop.error_kind, stop.detail)
        _step(job_id, lease.epoch, "llm.stop", f"Turn {turn_no}: stop ({stop.reason})",
              worker_id=lease.worker.id, turn_no=turn_no, ok=False,
              result={"reason": stop.reason, "detail": stop.detail,
                      "retry_at": _iso(stop.retry_at) if stop.retry_at else None})
        return _stop_reply(stop)
    except Exception:
        # Anything unexpected: give the turn back (failed, retakeable) so the
        # worker's retry does not wait out the thinking window.
        try:
            _fail_turn(job_id, turn_no, "internal_error", "BDR could not finish this turn.")
        except Exception:  # noqa: BLE001
            logger.exception("browser agents: releasing turn %s of job %s failed", turn_no, job_id)
        raise


def _think(lease: AgentLease, job: dict[str, Any], agent_type: str, turn_no: int, obs: dict[str, Any],
           shot: str | None, settings_row: dict[str, Any], type_row: dict[str, Any],
           secrets: redaction.SecretSet, api_host: str | None) -> TurnReply:
    job_id = lease.job_id
    job = {**job, **_fresh_job(job_id)}          # counters after any takeover charge
    turns = _all_turns(job_id)
    caps = _load_caps(job, settings_row, type_row)
    steps_used = _steps_used(turns, cap_reset_base(job))
    _check_caps(job, settings_row, caps, steps_used)

    snapshot = job.get("config_snapshot") or {}
    snap_type = snapshot.get("type") or {}
    provider, model = snap_type.get("provider"), snap_type.get("model")
    price_in, price_out = snap_type.get("price_in_per_mtok"), snap_type.get("price_out_per_mtok")
    if not provider or not model or price_in is None or price_out is None:
        raise _Stop("provider_config", detail="The agent type has no model configured.",
                    error_kind="provider_config")
    try:
        prompt = prompts.get_prompt(snap_type.get("prompt_version"), agent_type)
    except prompts.UnknownPrompt:
        raise _Stop("provider_config", detail="The agent type names a prompt version this server does not have.",
                    error_kind="provider_config") from None
    if snapshot.get("prompt_sha256") != prompt.sha256:
        _sb().table(JOBS).update({"config_snapshot": {**snapshot, "prompt_version": prompt.version,
                                                      "prompt_sha256": prompt.sha256}}) \
            .eq("id", job_id).eq("lease_epoch", lease.epoch).execute()
    try:
        adapter = providers.get_adapter(provider)
    except providers.ProviderNotConfigured:
        _alert_provider(provider, "config", "not_configured",
                        f"The browser agent model provider {provider} has no API key on the server.", email=True)
        raise _Stop("provider_config", detail=safe_error_text("not_configured"), error_kind="provider_config") \
            from None

    ctx = build_context(job, agent_type, [t for t in turns if int(t["turn_no"]) != turn_no], turn_no, obs, shot,
                        api_host=api_host)
    messages = list(ctx.messages)
    tools = protocol.tools_for(agent_type)
    deadline = time.monotonic() + CALL_BUDGET_SECONDS
    total_in = total_out = total_cached = 0
    total_cost = Decimal(0)
    duration_total = 0
    rejected: list[dict[str, Any]] = []
    chosen: dict[str, Any] | None = None
    assistant_text: str | None = None
    req_ids: tuple[str | None, str] = (None, "")
    resp_ids: tuple[str | None, str] = (None, "")
    request_id: str | None = None
    actor = "model"

    for attempt in range(MAX_INVALID_REPLIES + 1):
        args = CompleteArgs(model=model, system=prompt.text, messages=messages, tools=tools,
                            max_output_tokens=MAX_OUTPUT_TOKENS)
        try:
            result, duration_ms = _call_provider(adapter, args, provider, job_id, turn_no, deadline)
        except _ProviderFailed as failed:
            if total_cost or total_in or total_out:
                _charge_job(job_id, cost=total_cost, tokens_in=total_in, tokens_out=total_out)
                _add_turn_cost(job_id, turn_no, total_cost)
            raise _provider_failure(lease, agent_type, provider, model, failed, turn_no) from None
        duration_total += duration_ms
        cost = turn_cost(result.usage.input_tokens, result.usage.output_tokens, price_in, price_out)
        total_in += result.usage.input_tokens
        total_out += result.usage.output_tokens
        total_cached += result.usage.cached_tokens
        total_cost += cost
        request_id = result.provider_request_id or request_id
        llm_gate.log_call(feature="browser_agent", provider=provider, model=model, tier_name=llm_gate.TIER_JOB,
                          ok=True, error=None, duration_ms=duration_ms, input_tokens=result.usage.input_tokens,
                          cached_tokens=result.usage.cached_tokens, output_tokens=result.usage.output_tokens,
                          cost_usd=cost)
        report = redaction.ScrubReport()
        req_body = redaction.scrub(_request_record(args, provider, prompt, turn_no, attempt), secrets, report,
                                   untrusted=False)
        resp_body = redaction.scrub({
            "text": result.text, "stop_reason": result.stop_reason, "provider_request_id": result.provider_request_id,
            "usage": {"input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
                      "cached_tokens": result.usage.cached_tokens},
            "tool_calls": [{"call_id": c.call_id, "name": c.name, "args": c.args} for c in result.tool_calls],
            "raw": result.raw,
        }, secrets, report, untrusted=False)
        req_ids = _store_json_artifact(job_id, "llm_request", req_body, f"turn-{turn_no}-{attempt}-request.json")
        resp_ids = _store_json_artifact(job_id, "llm_response", resp_body, f"turn-{turn_no}-{attempt}-response.json")

        text = redaction.scrub(result.text or "", secrets, redaction.ScrubReport(), untrusted=False)
        reason: str | None
        call: ToolCall | None = None
        if len(result.tool_calls) != 1:
            reason = "no_tool_call" if not result.tool_calls else "more_than_one_tool_call"
        else:
            raw = result.tool_calls[0]
            call = ToolCall(call_id=raw.call_id or f"call_{uuid.uuid4().hex[:20]}", name=raw.name,
                            args=redaction.scrub(raw.args, secrets, redaction.ScrubReport(), untrusted=False),
                            replay=raw.replay, parse_error=raw.parse_error)
            reason = check_call(job, agent_type, call, type_row, api_host)
        if reason is None and call is not None:
            chosen = {"call_id": call.call_id, "tool": call.name, "args": call.args}
            if call.replay:
                chosen["replay"] = call.replay
            assistant_text = text[:ASSISTANT_TEXT_MAX] or None
            _step(job_id, lease.epoch, "llm.turn", f"Turn {turn_no}: {call.name}", actor="model",
                  worker_id=lease.worker.id, tool_name=call.name, args=call.args, turn_no=turn_no, ok=True,
                  duration_ms=duration_ms, llm_request_sha256=req_ids[1], llm_response_sha256=resp_ids[1],
                  result={"provider": provider, "model": model, "prompt_sha256": prompt.sha256,
                          "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
                          "cost_usd": float(cost), "provider_request_id": result.provider_request_id})
            break
        rejected.append({"tool": call.name if call else None, "reason": reason})
        _step(job_id, lease.epoch, "llm.invalid_call",
              f"Turn {turn_no}: refused model reply ({reason})"[: protocol.STEP_SUMMARY_MAX], actor="model",
              worker_id=lease.worker.id, tool_name=call.name if call else None,
              args=call.args if call else None, turn_no=turn_no, ok=False, duration_ms=duration_ms,
              llm_request_sha256=req_ids[1], llm_response_sha256=resp_ids[1],
              result={"reason": reason, "cost_usd": float(cost)})
        over_budget = (
            _dec(job.get("cost_usd")) + total_cost >= caps.per_job_cap
            or caps.spend_today + total_cost >= caps.daily_cap
            or steps_used + 1 + len(rejected) >= caps.max_steps
            or deadline - time.monotonic() < MIN_RETRY_SECONDS
        )
        if attempt == MAX_INVALID_REPLIES or over_budget:
            break
        # Tell the model what was wrong and ask again (same turn).
        if call is not None:
            messages.append(Message(role="assistant", text=text, tool_call=call))
            messages.append(Message(role="tool", call_id=call.call_id, tool_name=call.name, ok=False,
                                    text=f"Refused by BDR: {reason}. Nothing was run. Call one valid tool."))
        else:
            if text:
                messages.append(Message(role="assistant", text=text))
            messages.append(Message(role="user", text=(
                "BDR needs exactly one tool call per reply. "
                + ("You called more than one tool. " if result.tool_calls else "You called no tool. ")
                + "Call exactly one tool now.")))

    if chosen is None:
        chosen = _fallback_action(agent_type, turn_no, rejected[-1]["reason"] if rejected else "no reply")
        assistant_text = "BDR: the model gave no valid action; asking for help." \
            if chosen["tool"] == "request_help" else "BDR: the model gave no valid action; ending the job."
        actor = "bdr"
        _step(job_id, lease.epoch, "llm.fallback", f"Turn {turn_no}: BDR fallback {chosen['tool']}",
              worker_id=lease.worker.id, tool_name=chosen["tool"], args=chosen["args"], turn_no=turn_no,
              result={"rejected": rejected})
    if rejected:
        chosen["rejected"] = rejected

    # 8. Record: the turn row (compare-and-set: still ours), then the job.
    current = _get_turn(job_id, turn_no) or {}
    patch = {
        "status": "issued", "thinking_owner": None, "thinking_until": None,
        "assistant_text": assistant_text, "action": chosen,
        "provider": provider, "model": model, "provider_request_id": request_id,
        "request_artifact_id": req_ids[0], "response_artifact_id": resp_ids[0],
        "request_sha256": req_ids[1] or None, "response_sha256": resp_ids[1] or None,
        "input_tokens": total_in, "output_tokens": total_out, "cached_tokens": total_cached,
        "cost_usd": float(_q(_dec(current.get("cost_usd")) + total_cost)),
        "duration_ms": duration_total, "error_kind": None, "error": None,
    }
    won = (
        _sb().table(TURNS).update(patch).eq("job_id", job_id).eq("turn_no", turn_no).eq("status", "thinking")
        .eq("thinking_owner", WORKER_ID).execute()
    ).data
    if not won:
        # The turn is no longer ours. A same-epoch takeover by another
        # process billed an estimate that stands in for this call. Anything
        # else (a re-claim abandoned the turn, or a later epoch retook it)
        # billed nothing, so this call's real spend is charged here: the
        # money is spent and the caps must see it.
        logger.warning("browser agents: turn %s of job %s was taken over mid-call", turn_no, job_id)
        if _lost_call_unbilled(_get_turn(job_id, turn_no), lease.epoch) and (total_cost or total_in or total_out):
            _add_turn_cost(job_id, turn_no, total_cost)
            _charge_job(job_id, cost=total_cost, tokens_in=total_in, tokens_out=total_out)
            _step(job_id, lease.epoch, "llm.late_charge",
                  f"Turn {turn_no}: charged a model call that finished after the turn was abandoned (${total_cost})",
                  worker_id=lease.worker.id, turn_no=turn_no, ok=False,
                  result={"cost_usd": float(total_cost), "input_tokens": total_in, "output_tokens": total_out})
        return TurnReply(202, protocol.TurnPendingResponse(retry_after=PENDING_RETRY_SECONDS).model_dump(mode="json"))
    _charge_job(job_id, cost=total_cost, tokens_in=total_in, tokens_out=total_out, turn_no=turn_no)
    if actor == "bdr":
        logger.info("browser agents: job %s turn %s used the BDR fallback action", job_id, turn_no)
    job_after = {**job, **_fresh_job(job_id)}
    caps_after = _load_caps(job_after, settings_row, type_row)
    issued_row = {**current, **patch, "turn_no": turn_no}
    return _action_reply(issued_row, job_after, caps_after, [*turns, issued_row])


def _lost_call_unbilled(row: dict[str, Any] | None, epoch: int) -> bool:
    """After the issue compare-and-set lost: True unless a same-epoch
    takeover billed its estimate for this call (billing_unknown on a row of
    this epoch that another process now owns)."""
    if not row:
        return True
    if row.get("status") == "abandoned" or int(row.get("epoch") or 0) != epoch:
        return True
    return not row.get("billing_unknown")


def _add_turn_cost(job_id: str, turn_no: int, cost: Decimal) -> None:
    rows = _sb().table(TURNS).select("cost_usd").eq("job_id", job_id).eq("turn_no", turn_no).limit(1).execute() \
        .data or []
    if rows:
        _sb().table(TURNS).update({"cost_usd": float(_q(_dec(rows[0].get("cost_usd")) + cost))}) \
            .eq("job_id", job_id).eq("turn_no", turn_no).execute()


def _provider_failure(lease: AgentLease, agent_type: str, provider: str, model: str,
                      failed: _ProviderFailed, turn_no: int) -> _Stop:
    """Classify a failed provider call into the stop the worker acts on
    (returned for the caller to raise)."""
    kind = failed.kind
    text = safe_error_text(kind)
    if is_transient(kind):
        retry_at = _outage_retry_at(lease.job_id)
        _step(lease.job_id, lease.epoch, "llm.provider_outage", f"Turn {turn_no}: {provider} unavailable ({kind})",
              worker_id=lease.worker.id, turn_no=turn_no, ok=False,
              result={"provider": provider, "kind": kind, "retry_at": _iso(retry_at)})
        _alert_provider(provider, "outage", kind,
                        f"The browser agent model provider {provider} is failing ({kind}). Jobs wait and retry.",
                        email=False)
        return _Stop("provider_outage", detail=text, retry_at=retry_at, error_kind="provider_outage")
    _step(lease.job_id, lease.epoch, "llm.provider_config", f"Turn {turn_no}: {provider} refused ({kind})",
          worker_id=lease.worker.id, turn_no=turn_no, ok=False, result={"provider": provider, "kind": kind})
    if kind in CONFIG_KINDS:
        _disable_type_for_config(agent_type, provider, model, kind)
        message = (f"The browser agent type {agent_type} was switched off: the model provider {provider} "
                   f"refused model {model} ({kind}). Fix the key or the model in Settings and run Test model.")
    else:
        message = (f"The browser agent model provider {provider} rejected a request for model {model} ({kind}). "
                   "Check the model in Settings and run Test model.")
    _alert_provider(provider, "config", kind, message, email=True)
    return _Stop("provider_config", detail=text, error_kind="provider_config")
