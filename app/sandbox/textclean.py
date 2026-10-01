"""The text contract: strip what should not be in extracted page text, and
count what was hidden there.

Used by the child on every page (and on every metadata string) before the
text is written; the parent re-runs the SAME rules over what it reads back
(`rfp_sanitize.sanitize_text`) and re-counts the hazards, so the two
implementations must agree exactly. The rules, in order:

1. Control characters (Unicode category Cc, which is U+0000-U+001F and
   U+007F-U+009F) are removed EXCEPT `\\n` (U+000A) and `\\t` (U+0009). U+0000
   is dropped like the rest. These are not counted: PDF text extraction emits
   `\\r`, form feeds and the like as ordinary noise.
2. U+FFFD (the replacement character) is removed and counted as
   `replacement_chars`.
3. Code points in category Cf (format: zero-width joiners, BOMs, soft
   hyphens, bidi controls) are removed and counted as `format_chars`.
4. Category Co (private use) is removed and counted as `private_use`.
5. Categories Cn (unassigned) and Cs (lone surrogates, which cannot survive
   a UTF-8 round trip anyway) are removed and counted as `unassigned`.

Everything else, including every kind of whitespace and every printable
character in every script, passes through unchanged. Nothing is normalized:
the agent slice may want to see exactly what the document said.

The counts use `protocol.TEXT_HAZARD_KEYS` so a page whose text carried
hidden instructions shows up in the manifest as suspicious even though the
stored text is clean.

Performance: page text is capped at `max_text_chars_per_page` (200k by
default) BEFORE it reaches here, and the common all-ASCII page takes the
str.translate fast path; only non-ASCII code points pay for a
`unicodedata.category` lookup.

This module imports only the standard library.
"""

from __future__ import annotations

import re
import unicodedata

from app.sandbox.protocol import TEXT_HAZARD_KEYS

# Category Cc minus \n and \t: dropped silently by str.translate before any
# per-character work. C1 controls (U+0080-U+009F) are non-ASCII but the table
# removes them just the same.
_CONTROL_TABLE: dict[int, None] = {
    c: None for c in list(range(0x00, 0x20)) + list(range(0x7F, 0xA0)) if c not in (0x09, 0x0A)
}
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_CATEGORY_TO_KEY: dict[str, str] = {
    "Cf": "format_chars",
    "Co": "private_use",
    "Cn": "unassigned",
    "Cs": "unassigned",
}
REPLACEMENT_CHAR = "�"


def empty_hazards() -> dict[str, int]:
    return {key: 0 for key in TEXT_HAZARD_KEYS}


def sanitize(text: str) -> tuple[str, dict[str, int]]:
    """Return `(clean_text, hazards)` per the module rules. `hazards` always
    carries every key in `protocol.TEXT_HAZARD_KEYS`."""
    counts = empty_hazards()
    text = text.translate(_CONTROL_TABLE)
    if text.isascii():
        return text, counts

    def _replace(match: re.Match[str]) -> str:
        ch = match.group()
        if ch == REPLACEMENT_CHAR:
            counts["replacement_chars"] += 1
            return ""
        category = unicodedata.category(ch)
        key = _CATEGORY_TO_KEY.get(category)
        if key is not None:
            counts[key] += 1
            return ""
        if category == "Cc":
            return ""
        return ch

    return _NON_ASCII.sub(_replace, text), counts


def clip(text: str, max_chars: int) -> str:
    """Sanitize then cap at `max_chars` code points (metadata strings)."""
    clean, _counts = sanitize(text)
    return clean[:max_chars]
