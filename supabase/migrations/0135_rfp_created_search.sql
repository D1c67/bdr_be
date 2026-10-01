-- 0135 - RFP Ingestion: search over the "Created from RFP Ingestion" page
-- (docs/RFP_CREATE.md sections 8 and 9). Apply after 0134.
--
-- The page gains a search box and date filters. The date filters read
-- rfp_created_projects.created_at directly; the search has to match text
-- that lives on four other tables (the project, its GCs, the source email
-- or portal invitation), and it has to run server side so the
-- (created_at, project_id) paging stays correct. This view flattens that
-- text into one lowercased column the router filters with a single ILIKE,
-- next to the columns the page filters, orders and pages on.
--
-- search_text is, space separated: project number, project name, every
-- linked GC name (project_gcs -> general_contractors, distinct, sorted),
-- the email subject or the portal invitation title, sender_display and
-- invitation_method. A null part is skipped (concat_ws).
--
-- security_invoker so the view never widens what the caller's own
-- privileges and RLS allow; anon and authenticated lose every grant
-- anyway (the backend reads it with the service role). No index: the table
-- is small (one row per created project) and the router bounds every read
-- with a limit. Idempotent.

create or replace view public.rfp_created_search
with (security_invoker = true)
as
select
  r.project_id,
  r.created_at,
  r.cleared_at,
  lower(concat_ws(' ',
    p.number,
    p.name,
    (
      select string_agg(distinct gc.name, ' ' order by gc.name)
        from project_gcs pg
        join general_contractors gc on gc.id = pg.gc_id
       where pg.project_id = r.project_id
    ),
    e.subject,
    i.title,
    r.sender_display,
    r.invitation_method
  )) as search_text
from rfp_created_projects r
left join projects p on p.id = r.project_id
left join rfp_emails e on e.id = r.rfp_email_id
left join rfp_portal_invitations i on i.id = r.portal_invitation_id;

comment on view public.rfp_created_search is
  'One row per rfp_created_projects record: project_id, created_at, cleared_at and a lowercased search_text (project number and name, linked GC names, email subject or portal title, sender_display, invitation_method) for the search box on the "Created from RFP Ingestion" page. Service role only.';

revoke all on public.rfp_created_search from anon, authenticated;

notify pgrst, 'reload schema';
