# Assistant: edits to shared files, for the integration stage

Every edit below touches a file another build is editing right now, so this
build did not apply it. Each entry gives the file, the anchor to find, and the
exact code. Read the file fresh immediately before each Edit. Design:
docs/ASSISTANT.md (Architecture > Principal, Permissions items 1 to 4, 13 and
15, Changes required).

Order: 1 (CurrentUser field), 2 (the hook), 3 (config), then run
`uv run pytest tests/test_assistant_principal.py tests/test_assistant_shim.py -q`.
`test_hook_hands_bdra_tokens_to_the_principal` skips until item 2 lands and
runs for real after it.

## 1. app/core/deps.py: CurrentUser.via_assistant_run_id

Anchor: the last field of `class CurrentUser`, today

```python
    rfp_mailboxes: tuple[str, ...] = ()
```

Append directly after it (same dataclass, keep the default so every
hand-built CurrentUser in the tests stays valid):

```python
    # Set only by the bdra_ assistant principal (app/assistant/principal.py):
    # the run (chat turn or approval execution) this request is acting in.
    # None for every human request. Read by core/ratelimit (own buckets for
    # the assistant) and by the writers that fill the provenance columns.
    via_assistant_run_id: str | None = None
```

Until this lands, `resolve_bdra_principal` sets the attribute on the instance
(dataclasses allow it) so callers that read it see the same value either way.

## 2. app/core/deps.py: the bdra_ branch in get_current_user

Anchor, inside `get_current_user`, today

```python
    token = authorization.split(" ", 1)[1].strip()
    # A browser agent worker token (core/agent_auth) is never a user session.
    if token.startswith("bdrw_"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
```

Insert between the `token = ...` line and the `bdrw_` comment:

```python
    # The assistant principal (app/assistant/principal.py): a bdra_ token is
    # accepted only inside the in-process shim, which marks the ASGI scope
    # bdr_internal. Over the network the marker is absent and the token is
    # refused exactly like a bdrw_ token (same 401, same message). Imported
    # here, not at module top: principal.py imports CurrentUser from this
    # module, and app.main must never import app.assistant.
    if token.startswith("bdra_"):
        from app.assistant.principal import resolve_bdra_principal

        return resolve_bdra_principal(request.scope, token, _load_profile)
```

And add the loader as a module-level function, right after
`get_current_user` (before `_impersonated_estimator`). It is the same
SELECT get_current_user runs, as a plain `def` (sync SDK, threadpool), using
`limit(1)` rather than `single()` so a missing row is None, not an APIError:

```python
PROFILE_COLUMNS = (
    "id, email, role, is_active, is_dev, mfa_enrolled, "
    "invite_accepted_at, rfp_mailboxes"
)


def _load_profile(user_id: str) -> dict | None:
    """The caller's profiles row with the columns get_current_user reads, or
    None. Plain def: the Supabase SDK is synchronous. Shared with the bdra_
    assistant principal so the live-profile rule (docs/ASSISTANT.md,
    Permissions item 1) re-reads exactly what a human session reads."""
    rows = (
        get_supabase()
        .table("profiles")
        .select(PROFILE_COLUMNS)
        .eq("id", user_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None
```

Optional, not required by the tests: replace the inline `.select("id, email,
role, ...")` string in get_current_user with `PROFILE_COLUMNS` so the two
cannot drift. Do not change the `.single()` call there.

Why the branch sits before `decode_token`: a bdra_ token is not a JWT, so
without the branch it already fails `decode_token` with the same 401. The
branch is what lets the shim's requests through; the network refusal is the
scope check inside `resolve_bdra_principal`, not this line.

## 3. app/core/config.py: settings and boot validators

### 3a. Fields

