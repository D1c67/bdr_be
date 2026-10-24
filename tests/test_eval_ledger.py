"""services/eval_ledger.py and scripts/eval_ledger.py: the $400 live spend
ledger of the AI Estimating build (spec 0.4, WS0 step 10).

Pinned here:
  refusals     unpriced_model, no_open_sweep, sweep_budget, allocation,
               global_ceiling and not_dev, each in isolation; the guard is a
               no-op with AI_EVAL_LEDGER_ENFORCE off and for self-hosted
               routes.
  sweeps       one open sweep at a time; close and abort freeze spent_usd;
               the $360 final zone admits only smoke and holdout sweeps.
  reservation  under 3 concurrent threads at most what the budget covers is
               admitted; released reservations commit the actual cost;
               spend never double counts the process tally and the log.
  llm          a refused call never reaches the provider and is logged; an
               admitted call is stamped with its sweep id in llm_call_log.
  dev only     the stores and the CLI refuse the prod and staging refs.

All numbers are synthetic. No database, no network, no model call.
"""

from __future__ import annotations

import io
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import eval_ledger as el
from app.services import llm, llm_errors, llm_gate, llm_pricing

DEV_URL = f"https://{el.DEV_REF}.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
STAGING_URL = "https://qpfuugcwsdfpxvdgwvwx.supabase.co"
ROUTE = SimpleNamespace(provider="openai", model="gpt-6-astra")


def _s(**over):
    base = dict(ai_eval_ledger_enforce=True, eval_sweep_id="", supabase_url=DEV_URL, is_production=False)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def store():
    st = el.MemoryLedgerStore()
    el.use_store(st)
    yield st
    el.use_store(None)


def _open(store, budget="2", workstream="shared", purpose="dev_probe"):
    return el.open_sweep(store, label="probe", workstream=workstream, purpose=purpose, budget_usd=Decimal(budget))


def _closed(store, workstream, spent, purpose="core"):
    """A closed sweep row written straight into the store (history)."""
    return store.insert_sweep(
        {
            "id": str(uuid.uuid4()),
            "label": "history",
            "workstream": workstream,
            "purpose": purpose,
            "budget_usd": Decimal(spent) + 1,
            "status": "closed",
            "opened_at": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
            "closed_at": datetime(2026, 10, 1, 1, tzinfo=timezone.utc).isoformat(),
            "spent_usd": Decimal(spent),
        }
    )


def _admit(usd, settings=None, route=ROUTE):
    el.set_call_estimate(Decimal(usd))
    return el.admit("plans_main", route, settings or _s())


def _code(excinfo) -> str:
    return excinfo.value.code


# ── refusals ────────────────────────────────────────────────────────────────


def test_guard_is_a_noop_when_enforcement_is_off_or_self_hosted(store):
    reads = store.reads
    assert el.admit("plans_main", ROUTE, _s(ai_eval_ledger_enforce=False)) is el.NO_TICKET
    sh = SimpleNamespace(provider="self_hosted", model="qwen")
    assert el.admit("bid_split", sh, _s()) is el.NO_TICKET
    assert store.reads == reads


def test_unpriced_model_is_refused(store):
    _open(store)
    with pytest.raises(llm_errors.EvalBudgetRefused) as e:
        el.admit("plans_main", SimpleNamespace(provider="openai", model="gpt-6-astra-mini"), _s())
    assert _code(e) == "unpriced_model"


def test_no_open_sweep_is_refused(store):
    with pytest.raises(llm_errors.EvalBudgetRefused) as e:
        _admit("0.01")
    assert _code(e) == "no_open_sweep"
    # EVAL_SWEEP_ID naming a sweep that is not open is refused too.
    row = _closed(store, "shared", "1")
    with pytest.raises(llm_errors.EvalBudgetRefused) as e:
        _admit("0.01", _s(eval_sweep_id=row["id"]))
    assert _code(e) == "no_open_sweep"


