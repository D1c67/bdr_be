"""BOQ line v2: code computes every quantity from cited rows
(docs/AI_ESTIMATING_BUILD_SPEC.md sections 5.4 and 5.8; WS4).

Why this module exists: the legacy main call typed every BOQ quantity and
its free-text sheet_refs, so nothing tied a number to the rows it came from
(E11: a tile subtotal became a total). In v2 a line cites row ids with a
multiplier and a combine mode, and `compute(line, rows)` derives:

- `quantity`: sum (sum of q(ref) x multiplier), max, single (exactly one ref),
  schedule_value (the number in a schedule cell `S4#r6:QTY`),
  conductor_footage (run length x conductors of the line's size x
  multiplier), floor (like sum, sets needs_recount), manual (a human value,
  only with a user id). q(ref) is a count row's quantity, a run's length_ft,
  a measure row's quantity;
- `wastage_applies` (FT conductors, cables and conduits, never bare ground),
  `sheet_refs` (the sources' sheet numbers, never typed), `basis`
  (counted, schedule, measured, assumed_route, stated, census_floor, mixed,
  manual), `needs_recount` and a forced `(ASSUMED)` caveat on assumed routes.

Refusals are values (5.8 `add_boq_line`): unknown_ref, unverifiable_ref,
superseded_ref, unit_mismatch, ref_consumed (names the consuming line),
partial_requires_floor, excluded_by_fact (from a callback, verified facts
only, never a raceway-by-EC line), vocab, conduit_form (the renderer's
`_CONDUIT_RE` form), plus the common invalid_args. A refusal never changes
state.

Consumption: each (row, dimension) feeds at most one line. A count row has
one dimension; a run or measure row can feed one conduit line and one line
per conductor size; a schedule cell has one.

`compute` is pure and shared by the consolidator tools, the modify path and
the final gates (arithmetic re-check). `LineBook` is the small stateful
wrapper (B ids, consumption, add, update, remove) the consolidator uses.

Split pairs (WS5 device rule DEV-TR on 7154 run 2): enforce moves part of a
line's quantity to a new line of the same rows (31 duplex counted on C34 ->
28 plain + 3 TR). The split line carries `combine: "split"`, `split_of`
(its parent line id, as a string or as [{line_id, quantity}]) and the same
source_refs as its parent; the parent carries `split_to: [{line_id,
quantity}]`. `recheck` expects the parent at the sum of its sources minus
its split_to quantities and the split line at its own stored quantity
(bounded by the parent's sources, listed in the parent's split_to), and
the provenance gate treats the pair as one consumer of those rows.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services import plans_reader_rows as rows_mod
from app.services.electrical_items import is_linear, linear_evidence_required, parse_item
from app.services.estimating_contracts import (
    ASSUMED_CAVEAT,
    BOQ_LINE_PREFIX,
    COMBINE_MODES,
    MEASURE_BASES,
    QUANTITY_BASES,
    Fact,
    is_row_id,
    line_basis_for_measure,
    parse_row_id,
)
from app.services.plans_reader_render import _CONDUIT_RE
from app.services.plans_reader_schemas import (
    BOQ_GROUPS,
    BOQ_LEVEL0,
    BOQ_SUBGROUPS,
    MATERIAL_CATEGORIES,
    UNITS,
)
from app.services.plans_reader_tools import refusal_dict

# Refusal codes of add_boq_line (plans_reader_review_schemas.REFUSAL_CODES).
UNKNOWN_REF = "unknown_ref"
UNVERIFIABLE_REF = "unverifiable_ref"
SUPERSEDED_REF = "superseded_ref"
UNIT_MISMATCH = "unit_mismatch"
REF_CONSUMED = "ref_consumed"
PARTIAL_REQUIRES_FLOOR = "partial_requires_floor"
EXCLUDED_BY_FACT = "excluded_by_fact"
VOCAB = "vocab"
CONDUIT_FORM = "conduit_form"
INVALID_ARGS = "invalid_args"
UNKNOWN_LINE = "unknown_line"

# Split pairs (module docstring): the split line's combine and the marker
# fields enforce writes on the split line and its parent.
SPLIT_COMBINE = "split"
SPLIT_OF = "split_of"
SPLIT_TO = "split_to"

MAX_MULTIPLIER = 100.0
WASTAGE_CLASSES = frozenset({"conductor", "conduit", "cable"})
# Classes a scope fact never excludes: the EC still provides raceway and boxes
# when a system's devices are by others ("FA devices by others; EC provides
# conduit and boxes" keeps the rough-in).
RACEWAY_CLASSES = frozenset({"conduit", "raceway"})
_ROUGH_IN_RE = re.compile(
    r"(?i)\brough[\s-]?ins?\b|\bback\s?box(?:es)?\b|\bj-?box(?:es)?\b|\bjunction box(?:es)?\b|"
    r"\bpull string\b|\bsleeves?\b"
)
# Conduit types the renderer generates fittings for; a conduit line of one of
# these must be written in its form ('3/4"C EMT').
_FORM_TYPES_RE = re.compile(r"(?i)\b(EMT|RGS|PVC)\b")
_SIZE_GUESS_RE = re.compile(r"""(?i)^\s*(\d+(?:[-\s]\d+/\d+|/\d+)?)\s*(?:"|''|IN\.?|INCH(?:ES)?)?\s*C?\s+""")
_SCHEDULE_CELL_RE = re.compile(r"^(S[1-9][0-9]*)#r([1-9][0-9]*):(.+)$")
_CELL_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_NATURAL_RE = re.compile(r"(\d+)")

