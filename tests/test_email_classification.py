"""PM email classification (services/email_classification, routers/emails):
label validation, prompt context, the trigger on every assignment path (PM
projects only), corrections, permissions and the self-hosted-only route.

The model is always stubbed; the database is an in-memory fake that supports
the query surface the service and the email filer use.
"""

from types import SimpleNamespace

import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import emails as emails_router
from app.services import email_classification as ec
from app.services import email_ingest, llm, llm_queue, llm_target


# ── Fake Supabase ────────────────────────────────────────────────────────────

_KEYS = {"email_classifications": "email_id"}


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.on_conflict = "select", None, None
        self.filters: list = []
        self._negate = False
        self._limit = None

    def select(self, *a, **k):
        self.op = "select" if self.op == "select" else self.op
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, **k):
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict
        return self

    def delete(self):
        self.op = "delete"
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def _add(self, fn):
        neg, self._negate = self._negate, False
        self.filters.append((lambda r: not fn(r)) if neg else fn)
        return self

    def eq(self, c, v):
        return self._add(lambda r: r.get(c) == v)

    def neq(self, c, v):
        return self._add(lambda r: r.get(c) != v)

    def is_(self, c, v):
        return self._add(lambda r: r.get(c) is None)

    def in_(self, c, vs):
        vs = list(vs)
        return self._add(lambda r: r.get(c) in vs)

    def gte(self, c, v):
        return self._add(lambda r: r.get(c) is not None and r[c] >= v)

    def lte(self, c, v):
        return self._add(lambda r: r.get(c) is not None and r[c] <= v)

    def or_(self, expr):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    def range(self, *a, **k):
        return self

    def single(self):
        return self

    def _rows(self):
        return self.db.tables.setdefault(self.table, [])

    def execute(self):
        rows = self._rows()
        match = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "select":
            data = [dict(r) for r in match]
            if self._limit is not None:
                data = data[: self._limit]
            return SimpleNamespace(data=data, count=len(data))
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for item in items:
                row = {"id": f"{self.table}-{len(rows) + 1}", **item}
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self.op == "upsert":
            key = self.on_conflict or _KEYS.get(self.table, "id")
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for item in items:
                existing = next((r for r in rows if r.get(key) == item.get(key)), None)
                if existing is None:
                    existing = dict(item)
                    rows.append(existing)
                else:
                    existing.update(item)
                out.append(dict(existing))
            return SimpleNamespace(data=out)
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in match])
        if self.op == "delete":
            for r in match:
                rows.remove(r)
            return SimpleNamespace(data=[dict(r) for r in match])
        raise AssertionError(self.op)


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def table(self, name):
        return _Query(self, name)

    def rows(self, name):
        return self.tables.setdefault(name, [])


PM = "11111111-1111-1111-1111-111111111111"
PM2 = "22222222-2222-2222-2222-222222222222"
BID = "33333333-3333-3333-3333-333333333333"
E1 = "aaaaaaaa-0000-0000-0000-000000000001"
E2 = "aaaaaaaa-0000-0000-0000-000000000002"
E3 = "aaaaaaaa-0000-0000-0000-000000000003"


def _email(eid, *, project=None, status="processed", conv="conv-1", at="2026-10-01T16:00:00+00:00", **kw):
    return {
        "id": eid,
        "mailbox": "pm@g3electrical.com",
        "folder": "inbox",
        "direction": "inbound",
        "graph_message_id": f"g-{eid}",
        "internet_message_id": f"<{eid}@x>",
        "conversation_id": conv,
        "project_id": project,
        "status": status,
        "from_name": "Jane GC",
        "from_address": "jane@gc.com",
        "to_recipients": [{"name": "Tom", "address": "tom@g3electrical.com"}],
        "cc_recipients": [],
        "subject": "RFI 12 conduit routing",
        "body_preview": "preview",
        "body_text": "Please answer RFI 12 about conduit routing.",
        "message_at": at,
        "created_at": at,
        "attempts": 0,
        "has_attachments": False,
        **kw,
    }


