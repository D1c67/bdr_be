"""Unified project-documents hub: fold the three document stores into one flat
set of business folders.

A PM project's Documents tab shows *everything* — PM uploads (`pm_documents`,
the only writable store), bidding files (`project_files`, read-only mirror), and
certified-payroll files (`cp_record_files`, read-only, tagged to this project via
`cp_record_file_projects`). Each source category maps into a business folder
(Plans, Specs, Quotes, Billing, Certified Payroll, …); the folder is what the UI
groups by and what the ZIP export uses as its directory names.

This module is the single source of truth for that mapping and for the read-side
union. The frontend mirrors FOLDER_ORDER / the writable-folder map the same way
`ExportFilesModal` mirrors `file_export`'s category order.
"""

import logging

from app.core.features import SubApp, is_enabled
from app.core.supabase_client import get_supabase
from app.services import estimator_rounds

logger = logging.getLogger(__name__)


def _is_missing_relation(exc: Exception) -> bool:
    """True when an error means the referenced table/relation is not deployed
    (undefined_table / PostgREST schema-cache miss), as opposed to a transient
    failure that should propagate."""
    text = str(exc).lower()
    return (
        "42p01" in text  # postgres undefined_table
        or "pgrst205" in text  # PostgREST: table not found in schema cache
        or "does not exist" in text
        or "could not find the table" in text
    )

CP_FOLDER = "certified_payroll"

# Canonical display order for the business folders (also the ZIP dir order).
FOLDER_ORDER: list[str] = [
    "plans",
    "specifications",
    "quotes",
    "estimates",
    "proposals",
    "revisions",
    "contracts",
    "change_orders",
    "submittals",
    "rfis",
    "permits",
    "as_builts",
    "schedule",
    "billing",
    "photos",
    "correspondence",
    "closeout",
    CP_FOLDER,
    "other",
]

# Human labels — used as ZIP directory names. The frontend has its own i18n
# labels for the UI; keep these two in rough sync.
FOLDER_LABELS: dict[str, str] = {
    "plans": "Plans",
    "specifications": "Specifications",
    "quotes": "Quotes",
    "estimates": "Estimates",
    "proposals": "Proposals",
    "revisions": "Revisions & Addenda",
    "contracts": "Contracts",
    "change_orders": "Change Orders",
    "submittals": "Submittals",
    "rfis": "RFIs",
    "permits": "Permits",
    "as_builts": "As-Builts",
    "schedule": "Schedule",
    "billing": "Billing",
    "photos": "Photos",
    "correspondence": "Correspondence",
    "closeout": "Closeout",
    CP_FOLDER: "Certified Payroll",
    "other": "Other",
}

# pm_documents.category → folder. Every pm category maps to a writable folder,
# so the union of these values is exactly the set of upload targets.
_PM_CATEGORY_FOLDER: dict[str, str] = {
    "contract": "contracts",
    "change_order": "change_orders",
    "submittal": "submittals",
    # 0067. RFI attachments would be buried among unrelated mail under
    # 'correspondence', so they get their own folder.
    "rfi": "rfis",
    "permit": "permits",
    "as_built": "as_builts",
    "drawing": "plans",
    "schedule": "schedule",
    "correspondence": "correspondence",
    "photo": "photos",
    "closeout": "closeout",
    "specification": "specifications",
    "quote": "quotes",
    "estimate": "estimates",
    "billing": "billing",
    "other": "other",
}

# project_files.category (bidding, read-only) → folder.
_BID_CATEGORY_FOLDER: dict[str, str] = {
    "drawing": "plans",
    "specification": "specifications",
    "quote": "quotes",
    "rfq_split": "quotes",
    "estimate": "estimates",
    "boq": "estimates",
    "markup": "estimates",
    "proposal": "proposals",
    "revision": "revisions",
    "addendum": "revisions",
    "additional": "revisions",
    "estimator_additional": "revisions",
    "other": "other",
}


def folder_for(source: str, category: str | None) -> str:
    """Map a source row's native category into a business folder id."""
    if source == "cp":
        return CP_FOLDER
    if source == "pm":
        return _PM_CATEGORY_FOLDER.get(category or "", "other")
    return _BID_CATEGORY_FOLDER.get(category or "", "other")


def folder_rank(folder: str) -> int:
    try:
        return FOLDER_ORDER.index(folder)
    except ValueError:
        return len(FOLDER_ORDER)


