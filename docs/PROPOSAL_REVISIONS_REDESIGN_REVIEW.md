# Revise proposal redesign: independent backend review

Reviewed 2026-10-25 against the uncommitted `urgent-updates` backend diff and migration 0179. This report covers code review and automated tests, not completion of live API or browser E2E testing.

## Findings and fixes

| Severity | Finding in the reviewed implementation | Resolution |
| --- | --- | --- |
| High | `approve_costs` published approval before the verification snapshot was saved. Concurrent generation or dispatch could observe approved state with the old committed figures. | Commit first, then publish approval using the commit's start time. Pending revisions defer initial-document regeneration until after approval is published. A racing cost edit leaves approval pending. |
| High | Cancel/Done retained upstream edits but a subsequent revision did not inherit their pending approval requirement. Its documents could use the old committed figures without requesting approval. | Starting a revision carries unresolved cost-change time, actor and reason from the latest revision. |
| Medium | Pending state was written only through a best-effort after-edit hook. A failed stamp could leave persisted cost edits without an approval requirement. | Cost-affecting handlers now persist pending state before mutation, after validation. Stamp failures prevent mutation. The after-edit stamp remains to catch a concurrent approval. Candidate quote additions remain exempt. |
| Low | The route-level lock could disclose an open revision before authentication ran. | The lock now depends on authentication. An unauthenticated locked write returns 401. |

The pre-edit stamp also refuses an update that returns no rows because the revision closed after its lookup. It does not silently continue with the cost mutation in that case.

## Other review coverage

- Checked revision-context dependencies against the pricing, quote, RFQ, general-material, BOQ confirmation, scope and proposal write routes. The maintained route inventory is asserted against the registered FastAPI routes.
- Checked that initial proposals for newly added GCs also honor pending revision approval.
- Checked override snapshot/clear/restore behavior and the Cactus Ridge pricing default case. Held figures are read from live `proposal_sends`, separately from the project cost snapshot.
- Inspected 0179's open-revision data migration and freshness checks after override clearing. `start_gc_ids` already exists in committed migration 0178; this review added no schema changes.
- Checked the v1 send lifecycle: one live `proposal_sends` row per project/GC, preserved original submission stamps, revision-row-first SQL locking for claim/apply/finish, stuck-send handling, and refusal to close around an in-flight email. No additional confirmed regression was found in these paths.

## Automated validation

The expanded targeted run passed 241 tests across revisions, reverify, verification security gates, general material, pricing, late quotes and BOQ corrections. A subsequent revision-only run passed 82 tests, including the additional revision-closed-during-pre-stamp regression. Full Ruff passed before that final narrow addition; Ruff on the modified service/tests and the whitespace check also passed afterward. The coordinator runs the final whole-suite gates.

New regressions exercise dispatch refusal during an unfinished approval commit, commit failure, an edit during approval, pending costs carried across Cancel/start, pre-stamp failure preventing quote selection, after-stamp failure retaining pending state, authentication before revision lookup, and a revision closing before the pre-stamp update.

## Residual atomicity limit

Cost persistence, revision stamps, verification commits and dispatch checks remain separate database operations. They do not share a transaction or a common database lock. Specifically, an approval can start after a cost edit's pre-stamp but read the old costs before that edit persists. The normal after-edit stamp makes the revision pending again. If that after-edit stamp also fails, the pre-stamp can precede the approval timestamp and fail to preserve the pending requirement. A dispatch already beyond its checks can also finish while a later edit arrives.

The added tests cover ordinary interleavings and single stamp failures, not a proof of strict serialization under combined concurrent operations and database failures. Eliminating this limit requires transactional cost/version coordination with approval and dispatch claims; no such schema redesign was introduced here.

## End-to-end follow-up

The coordinator's dev lifecycle test found that a GC removed and re-added
could not generate an initial proposal: the old unsent row was `superseded`,
but generation only updated `generated` or `failed` rows. Generation now
compares against the exact status it read, allowing an already-retired row
to be reused while still refusing a retirement or send that races generation.
Two regressions cover successful retired-row reuse and a concurrent
retirement remaining protected. This follow-up was verified by the coordinator
after the independent review.
