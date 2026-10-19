## rfi-sync

# PM RFI sync and its email trigger: map for the browser-agent design (read-only, no files changed)

**Commit state.** The 0154, 0155 and 0156 backend work is already committed as BE `757f238` on branch `build`. The frontend part is committed as FE `797b10d`. Both trees are clean, and nothing is deployed. The commit message says the migrations were applied to dev BDR only.

**Summary.** The email trigger is not in `email_ingest.py`. It is a polling scan inside `services/pm_rfi_sync.py`, run on the same scheduler loop as the daily sync. The 0154 classifier has a table but no code that fills it.

## 1. The email-triggered entry point

There is no RFI or classification code in `app/services/email_ingest.py`. Commit 757f238 changed that file only for R2 fuzzy matching and R3 body matching. The 0154 classifier is also not built anywhere: there is no `email_classify` job handler, nothing writes `email_classifications`, and `docs/EMAIL_CLASSIFICATION.md` does not exist. Only the table, the `email_label_corrections` table and the `llm_jobs` job type `email_classify` exist (0154 lines 19-77). So the "classified as RFI" path reads a table nobody fills today.

**The real trigger is a scan, called on each tick:**
- `pm_rfi_sync.tick()` (lines 2083-2100) runs `schedule_daily`, then `scan_emails` every `pm_rfi_email_scan_interval_seconds` (300), then `run_pending`.
- `polling_loop()` (2103) runs `tick` in `asyncio.to_thread` every `PM_RFI_SYNC_POLL_SECONDS` (30).
- `app/main.py:161-170` starts the loop on every uvicorn worker when `settings.pm_rfi_sync_active` and `supabase_url` are set.

**`scan_emails(sb, settings, now, limit=200)`, lines 1889-1983:**
- **Lease and checkpoint.** The scan takes a lease on the single `pm_rfi_email_scan_state` row (`id='default'`) through `_claim_scan` (1759, compare-and-set on `lease_until`). With no checkpoint yet, it looks back `pm_rfi_email_scan_lookback_hours` (72).
- **Pass A, link in the body.**
  - It reads `ingested_emails` (`id, project_id, direction, from_address, body_text, updated_at`) where `updated_at >= emails_checkpoint`, `direction='inbound'` and `body_text ilike '*project*rfi*show*'`.
  - The checkpoint moves past every row read. Only emails filed to a project with a `pm_stage` (`_pm_projects`, 1773) are acted on.
  - `pc.extract_rfi_links(body_text)` (procore_client.py:389) finds the links, then `_email_trigger` (1843) handles them.
- **Pass B, classification.**
  - It reads `email_classifications` rows with `status='done'` and `updated_at >= classifications_checkpoint`. The effective labels are `user_labels`, or `ai_labels` when `user_labels` is null.
  - A row qualifies when a label has `category == "rfis"`, the email's project is a PM project, and the link is Procore with `sync_enabled`.
  - It then calls `enqueue_run(sb, pid, "email", source_email_id=email_id)`. This queues a full, untargeted run, because the RFI id is unknown.
  - If the table is missing, the error is swallowed (`_missing_table`, 1884).
- **Finish.** It writes both checkpoints and clears the lease.

**`_email_trigger(sb, email, links, stats)`, lines 1843-1871:**
- **No link yet.** If the project has no link and `_trusted_for_autolink` passes, it calls `auto_link_from_email` (1780) to insert a `pm_external_links` row with `linked_via='email'` and `sync_enabled=True`. That check (1819) needs a sender matching `@*.procore.com|procoretech.com` (`_PROCORE_SENDER_RE`, 1816), or a `storage.procore.com` link whose `projectId` matches and which carries a `companyId`.
  - Auto-link is refused if another BDR project already holds that Procore project (`_procore_conflict`, 290).
  - The company id comes from `pc.extract_company_id(body, project_id)` (procore_client.py:414).
- **Which run is queued.** RFI links matching the linked host and project are collected. Exactly one link queues `enqueue_run(..., "email", target_external_rfi_id=<id>, source_email_id=email.id)`. Several links queue an untargeted email run.

**`enqueue_run`, lines 455-514.** It checks `TRIGGERS = ("daily","manual","email")`.
- An email run joins a queued full run, or a queued run with the same target.
- Unique index `pm_rfi_sync_runs_email_uidx (source_email_id, trigger)` means one run per email; a violation returns `(None, False)`.

**What runs after that.** `claim_next` (556) and `process_run` (1614) call `sync_project(..., target_external_rfi_id, source_email_id)` (1427). A targeted run after the backfill fetches only `session.get_rfi(pid, ext_id, company)`. If Procore refuses the targeted id, `SyncError` is raised (1490). The first run of a project is always a full backfill.

**Hook for the replacement.** `enqueue_run`'s `"email"` branch, and the two places in `scan_emails` that call it (1868, 1970), are where an agent job would be enqueued instead. The daily path (`schedule_daily` 517, trigger `daily`) and the manual path stay as they are.

## 2. Tables and columns

**`pm_external_links`** (0156:26-58), one row per project:
- `project_id` (unique, FK projects)
- `platform` (procore | other | none), `host`, `external_company_id`, `external_project_id`, `external_url`
- `sync_enabled` (default true), `linked_by`, `linked_via` (manual | email)
- `last_synced_at`, `last_sync_status` (ok | failed), `last_error`
- `backfilled_at`, `backfill_skipped_ids text[]`
- Partial unique index `pm_external_links_procore_project_uidx (platform, host, external_project_id) where platform='procore'`

**`rfis`.**
- Base columns (0060:39): `id, project_id, rfi_number int, subject, question (HTML after 0068), answer, status rfi_status enum('open','answered','closed'), asked_of, sent_at date, due_at date, answered_at date, created_by, created_at, updated_at`.
- 0068 added `drawing_numbers, applicable_references, priority, assigned_gc_id, assigned_contact_id`. Other code paths also use `send_status`, `sent_via`, `last_sent_by` (from later migrations).
- 0156:62-98:
  - `rfi_number` is now nullable.
  - `source text default 'internal'`, with check `source in ('internal','procore')` (`rfis_source_chk`).
  - `rfis_internal_number_chk`: an internal row must have a number.
  - `external_id, external_number text ("Const-198", "Const-189.1"), external_revision text default '0', external_status, external_url, responsible_contractor, received_from, initiated_at timestamptz, ball_in_court, external_due_date date, external_updated_at, external_synced_at, external_raw jsonb (compact, no bodies), external_autolink_blocked bool, source_email_id uuid -> ingested_emails`
- **Dedupe keys:**
  - `rfis_project_rfi_number_uidx (project_id, rfi_number) where rfi_number is not null`
  - `rfis_project_external_id_uidx (project_id, external_id) where external_id is not null` (the primary key for imports)
  - `rfis_project_external_number_rev_uidx (project_id, external_number, external_revision) where source='procore'`
- **Revisions:** each Procore revision is its own row, keyed by its own Procore id, with `external_revision` set from `detail.revision`. "Const-189.1" is a separate RFI, not a revision.
- **Completeness marker:** `external_updated_at` is written only after the attachments land (`_mark_complete`, 1270). Null means "fetch again".
- **Status never regresses:** `_progress` (1217), and closed is terminal. `_finished` (1243) means closed and complete, and such a row is never refetched.
- **Columns cleared on Unlink:** `EXTERNAL_FIELDS` (pm_rfi_sync.py:89-104).

**`rfi_attachments`.**
- Base (0068): `id, rfi_id -> rfis cascade, doc_key text check '^(pm|bid|cp):<uuid>$', created_by, created_at, unique(rfi_id, doc_key)`.
- 0070 added `kind` ('question' | 'answer', default 'question'), and the unique key gained `kind`.
- 0156 added `external_file_id` with `rfi_attachments_external_file_uidx (rfi_id, external_file_id) where external_file_id is not null`.

**File storage.** The file goes to Storage at `storage.build_object_path(project_id, "pm/rfi", filename)`, and a `pm_documents` row is created (`project_id, category='rfi', storage_path, filename[:255], mime_type, size_bytes, note, uploaded_by=None`). The `note` carries the marker `[procore-file:<id>]` (`_FILE_MARKER`, 118). That marker is how `_existing_doc` (1018) reuses a file across links and unlinks. The attachment row then gets `doc_key = "pm:<pm_documents.id>"`.

**`pm_rfi_sync_runs`** (0156:108-139):
- Columns: `trigger` (daily | manual | email), `scheduled_for`, `target_external_rfi_id`, `source_email_id`, `requested_by`, `status` (queued | running | done | failed), `lease_until`, `lease_owner`, `attempts`, `next_attempt_at`, `stats jsonb`, `error`, `created_at`, `started_at`, `finished_at`.
- Unique indexes: daily `(project_id, trigger, scheduled_for)` and email `(source_email_id, trigger)`.

**`pm_rfi_email_scan_state`:** `emails_checkpoint, classifications_checkpoint, lease_until, lease_owner`.

**`email_classifications`** (0154): `email_id` (PK -> ingested_emails), `project_id`, `conversation_id`, `status`, `ai_labels`/`user_labels` jsonb (a list of `{category, subcategory, title, confidence}`, where category is one of `materials_submittals | rfis | financials | change_management | other`), `email_summary`, `thread_summary`, `model`, `error`, `classified_at`.

All new tables have RLS forced and are accessed by the service role only.

## 3. Functions an agent path can reuse

- **`map_detail(detail, host, project_id)`** (891): Procore RFI detail JSON to rfis columns.
  - Question = sanitized rich text (`sanitize_rich_text`). Answer = official answers, or all answers when none is official, as plain text, oldest first.
  - Status comes from `map_status` (845).
  - `external_url` is rebuilt from digit-checked ids (`_rfi_url`, 859); Procore's `link` field is never stored.
  - Helpers: `_compact_raw` (870), `detail_attachments(detail)` (942, returns [(kind, att)] with `att.id/filename/url`), `_attachment_name` (967).
- **`apply_detail(sb, project_id, link, detail, local, candidates, session, settings, stats, *, backfill, source_email_id, renew, listed_ids)`** (1277): the full upsert of one RFI.
  - Inserts or updates an imported row, or links and updates an internal G3 row. An internal row gets the external fields, status, answer and answer files, and is marked `send_status='sent_external', sent_via='procore'`.
  - Handles unique collisions (`_retire_stale`, 1248) and calls `sync_attachments`.
  - It needs `session.download(url, dest, max_bytes=)` returning a byte count and raising `pc.ProcoreForbidden` / `pc.ProcoreTransient`. An agent could supply a shim session that serves files it already downloaded, or the agent could upload files and call a new attach helper.
  - Loading prerequisites: `_load_local(sb, project_id)` (1196) returns `(by_ext, candidates)`. `_new_stats()` is at 1171.
- **`sync_attachments(...)`** (1031): idempotent per `external_file_id`, reuses Documents through the note marker, inserts `rfi_attachments`, and returns False for retry-later.
- **`_download_to_documents(...)`** (1105): download, then `storage.upload_file`, then the `pm_documents` insert. Content type comes from `routers.files._safe_content_type`. Cap: `pm_rfi_sync_attachment_max_bytes` (150 MB). Oversize or refused files go to `stats.skipped_files`.
- **`match_internal(detail, candidates)`** (978): auto-link to a G3 RFI, either by question-file name `^RFI[ #-]*0*<n>\b` (pattern at 974) or by an exact `normalize_subject` (177) match.
- **Manual link/unlink:** `link_rows` (1998) and `unlink_row` (2051), plus `purge_imported` (309).
- **Notifications:** `notify_after_run(project_id, stats, notify=notify_role)` (1532) and `summary_message` (1519). The stats it needs are the lists `new` / `answered` / `closed` and `backfill`.
- **Queue and lease pattern to copy:**
  - `claim_next` (556): compare-and-set on status plus the old `lease_owner`, waits out `next_attempt_at`, reclaims a run with an expired lease up to `max_attempts` then fails it, skips busy projects.
  - `_yield_to_rival` (627), `_renewer` (662, renews every 30 s, raises `_LeaseLost`), `_finish` (685), `_update_link_status` (701).
  - `_carry_forward` (1576) keeps the new/answered/closed lists across retries.
  - `WORKER_ID = host:pid:rand` (120).
- **Audit:** the routers call `app.services.notifications.audit(actor_id, action, entity_type, entity_id, meta)`. Existing actions: `pm_link.update`, `rfi.sync`, `rfi.link`, `rfi.unlink`.

## 4. Matching a BDR project to the GC's PM project

**The link is explicit.** `pm_external_links` is keyed on `project_id`. The email path relies on the email already being filed to a project by the ingest R1/R2/R3 rounds (`ingested_emails.project_id`), and on that project having a `pm_stage`.

**URL shapes** accepted by `pc.parse_project_url` (procore_client.py:358):
- Hosts: `APP_HOSTS = ("app.procore.com", "us02.procore.com")` (line 55). Only https; Outlook safelinks are unwrapped (`unwrap_link`, 217).
- `/{project}/project/...` (`_PROJECT_PATH_RE`, 331). The company id comes from the `companyId` / `company_id` query parameter if present.
- `/webclients/host/companies/{c}/projects/{p}/...` (`_WEBCLIENT_RE`, 333)
- `/companies/{c}/projects/{p}` (`_COMPANY_PROJECT_RE`, 336)
- RFI link: `/{project}/project/rfi/show/{rfi}` (`_RFI_SHOW_RE`, 329), returned as `ProcoreRfiLink(host, project_id, rfi_id)` (321).
- Ids may be up to 20 digits (`_LONG_ID`).

**Resolving the company id.** `save_link` (343) calls `resolve_company` when the URL has no company: `session.resolve_company_id` (procore_client.py:867), which reads the project-home redirect or GET `/rest/v1.0/companies`. Otherwise the run resolves it (`_resolve_company`, 1410) and stores it.

**Read-only allowlist** (`RFI_ALLOWED_PATHS`, procore_client.py:97-102):
- `/rest/v1.0/projects/{p}/rfis`
- `/rest/v1.0/projects/{p}/rfis/{id}`
- `/rest/v1.0/companies`
- `/rest/v1.0/projects/{p}`
- `ProcoreSession.list_rfis` is at 839 and `get_rfi` at 854.

