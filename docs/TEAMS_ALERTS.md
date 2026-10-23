# Teams alerts (BDR Alerts bot)

Migration 0175. Code: `app/services/teams_alerts.py`, `app/routers/teams_bot.py`,
the `/users/me/teams-alerts` routes in `app/routers/users.py`,
`scripts/build_teams_alerts_app.py`; frontend `components/TeamsAlertsSection.tsx`.

## What it does

Every notification that BDR mirrors to email (everything passed to
`notification_email.queue`) is also sent to the same person in Microsoft
Teams as a 1:1 message from the **BDR Alerts** bot. The message is an Adaptive
Card with the email's heading, the project, the message, and a button to the
same deep link the email uses. Bell-only notices (RFP match sweeps, NGEM and
BuildingConnected scan notices) stay out of Teams, as they stay out of email.

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

## How a message gets to a person

All app-only (no user sign-in), using the existing Graph app registration:

1. Graph `GET /users/{email}` (then `mail eq` as a fallback) for their
   Entra object id.
2. Graph finds the BDR Alerts app in the org catalog by its manifest id
   (`TEAMS_APP_MANIFEST_ID`) and installs it for the person
   (`POST /users/{id}/teamwork/installedApps`; 409 = already installed).
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
