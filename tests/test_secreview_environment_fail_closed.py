"""ENVIRONMENT guards fail closed (security review finding 23).

A missing, misspelled or differently cased ENVIRONMENT must never silently
drop the production posture (boot refusals, no public /docs, HSTS).
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_SAFE = dict(supabase_service_role_key="k", mfa_required=True, preview_engine="graph")


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


@pytest.mark.parametrize("value", ["production", "Production", " PRODUCTION ", "prod", "Prod"])
def test_production_spellings_normalize_and_refuse_testing_bench(value):
    assert _s(environment=value, **_SAFE).environment == "production"
    with pytest.raises(ValidationError, match="RFP_TESTING_ENABLED=true in production"):
        _s(environment=value, rfp_testing_enabled=True, **_SAFE)


@pytest.mark.parametrize("value", ["staging", "qa", "prd", "", "  "])
def test_unknown_or_blank_values_get_the_production_guards(value):
    assert _s(environment=value, **_SAFE).is_production is True
    with pytest.raises(ValidationError, match="MFA_REQUIRED=false in production"):
        _s(environment=value, supabase_service_role_key="k", preview_engine="graph",
           mfa_required=False)


@pytest.mark.parametrize("value", ["development", "Development", "dev", "local", "test", "TEST"])
def test_explicit_dev_labels_stay_relaxed(value):
    s = _s(environment=value, rfp_testing_enabled=False, mfa_required=False)
    assert s.is_production is False


def test_railway_without_explicit_environment_refuses_to_boot(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="ENVIRONMENT is not set on a Railway"):
        _s()
    # Setting it explicitly boots normally.
    assert _s(environment="production", **_SAFE).is_production is True


def test_off_railway_default_is_development(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    assert _s().environment == "development"
    assert _s().is_production is False


def test_health_does_not_reveal_environment():
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/health")  # no lifespan: no pollers start
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_docs_gate_uses_fail_closed_flag():
    import app.main as main

    assert main._is_prod is main.settings.is_production
