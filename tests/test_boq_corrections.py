"""BOQ correction drafts + dev training capture (migration 0084).

Covers the pieces that replaced the refine loop:

  run_extraction     snapshots the exact model input (system + user prompt) on
                     the analysis row before the LLM call, so even failed runs
                     keep what they were asked.
  draft PATCH        autosaves the reviewer's working draft on a done analysis
                     (last-write-wins, clearable, project-scoped, writer-only).
  confirm capture    boq_training.capture_example diffs the confirmed payload
                     against the pristine result_json — quantity/unit edits,
                     category moves, removals from mapped groups, added items,
                     held groups (neutral) and invented group names — and
                     upserts ONE example per analysis, resetting any review.
                     A capture failure must never fail the confirm itself.
  /training routes   dev-gated (require_dev) list / detail / review, plus the
                     fine-tuning surface: reconstruct_gold rebuilds the
                     confirmed truth in the model's own output schema (held
                     groups omitted — the reviewer judged them out; the input
                     keeps their source content so exclusion is learnable) and
                     /training/boq/export emits (system, user, assistant) JSONL.

Supabase is faked with the in-memory store used across the PM tests, extended
with `.range`/`count` (the training list pages) and an `upsert` keyed on its
on_conflict column, plus the projects/boq_analyses embeds the training routes
select.
"""

import asyncio
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app.core.deps import CurrentUser, require_dev, require_writer
from app.core.roles import Role
from app.models.schemas import (
    BoqConfirmIn,
    BoqDraftBody,
    BoqDraftIn,
    BoqGroupMapIn,
    BoqItemSrc,
    BoqOverrideIn,
    BoqTrainingReviewIn,
    RFQGroupIn,
    RFQLineItemIn,
)
from app.routers import boq_analysis as boq_router
from app.routers import training as training_router
from app.services import boq_extraction as bx
from app.services import boq_training, office_preview, rfq_excel, storage


# ── Fake Supabase ────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._on_conflict = None
        self._filters = []
        self._single = False
        self._sel = "*"
        self._order = None
        self._desc = False
        self._limit = None
        self._range = None

    def select(self, sel="*", *a, **k):
        self._op, self._sel = "select", sel
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, **k):
        self._op, self._payload, self._on_conflict = "upsert", payload, on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, col, desc=False, **k):
        self._order, self._desc = col, desc
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    def range(self, start, end, *a, **k):
        self._range = (start, end)
        return self

    def _matches(self, row):
        return all(
            row.get(c) in v if isinstance(v, list) else row.get(c) == v
            for c, v in self._filters
        )

    def _embed(self, row):
        """Resolve the nested selects the training routes use, PostgREST-style."""
        row = dict(row)
        t = self.db.tables
        if self.table == "boq_training_examples":
            if "projects(" in self._sel:
                row["projects"] = next(
                    (
                        {"name": p["name"], "number": p["number"]}
                        for p in t.get("projects", [])
                        if p["id"] == row.get("project_id")
                    ),
                    None,
                )
            if "boq_analyses(" in self._sel:
                match = next(
                    (a for a in t.get("boq_analyses", []) if a["id"] == row.get("analysis_id")),
                    None,
                )
                row["boq_analyses"] = (
                    {
                        k: match.get(k)
                        for k in ("input_snapshot", "result_json", "status", "created_at", "boq_file_id")
                    }
                    if match
                    else None
                )
        return row

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._order:
                hits = sorted(
                    hits,
                    key=lambda r: (r.get(self._order) is None, r.get(self._order)),
                    reverse=self._desc,
                )
            total = len(hits)
            if self._range is not None:
                hits = hits[self._range[0] : self._range[1] + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            hits = [self._embed(r) for r in hits]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None), count=total)
            return SimpleNamespace(data=hits, count=total)
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", uuid.uuid4().hex)
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "upsert":
            row = dict(self._payload)
            key = self._on_conflict
            existing = next((r for r in rows if key and r.get(key) == row.get(key)), None)
            if existing:
                existing.update(row)
                return SimpleNamespace(data=[dict(existing)])
            row.setdefault("id", uuid.uuid4().hex)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self, name)


