"""Phase 8 WI-16 (docs/ASSISTANT_PHASE8_PLAN.md 17.4, 16.5): scripts/assistant_triage_tick.py.

No database, model or send is reached: the script runs over the FakeDB twins
of the 0196 RPCs with the one model function replaced by a fake.

Pinned here:

  guards        wrong ref, the prod ref, a flag off, a missing allowlist, an
                allowlist entry that is not an e2e account (email or profile
                id), a bad debounce, a --now outside [real now - 30 days,
                real now + 8 days], a real-model run without the test cap or
                with more than 1 USD, another worker's claimed run under a
                live lease, a server worker that claimed a run within the last
                max(2 x poll, lease) seconds (an expired lease, the script's
                own worker or an older claim does not block), a --worker id
                without the script prefix; a refusal reaches no model and
                writes nothing;
  canned file   both shapes load; every malformed file is refused; rules
                match on kind, project number and key, first match wins,
                unmatched items take the default;
  canned run    no model call, no spend reservation, no llm_call_log row, the
                run stamped model 'canned', decisions stored, an escalation
                emits one router event, travel (+7 days) claims with that
                clock, the debounce override reaches the claim, the allowlist
                reaches enqueue and claim;
  model run     the (faked) model function is called with the pack and the
                settings (test cap, debounce override), the summary reports
                the test cap and one pack hash per call; the real classify with
                the cap at 0 settles skipped cap_test with no reservation;
  forbid-text   a pack holding a forbidden string never reaches the model or
                the canned answer, the run fails bad_answer and is stored with
                no model stamp, the summary counts the hit (never prints the
                string) and main exits 3; strings with a quote, backslash or
                newline match;
  source        clean text, no sender, no model client, no prod target.
"""

from __future__ import annotations

import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.assistant import spend
from app.core import supabase_client
from app.core.config import get_settings
from app.services import assistant_triage as svc
from app.services import llm, llm_pricing
from app.services import assistant_triage_model as model
from scripts import assistant_triage_tick as tt
from tests import assistant_fakes as fk
from tests.assistant_fakes import PROJECT, PROJECT2, USER, FakeDB

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
E2E = "bdr.e2e.admin@g3test.dev"
OTHER_E2E = "bdr.e2e.electrical@g3test.dev"
OUTSIDER = "11111111-1111-4111-8111-1111111111ff"
# Friday 2026-10-30, 3:00 PM Pacific.
T0 = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
EPOCH = T0 - timedelta(hours=20)
SCRIPT = Path(tt.__file__)
REAL_CLASSIFY = model.classify        # before any fixture replaces it
FREE_PRICE = llm_pricing.ModelPrice(input=Decimal("0.1"), cached_input=Decimal("0.01"), output=Decimal("0.5"))


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def uid(n: int) -> str:
    return f"bbbbbbbb-bbbb-4bbb-8bbb-{n:012d}"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    svc.reset_state()
    monkeypatch.setattr(tt, "_real_now", lambda: T0)
    yield
    svc.reset_state()


