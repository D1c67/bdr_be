"""The revise group of the independent review (docs/AI_ESTIMATING_BUILD_SPEC.md
5.8 REVISE_SCHEMA, WS7 file list): the builder's one bounded turn with no
tools that answers the review findings with dispositions (apply, modify,
decline) which the review chain applies through `plans_reader_ops`.

The implementation lives in `plans_reader_reviewer` next to the review
group (the two share the pack text, the evidence verification and the op
validation). This module is the seam the spine resolves for the `revise`
kind (`plans_reader_agents._LOOP_SEAMS["revise"]`) and re-exports every
public name of that seam, so both import paths stay valid:

    from app.services import plans_reader_revise as revise
    from app.services import plans_reader_reviewer as rv

    revise.REVISE_SYSTEM is rv.REVISE_SYSTEM
    revise.build_revise_loop is rv.build_revise_loop

Nothing here calls a provider or the database.
"""

from __future__ import annotations

from app.services.plans_reader_review_schemas import REVISE_SCHEMA
from app.services.plans_reader_reviewer import (
    REVISE_FEATURE,
    REVISE_KIND,
    REVISE_MAX_ROUNDS,
    REVISE_MAX_TOOL_CALLS,
    REVISE_STEP,
    REVISE_SYSTEM,
    ReviewerUnavailable,
    build_revise_loop,
    finalize_revise_loop,
    revise_budget,
    revise_pack_text,
    validate_dispositions,
)

__all__ = [
    "REVISE_SCHEMA",
    "REVISE_SYSTEM",
    "REVISE_FEATURE",
    "REVISE_KIND",
    "REVISE_STEP",
    "REVISE_MAX_ROUNDS",
    "REVISE_MAX_TOOL_CALLS",
    "ReviewerUnavailable",
    "revise_budget",
    "revise_pack_text",
    "build_revise_loop",
    "validate_dispositions",
    "finalize_revise_loop",
]
