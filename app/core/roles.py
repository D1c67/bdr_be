"""Role constants and the workflow stage definitions shared across the app."""

from enum import StrEnum


class Role(StrEnum):
    PM = "pm"
    PE = "pe"
    PA = "pa"
    EXECUTIVE = "executive"
    ACCOUNTANT = "accountant"
    IT_ADMIN = "it_admin"
    ESTIMATOR = "estimator"


# Internal roles see the dashboard and project status. The estimator is the sole
# external/untrusted role and is scoped to assigned projects only.
INTERNAL_ROLES = frozenset(
    {Role.PM, Role.PE, Role.PA, Role.EXECUTIVE, Role.ACCOUNTANT, Role.IT_ADMIN}
)

# The actual (to-GC) bid date is confidential: only these roles may see it.
# Project API responses null the field for everyone else (the rest of the team
# works against the internal bid date).
ACTUAL_BID_VIEWER_ROLES = frozenset({Role.PA, Role.EXECUTIVE, Role.IT_ADMIN})
