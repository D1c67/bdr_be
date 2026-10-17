-- 0150: live switch for the self-hosted LLM box (docs/SELF_HOSTED_LLM.md).
--
-- GPU capacity on AWS is unreliable, so IT Admins and devs can move every AI
-- feature to another EC2 box from Settings without a Railway redeploy. The
-- env var SELF_HOSTED_LLM_TARGET stays the default; a non-null
-- self_hosted_target here overrides it in every worker within a few seconds
-- (services/llm_target polls this row). Base URLs, keys and models still come
-- from the env, so this row only ever names a target (local | ec2 | ec2b |
-- ec2c), never a URL. Target names are checked in the app, not here, so
-- adding a box needs no migration.
--
-- Applied to the DEV database only; production waits for the release.

create table if not exists llm_runtime_settings (
  id                 smallint primary key default 1 check (id = 1),
  self_hosted_target text,
  updated_by         uuid references profiles(id) on delete set null,
  updated_at         timestamptz not null default now()
);

comment on table llm_runtime_settings is
  'Single row. self_hosted_target overrides the SELF_HOSTED_LLM_TARGET env var live; null = use the env default.';

insert into llm_runtime_settings (id, self_hosted_target)
values (1, null)
on conflict (id) do nothing;

create table if not exists llm_target_changes (
  id           bigserial primary key,
  from_target  text not null,
  to_target    text not null,
  -- true when the switch went through even though the new box did not answer
  -- its health probe (the "Switch anyway" path).
  forced       boolean not null default false,
  probe_state  text,
  changed_by   uuid references profiles(id) on delete set null,
  changed_at   timestamptz not null default now()
);

comment on table llm_target_changes is
  'Audit trail of live self-hosted LLM box switches made from Settings.';

create index if not exists llm_target_changes_changed_at_idx
  on llm_target_changes (changed_at desc);

-- RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.
alter table llm_runtime_settings enable row level security;
alter table llm_runtime_settings force row level security;
alter table llm_target_changes enable row level security;
alter table llm_target_changes force row level security;

notify pgrst, 'reload schema';