class World:
    """One FakeDB (company on, one e2e person with record rows) and the
    script's settings; the real `classify` is replaced by a fake that
    records calls (and fails the test if a canned run reaches it)."""

    def __init__(self, monkeypatch, **settings_over):
        self.mp = monkeypatch
        self.db = FakeDB(
            now=T0,
            assistant_settings=[fk.settings_row(proactive_enabled=True, triage_enabled=True,
                                                triage_enabled_at=iso(EPOCH), triage_fuse_tripped_at=None)],
            profiles=[fk.profile(id=USER, role="executive", email=E2E)],
            assistant_profiles=[], assistant_inbox=[], assistant_events=[], assistant_triage_items=[],
            assistant_triage_runs=[], assistant_spend_reservations=[], llm_call_log=[], notifications=[],
            assistant_approvals=[], todos=[],
            projects=[{"id": PROJECT, "name": "Plain", "number": "26.10.7301", "internal_bid_at": None,
                       "current_stage": "rfqs", "abandoned_at": None, "failed_to_bid_at": None},
                      {"id": PROJECT2, "name": "Plain", "number": "26.10.7302", "internal_bid_at": None,
                       "current_stage": "rfqs", "abandoned_at": None, "failed_to_bid_at": None}],
        )
        monkeypatch.setattr(supabase_client, "get_supabase", lambda: self.db)
        base = get_settings().model_copy(update={
            "supabase_url": DEV_URL, "assistant_enabled": True, "assistant_proactive_enabled": True,
            "assistant_triage_enabled": True, "assistant_triage_user_allowlist": E2E,
            "assistant_triage_test_spend_cap_usd": 1.0, "assistant_triage_model": "gpt-6-luna",
            "assistant_triage_debounce_seconds": 300, "assistant_triage_batch_max": 10,
            "assistant_triage_batches_per_tick": 5, "assistant_triage_lease_seconds": 420,
            "assistant_triage_window_hours": 24, "assistant_triage_fuse_ratio": 0.5,
            "assistant_triage_fuse_per_hour": 30, "assistant_triage_cap_pause_seconds": 900,
            "assistant_proactive_quiet_start": "18:00", "assistant_proactive_quiet_end": "06:00",
            "assistant_proactive_digest_times": "07:00,13:00", "assistant_proactive_user_allowlist": "",
            **settings_over,
        })
        self.settings = base
        monkeypatch.setattr(tt, "_settings", lambda: self.settings)
        monkeypatch.setattr(svc, "_clock", lambda: self.db.now)
        monkeypatch.setattr(svc.due_digest, "readiness_rows",
                            lambda sb, projects: [{"project": p, "checks": {"has_quotes": True, "has_labor": True,
                                                                            "has_markups": True},
                                                   "severity": None} for p in projects])
        monkeypatch.setattr(svc, "_missing_categories", lambda sb, ids: {})
        self.model_calls: list[dict] = []
        self.expected_debounce = 0          # the script's default override
        self.model_answer = lambda pack: {"items": [{"key": i["key"], "decision": "none", "reason": "info_only",
                                                     "action": None, "severity": None} for i in pack["items"]]}
        monkeypatch.setattr(model, "classify", self._fake_model)

    def _fake_model(self, run_id, pack, *, settings):
        assert settings.assistant_triage_test_spend_cap_usd == self.settings.assistant_triage_test_spend_cap_usd
        assert settings.assistant_triage_debounce_seconds == self.expected_debounce
        self.model_calls.append(pack)
        return model.TriageResult(answer=self.model_answer(pack))

    def record(self, n: int, *, kind: str = "bell.drawing_changed", project: str | None = PROJECT,
               minutes_old: int = 10) -> dict:
        row = fk.inbox_row(id=uid(n), user_id=USER, dedupe_key=f"p7:{kind}:{n}", event_id=None, severity="normal",
                           title="Plain", why="Plain", delivery="record", deliver_after=None, event_type=kind,
                           project_id=project, source_notification_id=None,
                           created_at=iso(T0 - timedelta(minutes=minutes_old)))
        self.db.tables["assistant_inbox"].append(row)
        return row

    def canned(self, tmp_path: Path, spec) -> str:
        path = tmp_path / "canned.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return str(path)

    def run(self, *args: str) -> dict:
        if "--debounce" in args:
            self.expected_debounce = int(args[args.index("--debounce") + 1])
        return tt.run(["--now", iso(T0), *args])

    @property
    def runs(self) -> list[dict]:
        return self.db.tables["assistant_triage_runs"]

    @property
    def items(self) -> list[dict]:
        return self.db.tables["assistant_triage_items"]

    def events(self) -> list[dict]:
        return [e for e in self.db.tables["assistant_events"] if e.get("event_type") == "assistant.triage_escalation"]


@pytest.fixture
def w(monkeypatch):
    return World(monkeypatch)


ESCALATE_HIGH = {"decision": "escalate", "reason": "waiting_on_you", "action": None, "severity": "high"}


# ── Guards ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["", None, "http://127.0.0.1:54321", "https://abcdefghijklmnopqrst.supabase.co",
                                 "https://bpidntbyvoooqvaispup.supabase.co.evil.test"])
def test_a_non_dev_supabase_url_is_refused(w, url):
    w.settings = w.settings.model_copy(update={"supabase_url": url})
    with pytest.raises(tt.Refused, match="not the dev project"):
        w.run("--canned-answer", "x")
    assert w.db.rpc_calls == []


