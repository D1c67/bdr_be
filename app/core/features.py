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
    """The flag set as the frontend consumes it (GET /features).

    `bid_file_splitter` is NOT a sub-app (no switcher tile, no home route, not
    counted by the at-least-one-sub-app boot validator) — it is an experimental
    standalone tool whose flag simply rides along here so the frontend learns
    about it the same way, with no rebuild when it flips. `rfp_ingest` (the
    RFP Ingestion sandbox, dev accounts only) rides along for the same reason.
    """
    settings = get_settings()
    flags = {sub_app.value: is_enabled(sub_app) for sub_app in SubApp}
    flags["bid_file_splitter"] = settings.bid_file_splitter_enabled
    flags["rfp_ingest"] = settings.rfp_ingest_enabled
    flags["rfp_email_ingest"] = settings.rfp_email_ingestion_enabled
    # The NGEM slice as the frontend gates it (the tab, the settings block,
    # the project-side link and the bells): the two switches only, the same
    # gate the /rfp-portal router uses. A configured account is NOT part of
    # it: the settings block has to show "not configured" and Run now has to
    # answer 503 on an unconfigured deployment, so the routes and the surface
    # stay reachable. The scheduler loop and the queue's claim pass keep
    # using `rfp_ngem_active` (docs/RFP_NGEM_PORTAL.md, section 11).
    flags["rfp_ngem"] = bool(settings.rfp_ingest_enabled and settings.rfp_ngem_enabled)
    # The BuildingConnected slice, same contract as rfp_ngem: the two
    # switches only (the settings block shows "not configured" and offers
    # Connect on a deployment without the APS client id and secret). The
    # scheduler keeps using `rfp_bc_active` (docs/RFP_BUILDINGCONNECTED.md).
    # getattr: fails closed on a settings stand-in without the field.
    flags["rfp_buildingconnected"] = bool(
        settings.rfp_ingest_enabled and getattr(settings, "rfp_bc_enabled", False)
    )
    # The RFP test bench (docs/RFP_TESTING.md 2): its own switch under the
    # master switch. The frontend additionally requires a dev profile with
    # the it_admin role, the same gate the /rfp-testing router enforces.
    flags["rfp_testing"] = bool(settings.rfp_testing_enabled and settings.rfp_ingest_enabled)
    return flags


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


def require_bid_file_splitter() -> None:
    """Router-level dependency gating /bid-splitter on its env flag.

    Same contract as require_feature: while BID_FILE_SPLITTER_ENABLED is false
    the routes 404 with the bare "Not Found" body before auth even runs, so a
    deployment that doesn't serve the tool is indistinguishable from one where
    it was never implemented.
    """
    if not get_settings().bid_file_splitter_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def require_rfp_ingest() -> None:
    """Router-level dependency gating /rfp-ingest on its env flag.

    Same contract as require_bid_file_splitter: while RFP_INGEST_ENABLED is
    false every route 404s with the bare "Not Found" body before auth runs
    (the router adds Depends(require_dev) per endpoint on top), so a
    deployment that does not serve the sandbox is indistinguishable from one
    where it was never implemented.
    """
    if not get_settings().rfp_ingest_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def require_feature(sub_app: SubApp):
    """Dependency factory gating a router on its sub-app's flag.

    Attached at include_router time in app/main.py — see the ROUTERS table
    there, which is the one place that records who owns what.
    """

    def _dep() -> None:
        if not is_enabled(sub_app):
            raise feature_404(sub_app)

    return _dep
