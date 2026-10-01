"""Single source of truth for `project_files.category` semantics.

Lives in `app/core` so every router and service imports the same sets instead of
re-declaring them. Before this module the sets were declared in
`app/routers/files.py` and hand-copied (in whole or in part) into
`app/routers/estimator.py`, `app/services/estimator_email.py`,
`app/services/file_export.py`, `app/services/estimator_rounds.py` and the
frontend - a ~12-site duplication in which adding a category meant finding every
copy. Add a category HERE and nowhere else.

`app/routers/files.py` re-exports every name verbatim, because
`tests/test_file_updates.py` and `tests/test_estimator_rounds.py` import them
from `files.py` and must keep working unchanged.

The consumer sites, as of the 0132 split (docs/RFP_SPLIT.md section 5). A new
category has to be considered at every one of them:

* `app/routers/files.py` - upload validation, the estimator read gate, the
  export query, the delete gate, the drawing-changed notification.
* `app/routers/estimator.py` - the drawing gate and the package query.
* `app/routers/workflow.py` - the "intake needs a drawing" gate.
* `app/routers/bid_drafts.py` - what the New Bid modal may hold.
* `app/services/estimator_email.py` - the hand-off email's sections.
* `app/services/file_sends.py` - the Plans & Specs Log / HandoffSummary.
* `app/services/file_export.py` - the ZIP's folder order.
* `app/services/office_preview.py` - which categories never get a derivative.
* `app/services/pm_folders.py` - bidding category to PM hub folder.
* `app/services/rfq_sending.py` - the vendor default attachment set.
* `bdr_fe/lib/fileCategories.ts` - the frontend mirror (builder C).

The 0132 additions (`civil_drawing` … `low_voltage_drawing`, `rfp`) are
first-class everywhere, not only on RFP-created projects: they are uploadable
by hand, part of the initial package, and readable by estimators.

The sets, and what each one actually means
------------------------------------------

* ``ESTIMATOR_READ`` - what an external estimator may read UNCONDITIONALLY, the
  moment it exists. `_estimator_visible` short-circuits on this set, so a
  category here is downloadable from the instant it is saved, before anything is
  emailed. The priced-off package plus, since 0132, `other`.
* ``ESTIMATOR_WRITE`` - what an estimator may UPLOAD: their own deliverables.
  Reads of these are additionally scoped to the uploader, so one estimator never
  reads a competitor's workbook.
* ``INITIAL_CATEGORIES`` - the initial package blocks. Frozen (no upload, no
  delete) once the hand-off has actually SENT.
* ``UPDATE_CATEGORIES`` - post-hand-off updates. Each requires a per-file note
  AND requires the lock to already be in place.
* ``ADDENDUM_CATEGORY`` - deliberately a member of NEITHER ``INITIAL_CATEGORIES``
  nor ``UPDATE_CATEGORIES``. An addendum is the one category uploadable on BOTH
  sides of the hand-off lock (the initial "Upload plans and specs" modal AND the
  post-send Revisions modal), it carries no note requirement, and it is
  identified by number + issue date instead. Widening either set to include it
  would break exactly that; `test_initial_and_update_sets_are_disjoint` is the
  canary for that tempting shortcut.
* ``SENT_GATED_CATEGORIES`` - categories the estimator sees only once the file
  was actually EMAILED to them. An uploaded-but-unsent revision or addendum is
  still a draft.
* ``PACKAGE_CATEGORIES`` - everything that travels team -> estimator: the
  package itself, and the universe the Plans & Specs Log is drawn from.
* ``ESTIMATOR_QUERY_CATEGORIES`` - the single list every estimator-scoped
  `project_files` query filters on (list, export, per-batch ZIP). Anything
  missing here is invisible to estimators no matter what the read gate says.
* ``VALID_CATEGORIES`` - everything `POST .../files` accepts. Note this is NOT
  the full `file_category` enum: `proposal` is a real enum label (0024) written
  only by the proposal-send service, never uploaded through the files router.
* ``CATEGORY_DISPLAY_ORDER`` - the one display/grouping order shared by the
  Plans & Specs Log, the ZIP export's folder ranking and the package email's
  sections. It DOES include `proposal` (which the ZIP export foldered before
  this module existed) and `other` as the trailing bucket.
* ``DRAWING_CATEGORIES`` - every trade drawing set (0132) plus the general
  `drawing` bucket. "Does this project have a drawing?" is asked of this set,
  and a change to any member pings whoever prices off the plans.
* ``CATEGORY_LABELS`` - the human label per category, used by the hand-off
  email's section titles, the notification texts and the RFQ modal payload.
  `bdr_fe/lib/fileCategories.ts` mirrors these strings.
* ``exclude_source_set`` / ``is_source_set`` - the split source-set predicate
  (0132). After the splitter cuts a drawing set, the untouched source PDF stays
  on the project as `other` with `is_source_set = true`. It is the team's
  reference copy only: it never travels to an estimator and never rides an RFQ,
  so every estimator/package/RFQ query filters it out through this one helper.
"""

