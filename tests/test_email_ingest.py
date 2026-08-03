"""Email-ingestion pipeline (services/email_ingest) — status machine, learn-back
and recovery semantics against an in-memory fake Supabase.

The fake supports the exact query surface email_ingest uses (eq/neq/in_/is_/
gte/or_/order/limit/range + insert/update/upsert/delete). Graph and OpenAI are
monkeypatched per test; settings come from a real Settings object built with
_env_file=None so the local .env can't leak in.
"""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import email_ingest


# ── Fake Supabase ──────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._op, self._payload = None, None
        self._eq, self._neq, self._null, self._notnull, self._in = [], [], [], [], []
        self._gte, self._lt, self._or = [], [], []
        self._negate_next = False

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload, **kwargs):
        self._op, self._payload = "upsert", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    @property
    def not_(self):
        self._negate_next = True
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def neq(self, col, val):
        self._neq.append((col, val))
        return self

    def is_(self, col, val):  # only ever "null" in the code under test
        if self._negate_next:
            self._negate_next = False
            self._notnull.append(col)
        else:
            self._null.append(col)
        return self

    def in_(self, col, vals):
        self._in.append((col, list(vals)))
        return self

    def gte(self, col, val):
        self._gte.append((col, val))
        return self

    def lt(self, col, val):  # rfq_inbox's stale-send sweep
        self._lt.append((col, val))
        return self

    def or_(self, expr):
        # Backoff gate + lease acquisition expressions.
        self._or.append(expr)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def _or_matches(self, row, expr):
        for part in expr.split(","):
            col, op, *rest = part.split(".", 2)
            val = rest[0] if rest else None
            if op == "is" and val == "null" and row.get(col) is None:
                return True
            if op == "lte" and row.get(col) is not None and row[col] <= val:
                return True
            if op == "lt" and row.get(col) is not None and row[col] < val:
                return True
            if op == "eq" and row.get(col) == val:
                return True
        return False

    def _matches(self, row):
        return (
            all(row.get(c) == v for c, v in self._eq)
            and all(row.get(c) != v for c, v in self._neq)
            and all(row.get(c) is None for c in self._null)
            and all(row.get(c) is not None for c in self._notnull)
            and all(row.get(c) in v for c, v in self._in)
            and all(row.get(c) is not None and row[c] >= v for c, v in self._gte)
            and all(row.get(c) is not None and row[c] < v for c, v in self._lt)
            and all(self._or_matches(row, e) for e in self._or)
        )

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [dict(r) for r in rows if self._matches(r)]
            return SimpleNamespace(data=hits, count=len(hits))
        if self._op == "insert":
            exc = self.db.raise_on_insert.get(self.table)
            if exc is not None:
                raise exc
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", f"{self.table}-{self.db.next_id()}")
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "upsert":  # keyed on "id" (graph_sync_state)
            p = self._payload
            existing = next((r for r in rows if r.get("id") == p.get("id")), None)
            if existing is not None:
                existing.update(p)
            else:
                rows.append(dict(p))
            return SimpleNamespace(data=[dict(p)])
        if self._op == "delete":
            hits = [dict(r) for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=hits)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.raise_on_insert = {}
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def table(self, name):
        return _Query(self, name)


# ── Fixtures ───────────────────────────────────────────────────────────────────

MAILBOX = "pm@example.com"
PROJECTS = [
    {"id": "p1", "number": "26-104", "name": "Riverside Plaza"},
    {"id": "p2", "number": "26-999", "name": "Maple Street TI"},
]


def _email(**over):
    row = {
        "id": "e1",
        "mailbox": MAILBOX,
        "folder": "inbox",
        "direction": "inbound",
        "graph_message_id": "g1",
        "conversation_id": "conv-1",
        "subject": "RE: 26-104 - Riverside BOM",
        "status": "received",
        "attempts": 0,
        "has_attachments": False,
        "project_id": None,
    }
    row.update(over)
    return row


def _settings(**over):
    over.setdefault("openai_api_key", "test-key")
    return Settings(_env_file=None, **over)


@pytest.fixture
def db():
    return FakeDB({"projects": PROJECTS, "notifications": []})


