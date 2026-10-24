"""Page-facts data contracts (spec 5.1 coordinates, 5.3 Word APIs, WS1 4.x).

Every shape here is JSON-serializable through `to_dict()` and rebuilt with
`from_dict()`. The JSON keys are normative: WS3 (measure), WS4 (regions and
floors) and WS8 (evidence anchors) read them, and the 7154 fixture
`tests/fixtures/plans_reader/run_7154/page_facts_elec.json` is written in
exactly this shape.

Word and Line are the storage blob (one gzip JSON per page), PageFacts is the
per-page row, DocFacts the per-document row. Nothing here carries prices.

`Word` and `Line` are dataclasses that also answer `word["t"]` so consumers
written against the 5.3 dict shape (`{t, b, h, r, f}` and `{t, b, w, h}`)
work unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from app.services.estimating_contracts import TEXT_LAYER_QUALITIES
from app.services.plans_reader_schemas import PAGE_ROLES

EXTRACTOR_VERSION = "pf-1.0.0"
FACTS_SCHEMA = "pagefacts/1"

# Word flag bits (5.3): 1 from an SHX annotation, 2 bold (font weight >= 600),
# 4 hyphen-joined (pdfium's U+FFFE soft-hyphen marker), 8 OCR text layer.
F_SHX = 1
F_BOLD = 2
F_HYPHEN = 4
F_OCR = 8

TEXT_CLASSES: tuple[str, ...] = (
    "cad_text",
    "sparse",
    "vector_text",
    "scanned",
    "scanned_ocr",
    "empty",
    "skipped_budget",
)

# The text-layer quality vocabulary is the S3 contract's tuple
# (estimating_contracts.TEXT_LAYER_QUALITIES: full for a usable text layer,
# none | sparse | vector_text otherwise); nothing here redefines it, and a
# value missing from that tuple fails at import, never in a row.
TEXT_LAYER_QUALITY_FULL = "full"
_QUALITY_BY_CLASS: dict[str, str] = {
    "cad_text": TEXT_LAYER_QUALITY_FULL,
    "scanned_ocr": TEXT_LAYER_QUALITY_FULL,
    "sparse": "sparse",
    "vector_text": "vector_text",
    "scanned": "none",
    "empty": "none",
    "skipped_budget": "none",
}
_unknown_qualities = set(_QUALITY_BY_CLASS.values()) - set(TEXT_LAYER_QUALITIES)
if _unknown_qualities:
    raise RuntimeError(
        f"pagefacts text_layer_quality values {sorted(_unknown_qualities)} are not in "
        f"estimating_contracts.TEXT_LAYER_QUALITIES {TEXT_LAYER_QUALITIES}"
    )

# Sheet-number sources: a learned document slot, a SHEET NO. label anchor, an
# SHX annotation, the bottom-right corner heuristic of a single-page document
# (a guess, never reported as a slot), the splitter title, vision, none.
SOURCE_KINDS: tuple[str, ...] = (
    "slot",
    "label",
    "shx",
    "corner",
    "splitter",
    "vision",
    "none",
)

VIEW_KINDS: tuple[str, ...] = (
    "plan",
    "enlarged_plan",
    "detail",
    "section",
    "elevation",
    "diagram",
    "schedule",
    "other",
)

REGION_KINDS: tuple[str, ...] = (
    "title_block",
    "legend",
    "abbreviations",
    "schedule",
    "general_notes",
    "sheet_notes",
    "keynotes",
    "key_plan",
    "revision_block",
    "view_title",
    "plan_area",
)

# Regions that never hold plan symbols: the plan area is the page minus these.
NON_PLAN_REGION_KINDS: frozenset[str] = frozenset(
    {
        "title_block",
        "legend",
        "abbreviations",
        "schedule",
        "general_notes",
        "sheet_notes",
        "keynotes",
        "key_plan",
        "revision_block",
    }
)

CATEGORY_ACTIONS: tuple[str, ...] = ("none", "relabel", "warn_mixed")

# Roles vocabulary (the existing PAGE_ROLES so the ledger and splitter stay
# compatible) and the tie-break order for `page_role`.
ROLES: tuple[str, ...] = PAGE_ROLES
ROLE_PRIORITY: tuple[str, ...] = (
    "schedule",
    "legend_symbols",
    "general_notes",
    "plan",
    "diagram",
    "detail",
    "site",
    "cover_index",
    "spec_text",
    "other",
)
ROLE_THRESHOLD = 3
LEDGER_LEGEND_ROLES: frozenset[str] = frozenset({"legend_symbols"})
LEDGER_NOTES_ROLES: frozenset[str] = frozenset({"general_notes"})
PLAN_ROLES: frozenset[str] = frozenset({"plan", "site"})

Bbox = list[float]


class FactsUnavailable(Exception):
    """Facts are missing, failed, skipped or the flag is off. Callers never
    receive fabricated data in their place."""


def text_layer_quality_for(text_class: str) -> str:
    return _QUALITY_BY_CLASS.get(text_class, "none")


def round_box(box: Any) -> Bbox:
    x0, y0, x1, y1 = (float(v) for v in box)
    return [
        round(min(x0, x1), 4),
        round(min(y0, y1), 4),
        round(max(x0, x1), 4),
        round(max(y0, y1), 4),
    ]


class _Keyed:
    """Dict-style read access on a dataclass (`obj["t"]`, `obj.get("t")`)."""

    __slots__ = ()

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> list[str]:
        return [f.name for f in fields(self)]  # type: ignore[arg-type]


@dataclass(slots=True)
class Word(_Keyed):
    """One positioned word: `t` sanitized text, `b` bbox (page fractions),
    `h` glyph height in points, `r` rotation after page rotation (0, 90, 180,
    270 or -1 for anything else), `f` flag bits (F_SHX, F_BOLD, F_HYPHEN,
    F_OCR)."""

    t: str
    b: Bbox
    h: float
    r: int = 0
    f: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.t, "b": list(self.b), "h": self.h, "r": self.r, "f": self.f}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Word:
        return cls(
            t=str(d["t"]),
            b=[float(v) for v in d["b"]],
            h=float(d["h"]),
            r=int(d.get("r", 0)),
            f=int(d.get("f", 0)),
        )

    @property
    def cx(self) -> float:
        return (self.b[0] + self.b[2]) / 2

    @property
    def cy(self) -> float:
        return (self.b[1] + self.b[3]) / 2


@dataclass(slots=True)
class Line(_Keyed):
    """Words joined on one baseline: `t` text, `b` bbox, `w` = [first word
    index, last word index + 1] into the page word list, `h` max glyph height
    in points."""

    t: str
    b: Bbox
    w: list[int]
    h: float

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.t, "b": list(self.b), "w": list(self.w), "h": self.h}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Line:
        return cls(
            t=str(d["t"]), b=[float(v) for v in d["b"]], w=[int(v) for v in d["w"]], h=float(d["h"])
        )

    @property
    def cx(self) -> float:
        return (self.b[0] + self.b[2]) / 2

    @property
    def cy(self) -> float:
        return (self.b[1] + self.b[3]) / 2


@dataclass(slots=True)
class Census:
    chars: int = 0
    visible_chars: int = 0
    invisible_chars: int = 0
    off_crop_chars: int = 0
    generated_chars: int = 0
    words: int = 0
    path_count: int = 0
    image_count: int = 0
    image_cover: float = 0.0
    shx_annots: int = 0
    shx_chars: int = 0
    hazards: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "chars": self.chars,
            "visible_chars": self.visible_chars,
            "invisible_chars": self.invisible_chars,
            "off_crop_chars": self.off_crop_chars,
            "generated_chars": self.generated_chars,
            "words": self.words,
            "path_count": self.path_count,
            "image_count": self.image_count,
            "image_cover": round(self.image_cover, 4),
            "shx_annots": self.shx_annots,
            "shx_chars": self.shx_chars,
            "hazards": dict(self.hazards),
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Census:
        return cls(**{f.name: d.get(f.name, getattr(cls(), f.name)) for f in fields(cls)})


@dataclass(slots=True)
class Frame:
    """The 5.1 transform from PDFium user space to displayed page fractions."""

    rotation: int
    left: float
    bottom: float
    right: float
    top: float
    w_in: float
    h_in: float

    def to_frac(self, x: float, y: float) -> tuple[float, float]:
        cw = (self.right - self.left) or 1.0
        ch = (self.top - self.bottom) or 1.0
        u = (x - self.left) / cw
        v = (self.top - y) / ch
        if self.rotation == 90:
            return 1 - v, u
        if self.rotation == 180:
            return 1 - u, 1 - v
        if self.rotation == 270:
            return v, 1 - u
        return u, v

    def box_to_frac(self, left: float, bottom: float, right: float, top: float) -> Bbox:
        ax, ay = self.to_frac(left, top)
        bx, by = self.to_frac(right, bottom)
        return round_box((ax, ay, bx, by))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rotation": self.rotation,
            "cropbox": [self.left, self.bottom, self.right, self.top],
            "w_in": round(self.w_in, 4),
            "h_in": round(self.h_in, 4),
        }


@dataclass(slots=True)
class PageWords:
    """`positioned_words()` result: the words and lines of one page in the
    displayed frame, plus the census and text class. `stream` (the raw char
    stream for regexes that span words) and `frame` are runtime-only and are
    not part of the blob."""

    page_w_in: float
    page_h_in: float
    rotation: int
    words: list[Word]
    lines: list[Line]
    text_class: str
    census: Census = field(default_factory=Census)
    stream: Any = None
    frame: Frame | None = None
    page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FACTS_SCHEMA,
            "page": self.page,
            "page_w_in": round(self.page_w_in, 4),
            "page_h_in": round(self.page_h_in, 4),
            "rotation": self.rotation,
            "text_class": self.text_class,
            "census": self.census.to_dict(),
            "words": [w.to_dict() for w in self.words],
            "lines": [ln.to_dict() for ln in self.lines],
            "stream": None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageWords:
        return cls(
            page_w_in=float(d["page_w_in"]),
            page_h_in=float(d["page_h_in"]),
            rotation=int(d.get("rotation", 0)),
            words=[Word.from_dict(w) for w in d.get("words", [])],
            lines=[Line.from_dict(ln) for ln in d.get("lines", [])],
            text_class=str(d.get("text_class", "empty")),
            census=Census.from_dict(d.get("census") or {}),
            page=d.get("page"),
        )

    def line_words(self, line: Line) -> list[Word]:
        return self.words[line.w[0] : line.w[1]]


@dataclass(slots=True)
class View:
    view_id: str
    title: str | None
    number: str | None
    scale_text: str | None
    ft_per_in: float | None
    kind: str = "other"
    anchor: Bbox | None = None
    region: Bbox | None = None
    example: bool = False
    nts: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "title": self.title,
            "number": self.number,
            "scale_text": self.scale_text,
            "ft_per_in": self.ft_per_in,
            "kind": self.kind,
            "anchor": list(self.anchor) if self.anchor else None,
            "region": list(self.region) if self.region else None,
            "example": self.example,
            "nts": self.nts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> View:
        return cls(
            view_id=str(d["view_id"]),
            title=d.get("title"),
            number=d.get("number"),
            scale_text=d.get("scale_text"),
            ft_per_in=d.get("ft_per_in"),
            kind=str(d.get("kind", "other")),
            anchor=list(d["anchor"]) if d.get("anchor") else None,
            region=list(d["region"]) if d.get("region") else None,
            example=bool(d.get("example", False)),
            nts=bool(d.get("nts", False)),
        )


@dataclass(slots=True)
class Region:
    kind: str
    bbox: Bbox
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "bbox": list(self.bbox), "label": self.label}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Region:
        return cls(kind=str(d["kind"]), bbox=[float(v) for v in d["bbox"]], label=d.get("label"))

    def contains(self, x: float, y: float) -> bool:
        return self.bbox[0] <= x <= self.bbox[2] and self.bbox[1] <= y <= self.bbox[3]


@dataclass(slots=True)
class Revision:
    number: str | None = None
    description: str | None = None
    date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "description": self.description, "date": self.date}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Revision:
        return cls(number=d.get("number"), description=d.get("description"), date=d.get("date"))


def empty_tags() -> dict[str, Any]:
    """The part B tag block shape, empty (circuit tags, status tags, type
    marks, panel labels)."""
    return {"circuit": [], "status": {}, "type_marks": {}, "panel_labels": []}


@dataclass(slots=True)
class PageFacts:
    """One page's facts (the `plans_reader_page_facts.facts` row). Part B
    fields (`notes`, `keynote_markers`, `schedule_ids`, `tags`) are present
    and empty until their modules plug in."""

    page: int
    w_in: float
    h_in: float
    rotation: int
    text_class: str
    census: Census = field(default_factory=Census)
    sheet_number: str | None = None
    sheet_number_source: str = "none"
    sheet_title: str | None = None
    title_source: str = "none"
    revision: Revision | None = None
    discipline: str | None = None
    discipline_category: str | None = None
    roles: list[str] = field(default_factory=list)
    page_role: str = "other"
    role_scores: dict[str, int] = field(default_factory=dict)
    views: list[View] = field(default_factory=list)
    primary_ft_per_in: float | None = None
    scale_conflict: bool = False
    scale_notes: list[str] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    keynote_markers: list[dict[str, Any]] = field(default_factory=list)
    schedule_ids: list[str] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=empty_tags)
    facts_block_tokens: int = 0
    timing_ms: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def text_layer_quality(self) -> str:
        return text_layer_quality_for(self.text_class)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "w_in": round(self.w_in, 4),
            "h_in": round(self.h_in, 4),
            "rotation": self.rotation,
            "text_class": self.text_class,
            "text_layer_quality": self.text_layer_quality,
            "census": self.census.to_dict(),
            "sheet_number": self.sheet_number,
            "sheet_number_source": self.sheet_number_source,
            "sheet_title": self.sheet_title,
            "title_source": self.title_source,
            "revision": self.revision.to_dict() if self.revision else None,
            "discipline": self.discipline,
            "discipline_category": self.discipline_category,
            "roles": list(self.roles),
            "page_role": self.page_role,
            "role_scores": dict(self.role_scores),
            "views": [v.to_dict() for v in self.views],
            "primary_ft_per_in": self.primary_ft_per_in,
            "scale_conflict": self.scale_conflict,
            "scale_notes": list(self.scale_notes),
            "regions": [r.to_dict() for r in self.regions],
            "notes": list(self.notes),
            "keynote_markers": list(self.keynote_markers),
            "schedule_ids": list(self.schedule_ids),
            "tags": self.tags,
            "facts_block_tokens": self.facts_block_tokens,
            "timing_ms": self.timing_ms,
            "problems": list(self.problems),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageFacts:
        return cls(
            page=int(d["page"]),
            w_in=float(d["w_in"]),
            h_in=float(d["h_in"]),
            rotation=int(d.get("rotation", 0)),
            text_class=str(d.get("text_class", "empty")),
            census=Census.from_dict(d.get("census") or {}),
            sheet_number=d.get("sheet_number"),
            sheet_number_source=str(d.get("sheet_number_source", "none")),
            sheet_title=d.get("sheet_title"),
            title_source=str(d.get("title_source", "none")),
            revision=Revision.from_dict(d["revision"]) if d.get("revision") else None,
            discipline=d.get("discipline"),
            discipline_category=d.get("discipline_category"),
            roles=list(d.get("roles") or []),
            page_role=str(d.get("page_role", "other")),
            role_scores={str(k): int(v) for k, v in (d.get("role_scores") or {}).items()},
            views=[View.from_dict(v) for v in d.get("views") or []],
            primary_ft_per_in=d.get("primary_ft_per_in"),
            scale_conflict=bool(d.get("scale_conflict", False)),
            scale_notes=list(d.get("scale_notes") or []),
            regions=[Region.from_dict(r) for r in d.get("regions") or []],
            notes=list(d.get("notes") or []),
            keynote_markers=list(d.get("keynote_markers") or []),
            schedule_ids=list(d.get("schedule_ids") or []),
            tags=dict(d.get("tags") or empty_tags()),
            facts_block_tokens=int(d.get("facts_block_tokens", 0)),
            timing_ms=int(d.get("timing_ms", 0)),
            problems=list(d.get("problems") or []),
        )

    def region(self, kind: str) -> Region | None:
        for r in self.regions:
            if r.kind == kind:
                return r
        return None


@dataclass(slots=True)
class TitleBlockSlot:
    x: float
    y: float
    cover: float
    distinct: float
    score: float
    pages: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "cover": round(self.cover, 4),
            "distinct": round(self.distinct, 4),
            "score": round(self.score, 4),
            "pages": list(self.pages),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TitleBlockSlot:
        return cls(
            x=float(d["x"]),
            y=float(d["y"]),
            cover=float(d.get("cover", 0)),
            distinct=float(d.get("distinct", 0)),
            score=float(d.get("score", 0)),
            pages=[int(p) for p in d.get("pages", [])],
        )


@dataclass(slots=True)
class CategoryCheck:
    label: str | None
    detected: str | None
    share: float
    numbered_pages: int
    by_discipline: dict[str, int]
    action: str = "none"
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "detected": self.detected,
            "share": round(self.share, 4),
            "numbered_pages": self.numbered_pages,
            "by_discipline": dict(self.by_discipline),
            "action": self.action,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CategoryCheck:
        return cls(
            label=d.get("label"),
            detected=d.get("detected"),
            share=float(d.get("share", 0)),
            numbered_pages=int(d.get("numbered_pages", 0)),
            by_discipline={str(k): int(v) for k, v in (d.get("by_discipline") or {}).items()},
            action=str(d.get("action", "none")),
            message=d.get("message"),
        )


def empty_legend() -> dict[str, Any]:
    return {"entries": [], "modifiers": [], "abbreviations": []}


@dataclass(slots=True)
class DocFacts:
    """Per-document facts (the `plans_reader_doc_facts.facts` row plus the
    page rows). `kind` is `drawing` or `text`. Part B fields (`legend`,
    `schedules`, `panels`, `panel_locations`, `fixture_types`, `spec`,
    `cross_trade`) are present and empty until their modules plug in."""

    sha256: str
    kind: str
    page_count: int
    name: str | None = None
    label: str | None = None
    extractor_version: str = EXTRACTOR_VERSION
    title_block_slots: list[TitleBlockSlot] = field(default_factory=list)
    category_check: CategoryCheck | None = None
    legend: dict[str, Any] = field(default_factory=empty_legend)
    schedules: list[dict[str, Any]] = field(default_factory=list)
    panels: list[dict[str, Any]] = field(default_factory=list)
    panel_locations: list[dict[str, Any]] = field(default_factory=list)
    fixture_types: list[dict[str, Any]] = field(default_factory=list)
    spec: dict[str, Any] | None = None
    cross_trade: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    pages: list[PageFacts] = field(default_factory=list)

    def to_dict(self, *, include_pages: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": FACTS_SCHEMA,
            "extractor_version": self.extractor_version,
            "sha256": self.sha256,
            "kind": self.kind,
            "name": self.name,
            "label": self.label,
            "page_count": self.page_count,
            "title_block_slots": [s.to_dict() for s in self.title_block_slots],
            "category_check": self.category_check.to_dict() if self.category_check else None,
            "legend": self.legend,
            "schedules": list(self.schedules),
            "panels": list(self.panels),
            "panel_locations": list(self.panel_locations),
            "fixture_types": list(self.fixture_types),
            "spec": self.spec,
            "cross_trade": list(self.cross_trade),
            "problems": list(self.problems),
            "stats": dict(self.stats),
        }
        if include_pages:
            out["pages"] = [p.to_dict() for p in self.pages]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DocFacts:
        return cls(
            sha256=str(d["sha256"]),
            kind=str(d.get("kind", "drawing")),
            page_count=int(d.get("page_count", 0)),
            name=d.get("name"),
            label=d.get("label"),
            extractor_version=str(d.get("extractor_version", EXTRACTOR_VERSION)),
            title_block_slots=[
                TitleBlockSlot.from_dict(s) for s in d.get("title_block_slots") or []
            ],
            category_check=CategoryCheck.from_dict(d["category_check"])
            if d.get("category_check")
            else None,
            legend=dict(d.get("legend") or empty_legend()),
            schedules=list(d.get("schedules") or []),
            panels=list(d.get("panels") or []),
            panel_locations=list(d.get("panel_locations") or []),
            fixture_types=list(d.get("fixture_types") or []),
            spec=d.get("spec"),
            cross_trade=list(d.get("cross_trade") or []),
            problems=list(d.get("problems") or []),
            stats=dict(d.get("stats") or {}),
            pages=[PageFacts.from_dict(p) for p in d.get("pages") or []],
        )

    def page(self, number: int) -> PageFacts | None:
        for p in self.pages:
            if p.page == number:
                return p
        return None
