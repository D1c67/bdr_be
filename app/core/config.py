"""Application configuration loaded from environment / .env."""

import logging
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_anon_key: str = ""
    supabase_jwt_secret: str = ""
    # Legacy shared-secret (HS256) verification. Current Supabase projects sign
    # asymmetrically (ES256/RS256, verified via JWKS); this fallback only applies
    # to tokens that explicitly declare alg=HS256. Once fully on JWKS, set this
    # False (or clear SUPABASE_JWT_SECRET) so the shared-secret path is dead.
    legacy_hs256_enabled: bool = True

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

    # Two-factor auth (TOTP, required for all users). The real enforcement lives
    # in get_current_user, which rejects any non-aal2 token. This flag is the
    # break-glass valve: set MFA_REQUIRED=false to disable enforcement instantly
    # (e.g. during rollout, or if TOTP is misconfigured in the Supabase dashboard)
    # without a code change. TOTP must be enabled in Supabase Auth for aal2 to be
    # reachable — shipping enforcement while it is disabled locks everyone out.
    mfa_required: bool = True

    # App
    environment: str = "development"
    cors_origins: str = "http://localhost:4500"
    # Public base URL of the frontend — used to build the invite redirect target.
    frontend_url: str = "http://localhost:4500"
    signed_url_ttl_seconds: int = 900
    # Max combined size of a single files-export ZIP (it's buffered in memory);
    # above this the export endpoint returns 413 and asks for a smaller subset.
    export_max_total_bytes: int = 500 * 1024 * 1024

    # Estimator hardening
    estimator_rate_limit_per_min: int = 60   # per-account request cap
    denied_access_alert_threshold: int = 5   # denials within the window → alert IT
    denied_access_alert_window_min: int = 10

    # ── Abuse / resource limits (security hardening) ──────────────────────────
    # Max size of a single uploaded file. upload_file enforces this while reading
    # (streamed, so an oversized body is rejected instead of fully buffered).
    upload_max_bytes: int = 100 * 1024 * 1024          # 100 MB
    # Global backstop: any request body larger than this is refused by middleware
    # before a handler can buffer it. Sits just above a max upload + multipart
    # overhead so nothing legitimate is blocked.
    max_request_body_bytes: int = 110 * 1024 * 1024    # 110 MB
    # Estimate/BOQ workbook guard, shared by boq_extraction / proposal_scope /
    # general_material: reject files above this before parsing, and cap the text
    # handed to the LLM (bounds both token spend and in-memory render size).
    boq_max_bytes: int = 50 * 1024 * 1024              # 50 MB
    boq_max_text_chars: int = 400_000

    # Per-account rate-limit budgets (fixed window; see app/core/ratelimit.py and
    # docs/ERROR_CODES.md). Every limit returns 429 with detail "rate_limited"
    # plus Retry-After and X-RateLimit-Scope headers so a legitimate user who
    # trips one gets an actionable, code-tagged message.
    rate_limit_enabled: bool = True           # master switch (disable during an incident)
    ai_rate_limit_per_min: int = 5            # Claude/OpenAI extraction + generation
    upload_rate_limit_per_min: int = 20       # file uploads
    export_rate_limit_per_min: int = 5        # in-memory ZIP export builds
    bulk_send_rate_limit_per_min: int = 3     # RFQ email fan-out
    outbound_email_rate_limit_per_hour: int = 60   # invites + package / proposal mail
    default_rate_limit_per_min: int = 240     # generous catch-all for all other routes

    # Inbound vendor-reply attachment ingestion (rfq_inbox): bound how much an
    # inbound message can pull into memory / storage / the paid PDF extractor.
    inbound_attachment_max_bytes: int = 25 * 1024 * 1024   # skip larger attachments
    inbound_attachment_max_count: int = 10                 # per inbound message
    inbound_pdf_extract_max: int = 3                       # paid OpenAI calls per message

    # ── PM mailbox email ingestion (services/email_ingest) ────────────────────
    # Poll a mailbox (Inbox + Sent Items) and assign every email to a project.
    # The app's Mail.ReadWrite permission is tenant-wide (no Exchange
    # ApplicationAccessPolicy is configured), so any mailbox works without
    # Exchange-side changes. Attachment caps reuse the inbound_* limits above.
    email_ingest_enabled: bool = False
    email_ingest_mailbox: str = ""              # e.g. t.moorejr@g3electrical.com; empty = poller never runs
    email_ingest_poll_interval_seconds: int = 120
    email_ingest_lookback_days: int = 1         # initial-sync window (deployment-forward, no backfill)
    email_ingest_reset_lookback_days: int = 7   # window after a DeltaExpired (410) reset
    email_body_max_chars: int = 100_000         # stored plain-text body cap
    # Identification round 3 (LLM subject-only match).
    openai_email_match_model: str = "gpt-5.4-mini"
    email_match_confidence_threshold: float = 0.85   # assign at/above; below → Unknown + suggestion
    email_match_max_attempts: int = 5                # transient-failure retries before 'failed'
    email_match_llm_max_candidates: int = 300        # prefilter cap bounding the R3 prompt
    email_llm_outage_retry_seconds: int = 3600       # wait when the provider is out of credits
    # New-project rescan of the Unknown pool (learn-back on project creation).
    email_rescan_llm_days: int = 14             # only LLM-confirm unknowns this recent
    email_rescan_llm_max: int = 50              # LLM call cap per project creation

    # Anonymous OneDrive "Anyone with the link" URLs for RFQ drawings expire after
    # this many days (Graph honors the tenant anonymous-link max-expiry policy).
    rfq_drawings_link_ttl_days: int = 30

    # Break-glass acknowledgement: production refuses to boot with mfa_required
    # False unless this is explicitly set True (see the validator below). Keeps
    # the break-glass valve usable while making "2FA off in prod" a deliberate,
    # loud decision rather than a silent config drift.
    mfa_break_glass_acknowledged: bool = False

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        """Fail fast (or loudly warn) on unsafe production configuration."""
        if self.environment == "production":
            if not self.supabase_service_role_key:
                raise ValueError(
                    "SUPABASE_SERVICE_ROLE_KEY must be set in production."
                )
            if not self.mfa_required and not self.mfa_break_glass_acknowledged:
                raise ValueError(
                    "Refusing to boot: MFA_REQUIRED=false in production without "
                    "MFA_BREAK_GLASS_ACKNOWLEDGED=true. 2FA must not be silently "
                    "disabled — set the acknowledgement var only for a deliberate "
                    "break-glass window."
                )
            if not self.mfa_required:
                logging.getLogger("bdr.security").critical(
                    "MFA enforcement is DISABLED in production (break-glass "
                    "acknowledged). Re-enable MFA_REQUIRED as soon as possible."
                )
        return self

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
