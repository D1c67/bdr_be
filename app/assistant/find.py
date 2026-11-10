"""find_tools resolver (docs/ASSISTANT_PHASE4_PLAN.md 3.16.1, deviation D11).

WI-1 created the stub; WI-10 filled it. The public names and signatures
below are binding: WI-9b's `GET /assistant/tools/find` route and WI-10's
loop and prompt code against them. Pure: no database, no network, no router
or service import.

Scoring (3.16.1): the query is lowercased and split into ASCII words, stop
words dropped and plurals folded ("drawings" -> "drawing", "rfis" -> "rfi",
"entries" -> "entry"). Each distinct query word adds 3 when it is a word of
the tool's name, 3 when it is a word of one of its `keywords`, 2 when it is
the namespace's name and 1 when it is a word of the description. Only
deferred specs are candidates (core tools are always offered), a spec with
no hit is never returned, and ties go by name, so the same query over the
same toolset always gives the same list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from app.assistant.registry import EFFECT_READ, ToolSpec

# The tool and skill names the loop watches (3.16.3).
FIND_TOOLS = "find_tools"
LOAD_SKILL = "load_skill"

DEFAULT_LIMIT = 6
MAX_LIMIT = 20

WEIGHT_NAME = 3
WEIGHT_KEYWORD = 3
WEIGHT_NAMESPACE = 2
WEIGHT_DESCRIPTION = 1

# Function words and the words every tool description opens with ("Use
# when ..."). "who" and "why" stay: they are what evidence questions ask.
STOP_WORDS: frozenset[str] = frozenset({
    "a", "about", "after", "all", "also", "am", "an", "and", "any", "are", "as", "at", "be", "been",
    "before", "but", "by", "can", "could", "did", "do", "does", "for", "from", "give", "had", "has",
    "have", "how", "i", "if", "in", "into", "is", "it", "its", "just", "let", "me", "my", "need",
    "of", "on", "one", "or", "our", "please", "show", "should", "so", "some", "tell", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "those", "to", "us", "use",
    "used", "want", "was", "we", "were", "what", "when", "where", "which", "will", "with", "would",
    "you", "your",
})

_WORD_RE = re.compile(r"[a-z0-9]+")

# The deferred areas the prompt's catalog line names, in a fixed order
# (3.16.1). A deferred bidding read is a report (quotes_summary,
# run_report, coded_report); a deferred bidding change is moving a lane
# (advance_lane).
AREA_PLANS = "plans and drawings"
AREA_REPORTS = "reports and spreadsheets"
AREA_EVIDENCE = "evidence (who did what and when)"
AREA_PM = "PM and RFIs"
AREA_PAYROLL = "certified payroll"
AREA_LANE = "moving a lane"
AREA_MAIL = "email and mailbox review"
AREA_WEB = "public web search and reading"
# Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.1 item 1): right after web.
AREA_BROWSER = "websites that need your login (browser)"
# Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md 6.1): the inbox_list read tool.
AREA_INBOX = "your assistant Inbox (alerts, flags and digests)"
AREA_ORDER: tuple[str, ...] = (AREA_MAIL, AREA_WEB, AREA_BROWSER, AREA_INBOX, AREA_PLANS, AREA_REPORTS, AREA_EVIDENCE, AREA_PM, AREA_PAYROLL, AREA_LANE)
_NAMESPACE_AREAS = {
    "mail": AREA_MAIL,
    "web": AREA_WEB,
    "browser": AREA_BROWSER,
    "inbox": AREA_INBOX,
    "plans": AREA_PLANS,
    "report": AREA_REPORTS,
    "evidence": AREA_EVIDENCE,
    "pm": AREA_PM,
    "payroll": AREA_PAYROLL,
}
CATALOG_PREFIX = "More tools load on demand with find_tools: "


def fold(word: str) -> str:
    """The simple plural fold applied to both sides: "ies" -> "y",
    "sses" -> "ss", a final "s" dropped (not "ss", not words of three
    letters or fewer)."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str | None) -> frozenset[str]:
    """The folded, stop-word-free ASCII words of `text`."""
    words = _WORD_RE.findall(str(text or "").lower())
    return frozenset(fold(w) for w in words if w not in STOP_WORDS)


def _words(values: Iterable[str]) -> frozenset[str]:
    out: set[str] = set()
    for value in values:
        out |= tokens(value)
    return frozenset(out)


def score(query_tokens: Iterable[str], spec: ToolSpec) -> int:
    """The weighted hit count of `query_tokens` on one spec (3.16.1)."""
    name_words = tokens(spec.name.replace("_", " "))
    keyword_words = _words(spec.keywords or ())
    namespace_words = tokens(spec.namespace)
    description_words = tokens(spec.description)
    total = 0
    for word in set(query_tokens):
        if word in name_words:
            total += WEIGHT_NAME
        if word in keyword_words:
            total += WEIGHT_KEYWORD
        if word in namespace_words:
            total += WEIGHT_NAMESPACE
        if word in description_words:
            total += WEIGHT_DESCRIPTION
    return total


def resolve(query: str, specs: Sequence[ToolSpec], limit: int = 6) -> list[ToolSpec]:
    """The deferred tools that best match `query`.

    Lowercase tokens of the query (stop words removed, simple plural fold),
    scored against the spec's name tokens (weight 3), `keywords` (weight 3),
    description words (weight 1) and namespace name (weight 2); ties by name.
    `specs` is the person's current toolset (role, sub-app, disabled, mode);
    only `deferred` specs are returned (core tools are already present).
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = DEFAULT_LIMIT
    cap = max(0, min(MAX_LIMIT, cap))
    wanted = tokens(query)
    if not wanted or cap == 0:
        return []
    scored: list[tuple[int, str, ToolSpec]] = []
    seen: set[str] = set()
    for spec in specs:
        if not getattr(spec, "deferred", False) or spec.name in seen:
            continue
        seen.add(spec.name)
        points = score(wanted, spec)
        if points > 0:
            scored.append((-points, spec.name, spec))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [spec for _neg, _name, spec in scored[:cap]]


def area_for(spec: ToolSpec) -> str | None:
    """The catalog area of one deferred spec, or None when it has none."""
    if spec.namespace == "bidding":
        return AREA_REPORTS if spec.effect == EFFECT_READ else AREA_LANE
    return _NAMESPACE_AREAS.get(spec.namespace)


def catalog_line(specs: Sequence[ToolSpec]) -> str:
    """One prompt line naming the deferred areas present, for example "More
    tools load on demand with find_tools: plans and drawings, reports and
    spreadsheets, evidence (who did what and when), PM and RFIs, certified
    payroll, moving a lane." Built from the namespaces present; no em dash."""
    present = {area_for(spec) for spec in specs if getattr(spec, "deferred", False)}
    areas = [area for area in AREA_ORDER if area in present]
    if not areas:
        return ""
    return CATALOG_PREFIX + ", ".join(areas) + "."
