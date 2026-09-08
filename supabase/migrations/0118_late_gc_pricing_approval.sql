-- 0118 - Executive approval of a per-GC price change for a GC added after the
-- bid went out, plus a metadata column on notifications for per-GC deep links.
--
-- A GC can join a project after "Done sending" (send_out lane head at
-- submitted or bid_outcome). Its proposal is sent from the GC list in the
-- project side menu. When the sender changes that GC's per-section prices, the
-- change must be approved by an Executive before anything is generated or
-- sent (an Executive's own change is auto-approved). The project stays on its
-- stage: this is a per-GC lock, not a re-verify bounce.
--
-- pricing_approval_status:
--   NULL      the GC never went through the approval flow (every pre-0118 GC,
--             every GC bid on the active Send Out step)
--   pending   overrides written, waiting for an Executive; generate / send /
--             mark-submitted / amount edits all refuse for this GC
--   approved  an Executive accepted (possibly after editing the figures)
--   rejected  an Executive declined; the overrides were cleared back to the
--             project figures and the requester may send at those or ask again
-- The five proposal_*_amount override columns (0031, 0100) keep being the
-- stored source of truth for the figures themselves.
--
-- notifications.metadata carries {"gc_id": ...} on the gc_pricing.* types so
-- the bell row and the mirrored email can open the exact GC's modal
-- (/projects/{id}?box=gcs&gc={gc_id}) and so an approval can dismiss the
-- request notifications for that one GC without touching another GC's.

alter table project_gcs
  add column if not exists pricing_approval_status text
    check (pricing_approval_status in ('pending', 'approved', 'rejected')),
  add column if not exists pricing_requested_by uuid references profiles(id) on delete set null,
  add column if not exists pricing_requested_at timestamptz,
  add column if not exists pricing_request_note text,
  add column if not exists pricing_decided_by uuid references profiles(id) on delete set null,
  add column if not exists pricing_decided_at timestamptz,
  add column if not exists pricing_decision_note text;

comment on column project_gcs.pricing_approval_status is
  'Executive approval of a per-GC price change for a GC added after send-out: NULL = never requested, pending = locked awaiting an Executive, approved, rejected (overrides cleared).';
comment on column project_gcs.pricing_requested_by is
  'Who asked for (or, for an Executive, made) the per-GC price change.';
comment on column project_gcs.pricing_decided_by is
  'The Executive who approved or rejected it; equals pricing_requested_by when auto-approved.';

-- The dashboard counts pending approvals per project; keep the index tiny.
create index if not exists project_gcs_pricing_pending_idx
  on project_gcs(project_id) where pricing_approval_status = 'pending';

alter table notifications add column if not exists metadata jsonb;

comment on column notifications.metadata is
  'Optional per-type detail for deep links and targeted dismissal; gc_pricing.* rows carry {"gc_id": ...}.';

notify pgrst, 'reload schema';
