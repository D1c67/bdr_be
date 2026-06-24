"""Pure-logic tests for project↔GC membership and the Send Out completion
record: membership is just the gc_id link, and the sent/skipped split written
at "Done sending" is the durable bid / did-not-bid evidence."""

import pytest
from pydantic import ValidationError

from app.models.schemas import ProjectGCIn
from app.services.proposal_send import send_out_outcome


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
    sent, skipped = send_out_outcome(GCS, {"g1", "g3"})
    assert sent == ["Alpha Builders", "Charlie GC"]
    # Never sent = decided not to bid to them.
    assert skipped == ["Bravo Construction"]


def test_outcome_all_sent_means_no_skips():
    sent, skipped = send_out_outcome(GCS, {"g1", "g2", "g3"})
    assert sent == ["Alpha Builders", "Bravo Construction", "Charlie GC"]
    assert skipped == []


def test_outcome_ignores_sends_to_gcs_no_longer_on_the_project():
    sent, skipped = send_out_outcome([GCS[0]], {"g1", "removed-gc"})
    assert sent == ["Alpha Builders"]
    assert skipped == []


def test_outcome_with_nothing_sent_skips_everyone():
    # complete_send_out refuses this case (≥1 sent required); the split itself
    # stays well-defined.
    sent, skipped = send_out_outcome(GCS, set())
    assert sent == []
    assert skipped == ["Alpha Builders", "Bravo Construction", "Charlie GC"]
