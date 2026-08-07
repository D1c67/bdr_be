-- 0103 — Selection IS the price. Removes price precedence entirely.
--
-- Until now a category's materials figure was inferred by a fallback chain:
--
--     rfqs.custom_amount  >  the selected quote  >  the lowest quote received
--
-- That chain is gone. A category's price is the amount on its SELECTED quote and
-- nothing else. Every candidate number is a `quotes` row — arrived by email, typed in
-- by hand (a user may enter as many as they like), or pulled off the estimate for
-- General Material — and picking the winner among them is the entire job of the new
-- Select Vendors step. No selection means no price, which is what holds that step open.
--
-- The migration therefore has to MATERIALIZE each project's current effective winner
-- as a real selection. Without this, every bid priced by the fallback (most of them —
-- a lowest-quote default was never recorded anywhere) would silently lose its
-- materials figure the moment the new rule takes effect.

-- ── Candidate provenance ─────────────────────────────────────────────────────
-- Where a candidate number came from. Only affects how a row is labelled and
-- iconed in the UI; it confers NO pricing priority whatsoever.
alter table quotes
  add column if not exists origin text not null default 'vendor'
    check (origin in ('vendor', 'manual', 'estimate'));

-- A hand-entered figure or the estimate's wiring number has no vendor behind it.
alter table quotes alter column vendor_id drop not null;

-- ── 1. Hand-entered category overrides become selected manual quotes ─────────
-- custom_amount was carried as-is with no tax added on top, so the materialized
-- row records tax as already included to reproduce that figure exactly.
insert into quotes (rfq_id, vendor_id, amount, is_selected, is_approved, approved_at,
                    tax_included, origin, notes, received_at)
select r.id, null, r.custom_amount, false, true, now(),
       true, 'manual', 'Migrated from the category''s hand-entered price', now()
  from rfqs r
 where r.custom_amount is not null
   and not exists (
     select 1 from quotes q where q.rfq_id = r.id and q.origin = 'manual'
   );

-- ── 2. General Material's estimate figure becomes a selected candidate ───────
-- It was never a quote, but under the new rule it has to be selectable like any
-- other candidate. Its own tax attestation carries over unchanged.
insert into quotes (rfq_id, vendor_id, amount, is_selected, is_approved, approved_at,
                    tax_included, tax_rate, origin, notes, received_at)
select r.id, null, g.amount, false, true, now(),
       g.tax_included, g.tax_rate, 'estimate',
       'Migrated from the estimate''s wiring figure', now()
  from rfqs r
  join material_categories mc on mc.id = r.material_category_id and mc.is_general
  join general_material_estimates g on g.project_id = r.project_id
 where g.amount is not null
   and not exists (
     select 1 from quotes q where q.rfq_id = r.id and q.origin = 'estimate'
   );

-- ── 3. Elect exactly one winner per RFQ, reproducing TODAY'S price exactly ───
-- The governing rule is PRESERVE, NEVER RE-DECIDE:
--
--   • a quote a human already selected stays the winner — it must never be demoted
--     in favour of a cheaper one, or bids silently reprice (on the dev data this
--     alone would have dropped a Switchgear category from $205,500 to $5,696), and
--   • a category that has NO price today must not acquire one. Only categories the
--     old fallback actually priced get a winner materialized.
--
-- Which is why General Material only ever elects its estimate row here: today its
-- figure comes from the estimate and its vendor quotes are ignored entirely, so
-- electing a vendor quote would invent a number nobody chose. Those quotes are still
-- there and fully selectable — a human just has to make that call on Select Vendors.
create temp table _bdr_winner on commit drop as
with ranked as (
  select q.id,
         row_number() over (
           partition by q.rfq_id
           order by
             -- An existing human decision outranks everything.
             case when q.is_selected then 0
                  when q.origin = 'manual' then 1
                  when q.origin = 'estimate' then 2
                  else 3 end,
             -- then the old lowest-received default, compared (as pricing always
             -- did) on the tax-INCLUSIVE total. This mirrors apply_tax exactly: an
             -- unanswered tax question counts as "not included", and the tax is
             -- rounded to cents BEFORE being added, not after.
             case when q.tax_included then q.amount
                  else q.amount + round(q.amount * coalesce(q.tax_rate, 8.375) / 100, 2)
             end,
             q.received_at,
             q.id
         ) as rn
    from quotes q
    join rfqs r on r.id = q.rfq_id
    join material_categories mc on mc.id = r.material_category_id
   where not mc.is_general or q.origin in ('estimate', 'manual')
)
select id from ranked where rn = 1;

-- Clear first: quotes_one_selected_per_rfq is a partial UNIQUE index, so setting the
-- winners while stale selections are still standing would collide.
update quotes set is_selected = false where is_selected;

update quotes q
   set is_selected = true
  from _bdr_winner w
 where w.id = q.id;

-- rfqs.custom_amount is intentionally LEFT IN PLACE but is no longer a pricing input;
-- step 1 copied every value into a real quote row. It is kept only so the migration
-- is auditable after the fact. Nothing in the application reads it anymore.

notify pgrst, 'reload schema';
