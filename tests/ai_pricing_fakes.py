"""In-memory Supabase stand-ins and synthetic fixtures for the WS6 pricing
tests (house convention of tests/test_plans_reader_routes.py, plus
`upsert` for the backfill). Synthetic numbers only."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.services import cost_library as cl
from app.services.electrical_items import parse_item


class _Query:
    def __init__(self, db: FakeDB, table: str):
        self.db, self.table = db, table
        self._op = "select"
        self._payload = None
        self._filters: list[tuple] = []
        self._orders: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None
        self._on_conflict: str | None = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def upsert(self, payload, on_conflict: str | None = None, **_k):
        self._op, self._payload, self._on_conflict = "upsert", payload, on_conflict
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def order(self, col, desc=False, **_k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **_k):
        self._limit = n
        return self

    def _match(self, row: dict) -> bool:
        for op, col, val in self._filters:
            have = row.get(col)
            if op == "eq" and have != val:
                return False
            if op == "neq" and have == val:
                return False
            if op == "in" and have not in val:
                return False
        return True

    def _insert_rows(self, payloads: list[dict]) -> list[dict]:
        rows = self.db.tables.setdefault(self.table, [])
        out = []
        for p in payloads:
            row = dict(p)
            row.setdefault("id", str(uuid.uuid4()))
            self.db.clock += 1
            row.setdefault("created_at", f"2026-10-21T12:00:00.{self.db.clock:06d}+00:00")
            for uq in self.db.unique.get(self.table, ()):
                key = tuple(row.get(c) for c in uq)
                if any(tuple(r.get(c) for c in uq) == key for r in rows):
                    exc = Exception(f"duplicate key value violates unique constraint ({', '.join(uq)}) 23505")
                    exc.code = "23505"  # type: ignore[attr-defined]
                    raise exc
            rows.append(row)
            out.append(dict(row))
        return out

    def execute(self):
        if self.table in self.db.missing:
            exc = Exception(f"PGRST205: table {self.table} not found")
            exc.code = "PGRST205"  # type: ignore[attr-defined]
            raise exc
        rows = self.db.tables.setdefault(self.table, [])
        self.db.ops.append((self.table, self._op, self._payload, list(self._filters)))
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            return SimpleNamespace(data=self._insert_rows(payloads))
        if self._op == "upsert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            cols = tuple(c.strip() for c in (self._on_conflict or "id").split(","))
            out = []
            for p in payloads:
                key = tuple(p.get(c) for c in cols)
                hit = next((r for r in rows if tuple(r.get(c) for c in cols) == key), None)
                if hit is not None:
                    hit.update(p)
                    out.append(dict(hit))
                else:
                    out.extend(self._insert_rows([p]))
            return SimpleNamespace(data=out)
        hits = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in hits:
                r.update(self._payload)
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self._op == "delete":
            self.db.tables[self.table] = [r for r in rows if r not in hits]
            return SimpleNamespace(data=[dict(r) for r in hits])
        for col, desc in reversed(self._orders):
            hits.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        if self._range is not None:
            hits = hits[self._range[0] : self._range[1] + 1]
        if self._limit is not None:
            hits = hits[: self._limit]
        return SimpleNamespace(data=[dict(r) for r in hits])


class FakeDB:
    """Tables in memory. `unique` maps a table to its unique column tuples
    (a duplicate insert raises 23505 like PostgREST); `missing` names
    tables that raise PGRST205 (a database before migration ME)."""

    def __init__(self, tables: dict | None = None, *, unique: dict | None = None, missing: set | None = None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.ops: list[tuple] = []
        self.clock = 0
        self.unique = {
            "plans_reader_pricing": (("run_id", "version"),),
            "plans_reader_priced_lines": (("pricing_id", "line_id"),),
            "vendor_quote_extractions": (("run_id", "file_sha256"),),
            "cost_items": (("item_key", "uom"),),
            **(unique or {}),
        }
        self.missing = set(missing or ())

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def rows(self, name: str) -> list[dict]:
        return self.tables.get(name, [])


# ── Synthetic pricing fixtures ─────────────────────────────────────────────


def obs(project: str, desc: str, unit: str, hours: float, material: float, when: dt.date = dt.date(2026, 5, 1)) -> cl.Observation:
    ident = parse_item(desc, unit)
    return cl.Observation(
        project_id=project, item_key=cl.library_key(ident), family=cl.library_family(ident),
        item_class=ident.item_class, uom=ident.uom, unit_labor_hours=hours,
        unit_material_cost=material, observed_on=when, description=desc,
    )


LIBRARY_ITEMS: tuple[tuple[str, str, float, float], ...] = (
    ("#12 THHN/THWN Cu", "FT", 0.01, 0.20),
    ("#10 THHN/THWN Cu", "FT", 0.012, 0.30),
    ('3/4"C EMT', "FT", 0.03, 1.00),
    ("DUPLEX RECEPTACLE", "EA", 0.40, 10.00),
    ("DUPLEX RECEPTACLE, GFCI", "EA", 0.45, 20.00),
    ("ELECTRICAL CONNECTION", "EA", 1.00, 50.00),
    ("L1 - 2' X 4' RECESSED TROFFER - LED", "EA", 1.00, 100.00),
    ("L3 - 6 INCH RECESSED DOWNLIGHT - LED", "EA", 0.75, 60.00),
    ("X1 - EXIT SIGN - LED", "EA", 0.75, 80.00),
    ("CEILING OCCUPANCY SENSOR", "EA", 0.50, 45.00),
    ("WALL DIMMER", "EA", 0.40, 35.00),
    ("REMOVAL OF LIGHT FIXTURE", "EA", 0.50, 0.0),
    ("REMOVAL OF POWER DEVICES", "EA", 0.30, 0.0),
    ("PANEL LP1, 225A MLO", "EA", 8.00, 2000.00),
)


def library(exclude=(), projects: int = 12) -> cl.CostLibrary:
    rows = []
    for i in range(1, projects + 1):
        for desc, unit, hours, material in LIBRARY_ITEMS:
            rows.append(obs(f"p{i}", desc, unit, hours, material))
    return cl.CostLibrary.from_observations(rows, exclude_projects=exclude)


def item(description: str, quantity: float, unit: str = "EA", **extra) -> dict:
    base = {
        "description": description, "quantity": quantity, "unit": unit, "sheet_refs": ["E3.01"],
        "caveat": None, "material_note": None, "material_category": extra.pop("material_category", "General Material"),
        "wastage_applies": extra.pop("wastage_applies", unit == "FT"), "is_accessory": False,
    }
    base.update(extra)
    return base


def boq_spec(*, groups: list[tuple[str, str, list[dict]]] | None = None, areas: list[str] | None = None) -> dict:
    """A BOQ spec with a DEMOLITION section and NEW WORK groups. `groups`
    is [(group name, subgroup name, items)] for NEW WORK."""
    new_work = groups if groups is not None else [
        ("WIRING", "CONDUCTORS (AWG)", [item("#12 THHN/THWN Cu", 1000, "FT")]),
        ("WIRING", "CONDUITS", [item('3/4"C EMT', 500, "FT")]),
        ("OTHER ITEMS", "RECEPTACLES", [item("DUPLEX RECEPTACLE", 20)]),
        ("LIGHTING FIXTURES", "FIXTURES", [
            item("L1, 2X4 RECESSED LED TROFFER", 16, material_category="Lighting"),
            item("L1, 2X4 RECESSED LED TROFFER, LIFE SAFETY", 5, material_category="Lighting"),
            item("L3, 6-INCH RECESSED LED DOWNLIGHT", 13, material_category="Lighting"),
            item("L3, 6-INCH RECESSED LED DOWNLIGHT, SHADED", 3, material_category="Lighting"),
            item("L5, UNDERCABINET LED LIGHTING RUN", 7, material_category="Lighting"),
            item("X1, LED EDGE-LIT EXIT SIGN", 2, material_category="Lighting"),
        ]),
        ("LIGHTING CONTROLS", "CONTROLS", [
            item("CEILING OCCUPANCY SENSOR", 3, material_category="Lighting"),
            item("WALL DIMMER", 10, material_category="Lighting"),
        ]),
    ]
    by_group: dict[str, list[dict]] = {}
    for gname, sname, items in new_work:
        by_group.setdefault(gname, []).append({"name": sname, "note": None, "items": items})

    def area(name: str) -> dict:
        return {
            "area": name,
            "notes": [],
            "sections": [
                {"heading": "DEMOLITION", "note": None, "groups": [
                    {"name": "DEMOLITION", "note": None, "subgroups": [
                        {"name": "", "note": None, "items": [
                            item("REMOVAL OF LIGHT FIXTURE", 10, material_category="Demolition"),
                            item("REMOVAL OF POWER DEVICES", 4, material_category="Demolition"),
                        ]},
                    ]},
                ]},
                {"heading": "NEW WORK", "note": None, "groups": [
                    {"name": g, "note": None, "subgroups": sgs} for g, sgs in by_group.items()
                ]},
            ],
        }

    return {"title": "BOQ", "subtitle": None, "areas": [area(a) for a in (areas or ["Main"])], "closing_notes": []}


def estimate_spec(**over) -> dict:
    base = {
        "project_name": "Test Project", "address": "1 Test Way", "include_standard_allowances": True,
        "extra_allowances": [], "general_notes": ["Synthetic."], "area_notes": [],
    }
    base.update(over)
    return base


QUOTE_LINES: tuple[str, ...] = (
    "PREPARED BY", "Someone - Graybar - Las Vegas", "Qty Type Model Number & Notes",
    "21 L1", "16 L3", "50 L5 (PER FOOT UNDERCAB)", "2 X1",
    "1 CONTROLS LOT LIGHTING CONTROLS -INCLUDES 1/2 OF COMMISSIONING",
    "- NOTE EM BATTERIES NOT INCLUDED. EM ON LIFE SAFETY CIRCUIT PER PLANS",
    "Grand Total $4,200.00", "10. SALES TAX NOT INCLUDED.", "Quote Number : 1-2-3",
)


def quote_row(run_id: str, *, category: str = "Lighting", lines=QUOTE_LINES, selected: bool = False, status: str | None = None) -> dict:
    from app.services import vendor_quotes as vq

    parsed = vq.parse_quote_text(list(lines))
    row = {
        "id": str(uuid.uuid4()), "run_id": run_id, "file_sha256": uuid.uuid4().hex * 2, "filename": "quote.pdf",
        "storage_path": f"plans-reader/{run_id}/quotes/quote.pdf", "category": category, "selected": selected,
        **parsed.to_row(),
    }
    if status:
        row["status"] = status
    return row


ZERO = Decimal(0)
