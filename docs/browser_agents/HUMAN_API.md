# Browser agents: human API contract (C2, frozen 2026-10-19)

This is the exact list of routes the BDR web app calls for the browser agent
platform. The backend lane implements exactly these paths and shapes in
`bdr_be/app/routers/browser_agents.py`; the frontend lane types them in
`bdr_fe/lib/browserAgents.ts` (already written to match this file). The
worker protocol (C1) is a different router and lives in
`app/services/browser_agents/protocol.py`.

Spec: `SPEC.md` sections 7 and 12.0 C2, adjusted by `SPEC_OVERRIDES.md`:
- O1: no LLM escalation toggle; an enabled type always has a provider, model and prices.
- O2: no browser-login fallback toggle or daily release cap.
- O4: caps live on global settings (`daily_cost_cap_usd` $25, `per_job_cost_cap_usd` $2) and IT Admin can reset the daily window.
- O5: no provider attestations (no `/admin/attestations`, no `attestation_missing`).

## Conventions

- Prefix: `/browser-agents`. All handlers are plain `def`. Every response sets `Cache-Control: no-store`.
- Router dependency: a bare 404 when `settings.browser_agents_active` is false. `GET /features` gains `browser_agents: boolean`.
- Auth: the normal user JWT (`get_current_user`). A `bdrw_` worker token is never accepted here.
- Roles (`core/roles.py`):

| Set | Roles | Used for |
|---|---|---|
| `BROWSER_AGENT_VIEW_ROLES` | `estimating_admin`, `it_admin`, `executive` | every GET under `/summary`, `/jobs`, `/help`, `/artifacts` |
| `BROWSER_AGENT_HELPER_ROLES` | `estimating_admin`, `it_admin` | help writes, job cancel, job retry |
| `BROWSER_AGENT_ADMIN_ROLES` | `it_admin`, or any profile with `is_dev` | everything under `/admin` |

- `llm_request` / `llm_response` artifact URLs are admin only.
- Times are ISO 8601 UTC strings; money is a JSON number in USD; ids are uuid strings except message and change ids (integers).
- Lists page with an opaque `cursor`; the response carries `next_cursor` (null at the end). Default `limit` 50, max 200.
- Errors: FastAPI `{detail}` with an `X-Error-Code` header (exposed via CORS) for the codes listed at the end.
- Every human write writes an `audit_log` row (`browser_agent.settings_update`, `.secret_set`, `.secret_cleared`, `.worker_enrolled`, `.worker_approved`, `.worker_revoked`, `.help_reply`, `.help_claim`, `.job_cancel`, `.job_retry`, `.spend_reset`, `.canary`, `.stop_running`). Settings writes go through the `browser_agent_update_settings` RPC; the spend reset through `browser_agent_reset_spend_window`.

## Shared types

