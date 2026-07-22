-- 0070 — RFI close: record who answered, and keep answer documents distinct
-- from the request's own supporting attachments.
--
-- Closing an RFI becomes a first-class, gated action (routers/pm_field.py): it
-- requires the responder, the answer text, and at least one response document.
-- That is stronger than the answer→answered convenience added around 0060,
-- which only notes that *some* answer was typed in.
--
--   1. rfis.answered_by — free text: who supplied the answer (a GC PM, the
--      engineer of record, an owner rep). Fills the RFI form's "Response by"
--      line, which had no backing column before. Free text for the same reason
--      asked_of is — the responder often isn't a record we hold.
--   2. rfi_attachments.kind — 'question' (the request's supporting files, the
--      only kind that existed before) or 'answer' (the response document(s)
--      captured at close). Existing rows are request attachments, hence the
--      default. The uniqueness key gains `kind` so the same document may serve
--      as both a request exhibit and the answer without colliding.

alter table rfis
  add column answered_by text;

alter table rfi_attachments
  add column kind text not null default 'question'
    check (kind in ('question', 'answer'));

alter table rfi_attachments
  drop constraint rfi_attachments_rfi_id_doc_key_key,
  add  constraint rfi_attachments_rfi_id_doc_key_kind_key unique (rfi_id, doc_key, kind);

notify pgrst, 'reload schema';
