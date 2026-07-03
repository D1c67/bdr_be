-- 0049 — Relax labor_time / wage_type from fixed enums to free text. Projects
-- sometimes need a labor-timing or wage class beyond the two seeded presets
-- (e.g. swing shift, a specific union agreement), so callers can now store any
-- custom string the estimator types. The former enum values
-- ('day_work', 'night_work', 'prevailing_wage', 'non_prevailing_wage') survive
-- unchanged as plain text and stay the picker's suggested options.
alter table projects alter column labor_time type text using labor_time::text;
alter table projects alter column wage_type  type text using wage_type::text;

-- Nothing references the enum types anymore.
drop type labor_time;
drop type wage_type;

notify pgrst, 'reload schema';
