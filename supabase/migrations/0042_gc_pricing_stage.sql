-- 0042 — New 'gc_pricing' pipeline stage, inserted between 'markup' and 'verify'.
-- This is where the per-GC bid numbers are set on purpose (one figure per general
-- contractor), so the editor moves off Send Out and the Executive reviews the
-- per-GC numbers (read-only) at Verify before committing.

-- New enum label. PG12+ allows ADD VALUE in a transaction as long as the new
-- label is not used later in the same transaction — this migration never
-- references 'gc_pricing'. BEFORE 'verify' keeps the enum's physical order in
-- step with the logical pipeline order (the app sequences on workflow.STAGES
-- order ints, so this is cosmetic, but kept tidy).
alter type project_stage add value if not exists 'gc_pricing' before 'verify';
