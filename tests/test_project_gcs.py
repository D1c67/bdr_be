"""Pure-logic tests for project↔GC membership and the Send Out completion
record: membership is just the gc_id link, and the emailed/external/skipped
split written at "Done sending" is the durable bid / did-not-bid evidence."""

import pytest
from pydantic import ValidationError

from app.models.schemas import ProjectGCIn
from app.services.proposal_send import build_done_sending_note, send_out_outcome


# ── schemas ────────────────────────────────────────────────────────────────


def test_membership_is_just_the_gc_id():
    assert ProjectGCIn(gc_id="g1").gc_id == "g1"


def test_gc_id_required():
    with pytest.raises(ValidationError):
        ProjectGCIn.model_validate({})


# ── send_out_outcome (the "Done sending" record) ───────────────────────────

GCS = [
    {"id": "g1", "name": "Alpha Builders"},
    {"id": "g2", "name": "Bravo Construction"},
    {"id": "g3", "name": "Charlie GC"},
]


def test_outcome_splits_sent_from_skipped():
    emailed, external, skipped = send_out_outcome(GCS, {"g1", "g3"})
    assert emailed == ["Alpha Builders", "Charlie GC"]
    assert external == []
    # Never sent = decided not to bid to them.
    assert skipped == ["Bravo Construction"]


def test_outcome_all_sent_means_no_skips():
    emailed, external, skipped = send_out_outcome(GCS, {"g1", "g2", "g3"})
    assert emailed == ["Alpha Builders", "Bravo Construction", "Charlie GC"]
    assert external == []
    assert skipped == []


def test_outcome_ignores_sends_to_gcs_no_longer_on_the_project():
    emailed, external, skipped = send_out_outcome([GCS[0]], {"g1", "removed-gc"})
    assert emailed == ["Alpha Builders"]
    assert external == []
    assert skipped == []


def test_outcome_with_nothing_sent_skips_everyone():
    # complete_send_out refuses this case (≥1 sent required); the split itself
    # stays well-defined.
    emailed, external, skipped = send_out_outcome(GCS, set())
    assert emailed == []
    assert external == []
    assert skipped == ["Alpha Builders", "Bravo Construction", "Charlie GC"]


def test_outcome_splits_external_from_emailed():
    # g3 was marked submitted through a third-party application — it counts as
    # sent (not skipped) but is reported separately from the emailed GCs.
    emailed, external, skipped = send_out_outcome(GCS, {"g1", "g3"}, {"g3"})
    assert emailed == ["Alpha Builders"]
    assert external == ["Charlie GC"]
    assert skipped == ["Bravo Construction"]


def test_outcome_all_external_is_a_valid_submission():
    emailed, external, skipped = send_out_outcome(GCS, {"g1", "g2"}, {"g1", "g2"})
    assert emailed == []
    assert external == ["Alpha Builders", "Bravo Construction"]
    assert skipped == ["Charlie GC"]


# ── build_done_sending_note (stage-event prose) ────────────────────────────


def test_note_lists_all_three_groups():
    note = build_done_sending_note(["Alpha"], ["Bravo"], ["Charlie"])
    assert note == (
        "Done sending — emailed: Alpha; "
        "submitted via third-party application: Bravo; "
        "skipped (no bid): Charlie"
    )


def test_note_omits_empty_groups():
    assert build_done_sending_note(["Alpha"], [], []) == "Done sending — emailed: Alpha"
    assert build_done_sending_note([], ["Bravo"], []) == (
        "Done sending — submitted via third-party application: Bravo"
    )
    assert build_done_sending_note([], [], []) == "Done sending"
