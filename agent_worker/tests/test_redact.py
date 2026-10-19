"""Owner-scoped secrets: a job's secrets are scrubbed while it runs and
forgotten when it ends; process secrets stay."""

import base64
from urllib.parse import quote

from bdr_agent.redact import MASK, LiveSecrets


def test_job_secrets_are_dropped_with_the_job():
    s = LiveSecrets()
    s.add("bdrw_worker_token_value")
    s.add("Corr3ct-Horse-Battery!", owner="job-1")
    s.add("481516", owner="job-2")
    pw = "Corr3ct-Horse-Battery!"
    text = f"a {pw} b {quote(pw, safe='')} c {base64.b64encode(pw.encode()).decode()} d 481516"
    assert s.contains(text)
    assert s.scrub_text(text).count(MASK) == 4
    s.discard_owner("job-1")
    assert pw in s.scrub_text(text)
    assert "481516" not in s.scrub_text(text)
    assert s.scrub_text("token bdrw_worker_token_value") == f"token {MASK}"
    s.discard_owner("job-2")
    s.discard_owner("never-registered")
    assert not s.contains("481516")
    assert s.contains("bdrw_worker_token_value")


def test_short_values_are_ignored():
    s = LiveSecrets()
    s.add("12345", owner="j")
    assert len(s) == 0 and not s.contains("12345")


def test_scrub_only_values_are_masked_but_never_trip():
    from bdr_agent.redact import LiveSecrets

    s = LiveSecrets()
    s.add("estimating@g3.example", owner="job", tripwire=False)
    s.add("Corr3ct-Horse-Battery!", owner="job")
    assert not s.contains("Created by estimating@g3.example")
    assert s.scrub_text("Created by estimating@g3.example") == "Created by [redacted]"
    assert s.contains("pw Corr3ct-Horse-Battery!")
    s.discard_owner("job")
    assert len(s) == 0 and not s.contains("Corr3ct-Horse-Battery!")
    assert s.scrub_text("estimating@g3.example") == "estimating@g3.example"


def test_only_session_cookies_are_tripwires():
    from bdr_agent.agents.pm_apps.procore.identity import is_credential_cookie

    assert is_credential_cookie("_session_id", "a3f9c2e81b7d4e6fa0c9d2b1")
    assert is_credential_cookie("procore_auth_token", "eyJhbGciOiJIUzI1NiJ9.abc")
    assert not is_credential_cookie("locale", "en-US-xyz")
    assert not is_credential_cookie("ajs_user_id", "123456789")
    assert not is_credential_cookie("_session_id", "1234567890123456789")   # all digits
    assert not is_credential_cookie("__cf_bm_session", "short")
    assert not is_credential_cookie("company_id", "8089xxxxxxxxxxxxxxxxxx")
