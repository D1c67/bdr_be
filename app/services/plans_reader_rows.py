"""Row ids, fingerprints and coverage for AI Estimating category outputs
(docs/AI_ESTIMATING_BUILD_SPEC.md sections 5.1 and 5.4; WS4).

Why this module exists: a category output today is a bag of rows (counts,
runs, schedules, specs, scope, details, questions) with nothing that names a
row, says where it came from, or says whether its number is a whole-sheet
total or only what a tile showed (E11: a tile subtotal became a BOQ total on
7154). Category output v2 adds, by code and never by a model:

- `row_id`: run-unique, never reused (`C<n>` counts, `R<n>` runs, `S<n>`
  schedules, `P<n>` specs, `O<n>` scope, `D<n>` details, `Q<n>` questions),
  issued through a per-run high-water mark (`row_seq`), so a later merge or a
  later category continues the sequence and an id is never handed out twice;
- `fingerprint`: a short identity hash that lets a re-merge keep a row's id;
- `origin {group_id, group_kind, pass, local_id}`;
- `coverage` (sheet_total, region_subtotal, tile_subtotal, partial, schedule,
  stated, derived, unknown) by the rules of the WS4 design paragraph (see
  `infer_coverage`);
- `status` (active, superseded, alternate, unverifiable), `superseded[]` and
  `alternates[]` history, `item_key` (electrical_items), `sheet {document_id,
  page, sheet_number, sheet_key "D1P8"}`, `positions [[x, y]]`,
  `family_tags` and `flags`.

`upgrade_category_output(v1, meta)` turns a stored v1 output (today's runs)
into v2. Legacy escalation patches are applied by the conservative legacy
rule: a patch without a `coverage` field is `inside_tiles_only`, so it can
never replace a first-pass row; it lands in that row's `alternates` (and can
only raise it, flagging `needs_recount`) or, with no target, becomes a new
`tile_subtotal` row. Every first-pass row therefore stays active. A
whole-sheet patch (model `whole_sheet` and complete containment) replaces its
target only when it does not lower it; a lower one becomes an alternate and
flags the target `needs_recount`, since lowering is a WS7 recount outcome.

`iter_rows(outputs)` is the one reader every consumer uses: it yields v2 rows
from v1 or v2 category outputs (v1 ones get transient ids that continue one
run-level sequence) and BOQ lines (`B<n>`, transient ids in BOQ order for
legacy specs without `line_id`).

Pure: no database, no model, no network. Imports only the S3 contracts.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.electrical_items import (
    BoqRow,
    count_key,
    is_linear,
    linear_evidence_required,
    normalize_description,
    parse_item,
    similarity,
)
from app.services.estimating_contracts import (
    BOQ_LINE_PREFIX,
    ROW_COVERAGES,
    ROW_ID_PREFIXES,
    ContractError,
    is_row_id,
    make_row_id,
    parse_row_id,
)
from app.services.plans_reader_schemas import CATEGORIES

logger = logging.getLogger(__name__)

ROWS_VERSION = 2

# Category output arrays that carry rows, in kind order, with their id prefix.
ROW_KINDS: tuple[tuple[str, str], ...] = (
    ("counts", "C"),
    ("runs", "R"),
    ("schedules", "S"),
    ("specs", "P"),
    ("scope", "O"),
    ("details", "D"),
    ("questions", "Q"),
)
PREFIX_BY_KEY: dict[str, str] = dict(ROW_KINDS)
KEY_BY_PREFIX: dict[str, str] = {p: k for k, p in ROW_KINDS}

# MeasureOutput v1 (WS3) quantity rows: `quantities[{row_id "M<n>", ...}]`.
MEASURE_PREFIX = "M"
MEASURE_KEY = "quantities"
assert MEASURE_PREFIX in ROW_ID_PREFIXES

# Coverage values that are a floor, never a total (gate partial_basis; the
# add_boq_line refusal partial_requires_floor).
INCOMPLETE_COVERAGES: frozenset[str] = frozenset({"tile_subtotal", "partial"})
assert INCOMPLETE_COVERAGES <= set(ROW_COVERAGES)

# A sub row whose notes or location say it is not a whole-sheet count.
SUBTOTAL_RE = re.compile(
    r"(?i)\b(tile|region|partial)\s+(sub)?total\b|\bsubtotal only\b|"
    r"\bnot a complete sheet total\b|\binside the tiles? only\b|\bclipped\b.*\bexcluded\b"
)

# Escalation and recount rows: the model's own coverage claim (WS4 schema).
MODEL_COVERAGES: tuple[str, ...] = ("whole_sheet", "inside_tiles_only")
LEGACY_MODEL_COVERAGE = "inside_tiles_only"

# Tiles whose union covers at least this share of the page count as the whole
# sheet (the scanned-page rule; also a sub page read through tiles).
TILE_COVERAGE_MIN = 0.98

# Similarity at which a legacy "corrects first pass: <old>" patch finds its
# target on the sheet (electrical_items.MATCH_THRESHOLD).
PATCH_MATCH_THRESHOLD = 0.72
PATCH_MATCH_MARGIN = 0.05

SHEET_KEY_RE = re.compile(r"^D([1-9][0-9]*)P([1-9][0-9]*)$")
_CORRECTS_RE = re.compile(r"(?i)corrects first pass:\s*(.+)")
_NUM = r"(\d*\.\d+|\d+(?:\.\d+)?)"
_POINT_RE = re.compile(r"\(\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*\)")
_DEMO_X_RE = re.compile(r"\(\s*X\s*\)")
_RELOCATE_RE = re.compile(r"\(\s*(?:R|RL|ER)\s*\)")
_WS_RE = re.compile(r"\s+")

GROUP_KINDS: tuple[str, ...] = ("sub", "rollup", "escalation", "recount", "measure")
RAISED_REASON = "raised by a patch floor"
# Set by plans_reader_merge.concat_outputs on every row of a concatenated
# multi-group output: the id of the sub group the row came from. The upgrade
# reads it (origin group, page item and local c-id come from that group, so
# two groups that read the same page keep their own numbering) and removes it.
ORIGIN_KEY = "_origin_group_id"
_FIRST_PASS_KINDS = frozenset({"sub", "rollup"})


# Small helpers -------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _page_key(document_id: Any, page: Any) -> tuple[str, int]:
    return (str(document_id or ""), _as_int(page) or 0)


def normalize_sheet_number(value: Any) -> str:
    """Comparison form of a sheet number: upper case, no spaces or dashes
    (`E-101` == `E101`). Stored values keep their printed form."""
    return re.sub(r"[\s\-_]+", "", str(value or "")).upper()


def sheet_key(doc_number: int | None, page: int | None) -> str | None:
    """`D<n>P<page>` with n the run-level document number (run_doc_order)."""
    if not doc_number or not page or doc_number < 1 or page < 1:
        return None
    return f"D{int(doc_number)}P{int(page)}"


def parse_sheet_key(value: Any) -> tuple[int, int]:
    m = SHEET_KEY_RE.match(str(value or ""))
    if not m:
        raise ContractError(f"not a sheet key: {value!r}")
    return int(m.group(1)), int(m.group(2))


def doc_order(documents: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """{document_id: n}, the D1..Dn numbering of every main and modify call.
    Mirrors plans_reader_agents.run_doc_order (category vocabulary order,
    then created_at, then id) without importing the agents module."""
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    ordered = sorted(
        (d for d in documents if isinstance(d, Mapping) and d.get("id")),
        key=lambda d: (
            rank.get(d.get("category"), len(rank)),
            str(d.get("created_at") or ""),
            str(d.get("id") or ""),
        ),
    )
    return {str(d["id"]): n for n, d in enumerate(ordered, start=1)}


def parse_positions(text: Any) -> list[list[float]]:
    """Page-fraction points written as `(x,y)` in a location string; values
    outside 0..1 are not page fractions and are skipped."""
    out: list[list[float]] = []
    for m in _POINT_RE.finditer(str(text or "")):
        x, y = float(m.group(1)), float(m.group(2))
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            out.append([round(x, 4), round(y, 4)])
    return out


def _points(value: Any) -> list[list[float]]:
    """Model `points` ([[x, y], ...], WS4 COUNT_ITEM) kept when they are page
    fractions, rounded to 4 decimals."""
    out: list[list[float]] = []
    for p in value or [] if isinstance(value, list) else []:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            x, y = _as_float(p[0]), _as_float(p[1])
            if x is not None and y is not None and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                out.append([round(x, 4), round(y, 4)])
    return out


def tiles_union_fraction(tiles: Any) -> float:
    """Area share of the page covered by the union of tile boxes
    [x0, y0, x1, y1] (page fractions, clipped to the page)."""
    boxes: list[tuple[float, float, float, float]] = []
    for t in tiles or []:
        if not isinstance(t, (list, tuple)) or len(t) != 4:
            continue
        try:
            x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in t)
        except (TypeError, ValueError):
            continue
        if x1 > x0 and y1 > y0:
            boxes.append((x0, y0, x1, y1))
    if not boxes:
        return 0.0
    xs = sorted({b[0] for b in boxes} | {b[2] for b in boxes})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((b[1], b[3]) for b in boxes if b[0] <= left and b[2] >= right)
        covered, cur_lo, cur_hi = 0.0, None, None
        for lo, hi in spans:
            if cur_hi is None or lo > cur_hi:
                if cur_hi is not None:
                    covered += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
            else:
                cur_hi = max(cur_hi, hi)
        if cur_hi is not None:
            covered += cur_hi - cur_lo
        area += covered * (right - left)
    return min(1.0, area)


def containment_complete(containment: Any) -> bool:
    """True when every text target of the row's family lay inside the tiles
    (`{targets, inside}` or `{complete: true}`); no targets is not complete."""
    if not isinstance(containment, Mapping):
        return False
    if containment.get("complete") is True:
        return True
    targets = _as_int(containment.get("targets"))
    inside = _as_int(containment.get("inside"))
    return bool(targets and targets > 0 and inside is not None and inside >= targets)


# Row ids --------------------------------------------------------------------------


class RowSeq:
    """Per-run high-water marks of the row id sequences. `next(prefix)` never
    returns an id that was issued or observed before."""

    def __init__(self, marks: Mapping[str, Any] | None = None) -> None:
        self.marks: dict[str, int] = {}
        for prefix, n in (marks or {}).items():
            if prefix in ROW_ID_PREFIXES and _as_int(n):
                self.marks[prefix] = max(self.marks.get(prefix, 0), int(n))

    def observe(self, row_id: Any) -> None:
        if not is_row_id(row_id):
            return
        prefix, n = parse_row_id(row_id)
        if n > self.marks.get(prefix, 0):
            self.marks[prefix] = n

    def next(self, prefix: str) -> str:
        n = self.marks.get(prefix, 0) + 1
        row_id = make_row_id(prefix, n)
        self.marks[prefix] = n
        return row_id

    def as_dict(self) -> dict[str, int]:
        return {p: self.marks[p] for p in sorted(self.marks) if self.marks[p] > 0}

    @classmethod
    def from_outputs(cls, outputs: Iterable[Mapping[str, Any]]) -> RowSeq:
        seq = cls()
        for out in outputs:
            if not isinstance(out, Mapping):
                continue
            for prefix, n in (out.get("row_seq") or {}).items():
                if prefix in ROW_ID_PREFIXES and _as_int(n):
                    seq.marks[prefix] = max(seq.marks.get(prefix, 0), int(n))
            for key, _prefix in ROW_KINDS:
                for row in out.get(key) or []:
                    if isinstance(row, Mapping):
                        seq.observe(row.get("row_id"))
        return seq


# Identity ---------------------------------------------------------------------------


def row_text(kind_key: str, row: Mapping[str, Any]) -> str:
    """The text that names a row of this kind (for fingerprints and search)."""
    if kind_key == "counts":
        return _text(row.get("description"))
    if kind_key == "runs":
        parts = [row.get("description"), row.get("conduit_size"), row.get("conduit_type"),
                 row.get("conductors"), row.get("from_location"), row.get("to_location")]
        return _text(" ".join(str(p) for p in parts if p))
    if kind_key == "schedules":
        return _text(f"{row.get('schedule_type') or ''} {row.get('name') or ''}")
    if kind_key == "specs":
        return _text(f"{row.get('section') or ''} {row.get('item') or ''}")
    if kind_key == "scope":
        return _text(f"{row.get('section') or ''} {row.get('statement') or ''}")
    if kind_key == "details":
        return _text(f"{row.get('detail_ref') or ''} {row.get('title') or ''}")
    if kind_key == "questions":
        return _text(row.get("question"))
    return _text(row.get("description"))


def row_unit(kind_key: str, row: Mapping[str, Any]) -> str:
    if kind_key == "counts":
        return str(row.get("unit") or "EA").upper()
    if kind_key == "runs":
        return "FT"
    return ""


def item_key_for(kind_key: str, row: Mapping[str, Any]) -> str | None:
    """electrical_items identity: count_key for counts, item_key for runs,
    None for rows that are not quantities."""
    if kind_key == "counts":
        desc = _text(row.get("description"))
        tag = _text(row.get("symbol_or_tag"))
        identity = parse_item(desc, row_unit(kind_key, row))
        if identity.item_class == "other" and tag:
            identity = parse_item(f"{tag} {desc}", row_unit(kind_key, row))
        return count_key(identity)
    if kind_key == "runs":
        return parse_item(_text(row.get("description")), "FT").item_key
    return None


def fingerprint(category: str | None, kind_key: str, row: Mapping[str, Any], item_key: str | None = None) -> str:
    """sha1(category|document_id|page|kind|item_key|unit|norm(text)|norm(tag))[:12].
    Quantity is not part of it: a recount keeps the row's id."""
    if item_key is None:
        item_key = item_key_for(kind_key, row)
    parts = [
        str(category or ""),
        str(row.get("document_id") or ""),
        str(_as_int(row.get("page")) or 0),
        kind_key,
        item_key or "",
        row_unit(kind_key, row),
        normalize_description(row_text(kind_key, row)),
        normalize_description(row.get("symbol_or_tag") or ""),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def tag_families(row: Mapping[str, Any], key: str | None) -> list[str]:
    """Census families a count row belongs to from its printed tag
    (`demo_x@D1P8`, `relocate_r@D1P8`). plans_reader_floors refines these."""
    if not key:
        return []
    tag = str(row.get("symbol_or_tag") or "")
    out = []
    if _DEMO_X_RE.search(tag):
        out.append(f"demo_x@{key}")
    if _RELOCATE_RE.search(tag):
        out.append(f"relocate_r@{key}")
    return out


# Coverage -----------------------------------------------------------------------------


def infer_coverage(
    kind_key: str,
    row: Mapping[str, Any],
    *,
    group_kind: str = "sub",
    page_item: Mapping[str, Any] | None = None,
    text_layer: bool | None = None,
    model_coverage: str | None = None,
) -> str:
    """Coverage of one row (spec WS4 design paragraph):

    - sub rows (and roll-up rows, which re-emit sub rows) are `sheet_total`
      unless their notes or location match SUBTOTAL_RE (then `partial`);
      schedule_text, stated_quantity and inferred counts are `schedule`,
      `stated`, `derived`; a sub page read through tiles that cover less than
      TILE_COVERAGE_MIN of the page is `tile_subtotal`;
    - escalation and recount rows are `sheet_total` only when the model said
      `whole_sheet` AND containment is complete (every text target of the
      family inside the tiles; on a page without a text layer, the tiles
      cover TILE_COVERAGE_MIN of the page); otherwise `tile_subtotal`;
    - legacy patches without a coverage field are `inside_tiles_only`, so
      `tile_subtotal`.
    Non-count rows: schedules `schedule`; specs and scope `stated`; details
    and questions `unknown`; runs by basis (stated, assumed -> derived,
    scaled_from_drawing like a counted row).
    """
    if kind_key == "schedules":
        return "schedule"
    if kind_key in ("specs", "scope"):
        return "stated"
    if kind_key in ("details", "questions"):
        return "unknown"
    basis = str(row.get("basis") or "")
    if kind_key == "runs":
        if basis == "stated":
            return "stated"
        if basis == "assumed":
            return "derived"
    note_text = f"{row.get('notes') or ''} {row.get('location') or ''}"
    says_partial = bool(SUBTOTAL_RE.search(note_text))
    tiles = (page_item or {}).get("tiles") if isinstance(page_item, Mapping) else None
    if group_kind in _FIRST_PASS_KINDS:
        if says_partial:
            return "partial"
        if basis == "schedule_text":
            return "schedule"
        if basis == "stated_quantity":
            return "stated"
        if basis == "inferred":
            return "derived"
        if tiles and tiles_union_fraction(tiles) < TILE_COVERAGE_MIN:
            return "tile_subtotal"
        return "sheet_total"
    # escalation, recount, measure: what a tile shows is a floor unless proven whole.
    if says_partial:
        return "tile_subtotal"
    if basis == "schedule_text":
        return "schedule"
    if basis == "stated_quantity":
        return "stated"
    if basis == "inferred":
        return "derived"
    claim = model_coverage if model_coverage is not None else row.get("coverage")
    if claim not in MODEL_COVERAGES:
        claim = LEGACY_MODEL_COVERAGE
    if claim != "whole_sheet":
        return "tile_subtotal"
    item = page_item if isinstance(page_item, Mapping) else {}
    if text_layer is False:
        complete = tiles_union_fraction(item.get("tiles")) >= TILE_COVERAGE_MIN
    else:
        complete = containment_complete(item.get("containment"))
    return "sheet_total" if complete else "tile_subtotal"


# Upgrade meta ---------------------------------------------------------------------------


def _key_from_any(key: Any) -> tuple[str, int] | None:
    if isinstance(key, tuple) and len(key) == 2:
        return _page_key(key[0], key[1])
    if isinstance(key, str) and ":" in key:
        doc, _, page = key.rpartition(":")
        if _as_int(page):
            return _page_key(doc, page)
    return None


@dataclass
class RowsMeta:
    """What the upgrade knows beyond the output itself (all optional):
    the run's group rows (kind, id, pass, pages, category, output), its
    documents (for the D-numbering), the ledger (sheet_index sheet numbers),
    explicit sheet numbers and text-layer presence per (document_id, page)."""

    groups: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    ledger: dict = field(default_factory=dict)
    sheet_numbers: dict[tuple[str, int], str] = field(default_factory=dict)
    text_layer: dict[tuple[str, int], bool] = field(default_factory=dict)

    @classmethod
    def coerce(cls, meta: Any) -> RowsMeta:
        if isinstance(meta, RowsMeta):
            return meta
        if meta is None:
            return cls()
        if isinstance(meta, (list, tuple)):
            return cls(groups=[g for g in meta if isinstance(g, Mapping)])
        if not isinstance(meta, Mapping):
            raise ContractError("rows meta must be a mapping, a list of groups or None")
        sheet_numbers: dict[tuple[str, int], str] = {}
        for k, v in (meta.get("sheet_numbers") or {}).items():
            pk = _key_from_any(k)
            if pk and v:
                sheet_numbers[pk] = str(v)
        text_layer: dict[tuple[str, int], bool] = {}
        for k, v in (meta.get("text_layer") or {}).items():
            pk = _key_from_any(k)
            if pk is not None and isinstance(v, bool):
                text_layer[pk] = v
        return cls(
            groups=[dict(g) for g in meta.get("groups") or [] if isinstance(g, Mapping)],
            documents=[dict(d) for d in meta.get("documents") or [] if isinstance(d, Mapping)],
            ledger=dict(meta.get("ledger") or {}),
            sheet_numbers=sheet_numbers,
            text_layer=text_layer,
        )

    def doc_numbers(self) -> dict[str, int]:
        return doc_order(self.documents) if self.documents else {}

    def authoritative_sheet_numbers(self) -> dict[tuple[str, int], str]:
        out: dict[tuple[str, int], str] = {}
        for entry in self.ledger.get("sheet_index") or []:
            if isinstance(entry, Mapping) and entry.get("sheet_number"):
                out[_page_key(entry.get("document_id"), entry.get("page"))] = str(entry["sheet_number"])
        out.update(self.sheet_numbers)
        return out

    def category_groups(self, category: str | None, kinds: Iterable[str]) -> list[dict]:
        wanted = set(kinds)
        rows = [
            g for g in self.groups
            if g.get("kind") in wanted and (category is None or g.get("category") == category)
        ]
        return sorted(rows, key=lambda g: (str(g.get("created_at") or ""), _as_int(g.get("group_index")) or 0, str(g.get("id") or "")))


def _page_items(group: Mapping[str, Any]) -> dict[tuple[str, int], dict]:
    out: dict[tuple[str, int], dict] = {}
    for item in group.get("pages") or []:
        if isinstance(item, Mapping):
            out[_page_key(item.get("document_id"), item.get("page"))] = dict(item)
    return out


# Upgrade ------------------------------------------------------------------------------------


def is_v2(output: Any) -> bool:
    return isinstance(output, Mapping) and _as_int(output.get("rows_version")) == ROWS_VERSION


def _quantity_ok(kind_key: str, row: Mapping[str, Any]) -> bool:
    key = "quantity" if kind_key == "counts" else "length_ft" if kind_key == "runs" else None
    if key is None:
        return True
    value = _as_float(row.get(key))
    return value is not None and math.isfinite(value) and value >= 0


def _snapshot(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    snap = {
        "quantity": row.get("quantity"),
        "basis": row.get("basis"),
        "coverage": row.get("coverage"),
        "origin": copy.deepcopy(row.get("origin")),
        "description": row.get("description"),
        "reason": reason,
    }
    if row.get("length_ft") is not None:
        snap["length_ft"] = row.get("length_ft")
    return snap


def _add_flag(row: dict, flag: str) -> None:
    flags = row.setdefault("flags", [])
    if flag not in flags:
        flags.append(flag)


class _Builder:
    def __init__(self, output: Mapping[str, Any], meta: RowsMeta, category: str | None) -> None:
        self.output = output
        self.meta = meta
        self.category = category
        self.docs = meta.doc_numbers()
        if not self.docs:
            # Fall back to the output's own numbering (category-local D-numbers).
            for key, _prefix in ROW_KINDS + (("pages", ""),):
                for row in output.get(key) or []:
                    if isinstance(row, Mapping) and row.get("document_id") and _as_int(row.get("doc_index")):
                        self.docs.setdefault(str(row["document_id"]), int(row["doc_index"]))
        self.sheet_truth = meta.authoritative_sheet_numbers()
        self.page_sheet: dict[tuple[str, int], str] = {}
        for page in output.get("pages") or []:
            if isinstance(page, Mapping) and page.get("sheet_number"):
                self.page_sheet[_page_key(page.get("document_id"), page.get("page"))] = str(page["sheet_number"])
        self.first_pass = meta.category_groups(category, ("sub",))
        self.escalations = meta.category_groups(category, ("escalation", "recount"))
        self.rolled_up = bool((output.get("source") or {}).get("rolled_up"))

    def origin_group(self, raw: Mapping[str, Any], key: tuple[str, int]) -> tuple[dict | None, dict | None]:
        """The sub group a stamped row came from (ORIGIN_KEY) and its page
        item; else the first sub group that read the page."""
        gid = raw.get(ORIGIN_KEY)
        if gid is not None:
            for g in self.first_pass:
                if str(g.get("id")) == str(gid):
                    return g, _page_items(g).get(key)
        return self.reading_group(key)

    def reading_group(self, key: tuple[str, int]) -> tuple[dict | None, dict | None]:
        for g in self.first_pass:
            items = _page_items(g)
            if key in items:
                return g, items[key]
        return None, None

    def sheet(self, row: Mapping[str, Any]) -> tuple[dict, bool]:
        key = _page_key(row.get("document_id"), row.get("page"))
        printed = row.get("sheet_number")
        truth = self.sheet_truth.get(key)
        repaired = False
        if truth:
            if printed and normalize_sheet_number(printed) != normalize_sheet_number(truth):
                repaired = True
            number = truth
        else:
            number = printed or self.page_sheet.get(key)
        return (
            {
                "document_id": key[0] or None,
                "page": key[1] or None,
                "sheet_number": number or None,
                "sheet_key": sheet_key(self.docs.get(key[0]), key[1]),
            },
            repaired,
        )

    def decorate(
        self,
        kind_key: str,
        raw: Mapping[str, Any],
        *,
        group: Mapping[str, Any] | None,
        group_kind: str,
        page_item: Mapping[str, Any] | None,
        local_id: str | None,
    ) -> dict:
        row = copy.deepcopy(dict(raw))
        row.pop(ORIGIN_KEY, None)
        key = _page_key(row.get("document_id"), row.get("page"))
        sheet, repaired = self.sheet(row)
        ikey = item_key_for(kind_key, row)
        row["row_id"] = row.get("row_id") if is_row_id(row.get("row_id"), PREFIX_BY_KEY[kind_key]) else None
        row["fingerprint"] = fingerprint(self.category, kind_key, row, ikey)
        row["origin"] = {
            "group_id": str(group["id"]) if group and group.get("id") else None,
            "group_kind": group_kind,
            "pass": _as_int((group or {}).get("pass")) or (1 if group_kind in _FIRST_PASS_KINDS else 2),
            "local_id": local_id,
        }
        model_cov = row.pop("coverage", None) if kind_key == "counts" else None
        if model_cov is not None:
            row["model_coverage"] = model_cov  # the model's claim; `coverage` is code's
        row["coverage"] = infer_coverage(
            kind_key,
            row,
            group_kind=group_kind,
            page_item=page_item,
            text_layer=self.meta.text_layer.get(key),
            model_coverage=model_cov if model_cov in MODEL_COVERAGES else None,
        )
        row["status"] = "active"
        row["superseded"] = list(row.get("superseded") or [])
        row["alternates"] = list(row.get("alternates") or [])
        row["item_key"] = ikey
        row["sheet"] = sheet
        positions: list[list[float]] = []
        if kind_key == "counts":
            positions = parse_positions(row.get("location"))
            points = _points(row.get("points"))
            if points:
                positions = points
        elif kind_key == "runs":
            positions = parse_positions(f"{row.get('from_location') or ''} {row.get('to_location') or ''}")
        row["positions"] = positions
        row["family_tags"] = tag_families(row, sheet["sheet_key"]) if kind_key == "counts" else []
        row["flags"] = list(row.get("flags") or [])
        if repaired:
            _add_flag(row, "sheet_number_repaired")
            row["sheet_number_printed"] = raw.get("sheet_number")
            row["sheet_number"] = sheet["sheet_number"]
        if not _quantity_ok(kind_key, row):
            row["status"] = "unverifiable"
            _add_flag(row, "bad_quantity")
        if self.first_pass and group is None and group_kind in _FIRST_PASS_KINDS:
            row["status"] = "unverifiable"
            _add_flag(row, "page_not_read")
        if group is not None and page_item is None:
            row["status"] = "unverifiable"
            _add_flag(row, "page_not_delivered")
        if row["coverage"] in INCOMPLETE_COVERAGES:
            _add_flag(row, "needs_recount")
        if kind_key == "counts" and row_unit(kind_key, row) != "FT":
            identity = parse_item(_text(row.get("description")), row_unit(kind_key, row))
            if is_linear(identity):
                _add_flag(row, "linear_in_ea")
            elif linear_evidence_required(identity):
                _add_flag(row, "linear_luminaire_ea")
        return row

    def escalation_group(self, patch: Mapping[str, Any], index: int) -> tuple[dict | None, dict | None]:
        keys = [
            _page_key(p.get("document_id"), p.get("page"))
            for p in patch.get("pages") or [] if isinstance(p, Mapping)
        ]
        if not keys:
            for kind_key, _prefix in ROW_KINDS:
                for r in patch.get(kind_key) or []:
                    if isinstance(r, Mapping):
                        keys.append(_page_key(r.get("document_id"), r.get("page")))
        for key in keys:
            for g in self.escalations:
                items = _page_items(g)
                if key in items:
                    return g, items[key]
        if keys and index < len(self.escalations):
            g = self.escalations[index]
            return g, _page_items(g).get(keys[0])
        return None, None


def _corrects_text(notes: Any) -> str | None:
    m = _CORRECTS_RE.search(str(notes or ""))
    if not m:
        return None
    text = m.group(1)
    cut = len(text)
    for sep in (";", ". "):
        i = text.find(sep)
        if i != -1:
            cut = min(cut, i)
    return text[:cut].strip().rstrip(".").strip() or None


def _boq_row(description: Any, unit: Any) -> BoqRow:
    return BoqRow("", "", "", "", str(description or ""), 0.0, str(unit or ""))


def find_patch_target(
    patch_row: Mapping[str, Any], sheet_rows: list[dict], *, source_group_id: str | None = None
) -> tuple[list[dict], str]:
    """Candidates a count patch targets among the active first-pass rows of
    its sheet, and how they were found: `supersedes_row_id` (a run-level row
    id, or a local c-id numbered within the sub group's own output, so a
    local id only names a row of `source_group_id` when the escalation page
    says which group it re-reads), else the exact normalized description and unit,
    else a legacy `corrects first pass: <old>` note (exact, then the unique
    best similarity >= PATCH_MATCH_THRESHOLD). Returns ([], "none") when no
    target is found; several candidates mean an ambiguous patch."""
    unit = str(patch_row.get("unit") or "EA").upper()
    live = [r for r in sheet_rows if r.get("status") == "active" and str(r.get("unit") or "EA").upper() == unit]
    sid = patch_row.get("supersedes_row_id")
    if sid:
        hit = [r for r in live if r.get("row_id") == sid]
        if not hit:
            hit = [
                r for r in live
                if (r.get("origin") or {}).get("local_id") == sid
                and (source_group_id is None or (r.get("origin") or {}).get("group_id") == str(source_group_id))
            ]
        if hit:
            return hit[:1], "supersedes_row_id"
    norm = normalize_description(patch_row.get("description"))
    exact = [r for r in live if normalize_description(r.get("description")) == norm]
    if exact:
        return exact, "description"
    old = _corrects_text(patch_row.get("notes"))
    if old:
        old_norm = normalize_description(old)
        exact = [r for r in live if normalize_description(r.get("description")) == old_norm]
        if exact:
            return exact, "corrects_text"
        target = _boq_row(old, unit)
        scored = sorted(
            ((similarity(target, _boq_row(r.get("description"), unit)), i) for i, r in enumerate(live)),
            reverse=True,
        )
        above = [(s, i) for s, i in scored if s >= PATCH_MATCH_THRESHOLD]
        if len(above) == 1:
            return [live[above[0][1]]], "corrects_similarity"
        if len(above) > 1:
            if above[0][0] - above[1][0] >= PATCH_MATCH_MARGIN:
                return [live[above[0][1]]], "corrects_similarity"
            return [live[i] for _s, i in above], "corrects_similarity"
    return [], "none"


def _apply_count_patch(rows: list[dict], patch: dict, log: list[dict], *, source_group_id: str | None = None) -> None:
    """Apply one count patch row to the category's count rows (in place).
    `source_group_id` is the sub group the escalation re-reads (its page
    item's `source_group_id`). The log entry keeps a reference to the row
    the patch landed on under `_target` until ids are assigned."""
    sheet = (patch.get("document_id"), patch.get("page"))
    on_sheet = [
        r for r in rows
        if (r.get("document_id"), r.get("page")) == sheet
        and (r.get("origin") or {}).get("group_kind") in _FIRST_PASS_KINDS
    ]
    candidates, how = find_patch_target(patch, on_sheet, source_group_id=source_group_id)
    entry: dict[str, Any] = {
        "patch_row": {
            "description": patch.get("description"),
            "quantity": patch.get("quantity"),
            "sheet_key": (patch.get("sheet") or {}).get("sheet_key"),
            "origin": patch.get("origin"),
        },
        "matched_by": how,
        "coverage": patch.get("coverage"),
    }
    log.append(entry)
    if len(candidates) == 1:
        target = candidates[0]
        pq, tq = _as_float(patch.get("quantity")), _as_float(target.get("quantity"))
        whole_sheet = patch.get("coverage") == "sheet_total" and patch.get("status") == "active"
        if whole_sheet and pq is not None and tq is not None and pq >= tq:
            # A whole-sheet patch replaces its target only when it does not
            # lower it; lowering is a WS7 recount outcome, never a patch effect.
            replacement = dict(patch)
            replacement["superseded"] = list(target.get("superseded") or []) + [
                _snapshot(target, "superseded by a whole-sheet patch")
            ]
            replacement["alternates"] = list(target.get("alternates") or [])
            replacement["row_id"] = target.get("row_id")
            replacement["_slot_of"] = target.get("fingerprint")
            rows[rows.index(target)] = replacement
            entry["action"] = "replaced"
            entry["_target"] = replacement
            return
        target.setdefault("alternates", []).append(
            {
                "quantity": patch.get("quantity"),
                "basis": patch.get("basis"),
                "coverage": patch.get("coverage"),
                "origin": patch.get("origin"),
                "description": patch.get("description"),
                "note": str(patch.get("notes") or "")[:400] or None,
            }
        )
        if pq is not None and tq is not None and pq > tq:
            # A floor can raise a row, never lower it.
            target.setdefault("superseded", []).append(_snapshot(target, RAISED_REASON))
            target["quantity"] = patch.get("quantity")
            target["coverage"] = "partial"
            _add_flag(target, "needs_recount")
            entry["action"] = "raised"
        else:
            if whole_sheet and (pq is None or tq is None or pq < tq):
                # A whole-sheet patch that would lower (or cannot be compared)
                # stays an alternate; the disagreement goes to the WS7 recount.
                _add_flag(target, "needs_recount")
            entry["action"] = "alternate"
        entry["_target"] = target
        return
    if candidates:
        _add_flag(patch, "ambiguous_patch")
        _add_flag(patch, "needs_recount")
        entry["action"] = "added_ambiguous"
    else:
        if patch.get("coverage") != "sheet_total":
            _add_flag(patch, "needs_recount")
        entry["action"] = "added"
    rows.append(patch)
    entry["_target"] = patch


def _prior_ids(prior: Any) -> dict[str, list[str]]:
    """fingerprint -> [row ids] from a previous v2 output (or a mapping)."""
    out: dict[str, list[str]] = {}
    if prior is None:
        return out
    if isinstance(prior, Mapping) and not any(k in prior for k, _p in ROW_KINDS):
        for fp, ids in prior.items():
            if isinstance(ids, str):
                out[str(fp)] = [ids]
            elif isinstance(ids, (list, tuple)):
                out[str(fp)] = [str(i) for i in ids]
        return out
    if isinstance(prior, Mapping):
        for key, _prefix in ROW_KINDS:
            for row in prior.get(key) or []:
                if isinstance(row, Mapping) and row.get("fingerprint") and is_row_id(row.get("row_id")):
                    out.setdefault(str(row["fingerprint"]), []).append(str(row["row_id"]))
    return out


def _order(builder: _Builder, row: Mapping[str, Any], phase: int, index: int) -> tuple:
    doc = str(row.get("document_id") or "")
    return (builder.docs.get(doc, 10**6), doc, _as_int(row.get("page")) or 0, phase, index)


def upgrade_category_output(
    output: Mapping[str, Any],
    meta: Any = None,
    *,
    category: str | None = None,
    row_seq: RowSeq | None = None,
    prior: Any = None,
) -> dict:
    """Category output v1 -> v2 (spec 5.4). `meta` is a RowsMeta or a mapping
    {groups, documents, ledger, sheet_numbers, text_layer} (all optional; see
    RowsMeta). `row_seq` continues a run-level sequence (mutated in place),
    `prior` (a previous v2 output or {fingerprint: id}) keeps ids stable on
    a re-merge. A v2 output is returned as a deep copy, its ids observed.

    Never lowers and never drops: every first-pass row stays active; legacy
    escalation patches become alternates (raising their target at most) or
    new tile_subtotal rows; exact duplicates are not removed here.
    """
    if not isinstance(output, Mapping):
        raise ContractError("a category output must be a mapping")
    seq = row_seq if row_seq is not None else RowSeq()
    if is_v2(output):
        done = copy.deepcopy(dict(output))
        for prefix, n in (done.get("row_seq") or {}).items():
            if prefix in ROW_ID_PREFIXES and _as_int(n):
                seq.marks[prefix] = max(seq.marks.get(prefix, 0), int(n))
        for key, _prefix in ROW_KINDS:
            for row in done.get(key) or []:
                if isinstance(row, Mapping):
                    seq.observe(row.get("row_id"))
        done["row_seq"] = seq.as_dict()
        return done

    m = RowsMeta.coerce(meta)
    cat = category or output.get("category")
    b = _Builder(output, m, cat)
    out: dict[str, Any] = {
        k: copy.deepcopy(v) for k, v in output.items() if k not in PREFIX_BY_KEY and k != "escalation_patches"
    }
    inputs: dict[str, int] = {}
    built: dict[str, list[dict]] = {key: [] for key, _p in ROW_KINDS}
    order_of: dict[int, tuple] = {}

    # 1. first-pass rows (the sub groups, or the roll-up that re-emitted them)
    for kind_key, prefix in ROW_KINDS:
        locals_seen: dict[tuple[str, int], int] = {}
        raw_rows = [r for r in output.get(kind_key) or [] if isinstance(r, Mapping)]
        inputs[kind_key] = len(raw_rows)
        for index, raw in enumerate(raw_rows):
            key = _page_key(raw.get("document_id"), raw.get("page"))
            group, item = b.origin_group(raw, key)
            locals_seen[key] = locals_seen.get(key, 0) + 1
            local_id = _local_id_from_group(group, kind_key, raw, key) or f"{prefix.lower()}{locals_seen[key]}"
            gkind = "sub" if group is not None or not b.rolled_up else "rollup"
            row = b.decorate(kind_key, raw, group=group, group_kind=gkind, page_item=item, local_id=local_id)
            built[kind_key].append(row)
            order_of[id(row)] = _order(b, row, 0, index)

    # 2. legacy escalation patches, applied by code
    patch_log: list[dict] = []
    for p_index, patch in enumerate(output.get("escalation_patches") or []):
        if not isinstance(patch, Mapping):
            continue
        group, item = b.escalation_group(patch, p_index)
        gkind = str((group or {}).get("kind") or "escalation")
        for kind_key, prefix in ROW_KINDS:
            raw_rows = [r for r in patch.get(kind_key) or [] if isinstance(r, Mapping)]
            inputs[kind_key] = inputs.get(kind_key, 0) + len(raw_rows)
            for index, raw in enumerate(raw_rows, start=1):
                row_item = item
                if group is not None:
                    row_item = _page_items(group).get(_page_key(raw.get("document_id"), raw.get("page")))
                row = b.decorate(
                    kind_key, raw, group=group, group_kind=gkind, page_item=row_item,
                    local_id=f"p{p_index + 1}.{prefix.lower()}{index}",
                )
                order = _order(b, row, 1, p_index * 1000 + index)
                rows = built[kind_key]
                if kind_key != "counts":
                    rows.append(row)
                    order_of[id(row)] = order
                    continue
                before = [id(r) for r in rows]
                source = (row_item or {}).get("source_group_id") if isinstance(row_item, Mapping) else None
                _apply_count_patch(rows, row, patch_log, source_group_id=str(source) if source else None)
                for i, r in enumerate(rows):
                    if id(r) not in order_of:
                        # a replacement keeps its target's place; an added row sorts after the sheet's first pass
                        order_of[id(r)] = order_of[before[i]] if i < len(before) else order
        _merge_patch_pages(out, patch)

    # 3. ids: prior fingerprints first, then the run-level sequence
    prior_ids = _prior_ids(prior)
    for ids in prior_ids.values():
        for rid in ids:
            seq.observe(rid)
    used: set[str] = set()
    alternates = superseded = 0
    outputs_count: dict[str, int] = {}
    for kind_key, prefix in ROW_KINDS:
        ordered = sorted(built[kind_key], key=lambda r: order_of[id(r)])
        for row in ordered:
            rid = row.get("row_id")
            fp = row.pop("_slot_of", None) or row["fingerprint"]
            if not (is_row_id(rid, prefix) and rid not in used):
                rid = None
                for cand in prior_ids.get(fp, []):
                    if cand not in used and is_row_id(cand, prefix):
                        rid = cand
                        break
            if rid is None:
                rid = seq.next(prefix)
            used.add(rid)
            seq.observe(rid)
            row["row_id"] = rid
            alternates += len(row.get("alternates") or [])
            superseded += sum(1 for x in row.get("superseded") or [] if x.get("reason") != RAISED_REASON)
        out[kind_key] = ordered
        outputs_count[kind_key] = sum(1 for r in ordered if r.get("status") == "active")
    for entry in patch_log:
        target = entry.pop("_target", None)
        entry["target_row_id"] = target.get("row_id") if isinstance(target, dict) else None

    out["rows_version"] = ROWS_VERSION
    out["merge_log"] = {
        "upgraded_from": 1,
        "inputs": inputs,
        "outputs": outputs_count,
        "superseded": superseded,
        "alternates": alternates,
        "dropped_exact_duplicates": 0,
        "patches": patch_log,
    }
    out.setdefault("floors", [])
    out["row_seq"] = seq.as_dict()
    if cat and not out.get("category"):
        out["category"] = cat
    return out


def _local_id_from_group(group: Mapping[str, Any] | None, kind_key: str, raw: Mapping[str, Any], key: tuple[str, int]) -> str | None:
    """The row's c-id in its sub group's own output (the numbering the
    escalation prompt shows), when that output is at hand."""
    if not group or not isinstance(group.get("output"), Mapping):
        return None
    prefix = PREFIX_BY_KEY[kind_key].lower()
    rows = [
        r for r in group["output"].get(kind_key) or []
        if isinstance(r, Mapping) and _page_key(r.get("document_id"), r.get("page")) == key
    ]
    want = (row_text(kind_key, raw), raw.get("quantity"), raw.get("symbol_or_tag"))
    for i, r in enumerate(rows, start=1):
        if (row_text(kind_key, r), r.get("quantity"), r.get("symbol_or_tag")) == want:
            return f"{prefix}{i}"
    return None


def _merge_patch_pages(out: dict, patch: Mapping[str, Any]) -> None:
    """Patch pages: notes, tags and resolution flags appended to the page."""
    pages = out.setdefault("pages", [])
    for p in patch.get("pages") or []:
        if not isinstance(p, Mapping):
            continue
        key = _page_key(p.get("document_id"), p.get("page"))
        target = next((x for x in pages if isinstance(x, dict) and _page_key(x.get("document_id"), x.get("page")) == key), None)
        if target is None:
            pages.append(copy.deepcopy(dict(p)))
            continue
        for field_name in ("notes", "tags", "resolution_flags"):
            have = target.setdefault(field_name, []) or []
            for v in p.get(field_name) or []:
                if v not in have:
                    have.append(copy.deepcopy(v))
            target[field_name] = have


# Readers ----------------------------------------------------------------------------------------


def _is_category_output(obj: Any) -> bool:
    return isinstance(obj, Mapping) and (is_v2(obj) or any(isinstance(obj.get(k), list) for k, _p in ROW_KINDS))


def _is_measure_output(obj: Any) -> bool:
    """A MeasureOutput v1 (spec 5.4): a `quantities[]` list of M rows and no
    category or BOQ arrays."""
    return (
        isinstance(obj, Mapping)
        and isinstance(obj.get(MEASURE_KEY), list)
        and not _is_category_output(obj)
        and not isinstance(obj.get("areas"), list)
    )


def _boq_spec(obj: Any) -> Mapping[str, Any] | None:
    if not isinstance(obj, Mapping):
        return None
    if isinstance(obj.get("areas"), list):
        return obj
    if isinstance(obj.get("boq"), Mapping) and isinstance(obj["boq"].get("areas"), list):
        return obj["boq"]
    return None


def _flatten(outputs: Any) -> list[tuple[str | None, Mapping[str, Any]]]:
    """(category, output) pairs from any accepted input shape."""
    if outputs is None:
        return []
    if isinstance(outputs, Mapping):
        if isinstance(outputs.get("output"), Mapping) and ("scope" in outputs or "run_id" in outputs):
            return [(outputs.get("category"), outputs["output"])]
        if _is_category_output(outputs) or _boq_spec(outputs) is not None or _is_measure_output(outputs):
            return [(outputs.get("category"), outputs)]
        pairs = []
        for cat, out in outputs.items():
            if isinstance(out, Mapping):
                for c, o in _flatten(out):
                    pairs.append((c or str(cat), o))
        return pairs
    if isinstance(outputs, (list, tuple)):
        pairs = []
        for item in outputs:
            pairs.extend(_flatten(item))
        return pairs
    return []


def _kind_filter(kinds: Any) -> set[str] | None:
    if kinds is None:
        return None
    if isinstance(kinds, str):
        kinds = [kinds]
    out = set()
    for k in kinds:
        k = str(k)
        if k in ROW_ID_PREFIXES:
            out.add(k)
        elif k in PREFIX_BY_KEY:
            out.add(PREFIX_BY_KEY[k])
        elif k in ("boq", "lines"):
            out.add(BOQ_LINE_PREFIX)
        elif k in (MEASURE_KEY, "measure"):
            out.add(MEASURE_PREFIX)
    return out


def iter_measure_rows(measure: Mapping[str, Any], *, seq: RowSeq | None = None) -> Iterator[dict]:
    """MeasureOutput v1 `quantities[]` as kind M rows, in output order. Rows
    without a valid `M<n>` row_id (or with a repeated one) get transient ids
    that continue after every id already in the output."""
    if not _is_measure_output(measure):
        return
    raw = [dict(q) for q in measure.get(MEASURE_KEY) or [] if isinstance(q, Mapping)]
    taken = RowSeq() if seq is None else seq
    for q in raw:
        taken.observe(q.get("row_id"))
    seen: set[str] = set()
    for q in raw:
        rid = q.get("row_id")
        row = copy.deepcopy(q)
        if not is_row_id(rid, MEASURE_PREFIX) or rid in seen:
            rid = taken.next(MEASURE_PREFIX)
            row["transient_id"] = True
        seen.add(rid)
        row["row_id"] = rid
        row["kind"] = MEASURE_PREFIX
        row.setdefault("unit", "FT")
        yield row


def iter_boq_lines(boq: Mapping[str, Any], *, seq: RowSeq | None = None) -> Iterator[dict]:
    """BOQ items in BOQ order (areas, sections, groups, subgroups, items)
    with their position. Items without a valid `line_id` (legacy specs) get
    transient B ids that never collide with ids already in the spec."""
    spec = _boq_spec(boq)
    if spec is None:
        return
    flat: list[tuple[dict, dict]] = []
    taken = RowSeq() if seq is None else seq
    for ai, area in enumerate(spec.get("areas") or []):
        if not isinstance(area, Mapping):
            continue
        for si, section in enumerate(area.get("sections") or []):
            if not isinstance(section, Mapping):
                continue
            for gi, group in enumerate(section.get("groups") or []):
                if not isinstance(group, Mapping):
                    continue
                for ui, sub in enumerate(group.get("subgroups") or []):
                    if not isinstance(sub, Mapping):
                        continue
                    for ii, item in enumerate(sub.get("items") or []):
                        if not isinstance(item, Mapping):
                            continue
                        where = {
                            "area": area.get("area"),
                            "section": section.get("heading"),
                            "group": group.get("name"),
                            "subgroup": sub.get("name"),
                            "path": f"areas[{ai}].sections[{si}].groups[{gi}].subgroups[{ui}].items[{ii}]",
                        }
                        flat.append((dict(item), where))
                        taken.observe(item.get("line_id"))
    seen: set[str] = set()
    for item, where in flat:
        lid = item.get("line_id")
        transient = False
        if not is_row_id(lid, BOQ_LINE_PREFIX) or lid in seen:
            lid = taken.next(BOQ_LINE_PREFIX)
            transient = True
        seen.add(lid)
        row = copy.deepcopy(item)
        row.update(where)
        row["line_id"] = lid
        row["row_id"] = lid
        row["kind"] = BOQ_LINE_PREFIX
        if transient:
            row["transient_id"] = True
        yield row


def iter_rows(outputs: Any, kinds: Any = None, *, meta: Any = None) -> Iterator[dict]:
    """Every row of the given outputs as v2 rows, plus BOQ lines.

    `outputs` may be one category output (v1 or v2), a main output or BOQ
    spec, a plans_reader_outputs row ({scope, category, output}), a mapping
    {category: output}, or a list of any of these. v1 category outputs are
    upgraded on the fly with one shared run-level sequence (categories in
    vocabulary order, after every v2 output's ids), so their transient ids
    are deterministic and never collide. A MeasureOutput (WS3) yields its
    `quantities[]` as kind M rows. Each yielded row is a copy with `kind`
    (the id prefix letter) and `category` added. `kinds` filters by any row
    id prefix letter ("C", "M", "B", ...) or array name ("counts")."""
    wanted = _kind_filter(kinds)
    pairs = _flatten(outputs)
    cats = [(c, o) for c, o in pairs if _is_category_output(o)]
    boqs = [(c, o) for c, o in pairs if not _is_category_output(o) and _boq_spec(o) is not None]
    measures = [(c, o) for c, o in pairs if _is_measure_output(o)]
    seq = RowSeq.from_outputs(o for _c, o in cats if is_v2(o))
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    order = sorted(range(len(cats)), key=lambda i: (0 if is_v2(cats[i][1]) else 1, rank.get(cats[i][0] or cats[i][1].get("category"), len(rank)), i))
    upgraded: dict[int, dict] = {}
    for i in order:
        c, o = cats[i]
        upgraded[i] = dict(o) if is_v2(o) else upgrade_category_output(o, meta, category=c or o.get("category"), row_seq=seq)
    for i in range(len(cats)):
        out = upgraded[i]
        cat = cats[i][0] or out.get("category")
        for kind_key, prefix in ROW_KINDS:
            if wanted is not None and prefix not in wanted:
                continue
            for row in out.get(kind_key) or []:
                if isinstance(row, Mapping):
                    item = copy.deepcopy(dict(row))
                    item["kind"] = prefix
                    item["category"] = cat
                    yield item
    if wanted is None or MEASURE_PREFIX in wanted:
        m_seq = RowSeq()
        for _c, o in measures:
            for q in o.get(MEASURE_KEY) or []:
                if isinstance(q, Mapping):
                    m_seq.observe(q.get("row_id"))
        for c, o in measures:
            for row in iter_measure_rows(o, seq=m_seq):
                row["category"] = c or o.get("category")
                yield row
    if wanted is None or BOQ_LINE_PREFIX in wanted:
        for _c, o in boqs:
            yield from iter_boq_lines(o)


def collision_identity(row: Mapping[str, Any]) -> tuple:
    """What tells two rows of the same id apart: the same output handed in
    twice yields equal identities (no collision), two categories merged
    from 1 do not."""
    origin = row.get("origin") if isinstance(row.get("origin"), Mapping) else {}
    sheet = row.get("sheet") if isinstance(row.get("sheet"), Mapping) else {}
    kind_key = KEY_BY_PREFIX.get(str(row.get("kind") or ""), "counts")
    return (
        str(row.get("category") or ""), str(origin.get("group_id") or ""), str(origin.get("local_id") or ""),
        row_text(kind_key, row), str(sheet.get("sheet_key") or ""), str(row.get("quantity")),
    )


def _log_collisions(collisions: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]]) -> None:
    """One warning per index call: a row id two different rows carry is a
    merge that did not continue the run sequence (every category merged
    from 1); the first row is kept, the other is NOT in the index.
    plans_reader_consolidator.index_sources recovers such rows under new
    ids for the main session; here the collision is at least visible."""
    if not collisions:
        return
    shown = ", ".join(
        f"{rid} ({kept.get('category') or '?'} kept, {lost.get('category') or '?'} dropped)"
        for rid, kept, lost in collisions[:8]
    )
    logger.warning(
        "plans_reader_rows: %d row id collision(s) across outputs, first row kept: %s%s",
        len(collisions), shown, " ..." if len(collisions) > 8 else "",
    )