def _user(role=Role.ESTIMATING_ADMIN, uid="u1", is_dev=False):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True, is_dev=is_dev)


# ── Fixtures ─────────────────────────────────────────────────────────────────

# One site, three groups: "Lighting" (real category name), "Gear Stuff" (an
# invented name the user maps to Switchgear) and "Held Grp" (left on Hold).
RESULT_JSON = {
    "sites": [
        {
            "site_name": "Site A",
            "material_groups": [
                {
                    "group_name": "Lighting",
                    "items": [
                        {"description": "2x4 Troffer", "quantity": 10, "unit": "EA", "notes": None},
                        {"description": "Downlight", "quantity": 5, "unit": "ea", "notes": None},
                        {"description": "Exit sign", "quantity": 3, "unit": "EA", "notes": None},
                        {"description": "Strut", "quantity": 7, "unit": "FT", "notes": None},
                    ],
                },
                {
                    "group_name": "Gear Stuff",
                    "items": [
                        {"description": "Panelboard", "quantity": 1, "unit": "EA", "notes": None},
                    ],
                },
                {
                    "group_name": "Held Grp",
                    "items": [
                        {"description": "Misc", "quantity": 2, "unit": "LOT", "notes": None},
                    ],
                },
            ],
        }
    ],
    "summary": "s",
    "total_material_count": 6,
}

ANALYSIS = {
    "id": "a1",
    "project_id": "p1",
    "boq_file_id": "bf1",
    "status": "done",
    "model": "claude-opus-4-8",
    "result_json": RESULT_JSON,
    "created_at": "2026-07-29T00:00:00Z",
}


def _base_tables(**over):
    t = {
        "projects": [{"id": "p1", "name": "Riverside Plaza", "number": "26-104"}],
        "material_categories": [
            {"id": "c1", "name": "Lighting"},
            {"id": "c2", "name": "Switchgear"},
        ],
        "boq_analyses": [dict(ANALYSIS)],
        "rfqs": [],
        "rfq_line_items": [],
        "project_files": [],
        "boq_training_examples": [],
    }
    t.update(over)
    return t


