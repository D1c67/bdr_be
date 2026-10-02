-- 0148 - RFP Ingestion: add smartbid as an invitation method
-- (docs/RFP_SMARTBID.md, sections 1 and 5; docs/RFP_EMAIL_INGESTION.md
-- sections 3.7 to 3.9; docs/RFP_HARVEST.md section 2.4). Apply after 0147.
--
-- SmartBid (ConstructConnect's SmartBid, sometimes "SmartInsight") is the
-- bid-invitation product a number of GCs send through. Every invitation
-- comes from the platform's own address (notifications@com2.smartbidnet.com),
-- not from the GC, so like Procore the method is granted by ONE locked domain
-- rule on the platform domain and one method-keyed harvester serves every GC
-- that uses it. The project link in the email body carries everything the
-- harvester needs (the bid project id and a per-recipient passport key), so
-- nothing is configured per GC. The locked rule is the 0127 mechanism: locked
-- rules outrank the same sender's organic match. This migration widens the
-- two named check constraints from 0129 by one value each and seeds
-- smartbidnet.com as a locked domain rule (it covers com2.smartbidnet.com on
-- a label boundary, like procoretech.com). The seed upserts on (kind, value)
-- so a rule a person already added by hand on that domain is upgraded in
-- place to the locked platform rule rather than duplicated.
--
-- Method vocabulary after this migration:
--   rfp_emails.invitation_method : organic, procore, pipelinesuite, smartbid, gc_portal, general, nonorganic
--   rfp_authorized_senders.method: procore, pipelinesuite, smartbid, gc_portal, general
--
-- rfp_harvests.method and rfp_harvest_sessions.provider have no check
-- constraints (0123), so they need no DDL: the harvester writes
-- method = 'smartbid' and provider = 'smartbid' as plain text.
--
-- Release step after applying: rows from smartbidnet.com already parked at
-- flagged_unauthorized are NOT touched here. Re-authorize them the same way
-- the POST /rfp-emails/authorized-senders route does after a rule is added,
-- by calling app.services.rfp_email_ingest.rescan_after_rule_added once for
-- the seeded rule (it re-runs authorize + method over the last 14 days of
-- flagged_unauthorized rows the rule now covers). Do NOT run that rescan
-- without the owner's go: on dev it touches about 55 rows across about 12
-- real projects, and every new project the match step does not find gets
-- logged into and downloaded (hundreds of MB each, and the GC sees the
-- email open and click pings for every one).
--
-- Every statement here is idempotent. Both DO blocks look the constraint up
-- by column (the 0124 pattern), so this also works on a database that still
-- carries the named 0129 constraint, an older named one, or the unnamed
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
    check (invitation_method in ('organic', 'procore', 'pipelinesuite', 'smartbid', 'gc_portal', 'general', 'nonorganic'));
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
    check (method in ('procore', 'pipelinesuite', 'smartbid', 'gc_portal', 'general'));
end
$$;

-- ── 3. Seed: the SmartBid platform domain ───────────────────────────────
-- One locked domain rule on the platform's own sending domain (subdomains
-- covered on a label boundary, like every domain rule). created_by stays
-- null (the seed). The unique index rfp_authorized_senders_uidx (kind, value)
-- from 0120 is what the on conflict clause resolves against.

insert into rfp_authorized_senders (kind, value, method, locked) values
  ('domain', 'smartbidnet.com', 'smartbid', true)
on conflict (kind, value) do update set method = 'smartbid', locked = true;

notify pgrst, 'reload schema';
