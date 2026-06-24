-- 0023 — Split the conflated labor_type into two independent fields. The old
-- enum forced one pick among prevailing_wage / non_prevailing_wage / night_work
-- / other, but a project can be night work AND prevailing wage at once.
-- labor_time captures when the work happens; wage_type captures the wage class.
create type labor_time as enum ('day_work', 'night_work');
create type wage_type as enum ('prevailing_wage', 'non_prevailing_wage');

alter table projects add column labor_time labor_time;
alter table projects add column wage_type  wage_type;

-- Carry over what the old single field expressed.
update projects set wage_type = 'prevailing_wage'     where labor_type = 'prevailing_wage';
update projects set wage_type = 'non_prevailing_wage' where labor_type = 'non_prevailing_wage';
update projects set labor_time = 'night_work'         where labor_type = 'night_work';
-- 'other' carried no structured meaning; its note text survives in labor_note.

alter table projects rename column labor_type_note to labor_note;
alter table projects drop column labor_type;
drop type labor_type;
