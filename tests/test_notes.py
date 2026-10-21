"""Pure-logic tests for the estimator-notes router helpers."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.routers.notes import (
    NOTE_MAX_CHARS,
    NoteIn,
    ReadIn,
    advance_read_mark,
    note_preview,
    recipient_ids,
    resolve_mentions,
)


# ── note_preview ───────────────────────────────────────────────────────────


def test_preview_passes_short_bodies_through():
    assert note_preview("Please double-check sheet E-3.") == "Please double-check sheet E-3."


def test_preview_flattens_whitespace_and_newlines():
    assert note_preview("line one\n\nline two\t tabbed") == "line one line two tabbed"


def test_preview_truncates_long_bodies_with_ellipsis():
    out = note_preview("word " * 100, limit=40)
    assert len(out) <= 40
    assert out.endswith("…")


# ── recipient_ids ──────────────────────────────────────────────────────────


def test_recipients_dedupe_across_groups_and_exclude_author():
    out = recipient_ids("me", ["a", "b", "me"], ["b", "c", ""])
    assert out == ["a", "b", "c"]


def test_recipients_empty_when_only_author():
    assert recipient_ids("me", ["me"], []) == []


# ── NoteIn validation ──────────────────────────────────────────────────────


def test_note_body_is_stripped():
    assert NoteIn(body="  hello  ").body == "hello"


def test_blank_note_rejected():
    with pytest.raises(ValidationError):
        NoteIn(body="   \n\t ")


def test_oversize_note_rejected():
    with pytest.raises(ValidationError):
        NoteIn(body="x" * (NOTE_MAX_CHARS + 1))


# ── advance_read_mark ──────────────────────────────────────────────────────


def _ts(hour: int) -> datetime:
    return datetime(2026, 6, 12, hour, 0, 0, tzinfo=timezone.utc)


def test_read_mark_set_from_scratch():
    assert advance_read_mark(None, _ts(10)) == _ts(10).isoformat()


def test_read_mark_advances_forward():
    assert advance_read_mark(_ts(9).isoformat(), _ts(10)) == _ts(10).isoformat()


def test_read_mark_never_moves_backwards():
    existing = _ts(11).isoformat()
    assert advance_read_mark(existing, _ts(10)) == existing


def test_read_mark_parses_supabase_offset_format():
    # timestamptz comes back as "+00:00"-suffixed ISO with microseconds.
    existing = "2026-06-12T11:00:00.123456+00:00"
    assert advance_read_mark(existing, _ts(10)) == existing


# ── ReadIn validation ──────────────────────────────────────────────────────


def test_read_in_accepts_aware_iso():
    assert ReadIn(up_to="2026-06-12T10:00:00+00:00").up_to == _ts(10)


def test_read_in_rejects_naive_datetimes():
    with pytest.raises(ValidationError):
        ReadIn(up_to="2026-06-12T10:00:00")


# ── resolve_mentions ───────────────────────────────────────────────────────

_PEOPLE = [
    {"id": "11111111-1111-1111-1111-111111111111", "full_name": "Ana Ruiz"},
    {"id": "22222222-2222-2222-2222-222222222222", "full_name": "Bo Chen"},
]
_ANA, _BO = _PEOPLE[0]["id"], _PEOPLE[1]["id"]


def test_mentions_kept_when_name_in_body():
    assert resolve_mentions("@Ana Ruiz and @Bo Chen please look", [_ANA, _BO], _PEOPLE) == [_ANA, _BO]


def test_mention_dropped_when_name_deleted_from_body():
    assert resolve_mentions("@Ana Ruiz please look", [_ANA, _BO], _PEOPLE) == [_ANA]


def test_mention_dropped_when_not_mentionable():
    stranger = "33333333-3333-3333-3333-333333333333"
    assert resolve_mentions("@Ana Ruiz", [stranger, _ANA], _PEOPLE) == [_ANA]


def test_mentions_ignore_case_and_dedupe():
    assert resolve_mentions("@ana ruiz hi", [_ANA, _ANA], _PEOPLE) == [_ANA]


def test_note_in_accepts_mention_ids():
    assert [str(u) for u in NoteIn(body="hi", mention_ids=[_ANA]).mention_ids] == [_ANA]
    with pytest.raises(ValidationError):
        NoteIn(body="hi", mention_ids=["not-a-uuid"])
