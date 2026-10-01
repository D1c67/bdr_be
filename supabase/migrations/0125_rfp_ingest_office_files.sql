-- 0125 - RFP Ingestion sandbox: Word and Excel files
-- (docs/RFP_INGESTION_SANDBOX.md, sections 2.1, 3.5 and 3.6). Apply after 0124.
--
-- The sandbox accepted PDFs only; anything else was rejected/not_pdf. From
-- this migration on a .docx / .xlsx / .doc / .xls (recognised by magic and
-- declared extension, never opened by the API) is quarantined as it is,
-- converted to a PDF through the Gotenberg service the previews already use,
-- and the derived PDF is verified by the sandbox child like any upload. Two
-- columns on rfp_ingest_files carry that:
--
--   source_format  : what the parent's sniff took the file for. 'pdf' for
--                    every row that exists today (the default), one of the
--                    four office formats otherwise. It is also the extension
--                    of the quarantine object, {run_id}/{file_id}/source.<ext>.
--   converted_path : the derived-bucket path of the converter's output,
--                    {run_id}/{file_id}/converted.pdf, once it has been
--                    uploaded; null for a PDF. A re-run reuses that object
--                    when its digest matches the manifest's conversion block,
--                    and the retention prune clears the column with the
--                    other path columns when the objects go.
--
-- The verdict codes the step adds (rejected/conversion_rejected, failed/
-- conversion_unavailable) need no DDL: reject_code has no CHECK, and
-- app/sandbox/protocol.py stays the authority.
--
-- Release order: apply this migration, then deploy. The old code never
-- writes either column and reads neither, so the default covers the window.
-- Every statement here is idempotent.

-- ── 1. rfp_ingest_files: the source format and the converted PDF ─────────

alter table rfp_ingest_files
  add column if not exists source_format  text not null default 'pdf',
  add column if not exists converted_path text;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.rfp_ingest_files'::regclass
      and conname = 'rfp_ingest_files_source_format_check'
  ) then
    alter table rfp_ingest_files add constraint rfp_ingest_files_source_format_check
      check (source_format in ('pdf', 'docx', 'xlsx', 'doc', 'xls'));
  end if;
end
$$;

comment on column rfp_ingest_files.source_format is
  'What the parent sniff took the file for (pdf, docx, xlsx, doc, xls); also the quarantine object extension. app/sandbox/protocol.py SOURCE_FORMATS is the authority.';
comment on column rfp_ingest_files.converted_path is
  'rfp-derived path of the PDF the converter produced from an office file ({run_id}/{file_id}/converted.pdf); null for a PDF source or once the objects are pruned.';

notify pgrst, 'reload schema';