# Conductors ---------------------------------------------------------------------

_COUNTED_SIZE_RE = re.compile(
    r"(?i)(?:(?P<n>\d{1,2})\s*(?:-|X|\*)?\s*)?#\s*(?P<size>\d{1,2}/0|\d{1,4})(?:\s*(?P<k>KCMIL|MCM))?"
)
_KCMIL_RE = re.compile(r"(?i)(?:(?P<n>\d{1,2})\s*(?:-|X|\*)\s*|(?P<n2>\d{1,2})\s+)?(?P<size>\d{3,4})\s*(?:KCMIL|MCM)\b")
_PAREN_COUNT_RE = re.compile(r"\(\s*(\d{1,2})\s*\)\s*(?=#|\d{3,4}\s*(?:KCMIL|MCM))", re.IGNORECASE)
_GROUND_TAIL_RE = re.compile(
    r"(?i)^\s*(?:(?:CU|AL|COPPER|ALUMINUM|THHN|THWN(?:-2)?|XHHW(?:-2)?|INSULATED|STRANDED|SOLID|BARE)\b[\s/,]*)*"
    r"(?:G|GND|GRD|GROUND|EG|EGC|GRN|GREEN)\b"
)
_GROUND_ATTACHED_RE = re.compile(r"(?i)^G\b")
_LINE_GROUND_RE = re.compile(r"(?i)\bGROUND\b|\bGND\b|\bGRD\b|\bEGC\b|\bBARE\b|\bGRN\b|(?<=\s)G\b|#\s*\d+(?:/0)?G\b")
GROUND_SUFFIX = " G"


def _size_key(size: str, kcmil: bool) -> str:
    size = size.strip()
    if kcmil or ("/" not in size and size.isdigit() and int(size) >= 250):
        return f"#{int(size)} KCMIL"
    return f"#{size}"


def parse_conductors(text: Any) -> dict[str, int]:
    """Conductors per size from a conductor callout, grounds apart:
    `"2#12, 1#12G"` -> {"#12": 2, "#12 G": 1};
    `"4#500 KCMIL CU + 1#2/0 CU G"` -> {"#500 KCMIL": 4, "#2/0 G": 1}.
    A size without a count is one conductor. Ground conductors (G, GND,
    GROUND, EGC after the size) are keyed with the " G" suffix so a line only
    counts them when it says GROUND or G."""
    raw = _PAREN_COUNT_RE.sub(r"\1 ", str(text or ""))
    out: dict[str, int] = {}
    spans: list[tuple[int, int, int, str]] = []
    for m in _COUNTED_SIZE_RE.finditer(raw):
        n = int(m.group("n")) if m.group("n") else 1
        spans.append((m.start(), m.end(), n, _size_key(m.group("size"), bool(m.group("k")))))
    for m in _KCMIL_RE.finditer(raw):
        if any(s <= m.start() < e for s, e, _n, _k in spans):
            continue
        n_txt = m.group("n") or m.group("n2")
        spans.append((m.start(), m.end(), int(n_txt) if n_txt else 1, _size_key(m.group("size"), True)))
    spans.sort()
    for i, (start, end, n, key) in enumerate(spans):
        nxt = spans[i + 1][0] if i + 1 < len(spans) else len(raw)
        tail = raw[end:nxt]
        tail = re.split(r"[,;+&]|\bAND\b", tail, maxsplit=1, flags=re.IGNORECASE)[0]
        ground = bool(_GROUND_ATTACHED_RE.match(tail) or _GROUND_TAIL_RE.match(tail))
        k = key + GROUND_SUFFIX if ground else key
        out[k] = out.get(k, 0) + n
    return out


def line_conductor(description: Any) -> str | None:
    """The conductor key a footage line counts: its first size, with the
    ground suffix when the line says GROUND, G, GND, EGC or BARE."""
    raw = str(description or "")
    m = _COUNTED_SIZE_RE.search(raw)
    key = None
    if m:
        key = _size_key(m.group("size"), bool(m.group("k")))
    else:
        k = _KCMIL_RE.search(raw)
        if k:
            key = _size_key(k.group("size"), True)
    if key is None:
        return None
    return key + GROUND_SUFFIX if _LINE_GROUND_RE.search(raw) else key


