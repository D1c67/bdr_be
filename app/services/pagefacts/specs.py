"""Specification documents (WS1 step 7): the section map, the table of
contents, the division notes ("DIVISION 26 - ELECTRICAL - REFER TO
ELECTRICAL DRAWINGS", "DIVISION 28 ... NOT USED") captured as facts, and
the page selection that fixes E24 (spec pages sent to models <= 40% of the
spec pages).

Section map: per page a section-page footer ("01 31 00 -1", "01 33 00-1",
"01 60 00 - 1") in the last six lines wins, then a "SECTION nn nn nn" header
anywhere, else the previous page's section carries forward. Consecutive
pages of one section form one entry in `sections` (numbered sections only:
`len(sections)` is the mapped-section count); page runs with no section
(the project-manual cover) are listed separately in `unmapped` as
{page_start, page_end} markers and never count as sections. On 7154
SPECS.pdf that is 33 mapped sections plus one unmapped cover run (the
spec's PF7 "34" counted the cover; the measured value is 33).

Selection policy `spec-select/1`: tier A pages are sent whole (Divisions
25 to 28 and the Division 01, 02, 07, 08, 33, 34 sections listed in
`TIER_A_RULES`; Divisions 11 and 14 only with two electrical keyword hits);
tier B sections contribute keyword snippets (1,500 tokens per section,
8,000 per document); tier C sections appear as titles only. Documents of six
pages or fewer are sent whole, documents with under half their pages mapped
are sent whole and flagged `spec_unmapped`, scanned pages are sent whole.

Pure functions over `PageWords`; no model, no I/O.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.pagefacts.block import estimate_tokens
from app.services.pagefacts.contracts import DocFacts, PageWords
from app.services.pagefacts.notes import baseline_rows, plain_text, row_text

SPEC_SELECT_POLICY = "spec-select/1"
FILE_TOKENS_PER_PAGE = 1_100  # plans_reader_pack.FILE_TOKENS_PER_PAGE
SCANNED_TOKENS_PER_LETTER_PAGE = 8_400
LETTER_PAGE_SQ_IN = 93.5
SCANNED_TOKENS_CAP = 33_000
TIER_B_SECTION_TOKENS = 1_500
TIER_B_DOCUMENT_TOKENS = 8_000
SMALL_DOC_PAGES = 6
UNMAPPED_SHARE = 0.5
E24_MAX_SHARE = 0.40
FOOTER_LINES = 6
DASH = r"(?:-|\u2013|\u2014)"

SECTION_RE = r"(\d{2})\s?(\d{2})\s?(\d{2})(?:\.(\d{1,2}))?"
FOOT_RE = re.compile(rf"(?<!\d){SECTION_RE}\s*{DASH}\s*(\d{{1,3}})\b")
HEADER_RE = re.compile(rf"^\s*(?:SECTION|DOCUMENT)\s+{SECTION_RE}\b", re.I)
TOC_TITLE_RE = re.compile(r"TABLE\s+OF\s+CONTENTS|PROJECT\s+MANUAL", re.I)
TOC_NUMBER_RE = re.compile(rf"^\s*{SECTION_RE}\s*$")
TOC_LINE_RE = re.compile(rf"^\s*{SECTION_RE}\s+(.+?)(?:\s+\.\.\s*|\s+)?(\d{{1,3}})?\s*$")
DIVISION_LINE_RE = re.compile(
    rf"^\s*DIVISIONS?\s+(?P<div>\d{{2}})(?:\s*{DASH}\s*(?P<to>\d{{2}}))?\s*(?:{DASH})?\s*(?P<rest>.*)$",
    re.I,
)
NOT_USED_RE = re.compile(r"NOT\s+USED|N/A|NOT\s+APPLICABLE", re.I)
REFER_RE = re.compile(r"REFER\s+TO\s+(.+?)\s*$", re.I)
ELECTRICAL_KEYWORDS_RE = re.compile(
    r"ELECTRICAL|DIVISION\s*26|DIV\.?\s*26|\bE\.C\.|\bPOWER\b|WIRING|DISCONNECT|CONDUIT|BREAKER"
    r"|PREVAILING|NRS\s*338|DAVIS[- ]BACON|PUBLIC\s+WORKS|BY\s+OTHERS|OWNER[- ]FURNISHED"
    r"|NOT\s+IN\s+CONTRACT|N\.I\.C\.",
    re.I,
)
PARAGRAPH_START_RE = re.compile(r"^\s*(?:\d+\.\d+|\d+\.|[A-Z]\.|[a-z]\.|\(\d+\)|\([a-z]\))\s")

# (division, section start, section end) inclusive on the "nn nn nn" string.
TIER_A_RULES: tuple[tuple[str, str, str], ...] = (
    ("25", "25 00 00", "25 99 99"),
    ("26", "26 00 00", "26 99 99"),
    ("27", "27 00 00", "27 99 99"),
    ("28", "28 00 00", "28 99 99"),
    ("33", "33 71 00", "33 82 99"),
    ("34", "34 41 00", "34 41 99"),
    ("01", "01 10 00", "01 14 99"),
    ("01", "01 21 00", "01 21 99"),
    ("01", "01 22 00", "01 22 99"),
    ("01", "01 23 00", "01 23 99"),
    ("01", "01 35 00", "01 35 99"),
    ("01", "01 41 00", "01 41 99"),
    ("01", "01 50 00", "01 58 99"),
    ("01", "01 73 29", "01 73 29"),
    ("01", "01 91 00", "01 91 99"),
    ("02", "02 41 19", "02 41 19"),
    ("07", "07 84 00", "07 84 99"),
    ("08", "08 71 00", "08 71 00"),
    ("08", "08 42 29", "08 42 29"),
)
TIER_A_KEYWORD_DIVISIONS: frozenset[str] = frozenset({"11", "14"})
TIER_A_KEYWORD_MIN_HITS = 2


def _section_key(a: str, b: str, c: str) -> str:
    return f"{a} {b} {c}"


def page_section(pw: PageWords) -> tuple[str | None, str]:
    """(section, source) for one page: footer, header or none."""
    lines = pw.lines
    for ln in reversed(lines[-FOOTER_LINES:]):
        m = FOOT_RE.search(plain_text(ln.t))
        if m:
            return _section_key(m.group(1), m.group(2), m.group(3)), "footer"
    for ln in lines:
        m = HEADER_RE.match(plain_text(ln.t))
        if m:
            return _section_key(m.group(1), m.group(2), m.group(3)), "header"
    return None, "none"


def _header_title(pw: PageWords, section: str) -> str | None:
    """The title after a SECTION header: the rest of that line, else the
    next one or two lines of the same block."""
    for i, ln in enumerate(pw.lines):
        text = plain_text(ln.t)
        m = HEADER_RE.match(text)
        if not m or _section_key(m.group(1), m.group(2), m.group(3)) != section:
            continue
        rest = text[m.end() :].strip(" -:\u2013\u2014")
        if rest:
            return plain_text(rest)
        parts: list[str] = []
        for nxt in pw.lines[i + 1 : i + 3]:
            t = plain_text(nxt.t)
            if not t or re.match(r"^PART\s+\d", t, re.I) or FOOT_RE.search(t):
                break
            if re.match(r"^[A-Z][a-z]+$", t) or t.upper() in ("SECTION", "TITLE", "PAGE"):
                break  # a table column header, not part of the title
            letters = [c for c in t if c.isalpha()]
            if (
                not letters
                or sum(c.isupper() for c in letters) / len(letters) < 0.8
                or len(t.split()) > 8
            ):
                break  # body text, not a title line
            parts.append(t)
        return plain_text(" ".join(parts)) or None
    return None


def parse_toc(pw: PageWords, page: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(toc rows, division notes) from one table-of-contents page. Section
    numbers and titles are separate lines on one baseline in most manuals
    (and dot leaders split a row into many), so lines are joined by baseline
    before matching."""
    toc: list[dict[str, Any]] = []
    divisions: list[dict[str, Any]] = []
    for row in baseline_rows(pw, max_gap_in=2.5):
        text = re.sub(r"(?:\s*\.){2,}", " .. ", row_text(row))
        dm = DIVISION_LINE_RE.match(text)
        if dm:
            rest = dm.group("rest") or ""
            status = "listed"
            note: str | None = None
            if NOT_USED_RE.search(rest):
                status = "not_used"
            else:
                rm = REFER_RE.search(rest)
                if rm:
                    status = (
                        "refer_to_drawings"
                        if re.search(r"DRAWING|PLAN|REPORT", rm.group(1), re.I)
                        else "refer"
                    )
                    note = plain_text(rm.group(1))
            title = re.split(rf"\s*{DASH}\s*", rest)[0].strip() if rest else ""
            divisions.append(
                {
                    "division": dm.group("div"),
                    "division_to": dm.group("to"),
                    "title": plain_text(title) or None,
                    "status": status,
                    "note": note,
                    "text": plain_text(row_text(row)),
                    "page": page,
                }
            )
            continue
        if TOC_NUMBER_RE.match(text):
            continue  # a bare number with no title on its baseline
        lm = TOC_LINE_RE.match(text)
        if not lm:
            continue
        title = re.sub(r"\s*\.\.\s*$", "", plain_text(lm.group(5)))
        toc.append(
            {
                "section": _section_key(lm.group(1), lm.group(2), lm.group(3)),
                "title": title,
                "page": int(lm.group(6)) if lm.group(6) else None,
            }
        )
    return toc, divisions


