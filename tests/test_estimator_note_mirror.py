"""services/estimator_notes.mirror_send_message (0080) — the send-message →
Project notes mirror.

The mirror runs AFTER a delivered email, so the contract under test is: write
the note when there is one to write, stay silent (no notification row, no
notification email), and never raise.
"""

from types import SimpleNamespace

from app.services import estimator_notes as mod


class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.payload = None

    def insert(self, payload):
        self.payload = payload
        return self

    def execute(self):
        self.db.calls.append(self)
        if self.db.error:
            raise self.db.error
        return SimpleNamespace(data=[{"id": "n1", **(self.payload or {})}])


class _FakeDB:
    def __init__(self, error=None):
        self.calls: list[_Query] = []
        self.error = error

    def table(self, name):
        return _Query(self, name)


def _patch(monkeypatch, db):
    monkeypatch.setattr(mod, "get_supabase", lambda: db)
    return db


def test_message_is_written_as_a_note_by_the_sender(monkeypatch):
    db = _patch(monkeypatch, _FakeDB())
    row = mod.mirror_send_message(
        project_id="p1",
        author_id="u1",
        message="Bob is picking this up",
        source=mod.SOURCE_PACKAGE_SEND,
    )
    assert row["id"] == "n1"
    assert db.calls[0].table_name == "estimator_notes"
    assert db.calls[0].payload == {
        "project_id": "p1",
        "author_id": "u1",
        "body": "Bob is picking this up",
        "source": "package_send",
    }


def test_mirror_is_silent_touching_no_notification_table(monkeypatch):
    # The same text already headed the package email; a bell on top of that is a
    # duplicate. estimator_notes is the ONLY table written.
    db = _patch(monkeypatch, _FakeDB())
    mod.mirror_send_message(
        project_id="p1", author_id="u1", message="hi", source=mod.SOURCE_UPDATE_SEND
    )
    assert {c.table_name for c in db.calls} == {"estimator_notes"}


def test_body_is_stripped(monkeypatch):
    db = _patch(monkeypatch, _FakeDB())
    mod.mirror_send_message(
        project_id="p1", author_id="u1", message="  padded  ", source=mod.SOURCE_UPDATE_SEND
    )
    assert db.calls[0].payload["body"] == "padded"


def test_blank_and_missing_messages_write_nothing(monkeypatch):
    db = _patch(monkeypatch, _FakeDB())
    for message in (None, "", "   \n\t "):
        assert (
            mod.mirror_send_message(
                project_id="p1",
                author_id="u1",
                message=message,
                source=mod.SOURCE_PACKAGE_SEND,
            )
            is None
        )
    assert db.calls == []


def test_unknown_source_is_dropped_rather_than_written(monkeypatch):
    # Would violate estimator_notes_source_chk; a programming error must not
    # become a 500 on a delivered send.
    db = _patch(monkeypatch, _FakeDB())
    assert (
        mod.mirror_send_message(
            project_id="p1", author_id="u1", message="hi", source="rfi_send"
        )
        is None
    )
    assert db.calls == []


def test_oversize_message_is_truncated_not_dropped(monkeypatch):
    db = _patch(monkeypatch, _FakeDB())
    mod.mirror_send_message(
        project_id="p1",
        author_id="u1",
        message="x" * (mod.NOTE_MAX_CHARS + 50),
        source=mod.SOURCE_PACKAGE_SEND,
    )
    body = db.calls[0].payload["body"]
    assert len(body) == mod.NOTE_MAX_CHARS
    assert body.endswith("…")


def test_insert_failure_is_swallowed(monkeypatch):
    # The email is already out — a lost mirror is cosmetic and must never bubble
    # up into a rollback of a delivered send.
    db = _patch(monkeypatch, _FakeDB(error=RuntimeError("db down")))
    assert (
        mod.mirror_send_message(
            project_id="p1", author_id="u1", message="hi", source=mod.SOURCE_PACKAGE_SEND
        )
        is None
    )
    assert len(db.calls) == 1


def test_author_may_be_missing(monkeypatch):
    db = _patch(monkeypatch, _FakeDB())
    mod.mirror_send_message(
        project_id="p1", author_id=None, message="hi", source=mod.SOURCE_PACKAGE_SEND
    )
    assert db.calls[0].payload["author_id"] is None
