"""Project ↔ Submittal Bank links (migration 0074).

Backs the "Other submittals — fill from the bank" section of a project's
Submittals page. For the materials NOT requested from a vendor (General Material
and self-performed / uncategorized items) the team either PULLS the matching bank
submittals (fuzzy `search_submittals`, 0072 — all of them, since one product
routinely needs several) or UPLOADS a PDF for one the bank doesn't cover. An uploaded PDF is archived into the Documents hub (pm_documents,
category 'submittal') and can later be pushed into the global bank.

A `pm_material_submittals` row is one submittal covering one pm_material, of a
single source: 'bank' (submittal_material_id set) or 'uploaded' (document_id set;
hub key `pm:<document_id>`). All lookups are scoped to the project, so an id from
another project is indistinguishable from a missing one.
"""

import logging
import re

from fastapi import HTTPException, status

from app.core.supabase_client import get_supabase
from app.services import openai_text, storage
from app.services.notifications import audit

logger = logging.getLogger(__name__)

# How many top fuzzy matches to consider per material. The RPC returns rows
# already ordered by word_similarity, so the best matches come first.
_PULL_CANDIDATES = 50
# A pull links EVERY file-bearing match, not just the best one — a product
# legitimately carries several submittals (multiple vendors, a group cut-sheet
# plus a spec sheet). This caps how many one material can accumulate so a vague
# description that matches half the bank can't bury the page; the overflow is
# logged rather than silently dropped.
_MAX_LINKS_PER_MATERIAL = 10
_MAX_ALIASES = 30


def _sanitize_query(q: str | None) -> str:
    """Strip LIKE/PostgREST metacharacters and cap length (mirrors
    submittals._sanitize_query) so a stray `%`/`_` can't act as a wildcard."""
    return re.sub(r"[,()%_*\\]", " ", (q or "")[:200]).strip()