def _pm_documents(project_id: str) -> list[dict]:
    rows = (
        get_supabase()
        .table("pm_documents")
        .select("id, category, storage_path, filename, size_bytes, note, created_at")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    return [
        {
            "key": f"pm:{r['id']}",
            "source": "pm",
            "id": r["id"],
            "folder": folder_for("pm", r.get("category")),
            "category": r.get("category"),
            "filename": r.get("filename"),
            "size_bytes": r.get("size_bytes"),
            "note": r.get("note"),
            "created_at": r.get("created_at"),
            "writable": True,
            "storage_path": r["storage_path"],
        }
        for r in rows
    ]


def _bidding_documents(project_id: str) -> list[dict]:
    # Read-only mirror. Exclude unsent estimator drafts exactly as the internal
    # bidding export does (estimator_rounds.exclude_unsent) so a hub download can
    # never surface a draft the team hasn't received.
    #
    # Deliberately NOT gated on BIDDING_ENABLED, unlike _cp_documents below: these
    # are the project's own plans, specs and quotes, and a PM crew must keep them
    # when the bidding module is dark. The CP asymmetry is about content, not
    # symmetry — payroll files are employee pay data that has no business
    # reaching a PM reader once that module is switched off.
    q = (
        get_supabase()
        .table("project_files")
        .select("id, category, storage_path, filename, size_bytes, note, created_at")
        .eq("project_id", project_id)
    )
    rows = estimator_rounds.exclude_unsent(q).execute().data or []
    return [
        {
            "key": f"bid:{r['id']}",
            "source": "bid",
            "id": r["id"],
            "folder": folder_for("bid", r.get("category")),
            "category": r.get("category"),
            "filename": r.get("filename"),
            "size_bytes": r.get("size_bytes"),
            "note": r.get("note"),
            "created_at": r.get("created_at"),
            "writable": False,
            "storage_path": r["storage_path"],
        }
        for r in rows
    ]


def _cp_documents(project_id: str) -> list[dict]:
    """Certified-payroll files tagged to this project, latest revision per report.

    Files are associated with projects at generation time (cp_record_file_projects,
    migration 0066). An aggregate file (PVW/eComply) that covers several projects
    is tagged to each; per-project LCPtracker/paper files to their one project. We
    show only the newest revision of each weekly report to keep the hub tidy —
    older revisions live in the Payroll module. Degrades to [] (logged) if the CP
    tagging table isn't present, so the hub never hard-fails on a partial deploy.

    Empty while CERTIFIED_PAYROLL_ENABLED is off. This is the containment
    boundary for the CP flag, not a cosmetic one: the hub's /documents/file and
    /documents/export routes resolve through this same list, so without the
    check a deployment with Payroll switched off would still hand every PM
    reader signed URLs to certified-payroll files — employee names,
    classifications and pay data — through the PM module.
    """
    if not is_enabled(SubApp.CERTIFIED_PAYROLL):
        return []
    sb = get_supabase()
    try:
        links = (
            sb.table("cp_record_file_projects")
            .select("record_file_id")
            .eq("project_id", project_id)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        # Degrade to "no CP folder" ONLY when the tagging table isn't deployed
        # (a partial-deploy signature). A transient error (timeout, network) must
        # propagate — swallowing it would silently hide real CP files and read as
        # "this project has no certified payroll".
        if not _is_missing_relation(exc):
            raise
        logger.warning("CP document tagging not deployed for project %s: %s", project_id, exc)
        return []
    file_ids = list({link["record_file_id"] for link in links})
    if not file_ids:
        return []

    files = (
        sb.table("cp_record_files")
        .select("id, record_id, filename, storage_path, size_bytes, created_at")
        .in_("id", file_ids)
        .execute()
    ).data or []
    record_ids = list({f["record_id"] for f in files})
    records = (
        sb.table("cp_records")
        .select("id, payroll_report_id, revision_number")
        .in_("id", record_ids)
        .execute()
    ).data or []
    rec_by_id = {r["id"]: r for r in records}

    # Keep only the highest-revision record per weekly report.
    latest_per_report: dict[str, dict] = {}
    for r in records:
        rep = r["payroll_report_id"]
        cur = latest_per_report.get(rep)
        if cur is None or (r.get("revision_number") or 0) > (cur.get("revision_number") or 0):
            latest_per_report[rep] = r
    keep_record_ids = {r["id"] for r in latest_per_report.values()}

    report_ids = list(latest_per_report.keys())
    reports = (
        sb.table("cp_payroll_reports")
        .select("id, week_start_date")
        .in_("id", report_ids)
        .execute()
    ).data or [] if report_ids else []
    week_by_report = {r["id"]: r.get("week_start_date") for r in reports}

    items: list[dict] = []
    for f in files:
        rec = rec_by_id.get(f["record_id"])
        if not rec or rec["id"] not in keep_record_ids:
            continue
        items.append(
            {
                "key": f"cp:{f['id']}",
                "source": "cp",
                "id": f["id"],
                "folder": CP_FOLDER,
                "category": None,
                "filename": f.get("filename"),
                "size_bytes": f.get("size_bytes"),
                "note": None,
                "created_at": f.get("created_at"),
                "writable": False,
                "storage_path": f["storage_path"],
                "cp_meta": {
                    "week_start_date": week_by_report.get(rec["payroll_report_id"]),
                    "revision_number": rec.get("revision_number"),
                },
            }
        )
    return items


def list_project_documents(project_id: str) -> list[dict]:
    """The unified hub read: PM + bidding + certified-payroll documents for a
    project, each carrying its business `folder`, a stable `key` ("source:id"),
    and `writable` (only PM uploads are). Rows include `storage_path` for the
    server-side download/export resolvers — the API layer strips it before
    returning to the client.

    Sorted by folder order, then filename, so grouping on the frontend is stable.
    """
    items = _pm_documents(project_id) + _bidding_documents(project_id) + _cp_documents(project_id)
    items.sort(key=lambda i: (folder_rank(i["folder"]), (i.get("filename") or "").lower()))
    return items
