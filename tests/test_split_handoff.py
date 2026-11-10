"""The assistant splitter handoff service (docs/ASSISTANT_PHASE2_PLAN.md 3.3,
WI-3): app/services/split_handoff.py over the assistant FakeDB, the fake
0174 spend rpcs and FakeStorage.

Invariants pinned here:

  estimate   the worst-case figures for 1, 23, 100 and 400 pages against
             the PRICES rows, 0 on the self-hosted route, None for an
             unpriced model;
  dupes      a lazily hashed size-matched project row is a duplicate; a row
             the hash budget could not reach is not (fail open);
  drop       the happy path (task, job, files, claim, hold, one direct
             enqueue per file with 2 attempts, staging objects purged unless
             the owner is on legal hold); the replay; two approvals over the
             same files; every refusal with no task, no job, no hold and no
             claim left behind; the one compensation block;
  outcome    split, intact, whole with each reason from structured state,
             failed, landing within the grace and missing after it;
  progress   only the staged files count; the project ref; a gone job;
  attach     links a manual job, files its done files, every refusal, a
             replay re-runs the reconcile without duplicates;
  status     the staged files of the turn's conversation, the manual jobs,
             the tasks, the project and the spend left;
  tables     what split_drop, resync_after_run and attach write is a subset
             of each tool's writes_tables (a local copy WI-5 pins).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.assistant import repo
from app.core import provenance, supabase_client
from app.core.deps import CurrentUser
from app.core.roles import Role
from app.services import file_hashes, llm, llm_queue, rfp_file_drop, rfp_split, split_handoff, storage

from tests.assistant_fakes import (
    APPROVAL_ID, CONV, MSG_ASSISTANT, MSG_USER, NOW, OTHER_USER, PROJECT, RUN, STAGED, STAGED2, USER,
    FakeDB, FakeStorage, approval, message, settings_row, staged_file,
)

APPROVAL2 = "99999999-9999-4999-8999-999999999998"
CONV2 = "44444444-4444-4444-8444-444444444445"
NUMBER = "26.10.7267"
MANUAL_JOB = "dddddddd-dddd-4ddd-8ddd-ddddddddddd9"
EST = Decimal("0.68")

# Local copies of the tools' writes_tables (docs/ASSISTANT_PHASE2_PLAN.md
# 3.10.1 and 3.10.2); WI-5's equality test pins them to app.assistant.tools.splits.
SPLIT_WRITES_TABLES = (
    "bid_split_jobs", "bid_split_files", "assistant_tasks", "assistant_files", "assistant_spend_reservations",
    "audit_log", "llm_jobs", "project_files", "bid_split_segments", "notifications", "projects",
    "rfp_file_issues",
)
ATTACH_WRITES_TABLES = ("bid_split_jobs", "project_files", "audit_log", "notifications", "projects", "rfp_file_issues")


def _user(uid: str = USER, role: Role = Role.EXECUTIVE, run: str | None = RUN) -> CurrentUser:
    return CurrentUser(id=uid, email="tom@g3.test", role=role, is_active=True, via_assistant_run_id=run)


def _linked(**over) -> dict:
    """A staged file sent with the user message of CONV."""
    return staged_file(**{"message_id": MSG_USER, "conversation_id": CONV, **over})


def _db(**tables) -> FakeDB:
    base = dict(
        assistant_files=[_linked(id=STAGED, sha256="a" * 64, size_bytes=1234, page_count=3),
                         _linked(id=STAGED2, filename="Spec.pdf", sha256="b" * 64, size_bytes=5678, page_count=2,
                                 storage_path=f"assistant-files/{USER}/{STAGED2}-Spec.pdf")],
        assistant_messages=[message(), message(id=MSG_ASSISTANT, role="assistant", content="card")],
        assistant_approvals=[approval(action_kind="split_files_into_project", status="executing")],
        assistant_tasks=[], assistant_spend_reservations=[], assistant_profiles=[],
        assistant_settings=[settings_row()], assistant_tool_calls=[], llm_call_log=[],
        projects=[{"id": PROJECT, "number": NUMBER, "name": "Monument"}],
        project_files=[], bid_split_jobs=[], bid_split_files=[], bid_split_segments=[], llm_jobs=[],
    )
    base.update(tables)
    return FakeDB(**base)


@pytest.fixture
def env(monkeypatch):
    db = _db()
    store = FakeStorage().install(monkeypatch)
    for row in db.tables["assistant_files"]:
        store.objects[row["storage_path"]] = b"%PDF-bytes-" + row["id"].encode()
    state = SimpleNamespace(db=db, store=store, locked=False, splitter=(True, None), enqueued=[], audits=[],
                            canceled=[], prefixes=[], hashes={}, fail_enqueue_at=None)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: state.db)
    monkeypatch.setattr(split_handoff, "_handoff_locked", lambda pid: state.locked)
    monkeypatch.setattr(rfp_file_drop, "splitter_state", lambda s=None: state.splitter)
    monkeypatch.setattr(split_handoff, "estimate_split_usd",
                        lambda pages, s=None: split_handoff.SplitEstimate(EST, "claude-opus-5", "anthropic"))
    monkeypatch.setattr(split_handoff, "audit", lambda *a, **k: state.audits.append(a))
    monkeypatch.setattr(llm, "active_model", lambda f, s=None: "claude-opus-5")

    def enqueue(job_type, **kw):
        if state.fail_enqueue_at is not None and len(state.enqueued) == state.fail_enqueue_at:
            raise RuntimeError("queue down")
        state.enqueued.append({"job_type": job_type, **kw})
        state.db.tables["llm_jobs"].append({"id": f"lj-{len(state.enqueued)}", "job_type": job_type,
                                            "target_id": kw["target_id"], "status": "queued"})
        return {"id": f"lj-{len(state.enqueued)}"}

    monkeypatch.setattr(llm_queue, "enqueue", enqueue)
    monkeypatch.setattr(llm_queue, "active_job", lambda jt, tid: next(
        (j for j in state.db.tables["llm_jobs"] if j["target_id"] == tid and j["status"] == "queued"), None))

    def cancel(job_id):
        state.canceled.append(job_id)
        for j in state.db.tables["llm_jobs"]:
            if j["id"] == job_id:
                j["status"] = "canceled"
        return {"id": job_id}

    monkeypatch.setattr(llm_queue, "cancel", cancel)
    monkeypatch.setattr(storage, "delete_bid_split_prefix", lambda job_id: state.prefixes.append(job_id))
    monkeypatch.setattr(file_hashes, "sha256_of_object", lambda path: state.hashes[path])
    return state


def _drop(env, ids=(STAGED, STAGED2), *, approval_id=APPROVAL_ID, max_usd="0.68", user=None):
    user = user or _user()
    with provenance.acting_as_assistant(user.id, RUN, approval_id):
        return split_handoff.split_drop(env.db, PROJECT, user, list(ids), approval_id=approval_id, run_id=RUN,
                                        max_usd=Decimal(max_usd))


def _nothing_left(env) -> None:
    db = env.db.tables
    assert db["assistant_tasks"] == [] or all(t["status"] == "failed" for t in db["assistant_tasks"])
    assert db["bid_split_jobs"] == [] and db["bid_split_files"] == []
    assert all(r["status"] != "active" for r in db["assistant_spend_reservations"])
    assert all(r["consumed_task_id"] is None for r in db["assistant_files"])
    assert env.enqueued == [] or all(j["status"] == "canceled" for j in db["llm_jobs"])


# ── estimate ─────────────────────────────────────────────────────────────────


def _route(monkeypatch, provider="anthropic", model="claude-opus-5"):
    monkeypatch.setattr(llm, "resolve", lambda f, s=None: SimpleNamespace(provider=provider, model=model))
    monkeypatch.setattr(llm, "active_model", lambda f, s=None: model)


ESTIMATE_SETTINGS = SimpleNamespace(bid_split_triage_pages=12, bid_split_pages_per_call=8)


@pytest.mark.parametrize("pages, expected", [([1], "0.13"), ([23], "0.68"), ([100], "2.16"), ([400], "7.86")])
def test_estimate_figures_against_the_prices_rows(monkeypatch, pages, expected):
    _route(monkeypatch)
    est = split_handoff.estimate_split_usd(pages, ESTIMATE_SETTINGS)
    assert est == split_handoff.SplitEstimate(Decimal(expected), "claude-opus-5", "anthropic")


def test_estimate_is_the_worst_case_sum_times_the_margin(monkeypatch):
    from app.services import llm_pricing

    _route(monkeypatch)
    row = llm_pricing.price_for("claude-opus-5")
    tin = (12 * 2300 + 1500) + (23 * 2300 + 3 * 1500) + 3000
    tout = 300 + 3 * 900 + 800
    raw = (Decimal(tin) * row.input + Decimal(tout) * row.output) / Decimal(1_000_000) * Decimal("1.25")
    est = split_handoff.estimate_split_usd([23], ESTIMATE_SETTINGS)
    assert est.amount >= raw and est.amount - raw < Decimal("0.01")


def test_estimate_is_zero_self_hosted_and_none_unpriced(monkeypatch):
    _route(monkeypatch, provider="self_hosted", model="self-hosted:gemma")
    assert split_handoff.estimate_split_usd([400], ESTIMATE_SETTINGS).amount == Decimal("0.00")
    _route(monkeypatch, model="a-model-with-no-price-row")
    assert split_handoff.estimate_split_usd([23], ESTIMATE_SETTINGS) is None


# ── duplicates ───────────────────────────────────────────────────────────────


def test_duplicates_hash_size_matched_rows_lazily(env):
    env.db.tables["project_files"] = [
        {"id": "pf-1", "project_id": PROJECT, "filename": "Old A-101.pdf", "category": "drawing",
         "size_bytes": 1234, "sha256": None, "storage_path": "p/pf-1", "created_at": "1"},
        {"id": "pf-2", "project_id": PROJECT, "filename": "Other.pdf", "category": "other",
         "size_bytes": 999, "sha256": None, "storage_path": "p/pf-2", "created_at": "2"},
    ]
    env.hashes = {"p/pf-1": "a" * 64}
    staged = env.db.tables["assistant_files"]
    out = split_handoff.duplicates(env.db, PROJECT, staged)
    assert out == {STAGED: {"project_file_id": "pf-1", "filename": "Old A-101.pdf", "category": "drawing"}}
    assert env.db.tables["project_files"][0]["sha256"] == "a" * 64      # cached for the next read
    assert env.db.tables["project_files"][1]["sha256"] is None          # not size-matched: never hashed


def test_an_unhashed_row_past_the_budget_is_not_a_duplicate(env, monkeypatch):
    env.db.tables["project_files"] = [
        {"id": "pf-1", "project_id": PROJECT, "filename": "x.pdf", "category": "drawing", "size_bytes": 1234,
         "sha256": None, "storage_path": "p/pf-1", "created_at": "1"},
    ]

    def unreadable(path):
        raise RuntimeError("timeout")

    monkeypatch.setattr(file_hashes, "sha256_of_object", unreadable)
    assert split_handoff.duplicates(env.db, PROJECT, env.db.tables["assistant_files"]) == {}


# ── split-drop ───────────────────────────────────────────────────────────────


def test_split_drop_happy_path(env):
    out, replay = _drop(env)
    assert replay is False
    db = env.db.tables
    [task] = db["assistant_tasks"]
    [job] = db["bid_split_jobs"]
    assert task["status"] == "waiting_external" and task["split_job_id"] == job["id"]
    assert task["conversation_id"] == CONV and task["message_id"] == MSG_ASSISTANT
    assert task["approval_id"] == APPROVAL_ID and task["run_id"] == RUN and task["project_id"] == PROJECT
    assert task["title"] == f"Split 2 files into {NUMBER}" and task["kind"] == "split_into_project"
    assert task["payload"]["estimate_usd"] == "0.68" and task["payload"]["max_usd"] == "0.68"
    [hold] = db["assistant_spend_reservations"]
    assert hold["status"] == "active" and hold["kind"] == "split_task" and hold["task_id"] == task["id"]
    assert Decimal(str(hold["amount_usd"])) == EST and task["spend_reservation_id"] == hold["id"]
    assert job["source"] == "project" and job["project_id"] == PROJECT and job["created_by"] == USER
    assert job["assistant_task_id"] == task["id"] and job["spend_reservation_id"] == hold["id"]
    assert job["actor_kind"] == "assistant" and job["assistant_run_id"] == RUN
    files = db["bid_split_files"]
    assert [f["filename"] for f in files] == ["A-101.pdf", "Spec.pdf"]
    for f in files:
        assert f["job_id"] == job["id"] and f["status"] == "pending"
        assert f["assistant_run_id"] == RUN and f["spend_reservation_id"] == hold["id"]
        assert f["storage_path"] in env.store.objects          # a server-side copy
    assert [e["target_id"] for e in env.enqueued] == [f["id"] for f in files]
    for e in env.enqueued:
        assert e["job_type"] == llm_queue.JOB_BID_SPLIT and e["max_attempts_override"] == 2
        assert e["payload"] == {"file_id": e["target_id"]} and e["created_by"] == USER
    for row in db["assistant_files"]:
        assert row["consumed_task_id"] == task["id"] and row["consumed_project_id"] == PROJECT
        assert row["purged_at"] is not None and row["storage_path"] not in env.store.objects
    assert out == {
        "task_id": task["id"], "job_id": job["id"], "project_id": PROJECT,
        "files": [{"assistant_file_id": STAGED, "split_file_id": files[0]["id"], "pages": 3},
                  {"assistant_file_id": STAGED2, "split_file_id": files[1]["id"], "pages": 2}],
        "pages": 5, "estimate_usd": "0.68",
    }
    assert env.audits == [(USER, "assistant.split_started", "project", PROJECT,
                           {"task_id": task["id"], "job_id": job["id"], "files": 2, "pages": 5})]


def test_split_drop_never_uses_the_dispatch_thread_fallback(env, monkeypatch):
    from app.services import bid_split

    monkeypatch.setattr(bid_split, "dispatch", lambda *a, **k: pytest.fail("dispatch must not run"))
    _drop(env)
    assert len(env.enqueued) == 2


def test_legal_hold_keeps_the_staging_objects(env):
    env.db.tables["assistant_profiles"] = [{"user_id": USER, "legal_hold": True, "daily_cost_cap_usd": 5}]
    _drop(env)
    for row in env.db.tables["assistant_files"]:
        assert row["purged_at"] is None and row["storage_path"] in env.store.objects
        assert row["consumed_task_id"] is not None


def test_replay_returns_the_same_body_and_opens_nothing(env):
    first, _ = _drop(env)
    second, replay = _drop(env)
    assert replay is True and second == first
    assert len(env.db.tables["bid_split_jobs"]) == 1 and len(env.enqueued) == 2
    assert len(env.db.tables["assistant_spend_reservations"]) == 1


def test_a_task_without_a_job_is_never_replayed_as_executed(env):
    env.db.tables["assistant_tasks"] = [{"id": "t-0", "user_id": USER, "approval_id": APPROVAL_ID,
                                         "status": "pending", "split_job_id": None, "payload": {}}]
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env)
    assert exc.value.code == "split_stage_failed"


def test_a_compensated_task_with_a_job_is_never_reported_as_executed(env):
    """A partial compensation can leave split_job_id on a task it failed
    stage_failed: by-key answers None (404) and a replay refuses."""
    env.db.tables["assistant_tasks"] = [{"id": "t-0", "user_id": USER, "approval_id": APPROVAL_ID,
                                         "project_id": PROJECT, "status": "failed",
                                         "failure_reason": "stage_failed", "split_job_id": "job-x",
                                         "payload": {}}]
    assert split_handoff.recovered_split_drop(_user(), PROJECT, APPROVAL_ID) is None
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env)
    assert exc.value.code == "split_stage_failed"
    # A split that ran and then failed on its own is still recoverable.
    env.db.tables["assistant_tasks"][0]["failure_reason"] = "run_failed"
    assert split_handoff.recovered_split_drop(_user(), PROJECT, APPROVAL_ID)["job_id"] == "job-x"


def test_two_approvals_over_the_same_files(env):
    env.db.tables["assistant_approvals"].append(approval(id=APPROVAL2, action_kind="split_files_into_project",
                                                         status="executing"))
    _drop(env)
    holds = len(env.db.tables["assistant_spend_reservations"])
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env, approval_id=APPROVAL2)
    assert exc.value.code == "file_not_available"
    assert len(env.db.tables["bid_split_jobs"]) == 1 and len(env.db.tables["assistant_tasks"]) == 1
    assert len(env.db.tables["assistant_spend_reservations"]) == holds


def test_the_claim_race_loser_unclaims_and_holds_nothing(env, monkeypatch):
    """The second card passed step 2 before the first claimed: the fenced
    claim comes back short, what it took goes back, no hold, no job."""
    real = repo.claim_staged_files

    def racing(user_id, ids, *, task_id, project_id):
        for row in env.db.tables["assistant_files"]:
            if row["id"] == STAGED2:
                row["consumed_at"], row["consumed_task_id"] = NOW.isoformat(), "someone-else"
        return real(user_id, ids, task_id=task_id, project_id=project_id)

    monkeypatch.setattr(repo, "claim_staged_files", racing)
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env)
    assert exc.value.code == "file_not_available"
    staged = {r["id"]: r for r in env.db.tables["assistant_files"]}
    assert staged[STAGED]["consumed_task_id"] is None
    assert env.db.tables["assistant_spend_reservations"] == [] and env.db.tables["bid_split_jobs"] == []


@pytest.mark.parametrize("change", [
    {"message_id": None, "conversation_id": None},          # never sent
    {"conversation_id": CONV2},                              # sent in another conversation
    {"consumed_at": NOW.isoformat()},
    {"purged_at": NOW.isoformat()},
    {"expires_at": (NOW - timedelta(minutes=1)).isoformat()},
    {"user_id": OTHER_USER},
])
def test_file_not_available(env, change):
    env.db.tables["assistant_files"][1].update(change)
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env)
    assert exc.value.code == "file_not_available" and exc.value.status == 409
    if change.get("user_id") != OTHER_USER:
        assert exc.value.detail == {"file_ids": [STAGED2]}
    _nothing_left(env)


@pytest.mark.parametrize("ids", [[], [STAGED, STAGED], ["not-a-uuid"], [STAGED] * 11])
def test_bad_file_ids(env, ids):
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env, ids)
    assert exc.value.code == "bad_file_ids" and exc.value.status == 400
    _nothing_left(env)


def test_refusals_leave_nothing_behind(env, monkeypatch):
    cases = []

    def check(code, **detail_keys):
        with pytest.raises(split_handoff.SplitRefused) as exc:
            _drop(env)
        assert exc.value.code == code and exc.value.status == 409, exc.value.code
        for key in detail_keys:
            assert key in exc.value.detail
        _nothing_left(env)
        cases.append(code)

    env.db.tables["assistant_files"][0]["page_count"] = 399
    check("too_many_pages", pages=1)
    env.db.tables["assistant_files"][0]["page_count"] = 3

    env.locked = True
    check("handoff_locked")
    env.locked = False

    env.splitter = (False, "The Bid File Splitter is turned off on this server.")
    check("splitter_unavailable", reason=1)
    env.splitter = (True, None)

    monkeypatch.setattr(split_handoff.get_settings(), "llm_queue_enabled", False)
    check("splitter_unavailable", reason=1)
    monkeypatch.setattr(split_handoff.get_settings(), "llm_queue_enabled", True)

    env.db.tables["project_files"] = [{"id": "pf-1", "project_id": PROJECT, "filename": "Old.pdf",
                                       "category": "drawing", "size_bytes": 1234, "sha256": "a" * 64,
                                       "storage_path": "p/1", "created_at": "1"}]
    check("duplicate_file", duplicates=1)
    env.db.tables["project_files"] = []

    monkeypatch.setattr(split_handoff, "estimate_split_usd", lambda pages, s=None: None)
    check("price_unknown")
    monkeypatch.setattr(split_handoff, "estimate_split_usd",
                        lambda pages, s=None: split_handoff.SplitEstimate(Decimal("0.90"), "m", "anthropic"))
    check("price_changed", estimate_usd=1, max_usd=1)

    monkeypatch.setattr(split_handoff, "estimate_split_usd",
                        lambda pages, s=None: split_handoff.SplitEstimate(Decimal("0.68"), "m", "anthropic"))
    env.db.tables["llm_call_log"] = [{"id": "c1", "user_id": USER, "cost_usd": 14.9, "feature": "assistant",
                                      "assistant_run_id": None, "spend_reservation_id": None,
                                      "created_at": NOW.isoformat()}]
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env)
    assert exc.value.code == "spend_cap_reached"
    assert exc.value.detail == {"scope": "user", "remaining_usd": "0.10"}
    _nothing_left(env)
    assert env.db.tables["assistant_spend_reservations"] == []
    assert len(cases) == 7


def test_price_changed_detail(env, monkeypatch):
    monkeypatch.setattr(split_handoff, "estimate_split_usd",
                        lambda pages, s=None: split_handoff.SplitEstimate(Decimal("0.90"), "m", "anthropic"))
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env)
    assert exc.value.detail == {"estimate_usd": "0.90", "max_usd": "0.68"}


@pytest.mark.parametrize("where", ["copy", "task_update", "enqueue"])
def test_a_failure_runs_the_one_compensation_block(env, monkeypatch, where):
    if where == "copy":
        env.store.objects.pop(env.db.tables["assistant_files"][1]["storage_path"])
    elif where == "task_update":
        monkeypatch.setattr(repo, "update_task", lambda *a, **k: False if "split_job_id" in a[2] else True)
    else:
        env.fail_enqueue_at = 1
    with pytest.raises(split_handoff.SplitRefused) as exc:
        _drop(env)
    assert exc.value.code == "split_stage_failed"
    db = env.db.tables
    assert db["bid_split_jobs"] == [] and db["bid_split_files"] == []
    assert len(env.prefixes) == 1
    [hold] = db["assistant_spend_reservations"]
    assert hold["status"] == "released"
    if where != "task_update":
        [task] = db["assistant_tasks"]
        assert task["status"] == "failed" and task["failure_reason"] == "stage_failed"
    assert all(r["consumed_task_id"] is None and r["purged_at"] is None for r in db["assistant_files"])
    assert all(j["status"] == "canceled" for j in db["llm_jobs"])
    if where == "enqueue":
        # Canceled directly: llm_queue.cancel's domain mark would fail the
        # file and _after_failed would file it whole into the project.
        assert [j["id"] for j in db["llm_jobs"]] == ["lj-1"] and env.canceled == []
    assert db["project_files"] == []


def test_compensation_never_runs_the_queue_domain_mark(env, monkeypatch):
    """The real llm_queue.cancel marks the bid_split file failed, which files
    it whole; the compensation must not go through it."""
    monkeypatch.setattr(llm_queue, "cancel", lambda job_id: pytest.fail("domain mark would file the PDF whole"))
    env.fail_enqueue_at = 1
    with pytest.raises(split_handoff.SplitRefused):
        _drop(env)
    assert env.db.tables["llm_jobs"][0]["status"] == "canceled"
    assert env.db.tables["project_files"] == []


def test_a_failing_compensation_reraises_the_original_error(env, monkeypatch):
    env.fail_enqueue_at = 0

    def broken(job_id):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "delete_bid_split_prefix", broken)
    with pytest.raises(RuntimeError, match="queue down"):
        _drop(env)
    assert env.db.tables["assistant_spend_reservations"][0]["status"] == "released"


# ── split_drop_out and the card helpers ──────────────────────────────────────


def test_split_drop_out_feeds_every_body_the_same(env):
    out, _ = _drop(env)
    [task] = env.db.tables["assistant_tasks"]
    assert split_handoff.split_drop_out(task) == out
    assert split_handoff.recovered_split_drop(_user(), PROJECT, APPROVAL_ID) == out
    assert split_handoff.recovered_split_drop(_user(), "other-project", APPROVAL_ID) is None
    assert set(out) == {"task_id", "job_id", "project_id", "files", "pages", "estimate_usd"}
    assert isinstance(out["estimate_usd"], str)
    task["status"] = "pending"
    assert split_handoff.recovered_split_drop(_user(), PROJECT, APPROVAL_ID) is None


def test_split_preview_reports_every_problem(env, monkeypatch):
    other = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"
    unsent = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4"
    in_card = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5"
    missing = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6"
    env.db.tables["assistant_files"] += [
        _linked(id=other, conversation_id=CONV2, storage_path="o", sha256="c" * 64),
        staged_file(id=unsent, storage_path="u", sha256="d" * 64),
        _linked(id=in_card, storage_path="i", sha256="e" * 64),
    ]
    env.db.tables["assistant_files"][1]["consumed_at"] = NOW.isoformat()
    env.db.tables["assistant_approvals"].append(approval(
        id=APPROVAL2, action_kind="split_files_into_project", status="pending",
        payload={"v": 1, "args": {}, "request": {"json": {"file_ids": [in_card]}}}))
    env.db.tables["assistant_tool_calls"] = [{"id": "tc", "user_id": USER, "run_id": RUN,
                                              "conversation_id": CONV}]
    with provenance.acting_as_assistant(USER, RUN):
        out = split_handoff.split_preview(env.db, PROJECT, _user(),
                                          [STAGED, STAGED2, other, unsent, in_card, missing])
    problems = {f["file_id"]: f["problem"] for f in out["files"]}
    assert problems == {STAGED: None, STAGED2: "consumed", other: "other_conversation", unsent: "not_attached",
                        in_card: "in_open_card", missing: "not_found"}
    assert out["project"] == {"id": PROJECT, "number": NUMBER, "name": "Monument"}
    assert out["limits"] == {"max_files": 10, "max_pages": 400}
    assert out["estimate"] == {"usd": "0.68", "model": "claude-opus-5", "provider": "anthropic", "priced": True}
    assert out["spend"] == {"user_remaining_usd": "15.00", "global_remaining_usd": "50.00"}
    assert out["splitter"] == {"available": True, "reason": None} and out["handoff_locked"] is False
    assert out["totals"]["files"] == 6
    with pytest.raises(split_handoff.SplitRefused) as exc:
        split_handoff.split_preview(env.db, PROJECT, _user(), [STAGED] * 11)
    assert exc.value.code == "bad_file_ids"


def test_split_preview_without_a_turn_does_not_check_the_conversation(env):
    env.db.tables["assistant_files"][1]["conversation_id"] = CONV2
    out = split_handoff.split_preview(env.db, PROJECT, _user(run=None), [STAGED, STAGED2])
    assert [f["problem"] for f in out["files"]] == [None, None]


# ── outcomes ─────────────────────────────────────────────────────────────────


JOB = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1"


def _file(fid, status="done", minutes_ago=30, **over):
    row = {"id": fid, "job_id": JOB, "filename": f"{fid}.pdf", "status": status, "page_count": 4,
           "finished_at": (NOW - timedelta(minutes=minutes_ago)).isoformat() if status in ("done", "failed") else None,
           "updated_at": None, "created_at": f"2026-10-22T00:00:0{len(fid) % 10}+00:00", "error": None,
           "file_kind": None, "size_bytes": 100}
    row.update(over)
    return row


def _row(fid, *, seg=None, source_set=False, note=None, category="drawing"):
    return {"id": f"pf-{fid}-{seg}-{source_set}", "project_id": PROJECT, "bid_split_file_id": fid,
            "bid_split_segment_id": seg, "is_source_set": source_set, "note": note, "category": category}


def _outcome_db(files, rows, segments=(), status="done"):
    return FakeDB(
        bid_split_jobs=[{"id": JOB, "status": status, "project_id": PROJECT, "source": "project"}],
        bid_split_files=files, project_files=rows,
        bid_split_segments=[{"id": f"s-{i}", "file_id": fid} for i, fid in enumerate(segments)],
        projects=[{"id": PROJECT, "number": NUMBER, "name": "Monument"}],
    )


def test_split_and_intact_files(env):
    rows = [_row("f1", seg="s1", category="electrical_drawing"), _row("f1", seg="s2", category="drawing"),
            _row("f1", seg="s3", category="electrical_drawing"), _row("f1", source_set=True, category="other"),
            _row("f2", seg="s4", category="specification")]
    db = _outcome_db([_file("f1"), _file("f2")], rows, segments=["f1", "f1", "f1", "f2"])
    out = split_handoff.job_outcome(db, db.tables["bid_split_jobs"][0])
    by = {f["split_file_id"]: f for f in out["files"]}
    assert by["f1"]["outcome"] == "split" and by["f1"]["documents"] == 3
    assert by["f1"]["categories"] == {"electrical_drawing": 2, "drawing": 1}
    assert by["f2"]["outcome"] == "split" and by["f2"]["documents"] == 1        # intact: filed by its kind
    assert out["ready"] is True and out["split"] == 2 and out["documents"] == 4
    assert out["job"] == {"id": JOB, "status": "done", "file_count": 2, "files_done": 2, "files_failed": 0}


def test_the_whole_file_reasons_come_from_structured_state(env):
    nss = rfp_split.MSG_WHOLE_NOT_SPLIT_SENT.format(reason="the run failed")
    ns = rfp_split.MSG_WHOLE_NOT_SPLIT
    files = [_file("a"), _file("b"), _file("c", status="failed", error="Parse (page 3) failed (bad)"),
             _file("d"), _file("e"), _file("f"), _file("g")]
    rows = [
        _row("a", note=rfp_split.MSG_WHOLE_SENT, category="additional"),
        _row("b", note=nss, category="additional"),
        # A failed run passes the splitter's own error text, parentheses included.
        _row("c", note=ns.format(reason="parse (page 3) failed (bad)")),
        _row("d", note=ns.format(reason=rfp_split._WHY_REFILE_FAILED)),
        _row("e", note=ns.format(reason=rfp_split._WHY_NO_DOCUMENTS)),
        _row("f", note=ns.format(reason="something else")),
        _row("g", note="A note a person wrote"),
    ]
    db = _outcome_db(files, rows, segments=["d", "f", "g"])
    out = split_handoff.job_outcome(db, db.tables["bid_split_jobs"][0])
    reasons = {f["split_file_id"]: (f["outcome"], f["reason"]) for f in out["files"]}
    assert reasons == {
        "a": ("whole", "package_sent"), "b": ("whole", "package_sent"), "c": ("whole", "run_failed"),
        "d": ("whole", "refile_failed"), "e": ("whole", "no_documents"), "f": ("whole", "other"),
        "g": ("whole", "other"),
    }


def test_the_rfp_split_templates_the_classifier_reads_are_pinned():
    assert rfp_split.MSG_WHOLE_SENT.startswith("The Bid File Splitter split this file, but the package")
    assert rfp_split.MSG_WHOLE_NOT_SPLIT.split("{reason}")[0] == "The Bid File Splitter could not split this file ("
    assert rfp_split.MSG_WHOLE_NOT_SPLIT_SENT.split("{reason}")[0] == rfp_split.MSG_WHOLE_NOT_SPLIT.split("{reason}")[0]
    assert rfp_split.MSG_WHOLE_NOT_SPLIT_SENT.split("{reason}")[1].endswith("package had already been sent.")
    assert rfp_split._WHY_REFILE_FAILED == "its documents could not be filed"
    assert rfp_split._WHY_NO_DOCUMENTS == "it found no documents in it"
    assert rfp_split._WHY_RUN_FAILED == "the run failed"


def test_failed_landing_missing_and_pending(env):
    files = [_file("gone", status="failed", minutes_ago=30), _file("late", status="done", minutes_ago=2),
             _file("dead", status="done", minutes_ago=30), _file("run", status="running")]
    db = _outcome_db(files, [], status="processing")
    out = split_handoff.job_outcome(db, db.tables["bid_split_jobs"][0])
    outcomes = {f["split_file_id"]: f["outcome"] for f in out["files"]}
    assert outcomes == {"gone": "failed", "late": "pending", "dead": "missing", "run": "pending"}
    assert out["ready"] is False
    # Settled job, one file still landing: not ready.
    db = _outcome_db([_file("late", minutes_ago=2)], [], status="done")
    assert split_handoff.job_outcome(db, db.tables["bid_split_jobs"][0])["ready"] is False
    # Past the grace it is missing, and the task is ready.
    db = _outcome_db([_file("late", minutes_ago=16)], [], status="done")
    out = split_handoff.job_outcome(db, db.tables["bid_split_jobs"][0])
    assert out["files"][0]["outcome"] == "missing" and out["ready"] is True


def test_task_progress_counts_only_the_staged_files(env):
    files = [_file("f1"), _file("human", status="running")]
    db = _outcome_db(files, [_row("f1", seg="s1")], segments=["f1"])
    task = {"id": "t1", "status": "waiting_external", "project_id": PROJECT, "split_job_id": JOB,
            "payload": {"files": [{"assistant_file_id": STAGED, "split_file_id": "f1", "pages": 4}]}}
    out = split_handoff.task_progress(db, task)
    assert [f["split_file_id"] for f in out["files"]] == ["f1"]
    assert out["ready"] is True and out["job"]["file_count"] == 1
    assert out["task"]["project"] == {"id": PROJECT, "number": NUMBER} == out["project"]
    assert set(out["files"][0]) == {"split_file_id", "filename", "status", "outcome", "documents", "categories",
                                    "reason"}


def test_task_progress_for_a_gone_job_and_no_job(env):
    db = _outcome_db([], [])
    db.tables["bid_split_jobs"] = []
    out = split_handoff.task_progress(db, {"id": "t1", "project_id": PROJECT, "split_job_id": JOB, "payload": {}})
    assert out["job"] is None and out["ready"] is True
    out = split_handoff.task_progress(db, {"id": "t1", "project_id": PROJECT, "split_job_id": None})
    assert out["job"] is None and out["ready"] is False


def test_task_progress_for_creates_its_own_client(env):
    env.db = _outcome_db([_file("f1")], [_row("f1", seg="s1")], segments=["f1"])
    out = split_handoff.task_progress_for({"id": "t1", "project_id": PROJECT, "split_job_id": JOB, "payload": {}})
    assert out["files"][0]["outcome"] == "split"
    assert split_handoff.job_outcome_for(JOB)["split"] == 1


# ── attach ───────────────────────────────────────────────────────────────────


def _manual(env, *, files=None, **over):
    job = {"id": MANUAL_JOB, "status": "done_with_errors", "source": "manual", "project_id": None,
           "created_by": USER, "created_at": NOW.isoformat(), "assistant_task_id": None}
    job.update(over)
    env.db.tables["bid_split_jobs"] = [job]
    env.db.tables["bid_split_files"] = files if files is not None else [
        {"id": "m1", "job_id": MANUAL_JOB, "filename": "Set.pdf", "status": "done", "page_count": 20,
         "size_bytes": 100, "file_kind": "drawing_set", "created_at": "1",
         "finished_at": NOW.isoformat()},
        {"id": "m2", "job_id": MANUAL_JOB, "filename": "Bad.pdf", "status": "failed", "page_count": 2,
         "size_bytes": 100, "file_kind": None, "created_at": "2", "finished_at": NOW.isoformat()},
    ]
    return job


def test_attach_links_a_manual_job_and_files_its_done_files(env, monkeypatch):
    _manual(env)
    resynced = []
    monkeypatch.setattr(rfp_split, "resync_after_run", lambda fid: resynced.append(fid))
    with provenance.acting_as_assistant(USER, RUN, APPROVAL_ID):
        out, replay = split_handoff.attach(env.db, PROJECT, _user(), MANUAL_JOB, idempotency_key=APPROVAL_ID)
    assert replay is False and resynced == ["m1"]                 # the failed file is not filed
    job = env.db.tables["bid_split_jobs"][0]
    assert job["project_id"] == PROJECT and job["source"] == "project"
    assert out["job_id"] == MANUAL_JOB and {"split", "whole", "documents", "files"} <= set(out)
    assert env.audits == [(USER, "assistant.split_attached", "project", PROJECT,
                           {"job_id": MANUAL_JOB, "files": 1, "approval_id": APPROVAL_ID})]
    # A replay re-runs the reconcile (idempotent) and says so.
    out2, replay = split_handoff.attach(env.db, PROJECT, _user(), MANUAL_JOB, idempotency_key=APPROVAL_ID)
    assert replay is True and resynced == ["m1", "m1"]


@pytest.mark.parametrize("over, files, code", [
    ({"created_by": OTHER_USER}, None, "job_not_found"),
    ({"source": "project", "project_id": "elsewhere"}, None, "job_already_linked"),
    ({"source": "rfp"}, None, "source_not_attachable"),
    ({"source": "plans_reader"}, None, "source_not_attachable"),
    ({"source": "project", "project_id": PROJECT, "assistant_task_id": "t"}, None, "source_not_attachable"),
    ({"status": "processing"}, None, "job_not_finished"),
    ({}, [{"id": "x", "job_id": MANUAL_JOB, "status": "failed", "size_bytes": 1, "created_at": "1"}],
     "nothing_to_file"),
    ({}, [{"id": f"x{i}", "job_id": MANUAL_JOB, "status": "done", "size_bytes": 1, "created_at": str(i)}
          for i in range(11)], "job_too_large"),
    ({}, [{"id": "x", "job_id": MANUAL_JOB, "status": "done", "size_bytes": 1536 * 1024 * 1024 + 1,
           "created_at": "1"}], "job_too_large"),
])
def test_attach_refusals(env, monkeypatch, over, files, code):
    _manual(env, files=files, **over)
    monkeypatch.setattr(rfp_split, "resync_after_run", lambda fid: pytest.fail("nothing is filed"))
    with pytest.raises(split_handoff.SplitRefused) as exc:
        split_handoff.attach(env.db, PROJECT, _user(), MANUAL_JOB, idempotency_key=APPROVAL_ID)
    assert exc.value.code == code and exc.value.status == 409
    assert env.db.tables["bid_split_jobs"][0].get("project_id") == over.get("project_id")


def test_attach_refuses_a_locked_project_and_a_missing_job(env, monkeypatch):
    _manual(env)
    env.locked = True
    with pytest.raises(split_handoff.SplitRefused) as exc:
        split_handoff.attach(env.db, PROJECT, _user(), MANUAL_JOB, idempotency_key=APPROVAL_ID)
    assert exc.value.code == "handoff_locked" and env.db.tables["bid_split_jobs"][0]["project_id"] is None
    with pytest.raises(split_handoff.SplitRefused) as exc:
        split_handoff.attach(env.db, PROJECT, _user(), "dddddddd-dddd-4ddd-8ddd-dddddddddd00", idempotency_key="k")
    assert exc.value.code == "job_not_found"


def test_attach_lost_cas_to_another_project(env, monkeypatch):
    _manual(env)
    real_table = env.db.table

    def racing(name):
        q = real_table(name)
        if name == "bid_split_jobs":
            orig = q.update

            def update(payload):
                env.db.tables["bid_split_jobs"][0].update(project_id="elsewhere", source="project")
                return orig(payload)

            q.update = update
        return q

    monkeypatch.setattr(env.db, "table", racing)
    with pytest.raises(split_handoff.SplitRefused) as exc:
        split_handoff.attach(env.db, PROJECT, _user(), MANUAL_JOB, idempotency_key=APPROVAL_ID)
    assert exc.value.code == "job_already_linked"


def test_attach_preview_and_the_recovery_read(env):
    _manual(env)
    out = split_handoff.attach_preview(env.db, PROJECT, _user(), MANUAL_JOB)
    assert out["problem"] is None and out["handoff_locked"] is False
    assert out["job"]["own"] is True and out["job"]["files_done"] == 1 and out["job"]["files_failed"] == 1
    assert out["job"]["source_bytes"] == 100 and out["job"]["linked_project_id"] is None
    assert out["files"][0] == {"filename": "Set.pdf", "status": "done", "pages": 20, "kind": "drawing_set"}
    assert split_handoff.attached_outcome(env.db, PROJECT, _user(), MANUAL_JOB) is None
    hidden = split_handoff.attach_preview(env.db, PROJECT, _user(OTHER_USER), MANUAL_JOB)
    assert hidden["job"] is None and hidden["files"] == [] and hidden["problem"] == "job_not_found"
    env.db.tables["bid_split_jobs"][0].update(project_id=PROJECT, source="project")
    # Linked but the done file never re-filed (an attach that died after its
    # CAS): not reported as executed.
    assert split_handoff.attached_outcome(env.db, PROJECT, _user(), MANUAL_JOB) is None
    env.db.tables["project_files"] = [{"id": "pf-m1", "project_id": PROJECT, "bid_split_file_id": "m1",
                                       "bid_split_segment_id": None, "is_source_set": False, "note": None,
                                       "category": "drawing"}]
    assert split_handoff.attached_outcome(env.db, PROJECT, _user(), MANUAL_JOB)["job_id"] == MANUAL_JOB
    assert split_handoff.attached_outcome(env.db, PROJECT, _user(OTHER_USER), MANUAL_JOB) is None


def test_attach_refiles_when_the_cas_commits_but_its_response_is_lost(env, monkeypatch):
    _manual(env)
    resynced = []
    monkeypatch.setattr(rfp_split, "resync_after_run", lambda fid: resynced.append(fid))
    real_table = env.db.table

    def lossy(name):
        q = real_table(name)
        if name == "bid_split_jobs":

            def update(payload):
                env.db.tables["bid_split_jobs"][0].update(payload)

                class Lost:
                    def __getattr__(self, attr):
                        return lambda *a, **k: self

                    def execute(self):
                        raise RuntimeError("connection reset")

                return Lost()

            q.update = update
        return q

    monkeypatch.setattr(env.db, "table", lossy)
    out, replay = split_handoff.attach(env.db, PROJECT, _user(), MANUAL_JOB, idempotency_key=APPROVAL_ID)
    assert resynced == ["m1"] and out["job_id"] == MANUAL_JOB and replay is False


# ── status ───────────────────────────────────────────────────────────────────


def test_status_for_lists_the_turns_staged_files_jobs_and_tasks(env):
    env.db.tables["assistant_files"].append(_linked(id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa7", conversation_id=CONV2,
                                                    storage_path="x"))
    env.db.tables["assistant_tool_calls"] = [{"id": "tc", "user_id": USER, "run_id": RUN, "conversation_id": CONV}]
    _manual(env, status="done")
    env.db.tables["bid_split_jobs"].append({"id": "old", "status": "done", "source": "manual", "project_id": None,
                                            "created_by": USER, "created_at": (NOW - timedelta(days=20)).isoformat()})
    env.db.tables["assistant_tasks"] = [{"id": "t1", "user_id": USER, "status": "done", "project_id": PROJECT,
                                         "payload": {"file_ids": [STAGED]}, "created_at": NOW.isoformat(),
                                         "failure_reason": None}]
    with provenance.acting_as_assistant(USER, RUN):
        out = split_handoff.status_for(_user(), PROJECT)
    assert [s["file_id"] for s in out["staged"]] == [STAGED2, STAGED]     # newest first, id breaks the tie
    assert {s["staged_filename"] for s in out["staged"]} == {"A-101.pdf", "Spec.pdf"}
    assert [j["job_id"] for j in out["jobs"]] == [MANUAL_JOB]
    assert out["jobs"][0]["job_first_filename"] == "Set.pdf" and out["jobs"][0]["files_done"] == 1
    assert out["tasks"] == [{"task_id": "t1", "status": "done", "project_id": PROJECT, "project_number": NUMBER,
                             "files": 1, "failure_reason": None, "created_at": NOW.isoformat()}]
    assert out["project"] == {"id": PROJECT, "number": NUMBER, "handoff_locked": False}
    assert out["limits"] == {"max_files_per_card": 10, "max_pages_per_card": 400}
    assert out["spend"] == {"user_remaining_usd": "15.00", "global_remaining_usd": "50.00"}
    from app.assistant.schemas import SplitStatusOut

    SplitStatusOut(**out)


def test_status_for_a_human_has_no_staged_list_and_a_missing_project_is_404(env):
    out = split_handoff.status_for(_user(run=None), None)
    assert out["staged"] == [] and out["project"] is None
    with pytest.raises(split_handoff.SplitRefused) as exc:
        split_handoff.status_for(_user(), "77777777-7777-4777-8777-000000000000")
    assert exc.value.code == "project_not_found" and exc.value.status == 404


def test_status_for_reads_staged_files_by_conversation_not_by_message_list(env, monkeypatch):
    """A long conversation never hides its newest files: the staged list
    comes from the files' own conversation_id, newest first, capped."""
    from app.assistant import repo

    def no_message_list(*a, **k):
        raise AssertionError("status_for must not page through messages")

    monkeypatch.setattr(repo, "list_messages", no_message_list)
    env.db.tables["assistant_tool_calls"] = [{"id": "tc", "user_id": USER, "run_id": RUN, "conversation_id": CONV}]
    extra = [
        _linked(id=f"aaaaaaaa-aaaa-4aaa-8aaa-{i:012d}", storage_path=f"s{i}", sha256=f"{i:064d}",
                message_id=f"m-{i}", created_at=(NOW + timedelta(minutes=i)).isoformat())
        for i in range(1, split_handoff.STATUS_MAX_STAGED + 3)
    ]
    env.db.tables["assistant_files"] += extra
    with provenance.acting_as_assistant(USER, RUN):
        out = split_handoff.status_for(_user(), None)
    ids = [s["file_id"] for s in out["staged"]]
    assert len(ids) == split_handoff.STATUS_MAX_STAGED
    assert ids[0] == extra[-1]["id"]                    # the newest is listed first
    assert STAGED not in ids and STAGED2 not in ids     # the oldest fall off the cap


