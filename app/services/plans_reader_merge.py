"""Code merge of a category's group outputs into category output v2
(docs/AI_ESTIMATING_BUILD_SPEC.md 4.1 "code merge", 5.4, WS4).

What it replaces: today `plans_reader_runs._category_output` concatenates
the sub outputs (or takes the roll-up model's answer) and appends the
escalation outputs as `escalation_patches` for the main model to reconcile
in prose. A model reconciling patches is how a cropped tile's subtotal
became the sheet total (E11). Here code does the merge, by these rules:

- the base is the sub outputs concatenated in group order; the roll-up
  model's output is used only behind `plans_reader_llm_rollup` (off);
- escalation and recount outputs are patches applied by
  `plans_reader_rows.upgrade_category_output`: a patch targets the row named
  by `supersedes_row_id`, else the row with the exact normalized description
  on the same sheet; a patch that does not raise its target leaves it
  unchanged and goes to the target's alternates; a raise sets coverage
  `partial` and flags `needs_recount`; a patch never lowers (lowering is a
  WS7 recount outcome);
- containment is recorded per escalation page from the WS1 facts: how many
  of the family's printed tags lie inside the tiles, so a `whole_sheet`
  claim becomes a sheet total only when every tag was inside;
- dedupe ONLY true duplicates: the same page read by two different groups,
  the same unit, the same printed tag and the same normalized description
  (or similarity >= DUPLICATE_SIMILARITY). Rows of one group are never
  deduped (two rows of a sheet are two things the reader saw), and nothing
  is ever summed across pages;
- conservation: every input row is accounted for as active, an alternate,
  a superseded snapshot or a merged duplicate; the merge raises
  `MergeError` rather than drop a row silently;
- census floors (plans_reader_floors) are computed from the same facts and
  stored under `floors` for the gates and the recount;
- recount patches (WS7 blind recount outputs) are applied by this module
  AFTER the upgrade, against every active row of the sheet with its
  run-level id: the rows module only sees first-pass targets and attributes
  a patch to the first escalation group that read the page, so a recount
  of a row an escalation had already replaced (7154 C27) was added as a
  second active row. A recount row lands on its target (`supersedes_row_id`
  by row id, by the prior output's fingerprint for that id, by the local
  c-id, else the unique exact description) by the same rules as an
  escalation patch: a whole-sheet recount that does not lower replaces its
  target and keeps its id (a confirmed count supersedes, never duplicates),
  anything else goes to the target's alternates (raising at most);
- on a re-merge (`prior`), a row whose slot rule missed its prior id (an
  escalation replacement keyed by its first-pass fingerprint) gets the
  prior id of its own fingerprint back when no other row holds it.

`consolidate_category(category, groups, *, words_by_page, ledger, prior,
run_outputs)` is the contract of 5.3; `consolidate_output` is the one-call
hook for `_category_output`, which already holds the v1 output. `prior`
keeps row ids stable on a re-merge after a recount; `run_outputs` (the
category outputs already stored for the run) seeds the id sequence so the
ids are run-unique across categories (`run_sequence`).
`legacy_view(output, groups=...)` gives the legacy main prompt the v1
category output it reads today.

Pure: group rows in, a v2 output out. No database, no model.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from app.services import plans_reader_rows as rows_mod
from app.services.electrical_items import BoqRow, normalize_description, similarity
from app.services.estimating_contracts import ContractError, is_row_id
from app.services.plans_reader_floors import compute_floors, family_points, text_class_usable, text_usable
from app.services.plans_reader_regions import _positions_from_words, points_inside
from app.services.plans_reader_rows import ORIGIN_KEY, RAISED_REASON, ROW_KINDS, RowsMeta, RowSeq
from app.services.plans_reader_schemas import CATEGORIES

MERGE_VERSION = "ws4-merge-1"
DUPLICATE_SIMILARITY = 0.9
PATCH_KINDS: frozenset[str] = frozenset({"escalation", "recount"})
DUPLICATE_REASON = "duplicate read of the same sheet by another group"
_OUTPUT_KEYS = ("pages", "counts", "runs", "scope", "specs", "schedules", "details", "questions")


class MergeError(ContractError):
    """The merge would lose a row (conservation) or was given bad input."""


# Settings -------------------------------------------------------------------------


def llm_rollup_enabled(settings: Any = None) -> bool:
    src = settings
    if src is None:
        try:
            from app.core.config import get_settings

            src = get_settings()
        except Exception:  # noqa: BLE001 - settings unavailable reads as the code default
            return False
    if isinstance(src, Mapping):
        return bool(src.get("plans_reader_llm_rollup", False))
    return bool(getattr(src, "plans_reader_llm_rollup", False))


# v1 assembly (mirrors plans_reader_runs._category_output without importing it) ------


def _order_key(group: Mapping[str, Any]) -> tuple:
    """The order today's code walks groups in (plans_reader_runs._order_key)."""
    return (str(group.get("created_at") or ""), str(group.get("id") or ""))


def _done(group: Mapping[str, Any]) -> bool:
    return group.get("status") == "done" and isinstance(group.get("output"), Mapping)


def _page_key(item: Mapping[str, Any]) -> tuple[str, int]:
    return (str(item.get("document_id") or ""), int(item.get("page") or 0))


def _own_pages(group: Mapping[str, Any]) -> set[tuple[str, int]]:
    return {
        _page_key(i) for i in group.get("pages") or [] if isinstance(i, Mapping) and i.get("kind") != "file"
    }