def test_the_prod_project_is_named_and_refused(w):
    w.settings = w.settings.model_copy(update={"supabase_url": PROD_URL})
    with pytest.raises(tt.Refused, match="production"):
        w.run()
    assert w.db.rpc_calls == []


@pytest.mark.parametrize("flag", ["assistant_enabled", "assistant_proactive_enabled", "assistant_triage_enabled"])
def test_every_flag_must_be_on(w, flag):
    w.settings = w.settings.model_copy(update={flag: False})
    with pytest.raises(tt.Refused, match=flag.upper()):
        w.run()
    assert w.db.rpc_calls == []


@pytest.mark.parametrize("raw", ["", " ", ",", " , ,", None])
def test_a_missing_allowlist_is_refused(w, raw):
    w.settings = w.settings.model_copy(update={"assistant_triage_user_allowlist": raw})
    with pytest.raises(tt.Refused, match="ALLOWLIST"):
        w.run()
    assert w.db.rpc_calls == []


def test_the_allowlist_must_hold_only_e2e_accounts(w):
    for raw in ("someone@example.com", f"{E2E},tom@g3.test", "not-an-id-or-email", OUTSIDER):
        w.settings = w.settings.model_copy(update={"assistant_triage_user_allowlist": raw})
        with pytest.raises(tt.Refused, match="e2e|profile id or an email"):
            w.run()
    assert w.db.rpc_calls == []


def test_a_profile_id_is_checked_through_its_profile_email(w):
    w.db.tables["profiles"].append(fk.profile(id=OUTSIDER, email="tom@g3.test"))
    w.settings = w.settings.model_copy(update={"assistant_triage_user_allowlist": f"{USER},{OUTSIDER}"})
    with pytest.raises(tt.Refused, match="e2e"):
        w.run()
    w.settings = w.settings.model_copy(update={"assistant_triage_user_allowlist": f"{USER},{OTHER_E2E}"})
    tt.require_e2e_allowlist(tt.allowlist_entries(w.settings), lambda i: {"email": E2E} if i == USER else None)


@pytest.mark.parametrize("value", [-1, 3601])
def test_a_bad_debounce_is_refused(w, value):
    with pytest.raises(tt.Refused, match="debounce"):
        w.run("--debounce", str(value))


@pytest.mark.parametrize("delta", [timedelta(days=-31), timedelta(days=9)])
def test_a_clock_outside_the_travel_window_is_refused(w, delta):
    with pytest.raises(tt.Refused, match="30 days"):
        tt.run(["--now", iso(T0 + delta)])
    assert w.db.rpc_calls == []


def test_naive_now_is_pacific(w):
    assert tt.build_parser().parse_args(["--now", "2026-10-30T15:00"]).now == T0


@pytest.mark.parametrize("cap", [None, 1.5, 5.0, -1.0])
def test_a_model_run_needs_the_test_cap_of_at_most_one_usd(w, cap):
    w.settings = w.settings.model_copy(update={"assistant_triage_test_spend_cap_usd": cap})
    w.record(1)
    with pytest.raises(tt.Refused, match="TEST_SPEND_CAP"):
        w.run()
    assert w.model_calls == [] and w.db.rpc_calls == [] and w.runs == []


def test_the_cap_is_not_needed_for_a_canned_run_and_zero_is_allowed_for_a_model_run(w, tmp_path):
    w.settings = w.settings.model_copy(update={"assistant_triage_test_spend_cap_usd": None})
    w.record(1)
    assert w.run("--canned-answer", w.canned(tmp_path, {"items": []}))["mode"] == "canned"
    w.settings = w.settings.model_copy(update={"assistant_triage_test_spend_cap_usd": 0.0})
    assert w.run()["test_spend_cap_usd"] == 0.0


def test_another_workers_live_claimed_run_blocks_the_tick(w):
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="server-5071", lease_until=iso(T0 + timedelta(minutes=5))))
    w.record(1)
    with pytest.raises(tt.Refused, match="another worker"):
        w.run("--worker", "triage-tick:mine")
    assert w.model_calls == [] and w.db.rpc_calls == []


