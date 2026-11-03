"""Application configuration loaded from environment / .env."""

import base64
import binascii
import json
import logging
import math
import re
import uuid
from typing import Literal
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ENVIRONMENT values that get the relaxed local posture (public /docs, no
# HSTS, no production boot refusals). Anything else, including a typo or an
# unknown label, is treated as production: the guards fail closed.
DEV_ENVIRONMENTS: frozenset[str] = frozenset({"development", "dev", "local", "test"})

# The only host the NGEM client may be pointed at (docs/RFP_NGEM_PORTAL.md 8).
NGEM_ENTRY_URL_PREFIX = "https://supplier.ionwave.net/"
_NGEM_SCHEDULE_MAX_ENTRIES = 12
# "Looks like one address": the RFP test bench's three addresses are checked
# against this at boot (docs/RFP_TESTING.md 2).
_EMAIL_ADDRESS_RE = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")


def _parse_schedule_times(raw: str) -> list[tuple[int, int]]:
    """RFP_NGEM_SCHEDULE_TIMES ("06:30,12:00") as (hour, minute) tuples: 1 to
    12 comma-separated `HH:MM` entries on the 24 h clock, no duplicates.
    Anything else raises a ValueError naming the offending entry."""
    entries = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not entries:
        raise ValueError("RFP_NGEM_SCHEDULE_TIMES must list at least one HH:MM time.")
    if len(entries) > _NGEM_SCHEDULE_MAX_ENTRIES:
        raise ValueError(
            f"RFP_NGEM_SCHEDULE_TIMES lists {len(entries)} times; at most "
            f"{_NGEM_SCHEDULE_MAX_ENTRIES} are allowed."
        )
    out: list[tuple[int, int]] = []
    for entry in entries:
        parts = entry.split(":")
        if len(parts) != 2 or not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):
            raise ValueError(
                f"RFP_NGEM_SCHEDULE_TIMES entry {entry!r} is not an HH:MM time (24 h clock)."
            )
        hour, minute = int(parts[0]), int(parts[1])
        if hour > 23 or minute > 59:
            raise ValueError(
                f"RFP_NGEM_SCHEDULE_TIMES entry {entry!r} is out of range (00:00 to 23:59)."
            )
        if (hour, minute) in out:
            raise ValueError(f"RFP_NGEM_SCHEDULE_TIMES lists {entry!r} twice.")
        out.append((hour, minute))
    return out


# The dev Supabase project ref: the only project the Phase 7 live-test email
# override may run against (docs/ASSISTANT_PHASE7_PLAN.md 12.1).
_PHASE7_DEV_SUPABASE_REF = "bpidntbyvoooqvaispup"


def _parse_hhmm(raw: str, env_name: str) -> tuple[int, int]:
    """One `HH:MM` time on the 24 h clock (RFP_BC_FULL_SYNC_TIME) as an
    (hour, minute) pair. Anything else, a list included, raises a ValueError
    naming `env_name`."""
    entry = (raw or "").strip()
    parts = entry.split(":")
    if len(parts) != 2 or not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):
        raise ValueError(f"{env_name} {entry!r} is not one HH:MM time (24 h clock).")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"{env_name} {entry!r} is out of range (00:00 to 23:59).")
    return hour, minute



def _b64_key(raw: str, env_name: str) -> bytes:
    """A base64 (standard or url-safe, padding optional) value that must
    decode to exactly 32 bytes: the browser agent AES and HMAC keys and the
    Ed25519 release public key. Anything else raises a ValueError naming
    `env_name` (never the value)."""
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"{env_name} must be set (base64 of 32 random bytes).")
    padded = text + "=" * (-len(text) % 4)
    try:
        data = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{env_name} is not valid base64.") from None
    if len(data) != 32:
        raise ValueError(f"{env_name} must decode to exactly 32 bytes (got {len(data)}).")
    return data

# Which self-hosted endpoint is live (SELF_HOSTED_LLM_TARGET). Each target is a
# base URL + API key + served-model triple: SELF_HOSTED_LLM_<TARGET>_{BASE_URL,
# API_KEY,MODEL}. Adding a box = a new entry here plus its three fields.
SELF_HOSTED_LLM_TARGETS: tuple[str, ...] = ("local", "ec2", "ec2b", "ec2c")

# reasoning.effort values GPT-6 Astra accepts (no "none"); AI Estimating only.
PLANS_READER_REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
# Seconds of a queue lease kept spare beyond one plans call's timeout and
# gate wait: the upload of up to 50 MB and the row writes after the call.
PLANS_READER_LEASE_HEADROOM_SECONDS = 120


