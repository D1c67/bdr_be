# Teams alerts (BDR Alerts bot)

Migration 0175. Code: `app/services/teams_alerts.py`, `app/routers/teams_bot.py`,
the `/users/me/teams-alerts` routes in `app/routers/users.py`,
`scripts/build_teams_alerts_app.py`; frontend `components/TeamsAlertsSection.tsx`.

## What it does

Notifications go to Microsoft Teams as a 1:1 message from the **BDR Alerts**
bot, but only the ones each role is routed to (see "Who gets what" below,
migration 0177). The message is a short Adaptive Card: the project number and
name in bold, the message, and a button to the same deep link the email
uses. The daily reports (Bids due today, Auto send preview and results) get a
short Teams copy too: one line per project, no amounts.

Teams is routed on its own, so a bell-only notification (Auto sent, an NGEM
scan failure) can still reach Teams. Every notification still lands in the
bell.

- Internal users only. The external estimator never gets a Teams copy.
- Each person has an on/off switch under Settings > Microsoft Teams alerts
  (on by default; no `teams_alert_users` row = on). Turning it off leaves
  email and the bell unchanged.
- Settings also shows the last delivery or the last error, and has a
  **Send a test alert** button.
- Fire-and-forget like the email mirror: a Teams failure never blocks or
  breaks the flow that created the notification.
- An active RFP test session holds every Teams send (fail closed).
- `notifications.teams_sent_at` records when each Teams copy was delivered.

## Who gets what (0177)

Code: `app/services/notification_routing.py` (the event catalog and the
defaults), `app/routers/notification_routing.py`
(`GET /notification-routing`, `PUT /notification-routing/cell`,
`POST /notification-routing/reset`, Executive + IT Admin), frontend
`components/NotificationRoutingSettings.tsx` (Settings > Notification
routing). Overrides live in `notification_channel_prefs` (one row per role
and event; absent row or NULL column = the code default). Changes reach both
workers within 30 seconds.

Each notification type belongs to one event (`stage_handoff` splits by
`metadata.stage`: Go/No-Go and Verify are their own events). For each role
that receives an event there is an Email switch and a Teams switch. A switch
only filters people who already receive the event; it never adds a role.
Email is a filter on the email a notification already sends.

Teams defaults (decided with G3 on 2026-10-23):

| Role | Teams by default |
| --- | --- |
| Executive | Bids due today, Go/No-Go, Verify, Calling In, approval requests, revisions, personal |
| Estimating Admin | Bids due today, Auto send results, intake needed, RFP emails to review, send to estimator, stage handoffs, numbers verified, auto sent, auto send problems, proposal send failed, estimator revisions, personal |
| Estimating Engineer (Labor) | Bids due today, estimate delivered, vendor quotes, late quotes, numbers verified, revisions, personal |
| Estimating Engineer (Materials) | Estimate delivered, revisions, personal |
| IT Admin | Bids due today, Go/No-Go, Verify, tech problems, security alerts, personal |
| Accountant | Auto send results, personal |

"Revisions" = estimator revision rounds, drawings changed, re-verify
required. "Personal" = @mentions in notes, nudges, approval decisions.
Email defaults keep everything that emailed before. The audiences added on
2026-10-23 get the bell and Teams but no email: the Labor engineer for vendor
quotes / RFQ replies and numbers verified, the IT Admin for Go/No-Go and
Verify.

RFQ email routing (2026-10-25): Estimating Admins, Executives and IT Admins
receive one `rfq.initial_send` announcement per project after its first
successful vendor batch. Later batches and retries do not announce it again.
Failed-only batches do not count; a partially successful batch reports its
sent and failed totals. Migration 0181 seeds projects with existing sends,
then atomically claims each new project and inserts its recipient notices.
The new "RFQs initially sent" routing row defaults to email on and Teams off.
These three roles never receive incoming RFQ reply, quote or late-quote
emails, including old queued rows or stale routing overrides. Engineers keep
their existing routing. The vendor email's configured shared bids-desk CC
is separate from role notification routing.

## How a message gets to a person

All app-only (no user sign-in), using the existing Graph app registration:

1. Graph `GET /users/{email}` (then `mail eq` as a fallback) for their
   Entra object id.
