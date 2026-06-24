-- 0007 — Row-Level Security: deny-by-default on every table.
--
-- All application data access flows through the FastAPI backend using the
-- Supabase service-role key, which BYPASSES RLS. Enabling RLS with NO
-- permissive policies means any other credential (anon key, a leaked end-user
-- token hitting PostgREST directly) is denied access to every row. This is the
-- defense-in-depth backstop described in the plan: even if an API guard were
-- missed, direct table access remains impossible for non-service callers.

alter table profiles              enable row level security;
alter table general_contractors   enable row level security;
alter table material_categories   enable row level security;
alter table projects              enable row level security;
alter table project_gcs           enable row level security;
alter table project_files         enable row level security;
alter table go_no_go_votes        enable row level security;
alter table go_no_go_decisions    enable row level security;
alter table stage_events          enable row level security;
alter table vendors               enable row level security;
alter table vendor_contacts       enable row level security;
alter table rfqs                  enable row level security;
alter table rfq_recipients        enable row level security;
alter table quotes                enable row level security;
alter table estimator_assignments enable row level security;
alter table labor_reviews         enable row level security;
alter table markups               enable row level security;
alter table verifications         enable row level security;
alter table notifications         enable row level security;
alter table email_log             enable row level security;
alter table audit_log             enable row level security;

-- Force RLS even for the table owner, so no policy-less path is ever permissive.
alter table profiles              force row level security;
alter table projects              force row level security;
alter table project_files         force row level security;
alter table estimator_assignments force row level security;
