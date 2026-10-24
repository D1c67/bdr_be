"""Run bundles for the AI Estimating eval harness (docs/AI_ESTIMATING_BUILD_SPEC.md
WS0 step 7; docs/AI_ESTIMATING_EVAL.md).

A bundle is one stored run on disk, in the layout of the 7154 weekend export
(`EVAL_ROOT/bundles/7154/ff8fad25`, see its INDEX.md):

    run.json            the plans_reader_runs row (settings_snapshot, ledger, tokens, cost)
    files.json          plans_reader_files rows (source PDFs, labels, page counts)
    documents.json      plans_reader_documents rows incl. page_meta and sheet sizes
    groups.json         plans_reader_groups rows WITHOUT their output
    groups/<kind>_<category or none>_<index>_p<pass>.json   one group output each (a group
                        whose name repeats an earlier group's gets `_<id8>` before
                        .json and records the file name as `output_file` on its row)
    outputs/<scope>_<category or none>_v<n>.json   one plans_reader_outputs row each, every
                        scope: category_<category>_v<n>, main_none_v<n>, and the
                        autonomy scopes policy_none_v<n> (WS5 scope context),
                        measure_none_v<n> (WS3) and evidence_none_v<n> (WS8)
    deliverables.json   plans_reader_deliverables rows without spec_json
    deliverables/<kind>_v<n>.json   the spec_json of each deliverable
    calls.json          plans_reader_calls rows (request snapshots carry hashes, never image bytes)
    chat.json           chat threads with their messages
    pricing.json        plans_reader_pricing rows, every version (stale ones included)
    pricing_lines/v<n>.json   plans_reader_priced_lines rows ({line_id, line}) of version n
    quotes.json         vendor_quote_extractions rows of the run
    reviews.json        plans_reader_reviews rows
    review_findings.json   plans_reader_review_findings rows
    gate_findings.json  plans_reader_gate_findings rows (every stage and status)
    recounts.json       plans_reader_recounts rows
    signoffs.json       plans_reader_price_signoffs rows
    artifacts/<name>.json   optional stage artifacts a later stage wrote (priced_estimate, gate_report, ...)
    inputs/<document or file id>.pdf   optional source PDFs (otherwise resolved from the backup)
    bundle.json         this module's metadata (case, source, run id, sha256 of every member)

The run row in run.json carries the autonomy columns too (bid_conditions,
gate_summary, autonomy_state, scope_policy_sha); `RunBundle.run_columns`
exposes them. Every member past the four required ones is optional: a bundle
exported before the member existed (the weekend bundle has none of the
pricing, review or gate members and only the category and main outputs)
loads with the member read as absent, never as an error. The replay's
`stored_artifacts` turns the pricing rows into the `priced_estimate`
artifact and the gate findings into `gate_report`.

Loading gives typed objects (`RunBundle`); the replay (`plans_reader_replay`)
and the acceptance suite (`plans_reader_acceptance`) read bundles only through
it. Source PDFs are never copied into a bundle by default: `doc_bytes` maps the
run file's `source_project_file_id` through the production backup's
`data/project_files.json` (category and storage path from the row, never from
the folder), cuts a splitter segment to its pages, and only then falls back to
`inputs/`.

`export_run` writes a bundle from a DEV run through a sync Supabase client (call
it from a worker or a script, never inside `async def`); it refuses a production
database. `import_weekend_export` turns the weekend export into a bundle in
place or in a copy (it only adds bundle.json). The Five Star (7172) and YMCA
(7177) dev-run exports are wave 2 work: they need the dev database and the run
ids recorded in the build log; `export_run` is ready for them.

Bundles hold the dev run's pricing rows when it has them, so they stay under
EVAL_ROOT, outside git; nothing in this module holds a price literal.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BUNDLE_SCHEMA = "bdr.eval.bundle/1"
DEV_REF = "bpidntbyvoooqvaispup"
PROD_REF = "zxqkqotlcgbalwoqeeca"
STAGING_REF = "qpfuugcwsdfpxvdgwvwx"
PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_BACKUP_DIR = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")

REQUIRED_MEMBERS: tuple[str, ...] = ("run.json", "files.json", "documents.json", "groups.json")
OPTIONAL_MEMBERS: tuple[str, ...] = ("calls.json", "chat.json", "deliverables.json")
# Row members of the autonomy build (0165 to 0169): bundle file -> (table, RunBundle field).
ROW_MEMBERS: dict[str, tuple[str, str]] = {
    "pricing.json": ("plans_reader_pricing", "pricing_rows"),
    "quotes.json": ("vendor_quote_extractions", "quote_rows"),
    "reviews.json": ("plans_reader_reviews", "review_rows"),
    "review_findings.json": ("plans_reader_review_findings", "review_finding_rows"),
    "gate_findings.json": ("plans_reader_gate_findings", "gate_finding_rows"),
    "recounts.json": ("plans_reader_recounts", "recount_rows"),
    "signoffs.json": ("plans_reader_price_signoffs", "signoff_rows"),
}
MEMBER_DIRS: tuple[str, ...] = ("groups", "outputs", "deliverables", "artifacts", "pricing_lines")
# Directories export_run writes (and clears on a re-export); artifacts/ and
# inputs/ are never written by an export and are left alone.
EXPORT_DIRS: tuple[str, ...] = ("groups", "outputs", "deliverables", "pricing_lines")
RUN_COLUMNS: tuple[str, ...] = ("bid_conditions", "gate_summary", "autonomy_state", "scope_policy_sha", "scope_policy_id")
SOURCE_KINDS: tuple[str, ...] = ("prod_export", "dev_db", "synthetic")

# Keys of a request snapshot (or any call payload) that would hold image bytes.
_IMAGE_KEYS: frozenset[str] = frozenset(
    {"image_url", "image", "data", "b64", "b64_json", "bytes", "png", "jpeg", "content_bytes"}
)
_DATA_URL_RE = re.compile(r"^data:[\w/.+-]+;base64,")
_OUTPUT_NAME_RE = re.compile(r"^(?P<scope>[a-z]+)_(?P<category>.+)_v(?P<version>\d+)\.json$")
_PRICING_LINES_RE = re.compile(r"^v(?P<version>\d+)\.json$")
_DELIVERABLE_NAME_RE = re.compile(r"^(?P<kind>[a-z_]+)_v(?P<version>\d+)\.json$")


class BundleError(RuntimeError):
    """The bundle, the backup or the target cannot be used as asked."""


# Helpers ---------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, default=str, ensure_ascii=False) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_pacific_iso() -> str:
    return datetime.now(PACIFIC).isoformat(timespec="seconds")


def group_output_name(group: Mapping[str, Any]) -> str:
    """The bundle file name of a group's output (the weekend export layout)."""
    return (
        f"{group.get('kind')}_{group.get('category') or 'none'}_"
        f"{int(group.get('group_index') or 0)}_p{int(group.get('pass') or 1)}.json"
    )