**Sessions and credentials.**
- `open_session(host)` (pm_rfi_sync.py:760) uses `rfp_harvest._SessionStore`. Its row is in `rfp_harvest_sessions` (0123:80: `provider` PK, `account`, `cookies` jsonb, `logged_in_at`, `last_login_attempt_at`, `login_failures`, `locked_until`, `last_error`), provider `procore`. us02 uses provider `procore:<host>` and shares the lock through `_SharedLockStore`.
- Credentials come from env only: `settings.procore_login_email` / `procore_login_password` (config.py:830-831), and `procore_configured` (1118).
- There is no IT Admin settings storage for the Procore password today. The request asks for the password to come from IT Admin settings, so that would be new.
- A login lock rings an IT Admin bell through `rfp_harvest._notify_lock` (1117), one per lock while unread, with `mirror_email=False`.
- Captcha is detected only as a Cloudflare challenge (`_looks_like_challenge`, procore_client.py:563).

**Frontend.**
- Link card: `bdr_fe/components/pm/field/PmLinkCard.tsx` (343 lines). It calls GET/PUT `{base}/pm-link` and POST `{base}/rfis/sync`, and polls `POLL_MS` while `running`. It shows `last_error`, `linked_via==="email"` and a backfill-pending note.
- The card is mounted in `bdr_fe/app/(app)/pm/projects/[id]/rfis/page.tsx:29` when `features.pm_rfi_sync`.
- Type `PmExternalLink` is at `bdr_fe/lib/types.ts:427`. The flag is in `bdr_fe/lib/features.ts:64,96`, and the notification types at 150-151.
- Backend schemas: `PmExternalLinkIn/Out` and `RFILinkIn` in `app/models/schemas.py`. The view is built by `link_view` (271).

## 5. Read-only enforcement

- `routers/pm_field.py:261` `_reject_read_only` returns 409 with `pm_rfi_sync.READ_ONLY_DETAIL` ("This RFI is imported from Procore and is read-only.") for `source='procore'`.
- It is called in `update_rfi` (602), `close_rfi` (692), `send_rfi` (785), `mark_rfi_sent` (907) and `delete_rfi` (950).
- `_enrich_rfis` (457) drops `external_raw` and sets `read_only = source=='procore'`. Its attachments are split by `kind`.
- `_rfi_sort_key` (270) puts G3 rows first by number, then Procore rows by `natural_key` and revision.

## 6. Notifications

- `NOTIFY_ROLES = (Role.ESTIMATING_ENGINEER_LABOR, Role.ESTIMATING_ADMIN)` (pm_rfi_sync.py:81).
- `notify_role(role, project_id, type_, message, rfq_id=None, mirror_email=True, metadata=None)` is in `services/notifications.py:35`. It refuses the estimator role.
- **Backfill:** one `pm_rfi.linked` per role, in-app only (`mirror_email=False`, metadata `{imported}`).
- **Later runs:** one `pm_rfi.sync_summary` per role per run, bell plus the branded email mirror. Metadata is `{new, answered, closed}` counts. The list shows at most 5 labels plus "and N more", and labels come from `rfi_label` (195).
- A run that fails for good still sends what it saved (`_notify_partial`, 1604).
- Email titles: `notification_email.py:150-151`. Deep link: lines 255-256 send `pm_rfi.*` to `/pm/projects/{id}/rfis`.
- Both types are registered in `core/features.py` `PM_NOTIFICATION_TYPES` and the frontend `lib/features.ts`.
- A future "agent needs help" notice to Estimating Admin would need a new type in both lists and in `_TYPE_META`.

## 7. Feature flag and settings

- `config.py:1021-1032`: `pm_rfi_sync_enabled=False`, plus the `pm_rfi_sync_*` and `pm_rfi_email_scan_*` settings (`pm_rfi_email_scan_enabled=True`, interval 300, lookback 72).
- `pm_rfi_sync_active = pm_enabled and pm_rfi_sync_enabled` (1035). Boot validation is at 1046-1068.
- `features.py:127` sets the `/features` flag `pm_rfi_sync`. `features.py:225` `require_pm_rfi_sync()` returns a bare 404 before auth and is applied on the router (`routers/pm_rfi_sync.py:31-35`). The router is mounted with `_PM` in `main.py:738`.
- **Router endpoints:** GET/PUT `/pm/projects/{id}/pm-link`, POST `/rfis/sync`, POST `/rfis/{rfi_id}/link`, POST `/rfis/{rfi_id}/unlink`. Read uses `require_pm_read`, writes use `require_pm_write`, and every route calls `require_pm_project`.
- `tests/conftest.py` sets `PM_RFI_SYNC_ENABLED=false` so the lifespan loop never starts in tests.
- The doc says the dev `.env` has the flag on.

## 8. Tests (`tests/test_pm_rfi_sync.py`, 1675 lines, about 119 defs)

**Style:** no network and no Supabase.
- `FakeDB` / `_Q` (around line 60 onward) is an in-memory imitation of the supabase-py query builder. It supports `select/insert/update/upsert/delete/eq/in_/gte/lte/is_/not_.is_/ilike/like/order/limit`.
- `_UNIQUE` (around line 216) reproduces the 0156 unique indexes as `(table, index_name, predicate, cols)` and raises "23505 duplicate key".
- `FakeSession` serves fixtures and records `calls`: `list_rfis`, `get_rfi` (unknown id raises `pc.ProcoreForbidden`), `resolve_company_id`, `download` (with an `oversize` set), `close`.
- `_settings(**over)` builds `Settings(supabase_url="", pm_enabled=True, pm_rfi_sync_enabled=True, procore_login_email="bot@example.com", procore_login_password="pw", notification_emails_enabled=False)`.
- An autouse fixture `_storage` monkeypatches `storage.upload_file` / `build_object_path`.
- Other helpers: `_db(**tables)` (seeds projects p1, p2 and "bid", which has no pm_stage), `_link(**over)`, `_sync(...)`, `_procore_rows`, `_email(eid, body, project_id, direction, updated_at)`, `NOW = 2026-10-18 12:00 UTC`.
- **Fixtures** in `tests/fixtures_pm_rfi/`: `list.json`, `detail_27914290.json` (Const-198), `detail_27699412.json` (Const-189.1), `email_closed_const198.txt`, `email_response_const189_1.txt`, `email_boyd_rfi20.txt` (bid phase, must not trigger). They are trimmed real captures of Procore project 3167322, company 6333.

**Email-path tests that need updating if that path moves to the agent:**
- 433, 441, 446: link extraction
- 932: auto-link plus targeted run
- 951: no stealing another project's link
- 960: ignores bid projects, outbound mail and None links
- 972: classifications queue a run
- 985: missing classifications table is tolerated
- 1469: Sync now does not join a targeted email run
- 1508: checkpoint moves past unfiled emails
- 1609: auto-link trust rules

Related tests touched by the same commit: `tests/test_email_ingest.py`, `tests/test_email_match.py`, `tests/test_feature_flags.py`.

## Points for the architect

1. The "classified as RFI" trigger reads a table nothing fills. Either the 0154 classifier gets built, or the agent's trigger keeps Pass A (the body has a Procore RFI link) as the signal. Pass A also gives the exact RFI id.
2. The email path and the daily path share one queue table and one per-project lock: `claim_next` skips a project while another run holds a live lease. An external agent writing the same `rfis` rows needs to work alongside that lock, for example by claiming through the same project-busy rule. Otherwise two writers can hit the `external_id` / `(external_number, revision)` unique indexes, which `apply_detail` already treats as conflicts or retires.
3. `apply_detail` and `sync_attachments` expect a session object that can download. The cleanest reuse for the agent is a backend ingest endpoint: it takes the RFI JSON fields and uploaded files, writes `pm_documents` with the `[procore-file:<id>]` marker, then runs the same upsert, linking and notification code.
4. Credentials today are env-only (`PROCORE_LOGIN_*`), and the cookie jar lives in `rfp_harvest_sessions` provider `procore`, shared with the RFP harvest and its lockout counter. An agent logging in separately with the same account adds to the same Procore login lockout unless it records attempts through the same `record_login` / lock bookkeeping.

## settings-llm

# Settings, secrets, LLM routing and roles for the Browser Agent platform

Nothing was modified. Paths are under `/Users/thomasmoore/Desktop/BDR`.

## 1. How app-wide settings are stored and edited

There is no generic `app_settings` or `system_settings` table. The pattern is one single-row table per feature, plus a separate append-only audit table:

| Table (migration) | Columns | Audit table |
|---|---|---|
| `llm_runtime_settings` (`bdr_be/supabase/migrations/0150_llm_runtime_target.sql`) | `id smallint pk default 1 check (id = 1)`, `self_hosted_target text`, `updated_by uuid -> profiles`, `updated_at` | `llm_target_changes` (`from_target`, `to_target`, `forced`, `probe_state`, `changed_by`, `changed_at`, index on changed_at desc) |
| `ai_spend_settings` (`0152_plans_reader.sql:280-292`) | `id=1` check, `plans_reader_daily_cap_usd numeric(10,2) default 75`, `cap_window_started_at`, `cap_hit_notified_at`, `updated_by`, `updated_at`; seeded with `insert ... on conflict do nothing` | `ai_spend_events` (`kind` check in cap_hit / cap_reset / cap_changed, `amount_usd`, `details jsonb`, `actor_id`, `created_at`) |
| `cp_settings`, `notification_prefs` (older) | per-module settings | none |

- **RLS:** every one of these tables uses `enable row level security` plus `force row level security`, with no policies. Only the service-role backend can reach them (this follows 0007 and 0055).
- **Every migration ends** with `notify pgrst, 'reload schema';`.

**How workers pick up a change live** (`bdr_be/app/services/llm_target.py`):
- `_POLL_SECONDS = 10` (line 43).
- `refresh()` (line 151) reads `llm_runtime_settings`.
- `polling_loop()` (line 173) is started in `bdr_be/app/main.py:35`.
- `apply(s: Settings) -> Settings` (line 102) overlays the DB override on the env Settings, cached per key.
- `switch()` (lines 286-324) upserts the row, inserts into `llm_target_changes` only when the target actually changes, updates memory, and calls `llm_health.reset_cache()`.
- `state()` (line 194) returns the options, live and default targets, and `recent_changes` (joined as `who:profiles!llm_target_changes_changed_by_fkey(full_name, email)`, line 237).

**Routers:**
- `bdr_be/app/routers/llm_status.py`:
  - `GET /model-status` and `POST /model-status/check` use `require_internal`.
  - `GET /model-status/target` and `PUT /model-status/target` (body `TargetSwitchIn{target: str|None max 16, force: bool}`, 409 on `TargetUnavailable`).
  - The gate is a local helper, `require_model_admin`: `user.role == Role.IT_ADMIN or user.is_dev`. It is defined in this router, not in deps.
  - Rate limit: `model_status_rate_limit` from `core/ratelimit.py`.
- `bdr_be/app/routers/ai_spend.py`:
  - Router-level dependencies: `require_plans_reader` (404 when the flag is off) and `require_ai_estimating`.
  - `GET /ai-spend` uses `require_internal`.
  - `PATCH /ai-spend/cap` and `POST /ai-spend/reset` use `require_spend_admin = require_role(Role.IT_ADMIN, Role.EXECUTIVE)`.
  - The service is `bdr_be/app/services/ai_spend.py` (`status`, `set_cap`, `reset_window`; `MIN_CAP_USD` / `MAX_CAP_USD`).
- Handlers are plain `def`, never `async def`, because the Supabase SDK is synchronous (repo rule on event-loop blocking).
- Both routers are on the shared spine: `main.py:640` and `main.py:644`, with no sub-app flag.

**Reuse for the browser agent:** add a single-row `browser_agent_settings` table (or one row per agent type) with `updated_by`/`updated_at`, plus a `browser_agent_settings_changes` audit table. Workers pull it (the PC worker over HTTP; the API through a `polling_loop` like `llm_target`).

## 2. Secrets: nothing is encrypted at rest today

A search for fernet, encrypt, pgcrypto, pgsodium and Supabase vault found no secret-storage mechanism in `bdr_be/app` or the migrations.

- **Procore login:** comes only from env, not the DB.
  - `bdr_be/app/core/config.py:830-836`: `procore_login_email`, `procore_login_password`, `procore_min_request_interval_seconds`, `procore_login_min_interval_seconds=600`, `procore_login_max_failures=3`, `procore_login_lock_seconds=21600`, `procore_request_timeout_seconds`.
  - `procore_configured` property at `config.py:1118-1120`.
  - Validator at `config.py:1557-1564`.
  - Comment at line 828: "the env is the only place to change it".
  - `main.py:167` warns when PM RFI sync is enabled without the credentials; `pm_rfi_sync.py:1633` gives the error message.
- **Procore session cookies:** table `rfp_harvest_sessions` (`0123_rfp_harvest.sql:80-101`).
  - Columns: `provider text pk`, `account`, `cookies jsonb` (plain text), `logged_in_at`, `last_used_at`, `last_login_attempt_at`, `login_failures`, `locked_until`, `last_error`, `updated_at`.
  - Used by the RFP harvest and the PM RFI sync. `pm_rfi_sync.session_provider(host)` is at line 717 and `open_session` at line 760.
  - Login discipline (allowlist, pace, lock, password only in the POST body) is documented in `bdr_be/app/services/procore_client.py:1-30`.
- **BuildingConnected OAuth tokens:** table `rfp_oauth_connections` (`0136_rfp_buildingconnected.sql:188-221`).
  - `access_token` and `refresh_token` are stored as plain text. The table comment says so explicitly: "Tokens are plain text; RLS forced, service role only."
  - It has `refresh_lock_until` / `refresh_lock_owner` as a lease, which is a reusable pattern.
- **SmartBid passport key:** never stored. It is parsed per email and held in memory only.
  - `bdr_be/app/services/smartbid_client.py:248-261`: `key_fingerprint` is a sha256 prefix, and the key field is `field(repr=False)`.
  - An httpx log redaction filter is at lines 578-613.
  - The `account` value in the store row is the literal "passport" (line 109).
- **Redaction helper:** `bdr_be/app/core/redact.py` exists.

**Consequence:** "password from IT Admin settings" needs a new encrypted secret store. Two options:

- App-side envelope encryption: AES-GCM or Fernet keyed by a new env master key such as `SECRETS_ENCRYPTION_KEY`, with a boot validator required in production.
- pgsodium / Supabase Vault, which is not used anywhere yet.

Either way, the store should be write-only from the UI: a "set / replace / clear" action that shows only `last4` or a "set" flag and `updated_at`/`updated_by`. It should never be returned by any route. Exposing it to the PC worker would need a dedicated worker-auth path.

## 3. How `bdr_be/app/services/llm.py` routes models