# Every drawing set. 'drawing' is the general/full plan set (labelled "General
# Drawings/Plans" in the UI); 'electrical_drawing' (0099) is the electrical-only
# sheet set RFQs attach; the other seven trade sets arrive with 0132, cut out of
# the general set by the bid file splitter but equally uploadable by hand.
# Any of them satisfies the "this project has drawings" gates.
DRAWING_CATEGORIES = {
    "drawing",
    "civil_drawing",
    "structural_drawing",
    "architectural_drawing",
    "mechanical_drawing",
    "plumbing_drawing",
    "electrical_drawing",
    "fire_protection_drawing",
    "low_voltage_drawing",
}

# What the estimator may read unconditionally: the priced-off package. Every
# drawing set, the specifications, the RFP/bid-invitation document (0132) and
# 'other'. They behave exactly alike: estimator-readable from the moment they
# exist, frozen once the hand-off has sent.
#
# 'other' joining this set (0132, the user's "all file types" decision) widens
# the estimator read gate on EXISTING projects too: a writer's 'other' upload is
# estimator-visible the moment it is saved. The one exception is a split source
# set, which is 'other' with `is_source_set = true` and is filtered out of every
# estimator query below. 'rfq_split' and 'quote' stay internal.
ESTIMATOR_READ = DRAWING_CATEGORIES | {"specification", "rfp", "other"}
# What the estimator may upload: their own deliverables. 'addendum' MUST NEVER
# be added here - estimators view addenda but never upload them, and the
# ESTIMATOR_WRITE check in files.upload_file is the only gate.
# 'marked_plans' (0098) is the drawing set the estimator marked up while
# pricing: uploadable pre-submit like estimate/boq/markup, and ALSO uploadable
# by writers (it has no estimator-only gate, unlike 'estimator_additional').
ESTIMATOR_WRITE = {"estimate", "boq", "markup", "marked_plans", "estimator_additional"}
# The initial package blocks - frozen once the hand-off has actually sent.
# ESTIMATOR_READ minus 'other'. The contract (docs/RFP_SPLIT.md section 5) reads
# INITIAL_CATEGORIES = ESTIMATOR_READ, but 'other' cannot be frozen: it is the
# category three POST-hand-off flows upload into (the RFQ Modify Files modal,
# the GC proposal send modal and Send Out all attach ad-hoc files as 'other'),
# and freezing it would 409 every one of them the moment a package had been
# sent. 'other' therefore stays estimator-READABLE without being a package
# block. Recorded in section 9 of the contract.
INITIAL_CATEGORIES = ESTIMATOR_READ - {"other"}
# Post-hand-off updates: each requires a note AND requires the lock.
# 'addendum' is deliberately NOT here (no note requirement, no lock requirement).
UPDATE_CATEGORIES = {"revision", "additional"}

# Addenda are the one category uploadable on BOTH sides of the hand-off lock
# (initial modal AND Revisions modal), so they belong to neither branch above.
ADDENDUM_CATEGORY = "addendum"
ADDENDUM_NUMBER_MAX_CHARS = 40

# Categories the estimator sees only once the file was actually EMAILED to them.
# An uploaded-but-unsent revision or addendum is still a draft.
SENT_GATED_CATEGORIES = UPDATE_CATEGORIES | {ADDENDUM_CATEGORY}
# Everything that travels team -> estimator: the package and the log's universe.
PACKAGE_CATEGORIES = INITIAL_CATEGORIES | SENT_GATED_CATEGORIES
# The single list every estimator-scoped project_files query filters on.
ESTIMATOR_QUERY_CATEGORIES = ESTIMATOR_READ | ESTIMATOR_WRITE | SENT_GATED_CATEGORIES

VALID_CATEGORIES = {
    "drawing",
    "civil_drawing",
    "structural_drawing",
    "architectural_drawing",
    "mechanical_drawing",
    "plumbing_drawing",
    "electrical_drawing",
    "fire_protection_drawing",
    "low_voltage_drawing",
    "specification",
    "rfp",
    "addendum",
    "revision",
    "additional",
    "estimate",
    "boq",
    "markup",
    "marked_plans",
    "estimator_additional",
    "rfq_split",
    "quote",
    "other",
}

FILE_NOTE_MAX_CHARS = 2000

# ── The plans-vs-specs axis (0077) ──────────────────────────────────────────
#
# `category` says WHAT KIND of thing a file is; `doc_type` says WHICH DOCUMENT
# SET it belongs to. The two are orthogonal, which is exactly why doc_type is a
# second column and not four more category labels - the lock rule, the note rule
# and the sent-gate are all properties of the category alone.
DOC_TYPES = {"drawing", "specification"}
# Categories that may carry a doc_type at all. Mirrors project_files_doc_type_ck.
# 'drawing'/'specification' are absent because their category IS the document
# set; 'additional' is absent because the Revisions modal keeps it as one
# undivided section (an additional file is by definition neither plan nor spec).
DOC_TYPE_CATEGORIES = {"revision", ADDENDUM_CATEGORY}
# Categories where the API REQUIRES it. Only 'revision': the Revisions modal is
# the sole uploader of revisions and always knows which section the file came
# from, whereas an addendum can also arrive from the initial "Upload plans and
# specs" modal, which does not ask. Legacy rows predating 0077 are NULL for both
# and stay readable - the DB CHECK never demands non-null.
DOC_TYPE_REQUIRED_CATEGORIES = {"revision"}


