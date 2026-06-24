"""Lightweight per-account rate limiting (in-memory, fixed window).

Applied to estimator-scoped routes as a basic abuse backstop. In-memory state
suits a single instance; for a multi-instance deployment, back this with Redis.
"""

import time
from collections import defaultdict

from fastapi import Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role

# user_id -> (window_start_epoch_minute, count)
_buckets: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))


async def estimator_rate_limit(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != Role.ESTIMATOR:
        return user
    limit = get_settings().estimator_rate_limit_per_min
    minute = int(time.time() // 60)
    start, count = _buckets[user.id]
    if start == minute:
        if count >= limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded; slow down"
            )
        _buckets[user.id] = (minute, count + 1)
    else:
        _buckets[user.id] = (minute, 1)
    return user
