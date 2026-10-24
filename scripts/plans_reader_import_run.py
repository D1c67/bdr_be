"""Create a DEV AI Estimating run from a stored run bundle, at status
`consolidating` (docs/AI_ESTIMATING_BUILD_SPEC.md WS4 step 0; S2).

Usage (from bdr_be; the dev database only, see the preflight below):

    grep '^SUPABASE_URL' .env    # must show the dev ref bpidntbyvoooqvaispup
    uv run python scripts/plans_reader_import_run.py \\
        --bundle /Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25 \\
        [--escalations on|off] [--recounts off|on] [--name "7154 import"] \\
        [--rows stored|v2|v1] [--consolidator tools|legacy] [--page-facts <facts.json>] \\
        [--backup /Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01] \\
        [--project-id <dev project uuid>] [--created-by <dev profile uuid>] \\
        [--facts on|off] [--queue-main] [--dry-run]

    # backfill the page facts of a run imported before --facts existed:
    uv run python scripts/plans_reader_import_run.py --facts-for-run <dev run uuid>

What it does, using ONLY the existing 0152 tables (plans_reader_runs, _files,
_documents, _groups, _outputs) and the project-files bucket:

1. Reads the bundle (run.json, files.json, documents.json, groups.json, the
   group outputs under groups/ and the category outputs under outputs/). A
   group output that is not stored (the roll-up, a category's single sub,
   the main group) is resolved from the category or main output it equals.
2. Finds every source PDF in the local production BACKUP (read only) by the
   file's `source_project_file_id` in data/project_files.json, never by
   folder, and uploads it to the new run's storage
   (`plans-reader/<run>/source/...`), so later stages can read pages. A
   splitter segment that is not the whole file is cut to its pages first.
3. Inserts the run at status `consolidating`, its files, documents (same
   created_at, so the D1..Dn numbering is unchanged), the reading groups
   (sub, roll-up and, with --escalations on, escalation) as done groups with
   their stored outputs, and the category outputs (version 1). Main, modify
   and chat groups are not imported: the run has no consolidation yet.
   --escalations off drops the escalation groups and recomputes the category
   outputs without their patches (plans_reader_runs._category_output), so
   prompt iterations see first-pass rows only. --recounts is recorded in the
   run's settings snapshot (`plans_reader_recount_enabled`); a v1 bundle has
   no recount groups.
   Rows (WS4 wave 3, wave 3 fix WB): --rows stored (the default when every
   category output in the bundle carries rows_version, i.e. a bundle of a
   live run that merged in code) imports the bundle's stored v2 category
   outputs VERBATIM, every stored version (outputs/category_<cat>_v<n>.json)
   at its own version number: the rows, row ids, row_seq, floors and
   merge_log the live run's main consolidation read, with only the run,
   file, document and group ids remapped to the new run. The recount
   groups (pass 3) are imported too, as done groups, so the patched rows'
   origin and each output's source_group_ids name groups of the new run
   and the stale repair (plans_reader_runs._write_outputs_and_queue_main)
   keeps the stored latest version instead of re-merging. Stored rows
   need --escalations on (they carry the escalation patches); the recount
   outcome table (plans_reader_recounts) is not imported. --rows v2 (the
   default for a bundle without stored v2 outputs) stores every category
   output as a v2 row set rebuilt from the imported groups with
   plans_reader_merge.consolidate_category (run-unique row ids, coverage,
   status, census floors when page facts are given), which is what the
   consolidator in mode tools and the final gates read; --rows v1 stores the
   bundle's v1 outputs as before. --page-facts points at a page facts JSON
   in the fixture layout (documents[].facts.pages, matched to the bundle's
   documents by name) so the v2 outputs carry census floors (ignored with
   stored rows, whose floors come from the bundle). --dry-run prints the
   rows mode that applies and why.

   Why the v2 rebuild is not the live merge: the live run merged each
   category once at reading -> consolidating with its census floors read
   from the page facts store (WS1, written by the facts job before the
   merge), and again after pass 3 with the recount outputs as whole-sheet
   patches over that stored prior (a new output version); the rebuild runs
   consolidate_category once over the sub, roll-up and escalation groups
   only, with no floors unless --page-facts is given (the facts store of
   the new run is written after the insert) and no recount groups. So the
   rebuild has no floors and no containment (7154 run 2: 0 floors against
   the live 11), applies the escalation replacements in the import's group
   order and without the floors that let a whole-sheet patch replace a tile
   subtotal (7154 run 2: C30 stays a tile_subtotal flagged needs_recount,
   the live row is a sheet_total), and has none of the recount patches (7154
   run 2: C16, C19 to C22, C42, C43, C45 and C46 keep their first-pass
   origin, notes and confidence; the live version 2 supersedes them from
   the recount groups). Row ids, item keys and quantities agree on 7154 run
   2, but nothing guarantees that in general, so a re-run from consolidating
   uses --rows stored.
   --consolidator tools|legacy records the intended consolidator mode in the
   snapshot (`plans_reader_consolidator_mode`); the backend's own setting
   decides what the main group runs, so the note says so.
4. Page facts (--facts on, the default): runs the $0 WS1 extraction on the
   uploaded bytes of every imported document and writes the result to the
   page facts store exactly as the `plans_reader_facts` job does
   (pagefacts.integration.extract_and_store: the (sha256, kind,
   extractor_version) cache rows, the word blobs, and sha256, facts_status
   and facts_doc on the document), drawing documents first, with the
   relabel never applied (the imported groups and outputs keep the
   bundle's categories). An imported run starts at consolidating, so it
   never passes the splitting gate that enqueues the facts job; without
   this step the derive measure finds no plan sheet and measures nothing.
   --page-facts stays an optional override for the v2 census floors only.
   --facts-for-run <run id> does this step alone for an existing dev run
   (its stored PDFs), for runs imported before it existed.
5. Prints the new run id (the last stdout line).

The main group: a run at `consolidating` with no main group is repaired by
plans_reader_runs.advance once it is stale (RECOVERY_AFTER_SECONDS, 600 s)
or by a poll of the run page: that queues the LIVE main consolidation on any
dev backend whose LLM queue is on. --queue-main queues it immediately. Run
this only inside an open eval sweep when a model call may follow (spec 0.4).

Never touches production: the preflight refuses any SUPABASE_URL that is not
the dev project, and the backup is only read. --dry-run prints the plan and
touches nothing.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import re
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEV_REF = "bpidntbyvoooqvaispup"
DEFAULT_BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
PDF_MIME = "application/pdf"
READING_KINDS = ("sub", "rollup", "escalation")
STORED_KINDS = READING_KINDS + ("recount",)  # --rows stored also imports the recount groups
ROWS_MODES = ("stored", "v2", "v1")
_CATEGORY_FILE = re.compile(r"^category_(.+)_v(\d+)$")
SKIPPED_KINDS = ("main", "modify", "chat")
_BASE_DROP = ("escalation_patches", "unresolved_flags", "source")


class ImportRefused(RuntimeError):
    """The bundle, the backup or the target cannot be imported as asked."""


# Bundle --------------------------------------------------------------------------


@dataclass
class Bundle:
    root: Path
    run: dict
    files: list[dict]
    documents: list[dict]
    groups: list[dict]
    category_outputs: dict[str, dict]
    main_output: dict | None
    group_outputs: dict[str, dict] = field(default_factory=dict)
    # Every stored version of each category output: {category: {version: output}}
    # (outputs/category_<cat>_v<n>.json; a live run that recounted has a v2).
    category_versions: dict[str, dict[int, dict]] = field(default_factory=dict)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def group_output_name(group: Mapping[str, Any]) -> str:
    """The bundle file name of a group's output (the weekend export layout)."""
    return f"{group.get('kind')}_{group.get('category') or 'none'}_{int(group.get('group_index') or 0)}_p{int(group.get('pass') or 1)}.json"


