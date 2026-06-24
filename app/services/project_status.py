"""Single source of truth for a project's derived lifecycle status.

`status` is never stored — it is computed from (abandoned_at, current_stage, and
the recorded bid outcome) so the dashboard, the project API, and analytics can
never disagree. Only the abandon marker (abandoned_at / abandoned_by) is
persisted; see migration 0039. Abandon preserves current_stage, so a reactivated
project resumes exactly where it left off.
"""

from typing import Literal

ProjectStatus = Literal[
    "active", "sent", "won", "lost", "no_award", "declined", "abandoned"
]


def derive_status(
    current_stage: str | None,
    abandoned_at: object | None,
    outcome_result: str | None,
) -> ProjectStatus:
    """Collapse stage + abandon flag + recorded bid outcome into one status.

    Abandon wins over everything (it can happen at any stage). Otherwise status
    follows the pipeline's terminal facts — declined at Go/No-Go, the win/loss
    result once recorded, `sent` while a submitted bid awaits its outcome — and
    defaults to `active` for any in-flight stage.
    """
    if abandoned_at:
        return "abandoned"
    if current_stage == "declined":
        return "declined"
    if current_stage == "bid_outcome":
        if outcome_result in ("won", "lost", "no_award"):
            return outcome_result  # type: ignore[return-value]
        # Reached bid_outcome but the outcome row is missing (shouldn't happen) —
        # treat it as still awaiting rather than inventing a win/loss.
        return "sent"
    if current_stage == "submitted":
        return "sent"
    return "active"
