-- Raise the per-bucket upload ceiling on project-files from 100 MB to 300 MB
-- to match the app cap (upload_max_bytes, raised to 300 MB in the same hotfix)
-- and the project's global storage limit (raised to 300 MB in the Dashboard).
-- Supabase Storage enforces min(global project limit, bucket file_size_limit),
-- so all three must be at 300 MB for uploads over 100 MB to succeed.
update storage.buckets
   set file_size_limit = 314572800  -- 300 MB
 where id = 'project-files';
