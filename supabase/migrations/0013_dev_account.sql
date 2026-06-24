-- 0013 — Dev account: a flag, independent of `role`, that lets a profile change
-- its own role from Settings to test the app as any role without re-provisioning.
-- The flag persists across role switches so a dev can always switch back.

alter table profiles add column is_dev boolean not null default false;

-- Flag the known dev account now, if its profile already exists.
update profiles set is_dev = true where email = 'baseballtom33@gmail.com';

-- Profiles are created via Supabase Auth invite, which may happen after this
-- migration runs. Auto-flag the dev account whenever its profile is inserted.
-- Empty search_path is safe (no unqualified object references); mirrors the
-- hardening applied to set_updated_at() in 0010.
create or replace function flag_dev_account()
returns trigger language plpgsql as $$
begin
  if new.email = 'baseballtom33@gmail.com' then
    new.is_dev = true;
  end if;
  return new;
end;
$$;
alter function flag_dev_account() set search_path = '';

create trigger profiles_flag_dev before insert on profiles
  for each row execute function flag_dev_account();
