-- Phase 5 application mail. No Microsoft permissions or tenant changes.
create table if not exists public.assistant_mail_sends (
 id uuid primary key default gen_random_uuid(), user_id uuid not null, user_label text not null,
 approval_id uuid not null unique, run_id uuid not null, conversation_id uuid,
 sender text not null, payload_sha256 text not null, effective_recipients text[] not null default '{}',
 status text not null default 'claimed' check(status in ('claimed','accepted','uncertain','refused')),
 error_code text, created_at timestamptz not null default now(), finished_at timestamptz
);
create index if not exists assistant_mail_sends_user_day on public.assistant_mail_sends(user_id,created_at);
create table if not exists public.assistant_mailbox_access (
 id uuid primary key default gen_random_uuid(), user_id uuid not null, reader_label text not null,
 approval_id uuid not null unique, run_id uuid not null, conversation_id uuid not null,
 target_upn text not null, reason text not null check(length(btrim(reason)) between 10 and 500),
 payload_sha256 text not null, created_at timestamptz not null default now()
);
create index if not exists assistant_mailbox_access_user_day on public.assistant_mailbox_access(user_id,created_at);
create index if not exists assistant_mailbox_access_run_target on public.assistant_mailbox_access(user_id,run_id,target_upn);
create table if not exists public.assistant_mailbox_outcomes (
 id uuid primary key default gen_random_uuid(),user_id uuid not null,access_id uuid not null unique,
 status text not null check(status in ('succeeded','failed','uncertain')),message_ids text[] not null default '{}',
 error_code text,created_at timestamptz not null default now()
);
create table if not exists public.assistant_calendar_preferences (
 user_id uuid primary key references public.profiles(id) on delete cascade,
 enabled boolean not null default false, mode text not null default 'context' check(mode in ('context','all')),
 lead_minutes int not null default 15 check(lead_minutes between 5 and 180),updated_at timestamptz not null default now()
);
create table if not exists public.assistant_calendar_reminders (
 id uuid primary key default gen_random_uuid(),user_id uuid not null references public.profiles(id) on delete cascade,
 event_key text not null check(length(event_key) between 1 and 512),starts_at timestamptz not null,
 lead_minutes int not null check(lead_minutes between 5 and 180),created_at timestamptz not null default now(),
 unique(user_id,event_key,starts_at,lead_minutes)
);
create table if not exists public.assistant_mail_notices (
 user_id uuid not null, notice_key text not null check(length(notice_key) between 1 and 512),
 created_at timestamptz not null default now(),primary key(user_id,notice_key)
);
alter table public.assistant_mail_sends enable row level security;
alter table public.assistant_mail_sends force row level security;
revoke all on public.assistant_mail_sends from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_mail_sends to service_role;
alter table public.assistant_mailbox_access enable row level security;
alter table public.assistant_mailbox_access force row level security;
revoke all on public.assistant_mailbox_access from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_mailbox_access to service_role;
alter table public.assistant_mailbox_outcomes enable row level security;
alter table public.assistant_mailbox_outcomes force row level security;
revoke all on public.assistant_mailbox_outcomes from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_mailbox_outcomes to service_role;
alter table public.assistant_calendar_preferences enable row level security;
alter table public.assistant_calendar_preferences force row level security;
revoke all on public.assistant_calendar_preferences from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_calendar_preferences to service_role;
alter table public.assistant_calendar_reminders enable row level security;
alter table public.assistant_calendar_reminders force row level security;
revoke all on public.assistant_calendar_reminders from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_calendar_reminders to service_role;
alter table public.assistant_mail_notices enable row level security;
alter table public.assistant_mail_notices force row level security;
revoke all on public.assistant_mail_notices from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_mail_notices to service_role;
do $$ begin execute 'drop trigger if exists assistant_mailbox_access_immutable on public.assistant_mailbox_access'; end $$;
create trigger assistant_mailbox_access_immutable before update or delete on public.assistant_mailbox_access for each row execute function public.assistant_refuse_change();
do $$ begin execute 'drop trigger if exists assistant_mailbox_outcomes_immutable on public.assistant_mailbox_outcomes'; end $$;
create trigger assistant_mailbox_outcomes_immutable before update or delete on public.assistant_mailbox_outcomes for each row execute function public.assistant_refuse_change();

