-- 0027 — Go/No-Go scoring intake answers (reference only).
-- Nine categorical answers picked at intake and shown with rubric points in a
-- reference table on the Go/No-Go step. The system never validates, gates, or
-- decides anything from them. Unlike labor_time/wage_type (0023) these are
-- plain text, not pg enums: the rubric is advisory and expected to be tweaked,
-- every write goes through the API (whose Literal types reject bad values),
-- and the points themselves live in the frontend rubric
-- (bdr_fe/lib/gonoScoring.ts), not the database.
alter table projects
  add column project_type     text,
  add column owner_type       text,
  add column labor_needed     text,
  add column bid_method       text,
  add column competitor_known text,
  add column gc_known         text,
  add column subs_needed      text,
  add column est_value_band   text,
  add column scope_fit        text;
