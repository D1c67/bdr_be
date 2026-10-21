"""Auto send hooks on the estimator's submit (docs/AUTO_SEND.md 6.1).

When an estimator seals a round, two things the 5:05 PM auto send needs can
start early, so neither is a surprise on the bid date:

  * an estimate workbook in the round warms the Bid Recap parse cache
    (auto_send_estimate.load_estimate), off the request thread;
  * a BOQ in the round starts a system scope-lines draft when the project has
    no draft yet (failed ones aside) and nothing was sent. A draft a person
    made or edited is never replaced: a newer row would outrank it and drop
    their edits (docs: there is one proposal_drafts row per generation).

Everything here is best effort. The caller wraps the call too, but each part
is isolated so a cache hiccup never stops the draft and neither ever fails an
estimator's submit. Sync code only (the submit route is a sync def).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.supabase_client import get_supabase
from app.services import estimator_rounds, proposal_scope

logger = logging.getLogger(__name__)


def on_estimator_round(project_id: str, counts: dict[str, int] | None, background: Any) -> None:
    """Called by routers/estimator.submit_deliverables after every sealed
    round (round 1 and the revision rounds alike: a BOQ may first arrive in a
    later round, and a second estimator's first submit lands there too).
    `counts` is the round summary ({"estimate": 1, "boq": 1, ...});
    `background` is the request's BackgroundTasks."""
    counts = counts or {}
    if counts.get("estimate"):
        try:
            if background is not None:
                background.add_task(warm_estimate, project_id)
        except Exception:  # noqa: BLE001
            logger.exception("auto send: estimate warm-up dispatch failed for %s", project_id)
    if counts.get("boq"):
        try:
            maybe_start_scope_lines(project_id, background)
        except Exception:  # noqa: BLE001
            logger.exception("auto send: scope-lines start failed for %s", project_id)


def warm_estimate(project_id: str) -> None:
    """Background task: parse the newest sealed estimate workbook into the
    auto_send_estimates cache. load_estimate never raises by contract; the
    guard covers the import and anything unexpected."""
    try:
        from app.services import auto_send_estimate

        auto_send_estimate.load_estimate(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("auto send: estimate warm-up failed for %s", project_id)


def maybe_start_scope_lines(project_id: str, background: Any = None) -> dict | None:
    """Start a system scope-lines draft when nothing would be overwritten.

    Skips when the project is abandoned, has any proposal_drafts row other
    than failed ones (pending, running or done: someone's draft, or one
    already on the way), or has a proposal at sent or sending (the lines are
    locked then). Also skips when no sent or internal BOQ resolves. Returns
    the inserted draft row, or None when it skipped."""
    sb = get_supabase()
    project = (
        sb.table("projects").select("abandoned_at").eq("id", project_id).limit(1).execute()
    ).data or []
    if not project or project[0].get("abandoned_at"):
        return None
    drafts = (
        sb.table("proposal_drafts")
        .select("id")
        .eq("project_id", project_id)
        .neq("status", "failed")
        .limit(1)
        .execute()
    ).data or []
    if drafts:
        return None
    sent = (
        sb.table("proposal_sends")
        .select("id")
        .eq("project_id", project_id)
        .in_("status", ["sent", "sending"])
        .limit(1)
        .execute()
    ).data or []
    if sent:
        return None
    try:
        boq_file_id = estimator_rounds.resolve_boq_file_id(project_id, None)
    except Exception:  # noqa: BLE001 - no sent BOQ is a skip, not a failure
        logger.info("auto send: no usable BOQ for %s, scope lines not started", project_id)
        return None
    return proposal_scope.start_generation(project_id, boq_file_id, None, background)