def test_sweep_budget(store):
    sweep = _open(store, budget="1.00")
    store.add_call_cost(sweep["id"], "0.80")
    el.reset()
    with pytest.raises(llm_errors.EvalBudgetRefused) as e:
        _admit("0.30")
    assert _code(e) == "sweep_budget"
    ticket = _admit("0.10")
    assert ticket.sweep_id == sweep["id"] and ticket.reserve == Decimal("0.10")
    ticket.release(Decimal("0.05"))


def test_workstream_allocation(store):
    _closed(store, "WS3", "4.50")
    _open(store, budget="1.50", workstream="WS3")
    el.allocate(store, workstream="WS3", usd="5.00", note="trim WS3 for the test")
    with pytest.raises(llm_errors.EvalBudgetRefused) as e:
        _admit("0.80")
    assert _code(e) == "allocation"
    _admit("0.40").release(Decimal("0"))


def test_global_ceiling():
    st = el.MemoryLedgerStore(ceiling_usd=10, allocations={"shared": 10})
    el.use_store(st)
    try:
        _closed(st, "shared", "9.50")
        _open(st, budget="0.50")
        with pytest.raises(llm_errors.EvalBudgetRefused) as e:
            _admit("0.60")
        assert _code(e) == "global_ceiling"
        _admit("0.40").release(Decimal("0.40"))
        assert el.global_spent(st) == Decimal("9.90")
    finally:
        el.use_store(None)


def test_production_and_non_dev_refs_are_refused(store, monkeypatch):
    _open(store)
    with pytest.raises(llm_errors.EvalBudgetRefused) as e:
        _admit("0.01", _s(is_production=True))
    assert _code(e) == "not_dev"
    for url in (PROD_URL, STAGING_URL, ""):
        with pytest.raises(el.EvalLedgerError):
            el.MemoryLedgerStore(supabase_url=url)
        with pytest.raises(el.EvalLedgerError):
            el.SupabaseLedgerStore(object(), supabase_url=url)
    # The default store checks SUPABASE_URL before building any client.
    el.use_store(None)
    monkeypatch.setattr(el, "get_settings", lambda: SimpleNamespace(supabase_url=PROD_URL, eval_sweep_id=""))
    import app.core.supabase_client as sc

    monkeypatch.setattr(sc, "get_supabase", lambda: pytest.fail("no client against prod"))
    with pytest.raises(llm_errors.EvalBudgetRefused) as e:
        _admit("0.01")
    assert _code(e) == "not_dev"


def test_refusals_are_permanent_errors():
    exc = llm_errors.EvalBudgetRefused("refused for the test", code="sweep_budget")
    assert llm_errors.classify(exc) == llm_errors.KIND_NOT_CONFIGURED
    assert not llm_errors.is_transient(exc)
    assert llm_errors.user_message(exc, "gpt-6-astra") == "refused for the test"


# ── sweeps ──────────────────────────────────────────────────────────────────


def test_one_open_sweep_and_close_freezes_spend(store):
    first = _open(store)
    with pytest.raises(el.EvalLedgerError):
        _open(store)
    store.add_call_cost(first["id"], "0.25")
    closed = el.close_sweep(store)
    assert closed["status"] == "closed" and closed["spent_usd"] == Decimal("0.250000")
    # A later log row never changes a closed sweep.
    store.add_call_cost(first["id"], "5")
    assert el.spent_for_sweep(store, first["id"]) == Decimal("0.25")
    second = _open(store)
    aborted = el.abort_sweep(store, second["id"])
    assert aborted["status"] == "aborted"
    with pytest.raises(el.EvalLedgerError):
        el.close_sweep(store, second["id"])
    with pytest.raises(el.EvalLedgerError):
        el.close_sweep(store)


