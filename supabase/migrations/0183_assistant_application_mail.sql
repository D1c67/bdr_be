-- Owner decision 2026-10-26: application access only. Preserve 0182 history,
-- retire its unused OAuth surfaces and discard any remaining token material.
-- Existing BDR MS_* application permissions are not changed by this migration.
do $$
declare f record;
begin
 if exists(select 1 from pg_trigger where tgname='assistant_ms_profile_changed'
  and tgrelid='public.profiles'::regclass) then
  execute 'drop trigger assistant_ms_profile_changed on public.profiles';
 end if;
 if to_regclass('public.assistant_ms_connections') is not null then
  update public.assistant_ms_connections set generation=generation+1, cache_version=cache_version+1,
   status='revoked', cache_ciphertext=null, cache_nonce=null, cache_key_id=null,
   refresh_claim_id=null, refresh_claim_until=null, token_expires_at=null,
   revoked_at=now(), revoked_reason='application_access_selected', updated_at=now();
  revoke all on public.assistant_ms_connections from public,anon,authenticated,service_role;
 end if;
 if to_regclass('public.assistant_ms_oauth_states') is not null then
  update public.assistant_ms_oauth_states set flow_ciphertext=null,flow_nonce=null,flow_key_id=null,
   expires_at=least(expires_at,now());
  revoke all on public.assistant_ms_oauth_states from public,anon,authenticated,service_role;
 end if;
 for f in select p.oid::regprocedure as signature from pg_proc p
  join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname in (
   'assistant_ms_eligible','assistant_ms_binding_valid','assistant_ms_begin','assistant_ms_consume',
   'assistant_ms_complete','assistant_ms_refresh_claim','assistant_ms_refresh_commit',
   'assistant_ms_refresh_release','assistant_ms_revoke','assistant_ms_profile_changed',
   'assistant_ms_purge_expired')
 loop
  execute format('revoke all on function %s from public,anon,authenticated,service_role', f.signature);
 end loop;
end $$;
notify pgrst, 'reload schema';