def run_conductors(row: Mapping[str, Any]) -> dict[str, int]:
    """WS3's `conductors_parsed` when present, else parse_conductors of the
    run's conductor callout (or description)."""
    parsed = row.get("conductors_parsed")
    if isinstance(parsed, Mapping) and parsed:
        return {str(k): int(v) for k, v in parsed.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return parse_conductors(row.get("conductors") or row.get("description"))


# Results --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineError:
    """A structured refusal (5.8): never changes state."""

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_refusal(self) -> dict[str, Any]:
        return refusal_dict(self.code, self.message, self.detail or None)


@dataclass(frozen=True)
class ComputedLine:
    quantity: float
    unit: str
    wastage_applies: bool
    sheet_refs: list[str]
    basis: str
    needs_recount: bool
    caveat: str | None
    item_class: str
    consumes: list[tuple[str, str]]
    warnings: list[str]

    def as_result(self, line_id: str | None) -> dict[str, Any]:
        """The add_boq_line result of 5.8 (plus needs_recount and caveat)."""
        return {
            "line_id": line_id,
            "quantity": self.quantity,
            "wastage_applies": self.wastage_applies,
            "sheet_refs": list(self.sheet_refs),
            "basis": self.basis,
            "warnings": list(self.warnings),
            "needs_recount": self.needs_recount,
            "caveat": self.caveat,
        }


ExcludedByFact = Callable[[Mapping[str, Any]], Any]


def _err(code: str, message: str, **detail: Any) -> LineError:
    return LineError(code, message, {k: v for k, v in detail.items() if v is not None})


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _natural(text: str) -> list[Any]:
    return [int(p) if p.isdigit() else p.upper() for p in _NATURAL_RE.split(text)]


# Vocabulary ------------------------------------------------------------------------


def _subgroup_ok(group: str, subgroup: str) -> bool:
    allowed = BOQ_SUBGROUPS.get(group)
    if not allowed:
        return True  # open vocabulary (or an unnamed subgroup)
    if subgroup == "":
        return True
    if subgroup == "OTHER CONDUIT ACCESSORIES":
        return False  # the renderer generates that block
    for name in allowed:
        if subgroup == name or subgroup == name.split("(")[0].strip():
            return True
    return group == "WIRING" and subgroup == "TRENCH AND UNDERGROUND CONDUITS"


def vocab_error(line: Mapping[str, Any]) -> LineError | None:
    section = str(line.get("section") or "")
    group = str(line.get("group") or "")
    subgroup = str(line.get("subgroup") or "")
    if section not in BOQ_LEVEL0:
        return _err(VOCAB, f"section must be one of {', '.join(BOQ_LEVEL0)}", field="section", value=section)
    if section == "DEMOLITION":
        if group not in ("DEMOLITION", ""):
            return _err(VOCAB, "under DEMOLITION use the one group named DEMOLITION", field="group", value=group)
    else:
        if group not in BOQ_GROUPS and group != "":
            return _err(VOCAB, f"group must be one of {', '.join(BOQ_GROUPS)}", field="group", value=group)
        if not _subgroup_ok(group, subgroup):
            allowed = [s for s in BOQ_SUBGROUPS.get(group, ()) if s != "OTHER CONDUIT ACCESSORIES"]
            return _err(VOCAB, f"subgroup of {group} must be one of: {'; '.join(allowed)}", field="subgroup", value=subgroup)
    mc = line.get("material_category")
    if mc is not None and mc not in MATERIAL_CATEGORIES:
        return _err(VOCAB, f"material_category must be one of {', '.join(MATERIAL_CATEGORIES)}", field="material_category", value=mc)
    return None


def suggest_conduit_form(description: str) -> str | None:
    """'3/4" EMT' -> '3/4"C EMT' (the renderer's conduit form), or None."""
    m = _FORM_TYPES_RE.search(description)
    s = _SIZE_GUESS_RE.match(description)
    if not m or not s:
        return None
    size = re.sub(r"\s+", "-", s.group(1).strip())
    rest = description[m.start():].strip()
    return f'{size}"C {rest.upper()}'


def conduit_form_error(line: Mapping[str, Any], item_class: str) -> LineError | None:
    desc = str(line.get("description") or "")
    if item_class != "conduit" or not _FORM_TYPES_RE.search(desc):
        return None
    if _CONDUIT_RE.match(desc):
        return None
    hint = suggest_conduit_form(desc)
    msg = "write conduit as the trade size, a double quote and C, then the type (3/4\"C EMT)"
    if hint:
        msg += f"; did you mean {hint!r}?"
    return _err(CONDUIT_FORM, msg, description=desc, suggestion=hint)


# Fact exclusion ----------------------------------------------------------------------


def _fact_view(fact: Any) -> dict[str, Any] | None:
    if fact is None:
        return None
    if isinstance(fact, Fact):
        if not fact.excludes:
            return None
        return {"fact_id": fact.id, "system": fact.system, "party": fact.party, "quote": fact.cite.quote}
    if isinstance(fact, Mapping):
        if fact.get("verified") is not True:
            return None
        cite = fact.get("cite") if isinstance(fact.get("cite"), Mapping) else {}
        return {
            "fact_id": fact.get("id") or fact.get("fact_id"),
            "system": fact.get("system"),
            "party": fact.get("party"),
            "quote": fact.get("quote") or cite.get("quote"),
        }
    return None


def excluded_error(line: Mapping[str, Any], item_class: str, excluded_by_fact: ExcludedByFact | None) -> LineError | None:
    if excluded_by_fact is None:
        return None
    if item_class in RACEWAY_CLASSES or _ROUGH_IN_RE.search(str(line.get("description") or "")):
        return None
    view = _fact_view(excluded_by_fact(line))
    if view is None:
        return None
    who = view.get("party") or "others"
    fid = view.get("fact_id") or "the fact"
    return _err(
        EXCLUDED_BY_FACT,
        f"{view.get('system') or 'this system'} is excluded ({who}): \"{view.get('quote') or ''}\". "
        f"Instead of a line: add_exclusion citing {fid}, and account the cited rows with "
        f"account_rows(disposition by_others, fact_id {fid}) so they are not orphans.",
        **view,
    )


# Refs -----------------------------------------------------------------------------------


@dataclass
class _Source:
    ref: str
    row: Mapping[str, Any]
    letter: str
    value: float | None
    unit: str
    cell: bool = False


def _row_letter(row_id: str) -> str:
    return parse_row_id(row_id)[0]


def _schedule_value(row: Mapping[str, Any], r_index: int, column: str) -> tuple[float | None, str | None]:
    rows = row.get("rows") or []
    if not isinstance(rows, list) or r_index > len(rows):
        return None, "row_out_of_range"
    cells = rows[r_index - 1]
    if not isinstance(cells, list):
        return None, "row_out_of_range"
    cols = [str(c or "").strip().upper() for c in row.get("columns") or []]
    want = column.strip().upper()
    j = None
    if want in cols:
        j = cols.index(want)
    elif re.fullmatch(r"C[1-9][0-9]*", want):
        j = int(want[1:]) - 1
    if j is None or j >= len(cells):
        return None, "unknown_column"
    m = _CELL_NUMBER_RE.search(str(cells[j] or ""))
    if not m:
        return None, "no_number"
    return float(m.group(0).replace(",", "")), None


def _row_value(row: Mapping[str, Any], letter: str) -> tuple[float | None, str]:
    if letter == "R":
        return _num(row.get("length_ft")), "FT"
    if letter == "M":
        return _num(row.get("quantity")), str(row.get("unit") or "FT").upper()
    if letter == "C":
        return _num(row.get("quantity")), str(row.get("unit") or "EA").upper()
    return None, ""


def _resolve(ref: str, rows: Mapping[str, Mapping[str, Any]]) -> _Source | LineError:
    m = _SCHEDULE_CELL_RE.match(ref)
    if m:
        sid, r_index, column = m.group(1), int(m.group(2)), m.group(3)
        row = rows.get(sid)
        if row is None:
            return _err(UNKNOWN_REF, f"{ref}: schedule {sid} is not a row of this run", ref=ref)
        value, why = _schedule_value(row, r_index, column)
        if value is None:
            return _err(UNKNOWN_REF, f"{ref}: the cell does not resolve to a number ({why})", ref=ref, reason=why)
        return _Source(ref, row, "S", value, "", cell=True)
    if not is_row_id(ref):
        return _err(
            UNKNOWN_REF,
            f"{ref}: cite a row id (C12, R3, M2) or a schedule cell (S4#r6:QTY)",
            ref=ref,
        )
    row = rows.get(ref)
    if row is None:
        return _err(UNKNOWN_REF, f"{ref} is not a row of this run", ref=ref)
    letter = _row_letter(ref)
    value, unit = _row_value(row, letter)
    return _Source(ref, row, letter, value, unit)


def _ref_basis(src: _Source) -> str:
    row = src.row
    if src.cell:
        return "schedule"
    if str(row.get("coverage") or "") in rows_mod.INCOMPLETE_COVERAGES:
        return "census_floor"
    basis = str(row.get("basis") or "")
    if src.letter == "C":
        return {"schedule_text": "schedule", "stated_quantity": "stated"}.get(basis, "counted")
    if src.letter == "R":
        return {"stated": "stated", "scaled_from_drawing": "measured", "assumed": "assumed_route"}.get(basis, "measured")
    if src.letter == "M":
        if basis in MEASURE_BASES:
            return line_basis_for_measure(basis)[0]
        if basis in QUANTITY_BASES:
            return basis
        return "assumed_route"
    if src.letter in ("P", "O"):
        return "stated"
    return "mixed"


def _dimension(src: _Source, combine: str, item_class: str, conductor_key: str | None) -> str:
    if src.cell:
        return "cell"
    if src.letter == "C":
        return "count"
    if combine == "conductor_footage" and conductor_key:
        return f"conductor:{conductor_key}"
    if item_class in RACEWAY_CLASSES:
        return "conduit"
    return item_class or "other"


def _sheet_number(row: Mapping[str, Any]) -> str | None:
    sheet = row.get("sheet") if isinstance(row.get("sheet"), Mapping) else {}
    number = sheet.get("sheet_number") or row.get("sheet_number")
    return str(number) if number else None


def _consumer(consumed: Mapping[Any, Any] | None, key: tuple[str, str]) -> str | None:
    if not consumed:
        return None
    value = consumed.get(key)
    return str(value) if value else None


# compute ----------------------------------------------------------------------------------


def compute(
    line: Mapping[str, Any],
    rows: Any,
    *,
    line_id: str | None = None,
    consumed: Mapping[tuple[str, str], str] | None = None,
    excluded_by_fact: ExcludedByFact | None = None,
    user_id: str | None = None,
    write_checks: bool = True,
) -> ComputedLine | LineError:
    """Compute a BOQ line v2 from its cited rows, or refuse.

    `line` carries the add_boq_line fields (area, section, group, subgroup,
    description, unit, source_refs [{ref, multiplier}], combine, caveat,
    material_note, material_category; `quantity` only for combine manual).
    `rows` is anything plans_reader_rows.index_rows takes ({id: row}, rows,
    or outputs). `write_checks=False` skips the write-time checks (vocab,
    conduit_form, excluded_by_fact) for the arithmetic re-check of stored
    lines. `consumed` maps (ref, dimension) to the line that holds it;
    the line itself (`line_id`) may hold its own refs (update).
    `excluded_by_fact(line)` returns a verified excluding Fact (or a dict with
    verified true) or None. Pure: nothing is mutated."""
    if not isinstance(line, Mapping):
        return _err(INVALID_ARGS, "a line must be an object")
    index = rows if isinstance(rows, Mapping) and all(is_row_id(k) for k in rows) else rows_mod.index_rows(rows)
    description = str(line.get("description") or "").strip()
    unit = str(line.get("unit") or "").upper()
    combine = str(line.get("combine") or "")
    if not description:
        return _err(INVALID_ARGS, "description is required")
    if combine not in COMBINE_MODES:
        return _err(INVALID_ARGS, f"combine must be one of {', '.join(COMBINE_MODES)}", combine=combine)
    if unit == "LS":
        return _err(UNIT_MISMATCH, "LS is never a BOQ line; use add_allowance for lump sums", unit=unit)
    if unit not in UNITS:
        return _err(UNIT_MISMATCH, f"unit must be one of {', '.join(u for u in UNITS if u != 'LS')}", unit=unit)
    identity = parse_item(description, unit)
    item_class = identity.item_class
    if write_checks:
        bad = (
            vocab_error(line)
            or conduit_form_error(line, item_class)
            or excluded_error(line, item_class, excluded_by_fact)
        )
        if bad:
            return bad

    warnings: list[str] = []
    if combine == "manual":
        who = user_id or line.get("user_id") or line.get("created_by")
        if not who:
            return _err(INVALID_ARGS, "combine manual is only for a human modify or override with a user id")
        q = _num(line.get("quantity"))
        if q is None or q < 0:
            return _err(INVALID_ARGS, "a manual line needs a quantity >= 0")
        refs = [r.get("ref") for r in line.get("source_refs") or [] if isinstance(r, Mapping)]
        sheets = sorted({s for s in (_sheet_number(index[r]) for r in refs if r in index) if s}, key=_natural)
        return ComputedLine(
            quantity=q, unit=unit, wastage_applies=_wastage(unit, identity),
            sheet_refs=sheets, basis="manual", needs_recount=False,
            caveat=line.get("caveat"), item_class=item_class, consumes=[], warnings=["manual quantity"],
        )

    if unit != "FT" and is_linear(identity):
        # Spec 5.1: linear classes are always FT; an EA row blocks.
        label = item_class if item_class != "demolition" else f"demolition of {identity.attrs.get('subject')}"
        return _err(
            UNIT_MISMATCH,
            f"{label} is a linear class and must be FT: cite the M row that measures it (never a count row in EA); "
            "with no measured length, account the count row (account_rows) and raise the missing measurement as a risk",
            unit=unit, item_class=item_class,
        )

    source_refs = line.get("source_refs")
    if not isinstance(source_refs, list) or not source_refs:
        return _err(INVALID_ARGS, "cite at least one source row in source_refs")
    parsed: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in source_refs:
        if not isinstance(item, Mapping):
            return _err(INVALID_ARGS, "each source ref is {ref, multiplier}")
        ref = str(item.get("ref") or "").strip()
        mult = _num(item.get("multiplier"))
        if not ref:
            return _err(INVALID_ARGS, "a source ref is empty")
        if ref in seen:
            return _err(INVALID_ARGS, f"{ref} is cited twice in this line", ref=ref)
        seen.add(ref)
        if mult is None or not 0 < mult <= MAX_MULTIPLIER:
            return _err(INVALID_ARGS, f"{ref}: multiplier must be > 0 and <= {MAX_MULTIPLIER:g}", ref=ref, multiplier=item.get("multiplier"))
        if unit == "EA" and not float(mult).is_integer():
            return _err(INVALID_ARGS, f"{ref}: EA lines take whole-number multipliers", ref=ref, multiplier=mult)
        parsed.append((ref, mult))
    if combine == "single" and len(parsed) != 1:
        return _err(INVALID_ARGS, "combine single takes exactly one source ref")

    conductor_key = None
    if combine == "conductor_footage":
        if unit != "FT":
            return _err(UNIT_MISMATCH, "conductor_footage lines are FT", unit=unit)
        conductor_key = line_conductor(description)
        if conductor_key is None:
            return _err(INVALID_ARGS, "conductor_footage needs a conductor size in the description (#12 THHN/THWN Cu)")

    sources: list[tuple[_Source, float]] = []
    for ref, mult in parsed:
        src = _resolve(ref, index)
        if isinstance(src, LineError):
            return src
        status = str(src.row.get("status") or "active")
        if status == "unverifiable":
            return _err(UNVERIFIABLE_REF, f"{ref} is unverifiable (its page was not read by its group, or its value is bad)", ref=ref)
        if status in ("superseded", "alternate"):
            return _err(SUPERSEDED_REF, f"{ref} is {status}; cite the active row", ref=ref, status=status)
        coverage = str(src.row.get("coverage") or "")
        if coverage in rows_mod.INCOMPLETE_COVERAGES and combine != "floor":
            return _err(
                PARTIAL_REQUIRES_FLOOR,
                f"{ref} is a {coverage} (a floor, not a sheet total); cite it with combine floor",
                ref=ref, coverage=coverage,
            )
        sources.append((src, mult))

    # Units and combine shapes.
    for src, _mult in sources:
        if combine == "schedule_value" and not src.cell:
            return _err(INVALID_ARGS, f"{src.ref}: schedule_value cites schedule cells (S4#r6:QTY)", ref=src.ref)
        if src.cell:
            if combine not in ("schedule_value", "sum", "max", "single", "floor"):
                return _err(UNIT_MISMATCH, f"{src.ref}: a schedule cell cannot feed {combine}", ref=src.ref)
            if unit not in ("EA", "FT"):
                return _err(UNIT_MISMATCH, f"{src.ref}: a schedule cell feeds EA or FT lines", ref=src.ref, unit=unit)
            continue
        if src.letter in ("S", "P", "O", "D", "Q"):
            return _err(
                UNIT_MISMATCH,
                f"{src.ref} has no quantity; cite count, run or measure rows (or a schedule cell)",
                ref=src.ref,
            )
        if combine == "conductor_footage" and src.letter not in ("R", "M"):
            return _err(UNIT_MISMATCH, f"{src.ref}: conductor_footage needs run or measure rows", ref=src.ref)
        if src.letter == "R" and unit != "FT":
            return _err(UNIT_MISMATCH, f"{src.ref} is a run (FT); the line is {unit}", ref=src.ref, row_unit="FT", line_unit=unit)
        if src.letter in ("C", "M") and src.unit != unit:
            return _err(UNIT_MISMATCH, f"{src.ref} is {src.unit}; the line is {unit}", ref=src.ref, row_unit=src.unit, line_unit=unit)
        if src.value is None or src.value < 0:
            return _err(UNVERIFIABLE_REF, f"{src.ref} has no usable quantity", ref=src.ref)

    # Consumption: one line per (row, dimension).
    consumes: list[tuple[str, str]] = []
    for src, _mult in sources:
        key = (src.ref, _dimension(src, combine, item_class, conductor_key))
        holder = _consumer(consumed, key)
        if holder and holder != line_id:
            return _err(
                REF_CONSUMED,
                f"{src.ref} ({key[1]}) is already used by line {holder}; update or remove that line",
                ref=src.ref, dimension=key[1], line_id=holder,
            )
        consumes.append(key)

    # Quantity.
    terms: list[float] = []
    for src, mult in sources:
        value = float(src.value or 0.0)
        if combine == "conductor_footage":
            n = run_conductors(src.row).get(conductor_key or "", 0)
            if n <= 0:
                return _err(
                    UNIT_MISMATCH,
                    f"{src.ref} carries no {conductor_key} conductors",
                    ref=src.ref, conductors=run_conductors(src.row), wanted=conductor_key,
                )
            terms.append(value * n * mult)
        else:
            terms.append(value * mult)
    quantity = max(terms) if combine == "max" else sum(terms)
    quantity = round(quantity, 4)
    if unit == "EA" and not float(quantity).is_integer():
        warnings.append(f"fractional EA quantity {quantity:g}")
    if quantity == 0:
        warnings.append("the cited rows sum to 0")

    # Basis, caveat, recount.
    bases = {_ref_basis(src) for src, _m in sources}
    if combine == "floor":
        basis = "census_floor"
    elif len(bases) == 1:
        basis = next(iter(bases))
    else:
        basis = "mixed"
    caveat = line.get("caveat")
    if "assumed_route" in bases:
        if caveat and caveat != ASSUMED_CAVEAT:
            warnings.append(f"caveat {caveat} replaced by {ASSUMED_CAVEAT} (an assumed route is cited)")
        caveat = ASSUMED_CAVEAT
    if any(str(src.row.get("basis") or "") == "inferred" for src, _m in sources if src.letter == "C"):
        warnings.append("a cited count row is inferred, not counted")
    needs_recount = combine == "floor" or any(
        str(src.row.get("coverage") or "") in rows_mod.INCOMPLETE_COVERAGES
        or "needs_recount" in (src.row.get("flags") or [])
        for src, _m in sources
    )
    if unit != "FT" and linear_evidence_required(identity):
        warnings.append(f"linear_in_ea: {item_class} is FT only on evidence (schedule per foot, quote per foot or a measured run)")

    sheets = sorted({s for s in (_sheet_number(src.row) for src, _m in sources) if s}, key=_natural)
    return ComputedLine(
        quantity=quantity,
        unit=unit,
        wastage_applies=_wastage(unit, identity),
        sheet_refs=sheets,
        basis=basis,
        needs_recount=needs_recount,
        caveat=caveat,
        item_class=item_class,
        consumes=consumes,
        warnings=warnings,
    )


def _wastage(unit: str, identity: Any) -> bool:
    """FT conductors, cables and conduits carry the 10% wastage; bare ground
    wire, devices, fixtures, gear, saw cut and trench never do."""
    if unit != "FT" or identity.item_class not in WASTAGE_CLASSES:
        return False
    return identity.attrs.get("insulation") != "BARE"


def assemble_line(line: Mapping[str, Any], computed: ComputedLine, line_id: str) -> dict[str, Any]:
    """The BOQ line v2 item (BOQ_ITEM_V2_ASSEMBLY shape) for the renderer."""
    return {
        "description": str(line.get("description") or "").strip(),
        "quantity": computed.quantity,
        "unit": computed.unit,
        "sheet_refs": list(computed.sheet_refs),
        "caveat": computed.caveat,
        "material_note": line.get("material_note"),
        "material_category": line.get("material_category") or "General Material",
        "wastage_applies": computed.wastage_applies,
        "is_accessory": False,
        "line_id": line_id,
        "source_refs": [
            {"ref": str(r.get("ref")), "multiplier": float(r.get("multiplier"))}
            for r in line.get("source_refs") or [] if isinstance(r, Mapping)
        ],
        "combine": str(line.get("combine")),
        "basis": computed.basis,
        "needs_recount": computed.needs_recount,
    }


# LineBook ---------------------------------------------------------------------------------


_LINE_FIELDS = (
    "area", "section", "group", "subgroup", "description", "unit", "source_refs", "combine",
    "caveat", "material_note", "material_category",
)


class LineBook:
    """BOQ lines being built against one row set: B ids from a run-level
    RowSeq, the consumption map, add, update and remove. Every method returns
    the 5.8 result dict or a refusal dict; a refusal changes nothing."""

    def __init__(
        self,
        rows: Any,
        *,
        seq: rows_mod.RowSeq | None = None,
        excluded_by_fact: ExcludedByFact | None = None,
        user_id: str | None = None,
    ) -> None:
        self.rows = rows_mod.index_rows(rows)
        self.seq = seq if seq is not None else rows_mod.RowSeq()
        self.excluded_by_fact = excluded_by_fact
        self.user_id = user_id
        self.lines: dict[str, dict[str, Any]] = {}
        self.args: dict[str, dict[str, Any]] = {}
        self.consumed: dict[tuple[str, str], str] = {}
        self.log: list[dict[str, Any]] = []

    def _compute(self, line: Mapping[str, Any], line_id: str | None) -> ComputedLine | LineError:
        return compute(
            line, self.rows, line_id=line_id, consumed=self.consumed,
            excluded_by_fact=self.excluded_by_fact, user_id=self.user_id,
        )

    def add(self, line: Mapping[str, Any]) -> dict[str, Any]:
        computed = self._compute(line, None)
        if isinstance(computed, LineError):
            return computed.as_refusal()
        line_id = self.seq.next(BOQ_LINE_PREFIX)
        self._store(line_id, dict(line), computed)
        return computed.as_result(line_id)

    def update(self, line_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        if line_id not in self.lines:
            return refusal_dict(UNKNOWN_LINE, f"{line_id} is not a line", {"line_id": line_id})
        merged = dict(self.args[line_id])
        for k in _LINE_FIELDS:
            if k in changes and changes[k] is not None:
                merged[k] = changes[k]
        computed = self._compute(merged, line_id)
        if isinstance(computed, LineError):
            return computed.as_refusal()
        self._release(line_id)
        self._store(line_id, merged, computed)
        return computed.as_result(line_id)

    def remove(self, line_id: str, reason: str) -> dict[str, Any]:
        if line_id not in self.lines:
            return refusal_dict(UNKNOWN_LINE, f"{line_id} is not a line", {"line_id": line_id})
        freed = self._release(line_id)
        self.lines.pop(line_id)
        self.args.pop(line_id)
        self.log.append({"action": "remove", "line_id": line_id, "reason": reason, "freed": freed})
        return {"line_id": line_id, "freed_refs": sorted({ref for ref, _dim in freed})}

    def consumed_by(self, row_id: str) -> str | None:
        for (ref, _dim), holder in self.consumed.items():
            if ref == row_id or ref.startswith(f"{row_id}#"):
                return holder
        return None

    def _store(self, line_id: str, args: dict[str, Any], computed: ComputedLine) -> None:
        self.args[line_id] = args
        self.lines[line_id] = assemble_line(args, computed, line_id)
        for key in computed.consumes:
            self.consumed[key] = line_id
        self.log.append({"action": "store", "line_id": line_id, "quantity": computed.quantity})

    def _release(self, line_id: str) -> list[tuple[str, str]]:
        freed = [k for k, v in self.consumed.items() if v == line_id]
        for k in freed:
            del self.consumed[k]
        return freed


# Split pairs ---------------------------------------------------------------------------


def _split_entries(value: Any) -> list[tuple[str, float | None]]:
    """[(line_id, quantity or None)] from a split marker: a line id, one
    {line_id, quantity} object, or a list of either."""
    items = value if isinstance(value, list) else [value]
    out: list[tuple[str, float | None]] = []
    for item in items:
        if isinstance(item, Mapping):
            lid = str(item.get("line_id") or "").strip()
            if lid:
                out.append((lid, _num(item.get("quantity"))))
        elif isinstance(item, str) and item.strip():
            out.append((item.strip(), None))
    return out


def split_parent(line: Mapping[str, Any]) -> str | None:
    """The parent line id of a split line (its `split_of`), else None."""
    entries = _split_entries(line.get(SPLIT_OF))
    return entries[0][0] if entries else None


def split_children(line: Mapping[str, Any]) -> list[tuple[str, float | None]]:
    """[(line_id, quantity)] a parent moved to its split lines (`split_to`)."""
    return _split_entries(line.get(SPLIT_TO))


def is_split_line(line: Mapping[str, Any]) -> bool:
    return str(line.get("combine") or "") == SPLIT_COMBINE or bool(line.get(SPLIT_OF))


def split_pairs(lines: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """{split line id: parent line id} for every pair linked from BOTH
    sides (the split names the parent in split_of and the parent lists the
    split in split_to). A one-sided marker is not a pair: the gates then
    see the rows consumed twice and the recheck names the missing link."""
    by_id = {str(ln.get("line_id")): ln for ln in lines if isinstance(ln, Mapping) and ln.get("line_id")}
    out: dict[str, str] = {}
    for lid, ln in by_id.items():
        parent = split_parent(ln)
        if not parent or parent == lid or parent not in by_id:
            continue
        if any(child == lid for child, _q in split_children(by_id[parent])):
            out[lid] = parent
    return out


def _ref_set(line: Mapping[str, Any]) -> set[str]:
    return {str(r.get("ref")) for r in line.get("source_refs") or [] if isinstance(r, Mapping) and r.get("ref")}


def _problem(line: Mapping[str, Any], message: str, **extra: Any) -> dict[str, Any]:
    out = {"line_id": line.get("line_id"), "code": "arithmetic", "message": message}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def recheck(lines: Iterable[Mapping[str, Any]], rows: Any) -> list[dict[str, Any]]:
    """The arithmetic re-check of the final gates: every v2 line recomputed
    from its refs (consumption ignored) against its stored quantity. EA must
    match exactly, FT within max(1 FT, 0.5%). A parent of split lines is
    expected at its sources minus its split_to quantities; a split line at
    its own stored quantity, which must equal the parent's split_to entry,
    cite the parent's rows and fit inside the parent's sources. Returns one
    entry per mismatch or refusal."""
    index = rows_mod.index_rows(rows)
    items = [ln for ln in lines if isinstance(ln, Mapping)]
    by_id = {str(ln.get("line_id")): ln for ln in items if ln.get("line_id")}
    computed: dict[str, ComputedLine | LineError] = {}

    def computed_of(line: Mapping[str, Any]) -> ComputedLine | LineError:
        key = str(line.get("line_id"))
        if key not in computed:
            computed[key] = compute(line, index, line_id=line.get("line_id"), write_checks=False)
        return computed[key]

    problems: list[dict[str, Any]] = []
    for line in items:
        combine = line.get("combine")
        if combine in (None, "manual"):
            continue
        stored = _num(line.get("quantity"))
        unit = str(line.get("unit") or "").upper()
        if is_split_line(line):
            problems.extend(_recheck_split(line, stored, unit, by_id, computed_of))
            continue
        got = computed_of(line)
        if isinstance(got, LineError):
            problems.append({"line_id": line.get("line_id"), "code": got.code, "message": got.message})
            continue
        if stored is None:
            problems.append(_problem(line, "no stored quantity"))
            continue
        expected = got.quantity
        moved = 0.0
        for child, qty in split_children(line):
            if qty is None:
                problems.append(_problem(line, f"split_to entry {child} has no quantity", split_to=child))
                continue
            moved += qty
        if moved:
            expected = round(expected - moved, 4)
        tol = 0.0 if unit == "EA" else max(1.0, 0.005 * got.quantity)
        if abs(stored - expected) > tol + 1e-9:
            note = f" (sources {got.quantity:g} minus {moved:g} split off)" if moved else ""
            problems.append(_problem(
                line, f"stored {stored:g} {unit}, computed {expected:g} {unit}{note}",
                stored=stored, computed=expected, sources=got.quantity if moved else None, split_off=moved or None,
            ))
    return problems


def _recheck_split(
    line: Mapping[str, Any],
    stored: float | None,
    unit: str,
    by_id: Mapping[str, Mapping[str, Any]],
    computed_of: Callable[[Mapping[str, Any]], ComputedLine | LineError],
) -> list[dict[str, Any]]:
    lid = str(line.get("line_id"))
    parent_id = split_parent(line)
    if not parent_id or parent_id == lid or parent_id not in by_id:
        return [_problem(line, f"split line {lid} names no parent line in split_of ({parent_id or 'missing'})", split_of=parent_id)]
    parent = by_id[parent_id]
    if is_split_line(parent):
        return [_problem(line, f"split line {lid}: its parent {parent_id} is itself a split line", split_of=parent_id)]
    out: list[dict[str, Any]] = []
    if stored is None or stored <= 0:
        out.append(_problem(line, f"split line {lid} needs a stored quantity > 0", split_of=parent_id))
        return out
    listed = {child: qty for child, qty in split_children(parent)}
    if lid not in listed:
        out.append(_problem(line, f"split line {lid} is not listed in the split_to of its parent {parent_id}", split_of=parent_id))
    elif listed[lid] is not None and abs(listed[lid] - stored) > 1e-9:
        out.append(_problem(
            line, f"stored {stored:g} {unit}, parent {parent_id} split_to says {listed[lid]:g} {unit}",
            stored=stored, computed=listed[lid], split_of=parent_id,
        ))
    if str(parent.get("unit") or "").upper() != unit:
        out.append(_problem(line, f"split line {lid} is {unit}; its parent {parent_id} is {str(parent.get('unit') or '').upper()}", split_of=parent_id))
    mine, theirs = _ref_set(line), _ref_set(parent)
    if mine != theirs:
        out.append(_problem(
            line,
            f"split line {lid} cites {', '.join(sorted(mine)) or 'nothing'}; its parent {parent_id} cites {', '.join(sorted(theirs)) or 'nothing'}",
            split_of=parent_id, refs=sorted(mine), parent_refs=sorted(theirs),
        ))
    got = computed_of(parent)
    if isinstance(got, LineError):
        return out  # the parent's own refusal is reported on the parent
    if stored - got.quantity > 1e-9:
        out.append(_problem(
            line, f"stored {stored:g} {unit} exceeds the parent's sources {got.quantity:g} {unit}",
            stored=stored, computed=got.quantity, split_of=parent_id,
        ))
    return out


__all__ = [
    "UNKNOWN_REF", "UNVERIFIABLE_REF", "SUPERSEDED_REF", "UNIT_MISMATCH", "REF_CONSUMED",
    "PARTIAL_REQUIRES_FLOOR", "EXCLUDED_BY_FACT", "VOCAB", "CONDUIT_FORM", "INVALID_ARGS",
    "UNKNOWN_LINE", "GROUND_SUFFIX", "LineError", "ComputedLine", "LineBook", "compute",
    "parse_conductors", "line_conductor", "run_conductors", "vocab_error", "suggest_conduit_form",
    "assemble_line", "recheck", "SPLIT_COMBINE", "SPLIT_OF", "SPLIT_TO", "split_parent", "split_children",
    "is_split_line", "split_pairs",
]
