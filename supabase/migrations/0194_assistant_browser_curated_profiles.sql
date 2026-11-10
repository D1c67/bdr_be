-- 0194: Curated browser site profiles from the WI-20 live probes
-- (docs/ASSISTANT_BROWSER_V2_PLAN.md 6.3, 6.4, 8.3, 17.5; runbook
-- docs/BROWSER_V2_RELEASE_RUNBOOK.md part F). Dev project (BDR) only.
-- Ledger name: 0194_assistant_browser_curated_profiles.
--
-- DRAFT: the values below marked TODO come from the live probe tasks and
-- scripts/browser_v2_probe_site.py. The DO block refuses to run while any
-- TODO remains, so applying this file early changes nothing. Fill it in:
--   1. uv run python scripts/browser_v2_probe_site.py summarize --site procore --owner <id> --since <time> --mail
--      (and the same for autodesk); paste each printed entry over that site's TODO entry.
--   2. uv run python scripts/browser_v2_probe_site.py check-migration supabase/migrations/0194_assistant_browser_curated_profiles.sql
--      must print "OK: the profiles block can be applied."
--   3. Apply on dev only, with the owner's approval. Never BDR_Prod.
--
-- Semantics: one JSON object keyed by curated site_key. A key present in a
-- site's entry replaces that column (profile replaces the whole profile; a
-- null code_email_regex clears it); a key left out keeps the 0192 value.
-- Allowed keys: registrable_domains, exact_origins, login_origins,
-- login_paths, cookie_domains, code_email_senders, code_email_regex, profile,
-- needs_headed. Profile keys: safe_mutations, final_actions, final_get,
-- websocket_ok, websocket_reason, logged_in_probe (protocol.UserSiteProfile).
-- JSON strings need doubled backslashes (a regex \d is written "\\d").
--
-- Hints already known from the codebase (confirm in the probe, do not trust):
--   procore login form posts (app/services/procore_client.py _LOGIN_LOGIN_PATHS):
--     ^/sessions/submit_login_email$ and ^/sessions/submit_login_password$ on https://login.procore.com
--   procore notification mail also comes from procoretech.com (docs/browser_agents/CODEBASE_MAP.md);
--     add it to code_email_senders only if the MFA code email itself comes from it.
--   autodesk sign-in origin: 0192 seeds https://accounts.autodesk.com (not confirmed in the codebase;
--     if the probe shows another Autodesk sign-in host, put that origin here; active saved logins of
--     a site get the new login_origins and cookie_domains in the same block).
--   smartbid: the 0192 seed (www.smartbidnet.com) is wrong. The sign-in page is https://gocc.smartbid.co
--     and its token API is apicc.smartinsight.co (app/services/smartbid_client.py SPA_ORIGIN, API_HOST);
--     the smartbid entry below corrects the domains and the sign-in origins (no probe needed for that).
--     The SPA posts the password to apicc.smartinsight.co/token, so that origin is a login origin too;
--     login_paths stay unset, so the worker exempts only the one post that carries the filled login.
--   code_email_regex: leave the key out to keep the 0192 A5.6 regex, or paste a regex tested by
--     the probe's --mail output (site_regex_codes must be 1 on a real code email).
-- No functions, no destructive statements; besides the site rows, only the
-- copied origins of active saved logins change (see the loop).

do $migration$
declare
 v jsonb := $profiles$
{
  "procore": {
    "login_origins": ["https://login.procore.com"],
    "login_paths": ["TODO: from the probe (expected ^/sessions/submit_login_email$ and ^/sessions/submit_login_password$)"],
    "code_email_senders": ["procore.com"],
    "code_email_regex": "TODO: leave this key out to keep the 0192 A5.6 regex, or paste a regex the probe --mail output confirmed",
    "profile": {
      "safe_mutations": ["TODO: paste proposed.profile.safe_mutations from the procore probe"],
      "final_actions": ["TODO: paste proposed.profile.final_actions from the procore probe"],
      "final_get": ["TODO: paste proposed.profile.final_get from the procore probe (or [])"],
      "websocket_ok": false,
      "websocket_reason": null,
      "logged_in_probe": "TODO: {\"path\": \"/...\", \"selector\": \"...\"} shown only when signed in, or null"
    }
  },
  "autodesk": {
    "login_origins": ["TODO: the Autodesk sign-in origin the probe recorded (0192 seed: https://accounts.autodesk.com)"],
    "login_paths": ["TODO: from the probe"],
    "code_email_senders": ["autodesk.com"],
    "code_email_regex": "TODO: leave this key out to keep the 0192 A5.6 regex, or paste a regex the probe --mail output confirmed",
    "profile": {
      "safe_mutations": ["TODO: paste proposed.profile.safe_mutations from the autodesk probe"],
      "final_actions": ["TODO: paste proposed.profile.final_actions from the autodesk probe"],
      "final_get": ["TODO: paste proposed.profile.final_get from the autodesk probe (or [])"],
      "websocket_ok": false,
      "websocket_reason": null,
      "logged_in_probe": "TODO: {\"path\": \"/...\", \"selector\": \"...\"} shown only when signed in, or null"
    }
  },
  "smartbid": {
    "registrable_domains": ["smartbidnet.com", "smartbid.co", "smartinsight.co"],
    "login_origins": ["https://gocc.smartbid.co", "https://apicc.smartinsight.co"],
    "cookie_domains": [".smartbidnet.com", ".smartbid.co"]
  }
}
$profiles$::jsonb;
 k text;
 s jsonb;
 p jsonb;
 n int;
 allowed text[] := array['registrable_domains','exact_origins','login_origins','login_paths','cookie_domains',
  'code_email_senders','code_email_regex','profile','needs_headed'];
 profile_keys text[] := array['safe_mutations','final_actions','final_get','websocket_ok','websocket_reason',
  'logged_in_probe'];
 verbs text := '(submit|send|accept|declin|delet|remov|pay|approv|answer|respond|sign|award|confirm)';