def section_key(category: str, doc_type: str | None) -> str:
    """The stable key one send-section is filed under, in
    `file_send_batches.section_notes` and in the modal's section list.

    "revision:drawing", "revision:specification", "addendum", "additional" - the
    doc_type suffix appears ONLY where the category is actually split, so a
    legacy revision with no doc_type keys to plain "revision" and shares the
    untitled group it already renders in.
    """
    if category in DOC_TYPE_CATEGORIES and doc_type in DOC_TYPES:
        return f"{category}:{doc_type}"
    return category


# Every key `section_notes` may contain. Anything else is rejected (400) rather
# than silently stored, so a typo can't create a note nothing will ever render.
SECTION_NOTE_KEYS = frozenset(
    {section_key(c, d) for c in DOC_TYPE_CATEGORIES for d in DOC_TYPES}
    | {"revision", "additional", ADDENDUM_CATEGORY}
)
# Sections whose note the API requires when the batch contains files for them.
# Same rule as the per-file note (UPDATE_CATEGORIES): revisions must explain
# themselves. Addenda carry a number + issue date instead, and 'additional'
# already requires a per-file note, so neither is forced to repeat itself.
SECTION_NOTE_REQUIRED_KEYS = frozenset({"revision:drawing", "revision:specification"})
SECTION_NOTE_MAX_CHARS = 2000

# Display/grouping order used by the log, the ZIP export and the package email.
# The RFP document leads (it is what the whole bid answers), then the drawing
# sets in the order a set is normally bound, then the specs and the addenda.
CATEGORY_DISPLAY_ORDER = [
    "rfp",
    "drawing",
    "civil_drawing",
    "structural_drawing",
    "architectural_drawing",
    "mechanical_drawing",
    "plumbing_drawing",
    "electrical_drawing",
    "fire_protection_drawing",
    "low_voltage_drawing",
    "specification",
    "addendum",
    "revision",
    "additional",
    "estimate",
    "boq",
    "markup",
    "marked_plans",
    "estimator_additional",
    "rfq_split",
    "quote",
    "proposal",
    "other",
]

# The human label per category. One string per category, shared by the hand-off
# email's section titles, the drawing-changed notifications and the RFQ Modify
# Files payload, so the backend never spells a category two ways.
# `bdr_fe/lib/fileCategories.ts` mirrors these exact strings.
CATEGORY_LABELS: dict[str, str] = {
    "rfp": "RFP / bid documents",
    "drawing": "General drawings/plans",
    "civil_drawing": "Civil drawings",
    "structural_drawing": "Structural drawings",
    "architectural_drawing": "Architectural drawings",
    "mechanical_drawing": "Mechanical drawings",
    "plumbing_drawing": "Plumbing drawings",
    "electrical_drawing": "Electrical drawings",
    "fire_protection_drawing": "Fire protection drawings",
    "low_voltage_drawing": "Low voltage drawings",
    "specification": "Specifications",
    "addendum": "Addenda",
    "revision": "Changes/Revisions",
    "additional": "Additional files",
    "estimate": "Estimate",
    "boq": "BOQ",
    "markup": "Markup",
    "marked_plans": "Marked plans",
    "estimator_additional": "Estimator additional files",
    "rfq_split": "BOM split",
    "quote": "Quotes",
    "proposal": "Proposals",
    "other": "Other",
}


def category_label(category: str | None) -> str:
    """The human label for a category, falling back to the raw value so an
    unmapped (or future) enum label still renders as itself."""
    return CATEGORY_LABELS.get(category or "", category or "Other")


def category_rank(category: str | None) -> int:
    """Position in CATEGORY_DISPLAY_ORDER; anything unknown sorts last."""
    try:
        return CATEGORY_DISPLAY_ORDER.index(category)  # type: ignore[arg-type]
    except ValueError:
        return len(CATEGORY_DISPLAY_ORDER)


# ── Split source sets (0132) ────────────────────────────────────────────────
#
# When the bid file splitter cuts a drawing set into trade sets, the untouched
# source PDF is kept on the project as `other` with `is_source_set = true` and
# the note "Source set: split into N documents". It exists so the team can go
# back to the original; it must never travel outward, because an estimator or a
# vendor would then receive the same sheets twice, once whole and once cut.
SOURCE_SET_COLUMN = "is_source_set"


def exclude_source_set(q):
    """Drop split source sets from a `project_files` query.

    The one place the predicate lives. `is_source_set` is NOT NULL DEFAULT false
    (0132), so an equality filter is exact and, unlike an `or=` filter, composes
    with `estimator_rounds.exclude_unsent` on the same query.
    """
    return q.eq(SOURCE_SET_COLUMN, False)


def is_source_set(rec: dict) -> bool:
    """Row-level mirror of `exclude_source_set`, for rows already in memory.
    `.get`, never `[]`: callers pass rows selected before 0132 existed."""
    return bool(rec.get(SOURCE_SET_COLUMN))
