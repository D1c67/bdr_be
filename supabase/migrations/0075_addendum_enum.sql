-- 0075 — 'addendum' as a first-class file_category.
--
-- Addenda (bid addenda issued by the GC/architect mid-bid) are today uploaded
-- as 'revision' files, which loses the two facts everyone actually asks for:
-- the addendum NUMBER and its ISSUE DATE. This migration adds the label; 0076
-- adds the metadata columns, the CHECK that ties them to the label, and the
-- send-batch tables that make "what did the estimator receive, and when"
-- answerable.
--
-- ENUM-ONLY BY DESIGN. PG12+ allows ALTER TYPE ... ADD VALUE inside a
-- transaction only while the new label is never USED later in that same
-- transaction, and the manual apply workflow pastes a whole file into the SQL
-- editor, which runs it as one implicit transaction. Everything that needs to
-- name 'addendum' — the metadata CHECK, the grouping index, any addendum-scoped
-- backfill — therefore lives in 0076, which runs afterwards as its own
-- transaction and may reference the label freely.
-- Precedent for the split: 0024:8-11, 0037:8-11, 0048:13-17, 0050:11-16.
--
-- file_category before this migration: drawing, estimate, boq, markup,
-- rfq_split, quote, other (0001:35-37), proposal (0024:11), specification
-- (0037:11), revision + additional (0048:16-17), estimator_additional (0050:16).
--
-- Re-runnable: `if not exists` makes a second apply a no-op.

alter type file_category add value if not exists 'addendum';

-- 0037 omitted this line. Without it PostgREST keeps the stale enum in its
-- cache and every category=addendum insert fails as an invalid-enum error,
-- which the FE surfaces as the opaque "Failed to fetch" (a raw 500 loses its
-- CORS headers). Safe here: NOTIFY takes a string literal, it does not USE the
-- new label — same pairing as 0048:16-17 + 0048:28.
notify pgrst, 'reload schema';
