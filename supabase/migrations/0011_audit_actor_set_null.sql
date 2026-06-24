-- 0011 — Preserve the audit trail when a user is deleted.
-- audit_log.actor_id was RESTRICT (blocked user deletion); switch to SET NULL so
-- the audit history outlives any deleted account. (Users are normally
-- deactivated via profiles.is_active rather than deleted.)

alter table audit_log drop constraint audit_log_actor_id_fkey;
alter table audit_log
  add constraint audit_log_actor_id_fkey
  foreign key (actor_id) references profiles(id) on delete set null;
