"""Replay the stored tool-loop rounds of one AI Estimating group at $0
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS2 step 13; 4.3; 8.2).

A tool loop stores every round in `plans_reader_loop_rounds` and every
tool call (arguments, result text, typed result, citations, view metadata)
in `plans_reader_tool_calls` (migration 0165). This script takes those rows
for one group (or one chat message), re-runs every READ tool against the
stored documents with today's code and services, and diffs each new
result against the stored one. No model is called: the model's choices
are taken from the stored rounds, so a replay costs $0 and tells whether a
tool, a service or a document changed what the model was shown.

Usage (from bdr_be):

    grep '^SUPABASE_URL' .env    # must show the dev ref bpidntbyvoooqvaispup
    # From the dev database (sync client; never prod, never staging):
    uv run python scripts/plans_reader_loop_replay.py db --group <group id> [--out report.json] [--strict]
    uv run python scripts/plans_reader_loop_replay.py db --message <chat message id>
    # Copy a run's loop rows into a bundle (loops/rounds.json, loops/tool_calls.json):
    uv run python scripts/plans_reader_loop_replay.py export --run <run id> --bundle <bundle dir>
    # From a bundle (source PDFs from the bundle or the production backup on disk):
    uv run python scripts/plans_reader_loop_replay.py bundle --bundle <bundle dir> --group <group id>

Outcomes per stored tool call:

- `match`: the re-run gives the same result text, typed result, citations,
  refusal state and view metadata.
- `mismatch`: something differs; the report names the fields and carries a
  short unified diff of the result text (pricing tools: fields only, no
  text, unless --show-pricing, because their text holds library prices).
- `skipped_write`: a write or review tool; never re-run (it would change
  state), it still counts toward its per-group call limit as it did live.
- `loop_refusal`: refused by the loop before dispatch (stray final-round
  call, unknown tool, interrupted write, per-round or per-loop call or image
  budget); not re-run and not counted, as the loop does on resume.
- `pending`: the stored row never got its result (a crash); not re-run.
- `unknown_tool`: the tool is no longer registered.
- `unavailable_now`: the re-run answered "service unavailable" while the
  stored call did not (offline replay without that service); not a diff.
- `missing_row`: a done round asked for a call that has no stored row.

Exit status: 0, or 1 with --strict when any call mismatches; 2 on a usage or
preflight error. Reports name dev runs only and hold no real-project
dollars beyond the dev round costs; write them under EVAL_ROOT/replays.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import plans_reader_tools as tools  # noqa: E402
from app.services.plans_reader_loop_store import PRECALL_ROUND, is_pending, key_columns, loop_key  # noqa: E402

REPORT_SCHEMA = "bdr.eval.loop_replay/1"
DEV_REF = "bpidntbyvoooqvaispup"
LOOPS_DIR = "loops"
ROUNDS_FILE = "rounds.json"
TOOL_CALLS_FILE = "tool_calls.json"

OUTCOMES: tuple[str, ...] = (
    "match",
    "mismatch",
    "skipped_write",
    "loop_refusal",
    "pending",
    "unknown_tool",
    "unavailable_now",
    "missing_row",
)
# Refusals the loop gives before dispatch (plans_reader_toolloop._serve and
# _dispatch_once); dispatch's own max_calls refusal carries calls_made in its
# detail, the loop's carries scope "loop".
LOOP_REFUSAL_CODES: frozenset[str] = frozenset({"final_round", "unknown_tool", "interrupted", "max_images"})
IMAGE_CAP_MARKER = "views not delivered: the image budget"
UNAVAILABLE_ERRORS: frozenset[str] = frozenset({"unavailable", "facts_unavailable"})
TEXT_DIFF_LINES = 24
COMPARED_FIELDS: tuple[str, ...] = ("result_text", "result_data", "citations", "refused", "views")


class ReplayError(RuntimeError):
    """The replay cannot run as asked (usage, preflight, missing rows)."""


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass
class LoopRows:
    """The stored rows of one loop key."""

    key: str
    run_id: str
    kind: str
    rounds: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def provider(self) -> str | None:
        for row in self.rounds:
            raw = row.get("assistant_raw")
            if isinstance(raw, Mapping) and raw.get("provider"):
                return str(raw["provider"])
        return None


def _key_matches(row: Mapping[str, Any], key: str) -> bool:
    cols = key_columns(key)
    return all(str(row.get(col) or "") == str(val) for col, val in cols.items())


def _sort_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(r) for r in rows),
        key=lambda r: (int(r.get("round_no") or 0), str(r.get("created_at") or ""), str(r.get("call_id") or "")),
    )


def rows_for_key(
    rounds: Iterable[Mapping[str, Any]],
    tool_calls: Iterable[Mapping[str, Any]],
    *,
    key: str,
    run_id: str = "",
    kind: str = "",
) -> LoopRows:
    """The rows of `key` out of a run's rows (bundle files hold every key)."""
    return LoopRows(
        key=key,
        run_id=run_id,
        kind=kind,
        rounds=_sort_rows(r for r in rounds if _key_matches(r, key)),
        tool_calls=_sort_rows(c for c in tool_calls if _key_matches(c, key)),
    )


