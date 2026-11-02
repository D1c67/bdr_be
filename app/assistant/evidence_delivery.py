"""Delivery of a sensitive_read result into the conversation
(docs/ASSISTANT_PHASE4_PLAN.md 3.2.4, deviation D2).

Created as a stub by WI-1; filled by WI-6. Called by
`approvals_job._outcome` after an executed sensitive_read card, before the
card's message is settled; a failure is logged there and leaves the card
executed.

No model runs here: the message is server-built and deterministic. The
route's answer goes through the spec's own trimmer (`Executor.shape`,
`response_format="detailed"`, so the role's amount rules and the short ids
apply), then becomes one assistant message:

  content  a header line naming the person, the view and the Pacific
           window, the counts, and up to 40 event lines
           "<at PT> | <kind> | <summary>", at most 3,800 characters (under
           the loop's 4,000 character history cap, so the next turn sees
           all of it), with "Showing N of M." when cut;
  blocks   one text, one table (first 25 rows), citation blocks for the
           projects the rows name, report_file when a workbook was asked;
  flags    server_data true (the next turn replays it as an untrusted data
           block, never as the assistant's own words) and tainted whenever
           the output carried free text.

The message id is uuid5 of "evidence:" + the approval id, so a replay
inserts nothing.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.assistant import repo
from app.assistant.tools._common import clip, is_uuid, pt

if TYPE_CHECKING:
    from app.assistant.executor import Executor
    from app.assistant.registry import ToolSpec

logger = logging.getLogger(__name__)

# uuid5 namespace of the evidence message ids: one message per approval, ever.
EVIDENCE_MESSAGE_NAMESPACE = uuid.UUID("0b6f9d3e-4c1a-4e57-b2d8-7a95c3e1f402")

CONTENT_MAX_CHARS = 3_800
MAX_EVENT_LINES = 40
MAX_TABLE_ROWS = 25
MAX_CITATIONS = 10
SUMMARY_MAX_CHARS = 200
CELL_MAX_CHARS = 200
# The limit the trimmer is asked for: enough for the event lines and the
# table, small enough to stay under the executor's output cap.
DELIVERY_LIMIT = MAX_EVENT_LINES

VIEW_LABELS = {
    "activity": "activity",
    "file_access": "file access",
    "notifications": "notifications",
    "ai_usage": "AI usage",
    "estimator_timeline": "estimator timeline",
}

ESTIMATOR_SUMMARY_FIELDS = (
    ("assigned_at", "Assigned"),
    ("emailed_at", "Emailed"),
    ("bell_read_at", "Bell notice read"),
    ("first_file_open_at", "First file open"),
    ("first_log_view_at", "First plans log view"),
)

TOO_LARGE_TEXT = "The result was too large to list here. Ask for a narrower window or one project."
NO_ROWS_TEXT = "No rows in this window."

_DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")


def message_id_for(approval_id: str) -> str:
    """The evidence message's deterministic id: a replay never posts twice."""
    return str(uuid.uuid5(EVIDENCE_MESSAGE_NAMESPACE, "evidence:" + str(approval_id)))


# ── Small readers ────────────────────────────────────────────────────────────


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int) -> str:
    """Plain one-line text: whitespace collapsed, dashes made commas, clipped."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return _DASHES.sub(", ", clip(value, limit) or "")


def _rows(output: Mapping[str, Any]) -> list[dict]:
    for key in ("rows", "events"):
        value = output.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, Mapping)]
    return []


def _preview_row(row: Mapping[str, Any], field: str) -> str:
    """The `after` text of the approval preview's row named `field`."""
    for prow in _dict(row.get("preview")).get("rows") or ():
        if isinstance(prow, Mapping) and str(prow.get("field") or "") == field:
            return _text(prow.get("after"), 200)
    return ""