def _settings(**over) -> Settings:
    base = dict(
        _env_file=None,
        email_classify_enabled=True,
        self_hosted_llm_local_base_url="http://localhost:8002/v1",
        self_hosted_llm_local_model="qwen-3.8-27b",
        openai_api_key="3p-key-must-never-be-used",
        anthropic_api_key="3p-key-must-never-be-used",
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    db.rows("projects").extend(
        [{"id": PM, "pm_stage": "precon"}, {"id": PM2, "pm_stage": "construction"}, {"id": BID, "pm_stage": None}]
    )
    db.rows("profiles").append({"id": "user-1", "full_name": "Pat Writer"})
    s = _settings()
    enqueued: list[dict] = []

    def fake_enqueue(job_type, **kw):
        enqueued.append({"job_type": job_type, **kw})
        return {"id": f"job-{len(enqueued)}", "status": "queued"}

    active: dict[str, dict] = {}
    monkeypatch.setattr(llm_queue, "enqueue", fake_enqueue)
    monkeypatch.setattr(llm_queue, "active_job", lambda job_type, target_id: active.get(str(target_id)))
    monkeypatch.setattr(ec, "get_supabase", lambda: db)
    monkeypatch.setattr(ec, "get_settings", lambda: s)
    monkeypatch.setattr(email_ingest, "get_settings", lambda: s)
    monkeypatch.setattr(email_ingest, "audit", lambda *a, **k: None)
    llm_target.reset()
    return SimpleNamespace(db=db, s=s, enqueued=enqueued, active=active)


def _answer(labels=None, summary="Asks for an RFI answer.", thread="Jane asked about conduit."):
    return {
        "labels": labels
        if labels is not None
        else [{"category": "rfis", "subcategory": "rfi_question", "title": "", "confidence": 0.93}],
        "email_summary": summary,
        "thread_summary": thread,
    }


# ── Label validation ─────────────────────────────────────────────────────────


def test_ai_labels_multi_label_and_coercion():
    labels = ec.normalize_ai_labels(
        [
            {"category": "rfis", "subcategory": "rfi_response", "title": "", "confidence": 0.7},
            {"category": "change_management", "subcategory": "nonsense", "title": "x", "confidence": 3},
            {"category": "materials_submittals", "subcategory": "pay_application", "title": "", "confidence": -1},
            {"category": "bogus", "subcategory": "none", "title": "", "confidence": 1},
            {"category": "other", "subcategory": "none", "title": "  Schedule update. ", "confidence": 0.5},
        ]
    )
    by_cat = {lab["category"]: lab for lab in labels}
    assert set(by_cat) == {"rfis", "change_management", "materials_submittals", "other"}
    assert by_cat["change_management"] == {
        "category": "change_management", "subcategory": None, "title": "x", "confidence": 1.0,
    }
    # A subcategory from another category falls back to the catch-all.
    assert by_cat["materials_submittals"]["subcategory"] == "submittal_general"
    assert by_cat["materials_submittals"]["confidence"] == 0.0
    assert by_cat["other"]["title"] == "Schedule update"
    assert by_cat["other"]["subcategory"] is None
    assert labels[0]["confidence"] >= labels[-1]["confidence"]


def test_other_requires_a_title_and_empty_output_is_bad_output():
    with pytest.raises(llm.LlmBadOutput):
        ec.normalize_ai_labels([{"category": "other", "subcategory": "none", "title": "", "confidence": 0.9}])
    with pytest.raises(llm.LlmBadOutput):
        ec.normalize_ai_labels([{"category": "nope"}])
    with pytest.raises(llm.LlmBadOutput):
        ec.normalize_ai_labels("not a list")


def test_user_labels_are_strict():
    ok = ec.normalize_user_labels(
        [{"category": "financials", "subcategory": "invoice", "title": "ignored"},
         {"category": "other", "subcategory": None, "title": "Site meeting"}]
    )
    assert ok == [
        {"category": "financials", "subcategory": "invoice", "title": None, "confidence": 1.0},
        {"category": "other", "subcategory": None, "title": "Site meeting", "confidence": 1.0},
    ]
    for bad in (
        [],
        [{"category": "rfis", "subcategory": "invoice"}],
        [{"category": "other", "title": ""}],
        [{"category": "made_up"}],
    ):
        with pytest.raises(ec.InvalidLabels):
            ec.normalize_user_labels(bad)


def test_effective_labels_prefers_the_user():
    row = {"ai_labels": [{"category": "rfis"}], "user_labels": None}
    assert ec.effective_labels(row) == [{"category": "rfis"}]
    row["user_labels"] = [{"category": "financials"}]
    assert ec.main_categories(row) == ["financials"]


# ── Context building ─────────────────────────────────────────────────────────


def test_split_quoted_markers():
    new, quoted = ec.split_quoted("Approved, see attached.\n\nOn Mon, Oct 5, 2026 at 9:14 AM Jane <j@x.com>\nwrote:\n> old text")
    assert new == "Approved, see attached."
    assert quoted.startswith("On Mon")
    new, quoted = ec.split_quoted("Thanks\n-----Original Message-----\nFrom: A\nold")
    assert (new, quoted.splitlines()[0]) == ("Thanks", "-----Original Message-----")
    new, quoted = ec.split_quoted("See below\n\nFrom: Bob <b@x.com>\nSent: Monday\nTo: Tom\nSubject: RE: x\n\nold body")
    assert new == "See below" and quoted.startswith("From: Bob")
    new, quoted = ec.split_quoted("Top line\n> quoted one\nInline answer")
    assert new == "Top line\nInline answer" and quoted == "> quoted one"
    new, quoted = ec.split_quoted("FYI\n________________________________\nFrom: X\nSent: Y\nbody")
    assert new == "FYI" and quoted.startswith("____")


def test_prompt_labels_previous_messages_and_quoted_content(env):
    s = env.s
    current = _email(E3, project=PM, at="2026-10-03T16:00:00+00:00",
                     body_text="Answer: route overhead.\nOn Tue, Jane wrote:\n> where to route?")
    previous = [
        _email(E1, project=PM, at="2026-10-01T16:00:00+00:00", body_text="First question"),
        _email(E2, project=PM, at="2026-10-02T16:00:00+00:00", body_text="Second note"),
    ]
    msgs = ec.build_messages(current, attachments=["RFI-12.pdf"], previous=previous,
                             thread_summary="Running summary", settings=s)
    text = msgs[0]["content"]
    assert text.count("PREVIOUS MESSAGE in this thread (context only, may not apply to the current message)") == 2
    assert text.index("First question") < text.index("Second note") < text.index("CURRENT MESSAGE")
    assert "QUOTED EARLIER THREAD CONTENT (context only)" in text
    assert text.index("Answer: route overhead.") < text.index("QUOTED EARLIER")
    assert "> where to route?" in text.split("QUOTED EARLIER")[1]
    assert "Attachments (file names only): RFI-12.pdf" in text
    assert "THREAD SUMMARY SO FAR" in text and "Running summary" in text
    # Pacific time: 16:00 UTC is 9:00 AM PDT.
    assert "Date: Sat 2026-10-03 9:00 AM PDT" in text
    assert "classify ONLY" not in text  # the rule lives in the system prompt
    assert "Label ONLY the CURRENT MESSAGE" in ec.system_prompt()


def test_prompt_budget_keeps_the_newest_previous_messages(env):
    s = _settings(email_classify_context_max_chars=1700, email_classify_previous_max_chars=500)
    previous = [
        _email(f"p{i}", project=PM, at=f"2026-10-0{i}T16:00:00+00:00", body_text=f"MSG{i} " + "x" * 2000)
        for i in range(1, 6)
    ]
    current = _email(E3, project=PM, at="2026-10-09T16:00:00+00:00")
    text = ec.build_messages(current, attachments=[], previous=previous, thread_summary=None, settings=s)[0]["content"]
    assert "MSG5" in text and "MSG4" in text
    assert "MSG1" not in text
    assert len(text) < 4000


def test_previous_messages_stay_on_the_thread_and_project(env):
    db = env.db
    db.rows("ingested_emails").extend(
        [
            _email(E1, project=PM, at="2026-10-01T16:00:00+00:00"),
            _email(E2, project=PM2, at="2026-10-02T16:00:00+00:00"),  # other project
            _email("e-other-conv", project=PM, conv="conv-2", at="2026-10-02T16:00:00+00:00"),
            _email("e-later", project=PM, at="2026-10-09T16:00:00+00:00"),
        ]
    )
    current = _email(E3, project=PM, at="2026-10-05T16:00:00+00:00")
    assert [p["id"] for p in ec.previous_messages(db, current)] == [E1]
    db.rows("email_classifications").append({"email_id": E1, "thread_summary": "Earlier summary"})
    assert ec.latest_thread_summary(db, [db.rows("ingested_emails")[0]]) == "Earlier summary"


# ── Execute (the queue job) ──────────────────────────────────────────────────


def test_execute_stores_labels_and_summaries(env, monkeypatch):
    db = env.db
    db.rows("ingested_emails").append(_email(E1, project=PM))
    db.rows("ingested_email_attachments").append({"email_id": E1, "filename": "RFI-12.pdf", "created_at": "1"})
    sent = {}

    def fake_complete_json(feature, **kw):
        sent.update(feature=feature, **kw)
        return _answer(labels=[
            {"category": "rfis", "subcategory": "rfi_question", "title": "", "confidence": 0.9},
            {"category": "other", "subcategory": "none", "title": "Schedule impact", "confidence": 0.4},
        ])

    monkeypatch.setattr(ec.llm, "complete_json", fake_complete_json)
    row = ec.execute(E1)
    assert sent["feature"] == "email_classify"
    assert sent["schema"]["properties"]["labels"]["items"]["properties"]["category"]["enum"] == list(ec.CATEGORY_KEYS)
    assert row["status"] == "done"
    assert [lab["category"] for lab in row["ai_labels"]] == ["rfis", "other"]
    assert row["email_summary"] == "Asks for an RFI answer."
    assert row["thread_summary"] == "Jane asked about conduit."
    assert row["model"] == "self-hosted:qwen-3.8-27b"
    assert row["project_id"] == PM and row["conversation_id"] == "conv-1"
    # Done is idempotent: no second model call.
    sent.clear()
    ec.execute(E1)
    assert sent == {}


def test_execute_waits_for_the_body_and_skips_non_pm(env, monkeypatch):
    db = env.db
    db.rows("ingested_emails").append(_email(E1, project=PM, status="received", body_text=None))
    monkeypatch.setattr(ec.llm, "complete_json", lambda *a, **k: pytest.fail("no call"))
    with pytest.raises(ec.EmailNotReady) as exc:
        ec.execute(E1)
    from app.services import llm_errors

    assert llm_errors.is_transient(exc.value)
    db.rows("ingested_emails")[0].update(status="processed", body_text="x", project_id=BID)
    db.rows("email_classifications").append({"email_id": E1, "status": "pending", "ai_labels": None})
    assert ec.execute(E1) is None
    assert db.rows("email_classifications") == []


def test_bad_model_output_raises_for_the_queue_to_retry(env, monkeypatch):
    env.db.rows("ingested_emails").append(_email(E1, project=PM))
    monkeypatch.setattr(ec.llm, "complete_json", lambda *a, **k: _answer(labels=[{"category": "zzz"}]))
    with pytest.raises(llm.LlmBadOutput):
        ec.execute(E1)
    from app.services import llm_errors

    assert llm_errors.is_transient(llm.LlmBadOutput("x"))
    # The queue's terminal mark fails the row; a done row is never demoted.
    ec.mark_from_queue(E1, "failed", "boom")
    assert env.db.rows("email_classifications")[0]["status"] == "failed"


def test_backfill_failure_marks_the_row_failed_not_running(env, monkeypatch):
    env.db.rows("ingested_emails").append(_email(E1, project=PM))
    monkeypatch.setattr(ec.llm, "complete_json", lambda *a, **k: _answer(labels=[{"category": "zzz"}]))
    with pytest.raises(llm.LlmBadOutput):
        ec.execute(E1)  # no current job: the script's synchronous path
    row = env.db.rows("email_classifications")[0]
    assert row["status"] == "failed" and row["error"]


def test_queue_failure_leaves_the_running_mark_to_the_queue(env, monkeypatch):
    env.db.rows("ingested_emails").append(_email(E1, project=PM))
    monkeypatch.setattr(ec.llm, "complete_json", lambda *a, **k: _answer(labels=[{"category": "zzz"}]))
    token = llm_queue.current_job.set({"id": "j1", "attempts": 6, "max_attempts": 6})
    try:
        with pytest.raises(llm.LlmBadOutput):
            ec.execute(E1)
    finally:
        llm_queue.current_job.reset(token)
    assert env.db.rows("email_classifications")[0]["status"] == "running"


def test_execute_waits_for_an_earlier_thread_message(env, monkeypatch):
    db = env.db
    db.rows("ingested_emails").extend(
        [_email(E1, project=PM, at="2026-10-01T16:00:00+00:00"), _email(E2, project=PM, at="2026-10-02T16:00:00+00:00")]
    )
    db.rows("email_classifications").append({"email_id": E1, "status": "pending"})
    db.rows("llm_jobs").append({"id": "j0", "job_type": "email_classify", "target_id": E1, "status": "queued"})
    monkeypatch.setattr(ec.llm, "complete_json", lambda *a, **k: _answer())
    token = llm_queue.current_job.set({"id": "j2", "attempts": 1, "max_attempts": 6})
    try:
        with pytest.raises(ec.ThreadNotReady) as exc:
            ec.execute(E2)
        from app.services import llm_errors

        assert llm_errors.is_transient(exc.value)
        # The last attempt proceeds regardless.
        llm_queue.current_job.set({"id": "j2", "attempts": 6, "max_attempts": 6})
        assert ec.execute(E2)["status"] == "done"
    finally:
        llm_queue.current_job.reset(token)


def test_previous_messages_window_ends_at_the_current_message(env):
    db = env.db
    db.rows("ingested_emails").extend(
        [
            _email(E1, project=PM, at="2026-10-01T16:00:00+00:00"),
            _email(E2, project=PM, at="2026-10-02T16:00:00+00:00"),
            _email(E3, project=PM, at="2026-10-03T16:00:00+00:00"),
        ]
    )
    prev = ec.previous_messages(db, db.rows("ingested_emails")[1])
    assert [p["id"] for p in prev] == [E1]


def test_rows_for_chunks_long_id_lists(env):
    calls = []
    real_in = _Query.in_

    def spy(self, c, vals):
        calls.append(len(list(vals)))
        return real_in(self, c, vals)

    import unittest.mock as m

    with m.patch.object(_Query, "in_", spy):
        ec.rows_for(env.db, [f"id-{i}" for i in range(450)])
    assert calls == [200, 200, 50]


def test_non_pm_cleanup_keeps_a_user_correction(env, monkeypatch):
    db = env.db
    db.rows("ingested_emails").append(_email(E1, project=BID))
    db.rows("email_classifications").append(
        {"email_id": E1, "status": "pending", "ai_labels": None, "user_labels": [{"category": "rfis"}]}
    )
    assert ec.execute(E1) is None
    assert db.rows("email_classifications")[0]["user_labels"] == [{"category": "rfis"}]


# ── Triggers ─────────────────────────────────────────────────────────────────


def _jobs(env):
    return [j["target_id"] for j in env.enqueued]


def test_enqueue_only_for_pm_projects_and_not_twice(env):
    db = env.db
    db.rows("ingested_emails").extend(
        [_email(E1, project=PM), _email(E2, project=BID), _email(E3, project=None)]
    )
    assert ec.enqueue_for_emails([E1, E2, E3], sb=db) == 1
    assert _jobs(env) == [E1]
    job = env.enqueued[0]
    assert job["job_type"] == "email_classify" and job["payload"] == {"email_id": E1, "force": False}
    assert job["priority"] == env.s.email_classify_queue_priority
    # Already classified: a reassignment to another PM project does not re-run.
    db.rows("email_classifications")[0].update(status="done", ai_labels=[{"category": "rfis"}])
    db.rows("ingested_emails")[0]["project_id"] = PM2
    assert ec.enqueue_for_emails([E1], sb=db) == 0
    # Forced (rerun) does, and keeps the existing labels in place meanwhile.
    assert ec.enqueue_for_emails([E1], sb=db, force=True) == 1
    assert db.rows("email_classifications")[0]["ai_labels"] == [{"category": "rfis"}]


def test_enqueue_is_off_with_the_flag_and_never_raises(env, monkeypatch):
    env.db.rows("ingested_emails").append(_email(E1, project=PM))
    assert ec.enqueue_for_emails([E1], sb=env.db, settings=_settings(email_classify_enabled=False)) == 0
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert ec.enqueue_for_emails([E1], sb=env.db) == 0  # logged, not raised


def test_enqueue_failure_never_leaves_a_pending_row(env, monkeypatch):
    db = env.db
    db.rows("ingested_emails").append(_email(E1, project=PM))
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert ec.enqueue_for_emails([E1], sb=db) == 0
    assert db.rows("email_classifications") == []  # the attempt's own row is gone
    db.rows("email_classifications").append({"email_id": E1, "status": "done", "ai_labels": [{"category": "rfis"}]})
    with pytest.raises(RuntimeError):
        ec.rerun(db, E1, "user-1")
    assert db.rows("email_classifications")[0]["status"] == "done"


def test_stale_pending_or_running_rows_are_requeued(env):
    db = env.db
    db.rows("ingested_emails").extend([_email(E1, project=PM), _email(E2, project=PM, conv="c2")])
    db.rows("email_classifications").extend([{"email_id": E1, "status": "running"}, {"email_id": E2, "status": "pending"}])
    env.active[E2] = {"id": "live", "status": "queued"}
    assert ec.enqueue_for_emails([E1, E2], sb=db) == 1
    assert _jobs(env) == [E1]  # E2 has a live job; E1 was stuck


def test_forced_rerun_racing_a_finishing_job_restores_the_row(env, monkeypatch):
    db = env.db
    db.rows("ingested_emails").append(_email(E1, project=PM))
    db.rows("email_classifications").append({"email_id": E1, "status": "done", "ai_labels": [{"category": "rfis"}]})

    def collapse(*a, **k):
        raise llm_queue.JobAlreadyActive({"id": "j", "status": "running"})

    monkeypatch.setattr(llm_queue, "enqueue", collapse)
    assert ec.enqueue_for_emails([E1], sb=db, force=True) == 0
    assert db.rows("email_classifications")[0]["status"] == "done"


def test_auto_assign_paths_enqueue_for_pm_only(env):
    db = env.db
    db.rows("ingested_emails").extend(
        [_email(E1, status="id_r2"), _email(E2, status="id_r2", conv="c2")]
    )
    assert email_ingest._assign(db, db.rows("ingested_emails")[0], PM, matched_by="subject", expected_status="id_r2")
    assert email_ingest._assign(db, db.rows("ingested_emails")[1], BID, matched_by="subject", expected_status="id_r2")
    assert _jobs(env) == [E1]


def test_manual_assign_and_retro_siblings_enqueue(env, monkeypatch):
    db = env.db
    db.rows("ingested_emails").extend(
        [
            _email(E1, project=None, at="2026-10-02T16:00:00+00:00"),
            _email(E2, project=None, at="2026-10-01T16:00:00+00:00"),  # retro sibling
            _email(E3, project=None, status="failed", body_text=None),  # revived, no body yet
        ]
    )
    email_ingest.assign_manual(db, db.rows("ingested_emails")[0], PM, "user-1")
    # Oldest first so the thread summaries chain; the revived one waits for its body.
    assert _jobs(env) == [E2, E1]
    # The body fetch of the revived sibling enqueues it.
    revived = next(r for r in db.rows("ingested_emails") if r["id"] == E3)
    assert revived["status"] == "received" and revived["project_id"] == PM

    monkeypatch.setattr(
        email_ingest.graph_inbox, "get_message", lambda *a, **k: {"body": {"content": "late body"}}
    )
    email_ingest._step_received(db, dict(revived))
    assert _jobs(env) == [E2, E1, E3]


def test_manual_assign_to_bidding_project_does_nothing(env):
    db = env.db
    db.rows("ingested_emails").append(_email(E1))
    email_ingest.assign_manual(db, db.rows("ingested_emails")[0], BID, "user-1")
    assert env.enqueued == []


def test_rescan_assign_enqueues(env):
    db = env.db
    db.rows("ingested_emails").append(_email(E1))
    email_ingest._rescan_assign(db, db.rows("ingested_emails")[0], PM, matched_by="subject")
    assert _jobs(env) == [E1]


def test_unassign_leaves_the_classification(env):
    db = env.db
    db.rows("ingested_emails").append(_email(E1, project=PM))
    db.rows("email_classifications").append({"email_id": E1, "status": "done", "ai_labels": [{"category": "rfis"}]})
    email_ingest.unassign(db, db.rows("ingested_emails")[0], "user-1")
    assert db.rows("email_classifications")[0]["ai_labels"] == [{"category": "rfis"}]


def test_project_entering_pm_enqueues_its_emails(env):
    db = env.db
    db.rows("ingested_emails").extend([_email(E1, project=PM), _email(E2, project=BID)])
    assert ec.enqueue_for_project(PM, sb=db) == 1
    assert ec.enqueue_for_project(BID, sb=db) == 0
    assert _jobs(env) == [E1]


def test_activate_pm_for_win_enqueues(env, monkeypatch):
    from app.services import pm

    called = []
    monkeypatch.setattr(ec, "enqueue_for_project", lambda pid, **kw: called.append(pid) or 0)
    db = env.db
    db.rows("projects").append({"id": "won-1", "name": "Won", "pm_stage": None, "abandoned_at": None})
    monkeypatch.setattr(pm, "get_supabase", lambda: db)
    monkeypatch.setattr(pm, "_winning_bid_amount", lambda *a: None)
    monkeypatch.setattr(pm, "_gc_name", lambda *a: None)
    monkeypatch.setattr(pm, "append_pm_stage_event", lambda *a, **k: None)
    monkeypatch.setattr(pm, "seed_pm_materials_from_boq", lambda *a: 0)
    monkeypatch.setattr(pm, "audit", lambda *a, **k: None)
    monkeypatch.setattr(pm, "notify_role", lambda *a, **k: None)
    assert pm.activate_pm_for_win("won-1", "user-1", None) is True
    assert called == ["won-1"]


def test_queue_spec_and_claim_pass(env, monkeypatch):
    spec = llm_queue._spec(llm_queue.JOB_EMAIL_CLASSIFY)
    assert spec.feature == "email_classify"
    assert llm_queue.JOB_EMAIL_CLASSIFY not in llm_queue.LLM_JOB_TYPES
    calls = []
    monkeypatch.setattr(llm_queue, "_claim_filtered", lambda s, n, types: calls.append((n, types)) or [])
    llm_queue._claim_email_classify(env.s, [])
    assert calls == [(1, ["email_classify"])]
    calls.clear()
    llm_queue._claim_email_classify(env.s, ["email_classify"])
    llm_queue._claim_email_classify(_settings(email_classify_enabled=False), [])
    assert calls == []


# ── Routes ───────────────────────────────────────────────────────────────────


class _Api:
    def __init__(self, monkeypatch, env, role):
        self.role = role
        monkeypatch.setattr(emails_router, "get_supabase", lambda: env.db)
        monkeypatch.setattr(emails_router, "audit", lambda *a, **k: None)
        monkeypatch.setattr(emails_router.visibility, "assert_visible", lambda row, user: None)
        monkeypatch.setattr(emails_router, "_audit_other_mailbox", lambda email, user: None)
        monkeypatch.setattr(ec.llm, "is_configured", lambda feature, settings=None: True)
        app = FastAPI()
        app.include_router(emails_router.router)
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            id="user-1", email="u@g3electrical.com", role=self.role, is_active=True
        )
        self.client = TestClient(app)


