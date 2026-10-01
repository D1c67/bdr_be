# BuildingConnected fixtures

Anonymised from the live pull of 2026-09-26 (names, GCs, ids, emails, addresses and text replaced; keys, nulls, dates and nesting kept exactly as the API returned them). Frozen 'now' for these rows: 2026-09-26T12:00Z.

| # | _fixture_role | why |
|---|---|---|
| 0 | open_undecided_full | active, future due, every optional field set (job walk, start/finish, trade instructions) |
| 1 | with_trade_instructions | tradeSpecificInstructions and rfisDueAt set (the two-section Project details) |
| 2 | open_undecided_min | active, future due, optional fields null |
| 3 | open_accepted | WILL_SUBMIT (Accepted bucket), future due |
| 4 | budget_request | requestType BUDGET -> the B suffix |
| 5 | no_due_notice | no due date, a GC outreach notice (must park in review, never auto-create) |
| 6 | no_due_real | no due date but a real invitation |
| 7 | nda_masked | isNdaRequired with client/updatedAt/location/projectInformation null (the masked shape) |
| 8 | past_due_accepted | unarchived Accepted row long past due (historical at backfill) |
| 9 | submitted_active | SUBMITTED with bid{} present |
| 10 | declined_archived | DECLINED + archived + declineReasons |
| 11 | archived_undecided | archived UNDECIDED (historical) |
| 12 | foreign_manual | source MANUAL: clientValues null |
| 13 | foreign_email | source EMAIL: clientValues null |
| 14 | group_parent | isParent true with groupChildren |
| 15 | group_child | parentId set |
| 16 | sealed | isSealedBidding true |
| 17 | same_gc_two_packages_a | same GC + same name as _b, different tradeName, invited the same day (one project) |
| 18 | same_gc_two_packages_b | see _a |
| 19 | rebid_old | same GC + same name as rebid_new, years apart |
| 20 | rebid_new | see rebid_old |
| 21 | cross_gc_a | same project name from three GCs |
| 22 | cross_gc_b | see cross_gc_a |
| 23 | cross_gc_c | see cross_gc_a |

`users_me.json` is a GET /users/me shape with viewAll true.

Rows carry an extra `_fixture_role` key that the real API does not have; strip it or ignore it in the client.