def section_map(pages: dict[int, PageWords]) -> dict[str, Any]:
    """Sections, TOC, divisions, mapped share and division presence."""
    by_page: dict[int, tuple[str | None, str]] = {}
    prev: str | None = None
    for p in sorted(pages):
        pw = pages[p]
        section, source = page_section(pw)
        if (
            section is None
            and prev is not None
            and pw.text_class not in ("empty", "skipped_budget")
        ):
            section, source = prev, "carry"
        by_page[p] = (section, source)
        prev = section if section is not None else prev
    entries: list[dict[str, Any]] = []
    for p in sorted(pages):
        section, source = by_page[p]
        if entries and entries[-1]["section"] == section:
            entries[-1]["page_end"] = p
            continue
        entries.append(
            {
                "section": section,
                "division": section[:2] if section else None,
                "title": None,
                "page_start": p,
                "page_end": p,
                "source": source,
                "tier": None,
            }
        )
    toc: list[dict[str, Any]] = []
    divisions: list[dict[str, Any]] = []
    toc_pages: list[int] = []
    for p in sorted(pages):
        pw = pages[p]
        head = " ".join(plain_text(ln.t) for ln in pw.lines[:4])
        if TOC_TITLE_RE.search(head) or by_page[p][0] == "00 01 10":
            rows, divs = parse_toc(pw, p)
            if rows or divs:
                toc_pages.append(p)
            toc.extend(rows)
            divisions.extend(divs)
    toc_titles = {row["section"]: row["title"] for row in toc}
    unmapped = [
        {"page_start": e["page_start"], "page_end": e["page_end"]}
        for e in entries
        if e["section"] is None
    ]
    entries = [e for e in entries if e["section"] is not None]
    for e in entries:
        for p in range(e["page_start"], e["page_end"] + 1):
            title = _header_title(pages[p], e["section"])
            if title:
                e["title"] = title
                break
        if not e["title"]:
            e["title"] = toc_titles.get(e["section"])
    body_sections = {e["section"] for e in entries if e["section"]}
    missing = [row["section"] for row in toc if row["section"] not in body_sections]
    mapped = sum(1 for p in by_page if by_page[p][0] is not None)
    scanned = [
        p for p in sorted(pages) if pages[p].text_class in ("scanned", "scanned_ocr", "vector_text")
    ]
    present = {d for d in (s[:2] for s in body_sections)}
    return {
        "sections": entries,
        "unmapped": unmapped,
        "toc": toc,
        "toc_pages": toc_pages,
        "divisions": divisions,
        "missing_in_body": missing,
        "mapped_page_share": round(mapped / len(pages), 4) if pages else 0.0,
        "has_div": {d: d in present for d in ("26", "27", "28")},
        "scanned_pages": scanned,
        "page_count": len(pages),
    }


