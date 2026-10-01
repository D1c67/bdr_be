-- 0133 - RFP Ingestion: managed blocked senders
-- (docs/RFP_EMAIL_INGESTION.md, sections 3.1, 4, 6, 7 and 9). Apply after 0132.
--
-- Until now the only sender block was the environment list
-- RFP_EMAIL_INGESTION_BLOCKED_DOMAINS (BuildingConnected, NGEM/IonWave and
-- PlanHub), which nobody could change without a deploy. This migration moves
-- that list into the database as `rfp_blocked_senders` so the Executive, the
-- Estimating Admin, the IT Admin and dev accounts can block and unblock
-- senders from Settings -> RFP Ingestion and from the review queue itself.
--
-- The four platform domains are seeded as LOCKED rows: they behave exactly as
-- they did before, they are visible in the list, and only an IT Admin (or a
-- dev account) can remove one. A row added by a person is unlocked and any of
-- the four roles can remove it.
--
-- The environment variable stays, with an EMPTY default: whatever a
-- deployment still lists there is unioned with these rows and cannot be
-- removed from the page (it is pinned by the server environment). Shipping an
-- empty default is what makes an unblock here actually take effect instead of
-- being silently overridden by the old default.
--
-- Blocking a sender also parks everything from them already in flight, which
-- needs one new terminal status, `blocked_sender`.
--
-- Release order: apply this migration, then deploy. A deploy that lands first
-- would find no table and every mailbox sync would fail closed (no rows
-- inserted) until the migration runs, which is the safe direction.
-- Every statement here is idempotent.

-- ── 1. The table ─────────────────────────────────────────────────────────
-- Same shape and vocabulary as rfp_authorized_senders (0120): `kind` is the
-- address/domain pair, `value` is lowercased and normalized by the service
-- before it is written, `locked` marks a platform row. `reason` is the
-- optional note the blocker typed, shown in the list so a year from now
-- somebody can tell why a company stopped arriving.

create table if not exists rfp_blocked_senders (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null check (kind in ('address', 'domain')),
  value       text not null,                       -- lowercased address or bare domain
  reason      text,                                -- optional, capped by the API
  locked      boolean not null default false,      -- platform row: IT Admin only
  created_by  uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now()
);

create unique index if not exists rfp_blocked_senders_uidx
  on rfp_blocked_senders(kind, value);

-- ── 2. The platform seed ─────────────────────────────────────────────────
-- The four domains that were RFP_EMAIL_INGESTION_BLOCKED_DOMAINS' default
-- through 0128. Subdomain coverage is decided by the poller on a label
-- boundary, so `customer.ionwave.net` and `message.planhub.com` are covered
-- by the bare domains here.

insert into rfp_blocked_senders (kind, value, reason, locked) values
  ('domain', 'buildingconnected.com',  'BuildingConnected is not an invitation source the intake processes.', true),
  ('domain', 'ionwave.net',            'NGEM / IonWave invitations are handled by the portal scan, not the email intake.', true),
  ('domain', 'planhub.com',            'PlanHub is not an invitation source the intake processes.', true),
  ('domain', 'planhubprojects.com',    'PlanHub is not an invitation source the intake processes.', true)
on conflict (kind, value) do nothing;

-- ── 3. rfp_emails.status check: + blocked_sender ─────────────────────────
-- Terminal, like the other flagged_* exits: a row parked because its sender
-- was blocked is never swept again. Looked up by column (the 0124 pattern) so
-- this lands on a database carrying the named 0132 constraint, an earlier
-- named one, or still the unnamed inline 0120 check.

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_emails'::regclass
      and c.contype = 'c'
      and a.attname = 'status'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_emails drop constraint %I', con.conname);
  end loop;

  alter table rfp_emails add constraint rfp_emails_status_check check (status in (
    'received', 'auth', 'keywords', 'classify', 'authorize', 'method', 'extract', 'match',
    'harvest', 'split', 'create',
    'review_llm', 'flagged_unauthorized', 'review_match',
    'done', 'created', 'merged', 'duplicate', 'flagged_auth', 'flagged_no_keywords', 'flagged_llm_no',
    'rejected_by_review', 'blocked_sender', 'failed'));
end
$$;

-- ── 4. RLS ───────────────────────────────────────────────────────────────
-- Deny-by-default, like every other rfp_* table: the service-role backend is
-- the only reader and writer.

alter table rfp_blocked_senders enable row level security;
alter table rfp_blocked_senders force row level security;

notify pgrst, 'reload schema';
