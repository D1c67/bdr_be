"""Bid File Splitter training capture - the user's corrections are the truth.

Both correction endpoints (file-kind PATCH, segments PUT) call
`capture_correction` inside try/except: a capture bug must never fail the
correction that feeds it.

One `bid_split_training_examples` row per corrected source file, upserted on
file_id. The FIRST correction freezes the model side: the pre-mutation file
row and segment rows ARE the model's answer exactly as the user saw it, and
the file row's input_snapshot / model_output jsonbs (prompts, sample pages,
batch page lists, render settings, pristine triage verdict + per-page
classifications) ride along verbatim. Later corrections replace only the user
side and the diff, so every capture diffs against the MODEL, never against
the prior edit. Re-correcting resets any review sign-off (the truth the
reviewer signed off on changed).

Unlike BOQ examples, everything the Training page needs is denormalized onto
the example and the source PDF is copied server-side under
`bid-splits/training/{file_id}/`: splitter jobs are routinely deleted by
testers, and the training data must outlive them (the job-delete sweep only
walks `bid-splits/{job_id}/`).
"""

import logging
from datetime import datetime, timezone
from typing import Any

from app.services import storage

logger = logging.getLogger(__name__)


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def _segment_snapshot(seg: dict, *, confidence: bool) -> dict:
    """The training-relevant fields of one segment row. The model side keeps
    the model's confidence; the user side has none (a user verdict is not a
    model estimate)."""
    out = {
        "category": seg.get("category"),
        "other_type": seg.get("other_type"),
        "name": seg.get("name"),
        "page_start": seg.get("page_start"),
        "page_end": seg.get("page_end"),
        "is_original": bool(seg.get("is_original")),
    }
    if confidence:
        out["confidence"] = seg.get("confidence")
        out["confidence_reason"] = seg.get("confidence_reason")
    return out


def _page_map(segments: list[dict], page_count: int) -> dict[int, str | None]:
    """page (1-based) -> category over 1..page_count. Pages no segment claims
    map to None (user edits must cover the file exactly; model rows from odd
    states may leave holes, which then diff honestly)."""
    out: dict[int, str | None] = {p: None for p in range(1, page_count + 1)}
    for seg in segments:
        try:
            start, end = int(seg["page_start"]), int(seg["page_end"])
        except (KeyError, TypeError, ValueError):
            continue
        for p in range(max(1, start), min(page_count, end) + 1):
            out[p] = seg.get("category")
    return out


def _changed_runs(
    model_map: dict[int, str | None],
    user_map: dict[int, str | None],
    page_count: int,
) -> tuple[list[dict], int]:
    """Fold the pages whose category changed into contiguous runs sharing one
    (model, user) category pair. Returns (runs, changed page count)."""
    runs: list[dict] = []
    changed = 0
    for p in range(1, page_count + 1):
        model_cat, user_cat = model_map.get(p), user_map.get(p)
        if model_cat == user_cat:
            continue
        changed += 1
        last = runs[-1] if runs else None
        if (
            last is not None
            and last["page_end"] == p - 1
            and last["model_category"] == model_cat
            and last["user_category"] == user_cat
        ):
            last["page_end"] = p
        else:
            runs.append(
                {
                    "page_start": p,
                    "page_end": p,
                    "model_category": model_cat,
                    "user_category": user_cat,
                }
            )
    return runs, changed


def _copy_source(file_row: dict) -> str | None:
    """Server-side copy of the source PDF into the training prefix, so the
    example survives job deletion. Best-effort: on failure the example still
    captures - the page images just cannot be re-rendered later."""
    src = file_row["storage_path"]
    dst = (
        f"bid-splits/training/{file_row['id']}/"
        f"{storage.safe_key_component(file_row['filename'])}"
    )
    try:
        storage.copy_file(src, dst)
        return dst
    except Exception:  # noqa: BLE001 - the copy is a nice-to-have, capture proceeds
        logger.warning(
            "bid_split training: could not copy source %s to %s", src, dst
        )
        return None