def rows_from_store(store: Any, *, key: str, run_id: str = "", kind: str = "") -> LoopRows:
    """Rows through a loop store (SupabaseLoopStore or FakeLoopStore)."""
    return LoopRows(key=key, run_id=run_id, kind=kind, rounds=_sort_rows(store.rounds(key)),
                    tool_calls=_sort_rows(store.tool_calls(key)))


def bundle_loop_paths(bundle_root: Path | str) -> tuple[Path, Path]:
    root = Path(bundle_root) / LOOPS_DIR
    return root / ROUNDS_FILE, root / TOOL_CALLS_FILE


def rows_from_bundle(bundle_root: Path | str, *, key: str, run_id: str = "", kind: str = "") -> LoopRows:
    rounds_path, calls_path = bundle_loop_paths(bundle_root)
    if not rounds_path.is_file() or not calls_path.is_file():
        raise ReplayError(
            f"bundle {bundle_root} has no {LOOPS_DIR}/{ROUNDS_FILE} and {LOOPS_DIR}/{TOOL_CALLS_FILE}; "
            "copy them from the dev run first with the export command"
        )
    rounds = json.loads(rounds_path.read_text())
    calls = json.loads(calls_path.read_text())
    return rows_for_key(rounds, calls, key=key, run_id=run_id, kind=kind)


def _strip_image_bytes(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip_image_bytes(v) for k, v in value.items() if k not in ("image", "png", "b64")}
    if isinstance(value, list):
        return [_strip_image_bytes(v) for v in value]
    return value


