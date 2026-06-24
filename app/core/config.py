"""Application configuration loaded from environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_anon_key: str = ""
    supabase_jwt_secret: str = ""

    # Anthropic / Claude (BOQ → RFQ extraction)
    anthropic_api_key: str = ""
    claude_boq_model: str = "claude-opus-4-8"
    claude_boq_max_tokens: int = 16000
    # General-material extraction (wiring cost from the estimate's bid recap).
    claude_estimate_model: str = "claude-sonnet-4-6"
    claude_estimate_max_tokens: int = 2000

    # Microsoft Graph
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_sender: str = "bids@g3electrical.com"

    # OpenAI (RFQ email wording variation + quote PDF price extraction)
    openai_api_key: str = ""
    openai_email_model: str = "gpt-5.4-nano"
    openai_quote_model: str = "gpt-5.4-mini"

    # Proposal scope-line generation (Send Out, step 10)
    openai_proposal_model: str = "gpt-5.4-mini"
    openai_proposal_max_output_tokens: int = 8000
    openai_proposal_max_input_chars: int = 400_000
    # Test/dev override; empty = packaged asset app/assets/proposal_template.docx
    proposal_template_path: str = ""

    # RFQ sending / inbound reply polling
    rfq_drawings_inline_limit_mb: int = 20   # above this → OneDrive link instead of attaching
    rfq_poll_interval_seconds: int = 180
    rfq_poll_active_days: int = 7            # stop watching a conversation after this
    rfq_polling_enabled: bool = True         # disable on extra workers
    display_timezone: str = "America/New_York"  # for dates in RFQ subject/body

    # Due-date reminder notifications (in-app, via the bell)
    due_reminders_enabled: bool = True
    due_reminder_poll_interval_seconds: int = 300   # must stay well under 1h (smallest window)
    due_reminder_expired_horizon_days: int = 7      # "expired" fires only this close to the date

    # Branded email mirror of every in-app notification (bell ↔ inbox parity).
    # Best-effort and fire-and-forget; also requires Graph creds (ms_client_id)
    # to actually send. Tests force this off (see tests/conftest.py).
    notification_emails_enabled: bool = True

    # In-app file preview (office → PDF derivative)
    preview_engine: str = "gotenberg"        # gotenberg | graph | off
    gotenberg_url: str = "http://localhost:3500"
    preview_convert_timeout_seconds: int = 120
    preview_max_convert_mb: int = 50         # skip conversion above this → failed

    # App
    environment: str = "development"
    cors_origins: str = "http://localhost:4500"
    # Public base URL of the frontend — used to build the invite redirect target.
    frontend_url: str = "http://localhost:4500"
    signed_url_ttl_seconds: int = 900

    # Estimator hardening
    estimator_rate_limit_per_min: int = 60   # per-account request cap
    denied_access_alert_threshold: int = 5   # denials within the window → alert IT
    denied_access_alert_window_min: int = 10

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def gotenberg_base_url(self) -> str:
        # Render's `fromService hostport` yields a bare host:port — add a scheme.
        url = self.gotenberg_url.rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url

    @property
    def supabase_jwks_url(self) -> str:
        # Supabase exposes JWKS for asymmetric (RS256/ES256) verification.
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
