-- 0145: close EXECUTE on the remaining public functions (security review,
-- rpc-revoke-migration group). Apply after 0144.
--
-- Postgres grants EXECUTE to PUBLIC on every new function, and Supabase also
-- grants it to anon and authenticated, so PostgREST exposes each one at
-- /rest/v1/rpc/<name> to anyone holding the public anon key. The functions
-- below are SECURITY INVOKER and every table has forced RLS with no policies,
-- so an anon call touches zero rows today. They still should not be callable:
-- the gap opens the moment one becomes SECURITY DEFINER or a policy is added.
-- 0130, 0140, 0141 and 0144 already revoke on their functions; this brings the
-- older ones in line. The backend calls all of them as service_role.
--
-- Trigger functions (set_updated_at, submittal_materials_build_search_text,
-- rfp_email_sightings_append_mailbox) keep firing: Postgres checks EXECUTE
-- when a trigger is created, not when it fires.
--
-- rfp_email_sightings_append_mailbox (0134) was the one function with no
-- pinned search_path; it gets pg_catalog, public like the others.
--
-- pg_trgm's own functions live in public too; they belong to the extension
-- and are left alone.
--
-- Every statement is idempotent.

revoke all on function public.claim_llm_jobs(text, integer, integer, text[]) from public;
revoke all on function public.claim_llm_jobs(text, integer, integer, text[]) from anon, authenticated;
grant execute on function public.claim_llm_jobs(text, integer, integer, text[]) to service_role;

revoke all on function public.remove_project_gc_unless_sent(uuid, uuid, uuid, boolean) from public;
revoke all on function public.remove_project_gc_unless_sent(uuid, uuid, uuid, boolean) from anon, authenticated;
grant execute on function public.remove_project_gc_unless_sent(uuid, uuid, uuid, boolean) to service_role;

revoke all on function public.search_submittals(text, text) from public;
revoke all on function public.search_submittals(text, text) from anon, authenticated;
grant execute on function public.search_submittals(text, text) to service_role;

revoke all on function public.set_updated_at() from public;
revoke all on function public.set_updated_at() from anon, authenticated;
grant execute on function public.set_updated_at() to service_role;

revoke all on function public.submittal_materials_build_search_text() from public;
revoke all on function public.submittal_materials_build_search_text() from anon, authenticated;
grant execute on function public.submittal_materials_build_search_text() to service_role;

alter function public.rfp_email_sightings_append_mailbox() set search_path = pg_catalog, public;
revoke all on function public.rfp_email_sightings_append_mailbox() from public;
revoke all on function public.rfp_email_sightings_append_mailbox() from anon, authenticated;
grant execute on function public.rfp_email_sightings_append_mailbox() to service_role;

notify pgrst, 'reload schema';