def _clean_aliases(aliases: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases or []:
        v = (a or "").strip()[:100]
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
        if len(out) >= _MAX_ALIASES:
            break
    return out


# ── Resolve link rows for display / preview ──────────────────────────────────


def _resolve_links(rows: list[dict]) -> list[dict]:
    """Attach display info to raw link rows: a bank link carries the matched
    material's name + its files ({file_id, file_name}, previewed via the bank's
    preview-url route); an uploaded link carries the pm_documents filename + the
    hub key `pm:<document_id>` (previewed via the Documents hub)."""
    if not rows:
        return []
    sb = get_supabase()
    bank_ids = [r["submittal_material_id"] for r in rows if r["source"] == "bank" and r.get("submittal_material_id")]
    doc_ids = [r["document_id"] for r in rows if r["source"] == "uploaded" and r.get("document_id")]

    names: dict[str, str] = {}
    files_by_mat: dict[str, list[dict]] = {}
    if bank_ids:
        for m in (sb.table("submittal_materials").select("id, name").in_("id", bank_ids).execute().data or []):
            names[m["id"]] = m["name"]
        links = (
            sb.table("submittal_material_files")
            .select("material_id, submittal_files(id, file_name)")
            .in_("material_id", bank_ids)
            .execute()
        ).data or []
        for link in links:
            f = link.get("submittal_files")
            if f:
                files_by_mat.setdefault(link["material_id"], []).append(
                    {"file_id": f["id"], "file_name": f["file_name"]}
                )

    docs: dict[str, str] = {}
    if doc_ids:
        for d in (sb.table("pm_documents").select("id, filename").in_("id", doc_ids).execute().data or []):
            docs[d["id"]] = d["filename"]

    out: list[dict] = []
    for r in rows:
        if r["source"] == "bank":
            mid = r["submittal_material_id"]
            out.append(
                {
                    "id": r["id"],
                    "pm_material_id": r["pm_material_id"],
                    "source": "bank",
                    "name": names.get(mid),
                    "submittal_material_id": mid,
                    "files": files_by_mat.get(mid, []),
                    "created_at": r["created_at"],
                }
            )
        else:
            did = r["document_id"]
            out.append(
                {
                    "id": r["id"],
                    "pm_material_id": r["pm_material_id"],
                    "source": "uploaded",
                    "name": docs.get(did),
                    "document_id": did,
                    "document_key": f"pm:{did}",
                    "in_bank": r.get("bank_material_id") is not None,
                    "created_at": r["created_at"],
                }
            )
    return out


def _get_link(project_id: str, link_id: str) -> dict:
    """Fetch a link scoped to the project, or 404. Scoping means a link id from
    another project is indistinguishable from one that doesn't exist."""
    row = (
        get_supabase()
        .table("pm_material_submittals")
        .select("*")
        .eq("project_id", project_id)
        .eq("id", link_id)
        .limit(1)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submittal link not found")
    return row[0]


# ── List ─────────────────────────────────────────────────────────────────────


def list_links(project_id: str) -> dict:
    """Every link for the project, grouped by pm_material_id and resolved for
    display. The frontend intersects this with its bank-eligible material set."""
    rows = (
        get_supabase()
        .table("pm_material_submittals")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    ).data or []
    grouped: dict[str, list[dict]] = {}
    for link in _resolve_links(rows):
        grouped.setdefault(link["pm_material_id"], []).append(link)
    return {"items": [{"pm_material_id": k, "links": v} for k, v in grouped.items()]}


# ── Pull from bank ───────────────────────────────────────────────────────────


def pull(project_id: str, material_ids: list[str], user_id: str) -> list[dict]:
    """For each project material, fuzzy-search the bank by its description and
    link EVERY match that HAS a file (a match with nothing to preview is no use)
    — one product often needs several submittals, so this is deliberately not a
    single best-match pick.

    Distinctness is judged by FILE, not by bank material: files are M:N with
    materials, so a shared cut-sheet is reachable from every size/color it
    covers, and linking each of those matches would attach the same PDF over and
    over. A match is linked only if it brings a file the material doesn't
    already have — from an earlier pull or from an earlier match in this one.

    Matches keep the RPC's relevance order and stop at `_MAX_LINKS_PER_MATERIAL`
    per material (existing links count toward it). Re-pulling after the bank
    grows adds only what's new. Returns the created links, resolved."""
    sb = get_supabase()
    mats = (
        sb.table("pm_materials")
        .select("id, description")
        .eq("project_id", project_id)
        .in_("id", material_ids)
        .execute()
    ).data or []
    if not mats:
        return []
    existing = (
        sb.table("pm_material_submittals")
        .select("pm_material_id, submittal_material_id")
        .eq("project_id", project_id)
        .in_("pm_material_id", [m["id"] for m in mats])
        .execute()
    ).data or []
    # Per material: how many submittals it already carries (any source, for the
    # cap) and which bank ones (whose files it therefore already has).
    link_count: dict[str, int] = {}
    linked_bank: dict[str, set[str]] = {}
    for r in existing:
        mid = r["pm_material_id"]
        link_count[mid] = link_count.get(mid, 0) + 1
        if r.get("submittal_material_id"):
            linked_bank.setdefault(mid, set()).add(r["submittal_material_id"])
    already_files = _files_by_material([sid for ids in linked_bank.values() for sid in ids])

    created_ids: list[str] = []
    for m in mats:
        room = _MAX_LINKS_PER_MATERIAL - link_count.get(m["id"], 0)
        if room <= 0:
            continue
        q = _sanitize_query(m.get("description"))
        if not q:
            continue
        rows = (sb.rpc("search_submittals", {"q": q, "cat": None}).execute().data or [])[:_PULL_CANDIDATES]
        if not rows:
            continue
        files_by_mat = _files_by_material([r["id"] for r in rows])
        linked = linked_bank.get(m["id"], set())
        # Files this material can already preview — anything a candidate adds on
        # top of these is genuinely new; a candidate that adds nothing is the
        # same PDF reached through a sibling bank item.
        have: set[str] = set()
        for sid in linked:
            have |= already_files.get(sid, set())
        matches = []
        for r in rows:
            if r["id"] in linked:
                continue
            new_files = files_by_mat.get(r["id"], set()) - have
            if not new_files:
                continue
            matches.append(r)
            have |= new_files
        if not matches:
            continue
        if len(matches) > room:
            logger.info(
                "pm_submittal pull: material %s capped at %d of %d bank matches",
                m["id"],
                room,
                len(matches),
            )
            matches = matches[:room]
        inserted = (
            sb.table("pm_material_submittals")
            .insert(
                [
                    {
                        "project_id": project_id,
                        "pm_material_id": m["id"],
                        "source": "bank",
                        "submittal_material_id": match["id"],
                        "created_by": user_id,
                    }
                    for match in matches
                ]
            )
            .execute()
        ).data or []
        created_ids.extend(row["id"] for row in inserted)

    if created_ids:
        audit(
            user_id,
            "pm_submittal.pull",
            "project",
            project_id,
            {"linked": len(created_ids), "materials": len(mats)},
        )
        new_rows = (
            sb.table("pm_material_submittals").select("*").in_("id", created_ids).execute()
        ).data or []
        return _resolve_links(new_rows)
    return []


def _files_by_material(material_ids: list[str]) -> dict[str, set[str]]:
    """{bank material id → its file ids}. Materials with no file are absent, so
    an empty lookup doubles as "nothing to preview here"."""
    ids = list(dict.fromkeys(material_ids))
    if not ids:
        return {}
    links = (
        get_supabase()
        .table("submittal_material_files")
        .select("material_id, file_id")
        .in_("material_id", ids)
        .execute()
    ).data or []
    out: dict[str, set[str]] = {}
    for link in links:
        out.setdefault(link["material_id"], set()).add(link["file_id"])
    return out


# ── Upload a PDF for a missing submittal ─────────────────────────────────────


def upload(project_id: str, pm_material_id: str, filename: str, content: bytes, user_id: str) -> dict:
    """Archive a user-provided submittal PDF into the Documents hub (category
    'submittal') and link it to the material. The material must belong to the
    project. Cleans up the storage object / doc row if a later step fails."""
    sb = get_supabase()
    mat = (
        sb.table("pm_materials")
        .select("id, description")
        .eq("project_id", project_id)
        .eq("id", pm_material_id)
        .limit(1)
        .execute()
    ).data
    if not mat:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Material not found")

    path = storage.build_object_path(project_id, "pm/submittal", filename)
    storage.upload_file(path, content, "application/pdf")
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
                    "note": f"Submittal — {(mat[0].get('description') or '')[:180]}",
                    "uploaded_by": user_id,
                }
            )
            .execute()
        ).data[0]
    except Exception:
        _best_effort_delete(path)
        raise

    try:
        link = (
            sb.table("pm_material_submittals")
            .insert(
                {
                    "project_id": project_id,
                    "pm_material_id": pm_material_id,
                    "source": "uploaded",
                    "document_id": doc["id"],
                    "created_by": user_id,
                }
            )
            .execute()
        ).data[0]
    except Exception:
        sb.table("pm_documents").delete().eq("id", doc["id"]).execute()
        _best_effort_delete(path)
        raise

    audit(user_id, "pm_submittal.upload", "project", project_id, {"material_id": pm_material_id, "document_id": doc["id"]})
    return _resolve_links([link])[0]