# ── the recording fake: written tables vs writes_tables ──────────────────────


class Recording:
    """Wraps a fake client and records every table a write touches."""

    RPC_TABLES = {"assistant_spend_reserve": "assistant_spend_reservations"}

    def __init__(self, db):
        self.db = db
        self.written: set[str] = set()

    def table(self, name):
        query = self.db.table(name)
        for op in ("insert", "update", "upsert", "delete"):
            orig = getattr(query, op)

            def wrapped(*a, _orig=orig, **k):
                self.written.add(name)
                return _orig(*a, **k)

            setattr(query, op, wrapped)
        return query

    def rpc(self, name, params=None):
        if name in self.RPC_TABLES:
            self.written.add(self.RPC_TABLES[name])
        return self.db.rpc(name, params)

    def __getattr__(self, attr):
        return getattr(self.db, attr)


def test_split_drop_writes_only_its_tables(env, monkeypatch):
    rec = Recording(env.db)
    env.db = rec
    real_enqueue = llm_queue.enqueue
    monkeypatch.setattr(llm_queue, "enqueue", lambda jt, **kw: (rec.written.add("llm_jobs"), real_enqueue(jt, **kw))[1])
    monkeypatch.setattr(split_handoff, "audit", lambda *a, **k: rec.written.add("audit_log"))
    with provenance.acting_as_assistant(USER, RUN, APPROVAL_ID):
        split_handoff.split_drop(rec, PROJECT, _user(), [STAGED, STAGED2], approval_id=APPROVAL_ID, run_id=RUN,
                                 max_usd=EST)
    assert rec.written <= set(SPLIT_WRITES_TABLES), rec.written - set(SPLIT_WRITES_TABLES)
    assert {"assistant_tasks", "assistant_files", "bid_split_jobs", "bid_split_files", "llm_jobs",
            "assistant_spend_reservations"} <= rec.written


