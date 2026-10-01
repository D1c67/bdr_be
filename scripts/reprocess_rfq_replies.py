"""One-time recovery for vendor quotes the RFQ reply ingestion missed before
the 2026-09-29 fix.

Two kinds of reply slipped through:

1. Stored, but the quote PDF was never read. Graph lists a reply's inline
   signature images ahead of its real attachments, and they used to count
   against the per-reply attachment cap, so on a thread with some back and
   forth the quote PDF was dropped and the reply was left without a quote.
   Phase 1 finds replies from the last --days days that have a PDF attached
   in the mailbox which was never stored for them, and re-reads them under the
   current rules (rfq_inbox.refetch_reply_files, the same code as the Receive
   Quotes "Re-read files" button).

2. Never stored: the background poller only accepted the exact contact the
   RFQ went to, so a quote from a coworker at the vendor was dropped. Phase 2
   runs "Check for quotes now" (rfq_inbox.check_project_quotes) for every
   project with an RFQ sent in the last --check-days days, which re-reads each
   thread and stores the replies from the vendor's people. Off unless
   --check-days is given. Each new reply notifies the Materials engineers,
   as a click on the button would.

Dry run by default: both phases only read the mailbox and the database (no
writes, no AI calls). Phase 1 prints the replies it would re-read, and skips
any whose vendor already has a quote on that RFQ recorded after the reply
arrived (most likely typed in by hand). Phase 2 prints, per project, the
thread messages it would store and how their sender relates to the vendor.
--apply does the work; every recovered reply runs a paid PDF extraction.

Usage:
    cd bdr_be
    uv run python scripts/reprocess_rfq_replies.py [--days 60] [--limit N]
        [--check-days 21] [--project <id> ...] [--apply]
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a plain script: put the project root (bdr_be) on the import
# path so `app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services import graph_inbox, llm_gate, rfq_inbox
from app.services.graph_email import graph_request
from app.services.notifications import audit

_RETRY_STATUSES = ["skipped", "no_amount", "failed"]


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _unstored_pdfs(sb, row: dict) -> list[str]:
    """PDFs attached to the reply in the mailbox that were never stored for it."""
    listing = graph_request(
        "GET",
        f"/users/{get_settings().ms_sender}/messages/{row['graph_message_id']}/attachments",
        params={"$select": "id,name,contentType,size,isInline"},
    ).json().get("value", [])
    pdfs = [
        a.get("name") or "attachment"
        for a in listing
        if a.get("@odata.type") == "#microsoft.graph.fileAttachment"
        and not graph_inbox.is_inline_image(a)
        and rfq_inbox._is_pdf(a.get("name") or "", a.get("contentType"))
    ]
    # Stored for this reply, or stored before 0105 linked files to their reply
    # (unlinked rows in the same category). Another reply's linked file never
    # counts, so a second vendor's same-named "Quote.pdf" cannot hide a miss.
    rfq = (row.get("rfq_sends") or {}).get("rfqs") or {}
    files = (
        sb.table("project_files")
        .select("filename, rfq_message_id")
        .eq("project_id", rfq.get("project_id"))
        .eq("category", "quote")
        .eq("material_category_id", rfq.get("material_category_id"))
        .execute()
    ).data or []
    stored = {
        f["filename"] for f in files if f.get("rfq_message_id") in (row["id"], None)
    }
    return [name for name in pdfs if name not in stored]


def _entered_later(sb, send: dict, contact: dict, received_at: str | None) -> bool:
    """A quote for this vendor on this RFQ recorded at or after the reply
    arrived: most likely the PE typed this very reply in by hand, so an
    automatic re-read would only add a duplicate. An older quote is left
    alone (the missed reply is then a revision worth recovering)."""
    if not (send.get("rfq_id") and contact.get("vendor_id") and received_at):
        return False
    return bool(
        (
            sb.table("quotes")
            .select("id")
            .eq("rfq_id", send["rfq_id"])
            .eq("vendor_id", contact["vendor_id"])
            .gte("created_at", received_at)
            .limit(1)
            .execute()
        ).data
    )


def phase_replies(sb, days: int, apply: bool, projects: set[str], limit: int) -> None:
    rows = (
        sb.table("rfq_messages")
        .select(
            "id, from_addr, graph_message_id, received_at, extraction_status, "
            "rfq_sends(id, rfq_id, vendor_contacts(id, name, email, vendor_id), "
            "rfqs(project_id, material_category_id, material_categories(name), "
            "projects(name, number)))"
        )
        .eq("has_attachments", True)
        .in_("extraction_status", _RETRY_STATUSES)
        .gte("received_at", _since(days))
        .order("received_at")
        .execute()
    ).data or []
    ids = [r["id"] for r in rows]
    quoted = set()
    if ids:
        quoted = {
            q["rfq_message_id"]
            for q in (
                sb.table("quotes").select("rfq_message_id").in_("rfq_message_id", ids).execute()
            ).data or []
        }
    print(f"Phase 1: {len(rows)} replies with attachments and no quote in the last {days} days")

    reread = recovered = 0
    for row in rows:
        if row["id"] in quoted:
            continue
        send = row.get("rfq_sends") or {}
        contact = send.get("vendor_contacts") or {}
        rfq = send.get("rfqs") or {}
        if projects and rfq.get("project_id") not in projects:
            continue
        from_addr = row.get("from_addr") or ""
        # Our own mail stored as a "reply" by the old rules (bids@'s Sent Items
        # copy, a teammate on the thread): never re-read it as a vendor quote.
        if (
            not from_addr
            or rfq_inbox._is_own_mailbox(from_addr)
            or rfq_inbox._sender_relation(sb, contact, from_addr) == rfq_inbox.SENDER_INTERNAL
        ):
            continue
        try:
            missing = _unstored_pdfs(sb, row)
        except Exception as exc:  # noqa: BLE001 - one unreadable message must not stop the run
            print(f"  ! {row['id']}: could not list attachments ({exc})")
            continue
        if not missing:
            continue
        project = rfq.get("projects") or {}
        label = (
            f"{project.get('number') or ''} {project.get('name') or ''} / "
            f"{(rfq.get('material_categories') or {}).get('name')} / {from_addr} "
            f"({(row.get('received_at') or '')[:10]}, {row['extraction_status']})"
        )
        if _entered_later(sb, send, contact, row.get("received_at")):
            print(f"  skip, a quote was entered after it arrived (check by hand): {label}")
            continue
        if not apply:
            print(f"  would re-read: {label}: {', '.join(missing)}")
            continue
        if limit and reread >= limit:
            print(f"  stopped at --limit {limit}; run again for the rest")
            break
        reread += 1
        try:
            with llm_gate.tier(llm_gate.TIER_PIPELINE):
                result = rfq_inbox.refetch_reply_files(rfq["project_id"], row["id"])
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {label}: {exc}")
            continue
        audit(None, "rfq.link_refetch", "rfq_message", row["id"], {
            "source": "reprocess_rfq_replies",
            "pdfs_found": result["pdfs_found"],
            "extraction_status": result["extraction_status"],
        })
        recovered += result["extraction_status"] == "done"
        print(f"  re-read: {label}: {result['extraction_status']}")
    if apply:
        print(f"Phase 1: {reread} replies re-read, {recovered} quotes recovered")


def _preview_check(sb, project_id: str) -> list[str]:
    """What "Check for quotes now" would store for a project, read-only: the
    messages on its RFQ threads not yet stored whose sender it accepts."""
    lines = []
    seen: set[str] = set()
    for send in rfq_inbox._project_check_sends(sb, project_id):
        conversation_id = send.get("conversation_id")
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        for msg in rfq_inbox._conversation_messages(conversation_id):
            from_addr = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
            if not from_addr or rfq_inbox._is_own_mailbox(from_addr):
                continue
            stored = (
                sb.table("rfq_messages").select("id").eq("graph_message_id", msg["id"])
                .execute()
            ).data
            if stored:
                continue
            relation = rfq_inbox._sender_relation(sb, send["vendor_contacts"], from_addr)
            if relation == rfq_inbox.SENDER_INTERNAL:
                continue
            lines.append(
                f"{rfq_inbox._send_label(send)}: {from_addr} [{relation}] "
                f"{(msg.get('receivedDateTime') or '')[:10]}"
                + (" with attachments" if msg.get("hasAttachments") else "")
            )
    return lines


def phase_check(sb, check_days: int, apply: bool, only: set[str]) -> None:
    sends = (
        sb.table("rfq_sends")
        .select("rfqs!inner(project_id, projects(name, number))")
        .eq("status", "sent")
        .gte("sent_at", _since(check_days))
        .execute()
    ).data or []
    projects: dict[str, dict] = {}
    for s in sends:
        rfq = s.get("rfqs") or {}
        if only and rfq["project_id"] not in only:
            continue
        projects.setdefault(rfq["project_id"], rfq.get("projects") or {})
    print(f"Phase 2: {len(projects)} projects with RFQs sent in the last {check_days} days")
    for project_id, project in projects.items():
        label = f"{project.get('number') or ''} {project.get('name') or ''} ({project_id})"
        if not apply:
            try:
                pending = _preview_check(sb, project_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {label}: could not read its threads ({exc})")
                continue
            print(f"  {label}: {len(pending)} new replies would be stored")
            for line in pending:
                print(f"      {line}")
            continue
        try:
            result = rfq_inbox.check_project_quotes(project_id)
        except rfq_inbox.CheckAlreadyRunning:
            print(f"  ! {label}: a check is already running, skipped")
            continue
        print(
            f"  checked: {label}: {result['quotes_created']} quotes from "
            f"{result['sends_checked']} threads"
            + (f" ({'; '.join(result['errors'])})" if result["errors"] else "")
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=60,
                        help="phase 1 window: replies received in the last N days")
    parser.add_argument("--limit", type=int, default=0,
                        help="phase 1: re-read at most N replies (0 = no limit)")
    parser.add_argument("--check-days", type=int, default=0,
                        help="phase 2 window: projects with RFQs sent in the last N days (0 = skip)")
    parser.add_argument("--project", action="append", default=[],
                        help="only this project id (repeatable; both phases)")
    parser.add_argument("--apply", action="store_true", help="do the work (default: dry run)")
    args = parser.parse_args()

    sb = get_supabase()
    only = set(args.project)
    print(f"Mailbox {get_settings().ms_sender}; {'APPLY' if args.apply else 'dry run'}")
    phase_replies(sb, args.days, args.apply, only, args.limit)
    if args.check_days > 0:
        phase_check(sb, args.check_days, args.apply, only)


if __name__ == "__main__":
    main()
