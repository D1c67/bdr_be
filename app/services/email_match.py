"""Email→project subject matching (identification round 2): the exact
deterministic rules below, then a fuzzy name score (r2_score) built on the RFP
matcher's normalization, which assigns only when it is high, clear of the
runner-up and conflict-free.

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
from dataclasses import dataclass, field

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


# ── Fuzzy round-2 scoring (the RFP matcher's name score) ──────────────────────

# Stored per email (match_candidates) for the triage UI and the R3 prompt.
FUZZY_TOP_N = 5

MATCH_KIND_NUMBER = "number"
MATCH_KIND_NAME = "name"
MATCH_KIND_FUZZY = "fuzzy"


@dataclass(frozen=True)
class R2Result:
    """Round 2's answer: the project to assign (or None), how it was found,
    the similarity behind it (1.0 for an exact number or name hit), and the
    top candidates by similarity so a human (and R3) can see the near-misses."""

    project_id: str | None
    kind: str | None
    similarity: float | None
    candidates: list[dict] = field(default_factory=list)


# Partial credit for a project-name token the subject only approximates:
# an abbreviation ("Elem" for "Elementary") or a typo ("Henderon").
_PREFIX_CREDIT = 0.9
_TYPO_CREDIT = 0.85
_MIN_PREFIX_LEN = 3
_MIN_TYPO_LEN = 5
_TYPO_DICE = 0.75


def _token_credit(project_token: str, subject_tokens: set[str]) -> float:
    if project_token in subject_tokens:
        return 1.0
    if not project_token.isalpha():
        return 0.0  # numbers are discriminators: exact or nothing
    from app.services import rfp_match

    best = 0.0
    for tok in subject_tokens:
        if not tok.isalpha():
            continue
        short, long_ = (tok, project_token) if len(tok) <= len(project_token) else (project_token, tok)
        if len(short) >= _MIN_PREFIX_LEN and long_.startswith(short):
            best = max(best, _PREFIX_CREDIT)
        elif (
            len(short) >= _MIN_TYPO_LEN
            and rfp_match._dice(tok, project_token) >= _TYPO_DICE
        ):
            best = max(best, _TYPO_CREDIT)
    return best


def _coverage(subject_tokens: list[str], project_tokens: list[str]) -> float:
    """How much of the PROJECT's name the subject carries (an email subject is
    a project name plus other words, so the direction matters: a short
    generic subject must not fully match a longer name). Counted only when
    the name has two tokens, or one of six or more characters (the RFP
    containment rule). Single letters stay out of the denominator."""
    wanted = [t for t in dict.fromkeys(project_tokens) if len(t) > 1 or t.isdigit()]
    if not wanted:
        return 0.0
    if len(wanted) < 2 and not any(len(t) >= 6 for t in wanted):
        return 0.0
    have = set(subject_tokens)
    return sum(_token_credit(t, have) for t in wanted) / len(wanted)


def similarity(subject: str, project: dict, settings) -> tuple[float, dict | None]:
    """Fuzzy subject-to-project score, built on the RFP matcher's name
    normalization (rfp_match: reference numbers, stop words, roman numerals
    and ordinals) and its discriminator conflict cap (Phase 1 vs Phase 2,
    Station 7 vs Station 12): max(trigram Dice, project-name coverage with
    prefix and typo credit). The project's own number is stripped from both
    sides so it never reads as a conflict."""
    from app.services import rfp_match

    number = project.get("number") or None
    name = project.get("name") or ""
    base = rfp_match.name_score(subject, name, a_number=number, b_number=number, settings=settings)
    subject_tokens = rfp_match.normalize_project_name(subject, project_number=number).split()
    project_tokens = rfp_match.normalize_project_name(name, project_number=number).split()
    score = max(base.dice, _coverage(subject_tokens, project_tokens))
    if base.conflict is not None:
        score = min(score, float(settings.rfp_match_conflict_cap))
    return round(score, 4), base.conflict


def _stems(norm: str) -> set[str]:
    """First three letters of each alphabetic word of 3+ chars: the cheap
    gate in front of the fuzzy score (a prefix or typo match shares them,
    almost always)."""
    return {tok[:3] for tok in norm.split() if len(tok) >= 3 and tok.isalpha()}


def r2_score(subject: str, projects: list[dict], settings) -> R2Result:
    """Exact rules first (r2_match: a single job-number hit, else a single
    name hit), then the fuzzy name score. A fuzzy best assigns only when it
    clears EMAIL_MATCH_FUZZY_AUTO_THRESHOLD with the runner-up more than
    EMAIL_MATCH_FUZZY_RUNNER_UP_GAP below, has no discriminator conflict, and
    the exact rules were not ambiguous (two projects hit = R3 decides)."""
    subject_norm = normalize(subject)
    if not subject_norm:
        return R2Result(None, None, None, [])
    subject_dehyph = _dehyphenated(subject)
    subject_stems = _stems(subject_norm)

    scored: list[dict] = []
    number_matches: set[str] = set()
    name_matches: set[str] = set()
    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        number_hit = _number_hits(subject_norm, subject_dehyph, p.get("number") or "")
        name_hit = not number_hit and _name_hits(subject_norm, p.get("name") or "")
        if number_hit or name_hit:
            (number_matches if number_hit else name_matches).add(pid)
            score, conflict = 1.0, None
        elif not subject_stems & _stems(normalize(p.get("name") or "")):
            continue  # no word in common: a near-zero score not worth computing
        else:
            score, conflict = similarity(subject, p, settings)
        if score <= 0:
            continue
        scored.append({
            "project_id": pid,
            "number": p.get("number"),
            "name": p.get("name"),
            "similarity": round(float(score), 3),
            "kind": (MATCH_KIND_NUMBER if number_hit
                     else MATCH_KIND_NAME if name_hit else MATCH_KIND_FUZZY),
            "conflict": conflict,
        })
    scored.sort(key=lambda c: (-c["similarity"], c["kind"] != MATCH_KIND_NUMBER,
                               c["number"] or "", c["project_id"]))
    top = scored[:FUZZY_TOP_N]

    # The r2_match rule, read off the same loop: a single number hit, else
    # (no number hits at all) a single name hit.
    if len(number_matches) == 1:
        return R2Result(next(iter(number_matches)), MATCH_KIND_NUMBER, 1.0, top)
    if not number_matches and len(name_matches) == 1:
        return R2Result(next(iter(name_matches)), MATCH_KIND_NAME, 1.0, top)
    if number_matches or name_matches or not top:
        return R2Result(None, None, None, top)

    best = top[0]
    runner_up = top[1]["similarity"] if len(top) > 1 else 0.0
    threshold = float(settings.email_match_fuzzy_auto_threshold)
    gap = float(settings.email_match_fuzzy_runner_up_gap)
    if (
        best["similarity"] >= threshold
        and runner_up < best["similarity"] - gap
        and best["conflict"] is None
    ):
        return R2Result(best["project_id"], MATCH_KIND_FUZZY, best["similarity"], top)
    return R2Result(None, None, None, top)


def could_match(subject: str, project: dict, settings) -> bool:
    """Whether round 2 could pick `project` for this subject at all: an exact
    number or name hit, or a fuzzy score at the auto threshold. The rescan
    uses it to skip the full-book r2_score for most of the Unknown pool."""
    subject_norm = normalize(subject)
    if not subject_norm:
        return False
    if _number_hits(subject_norm, _dehyphenated(subject), project.get("number") or ""):
        return True
    if _name_hits(subject_norm, project.get("name") or ""):
        return True
    score, conflict = similarity(subject, project, settings)
    return conflict is None and score >= float(settings.email_match_fuzzy_auto_threshold)