def test_an_expired_lease_or_its_own_claimed_run_does_not_block(w):
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="dead-worker", lease_until=iso(T0 - timedelta(minutes=5))))
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="triage-tick:mine", lease_until=iso(T0 + timedelta(minutes=5))))
    summary = w.run("--worker", "triage-tick:mine")
    assert summary["expired_claimed_runs_seen"] == 1


def test_a_finished_run_of_another_worker_does_not_block(w):
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="server-5071", status="decided", claimed_at=iso(T0 - timedelta(hours=2)),
        lease_until=iso(T0 + timedelta(minutes=5))))
    w.run()


def test_a_server_run_claimed_moments_ago_blocks_the_tick_even_when_it_is_settled(w):
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="host:5071:ab12cd34", status="decided", claimed_at=iso(T0 - timedelta(seconds=30)),
        lease_until=iso(T0 + timedelta(minutes=5))))
    w.record(1)
    with pytest.raises(tt.Refused, match="loop looks active"):
        w.run()
    assert w.model_calls == [] and w.db.rpc_calls == []


def test_server_claims_older_than_the_window_and_script_ticks_do_not_block(w):
    # default window: max(2 x 60 poll, 420 lease) = 420 seconds
    assert tt.recent_window_seconds(w.settings) == 420
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="host:5071:ab12cd34", status="decided", claimed_at=iso(T0 - timedelta(seconds=421)),
        lease_until=iso(T0 + timedelta(minutes=5))))
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="triage-tick:other:1:aa", status="decided", claimed_at=iso(T0 - timedelta(seconds=10)),
        lease_until=iso(T0 + timedelta(minutes=5))))
    w.run()


def test_the_recent_window_follows_the_poll_interval_when_it_is_longer(w):
    w.settings = w.settings.model_copy(update={"assistant_triage_poll_seconds": 600,
                                               "assistant_triage_lease_seconds": 420})
    assert tt.recent_window_seconds(w.settings) == 1200
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="host:5071:ab12cd34", status="decided", claimed_at=iso(T0 - timedelta(seconds=900)),
        lease_until=iso(T0 - timedelta(seconds=480))))
    with pytest.raises(tt.Refused, match="loop looks active"):
        w.run()


@pytest.mark.parametrize("worker", ["mine", "host:5071:ab12cd34", "TRIAGE-TICK:x", "triage-tick:" + "x" * 200])
def test_a_worker_id_without_the_script_prefix_is_refused(w, worker):
    w.record(1)
    with pytest.raises(tt.Refused, match="--worker"):
        w.run("--worker", worker)
    assert w.model_calls == [] and w.db.rpc_calls == [] and w.runs == []


# ── The canned-answer file ───────────────────────────────────────────────────


def test_both_canned_shapes_load(tmp_path, w):
    raw = tt.load_canned(w.canned(tmp_path, {"items": [{"key": "t1", "bogus": 1}]}))
    assert raw == {"raw": {"items": [{"key": "t1", "bogus": 1}]}}
    spec = tt.load_canned(w.canned(tmp_path, {"default": ESCALATE_HIGH, "rules": [
        {"kind": "bell.due", "decision": "none", "reason": "covered_by_app"}]}))
    assert spec["default"]["severity"] == "high" and spec["rules"][0]["match"] == {"kind": "bell.due"}
    spec = tt.load_canned(w.canned(tmp_path, {"rules": [{"key": "t1", "decision": "suggest",
                                                         "reason": "waiting_on_you", "action": "review_verify"}]}))
    assert spec["default"] == tt.DEFAULT_DECISION


