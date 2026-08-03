"""Per-account rate limiting (in-memory, sliding-window approximation).

A small abuse/DoS backstop applied to estimator-scoped routes and to the
expensive/abusable internal routes (AI jobs, outbound email, uploads, exports).
State is per-process: with N uvicorn workers (see the Dockerfile CMD) the
effective cap is up to N× the configured limit — fine for a backstop. For a
multi-instance deployment back the counter with Redis or a Supabase table keyed
on (scope, user_id, window).

Every limit raises HTTP 429 with a stable ``rate_limited`` code (see
app/core/error_codes.py) plus:
  * ``Retry-After``       — seconds until the window resets, so a client can wait
  * ``X-RateLimit-Scope`` — which limit tripped, for support/debugging
so a legitimate user who trips a limit gets an actionable, code-tagged message
rather than a bare "slow down". The frontend surfaces this (bdr_fe/lib/api.ts),
and docs/ERROR_CODES.md documents every code for developers.
"""

import time
from collections import defaultdict
from collections.abc import Callable

from fastapi import Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.roles import Role

# (scope, user_id) -> (window_index, count_this_window, count_prev_window)
# The previous-window count feeds a sliding-window approximation that removes the
# 2x burst a naive fixed window allows right at a window boundary.
_buckets: dict[tuple[str, str], tuple[int, int, int]] = defaultdict(lambda: (0, 0, 0))

# Bound memory: opportunistically drop stale buckets once the map grows large.
# Keys are (scope, user_id); for an internal app this is naturally small, but a
# flood of distinct ids must not grow the map without bound.
_MAX_BUCKETS = 20_000


def _prune(current_window: int) -> None:
    stale = [k for k, (w, _, _) in _buckets.items() if w < current_window - 1]
    for k in stale:
        del _buckets[k]


def _check(scope: str, user_id: str, limit: int, window_seconds: int = 60) -> None:
    """Record one hit for (scope, user_id); raise 429 if over the limit."""
    if not get_settings().rate_limit_enabled:
        return
    now = time.time()
    window = int(now // window_seconds)
    w, count, prev = _buckets[(scope, user_id)]
    if w == window:
        cur_count, prev_count = count, prev
    elif w == window - 1:
        cur_count, prev_count = 0, count
    else:
        cur_count, prev_count = 0, 0

    # Weight the previous window by the fraction of it still "in view".
    elapsed_fraction = (now % window_seconds) / window_seconds
    estimated = prev_count * (1 - elapsed_fraction) + cur_count
    if estimated >= limit:
        retry_after = window_seconds - int(now % window_seconds)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ErrorCode.RATE_LIMITED,
            headers={"Retry-After": str(retry_after), "X-RateLimit-Scope": scope},
        )

    _buckets[(scope, user_id)] = (window, cur_count + 1, prev_count)
    if len(_buckets) > _MAX_BUCKETS:
        _prune(window)


def rate_limit(
    scope: str,
    limit_getter: Callable[[], int],
    *,
    roles: frozenset[Role] | None = None,
    window_seconds: int = 60,
):
    """Build a FastAPI dependency that rate-limits per authenticated account.

    ``limit_getter`` is read at request time so config changes and tests take
    effect. ``roles`` narrows the limit to specific roles (None = all roles).
    """

    async def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if roles is not None and user.role not in roles:
            return user
        _check(scope, user.id, limit_getter(), window_seconds)
        return user

    return _dep


# ── Concrete limiters ─────────────────────────────────────────────────────────

# The external estimator's per-account cap (unchanged wiring across the app).
estimator_rate_limit = rate_limit(
    RateLimitScope.ESTIMATOR_API,
    lambda: get_settings().estimator_rate_limit_per_min,
    roles=frozenset({Role.ESTIMATOR}),
)

# Expensive/abusable surfaces — applied to ALL roles (the whole point is to cover
# internal accounts, which the estimator limiter deliberately skips).
ai_rate_limit = rate_limit(
    RateLimitScope.AI_JOBS, lambda: get_settings().ai_rate_limit_per_min
)
upload_rate_limit = rate_limit(
    RateLimitScope.FILE_UPLOAD, lambda: get_settings().upload_rate_limit_per_min
)
export_rate_limit = rate_limit(
    RateLimitScope.FILE_EXPORT, lambda: get_settings().export_rate_limit_per_min
)
bulk_send_rate_limit = rate_limit(
    RateLimitScope.BULK_SEND, lambda: get_settings().bulk_send_rate_limit_per_min
)
outbound_email_rate_limit = rate_limit(
    RateLimitScope.OUTBOUND_EMAIL,
    lambda: get_settings().outbound_email_rate_limit_per_hour,
    window_seconds=3600,
)
# The notification log is the app's most query-amplifying read: one request
# assembles the event view from dozens of project-scoped lookups.
notification_log_rate_limit = rate_limit(
    RateLimitScope.NOTIFICATION_LOG,
    lambda: get_settings().notification_log_rate_limit_per_min,
)
# The bid-invitations JSON report scans several tables across the whole window
# on every call; the export already sits behind export_rate_limit.
report_rate_limit = rate_limit(
    RateLimitScope.REPORT, lambda: get_settings().report_rate_limit_per_min
)
