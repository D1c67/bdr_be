"""plans_reader_notes service and backfill script (docs/ASSISTANT_PHASE4_PLAN.md
3.3, 3.4.1; WI-2). No database: a small in-memory PostgREST stand-in that
honours the 0180 unique key on upsert."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import plans_reader_notes as notes

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plans_outputs"
MIGRATION = ROOT / "supabase" / "migrations" / "0180_assistant_phase4.sql"
SCRIPT = ROOT / "scripts" / "backfill_plans_reader_notes.py"
D1 = "11111111-1111-4111-8111-111111111111"
UNIQUE = ("run_id", "source_scope", "category", "source_version", "source_key")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# -- Fake PostgREST -------------------------------------------------------------


class _Resp:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


def _cmp(a, b):
    try:
        return float(a), float(b)
    except (TypeError, ValueError):
        return str(a), str(b)


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.kw = "select", None, {}
        self.filters, self.orders = [], []
        self._limit = self._range = None

    def select(self, *_a, **_k):
        return self

    def insert(self, payload, **kw):
        self.op, self.payload, self.kw = "insert", payload, kw
        return self

    def upsert(self, payload, **kw):
        self.op, self.payload, self.kw = "upsert", payload, kw
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self, **kw):
        self.op, self.kw = "delete", kw
        return self

    def eq(self, c, v):
        self.filters.append(("eq", c, v))
        return self

    def in_(self, c, v):
        self.filters.append(("in", c, list(v)))
        return self

    def lt(self, c, v):
        self.filters.append(("lt", c, v))
        return self

    def gte(self, c, v):
        self.filters.append(("gte", c, v))
        return self

    def order(self, c, desc=False, **_k):
        self.orders.append((c, desc))
        return self

    def limit(self, n, **_k):
        self._limit = n
        return self

    def range(self, a, b, **_k):
        self._range = (a, b)
        return self

    def _match(self, r):
        for op, c, v in self.filters:
            have = r.get(c)
            if op == "eq" and str(have) != str(v):
                return False
            if op == "in" and str(have) not in {str(x) for x in v}:
                return False
            if op == "lt" and not (have is not None and _cmp(have, v)[0] < _cmp(have, v)[1]):
                return False
            if op == "gte" and not (have is not None and _cmp(have, v)[0] >= _cmp(have, v)[1]):
                return False
        return True

    def execute(self):
        db = self.db
        rows = db.tables.setdefault(self.table, [])
        db.calls.append((self.table, self.op, list(self.filters), self.kw))
        if self.op in ("insert", "upsert"):
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            keys = UNIQUE if self.table == notes.TABLE else None
            if self.op == "upsert" and self.table == notes.TABLE:
                assert self.kw.get("on_conflict") == notes.ON_CONFLICT
                assert self.kw.get("ignore_duplicates") is True
            out = []
            for item in items:
                row = copy.deepcopy(item)
                if keys and any(all(str(x.get(k)) == str(row.get(k)) for k in keys) for x in rows):
                    if self.op == "upsert":
                        continue
                    raise Exception("duplicate key (23505)")
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault("created_at", db.tick())
                rows.append(row)
                out.append(copy.deepcopy(row))
            return _Resp(out)
        matched = [r for r in rows if self._match(r)]
        if self.op == "delete":
            db.tables[self.table] = [r for r in rows if r not in matched]
            return _Resp([], count=len(matched))
        if self.op == "update":
            for r in matched:
                r.update(copy.deepcopy(self.payload))
            return _Resp(copy.deepcopy(matched))
        out = copy.deepcopy(matched)
        for c, desc in reversed(self.orders):
            out.sort(key=lambda r: (r.get(c) is None, str(r.get(c) or "")), reverse=desc)
        if self._range is not None:
            out = out[self._range[0] : self._range[1] + 1]
        if self._limit is not None:
            out = out[: self._limit]
        return _Resp(out)


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self._n = 0
        self._base = datetime(2026, 10, 20, tzinfo=timezone.utc)

    def tick(self) -> str:
        self._n += 1
        return (self._base + timedelta(seconds=self._n)).isoformat()

    def table(self, name):
        return FakeQuery(self, name)

    def add(self, table, **row):
        return self.table(table).insert(row).execute().data[0]

    def rows(self, table, **where):
        return [r for r in self.tables.get(table, []) if all(str(r.get(k)) == str(v) for k, v in where.items())]

    def writes(self, table=notes.TABLE):
        return [c for c in self.calls if c[0] == table and c[1] in ("insert", "upsert", "delete")]


RUN = "22222222-2222-4222-8222-222222222222"
RUN2 = "33333333-3333-4333-8333-333333333333"


def seed(db, run_id=RUN, *, category_v1=1, category_v2=None, main=1, brief=None):
    if category_v1:
        db.add("plans_reader_outputs", run_id=run_id, scope="category", category="electrical_drawings",
               version=category_v1, output=fixture("category_v1.json"))
    if category_v2:
        db.add("plans_reader_outputs", run_id=run_id, scope="category", category="low_voltage_drawings",
               version=category_v2, output=fixture("category_v2.json"))
    if main:
        db.add("plans_reader_outputs", run_id=run_id, scope="main", category=None, version=main,
               output=fixture("main.json"))
    if brief:
        db.add("plans_reader_deliverables", run_id=run_id, kind="brief", version=brief,
               spec_json=fixture("brief_deliverable.json"), filename="brief.docx", storage_path="x")


@pytest.fixture
def db():
    return FakeDB()


def _all_text(rows) -> str:
    return json.dumps(rows, default=str)


# -- Flattening -------------------------------------------------------------------


def test_v1_category_flattens_every_note_kind_and_skips_counts_and_runs():
    src = notes.Source("category", "electrical_drawings", 1, fixture("category_v1.json"))
    rows = notes.flatten(src, RUN)
    kinds = sorted({r["kind"] for r in rows})
    assert kinds == ["category_summary", "detail", "page_note", "question", "schedule", "scope", "spec"]
    assert sum(1 for r in rows if r["kind"] == "page_note") == 3
    text = _all_text(rows)
    assert "POSITIONS_MARKER" not in text and "RUNS_MARKER" not in text
    for r in rows:
        if r["kind"] != "category_summary":
            assert re.fullmatch(r"[a-z]+\[\d+\](\.notes\[\d+\])?:[0-9a-f]{16}", r["source_key"]), r["source_key"]
        assert set(r) == set(notes._COLUMNS)
    fa = next(r for r in rows if r["kind"] == "page_note" and r["page"] == 14)
    assert fa["sheet_number"] == "E-201" and fa["sheet_title"] == "E-201 FIRE ALARM PLAN" and fa["document_id"] == D1
    scope = next(r for r in rows if r["kind"] == "scope")
    assert scope["quote"] == "COMMISSIONING IS REQUIRED" and scope["section"] == "FA NOTES"
    assert scope["sheet_title"] == "E-201 FIRE ALARM PLAN"
    spec = next(r for r in rows if r["kind"] == "spec")
    assert spec["title"] == "Wiring devices" and "Hubbell" in spec["body"] and "Leviton" in spec["body"]
    sched = next(r for r in rows if r["kind"] == "schedule")
    assert "PANEL 1HD" in sched["body"] and "1 | FACP | 20 A" in sched["body"] and "Existing panel." in sched["body"]
    q = next(r for r in rows if r["kind"] == "question")
    assert "Why: Existing panel is unlabeled." in q["body"]


def test_v2_category_keys_rows_by_row_id_and_skips_superseded():
    src = notes.Source("category", "low_voltage_drawings", 2, fixture("category_v2.json"))
    rows = notes.flatten(src, RUN)
    keys = {r["source_key"] for r in rows if r["kind"] in ("scope", "spec", "question")}
    assert keys == {"O1", "P1", "Q1"}
    text = _all_text(rows)
    assert "SUPERSEDED_MARKER" not in text
    assert "0.98765" not in text and "0.4321" not in text and "positions" not in text
    spec = next(r for r in rows if r["source_key"] == "P1")
    assert spec["document_id"] == D1 and spec["page"] == 3 and spec["sheet_number"] == "T-101"
    assert spec["sheet_title"] == "T-101 DATA PLAN"
    assert all(r["category"] == "low_voltage_drawings" and r["source_version"] == 2 for r in rows)


def test_main_and_brief_rows_are_written_with_an_empty_category():
    main = notes.flatten(notes.Source("main", "", 1, fixture("main.json")), RUN)
    kinds = {r["kind"] for r in main}
    assert {"brief_summary", "system", "assumption", "exclusion", "risk", "rfi", "missing_document", "other_trade",
            "sheet_summary", "spec_highlight", "material_note", "confidence_note"} == kinds
    assert all(r["category"] == "" for r in main)
    risk = next(r for r in main if r["kind"] == "risk")
    assert risk["source_key"] == "brief.risks[0]" and risk["severity"] == "high"
    assert next(r for r in main if r["kind"] == "material_note")["source_key"] == "material_notes[0]"
    brief = notes.flatten(notes.Source("brief_deliverable", "", 3, fixture("brief_deliverable.json")), RUN)
    assert all(r["category"] == "" and r["source_scope"] == "brief_deliverable" for r in brief)
    skipped = notes.flatten(notes.Source("main", "", 1, fixture("main.json")), RUN, brief_deliverable_exists=True)
    assert {r["kind"] for r in skipped} == {"material_note", "confidence_note"}


def test_a_wrapped_brief_deliverable_is_unwrapped():
    src = notes.Source("brief_deliverable", "", 1, {"brief": fixture("brief_deliverable.json")})
    assert any(r["kind"] == "brief_summary" for r in notes.flatten(src, RUN))


def test_bodies_are_clipped_and_control_characters_stripped():
    out = fixture("category_v1.json")
    out["summary"] = "A\x00B\x1bC\u2014D " + ("x" * 9000)
    out["pages"][0]["sheet_number"] = "S" * 100
    rows = notes.flatten(notes.Source("category", "electrical_drawings", 1, out), RUN)
    summary = next(r for r in rows if r["kind"] == "category_summary")
    assert summary["body"].startswith("ABC-D ") and len(summary["body"]) <= 4000
    assert len(next(r for r in rows if r["kind"] == "page_note")["sheet_number"]) <= 60
    bell = next(r for r in rows if "Bell char" in r["body"])
    assert "\x07" not in bell["body"]
    for r in rows:
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", r["body"])
        assert "\u2014" not in r["body"]


def test_bad_document_ids_and_pages_become_null():
    out = fixture("category_v1.json")
    out["scope"][0]["document_id"] = "not-a-uuid"
    out["scope"][0]["page"] = 0
    row = next(r for r in notes.flatten(notes.Source("category", "e", 1, out), RUN) if r["kind"] == "scope")
    assert row["document_id"] is None and row["page"] is None


# -- The 0180 key ------------------------------------------------------------------


def test_on_conflict_list_equals_the_0180_constraint_columns():
    sql = MIGRATION.read_text()
    m = re.search(r"add constraint plans_reader_notes_key_uq\s+unique\s*\(([^)]*)\)", sql)
    assert m, "plans_reader_notes_key_uq not found in 0180"
    cols = [c.strip() for c in m.group(1).split(",")]
    assert ",".join(cols) == notes.ON_CONFLICT
    assert "coalesce" not in m.group(1)
    kinds = re.search(r"kind\s+text not null check \(kind in \((.*?)\)\)", sql, re.S)
    assert set(re.findall(r"'([a-z_]+)'", kinds.group(1))) == set(notes.KINDS)


# -- sync_run --------------------------------------------------------------------


def test_sync_run_writes_every_source_through_the_upsert(db):
    seed(db, category_v2=2)
    result = notes.sync_run(db, RUN)
    stored = db.rows(notes.TABLE, run_id=RUN)
    assert result["written"] == len(stored) > 0 and result["partial"] is False
    assert result["versions"] == {"category:electrical_drawings": 1, "category:low_voltage_drawings": 2, "main": 1}
    upserts = [c for c in db.calls if c[0] == notes.TABLE and c[1] == "upsert"]
    assert upserts and all(c[3]["on_conflict"] == notes.ON_CONFLICT and c[3]["ignore_duplicates"] for c in upserts)
    assert not [c for c in db.calls if c[0] == notes.TABLE and c[1] == "insert"]
    assert {r["category"] for r in stored if r["source_scope"] == "main"} == {""}


def test_sync_run_chunks_upserts_at_500(db):
    out = fixture("category_v1.json")
    out["pages"][0]["notes"] = [f"note {i}" for i in range(1200)]
    db.add("plans_reader_outputs", run_id=RUN, scope="category", category="electrical_drawings", version=1, output=out)
    result = notes.sync_run(db, RUN, max_rows=None)
    upserts = [c for c in db.calls if c[0] == notes.TABLE and c[1] == "upsert"]
    assert len(upserts) == 3
    assert result["written"] == len(db.rows(notes.TABLE)) > 1200


def test_a_rerun_writes_nothing(db):
    seed(db, brief=1)
    notes.sync_run(db, RUN)
    before = copy.deepcopy(db.rows(notes.TABLE))
    db.calls.clear()
    again = notes.sync_run(db, RUN)
    assert again["written"] == 0 and again["removed"] == 0
    assert not [c for c in db.calls if c[0] == notes.TABLE and c[1] in ("upsert", "insert")]
    assert db.rows(notes.TABLE) == before


def test_a_new_version_replaces_lower_versions_of_that_run_scope_and_category_only(db):
    seed(db)
    seed(db, RUN2)
    notes.sync_run(db, RUN)
    notes.sync_run(db, RUN2)
    other_run = len(db.rows(notes.TABLE, run_id=RUN2))
    main_rows = len(db.rows(notes.TABLE, run_id=RUN, source_scope="main"))
    db.add("plans_reader_outputs", run_id=RUN, scope="category", category="electrical_drawings", version=2,
           output=fixture("category_v1.json"))
    result = notes.sync_run(db, RUN)
    cat = db.rows(notes.TABLE, run_id=RUN, source_scope="category")
    assert {r["source_version"] for r in cat} == {2}
    assert result["removed"] == result["written"] == len(cat)
    assert len(db.rows(notes.TABLE, run_id=RUN, source_scope="main")) == main_rows
    assert len(db.rows(notes.TABLE, run_id=RUN2)) == other_run
    deletes = [c for c in db.calls if c[0] == notes.TABLE and c[1] == "delete"]
    for _t, _op, filters, _kw in deletes:
        assert ("eq", "run_id", RUN) in filters or ("eq", "run_id", RUN2) in filters
        assert any(f[0] == "eq" and f[1] == "category" for f in filters)


def test_a_brief_deliverable_suppresses_main_brief_kinds(db):
    seed(db)
    notes.sync_run(db, RUN)
    assert db.rows(notes.TABLE, run_id=RUN, source_scope="main", kind="risk")
    db.add("plans_reader_deliverables", run_id=RUN, kind="brief", version=2,
           spec_json=fixture("brief_deliverable.json"), filename="b", storage_path="x")
    result = notes.sync_run(db, RUN)
    assert result["versions"]["brief_deliverable"] == 2
    main_kinds = {r["kind"] for r in db.rows(notes.TABLE, run_id=RUN, source_scope="main")}
    assert main_kinds == {"material_note", "confidence_note"}
    risks = db.rows(notes.TABLE, run_id=RUN, kind="risk")
    assert [r["body"] for r in risks] == ["Deliverable risk."]
    assert all(r["category"] == "" for r in db.rows(notes.TABLE, run_id=RUN, source_scope="brief_deliverable"))


def test_the_highest_brief_deliverable_version_wins(db):
    seed(db, brief=1)
    db.add("plans_reader_deliverables", run_id=RUN, kind="brief", version=3,
           spec_json={**fixture("brief_deliverable.json"), "project_summary": "Version three."},
           filename="b", storage_path="x")
    notes.sync_run(db, RUN)
    bodies = [r["body"] for r in db.rows(notes.TABLE, run_id=RUN, source_scope="brief_deliverable",
                                          source_key="brief.project_summary")]
    assert bodies == ["Version three."]


def test_max_rows_leaves_whole_sources_for_the_sweep(db):
    seed(db, category_v2=1)
    first = notes.flatten(notes.Source("category", "electrical_drawings", 1, fixture("category_v1.json")), RUN)
    result = notes.sync_run(db, RUN, max_rows=len(first))
    assert result["partial"] is True and result["written"] == len(first)
    assert notes.notes_behind(db, [RUN]) == [RUN]
    rest = notes.sync_run(db, RUN, max_rows=None)
    assert rest["partial"] is False and rest["written"] > 0
    assert notes.notes_behind(db, [RUN]) == []


def test_chat_and_pricing_tables_are_never_read(db):
    seed(db, brief=1)
    notes.sync_run(db, RUN)
    notes.notes_behind(db, [RUN])
    tables = {c[0] for c in db.calls}
    assert tables <= {"plans_reader_outputs", "plans_reader_deliverables", notes.TABLE}


# -- notes_behind -----------------------------------------------------------------


def test_notes_behind_cases(db):
    seed(db)
    seed(db, RUN2)
    empty = str(uuid.uuid4())
    assert notes.notes_behind(db, [RUN, RUN2, empty]) == [RUN, RUN2]
    notes.sync_run(db, RUN)
    assert notes.notes_behind(db, [RUN, RUN2]) == [RUN2]
    db.add("plans_reader_outputs", run_id=RUN, scope="main", category=None, version=2, output=fixture("main.json"))
    assert notes.notes_behind(db, [RUN]) == [RUN]
    notes.sync_run(db, RUN)
    assert notes.notes_behind(db, [RUN]) == []
    db.add("plans_reader_deliverables", run_id=RUN, kind="brief", version=1,
           spec_json=fixture("brief_deliverable.json"), filename="b", storage_path="x")
    assert notes.notes_behind(db, [RUN]) == [RUN]
    notes.sync_run(db, RUN)
    assert notes.notes_behind(db, [RUN, RUN]) == []
    assert notes.notes_behind(db, []) == []


# -- stale_files -------------------------------------------------------------------


def test_stale_files_lists_addenda_after_the_run_only(db):
    project = str(uuid.uuid4())
    run = {"id": RUN, "created_at": "2026-10-20T00:00:00+00:00"}
    copied = db.add("project_files", project_id=project, category="electrical_drawing", filename="E.pdf",
                    created_at="2026-10-19T00:00:00+00:00")
    db.add("plans_reader_files", run_id=RUN, source_project_file_id=copied["id"],
           created_at="2026-10-21T00:00:00+00:00")
    db.add("project_files", project_id=project, category="specification", filename="Spec.pdf",
           created_at="2026-10-20T12:00:00+00:00")  # before the last copy: not stale
    addendum = db.add("project_files", project_id=project, category="addendum", filename="Add 1.PDF",
                      created_at="2026-10-22T00:00:00+00:00")
    db.add("project_files", project_id=project, category="boq", filename="boq.xlsx",
           created_at="2026-10-23T00:00:00+00:00")  # refused category
    db.add("project_files", project_id=project, category="addendum", filename="photo.jpg",
           created_at="2026-10-23T00:00:00+00:00")  # unreadable extension
    db.add("project_files", project_id=project, category="marked_plans", filename="m.pdf",
           created_at="2026-10-23T00:00:00+00:00")
    db.add("project_files", project_id=str(uuid.uuid4()), category="addendum", filename="other.pdf",
           created_at="2026-10-23T00:00:00+00:00")
    stale = notes.stale_files(db, project, run)
    assert [f["id"] for f in stale] == [addendum["id"]]
    assert set(stale[0]) == {"id", "filename", "category", "created_at"}


def test_stale_files_falls_back_to_the_run_creation_time(db):
    project = str(uuid.uuid4())
    run = {"id": RUN, "created_at": "2026-10-20T00:00:00+00:00"}
    db.add("project_files", project_id=project, category="drawing", filename="old.pdf",
           created_at="2026-10-19T00:00:00Z")
    new = db.add("project_files", project_id=project, category="drawing", filename="new.docx",
                 created_at="2026-10-20T00:00:01Z")
    assert [f["id"] for f in notes.stale_files(db, project, run)] == [new["id"]]


def test_stale_files_imports_the_ae_constants_by_name():
    from app.services import plans_reader_runs as runs

    assert isinstance(runs.PROJECT_CATEGORY_LABELS, dict) and "addendum" in runs.PROJECT_CATEGORY_LABELS
    assert "marked_plans" in runs.REFUSED_PROJECT_CATEGORIES
    from app.services import plans_reader_rows

    assert callable(plans_reader_rows.is_v2)


# -- Backfill script ------------------------------------------------------------------


def _script():
    spec = importlib.util.spec_from_file_location("backfill_plans_reader_notes", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("url", [
    "https://zxqkqotlcgbalwoqeeca.supabase.co",
    "https://qpfuugcwsdfpxvdgwvwx.supabase.co",
    "https://ZXQKQOTLCGBALWOQEECA.supabase.co",
    None,
    "",
])
def test_backfill_refuses_prod_staging_and_no_url(url):
    mod = _script()
    with pytest.raises(mod.RefusedError):
        mod.check_target(url)
    with pytest.raises(mod.RefusedError):
        mod.main(["--apply"], sb=FakeDB(), url=url or "")


def test_backfill_dry_run_writes_nothing_and_apply_writes(db, capsys):
    mod = _script()
    seed(db)
    seed(db, RUN2, main=0)
    dev = "https://bpidntbyvoooqvaispup.supabase.co"
    totals = mod.main(["--dry-run"], sb=db, url=dev)
    assert totals["runs"] == 2 and totals["rows"] > 0
    assert not db.writes()
    assert "dry run" in capsys.readouterr().out
    totals = mod.main(["--apply", "--run", RUN], sb=db, url=dev)
    assert totals["runs"] == 1 and totals["written"] == len(db.rows(notes.TABLE))
    assert not db.rows(notes.TABLE, run_id=RUN2)
    assert mod.main(["--apply", "--limit", "1"], sb=db, url=dev)["runs"] == 1


def test_backfill_requires_a_mode():
    mod = _script()
    with pytest.raises(SystemExit):
        mod.main([], sb=FakeDB(), url="https://bpidntbyvoooqvaispup.supabase.co")


def test_no_em_dash_in_the_wi2_files():
    for path in (
        ROOT / "app" / "services" / "plans_reader_notes.py",
        ROOT / "app" / "services" / "assistant_ai_estimate.py",
        SCRIPT,
        Path(__file__),
        Path(__file__).with_name("test_assistant_ai_estimate.py"),
        *FIXTURES.glob("*.json"),
    ):
        assert "\u2014" not in path.read_text(encoding="utf-8"), path