def _install_confirm(monkeypatch, db):
    """Point the confirm path (router + capture service) at the fake DB and
    make the RFQ side effects (workbook, storage, preview, audit) inert."""
    audits = []
    monkeypatch.setattr(boq_router, "get_supabase", lambda: db)
    monkeypatch.setattr(boq_training, "get_supabase", lambda: db)
    monkeypatch.setattr(boq_router, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(
        boq_router.workflow, "maybe_reopen_verify_after_edit", lambda *a, **k: None
    )
    monkeypatch.setattr(rfq_excel, "build_rfq_workbook", lambda name, items, project=None: b"xlsx")
    monkeypatch.setattr(storage, "build_object_path", lambda pid, cat, fn: f"{pid}/{cat}/{fn}")
    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(office_preview, "is_convertible", lambda *a: False)
    return audits


def _confirm_body():
    """Every change type at once: qty edit (Troffer), unit edit (Downlight),
    category move (Strut: Lighting → Switchgear), removal (Exit sign omitted
    from mapped Lighting), hand-added item (Custom strobe), held group
    (Held Grp) and an invented group name (Gear Stuff → Switchgear)."""
    return BoqConfirmIn(
        groups=[
            RFQGroupIn(
                material_category_id="c1",
                items=[
                    RFQLineItemIn(
                        site_name="Site A", description="2x4 Troffer",
                        quantity=Decimal("12"), unit="EA", src=BoqItemSrc(s=0, g=0, i=0),
                    ),
                    RFQLineItemIn(
                        site_name="Site A", description="Downlight",
                        quantity=Decimal("5"), unit="BOX", src=BoqItemSrc(s=0, g=0, i=1),
                    ),
                    RFQLineItemIn(
                        site_name="Site A", description="Custom strobe",
                        quantity=Decimal("2"), unit="EA",
                    ),
                ],
            ),
            RFQGroupIn(
                material_category_id="c2",
                items=[
                    RFQLineItemIn(
                        site_name="Site A", description="Panelboard",
                        quantity=Decimal("1"), unit="EA", src=BoqItemSrc(s=0, g=1, i=0),
                    ),
                    RFQLineItemIn(
                        site_name="Site A", description="Strut",
                        quantity=Decimal("7"), unit="FT", src=BoqItemSrc(s=0, g=0, i=3),
                    ),
                ],
            ),
        ],
        held_groups=["Held Grp"],
        group_mappings=[
            BoqGroupMapIn(group_name="Lighting", material_category_id="c1"),
            BoqGroupMapIn(group_name="Gear Stuff", material_category_id="c2"),
        ],
    )


# ── Schemas: draft shape + caps ──────────────────────────────────────────────


def test_draft_schema_accepts_the_documented_shape():
    body = BoqDraftIn(
        draft=BoqDraftBody(
            overrides=[
                BoqOverrideIn(
                    src=BoqItemSrc(s=0, g=1, i=2),
                    quantity=Decimal("5"), unit="EA", category_id=None, removed=False,
                )
            ],
            group_mappings={"Lighting": "c1", "Held Grp": ""},
        )
    )
    dumped = body.draft.model_dump(mode="json")
    assert dumped["overrides"][0]["src"] == {"s": 0, "g": 1, "i": 2}
    assert dumped["group_mappings"]["Held Grp"] == ""
    # null clears the draft.
    assert BoqDraftIn(draft=None).draft is None


def test_draft_schema_caps():
    over = {"src": {"s": 0, "g": 0, "i": 0}}
    with pytest.raises(ValidationError):
        BoqDraftBody(overrides=[over] * 5001)
    with pytest.raises(ValidationError):
        BoqDraftBody(group_mappings={f"g{i}": "c" for i in range(201)})
    with pytest.raises(ValidationError):
        BoqOverrideIn(src=BoqItemSrc(s=0, g=0, i=0), unit="x" * 81)
    with pytest.raises(ValidationError):
        BoqItemSrc(s=-1, g=0, i=0)


def test_confirm_schema_caps_and_backwards_compat():
    # Old payload shape (no src / held_groups / group_mappings) still validates.
    body = BoqConfirmIn(
        groups=[RFQGroupIn(material_category_id="c1", items=[RFQLineItemIn(description="x")])]
    )
    assert body.held_groups == [] and body.group_mappings == []
    assert body.groups[0].items[0].src is None
    with pytest.raises(ValidationError):
        BoqConfirmIn(groups=[RFQGroupIn(material_category_id="c1")], held_groups=["x" * 301])
    with pytest.raises(ValidationError):
        BoqGroupMapIn(group_name="x" * 301, material_category_id="c1")


# ── run_extraction: input snapshot ───────────────────────────────────────────


def _install_extraction(monkeypatch, db, result=None, error=None):
    monkeypatch.setattr(bx, "get_supabase", lambda: db)
    monkeypatch.setattr(bx, "_load_boq_text", lambda analysis: "DOC BODY 123")
    monkeypatch.setattr(bx, "_active_material_category_names", lambda: ["Lighting"])

    def _fake_call(system, messages):
        if error:
            raise error
        return result

    monkeypatch.setattr(bx, "_call_llm", _fake_call)


def test_run_extraction_persists_input_snapshot(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[{**ANALYSIS, "status": "pending", "result_json": None}]))
    _install_extraction(monkeypatch, db, result={"sites": [], "summary": "", "total_material_count": 0})
    bx.run_extraction("a1")
    row = db.tables["boq_analyses"][0]
    assert row["status"] == "done"
    snap = row["input_snapshot"]
    # The exact prompts, verbatim: system carries the category list + schema,
    # user carries the rendered document body.
    assert "Lighting" in snap["system"] and '"site_name"' in snap["system"]
    assert "<document>" in snap["user"] and "DOC BODY 123" in snap["user"]


def test_run_extraction_snapshots_input_even_when_the_llm_fails(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[{**ANALYSIS, "status": "pending", "result_json": None}]))
    _install_extraction(monkeypatch, db, error=RuntimeError("model exploded"))
    bx.run_extraction("a1")
    row = db.tables["boq_analyses"][0]
    assert row["status"] == "failed"
    assert "DOC BODY 123" in row["input_snapshot"]["user"]


