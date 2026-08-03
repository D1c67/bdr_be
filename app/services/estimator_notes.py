"""Mirroring a send's message into the estimator-notes thread (0080).

The send modals collect a batch-wide "Message to the estimator(s)", which used
to live only in two places: the top of the outbound email, and
`file_send_batches.message` (visible in the Plans & Specs Log). Someone who
typed it there reasonably expected it in Project notes — the panel that reads
`estimator_notes` — and never found it, because the only writer of that table
was the notes composer itself.

`mirror_send_message` closes that gap: after a send is actually delivered, the
message is copied into the thread as a note authored by the sender, tagged with
the send it came from (`source`) so the UI can label it as mirrored rather than
hand-written.

Deliberately SILENT
-------------------
No `notifications` row and no notification email — the recipients are getting
the package/update email that already carries this exact text at the top, and a
bell on top of that is pure duplicate. The note still counts toward the notes
panel's own unread badge for anyone who hasn't read the thread (the badge is
computed from note timestamps in routers/notes.py, not from notifications), so
the estimator does see it as new when they open the project.

What is NOT mirrored
--------------------
Only the batch-wide message. Per-file notes and the per-section "what changed"
notes (0077) stay where they belong — on the file rows and in the Plans & Specs
Log — so the thread doesn't turn into a duplicate of the log.

Best-effort by contract
-----------------------
Every caller invokes this AFTER the email is out, so it must never raise: a lost
mirror is cosmetic, while an exception here would tempt a rollback of a
delivered send. Failures are logged and swallowed.

sync SDK: plain `def`, called from plain-`def` handlers (bdr-event-loop-blocking).
"""

import logging

from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# Kept here (not in routers/notes.py) so the service layer never imports a
# router; the router re-exports it for its request model.
NOTE_MAX_CHARS = 4000

# Mirror sources — MUST match the estimator_notes_source_chk constraint (0080).
SOURCE_PACKAGE_SEND = "package_send"   # assign/re-send: the full plans & specs package
SOURCE_UPDATE_SEND = "update_send"     # send-file-updates: revisions/addenda/additional
MIRROR_SOURCES = (SOURCE_PACKAGE_SEND, SOURCE_UPDATE_SEND)


def mirror_send_message(
    *,
    project_id: str,
    author_id: str | None,
    message: str | None,
    source: str,
) -> dict | None:
    """Copy a send's batch-wide `message` into the project's notes thread.

    Returns the inserted row, or None when there was nothing to mirror (no
    message) or the insert failed. Never raises — see the module docstring.

    `source` must be one of `MIRROR_SOURCES`; an unknown value is a programming
    error, so it's logged and dropped rather than written as a row the check
    constraint would reject anyway.
    """
    text = (message or "").strip()
    if not text:
        return None
    if source not in MIRROR_SOURCES:
        logger.warning("mirror_send_message: unknown source %r — not mirrored", source)
        return None
    # The send modals cap the message at the same 4000 chars a note allows, so
    # this only bites if one of those limits drifts; truncating beats losing the
    # whole mirror.
    if len(text) > NOTE_MAX_CHARS:
        text = text[: NOTE_MAX_CHARS - 1].rstrip() + "…"

    try:
        rows = (
            get_supabase()
            .table("estimator_notes")
            .insert(
                {
                    "project_id": project_id,
                    "author_id": author_id,
                    "body": text,
                    "source": source,
                }
            )
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001 — the email is already delivered
        logger.warning("mirror_send_message: note mirror failed", exc_info=True)
        return None
    return rows[0] if rows else None