def _base(output: Mapping[str, Any]) -> dict:
    return {k: copy.deepcopy(v) for k, v in output.items() if k not in _BASE_DROP}


def load_bundle(root: Path | str) -> Bundle:
    root = Path(root)
    for name in ("run.json", "files.json", "documents.json", "groups.json"):
        if not (root / name).is_file():
            raise ImportRefused(f"bundle {root} has no {name}")
    run = _read_json(root / "run.json")
    files = _read_json(root / "files.json")
    documents = _read_json(root / "documents.json")
    groups = _read_json(root / "groups.json")
    cats: dict[str, dict] = {}
    versions: dict[str, dict[int, dict]] = {}
    for path in sorted((root / "outputs").glob("category_*_v*.json")):
        m = _CATEGORY_FILE.match(path.stem)
        if m is None:
            continue
        out = _read_json(path)
        cat = str((out.get("category") if isinstance(out, Mapping) else None) or m.group(1))
        versions.setdefault(cat, {})[int(m.group(2))] = out
        if int(m.group(2)) == 1:
            cats[cat] = out
    main_path = root / "outputs" / "main_none_v1.json"
    main = _read_json(main_path) if main_path.is_file() else None
    bundle = Bundle(root, run, files, documents, groups, cats, main,
                    category_versions={c: dict(sorted(v.items())) for c, v in versions.items()})
    subs_per_cat: dict[str, int] = {}
    for g in groups:
        if g.get("kind") == "sub":
            subs_per_cat[g.get("category")] = subs_per_cat.get(g.get("category"), 0) + 1
    for g in groups:
        gid = str(g.get("id"))
        if isinstance(g.get("output"), Mapping):
            bundle.group_outputs[gid] = g["output"]
            continue
        path = root / "groups" / group_output_name(g)
        if path.is_file():
            bundle.group_outputs[gid] = _read_json(path)
        elif g.get("kind") == "rollup" and g.get("category") in cats:
            bundle.group_outputs[gid] = _base(cats[g["category"]])
        elif g.get("kind") == "sub" and subs_per_cat.get(g.get("category")) == 1 and g.get("category") in cats:
            bundle.group_outputs[gid] = _base(cats[g["category"]])
        elif g.get("kind") == "main" and main is not None:
            bundle.group_outputs[gid] = main
    return bundle


# Backup sources ----------------------------------------------------------------------


@dataclass
class SourcePdf:
    file_id: str
    project_file_id: str
    path: Path
    filename: str
    category: str
    size_bytes: int | None


