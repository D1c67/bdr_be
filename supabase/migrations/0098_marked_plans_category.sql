-- 0098: 'marked_plans' as a first-class file_category.
--
-- Marked plans are the drawing set the estimator marked up while pricing the
-- job. Today they either ride along inside the 'markup' bucket or arrive as
-- 'estimator_additional', which loses the distinction the team actually wants
-- on the Estimate Received step: "show me the plans with the takeoff marks".
-- This migration adds the label; app-layer membership (ESTIMATOR_WRITE,
-- VALID_CATEGORIES, display order) lives in app/core/file_categories.py.
--
-- ENUM-ONLY BY DESIGN. PG12+ allows ALTER TYPE ... ADD VALUE inside a
-- transaction only while the new label is never USED later in that same
-- transaction, and the manual apply workflow pastes a whole file into the SQL
-- editor, which runs it as one implicit transaction. Nothing else here needs
-- the label, so this file stays enum-only (precedent: 0075:10-17).
--
-- file_category before this migration: drawing, estimate, boq, markup,
-- rfq_split, quote, other (0001:35-37), proposal (0024:11), specification
-- (0037:11), revision + additional (0048:16-17), estimator_additional
-- (0050:16), addendum (0075:25).
--
-- Re-runnable: `if not exists` makes a second apply a no-op.

alter type file_category add value if not exists 'marked_plans';

-- Without this PostgREST keeps the stale enum in its cache and every
-- category=marked_plans insert fails as an invalid-enum error, which the FE
-- surfaces as the opaque "Failed to fetch" (a raw 500 loses its CORS headers).
-- Safe here: NOTIFY takes a string literal, it does not USE the new label.
notify pgrst, 'reload schema';
