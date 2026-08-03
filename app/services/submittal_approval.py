"""Submittal approval packages — send collected submittals to the GC (0081).

The GC-facing half of the submittal round trip. 0073 asks each material
category's VENDORS for their product submittals and files their replies; this
module packages what came back — plus Submittal Bank pulls, project uploads, and
anything the sender uploads in the modal — and emails it to the general
contractor for approval.

DELIVERY SHAPE. The GC receives ONE combined PDF per material category —
"Requested Submittals - <Project#> - <Category>.pdf" — each opening on a G3 cover
page naming the category and listing its contents, with every submittal in that
category appended behind it (see _build_category_documents). A copy of each is
archived in the project's Documents hub under that same name, so what we sent and
what we kept are byte-identical and identically named. Alongside them rides the
package's single transmittal, which is the sheet the GC marks up and returns.

Two entry points:

  available()      what's on file, grouped by category, for the modal to show.
  create_and_send() build the package, email it, and log exactly what went out.

Unlike the vendor side this is ONE email: a To list and a CC list on a single
Graph draft, so the GC's approval thread stays single. It is sent FROM the
ingestion mailbox (like 0073's requests) purely so the captured conversationId
gives the future approval-response feature a key to match GC replies on —
nothing consumes it yet.

SECURITY. Every file the sender picks is named by an opaque key
("att:<id>" / "bank:<id>" / "pm:<id>"). Keys are never trusted: create_and_send
rebuilds the available index for the project and rejects any key not in it, so a
key belonging to another project is indistinguishable from one that doesn't
exist. This is the only thing standing between a project-scoped endpoint and
cross-project file disclosure — do not "optimize" it into a direct lookup.
"""

import logging
import mimetypes
import re

from app.core.config import get_settings
from app.services import email_branding, graph_email, pdf_combine, storage, submittal_pdf
from app.services.email_branding import SIGNOFF
from app.services.notifications import audit
from app.services.office_preview import ConversionError

logger = logging.getLogger(__name__)

# Materials with no category still need somewhere to live in the modal; the GC
# side (unlike the vendor side) legitimately submits them.
UNCATEGORIZED_LABEL = "Uncategorized"

# Guardrails on one package. Generous — a real submittal package runs 10-40
# files — but bounded so a runaway selection can't build a 2 GB email.
MAX_FILES_PER_PACKAGE = 300


def _content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _safe_component(name: str) -> str:
    """Make a value safe to use as a single OneDrive path component."""
    return re.sub(r'[\\/:*?"<>|#%]+', "_", name).strip() or "file"


# ── What's available to submit ───────────────────────────────────────────────


