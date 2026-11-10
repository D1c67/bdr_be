# Assistant browser v2: owner decisions (2026-10-31)

Source of truth for the browser redesign. Overrides docs/ASSISTANT_PHASE6_PLAN.md
wherever they disagree. Build waits until Phase 7 wave C is done. Dev only; prod is
not a concern yet (build branch).

## Goal (owner's words, condensed)

Give the assistant far more freedom with the web browser. The assistant should know
it can use the browser whenever it needs to get information and ordinary AI web
search is not viable: sites behind logins such as Autodesk Construction Cloud and
Procore, where traffic must come from a real computer. Not locked to Procore. The
assistant can request credentials from the user in chat, and the application handles
and stores them securely.

## Decisions

1. Actions: the browser can act (click, type, fill forms, submit), not read-only.
2. Final actions: once a site is approved, the assistant still asks the user for a
   click before final irreversible actions (submit, send, accept or decline a bid,
   answer an RFI, delete, pay). Browsing, reading, filling forms and downloading do
   not need a click.
3. Sites: any website it needs, but with user permission.
4. Approval granularity: approve a site once (per user), not per task.
5. One-time codes (MFA): the assistant can ask the user for the code in chat and the
   user returns it; the assistant also has email access and may read emailed codes.
6. Search: finding information uses the assistant's ordinary web search, not the
   browser. Opening links from emails needs the user's permission.
7. Downloads: allowed; files go into the BDR project they were downloaded for.
8. Cost: 5 USD maximum per task; browser spend counts toward the daily budget. Keep
   the company browser budget at 25 USD per day (editable later in Settings).
9. Concurrency: several tasks may run at the same time on the one worker PC.
10. Several logins per site per user are allowed.
11. SSO (Sign in with Microsoft or Google): always try the site's own username and
    password first; if that is not possible, the user signs in once themselves.
12. Terms of service: owner accepts the account risk; prefer official APIs where BDR
    already has one (BuildingConnected via Autodesk APS, Procore), browser otherwise.

## Owner acknowledgements

- Agreed risks to design against: prompt injection from arbitrary pages (data theft
  once the origin allowlist is gone) and the office LAN (block private networks).
- Not a concern: all users sharing the office IP address.
- Low concern: personal login colliding with the org Procore account (a warning when
  saving that account is enough).
- Encryption key loss is low impact (users can re-enter logins); leak is the real
  risk, so the key stays backend-only and apart from backups.

## Answers to plan section 22 (2026-11-02)

1. Automatic follow-up when a task finishes: yes (one text-only call per task, skipped if a newer user message exists).
2. Shared 25 USD daily browser pool with the RFI agent: ok, one shared pool for now; revisit before prod.
3. Live-view sign-in keystrokes passing through BDR's server: accepted, with the plan section 9 protections.
4. OpenAI hosted web search and dropping the typed-phrase rule: yes to both.
5. Daily assistant budget: 15 USD per user GLOBALLY (the default for every user, not only browser users).

Owner instruction: build all waves, starting with wave 0.
