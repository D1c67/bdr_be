-- 0031 — Per-GC proposal amounts. G3 sometimes bids different numbers to
-- different GCs on the same project: the committed pricing (verify, step 9)
-- stays the default, and the PA/PM may override material and/or labor per GC
-- on the Send Out step BEFORE generating that GC's proposal document.
--
-- Overrides live on project_gcs (the per-project×GC row): they exist before
-- any proposal_sends row does, vanish with the membership row when a GC is
-- removed, and a re-added GC starts back at the defaults. NULL = use the
-- committed pricing figure; the total is never stored — always material+labor.
alter table project_gcs
  add column proposal_material_amount numeric(14,2)
    check (proposal_material_amount >= 0),
  add column proposal_labor_amount numeric(14,2)
    check (proposal_labor_amount >= 0);

-- Stamp of the figures actually rendered into each generated .docx. Send-time
-- isolation compares these against the live (override-resolved) amounts and
-- refuses to ship a stale document; after a send they are the audit record of
-- the numbers that GC was given. NULL on rows generated before this feature.
alter table proposal_sends
  add column material_amount numeric(14,2),
  add column labor_amount numeric(14,2);
