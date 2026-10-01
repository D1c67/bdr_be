"""The missing-intake-fields rule (docs/RFP_CREATE.md section 7).

A project the RFP creation step made arrives with its name, GC(s), actual bid
date and documents, and nothing else the manual New Project form requires:
the internal bid date, the two due dates and the nine Go/No-Go rubric
answers stay null for the Estimating Admin to fill. This module is the one
definition of "what is still missing", used by the creation notice, the
dashboard chip, the "Created from RFPs" page and the PATCH that clears the
task. Pure; no RFP dependency. The names are the `projects` column names so
the catalogs can label them.
"""

from __future__ import annotations

# The nine Go/No-Go rubric answers, in the order the form asks them.
RUBRIC_KEYS: tuple[str, ...] = (
    "project_type",
    "owner_type",
    "labor_needed",
    "bid_method",
    "competitor_known",
    "gc_known",
    "subs_needed",
    "est_value_band",
    "scope_fit",
)

DATE_KEYS: tuple[str, ...] = (
    "internal_bid_at",
    "due_from_estimator_at",
    "due_from_vendors_at",
)

# The pseudo-field for an actual bid date that came from a date-only source
# (stored at midnight Pacific): the day is known, the time is not.
BID_TIME_KEY = "bid_time"


def missing_intake_fields(project: dict, *, bid_time_unknown: bool = False) -> list[str]:
    """The intake fields still unanswered on `project`: the three dates, then
    the rubric keys, each in its fixed order, then `bid_time` last when the
    created row says the actual bid time is unknown and the project still
    carries an actual bid date (a cleared date has nothing to time)."""
    missing = [key for key in DATE_KEYS if project.get(key) is None]
    missing.extend(key for key in RUBRIC_KEYS if project.get(key) is None)
    if bid_time_unknown and project.get("actual_bid_at") is not None:
        missing.append(BID_TIME_KEY)
    return missing


def intake_complete(project: dict, *, bid_time_unknown: bool = False) -> bool:
    return not missing_intake_fields(project, bid_time_unknown=bid_time_unknown)
