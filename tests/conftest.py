"""Shared test setup.

The local `.env` carries live Microsoft Graph credentials, so notification
emails would otherwise actually try to send during tests (the helpers self-gate
on `ms_client_id`, which is present). Force the feature off for the whole test
session — every test asserts on the in-app notification rows, never on email —
so no test spawns a sender thread or touches the network.
"""

import os

os.environ["NOTIFICATION_EMAILS_ENABLED"] = "false"
# Email ingestion defaults off, but pin it so a local .env that enables it can
# never make the test session poll a real mailbox.
os.environ["EMAIL_INGEST_ENABLED"] = "false"
# Pin the security-critical flags the tests assert on, so the suite is
# independent of whatever the local dev `.env` happens to set. The dev `.env`
# ships MFA_REQUIRED=false (a break-glass convenience); without this pin the 2FA
# enforcement tests would silently pass-through and fail. Tests that need it off
# monkeypatch get_settings explicitly.
os.environ["MFA_REQUIRED"] = "true"
os.environ["ENVIRONMENT"] = "test"
# Every sub-app is served during the suite, whatever a local .env is rehearsing.
# The flags default true, but pinning them keeps the whole suite independent of a
# developer who has temporarily switched a module off — otherwise a PM or CP test
# would fail with a bare 404 that looks nothing like the real cause. Tests that
# exercise a module being OFF set the flag themselves (see test_feature_flags).
os.environ["BIDDING_ENABLED"] = "true"
os.environ["PM_ENABLED"] = "true"
os.environ["CERTIFIED_PAYROLL_ENABLED"] = "true"
# Pin LLM routing to the 3rd-party pool so a local .env experimenting with
# self-hosted models can never redirect (or break) the suite's LLM stubs, and
# drop any shell-exported LLM knobs (.env.example documents them as the
# experimentation surface) — the routing tests assert on the field defaults.
os.environ["FULL_SELF_HOSTED_LLMS_ENABLED"] = "false"
for _var in list(os.environ):
    if _var.startswith("SELF_HOSTED_") or _var.startswith(("CLAUDE_", "OPENAI_")):
        if _var.endswith("_API_KEY"):
            continue  # key presence is orthogonal; tests always override keys
        os.environ.pop(_var)

# get_settings() is lru-cached; drop any value created before this flag was set.
from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()
