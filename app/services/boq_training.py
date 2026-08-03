"""BOQ extraction training capture — the user is the source of truth.

On confirm, one `boq_training_examples` row is written per analysis pairing the
model's pristine output (`boq_analyses.result_json`, joined at read time — never
copied) with the user's confirmed output, plus a computed diff of every
correction: quantity/unit edits, category moves, removals from a mapped group,
items the user added, and group names the model invented (renames). Held groups
are neutral — skipping a group is not a judgement on its items.

Re-confirming the same analysis upserts (latest confirm = truth) and resets any
prior review sign-off. The confirm handler wraps `capture_example` in
try/except: a capture bug must never fail the confirm that feeds it.

`reconstruct_gold` turns a captured example back into the completion the model
SHOULD have produced — same schema the extractor emits — pairing with the run's
frozen `input_snapshot` to make (system, user, assistant) fine-tuning triples.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.supabase_client import get_supabase
from app.models.schemas import BoqConfirmIn, BoqItemSrc

# Item-position key into result_json: sites[s].material_groups[g].items[i].
_Key = tuple[int, int, int]


def _num(value: Any) -> int | float | None:
    """Quantity → jsonb-safe int/float (int when integral), None when absent/junk."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _dec(value: Any) -> Decimal | None:
    """Normalize a quantity for comparison; None stays None, junk becomes None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def _src_dict(src: BoqItemSrc | None) -> dict[str, int] | None:
    return {"s": src.s, "g": src.g, "i": src.i} if src else None


def _index_model_items(result_json: Any) -> dict[_Key, dict[str, Any]]:
    """(s, g, i) → the model's item + its site/group context.

    Defensive on shape: result_json is model output, so any level may be
    missing, None, or the wrong type — such nodes are simply skipped.
    """
    index: dict[_Key, dict[str, Any]] = {}
    sites = result_json.get("sites") if isinstance(result_json, dict) else None
    for s, site in enumerate(sites if isinstance(sites, list) else []):
        if not isinstance(site, dict):
            continue
        groups = site.get("material_groups")
        for g, group in enumerate(groups if isinstance(groups, list) else []):
            if not isinstance(group, dict):
                continue
            items = group.get("items")
            for i, item in enumerate(items if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                index[(s, g, i)] = {
                    "description": item.get("description"),
                    "quantity": item.get("quantity"),
                    "unit": item.get("unit"),
                    "site_name": site.get("site_name"),
                    "group_name": group.get("group_name"),
                }
    return index


def reconstruct_gold(
    result_json: Any, user_output: Any
) -> tuple[dict[str, Any], list[str]]:
    """Rebuild the confirmed truth in the MODEL'S output schema — the completion
    the model should have produced for this input. Returns (gold, flags).

    Held groups are omitted on purpose: holding is the reviewer's judgement that
    the group does not belong in the output, and its source content stays in the
    input untouched — that pairing is what teaches the model to exclude it.
    Group names are the canonical category names (the list injected into that
    run's system prompt); `summary` rides through from the model verbatim (the
    user never corrects it); `total_material_count` is recomputed.
    """
    flags: set[str] = set()
    groups_in = user_output.get("groups") if isinstance(user_output, dict) else None
    if not isinstance(groups_in, list):
        groups_in = []
        flags.add("no_user_output")

    # Original worksheet order anchors the site order; confirmed items whose
    # site never appeared in the model output are appended in first-seen order.
    model_sites = result_json.get("sites") if isinstance(result_json, dict) else None
    if not isinstance(model_sites, list):
        model_sites = []
        flags.add("no_model_output")
    site_order: list[str] = []
    site_names: dict[str, str] = {}  # folded key → display name
    for site in model_sites:
        if isinstance(site, dict):
            name = site.get("site_name")
            key = _fold(name)
            if key not in site_names:
                site_names[key] = name if isinstance(name, str) else ""
                site_order.append(key)

    # Per site: one gold group per category, in first-confirmed order.
    by_site: dict[str, dict[str, list[dict[str, Any]]]] = {}
    cat_order: dict[str, list[str]] = {}
    cat_names: dict[str, str] = {}
    total = 0
    for group in groups_in:
        if not isinstance(group, dict):
            continue
        cat_id = str(group.get("material_category_id") or "")
        name = group.get("category_name")
        if not name:
            flags.add("missing_category_name")
            name = cat_id
        cat_names[cat_id] = name
        items = group.get("items")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            skey = _fold(item.get("site_name"))
            if skey not in site_names:
                site_names[skey] = str(item.get("site_name") or "")
                site_order.append(skey)
                flags.add("new_site")
            site_groups = by_site.setdefault(skey, {})
            if cat_id not in site_groups:
                site_groups[cat_id] = []
                cat_order.setdefault(skey, []).append(cat_id)
            site_groups[cat_id].append(
                {
                    "description": item.get("description"),
                    "quantity": _num(item.get("quantity")),
                    "unit": item.get("unit"),
                    "notes": item.get("notes"),
                }
            )
            total += 1

    sites_out: list[dict[str, Any]] = []
    for skey in site_order:
        cats = cat_order.get(skey) or []
        if not cats:
            # Every group of this worksheet was held or removed — the corrected
            # output has nothing here, so the whole site is omitted.
            flags.add("empty_site_omitted")
            continue
        sites_out.append(
            {
                "site_name": site_names[skey],
                "material_groups": [
                    {"group_name": cat_names[cid], "items": by_site[skey][cid]}
                    for cid in cats
                ],
            }
        )

    gold = {
        "sites": sites_out,
        "summary": result_json.get("summary") if isinstance(result_json, dict) else None,
        "total_material_count": total,
    }
    return gold, sorted(flags)


def gold_prompt_flags(gold: dict[str, Any], system_prompt: Any) -> list[str]:
    """Flag a gold output whose group names the run's frozen system prompt never
    mentions — a category created or renamed after the run. Such a pair would
    teach the model to emit names that were not on its list."""
    if not isinstance(system_prompt, str) or not system_prompt:
        return ["no_input_snapshot"]
    hay = system_prompt.casefold()
    for site in gold.get("sites") or []:
        for group in site.get("material_groups") or []:
            name = str(group.get("group_name") or "")
            if name and name.casefold() not in hay:
                return ["category_not_in_prompt"]
    return []


def capture_example(
    analysis: dict[str, Any],
    project_id: str,
    body: BoqConfirmIn,
    user_id: str,
    categories: dict[str, str],
) -> None:
    """Diff the confirmed payload against the model's output and upsert the
    training example. `categories` maps material_category_id → name."""
    model_items = _index_model_items(analysis.get("result_json"))
    mapping = {m.group_name: m.material_category_id for m in body.group_mappings}
    held = set(body.held_groups)

    # ── Per-item diff: edits / moves on confirmed items, plus pure additions ──
    diff_items: list[dict[str, Any]] = []
    confirmed_srcs: set[_Key] = set()
    items_confirmed = 0
    for group in body.groups:
        cat_id = group.material_category_id
        for item in group.items:
            items_confirmed += 1
            key = (item.src.s, item.src.g, item.src.i) if item.src else None
            model = model_items.get(key) if key else None
            if key is not None:
                confirmed_srcs.add(key)
            user_side = {
                "quantity": _num(item.quantity),
                "unit": item.unit,
                "category_id": cat_id,
                "category_name": categories.get(cat_id),
            }
            if model is None:
                # No src (hand-built payload) or out of range → the user added it.
                diff_items.append(
                    {
                        "src": _src_dict(item.src),
                        "description": item.description,
                        "site_name": item.site_name,
                        "from_group": None,
                        "changes": ["added"],
                        "model": None,
                        "user": user_side,
                    }
                )
                continue
            changes: list[str] = []
            if _dec(item.quantity) != _dec(model["quantity"]):
                changes.append("quantity")
            if _fold(item.unit) != _fold(model["unit"]):
                changes.append("unit")
            src_group = model.get("group_name")
            src_cat_id = mapping.get(src_group)  # None when unmapped/held
            # A move: the confirmed category differs from where the group
            # mapping would have put it. An unmapped source group is always a
            # move — the item only got in because the user relocated it.
            if src_group not in mapping or src_cat_id != cat_id:
                changes.append("category")
            if changes:
                diff_items.append(
                    {
                        "src": {"s": key[0], "g": key[1], "i": key[2]},
                        "description": model["description"],
                        "site_name": model["site_name"],
                        "from_group": src_group,
                        "changes": changes,
                        "model": {
                            "quantity": _num(model["quantity"]),
                            "unit": model["unit"],
                            "category_id": src_cat_id,
                            "category_name": categories.get(src_cat_id),
                        },
                        "user": user_side,
                    }
                )

    # ── Removals: a mapped group went in, but this item of it did not ─────────
    for key, model in model_items.items():
        group_name = model.get("group_name")
        if group_name not in mapping or group_name in held:
            continue  # held/unmapped groups are neutral — never "removed"
        if key in confirmed_srcs:
            continue
        src_cat_id = mapping.get(group_name)
        diff_items.append(
            {
                "src": {"s": key[0], "g": key[1], "i": key[2]},
                "description": model["description"],
                "site_name": model["site_name"],
                "from_group": group_name,
                "changes": ["removed"],
                "model": {
                    "quantity": _num(model["quantity"]),
                    "unit": model["unit"],
                    "category_id": src_cat_id,
                    "category_name": categories.get(src_cat_id),
                },
                "user": None,
            }
        )

    # ── Group mappings: renamed = the model invented/mislabeled the group ─────
    group_mappings = [
        {
            "group_name": m.group_name,
            "category_id": m.material_category_id,
            "category_name": categories.get(m.material_category_id),
            "renamed": _fold(m.group_name) != _fold(categories.get(m.material_category_id)),
        }
        for m in body.group_mappings
    ]

    held_groups = [
        {
            "group_name": name,
            "item_count": sum(
                1 for it in model_items.values() if it.get("group_name") == name
            ),
        }
        for name in body.held_groups
    ]

    counts = {
        "quantity": sum(1 for d in diff_items if "quantity" in d["changes"]),
        "unit": sum(1 for d in diff_items if "unit" in d["changes"]),
        "category": sum(1 for d in diff_items if "category" in d["changes"]),
        "removed": sum(1 for d in diff_items if d["changes"] == ["removed"]),
        "added": sum(1 for d in diff_items if d["changes"] == ["added"]),
        "group_renames": sum(1 for g in group_mappings if g["renamed"]),
        "items_total": len(model_items),
        "items_confirmed": items_confirmed,
    }
    modified = any(
        counts[k] > 0
        for k in ("quantity", "unit", "category", "removed", "added", "group_renames")
    )

    # Normalized confirmed payload — what the model SHOULD have produced.
    user_output = {
        "groups": [
            {
                "material_category_id": group.material_category_id,
                "category_name": categories.get(group.material_category_id),
                "items": [
                    {
                        "site_name": item.site_name,
                        "sr_no": item.sr_no,
                        "description": item.description,
                        "quantity": _num(item.quantity),
                        "unit": item.unit,
                        "notes": item.notes,
                        "src": _src_dict(item.src),
                    }
                    for item in group.items
                ],
            }
            for group in body.groups
        ]
    }

    get_supabase().table("boq_training_examples").upsert(
        {
            "analysis_id": analysis["id"],
            "project_id": project_id,
            "boq_file_id": analysis.get("boq_file_id"),
            "model": analysis.get("model"),
            "user_output": user_output,
            "diff_json": {
                "counts": counts,
                "items": diff_items,
                "group_mappings": group_mappings,
            },
            "modified": modified,
            "held_groups": held_groups,
            "confirmed_by": user_id,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            # A re-confirm changes the truth a reviewer signed off on — reset it.
            "reviewed_by": None,
            "reviewed_at": None,
            "review_note": None,
        },
        on_conflict="analysis_id",
    ).execute()