- **Providers today:** only `"anthropic" | "openai" | "self_hosted"`.
  - `Route` dataclass at line 305: `provider, model, api_key, base_url`.
  - Keys come from `config.py:138-139` (`anthropic_api_key`, `openai_api_key`) plus per-box self-hosted keys and models at lines 157-167 (local, ec2, ec2b, ec2c).
  - There is no Google, xAI or Meta key, model or client anywhere (`google_drive_api_key` at line 870 is unrelated).
- **Feature table:** `_Feature` dataclass (lines 196-214) has `third_party` (a string or a Settings-reader), `tp_model`, `sh_model`, `openai_only`, `self_hosted_only`, and `switch`. The `_FEATURES` dict is at lines 229-302.
  - Example of a per-feature env-selectable vendor: `bid_split` uses `BID_SPLIT_LLM_PROVIDER`.
  - `_plans_feature()` (lines 217-226) is the `openai_only` pin.
- **`resolve()`** (line 326): applies `llm_target.apply`, then checks in order: `openai_only`, then `self_hosted_only`, then `full_self_hosted_llms_enabled`, then the env vendor.
- **Other helpers:** `unavailable_reason` (372), `is_configured` (383), `active_model` (395), and `_require_route` (413), which raises `LlmNotConfigured` naming the env var.
- **Clients:** `_client_for` / `_build_client` (442-500) cache SDK clients keyed by provider and key. Anthropic and OpenAI use `third_party_llm_timeout_seconds` and `third_party_llm_max_retries`.
- **Accounting:**
  - `_guarded` (802) runs each call inside `llm_gate.slot(provider, tier, s)`, the per-provider concurrency gate.
  - `_log` (825) writes `llm_call_log` through `llm_gate.log_call`. Columns: `feature, provider, model, tier ('job'|'interactive'|'pipeline'), job_id, ok, error_kind, error`, plus token and `cost_usd` columns from 0152.
  - `llm_gate._gate_for(provider)` (line 119) keys gates by provider name.
- **Pricing:** `bdr_be/app/services/llm_pricing.py` prices only `gpt-6-astra` (line 62). Any other model's cost is "unknown" (`None`).
- **Transports:** `complete_text`, `complete_json`, `complete_pdf_json`, `complete_images_json`, and `complete_parts` (the OpenAI Responses API with tools and tool-call continuation, line 1208).
  - None of them is a computer-use or browser-tool loop. There is no Anthropic tool loop.

**Implications for the PC worker:**
- The worker calls the cloud LLM from the Windows PC, so the provider API keys must reach it somehow. Options: BDR hands out keys over the worker channel, BDR proxies the LLM calls, or keys live in a local worker env.
- The user's requirement is that IT Admin picks provider and model in Settings, and that nobody has to touch the PC to change the model. Setting changes therefore have to come down over the worker's config pull, not from the PC's local env.
- Proxying every agent LLM call through BDR would reuse `llm_call_log`, `llm_gate` and pricing for audit and spend, but needs new transports for Google, xAI and Meta (Meta has no first-party API beyond the OpenAI-compatible Llama API).
- An allowlist of provider and model choices plus an env key per provider (`GOOGLE_API_KEY`, `XAI_API_KEY`, `META_LLAMA_API_KEY`, ...) fits the existing `config.py` pattern. Validation would live in `ai_spend`-style service code, with a `CHECK` in SQL or in the app (0150 checks target names in the app so adding one needs no migration).

## 4. `bdr_be/app/core/config.py` patterns

- `Settings(BaseSettings)` at line 81, with `model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True)` (line 87). `get_settings()` is `@lru_cache` (line 1950).
- **Feature-block convention:** a commented section, then `<feature>_enabled: bool = False`, then numeric knobs. Example: PM RFI sync at lines 1016-1033 (`pm_rfi_sync_enabled`, `pm_rfi_sync_daily_time="06:30"`, `pm_rfi_sync_poll_seconds=30`, `pm_rfi_sync_lease_seconds=900`, `pm_rfi_sync_max_attempts=3`, `pm_rfi_sync_retry_seconds=600`, `pm_rfi_email_scan_enabled=True`, `pm_rfi_email_scan_interval_seconds=300`, `pm_rfi_email_scan_lookback_hours=72`).
  - Derived property `pm_rfi_sync_active` (pm_enabled and the flag), line 1036.
  - The validator is a `@model_validator(mode="after")` that raises `ValueError` with the env var name, e.g. `_validate_pm_rfi_sync` at line 1046.
- **Production guards:**
  - `is_production` (line 1222) fails closed outside `DEV_ENVIRONMENTS`.
  - `_validate_environment_explicit` (line 1228): `ENVIRONMENT` must be set on Railway.
  - `_validate_production` (line 1241) refuses dev-only features in production (e.g. `rfp_testing_enabled`).
  - `_validate_self_hosted_llms` (line 1307) enforces https in production.
- **Flags to the frontend:** `GET /features` (`main.py:534`) is the frontend's source of truth for flags (`features?.ai_estimating`, `features?.rfp_email_ingest`, ...). A `browser_agents` flag would be added there.
- **Background loops** start in the `main.py` lifespan through `asyncio.create_task(...polling_loop())`, guarded by `settings.<x>_active and settings.supabase_url`. PM RFI sync is at lines 161-170 and is cancelled in the list at line 253.

## 5. Roles and dependency helpers

- **`bdr_be/app/core/roles.py`:** the `Role` StrEnum has `estimating_engineer_materials`, `estimating_engineer_labor`, `estimating_admin`, `executive`, `accountant`, `it_admin`, `estimator`.
  - Role sets: `WRITER_ROLES`, `INTERNAL_ROLES`, `VERIFY_ROLES`, `PM_READ_ROLES = INTERNAL_ROLES`, `PM_WRITE_ROLES = WRITER_ROLES`, `RFP_REVIEW_ROLES`, `PROJECT_RESTORE_ROLES = (IT_ADMIN,)`, and others.
  - Convention: a named frozenset or tuple per audience. A new `BROWSER_AGENT_ADMIN_ROLES = (Role.IT_ADMIN,)` and `BROWSER_AGENT_HELPER_ROLES = (Role.ESTIMATING_ADMIN,)` (maybe plus IT_ADMIN) would fit.
- **`bdr_be/app/core/deps.py`:**
  - `CurrentUser` dataclass (line 47): `id, email, role, is_active, is_dev, aal, mfa_enrolled, impersonated_by, rfp_mailboxes`.
  - `get_current_user` (line 72) verifies the Supabase JWT, loads the profile and enforces aal2 (2FA).
  - Helpers: `require_role(*allowed)` (line 241, a factory returning 403 "Insufficient role"), `require_internal` (253), `require_ai_estimating` (266), `require_writer` (278), `require_dev` (292), `require_pm_read` / `require_pm_write` (305/326), `require_cp_*`, `require_project_assignment` (373).
  - IT Admin or dev in Settings is the local `require_model_admin` in `llm_status.py`. User management uses `MANAGE_USER_ROLES = (IT_ADMIN, EXECUTIVE)` in `bdr_be/app/routers/users.py:58`.
- **No machine-to-machine auth exists.** A search for hmac, compare_digest, worker_token and api_token found nothing. Every route needs a human Supabase JWT at aal2.
  - The Windows worker needs a new auth path, for example a hashed per-worker token in a `browser_agent_workers` table, checked with `hmac.compare_digest`, as a new dependency (`require_agent_worker`) that bypasses `get_current_user`.
  - The aal2 check means a service-account JWT will not work cleanly.
- **Notifications** (`bdr_be/app/services/notifications.py`):
  - `notify_role(role, project_id, type_, message, rfq_id=None, mirror_email=True, metadata=None)` (line 35) creates a bell row for every active user with that role, plus a generic email mirror. It refuses `Role.ESTIMATOR`.
  - `notify_user` (line 95) and `dismiss_notifications` (line 117).
  - `notify_role(Role.ESTIMATING_ADMIN, ...)` covers the "ask Estimating Admins for help" alert. New `type_` values must be added to the type list near the top of the file.
- **General audit:** `notifications.audit(actor_id, action, entity, entity_id, payload)` (line 175) writes `audit_log` (`0006_system_tables.sql:34`: `actor_id -> profiles`, `action`, `entity`, `entity_id uuid`, `payload jsonb`, `created_at`, indexes on actor and action).
  - Agent-step audit probably wants its own append-only table (job, step, action, screenshot/storage path, llm call ref) and can also write summary rows to `audit_log`.

## 6. Existing patterns to reuse for leases and resume

- **`pm_rfi_sync_runs`** (`0156_pm_rfi_sync.sql`): `status` (queued, running, done, failed), `lease_until`, `lease_owner`, `attempts`, `next_attempt_at`, `stats jsonb`, `error`, `started_at`, `finished_at`, and a partial open index.
  - Service functions in `bdr_be/app/services/pm_rfi_sync.py`: `claim_next` (556), `_renewer` (662), `_finish` (685), `process_run` (1614), `run_pending` (1731), `tick` (2083), `polling_loop` (2103).
- **The email trigger to be replaced** is in the same file:
  - `scan_emails` (1889), `_email_trigger` (1843), `auto_link_from_email` (1780), `_trusted_for_autolink` (1819).
  - Scan state lives in `pm_rfi_email_scan_state` (with its own lease), controlled by `pm_rfi_email_scan_enabled`.
  - The run has `trigger='email'`, with a unique index on `(source_email_id, trigger)`.
  - The daily sweep (`schedule_daily`, line 517, `trigger='daily'`) stays.
- **Other lease patterns:** the `rfp_oauth_connections` refresh lock, and the `llm_jobs` queue (0094, `bdr_be/app/services/llm_queue.py`).

## 7. Frontend Settings

- **`bdr_fe/app/(app)/settings/page.tsx`** is a single `max-w-2xl space-y-6` column of cards, each gated on `profile.role`, `profile.is_dev` and `features?.x`:
  - `AccountSettings`
  - `NotificationPrefsSection` (non-estimator)
  - Users card linking to `/settings/users` (it_admin or executive)
  - RFP Ingestion card linking to `/settings/rfp-ingestion` (`canManageRfpSenders`)
  - `ModelServerSettings` (`profile.role === "it_admin" || profile.is_dev`)
  - `AiSpendSettings` (non-estimator and `features?.ai_estimating === true`; editor only for it_admin and executive)
  - Dev role switcher
- **Sub-pages:**
  - `bdr_fe/app/(app)/settings/users/page.tsx`.
  - `bdr_fe/app/(app)/settings/rfp-ingestion/page.tsx` stacks sections: `RfpAuthorizedSendersSection`, `RfpBlockedSendersSection`, `RfpHarvestStatusSection` (the Procore session status from `GET /rfp-emails/harvest-status`, `bdr_be/app/routers/rfp_emails.py:1241`), `RfpNgemStatusSection` and `RfpBuildingConnectedSection`. It redirects to `/settings` when the user has no section.
- **Component pattern** (`bdr_fe/components/ModelServerSettings.tsx`, 214 lines):
  - Typed interfaces, `apiFetch<T>(path)` / `ApiError` from `bdr_fe/lib/api.ts`, and `useTranslation()` with an i18n namespace (`modelServer.*`).
  - Primitives from `@/components/ui`: `Badge, Button, Callout, Card, DateTime, ErrorText, SectionHeader, Spinner`.
  - Shows a recent-changes audit list.
  - Larger features put API helpers in `bdr_fe/lib/<feature>.ts` (e.g. `bdr_fe/lib/rfpBuildingConnected.ts`).
  - i18n uses react-i18next with `bdr_fe/locales` in 6 languages; English-only namespaces are accepted (RFP precedent).
- **How a "Browser agents" card would fit:**
  - The card is `<BrowserAgentSettings />` on `settings/page.tsx`, gated `profile.role === "it_admin"` (optionally `|| profile.is_dev`) and `features?.browser_agents === true`. It shows a summary and links to a roomier `/settings/browser-agents` sub-page, the same way Users and RFP Ingestion do.
  - The sub-page sections would be:
    - Provider and model picker per agent type (OpenAI, Anthropic, Meta, Google, xAI)
    - Procore credential (login email shown read-only from `PROCORE_LOGIN_EMAIL`; password write-only "Set / Replace / Clear" with a "set on ... by ..." display)
    - Registered workers (last heartbeat, version, revoke or rotate token)
    - Agent types (enable or disable, concurrency)
    - Change history from the settings-changes table
  - The Estimating Admin help-request chat belongs on a separate non-Settings surface (bell notification, then the job page), not in Settings.

## queue-notify

Report: queue, lease, scheduler, notification, email, auth, storage and audit patterns in BDR (read-only; no files modified)

All paths are under `/Users/thomasmoore/Desktop/BDR/bdr_be` unless noted.

## 0. State check (affects the "commit 0156" ask)
- The 0156 work is already committed. BE `git log` shows `757f238` "PM email classification (0154), email match candidates (0155), PM RFI sync from GC Procore (0156)", marked DEV ONLY, and `git status` is clean. FE (`/Users/thomasmoore/Desktop/BDR/bdr_fe`) has `797b10d` "PM RFI sync UI (PM link card, imported RFI display), email match candidates".
- 0154 is only partly built. The migration adds `email_classifications`, `email_label_corrections` and the `email_classify` value in `llm_jobs_job_type_check` (`supabase/migrations/0154_pm_email_classification.sql:70-77`). No backend code runs that job:
  - `app/services/llm_queue.py:90-134` has no `JOB_EMAIL_CLASSIFY` and `_FEATURE_BY_TYPE` has no entry for it.
  - Nothing outside `pm_rfi_sync.py` reads `email_classifications`.
  - `pm_rfi_sync.scan_emails` (`app/services/pm_rfi_sync.py:1889`) reads `email_classifications` rows with `status='done'` and a `rfis` label (user_labels overrides ai_labels). It tolerates the table being missing (`_missing_table`, line 1884).
  - The agent's trigger point would be that same scan, or a hook where the classifier writes `done`, once the classifier exists.

## 1. Durable job queue: `llm_jobs` (0094, extended by 0119, 0126, 0130, 0152, 0154)
**Table** (`supabase/migrations/0094_llm_job_queue.sql:43-66`):
- Identity and target: `id`, `job_type` (CHECK list), `feature`, `target_id` (text, not an FK), `project_id`, `payload` jsonb.
- State: `status` is one of queued, running, succeeded, failed, canceled. `priority` (lower runs first).
- Attempts and lease: `attempts`, `max_attempts`, `next_attempt_at`, `claimed_by`, `lease_expires_at`.
- Errors: `error_kind`, `last_error` (a sanitized message).
- Timestamps and owner: `created_by`, `started_at`, `finished_at`, `created_at`, `updated_at`.

