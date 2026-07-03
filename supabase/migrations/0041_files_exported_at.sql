-- 0041 — track the last successful project-files export.
-- Drives the post-send-out "export your files to the team" banner: the banner
-- shows once bids are out and clears (for everyone on the project) the first
-- time someone exports the files.
alter table public.projects
    add column if not exists files_exported_at timestamptz;

-- Reload PostgREST's schema cache so the new column is visible immediately.
notify pgrst, 'reload schema';
