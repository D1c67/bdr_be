"""The WS5 scope engine (docs/AI_ESTIMATING_BUILD_SPEC.md 4.1, 5.4, WS5).

Two stages on a run, both code (no model unless the capped attribution
fallback is allowed):

1. `prepare(sb, run, outputs, *, allow_llm)` (derive group, step 1) reads the
   run's documents through the WS1 page facts (`pagefacts.api` when the
   facts job ran, else an on-the-fly extraction of the same PDFs), and builds
   the ScopeContext stored as `plans_reader_outputs` scope `policy`:
   {policy_id, policy_sha, bid_conditions, documents (roles), obligations,
   facts F*, standards, breaker_plan, inclusions, device_facts, digest}.
2. `enforce(main_output, ctx, policy, overrides)` (render step 2, inside
   `_finish_with_main`) rewrites the main output so it obeys the G3 policy:
   breakers per the panel plan, receptacle merges and the TR split,
   inclusions replace-or-merge (thermostats deduped across demolition and
   new-work sheets, connections canonicalized, control cable), exclusions
   (policy rules with exempt subgroups and patterns, verified facts through
   the party rule), extra allowances only with a document amount, standards
   (raceway, demolition disposition), clarifications and exclusions with
   citations, compound RFIs rewritten by removing decided clauses, excluded
   items reconciled with the markups and marked-plan legends. Every drop is
   recorded in `excluded_lines` (quantity kept, reversible). It is
   idempotent: a second call changes nothing (operations set a target state
   and the report carries its earlier entries by key). Rows added through
   chat or modify are exempt; overrides apply only their own decision delta.

Provenance of the lines the engine writes (the WS4 final gates read it;
measured on 7154 run 2492ba7a, where B41 and B42 were unsourced and B8
failed the recheck):

- A line split off another (the TR duplex line, DEV-TR) carries the parent's
  `source_refs`, `combine: "split"`, `split_of: <parent line id>` and
  `split_quantity`; the parent carries `split_to: [{line_id, quantity}]`.
  `plans_reader_lines.recheck` expects the parent at its sources minus its
  split_to quantities and the split line at its stored quantity, and the
  provenance gate counts the pair as one consumer (`scope_devices.mark_split`).
- A policy inclusion inserted from reader rows cites those rows (combine sum
  or single); one inserted from page facts (the thermostat keynote of a
  mechanical set) cites the keynote text lines `t:<doc8>:<page>:<line>`
  (`keynote_text_ref`, the tool layer's text ref) with `basis: policy` and
  no `combine`, because a text fact has no quantity for the recheck; a
  measured companion cites its M row; a breaker line the plan inserts cites
  the consolidator's policy M row. A line whose context carries no ref
  cites nothing and the unsourced_line gate routes it to a human.

The report lands in the main output under `scope_policy` (spec 5.4
ScopeReport). Hooks for others: `facts_for_run`, `classify_obligation`,
`rules_text`, `evaluate`, `standards_for`, `proposal_lines` (suggestions only;
nothing is written to proposals), `render_digest`.

Sync Supabase only (never inside `async def`). New columns and tables (MD:
`ai_scope_policies`, `plans_reader_scope_overrides`, the three run columns)
are read and written defensively: a missing table or column never fails a
run, and keys whose value is None are omitted from inserts.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services import bid_conditions as bidc
from app.services import scope_attribution as attr
from app.services import scope_breakers as breakers
from app.services import scope_devices as devices
from app.services import scope_policy
from app.services import scope_standards as standards
from app.services.estimating_contracts import ExcludedLine, Fact, make_row_id, parse_row_id
from app.services.scope_attribution import Obligation, Statement

logger = logging.getLogger(__name__)

SCOPE_CONTEXT_SCHEMA = "bdr.scope_context/1"
SCOPE_REPORT_SCHEMA = "bdr.scope_report/1"
ENGINE_VERSION = "ws5-engine-1"
POLICY_SCOPE = "policy"
CHARS_PER_TOKEN = 3

DRAWING_CATEGORIES: frozenset[str] = frozenset(
    {"electrical_drawings", "drawings", "mechanical_drawings", "plumbing_drawings", "architectural_drawings"}
)
TEXT_ROLE_BY_CATEGORY: dict[str, str] = {
    "rfp": "owner_exhibit",
    "specifications": "spec",
    "addenda": "addendum",
    "other": "spec",
}
OWNER_ROLE_FOR_BID: dict[str, str] = {"owner_exhibit": "owner_scope_exhibit", "spec": "specification"}
NOTE_ROLE_BY_KIND: dict[str, str] = {"keynotes": "keynote", "sheet_notes": "sheet_note", "general_notes": "general_note"}
REF_PREFIX: dict[str, str] = {"keynote": "KN", "sheet_note": "SN", "general_note": "GN", "schedule_note": "SCH"}
LINE_STATEMENT_ROLES: frozenset[str] = frozenset({"general_notes", "legend_symbols", "cover_index"})
MAX_TEXT_PAGES = 260
MAX_SENTENCE_CHARS = 600
_SENT_RE = re.compile(r"(?<=[.;:])\s+(?=[A-Z0-9(])")
ANAPHORA_RE = re.compile(r"\b(?:these|such|said|the\s+above)\s+(?:devices|items|systems|components|equipment|units)\b", re.I)
E_SHEET_RE = re.compile(r"^E[A-Z]?\d", re.I)
DEMO_SHEET_RE = re.compile(r"^[A-Z]{1,2}D\d|^ED", re.I)
MECH_SHEET_RE = re.compile(r"^M[A-Z]?\d", re.I)
PLUMB_SHEET_RE = re.compile(r"^P[A-Z]?\d", re.I)
DEMO_WORDS_RE = re.compile(r"\bdemo\w*|\bremov\w*|\bexisting\b.{0,40}\bdemolition\b", re.I)

# Systems whose owner-exhibit or by-others obligations become exclusion lines.
EXCLUSION_SYSTEMS: tuple[str, ...] = (
    "fire_alarm", "access_control", "security", "telecom_data", "low_voltage", "av", "telemetry", "rtls",
    "wireless_ap", "nurse_call", "furniture", "door_hardware", "paint", "firestopping", "hazardous", "signage",
)
SYSTEM_LABELS: dict[str, str] = {
    "fire_alarm": "Fire alarm and fire detection",
    "access_control": "access control",
    "security": "security and CCTV",
    "telecom_data": "telephone and data cabling and devices",
    "low_voltage": "low voltage systems",
    "av": "audio/visual",
    "telemetry": "telemetry antennas",
    "rtls": "Centrak devices",
    "wireless_ap": "wireless access points (WAP)",
    "nurse_call": "nurse call",
    "furniture": "furniture connections and furniture systems",
    "door_hardware": "door hardware (locksets, exit devices, closers, operators)",
    "paint": "painting",
    "firestopping": "firestopping of penetrations",
    "hazardous": "hazardous materials",
    "signage": "signage",
}
PARTY_LABELS: dict[str, str] = {
    "gc": "the general contractor",
    "owner": "the owner",
    "others": "others",
    "other_trade": "another trade",
    "owner_vendor": "the owner's vendor",
}
# RFI topics decided by facts on these systems (rewrite removes their clauses).
SYSTEM_RFI_TOPICS: dict[str, tuple[str, ...]] = {
    # Hyphenated forms ("Access-control system", "fire-alarm") are the same
    # topic: the run 2492ba7a brief kept "Access-control system furnishing
    # and wiring." because the space-only pattern missed it.
    "fire_alarm": (r"\bhoneywell\b", r"\bfire[\s-]+alarm\b", r"\bFA\b", r"\bdetector[\s-]protection\b",
                   r"\bfire[\s-]watch\b", r"\bmonitoring\s+agency\b"),
    "access_control": (r"\baccess[\s-]+controls?\b", r"\bcard[\s-]+readers?\b", r"\bCR\b"),
    "telemetry": (r"\btelemetry\b",),
    "rtls": (r"\bcentrak\b",),
    "wireless_ap": (r"\bwireless\s+access\b", r"\bWAPs?\b"),
    "door_hardware": (r"\bhardware\s+sets?\b", r"\blocksets?\b", r"\bexit\s+devices?\b", r"\bhardware\s+standard\b",
                      r"\bvon\s+duprin\b", r"\bmagnetic\s+locks?\b", r"\bsubstitution\s+precedence\b"),
    "furniture": (r"\bfurniture\s+(?:data\s+|electrical\s+)?connections?\b", r"\bpre-?manufactured\s+furniture\b"),
    "firestopping": (r"\bfirestop\w*\b", r"\bputty\s+pads?\b", r"\bbox\s+pads?\b"),
    "paint": (r"\bpainting\b",),
}
RULE_RFI_TOPICS: dict[str, tuple[str, ...]] = {
    "temporary_power": (r"\btemporary\s+(?:power|lighting|office|electric\w*)\b", r"\bHEPA\b", r"\bnegative\s+air\b"),
    "studies": (r"\bcoordination\s+study\b", r"\barc\s+flash\b", r"\bshort[\s-]circuit\s+study\b"),
    "bim_coordination": (r"\bBIM\b", r"\bcoordination\s+drawings?\b", r"\b3D\s+model\w*\b"),
    "firestop_labels": (r"\bfirestop\w*\b", r"\bputty\s+pads?\b", r"\bbox\s+pads?\b", r"\blabeling\b"),
    "firestopping": (r"\bfirestop\w*\b",),
    "cutting_patching_painting": (r"\bcutting\b", r"\bpatching\b", r"\bpainting\b"),
    "hazardous_materials": (r"\bhazardous\b", r"\basbestos\b", r"\babatement\b"),
    "permits": (r"\bpermits?\b(?!\s+set)",),
}
DECIDED_TOPICS_FIXED: dict[str, tuple[str, ...]] = {
    "wage": (r"\bprevailing\s+wage\b", r"\bNRS\s*338\b", r"\bdavis.?bacon\b", r"\bwage\s+determination\b",
             r"\bcertified\s+payroll\b"),
    "breaker_reuse": (r"\breused?\s+(?:versus|vs\.?|or)\s+new\b", r"\bwhich\s+of\s+the\s+\d+\s+non-?gfci\b",
                      r"\bnew-?versus-?reuse\b"),
    "thermostats": (r"\bfive\s+relocations\b", r"\bthermostat\s+relocations?\b", r"\bsensor\s+conduit/cable\b",
                    r"\bcontrol\s+cable\b"),
}
DEMO_POWER_RE = re.compile(r"\brecept|\boutlet|\bpower\s+devices?|\bfloor\s+box|\bjunction\b|\bconnection\b|\bmake\s+safe\b"
                           r"|\bdisconnect\b|\bduplex\b|\bquad", re.I)
DEMO_LIGHTING_RE = re.compile(r"\bfixture|\blight\b|\blighting\b|\bdownlight|\btroffer|\bswitch|\bsensor|\bdimmer"
                              r"|\bhead\b|\bexit\s+sign|\bluminaire", re.I)
TRACK_ROW_RE = re.compile(r"\btrack\b(?!.*\bheads?\b)", re.I)
MAKE_SAFE_RE = re.compile(r"\bmake\s*safe\b", re.I)
DEMO_POWER_LINE = "REMOVAL OF POWER DEVICES W/ BACKBOX & ASSOCIATE WIRE, CONDUIT BACK TO SOURCE"
DEMO_LIGHTING_LINE = "REMOVAL OF LIGHTING FIXTURE AND CONTROLS W/ ASSOCIATE WIRE & CONDUIT BACK TO SOURCE"
DEMO_NOT_REMOVAL_RE = re.compile(
    r"\brelocat\w*|\breconnect\w*|\bre-?install\w*|\bre-?use\w*|\bmaintain\w*|\bremains?\b|\bto\s+remain\b"
    r"|\bprotect\w*|\bsalvag\w*|\bintercept\w*|\bextend\w*|\bnew\s+location\b|\bto\s+be\s+replaced\b", re.I)
DEMO_EQUIPMENT_RE = re.compile(
    r"\bdisconnect\s+switch\w*|\b(?:remove|removal\s+of)\s+(?:the\s+)?(?:existing\s+|\(e\)\s*)?disconnects?\b"
    r"|\bpanel\w*|\bpoles?\b|\bequipment\b|\btransformers?\b|\bfeeders?\b|\bmotors?\b|\bcompressors?\b|\bservers?\b"
    r"|\bworkstations?\b|\bevse\b|\bsign(?:s|age)?\b|\bunits?\b|\bswitchboards?\b|\bgenerators?\b|\bfans?\b"
    r"|\bpumps?\b|\bheaters?\b|\bbackboards?\b", re.I)
DEMO_LOW_VOLTAGE_RE = re.compile(
    r"\b(?:com+unications?|data|tele(?:phone|com)\w*|voice|catv|tv|av|fiber|speaker)\s+(?:outlets?|jacks?|devices?)\b"
    r"|\bpatch\s+cables?\b|\bcat\s?[56]\w*\b|\bLC-LC\b", re.I)
DEMO_FLOOR_LINE = "REMOVAL OF FLOOR BOX / FLOOR OUTLET ASSEMBLY W/ ASSOCIATE WIRE, CONDUIT BACK TO SOURCE"
DEMO_FLOOR_RE = re.compile(r"\bfloor\s+(?:box(?:es)?|outlets?|receptacles?|devices?|poke-?thru|assembl\w*)\b|\bpoke-?thru",
                           re.I)
THERMOSTAT_FAMILY_RE = re.compile(r"\b(?:thermostats?|t-?stats?|temp(?:erature)?\s+sensors?)\b", re.I)
CONTROL_CABLE_RE = re.compile(r"#\s?18\s*/\s*4\s*C|\bcontrol\s+cable\b|\bthermostat\s+cable\b", re.I)


# Inputs ---------------------------------------------------------------------------------------


class ScopeInputs:
    """Everything prepare reads, behind one seam (DB, replay or tests).

    `documents`: rows with id, name, category, page_count. `project`: the
    project row or None. `category_outputs`: {category: output}. `measure`:
    the WS3 MeasureOutput or None. `pdf_bytes(doc_id)` returns the PDF or
    None. `doc_facts(doc_id)` returns WS1 DocFacts (or None);
    `page_lines(doc_id, page)` the text lines of a 1-based page."""

    def __init__(
        self,
        documents: Sequence[Mapping[str, Any]],
        *,
        project: Mapping[str, Any] | None = None,
        category_outputs: Mapping[str, Any] | None = None,
        measure: Mapping[str, Any] | None = None,
        main_output: Mapping[str, Any] | None = None,
        pdf_bytes: Callable[[str], bytes | None] | None = None,
        doc_facts: Callable[[str], Any] | None = None,
        page_lines: Callable[[str, int], list[str] | None] | None = None,
        extract_budget_s: float = 240.0,
    ) -> None:
        self.documents = [dict(d) for d in documents]
        self.project = dict(project) if project else None
        self.category_outputs = {k: v for k, v in (category_outputs or {}).items() if isinstance(v, Mapping)}
        self.measure = dict(measure) if isinstance(measure, Mapping) else None
        self.main_output = main_output
        self._pdf = pdf_bytes
        self._facts_fn = doc_facts
        self._lines_fn = page_lines
        self._facts: dict[str, Any] = {}
        self._lines: dict[tuple[str, int], list[str]] = {}
        self._bytes: dict[str, bytes | None] = {}
        self.extract_budget_s = extract_budget_s
        self.notes: list[str] = []

    def document(self, doc_id: str) -> dict[str, Any] | None:
        for d in self.documents:
            if str(d.get("id")) == str(doc_id):
                return d
        return None

    def pdf(self, doc_id: str) -> bytes | None:
        if doc_id not in self._bytes:
            data = None
            if self._pdf is not None:
                try:
                    data = self._pdf(doc_id)
                except Exception as exc:  # noqa: BLE001 - a missing PDF degrades to no facts
                    self.notes.append(f"pdf {doc_id[:8]} unavailable: {type(exc).__name__}")
            self._bytes[doc_id] = data
        return self._bytes[doc_id]

    def facts(self, doc_id: str) -> Any:
        """WS1 DocFacts of a drawing document (cached). On-the-fly extraction
        from the PDF when no facts function answers."""
        if doc_id in self._facts:
            return self._facts[doc_id]
        facts = None
        if self._facts_fn is not None:
            try:
                facts = self._facts_fn(doc_id)
            except Exception as exc:  # noqa: BLE001 - FactsUnavailable and friends
                self.notes.append(f"facts {doc_id[:8]} unavailable: {type(exc).__name__}")
        if facts is None:
            data = self.pdf(doc_id)
            doc = self.document(doc_id) or {}
            if data:
                try:
                    from app.services.pagefacts.extract import extract_document

                    facts = extract_document(data, label=doc.get("category"), name=doc.get("name"),
                                             doc_budget_s=self.extract_budget_s)
                except Exception as exc:  # noqa: BLE001 - an unreadable PDF has no facts
                    self.notes.append(f"extract {doc_id[:8]} failed: {type(exc).__name__}: {exc}"[:200])
        if isinstance(facts, Mapping):
            from app.services.pagefacts.contracts import DocFacts

            facts = DocFacts.from_dict(dict(facts))
        self._facts[doc_id] = facts
        return facts

    def lines(self, doc_id: str, page: int) -> list[str]:
        key = (doc_id, int(page))
        if key in self._lines:
            return self._lines[key]
        out: list[str] | None = None
        if self._lines_fn is not None:
            try:
                out = self._lines_fn(doc_id, page)
            except Exception:  # noqa: BLE001
                out = None
        if out is None:
            data = self.pdf(doc_id)
            if data:
                try:
                    from app.services.pagefacts.textlayer import positioned_words

                    pw = positioned_words(data, int(page) - 1, objects=False)
                    out = [ln.t for ln in pw.lines]
                except Exception:  # noqa: BLE001 - a page past the end or unreadable
                    out = []
        self._lines[key] = list(out or [])
        return self._lines[key]

    def page_text(self, doc_id: str, page: int) -> str:
        return "\n".join(self.lines(doc_id, page))

    def page_count(self, doc_id: str) -> int:
        doc = self.document(doc_id) or {}
        n = doc.get("page_count")
        if isinstance(n, int) and n > 0:
            return n
        data = self.pdf(doc_id)
        if not data:
            return 0
        try:
            from app.services.pagefacts.textlayer import page_count

            return page_count(data)
        except Exception:  # noqa: BLE001
            return 0


# Statements -----------------------------------------------------------------------------------


def _sheet_role(sheet: str | None, category: str | None, base_role: str) -> str:
    s = (sheet or "").strip()
    if (category or "") == "mechanical_drawings" or MECH_SHEET_RE.match(s):
        return "mechanical_note"
    if (category or "") == "plumbing_drawings" or PLUMB_SHEET_RE.match(s):
        return "plumbing_note"
    return base_role


def _page_sheet(page: Any) -> str | None:
    return getattr(page, "sheet_number", None)


def statements_from_drawing(doc: Mapping[str, Any], facts: Any, inputs: ScopeInputs) -> list[Statement]:
    """Note items of every page (keynotes, sheet notes, general notes) plus
    the text lines of a notes page that has no parsed note blocks."""
    out: list[Statement] = []
    doc_id = str(doc.get("id"))
    category = str(doc.get("category") or "")
    if facts is None:
        return out
    for page in getattr(facts, "pages", []) or []:
        sheet = _page_sheet(page)
        items_found = 0
        for blk in getattr(page, "notes", []) or []:
            kind = str(blk.get("kind") or "")
            base = NOTE_ROLE_BY_KIND.get(kind)
            if base is None:
                continue
            role = _sheet_role(sheet, category, base)
            for it in blk.get("items") or []:
                text = attr.ascii_fold(str(it.get("text") or ""))
                if len(text) < 8:
                    continue
                items_found += 1
                num = it.get("number")
                out.append(Statement(
                    text=text[:MAX_SENTENCE_CHARS], document_id=doc_id, page=int(page.page),
                    role=role, level=attr.LEVEL_BY_ROLE.get(base, "general_note"), sheet=sheet,
                    ref=f"{REF_PREFIX.get(base, 'N')}{num}" if num else None, number=str(num) if num else None,
                ))
        roles = set(getattr(page, "roles", []) or []) | {getattr(page, "page_role", "")}
        if items_found == 0 and roles & LINE_STATEMENT_ROLES and (not sheet or E_SHEET_RE.match(sheet)):
            role = _sheet_role(sheet, category, "general_note")
            for i, line in enumerate(inputs.lines(doc_id, int(page.page)), 1):
                text = attr.ascii_fold(line)
                if len(text) >= 12:
                    out.append(Statement(text=text, document_id=doc_id, page=int(page.page), role=role,
                                         level="general_note", sheet=sheet, ref=f"line {i}"))
    return out


def _spec_role(lines: Sequence[str]) -> str:
    head = " ".join(lines[:6]) + " " + " ".join(lines[-6:])
    if re.search(r"\b(?:SECTION|DIVISION)\s+2[678]\b|\b2[678]\s?\d{2}\s?\d{2}\b", head):
        return "electrical_spec"
    return "spec"


def statements_from_text(doc: Mapping[str, Any], inputs: ScopeInputs, *, max_pages: int = MAX_TEXT_PAGES) -> list[Statement]:
    """Sentences of a text document (exhibit, specification, addendum)."""
    out: list[Statement] = []
    doc_id = str(doc.get("id"))
    base = TEXT_ROLE_BY_CATEGORY.get(str(doc.get("category") or ""), "spec")
    n = min(inputs.page_count(doc_id), max_pages)
    for page in range(1, n + 1):
        lines = inputs.lines(doc_id, page)
        if not lines:
            continue
        role = _spec_role(lines) if base == "spec" else base
        text = attr.ascii_fold(" ".join(lines))
        prev: str | None = None
        for sent in _SENT_RE.split(text):
            s = sent.strip()
            if len(s) < 12:
                continue
            if prev and ANAPHORA_RE.search(s):
                # "Removal ... of these devices ..." names its subject in the
                # sentence before; the statement carries both.
                if out and out[-1].text == prev[:MAX_SENTENCE_CHARS]:
                    out.pop()
                s = f"{prev} {s}"
            out.append(Statement(text=s[:MAX_SENTENCE_CHARS], document_id=doc_id, page=page, role=role,
                                 level=attr.LEVEL_BY_ROLE.get(role, "spec")))
            prev = s
    return out


def build_statements(inputs: ScopeInputs) -> list[Statement]:
    out: list[Statement] = []
    for doc in inputs.documents:
        category = str(doc.get("category") or "")
        if category in DRAWING_CATEGORIES:
            out.extend(statements_from_drawing(doc, inputs.facts(str(doc.get("id"))), inputs))
        elif category in TEXT_ROLE_BY_CATEGORY:
            out.extend(statements_from_text(doc, inputs))
    return out


# Drawn systems, breakers, devices and inclusions ----------------------------------------------


def _rows(output: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    return [r for r in output.get(key) or [] if isinstance(r, Mapping)]


def _row_sheet(row: Mapping[str, Any]) -> str:
    sheet = row.get("sheet_number")
    if not sheet and isinstance(row.get("sheet"), Mapping):
        sheet = row["sheet"].get("sheet_number")
    return str(sheet or "")


def boq_items(main_output: Mapping[str, Any] | None) -> Iterable[tuple[str, str, str, dict[str, Any]]]:
    boq = (main_output or {}).get("boq") if isinstance(main_output, Mapping) else None
    if not isinstance(boq, Mapping):
        return
    for area in boq.get("areas") or []:
        for sec in area.get("sections") or []:
            heading = str(sec.get("heading") or "")
            for grp in sec.get("groups") or []:
                for sub in grp.get("subgroups") or []:
                    for it in sub.get("items") or []:
                        if isinstance(it, dict):
                            yield heading, str(grp.get("name") or ""), str(sub.get("name") or ""), it


def drawn_systems(inputs: ScopeInputs) -> set[str]:
    """Systems counted, scheduled or listed on electrical sheets."""
    out: set[str] = set()
    for output in inputs.category_outputs.values():
        for key in ("counts", "runs", "schedules"):
            for row in _rows(output, key):
                sheet = _row_sheet(row)
                if sheet and not E_SHEET_RE.match(sheet):
                    continue
                out.update(s for s in attr.systems_in(str(row.get("description") or "")) if s not in attr.GENERIC_SYSTEMS)
    for _h, _g, _s, it in boq_items(inputs.main_output):
        refs = [str(r) for r in it.get("sheet_refs") or []]
        if any(E_SHEET_RE.match(r) for r in refs):
            out.update(s for s in attr.systems_in(str(it.get("description") or "")) if s not in attr.GENERIC_SYSTEMS)
    return out


def _electrical_docs(inputs: ScopeInputs) -> list[tuple[dict[str, Any], Any]]:
    out = []
    for doc in inputs.documents:
        if str(doc.get("category") or "") not in ("electrical_drawings", "drawings"):
            continue
        facts = inputs.facts(str(doc.get("id")))
        if facts is None:
            continue
        pages = getattr(facts, "pages", []) or []
        numbered = [p for p in pages if p.sheet_number]
        if numbered and sum(1 for p in numbered if MECH_SHEET_RE.match(p.sheet_number)) >= 0.8 * len(numbered):
            continue  # a mechanical set labeled electrical (WS1 relabel)
        out.append((doc, facts))
    return out


def _panel_schedule_rows(inputs: ScopeInputs) -> list[Mapping[str, Any]]:
    """The reader's panel schedule S rows on electrical sheets (any category
    output), so the breaker plan reads panels WS1 did not parse (run 785ae29e:
    WS1 found no schedule on E0.02 and read general notes on E0.00 as panel
    "AND"; the reader transcribed S2 "Branch Panel: A" and S4 "Branch Panel:
    B")."""
    out: list[Mapping[str, Any]] = []
    for _category, output in sorted(inputs.category_outputs.items()):
        for row in _rows(output, "schedules"):
            if str(row.get("schedule_type") or "").lower() != "panel":
                continue
            sheet = _row_sheet(row)
            if sheet and not E_SHEET_RE.match(sheet):
                continue
            out.append(row)
    return out


def breaker_plan(inputs: ScopeInputs) -> breakers.BreakerPlan:
    schedules: list[Mapping[str, Any]] = []
    sheet_for_page: dict[int, str] = {}
    for _doc, facts in _electrical_docs(inputs):
        schedules.extend(getattr(facts, "schedules", []) or [])
        for p in getattr(facts, "pages", []) or []:
            if p.sheet_number:
                sheet_for_page[int(p.page)] = p.sheet_number
    return breakers.plan_breakers(schedules, sheet_for_page=sheet_for_page,
                                  schedule_rows=_panel_schedule_rows(inputs))


def _new_work_plan_pages(facts: Any) -> list[int]:
    """New-work electrical plan pages at a working scale (overall plans at
    1/16" and demolition sheets excluded)."""
    out = []
    for p in getattr(facts, "pages", []) or []:
        sheet = (p.sheet_number or "").upper()
        title = (p.sheet_title or "").upper()
        roles = set(p.roles or []) | {p.page_role}
        if "plan" not in roles:
            continue
        if sheet and not E_SHEET_RE.match(sheet):
            continue
        if DEMO_SHEET_RE.match(sheet) or "DEMOLITION" in title:
            continue
        if p.primary_ft_per_in is not None and float(p.primary_ft_per_in) > 8.0:
            continue
        out.append(int(p.page))
    return out


def device_facts(inputs: ScopeInputs, statements: Sequence[Statement],
                 *, symbol_counter: Callable[..., dict[str, Any] | None] | None = None) -> devices.DeviceFacts:
    df = devices.DeviceFacts()
    e_statements = [s for s in statements if s.sheet and E_SHEET_RE.match(s.sheet)] + \
        [s for s in statements if s.role == "electrical_spec"]
    notes, specs, room = devices.grade_facts(e_statements)
    df.grade_notes, df.spec_facts, df.tr_room_rule = notes, specs, room
    counter = symbol_counter or devices.count_legend_symbol
    for doc, facts in _electrical_docs(inputs):
        entry = devices.tr_legend_entry(getattr(facts, "legend", None))
        if entry is None:
            continue
        pages = _new_work_plan_pages(facts)
        data = inputs.pdf(str(doc.get("id")))
        if not pages or not data:
            df.warnings.append("tr_symbol_census_unavailable")
            continue
        try:
            res = counter(data, legend_page=int(entry["page"]), symbol_bbox=entry["symbol_bbox"], plan_pages=pages)
        except Exception as exc:  # noqa: BLE001 - a census failure leaves TR unknown
            df.warnings.append(f"tr_symbol_census_failed: {type(exc).__name__}")
            continue
        if not res:
            df.warnings.append("tr_symbol_template_unmeasured")
            continue
        total = int(res.get("total") or 0)
        if total <= 0 or not any(int(n or 0) > 0 for n in (res.get("pages") or {}).values()):
            # A census with no hit on any plan page is a matcher miss until
            # proven otherwise (block instancing, another drawing style):
            # the TR count stays unknown (spec 5.1), never 0.
            df.warnings.append("tr_symbol_census_zero")
            continue
        sheet_of = {int(p.page): p.sheet_number for p in facts.pages}
        df.tr_count = (df.tr_count or 0) + total
        df.tr_method = "legend_symbol"
        for pg, n in (res.get("pages") or {}).items():
            if n:
                df.tr_pages[str(sheet_of.get(int(pg)) or pg)] = int(n)
        legend_sheet = sheet_of.get(int(entry["page"])) or "legend"
        on = ", ".join(f"{k} x{v}" for k, v in df.tr_pages.items()) or "no plan page"
        df.tr_cite = f"{entry.get('tag') or 'REC-TAMP'} legend symbol on {legend_sheet}; counted {on}"
    return df


def _is_demo_row(row: Mapping[str, Any]) -> bool:
    sheet = _row_sheet(row)
    if sheet and DEMO_SHEET_RE.match(sheet):
        return True
    return bool(DEMO_WORDS_RE.search(str(row.get("description") or "")))


def _page_is_demolition(page: Any, sheet: str) -> bool:
    title = str(getattr(page, "sheet_title", None) or "").upper()
    return bool(sheet and DEMO_SHEET_RE.match(sheet)) or "DEMOLITION" in title


def facts_triggers(inputs: ScopeInputs, pattern: re.Pattern[str], also: re.Pattern[str] | None,
                   *, categories: Iterable[str] = DRAWING_CATEGORIES) -> list[dict[str, Any]]:
    """Inclusion triggers from the WS1 page facts of the drawing sets: one
    trigger per keynote whose text matches the inclusion pattern (and
    `also_pattern`), with the quantity = that keynote's marker count on the
    page (`keynote_markers`, the hexagon numbers in the plan area). This is
    the $0 trigger source when no category row carries the item: a
    mechanical set relabeled from electrical is read in reduced mode and its
    thermostat keynotes never become reader rows, but its facts (keynote
    text and markers on MD2.01 and M2.01) are always there. The trigger cites
    the sheet and keynote; a keynote with no marker on its page is recorded
    with quantity 0 and never picked."""
    out: list[dict[str, Any]] = []
    wanted = set(categories)
    for doc in inputs.documents:
        if str(doc.get("category") or "") not in wanted:
            continue
        doc_id = str(doc.get("id"))
        facts = inputs.facts(doc_id)
        if facts is None:
            continue
        for page in getattr(facts, "pages", []) or []:
            sheet = str(_page_sheet(page) or "")
            markers = getattr(page, "keynote_markers", None) or []
            for blk in getattr(page, "notes", []) or []:
                if str(blk.get("kind") or "") != "keynotes":
                    continue
                for it in blk.get("items") or []:
                    text = attr.ascii_fold(str(it.get("text") or ""))
                    if not pattern.search(text) or (also is not None and not also.search(text)):
                        continue
                    number = str(it.get("number") or "")
                    count = sum(1 for m in markers if isinstance(m, Mapping) and str(m.get("number")) == number) \
                        if number else 0
                    page_no = int(getattr(page, "page", 0) or 0)
                    out.append({
                        "description": text[:200],
                        "quantity": float(count),
                        "sheet": sheet or None,
                        "demolition": _page_is_demolition(page, sheet)
                        or bool(re.search(r"\bdemolition\b|\bremove\s+wiring\b", text, re.I)),
                        "row_id": None,
                        "source": "page_facts",
                        "ref": f"KN{number}" if number else None,
                        "document_id": doc_id,
                        "page": page_no,
                        "markers": count,
                        # the keynote's text line in the WS1 text layer, as the
                        # tool layer cites it (t:<doc8>:<page>:<line>), so an
                        # inserted line can cite the fact it rests on
                        "text_ref": keynote_text_ref(inputs, doc_id, page_no, number, text),
                    })
    return out


def _fold(text: Any) -> str:
    return re.sub(r"\s+", " ", attr.ascii_fold(str(text or ""))).strip().upper()


KEYNOTE_HEAD_CHARS = (24, 14)


def keynote_text_ref(inputs: ScopeInputs, doc_id: str, page: int, number: str, text: str) -> str | None:
    """The `t:<doc8>:<page>:<line index>` ref of the text-layer line that
    starts the keynote `number` with `text` on `page` of `doc_id` (the same
    line indexing as pagefacts FactsTextIndex.runs, so the ref resolves
    through the tool layer). The line that opens with the keynote number and
    carries the head of its text wins; else the first line carrying the head
    (24 characters, then 14 when the line breaks early). None when the page
    has no text lines or none carries the keynote text."""
    if not page or page <= 0:
        return None
    try:
        lines = inputs.lines(doc_id, page)
    except Exception:  # noqa: BLE001 - a page without a text layer has no ref
        return None
    if not lines:
        return None
    folded = [_fold(ln) for ln in lines]
    whole = _fold(text)
    num_re = re.compile(rf"^{re.escape(str(number))}\b") if number else None
    if num_re is not None and whole:
        # The numbered line itself, even when it wraps after a few words
        # ("7. PROVIDE NEW" / "THERMOSTAT WIRING."): its text after the
        # number opens the keynote text.
        for i, t in enumerate(folded):
            if not num_re.match(t):
                continue
            rest = re.sub(rf"^{re.escape(str(number))}\W*", "", t).strip()
            if len(rest) >= 6 and whole.startswith(rest):
                return f"t:{str(doc_id)[:8]}:{int(page)}:{i}"
    for n in KEYNOTE_HEAD_CHARS:
        head = whole[:n]
        if len(head) < min(n, 8):
            continue
        hits = [i for i, t in enumerate(folded) if head in t]
        if not hits:
            continue
        numbered = [i for i in hits if num_re is not None and num_re.match(folded[i])]
        return f"t:{str(doc_id)[:8]}:{int(page)}:{(numbered or hits)[0]}"
    return None


def compute_inclusions(inputs: ScopeInputs, policy_doc: Mapping[str, Any], *, settings: Any = None) -> list[dict[str, Any]]:
    """Inclusion triggers from the reader rows (spec WS5, Q14); a thermostat
    inclusion with no reader row falls back to the WS1 page facts of the
    drawing sets (`facts_triggers`)."""
    rows: list[Mapping[str, Any]] = []
    for output in inputs.category_outputs.values():
        rows.extend(_rows(output, "counts"))
    incs = list(policy_doc.get("inclusions") or [])
    ids = scope_policy.inclusion_rule_ids(incs)
    out: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for inc, rid in zip(incs, ids, strict=True):
        if not isinstance(inc, Mapping) or inc.get("enabled") is False or not rid:
            continue
        tpl = dict(inc.get("line_template") or {})
        pat = scope_policy.compile_pattern(str(inc["pattern"]))
        also = scope_policy.compile_pattern(str(inc["also_pattern"])) if inc.get("also_pattern") else None
        companion = inc.get("companion_of")
        triggers = []
        for r in rows:
            desc = str(r.get("description") or "")
            blob = f"{desc} {r.get('notes') or ''}"
            if not pat.search(desc):
                continue
            if also is not None and not also.search(desc):
                continue
            if str(r.get("unit") or "EA").upper() != "EA":
                continue
            sheet = _row_sheet(r)
            if inc.get("trigger_family") == "equipment_connection" and sheet and not E_SHEET_RE.match(sheet):
                continue
            triggers.append({
                "description": desc[:200],
                "quantity": float(r.get("quantity") or 0),
                "sheet": sheet or None,
                "demolition": _is_demo_row(r) or bool(re.search(r"\bdemolition\b|\bremove\s+wiring\b", blob, re.I)),
                # the row id (v2 rows) so an inserted inclusion line can cite
                # its sources (spec 5.8: every line cites sources)
                "row_id": str(r["row_id"]) if r.get("row_id") else None,
            })
        quantity = 0.0
        basis = "counted"
        caveat = None
        measure_row_id = None
        from_facts = False
        scope = str(inc.get("dedupe_scope") or "sheet")
        if companion:
            parent = by_id.get(str(companion))
            if not parent or not parent.get("quantity"):
                continue
            mode = tpl.get("quantity")
            measured = _measured_quantity(inputs.measure, tpl)
            if mode == "measured_else_per_trigger" and measured:
                quantity, basis, measure_row_id = measured
            else:
                per = _per_trigger_length(settings, inc.get("settings_key"))
                if per is None or per <= 0:
                    continue  # no assumed length configured (0 turns the companion off)
                quantity = float(parent["quantity"]) * per
                basis = "assumed_per_trigger"
                caveat = tpl.get("caveat_when_assumed")
            triggers = parent["triggers"]
        else:
            if not triggers and inc.get("trigger_family") == "thermostat":
                triggers = facts_triggers(inputs, pat, also)
                from_facts = True
            if not triggers:
                continue
            counted = [t for t in triggers if t["quantity"] > 0]
            if from_facts and not counted:
                continue  # keynote text without a marker on its page: no quantity to carry
            if scope == "run_new_work_wins":
                new = [t for t in counted if not t["demolition"]]
                demo = [t for t in counted if t["demolition"]]
                pick = new or demo
                quantity = sum(t["quantity"] for t in pick)
                basis = "counted"
                for t in triggers:
                    t["used"] = t in pick
            else:
                quantity = sum(t["quantity"] for t in counted)
                for t in triggers:
                    t["used"] = t in counted
        rec = {
            "rule_id": rid,
            "trigger_family": inc.get("trigger_family"),
            "description": tpl.get("description"),
            "section": tpl.get("section") or "NEW WORK",
            "group": tpl.get("group"),
            "subgroup": tpl.get("subgroup"),
            "unit": tpl.get("unit") or "EA",
            "quantity": round(quantity, 2) if not float(quantity).is_integer() else int(quantity),
            "basis": basis,
            "caveat": caveat,
            "wastage_applies": bool(tpl.get("wastage_applies", False)),
            "material_category": tpl.get("material_category"),
            "dedupe_scope": scope,
            "companion_of": companion,
            "clarification": inc.get("clarification"),
            "pattern": inc.get("pattern"),
            "also_pattern": inc.get("also_pattern"),
            "triggers": triggers,
            "measure_row_id": measure_row_id,
            "trigger_source": "page_facts" if from_facts or (companion and (by_id.get(str(companion)) or {}).get(
                "trigger_source") == "page_facts") else "reader_rows",
        }
        by_id[rid] = rec
        out.append(rec)
    return out


def _per_trigger_length(settings: Any, key: Any) -> float | None:
    """The assumed length per trigger from settings only (5.7
    `plans_reader_control_cable_ft_per_device`, code default 25). 0 is a
    configured value (no assumed cable); None when the policy names no
    settings key or the settings carry no such attribute."""
    if not key:
        return None
    s = settings if settings is not None else _settings(None)
    value = getattr(s, str(key), None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _measured_quantity(measure: Mapping[str, Any] | None, tpl: Mapping[str, Any]) -> tuple[float, str, str | None] | None:
    """(quantity, 'measured', the M row id or None) of the measure output's
    control cable row; the row id lets an inserted companion line cite it."""
    if not measure:
        return None
    for q in measure.get("quantities") or []:
        if not isinstance(q, Mapping):
            continue
        if CONTROL_CABLE_RE.search(str(q.get("description") or "")) and str(q.get("unit") or "FT").upper() == "FT":
            try:
                return float(q.get("quantity") or 0), "measured", (str(q["row_id"]) if q.get("row_id") else None)
            except (TypeError, ValueError):
                return None
    return None


# Prepare ---------------------------------------------------------------------------------------


MAX_CONTEXT_QUESTIONS = 300


def category_questions(inputs: ScopeInputs) -> list[dict[str, Any]]:
    """The reader questions of every category output (row id, category,
    sheet, text), so enforce can resolve the ones the G3 decisions answer
    (E20: a demolition question survives in the category output after the
    disposition is decided; the brief RFIs are rewritten but category rows
    are immutable, so the report records the resolution)."""
    out: list[dict[str, Any]] = []
    for category, output in sorted(inputs.category_outputs.items()):
        for q in _rows(output, "questions"):
            if str(q.get("status") or "active") not in ("active", ""):
                continue
            text = str(q.get("question") or q.get("text") or "").strip()
            if not text:
                continue
            out.append({"row_id": str(q["row_id"]) if q.get("row_id") else None, "category": str(category),
                        "sheet": _row_sheet(q) or None, "question": text[:600]})
            if len(out) >= MAX_CONTEXT_QUESTIONS:
                return out
    return out


LUMINAIRE_CONTROL_TOPIC = "luminaire_control"


def category_luminaire_schedule(inputs: ScopeInputs) -> list[dict[str, Any]]:
    """The run's luminaire schedule as `[{tag, description, control, ref,
    sheet}]` (plans_reader_consolidator.luminaire_schedule over the S rows of
    the category outputs; `control` is None when the CONTROL cell is not
    readable), so enforce resolves a reader question the schedule already
    answers (E19: "confirm L4 local control" with L4 LOCAL on E0.04).
    Empty when no schedule is read or the outputs cannot be indexed."""
    outputs = {str(c): dict(o) for c, o in (inputs.category_outputs or {}).items() if isinstance(o, Mapping)}
    if not outputs:
        return []
    try:
        from app.services import plans_reader_consolidator as cons
        from app.services import plans_reader_rows as rows_mod

        rows = {rid: r for rid, r in rows_mod.index_rows(outputs).items() if rid.startswith("S")}
        return [{k: e.get(k) for k in ("tag", "description", "control", "ref", "sheet")} for e in cons.luminaire_schedule(rows)]
    except Exception:  # noqa: BLE001 - the schedule only resolves questions; never fail a context
        logger.exception("scope_engine: luminaire schedule unavailable")
        return []


def _bid_documents(statements: Sequence[Statement], inputs: ScopeInputs) -> list[bidc.DocumentText]:
    docs: list[bidc.DocumentText] = []
    for s in statements:
        docs.append(bidc.DocumentText(text=s.text, document_id=s.document_id, page=s.page,
                                      role=OWNER_ROLE_FOR_BID.get(s.role, s.role)))
    return docs


def prepare_context(
    inputs: ScopeInputs,
    *,
    policy: scope_policy.ActivePolicy | None = None,
    settings: Any = None,
    allow_llm: bool = False,
    llm_caller: attr.LlmCaller | None = None,
    symbol_counter: Callable[..., dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """The ScopeContext of one run from its inputs (pure over `inputs`)."""
    started = time.monotonic()
    active = policy or scope_policy.active_policy()
    doc = active.doc
    names = {str(d.get("id")): str(d.get("name") or "document") for d in inputs.documents}
    statements = build_statements(inputs)
    obligations = attr.attribute(statements, document_names=names)
    drawn = drawn_systems(inputs)
    for ob in obligations:
        ob.drawn = bool(set(ob.attribution.systems) & drawn)
    fallback: dict[str, Any] | None = None
    if allow_llm and settings is not None and getattr(settings, "ai_scope_attrib_llm", False):
        caller = llm_caller or attr.default_llm_caller(settings)
        res = attr.llm_fallback(obligations, caller=caller, page_text=inputs.page_text,
                                max_statements=int(getattr(settings, "ai_scope_attrib_max_statements", 40)))
        fallback = res.to_dict()
    fact_obs = [ob for ob in obligations if _fact_eligible(ob)]
    facts = attr.facts_from_obligations(fact_obs, inputs.page_text)
    bid = bidc.classify(inputs.project, _bid_documents(statements, inputs), doc)
    std = standards.resolve(statements, doc, document_names=names)
    plan = breaker_plan(inputs)
    dfacts = device_facts(inputs, statements, symbol_counter=symbol_counter)
    inclusions = compute_inclusions(inputs, doc, settings=settings)
    roles = []
    for d in inputs.documents:
        cat = str(d.get("category") or "")
        roles.append({"document_id": str(d.get("id")), "name": d.get("name"), "category": cat,
                      "role": "drawing" if cat in DRAWING_CATEGORIES else TEXT_ROLE_BY_CATEGORY.get(cat, "other")})
    ctx: dict[str, Any] = {
        "schema": SCOPE_CONTEXT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "policy_id": active.policy_id,
        "policy_sha": active.sha256,
        "policy_version": active.version,
        "policy_source": active.source,
        "bid_conditions": bid.to_dict(),
        "documents": roles,
        "obligations": [ob.to_dict() for ob in obligations],
        "facts": [f.to_dict() for f in facts],
        "fact_splits": _fact_splits(fact_obs, facts),
        "standards": standards.decisions_dict(std),
        "breaker_plan": plan.to_dict(),
        "inclusions": inclusions,
        "device_facts": dfacts.to_dict(),
        "drawn_systems": sorted(drawn),
        "document_amounts": _document_amounts(statements, doc),
        "questions": category_questions(inputs),
        "luminaire_schedule": category_luminaire_schedule(inputs),
        "fallback": fallback,
        "stats": {
            "statements": len(statements),
            "obligations": len(obligations),
            "excluded_obligations": sum(1 for ob in obligations if ob.carry == "exclude"),
            "facts": len(facts),
            "verified_facts": sum(1 for f in facts if f.verified),
            "seconds": round(time.monotonic() - started, 2),
        },
        "notes": list(inputs.notes),
    }
    ctx["digest"] = render_digest(ctx, max_tokens=int(getattr(settings, "ai_scope_digest_max_tokens", 3500) or 3500))
    return ctx


def _fact_eligible(ob: Obligation) -> bool:
    a = ob.attribution
    if a.fact_kind == "not_used":
        return True
    if a.carry != "exclude":
        return False
    return ob.statement.role in attr.OWNER_DOC_ROLES - {"spec"} or a.party in attr.THIRD_PARTIES


def _fact_splits(obligations: Sequence[Obligation], facts: Sequence[Fact]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    by_quote = {ob.statement.text[:300]: ob for ob in obligations}
    for f in facts:
        ob = by_quote.get(f.cite.quote)
        out[f.id] = ob.attribution.split if ob else None
    return out


def _document_amounts(statements: Sequence[Statement], policy_doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    pats = [scope_policy.compile_pattern(p) for p in (policy_doc.get("allowances") or {}).get("document_amount_patterns") or ()]
    out = []
    for s in statements:
        for p in pats:
            m = p.search(s.text)
            if m:
                out.append({"text": s.text[:240], "document_id": s.document_id, "page": s.page, "match": m.group(0)})
                break
    return out


# Digest, rules text, evaluate, standards and facts accessors --------------------------------------


def render_digest(ctx: Mapping[str, Any], *, max_tokens: int = 3500) -> str:
    """The binding G3 SCOPE DECISIONS block for the main and review prompts,
    capped at `max_tokens` (3 chars per token)."""
    lines = ["G3 SCOPE DECISIONS (binding; computed by code from the documents and the G3 policy):"]
    bid = ctx.get("bid_conditions") or {}
    if bid:
        cue = next((c for c in bid.get("cues") or [] if c.get("weight", 0) > 0), None)
        basis = bid.get("wage_basis")
        cue_text = f'; cue "{cue.get("quote", "")[:80]}"' if cue else ""
        lines.append(f"- Wage: {bid.get('wage_type')} ({basis}{cue_text}). Labor time: {bid.get('labor_time')}.")
    for topic, d in (ctx.get("standards") or {}).items():
        if d.get("clarification"):
            review = f" needs_review {d['needs_review']}." if d.get("needs_review") else ""
            lines.append(f"- Standard {topic}: {d.get('value')}. {d.get('clarification')}{review}")
    plan = ctx.get("breaker_plan") or {}
    if plan.get("clarification"):
        lines.append(f"- Breakers: {plan['clarification']} List breakers only for new panels and keyed positions.")
    for inc in ctx.get("inclusions") or []:
        lines.append(f"- Include {inc.get('description')} {inc.get('quantity')} {inc.get('unit')} ({inc.get('basis')}).")
    dev = ctx.get("device_facts") or {}
    if dev.get("tr_count") is not None:
        lines.append(f"- Tamper resistant duplex: {dev['tr_count']} EA ({dev.get('tr_cite')}).")
    for note in dev.get("grade_notes") or []:
        lines.append(f"- Devices: {note}")
    seen: set[str] = set()
    for f in ctx.get("facts") or []:
        if not f.get("verified") or f.get("kind") == "not_used":
            continue
        key = f"{f.get('system')}|{f.get('party')}"
        if key in seen:
            continue
        seen.add(key)
        cite = (f.get("cite") or {}).get("section")
        lines.append(f"- {f.get('system')} by {f.get('party')} per {cite} (fact {f.get('id')}).")
    lines.append("- Extra allowances only when a document names a dollar amount; house exclusions are never priced.")
    text = "\n".join(lines)
    cap = max(100, int(max_tokens)) * CHARS_PER_TOKEN
    if len(text) > cap:
        text = text[: cap - 40].rsplit("\n", 1)[0] + "\n- [digest cut at the token cap]"
    return text


def rules_text(ctx: Mapping[str, Any] | None = None, policy: Mapping[str, Any] | None = None) -> str:
    """The policy rules as prompt text (hook for WS4 and WS7)."""
    doc = policy or scope_policy.active_policy().doc
    lines = ["G3 scope policy rules:"]
    for r in doc.get("rules") or []:
        if not scope_policy.rule_enabled(r):
            continue
        lines.append(f"- p:{r['rule_id']} [{r['kind']} {r['decision']}] {r['default_text']}")
    if ctx:
        lines.append(render_digest(ctx))
    return "\n".join(lines)


@dataclass(frozen=True)
class PolicyHit:
    row_id: str | None
    rule_id: str
    decision: str
    kind: str
    matched: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"row_id": self.row_id, "rule_id": self.rule_id, "decision": self.decision, "kind": self.kind,
                "matched": self.matched, "text": self.text}


def evaluate(rows: Iterable[Mapping[str, Any]], ctx: Mapping[str, Any] | None = None,
             policy: Mapping[str, Any] | None = None) -> list[PolicyHit]:
    """Rule hits on rows or BOQ lines (hook for WS4 add_allowance refusals
    and WS7 rule lane). Exempt subgroups and patterns apply."""
    doc = policy or scope_policy.active_policy().doc
    hits: list[PolicyHit] = []
    for row in rows:
        text = str(row.get("description") or row.get("name") or "")
        for r in doc.get("rules") or []:
            if r.get("decision") == "include":
                continue
            m = scope_policy.match_rule(r, text, subgroup=row.get("subgroup"), group=row.get("group"))
            if m:
                hits.append(PolicyHit(row.get("line_id") or row.get("row_id"), r["rule_id"], r["decision"], r["kind"],
                                      m, text[:200]))
                break
    return hits


def classify_obligation(row_or_line: Mapping[str, Any], ctx: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The party rule for a row or BOQ line against a run's facts."""
    facts = [Fact.from_dict(f) for f in (ctx or {}).get("facts") or []]
    return attr.classify_obligation(row_or_line, facts=facts, splits=(ctx or {}).get("fact_splits"),
                                    ec_raceway_systems=ec_raceway_systems(ctx))


def ec_raceway_systems(ctx: Mapping[str, Any] | None) -> set[str]:
    """Systems whose raceway a document gives to the EC (an include
    obligation with split raceway_by_ec): their rough-in lines stay."""
    out: set[str] = set()
    for o in (ctx or {}).get("obligations") or []:
        if isinstance(o, Mapping) and o.get("carry") == "include" and o.get("split") == "raceway_by_ec":
            out.update(str(x) for x in o.get("systems") or [] if x not in attr.GENERIC_SYSTEMS)
    return out


# Enforce -------------------------------------------------------------------------------------


@dataclass
class _Report:
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    clarifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    exclusions: dict[str, dict[str, Any]] = field(default_factory=dict)
    needs_review: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)
    overrides_applied: dict[str, dict[str, Any]] = field(default_factory=dict)
    rfis_removed: dict[str, dict[str, Any]] = field(default_factory=dict)
    rfis_rewritten: dict[str, dict[str, Any]] = field(default_factory=dict)
    questions_resolved: dict[str, dict[str, Any]] = field(default_factory=dict)
    questions_rewritten: dict[str, dict[str, Any]] = field(default_factory=dict)
    excluded_allowances: dict[str, dict[str, Any]] = field(default_factory=dict)
    reconciled_marks: dict[str, dict[str, Any]] = field(default_factory=dict)
    added: dict[str, dict[str, Any]] = field(default_factory=dict)
    topics: set[str] = field(default_factory=set)

    @classmethod
    def from_prior(cls, prior: Mapping[str, Any] | None) -> _Report:
        rep = cls()
        if not isinstance(prior, Mapping):
            return rep
        for name in ("decisions", "clarifications", "exclusions", "needs_review", "overrides_applied", "rfis_removed",
                     "rfis_rewritten", "questions_resolved", "questions_rewritten", "excluded_allowances",
                     "reconciled_marks", "added"):
            target = getattr(rep, name)
            for e in prior.get("quantity_added" if name == "added" else name) or []:
                if isinstance(e, Mapping) and e.get("key"):
                    target[str(e["key"])] = dict(e)
        for w in prior.get("warnings") or []:
            if isinstance(w, Mapping) and w.get("key"):
                rep.warnings[str(w["key"])] = str(w.get("text") or "")
        rep.topics = set(prior.get("decided_topics") or [])
        return rep


def _norm(text: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).strip()


class _Enforcer:
    def __init__(self, main_output: Mapping[str, Any], ctx: Mapping[str, Any], policy: Mapping[str, Any],
                 overrides: Sequence[Mapping[str, Any]], settings: Any) -> None:
        self.out: dict[str, Any] = copy.deepcopy(dict(main_output))
        self.ctx = ctx or {}
        self.policy = policy
        self.settings = settings
        self.rep = _Report.from_prior(self.out.get("scope_policy"))
        self.excluded: dict[str, dict[str, Any]] = {
            str(e.get("line_id")): dict(e) for e in self.out.get("excluded_lines") or [] if isinstance(e, Mapping)
        }
        self.overrides = {str(o.get("decision_key")): dict(o) for o in overrides if o.get("decision_key")}
        self.facts = [Fact.from_dict(f) for f in self.ctx.get("facts") or []]
        self.splits = dict(self.ctx.get("fact_splits") or {})
        self.ec_raceway = ec_raceway_systems(self.ctx)
        self.inserted: list[dict[str, Any]] = []

    # helpers ----------------------------------------------------------------------------------

    def _boq(self) -> dict[str, Any]:
        boq = self.out.get("boq")
        if not isinstance(boq, dict):
            boq = {"areas": []}
            self.out["boq"] = boq
        return boq

    def _assign_line_ids(self) -> None:
        taken: set[str] = set(self.excluded)
        for _h, _g, _s, it in boq_items(self.out):
            lid = it.get("line_id")
            if isinstance(lid, str) and _is_line_id(lid):
                taken.add(lid)
        n = max([parse_row_id(t)[1] for t in taken if _is_line_id(t)] or [0])
        seen: set[str] = set()
        for _h, _g, _s, it in boq_items(self.out):
            lid = it.get("line_id")
            if not (isinstance(lid, str) and _is_line_id(lid)) or lid in seen:
                n += 1
                it["line_id"] = make_row_id("B", n)
            seen.add(it["line_id"])
        self._next = n

    def _new_line_id(self) -> str:
        self._next += 1
        return make_row_id("B", self._next)

    def _decide(self, key: str, rule_id: str | None, action: str, subject: str, *, before: Any = None,
                after: Any = None, evidence: Any = None, clarification: str | None = None,
                topics: Iterable[str] = ()) -> None:
        topics = list(topics)
        self.rep.decisions[key] = {
            "key": key, "rule_id": rule_id, "action": action, "subject": subject, "before": before, "after": after,
            "evidence": evidence or [], "clarification": clarification, "topics": topics,
        }
        self.rep.topics.update(topics)

    def _overridden(self, key: str, action: str) -> bool:
        ov = self.overrides.get(key)
        if ov and ov.get("action") == action:
            self.rep.overrides_applied[key] = {"key": key, "action": action, "note": ov.get("note"),
                                               "created_by": ov.get("created_by")}
            return True
        return False

    def _clarify(self, key: str, text: str, cite: str | None = None) -> None:
        self.rep.clarifications[key] = {"key": key, "text": text, "cite": cite}

    def _exclusion(self, key: str, text: str, cite: str | None = None) -> None:
        self.rep.exclusions[key] = {"key": key, "text": text, "cite": cite}

    def _review(self, key: str, code: str, detail: str, cite: str | None = None) -> None:
        if self.overrides.get(key, {}).get("action") == "accept":
            self._overridden(key, "accept")
            return
        self.rep.needs_review[key] = {"key": key, "code": code, "detail": detail, "cite": cite}

    def _locate(self, item: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]] | None:
        for area in self._boq().get("areas") or []:
            for sec in area.get("sections") or []:
                for grp in sec.get("groups") or []:
                    for sub in grp.get("subgroups") or []:
                        items = sub.get("items") or []
                        for it in items:
                            if it is item:
                                return items, {"area": area.get("area"), "section": sec.get("heading"),
                                               "group": grp.get("name"), "subgroup": sub.get("name")}
        return None

    def _exclude_item(self, item: dict[str, Any], *, key: str, reason: str, source: str, rule_id: str | None = None,
                      fact_id: str | None = None, party: str | None = None, system: str | None = None,
                      note: str | None = None) -> bool:
        if devices.is_exempt(item) or self._overridden(key, "include"):
            return False
        loc = self._locate(item)
        if loc is None:
            return False
        items, where = loc
        lid = str(item.get("line_id"))
        try:
            qty = float(item.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        line = ExcludedLine(line_id=lid, description=str(item.get("description") or ""), quantity=max(0.0, qty),
                            unit=str(item.get("unit") or "EA"), system=system, reason=reason[:300],
                            source=source if source in ("fact", "policy", "human") else "policy",
                            party=party, fact_id=fact_id, rule_id=rule_id, note=note)
        rec = line.to_dict()
        rec.update({"decision_key": key, "where": where, "item": copy.deepcopy(item)})
        self.excluded[lid] = rec
        items.remove(item)
        return True

    def _subgroup(self, section: str, group: str, subgroup: str, *, create: bool = True) -> list[dict[str, Any]] | None:
        boq = self._boq()
        areas = boq.setdefault("areas", [])
        if not areas:
            if not create:
                return None
            areas.append({"area": "AREA 1", "sections": []})
        area = areas[0]
        secs = area.setdefault("sections", [])
        sec = next((s for s in secs if str(s.get("heading") or "").upper() == section.upper()), None)
        if sec is None:
            if not create:
                return None
            sec = {"heading": section, "groups": []}
            secs.append(sec)
        grp = next((g for g in sec.setdefault("groups", []) if str(g.get("name") or "").upper() == group.upper()), None)
        if grp is None:
            if not create:
                return None
            grp = {"name": group, "subgroups": []}
            sec["groups"].append(grp)
        sub = next((s for s in grp.setdefault("subgroups", []) if str(s.get("name") or "").upper() == subgroup.upper()), None)
        if sub is None:
            if not create:
                return None
            sub = {"name": subgroup, "items": []}
            grp["subgroups"].append(sub)
        return sub.setdefault("items", [])

    def _insert(self, section: str, group: str, subgroup: str, item: dict[str, Any]) -> dict[str, Any]:
        items = self._subgroup(section, group, subgroup)
        assert items is not None
        item.setdefault("line_id", self._new_line_id())
        items.append(item)
        self.inserted.append({"line_id": item["line_id"], "quantity": item.get("quantity"), "unit": item.get("unit")})
        return item

    def _items(self, *, section: str | None = None) -> list[tuple[str, str, str, dict[str, Any]]]:
        out = []
        for h, g, s, it in boq_items(self.out):
            if section == "DEMOLITION" and not h.upper().startswith("DEMO"):
                continue
            if section == "NEW WORK" and h.upper().startswith("DEMO"):
                continue
            out.append((h, g, s, it))
        return out

    # steps ------------------------------------------------------------------------------------

    def bid_conditions(self) -> None:
        bid = self.ctx.get("bid_conditions") or {}
        wage = bid.get("wage_type")
        if wage not in ("prevailing_wage", "non_prevailing_wage"):
            return
        cue = next((c for c in bid.get("cues") or [] if c.get("weight", 0) > 0 and c.get("cue") in bidc.STATUTE_CUES), None)
        cite = None
        if cue:
            name = self._doc_name(cue.get("document_id"))
            cite = f"{name} p.{cue.get('page')}" if cue.get("page") else name
        label = "Prevailing wage" if wage == "prevailing_wage" else "Non-prevailing wage"
        why = {"project_record": "project record", "document_cue": "documents", "owner_type": "owner type",
               "owner_lexicon": "public owner", "default": "G3 default"}.get(str(bid.get("wage_basis")), "documents")
        text = f"{label} labor per the {why}" + (f" ({cite}: \"{str(cue.get('quote') or '')[:120]}\")." if cue else ".")
        self._decide("bid_conditions:wage_type", None, "set", "wage_type", after=wage,
                     evidence=[{"cue": cue.get("cue"), "cite": cite, "quote": cue.get("quote")}] if cue else [],
                     clarification=text, topics=["wage"])
        self._clarify("bid_conditions:wage_type", text, cite)
        for code in bid.get("needs_review") or []:
            self._review(f"bid_conditions:{code}", code, f"Bid conditions: {code.replace('_', ' ')}.")

    def _doc_name(self, doc_id: Any) -> str:
        for d in self.ctx.get("documents") or []:
            if str(d.get("document_id")) == str(doc_id):
                return str(d.get("name") or "document")
        return "document"

    def breakers(self) -> None:
        plan = breakers.BreakerPlan.from_dict(self.ctx.get("breaker_plan"))
        if plan.status == "unavailable":
            return
        key = "breakers:plan"
        if self._overridden(key, "include"):
            return
        rows = [(h, g, s, it) for h, g, s, it in self._items(section="NEW WORK")
                if breakers.is_breaker_row(str(it.get("description") or ""), group=g, subgroup=s)
                and not devices.is_exempt(it)]
        keep_unresolved = [it for *_x, it in rows if plan.unresolved_circuits and breakers.mentions_circuits(
            f"{it.get('description')} {it.get('material_note') or ''}", plan.unresolved_circuits)]
        target_sub = None
        for h, g, s, it in rows:
            target_sub = (h, g, s)
            break
        # Lowering the model's breakers needs positive evidence: existing
        # positions with new loads and no key (the reuse count). Without it
        # a model row the plan does not explain (a feeder breaker, a panel
        # the parser missed) stays for review, and a matched row is raised
        # to the plan but never lowered. Earlier reductions count against
        # the budget, so a second enforcement changes nothing.
        used = sum(_f(e.get("quantity")) for e in self.excluded.values() if e.get("rule_id") == "BRK-REUSE")
        budget = max(0.0, float(plan.reuse_count) - used)
        matched: dict[int, Any] = {}
        pending: list[tuple[dict[str, Any], Any]] = []
        for ln in plan.lines:
            if ln.amps is None or ln.poles is None:
                self.rep.warnings[f"breakers:unrated:{ln.description}"] = (
                    f"{ln.quantity} new panel positions without a trip or pole count were not turned into a breaker "
                    "line; the model's rows stay.")
                continue
            want = _breaker_key(ln.description)
            row = next((it for *_x, it in rows if id(it) not in matched and it not in keep_unresolved
                        and _breaker_key(str(it.get("description") or "")) == want), None)
            if row is None:
                sec, grp, sub = target_sub or ("NEW WORK", "SWITCHGEAR", "CIRCUIT BREAKERS")
                row = self._insert(sec, grp, sub, {
                    "description": ln.description, "quantity": ln.quantity, "unit": "EA", "caveat": None,
                    "sheet_refs": [ln.sheet] if ln.sheet else [], "is_accessory": False, "wastage_applies": False,
                    "material_category": "Switchgear", "scope_rule": "BRK-PLAN",
                    "material_note": f"New per keynoted panel positions {', '.join(ln.circuits)}.",
                    **self._policy_row_source(ln.description, float(ln.quantity)),
                })
                self._added(f"breakers:insert:{row['line_id']}", row, float(ln.quantity), "BRK-PLAN", "insert")
                matched[id(row)] = ln
            else:
                matched[id(row)] = ln
                pending.append((row, ln))
        unplanned: list[str] = []
        stale_refs = self._stale_policy_refs(plan)
        for *_x, it in rows:
            if id(it) in matched or it in keep_unresolved:
                continue
            lid = str(it.get("line_id"))
            qty = _f(it.get("quantity"))
            refs = [str(r.get("ref")) for r in it.get("source_refs") or [] if isinstance(r, Mapping)]
            if refs and all(r in stale_refs for r in refs):
                # a row the consolidator materialized from an earlier breaker plan that the current
                # plan no longer carries (run 785ae29e: "CIRCUIT BREAKER" 4 EA from the WS1 panel
                # "AND" that was general-note text)
                self._exclude_item(it, key=f"breakers:stale:{lid}", source="policy", rule_id="BRK-PLAN",
                                   reason="Breaker row from an earlier panel reading that the current panel "
                                          "schedules do not carry.")
                continue
            if qty > budget:
                unplanned.append(lid)
                continue
            reason = (f"Breaker reuse assumed: existing panel positions with new loads and no new-breaker key "
                      f"({plan.reuse_count} positions).")
            if self._exclude_item(it, key=f"breakers:line:{lid}", reason=reason, source="policy", rule_id="BRK-REUSE"):
                budget -= qty
        for row, ln in pending:
            budget = self._set_breaker_quantity(row, ln, plan, budget)
            if _f(row.get("quantity")) > float(ln.quantity):
                unplanned.append(str(row.get("line_id")))
        if unplanned:
            self._review("breakers:unplanned", "breaker_unplanned",
                         f"Breaker rows {', '.join(sorted(unplanned, key=_line_sort))} carry more than the panel "
                         "schedules' new and keyed positions and no reuse evidence covers the difference; kept.",
                         ", ".join(plan.sheets) or None)
        self._panel_lines(plan)
        text = plan.clarification() or ""
        self._decide(key, "BRK-PLAN", "set", "branch breakers", after=[ln.to_dict() for ln in plan.lines],
                     evidence=[{"cite": ", ".join(plan.sheets), "keynotes": plan.keynotes}], clarification=text,
                     topics=["breaker_reuse"] if plan.reuse_circuits else [])
        if text:
            self._clarify(key, text, ", ".join(plan.sheets) or None)
        if plan.reuse_circuits:
            self._review("breakers:reuse", breakers.REUSE_REVIEW,
                         f"{plan.reuse_count} existing positions with new loads assumed reused.", ", ".join(plan.sheets))
        for w in plan.warnings:
            self.rep.warnings[f"breakers:{w[:40]}"] = w

    def _stale_policy_refs(self, plan: breakers.BreakerPlan) -> set[str]:
        """Row ids of the consolidator's breaker policy rows (origin
        scope_breakers) whose breaker key and quantity no current plan line
        has."""
        cons = self.out.get("consolidator") if isinstance(self.out.get("consolidator"), Mapping) else {}
        current = {(_breaker_key(ln.description), float(ln.quantity)) for ln in plan.lines}
        out: set[str] = set()
        for row in cons.get("policy_rows") or []:
            if not isinstance(row, Mapping) or not row.get("row_id"):
                continue
            if str((row.get("origin") or {}).get("source") or "") != "scope_breakers":
                continue
            if (row.get("line_hint") or {}).get("kind") == "panel":
                continue
            if (_breaker_key(str(row.get("description") or "")), _f(row.get("quantity"))) not in current:
                out.add(str(row["row_id"]))
        return out

    def _panel_lines(self, plan: breakers.BreakerPlan) -> None:
        """Insert the panel line of each new panel the plan carries when no BOQ
        row names that panel (run 785ae29e: panels A and B were not lines at
        all). An existing panel row is left as the model wrote it."""
        for pl in plan.panel_lines:
            key = f"breakers:panel:{pl.name}"
            if self._overridden(key, "exclude"):
                continue
            if any(breakers.names_panel(str(it.get("description") or ""), pl.name)
                   for *_x, it in self._items(section="NEW WORK")):
                continue
            row = self._insert("NEW WORK", "SWITCHGEAR", "PANELS", {
                "description": pl.description, "quantity": 1.0, "unit": "EA", "caveat": None,
                "sheet_refs": [pl.sheet] if pl.sheet else [], "is_accessory": False, "wastage_applies": False,
                "material_category": "Switchgear", "scope_rule": "BRK-PLAN",
                "material_note": f"New panel per its schedule{' (' + pl.ref + ')' if pl.ref else ''}.",
                **self._policy_row_source(pl.description, 1.0, panel=True),
            })
            self._added(f"{key}:insert", row, 1.0, "BRK-PLAN", "insert")
            self._decide(key, "BRK-PLAN", "insert", pl.description, after=1,
                         evidence=[{"cite": pl.sheet, "ref": pl.ref, "header": pl.header}])

    def _policy_row_source(self, description: str, quantity: float, *, panel: bool = False) -> dict[str, Any]:
        """`source_refs`, `combine` and `basis` of a breaker line the plan
        inserts: the consolidator's policy row (an M row with basis policy
        materialized from the same breaker plan, `consolidator.policy_rows`
        of the main output) with the same breaker key and quantity, so the
        line reproduces from a row of the run. Nothing in legacy mode."""
        cons = self.out.get("consolidator") if isinstance(self.out.get("consolidator"), Mapping) else {}
        want = _breaker_key(description)
        for row in cons.get("policy_rows") or []:
            if not isinstance(row, Mapping) or not row.get("row_id"):
                continue
            if panel:
                # a panel policy row matches on its full canonical description
                if str(row.get("description") or "").strip().upper() != description.strip().upper():
                    continue
            elif _breaker_key(str(row.get("description") or "")) != want or (
                    (row.get("line_hint") or {}).get("kind") == "panel"):
                continue
            if abs(_f(row.get("quantity")) - float(quantity)) > 1e-6:
                continue
            return {"source_refs": [{"ref": str(row["row_id"]), "multiplier": 1.0}], "combine": "single",
                    "basis": "policy"}
        return {}

    def _added(self, key: str, item: Mapping[str, Any], quantity: float, rule_id: str, kind: str) -> None:
        """Record quantity the engine put into the BOQ (an insert or an
        increase), so conservation is checkable from the output: BOQ before
        = BOQ after + excluded_lines - quantity_added."""
        if quantity <= 0:
            return
        self.rep.added[key] = {"key": key, "line_id": item.get("line_id"), "description": item.get("description"),
                               "quantity": _q(quantity), "unit": str(item.get("unit") or "EA"), "rule_id": rule_id,
                               "kind": kind}

    def _set_breaker_quantity(self, row: dict[str, Any], ln: Any, plan: breakers.BreakerPlan, budget: float) -> float:
        """Set a matched model breaker row to the plan quantity and return the
        remaining reduction budget. A decrease (at most `budget`, the reuse
        evidence left) is recorded as an excluded_lines entry for the
        difference (reversible with an include override on its key); an
        increase is recorded as added quantity. Nothing is overwritten
        silently."""
        lid = str(row.get("line_id"))
        key = f"breakers:line:{lid}:delta"
        row["description"] = ln.description
        row["caveat"] = None
        if self.overrides.get(key, {}).get("action") == "include":
            self._overridden(key, "include")
            return budget  # the human kept the model's quantity for this row
        old = _f(row.get("quantity"))
        new = float(ln.quantity)
        if new < old:
            new = max(new, old - budget)
            budget -= old - new
        row["quantity"] = _q(new)
        if new < old:
            delta_id = self._new_line_id()
            reason = (f"Breaker quantity set to the keyed new positions ({_q(new)} EA); the other {_q(old - new)} EA "
                      f"assumed reused" + (f" ({plan.reuse_count} existing positions with new loads)."
                                           if plan.reuse_circuits else "."))
            line = ExcludedLine(line_id=delta_id, description=ln.description, quantity=old - new,
                                unit=str(row.get("unit") or "EA"), system=None, reason=reason, source="policy",
                                rule_id="BRK-REUSE")
            rec = line.to_dict()
            loc = self._locate(row)
            rec.update({"decision_key": key, "delta_of": lid, "where": loc[1] if loc else {},
                        "item": {**copy.deepcopy(row), "quantity": _q(old - new), "line_id": delta_id}})
            self.excluded[delta_id] = rec
            self._decide(key, "BRK-REUSE", "reduce", ln.description, before=_q(old), after=_q(new))
        elif new > old:
            self._added(f"breakers:line:{lid}:increase", row, new - old, "BRK-PLAN", "increase")
            self._decide(key, "BRK-PLAN", "increase", ln.description, before=_q(old), after=_q(new))
        return budget

    def devices(self) -> None:
        boq = self._boq()
        if not self._overridden("devices:merge", "include"):
            # The merged line keeps every member's provenance (union of
            # source_refs, combine sum), so its quantity reproduces from its
            # rows and no merged row becomes an orphan (run ad69e5a4: B36 C33
            # folded into B35 C32 left B35 at 35 EA computed 26 EA).
            for m in devices.merge_receptacles(boq, merge_sources=_merge_line_sources):
                key = f"devices:merge:{_norm(m['description'])}"
                self._decide(key, "DEV-MERGE", "merge", m["description"], before=m["merged"], after=m["quantity"],
                             clarification=None)
        dev = devices.DeviceFacts.from_dict(self.ctx.get("device_facts"))
        if dev.tr_count is not None and dev.tr_count <= 0:
            # A zero census is unknown (spec 5.1); a stored 0 never moves
            # quantity out of the model's TR line.
            self.rep.warnings["devices:tr_unknown"] = "TR count unknown (legend symbol census found no hit); TR line kept."
        elif dev.tr_count is not None and not self._overridden("devices:tr", "include"):
            res = devices.apply_tr(boq, dev.tr_count, cite=dev.tr_cite, tr_pages=dev.tr_pages,
                                   new_line_id=self._new_line_id)
            if res:
                self._decide("devices:tr", "DEV-TR", "split", devices.TR_DESCRIPTION, after=res,
                             evidence=[{"cite": dev.tr_cite, "method": dev.tr_method, "pages": dict(dev.tr_pages)}])
                if res.get("applied") and res.get("reduced") is False:
                    self._clarify("devices:tr", f"{devices.TR_DESCRIPTION} {res.get('tr_quantity')} EA added from the "
                                  f"legend symbol count ({dev.tr_cite}); the duplex line cites other sheets and was "
                                  "not reduced.")
                elif not res.get("applied"):
                    self.rep.warnings["devices:tr"] = (f"TR count {dev.tr_count} ({dev.tr_cite}) not applied: "
                                                       f"{res.get('reason')}.")
            else:
                self._repair_tr_split()
        if dev.tr_room_rule:
            self._clarify("devices:tr_room_rule", dev.tr_room_rule)
        if dev.grade_notes:
            devices.apply_grade_notes(boq, dev.grade_notes)
            self._decide("devices:grade", "DEV-GRADE", "note", "receptacle grade", after=dev.grade_notes,
                         evidence=dev.spec_facts)
        for _h, _g, _s, it in self._items(section="NEW WORK"):
            if it.get("scope_rule") == "DEV-TR" and not it.get("line_id"):
                it["line_id"] = self._new_line_id()

    def _repair_tr_split(self) -> None:
        """An output enforced before the split markers existed (run 2492ba7a:
        B41 "DUPLEX RECEPTACLE, TR" with no refs, B8 reduced by 3) is
        re-enforced with nothing to move (`apply_tr` is idempotent), so the
        markers are rebuilt from the recorded decision: the TR line the rule
        created (`scope_rule` DEV-TR, no `split_of`) cites the plain line of
        `devices:tr.after.from_line` with the quantity the decision moved."""
        prior = (self.rep.decisions.get("devices:tr") or {}).get("after")
        if not isinstance(prior, Mapping) or not prior.get("applied") or not prior.get("reduced"):
            return
        parent_id = str(prior.get("from_line") or "")
        moved = _f(prior.get("moved") if prior.get("moved") is not None else prior.get("tr_quantity"))
        if not parent_id or moved <= 0:
            return
        lines = {str(it.get("line_id")): it for _h, _g, _s, it in self._items(section="NEW WORK") if it.get("line_id")}
        parent = lines.get(parent_id)
        child = next((it for it in lines.values() if it.get("scope_rule") == "DEV-TR" and not it.get("split_of")
                      and devices.TR_RE.search(str(it.get("description") or ""))), None)
        if parent is None or child is None or _f(child.get("quantity")) < moved:
            return
        split = devices.mark_split(parent, child, moved, new_line_id=self._new_line_id)
        if split:
            self.rep.decisions["devices:tr"] = {**self.rep.decisions["devices:tr"], "after": {**prior, "split": split}}

    def inclusions(self) -> None:
        incs = list(self.ctx.get("inclusions") or [])
        for inc in incs:
            rid = str(inc.get("rule_id"))
            key = f"inclusion:{rid}"
            if self._overridden(key, "exclude"):
                continue
            family = str(inc.get("trigger_family") or "")
            pattern = scope_policy.compile_pattern(str(inc.get("pattern") or r"\bx\b"))
            also = scope_policy.compile_pattern(str(inc["also_pattern"])) if inc.get("also_pattern") else None
            if inc.get("companion_of"):
                fam_re = CONTROL_CABLE_RE
                rows = [(h, g, s, it) for h, g, s, it in self._items(section="NEW WORK")
                        if fam_re.search(str(it.get("description") or "")) and not devices.is_exempt(it)]
            elif family == "thermostat":
                rows = [(h, g, s, it) for h, g, s, it in self._items()
                        if THERMOSTAT_FAMILY_RE.search(str(it.get("description") or ""))
                        and (also is None or also.search(str(it.get("description") or "")))
                        and not devices.is_exempt(it)]
            else:
                canon = _norm(inc.get("description"))
                # A connection row worded as a RECONNECTION (7154 run b87f0ffd
                # B18) is the same line: the also_pattern reads it folded.
                fold = devices.connection_text if family == "equipment_connection" else str
                rows = [(h, g, s, it) for h, g, s, it in self._items(section="NEW WORK")
                        if ((pattern.search(str(it.get("description") or ""))
                             and (also is None or also.search(fold(str(it.get("description") or "")))))
                            or _norm(it.get("description")) == canon)
                        and not devices.is_exempt(it) and it.get("unit", "EA") == inc.get("unit", "EA")]
            desc = str(inc.get("description") or "")
            if rows:
                new_rows = [r for r in rows if not r[0].upper().startswith("DEMO")]
                demo_rows = [r for r in rows if r[0].upper().startswith("DEMO")]
                keep = (new_rows or demo_rows)[0][3]
                before = [{"line_id": r[3].get("line_id"), "description": r[3].get("description"),
                           "quantity": r[3].get("quantity")} for r in rows]
                if family == "thermostat" and new_rows and demo_rows:
                    for r in demo_rows:
                        self._exclude_item(r[3], key=f"{key}:dup:{r[3].get('line_id')}", source="policy", rule_id=rid,
                                           reason="Thermostat relocation counted once: the new-work sheet wins over "
                                                  "the demolition sheet.")
                    merge_set = new_rows
                else:
                    merge_set = new_rows or demo_rows
                total = sum(_f(r[3].get("quantity")) for r in merge_set)
                for r in merge_set[1:]:
                    loc = self._locate(r[3])
                    if loc:
                        loc[0].remove(r[3])
                _merge_line_sources(keep, [r[3] for r in merge_set])
                if (new_rows or demo_rows) is demo_rows and family == "thermostat":
                    loc = self._locate(keep)
                    if loc:
                        loc[0].remove(keep)
                    keep = self._insert(str(inc.get("section") or "NEW WORK"), str(inc.get("group")),
                                        str(inc.get("subgroup")), keep)
                    self.inserted.pop()
                changed = str(keep.get("description") or "") != desc or len(merge_set) > 1
                keep["description"] = desc
                keep["quantity"] = _q(total)
                from_triggers = None
                if family != "thermostat" and not inc.get("companion_of"):
                    from_triggers = _trigger_quantity_wins(keep, inc, total)
                if from_triggers is not None:
                    keep["quantity"] = _q(from_triggers["quantity"])
                    keep["source_refs"] = from_triggers["source_refs"]
                    keep["combine"] = from_triggers["combine"]
                    if from_triggers["sheet_refs"] or "sheet_refs" in keep:
                        keep["sheet_refs"] = from_triggers["sheet_refs"]
                    keep["basis"] = "counted"
                    changed = True
                if len(merge_set) == 1 and keep.get("scope_rule") == rid and not keep.get("source_refs"):
                    # The line this rule inserted on an earlier enforce (run
                    # 2492ba7a: B42 from the page facts) carried no refs; a
                    # context whose triggers now carry refs sources it.
                    keep.update(_trigger_sources(inc))
                if changed:
                    self._decide(key, rid, "canonicalize", desc, before=before, after=keep["quantity"],
                                 clarification=inc.get("clarification"),
                                 evidence=[from_triggers["evidence"]] if from_triggers is not None else None,
                                 topics=["thermostats"] if family == "thermostat" else [])
                elif key not in self.rep.decisions:
                    self._decide(key, rid, "keep", desc, after=keep["quantity"], clarification=inc.get("clarification"),
                                 topics=["thermostats"] if family == "thermostat" else [])
            else:
                qty = inc.get("quantity") or 0
                if not qty:
                    continue
                item = self._insert(str(inc.get("section") or "NEW WORK"), str(inc.get("group")), str(inc.get("subgroup")), {
                    "description": desc, "quantity": qty, "unit": inc.get("unit") or "EA", "caveat": inc.get("caveat"),
                    "sheet_refs": sorted({t.get("sheet") for t in inc.get("triggers") or [] if t.get("sheet") and t.get("used", True)}),
                    "is_accessory": False, "wastage_applies": bool(inc.get("wastage_applies")),
                    "material_category": inc.get("material_category"), "scope_rule": rid,
                    "basis": "policy" if inc.get("basis") != "measured" else "measured",
                    "material_note": _trigger_note(inc),
                    **_trigger_sources(inc),
                })
                self._added(f"{key}:insert", item, _f(item.get("quantity")), rid, "insert")
                self._decide(key, rid, "insert", desc, after=item["quantity"], clarification=inc.get("clarification"),
                             evidence=[{"triggers": inc.get("triggers")}],
                             topics=["thermostats"] if family in ("thermostat",) else [])
            if inc.get("clarification"):
                self._clarify(key, str(inc["clarification"]))

    def rule_exclusions(self) -> None:
        rules = [r for r in self.policy.get("rules") or [] if scope_policy.rule_enabled(r)
                 and r.get("decision") in ("exclude", "by_others")]
        statements_include = [o for o in self.ctx.get("obligations") or [] if o.get("carry") == "include"]
        amounts = bool(self.ctx.get("document_amounts"))
        for h, g, s, it in self._items():
            if devices.is_exempt(it) or it.get("scope_rule"):
                continue
            text = _rule_head(str(it.get("description") or ""))
            install_line = bool(INSTALL_LINE_RE.search(text))
            for r in rules:
                if r.get("decision") == "by_others" and install_line:
                    continue  # equipment by others: the installation line stays
                m = scope_policy.match_rule(r, text, subgroup=s, group=g)
                if not m:
                    continue
                if r["kind"] == "soft" and _unless_holds(r, it, statements_include, amounts):
                    self._decide(f"rule:{r['rule_id']}:{it.get('line_id')}", r["rule_id"], "keep", text,
                                 clarification=f"Kept: {r['label'] if 'label' in r else r['rule_id']} exception holds.")
                    break
                key = f"rule:{r['rule_id']}:{it.get('line_id')}"
                if self._exclude_item(it, key=key, reason=str(r["default_text"]), source="policy", rule_id=r["rule_id"]):
                    self._decide(key, r["rule_id"], "exclude", text, before=it.get("quantity"),
                                 clarification=r.get("proposal_line"), topics=[r.get("topic", "")])
                    self._exclusion(f"rule:{r['rule_id']}", str(r.get("proposal_line") or r["default_text"]),
                                    f"p:{r['rule_id']}")
                break

    def fact_exclusions(self) -> None:
        if not self.facts:
            return
        includes = [r for r in self.policy.get("rules") or [] if scope_policy.rule_enabled(r)
                    and r.get("decision") == "include"]
        for _h, g, s, it in self._items():
            if devices.is_exempt(it) or it.get("scope_rule"):
                continue
            if any(scope_policy.match_rule(r, str(it.get("description") or ""), subgroup=s, group=g) for r in includes):
                continue  # an include rule (Q14 thermostats) beats a fact
            res = attr.classify_obligation(it, facts=self.facts, splits=self.splits, ec_raceway_systems=self.ec_raceway)
            fid = res.get("fact_id")
            fact = next((f for f in self.facts if f.id == fid), None)
            if res["carry"] == "exclude" and fact is not None:
                key = f"fact:{fid}:{it.get('line_id')}"
                if self._exclude_item(it, key=key, reason=res["reason"], source="fact", fact_id=fid,
                                      party=fact.party, system=fact.system,
                                      note=_excluded_note(fact)):
                    self._decide(key, None, "exclude", str(it.get("description")), before=it.get("quantity"),
                                 evidence=[fact.to_dict()], topics=[fact.system])
            if res.get("needs_review"):
                self._review(f"fact_review:{fid}:{it.get('line_id')}", str(res["needs_review"]),
                             f"{it.get('description')}: {res['reason']}", (fact.cite.section if fact else None))

    def allowances(self) -> None:
        est = self.out.get("estimate")
        if not isinstance(est, dict):
            return
        extras = list(est.get("extra_allowances") or [])
        if not extras:
            return
        rules = [r for r in self.policy.get("rules") or [] if scope_policy.rule_enabled(r)
                 and r.get("decision") in ("exclude", "by_others")]
        conf = self.policy.get("allowances") or {}
        require_amount = bool(conf.get("extra_requires_document_amount", True))
        amounts = self.ctx.get("document_amounts") or []
        keep: list[Any] = []
        for entry in extras:
            name = str(entry.get("name") if isinstance(entry, Mapping) else entry)
            key = f"allowance:{_norm(name)}"
            if self._overridden(key, "include"):
                keep.append(entry)
                continue
            rule = next((r for r in rules if scope_policy.match_rule(r, name)), None)
            systems = [x for x in attr.systems_in(name) if x not in attr.GENERIC_SYSTEMS]
            fact = self._best_fact(systems)
            if rule is not None and rule.get("kind") == "hard":
                text = f"{rule.get('proposal_line') or rule['default_text']} (removed allowance: {name})"
                self._exclusion(key, text, f"p:{rule['rule_id']}")
                reason = rule["rule_id"]
            elif _amount_for(name, amounts):
                keep.append(entry)  # a document names this allowance with an amount
                continue
            elif fact is not None:
                text = f"{_fact_sentence(fact, systems, self.facts)} (removed allowance: {name})"
                self._exclusion(key, text, fact.cite.section)
                reason = "fact"
            elif rule is not None:
                text = f"{rule.get('proposal_line') or rule['default_text']} (removed allowance: {name})"
                self._exclusion(key, text, f"p:{rule['rule_id']}")
                reason = rule["rule_id"]
            elif require_amount and not _amount_for(name, amounts):
                text = f"No allowance carried for {name}: no document names an amount (removed allowance: {name})."
                self._clarify(key, text)
                reason = "no_document_amount"
            else:
                keep.append(entry)
                continue
            self.rep.excluded_allowances[key] = {"key": key, "name": name, "reason": reason}
            self._decide(key, rule["rule_id"] if rule else None, "remove_allowance", name, before=name,
                         clarification=text, topics=[rule.get("topic")] if rule else systems)
        est["extra_allowances"] = keep

    def _best_fact(self, systems: Sequence[str]) -> Fact | None:
        ranks = {str(o.get("text") or "")[:300]: _source_rank(o) for o in self.ctx.get("obligations") or []}
        cands = [f for f in self.facts if f.system in systems and f.excludes]
        cands.sort(key=lambda f: (ranks.get(f.cite.quote, (9, 0.0)), f.id))
        return cands[0] if cands else None

    def obligation_exclusions(self) -> None:
        obs = sorted((o for o in self.ctx.get("obligations") or [] if o.get("carry") == "exclude"), key=_source_rank)
        facts_by_quote: dict[str, Fact] = {}
        for f in self.facts:
            facts_by_quote.setdefault(f.cite.quote, f)
        done: set[str] = set()
        for o in obs:
            role = str(o.get("role") or "")
            party = str(o.get("party") or "")
            if role not in attr.OWNER_DOC_ROLES - {"spec"} and party not in attr.EXPLICIT_THIRD_PARTIES:
                continue
            systems = [s for s in o.get("systems") or [] if s in EXCLUSION_SYSTEMS and s not in done]
            if not systems:
                continue
            fact = facts_by_quote.get(str(o.get("text") or "")[:300])
            if fact is not None and not fact.verified:
                continue
            key = "obligation:" + "+".join(systems)
            if self._overridden(key, "include"):
                continue
            who = o.get("party_name") if party == "owner_vendor" and o.get("party_name") not in (None, "owner vendor") \
                else PARTY_LABELS.get(party, "others")
            label = _join_labels([SYSTEM_LABELS.get(s, s.replace("_", " ")) for s in systems])
            cite = str(o.get("cite") or "")
            quote = str(o.get("text") or "")[:180]
            text = f"{label[0].upper()}{label[1:]} by {who} per {cite}: \"{quote}\""
            if o.get("drawn"):
                self._review(key, "fact_conflict", f"{label} is drawn on the electrical sheets and assigned to {who}.", cite)
            self._exclusion(key, text, cite)
            self._decide(key, None, "exclude", label, evidence=[{"cite": cite, "quote": quote, "party": who}],
                         topics=systems)
            done.update(systems)

    def standards(self) -> None:
        stds = self.ctx.get("standards") or {}
        race = stds.get("raceway")
        if race:
            key = "standard:raceway"
            self._decide(key, "STD-RACEWAY", "set", "branch raceway", after=race.get("value"),
                         evidence=race.get("mentions") or [], clarification=race.get("clarification"), topics=["raceway"])
            self._clarify(key, str(race.get("clarification") or ""), race.get("cite"))
            if race.get("needs_review"):
                self._review(key, str(race["needs_review"]), str(race.get("clarification") or ""), race.get("cite"))
        demo = stds.get("demo_disposition")
        demo_rows = [it for _h, _g, _s, it in self._items(section="DEMOLITION") if not it.get("is_accessory")]
        if demo and demo_rows:
            key = "demo_disposition"
            cite = demo.get("cite") or "G3 standard"
            for it in demo_rows:
                it["demo_disposition"] = {"value": demo.get("value"), "cite": cite, "decision_key": key}
            self._decide(key, "STD-DEMO", "set", "demolition disposition", after=demo.get("value"),
                         evidence=demo.get("mentions") or [{"cite": cite}], clarification=demo.get("clarification"),
                         topics=["demo_disposition"])
            self._clarify(key, str(demo.get("clarification") or ""), cite)
            if demo.get("needs_review"):
                self._review(key, str(demo["needs_review"]), str(demo.get("clarification") or ""), cite)
            if str(demo.get("value")) == "REMOVE_TO_SOURCE":
                self._demo_canonical(cite)
        for topic in ("branch_conduit", "emt_fittings", "conductor_min_size"):
            d = stds.get(topic)
            if d and d.get("clarification") and topic == "branch_conduit":
                self._clarify(f"standard:{topic}", str(d["clarification"]))

    def _demo_canonical(self, cite: str) -> None:
        """Canonical demolition removal rows under the remove-to-source
        disposition, area by area (rows of different areas never merge)."""
        for area in self._boq().get("areas") or []:
            rows = []
            for sec in area.get("sections") or []:
                if not str(sec.get("heading") or "").upper().startswith("DEMO"):
                    continue
                for grp in sec.get("groups") or []:
                    for sub in grp.get("subgroups") or []:
                        for it in sub.get("items") or []:
                            if isinstance(it, dict):
                                rows.append((str(sec.get("heading") or ""), str(grp.get("name") or ""),
                                             str(sub.get("name") or ""), it))
            self._demo_canonical_rows(rows, cite, str(area.get("area") or ""), len(self._boq().get("areas") or []))

    def _demo_canonical_rows(self, rows: list[tuple[str, str, str, dict[str, Any]]], cite: str, area: str,
                             n_areas: int) -> None:
        two_line = bool(getattr(self.settings, "plans_reader_demo_two_line_style", False)) if self.settings else False

        def _desc(r: tuple[str, str, str, dict[str, Any]]) -> str:
            return str(r[3].get("description") or "")

        rows = [r for r in rows if not r[3].get("is_accessory") and not devices.is_exempt(r[3])
                and str(r[3].get("unit") or "EA").upper() == "EA" and not TRACK_ROW_RE.search(_desc(r))]
        # Relocations, reconnections and items that stay are not removals;
        # they are never folded into a removal line.
        rows = [r for r in rows if not DEMO_NOT_REMOVAL_RE.search(_desc(r))]
        power = [r for r in rows if MAKE_SAFE_RE.search(_desc(r))
                 or (_demo_power_first(_desc(r)) and not DEMO_LOW_VOLTAGE_RE.search(_desc(r)))]
        lighting = [r for r in rows if r not in power and DEMO_LIGHTING_RE.search(_desc(r))]
        groups = []
        if two_line:
            # The house two-line style: exactly one power removal row and one
            # lighting removal row (E22 with the flag on).
            groups = [("power", DEMO_POWER_LINE, power), ("lighting", DEMO_LIGHTING_LINE, lighting)]
        else:
            # Without the two-line style the lighting rows stay itemized, but
            # demolition power is at most 2 rows (E22): the device removal row
            # (make-safe rows folded in) and, only when the BOQ has them, one
            # floor box / floor outlet assembly row. Totals are conserved.
            # Equipment removals (disconnect switches, panels, poles, units)
            # and rows that name no removal stay itemized. A BOQ that already
            # has at most 2 device removal rows and no make-safe row is left
            # as written.
            power = [r for r in power if MAKE_SAFE_RE.search(_desc(r))
                     or (not DEMO_EQUIPMENT_RE.search(_desc(r)) and DEMO_WORDS_RE.search(_desc(r)))]
            floor = [r for r in power if DEMO_FLOOR_RE.search(_desc(r)) and not MAKE_SAFE_RE.search(_desc(r))]
            devices_rows = [r for r in power if r not in floor]
            if len(power) > 2 or any(MAKE_SAFE_RE.search(_desc(r)) for r in power):
                groups = [("power", DEMO_POWER_LINE, devices_rows), ("power_floor", DEMO_FLOOR_LINE, floor)]
        suffix = f":{_norm(area)[:40]}" if n_areas > 1 else ""
        for name, line, members in groups:
            if not members:
                continue
            if len(members) == 1 and str(members[0][3].get("description") or "") == line:
                continue
            head = members[0][3]
            before = [{"line_id": m[3].get("line_id"), "description": m[3].get("description"),
                       "quantity": m[3].get("quantity")} for m in members]
            total = sum(_f(m[3].get("quantity")) for m in members)
            for m in members[1:]:
                loc = self._locate(m[3])
                if loc:
                    loc[0].remove(m[3])
            _merge_line_sources(head, [m[3] for m in members])
            head["description"] = line
            head["quantity"] = _q(total)
            head["caveat"] = None
            head["material_note"] = f"Removal back to source per {cite}; combines {len(members)} demolition rows."
            self._decide(f"demo_canonical:{name}{suffix}", "STD-DEMO", "canonicalize", line, before=before,
                         after=head["quantity"])

    def rfis(self) -> None:
        """Brief RFIs on topics the G3 decisions decide are dropped, and
        compound ones lose their decided clauses, with the same machinery as
        the reader questions (`questions`): the policy topics (raceway even
        while `raceway_conflict` stands for review: the clarification and the
        needs_review chip carry the conflict; demolition disposition; the
        fire alarm and access control exclusions; ...) and the luminaire
        schedule (a tag whose CONTROL cell reads). Each removed or rewritten
        RFI records the deciding decision keys and clarification."""
        brief = self.out.get("brief")
        if not isinstance(brief, dict):
            return
        decided = dict(self._decided_topic_patterns())
        by_schedule, schedule_entries = self._schedule_decided()
        decided.update(by_schedule)
        kept = []
        for i, rfi in enumerate(brief.get("rfis") or []):
            q = str(rfi.get("question") if isinstance(rfi, Mapping) else rfi)
            new_q, removed = rewrite_question(q, decided)
            key = f"rfi:{_norm(q)[:80]}"
            if self._overridden(key, "include"):
                kept.append(rfi)
                continue
            if new_q is None or new_q != q:
                clarification, keys = self._resolution(removed, schedule_entries)
                source = rfi.get("source") if isinstance(rfi, Mapping) else None
            if new_q is None:
                self.rep.rfis_removed[key] = {"key": key, "question": q, "topics": removed, "index": i, "source": source,
                                              "decision_keys": keys, "clarification": clarification}
                continue
            if new_q != q:
                self.rep.rfis_rewritten[key] = {"key": key, "before": q, "after": new_q, "topics": removed,
                                                "source": source, "decision_keys": keys, "clarification": clarification}
                if isinstance(rfi, Mapping):
                    rfi = {**rfi, "question": new_q}
                else:
                    rfi = new_q
            kept.append(rfi)
        brief["rfis"] = kept

    def _resolution(self, removed: Sequence[str], schedule_entries: Mapping[str, Mapping[str, Any]]) \
            -> tuple[str | None, list[str]]:
        """(clarification, decision keys) that answer the removed topics: the
        policy decision's clarification, then one sentence per schedule-decided
        luminaire tag ("<tag> control <cell> per the luminaire schedule")."""
        policy_topics = [t for t in removed if t not in schedule_entries]
        clarification, keys = self._clarification_for_topics(policy_topics) if policy_topics else (None, [])
        texts = [clarification] if clarification else []
        for t in removed:
            if t in schedule_entries:
                texts.append(self._schedule_clarification(schedule_entries[t]))
                keys = [*keys, f"luminaire_schedule:{schedule_entries[t].get('ref') or t}"]
        return ("; ".join(texts) or None), list(keys)

    def _decided_topic_patterns(self) -> dict[str, tuple[str, ...]]:
        return decided_topic_patterns(self.rep.topics)

    def _clarification_for_topics(self, topics: Iterable[str]) -> tuple[str | None, list[str]]:
        """(clarification text, decision keys) of the decisions that decided
        these topics: the standard, bid condition, fact or rule that answers
        the question."""
        want = set(topics)
        # Deterministic (a report reloaded from a prior enforce holds its
        # decisions sorted by key): the standard, condition, obligation or
        # fact that decided the topic before an allowance or rule drop.
        order = ("standard:", "demo_disposition", "bid_conditions:", "inclusion:", "obligation:", "fact:", "rule:",
                 "allowance:")
        keys = sorted((k for k, d in self.rep.decisions.items() if want & set(d.get("topics") or [])),
                      key=lambda k: (next((i for i, p in enumerate(order) if k.startswith(p)), len(order)), k))
        for k in keys:
            text = self.rep.decisions[k].get("clarification") or (self.rep.clarifications.get(k) or {}).get("text") \
                or (self.rep.exclusions.get(k) or {}).get("text")
            if text:
                return str(text), keys
        return None, keys

    def _schedule_decided(self) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, Any]]]:
        """Topics the run's luminaire schedule decides: one per tag whose
        CONTROL cell is readable (`luminaire_control:<tag>`), matched by the
        clause pattern plans_reader_acceptance._L4_CONTROL_RE uses (the tag
        within 60 characters of CONTROL, either order), with the schedule
        entry. A tag whose cell is unreadable decides nothing."""
        patterns: dict[str, tuple[str, ...]] = {}
        entries: dict[str, dict[str, Any]] = {}
        for e in self.ctx.get("luminaire_schedule") or []:
            if not isinstance(e, Mapping) or not e.get("tag") or not e.get("control"):
                continue
            tag = str(e["tag"]).strip().upper()
            topic = f"{LUMINAIRE_CONTROL_TOPIC}:{tag}"
            if topic in patterns:
                continue
            t = re.escape(tag)
            patterns[topic] = (rf"(?s)\b{t}\b.{{0,60}}\bCONTROL|\bCONTROL.{{0,60}}\b{t}\b",)
            entries[topic] = dict(e)
        return patterns, entries

    @staticmethod
    def _schedule_clarification(entry: Mapping[str, Any]) -> str:
        sheet = str(entry.get("sheet") or "").strip()
        where = f" ({sheet})" if sheet else ""
        return f"{entry.get('tag')} control {entry.get('control')} per the luminaire schedule{where}"

    def questions(self) -> None:
        """Reader questions of the category outputs whose topics the G3
        decisions decide (E20: "retained and capped or removed to source"
        after the demolition disposition; E6: the raceway question after
        the raceway standard), or the luminaire schedule decides (E19: a
        question, or a clause of a compound question, about a tag's control
        when the schedule's CONTROL cell for that tag is readable; resolved
        with "<tag> control <cell> per the luminaire schedule (<sheet>)").
        Category rows are immutable, so the report records each resolved
        question with the deciding decision and its clarification, and the
        partially decided ones with their rewrite; a human override
        `question:<category>:<row id>` include keeps one open."""
        decided = dict(self._decided_topic_patterns())
        by_schedule, schedule_entries = self._schedule_decided()
        decided.update(by_schedule)
        if not decided:
            return
        for q in self.ctx.get("questions") or []:
            if not isinstance(q, Mapping):
                continue
            text = str(q.get("question") or "")
            rid = str(q.get("row_id") or "") or _norm(text)[:60]
            key = f"question:{q.get('category') or 'main'}:{rid}"  # row ids repeat across categories
            if not text or self._overridden(key, "include"):
                continue
            new_q, removed = rewrite_question(text, decided)
            if not removed:
                continue
            clarification, keys = self._resolution(removed, schedule_entries)
            entry = {"key": key, "row_id": q.get("row_id"), "category": q.get("category"), "sheet": q.get("sheet"),
                     "question": text, "topics": removed, "decision_keys": keys, "clarification": clarification}
            if new_q is None:
                self.rep.questions_resolved[key] = entry
            else:
                self.rep.questions_rewritten[key] = {**entry, "after": new_q}

    def reconcile_marks(self) -> None:
        names = {_norm(e.get("description")) for e in self.excluded.values()}
        names.discard("")
        mp = self.out.get("marked_plans")
        removed = 0
        if isinstance(mp, dict):
            for sheet in mp.get("sheets") or []:
                legend = sheet.get("legend") or []
                drop = {i for i, e in enumerate(legend) if _norm(e.get("description")) in names}
                if not drop:
                    continue
                remap, new_legend = {}, []
                for i, e in enumerate(legend):
                    if i in drop:
                        continue
                    remap[i] = len(new_legend)
                    new_legend.append(e)
                marks = []
                for mk in sheet.get("marks") or []:
                    li = mk.get("legend_index")
                    if li in drop:
                        removed += 1
                        continue
                    marks.append({**mk, "legend_index": remap.get(li, li)})
                sheet["legend"], sheet["marks"] = new_legend, marks
        mu = self.out.get("markups")
        if isinstance(mu, dict):
            for sheet in mu.get("sheets") or []:
                for key in ("runs", "items", "segments"):
                    rows = sheet.get(key)
                    if isinstance(rows, list):
                        keep = [r for r in rows if _norm(r.get("description") if isinstance(r, Mapping) else "") not in names]
                        removed += len(rows) - len(keep)
                        sheet[key] = keep
        if removed:
            self.rep.reconciled_marks["marks"] = {"key": "marks", "removed": removed}

    def forced_exclusions(self) -> None:
        for key, ov in self.overrides.items():
            if ov.get("action") != "exclude" or not key.startswith("line:"):
                continue
            lid = key.split(":", 1)[1]
            for _h, _g, _s, it in self._items():
                if str(it.get("line_id")) == lid:
                    if self._exclude_item(it, key=key, reason=str(ov.get("note") or "excluded by override"),
                                          source="human"):
                        self._overridden(key, "exclude")
                    break

    def restore_included(self) -> None:
        """An include override on an earlier exclusion puts the line back."""
        for lid, rec in list(self.excluded.items()):
            key = str(rec.get("decision_key") or "")
            if self.overrides.get(key, {}).get("action") != "include":
                continue
            where = rec.get("where") or {}
            item = copy.deepcopy(rec.get("item") or {})
            if not item:
                continue
            if rec.get("delta_of"):
                # A reduced quantity goes back onto its row (the row stays).
                row = next((it for *_x, it in self._items() if str(it.get("line_id")) == str(rec["delta_of"])), None)
                if row is not None:
                    row["quantity"] = _q(_f(row.get("quantity")) + _f(rec.get("quantity")))
                    del self.excluded[lid]
                    self._overridden(key, "include")
                    continue
                item["line_id"] = rec["delta_of"]
            items = self._subgroup(str(where.get("section") or "NEW WORK"), str(where.get("group") or "OTHER ITEMS"),
                                   str(where.get("subgroup") or "MISCELLANEOUS"))
            assert items is not None
            items.append(item)
            del self.excluded[lid]
            self._overridden(key, "include")

    def brief_exclusions(self) -> None:
        brief = self.out.get("brief")
        if not isinstance(brief, dict):
            return
        current = [str(e) for e in brief.get("exclusions") or []]
        for e in self.rep.exclusions.values():
            text = str(e.get("text") or "")
            if text and text not in current:
                current.append(text)
        brief["exclusions"] = current

    def run(self) -> dict[str, Any]:
        self._assign_line_ids()
        self.restore_included()
        self.bid_conditions()
        self.breakers()
        self.devices()
        self.inclusions()
        self.rule_exclusions()
        self.fact_exclusions()
        self.allowances()
        self.obligation_exclusions()
        self.standards()
        self.forced_exclusions()
        self.rfis()
        self.questions()
        self.reconcile_marks()
        self.brief_exclusions()
        report = self.report()
        self.out["scope_policy"] = report
        self.out["excluded_lines"] = [self.excluded[k] for k in sorted(self.excluded, key=_line_sort)]
        return self.out

    def report(self) -> dict[str, Any]:
        rep = self.rep
        lines = proposal_lines({"exclusions": [rep.exclusions[k] for k in sorted(rep.exclusions)],
                                "clarifications": [rep.clarifications[k] for k in sorted(rep.clarifications)]},
                               self.policy)
        return {
            "schema": SCOPE_REPORT_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "policy_id": self.ctx.get("policy_id"),
            "policy_sha": self.ctx.get("policy_sha"),
            "decisions": [rep.decisions[k] for k in sorted(rep.decisions)],
            "clarifications": [rep.clarifications[k] for k in sorted(rep.clarifications)],
            "exclusions": [rep.exclusions[k] for k in sorted(rep.exclusions)],
            "proposal_lines": lines,
            "warnings": [{"key": k, "text": v} for k, v in sorted(rep.warnings.items())],
            "needs_review": [rep.needs_review[k] for k in sorted(rep.needs_review)],
            "overrides_applied": [rep.overrides_applied[k] for k in sorted(rep.overrides_applied)],
            "rfis_removed": [rep.rfis_removed[k] for k in sorted(rep.rfis_removed)],
            "rfis_rewritten": [rep.rfis_rewritten[k] for k in sorted(rep.rfis_rewritten)],
            "questions_resolved": [rep.questions_resolved[k] for k in sorted(rep.questions_resolved)],
            "questions_rewritten": [rep.questions_rewritten[k] for k in sorted(rep.questions_rewritten)],
            "excluded_allowances": [rep.excluded_allowances[k] for k in sorted(rep.excluded_allowances)],
            "reconciled_marks": [rep.reconciled_marks[k] for k in sorted(rep.reconciled_marks)],
            "quantity_added": [rep.added[k] for k in sorted(rep.added)],
            "decided_topics": sorted(t for t in rep.topics if t),
            "counts": {
                "decisions": len(rep.decisions),
                "exclusions": len(rep.exclusions),
                "clarifications": len(rep.clarifications),
                "excluded_lines": len(self.excluded),
                "needs_review": len(rep.needs_review),
                "rfis_removed": len(rep.rfis_removed),
                "rfis_rewritten": len(rep.rfis_rewritten),
                "questions_resolved": len(rep.questions_resolved),
                "questions_rewritten": len(rep.questions_rewritten),
                "excluded_allowances": len(rep.excluded_allowances),
            },
        }


def decided_topic_patterns(topics: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """RFI topic regexes for the decided topics (the RFI rewrite uses them)."""
    have = {t for t in topics if t}
    out: dict[str, tuple[str, ...]] = {}
    for table in (DECIDED_TOPICS_FIXED, standards.RFI_TOPICS, SYSTEM_RFI_TOPICS, RULE_RFI_TOPICS):
        for t, pats in table.items():
            if t in have:
                out[t] = tuple(pats)
    return out


def _source_rank(o: Mapping[str, Any]) -> tuple[int, float]:
    """Owner exhibits first (a named vendor before the prime), then
    electrical sheets, electrical specs, other specs, mechanical sheets."""
    role = str(o.get("role") or "")
    party = str(o.get("party") or "")
    if role in ("owner_exhibit", "rfp", "bid_instructions", "contract_conditions", "owner_scope_exhibit"):
        rank = 0 if party == "owner_vendor" else 1
    elif role in ("keynote", "sheet_note", "general_note", "schedule_note", "legend", "addendum"):
        rank = 2
    elif role == "electrical_spec":
        rank = 3
    elif role == "spec":
        rank = 4
    else:
        rank = 5
    return (rank, -float(o.get("confidence") or 0.0))


INSTALL_LINE_RE = re.compile(r"\binstal+\w*|\bconnect\w*|\brough-?in\b|\bconduits?\b|\bj-?box|\bbackbox", re.I)


def _demo_power_first(description: str) -> bool:
    """A demolition power row: it names a power device, and no lighting
    word comes before the first power word ("EXISTING LIGHT FIXTURE TO BE
    REMOVED ... CONDUIT, CONNECTION" is lighting)."""
    power = DEMO_POWER_RE.search(description)
    if power is None:
        return False
    light = DEMO_LIGHTING_RE.search(description)
    return light is None or light.start() > power.start()


def _rule_head(description: str) -> str:
    """Policy rules read the item name: a long assembly description is cut
    to its head so an included sub-task never excludes the assembly."""
    d = description.strip()
    return d[:90] if len(d) > 120 else d


def _line_sort(lid: str) -> tuple[int, str]:
    try:
        return (parse_row_id(lid)[1], lid)
    except Exception:  # noqa: BLE001
        return (10**9, lid)


def _is_line_id(value: str) -> bool:
    try:
        return parse_row_id(value)[0] == "B"
    except Exception:  # noqa: BLE001
        return False


def _is_row_ref(value: Any) -> bool:
    """A row id ref (C12, M3, S4#r6:QTY) as opposed to a text-fact ref."""
    try:
        parse_row_id(str(value).split("#", 1)[0])
    except Exception:  # noqa: BLE001
        return False
    return True


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _q(v: float) -> int | float:
    return int(v) if float(v).is_integer() else round(float(v), 4)


_BRK_RE = re.compile(r"(\d{2,4})\s*A?\s*/\s*(\d)\s*P", re.I)


def _breaker_key(description: str) -> tuple[Any, ...]:
    m = _BRK_RE.search(description or "")
    d = (description or "").upper()
    return (int(m.group(1)) if m else None, int(m.group(2)) if m else None,
            bool(re.search(r"\bGFC?I\b|GROUND\s+FAULT", d)), bool(re.search(r"\bAFCI\b|ARC\s+FAULT", d)))


def _merge_line_sources(head: dict[str, Any], members: Sequence[Mapping[str, Any]]) -> None:
    """Carry the v2 provenance of merged lines into the surviving line: the
    union of their `source_refs` and `sheet_refs`, `combine` sum (floor when
    any member rests on a floor) and `needs_recount`. Without it a merge
    (demolition canonical rows, inclusion canonicalization) kept only the
    head's refs, so the merged quantity no longer reproduced from its
    sources (gate arithmetic) and the other members' rows turned into
    orphans (gate orphan_rows). Legacy lines (no provenance keys) are left
    as written."""
    if len(members) < 2 or not any("source_refs" in m for m in members):
        return
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in members:
        for r in m.get("source_refs") or []:
            ref = str(r.get("ref") if isinstance(r, Mapping) else r or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            mult = r.get("multiplier") if isinstance(r, Mapping) else None
            refs.append({"ref": ref, "multiplier": float(mult) if isinstance(mult, (int, float)) and mult else 1.0})
    head["source_refs"] = refs
    sheets = {str(s) for m in members for s in m.get("sheet_refs") or [] if s}
    if sheets or "sheet_refs" in head:
        head["sheet_refs"] = sorted(sheets)
    combines = {str(m.get("combine") or "") for m in members}
    if any(not _is_row_ref(r["ref"]) for r in refs):
        # A text-fact ref (t:...) has no quantity: a merged line that carries
        # one is not row arithmetic, so it carries no combine (the recheck
        # computes row-sourced combines only).
        head.pop("combine", None)
    elif len(refs) > 1 and combines <= {"single", "sum", "floor", ""}:
        head["combine"] = "floor" if "floor" in combines else "sum"
    if any(m.get("needs_recount") for m in members):
        head["needs_recount"] = True
    bases = {str(m.get("basis") or "") for m in members if m.get("basis")}
    if len(bases) > 1:
        head["basis"] = "mixed"


def _trigger_sources(inc: Mapping[str, Any]) -> dict[str, Any]:
    """`source_refs` (and `combine`) for an inserted inclusion line.

    - Reader-row triggers (every used trigger carries a row id and their
      quantities sum to the inclusion's, counted basis): the rows, combine
      sum or single, so the arithmetic recheck reproduces the quantity.
    - Page-facts triggers (no reader row; the keynote facts of a drawing set,
      spec WS5 Q14): the keynote text-line refs `t:<doc8>:<page>:<line>` the
      triggers carry (`keynote_text_ref`), one per used keynote, and NO
      `combine`: a text ref has no quantity, so the arithmetic recheck (which
      computes row-sourced combines only) leaves the line alone, while the
      unsourced_line and citation gates accept a non-row ref. The quantity is
      the keynote marker count named in `material_note`. A companion line
      (assumed length per trigger) cites the same keynote lines.
    - A measured companion cites its M row (combine single).

    A context stored before triggers carried row ids or text refs cites
    nothing, as before (the gate routes the line to a human)."""
    if str(inc.get("basis") or "counted") == "measured" and inc.get("measure_row_id"):
        return {"source_refs": [{"ref": str(inc["measure_row_id"]), "multiplier": 1.0}], "combine": "single"}
    used = [t for t in inc.get("triggers") or [] if isinstance(t, Mapping) and t.get("used", True)]
    if not used:
        return {}
    if all(t.get("source") == "page_facts" for t in used):
        text_refs: list[dict[str, Any]] = []
        seen_t: set[str] = set()
        for t in used:
            ref = str(t.get("text_ref") or "").strip()
            if not ref:
                return {}
            if ref not in seen_t:
                seen_t.add(ref)
                text_refs.append({"ref": ref, "multiplier": 1.0})
        return {"source_refs": text_refs}
    if inc.get("companion_of") or str(inc.get("basis") or "counted") != "counted":
        return {}
    if not all(t.get("row_id") for t in used):
        return {}
    if abs(sum(_f(t.get("quantity")) for t in used) - _f(inc.get("quantity"))) > 1e-6:
        return {}
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in used:
        rid = str(t["row_id"])
        if rid in seen:
            return {}
        seen.add(rid)
        refs.append({"ref": rid, "multiplier": 1.0})
    return {"source_refs": refs, "combine": "sum" if len(refs) > 1 else "single"}


def _trigger_quantity_wins(keep: Mapping[str, Any], inc: Mapping[str, Any], model_total: float) -> dict[str, Any] | None:
    """The inclusion's trigger quantity over the model line it merged
    (7154 run b87f0ffd: the model carried FIRE/SMOKE DAMPER ELECTRICAL
    RECONNECTION 6 EA from C40 and dropped the VAV row C41; the policy says
    ELECTRICAL CONNECTION 7 EA from both). It wins when the used triggers
    each cite a reader row, their quantity is above the model's and every
    row the model line cites is one of the triggers (so the trigger sum
    covers what the model counted). Returns the merged quantity, the union
    of the refs (combine sum over the trigger rows, which reproduces the
    quantity), the union of the sheets and the decision evidence; None when
    the model's count stands."""
    used = [t for t in inc.get("triggers") or [] if isinstance(t, Mapping) and t.get("used", True)]
    if not used or not all(t.get("row_id") for t in used):
        return None
    trig_qty = sum(_f(t.get("quantity")) for t in used)
    if trig_qty <= model_total + 1e-9:
        return None
    trig_rows = list(dict.fromkeys(str(t["row_id"]) for t in used))
    model_rows = [str(r.get("ref") if isinstance(r, Mapping) else r or "").strip() for r in keep.get("source_refs") or []]
    model_rows = [r for r in model_rows if r]
    if any(r not in trig_rows for r in model_rows):
        return None
    refs = [{"ref": r, "multiplier": 1.0} for r in trig_rows]
    sheets = sorted({str(x) for x in keep.get("sheet_refs") or [] if x} | {str(t["sheet"]) for t in used if t.get("sheet")})
    return {
        "quantity": trig_qty,
        "source_refs": refs,
        "combine": "sum" if len(refs) > 1 else "single",
        "sheet_refs": sheets,
        "evidence": {
            "quantity_from": "triggers", "model_quantity": _q(model_total), "trigger_quantity": _q(trig_qty),
            "model_refs": model_rows, "trigger_refs": trig_rows,
            "triggers": [{"row_id": t.get("row_id"), "description": t.get("description"), "quantity": t.get("quantity"),
                          "sheet": t.get("sheet")} for t in used],
        },
    }


def _trigger_note(inc: Mapping[str, Any]) -> str:
    used = [t for t in inc.get("triggers") or [] if t.get("used", True)]
    cites = sorted({f"{t.get('sheet')}{' ' + str(t['ref']) if t.get('ref') else ''}" for t in used if t.get("sheet")})
    where = ", ".join(cites) or "the reader rows"
    if inc.get("basis") == "assumed_per_trigger":
        return f"Per relocated device, assumed run length; triggers on {where}."
    if inc.get("trigger_source") == "page_facts":
        return f"{inc.get('quantity')} {inc.get('unit')} from the keynote markers on {where} (policy {inc.get('rule_id')})."
    return f"{inc.get('quantity')} {inc.get('unit')} from {where} (policy {inc.get('rule_id')})."


def _unless_holds(rule: Mapping[str, Any], item: Mapping[str, Any], includes: Sequence[Mapping[str, Any]],
                  amounts: bool) -> bool:
    unless = set(rule.get("unless") or [])
    if "drawn_with_quantity" in unless and attr.line_drawn(item) and _f(item.get("quantity")) > 0:
        return True
    if "assigned_to_ec_explicitly" in unless:
        pats = [scope_policy.compile_pattern(p) for p in (rule.get("match") or {}).get("patterns") or ()]
        for o in includes:
            if o.get("party") == "ec" and any(p.search(str(o.get("text") or "")) for p in pats):
                return True
    if "owner_allowance_amount" in unless and amounts:
        return True
    return False


def _amount_for(name: str, amounts: Sequence[Mapping[str, Any]]) -> bool:
    words = [w for w in _norm(name).split() if len(w) > 3]
    for a in amounts:
        text = _norm(a.get("text"))
        if words and sum(1 for w in words if w in text) >= max(1, len(words) // 2):
            return True
    return False


def _join_labels(labels: Sequence[str]) -> str:
    labels = [x for x in labels if x]
    if len(labels) <= 1:
        return labels[0] if labels else "scope"
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _fact_sentence(fact: Fact, systems: Sequence[str], facts: Sequence[Fact]) -> str:
    covered = [s for s in systems if any(f.system == s and f.excludes and f.cite.quote == fact.cite.quote for f in facts)]
    label = _join_labels([SYSTEM_LABELS.get(s, s.replace("_", " ")) for s in covered or [fact.system]])
    who = fact.party or "others"
    if who == "gc":
        who = "the general contractor"
    return f"{label[0].upper()}{label[1:]} by {who} per {fact.cite.section}: \"{fact.cite.quote[:160]}\""


def _excluded_note(fact: Fact) -> str:
    from app.services.estimating_contracts import excluded_note

    return excluded_note(fact.system, fact.party, str(fact.cite.section or ""), fact.cite.quote[:160])


ANAPHORIC_RFI_RE = re.compile(r"\b(?:these|this|its|their|such|said|the\s+specific)\b", re.I)
_RFI_SENT_RE = re.compile(r"(?<=[.?;])\s+(?=[A-Z])")
_LIST_SPLIT_RE = re.compile(r",\s+(?:and\s+)?|\s+and\s+")
_LEAD_VERB_RE = re.compile(
    r"^(?P<verb>(?:please\s+)?(?:confirm|provide|resolve|identify|define|allocate|delineate|clarify|state|advise)"
    r"(?:\s+(?:the|whether|which|all|any))?\s+)", re.I)


def rewrite_question(question: str, decided: Mapping[str, Sequence[str]]) -> tuple[str | None, list[str]]:
    """(rewritten question or None when nothing undecided is left, decided
    topics removed). Per sentence: no decided topic keeps it; a list sentence
    loses only its decided items, unless its subject (the first item or the
    scoping phrase) is decided, which drops the sentence; a single-subject
    sentence with a decided topic is dropped. When the first sentence goes
    and at least half of the sentences go, the whole question goes (the rest
    refers to the decided subject)."""
    if not decided:
        return question, []
    compiled = {t: [re.compile(p, re.I) for p in pats] for t, pats in decided.items()}

    def hits(text: str) -> set[str]:
        return {t for t, ps in compiled.items() if any(p.search(text) for p in ps)}

    removed: set[str] = set()
    sentences = [x.strip() for x in _RFI_SENT_RE.split(question.strip()) if x.strip()]
    kept: list[str] = []
    dropped_first = False
    for idx, s in enumerate(sentences):
        h = hits(s)
        if not h:
            kept.append(s)
            continue
        new, gone = _rewrite_sentence(s, hits)
        removed |= gone
        if new is None:
            if idx == 0:
                dropped_first = True
            continue
        kept.append(new)
    if dropped_first:
        # Later sentences that point back at the dropped subject go with it.
        kept = [k for k in kept if not ANAPHORIC_RFI_RE.search(k)]
    if not kept:
        return None, sorted(removed)
    return " ".join(kept), sorted(removed)


def _items_of(text: str) -> list[str]:
    return [x.strip() for x in _LIST_SPLIT_RE.split(text) if x.strip()]


def _join_items(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# A list item that names its own subject (a luminaire tag "L5", a sheet
# "E2.01", a keynote "KN10", a circuit "LP-1"): it stands when the item before
# it is decided ("Confirm L4 control method and L5 module lengths" keeps the L5
# clause, E19), unlike a bare noun that rides on the decided subject
# ("temporary office loads and layouts" goes whole).
_SUBJECT_TOKEN_RE = re.compile(r"\b[A-Z]{1,3}-?\d{1,3}(?:\.\d{1,2})?\b")


def _rewrite_sentence(s: str, hits: Callable[[str], set[str]]) -> tuple[str | None, set[str]]:
    """(the sentence without its decided items, or None when it goes whole;
    the topics removed: the dropped items' topics, or the whole sentence's
    when it goes)."""
    m = _LEAD_VERB_RE.match(s)
    lead = m.group("verb") if m else ""
    body = s[len(lead):]
    end = ""
    if body and body[-1] in ".?":
        body, end = body[:-1], body[-1]
    tail = ""
    prep = ""
    tm = re.search(r"\s+(between|for|with|while|including|so\s+that)\s+", body, re.I)
    if tm:
        body, prep, tail = body[: tm.start()], tm.group(0), body[tm.end():]
    body_items = _items_of(body)
    tail_items = _items_of(tail) if tail else []
    whole = hits(s)
    if len(tail_items) > len(body_items) and len(tail_items) > 1:
        if hits(body):
            return None, whole
        keep = [it for it in tail_items if not hits(it)]
        if not keep:
            return None, whole
        gone = set().union(*(hits(it) for it in tail_items if hits(it)))
        out = f"{lead}{body}{prep}{_join_items(keep)}"
    else:
        if len(body_items) <= 1:
            return None, whole
        if tail and hits(tail):
            return None, whole
        keep = [it for it in body_items if not hits(it)]
        if not keep:
            return None, whole
        if hits(body_items[0]) and not _SUBJECT_TOKEN_RE.search(keep[0]):
            # the decided first item is the subject the rest rides on
            return None, whole
        gone = set().union(*(hits(it) for it in body_items if hits(it)))
        out = f"{lead}{_join_items(keep)}{prep}{tail}"
    out = out.strip()
    if not out:
        return None, whole
    return out[0].upper() + out[1:] + (end or "."), gone


def enforce(
    main_output: Mapping[str, Any],
    ctx: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    overrides: Sequence[Mapping[str, Any]] = (),
    *,
    settings: Any = None,
) -> dict[str, Any]:
    """The enforced copy of `main_output` (see module doc). Idempotent."""
    doc = policy if policy is not None else scope_policy.active_policy().doc
    return _Enforcer(main_output, ctx or {}, doc, list(overrides or ()), settings).run()


def proposal_lines(report: Mapping[str, Any], policy: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Suggested proposal lines (Q25): the four template sentences plus one
    line per exclusion and clarification. Suggestions only; a human approves
    before anything reaches a proposal."""
    from app.services.scope_policy_seed import TEMPLATE_LINES

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in TEMPLATE_LINES:
        out.append({"text": text, "kind": "template", "key": f"template:{_norm(text)[:40]}", "status": "suggested"})
        seen.add(text)
    for kind in ("exclusions", "clarifications"):
        for e in report.get(kind) or []:
            text = str(e.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append({"text": text[:300], "kind": kind[:-1], "key": e.get("key"), "cite": e.get("cite"),
                        "status": "suggested"})
    return out


# Run-level wiring (DB) ----------------------------------------------------------------------


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from app.core.config import get_settings

    return get_settings()


def _clean(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if v is not None}


def _latest_output(sb: Any, run_id: str, scope: str, category: str | None = None) -> dict[str, Any] | None:
    try:
        q = sb.table("plans_reader_outputs").select("*").eq("run_id", run_id).eq("scope", scope)
        if category is not None:
            q = q.eq("category", category)
        rows = q.order("version", desc=True).limit(1).execute().data or []
    except Exception as exc:  # noqa: BLE001 - an older schema without the scope
        logger.debug("scope_engine: output %s/%s unavailable: %s", run_id, scope, exc)
        return None
    return rows[0] if rows else None


def _next_version(sb: Any, run_id: str, scope: str) -> int:
    try:
        rows = (sb.table("plans_reader_outputs").select("version").eq("run_id", run_id).eq("scope", scope)
                .order("version", desc=True).limit(1).execute().data or [])
    except Exception:  # noqa: BLE001
        return 1
    return int(rows[0]["version"]) + 1 if rows else 1


def write_context(sb: Any, run: Mapping[str, Any], ctx: Mapping[str, Any]) -> int:
    """Store the ScopeContext as outputs scope `policy` (next version) and
    stamp the run (MD columns; skipped quietly while MD is not applied)."""
    run_id = str(run["id"])
    version = _next_version(sb, run_id, POLICY_SCOPE)
    sb.table("plans_reader_outputs").insert(_clean({
        "run_id": run_id, "scope": POLICY_SCOPE, "category": None, "version": version,
        "output": dict(ctx), "source_group_ids": [],
    })).execute()
    stamp = _clean({
        "scope_policy_id": ctx.get("policy_id"),
        "scope_policy_sha": ctx.get("policy_sha"),
        "bid_conditions": ctx.get("bid_conditions"),
    })
    if stamp:
        try:
            sb.table("plans_reader_runs").update(stamp).eq("id", run_id).execute()
        except Exception as exc:  # noqa: BLE001 - MD columns arrive with 0166
            logger.info("scope_engine: run stamp skipped for %s: %s", run_id, exc)
    return version


def _project_row(sb: Any, run: Mapping[str, Any]) -> dict[str, Any] | None:
    pid = run.get("project_id")
    if not pid:
        return None
    for cols in ("name, number, wage_type, owner_type, labor_time, labor_needed, project_type", "name, number"):
        try:
            rows = sb.table("projects").select(cols).eq("id", pid).limit(1).execute().data or []
            return dict(rows[0]) if rows else None
        except Exception:  # noqa: BLE001 - try the narrower select
            continue
    return None


def _db_inputs(sb: Any, run: Mapping[str, Any], outputs: Any = None) -> ScopeInputs:
    run_id = str(run["id"])
    docs = sb.table("plans_reader_documents").select("*").eq("run_id", run_id).execute().data or []
    cats: dict[str, Any] = {}
    measure = None
    main = None
    if outputs:
        rows = list(outputs.values()) if isinstance(outputs, Mapping) and not outputs.get("scope") else (
            outputs if isinstance(outputs, list) else [outputs])
        for r in rows:
            if isinstance(r, Mapping) and r.get("scope") == "category":
                cats[str(r.get("category"))] = r.get("output")
            elif isinstance(r, Mapping) and r.get("scope") == "measure":
                measure = r.get("output")
            elif isinstance(r, Mapping) and r.get("scope") == "main":
                main = r.get("output")
            elif isinstance(r, Mapping) and r.get("category") and r.get("counts") is not None:
                cats[str(r.get("category"))] = r
    if not cats:
        try:
            rows = sb.table("plans_reader_outputs").select("*").eq("run_id", run_id).eq("scope", "category").execute().data or []
        except Exception:  # noqa: BLE001
            rows = []
        latest: dict[str, Mapping[str, Any]] = {}
        for r in rows:
            c = str(r.get("category"))
            if c not in latest or int(r.get("version") or 0) > int(latest[c].get("version") or 0):
                latest[c] = r
        cats = {c: r.get("output") for c, r in latest.items()}
    if measure is None:
        m = _latest_output(sb, run_id, "measure")
        measure = m.get("output") if m else None
    by_id = {str(d["id"]): d for d in docs}

    def _pdf(doc_id: str) -> bytes | None:
        from app.services import storage

        d = by_id.get(doc_id) or {}
        path = d.get("storage_path")
        return storage.download_file(path) if path else None

    def _facts(doc_id: str) -> Any:
        try:
            from app.services.pagefacts import api as pf_api
        except ImportError:
            return None
        fn = getattr(pf_api, "doc_facts", None)
        return fn(sb, doc_id) if fn else None

    def _lines(doc_id: str, page: int) -> list[str] | None:
        try:
            from app.services.pagefacts import api as pf_api
        except ImportError:
            return None
        fn = getattr(pf_api, "page_words", None)
        if fn is None:
            return None
        _words, lines = fn(sb, doc_id, page)
        return [ln["t"] if isinstance(ln, Mapping) else ln.t for ln in lines]

    return ScopeInputs(docs, project=_project_row(sb, run), category_outputs=cats, measure=measure, main_output=main,
                       pdf_bytes=_pdf, doc_facts=_facts, page_lines=_lines)


def _snapshot_route(run: Mapping[str, Any], step: str) -> dict[str, Any] | None:
    snap = run.get("settings_snapshot") if isinstance(run, Mapping) else None
    routes = snap.get("routes") if isinstance(snap, Mapping) else None
    route = routes.get(step) if isinstance(routes, Mapping) else None
    return dict(route) if isinstance(route, Mapping) else None


def prepare(sb: Any, run: Mapping[str, Any], outputs: Any = None, *, allow_llm: bool = False,
            settings: Any = None, write: bool = True) -> dict[str, Any]:
    """Derive step 1 on a run: build and store the ScopeContext."""
    s = _settings(settings)
    inputs = _db_inputs(sb, run, outputs)
    active = scope_policy.active_policy()
    caller = None
    if allow_llm and getattr(s, "ai_scope_attrib_llm", False):
        # The attribution call runs on the run's snapshotted scope_attrib
        # route when the run has one (spec 4.2), else on the live settings.
        caller = attr.default_llm_caller(s, route=_snapshot_route(run, attr.FALLBACK_STEP))
    ctx = prepare_context(inputs, policy=active, settings=s, allow_llm=allow_llm, llm_caller=caller)
    if write:
        write_context(sb, run, ctx)
    return ctx


def context_for_run(sb: Any, run_id: str) -> dict[str, Any] | None:
    row = _latest_output(sb, run_id, POLICY_SCOPE)
    out = row.get("output") if row else None
    return dict(out) if isinstance(out, Mapping) else None


def facts_for_run(sb: Any, run_id: str) -> list[Fact]:
    """The F* facts of a run (estimating_contracts.FactsForRun)."""
    ctx = context_for_run(sb, run_id) if sb is not None else None
    out: list[Fact] = []
    for f in (ctx or {}).get("facts") or []:
        try:
            out.append(Fact.from_dict(f))
        except Exception:  # noqa: BLE001 - a malformed stored fact is skipped
            continue
    return out


def standards_for(sb: Any, run_id: str) -> dict[str, Any]:
    ctx = context_for_run(sb, run_id) if sb is not None else None
    return dict((ctx or {}).get("standards") or {})


def overrides_for_run(sb: Any, run_id: str) -> list[dict[str, Any]]:
    try:
        rows = sb.table("plans_reader_scope_overrides").select("*").eq("run_id", run_id).execute().data or []
    except Exception as exc:  # noqa: BLE001 - the table arrives with 0166
        logger.debug("scope_engine: overrides unavailable for %s: %s", run_id, exc)
        return []
    return [dict(r) for r in rows]


def enforce_main_output(sb: Any, run: Mapping[str, Any], output: Mapping[str, Any], *, settings: Any = None) -> dict[str, Any]:
    """The `_finish_with_main` hook (render step 2): the enforced output when
    AI_SCOPE_POLICY_ENFORCE is on, else `output` unchanged. Never raises: a
    failure logs and returns the original output."""
    s = _settings(settings)
    if not getattr(s, "ai_scope_policy_enforce", False):
        return dict(output)
    try:
        ctx = context_for_run(sb, str(run["id"]))
        if ctx is None:
            ctx = prepare(sb, run, None, allow_llm=False, settings=s)
        return enforce(output, ctx, scope_policy.active_policy().doc, overrides_for_run(sb, str(run["id"])), settings=s)
    except Exception:  # noqa: BLE001 - scope enforcement never fails a run
        logger.exception("scope_engine: enforce failed for run %s; output kept", run.get("id"))
        return dict(output)


ENFORCED_KINDS: tuple[str, ...] = ("boq", "estimate", "brief")


def _canon_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _latest_deliverable_row(sb: Any, run_id: str, kind: str) -> dict[str, Any] | None:
    try:
        rows = (sb.table("plans_reader_deliverables").select("*").eq("run_id", run_id).eq("kind", kind)
                .execute().data or [])
    except Exception as exc:  # noqa: BLE001 - no deliverables table in an older schema
        logger.debug("scope_engine: deliverables unavailable for %s: %s", run_id, exc)
        return None
    rows = [r for r in rows if isinstance(r, Mapping)]
    return dict(max(rows, key=lambda r: int(r.get("version") or 0))) if rows else None


def _spec_parts(kind: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    if kind == "boq":
        return {"boq": copy.deepcopy(dict(spec))}
    if kind == "estimate":
        return {k: copy.deepcopy(spec[k]) for k in ("boq", "estimate") if isinstance(spec.get(k), Mapping)}
    if kind == "brief":
        return {"brief": copy.deepcopy(dict(spec))}
    return {}


def _item_key(it: Mapping[str, Any]) -> tuple[str, str]:
    return _norm(it.get("description")), str(it.get("unit") or "EA").upper()


def mark_spec_edits(boq: Any, main_boq: Any) -> int:
    """Mark the BOQ rows a chat or modify edit added or changed (against the
    main output the deliverable was first rendered from) with origin
    `modify`, so the automatic rules leave them alone (spec WS5: rows added
    through chat or modify are exempt). A row that lost its line id in the
    edit gets the id of the unchanged main row it matches, so line overrides
    keep pointing at the same row. Returns the number of rows marked."""
    if not isinstance(boq, Mapping):
        return 0
    main_items = [it for *_x, it in boq_items({"boq": main_boq})] if isinstance(main_boq, Mapping) else []
    by_id = {str(it.get("line_id")): it for it in main_items if it.get("line_id")}
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for it in main_items:
        by_key.setdefault(_item_key(it), []).append(it)
    used: set[str] = set()
    marked = 0
    for *_x, it in boq_items({"boq": boq}):
        if devices.is_exempt(it):
            continue
        lid = it.get("line_id")
        ref = by_id.get(str(lid)) if lid else None
        if ref is None and not lid:
            ref = next((m for m in by_key.get(_item_key(it), []) if str(m.get("line_id")) not in used), None)
            if ref is not None and ref.get("line_id"):
                it["line_id"] = ref["line_id"]
        if ref is not None:
            used.add(str(ref.get("line_id")))
        same = ref is not None and _item_key(ref) == _item_key(it) and _f(ref.get("quantity")) == _f(it.get("quantity"))
        if not same:
            it["origin"] = "modify"
            marked += 1
    return marked


def enforce_for_run(sb: Any, run_id: str, *, settings: Any = None, policy: Mapping[str, Any] | None = None,
                    write: bool = True, render: Callable[..., Any] | None = None,
                    reason: str = "scope re-enforcement") -> dict[str, Any] | None:
    """Re-enforce a run (an override or a new active policy) and store the
    result. Returns the enforced main output, or None when the run has no
    main output yet.

    Chat and modify edits live in the deliverable specs (a modify never
    rewrites main), so when the latest BOQ, estimate or brief spec differs
    from the main output, enforcement runs on those specs (never discarding
    the edits: changed and added rows are marked `origin: modify` and stay
    exempt) and each edited deliverable is enforced from its own spec. The
    output records what it enforced under `scope_enforced_from` (main
    version, deliverable versions, mode). With `write`, the enforced output
    is stored as the next main version and every deliverable whose spec
    changed is rendered as its next version through `render` (default
    plans_reader_runs.render_deliverable); a render failure is recorded,
    never raised."""
    row = _latest_output(sb, run_id, "main")
    if not row or not isinstance(row.get("output"), Mapping):
        return None
    main = copy.deepcopy(dict(row["output"]))
    ctx = context_for_run(sb, run_id) or {}
    doc = policy or scope_policy.active_policy().doc
    overrides = overrides_for_run(sb, run_id)
    s = _settings(settings)
    latest: dict[str, dict[str, Any]] = {}
    edited: dict[str, dict[str, Any]] = {}
    for kind in ENFORCED_KINDS:
        d = _latest_deliverable_row(sb, run_id, kind)
        if d is None:
            continue
        latest[kind] = d
        spec = d.get("spec_json")
        if isinstance(spec, Mapping) and _canon_json(spec) != _canon_json(_main_spec(kind, main)):
            edited[kind] = dict(spec)
    base = copy.deepcopy(main)
    for kind in ("estimate", "boq", "brief"):  # the BOQ deliverable wins over the estimate's BOQ copy
        if kind in edited:
            base.update(_spec_parts(kind, edited[kind]))
    marked = mark_spec_edits(base.get("boq"), main.get("boq")) if edited else 0
    enforced = enforce(base, ctx, doc, overrides, settings=s)
    enforced["scope_enforced_from"] = {
        "mode": "deliverable_specs" if edited else "main",
        "main_version": row.get("version"),
        "deliverable_versions": {k: latest[k].get("version") for k in sorted(latest)},
        "edited_kinds": sorted(edited),
        "rows_marked_modify": marked,
        "reason": reason,
    }
    specs: dict[str, dict[str, Any]] = {}
    for kind in ENFORCED_KINDS:
        if kind == "estimate" and "estimate" in edited and "boq" in edited:
            # Both carry a BOQ: the estimate's own copy keeps its edits, so it
            # is enforced from its own spec.
            own = copy.deepcopy(main)
            own.update(_spec_parts(kind, edited[kind]))
            mark_spec_edits(own.get("boq"), main.get("boq"))
            spec = _main_spec(kind, enforce(own, ctx, doc, overrides, settings=s))
        else:
            spec = _main_spec(kind, enforced)
        prior = latest.get(kind, {}).get("spec_json") if kind in latest else _main_spec(kind, main)
        if spec is not None and _canon_json(spec) != _canon_json(prior):
            specs[kind] = spec
    if write:
        sb.table("plans_reader_outputs").insert(_clean({
            "run_id": run_id, "scope": "main", "category": None, "version": _next_version(sb, run_id, "main"),
            "output": enforced, "source_group_ids": list(row.get("source_group_ids") or []),
        })).execute()
        rendered, errors = _render_specs(sb, run_id, specs, latest, render=render, reason=reason)
        enforced["scope_enforced_from"]["rendered"] = rendered
        if errors:
            enforced["scope_enforced_from"]["render_errors"] = errors
    else:
        enforced["scope_enforced_from"]["pending_specs"] = sorted(specs)
    return enforced


def _main_spec(kind: str, main: Mapping[str, Any]) -> dict[str, Any] | None:
    """deliverable_spec for the enforced kinds, without importing the run
    module (the same shapes: boq, {boq, estimate}, brief)."""
    if kind == "boq":
        return main.get("boq") if isinstance(main.get("boq"), dict) else None
    if kind == "estimate":
        if isinstance(main.get("boq"), dict) and isinstance(main.get("estimate"), dict):
            return {"boq": main["boq"], "estimate": main["estimate"]}
        return None
    if kind == "brief":
        return main.get("brief") if isinstance(main.get("brief"), dict) else None
    return None


def _render_specs(sb: Any, run_id: str, specs: Mapping[str, dict[str, Any]], latest: Mapping[str, Mapping[str, Any]],
                  *, render: Callable[..., Any] | None, reason: str) -> tuple[dict[str, Any], list[str]]:
    if not specs:
        return {}, []
    if render is None:
        from app.services.plans_reader_runs import render_deliverable as render
    try:
        runs = sb.table("plans_reader_runs").select("*").eq("id", run_id).limit(1).execute().data or []
    except Exception as exc:  # noqa: BLE001
        return {}, [f"run row unavailable: {type(exc).__name__}"]
    if not runs:
        return {}, ["run row not found"]
    run = dict(runs[0])
    rendered: dict[str, Any] = {}
    errors: list[str] = []
    for kind, spec in specs.items():
        version = int(latest.get(kind, {}).get("version") or 0) + 1
        try:
            res = render(sb, run, kind, spec, version=version, instruction=f"Scope policy: {reason}"[:300])
            rendered[kind] = (res or {}).get("version", version) if isinstance(res, Mapping) else version
        except Exception as exc:  # noqa: BLE001 - the enforced main is stored; a render can be retried
            logger.exception("scope_engine: rendering %s for run %s failed", kind, run_id)
            errors.append(f"{kind}: {type(exc).__name__}: {exc}"[:300])
    return rendered, errors


def apply_override(sb: Any, run_id: str, override: Mapping[str, Any], *, settings: Any = None,
                   render: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Apply one stored override to a finished run now: re-enforce the latest
    specs (the override is read back with the run's other overrides; every
    other decision is already in the report, so only this decision's delta
    changes) and render the deliverables whose spec changed. Off while
    AI_SCOPE_POLICY_ENFORCE is off (the override then applies at the next
    enforcement). Never raises."""
    s = _settings(settings)
    key = str(override.get("decision_key") or "")
    if not getattr(s, "ai_scope_policy_enforce", False):
        return {"applied": False, "reason": "enforcement_off"}
    try:
        out = enforce_for_run(sb, run_id, settings=s, render=render, reason=f"override {key} {override.get('action')}")
    except Exception as exc:  # noqa: BLE001 - the override row is stored either way
        logger.exception("scope_engine: applying override %s on run %s failed", key, run_id)
        return {"applied": False, "reason": f"{type(exc).__name__}"}
    if out is None:
        return {"applied": False, "reason": "no_main_output"}
    applied = [o for o in (out.get("scope_policy") or {}).get("overrides_applied") or [] if o.get("key") == key]
    meta = dict(out.get("scope_enforced_from") or {})
    return {"applied": bool(applied), "reason": None if applied else "decision_not_present",
            "enforced_from": meta}


# Active policy backed by ai_scope_policies (MD) -------------------------------------------------

_LOADER_LOCK = threading.Lock()
_LOADER_CACHES: list[dict[str, Any]] = []
POLICY_TTL_S = 30.0


def invalidate_policy_cache() -> None:
    """Expire every table loader cache of this process (an activation in
    this process is seen at once; other workers see it within the TTL)."""
    with _LOADER_LOCK:
        for cache in _LOADER_CACHES:
            cache["at"] = float("-inf")


def table_policy_loader(sb_factory: Callable[[], Any] | None = None, *, ttl_s: float = POLICY_TTL_S) -> scope_policy.PolicyLoader:
    """A loader for `scope_policy.set_active_policy_loader`: the active row of
    `ai_scope_policies`, cached `ttl_s` seconds (short, so every worker
    reads a new activation within it; nothing is pinned in process); None
    (code seed) while the table is missing or holds no active row."""
    cache: dict[str, Any] = {"at": float("-inf"), "value": None}
    with _LOADER_LOCK:
        _LOADER_CACHES.append(cache)

    def _load() -> scope_policy.ActivePolicy | None:
        now = time.monotonic()
        with _LOADER_LOCK:
            if now - cache["at"] < ttl_s:
                return cache["value"]
        value = None
        try:
            if sb_factory is None:
                from app.core.supabase_client import get_supabase

                sb = get_supabase()
            else:
                sb = sb_factory()
            rows = (sb.table("ai_scope_policies").select("id, version, doc, sha256").eq("status", "active")
                    .limit(1).execute().data or [])
            if rows and isinstance(rows[0].get("doc"), Mapping):
                info = scope_policy.validate(rows[0]["doc"])
                value = scope_policy.ActivePolicy(doc=rows[0]["doc"], sha256=info.sha256, version=info.version,
                                                  source="table", policy_id=str(rows[0]["id"]))
        except Exception as exc:  # noqa: BLE001 - missing table or invalid doc: the seed applies
            logger.debug("scope_engine: active policy table unavailable: %s", exc)
            value = None
        with _LOADER_LOCK:
            cache["at"], cache["value"] = now, value
        return value

    return _load


# Replay stages (WS0 harness) --------------------------------------------------------------------


def _replay_inputs(rctx: Any, art: Mapping[str, Any]) -> ScopeInputs:
    docs = [dict(d) for d in art.get("documents") or []]
    cats = {k.split(":", 1)[1]: v for k, v in art.items() if k.startswith("category:") and isinstance(v, Mapping)}
    project = art.get("project") if isinstance(art.get("project"), Mapping) else None
    if project is None:
        ledger_project = (art.get("ledger") or {}).get("project") if isinstance(art.get("ledger"), Mapping) else None
        project = dict(ledger_project) if isinstance(ledger_project, Mapping) else None
    measure = art.get("measurements") if isinstance(art.get("measurements"), Mapping) else art.get("measure")

    def _pdf(doc_id: str) -> bytes | None:
        try:
            return rctx.docs.get(doc_id)
        except Exception:  # noqa: BLE001
            return None

    return ScopeInputs(docs, project=project, category_outputs=cats,
                       measure=measure if isinstance(measure, Mapping) else None,
                       main_output=art.get("main") if isinstance(art.get("main"), Mapping) else None, pdf_bytes=_pdf)


def replay_prepare_stage(rctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    """Replay stage `derive` (scope part): the ScopeContext from the bundle's
    documents and category outputs, stored as artifact `scope_context`."""
    settings = _settings(None)
    overrides = dict(getattr(rctx, "settings_overrides", {}) or {})
    if overrides:
        settings = settings.model_copy(update=overrides) if hasattr(settings, "model_copy") else settings
    ctx = prepare_context(_replay_inputs(rctx, art), settings=settings, allow_llm=False)
    return {"scope_context": ctx}


def replay_enforce_stage(rctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    """Replay stage `enforce`: the enforced main output, its deliverable
    specs and the scope report."""
    main = art.get("main")
    ctx = art.get("scope_context")
    if not isinstance(main, Mapping) or not isinstance(ctx, Mapping):
        rctx.note("enforce: no main output or no scope context; nothing enforced")
        return {}
    settings = _settings(None)
    overrides = dict(getattr(rctx, "settings_overrides", {}) or {})
    if overrides and hasattr(settings, "model_copy"):
        settings = settings.model_copy(update=overrides)
    enforced = enforce(main, ctx, scope_policy.active_policy().doc, (), settings=settings)
    out: dict[str, Any] = {"main": enforced, "scope_report": enforced["scope_policy"],
                           "excluded_lines": enforced["excluded_lines"]}
    try:
        from app.services.plans_reader_runs import deliverable_spec

        for kind, key in (("boq", "boq_spec"), ("estimate", "estimate_spec"), ("brief", "brief_spec"),
                          ("marked_plans", "marked_plans_spec")):
            spec = deliverable_spec(kind, dict(enforced))
            if spec is not None:
                out[key] = spec
    except Exception as exc:  # noqa: BLE001 - specs are a convenience in replay
        rctx.note(f"enforce: deliverable specs not rebuilt ({type(exc).__name__})")
    return out


def register_replay_stages() -> None:
    """Register `derive` (scope part, chained before a WS3 derive stage when
    one is registered) and `enforce` in the WS0 replay registry. Explicit,
    never at import, so replay tests that expect the legacy entries are not
    affected by importing this module."""
    from app.services import plans_reader_replay as replay

    prior = replay.registered_stages().get("derive")
    chained = prior if prior is not None and not prior.legacy and prior.owner != "WS5" else None

    def derive_stage(rctx: Any, art: dict[str, Any]) -> dict[str, Any]:
        produced = dict(replay_prepare_stage(rctx, art))
        if chained is not None:
            art2 = {**art, **produced}
            produced.update(chained.fn(rctx, art2) or {})
        return produced

    replay.register_stage("derive", derive_stage, version=ENGINE_VERSION,
                          reads=("documents", "category:*", "ledger", "main"),
                          writes=("scope_context",) + (tuple(chained.writes) if chained else ()), owner="WS5")
    replay.register_stage("enforce", replay_enforce_stage, version=ENGINE_VERSION,
                          reads=("main", "scope_context"),
                          writes=("main", "scope_report", "excluded_lines", "boq_spec", "estimate_spec", "brief_spec",
                                  "marked_plans_spec"), owner="WS5")


def summarize(ctx: Mapping[str, Any]) -> str:
    """A short JSON summary (logs and scripts)."""
    keep = {k: ctx.get(k) for k in ("policy_sha", "stats", "drawn_systems")}
    return json.dumps(keep, sort_keys=True, default=str)


__all__ = [
    "SCOPE_CONTEXT_SCHEMA", "SCOPE_REPORT_SCHEMA", "ENGINE_VERSION", "ScopeInputs", "build_statements",
    "drawn_systems", "breaker_plan", "device_facts", "compute_inclusions", "prepare_context", "prepare",
    "write_context", "render_digest", "rules_text", "PolicyHit", "evaluate", "classify_obligation", "enforce",
    "rewrite_question", "decided_topic_patterns", "proposal_lines", "context_for_run", "facts_for_run", "standards_for", "overrides_for_run",
    "enforce_main_output", "enforce_for_run", "apply_override", "mark_spec_edits", "ec_raceway_systems",
    "invalidate_policy_cache", "table_policy_loader", "replay_prepare_stage", "replay_enforce_stage",
    "register_replay_stages", "summarize",
]
