-- 0100: Pricing sections. Switchgear, Generator & Equipment, Trenching and Low
-- Voltage break out of the single Materials pricing bucket into their own named
-- sections (gear / underground / low_voltage), each with its own markup, verify
-- snapshot line, per-GC price override, and proposal_sends stamp. The mapping
-- axis is a flag on material_categories (rename-proof after backfill, like
-- is_general); everything else 'materials'. Committed verifications written
-- BEFORE this release keep NULL in every new column: the readers resolve a
-- committed NULL section to "not part of the decomposition" so legacy projects
-- compute exactly as before (materials_amount carries the full figure).

-- 1) The mapping axis: which pricing section a material category rolls into.
alter table material_categories
  add column if not exists pricing_section text not null default 'materials';

do $$ begin
  alter table material_categories
    add constraint material_categories_pricing_section_check
    check (pricing_section in ('materials', 'gear', 'underground', 'low_voltage'));
exception when duplicate_object then null; end $$;

-- Backfill by exact name match, material-kind rows only ('Generator and
-- Equipment' is defensive; the seed says 'Generator & Equipment'). Custom and
-- LLM-invented categories, General Material, Lighting, EV Chargers and every
-- kind='markup' row stay 'materials'; new categories default to 'materials'.
update material_categories set pricing_section = 'gear'
 where kind = 'material'
   and lower(name) in ('switchgear', 'generator & equipment', 'generator and equipment');
update material_categories set pricing_section = 'underground'
 where kind = 'material' and lower(name) = 'trenching';
update material_categories set pricing_section = 'low_voltage'
 where kind = 'material' and lower(name) = 'low voltage';

-- 2) Per-section markups (step 8), same pct/amount pair shape as 0017.
alter table markups add column if not exists gear_markup_pct           numeric(6,3);
alter table markups add column if not exists gear_markup_amount        numeric(14,2);
alter table markups add column if not exists underground_markup_pct    numeric(6,3);
alter table markups add column if not exists underground_markup_amount numeric(14,2);
alter table markups add column if not exists low_voltage_markup_pct    numeric(6,3);
alter table markups add column if not exists low_voltage_markup_amount numeric(14,2);

-- 3) Verify snapshot lines (step 9), mirroring 0018. On commit a section not on
-- the project is stored NULL (never 0); present sections always get a value.
alter table verifications add column if not exists gear_amount               numeric(14,2);
alter table verifications add column if not exists gear_markup_amount        numeric(14,2);
alter table verifications add column if not exists underground_amount        numeric(14,2);
alter table verifications add column if not exists underground_markup_amount numeric(14,2);
alter table verifications add column if not exists low_voltage_amount        numeric(14,2);
alter table verifications add column if not exists low_voltage_markup_amount numeric(14,2);

-- 4) Per-GC price overrides, mirroring 0031. NULL = use the committed default.
alter table project_gcs
  add column if not exists proposal_gear_amount numeric(14,2)
    check (proposal_gear_amount >= 0);
alter table project_gcs
  add column if not exists proposal_underground_amount numeric(14,2)
    check (proposal_underground_amount >= 0);
alter table project_gcs
  add column if not exists proposal_low_voltage_amount numeric(14,2)
    check (proposal_low_voltage_amount >= 0);

-- 5) Stamps of the section figures rendered into each generated .docx,
-- mirroring 0031: the send-time staleness proof and the per-GC audit record.
-- NULL = section not on the project (or a pre-release document).
alter table proposal_sends add column if not exists gear_amount        numeric(14,2);
alter table proposal_sends add column if not exists underground_amount numeric(14,2);
alter table proposal_sends add column if not exists low_voltage_amount numeric(14,2);

-- 6) Release data fix, "reopen at Verify" (user-approved): projects sitting at
-- Send Out with committed pricing but NOTHING sent yet are bounced back to
-- Verify so the Executive re-commits under the new section breakdown (their
-- old 2-bucket snapshot would otherwise ship section-less documents). The
-- legacy snapshot numbers are cleared so the re-verify form seeds from the
-- live upstream figures. Projects at submitted/bid_outcome or with sent (or
-- in-flight) proposals are untouched; the legacy-NULL resolution rule keeps
-- their numbers coherent. Idempotent: a re-run finds committed_at NULL.
do $$
declare
  affected uuid[];
begin
  select coalesce(array_agg(v.project_id), '{}') into affected
    from verifications v
    join project_category_state pcs
      on pcs.project_id = v.project_id
     and pcs.category = 'send_out'
     and pcs.current_task = 'send_out'
   where v.committed_at is not null
     and not exists (
       select 1 from proposal_sends ps
        where ps.project_id = v.project_id
          and ps.status in ('sent', 'sending')
     );

  if array_length(affected, 1) is null then
    return;
  end if;

  update verifications
     set committed_at = null,
         verified_by = null,
         labor_amount = null,
         materials_amount = null,
         labor_markup_amount = null,
         materials_markup_amount = null
   where project_id = any(affected);

  update project_category_state
     set current_task = 'verify', status = 'active', completed_at = null
   where category = 'send_out' and project_id = any(affected);

  update projects
     set reverify_return_stage = 'send_out', current_stage = 'verify'
   where id = any(affected) and current_stage = 'send_out';

  insert into stage_events (project_id, from_stage, to_stage, category, note)
  select pid, 'send_out'::project_stage, 'verify'::project_stage,
         'send_out'::project_category,
         'Pricing sections release: pricing must be re-verified under the new section breakdown'
    from unnest(affected) as pid;
end $$;

-- Reload PostgREST's schema cache so the new columns are visible immediately.
notify pgrst, 'reload schema';