@pytest.fixture
def api(monkeypatch, env):
    env.db.rows("ingested_emails").extend([_email(E1, project=PM), _email(E2, project=BID)])
    env.db.rows("email_classifications").append(
        {"email_id": E1, "status": "done", "ai_labels": [
            {"category": "rfis", "subcategory": "rfi_question", "title": None, "confidence": 0.9}],
         "user_labels": None, "email_summary": "s", "thread_summary": "t", "model": "self-hosted:q"}
    )
    return _Api(monkeypatch, env, Role.ESTIMATING_ADMIN)


def test_override_reset_and_correction_log(api, env):
    r = api.client.put(f"/emails/{E1}/classification", json={"labels": [
        {"category": "financials", "subcategory": "invoice"},
        {"category": "other", "title": "Site meeting"},
    ]})
    assert r.status_code == 200, r.text
    c = r.json()["classification"]
    assert [lab["category"] for lab in c["labels"]] == ["financials", "other"]
    assert c["overridden"] is True and c["overridden_by_name"] == "Pat Writer"
    assert c["ai_labels"][0]["category"] == "rfis"
    log = env.db.rows("email_label_corrections")
    assert log[0]["action"] == "override" and log[0]["user_id"] == "user-1"
    assert log[0]["before"][0]["category"] == "rfis" and log[0]["after"][0]["category"] == "financials"

    r = api.client.delete(f"/emails/{E1}/classification/override")
    assert r.status_code == 200
    assert r.json()["classification"]["labels"][0]["category"] == "rfis"
    assert r.json()["classification"]["overridden"] is False
    assert [e["action"] for e in log] == ["override", "reset"]


