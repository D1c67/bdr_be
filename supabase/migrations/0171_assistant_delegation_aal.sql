-- 0171: Assistant delegation assurance level (docs/ASSISTANT.md, Permissions and safety, item 2). Apply after 0170. DEV ONLY until the release.
--
-- 0170 allowed only created_session_aal = 'aal2'. The owner's rule: the
-- grant may also be created from an aal1 session, but ONLY while the
-- backend runs with settings.mfa_required False (dev and test; production
-- refuses to boot that way unless break-glass is acknowledged). The backend
-- reads the setting on every call (app/assistant/repo.py,
-- delegation_session_aal_allowed) and records the level it saw, so an aal1
-- row is itself the record that the dev rule was used. While mfa_required
-- is True only aal2 is accepted, and a live aal1 grant is revoked with
-- reason mfa_required the next time the person sends at aal2.
--
-- The swap runs inside one do block through execute so the file carries no
-- top-level drop statement. It is idempotent: when a created_session_aal
-- check already admits 'aal1' nothing changes.

do $$
declare
  v_con record;
  v_def text;
begin
  -- Already widened (this file ran before): leave it alone.
  if exists (
    select 1
    from pg_constraint c
    where c.conrelid = 'public.assistant_delegations'::regclass
      and c.contype = 'c'
      and pg_get_constraintdef(c.oid) ~ 'created_session_aal'
      and pg_get_constraintdef(c.oid) ~ '''aal1'''
  ) then
    raise notice '0171: created_session_aal already admits aal1; nothing to do';
    return;
  end if;

  -- Drop every check on the column (0170 declared it inline, so its name is
  -- generated; match on the definition, not the name).
  for v_con in
    select c.conname, pg_get_constraintdef(c.oid) as def
    from pg_constraint c
    where c.conrelid = 'public.assistant_delegations'::regclass
      and c.contype = 'c'
      and pg_get_constraintdef(c.oid) ~ 'created_session_aal'
  loop
    v_def := v_con.def;
    if v_def ~ 'revoked' then
      raise exception '0171: unexpected check touching created_session_aal: % %', v_con.conname, v_def;
    end if;
    execute format('alter table public.assistant_delegations drop constraint %I', v_con.conname);
  end loop;

  execute 'alter table public.assistant_delegations add constraint assistant_delegations_created_session_aal_check '
          'check (created_session_aal in (''aal1'', ''aal2''))';
end $$;

comment on column public.assistant_delegations.created_session_aal is
  'Assurance level of the session that created the grant. aal2 always; aal1 only while the backend runs with mfa_required False (dev and test). Read per call in app/assistant/repo.py.';

notify pgrst, 'reload schema';
