# Phase 9 owner questions: briefing for a voice conversation

This file is a briefing. It holds 39 numbered questions about Phase 9 of the BDR assistant, plus two confirm-or-change blocks (open decisions from the assistant's design record, and the eleven Phase 8 follow-ups) and one small extra item, with everything needed to talk them through. The owner of G3 Electrical will discuss it out loud with a voice agent. Everything here comes from the Phase 9 build plan (written and adversarially reviewed on November 3, 2026), the owner's Phase 8 decisions of November 2, 2026, the assistant's design record, and the assistant build log as of November 3, 2026.

Nothing in Phase 9 is built yet. Nothing is applied to any database. The build waits until the owner answers these questions and the plan is updated to match.

Expected length: about 30 minutes if the owner takes the recommended route (items A, C and B; D and E skipped) and accepts recommendations in batches where they want to; about 45 minutes if the owner also walks through items D and E.

The questions keep the plan's own numbers (1 to 39) so the answers map straight back to the plan. They are grouped by item, so skipping an item skips all of its questions. Because of that grouping, the numbers do not run in order through the file.

---

## 1. Instructions for the voice agent

### Your role

- You are walking the owner of G3 Electrical through the Phase 9 decisions for the BDR assistant. The owner built and runs BDR and knows the business well. They are not a developer and may not remember every technical detail.
- Start with question 1 (which items they want at all). Then go only through the groups for the items they picked.
- Take the questions one at a time. For each one: say the question in one plain sentence, explain why it matters, walk through the options with the examples given, then give the recommendation and the reason. If the owner asks for the recommendation first, give it first.
- Answer follow-up questions only from this file. If the file does not cover something, say so plainly, for example: "The briefing doesn't say. Claude Code can check that when you bring the answers back." Never guess numbers, names, dates or behaviour.
- Never pressure. The recommendation is a suggestion, not the right answer. The owner may pick any option, combine options, add conditions, or give a number of their own.
- Let the owner skip a question, defer it ("come back to it"), or answer several at once ("take the recommendation on 3 through 8"). Keep track of which are answered, skipped, deferred or not applicable.

### A note on Phase 8 (nothing to decide here)

Phase 8 (the assistant checking each person's notifications, called triage) is finished: it is built and tested on the dev system, and on November 3 the owner said yes to saving it (committed) on the local working copy. It is not pushed and not on the live system. Nothing in this briefing decides anything about finishing Phase 8. If the owner asks: the "everything urgent" tuning finished on November 3 and the new instructions were kept (three live test runs all passed), and the open money-tracking question about a cut-off AI call was closed with the fix the owner approved. Because the tuning passed, the plan's fallback question (what to do if the tuning failed) does not come up and is not in this briefing. Phase 8 is committed, so Phase 9 work can start as soon as these answers are in.

### Which questions depend on earlier answers

If a question does not apply because of an earlier answer, say so and offer to record "not applicable".

- Question 1 picks the items. For every item the owner does not pick, all of that item's questions are not applicable:
  - Item A not picked: questions 3 to 8 are not applicable.
  - Item B not picked: questions 9 to 16 are not applicable.
  - Item C not picked: questions 17 to 19 are not applicable.
  - Item D not picked: questions 20 to 30 are not applicable.
  - Item E not picked: questions 31 to 36 and 39 are not applicable.
- Question 2 (the test budget) is always asked, but its recommended amount depends on question 1: 1 dollar without item E, 3 dollars with item E. If E is picked in question 1 but question 31 later comes out (a) (no Stage 2), the 1 dollar figure applies.
- Question 37 (database change number) only matters if a chosen answer needs a database change (see the next list). If nothing chosen needs one, record it as "not applicable".
- Question 38 (order and timing) is always asked if any item is picked.
- Inside item A: if question 3 is (b), question 4 is not applicable (mirror emails are left alone anyway) and question 5 has nothing to show (the scheduled proposal is not labelled), so record question 5 as not applicable. If question 7 is (b), mirror emails that an assistant action caused do not get the "system" label (they are logged as caused by the assistant); question 4 still applies to all other mirror emails.
- Inside item B: question 9 decides the shape. If question 9 is (d), item B is dropped: questions 10 to 16 are not applicable. If question 9 is (c) (no model, fixed rules only), the plan does not detail that option; questions 12, 13 and 14 are about the model and probably do not apply, while 10, 11 and 15 still shape which rows and titles. The plan says question 16 applies under every answer to question 9, so still ask it; whether it matters with no model is something Claude Code would confirm. Say that the briefing doesn't detail (c). Question 16 has an extra part only if question 9 is (a).
- Inside item C: if question 17 is (c) (keep English only), item C is dropped: questions 18 and 19 are not applicable.
- Inside item D: question 20 decides the shape. If question 20 is (d), questions 21 to 30 are not applicable. Options (b) and (c) of question 20 are not designed in the plan; if the owner picks one, questions 21 to 30 are not applicable as written and Claude Code would come back with a new design. Question 21 (who, and how many people per role) feeds question 29 (the fuse number). Question 27 option (b) leans on question 22 (a): if question 22 is (b), the sender gets no escalation of their own, so 27 (b) would be a real change; the briefing doesn't say how Claude Code would build that. Question 29 (a) says the teammate switch cannot go on until the fuse number is set; that switch is question 26 (a). If question 26 is (b), the briefing doesn't say how that rule works; Claude Code would confirm.
- Inside item E: question 31 decides the shape. If question 31 is (a), questions 32 to 36 and 39 are not applicable. If question 31 is (c), there is an extra decision this briefing does not hold (how long a drafted approval card stays valid; an earlier design proposed 24 hours plus a bell when one expires unseen). Question 33 (b) roughly triples the prep cost, which affects question 36.
- The open decisions block (section 7) and the Phase 8 follow-ups block (section 8) do not depend on question 1. Only open decision 4 is tied to an item (item D), and question 20 answers it.

### Which answers change the database design

These answers change the database change text (the "migration") or decide whether there is one at all. If the owner is short on time, try to settle these first:

- Question 1: picking D or E always means a database change. Picking B usually means a small one (see question 16).
- Question 6 (b): adds a database rule on the email log.
- Question 9 (a) together with question 16 (b): adds a second hourly fuse for health escalations. Question 9 (a) alone (with 16 (a)) does not.
- Question 13 (c): adds a 30-minute hold for health rows.
- Question 16 (b): a small change to how the safety fuse samples decisions.
- Questions 20, 22, 24, 26 and 29 (item D).
- Questions 31 (b) or (c), 32 (a), 36, and 39 (b) or (c) (item E).
- Question 37: the number itself.
- Phase 8 follow-up 11: its alternative changes one database function.

Which choices need the owner at the screen: the database tool asks a person to confirm certain kinds of changes (rewriting existing database rules). In Phase 9 that happens only if item D or item E is built (their "part B"), and then under every answer to their questions: picking D or E means the owner at the screen once, when the change is applied on dev. Items A, B and C need no owner at the screen under any answer.

### Suggested order

1. Question 1 (which items). This decides everything else.
2. Question 2 (test budget), question 38 (order and timing), question 37 (database change number, only if needed).
3. Item A: questions 3 to 8.
4. Item C: questions 17 to 19.
5. Item B: questions 9 to 16 (9 first; it decides the shape).
6. Item D, only if picked: questions 20 to 30 (20 first, then 21, because the headcounts feed 29).
7. Item E, only if picked: questions 31 to 36, then 39.
8. The open decisions block (section 7): quick, many are "nod or change".
9. The Phase 8 follow-ups block (section 8): quick confirm or change.
10. The extra item (release watch limits).
11. The readback.

This order follows the recommended build order (A, then C, then B). If the owner is short on time, mention that most smaller questions can be answered "take the recommendation" in one go, for example "take the recommendations on 3 through 8".

### How to speak

- Speak plainly. Say "dollars" and "cents", never "USD".
- Small money: say "about a fifth of a cent", "less than a tenth of a cent" or "a fraction of a cent". Round to an easy number unless the owner wants the exact figure.
- Read job numbers like 26.11.7301 as "twenty-six, eleven, seventy-three oh one", or however the owner prefers.
- Model names: say "G P T six luna" for the triage model, "G P T five point four" for OpenAI's full model, "G P T six astra" for the pricier chat model mentioned in question 34.
- Never read code labels, file names, setting names or ids aloud. This file avoids them on purpose. If the owner asks for a technical name, say Claude Code has it in the plan.
- The database change number can be said as "one ninety-seven".
- GC means general contractor. RFQ means request for quote. RFP means request for proposal (an invitation to bid). IT Admin is the system administrator role.
- "SQL" or "a direct database edit" means a change Claude Code or an admin makes straight in the database, not a screen in the app.
- Item letters (A, B, C, D, E) and question numbers are fine to say.

### The readback at the end

At the end, read back a decisions summary using the answer sheet in section 10: one line per question with the chosen letter, plus any conditions, numbers or notes the owner gave in their own words. Mark skipped questions "skipped", deferred ones "deferred", and ones that do not apply "not applicable". When the owner took the recommendation, say the letter and add "recommendation". Then read the open decisions and the Phase 8 follow-ups the same way ("nod" or the change). Example of the format:

- "1: A, C and B (recommendation). 2: a, 1 dollar (recommendation). 38: a (recommendation). 37: a (recommendation). 3: a (recommendation). ... 20 to 30: not applicable. 31 to 36 and 39: not applicable. Open decision 3: yes, one short line. Open decision 12: nod. Follow-ups 1 to 11: all nod. Extra item: propose the numbers to me before release."

Ask the owner to confirm the summary. The owner will bring that summary back to Claude Code.

---

## 2. Background

### What BDR is

BDR is G3 Electrical's own web app for bidding electrical work and running projects. Each bid has a job number like 26.11.7301. BDR tracks the bid's stages: requests for quotes (RFQs) go out to vendors, vendor quotes come back, engineers work the material and labor numbers, an Executive verifies the numbers, and a proposal goes to the general contractor (GC). Each bid has an internal due time, for example 5:00 PM on bid day.

BDR already notifies people a lot, on its own:

- The bell in the app (in-app notifications), for example "Quote received", "Drawings changed", "Bid due".
- Emails and Microsoft Teams messages for many bells, chosen per role and per event on the notification routing grid in Settings. An email that copies a bell is called a "mirror email".
- BDR's own scheduled emails: the 6:03 AM weekday due digest (what is due today) and the Auto Send emails (Auto Send sends proposals automatically at 5:05 PM Pacific, with a check report before and a results report after).
- "Tech problems" bells for IT Admins and Executives: the AI spending cap was hit, the RFP pipeline's AI model went away or came back, an RFP processing step failed, an email could not be matched, BuildingConnected or the NGEM portal scan or login failed. BDR already sends these as a bell, an email (for most types) and a Teams message.

### What the assistant is

The assistant is a personal AI helper inside BDR. Each internal person gets their own. It lives in a chat panel. It can read BDR data and do work for the person, but every write or send needs the person to click an approval card first. There are no standing approvals.

None of the assistant is in production yet. Production has no assistant database changes at all. Everything below is on the dev (test) system only.

### What Phase 7 built (the router)

- The "proactive router" is plain code with fixed rules and no AI model.
- It copies each person's bells quietly into their assistant Inbox as "record rows". Record rows are never pushed by Teams or email.
- It has its own items, such as "Bid due and your part is missing", "To-do due" and "Approval expiring".
- It sends each person an assistant digest on workdays at 7:00 AM and 1:00 PM Pacific (an Inbox digest, a bell and one email).
- Severity decides delivery: normal goes into the next digest; high goes now (bell plus Teams) unless it is quiet hours; urgent goes now even at night.
- Quiet hours are 6 PM to 6 AM Pacific, fixed for the company.

### What Phase 8 built (triage), as the owner decided on November 2

- A small, cheap AI model (G P T six luna) looks at each new record row (each bell copy) and decides one of three things: nothing to do, suggest a next step (shown on the Inbox row with a "Work on this" button that types a sentence into the chat; the person presses Send), or escalate (tell the person through the router).
- The model sees only facts: job numbers, times, true or false values, counts and codes. Never message text, names or email subjects.
- The model's choice is final. It may pick normal, high or urgent. Urgent breaks quiet hours.
- An escalation reaches only the person whose row it is (Phase 8 question 7). Item D in this briefing would change that.
- Triage is on for everyone who has the assistant, controlled by one company switch that an Executive or IT Admin flips. No personal opt-in or opt-out. No permission grant (delegation) is needed, because triage reads no data with tools.
- Triage spends only from the company's 50 dollar daily AI budget, not from any person's budget.
- A company-wide safety fuse pauses triage for everyone if it suddenly escalates far too much (more than half of the last 50 decisions, at least 20, or more than 30 escalations in one hour). An Executive or IT Admin clears it by turning the company switch off and on.
- The router's own digest emails are labelled "system" in the email log (Phase 8 question 18). Item A in this briefing would extend that label.
- New Phase 8 screen text ships in English only. Item C would translate it.
- Stage 2 (the assistant running on its own to prepare notes) was not built (Phase 8 question 1). Item E would build it.

### The guiding principle (the owner's words)

"The app owns notifications. The assistant does work and speaks up only when the app does not already cover something. The router must not duplicate what BDR already sends."

### What Phase 9 is: a menu of five items

Phase 9 is a menu. The owner picks any of the five items, or none. Each item is designed so it can be built alone.

- A. A "system" label on every background email. BDR's background emails (the 6:03 AM due digest, the Auto Send reports, the estimator revision alert, bell mirror emails, and the Auto Send proposal when the schedule sends it) are logged today as if a person sent them, with no name. Item A labels them "system", so the email log can tell machine emails from person emails and AI emails. No AI, no cost, no database change by default. About 1 to 1.5 days.
- B. Triage of "Tech problems" bells for IT Admins. The assistant would also look at the system-health bells an IT Admin gets, note "recovered, nothing to do" when an outage cleared, or suggest a next step when something keeps failing. By default it never escalates (BDR already sends the bell, email and Teams). A fraction of a cent a day. About 3 to 4 days.
- C. Translations of the 51 Phase 8 screen texts into BDR's five other languages (Cebuano, Filipino, Hindi, Swahili and Urdu). Today those texts show in English in every language. No cost, no database change. Half a day to 1 day.
- D. A notice to a teammate. When a person's assistant escalates (for example "bid due at 5 PM, a part is missing"), the router could also put one notice in a teammate's Inbox, chosen by role (for example the Estimating Admin), never by name. This reverses the owner's Phase 8 answer that escalations reach only the person. A database change. About 2 to 3 weeks.
- E. Stage 2: prep notes. For a few suggestions (review changed drawings, review a revision), the assistant would run on its own with read-only tools and leave private notes on the Inbox row ("Notes ready"). This reverses the owner's Phase 8 answer 1. About 3 to 10 cents per prep, never more than 25 cents. A database change. About 2.5 to 4 weeks, and it can only start after the browser work (another Claude Code session) releases its shared files.

Recommended: A, then C, then B. D and E only if the owner wants them (the plan recommends not building E now). All of it only after Phase 8 is committed.

### Other sessions working in the same code

- The browser session: another Claude Code session building "browser v2" (the assistant doing tasks in a web browser). It holds many shared assistant files. Items B and D make small additions to some of them; item E needs most of them and so waits for that session to release them.
- The Teams alerts work: another session looks after BDR's notification and email-sending code. Item A changes one word in four of its files, and it is told first.

---

## 3. How each item would work (the proposed design)

### A realistic day after Phase 9 (with every item taken; each item stands alone)

1. 6:03 AM: BDR's due digest goes out. The email log now records it as "system", not as an unnamed person. (Item A.)
2. 9:40 AM: the RFP pipeline's AI model goes quiet. BDR rings both IT Admins: "RFP processing is waiting on the AI model". The IT Admin's assistant Inbox now records it as a row titled "Tech problems: AI model away". About five minutes later triage looks at it: work is waiting and this is the third "away" notice today, so it suggests "ask for a short summary". (Item B.)
3. About 10:00 AM: the "model back" bell arrives 20 minutes later. Its own row is triaged "Checked: recovered, nothing to do". No second Teams ping from the assistant. (Item B.) Note: the "away" row was already decided before the "back" bell existed, so the away row keeps its suggestion. Question 13 (c) offers a 30-minute hold that would change that.
4. An Urdu-speaking engineer opens his Inbox and reads the triage note and the Alerts card in Urdu. The reason and action wording from the server stays English, so the row mixes Urdu and English. (Item C.)
5. 2:40 PM: the Materials engineer's assistant escalates "bid 26.11.7301 due at 5:00 PM, Gear quote missing". The same answer names the audience "Estimating Admin". The router (not the assistant) puts one notice in the Estimating Admin's Inbox: "A teammate's assistant flagged bid 26.11.7301, due today at 5:00 PM PT; a part is still missing". It is in-app only (bell and badge), no Teams by default, at most one per person per project per day. The engineer's own Inbox row says "Also flagged to the Estimating Admin". (Item D.)
6. On a "Drawings changed" row, the suggestion "review the changed sheets" also starts one read-only prep run, paid from the company budget. By the time the engineer opens the row, a "Notes ready" link shows private notes listing the changed sheets against his open RFQs. (Item E.)
7. 5:05 PM: Auto Send sends a bid's proposal on its schedule. That project's Notification log shows "Sent by: System" for it. (Item A, question 5.)

### Item A: the "system" label on background emails

- Five background senders get the label: the 6:03 AM due digest, the Auto Send check and results reports, the estimator revision alert (sent when an outside estimator seals a revision round), bell mirror emails, and the Auto Send proposal only when the schedule sends it. When a person clicks "Send now", the proposal stays labelled with that person.
- Person-sent emails stay labelled with the person (Failed to bid, bid date changes, proposals sent by a person, RFQs, RFIs, invitations and so on).
- The email itself does not change. Only the email log entry changes.
- Visible change (question 5): today those emails show no sender in a project's Notification log. With the recommended answer, "Sent by: System" shows, but in practice only for proposals Auto Send sends on schedule: the due digest and the reports belong to no project, and mirror emails and (in practice) revision alerts show under their bell entry.
- Safety: the email code already refuses the "system" label if it is used from inside an AI run, so an AI-caused email can never hide under "system". Tests check every email-sending spot in the code (question 8).
- Release: item A can reach production only together with the whole assistant release, because it relies on assistant database columns that production does not have yet. It is never shipped early on its own.
- Testing: the due digest and Auto Send reports are never sent for real during testing (they would email every eligible person). One real test email goes only to the owner's test address, marked "[TEST]".

### Item B: triage of "Tech problems" bells for IT Admins

- Which bells: the "Tech problems" family only (AI spending cap hit, RFP model away and back, RFP step failures, email match failures, BuildingConnected and NGEM scan and login problems). Not security alerts, not browser-agent bells (question 10).
- Who: IT Admins only. Executives also get some of these bells (AI spending cap, BuildingConnected), but those copies are not recorded in an Executive's Inbox (question 11).
- What the model sees: the health type (from a fixed list), how many of the same notice the person got in the last 24 hours, whether a recovery was seen, and how many items are waiting. Never the bell's text, which can hold email subjects, error text or account emails that outsiders control (question 13).
- What it may decide (question 9, recommended): only "nothing to do" (for example "recovered" or "a one-off") or "suggest" (for example when the same failure repeats or work is waiting). Never escalate, never urgent.
- What it may suggest (question 12): only "ask the assistant for a short summary" or "set up a follow-up to-do". The assistant has no tool to fix an outage.
- Row title (question 15): "Tech problems: AI model away" instead of only "Tech problems".
- Old notices (question 14): a health notice older than 6 hours is skipped. If the daily AI budget is used up, health notices are skipped, not retried after midnight.
- Timing: triage looks at a row about 5 minutes after the bell. For an outage longer than about 5 minutes, the "away" row is decided before the "back" bell exists.
- Quality check: a separate set of about 25 test cases with the right answers written down, with the same pass marks as Phase 8 (at least 85 percent agreement, zero escalations, zero invalid answers).
- One new screen label ("Tech problems") in all six languages.
- Volume on dev (real numbers, last 90 days): 739 "Tech problems" bells, about 516 to IT Admins and 223 to Executives; 579 of them were AI spending cap bells, mostly from one AI Estimating test burst on October 21 to 23. In the last 7 days: 43. Production was not looked at.

### Item C: translations of the 51 Phase 8 screen texts

- 51 short texts: 7 on the Inbox (for example the triage note) and 44 on the Alerts card and the admin block (switches, counts, fuse and budget wording). About 327 English words in total.
- Translated by AI translators (Claude Code helpers), one per language, with a fixed word list from the Phase 7 translations, then an AI reviewer that checks the six money and safety texts closely and checks that the fill-in spots (like the time) are kept.
- Urdu reads right to left. One line ("Checked" plus a time) gets a fix so it reads correctly.
- The word "Triage" may stay English in Cebuano and Filipino.
- What stays English: the reason and action wording that comes from the server, and the Teams and digest text (a standing owner decision: assistant replies are English only). So a non-English Inbox row mixes languages.
- No native speaker has checked any of BDR's Phase 7 translations either. Question 18 (b) adds a native check of Cebuano and Urdu.

### Item D: a notice to a teammate

- When triage escalates, the model also names an audience from a fixed list: "self only", "the role that owns the missing part", "the Estimating Admin" or "an Executive". It never names a person.
- "The role that owns the missing part": Materials engineer for quotes, Labor engineer for labor, Estimating Admin for markups. There is no per-project assignee in BDR, so everyone active in that role gets it.
- Never reached: IT Admin, Accountants, outside estimators, or the sender.
- The router, not the assistant, writes the notice from a fixed template. Examples:
  - "A teammate's assistant flagged bid 26.11.7301, due today at 5:00 PM, and a part your role owns is still missing" (the "part missing" phrase only when the router's own check finds it true for that person's role).
  - "A teammate's assistant flagged bid 26.11.7301 for you" (neutral wording otherwise).
  - "Why" line: "The Materials engineer's assistant flagged this bid and chose you as the person to tell. BDR's own notices about the bid are unchanged."
- Limits: capped at high (never urgent), the teammate's quiet hours always apply, in-app only with no Teams by default (question 23), at most one notice per person per project per day (question 24), its own company switch, default off (question 26), its own safety fuse (question 29).
- The notice is withdrawn if the bid is already submitted or its due time passed, if the switch is turned off, or if the sender left the company or was deactivated.
- The teammate's own assistant can see the notice in their Inbox list, but only as the router's fixed text, with no sender name and no triage detail.
- Phase 8's own escalation to the person is unchanged.
- Open decision 4 (real task assignment between people) stays open: item D is a notice only. No task is assigned and no permissions move.

### Item E: Stage 2 prep notes

- Which suggestions start a prep run (question 33): "review the changed drawings" and "review the revision".
- What a prep run is: the assistant runs on its own, unattended, using read-only tools only (search BDR, the project summary, plan sheet documents and notes, the bid file splitter status, the quotes summary, and the labor numbers for revisions). At most 3 rounds, 10 tool uses, a limit on how much each AI reply may say, and a hard stop at 25 cents per run.
- It cannot: send mail, use the web or the browser, make approval cards, write anything in BDR, read or write the assistant's memory, or see schedules or the evidence and report tools.
- It is treated as untrusted from its very start (it reads BDR data that may hold text outsiders control).
- The notes go into a private conversation for that Inbox row. The row shows "Preparing notes", then "Notes ready". Nothing is pushed for a prep run.
- The notes conversation is read-only (question 35). "Continue in a new chat" opens an empty chat; the person copies notes over by hand if they want.
- Permission (question 39): a prep run happens only while the person has a live permission grant (delegation) from their own chat use. No grant, no notes.
- Money: paid from the company budget under its own label (question 32), at most 30 prep runs a day for the whole company (question 36).
- Fairness to chat: prep runs wait behind live chat. At most one runs at a time on each running copy of BDR's server, and they never take the last chat slot.
- Kill switches: turning the company triage switch off, or a fuse trip, cancels prep runs still waiting.

### Work order

- Nothing starts until Phase 8 is committed.
- A and C first (they touch no shared browser file).
- B after the Phase 8 urgent tuning closed (it has, per the build log of November 3).
- D after B, if both are picked (they change the same triage files).
- E only after the browser session releases its shared files.
- Everything is built and tested on dev only. Production needs the owner's separate, explicit approval for every step, at that moment.

---

## 4. Glossary

- Bell: BDR's in-app notification (the bell icon). Each bell has a type, such as "Quote received" or "Tech problems".
- "Tech problems" bells: BDR's system-health bells (AI spending cap hit, RFP model away or back, RFP step failed, email match failed, BuildingConnected or NGEM portal scan or login problems). Sent today to IT Admins and Executives as a bell, an email for most types, and a Teams message.
- Mirror email: an email BDR sends as a copy of a bell, per the routing grid in Settings.
- Due digest: BDR's own 6:03 AM weekday email listing what is due today. Different from the assistant digest.
- Auto Send: BDR's automatic proposal sending at 5:05 PM Pacific, with a check report and a results report.
- Estimator revision alert: the email BDR sends internally when an outside estimator seals a revision round.
- Email log: BDR's record of every email it sends, with who sent it: a person, the assistant (with the AI run's reference number), or "system".
- Notification log: the per-project history screen of the notices sent for that project.
- Inbox: the assistant's own list of items for a person, a tab in the assistant area.
- Record row: a quiet copy of a BDR bell in the person's Inbox. Never pushed. The feed for triage.
- Router: Phase 7 code with no AI that writes Inbox rows, builds digests and sends the assistant's Teams messages and digest emails.
- Triage: Phase 8: a small AI model looks at each record row and decides nothing, suggest or escalate.
- Escalation: triage telling the router "this person should hear about this".
- Suggest: triage proposing a next step on the Inbox row with a "Work on this" button.
- "Work on this": a button that opens the chat with a sentence already typed. The person presses Send.
- Severity: normal = next digest; high = now (bell plus Teams), held during quiet hours; urgent = now, even at night.
- Quiet hours: 6 PM to 6 AM Pacific, for the router only.
- Digest (assistant digest): the router's summary on workdays at 7:00 AM and 1:00 PM Pacific.
- Company switch: an on or off switch for the whole company, flipped by an Executive or IT Admin in the admin block, no redeploy.
- Server setting: a setting in BDR's server configuration, changed by the developer (Claude Code, with the owner) with a restart or redeploy.
- Pilot list (allowlist): a server setting listing the only people who get a feature during a pilot. Empty means everyone.
- Company budget (company cap): 50 dollars a day of assistant AI cost across the company (chat, triage, browser tasks).
- Per-person budget: 15 dollars a day of assistant AI cost per person (the figure set in the browser v2 work).
- Hold: before each AI call, the system sets aside the most it could cost, so two calls cannot both squeeze under the cap. After the call it settles to the real cost.
- Fuse (safety fuse): a company-wide switch that pauses triage for everyone if it suddenly escalates far too much. A bug guard, not a limit on a person.
- Fuse sample: the last 50 triage decisions the fuse looks at to spot a runaway escalation rate.
- Untrusted (tainted) text: text that outsiders could control, such as a vendor's email name, an email subject or an error message from another company's system. The assistant treats it as possibly hostile.
- Prompt injection: hostile text trying to give orders to an AI.
- Permission grant (delegation): a "signed note" saying this person lets their assistant work for them. Created by the person's own chat use in a signed-in session with two-factor sign-in. Cancelled by a two-factor reset, by an admin, or when a person leaves.
- Unattended: the AI running without the person in the chat at that moment.
- Approval card: the box in chat showing exactly what the assistant wants to write or send. Nothing happens until the person clicks approve.
- Prep run (Stage 2): an unattended read-only assistant run that leaves private notes on an Inbox row (item E).
- Teammate notice (handoff): item D's notice to a teammate chosen by role.
- Handoff day: for item D's "once a day" rule, a day runs from 6 AM to 6 AM Pacific, so a late-night flag and an early-morning flag count once.
- Golden set: test cases with the right answer written down, used to measure an AI's decisions before switching it on. Phase 8's bid set has 63 cases now.
- Live test: Claude Code runs the real feature on the dev system with real AI calls, using only two test logins, then deletes what it made.
- Test accounts: two test logins on dev (an engineer account and an admin account) used for live tests. Never a real person's account.
- Migration (database change): a numbered change to the database structure. Numbers set the order, so each must be unique.
- Part A and part B: each database change is split. Part A only adds things. Part B rewrites existing database rules, and the database tool then asks a person to confirm, so the owner needs to be at the screen.
- Dev, staging and production: dev is the test system (the BDR database). Production is the live system G3 uses. Phase 9 is built and tested on dev only.
- Languages: BDR's six screen languages are English, Cebuano, Filipino, Hindi, Swahili and Urdu. Urdu reads right to left.
- Internal roles: Materials engineer, Labor engineer, Estimating Admin, Executive, IT Admin, Accountant. Outside estimators are external and get no assistant.
- Estimating Admin: runs intake, sends work to estimators, handles proposals.
- Executive: verifies numbers, makes Go/No-Go decisions; an admin for assistant settings.
- IT Admin: the system administrator; an admin for assistant settings.
- Accountant: an internal, read-only role.
- Browser session: the other Claude Code session building browser v2 in the same code folder.
- Teams alerts work: the session that looks after BDR's notification and email-sending code.

---

## 5. Costs in plain numbers

### Prices per million tokens (from the app's price list)

Tokens are the units AI providers charge by; roughly, a token is a few characters of text.

| Model | Input | Output |
|---|---|---|
| G P T six luna (the Phase 8 triage model) | 10 cents | 50 cents |
| G P T five point four (proposed for item E prep runs) | 2.50 dollars | 15 dollars |

The G P T five point four prices are from the app's price list, dated October 21, 2026. The plan asks Claude Code to re-check them on OpenAI's pricing page; this briefing does not say whether that was done.

### What Phase 8 actually cost (real numbers from dev)

- One triage check of one row: about a thirtieth of a cent.
- The 44-case live quality test: about six tenths of a cent.
- All Phase 8 real AI testing so far, including the urgent tuning: about 13 cents, out of the 1 dollar test budget the owner set.

### Running cost per item

| Item | Running cost | Per month | Test spend | Build size |
|---|---|---|---|---|
| A | nothing (no AI) | nothing | nothing | 1 to 1.5 days |
| B | about a fifth of a cent a day on dev volumes; under 3 cents on a burst day like October 22; under 1 cent a day in the production estimate | about 6 cents (dev rate); under 30 cents (production estimate) | under 2 cents | 3 to 4 days |
| C | nothing | nothing | nothing (the translators are Claude Code helpers) | half a day to 1 day (plus half a day for a native check, if wanted) |
| D | under a tenth of a cent a day extra (Teams messages cost nothing) | under 2 cents | under 6 cents | 10 to 12 working days (2 to 3 weeks) |
| E | 3 to 10 cents per prep run, never more than 25 cents | see below | 12 to 40 cents for 4 test prep runs, at most 1 dollar | 12 to 18 working days (2.5 to 4 weeks) |

### Item E in more detail

- Per prep run: about 3 to 10 cents typical, with a hard stop at 25 cents.
- Production estimate (an assumption, not measured): 300 Inbox rows a day company-wide, 10 percent of them get a prep run, so 30 runs a day: about 90 cents to 3 dollars a day typical (2 to 6 percent of the 50 dollar company budget), at most 7.50 dollars a day (15 percent).
- Per month at 30 runs each working day: about 20 to 66 dollars typical, at most about 165 dollars.
- On dev volumes: about 1 to 4 cents a day.
- If "ask for a short summary" also started prep runs (question 33 (b)): about three times as many runs (about 90 a day), 2.70 to 9 dollars a day typical, up to 22.50 dollars, but the daily limit of 30 runs (question 36) would still cap spending at 7.50 dollars a day.
- Why this is lower than the Phase 8 briefing's "about 22 cents each": the Phase 9 design adds hard limits on how much each AI reply may say, how much each round may read, and a 25 cent stop per run, and it pins the model to G P T five point four.
- Comparison: a prep run costs about 100 to 300 times a single triage check.

### The caps

- Company budget: 50 dollars a day across all assistant AI use (chat, triage, browser tasks, and item E prep runs). When it is used up, AI work stops for the day.
- Per-person budget: 15 dollars a day. Triage and (by default) item E prep runs do not count against it.
- Browser task holds (up to 5 dollars each while a browser task waits) count inside the same 50 dollars.

---

## 6. The questions

### Part 1. The menu and the basics

#### Question 1: Which items do you want at all?

The question: Which of the five items, A to E, should Phase 9 build?

Why it matters: Each item is independent. Every item not picked skips all its questions and all its build work.

The items, in plain words:

- A. "System" label on background emails. BDR's background emails are logged today as if an unnamed person sent them. Item A labels them "system" in the email log, and shows "Sent by: System" on scheduled Auto Send proposals in the project's Notification log. No AI, no cost, no database change by default. 1 to 1.5 days. Reaches production only with the full assistant release.
- B. Triage of "Tech problems" bells for IT Admins. The assistant notes "recovered, nothing to do" or suggests a next step on health alerts; by default it never escalates. A fraction of a cent a day (under 30 cents a month in the production estimate). 3 to 4 days. Usually a small database change (question 16) that needs no owner at the screen.
- C. Translate the 51 Phase 8 screen texts into the five other languages. No cost. Half a day to 1 day.
- D. Teammate notice. When your assistant escalates, the router can also tell a teammate by role. Under a tenth of a cent a day. 2 to 3 weeks. A database change with you at the screen. Reverses your Phase 8 answer 7.
- E. Stage 2 prep notes. The assistant runs on its own with read-only tools for a few suggestions and leaves private notes. 3 to 10 cents per run, never over 25 cents; at 30 runs a day, under 3 dollars typical and at most 7.50 dollars. 2.5 to 4 weeks. A database change with you at the screen. Waits for the browser session. Reverses your Phase 8 answer 1.

Options: any combination of A, B, C, D, E, or none.

Pros and cons:

- A: clearer audit trail for free. Con: you see little on screen (only scheduled Auto Send proposals show "Sent by: System").
- B: fewer "is it still broken?" checks for IT Admins and a clearer Inbox title. Con: value may be thin, since the assistant cannot fix an outage and BDR already alerts.
- C: Phase 8 screens in everyone's language. Con: server wording stays English, so rows mix languages.
- D: a second pair of eyes on a bid at risk. Cons: biggest risk of noise (one bad AI answer reaches several people), largest build after E, many shared files.
- E: real prep work waiting when the person looks. Cons: costs 100 to 300 times a triage check, the first unattended assistant run that uses tools (a new security surface), waits for the browser session.

Recommendation: A, then C, then B. D and E only if you want them; the plan recommends not building E now (revisit once Phase 8's suggestions are judged good).

Depends on or affects: every other question. See "Which questions depend on earlier answers".

Likely follow-ups:

- "Can I add D or E later?" Yes. Each is designed so it can be built alone later.
- "Does any of this go to production now?" No. Dev only. Production needs your separate approval, and item A can only go with the whole assistant release.
- "Does anything change for BDR's own notifications?" No. BDR's bells, emails, Teams messages and digests are unchanged. Item A only changes the email log label; item B only reads bells.

#### Question 2: Live-test budget in dollars

The question: How much real AI spending may Claude Code use to test the chosen items on the dev system?

In plain words: after the build, Claude Code runs the chosen items for real on the dev system, using the real AI model, but only for the two test accounts, then cleans up everything it made. This is the same kind of test as Phase 8's, where you set 1 dollar and about 13 cents was used.

Options:

- (a) One total budget for all chosen items, enforced by a hard spending limit on the test runs. Test accounts only; real Teams messages only to the test accounts; test emails only to your own test address, labelled "[TEST]".
- (b) A budget per item: A nothing, B 25 cents, C nothing, D 50 cents, E 3 dollars.
- (c) Fake AI answers only for the model steps (no real AI calls).

Expected real spend (from section 5): B under 2 cents, D under 6 cents, E 12 to 40 cents typical and at most 1 dollar for its 4 test prep runs. A and C use no AI.

Pros and cons: (a) proves the real model works, one simple limit. (b) finer control. (c) no cost, but does not test the real model.

Recommendation: (a), 1 dollar total if E is not chosen; 3 dollars total if E is chosen.

Depends on or affects: question 1. Item E's test is the only one that could approach 1 dollar.

Likely follow-ups:

- "Will real people get messages?" No. Teams only to the test accounts; a few emails only to your test address, labelled "[TEST]". For item A, exactly one real test email, to your test address. The due digest and Auto Send reports are never sent for real during testing.
- "Does it touch production?" No. Dev only.
- "How is the limit enforced?" A spending limit set only on the test run itself (never on a server), plus a cap of 4 prep runs for item E's test.

#### Question 38: Order and timing

The question: When should Phase 9 building start, and in what order?

Options:

- (a) Start only after Phase 8 is committed. A and C first; B after the Phase 8 urgent tuning closed (it has); D after B; E only after the browser session releases its shared files.
- (b) Wait until browser v2 is fully released before any Phase 9 work.

Pros and cons: (a) faster, and A and C touch no browser file. (b) simpler coordination, slower.

Recommendation: (a).

Depends on or affects: scheduling only. Question 19 sets item C's own timing inside this order.

Likely follow-ups:

- "When will I see something?" The briefing does not give dates. Sizes are in section 5.
- "Why after the Phase 8 commit?" So Phase 9 changes never mix with unsaved Phase 8 changes in the same files.

#### Question 37: Database change number

(Only if a chosen answer needs a database change; see section 1.)

The question: Which database change number should Phase 9 use?

Why it matters: Numbers set the order changes are applied, so each must be unique. 195 is reserved by the browser session. 197 was free on November 3, both in the code and on the dev database.

Options:

- (a) 197.
- (b) The next free number at write time, announced to the browser session.

Recommendation: (a), falling back to (b) if 197 is taken. It is checked again right before writing and before applying.

Likely follow-ups:

- "Where is it applied?" The dev database only. Never staging or production without your explicit approval.
- "Will I need to be at the screen?" Only if item D or E is built (their part B).

---

### Part 2. Item A: the "system" label on background emails (skip if A not picked)

#### Question 3: Which emails are labelled "system"

The question: Which background emails get the "system" label?

Options:

- (a) The 6:03 AM due digest, the Auto Send check and results reports, the estimator revision alert, bell mirror emails, and the Auto Send proposal when the schedule sends it. Anything a person triggers ("Send now" or any click) stays labelled with the person.
- (b) Only the digests and reports. Not mirror emails, not the scheduled proposal.

Pros and cons: (a) every email with no person behind it is labelled. (b) smaller, but leaves mirror emails and scheduled proposals looking like an unnamed person sent them.

Recommendation: (a).

Depends on or affects: under (b), question 4 and question 5 are not applicable.

Likely follow-ups:

- "Does the email itself change?" No. Only the log entry.
- "What about RFQs, submittals and emails a person sends?" They stay labelled with the person.
- "What about old emails?" Not changed. Emails sent before the change keep their old label (an empty sender can also mean a person whose account was deleted).

#### Question 4: Mirror emails

The question: A bell's mirror email can be caused by a person or by an action the person approved in the assistant. Should mirror emails be labelled "system"?

Options:

- (a) Label them all "system". They are machine-written copies with no human sender; the cause stays on the bell.
- (b) Leave mirror emails alone.

Recommendation: (a).

Depends on or affects: question 3, question 7.

Likely follow-ups:

- "Could an AI-caused mirror email hide under system?" Today mirror emails are sent from a background step that does not know an AI caused them, so they log as human anyway. Question 7 covers that gap.

#### Question 5: Show "Sent by: System" in the project's Notification log

The question: Should a project's Notification log show "Sent by: System" for these emails?

Why it matters: Today those emails show no sender there, so the label alone changes nothing on screen.

In practice: the line can appear only for proposals Auto Send sends on its schedule. The due digest and Auto Send reports belong to no project, so they are in no project's log. Mirror emails, and in practice estimator revision alerts, show under their bell entry.

Options:

- (a) Yes, for scheduled Auto Send proposals: one small change in BDR's server code; the screen itself is not redesigned.
- (b) No. The email log label from question 3 is still correct for all five senders.

Recommendation: (a).

Depends on or affects: not applicable if question 3 is (b).

#### Question 6: Database rule

The question: Should the database itself refuse a "system" email that also names a person or an AI run?

Options:

- (a) No database rule; rely on the email code's guard and tests.
- (b) Add the database rule. You declined this in Phase 8 (question 18). It would be checked against the whole email log once, without blocking new emails.

Pros and cons: (a) no database change. (b) one more safety layer, a database change (no owner at the screen needed).

Recommendation: (a).

Depends on or affects: (b) changes the database.

#### Question 7: The background-step gap

The question: Some emails an assistant action causes ("Failed to bid", bid date change, mirror emails) are sent from a background step that loses track of the AI run, so they are logged as human. Fix it now?

Options:

- (a) Leave it for later.
- (b) Fix it now, so those emails log as "assistant". Side effect: a mirror email caused inside an assistant run would then not get the "system" label (the email code refuses "system" inside an AI run); it would be logged as caused by the assistant instead.

Pros and cons: (a) smaller Phase 9. (b) a more accurate log, more files touched.

Recommendation: (a).

Depends on or affects: question 4.

#### Question 8: Safety-net test

The question: Should a test check every email-sending spot in BDR's code?

Options:

- (a) Yes: every send either names the person, says "system", or is on a short named list of exceptions.
- (b) Only tests per changed file.

Pros and cons: (a) a future new email sender cannot slip through unlabelled. (b) less test code.

Recommendation: (a).

---

### Part 3. Item C: translations (skip if C not picked)

#### Question 17: Reverse "English only" for the 51 Phase 8 texts

The question: Should the 51 Phase 8 screen texts be translated into the five other languages?

Options:

- (a) All 51.
- (b) Only the 15 texts everyone sees (Inbox and Alerts card); leave the 36 admin texts (Executive and IT Admin only, money and safety wording) in English.
- (c) Keep English only (drops item C).

Pros and cons: (a) consistent with Phase 7, which was fully translated. (b) less risk on money and safety wording. (c) nothing to do.

Note: the assistant's reason text and the Teams and digest text stay English under every option, so Inbox rows will mix languages.

Recommendation: (a). This reverses your Phase 8 extra answer "Translations: English only".

Likely follow-ups:

- "Which languages?" Cebuano, Filipino, Hindi, Swahili and Urdu.
- "Who checks the translations?" An AI reviewer, closely on the six money and safety texts. Native review is question 18.

#### Question 18: How to translate and check

Options:

- (a) Five AI translators with a fixed word list from the Phase 7 translations, and an AI reviewer focused on the money and safety texts.
- (b) (a) plus a native or bilingual check of Cebuano and Urdu. Adds about half a day and needs a person.
- (c) Use the AI Estimating team's translation script (it needs that team's test-tracking setup).

Also part of the recommendation: "Triage" may stay English in Cebuano and Filipino, and the Urdu "Checked" time line gets a fix so it reads right to left correctly.

Recommendation: (a).

Likely follow-ups:

- "Were Phase 7's translations checked by a native speaker?" No.

#### Question 19: Timing

Options:

- (a) Right after the Phase 8 commit, before the other items.
- (b) At the end, together with any English texts D or E add.

Recommendation: (a). If D or E add texts later, a second short translation pass can follow.

---

### Part 4. Item B: triage of "Tech problems" bells for IT Admins (skip if B not picked)

#### Question 9: Is a model worth it for health bells, and in what form

The question: Should the AI look at IT Admins' health bells at all, and may it escalate?

Why it matters: BDR already sends IT Admins the bell, an email for most types, and Teams. Any escalation would repeat that, against the guiding principle.

Options:

- (a) Triage with escalation allowed up to high, never urgent, counted in the company fuse.
- (b) Triage without escalation: the assistant only notes "recovered, nothing to do" or suggests a next step on the Inbox row.
- (c) No model: fixed rules pair each "away" with its "back" notice into one row.
- (d) Drop item B.

Pros and cons: (a) can push a "this keeps failing" alert, but duplicates BDR's Teams and needs a second fuse. (b) quiet help, no duplicates, a fraction of a cent a day. (c) no AI cost, but less judgement; not designed in detail. (d) nothing.

Recommendation: (b).

Cost: (b) is the fraction of a cent a day in section 5. The briefing gives no separate figure for (a); (c) uses no AI, so no AI cost.

Depends on or affects: under (d), questions 10 to 16 are not applicable; under (c), see section 1. Under (a), if question 16 is (b), question 16 also adds a second hourly fuse for health escalations (a database change, no owner at the screen).

Likely follow-ups:

- "Can the assistant fix an outage?" No. It has no such tool. Its suggestions are generic (question 12).
- "Will IT Admins get more pings?" Not under (b). Nothing is pushed.

#### Question 10: Which health bells

Options:

- (a) The "Tech problems" family only (AI spending cap, RFP model away and back, RFP step failures, email match failures, BuildingConnected and NGEM scan and login problems).
- (b) Also security alerts.
- (c) Also browser-agent bells (they belong to the browser session's files and carry text from the PC that runs browser tasks).

Cost: the briefing gives no figure for (b) or (c); they would add rows on top of the item B figures in section 5.

Recommendation: (a).

#### Question 11: Who

Options:

- (a) IT Admin only. Executive copies of the AI spending cap and BuildingConnected bells are not recorded in the Executive's Inbox.
- (b) IT Admin and Executive.

Cost: (b) would add the Executive copies (on dev, 223 of the 739 "Tech problems" bells in the last 90 days). The briefing gives no separate cost figure for that.

Recommendation: (a).

Likely follow-ups:

- "Does the Executive stop getting the bell?" No. BDR still sends the Executive the bell, email and Teams. Only the assistant Inbox copy is skipped.

#### Question 12: What the assistant may suggest

Options:

- (a) Only "ask for a short summary" and "set up a follow-up to-do".
- (b) Add health actions such as "open RFP processing" or "check AI spend" (needs new wording and a way for the chat to act on them).

Recommendation: (a).

Likely follow-ups:

- "Can the IT Admin's chat check the model status?" Not today. It has no model-status read tool, so suggestions stay generic.

#### Question 13: What facts the model sees

Options:

- (a) The health type, how many of the same notice in the last 24 hours, whether a recovery was seen, and how many items are waiting.
- (b) Only the health type.
- (c) (a) plus a 30-minute hold on health rows before triage, so an "AI model away" row can already see its "model back" bell. Needs a database change.

Example: without (c), triage looks at an "away" row about 5 minutes after it arrives, before any "back" bell exists, so the away row gets a suggestion while the outage is on, and only the later "back" row shows "recovered, nothing to do".

Cost: the plan's item B cost figures are the same with or without the hold.

Recommendation: (a), without the hold. All facts are counts and yes or no values; never the bell's text.

Depends on or affects: (c) changes the database (no owner at the screen).

Likely follow-ups:

- "Why never the bell's text?" Some health bells carry email subjects, error text from other systems, or account email addresses. Outsiders can control that text.
- "Are Phase 8's test cases affected?" No. Item B gets its own separate set of about 25 cases. The Phase 8 bid set (now 63 cases) is untouched.

#### Question 14: Old health notices and the budget

Options:

- (a) A health notice older than 6 hours is skipped as stale; if the daily budget refuses, health notices are skipped instead of waiting for midnight.
- (b) The bid rules (24 hours, retry after midnight).

Recommendation: (a). A health notice from yesterday is rarely worth a check.

#### Question 15: Show which health event it is on the Inbox row

Options:

- (a) Yes, from a fixed list: "Tech problems: AI model away" instead of only "Tech problems".
- (b) No.

Recommendation: (a).

#### Question 16: The safety fuse

The question: How should health decisions count in the triage safety fuse?

This question applies whatever the answer to question 9 (unless item B is dropped).

Why it matters: the fuse watches the last 50 triage decisions to spot a runaway escalation rate. Health "nothing to do" decisions would sit in that same sample. During a bid day where the AI wrongly escalates a lot, a stream of health decisions could keep the rate below the trip point. Example: 30 health decisions plus 20 bid decisions with 15 escalations is 30 percent, so no trip, although 15 of 20 bid decisions escalated.

Options:

- (a) Shared sample; accept the dilution.
- (b) Leave health decisions out of the fuse sample (a small database change, no owner at the screen, no new setting). Under question 9 (a), also a separate hourly limit for health escalations.

Recommendation: (b), once item B is switched on anywhere other than dev.

Depends on or affects: (b) changes the database.

---

### Part 5. Item D: teammate notice (skip if D not picked)

#### Question 20: Take D, and in what form

The question: Should an escalation also reach a teammate, and how?

Options:

- (a) Notice only: the router tells a teammate; no task is assigned, no permissions move; the teammate acts under their own role. Open decision 4 (real task assignment) stays open.
- (b) Real task assignment (a different, larger feature; not designed).
- (c) A fixed rule instead of the AI: if an escalation sits unread for some hours, the router tells the Estimating Admin (not designed in detail).
- (d) No D.

Pros and cons: (a) a second pair of eyes with tight limits. Con: one bad AI answer can reach several people. (b) most powerful, much bigger. (c) predictable, no AI choice. (d) no new risk.

Recommendation: (d) for now; if you want it, (a). This reverses your Phase 8 answer 7.

Depends on or affects: under (d), questions 21 to 30 are not applicable. Under (b) or (c), Claude Code returns with a new design.

Likely follow-ups:

- "Does the teammate's assistant message my assistant?" No. No assistant ever messages another person's assistant. The router writes a fixed notice; the teammate's assistant can only see it as that fixed text.

#### Question 21: Who may be reached (and how many people)

Options:

- (a) The role that owns the missing part (Materials engineer for quotes, Labor engineer for labor, Estimating Admin for markups), the Estimating Admin as coordinator, an Executive as last resort. Never IT Admin, Accountants, outside estimators or the sender.
- (b) Only the Estimating Admin.
- (c) Only an Executive.

There is no per-project assignee, so everyone active in the role is reached. Please give real headcounts per role (how many Materials engineers, Labor engineers, Estimating Admins and Executives). Dev has 1 Estimating Admin, 1 Executive and no engineers; production was not looked at.

Recommendation: (a).

Depends on or affects: question 29 (the fuse number is set from your headcounts).

#### Question 22: Does the sender still get their own escalation

Options:

- (a) Yes, both the sender and the teammate.
- (b) Only the teammate.

Recommendation: (a).

Depends on or affects: changes the database text.

#### Question 23: Urgent and Teams for a teammate notice

Background: urgent is never allowed for a teammate notice (that would mean Teams at any hour to someone who did not ask for it). It is capped at high, so the teammate's quiet hours always apply. Teams is on for everyone unless they turned it off, so a high notice would also ping the teammate on Teams during working hours.

Options:

- (a) High with Teams in working hours, held at night. A notice that was held and goes out on a weekend or holiday goes in-app only (no Teams).
- (b) Normal by default (in-app plus the digest, no Teams); the Alerts card lets a person raise it to "Alert" (four choices: Alert, Digest, Inbox, Off).
- (c) High in-app (bell and badge) with no Teams message for this kind.

Cost: Teams messages cost nothing, so this is about noise, not money.

Recommendation: (c), or (b) if you want people to be able to opt in to Teams.

Depends on or affects: (b) adds choices to the Alerts card, which means new screen text; if item C is picked, question 19 (b) would translate it in the same pass.

#### Question 24: Repeat limit

Options:

- (a) Each person gets at most one teammate notice per project per day, whatever the reason or however many assistants flagged it (the day runs 6 AM to 6 AM Pacific so a late-night flag and an early-morning flag count once); and at most one notice per project, audience and day. A notice withdrawn because the switch went off or the fuse tripped does not block a later valid one.
- (b) No limit (your Phase 8 answer 10, "no per-person limit", extended to teammates).

Recommendation: (a). It is a structural limit on repeats, not a limit on the AI.

Depends on or affects: changes the database text.

#### Question 25: What the notice says about the sender

Options:

- (a) The sender's role ("the Materials engineer's assistant").
- (b) "A teammate".
- (c) The person's name.

Recommendation: (a).

#### Question 26: Separate company switch for teammate notices

Options:

- (a) Yes, default off, controlled by an Executive or IT Admin.
- (b) No, it rides on the triage company switch.

Recommendation: (a). You can run triage without teammate notices.

Depends on or affects: changes the database.

#### Question 27: Nobody eligible

The question: What if no one in the chosen role can receive it (for example their assistant is off)?

Options:

- (a) Do nothing; count it on the admin block.
- (b) Fall back to the sender's own escalation (already sent under question 22 (a)).
- (c) Try the next audience.

Recommendation: (a).

Depends on or affects: question 22. Option (b) assumes question 22 is (a); if 22 is (b), the sender gets no escalation of their own, and the briefing doesn't say how (b) would then work.

#### Question 28: The sender's card says "Also flagged to the Estimating Admin"

Options:

- (a) Yes, shown only when the notice actually reached someone.
- (b) No.

Recommendation: (a).

#### Question 29: Fuse for teammate notices

Options:

- (a) A separate hourly limit for teammate notices that counts each person reached. When it trips, it stops only teammate notices; your own triage keeps running. Its number is set from your headcounts (question 21), and the switch cannot be turned on until it is set.
- (b) Count people reached in the shared triage fuse (30 an hour), where one busy bid day can pause all triage for everyone until an admin resets it.
- (c) Count decisions only.

Recommendation: (a).

Depends on or affects: question 21; question 26 (the "switch" in option (a) is the teammate switch from question 26 (a); if 26 is (b), the briefing doesn't say how the rule works); changes the database.

Likely follow-ups:

- "What number?" The plan says it is set from your headcounts but gives no formula. Ask Claude Code to propose a number for your approval.

#### Question 30: Design-record wording

Background: the design record says "no assistant ever messages another user's assistant". That rule stays. Item D adds that a router notice about another person's escalation is one of the allowed system notifications. The recipient's own assistant can see the notice in their Inbox list, but only as the router's fixed text (no sender name, no reason, no triage detail).

Options:

- (a) Claude Code rewords the design record after the browser session releases the file.
- (b) Leave the wording.

Recommendation: (a). Documentation only.

---

### Part 6. Item E: Stage 2 prep notes (skip if E not picked)

#### Question 31: Take Stage 2, and in what form

The question: Should the assistant run on its own to prepare notes for some suggestions?

Options:

- (a) No. Phase 8 stays as it is (decide and suggest only).
- (b) Notes only: a read-only prep run leaves private notes and a "Notes ready" link.
- (c) Notes plus drafted approval cards (to-dos or emails) left waiting for the person. Needs a card lifetime decision this briefing does not hold.

Cost: about 3 to 10 cents typical per prep run, never more than 25 cents. 30 a day is about 90 cents to 3 dollars typical, up to 7.50 dollars worst case (2 to 6 percent typical, 15 percent worst, of the 50 dollar company budget).

Pros and cons: (a) no new cost or risk. (b) real prep work waiting, still no writes. (c) most help, but AI-written text on cards and more risk.

Recommendation: (a) for now; revisit once Phase 8's suggestions are judged good. If you want it now, (b). This reverses your Phase 8 answer 1.

Depends on or affects: under (a), questions 32 to 36 and 39 are not applicable.

Likely follow-ups:

- "Is this the first time the assistant runs on its own with tools?" Yes. It gets a fresh security review, and it is treated as untrusted from its very start.
- "Can it send or change anything?" No. Read tools only, no cards, no mail, no web.

#### Question 32: Whose money

Options:

- (a) The company budget only, under its own spending label (like Phase 8 triage; needs a database rule change with you at the screen).
- (b) The person's 15 dollar daily budget (no spending database change, but a background job eats into a person's chat budget).

Note: item E needs you at the screen for its other database parts anyway, so (b) does not avoid that.

Recommendation: (a).

#### Question 33: Which suggestions start a prep run

Options:

- (a) "Review the changed drawings" and "review the revision" only.
- (b) Also "ask for a short summary" (the most common suggestion; roughly triples the cost).

Cost: (a) is about 90 cents to 3 dollars a day in the production estimate. (b) is about 90 runs a day, 2.70 to 9 dollars a day typical and up to 22.50 dollars, but the daily limit of 30 runs (question 36 (a)) would still hold spending to 7.50 dollars a day at most, so under (b) the limit would be reached most days.

Recommendation: (a).

Depends on or affects: question 36.

#### Question 34: Model for prep runs

Options:

- (a) A named model, G P T five point four by default, set by a server setting that may later name a cheaper priced model, never one priced above G P T five point four. Using "the person's chat model" would let a pricier chat model (such as G P T six astra) break the cost limits.
- (b) A cheaper model now.

Cost: all of item E's figures assume G P T five point four. The briefing does not name or price a cheaper model.

Recommendation: (a).

#### Question 35: Can the person reply inside the notes

Options:

- (a) No, read-only. "Continue in a new chat" opens an empty chat; notes move only when the person copies and pastes them by hand.
- (b) Yes, it becomes a normal chat that stays marked untrusted.

Recommendation: (a). It keeps untrusted notes from flowing into a normal chat by accident.

#### Question 36: Daily limit on prep runs

Options:

- (a) At most 30 a day for the whole company (a cost limit, not a limit on reaching anyone).
- (b) No limit.
- (c) Another number.

Also: a prep run that fails still uses its slot for the day. Say if you want failed runs to give their slot back.

Cost: under (b), the only limits left are the 25 cent stop per run and the 50 dollar company budget shared with chat and triage; the briefing gives no daily figure for that case.

Recommendation: (a), and failed runs keep their slot. At 25 cents worst case per run, 30 a day caps prep spending at 7.50 dollars a day.

Depends on or affects: changes the database.

#### Question 39: Background notes under your chat permission

Background: the assistant may only read BDR data for someone while that person has a live permission grant, which they create themselves when they use the chat (with two-factor sign-in). Phase 8 triage needed none because it reads nothing with tools. Prep runs do read with tools.

Options:

- (a) A prep run happens only while the person has a live grant from their own chat use. No grant, no notes. No personal off switch (cancelling the grant is the off switch).
- (b) As (a), plus a per-person "Prepare notes for me" switch, default on.
- (c) A separate background grant just for prep runs.

Recommendation: (a) or (b).

Note: your Phase 8 answers "no personal opt-out" and "no permission needed" covered triage without tools only.

Depends on or affects: (b) or (c) changes the database.

Likely follow-ups:

- "What happens to someone who never uses the chat?" Under (a), they have no grant, so they get no prep notes. Their triage suggestions still appear.

---

## 7. Open decisions from the assistant's design record

The assistant's design record has a list of open decisions. Some are pure owner choices; others were shipped with a default and only need a nod. Item D's question 20 already answers open decision 4 (task assignment between people) in its narrow "notice only" form. These do not depend on which items are picked. Go through them quickly; "nod" means "keep it as recommended or as built".

### Pure owner choices (answer, or leave open)

- Open decision 1. When an Executive's or IT Admin's assistant reads someone's mailbox: tell the mailbox owner each time, or only keep the access log plus the weekly mailbox-access digest (which goes to Executives and IT Admin). Recommendation: the log plus the weekly digest, as built.
- Open decision 2. The exclusion list: mailboxes no Executive or IT Admin assistant may read. Today it is empty, so every mailbox can be read, including yours. HR, legal and bookkeeping addresses are candidates. Whatever you name must match tags set in Exchange. Recommendation: you name the list (even if empty) and initial it in the decisions log.
- Open decision 3. Assistant-sent mail already carries a "Sent by assistant" footer. Should mail to GCs and vendors also carry a short line saying AI helped write it? Recommendation: yes, one short line.
- Open decision 5 (bell part only). When IT Admin reads a person's assistant conversation, the conversation already shows "Viewed by IT Admin on" a date. Should the person also get a bell? Recommendation: no bell; the banner already shows it.
- Open decision 9. The pilot group and success thresholds for switching assistant features on in production. Recommendation: the Phase 8 pilot (one Executive and one engineer of each kind for a week), with the triage quality pass marks as the thresholds.
- Open decision 13. The project email mailbox: mail BDR reads from your personal mailbox is visible only to you, Executives and IT Admins. Either point BDR at a shared mailbox such as the office mailbox, or build reading from several mailboxes. Recommendation: the shared office mailbox. (The design record says this was to be settled inside an earlier security ticket; the briefing doesn't say whether that already happened.)
- Open decision 15. The address of the admin assistant shared mailbox. Please confirm: adminassistant at g3electrical dot com.
- Open decision 16. Should Executive and IT Admin assistants see every RFP intake row, or keep today's rule (each person sees rows from the mailboxes they receive)? Recommendation: keep today's rule.

### Shipped by default (nod or change)

- Open decision 8. Calendar: the assistant looks only at calendar events tied to BDR, not every event coming up.
- Open decision 11. Plan sheet notes from AI Estimating become readable by every internal role once AI Estimating is fully live in production; reading the raw plan pages stays restricted.
- Open decision 12. The company holiday list as seeded: November 26 and 27, 2026; December 25, 2026; January 1, 2027; May 31, 2027; July 5, 2027; September 6, 2027; November 25 and 26, 2027; December 24, 2027. Executives and IT Admins can edit it in Settings.
- Open decision 14. In the project emails page, the Accountant sees their own mailbox plus the shared mailboxes (rather than shared only).
- Open decision 18. In the project emails page, Executives and IT Admins see every mailbox BDR reads, minus the exclusion list, with an audit record of each view.
- Open decision 19. The Accountant's assistant may send email to other users and outside contacts under the usual approval card (rather than only notifying themselves).

### Not asked here (for reference)

- The other half of open decision 5 (how long conversations, messages, tool results, memories and staging are kept) is still marked "proposed defaults, owner to confirm" in the design record. It is outside Phase 9. The owner may confirm it separately.
- Open decisions 6 and 7 (which sites the browser may log in to) belong to the browser work. Open decision 10 is overtaken by how the assistant already behaves. Open decision 17 is closed. If the owner wants to confirm that reading: "nod".

---

## 8. Phase 8 follow-ups: confirm or change

When the owner answered the Phase 8 questions, eleven smaller points were left open. Claude Code built each one as recommended on dev. Confirm each ("nod") or change it. None of the alternatives needs a database change except follow-up 11's.

- Follow-up 1. A closed project (abandoned, failed to bid, outcome recorded) still blocks a triage escalation. Alternative: deliver it anyway.
- Follow-up 2. Only a person's own mute and "not useful" marks lower the level the AI chose; there is no level picker for escalations. Alternative: let a person's own level act as a ceiling.
- Follow-up 3. The pilot list stays as a rollout lever, empty by default (meaning everyone). Alternative: remove it and go company-wide at once.
- Follow-up 4. The safety fuse counts urgent escalations like any other. Alternative: count urgent twice, or add an urgent-only hourly limit.
- Follow-up 5. The escalation reason picks the wording. A factual phrase such as "your part is still missing" appears only when the router's own check finds it true; otherwise neutral wording ("Your assistant flagged an update on bid 26.11.7301"). The escalation is delivered either way. Alternative: always show the reason's full wording, or drop reasons.
- Follow-up 6. The triage model's price is the one you gave: 10 cents per million input tokens, 1 cent per million for repeated input the provider has already seen recently ("cached" input), 50 cents per million output tokens. (The browser work lists a different price for the same model; that belongs to the browser session, not to this follow-up.)
- Follow-up 7. Triage keeps checking, and spending company money, for a person who muted the escalation kind; mute stops pushes only, and suggestions still appear. Alternative: skip people who muted it.
- Follow-up 8. A row retried after midnight (after a budget stop) may still escalate urgent at about 12:05 AM. Alternative: cap retried rows at high.
- Follow-up 9. When the fuse trips or the company switch goes off, escalations not yet delivered are withdrawn. Alternative: deliver what was already decided and stop only new triage.
- Follow-up 10. Rows retried after a budget stop count in the fuse, and retries are spread from midnight to 2 AM. Alternative: retry all at midnight and leave them out of the hourly count.
- Follow-up 11. Rows that arrive while the fuse is tripped or the switch is off are not checked after it is cleared. Alternative: check those still under 24 hours old (a database change).

---

## 9. Extra item (not numbered): release watch limits

The plan's production release checklist says that, for each Phase 9 item switched on, Claude Code watches it for a week, and that the owner sets a limit for each thing watched: for example, for item B the number and cost of health decisions per day; for item D the notices by audience, notices that found no one, withdrawn and failed notices, fuse trips and Teams sends; for item E prep runs per day and their outcomes, prep spending, peak money held against the 50 dollar budget, and how long chat waits behind prep runs. Item A has no switch; it is watched for any email that failed to send.

The plan gives no numbers. Options: (a) Claude Code proposes the numbers after the dev live test and you approve them at release time; (b) you give numbers now.

Recommendation: the briefing doesn't hold one. Option (a) is the simplest. Record the answer in the extra notes.

---

## 10. Answer sheet

Fill in the letter (and any condition or number). The recommended letter is shown in brackets. Use "skipped", "deferred" or "not applicable" where needed.

Part 1. The menu and the basics

1. Which items: ___ (recommended A, then C, then B; D and E only if wanted)
2. Live-test budget: ___ (recommended a: 1 dollar total without E, 3 dollars with E); amount: ___ dollars
38. Order and timing: ___ (recommended a)
37. Database change number: ___ (recommended a, 197, falling back to b; not applicable if nothing chosen needs a database change)

Part 2. Item A (not applicable if A not picked)

3. Which emails are labelled system: ___ (recommended a)
4. Mirror emails: ___ (recommended a)
5. "Sent by: System" in the Notification log: ___ (recommended a)
6. Database rule: ___ (recommended a)
7. Background-step gap: ___ (recommended a)
8. Safety-net test: ___ (recommended a)

Part 3. Item C (not applicable if C not picked)

17. Translate the 51 texts: ___ (recommended a)
18. How to translate and check: ___ (recommended a)
19. Timing: ___ (recommended a)

Part 4. Item B (not applicable if B not picked)

9. Model for health bells, and form: ___ (recommended b)
10. Which health bells: ___ (recommended a)
11. Who: ___ (recommended a)
12. What it may suggest: ___ (recommended a)
13. Facts: ___ (recommended a, no hold)
14. Old notices and the budget: ___ (recommended a)
15. Health event on the row title: ___ (recommended a)
16. Safety fuse: ___ (recommended b, once B is on outside dev)

Part 5. Item D (not applicable if D not picked)

20. Take D, and form: ___ (recommended d for now; if wanted, a)
21. Who may be reached: ___ (recommended a); headcounts: Materials engineers ___, Labor engineers ___, Estimating Admins ___, Executives ___
22. Sender still gets their own escalation: ___ (recommended a)
23. Urgent and Teams: ___ (recommended c, or b)
24. Repeat limit: ___ (recommended a)
25. What the notice says about the sender: ___ (recommended a)
26. Separate company switch: ___ (recommended a)
27. Nobody eligible: ___ (recommended a)
28. "Also flagged to" line: ___ (recommended a)
29. Teammate fuse: ___ (recommended a); number, if given: ___
30. Design-record wording: ___ (recommended a)

Part 6. Item E (not applicable if E not picked)

31. Take Stage 2, and form: ___ (recommended a for now; if wanted, b)
32. Whose money: ___ (recommended a)
33. Which suggestions start a prep run: ___ (recommended a)
34. Model for prep runs: ___ (recommended a)
35. Reply inside the notes: ___ (recommended a)
36. Daily limit: ___ (recommended a, 30, failed runs keep their slot); number if different: ___
39. Background notes under your chat permission: ___ (recommended a or b)

Open decisions (section 7)

- 1. Mailbox read notice: ___ (recommended: log plus weekly digest)
- 2. Exclusion list: ___ (recommended: you name it, even if empty, and initial it); mailboxes: ___
- 3. AI disclosure line to GCs and vendors: ___ (recommended: yes, one short line)
- 5. Bell on IT Admin conversation reads: ___ (recommended: no bell)
- 9. Pilot group and thresholds: ___ (recommended: Phase 8 pilot, triage pass marks)
- 13. Project email mailbox: ___ (recommended: shared office mailbox)
- 15. Admin assistant mailbox address: ___ (confirm adminassistant at g3electrical dot com)
- 16. RFP intake rows for Executive and IT Admin assistants: ___ (recommended: keep today's rule)
- 8. Calendar default: ___ (nod or change)
- 11. Plan sheet notes readable by every internal role when AI Estimating is fully live: ___ (nod or change)
- 12. Holiday list: ___ (nod or change)
- 14. Accountant mailboxes in project emails: ___ (nod or change)
- 18. Full project email view for Executive and IT Admin, with audit: ___ (nod or change)
- 19. Accountant's assistant may email others under approval: ___ (nod or change)
- 6, 7, 10, 17 reading (browser, overtaken, closed): ___ (nod or change)
- 4. Task assignment between people: answered by question 20 (record "see question 20")
- 5, retention half (optional, outside Phase 9): ___ (leave open, or confirm the design record's proposed defaults)

Phase 8 follow-ups (section 8): nod or change

- 1: ___  2: ___  3: ___  4: ___  5: ___  6: ___  7: ___  8: ___  9: ___  10: ___  11: ___

Extra item, release watch limits: ___ (simplest: Claude Code proposes, you approve at release)

Extra notes, conditions or questions for Claude Code: ___
