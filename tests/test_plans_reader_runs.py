"""AI Estimating run lifecycle (services/plans_reader_runs.py, docs/PLANS_READER.md 7).

What these tests pin:
- create_run / add_upload / add_project_files: validation, labels, the
  `plans-reader/{run_id}/source/` objects, refusals with HTTP statuses;
- estimate and start: labeled files become documents at once, unlabeled
  ones are staged into ONE splitter job (source plans_reader, no project)
  and dispatched after staging; refusals (queue off, not configured, cap,
  per-run token limit);
- the split hook: segments become documents (prepended cover pages are not
  read twice), idempotent when it fires twice, and the run advances once
  every file settled;
- advance: splitting -> reading packs and enqueues one job per group;
  roll-ups and escalations are added once (deduplicated after insert, also
  under concurrent workers); reading -> consolidating writes the category
  outputs and enqueues the main group EXACTLY once (status compare-and-set),
  consolidating -> rendering -> done renders every deliverable;
- failure paths: a renderer error or a failed group ends done_with_errors,
  a failed main group fails the run;
- cancel, delete (storage sweeps, busy refusal), resume after over_cap,
  totals, list and the poll payload;
- the storage helpers for the `plans-reader/` prefix.

Supabase, Storage, the queue, the spend cap and the model are faked in
memory; PDFs are real (tiny pages) so planning and rendering run for real.
"""

from __future__ import annotations

import copy
import io
import itertools
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.roles import Role
from app.services import (
    ai_spend,
    bid_split,
    llm,
    llm_queue,
    storage,
)
from app.services import plans_reader_agents as agents
from app.services import plans_reader_render as render
from app.services import plans_reader_runs as runs

# The real background kick (install_env replaces it with an inline advance).
_REAL_KICK = runs._kick_advance

# ---------------------------------------------------------------------------
# Fake Supabase (statement-atomic, like a database)
# ---------------------------------------------------------------------------

_UNIQUE = {
    "plans_reader_outputs": ("run_id", "scope", "category", "version"),
    "plans_reader_deliverables": ("run_id", "kind", "version"),
}
# Migration 0153's partial unique indexes: (columns, which rows the index covers).
_PARTIAL_UNIQUE = {
    "plans_reader_groups": (
        ("run_id",), lambda r: r.get("kind") == "modify" and r.get("status") in ("queued", "running"),
    ),
}
_DEFAULTS = {
    "plans_reader_runs": {
        "status": "created", "source": "upload", "project_id": None, "render_mode": "adaptive",
        "reasoning_effort": "medium", "model": None, "settings_snapshot": {}, "split_job_id": None,
        "ledger": None, "documents_total": 0, "groups_total": 0, "groups_done": 0, "groups_failed": 0,
        "estimated_input_tokens": None, "estimated_cost_usd": None, "input_tokens": 0,
        "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0, "error": None,
        "created_by": None, "started_at": None, "finished_at": None,
    },
    "plans_reader_files": {
        "status": "pending", "label": None, "page_count": None, "source_project_file_id": None,
        "split_file_id": None, "error": None, "mime_type": None, "size_bytes": 0,
    },
    "plans_reader_documents": {
        "status": "pending", "segment_id": None, "other_type": None, "page_meta": None,
        "sheet_width_in": None, "sheet_height_in": None, "page_start": 1, "page_end": 1, "page_count": 1,
    },
    "plans_reader_groups": {
        "status": "queued", "category": None, "group_index": 0, "pass": 1, "pages": [],
        "est_input_tokens": None, "output": None, "error": None, "attempts": 0, "llm_job_id": None,
        "started_at": None, "finished_at": None,
    },
    "plans_reader_calls": {"ok": True},
    "plans_reader_deliverables": {"instruction": None, "created_by": None, "size_bytes": 0},
    "bid_split_files": {"status": "pending", "error": None},
}


class _Resp:
    def __init__(self, data):
        self.data = data


def _same(a, b) -> bool:
    if a == b:
        return True
    return a is not None and b is not None and str(a) == str(b)


