-- 0128 - RFP Ingestion: drop planhub as an invitation method
-- (docs/RFP_EMAIL_INGESTION.md, sections 1, 3.1 and 3.7). Apply after 0127.
--
-- PlanHub is not an invitation source the intake processes. From this
-- migration on its mail is sanitized out at delta listing time
-- (RFP_EMAIL_INGESTION_BLOCKED_DOMAINS, default now
-- buildingconnected.com,ionwave.net,planhub.com,planhubprojects.com,
-- subdomains covered), exactly like internal, vendor, BuildingConnected and
-- NGEM senders: no row is written and no LLM call is spent. The rows PlanHub
-- already produced are noise and are deleted, the locked seed rule on
-- message.planhub.com goes, and both check constraints are narrowed to the
-- methods that remain. rfp_harvests.method has no check constraint (0123)
-- and no planhub rows, so it needs no DDL.
--
-- Method vocabulary after this migration:
--   rfp_emails.invitation_method : organic, procore, gc_portal, general, nonorganic
--   rfp_authorized_senders.method: procore, gc_portal, general
--
-- Release order: apply this migration, then deploy. Until the deploy the old
-- poller can still insert a row from a PlanHub sender but never labels it
-- planhub, because the rule it would match is gone.
-- Every statement here is idempotent.

-- ── 1. Rows from PlanHub ─────────────────────────────────────────────────
-- Match the From domain on a label boundary: planhub.com and
-- planhubprojects.com themselves plus any subdomain (message.planhub.com,
-- the seed rule's domain), never a lookalike such as fakeplanhub.com. A row
-- a person labelled planhub by hand from any other domain goes too, so the
-- narrowed check below can never fail on a leftover. The dependents cascade
-- (rfp_email_sightings, rfp_classify_training, rfp_harvests,
-- rfp_project_matches are on delete cascade) or clear
-- (rfp_ingest_runs.rfp_email_id and rfp_emails.sibling_of_email_id are on
-- delete set null), so nothing is left dangling.

delete from rfp_emails
 where lower(from_address) ~ '@([a-z0-9-]+\.)*(planhub\.com|planhubprojects\.com)$'
    or invitation_method = 'planhub';

-- ── 2. Seed rule for PlanHub ─────────────────────────────────────────────

delete from rfp_authorized_senders where method in ('planhub');

-- ── 3. rfp_emails.invitation_method check ───────────────────────────────
-- Looked up by column (the 0124 pattern), so this lands on a database
-- carrying the named 0127 constraint, the named 0124 one, or still the
-- unnamed inline 0120 check; on a re-run the named constraint below is what
-- gets dropped and re-added.

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
      and a.attname = 'invitation_method'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_emails drop constraint %I', con.conname);
  end loop;

  alter table rfp_emails add constraint rfp_emails_invitation_method_check
    check (invitation_method in ('organic', 'procore', 'gc_portal', 'general', 'nonorganic'));
end
$$;

-- ── 4. rfp_authorized_senders.method check ──────────────────────────────

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_authorized_senders'::regclass
      and c.contype = 'c'
      and a.attname = 'method'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_authorized_senders drop constraint %I', con.conname);
  end loop;

  alter table rfp_authorized_senders add constraint rfp_authorized_senders_method_check
    check (method in ('procore', 'gc_portal', 'general'));
end
$$;

notify pgrst, 'reload schema';