**Indexes:**
- `llm_jobs_active_target_uq` on `(job_type, target_id) WHERE status in ('queued','running')` allows one active job per target. A second enqueue fails with 23505.
- Also `llm_jobs_claim_idx` and `llm_jobs_running_lease_idx`.

**Claim RPC** `claim_llm_jobs(worker_id, lease_seconds, max_jobs, job_types text[])`:
- Defined in 0094:109, gains `job_types` in 0119:232-233, and has EXECUTE revoked from anon/authenticated in 0145:25.
- Uses `FOR UPDATE SKIP LOCKED` and increments `attempts` at claim time.

**Service** `app/services/llm_queue.py`:
- `_WORKER_TOKEN = uuid4().hex` per process (line 88).
- `enqueue(job_type, target_id=, project_id=, payload=, created_by=, priority=, raise_on_active=, next_attempt_at=)` (line 847). A duplicate collapses onto the existing active job, or raises `JobAlreadyActive` when asked.
- Lookups: `active_job`, `latest_job`, `queue_position`, `poll_info` (lines 910-980).
- `_claim_tick` (line 1000) claims in passes, each with its own capacity: LLM types against `llm_queue_worker_concurrency`, then the RFP sandbox, then the harvest family (`rfp_harvest_concurrency`), then AI Estimating (`_claim_plans`). This is the pattern for keeping agent jobs from starving other work.
- `_cas_job` (line 1046) fences every write on `(id, status=running, claimed_by, attempts)`.
- `renew_lease(job)` (line 1066) never raises. It returns False when the lease is lost, and the runner must then stop.
- `requeue_self(job)` (line 1093) is the graceful-shutdown handoff: status back to queued, `error_kind='interrupted'`.
- `current_job` contextvar (around line 160) exposes the running job to its runner.
- `_JobSpec(feature, run, mark, current_status, model_label, error_message)` (around line 192) is the per-type adapter, and `_spec(job_type)` (line 236) is its registry.
- `_handle_failure` / `_handle_failure_inner` (lines 1201, 1251):
  - Errors are classified with `llm_errors.classify`.
  - Transient errors retry on `llm_retry_delay_list`, from `LLM_QUEUE_RETRY_DELAYS="10,20,45,90,180"` (`app/core/config.py:342`).
  - Other errors fail terminally. `_is_lease_lost` means no writes.
- `_sweep` (line 1329) requeues running jobs whose lease expired. Once `attempts >= max_attempts` it fails them (`_INTERRUPTED_FINAL_MESSAGE`). It also prunes `llm_call_log` and terminal `llm_jobs` past `llm_call_log_retention_days`.
- Monitor actions: `requeue_terminal`, `cancel` (lines 1404, 1437).
- `worker_loop()` (line 1564) sweeps and claims each tick and runs each job via `asyncio.to_thread(_execute)`. The tick is `llm_queue_poll_interval_seconds=3`.
- Config: `llm_queue_lease_seconds=900`, `llm_queue_worker_concurrency=3` (`app/core/config.py:333-342`).

**Call log** `llm_call_log` (0094:135): `feature`, `provider`, `model`, `tier` (job, interactive or pipeline), `job_id`, `ok`, `error_kind`, `error`, `duration_ms`.
- A richer per-call record exists in `plans_reader_calls` (0152:180): `request_snapshot` jsonb, tokens, `cost_usd`, `duration_ms`, `ok`, `error_kind`. This is a good template for auditing agent LLM steps.

**Dev monitor:** `app/routers/llm_monitor.py` (`/summary`, `/failures`, `/queue`, `/jobs/{id}/retry`, `/jobs/{id}/cancel`), all `require_dev` plus `llm_monitor_rate_limit`.

## 2. The second queue pattern: `pm_rfi_sync_runs` (0156), the closest analogue
**Table** (`supabase/migrations/0156_pm_rfi_sync.sql:104-131`): `trigger` (daily, manual or email), `scheduled_for`, `target_external_rfi_id`, `source_email_id`, `requested_by`, `status` (queued, running, done, failed), `lease_until`, `lease_owner`, `attempts`, `next_attempt_at`, `stats` jsonb, `error`, plus timestamps.

**Unique indexes:**
- `pm_rfi_sync_runs_daily_uidx` is the slot ledger.
- `pm_rfi_sync_runs_email_uidx` on `(source_email_id, trigger) WHERE trigger='email'` allows one run per email.

**Service** `app/services/pm_rfi_sync.py`:
- `WORKER_ID = f"{hostname}:{pid}:{uuid6}"` (line 120).
- `enqueue_run(sb, project_id, trigger, requested_by=, target_external_rfi_id=, source_email_id=, scheduled_for=, fresh=)` (line 455).
- `schedule_daily` (line 517).
- `claim_next` (line 556) is a client-side compare-and-set claim. It skips busy projects and abandons runs once `attempts >= pm_rfi_sync_max_attempts`.
- `_yield_to_rival` (line 627) breaks same-instant double claims.
- `_renewer` (line 662) renews every 30 s and raises `_LeaseLost`.
- `_finish` (line 685) is fenced on `lease_owner`.
- `process_run` (line 1614) is the error taxonomy: transient means requeue with `next_attempt_at`, while forbidden or session-rejected means fail and call `_update_link_status`.
- `scan_emails` (line 1889) uses a checkpointed scan (`pm_rfi_email_scan_state`, single-row lease via `_claim_scan`, line 1759).
- `_email_trigger` (line 1843) is the email path.
- `tick()` (line 2083) and `polling_loop()` (line 2103).

**Email-path seam to replace:** `scan_emails` → `_email_trigger` / the classification loop → `enqueue_run(..., "email", ...)`. Pointing those enqueues at a browser-agent job instead keeps the daily path (`schedule_daily` → `process_run`) untouched.

**Shared pieces to reuse:**
- Writes: `apply_detail` (line 1277), `sync_attachments` (line 1031), `_download_to_documents` (line 1105, which writes `pm_documents` category `rfi` plus `rfi_attachments.external_file_id` dedupe).
- Mapping: `map_detail` (line 891).
- Notices: `notify_after_run` (line 1532, types `pm_rfi.sync_summary` and `pm_rfi.linked` to `NOTIFY_ROLES` = Estimating Engineer Labor and Estimating Admin).

**Config:** `pm_rfi_sync_*` settings (`app/core/config.py:1021-1032`), including `pm_rfi_sync_attachment_max_bytes=150MB`.

## 3. Single-runner leases (`graph_sync_state`) and the stale-lease incident
- **Table** (0020:79 plus a `holder` column in 0061/0083): `id` text PK, `delta_link`, `lease_until`, `holder`, `updated_at`.
- **Helpers** in `app/services/rfp_email_ingest.py`:
  - `acquire_lease(sb, key)` (line 597): insert-or-conditional-update with `or_(lease_until.is.null, lease_until.lt.now, holder.eq.token)`.
  - `renew_lease` (line 644).
  - `release_leases(sb)` (line 627): clears every row where `holder == _RUNNER_TOKEN`. It is called from the lifespan shutdown (`app/main.py:259-269`).
- **Incident** (memory file `bdr-release-2026-10-02.md`): on Railway, shutdown release did not run. `rfp-mail:lease` and `rfp_portal_sweep` stayed held about 33 minutes after every redeploy, env edits included. The fix is still open.
- **Implication for the Windows worker:** rely on short leases plus heartbeats and lease-expiry reclaim. Do not rely on a graceful release.

## 4. Scheduler / background loops (no APScheduler)
- Everything is an `asyncio.create_task(<module>.polling_loop())` in `lifespan` (`app/main.py:22-269`), gated by settings flags. Each tick does its sync work via `asyncio.to_thread`, and a failed tick never kills the loop.
- Loops: `llm_target`, `rfq_inbox`, `due_reminders`, `due_digest`, `calling_in`, `email_ingest`, `rfp_email_ingest`, `rfp_portal_ingest`, `pm_rfi_sync`, `llm_health`, `llm_queue.worker_loop`.
- Prod runs 2 uvicorn workers (`Dockerfile:33`, `--workers 2`). Every loop runs in both workers. Multi-worker safety comes from one of:
  - unique-index ledgers (`due_digest_log`, `call_in_entries`, the pm_rfi daily index);
  - SKIP LOCKED RPCs (`claim_llm_jobs`);
  - fenced leases (`graph_sync_state`, `pm_rfi_sync_runs.lease_owner`).
- Live settings propagation across workers: `app/services/llm_target.py`.
  - The single-row `llm_runtime_settings` table (0150) is re-read every `_POLL_SECONDS=10` into memory.
  - `switch()` writes the row plus the audit table `llm_target_changes`.
  - This is the template for the IT Admin "agent provider/model" setting that the remote worker pulls.

## 5. In-app notifications
- **Table `notifications`** (0006:3):
  - Base columns: `user_id`, `project_id`, `type` (free text, no CHECK), `message`, `read_at`, `created_at`.
  - Added by 0035: `dismissed_at` and `rfq_id`, plus index `notifications_user_active_idx`.
  - Added by 0118: `metadata` jsonb.
  - Added by 0091: `email_log_id`.
- **Helpers** in `app/services/notifications.py`:
  - `notify_role(role, project_id, type_, message, rfq_id=, mirror_email=True, metadata=)` refuses the estimator role.
  - `notify_user(...)`.
  - `dismiss_notifications(project_id|rfq_id, types=, type_prefixes=, user_id=, gc_id=, metadata_eq=)` requires a project or rfq scope.
  - `audit(actor_id, action, entity, entity_id, payload)`.
- **Router** `app/routers/notifications.py`: `GET /notifications` (50 rows, undismissed), `POST /{id}/read`, `POST /read-all`.
- **Frontend:**
  - `/Users/thomasmoore/Desktop/BDR/bdr_fe/components/NotificationsBell.tsx` polls every 30 s (line 136) and listens for `NOTIFICATIONS_REFRESH_EVENT`.
  - Its `destination(n)` switch on type and prefix (lines 158-290) needs a branch for new agent types (for example `browser_agent.help_needed` going to the help chat).
- **Wiring a new notification type requires:**
  - a title and button label in `_TYPE_META` (`app/services/notification_email.py:47`);
  - a deep link in `_deep_link` (line 231). `pm_rfi.*` already goes to `/pm/projects/{id}/rfis`;
  - membership in `PM_NOTIFICATION_TYPES` (`app/core/features.py:169`) if it is PM-scoped.
- **Dedupe pattern:** `rfp_harvest._notify_lock` (`app/services/rfp_harvest.py:1117`) skips the bell while an unread, undismissed one of the same type exists. Reuse this for "agent needs help" so a stuck job does not spam.
- **Batched IT Admin alerts:** `rfp_email_ingest.alert_batch` / `alert_step_failed` (`app/services/rfp_email_ingest.py:4845-4960`).

## 6. Email (Graph)
- `notification_email.queue(rows)` runs on a daemon thread per batch. It is gated by `notification_emails_enabled` (`app/core/config.py:584`) and `ms_client_id`. It renders `email_branding.render_notification_email`, sends via `graph_email.send_mail`, then links the result back through `_link_email_log`.
- `graph_email.send_mail(to, subject, body_html, cc=, attachments=, inline_images=, project_id=, rfq_id=, sent_by=, importance=)` (`app/services/graph_email.py:453`):
  - Writes `email_log` (0006:14) as queued, then sent or failed.
  - Posts to `/users/{ms_sender}/sendMail`; `ms_sender` defaults to bids@g3electrical.com.
  - Honors the RFP test-session redirect.
- `email_log` columns: `to_addrs`, `subject`, `body`, `graph_message_id`, `status`, `error`, `project_id`, `rfq_id`, `sent_by`.
- **Per-project notification log:** `app/services/notification_log.py` `build(project_id)` is a read-only merge of `notifications` and `email_log`. Router `app/routers/notification_log.py`. Agent notices that carry `project_id` appear there automatically.

## 7. Machine-to-machine auth: none exists
- The only auth is a Supabase JWT. `get_current_user` (`app/core/deps.py:72`) verifies via `app/core/security.decode_token` (JWKS RS256/ES256, or HS256 only when legacy is enabled). It loads `profiles` and enforces `aal2` (2FA) on everything except `AAL1_ALLOWED` = `GET /users/me` and `GET /features`.
- A grep for hmac, compare_digest, api-key headers and webhooks found nothing.
- The one unauthenticated route is the BC OAuth callback (`rfp_bc.callback_router`, gate plus state only).
- A Windows worker can therefore not use an existing path. A Supabase user would hit the 2FA gate. It needs a new worker credential, for example a hashed per-worker token table plus a `require_worker` dependency, a separate router prefix and rate limit.
- Role guards for the human side: `require_role(...)` (`app/core/deps.py:241`), `Role.IT_ADMIN` / `Role.ESTIMATING_ADMIN` (`app/core/roles.py:13,16`). Settings-write precedent: `require_spend_admin = require_role(IT_ADMIN, EXECUTIVE)` (`app/routers/ai_spend.py:48`), `llm_status` `PUT /target` (`require_model_admin`).

## 8. Secrets and settings storage
- The Procore password is env only today: `procore_login_email` / `procore_login_password` and the `procore_configured` property (`app/core/config.py:830-835, 1118`). Lock tuning: `procore_login_min_interval_seconds=600`, `max_failures=3`, `lock_seconds=21600`.
- No encrypted-secret table exists. `rfp_oauth_connections` (0136:189) stores BC tokens as plain text, protected by forced RLS and service-role-only access. The IT-Admin-set Procore password needs a new store (encrypted, or the same plain-text-plus-forced-RLS precedent).
- Provider keys are env only: `anthropic_api_key`, `openai_api_key`, and `self_hosted_llm_*_api_key`. There is nothing for Google, xAI or Meta.
- **Session/lock state:** `rfp_harvest_sessions` (0123:79) holds `provider` PK, `account`, `cookies` jsonb, `logged_in_at`, `last_login_attempt_at`, `login_failures`, `locked_until`, `last_error`. It is shared by the RFP harvest and the RFI sync via `pm_rfi_sync.open_session` / `_SharedLockStore` (`app/services/pm_rfi_sync.py:725-776`).
- An agent logging into Procore with the same email must respect this lock and pacing, or it will trip lockouts for the daily sweep.
- **Single-row settings pattern:** `ai_spend_settings` (0152:280, `id smallint=1 check`) plus its event trail `ai_spend_events` (0152:294); `llm_runtime_settings` plus `llm_target_changes` (0150).

