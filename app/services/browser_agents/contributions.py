"""Helper contributions onto imported RFIs (docs/browser_agents/SPEC.md 5.4.6, D15).

When a help conversation gives the agent something the RFI should carry (a
file the helper uploaded and marked attachable, or the helper's own words),
the worker records it as a browser_agent_rfi_contributions row keyed
(project_id, platform, external_rfi_id). This module applies those rows to
the RFI once that RFI exists in BDR, and never invents an RFI row for them:

  file  the artifact's bytes are copied into the project's Documents
        (pm_documents, category rfi, note marker [agent-help-file:<sha256>],
        reused when the same file was added before) and attached to the RFI
        (rfi_attachments, external_file_id 'help:<sha256>', deduped by the
        0156 unique index);
  note  {text, by, at, help_id, contribution_id} is appended to
        rfis.external_supplement unless that contribution id is already there
        (one atomic SQL statement, so concurrent writers never lose a note).

Each applied row gets applied_at and applied_rfi_id (compare-and-set on
applied_at is null, so two sweepers apply it once). `apply_pending` runs at
the end of every agent apply and from the sweeper, so a contribution for an
RFI the account cannot read yet lands when the RFI first appears.

zip and dwg files are never attached, whatever the row says. Unlink
(pm_rfi_sync.purge_imported) drops 'help:' attachments with the other
imported files; the documents stay in Documents.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core import supabase_client

logger = logging.getLogger(__name__)

TABLE = "browser_agent_rfi_contributions"
HELP_FILE_MARKER = "[agent-help-file:{sha}]"
NEVER_ATTACH_EXTENSIONS = (".zip", ".dwg")
SWEEP_PROJECTS = 100
_KIND_BY_CONTRIBUTION = {"file_question": "question", "file_answer": "answer"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_applied(sb, contribution: dict, rfi_id: str) -> bool:
    rows = (
        sb.table(TABLE).update({"applied_at": _now_iso(), "applied_rfi_id": rfi_id})
        .eq("id", contribution["id"]).is_("applied_at", "null").execute()
    ).data or []
    return bool(rows)


def _existing_help_doc(sb, project_id: str, sha: str) -> dict | None:
    marker = HELP_FILE_MARKER.format(sha=sha)
    rows = (
        sb.table("pm_documents").select("id, filename").eq("project_id", project_id)
        .like("note", f"%{marker}%").limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _apply_file(sb, contribution: dict, rfi: dict) -> bool:
    """Attach one helper file. False when it must never be attached (the
    row is then marked applied with nothing attached, so it is not retried)."""
    from app.routers.files import _safe_content_type
    from app.services import storage
    from app.services.browser_agents import artifacts

    rows = (
        sb.table("browser_agent_artifacts").select("*").eq("id", str(contribution["artifact_id"])).limit(1)
        .execute()
    ).data or []
    artifact = rows[0] if rows else None
    filename = str((artifact or {}).get("filename") or "helper-file")
    if (
        artifact is None
        or not artifact.get("attachable")
        or artifact.get("kind") != "helper_upload"
        or filename.lower().endswith(NEVER_ATTACH_EXTENSIONS)
    ):
        logger.warning("browser agents: contribution %s names a file that is not attachable", contribution["id"])
        return False
    sha = str(artifact["sha256"])
    project_id = contribution["project_id"]
    doc = _existing_help_doc(sb, project_id, sha)
    if doc is None:
        data = artifacts.download_object(artifact["storage_path"])
        mime = _safe_content_type(filename)
        path = storage.build_object_path(project_id, "pm/rfi", filename)
        storage.upload_file(path, data, mime)
        help_id = contribution.get("help_id") or ""
        doc = (
            sb.table("pm_documents").insert({
                "project_id": project_id,
                "category": "rfi",
                "storage_path": path,
                "filename": filename[:255],
                "mime_type": mime,
                "size_bytes": len(data),
                "note": f"Added from agent help {help_id} {HELP_FILE_MARKER.format(sha=sha)}",
                "uploaded_by": None,
            }).execute()
        ).data[0]
    try:
        sb.table("rfi_attachments").insert({
            "rfi_id": rfi["id"],
            "doc_key": f"pm:{doc['id']}",
            "kind": _KIND_BY_CONTRIBUTION.get(contribution.get("kind"), "question"),
            "external_file_id": f"help:{sha}",
            "created_by": None,
        }).execute()
    except Exception as exc:  # noqa: BLE001 - the same file is already on the RFI
        if "23505" not in str(exc) and "duplicate key" not in str(exc):
            raise
    return True


def _apply_note(sb, contribution: dict, rfi: dict) -> None:
    """Append the note unless its contribution id is there already, in one
    SQL statement (0158 browser_agent_append_rfi_supplement): two writers can
    never overwrite each other's entry."""
    entry = {
        "text": contribution.get("note_text") or "",
        "by": contribution.get("note_author_label"),
        "at": contribution.get("created_at") or _now_iso(),
        "help_id": str(contribution["help_id"]) if contribution.get("help_id") else None,
        "contribution_id": str(contribution["id"]),
    }
    data = sb.rpc("browser_agent_append_rfi_supplement", {"p_rfi": str(rfi["id"]), "p_entry": entry}).execute().data
    if isinstance(data, list):
        data = data[0] if data else None
    if not data:
        raise RuntimeError("the RFI note was not appended; retried later")


def apply_pending(sb, project_id: str) -> int:
    """Apply every pending contribution of `project_id` whose RFI exists.
    Returns contributions applied. One failing row never stops the rest."""
    pending = (
        sb.table(TABLE).select("*").eq("project_id", project_id).is_("applied_at", "null")
        .order("created_at").limit(200).execute()
    ).data or []
    if not pending:
        return 0
    rfis = {
        str(r["external_id"]): r for r in (
            sb.table("rfis").select("id, external_id").eq("project_id", project_id)
            .not_.is_("external_id", "null").execute()
        ).data or []
        if r.get("external_id")
    }
    applied = 0
    for contribution in pending:
        rfi = rfis.get(str(contribution.get("external_rfi_id")))
        if rfi is None or contribution.get("platform") != "procore":
            continue
        try:
            if contribution.get("kind") == "note":
                _apply_note(sb, contribution, rfi)
            else:
                _apply_file(sb, contribution, rfi)
        except Exception:  # noqa: BLE001 - retried by the next sweep
            logger.exception("browser agents: contribution %s not applied yet", contribution.get("id"))
            continue
        if _mark_applied(sb, contribution, rfi["id"]):
            applied += 1
    return applied


def sweep_pending(now: datetime, settings_row: dict[str, Any]) -> int:
    """Sweeper step: projects with pending contributions, each applied."""
    sb = supabase_client.get_supabase()
    rows = (
        sb.table(TABLE).select("project_id").is_("applied_at", "null").order("created_at")
        .limit(SWEEP_PROJECTS * 5).execute()
    ).data or []
    projects = list(dict.fromkeys(str(r["project_id"]) for r in rows if r.get("project_id")))[:SWEEP_PROJECTS]
    return sum(apply_pending(sb, pid) for pid in projects)
