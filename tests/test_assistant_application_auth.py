"""Regression guard for the owner's application-only decision."""

from pathlib import Path

from app.assistant import repo
from app.core.config import Settings
from app.services import assistant_ms

ROOT = Path(__file__).resolve().parents[1]


def test_oauth_runtime_and_config_removed():
    assert "assistant_outlook_enabled" in Settings.model_fields
    assert not any(name.startswith("assistant_ms_") for name in Settings.model_fields)
    assert not any(name.startswith(("ms_connection", "ms_oauth", "ms_refresh", "ms_purge"))
                   for name in vars(repo))
    assert not any(hasattr(assistant_ms, name) for name in ("begin", "complete", "disconnect", "delegated_token"))
    for filename in ("assistant_ms_crypto.py", "assistant_ms_logging.py", "assistant_ms_lifecycle.py"):
        assert not (ROOT / "app/services" / filename).exists()


def test_forward_migration_retires_without_rewriting_history():
    assert (ROOT / "supabase/migrations/0182_assistant_phase5.sql").exists()
    sql = (ROOT / "supabase/migrations/0183_assistant_application_mail.sql").read_text()
    assert "drop trigger assistant_ms_profile_changed on public.profiles" in sql
    assert "from public,anon,authenticated,service_role" in sql
    assert "cache_ciphertext=null" in sql and "flow_ciphertext=null" in sql
    assert "drop table" not in sql.lower()