def _when(value):
    """An ISO timestamp as an aware datetime (gte filters compare instants)."""
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _like_regex(pattern: str) -> str:
    """SQL LIKE (with backslash escapes) as a regex."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if c == "%" else "." if c == "_" else re.escape(c))
        i += 1
    return "^" + "".join(out) + "$"


class FakeQuery:
    def __init__(self, db: "FakeDB", table: str):
        self.db = db
        self.table = table
        self.op = None
        self.payload = None
        self.filters: list[tuple] = []
        self.orders: list[tuple[str, bool]] = []
        self._limit = None
        self._range = None

    # operations
    def select(self, *_a, **_k):
        self.op = self.op or "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    # filters
    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self.filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def ilike(self, col, pattern):
        self.filters.append(("ilike", col, pattern))
        return self

    def gte(self, col, val):
        self.filters.append(("gte", col, val))
        return self

    def order(self, col, desc=False, **_k):
        self.orders.append((col, desc))
        return self

    def limit(self, n, **_k):
        self._limit = n
        return self

    def range(self, start, end, **_k):
        self._range = (start, end)
        return self

    def _match(self, row) -> bool:
        for op, col, val in self.filters:
            have = row.get(col)
            if op == "eq" and not _same(have, val):
                return False
            if op == "neq" and _same(have, val):
                return False
            if op == "in" and not any(_same(have, v) for v in val):
                return False
            if op == "is" and not ((val in ("null", None) and have is None) or _same(have, val)):
                return False
            if op == "ilike" and (have is None or not re.match(_like_regex(val), str(have), re.I | re.S)):
                return False
            if op == "gte" and (have is None or _when(have) < _when(val)):
                return False
        return True

    def execute(self):
        with self.db.lock:
            self.db.statements.append((self.table, self.op, list(self.filters)))
            rows = self.db.tables.setdefault(self.table, [])
            if self.op == "insert":
                items = self.payload if isinstance(self.payload, list) else [self.payload]
                out = []
                for item in items:
                    row = {**copy.deepcopy(_DEFAULTS.get(self.table, {})), **copy.deepcopy(item)}
                    row.setdefault("id", uuid.uuid4().hex)
                    row.setdefault("created_at", self.db.tick())
                    row.setdefault("updated_at", row["created_at"])
                    keys = _UNIQUE.get(self.table)
                    if keys and any(all(_same(r.get(k), row.get(k)) for k in keys) for r in rows):
                        raise Exception('duplicate key value violates unique constraint (code 23505)')
                    partial = _PARTIAL_UNIQUE.get(self.table)
                    if partial and self.db.partial_unique and partial[1](row) and any(
                        partial[1](r) and all(_same(r.get(k), row.get(k)) for k in partial[0]) for r in rows
                    ):
                        raise Exception('duplicate key value violates unique constraint (code 23505)')
                    if self.db.fail_insert.get(self.table):
                        raise RuntimeError(f"insert into {self.table} failed")
                    rows.append(row)
                    out.append(copy.deepcopy(row))
                return _Resp(out)
            matched = [r for r in rows if self._match(r)]
            if self.op == "update":
                for r in matched:
                    r.update(copy.deepcopy(self.payload))
                    r["updated_at"] = self.db.tick()
                return _Resp([copy.deepcopy(r) for r in matched])
            if self.op == "delete":
                self.db.tables[self.table] = [r for r in rows if r not in matched]
                return _Resp([copy.deepcopy(r) for r in matched])
            out = [copy.deepcopy(r) for r in matched]
            for col, desc in reversed(self.orders):
                out.sort(key=lambda r: (r.get(col) is None, str(r.get(col) or "")), reverse=desc)
            if self._range is not None:
                out = out[self._range[0] : self._range[1] + 1]
            if self._limit is not None:
                out = out[: self._limit]
            return _Resp(out)


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.statements: list[tuple] = []
        self.lock = threading.RLock()
        self._clock = itertools.count(1)
        self._base = datetime.now(timezone.utc)
        self.fail_insert: dict[str, bool] = {}
        self.partial_unique = True  # False = a database without migration 0153

    def tick(self) -> str:
        """Now, one microsecond later per call: fresh (never stale) and
        strictly increasing, like created_at on consecutive statements."""
        return (self._base + timedelta(microseconds=next(self._clock))).isoformat()

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rows(self, _table: str, **where) -> list[dict]:
        return [r for r in self.tables.get(_table, []) if all(_same(r.get(k), v) for k, v in where.items())]

    def add(self, _table: str, **row) -> dict:
        return self.table(_table).insert(row).execute().data[0]


# ---------------------------------------------------------------------------
# PDFs and the environment fixture
# ---------------------------------------------------------------------------


def make_pdf(pages: int = 1, w_pt: float = 144, h_pt: float = 144) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=w_pt, height=h_pt)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        openai_api_key="test-key",
        anthropic_api_key="test-key",
        plans_reader_enabled=True,
        llm_queue_enabled=True,
        preview_engine="off",
    )
    base.update(overrides)
    return Settings(**base)



def user(role: Role = Role.ESTIMATING_ADMIN, uid: str = "u-1"):
    return SimpleNamespace(id=uid, role=role, email="x@example.com")


def sub_output(category: str, refs: list[tuple[int, int]], **extra) -> dict:
    """A schema-shaped sub agent answer for (doc_index, page) refs."""
    out = {
        "category": category,
        "summary": f"{category} summary",
        "pages": [
            {
                "doc_index": d, "page": p, "sheet_number": f"E-{100 + p}", "title": f"SHEET {p}",
                "page_role": "plan", "scale": None, "tags": ["t"], "notes": [], "resolution_flags": [],
            }
            for d, p in refs
        ],
        "counts": [
            {
                "description": "Duplex receptacle", "symbol_or_tag": None, "quantity": 4, "unit": "EA",
                "doc_index": d, "page": p, "sheet_number": None, "location": "at (0.10,0.20)",
                "basis": "counted_symbols", "confidence": 0.9, "material_category_hint": None, "notes": None,
            }
            for d, p in refs
        ],
        "runs": [], "scope": [], "specs": [], "schedules": [], "details": [], "questions": [],
    }
    out.update(extra)
    return out


def main_output_spec(doc_id: str) -> dict:
    item = {
        "description": '3/4"C EMT', "quantity": 100, "unit": "FT", "sheet_refs": ["E-101"],
        "caveat": None, "material_note": None, "material_category": "General Material",
        "wastage_applies": True, "is_accessory": False,
    }
    return {
        "brief": {
            "project_summary": "p", "electrical_scope_summary": "e", "systems": [], "assumptions": [],
            "exclusions": [], "risks": [], "rfis": [], "missing_documents": [],
            "other_trades_affecting_electrical": [], "sheet_summaries": [], "spec_highlights": [],
        },
        "boq": {
            "title": "Test", "subtitle": None, "closing_notes": [],
            "areas": [{"area": "Test", "notes": [], "sections": [{"heading": "NEW WORK", "note": None, "groups": [
                {"name": "WIRING", "note": None, "subgroups": [{"name": "CONDUITS", "note": None, "items": [item]}]}
            ]}]}],
        },
        "estimate": {
            "project_name": "Test", "address": None, "include_standard_allowances": True,
            "extra_allowances": [], "general_notes": [], "area_notes": [],
        },
        "markups": {"sheets": [
            {"kind": "trench", "doc_index": 1, "document_id": doc_id, "page": 1, "sheet_number": "E-101",
             "label": "TRENCH", "lines": [{"points": [[0.1, 0.1], [0.5, 0.5]], "length_ft": 40, "label": None}],
             "notes": None},
        ]},
        "marked_plans": {"sheets": [
            {"area": "Test", "doc_index": 1, "document_id": doc_id, "page": 1, "sheet_number": "E-101",
             "legend": [{"prefix": "R", "description": "Receptacle", "quantity": 1, "unit": "EA"}],
             "marks": [{"legend_index": 0, "kind": "dot", "points": [[0.2, 0.2]]}]},
        ]},
        "material_notes": [],
        "confidence_notes": [],
    }


def install_env(monkeypatch) -> SimpleNamespace:
    """Fake Supabase, Storage, the queue, the spend cap, the splitter's
    dispatch and the LLM gates; returns the namespace tests inspect.
    Shared with test_plans_reader_agents."""
    db = FakeDB()
    store: dict[str, bytes] = {}
    s = make_settings()
    ns = SimpleNamespace(
        db=db, store=store, settings=s, enqueued=[], dispatched=[], canceled=[], after_calls=[],
        cap_error=None, cap_status={"remaining_usd": 75.0, "blocked": False}, copies=[],
    )

    for module in (runs, agents, bid_split):
        monkeypatch.setattr(module, "get_settings", lambda: ns.settings)
    monkeypatch.setattr(runs, "get_supabase", lambda: db)

    def upload_file(path, content, content_type, *, upsert=False):
        store[path] = bytes(content)

    def download_file(path):
        if path not in store:
            raise RuntimeError(f"missing object {path}")
        return store[path]

    def copy_file(src, dst):
        ns.copies.append((src, dst))
        store[dst] = store[src]

    def delete_prefix(prefix_fmt):
        def _go(key):
            prefix = prefix_fmt.format(key)
            for k in [k for k in store if k.startswith(prefix)]:
                del store[k]
        return _go

    monkeypatch.setattr(storage, "upload_file", upload_file)
    monkeypatch.setattr(storage, "download_file", download_file)
    monkeypatch.setattr(storage, "copy_file", copy_file)
    monkeypatch.setattr(storage, "delete_file", lambda p: store.pop(p, None))
    monkeypatch.setattr(storage, "object_exists", lambda p: p in store)
    monkeypatch.setattr(storage, "delete_plans_reader_prefix", delete_prefix("plans-reader/{}/"))
    monkeypatch.setattr(storage, "delete_bid_split_prefix", delete_prefix("bid-splits/{}/"))

    def enqueue(job_type, *, target_id, project_id=None, payload=None, created_by=None, next_attempt_at=None, **_k):
        job = {"id": f"job-{len(ns.enqueued) + 1}", "job_type": job_type, "target_id": target_id,
               "payload": payload, "project_id": project_id, "next_attempt_at": next_attempt_at}
        ns.enqueued.append(job)
        return job

    monkeypatch.setattr(llm_queue, "enqueue", enqueue)
    monkeypatch.setattr(llm_queue, "cancel", lambda job_id: ns.canceled.append(job_id))
    monkeypatch.setattr(llm_queue, "poll_info", lambda jt, tid: {"state": "queued", "position": 1})
    monkeypatch.setattr(bid_split, "dispatch", lambda bg, fid, uid, forced_kind=None: ns.dispatched.append((bg, fid, uid)))

    def check_cap(sb, *, estimated_usd=0, now=None):
        if ns.cap_error:
            raise ai_spend.SpendCapReached(ns.cap_error, cap_usd=Decimal("75"), spent_usd=Decimal("75"))
        return Decimal("75")

    monkeypatch.setattr(ai_spend, "check_cap", check_cap)
    monkeypatch.setattr(ai_spend, "after_call", lambda sb, now=None: ns.after_calls.append(1))
    monkeypatch.setattr(ai_spend, "status", lambda sb, now=None: dict(ns.cap_status))
    monkeypatch.setattr(llm, "is_configured", lambda feature, settings=None: True)
    monkeypatch.setattr(llm, "unavailable_reason", lambda feature, settings=None: None)
    monkeypatch.setattr(llm, "active_model", lambda feature, settings=None: "test-model")
    # The request path kicks the next stage on a thread; run it inline here.
    monkeypatch.setattr(runs, "_kick_advance", lambda run_id, force=False: runs.advance(db, run_id))
    runs._INFO_CACHE.clear()
    return ns


@pytest.fixture
def env(monkeypatch):
    return install_env(monkeypatch)


def new_run(env, **kw) -> dict:
    return runs.create_run(env.db, name=kw.pop("name", "Edikted TI"), source=kw.pop("source", "upload"),
                           project_id=kw.pop("project_id", None), render_mode=kw.pop("render_mode", "adaptive"),
                           user=user())


def seed_document(env, run, *, category="electrical_drawings", pages=2, page_meta=None, name=None, w_pt=144, h_pt=144):
    path = f"plans-reader/{run['id']}/source/{uuid.uuid4().hex}-doc.pdf"
    env.store[path] = make_pdf(pages, w_pt, h_pt)
    f = env.db.add("plans_reader_files", run_id=run["id"], filename=f"{category}.pdf", storage_path=path,
                   label=category, status="labeled")
    return env.db.add(
        "plans_reader_documents", run_id=run["id"], file_id=f["id"], category=category,
        name=name or f"{category} set", storage_path=path, page_start=1, page_end=pages,
        page_count=pages, page_meta=page_meta, status="reading",
    )


def set_status(env, run, status, **fields):
    env.db.table("plans_reader_runs").update({"status": status, **fields}).eq("id", run["id"]).execute()
    return env.db.rows("plans_reader_runs", id=run["id"])[0]


# ---------------------------------------------------------------------------
# create / upload / project files
# ---------------------------------------------------------------------------


def test_create_run_records_model_effort_and_settings_snapshot(env):
    run = new_run(env)
    assert run["status"] == "created"
    assert run["model"] == env.settings.openai_plans_model
    assert run["reasoning_effort"] == env.settings.plans_reader_reasoning_effort
    snap = run["settings_snapshot"]
    assert snap["plans_reader_group_input_tokens"] == env.settings.plans_reader_group_input_tokens
    assert snap["plans_reader_tile_ppi"] == env.settings.plans_reader_tile_ppi
    assert run["created_by"] == "u-1"


@pytest.mark.parametrize(
    "kw, status",
    [
        ({"name": "   "}, 400),
        ({"render_mode": "tiles_9000"}, 400),
        ({"source": "email"}, 400),
        ({"source": "project"}, 400),
        ({"project_id": "missing"}, 404),
    ],
)
def test_create_run_refusals(env, kw, status):
    with pytest.raises(runs.RunRefused) as exc:
        new_run(env, **kw)
    assert exc.value.status_code == status


def test_external_estimator_is_refused(env):
    with pytest.raises(runs.RunRefused) as exc:
        runs.create_run(env.db, name="x", source="upload", project_id=None, render_mode=None,
                        user=user(Role.ESTIMATOR))
    assert exc.value.status_code == 403


def test_add_upload_stores_under_the_run_prefix(env):
    run = new_run(env)
    row = runs.add_upload(env.db, run, filename="../E-Set.pdf", content=make_pdf(3), label="electrical_drawings",
                          user=user())
    assert row["status"] == "pending" and row["label"] == "electrical_drawings"
    assert row["filename"] == "E-Set.pdf" and row["page_count"] == 3
    assert re.fullmatch(rf"plans-reader/{run['id']}/source/[0-9a-f]{{32}}-E-Set\.pdf", row["storage_path"])
    assert env.store[row["storage_path"]][:5] == b"%PDF-"


def test_add_upload_refusals(env):
    run = new_run(env)
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="notes.txt", content=b"hello", label=None, user=user())
    assert exc.value.status_code == 415
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="fake.pdf", content=b"PK\x03\x04zip", label=None, user=user())
    assert exc.value.status_code == 415
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="a.pdf", content=make_pdf(1), label="banana", user=user())
    assert exc.value.status_code == 400
    # Office files need the converter; preview_engine is off in these tests.
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="spec.docx", content=b"PK\x03\x04rest", label=None, user=user())
    assert exc.value.status_code == 415 and "PDF converter" in str(exc.value)
    env.settings = make_settings(upload_max_bytes=10, rfp_ingest_max_file_bytes=10)
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="a.pdf", content=make_pdf(1), label=None, user=user())
    assert exc.value.status_code == 413


def test_unlabeled_upload_over_the_splitter_page_cap_is_refused(env):
    env.settings = make_settings(bid_split_max_pages_per_file=2)
    run = new_run(env)
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="big.pdf", content=make_pdf(3), label=None, user=user())
    assert exc.value.status_code == 413 and "Label the file" in str(exc.value)
    # Labeled, the same file is read without splitting.
    assert runs.add_upload(env.db, run, filename="big.pdf", content=make_pdf(3), label="specifications",
                           user=user())["label"] == "specifications"


def test_add_upload_refused_after_start(env):
    run = set_status(env, new_run(env), "reading")
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="a.pdf", content=make_pdf(1), label=None, user=user())
    assert exc.value.status_code == 409


def test_add_project_files_copies_server_side_with_derived_labels(env):
    project = env.db.add("projects", name="YMCA", number="26.10.1")
    run = new_run(env, source="project", project_id=project["id"])
    files = {}
    for cat in ("electrical_drawing", "drawing", "specification", "addendum", "rfp", "other"):
        path = f"{project['id']}/{cat}/{uuid.uuid4().hex}-{cat}.pdf"
        env.store[path] = make_pdf(1)
        files[cat] = env.db.add("project_files", project_id=project["id"], category=cat, filename=f"{cat}.pdf",
                                storage_path=path, size_bytes=123)
    rows = runs.add_project_files(env.db, run, project_file_ids=[f["id"] for f in files.values()], user=user())
    labels = {r["filename"]: r["label"] for r in rows}
    assert labels == {
        "electrical_drawing.pdf": "electrical_drawings", "drawing.pdf": None,
        "specification.pdf": "specifications", "addendum.pdf": "addenda", "rfp.pdf": "rfp", "other.pdf": "other",
    }
    assert all(dst.startswith(f"plans-reader/{run['id']}/source/") for _src, dst in env.copies)
    assert {r["source_project_file_id"] for r in rows} == {f["id"] for f in files.values()}
    # A second add of the same files adds nothing.
    assert runs.add_project_files(env.db, run, project_file_ids=[files["rfp"]["id"]], user=user()) == []


@pytest.mark.parametrize("category", ["boq", "estimate", "proposal", "quote", "marked_plans", "rfq_split", "mystery"])
def test_add_project_files_refuses_our_own_and_commercial_files(env, category):
    project = env.db.add("projects", name="P", number="1")
    run = new_run(env, source="project", project_id=project["id"])
    pf = env.db.add("project_files", project_id=project["id"], category=category, filename="x.pdf",
                    storage_path="p/x.pdf")
    with pytest.raises(runs.RunRefused):
        runs.add_project_files(env.db, run, project_file_ids=[pf["id"]], user=user())


def test_add_project_files_must_come_from_the_runs_project(env):
    p1 = env.db.add("projects", name="A", number="1")
    p2 = env.db.add("projects", name="B", number="2")
    run = new_run(env, source="project", project_id=p1["id"])
    pf = env.db.add("project_files", project_id=p2["id"], category="rfp", filename="x.pdf", storage_path="p/x.pdf")
    with pytest.raises(runs.RunRefused):
        runs.add_project_files(env.db, run, project_file_ids=[pf["id"]], user=user())


# ---------------------------------------------------------------------------
# estimate / start
# ---------------------------------------------------------------------------


def test_estimate_prices_every_file_and_stores_it(env):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(3), label="electrical_drawings", user=user())
    runs.add_upload(env.db, run, filename="S.pdf", content=make_pdf(4, 612, 792), label="specifications",
                    user=user())
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(2), label=None, user=user())
    est = runs.estimate(env.db, run)
    assert est["documents"] == 3 and est["groups"] >= 2
    assert est["input_tokens"] > 0 and est["cost_usd"] > 0 and est["priced"] is True
    assert est["cap_remaining_usd"] == 75.0 and est["over_cap"] is False and est["over_run_limit"] is False
    stored = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert stored["estimated_input_tokens"] == est["input_tokens"]
    assert stored["estimated_cost_usd"] == est["cost_usd"]


def test_estimate_is_not_over_cap_just_because_an_earlier_step_was_refused(env):
    """A refusal earlier in the window sets status()["blocked"] (the Settings
    banner); a smaller run whose worst case fits in what is left can start."""
    env.cap_status = {"remaining_usd": 52.66, "blocked": True, "cap_hit": False}
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(2), label="electrical_drawings", user=user())
    est = runs.estimate(env.db, run)
    assert est["cost_usd_max"] < 52.66 and est["over_cap"] is False


def test_estimate_reports_over_cap(env):
    env.cap_status = {"remaining_usd": 0.01, "blocked": False}
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(2), label="electrical_drawings", user=user())
    assert runs.estimate(env.db, run)["over_cap"] is True


def test_start_with_labeled_files_creates_documents_groups_and_jobs(env):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(3), label="electrical_drawings", user=user())
    runs.add_upload(env.db, run, filename="S.pdf", content=make_pdf(4, 612, 792), label="specifications",
                    user=user())
    out = runs.start(env.db, run, user=user())
    assert out["status"] == "reading" and out["started_at"]
    docs = env.db.rows("plans_reader_documents", run_id=run["id"])
    assert sorted(d["category"] for d in docs) == ["electrical_drawings", "specifications"]
    assert all(f["status"] == "labeled" for f in env.db.rows("plans_reader_files", run_id=run["id"]))
    spec_doc = next(d for d in docs if d["category"] == "specifications")
    assert spec_doc["page_meta"][0]["page_role"] == "spec_text"
    groups = env.db.rows("plans_reader_groups", run_id=run["id"])
    assert len(groups) == 2 and {g["kind"] for g in groups} == {"sub"} and {g["pass"] for g in groups} == {1}
    drawing = next(g for g in groups if g["category"] == "electrical_drawings")
    assert [i["page"] for i in drawing["pages"]] == [1, 2, 3]
    assert {i["kind"] for i in drawing["pages"]} == {"image"}
    assert {i["detail"] for i in drawing["pages"]} == {"original"}
    assert drawing["pages"][0]["ppi"] == 260 and drawing["pages"][0]["w_in"] == 2.0
    spec = next(g for g in groups if g["category"] == "specifications")
    assert {i["kind"] for i in spec["pages"]} == {"file"} and len(spec["pages"]) == 4
    assert len(env.enqueued) == 2
    for job in env.enqueued:
        assert job["job_type"] == "plans_reader_agent" and job["project_id"] is None
        group_row = env.db.rows("plans_reader_groups", id=job["target_id"])[0]
        assert job["payload"] == {"group_id": job["target_id"], "run_id": run["id"],
                                  "est_input_tokens": group_row["est_input_tokens"]}
        assert job["next_attempt_at"] is None  # far under the per-minute budget: due at once
    assert {g["llm_job_id"] for g in env.db.rows("plans_reader_groups", run_id=run["id"])} == {"job-1", "job-2"}
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["groups_total"] == 2 and fresh["documents_total"] == 2
    assert {d["category"] for d in fresh["ledger"]["documents"]} == {"electrical_drawings", "specifications"}


@pytest.mark.parametrize(
    "setup, error, status, needle",
    [
        ("queue_off", runs.RunRefused, 409, "queue"),
        ("not_configured", llm.LlmNotConfigured, None, "not configured"),
        ("flag_off", llm.LlmNotConfigured, None, "PLANS_READER_ENABLED"),
        ("cap", ai_spend.SpendCapReached, None, "spend cap"),
        ("run_limit", runs.RunRefused, 413, "over the limit"),
        ("no_files", runs.RunRefused, 400, "at least one file"),
    ],
)
def test_start_refusals(env, monkeypatch, setup, error, status, needle):
    run = new_run(env)
    if setup != "no_files":
        runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(2), label="electrical_drawings", user=user())
    if setup == "queue_off":
        env.settings = make_settings(llm_queue_enabled=False)
    elif setup == "not_configured":
        monkeypatch.setattr(llm, "is_configured", lambda feature, settings=None: False)
    elif setup == "flag_off":
        monkeypatch.setattr(llm, "unavailable_reason", lambda feature, settings=None: llm.PLANS_READER_OFF_MESSAGE)
    elif setup == "cap":
        env.cap_error = "AI Estimating reached its daily spend cap of $75.00."
    elif setup == "run_limit":
        env.settings = make_settings(plans_reader_max_input_tokens_per_run=240_000)
        monkeypatch.setattr(
            runs, "estimate",
            lambda sb, r: {"input_tokens": 300_000, "escalation_max_input_tokens": 0, "cost_usd": 1.0},
        )
    with pytest.raises(error) as exc:
        runs.start(env.db, run, user=user())
    assert needle in str(exc.value)
    if status is not None:
        assert exc.value.status_code == status
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "created"


def test_start_refuses_a_second_start(env):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(1), label="electrical_drawings", user=user())
    runs.start(env.db, run, user=user())
    with pytest.raises(runs.RunRefused) as exc:
        runs.start(env.db, run, user=user())
    assert exc.value.status_code == 409


def _split_result(env, run, split_file_id):
    """What the splitter writes when a 3-page set finishes: a General segment
    (page 1) and an Electrical segment (pages 2-3) whose object starts with
    the prepended cover page."""
    job_id = env.db.rows("bid_split_files", id=split_file_id)[0]["job_id"]
    gen_path = f"bid-splits/{job_id}/output/{uuid.uuid4().hex}-gen.pdf"
    ele_path = f"bid-splits/{job_id}/output/{uuid.uuid4().hex}-ele.pdf"
    env.store[gen_path] = make_pdf(1)
    env.store[ele_path] = make_pdf(3)
    env.db.add("bid_split_segments", file_id=split_file_id, sort_order=0, category="general_drawings",
               name="General", page_start=1, page_end=1, storage_path=gen_path, filename="gen.pdf",
               page_meta=[{"page": 1, "pdf_page": 1, "prepended": False, "title": "G-001 COVER SHEET",
                           "page_role": "cover_index", "confidence": 0.9}])
    env.db.add("bid_split_segments", file_id=split_file_id, sort_order=1, category="electrical_drawings",
               name="Electrical", page_start=2, page_end=3, storage_path=ele_path, filename="ele.pdf",
               page_meta=[
                   {"page": 1, "pdf_page": 1, "prepended": True, "title": "G-001 COVER SHEET",
                    "page_role": "cover_index", "confidence": 0.9},
                   {"page": 2, "pdf_page": 2, "prepended": False, "title": "E-001 LEGEND",
                    "page_role": "legend_symbols", "confidence": 0.9},
                   {"page": 3, "pdf_page": 3, "prepended": False, "title": "E-101 POWER PLAN",
                    "page_role": "plan", "confidence": 0.9},
               ])
    env.db.table("bid_split_files").update({"status": "done"}).eq("id", split_file_id).execute()


def test_unlabeled_files_go_to_one_plans_reader_split_job_then_become_documents(env):
    run = new_run(env)
    f = runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(3), label=None, user=user())
    g = runs.add_upload(env.db, run, filename="gen.pdf", content=make_pdf(2), label="general_drawings",
                        user=user())
    out = runs.start(env.db, run, user=user())
    assert out["status"] == "splitting"
    jobs = env.db.rows("bid_split_jobs")
    assert len(jobs) == 1
    assert jobs[0]["source"] == "plans_reader" and jobs[0]["project_id"] is None
    assert jobs[0]["created_by"] == "u-1" and out["split_job_id"] == jobs[0]["id"]
    split_files = env.db.rows("bid_split_files", job_id=jobs[0]["id"])
    assert len(split_files) == 2
    assert sorted(fid for _bg, fid, _u in env.dispatched) == sorted(sf["id"] for sf in split_files)
    assert all(bg is None and uid == "u-1" for bg, _fid, uid in env.dispatched)
    files = {r["id"]: r for r in env.db.rows("plans_reader_files", run_id=run["id"])}
    assert files[f["id"]]["status"] == "splitting" and files[g["id"]]["status"] == "splitting"

    first, second = files[f["id"]]["split_file_id"], files[g["id"]]["split_file_id"]
    _split_result(env, run, first)
    runs.on_split_file_done(first)  # bid_split calls the hook with the file id only
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "splitting"  # g still splitting
    assert len(env.db.rows("plans_reader_documents", run_id=run["id"])) == 2
    runs.on_split_file_done(first)  # a second fire adds nothing
    assert len(env.db.rows("plans_reader_documents", run_id=run["id"])) == 2

    env.db.table("bid_split_files").update({"status": "failed", "error": "Unreadable."}).eq("id", second).execute()
    runs.on_split_file_done(env.db, second)
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "reading"
    files = {r["id"]: r for r in env.db.rows("plans_reader_files", run_id=run["id"])}
    assert files[f["id"]]["status"] == "split"
    assert files[g["id"]]["status"] == "failed" and files[g["id"]]["error"] == "Unreadable."
    groups = env.db.rows("plans_reader_groups", run_id=run["id"])
    electrical = next(gr for gr in groups if gr["category"] == "electrical_drawings")
    # The prepended cover page (pdf page 1) is read in the General document only.
    assert [i["page"] for i in electrical["pages"]] == [2, 3]
    ledger = fresh["ledger"]
    assert [p["title"] for p in ledger["legend_pages"]] == ["E-001 LEGEND"]
    assert {(r["sheet_number"], r["page"]) for r in ledger["sheet_index"]} == {
        ("G-001", 1), ("E-001", 2), ("E-101", 3),
    }
    ele_doc = next(d for d in env.db.rows("plans_reader_documents") if d["category"] == "electrical_drawings")
    assert ele_doc["page_count"] == 3 and ele_doc["sheet_width_in"] == 2.0


def test_split_file_refused_by_the_splitter_fails_that_file_only(env, monkeypatch):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(2), label=None, user=user())
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(1), label="electrical_drawings", user=user())

    def refuse(name, content, settings=None):
        raise bid_split.UploadRefused(f"{name}: the splitter accepts PDF files only", 415)

    monkeypatch.setattr(bid_split, "check_upload", refuse)
    out = runs.start(env.db, run, user=user())
    assert out["status"] == "reading"  # nothing left to split: advanced at once
    assert "splitter accepts PDF files only" in out["error"]
    statuses = sorted(f["status"] for f in env.db.rows("plans_reader_files", run_id=run["id"]))
    assert statuses == ["failed", "labeled"]


def test_run_with_no_readable_documents_fails(env, monkeypatch):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(2), label=None, user=user())
    monkeypatch.setattr(
        bid_split, "check_upload",
        lambda name, content, settings=None: (_ for _ in ()).throw(bid_split.UploadRefused("nope", 415)),
    )
    out = runs.start(env.db, run, user=user())
    fresh = env.db.rows("plans_reader_runs", id=out["id"])[0]
    assert fresh["status"] == "failed" and runs.MSG_NO_DOCUMENTS in fresh["error"]


# ---------------------------------------------------------------------------
# advance: roll-ups, escalations, the main group, CAS
# ---------------------------------------------------------------------------


def _reading_run(env, *, ledger=None):
    run = new_run(env)
    return set_status(env, run, "reading", ledger=ledger or {"text_tokens": 100, "images": []})


def _sub(env, run, doc, category, index, status="done", output=None, pages=None, est=1000):
    pages = pages or [{"document_id": doc["id"], "page": 1, "ppi": 155, "detail": "original", "tiles": None,
                       "kind": "image", "w_in": 42.0, "h_in": 30.0}]
    return env.db.add("plans_reader_groups", run_id=run["id"], kind="sub", category=category, group_index=index,
                      **{"pass": 1}, pages=pages, status=status, est_input_tokens=est,
                      output=output if output is not None else (sub_output(category, [(1, 1)]) if status == "done" else None))


def test_rollup_is_added_once_when_a_categorys_groups_are_done(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    _sub(env, run, doc, "electrical_drawings", 0)
    second = _sub(env, run, doc, "electrical_drawings", 1, status="running")
    runs.advance(env.db, run["id"])
    assert not env.db.rows("plans_reader_groups", kind="rollup")  # one still running
    env.db.table("plans_reader_groups").update({"status": "done", "output": sub_output("electrical_drawings", [(1, 1)])}).eq(
        "id", second["id"]).execute()
    runs.advance(env.db, run["id"])
    runs.advance(env.db, run["id"])
    rollups = env.db.rows("plans_reader_groups", kind="rollup")
    assert len(rollups) == 1 and rollups[0]["category"] == "electrical_drawings"
    assert rollups[0]["est_input_tokens"] > 0
    assert [j["target_id"] for j in env.enqueued] == [rollups[0]["id"]]
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"


def test_reading_to_consolidating_writes_outputs_and_enqueues_main_once(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    spec_doc = seed_document(env, run, category="specifications")
    _sub(env, run, doc, "electrical_drawings", 0)
    _sub(env, run, spec_doc, "specifications", 0, pages=[
        {"document_id": spec_doc["id"], "page": 1, "ppi": None, "detail": None, "tiles": None, "kind": "file"}])
    runs.advance(env.db, run["id"])
    runs.advance(env.db, run["id"])  # a second worker arriving late changes nothing
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "consolidating"
    outputs = env.db.rows("plans_reader_outputs", run_id=run["id"], scope="category")
    assert sorted(o["category"] for o in outputs) == ["electrical_drawings", "specifications"]
    assert all(o["version"] == 1 and o["source_group_ids"] for o in outputs)
    ele = next(o for o in outputs if o["category"] == "electrical_drawings")["output"]
    assert ele["escalation_patches"] == [] and ele["source"] == {"groups": 1, "failed_groups": 0, "rolled_up": False}
    mains = env.db.rows("plans_reader_groups", kind="main")
    assert len(mains) == 1 and mains[0]["est_input_tokens"] > 0
    assert [(j["job_type"], j["target_id"]) for j in env.enqueued] == [("plans_reader_main", mains[0]["id"])]


def test_stale_advance_loses_the_stage_cas(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    _sub(env, run, doc, "electrical_drawings", 0)
    stale = env.db.rows("plans_reader_runs", id=run["id"])[0]
    runs.advance(env.db, run["id"])  # the winner
    runs._advance_reading(env.db, stale, env.settings)  # a worker holding the old snapshot
    assert len(env.db.rows("plans_reader_groups", kind="main")) == 1
    assert len(env.db.rows("plans_reader_outputs", scope="category")) == 1
    assert len(env.enqueued) == 1


def test_concurrent_workers_enqueue_the_rollup_and_the_main_group_exactly_once(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    _sub(env, run, doc, "electrical_drawings", 0)
    _sub(env, run, doc, "electrical_drawings", 1)

    def race():
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            runs.advance(env.db, run["id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    race()
    rollups = env.db.rows("plans_reader_groups", kind="rollup")
    assert len(rollups) == 1
    assert [j["target_id"] for j in env.enqueued] == [rollups[0]["id"]]
    env.db.table("plans_reader_groups").update(
        {"status": "done", "output": sub_output("electrical_drawings", [(1, 1)], summary="merged")}
    ).eq("id", rollups[0]["id"]).execute()
    race()
    mains = env.db.rows("plans_reader_groups", kind="main")
    assert len(mains) == 1
    assert [j["job_type"] for j in env.enqueued].count("plans_reader_main") == 1
    out = env.db.rows("plans_reader_outputs", category="electrical_drawings")
    assert len(out) == 1 and out[0]["output"]["summary"] == "merged" and out[0]["output"]["source"]["rolled_up"]


def test_insert_dedupe_keeps_the_earliest_row(env):
    run = _reading_run(env)
    earlier = env.db.add("plans_reader_groups", run_id=run["id"], kind="rollup", category="electrical_drawings")
    mine = runs._insert_deduped(env.db, run, [{"run_id": run["id"], "kind": "rollup", "category": "electrical_drawings",
                                               "status": "queued"}])
    assert mine == []
    assert [g["id"] for g in env.db.rows("plans_reader_groups", kind="rollup")] == [earlier["id"]]


def test_insert_dedupe_keeps_a_running_row_over_an_earlier_stamped_one(env):
    """Commit-order inversion: this caller's row carries an earlier
    created_at than a racer's row whose job is already running. The running
    row wins and this caller enqueues nothing, so one call runs."""
    run = _reading_run(env)
    running = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="running",
                         created_at="2026-10-17T12:00:01+00:00")
    mine = runs._insert_deduped(env.db, run, [{"run_id": run["id"], "kind": "main", "status": "queued",
                                               "created_at": "2026-10-17T12:00:00+00:00"}])
    assert mine == []
    assert [g["id"] for g in env.db.rows("plans_reader_groups", kind="main")] == [running["id"]]


def test_insert_dedupe_yields_to_a_loser_that_started_meanwhile(env, monkeypatch):
    run = _reading_run(env)
    later = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="queued",
                       created_at="2026-10-17T12:00:01+00:00")
    real_delete = runs._delete_if_queued

    def racing_delete(sb, group_id):
        if group_id == later["id"]:  # its job claimed it between the read and the delete
            sb.table("plans_reader_groups").update({"status": "running"}).eq("id", group_id).execute()
        return real_delete(sb, group_id)

    monkeypatch.setattr(runs, "_delete_if_queued", racing_delete)
    mine = runs._insert_deduped(env.db, run, [{"run_id": run["id"], "kind": "main", "status": "queued",
                                               "created_at": "2026-10-17T12:00:00+00:00"}])
    assert mine == []
    assert [g["id"] for g in env.db.rows("plans_reader_groups", kind="main")] == [later["id"]]


def test_flagged_sheet_gets_an_escalation_group_within_budget(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    flagged = sub_output("electrical_drawings", [(1, 1)])
    flagged["pages"][0]["resolution_flags"] = [{"doc_index": 1, "page": 1, "region": "r2c3", "reason": "tiny keyed notes"}]
    agents.map_doc_index(flagged, {1: doc["id"]})
    _sub(env, run, doc, "electrical_drawings", 0, output=flagged, est=200_000)
    runs.advance(env.db, run["id"])
    esc = env.db.rows("plans_reader_groups", kind="escalation")
    assert len(esc) == 1 and esc[0]["pass"] == 2
    item = esc[0]["pages"][0]
    assert item["document_id"] == doc["id"] and item["page"] == 1 and item["ppi"] == 260
    assert item["regions"] == ["r2c3"] and item["reasons"] == ["tiny keyed notes"]
    assert item["tiles"] and all(0 <= v <= 1 for t in item["tiles"] for v in t)
    # The region is the middle-right cell: every tile sits in its box.
    assert min(t[0] for t in item["tiles"]) >= 0.66 and max(t[3] for t in item["tiles"]) <= 0.67
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"
    runs.advance(env.db, run["id"])
    assert len(env.db.rows("plans_reader_groups", kind="escalation")) == 1
    # Patched: the category output carries the patch and no unresolved flag.
    env.db.table("plans_reader_groups").update(
        {"status": "done", "output": sub_output("electrical_drawings", [(1, 1)], summary="patch")}
    ).eq("id", esc[0]["id"]).execute()
    runs.advance(env.db, run["id"])
    out = env.db.rows("plans_reader_outputs", category="electrical_drawings")[0]["output"]
    assert [p["summary"] for p in out["escalation_patches"]] == ["patch"] and out["unresolved_flags"] == []


def test_escalation_respects_the_budget_and_records_unresolved_flags(env):
    env.settings = make_settings(plans_reader_escalation_max_fraction=0.0)
    run = _reading_run(env)
    doc = seed_document(env, run)
    flagged = sub_output("electrical_drawings", [(1, 1)])
    flagged["pages"][0]["resolution_flags"] = [{"doc_index": 1, "page": 1, "region": "whole", "reason": "faint"}]
    agents.map_doc_index(flagged, {1: doc["id"]})
    _sub(env, run, doc, "electrical_drawings", 0, output=flagged)
    runs.advance(env.db, run["id"])
    assert not env.db.rows("plans_reader_groups", kind="escalation")
    out = env.db.rows("plans_reader_outputs", category="electrical_drawings")[0]["output"]
    assert out["unresolved_flags"] == [
        {"document_id": doc["id"], "doc_index": 1, "page": 1, "region": "whole", "reason": "faint"}
    ]


def _age(env, run, stamp="2026-01-01T00:00:00+00:00"):
    for row in env.db.tables["plans_reader_runs"]:
        if row["id"] == run["id"]:
            row["updated_at"] = stamp


def test_reading_without_groups_waits_for_its_winner_and_repairs_only_when_stale(env):
    run = _reading_run(env)
    seed_document(env, run)
    runs.advance(env.db, run["id"])  # a winner may be inserting right now
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"
    assert not env.db.rows("plans_reader_groups")
    _age(env, run)
    runs.advance(env.db, run["id"])  # its winner died: plan again
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"
    assert len(env.db.rows("plans_reader_groups", kind="sub")) == 1 and len(env.enqueued) == 1


def test_consolidating_without_a_main_group_is_repaired_only_when_stale(env):
    """The consolidating winner died after writing one of two category
    outputs: the repair writes the missing one (not the existing one twice),
    then queues the main group with a real input estimate."""
    run = _reading_run(env)
    doc = seed_document(env, run)
    spec_doc = seed_document(env, run, category="specifications")
    _sub(env, run, doc, "electrical_drawings", 0)
    _sub(env, run, spec_doc, "specifications", 0, output=sub_output("specifications", [(1, 1)]))
    env.db.add("plans_reader_outputs", run_id=run["id"], scope="category", category="electrical_drawings",
               version=1, output=sub_output("electrical_drawings", [(1, 1)]), source_group_ids=[])
    run = set_status(env, run, "consolidating")
    runs.advance(env.db, run["id"])
    assert not env.db.rows("plans_reader_groups", kind="main")
    _age(env, run)
    runs.advance(env.db, run["id"])
    outputs = env.db.rows("plans_reader_outputs", run_id=run["id"], scope="category")
    assert sorted(o["category"] for o in outputs) == ["electrical_drawings", "specifications"]
    mains = env.db.rows("plans_reader_groups", kind="main")
    assert len(mains) == 1 and mains[0]["est_input_tokens"] > agents.prompt_tokens("main")
    assert [j["job_type"] for j in env.enqueued] == ["plans_reader_main"]
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "consolidating"


def test_split_hook_for_an_unknown_file_is_a_no_op(env):
    runs.on_split_file_done("not-a-plans-reader-file")
    assert env.db.statements[-1][0] == "plans_reader_files"


def test_all_groups_failed_fails_the_run(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    _sub(env, run, doc, "electrical_drawings", 0, status="failed")
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "failed" and runs.MSG_ALL_GROUPS_FAILED in fresh["error"]
    assert not env.db.rows("plans_reader_groups", kind="main")


# ---------------------------------------------------------------------------
# consolidating -> rendering -> done
# ---------------------------------------------------------------------------


@pytest.fixture
def renders(monkeypatch):
    seen = SimpleNamespace(markup_sources=None, marked_pages=None, estimate_date=None)
    monkeypatch.setattr(render, "render_boq_xlsx", lambda spec: b"PK-boq")

    def estimate(boq, est, *, date):
        seen.estimate_date = date
        return b"PK-estimate"

    def markup(sources, sheets, *, kind, **_k):
        seen.markup_sources = (kind, sorted(sources))
        return b"%PDF-markup"

    def marked(pages):
        # The real renderer iterates the pages once (they are rendered lazily).
        seen.marked_pages = list(pages)
        return b"%PDF-marked"

    monkeypatch.setattr(render, "render_estimate_xlsx", estimate)
    monkeypatch.setattr(render, "render_markup_pdf", markup)
    monkeypatch.setattr(render, "render_marked_plans_pdf", marked)
    monkeypatch.setattr(render, "render_brief_docx", lambda spec, *, project_name: b"PK-docx")
    monkeypatch.setattr(render, "brief_pdf_bytes", lambda docx: b"%PDF-brief")
    seen.office_pdfs = []

    def office_pdf(content, filename):
        seen.office_pdfs.append(filename)
        return b"%PDF-copy-" + content

    monkeypatch.setattr(render, "office_pdf_bytes", office_pdf)
    return seen


def _consolidating_run(env, *, main_status="done", failed_sub=False):
    run = _reading_run(env)
    doc = seed_document(env, run, pages=1)
    _sub(env, run, doc, "electrical_drawings", 0)
    if failed_sub:
        _sub(env, run, doc, "electrical_drawings", 1, status="failed")
    run = set_status(env, run, "consolidating")
    env.db.add("plans_reader_outputs", run_id=run["id"], scope="category", category="electrical_drawings",
               version=1, output=sub_output("electrical_drawings", [(1, 1)]), source_group_ids=[])
    main = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status=main_status,
                      output=main_output_spec(doc["id"]) if main_status == "done" else None,
                      error=None if main_status == "done" else "The model timed out.")
    return run, doc, main


def test_main_done_renders_every_deliverable_and_finishes(env, renders):
    run, doc, main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "done" and fresh["finished_at"] and fresh["error"] is None
    assert env.db.rows("plans_reader_outputs", scope="main")[0]["output"]["boq"]["title"] == "Test"
    rows = {d["kind"]: d for d in env.db.rows("plans_reader_deliverables", run_id=run["id"])}
    assert set(rows) == {"boq", "estimate", "trench_markup", "marked_plans", "brief"}  # no sawcut sheets
    assert all(d["version"] == 1 for d in rows.values())
    assert rows["boq"]["filename"] == "BOQ - COUNTS.xlsx"
    # Named after the run's project (the run name here), never the model's text.
    assert rows["estimate"]["filename"] == "EDIKTED TI - ELECTRICAL ESTIMATE.xlsx"
    assert rows["trench_markup"]["spec_json"]["sheets"][0]["kind"] == "trench"
    for d in rows.values():
        assert d["storage_path"].startswith(f"plans-reader/{run['id']}/deliverables/v1/")
        assert env.store[d["storage_path"]]
    assert env.store[runs.brief_pdf_path(rows["brief"]["storage_path"])] == b"%PDF-brief"
    # The workbooks get a PDF copy beside them too, for the in-app preview.
    assert env.store[runs.pdf_copy_path(rows["boq"]["storage_path"])] == b"%PDF-copy-PK-boq"
    assert env.store[runs.pdf_copy_path(rows["estimate"]["storage_path"])] == b"%PDF-copy-PK-estimate"
    assert renders.office_pdfs == ["BOQ - COUNTS.xlsx", "EDIKTED TI - ELECTRICAL ESTIMATE.xlsx"]
    assert renders.markup_sources == ("trench_markup", [doc["id"]])
    assert renders.marked_pages[0]["png_bytes"][:4] == b"\x89PNG"
    assert renders.marked_pages[0]["legend"][0]["prefix"] == "R"
    assert renders.estimate_date == runs.today_pacific()
    assert env.db.rows("plans_reader_documents", id=doc["id"])[0]["status"] == "done"
    runs.advance(env.db, run["id"])  # nothing renders twice
    assert len(env.db.rows("plans_reader_deliverables", run_id=run["id"])) == 5


def test_a_renderer_failure_is_recorded_and_the_others_still_render(env, renders, monkeypatch):
    def broken(spec):
        raise ValueError("The BOQ spec has no areas.")

    monkeypatch.setattr(render, "render_boq_xlsx", broken)
    run, _doc, _main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "done_with_errors"
    assert "BOQ: The BOQ spec has no areas." in fresh["error"]
    kinds = {d["kind"] for d in env.db.rows("plans_reader_deliverables", run_id=run["id"])}
    assert kinds == {"estimate", "trench_markup", "marked_plans", "brief"}


def test_a_failed_reading_group_ends_done_with_errors(env, renders):
    run, _doc, _main = _consolidating_run(env, failed_sub=True)
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "done_with_errors" and "1 reading step(s) failed" in fresh["error"]
    assert fresh["groups_failed"] == 1


def test_a_failed_main_group_is_retried_once_at_lower_effort_then_fails_the_run(env, renders):
    run, _doc, main = _consolidating_run(env, main_status="failed")
    assert run["reasoning_effort"] == "medium"
    runs.advance(env.db, run["id"])
    runs.advance(env.db, run["id"])  # a second worker seeing the failure queues nothing more
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "consolidating"
    assert "failed at medium reasoning effort (The model timed out.); retrying once at low." in fresh["error"]
    retries = [g for g in env.db.rows("plans_reader_groups", kind="main") if g["pass"] == runs.MAIN_RETRY_PASS]
    assert len(retries) == 1 and retries[0]["status"] == "queued"
    assert retries[0]["est_input_tokens"] == (main["est_input_tokens"] or 0)
    assert [(j["job_type"], j["target_id"]) for j in env.enqueued] == [("plans_reader_main", retries[0]["id"])]
    assert agents.group_reasoning_effort(fresh, retries[0]) == "low"
    # The retry fails too: now the run fails, naming both failures.
    env.db.table("plans_reader_groups").update({"status": "failed", "error": "Cut off again."}).eq(
        "id", retries[0]["id"]).execute()
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "failed" and fresh["finished_at"]
    assert "The model timed out." in fresh["error"] and "The retry at low reasoning effort failed too: Cut off again." in fresh["error"]
    assert not env.db.rows("plans_reader_deliverables")
    assert len(env.enqueued) == 1


def test_a_successful_main_retry_renders_and_the_run_is_done(env, renders):
    run, doc, _main = _consolidating_run(env, main_status="failed")
    runs.advance(env.db, run["id"])
    retry = next(g for g in env.db.rows("plans_reader_groups", kind="main") if g["pass"] == runs.MAIN_RETRY_PASS)
    env.db.table("plans_reader_groups").update({"status": "done", "output": main_output_spec(doc["id"])}).eq(
        "id", retry["id"]).execute()
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    # The replaced first attempt is not a missing reading step, and not a
    # group of the result either: the counters add up.
    assert fresh["status"] == "done" and fresh["groups_failed"] == 0
    assert fresh["groups_total"] == fresh["groups_done"] + fresh["groups_failed"] == 2  # the sub and the retry
    # A run the retry saved shows no error.
    assert fresh["error"] is None
    (main_out,) = env.db.rows("plans_reader_outputs", scope="main")
    assert main_out["source_group_ids"] == [retry["id"]]
    assert {d["kind"] for d in env.db.rows("plans_reader_deliverables", run_id=run["id"])} >= {"boq", "brief"}


def test_a_main_retry_that_wins_on_a_run_with_errors_says_the_retry_succeeded(env, renders):
    run, doc, _main = _consolidating_run(env, main_status="failed", failed_sub=True)
    runs.advance(env.db, run["id"])
    retry = next(g for g in env.db.rows("plans_reader_groups", kind="main") if g["pass"] == runs.MAIN_RETRY_PASS)
    env.db.table("plans_reader_groups").update({"status": "done", "output": main_output_spec(doc["id"])}).eq(
        "id", retry["id"]).execute()
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "done_with_errors"
    assert fresh["groups_total"] == 3 and fresh["groups_done"] == 2 and fresh["groups_failed"] == 1
    assert fresh["groups_total"] == fresh["groups_done"] + fresh["groups_failed"]
    assert "retrying once" not in fresh["error"]
    assert "The consolidation step failed at medium reasoning effort; the retry at low succeeded." in fresh["error"]
    assert "1 reading step(s) failed" in fresh["error"]


def test_settling_the_retry_note_keeps_other_notes():
    run = {"reasoning_effort": "medium", "error": "Other note. | The consolidation step failed at medium "
           "reasoning effort (timed out); retrying once at low."}
    groups = [{"id": "m1", "kind": "main", "pass": 1}, {"id": "m2", "kind": "main", "pass": 2}]
    settled = runs._settle_retry_note(run, groups[1], groups)
    assert settled == "Other note. | The consolidation step failed at medium reasoning effort; the retry at low succeeded."
    assert runs._strip_retry_success(settled) == "Other note."
    # The first attempt answered: nothing to settle.
    assert runs._settle_retry_note(run, groups[0], groups) == run["error"]


def test_a_failed_main_group_at_low_effort_fails_the_run_at_once(env, renders):
    run, _doc, _main = _consolidating_run(env, main_status="failed")
    run = set_status(env, run, "consolidating", reasoning_effort="low")
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "failed" and "The model timed out." in fresh["error"]
    assert len(env.db.rows("plans_reader_groups", kind="main")) == 1 and env.enqueued == []


def test_reasoning_effort_ladder():
    assert [agents.lower_reasoning_effort(e) for e in ("max", "xhigh", "high", "medium", "low", "bogus", None)] == [
        "xhigh", "high", "medium", "low", None, None, None,
    ]
    run = {"reasoning_effort": "high"}
    assert agents.group_reasoning_effort(run, {"kind": "main", "pass": 1}) == "high"
    assert agents.group_reasoning_effort(run, {"kind": "main", "pass": 2}) == "medium"
    assert agents.group_reasoning_effort(run, {"kind": "escalation", "pass": 2}) == "high"
    assert agents.group_reasoning_effort({"reasoning_effort": "low"}, {"kind": "main", "pass": 2}) == "low"


def test_deliverable_version_conflict_moves_to_the_next_version(env, renders):
    run, doc, _main = _consolidating_run(env)
    env.db.add("plans_reader_deliverables", run_id=run["id"], kind="boq", version=1, filename="x",
               storage_path="x", spec_json={})
    row = runs.render_deliverable(env.db, run, "boq", main_output_spec(doc["id"])["boq"], version=1)
    assert row["version"] == 2 and row["filename"] == "Rev 2 - BOQ - COUNTS.xlsx"


def test_rerender_now_uses_the_latest_spec_or_the_main_output(env, renders):
    run, doc, main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    row = runs.rerender_now(env.db, run, "boq")
    assert row["version"] == 2 and row["instruction"] is None
    with pytest.raises(runs.RunRefused) as exc:
        runs.rerender_now(env.db, run, "sawcut_markup")
    assert exc.value.status_code == 404


def test_rerender_is_queued_off_the_request_as_a_modify_job(env, renders):
    run, doc, main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    done = env.db.rows("plans_reader_runs", id=run["id"])[0]
    before = len(env.db.rows("plans_reader_deliverables", run_id=run["id"]))
    out = runs.rerender_deliverable(env.db, done, "boq", user=user())
    assert set(out) == {"group", "queue", "kind", "version"}
    assert out["kind"] == "boq" and out["version"] == 2
    group = env.db.rows("plans_reader_groups", id=out["group"]["id"])[0]
    assert group["kind"] == "modify" and group["status"] == "queued"
    assert group["output"] == {"kind": "boq", "rerender": True}
    (job,) = env.enqueued
    assert job["job_type"] == "plans_reader_modify" and job["target_id"] == group["id"]
    assert job["payload"] == {
        "group_id": group["id"], "run_id": run["id"], "kind": "boq", "instruction": None, "rerender": True,
        "user_id": "u-1", "base_version": 1,
    }
    assert group["llm_job_id"] == job["id"] and out["queue"] == {"state": "queued", "position": 1}
    # Nothing rendered in the request.
    assert len(env.db.rows("plans_reader_deliverables", run_id=run["id"])) == before
    # One change per run at a time (the 0153 index).
    with pytest.raises(runs.RunRefused) as exc:
        runs.rerender_deliverable(env.db, done, "estimate", user=user())
    assert exc.value.status_code == 409 and runs.MSG_CHANGE_ACTIVE in str(exc.value)
    assert len(env.db.rows("plans_reader_groups", kind="modify")) == 1


def test_rerender_refusals(env, renders, monkeypatch):
    run, doc, main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    done = env.db.rows("plans_reader_runs", id=run["id"])[0]
    with pytest.raises(runs.RunRefused) as exc:
        runs.rerender_deliverable(env.db, done, "sawcut_markup")
    assert exc.value.status_code == 404
    with pytest.raises(runs.RunRefused) as exc:
        runs.rerender_deliverable(env.db, done, "invoice")
    assert exc.value.status_code == 400
    with pytest.raises(runs.RunRefused) as exc:
        runs.rerender_deliverable(env.db, set_status(env, done, "rendering"), "boq")
    assert exc.value.status_code == 409
    done = set_status(env, done, "done")
    env.settings = make_settings(llm_queue_enabled=False)
    with pytest.raises(runs.RunRefused) as exc:
        runs.rerender_deliverable(env.db, done, "boq")
    assert exc.value.status_code == 409 and "queue" in str(exc.value)
    env.settings = make_settings()

    def broken_enqueue(*_a, **_k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(llm_queue, "enqueue", broken_enqueue)
    with pytest.raises(RuntimeError):
        runs.rerender_deliverable(env.db, done, "boq")
    assert not env.db.rows("plans_reader_groups", kind="modify")  # no group left for a job that is not coming
    assert env.enqueued == []


def test_change_slot_without_the_0153_index_keeps_the_oldest_claim(env):
    env.db.partial_unique = False
    run = new_run(env)
    first = runs.claim_change_slot(env.db, run["id"])
    with pytest.raises(runs.RunRefused) as exc:
        runs.claim_change_slot(env.db, run["id"])
    assert exc.value.status_code == 409
    assert [g["id"] for g in env.db.rows("plans_reader_groups", kind="modify")] == [first["id"]]
    env.db.table("plans_reader_groups").update({"status": "done"}).eq("id", first["id"]).execute()
    assert runs.claim_change_slot(env.db, run["id"])["status"] == "queued"


# ---------------------------------------------------------------------------
# cancel / delete / resume / totals / payload
# ---------------------------------------------------------------------------


def test_cancel_cancels_groups_and_their_queued_jobs(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    g = _sub(env, run, doc, "electrical_drawings", 0, status="queued")
    env.db.table("plans_reader_groups").update({"llm_job_id": "job-9"}).eq("id", g["id"]).execute()
    out = runs.cancel(env.db, run, user=user())
    assert out["status"] == "canceled"
    assert env.db.rows("plans_reader_groups", id=g["id"])[0]["status"] == "canceled"
    assert env.canceled == ["job-9"]
    with pytest.raises(runs.RunRefused):
        runs.cancel(env.db, run, user=user())


def test_delete_refuses_a_busy_run_and_sweeps_everything_otherwise(env):
    run = _reading_run(env)
    with pytest.raises(runs.RunRefused) as exc:
        runs.delete(env.db, run, user=user())
    assert exc.value.status_code == 409
    job = env.db.add("bid_split_jobs", status="done", source="plans_reader")
    run = set_status(env, run, "canceled", split_job_id=job["id"])
    env.store[f"plans-reader/{run['id']}/deliverables/v1/a.xlsx"] = b"x"
    env.store[f"bid-splits/{job['id']}/output/b.pdf"] = b"y"
    env.store["plans-reader/other-run/source/c.pdf"] = b"z"
    assert runs.delete(env.db, run, user=user()) == {"id": run["id"], "deleted": True}
    assert list(env.store) == ["plans-reader/other-run/source/c.pdf"]
    assert not env.db.rows("plans_reader_runs", id=run["id"]) and not env.db.rows("bid_split_jobs")


def test_resume_after_over_cap_requeues_the_queued_groups(env):
    run = _reading_run(env)
    doc = seed_document(env, run)
    queued = _sub(env, run, doc, "electrical_drawings", 0, status="queued")
    _sub(env, run, doc, "electrical_drawings", 1, status="running")
    run = set_status(env, run, "over_cap", error="cap")
    env.cap_error = "still capped"
    with pytest.raises(ai_spend.SpendCapReached):
        runs.resume(env.db, run)
    env.cap_error = None
    out = runs.resume(env.db, run)
    assert out["status"] == "reading" and out["error"] is None
    assert [j["target_id"] for j in env.enqueued] == [queued["id"]]
    with pytest.raises(runs.RunRefused):
        runs.resume(env.db, set_status(env, run, "done"))


def test_recompute_totals_sums_calls(env):
    run = new_run(env)
    for i in range(3):
        env.db.add("plans_reader_calls", run_id=run["id"], kind="sub", input_tokens=1000, cached_tokens=100,
                   output_tokens=200, reasoning_tokens=50, cost_usd=0.0125)
    env.db.add("plans_reader_calls", run_id=run["id"], kind="sub", ok=False, input_tokens=None, cost_usd=None)
    totals = runs.recompute_totals(env.db, run["id"])
    assert totals["input_tokens"] == 3000 and totals["calls"] == 4 and totals["cost_usd"] == 0.0375
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["output_tokens"] == 600 and fresh["cost_usd"] == 0.0375


def test_list_runs_filters_and_searches(env):
    a = new_run(env, name="Edikted TI")
    new_run(env, name="YMCA site")
    set_status(env, a, "done")
    assert [r["name"] for r in runs.list_runs(env.db, status="done")] == ["Edikted TI"]
    assert [r["name"] for r in runs.list_runs(env.db, q="ymca")] == ["YMCA site"]
    assert len(runs.list_runs(env.db, limit=1)) == 1
    with pytest.raises(runs.RunRefused):
        runs.list_runs(env.db, status="bogus")


def test_get_run_payload_shape(env, renders):
    run, doc, main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    env.db.add("plans_reader_calls", run_id=run["id"], group_id=main["id"], kind="main", input_tokens=5000,
               output_tokens=900, cost_usd=0.095)
    queued = _sub(env, run, doc, "electrical_drawings", 7, status="queued")
    payload = runs.get_run_payload(env.db, run["id"])
    assert set(payload) == {"run", "files", "documents", "documents_by_category", "groups", "outputs",
                            "deliverables", "totals", "queue", "calls"}
    assert [c["group_id"] for c in payload["calls"]] == [main["id"]]
    assert all(isinstance(g["document_pages"], dict) for g in payload["groups"])
    assert sum(g["document_pages"].get(doc["id"], 0) for g in payload["groups"]) >= 1
    assert "ledger" not in payload["run"]
    assert all("page_meta" not in d for d in payload["documents"])
    assert payload["documents_by_category"] == {"electrical_drawings": [doc["id"]]}
    g = next(x for x in payload["groups"] if x["id"] == main["id"])
    assert g["input_tokens"] == 5000 and g["cost_usd"] == 0.095 and g["has_output"] is True
    assert "output" not in g
    assert {(o["scope"], o["category"]) for o in payload["outputs"]} == {("category", "electrical_drawings"), ("main", None)}
    brief = next(d for d in payload["deliverables"] if d["kind"] == "brief")
    assert brief["pdf_available"] is True and brief["pdf_filename"] == "Deep Brief.pdf"
    boq = next(d for d in payload["deliverables"] if d["kind"] == "boq")
    assert boq["pdf_available"] is True and boq["pdf_filename"] == "BOQ - COUNTS.pdf"
    assert all("pdf_filename" not in d for d in payload["deliverables"] if d["kind"] not in runs.PDF_COPY_KINDS)
    assert all("storage_path" not in d for d in payload["deliverables"])
    assert payload["totals"]["calls"] == 1 and payload["queue"] == {queued["id"]: {"state": "queued", "position": 1}}
    assert runs.get_run_payload(env.db, "missing") is None


def test_poll_settles_a_split_file_whose_hook_was_missed(env, monkeypatch):
    monkeypatch.setattr(bid_split, "sweep_stale_files", lambda job_id, sb=None: [])
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(3), label=None, user=user())
    runs.start(env.db, run, user=user())
    split_id = env.db.rows("plans_reader_files", run_id=run["id"])[0]["split_file_id"]
    _split_result(env, run, split_id)  # done, but the hook never fired
    runs.get_run_payload(env.db, run["id"])
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------


def test_build_plans_reader_path():
    path = storage.build_plans_reader_path("run-1", "deliverables/v2", "Deep Brief~1.docx")
    assert re.fullmatch(r"plans-reader/run-1/deliverables/v2/[0-9a-f]{32}-Deep Brief_1\.docx", path)
    for bad in ("", "../x", "a//b", "a/..", "a b"):
        with pytest.raises(ValueError):
            storage.build_plans_reader_path("run-1", bad, "x.pdf")


def test_delete_plans_reader_prefix_walks_every_level(monkeypatch):
    tree = {
        "plans-reader/r1": [{"name": "source", "id": None}, {"name": "deliverables", "id": None}],
        "plans-reader/r1/source": [{"name": "a.pdf", "id": "1"}],
        "plans-reader/r1/deliverables": [{"name": "v1", "id": None}, {"name": "v2", "id": None}],
        "plans-reader/r1/deliverables/v1": [{"name": "b.xlsx", "id": "2"}, {"name": "c.docx", "id": "3"}],
        "plans-reader/r1/deliverables/v2": [{"name": "d.xlsx", "id": "4"}],
    }
    removed = []

    class Store:
        def list(self, path, opts):
            return tree.get(path, [])[opts["offset"]:opts["offset"] + opts["limit"]]

        def remove(self, paths):
            removed.extend(paths)

    monkeypatch.setattr(storage, "get_supabase", lambda: SimpleNamespace(storage=SimpleNamespace(from_=lambda b: Store())))
    storage.delete_plans_reader_prefix("r1")
    assert sorted(removed) == [
        "plans-reader/r1/deliverables/v1/b.xlsx", "plans-reader/r1/deliverables/v1/c.docx",
        "plans-reader/r1/deliverables/v2/d.xlsx", "plans-reader/r1/source/a.pdf",
    ]
    with pytest.raises(ValueError):
        storage.delete_plans_reader_prefix("r1/../x")


# ---------------------------------------------------------------------------
# Races and repairs (post-review)
# ---------------------------------------------------------------------------


def test_upload_finishing_after_start_is_taken_back_out(env, monkeypatch):
    """start ran while the upload was converting/storing: the inserted row
    and its object go away and the upload is refused, so no pending file
    holds the started run in splitting."""
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(1), label="electrical_drawings", user=user())
    real_upload = storage.upload_file

    def upload_then_start(path, content, content_type, *, upsert=False):
        real_upload(path, content, content_type)
        if "/source/" in path and "late" in path:
            set_status(env, run, "splitting")

    monkeypatch.setattr(storage, "upload_file", upload_then_start)
    with pytest.raises(runs.RunRefused) as exc:
        runs.add_upload(env.db, run, filename="late.pdf", content=make_pdf(1), label="electrical_drawings",
                        user=user())
    assert exc.value.status_code == 409 and runs.MSG_STARTED_DURING_UPLOAD in str(exc.value)
    assert [f["filename"] for f in env.db.rows("plans_reader_files", run_id=run["id"])] == ["E.pdf"]
    assert not [k for k in env.store if "late" in k]


def test_file_inserted_while_start_runs_is_failed_not_left_pending(env, monkeypatch):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(1), label="electrical_drawings", user=user())
    real_estimate = runs.estimate

    def estimate_with_late_upload(sb, r):
        out = real_estimate(sb, r)
        env.db.add("plans_reader_files", run_id=run["id"], filename="late.pdf", storage_path="x/late.pdf",
                   label="electrical_drawings", status="pending")
        return out

    monkeypatch.setattr(runs, "estimate", estimate_with_late_upload)
    out = runs.start(env.db, run, user=user())
    assert out["status"] == "reading"  # not stuck in splitting
    late = next(f for f in env.db.rows("plans_reader_files", run_id=run["id"]) if f["filename"] == "late.pdf")
    assert late["status"] == "failed" and late["error"] == runs.MSG_ADDED_DURING_START
    assert runs.MSG_ADDED_DURING_START in out["error"]


def _rendering_run(env, renders):
    run, doc, main = _consolidating_run(env)
    run = set_status(env, run, "rendering")
    # The dead worker had saved the BOQ before it died.
    runs.render_deliverable(env.db, run, "boq", main["output"]["boq"], version=1)
    return run, doc, main


def test_a_run_stuck_in_rendering_is_finished_only_when_stale(env, renders):
    run, _doc, _main = _rendering_run(env, renders)
    runs.advance(env.db, run["id"])  # a live worker may still be rendering
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "rendering"
    assert [d["kind"] for d in env.db.rows("plans_reader_deliverables", run_id=run["id"])] == ["boq"]
    _age(env, run)
    runs.advance(env.db, run["id"])
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "done"
    rows = env.db.rows("plans_reader_deliverables", run_id=run["id"])
    assert sorted(d["kind"] for d in rows) == ["boq", "brief", "estimate", "marked_plans", "trench_markup"]
    assert all(d["version"] == 1 for d in rows)  # the saved BOQ was not rendered again


def test_resume_of_a_rendering_run_needs_it_stale_and_skips_the_cap(env, renders):
    run, _doc, _main = _rendering_run(env, renders)
    with pytest.raises(runs.RunRefused) as exc:
        runs.resume(env.db, run)
    assert exc.value.status_code == 409
    assert len(env.db.rows("plans_reader_deliverables", run_id=run["id"])) == 1
    _age(env, run)
    env.cap_error = "capped"  # rendering makes no model call
    out = runs.resume(env.db, env.db.rows("plans_reader_runs", id=run["id"])[0])
    assert out["status"] == "done"
    assert len(env.db.rows("plans_reader_deliverables", run_id=run["id"])) == 5


def test_poll_kicks_a_stale_rendering_run(env, renders, monkeypatch):
    run, _doc, _main = _rendering_run(env, renders)
    kicks = []
    monkeypatch.setattr(runs, "_kick_advance", lambda run_id, force=False: kicks.append(run_id))
    runs.get_run_payload(env.db, run["id"])
    assert kicks == []
    _age(env, run)
    runs.get_run_payload(env.db, run["id"])
    assert kicks == [run["id"]]


def test_poll_requeues_a_group_whose_enqueue_was_lost(env, monkeypatch):
    run = _reading_run(env)
    doc = seed_document(env, run)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    lost = _sub(env, run, doc, "electrical_drawings", 0, status="queued")
    live = _sub(env, run, doc, "electrical_drawings", 1, status="queued")
    young = _sub(env, run, doc, "electrical_drawings", 2, status="queued")
    for g, job in ((lost, None), (live, "job-live"), (young, None)):
        for row in env.db.tables["plans_reader_groups"]:
            if row["id"] == g["id"]:
                row["llm_job_id"] = job
                row["created_at"] = old if g is not young else row["created_at"]
    env.db.add("llm_jobs", id="job-live", status="queued")
    runs._REQUEUE_CHECKED.clear()
    runs.get_run_payload(env.db, run["id"])
    assert [j["target_id"] for j in env.enqueued] == [lost["id"]]
    assert env.db.rows("plans_reader_groups", id=lost["id"])[0]["llm_job_id"] == env.enqueued[0]["id"]
    runs.get_run_payload(env.db, run["id"])  # rate limited per run and process
    assert len(env.enqueued) == 1


def test_second_settler_of_a_split_file_writes_nothing(env):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(3), label=None, user=user())
    runs.start(env.db, run, user=user())
    frow = env.db.rows("plans_reader_files", run_id=run["id"])[0]
    _split_result(env, run, frow["split_file_id"])
    # Another caller claimed the file a moment ago and is writing its documents.
    env.db.table("plans_reader_files").update({"status": runs.FILE_SETTLING}).eq("id", frow["id"]).execute()
    assert runs._settle_split_file(env.db, frow) is False
    assert not env.db.rows("plans_reader_documents", run_id=run["id"])
    runs.advance(env.db, run["id"])  # a settling file is unsettled: the run waits
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "splitting"


def test_poll_reclaims_a_settling_file_whose_settler_died(env, monkeypatch):
    monkeypatch.setattr(bid_split, "sweep_stale_files", lambda job_id, sb=None: [])
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(3), label=None, user=user())
    runs.start(env.db, run, user=user())
    frow = env.db.rows("plans_reader_files", run_id=run["id"])[0]
    _split_result(env, run, frow["split_file_id"])
    env.db.table("plans_reader_files").update({"status": runs.FILE_SETTLING}).eq("id", frow["id"]).execute()
    runs.get_run_payload(env.db, run["id"])  # the run is fresh: a settler may be writing
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "splitting"
    _age(env, run)
    runs.get_run_payload(env.db, run["id"])
    assert env.db.rows("plans_reader_files", id=frow["id"])[0]["status"] == "split"
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"
    assert len(env.db.rows("plans_reader_documents", run_id=run["id"])) == 2


def test_a_live_planning_claim_keeps_other_callers_from_planning(env, monkeypatch):
    run = new_run(env)
    seed_document(env, run)
    now = datetime.now(timezone.utc).isoformat()
    run = set_status(env, run, "splitting", ledger={runs.PLANNING_CLAIM_KEY: {"at": now}})
    planned = []
    real_plan = runs._plan_reading
    monkeypatch.setattr(runs, "_plan_reading", lambda *a: planned.append(1) or real_plan(*a))
    runs.advance(env.db, run["id"])
    assert planned == [] and env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "splitting"
    # The holder died long ago: the claim is dead and the next caller plans.
    set_status(env, run, "splitting", ledger={runs.PLANNING_CLAIM_KEY: {"at": "2026-01-01T00:00:00+00:00"}})
    runs.advance(env.db, run["id"])
    assert planned == [1] and env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"


def test_a_caller_that_read_the_run_before_the_claim_loses_it(env):
    run = new_run(env)
    run = set_status(env, run, "splitting")
    first = runs._claim_planning(env.db, run)
    assert first is not None and runs.PLANNING_CLAIM_KEY in first["ledger"]
    assert runs._claim_planning(env.db, run) is None  # stale read: CAS lost, re-read sees the claim
    assert runs._claim_planning(env.db, first) is None  # fresh read: live claim


def test_a_claim_lost_to_an_unrelated_write_is_retried(env):
    run = new_run(env)
    run = set_status(env, run, "splitting")
    env.db.table("plans_reader_runs").update({"error": "x.pdf: refused"}).eq("id", run["id"]).execute()
    claimed = runs._claim_planning(env.db, run)  # read before that write
    assert claimed is not None and runs.PLANNING_CLAIM_KEY in claimed["ledger"]


def test_planning_error_releases_the_claim(env, monkeypatch):
    run = new_run(env)
    seed_document(env, run)
    run = set_status(env, run, "splitting")

    def boom(*_a):
        raise RuntimeError("storage outage")

    monkeypatch.setattr(runs, "_plan_reading", boom)
    with pytest.raises(RuntimeError):
        runs.advance(env.db, run["id"])
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["ledger"] is None


def test_background_kick_runs_once_per_run_at_a_time(monkeypatch):
    gate = threading.Event()
    calls = []

    def slow_advance(sb, run_id):
        calls.append(run_id)
        gate.wait(5)

    monkeypatch.setattr(runs, "advance", slow_advance)
    monkeypatch.setattr(runs, "get_supabase", lambda: None)
    runs._ADVANCING.clear()
    runs._LAST_KICK.clear()
    assert _REAL_KICK("run-k", force=True) is True
    assert _REAL_KICK("run-k", force=True) is False  # one already in flight
    gate.set()
    for _ in range(100):
        if "run-k" not in runs._ADVANCING:
            break
        threading.Event().wait(0.01)
    assert "run-k" not in runs._ADVANCING
    assert _REAL_KICK("run-k") is False  # rate limited after a recent kick
    assert _REAL_KICK("run-k", force=True) is True
    for _ in range(100):
        if "run-k" not in runs._ADVANCING:
            break
        threading.Event().wait(0.01)
    assert calls == ["run-k", "run-k"]


def test_markup_sheet_with_an_unknown_document_is_skipped(env, renders):
    run, doc, main = _consolidating_run(env)
    spec = {"sheets": [
        main["output"]["markups"]["sheets"][0],
        {**main["output"]["markups"]["sheets"][0], "doc_index": 9, "document_id": None},
    ]}
    row = runs.render_deliverable(env.db, run, "trench_markup", spec, version=1)
    assert row["kind"] == "trench_markup"
    assert renders.markup_sources == ("trench_markup", [doc["id"]])
    with pytest.raises(ValueError):
        runs.render_deliverable(env.db, run, "trench_markup", {"sheets": spec["sheets"][1:]}, version=2)


def test_marked_plans_render_lazily_and_evict_each_pdf(env, monkeypatch):
    run, doc, main = _consolidating_run(env)
    sheets = [
        main["output"]["marked_plans"]["sheets"][0],
        {**main["output"]["marked_plans"]["sheets"][0], "page": 5},  # past the end: skipped
    ]
    seen = []
    given = []

    def marked(pages):
        given.append(pages)
        for page in pages:
            seen.append((page["png_bytes"][:4], len(pages.docs._cache)))
        return b"%PDF-marked"

    monkeypatch.setattr(render, "render_marked_plans_pdf", marked)
    runs.render_deliverable(env.db, run, "marked_plans", {"sheets": sheets}, version=1)
    # One PNG made while the renderer iterated (the PDF still cached for the
    # next sheet); the document PDF was dropped after its last sheet.
    assert seen == [(b"\x89PNG", 1)]
    assert given[0].rendered == 1 and given[0].skipped == 1 and given[0].docs._cache == {}


def test_cancel_also_cancels_queued_split_jobs(env, monkeypatch):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(2), label=None, user=user())
    runs.start(env.db, run, user=user())
    split_id = env.db.rows("plans_reader_files", run_id=run["id"])[0]["split_file_id"]
    monkeypatch.setattr(
        llm_queue, "active_job",
        lambda jt, tid: {"id": "split-job-1", "status": "queued"} if tid == split_id else None,
    )
    runs.cancel(env.db, env.db.rows("plans_reader_runs", id=run["id"])[0], user=user())
    assert env.canceled == ["split-job-1"]


def test_delete_waits_for_a_split_file_still_running(env, monkeypatch):
    monkeypatch.setattr(bid_split, "sweep_stale_files", lambda job_id, sb=None: [])
    run = new_run(env)
    job = env.db.add("bid_split_jobs", status="processing", source="plans_reader")
    env.db.add("bid_split_files", job_id=job["id"], status="running")
    run = set_status(env, run, "canceled", split_job_id=job["id"])
    with pytest.raises(runs.RunRefused) as exc:
        runs.delete(env.db, run, user=user())
    assert exc.value.status_code == 409
    env.db.table("bid_split_files").update({"status": "failed"}).eq("job_id", job["id"]).execute()
    assert runs.delete(env.db, run, user=user())["deleted"] is True


# ---------------------------------------------------------------------------
# Follow-ups (docs/PLANS_READER.md 8.4, migration 0153)
# ---------------------------------------------------------------------------


def test_split_file_settling_uses_the_settling_status(env):
    run = new_run(env)
    f = env.db.add("plans_reader_files", run_id=run["id"], filename="a.pdf", storage_path="x", status="splitting")
    assert runs._claim_settling(env.db, f["id"]) == "settling"
    assert env.db.rows("plans_reader_files", id=f["id"])[0]["status"] == "settling"
    assert runs._claim_settling(env.db, f["id"]) is None  # already claimed
    assert "settling" in runs.FILE_UNSETTLED and "converted" in runs.FILE_UNSETTLED


def test_settling_claim_falls_back_to_converted_without_migration_0153(env, monkeypatch):
    run = new_run(env)
    f = env.db.add("plans_reader_files", run_id=run["id"], filename="a.pdf", storage_path="x", status="splitting")
    real_execute = FakeQuery.execute

    def execute(self):
        if self.table == "plans_reader_files" and self.op == "update" and (self.payload or {}).get("status") == "settling":
            raise Exception('new row for relation "plans_reader_files" violates check constraint (code 23514)')
        return real_execute(self)

    monkeypatch.setattr(FakeQuery, "execute", execute)
    assert runs._claim_settling(env.db, f["id"]) == "converted"
    assert env.db.rows("plans_reader_files", id=f["id"])[0]["status"] == "converted"


def test_poll_reclaims_a_legacy_converted_claim_too(env, monkeypatch):
    monkeypatch.setattr(bid_split, "sweep_stale_files", lambda job_id, sb=None: [])
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(3), label=None, user=user())
    runs.start(env.db, run, user=user())
    frow = env.db.rows("plans_reader_files", run_id=run["id"])[0]
    _split_result(env, run, frow["split_file_id"])
    env.db.table("plans_reader_files").update({"status": runs.FILE_SETTLING_LEGACY}).eq("id", frow["id"]).execute()
    runs.advance(env.db, run["id"])
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "splitting"  # still unsettled
    _age(env, run)
    runs.get_run_payload(env.db, run["id"])
    assert env.db.rows("plans_reader_files", id=frow["id"])[0]["status"] == "split"
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"


def test_labeled_drawing_set_gets_a_derived_page_list_and_ledger_entries(env):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(3, 144, 216), label="electrical_drawings",
                    user=user())
    runs.start(env.db, run, user=user())
    (doc,) = env.db.rows("plans_reader_documents", run_id=run["id"])
    assert [e["pdf_page"] for e in doc["page_meta"]] == [1, 2, 3]
    entry = doc["page_meta"][0]
    assert entry["derived"] is True and entry["prepended"] is False and entry["page_role"] == "other"
    assert entry["title"] is None and (entry["w_in"], entry["h_in"]) == (2.0, 3.0)
    ledger = env.db.rows("plans_reader_runs", id=run["id"])[0]["ledger"]
    assert [(r["document_id"], r["page"], r["sheet_number"]) for r in ledger["sheet_index"]] == [
        (doc["id"], 1, None), (doc["id"], 2, None), (doc["id"], 3, None),
    ]
    assert ledger["documents"][0]["pages"] == 3
    (group,) = env.db.rows("plans_reader_groups", run_id=run["id"])
    # The sub agent reads the same page numbers the ledger lists.
    assert [i["page"] for i in group["pages"]] == [1, 2, 3]
    assert '"E.pdf" p2 | other' in agents.ledger_text(ledger)


def test_drawing_document_without_page_meta_is_derived_while_planning(env):
    run = set_status(env, new_run(env), "splitting")
    doc = seed_document(env, run, pages=2, page_meta=None)
    ledger, rows, problems = runs._plan_reading(env.db, run, [doc], env.settings)
    assert problems == [] and rows
    stored = env.db.rows("plans_reader_documents", id=doc["id"])[0]
    assert [e["pdf_page"] for e in stored["page_meta"]] == [1, 2] and stored["page_meta"][0]["derived"] is True
    assert [r["page"] for r in ledger["sheet_index"]] == [1, 2]
    # Splitter page detail is never replaced.
    meta = [{"page": 1, "pdf_page": 1, "prepended": False, "title": "E-101 POWER", "page_role": "plan",
             "confidence": 0.9}]
    split_doc = seed_document(env, run, pages=1, page_meta=meta)
    runs._plan_reading(env.db, run, [split_doc], env.settings)
    assert env.db.rows("plans_reader_documents", id=split_doc["id"])[0]["page_meta"] == meta


def test_payload_polls_modify_groups_under_their_own_job_type(env, monkeypatch):
    run = _reading_run(env)
    doc = seed_document(env, run)
    sub = _sub(env, run, doc, "electrical_drawings", 0, status="queued")
    main = env.db.add("plans_reader_groups", run_id=run["id"], kind="main", status="queued")
    modify = env.db.add("plans_reader_groups", run_id=run["id"], kind="modify", status="queued")
    polled = []
    monkeypatch.setattr(llm_queue, "poll_info", lambda jt, tid: polled.append((jt, tid)) or {"state": "queued"})
    runs.get_run_payload(env.db, run["id"])
    assert sorted(polled) == sorted([
        ("plans_reader_agent", sub["id"]), ("plans_reader_main", main["id"]), ("plans_reader_modify", modify["id"]),
    ])


def test_delete_refuses_when_a_start_won_meanwhile(env, monkeypatch):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(1), label="electrical_drawings", user=user())
    snapshot = copy.deepcopy(env.db.rows("plans_reader_runs", id=run["id"])[0])
    set_status(env, run, "splitting")  # start won between the delete's read and its claim
    monkeypatch.setattr(runs, "_load_run", lambda sb, r: dict(snapshot))
    objects = dict(env.store)
    with pytest.raises(runs.RunRefused) as exc:
        runs.delete(env.db, run, user=user())
    assert exc.value.status_code == 409 and runs.MSG_DELETE_RACED in str(exc.value)
    assert env.store == objects  # nothing swept
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "splitting"


def test_delete_claims_the_run_and_fences_the_row_delete(env):
    run = set_status(env, new_run(env), "done")
    assert runs.delete(env.db, run, user=user())["deleted"] is True
    claims = [s for s in env.db.statements if s[0] == "plans_reader_runs" and s[1] == "update"]
    assert ("in", "status", ["done"]) not in claims[-1][2] and ("eq", "status", "done") in claims[-1][2]
    (row_delete,) = [s for s in env.db.statements if s[0] == "plans_reader_runs" and s[1] == "delete"]
    assert ("eq", "status", "canceled") in row_delete[2]


def test_delete_puts_the_status_back_when_the_sweep_fails(env, monkeypatch):
    run = set_status(env, new_run(env), "done_with_errors", error="BOQ: broken.")

    def broken(_key):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "delete_plans_reader_prefix", broken)
    with pytest.raises(runs.RunRefused) as exc:
        runs.delete(env.db, run, user=user())
    assert exc.value.status_code == 503
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "done_with_errors" and fresh["error"] == "BOQ: broken."


def _cancel_marks_failed(env, monkeypatch):
    """llm_queue.cancel as the real one behaves: the canceled job's group is failed."""

    def cancel(job_id):
        env.canceled.append(job_id)
        env.db.table("plans_reader_groups").update({"status": "failed", "error": "Canceled."}).eq(
            "llm_job_id", job_id).in_("status", ["queued", "running"]).execute()
        return {"id": job_id}

    monkeypatch.setattr(llm_queue, "cancel", cancel)


