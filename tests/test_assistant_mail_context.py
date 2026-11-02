"""Mail sensitivity provenance is owner-scoped and fails closed."""
import pytest

from app.assistant import repo
from app.core import supabase_client
from tests.assistant_fakes import CONV, MSG_ASSISTANT, RUN, USER, FakeDB, conversation, pending_answer


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(assistant_conversations=[conversation()], assistant_messages=[pending_answer()],
        assistant_tool_calls=[{"user_id": USER, "run_id": RUN, "phase": "begun",
            "message_id": MSG_ASSISTANT, "conversation_id": CONV, "tainted_context": False,
            "created_at": "2026-10-27T00:00:00+00:00"}])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def test_clean_bound_run_is_not_sensitive(db):
    assert repo.mail_sensitive_context(USER, RUN) is False


@pytest.mark.parametrize("change", ["missing_run", "other_owner", "ledger_taint", "conversation_taint",
                                   "mailbox_taint", "message_taint", "missing_message", "missing_conversation"])
def test_taint_or_missing_provenance_is_sensitive(db, change):
    owner, run = USER, RUN
    if change == "missing_run":
        run = "unknown"
    elif change == "other_owner":
        owner = "other"
    elif change == "ledger_taint":
        db.tables["assistant_tool_calls"][0]["tainted_context"] = True
    elif change == "conversation_taint":
        db.tables["assistant_conversations"][0]["tainted_at"] = "2026-10-27T00:00:00Z"
    elif change == "mailbox_taint":
        db.tables["assistant_conversations"][0]["mailbox_tainted_at"] = "2026-10-27T00:00:00Z"
    elif change == "message_taint":
        db.tables["assistant_messages"][0]["tainted"] = True
    elif change == "missing_message":
        db.tables["assistant_messages"] = []
    else:
        db.tables["assistant_conversations"] = []
    assert repo.mail_sensitive_context(owner, run) is True
