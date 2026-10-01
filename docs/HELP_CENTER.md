# Help Center (internal users)

Status: built 2026-09-28, DEV ONLY, uncommitted, code only (no migration, no
server settings, no backend change). First topic: RFP Ingestion.

## 1. What the owner asked for (2026-09-27)

A help feature for internal users: a conventional question-mark icon at the
top right of the app that opens a separate page. First topic: RFP ingestion,
how it works, how people interact with it, edge cases, and what happens on
failure. Tailored per role (Estimating Admin, Estimating Engineers, Executive,
IT Admin; engineers read the same page as the Estimating Admin). Elaborate
enough that people understand deeply, for example that an Estimating Admin
cannot see an invitation that reached only an Executive's mailbox, even after
the Executive mentions it by word of mouth. Search, pictures and videos.

Decisions taken with the owner (all four recommended options):

| Question | Decision |
|---|---|
| Engineers vs Admin permission gaps | Same page as the Admin; anything they cannot do carries a "Who can do this" tag naming the roles that can and whom to ask |
| Pictures and videos | Real UI captured on dev with a fictional sample RFP: every API call answered by fixtures, so no real data ships (public/ is downloadable without login) and nothing is written anywhere |
| Other roles' versions | Own role by default plus "Viewing help as" to switch (URL `?as=<role>`) |
| Accountant | A shorter view-only version (9 of the 16 chapters) |

## 2. Where it lives

Frontend only (`bdr_fe`):

| Path | What |
|---|---|
| `components/HelpButton.tsx` | The header ? button (mounted in `components/AppShell.tsx` before the bell). On an RFP page it links to the RFP chapter about that page (`lib/help/context.ts`), elsewhere to `/help` |
| `app/(app)/help/page.tsx` | Help Center home: search across topics, topic cards, "Common situations for the <role>" |
| `app/(app)/help/[topic]/page.tsx` | A topic: chapter rail, one chapter at a time, "On this page", prev/next, hash deep links (`#chapter`, `#section`, `#scenario`), copy-link per section |
| `lib/help/types.ts` | The content model (the contract) |
| `lib/help/registry.ts` | Topic list and lazy loaders (content is code-split to /help) |
| `lib/help/filter.ts` | Which chapters, sections and blocks a version shows |
| `lib/help/search.ts` | In-browser search over exactly what the version shows (synonyms, prefix match, role-specific boost) |
| `lib/help/markup.ts` | Inline markup: `**bold**`, `[[UI label]]`, `[text](#id)`, `[text](/route)`; no HTML |
| `lib/help/view.ts` | Own role vs "View as" (`?as=`) |
| `lib/help/media.ts` | Screenshot and video manifest |
| `lib/help/rfp/*.ts` | The RFP Ingestion topic, one module per chapter, assembled in `index.ts` |
| `lib/help/calling-in/*.ts` | The Calling In topic (added 2026-09-30), seven text-only chapters, assembled in `index.ts`; enabled wherever Bidding is served |
| `components/help/*` | Renderers: blocks, Who-can tag, scenarios, media, search, View as |
| `locales/*/translation.json` `help.*` | Chrome strings (English in all 6 catalogs); the content itself is English TS data |
| `public/help/rfp/*` | Screenshots (WebP) and videos (MP4 + WebP poster), fictional data only |
| `scripts/help-media/` | The capture harness (own package.json, ignored by eslint and git for node_modules/out). `node shots.mjs`, `node videos.mjs` |

## 3. Roles and versions

Help roles: `estimating_admin`, `estimating_engineer` (both focuses),
`executive`, `it_admin`, `accountant`. Audiences for `for` tags: `admin`
(Estimating Admin + engineers), `executive`, `it_admin`, `accountant`.
`canDo` lists exact help roles; a viewer outside the list sees "Your role
can't do this. Ask <askWho>." The external estimator never reaches the
internal shell. Links to parts a version hides render as plain text.

The Accountant's version hides the action-only chapters (review, matching,
harvest, file safety, portals, senders, IT toolkit). The IT toolkit chapter
shows only in the IT Admin version.

