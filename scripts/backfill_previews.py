"""Backfill PDF preview derivatives (and mime/size metadata) for existing files.

Files uploaded before the preview pipeline shipped have preview_status='none'
and no mime_type/size_bytes. This converts every convertible office file
(.xlsx/.xlsm/.docx/.doc, excluding drawings) via the configured engine
(PREVIEW_ENGINE, default gotenberg — have the container running) and fills in
the missing metadata. Idempotent: 'ready' rows are skipped on re-run.

Also recovers rows stranded at 'pending' (a restart/crash between the upload
and its background conversion leaves them there): any 'pending' row older than
15 minutes is treated as stale and reconverted.

Usage:
    cd bdr_be
    uv run python scripts/backfill_previews.py [--dry-run] [--include-failed]
"""

import mimetypes
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a plain script: put the project root (bdr_be) on the import
# path so `app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.supabase_client import get_supabase
from app.services import office_preview, storage


_STALE_PENDING = timedelta(minutes=15)  # conversion takes ≤ ~4 min; older = stranded


def _is_stale_pending(row: dict) -> bool:
    if row["preview_status"] != "pending":
        return True  # other selected statuses are always candidates
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - created > _STALE_PENDING


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    include_failed = "--include-failed" in sys.argv
    statuses = ["none", "pending"] + (["failed"] if include_failed else [])

    sb = get_supabase()
    rows = (
        sb.table("project_files")
        .select(
            "id, project_id, filename, category, storage_path, preview_status, "
            "mime_type, size_bytes, created_at"
        )
        .in_("preview_status", statuses)
        .order("created_at")
        .execute()
    ).data or []

    candidates = [
        r
        for r in rows
        if office_preview.is_convertible(r["filename"], r["category"])
        and _is_stale_pending(r)
    ]
    print(f"{len(rows)} files with status in {statuses}; {len(candidates)} convertible")

    ready = failed = 0
    for rec in candidates:
        label = f"{rec['filename']} ({rec['category']}, {rec['id']})"
        if dry_run:
            print(f"would convert: {label}")
            continue

        # Backfill missing metadata while we have the bytes anyway.
        updates: dict = {"preview_status": "pending"}
        if rec.get("mime_type") is None:
            updates["mime_type"] = mimetypes.guess_type(rec["filename"] or "")[0]
        if rec.get("size_bytes") is None:
            try:
                updates["size_bytes"] = len(storage.download_file(rec["storage_path"]))
            except Exception:  # noqa: BLE001 — generate_preview will surface it
                pass
        sb.table("project_files").update(updates).eq("id", rec["id"]).execute()

        office_preview.generate_preview(rec["id"])
        status = (
            sb.table("project_files")
            .select("preview_status, preview_error")
            .eq("id", rec["id"])
            .single()
            .execute()
        ).data
        if status["preview_status"] == "ready":
            ready += 1
            print(f"ready:  {label}")
        else:
            failed += 1
            print(f"failed: {label} — {status.get('preview_error')}")
        time.sleep(1)  # be kind to the converter

    if not dry_run:
        print(f"\nDone: {ready} ready, {failed} failed, {len(candidates)} attempted")


if __name__ == "__main__":
    main()
