-- 0026 — Personal to-dos. Each internal user keeps their own list; the To-Dos
-- page lets any internal teammate open another teammate's list read-only.
-- Writes are owner-only, enforced in the API (routers/todos.py). The external
-- estimator is excluded entirely (require_internal), so nothing here is ever
-- estimator-visible.

create table todos (
  id           uuid primary key default gen_random_uuid(),
  -- The owner. Personal scratch data — deleted with the account (not the
  -- actor set-null convention of 0012; an ownerless to-do means nothing).
  user_id      uuid not null references profiles(id) on delete cascade,
  title        text not null,
  due_date     date,
  is_done      boolean not null default false,
  completed_at timestamptz,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create trigger todos_updated_at before update on todos
  for each row execute function set_updated_at();
create index todos_user_idx on todos(user_id, created_at);

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
alter table todos enable row level security;
alter table todos force row level security;
