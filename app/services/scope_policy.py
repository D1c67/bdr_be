"""G3 scope policy document: model, validation, hashing and the active-policy
accessor (docs/AI_ESTIMATING_BUILD_SPEC.md 5.8 and WS5).

The policy is a plain JSON document (stored in `ai_scope_policies.doc` from
wave 2 on; seeded by `scope_policy_seed.SEED_POLICY`):

    {
      "version": int >= 1,
      "bid_conditions": {"rate_table": {wage_type: "pw" | "open_shop"},
                         "default_wage_type": wage_type,
                         "nrs338_min_value_usd": number, "evidence": [...] (optional)},
      "standards": {"precedence": [source levels, highest first],
                    "<topic>": {"value", "text", "evidence", ...}, ...},
      "rules": [{"rule_id", "topic", "kind" hard|soft,
                 "decision" include|exclude|allowance|by_others,
                 "match": {"patterns", "systems", "exempt_subgroups", "exempt_patterns"},
                 "unless": [condition] (soft rules only),
                 "default_text", "proposal_line", "evidence", ...}],
      "inclusions": [{"trigger_family", "pattern", "line_template", "dedupe_scope",
                      "rule_id" (optional), "evidence" (optional), ...}],
      "allowances": {"standard": [names], "standard_amount_usd": 0,
                     "extra_requires_document_amount": true,
                     "evidence": [...] (optional), ...}
    }

The exact 5.8 shape (no evidence on bid_conditions, inclusions and
allowances; no inclusion rule_id) is valid; the extended shape the seed uses
(evidence everywhere, explicit inclusion ids) is valid too. An inclusion
without a rule_id gets `IN-<TRIGGER-FAMILY>-<n>` (`inclusion_rule_ids`), n
counting that family's inclusions in document order.

`validate(doc)` is the only gate: a policy that fails it cannot be activated.
It refuses `unless` on hard rules, regexes that do not compile or are not
word-bounded, missing evidence on rules and standards, non-ASCII text (dashes included), unknown BOQ
vocabulary and documents over 200 kB. `policy_sha(doc)` is the sha256 of the
canonical JSON (sorted keys, compact separators).

Matching is case-insensitive. A pattern is word-bounded when both ends are
anchored: it starts with `\\b`, `^` or a lookbehind (or a non-word literal
such as `#` or `$`) and ends with `\\b`, `$` or a non-word literal, optionally
followed by lookaheads (so `\\bpatch\\b(?!\\s*(?:panel|cord))` is bounded).

The active-policy accessor is in memory in wave 1: `active_policy()` returns
an explicitly set policy, else what a registered loader returns (wave 2
registers a loader backed by the `ai_scope_policies` table), else the code
seed. This module is pure: no database, no network, no model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.estimating_contracts import WAGE_TYPES
from app.services.plans_reader_schemas import BOQ_GROUPS, BOQ_LEVEL0, BOQ_SUBGROUPS, UNITS

logger = logging.getLogger(__name__)

try:  # Python 3.11+
    from re import _constants as _sre_c  # type: ignore[attr-defined]
    from re import _parser as _sre_parse  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - older interpreters
    import sre_constants as _sre_c  # type: ignore[no-redef]
    import sre_parse as _sre_parse  # type: ignore[no-redef]

POLICY_SCHEMA_VERSION = 1
MAX_DOC_BYTES = 200_000
MAX_PROPOSAL_LINE_CHARS = 300

RULE_KINDS: tuple[str, ...] = ("hard", "soft")
RULE_DECISIONS: tuple[str, ...] = ("include", "exclude", "allowance", "by_others")
UNLESS_CONDITIONS: tuple[str, ...] = (
    "drawn_with_quantity",
    "assigned_to_ec_explicitly",
    "owner_allowance_amount",
)
DEDUPE_SCOPES: tuple[str, ...] = ("sheet", "run", "run_new_work_wins")
INCLUSION_QUANTITIES: tuple[str, ...] = (
    "sum_trigger_counts",
    "measured_else_per_trigger",
    "one_per_run",
)
SOURCE_LEVELS: tuple[str, ...] = (
    "addendum",
    "keynote",
    "sheet_note",
    "schedule_note",
    "general_note",
    "spec",
    "owner_exhibit",
    "house",
)
RATE_TABLE_KEYS: tuple[str, ...] = ("pw", "open_shop")
REQUIRED_STANDARDS: tuple[str, ...] = ("raceway", "branch_conduit", "demo_disposition")
TOP_KEYS: tuple[str, ...] = (
    "version",
    "bid_conditions",
    "standards",
    "rules",
    "inclusions",
    "allowances",
)
OPTIONAL_TOP_KEYS: tuple[str, ...] = ("schema_version", "notes")

RULE_KEYS: frozenset[str] = frozenset(
    {
        "rule_id",
        "topic",
        "kind",
        "decision",
        "match",
        "unless",
        "default_text",
        "proposal_line",
        "evidence",
    }
)
RULE_OPTIONAL_KEYS: frozenset[str] = frozenset({"label", "template_covered", "enabled", "review"})
MATCH_KEYS: frozenset[str] = frozenset(
    {"patterns", "systems", "exempt_subgroups", "exempt_patterns"}
)
INCLUSION_KEYS: frozenset[str] = frozenset(
    {"trigger_family", "pattern", "line_template", "dedupe_scope"}
)
INCLUSION_OPTIONAL_KEYS: frozenset[str] = frozenset(
    {"rule_id", "evidence", "also_pattern", "companion_of", "clarification", "enabled", "settings_key"}
)
LINE_TEMPLATE_KEYS: frozenset[str] = frozenset(
    {"section", "group", "subgroup", "description", "unit", "quantity"}
)
LINE_TEMPLATE_OPTIONAL_KEYS: frozenset[str] = frozenset(
    {"wastage_applies", "material_category", "caveat_when_assumed"}
)

_RULE_ID_RE = re.compile(r"^[A-Z]{2,}(?:-[A-Z0-9]+)+$")
_TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DASH_CHARS = ("\u2013", "\u2014")


class PolicyInvalid(ValueError):
    """Raised by `validate` with every problem found (never only the first)."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        head = "; ".join(self.errors[:5])
        more = f" (+{len(self.errors) - 5} more)" if len(self.errors) > 5 else ""
        super().__init__(f"invalid scope policy: {head}{more}")