@pytest.fixture(autouse=True)
def _fake_audit(monkeypatch, db):
    # audit() opens the REAL supabase client (via notifications.get_supabase) —
    # record into the fake instead so tests never touch the network.
    monkeypatch.setattr(
        email_ingest,
        "audit",
        lambda actor, action, *a, **k: db.tables.setdefault("audit_log", []).append(
            {"actor_id": actor, "action": action}
        ),
    )


@pytest.fixture
def use_settings(monkeypatch):
    def _apply(**over):
        s = _settings(**over)
        monkeypatch.setattr(email_ingest, "get_settings", lambda: s)
        return s

    return _apply


def _seed(db, email):
    db.tables.setdefault("ingested_emails", []).append(email)
    return db.tables["ingested_emails"][-1]


def _row(db, email_id="e1"):
    return next(r for r in db.tables["ingested_emails"] if r["id"] == email_id)


# ── Status walk ────────────────────────────────────────────────────────────────


def test_full_walk_received_to_subject_match(db, use_settings, monkeypatch):
    use_settings()
    monkeypatch.setattr(
        email_ingest.graph_inbox,
        "get_message",
        lambda *a, **k: {"body": {"content": "hello body"}},
    )
    email = _seed(db, _email())
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["status"] == "processed"
    assert row["project_id"] == "p1"
    assert row["matched_by"] == "subject"
    assert row["pipeline_round"] == "r2"
    assert row["body_text"] == "hello body"
    # Learn-back: the conversation map now knows conv-1 → p1.
    maps = db.tables["email_conversation_projects"]
    assert len(maps) == 1 and maps[0]["project_id"] == "p1" and maps[0]["source"] == "subject"


def test_r1_conversation_map_short_circuits(db, use_settings):
    use_settings()
    db.tables["email_conversation_projects"] = [
        {"id": "m1", "mailbox": MAILBOX, "conversation_id": "conv-1",
         "project_id": "p2", "source": "manual"}
    ]
    email = _seed(db, _email(status="id_r1", subject="totally unrelated"))
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["status"] == "processed"
    assert row["project_id"] == "p2"
    assert row["matched_by"] == "conversation"
    assert row["pipeline_round"] == "r1"
    # R1 must not rewrite the (manual) map entry.
    assert db.tables["email_conversation_projects"][0]["source"] == "manual"


def test_body_is_capped_and_flagged(db, use_settings, monkeypatch):
    use_settings(email_body_max_chars=5)
    monkeypatch.setattr(
        email_ingest.graph_inbox,
        "get_message",
        lambda *a, **k: {"body": {"content": "0123456789"}},
    )
    email = _seed(db, _email(subject="no match here"))
    # Stop after the fetch step so we can inspect the stored body.
    monkeypatch.setattr(email_ingest, "_step_r1", lambda sb, e: None)
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["body_text"] == "01234"
    assert row["body_truncated"] is True


# ── Round 3 (LLM) ──────────────────────────────────────────────────────────────


def test_r3_confident_match_assigns_and_learns(db, use_settings, monkeypatch):
    use_settings()
    monkeypatch.setattr(
        email_ingest.openai_text,
        "match_subject_to_project",
        lambda subject, candidates: {"candidate_index": 1, "confidence": 0.93},
    )
    email = _seed(db, _email(status="id_r3", subject="maplle streeet punch"))
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["status"] == "processed"
    assert row["project_id"] == "p2"
    assert row["matched_by"] == "llm"
    assert row["match_confidence"] == 0.93
    assert row["pipeline_round"] == "r3"
    assert db.tables["email_conversation_projects"][0]["source"] == "llm"


def test_r3_below_threshold_stores_suggestion_and_lands_unknown(db, use_settings, monkeypatch):
    use_settings()
    monkeypatch.setattr(
        email_ingest.openai_text,
        "match_subject_to_project",
        lambda subject, candidates: {"candidate_index": 0, "confidence": 0.6},
    )
    email = _seed(db, _email(status="id_r3", subject="ambiguous"))
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["status"] == "processed"
    assert row["project_id"] is None
    assert row["suggested_project_id"] == "p1"
    assert row["suggested_confidence"] == 0.6
    # Ran out of rounds at R3 — that is where it landed in the Unknown pool.
    assert row["pipeline_round"] == "r3"
    assert "email_conversation_projects" not in db.tables  # no learn-back on a guess


