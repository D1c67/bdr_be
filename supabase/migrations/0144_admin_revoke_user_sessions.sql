-- 0144: revoke every session a user holds (security review, users-admin group).
-- Apply after 0143.
--
-- GoTrue's /logout only signs out the session whose JWT is presented; the
-- admin API has no "sign this user out everywhere" call. When an IT Admin or
-- Executive moves a user's login email or resets their MFA, the old refresh
-- tokens must stop working, otherwise a browser signed in before the change
-- keeps minting fresh access tokens. Deleting the auth.sessions rows cascades
-- their refresh tokens; the second delete covers legacy tokens with no
-- session. Access tokens already issued stay valid until they expire (about
-- an hour), which is the same limit GoTrue's own global logout has.
--
-- Called only by the backend with the service role (app/routers/users.py
-- _revoke_sessions). Returns how many sessions were removed.
--
-- Every statement is idempotent.

create or replace function public.admin_revoke_user_sessions(p_user_id uuid)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  n integer;
begin
  delete from auth.sessions where user_id = p_user_id;
  get diagnostics n = row_count;
  delete from auth.refresh_tokens where user_id = p_user_id::text;
  return n;
end;
$$;

revoke all on function public.admin_revoke_user_sessions(uuid) from public;
revoke all on function public.admin_revoke_user_sessions(uuid) from anon, authenticated;
grant execute on function public.admin_revoke_user_sessions(uuid) to service_role;

notify pgrst, 'reload schema';