def locate_sources(bundle: Bundle, backup_root: Path | str) -> dict[str, SourcePdf]:
    """{bundle file id: SourcePdf} via the backup's data/project_files.json
    (category and storage path from the row, never from the folder)."""
    backup_root = Path(backup_root)
    index_path = backup_root / "data" / "project_files.json"
    if not index_path.is_file():
        raise ImportRefused(f"backup {backup_root} has no data/project_files.json")
    rows = _read_json(index_path)
    rows = rows.get("rows", []) if isinstance(rows, Mapping) else rows
    by_id = {str(r.get("id")): r for r in rows if isinstance(r, Mapping)}
    out: dict[str, SourcePdf] = {}
    missing: list[str] = []
    for f in bundle.files:
        pf_id = str(f.get("source_project_file_id") or "")
        row = by_id.get(pf_id)
        if row is None:
            # A live-run bundle (eval_run uploads from the backup under the
            # label map, so its files carry no project file id): the source
            # PDF is the bundle's own inputs/<file id>.pdf when present.
            local = bundle.root / "inputs" / f"{f.get('id')}.pdf"
            if local.is_file():
                doc = next((d for d in bundle.documents if str(d.get("file_id")) == str(f.get("id"))), None)
                out[str(f["id"])] = SourcePdf(
                    file_id=str(f["id"]), project_file_id="", path=local,
                    filename=str(f.get("filename") or local.name),
                    category=str((doc or {}).get("category") or ""),
                    size_bytes=f.get("size_bytes") or local.stat().st_size,
                )
                continue
            missing.append(f"{f.get('filename')} (project file {pf_id or 'none'}, no inputs/{f.get('id')}.pdf)")
            continue
        path = backup_root / "storage" / "project-files" / str(row.get("storage_path") or "")
        if not path.is_file():
            missing.append(f"{f.get('filename')} ({path})")
            continue
        out[str(f["id"])] = SourcePdf(
            file_id=str(f["id"]),
            project_file_id=pf_id,
            path=path,
            filename=str(row.get("filename") or f.get("filename") or path.name),
            category=str(row.get("category") or ""),
            size_bytes=row.get("size_bytes"),
        )
    if missing:
        raise ImportRefused("source PDFs not found in the backup: " + "; ".join(missing))
    return out


# Page facts (optional, for v2 census floors) --------------------------------------


def load_page_facts(path: Path | str, documents: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], dict]:
    """{(bundle document id, page): facts page} from a page facts JSON in the
    fixture layout (`documents[].facts.pages`, each with `page`), matched to
    the bundle's documents by `document_id` when present, else by name."""
    data = _read_json(Path(path))
    by_name = {str(d.get("name") or ""): str(d["id"]) for d in documents if isinstance(d, Mapping)}
    ids = {str(d["id"]) for d in documents if isinstance(d, Mapping)}
    out: dict[tuple[str, int], dict] = {}
    for entry in data.get("documents") or []:
        if not isinstance(entry, Mapping):
            continue
        facts = entry.get("facts") if isinstance(entry.get("facts"), Mapping) else {}
        doc_id = str(entry.get("document_id") or "")
        if doc_id not in ids:
            doc_id = by_name.get(str(facts.get("name") or entry.get("name") or ""), "")
        if not doc_id:
            continue
        for page in facts.get("pages") or []:
            if isinstance(page, Mapping) and page.get("page") is not None:
                out[(doc_id, int(page["page"]))] = dict(page)
    return out


def rebuild_v2_output(
    category: str,
    group_rows: list[dict],
    *,
    documents: list[dict],
    ledger: Mapping[str, Any] | None,
    words_by_page: Mapping[tuple[str, int], Any] | None = None,
    run_outputs: list[dict] | None = None,
) -> dict | None:
    """A category output v2 from the (already remapped) group rows with
    plans_reader_merge.consolidate_category (no roll-up call, $0).
    `run_outputs` are the outputs already rebuilt for the run: the merge
    continues their row id sequence so ids are run-unique (the live merge
    does the same through plans_reader_runs._stored_category_outputs)."""
    from app.services.plans_reader_merge import consolidate_category

    return consolidate_category(
        category, group_rows, words_by_page=words_by_page or None, ledger=ledger, documents=documents, llm_rollup=False,
        run_outputs=run_outputs,
    )


# Rows mode ---------------------------------------------------------------------------


def _sub_categories(bundle: Bundle) -> list[str]:
    return list(dict.fromkeys(
        str(g.get("category")) for g in bundle.groups if g.get("kind") == "sub" and g.get("status") == "done"
    ))


def stored_rows_problem(bundle: Bundle) -> str | None:
    """Why the bundle cannot be imported with --rows stored, or None when
    every category with a done sub group has stored outputs and every
    stored version carries rows_version (a live run's code merge)."""
    cats = _sub_categories(bundle)
    if not cats:
        return "the bundle has no done sub group"
    for cat in cats:
        versions = bundle.category_versions.get(cat) or {}
        if not versions:
            return f"{cat} has no stored category output"
        for version, out in versions.items():
            if not isinstance(out, Mapping) or not out.get("rows_version"):
                return f"{cat} output v{version} carries no rows_version (a v1 output)"
    return None


