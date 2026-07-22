"""Submittal Bank — a company-GLOBAL, searchable library of submittal PDFs.

Not project-scoped: one reusable library across General Materials, Low Voltage
and Switchgear (lighting & generators are vendor-provided and excluded).
Findability without exact names comes from a trigger-maintained `search_text`
(name + manufacturer + size + color + category + AI/manual aliases) that the
`search_submittals` pg_trgm RPC ranks over (migration 0072).

Files are MANY-TO-MANY with materials: one PDF can cover many materials (a
"group", so a shared cut-sheet is stored once) and one material can carry many
vendors' PDFs. Deleting a material cascades its links; a file with no remaining
links is treated as an orphan and its storage object is cleaned up.

Reads → any internal role (`require_internal`, admits the read-only accountant).
Writes → writer roles (`require_writer`, excludes accountant + external
estimator). The service-role client bypasses RLS, so these deps are the only
authorization boundary.
"""

import logging
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_internal, require_writer
from app.core.ratelimit import ai_rate_limit, upload_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    SubmittalFileUpdate,
    SubmittalGroupIn,
    SubmittalIn,
    SubmittalUpdate,
)
from app.services import openai_text, storage
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/submittals", tags=["submittals"])

_CATEGORIES = ("general_material", "low_voltage", "switchgear")
_MAX_ALIASES = 30


def _sanitize_query(q: str) -> str:
    """Strip LIKE/PostgREST metacharacters and cap length (mirrors emails.py) so
    a stray `%`/`_` in the query can't act as a wildcard in the RPC's ilike."""
    return re.sub(r"[,()%_*\\]", " ", q[:200]).strip()


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


