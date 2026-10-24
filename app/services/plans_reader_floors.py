"""Census floors from the WS1 page facts (docs/AI_ESTIMATING_BUILD_SPEC.md
5.1, 5.2 get_census, WS4).

A floor is the smallest count a sheet can honestly carry for one family of
tagged devices: the text layer prints one tag per instance (or per group),
so a BOQ total below the floor is missing devices (E11: demo lighting 34 vs
39 printed "(X)" tags on ED3.01). Text counts are floors, never totals.

Families (spec WS4 design paragraph):

- `demo_x`: status tags `(X)` (and `(D)`) next to devices to remove. Strong.
- `relocate_r`: status tags `(R)`, `(RL)`, `(ER)`. Strong.
- `circuit`: circuit tag tokens (`1LH-4`, `G/1EELC-1`, a panel name with the
  number attached by WS1); each token counts once (a stacked label is one
  token). Engineers tag one device per group with TYP, so this is weak and
  warn-only until the golden check passes.
- `fixture_mark`: luminaire type marks per tag (`L5` x7). Marks exceed
  fixtures on sheets that print the type beside every circuit label (E3.01
  prints L1 39 times for 21 luminaires), so weak.
- `device_mod`: circuit tag modifiers per modifier letter (`G/` GFCI, `WP/`
  weatherproof). Weak.

Rules: "none found" is unknown, never 0 (a page without the family, or
without a usable text layer, has no floor); tolerances come from the
settings (`plans_reader_census_tol_strong`, `_weak`, minimum 1); accounting
counts INSTANCES (a row of 3 track rows is 3 instances; a row superseded by
a measurement still contributes its instances); the legacy BOQ mode sums EA
lines by sheet_refs and leaves linear-class EA lines out (they are a
linear_in_ea finding, not a count).

Pure: no database, no model. Takes page facts as the WS1 PageFacts dataclass
or its `to_dict()` form; never reads a PDF.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.electrical_items import is_linear, parse_item
from app.services.estimating_contracts import TEXT_LAYER_QUALITIES
from app.services.pagefacts.contracts import text_layer_quality_for
from app.services.plans_reader_rows import sheet_key as make_sheet_key

FAMILIES: tuple[str, ...] = ("demo_x", "relocate_r", "circuit", "fixture_mark", "device_mod")
STRONG_FAMILIES: frozenset[str] = frozenset({"demo_x", "relocate_r"})
# Weak families are warn-only until scripts/plans_reader_golden_checks.py
# shows estimator totals honor them (spec: >= 95% of (project, family) pairs).
WEAK_FAMILIES: frozenset[str] = frozenset(FAMILIES) - STRONG_FAMILIES
STRENGTHS: tuple[str, ...] = ("strong", "weak")
METHODS: tuple[str, ...] = ("text_layer", "unavailable")
# Text layer qualities a floor can be read from (spec 5.1: a page whose text
# layer is none, sparse or vector_text has no usable text layer; census is
# `unavailable` there, never a count). vector_text means the labels are drawn
# as vector paths: the text layer holds no tags to count.
USABLE_TEXT_QUALITIES: frozenset[str] = frozenset({"full"})
assert USABLE_TEXT_QUALITIES <= set(TEXT_LAYER_QUALITIES)

DEMO_STATUS_TAGS: tuple[str, ...] = ("X", "D")
RELOCATE_STATUS_TAGS: tuple[str, ...] = ("R", "RL", "ER")
DEFAULT_TOL_STRONG = 0.05
DEFAULT_TOL_WEAK = 0.10
ACCOUNT_MODES: tuple[str, ...] = ("v2", "legacy")

_DEMO_RE = re.compile(r"(?i)\b(remov\w*|demo\w*|demolish\w*|disconnect\s+and\s+remove)\b")
_RELOCATE_RE = re.compile(r"(?i)\brelocat\w*\b|\bre-locat\w*\b|\(\s*(?:R|RL|ER)\s*\)")
_DEVICE_CLASSES: frozenset[str] = frozenset(
    {"receptacle", "switch", "control", "luminaire", "luminaire_linear", "equipment_connection", "other"}
)
_MOD_WORDS: dict[str, re.Pattern[str]] = {
    "G": re.compile(r"(?i)\bGFCI\b|\bGFI\b|\bGROUND\s+FAULT\b"),
    "WP": re.compile(r"(?i)\bWP\b|\bWEATHER\s*PROOF\b|\bWEATHERPROOF\b"),
    "IG": re.compile(r"(?i)\bIG\b|\bISOLATED\s+GROUND\b"),
    "TR": re.compile(r"(?i)\bTR\b|\bTAMPER\s*RESISTANT\b"),
    "USB": re.compile(r"(?i)\bUSB\b"),
    "EM": re.compile(r"(?i)\bEM\b|\bEMERGENCY\b|\bLIFE\s+SAFETY\b"),
}
_WS_RE = re.compile(r"\s+")


def family_key(family: str, tag: str | None = None) -> str:
    """`demo_x`, or `fixture_mark:L5` for the per-tag families."""
    if family not in FAMILIES:
        raise ValueError(f"unknown census family {family!r}")
    return f"{family}:{tag}" if tag else family


def split_family_key(key: str) -> tuple[str, str | None]:
    family, _, tag = str(key or "").partition(":")
    if family not in FAMILIES:
        raise ValueError(f"unknown census family {key!r}")
    return family, (tag or None)


def strength_of(family: str) -> str:
    return "strong" if family in STRONG_FAMILIES else "weak"


# Settings -------------------------------------------------------------------------


def tolerances(settings: Any = None) -> tuple[float, float]:
    """(strong, weak) relative tolerances from `settings` (an object with the
    5.7 attributes, a mapping, or None for the app settings when they load;
    the code defaults otherwise)."""
    strong, weak = DEFAULT_TOL_STRONG, DEFAULT_TOL_WEAK
    src = settings
    if src is None:
        try:
            from app.core.config import get_settings

            src = get_settings()
        except Exception:  # noqa: BLE001 - settings unavailable reads as the defaults
            src = None
    if isinstance(src, Mapping):
        strong = float(src.get("plans_reader_census_tol_strong", strong))
        weak = float(src.get("plans_reader_census_tol_weak", weak))
    elif src is not None:
        strong = float(getattr(src, "plans_reader_census_tol_strong", strong))
        weak = float(getattr(src, "plans_reader_census_tol_weak", weak))
    return strong, weak


def tolerance(floor_value: int | float, strength: str, *, settings: Any = None) -> int:
    """Absolute tolerance for a floor: ceil(rel x floor), never below 1."""
    strong, weak = tolerances(settings)
    rel = strong if strength == "strong" else weak
    return max(1, math.ceil(rel * float(floor_value) - 1e-9))


# Floors ---------------------------------------------------------------------------


@dataclass
class Floor:
    """One (sheet, family) census floor. `floor` is None when the family was
    not found or the page has no usable text layer (`method` says which);
    `positions` are the tag centers (page fractions) that make the count."""

    family: str
    sheet_key: str | None
    document_id: str | None
    page: int | None
    sheet_number: str | None
    floor: int | None
    strength: str
    method: str
    positions: list[list[float]] = field(default_factory=list)
    tag: str | None = None
    note: str | None = None

    @property
    def key(self) -> str:
        return family_key(self.family, self.tag)

    @property
    def known(self) -> bool:
        return self.floor is not None and self.method == "text_layer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "key": self.key,
            "tag": self.tag,
            "sheet_key": self.sheet_key,
            "document_id": self.document_id,
            "page": self.page,
            "sheet_number": self.sheet_number,
            "floor": self.floor,
            "strength": self.strength,
            "method": self.method,
            "positions": [list(p) for p in self.positions],
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Floor:
        family, tag = split_family_key(d.get("key") or d.get("family") or "")
        return cls(
            family=family,
            sheet_key=d.get("sheet_key"),
            document_id=d.get("document_id"),
            page=int(d["page"]) if d.get("page") is not None else None,
            sheet_number=d.get("sheet_number"),
            floor=int(d["floor"]) if d.get("floor") is not None else None,
            strength=str(d.get("strength") or strength_of(family)),
            method=str(d.get("method") or "unavailable"),
            positions=[list(p) for p in d.get("positions") or []],
            tag=d.get("tag") or tag,
            note=d.get("note"),
        )


def _page_dict(page: Any) -> dict[str, Any]:
    if page is None:
        return {}
    if isinstance(page, Mapping):
        return dict(page)
    to_dict = getattr(page, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise TypeError("a page facts value must be a PageFacts or its dict form")


def text_class_usable(text_class: Any) -> bool:
    """A WS1 text class (pagefacts.contracts.TEXT_CLASSES) has a usable text
    layer: its quality (the WS1 mapping) is in USABLE_TEXT_QUALITIES."""
    return text_layer_quality_for(str(text_class or "")) in USABLE_TEXT_QUALITIES


def text_usable(page: Mapping[str, Any]) -> bool:
    quality = page.get("text_layer_quality")
    if quality is None:
        return text_class_usable(page.get("text_class"))
    return str(quality) in USABLE_TEXT_QUALITIES


def _round_point(p: Any) -> list[float] | None:
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        try:
            return [round(float(p[0]), 4), round(float(p[1]), 4)]
        except (TypeError, ValueError):
            return None
    return None


def _points(values: Any) -> list[list[float]]:
    out = []
    for v in values or []:
        p = _round_point(v)
        if p is not None:
            out.append(p)
    return out


def _is_enlarged(view: Mapping[str, Any]) -> bool:
    title = str(view.get("title") or "").upper()
    kind = str(view.get("kind") or "")
    return "enlarged" in kind or "ENLARGED" in title or "ENLARGE" in title or "BLOW" in title


def _view_boxes(page: Mapping[str, Any]) -> tuple[list[list[float]], list[list[float]]]:
    """(overall plan view regions, enlarged view regions) of the page."""
    overall: list[list[float]] = []
    enlarged: list[list[float]] = []
    for v in page.get("views") or []:
        if not isinstance(v, Mapping) or not v.get("region"):
            continue
        region = [float(x) for x in v["region"]]
        if _is_enlarged(v):
            enlarged.append(region)
        elif str(v.get("kind") or "") in ("plan", "site") or "OVERALL" in str(v.get("title") or "").upper():
            overall.append(region)
    return overall, enlarged


def enlarged_view_unresolved(page: Mapping[str, Any]) -> bool:
    """True when the page draws an enlarged view beside an overall plan and
    WS1 could not place the enlarged view (no region): its tags repeat the
    overall plan's devices and cannot be told apart, so the sheet has no
    device floor (ambiguous is unknown, never a count)."""
    views = [v for v in page.get("views") or [] if isinstance(v, Mapping)]
    has_overall = any(not _is_enlarged(v) and str(v.get("kind") or "") in ("plan", "site") for v in views)
    return has_overall and any(_is_enlarged(v) and not v.get("region") for v in views)


def _inside(p: list[float], box: list[float]) -> bool:
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def dedupe_views(page: Mapping[str, Any], points: list[list[float]]) -> list[list[float]]:
    """Tags inside an enlarged view are the overall view's devices drawn
    again: when the page has both an overall plan view and enlarged views,
    the enlarged copies are dropped (the overall view carries the count).
    A page with enlarged views and no overall view keeps everything."""
    overall, enlarged = _view_boxes(page)
    if not overall or not enlarged:
        return list(points)
    return [p for p in points if not any(_inside(p, box) for box in enlarged)]


def family_points(page: Any, family: str, tag: str | None = None) -> list[list[float]]:
    """Positions of the family's tag occurrences on the page (after the
    overall-vs-enlarged dedupe), from the WS1 `tags` block."""
    pg = _page_dict(page)
    tags = pg.get("tags") or {}
    if not isinstance(tags, Mapping):
        return []
    pts: list[list[float]] = []
    if family == "demo_x":
        status = tags.get("status") or {}
        for t in DEMO_STATUS_TAGS:
            pts.extend(_points(status.get(t)))
    elif family == "relocate_r":
        status = tags.get("status") or {}
        for t in RELOCATE_STATUS_TAGS:
            pts.extend(_points(status.get(t)))
    elif family == "circuit":
        seen: set[tuple[float, float]] = set()
        for t in tags.get("circuit") or []:
            if not isinstance(t, Mapping):
                continue
            p = _round_point([t.get("x"), t.get("y")])
            if p is None or (p[0], p[1]) in seen:
                continue
            seen.add((p[0], p[1]))
            pts.append(p)
    elif family == "fixture_mark":
        marks = tags.get("type_marks") or {}
        wanted = [tag.upper()] if tag else list(marks)
        for t in wanted:
            pts.extend(_points(marks.get(t)))
    elif family == "device_mod":
        for t in tags.get("circuit") or []:
            if not isinstance(t, Mapping):
                continue
            mods = [str(m).upper() for m in t.get("mods") or []]
            if tag is None or tag.upper() in mods:
                p = _round_point([t.get("x"), t.get("y")])
                if p is not None and mods:
                    pts.append(p)
    else:
        raise ValueError(f"unknown census family {family!r}")
    return dedupe_views(pg, pts)


def family_tags_present(page: Any, family: str) -> list[str | None]:
    """The per-tag keys a family splits into on this page (fixture_mark by
    luminaire tag, device_mod by modifier); [None] for the plain families."""
    pg = _page_dict(page)
    tags = pg.get("tags") or {}
    if family == "fixture_mark":
        return sorted(k for k in (tags.get("type_marks") or {}) if k)
    if family == "device_mod":
        mods: set[str] = set()
        for t in tags.get("circuit") or []:
            if isinstance(t, Mapping):
                mods.update(str(m).upper() for m in t.get("mods") or [])
        # Only the device modifiers a BOQ description can carry (GFCI, WP,
        # ...); luminaire types and room letters in the mods slot are not.
        return sorted(m for m in mods if m in _MOD_WORDS)
    return [None]


def floors_for_page(
    page: Any,
    *,
    document_id: str | None,
    sheet_key: str | None,
    sheet_number: str | None = None,
    families: Iterable[str] = FAMILIES,
) -> list[Floor]:
    """Every known floor of one page (families with no occurrence are left
    out: none found is unknown). A page without a usable text layer yields
    one `unavailable` floor per plain family so consumers see why."""
    pg = _page_dict(page)
    page_no = pg.get("page")
    number = sheet_number or pg.get("sheet_number")
    out: list[Floor] = []
    if not text_usable(pg):
        for fam in families:
            if fam in ("demo_x", "relocate_r", "circuit"):
                out.append(
                    Floor(
                        family=fam, sheet_key=sheet_key, document_id=document_id, page=page_no,
                        sheet_number=number, floor=None, strength=strength_of(fam), method="unavailable",
                        note="no usable text layer",
                    )
                )
        return out
    ambiguous = enlarged_view_unresolved(pg)
    for fam in families:
        for tag in family_tags_present(pg, fam):
            pts = family_points(pg, fam, tag)
            if not pts:
                continue
            if ambiguous:
                out.append(
                    Floor(
                        family=fam, sheet_key=sheet_key, document_id=document_id, page=page_no,
                        sheet_number=number, floor=None, strength=strength_of(fam), method="unavailable",
                        positions=pts, tag=tag, note="enlarged view without a region repeats the overall plan's tags",
                    )
                )
                continue
            out.append(
                Floor(
                    family=fam, sheet_key=sheet_key, document_id=document_id, page=page_no,
                    sheet_number=number, floor=len(pts), strength=strength_of(fam),
                    method="text_layer", positions=pts, tag=tag,
                )
            )
    return out


def _page_entries(pages: Any) -> list[tuple[str | None, int | None, Any]]:
    """(document_id, page, facts) triples from the accepted shapes: a mapping
    {(document_id, page): facts} or {"doc:page": facts}, an iterable of
    (document_id, page, facts) or of facts dicts carrying document_id, or a
    WS1 DocFacts list [{document_id, pages: [...]}]."""
    out: list[tuple[str | None, int | None, Any]] = []
    if pages is None:
        return out
    if isinstance(pages, Mapping):
        for key, facts in pages.items():
            doc: Any
            page: Any
            if isinstance(key, tuple) and len(key) == 2:
                doc, page = key
            elif isinstance(key, str) and ":" in key:
                doc, _, page = key.rpartition(":")
            else:
                doc, page = key, _page_dict(facts).get("page")
            try:
                out.append((str(doc) if doc is not None else None, int(page), facts))
            except (TypeError, ValueError):
                continue
        return out
    for item in pages:
        if isinstance(item, tuple) and len(item) == 3:
            doc, page, facts = item
            try:
                out.append((str(doc) if doc is not None else None, int(page), facts))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, Mapping) and isinstance(item.get("pages"), list):
            doc = item.get("document_id") or item.get("id")
            for pg in item["pages"]:
                d = _page_dict(pg)
                if d.get("page") is not None:
                    out.append((str(doc) if doc else None, int(d["page"]), d))
        else:
            d = _page_dict(item)
            if d.get("page") is not None:
                out.append((str(d.get("document_id") or "") or None, int(d["page"]), d))
    return out


def compute_floors(
    pages: Any,
    *,
    doc_numbers: Mapping[str, int] | None = None,
    sheet_keys: Mapping[tuple[str, int], str] | None = None,
    families: Iterable[str] = FAMILIES,
    plan_only: bool = True,
) -> list[Floor]:
    """Floors over several pages (see `_page_entries` for the shapes).
    `doc_numbers` ({document_id: n}) or `sheet_keys` give each page its
    D<n>P<m> key; `plan_only` skips pages whose roles hold no plan or site
    (schedule and legend pages print tags that are not device instances)."""
    out: list[Floor] = []
    fams = list(families)
    for doc, page, facts in _page_entries(pages):
        pg = _page_dict(facts)
        roles = set(pg.get("roles") or ([pg["page_role"]] if pg.get("page_role") else []))
        if plan_only and roles and not roles & {"plan", "site"}:
            continue
        key = None
        if sheet_keys and doc is not None and page is not None:
            key = sheet_keys.get((doc, page))
        if key is None and doc_numbers and doc in doc_numbers:
            key = make_sheet_key(doc_numbers[doc], page)
        out.extend(floors_for_page(pg, document_id=doc, sheet_key=key, families=fams))
    return out


def floors_by_sheet(floors: Iterable[Floor | Mapping[str, Any]]) -> dict[tuple[str | None, str], Floor]:
    """{(sheet_key, family key): Floor} over known floors (unknown ones kept
    only when no known floor exists for the key)."""
    out: dict[tuple[str | None, str], Floor] = {}
    for f in floors:
        fl = f if isinstance(f, Floor) else Floor.from_dict(f)
        k = (fl.sheet_key, fl.key)
        if k not in out or (fl.known and not out[k].known):
            out[k] = fl
    return out


# Accounting -------------------------------------------------------------------------


@dataclass
class Accounted:
    """Instances accounted against one floor: `count` (None when nothing on
    the sheet could be read), the contributing ids and per-item instances."""

    count: int | None
    items: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "v2"

    @property
    def ids(self) -> list[str]:
        return [str(i["id"]) for i in self.items if i.get("id")]

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "ids": self.ids, "items": list(self.items), "mode": self.mode}


def _text(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _norm_sheet(value: Any) -> str:
    return re.sub(r"[\s\-_]+", "", str(value or "")).upper()


def _item_sheets(item: Mapping[str, Any]) -> tuple[str | None, set[str]]:
    """(sheet_key, normalized sheet numbers) an item sits on."""
    sheet = item.get("sheet") if isinstance(item.get("sheet"), Mapping) else {}
    key = sheet.get("sheet_key") or item.get("sheet_key")
    numbers = {_norm_sheet(sheet.get("sheet_number"))} if sheet.get("sheet_number") else set()
    for ref in item.get("sheet_refs") or []:
        numbers.add(_norm_sheet(ref))
    if item.get("sheet_number"):
        numbers.add(_norm_sheet(item.get("sheet_number")))
    return (str(key) if key else None), {n for n in numbers if n}


def _on_sheet(item: Mapping[str, Any], floor: Floor) -> bool:
    key, numbers = _item_sheets(item)
    if floor.sheet_key and key:
        return key == floor.sheet_key
    if floor.sheet_number:
        return _norm_sheet(floor.sheet_number) in numbers
    return False


def _description(item: Mapping[str, Any]) -> str:
    return _text(item.get("description") or item.get("name") or "")


def _tag_of(item: Mapping[str, Any], identity: Any) -> str | None:
    tag = identity.attrs.get("tag")
    if tag:
        return str(tag).upper()
    raw = _text(item.get("symbol_or_tag"))
    m = re.match(r"^([A-Z]{1,2}\d{1,2}[A-Z]?)\b", raw.upper())
    return m.group(1) if m else None


def _belongs(item: Mapping[str, Any], floor: Floor, identity: Any) -> bool:
    desc = _description(item)
    section = str(item.get("section") or "").upper()
    tags = " ".join(str(t) for t in item.get("family_tags") or [])
    if floor.family == "demo_x":
        if floor.sheet_key and f"demo_x@{floor.sheet_key}" in tags:
            return True
        return identity.item_class == "demolition" or section == "DEMOLITION" or bool(_DEMO_RE.search(desc))
    if floor.family == "relocate_r":
        if floor.sheet_key and f"relocate_r@{floor.sheet_key}" in tags:
            return True
        return bool(_RELOCATE_RE.search(desc)) or bool(_RELOCATE_RE.search(_text(item.get("symbol_or_tag"))))
    if floor.family == "circuit":
        return section != "DEMOLITION" and identity.item_class in _DEVICE_CLASSES and not _DEMO_RE.search(desc)
    if floor.family == "fixture_mark":
        return (
            identity.item_class in ("luminaire", "luminaire_linear", "lighting_track")
            and _tag_of(item, identity) == (floor.tag or "").upper()
        )
    if floor.family == "device_mod":
        pat = _MOD_WORDS.get((floor.tag or "").upper())
        return bool(pat and pat.search(desc)) and identity.item_class != "demolition"
    return False


def _instances(item: Mapping[str, Any], identity: Any, *, mode: str) -> float | None:
    """Instances one item contributes: EA quantity; segments of a linear row
    (v2 only: a track "row" 3 EA is 3 instances; an EA linear line in legacy
    mode is a linear_in_ea finding, not a count); FT rows contribute the
    number of their positions or segments when known, else nothing."""
    unit = str(item.get("unit") or "EA").upper()
    qty = item.get("quantity")
    try:
        q = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        q = None
    if unit == "EA":
        if q is None:
            return None
        if mode == "legacy" and is_linear(identity):
            return 0.0
        return q
    if unit == "FT":
        segs = item.get("segments") or item.get("instances")
        if isinstance(segs, list) and segs:
            return float(len(segs))
        positions = item.get("positions")
        if isinstance(positions, list) and positions:
            return float(len(positions))
        return None
    return None


def accounted(floor: Floor | Mapping[str, Any], items: Iterable[Mapping[str, Any]], *, mode: str = "v2") -> Accounted:
    """Instances accounted against `floor` by v2 rows or BOQ lines on its
    sheet. v2 mode: active rows and rows superseded by a measurement count;
    alternates never do. Legacy mode: BOQ EA lines by sheet_refs (section
    and description decide the family; linear EA lines excluded)."""
    if mode not in ACCOUNT_MODES:
        raise ValueError(f"mode must be one of {ACCOUNT_MODES}")
    fl = floor if isinstance(floor, Floor) else Floor.from_dict(floor)
    total = 0.0
    found = False
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) or not _on_sheet(item, fl):
            continue
        status = str(item.get("status") or "active")
        if status == "alternate" or status == "unverifiable":
            continue
        if status == "superseded":
            reasons = " ".join(str(s.get("reason") or "") for s in item.get("superseded") or [] if isinstance(s, Mapping))
            if "measure" not in reasons.lower():
                continue
        identity = parse_item(_description(item), str(item.get("unit") or "EA"))
        if not _belongs(item, fl, identity):
            continue
        n = _instances(item, identity, mode=mode)
        rid = item.get("row_id") or item.get("line_id")
        entries.append({"id": rid, "instances": n, "description": _description(item)[:120], "unit": item.get("unit")})
        if n is not None:
            total += n
            found = True
    return Accounted(count=int(round(total)) if found else None, items=entries, mode=mode)


def census_status(floor: Floor, acc: Accounted, *, settings: Any = None) -> dict[str, Any]:
    """`{status: pass|short|unknown, floor, accounted, tolerance, strength}`:
    short when accounted < floor - tolerance (never raises a number itself:
    the recount decides what the true count is)."""
    if not floor.known:
        return {"status": "unknown", "floor": None, "accounted": acc.count, "tolerance": None, "strength": floor.strength}
    tol = tolerance(floor.floor or 0, floor.strength, settings=settings)
    if acc.count is None:
        return {"status": "unknown", "floor": floor.floor, "accounted": None, "tolerance": tol, "strength": floor.strength}
    short = acc.count < (floor.floor or 0) - tol
    return {
        "status": "short" if short else "pass",
        "floor": floor.floor,
        "accounted": acc.count,
        "tolerance": tol,
        "strength": floor.strength,
    }


__all__ = [
    "FAMILIES", "STRONG_FAMILIES", "WEAK_FAMILIES", "STRENGTHS", "METHODS", "USABLE_TEXT_QUALITIES",
    "DEMO_STATUS_TAGS", "RELOCATE_STATUS_TAGS", "DEFAULT_TOL_STRONG", "DEFAULT_TOL_WEAK", "ACCOUNT_MODES",
    "Floor", "Accounted", "family_key", "split_family_key", "strength_of", "tolerances", "tolerance",
    "text_usable", "text_class_usable", "dedupe_views", "enlarged_view_unresolved", "family_points", "family_tags_present", "floors_for_page",
    "compute_floors", "floors_by_sheet", "accounted", "census_status",
]
