-- 0066 — Certified Payroll file→project mapping. A generated CPR file can
-- cover many projects (PVW Sheet, eComply CSV span every project on the
-- weekly report) or exactly one (per-project LCP Tracker CSV / paper CPR
-- XLSX), so a join table records EVERY project each cp_record_files row
-- pertains to — the project-documents hub shows a file under each of them.

create table cp_record_file_projects (
  record_file_id uuid not null references cp_record_files(id) on delete cascade,
  project_id     uuid not null references projects(id)        on delete cascade,
  primary key (record_file_id, project_id)
);
create index cp_record_file_projects_project_idx on cp_record_file_projects(project_id);

alter table cp_record_file_projects enable row level security;
alter table cp_record_file_projects force  row level security;

-- Best-effort backfill of existing cp_record_files (CP is dev-only, so this
-- is convenience — defensive so a hiccup never aborts the DDL above).
do $$
begin
  -- Aggregate files (PVW Sheet / eComply CSV, any revision prefix) cover every
  -- project with CP time entries on their report.
  begin
    insert into cp_record_file_projects (record_file_id, project_id)
    select distinct rf.id, te.project_id
    from cp_record_files rf
    join cp_records r on r.id = rf.record_id
    join cp_time_entries te on te.payroll_report_id = r.payroll_report_id
    where te.project_id is not null
      and (rf.filename ilike '%PVW%' or rf.filename ilike '%eComply%')
    on conflict do nothing;
  exception when others then
    raise notice 'cp_record_file_projects aggregate backfill skipped: %', sqlerrm;
  end;

  -- Per-project files ('{number} LCP CPR Upload.csv' / '{number} Paper CPR.xlsx',
  -- possibly with a 'Revised '/'Revised N ' prefix) map to the project whose
  -- number is the token immediately before the fixed suffix — anchoring on the
  -- suffix sidesteps the 'Revised 6370 …' vs 'Revised 2 6370 …' prefix
  -- ambiguity. Matched against that report's project set only.
  begin
    insert into cp_record_file_projects (record_file_id, project_id)
    select distinct rf.id, te.project_id
    from cp_record_files rf
    join cp_records r on r.id = rf.record_id
    join cp_time_entries te on te.payroll_report_id = r.payroll_report_id
    join projects p on p.id = te.project_id
    where te.project_id is not null
      and (rf.filename ilike '% LCP CPR Upload.csv' or rf.filename ilike '% Paper CPR.xlsx')
      and p.number = substring(rf.filename from '(\S+) (?:LCP CPR Upload\.csv|Paper CPR\.xlsx)$')
    on conflict do nothing;
  exception when others then
    raise notice 'cp_record_file_projects per-project backfill skipped: %', sqlerrm;
  end;
end $$;

notify pgrst, 'reload schema';