def test_r3_out_of_range_index_is_no_match(db, use_settings, monkeypatch):
    use_settings()
    monkeypatch.setattr(
        email_ingest.openai_text,
        "match_subject_to_project",
        lambda subject, candidates: {"candidate_index": 99, "confidence": 0.99},
    )
    email = _seed(db, _email(status="id_r3", subject="whatever"))
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["project_id"] is None and row.get("suggested_project_id") is None


def test_r3_without_api_key_finalizes_unknown(db, use_settings):
    use_settings(openai_api_key="")
    email = _seed(db, _email(status="id_r3", subject="whatever"))
    email_ingest._process_email(db, dict(email))
    assert _row(db)["status"] == "processed"
    assert _row(db)["project_id"] is None


def test_out_of_tokens_burns_no_attempts_and_notifies_once(db, use_settings, monkeypatch):
    use_settings()

    def _boom(subject, candidates):
        raise RuntimeError("credit balance is too low")

    notified = []

    def _fake_notify(role, project_id, type_, message):
        notified.append(type_)
        db.tables["notifications"].append({"type": type_, "created_at": "9999-01-01T00:00:00+00:00"})

    monkeypatch.setattr(email_ingest.openai_text, "match_subject_to_project", _boom)
    monkeypatch.setattr(email_ingest, "notify_role", _fake_notify)

    email = _seed(db, _email(status="id_r3", subject="whatever"))
    outcome = email_ingest._process_email(db, dict(email))
    assert outcome == "llm_down"
    row = _row(db)
    assert row["status"] == "id_r3"          # still pending — resumes later
    assert row["attempts"] == 0              # outage burns no attempts
    assert row["next_attempt_at"] is not None
    assert notified == ["email_ingest.llm_outage"]

    # Second outage inside the dedup window → no second notification.
    email_ingest._process_email(db, dict(_row(db), next_attempt_at=None))
    assert notified == ["email_ingest.llm_outage"]


def test_transient_r3_failure_backs_off_then_fails_at_max(db, use_settings, monkeypatch):
    use_settings(email_match_max_attempts=2)

    def _boom(subject, candidates):
        raise ValueError("http 500 from provider")

    monkeypatch.setattr(email_ingest.openai_text, "match_subject_to_project", _boom)
    monkeypatch.setattr(email_ingest, "notify_role", lambda *a, **k: None)

    email = _seed(db, _email(status="id_r3", subject="whatever"))
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["status"] == "id_r3" and row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    assert row.get("pipeline_round") is None   # not terminal yet — nothing decided

    email_ingest._process_email(db, dict(row))
    row = _row(db)
    assert row["status"] == "failed"         # terminal, but still triageable
    assert "http 500" in row["error"]
    assert row["pipeline_round"] == "r3"     # 'failed' alone would lose the step


def test_llm_down_halts_r3_for_the_rest_of_the_sweep(db, use_settings, monkeypatch):
    use_settings()

    calls = []

    def _boom(subject, candidates):
        calls.append(subject)
        raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(email_ingest.openai_text, "match_subject_to_project", _boom)
    monkeypatch.setattr(email_ingest, "notify_role", lambda *a, **k: None)
    _seed(db, _email(id="e1", graph_message_id="g1", status="id_r3", subject="a"))
    _seed(db, _email(id="e2", graph_message_id="g2", status="id_r3", subject="b"))

    email_ingest.process_pending(db)
    assert len(calls) == 1  # second row skipped this tick


# ── Concurrency guards ─────────────────────────────────────────────────────────


def test_sweep_never_clobbers_a_concurrent_manual_assign(db, use_settings):
    use_settings()
    # DB row was manually assigned between the sweep's select and this step.
    _seed(db, _email(status="processed", project_id="p2", matched_by="manual"))
    stale_view = _email(status="id_r2")
    email_ingest._process_email(db, stale_view)
    row = _row(db)
    assert row["project_id"] == "p2" and row["matched_by"] == "manual"