# ── Draft PATCH ──────────────────────────────────────────────────────────────


def _draft_in():
    return BoqDraftIn(
        draft=BoqDraftBody(
            overrides=[BoqOverrideIn(src=BoqItemSrc(s=0, g=0, i=0), quantity=Decimal("12"), removed=False)],
            group_mappings={"Lighting": "c1"},
        )
    )


def test_draft_patch_saves_and_returns_the_draft(monkeypatch):
    db = FakeDB(_base_tables())
    monkeypatch.setattr(boq_router, "get_supabase", lambda: db)
    out = boq_router.save_draft("p1", "a1", _draft_in(), user=_user())
    assert out["draft_json"]["group_mappings"] == {"Lighting": "c1"}
    assert out["draft_updated_at"]
    row = db.tables["boq_analyses"][0]
    assert row["draft_json"]["overrides"][0]["quantity"] == "12"
    assert row["draft_updated_by"] == "u1" and row["draft_updated_at"]


def test_draft_patch_null_clears(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[{**ANALYSIS, "draft_json": {"overrides": []}}]))
    monkeypatch.setattr(boq_router, "get_supabase", lambda: db)
    out = boq_router.save_draft("p1", "a1", BoqDraftIn(draft=None), user=_user())
    assert out["draft_json"] is None
    assert db.tables["boq_analyses"][0]["draft_json"] is None


def test_draft_patch_409_unless_done(monkeypatch):
    db = FakeDB(_base_tables(boq_analyses=[{**ANALYSIS, "status": "running"}]))
    monkeypatch.setattr(boq_router, "get_supabase", lambda: db)
    with pytest.raises(HTTPException) as exc:
        boq_router.save_draft("p1", "a1", _draft_in(), user=_user())
    assert exc.value.status_code == 409


def test_draft_patch_404_for_another_projects_analysis(monkeypatch):
    db = FakeDB(_base_tables())
    monkeypatch.setattr(boq_router, "get_supabase", lambda: db)
    with pytest.raises(HTTPException) as exc:
        boq_router.save_draft("p-other", "a1", _draft_in(), user=_user())
    assert exc.value.status_code == 404


def test_draft_route_requires_writer():
    route = next(
        r
        for r in boq_router.router.routes
        if getattr(r, "path", "").endswith("/draft") and "PATCH" in getattr(r, "methods", set())
    )
    # Param-level Depends(require_writer) lands in the route's resolved dependant.
    assert any(d.call is require_writer for d in route.dependant.dependencies)


# ── Confirm: training capture ────────────────────────────────────────────────


