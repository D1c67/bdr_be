# Browser v2: release 1.1.0 and live acceptance on dev (runbook)

Owner-facing steps for work item WI-20 of `docs/ASSISTANT_BROWSER_V2_PLAN.md`
(sections 15.5, 15.6, 17.5, 18.4 and 18.5). Owner decisions in
`docs/BROWSER_V2_OWNER_DECISIONS.md` win over anything here.

Rules for the whole runbook:

- Development only: the BDR project `bpidntbyvoooqvaispup`. Never BDR_Prod.
  Every helper below refuses any other project.
- Only the owner signs a worker release (the Ed25519 key on the owner's Mac).
- Never type a password or a one-time code into the chat or a terminal. Logins
  go through the "Browser sites and sign-ins" card or Settings; codes go
  through the code card.
- No command below prints a secret. If one ever does, stop and rotate it.
- The "Personal browser" type (`user_browser`) is enabled only for the
  acceptance window and disabled again at the end (part H).

Who does what: **Owner** = you at the Mac or at shop-pc-1. **Agent** = a Claude
session may run it for you (read-only checks, tests, filling 0194).

Ports: the acceptance helper uses the isolated Phase 6 ports (backend
`localhost:5071`, frontend `localhost:4510`). Your normal dev backend
(`5051`) can keep running; the helper refuses to start if 5071 or 4510 is
busy and never stops anything it did not start.

---

## Part A. Hold signing key in the dev `.env` (owner, once)

Final actions are released by tokens the backend signs with
`BROWSER_HOLD_SIGNING_KEY` (an Ed25519 private seed). The worker checks them
with the public half compiled into release 1.1.0 (part B), so the key must
exist before the release is built.

1. Generate the key straight into `bdr_be/.env`. Nothing is printed except a
   one-line confirmation; the command refuses if a hold key is already there:

   ```sh
   cd ~/Desktop/BDR/bdr_be && uv run python -c "import base64,os,re,pathlib; p=pathlib.Path('.env'); t=p.read_text(); assert not re.search(r'(?m)^BROWSER_HOLD_SIGNING_KEY(_ID)?=', t), 'BROWSER_HOLD_SIGNING_KEY is already in .env; nothing changed'; p.write_text(t.rstrip(chr(10))+chr(10)+'BROWSER_HOLD_SIGNING_KEY='+base64.b64encode(os.urandom(32)).decode()+chr(10)+'BROWSER_HOLD_SIGNING_KEY_ID=h1'+chr(10)); print('Hold signing key written to .env (value not shown).')"
   ```

2. What the config validator (`app/core/config.py`, `_validate_browser_v2`)
   requires, so you know what an error means. Errors name the variable,
   never its value.
   - `BROWSER_HOLD_SIGNING_KEY_ID`: 1 to 20 letters, digits, `_` or `-`
     (default `h1`). Checked even when v2 is off.
   - `ASSISTANT_HOSTED_SEARCH_ENABLED=true` needs `ASSISTANT_ENABLED=true`.
   - When `ASSISTANT_BROWSER_V2_ENABLED=true`:
     - `ASSISTANT_BROWSER_ENABLED` must be true (and that one needs
       `USER_CRED_KEK`, already in your `.env`);
     - `BROWSER_HOLD_SIGNING_KEY` must be base64 (standard or URL-safe,
       padding optional) of exactly 32 bytes;
     - it must differ from `USER_CRED_KEK`, `BROWSER_AGENT_SECRETS_KEY`,
       `BROWSER_AGENT_FINGERPRINT_KEY` and every rotated key in
       `USER_CRED_OLD_KEYS` and `BROWSER_AGENT_SECRETS_OLD_KEYS`;
     - its public half must not equal `BROWSER_AGENT_RELEASE_PUBKEY` (the
       offline release key never lives on the backend).
3. Flags. Leave `ASSISTANT_BROWSER_V2_ENABLED` and
   `ASSISTANT_HOSTED_SEARCH_ENABLED` out of `.env`: the acceptance helper turns
   them on for its own processes only, so your normal dev backend is
   unchanged. If you do want them in `.env` for the normal backend, also set
   `ASSISTANT_BROWSER_ENABLED=true`, or the backend will refuse to start.
4. Check the configuration exactly as the acceptance helper will validate it
   (same flags and overrides; prints error messages only, never a value):

   ```sh
   cd ~/Desktop/BDR/bdr_be && uv run python -c "import sys; sys.path.insert(0, 'scripts'); import start_phase6_credential_setup as s; sys.excepthook = lambda t, v, tb: print(v); s.process_environments(s.FRONTEND, {}); print('browser v2 helper configuration OK')"
   ```

   Expected: `browser v2 helper configuration OK`. Before step 1 it prints
   `BROWSER_HOLD_SIGNING_KEY must be set (base64 of 32 random bytes).`

## Part B. Put the hold public key into the worker and bump the version (owner or agent)

1. Print the public half (safe to show; it is public):

   ```sh
   cd ~/Desktop/BDR/bdr_be && uv run python -c "import base64; from app.services.assistant_browser_holds import public_key_bytes; print(base64.b64encode(public_key_bytes()).decode())"
   ```

2. In `agent_worker/bdr_agent/agents/user_browser_v2/holds.py` replace
   `HOLD_SIGNING_PUBLIC_KEYS: dict[str, str] = {}` with
   `HOLD_SIGNING_PUBLIC_KEYS: dict[str, str] = {"h1": "<the printed value>"}`.
   The key id must equal `BROWSER_HOLD_SIGNING_KEY_ID`. With the map empty
   the worker refuses every release token (fail closed), so no final action
   could ever be approved.
3. Set the version to `1.1.0` in all four places: `__version__` in
   `agent_worker/bdr_agent/__init__.py`, `version` in
   `agent_worker/pyproject.toml`, the `bdr-agent` package entry in
   `agent_worker/uv.lock`, and the pinned literal in
   `agent_worker/tests/test_release_bundle_inputs.py`
   (`test_source_release_version_matches_package_metadata`).
4. Run the gates (all must pass; the backend has 6 to 9 known baseline
   failures listed in plan 20.2 and the wave C build log entry):

   ```sh
   cd ~/Desktop/BDR/bdr_be/agent_worker && .venv/bin/python -m pytest -q
   cd ~/Desktop/BDR/bdr_be && uv run pytest -q -p no:cacheprovider tests/test_agent_protocol_drift.py tests/test_assistant_browser_holds.py
   ```

## Part C. Build, sign and publish 1.1.0 (owner only, on the Mac)

The existing publisher is `scripts/publish_agent_worker_release.py`. It
builds the Windows bundle, signs `{version, sha256, min_version,
created_at}` with the offline key, checks the signature against
`BROWSER_AGENT_RELEASE_PUBKEY`, uploads to the private `browser-agent`
bucket and inserts the release row. It never selects the release.
`--installer-from-settings` re-renders the installer template with the new
launcher (the launcher now copies the stable runtime before each start, which
the firewall rules depend on); that is why shop-pc-1 is re-enrolled in part D.

1. Dry run: builds, signs and verifies; uploads nothing. Enter the key
   passphrase at the prompt.

   ```sh
   cd ~/Desktop/BDR/bdr_be && uv run --with pip --with packaging python scripts/publish_agent_worker_release.py \
     --version 1.1.0 --min-version 1.0.5 \
     --key ~/.bdr/agent_release_ed25519.pem \
     --expected-project bpidntbyvoooqvaispup \
     --output-dir dist/browser-v2-worker-1.1.0 \
     --installer-from-settings --dry-run \
     --notes 'Browser v2 personal worker: any approved site, network-held final actions, per-job Chromium and egress proxy, user pool firewall self-test. Enable the Personal browser type only for the acceptance window.'
   ```

   It prints one JSON line with `version`, `sha256` and `"dry_run": true`.
   Write the size (`ls -l dist/browser-v2-worker-1.1.0/*.zip`) and the sha256
   into the build log.
2. Real publish of the same signed bundle (no rebuild; the saved signature is
   reused, so the key is not opened again). Use the zip name the dry run
   wrote in `dist/browser-v2-worker-1.1.0/`:

   ```sh
   cd ~/Desktop/BDR/bdr_be && uv run --with pip --with packaging python scripts/publish_agent_worker_release.py \
     --version 1.1.0 --min-version 1.0.5 \
     --bundle dist/browser-v2-worker-1.1.0/bdr-agent-1.1.0.zip \
     --key ~/.bdr/agent_release_ed25519.pem \
     --expected-project bpidntbyvoooqvaispup \
     --output-dir dist/browser-v2-worker-1.1.0 --resumable \
     --installer-from-settings \
     --notes 'Browser v2 personal worker: any approved site, network-held final actions, per-job Chromium and egress proxy, user pool firewall self-test. Enable the Personal browser type only for the acceptance window.'
   ```

   Expected last line: `Published. The selected worker version is unchanged; activation is a separate step.`
   `--min-version 1.0.5` keeps the rollback to 1.0.6 (the version selected
   before 1.1.0) or 1.0.5 possible (15.6).
3. Verify the stored row (read only):

   ```sh
   cd ~/Desktop/BDR/bdr_be && uv run python -c "import base64; from app.core.config import get_settings; from app.core.supabase_client import get_supabase; from scripts.publish_agent_worker_release import verify; r = get_supabase().table('browser_agent_worker_releases').select('*').eq('version', '1.1.0').single().execute().data; k = get_settings().browser_agent_release_pubkey.strip(); print(r['version'], r['sha256'], 'signature OK' if verify(base64.b64decode(k + '=' * (-len(k) % 4)), r['manifest'], r['signature']) else 'SIGNATURE BAD')"
   ```

## Part D. Acceptance server, release selection and re-enrollment

1. **Office network (15.5 step 4).** Check whether the office firewall or
   router has an outbound FQDN allowlist for shop-pc-1 (README_WINDOWS.md
   A5). If it does, widen it for that PC to general outbound HTTPS (443) and
   HTTP (80), DNS (UDP and TCP 53) to `1.1.1.1`, `8.8.8.8` and `9.9.9.9`, and
   HTTPS to `1.1.1.1` and `8.8.8.8`. Keep the Playwright download hosts
   reachable. The office LAN is protected on the PC itself (part E).
2. **Start the acceptance server** on the Mac and keep the terminal open:

   ```sh
   cd ~/Desktop/BDR/bdr_be && bash scripts/start_phase6_acceptance.sh
   ```

   It checks (read only) that: the project is dev; at most one personal
   worker row is not revoked and it allows no type other than
   `user_browser`/`user_browser_read`; the `user_browser` type row exists;
   every browser type is disabled; no assistant queue job and no personal
   browser job is open. Then it starts the backend on 5071 with two uvicorn
   workers, the scratch frontend on 4510 and a tunnel, and prints up to three
   PowerShell blocks: reconnect (only if an approved personal worker exists),
   re-enroll, and firewall. For this process only it turns on
   `ASSISTANT_BROWSER_V2_ENABLED`, `ASSISTANT_HOSTED_SEARCH_ENABLED`,
   `TEAMS_ALERTS_ENABLED` (step G3 alert) and `ASSISTANT_OUTLOOK_ENABLED` (the
   email code reader in G4). Everything else unrelated stays off. If it stops
   with "Development configuration refused", go back to part A.
   If the tunnel dies later: `bash scripts/reconnect_phase6_acceptance.sh`
   (new tunnel; prints the reconnect block when one personal worker is
   approved, and always the re-enroll and firewall blocks for the new tunnel
   URL, so it also works between revoking the old worker row and approving
   the re-enrolled one).
3. Open `http://localhost:4510/settings/browser-agents` and sign in.
4. **Select 1.1.0** in the Release section (the API is
   `PATCH /browser-agents/admin/settings {"desired_worker_version": "1.1.0"}`).
   Only now: the preflight just proved no personal job is running or waiting
   (15.5 step 6). This also upgrades the organization worker (one desired
   version for both pools); the org RFI agent stays disabled on dev.
5. **Revoke the old personal worker row** (`e0846bf6-...`, pool Personal
   tasks) and click **Add worker** with pool **Personal tasks** for a new
   enrollment code. Keep the code in the BDR dialog; do not paste it anywhere
   else.
6. **On shop-pc-1**, in Windows PowerShell as Administrator, paste the
   re-enroll block the helper printed. It downloads the installer through the
   tunnel and runs it with `-Pool user -Reenroll -PythonPath 'C:\Program
   Files\BDR Python 3.12.10\tools\python.exe' -ApiBase <tunnel>`. Compare the
   release key fingerprint with the password manager entry, type `yes`, then
   type the code when asked. Why a re-enroll: only the installer replaces the
   launcher, and the 1.1.0 launcher copies the driver and Chromium to the
   stable paths the firewall rules name. It creates a new worker row.
7. In Settings > Browser agents: **Approve** the new personal worker, then
   **Edit** it and set allowed types to **Personal browser** only (plan 15.5
   step 7). API equivalent:
   `PATCH /browser-agents/admin/workers/<new worker id> {"allowed_types": ["user_browser"]}`.
8. Check the worker row: online, version `1.1.0`, heartbeat within a minute,
   reported types include `user_browser` (not `user_browser_read`).

## Part E. Firewall rules and the boot self-test (owner at shop-pc-1)

1. When the worker shows 1.1.0 online, paste the **firewall block** the
   helper printed into an elevated PowerShell. It downloads
   `agent_worker/install/apply_user_pool_firewall.ps1` through the tunnel,
   checks its SHA-256 against the Mac copy, and runs it. If the office has
   more public addresses or a known IPv6 site prefix, run the downloaded
   script again with `-OfficeBlockedRanges 203.0.113.7,2001:db8:1200::/48`
   (your values).
2. What it should report: the rule group "BDR user pool floor" created for
   `python.exe`, `C:\ProgramData\BDRAgentUser\runtime\current\driver\node.exe`
   and `...\runtime\current\chrome\chrome.exe`; `firewall\floor.json`
   written; quarantine `C:\ProgramData\BDRQuarantine` created; the SYSTEM task
   "BDR Agent User Firewall" registered.
3. Restart the service so the boot self-test runs against the new rules:
   `Restart-Service BDRAgentUser`.
4. In `C:\ProgramData\BDRAgentUser\logs\worker.log` look for
   `user pool self-test: ok`. Anything else names the failed check (rules,
   python, node, chromium, defender); fix it and restart again. With a
   failed floor the worker keeps heartbeating but claims no personal task.
5. Windows Security shows a "Threat found: EICAR test file" entry after the
   restart. That is the Defender check passing (README_WINDOWS.md A6).
6. Confirm `C:\ProgramData\BDRAgentUser\runtime\current\driver\node.exe` and
   `...\chrome\chrome.exe` exist (the stable runtime copy).

## Part F. Model probe, the window, and the curated-profile probes

1. **Audited model probe (15.5 step 8).** Settings > Browser agents > Agent
   types > **Personal browser** > **Test model** with provider `openai`, model
   `gpt-5.4` (the type row's model). This writes an audit row and stamps
   `model_probe_ok_at`. API equivalent:
   `POST /browser-agents/admin/types/user_browser/probe {"provider": "openai", "model": "gpt-5.4"}`.
2. **Enable** the Personal browser type (`PATCH
   /browser-agents/admin/types/user_browser {"enabled": true}`). This opens the
   acceptance window. Note the time; it is the `--since` value below.
3. **Probe task, Procore** (in a normal assistant chat at
   `http://localhost:4510`):
   1. Ask: "Open Procore and list the open RFIs on <a real project>." Approve
      `procore` on the site card. Save your personal Procore login on the
      sign-in card (never in chat). If Procore asks for a code, use the code
      card.
   2. Read-style requests (search, filters, list loads) raise final-action
      cards because Procore has no profile yet. For each one that clearly is
      not a final action, click **This was not a final action** (it creates
      your own safe rule, which the probe collects), then **Approve**.
   3. Start an RFI response draft and press Submit; on that card click
      **Decline**. Do the same for any other real final action you can reach
      without changing anything (Decline every time).
   4. Let the task finish.
4. **Probe task, Autodesk**: the same with "Open Autodesk Construction Cloud
   and list the files in <a project>", approving `autodesk`, saving the
   Autodesk login, using the emailed code (step 6 checks the mail) or the
   code card.

   **If the Autodesk sign-in page is not `https://accounts.autodesk.com`**
   (the 0192 seed): `fill_credential` refuses on the real page, so this probe
   cannot sign in, and the full 0194 cannot run yet (it refuses while any
   TODO remains). Recover in this order:
   1. Note the real sign-in origin from the summary warning ("fill_credential
      was refused on ...") after step 5.
   2. With your approval, apply on dev only a one-statement fix for that
      origin, through the Supabase connector:
      `update public.assistant_browser_sites set login_origins = array['https://<real host>'], updated_at = now() where site_key in ('autodesk', 'buildingconnected') and kind = 'curated';`
      then the same origins on the saved logins:
      `update public.assistant_credentials c set login_origins = t.login_origins, origins = t.login_origins, cookie_domains = t.cookie_domains, updated_at = now() from public.assistant_browser_sites t where t.site_key = c.site_key and c.site_key in ('autodesk', 'buildingconnected') and c.status = 'active';`
   3. Run this Autodesk probe task again; it now signs in. Put the same
      origin in 0194's `autodesk` entry (0194 re-applies it, harmless).
5. **Summarize** each site (read only; prints no code, body, password or
   query). Use your profile id `5cfaaee6-0f1b-4f19-b9f9-7912dfc82bf3` and the
   window start time with its zone:

   ```sh
   cd ~/Desktop/BDR/bdr_be
   uv run python scripts/browser_v2_probe_site.py summarize --site procore --owner 5cfaaee6-0f1b-4f19-b9f9-7912dfc82bf3 --since 2026-11-03T08:00:00-08:00 --mail
   uv run python scripts/browser_v2_probe_site.py summarize --site autodesk --owner 5cfaaee6-0f1b-4f19-b9f9-7912dfc82bf3 --since 2026-11-03T08:00:00-08:00 --mail
   ```

   With `--mail` the script turns on `ASSISTANT_ENABLED` and
   `ASSISTANT_OUTLOOK_ENABLED` for its own process only (your `.env` keeps
   them off); if the mailbox still cannot be read it prints a warning with a
   short code and the rest of the summary. Procore's listing also includes
   `procoretech.com`; add any other sender domain with `--sender <domain>`
   (repeatable). Only a DKIM-aligned sender is added to the proposed
   `code_email_senders`, next to the ones the row already has.

   Safe rules are rebuilt from the held request of this run (word segments
   such as `bulk_filter_preferences` stay exact). A rule whose held request
   is not in the run and whose pattern has a generalized segment is not
   proposed; a warning names it for a manual look.

   It lists: the login form origins and the login post paths
   (`login.exempt` steps), pages seen, posts that went to other domains,
   WebSockets, holds by status, your safe rules and IT Admin suggestions,
   declined or approved final actions with their generalized paths, click
   holds on links (`final_get` candidates), and with `--mail` each code email
   in your own mailbox: sender domain, DKIM alignment, whether it was
   addressed to you, the subject with every digit shown as `#`, and how many
   codes the site regex and the generic pattern find (exactly 1 is right).
   Warnings call out GraphQL endpoints, a refused `fill_credential` page
   (the real sign-in origin is missing from `login_origins`), and rules
   that name a final-action verb (never proposed as safe).
6. **Fill 0194.** Paste each printed `migration_entry` over that site's TODO
   entry in `supabase/migrations/0194_assistant_browser_curated_profiles.sql`.
   Replace every remaining TODO by hand: action labels ("Submit RFI
   response"), `logged_in_probe` (a path and a CSS selector that only shows
   when signed in, or `null`), and `code_email_regex` (leave the key out to
   keep the 0192 A5.6 regex when `site_regex_codes` was 1 on the real code
   email; otherwise write a regex anchored with `\A` at the subject start
   that captures exactly the code; JSON needs doubled backslashes). Add
   `procoretech.com` to Procore's `code_email_senders` only if the code email
   itself came from it (the summary then shows it with `dkim_aligned: true`,
   and the printed entry already has it). The `smartbid` entry is already
   filled from the codebase (domains and sign-in origins); leave it.
7. **Check offline:**

   ```sh
   cd ~/Desktop/BDR/bdr_be && uv run python scripts/browser_v2_probe_site.py check-migration supabase/migrations/0194_assistant_browser_curated_profiles.sql
   ```

   Expected: `OK: the profiles block can be applied.` The migration itself
   refuses to run while any TODO remains, and refuses unknown keys, safe
   mutations that name a final-action verb, and `websocket_ok` without a
   reason.
8. **Apply 0194 on dev only**, with your approval, through the Supabase
   connector on project `bpidntbyvoooqvaispup`, ledger name
   `0194_assistant_browser_curated_profiles`. It is one DO block of updates
   (no destructive text). It also copies each changed site's
   `login_origins` and `cookie_domains` onto your active saved logins of
   that site: a login is checked against the origins copied when it was
   saved, so the logins saved during the probes would otherwise keep the
   0192 values. Check: `select site_key, login_origins,
   login_paths, jsonb_array_length(profile->'safe_mutations'),
   jsonb_array_length(profile->'final_actions') from
   assistant_browser_sites where kind = 'curated';` and
   `select site_key, login_origins from assistant_credentials where status = 'active';`
9. Delete the probe-made safe rules you no longer need in Settings >
   Browser sites and sign-ins (they are per user; the curated profile now
   covers them). Optional.

## Part G. Live acceptance (plan 18.4), owner present

Preconditions (all from parts A to F): 1.1.0 selected and running; office
allowlist checked; firewall applied and `user pool self-test: ok`; 0194
applied; `user_browser` enabled for the window only; the helper running
(two uvicorn workers, hosted search on). A second test user is needed for
G6.

1. **LAN floor.** Ask for a task that opens `http://192.168.1.1/`, then a
   static private-resolving name (`http://192.168.1.1.nip.io/`, always
   192.168.1.1), then a real DNS rebinding host whose answers switch between
   a public and a private address, for example
   `http://01010101.c0a80101.rbndr.us/` (1.1.1.1 and 192.168.1.1; ask for it
   three or four times in a row, since each lookup may give either address),
   then `http://<printer>.local/`. Check: every attempt is refused (the chat
   says the destination is unavailable; no site card offers it), including
   the rebinding attempts that resolved public first; the router's log
   shows no connection from shop-pc-1 to those addresses.
2. **Procore read.** New chat. Approve `procore` (if asked), sign in through
   the card if no session is saved, code card if asked. Ask for the RFI list
   of a real project. Check: the result arrives in the chat with facts and
   source links on procore.com; no password or code appears anywhere in the
   chat.
3. **Procore final action.** Start drafting an RFI response and press
   Submit; close the BDR tab. Check: a Teams alert arrives; reopen the chat:
   the card shows the screenshot, "POST to <full URL>", the field summary
   (values you did not type show `[hidden]`) and the profile label from 0194;
   **Decline**; check in Procore that nothing was submitted.
4. **Autodesk download.** Approve `autodesk`; sign in through the card; the
   emailed code is read by the server (chat shows "Code received from email")
   or use the code card. Open a project's Files and download one PDF into a
   TS-* project. Check: the file appears in BDR with category `other` and
   the origin note; the chat result names it.
5. **New site mid-task.** In the Autodesk task, follow a link to a domain
   outside the scope. Check: the site card appears with the target origin in
   bold and the full URL; **Approve**; the exact navigation continues.
6. **Concurrency.** Start two tasks for you on Procore at once and one for a
   second test user. Check: the second Procore task reuses the first one's
   session (no second login); all three run; note RAM on shop-pc-1 and the
   heartbeat latency in Settings; a third concurrent task for you is refused
   or queued by the per-owner limit.
7. **Cost.** Run a long task until it stops at the 5 USD cap (or your daily
   cap if it comes first). Check: the task ends with the cost-cap message;
   chat turns still worked during it; Settings shows the reservation steps
   settled and the org daily total includes the turns.
8. **Revoke.** While a task runs, revoke `autodesk` in Settings > Browser
   sites and sign-ins. Check: the task stops at its next step with a
   "site not approved" style message; queued jobs for it are cancelled.
9. **Taint.** After a browser result in a chat, ask for a write (for example
   a note on a TS-* project) with values you type. Check: the write card
   works. Then click **Continue in a new chat**: the new chat is untainted
   (hosted web search is offered again).
10. **Interrupted task.** While a task waits on a final-action card, run
    `Restart-Service BDRAgentUser` on shop-pc-1. Check: the card shows "The
    task stopped before you answered" and a chat message says what happened.
    A task that already had a released action is not re-run.

Step 11 (Microsoft sign-in through the live view) belongs to wave E
(release 1.2.0) and is not part of this window.

## Part H. Close the window

1. Disable the Personal browser type in Settings (the window ends).
2. Secret sweep (18.5), read only, on dev: search message rows, tool result
   rows (mail included), worker logs and the helper's protected logs
   (`dist/phase6-sessions/...`) for the test passwords and the codes you
   used. Expected: no hit. Run it yourself or give an agent the strings in a
   local file it reads but never prints.
3. Stop the helper with Ctrl+C (it stops only what it started).
4. Build log (`docs/ASSISTANT_BUILD_LOG.md`): release 1.1.0 (sha256, size,
   publish time), re-enrollment (new worker id), firewall and self-test
   result, 0194 applied (ledger timestamp), each acceptance step with
   pass or fail, and the open items.

## Rollback (plan 15.6)

The desired version is one setting for both pools. In order: disable the
Personal browser type; cancel queued and waiting personal jobs (the cancel
path settles reservations and aborts holds); select 1.0.6 (the version
selected on dev before 1.1.0; 1.0.5 also works) in the Release
section; rerun the org `rfi_from_email` smoke test; record it in the build
log. The firewall rules can stay (they only affect the personal pool); to
remove them run the firewall script with `-Remove`.

---

## Seeded login origins in 0192: re-verification against the codebase

Checked on 2026-11-02 against the existing harvesters and docs. "Confirmed"
means the codebase shows it; everything else is confirmed only by the part F
probes.

| Site | 0192 seed | Codebase evidence | Verdict |
|---|---|---|---|
| procore | domains `procore.com`; login `https://login.procore.com`; cookies `.procore.com`; senders `procore.com` | `app/services/procore_client.py`: `LOGIN_HOST = "login.procore.com"`, form posts `/sessions/submit_login_email` and `/sessions/submit_login_password`, app hosts `app.procore.com` and `us02.procore.com` | Login origin confirmed. Login paths are not seeded (0194 adds them). Procore notification mail also comes from `procoretech.com` (docs/browser_agents/CODEBASE_MAP.md); whether the MFA code email does is unknown, so `procore.com` alone may miss it. |
| autodesk | domains `autodesk.com`; login `https://accounts.autodesk.com`; senders `autodesk.com` | Only the APS API hosts appear (`developer.api.autodesk.com/authentication/v2/authorize` in `app/services/bc_client.py`); no browser sign-in host anywhere in the code | Not confirmable from the codebase. If the probe shows `fill_credential` refused on another Autodesk sign-in host (for example `signin.autodesk.com`), put that origin in 0194. Cookie domain `.autodesk.com` covers either. |
| buildingconnected | domains `buildingconnected.com`, `autodesk.com`; login `https://accounts.autodesk.com`; cookies `.buildingconnected.com`, `.autodesk.com` | App host `app.buildingconnected.com` (bc_client.py, RFP_BUILDINGCONNECTED.md); API auth is Autodesk APS 3-legged OAuth, consistent with Autodesk ID sign-in | Plausible, same open question as autodesk. Two side effects to know: a BuildingConnected grant covers all of `autodesk.com` (ACC included), and the same Autodesk ID saved for both sites triggers the password-reuse acknowledgement. `prefer_api` already points at the APS integration. |
| smartbid | domains `smartbidnet.com`; login `https://www.smartbidnet.com`; cookies `.smartbidnet.com`; login kind `password` | `app/services/smartbid_client.py` and `docs/RFP_SMARTBID.md`: emails link to `securecc.smartbidnet.com/Main/Login.aspx`, which redirects to the SPA `https://gocc.smartbid.co`; the SPA logs in with `POST https://apicc.smartinsight.co/token`; files come from `apicc.smartbidnet.com` and `*.blob.core.windows.net` | **Mismatch.** `www.smartbidnet.com` never appears; the sign-in page is on `smartbid.co` and the token post goes to `smartinsight.co`, neither in the seed's domains. As seeded, a SmartBid task would need extra site cards and `fill_credential` would refuse the real sign-in page. **Fixed in 0194** (the `smartbid` entry, no probe needed for the domains): `registrable_domains` `smartbidnet.com`, `smartbid.co`, `smartinsight.co`; `login_origins` `https://gocc.smartbid.co` and `https://apicc.smartinsight.co`; `cookie_domains` add `.smartbid.co`; `login_paths` left unset, so only the one post carrying the filled login is exempt. `CURATED_DOMAINS["smartbid"]` and the lookalike list in `app/services/assistant_browser_sites.py` now own the same domains, so the real sign-in page is no longer flagged as a lookalike. A SmartBid probe (profile rules, `^/token$` as a login path) is still a later item. |

## Files that support this runbook

- `scripts/start_phase6_credential_setup.py` (+ `.sh` wrappers
  `start_phase6_credential_setup.sh`, `start_phase6_acceptance.sh`): the
  isolated v2 servers, preflight, reconnect, re-enroll and firewall blocks.
- `scripts/phase6_acceptance_app.py`: the acceptance lifespan (assistant
  queue, personal sweeps, v2 sweep and chat outbox; serves the reconnect and
  firewall scripts).
- `scripts/reconnect_phase6_acceptance.py` (+ `.sh`): replacement tunnel.
- `scripts/phase6_reconnect_personal_worker.ps1`: takes `-ExpectedWorkerId`.
- `scripts/browser_v2_probe_site.py`: the read-only probe summary and the
  offline 0194 check.
- `supabase/migrations/0194_assistant_browser_curated_profiles.sql`: the
  curated-profile draft (refuses to run while any TODO remains).
