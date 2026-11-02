"""Recipient and URL binding for proposals (docs/ASSISTANT.md, Prompt
injection and taint; docs/ASSISTANT_PHASE1_PLAN.md 3.4).

When a conversation has read text written by other people or imported from
email (it is tainted), a write's free-text and URL arguments must come from
the user, not from that text. `bind_args` replaces every bound value that
also appears in the taint corpus (and was not typed by the user) with None
and reports it; each tool's `build` decides what None means. `taint_level`
grades the card's warning; it never changes what is stripped.

Pure functions: the corpus and the user texts come in, nothing here reads
the database or the registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

MIN_BOUND_CHARS = 12

# BDR-owned names and labels: reading only these keeps the warning low.
# Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md 3.10.4): the user's own staged file
# names and their own splitter job's first file name, under tool-specific
# field names. The generic "filename" stays high risk: project file names
# are written by the splitter model from third-party PDF text, and email
# attachment names are chosen by the sender.
LOW_RISK_TAINT_FIELDS = frozenset({"name", "label", "staged_filename", "job_first_filename"})

# The field of a corpus entry whose source field is unknown.
ANY_FIELD = "*"

REASON_TAINTED = "tainted_source"
REASON_URL = "url_not_typed"
REASON_RECIPIENT = "recipient_not_bound"

LEVELS = ("none", "low", "high")

_WS = re.compile(r"\s+")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HEX = frozenset("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def norm(text: str) -> str:
    """Casefold, collapse whitespace, strip."""
    return _WS.sub(" ", str(text or "")).casefold().strip()


def level_max(a: str, b: str) -> str:
    """The higher of two taint levels (none < low < high)."""
    ra = LEVELS.index(a) if a in LEVELS else len(LEVELS) - 1
    rb = LEVELS.index(b) if b in LEVELS else len(LEVELS) - 1
    return LEVELS[max(ra, rb)]


# ── Corpus ───────────────────────────────────────────────────────────────────


def unescape_partial(text: str) -> str:
    """Undo the JSON escapes of a truncated result's `partial` string
    (\\n, \\t, \\", \\\\, \\uXXXX and the other one-letter escapes). Best
    effort: a broken escape at the cut is dropped."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break  # a lone backslash at the cut
        nxt = text[i + 1]
        if nxt == "u":
            digits = text[i + 2:i + 6]
            if len(digits) < 4 or any(c not in _HEX for c in digits):
                if i + 6 > n:
                    break  # cut inside the escape
                i += 2
                continue
            out.append(chr(int(digits, 16)))
            i += 6
            continue
        out.append(_SIMPLE_ESCAPES.get(nxt, nxt))
        i += 2
    joined = "".join(out)
    try:
        # Rejoin surrogate pairs (\\ud83d\\ude00) into one character.
        return joined.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return joined


def _strings(value: Any, fields: tuple[str, ...] | None) -> list[tuple[str, str]]:
    """(field, text) for strings under `fields` at any depth; with `fields`
    None every string value, with field ANY_FIELD."""
    out: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, inner in node.items():
                if isinstance(inner, str):
                    if fields is None:
                        if inner.strip():
                            out.append((ANY_FIELD, inner))
                    elif key in fields and inner.strip():
                        out.append((str(key), inner))
                    continue
                walk(inner)
        elif isinstance(node, list):
            for inner in node:
                if isinstance(inner, str) and fields is None and inner.strip():
                    out.append((ANY_FIELD, inner))
                else:
                    walk(inner)

    walk(value)
    return out


def corpus_from_results(
    results: Iterable[Mapping[str, Any]],
    taint_fields_for: Callable[[str], tuple[str, ...] | None],
) -> tuple[tuple[str, str], ...]:
    """(field, text) entries from stored tainted results (rows with
    tool_name, payload, taint_corpus). A row with `taint_corpus` gives its
    entries exactly. A phase 0 row gives the payload's strings under the
    producing tool's taint fields (`taint_fields_for` returns None for an
    unknown tool, which contributes every string with field "*"), plus, for
    a truncated envelope, the unescaped `partial` string as one "*" entry."""
    out: list[tuple[str, str]] = []
    for row in results or ():
        if not isinstance(row, Mapping):
            continue
        stored = row.get("taint_corpus")
        if isinstance(stored, list):
            for entry in stored:
                if isinstance(entry, Mapping) and isinstance(entry.get("t"), str) and entry["t"].strip():
                    out.append((str(entry.get("f") or ANY_FIELD), entry["t"]))
            continue
        payload = row.get("payload")
        fields = taint_fields_for(str(row.get("tool_name") or ""))
        out.extend(_strings(payload, tuple(fields) if fields is not None else None))
        if isinstance(payload, Mapping) and payload.get("truncated") is True and isinstance(payload.get("partial"), str):
            out.append((ANY_FIELD, unescape_partial(payload["partial"])))
    return tuple(out)


def taint_level(tainted: bool, corpus: Iterable[tuple[str, str]], stripped: Iterable[dict]) -> str:
    """"none" when not tainted; "high" when a value was stripped, when some
    corpus entry comes from a field outside LOW_RISK_TAINT_FIELDS, or when
    the corpus is empty (the reason is unknown, so assume the worst);
    otherwise "low" (only project names or labels were read)."""
    if not tainted:
        return "none"
    if any(True for _ in stripped or ()):
        return "high"
    entries = list(corpus or ())
    if not entries:
        return "high"
    if any(field not in LOW_RISK_TAINT_FIELDS for field, _text in entries):
        return "high"
    return "low"


# ── Binding ──────────────────────────────────────────────────────────────────


def _from_corpus(value: str, corpus_norm: list[str]) -> bool:
    v = norm(value)
    if len(v) < MIN_BOUND_CHARS:
        return False
    for c in corpus_norm:
        if v in c or (len(c) >= MIN_BOUND_CHARS and c in v):
            return True
    return False


def _typed_by_user(value: str, user_norm: list[str]) -> bool:
    v = norm(value)
    return any(v in u for u in user_norm)


def url_from_user(value: str, user_texts: Iterable[str]) -> bool:
    """True when the URL, with its scheme and a trailing slash removed,
    casefolded, appears in some user text (same normalization)."""
    bare = norm(_SCHEME.sub("", str(value or "").strip()).rstrip("/"))
    if not bare:
        return False
    return any(bare in norm(u) for u in user_texts or ())


def _walk_path(node: Any, parts: list[str], visit: Callable[[Any, str], Any]) -> Any:
    """Apply `visit(value, key)` at the end of `parts`; lists along the way
    are walked element by element. Returns the (possibly copied) node."""
    if isinstance(node, list):
        return [_walk_path(item, parts, visit) for item in node]
    if not isinstance(node, Mapping) or not parts:
        return node
    head, rest = parts[0], parts[1:]
    if head not in node:
        return node
    out = dict(node)
    if rest:
        out[head] = _walk_path(node[head], rest, visit)
    elif isinstance(node[head], list):
        out[head] = [visit(item, head) for item in node[head]]
    else:
        out[head] = visit(node[head], head)
    return out


def bind_args(
    args: Mapping[str, Any],
    *,
    text_fields: tuple[str, ...],
    url_fields: tuple[str, ...],
    corpus: Iterable[str],
    user_texts: Iterable[str],
    tainted: bool,
    keep: Mapping[str, Iterable[str]] | None = None,
) -> tuple[dict, list[dict]]:
    """(bound args, stripped). Nothing is stripped when `tainted` is False.

    `keep` maps a text path to values already stored in the target record:
    a value at that path whose norm equals one of them is kept (BDR data
    resent unchanged, not foreign text moving into a new place).

    Text field (dotted path, list elements included): a string `v` with
    len(norm(v)) >= MIN_BOUND_CHARS is stripped when some corpus entry `c`
    has norm(v) in norm(c) or (len(norm(c)) >= MIN_BOUND_CHARS and norm(c)
    in norm(v)), unless norm(v) is a substring of a user text. URL field: a
    value is kept only when `url_from_user`. Stripped values become None and
    {"field": <dotted path>, "reason": ...} is reported once per field."""
    out = dict(args)
    stripped: list[dict] = []
    if not tainted:
        return out, stripped
    corpus_norm = [c for c in (norm(t) for t in corpus or ()) if c]
    users = [u for u in (user_texts or ()) if isinstance(u, str)]
    user_norm = [norm(u) for u in users]

    def note(path: str, reason: str) -> None:
        if not any(s["field"] == path for s in stripped):
            stripped.append({"field": path, "reason": reason})

    kept = {str(p): {norm(t) for t in texts or () if isinstance(t, str) and norm(t)}
            for p, texts in (keep or {}).items()}

    for path in text_fields:
        def visit_text(value: Any, _key: str, path: str = path) -> Any:
            if isinstance(value, str) and norm(value) in kept.get(path, ()):
                return value
            if isinstance(value, str) and _from_corpus(value, corpus_norm) and not _typed_by_user(value, user_norm):
                note(path, REASON_TAINTED)
                return None
            return value

        out = _walk_path(out, path.split("."), visit_text)

    for path in url_fields:
        def visit_url(value: Any, _key: str, path: str = path) -> Any:
            if isinstance(value, str) and value.strip() and not url_from_user(value, users):
                note(path, REASON_URL)
                return None
            return value

        out = _walk_path(out, path.split("."), visit_url)
    return out, stripped


def bind_recipients(
    candidates: Iterable[str],
    *,
    allowed_emails: Iterable[str],
    user_texts: Iterable[str],
) -> tuple[list[str], list[dict]]:
    """Scaffold for phase 5 (no phase 1 caller): keep a candidate address
    only when it is in `allowed_emails` or the user typed it. Returns
    (kept, stripped with reason recipient_not_bound)."""
    allowed = {str(e).strip().casefold() for e in allowed_emails or () if str(e).strip()}
    users = [str(u).casefold() for u in user_texts or ()]
    kept: list[str] = []
    stripped: list[dict] = []
    for raw in candidates or ():
        address = str(raw or "").strip()
        if not address:
            continue
        folded = address.casefold()
        if folded in allowed or any(folded in u for u in users):
            kept.append(address)
        else:
            stripped.append({"field": address, "reason": REASON_RECIPIENT})
    return kept, stripped
