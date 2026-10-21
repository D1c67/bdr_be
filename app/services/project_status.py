"""Single source of truth for a project's derived lifecycle status.

`status` is never stored — it is computed from (abandoned_at, current_stage, and
the recorded bid outcome) so the dashboard, the project API, and analytics can
never disagree. Only the abandon marker (abandoned_at / abandoned_by) is
persisted; see migration 0039. Abandon preserves current_stage, so a reactivated
project resumes exactly where it left off. Failed to bid (0162) is an abandon
that also stamps failed_to_bid_at, which turns the status into `failed_to_bid`.
"""

from typing import Literal

ProjectStatus = Literal[
    "active",
    "sent",
    "won",
    "lost",
    "no_award",
    "declined",
    "abandoned",
    "failed_to_bid",
    "no_bid",
]


def derive_status(
    current_stage: str | None,
    abandoned_at: object | None,
    outcome_result: str | None,
    failed_to_bid_at: object | None = None,
) -> ProjectStatus:
    """Collapse stage + abandon flag + recorded bid outcome into one status.

    Abandon wins over everything (it can happen at any stage); an abandon that
    was a missed bid (failed_to_bid_at set) reads `failed_to_bid`. Pass
    failed_to_bid_at wherever the row has it, or a failed bid reads as a plain
    abandon. Otherwise status
    follows the pipeline's terminal facts — declined at Go/No-Go, the win/loss
    result once recorded, `sent` while a submitted bid awaits its outcome — and
    defaults to `active` for any in-flight stage.
    """
    if abandoned_at:
        return "failed_to_bid" if failed_to_bid_at else "abandoned"
    # Created directly in Project Management (pm_only) or imported as a payroll-
    # only job from the legacy Certified Payroll app (cp_only) — never entered
    # the bidding pipeline, so no bid status applies (bidding surfaces exclude
    # these rows entirely).
    if current_stage in ("pm_only", "cp_only"):
        return "no_bid"
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