The topic appears when the deployment serves any RFP page
(`features.rfp_email_ingest` or the Created page's flags); NGEM and
BuildingConnected sections carry `feature` tags and hide when those switches
are off.

## 4. How the RFP content was produced and checked

1. Eight code readers mapped RFP ingestion from the user side (intake,
   visibility, matching, email harvest, portals, file safety and split,
   creation and intake, processing and outages) with file:line evidence, plus
   the exact English UI labels and role-by-role confusion scenarios.
2. Chapters were written from those notes (docs describe plans that were never
   built, notably RFP_FILE_VERDICTS.md: no Release or Re-check button exists,
   and the help says so).
3. A validator checks: unique ids, every `#anchor` resolves, media ids exist,
   no em or en dashes, and every `[[label]]` against the English catalog (263
   of 270 verbatim; the rest are templated labels such as "Stuck: <reason>").
4. Eight reviewers checked every claim against the code; all 24 findings were
   applied (mostly steps that told Estimating Engineers to use pages or dialogs
   they cannot open, BuildingConnected connect permissions, and upload
   category labels).
5. Pages rendered headless as each role (fixtures) and eyeballed.

Media shipped: 24 screenshots (WebP) and 5 videos (MP4 with WebP posters),
8.7 MB in `public/help/rfp/`, every frame from fictional fixture data.

## 5. Keeping it true

The help describes behavior as of 2026-09-28. When RFP ingestion changes, the
chapter that explains that part must change with it. The facts most likely to
move:

- Automatic creation (`RFP_CREATE_AUTO_ENABLED`) and automatic merging
  (`RFP_MATCH_AUTO_MERGE_ENABLED`): the text describes both settings.
- File verdicts (RFP_FILE_VERDICTS.md): if the Release / Re-check / IT alert
  plan is built, update `fileSafety.ts`, `itAdmin.ts`, `failures.ts`,
  `start.ts` ("never open a held file") and the troubleshooting entries.
- Colleague forwarding (deferred): if built, update `visibility.ts`
  (forwarding section) and every "forwards are ignored" line.
- Role tuples in `bdr_be/app/routers/rfp*.py` and `lib/rfp*.ts`: update the
  permission table in `visibility.ts` and the `canDo` lists.
- Delete project and Deleted Projects (added 2026-09-29): the section
  "Getting rid of a project that should not exist" (`new-projects-wrong` in
  `newProjects.ts`) explains who can delete (Estimating Admin, Executive, IT
  Admin), what happens (gone from every page, dashboard, report and reminder;
  nothing emailed; full server archive), the typed-name confirmation (the
  number when the project has no name), that the RFP email or portal
  invitation will not create it again, and that an IT Admin restores it from
  `/deleted-projects`. The Created page's "Working the list" bullets mention
  [[Delete]]. The ? button on `/deleted-projects` opens that section
  (`lib/help/context.ts`). If the delete roles, the reasons, the blocked
  cases (PM or Certified Payroll enrollment) or the restore rules change,
  update that section.

- Calling In (added 2026-09-30, `lib/help/calling-in/*.ts`): mirrors
  `CALLING_IN.md`. If the list timing (10 days, Pacific calendar days, end of
  day for an unknown bid time), the "only Spoke with them marks a GC done"
  rule, who is notified (Executive, Estimating Engineer Labor), who edits
  (author) or deletes (Executive, IT Admin) calls, or the analytics
  definitions change, update `timing.ts`, `logging.ts`, `editing.ts`,
  `alerts.ts` or `analytics.ts`. The ? button on `/calling-in` opens `#start`
  and on `/analytics/calling-in` opens `#analytics` (`lib/help/context.ts`,
  now a page to topic to chapter table).

To regenerate media after a UI change: `cd bdr_fe/scripts/help-media && npm
install && node shots.mjs && node videos.mjs` against the dev frontend.

## 6. Release

Code only: deploy the frontend. Nothing in Railway, no migration. The ?
button only shows when at least one topic is available to the viewer, and the
RFP topic only exists where RFP ingestion is switched on. The Calling In topic
is available wherever Bidding is served, so once Calling In is released the ?
button shows in production for every internal role (it opens Calling In help;
the RFP topic still appears only where RFP ingestion is on).
