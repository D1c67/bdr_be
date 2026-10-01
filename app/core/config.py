"""Application configuration loaded from environment / .env."""

import logging
import math
import re
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


# Which self-hosted endpoint is live (SELF_HOSTED_LLM_TARGET). Each target is a
# base URL + API key + served-model triple: SELF_HOSTED_LLM_<TARGET>_{BASE_URL,
# API_KEY,MODEL}. Adding a box = a new entry here plus its three fields.
SELF_HOSTED_LLM_TARGETS: tuple[str, ...] = ("local", "ec2", "ec2b")


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
    # Each target also names the model it serves (SELF_HOSTED_LLM_<TARGET>_MODEL);
    # every feature whose SELF_HOSTED_<FEATURE>_MODEL is empty uses that, so the
    # per-feature lines only need touching to override or switch a feature off.
    self_hosted_llm_target: str = "local"       # local | ec2 | ec2b
    self_hosted_llm_local_base_url: str = ""    # e.g. http://localhost:11434/v1
    self_hosted_llm_local_api_key: str = ""
    self_hosted_llm_local_model: str = ""
    self_hosted_llm_ec2_base_url: str = ""      # e.g. https://llm.internal.example.com/v1
    self_hosted_llm_ec2_api_key: str = ""
    self_hosted_llm_ec2_model: str = ""
    self_hosted_llm_ec2b_base_url: str = ""
    self_hosted_llm_ec2b_api_key: str = ""
    self_hosted_llm_ec2b_model: str = ""
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
    # Identification round 3 (LLM subject-only match).
    openai_email_match_model: str = "gpt-5.4-mini"
    email_match_confidence_threshold: float = 0.85   # assign at/above; below → Unknown + suggestion
    email_match_max_attempts: int = 5                # transient-failure retries before 'failed'
    email_match_llm_max_candidates: int = 300        # prefilter cap bounding the R3 prompt
    email_llm_outage_retry_seconds: int = 3600       # wait when the provider is out of credits
    # New-project rescan of the Unknown pool (learn-back on project creation).
    email_rescan_llm_days: int = 14             # only LLM-confirm unknowns this recent
    email_rescan_llm_max: int = 50              # LLM call cap per project creation

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
    rfp_match_candidate_window_days: int = 30        # bid date not older than this
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
        one harvester (Procore with credentials, PipelineSuite, or the email
        harvester; the last two need no credentials)."""
        return (
            self.rfp_ingest_enabled
            and self.rfp_harvest_enabled
            and (
                self.procore_configured
                or self.pipelinesuite_enabled
                or self.rfp_harvest_email_enabled
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
