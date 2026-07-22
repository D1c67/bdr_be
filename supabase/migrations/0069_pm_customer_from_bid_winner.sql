-- 0069_pm_customer_from_bid_winner.sql
--
-- Backfill: a won bid's GC is the GC over the project in PM.
--
-- activate_pm_for_win has always copied the winning GC onto pm_details as the
-- customer, but "Won" could be recorded without naming a winner (the Win/Loss
-- selector was labelled Optional and defaulted to "Unknown"), and the handoff
-- faithfully copied that NULL. Those projects landed in Preconstruction with no
-- GC over them — the Customer and Winning GC rows both render "—".
--
-- The code path is now closed on both ends (services/outcome.record_outcome
-- rejects a win with no winning GC; services/pm.reconcile_pm_customer adopts a
-- late-recorded winner onto a GC-less PM record). This heals the rows already
-- stranded, wherever the winner IS on record.
--
-- Only blanks are touched: a customer name typed by hand outranks the GC's
-- company name and is left alone. Idempotent — re-running matches nothing.
--
-- NOT healed here: a won project whose winning_gc_id was never recorded at all.
-- No amount of SQL knows which GC that was; a human records the winner in the
-- Win/Loss panel and reconcile_pm_customer fills the PM record from there.

update pm_details d
set customer_gc_id = bo.winning_gc_id,
    customer_name  = coalesce(nullif(btrim(d.customer_name), ''), gc.name),
    updated_at     = now()
from bid_outcomes bo
join general_contractors gc on gc.id = bo.winning_gc_id
join projects p on p.id = bo.project_id
where d.project_id = bo.project_id
  and bo.result = 'won'
  and bo.winning_gc_id is not null
  and d.customer_gc_id is null
  -- Bid-origin only: a direct/pm_only project's customer was chosen deliberately.
  and p.pm_origin = 'bid'
  and p.pm_stage is not null;
