-- 0010 — Security hardening: pin the trigger function's search_path.
-- Resolves the `function_search_path_mutable` advisor warning. now() lives in
-- pg_catalog (always on the path) so an empty search_path is safe here.

alter function set_updated_at() set search_path = '';