def test_a_failed_sweep_queues_the_waiting_groups_of_an_over_cap_run_again(env, monkeypatch):
    run = set_status(env, new_run(env), "over_cap", error="Paused by the spend cap.")
    waiting = env.db.add("plans_reader_groups", run_id=run["id"], kind="sub", status="queued", llm_job_id="job-a")
    done = env.db.add("plans_reader_groups", run_id=run["id"], kind="sub", status="done", llm_job_id="job-b")
    modify = env.db.add("plans_reader_groups", run_id=run["id"], kind="modify", status="queued", llm_job_id="job-c")
    _cancel_marks_failed(env, monkeypatch)

    def broken(_key):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "delete_plans_reader_prefix", broken)
    with pytest.raises(runs.RunRefused) as exc:
        runs.delete(env.db, run, user=user())
    assert exc.value.status_code == 503
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "over_cap" and fresh["error"] == "Paused by the spend cap."
    g = env.db.rows("plans_reader_groups", id=waiting["id"])[0]
    assert g["status"] == "queued" and g["error"] is None and g["llm_job_id"] is None
    assert env.db.rows("plans_reader_groups", id=done["id"])[0]["status"] == "done"
    # A modify group has no resume to queue it again: it stays failed.
    assert env.db.rows("plans_reader_groups", id=modify["id"])[0]["status"] == "failed"
    # Resume then enqueues the restored group.
    runs.resume(env.db, fresh)
    assert [j["target_id"] for j in env.enqueued] == [waiting["id"]]