def test_confirm_captures_a_full_diff(monkeypatch):
    db = FakeDB(_base_tables())
    _install_confirm(monkeypatch, db)
    out = boq_router.confirm_analysis("p1", "a1", _confirm_body(), BackgroundTasks(), user=_user())
    assert len(out["created"]) == 2

    examples = db.tables["boq_training_examples"]
    assert len(examples) == 1
    ex = examples[0]
    assert ex["analysis_id"] == "a1" and ex["project_id"] == "p1"
    assert ex["boq_file_id"] == "bf1" and ex["model"] == "claude-opus-4-8"
    assert ex["modified"] is True
    assert ex["confirmed_by"] == "u1" and ex["confirmed_at"]
    assert ex["reviewed_by"] is None and ex["reviewed_at"] is None

    counts = ex["diff_json"]["counts"]
    assert counts == {
        "quantity": 1, "unit": 1, "category": 1, "removed": 1, "added": 1,
        "group_renames": 1, "items_total": 6, "items_confirmed": 5,
    }

    items = {(d["description"], tuple(d["changes"])): d for d in ex["diff_json"]["items"]}
    qty = items[("2x4 Troffer", ("quantity",))]
    assert qty["model"]["quantity"] == 10 and qty["user"]["quantity"] == 12
    assert qty["from_group"] == "Lighting" and qty["src"] == {"s": 0, "g": 0, "i": 0}
    unit = items[("Downlight", ("unit",))]
    assert unit["model"]["unit"] == "ea" and unit["user"]["unit"] == "BOX"
    move = items[("Strut", ("category",))]
    assert move["model"]["category_id"] == "c1" and move["model"]["category_name"] == "Lighting"
    assert move["user"]["category_id"] == "c2" and move["user"]["category_name"] == "Switchgear"
    removed = items[("Exit sign", ("removed",))]
    assert removed["user"] is None and removed["src"] == {"s": 0, "g": 0, "i": 2}
    added = items[("Custom strobe", ("added",))]
    assert added["model"] is None and added["src"] is None
    # Panelboard went in untouched, Misc is held — neither may appear.
    assert not any(d["description"] in ("Panelboard", "Misc") for d in ex["diff_json"]["items"])

    mappings = {m["group_name"]: m for m in ex["diff_json"]["group_mappings"]}
    assert mappings["Lighting"]["renamed"] is False
    assert mappings["Gear Stuff"] == {
        "group_name": "Gear Stuff", "category_id": "c2",
        "category_name": "Switchgear", "renamed": True,
    }
    assert ex["held_groups"] == [{"group_name": "Held Grp", "item_count": 1}]

    # user_output is the normalized confirmed payload, src riding along.
    groups = ex["user_output"]["groups"]
    assert [g["category_name"] for g in groups] == ["Lighting", "Switchgear"]
    assert groups[0]["items"][0]["quantity"] == 12
    assert groups[1]["items"][1]["src"] == {"s": 0, "g": 0, "i": 3}


def test_confirm_line_items_never_carry_src(monkeypatch):
    # `src` is diff bookkeeping — inserting it would 42703 on rfq_line_items.
    db = FakeDB(_base_tables())
    _install_confirm(monkeypatch, db)
    boq_router.confirm_analysis("p1", "a1", _confirm_body(), BackgroundTasks(), user=_user())
    line_items = db.tables["rfq_line_items"]
    assert line_items and all("src" not in li for li in line_items)


def test_unmodified_confirm_is_clean(monkeypatch):
    result = {
        "sites": [
            {
                "site_name": "Site A",
                "material_groups": [
                    {"group_name": "Lighting", "items": [
                        {"description": "2x4 Troffer", "quantity": 10, "unit": "EA", "notes": None},
                    ]},
                ],
            }
        ],
        "summary": "s",
        "total_material_count": 1,
    }
    db = FakeDB(_base_tables(boq_analyses=[{**ANALYSIS, "result_json": result}]))
    _install_confirm(monkeypatch, db)
    body = BoqConfirmIn(
        groups=[
            RFQGroupIn(
                material_category_id="c1",
                items=[
                    RFQLineItemIn(
                        site_name="Site A", description="2x4 Troffer",
                        quantity=Decimal("10"), unit="EA", src=BoqItemSrc(s=0, g=0, i=0),
                    )
                ],
            )
        ],
        group_mappings=[BoqGroupMapIn(group_name="Lighting", material_category_id="c1")],
    )
    boq_router.confirm_analysis("p1", "a1", body, BackgroundTasks(), user=_user())
    ex = db.tables["boq_training_examples"][0]
    assert ex["modified"] is False
    assert ex["diff_json"]["items"] == []
    assert ex["diff_json"]["counts"]["items_confirmed"] == 1


def test_reconfirm_upserts_and_resets_the_review(monkeypatch):
    db = FakeDB(_base_tables())
    _install_confirm(monkeypatch, db)
    boq_router.confirm_analysis("p1", "a1", _confirm_body(), BackgroundTasks(), user=_user())
    # A dev signs the first capture off…
    db.tables["boq_training_examples"][0].update(
        {"reviewed_by": "dev1", "reviewed_at": "2026-07-30T00:00:00Z", "review_note": "ok"}
    )
    # …then a re-confirm replaces it: still one row, review reset.
    boq_router.confirm_analysis(
        "p1", "a1", _confirm_body(), BackgroundTasks(), user=_user(uid="u2")
    )
    examples = db.tables["boq_training_examples"]
    assert len(examples) == 1
    ex = examples[0]
    assert ex["confirmed_by"] == "u2"
    assert ex["reviewed_by"] is None and ex["reviewed_at"] is None and ex["review_note"] is None


