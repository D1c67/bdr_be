-- 0120 - RFP Ingestion: email intake (docs/RFP_EMAIL_INGESTION.md).
--
-- Watches the mailboxes in RFP_EMAIL_INGESTION_INBOXES_ALLOWED, authenticates
-- and classifies every external message, decides whether the sender is
-- authorized and labels the invitation method. One row per message (by the
-- normalized Internet Message-ID) plus one sighting row per watched mailbox
-- that received it. `status` names the NEXT step to run (crash-resumable
-- sweep, same design as ingested_emails / 0061).
--
-- Status vocabulary:
--   pending  : received, auth, keywords, classify, authorize, method
--   human    : review_llm, flagged_unauthorized
--   terminal : done, flagged_auth, flagged_no_keywords, flagged_llm_no,
--              rejected_by_review, failed

create table rfp_emails (
  id                      uuid primary key default gen_random_uuid(),
  internet_message_id     text not null,           -- normalized; unique below
  primary_mailbox         text not null,           -- mailbox the body is fetched from
  conversation_id         text,
  from_address            text not null,
  from_name               text,
  to_recipients           jsonb not null default '[]'::jsonb,   -- [{name, address}]
  cc_recipients           jsonb not null default '[]'::jsonb,
  subject                 text,
  body_preview            text,
  body_text               text,                    -- plain text, capped
  received_at             timestamptz not null,
  has_attachments         boolean not null default false,
  attachments_meta        jsonb not null default '[]'::jsonb,   -- [{name, contentType, size, kind}]
  -- Authentication-Results verdicts (tenant-stamped header only)
  auth_spf                text,
  auth_dkim               text,
  auth_dmarc              text,
  auth_compauth           text,
  auth_raw                text,
  auth_verdict            text check (auth_verdict in ('pass', 'fail')),
  -- Keyword gate
  keyword_hits            text[] not null default '{}',
  -- LLM classification
  llm_answer              text check (llm_answer in ('yes', 'no', 'undetermined')),
  llm_confidence          numeric(4,3),
  llm_reasoning           text,
  llm_model               text,
  llm_prompt_version      text,
  -- Human review of the LLM verdict
  review_decision         text check (review_decision in ('yes', 'no')),
  review_by               uuid references profiles(id) on delete set null,
  review_at               timestamptz,
  -- Authorization
  authorization_kind      text check (authorization_kind in ('address', 'domain', 'gc_domain', 'override')),
  authorization_rule_id   uuid,                    -- FK added after the rules table exists
  continued_by            uuid references profiles(id) on delete set null,
  continued_at            timestamptz,
  invitation_method       text check (invitation_method in
                            ('organic', 'planhub', 'buildingconnected', 'procore', 'ngem', 'general', 'nonorganic')),
  -- Pipeline bookkeeping
  status                  text not null default 'received' check (status in (
                            'received', 'auth', 'keywords', 'classify', 'authorize', 'method',
                            'review_llm', 'flagged_unauthorized',
                            'done', 'flagged_auth', 'flagged_no_keywords', 'flagged_llm_no',
                            'rejected_by_review', 'failed')),
  flag_reason             text,
  decided_at_step         text,
  attempts                int not null default 0,
  next_attempt_at         timestamptz,
  last_error              text,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now()
);

create unique index rfp_emails_message_id_uidx on rfp_emails(internet_message_id);
create index rfp_emails_conversation_idx on rfp_emails(conversation_id);
create index rfp_emails_received_idx on rfp_emails(received_at desc);
create index rfp_emails_status_idx on rfp_emails(status, next_attempt_at);
create index rfp_emails_from_idx on rfp_emails(lower(from_address));

-- One row per watched mailbox that received the message.
create table rfp_email_sightings (
  id                uuid primary key default gen_random_uuid(),
  rfp_email_id      uuid not null references rfp_emails(id) on delete cascade,
  mailbox           text not null,
  graph_message_id  text not null,
  received_at       timestamptz,
  created_at        timestamptz not null default now()
);
create unique index rfp_email_sightings_mailbox_uidx on rfp_email_sightings(mailbox, graph_message_id);
create index rfp_email_sightings_email_idx on rfp_email_sightings(rfp_email_id);

-- Authorized senders. Locked rows are the platform seeds (IT Admin only).
-- Non-locked rows always carry method 'general'.
create table rfp_authorized_senders (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null check (kind in ('address', 'domain')),
  value       text not null,                       -- lowercased address or bare domain
  method      text not null default 'general' check (method in
                ('planhub', 'buildingconnected', 'procore', 'ngem', 'general')),
  locked      boolean not null default false,
  created_by  uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now()
);
create unique index rfp_authorized_senders_uidx on rfp_authorized_senders(kind, value);

alter table rfp_emails
  add constraint rfp_emails_rule_fk
  foreign key (authorization_rule_id) references rfp_authorized_senders(id) on delete set null;

insert into rfp_authorized_senders (kind, value, method, locked) values
  ('domain',  'message.planhub.com',          'planhub',            true),
  ('address', 'team@buildingconnected.com',   'buildingconnected',  true),
  ('domain',  'procoretech.com',              'procore',            true),
  ('address', 'nevada@customer.ionwave.net',  'ngem',               true);

-- Human review decisions captured for prompt/threshold tuning.
create table rfp_classify_training (
  id              uuid primary key default gen_random_uuid(),
  rfp_email_id    uuid not null references rfp_emails(id) on delete cascade,
  subject         text,
  body_excerpt    text,
  llm_answer      text,
  llm_confidence  numeric(4,3),
  human_answer    text not null check (human_answer in ('yes', 'no')),
  decided_by      uuid references profiles(id) on delete set null,
  created_at      timestamptz not null default now()
);
create index rfp_classify_training_email_idx on rfp_classify_training(rfp_email_id);

-- Deny-by-default RLS; the service-role backend is the only reader.
alter table rfp_emails             enable row level security;
alter table rfp_emails             force row level security;
alter table rfp_email_sightings    enable row level security;
alter table rfp_email_sightings    force row level security;
alter table rfp_authorized_senders enable row level security;
alter table rfp_authorized_senders force row level security;
alter table rfp_classify_training  enable row level security;
alter table rfp_classify_training  force row level security;

notify pgrst, 'reload schema';
