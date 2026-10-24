-- 0164 (MB) - AI Estimating page facts (docs/AI_ESTIMATING_BUILD_SPEC.md 5.6 MB,
-- section 6 WS1 steps 8 and 9).
--
-- DEV ONLY during the build (Supabase project bpidntbyvoooqvaispup); ships
-- with 0165 to 0169 at release. Applied by the integrator in letter order
-- (MB before MC).
--
-- plans_reader_documents gains the per-document facts state:
--   sha256         the stored PDF's sha256 (the facts cache key with kind and
--                  extractor_version)
--   facts_status   pending | running | done | failed | skipped; NULL on rows
--                  from before this migration (the gate treats NULL as pending)
--   facts_error    the last failure sentence (500 chars)
--   category_label the label the user or the splitter gave the document; the
--                  category column holds the label the run reads it under
--                  (the facts job's relabel changes `category`, never this)
--   facts_doc      per-document results: cache hit or miss, the category
--                  check and relabel decision, the reduced-reading flag, the
--                  spec page selection summary, extraction stats
--
-- plans_reader_doc_facts   one row per (sha256, kind, extractor_version):
--                          DocFacts without the page list (legend, schedules,
--                          panels, fixture types, spec map, cross-trade
--                          mentions, title-block slots, problems, stats);
--                          blob_prefix names the per-page word blobs in
--                          storage (plans-reader/_facts/{sha256}/{version}/).
-- plans_reader_page_facts  one row per page of a cached document with the
--                          indexed columns beside the PageFacts JSON; cascades
--                          with its document row.
--
-- The same bytes labeled specifications in run A and electrical_drawings in
-- run B are two cache entries (kind text vs drawing). Bumping the extractor
-- version invalidates everything without a delete.
--
-- Idempotent: every statement can run twice. Service role only: RLS on and
-- forced, no policies, nothing granted to anon or authenticated.

alter table public.plans_reader_documents add column if not exists sha256         text;
alter table public.plans_reader_documents add column if not exists facts_status   text;
alter table public.plans_reader_documents add column if not exists facts_error    text;
alter table public.plans_reader_documents add column if not exists category_label text;
alter table public.plans_reader_documents add column if not exists facts_doc      jsonb;

alter table public.plans_reader_documents
  drop constraint if exists plans_reader_documents_facts_status_check;
alter table public.plans_reader_documents
  add constraint plans_reader_documents_facts_status_check
  check (facts_status is null or facts_status in ('pending', 'running', 'done', 'failed', 'skipped'));

comment on column public.plans_reader_documents.sha256 is
  'sha256 of the stored PDF; with kind and extractor_version the key into plans_reader_doc_facts.';
comment on column public.plans_reader_documents.facts_status is
  'Page facts job state: pending, running, done, failed or skipped. NULL = never enqueued (treated as pending while PLANS_READER_PAGE_FACTS_ENABLED).';
comment on column public.plans_reader_documents.category_label is
  'The label the user or splitter gave the document. category is what the run reads it under (the facts relabel may differ).';
comment on column public.plans_reader_documents.facts_doc is
  'Per-document facts results: cache hit or miss, category check and relabel, reduced_reading, spec selection summary, stats.';

create index if not exists plans_reader_documents_sha256_idx
  on public.plans_reader_documents (sha256) where sha256 is not null;

create table if not exists public.plans_reader_doc_facts (
  sha256            text not null,
  kind              text not null check (kind in ('drawing', 'text')),
  extractor_version text not null,
  facts             jsonb not null,
  blob_prefix       text,
  stats             jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  primary key (sha256, kind, extractor_version)
);

comment on table public.plans_reader_doc_facts is
  'Page facts cache per document bytes: DocFacts without pages (legend, schedules, panels, fixture types, spec map, cross-trade, slots, stats). Word blobs live in storage under blob_prefix.';

create table if not exists public.plans_reader_page_facts (
  sha256            text not null,
  kind              text not null,
  extractor_version text not null,
  page              int not null check (page >= 1),
  sheet_number      text,
  sheet_title       text,
  discipline        text,
  page_role         text,
  roles             text[] not null default '{}',
  text_class        text,
  primary_ft_per_in numeric(10, 4),
  facts             jsonb not null,
  created_at        timestamptz not null default now(),
  primary key (sha256, kind, extractor_version, page),
  foreign key (sha256, kind, extractor_version)
    references public.plans_reader_doc_facts (sha256, kind, extractor_version)
    on delete cascade
);

comment on table public.plans_reader_page_facts is
  'One PageFacts row per page of a cached document (sheet number, title, discipline, roles, text class, primary scale, the facts JSON).';

create index if not exists plans_reader_page_facts_sheet_idx
  on public.plans_reader_page_facts (sheet_number) where sheet_number is not null;

alter table public.plans_reader_doc_facts enable row level security;
alter table public.plans_reader_doc_facts force row level security;
alter table public.plans_reader_page_facts enable row level security;
alter table public.plans_reader_page_facts force row level security;
revoke all on public.plans_reader_doc_facts from anon, authenticated;
revoke all on public.plans_reader_page_facts from anon, authenticated;

notify pgrst, 'reload schema';