@pytest.mark.parametrize("spec", [
    [], "text", {}, {"items": [], "rules": []}, {"rules": []}, {"rules": "x"}, {"rules": [5]},
    {"rules": [{"decision": "none", "reason": "info_only"}]},                       # no match field
    {"rules": [{"kind": "bell.due", "decision": "maybe", "reason": "info_only"}]},
    {"rules": [{"kind": "bell.due", "decision": "none", "reason": "waiting_on_you"}]},   # reason not for none
    {"rules": [{"kind": "bell.due", "decision": "escalate", "reason": "waiting_on_you"}]},   # no severity
    {"rules": [{"kind": "bell.due", "decision": "escalate", "reason": "waiting_on_you", "severity": "now"}]},
    {"rules": [{"kind": "bell.due", "decision": "escalate", "reason": "waiting_on_you", "severity": "high",
                "action": "review_verify"}]},
    {"rules": [{"kind": "bell.due", "decision": "suggest", "reason": "waiting_on_you"}]},   # no action
    {"rules": [{"kind": "bell.due", "decision": "suggest", "reason": "waiting_on_you", "action": "wire_money"}]},
    {"rules": [{"kind": "bell.due", "decision": "none", "reason": "info_only", "severity": "high"}]},
    {"rules": [{"kind": "verified.nope", "decision": "none", "reason": "info_only"}]},
    {"rules": [{"kind": "bell.due", "decision": "none", "reason": "info_only", "extra": 1}]},
    {"default": "x", "rules": [{"kind": "bell.due", "decision": "none", "reason": "info_only"}]},
    {"unknown": 1, "rules": [{"kind": "bell.due", "decision": "none", "reason": "info_only"}]},
])
def test_a_malformed_canned_file_is_refused(w, tmp_path, spec):
    with pytest.raises(tt.Refused):
        tt.load_canned(w.canned(tmp_path, spec))