def index_rows(source: Any, *, meta: Any = None) -> dict[str, dict]:
    """{row id: row} from outputs (anything iter_rows takes), an iterable of
    rows carrying row_id or line_id, or an existing {id: row} mapping. The
    first row of an id wins; a second, different row of the same id is
    logged as a collision (never silently)."""
    if isinstance(source, Mapping) and source and all(is_row_id(k) for k in source):
        return {str(k): dict(v) for k, v in source.items() if isinstance(v, Mapping)}
    collisions: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    if isinstance(source, (list, tuple)) and source and all(
        isinstance(r, Mapping) and (is_row_id(r.get("row_id")) or is_row_id(r.get("line_id")))
        and not _is_category_output(r) and _boq_spec(r) is None
        for r in source
    ):
        out: dict[str, dict] = {}
        for r in source:
            rid = str(r.get("row_id") if is_row_id(r.get("row_id")) else r.get("line_id"))
            if rid in out:
                if collision_identity(out[rid]) != collision_identity(r):
                    collisions.append((rid, out[rid], r))
                continue
            out[rid] = dict(r)
        _log_collisions(collisions)
        return out
    out = {}
    for row in iter_rows(source, meta=meta):
        rid = row.get("row_id")
        if not rid:
            continue
        rid = str(rid)
        if rid in out:
            if collision_identity(out[rid]) != collision_identity(row):
                collisions.append((rid, out[rid], row))
            continue
        out[rid] = row
    _log_collisions(collisions)
    return out


