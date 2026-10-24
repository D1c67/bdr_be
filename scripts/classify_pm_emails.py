"""Backfill the AI classification of emails filed to PM projects
(docs/EMAIL_CLASSIFICATION.md).

Walks every email on a project in Project Management (or one --project-id)
in message order, so each thread is classified oldest first and every
message gets the thread summary of the one before it. Each email runs the
same service function the queue job runs (email_classification.execute),
synchronously, on the self-hosted box chosen in Settings > AI model server.
Nothing is enqueued.

Already classified emails are skipped unless --force. --dry-run prints the
exact prompt for the first email that would be classified and stops (no
model call, no writes). Results print as a table and, with --out, are
written as JSON.

Usage:
    cd bdr_be
    uv run python scripts/classify_pm_emails.py [--project-id <uuid>]
        [--limit N] [--force] [--dry-run] [--out results.json]
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Allow running as a plain script: put the project root (bdr_be) on the import
# path so `app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.core.supabase_client import get_supabase  # noqa: E402
from app.services import email_classification as ec  # noqa: E402
from app.services import llm, llm_gate  # noqa: E402

_PAGE = 1000


def _pm_projects(sb, project_id: str | None) -> dict[str, dict]:
    q = sb.table("projects").select("id, number, name, pm_stage").not_.is_("pm_stage", "null")
    if project_id:
        q = q.eq("id", project_id)
    return {p["id"]: p for p in (q.execute().data or [])}


def _emails(sb, project_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for start in range(0, len(project_ids), 50):
        chunk = project_ids[start : start + 50]
        offset = 0
        while True:
            page = (
                sb.table("ingested_emails")
                .select("id, project_id, conversation_id, status, message_at, created_at, from_address, subject")
                .in_("project_id", chunk)
                .order("message_at", desc=False)
                .range(offset, offset + _PAGE - 1)
                .execute()
            ).data or []
            out.extend(page)
            if len(page) < _PAGE:
                break
            offset += _PAGE
    out.sort(key=lambda e: (e.get("message_at") or "", e.get("created_at") or ""))
    return out


def _labels_text(labels: list[dict]) -> str:
    parts = []
    for lab in labels or []:
        name = lab.get("title") if lab.get("category") == ec.OTHER else lab.get("category")
        if lab.get("subcategory"):
            name = f"{name}/{lab['subcategory']}"
        parts.append(f"{name} {round(float(lab.get('confidence') or 0) * 100)}%")
    return "; ".join(parts)


def _clip(text, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 3] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-id", help="only this project (must be in PM)")
    ap.add_argument("--limit", type=int, default=0, help="stop after classifying this many emails")
    ap.add_argument("--force", action="store_true", help="re-run emails that are already classified")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt for one email and stop")
    ap.add_argument("--out", help="write the results as JSON to this path")
    args = ap.parse_args()

    s = get_settings()
    sb = get_supabase()
    print(f"Model: {llm.active_model(ec.FEATURE, s)} (configured: {llm.is_configured(ec.FEATURE, s)})")
    projects = _pm_projects(sb, args.project_id)
    if not projects:
        print("No PM projects matched.")
        return 1
    emails = _emails(sb, list(projects))
    done = ec.rows_for(sb, [e["id"] for e in emails])
    todo = [
        e for e in emails
        if args.force or (done.get(e["id"]) or {}).get("status") != ec.STATUS_DONE
    ]
    print(f"{len(emails)} emails on {len(projects)} PM projects; {len(todo)} to classify.")

    if args.dry_run:
        if not todo:
            print("Nothing to classify.")
            return 0
        system, messages = ec.prompt_for(todo[0]["id"], sb=sb, settings=s)
        print("=" * 30 + " SYSTEM " + "=" * 30)
        print(system)
        print("=" * 30 + " USER " + "=" * 32)
        print(messages[0]["content"])
        return 0

    results: list[dict] = []
    for e in todo:
        if args.limit and len(results) >= args.limit:
            break
        project = projects.get(e["project_id"]) or {}
        started = time.monotonic()
        entry = {
            "email_id": e["id"],
            "project_id": e["project_id"],
            "project": f"{project.get('number') or ''} {project.get('name') or ''}".strip(),
            "message_at": e.get("message_at"),
            "from": e.get("from_address"),
            "subject": e.get("subject"),
        }
        try:
            with llm_gate.tier(llm_gate.TIER_PIPELINE):
                row = ec.execute(e["id"], force=args.force)
            entry.update(
                {
                    "status": (row or {}).get("status", "skipped"),
                    "labels": (row or {}).get("ai_labels") or [],
                    "email_summary": (row or {}).get("email_summary"),
                    "thread_summary": (row or {}).get("thread_summary"),
                    "model": (row or {}).get("model"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one bad email must not stop the backfill
            entry.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:400], "labels": []})
        entry["seconds"] = round(time.monotonic() - started, 1)
        results.append(entry)
        print(
            f"{_clip(entry['project'], 28):28} | {str(entry['message_at'] or '')[:16]:16} | "
            f"{_clip(entry['from'], 26):26} | {_clip(entry['subject'], 40):40} | "
            f"{_clip(_labels_text(entry['labels']) or entry.get('error') or entry['status'], 50):50} | "
            f"{_clip(entry.get('email_summary'), 80)}",
            flush=True,
        )

    errors = sum(1 for r in results if r["status"] == "error")
    print(f"\nClassified {len(results) - errors}, errors {errors}.")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str))
        print(f"Wrote {args.out}")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
