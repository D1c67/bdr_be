"""Kill switches: who has a working assistant right now (docs/ASSISTANT.md,
Permissions and safety, items 3 and 15).

Four switches, all of which must be on:

  ASSISTANT_ENABLED            the deployment (env)
  assistant_settings.enabled   the company (IT Admin, one row, id = 1)
  assistant_profiles.enabled   the person (IT Admin editable)
  role in INTERNAL_ROLES       external estimators never get one

plus `assistant_settings.disabled_tools`, which hides single tools from the
registry. Pure helpers: the rows come in, nothing here touches the database,
so GET /features and the registry can share one decision.

The one exception is `owner_check` / `owner_problem` (phase 2,
docs/ASSISTANT_PHASE2_PLAN.md 3.7 step 2): the run-time eligibility of a job
owner, shared by the approval execute job, the assistant_continue job and
the split run scope. It reads the rows through app/assistant/repo.py
(imported lazily, so the pure helpers stay import-light).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role

# owner_problem reasons (the expired_reason / failure_reason values).
OWNER_CHANGED = "owner_changed"
FEATURE_OFF = "feature_off"

# OwnerCheck.detail: which OWNER_CHANGED test failed (phase 3, 3.4.3). The
# scheduler pauses a routine as no_delegation (auto-resumed by a fresh
# grant) and as owner_changed for every other detail (the user resumes).
DETAIL_INACTIVE = "inactive"
DETAIL_NOT_INTERNAL = "not_internal"
DETAIL_NOT_WRITER = "not_writer"
DETAIL_SWITCHED_OFF = "switched_off"
DETAIL_NO_DELEGATION = "no_delegation"


def _flag(row: Mapping[str, Any] | None, key: str, *, missing: bool) -> bool:
    """A boolean column read defensively: a row that is None takes
    `missing`, a column that is NULL takes `missing` too."""
    if row is None:
        return missing
    value = row.get(key)
    if value is None:
        return missing
    return bool(value)


def env_enabled() -> bool:
    """ASSISTANT_ENABLED; getattr so a settings stand-in without the field
    (the setting is added by the integration stage) fails closed."""
    return bool(getattr(get_settings(), "assistant_enabled", False))


def assistant_enabled_for(
    user: Any,
    settings_row: Mapping[str, Any] | None,
    profile_row: Mapping[str, Any] | None,
    *,
    env: bool | None = None,
) -> bool:
    """True when `user` has a working assistant.

    `settings_row` is the assistant_settings row (id = 1). A missing row
    fails closed: the migration seeds it, so its absence means the schema is
    not there. `profile_row` is the person's assistant_profiles row; a
    missing row counts as enabled because the row is created lazily and its
    column default is true (every internal user gets one). `env` overrides
    the ASSISTANT_ENABLED read for callers that already hold Settings."""
    if user is None or not getattr(user, "is_active", False):
        return False
    role = getattr(user, "role", None)
    try:
        role = Role(role)
    except ValueError:
        return False
    if role not in INTERNAL_ROLES:
        return False
    if not (env_enabled() if env is None else bool(env)):
        return False
    if not _flag(settings_row, "enabled", missing=False):
        return False
    return _flag(profile_row, "enabled", missing=True)


def disabled_tools(settings_row: Mapping[str, Any] | None) -> frozenset[str]:
    """assistant_settings.disabled_tools as a set of tool names."""
    if settings_row is None:
        return frozenset()
    raw = settings_row.get("disabled_tools") or ()
    return frozenset(str(t).strip() for t in raw if str(t).strip())


def tool_enabled(settings_row: Mapping[str, Any] | None, tool_name: str) -> bool:
    return tool_name not in disabled_tools(settings_row)


# ── Run-time owner eligibility (phase 2) ────────────────────────────────────


@dataclass(frozen=True)
class OwnerCheck:
    """What owner_check found: `problem` is None (eligible), OWNER_CHANGED or
    FEATURE_OFF; the rows ride along so a caller does not read them twice.
    `detail` names the failed test of an OWNER_CHANGED (a DETAIL_* value),
    else None."""

    problem: str | None
    role: Role | None
    human: dict | None
    settings_row: dict | None
    profile_row: dict | None
    detail: str | None = None


def owner_check(user_id: str, *, sub_app: str | None = None, require_writer: bool = False) -> OwnerCheck:
    """Whether the owner of a job may still have it run unattended, in
    order: an active profile with an internal role, a writer role when
    `require_writer`, a working assistant (assistant_enabled_for: every kill
    switch), a live unrevoked delegation (Permissions item 2), and `sub_app`
    enabled for the role. Account, role, switch and delegation problems give
    OWNER_CHANGED; a sub-app that is off gives FEATURE_OFF. Reads raise to
    the caller (a transport failure is not a verdict)."""
    from app.assistant import repo

    human = repo.human_profile(user_id)
    if not human or not human.get("is_active"):
        return OwnerCheck(OWNER_CHANGED, None, human, None, None, DETAIL_INACTIVE)
    try:
        role = Role(human.get("role"))
    except ValueError:
        return OwnerCheck(OWNER_CHANGED, None, human, None, None, DETAIL_NOT_INTERNAL)
    if role not in INTERNAL_ROLES:
        return OwnerCheck(OWNER_CHANGED, None, human, None, None, DETAIL_NOT_INTERNAL)
    settings_row = repo.settings_row(user_id)
    profile_row = repo.profile_row(user_id)
    if require_writer and role not in WRITER_ROLES:
        return OwnerCheck(OWNER_CHANGED, role, human, settings_row, profile_row, DETAIL_NOT_WRITER)
    if not assistant_enabled_for(SimpleNamespace(role=role, is_active=True), settings_row, profile_row):
        return OwnerCheck(OWNER_CHANGED, role, human, settings_row, profile_row, DETAIL_SWITCHED_OFF)
    if repo.live_delegation(user_id) is None:
        return OwnerCheck(OWNER_CHANGED, role, human, settings_row, profile_row, DETAIL_NO_DELEGATION)
    if sub_app is not None:
        from app.core import features

        if not features.enabled_map(role).get(sub_app, False):
            return OwnerCheck(FEATURE_OFF, role, human, settings_row, profile_row)
    return OwnerCheck(None, role, human, settings_row, profile_row)


def owner_problem(user_id: str, *, sub_app: str | None = "bidding", require_writer: bool = True) -> str | None:
    """None when the owner may still run assistant work in `sub_app`, else
    OWNER_CHANGED or FEATURE_OFF (owner_check). The defaults are the split
    task's: a writer with bidding on (the continuation and the split run
    scope call it as `owner_problem(user_id, sub_app="bidding")`)."""
    return owner_check(user_id, sub_app=sub_app, require_writer=require_writer).problem
