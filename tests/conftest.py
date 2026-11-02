"""Shared test setup.

The local `.env` carries live Microsoft Graph credentials, so notification
emails would otherwise actually try to send during tests (the helpers self-gate
on `ms_client_id`, which is present). Force the feature off for the whole test
session — every test asserts on the in-app notification rows, never on email —
so no test spawns a sender thread or touches the network.
"""

import os

os.environ["NOTIFICATION_EMAILS_ENABLED"] = "false"
# Same for the Teams copy of each notification (services/teams_alerts).
os.environ["TEAMS_ALERTS_ENABLED"] = "false"
# Email ingestion defaults off, but pin it so a local .env that enables it can
# never make the test session poll a real mailbox.
os.environ["EMAIL_INGEST_ENABLED"] = "false"
# The daily due digest defaults on; pin it off so no test that runs the app
# lifespan can ever start the sender loop against real Graph creds.
os.environ["DUE_DIGEST_ENABLED"] = "false"
# The Calling In poller defaults on; pin it off so no test that runs the app
# lifespan can claim list entries or notify against a real database.
os.environ["CALL_IN_ENABLED"] = "false"
# The Auto send poller defaults on; pin it off so no test that runs the app
# lifespan can ever start the 5:05 PM GC sender against real Graph creds, and
# blank the test redirect so a local .env that sets one never changes what the
# suite asserts. Tests that exercise them patch get_settings themselves.
os.environ["AUTO_SEND_POLLER_ENABLED"] = "false"
os.environ["AUTO_SEND_GC_REDIRECT"] = ""
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
# The assistant defaults off; pin it so a dev .env that enables it can never
# start the claim pass or the scheduler under the suite. Tests that exercise
# the assistant patch get_settings themselves.
os.environ["ASSISTANT_ENABLED"] = "false"
os.environ["ASSISTANT_SCHEDULER_ENABLED"] = "false"
# The Bid File Splitter flag defaults FALSE (experimental tool); pin it ON so
# its router tests exercise real routes whatever the local .env says. Tests
# that exercise the flag being off set it themselves (test_bid_splitter).
os.environ["BID_FILE_SPLITTER_ENABLED"] = "true"
os.environ["PLANS_READER_ENABLED"] = "true"  # AI Estimating routes stay reachable in the suite
os.environ["AI_ESTIMATING_STATE"] = "live"
# The AI Estimating autonomous build (docs/AI_ESTIMATING_BUILD_SPEC.md 5.7):
# every parent flag is off in code and on in the dev .env, so pin the suite to
# the code defaults. Tests that exercise a flag being on set it themselves.
for _k, _v in (
    ("AI_PRICING_ENABLED", "false"),
    ("AI_PRICING_STATE", "test"),
    ("AI_PRICING_AUTORUN", "false"),
    ("AI_EVAL_LEDGER_ENFORCE", "false"),
    ("LLM_CASSETTE_MODE", "off"),
    ("EVAL_SWEEP_ID", ""),
    ("LLM_QUEUE_CLAIM_TYPES", ""),
    ("PLANS_READER_PAGE_FACTS_ENABLED", "false"),
    ("PLANS_READER_FACTS_RELABEL", "false"),
    ("PLANS_READER_DERIVE_ENABLED", "false"),
    ("PLANS_READER_MEASURE_ASSIST_ENABLED", "false"),
    ("PLANS_READER_CONSOLIDATOR_MODE", "legacy"),
    ("PLANS_READER_CODE_REGIONS", "false"),
    ("PLANS_READER_GATES_BLOCKING", "false"),
    ("PLANS_READER_RECOUNT_ENABLED", "false"),
    ("PLANS_READER_REVIEW_MODE", "off"),
    ("AI_SCOPE_POLICY_ENFORCE", "false"),
    ("AI_SCOPE_ATTRIB_LLM", "false"),
    ("AI_PRICING_CHAT_TOOLS_ENABLED", "false"),
    ("AI_PRICING_AGENT_TOOLS_ENABLED", "false"),
    ("QUOTE_LINES_LLM_FALLBACK_ENABLED", "false"),
    ("PLANS_READER_MARKED_PLANS_MODE", "model"),
    ("PLANS_READER_ANNOTATED_PLANS_ENABLED", "false"),
):
    os.environ[_k] = _v
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
# PM email classification defaults ON; pin it off so no assignment in the
# suite can queue a job against the real llm_jobs table (the hooks call the
# queue's own Supabase client). tests/test_email_classification.py turns it on
# with its own fakes.
os.environ["EMAIL_CLASSIFY_ENABLED"] = "false"
# The PM RFI sync (docs/PM_RFI_SYNC.md) is on in the dev .env; pin it off so
# no test that runs the app lifespan can start its scheduler loop against a
# real database or Procore. Its tests patch settings themselves.
os.environ["PM_RFI_SYNC_ENABLED"] = "false"
# Browser agents (docs/browser_agents/SPEC.md) default off; pin them off so
# no test that runs the app lifespan can start the sweeper against a real
# database. Their tests build their own Settings and patch get_settings.
os.environ["BROWSER_AGENTS_ENABLED"] = "false"
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

