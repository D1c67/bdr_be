-- 0073 — Project Submittals
--
-- Per-project submittal REQUESTS to vendors (distinct from the company-global
-- Submittal Bank in 0072). Under a PM project, the team picks materials by
-- category, picks vendor contacts of that category (exactly like the RFQ step),
-- and emails each contact a request for product submittals — the category's
-- materials list PDF, always the plans/drawings, and optionally specific spec
-- sheets. Vendor REPLIES thread back through the existing email-ingestion
-- pipeline (the mailbox in EMAIL_INGEST_MAILBOX) because the requests are sent
-- FROM that mailbox; a reply carries the same Graph conversationId as the send.
--
-- A "request" is the whole-modal BATCH (it spans categories); category is an
-- attribute of the line items and the per-contact sends, not of the request.
-- Coverage ("which materials have had submittals requested") is derived: a
-- pm_material is Requested iff a submittal_request_items row references it.
-- Deselecting a material simply never produces an item, so it stays "not
-- requested" and is visible as such on the page. Ad-hoc extras (typed-in lines
-- to cover ourselves) are items with a null pm_material_id.

-- 1. One "Request Submittals" batch. ─────────────────────────────────────────
create table submittal_requests (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  status        text not null default 'sent' check (status in ('sent', 'partial', 'failed')),
  include_specs boolean not null default false,
  spec_document_keys      text[] not null default '{}',   -- hub keys ("source:id") attached
  drawings_delivery       text check (drawings_delivery in ('attached', 'onedrive_link')),
  deselected_material_ids uuid[] not null default '{}',    -- audit of what the sender unchecked
  email_body    text,
  created_by    uuid references profiles(id) on delete set null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index submittal_requests_project_idx on submittal_requests(project_id, created_at desc);
create trigger submittal_requests_updated_at before update on submittal_requests
  for each row execute function set_updated_at();

-- 2. Per-request snapshot line items — material-backed or ad-hoc. ─────────────
--    `description` is a SNAPSHOT so the record survives a later material edit or
--    delete; pm_material_id is nulled (not cascaded) when that happens.
create table submittal_request_items (
  id                   uuid primary key default gen_random_uuid(),
  request_id           uuid not null references submittal_requests(id) on delete cascade,
  material_category_id uuid references material_categories(id) on delete set null,
  category_label       text,                                              -- uncategorized fallback name
  pm_material_id       uuid references pm_materials(id) on delete set null, -- null = ad-hoc extra
  description          text not null,
  source               text not null check (source in ('material', 'adhoc')),
  created_at           timestamptz not null default now()
);
create index submittal_request_items_request_idx  on submittal_request_items(request_id);
create index submittal_request_items_material_idx on submittal_request_items(pm_material_id)
  where pm_material_id is not null;

-- 3. One row per vendor contact emailed (mirror of rfq_sends, 0020). The
--    conversation_id is the reply-matching key; response_* is flipped by the
--    email-ingestion hook when the vendor replies. ────────────────────────────
create table submittal_request_sends (
  id                   uuid primary key default gen_random_uuid(),
  request_id           uuid not null references submittal_requests(id) on delete cascade,
  material_category_id uuid references material_categories(id) on delete set null,
  vendor_contact_id    uuid not null references vendor_contacts(id),
  graph_message_id     text,
  conversation_id      text,                    -- Graph conversationId = reply-matching key
  internet_message_id  text,
  subject              text,
  body                 text,
  status               text not null default 'pending' check (status in ('pending', 'sent', 'failed')),
  error                text,
  response_received_at timestamptz,             -- first vendor reply (set once)
  response_count       int not null default 0,  -- distinct inbound replies matched to this send
  sent_at              timestamptz,
  sent_by              uuid references profiles(id) on delete set null,
  email_log_id         uuid references email_log(id) on delete set null,
  created_at           timestamptz not null default now()
);
create index submittal_request_sends_request_idx      on submittal_request_sends(request_id);
create index submittal_request_sends_conversation_idx on submittal_request_sends(conversation_id);

-- 4. Bind an inbound (ingested) email to the send it answers. The attachment
--    bytes already live in ingested_email_attachments (stored by the ingestion
--    pipeline), so this is a pure link — no re-download, no byte copy. The
--    unique(email_id) makes reply matching idempotent across pipeline re-runs. ─
create table submittal_response_emails (
  id         uuid primary key default gen_random_uuid(),
  send_id    uuid not null references submittal_request_sends(id) on delete cascade,
  email_id   uuid not null references ingested_emails(id) on delete cascade,
  created_at timestamptz not null default now(),
  unique (email_id)
);
create index submittal_response_emails_send_idx on submittal_response_emails(send_id);

-- RLS: deny-by-default with no policies. The service-role backend bypasses RLS;
-- every endpoint gates on the FastAPI require_pm_read/require_pm_write deps, and
-- the ingestion hook runs under the same service-role client.
alter table submittal_requests        enable row level security;
alter table submittal_requests        force  row level security;
alter table submittal_request_items   enable row level security;
alter table submittal_request_items   force  row level security;
alter table submittal_request_sends   enable row level security;
alter table submittal_request_sends   force  row level security;
alter table submittal_response_emails enable row level security;
alter table submittal_response_emails force  row level security;

notify pgrst, 'reload schema';