def capture_correction(
    sb,
    file_row_before: dict,
    segments_before: list[dict],
    file_row_after: dict,
    segments_after: list[dict],
    user_id: str,
) -> None:
    """Upsert the training example for one corrected file. `file_row_before`
    must carry the heavy input_snapshot/model_output columns (the endpoints
    fetch them separately; every list/detail select excludes them)."""
    file_id = file_row_before["id"]
    existing = (
        sb.table("bid_split_training_examples")
        .select("model_output, input_snapshot, training_source_path")
        .eq("file_id", file_id)
        .limit(1)
        .execute()
    ).data or []

    if existing:
        # The model side froze on the first correction - keep it, and keep
        # the source copy; only the user side and the diff move.
        model_output = existing[0]["model_output"]
        input_snapshot = existing[0]["input_snapshot"]
        training_source_path = existing[0]["training_source_path"]
    else:
        # First correction: the pre-mutation state IS the model's answer as
        # the user saw it. "raw" is the run's pristine triage verdict +
        # per-page classifications (None on runs from before 0113).
        model_output = {
            "file_kind": file_row_before.get("file_kind"),
            "file_kind_label": file_row_before.get("file_kind_label"),
            "file_kind_confidence": file_row_before.get("file_kind_confidence"),
            "file_kind_confidence_reason": file_row_before.get(
                "file_kind_confidence_reason"
            ),
            "segments": [
                _segment_snapshot(s, confidence=True) for s in segments_before
            ],
            "raw": file_row_before.get("model_output") or None,
        }
        input_snapshot = {
            "source_path": file_row_before["storage_path"],
            "filename": file_row_before["filename"],
            "page_count": file_row_before.get("page_count"),
            **(file_row_before.get("input_snapshot") or {}),
        }
        training_source_path = _copy_source(file_row_before)

    user_output = {
        "file_kind": file_row_after.get("file_kind"),
        "file_kind_label": file_row_after.get("file_kind_label"),
        "segments": [
            _segment_snapshot(s, confidence=False) for s in segments_after
        ],
    }

    # Kind diff against the EXAMPLE's stored model output, so later
    # corrections always diff against the model, not the prior edit. A label
    # change on 'other' counts as a kind change - relabeling IS a correction.
    kind_changed = model_output.get("file_kind") != user_output["file_kind"] or _fold(
        model_output.get("file_kind_label")
    ) != _fold(user_output["file_kind_label"])

    page_count = (
        file_row_after.get("page_count") or file_row_before.get("page_count") or 0
    )
    model_map = _page_map(model_output.get("segments") or [], page_count)
    user_map = _page_map(user_output["segments"], page_count)
    runs, pages_changed = _changed_runs(model_map, user_map, page_count)

    flags: list[str] = []
    if model_output.get("raw") is None:
        flags.append("no_raw_model_output")

    diff_json = {
        "kind": {
            "model": model_output.get("file_kind"),
            "user": user_output["file_kind"],
            "changed": kind_changed,
        },
        "pages": {"changed_runs": runs},
        "counts": {
            "kind_changed": 1 if kind_changed else 0,
            "pages_changed": pages_changed,
            "runs_changed": len(runs),
            "segments_model": len(model_output.get("segments") or []),
            "segments_user": len(user_output["segments"]),
        },
        "flags": flags,
    }
    modified = kind_changed or pages_changed > 0

    # Denormalized on purpose (see the module docstring): the example must
    # answer the Training page even after its file/job rows are gone.
    job_id = file_row_before.get("job_id")
    model = None
    if job_id:
        jobs = (
            sb.table("bid_split_jobs")
            .select("model")
            .eq("id", job_id)
            .limit(1)
            .execute()
        ).data or []
        model = jobs[0].get("model") if jobs else None

    sb.table("bid_split_training_examples").upsert(
        {
            "file_id": file_id,
            "job_id": job_id,
            "source_filename": file_row_before["filename"],
            "page_count": file_row_before.get("page_count"),
            "model": model,
            "input_snapshot": input_snapshot,
            "model_output": model_output,
            "user_output": user_output,
            "diff_json": diff_json,
            "modified": modified,
            "training_source_path": training_source_path,
            "corrected_by": user_id,
            "corrected_at": datetime.now(timezone.utc).isoformat(),
            # A re-correction changes the truth a reviewer signed off on - reset it.
            "reviewed_by": None,
            "reviewed_at": None,
            "review_note": None,
        },
        on_conflict="file_id",
    ).execute()
