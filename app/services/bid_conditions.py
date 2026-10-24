"""Bid conditions classifier (docs/AI_ESTIMATING_BUILD_SPEC.md 5.4, WS5; fixes
E4): wage type, labor time, union, bond and the labor rate table key.

`classify(project_row, document_texts)` is pure and deterministic:

1. Wage type. The project record wins (`projects.wage_type` set to
   prevailing_wage or non_prevailing_wage; basis project_record). A free-text
   value such as "If over $100,000 then PW ..." is treated as unset plus
   needs_review `wage_threshold_text` (`recheck_threshold` resolves it once a
   priced total exists). Next, weighted document cues, each cue counted once:
   NRS 338 (including the quoted-abbreviation form and "Chapter 338") 3,
   Davis-Bacon 3, wage determination 3, labor commissioner 2, public works
   project or contract 2, prevailing wage 2, certified payroll 1, "not subject
   to prevailing" -3. A score >= 2 is prevailing wage, <= -2 non-prevailing
   (basis document_cue). Next the owner type (public -> prevailing; private or
   casino -> non-prevailing with needs_review `private_owner_confirm`), then a
   public owner lexicon over the project name and the first pages of each
   document. Else the policy default (prevailing wage, owner Q13) with
   needs_review `wage_unknown`.
2. Labor time from `projects.labor_time` (day_work -> day, night_work ->
   night); null -> from the documents (phasing or after-hours language ->
   mixed plus needs_review even when a day work window is also stated,
   night language -> night, a day work window -> day, else day with
   needs_review `labor_time_unknown`); any other value -> day with
   needs_review (mixed when it names both day and night).
3. Union from `projects.labor_needed`, else union cues, else unknown.
4. bond_required from bid instruction language, else None (the proposal
   template's "Add 3%" line covers it).
5. rate_table_key from the policy rate table (pw | open_shop).

No database, network or model is touched here except `for_run`, a read of
`plans_reader_runs.bid_conditions` that wave 2 wires (it returns None while
the column does not exist).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.services.estimating_contracts import WAGE_TYPES, WAGE_UNKNOWN_REVIEW

logger = logging.getLogger(__name__)

PREVAILING = "prevailing_wage"
NON_PREVAILING = "non_prevailing_wage"

WAGE_BASES: tuple[str, ...] = (
    "project_record",
    "document_cue",
    "owner_type",
    "owner_lexicon",
    "default",
)
WAGE_CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
LABOR_TIMES: tuple[str, ...] = ("day", "night", "mixed")
UNION_VALUES: tuple[str, ...] = ("union", "non_union", "unknown")

# needs_review reasons (WAGE_UNKNOWN_REVIEW = "wage_unknown" comes from the contracts).
REVIEW_WAGE_CONFLICT = "wage_conflict"
REVIEW_WAGE_THRESHOLD_TEXT = "wage_threshold_text"
REVIEW_PRIVATE_OWNER = "private_owner_confirm"
REVIEW_OWNER_LEXICON = "owner_lexicon_confirm"
REVIEW_BELOW_NRS338 = "below_nrs338_threshold"
REVIEW_LABOR_TIME_MIXED = "labor_time_mixed"
REVIEW_LABOR_TIME_UNRECOGNIZED = "labor_time_unrecognized"
REVIEW_LABOR_TIME_CONFLICT = "labor_time_conflict"
REVIEW_LABOR_TIME_UNKNOWN = "labor_time_unknown"

DEFAULT_RATE_TABLE: dict[str, str] = {PREVAILING: "pw", NON_PREVAILING: "open_shop"}
DEFAULT_NRS338_MIN_VALUE_USD = 100_000  # NRS 338 statute threshold, not project data
PW_THRESHOLD = 2
NON_PW_THRESHOLD = -2
CONFLICT_MARGIN = 3
QUOTE_MAX_CHARS = 200
LEXICON_PAGES = 3

# Cue name -> (weight, regexes). Each cue counts once per classification.
CUES: dict[str, tuple[int, tuple[str, ...]]] = {
    "nrs338": (3, (r"N\.?R\.?S\.?[^0-9\n]{0,20}338\b", r"\bChapter\s+338\b")),
    "davis_bacon": (3, (r"\bdavis[\s-]*bacon\b",)),
    "wage_determination": (
        3,
        (r"\bwage\s+determinations?\b", r"\bgeneral\s+decision\s+(?:number|no)\b"),
    ),
    "labor_commissioner": (2, (r"\blabor\s+commissioner\b",)),
    "public_works": (2, (r"\bpublic\s+works?\s+(?:project|contract)s?\b", r"\bPWP[\s#-]")),
    "prevailing_wage": (2, (r"(?<!non[\s-])\bprevailing\s+wages?\b",)),
    "certified_payroll": (1, (r"\bcertified\s+payrolls?\b",)),
    "not_prevailing": (
        -3,
        (r"\bnot\s+(?:subject\s+to|a)\s+prevailing\b", r"\bnon[\s-]prevailing\b"),
    ),
}
STATUTE_CUES: frozenset[str] = frozenset({"nrs338", "davis_bacon", "wage_determination"})

PUBLIC_OWNER_TYPES: frozenset[str] = frozenset({"public_other", "rtc", "ccsd"})
PRIVATE_OWNER_TYPES: frozenset[str] = frozenset(
    {"private_commercial", "private_residential", "casino_strip", "casino_other"}
)

# Public owner names. Matched against the project name, and against the first
# pages of owner documents only (roles in OWNER_ROLES): drawing and spec text
# is too noisy (engineer stamps name the State of Nevada, jurisdiction notes
# name the city, "UMC" is also the Uniform Mechanical Code).
PUBLIC_OWNER_LEXICON: tuple[str, ...] = (
    r"\buniversity\s+medical\s+center\b",
    r"\bclark\s+county\s+(?:school\s+district|department|detention|government|fire)\b",
    r"\bCCSD\b",
    r"\bcity\s+of\s+(?:las\s+vegas|henderson|north\s+las\s+vegas|boulder\s+city|mesquite)\b",
    r"\bRTC\b",
    r"\bregional\s+transportation\s+commission\b",
    r"\bNDOT\b",
    r"\bnevada\s+department\s+of\s+transportation\b",
    r"\bUNLV\b",
    r"\bCSN\b",
    r"\bNSHE\b",
    r"\bnevada\s+state\s+(?:college|university)\b",
    r"\blas\s+vegas\s+valley\s+water\s+district\b",
    r"\bLVVWD\b",
    r"\bSNWA\b",
    r"\bdepartment\s+of\s+aviation\b",
    r"\bLVCVA\b",
    r"\bstate\s+of\s+nevada\b",
    r"\bGSA\b",
)

_DAY_WINDOW_RE = re.compile(
    r"\b(\d{1,2})(?::\d\d)?\s*(?:am|a\.m\.)\s*(?:-|to|through|until)\s*"
    r"\d{1,2}(?::\d\d)?\s*(?:pm|p\.m\.)",
    re.IGNORECASE,
)
_NIGHT_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bnight\s+(?:shifts?|work|hours)\b",
        r"\b(?:9|10|11):\d\d\s*(?:pm|p\.m\.)\s*(?:-|to|through|until)\s*\d{1,2}(?::\d\d)?\s*(?:am|a\.m\.)",
    )
)
_MIXED_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bafter[\s-]hours?\b",
        r"\boff[\s-]hours\b",
        r"\bweekend\s+work\b",
        r"\bphas(?:ed|ing)\s+(?:construction|work|plan|schedule)\b",
        r"\bconstruction\s+phasing\b",
        r"\bwork\s+(?:shall\s+be\s+)?(?:performed\s+)?in\s+phases\b",
    )
)
_UNION_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bIBEW\b",
        r"\blocal\s+357\b",
        r"\bproject\s+labor\s+agreement\b",
        r"\bunion\s+(?:labor|contractors?|workforce|signatory)\b",
    )
)
_BOND_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bpayment\s+(?:and|&)\s+performance\s+bonds?\b",
        r"\bperformance\s+(?:and|&)\s+payment\s+bonds?\b",
        r"\bperformance\s+bonds?\b",
    )
)
_NO_BOND_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bno\s+(?:performance\s+|payment\s+)?bonds?\s+(?:is\s+|are\s+|will\s+be\s+)?required\b",
        r"\bbonds?\s+(?:is|are|will)\s+not\s+(?:be\s+)?required\b",
    )
)
# Document roles that carry bid instructions (when a role is given).
BOND_ROLES: frozenset[str] = frozenset(
    {"rfp", "bid_instructions", "contract_conditions", "owner_scope_exhibit", "specification", "spec"}
)

_COMPILED_CUES: dict[str, tuple[int, tuple[re.Pattern[str], ...]]] = {
    name: (weight, tuple(re.compile(p, re.IGNORECASE) for p in patterns))
    for name, (weight, patterns) in CUES.items()
}
# Only in a project name: "UMC" there is the hospital, not the mechanical code.
NAME_ONLY_LEXICON: tuple[str, ...] = (r"\bUMC\b",)
OWNER_ROLES: frozenset[str] = frozenset(
    {"rfp", "bid_instructions", "contract_conditions", "owner_scope_exhibit"}
)

_LEXICON_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in PUBLIC_OWNER_LEXICON
)
_NAME_LEXICON_RES: tuple[re.Pattern[str], ...] = _LEXICON_RES + tuple(
    re.compile(p, re.IGNORECASE) for p in NAME_ONLY_LEXICON
)


@dataclass(frozen=True)
class CueHit:
    cue: str
    weight: int
    document_id: str | None
    page: int | None
    quote: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cue": self.cue,
            "weight": self.weight,
            "document_id": self.document_id,
            "page": self.page,
            "quote": self.quote,
        }


@dataclass(frozen=True)
class BidConditions:
    """Spec 5.4 BidConditions (plus `labor_time_basis` and `owner_basis`)."""

    wage_type: str
    wage_basis: str
    wage_confidence: str
    federal_wage: bool
    pw_score: int
    cues: tuple[CueHit, ...]
    labor_time: str
    union: str
    bond_required: bool | None
    owner_type: str | None
    rate_table_key: str
    needs_review: tuple[str, ...]
    labor_time_basis: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "wage_type": self.wage_type,
            "wage_basis": self.wage_basis,
            "wage_confidence": self.wage_confidence,
            "federal_wage": self.federal_wage,
            "pw_score": self.pw_score,
            "cues": [c.to_dict() for c in self.cues],
            "labor_time": self.labor_time,
            "labor_time_basis": self.labor_time_basis,
            "union": self.union,
            "bond_required": self.bond_required,
            "owner_type": self.owner_type,
            "rate_table_key": self.rate_table_key,
            "needs_review": list(self.needs_review),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BidConditions:
        return cls(
            wage_type=data["wage_type"],
            wage_basis=data["wage_basis"],
            wage_confidence=data.get("wage_confidence") or "low",
            federal_wage=bool(data.get("federal_wage")),
            pw_score=int(data.get("pw_score") or 0),
            cues=tuple(
                CueHit(
                    cue=c["cue"],
                    weight=int(c["weight"]),
                    document_id=c.get("document_id"),
                    page=c.get("page"),
                    quote=c.get("quote") or "",
                )
                for c in data.get("cues") or ()
            ),
            labor_time=data.get("labor_time") or "day",
            labor_time_basis=data.get("labor_time_basis") or "default",
            union=data.get("union") or "unknown",
            bond_required=data.get("bond_required"),
            owner_type=data.get("owner_type"),
            rate_table_key=data.get("rate_table_key") or "pw",
            needs_review=tuple(data.get("needs_review") or ()),
        )

    @property
    def statute_cue(self) -> bool:
        return any(c.cue in STATUTE_CUES for c in self.cues)


@dataclass(frozen=True)
class DocumentText:
    """One page (or block) of document text fed to the classifier."""

    text: str
    document_id: str | None = None
    page: int | None = None
    role: str | None = None


@dataclass
class _Policy:
    rate_table: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_RATE_TABLE))
    default_wage_type: str = PREVAILING
    nrs338_min_value_usd: float = DEFAULT_NRS338_MIN_VALUE_USD


def _policy(policy: Mapping[str, Any] | None) -> _Policy:
    """Accepts a whole policy document or its `bid_conditions` block."""
    out = _Policy()
    if not policy:
        return out
    block = policy.get("bid_conditions", policy)
    if not isinstance(block, Mapping):
        return out
    rate_table = block.get("rate_table")
    if isinstance(rate_table, Mapping) and set(rate_table) >= set(WAGE_TYPES):
        out.rate_table = {k: str(rate_table[k]) for k in WAGE_TYPES}
    if block.get("default_wage_type") in WAGE_TYPES:
        out.default_wage_type = block["default_wage_type"]
    threshold = block.get("nrs338_min_value_usd")
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        out.nrs338_min_value_usd = float(threshold)
    return out


def _as_documents(document_texts: Iterable[Any] | None) -> list[DocumentText]:
    docs: list[DocumentText] = []
    for item in document_texts or ():
        if isinstance(item, DocumentText):
            docs.append(item)
        elif isinstance(item, str):
            docs.append(DocumentText(text=item))
        elif isinstance(item, Mapping):
            page = item.get("page")
            docs.append(
                DocumentText(
                    text=str(item.get("text") or ""),
                    document_id=item.get("document_id"),
                    page=int(page) if isinstance(page, (int, float)) else None,
                    role=item.get("role"),
                )
            )
    return docs


def _quote(text: str, start: int, end: int) -> str:
    pad = max(0, (QUOTE_MAX_CHARS - (end - start)) // 2)
    lo, hi = max(0, start - pad), min(len(text), end + pad)
    snippet = re.sub(r"\s+", " ", text[lo:hi]).strip()
    return snippet[:QUOTE_MAX_CHARS]


def score_cues(document_texts: Iterable[Any]) -> tuple[int, list[CueHit]]:
    """(pw_score, hits): the first hit of each cue, weights summed once."""
    hits: dict[str, CueHit] = {}
    for doc in _as_documents(document_texts):
        if not doc.text:
            continue
        for name, (weight, patterns) in _COMPILED_CUES.items():
            if name in hits:
                continue
            for pattern in patterns:
                m = pattern.search(doc.text)
                if m:
                    hits[name] = CueHit(
                        cue=name,
                        weight=weight,
                        document_id=doc.document_id,
                        page=doc.page,
                        quote=_quote(doc.text, m.start(), m.end()),
                    )
                    break
        if len(hits) == len(_COMPILED_CUES):
            break
    ordered = [hits[name] for name in CUES if name in hits]
    return sum(h.weight for h in ordered), ordered


def _lexicon_hit(project_name: str | None, docs: Sequence[DocumentText]) -> bool:
    if project_name and any(p.search(project_name) for p in _NAME_LEXICON_RES):
        return True
    texts = [
        d.text
        for d in docs
        if d.text and d.role in OWNER_ROLES and (d.page is None or d.page <= LEXICON_PAGES)
    ]
    return any(p.search(t) for t in texts for p in _LEXICON_RES)


def _any(patterns: Sequence[re.Pattern[str]], docs: Sequence[DocumentText]) -> bool:
    return any(p.search(d.text) for d in docs if d.text for p in patterns)


def _has_day_window(docs: Sequence[DocumentText]) -> bool:
    for d in docs:
        for m in _DAY_WINDOW_RE.finditer(d.text or ""):
            if 5 <= int(m.group(1)) <= 9:
                return True
    return False


def _labor_time(project: Mapping[str, Any], docs: Sequence[DocumentText]) -> tuple[str, str, list[str]]:
    """(labor_time, basis, needs_review)."""
    day_window = _has_day_window(docs)
    # Phasing or after-hours language means mixed (spec 5.4) even next to a
    # stated day work window: the window is the normal shift, the language
    # adds work outside it. It is never dropped silently.
    mixed_lang = _any(_MIXED_RES, docs)
    night_lang = _any(_NIGHT_RES, docs)
    raw = project.get("labor_time")
    value = raw.strip().lower() if isinstance(raw, str) else raw
    if value in ("day_work", "night_work"):
        lt = "day" if value == "day_work" else "night"
        review = []
        if mixed_lang or (lt == "day" and night_lang and not day_window):
            review.append(REVIEW_LABOR_TIME_CONFLICT)
        return lt, "project_record", review
    if value:
        if "night" in value and "day" in value:
            return "mixed", "project_record", [REVIEW_LABOR_TIME_MIXED]
        return "day", "project_record", [REVIEW_LABOR_TIME_UNRECOGNIZED]
    if mixed_lang:
        return "mixed", "document_cue", [REVIEW_LABOR_TIME_MIXED]
    if night_lang:
        return "night", "document_cue", []
    if day_window:
        return "day", "document_cue", []
    return "day", "default", [REVIEW_LABOR_TIME_UNKNOWN]


def _union(project: Mapping[str, Any], docs: Sequence[DocumentText]) -> str:
    raw = project.get("labor_needed")
    value = raw.strip().lower() if isinstance(raw, str) else None
    if value == "union":
        return "union"
    if value in ("non_union", "ce_cw"):
        return "non_union"
    return "union" if _any(_UNION_RES, docs) else "unknown"


def _bond(docs: Sequence[DocumentText]) -> bool | None:
    scoped = [d for d in docs if d.role is None or d.role in BOND_ROLES]
    if _any(_NO_BOND_RES, scoped):
        return False
    if _any(_BOND_RES, scoped):
        return True
    return None


def _owner_type(project: Mapping[str, Any]) -> str | None:
    raw = project.get("owner_type")
    if isinstance(raw, str) and raw.strip() and raw.strip().lower() not in ("unknown", "other"):
        return raw.strip().lower()
    return None


def classify(
    project_row: Mapping[str, Any] | None,
    document_texts: Iterable[Any] | None = (),
    policy: Mapping[str, Any] | None = None,
) -> BidConditions:
    """Classify the bid conditions of one project or upload (spec 5.4).

    `document_texts` items are DocumentText, dicts with `text`, `document_id`,
    `page` (1-based) and optional `role`, or plain strings. `policy` is the
    scope policy document or its `bid_conditions` block (defaults: rate table
    pw | open_shop, unknown wage -> prevailing wage)."""
    pol = _policy(policy)
    project = dict(project_row or {})
    docs = _as_documents(document_texts)
    needs_review: list[str] = []
    pw_score, cues = score_cues(docs)
    federal = any(c.cue == "davis_bacon" for c in cues)

    raw_wage = project.get("wage_type")
    record = raw_wage.strip() if isinstance(raw_wage, str) else None
    owner_type = _owner_type(project)
    raw_owner = project.get("owner_type") if isinstance(project.get("owner_type"), str) else None

    if record in WAGE_TYPES:
        wage, basis, confidence = record, "project_record", "high"
        if (record == NON_PREVAILING and pw_score >= CONFLICT_MARGIN) or (
            record == PREVAILING and pw_score <= -CONFLICT_MARGIN
        ):
            needs_review.append(REVIEW_WAGE_CONFLICT)
    else:
        if record:
            needs_review.append(REVIEW_WAGE_THRESHOLD_TEXT)
        if pw_score >= PW_THRESHOLD:
            wage, basis, confidence = PREVAILING, "document_cue", "high"
        elif pw_score <= NON_PW_THRESHOLD:
            wage, basis, confidence = NON_PREVAILING, "document_cue", "high"
        elif owner_type in PUBLIC_OWNER_TYPES:
            wage, basis, confidence = PREVAILING, "owner_type", "medium"
        elif owner_type in PRIVATE_OWNER_TYPES:
            wage, basis, confidence = NON_PREVAILING, "owner_type", "medium"
            needs_review.append(REVIEW_PRIVATE_OWNER)
        elif _lexicon_hit(project.get("name"), docs):
            wage, basis, confidence = PREVAILING, "owner_lexicon", "medium"
            needs_review.append(REVIEW_OWNER_LEXICON)
        else:
            wage, basis, confidence = pol.default_wage_type, "default", "low"
            needs_review.append(WAGE_UNKNOWN_REVIEW)

    labor_time, labor_basis, labor_review = _labor_time(project, docs)
    needs_review.extend(labor_review)
    return BidConditions(
        wage_type=wage,
        wage_basis=basis,
        wage_confidence=confidence,
        federal_wage=federal,
        pw_score=pw_score,
        cues=tuple(cues),
        labor_time=labor_time,
        labor_time_basis=labor_basis,
        union=_union(project, docs),
        bond_required=_bond(docs),
        owner_type=raw_owner,
        rate_table_key=pol.rate_table.get(wage, DEFAULT_RATE_TABLE[wage]),
        needs_review=tuple(dict.fromkeys(needs_review)),
    )


def recheck_threshold(
    bid: BidConditions,
    priced_total_usd: float | None,
    policy: Mapping[str, Any] | None = None,
) -> BidConditions:
    """Re-check the NRS 338 threshold once the pricing engine has a total.

    - A threshold-text project record ("If over $100,000 then PW ...") is
      resolved by the total: at or over the threshold prevailing wage, under it
      non-prevailing (basis project_record).
    - Prevailing wage decided WITHOUT a statute cue and without the project
      record (owner type, lexicon or default) under the threshold adds
      needs_review below_nrs338_threshold.
    Anything else is returned unchanged."""
    if priced_total_usd is None:
        return bid
    pol = _policy(policy)
    threshold = pol.nrs338_min_value_usd
    review = list(bid.needs_review)
    if REVIEW_WAGE_THRESHOLD_TEXT in review and not bid.statute_cue:
        wage = PREVAILING if priced_total_usd >= threshold else NON_PREVAILING
        review = [r for r in review if r not in (REVIEW_WAGE_THRESHOLD_TEXT, WAGE_UNKNOWN_REVIEW)]
        if wage == NON_PREVAILING:
            review.append(REVIEW_BELOW_NRS338)
        return replace(
            bid,
            wage_type=wage,
            wage_basis="project_record",
            wage_confidence="medium",
            rate_table_key=pol.rate_table.get(wage, DEFAULT_RATE_TABLE[wage]),
            needs_review=tuple(dict.fromkeys(review)),
        )
    if (
        bid.wage_type == PREVAILING
        and bid.wage_basis in ("owner_type", "owner_lexicon", "default")
        and not bid.statute_cue
        and priced_total_usd < threshold
        and REVIEW_BELOW_NRS338 not in review
    ):
        review.append(REVIEW_BELOW_NRS338)
        return replace(bid, needs_review=tuple(review))
    return bid


def for_run(sb: Any, run_id: str) -> BidConditions | None:
    """The stored BidConditions of a run (`plans_reader_runs.bid_conditions`).

    Wave 1 stub: the column arrives with migration MD (wave 2). Returns None
    when the column, the row or the value is missing. Sync only: never call
    from inside `async def` (wrap in run_in_threadpool)."""
    if sb is None or not run_id:
        return None
    try:
        res = (
            sb.table("plans_reader_runs")
            .select("bid_conditions")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - missing column before MD is expected
        logger.debug("bid_conditions.for_run(%s) unavailable: %s", run_id, exc)
        return None
    rows = getattr(res, "data", None) or []
    value = rows[0].get("bid_conditions") if rows else None
    if not isinstance(value, Mapping) or value.get("wage_type") not in WAGE_TYPES:
        return None
    try:
        return BidConditions.from_dict(value)
    except (KeyError, TypeError, ValueError):
        return None
