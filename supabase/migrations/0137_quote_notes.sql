-- 0137: Quote notes. A per-quote thread on Receive Quotes / Select Vendors so
-- the team can record what sets one quote apart from another in the same
-- category ("excludes fixtures", "lead time 12 weeks").
--
-- Separate from quotes.notes, which is the single note typed when a quote is
-- entered by hand (shown as the pinned "entry note" and left untouched here).
-- Notes are commentary only: adding or removing one never changes a price.
-- Internal only: the estimator portal never reads this table.

create table if not exists quote_notes (
  id          uuid primary key default gen_random_uuid(),
  quote_id    uuid not null references quotes(id) on delete cascade,
  body        text not null
              constraint quote_notes_body_len
              check (char_length(btrim(body)) between 1 and 2000),
  -- Actor convention (see 0012): keep the note if the author is deleted.
  author_id   uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now()
);

create index if not exists quote_notes_quote_idx on quote_notes(quote_id, created_at);
create index if not exists quote_notes_author_idx on quote_notes(author_id);

-- RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.
alter table quote_notes enable row level security;
alter table quote_notes force row level security;
