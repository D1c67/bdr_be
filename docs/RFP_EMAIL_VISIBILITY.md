# RFP email visibility: per-mailbox ownership

Status: spec agreed with the owner 2026-09-22. Migration 0134, dev only.
Companion to `RFP_EMAIL_INGESTION.md` (pipeline, tables, API), which this
document narrows: every read and every action on an `rfp_emails` row is now
scoped to the people whose mailbox received it.

## 1. Decisions locked in (2026-09-22)

| Topic | Decision |
|---|---|
| Ownership | A row belongs to the mailboxes that received it (`rfp_email_sightings`), not to the To/CC headers: about a fifth of rows reach a mailbox by BCC or a distribution list. |
| Mapping | `profiles.rfp_mailboxes text[]`: the mailboxes a person owns. Edited by the user-management roles on Settings > Users. A mailbox may sit on more than one profile. Nothing is seeded; dev has no profiles for the real mailboxes. |
| Shared mailboxes | `RFP_EMAIL_INGESTION_SHARED_MAILBOXES` (default `bids@g3electrical.com`). Every internal role sees a row sighted in a shared mailbox. The external estimator never does. |
| Who sees a row | The row's `mailboxes` overlap the viewer's `rfp_mailboxes`, OR overlap the shared list. Nothing else. |
| Who sees everything | A dev account (`profiles.is_dev`) while wearing the IT Admin role. A dev wearing any other role is scoped like anyone else. Executives are scoped to their own mailbox (owner: "they are executives not micromanagers"). The Estimating Admin is scoped too: they must not see into the executives' mailboxes. |
| Who acts | Unchanged: `RFP_REVIEW_ROLES`. The Accountant may VIEW rows it can see (shared-mailbox rows, plus any mailbox mapped onto it) but never act. |
| Not visible | 404 with the existing `rfp_email_not_found` code on every `/{email_id}` route, the same body a bad id gets, so ids never leak. |
| Multi-mailbox rows | One message sighted in several mailboxes is one row; every owner sees and may act on it. |
| Per-recipient siblings | Unchanged (`RFP_MATCHING.md` 3.1): the follower copy takes the leader's decision even when the copies sit in different people's mailboxes. |
| After a project exists | Created, merged and duplicate rows are visible to non-owners ONLY through the project, and only as metadata: sender name and address, subject, received time, method, GC, extracted name and due date, attachment names. Never `body_text`, `body_preview`, links, or the raw auth header. The projects router's `_RFP_MATCH_EMAIL_SELECT` and `/rfp-created`'s `_EMAIL_SELECT` already select exactly that; they must stay that way. The RFP emails page itself never shows a non-owner anything. |
| Counts and badges | `GET /rfp-emails/counts`, `/match-stats`, the sidebar badge, the dashboard card and the 30 s activity poll all count only what the viewer can see. |
| Notifications | The per-tick "New RFP emails need a decision" bell goes to each OWNER of the new rows' mailboxes (`notify_user`, one row per person, deduped while an unread one exists for that person). New rows in a shared mailbox notify the Estimating Admin role as today. The "matcher added a GC" notice stays with the Estimating Admin role (it is about projects, not mail). |
| Cross-user effects that stay global | Block sender (parks everyone's mail from that sender), authorized-sender rules, auto-merge, the learn-back rescan. |
| NGEM portal invitations | Not mail, no mailbox: visible to every internal role, as today. |
| Recipient column | The RFP emails page shows a "Recipient" column listing the row's `mailboxes` (owner names when mapped, else the address) for the dev IT Admin viewer only. |
| Dev account | `t.moorejr@g3electrical.com` becomes `is_dev = true` on the dev database now (plain SQL, not the migration). Prod at release, with approval. |

## 2. Schema (0134_rfp_email_visibility.sql)

```
alter table profiles add column if not exists rfp_mailboxes text[] not null default '{}';
alter table rfp_emails add column if not exists mailboxes text[] not null default '{}';
create index if not exists rfp_emails_mailboxes_gin on rfp_emails using gin (mailboxes);
```

`rfp_emails.mailboxes` is a denormalized copy of the sightings, maintained by
an AFTER INSERT trigger on `rfp_email_sightings` (`array_append` when the
lowercased mailbox is not already present) and backfilled once from the
existing sightings. The trigger means no ingest path can forget it: the
poller, the harvest sighting writers and the testing bench all insert
sightings and nothing else changes. Deletes never happen on sightings.

Every value is lowercased and trimmed on the way in (trigger and API).

## 3. Backend

### 3.1 CurrentUser

`get_current_user` selects `rfp_mailboxes` with the profile and carries it on
`CurrentUser.rfp_mailboxes: tuple[str, ...]` (empty for an impersonated
estimator and for the test-built users).

### 3.2 Visibility helper (`app/services/rfp_email_visibility.py`)

```
def sees_everything(user) -> bool: user.is_dev and user.role == Role.IT_ADMIN
def visible_mailboxes(user) -> set[str] | None:
    None when sees_everything; else lower(rfp_mailboxes) | shared_mailboxes()
def apply_scope(query, user): no-op for None; `.overlaps("mailboxes", sorted(set))`
    otherwise; an EMPTY set short-circuits the caller to zero rows (no query)
def assert_visible(row, user) -> None: raises the 404 used for unknown ids
```

`shared_mailboxes()` reads `Settings.rfp_email_ingestion_shared_mailboxes`
(comma-separated, lowercased, default `bids@g3electrical.com`).

### 3.3 Routes (`app/routers/rfp_emails.py`)

- New role set `RFP_VIEW_ROLES = RFP_REVIEW_ROLES + (Role.ACCOUNTANT,)` in
  `app/core/roles.py`. `GET ""`, `GET /counts`, `GET /match-stats`,
  `GET /{email_id}` take `require_role(*RFP_VIEW_ROLES)`. Every action keeps
  its current gate.
- `GET ""` and `GET /counts`: scope applied to the query; empty scope returns
  `{items: [], total: 0}` / all zeros without a query.
- `GET /match-stats`: `match_stats(sb, mailboxes)` in the service applies the
  same overlap to its three counts.
- `GET /{email_id}` and every `POST /{email_id}/...` and `PATCH /{email_id}`:
  load the row's `mailboxes` and `assert_visible` BEFORE anything else,
  including before the `_uuid_or_404` style refusals differ in any way.
  The block-sender, harvest, create, set-name, review, continue, dismiss,
  method patch and all six match routes are covered.
- List rows already carry `mailboxes` from the sightings; keep that. Add
  `owners: [{mailbox, name}]` resolved from profiles (name null when
  unmapped) so the FE can label the Recipient column. Only populate it when
  `sees_everything(user)`; otherwise omit the key.

### 3.4 Notifications (`rfp_email_ingest._notify_review_queue`)

`_TickStats` gains per-mailbox counters (`review_new_by_mailbox`,
`unauthorized_new_by_mailbox`, `match_review_new_by_mailbox`, keyed by the
lowercased mailbox, incremented wherever the totals are today, once per
mailbox on the row). At the end of the tick:

- For every mailbox in the shared list with any count: one
  `notify_role(Role.ESTIMATING_ADMIN, ...)` exactly as today (summed over the
  shared mailboxes, deduped by the existing unread check).
- For every other mailbox: load the profiles whose `rfp_mailboxes` contain
  it (one query for the tick) and `notify_user` each, message and metadata
  as today but summed over that person's mailboxes; deduped while that user
  has an unread `NOTIFY_TYPE_REVIEW` row. A mailbox nobody owns notifies
  nobody (the dev IT Admin sees it in the queue).

Both rows keep the type `rfp_email.review`, so the frontend bell renders them
exactly as before. They are told apart by `metadata.audience`, `"shared"` or
`"owner"` (`rfp_email_ingest.AUDIENCE_SHARED` / `AUDIENCE_OWNER`), and EACH
DEDUPE CHECK IS NARROWED TO ITS OWN AUDIENCE. That tag is load-bearing, not
bookkeeping:

- Without it, one owner leaving a personal bell unread would match the
  shared check (which looks only at type and unread) and silence the
  Estimating Admin's bell on every later tick.
- Within one tick, the shared row is written first and goes to the whole
  Estimating Admin role; an Estimating Admin who also owns a mailbox would
  then have an unread row of that type and never receive their personal one.

`notify_role` for `rfp_match.merged` carries no audience and its dedupe is
unchanged.

### 3.5 Users (`app/routers/users.py`, `app/models/schemas.py`)

- `ProfileOut.rfp_mailboxes: list[str]` (default `[]`).
- `AdminUpdateUserIn.rfp_mailboxes: list[str] | None`. Validation: each a
  well-formed address, trimmed and lowercased, deduped, at most 10, and the
  target profile must hold an internal role (an external estimator is
  refused with 400 `rfp_mailboxes_internal_only`). `[]` clears. Audited in
  the existing `user.update` patch, plus a dedicated
  `user.rfp_mailboxes_changed` row (`from`, `to`) whenever the list actually
  moves, so a remap (including an admin mapping a mailbox onto their own
  profile) stands out in a review.
- `is_dev` (the "sees everything" half of section 1) cannot be changed on
  your own profile through `PATCH /users/{id}`: 403
  `self_privilege_change`, and the same for your own role. Another admin
  must do it. It is only granted on an internal role (400
  `is_dev_internal_only` on an estimator) and is cleared when a dev is
  demoted to estimator. Grants and revocations write `user.dev_granted` /
  `user.dev_revoked`.
- `GET /users` returns it; `GET /users/me` returns it.

### 3.6 Tests

- Visibility helper unit tests: dev+IT Admin sees all, dev+other role
  scoped, exec scoped, estimating admin scoped, accountant shared only,
  empty scope returns nothing.
- Router tests: list/counts/detail scoped; 404 on a row outside scope for
  detail and for one action of each family (review, match/merge, block);
  Accountant may list but gets 403 on review.
- Users route tests: set, clear, refuse for estimator, refuse 11 values,
  lowercase.
- Notification test: two mailboxes owned by two profiles get one bell row
  each; a shared mailbox row goes to the Estimating Admin role.
- Full suite stays green (baseline 4764).

## 4. Frontend

- `lib/rfpEmails.ts`: `RFP_EMAIL_VIEW_ROLES` (review roles + accountant),
  `canViewRfpEmails(role)`; `canReviewRfpEmails` unchanged. Sidebar entry,
  `RfpEmailsActivity` and the page gate use `canViewRfpEmails`; every action
  control uses `canReviewRfpEmails`.
- RFP emails page: "Recipient" column after the sender column, shown only
  when `me.is_dev && me.role === "it_admin"`, rendering `owners` (name, else
  address; several joined with ", ").
- Settings > Users: an "RFP mailboxes" field per user (chip/tag input of
  addresses, internal roles only), saved through the existing PATCH. Shown
  and editable for the same roles that already edit users there.
- Keys in all six catalogs. No em dashes.
- Types: `RfpEmailListRow.owners?`, `Profile.rfp_mailboxes`.
- Lint: `npm run lint` and `npx tsc --noEmit` (never `npm run build` beside
  a running `next dev`).

## 5. Release notes

- APPLY 0134 BEFORE THE BACKEND DEPLOY, not after. `get_current_user`
  selects `profiles.rfp_mailboxes` with every profile, so a backend running
  against a database without the column fails that select on EVERY request:
  no one can sign in, including the people who would roll it back. The
  migration is additive and idempotent and the old backend ignores the new
  columns, so applying it first is safe in both directions. The same order
  holds for staging.
- 0134 to staging and prod at release only, with approval.
- Set `t.moorejr@g3electrical.com` `is_dev = true` on prod with approval.
- Map the real mailboxes on Settings > Users on prod: tmoore@ to Thomas
  Moore (Executive), tiesha@ and vmadrid@ to the matching people.
- `RFP_EMAIL_INGESTION_SHARED_MAILBOXES` only if the shared list ever
  differs from the default.