def _parse_ids(raw: str | None) -> list[str]:
    # dict.fromkeys dedupes while preserving order — a duplicated id would
    # otherwise blow up the link upsert's ON CONFLICT with a cardinality error.
    # Only well-formed UUIDs survive: a malformed id would otherwise reach
    # `.in_("id", …)` and make Postgres raise an invalid-uuid error AFTER the
    # file object + row are stored, orphaning them behind a CORS-less 500.
    out: list[str] = []
    for p in dict.fromkeys(p.strip() for p in (raw or "").split(",") if p.strip()):
        try:
            uuid.UUID(p)
        except ValueError:
            continue
        out.append(p)
    return out


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read the upload into memory, aborting past `max_bytes` (single-request OOM
    defence). Mirrors files._read_capped."""
    limit_mb = max_bytes // (1024 * 1024)
    if upload.size is not None and upload.size > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is too large (limit {limit_mb} MB)."
        )
    buf = bytearray()
    while chunk := await upload.read(1024 * 1024):
        buf += chunk
        if len(buf) > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is too large (limit {limit_mb} MB)."
            )
    return bytes(buf)


# Chunk size for `.in_(...)` filters: PostgREST/Kong reject an over-long request
# URL (~29 KB for 748 UUIDs → a 400 that surfaces as a CORS-less 500). Each UUID
# is ~37 chars, so 200 ids ≈ 8 KB — comfortably under the gateway limit.
_IN_FILTER_CHUNK = 200


def _attach_files(rows: list[dict]) -> list[dict]:
    """Attach each material's linked file rows as a `files` list. The link lookup
    is chunked so the whole material set (hundreds of ids) can't build a request
    URL longer than the PostgREST gateway accepts."""
    ids = [r["id"] for r in rows]
    if not ids:
        return rows
    sb = get_supabase()
    links: list[dict] = []
    for i in range(0, len(ids), _IN_FILTER_CHUNK):
        chunk = ids[i : i + _IN_FILTER_CHUNK]
        links += (
            sb.table("submittal_material_files")
            .select("material_id, submittal_files(*)")
            .in_("material_id", chunk)
            .execute()
        ).data or []
    by_material: dict[str, list] = {}
    for link in links:
        f = link.get("submittal_files")
        if f:
            by_material.setdefault(link["material_id"], []).append(f)
    for r in rows:
        r["files"] = by_material.get(r["id"], [])
    return rows


def _get_material(material_id: str) -> dict:
    row = (
        get_supabase()
        .table("submittal_materials")
        .select("*")
        .eq("id", material_id)
        .limit(1)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submittal not found")
    return row[0]


def _cleanup_orphan_files(file_ids: list[str]) -> None:
    """Delete file rows (and their storage objects) that no longer link to any
    material. Best-effort — a failed storage delete is logged, not fatal."""
    sb = get_supabase()
    for fid in set(file_ids):
        still_linked = (
            sb.table("submittal_material_files")
            .select("material_id")
            .eq("file_id", fid)
            .limit(1)
            .execute()
        ).data
        if still_linked:
            continue
        frow = (
            sb.table("submittal_files").select("file_path").eq("id", fid).limit(1).execute()
        ).data
        sb.table("submittal_files").delete().eq("id", fid).execute()
        path = frow[0].get("file_path") if frow else None
        if path:
            try:
                storage.delete_file(path)
            except Exception:  # noqa: BLE001 — storage cleanup is best-effort
                logger.exception("failed to delete orphaned submittal file object %s", fid)


# ── Materials: list / search ─────────────────────────────────────────────────


@router.get("")
def list_submittals(
    q: str | None = None,
    category: str | None = None,
    made_in_usa: bool | None = None,
    has_file: bool | None = None,
    _: CurrentUser = Depends(require_internal),
):
    """List (or fuzzy-search) bank materials. `q` runs the pg_trgm RPC; otherwise
    a plain category-scoped list ordered by name. `made_in_usa`/`has_file` narrow
    the result set."""
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    sb = get_supabase()
    term = _sanitize_query(q) if q else ""
    if term:
        rows = sb.rpc("search_submittals", {"q": term, "cat": category}).execute().data or []
    else:
        query = sb.table("submittal_materials").select("*")
        if category is not None:
            query = query.eq("category", category)
        rows = query.order("name").execute().data or []
    rows = _attach_files(rows)
    if made_in_usa is not None:
        rows = [r for r in rows if r.get("made_in_usa") == made_in_usa]
    if has_file is not None:
        rows = [r for r in rows if bool(r.get("files")) == has_file]
    return rows


# ── Materials: create ────────────────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(ai_rate_limit)])
def create_submittal(body: SubmittalIn, user: CurrentUser = Depends(require_writer)):
    aliases = body.aliases
    if aliases is None:
        aliases = (
            openai_text.alt_material_names(body.name, body.category, body.manufacturer)
            if body.generate_aliases
            else []
        )
    payload = body.model_dump(mode="json", exclude={"aliases", "generate_aliases"})
    payload["aliases"] = _clean_aliases(aliases)
    payload["created_by"] = user.id
    created = get_supabase().table("submittal_materials").insert(payload).execute().data[0]
    audit(
        user.id,
        "submittal.create",
        "submittal_material",
        created["id"],
        {"name": body.name[:200], "category": body.category},
    )
    created["files"] = []
    return created


@router.post("/group", status_code=status.HTTP_201_CREATED, dependencies=[Depends(ai_rate_limit)])
def create_submittal_group(body: SubmittalGroupIn, user: CurrentUser = Depends(require_writer)):
    """Create several (size, color) materials sharing a name/manufacturer at once.
    Aliases are generated once from the group name and applied to every row; the
    client uploads one PDF afterward and links it to all returned ids."""
    aliases = _clean_aliases(
        openai_text.alt_material_names(body.name, body.category, body.manufacturer)
        if body.generate_aliases
        else []
    )
    rows = [
        {
            "category": body.category,
            "name": v.name or body.name,
            "size": v.size,
            "color": v.color,
            "made_in_usa": v.made_in_usa if v.made_in_usa is not None else body.made_in_usa,
            "manufacturer": body.manufacturer,
            "notes": body.notes,
            "aliases": aliases,
            "created_by": user.id,
        }
        for v in body.variants
    ]
    created = get_supabase().table("submittal_materials").insert(rows).execute().data or []
    audit(
        user.id,
        "submittal.create_group",
        "submittal_material",
        None,
        {"name": body.name[:200], "category": body.category, "count": len(created)},
    )
    for c in created:
        c["files"] = []
    return created


# ── Facets (for the size/color/vendor combo-selects) ─────────────────────────


@router.get("/facets")
def submittal_facets(category: str | None = None, _: CurrentUser = Depends(require_internal)):
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    sb = get_supabase()
    mq = sb.table("submittal_materials").select("size, color, manufacturer")
    if category is not None:
        mq = mq.eq("category", category)
    mats = mq.execute().data or []
    vendor_rows = sb.table("submittal_files").select("vendor").execute().data or []

    def distinct(values) -> list[str]:
        seen: dict[str, str] = {}
        for v in values:
            v = (v or "").strip()
            if v and v.lower() not in seen:
                seen[v.lower()] = v
        return sorted(seen.values(), key=str.lower)

    return {
        "sizes": distinct(m.get("size") for m in mats),
        "colors": distinct(m.get("color") for m in mats),
        "manufacturers": distinct(m.get("manufacturer") for m in mats),
        "vendors": distinct(r.get("vendor") for r in vendor_rows),
    }


# ── Files: list / upload / update / delete / preview ─────────────────────────


@router.get("/files")
def list_submittal_files(_: CurrentUser = Depends(require_internal)):
    """All bank files with the ids of the materials each is linked to — powers the
    detail modal's 'link an existing PDF' picker (sharing a sheet across a group)."""
    files = (
        get_supabase()
        .table("submittal_files")
        .select("*, submittal_material_files(material_id)")
        .order("created_at", desc=True)
        .execute()
    ).data or []
    for f in files:
        links = f.pop("submittal_material_files", None) or []
        f["material_ids"] = [link["material_id"] for link in links]
    return files


