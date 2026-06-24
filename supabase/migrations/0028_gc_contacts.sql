-- 0028 — GC contacts: multiple named contacts per general contractor,
-- mirroring vendor_contacts. The company row keeps only the name; the legacy
-- single contact/email/phone columns migrate into gc_contacts and are dropped.
-- Proposal recipients are now PICKED PER SEND from these contacts in the
-- confirm dialog; proposal_sends.gc_email changes meaning from "snapshot at
-- generation" to "the recipient list the send actually used" (written when the
-- row is claimed for sending, so crash recovery can match it against
-- email_log.to_addrs).

create table gc_contacts (
  id          uuid primary key default gen_random_uuid(),
  gc_id       uuid not null references general_contractors(id) on delete cascade,
  name        text not null,
  email       text,          -- nullable: phone-only contacts are fine; sends need an email
  phone       text,
  created_at  timestamptz not null default now()
);
create index gc_contacts_gc_idx on gc_contacts(gc_id);

-- Deny-by-default RLS (service-role backend bypasses it; the estimator has no
-- route to this table).
alter table gc_contacts enable row level security;
alter table gc_contacts force row level security;

-- Carry the legacy single-contact fields over as the first contact. Rows with
-- no contact info at all get no contact row; a blank contact name falls back
-- to the company name so the not-null holds.
insert into gc_contacts (gc_id, name, email, phone)
select id,
       coalesce(nullif(trim(contact), ''), name),
       nullif(trim(email), ''),
       nullif(trim(phone), '')
from general_contractors
where coalesce(nullif(trim(contact), ''), nullif(trim(email), ''), nullif(trim(phone), '')) is not null;

alter table general_contractors
  drop column contact,
  drop column email,
  drop column phone;
