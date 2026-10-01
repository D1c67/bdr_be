-- RFP email visibility: per-mailbox ownership (docs/RFP_EMAIL_VISIBILITY.md).
--
-- A row of rfp_emails belongs to the mailboxes that received it. The
-- sightings table already records that; this migration denormalizes the
-- mailbox list onto the row so the API can filter with one array overlap,
-- and gives profiles the list of mailboxes a person owns.
--
-- Idempotent. Dev only until release.

alter table profiles
  add column if not exists rfp_mailboxes text[] not null default '{}';

alter table rfp_emails
  add column if not exists mailboxes text[] not null default '{}';

create index if not exists rfp_emails_mailboxes_gin
  on rfp_emails using gin (mailboxes);

-- Keep rfp_emails.mailboxes in step with the sightings. Every ingest path
-- (poller, harvest sighting writers, testing bench) inserts a sighting and
-- nothing else has to remember the array. Sightings are never deleted.
create or replace function rfp_email_sightings_append_mailbox()
returns trigger
language plpgsql
as $$
declare
  mb text := lower(trim(new.mailbox));
begin
  if mb is null or mb = '' then
    return new;
  end if;
  update rfp_emails
     set mailboxes = array_append(mailboxes, mb)
   where id = new.rfp_email_id
     and not (mailboxes @> array[mb]);
  return new;
end;
$$;

drop trigger if exists rfp_email_sightings_append_mailbox on rfp_email_sightings;
create trigger rfp_email_sightings_append_mailbox
  after insert on rfp_email_sightings
  for each row execute function rfp_email_sightings_append_mailbox();

-- Backfill from the sightings that already exist.
update rfp_emails e
   set mailboxes = s.mailboxes
  from (
    select rfp_email_id,
           array_agg(distinct lower(trim(mailbox)) order by lower(trim(mailbox))) as mailboxes
      from rfp_email_sightings
     where mailbox is not null and trim(mailbox) <> ''
     group by rfp_email_id
  ) s
 where e.id = s.rfp_email_id
   and e.mailboxes <> s.mailboxes;

notify pgrst, 'reload schema';