def _in_rule(section: str) -> bool:
    for _div, lo, hi in TIER_A_RULES:
        if lo <= section <= hi:
            return True
    return False


def _keyword_hits(pages: dict[int, PageWords], start: int, end: int) -> int:
    n = 0
    for p in range(start, end + 1):
        pw = pages.get(p)
        if pw is None:
            continue
        for ln in pw.lines:
            n += len(ELECTRICAL_KEYWORDS_RE.findall(ln.t))
    return n


def _paragraphs(pw: PageWords) -> list[str]:
    """Blank-line or numbered-paragraph delimited text of one page."""
    out: list[list[str]] = []
    last_bottom: float | None = None
    last_h = 0.0
    for ln in sorted(pw.lines, key=lambda ln: (ln.cy, ln.b[0])):
        h = ln.h / ((pw.page_h_in or 11.0) * 72.0)
        text = plain_text(ln.t)
        new_par = (
            not out
            or (last_bottom is not None and ln.b[1] - last_bottom > 1.5 * max(last_h, h, 0.002))
            or bool(PARAGRAPH_START_RE.match(text))
        )
        if new_par:
            out.append([text])
        else:
            out[-1].append(text)
        last_bottom = ln.b[3]
        last_h = h
    return [" ".join(p) for p in out if any(p)]


