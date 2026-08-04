"""Bundle a project's stored files into a single in-memory ZIP for download.

The archive groups files into one folder per category (`drawing/`, `estimate/`,
…). Filenames are sanitised to a safe basename (zip-slip defence) and de-duped
within their folder. A missing storage object is recorded in `MANIFEST.txt` and
skipped rather than failing the whole export.

This is the read-half of an export, modelled on `rfq_sending._load_files`
(which already loops `storage.download_file` over `project_files` rows).
"""

import io
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import IO

from app.services import storage

# Above this size the export archive spills from RAM to a temp file, bounding
# peak memory on a small instance no matter how large the export.
_SPOOL_MAX_MEMORY = 8 * 1024 * 1024

# Display/sort order for the category folders. Mirrors FilesPanel's order, plus
# `proposal` (a real file_category from migration 0024 that FilesPanel omits) so
# sent proposals get their own folder rather than the unranked fallback bucket.
_CATEGORY_ORDER = [
    "drawing",
    "specification",
    "addendum",
    "revision",
    "additional",
    "estimate",
    "boq",
    "markup",
    "marked_plans",
    "rfq_split",
    "quote",
    "proposal",
    "other",
]


def _category_rank(category: str | None) -> int:
    try:
        return _CATEGORY_ORDER.index(category)  # type: ignore[arg-type]
    except ValueError:
        return len(_CATEGORY_ORDER)


def _safe_name(filename: str | None) -> str:
    """Reduce a stored filename to a safe single path component (zip-slip safe).

    `storage.build_object_path` only ever replaced `/`, so a stored filename can
    still carry `\\`, `..`, drive letters or NULs — strip them all here, since
    the arcname is the only defence on the extracting side.
    """
    name = filename or "file"
    # Basename only — drop any directory the name might smuggle in.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"^[A-Za-z]:", "", name)        # leading drive letter
    name = name.replace("\x00", "").replace("..", "_")
    name = name.strip().strip(".")                # no leading/trailing dots/spaces
    return name or "file"


def _dedupe(taken: set[str], arcname: str) -> str:
    """Return `arcname`, or `name (2).ext` etc. if it's already used.

    Collisions are tracked case-insensitively: most extraction targets (Windows,
    default macOS) are case-insensitive, so "Plan.pdf" and "plan.pdf" would
    overwrite each other on extract even though they differ as Python strings.
    """
    if arcname.casefold() not in taken:
        taken.add(arcname.casefold())
        return arcname
    base, dot, ext = arcname.rpartition(".")
    stem, suffix = (base, f".{ext}") if dot else (arcname, "")
    i = 2
    while True:
        candidate = f"{stem} ({i}){suffix}"
        if candidate.casefold() not in taken:
            taken.add(candidate.casefold())
            return candidate
        i += 1


def _render_manifest(manifest: list[dict]) -> str:
    ok = [m for m in manifest if m["status"] == "ok"]
    missing = [m for m in manifest if m["status"] == "missing"]
    lines = ["BDR project file export", ""]
    lines.append(f"{len(ok)} file(s) exported.")
    for m in ok:
        lines.append(f"  {m['file']}  ({m['bytes']:,} bytes)")
    if missing:
        lines += ["", f"{len(missing)} file(s) could not be retrieved and were skipped:"]
        for m in missing:
            lines.append(f"  {m['file']}  — {m.get('error', 'unavailable')}")
    return "\n".join(lines) + "\n"