```ts
type JobStatus = "queued" | "running" | "waiting_help" | "applying" | "succeeded" | "failed" | "canceled" | "expired";
type JobOutcome = "pending" | "applied" | "apply_failed" | "nothing_to_do" | "already_current"
                | "skipped_stale" | "no_link" | "self_test_ok";
type HelpStatus = "open" | "answered" | "resolved" | "expired" | "canceled";
type HelpReasonCode = "login_failed" | "login_locked" | "captcha" | "mfa" | "sso" | "logged_out" | "access_denied"
                    | "not_found" | "ambiguous" | "unexpected_page" | "cap_reached" | "needs_info"
                    | "provider_config" | "other";
type HelpAction = "message" | "retry" | "give_up" | "skip_daily";
type Provider = "openai" | "anthropic" | "meta" | "google" | "xai";
type StepActor = "worker" | "playbook" | "model" | "bdr" | "helper" | "admin" | "system";

Summary {
  enabled: boolean;                 // global kill switch
  open_help: number;                // help requests in open|answered
  waiting_jobs: number;             // jobs in waiting_help
  running_jobs: number;
  queued_jobs: number;
  worker_online: boolean;           // any active worker seen within 2 heartbeats (30 s)
  workers_online: number;
  spend_today_usd: number;
  daily_cap_usd: number;
  per_job_cap_usd: number;
}

JobRow {
  id; agent_type: string; status: JobStatus; outcome: JobOutcome | null;
  project_id: string | null; project_name: string | null;
  rfi_label: string | null;         // "RFI Const-198" | "RFI 27914290" | "Locate RFI #198" | null (canary)
  attempts: number; interrupted_count: number;
  created_at; finished_at: string | null;
  waiting_help_id: string | null;
  cost_usd: number;
}

JobDetail = JobRow & {
  input_summary: { platform: string | null; host: string | null; external_project_id: string | null;
                   rfi_id: string | null; rfi_number_hint: string | null; event_hint: string | null;
                   email_message_at: string | null };   // never the email body
  config_snapshot: { provider: Provider | null; model: string | null; prompt_version: string | null;
                     prompt_sha: string | null; per_job_cap_usd: number | null } | null;
  lease_epoch: number; tokens_in: number; tokens_out: number; step_count: number; turn_count: number;
  active_seconds: number; sync_run_id: string | null; error_kind: string | null; error: string | null;
  source_email_id: string | null; source_email_subject: string | null;
  checkpoint_phase: string | null;
  cancel_requested_at: string | null;
  can_retry: boolean;               // for the caller: false when a helper already used the one cap_reached retry and the caller is not IT Admin
}

Step {
  seq: number; epoch: number; late: boolean; kind: string; actor: StepActor; actor_label: string | null;
  summary: string; tool_name: string | null; args: object | null; result: object | null;
  url_before: string | null; url_after: string | null; screenshot_artifact_id: string | null;
  turn_no: number | null; ok: boolean | null; duration_ms: number | null; at: string;
  redacted_at: string | null;
}

Turn {
  turn_no: number; epoch: number; status: "thinking" | "issued" | "failed" | "abandoned";
  assistant_text: string | null; action: { call_id: string; tool: string; args: object } | null;
  provider: string | null; model: string | null; input_tokens: number | null; output_tokens: number | null;
  cost_usd: number | null; billing_unknown: boolean; error_kind: string | null; created_at: string;
}

HelpRow {
  id; scope_key: string; reason_code: HelpReasonCode; title: string; status: HelpStatus;
  project_id: string | null; project_name: string | null;
  claimed_by_label: string | null; waiting_jobs: number;
  created_at; expires_at;
}

HelpFile { artifact_id; filename: string; size_bytes: number; mime_type: string; attachable: boolean; }

HelpMessage {
  id: number; author_kind: "agent" | "helper" | "system"; author_label: string | null;
  action: HelpAction; body: string | null;          // null for secrets
  is_secret: boolean; secret_consumed: boolean;      // the secret value is never returned
  files: HelpFile[]; created_at: string;
}

HelpDetail = HelpRow & {
  agent_message: string | null;                      // untrusted; render escaped, collapsed
  screenshot_artifact_id: string | null;
  is_login_scope: boolean;                           // secret checkbox shown only when true
  claimed_by_me: boolean;
  waiters: { job_id: string; rfi_label: string | null; status: JobStatus; project_name: string | null }[];
  messages: HelpMessage[];
}

GlobalSettings {
  enabled: boolean; config_version: number;
  daily_cost_cap_usd: number; per_job_cost_cap_usd: number;
  help_hold_seconds: number; help_reminder_minutes: number; help_claim_lapse_minutes: number;
  help_expire_hours: number; worker_offline_alert_minutes: number; worker_offline_email_hours: number;
  job_ttl_hours: number; desired_worker_version: string | null;
  updated_by_label: string | null; updated_at: string;
}

Caps { daily_cap_usd: number; per_job_cap_usd: number; window_started_at: string;
       spend_today_usd: number; window_reset_by_label: string | null; }

TypeCfg {
  key: string; label: string; enabled: boolean; identity: string | null;
  provider: Provider | null; model: string | null;
  price_in_per_mtok: number | null; price_out_per_mtok: number | null; model_probe_ok_at: string | null;
  max_concurrency: number; max_attempts: number; max_interruptions: number; max_steps: number;
  max_total_tokens: number; max_active_seconds: number; host_narrowing: string[] | null;
  prompt_version: string; priority_default: number; updated_at: string;
}

Worker {
  id; name: string; status: "pending" | "active" | "revoked"; online: boolean; draining: boolean;
  max_slots: number; allowed_types: string[]; allowed_ips: string[];
  version: string | null; bad_versions: string[]; hostname: string | null; chromium_version: string | null;
  chromium_stale: boolean;          // Chromium build older than 30 days
  boot_at: string | null; last_seen_at: string | null; last_ip: string | null; enroll_ip: string | null;
  running_jobs: number; token_prefix: string | null;
  approved_at: string | null; approved_by_label: string | null; revoked_at: string | null; created_at: string;
}

SecretRow { name: string; set: boolean; set_at: string | null; set_by_label: string | null; fingerprint: string | null; }

ProcoreLoginState { email: string | null; locked_until: string | null; login_failures: number;
                    last_login_source: string | null; last_error: string | null; logged_in_at: string | null; }

Release { version: string; min_version: string; sha256: string; chromium_version: string | null;
          notes: string | null; created_at: string; }

ProviderInfo { provider: Provider; key_configured: boolean; experimental: boolean; }

AdminState {
  settings: GlobalSettings;
  runtime: { spend_today_usd: number; window_started_at: string;
             half_cap_notified_at: string | null; cap_hit_notified_at: string | null };
  caps: Caps;
  types: TypeCfg[];
  workers: Worker[];
  secrets: SecretRow[];
  procore: ProcoreLoginState;
  releases: Release[];
  providers: ProviderInfo[];
  ceilings: { [agent_type: string]: string[] };   // nav hosts from protocol.CEILINGS; host_narrowing must be a subset
  pm_rfi_sync_active: boolean;                     // rfi_from_email cannot be enabled while false
}

SettingsChange { id: number; scope: "global" | "agent_type" | "secret" | "worker" | "release" | "help" | "spend";
                 target: string | null; field: string; old_value: unknown; new_value: unknown;
                 probe: object | null; changed_by_label: string; changed_at: string; }
```

