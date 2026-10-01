"""BuildingConnected settings, feature flag, queue gate, bell registry and the
conftest pins (docs/RFP_BUILDINGCONNECTED.md section 9, build contract 3.7,
D4 to D7)."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import features
from app.core.config import Settings, get_settings
from app.services import llm_queue as lq
from app.services import notification_email
from tests.test_llm_queue import _queue_env

_BC_ENV = (
    "RFP_BC_ENABLED",
    "BUILDING_CONNECTED_ENABLED",
    "BUILDING_CONNECTED_CLIENT_ID",
    "BUILDING_CONNECTED_CLIENT_SECRET",
    "RFP_BC_TEST_MODE",
    "RFP_BC_REDIRECT_URL",
)
_PROD = dict(
    environment="production", supabase_service_role_key="k", mfa_required=True,
    preview_engine="graph",
)
_CREDS = dict(building_connected_client_id="cid", building_connected_client_secret="sec")


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


# ── Defaults ──────────────────────────────────────────────────────────────


def test_defaults_match_the_contract():
    s = _s()
    assert s.rfp_bc_enabled is False
    assert s.building_connected_client_id == "" and s.building_connected_client_secret == ""
    assert s.rfp_bc_redirect_url == ""
    assert s.rfp_bc_poll_minutes == 15 and s.rfp_bc_full_sync_time == "02:30"
    assert s.rfp_bc_full_sync_slot == (2, 30)
    assert s.rfp_bc_full_sync_catchup_hours == 4 and s.rfp_bc_overlap_minutes == 30
    assert s.rfp_bc_expire_days == 7 and s.rfp_bc_missing_days == 3
    assert s.rfp_bc_text_max_chars == 20000 and s.rfp_bc_request_timeout_seconds == 60.0
    assert s.rfp_bc_test_mode is False
    assert s.rfp_bc_scan_queue_priority == 140 < s.rfp_bc_full_sync_queue_priority == 150
    assert s.rfp_bc_auto_resolve_enabled is True and s.rfp_bc_max_pages == 200
    assert s.bc_configured is False and s.rfp_bc_active is False
    # The field default (not the pinned env) is what production inherits.
    assert Settings.model_fields["rfp_bc_enabled"].default is False
    assert Settings.model_fields["rfp_bc_test_mode"].default is False


# ── Env aliases (D4) ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"RFP_BC_ENABLED": "true", "BUILDING_CONNECTED_ENABLED": "false"}, True),
        ({"RFP_BC_ENABLED": "false", "BUILDING_CONNECTED_ENABLED": "true"}, False),
        ({"RFP_BC_ENABLED": "true"}, True),
        ({"BUILDING_CONNECTED_ENABLED": "true"}, True),
        ({"BUILDING_CONNECTED_ENABLED": "false"}, False),
        ({}, False),
    ],
)
def test_rfp_bc_enabled_alias_precedence(monkeypatch, env, expected):
    """RFP_BC_ENABLED is listed first and wins; the older
    BUILDING_CONNECTED_ENABLED is read when it is the only one set."""
    monkeypatch.delenv("RFP_BC_ENABLED", raising=False)
    monkeypatch.delenv("BUILDING_CONNECTED_ENABLED", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert _s().rfp_bc_enabled is expected


def test_rfp_bc_enabled_alias_from_a_dotenv_file(monkeypatch, tmp_path):
    """The local .env carries the legacy name; with no OS value it is read."""
    monkeypatch.delenv("RFP_BC_ENABLED", raising=False)
    monkeypatch.delenv("BUILDING_CONNECTED_ENABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("BUILDING_CONNECTED_ENABLED=true\n", encoding="utf-8")
    assert Settings(_env_file=env_file).rfp_bc_enabled is True
    env_file.write_text(
        "RFP_BC_ENABLED=false\nBUILDING_CONNECTED_ENABLED=true\n", encoding="utf-8"
    )
    assert Settings(_env_file=env_file).rfp_bc_enabled is False


def test_rfp_bc_enabled_is_constructible_by_field_name():
    assert _s(rfp_bc_enabled=True).rfp_bc_enabled is True
    assert _s(rfp_bc_enabled=False).rfp_bc_enabled is False


def test_client_credentials_read_their_env_names(monkeypatch):
    monkeypatch.setenv("BUILDING_CONNECTED_CLIENT_ID", "cid-env")
    monkeypatch.setenv("BUILDING_CONNECTED_CLIENT_SECRET", "sec-env")
    monkeypatch.setenv("RFP_BC_REDIRECT_URL", "https://api.example.com/cb")
    s = _s()
    assert s.building_connected_client_id == "cid-env"
    assert s.building_connected_client_secret == "sec-env"
    assert s.rfp_bc_redirect_url == "https://api.example.com/cb"
    assert s.bc_configured is True


# ── Validator (D5, D6) ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, slot",
    [("02:30", (2, 30)), ("2:30", (2, 30)), ("00:00", (0, 0)), ("23:59", (23, 59)), (" 06:05 ", (6, 5))],
)
def test_full_sync_time_accepts_one_hhmm(raw, slot):
    assert _s(rfp_bc_full_sync_time=raw).rfp_bc_full_sync_slot == slot


@pytest.mark.parametrize(
    "raw", ["02:30,03:00", "24:00", "02:60", "0230", "ab:cd", "", "2:3:0", "-1:30", "002:30"]
)
def test_full_sync_time_refuses_anything_but_one_hhmm(raw):
    with pytest.raises(ValidationError, match="RFP_BC_FULL_SYNC_TIME"):
        _s(rfp_bc_full_sync_time=raw)


@pytest.mark.parametrize("minutes", [5, 6, 10, 12, 15, 20, 30, 60])
def test_poll_minutes_accepts_every_divisor_of_60_from_5(minutes):
    assert _s(rfp_bc_poll_minutes=minutes).rfp_bc_poll_minutes == minutes


@pytest.mark.parametrize("minutes", [0, 1, 4, 7, 9, 25, 45, 61, 120, -15])
def test_poll_minutes_refuses_off_grid_or_out_of_range(minutes):
    with pytest.raises(ValidationError, match="RFP_BC_POLL_MINUTES"):
        _s(rfp_bc_poll_minutes=minutes)


@pytest.mark.parametrize(
    "field, env, good, bad",
    [
        ("rfp_bc_full_sync_catchup_hours", "RFP_BC_FULL_SYNC_CATCHUP_HOURS", (1, 23), (0, 24)),
        ("rfp_bc_overlap_minutes", "RFP_BC_OVERLAP_MINUTES", (0, 1440), (-1, 1441)),
        ("rfp_bc_expire_days", "RFP_BC_EXPIRE_DAYS", (1, 365), (0, 366)),
        ("rfp_bc_missing_days", "RFP_BC_MISSING_DAYS", (1, 365), (0, 366)),
        ("rfp_bc_text_max_chars", "RFP_BC_TEXT_MAX_CHARS", (1000, 20000), (999, 20001)),
        ("rfp_bc_request_timeout_seconds", "RFP_BC_REQUEST_TIMEOUT_SECONDS", (0.1, 600), (0, 601)),
        ("rfp_bc_max_pages", "RFP_BC_MAX_PAGES", (1, 10000), (0, 10001)),
        ("rfp_bc_scan_queue_priority", "RFP_BC_SCAN_QUEUE_PRIORITY", (0, 1000), (-1, -100)),
        ("rfp_bc_full_sync_queue_priority", "RFP_BC_FULL_SYNC_QUEUE_PRIORITY", (0, 1000), (-1, -100)),
    ],
)
def test_integer_bounds(field, env, good, bad):
    for value in good:
        assert getattr(_s(**{field: value}), field) == value
    for value in bad:
        with pytest.raises(ValidationError, match=env):
            _s(**{field: value})


@pytest.mark.parametrize(
    "url",
    ["", "http://localhost:5051/rfp-portal/buildingconnected/callback",
     "https://api.example.com/rfp-portal/buildingconnected/callback"],
)
def test_redirect_url_accepts_blank_or_http_urls_outside_production(url):
    assert _s(rfp_bc_redirect_url=url).rfp_bc_redirect_url == url


@pytest.mark.parametrize(
    "url",
    ["localhost:5051/cb", "ftp://example.com/cb", " https://api.example.com/cb",
     "https://api.example.com/cb ", "https://api.example.com/c b"],
)
def test_redirect_url_refuses_non_urls(url):
    with pytest.raises(ValidationError, match="RFP_BC_REDIRECT_URL"):
        _s(rfp_bc_redirect_url=url)


def test_redirect_url_not_required_outside_production_even_when_enabled():
    s = _s(rfp_ingest_enabled=True, rfp_bc_enabled=True, **_CREDS)
    assert s.rfp_bc_redirect_url == "" and s.rfp_bc_active is True


# ── Production refusals (D5, D6) ──────────────────────────────────────────


def test_production_boots_with_bc_off():
    assert _s(**_PROD).rfp_bc_enabled is False


@pytest.mark.parametrize("url", ["", "http://api.example.com/rfp-portal/buildingconnected/callback"])
def test_production_refuses_an_enabled_slice_without_an_https_redirect(url):
    with pytest.raises(ValidationError, match="RFP_BC_REDIRECT_URL"):
        _s(**_PROD, rfp_ingest_enabled=True, rfp_bc_enabled=True, rfp_bc_redirect_url=url, **_CREDS)


def test_production_accepts_an_enabled_slice_with_an_https_redirect():
    url = "https://api.example.com/rfp-portal/buildingconnected/callback"
    s = _s(**_PROD, rfp_ingest_enabled=True, rfp_bc_enabled=True, rfp_bc_redirect_url=url, **_CREDS)
    assert s.rfp_bc_active is True


def test_production_redirect_rule_needs_both_switches():
    # The master switch off: the slice is not enabled, no redirect needed.
    assert _s(**_PROD, rfp_ingest_enabled=False, rfp_bc_enabled=True).rfp_bc_enabled is True
    assert _s(**_PROD, rfp_ingest_enabled=True, rfp_bc_enabled=False).rfp_bc_enabled is False


def test_production_refuses_test_mode_whatever_the_switches():
    with pytest.raises(ValidationError, match="RFP_BC_TEST_MODE=true in production"):
        _s(**_PROD, rfp_bc_test_mode=True)
    url = "https://api.example.com/rfp-portal/buildingconnected/callback"
    with pytest.raises(ValidationError, match="RFP_BC_TEST_MODE=true in production"):
        _s(**_PROD, rfp_ingest_enabled=True, rfp_bc_enabled=True, rfp_bc_redirect_url=url,
           rfp_bc_test_mode=True, **_CREDS)


def test_test_mode_is_allowed_outside_production():
    assert _s(rfp_bc_test_mode=True).rfp_bc_test_mode is True
    assert _s(environment="test", rfp_bc_test_mode=True).rfp_bc_test_mode is True
    # Fail closed: staging (any non-dev label) gets the production refusal.
    with pytest.raises(ValidationError, match="RFP_BC_TEST_MODE=true in production"):
        _s(**{**_PROD, "environment": "staging"}, rfp_bc_test_mode=True)


def test_the_refusal_never_echoes_the_secret():
    with pytest.raises(ValidationError) as exc:
        _s(**_PROD, rfp_ingest_enabled=True, rfp_bc_enabled=True,
           building_connected_client_id="cid", building_connected_client_secret="s3cr3t-value")
    assert "s3cr3t-value" not in str(exc.value)


# ── Derived properties (D4) ───────────────────────────────────────────────


def test_bc_configured_needs_both_non_blank_credentials():
    assert _s(**_CREDS).bc_configured is True
    assert _s(building_connected_client_id="cid").bc_configured is False
    assert _s(building_connected_client_secret="sec").bc_configured is False
    assert _s(building_connected_client_id="  ", building_connected_client_secret="sec").bc_configured is False
    assert _s(building_connected_client_id="cid", building_connected_client_secret="   ").bc_configured is False


def test_rfp_bc_active_needs_every_switch_credentials_and_the_queue():
    on = dict(rfp_ingest_enabled=True, rfp_bc_enabled=True, llm_queue_enabled=True, **_CREDS)
    assert _s(**on).rfp_bc_active is True
    assert _s(**{**on, "rfp_ingest_enabled": False}).rfp_bc_active is False
    assert _s(**{**on, "rfp_bc_enabled": False}).rfp_bc_active is False
    assert _s(**{**on, "llm_queue_enabled": False}).rfp_bc_active is False
    assert _s(**{**on, "building_connected_client_secret": ""}).rfp_bc_active is False


_NGEM_CREDS = dict(
    ngem_login_username="acct", ngem_login_password="pw",
    ngem_entry_url="https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=x",
)


@pytest.mark.parametrize(
    "over, any_active, any_enabled",
    [
        ({}, False, False),
        ({"rfp_ingest_enabled": True}, False, False),
        # Switches on, nothing configured: enabled (routes, queue) but not active (loop).
        ({"rfp_ingest_enabled": True, "rfp_bc_enabled": True}, False, True),
        ({"rfp_ingest_enabled": True, "rfp_ngem_enabled": True}, False, True),
        ({"rfp_ingest_enabled": True, "rfp_bc_enabled": True, **_CREDS}, True, True),
        ({"rfp_ingest_enabled": True, "rfp_ngem_enabled": True, **_NGEM_CREDS}, True, True),
        ({"rfp_ingest_enabled": True, "rfp_ngem_enabled": True, "rfp_bc_enabled": True,
          **_CREDS, **_NGEM_CREDS}, True, True),
        # The master switch off beats both portal switches.
        ({"rfp_ingest_enabled": False, "rfp_ngem_enabled": True, "rfp_bc_enabled": True,
          **_CREDS, **_NGEM_CREDS}, False, False),
        # The queue off: still enabled, never active.
        ({"rfp_ingest_enabled": True, "rfp_bc_enabled": True, "llm_queue_enabled": False,
          **_CREDS}, False, True),
    ],
)
def test_any_portal_properties(over, any_active, any_enabled):
    s = _s(**over)
    assert s.rfp_portal_any_active is any_active
    assert s.rfp_portal_any_enabled is any_enabled
    assert s.rfp_portal_any_active is (s.rfp_ngem_active or s.rfp_bc_active)


# ── Feature flag (D4) ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "over, expected",
    [
        ({"rfp_ingest_enabled": True, "rfp_bc_enabled": True}, True),
        # Two switches only: no credentials needed (the settings block says "not configured").
        ({"rfp_ingest_enabled": True, "rfp_bc_enabled": True, **_CREDS}, True),
        ({"rfp_ingest_enabled": True, "rfp_bc_enabled": False, **_CREDS}, False),
        ({"rfp_ingest_enabled": False, "rfp_bc_enabled": True, **_CREDS}, False),
    ],
)
def test_features_key(monkeypatch, over, expected):
    monkeypatch.setattr(features, "get_settings", lambda: _s(**over))
    flags = features.enabled_map()
    assert flags["rfp_buildingconnected"] is expected
    # Independent of the NGEM key.
    assert flags["rfp_ngem"] is False


def test_features_key_under_the_suite_pins_is_off():
    assert features.enabled_map()["rfp_buildingconnected"] is False


# ── Queue claim pass (D4: any portal) ─────────────────────────────────────


@pytest.mark.parametrize(
    "over, claims_portal",
    [
        ({"rfp_ingest_enabled": True, "rfp_bc_enabled": True}, True),
        ({"rfp_ingest_enabled": True, "rfp_ngem_enabled": True}, True),
        ({"rfp_ingest_enabled": True}, False),
        ({"rfp_ingest_enabled": False, "rfp_bc_enabled": True}, False),
    ],
)
def test_claim_tick_portal_pass_follows_any_portal(monkeypatch, over, claims_portal):
    db = _queue_env(monkeypatch)
    s = _s(rfp_harvest_enabled=False, **over)
    scan = lq.enqueue(lq.JOB_PORTAL_SCAN, target_id="r1", payload={"run_id": "r1"}, priority=140)
    claimed = lq._claim_tick(s, [])
    portal_calls = [c for c in db.rpc_calls if set(c.get("job_types") or []) & set(lq.PORTAL_JOB_TYPES)]
    if claims_portal:
        assert [j["id"] for j in claimed] == [scan["id"]]
        assert portal_calls and portal_calls[-1]["job_types"] == [
            *lq.PORTAL_JOB_TYPES, lq.JOB_RFP_CREATE_FILES
        ]
    else:
        assert claimed == [] and portal_calls == []


# ── Bell registry (D7) ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "type_, heading, cta",
    [
        ("rfp_bc.new_invitations", "New BuildingConnected invitations", "Open the BuildingConnected tab"),
        ("rfp_bc.invitation_changed", "A BuildingConnected invitation changed",
         "Open the BuildingConnected tab"),
        ("rfp_bc.scan_failed", "A BuildingConnected scan failed", "Open RFP ingestion settings"),
        ("rfp_bc.disconnected", "BuildingConnected disconnected", "Open RFP ingestion settings"),
    ],
)
def test_bell_types_are_registered(type_, heading, cta):
    assert notification_email._TYPE_META[type_] == (heading, cta)
    assert notification_email.heading_for(type_) == heading


# ── main.py (lifespan gate, router marker) ────────────────────────────────


_MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_main_carries_the_bc_router_marker_or_its_replacement():
    """Agent B leaves `# BC router include` right after the rfp_portal
    include; agent G later replaces it with the real include line."""
    lines = _MAIN.read_text(encoding="utf-8").splitlines()
    idx = lines.index("app.include_router(rfp_portal.router)")
    nxt = lines[idx + 1].strip()
    assert nxt == "# BC router include" or nxt.startswith("app.include_router(rfp_bc.")


def test_main_starts_the_portal_loop_under_the_any_portal_gate():
    src = _MAIN.read_text(encoding="utf-8")
    assert "if settings.rfp_portal_any_active and settings.supabase_url:" in src
    assert "if settings.rfp_ngem_active and settings.supabase_url:" not in src
    assert "rfp portal (BuildingConnected): enabled" in src
    # The secret is never an argument of a log call.
    assert "building_connected_client_secret" not in src


# ── conftest pins (the local .env enables BC with real credentials) ───────


def test_conftest_pins_are_in_the_environment():
    assert os.environ["RFP_BC_ENABLED"] == "false"
    assert os.environ["BUILDING_CONNECTED_ENABLED"] == "false"
    assert os.environ["BUILDING_CONNECTED_CLIENT_ID"] == ""
    assert os.environ["BUILDING_CONNECTED_CLIENT_SECRET"] == ""
    assert os.environ["RFP_BC_TEST_MODE"] == "false"
    assert os.environ["RFP_BC_REDIRECT_URL"] == ""


def test_conftest_pins_beat_the_dotenv(tmp_path):
    """A .env shaped like the local one (legacy switch on, real-looking
    credentials, test mode) is overridden by the OS pins."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BUILDING_CONNECTED_ENABLED=true\nRFP_BC_ENABLED=true\n"
        "BUILDING_CONNECTED_CLIENT_ID=real-id\nBUILDING_CONNECTED_CLIENT_SECRET=real-secret\n"
        "RFP_BC_TEST_MODE=true\nRFP_BC_REDIRECT_URL=http://localhost:5051/cb\n",
        encoding="utf-8",
    )
    s = Settings(_env_file=env_file, rfp_ingest_enabled=True)
    assert s.rfp_bc_enabled is False
    assert s.building_connected_client_id == "" and s.building_connected_client_secret == ""
    assert s.rfp_bc_test_mode is False and s.rfp_bc_redirect_url == ""
    assert s.bc_configured is False and s.rfp_bc_active is False


def test_conftest_pins_hold_for_the_real_settings():
    """get_settings() (which reads the repo .env from the bdr_be cwd) sees
    the slice off and unconfigured."""
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.rfp_bc_enabled is False and s.bc_configured is False
        assert s.rfp_bc_active is False and s.rfp_bc_test_mode is False
        assert s.rfp_portal_any_active is False
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("name", _BC_ENV)
def test_every_bc_env_name_is_pinned(name):
    assert name in os.environ
