"""The assistant firewall (docs/ASSISTANT.md, "The shim is never the served
app" and Permissions item 4): pure decisions over (method, path_format).

Three rules, in order:

  1. method DELETE is refused outright, whatever the route;
  2. the admin deny-list (DENY_LIST) is refused regardless of role, so an
     IT Admin's assistant cannot reach what the IT Admin can;
  3. anything the registry allowlist does not declare is refused.

Entries are exact (method, path_format) pairs as FastAPI reports them
(`APIRoute.path_format`, the template with `{param}` placeholders), never
raw request paths, because the shim matches the request against
app.main.app.router first and compares the matched route. Every pair below
was checked against the route table on 2026-10-21; tests/test_assistant_shim
walks app.main.app.routes and fails when a pair goes stale or when a route
carrying an IT Admin only or dev only dependency is missing here.

The worker protocol prefix (/agent-worker/v1, protocol.API_PREFIX) is listed
pair by pair and also covered by DENY_PATH_PREFIXES, so a route added to the
worker API later is refused before anyone updates this file.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

RoutePair = tuple[str, str]

REFUSED_METHODS: frozenset[str] = frozenset({"DELETE"})

# path_format prefixes refused whole, in addition to the explicit pairs.
DENY_PATH_PREFIXES: tuple[str, ...] = ("/agent-worker/v1/", "/assistant/outlook", "/assistant/calendar/", "/assistant/mailbox-access", "/assistant/evidence-access", "/assistant/inspection/", "/assistant/browser-credentials", "/assistant/browser-sites", "/assistant/inbox", "/assistant/alerts",
                                     # Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.6, 6.5, 9): holds, live
                                     # sign-in and result carry are the person's own clicks. The model-facing
                                     # /assistant/browser-requests stays assistant-callable.
                                     "/assistant/browser-holds", "/assistant/browser-live", "/assistant/browser-results")

_PERSONAL_BROWSER: tuple[RoutePair, ...] = (
    ("POST", "/assistant/inspection/browser-tasks/{job_id}/read"),
    ("POST", "/assistant/inspection/browser-tasks/{job_id}/records/{record_id}/content"),
    ("GET", "/assistant/browser-tasks"),
    ("GET", "/assistant/browser-tasks/{job_id}/trace"),
    ("GET", "/assistant/browser-tasks/{job_id}/records/{record_id}/content"),
    ("GET", "/assistant/browser-tasks/{job_id}/help"),
    ("POST", "/assistant/browser-tasks/{job_id}/help"),
    ("GET", "/assistant/browser-tasks/{job_id}/views"),
    # Browser v2 human routes (WI-4), also under the denied prefixes above.
    ("GET", "/assistant/browser-sites"),
    ("GET", "/assistant/browser-sites/signins"),
    ("GET", "/assistant/browser-sites/grants"),
    ("POST", "/assistant/browser-sites/grants"),
    ("DELETE", "/assistant/browser-sites/grants/{grant_id}"),
    ("GET", "/assistant/browser-sites/sessions"),
    ("DELETE", "/assistant/browser-sites/sessions/{session_id}"),
    ("GET", "/assistant/browser-sites/safe-rules"),
    ("DELETE", "/assistant/browser-sites/safe-rules/{rule_id}"),
    ("GET", "/assistant/browser-sites/requests/{request_id}"),
    ("POST", "/assistant/browser-sites/requests/{request_id}/decision"),
    ("GET", "/assistant/browser-credentials"),
    ("PUT", "/assistant/browser-credentials/{site_key}"),
    ("DELETE", "/assistant/browser-credentials/{site_key}"),
    ("GET", "/assistant/browser-holds/{hold_id}"),
    ("POST", "/assistant/browser-holds/{hold_id}/decision"),
    ("POST", "/assistant/browser-holds/{hold_id}/safe-rule"),
    ("POST", "/assistant/browser-holds/start/{admission_id}/decision"),
    ("POST", "/assistant/browser-results/{message_id}/carry"),
)

_USERS: tuple[RoutePair, ...] = (
    ("POST", "/users"),
    ("POST", "/users/{user_id}/reinvite"),
    ("POST", "/users/{user_id}/reset-mfa"),
    ("POST", "/users/{user_id}/reset-password"),
    ("PATCH", "/users/me/role"),
    ("PATCH", "/users/{user_id}"),
    ("DELETE", "/users/{user_id}"),
)

_BROWSER_AGENTS: tuple[RoutePair, ...] = (
    ("POST", "/browser-agents/jobs/{job_id}/retry"),
    ("POST", "/browser-agents/jobs/{job_id}/cancel"),
    ("POST", "/browser-agents/help/{help_id}/messages"),
    ("POST", "/browser-agents/help/{help_id}/files"),
    ("POST", "/browser-agents/help/{help_id}/claim"),
    ("POST", "/browser-agents/help/{help_id}/unclaim"),
    ("GET", "/browser-agents/admin/state"),
    ("GET", "/browser-agents/admin/models"),
    ("GET", "/browser-agents/admin/history"),
    ("PATCH", "/browser-agents/admin/settings"),
    ("POST", "/browser-agents/admin/spend/reset"),
    ("PATCH", "/browser-agents/admin/types/{key}"),
    ("POST", "/browser-agents/admin/types/{key}/probe"),
    ("PUT", "/browser-agents/admin/secrets/{name}"),
    ("DELETE", "/browser-agents/admin/secrets/{name}"),
    ("POST", "/browser-agents/admin/enrollments"),
    ("PATCH", "/browser-agents/admin/workers/{worker_id}"),
    ("POST", "/browser-agents/admin/workers/{worker_id}/approve"),
    ("POST", "/browser-agents/admin/workers/{worker_id}/revoke"),
    ("POST", "/browser-agents/admin/canary"),
    ("POST", "/browser-agents/admin/stop-running"),
)

_AGENT_WORKER: tuple[RoutePair, ...] = (
    ("GET", "/agent-worker/v1/canary/page"),
    ("GET", "/agent-worker/v1/config"),
    ("GET", "/agent-worker/v1/installer.ps1"),
    ("GET", "/agent-worker/v1/jobs/{job_id}/help/{help_id}/messages"),
    ("GET", "/agent-worker/v1/releases/{version}"),
    ("POST", "/agent-worker/v1/enroll"),
    ("POST", "/agent-worker/v1/hello"),
    ("POST", "/agent-worker/v1/heartbeat"),
    ("POST", "/agent-worker/v1/claim"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/steps"),
    ("PUT", "/agent-worker/v1/jobs/{job_id}/checkpoint"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/artifacts"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/finish"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/requeue"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/park"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/turns"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/identities/{identity}/session"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/identities/{identity}/login/begin"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/identities/{identity}/login/result"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/rfi-capture"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/help"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/help/{help_id}/consume"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/help/{help_id}/messages"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/help/{help_id}/secrets/{message_id}/use"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/contributions"),
    ("POST", "/agent-worker/v1/user/heartbeat"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/steps"),
    ("PUT", "/agent-worker/v1/jobs/{job_id}/user/checkpoint"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/artifacts"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/turns"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/finish"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/requeue"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/park"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/help"),
    ("GET", "/agent-worker/v1/jobs/{job_id}/user/help/{help_id}/messages"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/help/{help_id}/messages"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/help/{help_id}/consume"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/help/{help_id}/secrets/{message_id}/use"),
    # Browser v2 worker routes (WI-6, protocol.USER_V2_ENDPOINTS); the prefix rule already denies them.
    ("GET", "/agent-worker/v1/jobs/{job_id}/user/policy"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/holds"),
    ("GET", "/agent-worker/v1/jobs/{job_id}/user/holds/{hold_id}/release"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/site-hold"),
    ("GET", "/agent-worker/v1/jobs/{job_id}/user/session"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/login/begin"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/login/result"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user/downloads"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user-identities/{identity}/session"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user-identities/{identity}/login/begin"),
    ("POST", "/agent-worker/v1/jobs/{job_id}/user-identities/{identity}/login/result"),
)

_AUTO_SEND: tuple[RoutePair, ...] = (
    ("PATCH", "/auto-send/settings/enabled"),
    ("PATCH", "/auto-send/settings/markup"),
    ("POST", "/projects/{project_id}/auto-send/send-now"),
    ("POST", "/projects/{project_id}/auto-send/accept"),
)

_AI_SPEND: tuple[RoutePair, ...] = (
    ("PATCH", "/ai-spend/cap"),
    ("POST", "/ai-spend/reset"),
)

# Dev only: the LLM monitor and the Training surface, reads included.
_DEV_ONLY: tuple[RoutePair, ...] = (
    ("GET", "/llm-monitor/summary"),
    ("GET", "/llm-monitor/failures"),
    ("GET", "/llm-monitor/queue"),
    ("POST", "/llm-monitor/jobs/{job_id}/retry"),
    ("POST", "/llm-monitor/jobs/{job_id}/cancel"),
    ("GET", "/training/boq"),
    ("GET", "/training/boq/export"),
    ("GET", "/training/boq/{example_id}"),
    ("GET", "/training/boq/{example_id}/gold"),
    ("PATCH", "/training/boq/{example_id}/review"),
    ("GET", "/training/bid-split"),
    ("GET", "/training/bid-split/{example_id}"),
    ("PATCH", "/training/bid-split/{example_id}/review"),
    ("GET", "/rfp-ingest/status"),
    ("GET", "/rfp-ingest/emails"),
    ("GET", "/rfp-ingest/runs"),
    ("GET", "/rfp-ingest/runs/{run_id}"),
    ("GET", "/rfp-ingest/files/{file_id}"),
    ("GET", "/rfp-ingest/files/{file_id}/pages"),
    ("GET", "/rfp-ingest/files/{file_id}/urls"),
    ("GET", "/rfp-ingest/pages/{page_id}/urls"),
    ("POST", "/rfp-ingest/runs"),
    ("POST", "/rfp-ingest/runs/{run_id}/files"),
    ("POST", "/rfp-ingest/runs/{run_id}/start"),
    ("POST", "/rfp-ingest/runs/{run_id}/cancel"),
    ("POST", "/rfp-ingest/runs/{run_id}/retry"),
    ("DELETE", "/rfp-ingest/runs/{run_id}"),
    # The RFP testing bench: a dev account in the IT Admin role.
    ("GET", "/rfp-testing/state"),
    ("GET", "/rfp-testing/sessions/{session_id}/emails"),
    ("GET", "/rfp-testing/sessions/{session_id}/ignored"),
    ("GET", "/rfp-testing/sessions/{session_id}/filed"),
    ("GET", "/rfp-testing/sessions/{session_id}/mail-out"),
    ("GET", "/rfp-testing/sessions/{session_id}/projects"),
    ("GET", "/rfp-testing/projects/{project_id}"),
    ("POST", "/rfp-testing/sessions"),
    ("POST", "/rfp-testing/sessions/{session_id}/end"),
    ("PATCH", "/rfp-testing/sessions/{session_id}"),
    ("POST", "/rfp-testing/sessions/{session_id}/cleanup"),
)

_DELETED_PROJECTS: tuple[RoutePair, ...] = (
    ("GET", "/deleted-projects"),
    ("GET", "/deleted-projects/summary"),
    ("POST", "/deleted-projects/{archive_id}/restore"),
)

_PROJECTS: tuple[RoutePair, ...] = (
    ("DELETE", "/projects/{project_id}"),
    ("POST", "/projects/{project_id}/delete"),
    ("POST", "/projects/{project_id}/merge-preview"),
    ("POST", "/projects/{project_id}/merge"),
    ("POST", "/projects/{project_id}/rfp-matches/{match_id}/unmerge"),
    ("POST", "/projects/{project_id}/abandon"),
    ("POST", "/projects/{project_id}/failed-to-bid"),
    ("POST", "/projects/{project_id}/outcome"),
    ("POST", "/projects/{project_id}/verify"),
    ("POST", "/projects/{project_id}/proposals/complete-send-out"),
    ("POST", "/projects/{project_id}/gono/decide"),
    ("POST", "/projects/{project_id}/gono/undo"),
    ("POST", "/projects/{project_id}/assignments/{assignment_id}/revoke"),
    # The design record writes this one as DELETE /rfqs/{rfq_id}/select; the
    # route is mounted under the project (rfqs.py, DELETE .../select).
    ("DELETE", "/projects/{project_id}/rfqs/{rfq_id}/select"),
)

_EXTERNAL_SUBMISSION: tuple[RoutePair, ...] = (
    ("POST", "/projects/{project_id}/external-submission"),
    ("POST", "/projects/{project_id}/external-submission/undo"),
    ("POST", "/projects/{project_id}/external-submission/requests/{request_id}/cancel"),
    ("POST", "/projects/{project_id}/external-submission/requests/{request_id}/grant"),
    ("POST", "/projects/{project_id}/external-submission/requests/{request_id}/deny"),
    ("POST", "/projects/{project_id}/external-submission/requests/{request_id}/revoke"),
)

_RFP_EMAILS: tuple[RoutePair, ...] = (
    ("POST", "/rfp-emails/{email_id}/match/merge"),
    ("POST", "/rfp-emails/{email_id}/match/unmerge"),
    # IT Admin Settings: blocked and authorized senders.
    ("GET", "/rfp-emails/blocked-senders"),
    ("POST", "/rfp-emails/blocked-senders"),
    ("DELETE", "/rfp-emails/blocked-senders/{block_id}"),
    ("POST", "/rfp-emails/{email_id}/block-sender"),
    ("POST", "/rfp-emails/authorized-senders"),
    ("DELETE", "/rfp-emails/authorized-senders/{rule_id}"),
)

# IT Admin Settings writes: the 0150 model box switch, the BuildingConnected
# GC aliases, and the PM RFI sync link and sync actions.
_IT_ADMIN_SETTINGS: tuple[RoutePair, ...] = (
    ("GET", "/model-status/target"),
    ("PUT", "/model-status/target"),
    ("GET", "/rfp-portal/buildingconnected/gc-aliases"),
    ("PATCH", "/rfp-portal/buildingconnected/gc-aliases/{alias_id}"),
    ("DELETE", "/rfp-portal/buildingconnected/gc-aliases/{alias_id}"),
    ("PUT", "/pm/projects/{project_id}/pm-link"),
    ("POST", "/pm/projects/{project_id}/rfis/sync"),
    ("POST", "/pm/projects/{project_id}/rfis/{rfi_id}/link"),
    ("POST", "/pm/projects/{project_id}/rfis/{rfi_id}/unlink"),
)

# Decision 19: certified payroll is aggregates only, so every write under
# the payroll routers is refused (there is no certify route).
_PAYROLL: tuple[RoutePair, ...] = (
    ("POST", "/payroll/projects"),
    ("POST", "/payroll/projects/{project_id}/enroll"),
    ("PATCH", "/payroll/projects/{project_id}"),
    ("DELETE", "/payroll/projects/{project_id}/enrollment"),
    ("POST", "/payroll/reports"),
    ("DELETE", "/payroll/reports/{report_id}"),
    ("POST", "/payroll/reports/{report_id}/timesheet"),
    ("POST", "/payroll/reports/{report_id}/payroll-detail"),
    ("POST", "/payroll/reports/{report_id}/rematch"),
    ("POST", "/payroll/reports/{report_id}/finalize"),
    ("POST", "/payroll/reports/{report_id}/submit"),
    ("POST", "/payroll/reports/{report_id}/cpr/generate"),
    ("POST", "/payroll/reports/{report_id}/cpr/paper-data"),
    ("POST", "/payroll/employees"),
    ("PATCH", "/payroll/employees/{employee_id}"),
    ("DELETE", "/payroll/employees/{employee_id}"),
    ("POST", "/payroll/employees/{employee_id}/documents"),
    ("DELETE", "/payroll/employees/{employee_id}/documents/{doc_id}"),
    ("POST", "/payroll/classifications"),
    ("PATCH", "/payroll/classifications/{classification_id}"),
    ("POST", "/payroll/rates"),
    ("PATCH", "/payroll/rates/{rate_id}"),
    ("PUT", "/payroll/settings"),
    ("PUT", "/payroll/profile"),
    ("POST", "/payroll/ignored-projects"),
    ("DELETE", "/payroll/ignored-projects/{ignored_id}"),
)

# The assistant's own controls (docs/ASSISTANT_PHASE1_PLAN.md 3.8): an
# assistant never approves, rejects, reverts or resolves its own cards,
# never changes its own settings and never starts a conversation or a turn.
_ASSISTANT_ROUTES: tuple[RoutePair, ...] = (
    ("POST", "/assistant/approvals/{approval_id}/approve"),
    ("POST", "/assistant/approvals/{approval_id}/reject"),
    ("POST", "/assistant/approvals/{approval_id}/revert"),
    ("POST", "/assistant/approvals/{approval_id}/recheck"),
    ("POST", "/assistant/approvals/{approval_id}/resolve"),
    ("PATCH", "/assistant/settings"),
    ("GET", "/assistant/outlook"),
    ("GET", "/assistant/files/{file_id}/preview"),
    ("GET", "/assistant/conversations/{conversation_id}/views"),
    ("POST", "/assistant/conversations"),
    ("POST", "/assistant/conversations/{conversation_id}/messages"),
    # Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md 3.9.6): only the person stages
    # files (the shim cannot send multipart anyway; DELETE is refused above).
    ("POST", "/assistant/files"),
    # Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.3.6): the Routines tab's
    # actions, the Memory tab and the holiday list are the person's own. The
    # assistant has its own routes for what it may do (the housekeeping tool
    # routes under /assistant/tools, the reads, and POST /assistant/schedules
    # for the execute job of an approved card), so none of these is open.
    ("POST", "/assistant/schedules/{schedule_id}/pause"),
    ("POST", "/assistant/schedules/{schedule_id}/resume"),
    ("POST", "/assistant/schedules/{schedule_id}/end"),
    ("POST", "/assistant/schedules/{schedule_id}/run-now"),
    ("POST", "/assistant/schedules/seen"),
    ("GET", "/assistant/memories"),
    ("GET", "/assistant/memories/summary"),
    ("GET", "/assistant/memories/{memory_id}"),
    ("POST", "/assistant/memories"),
    ("PATCH", "/assistant/memories/{memory_id}"),
    ("POST", "/assistant/memories/{memory_id}/pin"),
    ("POST", "/assistant/memories/{memory_id}/unpin"),
    ("POST", "/assistant/memories/{memory_id}/archive"),
    ("POST", "/assistant/memories/{memory_id}/restore"),
    ("POST", "/assistant/memories/{memory_id}/approve"),
    ("POST", "/assistant/memories/{memory_id}/reject"),
    ("POST", "/assistant/memories/{memory_id}/revisions/{revision_id}/approve"),
    ("POST", "/assistant/memories/{memory_id}/revisions/{revision_id}/reject"),
    ("DELETE", "/assistant/memories/{memory_id}"),
    ("GET", "/assistant/admin/holidays"),
    ("PUT", "/assistant/admin/holidays"),
    # Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.9.3, 3.22): report files are
    # downloaded by the person only, so a signed link never reaches the model.
    ("GET", "/assistant/reports/{report_id}/download"),
    ("GET", "/assistant/reports"),
)

# Phase 7 (docs/ASSISTANT_PHASE7_PLAN.md 10.1): the Inbox, the Alerts settings
# and the admin role defaults, company switch and feedback report are the
# person's own buttons and the admins' own switches. The model has no tool for
# any of them. Listed pair by pair (the two prefixes below are the catch-all).
_ASSISTANT_INBOX: tuple[RoutePair, ...] = (
    ("GET", "/assistant/inbox"),
    ("GET", "/assistant/inbox/summary"),
    ("GET", "/assistant/inbox/digests/{digest_id}"),
    ("POST", "/assistant/inbox/read"),
    ("POST", "/assistant/inbox/dismiss"),
    ("POST", "/assistant/inbox/{inbox_id}/feedback"),
    ("POST", "/assistant/inbox/{inbox_id}/snooze"),
    ("GET", "/assistant/inbox/feedback-report"),
    ("GET", "/assistant/alerts"),
    ("GET", "/assistant/alerts/company"),
    ("PUT", "/assistant/alerts/company"),
    # Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md 8.1): the person's digest email
    # switch and the admins' company triage switch.
    ("PUT", "/assistant/alerts/me/digest-email"),
    ("GET", "/assistant/alerts/company/triage"),
    ("PUT", "/assistant/alerts/company/triage"),
    ("GET", "/assistant/alerts/defaults"),
    ("PUT", "/assistant/alerts/defaults/{role}/{event_type}"),
    ("POST", "/assistant/alerts/defaults/{role}/{event_type}/reset"),
    ("PUT", "/assistant/alerts/{event_type}"),
    ("POST", "/assistant/alerts/{event_type}/reset"),
    ("POST", "/assistant/alerts/mutes/{project_id}/remove"),
)

# Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.13, 3.22; decisions 11 and 19):
# certified payroll is aggregates only, so every payroll READ that carries a
# person, a wage, a rate, a document or a setting is refused too. The two
# aggregate reads stay open for the cp tools (PAYROLL_SPEC_READS) and the
# enrollment lists are neither denied nor declared (PAYROLL_OPEN_READS).
_PAYROLL_READS: tuple[RoutePair, ...] = (
    ("GET", "/payroll/reports/{report_id}"),
    ("GET", "/payroll/reports/{report_id}/entries"),
    ("GET", "/payroll/reports/{report_id}/cpr/records"),
    ("GET", "/payroll/reports/{report_id}/cpr/records/{record_id}/download"),
    ("GET", "/payroll/reports/{report_id}/cpr/records/{record_id}/files/{file_id}/download"),
    ("GET", "/payroll/employees"),
    ("GET", "/payroll/employees/check-id"),
    ("GET", "/payroll/employees/{employee_id}"),
    ("GET", "/payroll/employees/{employee_id}/documents"),
    ("GET", "/payroll/employees/{employee_id}/documents/{doc_id}/download"),
    ("GET", "/payroll/classifications"),
    ("GET", "/payroll/rates"),
    ("GET", "/payroll/settings"),
    ("GET", "/payroll/profile"),
    ("GET", "/payroll/ignored-projects"),
)

# The per-person audit feed: the assistant reads one person's activity only
# through the person_evidence card (3.22).
_ANALYTICS: tuple[RoutePair, ...] = (
    ("GET", "/analytics/activity"),
)

# Payroll GETs the cp tools declare (cp_report_status, cp_week_summary), and
# the enrollment lists that are neither denied nor declared in phase 4. A
# test walks the route table: every other /payroll GET must be deny-listed.
PAYROLL_SPEC_READS: frozenset[RoutePair] = frozenset({
    ("GET", "/payroll/reports"),
    ("GET", "/payroll/reports/summary"),
})
PAYROLL_OPEN_READS: frozenset[RoutePair] = frozenset({
    ("GET", "/payroll/projects"),
    ("GET", "/payroll/projects/eligible"),
    ("GET", "/payroll/projects/{project_id}"),
})

DENY_LIST: frozenset[RoutePair] = frozenset(
    _USERS + _PERSONAL_BROWSER + _BROWSER_AGENTS + _AGENT_WORKER + _AUTO_SEND + _AI_SPEND + _DEV_ONLY
    + _DELETED_PROJECTS + _PROJECTS + _EXTERNAL_SUBMISSION + _RFP_EMAILS
    + _IT_ADMIN_SETTINGS + _PAYROLL + _ASSISTANT_ROUTES + _ASSISTANT_INBOX + _PAYROLL_READS + _ANALYTICS
)

# Routes the deny-list deliberately leaves open, with the reason.
NOT_DENIED: dict[RoutePair, str] = {
    ("POST", "/projects/{project_id}/advance"):
        "advance_lane reaches it with gono_action='review' hard-coded in the ToolSpec",
    ("GET", "/payroll/reports"):
        "cp_report_status: report headers only, its trimmer keeps aggregates (no names, wages or paths)",
    ("GET", "/payroll/reports/summary"):
        "cp_week_summary: week aggregates with small cells merged (cp_summary.py)",
    ("GET", "/payroll/projects"): "enrollment list, no wages; not declared by any phase 4 spec",
    ("GET", "/payroll/projects/eligible"): "enrollment picker, no wages; not declared by any phase 4 spec",
    ("GET", "/payroll/projects/{project_id}"): "one enrollment, no wages; not declared by any phase 4 spec",
}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    method: str
    path_format: str | None

    # Reasons, one short code each; the 403 body carries it.
    ALLOWED = "allowed"
    METHOD_REFUSED = "method_refused"
    DENY_LISTED = "deny_listed"
    NOT_ALLOWLISTED = "not_allowlisted"
    NO_ROUTE = "no_route"


def normalize_pairs(pairs: Iterable[RoutePair | Collection[str]]) -> frozenset[RoutePair]:
    """An allowlist as the registry hands it, as a set of (METHOD, path_format)."""
    out: set[RoutePair] = set()
    for pair in pairs:
        method, path_format = pair  # a 2-tuple or any 2-item collection
        out.add((str(method).upper(), str(path_format)))
    return frozenset(out)


def is_deny_listed(method: str, path_format: str) -> bool:
    method = method.upper()
    if (method, path_format) in DENY_LIST:
        return True
    return any(path_format.startswith(prefix) or path_format == prefix.rstrip("/")
               for prefix in DENY_PATH_PREFIXES)


def check(method: str, path_format: str | None, allowlist: Collection[RoutePair]) -> Verdict:
    """The pure decision for a resolved route. `path_format` is None when the
    request matched no route; that is refused too, so an unknown path never
    reaches the app's own 404 handler and leaks route existence."""
    method = (method or "").upper()
    if method in REFUSED_METHODS:
        return Verdict(False, Verdict.METHOD_REFUSED, method, path_format)
    if path_format is None:
        return Verdict(False, Verdict.NO_ROUTE, method, None)
    if is_deny_listed(method, path_format):
        return Verdict(False, Verdict.DENY_LISTED, method, path_format)
    if (method, path_format) not in allowlist:
        return Verdict(False, Verdict.NOT_ALLOWLISTED, method, path_format)
    return Verdict(True, Verdict.ALLOWED, method, path_format)


def resolve_path_format(app: Any, scope: Mapping[str, Any]) -> str | None:
    """The path_format of the route `app` would dispatch `scope` to, or None.

    Walks app.router.routes with Route.matches exactly as Starlette does, so
    the firewall judges the same route the app will run. A PARTIAL match
    (path known, method not) is None: the app would answer 405, and the
    firewall refuses before that."""
    from starlette.routing import Match

    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return getattr(route, "path_format", None) or getattr(route, "path", None)
    return None


def check_scope(app: Any, scope: Mapping[str, Any], allowlist: Collection[RoutePair]) -> Verdict:
    """Resolve the route for an ASGI http scope and judge it."""
    method = str(scope.get("method", "")).upper()
    if method in REFUSED_METHODS:
        return Verdict(False, Verdict.METHOD_REFUSED, method, None)
    return check(method, resolve_path_format(app, scope), allowlist)