def test_unreadable_or_non_json_canned_files_are_refused(tmp_path):
    with pytest.raises(tt.Refused, match="could not be read"):
        tt.load_canned(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(tt.Refused, match="not valid JSON"):
        tt.load_canned(str(bad))


def test_rules_match_on_all_named_fields_first_match_wins_and_the_default_covers_the_rest():
    spec = {"default": {**tt.DEFAULT_DECISION}, "rules": [
        {"match": {"kind": "bell.due", "project_number": "26.10.7301"}, **ESCALATE_HIGH},
        {"match": {"kind": "bell.due"}, "decision": "none", "reason": "covered_by_app", "action": None,
         "severity": None},
        {"match": {"key": "t3"}, "decision": "suggest", "reason": "waiting_on_you", "action": "review_verify",
         "severity": None},
    ]}
    pack = {"items": [
        {"key": "t1", "kind": "bell.due", "project_number": "26.10.7301"},
        {"key": "t2", "kind": "bell.due", "project_number": "26.10.7302"},
        {"key": "t3", "kind": "bell.drawing_changed", "project_number": None},
        {"key": "t4", "kind": "bell.drawing_changed", "project_number": None},
    ]}
    got = {i["key"]: (i["decision"], i["reason"]) for i in tt.canned_answer(spec, pack)["items"]}
    assert got == {"t1": ("escalate", "waiting_on_you"), "t2": ("none", "covered_by_app"),
                   "t3": ("suggest", "waiting_on_you"), "t4": ("none", "no_action_needed")}


# ── A canned run ─────────────────────────────────────────────────────────────


def test_a_canned_run_decides_without_a_model_a_reservation_or_a_cost(w, tmp_path):
    w.mp.setattr(model, "classify", lambda *a, **k: pytest.fail("the real model function was called"))
    w.mp.setattr(spend, "reserve", lambda *a, **k: pytest.fail("a spend reservation was made"))
    for n in (1, 2, 3):
        w.record(n)
    path = w.canned(tmp_path, {"default": {**tt.DEFAULT_DECISION}, "rules": [{"key": "t2", **ESCALATE_HIGH}]})
    summary = w.run("--canned-answer", path, "--debounce", "0")
    assert summary["mode"] == "canned" and summary["poll"]["enqueued"] == 3
    [run] = w.runs
    assert run["model"] == "canned" and run["status"] == "decided" and run["cost_usd"] is None
    assert sorted(i["decision"] for i in w.items) == ["escalate", "none", "none"]
    assert w.db.tables["assistant_spend_reservations"] == [] and w.db.tables["llm_call_log"] == []
    assert len(w.events()) == 1 and summary["poll"]["batches"] == {"decided": 1}
    [rep] = summary["runs"]
    assert rep["model"] == "canned" and len(rep["items"]) == 3
    assert [p["items"] for p in summary["packs"]] == [3] and len(summary["packs"][0]["pack_sha256"]) == 64


def test_a_canned_batch_with_nothing_to_judge_leaves_the_model_unset(w, tmp_path):
    w.record(1)
    path = w.canned(tmp_path, {"items": []})
    w.run("--canned-answer", path, "--debounce", "3600")          # enqueues, claims nothing
    w.db.tables["assistant_inbox"][0]["dismissed_at"] = iso(T0 - timedelta(minutes=1))
    summary = w.run("--canned-answer", path)
    [run] = w.runs
    assert run["model"] is None and summary["packs"] == []
    assert [i["skip_reason"] for i in w.items] == ["dismissed"]


def test_a_raw_malformed_canned_answer_goes_through_the_validator(w, tmp_path):
    w.record(1)
    summary = w.run("--canned-answer", w.canned(tmp_path, {"items": [
        {"key": "t1", "decision": "escalate", "reason": "waiting_on_you", "action": None, "severity": "now"}]}))
    [item] = w.items
    assert item["decision"] == "none" and item["reason"] == "invalid_answer"
    assert summary["runs"][0]["model"] == "canned" and w.events() == []


def test_the_debounce_override_and_the_allowlist_reach_enqueue_and_claim(w, tmp_path):
    w.record(1, minutes_old=1)          # one minute old: a 300 second debounce would hold it
    summary = w.run("--canned-answer", w.canned(tmp_path, {"items": []}), "--debounce", "0")
    assert summary["debounce_seconds"] == 0 and len(w.runs) == 1
    claims = [p for n, p in w.db.rpc_calls if n == "assistant_triage_claim"]
    enqueues = [p for n, p in w.db.rpc_calls if n == "assistant_triage_enqueue"]
    assert claims and all(c["p_debounce_seconds"] == 0 and c["p_user_ids"] == [USER] for c in claims)
    assert enqueues and all(e["p_user_ids"] == [USER] for e in enqueues)
    assert w.settings.assistant_triage_debounce_seconds == 300      # the shared settings are untouched


def test_the_default_debounce_is_zero_and_a_longer_one_holds_a_young_row(w, tmp_path):
    w.record(1, minutes_old=1)
    summary = w.run("--canned-answer", w.canned(tmp_path, {"items": []}), "--debounce", "600")
    assert w.runs == [] and summary["poll"].get("batches", {}) == {}
    assert tt.build_parser().parse_args(["--now", "2026-10-30T15:00"]).debounce == 0


def test_time_travel_claims_with_the_travelled_clock(w, tmp_path):
    w.record(1)
    later = T0 + timedelta(days=7)
    w.db.now = later
    summary = tt.run(["--now", iso(later), "--canned-answer", w.canned(tmp_path, {"items": []})])
    claims = [p for n, p in w.db.rpc_calls if n == "assistant_triage_claim"]
    assert claims and claims[0]["p_now"] == later.isoformat()
    assert summary["now"] == later.isoformat() and summary["now_pacific"].endswith("-08:00")


def test_a_live_foreign_run_check_uses_the_real_clock_not_the_travelled_one(w, tmp_path):
    w.db.tables["assistant_triage_runs"].append(fk.triage_run_row(
        claimed_by="server-5071", lease_until=iso(T0 + timedelta(minutes=5))))
    with pytest.raises(tt.Refused, match="another worker"):
        tt.run(["--now", iso(T0 + timedelta(days=7)), "--canned-answer", w.canned(tmp_path, {"items": []})])


# ── A model run ──────────────────────────────────────────────────────────────


def test_a_model_run_calls_the_model_function_with_the_pack_and_reports_the_cap(w):
    w.record(1)
    w.record(2)
    summary = w.run()
    assert len(w.model_calls) == 1 and [i["key"] for i in w.model_calls[0]["items"]] == ["t1", "t2"]
    assert summary["mode"] == "model" and summary["test_spend_cap_usd"] == 1.0
    assert [p["items"] for p in summary["packs"]] == [2]
    [run] = w.runs
    assert run["model"] == "gpt-6-luna"          # stamped from the setting, never 'canned'


def test_the_real_classify_with_a_zero_cap_settles_skipped_cap_test_and_defers_the_items(w):
    w.mp.setattr(model, "classify", REAL_CLASSIFY)
    w.mp.setattr(llm, "resolve", lambda feature, settings=None: SimpleNamespace(provider="openai",
                                                                               model="gpt-6-luna"))
    w.mp.setitem(llm_pricing.PRICES, "gpt-6-luna", FREE_PRICE)
    w.mp.setattr(model.repo, "triage_company_spend", lambda user_id: {"spent": 0, "held": 0}, raising=False)
    w.mp.setattr(spend, "reserve", lambda *a, **k: pytest.fail("a spend reservation was made"))
    w.mp.setattr(llm, "complete_json", lambda *a, **k: pytest.fail("the model was called"))
    w.settings = w.settings.model_copy(update={"assistant_triage_test_spend_cap_usd": 0.0})
    w.record(1)
    w.record(2)
    summary = w.run()
    assert summary["test_spend_cap_usd"] == 0.0
    [run] = w.runs
    assert run["status"] == "skipped" and run["error_kind"] == "cap_test" and run["model"] is None
    assert run["reservation_id"] is None and run["cost_usd"] is None
    assert len(w.items) == 2
    assert all(i["status"] == "pending" and i["cap_deferred_at"] for i in w.items)
    assert w.db.tables["assistant_spend_reservations"] == [] and w.db.tables["llm_call_log"] == []


def test_the_classify_and_finish_functions_are_restored_after_a_run(w, tmp_path):
    real_classify, real_finish, real_settings = model.classify, svc._finish, svc.get_settings
    w.record(1)
    w.run("--canned-answer", w.canned(tmp_path, {"items": []}))
    assert model.classify is real_classify and svc._finish is real_finish and svc.get_settings is real_settings


# ── Forbidden text ───────────────────────────────────────────────────────────


def test_a_forbidden_string_in_a_pack_fails_the_run_closed(w, tmp_path, capsys):
    w.record(1)
    forbid = tmp_path / "forbid.json"
    forbid.write_text(json.dumps(["26.10.7301", "never present"]), encoding="utf-8")
    summary = w.run("--forbid-text", str(forbid))
    assert w.model_calls == []
    [run] = w.runs
    assert run["status"] == "failed" and run["error_kind"] == "bad_answer"
    assert summary["forbidden_hits"] == 1 and summary["packs"][0]["forbidden_hits"] == 1
    assert "26.10.7301" not in json.dumps(summary["packs"])


def test_main_exits_three_on_a_forbidden_hit_and_zero_otherwise(w, tmp_path, capsys):
    w.record(1)
    forbid = tmp_path / "forbid.txt"
    forbid.write_text("26.10.7301\n", encoding="utf-8")          # one string per line
    assert tt.main(["--now", iso(T0), "--forbid-text", str(forbid)]) == 3
    out = capsys.readouterr()
    assert "forbidden" in out.err and json.loads(out.out)["forbidden_hits"] == 1
    w.db.tables["assistant_triage_items"].clear()
    w.db.tables["assistant_triage_runs"].clear()
    w.record(2)
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps(["Ignore previous instructions"]), encoding="utf-8")
    assert tt.main(["--now", iso(T0), "--forbid-text", str(clean)]) == 0