Secret values never appear in any response: secrets are `{set, fingerprint}`, helper secrets are `is_secret` plus `secret_consumed`. Tests inspect serialized responses for this.

## Routes

### Summary (view roles)

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/browser-agents/summary` | none | `Summary` |

### Jobs

| Method | Path | Roles | Request | Response |
|---|---|---|---|---|
| GET | `/browser-agents/jobs` | view | query `status` (JobStatus, or `needs_help` = waiting_help, or `finished` = succeeded+canceled+expired), `type`, `project_id`, `cursor`, `limit` | `{items: JobRow[], next_cursor: string \| null}` newest first |
| GET | `/browser-agents/jobs/{id}` | view | none | `JobDetail` |
| GET | `/browser-agents/jobs/{id}/steps` | view | query `after_seq` (default 0), `limit` (default 200, max 500) | `{items: Step[], next_after_seq: number \| null}` ascending by seq |
| GET | `/browser-agents/jobs/{id}/turns` | view | none | `{items: Turn[]}` ascending by turn_no |
| POST | `/browser-agents/jobs/{id}/cancel` | helper | none | `JobDetail` (queued: canceled at once; running/waiting_help: `cancel_requested_at` set, delivered by the heartbeat) |
| POST | `/browser-agents/jobs/{id}/retry` | helper | none | `JobDetail` (failed/canceled/expired, or waiting_help on cap_reached: requeued with `resume_reason='manual'`, priority 10, uncharged; a cap_reached retry resets the counters once for a helper, after that IT Admin only) |

### Artifacts

| Method | Path | Roles | Request | Response |
|---|---|---|---|---|
| GET | `/browser-agents/artifacts/{id}/url` | view; admin for `llm_request`/`llm_response` | none | `{url: string, filename: string \| null, mime_type: string, expires_in: number}` (signed storage URL, 300 s) |

### Help

| Method | Path | Roles | Request | Response |
|---|---|---|---|---|
| GET | `/browser-agents/help` | view | query `status` (HelpStatus or `active` = open+answered), `cursor`, `limit` | `{items: HelpRow[], next_cursor}` |
| GET | `/browser-agents/help/{id}` | view | none | `HelpDetail` |
| GET | `/browser-agents/help/{id}/messages` | view | query `after_id` | `{items: HelpMessage[], status: HelpStatus, claimed_by_label: string \| null}` (2 s polling) |
| POST | `/browser-agents/help/{id}/messages` | helper | `{client_msg_id: uuid, action: HelpAction, body?: string<=8000, is_secret?: boolean, secret_id?: uuid, secret_value?: string<=64, artifact_ids?: uuid[]}` | `HelpMessage` (idempotent on `client_msg_id`). A secret needs `is_secret`, `secret_id`, `secret_value`, no `body`, and a login scope; it is encrypted before the single insert. `retry`, `give_up`, `skip_daily` act on every waiter. |
| POST | `/browser-agents/help/{id}/files` | helper | multipart: `file`, `attachable` ("true"/"false") | `HelpFile` (150 MB cap, magic-byte sniff; pdf, png, jpg, docx, xlsx, txt may be attachable; zip and dwg stay in the thread with `attachable=false`) |
| POST | `/browser-agents/help/{id}/claim` | helper | none | `HelpDetail` ("I'm on it"; lapses after `help_claim_lapse_minutes` without activity) |
| POST | `/browser-agents/help/{id}/unclaim` | helper | none | `HelpDetail` |

### Admin (IT Admin or dev)

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/browser-agents/admin/state` | none | `AdminState` |
| PATCH | `/browser-agents/admin/settings` | partial `GlobalSettings` writable fields: `enabled`, `daily_cost_cap_usd` (0..1000), `per_job_cost_cap_usd` (0..50), `help_hold_seconds`, `help_reminder_minutes`, `help_claim_lapse_minutes`, `help_expire_hours`, `worker_offline_alert_minutes`, `worker_offline_email_hours`, `job_ttl_hours`, `desired_worker_version` | `AdminState` |
| POST | `/browser-agents/admin/spend/reset` | `{note?: string<=500}` | `Caps` (O4; audited, re-arms the 50% and 100% alerts) |
| PATCH | `/browser-agents/admin/types/{key}` | partial `TypeCfg` writable fields: `label`, `enabled`, `provider`, `model`, `price_in_per_mtok`, `price_out_per_mtok`, `max_concurrency`, `max_attempts`, `max_interruptions`, `max_steps`, `max_total_tokens`, `max_active_seconds`, `host_narrowing`, `priority_default`; plus `save_anyway?: boolean` | `TypeCfg`. Changing `provider` or `model` requires a passing probe for that pair within 10 min, or `save_anyway: true` (logged in the change row's `probe`). Enabling `rfi_from_email` requires `pm_rfi_sync_active`. |
| GET | `/browser-agents/admin/models` | query `provider` | `{provider: Provider, models: {id: string, label: string, price_in_per_mtok: number \| null, price_out_per_mtok: number \| null}[], cached_at: string}` (1 h cache; needs the provider env key) |
| POST | `/browser-agents/admin/types/{key}/probe` | `{provider: Provider, model: string}` | `{ok: boolean, latency_ms: number, error: string \| null, probed_at: string}` (one forced tool call plus a 1x1 PNG; sets `model_probe_ok_at` on success) |
| PUT | `/browser-agents/admin/secrets/{name}` | `{value: string<=200}` | `SecretRow` (never echoes; resets the procore lock, audited) |
| DELETE | `/browser-agents/admin/secrets/{name}` | none | 204 |
| POST | `/browser-agents/admin/enrollments` | `{worker_name: string 2..60}` | `{code: string, expires_at: string, install_command: string, key_fingerprint: string}` (20-char base32 code, single use, 30 min) |
| PATCH | `/browser-agents/admin/workers/{id}` | `{name?, draining?, max_slots? 1..8, allowed_types?: string[], allowed_ips?: string[] (CIDR)}` | `Worker` |
| POST | `/browser-agents/admin/workers/{id}/approve` | none | `Worker` |
| POST | `/browser-agents/admin/workers/{id}/revoke` | none | `Worker` (instant; token hash cleared) |
| GET | `/browser-agents/admin/history` | query `cursor`, `limit` | `{items: SettingsChange[], next_cursor}` newest first |
| POST | `/browser-agents/admin/canary` | `{worker_id?: string}` | `{job_id: string}` (queues a `canary` job) |
| POST | `/browser-agents/admin/stop-running` | none | `{cancel_requested: number}` (sets `cancel_requested_at` on every running/waiting_help job) |

Not in v1 (removed by the overrides): `PUT /admin/attestations/{provider}`, any LLM escalation toggle, the browser-login fallback toggle and release cap.

## Error codes (`X-Error-Code`)

| Code | Status | When |
|---|---|---|
| `looks_like_secret` | 422 | a plaintext help message matches the Procore password fingerprint, or is 4 to 10 digits in a login scope |
| `no_agent_at_login` | 409 | a code was sent while no agent holds the Procore login page (the holder parked or the PC restarted); the detail tells the helper to press Retry now and send the new code when asked |
| `secret_not_allowed` | 422 | a secret message outside a login scope |
| `pm_rfi_sync_off` | 409 | enabling `rfi_from_email` while PM RFI sync is off |
| `probe_failed` | 409 | provider/model change without a passing probe and without `save_anyway` |
| `type_needs_model` | 422 | enabling a type without provider, model and prices (the DB check) |
| `below_min_version` | 409 | `desired_worker_version` below a published `min_version` or unknown |
| `host_not_in_ceiling` | 422 | `host_narrowing` is not a subset of the code ceiling |
| `help_closed` | 409 | writing to a resolved, expired or canceled help request |
| `retry_admin_only` | 403 | a second cap_reached retry by a non IT Admin |
| `job_not_retryable` | 409 | retry on a job that is queued, running or applying |
| `file_not_allowed` | 422 | upload type outside the allowlist or sniff mismatch |
| `file_too_large` | 413 | upload over 150 MB |
| `provider_key_missing` | 409 | models or probe for a provider with no env key |

## Notifications (C3)

| Type | Recipients | Metadata | Bell destination |
|---|---|---|---|
| `browser_agent.help_needed` | Estimating Admin (IT Admin also for login scopes; IT Admin when no active Estimating Admin) | `help_id`, `job_id`, `reason` | `/agents/help/{help_id}` |
| `browser_agent.help_reminder` | same | same | `/agents/help/{help_id}` |
| `browser_agent.help_expired` | Estimating Admin | `help_id`, `job_id`, `reason` | `/agents/jobs/{job_id}` |
| `browser_agent.job_failed` | IT Admin | `job_id`, `error_kind` | `/agents/jobs/{job_id}` |
| `browser_agent.worker_offline` | IT Admin | `worker_id` | `/settings/browser-agents` |
| `browser_agent.worker_pending` | IT Admin | `worker_id`, `hostname`, `ip` | `/settings/browser-agents` |
| `browser_agent.worker_suspicious` | IT Admin | `worker_id` | `/settings/browser-agents` |
| `browser_agent.login_released` | IT Admin | `job_id`, `attempt_id` | `/settings/browser-agents` (registered but no longer sent: a granted login is recorded as a ledger step and a login attempt row only) |
| `browser_agent.provider_problem` | IT Admin | `provider`, `kind` | `/settings/browser-agents` |
| `browser_agent.spend_alert` | IT Admin | `fraction`, `spend_usd`, `cap_usd` | `/settings/browser-agents` |

Email text is fixed per type and reason code; it never includes model text, page content or screenshots.

## Additions made while building B6/B7 (2026-10-19)

These do not change any shape above; they add codes and audit actions the build needed.

| Code | Status | When |
|---|---|---|
| `forbidden` | 403 | the caller's role may not use the route (viewer, helper or admin gate; a model transcript for a non admin) |
| `not_found` | 404 | unknown job, help request, artifact, worker, agent type or secret name |
| `invalid_request` | 422 | a malformed body or query (names the first bad field only), or a change the settings RPC refuses |
| `secrets_key_missing` | 409 | a secret or helper code cannot be stored because `BROWSER_AGENT_SECRETS_KEY` is not set |
| `provider_unreachable` | 502 | `/admin/models` could not list a provider's models |

- Audit actions also include `browser_agent.enrollment_created` (an Add worker code was issued) and `browser_agent.help_file` (a helper upload).
- `POST /jobs/{id}/cancel` on an `applying` or finished job is a no-op that returns the `JobDetail`.
- `POST /jobs/{id}/retry` on a `failed` job with outcome `apply_failed` re-applies the same capture (a new sync run) instead of re-running the browser.
- `install_command` points at `GET /agent-worker/v1/installer.ps1` (no auth), which serves the installer template uploaded by `scripts/publish_agent_worker_release.py --installer-api` with the desired release filled in.

## Integration changes (2026-10-19)

- `POST /admin/workers/{id}/approve` sets `allowed_types` to every built-in type when the worker has none (an empty list never claims). IT Admin narrows it with `PATCH /admin/workers/{id}`.
- RFI attachments in the PM field API carry `from_help: boolean` (true when `external_file_id` starts with `help:`).
- Worker side: `POST .../help/{hid}/secrets/{mid}/use` moves a `waiting_help` job back to `running`, like `/consume`; `POST .../login/result` accepts a final outcome after an interim `mfa` report for the same `attempt_id`.
