-- 0071 — RFI send: how an RFI reached the GC, and a durable record of what went out.
--
-- 0068 grew the RFI log and 0070 gave it a gated close; the send flow those left
-- "deliberately untouched" lands here. An RFI can leave G3 two ways:
--
--   1. via the app — BDR renders the RFI as a filled PDF (identical to the in-app
--      "View RFI" form), archives that PDF in the project's Documents as the record
--      of exactly what was sent, and emails it to chosen GC contacts.
--   2. via Procore/Autodesk — the PM sent it outside BDR and only records that it
--      went out, so the log's send status stays truthful without a second email.
--
--   * send_status — the single value the RFI log badges. 'sent_external' pairs with
--     sent_via to name the platform; 'sent_app' leaves sent_via null.
--   * sent_via — CHECK-constrained (no enum: the platform list is more likely to
--     grow than the status set, and a text+CHECK is cheaper to extend than an enum).
--   * last_sent_at / last_sent_by — denormalized onto rfis so the log and badges need
--     no join; rfi_sends is the full history behind them.
--
-- sent_at (the form's "date requested/sent") is a separate, user-editable date and is
-- left alone here; the router only fills it from a send when it was still blank.

create type rfi_send_status as enum ('not_sent', 'sent_app', 'sent_external');
create type rfi_send_method as enum ('app', 'procore', 'autodesk');

alter table rfis
  add column send_status  rfi_send_status not null default 'not_sent',
  add column sent_via      text,
  add column last_sent_at  timestamptz,
  add column last_sent_by  uuid references profiles(id) on delete set null,
  add constraint rfis_sent_via_chk
    check (sent_via is null or sent_via in ('procore', 'autodesk'));

-- The send log: one row per send action (an app email or an external mark).
--   * recipients — denormalized [{contact_id, name, email}] for app sends, so the
--     record survives a later contact edit/delete (gc_contacts isn't append-only).
--   * pdf_doc_id — the archived RFI PDF in pm_documents (app sends only). ON DELETE
--     SET NULL: purging the document from the hub must not erase the send happened.
create table rfi_sends (
  id          uuid primary key default gen_random_uuid(),
  rfi_id      uuid not null references rfis(id) on delete cascade,
  method      rfi_send_method not null,
  message     text,
  recipients  jsonb not null default '[]'::jsonb,
  pdf_doc_id  uuid references pm_documents(id) on delete set null,
  sent_by     uuid references profiles(id) on delete set null,
  sent_at     timestamptz not null default now()
);
create index rfi_sends_rfi_idx on rfi_sends(rfi_id);

-- Deny-by-default like every table: the API reaches these through the service role
-- (which bypasses RLS); any other credential is denied every row (see 0007/0055).
alter table rfi_sends enable row level security;
alter table rfi_sends force  row level security;

notify pgrst, 'reload schema';
