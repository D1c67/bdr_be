-- 0146: RFP email intake, aligned sender authentication
-- (docs/RFP_EMAIL_INGESTION.md 3.3, security review 2026-09-30).
--
-- The auth step used to pass a message on ANY SPF or DKIM pass, without
-- checking which domain the pass was for. A sender with their own SPF-valid
-- domain and a forged From at a GC domain that publishes no DMARC record was
-- judged authenticated and then authorized as that GC. The verdict now
-- requires an ALIGNED pass (or Exchange's own dmarc / compauth verdict), so
-- the fetch step stores the two domains the passes were judged for and the
-- auth step can re-run the alignment check from the row on a crash resume.
--
-- Applied to the DEV database only; production waits for the release.

alter table rfp_emails
  add column if not exists auth_spf_domain  text,
  add column if not exists auth_dkim_domain text;

comment on column rfp_emails.auth_spf_domain is
  'Domain of smtp.mailfrom in the tenant Authentication-Results header (what the SPF result was judged for), lowercased. Null when absent.';
comment on column rfp_emails.auth_dkim_domain is
  'header.d of the tenant Authentication-Results header (the DKIM signing domain), lowercased. Null when absent or none.';

notify pgrst, 'reload schema';
