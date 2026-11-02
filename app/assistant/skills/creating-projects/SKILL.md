---
name: creating-projects
description: Use when the user wants to create a new project in BDR.
metadata:
  requires_flags: [bidding]
  roles: [estimating_admin, executive, it_admin, estimating_engineer_materials, estimating_engineer_labor]
  version: "1"
---

# Creating projects

The tool is create_project_draft. It prepares an approval card; the project is created only when the user approves it.

## Every value comes from the user

Ask the user for every field. Never copy a value from an RFP email, a BuildingConnected invite, a note or any other tool result, even when the user says "use what is in the email". If the user pastes text, use only what they typed in their message.

Never propose with a missing required value. Ask for all the missing ones in one short message instead.

## Required

- name: the project name.
- The four dates, as Pacific local time YYYY-MM-DDTHH:MM (for example 2026-11-10T14:00): internal_bid_at (the team's own deadline), invitation_at (when the invitation came in), due_from_estimator_at, due_from_vendors_at. When the user gives a date without a time, ask for the time.
- The bidding URL, or that the project has no bidding link (no_bidding_url true). Never both.
- The nine Go/No-Go answers, each one of its listed values:
  - project_type: what kind of job (new_construction, ti for tenant improvement, multi_family, casino_strip, casino_other, lighting, roadway, generator, other, unknown).
  - owner_type: who owns it (rtc, doa, ccsd, public_other, casino_strip, casino_other, private_commercial, private_residential, other, unknown).
  - labor_needed: union, ce_cw, ce, cw, non_union, other or unknown.
  - bid_method: hard_bid, cmar, single_gc_hard_bid, other or unknown.
  - competitor_known: whether we know the other electrical contractors (yes_1_2, yes_3_plus, no_unknown, only_ec_bidding, other).
  - gc_known: whether we know the GCs bidding (yes_1_2, yes_3_plus, no_unknown, only_gc_bidding, no_gc_needed, other).
  - subs_needed: no, yes_underground, yes_low_voltage, yes_fire_alarm, two_subs, three_plus_subs, other or unknown.
  - est_value_band: the value of our scope (under_50k, 50k_150k, 150k_500k, 500k_1m, 1m_3m, over_3m, other, unknown).
  - scope_fit: whether the scope fits G3 Electrical (yes, no, maybe, other, unknown).
  Map the user's plain words to a value only when it is clear; otherwise ask, and offer "unknown" when they do not know.

## Optional

- budgetary: true for a budgetary estimate; the project number then ends in B.
- actual_bid_at: only roles that may see the actual bid date can set it. Leave it null unless the user gives it.
- Estimated start and finish dates (YYYY-MM-DD), address, labor time, wage type, labor note, notes, bid notes, NGEM, rebid.
- Labor time is day_work or night_work; wage type is prevailing_wage or non_prevailing_wage. Send the code (plain words such as night work are saved as the code). Send other text only when the user asks for a custom value.
- GCs: find each GC with app_search (kind gcs) and its contacts with list_gc_contacts. Add a needs-by date per GC when the user gives one.

## The card

- The card lists similar projects and possible rebids. Point them out and ask whether this is the same job before the user approves.
- After approval the project number appears on the card. Until then say "I have prepared" or "waiting for your approval", never that the project exists.
- A project created by mistake is discarded in BDR (the Discard button in the Activity tab, or the project page), not by the assistant.
