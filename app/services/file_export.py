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
import unicodedata
import zipfile
from typing import IO

from app.core.file_categories import category_rank as _category_rank
from app.services import storage
from app.services.export_names import fit_path, flat_name, short_name

# Above this size the export archive spills from RAM to a temp file, bounding
# peak memory on a small instance no matter how large the export.
_SPOOL_MAX_MEMORY = 8 * 1024 * 1024

# Display/sort order for the category folders is CATEGORY_DISPLAY_ORDER, the one
# shared order (app/core/file_categories.py). It was duplicated here until 0132
# added eight categories and the copy fell behind; `category_rank` is imported as
# `_category_rank` so the sort below reads unchanged. Folder names are the
# short labels below, kept brief so deep save locations stay under the Windows
# 260-character path limit (see app/services/export_names.py).

EXPORT_FOLDERS: dict[str, str] = {
    "rfp": "RFP",
    "drawing": "Gen Dwgs",
    "civil_drawing": "Civil Dwgs",
    "structural_drawing": "Struct Dwgs",
    "architectural_drawing": "Arch Dwgs",
    "mechanical_drawing": "Mech Dwgs",
    "plumbing_drawing": "Plumb Dwgs",
    "electrical_drawing": "Elec Dwgs",
    "fire_protection_drawing": "FP Dwgs",
    "low_voltage_drawing": "LV Dwgs",
    "specification": "Spec",
    "addendum": "Add",
    "revision": "Rev",
    "additional": "Additional",
    "estimate": "Estimate",
    "boq": "BOQ",
    "markup": "Markup",
    "marked_plans": "Marked Plans",
    "estimator_additional": "Est Additional",
    "rfq_split": "BOM Split",
    "quote": "Quotes",
    "proposal": "Proposals",
    "other": "Other",
}


def _safe_name(filename: str | None) -> str:
    """Reduce a stored filename to a safe single path component (zip-slip safe).

    `storage.build_object_path` only ever replaced `/`, so a stored filename can
    still carry `\\`, `..`, drive letters or NULs - strip them all here, since
    the arcname is the only defence on the extracting side.
    """
    name = filename or "file"
    # Basename only - drop any directory the name might smuggle in.
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


def _arcname(taken: set[str], folders: list[str], filename: str | None, flat: bool) -> str:
    """The archive path for one file: every component shortened (job numbers,
    common terms), sanitised (zip-slip), length-capped, then de-duped. `flat`
    puts everything at the root with the folders as a filename prefix."""
    parts = [_safe_name(short_name(f)) for f in folders if f]
    name = _safe_name(short_name(filename or "file"))
    return _dedupe(taken, flat_name(parts, name) if flat else fit_path(parts, name))


def _render_manifest(
    manifest: list[dict],
    *,
    title: str = "BDR project file export",
    notes: list[str] | None = None,
) -> str:
    """Human-readable inventory of the archive.

    `notes` carries anything the caller wants recorded that never became an
    entry (e.g. a splitter source file that failed and produced no sections),
    so the archive explains its own gaps.
    """
    ok = [m for m in manifest if m["status"] == "ok"]
    missing = [m for m in manifest if m["status"] == "missing"]
    lines = [title, ""]
    lines.append(f"{len(ok)} file(s) exported.")
    for m in ok:
        lines.append(f"  {m['file']}  ({m['bytes']:,} bytes)")
    if missing:
        lines += ["", f"{len(missing)} file(s) could not be retrieved and were skipped:"]
        for m in missing:
            lines.append(f"  {m['file']}  - {m.get('error', 'unavailable')}")
    if notes:
        lines += ["", "Notes:"]
        for note in notes:
            lines.append(f"  {note}")
    return "\n".join(lines) + "\n"


def _write_entries(zf: zipfile.ZipFile, rows: list[dict], *, flat: bool = False) -> list[dict]:
    """Download each row's object and write it into `zf`; returns the manifest.

    A file is dropped from memory each iteration (only one object is resident at
    a time), so peak RAM is bounded by the largest single file - the archive
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
        folder = EXPORT_FOLDERS.get(category, category)
        arcname = _arcname(taken, [folder], r.get("filename"), flat)
        try:
            content = storage.download_file(r["storage_path"])
        except Exception as exc:  # noqa: BLE001 - missing object: record, skip, continue
            manifest.append({"file": arcname, "status": "missing", "error": str(exc)})
            continue
        zf.writestr(arcname, content)
        manifest.append({"file": arcname, "status": "ok", "bytes": len(content)})
    zf.writestr("MANIFEST.txt", _render_manifest(manifest))
    return manifest


def build_export_zip(rows: list[dict], *, flat: bool = False) -> tuple[bytes, list[dict]]:
    """Build the ZIP fully in memory. Retained for unit tests; the HTTP endpoint
    uses `build_export_spooled` to avoid holding the whole archive in RAM.

    Returns `(zip_bytes, manifest)`.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _write_entries(zf, rows, flat=flat)
    return buf.getvalue(), manifest


def build_export_spooled(
    rows: list[dict], *, flat: bool = False
) -> tuple[IO[bytes], list[dict], int]:
    """Build the ZIP into a spooled temp file (RAM up to `_SPOOL_MAX_MEMORY`,
    then disk) and return `(open_file_at_pos0, manifest, size_bytes)`.

    Synchronous - call via `run_in_threadpool`. The caller MUST close the file
    (e.g. after streaming it out). This keeps peak memory to ~one file plus the
    spool threshold even for a near-cap export.
    """
    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _write_entries(zf, rows, flat=flat)
    size = spool.tell()
    spool.seek(0)
    return spool, manifest, size