begin
 if v::text ~ 'TODO' then
  raise exception '0194 still has TODO values: fill them from scripts/browser_v2_probe_site.py, then run its check-migration';
 end if;
 for k, s in select e.key, e.value from jsonb_each(v) e loop
  if jsonb_typeof(s) <> 'object' then raise exception '0194: % must be an object', k; end if;
  if exists (select 1 from jsonb_object_keys(s) x where x <> all(allowed)) then
   raise exception '0194: % has a key outside %', k, allowed;
  end if;
  if exists (select 1 from jsonb_each(s) e where e.key <> all(array['code_email_regex','profile','needs_headed'])
             and jsonb_typeof(e.value) <> 'array') then
   raise exception '0194: % list values must be JSON arrays', k;
  end if;
  if s ? 'login_origins' and exists (select 1 from jsonb_array_elements_text(s->'login_origins') o
             where o !~ '^https://[a-z0-9.-]+(:[0-9]{1,5})?$' or public.assistant_browser_v2_is_idp(o)) then
   raise exception '0194: % login_origins must be https origins and never an identity provider', k;
  end if;
  p := s->'profile';
  if p is not null then
   if jsonb_typeof(p) <> 'object' or exists (select 1 from jsonb_object_keys(p) x where x <> all(profile_keys)) then
    raise exception '0194: % profile keys must be among %', k, profile_keys;
   end if;
   if exists (select 1 from jsonb_array_elements(coalesce(p->'safe_mutations', '[]'::jsonb)) r
              where (r->>'path_regex') ~* verbs
                 or exists (select 1 from jsonb_array_elements_text(coalesce(r->'graphql_operations', '[]'::jsonb)) o
                            where o ~* verbs)) then
    raise exception '0194: a % safe mutation names a final-action verb', k;
   end if;
   if coalesce((p->>'websocket_ok')::boolean, false) and coalesce(p->>'websocket_reason', '') = '' then
    raise exception '0194: % websocket_ok needs websocket_reason', k;
   end if;
  end if;
  update public.assistant_browser_sites t set
   registrable_domains = case when s ? 'registrable_domains'
    then array(select jsonb_array_elements_text(s->'registrable_domains')) else t.registrable_domains end,
   exact_origins = case when s ? 'exact_origins'
    then array(select jsonb_array_elements_text(s->'exact_origins')) else t.exact_origins end,
   login_origins = case when s ? 'login_origins'
    then array(select jsonb_array_elements_text(s->'login_origins')) else t.login_origins end,
   login_paths = case when s ? 'login_paths'
    then array(select jsonb_array_elements_text(s->'login_paths')) else t.login_paths end,
   cookie_domains = case when s ? 'cookie_domains'
    then array(select jsonb_array_elements_text(s->'cookie_domains')) else t.cookie_domains end,
   code_email_senders = case when s ? 'code_email_senders'
    then array(select jsonb_array_elements_text(s->'code_email_senders')) else t.code_email_senders end,
   code_email_regex = case when s ? 'code_email_regex' then s->>'code_email_regex' else t.code_email_regex end,
   profile = case when s ? 'profile' then s->'profile' else t.profile end,
   needs_headed = case when s ? 'needs_headed' then (s->>'needs_headed')::boolean else t.needs_headed end,
   updated_at = now()
  where t.site_key = k and t.kind = 'curated';
  get diagnostics n = row_count;
  if n <> 1 then raise exception '0194: curated site % not found', k; end if;
  -- A saved login copies the site's login_origins and cookie_domains when it
  -- is saved (0192 credential_put_v2), and sign-in checks that copy. Bring
  -- every active saved login of this site to the new values, so a login
  -- saved during the probes (before 0194) fills on the corrected origin and
  -- never on a removed one. Encrypted material is unchanged: no version bump.
  if s ? 'login_origins' or s ? 'cookie_domains' then
   update public.assistant_credentials c set login_origins = t.login_origins, origins = t.login_origins,
    cookie_domains = t.cookie_domains, updated_at = now()
   from public.assistant_browser_sites t
   where t.site_key = k and c.site_key = k and c.status = 'active';
  end if;
 end loop;
end
$migration$;