def flags_by_sheet(group: Mapping[str, Any]) -> dict[tuple[str, int], list[dict]]:
    """Resolution flags of a done sub group per (document_id, page) of its
    own image pages (the same rule as plans_reader_runs._flags_by_sheet)."""
    output = group.get("output") if isinstance(group.get("output"), Mapping) else {}
    own = _own_pages(group)
    out: dict[tuple[str, int], list[dict]] = {}
    for page in output.get("pages") or []:
        if not isinstance(page, Mapping):
            continue
        for flag in page.get("resolution_flags") or []:
            if not isinstance(flag, Mapping):
                continue
            doc = flag.get("document_id") or page.get("document_id")
            pg = rows_mod._as_int(flag.get("page")) or rows_mod._as_int(page.get("page")) or 0
            key = (str(doc), pg)
            if key in own:
                out.setdefault(key, []).append(dict(flag))
    return out


def concat_outputs(
    outputs: Iterable[Mapping[str, Any]], category: str, *, group_ids: Iterable[str | None] | None = None
) -> dict[str, Any]:
    """Several group outputs of one category joined in group order (arrays
    concatenated, summaries joined); the code path of `_merge_outputs`.
    With `group_ids`, every row is stamped with the id of the group it came
    from (plans_reader_rows.ORIGIN_KEY). The upgrade reads the stamp BEFORE
    it applies any patch (origin group, page item and the local c-id of that
    group's own output) and removes it, so two groups that read the same
    page keep their provenance and a `supersedes_row_id` names the row of
    the group the escalation re-reads."""
    merged: dict[str, Any] = {"category": category, "summary": ""}
    for key in _OUTPUT_KEYS:
        merged[key] = []
    summaries: list[str] = []
    ids = list(group_ids) if group_ids is not None else []
    for i, out in enumerate(outputs):
        gid = ids[i] if i < len(ids) else None
        for key in _OUTPUT_KEYS:
            for x in out.get(key) or []:
                if not isinstance(x, Mapping):
                    continue
                row = copy.deepcopy(dict(x))
                if gid is not None and key != "pages":
                    row[ORIGIN_KEY] = str(gid)
                merged[key].append(row)
        if out.get("summary"):
            summaries.append(str(out["summary"]))
    merged["summary"] = " ".join(summaries)
    return merged


def category_v1_output(
    category: str, groups: Iterable[Mapping[str, Any]], *, llm_rollup: bool = False, stamp_origin: bool = False
) -> tuple[dict[str, Any] | None, list[str]]:
    """The v1 category output for `groups` (the run's group rows with their
    outputs): sub outputs concatenated (the roll-up model's output only when
    `llm_rollup`), escalation and recount outputs as `escalation_patches`,
    the flags nobody re-read as `unresolved_flags`, and `source`. Returns
    (None, []) when no sub group of the category is done."""
    all_groups = [g for g in groups if isinstance(g, Mapping)]
    subs = sorted(
        (g for g in all_groups if g.get("kind") == "sub" and g.get("category") == category),
        key=lambda g: int(g.get("group_index") or 0),
    )
    done = [g for g in subs if _done(g)]
    if not done:
        return None, []
    rollup = None
    if llm_rollup:
        rollup = next(
            (
                g for g in sorted(all_groups, key=_order_key)
                if g.get("kind") == "rollup" and g.get("category") == category and _done(g)
            ),
            None,
        )
    if rollup is not None:
        base = copy.deepcopy(dict(rollup["output"]))
        sources = [str(rollup["id"])] + [str(g["id"]) for g in done]
    elif len(done) == 1:
        base = copy.deepcopy(dict(done[0]["output"]))
        sources = [str(done[0]["id"])]
    else:
        base = concat_outputs([g["output"] for g in done], category, group_ids=[str(g["id"]) for g in done] if stamp_origin else None)
        sources = [str(g["id"]) for g in done]
    patched: set[tuple[str, int]] = set()
    patches: list[dict[str, Any]] = []
    for g in sorted(all_groups, key=_order_key):
        if g.get("kind") not in PATCH_KINDS or g.get("category") != category:
            continue
        if _done(g):
            patches.append(copy.deepcopy(dict(g["output"])))
            sources.append(str(g["id"]))
            patched |= _own_pages(g)
    unresolved: list[dict[str, Any]] = []
    for g in done:
        for key, flags in flags_by_sheet(g).items():
            if key in patched:
                continue
            for flag in flags:
                unresolved.append(
                    {
                        "document_id": key[0],
                        "doc_index": flag.get("doc_index"),
                        "page": key[1],
                        "region": flag.get("region"),
                        "reason": flag.get("reason"),
                    }
                )
    base["category"] = category
    base["escalation_patches"] = patches
    base["unresolved_flags"] = unresolved
    base["source"] = {
        "groups": len(subs),
        "failed_groups": sum(1 for g in subs if g.get("status") != "done"),
        "rolled_up": rollup is not None,
    }
    return base, sources


# Facts per page ---------------------------------------------------------------------


def _facts_key(key: Any) -> tuple[str, int] | None:
    if isinstance(key, tuple) and len(key) == 2:
        try:
            return (str(key[0]), int(key[1]))
        except (TypeError, ValueError):
            return None
    if isinstance(key, str) and ":" in key:
        doc, _, page = key.rpartition(":")
        if page.isdigit():
            return (doc, int(page))
    return None


def _facts_index(words_by_page: Any) -> dict[tuple[str, int], Any]:
    """{(document_id, page): PageWords | facts page dict}."""
    out: dict[tuple[str, int], Any] = {}
    if not words_by_page:
        return out
    if isinstance(words_by_page, Mapping):
        for key, value in words_by_page.items():
            k = _facts_key(key)
            if k is not None:
                out[k] = value
        return out
    for item in words_by_page:
        if isinstance(item, Mapping) and isinstance(item.get("pages"), list):
            doc = str(item.get("document_id") or item.get("id") or "")
            for pg in item["pages"]:
                d = pg if isinstance(pg, Mapping) else getattr(pg, "to_dict", lambda: {})()
                if d.get("page") is not None:
                    out[(doc, int(d["page"]))] = d
    return out