# ── Delta ingest ───────────────────────────────────────────────────────────────


def _delta_msg(**over):
    msg = {
        "id": "gm-1",
        "conversationId": "conv-9",
        "internetMessageId": "<x@y>",
        "from": {"emailAddress": {"name": "Jane", "address": "jane@gc.com"}},
        "toRecipients": [{"emailAddress": {"address": MAILBOX}}],
        "subject": "hello",
        "bodyPreview": "hi",
        "receivedDateTime": "2026-07-01T00:00:00Z",
        "sentDateTime": "2026-07-01T00:00:01Z",
        "hasAttachments": False,
    }
    msg.update(over)
    return msg


def test_inbox_self_sent_mail_is_skipped_but_sentitems_copy_ingests(db):
    self_msg = _delta_msg(**{"from": {"emailAddress": {"address": MAILBOX}}})
    email_ingest._insert_from_delta(db, MAILBOX, "inbox", self_msg)
    assert not db.tables.get("ingested_emails")
    email_ingest._insert_from_delta(db, MAILBOX, "sentitems", self_msg)
    rows = db.tables["ingested_emails"]
    assert len(rows) == 1
    assert rows[0]["direction"] == "outbound"
    assert rows[0]["message_at"] == "2026-07-01T00:00:01Z"  # sentDateTime wins


def test_duplicate_and_tombstone_deliveries_are_ignored(db):
    email_ingest._insert_from_delta(db, MAILBOX, "inbox", _delta_msg())
    email_ingest._insert_from_delta(db, MAILBOX, "inbox", _delta_msg())  # re-seen
    email_ingest._insert_from_delta(db, MAILBOX, "inbox", {"id": "gm-2", "@removed": {}})
    assert len(db.tables["ingested_emails"]) == 1


def test_delta_link_not_persisted_when_an_insert_fails(db, use_settings, monkeypatch):
    use_settings(email_ingest_mailbox=MAILBOX)
    monkeypatch.setattr(
        email_ingest.graph_inbox,
        "delta_inbox",
        lambda *a, **k: ([_delta_msg()], "NEW-DELTA"),
    )
    db.raise_on_insert["ingested_emails"] = RuntimeError("db down")
    email_ingest._sync_folder(db, MAILBOX, "inbox")
    assert not db.tables.get("graph_sync_state")  # old cursor kept → re-pull next tick

    del db.raise_on_insert["ingested_emails"]
    email_ingest._sync_folder(db, MAILBOX, "inbox")
    states = db.tables["graph_sync_state"]
    assert states[0]["delta_link"] == "NEW-DELTA"


# ── Manual triage + learn-back ─────────────────────────────────────────────────


def test_assign_manual_retro_assigns_only_unassigned_siblings(db, use_settings):
    use_settings()
    target = _seed(db, _email(id="e1", graph_message_id="g1", status="processed",
                              body_text="hello"))
    _seed(db, _email(id="e2", graph_message_id="g2", status="failed"))          # never fetched
    _seed(db, _email(id="e3", graph_message_id="g3", status="processed",
                     project_id="p2", matched_by="manual"))                      # already filed
    _seed(db, _email(id="e4", graph_message_id="g4", status="id_r2"))            # mid-pipeline
    _seed(db, _email(id="e5", graph_message_id="g5", status="failed",
                     body_text="fetched"))                                       # failed post-fetch

    updated, retro = email_ingest.assign_manual(db, dict(target), "p1", "user-1")
    assert updated["project_id"] == "p1" and updated["matched_by"] == "manual"
    assert retro == 3
    # Failed sibling whose content was never fetched: assigned AND revived so
    # the sweep pulls its body/attachments.
    assert _row(db, "e2")["project_id"] == "p1"
    assert _row(db, "e2")["matched_by"] == "conversation"
    assert _row(db, "e2")["status"] == "received"
    assert _row(db, "e2")["attempts"] == 0
    assert _row(db, "e3")["project_id"] == "p2"      # never stolen
    # Mid-pipeline sibling: assignment lands now; its in-flight step's guarded
    # CAS misses and the finalize path closes it out.
    assert _row(db, "e4")["project_id"] == "p1"
    assert _row(db, "e4")["status"] == "id_r2"
    # Failed-after-fetch sibling: content exists → straight to processed.
    assert _row(db, "e5")["project_id"] == "p1"
    assert _row(db, "e5")["status"] == "processed"
    maps = db.tables["email_conversation_projects"]
    assert maps[0]["source"] == "manual" and maps[0]["project_id"] == "p1"
    assert any(r["action"] == "email.assign" for r in db.tables["audit_log"])


