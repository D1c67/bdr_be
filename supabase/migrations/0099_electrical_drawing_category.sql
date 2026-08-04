-- 0099: 'electrical_drawing' as a first-class file_category.
--
-- Splits the drawings bucket in two. 'drawing' (the original bucket) now holds
-- the general/full plan set and is labelled "General Drawings/Plans" in the
-- UI; 'electrical_drawing' holds the electrical-only sheets. RFQ vendor emails
-- attach the electrical set, falling back to 'drawing' on projects that
-- predate this split. App-layer membership (INITIAL_CATEGORIES,
-- ESTIMATOR_READ, VALID_CATEGORIES, display order) lives in
-- app/core/file_categories.py. Existing 'drawing' rows are untouched: they ARE
-- the general set, so there is nothing to backfill.
--
-- ENUM-ONLY BY DESIGN. PG12+ allows ALTER TYPE ... ADD VALUE inside a
-- transaction only while the new label is never USED later in that same
-- transaction, and the manual apply workflow pastes a whole file into the SQL
-- editor, which runs it as one implicit transaction. Nothing else here needs
-- the label, so this file stays enum-only (precedent: 0075, 0098).
--
-- file_category before this migration: drawing, estimate, boq, markup,
-- rfq_split, quote, other (0001:35-37), proposal (0024:11), specification
-- (0037:11), revision + additional (0048:16-17), estimator_additional
-- (0050:16), addendum (0075:25), marked_plans (0098:23).
--
-- Re-runnable: `if not exists` makes a second apply a no-op.

alter type file_category add value if not exists 'electrical_drawing';

-- Without this PostgREST keeps the stale enum in its cache and every
-- category=electrical_drawing insert fails as an invalid-enum error, which the
-- FE surfaces as the opaque "Failed to fetch" (a raw 500 loses its CORS
-- headers). Safe here: NOTIFY takes a string literal, it does not USE the new
-- label.
notify pgrst, 'reload schema';
