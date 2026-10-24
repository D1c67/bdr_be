"""The facts gate (WS1 step 9, pure part): what `_advance_splitting` waits
on before a run leaves `splitting`, as functions over plain document dicts.

Wave 2 wires `ensure_facts(sb, run, documents, s)` around `plan()`: it
enqueues one `plans_reader_facts` job per document the decision names,
patches `facts_status`, and returns `decision.ready`. Nothing here touches a
database or a queue, so the rules are testable on dicts:

- a document with `facts_status` NULL or `pending` and no job yet is
  enqueued once;
- `running` is waited on; the gate keeps no stale guard of its own (no
  `facts_started_at`, no attempt counter: migration MB adds only sha256,
  facts_status, facts_error, category_label and facts_doc). A worker that
  dies mid-document loses its queue lease (`LEASE_S`, renewed every
  `LEASE_RENEW_EVERY_PAGES` pages) and the queue hands the job out again;
  re-enqueueing from the gate could duplicate a job whose lease is still
  being renewed;
- `done`, `failed` and `skipped` are settled; the run never waits on a
  failed document (facts are an enhancement, never a blocker);
- a value outside `FACTS_STATUSES` is marked `failed` so a corrupt row can
  never hold a run in `splitting`;
- with the flag off the gate is always ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.pagefacts.extract import kind_for_label

JOB_PLANS_FACTS = "plans_reader_facts"
FACTS_STATUSES: tuple[str, ...] = ("pending", "running", "done", "failed", "skipped")
SETTLED_STATUSES: frozenset[str] = frozenset({"done", "failed", "skipped"})
LEASE_S = 900
LEASE_RENEW_EVERY_PAGES = 30


@dataclass(slots=True)
class GateDecision:
    ready: bool
    enqueue: list[str] = field(default_factory=list)  # document ids: first job
    mark_failed: list[str] = field(default_factory=list)  # document ids: unknown status value
    waiting_on: list[str] = field(default_factory=list)  # document ids still unsettled
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "enqueue": list(self.enqueue),
            "mark_failed": list(self.mark_failed),
            "waiting_on": list(self.waiting_on),
            "reasons": list(self.reasons),
        }


def settled(status: str | None) -> bool:
    return status in SETTLED_STATUSES


def plan(
    documents: list[dict[str, Any]],
    *,
    enabled: bool,
    pending_jobs: set[str] | None = None,
) -> GateDecision:
    """The gate decision for one run's documents. `pending_jobs` holds the
    document ids that already have a queued or running facts job."""
    if not enabled:
        return GateDecision(ready=True, reasons=["page facts disabled"])
    pending_jobs = pending_jobs or set()
    decision = GateDecision(ready=True)
    for doc in documents:
        doc_id = str(doc.get("id") or "")
        status = doc.get("facts_status")
        if settled(status):
            continue
        if status in (None, "pending"):
            if doc_id not in pending_jobs:
                decision.enqueue.append(doc_id)
            decision.waiting_on.append(doc_id)
            decision.ready = False
            continue
        if status == "running":
            decision.waiting_on.append(doc_id)
            decision.ready = False
            continue
        decision.reasons.append(f"{doc_id}: unknown facts_status {status!r}")
        decision.mark_failed.append(doc_id)
    return decision


def facts_job_payload(run_id: str, document: dict[str, Any]) -> dict[str, Any]:
    """The `llm_jobs` payload for one document's facts job."""
    label = document.get("label") or document.get("category_label")
    return {
        "job_type": JOB_PLANS_FACTS,
        "run_id": run_id,
        "document_id": str(document.get("id") or ""),
        "storage_path": document.get("storage_path"),
        "label": label,
        "kind": kind_for_label(label),
        "lease_s": LEASE_S,
    }


def runs_to_advance(document_id: str, runs: list[dict[str, Any]]) -> list[str]:
    """Run ids in `splitting` that hold the document (the facts job ends by
    calling `plans_reader_runs.advance` for each)."""
    out: list[str] = []
    for run in runs:
        if run.get("status") != "splitting":
            continue
        doc_ids = {
            str(d.get("id")) if isinstance(d, dict) else str(d)
            for d in (run.get("documents") or [])
        }
        if document_id in doc_ids:
            out.append(str(run.get("id")))
    return out


def status_after_job(ok: bool, *, error: str | None = None) -> dict[str, Any]:
    """The document patch a facts job writes when it ends."""
    if ok:
        return {"facts_status": "done", "facts_error": None}
    return {"facts_status": "failed", "facts_error": (error or "facts extraction failed")[:500]}