def test_assign_manual_revives_content_fetch_for_unfetched_email(db, use_settings):
    use_settings()
    target = _seed(db, _email(status="failed", attempts=5))  # died at 'received'
    updated, _ = email_ingest.assign_manual(db, dict(target), "p1", "user-1")
    # The assignment sticks, but the content-fetch obligation is revived.
    assert updated["project_id"] == "p1" and updated["matched_by"] == "manual"
    assert updated["status"] == "received"
    assert updated["attempts"] == 0


def test_pipeline_finalizes_a_mid_pipeline_manual_assignment(db, use_settings, monkeypatch):
    use_settings()
    monkeypatch.setattr(
        email_ingest.graph_inbox,
        "get_message",
        lambda *a, **k: {"body": {"content": "body"}},
    )
    # Manually assigned while still at 'received' (no conversation → no map).
    email = _seed(db, _email(status="received", project_id="p2", matched_by="manual",
                             pipeline_round="manual", conversation_id=None,
                             subject="RE: 26-104 - Riverside BOM"))  # R2 would say p1!
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    # Content fetched, but the manual assignment to p2 must never be replaced
    # by the automatic subject match to p1.
    assert row["body_text"] == "body"
    assert row["status"] == "processed"
    assert row["project_id"] == "p2"
    assert row["matched_by"] == "manual"
    assert row["pipeline_round"] == "manual"  # the sweep never restamps it


def test_lease_is_fenced_and_reacquirable_by_its_holder(db, use_settings, monkeypatch):
    use_settings()
    key = "pm-mail:x:lease"
    assert email_ingest._acquire_lease(db, key) is True        # fresh insert
    assert email_ingest._acquire_lease(db, key) is True        # own live lease → ok
    assert email_ingest._renew_lease(db, key) is True
    monkeypatch.setattr(email_ingest, "_RUNNER_TOKEN", "other-runner")
    assert email_ingest._acquire_lease(db, key) is False       # live lease, not ours
    assert email_ingest._renew_lease(db, key) is False         # renewal fails closed


def test_manual_assign_overrides_auto_map_but_auto_never_overrides_manual(db, use_settings):
    use_settings()
    email_ingest._upsert_conversation_map(db, MAILBOX, "c1", "p1", source="llm")
    email_ingest._upsert_conversation_map(db, MAILBOX, "c1", "p2", source="manual")
    maps = db.tables["email_conversation_projects"]
    assert maps[0]["project_id"] == "p2" and maps[0]["source"] == "manual"
    email_ingest._upsert_conversation_map(db, MAILBOX, "c1", "p1", source="subject")
    assert maps[0]["project_id"] == "p2"  # auto could not demote the manual row


def test_unassign_removes_map_only_when_no_sibling_remains(db, use_settings):
    use_settings()
    e1 = _seed(db, _email(id="e1", graph_message_id="g1", status="processed",
                          project_id="p1", matched_by="manual"))
    _seed(db, _email(id="e2", graph_message_id="g2", status="processed",
                     project_id="p1", matched_by="conversation"))
    db.tables["email_conversation_projects"] = [
        {"id": "m1", "mailbox": MAILBOX, "conversation_id": "conv-1",
         "project_id": "p1", "source": "manual"}
    ]

    email_ingest.unassign(db, dict(e1), "user-1")
    assert _row(db, "e1")["project_id"] is None
    # e2 still holds the conversation on p1 → the map must survive.
    assert len(db.tables["email_conversation_projects"]) == 1

    e2 = _row(db, "e2")
    email_ingest.unassign(db, dict(e2), "user-1")
    assert db.tables["email_conversation_projects"] == []  # last one out → map gone


