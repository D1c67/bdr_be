"""Dev-only Training data — model output vs the user's corrected output.

Backs the /training page (dev accounts only, any role). Today one feature
section — BOQ extraction examples captured on confirm — with room for more
capture surfaces to mount alongside. The exact model input/output are never
copied onto the example row; the detail route joins the pristine
`boq_analyses` row instead.

Handlers are plain `def` — the sync Supabase SDK runs in FastAPI's threadpool.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.core.deps import CurrentUser, require_dev
from app.core.supabase_client import get_supabase
from app.models.schemas import BoqTrainingReviewIn
from app.services.boq_training import gold_prompt_flags, reconstruct_gold

router = APIRouter(prefix="/training", tags=["training"])

# List rows exclude the (potentially huge) diff item list and user_output —
# the handler strips diff_json down to its counts; detail serves the rest.
# Export walks the whole table in pages; the cap is a runaway backstop far
# above any real example count on a dev database.
_EXPORT_PAGE = 200
_EXPORT_CAP = 5000

_LIST_SELECT = (
    "id, analysis_id, project_id, model, modified, diff_json, "
    "confirmed_by, confirmed_at, reviewed_by, reviewed_at, "
    "projects(name, number), "
    "confirmed_by_profile:profiles!boq_training_examples_confirmed_by_fkey(full_name), "
    "reviewed_by_profile:profiles!boq_training_examples_reviewed_by_fkey(full_name)"
)


@router.get("/boq")
def list_boq_examples(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    user: CurrentUser = Depends(require_dev),
):
    """Captured BOQ examples, newest confirm first."""
    resp = (
        get_supabase()
        .table("boq_training_examples")
        .select(_LIST_SELECT, count="exact")
        .order("confirmed_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = resp.data or []
    for row in rows:
        diff = row.pop("diff_json", None) or {}
        row["counts"] = diff.get("counts") or {}
    return {"rows": rows, "total": resp.count or 0, "offset": offset, "limit": limit}


# Registered BEFORE /boq/{example_id} — FastAPI matches in declaration order,
# so a later placement would swallow "export" as an example id.
@router.get("/boq/export")
def export_boq_examples(
    reviewed_only: bool = Query(False),
    modified_only: bool = Query(False),
    user: CurrentUser = Depends(require_dev),
):
    """Fine-tuning JSONL — one {system, user, assistant, meta} line per example.

    system/user are the run's frozen input_snapshot VERBATIM (held-group source
    content included — the input must stay real for the exclusion to be
    learnable); assistant is the corrected output rebuilt in the model's own
    schema. Examples missing their snapshot or user_output are skipped and
    counted in the X-Skipped-Count header.
    """
    sb = get_supabase()
    rows: list[dict] = []
    while len(rows) < _EXPORT_CAP:
        page = (
            sb.table("boq_training_examples")
            .select("*, boq_analyses(input_snapshot, result_json)")
            .order("confirmed_at", desc=False)
            .range(len(rows), len(rows) + _EXPORT_PAGE - 1)
            .execute()
        ).data or []
        rows.extend(page)
        if len(page) < _EXPORT_PAGE:
            break

    lines: list[str] = []
    skipped = 0
    for row in rows:
        if reviewed_only and not row.get("reviewed_by"):
            continue
        if modified_only and not row.get("modified"):
            continue
        analysis = row.get("boq_analyses") or {}
        snap = analysis.get("input_snapshot") or {}
        system_prompt, user_prompt = snap.get("system"), snap.get("user")
        if not system_prompt or not user_prompt:
            skipped += 1
            continue
        gold, flags = reconstruct_gold(analysis.get("result_json"), row.get("user_output"))
        if "no_user_output" in flags:
            skipped += 1
            continue
        flags += gold_prompt_flags(gold, system_prompt)
        lines.append(
            json.dumps(
                {
                    "system": system_prompt,
                    "user": user_prompt,
                    "assistant": json.dumps(gold, ensure_ascii=False),
                    "meta": {
                        "example_id": row.get("id"),
                        "analysis_id": row.get("analysis_id"),
                        "project_id": row.get("project_id"),
                        "model": row.get("model"),
                        "modified": row.get("modified"),
                        "reviewed": bool(row.get("reviewed_by")),
                        "confirmed_at": row.get("confirmed_at"),
                        "flags": flags,
                    },
                },
                ensure_ascii=False,
            )
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        content="\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="boq-training-{stamp}.jsonl"',
            "X-Example-Count": str(len(lines)),
            "X-Skipped-Count": str(skipped),
        },
    )


@router.get("/boq/{example_id}/gold")
def boq_example_gold(example_id: str, user: CurrentUser = Depends(require_dev)):
    """The corrected output rebuilt in the model's schema, plus quality flags."""
    rows = (
        get_supabase()
        .table("boq_training_examples")
        .select("user_output, boq_analyses(input_snapshot, result_json)")
        .eq("id", example_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Training example not found")
    analysis = rows[0].get("boq_analyses") or {}
    gold, flags = reconstruct_gold(analysis.get("result_json"), rows[0].get("user_output"))
    flags += gold_prompt_flags(gold, (analysis.get("input_snapshot") or {}).get("system"))
    return {"gold": gold, "flags": flags}


@router.get("/boq/{example_id}")
def boq_example_detail(example_id: str, user: CurrentUser = Depends(require_dev)):
    """Full example + the joined analysis (exact model input and output)."""
    rows = (
        get_supabase()
        .table("boq_training_examples")
        .select(
            "*, projects(name, number), "
            "confirmed_by_profile:profiles!boq_training_examples_confirmed_by_fkey(full_name), "
            "reviewed_by_profile:profiles!boq_training_examples_reviewed_by_fkey(full_name), "
            "boq_analyses(input_snapshot, result_json, status, created_at, boq_file_id)"
        )
        .eq("id", example_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Training example not found")
    return rows[0]


@router.patch("/boq/{example_id}/review")
def review_boq_example(
    example_id: str,
    body: BoqTrainingReviewIn,
    user: CurrentUser = Depends(require_dev),
):
    """Mark an example reviewed (with an optional note); false clears the review."""
    sb = get_supabase()
    exists = (
        sb.table("boq_training_examples").select("id").eq("id", example_id).limit(1).execute()
    ).data or []
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Training example not found")
    patch = (
        {
            "reviewed_by": user.id,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "review_note": body.note,
        }
        if body.reviewed
        else {"reviewed_by": None, "reviewed_at": None, "review_note": None}
    )
    sb.table("boq_training_examples").update(patch).eq("id", example_id).execute()
    return patch