def _is_words(entry: Any) -> bool:
    return hasattr(entry, "words") and hasattr(entry, "text_class")


def _page_text_usable(entry: Any) -> bool | None:
    """Whether the page has a usable text layer, by the floors rule (the WS1
    text class mapped to its quality; only `full` is usable, so vector_text,
    sparse and scanned pages use the 0.98 tile-coverage rule)."""
    if entry is None:
        return None
    if _is_words(entry):
        return text_class_usable(getattr(entry, "text_class", ""))
    if isinstance(entry, Mapping):
        return text_usable(entry)
    to_dict = getattr(entry, "to_dict", None)
    return text_usable(to_dict()) if callable(to_dict) else None


def family_positions(entry: Any, family: str) -> list[list[float]]:
    """Printed tag positions of a census family from a facts page (tags
    block) or from WS1 words (status tags only)."""
    if entry is None:
        return []
    if _is_words(entry):
        return _positions_from_words(entry, family)
    if isinstance(entry, Mapping):
        return family_points(entry, family)
    to_dict = getattr(entry, "to_dict", None)
    return family_points(to_dict(), family) if callable(to_dict) else []


def _patch_family(patch: Mapping[str, Any], key: tuple[str, int]) -> str:
    """The census family an escalation patch counts on a page: relocate_r
    when its rows carry (R) tags, else demo_x (demolition patches are the
    escalation case this build fixes); every family is checked in turn by
    the caller when the first has no printed tags."""
    for r in patch.get("counts") or []:
        if not isinstance(r, Mapping) or rows_mod._page_key(r.get("document_id"), r.get("page")) != key:
            continue
        tag = str(r.get("symbol_or_tag") or "")
        if rows_mod._RELOCATE_RE.search(tag):
            return "relocate_r"
    return "demo_x"


def enrich_containment(
    groups: Iterable[Mapping[str, Any]], facts: Mapping[tuple[str, int], Any]
) -> list[dict[str, Any]]:
    """Deep copies of the groups with `containment` {family, targets,
    inside, complete} on every escalation or recount page item whose tiles
    and facts are known; other groups and pages pass through unchanged."""
    out: list[dict[str, Any]] = []
    for g in groups:
        if not isinstance(g, Mapping):
            continue
        copy_g = copy.deepcopy(dict(g))
        if copy_g.get("kind") not in PATCH_KINDS:
            out.append(copy_g)
            continue
        patch = copy_g.get("output") if isinstance(copy_g.get("output"), Mapping) else {}
        for item in copy_g.get("pages") or []:
            if not isinstance(item, dict) or item.get("kind") == "file":
                continue
            key = _page_key(item)
            entry = facts.get(key)
            tiles = item.get("tiles")
            if entry is None or not tiles or isinstance(item.get("containment"), Mapping):
                continue
            family = _patch_family(patch, key)
            pts = family_positions(entry, family)
            if not pts:
                for other in ("demo_x", "relocate_r", "circuit"):
                    if other != family:
                        pts = family_positions(entry, other)
                        if pts:
                            family = other
                            break
            if not pts:
                continue
            inside = points_inside(pts, [list(t) for t in tiles if isinstance(t, (list, tuple)) and len(t) == 4])
            item["containment"] = {"family": family, "targets": len(pts), "inside": inside, "complete": inside >= len(pts)}
        out.append(copy_g)
    return out


# Duplicates -------------------------------------------------------------------------


def _norm_tag(value: Any) -> str:
    return normalize_description(value or "")