def available(project_id: str) -> dict:
    """Every submittal file on file for the project, grouped by material category.

    Three sources feed a category:
      • vendor replies — attachments on emails the ingestion pipeline matched to
        a submittal request send for that category (0073 + 0061).
      • bank links     — Submittal Bank files linked to one of the category's
        materials (0072 + 0074).
      • project docs   — PDFs uploaded against one of the category's materials,
        archived in the Documents hub (0074), and anything uploaded straight into
        an approval package by `stage_upload`.

    Categories are returned in the same sort order as /material-categories, with
    an Uncategorized bucket last when the project has uncategorized materials.
    Markup categories are excluded (there is nothing to submit); General Material
    is INCLUDED — unlike the vendor side, we do submit it to the GC.

    A file appears at most once per category even when several of the category's
    materials link it (a shared cut sheet is reachable from every size it covers).
    """
    from app.core.supabase_client import get_supabase

    sb = get_supabase()

    cats = (
        sb.table("material_categories")
        .select("id, name, kind, is_general, sort_order")
        .eq("kind", "material")
        .order("sort_order")
        .execute()
    ).data or []
    cat_by_id = {c["id"]: c for c in cats}

    materials = (
        sb.table("pm_materials")
        .select("id, description, material_category_id, category_label")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    mat_by_id = {m["id"]: m for m in materials}

    # bucket key: the category id, or None for the Uncategorized bucket.
    buckets: dict[str | None, dict] = {}

    def bucket(cat_id: str | None) -> dict:
        if cat_id not in buckets:
            cat = cat_by_id.get(cat_id) if cat_id else None
            buckets[cat_id] = {
                "material_category_id": cat_id,
                "name": cat["name"] if cat else UNCATEGORIZED_LABEL,
                "is_general": bool(cat.get("is_general")) if cat else False,
                "sort_order": cat.get("sort_order") if cat else None,
                "material_count": 0,
                "_seen": set(),
                "files": [],
            }
        return buckets[cat_id]

    def add_file(cat_id: str | None, entry: dict) -> None:
        b = bucket(cat_id)
        if entry["key"] in b["_seen"]:
            return
        b["_seen"].add(entry["key"])
        b["files"].append(entry)

    for m in materials:
        cid = m.get("material_category_id")
        # A category id that no longer resolves (deleted category) falls into
        # Uncategorized rather than creating a nameless bucket.
        bucket(cid if cid in cat_by_id else None)["material_count"] += 1

    # ── Source 1: vendor reply attachments ───────────────────────────────────
    req_ids = [
        r["id"]
        for r in (
            sb.table("submittal_requests").select("id").eq("project_id", project_id).execute()
        ).data
        or []
    ]
    if req_ids:
        sends = (
            sb.table("submittal_request_sends")
            .select(
                "id, material_category_id, vendor_contacts(name, vendors(name)), "
                "submittal_response_emails(email_id)"
            )
            .in_("request_id", req_ids)
            .execute()
        ).data or []
        email_to_send: dict[str, dict] = {}
        for s in sends:
            for link in s.get("submittal_response_emails") or []:
                email_to_send[link["email_id"]] = s
        if email_to_send:
            atts = (
                sb.table("ingested_email_attachments")
                .select("id, email_id, filename, mime_type, size_bytes, storage_path")
                .in_("email_id", list(email_to_send))
                .execute()
            ).data or []
            for a in atts:
                # Metadata-only rows (too large / item attachments) have no bytes
                # to send — showing them would offer a file we can't attach.
                if not a.get("storage_path"):
                    continue
                s = email_to_send[a["email_id"]]
                vc = s.get("vendor_contacts") or {}
                vendor = (vc.get("vendors") or {}).get("name") or vc.get("name") or "Vendor"
                cid = s.get("material_category_id")
                add_file(
                    cid if cid in cat_by_id else None,
                    {
                        "key": f"att:{a['id']}",
                        "source": "vendor_reply",
                        "filename": a["filename"],
                        "size_bytes": a.get("size_bytes"),
                        "origin": vendor,
                        "description": None,
                        "pm_material_id": None,
                    },
                )

    # ── Sources 2 & 3: bank links and project document uploads ───────────────
    links = (
        sb.table("pm_material_submittals")
        .select("id, pm_material_id, source, submittal_material_id, document_id")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if links:
        bank_mat_ids = [
            link["submittal_material_id"]
            for link in links
            if link["source"] == "bank" and link.get("submittal_material_id")
        ]
        doc_ids = [
            link["document_id"]
            for link in links
            if link["source"] == "uploaded" and link.get("document_id")
        ]

        files_by_bank_mat: dict[str, list[dict]] = {}
        if bank_mat_ids:
            for row in (
                sb.table("submittal_material_files")
                .select("material_id, submittal_files(id, file_name, file_path, size_bytes)")
                .in_("material_id", bank_mat_ids)
                .execute()
            ).data or []:
                f = row.get("submittal_files")
                if f and f.get("file_path"):
                    files_by_bank_mat.setdefault(row["material_id"], []).append(f)

        docs: dict[str, dict] = {}
        if doc_ids:
            for d in (
                sb.table("pm_documents")
                .select("id, filename, storage_path, size_bytes")
                .in_("id", doc_ids)
                .execute()
            ).data or []:
                docs[d["id"]] = d

        for link in links:
            mat = mat_by_id.get(link["pm_material_id"])
            if not mat:
                continue
            cid = mat.get("material_category_id")
            cid = cid if cid in cat_by_id else None
            desc = mat.get("description")
            if link["source"] == "bank":
                for f in files_by_bank_mat.get(link.get("submittal_material_id") or "", []):
                    add_file(
                        cid,
                        {
                            "key": f"bank:{f['id']}",
                            "source": "bank",
                            "filename": f["file_name"],
                            "size_bytes": f.get("size_bytes"),
                            "origin": "Submittal Bank",
                            "description": desc,
                            "pm_material_id": mat["id"],
                        },
                    )
            else:
                d = docs.get(link.get("document_id") or "")
                if d and d.get("storage_path"):
                    add_file(
                        cid,
                        {
                            "key": f"pm:{d['id']}",
                            "source": "document",
                            "filename": d["filename"],
                            "size_bytes": d.get("size_bytes"),
                            "origin": "Uploaded",
                            "description": desc,
                            "pm_material_id": mat["id"],
                        },
                    )

    # ── Staged uploads: submittal docs with no material link ─────────────────
    # `stage_upload` archives straight into pm_documents with no
    # pm_material_submittals row, so they'd otherwise be invisible here. They
    # land in the category recorded on the doc note, or Uncategorized.
    #
    # The category-'submittal' folder also holds things that are NOT submittals
    # to re-send — the vendor request sheets (0073) and this feature's own sent
    # transmittals — so match on the staged marker rather than the folder, or
    # every package would offer its predecessor's cover sheet as a file.
    linked_doc_ids = {link.get("document_id") for link in links if link.get("document_id")}
    staged = (
        sb.table("pm_documents")
        .select("id, filename, storage_path, size_bytes, note")
        .eq("project_id", project_id)
        .eq("category", "submittal")
        .like("note", f"{STAGED_NOTE_PREFIX}%")
        .execute()
    ).data or []
    for d in staged:
        if d["id"] in linked_doc_ids or not d.get("storage_path"):
            continue
        cid = _staged_category(d.get("note"))
        add_file(
            cid if cid in cat_by_id else None,
            {
                "key": f"pm:{d['id']}",
                "source": "document",
                "filename": d["filename"],
                "size_bytes": d.get("size_bytes"),
                "origin": "Uploaded",
                "description": None,
                "pm_material_id": None,
            },
        )

    out = [
        {k: v for k, v in b.items() if k != "_seen"}
        for b in sorted(
            buckets.values(),
            # Uncategorized (sort_order None) sorts last.
            key=lambda b: (b["sort_order"] is None, b["sort_order"] or 0, b["name"]),
        )
    ]
    return {"categories": out}


# A staged upload is identified by its note, because pm_documents has no column
# for either fact: STAGED_NOTE_PREFIX marks it as "a file the team is submitting"
# (as opposed to a vendor request sheet or a sent transmittal, which share the
# 'submittal' folder), and the [cat:<uuid>] marker records which category it was
# filed under. Keep both in sync with `stage_upload`.
STAGED_NOTE_PREFIX = "Submittal for approval"
# Deliberately loose on shape: the extracted value is only ever used as a lookup
# into the project's real category map (`cid if cid in cat_by_id else None`), so
# that membership check — not this pattern — is what makes it trustworthy.
# Pinning it to a 36-char UUID here would just make the parse silently
# category-blind if ids ever change shape.
_STAGED_CAT_RE = re.compile(r"\[cat:([^\]]{1,64})\]")


def _staged_category(note: str | None) -> str | None:
    m = _STAGED_CAT_RE.search(note or "")
    return m.group(1) if m else None


def stage_upload(
    project_id: str,
    material_category_id: str | None,
    filename: str,
    content: bytes,
    user_id: str,
) -> dict:
    """Archive a submittal PDF the sender is adding to a package and return the
    file entry the modal puts in its selection (key `pm:<document_id>`).

    The file lands in the Documents hub straight away rather than being held in
    memory until send: a submittal the team uploaded belongs in the project's
    documents whether or not this particular package goes out, and it means the
    send endpoint stays JSON instead of multipart. The category is stamped into
    the note so `available` can bucket it (pm_documents has no category column).
    """
    from app.core.supabase_client import get_supabase
    from fastapi import HTTPException, status

    sb = get_supabase()
    label = UNCATEGORIZED_LABEL
    if material_category_id:
        rows = (
            sb.table("material_categories")
            .select("id, name")
            .eq("id", material_category_id)
            .limit(1)
            .execute()
        ).data
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material category not found")
        label = rows[0]["name"]

    path = storage.build_object_path(project_id, "pm/submittal", filename)
    storage.upload_file(path, content, "application/pdf")
    note = f"{STAGED_NOTE_PREFIX} — {label}"
    if material_category_id:
        note += f" [cat:{material_category_id}]"
    try:
        doc = (
            sb.table("pm_documents")
            .insert(
                {
                    "project_id": project_id,
                    "category": "submittal",
                    "storage_path": path,
                    "filename": filename,
                    "mime_type": "application/pdf",
                    "size_bytes": len(content),
                    "note": note,
                    "uploaded_by": user_id,
                }
            )
            .execute()
        ).data[0]
    except Exception:
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001 — storage cleanup is best-effort
            logger.exception("failed to delete staged submittal object %s", path)
        raise

    audit(
        user_id,
        "submittal_approval.upload",
        "project",
        project_id,
        {"document_id": doc["id"], "category": label},
    )
    return {
        "key": f"pm:{doc['id']}",
        "source": "document",
        "filename": doc["filename"],
        "size_bytes": doc.get("size_bytes"),
        "origin": "Uploaded",
        "description": None,
        "pm_material_id": None,
        "material_category_id": material_category_id,
    }


# ── History ──────────────────────────────────────────────────────────────────


PACKAGE_SELECT = (
    "id, number, gc_id, recipients, cc_recipients, subject, message, "
    "send_status, error, files_delivery, sent_at, sent_by, "
    "approval_status, responded_at, responded_by, response_notes, "
    "supersedes_package_id, created_at, created_by, "
    "submittal_package_items(id, material_category_id, category_label, "
    "pm_material_id, source, filename, size_bytes, approval_status, "
    "responded_at, responded_by, response_notes)"
)


def list_packages(project_id: str) -> list[dict]:
    """Package history, newest first, with the per-file items.

    `supersedes_package_id` is returned raw rather than resolved to a number:
    every package in the chain is already in this same list, so the caller maps
    id → number without a second query.
    """
    from app.core.supabase_client import get_supabase

    return (
        get_supabase()
        .table("submittal_packages")
        .select(PACKAGE_SELECT)
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []


def _get_package(sb, project_id: str, package_id: str) -> dict:
    """One package, scoped to the project. A package belonging to another project
    is reported as missing — the caller must never be able to tell the two
    apart."""
    rows = (
        sb.table("submittal_packages")
        .select(PACKAGE_SELECT)
        .eq("project_id", project_id)
        .eq("id", package_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise ValueError("Submittal package not found for this project")
    return rows[0]


# ── Recording the GC's verdict (0082) ────────────────────────────────────────
#
# The GC's answer comes back as an email, a marked-up transmittal, or a phone
# call. A human reads it and records it here, per file — because a GC routinely
# approves most of a package and rejects one cut sheet.

# Item verdicts that mean "the GC accepted this file". 'approved_as_noted' is an
# acceptance with comments — the team may proceed — so it rolls up as approved,
# and the item's own badge keeps the distinction visible.
_ACCEPTED = ("approved", "approved_as_noted")


def _rollup(statuses: list[str]) -> str:
    """The package headline derived from its items' verdicts.

    Deliberately in code, not a DB trigger: this is presentation policy and it
    must be changeable without a migration.

      nothing decided        → pending
      decided, all accepted  → approved
      decided, all rejected  → denied
      anything else          → partial  (a mix, or some files still undecided)
    """
    if not statuses:
        return "pending"
    decided = [s for s in statuses if s != "pending"]
    if not decided:
        return "pending"
    if len(decided) < len(statuses):
        return "partial"
    if all(s in _ACCEPTED for s in decided):
        return "approved"
    if all(s == "rejected" for s in decided):
        return "denied"
    return "partial"


def record_verdicts(project_id: str, package_id: str, body: dict, user_id: str) -> dict:
    """Record the GC's per-file verdicts on a package and re-derive its headline.

    `body` is a SubmittalVerdictIn dump: a list of {id, approval_status,
    response_notes} plus an optional package-level note. Only the items named are
    touched, so the modal can save one row or all of them.

    Setting an item back to 'pending' CLEARS its responded_at/responded_by rather
    than leaving a timestamp on an undecided row — "pending with a response time"
    would be a lie the log has no way to explain.

    Raises ValueError (→ 400) for a package outside the project, an item outside
    the package, or a package that was never delivered.
    """
    from app.core.supabase_client import get_supabase

    sb = get_supabase()
    package = _get_package(sb, project_id, package_id)
    if package.get("send_status") != "sent":
        # A package the GC never received cannot have answered. Recording a
        # verdict on a failed send would put an approval in the log for an email
        # that does not exist.
        raise ValueError(
            "This package hasn't been sent, so there's no response to record."
        )

    items = {it["id"]: it for it in package.get("submittal_package_items") or []}
    updates = body.get("items") or []
    for row in updates:
        item_id = row.get("id")
        # Not in this package = fabricated, or from another project's package.
        # Both are reported identically.
        if item_id not in items:
            raise ValueError(f"Submittal file not found in this package: {item_id}")

    decided_now = 0
    for row in updates:
        item_id = row["id"]
        new_status = row["approval_status"]
        patch: dict = {
            "approval_status": new_status,
            "response_notes": (row.get("response_notes") or "").strip() or None,
        }
        if new_status == "pending":
            patch["responded_at"] = None
            patch["responded_by"] = None
        else:
            decided_now += 1
            patch["responded_at"] = "now()"
            patch["responded_by"] = user_id
        sb.table("submittal_package_items").update(patch).eq("id", item_id).execute()
        items[item_id] = {**items[item_id], **patch}

    rollup = _rollup([it["approval_status"] for it in items.values()])
    notes = (body.get("response_notes") or "").strip() or None
    pkg_patch: dict = {"approval_status": rollup, "response_notes": notes}
    if rollup == "pending":
        pkg_patch["responded_at"] = None
        pkg_patch["responded_by"] = None
    else:
        pkg_patch["responded_at"] = "now()"
        pkg_patch["responded_by"] = user_id
    sb.table("submittal_packages").update(pkg_patch).eq("id", package_id).execute()

    audit(
        user_id,
        "submittal_approval.verdict",
        "submittal_package",
        package_id,
        {
            "number": package.get("number"),
            "items": len(updates),
            "decided": decided_now,
            "approval_status": rollup,
        },
    )
    return _get_package(sb, project_id, package_id)


# ── Resubmittals (0082) ──────────────────────────────────────────────────────


def _item_key(item: dict) -> str | None:
    """The `available`-style key for a file already sent in a package, or None if
    its source row is gone.

    Rebuilding the key from the item's pointer column is what lets a previously
    sent file be re-selected: the same file keeps the same key, so ticking it in
    the resend modal is indistinguishable from ticking it in the original send.
    A null pointer means the source was deleted since (every pointer FK is ON
    DELETE SET NULL, by design) — there is nothing left to re-send, and the modal
    shows the row greyed out rather than silently dropping it.
    """
    if item.get("source") == "vendor_reply":
        return f"att:{item['attachment_id']}" if item.get("attachment_id") else None
    if item.get("source") == "bank":
        return f"bank:{item['submittal_file_id']}" if item.get("submittal_file_id") else None
    return f"pm:{item['document_id']}" if item.get("document_id") else None


def _prior_items(sb, package_id: str) -> list[dict]:
    """A package's items with the pointer columns `list_packages` omits — needed
    to rebuild each file's key."""
    return (
        sb.table("submittal_package_items")
        .select(
            "id, material_category_id, category_label, pm_material_id, source, "
            "attachment_id, submittal_file_id, document_id, filename, size_bytes, "
            "storage_path, approval_status, response_notes"
        )
        .eq("package_id", package_id)
        .execute()
    ).data or []


def _prior_entries(sb, package_id: str) -> dict[tuple[str | None, str], dict]:
    """Index a prior package's still-resendable files by (category, key).

    This is the SECOND source the resend path validates against, alongside
    `available`. It is safe to trust as a key source because the package it comes
    from was already scoped to the project by `_get_package`: every file in it is
    one this project previously sent. Without it, a file whose material link
    changed since — or whose bank link was removed — would be unresendable even
    though the GC is holding it and asking for it again.
    """
    out: dict[tuple[str | None, str], dict] = {}
    for it in _prior_items(sb, package_id):
        key = _item_key(it)
        if not key:
            continue
        out[(it.get("material_category_id"), key)] = {
            "key": key,
            "source": it["source"],
            "filename": it["filename"],
            "size_bytes": it.get("size_bytes"),
            "origin": PRIOR_ORIGIN,
            "description": None,
            "pm_material_id": it.get("pm_material_id"),
            "material_category_id": it.get("material_category_id"),
            "category_label": it.get("category_label") or UNCATEGORIZED_LABEL,
        }
    return out


# Shown as the file's origin badge when it is only reachable via the package
# being resent (its current-state source is gone from `available`).
PRIOR_ORIGIN = "Previously sent"


def resend_options(project_id: str, package_id: str) -> dict:
    """Everything the resend modal renders: the project's currently available
    submittals, MERGED with the files the package being resent contains.

    One list, not two. A resend is usually "the GC rejected this cut sheet, here
    is the corrected one" — so the modal must offer both the original files and
    everything else on file (plus its upload path), and showing them as separate
    trees would mean the same file appearing twice with two checkboxes.

    A file that was in the package is annotated with `prior_status` (the verdict
    it came back with) and `prior: true`; the rejected ones are what the caller
    pre-ticks. A file whose source has since been deleted comes back with
    `available: false` and no key — visible, explained, not selectable.
    """
    from app.core.supabase_client import get_supabase

    sb = get_supabase()
    package = _get_package(sb, project_id, package_id)

    data = available(project_id)
    by_cat = {c["material_category_id"]: c for c in data["categories"]}
    for cat in data["categories"]:
        for f in cat["files"]:
            f["prior"] = False
            f["prior_status"] = None
            f["available"] = True

    for it in _prior_items(sb, package_id):
        cid = it.get("material_category_id")
        cat = by_cat.get(cid)
        if cat is None:
            # The category has no materials on this project any more (renamed,
            # emptied, or the file was sent under Uncategorized). Rebuild the
            # bucket from the item's own snapshot so the file still has a home.
            cat = {
                "material_category_id": cid,
                "name": it.get("category_label") or UNCATEGORIZED_LABEL,
                "is_general": False,
                "sort_order": None,
                "material_count": 0,
                "files": [],
            }
            by_cat[cid] = cat
            data["categories"].append(cat)

        key = _item_key(it)
        existing = next((f for f in cat["files"] if key and f["key"] == key), None)
        if existing:
            existing["prior"] = True
            existing["prior_status"] = it["approval_status"]
            continue
        cat["files"].append(
            {
                "key": key,
                "source": it["source"],
                "filename": it["filename"],
                "size_bytes": it.get("size_bytes"),
                "origin": PRIOR_ORIGIN,
                "description": None,
                "pm_material_id": it.get("pm_material_id"),
                "prior": True,
                "prior_status": it["approval_status"],
                # No key ⇒ the source row is gone ⇒ nothing to re-send.
                "available": key is not None,
            }
        )

    return {
        "package": {
            "id": package["id"],
            "number": package["number"],
            "message": package.get("message"),
            "recipients": package.get("recipients") or [],
            "cc_recipients": package.get("cc_recipients") or [],
            "approval_status": package.get("approval_status"),
        },
        "categories": data["categories"],
    }


# ── Create + send ────────────────────────────────────────────────────────────


def _next_number(sb, project_id: str) -> int:
    """max+1 per project, mirroring _next_rfi_number. The unique(project_id,
    number) index is the real guard against a concurrent double-send."""
    rows = (
        sb.table("submittal_packages")
        .select("number")
        .eq("project_id", project_id)
        .order("number", desc=True)
        .limit(1)
        .execute()
    ).data or []
    return (rows[0]["number"] if rows else 0) + 1


def _fetch_bytes(entry: dict) -> bytes:
    """Download one selected file. Which table holds the path depends on the
    key's source — resolved here so the caller works in entries, not keys."""
    return storage.download_file(entry["storage_path"])


def _resolve_paths(sb, entries: list[dict]) -> None:
    """Fill `storage_path` on each selected entry, in one query per source.

    `available` deliberately does not return storage paths (they'd be handed to
    the browser for no reason), so the send path re-resolves them here from the
    key it already validated against the project's available index.
    """
    by_source: dict[str, list[dict]] = {}
    for e in entries:
        by_source.setdefault(e["source"], []).append(e)

    for source, rows in by_source.items():
        ids = [e["key"].split(":", 1)[1] for e in rows]
        if source == "vendor_reply":
            table, id_col, path_col = "ingested_email_attachments", "id", "storage_path"
        elif source == "bank":
            table, id_col, path_col = "submittal_files", "id", "file_path"
        else:
            table, id_col, path_col = "pm_documents", "id", "storage_path"
        found = {
            r[id_col]: r.get(path_col)
            for r in (sb.table(table).select(f"{id_col}, {path_col}").in_(id_col, ids).execute()).data
            or []
        }
        for e in rows:
            e["storage_path"] = found.get(e["key"].split(":", 1)[1])


def _item_row(package_id: str, entry: dict) -> dict:
    """One submittal_package_items row. Sets exactly the pointer column that
    matches `source` — the pairing the migration documents but deliberately does
    not CHECK (a CHECK would block ON DELETE SET NULL on the source row)."""
    raw_id = entry["key"].split(":", 1)[1]
    row = {
        "package_id": package_id,
        "material_category_id": entry.get("material_category_id"),
        "category_label": entry.get("category_label") or UNCATEGORIZED_LABEL,
        "pm_material_id": entry.get("pm_material_id"),
        "source": entry["source"],
        "filename": entry["filename"],
        "storage_path": entry.get("storage_path"),
        "size_bytes": entry.get("size_bytes"),
    }
    if entry["source"] == "vendor_reply":
        row["attachment_id"] = raw_id
    elif entry["source"] == "bank":
        row["submittal_file_id"] = raw_id
    else:
        row["document_id"] = raw_id
    return row


def build_subject(project: dict, number: int, supersedes: int | None = None) -> str:
    """`<Proj#> - <Name> - Submittal 004 (Resubmittal of 003)`.

    The resubmittal marker rides in the SUBJECT, not just the body, because it is
    what a GC scanning their inbox sees — and because the package it answers is
    the thing they need to reconcile it against.
    """
    tail = f" (Resubmittal of {str(supersedes).zfill(3)})" if supersedes else ""
    return (
        f"{project.get('number') or 'TBD'} - {project['name']} - "
        f"Submittal {str(number).zfill(3)}{tail}"
    )


def build_body(
    project: dict,
    number: int,
    message: str | None,
    link: str | None,
    supersedes: int | None = None,
) -> str:
    proj = f"{project.get('number') or ''} {project.get('name') or ''}".strip()
    files_line = (
        f"The submittals are available here, as one combined PDF per category: {link}\n\n"
        if link
        else "The submittals are attached as one combined PDF per category.\n\n"
    )
    note = f"{message.strip()}\n\n" if (message or "").strip() else ""
    if supersedes:
        opening = (
            f"Please find attached Submittal {str(number).zfill(3)} for {proj}, "
            f"resubmitted in response to your review of Submittal "
            f"{str(supersedes).zfill(3)}.\n\n"
        )
    else:
        opening = (
            f"Please find attached Submittal {str(number).zfill(3)} for {proj}, "
            "submitted for your review and approval.\n\n"
        )
    return (
        "Hello,\n\n"
        f"{opening}"
        f"{note}"
        f"{files_line}"
        "The attached transmittal lists each item — please mark it up and return "
        "it with your response, or reply to this email with your approval status.\n\n"
        "Thank you,\n"
        f"{SIGNOFF}"
    )


def _category_groups(entries: list[dict]) -> list[dict]:
    """Selected files bucketed by the category they were picked under, in the
    order the sender's groups arrived (which is the modal's display order)."""
    groups: dict[str | None, dict] = {}
    for e in entries:
        cid = e.get("material_category_id")
        g = groups.setdefault(
            cid,
            {
                "material_category_id": cid,
                "label": e.get("category_label") or UNCATEGORIZED_LABEL,
                "entries": [],
            },
        )
        g["entries"].append(e)
    return list(groups.values())


def _build_category_documents(
    project: dict, number: int, entries: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Build ONE combined PDF per category and return (documents, loose_files).

    Each document is a G3 cover page naming the category, followed by that
    category's submittals appended in the order they were selected. Files that
    can't be normalized to PDF (a .zip of shop drawings, a password-protected cut
    sheet, an office file the converter refused) are NOT dropped and do NOT fail
    the send — they come back in `loose_files` to be attached separately, and the
    cover page lists them as such so the GC knows the category has more to it than
    the pages in front of them.

    A cover-page render failure IS fatal (ConversionError propagates): an
    unlabelled merge is worse than no merge, and the transmittal render just
    ahead of this has already proven the converter is up.
    """
    documents: list[dict] = []
    loose: list[dict] = []

    for group in _category_groups(entries):
        parts: list[bytes] = []
        listed: list[dict] = []
        for e in group["entries"]:
            content = _fetch_bytes(e)
            try:
                parts.append(pdf_combine.to_pdf(content, e["filename"]))
                merged = True
            except pdf_combine.UnmergeableFile as exc:
                logger.info("Submittal file kept as a separate attachment — %s", exc)
                loose.append({"filename": e["filename"], "content": content})
                merged = False
            listed.append(
                {
                    "filename": e["filename"],
                    "description": e.get("description"),
                    "merged": merged,
                }
            )

        cover = submittal_pdf.render_category_cover_pdf(
            project, number=number, category_name=group["label"], files=listed
        )
        documents.append(
            {
                "material_category_id": group["material_category_id"],
                "category_label": group["label"],
                "filename": submittal_pdf.category_pdf_filename(project, group["label"]),
                "content": pdf_combine.merge([cover, *parts]),
                "merged_count": len(parts),
            }
        )

    return documents, loose


def _prepare_delivery(
    project: dict, sender: str, documents: list[dict], loose: list[dict], package_id: str
) -> tuple[list[dict], str | None]:
    """Decide how the category PDFs (and any file that couldn't join one) reach
    the GC. Past the inline limit, upload them once to the sender's OneDrive and
    return a single anonymous link instead — a submittal package is exactly the
    shape of email that blows past attachment limits (mirrors
    submittal_sending._prepare_shared_files).

    The anonymous "anyone with the link" URL is minted on the upload FOLDER, so
    the folder MUST be unique per package: a project-level folder would let any
    one package's recipient read every other package's files (different GCs,
    different verdicts) for the life of the link. Namespacing by package_id keeps
    each link scoped to exactly the files that package sent."""
    files = [{"filename": d["filename"], "content": d["content"]} for d in documents] + loose
    if not files:
        return [], None
    total = sum(len(f["content"]) for f in files)
    if total <= get_settings().rfq_drawings_inline_limit_mb * 1024 * 1024:
        return files, None
    number = _safe_component(str(project.get("number") or project["id"]))
    folder = f"BDR/{number}/submittals-for-approval/{_safe_component(package_id)}"
    # Category names are unique, but two loose files can share a filename (two
    # vendors' "cutsheet.pdf"); prefix with the index so the OneDrive upload
    # doesn't silently overwrite.
    for i, f in enumerate(files, start=1):
        graph_email.drive_upload(
            f"{folder}/{i:03d}-{_safe_component(f['filename'])}", f["content"], sender=sender
        )
    link = graph_email.drive_create_link(
        graph_email.drive_get_item_id(folder, sender=sender), sender=sender
    )
    return [], link


def create_and_send(
    project_id: str,
    body: dict,
    user_id: str,
    supersedes_package_id: str | None = None,
) -> dict:
    """Build a submittal approval package and email it to the selected GC
    contacts as ONE message (To + CC). `body` is a SubmittalApprovalIn dump.

    `supersedes_package_id` makes this a RESUBMITTAL of an earlier package: the
    new package gets its own number, transmittal, email thread and verdicts, and
    points back at the one it answers. The original is left untouched — its
    verdicts are the historical record of what the GC said the first time, and
    overwriting them would erase why the resubmittal exists.

    Raises ValueError (→ 400) for a bad config, an empty selection, or any id
    that doesn't belong to this project. Unlike the vendor-side batch there is no
    partial success to report: one email either goes or it doesn't, and a failure
    is recorded on the package with send_status='failed' rather than raised, so
    the attempt stays visible in the log.
    """
    from app.core.supabase_client import get_supabase

    settings = get_settings()
    sender = (settings.submittal_sender or settings.email_ingest_mailbox or "").strip()
    if not sender:
        raise ValueError(
            "Configure SUBMITTAL_SENDER (or EMAIL_INGEST_MAILBOX) before sending "
            "submittal packages — the GC's approval reply must have a mailbox to "
            "return to."
        )

    sb = get_supabase()
    project = (
        sb.table("projects")
        .select("id, number, name")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    # The customer GC is PM data, not project data — it lands on pm_details at
    # activation (0057) or when the bid winner carries over (0069). A project
    # with no pm_details row simply has no customer yet, which the check below
    # reports the same way an unset column would.
    pm_details = (
        sb.table("pm_details").select("customer_gc_id").eq("project_id", project_id).execute()
    ).data or []

    # ── Validate the file selection against what this project actually has ───
    # Indexed BY CATEGORY, not by key alone: one file legitimately appears under
    # several categories (a shared cut sheet reached from materials in each), and
    # the item must be filed under the category the sender picked it in, not
    # whichever bucket happened to be built last.
    index: dict[tuple[str | None, str], dict] = {}
    for cat in available(project_id)["categories"]:
        for f in cat["files"]:
            index[(cat["material_category_id"], f["key"])] = {
                **f,
                "material_category_id": cat["material_category_id"],
                "category_label": cat["name"],
            }

    # A resubmittal may re-send a file that has since fallen out of `available`
    # — its material link was changed, or its bank link removed — so the package
    # being answered contributes its own files as a second valid source. Safe
    # because _get_package scoped that package to THIS project: everything in it
    # is a file this project already sent. `available` wins on collision (its
    # entry carries the live description and origin).
    supersedes_number: int | None = None
    if supersedes_package_id:
        prior = _get_package(sb, project_id, supersedes_package_id)
        supersedes_number = prior["number"]
        for pair, entry in _prior_entries(sb, supersedes_package_id).items():
            index.setdefault(pair, entry)

    groups = body.get("groups") or []
    entries: list[dict] = []
    seen_keys: set[str] = set()
    for g in groups:
        cat_id = g.get("material_category_id")
        for key in g.get("file_keys") or []:
            # Global dedup: the same bytes are attached once even if the sender
            # ticked them under two categories. First group wins.
            if key in seen_keys:
                continue
            entry = index.get((cat_id, key))
            # An unknown pairing is fabricated, from another project, or from a
            # different category — all deliberately indistinguishable here.
            if not entry:
                raise ValueError(f"Submittal file not found in this project: {key}")
            seen_keys.add(key)
            entries.append(dict(entry))
    if not entries:
        raise ValueError("Select at least one submittal file to send")
    if len(entries) > MAX_FILES_PER_PACKAGE:
        raise ValueError(
            f"A package can hold at most {MAX_FILES_PER_PACKAGE} files "
            f"({len(entries)} selected) — split it into several packages."
        )

    # ── Resolve recipients: GC contacts of THIS project's customer GC ────────
    gc_id = pm_details[0].get("customer_gc_id") if pm_details else None
    if not gc_id:
        raise ValueError(
            "This project has no customer (general contractor) set — set one "
            "before sending submittals for approval."
        )
    to_ids = list(dict.fromkeys(body.get("recipient_contact_ids") or []))
    cc_ids = [c for c in dict.fromkeys(body.get("cc_contact_ids") or []) if c not in to_ids]
    if not to_ids:
        raise ValueError("Select at least one recipient")

    contacts = {
        c["id"]: c
        for c in (
            sb.table("gc_contacts")
            .select("id, name, email")
            .eq("gc_id", gc_id)
            .in_("id", to_ids + cc_ids)
            .execute()
        ).data
        or []
    }

    def _people(ids: list[str], *, require_email: bool) -> list[dict]:
        out = []
        for cid in ids:
            c = contacts.get(cid)
            # Scoped to gc_id above, so "not this GC's contact" reads as missing.
            if not c:
                raise ValueError(f"Contact not found for this project's customer: {cid}")
            if not (c.get("email") or "").strip():
                if require_email:
                    raise ValueError(f"{c['name']} has no email address on file")
                continue
            out.append({"contact_id": c["id"], "name": c["name"], "email": c["email"].strip()})
        return out

    recipients = _people(to_ids, require_email=True)
    cc_recipients = _people(cc_ids, require_email=False)

    # ── Persist the package + items before sending ──────────────────────────
    # Row first, send second: a package whose email fails must still be visible
    # in the log as a failed attempt (the vendor side takes the same order).
    number = _next_number(sb, project_id)
    subject = build_subject(project, number, supersedes_number)
    package = (
        sb.table("submittal_packages")
        .insert(
            {
                "project_id": project_id,
                "number": number,
                "gc_id": gc_id,
                "recipients": recipients,
                "cc_recipients": cc_recipients,
                "subject": subject,
                "message": (body.get("message") or "").strip() or None,
                "send_status": "pending",
                "supersedes_package_id": supersedes_package_id,
                "created_by": user_id,
            }
        )
        .execute()
    ).data[0]
    package_id = package["id"]

    _resolve_paths(sb, entries)
    missing = [e for e in entries if not e.get("storage_path")]
    if missing:
        # The row exists, so mark it failed rather than leaving it 'pending'.
        _fail(sb, package_id, f"File is no longer available: {missing[0]['filename']}")
        raise ValueError(f"File is no longer available: {missing[0]['filename']}")

    sb.table("submittal_package_items").insert(
        [_item_row(package_id, e) for e in entries]
    ).execute()

    # ── Render, attach, send ────────────────────────────────────────────────
    grouped: dict[str, list[dict]] = {}
    for e in entries:
        grouped.setdefault(e["category_label"], []).append(
            {"filename": e["filename"], "description": e.get("description")}
        )
    pdf_groups = list(grouped.items())

    try:
        pdf_bytes = submittal_pdf.render_package_pdf(
            project,
            number=number,
            groups=pdf_groups,
            message=body.get("message"),
            recipients=recipients,
            cc_recipients=cc_recipients,
            supersedes_number=supersedes_number,
        )
    except ConversionError as exc:
        logger.warning("Submittal package PDF render failed for %s: %s", project_id, exc)
        _fail(sb, package_id, f"Could not render the submittal transmittal — retry. ({exc})")
        raise ValueError(
            f"Could not render the submittal transmittal — retry. ({exc})"
        ) from exc

    pdf_name = submittal_pdf.package_pdf_filename(project, number)
    pdf_doc_id = _archive_pdf(
        sb, project_id, pdf_name, pdf_bytes, f"Submittal transmittal {str(number).zfill(3)}", user_id
    )

    # ── One combined PDF per category ───────────────────────────────────────
    try:
        documents, loose = _build_category_documents(project, number, entries)
    except ConversionError as exc:
        logger.warning("Submittal category PDF render failed for %s: %s", project_id, exc)
        _fail(sb, package_id, f"Could not build the category submittal PDFs — retry. ({exc})")
        raise ValueError(
            f"Could not build the category submittal PDFs — retry. ({exc})"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — a storage/merge failure is a send failure
        logger.exception("Submittal category PDF build failed for %s", project_id)
        _fail(sb, package_id, str(exc))
        return _result(package_id, number, "failed", None, str(exc))

    # Archived under the exact name the GC receives, so "what did we send them"
    # is answered by the Documents hub without cross-referencing anything.
    for d in documents:
        _archive_pdf(
            sb,
            project_id,
            d["filename"],
            d["content"],
            f"Requested submittals {str(number).zfill(3)} — {d['category_label']}",
            user_id,
        )

    try:
        files, link = _prepare_delivery(project, sender, documents, loose, package_id)
    except Exception as exc:  # noqa: BLE001 — a storage/OneDrive failure is a send failure
        logger.exception("Submittal package attachment prep failed for %s", project_id)
        _fail(sb, package_id, str(exc))
        return _result(package_id, number, "failed", None, str(exc))

    email_body = build_body(project, number, body.get("message"), link, supersedes_number)
    to_addrs = [r["email"] for r in recipients]
    cc_addrs = [r["email"] for r in cc_recipients]

    try:
        draft = graph_email.create_draft(
            to_addrs,
            subject,
            email_branding.render_vendor_email(email_body, subtitle="SUBMITTAL APPROVAL"),
            html=True,
            sender=sender,
            cc=cc_addrs or None,
        )
        graph_email.add_attachment(
            draft["id"],
            email_branding.LOGO_FILENAME,
            email_branding.logo_bytes(),
            "image/jpeg",
            content_id=email_branding.LOGO_CONTENT_ID,
            sender=sender,
        )
        graph_email.add_attachment(
            draft["id"], pdf_name, pdf_bytes, "application/pdf", sender=sender
        )
        for f in files:
            graph_email.add_attachment(
                draft["id"], f["filename"], f["content"], _content_type(f["filename"]), sender=sender
            )
        graph_email.send_draft(draft["id"], sender=sender)
    except Exception as exc:  # noqa: BLE001 — recorded on the package, not raised
        logger.exception("Submittal package send failed for %s", project_id)
        _fail(sb, package_id, str(exc))
        return _result(package_id, number, "failed", None, str(exc))

    log = (
        sb.table("email_log")
        .insert(
            {
                "to_addrs": ", ".join(to_addrs + cc_addrs),
                "subject": subject,
                "body": email_body,
                "status": "sent",
                "graph_message_id": draft.get("id"),
                "project_id": project_id,
                "sent_by": user_id,
            }
        )
        .execute()
    ).data[0]

    sb.table("submittal_packages").update(
        {
            "send_status": "sent",
            "body": email_body,
            "files_delivery": "onedrive_link" if link else "attached",
            "graph_message_id": draft.get("id"),
            "conversation_id": draft.get("conversationId"),
            "internet_message_id": draft.get("internetMessageId"),
            "email_log_id": log["id"],
            "pdf_doc_id": pdf_doc_id,
            "sent_at": "now()",
            "sent_by": user_id,
        }
    ).eq("id", package_id).execute()

    audit(
        user_id,
        "submittal_approval.send",
        "submittal_package",
        package_id,
        {
            "number": number,
            "supersedes": supersedes_number,
            "files": len(entries),
            "categories": len(pdf_groups),
            "category_pdfs": len(documents),
            # Anything here is a file the GC got loose because it wouldn't merge
            # — a non-zero count is the signal to look at what vendors are sending.
            "loose_files": len(loose),
            "to": len(recipients),
            "cc": len(cc_recipients),
            "files_delivery": "onedrive_link" if link else "attached",
            "conversation_id": draft.get("conversationId"),
        },
    )
    return _result(
        package_id,
        number,
        "sent",
        "onedrive_link" if link else "attached",
        None,
        len(entries),
        len(documents),
    )


def _result(
    package_id: str,
    number: int,
    status_: str,
    delivery: str | None,
    error: str | None,
    file_count: int = 0,
    category_count: int = 0,
) -> dict:
    return {
        "package_id": package_id,
        "number": number,
        "send_status": status_,
        "files_delivery": delivery,
        "file_count": file_count,
        # How many combined PDFs the GC received — one per category.
        "category_count": category_count,
        "error": error,
    }


def _fail(sb, package_id: str, error: str) -> None:
    """Mark a package failed. Best-effort: never mask the original failure."""
    try:
        sb.table("submittal_packages").update(
            {"send_status": "failed", "error": error[:2000]}
        ).eq("id", package_id).execute()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record failed submittal package %s", package_id)


def _archive_pdf(
    sb, project_id: str, filename: str, content: bytes, note: str, user_id: str
) -> str | None:
    """File one sent PDF — the transmittal, or a category's combined submittals —
    into the Documents hub (Submittals folder) as the record of exactly what went
    out. Best-effort: an archive failure must never fail the send it records.

    The note is what distinguishes these from the STAGED_NOTE_PREFIX uploads
    `available` offers back as selectable files; keep them from colliding or the
    next package will offer this one's output as an input.
    """
    try:
        path = storage.build_object_path(project_id, "pm/submittal", filename)
        storage.upload_file(path, content, "application/pdf")
        return (
            sb.table("pm_documents")
            .insert(
                {
                    "project_id": project_id,
                    "category": "submittal",
                    "storage_path": path,
                    "filename": filename,
                    "mime_type": "application/pdf",
                    "size_bytes": len(content),
                    "note": note,
                    "uploaded_by": user_id,
                }
            )
            .execute()
        ).data[0]["id"]
    except Exception:  # noqa: BLE001
        logger.exception("Failed to archive submittal transmittal for %s", project_id)
        return None
