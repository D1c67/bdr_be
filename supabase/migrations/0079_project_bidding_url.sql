-- 0079 — the bidding site link for a project.
-- The invitation usually points at a host (BuildingConnected, iSqFt, a GC's own
-- portal) that carries the full bid details. The team needs it at Go/No-Go and
-- again at Labor Numbers, so it's captured once at intake and linked from both
-- steps. Answering is mandatory at intake: either a URL, or `no_bidding_url`
-- ticked to say this project has no site. Existing rows read as "unanswered"
-- (null URL, flag false), which the UI surfaces as "add a link".
alter table public.projects
    add column if not exists bidding_url text,
    add column if not exists no_bidding_url boolean not null default false;

-- The two columns are halves of one answer — a URL and "there is no URL" can
-- never both be set. The API enforces the same rule; this is the backstop.
alter table public.projects
    drop constraint if exists projects_bidding_url_answer_ck;
alter table public.projects
    add constraint projects_bidding_url_answer_ck
    check (not (no_bidding_url and bidding_url is not null));

-- Reload PostgREST's schema cache so the new columns are visible immediately.
notify pgrst, 'reload schema';