2. Graph finds the BDR Alerts app in the org catalog by its manifest id
   (`TEAMS_APP_MANIFEST_ID`) and uses the person's existing install of it.
   Only when they have none does BDR install it
   (`POST /users/{id}/teamwork/installedApps`; 409 = already installed).
   In the G3 tenant that install is refused ("Caller is not authorized")
   even with the Self permission and webApplicationInfo in place, so the
   app is installed for everyone by a Teams admin (setup policy, 10-23).
   A policy install reaches each person when they next open Teams and can
   take hours; until then their alerts fail with "not installed for you yet".
3. Graph returns the bot's 1:1 chat with them (`.../installedApps/{id}/chat`).
4. The card is posted to that chat through the Bot Connector
   (`TEAMS_BOT_SERVICE_URL/v3/conversations/{chat}/activities`), signed in as
   the bot.

Steps 1 to 3 run once per person; the object id and chat id are cached in
`teams_alert_users`. If the cached chat stops working (the person removed the
app in Teams), the sender installs again once before counting a failure. To
stop alerts, people use the switch in BDR, not Teams.

## One-time setup (Azure + Teams admin)

1. **Graph permissions.** On the existing app registration (`MS_CLIENT_ID`),
   add these Microsoft Graph *application* permissions and grant admin consent:
   - `User.Read.All` (find each person's account; skip if already granted)
   - `TeamsAppInstallation.ReadWriteSelfForUser.All` (install BDR Alerts for each person)
   - `AppCatalog.Read.All` (find BDR Alerts in the org catalog)
2. **Azure Bot.** Azure portal > Create a resource > **Azure Bot**.
   - Bot handle: `bdr-alerts`. Pricing tier: **F0 (free)**.
   - Type of App: **Single Tenant**. Creation type: **Use existing app
     registration**, App ID = `MS_CLIENT_ID`, tenant = `MS_TENANT_ID`.
   - After it is created: Configuration > Messaging endpoint =
     `https://bdr-api-production.up.railway.app/teams/messages`.
   - Channels > **Microsoft Teams** > accept the terms > Apply.
   (A separate app registration for the bot also works: set
   `TEAMS_BOT_APP_ID` / `TEAMS_BOT_APP_SECRET`. The package still names
   `MS_CLIENT_ID` in `webApplicationInfo`, so Graph's "Self" install
   permission keeps covering it.)
3. **Teams app package.** From `bdr_be/`:
   ```
   uv run python scripts/build_teams_alerts_app.py \
     --frontend-url https://<the production BDR site> \
     --out ../bdr-alerts-teams-app.zip
   ```
   The manifest's `webApplicationInfo.id` must be `MS_CLIENT_ID`: Graph's
   "Self" install permission only covers a Teams app that names the calling
   registration there (without it every install is refused with "Caller is
   not authorized"). Then Teams admin center > Teams apps > Manage apps >
   **Upload new app**
   and pick the zip. Make sure the org's app permission policy allows it
   (Manage apps > the app > Allowed). Re-uploading a changed package needs
   `--version` bumped.
4. **Turn it on.** Railway: `TEAMS_ALERTS_ENABLED=true`, redeploy. Then in
   BDR, Settings > Microsoft Teams alerts > **Send a test alert**. The first
   one installs BDR Alerts for you, so it can take a few seconds.

## Settings (env)

| Var | Default | Meaning |
| --- | --- | --- |
| `TEAMS_ALERTS_ENABLED` | `false` | Master switch. Off = nothing is sent and Settings says "not set up". |
| `TEAMS_BOT_APP_ID` / `TEAMS_BOT_APP_SECRET` | empty | The bot's app registration; empty = `MS_CLIENT_ID` / `MS_CLIENT_SECRET`. |
| `TEAMS_APP_MANIFEST_ID` | fixed UUID | The manifest id the package is built with and the sender looks up. Same on every server. |
| `TEAMS_BOT_SERVICE_URL` | `https://smba.trafficmanager.net/teams/` | Bot Connector base for proactive messages. |

## Errors on the Settings status line

- *missing a Graph permission*: step 1 (or admin consent) is not done.
- *Teams will not let BDR install this app package*: the uploaded package
  predates `webApplicationInfo` (or names another registration); rebuild
  with a bumped `--version` and upload it over the existing app.
- *not in the Teams app catalog yet*: step 3 upload is not done, or the
  package was built with a different `TEAMS_APP_MANIFEST_ID`.
- *bot could not sign in* / *Teams rejected the bot's sign-in*: the Azure
  Bot's app id or secret does not match `MS_CLIENT_ID` / the bot vars.
- *No Microsoft 365 account found*: the person's BDR email is not a user
  in the G3 tenant.