def test_delete_checks_for_busy_work_again_after_its_claim(env, monkeypatch):
    """A job picked up between the router's busy check and the claim: the
    claim is undone (with the groups) and the delete answers 409."""
    run = set_status(env, new_run(env), "over_cap")
    waiting = env.db.add("plans_reader_groups", run_id=run["id"], kind="sub", status="queued", llm_job_id="job-a")
    real_cas = runs._cas_status

    def cas_then_a_worker_cancels(sb, run_id, expected, new, fields=None):
        out = real_cas(sb, run_id, expected, new, fields)
        if new == "canceled" and out is not None:
            # run_group found the run claimed and canceled its group, and a
            # modify job had already marked its group running.
            env.db.table("plans_reader_groups").update({"status": "canceled"}).eq("id", waiting["id"]).execute()
            env.db.add("plans_reader_groups", run_id=run_id, kind="modify", status="running")
        return out

    monkeypatch.setattr(runs, "_cas_status", cas_then_a_worker_cancels)
    objects = dict(env.store)
    with pytest.raises(runs.RunRefused) as exc:
        runs.delete(env.db, run, user=user())
    assert exc.value.status_code == 409 and runs.MSG_DELETE_BUSY in str(exc.value)
    assert env.store == objects and env.canceled == []
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "over_cap"
    assert env.db.rows("plans_reader_groups", id=waiting["id"])[0]["status"] == "queued"