def test_capture_failure_never_fails_the_confirm(monkeypatch):
    db = FakeDB(_base_tables())
    _install_confirm(monkeypatch, db)

    def _boom(*a, **k):
        raise RuntimeError("capture bug")

    monkeypatch.setattr(boq_router.boq_training, "capture_example", _boom)
    out = boq_router.confirm_analysis("p1", "a1", _confirm_body(), BackgroundTasks(), user=_user())
    assert len(out["created"]) == 2  # RFQs still created, no exception escaped
    assert db.tables["boq_training_examples"] == []


# ── Training routes (dev gate + list/detail/review) ──────────────────────────


def _example(eid="ex1", aid="a1", confirmed_at="2026-07-30T01:00:00Z", **over):
    row = {
        "id": eid,
        "analysis_id": aid,
        "project_id": "p1",
        "boq_file_id": "bf1",
        "model": "claude-opus-4-8",
        "user_output": {"groups": []},
        "diff_json": {"counts": {"quantity": 1}, "items": [{"x": 1}], "group_mappings": []},
        "modified": True,
        "held_groups": [],
        "confirmed_by": "u1",
        "confirmed_at": confirmed_at,
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": None,
    }
    row.update(over)
    return row


def test_require_dev_rejects_non_dev_users():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_dev(_user()))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Dev account required"
    dev = _user(is_dev=True)
    assert asyncio.run(require_dev(dev)) is dev
    # A dev account passes regardless of role — even the read-only accountant.
    acct = _user(role=Role.ACCOUNTANT, is_dev=True)
    assert asyncio.run(require_dev(acct)) is acct


def test_every_training_route_is_dev_gated():
    for route in training_router.router.routes:
        assert any(
            d.call is require_dev for d in route.dependant.dependencies
        ), f"{route.path} missing require_dev"


def test_training_list_is_light_and_newest_first(monkeypatch):
    db = FakeDB(
        _base_tables(
            boq_training_examples=[
                _example("ex1", confirmed_at="2026-07-29T00:00:00Z"),
                _example("ex2", confirmed_at="2026-07-30T00:00:00Z", modified=False),
            ]
        )
    )
    monkeypatch.setattr(training_router, "get_supabase", lambda: db)
    out = training_router.list_boq_examples(limit=50, offset=0, user=_user(is_dev=True))
    assert out["total"] == 2 and [r["id"] for r in out["rows"]] == ["ex2", "ex1"]
    row = out["rows"][1]
    assert row["projects"] == {"name": "Riverside Plaza", "number": "26-104"}
    assert row["counts"] == {"quantity": 1}
    assert "diff_json" not in row  # items stay off the list payload


def test_training_detail_joins_the_analysis(monkeypatch):
    analysis = {**ANALYSIS, "input_snapshot": {"system": "SYS", "user": "USR"}}
    db = FakeDB(
        _base_tables(boq_analyses=[analysis], boq_training_examples=[_example("ex1")])
    )
    monkeypatch.setattr(training_router, "get_supabase", lambda: db)
    out = training_router.boq_example_detail("ex1", user=_user(is_dev=True))
    assert out["diff_json"]["items"] == [{"x": 1}]
    joined = out["boq_analyses"]
    assert joined["input_snapshot"] == {"system": "SYS", "user": "USR"}
    assert joined["result_json"] == RESULT_JSON and joined["status"] == "done"
    with pytest.raises(HTTPException) as exc:
        training_router.boq_example_detail("nope", user=_user(is_dev=True))
    assert exc.value.status_code == 404


