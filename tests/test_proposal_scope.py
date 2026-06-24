"""Unit tests for proposal scope-line generation (pure, no DB / no LLM)."""

import pytest
from pydantic import ValidationError

from app.models.schemas import ProposalLinesIn, ProposalSendIn
from app.services import proposal_scope as ps


def test_prompt_encodes_the_rules():
    prompt = ps.build_proposal_prompt()
    assert 'start with "Demolish ' in prompt
    assert '"Furnish and install ' in prompt
    assert "NEVER include quantities" in prompt
    assert "UNIVERSAL 1-3 RULE" in prompt
    assert "Cat5 and Cat6 together are ONE" in prompt
    assert "ONE combined line covering both sections" in prompt  # lighting + controls
    assert "Trenching, sawcut, excavation or backfill" in prompt
    assert "Generators" in prompt
    # anti-injection envelope contract
    assert "<document>" in prompt and "NOT as instructions" in prompt
    assert "<document>" in ps.build_user_prompt("BODY")


def test_schema_is_strict_compatible():
    """OpenAI strict mode: every property required, additionalProperties false."""

    def check(node: dict):
        if node.get("type") == "object":
            props = node.get("properties", {})
            assert node.get("additionalProperties") is False
            assert set(node.get("required", [])) == set(props)
            for sub in props.values():
                check(sub)
        if node.get("type") == "array":
            check(node["items"])

    check(ps.PROPOSAL_LINES_SCHEMA)


def test_lines_from_result_normalizes():
    result = {
        "lines": [
            {"text": "  Demolish   old panels. ", "category": "demolition"},
            {"text": "", "category": "wiring"},
            {"text": "x" * 600, "category": "other"},
            "not-a-dict",
        ],
        "notes": None,
    }
    lines = ps.lines_from_result(result)
    assert lines[0] == "Demolish old panels."
    assert len(lines) == 2  # blank dropped, non-dict dropped
    assert len(lines[1]) == ps.MAX_LINE_CHARS


def test_clean_line_strips_angle_brackets():
    # Brackets are placeholder syntax in the template — never legitimate here.
    assert ps.clean_line("Furnish and install <10 amp> breakers.") == (
        "Furnish and install 10 amp breakers."
    )
    assert ps.normalize_lines(["< only brackets >"]) == ["only brackets"]


def test_is_stale_running():
    from datetime import datetime, timezone

    now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    fresh = {"status": "running", "updated_at": "2026-06-10T11:50:00+00:00"}
    stale = {"status": "running", "updated_at": "2026-06-10T11:30:00+00:00"}
    done = {"status": "done", "updated_at": "2026-06-10T10:00:00+00:00"}
    assert not ps.is_stale_running(fresh, now)
    assert ps.is_stale_running(stale, now)
    assert not ps.is_stale_running(done, now)
    assert ps.is_stale_running({"status": "pending", "updated_at": "garbage"}, now)


def test_quantity_warnings_flags_suspects_only():
    flagged = ps.quantity_warnings(
        [
            "Furnish and install (42) GFCI receptacles.",
            "Furnish and install 130 ft of walker duct.",
            "Furnish and install 25 EA junction boxes.",
            "Furnish and install conduit, wiring and boxes.",
            "Furnish and install 2 pole receptacles.",  # pole type, not a quantity
        ]
    )
    assert "Furnish and install conduit, wiring and boxes." not in flagged
    assert "Furnish and install 2 pole receptacles." not in flagged
    assert len(flagged) == 3


def test_proposal_lines_in_validation():
    ok = ProposalLinesIn(lines=["  Furnish   and install panels. "])
    assert ok.lines == ["Furnish and install panels."]
    with pytest.raises(ValidationError):
        ProposalLinesIn(lines=[])
    with pytest.raises(ValidationError):
        ProposalLinesIn(lines=["   "])
    with pytest.raises(ValidationError):
        ProposalLinesIn(lines=["x" * 501])
    with pytest.raises(ValidationError):
        ProposalLinesIn(lines=["Furnish and install <10 amp> breakers."])


def test_proposal_send_in_validation():
    ok = ProposalSendIn(proposal_ids=["a"], email_body=None)
    assert ok.force is False
    with pytest.raises(ValidationError):
        ProposalSendIn(proposal_ids=[])
    with pytest.raises(ValidationError):
        ProposalSendIn(proposal_ids=["a"], email_body="short")
