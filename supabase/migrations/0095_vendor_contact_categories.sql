-- 0095 - Vendors serve multiple material categories.
--
-- Until now a vendor contact carried exactly one category
-- (vendor_contacts.material_category_id, 0004). That modelled "one person per
-- trade at the company", but real vendors staff people who quote several
-- categories at once: a switchgear rep who also handles lighting had to be
-- entered twice, once per category. That duplicated them in every RFQ
-- recipient list and split their send history across two unrelated contact
-- rows, so "have we heard back from Jane?" had two answers.
--
-- This replaces the single FK with a link table. A contact now carries a SET of
-- categories, and a vendor company's categories are the union across its
-- contacts. The recipient filter (GET /vendor-contacts?material_category_id=,
-- used by RFQ send and PM submittal requests) resolves through this table, so a
-- multi-category contact shows up under every category they serve while staying
-- one row with one history.
--
-- The old column is backfilled and then dropped: keeping it would leave two
-- sources of truth for the same fact, and it had exactly one reader in the
-- backend (the category filter in routers/vendors.py). Contacts that were
-- uncategorized (material_category_id null, allowed since 0004) simply get no
-- link rows and keep reading as "Uncategorized".
--
-- ON DELETE CASCADE on both sides: a link means nothing once either end is
-- gone. This is looser than the old column's implicit NO ACTION, which blocked
-- deleting a category that any contact used, but categories are retired via
-- is_active (0002) rather than deleted, and a hard delete losing its links is
-- the graceful outcome (the contact falls back to uncategorized).

create table vendor_contact_categories (
  vendor_contact_id    uuid not null references vendor_contacts(id)     on delete cascade,
  material_category_id uuid not null references material_categories(id) on delete cascade,
  created_at           timestamptz not null default now(),
  primary key (vendor_contact_id, material_category_id)
);

-- The primary key already covers "which categories does this contact serve".
-- This index covers the hot direction, "who quotes this category", which is
-- what the RFQ/submittal recipient filter runs on every send.
create index vendor_contact_categories_category_idx
  on vendor_contact_categories(material_category_id);

-- Backfill: every contact that had a category keeps it as its only one.
insert into vendor_contact_categories (vendor_contact_id, material_category_id)
select id, material_category_id
from vendor_contacts
where material_category_id is not null
on conflict do nothing;

-- Dropping the column also drops vendor_contacts_category_idx (0004).
alter table vendor_contacts drop column material_category_id;

-- RLS: deny-by-default with no policies, matching vendor_contacts itself. The
-- service-role backend bypasses RLS; every endpoint gates on require_internal
-- (read) or require_writer (write).
alter table vendor_contact_categories enable row level security;
alter table vendor_contact_categories force  row level security;

notify pgrst, 'reload schema';
