-- 0021 — In-app file preview: file metadata + office→PDF derivative tracking.
--
-- Office files (.xlsx/.xlsm/.docx/.doc) get a PDF derivative generated once at
-- upload time and stored next to the original in the private bucket
-- ({project_id}/previews/{file_id}.pdf), so preview is a cheap signed-URL fetch
-- through the existing PDF modal.
--
-- preview_status lifecycle: none → pending → ready | failed (single in-app retry).

alter table project_files
  add column mime_type      text,
  add column size_bytes     bigint,
  add column preview_path   text,
  add column preview_status text not null default 'none'
    check (preview_status in ('none', 'pending', 'ready', 'failed')),
  add column preview_error  text;