def resolve_rows_mode(bundle: Bundle, requested: int | str | None, *, escalations: bool = True) -> tuple[str, str]:
    """(mode, reason): mode is stored, v2 or v1. `requested` None (or
    "auto") picks stored when the bundle allows it and escalations are on,
    else v2; 1 and 2 are the v1 and v2 modes. An explicit stored on a
    bundle without stored v2 outputs, or with escalations off, is refused."""
    if requested in (None, "auto"):
        problem = stored_rows_problem(bundle)
        if problem is None and escalations:
            return "stored", "default: every category output in the bundle carries rows_version"
        why = problem or "escalations off (the stored rows carry the escalation patches)"
        return "v2", f"default: rebuilt from the groups ({why})"
    mode = {1: "v1", 2: "v2"}.get(requested, requested) if not isinstance(requested, bool) else None
    if mode not in ROWS_MODES:
        raise ImportRefused("rows must be stored, v2 or v1 (rows_version 1 or 2)")
    if mode == "stored":
        problem = stored_rows_problem(bundle)
        if problem is not None:
            raise ImportRefused(f"--rows stored: {problem}; use --rows v2")
        if not escalations:
            raise ImportRefused("--rows stored needs --escalations on (the stored rows carry the escalation patches); use --rows v2")
        return "stored", "requested: the bundle's stored v2 category outputs verbatim"
    return mode, f"requested: {mode}"


# Plan --------------------------------------------------------------------------------


@dataclass
class Upload:
    storage_path: str
    source: Path
    pages: list[int] | None = None  # 1-based source pages, in order; None = the whole file


@dataclass
class ImportPlan:
    run_id: str
    run_row: dict
    file_rows: list[dict]
    document_rows: list[dict]
    group_rows: list[dict]
    output_rows: list[dict]
    uploads: list[Upload]
    id_map: dict[str, str]
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.run_row["status"],
            "files": len(self.file_rows),
            "documents": len(self.document_rows),
            "groups": {k: sum(1 for g in self.group_rows if g["kind"] == k) for k in STORED_KINDS},
            "outputs": [o["category"] for o in self.output_rows],
            "output_versions": [f"{o['category']} v{o['version']}" for o in self.output_rows],
            "rows_version": self.run_row.get("settings_snapshot", {}).get("import", {}).get("rows_version"),
            "rows_mode": self.run_row.get("settings_snapshot", {}).get("import", {}).get("rows_mode"),
            "rows_mode_reason": self.run_row.get("settings_snapshot", {}).get("import", {}).get("rows_mode_reason"),
            "consolidator": self.run_row.get("settings_snapshot", {}).get("import", {}).get("consolidator"),
            "uploads": [{"path": u.storage_path, "source": str(u.source), "pages": u.pages} for u in self.uploads],
            "notes": self.notes,
        }


