-- DEV ONLY. Sequential interleavings and constraints; all fixtures roll back.
-- This does not replace simultaneous two-session contention tests.
begin;
do $$
declare u uuid:=gen_random_uuid();c uuid:=gen_random_uuid();rc uuid:=gen_random_uuid();m uuid:=gen_random_uuid();
 r uuid:=gen_random_uuid();a uuid;sid uuid;aid uuid;v jsonb;i int;mail text;f record;page_one jsonb;page_two jsonb;last_row jsonb;
begin
 mail:='phase5-'||u::text||'@example.invalid';
 insert into auth.users(id,email) values(u,mail);
 insert into public.profiles(id,full_name,email,role,is_active) values(u,'Phase 5 rollback fixture',mail,'it_admin',true)
 on conflict(id) do update set full_name=excluded.full_name,email=excluded.email,role=excluded.role,is_active=true;
 update public.assistant_settings set enabled=true where id=1;
 insert into public.assistant_delegations(user_id,created_session_aal) values(u,'aal2');
 insert into public.assistant_conversations(id,user_id,mode) values(c,u,'normal'),(rc,u,'mailbox_review');
 insert into public.assistant_messages(id,conversation_id,user_id,role,content) values(m,rc,u,'assistant','fixture');
 insert into public.assistant_tool_calls(user_id,user_label,conversation_id,message_id,run_id,seq,idempotency_key,phase,lease_owner,tool_name,effect)
 values(u,'fixture',rc,m,r,0,'fixture-'||u,'begun','fixture','read_mailbox','sensitive_read');
 for i in 1..21 loop
  a:=gen_random_uuid();sid:=gen_random_uuid();
  insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
  values(a,u,'fixture',c,'email_draft',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/mail/send','json',jsonb_build_object('sender',mail,'send_id',sid,'authority',jsonb_build_object('role','it_admin','source_tainted',false),'attachments','[]'::jsonb,'to',jsonb_build_array(jsonb_build_object('address',mail)),'cc','[]'::jsonb))),repeat('a',64),'approved',u,now(),now()+interval '1 hour');
  v:=public.assistant_mail_send_claim(u,a,r,mail,repeat('a',64),array['safe@example.invalid']);
  if i<=20 then
   assert v->>'claimed'='true','send claim failed';
   assert (v->>'send_id')::uuid=sid,'marker ID mismatch';
   v:=public.assistant_mail_send_claim(u,a,r,mail,repeat('a',64),'{}');
   assert v->>'claimed'='false','send replay claimed twice';
   v:=public.assistant_mail_send_finish(u,sid,'uncertain','transport_error');
   assert v->>'ok'='true','uncertain finish failed';
   v:=public.assistant_mail_send_finish(u,sid,'accepted',null);
   assert v->>'ok'='false','terminal send was overwritten';
  else assert v->>'code'='daily_cap','send cap missed'; end if;
 end loop;
 for i in 1..4 loop
  a:=gen_random_uuid();
  insert into public.assistant_approvals(id,user_id,user_label,conversation_id,message_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,decision_reason,expires_at)
  values(a,u,'fixture',rc,m,'read_mailbox',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/mail/review','json',jsonb_build_object('target_upn','person'||i||'@example.invalid'))),repeat('b',64),'approved',u,now(),'Review for rollback fixture',now()+interval '1 hour');
  v:=public.assistant_mailbox_access_claim(u,a,gen_random_uuid(),'person'||i||'@example.invalid',repeat('b',64));
  if i<=3 then
   assert v->>'claimed'='true','review claim failed';aid:=(v->>'access_id')::uuid;
   v:=public.assistant_mailbox_access_outcome(u,aid,'succeeded',array['message'],null);
   assert v->>'inserted'='true','outcome missing';
   v:=public.assistant_mailbox_access_outcome(u,aid,'failed','{}','late');
   assert v->>'inserted'='false','outcome overwritten';
  else assert v->>'code'='run_cap','random execution run bypassed target cap'; end if;
 end loop;
 for i in 4..11 loop
  a:=gen_random_uuid();
  insert into public.assistant_approvals(id,user_id,user_label,conversation_id,message_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,decision_reason,expires_at)
  values(a,u,'fixture',rc,m,'read_mailbox',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/mail/review','json',jsonb_build_object('target_upn','person1@example.invalid'))),repeat('b',64),'approved',u,now(),'Review for rollback fixture',now()+interval '1 hour');
  v:=public.assistant_mailbox_access_claim(u,a,gen_random_uuid(),'person1@example.invalid',repeat('b',64));
  if i<=10 then assert v->>'claimed'='true','same-target allowance failed';
  else assert v->>'code'='daily_cap','review daily cap missed'; end if;
 end loop;
 -- Post-preview taint and recipient/attachment authority fence regressions.
 update public.assistant_conversations set tainted_at=now() where id=c;
 a:=gen_random_uuid();
 insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
 values(a,u,'fixture',c,'email_draft',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/mail/send','json',jsonb_build_object('sender',mail,'send_id',gen_random_uuid(),'authority',jsonb_build_object('role','it_admin','source_tainted',false),'attachments','[]'::jsonb,'to',jsonb_build_array(jsonb_build_object('address',mail)),'cc','[]'::jsonb))),repeat('a',64),'approved',u,now(),now()+interval '1 hour');
 v:=public.assistant_mail_send_claim(u,a,r,mail,repeat('a',64),'{}');assert v->>'code'='authority_changed','new taint not fenced';
 a:=gen_random_uuid();
 insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
 values(a,u,'fixture',c,'email_draft',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/mail/send','json',jsonb_build_object('sender',mail,'send_id',gen_random_uuid(),'authority',jsonb_build_object('role','it_admin','source_tainted',true),'attachments','[]'::jsonb,'to',jsonb_build_array(jsonb_build_object('address','outside@example.invalid')),'cc','[]'::jsonb))),repeat('a',64),'approved',u,now(),now()+interval '1 hour');
 v:=public.assistant_mail_send_claim(u,a,r,mail,repeat('a',64),'{}');assert v->>'code'='recipient_changed','sensitive external recipient allowed';
 a:=gen_random_uuid();
 insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
 values(a,u,'fixture',c,'email_draft',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/mail/send','json',jsonb_build_object('sender',mail,'send_id',gen_random_uuid(),'authority',jsonb_build_object('role','it_admin','source_tainted',true),'attachments',jsonb_build_array(jsonb_build_object('id',gen_random_uuid(),'sha256',repeat('c',64),'size_bytes',500)),'to',jsonb_build_array(jsonb_build_object('address',mail)),'cc','[]'::jsonb))),repeat('a',64),'approved',u,now(),now()+interval '1 hour');
 v:=public.assistant_mail_send_claim(u,a,r,mail,repeat('a',64),'{}');assert v->>'code'='attachment_changed','missing attachment allowed';
 page_one:=public.assistant_mailbox_access_page(u,3,null,null)->'rows';
 last_row:=page_one->2;
 page_two:=public.assistant_mailbox_access_page(u,3,(last_row->>'created_at')::timestamptz,(last_row->>'id')::uuid)->'rows';
 assert jsonb_array_length(page_one)=3 and jsonb_array_length(page_two)=3,'tied timestamp page lost rows';
 assert not exists(select 1 from jsonb_array_elements(page_one)x join jsonb_array_elements(page_two)y on x->>'id'=y->>'id'),'page repeated rows';
 begin update public.assistant_mailbox_access set reason='Changed reason fixture' where user_id=u;raise exception 'immutable update succeeded';
 exception when restrict_violation then null;end;
 v:=public.assistant_calendar_preferences_set(u,true,'all',15);assert v->>'ok'='true';
 v:=public.assistant_calendar_reminder_claim(u,'fixture-event',now()+interval '5 minutes',15,'all',mail);assert v->>'claimed'='true';
 v:=public.assistant_calendar_reminder_claim(u,'fixture-event',now()+interval '5 minutes',15,'all',mail);assert v->>'claimed'='false';
 assert (select count(*) from public.notifications where user_id=u and type='assistant.calendar')=1,'calendar duplicate/lost bell';
 update public.assistant_delegations set revoked_at=now(),revoked_reason='test' where user_id=u;
 v:=public.assistant_calendar_preferences_set(u,false,'all',15);assert v->>'ok'='true','revoked user cannot disable';
 for f in select oid from pg_proc where pronamespace='public'::regnamespace and proname like 'assistant_mail%' and proname not in ('assistant_mailbox_access_list') loop
  assert not has_function_privilege('anon',f.oid,'EXECUTE'),'anon RPC grant';
  assert not has_function_privilege('authenticated',f.oid,'EXECUTE'),'authenticated RPC grant';
  assert has_function_privilege('service_role',f.oid,'EXECUTE'),'missing service RPC grant';
 end loop;
end $$;
rollback;
