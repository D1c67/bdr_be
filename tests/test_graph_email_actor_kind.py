"""`graph_email.send_mail(actor_kind=...)` (Phase 8 plan section 10, owner
question 18 (a)).

`None` keeps every existing caller unchanged. `"system"` stamps the email_log
row as `system` with no assistant run id, is refused inside an assistant run
(so a model-driven send can never be logged as `system`), and is refused with a
`sent_by`. Any other value is refused. Every refusal raises before anything is
written. No network and no database: the Supabase client, Graph token and HTTP
post are replaced.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import provenance
from app.services import graph_email

USER = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"


class _Table:
    def __init__(self, db: "_DB", name: str):
        self.db, self.name, self._op, self._payload = db, name, "select", None

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def eq(self, *_a):
        return self

    def execute(self):
        if self._op == "insert":
            row = dict(self._payload, id="log-1")
            self.db.inserted.append((self.name, row))
            return SimpleNamespace(data=[row])
        if self._op == "update":
            self.db.updated.append((self.name, self._payload))
        return SimpleNamespace(data=[])


class _DB:
    def __init__(self):
        self.inserted: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, dict]] = []

    def table(self, name):
        return _Table(self, name)


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(graph_email, "get_supabase", lambda: fake)
    monkeypatch.setattr(graph_email, "_active_test_session", lambda sb=None: None)
    monkeypatch.setattr(graph_email, "get_settings", lambda: SimpleNamespace(ms_sender="bids@example.com"))
    monkeypatch.setattr(graph_email, "_acquire_token", lambda: "token")
    posts: list[dict] = []

    def post(url, headers=None, json=None, timeout=None):
        posts.append({"url": url, "json": json})
        return SimpleNamespace(raise_for_status=lambda: None, headers={"request-id": "gid-1"})

    monkeypatch.setattr(graph_email.httpx, "post", post)
    fake.posts = posts
    return fake


def _send(**kw):
    return graph_email.send_mail(to=["a@example.com"], subject="s", body_html="<p>b</p>", **kw)


def _logged(db: _DB) -> dict:
    rows = [r for name, r in db.inserted if name == "email_log"]
    assert len(rows) == 1
    return rows[0]


def test_default_is_unchanged_for_a_plain_caller(db):
    _send()
    row = _logged(db)
    assert "actor_kind" not in row and "assistant_run_id" not in row
    assert len(db.posts) == 1


def test_default_inside_an_assistant_run_still_stamps_the_assistant(db):
    with provenance.acting_as_assistant(USER, RUN):
        _send(sent_by=USER)
    row = _logged(db)
    assert row["actor_kind"] == "assistant" and row["assistant_run_id"] == RUN


def test_system_stamps_with_a_null_run_id(db):
    _send(actor_kind="system")
    row = _logged(db)
    assert row["actor_kind"] == "system"
    assert "assistant_run_id" in row and row["assistant_run_id"] is None
    assert row["sent_by"] is None
    assert len(db.posts) == 1


def test_system_inside_an_assistant_run_raises_before_any_write(db):
    with provenance.acting_as_assistant(USER, RUN):
        with pytest.raises(ValueError, match="assistant run"):
            _send(actor_kind="system")
    assert db.inserted == [] and db.updated == [] and db.posts == []


def test_system_with_sent_by_is_refused_before_any_write(db):
    with pytest.raises(ValueError, match="sent_by"):
        _send(actor_kind="system", sent_by=USER)
    assert db.inserted == [] and db.posts == []


@pytest.mark.parametrize("value", ["assistant", "human", "", "System", "robot"])
def test_other_values_are_refused_before_any_write(db, value):
    with pytest.raises(ValueError, match="actor_kind"):
        _send(actor_kind=value)
    assert db.inserted == [] and db.posts == []


def test_actor_kind_is_keyword_only_and_last():
    import inspect

    params = list(inspect.signature(graph_email.send_mail).parameters.values())
    assert params[-1].name == "actor_kind"
    assert params[-1].kind is inspect.Parameter.KEYWORD_ONLY
    assert params[-1].default is None
