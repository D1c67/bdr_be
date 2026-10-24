# PM email classification

Every email filed to a project in Project Management (`projects.pm_stage` is
not null) gets AI labels, a short summary of the message and an updated
running summary of its thread. Writers can correct the labels. Code:
`app/services/email_classification.py`; migration `0154_pm_email_classification.sql`.

## Taxonomy

Multi-label: an email can carry up to 5 labels. Each label is
`{category, subcategory, title, confidence}`. The MAIN category keys are fixed
(later features key off them, for example the RFI sync reads `rfis`):

| category | subcategories |
| --- | --- |
| `materials_submittals` | `product_data`, `shop_drawings`, `cut_sheets`, `submittal_approval`, `submittal_rejection`, `submittal_general` |
| `rfis` | `rfi_question`, `rfi_response`, `clarification` |
| `financials` | `pay_application`, `invoice`, `lien_waiver`, `payment_status`, `schedule_of_values` |
| `change_management` | `pco`, `cor`, `change_order`, `pricing_request`, `scope_change` |
| `other` | none; the model must give a 2 to 6 word `title` ("Schedule update"), which the UI shows |

Model output is validated server-side: unknown categories are dropped, an
unknown subcategory becomes null (or `submittal_general` for a materials
label), confidence is clamped to 0..1, an `other` without a title is dropped,
and an answer with no valid label is `LlmBadOutput` (the queue regenerates;
after the last attempt the row is `failed`).

## Where it runs

Self-hosted box ONLY: llm feature `email_classify` is `self_hosted_only`
(the mirror of the AI Estimating `openai_only` pin). It uses the box picked
in Settings > AI model server (the live target, else `SELF_HOSTED_LLM_TARGET`)
and that box's model, whatever `FULL_SELF_HOSTED_LLMS_ENABLED` says. It never
reaches a 3rd-party model. `SELF_HOSTED_EMAIL_CLASSIFY_MODEL` pins a model on
the env default box (empty = the box's model, `off` = disabled). A box outage
is a transient queue failure retried on `LLM_QUEUE_RETRY_DELAYS`, never a
fallback.

## Triggers

`enqueue_for_emails` (best-effort, never raises) runs from every path that
sets `ingested_emails.project_id`:

- the filer's automatic rounds (`email_ingest._assign`: R1 conversation map
  and submittal replies, R2 subject, R3 LLM);
- manual assign (`email_ingest.assign_manual`, `POST /emails/{id}/assign`)
  and the retro-assignment of the rest of the conversation;
- the new-project rescan of the Unknown pool (`_rescan_assign`);
- the body fetch (`_step_received`) of an email that was filed before its
  content arrived (such emails are skipped until then).

`pm.activate_pm_for_win` calls `enqueue_for_project` so a won bid's existing
mail is classified. Direct-created PM projects start with no mail (the
rescan covers what lands on them). A project merge never targets a PM
project, so it needs no hook.

Skipped: emails on bidding-only projects; emails already classified (a move
to another PM project does not re-run; labels describe the email);
unassigning leaves the row in place. Writers can force a fresh run.

Queue: job type `email_classify` (one per email, target = email id), its own
claim pass with `EMAIL_CLASSIFY_CONCURRENCY` (default 1) per worker and
priority `EMAIL_CLASSIFY_QUEUE_PRIORITY` (180, behind the bidding jobs).
Emails are queued oldest first so each thread chains its summaries.
`EMAIL_CLASSIFY_ENABLED=false` stops new jobs; stored labels stay readable.

## Prompt

One user turn, context first, the current message last:

1. `THREAD SUMMARY SO FAR (context only)`: the thread summary of the latest
   earlier message in the thread that has one.
2. `PREVIOUS MESSAGE in this thread (context only, may not apply to the
   current message)`: earlier messages of the same mailbox, conversation and
   project, oldest first; only their new content; kept newest first inside
   `EMAIL_CLASSIFY_CONTEXT_MAX_CHARS` (12k), each capped at
   `EMAIL_CLASSIFY_PREVIOUS_MAX_CHARS` (2.5k).
3. `CURRENT MESSAGE`: from, to, cc, Pacific date, subject, attachment file
   NAMES only, the new part of the body (cap `EMAIL_CLASSIFY_BODY_MAX_CHARS`, 8k).
4. `QUOTED EARLIER THREAD CONTENT (context only)`: the current body after
   the first reply or forward marker ("On ... wrote:", "-----Original
   Message-----", an Outlook From/Sent header block, a forward banner) plus
   `>` quoted lines (cap 3k).

Long tracking links become `[link]` and inline image placeholders are
dropped. The system prompt says to label ONLY the current message. Output:
`labels[]`, `email_summary` (1 to 2 sentences), `thread_summary` (at most
about 120 words), requested with `response_format` json_schema.

## Data

- `email_classifications` (1:1 with `ingested_emails`): `status`
  (pending | running | done | failed), `ai_labels`, `user_labels`,
  `overridden_by` / `overridden_at`, `email_summary`, `thread_summary`,
  `model` (`self-hosted:<model>`), `error`, `classified_at`, plus copies of
  `project_id` and `conversation_id`. Effective labels = `user_labels` when
  set, else `ai_labels`.
- `email_label_corrections`: every override or reset with who, when, before
  and after (material for prompt work).

## API

- `GET /emails?project_id=` and `GET /emails/{id}` add `classification`
  (`status`, `labels` (effective), `ai_labels`, `user_labels`, `overridden`,
  `overridden_by_name`, `overridden_at`, `email_summary`, `thread_summary`,
  `model`, `classified_at`, `error`), one extra query per page.
- `PUT /emails/{id}/classification` `{labels: [{category, subcategory, title}]}`
  replaces the labels (422 on an invalid set).
- `DELETE /emails/{id}/classification/override` resets to the AI labels.
- `POST /emails/{id}/classification/rerun` queues a fresh run.

Writes need `require_pm_write` and a PM project (409 otherwise); read-only
roles see the labels.

## UI

PM project Emails tab (`components/pm/PmEmailsPanel.tsx`,
`components/emails/EmailClassification.tsx`): color-coded chips per row
(hover for subcategory, title, confidence and the summary), Classifying and
Failed states (writers get Retry), and a Classification section in the
opened message with Edit, Reset to AI and Re-run.

## Backfill

```
cd bdr_be
uv run python scripts/classify_pm_emails.py --dry-run        # print one prompt
uv run python scripts/classify_pm_emails.py --limit 2        # smoke test
uv run python scripts/classify_pm_emails.py --out results.json [--project-id <uuid>] [--force]
```

It calls `email_classification.execute` synchronously in message order (no
queue) and prints project, time, sender, subject, labels and summary.

## Release

1. Apply `0154_pm_email_classification.sql` (with approval for prod).
2. Railway: `EMAIL_CLASSIFY_ENABLED=true` (the default) and an empty
   `SELF_HOSTED_EMAIL_CLASSIFY_MODEL`; the API host must reach the box's
   `SELF_HOSTED_LLM_<TARGET>_BASE_URL` with its key and model set, even
   while `FULL_SELF_HOSTED_LLMS_ENABLED=false`.
3. Run the backfill once for existing PM mail.