def _page_tokens(pw: PageWords) -> int:
    if pw.text_class in ("scanned", "scanned_ocr", "vector_text"):
        area = (pw.page_w_in or 8.5) * (pw.page_h_in or 11.0)
        return min(
            SCANNED_TOKENS_CAP, round(SCANNED_TOKENS_PER_LETTER_PAGE * area / LETTER_PAGE_SQ_IN)
        )
    return FILE_TOKENS_PER_PAGE


def select_pages(spec: dict[str, Any], pages: dict[int, PageWords]) -> dict[str, Any]:
    """The `selection` block: pages sent whole, tier B snippets, token
    estimates before and after, policy and flags."""
    total_pages = sorted(pages)
    flags: list[str] = []
    est_before = sum(_page_tokens(pages[p]) for p in total_pages)
    whole = False
    if len(total_pages) <= SMALL_DOC_PAGES:
        whole, flags = True, flags + ["small_doc"]
    elif spec.get("mapped_page_share", 0.0) < UNMAPPED_SHARE:
        whole, flags = True, flags + ["spec_unmapped"]
    selected: set[int] = set()
    snippets: list[dict[str, Any]] = []
    if whole:
        selected = set(total_pages)
        for e in spec["sections"]:
            e["tier"] = "A"
    else:
        doc_budget = TIER_B_DOCUMENT_TOKENS
        for e in spec["sections"]:
            section = e["section"]
            start, end = e["page_start"], e["page_end"]
            tier_a = _in_rule(section) or (
                section[:2] in TIER_A_KEYWORD_DIVISIONS
                and _keyword_hits(pages, start, end) >= TIER_A_KEYWORD_MIN_HITS
            )
            if tier_a:
                e["tier"] = "A"
                selected.update(range(start, end + 1))
                continue
            section_budget = min(TIER_B_SECTION_TOKENS, doc_budget)
            found: list[dict[str, Any]] = []
            for p in range(start, end + 1):
                pw = pages.get(p)
                if pw is None:
                    continue
                for par in _paragraphs(pw):
                    kws = sorted({k.upper() for k in ELECTRICAL_KEYWORDS_RE.findall(par)})
                    if not kws:
                        continue
                    cost = estimate_tokens(par)
                    if cost > section_budget:
                        continue
                    section_budget -= cost
                    doc_budget -= cost
                    found.append({"section": section, "page": p, "text": par, "keywords": kws})
            e["tier"] = "B" if found else "C"
            snippets.extend(found)
        if spec.get("scanned_pages"):
            selected.update(spec["scanned_pages"])  # vision reads them whole
            flags.append("scanned_pages")
    pages_sent = sorted(selected)
    est_after = sum(_page_tokens(pages[p]) for p in pages_sent) + sum(
        estimate_tokens(s["text"]) for s in snippets
    )
    return {
        "pages": pages_sent,
        "tier_b_snippets": snippets,
        "est_tokens_before": est_before,
        "est_tokens_after": est_after,
        "policy": SPEC_SELECT_POLICY,
        "flags": flags,
        "pages_share": round(len(pages_sent) / len(total_pages), 4) if total_pages else 0.0,
    }


