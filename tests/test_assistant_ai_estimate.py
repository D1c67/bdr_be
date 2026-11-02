"""assistant_ai_estimate: page-read and run cost previews, AE state reads
(docs/ASSISTANT_PHASE4_PLAN.md 3.5.1, 3.5.4, 3.6.1, 3.6.4; WI-2). No database,
no storage, no model: fakes only."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.services import ai_spend, llm_pricing, split_handoff, storage
from app.services import assistant_ai_estimate as est
from app.services import plans_reader_chat as chat
from app.services import plans_reader_images as images
from app.services import plans_reader_pack as pack
from app.services import plans_reader_runs as runs
from tests.test_plans_reader_notes import FakeDB
from tests.test_plans_reader_runs import install_env, make_pdf, make_settings, new_run, user

MODEL = "gpt-6-astra"
REAL_ASSISTANT_BLOCK = est._assistant_block
RUN_ID = "44444444-4444-4444-8444-444444444444"
DOC_ID = "55555555-5555-4555-8555-555555555555"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    est.clear_caches()
    monkeypatch.setattr(est, "_chat_model", lambda s, run=None: MODEL)
    monkeypatch.setattr(est, "_assistant_block",
                        lambda uid: {"user_remaining_usd": "5.00", "global_remaining_usd": "50.00"})
    monkeypatch.setattr(ai_spend, "status",
                        lambda sb, now=None: {"cap_usd": 75.0, "remaining_usd": 61.239, "blocked": False,
                                              "cap_hit": False})
    yield
    est.clear_caches()


def settings(**over):
    return make_settings(**over)


def page_db(w_in: float, h_in: float, pages: int = 20, *, status: str = "done") -> tuple[FakeDB, dict, dict]:
    db = FakeDB()
    run = db.add("plans_reader_runs", id=RUN_ID, name="7212 run", status=status, model=MODEL)
    meta = [{"page": i, "pdf_page": i, "title": f"E-{200 + i} POWER PLAN", "page_role": "plan"}
            for i in range(1, pages + 1)]
    doc = db.add("plans_reader_documents", id=DOC_ID, run_id=RUN_ID, name="Electrical set", page_count=pages,
                 page_meta=meta, storage_path="plans-reader/doc.pdf", sheet_width_in=w_in, sheet_height_in=h_in)
    return db, run, doc


def serve_pdf(monkeypatch, pdf: bytes):
    monkeypatch.setattr(storage, "download_file", lambda path: pdf)


def formula(tokens: list[int], context: int = 24_000, rounds: int = 8) -> Decimal:
    price = llm_pricing.price_for(MODEL)
    base = sum(tokens) + context
    return (Decimal(base) * price.input + Decimal(base * (rounds - 1)) * price.cached_input
            + Decimal(8_000) * price.output) / Decimal(1_000_000)


# -- 3.5.1 Page read estimate --------------------------------------------------


@pytest.mark.parametrize("w_in,h_in,ppi", [(36, 24, 188), (42, 30, 155), (48, 36, 133)])
def test_page_read_estimate_matches_the_reader_figures_and_the_formula(monkeypatch, w_in, h_in, ppi):
    db, run, _doc = page_db(w_in, h_in)
    serve_pdf(monkeypatch, make_pdf(20, w_in * 72, h_in * 72))
    out = est.page_read_estimate(db, run, DOC_ID, [14], "u-1", settings=settings())
    assert out["problems"] == []
    page = out["pages"][0]
    assert page["ppi"] == ppi and page["w_in"] == w_in and page["h_in"] == h_in
    assert page["sheet_number"] == "E-214" and page["title"] == "E-214 POWER PLAN"
    reader = pack.plan_page(14, w_in, h_in, render_mode="adaptive", tile_ppi=260, max_patches=30_000)
    assert abs(page["tokens"] - reader.est_tokens) <= reader.est_tokens * 0.01
    expected = formula([page["tokens"]])
    assert abs(Decimal(out["estimate_usd"]) - expected) <= expected * Decimal("0.01")
    assert out["rounds"] == chat.MAX_ROUNDS == 8 and out["output_tokens"] == 8_000
    assert out["base_tokens"] == page["tokens"] + 24_000 and out["input_tokens"] == out["base_tokens"] * 8
    assert out["sizes_approx"] is False and out["model"] == MODEL


def test_cached_rate_applies_to_max_rounds_minus_one_rebills(monkeypatch):
    db, run, _doc = page_db(36, 24)
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    out = est.page_read_estimate(db, run, DOC_ID, [1], "u-1", settings=settings())
    t = out["pages"][0]["tokens"]
    price = llm_pricing.price_for(MODEL)
    full_every_round = (Decimal((t + 24_000) * 8) * price.input + Decimal(8_000) * price.output) / Decimal(1_000_000)
    assert Decimal(out["estimate_usd"]) < full_every_round / 2
    assert out["estimate_usd"] == "1.42" and out["max_usd"] == "2.13"


@pytest.mark.parametrize("pages,estimate,hold", [([1, 2, 3], "2.64", "3.96"), ([1, 2, 3, 4, 5, 6], "4.47", "6.71")])
def test_reference_figures_for_three_and_six_large_sheets(monkeypatch, pages, estimate, hold):
    db, run, _doc = page_db(36, 24)
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    out = est.page_read_estimate(db, run, DOC_ID, pages, "u-1", settings=settings())
    assert out["problems"] == []
    # The hold rounds up to the cent (6.70011 -> 6.71), so it always covers the estimate x 1.5.
    assert out["estimate_usd"] == estimate and out["max_usd"] == hold


def test_money_is_strings_and_the_cap_blocks_ride_along(monkeypatch):
    db, run, _doc = page_db(36, 24)
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    out = est.page_read_estimate(db, run, DOC_ID, [2], "u-1", settings=settings())
    for key in ("estimate_usd", "max_usd"):
        assert isinstance(out[key], str) and len(out[key].split(".")[1]) == 2
    assert out["ai_estimating"] == {"cap_usd": "75.00", "remaining_usd": "61.23", "blocked": False}
    assert out["assistant"] == {"user_remaining_usd": "5.00", "global_remaining_usd": "50.00"}
    assert out["run"] == {"id": RUN_ID, "name": "7212 run"} and out["document"]["name"] == "Electrical set"


def test_seven_pages_are_refused_and_the_chat_cap_also_binds(monkeypatch):
    db, run, _doc = page_db(36, 24)
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    out = est.page_read_estimate(db, run, DOC_ID, [1, 2, 3, 4, 5, 6, 7], "u-1", settings=settings())
    assert "too_many_pages" in {p["code"] for p in out["problems"]}
    tight = settings(plans_reader_promax_max_pages_per_message=3)
    out = est.page_read_estimate(db, run, DOC_ID, [1, 2, 3, 4], "u-1", settings=tight)
    assert "too_many_pages" in {p["code"] for p in out["problems"]} and out["max_pages"] == 3


def test_long_context_is_refused(monkeypatch):
    db, run, _doc = page_db(36, 24)
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    out = est.page_read_estimate(db, run, DOC_ID, [1, 2, 3, 4, 5, 6], "u-1",
                                 settings=settings(assistant_plans_context_tokens=60_000))
    assert out["base_tokens"] > 272_000
    assert [p["code"] for p in out["problems"]] == ["long_context"]


@pytest.mark.parametrize("pages", [[], [0], [21], [3, 3], ["x"]])
def test_bad_pages_are_a_problem(monkeypatch, pages):
    db, run, _doc = page_db(36, 24)
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    out = est.page_read_estimate(db, run, DOC_ID, pages, "u-1", settings=settings())
    assert "pages_invalid" in {p["code"] for p in out["problems"]}


def test_unfinished_run_and_foreign_document_are_problems(monkeypatch):
    db, run, _doc = page_db(36, 24, status="reading")
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    out = est.page_read_estimate(db, run, DOC_ID, [1], "u-1", settings=settings())
    assert "run_not_finished" in {p["code"] for p in out["problems"]}
    other = {"id": str(uuid.uuid4()), "name": "other", "status": "done"}
    out = est.page_read_estimate(db, other, DOC_ID, [1], "u-1", settings=settings())
    assert {p["code"] for p in out["problems"]} == {"document_not_in_run"} and out["pages"] == []
    out = est.page_read_estimate(db, run | {"status": "done"}, "not-a-uuid", [1], "u-1", settings=settings())
    assert "document_not_in_run" in {p["code"] for p in out["problems"]}


def test_sizes_fall_back_to_the_stored_sheet_size(monkeypatch):
    db, run, _doc = page_db(42, 30)

    def boom(path):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "download_file", boom)
    out = est.page_read_estimate(db, run, DOC_ID, [1], "u-1", settings=settings())
    assert out["sizes_approx"] is True and out["problems"] == []
    assert out["pages"][0]["ppi"] == 155


def test_an_unpriced_model_is_a_problem(monkeypatch):
    db, run, _doc = page_db(36, 24)
    serve_pdf(monkeypatch, make_pdf(20, 36 * 72, 24 * 72))
    monkeypatch.setattr(est, "_chat_model", lambda s, run=None: "self-hosted:gemma")
    out = est.page_read_estimate(db, run, DOC_ID, [1], "u-1", settings=settings())
    assert [p["code"] for p in out["problems"]] == ["unpriced_model"] and out["estimate_usd"] == "0.00"


def test_spend_reads_fail_closed(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ai_spend, "status", boom)
    assert est._ai_estimating_block(None) == {"cap_usd": "0.00", "remaining_usd": "0.00", "blocked": True}
    from app.assistant import spend

    monkeypatch.setattr(spend, "state", boom)
    assert REAL_ASSISTANT_BLOCK("u-1") == {"user_remaining_usd": "0.00", "global_remaining_usd": "0.00"}


def test_assistant_block_floors_the_remainders(monkeypatch):
    from app.assistant import spend

    state = spend.SpendState(user_cap=Decimal("5"), user_spent=Decimal("1.234"), user_held=Decimal("0"),
                             global_cap=Decimal("50"), global_spent=Decimal("0.001"), global_held=Decimal("0"))
    monkeypatch.setattr(spend, "state", lambda uid, settings=None: state)
    assert REAL_ASSISTANT_BLOCK("u-1") == {"user_remaining_usd": "3.76", "global_remaining_usd": "49.99"}


# -- 3.6.1 Run preview ------------------------------------------------------------


def preview_db(files: list[tuple[str, str, int, float, float]]) -> tuple[FakeDB, str, list[str], dict[str, bytes]]:
    """files: (filename, category, pages, w_pt, h_pt)."""
    db = FakeDB()
    project = str(uuid.uuid4())
    db.add("projects", id=project, number="26.10.7212", name="Test project")
    store: dict[str, bytes] = {}
    ids = []
    for i, (name, category, pages, w, h) in enumerate(files):
        path = f"projects/{project}/{i}-{name}"
        content = make_pdf(pages, w, h) if name.lower().endswith(".pdf") else b"PK office"
        store[path] = content
        row = db.add("project_files", id=str(uuid.uuid4()), project_id=project, filename=name, category=category,
                     storage_path=path, size_bytes=len(content))
        ids.append(row["id"])
    return db, project, ids, store


@pytest.fixture
def split_price(monkeypatch):
    monkeypatch.setattr(split_handoff, "estimate_split_usd",
                        lambda pages, settings=None: split_handoff.SplitEstimate(Decimal("0.37") * len(pages),
                                                                                 "m", "openai"))


def test_run_preview_shape_and_money(monkeypatch, split_price):
    db, project, ids, store = preview_db([
        ("E.pdf", "electrical_drawing", 3, 2592, 1728),
        ("Spec.pdf", "specification", 5, 612, 792),
        ("Set.pdf", "drawing", 2, 2592, 1728),
        ("Narrative.docx", "rfp", 0, 0, 0),
    ])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    out = est.run_preview(db, project, None, "u-1", settings=settings())
    assert out["problems"] == []
    assert out["project"] == {"id": project, "number": "26.10.7212", "name": "Test project"}
    assert [f["id"] for f in out["files"]] == ids
    labels = {f["filename"]: f["label"] for f in out["files"]}
    assert labels == {"E.pdf": "electrical_drawings", "Spec.pdf": "specifications", "Set.pdf": None,
                      "Narrative.docx": "rfp"}
    docx = next(f for f in out["files"] if f["filename"] == "Narrative.docx")
    assert docx["pages"] == 1 and docx["approx"] is True
    assert out["pages_total"] == 11 and out["unlabeled_files"] == 1
    assert out["split_usd"] == "0.37"
    for key in ("cost_usd", "cost_usd_max", "ceiling_usd", "split_usd", "hold_usd"):
        assert isinstance(out[key], str)
    ceiling = (Decimal(out["cost_usd_max"]) + Decimal(out["loop_budget_usd"])) * Decimal("1.25")
    assert abs(Decimal(out["ceiling_usd"]) - ceiling) <= Decimal("0.02")
    assert Decimal(out["hold_usd"]) == Decimal(out["ceiling_usd"]) + Decimal("0.37")
    assert out["over_run_limit"] is False and out["model"] == "gpt-6-astra"


def test_run_preview_caps(monkeypatch, split_price):
    many = [(f"E{i}.pdf", "electrical_drawing", 1, 612, 792) for i in range(16)]
    db, project, ids, store = preview_db(many)
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    out = est.run_preview(db, project, ids, "u-1", settings=settings())
    assert "too_many_files" in {p["code"] for p in out["problems"]}
    assert "AI Estimating page" in next(p["message"] for p in out["problems"] if p["code"] == "too_many_files")
    db, project, ids, store = preview_db([("Big.pdf", "specification", 401, 612, 792)])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    out = est.run_preview(db, project, ids, "u-1", settings=settings())
    assert {p["code"] for p in out["problems"]} == {"too_many_pages"}
    db, project, ids, store = preview_db([("Big.pdf", "drawing", 601, 612, 792)])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    out = est.run_preview(db, project, ids, "u-1", settings=settings(assistant_ae_run_max_pages=1500))
    assert {p["code"] for p in out["problems"]} == {"unlabeled_file_too_long"}
    db, project, ids, store = preview_db([(f"S{i}.pdf", "drawing", 1, 612, 792) for i in range(11)])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    out = est.run_preview(db, project, ids, "u-1", settings=settings(assistant_ae_run_max_files=40))
    assert {p["code"] for p in out["problems"]} == {"too_many_unlabeled_files"}
    db, project, ids, store = preview_db([(f"E{i}.pdf", "electrical_drawing", 1, 612, 792) for i in range(15)])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    assert est.run_preview(db, project, ids, "u-1", settings=settings())["problems"] == []


def test_run_preview_lists_refused_and_unreadable_files_as_problems(monkeypatch, split_price):
    db, project, ids, store = preview_db([
        ("E.pdf", "electrical_drawing", 1, 612, 792),
        ("boq.xlsx", "boq", 0, 0, 0),
        ("photo.jpg", "addendum", 0, 0, 0),
    ])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    out = est.run_preview(db, project, ids, "u-1", settings=settings())
    codes = {(p["code"], p.get("file_id")) for p in out["problems"]}
    assert codes == {("file_refused_category", ids[1]), ("file_unreadable_type", ids[2])}
    assert [f["problem"] for f in out["files"]] == [None, "file_refused_category", "file_unreadable_type"]
    # file_ids=None skips them silently (every readable file).
    assert est.run_preview(db, project, None, "u-1", settings=settings())["problems"] == []


def test_run_preview_refusals(monkeypatch, split_price):
    db, project, ids, store = preview_db([("E.pdf", "electrical_drawing", 1, 612, 792)])
    _db2, _p2, foreign_ids, _s2 = preview_db([("X.pdf", "electrical_drawing", 1, 612, 792)])
    db.tables["project_files"].extend(_db2.tables["project_files"])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    with pytest.raises(est.PreviewRefused) as e:
        est.run_preview(db, str(uuid.uuid4()), ids, "u-1", settings=settings())
    assert (e.value.status, e.value.code) == (404, "project_not_found")
    with pytest.raises(est.PreviewRefused) as e:
        est.run_preview(db, project, [str(uuid.uuid4())], "u-1", settings=settings())
    assert (e.value.status, e.value.code) == (404, "file_not_found")
    with pytest.raises(est.PreviewRefused) as e:
        est.run_preview(db, project, foreign_ids, "u-1", settings=settings())
    assert (e.value.status, e.value.code) == (400, "file_not_in_project")
    with pytest.raises(est.PreviewRefused) as e:
        est.run_preview(db, project, ["nope"], "u-1", settings=settings())
    assert (e.value.status, e.value.code) == (400, "file_ids_invalid")
    with pytest.raises(est.PreviewRefused) as e:
        est.run_preview(db, project, [str(uuid.uuid4()) for _ in range(201)], "u-1", settings=settings())
    assert e.value.code == "file_ids_invalid"


def test_an_unpriced_splitter_is_a_problem(monkeypatch):
    db, project, ids, store = preview_db([("Set.pdf", "drawing", 2, 612, 792)])
    monkeypatch.setattr(storage, "download_file", lambda p: store[p])
    monkeypatch.setattr(split_handoff, "estimate_split_usd", lambda pages, settings=None: None)
    out = est.run_preview(db, project, ids, "u-1", settings=settings())
    assert {p["code"] for p in out["problems"]} == {"split_unpriced"}


def test_run_preview_is_within_five_percent_of_runs_estimate(monkeypatch, split_price):
    env = install_env(monkeypatch)
    project = env.db.add("projects", name="Fixture", number="26.10.7212")
    files = [
        ("E.pdf", "electrical_drawing", make_pdf(4, 2592, 1728)),
        ("M.pdf", "mechanical_drawing", make_pdf(2, 3024, 2160)),
        ("Spec.pdf", "specification", make_pdf(30, 612, 792)),
        ("Addendum.pdf", "addendum", make_pdf(3, 612, 792)),
    ]
    ids = []
    for name, category, content in files:
        path = f"projects/{project['id']}/{name}"
        env.store[path] = content
        ids.append(env.db.add("project_files", project_id=project["id"], category=category, filename=name,
                              storage_path=path, size_bytes=len(content))["id"])
    run = new_run(env, source="project", project_id=project["id"])
    runs.add_project_files(env.db, run, project_file_ids=ids, user=user())
    ae = runs.estimate(env.db, run)
    assert ae["problems"] == [] and ae["cost_usd_max"] > 0
    preview = est.run_preview(env.db, project["id"], ids, "u-1", settings=env.settings)
    assert preview["problems"] == []
    for key in ("cost_usd", "cost_usd_max"):
        ours, theirs = float(preview[key]), float(ae[key])
        assert abs(ours - theirs) <= theirs * 0.05, (key, ours, theirs)


# -- 3.5.4 and 3.6.4 state reads ---------------------------------------------------


def test_chat_message_state_redacts_a_pricing_restricted_answer(monkeypatch):
    db = FakeDB()
    msg = db.add("plans_reader_chat_messages", role="assistant", status="done", content="Panel price is $9,999.",
                 error=None, cost_usd=1.234567, pricing_restricted=True)
    out = est.chat_message_state(db, msg["id"], viewer_role="accountant")
    assert out["content"] == chat.PRICING_REDACTED_MESSAGE and "9,999" not in str(out)
    assert out["status"] == "done" and out["cost_usd"] == "1.24" and out["error"] is None
    assert set(out) == {"status", "content", "cost_usd", "error"}
    monkeypatch.setattr(chat, "viewer_sees_pricing", lambda role: True)
    assert "9,999" in est.chat_message_state(db, msg["id"], viewer_role="executive")["content"]


def test_chat_message_state_with_no_client_and_a_missing_message(monkeypatch):
    from app.core import supabase_client

    db = FakeDB()
    msg = db.add("plans_reader_chat_messages", role="assistant", status="failed", content="", cost_usd=None,
                 error="x" * 900)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    out = est.chat_message_state(None, msg["id"], viewer_role="executive")
    assert out["status"] == "failed" and out["cost_usd"] == "0.00" and len(out["error"]) == 300
    assert est.chat_message_state(None, str(uuid.uuid4()), viewer_role="executive")["status"] == "missing"


def test_run_state_reads_the_four_fields(monkeypatch):
    from app.core import supabase_client

    db = FakeDB()
    run = db.add("plans_reader_runs", status="canceled", cost_usd=3.141, finished_at="2026-10-24T10:00:00Z",
                 error="stopped", created_by="u-9")
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    out = est.run_state(None, run["id"])
    assert out == {"status": "canceled", "cost_usd": "3.15", "finished_at": "2026-10-24T10:00:00Z", "error": "stopped"}
    assert est.run_state(db, str(uuid.uuid4()))["status"] == "missing"


# -- Pins ----------------------------------------------------------------------------


def test_every_ae_name_of_3_23_is_still_importable():
    from app.routers import plans_reader as plans_router
    from app.services import plans_reader_agents as agents
    from app.services import plans_reader_rows

    for name in ("create_run", "add_project_files", "estimate", "start", "cancel", "spine_enabled",
                 "loop_budget_usd_max"):
        assert callable(getattr(runs, name)), name
    assert isinstance(runs.PROJECT_CATEGORY_LABELS, dict) and isinstance(runs.REFUSED_PROJECT_CATEGORIES, frozenset)
    for name in ("create_thread", "send_message", "asks_for_change", "redact_messages"):
        assert callable(getattr(chat, name)), name
    assert isinstance(chat.MAX_ROUNDS, int) and isinstance(chat.FEATURE, str)
    for name in ("estimate_run", "plan_page", "plan_file_document", "max_file_pages_for_budget"):
        assert callable(getattr(pack, name)), name
    assert isinstance(pack.PackDoc, type)
    for name in ("page_sizes_in", "native_ppi_for", "image_tokens", "px_for_inches"):
        assert callable(getattr(images, name)), name
    assert isinstance(images.FILE_MAX_PAGES, int)
    assert callable(plans_reader_rows.is_v2)
    assert callable(plans_router._require_available)
    assert callable(agents.prompt_tokens) and callable(agents.split_sheet_title)
    assert isinstance(agents.DRAWING_CATEGORIES, frozenset) and isinstance(agents.LEDGER_MAX_IMAGES, int)
    assert callable(llm_pricing.price_for) and callable(split_handoff.estimate_split_usd)
    assert callable(ai_spend.status)
