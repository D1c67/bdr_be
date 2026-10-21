"""Failed to bid (0162): request validation and the team email body."""

import pytest
from pydantic import ValidationError

from app.models.schemas import FailedToBidIn
from app.services import failed_to_bid_email
from app.services.failed_to_bid_email import REASON_LABELS, render_failed_to_bid_email, subject_for


def test_reason_and_note_required():
    with pytest.raises(ValidationError):
        FailedToBidIn(reason="missed_deadline", note="   ")
    with pytest.raises(ValidationError):
        FailedToBidIn(reason="not_a_reason", note="x")
    body = FailedToBidIn(reason="late_plans", note="  Plans came Friday night.  ")
    assert body.note == "Plans came Friday night."


def test_every_schema_reason_has_a_label():
    for reason in FailedToBidIn.model_fields["reason"].annotation.__args__:
        assert reason in REASON_LABELS


def test_email_names_project_reason_and_note():
    project = {"name": "Van Ness Tower", "number": "26-014"}
    assert subject_for(project) == "Failed to bid: #26-014 Van Ness Tower"
    html = render_failed_to_bid_email(
        recipient_name="Pat",
        project=project,
        stage_label="Markup",
        reason="missing_quotes",
        note="Gear quote <never> came back.",
        marked_by="Sam Lee",
        cta_url="https://example.test/projects/p1",
    )
    assert "marked as Failed to bid by Sam Lee" in html
    assert "Vendor quotes not received in time" in html
    assert "Stage: Markup" in html
    assert "&lt;never&gt;" in html  # user text is escaped


def test_queue_is_noop_when_emails_disabled(monkeypatch):
    started = []
    monkeypatch.setattr(failed_to_bid_email.threading, "Thread",
                        lambda *a, **k: started.append(1))
    failed_to_bid_email.queue_failed_to_bid("p1", None, "other", "x", None)
    assert started == []


def test_failed_to_bid_is_its_own_status():
    from app.services.project_status import derive_status

    assert derive_status("markup", "2026-10-20T00:00:00Z", None) == "abandoned"
    assert (
        derive_status("markup", "2026-10-20T00:00:00Z", None, "2026-10-20T00:00:00Z")
        == "failed_to_bid"
    )
    # Reactivated (abandon marker cleared) wins back the stage-derived status.
    assert derive_status("markup", None, None, None) == "active"