def _same_item(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if str(a.get("unit") or "EA").upper() != str(b.get("unit") or "EA").upper():
        return False
    if _norm_tag(a.get("symbol_or_tag")) != _norm_tag(b.get("symbol_or_tag")):
        return False
    da, db = normalize_description(a.get("description")), normalize_description(b.get("description"))
    if da == db:
        return True
    unit = str(a.get("unit") or "EA").upper()
    return similarity(BoqRow("", "", "", "", str(a.get("description") or ""), 0.0, unit),
                      BoqRow("", "", "", "", str(b.get("description") or ""), 0.0, unit)) >= DUPLICATE_SIMILARITY


def dedupe_duplicates(v2: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge true duplicates among the active count rows (in place): same
    page, different groups, same unit and tag, same normalized description
    or similarity >= DUPLICATE_SIMILARITY. The first row keeps its id and
    the larger quantity (a floor never lowers); the other becomes its
    alternate. Returns the merge-log entries."""
    counts = [r for r in v2.get("counts") or [] if isinstance(r, dict)]
    log: list[dict[str, Any]] = []
    removed: set[int] = set()
    for i, a in enumerate(counts):
        if id(a) in removed or a.get("status") != "active":
            continue
        for b in counts[i + 1:]:
            if id(b) in removed or b.get("status") != "active":
                continue
            if rows_mod._page_key(a.get("document_id"), a.get("page")) != rows_mod._page_key(b.get("document_id"), b.get("page")):
                continue
            ga, gb = (a.get("origin") or {}).get("group_id"), (b.get("origin") or {}).get("group_id")
            if ga is None or gb is None or ga == gb:
                continue  # never within one group (or without provenance)
            if not _same_item(a, b):
                continue
            qa, qb = rows_mod._as_float(a.get("quantity")), rows_mod._as_float(b.get("quantity"))
            entry = {"kept": a.get("row_id"), "merged": b.get("row_id"), "quantity_kept": qa, "quantity_merged": qb, "reason": DUPLICATE_REASON}
            a.setdefault("alternates", []).append(
                {
                    "quantity": b.get("quantity"), "basis": b.get("basis"), "coverage": b.get("coverage"),
                    "origin": b.get("origin"), "description": b.get("description"), "row_id": b.get("row_id"),
                    "note": DUPLICATE_REASON,
                }
            )
            if qa is not None and qb is not None and qb > qa:
                a.setdefault("superseded", []).append(rows_mod._snapshot(a, RAISED_REASON))
                a["quantity"] = b.get("quantity")
                rows_mod._add_flag(a, "needs_recount")
                entry["raised_to"] = qb
            elif qa is not None and qb is not None and qb != qa:
                rows_mod._add_flag(a, "needs_recount")
            removed.add(id(b))
            log.append(entry)
    if removed:
        v2["counts"] = [r for r in v2.get("counts") or [] if id(r) not in removed]
    return log


# Recount patches and prior ids ---------------------------------------------------------

REPLACED_REASON = "superseded by a whole-sheet patch"


def split_recount_patches(
    output: Mapping[str, Any], groups: Iterable[Mapping[str, Any]], category: str | None
) -> tuple[dict[str, Any], list[tuple[int, dict[str, Any], dict[str, Any]]]]:
    """(the v1 output without its recount patches, [(patch index, recount
    group, patch)]). A patch is a recount patch when it equals the output of
    a done recount group of the category (category_v1_output deep-copies the
    group outputs in order); every other patch stays for the rows module."""
    patches = list(output.get("escalation_patches") or [])
    recounts = [
        g for g in groups
        if isinstance(g, Mapping) and g.get("kind") == "recount" and _done(g)
        and (category is None or g.get("category") == category)
    ]
    taken: set[int] = set()
    found: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    kept: list[Any] = []
    for index, patch in enumerate(patches):
        hit = None
        if isinstance(patch, Mapping):
            for gi, g in enumerate(recounts):
                if gi not in taken and patch == g.get("output"):
                    hit = gi
                    break
        if hit is None:
            kept.append(patch)
            continue
        taken.add(hit)
        found.append((index, dict(recounts[hit]), copy.deepcopy(dict(patch))))
    base = dict(output)
    if found:
        base["escalation_patches"] = kept
    return base, found


def _prior_rows_by_id(prior: Any) -> dict[str, str]:
    """row id -> fingerprint of a prior v2 output (or {fingerprint: ids})."""
    out: dict[str, str] = {}
    for fp, ids in rows_mod._prior_ids(prior).items():
        for rid in ids:
            out.setdefault(rid, fp)
    return out


def restore_prior_ids(v2: dict[str, Any], prior: Any) -> dict[str, str]:
    """On a re-merge, a row that got a new id although the prior output held
    an id for its own fingerprint (the upgrade keys a whole-sheet
    replacement by its target's first-pass fingerprint, while the prior
    output stored the replacement's own) takes that prior id back when no
    other row of the output holds it. Returns {old id: restored id}; the
    merge log's patch targets follow the rename."""
    fp_ids = rows_mod._prior_ids(prior)
    if not fp_ids:
        return {}
    prior_ids = {rid for ids in fp_ids.values() for rid in ids}
    renamed: dict[str, str] = {}
    for key, prefix in ROW_KINDS:
        rows = [r for r in v2.get(key) or [] if isinstance(r, dict)]
        used = {r.get("row_id") for r in rows}
        for row in rows:
            rid = row.get("row_id")
            if rid in prior_ids:
                continue
            for cand in fp_ids.get(str(row.get("fingerprint") or ""), []):
                if cand not in used and is_row_id(cand, prefix):
                    used.add(cand)
                    renamed[str(rid)] = cand
                    row["row_id"] = cand
                    break
    if renamed:
        for entry in (v2.get("merge_log") or {}).get("patches") or []:
            if isinstance(entry, dict) and entry.get("target_row_id") in renamed:
                entry["target_row_id"] = renamed[entry["target_row_id"]]
    return renamed


def _same_identity(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """A recount names its target by the target's own description, so a
    run-level id is trusted only when the two rows agree on it (the same
    normalized description or the same electrical_items key): an id from a
    different numbering (a fresh merge without `prior`) never lands a
    recount on another item."""
    if normalize_description(a.get("description")) == normalize_description(b.get("description")):
        return True
    ka, kb = a.get("item_key"), b.get("item_key")
    return bool(ka) and ka == kb


def find_recount_target(
    patch_row: Mapping[str, Any], counts: list[dict], *, prior_fp: Mapping[str, str] | None = None,
    source_group_id: str | None = None,
) -> tuple[list[dict], str]:
    """Candidates a recount row targets among the ACTIVE count rows of its
    sheet of the same unit, whatever pass produced them (an escalation
    replacement is a valid target): `supersedes_row_id` as a run-level row
    id (when the rows agree on the item, _same_identity), then as the id of
    the prior output's row with that fingerprint, then
    as a local c-id of `source_group_id`; else the exact normalized
    description. ([], "none") when nothing matches."""
    unit = str(patch_row.get("unit") or "EA").upper()
    sheet = rows_mod._page_key(patch_row.get("document_id"), patch_row.get("page"))
    live = [
        r for r in counts
        if r is not patch_row and r.get("status") == "active"
        and rows_mod._page_key(r.get("document_id"), r.get("page")) == sheet
        and str(r.get("unit") or "EA").upper() == unit
    ]
    sid = patch_row.get("supersedes_row_id")
    if sid:
        hit = [r for r in live if r.get("row_id") == sid and _same_identity(patch_row, r)]
        if hit:
            return hit[:1], "supersedes_row_id"
        fp = (prior_fp or {}).get(str(sid))
        if fp:
            hit = [r for r in live if r.get("fingerprint") == fp]
            if len(hit) == 1:
                return hit, "supersedes_row_id_prior"
        hit = [
            r for r in live
            if (r.get("origin") or {}).get("local_id") == sid
            and (source_group_id is None or (r.get("origin") or {}).get("group_id") == str(source_group_id))
        ]
        if hit:
            return hit[:1], "supersedes_row_id_local"
    norm = normalize_description(patch_row.get("description"))
    exact = [r for r in live if normalize_description(r.get("description")) == norm]
    if exact:
        return exact, "description"
    return [], "none"


def _new_row_id(row: Mapping[str, Any], prefix: str, used: set[str], seq: RowSeq, fp_ids: Mapping[str, list[str]]) -> str:
    for cand in fp_ids.get(str(row.get("fingerprint") or ""), []):
        if cand not in used and is_row_id(cand, prefix):
            return cand
    return seq.next(prefix)


def _recount_page_item(group: Mapping[str, Any], patch: Mapping[str, Any], key: tuple[str, int]) -> dict[str, Any] | None:
    """The recount group's page item for a sheet, with the code-computed
    tiles and region containment the recount wrote on its own output page
    (plans_reader_recount.group_output) when the item lacks them."""
    item = rows_mod._page_items(group).get(key)
    if item is None:
        return None
    page = next(
        (p for p in patch.get("pages") or [] if isinstance(p, Mapping) and _page_key(p) == key), None
    )
    if isinstance(page, Mapping):
        if not item.get("tiles") and page.get("tiles"):
            item["tiles"] = copy.deepcopy(page["tiles"])
        if not isinstance(item.get("containment"), Mapping) and isinstance(page.get("containment"), Mapping):
            item["containment"] = copy.deepcopy(dict(page["containment"]))
    return item


def _land_recount(counts: list[dict], target: dict, row: dict, entry: dict[str, Any]) -> None:
    """One recount row on its target (in place), by the escalation patch
    rules of plans_reader_rows._apply_count_patch: a whole-sheet recount that
    does not lower replaces the target and keeps its id; anything else is an
    alternate of the target, raising it at most."""
    pq, tq = rows_mod._as_float(row.get("quantity")), rows_mod._as_float(target.get("quantity"))
    whole_sheet = row.get("coverage") == "sheet_total" and row.get("status") == "active"
    if whole_sheet and pq is not None and tq is not None and pq >= tq:
        replacement = dict(row)
        replacement["superseded"] = list(target.get("superseded") or []) + [rows_mod._snapshot(target, REPLACED_REASON)]
        replacement["alternates"] = list(target.get("alternates") or [])
        replacement["row_id"] = target.get("row_id")
        counts[counts.index(target)] = replacement
        entry["action"] = "replaced" if pq > tq else "confirmed"
        entry["target_row_id"] = replacement["row_id"]
        return
    target.setdefault("alternates", []).append(
        {
            "quantity": row.get("quantity"), "basis": row.get("basis"), "coverage": row.get("coverage"),
            "origin": row.get("origin"), "description": row.get("description"),
            "note": str(row.get("notes") or "")[:400] or None,
        }
    )
    if pq is not None and tq is not None and pq > tq:
        target.setdefault("superseded", []).append(rows_mod._snapshot(target, RAISED_REASON))
        target["quantity"] = row.get("quantity")
        target["coverage"] = "partial"
        rows_mod._add_flag(target, "needs_recount")
        entry["action"] = "raised"
    else:
        if whole_sheet and (pq is None or tq is None or pq < tq):
            rows_mod._add_flag(target, "needs_recount")
        entry["action"] = "alternate"
    entry["target_row_id"] = target.get("row_id")


def apply_recount_patches(
    v2: dict[str, Any],
    recounts: list[tuple[int, dict[str, Any], dict[str, Any]]],
    *,
    meta: RowsMeta,
    base_output: Mapping[str, Any],
    category: str | None,
    seq: RowSeq,
    prior: Any = None,
) -> None:
    """Apply the recount patches split off the v1 output to the upgraded v2
    output (in place): rows decorated with their own recount group as
    origin, count rows landed on their target (find_recount_target,
    _land_recount) or added with a new id, the merge log's inputs, outputs,
    patches and alternate/superseded totals kept in step."""
    if not recounts:
        return
    builder = rows_mod._Builder(base_output, meta, category)
    prior_fp = _prior_rows_by_id(prior)
    fp_ids = rows_mod._prior_ids(prior)
    log = v2.setdefault("merge_log", {})
    inputs = log.setdefault("inputs", {})
    patch_log = log.setdefault("patches", [])
    for p_index, group, patch in recounts:
        for kind_key, prefix in ROW_KINDS:
            raw_rows = [r for r in patch.get(kind_key) or [] if isinstance(r, Mapping)]
            inputs[kind_key] = int(inputs.get(kind_key) or 0) + len(raw_rows)
            rows = v2.setdefault(kind_key, [])
            for index, raw in enumerate(raw_rows, start=1):
                key = _page_key(raw)
                item = _recount_page_item(group, patch, key)
                row = builder.decorate(
                    kind_key, raw, group=group, group_kind="recount", page_item=item,
                    local_id=f"p{p_index + 1}.{prefix.lower()}{index}",
                )
                row.pop("_slot_of", None)
                used = {str(r.get("row_id")) for r in rows if isinstance(r, Mapping)}
                if kind_key != "counts":
                    row["row_id"] = _new_row_id(row, prefix, used, seq, fp_ids)
                    seq.observe(row["row_id"])
                    rows.append(row)
                    continue
                entry: dict[str, Any] = {
                    "patch_row": {
                        "description": row.get("description"), "quantity": row.get("quantity"),
                        "sheet_key": (row.get("sheet") or {}).get("sheet_key"), "origin": row.get("origin"),
                    },
                    "coverage": row.get("coverage"),
                }
                source = (item or {}).get("source_group_id")
                candidates, how = find_recount_target(
                    row, rows, prior_fp=prior_fp, source_group_id=str(source) if source else None
                )
                entry["matched_by"] = how
                patch_log.append(entry)
                if len(candidates) == 1:
                    _land_recount(rows, candidates[0], row, entry)
                    continue
                if candidates:
                    rows_mod._add_flag(row, "ambiguous_patch")
                    rows_mod._add_flag(row, "needs_recount")
                    entry["action"] = "added_ambiguous"
                else:
                    if row.get("coverage") != "sheet_total":
                        rows_mod._add_flag(row, "needs_recount")
                    entry["action"] = "added"
                row["row_id"] = _new_row_id(row, prefix, used, seq, fp_ids)
                seq.observe(row["row_id"])
                entry["target_row_id"] = row["row_id"]
                sheet = rows_mod._page_key(row.get("document_id"), row.get("page"))
                last = max(
                    (i for i, r in enumerate(rows) if rows_mod._page_key(r.get("document_id"), r.get("page")) == sheet),
                    default=len(rows) - 1,
                )
                rows.insert(last + 1, row)
        rows_mod._merge_patch_pages(v2, patch)
    log["outputs"] = {
        key: sum(1 for r in v2.get(key) or [] if isinstance(r, Mapping) and r.get("status") == "active")
        for key, _p in ROW_KINDS
    }
    log["alternates"] = sum(len(r.get("alternates") or []) for key, _p in ROW_KINDS for r in v2.get(key) or [] if isinstance(r, Mapping))
    log["superseded"] = sum(
        1 for key, _p in ROW_KINDS for r in v2.get(key) or [] if isinstance(r, Mapping)
        for x in r.get("superseded") or [] if isinstance(x, Mapping) and x.get("reason") != RAISED_REASON
    )
    v2["row_seq"] = seq.as_dict()


# Conservation -----------------------------------------------------------------------


def conservation(v2: Mapping[str, Any]) -> dict[str, Any]:
    """Per kind: inputs vs active + alternates + superseded (non-raise
    snapshots) + dropped exact duplicates. `ok` is False when a row is
    unaccounted for."""
    log = v2.get("merge_log") or {}
    inputs = log.get("inputs") or {}
    dropped = int(log.get("dropped_exact_duplicates") or 0)
    detail: dict[str, Any] = {}
    ok = True
    for key, _prefix in ROW_KINDS:
        rows_k = [r for r in v2.get(key) or [] if isinstance(r, Mapping)]
        active = sum(1 for r in rows_k if r.get("status") == "active")
        unverifiable = sum(1 for r in rows_k if r.get("status") == "unverifiable")
        alternates = sum(len(r.get("alternates") or []) for r in rows_k)
        superseded = sum(
            1 for r in rows_k for s in r.get("superseded") or []
            if isinstance(s, Mapping) and s.get("reason") != RAISED_REASON
        )
        n_in = int(inputs.get(key) or 0)
        accounted = active + unverifiable + alternates + superseded + (dropped if key == "counts" else 0)
        detail[key] = {"inputs": n_in, "active": active, "unverifiable": unverifiable, "alternates": alternates, "superseded": superseded}
        if n_in and accounted != n_in:
            ok = False
            detail[key]["unaccounted"] = n_in - accounted
    return {"ok": ok, "kinds": detail}


# Public API -------------------------------------------------------------------------


def run_sequence(run_outputs: Iterable[Mapping[str, Any]] | None) -> RowSeq:
    """The run-level RowSeq a new merge continues: the high-water mark of
    every category output already stored for the run (their `row_seq`
    marks and every row id they hold, v1 outputs contributing nothing).
    A category merged on this sequence never reuses an id another
    category's rows carry."""
    outputs = [o for o in run_outputs or [] if isinstance(o, Mapping)]
    return RowSeq.from_outputs(outputs) if outputs else RowSeq()


def consolidate_output(
    category: str,
    output: Mapping[str, Any],
    groups: Iterable[Mapping[str, Any]],
    *,
    words_by_page: Any = None,
    ledger: Mapping[str, Any] | None = None,
    prior: Any = None,
    documents: Iterable[Mapping[str, Any]] | None = None,
    row_seq: RowSeq | None = None,
    floors: bool = True,
    run_outputs: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Category output v1 (as `_category_output` builds it) -> v2 with
    containment, patches applied, duplicates merged, floors and a
    conservation check. A v2 input passes through (ids observed).

    Row ids are run-unique (spec 5.1): the sequence the new ids come from
    is `row_seq` when given, else one seeded from `run_outputs`, the
    category outputs already stored for the run (every category, this one's
    own prior included), through `run_sequence`. Without either the ids
    start at 1, which is right only for the run's first category: every
    category of the 7172 run was merged from C1, so the general and
    mechanical ids collided with the electrical ones and the main session
    dropped the electrical schedule counts, panels and breaker rows."""
    if not isinstance(output, Mapping):
        raise MergeError("a category output must be a mapping")
    facts = _facts_index(words_by_page)
    docs = [dict(d) for d in documents or [] if isinstance(d, Mapping)]
    groups_list = [g for g in groups or [] if isinstance(g, Mapping)]
    seq = row_seq if row_seq is not None else run_sequence(run_outputs)
    if rows_mod.is_v2(output):
        v2 = rows_mod.upgrade_category_output(output, None, category=category, row_seq=seq, prior=prior)
    else:
        enriched = enrich_containment(groups_list, facts)
        text_layer = {k: v for k, v in ((k, _page_text_usable(e)) for k, e in facts.items()) if v is not None}
        meta = RowsMeta(groups=enriched, documents=docs, ledger=dict(ledger or {}), text_layer=text_layer)
        base, recounts = split_recount_patches(output, enriched, category)
        v2 = rows_mod.upgrade_category_output(base, meta, category=category, row_seq=seq, prior=prior)
        restore_prior_ids(v2, prior)
        apply_recount_patches(v2, recounts, meta=meta, base_output=base, category=category, seq=seq, prior=prior)
    log = v2.setdefault("merge_log", {})
    log["merge_version"] = MERGE_VERSION
    log["duplicates"] = dedupe_duplicates(v2)
    log["containment"] = [
        {"document_id": k[0], "page": k[1], **dict(item["containment"])}
        for g in (enrich_containment(groups_list, facts) if facts else [])
        for item in g.get("pages") or []
        if isinstance(item, Mapping) and isinstance(item.get("containment"), Mapping)
        for k in [_page_key(item)]
    ]
    if floors and facts:
        doc_numbers = rows_mod.doc_order(docs) if docs else {}
        if not doc_numbers:
            for r in v2.get("counts") or []:
                if isinstance(r, Mapping) and r.get("document_id") and rows_mod._as_int(r.get("doc_index")):
                    doc_numbers.setdefault(str(r["document_id"]), int(r["doc_index"]))
        pages = {k: e for k, e in facts.items() if not _is_words(e)}
        v2["floors"] = [f.to_dict() for f in compute_floors(pages, doc_numbers=doc_numbers)]
    else:
        v2.setdefault("floors", [])
    v2["source"] = dict(output.get("source") or v2.get("source") or {})
    check = conservation(v2)
    log["conservation"] = check
    if not check["ok"]:
        raise MergeError(f"merge lost rows: {check['kinds']}")
    return v2


def consolidate_category(
    category: str,
    groups: Iterable[Mapping[str, Any]],
    *,
    words_by_page: Any = None,
    ledger: Mapping[str, Any] | None = None,
    prior: Any = None,
    documents: Iterable[Mapping[str, Any]] | None = None,
    settings: Any = None,
    llm_rollup: bool | None = None,
    row_seq: RowSeq | None = None,
    run_outputs: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The 5.3 contract: the run's group rows (outputs attached) of one
    category -> category output v2, or None when no sub group is done.
    `words_by_page` maps (document_id, page) to WS1 PageWords or facts
    pages; `ledger` is the run ledger (sheet numbers); `prior` a previous v2
    output whose ids are kept; `documents` the run's documents (D numbers);
    `run_outputs` the category outputs already stored for the run, whose
    ids the new output's sequence continues (`row_seq`, when given, wins)."""
    groups_list = [g for g in groups if isinstance(g, Mapping)]
    rollup = llm_rollup_enabled(settings) if llm_rollup is None else bool(llm_rollup)
    v1, sources = category_v1_output(category, groups_list, llm_rollup=rollup, stamp_origin=True)
    if v1 is None:
        return None
    v2 = consolidate_output(
        category, v1, groups_list, words_by_page=words_by_page, ledger=ledger, prior=prior,
        documents=documents, row_seq=row_seq, run_outputs=run_outputs,
    )
    v2["source"]["group_ids"] = sources
    return v2


# Legacy view ------------------------------------------------------------------------

# Keys v2 adds: on the output (top level) and on every row.
V2_OUTPUT_KEYS: tuple[str, ...] = ("rows_version", "merge_log", "floors", "row_seq")
V2_ROW_KEYS: tuple[str, ...] = (
    "row_id", "fingerprint", "origin", "coverage", "status", "superseded", "alternates", "item_key", "sheet",
    "positions", "family_tags", "flags",
)


def _strip_v2(output: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: copy.deepcopy(v) for k, v in output.items() if k not in V2_OUTPUT_KEYS}
    for key, _prefix in ROW_KINDS:
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = [_strip_row(r) if isinstance(r, Mapping) else r for r in rows]
    return out


def _strip_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A v2 row without the v2-only keys; the two renames the upgrade made
    are undone (the model's coverage claim back under `coverage`, the
    printed sheet number back under `sheet_number`)."""
    out = {k: v for k, v in row.items() if k not in V2_ROW_KEYS}
    if "model_coverage" in out:
        out["coverage"] = out.pop("model_coverage")
    if "sheet_number_printed" in out:
        out["sheet_number"] = out.pop("sheet_number_printed")
    return out


def legacy_view(
    output: Mapping[str, Any], *, groups: Iterable[Mapping[str, Any]] | None = None, category: str | None = None
) -> dict[str, Any]:
    """The category output the legacy main prompt reads (flags off).

    With the run's `groups` (outputs attached), this is today's v1 output
    rebuilt from them: `category_v1_output(..., llm_rollup=True)`, the exact
    assembly `plans_reader_runs._category_output` performs (the roll-up's
    answer when one exists, else the subs concatenated, escalation outputs
    as `escalation_patches`), so the main request stays byte-identical to
    today's with every flag off. Without groups (or when no sub group of the
    category is done) the v2 output is returned with the v2-only keys
    stripped (V2_OUTPUT_KEYS on the output, V2_ROW_KEYS on every row): a v1
    SHAPE only, not today's bytes, because the code merge has applied the
    patches (raised or replaced rows, patch rows added, no
    `escalation_patches`), merged true duplicates and ordered rows by sheet.
    A v1 input passes through as a deep copy."""
    if not isinstance(output, Mapping):
        raise MergeError("a category output must be a mapping")
    if not rows_mod.is_v2(output):
        return copy.deepcopy(dict(output))
    cat = category or output.get("category")
    if groups is not None and cat:
        v1, _sources = category_v1_output(str(cat), [g for g in groups if isinstance(g, Mapping)], llm_rollup=True)
        if v1 is not None:
            return v1
    return _strip_v2(output)


# Replay stage -----------------------------------------------------------------------


def _replay_groups(ctx: Any, art: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The groups the merge reads in a replay: the artifact groups minus the
    plan stage's `planned` sub groups (what a new plan would send, never
    read), plus the bundle's stored sub groups the plan stage replaced."""
    groups = [dict(g) for g in art.get("groups") or [] if isinstance(g, Mapping) and g.get("status") != "planned"]
    have = {str(g.get("id")) for g in groups}
    bundle = getattr(ctx, "bundle", None)
    for g in getattr(bundle, "groups", None) or []:
        if isinstance(g, Mapping) and g.get("kind") == "sub" and str(g.get("id")) not in have:
            groups.append(dict(g))
            have.add(str(g.get("id")))
    return groups


def _replay_facts(ctx: Any, documents: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Page facts of the documents the WS1 plan stage marked done
    (`facts_status` done, `facts_doc` sha256 and kind): the plan stage keeps
    the DocFacts in its in-process cache (pagefacts.replay.extract_facts,
    keyed by sha256 and kind), so this is a cache hit after `plan` and a
    deterministic $0 extraction otherwise. Documents without facts or
    without bytes are skipped (unknown, never fabricated)."""
    out: dict[tuple[str, int], dict[str, Any]] = {}
    docs = getattr(ctx, "docs", None)
    if docs is None:
        return out
    try:
        from app.services.pagefacts import replay as pf_replay
    except Exception:  # noqa: BLE001 - the facts package is optional in a replay
        return out
    for doc in documents:
        if not isinstance(doc, Mapping) or doc.get("facts_status") != "done" or not isinstance(doc.get("facts_doc"), Mapping):
            continue
        doc_id = str(doc.get("id") or "")
        try:
            facts = pf_replay.extract_facts(docs.get(doc_id), doc)
        except Exception as exc:  # noqa: BLE001 - a missing PDF leaves the document without floors
            ctx.note(f"merge: no page facts for document {doc_id[:8]} ({type(exc).__name__})")
            continue
        for page in facts.pages:
            d = page.to_dict()
            out[(doc_id, int(d["page"]))] = d
    return out


def _category_doc_ids(groups: Iterable[Mapping[str, Any]], category: str) -> set[str]:
    return {
        str(i.get("document_id"))
        for g in groups if isinstance(g, Mapping) and g.get("category") == category
        for i in g.get("pages") or [] if isinstance(i, Mapping) and i.get("document_id")
    }


def replay_merge(ctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    """The `merge` stage of plans_reader_replay: every category output
    rebuilt as v2 from the stored groups, noting where the row counts differ
    from the stored v1 output. Facts: `page_facts` when given (tests and
    review rules), else the facts of the documents the WS1 `plan` stage
    marked done (it writes `documents`, not a facts artifact); each category
    gets the facts of the documents its own groups read, so a mechanical
    sheet never lends a floor to the electrical output."""
    groups = _replay_groups(ctx, art)
    documents = list(art.get("documents") or [])
    ledger = dict(art.get("ledger") or {})
    given = art.get("page_facts") or art.get("facts_pages") or None
    facts_all = _facts_index(given) if given else _replay_facts(ctx, documents)
    if not given:
        ctx.note(f"merge: {len(facts_all)} facts pages from the plan stage documents")
    out: dict[str, Any] = {}
    # One run-level sequence for every category (vocabulary order, the
    # order iter_rows and index_sources rank categories in), so a replayed
    # run never holds two rows of one id; the first category still merges
    # from 1, so a single-category bundle keeps its stored ids.
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    cats = sorted((k.split(":", 1)[1] for k in art if k.startswith("category:")), key=lambda c: (rank.get(c, len(rank)), c))
    seq = RowSeq()
    for cat in cats:
        doc_ids = _category_doc_ids(groups, cat)
        facts = {k: v for k, v in facts_all.items() if k[0] in doc_ids} if doc_ids else facts_all
        v2 = consolidate_category(
            cat, groups, words_by_page=facts or None, ledger=ledger, documents=documents, llm_rollup=False, row_seq=seq,
        )
        if v2 is None:
            ctx.note(f"merge: category {cat} has no done sub group output; stored output kept")
            continue
        stored = art.get(f"category:{cat}") or {}
        for key, _p in ROW_KINDS:
            n_stored = len(stored.get(key) or [])
            n_active = sum(1 for r in v2.get(key) or [] if r.get("status") == "active")
            if n_stored != n_active:
                ctx.note(f"merge: {cat}.{key} stored {n_stored} rows, v2 active {n_active}")
        out[f"category:{cat}"] = v2
    return out


def register_replay_stage() -> None:
    """Register `merge` with plans_reader_replay (replaces the legacy entry)."""
    from app.services.plans_reader_replay import register_stage

    register_stage(
        "merge", replay_merge, version=MERGE_VERSION, reads=("groups", "documents", "ledger"),
        writes=("category:*",), owner="WS4",
    )


try:  # the replay registry is optional at import (scripts without WS0)
    register_replay_stage()
except Exception:  # noqa: BLE001 - never block the product import on the eval registry
    pass


__all__ = [
    "MERGE_VERSION", "DUPLICATE_SIMILARITY", "PATCH_KINDS", "DUPLICATE_REASON", "ORIGIN_KEY", "MergeError",
    "llm_rollup_enabled", "flags_by_sheet", "concat_outputs", "category_v1_output", "family_positions",
    "enrich_containment", "dedupe_duplicates", "conservation", "run_sequence", "consolidate_output", "consolidate_category",
    "V2_OUTPUT_KEYS", "V2_ROW_KEYS", "legacy_view", "replay_merge", "register_replay_stage",
]
