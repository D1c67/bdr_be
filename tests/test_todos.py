"""Pure-logic tests for the to-dos router models and PATCH translation."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.todos import (
    NUDGE_COOLDOWN,
    TODO_MAX_CHARS,
    TodoIn,
    TodoPatch,
    _nudge_blocked,
    db_patch,
)


# ── TodoIn validation ──────────────────────────────────────────────────────


def test_title_is_stripped():
    assert TodoIn(title="  call the inspector  ").title == "call the inspector"


def test_blank_title_rejected():
    with pytest.raises(ValidationError):
        TodoIn(title="   \n\t ")


def test_oversize_title_rejected():
    with pytest.raises(ValidationError):
        TodoIn(title="x" * (TODO_MAX_CHARS + 1))


def test_due_date_parses_iso_string():
    assert TodoIn.model_validate(
        {"title": "x", "due_date": "2026-06-12"}
    ).due_date == date(2026, 6, 12)


# ── db_patch (PATCH body → DB update) ──────────────────────────────────────


def test_empty_patch_rejected():
    with pytest.raises(HTTPException) as exc:
        db_patch(TodoPatch())
    assert exc.value.status_code == 400


def test_explicit_null_title_rejected():
    with pytest.raises(HTTPException) as exc:
        db_patch(TodoPatch.model_validate({"title": None}))
    assert exc.value.status_code == 400


def test_omitted_due_date_left_untouched():
    assert db_patch(TodoPatch(title=" buy wire ")) == {"title": "buy wire"}


def test_explicit_null_clears_due_date():
    assert db_patch(TodoPatch.model_validate({"due_date": None})) == {"due_date": None}


def test_due_date_serialized_for_db():
    assert db_patch(TodoPatch(due_date=date(2026, 6, 12))) == {"due_date": "2026-06-12"}


def test_marking_done_stamps_completed_at():
    patch = db_patch(TodoPatch(is_done=True))
    assert patch["is_done"] is True
    assert patch["completed_at"]  # ISO timestamp, set server-side


def test_reopening_clears_completed_at():
    assert db_patch(TodoPatch(is_done=False)) == {
        "is_done": False,
        "completed_at": None,
    }


# ── _nudge_blocked (Nudge cooldown) ────────────────────────────────────────

_NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


def test_never_nudged_is_not_blocked():
    assert _nudge_blocked(None, _NOW) is False


def test_nudged_within_cooldown_is_blocked():
    assert _nudge_blocked(_NOW - timedelta(hours=1), _NOW) is True


def test_nudged_after_cooldown_is_allowed():
    assert _nudge_blocked(_NOW - NUDGE_COOLDOWN - timedelta(minutes=1), _NOW) is False