def test_override_validation_and_pm_guard(api):
    bad = api.client.put(f"/emails/{E1}/classification", json={"labels": [{"category": "rfis", "subcategory": "invoice"}]})
    assert bad.status_code == 422
    bad = api.client.put(f"/emails/{E1}/classification", json={"labels": [{"category": "nope"}]})
    assert bad.status_code == 422
    bad = api.client.put(f"/emails/{E1}/classification", json={"labels": [{"category": "other", "title": ""}]})
    assert bad.status_code == 422
    bidding = api.client.put(f"/emails/{E2}/classification", json={"labels": [{"category": "rfis"}]})
    assert bidding.status_code == 409
    assert api.client.put("/emails/not-a-uuid/classification", json={"labels": [{"category": "rfis"}]}).status_code == 404


def test_rerun_forces_a_new_job(api, env):
    r = api.client.post(f"/emails/{E1}/classification/rerun")
    assert r.status_code == 200
    assert env.enqueued[-1]["payload"] == {"email_id": E1, "force": True}
    assert r.json()["classification"]["status"] == "pending"


def test_read_only_roles_get_403_on_writes(api, env):
    api.role = Role.ACCOUNTANT
    assert api.client.put(f"/emails/{E1}/classification", json={"labels": [{"category": "rfis"}]}).status_code == 403
    assert api.client.delete(f"/emails/{E1}/classification/override").status_code == 403
    assert api.client.post(f"/emails/{E1}/classification/rerun").status_code == 403
    # Reads still show the labels.
    r = api.client.get(f"/emails/{E1}")
    assert r.status_code == 200
    assert r.json()["classification"]["labels"][0]["category"] == "rfis"


