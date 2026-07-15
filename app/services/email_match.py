"""Deterministic email→project subject matching (identification round 2).

Pure functions, no I/O — fully unit-testable. The guardrails exist because a
false auto-assignment is worse than an Unknown: users treat project email lists
as truth (and every assignment teaches the conversation map), so R2 only
assigns on a single unambiguous hit and leaves everything else to the LLM
round (R3) or manual triage.

Guardrails, each pinned by tests:
- job numbers need ≥ 4 alphanumeric chars ("21" never hits "21st Street");
- matches use non-alphanumeric lookaround boundaries ("26-104" ≠ "26-1040");
- the hyphen-insensitive fallback only collapses SINGLE-joiner tokens, so CSI
  spec sections ("26.05.19") never impersonate job numbers ("26-0519");
- bare calendar years are unmatchable ("20-26" must not hit "2026 forecast");
- names need real length and a substantial token (no generic one-worders).
"""

import difflib
import re
import unicodedata

# A job number must have at least this many alphanumeric characters to be
# matchable — a legacy number like "21" must never hit "21st Street".
_MIN_NUMBER_ALNUM = 4
# A project name must normalize to at least this length, with at least one
# token of _MIN_NAME_TOKEN chars, to be matchable (blocks generic one-worders).
_MIN_NAME_LEN = 5
_MIN_NAME_TOKEN = 4

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Joiners that glue number fragments together inside one token.
_JOINER_BETWEEN = re.compile(r"(?<=[a-z0-9])[-_./](?=[a-z0-9])")
# Maximal alnum-with-joiners runs, used to collapse tokens individually.
_TOKEN = re.compile(r"[a-z0-9]+(?:[-_./][a-z0-9]+)*")
_YEARLIKE = re.compile(r"(19|20)\d\d")


def _prepare(text: str) -> str:
    """NFKC (fullwidth digits → ASCII, etc.) + lowercase."""
    return unicodedata.normalize("NFKC", text or "").lower()


def normalize(text: str) -> str:
    """Lowercase, punctuation→space, collapsed whitespace."""
    return _NON_ALNUM.sub(" ", _prepare(text)).strip()


def _squash(text: str) -> str:
    """All alphanumerics, no separators at all."""
    return _NON_ALNUM.sub("", _prepare(text))


def _dehyphenated(text: str) -> str:
    """Like normalize(), but a token containing EXACTLY ONE joiner keeps its
    fragments glued ("26-104" → "26104"). Tokens with two or more joiners are
    left to normalize()'s splitting — collapsing "26.05.19" to "260519" would
    let spec-section references impersonate job numbers."""

    def collapse(match: re.Match) -> str:
        token = match.group(0)
        if len(_JOINER_BETWEEN.findall(token)) == 1:
            return _JOINER_BETWEEN.sub("", token)
        return token

    return normalize(_TOKEN.sub(collapse, _prepare(text)))


def _phrase_pattern(phrase: str) -> re.Pattern:
    """Boundary-guarded phrase match. Lookarounds are non-alphanumeric (not
    \\b), so "26 104" never matches inside "26 1040"."""
    return re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])")


def _number_hits(subject_norm: str, subject_dehyph: str, number: str) -> bool:
    squashed = _squash(number)
    if len(squashed) < _MIN_NUMBER_ALNUM:
        return False
    if _YEARLIKE.fullmatch(squashed):
        # "20-26" / "2026" would hit every "2026 rate sheet" subject — a bare
        # calendar year is too ambiguous for deterministic assignment.
        return False
    norm = normalize(number)
    if norm and _phrase_pattern(norm).search(subject_norm):
        return True
    # Hyphen-insensitive fallback: "26104" ↔ "26-104" in either direction.
    # Only for 0/1-joiner numbers — multi-joiner numbers keep exact-form only.
    if len(_JOINER_BETWEEN.findall(_prepare(number))) <= 1:
        return bool(_phrase_pattern(squashed).search(subject_dehyph))
    return False


def _name_hits(subject_norm: str, name: str) -> bool:
    norm = normalize(name)
    if len(norm) < _MIN_NAME_LEN:
        return False
    if not any(len(tok) >= _MIN_NAME_TOKEN for tok in norm.split()):
        return False
    return bool(_phrase_pattern(norm).search(subject_norm))


def r2_match(subject: str, projects: list[dict]) -> str | None:
    """Return a project id ONLY when the subject names exactly one project.

    `projects` rows need `id`, `number`, `name`. Job-number hits outrank name
    hits (numbers are unique by design — 0052). Zero hits or an ambiguous
    (≥2 projects) result returns None so R3 / manual triage decides.
    """
    subject_norm = normalize(subject)
    if not subject_norm:
        return None
    subject_dehyph = _dehyphenated(subject)

    number_matches: set[str] = set()
    name_matches: set[str] = set()
    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        if _number_hits(subject_norm, subject_dehyph, p.get("number") or ""):
            number_matches.add(pid)
        elif _name_hits(subject_norm, p.get("name") or ""):
            name_matches.add(pid)

    if len(number_matches) == 1:
        return next(iter(number_matches))
    if not number_matches and len(name_matches) == 1:
        return next(iter(name_matches))
    return None


def prefilter_candidates(subject: str, projects: list[dict], max_n: int) -> list[dict]:
    """Deterministically rank projects by subject affinity and keep the top
    `max_n`, bounding the R3 prompt when the project book grows large.

    Any project whose number or name actually HITS the subject (same logic as
    r2_match — R3 exists precisely for the multi-hit ambiguous cases) is
    force-included ahead of the score ranking, so the true candidate can never
    be cut by a difflib tie against irrelevant projects.
    """
    if len(projects) <= max_n:
        return list(projects)
    subject_norm = normalize(subject)
    subject_dehyph = _dehyphenated(subject)
    subject_tokens = set(subject_norm.split())

    forced: list[dict] = []
    rest: list[dict] = []
    for p in projects:
        if _number_hits(subject_norm, subject_dehyph, p.get("number") or "") or _name_hits(
            subject_norm, p.get("name") or ""
        ):
            forced.append(p)
        else:
            rest.append(p)

    def score(p: dict) -> tuple:
        label = normalize(f"{p.get('number') or ''} {p.get('name') or ''}")
        shared = len(subject_tokens & set(label.split()))
        ratio = difflib.SequenceMatcher(None, subject_norm, label).ratio()
        return (-shared, -ratio, p.get("number") or "", p.get("id") or "")

    forced = forced[:max_n]
    return forced + sorted(rest, key=score)[: max_n - len(forced)]
