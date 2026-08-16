-- Per-project GC bid contacts + user-chosen CC on proposal sends.
--
-- project_gc_contacts: which specific people at each GC company the team
-- intends to bid to on THIS project. Chosen at project creation or in the
-- project's General Contractors modal. It is a hint, not a gate: the Send Out
-- confirm dialog seeds and highlights these contacts, but the sender may still
-- email any contact of the GC. Rows hang off the project_gcs link row so
-- removing a GC from the project (or deleting the project) cleans them up.
--
-- proposal_send_events.cc_recipients: snapshot of the same-company GC contacts
-- the sender chose to CC on that transmission, as
-- [{"gc_contact_id", "name", "email"}]. Null/absent = no GC CC. Mirrors
-- rfq_sends.cc_recipients (0096). The standing internal CC (PROPOSAL_CC) is
-- config, not data, and is deliberately NOT recorded here; the To line contract
-- (proposal_sends.gc_email == email_log.to_addrs) is untouched.

create table if not exists project_gc_contacts (
  id uuid primary key default gen_random_uuid(),
  project_gc_id uuid not null references project_gcs (id) on delete cascade,
  gc_contact_id uuid not null references gc_contacts (id) on delete cascade,
  created_at timestamptz not null default now(),
  constraint project_gc_contacts_link_contact_key unique (project_gc_id, gc_contact_id)
);

-- The unique constraint above indexes the project_gc_id prefix; this one keeps
-- gc_contacts deletes (FK cascade) off a sequential scan.
create index if not exists project_gc_contacts_contact_idx
  on project_gc_contacts (gc_contact_id);

alter table project_gc_contacts enable row level security;
alter table project_gc_contacts force row level security;

comment on table project_gc_contacts is
  'Per-project preferred bid contacts at a GC company. Advisory: seeds/highlights the Send Out recipient picker; any GC contact may still be emailed.';

alter table proposal_send_events
  add column if not exists cc_recipients jsonb;

comment on column proposal_send_events.cc_recipients is
  'Same-GC contacts CC''d on this transmission: [{"gc_contact_id","name","email"}]. Null = none. Internal standing CC (PROPOSAL_CC) is not recorded here.';
