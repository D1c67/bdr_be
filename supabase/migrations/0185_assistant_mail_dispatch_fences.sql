-- Forward hardening after0184; preserve applied migration history.
create or replace function public.assistant_mail_send_claim(p_user_id uuid,p_approval_id uuid,p_run_id uuid,p_sender text,p_payload_sha256 text,p_effective_recipients text[]) returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare a public.assistant_approvals%rowtype; r public.assistant_mail_sends%rowtype;v_role text;v_tainted boolean;v_sensitive boolean;v_address text;v_recipient jsonb;v_file jsonb;v_conversation public.assistant_conversations%rowtype;
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
-- Freeze all mutable authority before the dispatch claim is inserted.
select role::text into v_role from public.profiles where id=p_user_id for share;
if a.payload#>>'{request,json,authority,role}' is distinct from v_role then return '{"ok":false,"code":"authority_changed"}'::jsonb; end if;
select * into v_conversation from public.assistant_conversations where user_id=p_user_id and id=a.conversation_id for share;
if v_conversation.id is null or v_conversation.mode<>'normal' or v_conversation.mailbox_tainted_at is not null then return '{"ok":false,"code":"authority_changed"}'::jsonb; end if;
v_tainted:=v_conversation.tainted_at is not null or exists(select 1 from public.assistant_messages where user_id=p_user_id and id=a.message_id and (tainted or mailbox_tainted))
 or exists(select 1 from public.assistant_tool_calls where user_id=p_user_id and message_id=a.message_id and tainted_context);
if v_tainted and coalesce(a.payload#>>'{request,json,authority,source_tainted}','false')<>'true' then return '{"ok":false,"code":"authority_changed"}'::jsonb; end if;
if jsonb_typeof(a.payload#>'{request,json,attachments}') is distinct from 'array'
 or jsonb_typeof(a.payload#>'{request,json,to}') is distinct from 'array'
 or jsonb_typeof(a.payload#>'{request,json,cc}') is distinct from 'array' then return '{"ok":false,"code":"payload_invalid"}'::jsonb; end if;
if jsonb_array_length(a.payload#>'{request,json,attachments}')>10 then return '{"ok":false,"code":"payload_invalid"}'::jsonb; end if;
v_sensitive:=v_tainted or coalesce(a.payload#>>'{request,json,authority,source_tainted}','false')='true'
 or jsonb_array_length(a.payload#>'{request,json,attachments}')>0;
if v_sensitive then
 for v_recipient in select value from jsonb_array_elements((a.payload#>'{request,json,to}')||(a.payload#>'{request,json,cc}')) loop
  v_address:=lower(btrim(v_recipient->>'address'));
  perform 1 from public.profiles where lower(btrim(email))=v_address and is_active=true and role::text in ('estimating_admin','executive','it_admin','accountant') for share;
  if not found then return '{"ok":false,"code":"recipient_changed"}'::jsonb; end if;
 end loop;
end if;
for v_file in select value from jsonb_array_elements(a.payload#>'{request,json,attachments}') loop
 perform 1 from public.assistant_files where user_id=p_user_id and id=(v_file->>'id')::uuid
 and sha256=v_file->>'sha256' and size_bytes=(v_file->>'size_bytes')::bigint
 and expires_at>now() and consumed_at is null and purged_at is null and mime='application/pdf' for share;
 if not found then return '{"ok":false,"code":"attachment_changed"}'::jsonb; end if;
end loop;
if (select count(*) from public.assistant_mail_sends where user_id=p_user_id and (created_at at time zone 'America/Los_Angeles')::date=(now() at time zone 'America/Los_Angeles')::date)>=20 then return '{"ok":false,"code":"daily_cap"}'::jsonb; end if;
insert into public.assistant_mail_sends(id,user_id,user_label,approval_id,run_id,conversation_id,sender,payload_sha256,effective_recipients)
values(coalesce(nullif(a.payload#>>'{request,json,send_id}','')::uuid,gen_random_uuid()),p_user_id,a.user_label,p_approval_id,p_run_id,a.conversation_id,p_sender,p_payload_sha256,coalesce(p_effective_recipients,'{}')) returning * into r;
return jsonb_build_object('ok',true,'claimed',true,'send_id',r.id,'status',r.status);
end $$;

create or replace function public.assistant_mailbox_access_page(p_user_id uuid,p_limit int,p_before timestamptz,p_before_id uuid)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare rows jsonb;
begin
 if not exists(select 1 from public.profiles where id=p_user_id and is_active=true and role::text='it_admin') then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into rows from
 (select * from public.assistant_mailbox_access where p_before is null
 or (p_before_id is null and created_at<p_before)
 or (p_before_id is not null and (created_at,id)<(p_before,p_before_id))
 order by created_at desc,id desc limit greatest(1,least(200,p_limit))) x;
 return jsonb_build_object('ok',true,'rows',rows);
end $$;
revoke all on function public.assistant_mailbox_access_page(uuid,int,timestamptz,uuid) from public,anon,authenticated;
grant execute on function public.assistant_mailbox_access_page(uuid,int,timestamptz,uuid) to service_role;
revoke execute on function public.assistant_mailbox_access_list(uuid,int,timestamptz) from service_role;

create or replace function public.assistant_evidence_access_page(p_user_id uuid,p_limit int,p_before timestamptz,p_before_id uuid)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare rows jsonb;
begin
 if not exists(select 1 from public.profiles where id=p_user_id and is_active=true and role::text='it_admin') then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into rows from
 (select * from public.assistant_evidence_access where p_before is null
 or (p_before_id is null and created_at<p_before)
 or (p_before_id is not null and (created_at,id)<(p_before,p_before_id))
 order by created_at desc,id desc limit greatest(1,least(200,p_limit))) x;
 return jsonb_build_object('ok',true,'rows',rows);
end $$;
revoke all on function public.assistant_evidence_access_page(uuid,int,timestamptz,uuid) from public,anon,authenticated;
grant execute on function public.assistant_evidence_access_page(uuid,int,timestamptz,uuid) to service_role;
revoke execute on function public.assistant_evidence_access_list(uuid,int,timestamptz) from service_role;
notify pgrst,'reload schema';
