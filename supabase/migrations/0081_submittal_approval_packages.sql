-- 0081 — Submittal approval packages (GC-facing)
--
-- 0073 gave a project the vendor-facing direction: we ask each material
-- category's VENDORS to send us their product submittals, and their replies land
-- back through the email-ingestion pipeline. This migration adds the other half
-- of the round trip: we package what we've collected and send it to the GENERAL
-- CONTRACTOR for approval.
--
-- The two are deliberately separate tables rather than a `direction` column on
-- submittal_requests: the vendor side fans out to one email per vendor contact
-- (each needing its own conversation to match a reply), while the GC side is a
-- single email with a To list and a CC list, and it carries an approval verdict
-- the vendor side has no concept of. Sharing a table would leave half the
-- columns null on either side.
--
--   submittal_packages       — one "Request Submittal Approval" send.
--   submittal_package_items  — one row per FILE in that package.
--
-- A package is numbered per project (max+1, like rfis.rfi_number in 0060) so it
-- can be referenced in correspondence: "Submittal Package 003".
--
-- APPROVAL TRACKING. approval_status exists at both grains from day one, so the
-- feature that records the GC's verdict needs no second migration:
--   • package.approval_status — the headline the log badges.
--   • item.approval_status    — per-file, because a GC routinely approves most of
--     a package and rejects one cut sheet. 'partial' is meaningful only on the
--     package (an individual file is approved, rejected, or approved-as-noted);
--     the CHECK constraints differ accordingly.
-- Both default to 'pending'. Nothing writes anything else yet — the send path
-- leaves them pending and the UI badges them read-only.
--
-- COVERAGE. "Which materials have been submitted to the GC for approval" is
-- derived from the items (a category is included iff it has ≥1 item), matching
-- how 0073 derives its vendor-side coverage from submittal_request_items rather
-- than storing a redundant category list.

-- 1. One "Request Submittal Approval" send. ──────────────────────────────────
--    recipients/cc_recipients are denormalized [{contact_id, name, email}] like
--    rfi_sends.recipients (0071): gc_contacts is not append-only, and the record
--    of who received a package must survive a later contact edit or delete.
--
--    conversation_id is captured from the Graph draft for the same reason 0073
--    captures it — a GC reply carries the same conversationId, which is the key
--    the approval-response feature will match on. Nothing reads it yet.
create table submittal_packages (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete cascade,
  number          int not null,                       -- server-assigned: max+1 per project
  gc_id           uuid references general_contractors(id) on delete set null,
  recipients      jsonb not null default '[]'::jsonb, -- To  [{contact_id,name,email}]
  cc_recipients   jsonb not null default '[]'::jsonb, -- CC  [{contact_id,name,email}]
  subject         text,
  message         text,                               -- the sender's optional cover note
  body            text,                               -- the rendered email body, as sent

  -- Delivery. One email, so this is a plain sent/failed — no 'partial'.
  send_status     text not null default 'pending' check (send_status in ('pending', 'sent', 'failed')),
  error           text,
  files_delivery  text check (files_delivery in ('attached', 'onedrive_link')),
  graph_message_id    text,
  conversation_id     text,                           -- Graph conversationId = future reply-matching key
  internet_message_id text,
  email_log_id    uuid references email_log(id) on delete set null,
  pdf_doc_id      uuid references pm_documents(id) on delete set null,  -- archived cover sheet
  sent_at         timestamptz,
  sent_by         uuid references profiles(id) on delete set null,

  -- Approval verdict (see header). Written by the later response feature.
  approval_status text not null default 'pending'
    check (approval_status in ('pending', 'approved', 'partial', 'denied')),
  responded_at    timestamptz,
  response_notes  text,

  created_by      uuid references profiles(id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (project_id, number)
);
create index submittal_packages_project_idx      on submittal_packages(project_id, created_at desc);
create index submittal_packages_conversation_idx on submittal_packages(conversation_id);
create trigger submittal_packages_updated_at before update on submittal_packages
  for each row execute function set_updated_at();

-- 2. One row per file sent. ──────────────────────────────────────────────────
--    `source` names where the file came from, and exactly one pointer column is
--    set to match — see the CHECK below:
--      'vendor_reply' → attachment_id (a vendor's emailed cut sheet, 0061)
--      'bank'         → submittal_file_id (the global Submittal Bank, 0072)
--      'document'     → document_id (an existing pm_documents file, incl. the
--                       project-level uploads 0074 archives there)
--      'upload'       → document_id (uploaded in this modal, archived on send)
--
--    filename/storage_path are SNAPSHOTS so the record of exactly what was sent
--    survives the source row being edited or deleted; every pointer FK is ON
--    DELETE SET NULL for the same reason. category_label snapshots the category
--    name so a renamed or deleted category doesn't rewrite history.
--
--    pm_material_id is the coverage link — set when the file was gathered for a
--    known project material, null for a free-floating upload.
create table submittal_package_items (
  id                   uuid primary key default gen_random_uuid(),
  package_id           uuid not null references submittal_packages(id) on delete cascade,
  material_category_id uuid references material_categories(id) on delete set null,
  category_label       text not null,
  pm_material_id       uuid references pm_materials(id) on delete set null,
  source               text not null check (source in ('vendor_reply', 'bank', 'document', 'upload')),
  attachment_id        uuid references ingested_email_attachments(id) on delete set null,
  submittal_file_id    uuid references submittal_files(id) on delete set null,
  document_id          uuid references pm_documents(id) on delete set null,
  filename             text not null,
  storage_path         text,
  size_bytes           bigint,

  -- Per-file verdict. No 'partial' here: one file is approved, approved with
  -- comments, or rejected. 'partial' belongs to the package alone.
  approval_status      text not null default 'pending'
    check (approval_status in ('pending', 'approved', 'approved_as_noted', 'rejected')),
  responded_at         timestamptz,
  response_notes       text,

  created_at           timestamptz not null default now()
  -- NOTE: there is deliberately no "the pointer matching `source` is not null"
  -- CHECK. It would be correct at insert time but would break the snapshot
  -- guarantee: ON DELETE SET NULL fires as an UPDATE on this row, that UPDATE
  -- re-evaluates the CHECK, and the constraint violation would abort the parent
  -- DELETE — pinning every source row alive just to satisfy a constraint. Since
  -- a null pointer is the intended steady state for deleted sources, the
  -- source→pointer pairing is enforced in the service layer at insert
  -- (submittal_approval._item_row) instead. filename is not-null, so a row whose
  -- pointers have all been nulled still records what was sent.
);
create index submittal_package_items_package_idx  on submittal_package_items(package_id);
create index submittal_package_items_material_idx on submittal_package_items(pm_material_id)
  where pm_material_id is not null;
create index submittal_package_items_category_idx on submittal_package_items(material_category_id)
  where material_category_id is not null;

-- RLS: deny-by-default with no policies, matching 0073/0074. The service-role
-- backend bypasses RLS; every endpoint gates on require_pm_read/require_pm_write.
alter table submittal_packages       enable row level security;
alter table submittal_packages       force  row level security;
alter table submittal_package_items  enable row level security;
alter table submittal_package_items  force  row level security;

notify pgrst, 'reload schema';
