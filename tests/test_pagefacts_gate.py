"""pagefacts.gate: the pure facts-gate decisions `_advance_splitting` will
act on (enqueue once, wait on pending and running documents, never wait on
failed or skipped facts, fail an unknown status value, disabled means
ready), the job payload and the runs a finished job advances. The gate has
no stale guard of its own: a dead worker is recovered by the queue lease.

No network, no database, no model.
"""

from __future__ import annotations

from app.services.pagefacts import gate


def _doc(doc_id: str, status: str | None, **kw) -> dict:
    return {"id": doc_id, "facts_status": status, **kw}


def test_disabled_gate_is_always_ready():
    d = gate.plan([_doc("a", None), _doc("b", "running")], enabled=False)
    assert d.ready is True and d.enqueue == [] and d.reasons == ["page facts disabled"]


def test_pending_documents_are_enqueued_once_and_waited_on():
    docs = [
        _doc("a", None),
        _doc("b", "pending"),
        _doc("c", "done"),
        _doc("d", "failed"),
        _doc("e", "skipped"),
    ]
    d = gate.plan(docs, enabled=True)
    assert d.ready is False
    assert d.enqueue == ["a", "b"] and d.waiting_on == ["a", "b"]
    assert d.mark_failed == []
    again = gate.plan(docs, enabled=True, pending_jobs={"a", "b"})
    assert again.enqueue == [] and again.waiting_on == ["a", "b"] and again.ready is False
    assert gate.plan(docs[2:], enabled=True).ready is True
    assert d.to_dict()["waiting_on"] == ["a", "b"]


def test_running_is_waited_on_without_a_stale_guard():
    """A running document is waited on however old it is: the queue lease,
    not the gate, recovers a dead worker, so no re-enqueue can duplicate a
    job whose lease is still being renewed."""
    running = _doc("a", "running", updated_at="2026-10-21T10:00:00+00:00")
    d = gate.plan([running], enabled=True)
    assert d.waiting_on == ["a"] and d.ready is False
    assert d.enqueue == [] and d.mark_failed == [] and d.reasons == []
    assert not hasattr(gate, "is_stale") and not hasattr(gate, "STALE_RUNNING_S")
    assert "reenqueue" not in d.to_dict()
    assert gate.LEASE_S == 900 and gate.LEASE_RENEW_EVERY_PAGES == 30


def test_unknown_status_is_failed_not_waited_on():
    d = gate.plan([_doc("a", "weird")], enabled=True)
    assert d.mark_failed == ["a"] and d.ready is True and "unknown facts_status" in d.reasons[0]


def test_job_payload_runs_to_advance_and_status_patch():
    payload = gate.facts_job_payload(
        "run-1", {"id": "doc-1", "storage_path": "p/x.pdf", "label": "specifications"}
    )
    assert payload == {
        "job_type": "plans_reader_facts",
        "run_id": "run-1",
        "document_id": "doc-1",
        "storage_path": "p/x.pdf",
        "label": "specifications",
        "kind": "text",
        "lease_s": 900,
    }
    assert (
        gate.facts_job_payload("r", {"id": "d", "category_label": "mechanical_drawings"})["kind"]
        == "drawing"
    )
    runs = [
        {"id": "r1", "status": "splitting", "documents": [{"id": "d1"}, {"id": "d2"}]},
        {"id": "r2", "status": "reading", "documents": [{"id": "d1"}]},
        {"id": "r3", "status": "splitting", "documents": ["d3"]},
    ]
    assert gate.runs_to_advance("d1", runs) == ["r1"] and gate.runs_to_advance("d3", runs) == ["r3"]
    assert gate.status_after_job(True) == {"facts_status": "done", "facts_error": None}
    assert gate.status_after_job(False, error="boom") == {
        "facts_status": "failed",
        "facts_error": "boom",
    }
    assert (
        gate.settled("done")
        and gate.settled("skipped")
        and not gate.settled("running")
        and not gate.settled(None)
    )
    assert gate.JOB_PLANS_FACTS == "plans_reader_facts" and set(gate.SETTLED_STATUSES) <= set(
        gate.FACTS_STATUSES
    )


def test_settings_snapshot_carries_every_page_facts_flag_and_cap():
    import app.services.pagefacts as pagefacts
    from app.core.config import get_settings

    s = get_settings().model_copy(update={"plans_reader_page_facts_enabled": True, "plans_reader_ledger_scales_max_tokens": 250})
    snap = pagefacts.settings_snapshot(s)
    assert set(pagefacts.PAGE_FACTS_SETTINGS) <= set(snap) and len(pagefacts.PAGE_FACTS_SETTINGS) == 11
    for name in pagefacts.PAGE_FACTS_SETTINGS:
        assert snap[name] == getattr(s, name), name
    assert snap["plans_reader_page_facts_enabled"] is True and snap["plans_reader_ledger_scales_max_tokens"] == 250
    assert snap["page_facts_extractor_version"] == pagefacts.EXTRACTOR_VERSION
    import json

    json.dumps(snap)  # stored in a jsonb column
