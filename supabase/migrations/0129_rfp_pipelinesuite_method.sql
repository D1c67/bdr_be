-- 0129 - RFP Ingestion: add pipelinesuite as an invitation method
-- (docs/RFP_PIPELINESUITE.md, sections 1 and 5; docs/RFP_EMAIL_INGESTION.md
-- sections 3.7 to 3.9; docs/RFP_HARVEST.md section 2.4). Apply after 0128.
--
-- PipelineSuite (PreconSuite, <gc>.pipelinesuite.com) is the bid-invitation
-- product a number of GCs run their own plan room on. The invitation email
-- comes from the GC's own domain and carries the portal host, a Project ID
-- and a Security Key, so one method-keyed harvester serves every
-- PipelineSuite GC. The method is granted by a LOCKED domain rule on the
-- GC's own domain (the 0127 mechanism: locked rules outrank the same GC's
-- organic match). This migration widens the two named check constraints
-- from 0128 by one value each and seeds the first two portals, CG&B
-- Enterprises (cgandbinc.com) and SHF International (shfcontracting.com),
-- as locked domain rules. The seed upserts on (kind, value) so a rule a
-- person already added by hand on either domain is upgraded in place to the
-- locked platform rule rather than duplicated.
--
-- Method vocabulary after this migration:
--   rfp_emails.invitation_method : organic, procore, pipelinesuite, gc_portal, general, nonorganic
--   rfp_authorized_senders.method: procore, pipelinesuite, gc_portal, general
--
-- rfp_harvests.method and rfp_harvest_sessions.provider have no check
-- constraints (0123), so they need no DDL: the harvester writes
-- method = 'pipelinesuite' and provider = 'pipelinesuite:<portal host>'
-- as plain text.
--
-- Release step after applying: rows from the two seeded domains already
-- parked at flagged_unauthorized are NOT touched here. Re-authorize them the
-- same way the POST /rfp-emails/authorized-senders route does after a rule
-- is added, by calling app.services.rfp_email_ingest.rescan_after_rule_added
-- once per seeded rule (it re-runs authorize + method over the last 14 days
-- of flagged_unauthorized rows the rule now covers).
--
-- Every statement here is idempotent. Both DO blocks look the constraint up
-- by column (the 0124 pattern), so this also works on a database that still
-- carries the named 0127 constraint, the named 0124 one, or the unnamed
-- inline 0120 check; on a re-run the named constraint below is what gets
-- dropped and re-added.

-- ── 1. rfp_emails.invitation_method check ───────────────────────────────

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
    check (invitation_method in ('organic', 'procore', 'pipelinesuite', 'gc_portal', 'general', 'nonorganic'));
end
$$;

-- ── 2. rfp_authorized_senders.method check ──────────────────────────────

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
    check (method in ('procore', 'pipelinesuite', 'gc_portal', 'general'));
end
$$;

-- ── 3. Seed: the first two PipelineSuite portals ────────────────────────
-- Locked domain rules on the GC's own sending domain (subdomains covered on
-- a label boundary, like every domain rule). created_by stays null (the
-- seed). The unique index rfp_authorized_senders_uidx (kind, value) from
-- 0120 is what the on conflict clause resolves against.

insert into rfp_authorized_senders (kind, value, method, locked) values
  ('domain', 'cgandbinc.com',      'pipelinesuite', true),
  ('domain', 'shfcontracting.com', 'pipelinesuite', true)
on conflict (kind, value) do update set method = 'pipelinesuite', locked = true;

notify pgrst, 'reload schema';