def test_training_review_sets_and_clears(monkeypatch):
    db = FakeDB(_base_tables(boq_training_examples=[_example("ex1")]))
    monkeypatch.setattr(training_router, "get_supabase", lambda: db)
    dev = _user(uid="dev1", is_dev=True)

    training_router.review_boq_example(
        "ex1", BoqTrainingReviewIn(reviewed=True, note="looks right"), user=dev
    )
    row = db.tables["boq_training_examples"][0]
    assert row["reviewed_by"] == "dev1" and row["reviewed_at"]
    assert row["review_note"] == "looks right"

    training_router.review_boq_example("ex1", BoqTrainingReviewIn(reviewed=False), user=dev)
    row = db.tables["boq_training_examples"][0]
    assert row["reviewed_by"] is None and row["reviewed_at"] is None and row["review_note"] is None

    with pytest.raises(HTTPException) as exc:
        training_router.review_boq_example(
            "nope", BoqTrainingReviewIn(reviewed=True), user=dev
        )
    assert exc.value.status_code == 404
    with pytest.raises(ValidationError):
        BoqTrainingReviewIn(reviewed=True, note="x" * 2001)


# ── Gold reconstruction + fine-tuning export ─────────────────────────────────


def _confirmed_db(monkeypatch, snapshot=True):
    """A DB holding one captured example, its analysis carrying (or missing)
    the frozen input snapshot, with the training routes pointed at it."""
    analysis = dict(ANALYSIS)
    if snapshot:
        analysis["input_snapshot"] = {
            "system": "SYS listing Lighting and Switchgear",
            "user": "DOC",
        }
    db = FakeDB(_base_tables(boq_analyses=[analysis]))
    _install_confirm(monkeypatch, db)
    boq_router.confirm_analysis("p1", "a1", _confirm_body(), BackgroundTasks(), user=_user())
    monkeypatch.setattr(training_router, "get_supabase", lambda: db)
    return db


def test_reconstruct_gold_is_the_corrected_output_in_the_model_schema(monkeypatch):
    db = _confirmed_db(monkeypatch)
    user_output = db.tables["boq_training_examples"][0]["user_output"]
    gold, flags = boq_training.reconstruct_gold(RESULT_JSON, user_output)
    assert flags == []
    assert [s["site_name"] for s in gold["sites"]] == ["Site A"]
    groups = gold["sites"][0]["material_groups"]
    # Canonical category names; the held group is gone entirely.
    assert [g["group_name"] for g in groups] == ["Lighting", "Switchgear"]
    lighting = {i["description"]: i for i in groups[0]["items"]}
    assert lighting["2x4 Troffer"]["quantity"] == 12  # qty edit carried
    assert lighting["Downlight"]["unit"] == "BOX"  # unit edit carried
    assert "Exit sign" not in lighting  # removal honored
    assert "Custom strobe" in lighting  # hand-added item present
    assert "Misc" not in lighting  # held-group item omitted
    # The move landed under Switchgear, after its untouched sibling.
    assert [i["description"] for i in groups[1]["items"]] == ["Panelboard", "Strut"]
    # Items carry exactly the model's item fields — no src/sr_no bookkeeping.
    assert set(groups[0]["items"][0]) == {"description", "quantity", "unit", "notes"}
    assert gold["summary"] == "s"  # model prose rides through verbatim
    assert gold["total_material_count"] == 5  # recomputed, not the model's 6


def test_reconstruct_gold_omits_a_fully_held_site_and_appends_new_sites():
    result = {
        "sites": [
            {"site_name": "Site A", "material_groups": [
                {"group_name": "Junk", "items": [
                    {"description": "X", "quantity": 1, "unit": "EA", "notes": None},
                ]},
            ]},
            {"site_name": "Site B", "material_groups": [
                {"group_name": "Lighting", "items": [
                    {"description": "Troffer", "quantity": 2, "unit": "EA", "notes": None},
                ]},
            ]},
        ],
        "summary": "sum",
        "total_material_count": 2,
    }
    user_output = {"groups": [
        {"material_category_id": "c1", "category_name": "Lighting", "items": [
            {"site_name": "Site B", "description": "Troffer", "quantity": 2,
             "unit": "EA", "notes": None, "src": {"s": 1, "g": 0, "i": 0}},
            {"site_name": "Site C", "description": "Hand-added", "quantity": 1,
             "unit": "EA", "notes": None, "src": None},
        ]},
    ]}
    gold, flags = boq_training.reconstruct_gold(result, user_output)
    # Site A (everything held) disappears; the hand-added Site C is appended.
    assert [s["site_name"] for s in gold["sites"]] == ["Site B", "Site C"]
    assert flags == ["empty_site_omitted", "new_site"]
    assert gold["total_material_count"] == 2


