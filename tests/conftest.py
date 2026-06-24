"""Shared test setup.

The local `.env` carries live Microsoft Graph credentials, so notification
emails would otherwise actually try to send during tests (the helpers self-gate
on `ms_client_id`, which is present). Force the feature off for the whole test
session — every test asserts on the in-app notification rows, never on email —
so no test spawns a sender thread or touches the network.
"""

import os

os.environ["NOTIFICATION_EMAILS_ENABLED"] = "false"

# get_settings() is lru-cached; drop any value created before this flag was set.
from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()