def test_a_forbidden_string_blocks_the_canned_answer_too(w, tmp_path):
    w.record(1)
    forbid = tmp_path / "forbid.json"
    forbid.write_text(json.dumps(["26.10.7301"]), encoding="utf-8")
    summary = w.run("--canned-answer", w.canned(tmp_path, {"default": ESCALATE_HIGH, "rules": [
        {"key": "t1", **ESCALATE_HIGH}]}), "--forbid-text", str(forbid))
    assert summary["forbidden_hits"] == 1 and w.events() == []
    assert w.runs[0]["error_kind"] == "bad_answer"


def test_a_model_mode_forbidden_hit_is_stored_without_a_model_stamp(w, tmp_path):
    w.record(1)
    forbid = tmp_path / "forbid.json"
    forbid.write_text(json.dumps(["26.10.7301"]), encoding="utf-8")
    w.run("--forbid-text", str(forbid))
    [run] = w.runs
    assert w.model_calls == [] and run["status"] == "failed" and run["error_kind"] == "bad_answer"
    assert run["model"] is None and run["input_tokens"] is None


@pytest.mark.parametrize("needle", ['He said "ignore all" now', "line1\nline2", "a\\b", "tab\there", "x" + chr(1) + "y"])
def test_forbidden_strings_with_escapable_characters_match_the_raw_text(needle):
    assert tt.forbidden_hits({"x": f"before {needle} after"}, [needle]) == 1
    assert tt.forbidden_hits({"k": ["nested", {"deep": needle.upper()}]}, [needle]) == 1
    assert tt.forbidden_hits({needle: 1}, [needle]) == 1             # a key
    assert tt.forbidden_hits({"x": "clean"}, [needle]) == 0