def test_delete_refuses_a_chat_answer_that_started_after_the_router_check(env):
    run = set_status(env, new_run(env), "done")
    thread = env.db.add("plans_reader_chat_threads", run_id=run["id"], title="t")
    env.db.add("plans_reader_chat_messages", thread_id=thread["id"], role="assistant", status="pending")
    with pytest.raises(runs.RunRefused) as exc:
        runs.delete(env.db, run, user=user())
    assert exc.value.status_code == 409
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "done"


def test_resume_refuses_a_run_being_deleted(env):
    run = set_status(env, new_run(env), "over_cap")
    set_status(env, run, "canceled", error=runs.MSG_DELETING)  # delete claimed it
    with pytest.raises(runs.RunRefused):
        runs.resume(env.db, run)


def test_a_crash_part_way_through_start_rolls_the_run_back(env):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(1), label="electrical_drawings", user=user())
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(2), label=None, user=user())
    env.db.fail_insert["bid_split_jobs"] = True  # after the labeled file became a document
    with pytest.raises(runs.RunRefused) as exc:
        runs.start(env.db, run, user=user())
    assert exc.value.status_code == 503 and runs.MSG_START_FAILED in str(exc.value)
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "created" and fresh["started_at"] is None and fresh["split_job_id"] is None
    assert fresh["error"] == runs.MSG_START_ROLLED_BACK
    assert not env.db.rows("plans_reader_documents", run_id=run["id"])
    files = env.db.rows("plans_reader_files", run_id=run["id"])
    assert {f["status"] for f in files} == {"pending"} and all(f["split_file_id"] is None for f in files)
    assert env.dispatched == [] and env.enqueued == []
    # Once the fault is gone the same run starts normally.
    env.db.fail_insert["bid_split_jobs"] = False
    assert runs.start(env.db, fresh, user=user())["status"] == "splitting"
    assert len(env.db.rows("plans_reader_documents", run_id=run["id"])) == 1


