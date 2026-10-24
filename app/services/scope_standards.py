"""G3 installation standards resolved from the documents
(docs/AI_ESTIMATING_BUILD_SPEC.md WS5; fixes E6 and E20).

Precedence of a statement's source: addendum > keynote > sheet note >
schedule note > general note > spec > owner exhibit > house (the policy's
`standards.precedence` may reorder it).

- Code-reference sentences ("FOR EMT FOLLOW NEC 358", "per NEC 358") are
  NEUTRAL: they cite a code article and never permit or require a raceway.
- Raceway: when the documents agree on one branch raceway (EMT, RGS or IMC)
  that value stands; when they conflict, or prohibit the house value with
  nothing else stated, the decision is the house value (3/4" EMT, steel
  set-screw fittings) with a clarification and `needs_review:
  raceway_conflict`. It never lands on EMT silently against a document that
  requires something else. Below-grade, exterior, feeder and riser clauses
  are not branch raceway and are skipped.
- Demolition disposition: a keynote that removes to source beats a sheet note
  that says maintain and cap (E20); the clarification names both notes. A tie
  at one level is the house value plus `needs_review: demo_conflict`.
- Every other standard with `value_patterns` follows the same precedence
  rule; standards without patterns are house values.

Pure: statements in (scope_attribution.Statement or dicts with text, level,
document_id, page, sheet, ref), decisions out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.scope_attribution import Statement, ascii_fold
from app.services.scope_policy import SOURCE_LEVELS, compile_pattern

DEFAULT_PRECEDENCE: tuple[str, ...] = SOURCE_LEVELS
BRANCH_RACEWAYS: tuple[str, ...] = ("EMT", "RGS", "IMC")
RACEWAY_REVIEW = "raceway_conflict"
DEMO_REVIEW = "demo_conflict"
BUILTIN_NEUTRAL_RE = re.compile(r"\b(?:follow|per|comply\s+with)\s+(?:the\s+)?NEC\b|\bNEC\s+(?:article\s+)?\d{3}\b", re.I)
NOT_BRANCH_RE = re.compile(
    r"\bbelow\s+grade\b|\bunderground\b|\bexterior\b|\bexposed\s+to\s+(?:the\s+)?(?:elements|weather)\b"
    r"|\bphysical\s+damage\b|\bthrough-?grade\b|\bstub-?ups?\b|\bwet\s+locations?\b|\boutdoors?\b|\bin\s+slab\b"
    r"|\belbows?\b|\brisers?\b|\bservice\s+entrance\b|\bfeeders?\b|\bunder\s+(?:the\s+)?slab\b|\bdirect\s+burial\b",
    re.I,
)
PROHIBIT_RE = re.compile(
    r"\bshall\s+not\b|\bnot\s+(?:be\s+)?(?:allowed|permitted|acceptable|used)\b|\bprohibited\b|\bshall\s+not\s*$"
    r"|\bnot\s+allowed\b|\bno\s+(?:emt|rigid|mc)\b",
    re.I,
)
PERMIT_RE = re.compile(r"\bmay\s+be\b|\bpermitted\b|\bacceptable\b|\ballowed\b|\bmay\s+use\b", re.I)
# A non-electrical specification page (role `spec`: architectural, door,
# finish sections) decides branch raceway only when it talks about raceway
# at all; "rigid plastic", "hollow metal work to be rigid" never do.
RACEWAY_CONTEXT_RE = re.compile(
    r"\bconduits?\b|\braceways?\b|\bwiring\b|\bbranch\s+circuits?\b|\bEMT\b|\bRGS\b|\bIMC\b|\bRMC\b|\bGRC\b"
    r"|\bmetallic\s+tubing\b|\bintermediate\s+metal\b|\bmc\s+cable\b|\bmetal[\s-]clad\b",
    re.I,
)
# The policy's RGS pattern admits the bare word "rigid"; a clause counts as a
# rigid conduit mention only when "rigid" sits with conduit, steel, metal or
# galvanized ("rigid plastic", "semi-rigid", "rigid hairline joints" on the
# architectural spec pages of 7154 are not raceway).
RIGID_CONDUIT_RE = re.compile(
    r"\bRGS\b|\bRMC\b|\bGRC\b|\brigid\s+(?:galvanized\s+)?(?:steel\s+|metal(?:lic)?\s+)*(?:conduit|steel|metal)\b",
    re.I,
)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.;])\s+|,\s+(?:and\s+)?(?=[A-Za-z])")

# RFI topics each standard decides (scope_engine rewrites RFIs with them).
RFI_TOPICS: dict[str, tuple[str, ...]] = {
    "raceway": (
        r"\braceways?\b(?!\s+for\s+communication)",
        r"\bEMT\b",
        r"\brigid\b",
        r"\bhospital-?grade\s+MC\b",
        r"\bMC\s+(?:cable|prohibition|language)\b",
        r"\bconduit\s+(?:type|method)\b",
        r"\bmetal(?:lic)?\s+cable\b",
    ),
    "demo_disposition": (
        r"\bmaintain(?:ed)?\s+and\s+cap",
        r"\bretained/capped\b",
        r"\bretained\s+(?:or|and|/)\s*capped\b",
        r"\bcapp?(?:ed)?\b.{0,80}\bremov",
        r"\bremov\w*\b.{0,80}\bto\s+(?:the\s+)?source\b",
    ),
}


@dataclass(frozen=True)
class Mention:
    topic: str
    value: str
    polarity: str  # require | permit | prohibit
    level: str
    clause: str
    document_id: str | None
    page: int | None
    sheet: str | None
    ref: str | None
    cite: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "value": self.value,
            "polarity": self.polarity,
            "level": self.level,
            "clause": self.clause,
            "document_id": self.document_id,
            "page": self.page,
            "sheet": self.sheet,
            "ref": self.ref,
            "cite": self.cite,
        }


@dataclass
class StandardDecision:
    topic: str
    value: str
    source: str  # documents | precedence | house | conflict
    level: str
    clarification: str
    needs_review: str | None = None
    cites: list[str] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    conflicts: list[Mention] = field(default_factory=list)
    neutral_refs: list[str] = field(default_factory=list)

    @property
    def cite(self) -> str | None:
        return self.cites[0] if self.cites else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "value": self.value,
            "source": self.source,
            "level": self.level,
            "clarification": self.clarification,
            "needs_review": self.needs_review,
            "cite": self.cite,
            "cites": list(self.cites),
            "mentions": [m.to_dict() for m in self.mentions],
            "conflicts": [m.to_dict() for m in self.conflicts],
            "neutral_refs": list(self.neutral_refs),
            "decided": True,
        }


def _as_statement(obj: Any) -> Statement:
    if isinstance(obj, Statement):
        return obj
    d = dict(obj)
    return Statement(
        text=str(d.get("text") or ""),
        document_id=str(d.get("document_id") or ""),
        page=int(d.get("page") or 0),
        role=str(d.get("role") or "general_note"),
        level=str(d.get("level") or "general_note"),
        sheet=d.get("sheet"),
        ref=d.get("ref"),
        number=d.get("number"),
    )


def _cite(st: Statement, names: Mapping[str, str] | None) -> str:
    name = (names or {}).get(st.document_id)
    if st.sheet:
        tail = f" {st.ref}" if st.ref else ""
        return f"{st.sheet}{tail}"
    return f"{name or 'document'} p.{st.page}"


def precedence_of(policy: Mapping[str, Any] | None) -> tuple[str, ...]:
    std = (policy or {}).get("standards") or {}
    order = std.get("precedence")
    if isinstance(order, list) and sorted(order) == sorted(SOURCE_LEVELS):
        return tuple(order)
    return DEFAULT_PRECEDENCE


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+")


def clauses(text: str, *, split_commas: bool = True) -> list[str]:
    splitter = _CLAUSE_SPLIT_RE if split_commas else _SENTENCE_SPLIT_RE
    return [c.strip() for c in splitter.split(ascii_fold(text)) if c and c.strip()]


def _polarity(clause: str) -> str:
    if PROHIBIT_RE.search(clause):
        return "prohibit"
    if PERMIT_RE.search(clause):
        return "permit"
    return "require"


def topic_mentions(
    statements: Iterable[Any],
    topic: str,
    std: Mapping[str, Any],
    *,
    document_names: Mapping[str, str] | None = None,
    branch_only: bool = False,
) -> tuple[list[Mention], list[str]]:
    """(mentions, neutral clause cites) of one standard topic."""
    patterns: Mapping[str, Sequence[str]] = std.get("value_patterns") or {}
    neutral = [compile_pattern(p) for p in std.get("neutral_patterns") or ()]
    found: list[Mention] = []
    neutral_refs: list[str] = []
    for raw in statements:
        st = _as_statement(raw)
        if branch_only and st.role == "spec" and not RACEWAY_CONTEXT_RE.search(st.text):
            continue  # a non-electrical spec sentence that never names raceway
        cite = _cite(st, document_names)
        for clause in clauses(st.text, split_commas=branch_only):
            hits = [(v, p) for v, plist in patterns.items() for p in plist if compile_pattern(p).search(clause)]
            if not hits:
                continue
            if any(n.search(clause) for n in neutral) or BUILTIN_NEUTRAL_RE.search(clause):
                neutral_refs.append(f"{cite}: {clause[:120]}")
                continue
            if branch_only and NOT_BRANCH_RE.search(clause):
                continue
            pol = _polarity(clause)
            for value in dict.fromkeys(v for v, _p in hits):
                if branch_only and value == "RGS" and not RIGID_CONDUIT_RE.search(clause):
                    continue  # "rigid" without conduit, steel or metal is not a raceway
                found.append(Mention(topic, value, pol, st.level, clause[:240], st.document_id or None,
                                     st.page or None, st.sheet, st.ref, cite))
    return found, neutral_refs


def _rank(level: str, order: Sequence[str]) -> int:
    return order.index(level) if level in order else len(order)


def resolve_raceway(
    statements: Iterable[Any],
    policy: Mapping[str, Any],
    *,
    document_names: Mapping[str, str] | None = None,
) -> StandardDecision:
    std = (policy.get("standards") or {}).get("raceway") or {}
    house = str(std.get("value") or "EMT")
    house_text = str(std.get("text") or 'Branch circuits priced in 3/4" EMT with steel set-screw fittings.')
    review = str(std.get("conflict_review") or RACEWAY_REVIEW)
    order = precedence_of(policy)
    mentions, neutral_refs = topic_mentions(statements, "raceway", std, document_names=document_names, branch_only=True)
    branch = [m for m in mentions if m.value in BRANCH_RACEWAYS]
    positive: dict[str, list[Mention]] = {}
    prohibited: dict[str, list[Mention]] = {}
    for m in branch:
        (prohibited if m.polarity == "prohibit" else positive).setdefault(m.value, []).append(m)
    for vals in positive.values():
        vals.sort(key=lambda m: _rank(m.level, order))
    if not positive:
        if house in prohibited:
            cites = [m.cite for m in prohibited[house]]
            pref = [v for v in std.get("preference") or BRANCH_RACEWAYS if v not in prohibited] or [house]
            return StandardDecision(
                "raceway", pref[0], "conflict", "house",
                f"{house_text} The documents prohibit {house} ({'; '.join(cites)}) and name no other branch raceway; "
                "raceway to be confirmed.",
                needs_review=review, cites=cites, mentions=branch, conflicts=prohibited[house], neutral_refs=neutral_refs,
            )
        return StandardDecision("raceway", house, "house", "house", house_text, mentions=branch,
                                neutral_refs=neutral_refs)
    values = sorted(positive)
    contradicted = [v for v in values if v in prohibited]
    if len(values) == 1 and not contradicted:
        value = values[0]
        best = positive[value][0]
        if value == house:
            text = f"{house_text} ({best.cite} permits {value})." if best.polarity == "permit" else \
                f"{house_text} ({best.cite} requires {value})."
        else:
            text = f"Branch circuits priced in {value} per {best.cite}."
        return StandardDecision("raceway", value, "documents", best.level, text,
                                cites=[m.cite for m in positive[value]], mentions=branch, neutral_refs=neutral_refs)
    conflicts = [m for v in values for m in positive[v]] + [m for v in contradicted for m in prohibited[v]]
    detail = "; ".join(
        f"{m.cite} {m.polarity}s {m.value}" for m in sorted(conflicts, key=lambda m: (_rank(m.level, order), m.cite))
    )
    return StandardDecision(
        "raceway", house, "conflict", "house",
        f"{house_text} The documents conflict on branch raceway ({detail}); priced per the G3 standard.",
        needs_review=review, cites=list(dict.fromkeys(m.cite for m in conflicts)), mentions=branch,
        conflicts=conflicts, neutral_refs=neutral_refs,
    )


def resolve_by_precedence(
    statements: Iterable[Any],
    topic: str,
    policy: Mapping[str, Any],
    *,
    document_names: Mapping[str, str] | None = None,
) -> StandardDecision:
    """The precedence rule for a standard with value_patterns (demolition
    disposition and any policy-added topic)."""
    std = (policy.get("standards") or {}).get(topic) or {}
    house = str(std.get("value") or "")
    text = str(std.get("text") or "")
    review = str(std.get("conflict_review") or f"{topic}_conflict")
    order = list(precedence_of(policy))
    beats = [(b.get("winner"), b.get("loser")) for b in std.get("level_beats") or () if isinstance(b, Mapping)]
    mentions, neutral_refs = topic_mentions(statements, topic, std, document_names=document_names)
    positive = [m for m in mentions if m.polarity != "prohibit"]
    if not positive:
        return StandardDecision(topic, house, "house", "house", text, mentions=mentions, neutral_refs=neutral_refs)
    by_value: dict[str, list[Mention]] = {}
    for m in positive:
        by_value.setdefault(m.value, []).append(m)
    for vals in by_value.values():
        vals.sort(key=lambda m: (_rank(m.level, order), m.cite))
    if len(by_value) == 1:
        value, ms = next(iter(by_value.items()))
        clar = text if value == house else f"{topic.replace('_', ' ').capitalize()} per {ms[0].cite}."
        return StandardDecision(topic, value, "documents", ms[0].level, clar, cites=[m.cite for m in ms],
                                mentions=mentions, neutral_refs=neutral_refs)
    ranked = sorted(by_value.items(), key=lambda kv: _rank(kv[1][0].level, order))
    (win_v, win_ms), (lose_v, lose_ms) = ranked[0], ranked[1]
    win_level, lose_level = win_ms[0].level, lose_ms[0].level
    explicit = (win_level, lose_level) in beats
    if _rank(win_level, order) < _rank(lose_level, order) or explicit:
        loser_cites = ", ".join(dict.fromkeys(m.cite for v, ms in ranked[1:] for m in ms))
        clar = (
            f"{text} {win_ms[0].cite} ({win_level.replace('_', ' ')}) governs over {loser_cites} "
            f"({lose_level.replace('_', ' ')}: {lose_v.replace('_', ' ').lower()})."
        ).strip()
        return StandardDecision(
            topic, win_v, "precedence", win_level, clar,
            cites=list(dict.fromkeys([m.cite for m in win_ms] + [m.cite for v, ms in ranked[1:] for m in ms])),
            mentions=mentions, conflicts=[m for _v, ms in ranked[1:] for m in ms], neutral_refs=neutral_refs,
        )
    cites = list(dict.fromkeys(m.cite for _v, ms in ranked for m in ms))
    return StandardDecision(
        topic, house or win_v, "conflict", "house",
        f"{text} Same-level documents conflict ({'; '.join(cites)}); priced per the G3 standard.".strip(),
        needs_review=review, cites=cites, mentions=mentions, conflicts=[m for _v, ms in ranked for m in ms],
        neutral_refs=neutral_refs,
    )


def resolve(
    statements: Sequence[Any],
    policy: Mapping[str, Any],
    *,
    document_names: Mapping[str, str] | None = None,
) -> dict[str, StandardDecision]:
    """Every standard of the policy resolved against the statements."""
    std_all = policy.get("standards") or {}
    out: dict[str, StandardDecision] = {}
    for topic, std in std_all.items():
        if topic == "precedence" or not isinstance(std, Mapping):
            continue
        if topic == "raceway":
            out[topic] = resolve_raceway(statements, policy, document_names=document_names)
        elif std.get("value_patterns"):
            out[topic] = resolve_by_precedence(statements, topic, policy, document_names=document_names)
        else:
            out[topic] = StandardDecision(topic, str(std.get("value") or ""), "house", "house", str(std.get("text") or ""))
    return out


def decisions_dict(decisions: Mapping[str, StandardDecision]) -> dict[str, dict[str, Any]]:
    return {k: v.to_dict() for k, v in decisions.items()}


__all__ = [
    "DEFAULT_PRECEDENCE", "BRANCH_RACEWAYS", "RACEWAY_REVIEW", "DEMO_REVIEW", "RFI_TOPICS", "Mention",
    "StandardDecision", "precedence_of", "clauses", "topic_mentions", "resolve_raceway", "resolve_by_precedence",
    "resolve", "decisions_dict",
]
