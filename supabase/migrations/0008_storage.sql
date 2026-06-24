-- 0008 — Storage buckets.
--
-- Single private bucket for all project files (drawings, estimates, BOQs,
-- markups, RFQ splits, quotes). Private = no public URLs; the backend issues
-- short-TTL signed URLs (especially important for the hardened estimator).
-- No storage RLS policies are added, so only the service-role backend can
-- read/write objects directly.

insert into storage.buckets (id, name, public)
values ('project-files', 'project-files', false)
on conflict (id) do nothing;