def build_spec_facts(pages: dict[int, PageWords]) -> dict[str, Any]:
    """`DocFacts.spec` for a text document."""
    spec = section_map(pages)
    spec["selection"] = select_pages(spec, pages)
    return spec


def pages_sent_share(spec_docs: list[dict[str, Any]]) -> float:
    """Spec pages sent to models over all spec pages (E24: <= 0.40), from
    the `spec` facts of every text document."""
    sent = sum(len((d.get("selection") or {}).get("pages") or []) for d in spec_docs)
    total = sum(int(d.get("page_count") or 0) for d in spec_docs)
    return round(sent / total, 4) if total else 0.0


def planned_spec_tokens(spec_docs: list[dict[str, Any]]) -> int:
    return sum(int((d.get("selection") or {}).get("est_tokens_after") or 0) for d in spec_docs)


ELECTRICAL_SPEC_SHEET_RE = re.compile(r"ELECTRICAL\s+SPECIFICATIONS?", re.I)


def no_div26_note(
    spec_docs: list[dict[str, Any]],
    drawing_docs: list[DocFacts],
    *,
    doc_refs: dict[str, str] | None = None,
) -> str | None:
    """When no spec document carries Division 26 and an electrical drawing
    page is titled ELECTRICAL SPECIFICATIONS, the ledger says where the
    specifications are (E24). `doc_refs` maps a drawing document's sha256
    to the label the ledger names it by (its quoted name: never a D number,
    since sub requests number documents per group)."""
    if not spec_docs or any((d.get("has_div") or {}).get("26") for d in spec_docs):
        return None
    for doc in drawing_docs:
        for page in doc.pages:
            if page.sheet_title and ELECTRICAL_SPEC_SHEET_RE.search(page.sheet_title):
                ref = (doc_refs or {}).get(doc.sha256) or "the drawings"
                number = page.sheet_number or f"page {page.page}"
                return (
                    f"Electrical specifications are on sheet {number} ({ref} page {page.page}); "
                    "the project manual has no Division 26."
                )
    return "The project manual has no Division 26; no drawing sheet titled ELECTRICAL SPECIFICATIONS was found."


def lookup(
    spec: dict[str, Any],
    pages: dict[int, PageWords] | None = None,
    *,
    section: str | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """`spec_lookup` semantics: entries by section prefix or keyword in the
    title or (when page words are given) in the page text."""
    out: list[dict[str, Any]] = []
    sec = (section or "").strip()
    kw = (keyword or "").strip().upper()
    for e in spec.get("sections") or []:
        if sec and not (e["section"] or "").startswith(sec):
            continue
        hit_text = None
        if kw:
            in_title = kw in (e.get("title") or "").upper()
            if not in_title and pages:
                for p in range(e["page_start"], e["page_end"] + 1):
                    pw = pages.get(p)
                    if pw is None:
                        continue
                    for ln in pw.lines:
                        if kw in ln.t.upper():
                            hit_text = plain_text(ln.t)
                            break
                    if hit_text:
                        break
            if not in_title and not hit_text:
                continue
        out.append({**e, "text": hit_text})
        if len(out) >= limit:
            break
    return out
