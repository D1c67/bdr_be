"""Sub-app feature flags — which of the three modules this deployment serves.

BDR is one deployment containing three sub-apps that share a `projects` table:
Bidding (the bid pipeline), Project Management (won work) and Certified Payroll
(prevailing-wage reporting). `BIDDING_ENABLED` / `PM_ENABLED` /
`CERTIFIED_PAYROLL_ENABLED` (all default true — see app/core/config.py) decide
which of them is reachable, so the whole codebase can ship to production while a
module that hasn't been tested there yet stays dark.

A disabled sub-app is GONE, not merely hidden. `require_feature` is attached to
every one of its routers in app/main.py, and because router-level dependencies
are solved BEFORE the endpoint's own, a disabled route 404s without ever reading
the caller's token — indistinguishable from a path that was never implemented.
The frontend mirrors this off GET /features, so the UI can never advertise a
module the API refuses to serve.

WHAT IS DELIBERATELY NOT GATED — the shared spine every sub-app hangs off:
/users, /notifications, /projects (PM and CP create and read rows in the same
table — though a few genuinely bidding-only routes on that router carry the flag
individually), /vendors, /gcs, /material-categories, /todos (a personal task
list with no project on it, merely reached from the Bidding nav today),
/projects/{id}/notification-log (opened from both the bidding side menu and the
PM rail), and /submittals — the company-global Submittal Bank, a standalone
reference library offered from both the Bidding and the PM nav. Gating any of
these would break the sub-apps that are still switched on.

Row-level data is never flag-gated either: a project won while PM was off still
exists, and `pm_only` / `cp_only` rows outlive the flag that created them. The
bidding surfaces that filter those stages out (analytics, due reminders, the
dashboard list) therefore keep doing so unconditionally.
"""

from enum import Enum

from fastapi import HTTPException, status

from app.core.config import get_settings


class SubApp(str, Enum):
    """The three modules a BDR deployment can serve."""

    BIDDING = "bidding"
    PM = "pm"
    CERTIFIED_PAYROLL = "certified_payroll"


def is_enabled(sub_app: SubApp) -> bool:
    """Whether this deployment serves the given sub-app (read live, not cached
    at import, so a test can monkeypatch settings and see it take effect)."""
    settings = get_settings()
    return {
        SubApp.BIDDING: settings.bidding_enabled,
        SubApp.PM: settings.pm_enabled,
        SubApp.CERTIFIED_PAYROLL: settings.certified_payroll_enabled,
    }[sub_app]


def enabled_map() -> dict[str, bool]:
    """The flag set as the frontend consumes it (GET /features)."""
    return {sub_app.value: is_enabled(sub_app) for sub_app in SubApp}


def feature_404(sub_app: SubApp) -> HTTPException:
    """The response a disabled sub-app gives.

    404 rather than 403 on purpose: a module this deployment does not serve
    should look like a path that does not exist, not like one the caller merely
    lacks permission for. The bare "Not Found" detail matches FastAPI's own
    unmatched-route body, so a disabled sub-app is not enumerable.
    """
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def home_path() -> str:
    """Frontend landing route of the first sub-app this deployment serves.

    Mirrors `homePath` in bdr_fe/lib/features.ts, for the places the backend has
    to build a link into the app without a browser to ask — chiefly the
    notification emails, whose links are permanent and land outside the app.
    """
    for sub_app, path in (
        (SubApp.BIDDING, "/dashboard"),
        (SubApp.PM, "/pm"),
        (SubApp.CERTIFIED_PAYROLL, "/payroll"),
    ):
        if is_enabled(sub_app):
            return path
    return "/dashboard"  # unreachable: config refuses to boot with all three off


# Notification types that belong to Project Management rather than Bidding.
#
# Declared explicitly because the `pm_` prefix is NOT a reliable discriminator:
# `submittal.response_received` is raised by PM's submittal ingestion and has
# always been routed to the bidding project page by the prefix test — a
# wrong-page bug on its own, and a dead link once a module is switched off.
# Both link builders (services/notification_email._deep_link and the frontend
# bell) route off this set so they cannot drift apart.
PM_NOTIFICATION_TYPES: frozenset[str] = frozenset(
    {
        "pm_activated",
        "pm_stage_change",
        "pm_outcome_conflict",
        "submittal.response_received",
    }
)


def notification_sub_app(type_: str | None) -> SubApp:
    """Which sub-app a notification type's project link belongs to."""
    return SubApp.PM if type_ in PM_NOTIFICATION_TYPES else SubApp.BIDDING


def require_feature(sub_app: SubApp):
    """Dependency factory gating a router on its sub-app's flag.

    Attached at include_router time in app/main.py — see the ROUTERS table
    there, which is the one place that records who owns what.
    """

    def _dep() -> None:
        if not is_enabled(sub_app):
            raise feature_404(sub_app)

    return _dep
