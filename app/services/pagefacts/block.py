"""The per-page facts block (WS1 integration 6.4): the text a sub request
prints after each page image, built from that page's facts, and the token
estimate `extract` stores as `PageFacts.facts_block_tokens`.

Token estimates use the plans reader's `CHARS_PER_TOKEN = 3` so the pack
planner and the ledger caps agree with the rest of the pipeline.

Pure functions over the facts dicts; no model, no I/O.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from app.services.pagefacts.contracts import DocFacts, PageFacts

CHARS_PER_TOKEN = 3
DEFAULT_BLOCK_TOKENS = 2500
MAX_NOTE_ITEMS = 40
MAX_CIRCUIT_GROUPS = 60


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    budget = max(0, max_tokens) * CHARS_PER_TOKEN
    if len(text) <= budget:
        return text
    cut = text[: max(0, budget - 40)].rsplit("\n", 1)[0]
    return cut + "\n(facts block truncated)"


def _scale_text(facts: PageFacts) -> str:
    views = [v for v in facts.views if v.ft_per_in and not v.example]
    if not views:
        return "no scaled plan view found in the text layer"
    parts = []
    for v in views:
        label = v.title or v.view_id
        parts.append(f"{label}: {v.scale_text} ({v.ft_per_in:g} ft per in)")
    text = "; ".join(parts)
    if facts.scale_conflict:
        text += " [scale conflict: do not measure across views]"
    return text


def _notes_text(facts: PageFacts) -> list[str]:
    out: list[str] = []
    shown = 0
    for block in facts.notes:
        label = block.get("label") or block.get("kind", "notes").upper()
        items = block.get("items") or []
        if not items:
            continue
        out.append(f"{label}")
        for item in items:
            if shown >= MAX_NOTE_ITEMS:
                out.append("  (more notes not shown)")
                return out
            out.append(f"  {item['number']}. {item['text']}")
            shown += 1
    return out


def _tags_text(facts: PageFacts) -> list[str]:
    tags = facts.tags or {}
    out: list[str] = []
    circuits = tags.get("circuit") or []
    if circuits:
        groups: Counter[str] = Counter()
        for t in circuits:
            panel = t.get("panel") or "?"
            ckts = t.get("circuits") or []
            key = f"{panel}-{','.join(str(c) for c in ckts)}" if ckts else f"{panel} (no number)"
            groups[key] += 1
        parts = [f"{k} x{n}" if n > 1 else k for k, n in sorted(groups.items())]
        out.append(
            f"CIRCUIT TAGS IN THE PLAN AREA ({len(circuits)} text occurrences; a floor, not a device count): "
            + "; ".join(parts[:MAX_CIRCUIT_GROUPS])
            + (" ..." if len(parts) > MAX_CIRCUIT_GROUPS else "")
        )
    status = tags.get("status") or {}
    if status:
        out.append(
            "STATUS TAGS IN THE PLAN AREA: "
            + "; ".join(f"({k}) x{len(v)}" for k, v in sorted(status.items()) if v)
        )
    marks = tags.get("type_marks") or {}
    if marks:
        out.append(
            "LUMINAIRE TYPE MARKS IN THE PLAN AREA (floors): "
            + "; ".join(f"{k} x{len(v)}" for k, v in sorted(marks.items()) if v)
        )
    labels = tags.get("panel_labels") or []
    if labels:
        out.append("PANEL LABELS ON THIS SHEET: " + ", ".join(sorted({p["panel"] for p in labels})))
    if tags.get("default_panel"):
        out.append(
            f"DEFAULT PANEL NOTE: circuits without a panel belong to {tags['default_panel']}"
        )
    markers = facts.keynote_markers or []
    if markers:
        counts = Counter(m["number"] for m in markers)
        out.append(
            "KEYNOTE MARKER CANDIDATES (text hints, confidence 0.6): "
            + ", ".join(f"#{k} x{n}" for k, n in sorted(counts.items()))
        )
    return out


def _doc_schedules(doc: DocFacts | dict[str, Any] | None) -> list[dict[str, Any]]:
    if doc is None:
        return []
    if isinstance(doc, dict):
        return list(doc.get("schedules") or [])
    return list(doc.schedules)


def _schedules_text(facts: PageFacts, doc: DocFacts | dict[str, Any] | None) -> list[str]:
    if doc is None or not facts.schedule_ids:
        return []
    out: list[str] = []
    for sched in _doc_schedules(doc):
        if sched.get("schedule_id") not in facts.schedule_ids:
            continue
        kind = sched.get("kind")
        title = sched.get("title") or kind
        if kind == "panel" and sched.get("panel"):
            p = sched["panel"]
            circuits = p.get("circuits") or []
            new = [c for c in circuits if c.get("status") == "new"]
            unknown = [c for c in circuits if c.get("status") == "unknown"]
            out.append(
                f"PANEL SCHEDULE {p.get('name')} ({'existing' if p.get('existing') else 'new'}"
                f"{', ' + p['volts'] + 'V' if p.get('volts') else ''}): {len(circuits)} circuit rows parsed"
                f" (confidence {sched.get('parse_confidence', 0):.2f}); new breakers: "
                + (
                    ", ".join(f"{c['ckt']} ({c.get('status_basis') or 'new'})" for c in new)
                    if new
                    else "none marked"
                )
                + (f"; {len(unknown)} rows with unknown status" if unknown else "")
            )
        elif kind == "luminaire" and sched.get("luminaires"):
            parts = []
            for lum in sched["luminaires"]:
                bits = [lum.get("description") or ""]
                if lum.get("volts"):
                    bits.append(f"{lum['volts']}V")
                if lum.get("control"):
                    bits.append(f"control {lum['control']}")
                if lum.get("linear_hint"):
                    bits.append("LINEAR")
                parts.append(f"{lum['tag']} {' '.join(b for b in bits if b)}")
            out.append(f"LUMINAIRE SCHEDULE ({title}): " + " | ".join(parts))
        else:
            out.append(f"SCHEDULE {title}: {len(sched.get('rows_raw') or [])} rows")
    return out


def page_block(
    facts: PageFacts,
    doc: DocFacts | dict[str, Any] | None = None,
    *,
    max_tokens: int = DEFAULT_BLOCK_TOKENS,
) -> str:
    """The facts block for one page: sheet identity, roles and scale, then
    notes (exact text), schedule summaries, tags and keynote markers."""
    header = f"FACTS FOR PAGE {facts.page}"
    if facts.sheet_number:
        header += (
            f" (sheet {facts.sheet_number}{': ' + facts.sheet_title if facts.sheet_title else ''})"
        )
    lines = [
        header,
        f"roles: {', '.join(facts.roles) or 'none'}; text layer: {facts.text_layer_quality}"
        f"; sheet {facts.w_in:g} x {facts.h_in:g} in; scale: {_scale_text(facts)}",
    ]
    lines.extend(_notes_text(facts))
    lines.extend(_schedules_text(facts, doc))
    lines.extend(_tags_text(facts))
    if facts.text_layer_quality != "full":
        lines.append(
            "Text counts are unavailable on this page (no usable text layer); none found means unknown, never 0."
        )
    return truncate_to_tokens("\n".join(lines), max_tokens)


def page_block_tokens(
    facts: PageFacts,
    doc: DocFacts | dict[str, Any] | None = None,
    *,
    max_tokens: int = DEFAULT_BLOCK_TOKENS,
) -> int:
    return estimate_tokens(page_block(facts, doc, max_tokens=max_tokens))
