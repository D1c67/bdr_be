-- 0055 — FORCE row-level security on every remaining RLS-enabled table.
--
-- Convention alignment / defense-in-depth. 0007 enabled RLS everywhere but
-- FORCED it on only four tables; later migrations (0025, 0026, 0028, 0029,
-- 0032, 0050) forced their new tables, leaving the rest enabled-but-not-forced.
-- Without FORCE the table OWNER bypasses RLS, so a policy-less owner path
-- stays permissive; forcing closes that path on every table. This changes
-- nothing for the app: all access goes through postgres/service_role, which
-- carry BYPASSRLS and ignore FORCE entirely. Safe under the manual-migration
-- workflow, and RLS-only DDL needs no PostgREST schema reload.
--
-- `if exists` keeps this re-runnable and tolerant of tables dropped later
-- (go_no_go_votes, dropped in 0047, is deliberately absent).

alter table if exists general_contractors        force row level security;
alter table if exists material_categories        force row level security;
alter table if exists project_gcs                force row level security;
alter table if exists go_no_go_decisions         force row level security;
alter table if exists stage_events               force row level security;
alter table if exists vendors                    force row level security;
alter table if exists vendor_contacts            force row level security;
alter table if exists rfqs                       force row level security;
alter table if exists rfq_recipients             force row level security;
alter table if exists quotes                     force row level security;
alter table if exists labor_reviews              force row level security;
alter table if exists markups                    force row level security;
alter table if exists verifications              force row level security;
alter table if exists notifications              force row level security;
alter table if exists email_log                  force row level security;
alter table if exists audit_log                  force row level security;
alter table if exists boq_analyses               force row level security;
alter table if exists rfq_line_items             force row level security;
alter table if exists general_material_estimates force row level security;
alter table if exists rfq_sends                  force row level security;
alter table if exists rfq_messages               force row level security;
alter table if exists quote_revisions            force row level security;
alter table if exists graph_sync_state           force row level security;
alter table if exists proposal_drafts            force row level security;
alter table if exists proposal_sends             force row level security;
alter table if exists bid_outcomes               force row level security;
alter table if exists bid_gc_outcomes            force row level security;