@router.post("/files", status_code=status.HTTP_201_CREATED, dependencies=[Depends(upload_rate_limit)])
async def upload_submittal_file(
    vendor: str | None = Form(None),
    title: str | None = Form(None),
    notes: str | None = Form(None),
    material_ids: str | None = Form(None),  # comma-separated material ids to link now
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_writer),
):
    """Upload one PDF into the bank and optionally link it to materials. PDF-only
    is enforced by extension AND magic bytes (extension alone is spoofable)."""
    filename = file.filename or "submittal.pdf"
    content = await _read_capped(file, get_settings().upload_max_bytes)
    if not filename.lower().endswith(".pdf") or content[:5] != b"%PDF-":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Submittals must be PDF files"
        )
    path = storage.build_submittal_object_path(filename)
    await run_in_threadpool(storage.upload_file, path, content, "application/pdf")

    insert = get_supabase().table("submittal_files").insert(
        {
            "vendor": (vendor or "").strip() or None,
            "title": (title or "").strip() or None,
            "notes": (notes or "").strip() or None,
            "file_path": path,
            "file_name": filename,
            "size_bytes": len(content),
            "uploaded_by": user.id,
        }
    )
    try:
        frow = (await run_in_threadpool(insert.execute)).data[0]
    except Exception:
        # The object is already stored; drop it so a failed insert leaves no orphan.
        await run_in_threadpool(storage.delete_file, path)
        raise

    # Link only to materials that actually exist (a bad/stale id must not FK-crash
    # after the object + row are already committed). Guard the link so any residual
    # failure cleans up the just-uploaded orphan and returns a clean 400.
    ids = _parse_ids(material_ids)
    linked_ids: list[str] = []
    if ids:
        existing = (
            await run_in_threadpool(
                get_supabase().table("submittal_materials").select("id").in_("id", ids).execute
            )
        ).data or []
        linked_ids = [r["id"] for r in existing]
        if linked_ids:
            links = [{"material_id": mid, "file_id": frow["id"], "created_by": user.id} for mid in linked_ids]
            try:
                await run_in_threadpool(
                    get_supabase()
                    .table("submittal_material_files")
                    .upsert(links, ignore_duplicates=True)
                    .execute
                )
            except Exception:
                await run_in_threadpool(storage.delete_file, path)
                await run_in_threadpool(
                    get_supabase().table("submittal_files").delete().eq("id", frow["id"]).execute
                )
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "Could not link the file to the given materials"
                )
    await run_in_threadpool(
        audit,
        user.id,
        "submittal_file.upload",
        "submittal_file",
        frow["id"],
        {"file_name": filename, "materials": len(linked_ids)},
    )
    frow["material_ids"] = linked_ids
    return frow