@dataclass(frozen=True)
class PolicyInfo:
    """What `validate` returns for a valid document."""

    version: int
    sha256: str
    rule_ids: tuple[str, ...]
    size_bytes: int


# Hashing, load and dump -------------------------------------------------------------


def canonical_json(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def policy_sha(doc: Mapping[str, Any]) -> str:
    """sha256 hex of the canonical JSON; stable across key order."""
    return hashlib.sha256(canonical_json(doc).encode("ascii")).hexdigest()


def dump(doc: Mapping[str, Any]) -> str:
    """Readable JSON (insertion order kept, ASCII only) with a final newline."""
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def loads(text: str | bytes) -> dict[str, Any]:
    """Parse and validate a policy document; raises PolicyInvalid."""
    try:
        doc = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise PolicyInvalid([f"not valid JSON: {exc}"]) from exc
    validate(doc)
    return doc


def load(path: str | Path) -> dict[str, Any]:
    """Read, parse and validate a policy file."""
    return loads(Path(path).read_text(encoding="utf-8"))


# Regex checks -----------------------------------------------------------------------


def _is_word_literal(code: int) -> bool:
    ch = chr(code)
    return ch.isalnum() or ch == "_"


def _left_bounded(items: Iterable[tuple[Any, Any]]) -> bool:
    for op, av in items:
        if op is _sre_c.AT:
            return av in (_sre_c.AT_BEGINNING, _sre_c.AT_BEGINNING_STRING, _sre_c.AT_BOUNDARY)
        if op in (_sre_c.ASSERT, _sre_c.ASSERT_NOT):
            direction, _sub = av
            if direction < 0:
                return True  # a lookbehind guards the start
            continue  # a leading lookahead does not consume; keep scanning
        if op is _sre_c.SUBPATTERN:
            return _left_bounded(av[-1])
        if op is _sre_c.BRANCH:
            return all(_left_bounded(branch) for branch in av[1])
        if op in (_sre_c.MAX_REPEAT, _sre_c.MIN_REPEAT):
            lo, _hi, sub = av
            return lo >= 1 and _left_bounded(sub)
        if op is _sre_c.LITERAL:
            return not _is_word_literal(av)
        return False
    return False


def _right_bounded(items: Iterable[tuple[Any, Any]]) -> bool:
    for op, av in reversed(list(items)):
        if op is _sre_c.AT:
            return av in (_sre_c.AT_END, _sre_c.AT_END_STRING, _sre_c.AT_BOUNDARY)
        if op in (_sre_c.ASSERT, _sre_c.ASSERT_NOT):
            direction, _sub = av
            if direction > 0:
                continue  # a trailing lookahead narrows, the boundary is before it
            return False
        if op is _sre_c.SUBPATTERN:
            return _right_bounded(av[-1])
        if op is _sre_c.BRANCH:
            return all(_right_bounded(branch) for branch in av[1])
        if op in (_sre_c.MAX_REPEAT, _sre_c.MIN_REPEAT):
            lo, _hi, sub = av
            return lo >= 1 and _right_bounded(sub)
        if op is _sre_c.LITERAL:
            return not _is_word_literal(av)
        return False
    return False


def regex_problem(pattern: Any) -> str | None:
    """None when `pattern` compiles case-insensitively and is word-bounded on
    both ends; else a short reason."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "pattern must be a non-empty string"
    try:
        re.compile(pattern, re.IGNORECASE)
        parsed = _sre_parse.parse(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"does not compile ({exc})"
    if not _left_bounded(parsed):
        return "not word-bounded at the start (use \\b, ^ or a lookbehind)"
    if not _right_bounded(parsed):
        return "not word-bounded at the end (use \\b or $; lookaheads may follow)"
    return None


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Case-insensitive compiled pattern (cached)."""
    return _compiled(pattern)


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str]:
    hit = _PATTERN_CACHE.get(pattern)
    if hit is None:
        hit = re.compile(pattern, re.IGNORECASE)
        _PATTERN_CACHE[pattern] = hit
    return hit


