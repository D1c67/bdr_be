-- 0072 — Submittal Bank
--
-- A company-GLOBAL (not project-scoped) library of submittal PDFs for
-- General Materials / Low Voltage / Switchgear (lighting & generators are
-- vendor-provided and intentionally excluded). Attaching bank submittals to
-- project materials is a future concern (a later join table).
--
-- Findability is the point: users won't type exact names, so each material row
-- carries a trigger-maintained `search_text` (name + manufacturer + size +
-- color + category + AI/manual aliases) that a pg_trgm fuzzy `search_submittals`
-- RPC ranks over. Files are MANY-TO-MANY with materials: one PDF can cover many
-- materials (a "group", so a shared cut-sheet isn't stored per size/color), and
-- one material can carry many vendors' PDFs.

create extension if not exists pg_trgm;   -- similarity / word_similarity / % + gin_trgm_ops

create type submittal_category as enum ('general_material', 'low_voltage', 'switchgear');

-- 1. Materials — the atomic, searchable bank item (one size/color SKU). ────────
--    name + made_in_usa live here (they may differ per size/color), so a "group"
--    is simply several material rows that share a file (see the M:N table).
create table submittal_materials (
  id            uuid primary key default gen_random_uuid(),
  category      submittal_category not null,
  name          text not null,
  size          text,
  color         text,
  made_in_usa   boolean,                        -- nullable tri-state: yes / no / unknown
  manufacturer  text,
  aliases       text[] not null default '{}',   -- AI-generated + hand-edited search aliases
  search_text   text not null default '',       -- maintained by trigger below
  notes         text,
  created_by    uuid references profiles(id) on delete set null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index submittal_materials_category_idx on submittal_materials(category);
create index submittal_materials_search_trgm_idx
  on submittal_materials using gin (search_text gin_trgm_ops);
create trigger submittal_materials_updated_at before update on submittal_materials
  for each row execute function set_updated_at();

-- 2. Files — a PDF document, reusable across materials. A row exists only once a
--    PDF is uploaded; "upload later" = a material with zero linked files. ───────
create table submittal_files (
  id            uuid primary key default gen_random_uuid(),
  vendor        text,                            -- the supplier this submittal is from (multi-vendor)
  title         text,
  file_path     text not null,                   -- storage object path in the project-files bucket
  file_name     text not null,
  size_bytes    bigint,
  notes         text,
  uploaded_by   uuid references profiles(id) on delete set null,
  created_at    timestamptz not null default now()
);

-- 3. The M:N link — this single table is BOTH the "group" (one file → many
--    materials) and the "multi-vendor" (one material → many files) mechanism. ──
create table submittal_material_files (
  material_id   uuid not null references submittal_materials(id) on delete cascade,
  file_id       uuid not null references submittal_files(id) on delete cascade,
  created_by    uuid references profiles(id) on delete set null,
  created_at    timestamptz not null default now(),
  primary key (material_id, file_id)
);
create index submittal_material_files_file_idx on submittal_material_files(file_id);

-- search_text maintenance: single-table (every input is on the row, incl.
-- aliases[]), recomputed on every insert/update — no cross-table drift.
create or replace function submittal_materials_build_search_text()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  new.search_text := lower(concat_ws(' ',
    new.name,
    new.manufacturer,
    new.size,
    new.color,
    new.category::text,
    array_to_string(new.aliases, ' ')
  ));
  return new;
end;
$$;
create trigger submittal_materials_search_text_biu
  before insert or update on submittal_materials
  for each row execute function submittal_materials_build_search_text();

-- Fuzzy search RPC (the query builder can't express ORDER BY similarity()).
-- SECURITY INVOKER: the backend calls it as the service-role/BYPASSRLS key, so
-- it reads the (policy-less, RLS-forced) tables fine without SECURITY DEFINER.
-- search_path is pinned (function_search_path_mutable advisor) and includes
-- `extensions` so pg_trgm's operators/functions resolve whether the extension
-- landed in public or in Supabase's extensions schema.
-- WHERE is permissive (substring OR trigram); ORDER ranks by word_similarity so
-- the best fuzzy match floats up. The ilike substring path uses the GIN index.
create or replace function search_submittals(q text, cat text default null)
returns setof submittal_materials
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
  select m.*
  from submittal_materials m
  where (cat is null or m.category::text = cat)
    and (
      q is null or btrim(q) = ''
      or m.search_text ilike '%' || q || '%'
      or word_similarity(q, m.search_text) > 0.2
    )
  order by
    case when q is null or btrim(q) = '' then 0
         else word_similarity(q, m.search_text) end desc,
    m.name asc;
$$;

alter table submittal_materials      enable row level security;
alter table submittal_materials      force  row level security;
alter table submittal_files          enable row level security;
alter table submittal_files          force  row level security;
alter table submittal_material_files enable row level security;
alter table submittal_material_files force  row level security;

notify pgrst, 'reload schema';