## 9. File storage
- `app/services/storage.py`:
  - `BUCKET="project-files"`.
  - Paths: `build_object_path(project_id, category, filename)` → `{project}/{category}/{uuid}-{safe}`. Also `build_plans_reader_path` (line 419) and `build_bid_split_*`.
  - `upload_file(path, bytes, content_type, upsert=)` retries on TransportError up to 3 times.
  - `signed_url(path, ttl_seconds=, download=)` is memoized; `signed_url_ttl_seconds=900` (`app/core/config.py:606`).
  - Also `download_file`, `delete_file`, `copy_file`, `move_object`, `object_exists`, and prefix deletes.
- There are no signed upload URLs; all uploads proxy through the API.
- Buckets:
  - `project-files` is 450 MB (0132:190).
  - `rfp-quarantine` is 300 MB (0119:269). It is never signed.
  - `rfp-derived` is 200 MB (0119:273).
- Request caps:
  - `upload_max_bytes=450MB` (`app/core/config.py:623`).
  - `max_request_body_bytes=460MB` applies only to Form/File routes via `MaxBodySizeMiddleware` and `form_routes_matcher` (`app/main.py:301-306`).
  - Other routes get `max_json_body_bytes=16MB`.
- `_read_capped(upload, max_bytes)` (`app/routers/files.py:150`) is the bounded upload reader. Upload concurrency: `ratelimit.large_upload_slot`, `upload_rate_limit`.
- `_safe_content_type` (`app/routers/files.py`) is the house MIME rule for outside files.
- `StorageException` maps to 413/400/502 in `app/main.py:355`.
- RFI files land in `pm_documents` (category `rfi`) and are linked by `rfi_attachments(rfi_id, doc_key 'pm:<uuid>', external_file_id)` (0068:88, 0156).
- Chat-with-files precedent: `plans_reader_chat_threads` / `plans_reader_chat_messages` (0152:244-278). Columns: `role`, `content`, `tool_calls` jsonb, `status`, `llm_job_id`, `created_by`.

## 10. Audit / event tables to reuse or mirror
- **`audit_log`** (0006:34): `actor_id`, `action`, `entity`, `entity_id` uuid, `payload` jsonb. Written by `notifications.audit()` (about 300 call sites). It feeds `/analytics/activity` (`app/routers/analytics.py:250`), which merges it with stage events.
- **Append-only stage ledgers:** `stage_events` (0003:27: `from_stage`, `to_stage`, `actor_id`, `note`) and `pm_stage_events` (0057:65).
- **`rfp_test_events`** (0131:56) is the closest structured step ledger. Columns: bigint identity, `session_id`, `at`, `source`, `kind`, `level` (info, warn, error), `title`, FK refs that are `on delete set null`, and `detail` jsonb capped at 64 KB by the writer. It is the best template for a per-job agent step log.
- Others: `llm_call_log`, `plans_reader_calls`, `llm_target_changes`, `ai_spend_events`, `proposal_send_events` (0104), `project_open_events` (0117), `due_reminder_log` / `due_digest_log`, `email_log`.
- Retention precedent: the `llm_queue._sweep` hourly prune.

## Bottom line for the architect
The backend has no remote-worker concept at all. The remote worker will need four new things:
1. **A worker credential.** No machine auth exists, and a normal user token is blocked by the 2FA gate.
2. **A claim/heartbeat API.** Modeled on `claim_llm_jobs` (SKIP LOCKED) plus `_cas_job` fencing on `(claimed_by, attempts)`, with short leases. The Railway stale-lease incident shows that release on shutdown is unreliable.
3. **A per-job step ledger.** Modeled on `rfp_test_events`.
4. **A help thread with files.** Modeled on `plans_reader_chat_*`, with storage paths built like `build_object_path`.

A dedicated `agent_jobs` table is likely cleaner than adding to `llm_jobs`. The in-process `_spec` / `_execute` model does not fit a remote worker, and every new `job_type` means another drop-and-re-add of the CHECK constraint.

Notifications can go through `notify_role(Role.ESTIMATING_ADMIN, ...)` or `notify_user(...)` with metadata. Gate the "needs help" bell with the `_notify_lock` dedupe so a stuck job does not spam.

## frontend

Here is the map of the frontend pieces in `/Users/thomasmoore/Desktop/BDR/bdr_fe` (Next 16.2.7, React 19.2.4, i18next 25). I changed no files. There is no agent UI yet, so everything below is something an agent UI can reuse or plug into.

## 1. Settings page: `app/(app)/settings/page.tsx` (87 lines)
- **Layout:** one column (`mx-auto max-w-2xl space-y-6`), each block a `Card` or a self-contained component. Gating reads `const { profile, features } = useAuth()` (line 14).
- **Existing gates:**
  - Users card (lines 29-39): `it_admin || executive`, with a `ButtonLink` to the `/settings/users` sub-page.
  - RFP Ingestion card (lines 44-54): `features?.rfp_email_ingest === true && canManageRfpSenders(role)`, linking to `/settings/rfp-ingestion`.
  - `<ModelServerSettings/>` (line 58): `role === "it_admin" || profile.is_dev`. This is the closest precedent for an IT Admin "agent model / provider" card.
  - `<AiSpendSettings/>` (line 65): any role except estimator, plus `features.ai_estimating`. Editing is limited inside the card to IT Admin and Executive.
  - Dev role switcher (line 69).
- **Sub-page pattern:** `app/(app)/settings/rfp-ingestion/page.tsx` (119 lines). It computes `allowed`, does `useEffect(() => { if (!loading && profile && !allowed) router.replace("/settings") })` (lines 72-76), returns `null` until it knows the role (line 81), then stacks sections: `RfpBlockedSendersSection`, `RfpHarvestStatusSection` (line 100), `RfpNgemStatusSection` (line 106), `RfpBuildingConnectedSection`.
- **`components/ModelServerSettings.tsx` (214 lines):**
  - Reads `GET /model-status/target` (line 68) and switches with a `POST` to the same path (line 87).
  - Option shape: `TargetOption {key,label,detail,model,available,problem,is_default,is_live}`.
  - The backend probes the box before switching; an unhealthy box gets a "Switch anyway" confirm. It also shows `recent_changes`, an audit-style list.
  - A provider/model picker for the agent can copy this card.
- **`components/RfpHarvestStatusSection.tsx` (345 lines):** the read-only Procore login-status block. It shows `GET /rfp-emails/harvest-status` with `{enabled, configured, account, logged_in_at, last_used_at, last_login_attempt_at, login_failures, locked_until, last_error, active_jobs}` and has a Refresh button. The comment at line 15 says it never shows the password or cookies. A worker-health / agent-login status block can copy this shape.
- **No secret-entry UI exists in Settings yet.** `type="password"` appears only in auth pages and `components/AccountSettings.tsx:323-343`. A write-only "Procore password" field is new work. The convention to keep is that the backend never returns the secret, only `configured: bool`.

## 2. NotificationsBell: `components/NotificationsBell.tsx` (344 lines)
- Mounted in `components/AppShell.tsx:105`, next to `HelpButton` (line 104).
- **Data:** `GET /notifications`, polled every 30s with `setInterval` (line 136). It also reloads on the window event `NOTIFICATIONS_REFRESH_EVENT` (`lib/api.ts:282`, fired by `refreshNotifications()`).
- **Actions:** `POST /notifications/{id}/read` (optimistic) and `POST /notifications/read-all`.
- **Row type:** `{id, message, type, project_id, read_at, created_at, metadata?: {gc_id, job_id, run_id, invitation_id, row_id, source}}` (lines 13-30). An agent task id would need a new metadata key, e.g. `task_id`.
- **Routing:** `destination(n)` (lines 154-278) is a chain of `if` checks on `n.type`.
  - Project-less types use a const plus a feature-flag gate and build a query-param deep link, e.g. `bid_split.finished` goes to `/bid-splitter?job=`, and `rfp_processing.step_failed` goes to `/rfp-processing?lane=stuck&email=`.
  - Rows whose destination is off or unknown return `null` and become inert.
  - PM types are listed in `PM_NOTIFICATION_TYPES` (`lib/features.ts:145-152`, which includes `pm_rfi.sync_summary` and `pm_rfi.linked`). It must stay in step with backend `PM_NOTIFICATION_TYPES` in `bdr_be/app/core/features.py`. Any `pm_rfi.*` type goes to `/pm/projects/{id}/rfis` (lines 254-256).
- **For a help request:** add a const (e.g. `agent.help_needed`) and route it to a help page with `?task=<metadata.task_id>`, gated on a new feature flag. Messages are stored in English on the backend; localizing notifications is unfinished i18n Phase 2.

## 3. PM RFI UI
- **`app/(app)/pm/projects/[id]/rfis/page.tsx` (39 lines):** uses `usePmProject()` and `canWritePm(role)`. It renders `<PmLinkCard projectId canWrite onSynced/>` only when `features?.pm_rfi_sync === true`, then `<RfisSection projectId canWrite refreshKey/>`. `refreshKey` is bumped when a sync finishes.
- **`components/pm/field/PmLinkCard.tsx` (343 lines):**
  - Calls `GET` / `PUT /pm/projects/{id}/pm-link` and `POST /pm/projects/{id}/rfis/sync`, which returns `{run_id, status}`.
  - Polls every `POLL_MS = 5000` (line 25) only while `link.running`, and spots the running-to-idle transition to call `onSynced` (lines 85-90, 110-115).
  - `dirtyRef` stops a poll from overwriting a form the user is editing.
  - Shows `last_sync_status` with a tone helper (`statusTone`, line 43), `last_error` in a danger `Callout`, and a `linked_via === "email"` callout (line 259).
  - **Agent status for an RFI (queued, running, waiting on help) fits here** as an extra row or callout, or as a separate "Agent activity" card.
- **`PmExternalLink` type** (`lib/types.ts:427-442`): `{platform, host, external_company_id, external_project_id, external_url, sync_enabled, linked_via:"manual"|"email"|null, last_synced_at, last_sync_status, last_error, backfilled_at, running}`.
- **`components/pm/field/RfisSection.tsx` (792 lines):**
  - `GET /pm/projects/{id}/rfis` returns `Rfi[]`, sorted by `compareRfis`.
  - A Source column (lines 191-228) shows "Procore" and "G3" badges.
  - It contains the `RfiEditor` (line 406) and opens `RfiDetailModal`, `RfiCloseModal`, `RfiSendModal` and `RfiMarkSentModal`.
- **`components/pm/field/RfiDetailModal.tsx` (597 lines):**
  - A ruled "document form" layout (`BandRow`, `LabelValue`).
  - `ProcoreFacts` (line 388) and `ProcoreRfiBody` (line 411) render the imported fields.
  - `RfiLinkModal` / `RfiUnlinkModal` call `POST .../rfis/{id}/link` and `/unlink`.
  - Attachments render through `RfiAttachmentList`.
- **`components/pm/field/rfiDisplay.ts`:** `isImported`, `isLinked`, `internalRfiNo`, `externalRfiNo`, `rfiDisplayNo`, `compareRfis`, `safeExternalUrl` (https only).
- **`Rfi` type** (`lib/types.ts:365-421`):
  - `source: "internal"|"procore"`
  - `external_id`, `external_number`, `external_revision`, `external_status`, `external_url`
  - `responsible_contractor`, `received_from`, `initiated_at`, `ball_in_court`, `external_due_date`, `external_synced_at`
  - `read_only`, `attachments: RfiAttachment[]`, `answer_attachments`
  - `RfiAttachment = {id, key, filename, folder, source, size_bytes}` (line 352)
  - An agent-imported row would fit this shape; it may need a provenance field such as `imported_by: "agent"|"web_session"`.
- **`RfiAttachmentList({projectId, items, onRemove?, onPreviewOpenChange?, emptyText?, className?})`** (`RfiAttachmentPicker.tsx:487-520`): chips with click-to-preview through `useFilePreview(HUB_DOCUMENTS)`.
- **`components/pm/PmModuleNav.tsx:14-22`:** a `MODULES` array of `{seg, labelKey}` for the PM project tabs (`/rfis` is one).

## 4. Chat UI to reuse: `components/aiEstimating/ChatDrawer.tsx` (446 lines)
- **Props:** `{runId, open, onClose, pageCap, disabledReason, deliverables, onOpenDeliverable, onAnswered}`. It is tied to a plans-reader run and its deliverables, so it would need generalizing or copying.
- **Structure:**
  - A drawer: `<aside className="panel-elevated fixed inset-y-0 end-0 z-40 flex w-full flex-col sm:w-[30rem]">`.
  - A header with a close `IconX`, then a thread `Select` plus a "New thread" button.
  - A scrolling message list, then a composer: `Textarea` (Enter to send), a checkbox and Send.
- **Message bubble:** `mine = m.role === "user"`. User bubbles use `border-navy-soft-line bg-navy-soft`; others use `border-hairline bg-surface`, inside `max-w-[90%] rounded-card border px-3 py-2`. Tool calls render in a `<details>` element (a good fit for agent step traces).
- **Polling:** recursive `setTimeout(tick, CHAT_POLL_MS)` with `CHAT_POLL_MS = 2000` (`lib/aiEstimating.ts:1113`), only while `threadHasPendingAnswer(messages)`. Per-thread sequence refs throw away stale reads; `wasPendingRef` detects a pending-to-settled transition.
- **API helpers** (`lib/aiEstimating.ts:992-1016`): `listThreads`, `createThread`, `getThread`, `sendMessage` (via `postJson`).
- **Types** (`lib/aiEstimating.ts:634-675`): `ChatThread {id, run_id, title, created_by, created_at, updated_at}` and `ChatMessage {id, thread_id, role, content, tool_calls, status, error, tokens, cost_usd, llm_job_id, created_by, created_at}`. These are a good model for agent help-message and audit-step rows.
- **The chat has no file upload.** Uploads go through `uploadRunFile(runId, file, label)`, a FormData `POST` (`lib/aiEstimating.ts:890-895`). `components/aiEstimating/uploadStore.ts` (198 lines) is a module-level upload queue using `withRateLimitRetry`, with `startRunUploads`, `useRunUploads` and `onRunFileUploaded`.
- **Other thread-like UI:** `components/NoteList.tsx` (`Note`, `NoteList`, `IntakeNoteCard`) and `ProjectNotesPanel.tsx` (175 lines) are text-only note lists. There is no other chat with attachments.

## 5. DropZone: `components/ui/DropZone.tsx` (128 lines)
- **Props:** `{onFiles(files: File[]), onFolderFiles?(DroppedFile[]), folders?: "reject"|"flatten" (default reject), accept?, multiple?=true, disabled?, label?, hint?, className?}`.
- Folder traversal goes through `collectDroppedFiles` and `MAX_DROPPED_FILES` from `lib/folderDrop.ts`.
- i18n keys: `dropZone.tooManyFiles`, `dropZone.foldersNotAllowed`, `dropZone.emptyFolder`.
- **For chat uploads:** use `DropZone` in compact form (`className` with small padding) above the composer. The FormData `apiFetch` drops `Content-Type`, so multipart works as-is.