def _resync_recording(monkeypatch, whole: bool):
    """rfp_split.resync_after_run over the RFP fake (tests/test_rfp_split_project_jobs)
    with a flagged project and an open file item: segments land, the flag
    clears, the item resolves; or the file is filed whole."""
    from app.services import files_needed, office_preview
    from app.services import rfp_file_issues as rfi
    from tests.test_rfp_email_ingest import FakeDB as RfpFakeDB

    job, fid = "6d000000-0000-4000-8000-000000000001", "7e000000-0000-4000-8000-000000000001"
    source = f"bid-splits/{job}/source/abc-Bid Set.pdf"
    segments = [] if whole else [
        {"id": "s-e", "file_id": fid, "sort_order": 0, "category": "electrical_drawings", "other_type": None,
         "name": "E-Sheets", "description": "E.", "page_start": 1, "page_end": 1,
         "storage_path": f"bid-splits/{job}/output/s-e.pdf", "filename": "E.pdf", "size_bytes": 10,
         "is_original": False},
        {"id": "s-g", "file_id": fid, "sort_order": 1, "category": "general_drawings", "other_type": None,
         "name": "Cover", "description": "C.", "page_start": 2, "page_end": 2,
         "storage_path": f"bid-splits/{job}/output/s-g.pdf", "filename": "C.pdf", "size_bytes": 10,
         "is_original": False},
    ]
    db = RfpFakeDB({
        "bid_split_jobs": [{"id": job, "status": "done", "source": "project", "project_id": PROJECT,
                            "rfp_harvest_id": None, "created_by": USER}],
        "bid_split_files": [{"id": fid, "job_id": job, "filename": "Bid Set.pdf", "storage_path": source,
                             "size_bytes": 22, "page_count": 2, "status": "done", "rfp_sandbox_file_id": None,
                             "source_format": None, "classified_from": None}],
        "bid_split_segments": segments,
        "project_files": [],
        "rfp_created_projects": [{"project_id": PROJECT, "harvest_id": None, "files_issue_total": False,
                                  "files_promoted": 0}],
        "rfp_file_issues": [{"id": "i-1", "project_id": PROJECT, "kind": "file", "group": "not_downloaded",
                             "name": "Bid Set.pdf", "status": "download_failed", "item_key": "k1"}],
        "projects": [{"id": PROJECT, "files_needed_set_at": NOW.isoformat(), "files_needed_cleared_at": None}],
    })
    rec = Recording(db)
    from app.routers import files as files_router

    monkeypatch.setattr(supabase_client, "get_supabase", lambda: rec)
    monkeypatch.setattr(files_router, "handoff_locked", lambda pid: False)
    for mod in (rfp_split, rfi, files_needed):
        monkeypatch.setattr(mod, "audit", lambda *a, **k: rec.written.add("audit_log"))
    monkeypatch.setattr(office_preview, "is_convertible", lambda *a, **k: False)
    monkeypatch.setattr(storage, "copy_file", lambda src, dst: None)
    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(storage, "delete_file", lambda *a, **k: None)
    monkeypatch.setattr(storage, "download_file", lambda path: b"%PDF-the dropped bytes")
    rfp_split.resync_after_run(fid)
    assert db.tables["project_files"], "the resync filed nothing"
    return rec.written