Anchor: the `# ── LLMs (routing lives in app/services/llm.py) ───` section
header (around line 151). Insert this block immediately BEFORE that header
(after the previous section's last field), inside `class Settings`:

```python
    # ── BDR Assistant (docs/ASSISTANT.md) ──────────────────────────────────
    # Master switch for the per-user assistant: routes, worker claim pass,
    # bdra_ principal. Off = the assistant does not exist (GET /features
    # reports assistant=false, every /assistant route 404s).
    assistant_enabled: bool = False
    # The schedule loop (phase 3). Needs ASSISTANT_ENABLED.
    assistant_scheduler_enabled: bool = False
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
```

### 3b. Boot validator

Anchor: `_validate_self_hosted_llms` (the `@model_validator(mode="after")`
block around line 1473). Insert this validator immediately AFTER that method
ends (after its `return self`), as the next method:

```python
    @model_validator(mode="after")
    def _validate_assistant(self) -> "Settings":
        """Refuse to boot on an assistant configuration that could only fail
        at run time (docs/ASSISTANT.md, Architecture): no token secret, an
        OpenAI-only feature under the self-hosted master switch without the
        owner's explicit allow, a scheduler without an assistant, or caps that
        cannot be enforced. Only enforced while ASSISTANT_ENABLED is on (the
        scheduler check runs always, since it is meaningless on its own)."""
        if self.assistant_scheduler_enabled and not self.assistant_enabled:
            raise ValueError(
                "ASSISTANT_SCHEDULER_ENABLED=true needs ASSISTANT_ENABLED=true."
            )
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
        return self
```

`app/assistant/principal.MIN_SECRET_CHARS` is 32 and must stay equal to the
validator's threshold: `_secret()` refuses to mint or verify under it, so a
deployment that boots can never produce an unverifiable token.

### 3c. .env.example

Append after the `MAILBOX_EXCLUSIONS` lines the PM scoping ticket added:

```
# BDR Assistant (docs/ASSISTANT.md). Off by default. The token secret is
# required when enabled (32+ chars, Railway env only).
# ASSISTANT_ENABLED=false
# ASSISTANT_SCHEDULER_ENABLED=false
# ASSISTANT_TOKEN_SECRET=
# ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED=false
# ASSISTANT_DAILY_CAP_USD=5
# ASSISTANT_GLOBAL_DAILY_CAP_USD=50
```

### 3d. tests/conftest.py

Pin the switch off for the suite (the pattern every other flag follows), in
the block of `os.environ[...]` pins:

```python
# The assistant defaults off; pin it so a dev .env that enables it can never
# start the claim pass or the scheduler under the suite. Tests that exercise
# the assistant patch get_settings themselves.
os.environ["ASSISTANT_ENABLED"] = "false"
os.environ["ASSISTANT_SCHEDULER_ENABLED"] = "false"
```

## 4. Not in this build, listed so nothing is lost

- core/ratelimit.py `_check` and `check_outbound_email`: when the principal
  carries `via_assistant_run_id`, key the bucket on `user_id + ":assistant"`
  with its own lower limits (Permissions item 12). The registry/executor
  build owns this edit.
- services/notifications.py `audit`, `notify_role`, `notify_roles`,
  `notify_user` and the writers under Actor provenance details: spread
  `app.core.provenance.fields()` into the row (safe before 0171 only if the
  columns exist; gate on the migration having been applied). The 0171
  migration build owns this.
- core/features.py / GET /features: the `assistant` key per user through
  `app.assistant.access.assistant_enabled_for(user, settings_row,
  profile_row)` (Permissions item 15). The router build owns this.

# Assistant core (registry, executor, loop, router): edits to shared files

Appended 2026-10-22 by the assistant-core build. Same rules as above: read
each file fresh immediately before the Edit, minimal diff, no em dashes.
Design: docs/ASSISTANT.md (Model and transport, Agent loop, Permissions
items 5, 7, 10, 12, 14 and 15, Changes required). After these land, run
`uv run pytest tests/test_assistant_*.py -q` and the integration checks in
section 16.

Order: 5 (config), 6 (llm.py features), 7 (llm_health label), 8
(llm_queue), 9 (llm_gate attribution), 10 (llm_pricing, usually nothing),
11 (main.py mount and GET /features), 12 (ratelimit), 13 (features.ts), 14
(.env.example), then 15 and 16.

## 5. app/core/config.py: model and effort fields

Anchor: the `# ── BDR Assistant (docs/ASSISTANT.md)` block from item 3a.
Append inside it, after `assistant_global_daily_cap_usd`:

```python
    # The planner model (OpenAI only; services/llm_pricing.PRICES must hold
    # a row for it, or every assistant call logs cost NULL) and its default
    # reasoning effort. assistant_settings.model overrides the model per
    # deployment from IT Admin Settings.
    assistant_model: str = "gpt-5.4"
    assistant_reasoning_effort: str = "low"
```

Add to `_validate_assistant` (item 3b), before `return self` at the end:

```python
        if self.assistant_reasoning_effort not in ("minimal", "low", "medium", "high"):
            raise ValueError("ASSISTANT_REASONING_EFFORT must be minimal, low, medium or high.")
        from app.services import llm_pricing  # local: config must stay import-light

        if llm_pricing.price_for(self.assistant_model) is None:
            raise ValueError(
                f"ASSISTANT_MODEL={self.assistant_model!r} has no row in services/llm_pricing.PRICES; "
                "add the price before enabling the assistant (spend caps need it)."
            )
```

The loop reads `assistant_model`, `assistant_daily_cap_usd` and
`assistant_global_daily_cap_usd` through getattr with the same defaults, so
it runs before this lands.

## 6. app/services/llm.py: the two feature entries

Anchor: the last entry of `_FEATURES` (today the plans_* entries built with
`_plans_feature(...)`), right before the dict's closing `}`. Append:

```python
    # BDR Assistant (docs/ASSISTANT.md, Model and transport). The planner is
    # pinned to OpenAI in both LLM modes and carries NO switch: ASSISTANT_ENABLED
    # gates the routes and the worker pass, not the route table, so the feature
    # stays on while PLANS_READER_ENABLED is off (a test asserts it). It is NOT
    # built from _plans_feature, which carries switch=plans_reader_enabled.
    "assistant": _Feature(
        "openai",
        lambda s: s.assistant_model,
        lambda s: SELF_HOSTED_MODEL_OFF,
        openai_only=True,
    ),
    # Tool-less summarizing and classifying of untrusted text (mail bodies,
    # phase 5); may run on the Qwen boxes, so it follows the master switch.
    "assistant_extract": _Feature(
        "openai",
        lambda s: s.assistant_model,
        lambda s: "",
    ),
```

`app/assistant/llm_messages.complete_messages` resolves the `assistant`
feature through `llm.resolve` and runs inside `llm._guarded`; until this
entry exists it raises LlmNotConfigured and GET /assistant/health answers
`model_unavailable`. Do not edit complete_parts.

## 7. app/services/llm_health.py: FEATURE_LABELS

Anchor: the last entry of `FEATURE_LABELS` (`"plans_chat": "AI Estimating: chat",`).
Append:

```python
    "assistant": "BDR Assistant",
    "assistant_extract": "BDR Assistant: text extraction",
```

## 8. app/services/llm_queue.py: job type, spec, claim pass, max_attempts_override

### 8a. Constants

Anchor: `JOB_PLANS_MODIFY = "plans_reader_modify"` and the lines after it
(`PLANS_BATCH_JOB_TYPES`, `PLANS_INTERACTIVE_JOB_TYPES`, `PLANS_JOB_TYPES`).
Insert after `PLANS_JOB_TYPES = ...`:

```python
# BDR Assistant (docs/ASSISTANT.md, Agent loop): one job per chat turn,
# target = conversation id, claimed in its own pass with its own capacity.
JOB_ASSISTANT_TURN = "assistant_turn"
ASSISTANT_JOB_TYPES = (JOB_ASSISTANT_TURN,)
ASSISTANT_DEFAULT_CONCURRENCY = 2
```

Anchor: `ALL_JOB_TYPES = LLM_JOB_TYPES + PLANS_JOB_TYPES + NON_LLM_JOB_TYPES`.
Replace with:

```python
ALL_JOB_TYPES = LLM_JOB_TYPES + PLANS_JOB_TYPES + NON_LLM_JOB_TYPES + ASSISTANT_JOB_TYPES
```

(0170 section 9a widens `llm_jobs_job_type_check` with assistant_turn,
assistant_continue and assistant_approval_execute; only assistant_turn is
claimed in phase 0.)

### 8b. _FEATURE_BY_TYPE

Anchor: `    JOB_PLANS_MODIFY: "plans_modify",` inside `_FEATURE_BY_TYPE`.
Append after it:

```python
    JOB_ASSISTANT_TURN: "assistant",
```

### 8c. _spec

Anchor: inside `_spec`, the `if job_type == JOB_PLANS_MODIFY:` block.
Insert after that block, before `raise KeyError(...)`:

```python
    if job_type == JOB_ASSISTANT_TURN:
        # One job per pending assistant message (target = conversation id,
        # docs/ASSISTANT.md Agent loop). The body, marks and labels live in
        # app/assistant/jobs.py; this module never imports app.assistant at
        # load time.
        from app.assistant import jobs as assistant_jobs

        return _JobSpec(**assistant_jobs.spec_kwargs())
```

`assistant_jobs.spec_kwargs()` returns feature, run, mark, current_status,
model_label and error_message. The body raises `loop.PermanentTurnFailure`
(llm_error_kind bad_input, never retried) for a missing or inactive
profile, a switched-off assistant and a reached cap; every other failure
is written to the message as status failed and swallowed, so the job
succeeds and nothing is re-run.

### 8d. enqueue: max_attempts_override

Anchor: the `enqueue(` signature, parameter `next_attempt_at: datetime | str | None = None,`.
Add after it:

```python
    max_attempts_override: int | None = None,
```

Anchor: inside `enqueue`, the row line
`"max_attempts": 1 + len(s.llm_retry_delay_list),`. Replace with:

```python
        "max_attempts": (
            max(1, int(max_attempts_override))
            if max_attempts_override is not None
            else 1 + len(s.llm_retry_delay_list)
        ),
```

Add to the docstring: "`max_attempts_override` pins the attempt count (the
assistant's chat turns use 1: a turn is never re-run, the user asks again)."
`app/routers/assistant.py` passes it only when the parameter exists
(inspect.signature), so the router runs before and after this edit.

### 8e. The assistant claim pass

Anchor: `def _claim_plans(s: Settings, types: list[str]) -> list[dict]:`.
Insert BEFORE it:

```python
def _assistant_concurrency(s: Settings) -> int:
    """Assistant turns this process runs at once (ASSISTANT_CONCURRENCY when
    it exists; the design's capacity 2 otherwise)."""
    try:
        return max(1, int(getattr(s, "assistant_concurrency", ASSISTANT_DEFAULT_CONCURRENCY)))
    except (TypeError, ValueError):
        return ASSISTANT_DEFAULT_CONCURRENCY


def _claim_assistant(s: Settings, types: list[str]) -> list[dict]:
    """The assistant pass (docs/ASSISTANT.md, Agent loop), modeled on
    _claim_plans: skipped while ASSISTANT_ENABLED is off (queued turns wait
    for the flag or a cancel, never spending on a switched-off feature);
    capacity 2 per process, independent of the pipeline and AI Estimating
    slots. Every turn is interactive: someone is watching the panel."""
    if not getattr(s, "assistant_enabled", False):
        return []
    free = _assistant_concurrency(s) - sum(1 for jt in types if jt in ASSISTANT_JOB_TYPES)
    if free <= 0:
        return []
    return _claim_filtered(s, free, list(ASSISTANT_JOB_TYPES))
```

Anchor: inside `_claim_tick`, the line `claimed.extend(_claim_plans(s, types))`.
Add after it:

```python
    claimed.extend(_claim_assistant(s, types))
```

Before adding the pass, verify `os.cpu_count()` on the Railway box as the
design asks (main.py sets no executor; about 12 pollers plus every llm_jobs
run share the default thread pool). Two more threads per worker is the
assistant's whole footprint in phase 0.

## 9. app/services/llm_gate.py: user_id and assistant_run_id on log_call

The 0170 migration adds `llm_call_log.user_id` and `assistant_run_id`. The
loop sets `app.assistant.llm_messages.attributed(user_id, run_id)` around
the turn; log_call reads it.

Anchor: inside `log_call`, the line `sweep_id = _eval_sweep_id()`. Insert
BEFORE it:

```python
        attribution = _call_attribution()
        if attribution is not None:
            row["user_id"] = attribution.user_id
            row["assistant_run_id"] = attribution.run_id
```

Add as a module-level function next to `_eval_sweep_id`:

```python
_ATTRIBUTION_COLUMNS = ("user_id", "assistant_run_id")


def _call_attribution():
    """The (user_id, run_id) an assistant turn stamps on its calls (the
    per-user daily cap sums llm_call_log by user_id), or None outside one.
    Lazy import: the gate never depends on the assistant package at load."""
    try:
        from app.assistant.llm_messages import current_attribution

        return current_attribution()
    except Exception:  # noqa: BLE001 - accounting must never break the call
        return None
```

And in `_insert_with_fallback`, so a database without 0170 keeps logging:
add a third group to `groups`:

```python
        (list(_ATTRIBUTION_COLUMNS), _warn_attribution_columns_missing),
```

with the matching one-per-process warning function modeled on
`_warn_usage_columns_missing` ("llm_call_log has no user_id/assistant_run_id
columns (migration 0170 not applied); logging assistant calls without them").

## 10. app/services/llm_pricing.py: PRICES

`gpt-5.4` (the default ASSISTANT_MODEL) and `claude-opus-5` (the splitter)
already have rows as of 2026-10-21, so no edit is needed. If
ASSISTANT_MODEL is set to another id, add its row the same day (spec 0.4
rule 3); the config validator in item 5 refuses to boot without one.

## 11. app/main.py: mount the router, GET /features "assistant"

main.py must never import `app.assistant` (tests/test_assistant_shim and
tests/test_assistant_repo_isolation pin the string). It imports the ROUTER
module, which is under app/routers.

### 11a. Import

Anchor: the `from app.routers import (` block after GET /features. Add
`assistant,` in alphabetical position (after `analytics,`).

### 11b. Mount

Anchor: `app.include_router(todos.router)`. Add after it:

```python
# BDR Assistant (docs/ASSISTANT.md): the per-user assistant's API. On the
# spine: require_internal on the router, and every route 404s for a caller
# whose assistant is off (ASSISTANT_ENABLED, company and profile switches).
# The worker's tool calls never come through here: they go through the
# in-process shim (app/assistant/shim.py), which main.py never touches.
app.include_router(assistant.router)
```

### 11c. GET /features

Anchor: the body of `features(...)`, `return enabled_map(user.role)`.
Replace with:

```python
    flags = enabled_map(user.role)
    # Per user, not per role (docs/ASSISTANT.md, Permissions item 15):
    # ASSISTANT_ENABLED and assistant_settings.enabled and
    # assistant_profiles.enabled and an internal role.
    flags["assistant"] = assistant.features_flag(user)
    return flags
```

`assistant.features_flag` fails closed on any read error and returns False
while ASSISTANT_ENABLED is off without touching the database.

## 12. app/core/ratelimit.py: principal-aware keys

The human and the assistant never share a bucket (Permissions item 12). The
shim enters `app.core.provenance.acting_as_assistant` around every
in-process request, so the limiter can read the contextvar instead of
threading a new argument.

Anchor: the import block. Add:

```python
from app.core import provenance
```

Anchor: `def _check(scope: str, user_id: str, limit: int, window_seconds: int = 60) -> None:`.
Insert BEFORE it:

```python
# The assistant's own buckets: keyed on user_id + ":assistant" with a lower
# limit, so a chatty turn can never spend the human's quota (and the other
# way round).
ASSISTANT_BUCKET_SUFFIX = ":assistant"
ASSISTANT_LIMIT_DIVISOR = 2


def _bucket_id(user_id: str) -> str:
    return f"{user_id}{ASSISTANT_BUCKET_SUFFIX}" if provenance.is_assistant() else user_id


def _bucket_limit(limit: int) -> int:
    return max(1, limit // ASSISTANT_LIMIT_DIVISOR) if provenance.is_assistant() else limit
```

Inside `_check`, right after the `rate_limit_enabled` early return, add:

```python
    user_id = _bucket_id(user_id)
    limit = _bucket_limit(limit)
```

`check_outbound_email` needs no change: it calls `_check`. Test to add
(section 16): an assistant 429 leaves the human's quota unchanged.

## 13. bdr_fe/lib/features.ts

Already done by the frontend item (`assistant?: boolean`, false in
ALL_ENABLED). Nothing to do.

## 14. .env.example

Append to the assistant block from item 3c:

```
# ASSISTANT_MODEL=gpt-5.4
# ASSISTANT_REASONING_EFFORT=low
# ASSISTANT_CONCURRENCY=2
```

## 15. Notes the integrator should know

- The assistant router adds `GET /assistant/skills` and
  `GET /assistant/skills/{name}` beyond the phase 0 contract: `load_skill`
  is a tool, and the registry refuses a ToolSpec without a route, so the
  skill bodies are served by a route under require_internal. The frontend
  does not need them.
- `app/routers/projects.py` gained `GET /projects/search` and
  `GET /projects/due-checks` above `GET /projects/{project_id}`, and
  `app/services/due_digest.py` gained `day_bounds`, `due_today_projects`,
  `readiness_rows` and `due_rows_for_today` (poll_once now composes them).
  Both are already applied (the task assigned those files to this build).
- `tests/test_assistant_shim.py::test_every_it_admin_only_or_dev_only_route_is_deny_listed`
  walks the mounted routes; the assistant router carries no admin-only
  dependency, so mounting it adds nothing to the deny-list.
- Spend reservation rows (the ai_spend.check_cap shape with a reserve at
  call start) are NOT built in phase 0: the loop checks the summed
  llm_call_log cost plus the call's estimate before every call. Two
  workers can each pass the check at the same instant and overshoot by at
  most one call each. Phase 1 adds the reservation.

## 16. Tests to add with the integration (suggested file tests/test_assistant_integration.py)

- `llm.unavailable_reason("assistant", s)` is None with
  `plans_reader_enabled=False` (feature switch independence).
- `llm.resolve("assistant", s).provider == "openai"` with
  `full_self_hosted_llms_enabled=True` (pinned).
- GET /features through TestClient(app.main.app) with get_current_user
  overridden: "assistant" is False with ASSISTANT_ENABLED off, True for an
  internal role with the settings row enabled and no profile row, False for
  the estimator, False when assistant_profiles.enabled is false.
- `llm_queue.enqueue("assistant_turn", target_id=..., max_attempts_override=1)`
  writes max_attempts 1; a second enqueue for the same target raises
  JobAlreadyActive with raise_on_active.
- `_claim_tick` with two running assistant_turn jobs claims no third one;
  with ASSISTANT_ENABLED off it claims none.
- `_spec("assistant_turn").run` is `app.assistant.jobs.run_assistant_turn`.
- ratelimit: inside `provenance.acting_as_assistant(...)`, 429 after
  limit // 2 hits on the assistant bucket; the human's bucket for the same
  user_id is untouched (`_buckets[(scope, user_id)]` absent).
- llm_gate.log_call inside `llm_messages.attributed(USER, RUN)` writes
  user_id and assistant_run_id on the row; outside it, neither key.
- A three-round tool loop under store=False against a fake OpenAI client:
  every request carries `store: False` and
  `include: ["reasoning.encrypted_content"]`, never previous_response_id,
  and round n+1's input replays round n's reasoning and function_call items
  followed by the function_call_output (tests/test_assistant_loop.py
  already pins the loop side with complete_messages faked; this test pins
  the transport side with `llm._client_for` faked).

# Integration stage, 2026-10-22: status of every item above

Applied to the shared files (fresh read before each edit, smallest additive
diff, anchors as written): 1, 2 (plus PROFILE_COLUMNS and _load_profile),
3a, 3b, 3c, 3d, 5, 6, 7, 8a to 8e, 9, 11a to 11c, 12, 14. Item 10 needed
nothing (gpt-5.4 and claude-opus-5 both have PRICES rows). Item 13 was
already done. The section 16 tests live in tests/test_assistant_integration.py.

Deviations from the text above, all forced by tests the AI Estimating build
owns (tests/test_llm_routing.py, tests/test_llm_health.py) and could not be
edited:

- llm.py: the `assistant` feature is NOT `openai_only`. Two tests pin that
  every openai_only feature is an AI Estimating feature (PLANS_FEATURES is
  derived from the flag). A new `_Feature.openai_pinned` flag carries the
  pin instead; `resolve` honours it only when
  ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED is set, because a third test pins
  that every non-openai_only feature routes to the box under the master
  switch. The boot validator refuses the self-hosted switch without that
  allow flag while the assistant is on, so a booted deployment with the
  assistant on is always pinned; with the assistant off the feature follows
  the box, which only the health snapshot can observe.
- llm.py: the assistant's self-hosted model is "" (the box's model), not
  SELF_HOSTED_MODEL_OFF, so the health count test (every non-openai_only
  feature graded ok on the box) holds. Neither feature carries a switch.
- config.py: `assistant_concurrency: int = 2` is a real field (item 8e read
  it through getattr), validated >= 1 while the assistant is on.
- llm_queue.py: the `_FEATURE_BY_TYPE` entry sits after JOB_PLANS_FACTS
  (the real last entry), not after JOB_PLANS_MODIFY.
- conftest.py pins are placed after CERTIFIED_PAYROLL_ENABLED.

0165 decision: option 2. scripts/check_llm_jobs_constraint_relists.py now
accepts 0165 as baseline, and the release note must order 0165 before 0170
in every environment (0170 widens whatever list it finds). 0165 itself is
untouched (AI Estimating build).
- llm_queue.py (item 8a, second half): ALL_JOB_TYPES is NOT widened.
  tests/test_plans_reader_routes.py (AI Estimating) pins that tuple to the
  0165 CHECK list, and no app code reads it; the assistant types live in
  ASSISTANT_JOB_TYPES and the CHECK accepts them once 0170 is applied.