## 6. API client: `lib/api.ts`
- **`apiFetch<T>(path, options)`** (line 153):
  - Gets the Supabase session, sets the bearer token and the impersonation header.
  - Sets JSON `Content-Type` unless the body is `FormData`.
  - On an error it throws `ApiError(status, message, retryAfter, code)`. `code` comes from the `X-Error-Code` header; 422 validation lists are flattened to text.
  - Returns `undefined` on 204.
  - Base URL is `NEXT_PUBLIC_API_BASE_URL`, default `http://localhost:8000`.
- **Also exported:** `apiFetchBlob` (downloads, with the filename from `Content-Disposition`), `withRateLimitRetry(run, maxWaits=5)`, `refreshNotifications()`.
- **Convention:** each feature has a `lib/<feature>.ts` with its types and thin wrappers (`lib/aiEstimating.ts`, `lib/rfpEmails.ts`, `lib/callingIn.ts`, and others). Role lists and `canX(role)` helpers live there too, e.g. `lib/rfpEmails.ts:28-64` with `RFP_EMAIL_VIEW_ROLES`, `canManageRfpSenders` and `canManageRfpBlocks(role, isDev)`.

## 7. Feature flags: `lib/features.ts` (182 lines)
- `Features` (lines 16-65) is loaded once at sign-in from `GET /features` in `lib/auth.tsx:87`. If the fetch fails it falls back to `ALL_ENABLED` (lines 85-97).
- **Non-sub-app flags fail closed:** they are optional keys (`pm_rfi_sync?: boolean`) set to `false` in `ALL_ENABLED`, and the UI tests `features?.x === true`.
- **A new flag (e.g. `browser_agents?`) needs:** the type key, an `ALL_ENABLED` entry, a backend `/features` entry, and a decision in `subAppForPath` (lines 112-133). Pages that belong to no sub-app (`/settings`, `/ai-monitor`) return `null` there.

## 8. Roles and auth
- **`Role`** (`lib/types.ts:6-16`): `estimating_engineer_materials | estimating_engineer_labor | estimating_admin | executive | accountant | it_admin | estimator`.
- **Helpers:** `WRITER_ROLES` (line 76), `canWrite`, `canWritePm` (line 168), `ROLE_LABELS`.
- **`Profile`** (`lib/auth.tsx:13-32`): `{id, full_name, email, role, is_active, is_dev, locale?, rfp_mailboxes?, ...}`.
- `useAuth()` returns `{session, profile, features, loading, refreshProfile, signOut}`.
- For "Estimating Admins answer help requests", add `AGENT_HELP_ROLES = ["estimating_admin", ...]` and a `canAnswerAgentHelp` helper following the `lib/rfpEmails.ts` pattern.

## 9. Sidebar: `components/Sidebar.tsx` (532 lines)
- **`NavItem`** (lines 60-74): `{href, labelKey, icon, roles?: Role[], devOnly?, featureFlag?: keyof Features | readonly (keyof Features)[], alsoActiveOn?}`.
- Visibility filter (lines 324-329): roles, then `devOnly`, then `featureFlagOpen`. Flags fail closed while loading.
- **Nav sets:**
  - `BIDDING_NAV` (lines 200-218)
  - `PM_NAV` (lines 222-228): `/pm`, `/pm/emails`, `/pm/submittals`, `/pm/vendors`, `/pm/contacts`
  - `CP_NAV`
  - Shared items are defined as constants (`AI_MONITOR_NAV`, `RFP_HUB_NAV`, ...) and carried over when Bidding is off (lines 313-323).
- **Count badges:** the RFP hub and Calling In show a count bubble from an app-wide provider hook (lines 282-288 and 451-503; red when `waitingDanger`). An "Agents" nav item with a red count of open help requests would follow this pattern.
- The `ModelStatusIndicator` (always visible) is pinned at the bottom (lines 525-531).
- **Icons** (`components/ui/icons.tsx`) are inline SVGs. There is no bot, cpu or message icon, so one would need adding (lucide-style, `currentColor`). Existing: Dashboard, Scale, ListTodo, Chart, Building, Users, ClipboardList, CalendarDays, FolderKanban, Mail, FileText, Briefcase, Dollar, BookOpen, Sparkles, Compass, CursorClick, Settings, Banknote, Plus, ExternalLink, Pencil, Lock, Bell, Activity, Refresh, HelpCircle, Search, ChevronLeft, ChevronRight, Link, Check, X, UserCheck, Maximize, Flag, Trash, Phone.

## 10. Polling and realtime
- **No Supabase realtime anywhere.** No `.channel(` or `postgres_changes` in `app`, `components` or `lib`. Everything polls.
- **Polling patterns:**
  - App-wide providers in `app/(app)/layout.tsx` (lines 6-26): `BidSplitterActivityProvider` and `RfpEmailsActivityProvider`.
  - `components/RfpEmailsActivity.tsx:100-117` polls on `setInterval`, skips while `document.visibilityState !== "visible"`, refreshes on `visibilitychange`, and drops stale reads with sequence refs. It masks values when the flag or role turns off.
  - `components/BidSplitterActivity.tsx` does the same and calls `refreshNotifications()` when a job leaves the processing set.
  - Per-component polling: `PmLinkCard` (5s while running), `ChatDrawer` (2s while pending), `ai-monitor` (`QUEUE_POLL_MS`), `ModelStatusIndicator`.
- An `AgentActivityProvider` (open help count, live task states) would copy `RfpEmailsActivity`.

## 11. Audit-trail UI precedent: `app/(app)/ai-monitor/page.tsx` (673 lines)
- Dev-only: `profile.is_dev`, otherwise `router.replace(homePath(features))` (lines 644-656).
- Uses `Tabs`, the `Table` family, `StatTrend`, `TrendChart`, `DonutChart` and a polled queue view, against `GET /llm-monitor/...`. Response types are kept local to the page.
- `lib/aiQueue.ts` `aiQueueStatus(queue)` turns queued / retrying / running attempt into i18n keys under `aiQueue.*`. That fits lease/retry display for agent tasks.

## 12. UI kit: `components/ui/index.ts`
- **Barrel exports:** Button, ButtonLink, IconButton, Card, CardHeader, CardFooter, Input, Checkbox, Select, ComboSelect, SearchSelect, Textarea, RichTextEditor, RichText, Field, Label, Badge, RoleBadge, Callout, Modal, Tabs, Table/THead/TBody/TR/TH/TD, StepperChip, EmptyState, SectionHeader, ErrorText, InfoHint, HoverCard, DateTime, Spinner, DropZone, StatTrend, charts, icons.
- **Prop options:**

| Component | Options |
|---|---|
| `Button` | variant `primary\|secondary\|success\|danger\|ghost\|link`; size `sm\|md\|lg`; `loading`, `iconLeft` |
| `Badge` | tone `neutral\|ok\|warn\|danger\|info\|navy`; variant `soft\|solid`; size `sm\|md` |
| `Callout` | tone `info\|warn\|neutral\|danger\|success` |
| `Card` | variant `raised\|inset\|flat`; padding `none\|sm\|md\|lg` |
| `Modal` | `{open, onClose, size: sm\|md\|lg\|xl\|full, suppressClose, labelledBy}`; parts `Modal.Header`, `Modal.Body`, `Modal.Footer` |
| `Tabs` | `{value, onValueChange, items: {value,label,badge?}[], variant: underline\|segmented}` |

- **Design rules:**
  - Neumorphic navy-on-light, light only, no glass.
  - Floating panels use `.panel-elevated` and stay opaque.
  - Logical CSS properties only (`start`/`end`/`ps`/`text-start`) because `ur` is right-to-left.
  - Combine classes with `cn` from `lib/cn.ts`.
  - Show timestamps with `fmtDateTime` / `fmtDay` from `lib/format.ts` (Pacific time).
  - `FilePreviewModal` (`components/FilePreviewModal.tsx:17`) props: `{filename, url, kind: pdf|image|text, subtitle?, newTabUrl?, onDownloadOriginal?, onClose}`.

## 13. Translations (i18n)
- `lib/i18n.ts` uses one `translation` namespace. English is bundled; the other 5 languages load on demand and fall back to English. Locales are set in `lib/locales.ts`: `["en","fil","ceb","sw","hi","ur"]`, and `ur` is right-to-left.
- **Catalogs:** `locales/<lng>/translation.json`, nested keys with a camelCase prefix per feature (`pmRfiSync.*`, `pmField.*`, `aiEstimating.chat.*`, `modelServer.*`, `notificationsBell.*`, `dropZone.*`, `settings.<card>.*`, `nav.*`).
- English has 170 top-level keys; the other 5 have 108 each. `pmRfiSync` exists only in `en`. `settings.rfpBlocked` and all the `nav.*` keys are also missing from the non-English files.
- New keys go in `en`. Translations come from `bdr_be` `scripts/translate_catalog.py`; adding keys to `en` alone also works, because missing keys fall back to English.
- `t()` is called with keys (sometimes with `defaultValue`).

## 14. Other notes
- `bdr_fe/AGENTS.md`: Next 16 has breaking changes; read `node_modules/next/dist/docs/` before writing code.
- Lint is strict `react-hooks` (`set-state-in-effect`). Existing code works around it by setting state from promise chains (see the comments in `PmLinkCard` and `RfisSection`).
- Tests: `node --test lib/__tests__/**/*.test.mjs` covers pure `lib/` helpers only.
- The frontend has no code for 0154 RFI email classification yet; the only 0154-0156 references in `lib`, `components` and `app` are about 0155 and 0156. PM emails use `components/pm/PmEmailsPanel.tsx`, `app/(app)/pm/emails/page.tsx` and `components/emails/*`.
- The in-app help center (`app/(app)/help/[topic]`, `lib/help/registry.ts`) can hold an "Agents" topic.

## Suggested new UI pieces (inferred from the above)
- **Settings > "Browser agents" card:** IT Admin, copied from `ModelServerSettings`. Holds the provider/model select, a write-only Procore password with `configured` status, and the worker list with heartbeat and lease status.
- **`/agents` page:** shared, outside any sub-app. Lists tasks with an audit timeline and a help inbox, gated by a fail-closed flag. Nav item uses `roles` plus a count badge from an `AgentActivityProvider`.
- **Help chat:** generalize `ChatDrawer` (thread list, bubbles, `<details>` step traces, 2s polling) and add a compact `DropZone` with a FormData upload helper.
- **Bell:** add `agent.help_needed` / `agent.task_failed` cases in `destination()` that use `metadata.task_id`.
- **RFI page:** show agent status in `PmLinkCard` or a new card; agent-imported rows reuse the `Rfi` `source='procore'` shape and `RfiAttachmentList`.

## procore-client

The Procore login is plain HTTP and needs no browser. That login, the cookie session and the RFI JSON reads in `procore_client.py` can be reused almost as they are. Procore's RFI notification emails contain the exact RFI URL, so an agent job can be started straight from the email. Two gaps for the planned design: the Procore password lives only in an env var today, and nothing in the codebase stores encrypted secrets yet.

## 1. `bdr_be/app/services/procore_client.py` (1216 lines)

**Design (docstring, lines 1-30).** It sends plain `httpx` requests to the same JSON endpoints the Procore web page calls. There is no browser. It enforces three rules itself:
- **Allowlist.** Requests can only go to listed paths.
- **Pace.** Requests are spaced out to a human speed.
- **Login discipline.** It logs in only after a request proves the session is gone. The password appears only in the login POST body, never in logs, errors or rows.

**Hosts (lines 51-59):**
- `APP_HOST = app.procore.com`
- `US02_HOST = us02.procore.com`
- `APP_HOSTS` covers both
- `LOGIN_HOST = login.procore.com`, shared by both regions
- `STORAGE_HOST = storage.procore.com`

The User-Agent is a hardcoded desktop Chrome 151 string on a Mac (lines 65-68).

**Allowlists:**
- `ALLOWED_PATHS` (lines 82-88): the 5 bid harvest GETs, ids up to 12 digits.
- `RFI_ALLOWED_PATHS` (lines 98-103), all GET, ids up to 20 digits (`_LONG_ID`, because us02 ids run to 15 digits):
  - `/rest/v1.0/projects/{p}/rfis`
  - `/rest/v1.0/projects/{p}/rfis/{r}`
  - `/rest/v1.0/companies`
  - `/rest/v1.0/projects/{p}`
- `_PROJECT_HOME_RE` (line 106): `/{p}/project/home`, used only to read the redirect Location.
- Login chain paths: `_LOGIN_APP_PATHS` (lines 107-114) and `_LOGIN_LOGIN_PATHS` (lines 115-121), which are `/`, `/oauth/authorize`, `/login/password`, `/sessions/submit_login_email` and `/sessions/submit_login_password`.
- `_MAX_HOPS = 8`. The success cookie is `_SESSION_COOKIE = "_session_id"` on the app host (line 128).

**Errors (lines 134-175):**

| Error | Meaning |
|---|---|
| `ProcoreTransient` | network, 5xx, 429 |
| `ProcoreUnavailable(locked_until)` | no credentials, locked, interstitial page, recent login attempt |
| `ProcoreLoginLocked` | login failure cap reached |
| `ProcoreLoginFailed(step, msg)` | one attempt failed at a named step |
| `ProcoreSessionExpired` | internal: session gone |
| `ProcoreForbidden` | 403/404, permanent |
| `ProcoreSessionRejected` | RFI read still unauthenticated right after a fresh login |

**RFI parsing helpers (pure functions, reusable on the BE side to seed jobs):**
- `unwrap_link` (line 217) unwraps Outlook safelinks.
- `ProcoreProjectRef(host, project_id, company_id)` (line 307) has a `.project_url` property.
- `ProcoreRfiLink(host, project_id, rfi_id)` (line 321).
- `_RFI_SHOW_RE` (line 329) matches `^/{p}/project/rfi/show/{r}/?$`.
- `parse_project_url` (line 358) accepts the webclient path `/webclients/host/companies/{c}/projects/{p}/...`, `/companies/{c}/projects/{p}`, `/{p}/project/...` and the `companyId` query parameter.
- `extract_rfi_links(body)` (line 389) returns deduped RFI show links, app/us02 hosts only.
- `extract_company_id(body, project_id)` (line 414) reads `companyId` / `projectId` from signed `storage.procore.com` links.

