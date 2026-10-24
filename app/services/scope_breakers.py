"""Breaker plan from the WS1 panel schedules (docs/AI_ESTIMATING_BUILD_SPEC.md
WS5; fixes E12).

WS1 is the single panel parser (`pagefacts.schedules`); this module only
reads its PanelSchedule shape (`schedule["panel"]["circuits"]`, each circuit
with `status` decided by positive evidence on the row: `new` only from a
NEW-breaker keynote or a NEW description).

- Keyed NEW rows are breakers to buy: one BOQ line per (trip, poles, GFCI,
  AFCI), quantity = the number of positions (a multi-pole breaker is one).
- New panels: every non-spare, non-space position is a breaker to buy.
- Existing-panel positions that carry new loads (bold or filled wire and
  voltage-drop cells, a named load) and no key are NOT bought: they are
  counted into `needs_review: breaker_reuse_assumed` with the count and the
  circuits in the clarification.
- A position whose KEY cell is set but whose keynote text could not be
  resolved keeps the model's breaker rows for those circuits plus a warning.
- A WS1 panel with no circuit carrying a trip or pole rating is not a panel
  schedule (run 785ae29e: general-note text on E0.00 parsed as panel "AND"
  with 4 unrated positions, which became "CIRCUIT BREAKER 4 EA"); it is
  dropped with a `panel_unrated` warning.
- Panel schedules the reader transcribed (category output S rows with
  schedule_type panel, e.g. "Branch Panel: A") are parsed here when WS1 has no
  usable panel of the same name (WS1 found no schedule on 7172 E0.02): the
  header (name, volts, phase, wires, bus amps, MLO or MCB, AIC, mounting,
  enclosure) from the schedule notes, and each circuit row's trip and poles
  for both sides; a multi-pole breaker spans 2 or 3 circuit rows and counts
  once. SPARE and SPACE rows count only with a trip rating on the row.
- A new panel (not marked (E) or EXISTING) gives a canonical panel line
  ("PANEL A, 277/480V, 3-PH, 4W, 200A MLO TYPE, 14KAIC, 42 POLES, SURFACE
  MOUNT, NEMA 1") in `panel_lines`; enforce inserts it when no BOQ row names
  that panel.

Pure: schedules in, `BreakerPlan` out. Nothing here prices a breaker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

REUSE_REVIEW = "breaker_reuse_assumed"
UNRESOLVED_KEY_WARNING = "breaker_key_unresolved"
UNRATED_PANEL_WARNING = "panel_unrated"
EXISTING_LOAD_RE = re.compile(r"^\s*\(?E\)?\s*EXISTING\s+LOAD\s*$|^\s*EXISTING\s+LOAD\b", re.I)
SKIP_STATUSES = frozenset({"spare", "space"})
BREAKER_ROW_RE = re.compile(r"\bCIRCUIT\s+BREAKERS?\b|\b\d{2,4}\s*A\s*/\s*\d\s*P\b.*\bBREAKER\b|\bGFCI\s+BREAKER\b", re.I)
_NOT_BRANCH_BREAKER_RE = re.compile(r"\bMAIN\s+BREAKER\b|\bDISCONNECT\b|\bPANEL(?:BOARD)?\b(?!.*CIRCUIT\s+BREAKER)", re.I)


@dataclass
class BreakerLine:
    amps: int | None
    poles: int | None
    gfci: bool
    afci: bool
    quantity: int
    circuits: list[str]
    sheet: str | None
    basis: str  # keynote_new | new_panel | description_new

    @property
    def description(self) -> str:
        return breaker_description(self.amps, self.poles, gfci=self.gfci, afci=self.afci)

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "amps": self.amps,
            "poles": self.poles,
            "gfci": self.gfci,
            "afci": self.afci,
            "quantity": self.quantity,
            "unit": "EA",
            "circuits": list(self.circuits),
            "sheet": self.sheet,
            "basis": self.basis,
        }


@dataclass
class PanelLine:
    """One new panelboard to buy, described from its schedule header."""

    name: str
    header: dict[str, Any]
    sheet: str | None
    source: str  # ws1 | schedule_row
    ref: str | None = None  # the category S row id when read from the reader's schedule

    @property
    def description(self) -> str:
        return panel_description(self.name, self.header)

    def to_dict(self) -> dict[str, Any]:
        return {"description": self.description, "name": self.name, "header": dict(self.header), "quantity": 1,
                "unit": "EA", "sheet": self.sheet, "source": self.source, "ref": self.ref, "basis": "new_panel"}


@dataclass
class BreakerPlan:
    status: str  # resolved | partial | unavailable
    lines: list[BreakerLine] = field(default_factory=list)
    panel_lines: list[PanelLine] = field(default_factory=list)
    reuse_circuits: list[str] = field(default_factory=list)
    unresolved_circuits: list[str] = field(default_factory=list)
    keynotes: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    panels: list[str] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)

    @property
    def reuse_count(self) -> int:
        return len(self.reuse_circuits)

    @property
    def new_count(self) -> int:
        return sum(ln.quantity for ln in self.lines)

    def clarification(self) -> str | None:
        if self.status == "unavailable":
            return None
        parts = []
        if self.lines:
            for ln in self.lines:
                cites = ", ".join(ln.circuits)
                parts.append(f"{ln.quantity} EA {ln.description} ({cites})")
            text = "Branch breakers carried: " + "; ".join(parts) + "."
        else:
            text = "No new branch breakers are keyed on the panel schedules."
        if self.reuse_circuits:
            text += (
                f" {self.reuse_count} existing panel positions with new loads and no new-breaker key are assumed "
                f"to reuse the existing breakers ({', '.join(self.reuse_circuits)})."
            )
        if self.panel_lines:
            text += " New panels carried: " + "; ".join(p.description for p in self.panel_lines) + "."
        sheet = ", ".join(self.sheets)
        if sheet:
            text += f" Per {sheet}."
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lines": [ln.to_dict() for ln in self.lines],
            "panel_lines": [p.to_dict() for p in self.panel_lines],
            "new_count": self.new_count,
            "reuse_count": self.reuse_count,
            "reuse_circuits": list(self.reuse_circuits),
            "unresolved_circuits": list(self.unresolved_circuits),
            "keynotes": dict(self.keynotes),
            "warnings": list(self.warnings),
            "panels": list(self.panels),
            "sheets": list(self.sheets),
            "clarification": self.clarification(),
            "needs_review": REUSE_REVIEW if self.reuse_circuits else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> BreakerPlan:
        d = dict(data or {})
        return cls(
            status=str(d.get("status") or "unavailable"),
            lines=[
                BreakerLine(
                    amps=ln.get("amps"), poles=ln.get("poles"), gfci=bool(ln.get("gfci")), afci=bool(ln.get("afci")),
                    quantity=int(ln.get("quantity") or 0), circuits=list(ln.get("circuits") or []),
                    sheet=ln.get("sheet"), basis=str(ln.get("basis") or "keynote_new"),
                )
                for ln in d.get("lines") or []
            ],
            panel_lines=[
                PanelLine(name=str(p.get("name") or ""), header=dict(p.get("header") or {}), sheet=p.get("sheet"),
                          source=str(p.get("source") or "ws1"), ref=p.get("ref"))
                for p in d.get("panel_lines") or [] if isinstance(p, Mapping) and p.get("name")
            ],
            reuse_circuits=list(d.get("reuse_circuits") or []),
            unresolved_circuits=list(d.get("unresolved_circuits") or []),
            keynotes=dict(d.get("keynotes") or {}),
            warnings=list(d.get("warnings") or []),
            panels=list(d.get("panels") or []),
            sheets=list(d.get("sheets") or []),
        )


def breaker_description(amps: int | None, poles: int | None, *, gfci: bool = False, afci: bool = False) -> str:
    """'20A/1P CIRCUIT BREAKER, GFCI' (the G3 estimator's wording)."""
    head = f"{amps}A/{poles}P CIRCUIT BREAKER" if amps and poles else "CIRCUIT BREAKER"
    mods = (["GFCI"] if gfci else []) + (["AFCI"] if afci else [])
    return head + (", " + ", ".join(mods) if mods else "")


def panel_description(name: str, header: Mapping[str, Any]) -> str:
    """'PANEL A, 277/480V, 3-PH, 4W, 200A MLO TYPE, 14KAIC, 42 POLES, SURFACE
    MOUNT, NEMA 1' (the G3 estimator's wording); parts the header lacks are
    left out, never guessed."""
    parts = [f"PANEL {name}".strip()]
    if header.get("volts"):
        parts.append(f"{header['volts']}V")
    if header.get("phase"):
        parts.append(f"{header['phase']}-PH")
    if header.get("wires"):
        parts.append(f"{header['wires']}W")
    main = str(header.get("main_type") or "").upper()
    amps = header.get("main_amps") if main == "MCB" and header.get("main_amps") else header.get("bus_amps")
    if main or amps:
        parts.append(" ".join(x for x in (f"{amps}A" if amps else "", f"{main} TYPE" if main else "") if x))
    if header.get("aic_ka"):
        parts.append(f"{_num_text(header['aic_ka'])}KAIC")
    if header.get("positions"):
        parts.append(f"{header['positions']} POLES")
    if header.get("mounting"):
        parts.append(f"{str(header['mounting']).upper()} MOUNT")
    if header.get("enclosure"):
        parts.append(str(header["enclosure"]).upper())
    return ", ".join(parts)


def _num_text(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _new_load(circuit: Mapping[str, Any]) -> bool:
    desc = str(circuit.get("desc") or "").strip()
    if not desc or EXISTING_LOAD_RE.search(desc):
        return False
    signals = circuit.get("signals") or {}
    wire = circuit.get("wire") or {}
    return bool(signals.get("wire_filled") or signals.get("vd_filled") or wire.get("hot"))


def _keynote_resolved(circuit: Mapping[str, Any]) -> bool:
    return "keynote" in str(circuit.get("status_basis") or "").lower()


def panel_schedules(schedules: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [s for s in schedules if isinstance(s, Mapping) and s.get("kind") == "panel" and isinstance(s.get("panel"), Mapping)]


# Panel schedules the reader transcribed (category output S rows) -------------------------------

_EXISTING_NAME_RE = re.compile(r"\(\s*E\s*\)|\bEXISTING\b|\bEXIST\.?(?=\s)", re.I)
_NAME_NOISE_RE = re.compile(
    r"\(\s*[EN]\s*\)|\bEXISTING\b|\bNEW\b|\bBRANCH\b|\bPANEL(?:BOARD)?S?\b|\bSCHEDULE\b|\bLOAD\s+CENTER\b|"
    r"\bDISTRIBUTION\b|\bLIGHTING\b(?=\s*[:\-]?\s*$)|[:#'\"]", re.I)
_CKT_COL_RE = re.compile(r"^\s*(?:CKT|CCT|CIR(?:CUIT)?)\.?\s*(?:NO\.?|#|NUM(?:BER)?)?\s*$", re.I)
_DESC_COL_RE = re.compile(r"DESC|^\s*LOAD(?:\s+NAME)?\s*$|^\s*(?:LOAD\s+)?SERV(?:ED|ING)\b|^\s*EQUIPMENT\s*$", re.I)
_TRIP_COL_RE = re.compile(
    r"^\s*(?:TRIP|OCPD|OCPD\s*(?:AMPS?|\(A\)|SIZE)|AMPS?|BKR|BREAKER|C\.?B\.?|TRIP\s*\(?A\)?|BKR\s*(?:SIZE|AMPS?))\s*$", re.I)
_POLE_COL_RE = re.compile(r"^\s*(?:P|POLES?|#\s*P|NO\.?\s*(?:OF\s*)?POLES?|PL)\s*$", re.I)
_DASH_CELL_RE = re.compile(r"^\s*[-_=.\"']{1,6}\s*$")
_SPACE_RE = re.compile(r"^\s*(?:SPACE|SPACES|SPC)\b", re.I)
_SPARE_RE = re.compile(r"^\s*SPARE\b", re.I)
_AMPS_CELL_RE = re.compile(r"^\s*(\d{1,4})\s*(?:A|AMPS?|AT)?\s*$", re.I)


def _header_from_text(text: str) -> dict[str, Any]:
    """Volts, phase, wires, bus and main amps, MLO/MCB, AIC (kA), mounting and
    enclosure from a panel schedule's header text (the reader's schedule
    notes: "Volts: 277/480; Phases: 3; Wires: 4; A.I.C. Rating: 14,000; Mains
    Type: MLO; Mains Rating: 200 A", or "400A MLO, 480/277 Wye, 3-phase,
    4-wire; SCCR 65kA; Surface, NEMA 1")."""
    t = str(text or "")
    out: dict[str, Any] = {}
    m = re.search(r"\bVOLTS?(?:AGE)?\s*[:=]\s*(\d{3}\s*/\s*\d{3}|\d{3})", t, re.I) or re.search(
        r"\b(\d{3}\s*/\s*\d{3})\s*V?\b", t)
    if m:
        out["volts"] = re.sub(r"\s+", "", m.group(1))
    m = re.search(r"\bPHASES?\s*[:=]\s*(\d)\b", t, re.I) or re.search(r"\b(\d)\s*-?\s*(?:PHASE|PH)\b", t, re.I)
    if m:
        out["phase"] = int(m.group(1))
    m = re.search(r"\bWIRES?\s*[:=]\s*(\d)\b", t, re.I) or re.search(r"\b(\d)\s*-?\s*(?:WIRE|W)\b", t, re.I)
    if m:
        out["wires"] = int(m.group(1))
    m = re.search(r"\bA\.?\s*I\.?\s*C\.?(?:\s*RATING)?\s*[:=]\s*([\d,]+)\s*(KA|K)?", t, re.I) or re.search(
        r"\b(?:SCCR|AIC|A\.I\.C\.)\s*[:=]?\s*([\d,]+)\s*(KA|K)\b", t, re.I) or re.search(
        r"\b([\d,]+)\s*(KA|K)?\s*A\.?I\.?C\b", t, re.I)
    if m:
        val = float(m.group(1).replace(",", ""))
        if not m.group(2) and val >= 1000:
            val = val / 1000.0
        if val > 0:
            out["aic_ka"] = val
    m = re.search(r"\bMAINS?\s*TYPE\s*[:=]\s*(MLO|MCB|M\.?L\.?O|M\.?C\.?B)", t, re.I) or re.search(
        r"\b(MLO|MCB)\b", t, re.I)
    if m:
        out["main_type"] = m.group(1).replace(".", "").upper()
    m = re.search(r"\b(?:MAINS?|BUS)\s*(?:RATING|AMPS?|SIZE)\s*[:=]\s*(\d{2,4})\s*A?", t, re.I) or re.search(
        r"\b(\d{2,4})\s*A\s*(?:MLO|MCB|BUS)\b", t, re.I)
    if m:
        out["bus_amps"] = int(m.group(1))
    m = re.search(r"\bMCB\s*(?:RATING|AMPS?|SIZE)\s*[:=]\s*(\d{2,4})\s*A?", t, re.I) or re.search(
        r"\b(\d{2,4})\s*A\s*MCB\b", t, re.I)
    if m:
        out["main_amps"] = int(m.group(1))
    m = re.search(r"\bMOUNTING\s*[:=]\s*(SURFACE|FLUSH|RECESSED|SEMI-?FLUSH)", t, re.I) or re.search(
        r"\b(SURFACE|FLUSH)\b", t, re.I)
    if m:
        out["mounting"] = m.group(1).upper()
    m = re.search(r"\bENCLOSURE\s*[:=]\s*(?:TYPE|NEMA)\s*(\d{1,2}[A-Z]?)\b", t, re.I) or re.search(
        r"\bNEMA\s*(\d{1,2}[A-Z]?)\b", t, re.I)
    if m:
        out["enclosure"] = f"NEMA {m.group(1).upper()}"
    return out


def _ws1_header(panel: Mapping[str, Any]) -> dict[str, Any]:
    main = panel.get("main") if isinstance(panel.get("main"), Mapping) else {}
    out: dict[str, Any] = {
        "volts": str(panel.get("volts")).replace(" ", "") if panel.get("volts") else None,
        "phase": panel.get("phase"), "wires": panel.get("wires"), "bus_amps": panel.get("bus_amps"),
        "main_type": str(main.get("type") or "").upper() or None, "main_amps": main.get("amps"),
        "aic_ka": panel.get("aic_ka"), "mounting": panel.get("mounting"), "enclosure": panel.get("enclosure"),
    }
    if not out["bus_amps"] and out["main_amps"] and out["main_type"] == "MLO":
        out["bus_amps"] = out["main_amps"]
    nums = [n for c in panel.get("circuits") or [] if isinstance(c, Mapping)
            for n in (c.get("ckts") or [c.get("ckt")]) if isinstance(n, int)]
    if nums:
        out["positions"] = max(nums)
    return {k: v for k, v in out.items() if v not in (None, "")}


def panel_name(text: Any) -> str:
    """'Branch Panel: A' -> 'A'; '(E) PANEL 1HD' -> '1HD'."""
    name = _NAME_NOISE_RE.sub(" ", str(text or ""))
    name = re.sub(r"\s+", " ", name).strip(" -,.;")
    return name.upper()


def _ckt_num(cell: Any) -> int | None:
    m = re.match(r"^\s*(\d{1,3})\b", str(cell or ""))
    return int(m.group(1)) if m else None


def _amps(cell: Any) -> int | None:
    m = _AMPS_CELL_RE.match(str(cell or ""))
    return int(m.group(1)) if m and 0 < int(m.group(1)) <= 4000 else None


def _poles(cell: Any) -> int | None:
    m = re.match(r"^\s*([123])\s*P?\s*$", str(cell or ""), re.I)
    return int(m.group(1)) if m else None


def _blank(cell: Any) -> bool:
    return not str(cell or "").strip() or bool(_DASH_CELL_RE.match(str(cell or "")))


def _side_columns(columns: list[str]) -> list[dict[str, int]]:
    """Column indices (ckt, desc, trip, poles) of the left and right halves of a
    two-sided panel schedule (one side when there is one CKT column)."""
    def idx(rx: re.Pattern[str]) -> list[int]:
        return [i for i, c in enumerate(columns) if rx.search(str(c or ""))]

    ckt, desc, trip, poles = idx(_CKT_COL_RE), idx(_DESC_COL_RE), idx(_TRIP_COL_RE), idx(_POLE_COL_RE)
    if not ckt or not trip:
        return []
    sides = []
    left = {"ckt": ckt[0], "trip": trip[0]}
    if desc:
        left["desc"] = desc[0]
    if poles:
        left["poles"] = poles[0]
    sides.append(left)
    if len(ckt) > 1 and len(trip) > 1:
        right = {"ckt": ckt[-1], "trip": trip[-1]}
        if len(desc) > 1:
            right["desc"] = desc[-1]
        if len(poles) > 1:
            right["poles"] = poles[-1]
        sides.append(right)
    return sides


def panel_from_schedule_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """A reader-transcribed panel schedule (category output S row, schedule_type
    panel) as a WS1-shaped PanelSchedule: {kind, page, sheet, source, ref,
    panel{name, existing, header, circuits[{ckt, side, desc, trip_a, poles,
    status}]}}. None when no circuit column, trip column or rated circuit is
    found. Continuation rows of a multi-pole breaker ('--' cells, or any row
    inside the span) are not circuits of their own."""
    if str(row.get("schedule_type") or "").lower() != "panel":
        return None
    if str(row.get("status") or "active").lower() not in ("active", ""):
        return None
    columns = [str(c or "") for c in row.get("columns") or []]
    sides = _side_columns(columns)
    if not sides:
        return None
    raw_name = str(row.get("name") or "")
    name = panel_name(raw_name) or panel_name(row.get("schedule_id"))
    if not name:
        return None
    rows = [r for r in row.get("rows") or [] if isinstance(r, list | tuple)]
    circuits: list[dict[str, Any]] = []
    for side_no, cols in enumerate(sides):
        side = "L" if side_no == 0 else "R"
        parsed = []
        for r in rows:
            def cell(key: str, r: Any = r, cols: dict[str, int] = cols) -> Any:
                i = cols.get(key)
                return r[i] if i is not None and i < len(r) else None

            n = _ckt_num(cell("ckt"))
            if n is None:
                continue
            parsed.append((n, cell("desc"), cell("trip"), cell("poles")))
        parsed.sort(key=lambda x: x[0])
        covered: set[int] = set()
        for pos, (n, desc, trip, pole_cell) in enumerate(parsed):
            if n in covered:
                continue
            amps = _amps(trip)
            poles = _poles(pole_cell)
            text = str(desc or "").strip()
            status = "space" if _SPACE_RE.match(text) else ("spare" if _SPARE_RE.match(text) else "new")
            if amps is None:
                if status in ("space", "spare") or _blank(desc):
                    continue  # a space, an unrated spare, a blank or a continuation row
            elif poles is None:
                # poles inferred from the explicit continuation rows that follow
                span = 1
                for n2, d2, t2, _p2 in parsed[pos + 1:pos + 3]:
                    if n2 == n + 2 * span and _blank(d2) and _blank(t2):
                        span += 1
                    else:
                        break
                poles = span if span > 1 else None
            if amps is None:
                circuits.append({"ckt": n, "side": side, "desc": text, "trip_a": None, "poles": poles,
                                 "status": "unknown", "keynotes": [], "gfci": False})
                continue
            for k in range(1, (poles or 1)):
                covered.add(n + 2 * k)
            circuits.append({"ckt": n, "side": side, "desc": text, "trip_a": amps, "poles": poles,
                             # a load row is "unknown" like a WS1 row without positive evidence: new only
                             # because its panel is new (an (E) panel's rows give no lines)
                             "status": "unknown" if status == "new" else status, "spare_rated": status != "new",
                             "keynotes": [], "gfci": bool(re.search(r"\bGFC?I\b", text, re.I))})
    if not any(c.get("trip_a") for c in circuits):
        return None
    circuits.sort(key=lambda c: c["ckt"])
    header = _header_from_text(str(row.get("notes") or ""))
    nums = [_ckt_num(r[cols["ckt"]]) for r in rows for cols in sides if cols["ckt"] < len(r)]
    nums = [n for n in nums if n is not None]
    if nums:
        header["positions"] = max(nums)
    sheet = row.get("sheet_number")
    if not sheet and isinstance(row.get("sheet"), Mapping):
        sheet = row["sheet"].get("sheet_number")
    return {"kind": "panel", "page": row.get("page"), "sheet": sheet, "source": "schedule_row",
            "ref": row.get("row_id"),
            "panel": {"name": name, "existing": bool(_EXISTING_NAME_RE.search(raw_name)), "header": header,
                      "circuits": circuits}}


def _ws1_usable(panel: Mapping[str, Any]) -> bool:
    return any(isinstance(c, Mapping) and (c.get("trip_a") or c.get("poles")) for c in panel.get("circuits") or [])


def plan_breakers(
    schedules: Iterable[Mapping[str, Any]],
    *,
    sheet_for_page: Mapping[int, str] | None = None,
    schedule_rows: Iterable[Mapping[str, Any]] = (),
) -> BreakerPlan:
    """The breaker plan of every parsed panel schedule (see module doc).
    `schedules` are WS1 schedules; `schedule_rows` the reader's S rows (any
    schedule type; only panel rows are read), used for panels WS1 did not
    parse."""
    warnings: list[str] = []
    panels: list[Mapping[str, Any]] = []
    sheets = dict(sheet_for_page or {})
    for sched in panel_schedules(schedules):
        if _ws1_usable(sched["panel"]):
            panels.append(sched)
            continue
        page = sched.get("page")
        warnings.append(
            f"{UNRATED_PANEL_WARNING}: WS1 panel {sched['panel'].get('name') or '?'}"
            + (f" ({sheets.get(int(page)) or 'page ' + str(page)})" if page is not None else "")
            + " has no circuit with a trip or pole rating; not read as a panel schedule")
    ws1_names = {panel_name(s["panel"].get("name")) for s in panels}
    by_name: dict[str, dict[str, Any]] = {}
    for row in schedule_rows:
        if not isinstance(row, Mapping):
            continue
        parsed = panel_from_schedule_row(row)
        if parsed is None:
            continue
        name = parsed["panel"]["name"]
        prev = by_name.get(name)
        if prev is None or len(parsed["panel"]["circuits"]) > len(prev["panel"]["circuits"]):
            by_name[name] = parsed
    header_rows = dict(by_name)
    panels.extend(v for k, v in by_name.items() if k not in ws1_names)
    if not panels:
        return BreakerPlan(status="unavailable", warnings=warnings or ["no parsed panel schedule"])
    groups: dict[tuple[Any, ...], BreakerLine] = {}
    plan = BreakerPlan(status="resolved", warnings=warnings)
    for sched in panels:
        panel = sched["panel"]
        name = str(panel.get("name") or "PANEL")
        plan.panels.append(name)
        sheet = sched.get("sheet") or (
            sheets.get(int(sched.get("page") or 0)) if sched.get("page") is not None else None)
        if sheet and sheet not in plan.sheets:
            plan.sheets.append(sheet)
        existing = bool(panel.get("existing"))
        if not existing:
            if sched.get("source") == "schedule_row":
                header = dict(panel.get("header") or {})
            else:
                header = _ws1_header(panel)
                extra = header_rows.get(panel_name(name))
                for k, v in ((extra or {}).get("panel", {}).get("header") or {}).items():
                    header.setdefault(k, v)
            plan.panel_lines.append(PanelLine(name=panel_name(name) or name, header=header, sheet=sheet,
                                              source=str(sched.get("source") or "ws1"), ref=sched.get("ref")))
        for c in panel.get("circuits") or []:
            if not isinstance(c, Mapping):
                continue
            status = str(c.get("status") or "unknown")
            if status in SKIP_STATUSES and not (c.get("spare_rated") and c.get("trip_a")):
                continue
            label = f"{name}-{c.get('ckt')}"
            keys = [str(k) for k in c.get("keynotes") or [] if str(k).strip()]
            if status == "new" or (not existing and status not in ("existing", "existing_reused")):
                if sched.get("source") == "schedule_row":
                    basis = "new_panel"
                else:
                    basis = "keynote_new" if _keynote_resolved(c) else ("description_new" if status == "new" else "new_panel")
                if status == "new" and _keynote_resolved(c):
                    text = str(c.get("status_basis") or "").split(":", 1)[-1].strip()
                    for k in keys:
                        plan.keynotes.setdefault(f"{name}:{k}", text)
                amps = c.get("trip_a")
                poles = c.get("poles")
                gfci = bool(c.get("gfci"))
                afci = bool(re.search(r"\bAFCI\b|ARC\s+FAULT", f"{c.get('desc') or ''} {c.get('status_basis') or ''}", re.I))
                key = (amps, poles, gfci, afci)
                line = groups.get(key)
                if line is None:
                    line = groups[key] = BreakerLine(amps, poles, gfci, afci, 0, [], sheet, basis)
                line.quantity += 1
                line.circuits.append(label)
                continue
            if keys and not _keynote_resolved(c):
                plan.unresolved_circuits.append(label)
                continue
            if existing and status in ("unknown", "existing_reused") and _new_load(c):
                plan.reuse_circuits.append(label)
    plan.lines = sorted(groups.values(), key=lambda ln: (ln.amps or 0, ln.poles or 0, ln.gfci, ln.afci))
    if plan.unresolved_circuits:
        plan.status = "partial"
        plan.warnings.append(
            f"{UNRESOLVED_KEY_WARNING}: the KEY cell is set on {', '.join(plan.unresolved_circuits)} but the keynote "
            "text was not found; the model's breaker rows for those circuits are kept"
        )
    return plan


def is_breaker_row(description: str, *, group: str | None = None, subgroup: str | None = None) -> bool:
    """A branch circuit breaker BOQ row (never a panel, main or disconnect)."""
    text = str(description or "")
    if not BREAKER_ROW_RE.search(text):
        return False
    return not _NOT_BRANCH_BREAKER_RE.search(text) or "CIRCUIT BREAKER" in text.upper()


def names_panel(description: str, name: str) -> bool:
    """True when a BOQ row is the panelboard `name` ('PANEL A, 277/480V ...',
    'PANELBOARD "A"', 'PANEL LP-1 225A MLO'); never a breaker, feeder or
    circuit row that only mentions the panel."""
    text = str(description or "").upper()
    if not name or not re.search(r"\bPANEL(?:BOARD)?\b", text) or BREAKER_ROW_RE.search(text):
        return False
    if re.search(r"\b(?:FEEDER|CIRCUIT|CKT|WIRE|CONDUIT|HOME\s*RUN)\b", text):
        return False
    rx = rf"\bPANEL(?:BOARD)?\s*[:#]?\s*['\"]?{re.escape(name.upper())}(?![A-Z0-9])"
    return bool(re.search(rx, text))


def mentions_circuits(text: str, circuits: Iterable[str]) -> bool:
    """True when a row's text names one of `circuits` ('1EELC-9' or '1EELC
    circuits 9, 11')."""
    t = str(text or "").upper()
    for label in circuits:
        panel, _, num = label.rpartition("-")
        if label.upper() in t:
            return True
        if panel and panel.upper() in t:
            rest = t.replace(panel.upper(), " ")
            if re.search(rf"(?<![\w/.-]){re.escape(num)}(?![\w/.])", rest):
                return True
    return False


__all__ = [
    "REUSE_REVIEW", "UNRESOLVED_KEY_WARNING", "UNRATED_PANEL_WARNING", "BreakerLine", "BreakerPlan", "PanelLine",
    "breaker_description", "panel_description", "panel_name", "panel_from_schedule_row", "panel_schedules",
    "plan_breakers", "is_breaker_row", "mentions_circuits", "names_panel",
]