def test_a_crash_after_staging_cancels_the_queued_splits_and_drops_the_split_job(env, monkeypatch):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="a.pdf", content=make_pdf(2), label=None, user=user())
    runs.add_upload(env.db, run, filename="b.pdf", content=make_pdf(2), label=None, user=user())
    calls = []

    def dispatch(bg, fid, uid, forced_kind=None):
        calls.append(fid)
        if len(calls) == 2:
            raise RuntimeError("dispatch failed")

    monkeypatch.setattr(bid_split, "dispatch", dispatch)
    monkeypatch.setattr(llm_queue, "active_job", lambda jt, tid: {"id": f"split-{tid}", "status": "queued"})
    with pytest.raises(runs.RunRefused):
        runs.start(env.db, run, user=user())
    assert sorted(env.canceled) == sorted(f"split-{fid}" for fid in calls)
    assert not env.db.rows("bid_split_jobs") and not [k for k in env.store if k.startswith("bid-splits/")]
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "created"
    assert {f["status"] for f in env.db.rows("plans_reader_files", run_id=run["id"])} == {"pending"}


def test_a_crash_with_a_split_already_running_keeps_the_split_job(env, monkeypatch):
    """A split a worker already claimed cannot be stopped: its job row and
    objects stay, so what it writes still has an owner row."""
    run = new_run(env)
    runs.add_upload(env.db, run, filename="a.pdf", content=make_pdf(2), label=None, user=user())
    runs.add_upload(env.db, run, filename="b.pdf", content=make_pdf(2), label=None, user=user())
    calls = []

    def dispatch(bg, fid, uid, forced_kind=None):
        calls.append(fid)
        if len(calls) == 2:
            raise RuntimeError("dispatch failed")

    monkeypatch.setattr(bid_split, "dispatch", dispatch)
    monkeypatch.setattr(
        llm_queue, "active_job",
        lambda jt, tid: {"id": f"split-{tid}", "status": "running" if tid == calls[0] else "queued"},
    )
    with pytest.raises(runs.RunRefused):
        runs.start(env.db, run, user=user())
    assert env.canceled == [f"split-{calls[1]}"]
    assert len(env.db.rows("bid_split_jobs")) == 1
    assert [k for k in env.store if k.startswith("bid-splits/")]
    fresh = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert fresh["status"] == "created" and fresh["split_job_id"] is None
    assert {f["status"] for f in env.db.rows("plans_reader_files", run_id=run["id"])} == {"pending"}