**Session store.** `SessionStore` protocol (line 441): `load`, `save_cookies(account, cookies)`, `record_login(ok, error)`, `touch`. `MemorySessionStore` (line 452) implements it for tests. The cookie format is `[{name, value, domain, path, expires, secure}]` (`_export_cookies`, line 716).

**Pace.** `_pace` (line 501) is a lock held for the whole process: `min_interval * U(1, 2)` between any two Procore requests. It is in memory only, so it is not shared across processes or machines.

**Config.** `ProcoreConfig(email, password, min_request_interval=2.0, login_min_interval=600, timeout=30)` (line 597).

**`ProcoreSession` (line 633).** It takes `app_host` (one of the two app hosts), an `on_lock` callback, and test seams. Redirects are not followed automatically.
- `_load_store` (line 693) loads the stored cookie jar and skips expired cookies. A jar saved for a different account is ignored.
- `_request` (line 742) rejects any host other than app_host or login, paces the call, and maps 429/5xx to Transient.
- `_get_json_once` (line 773):
  - 401, or a redirect to login or `/auth/procore`: SessionExpired
  - any other redirect: Forbidden
  - 403/404: Forbidden
  - a Cloudflare challenge (`_looks_like_challenge`, line 563: `cf-mitigated` header, or "challenge-platform" plus "just a moment" / "cf-chl"): Unavailable
  - non-JSON content type: Unavailable
  - body over 8 MB: Forbidden
- `get_rfi_json` (line 809) adds the `Procore-Company-Id` header and retries once after a login. A second SessionExpired raises `ProcoreSessionRejected`.
- `list_rfis` (line 839) returns all rows with no `per_page`. List rows have no answers or attachments.
- `get_rfi(project, rfi, company)` (line 854) returns the detail: questions, answers, attachments.
- `resolve_company_id` (line 867) tries the project home redirect, then loops over `/companies` (at most 10) asking each for the project.

**Login (`ensure_session`, line 977).**
1. Credentials must be configured and the account must not be locked.
2. If another worker saved a newer jar, it adopts that jar instead of logging in (`_jar_is_newer`).
3. It refuses if the last attempt was less than 600 s ago, raising `Unavailable(locked_until)`.
4. It runs `_login()`. On failure, `record_login(ok=False)` runs; if that reaches the cap, it calls `on_lock` and raises Locked.
5. On success it saves the cookies.

`_login` (line 1060) steps:
1. GET the continue URL or `/auth/procore`, then follow redirects by hand (`_follow`, line 1032, same site only, at most 8 hops). A challenge fails at step "challenge".
2. Parse the email form (`parse_login_form` collects every hidden field, including `authenticity_token`). POST `session[email]` with `session[remember_me]=true` and expect a redirect to `/login/password`.
3. GET the password page, parse its form, POST `session[password]`. A redirect back to `/login/password` or `/` means the password was rejected.
4. Follow the OAuth callback. Success means `_session_id` exists on the app host.

The failure steps are named: `challenge`, `login_page`, `email`, `password_page`, `password`, `callback`, `redirect`.

**MFA and CAPTCHA handling:** there is none. `docs/RFP_HARVEST.md:40` says "No MFA on the account." A Cloudflare challenge is only detected, then fails. There is no SSO or verification-code branch, so any new page in the chain fails as `redirect` or `login_page`.

**Downloads.** `download(url, dest, max_bytes)` (line 1155) accepts only `https://storage.procore.com` URLs and sends no cookies. It follows one redirect, only to `*.amazonaws.com`, and writes the file with `O_EXCL`.

## 2. Where the session is stored and how callers use it

**Table `rfp_harvest_sessions`** (`supabase/migrations/0123_rfp_harvest.sql:80-101`):
- Columns: `provider` (primary key), `account`, `cookies` (jsonb), `logged_in_at`, `last_used_at`, `last_login_attempt_at`, `login_failures`, `locked_until`, `last_error`, `updated_at`.
- Forced RLS, service role only. The comment calls the cookies "a bearer credential", never returned by any route.
- Rows: `procore` (app.procore.com) and `procore:us02.procore.com`. Other harvesters use `pipelinesuite:<host>` and `smartbid` rows.

**`app/services/rfp_harvest.py`:**
- `SESSIONS_TABLE` (line 88).
- `_SessionStore(settings, provider)` (lines 1032-1110) is the Supabase version of the store. It applies the lock policy from settings (max failures, lock seconds) and limits `touch` writes.
- `_notify_lock` (line 1113) sends one deduped notification to every IT Admin (`NOTIFY_LOGIN_FAILED`, `mirror_email=False`). Its text says "Check PROCORE_LOGIN_EMAIL and PROCORE_LOGIN_PASSWORD."
- `procore_config(settings)` (line 1168) and `open_session()` (line 1178).
- `availability(settings, provider)` (line 1247) checks whether Procore is usable without contacting it.
- `session_provider_for` (line 967).

**`app/services/pm_rfi_sync.py`:**
- `session_provider(host)` (line 717).
- `_SharedLockStore` (line 725): us02 keeps its cookies in its own row, but its lock, failure count and login spacing are merged with the shared `procore` row.
- `open_session(host)` (line 760) logs a warning that us02 is an "untested host". `docs/PM_RFI_SYNC.md:243` says the same.

**Credentials (`app/core/config.py`):**
- Lines 830-836: `procore_login_email`, `procore_login_password`, `procore_min_request_interval_seconds` (2.0), `procore_login_min_interval_seconds` (600), `procore_login_max_failures` (3), `procore_login_lock_seconds` (21600), `procore_request_timeout_seconds` (30).
- Line 1118: the `procore_configured` property.
- Lines 1551-1565: validators.

The password is **env only today**. There is no IT Admin settings field for it and no encrypted secret store anywhere in the repo. API keys are env-only too (config.py:138-166). Similar runtime settings tables exist (`llm_runtime_settings` in 0150, `ai_spend_settings` in 0152), but neither holds secrets. Storing the password in Settings therefore needs a new encrypted column or Supabase Vault, plus a way to hand it to the worker.

**The email-triggered path being replaced (`pm_rfi_sync.py`):**
- `scan_emails` (line 1889) watches two sources:
  - `ingested_emails` where `body_text ilike '*project*rfi*show*'`, filed to a PM project. It calls `extract_rfi_links` and then `_email_trigger` (line 1843).
  - `email_classifications` rows labeled `category == "rfis"` (from 0154).
- Both call `enqueue_run(sb, project_id, "email", target_external_rfi_id=..., source_email_id=...)` (line 455).
- `_trusted_for_autolink` (line 1819) accepts mail from `*@procore.com` / `*@procoretech.com`, or with a signed storage link whose `projectId` matches. `auto_link_from_email` (line 1780) creates the `pm_external_links` row.
- **Gap:** a classified email with no RFI link queues an untargeted run (no `target_external_rfi_id`). It needs an already linked project.

## 3. What an RFI notification email contains (scratch samples)

`scratch/procore_rfi_email_raw.json` is the Graph message for Const-198. `scratch/procore_rfi_samples/*.json` / `*.txt` hold 6 emails.

**PM-phase RFI notifications** (Cobblestone samples: closed_const188_02, closed_const198, response_const189_1, response_const196):
- **From:** `Cobblestone_Construction_notifications@procoretech.com`. The display name is the actor, e.g. "Nenette Inovero (Cobblestone Construction)".
- **Subject:** `"{job#}, {project name}: RFI Closed, #Const-198, EF3 Circuit Clarification"` or `"...: Response to RFI #Const-196, Paver Edging"`. The RFI number has a GC prefix ("Const-", "Const -188.02", "Const-189.1"), and the event type is "Closed" or "Response to".
- **Body (HTML):** actor and action, the GC, the project, "Ball in court", due date, the response text with author and timestamp, the Question (asked by, location, text), attachment file names, "Official Responses", and Additional Information (Responsible Contractor, Date Initiated, Subject, Schedule/Cost Impact, Distribution List).
- **Links (behind safelinks):**
  - "View in Procore": `https://app.procore.com/3167322/project/rfi/show/27914290`. This is exactly what `extract_rfi_links` parses: host, project_id, rfi_id.
  - One signed `storage.procore.com/api/v5/files/us-east-1/pro-core.com/3336-c/2237613-p/<fileid>?companyId=6333&projectId=3167322&sig=...` link per attachment.
  - One `.../procore-pdf/...` link, which is the "Download as PDF" of the whole RFI.
  - `support.procore.com`.
- **Headers:** the Message-ID encodes company and RFI, e.g. `<c6333-t27914290-...@procore.com>` (`c` = company id, `t` = RFI id). Reply-To is `do-not-reply@procore.com`; Return-Path is `@email.procoretech.com`. Only the raw file captured headers; the sample JSONs did not.
- `hasAttachments` is False. The files arrive only as signed links. These download without cookies through `ProcoreSession.download`, so even a backend with no login could pull them.