def remap_ids(obj: Any, id_map: Mapping[str, str]) -> Any:
    """A deep copy with every string equal to an old id replaced (ids are
    uuids, so a plain value match cannot hit unrelated text)."""
    if isinstance(obj, str):
        return id_map.get(obj, obj)
    if isinstance(obj, Mapping):
        return {k: remap_ids(v, id_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [remap_ids(v, id_map) for v in obj]
    return copy.deepcopy(obj)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _segment_pages(document: Mapping[str, Any], file_pages: int | None) -> list[int] | None:
    """Source-file pages of a document's stored PDF, or None when the
    document is the whole file."""
    meta = [m for m in document.get("page_meta") or [] if isinstance(m, Mapping)]
    pages = [int(m["page"]) for m in sorted(meta, key=lambda m: int(m.get("pdf_page") or m.get("page") or 0)) if m.get("page")]
    if not pages:
        start, end = int(document.get("page_start") or 1), int(document.get("page_end") or 1)
        pages = list(range(start, end + 1))
    if file_pages and pages == list(range(1, int(file_pages) + 1)):
        return None
    return pages


def plan_import(
    bundle: Bundle,
    sources: Mapping[str, SourcePdf],
    *,
    name: str | None = None,
    escalations: bool = True,
    recounts: bool = False,
    project_id: str | None = None,
    created_by: str | None = None,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    path_for: Callable[[str, str, str], str] | None = None,
    category_output: Callable[[str, list[dict]], tuple[dict | None, list[str]]] | None = None,
    now: Callable[[], str] = _now_iso,
    rows_version: int | str | None = None,
    consolidator: str | None = None,
    page_facts: Mapping[tuple[str, int], Any] | None = None,
) -> ImportPlan:
    """Pure: every row to insert and every object to upload, no I/O.
    `path_for(run_id, kind, filename)` mints storage paths
    (storage.build_plans_reader_path); `category_output(category, groups)`
    rebuilds a category output (plans_reader_runs._category_output).
    `rows_version` picks the rows mode (resolve_rows_mode): None (default)
    is stored when the bundle has stored v2 outputs and escalations are on,
    else 2; "stored" imports the bundle's stored v2 outputs verbatim (every
    version, ids remapped, recount groups imported), 2 stores v2 row sets
    rebuilt from the groups (`rebuild_v2_output`), 1 the bundle's v1
    outputs; `page_facts` keyed by the BUNDLE document ids gives the
    rebuilt v2 outputs their census floors; `consolidator` (tools or
    legacy) is recorded in the snapshot."""
    rows_mode, rows_reason = resolve_rows_mode(bundle, rows_version, escalations=escalations)
    if consolidator not in (None, "tools", "legacy"):
        raise ImportRefused("consolidator must be tools or legacy")
    if path_for is None:
        from app.services.storage import build_plans_reader_path as path_for  # noqa: N813
    if category_output is None:
        from app.services.plans_reader_runs import _category_output as category_output
    run_id = new_id()
    id_map: dict[str, str] = {str(bundle.run.get("id")): run_id}
    notes: list[str] = []

    kinds = STORED_KINDS if rows_mode == "stored" else READING_KINDS
    groups_in = [g for g in bundle.groups if g.get("kind") in kinds and g.get("status") == "done"]
    if not escalations:
        dropped = [g for g in groups_in if g.get("kind") == "escalation"]
        groups_in = [g for g in groups_in if g.get("kind") != "escalation"]
        if dropped:
            notes.append(f"escalations off: {len(dropped)} escalation groups not imported; category outputs rebuilt without patches")
    for g in groups_in:
        if str(g.get("id")) not in bundle.group_outputs:
            raise ImportRefused(f"group {g.get('id')} ({group_output_name(g)}) has no stored output in the bundle")
    skipped = [g for g in bundle.groups if g.get("kind") in SKIPPED_KINDS]
    if skipped:
        notes.append(f"not imported (the run starts at consolidating): {', '.join(sorted({str(g.get('kind')) for g in skipped}))}")

    used_docs = list(bundle.documents)
    used_file_ids = {str(d.get("file_id")) for d in used_docs}
    for f in bundle.files:
        id_map[str(f["id"])] = new_id()
    for d in used_docs:
        id_map[str(d["id"])] = new_id()
    for g in groups_in:
        id_map[str(g["id"])] = new_id()

    uploads: list[Upload] = []
    file_rows: list[dict] = []
    file_paths: dict[str, str] = {}
    for f in bundle.files:
        if str(f["id"]) not in used_file_ids:
            continue
        src = sources.get(str(f["id"]))
        if src is None:
            raise ImportRefused(f"no source PDF located for {f.get('filename')}")
        path = path_for(run_id, "source", str(f.get("filename") or src.filename))
        file_paths[str(f["id"])] = path
        uploads.append(Upload(path, src.path))
        file_rows.append(
            {
                "id": id_map[str(f["id"])],
                "run_id": run_id,
                "filename": f.get("filename") or src.filename,
                "storage_path": path,
                "size_bytes": int(f.get("size_bytes") or src.size_bytes or 0),
                "mime_type": f.get("mime_type") or PDF_MIME,
                "page_count": f.get("page_count"),
                "label": f.get("label"),
                "source_project_file_id": None,
                "split_file_id": None,
                "status": "labeled" if f.get("label") else "split",
                "created_at": f.get("created_at"),
            }
        )

    files_by_id = {str(f["id"]): f for f in bundle.files}
    document_rows: list[dict] = []
    for d in used_docs:
        f = files_by_id.get(str(d.get("file_id")))
        if f is None:
            raise ImportRefused(f"document {d.get('id')} names a file that is not in the bundle")
        pages = _segment_pages(d, f.get("page_count"))
        if pages is None:
            storage_path = file_paths[str(f["id"])]
        else:
            storage_path = path_for(run_id, "segments", f"{d.get('name') or 'segment'}.pdf")
            uploads.append(Upload(storage_path, sources[str(f["id"])].path, pages))
        document_rows.append(
            {
                "id": id_map[str(d["id"])],
                "run_id": run_id,
                "file_id": id_map[str(f["id"])],
                "segment_id": None,
                "category": d.get("category"),
                "other_type": d.get("other_type"),
                "name": d.get("name"),
                "storage_path": storage_path,
                "page_start": d.get("page_start") or 1,
                "page_end": d.get("page_end") or d.get("page_count") or 1,
                "page_count": d.get("page_count") or 1,
                "page_meta": d.get("page_meta"),
                "sheet_width_in": d.get("sheet_width_in"),
                "sheet_height_in": d.get("sheet_height_in"),
                "status": "done",
                "created_at": d.get("created_at"),
            }
        )

    group_rows: list[dict] = []
    for g in groups_in:
        group_rows.append(
            {
                "id": id_map[str(g["id"])],
                "run_id": run_id,
                "kind": g.get("kind"),
                "category": g.get("category"),
                "group_index": int(g.get("group_index") or 0),
                "pass": int(g.get("pass") or 1),
                "pages": remap_ids(g.get("pages") or [], id_map),
                "est_input_tokens": g.get("est_input_tokens"),
                "status": "done",
                "output": remap_ids(bundle.group_outputs[str(g["id"])], id_map),
                "error": None,
                "attempts": int(g.get("attempts") or 1),
                "started_at": g.get("started_at"),
                "finished_at": g.get("finished_at"),
                "created_at": g.get("created_at"),
            }
        )

    output_rows: list[dict] = []
    categories = list(dict.fromkeys(g.get("category") for g in group_rows if g.get("kind") == "sub"))
    ledger = remap_ids(bundle.run.get("ledger") or {}, id_map)
    if isinstance(ledger, dict):
        ledger.pop("planning_claim", None)
    facts_remapped = {(id_map.get(doc, doc), page): facts for (doc, page), facts in (page_facts or {}).items()}
    if rows_mode == "stored":
        output_rows.extend(_stored_output_rows(bundle, categories, run_id, id_map, notes))
        if page_facts:
            notes.append("rows stored: --page-facts ignored for the rows (the stored outputs carry the live run's floors)")
        categories = []
    rebuilt_outputs: list[dict] = []  # the run-level row id sequence the v2 rebuilds continue
    for cat in categories:
        rebuilt, sources_ids = category_output(cat, group_rows)
        if rebuilt is None:
            notes.append(f"{cat}: no done sub group, no category output")
            continue
        if rows_mode == "v2":
            v2 = rebuild_v2_output(cat, group_rows, documents=document_rows, ledger=ledger, words_by_page=facts_remapped or None,
                                   run_outputs=rebuilt_outputs)
            if v2 is not None:
                rebuilt_outputs.append(v2)
            if v2 is None:
                notes.append(f"{cat}: v2 rebuild found no done sub group; v1 output stored")
                output = rebuilt
            else:
                active = {key: sum(1 for r in v2.get(key) or [] if isinstance(r, Mapping) and r.get("status") == "active") for key in ("counts", "runs", "schedules")}
                notes.append(f"{cat}: v2 rows (active counts {active['counts']}, runs {active['runs']}, schedules {active['schedules']}, floors {len(v2.get('floors') or [])})")
                output = v2
        else:
            stored = bundle.category_outputs.get(cat)
            output = remap_ids(stored, id_map) if (escalations and stored is not None) else rebuilt
        output_rows.append(
            {
                "run_id": run_id,
                "scope": "category",
                "category": cat,
                "version": 1,
                "output": output,
                "source_group_ids": list(sources_ids),
            }
        )

    snapshot = dict(bundle.run.get("settings_snapshot") or {})
    snapshot["plans_reader_escalations_enabled"] = bool(escalations)
    snapshot["plans_reader_recount_enabled"] = bool(recounts)
    if consolidator is not None:
        snapshot["plans_reader_consolidator_mode"] = consolidator
        notes.append(f"consolidator {consolidator}: recorded in the snapshot; the backend's PLANS_READER_CONSOLIDATOR_MODE decides what the main group runs")
    snapshot["import"] = {
        "bundle": str(bundle.root),
        "source_run_id": str(bundle.run.get("id")),
        "escalations": bool(escalations),
        "recounts": bool(recounts),
        "rows_version": 1 if rows_mode == "v1" else 2,
        "rows_mode": rows_mode,
        "rows_mode_reason": rows_reason,
        "consolidator": consolidator,
        "page_facts_pages": len(page_facts or {}),
        "imported_at": now(),
        "project_files": {id_map[str(f["id"])]: str(f.get("source_project_file_id") or "") for f in bundle.files},
    }
    notes.insert(0, f"rows {rows_mode}: {rows_reason}")
    if recounts:
        notes.append("recounts on: recorded only; a v1 bundle has no recount groups")
    pipeline = [g for g in group_rows if g["kind"] in STORED_KINDS]
    short = str(bundle.run.get("id") or "")[:8]
    run_row = {
        "id": run_id,
        "name": (name or f"{bundle.run.get('name') or 'Run'} (import {short})")[:200],
        "status": "consolidating",
        "source": "project" if project_id else "upload",
        "project_id": project_id,
        "render_mode": bundle.run.get("render_mode") or "adaptive",
        "reasoning_effort": bundle.run.get("reasoning_effort") or "medium",
        "model": bundle.run.get("model"),
        "settings_snapshot": snapshot,
        "ledger": ledger,
        "documents_total": len(document_rows),
        "groups_total": len(pipeline),
        "groups_done": len(pipeline),
        "groups_failed": 0,
        "created_by": created_by,
        "started_at": now(),
    }
    case_id = _bundle_case_id(bundle)
    if case_id:
        # An eval bundle's case (bundle.json case_id, else the source run's
        # eval_case_id) marks the imported run leave-one-project-out too, so
        # the pricing library excludes the case's own project (spec 1.3 item 5).
        run_row["eval_case_id"] = case_id
        notes.append(f"eval_case_id {case_id} carried from the bundle")
    return ImportPlan(run_id, run_row, file_rows, document_rows, group_rows, output_rows, uploads, id_map, notes)


def _bundle_case_id(bundle: Any) -> str | None:
    """The bundle's golden case id: bundle.json case_id, else the source run
    row's eval_case_id, else None (a bundle made from an ad hoc run)."""
    try:
        meta_path = Path(getattr(bundle, "root", "")) / "bundle.json"
        if meta_path.is_file():
            value = (json.loads(meta_path.read_text()) or {}).get("case_id")
            if value:
                return str(value)
    except Exception:  # noqa: BLE001 - a bundle without readable meta has no case
        pass
    value = (getattr(bundle, "run", None) or {}).get("eval_case_id")
    return str(value) if value else None


def _stored_output_rows(
    bundle: Bundle, categories: list[str], run_id: str, id_map: Mapping[str, str], notes: list[str],
) -> list[dict]:
    """The bundle's stored v2 category outputs, every version at its own
    number, verbatim but for the remapped ids (run, files, documents,
    groups). source_group_ids come from the output's source.group_ids so
    the latest version names the recount groups it already includes."""
    old_ids = {k for k in id_map if k}
    unknown_group_ids = {str(g.get("id")) for g in bundle.groups} - set(id_map)
    rows: list[dict] = []
    for cat in categories:
        for version, stored in (bundle.category_versions.get(cat) or {}).items():
            output = remap_ids(stored, id_map)
            source_ids = [str(x) for x in ((output.get("source") or {}).get("group_ids") or [])]
            rows.append(
                {
                    "run_id": run_id,
                    "scope": "category",
                    "category": cat,
                    "version": int(version),
                    "output": output,
                    "source_group_ids": source_ids,
                }
            )
            blob = json.dumps(output)
            stale = sorted(i for i in old_ids | unknown_group_ids if i in blob)
            counts = [r for r in output.get("counts") or [] if isinstance(r, Mapping)]
            active = sum(1 for r in counts if r.get("status") == "active")
            notes.append(
                f"{cat} v{version}: stored v2 rows verbatim (counts {len(counts)}, active {active}, runs {len(output.get('runs') or [])}, "
                f"schedules {len(output.get('schedules') or [])}, floors {len(output.get('floors') or [])}, row_seq {output.get('row_seq')})"
                + (f"; {len(stale)} bundle ids not remapped (groups not imported or ids inside longer text)" if stale else "")
            )
    return rows


# Execute -----------------------------------------------------------------------------


def cut_pages(pdf: bytes, pages: Iterable[int]) -> bytes:
    """A PDF of the given 1-based pages, in order."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[int(p) - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def execute_plan(
    sb: Any,
    plan: ImportPlan,
    *,
    upload: Callable[[str, bytes, str], None] | None = None,
    delete_object: Callable[[str], None] | None = None,
    read_bytes: Callable[[Path], bytes] = lambda p: p.read_bytes(),
    cut: Callable[[bytes, list[int]], bytes] = cut_pages,
) -> str:
    """Upload the objects, then insert run, files, documents, groups and
    outputs. On a failure part way the run row (cascade) and the uploaded
    objects are removed. Returns the run id."""
    if upload is None or delete_object is None:
        from app.services import storage

        upload = upload or storage.upload_file
        delete_object = delete_object or storage.delete_file
    done_uploads: list[str] = []
    run_inserted = False
    try:
        for u in plan.uploads:
            data = read_bytes(u.source)
            if u.pages is not None:
                data = cut(data, u.pages)
            upload(u.storage_path, data, PDF_MIME)
            done_uploads.append(u.storage_path)
        sb.table("plans_reader_runs").insert(plan.run_row).execute()
        run_inserted = True
        for table, rows in (
            ("plans_reader_files", plan.file_rows),
            ("plans_reader_documents", plan.document_rows),
            ("plans_reader_groups", plan.group_rows),
            ("plans_reader_outputs", plan.output_rows),
        ):
            if rows:
                sb.table(table).insert(rows).execute()
    except Exception:
        if run_inserted:
            try:
                sb.table("plans_reader_runs").delete().eq("id", plan.run_id).execute()
            except Exception:  # noqa: BLE001 - best effort cleanup; the first error is raised
                print(f"cleanup: could not delete run {plan.run_id}", file=sys.stderr)
        for path in done_uploads:
            try:
                delete_object(path)
            except Exception:  # noqa: BLE001 - best effort cleanup
                print(f"cleanup: could not delete {path}", file=sys.stderr)
        raise
    return plan.run_id


# Page facts store (WS1, $0) --------------------------------------------------------------


def _facts_order(rows: list[dict]) -> list[dict]:
    """Drawing documents first (the derive measure reads them), then the
    rest, each in plan order."""
    return sorted(rows, key=lambda d: (0 if str(d.get("category") or "").endswith("_drawings") else 1))


def _facts_line(doc: Mapping[str, Any], summary: Mapping[str, Any]) -> dict:
    return {
        "document_id": str(doc.get("id")),
        "name": doc.get("name"),
        "category": doc.get("category"),
        "status": summary.get("status"),
        "cache": summary.get("cache"),
        "error": summary.get("error"),
    }


def persist_page_facts(
    sb: Any,
    plan: ImportPlan,
    *,
    read_bytes: Callable[[Path], bytes] = lambda p: p.read_bytes(),
    cut: Callable[[bytes, list[int]], bytes] = cut_pages,
    store_facts: Callable[..., Mapping[str, Any]] | None = None,
    settings: Any = None,
) -> list[dict]:
    """Write the WS1 page facts of every imported document (the plan's
    document ids, after execute_plan inserted them) to the page facts
    store, from the same bytes the plan uploaded (a segment cut to its
    pages). `store_facts(sb, document_id, pdf_bytes=..., settings=...)`
    defaults to pagefacts.integration.extract_and_store (relabel off). A
    document whose extraction fails is marked failed by the job core and
    the others still run. Returns one line per document."""
    if store_facts is None:
        from app.services.pagefacts.integration import extract_and_store as store_facts
    uploads = {u.storage_path: u for u in plan.uploads}
    lines: list[dict] = []
    for doc in _facts_order(plan.document_rows):
        u = uploads.get(str(doc.get("storage_path")))
        data: bytes | None = None
        if u is not None:
            data = read_bytes(u.source)
            if u.pages is not None:
                data = cut(data, u.pages)
        try:
            summary = store_facts(sb, str(doc["id"]), pdf_bytes=data, settings=settings)
        except Exception as exc:  # noqa: BLE001 - one document never stops the others
            summary = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        lines.append(_facts_line(doc, summary or {}))
    return lines


def backfill_page_facts(
    sb: Any,
    run_id: str,
    *,
    store_facts: Callable[..., Mapping[str, Any]] | None = None,
    settings: Any = None,
) -> list[dict]:
    """persist_page_facts for an existing run: every document of the run
    (failed ones skipped) from its stored PDF. Refuses a run that is not
    an import (the live facts job owns every other run)."""
    if store_facts is None:
        from app.services.pagefacts.integration import extract_and_store as store_facts
    runs = sb.table("plans_reader_runs").select("id, settings_snapshot").eq("id", str(run_id)).limit(1).execute().data or []
    if not runs:
        raise ImportRefused(f"run {run_id} does not exist")
    if not isinstance((runs[0].get("settings_snapshot") or {}).get("import"), Mapping):
        raise ImportRefused(f"run {run_id} was not made by this import harness; its facts belong to the facts job")
    docs = sb.table("plans_reader_documents").select("*").eq("run_id", str(run_id)).execute().data or []
    lines: list[dict] = []
    for doc in _facts_order([d for d in docs if d.get("status") != "failed"]):
        try:
            summary = store_facts(sb, str(doc["id"]), pdf_bytes=None, settings=settings)
        except Exception as exc:  # noqa: BLE001 - one document never stops the others
            summary = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        lines.append(_facts_line(doc, summary or {}))
    return lines


def check_dev_target(url: str | None) -> None:
    """Refuse anything but the dev BDR project (never BDR_Prod or staging)."""
    if not url or DEV_REF not in url:
        raise ImportRefused(
            f"SUPABASE_URL must point at the dev project {DEV_REF}; got {url or 'nothing'}. "
            "Refusing (BDR_Prod and BDR_Staging need explicit owner approval)."
        )


def _on_off(value: str) -> bool:
    v = value.strip().lower()
    if v in ("on", "true", "yes", "1"):
        return True
    if v in ("off", "false", "no", "0"):
        return False
    raise argparse.ArgumentTypeError("use on or off")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Import a stored AI Estimating run bundle as a DEV run at consolidating.")
    p.add_argument("--bundle", type=Path, default=None, help="bundle folder (run.json, files.json, documents.json, groups.json, outputs/); required unless --facts-for-run")
    p.add_argument("--backup", type=Path, default=DEFAULT_BACKUP, help="local production backup (read only) holding the source PDFs")
    p.add_argument("--escalations", type=_on_off, default=True, metavar="on|off", help="import escalation groups and patches (default on)")
    p.add_argument("--recounts", type=_on_off, default=False, metavar="on|off", help="record the recount pass flag (default off)")
    p.add_argument("--name", default=None, help="run name (default: '<bundle run name> (import <id8>)')")
    p.add_argument("--project-id", default=None, help="a DEV project id to attach (source becomes project)")
    p.add_argument("--created-by", default=None, help="a DEV profile id for created_by")
    p.add_argument(
        "--rows", choices=("auto",) + ROWS_MODES, default="auto",
        help="stored: the bundle's stored v2 category outputs verbatim (the default when every category output carries rows_version); "
        "v2: rebuilt from the groups (the default otherwise); v1: the bundle's v1 outputs",
    )
    p.add_argument("--consolidator", choices=("tools", "legacy"), default=None, help="record the intended consolidator mode in the run's snapshot (the backend setting decides)")
    p.add_argument("--page-facts", type=Path, default=None, help="page facts JSON (fixture layout) matched to the bundle's documents by name; an optional override for the v2 census floors")
    p.add_argument("--facts", type=_on_off, default=True, metavar="on|off", help="write WS1 page facts of every imported document to the facts store ($0, default on; the derive measure needs them)")
    p.add_argument("--facts-for-run", default=None, metavar="RUN_ID", help="only write the page facts of an existing imported DEV run's documents (from its stored PDFs), then exit")
    p.add_argument("--queue-main", action="store_true", help="queue the main consolidation now (a LIVE model call on a dev backend)")
    p.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.facts_for_run:
        from app.core.config import get_settings
        from app.core.supabase_client import get_supabase

        check_dev_target(get_settings().supabase_url)
        lines = backfill_page_facts(get_supabase(), args.facts_for_run)
        print(json.dumps({"run_id": args.facts_for_run, "page_facts": lines}, indent=1))
        return 0 if all(line["status"] == "done" for line in lines) else 1
    if args.bundle is None:
        parser.error("--bundle is required (or --facts-for-run RUN_ID)")
    bundle = load_bundle(args.bundle)
    sources = locate_sources(bundle, args.backup)
    page_facts = load_page_facts(args.page_facts, bundle.documents) if args.page_facts else None
    plan = plan_import(
        bundle,
        sources,
        name=args.name,
        escalations=args.escalations,
        recounts=args.recounts,
        project_id=args.project_id,
        created_by=args.created_by,
        rows_version=None if args.rows == "auto" else args.rows,
        consolidator=args.consolidator,
        page_facts=page_facts,
    )
    if args.dry_run:
        summary = plan.summary()
        summary["page_facts_store"] = (
            f"WS1 facts extracted and stored for {len(plan.document_rows)} documents after the insert" if args.facts else "off"
        )
        print(json.dumps(summary, indent=1))
        return 0
    from app.core.config import get_settings
    from app.core.supabase_client import get_supabase

    check_dev_target(get_settings().supabase_url)
    sb = get_supabase()
    run_id = execute_plan(sb, plan)
    summary = plan.summary()
    if args.facts:
        summary["page_facts_store"] = persist_page_facts(sb, plan)
        bad = [line for line in summary["page_facts_store"] if line["status"] != "done"]
        if bad:
            print(f"page facts: {len(bad)} documents without finished facts: {[line['name'] for line in bad]}", file=sys.stderr)
    print(json.dumps(summary, indent=1), file=sys.stderr)
    if args.queue_main:
        from app.services import plans_reader_runs as runs

        run = sb.table("plans_reader_runs").select("*").eq("id", run_id).execute().data[0]
        runs._write_outputs_and_queue_main(sb, run, runs._groups(sb, run_id))
        print("main consolidation queued", file=sys.stderr)
    print(run_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ImportRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(2)
