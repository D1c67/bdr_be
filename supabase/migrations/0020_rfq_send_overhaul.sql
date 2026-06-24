-- 0020 — RFQ send overhaul: per-contact sends with Graph conversation tracking,
-- inbound vendor replies, AI-extracted quotes with manual-override history.

-- Vendor quote deadline, entered on the RFQ step and used in every RFQ email.
alter table projects add column due_from_vendors_at timestamptz;

-- New vendor category; the estimator's markup files are auto-attached to its emails.
insert into material_categories (name, kind, sort_order)
values ('Trenching', 'material', 55)
on conflict do nothing;

-- One row per individual email sent to a vendor contact (no CC — each contact
-- gets their own message and Graph conversation). Supersedes rfq_recipients,
-- which is kept (and still upserted) for back-compat reads for now.
create table rfq_sends (
  id                   uuid primary key default gen_random_uuid(),
  rfq_id               uuid not null references rfqs(id) on delete cascade,
  vendor_contact_id    uuid not null references vendor_contacts(id),
  graph_message_id     text,            -- immutable id of the sent message
  conversation_id      text,            -- Graph conversationId — reply matching key
  internet_message_id  text,
  subject              text not null,
  body                 text,            -- final (AI-varied) body actually sent
  status               text not null default 'pending'
                       check (status in ('pending', 'sent', 'failed')),
  error                text,
  polling_active       boolean not null default true,
  quote_received_at    timestamptz,
  sent_at              timestamptz,
  sent_by              uuid references profiles(id),
  email_log_id         uuid references email_log(id) on delete set null,
  created_at           timestamptz not null default now()
);
create index rfq_sends_rfq_idx on rfq_sends(rfq_id);
create index rfq_sends_conversation_idx on rfq_sends(conversation_id);
create index rfq_sends_active_idx on rfq_sends(polling_active, sent_at)
  where polling_active;

-- Inbound vendor replies matched to a send by conversationId.
create table rfq_messages (
  id                 uuid primary key default gen_random_uuid(),
  rfq_send_id        uuid not null references rfq_sends(id) on delete cascade,
  graph_message_id   text not null unique,   -- idempotency key for the poller
  from_addr          text,
  subject            text,
  body_preview       text,
  body               text,
  received_at        timestamptz,
  has_attachments    boolean not null default false,
  extraction_status  text not null default 'skipped'
                     check (extraction_status in
                            ('skipped', 'pending', 'done', 'no_amount', 'failed')),
  extraction_error   text,
  created_at         timestamptz not null default now()
);
create index rfq_messages_send_idx on rfq_messages(rfq_send_id);

-- Quote provenance: where the number came from and the raw AI extraction.
alter table quotes add column source text not null default 'manual'
  check (source in ('manual', 'ai_extracted'));
alter table quotes add column rfq_send_id    uuid references rfq_sends(id)    on delete set null;
alter table quotes add column rfq_message_id uuid references rfq_messages(id) on delete set null;
alter table quotes add column ai_extraction  jsonb;

-- Audit trail for manual changes to quote amounts (incl. overriding AI numbers).
create table quote_revisions (
  id              uuid primary key default gen_random_uuid(),
  quote_id        uuid not null references quotes(id) on delete cascade,
  previous_amount numeric(14,2),
  new_amount      numeric(14,2) not null,
  previous_source text,
  changed_by      uuid references profiles(id),
  changed_at      timestamptz not null default now(),
  note            text
);
create index quote_revisions_quote_idx on quote_revisions(quote_id);

-- Graph inbox delta-token storage + single-runner lease for the poller.
create table graph_sync_state (
  id           text primary key,        -- e.g. 'inbox:bids@g3electrical.com'
  delta_link   text,
  lease_until  timestamptz,
  updated_at   timestamptz not null default now()
);

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
alter table rfq_sends        enable row level security;
alter table rfq_messages     enable row level security;
alter table quote_revisions  enable row level security;
alter table graph_sync_state enable row level security;
