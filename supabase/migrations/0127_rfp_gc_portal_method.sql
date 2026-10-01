-- 0127 - RFP Ingestion: add gc_portal as an invitation method
-- (docs/RFP_EMAIL_INGESTION.md, sections 1, 3.7 and 3.8; docs/RFP_HARVEST.md
-- section 2.3). Apply after 0124. Numbered 0127 because 0126 is reserved by
-- the NGEM portal slice.
--
-- A GC that invites through its own bidding portal gets the method
-- `gc_portal`. Every such portal is different, so the harvest step picks the
-- scraper by the sender's domain; the method itself is granted by a locked
-- rule the IT Admin adds on that domain, which is how it outranks the same
-- GC's organic match. No rows change: the two named check constraints from
-- 0124 are widened by one value each.
--
-- Method vocabulary after this migration:
--   rfp_emails.invitation_method : organic, planhub, procore, gc_portal, general, nonorganic
--   rfp_authorized_senders.method: planhub, procore, gc_portal, general
--
-- Every statement here is idempotent. Both DO blocks look the constraint up
-- by column (the 0124 pattern), so this also works on a database that still
-- carries the unnamed inline 0120 check.

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
    check (invitation_method in ('organic', 'planhub', 'procore', 'gc_portal', 'general', 'nonorganic'));
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
    check (method in ('planhub', 'procore', 'gc_portal', 'general'));
end
$$;

notify pgrst, 'reload schema';
