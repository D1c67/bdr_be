-- 0046 — flag projects that originated from NGEM.
-- Set at intake via a checkbox on the New Project form; editable afterwards from
-- the project details box. Defaults to false so existing rows read as "not NGEM".
alter table public.projects
    add column if not exists is_ngem boolean not null default false;

-- Reload PostgREST's schema cache so the new column is visible immediately.
notify pgrst, 'reload schema';