def output_name(scope: str, category: str | None, version: int) -> str:
    return f"{scope}_{category or 'none'}_v{int(version)}.json"


def deliverable_name(kind: str, version: int) -> str:
    return f"{kind}_v{int(version)}.json"


def pricing_lines_name(version: int) -> str:
    return f"v{int(version)}.json"


def output_key(scope: str, category: str | None) -> str:
    """The RunBundle.outputs key of one output: `category:<category>`,
    `main`, or the scope itself (`policy`, `measure`, `evidence`); a
    non-category scope that names a category keys as `<scope>:<category>`."""
    if scope == "category":
        return f"category:{category}"
    if category in (None, "", "none"):
        return scope
    return f"{scope}:{category}"


def strip_images(obj: Any) -> Any:
    """A deep copy with image payloads removed: data URLs anywhere, and
    strings under image-like keys longer than a hash. Snapshots keep their
    heads, sizes and sha256 fields."""
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and (_DATA_URL_RE.match(v) or (k in _IMAGE_KEYS and len(v) > 256)):
                out[k] = {"stripped": True, "chars": len(v)}
            else:
                out[k] = strip_images(v)
        return out
    if isinstance(obj, list):
        return [strip_images(v) for v in obj]
    if isinstance(obj, str) and _DATA_URL_RE.match(obj):
        return {"stripped": True, "chars": len(obj)}
    return copy.deepcopy(obj)


def check_not_production(settings: Any = None, url: str | None = None) -> None:
    """Refuse any export that would read BDR_Prod or BDR_Staging."""
    if settings is not None:
        if getattr(settings, "is_production", False):
            raise BundleError("export_run refuses to run against a production environment")
        url = url or getattr(settings, "supabase_url", "") or ""
    text = url or ""
    if PROD_REF in text or STAGING_REF in text:
        raise BundleError("export_run refuses the BDR_Prod and BDR_Staging databases")


