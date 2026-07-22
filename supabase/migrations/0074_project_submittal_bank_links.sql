-- 0074 — Project ↔ Submittal Bank links
--
-- Attaches Submittal Bank items (0072) to a project's materials (0062) — the
-- join table the 0072 header called "a future concern". Powers the "Other
-- submittals — fill from the bank" section of the Project Submittals page: for
-- the materials that are NOT requested from a vendor (General Material and
-- self-performed / uncategorized items), the team either PULLS a matching bank
-- submittal (fuzzy `search_submittals`) or UPLOADS a PDF for one the bank
-- doesn't cover. An uploaded PDF is archived into the Documents hub
-- (pm_documents, category 'submittal') and can later be pushed into the global
-- bank (bank_material_id records that it was).
--
-- One row = one submittal covering one pm_material, of exactly one source:
--   • 'bank'     → submittal_material_id set  (preview via that material's files)
--   • 'uploaded' → document_id set            (a pm_documents PDF; hub key pm:<id>)

create table pm_material_submittals (
  id                    uuid primary key default gen_random_uuid(),
  project_id            uuid not null references projects(id) on delete cascade,
  pm_material_id        uuid not null references pm_materials(id) on delete cascade,
  source                text not null check (source in ('bank', 'uploaded')),
  submittal_material_id uuid references submittal_materials(id) on delete cascade,  -- source='bank'
  document_id           uuid references pm_documents(id) on delete cascade,         -- source='uploaded'
  bank_material_id      uuid references submittal_materials(id) on delete set null, -- set once pushed to bank
  created_by            uuid references profiles(id) on delete set null,
  created_at            timestamptz not null default now(),
  -- Exactly one target per source; the other target column stays null.
  check (
    (source = 'bank'     and submittal_material_id is not null and document_id is null) or
    (source = 'uploaded' and document_id is not null and submittal_material_id is null)
  )
);

-- A material carries a given bank submittal at most once; an uploaded PDF is
-- linked at most once. NULLs are distinct in a unique index, so 'uploaded' rows
-- (submittal_material_id null) never collide on the first index and 'bank' rows
-- (document_id null) never collide on the second.
create unique index pm_material_submittals_bank_uq
  on pm_material_submittals(pm_material_id, submittal_material_id);
create unique index pm_material_submittals_doc_uq
  on pm_material_submittals(pm_material_id, document_id);
create index pm_material_submittals_project_idx  on pm_material_submittals(project_id);
create index pm_material_submittals_material_idx on pm_material_submittals(pm_material_id);

-- RLS: deny-by-default with no policies. The service-role backend bypasses RLS;
-- every endpoint gates on require_pm_read/require_pm_write.
alter table pm_material_submittals enable row level security;
alter table pm_material_submittals force  row level security;

notify pgrst, 'reload schema';
