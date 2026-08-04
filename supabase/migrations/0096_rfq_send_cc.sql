-- CC recipients on an RFQ send. Each rfq_sends row is still ONE email to ONE
-- To contact (its Graph conversation stays the reply-matching key); cc_recipients
-- snapshots the same-company contacts copied on that email as
-- [{"vendor_contact_id", "name", "email"}]. Null/absent = no CC.
alter table rfq_sends add column if not exists cc_recipients jsonb;