# Validation -------------------------------------------------------------------------


def _walk_strings(value: Any, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(k, str):
                yield f"{path}.<key>", k
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _walk_strings(v, f"{path}[{i}]")


def _check_text(where: str, text: str, errors: list[str]) -> None:
    for ch in _DASH_CHARS:
        if ch in text:
            errors.append(f"{where}: en or em dash (U+{ord(ch):04X}) not allowed")
            return
    if any(ord(c) > 127 for c in text):
        errors.append(f"{where}: non-ASCII character")


def _check_regex_list(where: str, value: Any, errors: list[str], *, allow_empty: bool) -> None:
    if not isinstance(value, list):
        errors.append(f"{where}: must be a list of regexes")
        return
    if not value and not allow_empty:
        errors.append(f"{where}: at least one pattern required")
    for i, pattern in enumerate(value):
        problem = regex_problem(pattern)
        if problem:
            errors.append(f"{where}[{i}] {pattern!r}: {problem}")


def _check_str_list(where: str, value: Any, errors: list[str]) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        errors.append(f"{where}: must be a list of non-empty strings")


def _check_evidence(where: str, value: Any, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: evidence required (a non-empty list)")
        return
    for i, ev in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(ev, Mapping):
            errors.append(f"{at}: must be an object")
            continue
        if not isinstance(ev.get("source"), str) or not ev.get("source"):
            errors.append(f"{at}.source: required string")
        support, of = ev.get("support"), ev.get("of")
        if not isinstance(support, int) or isinstance(support, bool) or support < 0:
            errors.append(f"{at}.support: must be an integer >= 0")
        if not isinstance(of, int) or isinstance(of, bool) or of < 1:
            errors.append(f"{at}.of: must be an integer >= 1")
        elif isinstance(support, int) and support > of:
            errors.append(f"{at}: support {support} exceeds of {of}")
        note = ev.get("note")
        if note is not None and not isinstance(note, str):
            errors.append(f"{at}.note: must be a string or null")
        extra = set(ev) - {"source", "support", "of", "note"}
        if extra:
            errors.append(f"{at}: unknown keys {sorted(extra)}")


def _check_keys(
    where: str,
    obj: Mapping[str, Any],
    required: Iterable[str],
    optional: Iterable[str],
    errors: list[str],
) -> bool:
    req = set(required)
    missing = sorted(req - set(obj))
    extra = sorted(set(obj) - req - set(optional))
    if missing:
        errors.append(f"{where}: missing keys {missing}")
    if extra:
        errors.append(f"{where}: unknown keys {extra}")
    return not missing


def _check_proposal_line(where: str, value: Any, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: must be a non-empty string or null")
        return
    if len(value) > MAX_PROPOSAL_LINE_CHARS:
        errors.append(f"{where}: longer than {MAX_PROPOSAL_LINE_CHARS} characters")
    if not value.rstrip().endswith("."):
        errors.append(f"{where}: must end with a period (G3 proposal line style)")


def _check_bid_conditions(bc: Any, errors: list[str]) -> None:
    where = "bid_conditions"
    if not isinstance(bc, Mapping):
        errors.append(f"{where}: must be an object")
        return
    _check_keys(
        where,
        bc,
        ("rate_table", "default_wage_type"),
        ("nrs338_min_value_usd", "evidence"),
        errors,
    )
    rate_table = bc.get("rate_table")
    if not isinstance(rate_table, Mapping) or set(rate_table) != set(WAGE_TYPES):
        errors.append(f"{where}.rate_table: must map exactly {list(WAGE_TYPES)}")
    else:
        for wage, key in rate_table.items():
            if key not in RATE_TABLE_KEYS:
                errors.append(f"{where}.rate_table.{wage}: must be one of {list(RATE_TABLE_KEYS)}")
    if bc.get("default_wage_type") not in WAGE_TYPES:
        errors.append(f"{where}.default_wage_type: must be one of {list(WAGE_TYPES)}")
    threshold = bc.get("nrs338_min_value_usd")
    if threshold is not None and (
        not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold < 0
    ):
        errors.append(f"{where}.nrs338_min_value_usd: must be a number >= 0")
    if "evidence" in bc:
        _check_evidence(f"{where}.evidence", bc["evidence"], errors)


def _check_standards(standards: Any, errors: list[str]) -> None:
    where = "standards"
    if not isinstance(standards, Mapping):
        errors.append(f"{where}: must be an object")
        return
    for key in REQUIRED_STANDARDS:
        if key not in standards:
            errors.append(f"{where}: missing required standard {key!r}")
    for topic, std in standards.items():
        at = f"{where}.{topic}"
        if not isinstance(topic, str) or not _TOPIC_RE.match(topic):
            errors.append(f"{at}: topic must be snake_case")
            continue
        if topic == "precedence":
            if not isinstance(std, list) or sorted(std) != sorted(SOURCE_LEVELS) or len(std) != len(
                SOURCE_LEVELS
            ):
                errors.append(f"{at}: must be a permutation of {list(SOURCE_LEVELS)}")
            continue
        if not isinstance(std, Mapping):
            errors.append(f"{at}: must be an object")
            continue
        if not isinstance(std.get("value"), str) or not std.get("value"):
            errors.append(f"{at}.value: required string")
        if not isinstance(std.get("text"), str) or not std.get("text"):
            errors.append(f"{at}.text: required string (the clarification wording)")
        _check_evidence(f"{at}.evidence", std.get("evidence"), errors)
        patterns = std.get("value_patterns")
        if patterns is not None:
            if not isinstance(patterns, Mapping):
                errors.append(f"{at}.value_patterns: must map value -> regex list")
            else:
                for value, plist in patterns.items():
                    _check_regex_list(f"{at}.value_patterns.{value}", plist, errors, allow_empty=False)
        for key in ("neutral_patterns",):
            if key in std:
                _check_regex_list(f"{at}.{key}", std[key], errors, allow_empty=True)
        beats = std.get("level_beats")
        if beats is not None:
            if not isinstance(beats, list):
                errors.append(f"{at}.level_beats: must be a list")
            else:
                for i, pair in enumerate(beats):
                    if (
                        not isinstance(pair, Mapping)
                        or pair.get("winner") not in SOURCE_LEVELS
                        or pair.get("loser") not in SOURCE_LEVELS
                    ):
                        errors.append(f"{at}.level_beats[{i}]: needs winner and loser source levels")


def _check_match(where: str, match: Any, errors: list[str]) -> None:
    if not isinstance(match, Mapping):
        errors.append(f"{where}: must be an object")
        return
    if not _check_keys(where, match, MATCH_KEYS, (), errors):
        return
    _check_regex_list(f"{where}.patterns", match["patterns"], errors, allow_empty=True)
    _check_str_list(f"{where}.systems", match["systems"], errors)
    _check_str_list(f"{where}.exempt_subgroups", match["exempt_subgroups"], errors)
    _check_regex_list(f"{where}.exempt_patterns", match["exempt_patterns"], errors, allow_empty=True)


def _check_rules(rules: Any, errors: list[str]) -> list[str]:
    ids: list[str] = []
    if not isinstance(rules, list) or not rules:
        errors.append("rules: must be a non-empty list")
        return ids
    for i, rule in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(rule, Mapping):
            errors.append(f"{where}: must be an object")
            continue
        rid = rule.get("rule_id")
        if isinstance(rid, str):
            where = f"rules[{i}] {rid}"
        if not _check_keys(where, rule, RULE_KEYS, RULE_OPTIONAL_KEYS, errors):
            continue
        if not isinstance(rid, str) or not _RULE_ID_RE.match(rid):
            errors.append(f"{where}.rule_id: must look like 'EX-TEMP-POWER'")
        else:
            ids.append(rid)
        if not isinstance(rule["topic"], str) or not _TOPIC_RE.match(rule["topic"]):
            errors.append(f"{where}.topic: must be snake_case")
        kind = rule["kind"]
        if kind not in RULE_KINDS:
            errors.append(f"{where}.kind: must be one of {list(RULE_KINDS)}")
        if rule["decision"] not in RULE_DECISIONS:
            errors.append(f"{where}.decision: must be one of {list(RULE_DECISIONS)}")
        _check_match(f"{where}.match", rule["match"], errors)
        unless = rule["unless"]
        if not isinstance(unless, list):
            errors.append(f"{where}.unless: must be a list")
        else:
            bad = [u for u in unless if u not in UNLESS_CONDITIONS]
            if bad:
                errors.append(f"{where}.unless: unknown conditions {bad}")
            if kind == "hard" and unless:
                errors.append(f"{where}: a hard rule may not carry unless conditions")
        if not isinstance(rule["default_text"], str) or not rule["default_text"].strip():
            errors.append(f"{where}.default_text: required string")
        _check_proposal_line(f"{where}.proposal_line", rule["proposal_line"], errors)
        _check_evidence(f"{where}.evidence", rule["evidence"], errors)
        for flag in ("template_covered", "enabled"):
            if flag in rule and not isinstance(rule[flag], bool):
                errors.append(f"{where}.{flag}: must be a bool")
        if "review" in rule and not (rule["review"] is None or isinstance(rule["review"], str)):
            errors.append(f"{where}.review: must be a string or null")
    return ids


def _check_line_template(where: str, tpl: Any, errors: list[str]) -> None:
    if not isinstance(tpl, Mapping):
        errors.append(f"{where}: must be an object")
        return
    if not _check_keys(where, tpl, LINE_TEMPLATE_KEYS, LINE_TEMPLATE_OPTIONAL_KEYS, errors):
        return
    if tpl["section"] not in BOQ_LEVEL0:
        errors.append(f"{where}.section: must be one of {list(BOQ_LEVEL0)}")
    group = tpl["group"]
    if group not in BOQ_GROUPS:
        errors.append(f"{where}.group: {group!r} is not a BOQ group")
    elif tpl["subgroup"] not in BOQ_SUBGROUPS.get(group, ()):
        errors.append(f"{where}.subgroup: {tpl['subgroup']!r} is not a subgroup of {group}")
    if tpl["unit"] not in UNITS:
        errors.append(f"{where}.unit: must be one of {list(UNITS)}")
    if tpl["quantity"] not in INCLUSION_QUANTITIES:
        errors.append(f"{where}.quantity: must be one of {list(INCLUSION_QUANTITIES)}")
    if not isinstance(tpl["description"], str) or not tpl["description"].strip():
        errors.append(f"{where}.description: required string")
    if "wastage_applies" in tpl and not isinstance(tpl["wastage_applies"], bool):
        errors.append(f"{where}.wastage_applies: must be a bool")


def _derived_inclusion_id(family: Any, n: int) -> str:
    stem = re.sub(r"[^A-Z0-9]+", "-", str(family or "").upper()).strip("-") or "RULE"
    return f"IN-{stem}-{n}"


def inclusion_rule_ids(inclusions: Any) -> list[str | None]:
    """The rule id of every inclusion, in order: its own `rule_id` when it is
    a string, else `IN-<TRIGGER-FAMILY>-<n>` (n counts that family's
    inclusions in document order and skips ids already taken). None for an
    entry that is not an object."""
    if not isinstance(inclusions, (list, tuple)):
        return []
    taken = {
        inc.get("rule_id")
        for inc in inclusions
        if isinstance(inc, Mapping) and isinstance(inc.get("rule_id"), str)
    }
    per_family: dict[str, int] = {}
    out: list[str | None] = []
    for inc in inclusions:
        if not isinstance(inc, Mapping):
            out.append(None)
            continue
        rid = inc.get("rule_id")
        if isinstance(rid, str):
            out.append(rid)
            continue
        family = str(inc.get("trigger_family") or "")
        while True:
            per_family[family] = per_family.get(family, 0) + 1
            derived = _derived_inclusion_id(family, per_family[family])
            if derived not in taken:
                break
        taken.add(derived)
        out.append(derived)
    return out


def _check_inclusions(inclusions: Any, errors: list[str]) -> list[str]:
    ids: list[str] = []
    if not isinstance(inclusions, list):
        errors.append("inclusions: must be a list")
        return ids
    resolved = inclusion_rule_ids(inclusions)
    for i, inc in enumerate(inclusions):
        where = f"inclusions[{i}]"
        if not isinstance(inc, Mapping):
            errors.append(f"{where}: must be an object")
            continue
        explicit = "rule_id" in inc
        rid = resolved[i]
        if isinstance(rid, str):
            where = f"inclusions[{i}] {rid}"
        if not _check_keys(where, inc, INCLUSION_KEYS, INCLUSION_OPTIONAL_KEYS, errors):
            continue
        if not isinstance(rid, str) or not _RULE_ID_RE.match(rid):
            what = "rule_id" if explicit else "trigger_family (derived rule id)"
            errors.append(f"{where}.{what}: must look like 'IN-THERMOSTAT-RELOC'")
        else:
            ids.append(rid)
        if not isinstance(inc["trigger_family"], str) or not _TOPIC_RE.match(inc["trigger_family"]):
            errors.append(f"{where}.trigger_family: must be snake_case")
        problem = regex_problem(inc["pattern"])
        if problem:
            errors.append(f"{where}.pattern {inc['pattern']!r}: {problem}")
        also = inc.get("also_pattern")
        if also is not None:
            problem = regex_problem(also)
            if problem:
                errors.append(f"{where}.also_pattern {also!r}: {problem}")
        if inc["dedupe_scope"] not in DEDUPE_SCOPES:
            errors.append(f"{where}.dedupe_scope: must be one of {list(DEDUPE_SCOPES)}")
        _check_line_template(f"{where}.line_template", inc["line_template"], errors)
        if "evidence" in inc:
            _check_evidence(f"{where}.evidence", inc["evidence"], errors)
        _check_proposal_line(f"{where}.clarification", inc.get("clarification"), errors)
    return ids


def _check_allowances(allowances: Any, errors: list[str]) -> None:
    where = "allowances"
    if not isinstance(allowances, Mapping):
        errors.append(f"{where}: must be an object")
        return
    if not _check_keys(
        where,
        allowances,
        ("standard", "extra_requires_document_amount"),
        ("evidence", "standard_amount_usd", "document_amount_patterns", "max_extra_allowances"),
        errors,
    ):
        return
    std = allowances["standard"]
    if not isinstance(std, list) or not std or not all(isinstance(s, str) and s for s in std):
        errors.append(f"{where}.standard: must be a non-empty list of names")
    elif len(set(std)) != len(std):
        errors.append(f"{where}.standard: duplicate names")
    if not isinstance(allowances["extra_requires_document_amount"], bool):
        errors.append(f"{where}.extra_requires_document_amount: must be a bool")
    amount = allowances.get("standard_amount_usd", 0)
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
        errors.append(f"{where}.standard_amount_usd: must be a number >= 0")
    if "document_amount_patterns" in allowances:
        _check_regex_list(
            f"{where}.document_amount_patterns",
            allowances["document_amount_patterns"],
            errors,
            allow_empty=False,
        )
    cap = allowances.get("max_extra_allowances")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0):
        errors.append(f"{where}.max_extra_allowances: must be an integer >= 0")
    if "evidence" in allowances:
        _check_evidence(f"{where}.evidence", allowances["evidence"], errors)


def validation_errors(doc: Any) -> list[str]:
    """Every problem with `doc` (empty when valid)."""
    errors: list[str] = []
    if not isinstance(doc, Mapping):
        return ["policy must be a JSON object"]
    _check_keys("policy", doc, TOP_KEYS, OPTIONAL_TOP_KEYS, errors)
    try:
        size = len(canonical_json(doc).encode("ascii"))
    except (TypeError, ValueError) as exc:
        return errors + [f"policy is not JSON-serializable: {exc}"]
    if size > MAX_DOC_BYTES:
        errors.append(f"policy is {size} bytes; the limit is {MAX_DOC_BYTES}")
    for path, text in _walk_strings(doc, "policy"):
        _check_text(path, text, errors)
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("version: must be an integer >= 1")
    schema_version = doc.get("schema_version", POLICY_SCHEMA_VERSION)
    if schema_version != POLICY_SCHEMA_VERSION:
        errors.append(f"schema_version: must be {POLICY_SCHEMA_VERSION}")
    if "bid_conditions" in doc:
        _check_bid_conditions(doc["bid_conditions"], errors)
    if "standards" in doc:
        _check_standards(doc["standards"], errors)
    ids = _check_rules(doc.get("rules"), errors) if "rules" in doc else []
    ids += _check_inclusions(doc.get("inclusions"), errors) if "inclusions" in doc else []
    dupes = sorted({rid for rid in ids if ids.count(rid) > 1})
    if dupes:
        errors.append(f"duplicate rule ids {dupes}")
    if "allowances" in doc:
        _check_allowances(doc["allowances"], errors)
    known = set(ids)
    for i, inc in enumerate(doc.get("inclusions") or []):
        if isinstance(inc, Mapping):
            companion = inc.get("companion_of")
            if companion is not None and companion not in known:
                errors.append(f"inclusions[{i}].companion_of: unknown rule id {companion!r}")
    return errors


def validate(doc: Any) -> PolicyInfo:
    """Validate a policy document; raises PolicyInvalid with every error."""
    errors = validation_errors(doc)
    if errors:
        raise PolicyInvalid(errors)
    rule_ids = tuple(r["rule_id"] for r in doc["rules"]) + tuple(
        str(rid) for rid in inclusion_rule_ids(doc["inclusions"])
    )
    return PolicyInfo(
        version=int(doc["version"]),
        sha256=policy_sha(doc),
        rule_ids=rule_ids,
        size_bytes=len(canonical_json(doc).encode("ascii")),
    )


# Matching helpers (pure; used by enforce, the tools and the eval) --------------------


def rule_enabled(rule: Mapping[str, Any]) -> bool:
    return bool(rule.get("enabled", True))


def match_rule(
    rule: Mapping[str, Any],
    text: str,
    *,
    subgroup: str | None = None,
    group: str | None = None,
    system: str | None = None,
) -> str | None:
    """The matched snippet when `rule` fires on `text`, else None.

    A row whose subgroup or group heading is listed in `exempt_subgroups`, or
    whose text matches an `exempt_patterns` regex, never fires. `system` fires
    when it is listed in `match.systems` (case-insensitive)."""
    if not rule_enabled(rule):
        return None
    match = rule.get("match") or {}
    exempt = {s.strip().upper() for s in match.get("exempt_subgroups") or ()}
    if any(h and h.strip().upper() in exempt for h in (subgroup, group)):
        return None
    text = text or ""
    for pattern in match.get("exempt_patterns") or ():
        if _compiled(pattern).search(text):
            return None
    for pattern in match.get("patterns") or ():
        found = _compiled(pattern).search(text)
        if found:
            return found.group(0)
    if system and system.strip().lower() in {s.strip().lower() for s in match.get("systems") or ()}:
        return system
    return None


def rules_for(
    doc: Mapping[str, Any],
    *,
    topic: str | None = None,
    item_description: str | None = None,
    subgroup: str | None = None,
    group: str | None = None,
) -> list[dict[str, Any]]:
    """Enabled rules filtered by topic (substring, case-insensitive) and by an
    item description they fire on. No filter returns every enabled rule."""
    out: list[dict[str, Any]] = []
    needle = (topic or "").strip().lower().replace(" ", "_")
    for rule in doc.get("rules") or ():
        if not rule_enabled(rule):
            continue
        if needle and needle not in rule["topic"] and needle not in rule["rule_id"].lower():
            continue
        if (
            item_description
            and match_rule(rule, item_description, subgroup=subgroup, group=group) is None
        ):
            continue
        out.append(dict(rule))
    return out


def rule_by_id(doc: Mapping[str, Any], rule_id: str) -> dict[str, Any] | None:
    """A rule or inclusion by id. An inclusion found by its derived id comes
    back with that `rule_id` filled in."""
    for rule in doc.get("rules") or ():
        if isinstance(rule, Mapping) and rule.get("rule_id") == rule_id:
            return dict(rule)
    inclusions = list(doc.get("inclusions") or ())
    for inc, rid in zip(inclusions, inclusion_rule_ids(inclusions), strict=True):
        if rid == rule_id and isinstance(inc, Mapping):
            return {**dict(inc), "rule_id": rid}
    return None


# Active policy (in memory in wave 1; wave 2 registers a table-backed loader) ----------


@dataclass(frozen=True)
class ActivePolicy:
    doc: Mapping[str, Any]
    sha256: str
    version: int
    source: str  # seed | memory | table
    policy_id: str | None = None


PolicyLoader = Callable[[], "ActivePolicy | Mapping[str, Any] | None"]

_LOCK = threading.Lock()
_ACTIVE: ActivePolicy | None = None
_LOADER: PolicyLoader | None = None
_SEED_CACHE: ActivePolicy | None = None


def _seed_active() -> ActivePolicy:
    global _SEED_CACHE
    if _SEED_CACHE is None:
        from app.services.scope_policy_seed import SEED_POLICY

        info = validate(SEED_POLICY)
        _SEED_CACHE = ActivePolicy(
            doc=SEED_POLICY, sha256=info.sha256, version=info.version, source="seed"
        )
    return _SEED_CACHE


def seed_policy() -> ActivePolicy:
    """The validated code seed."""
    return _seed_active()


def set_active_policy(
    doc: Mapping[str, Any], *, source: str = "memory", policy_id: str | None = None
) -> ActivePolicy:
    """Validate and pin `doc` as the active policy of this process."""
    global _ACTIVE
    info = validate(doc)
    active = ActivePolicy(
        doc=doc, sha256=info.sha256, version=info.version, source=source, policy_id=policy_id
    )
    with _LOCK:
        _ACTIVE = active
    return active


def set_active_policy_loader(loader: PolicyLoader | None) -> None:
    """Wave 2 registers a loader backed by `ai_scope_policies`; None removes it."""
    global _LOADER
    with _LOCK:
        _LOADER = loader


def reset_active_policy() -> None:
    """Drop the pinned policy and the loader (tests)."""
    global _ACTIVE, _LOADER
    with _LOCK:
        _ACTIVE = None
        _LOADER = None


def active_policy() -> ActivePolicy:
    """The pinned policy, else the loader's, else the code seed. A loader that
    returns None or an invalid document falls back to the seed."""
    with _LOCK:
        pinned, loader = _ACTIVE, _LOADER
    if pinned is not None:
        return pinned
    if loader is not None:
        loaded = loader()
        if isinstance(loaded, ActivePolicy):
            return loaded
        if isinstance(loaded, Mapping):
            errors = validation_errors(loaded)
            if not errors:
                info = validate(loaded)
                return ActivePolicy(
                    doc=loaded, sha256=info.sha256, version=info.version, source="table"
                )
            logger.warning("active scope policy is invalid, using the code seed: %s", errors[:3])
    return _seed_active()