def export_loop_rows(sb: Any, run_id: str, bundle_root: Path | str) -> tuple[Path, Path, int, int]:
    """Copy one dev run's loop rounds and tool calls into
    `<bundle>/loops/` (sync client; call from a script, never inside async
    code). Returns (rounds path, tool calls path, n rounds, n calls)."""
    rounds_path, calls_path = bundle_loop_paths(bundle_root)
    rounds_path.parent.mkdir(parents=True, exist_ok=True)

    def all_rows(table: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            chunk = (
                sb.table(table).select("*").eq("run_id", str(run_id)).order("created_at")
                .range(start, start + 999).execute().data or []
            )
            out.extend(chunk)
            if len(chunk) < 1000:
                return out
            start += 1000

    rounds = _strip_image_bytes(all_rows("plans_reader_loop_rounds"))
    calls = _strip_image_bytes(all_rows("plans_reader_tool_calls"))
    rounds_path.write_text(json.dumps(rounds, indent=1, default=str))
    calls_path.write_text(json.dumps(calls, indent=1, default=str))
    return rounds_path, calls_path, len(rounds), len(calls)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    """JSON-normalized value (what the database hands back)."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _detail(row: Mapping[str, Any]) -> dict[str, Any]:
    data = row.get("result_data")
    if isinstance(data, Mapping) and isinstance(data.get("detail"), Mapping):
        return dict(data["detail"])
    return {}


def _code(row: Mapping[str, Any]) -> str | None:
    data = row.get("result_data")
    if isinstance(data, Mapping) and data.get("code"):
        return str(data["code"])
    return None


def is_loop_refusal(row: Mapping[str, Any]) -> bool:
    """A stored call the loop refused before dispatch (never re-run)."""
    if not row.get("refused"):
        return False
    code = _code(row)
    if code in LOOP_REFUSAL_CODES:
        return True
    return code == "max_calls" and _detail(row).get("scope") == "loop"


def _is_unavailable(data: Any) -> bool:
    return isinstance(data, Mapping) and str(data.get("error") or "") in UNAVAILABLE_ERRORS


def _cap_views(result: tools.ToolResult, stored: Mapping[str, Any]) -> tools.ToolResult:
    """Apply the loop's image cap again when the stored text says some views
    were not delivered (plans_reader_toolloop._Loop._cap_images)."""
    text = str(stored.get("result_text") or "")
    if IMAGE_CAP_MARKER not in text:
        return result
    keep = len(stored.get("views") or [])
    total = len(result.views)
    if total <= keep:
        return result
    result.views = result.views[:keep]
    result.text = (
        f"{result.text}\n[{total - keep} of {total} views not delivered: the image budget of this step is used up]"
    )
    return result


def _replayed_record(result: tools.ToolResult) -> dict[str, Any]:
    rec = result.to_record()
    return {
        "result_text": rec["result_text"],
        "result_data": rec["result_data"],
        "citations": rec["citations"],
        "refused": rec["refused"],
        "views": rec["views"],
    }


def diff_fields(stored: Mapping[str, Any], replayed: Mapping[str, Any]) -> list[str]:
    """Names of the compared fields that differ (views compare by their
    stored metadata keys; the store keeps no image bytes)."""
    out: list[str] = []
    for name in COMPARED_FIELDS:
        a, b = stored.get(name), replayed.get(name)
        if name == "refused":
            if bool(a) != bool(b):
                out.append(name)
            continue
        if name == "citations":
            a, b = list(a or []), list(b or [])
        if name == "views":
            a = [dict(v) for v in (a or []) if isinstance(v, Mapping)]
            b = [{k: v.get(k) for k in a[i]} if i < len(a) else dict(v) for i, v in enumerate(b or []) if isinstance(v, Mapping)]
        if name == "result_text":
            a, b = str(a or ""), str(b or "")
        if _norm(a) != _norm(b):
            out.append(name)
    return out


def text_diff(stored: str, replayed: str, *, limit: int = TEXT_DIFF_LINES) -> list[str]:
    lines = list(
        difflib.unified_diff(
            stored.splitlines(), replayed.splitlines(), fromfile="stored", tofile="replayed", lineterm="", n=1
        )
    )
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more diff lines"]
    return lines


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _image_spec(provider: str | None) -> Any:
    from app.services import llm_parts

    try:
        return llm_parts.image_spec_for(provider or "openai")
    except ValueError:
        return llm_parts.OPENAI_IMAGES


def replay_rows(
    rows: LoopRows,
    *,
    services: Any,
    sb: Any = None,
    provider: str | None = None,
    registry: Mapping[str, tools.ToolSpec] | None = None,
    show_pricing: bool = False,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Re-run the stored tool calls of one loop in their stored order
    (pre-call stage first) and diff each against its stored row. Returns
    the report (schema bdr.eval.loop_replay/1)."""
    reg = registry if registry is not None else tools.REGISTRY
    cols = key_columns(rows.key)
    ctx = tools.ToolContext(
        run_id=rows.run_id,
        kind=rows.kind or ("chat" if "message_id" in cols else "recount"),
        sb=sb,
        group_id=cols.get("group_id"),
        message_id=cols.get("message_id"),
        services=services,
        heartbeat=heartbeat or (lambda: None),
        image_spec=_image_spec(provider or rows.provider),
    )
    calls_out: list[dict[str, Any]] = []
    stored_ids = {str(c.get("call_id")) for c in rows.tool_calls}
    for stored in rows.tool_calls:
        call_id = str(stored.get("call_id") or "")
        name = str(stored.get("tool_name") or "")
        args = stored.get("args") if isinstance(stored.get("args"), Mapping) else {}
        entry: dict[str, Any] = {"round_no": int(stored.get("round_no") or 0), "call_id": call_id, "tool": name}
        spec = reg.get(name)
        if is_pending(stored):
            entry["outcome"] = "pending"
        elif is_loop_refusal(stored):
            entry["outcome"] = "loop_refusal"
            entry["code"] = _code(stored)
        elif spec is None:
            entry["outcome"] = "unknown_tool"
        elif spec.mode != "read":
            entry["outcome"] = "skipped_write"
            if not stored.get("refused") or _code(stored) not in ("invalid_args", "max_calls"):
                ctx.call_counts[name] = ctx.calls_made(name) + 1
        else:
            ctx.call_id = call_id
            try:
                result = tools.dispatch(spec, ctx, dict(args))
            finally:
                ctx.call_id = None
            result = _cap_views(result, stored)
            replayed = _replayed_record(result)
            fields = diff_fields(stored, replayed)
            if fields and _is_unavailable(replayed["result_data"]) and not _is_unavailable(stored.get("result_data")):
                entry["outcome"] = "unavailable_now"
                entry["service"] = (replayed["result_data"] or {}).get("service") or (replayed["result_data"] or {}).get("error")
            elif fields:
                entry["outcome"] = "mismatch"
                entry["fields"] = fields
                if spec.internal_pricing and not show_pricing:
                    entry["text_diff"] = ["[pricing tool: text diff withheld; rerun with --show-pricing under EVAL_ROOT]"]
                elif "result_text" in fields:
                    entry["text_diff"] = text_diff(str(stored.get("result_text") or ""), str(replayed["result_text"] or ""))
            else:
                entry["outcome"] = "match"
        calls_out.append(entry)

    rounds_out: list[dict[str, Any]] = []
    for row in rows.rounds:
        asked = [c for c in (row.get("tool_calls") or []) if isinstance(c, Mapping)]
        missing = [str(c.get("call_id")) for c in asked if str(c.get("call_id")) not in stored_ids]
        info = {
            "round_no": int(row.get("round_no") or 0),
            "status": row.get("status"),
            "tool_calls": len(asked),
            "cost_usd": float(Decimal(str(row.get("cost_usd") or 0))),
        }
        raw = row.get("assistant_raw")
        if isinstance(raw, Mapping):
            info["final"] = bool(raw.get("final"))
            info["answered"] = raw.get("parsed") is not None
            if raw.get("stop_reason"):
                info["stop_reason"] = raw.get("stop_reason")
        if missing and row.get("status") == "done":
            info["missing_call_ids"] = missing
            for cid in missing:
                calls_out.append({"round_no": info["round_no"], "call_id": cid, "tool": next(
                    (str(c.get("name")) for c in asked if str(c.get("call_id")) == cid), ""), "outcome": "missing_row"})
        rounds_out.append(info)

    counts = {name: 0 for name in OUTCOMES}
    for entry in calls_out:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    rerun = counts["match"] + counts["mismatch"]
    return {
        "schema": REPORT_SCHEMA,
        "key": rows.key,
        "run_id": rows.run_id,
        "kind": ctx.kind,
        "provider": provider or rows.provider,
        "precall_calls": sum(1 for c in rows.tool_calls if int(c.get("round_no") or 0) == PRECALL_ROUND),
        "rounds": rounds_out,
        "calls": calls_out,
        "counts": counts,
        "rerun": rerun,
        "match_ratio": round(counts["match"] / rerun, 4) if rerun else None,
        "spent_usd": round(sum(r["cost_usd"] for r in rounds_out), 6),
        "model_calls_made": 0,
    }


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    counts = report.get("counts") or {}
    lines = [
        f"loop {report.get('key')} (run {str(report.get('run_id') or '')[:8]}, kind {report.get('kind')}, "
        f"provider {report.get('provider') or 'unknown'})",
        f"rounds {len(report.get('rounds') or [])}, stored calls {sum(counts.values())}, re-run {report.get('rerun')}, "
        f"match ratio {report.get('match_ratio')}",
        "outcomes: " + ", ".join(f"{k} {counts.get(k, 0)}" for k in OUTCOMES),
    ]
    for entry in report.get("calls") or []:
        if entry.get("outcome") == "mismatch":
            lines.append(f"  MISMATCH round {entry['round_no']} {entry['call_id']} {entry['tool']}: {', '.join(entry.get('fields') or [])}")
            lines.extend(f"    {line}" for line in entry.get("text_diff") or [])
        elif entry.get("outcome") == "missing_row":
            lines.append(f"  MISSING round {entry['round_no']} {entry['call_id']} {entry['tool']}")
    return lines


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def db_services(sb: Any, run_id: str) -> Any:
    """The run's ToolServices as a live loop builds them
    (plans_reader_runs._run_services): document access over the run's rows
    plus the WS1 adapters bound to the run."""
    from app.services import plans_reader_evidence_inputs as evidence_inputs
    from app.services import plans_reader_tool_handlers as tool_handlers

    rows = sb.table("plans_reader_documents").select("*").eq("run_id", str(run_id)).execute().data or []
    services = tool_handlers.services_for_run(sb, run_id, inputs=evidence_inputs.inputs_for(sb), document_rows=rows)
    try:
        from app.services.pagefacts import services as pagefacts_services

        services = pagefacts_services.bind_services(services, sb, run_id, documents=rows)
    except Exception as exc:  # noqa: BLE001 - the unbound registry still answers
        print(f"note: page facts services not bound ({exc})", file=sys.stderr)
    return services


def _referenced_documents(calls: Sequence[Mapping[str, Any]]) -> tuple[set[str], bool]:
    ids: set[str] = set()
    needs_all = False
    for c in calls:
        args = c.get("args") if isinstance(c.get("args"), Mapping) else {}
        doc = args.get("document_id") if isinstance(args, Mapping) else None
        if doc:
            ids.add(str(doc))
        else:
            needs_all = True
    return ids, needs_all


def bundle_services(bundle: Any, calls: Sequence[Mapping[str, Any]], *, backup_dir: Path | str | None = None) -> tuple[Any, list[str]]:
    """Offline ToolServices for a bundle: the documents' PDFs (from the
    bundle or the production backup on disk), a words text index over them
    and the process registry for the rest. Returns (services, notes)."""
    from app.services import eval_bundle
    from app.services import plans_reader_evidence_inputs as evidence_inputs
    from app.services import plans_reader_tool_handlers as tool_handlers

    ids, needs_all = _referenced_documents(calls)
    wanted = [str(d.get("id")) for d in bundle.documents] if needs_all else sorted(ids)
    pdfs: dict[str, bytes] = {}
    notes: list[str] = []
    for doc_id in wanted:
        try:
            pdfs[doc_id] = bundle.doc_bytes(doc_id, backup_dir=backup_dir or eval_bundle.DEFAULT_BACKUP_DIR)
        except Exception as exc:  # noqa: BLE001 - a missing PDF is reported, the rest replays
            notes.append(f"document {doc_id[:8]}: {exc}")
    names = {str(d.get("id")): str(d.get("name") or "") for d in bundle.documents}
    documents = tool_handlers.LocalDocuments(pdfs, names=names)
    inputs = evidence_inputs.inputs_for(pdfs=pdfs) if pdfs else None
    services = tool_handlers.services_for_run(None, bundle.run_id, documents=documents, inputs=inputs)
    return services, notes


def register_registry_tools() -> None:
    """Import the modules that register tools (the same set app/main.py
    imports at startup, plus the pricing tools)."""
    import importlib

    for name in (
        "app.services.plans_reader_tool_handlers",
        "app.services.plans_reader_review_tools",
        "app.services.scope_tools",
        "app.services.pricing_tools",
        "app.services.plans_reader_measure",
        "app.services.pagefacts.services",
    ):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - a missing module leaves its tools unknown
            print(f"note: {name} not imported ({exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _check_dev(settings: Any) -> None:
    url = str(getattr(settings, "supabase_url", "") or "")
    if DEV_REF not in url:
        raise ReplayError(f"SUPABASE_URL must point at the dev project {DEV_REF}; refusing (got {url or 'empty'})")


def _dev_client() -> Any:
    from app.core.config import get_settings
    from app.core.supabase_client import get_supabase

    _check_dev(get_settings())
    return get_supabase()


def _key_args(args: argparse.Namespace) -> str:
    if bool(args.group) == bool(args.message):
        raise ReplayError("name exactly one of --group or --message")
    return loop_key(group_id=args.group, message_id=args.message)


def _group_meta(sb: Any, args: argparse.Namespace) -> tuple[str, str]:
    """(run_id, kind) of the loop's owner row."""
    if args.group:
        rows = sb.table("plans_reader_groups").select("id, run_id, kind").eq("id", args.group).execute().data or []
        if not rows:
            raise ReplayError(f"group {args.group} not found")
        return str(rows[0]["run_id"]), str(rows[0].get("kind") or "")
    rows = sb.table("plans_reader_chat_messages").select("id, thread_id").eq("id", args.message).execute().data or []
    if not rows:
        raise ReplayError(f"message {args.message} not found")
    threads = sb.table("plans_reader_chat_threads").select("id, run_id").eq("id", rows[0]["thread_id"]).execute().data or []
    if not threads:
        raise ReplayError(f"the thread of message {args.message} is gone")
    return str(threads[0]["run_id"]), "chat"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Replay stored AI Estimating tool-loop rounds at $0 and diff tool results.")
    sub = p.add_subparsers(dest="command", required=True)
    for name, help_text in (("db", "replay from the dev database"), ("bundle", "replay from a bundle directory")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--group", default=None, help="plans_reader_groups id of the loop")
        sp.add_argument("--message", default=None, help="chat message id of the loop")
        sp.add_argument("--kind", default=None, help="group kind (default: read from the group row)")
        sp.add_argument("--provider", default=None, help="provider the loop sent images to (default: from the rounds)")
        sp.add_argument("--out", type=Path, default=None, help="write the JSON report here")
        sp.add_argument("--strict", action="store_true", help="exit 1 when any call mismatches")
        sp.add_argument("--show-pricing", action="store_true", help="include pricing tool text diffs (EVAL_ROOT only)")
        if name == "bundle":
            sp.add_argument("--bundle", type=Path, required=True)
            sp.add_argument("--backup", type=Path, default=None, help="production backup dir (default: eval_bundle's)")
    ex = sub.add_parser("export", help="copy a dev run's loop rows into <bundle>/loops/")
    ex.add_argument("--run", required=True)
    ex.add_argument("--bundle", type=Path, required=True)
    return p


def _emit(report: dict[str, Any], args: argparse.Namespace, notes: Sequence[str] = ()) -> int:
    if notes:
        report["notes"] = list(notes)
    for line in summary_lines(report):
        print(line)
    for note in notes:
        print(f"note: {note}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, default=str))
        print(f"report: {args.out}")
    return 1 if args.strict and report["counts"].get("mismatch") else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            sb = _dev_client()
            rounds_path, calls_path, n_rounds, n_calls = export_loop_rows(sb, args.run, args.bundle)
            print(f"exported {n_rounds} rounds to {rounds_path} and {n_calls} tool calls to {calls_path}")
            return 0
        key = _key_args(args)
        register_registry_tools()
        if args.command == "db":
            sb = _dev_client()
            run_id, kind = _group_meta(sb, args)
            from app.services.plans_reader_loop_store import SupabaseLoopStore

            rows = rows_from_store(SupabaseLoopStore(sb, run_id=run_id), key=key, run_id=run_id, kind=args.kind or kind)
            if not rows.rounds and not rows.tool_calls:
                raise ReplayError(f"no stored rounds or tool calls for {key}")
            report = replay_rows(rows, services=db_services(sb, run_id), sb=sb, provider=args.provider,
                                 show_pricing=args.show_pricing)
            return _emit(report, args)
        from app.services import eval_bundle

        bundle = eval_bundle.load_bundle(args.bundle)
        kind = args.kind
        if kind is None and args.group:
            kind = next((str(g.get("kind")) for g in bundle.groups if str(g.get("id")) == args.group), "")
        rows = rows_from_bundle(args.bundle, key=key, run_id=bundle.run_id, kind=kind or ("chat" if args.message else ""))
        if not rows.rounds and not rows.tool_calls:
            raise ReplayError(f"no stored rounds or tool calls for {key} in {args.bundle}")
        services, notes = bundle_services(bundle, rows.tool_calls, backup_dir=args.backup)
        report = replay_rows(rows, services=services, provider=args.provider, show_pricing=args.show_pricing)
        return _emit(report, args, notes)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "LoopRows",
    "OUTCOMES",
    "REPORT_SCHEMA",
    "ReplayError",
    "build_parser",
    "bundle_loop_paths",
    "bundle_services",
    "db_services",
    "diff_fields",
    "export_loop_rows",
    "is_loop_refusal",
    "main",
    "replay_rows",
    "rows_for_key",
    "rows_from_bundle",
    "rows_from_store",
    "summary_lines",
    "text_diff",
]


if __name__ == "__main__":
    raise SystemExit(main())