@router.patch("/files/{file_id}")
def update_submittal_file(
    file_id: str, body: SubmittalFileUpdate, user: CurrentUser = Depends(require_writer)
):
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    updated = (
        get_supabase().table("submittal_files").update(patch).eq("id", file_id).execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    audit(user.id, "submittal_file.update", "submittal_file", file_id, {"fields": sorted(patch)})
    return updated[0]


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_submittal_file(file_id: str, user: CurrentUser = Depends(require_writer)):
    sb = get_supabase()
    frow = sb.table("submittal_files").select("file_path").eq("id", file_id).limit(1).execute().data
    if not frow:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    sb.table("submittal_files").delete().eq("id", file_id).execute()  # cascades links
    path = frow[0].get("file_path")
    if path:
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001 — best-effort storage cleanup
            logger.exception("failed to delete submittal file object %s", file_id)
    audit(user.id, "submittal_file.delete", "submittal_file", file_id, None)


@router.get("/files/{file_id}/preview-url")
def submittal_file_preview_url(file_id: str, _: CurrentUser = Depends(require_internal)):
    frow = (
        get_supabase().table("submittal_files").select("file_path").eq("id", file_id).limit(1).execute()
    ).data
    if not frow:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return {"url": storage.signed_url(frow[0]["file_path"])}


# ── Materials: update / delete / aliases / linking ───────────────────────────


@router.patch("/{material_id}", dependencies=[Depends(ai_rate_limit)])
def update_submittal(
    material_id: str, body: SubmittalUpdate, user: CurrentUser = Depends(require_writer)
):
    existing = _get_material(material_id)
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if "aliases" in patch:
        patch["aliases"] = _clean_aliases(patch["aliases"])
    elif "name" in patch:
        # Name changed without explicit aliases → refresh the AI aliases to match,
        # but ONLY overwrite when the model actually returned some. Otherwise
        # (OpenAI unconfigured or a failed call → []) keep the existing aliases so
        # a plain rename can't silently wipe hand-curated search terms.
        cat = patch.get("category", existing["category"])
        manu = patch.get("manufacturer", existing.get("manufacturer"))
        regenerated = _clean_aliases(openai_text.alt_material_names(patch["name"], cat, manu))
        if regenerated:
            patch["aliases"] = regenerated
    updated = (
        get_supabase().table("submittal_materials").update(patch).eq("id", material_id).execute()
    ).data
    audit(
        user.id, "submittal.update", "submittal_material", material_id, {"fields": sorted(patch)}
    )
    return _attach_files(updated)[0]


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_submittal(material_id: str, user: CurrentUser = Depends(require_writer)):
    sb = get_supabase()
    link_file_ids = [
        link["file_id"]
        for link in (
            sb.table("submittal_material_files")
            .select("file_id")
            .eq("material_id", material_id)
            .execute()
        ).data
        or []
    ]
    deleted = sb.table("submittal_materials").delete().eq("id", material_id).execute().data
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submittal not found")
    _cleanup_orphan_files(link_file_ids)  # links cascaded; drop now-orphaned shared files
    audit(user.id, "submittal.delete", "submittal_material", material_id, None)


@router.post("/{material_id}/aliases/regenerate", dependencies=[Depends(ai_rate_limit)])
def regenerate_submittal_aliases(material_id: str, user: CurrentUser = Depends(require_writer)):
    m = _get_material(material_id)
    aliases = _clean_aliases(
        openai_text.alt_material_names(m["name"], m["category"], m.get("manufacturer"))
    )
    updated = (
        get_supabase()
        .table("submittal_materials")
        .update({"aliases": aliases})
        .eq("id", material_id)
        .execute()
    ).data
    audit(
        user.id,
        "submittal.regenerate_aliases",
        "submittal_material",
        material_id,
        {"count": len(aliases)},
    )
    return _attach_files(updated)[0]


@router.post("/{material_id}/files/{file_id}", status_code=status.HTTP_201_CREATED)
def link_submittal_file(
    material_id: str, file_id: str, user: CurrentUser = Depends(require_writer)
):
    """Link an existing file to a material — the 'share one PDF across a group' path."""
    sb = get_supabase()
    _get_material(material_id)
    if not sb.table("submittal_files").select("id").eq("id", file_id).limit(1).execute().data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    # ignore_duplicates → re-linking an existing pair is a no-op and preserves the
    # original created_by rather than overwriting it with the current user.
    sb.table("submittal_material_files").upsert(
        {"material_id": material_id, "file_id": file_id, "created_by": user.id},
        ignore_duplicates=True,
    ).execute()
    audit(user.id, "submittal_file.link", "submittal_material", material_id, {"file_id": file_id})
    return _attach_files([_get_material(material_id)])[0]


@router.delete("/{material_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_submittal_file(
    material_id: str, file_id: str, user: CurrentUser = Depends(require_writer)
):
    deleted = (
        get_supabase()
        .table("submittal_material_files")
        .delete()
        .eq("material_id", material_id)
        .eq("file_id", file_id)
        .execute()
    ).data
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    _cleanup_orphan_files([file_id])
    audit(user.id, "submittal_file.unlink", "submittal_material", material_id, {"file_id": file_id})
