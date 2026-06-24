-- 0004 — Vendors, RFQs, and received quotes (steps 5-6).

create table vendors (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  notes       text,
  created_at  timestamptz not null default now()
);

create table vendor_contacts (
  id                    uuid primary key default gen_random_uuid(),
  vendor_id             uuid not null references vendors(id) on delete cascade,
  name                  text not null,
  email                 text not null,
  phone                 text,
  material_category_id  uuid references material_categories(id),
  created_at            timestamptz not null default now()
);
create index vendor_contacts_vendor_idx on vendor_contacts(vendor_id);
create index vendor_contacts_category_idx on vendor_contacts(material_category_id);

-- One RFQ per material category per project.
create table rfqs (
  id                    uuid primary key default gen_random_uuid(),
  project_id            uuid not null references projects(id) on delete cascade,
  material_category_id  uuid not null references material_categories(id),
  due_date              date,
  status                rfq_status not null default 'draft',
  split_file_id         uuid references project_files(id),
  created_by            uuid references profiles(id),
  created_at            timestamptz not null default now(),
  unique (project_id, material_category_id)
);
create index rfqs_project_idx on rfqs(project_id);

-- Which vendor contacts an RFQ was emailed to.
create table rfq_recipients (
  id                uuid primary key default gen_random_uuid(),
  rfq_id            uuid not null references rfqs(id) on delete cascade,
  vendor_contact_id uuid not null references vendor_contacts(id),
  email_log_id      uuid,        -- FK added in 0006 after email_log exists
  sent_at           timestamptz,
  unique (rfq_id, vendor_contact_id)
);
create index rfq_recipients_rfq_idx on rfq_recipients(rfq_id);

-- Quotes received back from vendors; PE selects the best price per RFQ.
create table quotes (
  id                uuid primary key default gen_random_uuid(),
  rfq_id            uuid not null references rfqs(id) on delete cascade,
  vendor_id         uuid not null references vendors(id),
  vendor_contact_id uuid references vendor_contacts(id),
  amount            numeric(14,2) not null,
  quote_file_id     uuid references project_files(id),
  is_selected       boolean not null default false,
  notes             text,
  received_at       timestamptz not null default now()
);
create index quotes_rfq_idx on quotes(rfq_id);
-- At most one selected quote per RFQ.
create unique index quotes_one_selected_per_rfq
  on quotes(rfq_id) where is_selected;
