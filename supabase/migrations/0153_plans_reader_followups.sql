-- 0153: AI Estimating follow-ups (docs/PLANS_READER.md section 8.4).
--
-- - One active deliverable change per run: a partial unique index on the
--   run's queued or running `modify` groups (a model modification or a
--   re-render). The Modify route and plans_reader_runs.claim_change_slot
--   turn the 23505 into a 409.
-- - One active answer per chat thread: a partial unique index on the
--   thread's pending or running assistant message
--   (plans_reader_chat.send_message turns the 23505 into its busy refusal).
-- - A dedicated `settling` status for a split file whose documents are
--   being written (the settler's claim); before this the code borrowed
--   `converted`, which stays accepted for rows written by older code.
-- - Column comments corrected to what the code writes: the segment and
--   document page_meta entries, the groups' pages items and pass.
--
-- Existing duplicates that would block an index are settled first (the
-- oldest active row is kept), so the index creation cannot fail on old
-- test data. Every statement is idempotent. DEV database only until the
-- release.

-- ---------------------------------------------------------------------------
-- One active modify group per run
-- ---------------------------------------------------------------------------
update plans_reader_groups g
   set status = 'canceled',
       finished_at = now(),
       error = 'Superseded by an earlier change to this run (migration 0153 cleanup).'
 where g.kind = 'modify'
   and g.status in ('queued', 'running')
   and exists (
     select 1
       from plans_reader_groups o
      where o.run_id = g.run_id
        and o.kind = 'modify'
        and o.status in ('queued', 'running')
        and (o.created_at, o.id) < (g.created_at, g.id)
   );

create unique index if not exists plans_reader_groups_one_active_modify_uq
  on plans_reader_groups (run_id)
  where kind = 'modify' and status in ('queued', 'running');

-- ---------------------------------------------------------------------------
-- One active assistant answer per chat thread
-- ---------------------------------------------------------------------------
update plans_reader_chat_messages m
   set status = 'failed',
       error = 'This answer stopped before it finished. Send the message again.'
 where m.role = 'assistant'
   and m.status in ('pending', 'running')
   and exists (
     select 1
       from plans_reader_chat_messages o
      where o.thread_id = m.thread_id
        and o.role = 'assistant'
        and o.status in ('pending', 'running')
        and (o.created_at, o.id) < (m.created_at, m.id)
   );

create unique index if not exists plans_reader_chat_messages_one_active_answer_uq
  on plans_reader_chat_messages (thread_id)
  where role = 'assistant' and status in ('pending', 'running');

-- ---------------------------------------------------------------------------
-- plans_reader_files.status gains 'settling'
-- ---------------------------------------------------------------------------
alter table plans_reader_files drop constraint if exists plans_reader_files_status_check;
alter table plans_reader_files add constraint plans_reader_files_status_check
  check (status in ('pending', 'splitting', 'settling', 'split', 'labeled', 'converted', 'failed'));

comment on column plans_reader_files.status is
  'pending (uploaded, not started) -> splitting (staged into the run''s splitter job) -> settling (the settler claimed the split result and is writing its documents; a claim older than 10 minutes in an unchanged run is handed back to splitting) -> split; labeled = read whole as one document; failed = not read (error says why). converted is the settling value code before 0153 wrote; read as settling.';

-- ---------------------------------------------------------------------------
-- Column comments that now match the code
-- ---------------------------------------------------------------------------
comment on column bid_split_segments.page_meta is
  'Per-page detail for this segment, one entry per page of the stored segment PDF in segment-PDF order: [{"page": <1-based source-file page>, "pdf_page": <1-based page of the segment PDF>, "prepended": <true for a General / Cover Sheets page the splitter copied in front of a trade segment; such pages are read in the General document, not again in the trade document>, "title": <sheet number and title or null>, "page_role": <cover_index|legend_symbols|general_notes|schedule|plan|detail|diagram|site|spec_text|other>, "confidence": <0..1>}]. A file triage left intact gets one entry per page with null titles and the role its kind implies (spec_text for specifications, rfp and addenda). Written after island repair; NULL on segments cut before 0152 and where the stored model pages do not cover the range.';

comment on column plans_reader_documents.page_meta is
  'The page detail of this document in stored-PDF order: a splitter segment copies bid_split_segments.page_meta; a labeled text file (specifications, rfp, addenda, other) gets the triage-style list; a labeled drawing set gets a derived list (no classify pass ran): {"page", "pdf_page", "prepended": false, "title": null, "page_role": "other", "confidence": null, "derived": true, "w_in", "h_in"} per page, so its sheets reach the run ledger''s sheet index under the same page numbers the sub agent reads. Output and group `page` values are pdf_page values.';

comment on column plans_reader_groups.pages is
  'The pages one call reads, in request order: [{"document_id": uuid, "page": <1-based page of the document''s stored PDF, prepended cover pages included>, "kind": "image"|"file", "ppi": int|null, "detail": "original"|"high"|null, "tiles": null|[[x0,y0,x1,y1] page fractions]}]. file items (PDF spans of specifications, rfp, addenda and other documents) carry null ppi, detail and tiles. image items also carry "w_in" and "h_in" (the sheet size in inches). Escalation items (pass 2, one sheet per group) add "regions" and "reasons" (the first-pass resolution flags) and "source_group_id" (the pass-1 group that flagged the sheet). Roll-up, main and modify groups have an empty list.';

comment on column plans_reader_groups.pass is
  '1 = first pass (sub groups, the roll-up, the main consolidation); 2 = second pass: an escalation group, or the one retry of a main consolidation that failed for good, made at the run''s reasoning effort one step lower.';

notify pgrst, 'reload schema';