@pytest.mark.parametrize("whole", [False, True])
def test_resync_after_run_writes_only_the_tools_tables(monkeypatch, whole):
    written = _resync_recording(monkeypatch, whole)
    assert "project_files" in written
    assert written <= set(SPLIT_WRITES_TABLES), written - set(SPLIT_WRITES_TABLES)
    assert written <= set(ATTACH_WRITES_TABLES), written - set(ATTACH_WRITES_TABLES)


def test_attach_writes_only_its_tables(env, monkeypatch):
    _manual(env)
    rec = Recording(env.db)
    env.db = rec
    monkeypatch.setattr(split_handoff, "audit", lambda *a, **k: rec.written.add("audit_log"))
    monkeypatch.setattr(rfp_split, "resync_after_run", lambda fid: None)    # recorded on its own above
    with provenance.acting_as_assistant(USER, RUN, APPROVAL_ID):
        split_handoff.attach(rec, PROJECT, _user(), MANUAL_JOB, idempotency_key=APPROVAL_ID)
    assert rec.written == {"bid_split_jobs", "audit_log"}
    assert rec.written <= set(ATTACH_WRITES_TABLES)


def test_the_module_never_names_an_assistant_table_or_an_em_dash():
    import inspect

    source = inspect.getsource(split_handoff)
    assert ".table(\"assistant_" not in source and ".table('assistant_" not in source
    assert "\u2014" not in source
