-- 0006 — System tables: notifications, email log, audit log.

create table notifications (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  project_id  uuid references projects(id) on delete cascade,
  type        text not null,
  message     text not null,
  read_at     timestamptz,
  created_at  timestamptz not null default now()
);
create index notifications_user_idx on notifications(user_id, read_at);

create table email_log (
  id               uuid primary key default gen_random_uuid(),
  to_addrs         text not null,           -- comma-separated recipients
  subject          text not null,
  body             text,
  graph_message_id text,
  status           email_status not null default 'queued',
  error            text,
  project_id       uuid references projects(id) on delete set null,
  rfq_id           uuid references rfqs(id) on delete set null,
  sent_by          uuid references profiles(id),
  created_at       timestamptz not null default now()
);
create index email_log_project_idx on email_log(project_id);

-- Now that email_log exists, wire rfq_recipients.email_log_id to it.
alter table rfq_recipients
  add constraint rfq_recipients_email_log_fk
  foreign key (email_log_id) references email_log(id) on delete set null;

create table audit_log (
  id          uuid primary key default gen_random_uuid(),
  actor_id    uuid references profiles(id),
  action      text not null,             -- e.g. 'file.download', 'login', 'access.denied'
  entity      text,                      -- e.g. 'project', 'project_file'
  entity_id   uuid,
  payload     jsonb,
  created_at  timestamptz not null default now()
);
create index audit_log_actor_idx on audit_log(actor_id, created_at);
create index audit_log_action_idx on audit_log(action, created_at);