def test_list_attaches_classification_in_one_query(api, env, monkeypatch):
    monkeypatch.setattr(emails_router.visibility, "visible_mailboxes", lambda user: SimpleNamespace(allowed=None))
    monkeypatch.setattr(emails_router.visibility, "apply_scope", lambda q, user: q)
    calls = []
    real = ec.rows_for
    monkeypatch.setattr(ec, "rows_for", lambda sb, ids: calls.append(list(ids)) or real(sb, ids))
    r = api.client.get(f"/emails?project_id={PM}")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows[0]["classification"]["labels"][0]["subcategory"] == "rfi_question"
    assert len(calls) == 1


# ── Routing: self-hosted only, never a fallback ──────────────────────────────


def test_email_classify_is_self_hosted_only_whatever_the_flag():
    spec = llm._FEATURES["email_classify"]
    assert spec.self_hosted_only and not spec.openai_only
    for flag in (False, True):
        s = _settings(full_self_hosted_llms_enabled=flag)
        route = llm.resolve("email_classify", s)
        assert route.provider == "self_hosted"
        assert route.model == "qwen-3.8-27b"
        assert llm.active_model("email_classify", s) == "self-hosted:qwen-3.8-27b"


def test_email_classify_pin_off_switch_and_live_box():
    s = _settings(self_hosted_email_classify_model="qwen-pinned")
    assert llm.resolve("email_classify", s).model == "qwen-pinned"
    assert llm.is_configured("email_classify", _settings(self_hosted_email_classify_model="off")) is False
    off = _settings(email_classify_enabled=False)
    assert llm.unavailable_reason("email_classify", off) is not None
    assert llm.is_configured("email_classify", off) is False
    # The live box override moves it (and drops the pin, like every feature).
    boxed = _settings(
        self_hosted_email_classify_model="qwen-pinned",
        self_hosted_llm_ec2c_base_url="https://box-c.example/v1",
        self_hosted_llm_ec2c_model="qwen-c",
    )
    llm_target._set_memory("ec2c")
    try:
        route = llm.resolve("email_classify", boxed)
        assert (route.base_url, route.model) == ("https://box-c.example/v1", "qwen-c")
    finally:
        llm_target.reset()


def test_email_classify_never_falls_back_to_a_third_party(monkeypatch):
    used: list[str] = []

    def client_for(route, settings):
        used.append(route.provider)

        def create(**kw):
            raise openai.APIConnectionError(request=SimpleNamespace(method="POST", url="x"))

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client.with_options = lambda **kw: client
        return client

    monkeypatch.setattr(llm, "_client_for", client_for)
    s = _settings(full_self_hosted_llms_enabled=False)
    with pytest.raises(llm.SelfHostedUnreachable):
        llm.complete_json("email_classify", system="s", messages=[{"role": "user", "content": "x"}], settings=s)
    assert used == ["self_hosted"]
    from app.services import llm_errors

    assert llm_errors.is_transient(llm.SelfHostedUnreachable("down"))
    # No box configured: not configured (permanent), still never 3rd party.
    bare = Settings(_env_file=None, email_classify_enabled=True, openai_api_key="o", anthropic_api_key="a")
    assert llm.resolve("email_classify", bare).provider == "self_hosted"
    with pytest.raises(llm.LlmNotConfigured):
        llm.complete_json("email_classify", system="s", messages=[{"role": "user", "content": "x"}], settings=bare)
    assert used == ["self_hosted"]
