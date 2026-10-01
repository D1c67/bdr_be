"""External submission: "Mark submitted" from the project side menu (0140).

Sometimes G3 sends a proposal outside the app while the project already
exists here. This records the numbers that went out and moves the project to
Submitted from any stage past Go/No-Go, without walking the pipeline. The
existing Send Out "Mark as submitted" (proposal_send.mark_submitted) is a
different, unchanged path: it needs generated documents; this one does not.

Who may do it:
  * APPROVER_ROLES (Executive, Estimating Admin, IT Admin) go straight to the
    wizard and decide everyone else's requests.
  * REQUESTER_ROLES (the other writers: both engineer focuses) must ask first.
    A grant is for one user on one project, lasts GRANT_HOURS from the
    decision, and is consumed by the submit. Expiry is enforced server side on
    every wizard read that returns data and, atomically, inside the apply.
  * The accountant (read only) and the estimator (external) never get here.

Where the numbers land. Besides its own tables (the submission, one row per
material category for data studies, one row per project GC), the submission
writes the SAME canonical rows the pipeline writes, so Win/Loss, analytics
and the reports read it unchanged:
  labor_reviews.labor_amount           (Labor Numbers)
  markups pct/amount per section       (Markup)
  verifications, committed snapshot    (the Verify commit: bid price)
  project_gcs proposal_*_amount        (per-GC overrides, GC Pricing)
  proposal_sends status 'sent', sent_via 'external',
      origin 'external_submission'     (what Win/Loss, GC spread and the
                                        Estimator vs Bid report read)
  project_category_state + stage_events + projects headline
                                        (send_out head 'submitted', every
                                        other lane complete)
All of it runs in ONE database transaction (apply_external_submission), which
also snapshots what it overwrites so an undo can restore it
(undo_external_submission). In-flight RFQs, quotes and drafts are untouched.

The pure functions at the top are the tested seam; the DB-backed functions
below them stay thin.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from postgrest.exceptions import APIError

from app.core.roles import WRITER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import proposal_send, workflow
from app.services.notifications import audit, dismiss_notifications, notify_role, notify_user

logger = logging.getLogger(__name__)

APPROVER_ROLES = frozenset({Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN})
UNDO_ROLES = APPROVER_ROLES
REQUESTER_ROLES = WRITER_ROLES - APPROVER_ROLES
GRANT_HOURS = 48

SECTION_KEYS = ("materials", "gear", "underground", "low_voltage")
BREAKOUT_KEYS = ("gear", "underground", "low_voltage")
MARKUP_KEYS = SECTION_KEYS + ("labor",)
# proposal_send's per-GC field names, keyed by our section name.
GC_FIELD_OF_SECTION = {
    "materials": "material",
    "gear": "gear",
    "underground": "underground",
    "low_voltage": "low_voltage",
}
GC_FIELDS = ("material", "gear", "underground", "low_voltage", "labor")

REQUEST_TYPE = "external_submission.requested"
DECISION_TYPES = {
    "granted": "external_submission.granted",
    "denied": "external_submission.denied",
    "revoked": "external_submission.revoked",
}
UNDONE_TYPE = "external_submission.undone"

CENT = Decimal("0.01")

# Readable messages for the reason codes eligibility() and the database
# functions return. The FE has its own localized copy keyed the same way.
# markups.*_markup_pct is numeric(6,3).
MAX_MARKUP_PCT = Decimal("1000")

REASON_MESSAGES = {
    "not_found": "Project not found.",
    "never_bid": "This project was never bid in the app.",
    "declined": "This project was declined at Go/No-Go.",
    "abandoned": "This project has been abandoned.",
    "before_go_no_go": "The project has not passed Go/No-Go yet.",
    "already_submitted": "This bid is already submitted.",
    "outcome_recorded": "A win/loss outcome is already recorded for this bid.",
    "sending_in_progress": "A proposal email is being sent right now. Wait for it to finish.",
    "stale": "The project changed while you were working. Reopen Mark submitted and try again.",
    "grant_invalid": "Your permission is no longer valid (expired, revoked or already used). "
    "Request permission again.",
    "gc_already_sent": "A GC's proposal was sent while you were working. Reopen and try again.",
    "gc_not_on_project": "A GC was removed from the project while you were working.",
    "not_active": "This submission was already undone.",
    "not_submitted_head": "The bid is not on Submitted right now (for example it was bounced "
    "back to Verify). Commit Verify first, then undo.",
    "project_not_found": "Project not found.",
}


class ExternalSubmissionError(Exception):
    """User-actionable failure; the router surfaces .args[0] as the detail and
    `code` as the X-Error-Code header."""

    def __init__(self, message: str, status_code: int = 409, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# ── pure helpers ───────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dec(v) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


def _s(v: Decimal | None) -> str | None:
    return str(v) if v is not None else None


def _cents(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def role_kind(role: Role | str | None) -> str:
    """'approver' (skips the request step), 'requester' (must ask first) or
    'none' (accountant, estimator, unknown: no access)."""
    try:
        r = Role(role) if role is not None else None
    except ValueError:
        return "none"
    if r in APPROVER_ROLES:
        return "approver"
    if r in REQUESTER_ROLES:
        return "requester"
    return "none"


def eligibility(
    project: dict | None,
    state: dict[str, dict],
    *,
    has_outcome: bool,
    has_active_submission: bool,
) -> str | None:
    """Pure: None when a project may be marked submitted, else the reason code.

    Available once the project is past Go/No-Go, while it is not declined, not
    abandoned, not already submitted (the send_out head at Submitted / Win-Loss,
    or parked at Verify by a post-submission re-verify bounce), and has no
    recorded outcome."""
    if not project:
        return "not_found"
    stage = project.get("current_stage")
    if stage in ("pm_only", "cp_only"):
        return "never_bid"
    if stage == "declined":
        return "declined"
    if project.get("abandoned_at"):
        return "abandoned"
    if has_active_submission:
        return "already_submitted"
    if has_outcome:
        return "outcome_recorded"
    so = state.get("send_out", {})
    if proposal_send.gone_out_rule(so.get("current_task"), project.get("reverify_return_stage")):
        return "already_submitted"
    if not workflow.category_past(state, "go_no_go"):
        return "before_go_no_go"
    return None


def request_is_expired(req: dict, now: datetime) -> bool:
    """An approved grant whose 48 hours ran out (or a row already marked so)."""
    if req.get("status") == "expired":
        return True
    if req.get("status") != "approved":
        return False
    exp = _parse_ts(req.get("expires_at"))
    return exp is None or exp <= now


def grant_is_valid(req: dict | None, user_id: str, project_id: str, now: datetime) -> bool:
    """Pure: this row is an unused, unexpired grant for exactly this user on
    exactly this project."""
    if not req:
        return False
    return (
        req.get("status") == "approved"
        and req.get("requested_by") == user_id
        and req.get("project_id") == project_id
        and not request_is_expired(req, now)
    )


def display_status(req: dict, now: datetime) -> str:
    """The status a person should see: an approved grant past its expiry reads
    'expired' even before the lazy write catches up."""
    return "expired" if request_is_expired(req, now) else req.get("status") or "pending"


def section_costs(lines: list[dict]) -> dict[str, Decimal | None]:
    """Pure: the cost of each pricing section = the sum of its category lines.
    The residual materials section is always present (0 with no lines); a
    breakout section with no lines is None, meaning "not on this bid" (the
    verifications snapshot convention)."""
    out: dict[str, Decimal | None] = {}
    for key in SECTION_KEYS:
        mine = [Decimal(str(line["amount"])) for line in lines if line["pricing_section"] == key]
        if key == "materials":
            out[key] = sum(mine, Decimal(0))
        else:
            out[key] = sum(mine, Decimal(0)) if mine else None
    return out


def resolve_markup(
    cost: Decimal | None, pct: Decimal | None, amount: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    """Pure: (pct, amount) for one section, mirroring the Markup step. The
    amount prices the bid: a typed amount wins; a percent alone becomes
    cost x pct / 100 (to the cent); the percent is derived from a typed amount
    when it was left blank and the cost is positive. A derived percent that
    does not fit markups.*_markup_pct (numeric(6,3), under 1000) is left out:
    it is only kept for the record, and a large markup on a small section
    (say a markup amount carried over from before a gear split) must not
    fail the whole submission."""
    if amount is not None:
        amount = _cents(amount)
        if pct is None and cost is not None and cost > 0:
            pct = (amount / cost * 100).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            if pct >= MAX_MARKUP_PCT:
                pct = None
        return pct, amount
    if pct is not None:
        base = cost if cost is not None else Decimal(0)
        return pct, _cents(base * pct / 100)
    return None, None


def compute_final(
    costs: dict[str, Decimal | None],
    labor_amount: Decimal,
    markups: dict[str, tuple[Decimal | None, Decimal | None]],
) -> dict[str, Decimal | None]:
    """Pure: the ten figures in the verifications snapshot shape
    (resolved_verify_numbers' output). Legacy keys (labor/materials) are never
    None; a breakout section not on the bid is None for cost and markup."""
    final: dict[str, Decimal | None] = {
        "labor_amount": labor_amount,
        "materials_amount": costs["materials"] or Decimal(0),
        "labor_markup_amount": markups["labor"][1] or Decimal(0),
        "materials_markup_amount": markups["materials"][1] or Decimal(0),
    }
    for key in BREAKOUT_KEYS:
        cost = costs[key]
        final[f"{key}_amount"] = cost
        final[f"{key}_markup_amount"] = (
            None if cost is None else (markups[key][1] or Decimal(0))
        )
    return final


def prefill_markup(
    cost: Decimal | None,
    stored_pct,
    stored_amount: Decimal | None,
    amount: Decimal | None,
) -> dict[str, str | None]:
    """Pure: one section's markup seed for the wizard. The amount is the
    project's resolved markup, to the cent. The percent is offered (the wizard
    then drives the amount from it) only when it reproduces that exact amount
    on the prefilled cost and fits the three decimals the submit accepts;
    otherwise the amount drives, so the prefill never shifts the price and
    never seeds a percent the server would refuse."""
    if amount is None:
        return {"pct": None, "amount": None}
    amount = _cents(amount)
    pct = _dec(stored_pct)
    keep_pct = (
        pct is not None
        and stored_amount is not None
        and _cents(stored_amount) == amount
        and pct == pct.quantize(Decimal("0.001"))
        and cost is not None
        and _cents(cost * pct / 100) == amount
    )
    return {"pct": _s(pct) if keep_pct else None, "amount": _s(amount)}


def gc_amounts(
    final: dict[str, Decimal | None], overrides: dict[str, Decimal | None]
) -> tuple[dict[str, Decimal | None], dict[str, Decimal | None]]:
    """Pure: (project defaults, this GC's resolved figures). A GC's own figure
    for a section overrides the default (cost plus markup) for that section
    only; the cost basis never moves (the difference is that GC's markup)."""
    defaults = proposal_send.amounts_from_final(final)
    gc = {f"{f}_override": overrides.get(f) for f in GC_FIELDS}
    return defaults, proposal_send.resolve_gc_amounts(defaults, gc)


def validate_submission(
    body,
    *,
    categories: dict[str, dict],
    project_gcs: dict[str, dict],
    sent_gc_ids: set[str],
) -> None:
    """Pure: every server-side rule on the posted wizard, before any write.

    `categories` = active material categories by id (with pricing_section);
    `project_gcs` = the project's GCs by id; `sent_gc_ids` = GCs whose
    proposal already went out through Send Out (left untouched, count as
    included)."""
    seen: set[str] = set()
    for line in body.lines:
        cat = categories.get(line.material_category_id)
        if not cat:
            raise ExternalSubmissionError(
                "A material category is not in the category list.", 400, "bad_category"
            )
        if (cat.get("pricing_section") or "materials") != line.pricing_section:
            raise ExternalSubmissionError(
                f"{cat.get('name')} does not belong to that pricing section.", 400,
                "bad_section",
            )
        if line.material_category_id in seen:
            raise ExternalSubmissionError(
                f"{cat.get('name')} is listed twice.", 400, "duplicate_category"
            )
        seen.add(line.material_category_id)
        if line.amount < 0:
            raise ExternalSubmissionError("Amounts cannot be negative.", 400, "negative")

    present = {line.pricing_section for line in body.lines}
    for key in BREAKOUT_KEYS:
        mk = getattr(body.markups, key)
        if key not in present and (mk.pct is not None or mk.amount is not None):
            raise ExternalSubmissionError(
                "A markup was entered for a section with no categories.", 400,
                "markup_without_section",
            )

    gc_seen: set[str] = set()
    included = 0
    for gc in body.gcs:
        if gc.gc_id not in project_gcs:
            raise ExternalSubmissionError("A GC is not on this project.", 400, "gc_not_on_project")
        if gc.gc_id in gc_seen:
            raise ExternalSubmissionError("A GC is listed twice.", 400, "duplicate_gc")
        gc_seen.add(gc.gc_id)
        if gc.gc_id in sent_gc_ids:
            included += 1
            continue
        for key in BREAKOUT_KEYS:
            if getattr(gc, f"{key}_amount") is not None and key not in present:
                raise ExternalSubmissionError(
                    "A GC price was entered for a section with no categories.", 400,
                    "override_without_section",
                )
        if gc.included:
            included += 1
    # GCs whose proposal already went out count even when the wizard omitted them.
    included += len(sent_gc_ids - gc_seen)
    if included < 1:
        raise ExternalSubmissionError(
            "Include at least one GC the bid was submitted to.", 400, "no_included_gc"
        )


def target_lanes(state: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Pure: (category_state rows, stage events) that take the project from
    `state` to Submitted: every lane but send_out complete (its head on its
    last task, the invariant a normal submission leaves), send_out active on
    'submitted'. Events only for the send_out lane, exactly what the normal
    flow records: an unlock row when the lane was still locked, then one row
    from its head to 'submitted' (its note is filled in by the caller).
    Completing a lane emits no event (completed_at is the record)."""
    rows: list[dict] = []
    for cat in workflow.CATEGORY_ORDER:
        if cat == "send_out":
            continue
        last = workflow.CATEGORY_TASKS[cat][-1]
        owner = workflow.owner_role_for(last)
        rows.append(
            {
                "category": cat,
                "current_task": last,
                "status": "complete",
                "owner_role": owner.value if owner else None,
            }
        )
    owner = workflow.owner_role_for("submitted")
    rows.append(
        {
            "category": "send_out",
            "current_task": "submitted",
            "status": "active",
            "owner_role": owner.value if owner else None,
        }
    )
    so = state.get("send_out", {})
    events: list[dict] = []
    if so.get("status") == "locked" or not so.get("current_task"):
        first = workflow.CATEGORY_TASKS["send_out"][0]
        events.append(
            {"category": "send_out", "from_stage": None, "to_stage": first,
             "note": "Category unlocked"}
        )
        from_task = first
    else:
        from_task = so["current_task"]
    events.append(
        {"category": "send_out", "from_stage": from_task, "to_stage": "submitted", "note": None}
    )
    return rows, events


def submission_note(submitted: list[str], no_bid: list[str], who: str) -> str:
    """The stage-event note: the durable prose record, in the spirit of the
    "Done sending" note."""
    parts = [f"Marked submitted by {who} (proposal sent outside the app)."]
    if submitted:
        parts.append("Submitted to: " + ", ".join(submitted) + ".")
    if no_bid:
        parts.append("No bid: " + ", ".join(no_bid) + ".")
    return " ".join(parts)


def build_plan(
    body,
    *,
    categories: dict[str, dict],
    project_gcs: dict[str, dict],
    sent_rows: dict[str, dict],
    state: dict[str, dict],
    who: str,
) -> dict:
    """Pure: validate, then compute everything the apply writes. Returns the
    jsonb document (minus project/user/request ids) plus a `summary` for the
    notifications and the audit row."""
    validate_submission(
        body, categories=categories, project_gcs=project_gcs, sent_gc_ids=set(sent_rows)
    )
    lines = [
        {
            "material_category_id": line.material_category_id,
            "category_name": categories[line.material_category_id].get("name") or "",
            "pricing_section": line.pricing_section,
            "amount": _cents(line.amount),
            "note": (line.note or "").strip() or None,
            "sort_order": i,
        }
        for i, line in enumerate(body.lines)
    ]
    costs = section_costs(lines)
    labor = _cents(body.labor_amount)
    markups: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for key in MARKUP_KEYS:
        mk = getattr(body.markups, key)
        cost = labor if key == "labor" else costs[key]
        if key in BREAKOUT_KEYS and cost is None:
            markups[key] = (None, None)
        else:
            markups[key] = resolve_markup(cost, mk.pct, mk.amount)
    final = compute_final(costs, labor, markups)

    posted = {g.gc_id: g for g in body.gcs}
    gcs_out: list[dict] = []
    submitted_names: list[str] = []
    no_bid_names: list[str] = []
    for gc_id, gc in sorted(project_gcs.items(), key=lambda kv: (kv[1].get("name") or "").lower()):
        name = gc.get("name") or "GC"
        if gc_id in sent_rows:
            row = sent_rows[gc_id]
            stamped = {f: _dec(row.get(f"{f}_amount")) for f in GC_FIELDS}
            total = sum((v for v in stamped.values() if v is not None), Decimal(0))
            gcs_out.append(
                {"gc_id": gc_id, "gc_name": name, "included": True, "already_sent": True,
                 "write": False, **{f"{f}_amount": stamped[f] for f in GC_FIELDS},
                 "total_amount": total, "default_total": None}
            )
            submitted_names.append(name)
            continue
        p = posted.get(gc_id)
        if p is None or not p.included:
            gcs_out.append(
                {"gc_id": gc_id, "gc_name": name, "included": False, "already_sent": False,
                 "write": False, **{f"{f}_amount": None for f in GC_FIELDS},
                 "total_amount": None, "default_total": None}
            )
            no_bid_names.append(name)
            continue
        overrides = {
            f: (_cents(getattr(p, f"{f}_amount")) if getattr(p, f"{f}_amount") is not None else None)
            for f in GC_FIELDS
        }
        defaults, resolved = gc_amounts(final, overrides)
        entry = {
            "gc_id": gc_id, "gc_name": name, "included": True, "already_sent": False,
            "write": True,
            **{f"{f}_amount": resolved[f] for f in GC_FIELDS},
            "total_amount": resolved["total"],
            "default_total": defaults["total"],
        }
        for f in GC_FIELDS:
            entry[f"override_{f}"] = overrides[f]
        gcs_out.append(entry)
        submitted_names.append(name)

    rows, events = target_lanes(state)
    note = submission_note(submitted_names, no_bid_names, who)
    events[-1]["note"] = note
    owner = workflow.owner_role_for("submitted")
    defaults = proposal_send.amounts_from_final(final)
    total_cost = sum(
        (v for v in (final["labor_amount"], *(costs[k] for k in SECTION_KEYS)) if v is not None),
        Decimal(0),
    )
    submission = {
        "materials_amount": costs["materials"],
        "gear_amount": costs["gear"],
        "underground_amount": costs["underground"],
        "low_voltage_amount": costs["low_voltage"],
        "labor_amount": labor,
        "labor_note": (body.labor_note or "").strip() or None,
        "total_cost": total_cost,
        "total_price": defaults["total"],
    }
    for key in MARKUP_KEYS:
        submission[f"{key}_markup_pct"], submission[f"{key}_markup_amount"] = markups[key]
    markup_row = {
        f"{key}_markup_{part}": markups[key][i]
        for key in MARKUP_KEYS
        for i, part in ((0, "pct"), (1, "amount"))
    }
    verification = {k: final[k] for k in final}
    verification["notes"] = "Recorded by Mark submitted (proposal sent outside the app)."
    doc = {
        "submission": submission,
        "lines": lines,
        "gcs": gcs_out,
        "labor_review": {"labor_amount": labor},
        "markup": markup_row,
        "verification": verification,
        "category_state": rows,
        "events": events,
        "headline": {
            "current_stage": "submitted",
            "current_owner_role": owner.value if owner else None,
        },
    }
    return {
        "doc": _jsonable(doc),
        "summary": {
            "submitted": submitted_names,
            "no_bid": no_bid_names,
            "total_price": _s(defaults["total"]),
            "note": note,
        },
    }


def _jsonable(v):
    """Decimals to strings (numeric-safe in jsonb ->> casts), recursively."""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def error_code_of(exc: Exception) -> str | None:
    """The 'external_submission:<code>' a database function raised, if any."""
    text = getattr(exc, "message", None) or str(exc)
    marker = "external_submission:"
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split()[0].strip("'\",.}")


def _db_error(exc: Exception) -> ExternalSubmissionError:
    code = error_code_of(exc)
    if code is None:
        # Anything else (an overflow, a constraint) is still answered with a
        # message: a bare 500 loses its CORS headers and the wizard just sits
        # on "Saving".
        logger.error("external submission database error: %s", exc)
        return ExternalSubmissionError(
            "The submission could not be saved. Check the numbers and try again; "
            "if it keeps failing, contact IT.",
            500,
            "db_error",
        )
    status_code = 404 if code in ("not_found", "project_not_found") else 409
    return ExternalSubmissionError(
        REASON_MESSAGES.get(code, "The submission could not be saved."), status_code, code
    )


# ── DB reads ───────────────────────────────────────────────────────────────


def _project(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("projects")
        .select("id, name, number, current_stage, abandoned_at, reverify_return_stage")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _has_outcome(sb, project_id: str) -> bool:
    return bool(
        (sb.table("bid_outcomes").select("id").eq("project_id", project_id).limit(1).execute()).data
    )


def _active_submission(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("external_submissions")
        .select("*")
        .eq("project_id", project_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _names(sb, ids) -> dict[str, str | None]:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = (sb.table("profiles").select("id, full_name").in_("id", ids).execute()).data or []
    return {r["id"]: r.get("full_name") for r in rows}


def _display_name(sb, user_id: str | None) -> str:
    return _names(sb, [user_id]).get(user_id) or "A teammate"


def _project_tag(project: dict) -> str:
    number, name = project.get("number"), project.get("name")
    if number and name:
        return f"#{number} {name}"
    return name or (f"#{number}" if number else "the project")


def _requests(sb, project_id: str) -> list[dict]:
    return (
        sb.table("external_submission_requests")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []


def expire_stale_grants(sb, rows: list[dict], now: datetime) -> None:
    """Lazily mark approved grants past their 48 hours as 'expired' (a
    compare-and-set on status, so a submit racing it wins or loses cleanly).
    Needed before a new request: the one-open-request index counts an
    approved row as open."""
    for r in rows:
        if r.get("status") == "approved" and request_is_expired(r, now):
            (
                sb.table("external_submission_requests")
                .update({"status": "expired", "updated_at": now.isoformat()})
                .eq("id", r["id"])
                .eq("status", "approved")
                .execute()
            )
            r["status"] = "expired"


def _request_wire(r: dict, names: dict[str, str | None], now: datetime) -> dict:
    return {
        "id": r["id"],
        "requested_by": r.get("requested_by"),
        "requested_by_name": names.get(r.get("requested_by")),
        "message": r.get("message"),
        "status": display_status(r, now),
        "created_at": r.get("created_at"),
        "decided_by_name": names.get(r.get("decided_by")),
        "decided_at": r.get("decided_at"),
        "deny_reason": r.get("deny_reason"),
        "expires_at": r.get("expires_at"),
        "revoked_by_name": names.get(r.get("revoked_by")),
        "revoked_at": r.get("revoked_at"),
        "used_at": r.get("used_at"),
    }


def _submission_detail(sb, sub: dict) -> dict:
    lines = (
        sb.table("external_submission_lines")
        .select("*")
        .eq("submission_id", sub["id"])
        .order("sort_order")
        .execute()
    ).data or []
    gcs = (
        sb.table("external_submission_gcs")
        .select("*")
        .eq("submission_id", sub["id"])
        .execute()
    ).data or []
    names = _names(sb, [sub.get("submitted_by"), sub.get("undone_by")])
    approver = None
    if sub.get("approval_request_id"):
        req = (
            sb.table("external_submission_requests")
            .select("decided_by")
            .eq("id", sub["approval_request_id"])
            .limit(1)
            .execute()
        ).data or []
        if req:
            approver = _display_name(sb, req[0].get("decided_by"))
    keep = (
        "id", "status", "submitted_at", "materials_amount", "gear_amount",
        "underground_amount", "low_voltage_amount", "labor_amount", "labor_note",
        "total_cost", "total_price", "undone_at", "undo_reason",
    )
    out = {k: sub.get(k) for k in keep}
    for key in MARKUP_KEYS:
        out[f"{key}_markup_pct"] = sub.get(f"{key}_markup_pct")
        out[f"{key}_markup_amount"] = sub.get(f"{key}_markup_amount")
    out["submitted_by_name"] = names.get(sub.get("submitted_by"))
    out["undone_by_name"] = names.get(sub.get("undone_by"))
    out["approved_by_name"] = approver
    out["lines"] = [
        {k: line.get(k) for k in (
            "material_category_id", "category_name", "pricing_section", "amount", "note",
        )}
        for line in lines
    ]
    out["gcs"] = sorted(
        (
            {k: g.get(k) for k in (
                "gc_id", "gc_name", "included", "already_sent", "material_amount",
                "gear_amount", "underground_amount", "low_voltage_amount", "labor_amount",
                "total_amount", "default_total",
            )}
            for g in gcs
        ),
        key=lambda g: (not g["included"], (g.get("gc_name") or "").lower()),
    )
    return out


def overview(project_id: str, user_id: str, role: Role | str) -> dict:
    """Everything the side-menu modal needs to pick its screen: who this user
    is to the feature, whether the project can be marked, their own request
    or grant, the approvers' queue, and the live submission (read-only view)."""
    sb = get_supabase()
    now = _now()
    kind = role_kind(role)
    project = _project(sb, project_id)
    if not project:
        raise ExternalSubmissionError("Project not found", 404, "not_found")
    state = workflow.load_category_state(project_id)
    active = _active_submission(sb, project_id)
    has_outcome = _has_outcome(sb, project_id)
    reason = eligibility(
        project, state, has_outcome=has_outcome, has_active_submission=active is not None
    )
    rows = _requests(sb, project_id) if kind != "none" else []
    expire_stale_grants(sb, rows, now)
    names = _names(
        sb,
        [r.get(k) for r in rows for k in ("requested_by", "decided_by", "revoked_by")],
    )
    mine = [r for r in rows if r.get("requested_by") == user_id]
    my_grant = next((r for r in mine if grant_is_valid(r, user_id, project_id, now)), None)
    out: dict = {
        "role_kind": kind,
        "eligible": reason is None,
        "reason": reason,
        "grant_hours": GRANT_HOURS,
        "my_request": _request_wire(mine[0], names, now) if mine else None,
        "my_grant": _request_wire(my_grant, names, now) if my_grant else None,
        "can_submit": reason is None
        and (kind == "approver" or (kind == "requester" and my_grant is not None)),
        "pending_requests": [],
        "active_grants": [],
        "submission": None,
        "can_undo": False,
        "undo_blocked_reason": None,
    }
    if kind == "approver":
        out["pending_requests"] = [
            _request_wire(r, names, now) for r in rows if r.get("status") == "pending"
        ]
        out["active_grants"] = [
            _request_wire(r, names, now)
            for r in rows
            if r.get("status") == "approved" and not request_is_expired(r, now)
        ]
    if active and kind != "none":
        out["submission"] = _submission_detail(sb, active)
        if kind == "approver":
            head = state.get("send_out", {}).get("current_task")
            if has_outcome:
                out["undo_blocked_reason"] = "outcome_recorded"
            elif head != "submitted":
                out["undo_blocked_reason"] = "not_submitted_head"
            else:
                out["can_undo"] = True
    return out


def _reason_for(sb, project_id: str) -> str | None:
    """eligibility() for one project, read fresh."""
    project = _project(sb, project_id)
    if not project:
        return "not_found"
    return eligibility(
        project,
        workflow.load_category_state(project_id),
        has_outcome=_has_outcome(sb, project_id),
        has_active_submission=_active_submission(sb, project_id) is not None,
    )


def pending_count(project_id: str, role: Role | str) -> int:
    """Pending requests on the project for the side-menu badge (approvers only).
    Zero once the project cannot be marked any more (submitted through Send
    Out, declined, abandoned, an outcome recorded): the modal then shows the
    reason instead of the queue, so a count would be a badge nobody can clear.
    The eligibility reads run only when there is something to count."""
    if role_kind(role) != "approver":
        return 0
    sb = get_supabase()
    rows = (
        sb.table("external_submission_requests")
        .select("id")
        .eq("project_id", project_id)
        .eq("status", "pending")
        .execute()
    ).data or []
    if not rows or _reason_for(sb, project_id) is not None:
        return 0
    return len(rows)


def _categories(sb) -> dict[str, dict]:
    rows = (
        sb.table("material_categories")
        .select("id, name, kind, is_active, is_general, pricing_section, sort_order")
        .eq("is_active", True)
        .eq("kind", "material")
        .execute()
    ).data or []
    return {r["id"]: r for r in rows}


def _project_gc_map(sb, project_id: str) -> dict[str, dict]:
    rows = (
        sb.table("project_gcs")
        .select(
            "gc_id, proposal_material_amount, proposal_gear_amount,"
            " proposal_underground_amount, proposal_low_voltage_amount,"
            " proposal_labor_amount, general_contractors(id, name)"
        )
        .eq("project_id", project_id)
        .execute()
    ).data or []
    out: dict[str, dict] = {}
    for r in rows:
        gc = r.get("general_contractors") or {}
        if not r.get("gc_id"):
            continue
        out[r["gc_id"]] = {**r, "name": gc.get("name") or "GC"}
    return out


def _proposal_rows(sb, project_id: str) -> dict[str, dict]:
    rows = (
        sb.table("proposal_sends")
        .select(
            "id, gc_id, status, sent_via, sent_at, material_amount, gear_amount,"
            " underground_amount, low_voltage_amount, labor_amount"
        )
        .eq("project_id", project_id)
        .execute()
    ).data or []
    return {r["gc_id"]: r for r in rows}


def _assert_can_use_wizard(sb, project_id: str, user_id: str, role) -> tuple[dict, dict | None]:
    """Role + grant + eligibility gate shared by the wizard read and the
    submit. Returns (project, grant row or None for approvers)."""
    kind = role_kind(role)
    if kind == "none":
        raise ExternalSubmissionError("Not permitted", 403, "forbidden")
    project = _project(sb, project_id)
    if not project:
        raise ExternalSubmissionError("Project not found", 404, "not_found")
    state = workflow.load_category_state(project_id)
    reason = eligibility(
        project, state,
        has_outcome=_has_outcome(sb, project_id),
        has_active_submission=_active_submission(sb, project_id) is not None,
    )
    if reason:
        raise ExternalSubmissionError(REASON_MESSAGES[reason], 409, reason)
    grant = None
    if kind == "requester":
        now = _now()
        rows = _requests(sb, project_id)
        expire_stale_grants(sb, rows, now)
        grant = next(
            (r for r in rows if grant_is_valid(r, user_id, project_id, now)), None
        )
        if grant is None:
            raise ExternalSubmissionError(
                "You need an approver's permission (valid for 48 hours) to mark this "
                "bid submitted.",
                403,
                "grant_required",
            )
    return project, grant


def wizard_data(project_id: str, user_id: str, role) -> dict:
    """The wizard's inputs: the category list (active material categories with
    their pricing section) and a prefill from whatever the project already has
    (RFQ categories at their selected quote, labor, markups, per-GC prices).
    Gated like the submit: approver, or a requester holding a valid grant."""
    from app.routers.pricing import _get_one, _materials_rows, _verify_originals

    sb = get_supabase()
    _assert_can_use_wizard(sb, project_id, user_id, role)
    categories = _categories(sb)

    lines: list[dict] = []
    seen: set[str] = set()
    for r in _materials_rows(project_id):
        cid = r.get("material_category_id")
        if not cid or cid in seen or cid not in categories:
            continue
        seen.add(cid)
        lines.append(
            {
                "material_category_id": cid,
                "pricing_section": categories[cid].get("pricing_section") or "materials",
                # To the cent: the submit accepts two decimals.
                "amount": _s(_cents(Decimal(str(r["amount"]))))
                if r.get("amount") is not None
                else None,
                "note": None,
            }
        )

    labor_row = _get_one("labor_reviews", project_id)
    markup_row = _get_one("markups", project_id)
    verification = _get_one("verifications", project_id)
    final = proposal_send.resolved_verify_numbers(_verify_originals(project_id), verification or {})
    has_pricing = bool(labor_row or verification)
    labor_cost = _cents(final["labor_amount"]) if has_pricing else None
    labor = _s(labor_cost)
    # The costs the wizard will compute from this prefill, so a seeded percent
    # can be checked against exactly them.
    prefill_costs = section_costs(
        [{"pricing_section": ln["pricing_section"], "amount": ln["amount"] or 0}
         for ln in lines]
    )
    markups: dict[str, dict] = {}
    for key in MARKUP_KEYS:
        if not (markup_row or verification):
            markups[key] = {"pct": None, "amount": None}
            continue
        markups[key] = prefill_markup(
            (labor_cost or Decimal(0)) if key == "labor" else prefill_costs[key],
            (markup_row or {}).get(f"{key}_markup_pct"),
            _dec((markup_row or {}).get(f"{key}_markup_amount")),
            final.get(f"{key}_markup_amount"),
        )

    gcs_map = _project_gc_map(sb, project_id)
    sends = _proposal_rows(sb, project_id)
    gcs = []
    for gc_id, g in sorted(gcs_map.items(), key=lambda kv: kv[1]["name"].lower()):
        send = sends.get(gc_id)
        already = bool(send and send.get("status") == "sent")
        entry = {
            "gc_id": gc_id,
            "gc_name": g["name"],
            "already_sent": already,
            "sending": bool(send and send.get("status") == "sending"),
            "sent_via": send.get("sent_via") if already else None,
            "sent_at": send.get("sent_at") if already else None,
        }
        for f in GC_FIELDS:
            override = g.get(f"proposal_{f}_amount")
            entry[f"{f}_override"] = (
                _s(_cents(Decimal(str(override)))) if override is not None else None
            )
            entry[f"sent_{f}_amount"] = send.get(f"{f}_amount") if already else None
        gcs.append(entry)

    return {
        "categories": sorted(
            (
                {
                    "id": c["id"],
                    "name": c.get("name"),
                    "pricing_section": c.get("pricing_section") or "materials",
                    "is_general": bool(c.get("is_general")),
                    "sort_order": c.get("sort_order") or 0,
                }
                for c in categories.values()
            ),
            key=lambda c: (c["sort_order"], (c["name"] or "").lower()),
        ),
        "prefill": {
            "lines": lines,
            "labor_amount": labor,
            "labor_note": None,
            "markups": markups,
        },
        "gcs": gcs,
    }


# ── requests ───────────────────────────────────────────────────────────────


def create_request(project_id: str, user_id: str, role, message: str | None) -> dict:
    sb = get_supabase()
    if role_kind(role) != "requester":
        raise ExternalSubmissionError(
            "Only users who need permission can request it.", 400, "not_a_requester"
        )
    project = _project(sb, project_id)
    state = workflow.load_category_state(project_id) if project else {}
    reason = eligibility(
        project, state,
        has_outcome=_has_outcome(sb, project_id) if project else False,
        has_active_submission=bool(project) and _active_submission(sb, project_id) is not None,
    )
    if reason:
        raise ExternalSubmissionError(
            REASON_MESSAGES[reason], 404 if reason == "not_found" else 409, reason
        )
    now = _now()
    rows = [r for r in _requests(sb, project_id) if r.get("requested_by") == user_id]
    expire_stale_grants(sb, rows, now)
    if any(r.get("status") in ("pending", "approved") for r in rows):
        raise ExternalSubmissionError(
            "You already have an open request or permission on this project.", 409,
            "request_open",
        )
    text = (message or "").strip() or None
    try:
        row = (
            sb.table("external_submission_requests")
            .insert(
                {"project_id": project_id, "requested_by": user_id, "message": text,
                 "status": "pending"}
            )
            .execute()
        ).data[0]
    except APIError as exc:  # the one-open index lost a race
        raise ExternalSubmissionError(
            "You already have an open request or permission on this project.", 409,
            "request_open",
        ) from exc
    audit(user_id, "external_submission.requested", "project", project_id,
          {"request_id": row["id"], "message": text})
    requester = _display_name(sb, user_id)
    body = (
        f"{requester} asked for permission to mark {_project_tag(project)} as submitted "
        "(the proposal was sent outside the app). Open Mark submitted on the project to "
        "grant or deny it."
    )
    if text:
        body += f" Message: {text}"
    for approver_role in sorted(APPROVER_ROLES, key=lambda r: r.value):
        notify_role(
            approver_role, project_id, REQUEST_TYPE, body, metadata={"request_id": row["id"]}
        )
    return row


def cancel_request(project_id: str, request_id: str, user_id: str) -> None:
    sb = get_supabase()
    done = (
        sb.table("external_submission_requests")
        .update({"status": "cancelled", "updated_at": _now().isoformat()})
        .eq("id", request_id)
        .eq("project_id", project_id)
        .eq("requested_by", user_id)
        .eq("status", "pending")
        .execute()
    ).data
    if not done:
        raise ExternalSubmissionError(
            "There is no pending request of yours to cancel.", 409, "not_pending"
        )
    audit(user_id, "external_submission.cancelled", "project", project_id,
          {"request_id": request_id})
    _dismiss_request_notices(project_id, request_id)


def _dismiss_request_notices(project_id: str, request_id: str) -> None:
    try:
        dismiss_notifications(
            project_id=project_id, types=[REQUEST_TYPE], metadata_eq={"request_id": request_id}
        )
    except Exception:  # noqa: BLE001 (cleanup must never fail the decision)
        logger.exception("could not dismiss request notices for %s", request_id)


def _decide(
    project_id: str,
    request_id: str,
    user_id: str,
    role,
    *,
    from_status: str,
    update: dict,
    action: str,
    decision: str,
    message_for_requester,
) -> dict:
    if role_kind(role) != "approver":
        raise ExternalSubmissionError("Not permitted", 403, "forbidden")
    sb = get_supabase()
    project = _project(sb, project_id)
    if not project:
        raise ExternalSubmissionError("Project not found", 404, "not_found")
    if from_status == "pending":
        # Nobody decides their own request: a requester promoted to an
        # approver role while it was pending cancels it instead (or, as an
        # approver now, simply marks the bid submitted).
        own = (
            sb.table("external_submission_requests")
            .select("id")
            .eq("id", request_id)
            .eq("project_id", project_id)
            .eq("requested_by", user_id)
            .limit(1)
            .execute()
        ).data
        if own:
            raise ExternalSubmissionError(
                "You cannot decide your own request. Cancel it instead.", 403, "own_request"
            )
    if decision == "granted":
        # A grant for a project that can no longer be marked would sit unused
        # (or come back to life if the project became eligible again).
        reason = _reason_for(sb, project_id)
        if reason:
            raise ExternalSubmissionError(REASON_MESSAGES[reason], 409, reason)
    q = (
        sb.table("external_submission_requests")
        .update({**update, "updated_at": _now().isoformat()})
        .eq("id", request_id)
        .eq("project_id", project_id)
        .eq("status", from_status)
    )
    if from_status == "approved":
        # Revoke only an unused grant still inside its 48 hours.
        q = q.is_("used_at", "null")
    done = q.execute().data
    if not done:
        raise ExternalSubmissionError(
            "That request was already decided by someone else, or it changed.", 409,
            "already_decided",
        )
    row = done[0]
    audit(user_id, action, "project", project_id,
          {"request_id": request_id, "requested_by": row.get("requested_by"),
           **{k: v for k, v in update.items() if k in ("deny_reason", "expires_at")}})
    _dismiss_request_notices(project_id, request_id)
    requester = row.get("requested_by")
    if requester and requester != user_id:
        notify_user(
            requester, project_id, DECISION_TYPES[decision],
            message_for_requester(_display_name(sb, user_id), _project_tag(project), row),
            metadata={"request_id": request_id},
        )
    return row


def grant_request(project_id: str, request_id: str, user_id: str, role) -> dict:
    now = _now()
    expires = now + timedelta(hours=GRANT_HOURS)

    def _msg(who, tag, row):
        return (
            f"{who} granted your request to mark {tag} as submitted. The permission is "
            f"yours for {GRANT_HOURS} hours and works once. Open Mark submitted on the "
            "project to enter the numbers."
        )

    return _decide(
        project_id, request_id, user_id, role,
        from_status="pending",
        update={
            "status": "approved",
            "decided_by": user_id,
            "decided_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        },
        action="external_submission.granted",
        decision="granted",
        message_for_requester=_msg,
    )


def deny_request(project_id: str, request_id: str, user_id: str, role, reason: str | None) -> dict:
    text = (reason or "").strip() or None

    def _msg(who, tag, row):
        out = f"{who} denied your request to mark {tag} as submitted."
        return out + (f" Reason: {text}" if text else "")

    return _decide(
        project_id, request_id, user_id, role,
        from_status="pending",
        update={
            "status": "denied",
            "decided_by": user_id,
            "decided_at": _now().isoformat(),
            "deny_reason": text,
        },
        action="external_submission.denied",
        decision="denied",
        message_for_requester=_msg,
    )


def revoke_grant(project_id: str, request_id: str, user_id: str, role) -> dict:
    def _msg(who, tag, row):
        return (
            f"{who} revoked your permission to mark {tag} as submitted. Request it again "
            "if you still need it."
        )

    return _decide(
        project_id, request_id, user_id, role,
        from_status="approved",
        update={"status": "revoked", "revoked_by": user_id, "revoked_at": _now().isoformat()},
        action="external_submission.revoked",
        decision="revoked",
        message_for_requester=_msg,
    )


# ── submit / undo ──────────────────────────────────────────────────────────


def _state_and_send_out_head(sb, project_id: str) -> tuple[dict[str, dict], str | None]:
    """ONE read of the lane rows: the category-state map (missing rows filled
    with the defaults, like workflow.load_category_state) and the send_out
    row's head exactly as stored (None when the row does not exist). The plan
    is computed from the first and the database compare-and-set guards the
    second, so both must come from the same read."""
    rows = (
        sb.table("project_category_state")
        .select("category, current_task, status, owner_role, completed_at")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    state = workflow._default_state()
    head = None
    for r in rows:
        state[r["category"]] = {
            "current_task": r["current_task"],
            "status": r["status"],
            "owner_role": r.get("owner_role"),
            "completed_at": r.get("completed_at"),
        }
        if r["category"] == "send_out":
            head = r["current_task"]
    return state, head


def submit(project_id: str, user_id: str, role, body) -> dict:
    """Validate, compute, and apply in one transaction. Returns the fresh
    overview (the modal swaps to its read-only summary)."""
    sb = get_supabase()
    project, grant = _assert_can_use_wizard(sb, project_id, user_id, role)
    state, head = _state_and_send_out_head(sb, project_id)
    if eligibility(project, state, has_outcome=False, has_active_submission=False):
        # The lanes moved since the gate read them; the database would refuse
        # too, this just says so before computing anything.
        raise ExternalSubmissionError(REASON_MESSAGES["stale"], 409, "stale")
    sends = _proposal_rows(sb, project_id)
    if any(r.get("status") == "sending" for r in sends.values()):
        raise ExternalSubmissionError(
            REASON_MESSAGES["sending_in_progress"], 409, "sending_in_progress"
        )
    sent_rows = {gid: r for gid, r in sends.items() if r.get("status") == "sent"}
    who = _display_name(sb, user_id)
    plan = build_plan(
        body,
        categories=_categories(sb),
        project_gcs=_project_gc_map(sb, project_id),
        sent_rows=sent_rows,
        state=state,
        who=who,
    )
    doc = {
        **plan["doc"],
        "project_id": project_id,
        "user_id": user_id,
        "request_id": grant["id"] if grant else None,
        # The head the plan (lanes, events) was computed from, never a fresh
        # read: the database compare-and-set must guard exactly that state.
        "expected_send_out_head": head,
    }
    try:
        submission_id = sb.rpc("apply_external_submission", {"p": doc}).execute().data
    except APIError as exc:
        raise _db_error(exc) from exc

    summary = plan["summary"]
    audit(user_id, "external_submission.submitted", "project", project_id,
          {"submission_id": submission_id, "request_id": grant["id"] if grant else None,
           "submitted_gcs": summary["submitted"], "no_bid_gcs": summary["no_bid"],
           "total_price": summary["total_price"]})
    _after_submit(project, project_id, user_id, grant)
    return overview(project_id, user_id, role)


def _after_submit(project: dict, project_id: str, user_id: str, grant: dict | None) -> None:
    """Best-effort follow-ups once the transaction committed: the same
    submission news "Done sending" sends, and the bell cleanup a walk through
    every lane would have done."""
    try:
        state = workflow.load_category_state(project_id)
        for cat in workflow.CATEGORY_ORDER:
            workflow._dismiss_stale_notifications(project_id, cat, state)
        dismiss_notifications(project_id=project_id, types=["proposal_send_failed"])
        # The apply closed every other open request on the project (pending
        # ones cancelled, unused grants retired), so every approver's request
        # row and every requester's "granted" row on it is moot now.
        dismiss_notifications(
            project_id=project_id, types=[REQUEST_TYPE, DECISION_TYPES["granted"]]
        )
        for r in (
            Role.ESTIMATING_ENGINEER_MATERIALS,
            Role.ESTIMATING_ENGINEER_LABOR,
            Role.EXECUTIVE,
        ):
            notify_role(
                r, project_id, "submitted",
                f"Bid marked submitted (sent outside the app) for {project['name']}",
            )
    except Exception:  # noqa: BLE001 (the submission is committed)
        logger.exception("post-submit follow-ups failed for project %s", project_id)


def undo(project_id: str, user_id: str, role, reason: str | None) -> dict:
    if role_kind(role) != "approver":
        raise ExternalSubmissionError("Not permitted", 403, "forbidden")
    sb = get_supabase()
    project = _project(sb, project_id)
    if not project:
        raise ExternalSubmissionError("Project not found", 404, "not_found")
    active = _active_submission(sb, project_id)
    if not active:
        raise ExternalSubmissionError(
            "There is no marked submission to undo on this project.", 409, "not_active"
        )
    if _has_outcome(sb, project_id):
        raise ExternalSubmissionError(
            "Undo is not available once a win/loss outcome is recorded.",
            409,
            "outcome_recorded",
        )
    text = (reason or "").strip() or None
    try:
        sb.rpc(
            "undo_external_submission",
            {"p_submission_id": active["id"], "p_user_id": user_id, "p_reason": text},
        ).execute()
    except APIError as exc:
        raise _db_error(exc) from exc
    audit(user_id, "external_submission.undone", "project", project_id,
          {"submission_id": active["id"], "reason": text})
    try:
        dismiss_notifications(project_id=project_id, types=["submitted"])
        submitter = active.get("submitted_by")
        if submitter and submitter != user_id:
            msg = (
                f"{_display_name(sb, user_id)} undid the submission you marked on "
                f"{_project_tag(project)}. The project is back where it was before."
            )
            if text:
                msg += f" Reason: {text}"
            notify_user(submitter, project_id, UNDONE_TYPE, msg, mirror_email=False,
                        metadata={"submission_id": active["id"]})
    except Exception:  # noqa: BLE001 (the undo is committed)
        logger.exception("post-undo follow-ups failed for project %s", project_id)
    return overview(project_id, user_id, role)