# ── Push an uploaded PDF into the global bank ─────────────────────────────────


def add_to_bank(project_id: str, link_id: str, body: dict, user_id: str) -> dict:
    """Create a bank material from an uploaded link and copy its PDF into the
    bank, then record bank_material_id on the link. Metadata is optional — an
    unset name defaults to the material's description."""
    sb = get_supabase()
    link = _get_link(project_id, link_id)
    if link["source"] != "uploaded" or not link.get("document_id"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only an uploaded submittal can be added to the bank")
    if link.get("bank_material_id"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This submittal is already in the bank")

    doc = (
        sb.table("pm_documents").select("storage_path, filename").eq("id", link["document_id"]).limit(1).execute()
    ).data
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded document not found")
    mat = (
        sb.table("pm_materials").select("description").eq("id", link["pm_material_id"]).limit(1).execute()
    ).data
    default_name = (mat[0].get("description") if mat else None) or doc[0]["filename"]

    category = body.get("category") or "general_material"
    name = (body.get("name") or default_name or "").strip()[:300] or default_name
    aliases = (
        _clean_aliases(openai_text.alt_material_names(name, category, body.get("manufacturer")))
        if body.get("generate_aliases", True)
        else []
    )
    material = (
        sb.table("submittal_materials")
        .insert(
            {
                "category": category,
                "name": name,
                "size": body.get("size"),
                "color": body.get("color"),
                "made_in_usa": body.get("made_in_usa"),
                "manufacturer": body.get("manufacturer"),
                "notes": body.get("notes"),
                "aliases": aliases,
                "created_by": user_id,
            }
        )
        .execute()
    ).data[0]

    # Copy the archived PDF into the bank's global prefix and link it. On any
    # failure past the material insert, roll the material back so the bank never
    # holds a nameless, fileless orphan the user didn't intend.
    try:
        content = storage.download_file(doc[0]["storage_path"])
        bank_path = storage.build_submittal_object_path(doc[0]["filename"])
        storage.upload_file(bank_path, content, "application/pdf")
        try:
            frow = (
                sb.table("submittal_files")
                .insert(
                    {
                        "file_path": bank_path,
                        "file_name": doc[0]["filename"],
                        "size_bytes": len(content),
                        "uploaded_by": user_id,
                    }
                )
                .execute()
            ).data[0]
        except Exception:
            _best_effort_delete(bank_path)
            raise
        sb.table("submittal_material_files").insert(
            {"material_id": material["id"], "file_id": frow["id"], "created_by": user_id}
        ).execute()
    except Exception:
        sb.table("submittal_materials").delete().eq("id", material["id"]).execute()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not add the submittal to the bank")

    updated = (
        sb.table("pm_material_submittals")
        .update({"bank_material_id": material["id"]})
        .eq("id", link_id)
        .execute()
    ).data[0]
    audit(user_id, "submittal.create", "submittal_material", material["id"], {"name": name[:200], "category": category, "from": "project"})
    audit(user_id, "submittal_file.upload", "submittal_file", frow["id"], {"file_name": doc[0]["filename"], "materials": 1})
    return _resolve_links([updated])[0]


# ── Delete / unlink ──────────────────────────────────────────────────────────


def delete_link(project_id: str, link_id: str, user_id: str) -> None:
    """Remove a link. A 'bank' link is a pure unlink (the global bank is
    untouched). An 'uploaded' link deletes its Documents-hub row — which cascades
    the link — and best-effort removes the storage object; any bank copy that was
    already pushed stays (once global, always global)."""
    sb = get_supabase()
    link = _get_link(project_id, link_id)
    if link["source"] == "uploaded" and link.get("document_id"):
        doc = (
            sb.table("pm_documents").select("storage_path").eq("id", link["document_id"]).limit(1).execute()
        ).data
        sb.table("pm_documents").delete().eq("id", link["document_id"]).execute()  # cascades the link
        if doc and doc[0].get("storage_path"):
            _best_effort_delete(doc[0]["storage_path"])
    else:
        sb.table("pm_material_submittals").delete().eq("id", link_id).execute()
    audit(user_id, "pm_submittal.unlink", "project", project_id, {"link_id": link_id, "source": link["source"]})


def _best_effort_delete(path: str) -> None:
    try:
        storage.delete_file(path)
    except Exception:  # noqa: BLE001 — storage cleanup is best-effort
        logger.exception("failed to delete submittal object %s", path)
