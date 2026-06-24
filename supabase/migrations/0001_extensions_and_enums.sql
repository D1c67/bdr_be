-- 0001 — Extensions and enum types
-- BDR bidding-process schema for G3 Electrical.

create extension if not exists pgcrypto;      -- gen_random_uuid()

-- Application roles.
create type role as enum (
  'pm', 'pe', 'pa', 'executive', 'accountant', 'it_admin', 'estimator'
);

-- Workflow stages (the 10-step pipeline + terminal states).
create type project_stage as enum (
  'intake',
  'go_no_go',
  'to_estimator',
  'estimate_received',
  'rfqs',
  'receive_quotes',
  'labor_numbers',
  'markup',
  'verify',
  'send_out',
  'submitted',   -- terminal: bid sent to GC
  'declined'     -- terminal: Go/No-Go declined
);

create type labor_type as enum (
  'prevailing_wage', 'non_prevailing_wage', 'night_work', 'other'
);

create type gc_relationship as enum ('invited_us', 'bidding_to');

create type material_kind as enum ('material', 'markup');

create type file_category as enum (
  'drawing', 'estimate', 'boq', 'markup', 'rfq_split', 'quote', 'other'
);

create type vote_choice as enum ('go', 'no_go');

create type gono_method as enum ('majority', 'override');

create type rfq_status as enum ('draft', 'sent', 'quotes_in', 'closed');

create type email_status as enum ('queued', 'sent', 'failed');