# -- change slot -----------------------------------------------------------------


def test_an_inline_change_takes_the_slot_running_and_blocks_a_rerender(env, renders):
    run, _doc, _main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    done = env.db.rows("plans_reader_runs", id=run["id"])[0]
    msg = env.db.add("plans_reader_chat_messages", thread_id="t1", role="assistant", status="running")
    slot = runs.claim_change_slot(env.db, run["id"], output={"kind": "boq", "inline": True,
                                                             "message_id": msg["id"]}, status="running")
    assert slot["status"] == "running" and slot["started_at"] and slot["attempts"] == 1
    with pytest.raises(runs.RunRefused) as exc:
        runs.rerender_deliverable(env.db, done, "boq", user=user())
    assert exc.value.status_code == 409


def test_an_inline_change_whose_answer_died_is_released(env, renders):
    run, _doc, _main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    done = env.db.rows("plans_reader_runs", id=run["id"])[0]
    msg = env.db.add("plans_reader_chat_messages", thread_id="t1", role="assistant", status="running")
    slot = runs.claim_change_slot(env.db, run["id"], output={"kind": "boq", "inline": True,
                                                             "message_id": msg["id"]}, status="running")
    # Still answering: nothing is released.
    assert runs.release_orphan_changes(env.db, run["id"]) == 0
    # The worker died and the stale sweep failed the answer.
    env.db.table("plans_reader_chat_messages").update({"status": "failed"}).eq("id", msg["id"]).execute()
    payload = runs.get_run_payload(env.db, run["id"])  # the poll's self-heal releases it
    g = env.db.rows("plans_reader_groups", id=slot["id"])[0]
    assert g["status"] == "failed" and g["error"] == runs.MSG_INLINE_CHANGE_LOST
    row = next(r for r in payload["groups"] if r["id"] == slot["id"])
    assert row["change"] == "chat" and row["deliverable_kind"] == "boq"
    # The slot is free again.
    assert runs.rerender_deliverable(env.db, done, "boq", user=user())["version"] == 2