def test_gold_prompt_flags():
    gold = {"sites": [{"site_name": "A", "material_groups": [
        {"group_name": "Lighting", "items": []},
    ]}]}
    assert boq_training.gold_prompt_flags(gold, "Categories:\n- Lighting\n") == []
    assert boq_training.gold_prompt_flags(gold, "no such category here") == [
        "category_not_in_prompt"
    ]
    assert boq_training.gold_prompt_flags(gold, None) == ["no_input_snapshot"]


def test_export_emits_finetune_ready_jsonl(monkeypatch):
    _confirmed_db(monkeypatch)
    resp = training_router.export_boq_examples(
        reviewed_only=False, modified_only=False, user=_user(is_dev=True)
    )
    assert resp.headers["X-Example-Count"] == "1"
    assert resp.headers["X-Skipped-Count"] == "0"
    assert resp.media_type == "application/x-ndjson"
    line = json.loads(resp.body.decode().strip())
    # Input verbatim from the frozen snapshot; assistant parses back to gold.
    assert line["system"].startswith("SYS") and line["user"] == "DOC"
    gold = json.loads(line["assistant"])
    names = [g["group_name"] for s in gold["sites"] for g in s["material_groups"]]
    assert names == ["Lighting", "Switchgear"]
    meta = line["meta"]
    assert meta["analysis_id"] == "a1" and meta["project_id"] == "p1"
    assert meta["modified"] is True and meta["reviewed"] is False
    assert meta["flags"] == []


def test_export_skips_examples_without_a_snapshot(monkeypatch):
    _confirmed_db(monkeypatch, snapshot=False)
    resp = training_router.export_boq_examples(
        reviewed_only=False, modified_only=False, user=_user(is_dev=True)
    )
    assert resp.headers["X-Example-Count"] == "0"
    assert resp.headers["X-Skipped-Count"] == "1"
    assert resp.body.decode() == ""


def test_export_flags_categories_missing_from_the_frozen_prompt(monkeypatch):
    db = _confirmed_db(monkeypatch)
    db.tables["boq_analyses"][0]["input_snapshot"] = {
        "system": "Categories: Lighting only", "user": "DOC",
    }
    resp = training_router.export_boq_examples(
        reviewed_only=False, modified_only=False, user=_user(is_dev=True)
    )
    line = json.loads(resp.body.decode().strip())
    assert line["meta"]["flags"] == ["category_not_in_prompt"]


def test_export_filters_reviewed_and_modified(monkeypatch):
    db = _confirmed_db(monkeypatch)
    resp = training_router.export_boq_examples(
        reviewed_only=True, modified_only=False, user=_user(is_dev=True)
    )
    assert resp.headers["X-Example-Count"] == "0"
    db.tables["boq_training_examples"][0]["reviewed_by"] = "dev1"
    resp = training_router.export_boq_examples(
        reviewed_only=True, modified_only=True, user=_user(is_dev=True)
    )
    assert resp.headers["X-Example-Count"] == "1"


def test_gold_endpoint_serves_reconstruction_and_flags(monkeypatch):
    db = _confirmed_db(monkeypatch)
    eid = db.tables["boq_training_examples"][0]["id"]
    out = training_router.boq_example_gold(eid, user=_user(is_dev=True))
    assert out["flags"] == []
    assert out["gold"]["total_material_count"] == 5
    with pytest.raises(HTTPException) as exc:
        training_router.boq_example_gold("nope", user=_user(is_dev=True))
    assert exc.value.status_code == 404


def test_export_route_precedes_the_dynamic_detail_route():
    # FastAPI matches in declaration order — if /boq/{example_id} ever moves
    # above /boq/export, "export" gets swallowed as an example id.
    paths = [r.path for r in training_router.router.routes]
    assert paths.index("/training/boq/export") < paths.index("/training/boq/{example_id}")