def test_open_refuses_bad_input(store):
    with pytest.raises(el.EvalLedgerError):
        _open(store, purpose="tuning")
    with pytest.raises(el.EvalLedgerError):
        _open(store, workstream="WS9")
    with pytest.raises(el.EvalLedgerError):
        _open(store, workstream="contingency")
    with pytest.raises(el.EvalLedgerError):
        _open(store, budget="0")
    with pytest.raises(el.EvalLedgerError):
        _open(store, budget="30", workstream="WS2")  # past the WS2 allocation


def test_final_zone_admits_only_smoke_and_holdout():
    st = el.MemoryLedgerStore(allocations={"shared": 390, "WS2": 10})
    _closed(st, "shared", str(el.FINAL_ZONE_USD))
    with pytest.raises(el.EvalLedgerError):
        _open(st, budget="1", purpose="dev_probe")
    with pytest.raises(el.EvalLedgerError):
        _open(st, budget="1", purpose="core")
    row = _open(st, budget="1", purpose="smoke")
    assert row["status"] == "open"
    el.close_sweep(st)
    assert _open(st, budget="1", purpose="holdout")["status"] == "open"


def test_allocate_needs_a_note_and_keeps_the_ceiling(store):
    with pytest.raises(el.EvalLedgerError):
        el.allocate(store, workstream="WS4", usd="4", note="  ", from_workstream="contingency")
    allocations = el.allocate(store, workstream="WS4", usd="4", note="owner approved", from_workstream="contingency")
    assert allocations["WS4"]["allocated_usd"] == Decimal("30")
    assert allocations["contingency"]["allocated_usd"] == Decimal("10")
    assert "owner approved" in allocations["contingency"]["note"]
    with pytest.raises(el.EvalLedgerError):
        el.allocate(store, workstream="WS8", usd="50", note="too much")
    _closed(store, "WS5", "3")
    with pytest.raises(el.EvalLedgerError):
        el.allocate(store, workstream="WS5", usd="2", note="below spent")


def test_report_flags_overrun_and_uses_pacific_time(store):
    row = store.insert_sweep(
        {
            "id": str(uuid.uuid4()), "label": "over", "workstream": "WS7", "purpose": "dev_probe",
            "budget_usd": Decimal("2"), "status": "closed", "spent_usd": Decimal("2.40"),
            "opened_at": "2026-10-21T19:00:00+00:00", "closed_at": "2026-10-21T20:00:00+00:00",
        }
    )
    rep = el.report(store)
    sweep = next(s for s in rep["sweeps"] if s["id"] == row["id"])
    assert sweep["overrun"] is True
    assert sweep["opened_at"].startswith("2026-10-21 12:00")
    assert rep["spent_usd"] == Decimal("2.40")
    ws7 = next(w for w in rep["workstreams"] if w["workstream"] == "WS7")
    assert ws7["remaining_usd"] == Decimal("25.60")
    assert "OVERRUN" in el.report_markdown(rep)


def test_status_block(store):
    assert el.status_block(_s(ai_eval_ledger_enforce=False)) == {
        "enforced": False, "open_sweep_id": None, "spent": None, "remaining": None,
        "sweep_spent": None, "sweep_remaining": None,
    }
    sweep = _open(store, budget="3")
    store.add_call_cost(sweep["id"], "1")
    el.reset()
    block = el.status_block(_s())
    assert block["enforced"] is True and block["open_sweep_id"] == sweep["id"]
    assert block["spent"] == 1.0 and block["remaining"] == 399.0
    assert block["sweep_spent"] == 1.0 and block["sweep_remaining"] == 2.0


# ── in-flight reservation ───────────────────────────────────────────────────


