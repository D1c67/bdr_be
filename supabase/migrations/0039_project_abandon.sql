-- 0039 — project abandon flag. A bid can be abandoned at any stage (Executive /
-- PA / IT admin). We keep current_stage intact so we always know where in the
-- pipeline it died ("Abandoned at Markup"). The project's lifecycle *status*
-- (active / sent / won / lost / no_award / declined / abandoned) is derived, never
-- stored — only the abandon marker below is persisted (see app/services/
-- project_status.py). Reactivating simply clears these two columns, returning the
-- project to its stage-derived status.
alter table projects add column abandoned_at timestamptz;
-- SET NULL (not cascade) keeps the abandon record if the actor's profile is later
-- removed — matches recorded_by / created_by elsewhere.
alter table projects add column abandoned_by uuid references profiles(id) on delete set null;