def test_a_forbidden_string_with_quote_and_newline_in_a_real_pack_fails_the_run(w, tmp_path):
    w.record(1)
    forbid = tmp_path / "forbid.json"
    forbid.write_text(json.dumps(['"26.10.7301"\nx', "26.10.7301"]), encoding="utf-8")
    # first string never appears; the second does (a plain project number in the pack)
    assert w.run("--forbid-text", str(forbid))["forbidden_hits"] == 1
    assert w.model_calls == []


def test_the_rendered_model_text_is_scanned_too(monkeypatch):
    from app.assistant import triage

    pack = {"time": {}, "items": []}
    assert tt.forbidden_hits(pack, ["only in rendered"]) == 0          # a pack that cannot render: leaves only
    monkeypatch.setattr(triage, "render_pack", lambda p: "Header\nONLY IN RENDERED text")
    assert tt.forbidden_hits(pack, ["only in rendered"]) == 1


@pytest.mark.parametrize("content", ["", "[]", "[1]", '["", "x"]', "{}"])
def test_a_bad_forbid_file_is_refused(tmp_path, content):
    path = tmp_path / "f.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(tt.Refused):
        tt.load_forbidden(str(path))


def test_the_pack_hash_is_stable_and_order_independent():
    assert tt.pack_hash({"a": 1, "b": [1, 2]}) == tt.pack_hash({"b": [1, 2], "a": 1})
    assert tt.pack_hash({"a": 1}) != tt.pack_hash({"a": 2})
    assert tt.forbidden_hits({"x": "Hello World"}, ["hello", "nope", "WORLD"]) == 2


# ── main ─────────────────────────────────────────────────────────────────────


def test_main_prints_json_and_a_refusal_exits_two_with_nothing_on_stdout(w, tmp_path, capsys):
    w.record(1)
    assert tt.main(["--now", iso(T0), "--canned-answer", w.canned(tmp_path, {"items": []})]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "canned"
    w.settings = w.settings.model_copy(update={"supabase_url": PROD_URL})
    assert tt.main(["--now", iso(T0)]) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("REFUSED: ")


def test_settings_that_do_not_load_are_a_refusal_without_values(monkeypatch):
    def boom():
        raise ValueError("secret-value-123")

    monkeypatch.setattr(tt, "_settings", boom)
    with pytest.raises(tt.Refused) as err:
        tt.run(["--now", iso(T0)])
    assert "secret-value-123" not in str(err.value) and "ValueError" in str(err.value)


def test_the_guards_read_only_the_settings_they_are_given():
    ok = SimpleNamespace(assistant_enabled=True, assistant_proactive_enabled=True, assistant_triage_enabled=True,
                         assistant_triage_user_allowlist=f" {E2E.upper()} , {OTHER_E2E} ",
                         assistant_triage_test_spend_cap_usd=0.5)
    tt.require_flags(ok)
    assert tt.require_allowlist(ok) == [E2E, OTHER_E2E]
    assert tt.require_live_cap(ok) == 0.5
    tt.require_e2e_allowlist([E2E, OTHER_E2E], lambda i: None)


# ── Source scan ──────────────────────────────────────────────────────────────


def test_the_script_is_clean_text_that_never_sends_or_calls_a_model_client():
    text = SCRIPT.read_text(encoding="utf-8")
    assert EM_DASH not in text and EN_DASH not in text
    for banned in ("notify_role", "notify_user", "notifications.deliver", "notification_email", "teams_alerts",
                   "graph_email", "send_mail", "openai", "anthropic", "get_supabase_admin", "llm.complete",
                   "assistant_proactive.", "poll_once(now" + ", tails"):
        assert banned not in text, banned
    assert "require_dev_ref(settings)" in text            # the dev ref guard is the Phase 7 one
    assert "zxqkqotlcgbalwoqeeca" not in text and "qpfuugcwsdfpxvdgwvwx" not in text


def test_this_file_has_no_em_or_en_dash():
    text = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in text and EN_DASH not in text
