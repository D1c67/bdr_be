-- 0093 — FORCE row-level security on every public table that lacks it.
--
-- 0055 swept the tables that existed then, but most module migrations since
-- (0057–0092: PM, CP, email ingestion, submittals, training, …) create their
-- tables with ENABLE only. On dev the gap was closed by hand outside any
-- migration file, leaving the ledger and the live posture out of sync. This is
-- the durable version of that sweep: dynamic and idempotent, so it converges
-- every environment (a re-run is a no-op) and also covers any future table
-- that slips through with ENABLE only.
--
-- Same posture as 0055: deny-by-default, no per-role policies — all access is
-- enforced in the FastAPI layer. The service-role key carries BYPASSRLS and is
-- unaffected; FORCE closes the table-OWNER path (SQL editor, owner-connected
-- tooling), which otherwise bypasses RLS entirely on a policy-less table.

do $$
declare
  t record;
begin
  for t in
    select c.relname, c.relrowsecurity
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind = 'r'
      and (not c.relrowsecurity or not c.relforcerowsecurity)
  loop
    if not t.relrowsecurity then
      execute format('alter table public.%I enable row level security', t.relname);
    end if;
    execute format('alter table public.%I force row level security', t.relname);
  end loop;
end $$;

notify pgrst, 'reload schema';