def test_inflight_reservation_under_three_concurrent_threads(store):
    sweep = _open(store, budget="1.00")
    start = threading.Barrier(3)
    attempted = threading.Barrier(4)
    hold = threading.Event()
    admitted: list[el.Ticket] = []
    refused: list[str] = []
    lock = threading.Lock()

    def worker():
        start.wait()
        try:
            ticket = _admit("0.40")
        except llm_errors.EvalBudgetRefused as exc:
            with lock:
                refused.append(exc.code)
            attempted.wait()
            return
        with lock:
            admitted.append(ticket)
        attempted.wait()
        hold.wait(5)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    attempted.wait(5)
    # 0.40 + 0.40 fit the 1.00 budget, a third 0.40 does not.
    assert len(admitted) == 2 and refused == ["sweep_budget"]
    assert el.inflight_usd(sweep["id"]) == Decimal("0.80")
    for ticket in admitted:
        ticket.release(Decimal("0.10"))
    hold.set()
    for t in threads:
        t.join(5)
    assert el.inflight_usd(sweep["id"]) == Decimal("0")
    # Released reservations committed their actual cost: 0.20 spent.
    assert el.spent_for_sweep(store, sweep["id"]) == Decimal("0.20")
    _admit("0.80").release(Decimal("0"))
    with pytest.raises(llm_errors.EvalBudgetRefused):
        _admit("0.81")


def test_spend_is_never_double_counted(store):
    sweep = _open(store, budget="5")
    _admit("1").release(Decimal("0.30"))
    assert el.spent_for_sweep(store, sweep["id"]) == Decimal("0.30")
    store.add_call_cost(sweep["id"], "0.30")  # the stamped log row lands
    el._invalidate()
    assert el.spent_for_sweep(store, sweep["id"]) == Decimal("0.30")


def test_timeout_without_usage_commits_the_reserve(store):
    sweep = _open(store, budget="5")
    _admit("0.70").release(None, TimeoutError("read timed out"))
    assert el.spent_for_sweep(store, sweep["id"]) == Decimal("0.70")
    _admit("0.70").release(None, ValueError("never sent"))
    assert el.spent_for_sweep(store, sweep["id"]) == Decimal("0.70")


def test_gate_round_hook_sets_the_next_reserve(store, monkeypatch):
    sweep = _open(store, budget="1")
    monkeypatch.setattr(el, "get_settings", lambda: _s())
    el.gate_round_hook(SimpleNamespace(estimated_usd=Decimal("0.35")))
    ticket = el.admit("plans_recount", ROUTE, _s(), {"op": "turn"})
    assert ticket.reserve == Decimal("0.35") and ticket.sweep_id == sweep["id"]
    ticket.release(Decimal("0.2"))
    # Taken once: the next call derives its own reserve from the request.
    ticket = el.admit("plans_recount", ROUTE, _s(), {"op": "text", "system": "abc", "max_tokens": 10})
    assert ticket.reserve < Decimal("0.01")
    ticket.release(Decimal("0"))


def test_request_derived_reserve_uses_the_long_context_price():
    big = {"op": "text", "system": "x" * 900_000, "messages": [], "max_tokens": 1000}
    got = el.estimate_request_usd("gpt-6-astra", big)
    tokens_in, tokens_out = el.request_tokens(big, "openai")
    assert 300_000 <= tokens_in < 300_010 and tokens_out == 1000
    assert got == llm_pricing.estimate_cost("gpt-6-astra", input_tokens=tokens_in, output_tokens=1000)
    flat = Decimal(300_000) * 10 / 1_000_000 + Decimal(1000) * 50 / 1_000_000
    assert got > flat
    # Images count by their pixel size; no max_tokens means the default.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big") + b"\x08\x02"
    req = {"op": "images_json", "images": [("Page 1", png)], "prompt": ""}
    tokens_in, tokens_out = el.request_tokens(req, "openai")
    assert tokens_in >= 20 * 15  # 640x480 is 20 x 15 patches of 32 px
    assert tokens_out == el.DEFAULT_OUTPUT_TOKENS


# ── through llm._guarded ────────────────────────────────────────────────────