# The real (lru-cached) client factory, kept before any test patches the
# module attribute, so the guard below can always empty its cache.
from app.core.supabase_client import get_supabase as _REAL_GET_SUPABASE  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _routing_defaults(monkeypatch):
    """Notification routing (services/notification_routing, 0177) reads its
    overrides from Supabase. Every test runs on the code defaults instead,
    so no send path in the suite reaches a real database; routing tests
    patch `_overrides` themselves."""
    from app.services import notification_routing

    monkeypatch.setattr(notification_routing, "_overrides", lambda: {})


@pytest.fixture(autouse=True)
def _no_revision_lock(monkeypatch):
    """The Revise proposal lock (app/core/revision_context, 0179) reads
    Supabase on every locked write route. Route tests run with no revision
    open, so none of them reaches a real database; the revision tests put
    the real lookup back on their fake store."""
    from app.core import revision_context

    monkeypatch.setattr(revision_context, "_open_revision", lambda project_id: None)


@pytest.fixture(autouse=True)
def _assistant_offboarding_defaults(monkeypatch):
    """An MFA reset and a hard delete read the assistant's legal hold whatever
    ASSISTANT_ENABLED says (app/routers/users.py _assistant_tables). Every
    test runs as if the database had no assistant tables, so no user-admin
    test reaches a real database; tests/test_users_assistant_offboarding.py
    restores the real reader over its own stubs."""
    from app.routers import users

    monkeypatch.setattr(users, "_assistant_tables", lambda user_id: None)


class RealSupabaseBlocked(RuntimeError):
    """A test reached the real Supabase client (see _no_real_supabase)."""


@pytest.fixture(autouse=True)
def _no_real_supabase(monkeypatch):
    """The local .env carries the dev project's service-role key, so any path
    that calls the real get_supabase() (directly or through a module that
    bound it by name, such as services/notifications) would write to the dev
    database: a test once left assistant.offboarded audit rows and
    ai_spend.cap_hit bells for real users there. Every client goes through
    supabase_client.create_client behind the lru cache, so the cache is
    emptied and the constructor refuses for the whole test. Tests fake the
    client by patching get_supabase (or the writer) as before; a test that
    forgets fails or degrades here instead of reaching the network."""
    from app.core import supabase_client

    def _blocked(*args, **kwargs):
        raise RealSupabaseBlocked("tests must not reach the real Supabase; fake get_supabase or the writer")

    _REAL_GET_SUPABASE.cache_clear()
    monkeypatch.setattr(supabase_client, "create_client", _blocked)
    yield
    _REAL_GET_SUPABASE.cache_clear()
