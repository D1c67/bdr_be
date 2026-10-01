-- 0124 - RFP Ingestion: drop buildingconnected and ngem as invitation methods
-- (docs/RFP_EMAIL_INGESTION.md, sections 1, 3.1 and 3.7). Apply after 0123.
--
-- BuildingConnected and NGEM (IonWave) are not invitation sources the intake
-- processes. From this migration on their mail is sanitized out at delta
-- listing time (RFP_EMAIL_INGESTION_BLOCKED_DOMAINS, default
-- buildingconnected.com,ionwave.net, subdomains covered), exactly like
-- internal and vendor senders: no row is written and no LLM call is spent.
-- The rows those senders already produced are noise and are deleted, the
-- two locked seed rules that named them go, and both check constraints are
-- narrowed to the five methods that remain.
--
-- Method vocabulary after this migration:
--   rfp_emails.invitation_method : organic, planhub, procore, general, nonorganic
--   rfp_authorized_senders.method: planhub, procore, general
--
-- Release order: apply this migration, then deploy. Until the deploy the old
-- poller can still insert a row from a blocked sender but never labels it
-- with a removed method, because the rules it would match are gone.
-- Every statement here is idempotent.

-- ── 1. Rows from the two platforms ──────────────────────────────────────
-- Match the From domain on a label boundary: buildingconnected.com and
-- ionwave.net themselves plus any subdomain (customer.ionwave.net), never a
-- lookalike such as fakebuildingconnected.com. The dependents cascade
-- (rfp_email_sightings, rfp_classify_training, rfp_harvests,
-- rfp_project_matches are on delete cascade) or clear
-- (rfp_ingest_runs.rfp_email_id and rfp_emails.sibling_of_email_id are on
-- delete set null), so nothing is left dangling.

delete from rfp_emails
 where lower(from_address) ~ '@([a-z0-9-]+\.)*(buildingconnected\.com|ionwave\.net)$';

-- ── 2. Seed rules for the two platforms ─────────────────────────────────

delete from rfp_authorized_senders where method in ('buildingconnected', 'ngem');

-- ── 3. rfp_emails.invitation_method check ───────────────────────────────
-- The 0120 constraint is inline and unnamed, so it is looked up from
-- pg_constraint on the column; on a re-run the named constraint below is
-- what gets dropped and re-added.

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
    check (invitation_method in ('organic', 'planhub', 'procore', 'general', 'nonorganic'));
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
    check (method in ('planhub', 'procore', 'general'));
end
$$;

notify pgrst, 'reload schema';
