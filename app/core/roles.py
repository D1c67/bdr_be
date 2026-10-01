"""Role constants and the workflow stage definitions shared across the app."""

from enum import StrEnum


class Role(StrEnum):
    # The former single Estimating Engineer role (itself a merge of PM + PE) is
    # split by FOCUS: identical permissions, different default to-do list.
    # Materials owns the material-numbers lane; Labor owns the labor-numbers
    # lane plus the engineer-owned send-out tasks.
    ESTIMATING_ENGINEER_MATERIALS = "estimating_engineer_materials"
    ESTIMATING_ENGINEER_LABOR = "estimating_engineer_labor"
    ESTIMATING_ADMIN = "estimating_admin"  # the former PA role, renamed
    EXECUTIVE = "executive"
    ACCOUNTANT = "accountant"
    IT_ADMIN = "it_admin"
    ESTIMATOR = "estimator"


# Both engineer focuses — for audiences that mean "the engineers" regardless of
# materials/labor focus (permissions, cross-cutting alerts, note recipients).
ESTIMATING_ENGINEER_ROLES = frozenset(
    {Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR}
)

# Roles that may perform any pipeline step / write. Everyone internal can act on
# any stage (the per-stage owner is just a "whose task" hint now) EXCEPT the
# accountant (read-only) and the estimator (external, narrowly scoped).
WRITER_ROLES = frozenset(
    {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN}
) | ESTIMATING_ENGINEER_ROLES

# Internal roles see the dashboard, project status and all read views. This is
# the writer set plus the read-only accountant. The estimator is the sole
# external/untrusted role and is scoped to assigned projects only.
INTERNAL_ROLES = WRITER_ROLES | {Role.ACCOUNTANT}

# Only these roles may edit/commit the Verify step. Executive is the verifier;
# IT Admin is retained as the system/superuser override.
VERIFY_ROLES = frozenset({Role.EXECUTIVE, Role.IT_ADMIN})

# Who may RENAME, reorder or retire a material category (the Contacts →
# Categories tab). ADDING one is open to every writer — the BOQ and PM panels
# create a bucket inline — but editing one rewrites the taxonomy under live
# projects, so it stays with the Executive and the IT Admin override.
CATEGORY_ADMIN_ROLES = frozenset({Role.EXECUTIVE, Role.IT_ADMIN})

# Who must review estimator revision rounds (post-hand-off file changes): they
# get the high-importance alert email + bell row, and the per-user red banner
# on the project until they press "Mark as reviewed". Everyone who works the
# bid — not the read-only accountant, not the IT Admin override account.
CHANGE_REVIEW_ROLES = frozenset(
    {Role.ESTIMATING_ADMIN, Role.EXECUTIVE}
) | ESTIMATING_ENGINEER_ROLES

# Project Management (PM) module access. Today these are identical to the bidding
# sets — the accountant reads everything, writers write, and the external
# estimator has ZERO PM access (never in INTERNAL_ROLES). Kept as distinct names
# so future PM-specific roles are added by editing these two lines only.
PM_READ_ROLES = INTERNAL_ROLES
PM_WRITE_ROLES = WRITER_ROLES

# Certified Payroll (CP) module access. Same shape and rationale as the PM sets:
# identical to the bidding sets today, distinct names so tightening CP access
# (it holds employee SSN last-4 and pay data) is a two-line edit here.
CP_READ_ROLES = INTERNAL_ROLES
CP_WRITE_ROLES = WRITER_ROLES

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

# Who works the RFP Ingestion review queue (docs/RFP_EMAIL_INGESTION.md section
# 1): the Estimating Admin primarily, with the IT Admin, the Executive and both
# engineer focuses able to act. routers/rfp_emails.py keeps REVIEW_QUEUE_ROLES
# equal to this tuple; it lives here so routers/projects.py can gate the email
# block of the project-side match route without a router-to-router import.
# The accountant is read-only and the estimator is external, so neither is
# here: email content never leaves this set.
RFP_REVIEW_ROLES = (
    Role.ESTIMATING_ADMIN,
    Role.IT_ADMIN,
    Role.EXECUTIVE,
    Role.ESTIMATING_ENGINEER_MATERIALS,
    Role.ESTIMATING_ENGINEER_LABOR,
)

# Who may READ the queue (docs/RFP_EMAIL_VISIBILITY.md 3.3). Wider than the
# set above by exactly the accountant: since 0134 every read is scoped to the
# mailboxes the viewer owns plus the shared list, so letting the read-only
# accountant open the page shows them their own mail and nothing else. They
# still hit RFP_REVIEW_ROLES (403) on every action.
RFP_VIEW_ROLES = RFP_REVIEW_ROLES + (Role.ACCOUNTANT,)

# Who may DELETE a project (docs/PROJECT_DELETE.md 3): the same three roles
# that clear rows on the "Created from RFP Ingestion" page
# (routers/rfp_created.PAGE_ROLES). Only the IT Admin sees the Deleted
# Projects page and restores.
PROJECT_DELETE_ROLES = (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN)
PROJECT_RESTORE_ROLES = (Role.IT_ADMIN,)
