-- 0036 — Estimator turnaround timestamps + analytics indexes.
--
-- Analytics needs to measure how long an estimate took: from when we emailed the
-- drawings (the estimator "received" it) to when they handed deliverables back.
-- Neither moment was persisted — send-to-estimator only emailed, and submit only
-- advanced/notified. We capture both on the assignment row (the right grain: one
-- estimator hand-off, which already carries due_at as the on-time benchmark).
-- Populated going forward only; historical assignments stay null.
alter table estimator_assignments
  add column sent_to_estimator_at timestamptz,  -- first send-to-estimator email
  add column returned_at          timestamptz;  -- estimator submitted deliverables

-- Cohort selection scans stage_events for 'submitted' transitions inside a date
-- window; the existing (to_stage) index lacks entered_at to order/range on.
create index stage_events_to_stage_entered_idx on stage_events(to_stage, entered_at);

-- Turnaround joins the active (non-revoked) assignment for a project; the existing
-- index leads with estimator_id, so a by-project active lookup can't use it.
create index estimator_assignments_project_active_idx
  on estimator_assignments(project_id) where revoked_at is null;