create or replace function public.assistant_mail_eligible(p_user_id uuid) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare 
begin
 perform 1 from public.profiles where id=p_user_id and is_active=true and role::text in
 ('estimating_admin','executive','it_admin','accountant','estimating_engineer_materials','estimating_engineer_labor') for share;
 if not found then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform 1 from public.assistant_settings where id=1 and enabled=true for share;
 if not found then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform 1 from public.assistant_profiles where user_id=p_user_id for share;
 if exists(select 1 from public.assistant_profiles where user_id=p_user_id and enabled=false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform 1 from public.assistant_delegations where user_id=p_user_id and revoked_at is null and (expires_at is null or expires_at>now()) for share;
 return jsonb_build_object('ok',found);
end $$;

create or replace function public.assistant_mail_send_claim(p_user_id uuid,p_approval_id uuid,p_run_id uuid,p_sender text,p_payload_sha256 text,p_effective_recipients text[]) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare a public.assistant_approvals%rowtype; r public.assistant_mail_sends%rowtype;
begin
if not coalesce((public.assistant_mail_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
perform pg_advisory_xact_lock(hashtextextended('assistant-mail:'||p_user_id::text,0));
select * into r from public.assistant_mail_sends where user_id=p_user_id and approval_id=p_approval_id;
if found then return jsonb_build_object('ok',true,'claimed',false,'send_id',r.id,'status',r.status); end if;
select * into a from public.assistant_approvals where id=p_approval_id and user_id=p_user_id for update;
 if a.id is null or a.status not in ('approved','executing') or a.expires_at<=now() or a.payload_sha256 is distinct from p_payload_sha256
 or a.decided_by is distinct from p_user_id then return '{"ok":false,"code":"approval_invalid"}'::jsonb; end if;
if exists(select 1 from public.assistant_settings where id=1 and 'email_draft'=any(disabled_tools)) then return '{"ok":false,"code":"tool_disabled"}'::jsonb; end if;
if a.action_kind<>'email_draft' or a.payload#>>'{request,method}' is distinct from 'POST'
 or not exists(select 1 from public.assistant_conversations where user_id=p_user_id and id=a.conversation_id and mode='normal' and mailbox_tainted_at is null)
 or a.payload#>>'{request,json,sender}' is distinct from p_sender
 or a.payload#>>'{request,path}' is distinct from '/assistant/mail/send'
 or not exists(select 1 from public.profiles where id=p_user_id and lower(btrim(email))=p_sender)
 then return '{"ok":false,"code":"sender_changed"}'::jsonb; end if;
if (select count(*) from public.assistant_mail_sends where user_id=p_user_id and (created_at at time zone 'America/Los_Angeles')::date=(now() at time zone 'America/Los_Angeles')::date)>=20 then return '{"ok":false,"code":"daily_cap"}'::jsonb; end if;
insert into public.assistant_mail_sends(id,user_id,user_label,approval_id,run_id,conversation_id,sender,payload_sha256,effective_recipients)
values(coalesce(nullif(a.payload#>>'{request,json,send_id}','')::uuid,gen_random_uuid()),p_user_id,a.user_label,p_approval_id,p_run_id,a.conversation_id,p_sender,p_payload_sha256,coalesce(p_effective_recipients,'{}')) returning * into r;
return jsonb_build_object('ok',true,'claimed',true,'send_id',r.id,'status',r.status);
end $$;

create or replace function public.assistant_mail_send_finish(p_user_id uuid,p_send_id uuid,p_status text,p_error_code text) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare 
begin
if p_status not in ('accepted','uncertain','refused') then return '{"ok":false,"code":"invalid_status"}'::jsonb; end if;
update public.assistant_mail_sends set status=p_status,error_code=left(p_error_code,80),finished_at=now() where user_id=p_user_id and id=p_send_id and status='claimed';
return jsonb_build_object('ok',found);
end $$;

create or replace function public.assistant_mailbox_access_claim(p_user_id uuid,p_approval_id uuid,p_run_id uuid,p_target_upn text,p_payload_sha256 text) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare a public.assistant_approvals%rowtype;r public.assistant_mailbox_access%rowtype; v_reviewer record;
begin
if not coalesce((public.assistant_mail_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
perform pg_advisory_xact_lock(hashtextextended('assistant-mail:'||p_user_id::text,0));
if not exists(select 1 from public.profiles where id=p_user_id and role::text in ('executive','it_admin')) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
select * into r from public.assistant_mailbox_access where user_id=p_user_id and approval_id=p_approval_id;
if found then return jsonb_build_object('ok',true,'claimed',false,'access_id',r.id); end if;
select * into a from public.assistant_approvals where id=p_approval_id and user_id=p_user_id for update;
 if a.id is null or a.status not in ('approved','executing') or a.expires_at<=now() or a.payload_sha256 is distinct from p_payload_sha256
 or a.decided_by is distinct from p_user_id then return '{"ok":false,"code":"approval_invalid"}'::jsonb; end if;
select run_id into p_run_id from public.assistant_tool_calls
 where user_id=p_user_id and message_id=a.message_id and phase='begun'
 order by created_at,id limit 1;
if p_run_id is null then return '{"ok":false,"code":"run_missing"}'::jsonb; end if;
if exists(select 1 from public.assistant_settings where id=1 and 'read_mailbox'=any(disabled_tools)) then return '{"ok":false,"code":"tool_disabled"}'::jsonb; end if;
if a.action_kind<>'read_mailbox' or a.payload#>>'{request,method}' is distinct from 'POST' or a.payload#>>'{request,json,target_upn}' is distinct from p_target_upn
 or a.payload#>>'{request,path}' is distinct from '/assistant/mail/review'
 or length(btrim(coalesce(a.decision_reason,''))) not between 10 and 500
 or not exists(select 1 from public.assistant_conversations where id=a.conversation_id and user_id=p_user_id and mode='mailbox_review')
 then return '{"ok":false,"code":"review_invalid"}'::jsonb; end if;
if (select count(*) from public.assistant_mailbox_access where user_id=p_user_id and (created_at at time zone 'America/Los_Angeles')::date=(now() at time zone 'America/Los_Angeles')::date)>=10 then return '{"ok":false,"code":"daily_cap"}'::jsonb; end if;
if not exists(select 1 from public.assistant_mailbox_access where user_id=p_user_id and run_id=p_run_id and target_upn=p_target_upn)
 and (select count(distinct target_upn) from public.assistant_mailbox_access where user_id=p_user_id and run_id=p_run_id)>=3 then return '{"ok":false,"code":"run_cap"}'::jsonb; end if;
insert into public.assistant_mailbox_access(user_id,reader_label,approval_id,run_id,conversation_id,target_upn,reason,payload_sha256)
values(p_user_id,a.user_label,p_approval_id,p_run_id,a.conversation_id,p_target_upn,a.decision_reason,p_payload_sha256) returning * into r;
for v_reviewer in select id from public.profiles where is_active=true and role::text in ('executive','it_admin') loop
 perform public.assistant_mail_notice_deliver(v_reviewer.id,
 'mailbox-first:'||to_char(date_trunc('week',now() at time zone 'America/Los_Angeles'),'YYYY-MM-DD')||':'||encode(sha256(convert_to(p_target_upn,'UTF8')),'hex'),
 'A mailbox was opened for assistant review.','First review of this mailbox this Pacific week.',jsonb_build_object('target_upn',p_target_upn,'reader_user_id',p_user_id));
end loop;
return jsonb_build_object('ok',true,'claimed',true,'access_id',r.id);
end $$;

create or replace function public.assistant_mailbox_access_outcome(p_user_id uuid,p_access_id uuid,p_status text,p_message_ids text[],p_error_code text) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare 
begin
if not exists(select 1 from public.assistant_mailbox_access where user_id=p_user_id and id=p_access_id) then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
if cardinality(p_message_ids)>50 or p_status not in ('succeeded','failed','uncertain') then return '{"ok":false,"code":"invalid_outcome"}'::jsonb; end if;
insert into public.assistant_mailbox_outcomes(user_id,access_id,status,message_ids,error_code) values(p_user_id,p_access_id,p_status,coalesce(p_message_ids,'{}'),left(p_error_code,80)) on conflict(access_id) do nothing;
return jsonb_build_object('ok',true,'inserted',found);
end $$;

create or replace function public.assistant_mailbox_access_list(p_user_id uuid,p_limit int,p_before timestamptz) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare rows jsonb;
begin
if not exists(select 1 from public.profiles where id=p_user_id and is_active=true and role::text='it_admin') then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into rows from (select * from public.assistant_mailbox_access where p_before is null or created_at<p_before order by created_at desc,id desc limit greatest(1,least(200,p_limit))) x;
return jsonb_build_object('ok',true,'rows',rows);
end $$;

create or replace function public.assistant_evidence_access_list(p_user_id uuid,p_limit int,p_before timestamptz) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare rows jsonb;
begin
if not exists(select 1 from public.profiles where id=p_user_id and is_active=true and role::text='it_admin') then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into rows from (select * from public.assistant_evidence_access where p_before is null or created_at<p_before order by created_at desc,id desc limit greatest(1,least(200,p_limit))) x;
return jsonb_build_object('ok',true,'rows',rows);
end $$;

create or replace function public.assistant_calendar_preferences_set(p_user_id uuid,p_enabled boolean,p_mode text,p_lead_minutes int) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare 
begin
if p_enabled then
 if not coalesce((public.assistant_mail_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
else
 if not exists(select 1 from public.profiles where id=p_user_id and is_active=true and role::text in ('estimating_admin','executive','it_admin','accountant','estimating_engineer_materials','estimating_engineer_labor')) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
end if;
insert into public.assistant_calendar_preferences(user_id,enabled,mode,lead_minutes) values(p_user_id,p_enabled,p_mode,p_lead_minutes)
on conflict(user_id) do update set enabled=excluded.enabled,mode=excluded.mode,lead_minutes=excluded.lead_minutes,updated_at=now();
return jsonb_build_object('ok',true,'enabled',p_enabled,'mode',p_mode,'lead_minutes',p_lead_minutes);
end $$;

create or replace function public.assistant_calendar_reminder_claim(p_user_id uuid,p_event_key text,p_starts_at timestamptz,p_lead_minutes int,p_expected_mode text,p_expected_email text) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare 
begin
if not coalesce((public.assistant_mail_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
perform 1 from public.assistant_calendar_preferences where user_id=p_user_id and enabled=true and lead_minutes=p_lead_minutes and mode=p_expected_mode for share;
if not found or p_starts_at<now() or p_starts_at>now()+make_interval(mins=>p_lead_minutes)
 or not exists(select 1 from public.profiles where id=p_user_id and lower(btrim(email))=p_expected_email) then return '{"ok":false,"code":"disabled"}'::jsonb; end if;
insert into public.assistant_calendar_reminders(user_id,event_key,starts_at,lead_minutes) values(p_user_id,p_event_key,p_starts_at,p_lead_minutes) on conflict do nothing;
if not found then return '{"ok":true,"claimed":false}'::jsonb; end if;
insert into public.assistant_inbox(user_id,dedupe_key,title,why,payload,delivered_at)
values(p_user_id,'calendar:'||p_event_key||':'||p_starts_at::text||':'||p_lead_minutes::text,
 'Calendar event starts soon.','Your calendar reminder preference.',jsonb_build_object('starts_at',p_starts_at),now()) on conflict(user_id,dedupe_key) do nothing;
insert into public.notifications(user_id,type,message) values(p_user_id,'assistant.calendar','Calendar event starts soon.');
return '{"ok":true,"claimed":true}'::jsonb;
end $$;

create or replace function public.assistant_mail_notice_claim(p_user_id uuid,p_notice_key text) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare 
begin
insert into public.assistant_mail_notices(user_id,notice_key) values(p_user_id,p_notice_key) on conflict do nothing;
return jsonb_build_object('ok',true,'claimed',found);
end $$;

revoke all on function public.assistant_mail_eligible(uuid) from public,anon,authenticated;
grant execute on function public.assistant_mail_eligible(uuid) to service_role;

revoke all on function public.assistant_mail_send_claim(uuid,uuid,uuid,text,text,text[]) from public,anon,authenticated;
grant execute on function public.assistant_mail_send_claim(uuid,uuid,uuid,text,text,text[]) to service_role;

revoke all on function public.assistant_mail_send_finish(uuid,uuid,text,text) from public,anon,authenticated;
grant execute on function public.assistant_mail_send_finish(uuid,uuid,text,text) to service_role;

revoke all on function public.assistant_mailbox_access_claim(uuid,uuid,uuid,text,text) from public,anon,authenticated;
grant execute on function public.assistant_mailbox_access_claim(uuid,uuid,uuid,text,text) to service_role;

revoke all on function public.assistant_mailbox_access_outcome(uuid,uuid,text,text[],text) from public,anon,authenticated;
grant execute on function public.assistant_mailbox_access_outcome(uuid,uuid,text,text[],text) to service_role;

revoke all on function public.assistant_mailbox_access_list(uuid,int,timestamptz) from public,anon,authenticated;
grant execute on function public.assistant_mailbox_access_list(uuid,int,timestamptz) to service_role;

revoke all on function public.assistant_evidence_access_list(uuid,int,timestamptz) from public,anon,authenticated;
grant execute on function public.assistant_evidence_access_list(uuid,int,timestamptz) to service_role;

revoke all on function public.assistant_calendar_preferences_set(uuid,boolean,text,int) from public,anon,authenticated;
grant execute on function public.assistant_calendar_preferences_set(uuid,boolean,text,int) to service_role;

revoke all on function public.assistant_calendar_reminder_claim(uuid,text,timestamptz,int,text,text) from public,anon,authenticated;
grant execute on function public.assistant_calendar_reminder_claim(uuid,text,timestamptz,int,text,text) to service_role;

revoke all on function public.assistant_mail_notice_claim(uuid,text) from public,anon,authenticated;
grant execute on function public.assistant_mail_notice_claim(uuid,text) to service_role;

alter table public.email_log add column if not exists approval_id uuid;
alter table public.email_log add column if not exists assistant_send_id uuid;
do $$ declare c record; begin
 for c in select conname from pg_constraint where conrelid='public.assistant_tool_results'::regclass and contype='c' and pg_get_constraintdef(oid) like '%retention_class%'
 loop execute format('alter table public.assistant_tool_results drop constraint %I',c.conname); end loop;
 alter table public.assistant_tool_results add constraint assistant_tool_results_retention_class_check check(retention_class in ('standard','mail','cross_mailbox','payroll_aggregate'));
end $$;
notify pgrst,'reload schema';

do $$ begin execute 'drop trigger if exists assistant_mailbox_access_no_truncate on public.assistant_mailbox_access'; end $$;
create trigger assistant_mailbox_access_no_truncate before truncate on public.assistant_mailbox_access for each statement execute function public.assistant_refuse_change();

do $$ begin execute 'drop trigger if exists assistant_mailbox_outcomes_no_truncate on public.assistant_mailbox_outcomes'; end $$;
create trigger assistant_mailbox_outcomes_no_truncate before truncate on public.assistant_mailbox_outcomes for each statement execute function public.assistant_refuse_change();

-- IT Admin inspection records its reason before returning any other-owner data.
create or replace function public.assistant_admin_conversation_read(
 p_user_id uuid,p_owner_id uuid,p_conversation_id uuid,p_reason_kind text,p_reason text,p_action text
) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare v_label text;v_messages jsonb;v_results jsonb;v_conversations jsonb;
begin
 select coalesce(full_name,email,id::text) into v_label from public.profiles where id=p_user_id and is_active=true and role::text='it_admin';
 if not found then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 if p_reason_kind not in ('support','legal','compliance','offboarding','other') or p_action not in ('read','export')
 or length(btrim(coalesce(p_reason,''))) not between 10 and 500 then return '{"ok":false,"code":"reason_required"}'::jsonb; end if;
 if p_conversation_id is not null and not exists(select 1 from public.assistant_conversations where user_id=p_owner_id and id=p_conversation_id) then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 insert into public.assistant_conversation_views(viewer_id,viewer_label,owner_id,conversation_id,action,reason_kind,reason)
 values(p_user_id,v_label,p_owner_id,p_conversation_id,p_action,p_reason_kind,btrim(p_reason));
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into v_conversations from
 (select id,title,created_at,mode,mailbox_tainted_at from public.assistant_conversations where user_id=p_owner_id
 and (p_conversation_id is null or id=p_conversation_id) and (p_reason_kind='legal' or mailbox_tainted_at is null)
 order by created_at,id limit 1000) x;
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into v_messages from
 (select m.id,m.conversation_id,m.role,m.content,m.blocks,m.created_at from public.assistant_messages m
 join public.assistant_conversations c on c.id=m.conversation_id and c.user_id=m.user_id
 where m.user_id=p_owner_id and (p_conversation_id is null or m.conversation_id=p_conversation_id)
 and (p_reason_kind='legal' or (not m.mailbox_tainted and c.mailbox_tainted_at is null))
 order by m.created_at,m.id limit 5000) x;
 v_results:='[]'::jsonb;
 if p_reason_kind='legal' then
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into v_results from
 (select id,conversation_id,tool_name,payload,created_at from public.assistant_tool_results where user_id=p_owner_id
 and (p_conversation_id is null or conversation_id=p_conversation_id) order by created_at,id limit 1000) x;
 end if;
 return jsonb_build_object('ok',true,'conversations',v_conversations,'messages',v_messages,'tool_results',v_results,'bounded',true);
end $$;
revoke all on function public.assistant_admin_conversation_read(uuid,uuid,uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.assistant_admin_conversation_read(uuid,uuid,uuid,text,text,text) to service_role;

create or replace function public.assistant_mail_notice_deliver(p_user_id uuid,p_notice_key text,p_title text,p_why text,p_payload jsonb)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
begin
 if not exists(select 1 from public.profiles where id=p_user_id and is_active=true and role::text in ('executive','it_admin')) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 insert into public.assistant_mail_notices(user_id,notice_key) values(p_user_id,p_notice_key) on conflict do nothing;
 if not found then return '{"ok":true,"delivered":false}'::jsonb; end if;
 insert into public.assistant_inbox(user_id,dedupe_key,title,why,payload,delivered_at)
 values(p_user_id,p_notice_key,left(p_title,200),left(p_why,1000),p_payload,now()) on conflict(user_id,dedupe_key) do nothing;
 insert into public.notifications(user_id,type,message) values(p_user_id,'assistant.mailbox_access',left(p_title,200));
 return '{"ok":true,"delivered":true}'::jsonb;
end $$;
revoke all on function public.assistant_mail_notice_deliver(uuid,text,text,text,jsonb) from public,anon,authenticated;
grant execute on function public.assistant_mail_notice_deliver(uuid,text,text,text,jsonb) to service_role;

create or replace function public.assistant_mail_audit_summary(p_user_id uuid,p_start timestamptz,p_end timestamptz)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare v_total bigint;v_targets bigint;v_readers bigint;
begin
 if not exists(select 1 from public.profiles where id=p_user_id and is_active=true and role::text in ('executive','it_admin')) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 if p_start is null or p_end is null or p_end<=p_start or p_end-p_start>interval '31 days' then return '{"ok":false,"code":"window_invalid"}'::jsonb; end if;
 select count(*),count(distinct target_upn),count(distinct user_id) into v_total,v_targets,v_readers from public.assistant_mailbox_access where created_at>=p_start and created_at<p_end;
 return jsonb_build_object('ok',true,'accesses',v_total,'targets',v_targets,'readers',v_readers);
end $$;
revoke all on function public.assistant_mail_audit_summary(uuid,timestamptz,timestamptz) from public,anon,authenticated;
grant execute on function public.assistant_mail_audit_summary(uuid,timestamptz,timestamptz) to service_role;

-- Preserve historical reason kinds while adding the explicitly requested compliance purpose.
do $$ declare c record; begin
 for c in select conname from pg_constraint where conrelid='public.assistant_conversation_views'::regclass and contype='c' and pg_get_constraintdef(oid) like '%reason_kind%'
 loop execute format('alter table public.assistant_conversation_views drop constraint %I',c.conname); end loop;
 alter table public.assistant_conversation_views add constraint assistant_conversation_views_reason_kind_check
 check(reason_kind in ('support','legal','compliance','offboarding','other'));
end $$;

create table if not exists public.assistant_mail_configuration (
 id smallint primary key check(id=1),exclusions text[] not null default '{}',version bigint not null default 0,
 updated_at timestamptz not null default now()
);
alter table public.assistant_mail_configuration enable row level security;
alter table public.assistant_mail_configuration force row level security;
revoke all on public.assistant_mail_configuration from public,anon,authenticated;
grant select,insert,update on public.assistant_mail_configuration to service_role;
create or replace function public.assistant_mail_exclusions_sync(p_exclusions text[])
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare old_values text[];new_values text[];v_version bigint;v_user record;
begin
 select coalesce(array_agg(distinct lower(btrim(x)) order by lower(btrim(x))),'{}') into new_values from unnest(coalesce(p_exclusions,'{}')) x where btrim(x)<>'';
 insert into public.assistant_mail_configuration(id) values(1) on conflict do nothing;
 select exclusions,version into old_values,v_version from public.assistant_mail_configuration where id=1 for update;
 if old_values=new_values then return '{"ok":true,"changed":false}'::jsonb; end if;
 update public.assistant_mail_configuration set exclusions=new_values,version=version+1,updated_at=now() where id=1 returning version into v_version;
 insert into public.audit_log(action,entity,payload) values('mailbox_exclusions.changed','assistant',jsonb_build_object('before',old_values,'after',new_values));
 for v_user in select id from public.profiles where is_active=true and role::text in ('executive','it_admin') loop
  perform public.assistant_mail_notice_deliver(v_user.id,'mail-exclusions:'||v_version::text,'Mailbox review exclusions changed.','The effective mailbox exclusion configuration changed.',jsonb_build_object('version',v_version));
 end loop;
 return jsonb_build_object('ok',true,'changed',true,'version',v_version);
end $$;
revoke all on function public.assistant_mail_exclusions_sync(text[]) from public,anon,authenticated;
grant execute on function public.assistant_mail_exclusions_sync(text[]) to service_role;