So an agent job can be seeded with `rfi_url = https://{host}/{project}/project/rfi/show/{rfi}`, the host, project_id, rfi_id and company_id (from the storage link's `companyId`, or from the Message-ID `c<id>`), plus the RFI number and event type from the subject.

**Bid-phase "RFI" correspondence** (Boyd samples: boyd_rfi20, boyd_additional):
- From `Boyd_Martin_Construction@us02.procoretech.com`. Subjects "RFI #20 - Mechanical Controls" and "Additional RFI Responses".
- **No `/project/rfi/show/` link.** The links are:
  - `us02.procore.com/{company}/company/planroom/route_to_bid_sheet/{bid}?from_email=correspondence`
  - storage links with `companyId=598134325511223&projectId=598134326762832` (15-digit us02 ids)
  - `streaming-us02.procore.com/download?uuid=...&company_id=...`
- These are bid package correspondence, not PM RFIs. `extract_rfi_links` returns nothing for them, and `parse_reference` only matches `app.procore.com`, so it would miss this us02 bid sheet. An RFI-classified email can therefore lack an RFI URL. The agent must handle "no direct link" (navigate from the project or bid sheet), or the classifier must exclude these.

## 4. What can fail

- **CAPTCHA / Cloudflare:** detected (`_looks_like_challenge`) but never solved. The docs say it never happened with plain httpx (`docs/RFP_HARVEST.md:829`).
- **MFA:** no handling. If Procore turns on MFA or SSO for the account, the password step fails with an unexpected redirect.
- **Bad credentials:** fail at `email` or `password`. The 3rd consecutive failure locks logins for 6 h and notifies IT Admins once.
- **Login spacing:** at most one login attempt per 600 s across all workers (stored `last_login_attempt_at`). Otherwise `ProcoreUnavailable(locked_until)`.
- **Rate limits:** 429 or 5xx raise `ProcoreTransient`, retried by the run queue: `pm_rfi_sync_max_attempts=3`, `retry_seconds=600`.
- **Session dropped after a fresh login:** `ProcoreSessionRejected`, not retried.
- **No access:** 403/404 raise `ProcoreForbidden`, permanent.
- **us02:** untested live.
- **Shared account:** every consumer shares the lockout (`procore` row). An agent that logs in from a real browser on the Windows PC is the same account, so its failures should feed the same lock or it may trigger Procore's own lockout.
- **Pace limits are per process.** A second machine (the worker) does not see the BE's `_pace`.

## 5. Reuse versus real browser

**Reuse as-is (backend side):**
- `extract_rfi_links`, `extract_company_id`, `parse_project_url` and `unwrap_link` to seed jobs from the classified email.
- `_trusted_for_autolink` and `auto_link_from_email` for project linking.
- `ProcoreSession.download` for signed storage links (no cookies).
- The import code in `pm_rfi_sync`: `map_detail` (line 891), `detail_attachments` (line 942), `apply_detail` (line 1277), `sync_attachments` (line 1031), `match_internal` (line 978). An agent result shaped like the `/rest/v1.0/projects/{p}/rfis/{r}` detail JSON can go straight into the existing RFI log writer. The lowest-risk shape: the agent fetches that JSON inside its logged-in browser and posts it back.
- The locking and notification patterns: `rfp_harvest_sessions` columns, `_notify_lock`, `availability()`.

**Reuse with change:** the cookie jar format matches Playwright's cookie format (`name`, `value`, `domain`, `path`, `expires`, `secure`). That allows two-way handoff: the agent can load cookies the BE saved, or save its cookies after a browser login so the 6:30 AM httpx sweep reuses them. The cookies are a bearer credential, so sending them to the worker needs a decision.

**Must happen in a real browser on the worker:**
- Solving a CAPTCHA, Cloudflare challenge or MFA prompt, with an Estimating Admin taking over. Today these are only detected.
- Any page outside the allowlists, e.g. us02 bid-phase correspondence with no RFI URL, or navigating from the project to the RFI.
- Rendering the RFI page as a fallback if the REST detail call is refused.

The httpx login could serve as the worker's fast path, with the browser reached only on a `challenge`, `login_page` or `redirect` failure.

**Note:** `procore_client` has no Supabase import (the store is injected), so it is portable to the Windows worker. Credentials come in through `ProcoreConfig`, so they could be fetched from BDR instead of the env.

## providers

# Provider APIs, agent design, libraries and Windows hosting for the BDR browser agent (as of 2026-10-19)

This was a read-only task: I changed no files and wrote none.

Bottom line: every one of the five providers supports function calling with screenshot input, but only OpenAI, Anthropic and Google have native computer use, and those three are not compatible with each other. Build our own browser tools on Playwright, call them through function calling, and keep native computer use as an optional extra.

## Part 1: The five providers

| | OpenAI | Anthropic | Meta | Google | xAI |
|---|---|---|---|---|---|
| Base URL | `https://api.openai.com/v1` | `https://api.anthropic.com/v1/messages` | `https://api.meta.ai/v1` | Native: `https://generativelanguage.googleapis.com/v1beta`. OpenAI-compatible: `.../v1beta/openai/` | `https://api.x.ai/v1` |
| Auth | `Authorization: Bearer` | Native: `x-api-key` plus `anthropic-version: 2023-06-01`. OpenAI layer: Bearer | `Authorization: Bearer $MODEL_API_KEY` | `?key=` or `x-goog-api-key`. OpenAI layer: Bearer | `Authorization: Bearer $XAI_API_KEY` |
| OpenAI-compatible Chat Completions | Native | Yes, at `https://api.anthropic.com/v1/`, but Anthropic says it is for testing and not production-ready | Yes. Its docs lead with Responses, and it also has an Anthropic-style Messages endpoint | Yes, in beta | Yes. Its docs lead with Responses (`/v1/responses`) |
| Tool calling | Responses: `{"type":"function"}`. Chat: `tools` / `tool_calls` | `tools` with `input_schema`, then `tool_use` / `tool_result` blocks | Yes, all Muse Spark models | `functionDeclarations`, or OpenAI-style through the compat layer | Responses `function_call` / `function_call_output` by `call_id`. Parallel calls are on by default. Up to 350 tools per request |
| Image input | All current models | All current models | Muse Spark takes text, image, video, audio and PDF | Yes, including the compat layer | `input_image` (base64 data URL). JPG or PNG, 20 MiB maximum. Docs only say "some models" accept images |
| Native computer use | Yes: Responses `tools:[{"type":"computer"}]` | Yes: `computer_toolset_20260801` and `browser_toolset_20260801` (both GA, no beta header) | No | Yes: `computer_use` tool, Interactions API only | No |
| Model list endpoint | `GET /v1/models` | `GET /v1/models`. Returns `max_input_tokens`, `max_tokens`, `capabilities` and `line` | "List models" in its API reference (exact path not shown on the quickstart page) | `GET /v1beta/models`. Paginated, with `inputTokenLimit` and `supportedGenerationMethods` | `GET /v1/models` (id only) and `GET /v1/language-models` (modalities, aliases, pricing). Both confirmed only in third-party mirrors |
| Current model ids | `gpt-6-astra` (frontier), `gpt-6.1-sol` (balanced), `gpt-6-luna` (efficient) | `claude-opus-5-5` (recommended default), `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-1` | `muse-spark-1.3` (recommended), `muse-spark-1.2`, `muse-spark-1.1`, `muse-wonder-1.0` (preview). All 1M context | `gemini-3.8-flash` (recommended for computer use), `gemini-4-argon`, `gemini-3.5-flash-lite`, `gemini-3-flash-preview` | `grok-4.7`, `grok-4.6`, `grok-4.5`, `grok-4.3`, `grok-4.20-0309-reasoning` / `-non-reasoning`. Aliases: `<model>`, `<model>-latest`, `<model>-<date>` |

### Provider notes

**OpenAI**
- The computer tool returns `computer_call` items. Each has a `call_id`, an ordered `actions[]` and a `status`. Actions are click, double_click, drag, move, scroll, keypress, type, wait and screenshot.
- You reply with `computer_call_output` containing `{type:"computer_screenshot", image_url:"data:image/png;base64,..."}` and continue with `previous_response_id`.
- When API safety checks are pending, the sample loop stops. The guide says to add a human approval callback only if your app can handle them.
- The SDK ships `openai.lib.browser` (`BrowserToolset`, `AsyncBrowserToolset`): you write the driver and it exposes members such as `read_page`, `click` and `type` as function tools, with a `confirm` callback. For `gpt-6-astra`, OpenAI recommends a code-execution loop instead of the computer tool.
- `computer-use-preview` was shut down on 2026-07-23. The Assistants API was removed on 2026-08-26. Chat Completions has no deprecation notice.

**Anthropic**
- Claude 5.5 models on the Claude API accept only `computer_toolset_20260801`. Older models use `computer_20251124` with the `computer-use-2025-11-24` beta header.
- Calls arrive as `tool_use` blocks with `toolset_name:"computer"` (or `"browser"`), and every result must echo the same `toolset_name`.
- `browser_toolset_20260801` is the closest official match to the design recommended below:
  - 31 members: navigate, screenshot, zoom, clicks, hover, scroll, type, key, wait, `read_page` (accessibility tree with `[ref_N]` refs), `find`, `get_page_text`, `form_input`, and tab tools.
  - Off by default: `javascript_exec`, `file_upload`, `read_console`, `read_network`.
  - The client runs the browser; nothing runs on Anthropic's side. Refs are per tab and our executor must report stale refs.
- Screenshot limits: up to 2576 px on the long edge for current models. Oversized images are rejected, not downscaled. Recommended sizes for web apps are 1280x800 or 1366x768.
- Anthropic runs prompt-injection classifiers on returned screenshots and content.
- The OpenAI compatibility layer ignores `strict`, `response_format` and prompt caching. Forced `tool_choice` returns an error on Opus 5.5 and Sonnet 5.5.

**Meta**
- `llama.developer.meta.com/docs/...` now redirects (302) to `dev.meta.ai`, the "Meta Model API". The current docs list only Muse models; Llama 4 is not mentioned.
- The older Llama API endpoint `https://api.llama.com/compat/v1/` (models list, chat completions) appears only in older and third-party docs. I could not confirm it still works. Treat Meta as OpenAI-compatible at `https://api.meta.ai/v1` and fill the dropdown from its list endpoint rather than a hard-coded list.

**Google**
- The computer use tool is `{"type":"computer_use","environment":"browser"}` with optional `excluded_predefined_functions` and `enable_prompt_injection_detection`.
- Actions: click, double/triple/middle/right click, move, type, drag_and_drop, wait, press_key, hotkey, take_screenshot, scroll, navigate, go_back, go_forward.
- Coordinates are normalized 0-999 and must be scaled to the viewport.
- A `safety_decision` with `require_confirmation` must get a human `safety_acknowledgement` before the action runs.
- I found no evidence that computer use works through the OpenAI-compatible endpoint.

**xAI**
- The docs say the OpenAI SDK, and the Anthropic SDK, work by changing the base URL.
- The docs advise not storing request and response history on the server.

## Part 2: Recommended design

**1. Use our own browser tools through plain function calling, not native computer use.**
- Meta and xAI have no native computer use.
- The three native versions are incompatible: OpenAI has its own action schema, Anthropic uses pixel coordinates and member tools, and Google uses 0-999 coordinates through the Interactions API only. Supporting all five natively means five loops and five safety flows.
- A shared tool set of about 10-15 tools mirroring Anthropic's browser toolset works on all five through ordinary tool calling: `navigate`, `read_page` (accessibility tree with stable `ref_N`), `find_text`, `click(ref|x,y)`, `fill(ref, value)`, `select`, `press_key`, `scroll`, `screenshot`, `download(ref)`, `wait_for`, `ask_human(question, attach_screenshot)`, `report_result(json)` and `fail(reason)`.
- Attach a screenshot each turn as an image content part.
- Ref-based actions are more deterministic than pixel clicks and give a readable audit trail, for example `click ref_12 "Download All"`.
- An optional "native computer use mode" per provider can come later behind the same executor.

**2. Keep credentials and login out of the model.**
- Write the Procore login as a deterministic Playwright routine: email from `PROCORE_LOGIN_EMAIL`, password pulled from BDR settings at runtime, never put into a prompt and never logged.
- If the model needs to sign in elsewhere, give it a `fill_secret(ref, secret_name)` tool that the executor resolves locally, so it never sees the value.
- Detect logout or a captcha deterministically (URL, title or selector checks). On detection, pause and raise an Estimating Admin help request.
- Anthropic's own guidance is not to give the model login credentials.

**3. Restrict what the agent can reach and do.**
- Allow only Procore domains (and similar), checked again after redirects.
- Allow only http and https in `navigate`.
- Keep JavaScript execution and file upload off.
- Put downloads in a per-job directory.
- Pass page text to the model as untrusted data.

**4. Make every step auditable.**
- For each step, record: provider, model, request id, token usage, the tool call and its arguments, the executor result, the URL before and after, a screenshot hash plus the stored file, and timing.
- The executor is the single choke point, so the trail is complete whichever provider is used.
- Native computer-use loops, and libraries such as browser-use, hide part of this.

**5. Make jobs survive shutdowns.**
- Checkpoint after each tool step: the step number plus a compact summary of the conversation. Do not rely on a provider's server-side conversation state.
- On restart, replay from the last checkpoint in a fresh page, or simply re-run the job idempotently.
- RFI ingest should dedupe on RFI number plus a sha256 of each file.

**6. Fill the IT Admin model dropdown from the providers.**
- The BDR backend calls each provider's model list endpoint with the stored key and caches the result.
- Filter to models that accept images and tool calling, using Anthropic `capabilities`, xAI `/v1/language-models` `input_modalities`, and Google `supportedGenerationMethods`.
- The worker pulls the selected provider, model and key from BDR at the start of each job, so nobody has to touch the PC to change models.

## Part 3: Python libraries

Recommendation: official SDKs behind a thin adapter of our own.
- Use `openai` for OpenAI, xAI and Meta (the latter two via `base_url`), `anthropic` for Claude, and `google-genai` for Gemini (or the OpenAI SDK against its compat endpoint).
- Write one adapter interface, roughly `complete(messages, tools, images) -> {text, tool_calls, usage, request_id}`, with about 150 lines per provider.
- Use native Anthropic rather than its OpenAI layer, because Anthropic calls that layer not production-ready.

Pros and cons of the alternatives:

| Option | Pros | Cons |
|---|---|---|
| Raw httpx | Fewest dependencies | We would rebuild retries, streaming and types that the official SDKs already provide |
| LiteLLM (1.106.0, released 2026-10-18) | One OpenAI-format interface for 100+ providers, covering tools, vision and `/responses`; includes Meta Model API | Versions 1.82.7 and 1.82.8 were malicious (published 2026-03-24 through a hijacked maintainer account; stole env vars and API keys; removed from PyPI). If used: pin exact versions with hashes. Large dependency surface, and translation quirks hurt auditability |
| browser-use (0.13.11, 2026-10-07, MIT, Python 3.11+) | Fastest to prototype. Classes for OpenAI, Anthropic and Google, OpenAI-compatible `base_url`, and LiteLLM | No dedicated xAI or Meta class. Opaque internal prompts and loop. Default model `ChatBrowserUse` goes through a cloud gateway. Telemetry exists and I could not confirm its default. Harder to checkpoint and resume. Not recommended for the main loop |
| Playwright (Python) | Executor of choice: `launch_persistent_context`, accessibility snapshots, downloads, tracing | None noted. `context.tracing` produces a zip trace that is a good audit artifact |

## Part 4: Playwright on Windows

**Session 0.** Windows services run in Session 0, which has no interactive desktop. Interactive services and the UI0Detect viewer were removed in recent Windows 10, 11 and Server releases.
- A service (NSSM, WinSW or pywin32) can run headless Chromium fine, starts at boot without anyone logging in, and is restarted automatically.
- A headful browser started from a service is invisible.

**Task Scheduler with "Run only when user is logged on" (trigger: at logon).** Runs in the user's interactive session, so headful works.
- Needs automatic logon after a reboot: Microsoft recommends Sysinternals Autologon, which stores the password as an LSA secret.
- A Group Policy logon banner blocks auto logon.
- Set the task to restart on failure.
- "Run whether user is logged on or not" is non-interactive, which is effectively the same as a service.

**Recommendation.**
- Run the worker as a service with headless Chromium. This survives reboots and power loss best; also set the BIOS to restore power after an outage.
- The future "watch and steer live" use case can stream the page over CDP (`Page.startScreencast`) into BDR, so it does not need a visible window.
- If captcha or bot detection proves worse in headless, switch to the at-logon task plus Autologon in headful mode. Keep the switch as a config flag.

**Persistent profiles.**
- Use `chromium.launch_persistent_context(user_data_dir=...)`, which keeps cookies and storage so Procore logins persist.
- Only one browser process can use a given profile directory at a time, so give each agent slot its own directory, for example `profiles/<agent_type>/<slot>`. That also lets several agents run at once.
- After a hard crash, leftover `Singleton*` lock files can block launch. Delete them only after confirming no Chrome process still holds the profile.
- Always close the context in a `finally` block.
- Use Playwright's bundled Chromium, not the system Chrome profile: Chrome 136+ does not support automating its default user-data directory, and Chrome on Windows has conflicts with multiple user-data directories.
- I found no source for suppressing the "Restore pages" prompt after a crash. Disposable pages per job make it mostly irrelevant.

## Sources
- [OpenAI computer use](https://developers.openai.com/api/docs/guides/tools-computer-use)
- [OpenAI models](https://developers.openai.com/api/docs/models)
- [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations)
- [Anthropic computer use tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- [Anthropic browser use tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool)
- [Anthropic models overview](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Anthropic OpenAI SDK compatibility](https://platform.claude.com/docs/en/api/openai-sdk)
- [Meta Model API models](https://dev.meta.ai/docs/models)
- [Meta quickstart](https://dev.meta.ai/docs/quickstart)
- [Old Llama compatibility page (now redirects)](https://llama.developer.meta.com/docs/features/compatibility/)
- [LiteLLM Meta Llama provider](https://docs.litellm.ai/docs/providers/meta_llama)
- [Gemini computer use](https://ai.google.dev/gemini-api/docs/interactions/computer-use)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Gemini models API](https://ai.google.dev/api/models)
- [Gemini forum: computer use via OpenAI compat](https://discuss.ai.google.dev/t/does-gemini-2-5-pro-computer-use-model-support-openai-compatible-api-calls/107379)
- [xAI quickstart](https://docs.x.ai/developers/quickstart)
- [xAI models](https://docs.x.ai/docs/models)
- [xAI function calling](https://docs.x.ai/docs/guides/function-calling)
- [xAI image understanding](https://docs.x.ai/developers/model-capabilities/images/understanding)
- [xAI models list endpoints (third-party mirror)](https://www.withone.ai/knowledge/x-ai/conn_mod_def%3A%3AGJ8XVu7XnPI%3A%3A8IsJkWc_TYOFSFGiXHOeTg/md)
- [browser-use on PyPI](https://pypi.org/project/browser-use/)
- [browser-use supported models](https://docs.browser-use.com/supported-models)
- [LiteLLM on PyPI](https://pypi.org/project/litellm/)
- [NetSPI: LiteLLM supply chain compromise](https://www.netspi.com/blog/executive-blog/ai-ml-pentesting/litellm-supply-chain-compromise/)
- [Comet: LiteLLM supply chain attack](https://www.comet.com/site/blog/litellm-supply-chain-attack/)
- [FireDaemon: Session 0 isolation](https://www.firedaemon.com/post/microsoft-windows-interactive-services-and-session-0-isolation)
- [Alex Ionescu: Inside Session 0 isolation](https://www.alex-ionescu.com/inside-session-0-isolation-and-the-ui-detection-service-part-1/)
- [Microsoft: turn on automatic logon](https://learn.microsoft.com/en-us/troubleshoot/windows-server/user-profiles-and-logon/turn-on-automatic-logon)
- [BrowserStack: Playwright persistent context](https://www.browserstack.com/guide/playwright-persistent-context)
- [Claude Code issue on Chrome and multiple user-data dirs on Windows](https://github.com/anthropics/claude-code/issues/24144)