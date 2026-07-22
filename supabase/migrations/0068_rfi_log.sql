-- 0068 — RFI log: drawing/reference lists, priority, a real assignee, attachments.
--
-- Grows the RFIs added in 0060 from a bare subject/question record into a proper
-- RFI log. Status and the send flow are deliberately untouched here.
--
--   1. drawing_numbers / applicable_references — free-text lists. There is no
--      drawings or specs table to point at, so these stay text[] chips rather than
--      fabricate FKs to records that don't exist. Note "references" is a reserved
--      SQL word; hence applicable_references.
--   2. priority — new enum, exactly the two levels the RFI log shows. Nothing in
--      the schema had a priority pattern before, so this establishes one.
--   3. assigned_gc_id / assigned_contact_id — replaces free-text asked_of with the
--      established general_contractors → gc_contacts company/contact pair (the same
--      shape 0028 migrated GCs onto). The FK cannot express that a contact belongs
--      to the assigned company — routers/pm_field.py enforces that pairing.
--   4. question becomes sanitized HTML (Tiptap on the client, nh3 on the server).
--
-- asked_of is KEPT, not dropped: it is backfilled into assigned_gc_id where a name
-- matches exactly and otherwise preserved as legacy text the UI still renders, so
-- no typed-in assignee is destroyed by this migration. Drop it in a later migration
-- once the backfill has been eyeballed.

create type rfi_priority as enum ('standard', 'urgent');

alter table rfis
  add column drawing_numbers       text[]       not null default '{}',
  add column applicable_references text[]       not null default '{}',
  add column priority              rfi_priority not null default 'standard',
  add column assigned_gc_id        uuid references general_contractors(id) on delete set null,
  add column assigned_contact_id   uuid references gc_contacts(id) on delete set null;

create index if not exists rfis_assigned_gc_idx on rfis(assigned_gc_id);

-- Plain text → HTML. Every existing value is plain text (the field has only ever
-- been fed by a <Textarea>), so a blanket escape is correct and deterministic — no
-- "does this look like HTML" heuristic needed. Escape & FIRST or the later escapes
-- double-encode their own output; then newlines → <br> so existing line breaks
-- survive the format change. The ^<p> guard makes a re-run a no-op rather than
-- silently double-escaping real RFI text.
update rfis
set question = '<p>' ||
      replace(
        replace(
          replace(
            replace(question, '&', '&amp;'),
          '<', '&lt;'),
        '>', '&gt;'),
      E'\n', '<br>') ||
    '</p>'
where question !~ '^<p>';

-- Backfill the assignee where asked_of names a GC we already have. In practice the
-- field was typed as "Company — Role" ("Armstrong Co. — PM"), so a plain equality
-- match finds nothing; take the segment before a " — " role suffix as the candidate
-- company. split_part returns the whole string when the separator is absent, so
-- this one pass covers bare company names too. Only the em-dash form is split — a
-- hyphen is too plausible inside a real company name to treat as a separator.
--
-- Still deliberately conservative: case-insensitive, and only where the candidate
-- resolves to exactly ONE known GC. Anything else (an owner or municipality that
-- was never a GC, e.g. "City of Henderson — Project Engineer") keeps its free text
-- and surfaces in the UI for a human to reconcile, rather than being guessed at.
with candidate as (
  select r.id as rfi_id, btrim(split_part(r.asked_of, ' — ', 1)) as company
  from rfis r
  where r.asked_of is not null and r.assigned_gc_id is null
)
update rfis r
set assigned_gc_id = m.gc_id
from (
  select c.rfi_id, g.id as gc_id
  from candidate c
  join general_contractors g on lower(btrim(g.name)) = lower(c.company)
  where (
    select count(*) from general_contractors g2
    where lower(btrim(g2.name)) = lower(c.company)
  ) = 1
) m
where r.id = m.rfi_id;

-- Attachments reference the Documents hub's "source:id" key rather than a per-store
-- FK, so one row shape covers a PM upload, a bidding file and a CP file alike. The
-- tradeoff is a soft reference: keys are resolved through
-- pm_folders.list_project_documents on read, which means attachment visibility
-- inherits the hub's rules for free (unsent estimator drafts stay hidden) and a
-- deleted document drops out of the list instead of dangling. The check constraint
-- keeps the key shape honest since no FK can.
create table rfi_attachments (
  id         uuid primary key default gen_random_uuid(),
  rfi_id     uuid not null references rfis(id) on delete cascade,
  doc_key    text not null check (doc_key ~ '^(pm|bid|cp):[0-9a-f-]{36}$'),
  created_by uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (rfi_id, doc_key)
);
create index if not exists rfi_attachments_rfi_idx on rfi_attachments(rfi_id);

alter table rfi_attachments enable row level security;
alter table rfi_attachments force  row level security;

notify pgrst, 'reload schema';
