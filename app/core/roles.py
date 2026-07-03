"""Role constants and the workflow stage definitions shared across the app."""

from enum import StrEnum


class Role(StrEnum):
    ESTIMATING_ENGINEER = "estimating_engineer"  # merge of the former PM + PE roles
    ESTIMATING_ADMIN = "estimating_admin"  # the former PA role, renamed
    EXECUTIVE = "executive"
    ACCOUNTANT = "accountant"
    IT_ADMIN = "it_admin"
    ESTIMATOR = "estimator"


# Roles that may perform any pipeline step / write. Everyone internal can act on
# any stage (the per-stage owner is just a "whose task" hint now) EXCEPT the
# accountant (read-only) and the estimator (external, narrowly scoped).
WRITER_ROLES = frozenset(
    {Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER, Role.EXECUTIVE, Role.IT_ADMIN}
)

# Internal roles see the dashboard, project status and all read views. This is
# the writer set plus the read-only accountant. The estimator is the sole
# external/untrusted role and is scoped to assigned projects only.
INTERNAL_ROLES = WRITER_ROLES | {Role.ACCOUNTANT}

# Only these roles may edit/commit the Verify step. Executive is the verifier;
# IT Admin is retained as the system/superuser override.
VERIFY_ROLES = frozenset({Role.EXECUTIVE, Role.IT_ADMIN})

# Who must review estimator revision rounds (post-hand-off file changes): they
# get the high-importance alert email + bell row, and the per-user red banner
# on the project until they press "Mark as reviewed". Everyone who works the
# bid — not the read-only accountant, not the IT Admin override account.
CHANGE_REVIEW_ROLES = frozenset(
    {Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER, Role.EXECUTIVE}
)

# The actual (to-GC) bid date/amount is confidential: only these roles may see it.
# Project API responses null the field for everyone else (the rest of the team
# works against the internal bid date). The accountant is read-only but may view
# these figures. Editing the actual bid stays with the writer subset below.
ACTUAL_BID_VIEWER_ROLES = frozenset(
    {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN, Role.ACCOUNTANT}
)
ACTUAL_BID_EDITOR_ROLES = frozenset(
    {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}
)