# The bundle ------------------------------------------------------------------------------


@dataclass
class RunBundle:
    """One stored run. `groups` carry their stored `output` when the bundle
    has it; `outputs` are keyed `category:<category>`, `main`, `policy`,
    `measure`, `evidence` (highest version; see `output_key`);
    `deliverables` map (kind, version) to the spec_json. The row members
    (pricing, quotes, reviews, findings, recounts, sign-offs) are empty
    lists when the bundle has none; `pricing_lines` maps a pricing version
    to its line rows."""

    root: Path
    run: dict[str, Any]
    files: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_versions: dict[str, int] = field(default_factory=dict)
    deliverables: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    deliverable_rows: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    chat: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    group_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    pricing_rows: list[dict[str, Any]] = field(default_factory=list)
    pricing_lines: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    quote_rows: list[dict[str, Any]] = field(default_factory=list)
    review_rows: list[dict[str, Any]] = field(default_factory=list)
    review_finding_rows: list[dict[str, Any]] = field(default_factory=list)
    gate_finding_rows: list[dict[str, Any]] = field(default_factory=list)
    recount_rows: list[dict[str, Any]] = field(default_factory=list)
    signoff_rows: list[dict[str, Any]] = field(default_factory=list)
    members_present: set[str] = field(default_factory=set)

    # identity

    @property
    def run_id(self) -> str:
        return str(self.run.get("id") or "")

    @property
    def run_id8(self) -> str:
        return self.run_id[:8]

    @property
    def case_id(self) -> str | None:
        value = self.meta.get("case_id") or self.run.get("eval_case_id")
        return str(value) if value else None

    @property
    def source(self) -> str:
        return str(self.meta.get("source") or "unknown")

    # lookups

    def document(self, document_id: str) -> dict[str, Any]:
        for d in self.documents:
            if str(d.get("id")) == str(document_id):
                return d
        raise BundleError(f"document {document_id} is not in bundle {self.root}")

    def file_for(self, document: Mapping[str, Any]) -> dict[str, Any] | None:
        file_id = str(document.get("file_id") or "")
        for f in self.files:
            if str(f.get("id")) == file_id:
                return f
        return None

    def documents_by_category(self, category: str) -> list[dict[str, Any]]:
        return [d for d in self.documents if d.get("category") == category]

    def groups_of(self, kind: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
        out = []
        for g in self.groups:
            if kind is not None and g.get("kind") != kind:
                continue
            if category is not None and g.get("category") != category:
                continue
            out.append(g)
        return out

    def category_output(self, category: str) -> dict[str, Any] | None:
        return self.outputs.get(f"category:{category}")

    @property
    def categories(self) -> list[str]:
        return sorted(k.split(":", 1)[1] for k in self.outputs if k.startswith("category:"))

    @property
    def main_output(self) -> dict[str, Any] | None:
        return self.outputs.get("main")

    def scope_output(self, scope: str) -> dict[str, Any] | None:
        """The highest version of a non-category scope (`policy`, `measure`,
        `evidence`), or None."""
        value = self.outputs.get(scope)
        return value if isinstance(value, Mapping) else None

    @property
    def run_columns(self) -> dict[str, Any]:
        """The autonomy columns of the run row that are present (RUN_COLUMNS)."""
        return {k: self.run[k] for k in RUN_COLUMNS if self.run.get(k) is not None}

    def has_member(self, name: str) -> bool:
        return name in self.members_present

    def latest_pricing(self) -> dict[str, Any] | None:
        """The pricing version a reader should use: the highest non-stale
        version, else the highest version; None without pricing rows."""
        rows = [r for r in self.pricing_rows if isinstance(r, Mapping)]
        if not rows:
            return None
        fresh = [r for r in rows if not r.get("stale")]
        pool = fresh or rows
        return max(pool, key=lambda r: int(r.get("version") or 0))

    def pricing_version_lines(self, version: int) -> list[dict[str, Any]]:
        return list(self.pricing_lines.get(int(version)) or [])

    def latest_review(self) -> dict[str, Any] | None:
        """The newest review row with its findings under `findings`."""
        rows = [r for r in self.review_rows if isinstance(r, Mapping)]
        if not rows:
            return None
        latest = max(rows, key=lambda r: (int(r.get("base_pricing_version") or 0), str(r.get("created_at") or "")))
        findings = [f for f in self.review_finding_rows if isinstance(f, Mapping) and str(f.get("review_id")) == str(latest.get("id"))]
        return {**latest, "findings": sorted(findings, key=lambda f: str(f.get("finding_id") or ""))}

    def deliverable_versions(self, kind: str) -> list[int]:
        return sorted(v for (k, v) in self.deliverables if k == kind)

    def latest_deliverable(self, kind: str) -> tuple[int, dict[str, Any]] | None:
        versions = self.deliverable_versions(kind)
        if not versions:
            return None
        return versions[-1], self.deliverables[(kind, versions[-1])]

    def boq_spec(self, version: int | None = None) -> dict[str, Any] | None:
        """The BOQ deliverable spec (latest version unless given), else the
        main output's boq."""
        if version is not None:
            return self.deliverables.get(("boq", int(version)))
        latest = self.latest_deliverable("boq")
        if latest is not None:
            return latest[1]
        main = self.main_output
        if isinstance(main, dict) and isinstance(main.get("boq"), dict):
            return main["boq"]
        return None

    def artifact(self, name: str) -> Any:
        return self.artifacts.get(name)

    def chat_probe(self) -> dict[str, Any] | None:
        """The labor-hours chat probe when the run has one: the first thread
        whose title or user message asks about labor hours, with the
        assistant answer text."""
        for thread in self.chat:
            title = str(thread.get("title") or "")
            messages = [m for m in (thread.get("messages") or []) if isinstance(m, Mapping)]
            user = next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")
            if re.search(r"labou?r.hours|how many .*hours", f"{title} {user}", re.I):
                answer = next(
                    (str(m.get("content") or "") for m in messages if m.get("role") == "assistant"), ""
                )
                return {"thread_id": thread.get("id"), "question": user or title, "answer": answer}
        return None

    # source PDFs

    def doc_bytes(self, document_id: str, *, backup_dir: Path | str = DEFAULT_BACKUP_DIR) -> bytes:
        """The PDF of one run document: the whole source file, or the
        document's pages cut from it when the document is a splitter
        segment. Resolution order: the backup (by the run file's
        `source_project_file_id` through data/project_files.json), then
        `inputs/<document id>.pdf` or `inputs/<file id>.pdf` in the bundle."""
        document = self.document(document_id)
        f = self.file_for(document)
        source = self._source_pdf(document, f, Path(backup_dir))
        pages = segment_pages(document, f.get("page_count") if f else None)
        if pages is None:
            return source
        return cut_pages(source, pages)

    def _source_pdf(self, document: Mapping[str, Any], f: Mapping[str, Any] | None, backup_dir: Path) -> bytes:
        tried: list[str] = []
        if f is not None:
            pf_id = str(f.get("source_project_file_id") or "")
            if pf_id:
                row = backup_project_file(backup_dir, pf_id)
                if row is not None:
                    path = backup_dir / "storage" / "project-files" / str(row.get("storage_path") or "")
                    if path.is_file():
                        return path.read_bytes()
                    tried.append(str(path))
                else:
                    tried.append(f"project file {pf_id} not in {backup_dir}")
        for candidate in (
            self.root / "inputs" / f"{document.get('id')}.pdf",
            self.root / "inputs" / f"{(f or {}).get('id')}.pdf",
        ):
            if candidate.is_file():
                return candidate.read_bytes()
            tried.append(str(candidate))
        raise BundleError(f"no source PDF for document {document.get('id')} ({document.get('name')}): tried {tried}")


_project_files_cache: dict[str, dict[str, dict[str, Any]]] = {}


def backup_project_file(backup_dir: Path | str, project_file_id: str) -> dict[str, Any] | None:
    """The data/project_files.json row of one project file (cached per
    backup directory; the backup is read only)."""
    backup_dir = Path(backup_dir)
    key = str(backup_dir)
    index = _project_files_cache.get(key)
    if index is None:
        path = backup_dir / "data" / "project_files.json"
        if not path.is_file():
            return None
        rows = _read_json(path)
        rows = rows.get("rows", []) if isinstance(rows, Mapping) else rows
        index = {str(r.get("id")): r for r in rows if isinstance(r, Mapping)}
        _project_files_cache[key] = index
    return index.get(str(project_file_id))


def segment_pages(document: Mapping[str, Any], file_pages: int | None) -> list[int] | None:
    """Source-file pages of a document, or None when the document is the
    whole file (the import harness rule)."""
    meta = [m for m in document.get("page_meta") or [] if isinstance(m, Mapping)]
    pages = [
        int(m["page"])
        for m in sorted(meta, key=lambda m: int(m.get("pdf_page") or m.get("page") or 0))
        if m.get("page")
    ]
    if not pages:
        start, end = int(document.get("page_start") or 1), int(document.get("page_end") or 1)
        pages = list(range(start, end + 1))
    if file_pages and pages == list(range(1, int(file_pages) + 1)):
        return None
    if not file_pages and pages == list(range(1, len(pages) + 1)) and not document.get("segment_id"):
        return None
    return pages


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


# Loading ---------------------------------------------------------------------------------


def _attach_group_outputs(bundle: RunBundle) -> None:
    """Resolve each group's stored output: groups/<name>.json, else the
    category or main output it equals (a roll-up, a category's single sub,
    the main group), the way the import harness does."""
    subs_per_cat: dict[str, int] = {}
    for g in bundle.groups:
        if g.get("kind") == "sub":
            subs_per_cat[str(g.get("category"))] = subs_per_cat.get(str(g.get("category")), 0) + 1
    drop = ("escalation_patches", "unresolved_flags", "source")
    for g in bundle.groups:
        gid = str(g.get("id"))
        if isinstance(g.get("output"), Mapping):
            bundle.group_outputs[gid] = g["output"]
            continue
        path = bundle.root / "groups" / str(g.get("output_file") or group_output_name(g))
        cat = str(g.get("category"))
        cat_out = bundle.outputs.get(f"category:{cat}")
        if path.is_file():
            out = _read_json(path)
        elif g.get("kind") == "rollup" and cat_out is not None:
            out = {k: copy.deepcopy(v) for k, v in cat_out.items() if k not in drop}
        elif g.get("kind") == "sub" and subs_per_cat.get(cat) == 1 and cat_out is not None:
            out = {k: copy.deepcopy(v) for k, v in cat_out.items() if k not in drop}
        elif g.get("kind") == "main" and bundle.main_output is not None:
            out = bundle.main_output
        else:
            continue
        g["output"] = out
        bundle.group_outputs[gid] = out


def load_bundle(root: Path | str, *, require_meta: bool = False) -> RunBundle:
    """Load a bundle directory into a RunBundle. `bundle.json` is optional
    unless `require_meta` (the weekend export has none until imported)."""
    root = Path(root)
    for name in REQUIRED_MEMBERS:
        if not (root / name).is_file():
            raise BundleError(f"bundle {root} has no {name}")
    run = _read_json(root / "run.json")
    files = _read_json(root / "files.json")
    documents = _read_json(root / "documents.json")
    groups = _read_json(root / "groups.json")
    bundle = RunBundle(root=root, run=run, files=files, documents=documents, groups=groups)
    for name in OPTIONAL_MEMBERS:
        path = root / name
        if not path.is_file():
            continue
        data = _read_json(path)
        if name == "calls.json":
            bundle.calls = list(data or [])
        elif name == "chat.json":
            bundle.chat = list(data or [])
        else:
            bundle.deliverable_rows = list(data or [])
        bundle.members_present.add(name)
    for name, (_table, attr) in ROW_MEMBERS.items():
        path = root / name
        if not path.is_file():
            continue
        data = _read_json(path)
        if isinstance(data, list):
            setattr(bundle, attr, [r for r in data if isinstance(r, Mapping)])
            bundle.members_present.add(name)
        else:
            bundle.notes.append(f"{name} ignored (not a list of rows)")
    lines_dir = root / "pricing_lines"
    if lines_dir.is_dir():
        for path in sorted(lines_dir.glob("*.json")):
            m = _PRICING_LINES_RE.match(path.name)
            data = _read_json(path) if m else None
            if not m or not isinstance(data, list):
                bundle.notes.append(f"pricing_lines/{path.name} ignored (unexpected name or shape)")
                continue
            bundle.pricing_lines[int(m.group("version"))] = [r for r in data if isinstance(r, Mapping)]
    outputs_dir = root / "outputs"
    if outputs_dir.is_dir():
        for path in sorted(outputs_dir.glob("*.json")):
            m = _OUTPUT_NAME_RE.match(path.name)
            if not m:
                bundle.notes.append(f"outputs/{path.name} ignored (unexpected name)")
                continue
            version = int(m.group("version"))
            data = _read_json(path)
            scope = m.group("scope")
            if scope == "category":
                category = str(data.get("category") or m.group("category")) if isinstance(data, Mapping) else m.group("category")
                key = output_key("category", category)
            else:
                key = output_key(scope, m.group("category"))
            if version >= bundle.output_versions.get(key, 0):
                bundle.outputs[key] = data
                bundle.output_versions[key] = version
    deliv_dir = root / "deliverables"
    if deliv_dir.is_dir():
        for path in sorted(deliv_dir.glob("*.json")):
            m = _DELIVERABLE_NAME_RE.match(path.name)
            if not m:
                bundle.notes.append(f"deliverables/{path.name} ignored (unexpected name)")
                continue
            bundle.deliverables[(m.group("kind"), int(m.group("version")))] = _read_json(path)
    art_dir = root / "artifacts"
    if art_dir.is_dir():
        for path in sorted(art_dir.glob("*.json")):
            bundle.artifacts[path.stem] = _read_json(path)
    meta_path = root / "bundle.json"
    if meta_path.is_file():
        bundle.meta = _read_json(meta_path)
    elif require_meta:
        raise BundleError(f"bundle {root} has no bundle.json (import it first)")
    _attach_group_outputs(bundle)
    return bundle


# Metadata ---------------------------------------------------------------------------------


def member_paths(root: Path | str) -> list[Path]:
    """Every member file of a bundle (bundle.json excluded), sorted."""
    root = Path(root)
    out: list[Path] = []
    for name in REQUIRED_MEMBERS + OPTIONAL_MEMBERS + tuple(ROW_MEMBERS):
        if (root / name).is_file():
            out.append(root / name)
    for sub in MEMBER_DIRS + ("inputs",):
        d = root / sub
        if d.is_dir():
            out.extend(p for p in sorted(d.rglob("*")) if p.is_file())
    return sorted(set(out))


def member_sha256s(root: Path | str) -> dict[str, str]:
    root = Path(root)
    return {str(p.relative_to(root)): sha256_file(p) for p in member_paths(root)}


def bundle_meta(
    root: Path | str,
    *,
    case_id: str | None,
    source: str,
    run: Mapping[str, Any] | None = None,
    note: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if source not in SOURCE_KINDS:
        raise BundleError(f"source must be one of {SOURCE_KINDS}; got {source!r}")
    root = Path(root)
    run = run or _read_json(root / "run.json")
    snapshot = run.get("settings_snapshot") if isinstance(run.get("settings_snapshot"), Mapping) else {}
    meta: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "case_id": case_id,
        "source": source,
        "run_id": run.get("id"),
        "run_name": run.get("name"),
        "project_id": run.get("project_id"),
        "exported_at": now_pacific_iso(),
        "code_version": snapshot.get("code_version"),
        "prompt_hash": snapshot.get("prompt_hash"),
        "pricing_free": not ((root / "artifacts" / "priced_estimate.json").is_file() or _has_rows(root / "pricing.json")),
        "note": note,
        "members": member_sha256s(root),
    }
    if extra:
        meta.update(dict(extra))
    return meta


def _has_rows(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = _read_json(path)
    except ValueError:
        return False
    return isinstance(data, list) and bool(data)


def write_bundle_meta(root: Path | str, meta: Mapping[str, Any]) -> Path:
    root = Path(root)
    path = root / "bundle.json"
    path.write_text(_dump(meta))
    return path


def verify_members(root: Path | str) -> list[str]:
    """Members whose sha256 differs from bundle.json (empty when intact)."""
    root = Path(root)
    meta_path = root / "bundle.json"
    if not meta_path.is_file():
        raise BundleError(f"bundle {root} has no bundle.json")
    recorded = dict((_read_json(meta_path).get("members") or {}))
    current = member_sha256s(root)
    bad = [k for k, v in recorded.items() if current.get(k) != v]
    bad += [k for k in current if k not in recorded]
    return sorted(bad)


# Weekend import ---------------------------------------------------------------------------


def import_weekend_export(
    src: Path | str,
    out_dir: Path | str | None = None,
    *,
    case_id: str = "7154",
    source: str = "prod_export",
    note: str = "",
) -> Path:
    """Make the weekend export a bundle: copy its members to `out_dir` (or
    work in place when `out_dir` is the source or None) and write
    bundle.json. Extra files of the export (INDEX.md, DIAGNOSIS.md, raw/,
    renders/, scripts/, prop/) are copied alongside but are not members."""
    import shutil

    src = Path(src)
    out = Path(out_dir) if out_dir is not None else src
    if not (src / "run.json").is_file():
        raise BundleError(f"{src} is not a run export (no run.json)")
    if out.resolve() != src.resolve():
        out.mkdir(parents=True, exist_ok=True)
        for item in sorted(src.iterdir()):
            target = out / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
    bundle = load_bundle(out)
    meta = bundle_meta(
        out,
        case_id=case_id,
        source=source,
        run=bundle.run,
        note=note or "weekend export of the 7154 prod run (spec 3.2), imported as the baseline bundle",
        extra={"imported_from": str(src)},
    )
    write_bundle_meta(out, meta)
    return out


# Dev export --------------------------------------------------------------------------------


def _rows(resp: Any) -> list[dict[str, Any]]:
    data = getattr(resp, "data", resp)
    return list(data or [])


def _select(sb: Any, table: str, **eq: Any) -> list[dict[str, Any]]:
    q = sb.table(table).select("*")
    for k, v in eq.items():
        q = q.eq(k, v)
    return _rows(q.execute())


def _select_optional(sb: Any, table: str, unavailable: list[str], **eq: Any) -> list[dict[str, Any]] | None:
    """`_select` for a table a dev database may not have yet (0165 to 0169):
    None, recorded in `unavailable`, when the read fails."""
    try:
        return _select(sb, table, **eq)
    except Exception as exc:  # noqa: BLE001 - a missing table is an absent member, not an export failure
        unavailable.append(f"{table}: {str(exc)[:120]}")
        return None


def clear_export_members(out: Path) -> list[str]:
    """Remove what a previous export wrote to `out` (the member files and
    the EXPORT_DIRS contents and bundle.json), keeping the directory and
    everything an export never writes (replays/, artifacts/, inputs/, notes).
    Returns the removed paths relative to `out`."""
    removed: list[str] = []
    if not out.is_dir():
        return removed
    for name in REQUIRED_MEMBERS + OPTIONAL_MEMBERS + tuple(ROW_MEMBERS) + ("bundle.json",):
        path = out / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    for sub in EXPORT_DIRS:
        d = out / sub
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            path.unlink()
            removed.append(f"{sub}/{path.name}")
    return removed


def export_run(
    sb: Any,
    run_id: str,
    out_dir: Path | str,
    *,
    case_id: str | None = None,
    settings: Any = None,
    supabase_url: str | None = None,
    note: str = "",
) -> Path:
    """Write the bundle of a DEV run to `out_dir`. A re-export into an
    existing bundle directory replaces every member (stale group, output,
    deliverable and pricing-line files of the earlier export are removed;
    replays/ and other non-member files stay). Reads plans_reader_runs,
    _files, _documents, _groups (with output), _outputs (every scope),
    _deliverables (spec_json), _calls, the chat tables and the autonomy
    tables of ROW_MEMBERS plus each pricing version's priced lines through
    the given sync client; a table the database does not have is recorded
    under `unavailable_tables` in bundle.json and its member is not written.
    Refuses BDR_Prod and BDR_Staging."""
    check_not_production(settings, supabase_url)
    out = Path(out_dir)
    runs = _select(sb, "plans_reader_runs", id=run_id)
    if not runs:
        raise BundleError(f"run {run_id} not found")
    run = runs[0]
    files = _select(sb, "plans_reader_files", run_id=run_id)
    documents = _select(sb, "plans_reader_documents", run_id=run_id)
    groups = _select(sb, "plans_reader_groups", run_id=run_id)
    outputs = _select(sb, "plans_reader_outputs", run_id=run_id)
    deliverables = _select(sb, "plans_reader_deliverables", run_id=run_id)
    calls = _select(sb, "plans_reader_calls", run_id=run_id)
    threads = _select(sb, "plans_reader_chat_threads", run_id=run_id)
    for t in threads:
        t["messages"] = sorted(
            _select(sb, "plans_reader_chat_messages", thread_id=t.get("id")),
            key=lambda m: str(m.get("created_at") or ""),
        )

    unavailable: list[str] = []
    row_members: dict[str, list[dict[str, Any]]] = {}
    for name, (table, _attr) in ROW_MEMBERS.items():
        rows = _select_optional(sb, table, unavailable, run_id=run_id)
        if rows is not None:
            row_members[name] = rows
    pricing_lines: dict[int, list[dict[str, Any]]] = {}
    for p in row_members.get("pricing.json") or []:
        lines = _select_optional(sb, "plans_reader_priced_lines", unavailable, pricing_id=p.get("id"))
        if lines is None:
            break
        pricing_lines[int(p.get("version") or 0)] = sorted(lines, key=lambda r: str(r.get("line_id") or ""))

    def sort_key(row: Mapping[str, Any]) -> tuple:
        return (str(row.get("kind") or ""), str(row.get("category") or ""), int(row.get("group_index") or 0),
                int(row.get("pass") or 1), str(row.get("created_at") or ""))

    out.mkdir(parents=True, exist_ok=True)
    removed = clear_export_members(out)
    for sub in EXPORT_DIRS:
        (out / sub).mkdir(exist_ok=True)
    (out / "run.json").write_text(_dump(run))
    (out / "files.json").write_text(_dump(sorted(files, key=lambda r: str(r.get("created_at") or ""))))
    (out / "documents.json").write_text(_dump(sorted(documents, key=lambda r: str(r.get("created_at") or ""))))
    slim_groups = []
    used_names: set[str] = set()
    for g in sorted(groups, key=sort_key):
        g = dict(g)
        output = g.pop("output", None)
        if isinstance(output, Mapping):
            name = group_output_name(g)
            if name in used_names:
                # Two groups share kind, category, index and pass (repeated
                # modify groups): the later one gets its id prefix, recorded
                # on the row so the loader finds it.
                name = f"{name[:-5]}_{str(g.get('id') or '')[:8]}.json"
                g["output_file"] = name
            used_names.add(name)
            (out / "groups" / name).write_text(_dump(output))
            g["output_len"] = g.get("output_len") or len(json.dumps(output))
        slim_groups.append(g)
    (out / "groups.json").write_text(_dump(slim_groups))
    for o in outputs:
        scope = str(o.get("scope") or "category")
        name = output_name(scope, o.get("category"), int(o.get("version") or 1))
        payload = o.get("output") if isinstance(o.get("output"), Mapping) else {}
        (out / "outputs" / name).write_text(_dump(payload))
    slim_deliverables = []
    for d in sorted(deliverables, key=lambda r: (str(r.get("kind")), int(r.get("version") or 1))):
        d = dict(d)
        spec = d.pop("spec_json", None)
        if isinstance(spec, Mapping):
            (out / "deliverables" / deliverable_name(str(d.get("kind")), int(d.get("version") or 1))).write_text(_dump(spec))
            d["spec_len"] = d.get("spec_len") or len(json.dumps(spec))
        slim_deliverables.append(d)
    (out / "deliverables.json").write_text(_dump(slim_deliverables))
    (out / "calls.json").write_text(_dump([strip_images(c) for c in sorted(calls, key=lambda r: str(r.get("created_at") or ""))]))
    (out / "chat.json").write_text(_dump(threads))
    for name, rows in row_members.items():
        (out / name).write_text(_dump(sorted(rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))))
    for version, lines in pricing_lines.items():
        (out / "pricing_lines" / pricing_lines_name(version)).write_text(_dump(lines))
    extra: dict[str, Any] = {"unavailable_tables": unavailable}
    if removed:
        extra["reexport_replaced"] = len(removed)
    meta = bundle_meta(out, case_id=case_id or run.get("eval_case_id"), source="dev_db", run=run, note=note, extra=extra)
    write_bundle_meta(out, meta)
    return out


__all__ = [
    "BUNDLE_SCHEMA", "DEV_REF", "PROD_REF", "DEFAULT_BACKUP_DIR", "SOURCE_KINDS",
    "BundleError", "RunBundle", "load_bundle", "group_output_name", "output_name", "deliverable_name",
    "output_key", "pricing_lines_name", "clear_export_members", "ROW_MEMBERS", "RUN_COLUMNS", "EXPORT_DIRS",
    "strip_images", "check_not_production", "backup_project_file", "segment_pages", "cut_pages",
    "member_paths", "member_sha256s", "bundle_meta", "write_bundle_meta", "verify_members",
    "import_weekend_export", "export_run", "sha256_bytes", "sha256_file", "now_pacific_iso",
]