class Settings(BaseSettings):
    # populate_by_name: fields that carry a validation_alias (rfp_ingest_enabled)
    # stay constructible by field name too (tests build Settings(...) directly).
    # hide_input_in_errors: a boot refusal must name the variable, never
    # print the whole input (every secret read from the environment) into
    # the deploy log.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )

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
    # One deployment serves three sub-apps - Bidding, Project Management and
    # Certified Payroll. These switches decide which of them this deployment
    # actually serves, so the whole codebase can ship to production while a
    # not-yet-tested module stays dark until its var is flipped.
    #
    # THIS BACKEND IS THE SINGLE SOURCE OF TRUTH: the frontend reads the live
    # values from GET /features at sign-in rather than carrying its own build-
    # time copy, so turning a module on/off is one env change here (plus the
    # restart the platform does anyway) - no frontend rebuild, and the UI can
    # never advertise a module the API refuses to serve.
    #
    # Default TRUE so development, staging and the test suite are unaffected and
    # a forgotten var never silently kills a working module; production sets the
    # ones that aren't ready to false. Disabled means GONE, not hidden: every
    # route of that sub-app 404s (see app/core/features.py) and the frontend
    # drops its nav, its switcher tile and every cross-app link into it.
    #
    # What each flag does NOT cover: the shared spine all three hang off -
    # /users, /notifications, /projects (the row PM and CP also use), /vendors,
    # /gcs, /material-categories, /submittals (the global bank, reachable from
    # both Bidding and PM) - stays served whatever the flags say, because
    # switching one module off must never break the other two.
    bidding_enabled: bool = True
    pm_enabled: bool = True
    certified_payroll_enabled: bool = True

    # ── BDR Assistant (docs/ASSISTANT.md) ──────────────────────────────────
    # Master switch for the per-user assistant: routes, worker claim pass,
    # bdra_ principal. Off = the assistant does not exist (GET /features
    # reports assistant=false, every /assistant route 404s).
    assistant_enabled: bool = False
    # The schedule loop (phase 3). Needs ASSISTANT_ENABLED.
    assistant_scheduler_enabled: bool = False
    # Routine knobs (docs/ASSISTANT_PHASE3_PLAN.md 3.4, 3.16). The loop polls
    # every assistant_scheduler_poll_seconds (15 to 600); an occurrence older
    # than the catch-up window is recorded as missed; one claim takes at most
    # assistant_schedule_claim_batch runs (1 to 50). A routine pauses after
    # max_failures failed runs in a row or max_unread unseen deliveries; a run
    # open longer than stale_run_minutes is failed by the reaper; Run now waits
    # run_now_min_seconds between clicks; routine email to self stops at
    # max_emails_per_day per Pacific day (0 to 200; 0 turns it off).
    assistant_scheduler_poll_seconds: int = 60
    assistant_schedule_catchup_hours: int = 3
    assistant_schedule_claim_batch: int = 20
    assistant_schedule_max_failures: int = 3
    assistant_schedule_max_unread: int = 10
    assistant_schedule_stale_run_minutes: int = 15
    assistant_run_now_min_seconds: int = 300
    assistant_schedule_max_emails_per_day: int = 20
    # The loop's maintenance riders (approval expiry, staging and task sweeps,
    # the nightly memory and tool-result step, the orphan sweep). Off, the
    # scheduler runs routines only.
    assistant_maintenance_enabled: bool = True
    # HMAC-SHA256 key for the short-lived bdra_ tokens the worker mints
    # (app/assistant/principal.py). At least 32 characters; Railway env only,
    # never in a table. Rotating it invalidates in-flight runs (5 min TTL).
    assistant_token_secret: str = ""
    # The assistant feature is openai_only. With FULL_SELF_HOSTED_LLMS_ENABLED
    # on, the assistant refuses to boot unless the owner sets this (owner
    # authorized 2026-10-21, decisions log).
    assistant_allow_openai_in_self_hosted: bool = False
    # Spend caps in USD per calendar day (Pacific). The per-user default is
    # overridable per person in assistant_profiles.daily_cost_cap_usd; the
    # global cap is assistant_settings.global_daily_cap_usd's seed value.
    assistant_daily_cap_usd: float = 5.0
    assistant_global_daily_cap_usd: float = 50.0
    # The planner model (OpenAI only; services/llm_pricing.PRICES must hold
    # a row for it, or every assistant call logs cost NULL) and its default
    # reasoning effort. assistant_settings.model overrides the model per
    # deployment from IT Admin Settings.
    assistant_model: str = "gpt-5.4"
    assistant_reasoning_effort: str = "low"
    # Assistant turns one worker process runs at once (the design's capacity 2).
    assistant_concurrency: int = 2
    # Phase 5 uses the existing MS_* application identity. BDR enforces
    # the current user's mailbox scope and approved sender on every call.
    assistant_outlook_enabled: bool = False
    assistant_calendar_enabled: bool = False
    assistant_web_enabled: bool = False
    assistant_web_api_url: str = ""
    assistant_web_api_key: str = ""
    # Personal browser access has a separate encryption boundary and pool.
    assistant_browser_enabled: bool = False
    user_cred_kek: str = ""
    user_cred_kek_id: str = "u1"
    user_cred_old_keys: str = ""
    # Phase 4 knobs (docs/ASSISTANT_PHASE4_PLAN.md 3.21). Page reads: pages
    # per card (1 to 12), the run-details context assumed per read, the hold
    # factor over the estimate (1 to 3) and the page-read task timeout. AE
    # runs started by the assistant: hold TTL in seconds (30 to 43,200, the
    # 0174 reserve guard and spend.MAX_TTL_SECONDS), file and page caps.
    # Reports: file lifetime in days (1 to 30) and rows per workbook.
    # Evidence: default and maximum window in days. Certified payroll: the
    # smallest employee count a per-project cell may show. Ranges are checked
    # at boot while the assistant is on.
    assistant_plans_pages_per_card: int = 6
    assistant_plans_context_tokens: int = 24000
    assistant_plans_hold_factor: float = 1.5
    assistant_plans_page_read_timeout_minutes: int = 120
    assistant_ae_run_hold_ttl_seconds: int = 43200
    assistant_ae_run_max_files: int = 15
    assistant_ae_run_max_pages: int = 400
    assistant_report_ttl_days: int = 7
    assistant_report_max_rows: int = 20000
    assistant_evidence_default_days: int = 90
    assistant_evidence_max_days: int = 400
    assistant_cp_small_cell_min: int = 3
    # Phase 7 proactive router (docs/ASSISTANT_PHASE7_PLAN.md 12.1). Off by
    # default. The loop needs ASSISTANT_ENABLED; the DB company switch
    # (assistant_settings.proactive_enabled) is a second, reversible gate.
    # Poll seconds 10 to 300, claim batch 1 to 200. Digest slots are two
    # HH:MM Pacific times; each slot plus its catch-up window (1 to 5 hours)
    # must stay clear of quiet hours (quiet start and end, HH:MM Pacific,
    # router only). A tail cursor older than cursor_max_lag_minutes (5 to
    # 1440) is reseeded. External sends (Teams for high and urgent items,
    # digest email) need their own switch on top of the global
    # NOTIFICATION_EMAILS_ENABLED and TEAMS_ALERTS_ENABLED; the send step
    # claims send_poll_batch ledger rows per tick (1 to 50) under a lease of
    # send_lease_seconds (60 to 900). Retention days (7 to 3650): inbox rows,
    # processed events, digests and slot ledger, send ledger. The allowlist
    # (profile ids or emails, comma separated) limits both the router and the
    # Inbox to those people during a pilot. test_email_to is the live-test
    # recipient override: set only on the tick script's command line, never
    # in .env or on a server; dev project only, one address, allowlist
    # required.
    assistant_proactive_enabled: bool = False
    assistant_proactive_poll_seconds: int = 30
    assistant_proactive_claim_batch: int = 50
    assistant_proactive_digest_times: str = "07:00,13:00"
    assistant_proactive_digest_catchup_hours: int = 3
    assistant_proactive_quiet_start: str = "18:00"
    assistant_proactive_quiet_end: str = "06:00"
    assistant_proactive_cursor_max_lag_minutes: int = 60
    assistant_proactive_external_enabled: bool = False
    assistant_proactive_send_poll_batch: int = 20
    assistant_proactive_send_lease_seconds: int = 600
    assistant_proactive_inbox_retention_days: int = 90
    assistant_proactive_event_retention_days: int = 90
    assistant_proactive_digest_retention_days: int = 365
    assistant_proactive_send_retention_days: int = 90
    assistant_proactive_user_allowlist: str = ""
    assistant_proactive_test_email_to: str = ""

    # Phase 8 notification triage (docs/ASSISTANT_PHASE8_PLAN.md 13). A facts
    # only model pass over each person's recordable Inbox rows, paid by the
    # company spend (no person's cap). The model must have an OpenAI price row.
    # The user allowlist (profile ids or emails; empty means everyone who has
    # the assistant) is a rollout lever only. test_spend_cap_usd is test only:
    # set it on the tick script's and the eval runner's command lines, never
    # in .env or on a server; dev project only, allowlist required.
    assistant_triage_enabled: bool = False
    assistant_triage_loop_enabled: bool = True
    assistant_triage_user_allowlist: str = ""
    assistant_triage_model: str = "gpt-6-luna"
    assistant_triage_poll_seconds: int = 60
    assistant_triage_debounce_seconds: int = 300
    assistant_triage_batch_max: int = 10
    assistant_triage_batches_per_tick: int = 5
    assistant_triage_lease_seconds: int = 420
    assistant_triage_timeout_seconds: int = 45
    assistant_triage_max_output_tokens: int = 3200
    # Sent as the Responses reasoning effort of every triage call (P8L-3).
    # Empty (the default) sends none, so the model's own default applies: the
    # 16.4 golden set passed there, while "low" gave 2 invalid answers in 44 at
    # batch size 10 on two runs (2026-11-03), a gate miss. Reasoning tokens
    # count against ASSISTANT_TRIAGE_MAX_OUTPUT_TOKENS either way.
    assistant_triage_reasoning_effort: str = ""
    assistant_triage_window_hours: int = 24
    assistant_triage_fuse_ratio: float = 0.5
    assistant_triage_fuse_per_hour: int = 30
    assistant_triage_cap_pause_seconds: int = 900
    assistant_triage_retention_days: int = 90
    assistant_triage_test_spend_cap_usd: float | None = None

    # ── LLMs (routing lives in app/services/llm.py) ───────────────────────────
    # Master switch: when true EVERY AI feature routes to the self-hosted
    # OpenAI-compatible endpoint below and the SELF_HOSTED_* per-feature models.
    # STRICT - while true, no prompt is ever sent to a 3rd-party provider; a
    # self-hosted outage degrades each feature gracefully (same paths as a
    # missing API key), it never falls back.
    full_self_hosted_llms_enabled: bool = False

    # 3rd-party provider keys (live while the master switch is false)
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Self-hosted connection (OpenAI-compatible: vLLM / Ollama / llama.cpp /
    # TGI / LM Studio). `target` picks which base-URL/key/model triple is
    # live, so flipping between a local server and either EC2 box is ONE line:
    #   local - a server on this machine (or an SSH tunnel to a box)
    #   ec2   - box A: g6.xlarge, serves qwen-3.5-4b at llm.g3electrical.com
    #   ec2b  - box B: g7e.2xlarge, serves qwen-3.8-27b at llm2.g3electrical.com
    #   ec2c  - box C: g7e.2xlarge in us-east-1 (N. Virginia), standby for when
    #           us-west-2 has no g7e capacity
    # IT Admins and devs can override the target live from Settings (stored in
    # llm_runtime_settings, see services/llm_target); this env value is the
    # default whenever no override is set.
    # Each target also names the model it serves (SELF_HOSTED_LLM_<TARGET>_MODEL);
    # every feature whose SELF_HOSTED_<FEATURE>_MODEL is empty uses that, so the
    # per-feature lines only need touching to override or switch a feature off.
    self_hosted_llm_target: str = "local"       # local | ec2 | ec2b | ec2c
    self_hosted_llm_local_base_url: str = ""    # e.g. http://localhost:11434/v1
    self_hosted_llm_local_api_key: str = ""
    self_hosted_llm_local_model: str = ""
    self_hosted_llm_ec2_base_url: str = ""      # e.g. https://llm.internal.example.com/v1
    self_hosted_llm_ec2_api_key: str = ""
    self_hosted_llm_ec2_model: str = ""
    self_hosted_llm_ec2b_base_url: str = ""
    self_hosted_llm_ec2b_api_key: str = ""
    self_hosted_llm_ec2b_model: str = ""
    self_hosted_llm_ec2c_base_url: str = ""
    self_hosted_llm_ec2c_api_key: str = ""
    self_hosted_llm_ec2c_model: str = ""
    self_hosted_llm_timeout_seconds: int = 120
    # TLS hardening: verification stays on; for internal ALB certs point
    # ca_bundle at your private CA's PEM instead of disabling verification.
    # Plain http:// in production refuses to boot unless allow_http is set
    # (only for traffic that never leaves a private network).
    self_hosted_llm_verify_tls: bool = True
    self_hosted_llm_ca_bundle: str = ""
    self_hosted_llm_allow_http: bool = False

    # Per-feature models - 3rd party (Anthropic side)
    claude_boq_model: str = "claude-opus-4-8"   # BOQ → RFQ category extraction
    claude_boq_max_tokens: int = 16000
    # General-material extraction (wiring cost from the estimate's bid recap).
    claude_estimate_model: str = "claude-sonnet-4-6"
    claude_estimate_max_tokens: int = 2000
    # i18n catalog translation (scripts/translate_catalog.py)
    claude_translate_model: str = "claude-opus-4-8"

    # Per-feature models - self-hosted (live while the master switch is true).
    # Resolution per feature (services/llm.py): the value here if set; else the
    # live target's SELF_HOSTED_LLM_<TARGET>_MODEL; the literal "off" switches
    # the feature off even when the target has a model. Empty everywhere =
    # off (the pre-target-model behaviour).
    self_hosted_boq_model: str = ""
    self_hosted_estimate_model: str = ""
    self_hosted_translate_model: str = ""
    self_hosted_proposal_model: str = ""
    self_hosted_quote_pdf_model: str = ""
    self_hosted_email_match_model: str = ""
    self_hosted_email_vary_model: str = ""
    self_hosted_aliases_model: str = ""
    self_hosted_bid_split_model: str = ""
    self_hosted_rfp_classify_model: str = ""      # RFP email yes/no classification
    # RFP field extraction and project matching (services/rfp_match). Empty =
    # the classify model, so no new env is required to run them.
    self_hosted_rfp_extract_model: str = ""
    self_hosted_rfp_match_model: str = ""
    # PM email classification (services/email_classification). self_hosted_only:
    # empty = the live box's own model; a value pins the model on the env
    # default box; "off" disables the feature. Never a 3rd-party model.
    self_hosted_email_classify_model: str = ""

    # ── Bid File Splitter (experimental; routers/bid_splitter.py) ─────────────
    # Standalone AI tool that classifies every page of an uploaded bid-set PDF
    # (rendered to an image, sent to a vision model) and splits the PDF into one
    # output per contiguous category run. NOT wired into the bidding pipeline.
    # Default OFF everywhere; the dev environment turns it on. While off every
    # /bid-splitter route 404s and the frontend hides the page (same contract as
    # the sub-app flags; the flag rides along in GET /features).
    bid_file_splitter_enabled: bool = False
    # Which provider pool serves the 'bid_split' feature: "anthropic",
    # "openai", or "self_hosted" (the connection below + the vision-capable
    # SELF_HOSTED_BID_SPLIT_MODEL). Unlike the other features the vendor is
    # env-selectable, not fixed in llm.py _FEATURES: the whole point of the
    # tool right now is comparing models. FULL_SELF_HOSTED_LLMS_ENABLED=true
    # still overrides this to self-hosted (STRICT, like every feature); the
    # reverse is allowed though - "self_hosted" here routes ONLY bid_split to
    # the box while the rest of the app stays on its 3rd-party vendors.
    bid_split_llm_provider: str = "anthropic"
    claude_bid_split_model: str = "claude-opus-5"
    openai_bid_split_model: str = "gpt-5.4-mini"
    bid_split_max_tokens: int = 8000
    # Pipeline tuning knobs (all per-file): how many page images ride in one
    # model call, the rendered JPEG's long side in pixels / quality, and the
    # guardrails on job size. Bigger pages_per_call = fewer calls but more
    # tokens at risk per retry.
    bid_split_pages_per_call: int = 8
    bid_split_render_long_side: int = 1568
    bid_split_render_jpeg_quality: int = 70
    bid_split_max_pages_per_file: int = 600
    # Raised 100 -> 250 with the RFP split step (docs/RFP_SPLIT.md 1): a
    # harvest of up to rfp_harvest_max_files documents is one job.
    bid_split_max_files_per_job: int = 250
    # File triage (stage 1): one vision call over a sample of pages decides
    # what the FILE is before any per-page work. Specifications, RFP and
    # addendum files are identified and left intact (never split, never
    # re-written) - only drawing sets and mixed packages go on to the
    # per-page trade classification. triage_pages is the sample size; a
    # verdict below triage_min_confidence escalates to the per-page pass
    # instead of being trusted. island_max_pages caps the "island repair"
    # absorption: a run of pages that long or shorter, sandwiched between two
    # runs of one same category, is assumed misclassified (e.g. figures
    # inside a spec book reading as drawings) and absorbed.
    bid_split_triage_pages: int = 12
    bid_split_triage_min_confidence: float = 0.7
    bid_split_island_max_pages: int = 10

    # ── AI Estimating (plans reader; docs/PLANS_READER.md) ───────────────────
    # GPT-6 Astra reads a bid set (drawings as PNG images, specs as PDF files)
    # in token-budgeted groups, consolidates it into a brief plus BOQ,
    # estimate and markup specs, and answers chat questions about it. Default
    # OFF everywhere; while off every /ai-estimating route 404s and the
    # frontend hides the page (`ai_estimating` in GET /features). OpenAI
    # ONLY: the plans_* features are pinned to OpenAI (services/llm
    # openai_only), so FULL_SELF_HOSTED_LLMS_ENABLED=true leaves them running
    # on the OpenAI key instead of moving them to the box.
    plans_reader_enabled: bool = False
    # Who may open AI Estimating while PLANS_READER_ENABLED is on
    # (core/features.ai_estimating_roles): live = every internal role (the
    # external estimator never), test = Executives and IT Admins only, dev =
    # nobody (the routes 403 and the page is hidden for every account).
    # Default dev so a deployment that forgets the variable shows it to no one.
    ai_estimating_state: Literal["live", "test", "dev"] = "dev"
    openai_plans_model: str = "gpt-6-astra"
    # reasoning.effort for every plans call: low | medium | high | xhigh | max.
    plans_reader_reasoning_effort: str = "medium"
    # One sub agent group's input budget (ledger and prompt included). Kept
    # under the 272,000-token line above which GPT-6 Astra bills the whole
    # request at 2x input and 1.5x output.
    plans_reader_group_input_tokens: int = 240_000
    # The ledger (sheet index, legends, general notes, spec TOC) rides with
    # every group; above this the legend images drop to detail high.
    plans_reader_ledger_max_tokens: int = 40_000
    # A run whose estimated input is above this refuses to start (about $100
    # of input at $10 per 1M). There is no override; raise this setting.
    plans_reader_max_input_tokens_per_run: int = 10_000_000
    # Pro Max chat: most pages one message may render and send.
    plans_reader_promax_max_pages_per_message: int = 12
    # Second-pass (higher resolution) escalation budget as a fraction of the
    # first pass's input tokens. 0 disables escalation.
    plans_reader_escalation_max_fraction: float = 0.5
    # Escalation tiles render at this ppi.
    plans_reader_tile_ppi: int = 260
    # detail "original" images are rejected by OpenAI above 30,000 32px
    # patches; the native ppi of each sheet is the largest under this.
    plans_reader_native_max_patches: int = 30_000
    # OpenAI caps input_file PDFs at 50 MB per file and per request.
    plans_reader_file_max_bytes: int = 50 * 1024 * 1024
    # Client timeout for one plans Responses call (a 240k-token group can
    # take minutes). The SDK does not retry these; the queue does, so one
    # attempt plus the background gate wait must fit in LLM_QUEUE_LEASE_SECONDS.
    # complete_parts renews the job's lease right before the request, so page
    # rendering and payload building BEFORE the call do not count against it;
    # the validator keeps PLANS_READER_LEASE_HEADROOM_SECONDS spare for the
    # upload and the row writes after the call.
    plans_reader_call_timeout_seconds: int = 600
    # AI Estimating jobs one worker process runs at once, in their own claim
    # pass (services/llm_queue._claim_plans), never against
    # LLM_QUEUE_WORKER_CONCURRENCY. Prod runs two uvicorn workers, so twice
    # this across the deployment.
    plans_reader_concurrency: int = 2
    # Pacing against the OpenAI account's tokens-per-minute limit (1,000,000
    # for this key, shared by every run): before advance() dispatches group
    # jobs it spreads their start times over 60 s windows so the estimated
    # input tokens of the jobs scheduled in any one window stay under this
    # (a 429 still retries on the queue's schedule). 0 turns pacing off.
    plans_reader_tokens_per_minute: int = 800_000

    # ── LLM health monitoring (app/services/llm_health.py) ────────────────────
    # Feeds the sidebar's "Model status" indicator. A background poller keeps a
    # snapshot warm per worker; each tick is one free /models catalog call per
    # active provider - no tokens are spent. Turn the poller off and reads still
    # work, they just probe inline when the snapshot goes stale.
    llm_health_enabled: bool = True
    llm_health_poll_interval_seconds: int = 120
    # Deliberately short and independent of self_hosted_llm_timeout_seconds
    # (which is sized for a multi-minute BOQ run) - a status check must fail
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
    # Anthropic / OpenAI client bounds. Without these the SDKs default to a
    # 600s read timeout and 2 retries, so one stalled call could outlive the
    # RFP sweep lease. Worst case per call = timeout x (max_retries + 1).
    third_party_llm_timeout_seconds: int = 120
    third_party_llm_max_retries: int = 1
    llm_monitor_rate_limit_per_min: int = 120  # dev AI monitor page polling

    # ── RFP Ingestion sandbox (experimental; routers/rfp_ingest.py) ──────────
    # First slice of RFP Ingestion: inbound RFP PDFs (uploads or email
    # attachments) are treated as attacker-controlled bytes and processed in an
    # out-of-process, credential-free sandbox (app/sandbox/) that renders every
    # page to JPEG tiers and extracts sanitized text under hard rlimits, a
    # wall-clock kill and a disk quota. The parent re-validates every output
    # byte-for-byte before anything is uploaded. Nothing here creates projects,
    # sends email or touches the bidding pipeline. Design record:
    # docs/RFP_INGESTION_SANDBOX.md; the child <-> parent contract is
    # app/sandbox/protocol.py. Default OFF everywhere; while off every
    # /rfp-ingest route 404s, the frontend hides the page, the queue never
    # claims a sandbox job and the retention prune never runs (the flag rides
    # along in GET /features as `rfp_ingest`, same contract as the splitter).
    # Master switch for BOTH RFP Ingestion slices (the sandbox below and the
    # email intake, docs/RFP_EMAIL_INGESTION.md). Env name RFP_INGESTION_ENABLED;
    # the older RFP_INGEST_ENABLED spelling is still accepted.
    rfp_ingest_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("RFP_INGESTION_ENABLED", "RFP_INGEST_ENABLED"),
    )
    # Office files (docs/RFP_INGESTION_SANDBOX.md, section 2.1). With this on,
    # a .docx / .xlsx / .doc / .xls (recognised by magic AND extension, never
    # opened by the API) is accepted on every intake path and converted to a
    # PDF through the same Gotenberg service the previews use
    # (gotenberg_url); the derived PDF is then verified by the sandbox child
    # exactly like an upload. Off = such files are rejected/not_pdf, as
    # before, with no deploy. The converter call is bounded by the timeout
    # below (its read timeout, since Gotenberg sends nothing until LibreOffice
    # is done) and the returned PDF by rfp_ingest_max_file_bytes; it must stay
    # well under half the queue lease, as the lease is not renewed while the
    # worker thread waits on the converter.
    rfp_ingest_office_files_enabled: bool = True
    rfp_ingest_office_convert_timeout_seconds: int = 180
    # Per-file byte cap: enforced at upload (413) and as the Graph stream cap
    # when an email attachment is materialized. Must not exceed
    # upload_max_bytes (the request layer would refuse first anyway).
    # Raised 50% with upload_max_bytes (docs/RFP_SPLIT.md section 1).
    rfp_ingest_max_file_bytes: int = 450 * 1024 * 1024        # 450 MB
    # Files per run: enforced at upload (413) and at email run creation.
    # 250 since the RFP split step (docs/RFP_SPLIT.md 1), matching the
    # harvest cap and the splitter's per-job cap.
    rfp_ingest_max_files_per_run: int = 250
    # A file with more pages is rejected/too_many_pages by the child; a run
    # whose files together pass the run budget rejects the overflow files as
    # run_page_budget (the child is killed on its `document` event).
    rfp_ingest_max_pages_per_file: int = 3000
    rfp_ingest_max_pages_per_run: int = 20000
    # A page whose longer side exceeds this fails individually (page_size);
    # 14400 pt is 200 inches, the PDF spec's own ceiling.
    rfp_ingest_max_page_side_pt: int = 14400
    # Rendering tiers. thumb is the classification tier (the splitter's 1568
    # px); full is the reading tier: pages whose long side is at or below
    # full_small_threshold_pt (letter/tabloid) render at full_small_long_side
    # (about 200 dpi), larger sheets at full_long_side. Keeps a 2000-page spec
    # book near 1.4 GB instead of 5 GB. thumb <= full_small <= full.
    rfp_ingest_thumb_long_side: int = 1568
    rfp_ingest_thumb_jpeg_quality: int = 70
    rfp_ingest_full_long_side: int = 4000
    rfp_ingest_full_small_long_side: int = 2200
    rfp_ingest_full_small_threshold_pt: int = 1300
    rfp_ingest_full_jpeg_quality: int = 85
    # Text extraction caps: per page (the child truncates) and per file. The
    # per-file cap is spent twice over, once in the runner as it validates
    # pages (later pages come back with no text at all, so the parent never
    # holds more than this many bytes of page text) and once when text.json is
    # written. Either one flags the file truncated, bounds_hit: text_bytes.
    rfp_ingest_max_text_chars_per_page: int = 200_000
    rfp_ingest_max_text_bytes_per_file: int = 64 * 1024 * 1024     # 64 MB
    # The thumb-tier images PDF is split into parts no larger than this, which
    # must fit under the derived bucket's 200 MB object limit.
    rfp_ingest_images_pdf_part_bytes: int = 150 * 1024 * 1024      # 150 MB
    # Parent-side timeouts. open: from the child's `ready` event to its
    # `document`/`reject` event (heartbeats do not extend it). page stall: no
    # complete progress event for this long after the first page_start. The
    # per-file wall clock is clamp(base + per_page_ms x pages, 600, max). The
    # stall must stay well under half the queue lease so a stalled child is
    # killed and the job requeued long before the lease sweep can intervene.
    rfp_ingest_open_timeout_seconds: int = 300
    rfp_ingest_page_stall_seconds: int = 120
    rfp_ingest_file_timeout_base_seconds: int = 300
    rfp_ingest_file_timeout_per_page_ms: int = 1500
    rfp_ingest_file_timeout_max_seconds: int = 14400
    # Child rlimits per spawn (soft == hard, applied before pypdfium2 imports).
    # memory is RLIMIT_AS (address space, which runs above RSS), cpu is
    # RLIMIT_CPU, output_file is RLIMIT_FSIZE (the largest single artifact,
    # NOT the disk quota), disk is the out-dir quota the parent mirrors with
    # lstat sums. RLIMIT_AS does not apply on macOS and is recorded as not
    # applied; the rest apply everywhere.
    # 3072 MB covers the measured peaks with headroom for the virtual/RSS gap:
    # 1.18 GB RSS for a 3-sheet 30x42 in set at the 4000 px tier, 650 to 700 MB
    # for 36 to 40 sheet sets, 340 MB for a 475-page spec book. Sizing matters
    # because a page that trips the limit is a `memory` page failure, and on a
    # small file the failed-page allowance (2 pages) is spent immediately and
    # the whole file is rejected. The floor is 512 MB.
    rfp_ingest_sandbox_memory_mb: int = 3072
    rfp_ingest_sandbox_cpu_seconds: int = 1800
    rfp_ingest_sandbox_output_file_mb: int = 64
    rfp_ingest_sandbox_disk_mb: int = 3072
    # Free scratch disk that must remain before a file starts; a file whose
    # estimate would dip below it is skipped as failed/storage.
    rfp_ingest_scratch_reserve_mb: int = 2048
    # Crash handling: a crashed child is respawned with the blamed page skipped,
    # up to max(this floor, ceil(pages x max_failed_page_ratio)) times. A file
    # stays usable while its failed pages are at most
    # max(min_failed_pages_allowed, ceil(ratio x page_count)); beyond that it is
    # rejected/too_many_failed_pages. A file with no rendered page at all is
    # rejected the same way even when its failure count is inside the floor,
    # since there is nothing left for a reviewer to look at.
    rfp_ingest_max_child_restarts: int = 5
    rfp_ingest_max_failed_page_ratio: float = 0.05
    rfp_ingest_min_failed_pages_allowed: int = 2
    # Sandbox children per uvicorn worker process (2 workers in prod, so the
    # effective count is 2x). Claimed in the queue's second pass with its own
    # capacity: a sandbox run never occupies an LLM slot.
    rfp_ingest_sandbox_concurrency: int = 1
    # uid the child runs as when the parent is root (Railway). 0 disables the
    # switch even as root (an explicit, loudly logged opt-out). When the parent
    # is not root (local dev) no switch happens and it is recorded as not
    # applied. Root parents take a per-slot uid from pool_base .. base+size-1
    # so concurrent children cannot read each other's output directories; the
    # pool must hold at least 2 x concurrency.
    rfp_ingest_sandbox_uid: int = 65534
    rfp_ingest_sandbox_uid_pool_base: int = 60100
    rfp_ingest_sandbox_uid_pool_size: int = 4
    # Scratch root for quarantine copies and child output. Empty = the system
    # temp dir; on Railway point it at a volume mount once one exists.
    rfp_ingest_scratch_dir: str = ""
    # llm_jobs priority for sandbox runs (lower runs first). 200 keeps them
    # behind every user-facing LLM job (100).
    rfp_ingest_queue_priority: int = 200
    # Derived outputs and the quarantine copy of terminal runs older than this
    # are deleted by the queue's hourly prune; rows and manifests are kept and
    # the run is marked expired.
    rfp_ingest_retention_days: int = 14
    # Read routes of the dev-only /rfp-ingest API (run/file/page listings and
    # signed URLs); the page polls every 3 s while a run is active.
    rfp_ingest_rate_limit_per_min: int = 120

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

    # Per-feature models - 3rd party (OpenAI side)
    openai_email_model: str = "gpt-5.4-nano"    # RFQ email wording variation
    openai_quote_model: str = "gpt-5.4-mini"    # vendor quote PDF price extraction
    openai_alias_model: str = "gpt-5.4-nano"    # submittal-bank alternate names
    openai_rfp_classify_model: str = "gpt-5.4-mini"   # RFP email yes/no classification
    # RFP field extraction and project matching; empty = the classify model
    # (the fallback lives in llm._FEATURES, so the setting stays a plain string).
    openai_rfp_extract_model: str = ""
    openai_rfp_match_model: str = ""

    # Proposal scope-line generation (Send Out, step 10)
    openai_proposal_model: str = "gpt-5.4-mini"
    openai_proposal_max_output_tokens: int = 8000
    openai_proposal_max_input_chars: int = 400_000
    # Test/dev override; empty = packaged asset app/assets/proposal_template.docx
    proposal_template_path: str = ""

    # RFQ sending / inbound reply polling
    rfq_drawings_inline_limit_mb: int = 20   # above this → OneDrive link instead of attaching
    # Hard cap on one category's selected files together (matches the 450 MB
    # per-file upload cap). Past the inline limit files ride as a OneDrive
    # link, so this bounds the link path, not the email.
    rfq_attachments_total_limit_mb: int = 450
    rfq_poll_interval_seconds: int = 180
    rfq_poll_active_days: int = 7            # stop watching a conversation after this
    rfq_polling_enabled: bool = True         # disable on extra workers
    # Company (Pacific) time for dates in RFQ subject/body, proposal dates, and
    # due reminders. Must stay in lockstep with COMPANY_TZ in the frontend's
    # lib/format.ts and NUDGE_DUE_TIMEZONE in QuotesPanel.tsx.
    display_timezone: str = "America/Los_Angeles"

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

    # Daily "bids due today" digest email (services/due_digest): weekday
    # mornings, America/Los_Angeles, to every active internal user except
    # accountants. Extra workers are safe (the due_digest_log unique index
    # dedups), and Graph creds (ms_client_id) are required to actually send.
    due_digest_enabled: bool = True
    due_digest_poll_interval_seconds: int = 60      # must stay well under the catch-up window
    due_digest_send_hour: int = 6                   # 6:03 AM Pacific
    due_digest_send_minute: int = 3
    due_digest_catchup_hours: int = 4               # down at send time → send late until this, then skip the day

    # Calling In poller (services/calling_in, docs/CALLING_IN.md section 5):
    # claims a call_in_entries row when a project lands on a call list and
    # notifies Executive + Estimating Engineer Labor once (24-hour burst
    # guard), then closes the row when the project leaves. Multi-worker safe
    # (the partial unique index on call_in_entries is the claim). Also needs
    # BIDDING_ENABLED and Supabase. Tests pin it off (tests/conftest.py).
    call_in_enabled: bool = True
    call_in_poll_interval_seconds: int = 60

    # Auto send poller (services/auto_send_scheduler, docs/AUTO_SEND.md section
    # 7): the 3:00 PM preflight email, the 5:05 PM proposal send to every GC on
    # projects whose internal bid date is today, the missed-send notices and
    # the results digest. This flag is the env kill switch; the business on/off
    # is the auto_send_settings DB row (IT Admin, default OFF). Multi-worker
    # safe (the auto_send_runs and auto_send_email_log unique indexes are the
    # claims). Also needs BIDDING_ENABLED, Supabase and Graph creds. Tests pin
    # it off (tests/conftest.py). Times are Pacific HH:MM, validated at boot.
    auto_send_poller_enabled: bool = True
    auto_send_poll_interval_seconds: int = 30       # must stay well under the catch-up window
    auto_send_preflight_time: str = "15:00"         # "what will auto send" email
    auto_send_send_time: str = "17:05"              # the GC send
    auto_send_digest_time: str = "17:10"            # results email (waits up to 30 min for sends)
    auto_send_catchup_minutes: int = 120            # down at send time: send late until this, same day only
    auto_send_missed_lookback_days: int = 7         # past bid dates checked for a missed send
    # Outside production GC emails are refused unless this is set; when set,
    # every auto send GC email goes ONLY to this one address (no CC). Leave it
    # empty in production.
    auto_send_gc_redirect: str = ""

    # Branded email mirror of every in-app notification (bell ↔ inbox parity).
    # Best-effort and fire-and-forget; also requires Graph creds (ms_client_id)
    # to actually send. Tests force this off (see tests/conftest.py).
    notification_emails_enabled: bool = True

    # Teams alerts (docs/TEAMS_ALERTS.md): every notification mirrored to
    # email also goes to the recipient in Teams from the "BDR Alerts" bot.
    # Off until the Azure Bot and the Teams app are set up. The bot signs in
    # with its own app registration when these are set, else with ms_client_id
    # / ms_client_secret (an Azure Bot can reuse that registration). Tests
    # force the flag off (see tests/conftest.py).
    teams_alerts_enabled: bool = False
    teams_bot_app_id: str = ""
    teams_bot_app_secret: str = ""
    # The Teams app manifest id (scripts/build_teams_alerts_app.py writes it
    # into manifest.json). The sender finds the uploaded app in the org
    # catalog by this id to install it for each user.
    teams_app_manifest_id: str = "6c1f3e2a-9b7d-4f0e-8a51-2d4b7c9e1f30"
    # Bot Connector base for proactive messages (the global Teams endpoint).
    teams_bot_service_url: str = "https://smba.trafficmanager.net/teams/"

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
    # reachable - shipping enforcement while it is disabled locks everyone out.
    mfa_required: bool = True

    # App. Normalized (stripped, lowercased, "prod" -> "production"); every
    # value outside DEV_ENVIRONMENTS gets the production guards (is_production).
    environment: str = "development"
    cors_origins: str = "http://localhost:4500"
    # Public base URL of the frontend - used to build the invite redirect target.
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
    # 300 MB until 0132; raised 50% (docs/RFP_SPLIT.md section 1) because whole
    # bid sets now arrive through the splitter. The storage buckets (0132) and
    # the Supabase Dashboard global limit (a manual release step) must match, or
    # an upload the API accepts is rejected by storage.
    upload_max_bytes: int = 450 * 1024 * 1024          # 450 MB
    # The external estimator's per-file cap on POST /projects/{id}/files. Their
    # deliverables (estimate, boq, markup, marked plans) are far smaller than a
    # bid set, and the estimator is the one untrusted role, so it never gets to
    # buffer a full upload_max_bytes body in a shared worker. Internal caps are
    # unchanged.
    estimator_upload_max_bytes: int = 200 * 1024 * 1024  # 200 MB
    # In-flight upload limiter (app/core/ratelimit.py large_upload_slot): an
    # upload at or above `large_upload_bytes` holds one of
    # `large_upload_max_concurrent` process-wide slots and the caller's single
    # per-account slot while it is buffered and pushed to storage. The per-minute
    # upload budget bounds request STARTS; this bounds how many near-cap bodies
    # a worker holds in RAM at once (the concurrent-upload OOM vector).
    large_upload_bytes: int = 20 * 1024 * 1024         # 20 MB
    large_upload_max_concurrent: int = 3               # per process, all accounts
    large_upload_max_concurrent_per_user: int = 1
    # Global backstop: any request body larger than this is refused by middleware
    # before a handler can buffer it. Sits just above a max upload + multipart
    # overhead so nothing legitimate is blocked.
    max_request_body_bytes: int = 460 * 1024 * 1024    # 460 MB
    # Cap for every request that is NOT a multipart/form-data call to a known
    # upload route (one whose handler declares Form/File params; the middleware
    # reads those from app.routes). FastAPI buffers the whole body before auth
    # runs, so the allowance above would let an anonymous caller pin hundreds
    # of MB per request on a plain JSON route, and the Content-Type header is
    # client-controlled so it alone cannot pick the cap. 16 MB is far above the largest legitimate
    # JSON body (boq_max_text_chars / openai_proposal_max_input_chars are
    # 400k chars). The middleware counts the bytes actually received, so a
    # chunked or under-declared body is refused at the cap too.
    max_json_body_bytes: int = 16 * 1024 * 1024        # 16 MB
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
    gc_pricing_request_rate_limit_per_hour: int = 20  # late-GC price-change requests (Executive fan-out)
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
    # Identification round 2 fuzzy tier (the RFP matcher's name score).
    email_match_fuzzy_auto_threshold: float = 0.9    # auto-assign at/above (no conflict, clear gap)
    email_match_fuzzy_runner_up_gap: float = 0.1     # best must beat the runner-up by more than this
    email_match_fuzzy_suggest_floor: float = 0.6     # R2 near-miss offered in triage when R3 has none
    # Identification round 3 (LLM match on subject + body). Runs on the
    # self-hosted box chosen in Settings > AI model server (llm self_hosted_only).
    openai_email_match_model: str = "gpt-5.4-mini"
    email_match_confidence_threshold: float = 0.85   # assign at/above; below → Unknown + suggestion
    email_match_max_attempts: int = 5                # transient-failure retries before 'failed'
    email_match_llm_max_candidates: int = 300        # prefilter cap bounding the R3 prompt
    email_llm_outage_retry_seconds: int = 3600       # wait when the provider is out of credits
    email_match_body_max_chars: int = 6000           # body excerpt sent to the R3 model
    # New-project rescan of the Unknown pool (learn-back on project creation).
    email_rescan_llm_days: int = 14             # only LLM-confirm unknowns this recent
    email_rescan_llm_max: int = 50              # LLM call cap per project creation

    # ── PM email classification (services/email_classification) ──────────────
    # Labels every email filed to a PM project (materials/submittals, RFIs,
    # financials, change management, other) on the self-hosted box ONLY
    # (llm feature email_classify, self_hosted_only). Off = nothing enqueues;
    # existing labels stay readable. Docs: docs/EMAIL_CLASSIFICATION.md.
    email_classify_enabled: bool = True
    email_classify_concurrency: int = 1          # classify jobs per worker process (own claim pass)
    email_classify_queue_priority: int = 180     # behind the bidding jobs (100) and harvests
    email_classify_body_max_chars: int = 8000    # new content of the current message
    email_classify_quoted_max_chars: int = 3000  # quoted earlier thread text in the current body
    email_classify_context_max_chars: int = 12000  # previous thread messages, newest first
    email_classify_previous_max_chars: int = 2500  # cap per previous message
    email_classify_max_tokens: int = 1500        # answer budget (labels + two summaries)

    # ── RFP Ingestion: email intake (services/rfp_email_ingest) ──────────────
    # Watches ONLY the mailboxes listed here (Inbox folder), authenticates and
    # classifies every external message, checks the sender against the
    # authorized-sender rules and labels the invitation method. Runs only when
    # rfp_ingest_enabled (RFP_INGESTION_ENABLED) is on AND this list is
    # non-empty. Design record: docs/RFP_EMAIL_INGESTION.md.
    rfp_email_ingestion_inboxes_allowed: str = ""       # comma-separated mailboxes
    rfp_email_ingestion_poll_interval_seconds: int = 120
    rfp_email_ingestion_lookback_days: int = 3          # first-sync window (also the prod first start)
    rfp_email_ingestion_reset_lookback_days: int = 7    # window after a DeltaExpired (410) reset
    rfp_email_ingestion_internal_domains: str = "g3electrical.com"  # comma-separated; senders here are ignored
    # Extra domains sanitized out at listing time, pinned by the environment.
    # The MANAGED block list lives in the `rfp_blocked_senders` table (0133),
    # where the Executive, Estimating Admin, IT Admin and dev accounts add and
    # remove senders from Settings -> RFP Ingestion; 0133 seeds the four
    # platform domains that used to be this value's default
    # (buildingconnected.com, ionwave.net, planhub.com, planhubprojects.com)
    # as locked rows there. The default here is EMPTY on purpose: anything
    # listed in the environment is unioned with the table and CANNOT be
    # unblocked from the page, so a non-empty default would make an unblock
    # look like it worked while the poller kept dropping the mail.
    # Comma-separated domains; subdomains are covered on a label boundary
    # (planhub.com covers message.planhub.com).
    rfp_email_ingestion_blocked_domains: str = ""
    # Mailboxes every internal role may see into (docs/RFP_EMAIL_VISIBILITY.md
    # 1). Since 0134 an rfp_emails row belongs to the mailboxes that received
    # it and is visible only to the people those mailboxes are mapped onto;
    # a shared team mailbox has no single owner, so it is named here instead
    # and everyone internal sees what lands in it. The external estimator
    # never reaches this surface at all. Comma-separated addresses.
    rfp_email_ingestion_shared_mailboxes: str = "bids@g3electrical.com"
    # PM mailbox scoping (docs/ASSISTANT.md, "PM mailbox scoping fix"). The
    # mailboxes every internal role may see through PM Emails on top of their
    # own (profiles.email plus profiles.rfp_mailboxes), and the mailboxes
    # nobody sees unless it is their own, Executive and IT Admin included.
    # Comma-separated addresses; the owner edits both in Railway.
    shared_mailboxes: str = (
        "bids@g3electrical.com,office@g3electrical.com,adminassistant@g3electrical.com"
    )
    mailbox_exclusions: str = ""

    @property
    def shared_mailbox_set(self) -> set[str]:
        """SHARED_MAILBOXES, lowercased, blanks dropped."""
        return {m.strip().lower() for m in self.shared_mailboxes.split(",") if m.strip()}

    @property
    def mailbox_exclusion_set(self) -> set[str]:
        """MAILBOX_EXCLUSIONS, lowercased, blanks dropped."""
        return {m.strip().lower() for m in self.mailbox_exclusions.split(",") if m.strip()}

    # LLM classify step. The threshold is an env knob so a larger model can be
    # tuned without a code change.
    rfp_email_ingestion_confidence_threshold: float = 0.85
    rfp_email_ingestion_classify_max_body_chars: int = 12_000
    rfp_email_ingestion_classify_retry_seconds: int = 300   # wait while the model is down (no attempt spent)
    # Real-failure cap before 'failed'. With the 1 / 5 / 15 min backoff a step
    # reaches its verdict in about 21 minutes (IT Admin is alerted then).
    rfp_email_ingestion_classify_max_attempts: int = 4
    # Budget on classify calls for senders no rule or GC domain authorizes
    # yet (the model runs before the authorize step, docs/RFP_EMAIL_INGESTION.md
    # 3.5): at most this many calls per budget key (the registrable From
    # domain with its subdomains, or the exact address at a public mail
    # provider) per rolling day (counted by classified_at, migration 0147),
    # and this many per sweep tick. A row over budget waits at
    # `classify` (next_attempt_at pushed, no attempt spent) and is never
    # dropped; an authorized sender is never budgeted. 0 disables that cap.
    rfp_email_ingestion_classify_budget_per_sender_per_day: int = 20
    rfp_email_ingestion_classify_budget_per_tick: int = 25
    # The model (or our own AI gate) has been unavailable this long while RFP
    # rows wait on it: IT Admin gets one alert per outage.
    rfp_model_away_alert_minutes: int = 60
    # The /rfp-processing page (docs/RFP_PROCESSING.md 3 and 6): a pending
    # row with no progress (updated_at) for this long, and no retry scheduled
    # in the future, is shown as "stalled". The slow value covers `harvest`
    # and `split`, whose jobs legitimately run for a long time. Display only:
    # nothing in the pipeline reads either value.
    rfp_processing_stall_minutes: int = 30
    rfp_processing_slow_stall_minutes: int = 360
    # Sweep lease. Renewed before every LLM call; validated below to cover one
    # call (background wait + self-hosted timeout) so the second production
    # worker can never take the lease while the holder is inside a call.
    rfp_email_ingestion_lease_seconds: int = 600

    # ── RFP Ingestion: field extract and project matching (services/rfp_match)
    # Every weight, threshold, tolerance and window is an env value. The full
    # resolved set plus rfp_match.SCORER_VERSION is stored with every decision
    # (rfp_emails.match_weights), so tuning never makes an old decision
    # unexplainable. Design record: docs/RFP_MATCHING.md, section 6.
    # The system may merge and mark duplicates without a person. Off until the
    # Matches tab shows reviewers agreeing with the system; while off every
    # confident match waits for one human click, labeled as confident.
    rfp_match_auto_merge_enabled: bool = False
    # Name and date form the weighted average over the PRESENT signals (a
    # missing due date drops its weight); notes are a bonus of up to
    # weight_bid_notes * Dice, never in the denominator.
    rfp_match_weight_name: float = 0.6
    rfp_match_weight_bid_date: float = 0.3
    rfp_match_weight_bid_notes: float = 0.1
    rfp_match_notes_min: float = 0.5                 # notes Dice below this adds nothing
    # Date ladder against the closest candidate date (Pacific calendar days):
    # 1.0 inside the tolerance, date_score_far inside far_days, 0 beyond.
    rfp_match_bid_date_tolerance_days: int = 3
    rfp_match_bid_date_far_days: int = 14
    rfp_match_date_score_far: float = 0.5
    rfp_match_exact_time_bonus: float = 0.1          # same instant to the minute, email gave a time
    rfp_match_conflict_cap: float = 0.4              # name cap on a discriminator conflict (Phase 1 vs 2)
    rfp_match_candidate_window_days: int = 30        # bid date OR created_at not older than this
    rfp_match_auto_threshold: float = 0.85           # floor on the TOTAL for a confident candidate
    rfp_match_review_threshold: float = 0.55         # floor for a candidate to reach review / the model
    rfp_match_name_min_auto: float = 0.8             # name floor for a confident candidate
    rfp_match_name_min_auto_no_date: float = 0.9     # the same floor when the email has no due date
    # Best minus runner-up must exceed this for an automatic project match or
    # GC resolution; closer than this goes to review (the system never picks
    # between equals).
    rfp_match_runner_up_gap: float = 0.1
    rfp_match_llm_confidence_threshold: float = 0.8  # confidence floor on a 'same' verdict
    rfp_match_max_candidates: int = 5                # kept per email and sent to the model
    rfp_match_gc_auto_threshold: float = 0.85        # fuzzy GC-name floor
    rfp_match_rebid_lookback_days: int = 365         # wider name-only rebid lookup
    rfp_match_rebid_name_threshold: float = 0.85     # name-only floor for possible_rebid_project_id
    rfp_match_precreate_threshold: float = 0.5       # New Bid similar-projects check
    rfp_match_precreate_window_days: int = 60        # New Bid check, on internal_bid_at
    rfp_match_sibling_window_minutes: int = 10       # per-recipient sibling window; 0 disables

    # ── RFP Ingestion: project data harvest (services/rfp_harvest) ───────────
    # After the match step finds no existing project, a `harvest` step pulls
    # the project facts and the bidding documents from the invitation
    # platform (Procore first) and parks them: facts on rfp_harvests, files in
    # an Ingestion Sandbox run. Off by default; the master switch
    # RFP_INGESTION_ENABLED still gates everything. Design record:
    # docs/RFP_HARVEST.md, section 8.
    rfp_harvest_enabled: bool = False
    rfp_harvest_concurrency: int = 1               # harvest jobs per worker (third claim pass)
    rfp_harvest_queue_priority: int = 150          # behind user LLM jobs (100), ahead of the sandbox (200)
    rfp_harvest_poll_seconds: int = 60             # a `harvest` row waits this long between sweep checks
    rfp_harvest_reuse_days: int = 14               # a complete harvest younger than this is reused
    rfp_harvest_max_files: int = 250               # manifest rows; <= rfp_ingest_max_files_per_run (250 since docs/RFP_SPLIT.md)
    rfp_harvest_max_total_bytes: int = 2 * 1024 * 1024 * 1024   # 2 GB of declared manifest bytes
    # Procore account the harvester logs in as (a personal account today; the
    # env is the only place to change it). Both empty = no Procore harvester.
    procore_login_email: str = ""
    procore_login_password: str = ""
    procore_min_request_interval_seconds: float = 2.0   # pace floor; each gap is this x U(1, 2)
    procore_login_min_interval_seconds: int = 600       # never two login attempts closer than this
    procore_login_max_failures: int = 3                 # consecutive failures before the lock
    procore_login_lock_seconds: int = 21600             # lock length (6 h)
    procore_request_timeout_seconds: float = 30.0
    # PipelineSuite (PreconSuite) portals: the GC's own plan room at
    # <gc>.pipelinesuite.com. No credentials here: the invitation email
    # carries the portal host, the Project ID and the company-wide Security
    # Key, and the harvester logs in with those (one stored session per
    # portal). Design record: docs/RFP_PIPELINESUITE.md, section 6.
    pipelinesuite_enabled: bool = True
    pipelinesuite_tracking_pings_enabled: bool = True   # fire the email's open pixel + View Files click once per harvest
    pipelinesuite_min_request_interval_seconds: float = 2.0
    pipelinesuite_login_min_interval_seconds: int = 600  # per portal
    pipelinesuite_login_max_failures: int = 3            # per portal
    pipelinesuite_login_lock_seconds: int = 21600        # per portal (6 h)
    pipelinesuite_request_timeout_seconds: float = 30.0
    # SmartBid (ConstructConnect): many GCs invite through it from the
    # platform's own address. No credentials here: each invitation email
    # carries a per-recipient, per-project passport key in its "View the
    # Project" link, and the harvester logs in with that for one harvest
    # (nothing secret is stored). Design record: docs/RFP_SMARTBID.md, section 6.
    smartbid_enabled: bool = True
    smartbid_tracking_pings_enabled: bool = True   # fire the read receipt, open pixel + View the Project click once per harvest
    smartbid_min_request_interval_seconds: float = 2.0
    smartbid_login_min_interval_seconds: int = 30  # between token requests
    smartbid_login_max_failures: int = 3           # unexpected login answers before the lock
    smartbid_login_lock_seconds: int = 21600       # 6 h
    smartbid_request_timeout_seconds: float = 60.0
    # Email harvester (docs/RFP_HARVEST.md 2.5): organic, general and
    # nonorganic invitations carry their files as attachments and as
    # cloud-share links (SharePoint/OneDrive/Dropbox/Box/Google Drive), so
    # the "platform" is the email itself. No credentials anywhere: shares
    # are fetched anonymously, exactly like vendor cloud links.
    rfp_harvest_email_enabled: bool = True
    rfp_harvest_link_max_count: int = 10                 # share links resolved per email; the rest are recorded skipped_cap
    rfp_harvest_folder_max_depth: int = 4                # subfolder depth walked inside one shared folder
    rfp_harvest_image_signature_max_bytes: int = 100 * 1024   # an image at or under this is signature-like (collapsed on the card)
    google_drive_api_key: str = ""                       # lists Drive folders through the API; empty = keyless embedded view
    cloud_request_timeout_seconds: float = 30.0          # per request to a share host

    # ── RFP Ingestion: NGEM portal invitations (services/rfp_portal_ingest) ──
    # NGEM (supplier.ionwave.net) sends no usable invitation mail, so the
    # application logs into the company's supplier account twice a day, reads
    # the "My Invitations" grid, matches every open invitation against the
    # projects and harvests the new ones into the sandbox. Off by default;
    # RFP_INGESTION_ENABLED still gates everything, and with the credentials
    # or the entry URL empty the feature is inert (`ngem_configured` false).
    # Design record: docs/RFP_NGEM_PORTAL.md, section 8.
    rfp_ngem_enabled: bool = False
    ngem_login_username: str = ""
    ngem_login_password: str = ""
    ngem_entry_url: str = ""                        # the ResponseList.aspx?e=... link
    rfp_ngem_schedule_times: str = "06:30,12:00"    # Pacific HH:MM list, 1 to 12 entries
    rfp_ngem_catchup_hours: int = 4                 # a missed slot still runs inside this window
    rfp_ngem_poll_seconds: int = 60                 # scheduler and sweep tick
    rfp_ngem_auto_resolve_enabled: bool = True      # confident matches resolve to exists without a click
    rfp_ngem_max_list_pages: int = 20               # grid pages per scan
    rfp_ngem_scan_timeout_seconds: int = 1800       # a running run with no job past this is failed
    rfp_ngem_sweep_batch: int = 50                  # invitations per tick
    rfp_ngem_queue_priority: int = 150              # the harvest job type
    rfp_ngem_scan_queue_priority: int = 140          # the scan job: ahead of the harvest backlog
    ngem_min_request_interval_seconds: float = 2.0  # pace floor; each gap is this x U(1, 2)
    ngem_login_min_interval_seconds: int = 600      # never two login attempts closer than this
    ngem_login_max_failures: int = 3                # consecutive failures before the lock
    ngem_login_lock_seconds: int = 21600            # lock length (6 h)
    ngem_request_timeout_seconds: float = 30.0

    # ── RFP Ingestion: BuildingConnected Bid Board (services/rfp_bc_portal, bc_client) ──
    # BuildingConnected invitations are read from the Autodesk (APS) API over
    # 3-legged OAuth: one user with bid board view-all connects once from
    # Settings and the refresh token keeps the connection alive. A 15 minute
    # incremental scan plus a nightly full sync (Pacific) upsert the board
    # into rfp_portal_invitations. Off by default; RFP_INGESTION_ENABLED
    # still gates everything, and with the client id or secret empty the
    # feature is inert (`bc_configured` false). The env name is
    # RFP_BC_ENABLED; the older BUILDING_CONNECTED_ENABLED spelling is still
    # accepted (the first listed wins when both are set). Design record:
    # docs/RFP_BUILDINGCONNECTED.md, section 9.
    rfp_bc_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("RFP_BC_ENABLED", "BUILDING_CONNECTED_ENABLED"),
    )
    building_connected_client_id: str = ""
    building_connected_client_secret: str = ""       # never logged or returned
    rfp_bc_redirect_url: str = ""                    # OAuth redirect_uri, byte for byte as registered with APS
    rfp_bc_poll_minutes: int = 15                    # incremental slot grid; 5..60 and divides 60
    rfp_bc_full_sync_time: str = "02:30"             # one Pacific HH:MM
    rfp_bc_full_sync_catchup_hours: int = 4          # a missed full sync still runs inside this window
    rfp_bc_overlap_minutes: int = 30                 # incremental updatedAt filter reaches back this far
    rfp_bc_expire_days: int = 7                      # pipeline rows this far past due park as expired
    rfp_bc_missing_days: int = 3                     # rows absent from a full sync this long park as withdrawn
    rfp_bc_text_max_chars: int = 20000               # project information / trade instructions cap
    rfp_bc_request_timeout_seconds: float = 60.0
    rfp_bc_test_mode: bool = False                   # x-bc-mode: test (sample data); refused in production
    rfp_bc_scan_queue_priority: int = 140            # the incremental scan job
    rfp_bc_full_sync_queue_priority: int = 150       # the full sync job: behind the incremental scans
    rfp_bc_auto_resolve_enabled: bool = True         # confident matches resolve to exists without a click
    rfp_bc_max_pages: int = 200                      # API pages per scan (100 rows each)
    rfp_bc_expected_company_id: str = ""             # when set, the OAuth callback refuses any other /users/me companyId
    rfp_bc_callback_rate_limit_per_min: int = 30     # unauthenticated OAuth callback, per process, all callers together

    # ── RFP Ingestion: project creation (services/rfp_create) ────────────────
    # After the harvest, a `create` step turns what the pipeline collected
    # into a bidding project parked in Go/No-Go, with the intake fields
    # nobody could fill left for the Estimating Admin. The "Create project"
    # button on the email detail and the NGEM modal works whatever the
    # switch says; AUTOMATIC creation at the end of the pipeline is off by
    # default (a `create` row drains to `done`, where the button acts). The
    # master switch RFP_INGESTION_ENABLED still gates everything. Design
    # record: docs/RFP_CREATE.md, section 10.
    rfp_create_auto_enabled: bool = False
    rfp_create_poll_seconds: int = 30              # re-check interval while another worker holds a create claim
    rfp_create_claim_seconds: int = 600            # a create claim older than this is stale
    rfp_create_notes_max_chars: int = 4000         # bid_notes cap (extracted notes + harvest instructions + description)
    rfp_create_files_queue_priority: int = 160     # the document promotion job: behind the harvests (150)
    # The create-time duplicate guard (docs/RFP_CREATE.md 4.6): right before
    # the insert, a project created this recently with the same normalized
    # name for the same resolved GC is joined instead of duplicated. 0
    # disables the project half of the guard (the sibling half always runs).
    rfp_create_duplicate_window_minutes: int = 60

    # ── RFP Ingestion: Bid File Splitter step (services/rfp_split) ──────────
    # Between the harvest and project creation, a `split` step stages every
    # verified harvested document into one Bid File Splitter job so the
    # project is born with its documents categorized (drawing sets cut by
    # trade, specs / RFP / addenda identified). Runs only while BOTH
    # BID_FILE_SPLITTER_ENABLED and this flag are on; otherwise a row entering
    # `split` falls straight through to `create` (today's behavior). Design
    # record: docs/RFP_SPLIT.md.
    rfp_split_enabled: bool = False
    rfp_split_poll_seconds: int = 30               # a `split` row re-checks its job this often
    # Staging (docs/RFP_SPLIT.md 10.5): how many harvested documents are
    # fetched, checked and uploaded into the split job at once (the email
    # and portal sweeps stage inline; 105 files one at a time held the whole
    # intake for minutes). Each in-flight file holds its bytes in memory
    # (up to UPLOAD_MAX_BYTES, plus a converted PDF for an office file), so
    # the peak is about this many times the largest file.
    rfp_split_stage_concurrency: int = 4
    # A staging claim (`rfp_harvests.split_status = pending`) is stamped
    # (`split_started_at`) every RFP_SPLIT_STAGING_HEARTBEAT_SECONDS while
    # the staging loop is alive, and read as dead (the worker or the server
    # went away mid-staging) once the stamp is older than
    # RFP_SPLIT_STAGING_STALE_SECONDS: the pipeline restages, the project's
    # flag offers "Run the splitter" again. The same window decides when a
    # `processing` split job with nothing queued and nothing running is dead.
    rfp_split_staging_heartbeat_seconds: int = 30
    rfp_split_staging_stale_seconds: int = 300

    # ── RFP Ingestion: test mode and monitor (services/rfp_test) ─────────────
    # A dev-only bench (docs/RFP_TESTING.md): while a test session is active
    # the RFP intake and the project email filer watch ONE dedicated test
    # mailbox for forwards from ONE accepted sender, every other poller of
    # theirs is paused, and every send from this server is redirected as
    # plain text to one address. Off by default; while off every
    # /rfp-testing route 404s, the pollers never consult the session table
    # and the redirect is inert. Never on in production (the guard below).
    rfp_testing_enabled: bool = False
    rfp_testing_mailbox: str = ""              # RFP_TESTING_MAILBOX (Inbox only; dedicated)
    rfp_testing_sender: str = ""               # RFP_TESTING_SENDER (the only From accepted)
    rfp_testing_redirect_to: str = ""          # RFP_TESTING_REDIRECT_TO (every send goes here)
    rfp_testing_poll_seconds: int = 15         # RFP_TESTING_POLL_SECONDS (min 5): both pollers while active
    rfp_testing_unwrap_scan_chars: int = 4000  # a forwarded-header block further down is quoted history

    # ── Project submittal requests (services/submittal_sending) ───────────────
    # The mailbox a project's submittal REQUESTS are sent FROM. Empty falls back
    # to EMAIL_INGEST_MAILBOX at send time, which is what makes vendor replies
    # thread back through the ingestion pipeline (both the Sent-Items copy and
    # the reply land in that one mailbox under a shared conversationId). Set this
    # only to deliberately send from a DIFFERENT mailbox than the one ingested -
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

    # ── PM RFI sync from the GC's Procore (services/pm_rfi_sync) ─────────────
    # Read-only import of every RFI on a linked GC Procore project into the
    # PM RFI log, through the shared Procore web session the RFP harvest uses
    # (PROCORE_LOGIN_EMAIL / PROCORE_LOGIN_PASSWORD, same lock and pace).
    # Off by default; also needs PM_ENABLED. Design record: docs/PM_RFI_SYNC.md.
    pm_rfi_sync_enabled: bool = False
    pm_rfi_sync_daily_time: str = "06:30"            # one Pacific HH:MM (DST aware)
    pm_rfi_sync_catchup_hours: int = 4               # a missed daily slot still runs inside this window
    pm_rfi_sync_poll_seconds: int = 30               # worker tick: slots, claims, runs
    pm_rfi_sync_lease_seconds: int = 900             # a run's lease; renewed while it works
    pm_rfi_sync_max_attempts: int = 3                # transient failures before a run is failed
    pm_rfi_sync_retry_seconds: int = 600             # wait before a transient failure is retried
    pm_rfi_sync_attachment_max_bytes: int = 150 * 1024 * 1024   # per file; larger files are skipped and recorded
    pm_rfi_sync_max_details_per_run: int = 1000      # RFI detail fetches per run (the rest wait for the next run)
    pm_rfi_email_scan_enabled: bool = True           # Procore RFI links in filed PM emails trigger a targeted fetch
    pm_rfi_email_scan_interval_seconds: int = 300
    pm_rfi_email_scan_lookback_hours: int = 72       # first scan window (no checkpoint yet)

    @property
    def pm_rfi_sync_active(self) -> bool:
        """The PM RFI sync runs: the PM sub-app and its own switch."""
        return bool(self.pm_enabled and self.pm_rfi_sync_enabled)

    @property
    def pm_rfi_sync_slot(self) -> tuple[int, int]:
        """PM_RFI_SYNC_DAILY_TIME as one Pacific (hour, minute) pair
        (validated at boot by `_validate_pm_rfi_sync`)."""
        return _parse_hhmm(self.pm_rfi_sync_daily_time, "PM_RFI_SYNC_DAILY_TIME")

    @model_validator(mode="after")
    def _validate_pm_rfi_sync(self) -> "Settings":
        """Refuse to boot on a PM RFI sync setting that could only fail at run
        time. Always enforced (the defaults satisfy every rule)."""
        _parse_hhmm(self.pm_rfi_sync_daily_time, "PM_RFI_SYNC_DAILY_TIME")
        if not 1 <= self.pm_rfi_sync_catchup_hours <= 23:
            raise ValueError("PM_RFI_SYNC_CATCHUP_HOURS must be between 1 and 23.")
        if self.pm_rfi_sync_poll_seconds < 5 or self.pm_rfi_sync_lease_seconds < 60:
            raise ValueError(
                "PM_RFI_SYNC_POLL_SECONDS must be at least 5 and PM_RFI_SYNC_LEASE_SECONDS at least 60."
            )
        if self.pm_rfi_sync_max_attempts < 1 or self.pm_rfi_sync_retry_seconds < 0:
            raise ValueError(
                "PM_RFI_SYNC_MAX_ATTEMPTS must be at least 1 and PM_RFI_SYNC_RETRY_SECONDS not negative."
            )
        if self.pm_rfi_sync_attachment_max_bytes < 1 or self.pm_rfi_sync_max_details_per_run < 1:
            raise ValueError(
                "PM_RFI_SYNC_ATTACHMENT_MAX_BYTES and PM_RFI_SYNC_MAX_DETAILS_PER_RUN must be positive."
            )
        if self.pm_rfi_email_scan_interval_seconds < 30 or self.pm_rfi_email_scan_lookback_hours < 1:
            raise ValueError(
                "PM_RFI_EMAIL_SCAN_INTERVAL_SECONDS must be at least 30 and "
                "PM_RFI_EMAIL_SCAN_LOOKBACK_HOURS at least 1."
            )
        return self

    # ── Browser agents (docs/browser_agents/SPEC.md, services/browser_agents) ─
    # A Windows PC runs a worker that drives headless Chromium for BDR; BDR
    # owns the queue, the model transcript, the audit ledger and every write.
    # The worker talks to /agent-worker/v1 with its own hashed token (never a
    # user JWT). Off by default. Run migration 0157 first. Provider, model and
    # spend caps live in the DB (Settings > Browser agents), never on the PC.
    browser_agents_enabled: bool = False
    # AES-256-GCM key for the encrypted secret store (the Procore password set
    # in Settings) and the helper one-time secrets: base64 of 32 random bytes.
    browser_agent_secrets_key: str = ""
    browser_agent_secrets_key_id: str = "k1"
    # Retired keys still able to decrypt during a rotation: "k0:<b64>,..."
    browser_agent_secrets_old_keys: str = ""
    # A SEPARATE base64 32-byte key for the HMAC fingerprints shown in Settings.
    browser_agent_fingerprint_key: str = ""
    # Ed25519 public key (base64 of the 32 raw bytes) the signed worker
    # releases are checked against before BDR advertises one. The private key
    # stays offline on the owner's Mac.
    browser_agent_release_pubkey: str = ""
    browser_agent_lease_seconds: int = 90           # a claim's lease; the worker beats every 15 s
    browser_agent_sweep_seconds: int = 20           # sweeper tick (reclaim, park, reconcile, expire)
    browser_agent_rate_limit_per_minute: int = 600  # per worker, per process (the DB fence is the real guard)
    browser_agent_enroll_per_hour: int = 5          # enrollment attempts per IP per hour (DB counter)
    # The public https base of THIS API as the agent PC reaches it, used in
    # the install command Settings > Browser agents > Add worker shows. Empty:
    # derived from the request (https forced).
    browser_agent_api_base_url: str = ""
    # Provider keys for the agent model (BDR proxies every model call; the
    # keys never reach the PC). OPENAI_API_KEY / ANTHROPIC_API_KEY are above.
    google_api_key: str = ""
    xai_api_key: str = ""
    meta_api_key: str = ""
    # Read the client IP from the rightmost X-Forwarded-For entry (Railway's
    # edge proxy appends it). Only for the worker IP allowlist and the
    # enrollment counter; must be true in production when agents are on.
    trust_forwarded_for: bool = False

    @property
    def browser_agents_active(self) -> bool:
        """The worker API, the sweeper and the human routes are served."""
        return bool(self.browser_agents_enabled and self.supabase_url)

    @property
    def browser_agent_old_keys(self) -> dict[str, bytes]:
        """BROWSER_AGENT_SECRETS_OLD_KEYS parsed as {key_id: 32 raw bytes}
        (validated at boot by `_validate_browser_agents`)."""
        out: dict[str, bytes] = {}
        for entry in (self.browser_agent_secrets_old_keys or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            key_id, sep, raw = entry.partition(":")
            if not sep or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", key_id.strip()):
                raise ValueError("BROWSER_AGENT_SECRETS_OLD_KEYS entries must look like <key_id>:<base64>.")
            out[key_id.strip()] = _b64_key(raw, "BROWSER_AGENT_SECRETS_OLD_KEYS")
        return out

    @model_validator(mode="after")
    def _validate_browser_agents(self) -> "Settings":
        """Refuse to boot on a browser agent setting that could only fail at
        run time. The numeric rules always apply; the key and dependency rules
        apply once BROWSER_AGENTS_ENABLED is true."""
        if not 30 <= self.browser_agent_lease_seconds <= 600:
            raise ValueError("BROWSER_AGENT_LEASE_SECONDS must be between 30 and 600.")
        if not 5 <= self.browser_agent_sweep_seconds <= 300:
            raise ValueError("BROWSER_AGENT_SWEEP_SECONDS must be between 5 and 300.")
        if self.browser_agent_rate_limit_per_minute < 60 or self.browser_agent_enroll_per_hour < 1:
            raise ValueError(
                "BROWSER_AGENT_RATE_LIMIT_PER_MINUTE must be at least 60 and "
                "BROWSER_AGENT_ENROLL_PER_HOUR at least 1."
            )
        if self.browser_agent_release_pubkey.strip():
            _b64_key(self.browser_agent_release_pubkey, "BROWSER_AGENT_RELEASE_PUBKEY")
        if not self.browser_agents_enabled:
            return self
        _b64_key(self.browser_agent_secrets_key, "BROWSER_AGENT_SECRETS_KEY")
        fingerprint = _b64_key(self.browser_agent_fingerprint_key, "BROWSER_AGENT_FINGERPRINT_KEY")
        if fingerprint == _b64_key(self.browser_agent_secrets_key, "BROWSER_AGENT_SECRETS_KEY"):
            raise ValueError(
                "BROWSER_AGENT_FINGERPRINT_KEY must differ from BROWSER_AGENT_SECRETS_KEY."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", self.browser_agent_secrets_key_id or ""):
            raise ValueError("BROWSER_AGENT_SECRETS_KEY_ID must be 1 to 20 letters, digits, _ or -.")
        if self.browser_agent_secrets_key_id in self.browser_agent_old_keys:
            raise ValueError(
                "BROWSER_AGENT_SECRETS_OLD_KEYS must not reuse BROWSER_AGENT_SECRETS_KEY_ID."
            )
        # Browser pools also serve standalone read tasks. The org RFI type
        # checks its own PM integration and identity when preparing a job.
        if self.is_production and not self.trust_forwarded_for:
            raise ValueError(
                "BROWSER_AGENTS_ENABLED=true in production needs TRUST_FORWARDED_FOR=true "
                "(the worker IP allowlist and the enrollment counter read the client IP)."
            )
        return self

    @model_validator(mode="after")
    def _validate_personal_browser(self) -> "Settings":
        if not self.assistant_browser_enabled:
            return self
        if not self.assistant_enabled or not self.browser_agents_enabled:
            raise ValueError("ASSISTANT_BROWSER_ENABLED needs assistant and browser agents enabled.")
        key = _b64_key(self.user_cred_kek, "USER_CRED_KEK")
        for other in (self.browser_agent_secrets_key, self.browser_agent_fingerprint_key):
            if other.strip() and key == _b64_key(other, "browser agent key"):
                raise ValueError("USER_CRED_KEK must differ from organization browser keys.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", self.user_cred_kek_id):
            raise ValueError("USER_CRED_KEK_ID must be 1 to 20 letters, digits, _ or -.")
        seen = {self.user_cred_kek_id}
        for entry in self.user_cred_old_keys.split(","):
            if not entry.strip():
                continue
            name, sep, value = entry.strip().partition(":")
            if not sep or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", name) or name in seen:
                raise ValueError("USER_CRED_OLD_KEYS contains an invalid or duplicate key id.")
            seen.add(name)
            _b64_key(value, "USER_CRED_OLD_KEYS")
        return self

    @property
    def rfp_email_ingestion_inboxes(self) -> list[str]:
        """Watched mailboxes, lowercased, deduplicated, blanks dropped."""
        seen: list[str] = []
        for raw in self.rfp_email_ingestion_inboxes_allowed.split(","):
            addr = raw.strip().lower()
            if addr and addr not in seen:
                seen.append(addr)
        return seen

    @property
    def rfp_email_ingestion_shared_mailbox_set(self) -> set[str]:
        """Mailboxes every internal role may see into, lowercased, blanks
        dropped (docs/RFP_EMAIL_VISIBILITY.md 3.2). A set rather than a list:
        it is only ever unioned with a person's own mailboxes."""
        return {
            m.strip().lower()
            for m in self.rfp_email_ingestion_shared_mailboxes.split(",")
            if m.strip()
        }

    @property
    def rfp_email_ingestion_internal_domain_set(self) -> set[str]:
        return {
            d.strip().lower()
            for d in self.rfp_email_ingestion_internal_domains.split(",")
            if d.strip()
        }

    @property
    def rfp_email_ingestion_blocked_domain_set(self) -> set[str]:
        """Environment-pinned domains dropped at listing time, lowercased,
        blanks and stray dots removed (subdomain coverage is decided by the
        poller). Empty by default: the managed list is `rfp_blocked_senders`
        and the poller drops the union of the two."""
        return {
            d.strip().lower().strip(".")
            for d in self.rfp_email_ingestion_blocked_domains.split(",")
            if d.strip().strip(".")
        }

    @property
    def rfp_email_ingestion_enabled(self) -> bool:
        """The email intake runs only under the master switch with mailboxes set."""
        return self.rfp_ingest_enabled and bool(self.rfp_email_ingestion_inboxes)

    @property
    def procore_configured(self) -> bool:
        """Both Procore credentials are set (the harvester exists at all)."""
        return bool(self.procore_login_email.strip() and self.procore_login_password)

    @property
    def rfp_harvest_file_cap(self) -> int:
        """Manifest rows the harvest accepts: its own cap, never more than
        the sandbox run it fills can hold."""
        return max(1, min(self.rfp_harvest_max_files, self.rfp_ingest_max_files_per_run))

    @property
    def rfp_harvest_active(self) -> bool:
        """The harvest slice runs: master switch, its own switch, and at least
        one harvester (Procore with credentials, PipelineSuite, SmartBid, or
        the email harvester; the last three need no credentials). Procore
        counts as configured with the password in Settings > Browser agents
        too, not only the env value (procore_creds.configured)."""
        from app.services import procore_creds

        return (
            self.rfp_ingest_enabled
            and self.rfp_harvest_enabled
            and (
                self.pipelinesuite_enabled
                or self.smartbid_enabled
                or self.rfp_harvest_email_enabled
                or procore_creds.configured(self)
            )
        )

    @property
    def ngem_configured(self) -> bool:
        """The NGEM supplier account exists at all: both credentials and the
        entry URL (the client cannot reach the grid without it)."""
        return bool(
            self.ngem_login_username.strip()
            and self.ngem_login_password
            and self.ngem_entry_url.strip()
        )

    @property
    def rfp_ngem_active(self) -> bool:
        """The NGEM slice runs (the scheduler and the sweep): master switch,
        its own switch, a configured account, and the queue that runs its
        two job types."""
        return (
            self.rfp_ingest_enabled
            and self.rfp_ngem_enabled
            and self.ngem_configured
            and self.llm_queue_enabled
        )

    @property
    def rfp_ngem_schedule(self) -> list[tuple[int, int]]:
        """RFP_NGEM_SCHEDULE_TIMES as (hour, minute) tuples, in the order
        given (validated at boot by `_validate_rfp_ngem`)."""
        return _parse_schedule_times(self.rfp_ngem_schedule_times)

    @property
    def bc_configured(self) -> bool:
        """The APS app exists at all: both the client id and the client
        secret are non-blank."""
        return bool(
            self.building_connected_client_id.strip()
            and self.building_connected_client_secret.strip()
        )

    @property
    def rfp_bc_active(self) -> bool:
        """The BuildingConnected slice runs (its scheduler slots and the
        sweep): master switch, its own switch, a configured APS app, and the
        queue that runs the scan job."""
        return (
            self.rfp_ingest_enabled
            and self.rfp_bc_enabled
            and self.bc_configured
            and self.llm_queue_enabled
        )

    @property
    def rfp_bc_full_sync_slot(self) -> tuple[int, int]:
        """RFP_BC_FULL_SYNC_TIME as one Pacific (hour, minute) pair
        (validated at boot by `_validate_rfp_bc`)."""
        return _parse_hhmm(self.rfp_bc_full_sync_time, "RFP_BC_FULL_SYNC_TIME")

    @property
    def rfp_portal_any_active(self) -> bool:
        """At least one portal slice runs: the gate for the shared scheduler
        loop (main.py lifespan)."""
        return self.rfp_ngem_active or self.rfp_bc_active

    @property
    def rfp_portal_any_enabled(self) -> bool:
        """At least one portal slice is switched on (the two switches only,
        no credentials): the gate for the queue's portal claim pass and the
        shared /rfp-portal surface."""
        return bool(self.rfp_ingest_enabled and (self.rfp_ngem_enabled or self.rfp_bc_enabled))

    @field_validator("environment", mode="before")
    @classmethod
    def _normalize_environment(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower()
            if v == "prod":
                v = "production"
        return v

    @property
    def is_production(self) -> bool:
        """Fail closed: only an explicit dev label relaxes the production
        guards, so an unknown or misspelled ENVIRONMENT stays locked down."""
        return self.environment not in DEV_ENVIRONMENTS

    @model_validator(mode="after")
    def _validate_environment_explicit(self) -> "Settings":
        """On Railway ENVIRONMENT must be set explicitly: a dropped variable
        would otherwise fall back to the development default."""
        import os

        if os.environ.get("RAILWAY_ENVIRONMENT") and "environment" not in self.model_fields_set:
            raise ValueError(
                "Refusing to boot: ENVIRONMENT is not set on a Railway deployment. "
                "Set ENVIRONMENT=production (or the intended environment) explicitly."
            )
        return self

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        """Fail fast (or loudly warn) on unsafe production configuration."""
        if self.is_production:
            if not self.supabase_service_role_key:
                raise ValueError(
                    "SUPABASE_SERVICE_ROLE_KEY must be set in production."
                )
            if not self.mfa_required and not self.mfa_break_glass_acknowledged:
                raise ValueError(
                    "Refusing to boot: MFA_REQUIRED=false in production without "
                    "MFA_BREAK_GLASS_ACKNOWLEDGED=true. 2FA must not be silently "
                    "disabled - set the acknowledgement var only for a deliberate "
                    "break-glass window."
                )
            if not self.mfa_required:
                logging.getLogger("bdr.security").critical(
                    "MFA enforcement is DISABLED in production (break-glass "
                    "acknowledged). Re-enable MFA_REQUIRED as soon as possible."
                )
            # The RFP test bench redirects EVERY outbound email and pauses the
            # real RFP mailboxes while a session is active: a dev-only tool
            # that must never exist on the production server.
            if self.rfp_testing_enabled:
                raise ValueError(
                    "Refusing to boot: RFP_TESTING_ENABLED=true in production. The "
                    "RFP test bench is dev only (docs/RFP_TESTING.md); leave the "
                    "variable unset in Railway."
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
        """At least one sub-app must be served - all three off is a config typo,
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
        if self.self_hosted_llm_target not in SELF_HOSTED_LLM_TARGETS:
            raise ValueError(
                "SELF_HOSTED_LLM_TARGET must be one of "
                f"{', '.join(repr(t) for t in SELF_HOSTED_LLM_TARGETS)} "
                f"(got {self.self_hosted_llm_target!r})."
            )
        url = self.self_hosted_llm_base_url
        if not url:
            raise ValueError(
                "Refusing to boot: FULL_SELF_HOSTED_LLMS_ENABLED=true but the "
                f"'{self.self_hosted_llm_target}' target has no base URL. Set "
                f"SELF_HOSTED_LLM_{self.self_hosted_llm_target.upper()}_BASE_URL "
                "(e.g. http://localhost:11434/v1), pick another target, or turn "
                "the switch off."
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
        if self.is_production:
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
                    "bid data - move to https as soon as possible."
                )
            elif not self.self_hosted_llm_verify_tls:
                sec_log.critical(
                    "TLS verification for the self-hosted LLM endpoint is DISABLED "
                    "in production. Use SELF_HOSTED_LLM_CA_BUNDLE for private CAs "
                    "and re-enable SELF_HOSTED_LLM_VERIFY_TLS."
                )
        return self

    @model_validator(mode="after")
    def _validate_assistant(self) -> "Settings":
        """Refuse to boot on an assistant configuration that could only fail
        at run time (docs/ASSISTANT.md, Architecture): no token secret, an
        OpenAI-only feature under the self-hosted master switch without the
        owner's explicit allow, a scheduler without an assistant, caps that
        cannot be enforced, or routine knobs out of range. Only enforced while ASSISTANT_ENABLED is on (the
        scheduler check runs always, since it is meaningless on its own)."""
        if self.assistant_scheduler_enabled and not self.assistant_enabled:
            raise ValueError(
                "ASSISTANT_SCHEDULER_ENABLED=true needs ASSISTANT_ENABLED=true."
            )
        if self.assistant_proactive_enabled and not self.assistant_enabled:
            raise ValueError("ASSISTANT_PROACTIVE_ENABLED=true needs ASSISTANT_ENABLED=true.")
        if self.assistant_proactive_external_enabled and not self.assistant_proactive_enabled:
            raise ValueError(
                "ASSISTANT_PROACTIVE_EXTERNAL_ENABLED=true needs ASSISTANT_PROACTIVE_ENABLED=true."
            )
        if self.assistant_triage_enabled and not self.assistant_proactive_enabled:
            raise ValueError("ASSISTANT_TRIAGE_ENABLED=true needs ASSISTANT_PROACTIVE_ENABLED=true.")
        self._check_proactive_test_email()
        self._check_triage_test_cap()
        if not self.assistant_enabled:
            return self
        if len((self.assistant_token_secret or "").strip()) < 32:
            raise ValueError(
                "Refusing to boot: ASSISTANT_ENABLED=true but ASSISTANT_TOKEN_SECRET "
                "is missing or shorter than 32 characters. Generate one with "
                "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        if self.full_self_hosted_llms_enabled and not self.assistant_allow_openai_in_self_hosted:
            raise ValueError(
                "Refusing to boot: ASSISTANT_ENABLED=true while "
                "FULL_SELF_HOSTED_LLMS_ENABLED=true. The assistant runs on OpenAI "
                "only; set ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED=true (owner "
                "authorized) or turn the assistant off."
            )
        if self.assistant_daily_cap_usd <= 0 or self.assistant_global_daily_cap_usd <= 0:
            raise ValueError(
                "ASSISTANT_DAILY_CAP_USD and ASSISTANT_GLOBAL_DAILY_CAP_USD must be "
                "greater than zero."
            )
        if self.assistant_daily_cap_usd > self.assistant_global_daily_cap_usd:
            raise ValueError(
                "ASSISTANT_DAILY_CAP_USD must not exceed ASSISTANT_GLOBAL_DAILY_CAP_USD."
            )
        if self.assistant_reasoning_effort not in ("minimal", "low", "medium", "high"):
            raise ValueError("ASSISTANT_REASONING_EFFORT must be minimal, low, medium or high.")
        if self.assistant_concurrency < 1:
            raise ValueError("ASSISTANT_CONCURRENCY must be at least 1.")
        for name, low, high in (
            ("assistant_scheduler_poll_seconds", 15, 600),
            ("assistant_schedule_catchup_hours", 1, 24),
            ("assistant_schedule_claim_batch", 1, 50),
            ("assistant_schedule_max_failures", 1, 20),
            ("assistant_schedule_max_unread", 1, 100),
            ("assistant_schedule_stale_run_minutes", 5, 120),
            ("assistant_run_now_min_seconds", 0, 3600),
            ("assistant_schedule_max_emails_per_day", 0, 200),
            ("assistant_proactive_poll_seconds", 10, 300),
            ("assistant_proactive_claim_batch", 1, 200),
            ("assistant_proactive_digest_catchup_hours", 1, 5),
            ("assistant_proactive_cursor_max_lag_minutes", 5, 1440),
            ("assistant_proactive_send_poll_batch", 1, 50),
            ("assistant_proactive_send_lease_seconds", 60, 900),
            ("assistant_proactive_inbox_retention_days", 7, 3650),
            ("assistant_proactive_event_retention_days", 7, 3650),
            ("assistant_proactive_digest_retention_days", 7, 3650),
            ("assistant_proactive_send_retention_days", 7, 3650),
        ):
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"{name.upper()} must be between {low} and {high}.")
        self._check_proactive_times()
        self._check_triage()
        from app.services import llm_pricing  # local: config must stay import-light

        if llm_pricing.price_for(self.assistant_model) is None:
            raise ValueError(
                f"ASSISTANT_MODEL={self.assistant_model!r} has no row in services/llm_pricing.PRICES; "
                "add the price before enabling the assistant (spend caps need it)."
            )
        return self

    def _check_proactive_times(self) -> None:
        """Phase 7 digest slots and quiet hours (plan 12.1): exactly two
        distinct HH:MM slots at least catch-up hours apart, and no slot's
        delivery window (slot to slot + catch-up hours) touches quiet hours,
        so the AM slot is at or after the quiet end and each slot plus its
        window is at or before the quiet start."""
        raw = (self.assistant_proactive_digest_times or "").split(",")
        if len(raw) != 2:
            raise ValueError(
                "ASSISTANT_PROACTIVE_DIGEST_TIMES must list exactly two HH:MM times."
            )
        slots = sorted(
            h * 60 + m
            for h, m in (_parse_hhmm(part, "ASSISTANT_PROACTIVE_DIGEST_TIMES") for part in raw)
        )
        qs_h, qs_m = _parse_hhmm(self.assistant_proactive_quiet_start, "ASSISTANT_PROACTIVE_QUIET_START")
        qe_h, qe_m = _parse_hhmm(self.assistant_proactive_quiet_end, "ASSISTANT_PROACTIVE_QUIET_END")
        catchup = self.assistant_proactive_digest_catchup_hours * 60
        if slots[0] == slots[1]:
            raise ValueError("ASSISTANT_PROACTIVE_DIGEST_TIMES lists the same time twice.")
        if slots[1] - slots[0] < catchup:
            raise ValueError(
                "ASSISTANT_PROACTIVE_DIGEST_TIMES slots must be at least "
                "ASSISTANT_PROACTIVE_DIGEST_CATCHUP_HOURS apart."
            )
        quiet_start, quiet_end = qs_h * 60 + qs_m, qe_h * 60 + qe_m
        quiet = {(quiet_start + i) % 1440 for i in range((quiet_end - quiet_start) % 1440)}
        for slot in slots:
            if any((slot + i) % 1440 in quiet for i in range(catchup)):
                raise ValueError(
                    "ASSISTANT_PROACTIVE_DIGEST_TIMES: each slot plus the catch-up window must "
                    "stay outside quiet hours (ASSISTANT_PROACTIVE_QUIET_START to _END)."
                )

    def _check_triage(self) -> None:
        """Phase 8 triage knobs (plan 13), checked while ASSISTANT_ENABLED is
        on: ranges, the model price rule (an OpenAI row in llm_pricing.PRICES,
        or every triage call would log a NULL cost), the lease that outlives
        one worst-case model call (the RFP lease rule), and an output
        allowance that fits a full batch. The self-hosted master switch needs
        no extra check here: triage needs the proactive router, which needs
        the assistant, and the assistant already refuses to boot under the
        master switch without ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED."""
        for name, low, high in (
            ("assistant_triage_poll_seconds", 15, 600),
            ("assistant_triage_debounce_seconds", 0, 3600),
            ("assistant_triage_batch_max", 1, 20),
            ("assistant_triage_batches_per_tick", 1, 20),
            ("assistant_triage_lease_seconds", 60, 1800),
            ("assistant_triage_timeout_seconds", 10, 120),
            ("assistant_triage_max_output_tokens", 1400, 6000),
            ("assistant_triage_window_hours", 1, 72),
            ("assistant_triage_fuse_ratio", 0, 1),
            ("assistant_triage_fuse_per_hour", 0, 5000),
            ("assistant_triage_cap_pause_seconds", 60, 3600),
            ("assistant_triage_retention_days", 7, 3650),
        ):
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"{name.upper()} must be between {low} and {high}.")
        from app.services import llm_pricing  # local: config must stay import-light

        price = llm_pricing.price_for(self.assistant_triage_model)
        if price is None or price.provider != "openai":
            raise ValueError(
                f"ASSISTANT_TRIAGE_MODEL={self.assistant_triage_model!r} must have an OpenAI row in "
                "services/llm_pricing.PRICES; add the price first (spend accounting needs it)."
            )
        needed_lease = (
            self.llm_background_wait_seconds
            + self.assistant_triage_timeout_seconds * (self.third_party_llm_max_retries + 1)
            + 60
        )
        if self.assistant_triage_lease_seconds < needed_lease:
            too_short = (
                "ASSISTANT_TRIAGE_LEASE_SECONDS must be at least LLM_BACKGROUND_WAIT_SECONDS plus "
                "ASSISTANT_TRIAGE_TIMEOUT_SECONDS x (THIRD_PARTY_LLM_MAX_RETRIES + 1) plus 60 "
                f"({needed_lease:g}) so the lease outlives one model call."
            )
            if "assistant_triage_lease_seconds" in self.model_fields_set:
                raise ValueError(too_short)
            # Not configured: stretch the default to cover one call instead of
            # refusing to boot a deployment with a long LLM wait or retry count
            # (the RFP lease pattern). If the stretch would pass the 1800 cap,
            # only a deployment that actually runs triage is refused.
            stretched = int(math.ceil(needed_lease))
            if stretched <= 1800:
                self.assistant_triage_lease_seconds = stretched
            elif self.assistant_triage_enabled:
                raise ValueError(too_short)
        # 1200 + 200 x items: assistant_triage_model.OUTPUT_TOKENS_BASE and
        # OUTPUT_TOKENS_PER_ITEM (P8L-3; pinned equal by the config tests).
        needed_out = 1200 + 200 * self.assistant_triage_batch_max
        if self.assistant_triage_max_output_tokens < needed_out:
            raise ValueError(
                "ASSISTANT_TRIAGE_MAX_OUTPUT_TOKENS must be at least 1200 + 200 x "
                f"ASSISTANT_TRIAGE_BATCH_MAX ({needed_out})."
            )
        effort = (self.assistant_triage_reasoning_effort or "").strip().lower()
        if effort not in ("", "low", "medium", "high"):
            raise ValueError("ASSISTANT_TRIAGE_REASONING_EFFORT must be low, medium, high or empty.")
        self.assistant_triage_reasoning_effort = effort

    def _check_triage_test_cap(self) -> None:
        """The live-test spend cap (plan 13, owner question 22): 0 to 5 USD,
        only on the dev Supabase project and only with an allowlist, so it can
        never exist on a prod-like server. Unset (None) is the normal state."""
        cap = self.assistant_triage_test_spend_cap_usd
        if cap is None:
            return
        if not 0 <= cap <= 5:
            raise ValueError("ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD must be between 0 and 5.")
        if not (self.assistant_triage_user_allowlist or "").strip(" ,"):
            raise ValueError(
                "ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD needs ASSISTANT_TRIAGE_USER_ALLOWLIST."
            )
        if _PHASE7_DEV_SUPABASE_REF not in (self.supabase_url or ""):
            raise ValueError(
                "ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD is allowed only on the dev Supabase project."
            )

    def _check_proactive_test_email(self) -> None:
        """The live-test recipient override (plan 12.1, owner question 38):
        one address, an allowlist required, and only on the dev Supabase
        project, so it can never exist on a prod-like server."""
        raw = (self.assistant_proactive_test_email_to or "").strip()
        if not raw:
            return
        if len(raw.split()) != 1 or any(c in raw for c in ",;<>") or raw.count("@") != 1:
            raise ValueError(
                "ASSISTANT_PROACTIVE_TEST_EMAIL_TO must be exactly one email address."
            )
        local, _, domain = raw.partition("@")
        if not local or "." not in domain:
            raise ValueError(
                "ASSISTANT_PROACTIVE_TEST_EMAIL_TO must be exactly one email address."
            )
        if not (self.assistant_proactive_user_allowlist or "").strip(" ,"):
            raise ValueError(
                "ASSISTANT_PROACTIVE_TEST_EMAIL_TO needs ASSISTANT_PROACTIVE_USER_ALLOWLIST."
            )
        url = (self.supabase_url or "").strip()
        if url and _PHASE7_DEV_SUPABASE_REF not in url:
            raise ValueError(
                "ASSISTANT_PROACTIVE_TEST_EMAIL_TO is allowed only on the dev Supabase project."
            )

    @model_validator(mode="after")
    def _validate_assistant_phase4(self) -> "Settings":
        """Phase 4 ranges (docs/ASSISTANT_PHASE4_PLAN.md 3.21), checked only
        while ASSISTANT_ENABLED is on, like the phase 3 knobs. The AE run hold
        TTL stops at 43,200 seconds because the 0174 reserve guard refuses a
        longer hold."""
        if not self.assistant_enabled:
            return self
        for name, low, high in (
            ("assistant_plans_pages_per_card", 1, 12),
            ("assistant_plans_context_tokens", 0, 200000),
            ("assistant_plans_hold_factor", 1, 3),
            ("assistant_plans_page_read_timeout_minutes", 10, 720),
            ("assistant_ae_run_hold_ttl_seconds", 30, 43200),
            ("assistant_ae_run_max_files", 1, 40),
            ("assistant_ae_run_max_pages", 1, 1500),
            ("assistant_report_ttl_days", 1, 30),
            ("assistant_report_max_rows", 1, 20000),
            ("assistant_evidence_default_days", 1, 400),
            ("assistant_evidence_max_days", 1, 400),
            ("assistant_cp_small_cell_min", 3, 50),
        ):
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"{name.upper()} must be between {low} and {high}.")
        if self.assistant_evidence_default_days > self.assistant_evidence_max_days:
            raise ValueError(
                "ASSISTANT_EVIDENCE_DEFAULT_DAYS must not exceed ASSISTANT_EVIDENCE_MAX_DAYS."
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

    @model_validator(mode="after")
    def _validate_bid_splitter(self) -> "Settings":
        """A typo'd provider would otherwise surface as a KeyError mid-job."""
        if self.bid_split_llm_provider not in ("anthropic", "openai", "self_hosted"):
            raise ValueError(
                "BID_SPLIT_LLM_PROVIDER must be 'anthropic', 'openai' or "
                f"'self_hosted' (got {self.bid_split_llm_provider!r})."
            )
        return self

    @model_validator(mode="after")
    def _validate_plans_reader(self) -> "Settings":
        """AI Estimating knobs: a typo'd effort or a budget past an OpenAI
        hard limit should refuse to boot, not fail every run at call time."""
        effort = (self.plans_reader_reasoning_effort or "").strip().lower()
        if effort not in PLANS_READER_REASONING_EFFORTS:
            raise ValueError(
                "PLANS_READER_REASONING_EFFORT must be one of "
                f"{', '.join(PLANS_READER_REASONING_EFFORTS)} "
                f"(got {self.plans_reader_reasoning_effort!r})."
            )
        self.plans_reader_reasoning_effort = effort
        if not (self.openai_plans_model or "").strip():
            raise ValueError("OPENAI_PLANS_MODEL must not be empty.")
        if not 10_000 <= self.plans_reader_group_input_tokens <= 272_000:
            raise ValueError(
                "PLANS_READER_GROUP_INPUT_TOKENS must be between 10000 and 272000 "
                "(above 272000 tokens the whole request bills at the long-context rate)."
            )
        if not 1_000 <= self.plans_reader_ledger_max_tokens < self.plans_reader_group_input_tokens:
            raise ValueError(
                "PLANS_READER_LEDGER_MAX_TOKENS must be at least 1000 and below "
                "PLANS_READER_GROUP_INPUT_TOKENS (the ledger rides inside every group)."
            )
        if self.plans_reader_max_input_tokens_per_run < self.plans_reader_group_input_tokens:
            raise ValueError(
                "PLANS_READER_MAX_INPUT_TOKENS_PER_RUN must be at least "
                "PLANS_READER_GROUP_INPUT_TOKENS (a run is at least one group)."
            )
        if not 1 <= self.plans_reader_promax_max_pages_per_message <= 100:
            raise ValueError("PLANS_READER_PROMAX_MAX_PAGES_PER_MESSAGE must be between 1 and 100.")
        if not 0.0 <= self.plans_reader_escalation_max_fraction <= 1.0:
            raise ValueError(
                "PLANS_READER_ESCALATION_MAX_FRACTION must be between 0 and 1 (0 disables)."
            )
        if not 72 <= self.plans_reader_tile_ppi <= 600:
            raise ValueError("PLANS_READER_TILE_PPI must be between 72 and 600.")
        if not 1_000 <= self.plans_reader_native_max_patches <= 30_000:
            raise ValueError(
                "PLANS_READER_NATIVE_MAX_PATCHES must be between 1000 and 30000 "
                "(OpenAI rejects detail original images above 30000 patches)."
            )
        if not 1 <= self.plans_reader_file_max_bytes <= 50 * 1024 * 1024:
            raise ValueError(
                "PLANS_READER_FILE_MAX_BYTES must be between 1 and 52428800 "
                "(OpenAI caps input files at 50 MB)."
            )
        if self.plans_reader_call_timeout_seconds < 30:
            raise ValueError("PLANS_READER_CALL_TIMEOUT_SECONDS must be at least 30.")
        if not 1 <= self.plans_reader_concurrency <= 32:
            raise ValueError("PLANS_READER_CONCURRENCY must be between 1 and 32.")
        if self.plans_reader_tokens_per_minute != 0 and self.plans_reader_tokens_per_minute < 10_000:
            raise ValueError(
                "PLANS_READER_TOKENS_PER_MINUTE must be 0 (pacing off) or at least 10000."
            )
        if self.plans_reader_enabled:
            # The lease is renewed after gate admission, so the gate wait is
            # counted only as a conservative bound; the headroom covers the
            # upload of up to 50 MB and the writes after the call.
            needed = (
                self.plans_reader_call_timeout_seconds
                + self.llm_background_wait_seconds
                + PLANS_READER_LEASE_HEADROOM_SECONDS
            )
            if needed > self.llm_queue_lease_seconds:
                raise ValueError(
                    "PLANS_READER_CALL_TIMEOUT_SECONDS plus LLM_BACKGROUND_WAIT_SECONDS "
                    f"plus {PLANS_READER_LEASE_HEADROOM_SECONDS}s of headroom ({needed:g}) "
                    "must fit inside LLM_QUEUE_LEASE_SECONDS so one AI Estimating "
                    "call fits inside a queue lease."
                )
        return self

    @model_validator(mode="after")
    def _validate_rfp_ingest(self) -> "Settings":
        """Refuse to boot on a sandbox configuration that could only fail at
        run time: tiers out of order, a memory limit the child cannot start
        under, a uid pool too small for the concurrency, byte caps that the
        request layer or the storage bucket would refuse first, or a stall
        timeout that lets the queue's lease sweep race the runner. Always
        enforced (the defaults satisfy every rule) so a bad value surfaces
        at deploy time rather than when the flag is flipped on."""
        if not (
            self.rfp_ingest_thumb_long_side
            <= self.rfp_ingest_full_small_long_side
            <= self.rfp_ingest_full_long_side
        ):
            raise ValueError(
                "RFP_INGEST_THUMB_LONG_SIDE must be at most "
                "RFP_INGEST_FULL_SMALL_LONG_SIDE, which must be at most "
                "RFP_INGEST_FULL_LONG_SIDE (the rendering tiers are ordered)."
            )
        if not 0.0 <= self.rfp_ingest_max_failed_page_ratio <= 1.0:
            raise ValueError(
                "RFP_INGEST_MAX_FAILED_PAGE_RATIO must be between 0 and 1 "
                f"(got {self.rfp_ingest_max_failed_page_ratio!r})."
            )
        if self.rfp_ingest_sandbox_memory_mb < 512:
            raise ValueError(
                "RFP_INGEST_SANDBOX_MEMORY_MB must be at least 512 (the child "
                "cannot import PDFium and render a large sheet under less)."
            )
        if self.rfp_ingest_sandbox_concurrency < 1:
            raise ValueError("RFP_INGEST_SANDBOX_CONCURRENCY must be at least 1.")
        if self.rfp_ingest_sandbox_uid_pool_size < 2 * self.rfp_ingest_sandbox_concurrency:
            raise ValueError(
                "RFP_INGEST_SANDBOX_UID_POOL_SIZE must be at least twice "
                "RFP_INGEST_SANDBOX_CONCURRENCY (one slot uid per child with "
                "headroom for a second worker process)."
            )
        if self.rfp_ingest_max_file_bytes > self.upload_max_bytes:
            raise ValueError(
                "RFP_INGEST_MAX_FILE_BYTES must not exceed UPLOAD_MAX_BYTES "
                "(the upload layer would refuse the file first)."
            )
        if not 1 <= self.max_json_body_bytes <= self.max_request_body_bytes:
            raise ValueError(
                "MAX_JSON_BODY_BYTES must be at least 1 and at most "
                "MAX_REQUEST_BODY_BYTES (the non-multipart cap can never "
                "exceed the multipart one)."
            )
        if self.rfp_ingest_sandbox_output_file_mb > self.rfp_ingest_sandbox_disk_mb:
            raise ValueError(
                "RFP_INGEST_SANDBOX_OUTPUT_FILE_MB must not exceed "
                "RFP_INGEST_SANDBOX_DISK_MB (a single artifact cannot be "
                "larger than the whole output quota)."
            )
        if self.rfp_ingest_page_stall_seconds >= self.llm_queue_lease_seconds / 2:
            raise ValueError(
                "RFP_INGEST_PAGE_STALL_SECONDS must be below half of "
                "LLM_QUEUE_LEASE_SECONDS so a stalled child is killed and the "
                "job requeued before the lease sweep can race the runner."
            )
        if self.rfp_ingest_images_pdf_part_bytes > 200 * 1024 * 1024:
            raise ValueError(
                "RFP_INGEST_IMAGES_PDF_PART_BYTES must not exceed 200 MB "
                "(209715200), the rfp-derived storage bucket's object limit."
            )
        if self.rfp_ingest_office_convert_timeout_seconds < 1:
            raise ValueError("RFP_INGEST_OFFICE_CONVERT_TIMEOUT_SECONDS must be at least 1.")
        if self.rfp_ingest_office_convert_timeout_seconds >= self.llm_queue_lease_seconds / 2:
            raise ValueError(
                "RFP_INGEST_OFFICE_CONVERT_TIMEOUT_SECONDS must be below half of "
                "LLM_QUEUE_LEASE_SECONDS: the queue lease is not renewed while a "
                "file waits on the office-to-PDF converter."
            )
        return self

    @model_validator(mode="after")
    def _validate_rfp_harvest(self) -> "Settings":
        """Pace and lock intervals must be positive or the discipline they
        express is gone; caps must be positive. The file cap is clamped to the
        sandbox's per-run cap at use time (`rfp_harvest_file_cap`)."""
        if self.rfp_harvest_max_files < 1 or self.rfp_harvest_max_total_bytes < 1:
            raise ValueError("RFP_HARVEST_MAX_FILES and RFP_HARVEST_MAX_TOTAL_BYTES must be positive.")
        if self.rfp_harvest_concurrency < 1:
            raise ValueError("RFP_HARVEST_CONCURRENCY must be at least 1.")
        if self.rfp_harvest_poll_seconds < 5 or self.rfp_harvest_reuse_days < 0:
            raise ValueError(
                "RFP_HARVEST_POLL_SECONDS must be at least 5 and RFP_HARVEST_REUSE_DAYS non-negative."
            )
        if self.procore_min_request_interval_seconds < 0.5:
            raise ValueError(
                "PROCORE_MIN_REQUEST_INTERVAL_SECONDS must be at least 0.5 (the harvest "
                "must keep a human pace against Procore)."
            )
        if (
            self.procore_login_min_interval_seconds < 60
            or self.procore_login_max_failures < 1
            or self.procore_login_lock_seconds < 60
            or self.procore_request_timeout_seconds <= 0
        ):
            raise ValueError(
                "PROCORE_LOGIN_MIN_INTERVAL_SECONDS and PROCORE_LOGIN_LOCK_SECONDS must be "
                "at least 60, PROCORE_LOGIN_MAX_FAILURES at least 1, and "
                "PROCORE_REQUEST_TIMEOUT_SECONDS positive."
            )
        if self.pipelinesuite_min_request_interval_seconds < 0.5:
            raise ValueError(
                "PIPELINESUITE_MIN_REQUEST_INTERVAL_SECONDS must be at least 0.5 (the harvest "
                "must keep a human pace against a GC's portal)."
            )
        if (
            self.pipelinesuite_login_min_interval_seconds < 60
            or self.pipelinesuite_login_max_failures < 1
            or self.pipelinesuite_login_lock_seconds < 60
            or self.pipelinesuite_request_timeout_seconds <= 0
        ):
            raise ValueError(
                "PIPELINESUITE_LOGIN_MIN_INTERVAL_SECONDS and PIPELINESUITE_LOGIN_LOCK_SECONDS "
                "must be at least 60, PIPELINESUITE_LOGIN_MAX_FAILURES at least 1, and "
                "PIPELINESUITE_REQUEST_TIMEOUT_SECONDS positive."
            )
        if self.smartbid_min_request_interval_seconds < 0.5:
            raise ValueError(
                "SMARTBID_MIN_REQUEST_INTERVAL_SECONDS must be at least 0.5 (the harvest "
                "must keep a human pace against the platform)."
            )
        if (
            self.smartbid_login_min_interval_seconds < 10
            or self.smartbid_login_max_failures < 1
            or self.smartbid_login_lock_seconds < 60
            or self.smartbid_request_timeout_seconds <= 0
        ):
            raise ValueError(
                "SMARTBID_LOGIN_MIN_INTERVAL_SECONDS must be at least 10, "
                "SMARTBID_LOGIN_LOCK_SECONDS at least 60, SMARTBID_LOGIN_MAX_FAILURES at "
                "least 1, and SMARTBID_REQUEST_TIMEOUT_SECONDS positive."
            )
        if (
            self.rfp_harvest_link_max_count < 1
            or self.rfp_harvest_folder_max_depth < 0
            or self.rfp_harvest_image_signature_max_bytes < 0
            or self.cloud_request_timeout_seconds <= 0
        ):
            raise ValueError(
                "RFP_HARVEST_LINK_MAX_COUNT must be at least 1, RFP_HARVEST_FOLDER_MAX_DEPTH "
                "and RFP_HARVEST_IMAGE_SIGNATURE_MAX_BYTES non-negative, and "
                "CLOUD_REQUEST_TIMEOUT_SECONDS positive."
            )
        return self

    @model_validator(mode="after")
    def _validate_rfp_ngem(self) -> "Settings":
        """Refuse to boot on an NGEM configuration that could only fail at
        run time: a schedule that does not parse (1 to 12 `HH:MM` entries on
        the 24 h clock), an entry URL on any host but the portal's, a
        catch-up window or a pace that would lose the discipline they
        express. Always enforced (the defaults satisfy every rule)."""
        _parse_schedule_times(self.rfp_ngem_schedule_times)
        url = self.ngem_entry_url.strip()
        if url and not url.startswith(NGEM_ENTRY_URL_PREFIX):
            raise ValueError(
                f"NGEM_ENTRY_URL must start with {NGEM_ENTRY_URL_PREFIX} (the supplier "
                "portal's ResponseList.aspx link); the client refuses any other host."
            )
        if not 1 <= self.rfp_ngem_catchup_hours <= 24:
            raise ValueError("RFP_NGEM_CATCHUP_HOURS must be between 1 and 24.")
        if self.rfp_ngem_poll_seconds < 5:
            raise ValueError("RFP_NGEM_POLL_SECONDS must be at least 5.")
        if self.rfp_ngem_max_list_pages < 1 or self.rfp_ngem_sweep_batch < 1:
            raise ValueError("RFP_NGEM_MAX_LIST_PAGES and RFP_NGEM_SWEEP_BATCH must be at least 1.")
        if self.rfp_ngem_scan_timeout_seconds < 60:
            raise ValueError("RFP_NGEM_SCAN_TIMEOUT_SECONDS must be at least 60.")
        if self.ngem_min_request_interval_seconds < 0.5:
            raise ValueError(
                "NGEM_MIN_REQUEST_INTERVAL_SECONDS must be at least 0.5 (the scan and the "
                "harvest must keep a human pace against the portal)."
            )
        if (
            self.ngem_login_min_interval_seconds < 60
            or self.ngem_login_max_failures < 1
            or self.ngem_login_lock_seconds < 60
            or self.ngem_request_timeout_seconds <= 0
        ):
            raise ValueError(
                "NGEM_LOGIN_MIN_INTERVAL_SECONDS and NGEM_LOGIN_LOCK_SECONDS must be at "
                "least 60, NGEM_LOGIN_MAX_FAILURES at least 1, and "
                "NGEM_REQUEST_TIMEOUT_SECONDS positive."
            )
        return self

    @model_validator(mode="after")
    def _validate_rfp_bc(self) -> "Settings":
        """Refuse to boot on a BuildingConnected configuration that could
        only fail at run time: a full sync time that is not one `HH:MM`, a
        poll interval off the hour grid, bounds that would lose what they
        express, a redirect URL that is not a URL. In production, an enabled
        slice needs an https redirect URL (APS matches it byte for byte and
        the callback carries the authorization code), and RFP_BC_TEST_MODE
        (sample data in place of the real board) is refused outright.
        Always enforced (the defaults satisfy every rule)."""
        _parse_hhmm(self.rfp_bc_full_sync_time, "RFP_BC_FULL_SYNC_TIME")
        minutes = self.rfp_bc_poll_minutes
        if not 5 <= minutes <= 60 or 60 % minutes != 0:
            raise ValueError(
                "RFP_BC_POLL_MINUTES must be between 5 and 60 and divide 60 "
                "(5, 6, 10, 12, 15, 20, 30 or 60) so the slots stay on the hour grid."
            )
        if not 1 <= self.rfp_bc_full_sync_catchup_hours <= 23:
            raise ValueError("RFP_BC_FULL_SYNC_CATCHUP_HOURS must be between 1 and 23.")
        if not 0 <= self.rfp_bc_overlap_minutes <= 1440:
            raise ValueError("RFP_BC_OVERLAP_MINUTES must be between 0 and 1440.")
        if not 1 <= self.rfp_bc_expire_days <= 365:
            raise ValueError("RFP_BC_EXPIRE_DAYS must be between 1 and 365.")
        if not 1 <= self.rfp_bc_missing_days <= 365:
            raise ValueError("RFP_BC_MISSING_DAYS must be between 1 and 365.")
        if not 1000 <= self.rfp_bc_text_max_chars <= 20000:
            raise ValueError(
                "RFP_BC_TEXT_MAX_CHARS must be between 1000 and 20000 (the project "
                "detail fields accept at most 20000 characters)."
            )
        if not 0 < self.rfp_bc_request_timeout_seconds <= 600:
            raise ValueError(
                "RFP_BC_REQUEST_TIMEOUT_SECONDS must be positive and at most 600."
            )
        if not 1 <= self.rfp_bc_max_pages <= 10000:
            raise ValueError("RFP_BC_MAX_PAGES must be between 1 and 10000.")
        if self.rfp_bc_scan_queue_priority < 0 or self.rfp_bc_full_sync_queue_priority < 0:
            raise ValueError(
                "RFP_BC_SCAN_QUEUE_PRIORITY and RFP_BC_FULL_SYNC_QUEUE_PRIORITY must "
                "not be negative."
            )
        if self.rfp_bc_callback_rate_limit_per_min < 1:
            # Below 1 the callback's limiter would refuse or crash on every
            # hit (an empty window has no oldest entry to compute Retry-After
            # from); the limit cannot be disabled here, RATE_LIMIT_ENABLED does that.
            raise ValueError(
                "RFP_BC_CALLBACK_RATE_LIMIT_PER_MIN must be at least 1 (set "
                "RATE_LIMIT_ENABLED=false to turn rate limiting off)."
            )
        url = self.rfp_bc_redirect_url
        if url and (
            url != url.strip()
            or any(ch.isspace() for ch in url)
            or not url.startswith(("https://", "http://"))
        ):
            raise ValueError(
                "RFP_BC_REDIRECT_URL must be an http(s) URL with no spaces (it is sent "
                "to Autodesk byte for byte as the OAuth redirect_uri)."
            )
        if self.is_production:
            if self.rfp_bc_test_mode:
                raise ValueError(
                    "Refusing to boot: RFP_BC_TEST_MODE=true in production. Test mode "
                    "swaps the real BuildingConnected board for sample data "
                    "(docs/RFP_BUILDINGCONNECTED.md); leave the variable unset in Railway."
                )
            if (
                self.rfp_ingest_enabled
                and self.rfp_bc_enabled
                and not url.startswith("https://")
            ):
                raise ValueError(
                    "Refusing to boot: BuildingConnected is enabled in production but "
                    "RFP_BC_REDIRECT_URL is "
                    f"{'unset' if not url else 'not an https URL'}; set it to the "
                    "backend's public https callback "
                    "(.../rfp-portal/buildingconnected/callback) as registered with APS."
                )
        return self

    @model_validator(mode="after")
    def _validate_rfp_create(self) -> "Settings":
        """A create claim must outlive the poll that waits on it (or a waiting
        row could take over a live claim), and the caps must be positive.
        Always enforced (the defaults satisfy every rule)."""
        if self.rfp_create_poll_seconds < 5:
            raise ValueError("RFP_CREATE_POLL_SECONDS must be at least 5.")
        if self.rfp_create_claim_seconds < 60 or (
            self.rfp_create_claim_seconds <= self.rfp_create_poll_seconds
        ):
            raise ValueError(
                "RFP_CREATE_CLAIM_SECONDS must be at least 60 and longer than "
                "RFP_CREATE_POLL_SECONDS."
            )
        if self.rfp_create_notes_max_chars < 1:
            raise ValueError("RFP_CREATE_NOTES_MAX_CHARS must be positive.")
        if self.rfp_create_duplicate_window_minutes < 0:
            raise ValueError(
                "RFP_CREATE_DUPLICATE_WINDOW_MINUTES must not be negative (0 disables)."
            )
        if self.rfp_split_poll_seconds < 5:
            raise ValueError("RFP_SPLIT_POLL_SECONDS must be at least 5.")
        if self.rfp_split_stage_concurrency < 1:
            raise ValueError("RFP_SPLIT_STAGE_CONCURRENCY must be at least 1.")
        if self.rfp_split_staging_heartbeat_seconds < 5:
            raise ValueError("RFP_SPLIT_STAGING_HEARTBEAT_SECONDS must be at least 5.")
        if self.rfp_split_staging_stale_seconds < 3 * self.rfp_split_staging_heartbeat_seconds:
            # Two missed heartbeats (a slow database write, a long GC pause)
            # must never read a live staging as dead.
            raise ValueError(
                "RFP_SPLIT_STAGING_STALE_SECONDS must be at least three times "
                "RFP_SPLIT_STAGING_HEARTBEAT_SECONDS."
            )
        return self

    @model_validator(mode="after")
    def _validate_rfp_testing(self) -> "Settings":
        """With the test bench on, the three addresses must be set and well
        formed, and the test mailbox must be dedicated: not one of the real
        RFP mailboxes and not the project email filer's own (a session pauses
        those and watches this one; the same address in both lists would
        make "paused" and "watched" the same mailbox). The poll interval and
        the unwrap scan window are checked whatever the switch says so a bad
        value surfaces at deploy time."""
        if self.rfp_testing_poll_seconds < 5:
            raise ValueError("RFP_TESTING_POLL_SECONDS must be at least 5.")
        if self.rfp_testing_unwrap_scan_chars < 500:
            raise ValueError("RFP_TESTING_UNWRAP_SCAN_CHARS must be at least 500.")
        if not self.rfp_testing_enabled:
            return self
        addresses = {
            "RFP_TESTING_MAILBOX": self.rfp_testing_mailbox,
            "RFP_TESTING_SENDER": self.rfp_testing_sender,
            "RFP_TESTING_REDIRECT_TO": self.rfp_testing_redirect_to,
        }
        for name, value in addresses.items():
            if not _EMAIL_ADDRESS_RE.fullmatch((value or "").strip()):
                raise ValueError(
                    f"Refusing to boot: RFP_TESTING_ENABLED=true but {name} is "
                    f"{'empty' if not (value or '').strip() else 'not an email address'} "
                    f"(got {value!r})."
                )
        mailbox = self.rfp_testing_mailbox.strip().lower()
        if mailbox in self.rfp_email_ingestion_inboxes:
            raise ValueError(
                "Refusing to boot: RFP_TESTING_MAILBOX is also listed in "
                "RFP_EMAIL_INGESTION_INBOXES_ALLOWED; the test mailbox must be dedicated."
            )
        if mailbox == (self.email_ingest_mailbox or "").strip().lower():
            raise ValueError(
                "Refusing to boot: RFP_TESTING_MAILBOX equals EMAIL_INGEST_MAILBOX; "
                "the test mailbox must be dedicated."
            )
        return self

    @model_validator(mode="after")
    def _validate_auto_send(self) -> "Settings":
        """Refuse to boot on an Auto send schedule that could only misfire at
        run time: a time that is not one `HH:MM`, a preflight at or after the
        send, a digest before the send, a catch-up window that would run past
        midnight (auto send never sends on a later day), or a redirect that is
        not one address. Always enforced (the defaults satisfy every rule)."""
        pre_h, pre_m = _parse_hhmm(self.auto_send_preflight_time, "AUTO_SEND_PREFLIGHT_TIME")
        send_h, send_m = _parse_hhmm(self.auto_send_send_time, "AUTO_SEND_SEND_TIME")
        dig_h, dig_m = _parse_hhmm(self.auto_send_digest_time, "AUTO_SEND_DIGEST_TIME")
        preflight = pre_h * 60 + pre_m
        send = send_h * 60 + send_m
        digest = dig_h * 60 + dig_m
        if preflight >= send:
            raise ValueError("AUTO_SEND_PREFLIGHT_TIME must be before AUTO_SEND_SEND_TIME.")
        if digest < send:
            raise ValueError("AUTO_SEND_DIGEST_TIME must not be before AUTO_SEND_SEND_TIME.")
        if self.auto_send_catchup_minutes < 1 or send + self.auto_send_catchup_minutes > 24 * 60:
            raise ValueError(
                "AUTO_SEND_CATCHUP_MINUTES must be at least 1 and must end the catch-up "
                "window by midnight (auto send never sends on a later day)."
            )
        if self.auto_send_poll_interval_seconds < 5:
            raise ValueError("AUTO_SEND_POLL_INTERVAL_SECONDS must be at least 5.")
        if not 0 <= self.auto_send_missed_lookback_days <= 31:
            raise ValueError("AUTO_SEND_MISSED_LOOKBACK_DAYS must be between 0 and 31.")
        redirect = (self.auto_send_gc_redirect or "").strip()
        if redirect and not _EMAIL_ADDRESS_RE.fullmatch(redirect):
            raise ValueError("AUTO_SEND_GC_REDIRECT must be one email address (or empty).")
        return self

    @model_validator(mode="after")
    def _validate_rfp_match(self) -> "Settings":
        """Refuse to boot on a matcher configuration that could only fail at
        run time or quietly change what a stored score means: weights that
        cannot form an average, a threshold outside [0, 1], a review threshold
        above the auto threshold, date bands out of order, a rebid lookback
        inside the candidate window, or a sweep lease shorter than one LLM
        call can take. Always enforced (the defaults satisfy every rule) so a
        bad value surfaces at deploy time rather than on the first match."""
        if (
            self.rfp_match_weight_name < 0
            or self.rfp_match_weight_bid_date < 0
            or self.rfp_match_weight_bid_notes < 0
        ):
            raise ValueError("RFP_MATCH_WEIGHT_* values must be non-negative.")
        if self.rfp_match_weight_name + self.rfp_match_weight_bid_date <= 0:
            raise ValueError(
                "RFP_MATCH_WEIGHT_NAME plus RFP_MATCH_WEIGHT_BID_DATE must be positive "
                "(the scorer divides by their sum)."
            )
        unit = {
            "RFP_MATCH_NOTES_MIN": self.rfp_match_notes_min,
            "RFP_MATCH_DATE_SCORE_FAR": self.rfp_match_date_score_far,
            "RFP_MATCH_EXACT_TIME_BONUS": self.rfp_match_exact_time_bonus,
            "RFP_MATCH_CONFLICT_CAP": self.rfp_match_conflict_cap,
            "RFP_MATCH_AUTO_THRESHOLD": self.rfp_match_auto_threshold,
            "RFP_MATCH_REVIEW_THRESHOLD": self.rfp_match_review_threshold,
            "RFP_MATCH_NAME_MIN_AUTO": self.rfp_match_name_min_auto,
            "RFP_MATCH_NAME_MIN_AUTO_NO_DATE": self.rfp_match_name_min_auto_no_date,
            "RFP_MATCH_RUNNER_UP_GAP": self.rfp_match_runner_up_gap,
            "RFP_MATCH_LLM_CONFIDENCE_THRESHOLD": self.rfp_match_llm_confidence_threshold,
            "RFP_MATCH_GC_AUTO_THRESHOLD": self.rfp_match_gc_auto_threshold,
            "RFP_MATCH_REBID_NAME_THRESHOLD": self.rfp_match_rebid_name_threshold,
            "RFP_MATCH_PRECREATE_THRESHOLD": self.rfp_match_precreate_threshold,
        }
        for name, value in unit.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1 (got {value!r}).")
        if self.rfp_match_review_threshold > self.rfp_match_auto_threshold:
            raise ValueError(
                "RFP_MATCH_REVIEW_THRESHOLD must not exceed RFP_MATCH_AUTO_THRESHOLD "
                "(a candidate must reach review before it can be confident)."
            )
        if self.rfp_match_bid_date_tolerance_days < 0:
            raise ValueError("RFP_MATCH_BID_DATE_TOLERANCE_DAYS must not be negative.")
        if self.rfp_match_bid_date_far_days <= self.rfp_match_bid_date_tolerance_days:
            raise ValueError(
                "RFP_MATCH_BID_DATE_FAR_DAYS must be greater than "
                "RFP_MATCH_BID_DATE_TOLERANCE_DAYS (the date bands are ordered)."
            )
        if self.rfp_match_candidate_window_days <= 0:
            raise ValueError("RFP_MATCH_CANDIDATE_WINDOW_DAYS must be positive.")
        if self.rfp_match_rebid_lookback_days <= self.rfp_match_candidate_window_days:
            raise ValueError(
                "RFP_MATCH_REBID_LOOKBACK_DAYS must be greater than "
                "RFP_MATCH_CANDIDATE_WINDOW_DAYS (the rebid band is the older slice)."
            )
        if self.rfp_match_precreate_window_days < self.rfp_match_candidate_window_days:
            raise ValueError(
                "RFP_MATCH_PRECREATE_WINDOW_DAYS must be at least "
                "RFP_MATCH_CANDIDATE_WINDOW_DAYS (the New Bid check looks at least as far)."
            )
        if self.rfp_match_max_candidates < 1:
            raise ValueError("RFP_MATCH_MAX_CANDIDATES must be at least 1.")
        if self.rfp_match_sibling_window_minutes < 0:
            raise ValueError("RFP_MATCH_SIBLING_WINDOW_MINUTES must not be negative (0 disables).")
        if self.third_party_llm_timeout_seconds < 1:
            raise ValueError("THIRD_PARTY_LLM_TIMEOUT_SECONDS must be at least 1.")
        if self.third_party_llm_max_retries < 0:
            raise ValueError("THIRD_PARTY_LLM_MAX_RETRIES must not be negative.")
        # One call's worst case on either route: the self-hosted client never
        # retries; the third-party SDKs retry max_retries times on timeout.
        worst_call = max(
            self.self_hosted_llm_timeout_seconds,
            self.third_party_llm_timeout_seconds * (self.third_party_llm_max_retries + 1),
        )
        needed = self.llm_background_wait_seconds + worst_call
        if self.rfp_email_ingestion_lease_seconds < needed:
            if "rfp_email_ingestion_lease_seconds" in self.model_fields_set:
                raise ValueError(
                    "RFP_EMAIL_INGESTION_LEASE_SECONDS must be at least "
                    "LLM_BACKGROUND_WAIT_SECONDS plus the longest single LLM call "
                    "(the larger of SELF_HOSTED_LLM_TIMEOUT_SECONDS and "
                    "THIRD_PARTY_LLM_TIMEOUT_SECONDS x (THIRD_PARTY_LLM_MAX_RETRIES + 1)) "
                    f"({needed:g}) so the lease outlives one LLM call."
                )
            # Not configured: stretch the default to cover one call instead of
            # refusing to boot on a deployment with a long LLM timeout.
            # The invariant (lease >= one call) holds either way.
            self.rfp_email_ingestion_lease_seconds = int(math.ceil(needed))
        return self

    @property
    def llm_retry_delay_list(self) -> list[int]:
        """Parsed LLM_QUEUE_RETRY_DELAYS. Empty list = no automatic retries."""
        raw = self.llm_queue_retry_delays.strip()
        if not raw:
            return []
        return [int(part.strip()) for part in raw.split(",") if part.strip()]

    def _self_hosted_target_value(self, suffix: str) -> str:
        """SELF_HOSTED_LLM_<TARGET>_<suffix> for the live target. An unknown
        target (only possible while the master switch is off, where nothing
        validates it) resolves like `local`, matching the pre-ec2b behaviour."""
        target = self.self_hosted_llm_target
        if target not in SELF_HOSTED_LLM_TARGETS:
            target = "local"
        return getattr(self, f"self_hosted_llm_{target}_{suffix}") or ""

    @property
    def self_hosted_llm_base_url(self) -> str:
        """The live self-hosted endpoint (per SELF_HOSTED_LLM_TARGET). Includes
        the /v1 suffix, e.g. http://localhost:11434/v1."""
        return self._self_hosted_target_value("base_url").strip().rstrip("/")

    @property
    def self_hosted_llm_api_key(self) -> str:
        return self._self_hosted_target_value("api_key")

    @property
    def self_hosted_llm_target_model(self) -> str:
        """The model the live target serves (SELF_HOSTED_LLM_<TARGET>_MODEL);
        the default for every feature without an explicit self-hosted model."""
        return self._self_hosted_target_value("model").strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def gotenberg_base_url(self) -> str:
        # Render's `fromService hostport` yields a bare host:port - add a scheme.
        url = self.gotenberg_url.rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url

    @property
    def supabase_jwks_url(self) -> str:
        # Supabase exposes JWKS for asymmetric (RS256/ES256) verification.
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"

    # ── AI Estimating autonomous build (docs/AI_ESTIMATING_BUILD_SPEC.md 5.7) ─
    # Every PARENT flag (one that turns a behavior on) is off here; the few
    # CHILD settings that default true are inert while their parent is off,
    # so none of these changes prod behavior on deploy. Each line names its
    # owning workstream; the spec table is the source of truth.
    # WS1 page facts (text layer, schedules, legends, scales per page).
    plans_reader_page_facts_enabled: bool = False
    plans_reader_page_facts_in_subs: bool = True  # child of page_facts_enabled
    plans_reader_spec_select: bool = True  # child of page_facts_enabled
    plans_reader_facts_relabel: bool = False  # child of page_facts_enabled
    plans_reader_facts_title_vision: bool = False  # vision title fallback, deferred
    plans_reader_facts_page_budget_s: float = 4.0
    plans_reader_facts_doc_budget_s: float = 240.0
    plans_reader_facts_block_max_tokens: int = 2500
    plans_reader_ledger_legend_max_tokens: int = 6000  # < PLANS_READER_LEDGER_MAX_TOKENS
    plans_reader_ledger_schedule_max_tokens: int = 1500  # panels and luminaires, each
    plans_reader_ledger_scales_max_tokens: int = 300
    # WS2 routes: JSON object of step -> partial route merged over the code
    # defaults (plans_reader_route_map; empty = the defaults, all on OpenAI).
    plans_reader_routes: str = ""
    plans_reader_run_ceiling_factor: float = 1.25
    # Inert unless a call runs on an Anthropic route (no default route does).
    anthropic_plans_refusal_fallback: bool = True
    # WS3 derive and measure conventions.
    plans_reader_derive_enabled: bool = False
    plans_reader_branch_conduit: str = '3/4"C EMT'
    plans_reader_min_branch_awg: int = 12
    plans_reader_vd_max_pct: float = 3.0
    plans_reader_route_factor: float = 1.00
    plans_reader_drop_ft_per_device: float = 0.0
    plans_reader_panel_rise_ft: float = 0.0
    plans_reader_existing_tie_in_ft: float = 25.0
    plans_reader_homerun_default_ft: float = 150.0
    plans_reader_floor_to_floor_ft: float = 15.0
    plans_reader_receptacles_per_circuit: int = 6
    plans_reader_control_cable_ft_per_device: float = 25.0
    plans_reader_measure_conventions_version: str = "g3-branch-2026-10-a"
    plans_reader_measure_assist_enabled: bool = False
    plans_reader_measure_assist_max_items: int = 8  # child of measure_assist_enabled
    plans_reader_measure_assist_max_usd: float = 0.75  # child, per run
    # WS4 consolidation, regions and integrity gates.
    plans_reader_consolidator_mode: str = "legacy"  # tools | legacy
    plans_reader_consolidator_max_rounds: int = 20
    plans_reader_consolidator_max_tool_calls: int = 400
    plans_reader_consolidator_max_usd: float = 6.00
    plans_reader_llm_rollup: bool = False
    plans_reader_code_regions: bool = False
    plans_reader_region_pad: float = 0.03  # child of code_regions, page fraction
    plans_reader_cluster_gap: float = 0.06  # child of code_regions, page fraction
    plans_reader_census_tol_strong: float = 0.05  # applied with a minimum of 1
    plans_reader_census_tol_weak: float = 0.10  # applied with a minimum of 1
    plans_reader_gates_blocking: bool = False
    plans_reader_demo_two_line_style: bool = False
    # WS7 blind recount and independent review.
    plans_reader_recount_enabled: bool = False
    plans_reader_recount_max_targets: int = 12
    plans_reader_recount_max_sheets: int = 6
    plans_reader_recount_budget_usd: float = 2.00
    plans_reader_review_mode: str = "off"  # off | shadow | apply
    plans_reader_review_budget_usd: float = 4.00  # at most 6.00
    plans_reader_review_local_rules_fallback: bool = False
    # WS5 scope policy and bid conditions.
    ai_scope_policy_enforce: bool = False
    ai_scope_attrib_llm: bool = False
    ai_scope_attrib_max_statements: int = 40
    ai_scope_digest_max_tokens: int = 3500
    # WS6 pricing. AI_PRICING_STATE decides who sees prices
    # (core/features.ai_pricing_allowed): test = Executive and IT Admin,
    # live = every pricing viewer (never the accountant or the estimator).
    ai_pricing_enabled: bool = False
    ai_pricing_state: str = "test"  # test | live
    ai_pricing_autorun: bool = False
    ai_pricing_chat_tools_enabled: bool = False
    ai_pricing_agent_tools_enabled: bool = False
    quote_lines_llm_fallback_enabled: bool = False
    # WS8 evidence and deliverables.
    plans_reader_marked_plans_mode: str = "model"  # code | model
    plans_reader_annotated_plans_enabled: bool = False
    # WS0 eval harness, live spend ledger and cassettes. The ledger, the
    # cassette mode, the sweep id and the claim filter are per process: set
    # them on the sweep backend and live scripts only, never in a shared .env.
    eval_root: str = "/Users/thomasmoore/Desktop/BDR/backups/eval"
    ai_eval_ledger_enforce: bool = False
    llm_cassette_mode: str = "off"  # off | record | replay | record_missing
    llm_cassette_dir: str = ""
    eval_sweep_id: str = ""  # stamped into llm_call_log by llm_gate.log_call
    # Comma list of llm_jobs job types this worker may claim; empty = all.
    llm_queue_claim_types: str = ""

    @property
    def plans_reader_route_map(self) -> dict[str, dict]:
        """PLANS_READER_ROUTES merged over PLANS_READER_DEFAULT_ROUTES: one
        {provider, model, effort, budget_usd, tools} dict per step, a fresh
        copy per call. A null effort inherits PLANS_READER_REASONING_EFFORT."""
        routes = _parse_plans_reader_routes(self.plans_reader_routes)
        for route in routes.values():
            if route["effort"] is None:
                route["effort"] = self.plans_reader_reasoning_effort
        return routes

    @property
    def llm_queue_claim_job_types(self) -> tuple[str, ...]:
        """Parsed LLM_QUEUE_CLAIM_TYPES. An empty tuple means every type."""
        return _parse_claim_types(self.llm_queue_claim_types)

    @model_validator(mode="after")
    def _validate_ai_estimating_build(self) -> "Settings":
        """The section 5.7 knobs: a typo'd mode, a malformed route table or a
        budget past its cap refuses to boot instead of failing a run later.
        Always enforced (the defaults satisfy every rule)."""
        modes = (
            ("plans_reader_consolidator_mode", PLANS_READER_CONSOLIDATOR_MODES),
            ("plans_reader_review_mode", PLANS_READER_REVIEW_MODES),
            ("plans_reader_marked_plans_mode", PLANS_READER_MARKED_PLANS_MODES),
            ("ai_pricing_state", AI_PRICING_STATES),
            ("llm_cassette_mode", LLM_CASSETTE_MODES),
        )
        for field, allowed in modes:
            value = (getattr(self, field) or "").strip().lower()
            if value not in allowed:
                raise ValueError(
                    f"{field.upper()} must be one of {', '.join(allowed)} "
                    f"(got {getattr(self, field)!r})."
                )
            setattr(self, field, value)
        # WS1 page facts budgets.
        if not 0 < self.plans_reader_facts_page_budget_s <= self.plans_reader_facts_doc_budget_s:
            raise ValueError(
                "PLANS_READER_FACTS_PAGE_BUDGET_S must be positive and at most "
                "PLANS_READER_FACTS_DOC_BUDGET_S."
            )
        if not 100 <= self.plans_reader_facts_block_max_tokens <= self.plans_reader_group_input_tokens:
            raise ValueError(
                "PLANS_READER_FACTS_BLOCK_MAX_TOKENS must be between 100 and "
                "PLANS_READER_GROUP_INPUT_TOKENS."
            )
        if not 0 < self.plans_reader_ledger_legend_max_tokens < self.plans_reader_ledger_max_tokens:
            raise ValueError(
                "PLANS_READER_LEDGER_LEGEND_MAX_TOKENS must be positive and below "
                "PLANS_READER_LEDGER_MAX_TOKENS (the legends ride inside the ledger)."
            )
        for name, value in (
            ("PLANS_READER_LEDGER_SCHEDULE_MAX_TOKENS", self.plans_reader_ledger_schedule_max_tokens),
            ("PLANS_READER_LEDGER_SCALES_MAX_TOKENS", self.plans_reader_ledger_scales_max_tokens),
        ):
            if not 0 <= value < self.plans_reader_ledger_max_tokens:
                raise ValueError(
                    f"{name} must be between 0 and PLANS_READER_LEDGER_MAX_TOKENS (exclusive)."
                )
        # WS2 routes and the run ceiling.
        try:
            parsed_routes = _parse_plans_reader_routes(self.plans_reader_routes)
        except ValueError as exc:
            raise ValueError(f"PLANS_READER_ROUTES is invalid: {exc}") from exc
        # Steps still served by llm.complete_parts (OpenAI only) cannot route
        # to Anthropic until complete_turn serves them: refuse at boot rather
        # than fail every call of a run.
        parts_steps = list(PLANS_READER_OPENAI_ONLY_STEPS)
        if self.plans_reader_consolidator_mode == "legacy":
            parts_steps.append("main")
        anthropic_steps = [step for step in parts_steps if parsed_routes[step]["provider"] == "anthropic"]
        if anthropic_steps:
            raise ValueError(
                "PLANS_READER_ROUTES is invalid: step(s) "
                f"{', '.join(anthropic_steps)} cannot use provider 'anthropic' yet (they run on "
                "the OpenAI-only complete_parts transport"
                + ("; main does while PLANS_READER_CONSOLIDATOR_MODE is legacy" if "main" in anthropic_steps else "")
                + ")."
            )
        if not 1.0 <= self.plans_reader_run_ceiling_factor <= 5.0:
            raise ValueError("PLANS_READER_RUN_CEILING_FACTOR must be between 1 and 5.")
        # WS3 conventions.
        if not (self.plans_reader_branch_conduit or "").strip():
            raise ValueError("PLANS_READER_BRANCH_CONDUIT must not be empty.")
        if not (self.plans_reader_measure_conventions_version or "").strip():
            raise ValueError("PLANS_READER_MEASURE_CONVENTIONS_VERSION must not be empty.")
        if self.plans_reader_min_branch_awg not in (14, 12, 10, 8, 6, 4):
            raise ValueError("PLANS_READER_MIN_BRANCH_AWG must be one of 14, 12, 10, 8, 6, 4.")
        if not 0 < self.plans_reader_vd_max_pct <= 10:
            raise ValueError("PLANS_READER_VD_MAX_PCT must be above 0 and at most 10.")
        if not 0.5 <= self.plans_reader_route_factor <= 2.0:
            raise ValueError("PLANS_READER_ROUTE_FACTOR must be between 0.5 and 2.")
        for name, value in (
            ("PLANS_READER_DROP_FT_PER_DEVICE", self.plans_reader_drop_ft_per_device),
            ("PLANS_READER_PANEL_RISE_FT", self.plans_reader_panel_rise_ft),
            ("PLANS_READER_EXISTING_TIE_IN_FT", self.plans_reader_existing_tie_in_ft),
            ("PLANS_READER_CONTROL_CABLE_FT_PER_DEVICE", self.plans_reader_control_cable_ft_per_device),
            ("PLANS_READER_MEASURE_ASSIST_MAX_USD", self.plans_reader_measure_assist_max_usd),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative.")
        if self.plans_reader_homerun_default_ft <= 0 or self.plans_reader_floor_to_floor_ft <= 0:
            raise ValueError(
                "PLANS_READER_HOMERUN_DEFAULT_FT and PLANS_READER_FLOOR_TO_FLOOR_FT must be positive."
            )
        if not 1 <= self.plans_reader_receptacles_per_circuit <= 20:
            raise ValueError("PLANS_READER_RECEPTACLES_PER_CIRCUIT must be between 1 and 20.")
        if not 0 <= self.plans_reader_measure_assist_max_items <= 100:
            raise ValueError("PLANS_READER_MEASURE_ASSIST_MAX_ITEMS must be between 0 and 100.")
        # WS4 consolidator, regions and census tolerances.
        if not 1 <= self.plans_reader_consolidator_max_rounds <= 100:
            raise ValueError("PLANS_READER_CONSOLIDATOR_MAX_ROUNDS must be between 1 and 100.")
        if not 1 <= self.plans_reader_consolidator_max_tool_calls <= 5000:
            raise ValueError("PLANS_READER_CONSOLIDATOR_MAX_TOOL_CALLS must be between 1 and 5000.")
        if self.plans_reader_consolidator_max_usd <= 0:
            raise ValueError("PLANS_READER_CONSOLIDATOR_MAX_USD must be positive.")
        if not 0 <= self.plans_reader_region_pad <= 0.5:
            raise ValueError("PLANS_READER_REGION_PAD must be a page fraction between 0 and 0.5.")
        if not 0 < self.plans_reader_cluster_gap <= 0.5:
            raise ValueError(
                "PLANS_READER_CLUSTER_GAP must be a page fraction above 0 and at most 0.5."
            )
        if not 0 <= self.plans_reader_census_tol_strong <= self.plans_reader_census_tol_weak <= 1:
            raise ValueError(
                "PLANS_READER_CENSUS_TOL_STRONG and PLANS_READER_CENSUS_TOL_WEAK must be "
                "fractions between 0 and 1 with STRONG at most WEAK."
            )
        # WS7 recount and review.
        if self.plans_reader_recount_max_targets < 1 or self.plans_reader_recount_max_sheets < 1:
            raise ValueError(
                "PLANS_READER_RECOUNT_MAX_TARGETS and PLANS_READER_RECOUNT_MAX_SHEETS "
                "must be at least 1."
            )
        if self.plans_reader_recount_budget_usd <= 0:
            raise ValueError("PLANS_READER_RECOUNT_BUDGET_USD must be positive.")
        if not 0 < self.plans_reader_review_budget_usd <= PLANS_READER_REVIEW_BUDGET_MAX_USD:
            raise ValueError(
                "PLANS_READER_REVIEW_BUDGET_USD must be above 0 and at most "
                f"{PLANS_READER_REVIEW_BUDGET_MAX_USD:.2f}."
            )
        # WS5 scope policy.
        if self.ai_scope_attrib_max_statements < 1:
            raise ValueError("AI_SCOPE_ATTRIB_MAX_STATEMENTS must be at least 1.")
        if self.ai_scope_digest_max_tokens < 100:
            raise ValueError("AI_SCOPE_DIGEST_MAX_TOKENS must be at least 100.")
        # WS0 eval harness.
        if self.llm_cassette_mode != "off" and not (self.llm_cassette_dir or "").strip():
            raise ValueError("LLM_CASSETTE_DIR must be set when LLM_CASSETTE_MODE is not off.")
        sweep = (self.eval_sweep_id or "").strip()
        if sweep:
            try:
                sweep = str(uuid.UUID(sweep))
            except ValueError as exc:
                raise ValueError("EVAL_SWEEP_ID must be empty or one uuid.") from exc
        self.eval_sweep_id = sweep
        try:
            _parse_claim_types(self.llm_queue_claim_types)
        except ValueError as exc:
            raise ValueError(f"LLM_QUEUE_CLAIM_TYPES is invalid: {exc}") from exc
        return self


# ── AI Estimating autonomous build: section 5.7 vocabularies and parsers ────
PLANS_READER_CONSOLIDATOR_MODES: tuple[str, ...] = ("tools", "legacy")
PLANS_READER_REVIEW_MODES: tuple[str, ...] = ("off", "shadow", "apply")
PLANS_READER_MARKED_PLANS_MODES: tuple[str, ...] = ("code", "model")
AI_PRICING_STATES: tuple[str, ...] = ("test", "live")
LLM_CASSETTE_MODES: tuple[str, ...] = ("off", "record", "replay", "record_missing")
PLANS_READER_REVIEW_BUDGET_MAX_USD = 6.00
PLANS_READER_ROUTE_PROVIDERS: tuple[str, ...] = ("openai", "anthropic")
PLANS_READER_ROUTE_FIELDS: tuple[str, ...] = ("provider", "model", "effort", "budget_usd", "tools")
# Steps that run on llm.complete_parts (OpenAI only) in this build; main joins
# them while PLANS_READER_CONSOLIDATOR_MODE is legacy.
PLANS_READER_OPENAI_ONLY_STEPS: tuple[str, ...] = ("sub", "escalation", "chat")

# Section 8.3 default routes, every one on OpenAI by owner decision
# (2026-10-21: all live spend on OpenAI). Sub and escalation stay single calls
# with no tools; a budget of None means the step is capped by its own setting
# or the run estimate; an effort of None inherits PLANS_READER_REASONING_EFFORT.
PLANS_READER_DEFAULT_ROUTES: dict[str, dict] = {
    "sub": {"provider": "openai", "model": "gpt-6-astra", "effort": "medium",
            "budget_usd": None, "tools": False},
    "escalation": {"provider": "openai", "model": "gpt-6-astra", "effort": "medium",
                   "budget_usd": None, "tools": False},
    "recount": {"provider": "openai", "model": "gpt-6-astra", "effort": "medium",
                "budget_usd": 2.00, "tools": True},
    "main": {"provider": "openai", "model": "gpt-6-astra", "effort": "medium",
             "budget_usd": 6.00, "tools": True},
    "review": {"provider": "openai", "model": "gpt-6-astra", "effort": "medium",
               "budget_usd": 4.00, "tools": True},
    "revise": {"provider": "openai", "model": "gpt-6-astra", "effort": "medium",
               "budget_usd": None, "tools": True},
    # Provider and model are derived from the main route unless
    # PLANS_READER_ROUTES names scope_attrib (spec 8.3; see
    # plans_reader_model_routes.DERIVED_FROM); this row's model is only the
    # fallback shape. Effort stays low.
    "scope_attrib": {"provider": "openai", "model": "gpt-6-astra", "effort": "low",
                     "budget_usd": None, "tools": False},
    "quote": {"provider": "openai", "model": "gpt-6-astra", "effort": None,
              "budget_usd": None, "tools": False},
    "derive": {"provider": "openai", "model": "gpt-6-astra", "effort": None,
               "budget_usd": 0.75, "tools": True},
    "chat": {"provider": "openai", "model": "gpt-6-astra", "effort": None,
             "budget_usd": None, "tools": True},
}

_CLAIM_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _parse_plans_reader_routes(raw: str) -> dict[str, dict]:
    """PLANS_READER_ROUTES as {step: {provider, model, effort, budget_usd,
    tools}}. Empty = a deep copy of PLANS_READER_DEFAULT_ROUTES; otherwise a
    JSON object of step -> partial route whose fields replace the default's.
    An unknown step or field, a bad provider, an empty model, an effort
    outside PLANS_READER_REASONING_EFFORTS, a non-positive budget or a
    non-boolean tools flag raises a ValueError naming it."""
    routes = {step: dict(route) for step, route in PLANS_READER_DEFAULT_ROUTES.items()}
    text = (raw or "").strip()
    if not text:
        return routes
    try:
        overrides = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from exc
    if not isinstance(overrides, dict):
        raise ValueError("must be a JSON object of step -> route")
    for step, override in overrides.items():
        if step not in routes:
            raise ValueError(
                f"unknown step {step!r} (steps: {', '.join(PLANS_READER_DEFAULT_ROUTES)})"
            )
        if not isinstance(override, dict):
            raise ValueError(f"step {step!r} must be an object")
        unknown = sorted(set(override) - set(PLANS_READER_ROUTE_FIELDS))
        if unknown:
            raise ValueError(f"step {step!r} has unknown fields {', '.join(unknown)}")
        routes[step].update(override)
    for step, route in routes.items():
        if route["provider"] not in PLANS_READER_ROUTE_PROVIDERS:
            raise ValueError(
                f"step {step!r} provider must be one of {', '.join(PLANS_READER_ROUTE_PROVIDERS)}"
            )
        if not isinstance(route["model"], str) or not route["model"].strip():
            raise ValueError(f"step {step!r} model must be a non-empty string")
        route["model"] = route["model"].strip()
        effort = route["effort"]
        if effort is not None:
            if not isinstance(effort, str) or effort.strip().lower() not in PLANS_READER_REASONING_EFFORTS:
                raise ValueError(
                    f"step {step!r} effort must be null or one of "
                    f"{', '.join(PLANS_READER_REASONING_EFFORTS)}"
                )
            route["effort"] = effort.strip().lower()
        budget = route["budget_usd"]
        if budget is not None and (
            isinstance(budget, bool) or not isinstance(budget, (int, float)) or not budget > 0
        ):
            raise ValueError(f"step {step!r} budget_usd must be null or a positive number")
        if not isinstance(route["tools"], bool):
            raise ValueError(f"step {step!r} tools must be true or false")
    return routes


def _parse_claim_types(raw: str) -> tuple[str, ...]:
    """LLM_QUEUE_CLAIM_TYPES ("plans_reader_agent,ai_pricing") as a tuple of
    job type names, de-duplicated in order. Empty = every type. A name that
    is not lower-case snake case raises a ValueError naming it."""
    out: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if not _CLAIM_TYPE_RE.fullmatch(name):
            raise ValueError(f"{part.strip()!r} is not a job type name")
        if name not in out:
            out.append(name)
    return tuple(out)


@lru_cache
def get_settings() -> Settings:
    return Settings()