def test_the_payload_labels_each_change(env, renders):
    run, _doc, _main = _consolidating_run(env)
    runs.advance(env.db, run["id"])
    done = env.db.rows("plans_reader_runs", id=run["id"])[0]
    out = runs.rerender_deliverable(env.db, done, "estimate", user=user())
    env.db.add("plans_reader_groups", run_id=run["id"], kind="modify", status="done",
               output={"kind": "boq", "deliverable_id": "x", "version": 3})
    rows = {r["id"]: r for r in runs.get_run_payload(env.db, run["id"])["groups"] if r["kind"] == "modify"}
    assert rows[out["group"]["id"]]["change"] == "rerender"
    assert rows[out["group"]["id"]]["deliverable_kind"] == "estimate"
    assert rows[out["group"]["id"]]["deliverable_version"] is None
    other = next(r for gid, r in rows.items() if gid != out["group"]["id"])
    assert other["change"] == "modify" and other["deliverable_kind"] == "boq" and other["deliverable_version"] == 3
    assert all("change" not in r for r in runs.get_run_payload(env.db, run["id"])["groups"] if r["kind"] != "modify")


def test_a_late_file_failed_by_a_rolled_back_start_is_pending_again(env, monkeypatch):
    run = new_run(env)
    runs.add_upload(env.db, run, filename="set.pdf", content=make_pdf(2), label=None, user=user())
    real_estimate = runs.estimate

    def estimate_with_late_upload(sb, r):
        out = real_estimate(sb, r)
        env.db.add("plans_reader_files", run_id=run["id"], filename="late.pdf", storage_path="x/late.pdf",
                   label="electrical_drawings", status="pending")
        return out

    monkeypatch.setattr(runs, "estimate", estimate_with_late_upload)
    env.db.fail_insert["bid_split_jobs"] = True
    with pytest.raises(runs.RunRefused):
        runs.start(env.db, run, user=user())
    assert {f["status"] for f in env.db.rows("plans_reader_files", run_id=run["id"])} == {"pending"}


# -- pacing against the tokens-per-minute limit ---------------------------------


def _offsets(starts):
    """Seconds from now of each paced start (None = now)."""
    now = datetime.now(timezone.utc)
    return [0 if s is None else round((_when(s) - now).total_seconds() / 60) * 60 for s in starts]


def test_paced_starts_keep_each_minute_under_the_budget(env):
    s = make_settings(plans_reader_tokens_per_minute=100_000)
    groups = [{"est_input_tokens": 40_000} for _ in range(5)]
    assert _offsets(runs._paced_starts(env.db, groups, s)) == [0, 0, 60, 60, 120]
    # A group larger than the whole budget gets a minute to itself.
    big = [{"est_input_tokens": 150_000}, {"est_input_tokens": 10_000}, {"est_input_tokens": 10_000}]
    assert _offsets(runs._paced_starts(env.db, big, s)) == [0, 60, 60]
    # Pacing off.
    off = make_settings(plans_reader_tokens_per_minute=0)
    assert runs._paced_starts(env.db, groups, off) == [None] * 5


def test_pacing_counts_the_jobs_other_runs_already_scheduled(env):
    s = make_settings(plans_reader_tokens_per_minute=100_000)
    now = datetime.now(timezone.utc)
    env.db.add("llm_jobs", job_type="plans_reader_agent", status="running",
               next_attempt_at=(now - timedelta(seconds=20)).isoformat(), payload={"est_input_tokens": 70_000})
    env.db.add("llm_jobs", job_type="plans_reader_main", status="queued",
               next_attempt_at=(now + timedelta(seconds=75)).isoformat(), payload={"est_input_tokens": 70_000})
    # Ignored: too old, finished, another job type, no estimate.
    env.db.add("llm_jobs", job_type="plans_reader_agent", status="queued",
               next_attempt_at=(now - timedelta(seconds=600)).isoformat(), payload={"est_input_tokens": 99_000})
    env.db.add("llm_jobs", job_type="plans_reader_agent", status="succeeded",
               next_attempt_at=now.isoformat(), payload={"est_input_tokens": 99_000})
    env.db.add("llm_jobs", job_type="plans_reader_chat", status="queued",
               next_attempt_at=now.isoformat(), payload={"est_input_tokens": 99_000})
    env.db.add("llm_jobs", job_type="plans_reader_agent", status="queued", next_attempt_at=now.isoformat(),
               payload={"group_id": "old"})
    groups = [{"est_input_tokens": 40_000}, {"est_input_tokens": 40_000}]
    assert _offsets(runs._paced_starts(env.db, groups, s)) == [120, 120]


def test_advance_dispatches_paced_group_jobs(env):
    env.settings = make_settings(plans_reader_tokens_per_minute=10_000)
    run = new_run(env)
    runs.add_upload(env.db, run, filename="E.pdf", content=make_pdf(2), label="electrical_drawings", user=user())
    runs.add_upload(env.db, run, filename="S.pdf", content=make_pdf(2, 612, 792), label="specifications",
                    user=user())
    runs.start(env.db, run, user=user())
    assert len(env.enqueued) == 2
    assert env.enqueued[0]["next_attempt_at"] is None
    assert _offsets([env.enqueued[1]["next_attempt_at"]]) == [60]
    assert all(j["payload"]["est_input_tokens"] > 0 for j in env.enqueued)


# PDF copies ------------------------------------------------------------------


def test_pdf_copy_paths_and_names():
    assert runs.pdf_copy_path("plans-reader/r/deliverables/v1/x-Deep Brief.docx") == "plans-reader/r/deliverables/v1/x-Deep Brief.pdf"
    assert runs.pdf_copy_path("plans-reader/r/deliverables/v2/x-BOQ - COUNTS.XLSX") == "plans-reader/r/deliverables/v2/x-BOQ - COUNTS.pdf"
    assert runs.pdf_copy_path("plans-reader/r/deliverables/v1/odd") == "plans-reader/r/deliverables/v1/odd.pdf"
    assert runs.brief_pdf_path is runs.pdf_copy_path
    assert runs.pdf_copy_filename("brief", "Edikted", 1) == "Deep Brief.pdf"
    assert runs.pdf_copy_filename("brief", "Edikted", 3) == "Rev 3 - Deep Brief.pdf"
    assert runs.pdf_copy_filename("boq", "Edikted", 2) == "Rev 2 - BOQ - COUNTS.pdf"
    assert runs.pdf_copy_filename("estimate", "Five Star", 1) == "FIVE STAR - ELECTRICAL ESTIMATE.pdf"
    assert runs.PDF_COPY_KINDS == ("boq", "estimate", "brief")
