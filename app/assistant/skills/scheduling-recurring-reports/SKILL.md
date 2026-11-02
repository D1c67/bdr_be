---
name: scheduling-recurring-reports
description: Use when the user wants a report on a schedule (every weekday, every Monday, each month), wants to stop, pause or change one, or asks what routines they have.
metadata:
  requires_flags: [bidding]
  roles: []
  version: "1"
---

# Scheduling recurring reports (routines)

A routine is a report BDR builds and sends to the user on a schedule. It runs no AI: BDR builds the same table every time from the bids, posts it in the routine's own conversation, rings the bell and, when chosen, emails the user. The tools are schedule_create, schedule_list, schedule_preview, schedule_pause, schedule_resume, schedule_end and schedule_run_now.

## The six recipes

- due_today: the bids due today, with what each is still missing (vendor quotes, labor, markups).
- due_this_week: the bids due from today to Sunday, with the same checks.
- missing_quotes: the bids in the next horizon_days (1 to 30, default 7) that are missing vendor quotes, with the categories that have none. With project_id it watches one bid until it is submitted or closed.
- labor_not_entered: the bids in the next horizon_days with no labor entered. With project_id it watches one bid.
- estimator_vs_bid: "Estimator vs our price" for the bids sent in a month: the estimator's price beside our lowest price sent, and which was higher. period is last_month (the default), this_month or a month like 2026-09. Only Estimating Admins, Executives, IT Admins and Accountants can have it; the message and the email carry counts only, the table is in the conversation.
- quote_readiness: "Key quotes for the bid week": the bids due in a week, how many key quotes are in, and when the estimator returned the estimate to us. week is next_week (the default), this_week or the date of a Monday. A monthly routine on the first takes period last_month; a weekly routine on Friday takes week next_week.

period goes only with estimator_vs_bid and week only with quote_readiness; leave both null for the other recipes. If the user asks for a report that is none of these, say which six exist and ask which one fits. Never promise another report.

## Creating one

1. Get the time right. Times are Pacific. When the user gives an hour without morning or evening ("every weekday at 7"), ask which one. Never invent a time or a day.
2. Pick the cadence: weekdays (Monday to Friday, G3 holidays skipped), daily (every day, holidays included), weekly (the days listed, every 1 to 4 weeks), monthly (day 1 to 28) or hourly (every hour inside a window of at most 18 hours, on weekdays or every day). Leave unused fields null and days as an empty list.
3. Pick the delivery: chat_bell unless the user asks for chat only or for an email too (chat_bell_email). only_if_changed is true only when the user says to send it only when something changed.
4. Call schedule_create once. It becomes an approval card listing the recipe, the schedule in words, the delivery and the next three runs. The card is the only way to create a routine: nothing is set up until the user approves it. Say "I have prepared" or "waiting for your approval", never that it is set up.
5. At most 10 routines can be active. When the card is refused for that, tell the user to end or pause one first.

The card may note that the 6:03 AM daily digest email already lists today's bids (that routine stays quiet on days the digest reaches the user), or that BDR's own due-date reminders also arrive. Mention it in one sentence.

## Stopping, pausing and starting again

- "Stop sending that report" means end it: call schedule_list, find the routine the user means (ask when two could match), then call schedule_end with its id. Ending is for good; its history stays on the Routines tab.
- Pausing is reversible: schedule_pause stops it until the user resumes it. Use it when the user says "pause", "hold" or "not this week".
- When schedule_end answers with status paused and a note, the conversation read outside content, so the routine was paused instead of ended. Tell the user it is paused and that End on the Routines tab stops it for good.
- schedule_resume and schedule_run_now are refused in a conversation that read outside content; then the user does it on the Routines tab.
- A routine cannot be edited. To change the time, the recipe or the delivery, end it and propose a new one.

## Looking and trying

- schedule_list shows each routine's status, why it is paused, its schedule in words and its next run.
- schedule_preview is a dry run: what the routine would post right now. Nothing is sent or saved. Use it when the user asks what a routine would show.
- schedule_run_now runs a routine once now through its usual channels. It is refused when it ran in the last few minutes or is running. The result arrives in the routine's own conversation, not in this answer.

## Pauses BDR makes on its own

A routine pauses after 3 failed runs, after 10 deliveries nobody opened, when the user's access changes, or when Bidding is switched off for the user. Say why when schedule_list shows it, and point the user to Resume on the Routines tab.
