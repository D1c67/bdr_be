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
# The daily due digest defaults on; pin it off so no test that runs the app
# lifespan can ever start the sender loop against real Graph creds.
os.environ["DUE_DIGEST_ENABLED"] = "false"
# The Calling In poller defaults on; pin it off so no test that runs the app
# lifespan can claim list entries or notify against a real database.
os.environ["CALL_IN_ENABLED"] = "false"
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
# The Bid File Splitter flag defaults FALSE (experimental tool); pin it ON so
# its router tests exercise real routes whatever the local .env says. Tests
# that exercise the flag being off set it themselves (test_bid_splitter).
os.environ["BID_FILE_SPLITTER_ENABLED"] = "true"
# The RFP Ingestion sandbox flag defaults FALSE too (experimental tool); pin it
# ON so its router, gate and queue-pass tests exercise the real code paths.
# Tests that exercise the flag being off set it themselves (test_feature_flags).
os.environ["RFP_INGEST_ENABLED"] = "true"
# The email intake rides the same switch but also needs a watched mailbox.
os.environ["RFP_EMAIL_INGESTION_INBOXES_ALLOWED"] = "rfp-test@example.com"
# The project data harvest defaults OFF; pin it off and blank the Procore
# credentials so a local .env used for live harvesting can never route a
# pipeline test to Procore (or its session store to the real database).
# Tests that exercise the harvest patch rfp_harvest.get_settings themselves.
os.environ["RFP_HARVEST_ENABLED"] = "false"
os.environ["PROCORE_LOGIN_EMAIL"] = ""
os.environ["PROCORE_LOGIN_PASSWORD"] = ""
# The NGEM portal slice defaults OFF too; pin it off and blank the supplier
# account so a local .env used for live scans can never start the scheduler
# loop or route a test to the portal. Tests that exercise it patch
# rfp_portal_ingest.get_settings themselves.
os.environ["RFP_NGEM_ENABLED"] = "false"
os.environ["NGEM_LOGIN_USERNAME"] = ""
os.environ["NGEM_LOGIN_PASSWORD"] = ""
os.environ["NGEM_ENTRY_URL"] = ""
# The BuildingConnected slice defaults OFF; the local .env enables it (under
# the older BUILDING_CONNECTED_ENABLED name) with a real APS client id and
# secret. Pin BOTH switch names off (the first alias listed wins when both
# are set, and an OS value beats the .env) and blank the credentials so the
# suite can never start the scheduler or reach Autodesk. Tests that
# exercise it build their own Settings and patch get_settings themselves.
os.environ["RFP_BC_ENABLED"] = "false"
os.environ["BUILDING_CONNECTED_ENABLED"] = "false"
os.environ["BUILDING_CONNECTED_CLIENT_ID"] = ""
os.environ["BUILDING_CONNECTED_CLIENT_SECRET"] = ""
os.environ["RFP_BC_TEST_MODE"] = "false"
os.environ["RFP_BC_REDIRECT_URL"] = ""
# The RFP test bench defaults OFF; pin it off so the local .env that runs it
# (docs/RFP_TESTING.md 2) can never make a poller or a send in the suite
# consult the session table. Tests that exercise it patch
# rfp_test.get_settings (and the router's) themselves.
os.environ["RFP_TESTING_ENABLED"] = "false"
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
