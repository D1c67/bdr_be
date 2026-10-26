-- 0181 - Oversight roles receive one initial RFQ announcement per project, never
-- one email per vendor, resend or reply. Claim and bell insert are atomic;
-- concurrent API workers cannot each announce the same project.
create table if not exists rfq_initial_notifications (
  project_id uuid primary key references projects(id) on delete cascade,
  created_at timestamptz not null default now()
);
alter table rfq_initial_notifications enable row level security;
alter table rfq_initial_notifications force row level security;
revoke all on table rfq_initial_notifications from public, anon, authenticated;
grant all on table rfq_initial_notifications to service_role;

-- Existing sends predate this announcement. Do not announce their next
-- resend as an initial send; failed-only projects remain eligible.
insert into rfq_initial_notifications (project_id)
select distinct r.project_id
from rfqs r join rfq_sends s on s.rfq_id = r.id
where s.status = 'sent'
on conflict (project_id) do nothing;

create or replace function notify_initial_rfq_send(p_project_id uuid, p_message text)
returns setof notifications
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into rfq_initial_notifications (project_id)
  select p_project_id
  where exists (
    select 1 from rfqs r join rfq_sends s on s.rfq_id = r.id
    where r.project_id = p_project_id and s.status = 'sent'
  )
  on conflict (project_id) do nothing;

  if not found then
    return;
  end if;

  return query
  insert into notifications (user_id, project_id, type, message)
  select p.id, p_project_id, 'rfq.initial_send', p_message
  from profiles p
  where p.is_active and p.role::text in ('estimating_admin', 'executive', 'it_admin')
  returning *;
end;
$$;

revoke all on function notify_initial_rfq_send(uuid, text) from public;
revoke all on function notify_initial_rfq_send(uuid, text) from anon, authenticated;
grant execute on function notify_initial_rfq_send(uuid, text) to service_role;
notify pgrst, 'reload schema';