def zip_filename(label: str, suffix: str) -> str:
    """`{label}_{suffix}.zip`, with the label stripped of the characters
    Windows refuses in a filename. No date stamp: Windows "Extract All" makes
    a folder named after the zip, so every character here lengthens every
    path inside it.

    The result is printable ASCII only: it lands in a Content-Disposition
    header, which Starlette encodes as latin-1 (an emoji or en dash in an
    uploaded filename would 500 the export) and where CR/LF must never
    appear. Accents fold to their base letter (NFKD); anything else that is
    not printable ASCII becomes `_`."""
    label = unicodedata.normalize("NFKD", label)
    label = "".join(c for c in label if not unicodedata.combining(c))
    label = re.sub(r"[\x00-\x1f\x7f]+", "", label)
    label = re.sub(r"[^\x20-\x7e]+", "_", label)
    label = re.sub(r'[\\/:*?"<>|]+', "_", label).strip() or "export"
    return f"{label}_{suffix}.zip"


def export_filename(project: dict, suffix: str = "files") -> str:
    """A download filename like `24-118_files_20260624.zip` (or `_documents_…`
    for the unified PM hub - pass `suffix="documents"`)."""
    return zip_filename(str(project.get("number") or project.get("name") or "project"), suffix)


# ── Folder-based export (unified PM documents hub) ────────────────────────────
# The bidding export above groups by file *category*; the PM hub groups by
# business *folder* (Plans, Quotes, Certified Payroll, …) drawn from three
# different tables. Rows here are pre-shaped by the caller
# (app.services.pm_folders): each carries a display `folder` label and a
# `folder_rank` for ordering. The download/dedupe/manifest mechanics are shared.


def _write_folder_entries(
    zf: zipfile.ZipFile, rows: list[dict], *, flat: bool = False
) -> list[dict]:
    """Download each row's object into `zf` under `{folder}/{filename}`; returns
    the manifest. Peak RAM stays ~one file (see `_write_entries`)."""
    ordered = sorted(
        rows,
        key=lambda r: (r.get("folder_rank", 1_000), (r.get("filename") or "").lower()),
    )
    taken: set[str] = set()
    manifest: list[dict] = []
    for r in ordered:
        arcname = _arcname(taken, [r.get("folder") or "Other"], r.get("filename"), flat)
        try:
            content = storage.download_file(r["storage_path"])
        except Exception as exc:  # noqa: BLE001 - missing object: record, skip, continue
            manifest.append({"file": arcname, "status": "missing", "error": str(exc)})
            continue
        zf.writestr(arcname, content)
        manifest.append({"file": arcname, "status": "ok", "bytes": len(content)})
    zf.writestr("MANIFEST.txt", _render_manifest(manifest))
    return manifest


def build_folder_export_spooled(
    rows: list[dict], *, flat: bool = False
) -> tuple[IO[bytes], list[dict], int]:
    """Folder-grouped variant of `build_export_spooled` for the PM documents hub.

    Each row needs: `folder` (display label), `folder_rank` (int), `filename`,
    `storage_path`. Synchronous - call via `run_in_threadpool`; the caller MUST
    close the returned file.
    """
    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _write_folder_entries(zf, rows, flat=flat)
    size = spool.tell()
    spool.seek(0)
    return spool, manifest, size


# ── Tree export (arbitrary nested folders) ────────────────────────────────────
# The two exports above are one level deep. The Bid File Splitter needs a real
# tree - one folder per source PDF, a category folder inside it, and a further
# label folder for a free-text "other" section - so rows here carry the folder
# path as a list of components instead of a single label. Caller order is
# preserved verbatim (the splitter already orders by source file, then segment),
# and every component is sanitised the same way a filename is: the arcname is
# the only zip-slip defence on the extracting side.


def _write_tree_entries(
    zf: zipfile.ZipFile,
    rows: list[dict],
    *,
    title: str,
    notes: list[str] | None,
    flat: bool = False,
) -> list[dict]:
    """Download each row's object into `zf` under `{folders…}/{filename}`.

    Each row needs: `folders` (list of path components, outermost first),
    `filename`, `storage_path`. Peak RAM stays ~one file (see `_write_entries`).
    """
    taken: set[str] = set()
    manifest: list[dict] = []
    for r in rows:
        arcname = _arcname(taken, list(r.get("folders") or []), r.get("filename"), flat)
        try:
            content = storage.download_file(r["storage_path"])
        except Exception as exc:  # noqa: BLE001 - missing object: record, skip, continue
            manifest.append({"file": arcname, "status": "missing", "error": str(exc)})
            continue
        zf.writestr(arcname, content)
        manifest.append({"file": arcname, "status": "ok", "bytes": len(content)})
    zf.writestr("MANIFEST.txt", _render_manifest(manifest, title=title, notes=notes))
    return manifest


def build_tree_export_spooled(
    rows: list[dict],
    *,
    title: str = "BDR export",
    notes: list[str] | None = None,
    flat: bool = False,
) -> tuple[IO[bytes], list[dict], int]:
    """Nested-folder variant of `build_export_spooled`.

    Synchronous - call via `run_in_threadpool`; the caller MUST close the
    returned file.
    """
    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _write_tree_entries(zf, rows, title=title, notes=notes, flat=flat)
    size = spool.tell()
    spool.seek(0)
    return spool, manifest, size
