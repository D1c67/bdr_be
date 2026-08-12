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

    # ── Sub-app feature flags (release gating) ────────────────────────────────
    # One deployment serves three sub-apps — Bidding, Project Management and
    # Certified Payroll. These switches decide which of them this deployment
    # actually serves, so the whole codebase can ship to production while a
    # not-yet-tested module stays dark until its var is flipped.
    #
    # THIS BACKEND IS THE SINGLE SOURCE OF TRUTH: the frontend reads the live
    # values from GET /features at sign-in rather than carrying its own build-
    # time copy, so turning a module on/off is one env change here (plus the
    # restart the platform does anyway) — no frontend rebuild, and the UI can
    # never advertise a module the API refuses to serve.
    #
    # Default TRUE so development, staging and the test suite are unaffected and
    # a forgotten var never silently kills a working module; production sets the
    # ones that aren't ready to false. Disabled means GONE, not hidden: every
    # route of that sub-app 404s (see app/core/features.py) and the frontend
    # drops its nav, its switcher tile and every cross-app link into it.
    #
    # What each flag does NOT cover: the shared spine all three hang off —
    # /users, /notifications, /projects (the row PM and CP also use), /vendors,
    # /gcs, /material-categories, /submittals (the global bank, reachable from
    # both Bidding and PM) — stays served whatever the flags say, because
    # switching one module off must never break the other two.
    bidding_enabled: bool = True
    pm_enabled: bool = True
    certified_payroll_enabled: bool = True

    # ── LLMs (routing lives in app/services/llm.py) ───────────────────────────
    # Master switch: when true EVERY AI feature routes to the self-hosted
    # OpenAI-compatible endpoint below and the SELF_HOSTED_* per-feature models.
    # STRICT — while true, no prompt is ever sent to a 3rd-party provider; a
    # self-hosted outage degrades each feature gracefully (same paths as a
    # missing API key), it never falls back.
    full_self_hosted_llms_enabled: bool = False

    # 3rd-party provider keys (live while the master switch is false)
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Self-hosted connection (OpenAI-compatible: vLLM / Ollama / llama.cpp /
    # TGI / LM Studio). `target` picks which base-URL/key pair is live, so you
    # can flip between a local server and the EC2 load balancer in one line.
    self_hosted_llm_target: str = "local"       # local | ec2
    self_hosted_llm_local_base_url: str = ""    # e.g. http://localhost:11434/v1
    self_hosted_llm_local_api_key: str = ""
    self_hosted_llm_ec2_base_url: str = ""      # e.g. https://llm.internal.example.com/v1
    self_hosted_llm_ec2_api_key: str = ""
    self_hosted_llm_timeout_seconds: int = 120
    # TLS hardening: verification stays on; for internal ALB certs point
    # ca_bundle at your private CA's PEM instead of disabling verification.
    # Plain http:// in production refuses to boot unless allow_http is set
    # (only for traffic that never leaves a private network).
    self_hosted_llm_verify_tls: bool = True
    self_hosted_llm_ca_bundle: str = ""
    self_hosted_llm_allow_http: bool = False

    # Per-feature models — 3rd party (Anthropic side)
    claude_boq_model: str = "claude-opus-4-8"   # BOQ → RFQ category extraction
    claude_boq_max_tokens: int = 16000
    # General-material extraction (wiring cost from the estimate's bid recap).
    claude_estimate_model: str = "claude-sonnet-4-6"
    claude_estimate_max_tokens: int = 2000
    # i18n catalog translation (scripts/translate_catalog.py)
    claude_translate_model: str = "claude-opus-4-8"

    # Per-feature models — self-hosted (live while the master switch is true;
    # empty = that feature is off in self-hosted mode)
    self_hosted_boq_model: str = ""
    self_hosted_estimate_model: str = ""
    self_hosted_translate_model: str = ""
    self_hosted_proposal_model: str = ""
    self_hosted_quote_pdf_model: str = ""
    self_hosted_email_match_model: str = ""
    self_hosted_email_vary_model: str = ""
    self_hosted_aliases_model: str = ""

    # ── LLM health monitoring (app/services/llm_health.py) ────────────────────
    # Feeds the sidebar's "Model status" indicator. A background poller keeps a
    # snapshot warm per worker; each tick is one free /models catalog call per
    # active provider — no tokens are spent. Turn the poller off and reads still
    # work, they just probe inline when the snapshot goes stale.
    llm_health_enabled: bool = True
    llm_health_poll_interval_seconds: int = 120
    # Deliberately short and independent of self_hosted_llm_timeout_seconds
    # (which is sized for a multi-minute BOQ run) — a status check must fail
    # fast rather than pin a worker against a stopped model server.
    llm_health_probe_timeout_seconds: int = 8
    # Manual "Check now" from the modal; the cached read is not limited.
    llm_health_rate_limit_per_min: int = 12

    # ── LLM job queue + concurrency gate (services/llm_queue, llm_gate) ───────
    # Durable queue for the long AI jobs (BOQ extraction, general material,
    # proposal lines): jobs survive restarts, transient failures retry on the
    # schedule below, and the dev AI monitor page reads the ledger. The gate
    # caps concurrent in-flight LLM calls per provider so a burst can never
    # drown the self-hosted box. All limits are per uvicorn worker process
    # (2 workers in prod, so the effective cap is 2x).
    llm_queue_enabled: bool = True
    llm_queue_poll_interval_seconds: int = 3   # worker tick; also the claim cadence
    llm_queue_worker_concurrency: int = 3      # jobs running at once per worker process
    # A running job whose lease expired is treated as interrupted (deploy /
    # crash) and requeued. Must comfortably exceed the slowest real run.
    llm_queue_lease_seconds: int = 900
    # Seconds between attempts for TRANSIENT failures (comma-separated).
    # Total attempts = 1 + number of delays. Permanent failures (scanned PDF,
    # missing config, out of API tokens) never retry.
    llm_queue_retry_delays: str = "10,20,45,90,180"
    llm_call_log_retention_days: int = 90      # llm_call_log + terminal llm_jobs pruning
    # Concurrency gate. Self-hosted default sits inside the vLLM box's
    # measured sweet spot (8-16 concurrent) accounting for 2 prod workers.
    llm_max_concurrent_self_hosted: int = 6
    llm_max_concurrent_third_party: int = 16
    llm_interactive_reserved_slots: int = 2    # held back for user-facing calls
    llm_interactive_wait_seconds: float = 10.0   # then LlmBusy -> "try again in a moment"
    llm_background_wait_seconds: float = 180.0   # queue/pipeline callers wait longer
    llm_monitor_rate_limit_per_min: int = 120  # dev AI monitor page polling

    # Microsoft Graph
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_sender: str = "bids@g3electrical.com"
    # OneDrive owner for oversize-attachment uploads/links (RFQ drawing sets
    # past rfq_drawings_inline_limit_mb, and the submittal equivalents).
    # ms_sender is a shared mailbox, which CANNOT have a OneDrive - drive calls
    # against it 404 - so this must name a licensed account whose OneDrive is
    # provisioned, and the app registration needs the Files.ReadWrite.All
    # application permission (admin-consented). Empty falls back to ms_sender.
    ms_drive_owner: str = ""
    # CC'd on every proposal email sent out to a GC, so the bids desk sees the
    # outgoing bid on the thread itself (and GCs reply-all back to it) rather
    # than only in the sending mailbox's Sent Items. Empty disables the CC.
    proposal_cc: str = "bids@g3electrical.com"
    # Same idea for every outgoing RFQ to a vendor. This is normally ms_sender
    # itself: the copy lands in the bids inbox on the vendor's conversation, and
    # the reply poller skips it (rfq_inbox._ingest_message drops anything from
    # ms_sender as our own outbound copy). Empty disables the CC.
    rfq_cc: str = "bids@g3electrical.com"

    # Per-feature models — 3rd party (OpenAI side)
    openai_email_model: str = "gpt-5.4-nano"    # RFQ email wording variation
    openai_quote_model: str = "gpt-5.4-mini"    # vendor quote PDF price extraction
    openai_alias_model: str = "gpt-5.4-nano"    # submittal-bank alternate names

    # Proposal scope-line generation (Send Out, step 10)
    openai_proposal_model: str = "gpt-5.4-mini"
    openai_proposal_max_output_tokens: int = 8000
    openai_proposal_max_input_chars: int = 400_000
    # Test/dev override; empty = packaged asset app/assets/proposal_template.docx
    proposal_template_path: str = ""

    # RFQ sending / inbound reply polling
    rfq_drawings_inline_limit_mb: int = 20   # above this → OneDrive link instead of attaching
    # Hard cap on one category's selected files together (matches the 300 MB
    # per-file upload cap). Past the inline limit files ride as a OneDrive
    # link, so this bounds the link path, not the email.
    rfq_attachments_total_limit_mb: int = 300
    rfq_poll_interval_seconds: int = 180
    rfq_poll_active_days: int = 7            # stop watching a conversation after this
    rfq_polling_enabled: bool = True         # disable on extra workers
    display_timezone: str = "America/New_York"  # for dates in RFQ subject/body

    # Due-date reminder notifications (in-app, via the bell)
    due_reminders_enabled: bool = True
    due_reminder_poll_interval_seconds: int = 300   # must stay well under 1h (smallest window)
    due_reminder_expired_horizon_days: int = 7      # "expired" fires only this close to the date
    # Grace period for just-created projects: the New Project modal creates the
    # row first and uploads staged files after, so without it the first tick
    # emails the team about a project whose creator is still mid-modal. Sized to
    # the worst realistic upload session: staged folder drops ride the 20/min
    # upload limiter, so a few hundred files is upwards of half an hour. Only
    # delays reminders (kinds with an expired notice); actual_bid is exempt so
    # its no-expired-fallback reminders can never be lost outright.
    due_reminder_min_project_age_seconds: int = 3600

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
    upload_max_bytes: int = 300 * 1024 * 1024          # 300 MB
    # Global backstop: any request body larger than this is refused by middleware
    # before a handler can buffer it. Sits just above a max upload + multipart
    # overhead so nothing legitimate is blocked.
    max_request_body_bytes: int = 310 * 1024 * 1024    # 310 MB
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
    rfq_nudge_rate_limit_per_min: int = 3     # RFQ nudge reminder fan-out
    outbound_email_rate_limit_per_hour: int = 60   # invites + package / proposal mail
    notification_log_rate_limit_per_min: int = 30  # per-project log assembly (query fan-out)
    report_rate_limit_per_min: int = 30       # bid-invitations report assembly
    default_rate_limit_per_min: int = 240     # generous catch-all for all other routes

    # Inbound vendor-reply attachment ingestion (rfq_inbox): bound how much an
    # inbound message can pull into memory / storage / the paid PDF extractor.
    inbound_attachment_max_bytes: int = 25 * 1024 * 1024   # skip larger attachments
    inbound_attachment_max_count: int = 10                 # per inbound message
    inbound_pdf_extract_max: int = 3                       # paid OpenAI calls per message
    inbound_link_max_count: int = 5                        # cloud-share links fetched per message

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

    # ── Project submittal requests (services/submittal_sending) ───────────────
    # The mailbox a project's submittal REQUESTS are sent FROM. Empty falls back
    # to EMAIL_INGEST_MAILBOX at send time, which is what makes vendor replies
    # thread back through the ingestion pipeline (both the Sent-Items copy and
    # the reply land in that one mailbox under a shared conversationId). Set this
    # only to deliberately send from a DIFFERENT mailbox than the one ingested —
    # doing so means replies are NOT tracked. Empty here + empty ingest mailbox =
    # submittal sending is refused (nowhere for replies to return).
    submittal_sender: str = ""

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
            # The gotenberg_url default (localhost:3500) is a LOCAL convenience:
            # it matches `docker run -p 3500:3000` from the README. In a real
            # deployment Gotenberg is a separate service, so an unset var makes
            # the API dial its own container and get refused. Keyed on
            # model_fields_set rather than on the value, so a deliberate
            # same-host sidecar can still be pointed at localhost explicitly.
            if (
                self.preview_engine == "gotenberg"
                and "gotenberg_url" not in self.model_fields_set
            ):
                raise ValueError(
                    "Refusing to boot: PREVIEW_ENGINE=gotenberg in production "
                    "but GOTENBERG_URL is unset, so office-to-PDF conversion "
                    "would dial the built-in localhost default and be refused. "
                    "That silently breaks every RFQ send (the send path refuses "
                    "to email an editable BOM) and every xlsx/docx preview. "
                    "Point it at the Gotenberg service, e.g. "
                    "http://bdr-gotenberg.railway.internal:3000 (container port "
                    "3000, not the local 3500 mapping), or set "
                    "PREVIEW_ENGINE=graph to convert via Microsoft Graph."
                )
        return self

    @model_validator(mode="after")
    def _validate_sub_apps(self) -> "Settings":
        """At least one sub-app must be served — all three off is a config typo,
        not a deployment. Every route would 404 and the frontend would have
        nowhere to land, which is far harder to diagnose after the fact than a
        refused boot."""
        if not (self.bidding_enabled or self.pm_enabled or self.certified_payroll_enabled):
            raise ValueError(
                "Refusing to boot: BIDDING_ENABLED, PM_ENABLED and "
                "CERTIFIED_PAYROLL_ENABLED are all false, so this deployment "
                "would serve no application at all. Enable at least one."
            )
        return self

    @model_validator(mode="after")
    def _validate_self_hosted_llms(self) -> "Settings":
        """Fail fast on a broken/unsafe self-hosted LLM configuration. Only
        enforced while the master switch is on, so 3rd-party-only deployments
        are unaffected."""
        if not self.full_self_hosted_llms_enabled:
            return self
        if self.self_hosted_llm_target not in ("local", "ec2"):
            raise ValueError(
                "SELF_HOSTED_LLM_TARGET must be 'local' or 'ec2' "
                f"(got {self.self_hosted_llm_target!r})."
            )
        url = self.self_hosted_llm_base_url
        if not url:
            raise ValueError(
                "Refusing to boot: FULL_SELF_HOSTED_LLMS_ENABLED=true but the "
                f"'{self.self_hosted_llm_target}' target has no base URL. Set "
                "SELF_HOSTED_LLM_LOCAL_BASE_URL or SELF_HOSTED_LLM_EC2_BASE_URL "
                "(e.g. http://localhost:11434/v1), or turn the switch off."
            )
        if not url.startswith(("https://", "http://")):
            raise ValueError(
                "The self-hosted LLM base URL must include a scheme "
                f"(https:// or http://); got {url!r}."
            )
        if self.self_hosted_llm_ca_bundle:
            import os

            if not os.path.isfile(self.self_hosted_llm_ca_bundle):
                raise ValueError(
                    "SELF_HOSTED_LLM_CA_BUNDLE points to a missing file: "
                    f"{self.self_hosted_llm_ca_bundle!r}."
                )
        if self.environment == "production":
            sec_log = logging.getLogger("bdr.security")
            if url.startswith("http://"):
                if not self.self_hosted_llm_allow_http:
                    raise ValueError(
                        "Refusing to boot: the self-hosted LLM URL is plain http:// "
                        "in production. Use https (a private CA is supported via "
                        "SELF_HOSTED_LLM_CA_BUNDLE), or set "
                        "SELF_HOSTED_LLM_ALLOW_HTTP=true only if the traffic never "
                        "leaves a private network."
                    )
                sec_log.critical(
                    "Self-hosted LLM traffic is UNENCRYPTED (http) in production "
                    "(SELF_HOSTED_LLM_ALLOW_HTTP acknowledged). Prompts contain "
                    "bid data — move to https as soon as possible."
                )
            elif not self.self_hosted_llm_verify_tls:
                sec_log.critical(
                    "TLS verification for the self-hosted LLM endpoint is DISABLED "
                    "in production. Use SELF_HOSTED_LLM_CA_BUNDLE for private CAs "
                    "and re-enable SELF_HOSTED_LLM_VERIFY_TLS."
                )
        return self

    @model_validator(mode="after")
    def _validate_llm_queue(self) -> "Settings":
        """A malformed retry schedule should refuse to boot, not silently turn
        every transient failure terminal at runtime."""
        try:
            delays = self.llm_retry_delay_list
        except ValueError as exc:
            raise ValueError(
                "LLM_QUEUE_RETRY_DELAYS must be comma-separated positive "
                "integers (seconds), e.g. '10,20,45,90,180'."
            ) from exc
        if any(d <= 0 for d in delays):
            raise ValueError("LLM_QUEUE_RETRY_DELAYS entries must be positive seconds.")
        return self

    @property
    def llm_retry_delay_list(self) -> list[int]:
        """Parsed LLM_QUEUE_RETRY_DELAYS. Empty list = no automatic retries."""
        raw = self.llm_queue_retry_delays.strip()
        if not raw:
            return []
        return [int(part.strip()) for part in raw.split(",") if part.strip()]

    @property
    def self_hosted_llm_base_url(self) -> str:
        """The live self-hosted endpoint (per SELF_HOSTED_LLM_TARGET). Includes
        the /v1 suffix, e.g. http://localhost:11434/v1."""
        url = (
            self.self_hosted_llm_ec2_base_url
            if self.self_hosted_llm_target == "ec2"
            else self.self_hosted_llm_local_base_url
        )
        return url.strip().rstrip("/")

    @property
    def self_hosted_llm_api_key(self) -> str:
        return (
            self.self_hosted_llm_ec2_api_key
            if self.self_hosted_llm_target == "ec2"
            else self.self_hosted_llm_local_api_key
        )

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