class _FakeOpenAI:
    def __init__(self):
        self.calls = 0
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        usage = SimpleNamespace(
            input_tokens=1000, input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens=100, output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        )
        return SimpleNamespace(id="resp_x", status="completed", output_text="ok", output=[], usage=usage)

    def with_options(self, **kw):
        return self


@pytest.fixture
def wired(store, monkeypatch):
    llm_gate.reset_gates()
    fake = _FakeOpenAI()
    monkeypatch.setattr(llm, "_client_for", lambda route, s: fake)
    inserted: list[dict] = []

    class _Table:
        def insert(self, row):
            inserted.append(dict(row))
            return SimpleNamespace(execute=lambda: None)

    import app.core.supabase_client as sc

    monkeypatch.setattr(sc, "get_supabase", lambda: SimpleNamespace(table=lambda name: _Table()))
    yield fake, inserted
    llm_gate.reset_gates()


def _settings(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", ai_eval_ledger_enforce=True, openai_proposal_model="gpt-6-astra")
    base.update(over)
    return Settings(**base)


def test_refused_call_never_reaches_the_provider_and_is_logged(wired):
    fake, inserted = wired
    with pytest.raises(llm_errors.EvalBudgetRefused):
        llm.complete_text("proposal", system="s", messages=[{"role": "user", "content": "hi"}], settings=_settings())
    assert fake.calls == 0
    assert len(inserted) == 1 and inserted[0]["ok"] is False
    assert "eval_sweep_id" not in inserted[0]


def test_admitted_call_is_stamped_and_committed(store, wired):
    fake, inserted = wired
    sweep = _open(store, budget="5")
    out = llm.complete_text(
        "proposal", system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=200, settings=_settings()
    )
    assert out == "ok" and fake.calls == 1
    assert inserted[-1]["eval_sweep_id"] == sweep["id"] and inserted[-1]["ok"] is True
    cost = llm.last_cost_usd()
    assert cost is not None and cost > 0
    assert el.spent_for_sweep(store, sweep["id"]) == cost
    assert el.inflight_usd() == Decimal("0")
    assert el.admitted_sweep_id() is None


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_ledger.py"
    spec = importlib.util.spec_from_file_location("eval_ledger_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_refuses_a_non_dev_ref(store, capsys):
    cli = _cli()
    for url in (PROD_URL, STAGING_URL):
        rc = cli.run(["status"], store=store, settings=SimpleNamespace(supabase_url=url, is_production=False))
        assert rc == 2
        assert "refused" in capsys.readouterr().err
    reads = store.reads
    rc = cli.run(["report"], store=store, settings=SimpleNamespace(supabase_url=DEV_URL, is_production=True))
    assert rc == 2 and store.reads == reads


def test_cli_open_status_close(store, monkeypatch):
    cli = _cli()
    from app.services import plans_reader_prompt_hash as ph

    monkeypatch.setattr(ph, "prompt_hash", lambda **kw: "h" * 64)
    monkeypatch.setattr(ph, "code_version", lambda: "abc123")
    dev = SimpleNamespace(supabase_url=DEV_URL, is_production=False)
    out = io.StringIO()
    assert cli.run(
        ["open", "--workstream", "WS7", "--purpose", "dev_probe", "--budget", "3", "--label", "recount probe"],
        store=store, settings=dev, out=out,
    ) == 0
    sweep_id = out.getvalue().strip().splitlines()[-1]
    row = store.get_sweep(sweep_id)
    assert row["prompt_hash"] == "h" * 64 and row["code_version"] == "abc123"
    out = io.StringIO()
    assert cli.run(["status"], store=store, settings=dev, out=out) == 0
    assert "Open sweep recount probe" in out.getvalue()
    out = io.StringIO()
    assert cli.run(["close"], store=store, settings=dev, out=out) == 0
    assert store.get_sweep(sweep_id)["status"] == "closed"
    assert cli.run(["report", "--json"], store=store, settings=dev, out=io.StringIO()) == 0