# ── New-project rescan ─────────────────────────────────────────────────────────


def test_rescan_assigns_deterministic_hits_and_respects_manual_race(db, use_settings, monkeypatch):
    use_settings()
    monkeypatch.setattr(email_ingest, "get_supabase", lambda: db)
    _seed(db, _email(id="e1", graph_message_id="g1", status="processed",
                     subject="RE: 26-104 – Riverside", conversation_id="cA",
                     message_at="2026-07-01T00:00:00+00:00"))
    _seed(db, _email(id="e2", graph_message_id="g2", status="processed",
                     subject="unrelated", conversation_id="cB",
                     message_at="2026-07-01T00:00:00+00:00"))
    monkeypatch.setattr(
        email_ingest.openai_text,
        "confirm_subject_matches_project",
        lambda subject, project: {"match": False, "confidence": 0.1},
    )
    email_ingest.rescan_unknown_for_project("p1")
    assert _row(db, "e1")["project_id"] == "p1"
    assert _row(db, "e1")["matched_by"] == "subject"
    assert _row(db, "e2")["project_id"] is None


# ── pipeline_round: which step decided the email (0084) ────────────────────────
#
# `status` collapses to 'failed' from every step and `matched_by` names evidence
# rather than a round (the retro-assign writes 'conversation', the rescan writes
# 'subject'/'llm'), so the round only survives if each terminal write stamps it.
# The live-round cases ride along on the tests above; these cover the paths that
# would otherwise be indistinguishable in the UI.


def test_fetch_failure_records_the_step_it_died_in(db, use_settings, monkeypatch):
    use_settings(email_match_max_attempts=1)

    def _boom(*a, **k):
        raise RuntimeError("graph 503")

    monkeypatch.setattr(email_ingest.graph_inbox, "get_message", _boom)
    monkeypatch.setattr(email_ingest, "notify_role", lambda *a, **k: None)

    email = _seed(db, _email(status="received"))
    email_ingest._process_email(db, dict(email))
    row = _row(db)
    assert row["status"] == "failed"
    # Never reached an identification round — it died pulling the content.
    assert row["pipeline_round"] == "fetch"


def test_manual_assign_and_its_retro_siblings_are_distinguishable(db, use_settings):
    use_settings()
    target = _seed(db, _email(id="e1", graph_message_id="g1", status="processed",
                              body_text="hello"))
    _seed(db, _email(id="e2", graph_message_id="g2", status="processed",
                     body_text="sibling"))

    updated, retro = email_ingest.assign_manual(db, dict(target), "p1", "user-1")
    assert retro == 1
    assert updated["pipeline_round"] == "manual"
    sibling = _row(db, "e2")
    # Same matched_by as a live R1 match, but it never ran a round — it rode
    # along on the human's decision, and the badge must say so.
    assert sibling["matched_by"] == "conversation"
    assert sibling["pipeline_round"] == "retro"


def test_unassign_clears_the_round_with_the_decision(db, use_settings):
    use_settings()
    email = _seed(db, _email(status="processed", project_id="p1",
                             matched_by="subject", pipeline_round="r2"))
    updated = email_ingest.unassign(db, dict(email), "user-1")
    assert updated["project_id"] is None
    assert updated["matched_by"] is None
    assert updated["pipeline_round"] is None


def test_rescan_is_stamped_apart_from_the_live_subject_round(db, use_settings, monkeypatch):
    use_settings()
    monkeypatch.setattr(email_ingest, "get_supabase", lambda: db)
    monkeypatch.setattr(
        email_ingest.openai_text,
        "confirm_subject_matches_project",
        lambda subject, project: {"match": False, "confidence": 0.1},
    )
    _seed(db, _email(status="processed", subject="RE: 26-104 – Riverside",
                     pipeline_round="r3", message_at="2026-07-01T00:00:00+00:00"))
    email_ingest.rescan_unknown_for_project("p1")
    row = _row(db)
    assert row["project_id"] == "p1" and row["matched_by"] == "subject"
    # Ran against ONE new project outside the sweep — not the live R2 round.
    assert row["pipeline_round"] == "rescan"