def _write_entries(zf: zipfile.ZipFile, rows: list[dict]) -> list[dict]:
    """Download each row's object and write it into `zf`; returns the manifest.

    A file is dropped from memory each iteration (only one object is resident at
    a time), so peak RAM is bounded by the largest single file — the archive
    itself is written straight into `zf`'s backing store (see the spooled path).
    """
    ordered = sorted(
        rows,
        key=lambda r: (_category_rank(r.get("category")), (r.get("filename") or "").lower()),
    )
    taken: set[str] = set()
    manifest: list[dict] = []
    for r in ordered:
        category = r.get("category") or "other"
        arcname = _dedupe(taken, f"{category}/{_safe_name(r.get('filename'))}")
        try:
            content = storage.download_file(r["storage_path"])
        except Exception as exc:  # noqa: BLE001 — missing object: record, skip, continue
            manifest.append({"file": arcname, "status": "missing", "error": str(exc)})
            continue
        zf.writestr(arcname, content)
        manifest.append({"file": arcname, "status": "ok", "bytes": len(content)})
    zf.writestr("MANIFEST.txt", _render_manifest(manifest))
    return manifest


def build_export_zip(rows: list[dict]) -> tuple[bytes, list[dict]]:
    """Build the ZIP fully in memory. Retained for unit tests; the HTTP endpoint
    uses `build_export_spooled` to avoid holding the whole archive in RAM.

    Returns `(zip_bytes, manifest)`.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _write_entries(zf, rows)
    return buf.getvalue(), manifest


def build_export_spooled(rows: list[dict]) -> tuple[IO[bytes], list[dict], int]:
    """Build the ZIP into a spooled temp file (RAM up to `_SPOOL_MAX_MEMORY`,
    then disk) and return `(open_file_at_pos0, manifest, size_bytes)`.

    Synchronous — call via `run_in_threadpool`. The caller MUST close the file
    (e.g. after streaming it out). This keeps peak memory to ~one file plus the
    spool threshold even for a near-cap export.
    """
    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _write_entries(zf, rows)
    size = spool.tell()
    spool.seek(0)
    return spool, manifest, size


def export_filename(project: dict, suffix: str = "files") -> str:
    """A download filename like `24-118_files_20260624.zip` (or `_documents_…`
    for the unified PM hub — pass `suffix="documents"`)."""
    label = str(project.get("number") or project.get("name") or "project")
    label = re.sub(r'[\\/:*?"<>|]+', "_", label).strip() or "project"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{label}_{suffix}_{stamp}.zip"


# ── Folder-based export (unified PM documents hub) ────────────────────────────
# The bidding export above groups by file *category*; the PM hub groups by
# business *folder* (Plans, Quotes, Certified Payroll, …) drawn from three
# different tables. Rows here are pre-shaped by the caller
# (app.services.pm_folders): each carries a display `folder` label and a
# `folder_rank` for ordering. The download/dedupe/manifest mechanics are shared.


def _write_folder_entries(zf: zipfile.ZipFile, rows: list[dict]) -> list[dict]:
    """Download each row's object into `zf` under `{folder}/{filename}`; returns
    the manifest. Peak RAM stays ~one file (see `_write_entries`)."""
    ordered = sorted(
        rows,
        key=lambda r: (r.get("folder_rank", 1_000), (r.get("filename") or "").lower()),
    )
    taken: set[str] = set()
    manifest: list[dict] = []
    for r in ordered:
        folder = _safe_name(r.get("folder") or "Other")
        arcname = _dedupe(taken, f"{folder}/{_safe_name(r.get('filename'))}")
        try:
            content = storage.download_file(r["storage_path"])
        except Exception as exc:  # noqa: BLE001 — missing object: record, skip, continue
            manifest.append({"file": arcname, "status": "missing", "error": str(exc)})
            continue
        zf.writestr(arcname, content)
        manifest.append({"file": arcname, "status": "ok", "bytes": len(content)})
    zf.writestr("MANIFEST.txt", _render_manifest(manifest))
    return manifest


def build_folder_export_spooled(rows: list[dict]) -> tuple[IO[bytes], list[dict], int]:
    """Folder-grouped variant of `build_export_spooled` for the PM documents hub.

    Each row needs: `folder` (display label), `folder_rank` (int), `filename`,
    `storage_path`. Synchronous — call via `run_in_threadpool`; the caller MUST
    close the returned file.
    """
    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _write_folder_entries(zf, rows)
    size = spool.tell()
    spool.seek(0)
    return spool, manifest, size