def _person(output: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    subject = _dict(output.get("subject"))
    for key in ("label", "full_name", "name"):
        name = _text(subject.get(key), 120)
        if name:
            return name
    return _preview_row(row, "Person") or "this person"


def _view(spec: ToolSpec, args: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    view = str(args.get("view") or output.get("view") or "")
    if not view and spec.name == "estimator_timeline":
        view = "estimator_timeline"
    return view


def _window(args: Mapping[str, Any], output: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """"<from> to <to> Pacific", or "" when nothing names the window."""
    window = _dict(output.get("window"))
    since = window.get("since") or window.get("from") or output.get("since") or args.get("since")
    until = window.get("until") or window.get("to") or output.get("until") or args.get("until")
    if since or until:
        return f"{_text(since, 40) or 'the start'} to {_text(until, 40) or 'today'} Pacific"
    text = _preview_row(row, "Window")
    return text.replace("(Pacific)", "Pacific").strip() if text else ""


def _when(event: Mapping[str, Any]) -> str:
    return _text(event.get("at_pt"), 40) or pt(event.get("at")) or _text(event.get("day"), 40) or "unknown time"


def _kind(event: Mapping[str, Any]) -> str:
    return _text(event.get("kind") or event.get("source") or event.get("feature"), 60) or "event"


def _summary(event: Mapping[str, Any]) -> str:
    for key in ("summary", "message", "subject", "title", "action"):
        text = _text(event.get(key), SUMMARY_MAX_CHARS)
        if text:
            return text
    return ""


def _project(event: Mapping[str, Any]) -> str:
    project = event.get("project")
    if isinstance(project, Mapping):
        return _text(project.get("number"), 40)
    return _text(event.get("project_number"), 40)


def _read_at(event: Mapping[str, Any]) -> str:
    return _text(event.get("read_at_pt"), 40) or pt(event.get("read_at")) or ""


_WHEN = ("When (Pacific)", _when)
_KIND = ("Kind", _kind)
_SUMMARY = ("Summary", _summary)
_PROJECT = ("Project", _project)
_SOURCE = ("Source", lambda e: _text(e.get("source"), 60))

VIEW_COLUMNS: dict[str, tuple] = {
    "activity": (_WHEN, _SOURCE, _KIND, _PROJECT, _SUMMARY),
    "file_access": (_WHEN, _KIND, _PROJECT, _SUMMARY),
    "notifications": (_WHEN, _KIND, _PROJECT, _SUMMARY, ("Read (Pacific)", _read_at)),
    "ai_usage": (_WHEN, _KIND, _SUMMARY),
    "estimator_timeline": (_WHEN, _KIND, _SUMMARY),
}
DEFAULT_COLUMNS = (_WHEN, _KIND, _SUMMARY)


# ── Content ──────────────────────────────────────────────────────────────────


def _counts_lines(output: Mapping[str, Any], total: int, truncated: bool) -> list[str]:
    lines = [f"{total} row{'s' if total != 1 else ''}."]
    if truncated:
        lines.append("Some sources hit their cap, so the real count may be higher.")
    assistant = _dict(output.get("assistant_counts"))
    parts = [(str(k), int(v)) for k, v in sorted(assistant.items())
             if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    if parts:
        listed = ", ".join(f"{_text(k, 40)} {n}" for k, n in parts)
        lines.append(f"Rows their assistant wrote, not counted as theirs: {sum(n for _k, n in parts)} ({listed}).")
    summary = _dict(output.get("summary"))
    facts = []
    for key, label in ESTIMATOR_SUMMARY_FIELDS:
        if key in summary:
            facts.append(f"{label}: {pt(summary.get(key)) or _text(summary.get(key), 40) or 'not yet'}")
    if "email_status" in summary and summary.get("email_status"):
        facts.append(f"Email status: {_text(summary.get('email_status'), 40)}")
    for entry in summary.get("rounds") or ():
        if isinstance(entry, Mapping):
            facts.append(f"Round {_text(entry.get('round'), 10) or '?'} submitted: "
                         f"{pt(entry.get('submitted_at')) or 'not yet'}")
    if facts:
        lines.append(". ".join(facts) + ".")
    return lines


def build_content(header: str, count_lines: list[str], events: list[dict], total: int,
                  *, too_large: bool = False) -> tuple[str, int]:
    """(content, events shown). At most CONTENT_MAX_CHARS characters and
    MAX_EVENT_LINES event lines; "Showing N of M." closes a cut list."""
    head = "\n".join([header, *count_lines])
    if too_large:
        return (head + "\n" + TOO_LARGE_TEXT)[:CONTENT_MAX_CHARS], 0
    if not events:
        return (head + "\n" + NO_ROWS_TEXT)[:CONTENT_MAX_CHARS], 0
    total = max(total, len(events))
    lines: list[str] = []
    used = len(head)
    for event in events[:MAX_EVENT_LINES]:
        line = f"{_when(event)} | {_kind(event)} | {_summary(event)}".rstrip(" |")
        tail = len(f"\nShowing {len(lines) + 1} of {total}.")
        if used + 1 + len(line) + tail > CONTENT_MAX_CHARS:
            break
        lines.append(line)
        used += 1 + len(line)
    content = "\n".join([head, *lines])
    if len(lines) < total:
        content += f"\nShowing {len(lines)} of {total}."
    return content[:CONTENT_MAX_CHARS], len(lines)


def _table(view: str, events: list[dict]) -> dict | None:
    if not events:
        return None
    columns = VIEW_COLUMNS.get(view, DEFAULT_COLUMNS)
    return {
        "type": "table",
        "columns": [label for label, _get in columns],
        "rows": [[_text(get(e), CELL_MAX_CHARS) for _label, get in columns] for e in events[:MAX_TABLE_ROWS]],
    }


def _report_id(payload: Any, output: Any, executor: Executor) -> str | None:
    """The workbook a person report built: the route's own full id first,
    else the trimmed output's short id expanded."""
    for source in (payload, output):
        data = _dict(source)
        for key in ("xlsx_report_id", "report_id"):
            value = data.get(key)
            if isinstance(value, str):
                full = executor.ids.get(value, value)
                if is_uuid(full):
                    return full
        report = _dict(data.get("report"))
        value = report.get("id") or report.get("report_id")
        if isinstance(value, str):
            full = executor.ids.get(value, value)
            if is_uuid(full):
                return full
    return None


def _citations(executor: Executor) -> list[dict]:
    out = []
    for number in sorted(executor.projects)[:MAX_CITATIONS]:
        pid = executor.projects[number]
        out.append({"type": "citation", "label": number, "project_id": pid, "path": f"/projects/{pid}"})
    return out


def _total(spec: ToolSpec, payload: Any, output: Mapping[str, Any], events: list[dict]) -> tuple[int, bool]:
    """(row count, truncated) from the spec's counts summary, else the output."""
    summary: dict = {}
    if spec.write is not None and spec.write.result_summary is not None:
        try:
            summary = _dict(spec.write.result_summary(payload))
        except Exception:  # noqa: BLE001 - the counts fall back to the output
            logger.exception("assistant evidence: result_summary failed for %s", spec.name)
    count = summary.get("row_count")
    if not isinstance(count, int) or isinstance(count, bool):
        count = output.get("row_count") if isinstance(output.get("row_count"), int) else len(events)
    truncated = bool(summary.get("truncated") or output.get("truncated"))
    return max(int(count), len(events)), truncated


# ── Entry point ──────────────────────────────────────────────────────────────


def post_result(user_id: str, row: dict, spec: ToolSpec, payload: Any, executor: Executor) -> None:
    """Post one evidence message for an executed sensitive_read approval.

    `row` is the approval row, `payload` the route's response
    (`result.payload`). Runs `spec.response_trimmer` with
    `response_format="detailed"` through `Executor.shape` (16,000 character
    cap; the trimmer gets `[payload]`), then builds `content`: a header line
    ("Evidence for <name>, <view>, <from> to <to> Pacific. Reason on file."),
    counts, up to 40 event lines "<at PT> | <kind> | <summary>" clipped so
    `content` stays at or under 3,800 characters, and "Showing N of M." when
    cut. Blocks: one `text`, one `table` (first 25 rows), `citation` blocks
    for project ids, `report_file` when an xlsx was asked.

    Message id `uuid5(NAMESPACE, "evidence:" + approval_id)` (a replay
    inserts nothing); written with `server_data=true` and `tainted=True`
    whenever the output carried a taint field. Called by
    `approvals_job._outcome` after the ledger row is finished and before the
    card is marked `executed` (so a client reloading on that status finds the
    message); a failure is logged by the caller and the card still ends
    executed.
    """
    conversation_id = row.get("conversation_id")
    if not conversation_id:
        return
    approval_id = str(row["id"])
    message_id = message_id_for(approval_id)
    if repo.get_message(user_id, message_id) is not None:
        return
    args = _dict(_dict(row.get("payload")).get("args"))
    shape_args = {**args, "response_format": "detailed", "limit": DELIVERY_LIMIT}
    shaped, tainted = executor.shape(spec, [payload], shape_args)
    output = _dict(shaped)
    too_large = bool(output.get("truncated")) and "partial" in output and not _rows(output)
    events = [] if too_large else _rows(output)
    total, truncated = _total(spec, payload, output, events)

    view = _view(spec, args, output)
    window = _window(args, output, row)
    header = f"Evidence for {_person(output, row)}, {VIEW_LABELS.get(view, view.replace('_', ' ') or 'evidence')}"
    header += f", {window}. Reason on file." if window else ". Reason on file."
    content, _shown = build_content(header, _counts_lines(output, total, truncated), events, total,
                                    too_large=too_large)

    blocks: list[dict] = [{"type": "text", "text": content}]
    table = _table(view, events)
    if table is not None:
        blocks.append(table)
    blocks.extend(_citations(executor))
    if args.get("xlsx"):
        report_id = _report_id(payload, shaped, executor)
        if report_id:
            blocks.append({"type": "report_file", "report_id": report_id})

    now = datetime.now(timezone.utc).isoformat()
    message = {
        "id": message_id,
        "conversation_id": str(conversation_id),
        "role": "assistant",
        "status": "done",
        "content": content,
        "blocks": blocks,
        "completed_at": now,
        "tainted": bool(tainted),
        "server_data": True,
    }
    try:
        inserted = repo.insert_message(user_id, message)
    except Exception:
        # A 23505 on the primary key: an earlier attempt posted it.
        inserted = repo.get_message(user_id, message_id)
        if inserted is None:
            raise
    try:
        repo.update_conversation(user_id, str(conversation_id), {
            "last_message_at": str((inserted or {}).get("created_at") or now),
        })
    except Exception:  # noqa: BLE001 - the sort order is a convenience
        logger.warning("assistant evidence: conversation touch failed for %s", conversation_id)