def get_row(outputs: Any, row_id: str, *, meta: Any = None) -> dict | None:
    """The full row (positions, superseded, alternates, origin) or None."""
    if not is_row_id(row_id):
        return None
    prefix, _n = parse_row_id(row_id)
    for row in iter_rows(outputs, kinds=[prefix], meta=meta):
        if row.get("row_id") == row_id or row.get("line_id") == row_id:
            return row
    return None


def compact_row(row: Mapping[str, Any], *, consumed_by: str | None = None) -> dict[str, Any]:
    """The get_rows tool view: {id, sheet_key, description, tag, qty, unit,
    basis, coverage, conf, status, consumed_by}."""
    kind = str(row.get("kind") or "")
    kind_key = KEY_BY_PREFIX.get(kind, "counts")
    qty = row.get("quantity") if kind_key != "runs" else row.get("length_ft")
    unit = row_unit(kind_key, row) or None
    if kind == MEASURE_PREFIX:
        unit = str(row.get("unit") or "FT").upper()
    return {
        "id": row.get("row_id"),
        "sheet_key": (row.get("sheet") or {}).get("sheet_key"),
        "description": row_text(kind_key, row)[:240],
        "tag": row.get("symbol_or_tag"),
        "qty": qty,
        "unit": unit,
        "basis": row.get("basis"),
        "coverage": row.get("coverage"),
        "conf": row.get("confidence"),
        "status": row.get("status"),
        "consumed_by": consumed_by,
    }


__all__ = [
    "ROWS_VERSION", "ROW_KINDS", "PREFIX_BY_KEY", "KEY_BY_PREFIX", "MEASURE_PREFIX", "MEASURE_KEY",
    "INCOMPLETE_COVERAGES", "iter_measure_rows", "ORIGIN_KEY",
    "SUBTOTAL_RE", "MODEL_COVERAGES", "TILE_COVERAGE_MIN", "SHEET_KEY_RE", "RowSeq", "RowsMeta",
    "normalize_sheet_number", "sheet_key", "parse_sheet_key", "doc_order", "parse_positions",
    "tiles_union_fraction", "containment_complete", "row_text", "row_unit", "item_key_for",
    "fingerprint", "tag_families", "infer_coverage", "is_v2", "find_patch_target",
    "upgrade_category_output", "iter_boq_lines", "iter_rows", "index_rows", "collision_identity", "get_row", "compact_row",
]
