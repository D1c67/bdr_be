"""The splitter handoff tools (app/assistant/tools/splits.py;
docs/ASSISTANT_PHASE2_PLAN.md 3.10, 3.12, 3.13).

  literals  the route templates, the idempotency header, the per-card
            limits and the writes_tables equal the route and service side's
            (and WI-3's recording-fake copies);
  specs     effects, roles, ceilings, schemas, the turn allowlist; the
            Accountant gets splitter_status only, Mailbox review no write;
  status    splitter_status requests and trimming (short ids, empty lists
            kept, names only under the tool-specific taint fields);
  split     every refusal of 3.10.1 from fake preview reads; the request,
            target, card (cost strings, totals ints, no em dash) and the
            precondition changes; result_summary and created_ref over the
            201, replay and by-key bodies;
  attach    every refusal of 3.10.2, the request, target and card;
  skill     splitting-bid-files loads under its folder name;
  evals     run_evals --check passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.assistant import approvals, registry, skills_index
from app.assistant.approvals import canonical_json, precondition_for
from app.assistant.registry import BuildContext, ProposalRefused
from app.assistant.tools import splits
from app.assistant.tools._common import ToolArgumentError
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role

EM_DASH = chr(0x2014)
PROJECT = "77777777-7777-4777-8777-777777777777"
APPROVAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
F1 = "f1f1f1f1-f1f1-4f1f-8f1f-f1f1f1f1f1f1"
F2 = "f2f2f2f2-f2f2-4f2f-8f2f-f2f2f2f2f2f2"
JOB = "a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1"
TASK = "b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1"
OTHER_PROJECT = "c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1"
NUMBER = "26.10.7267"
ON = {"bidding": True, "pm": True, "certified_payroll": True}
NEW = ("splitter_status", "split_files_into_project", "attach_split_job_to_project")
CONFIRMS = ("split_files_into_project", "attach_split_job_to_project")


def _spec(name):
    return registry.all_tools()[name]


def _ctx(**over) -> BuildContext:
    base = dict(approval_id=APPROVAL, role=Role.EXECUTIVE, short=lambda v: str(v)[:8], tainted=False,
                stripped=(), now=datetime.now(timezone.utc), is_revert=False)
    base.update(over)
    return BuildContext(**base)


def _project(**over) -> dict:
    base = {"id": PROJECT, "number": NUMBER, "name": "ZZ TEST Split", "current_stage": "labor_numbers",
            "abandoned_at": None, "failed_to_bid_at": None,
            "category_state": {"send_out": {"current_task": "gc_pricing", "status": "active"}}}
    base.update(over)
    return base


def _file(fid=F1, **over) -> dict:
    base = {"file_id": fid, "filename": f"E-{fid[:2]} Lighting.pdf", "pages": 23, "size_bytes": 12_998_000,
            "sha256": fid[:2] * 32, "problem": None, "duplicate_of": None}
    base.update(over)
    return base


def _preview(files=None, **over) -> dict:
    """GET /projects/{id}/split-drop/preview as split_handoff.split_preview serves it."""
    files = [_file(F1), _file(F2, pages=12, size_bytes=400_000)] if files is None else files
    base = {
        "project": {"id": PROJECT, "number": NUMBER, "name": "ZZ TEST Split"},
        "handoff_locked": False,
        "splitter": {"available": True, "reason": None},
        "files": files,
        "totals": {"files": len(files), "pages": sum(f["pages"] for f in files)},
        "limits": {"max_files": 10, "max_pages": 400},
        "estimate": {"usd": "1.23", "model": "claude-opus-5", "provider": "anthropic", "priced": True},
        "spend": {"user_remaining_usd": "4.50", "global_remaining_usd": "48.00"},
    }
    base.update(over)
    return base


def _split_args(ids=(F1, F2)) -> dict:
    return {"project_id": PROJECT, "file_ids": list(ids)}


def _split(preview=None, project=None, args=None, **ctx_over):
    args = args or _split_args()
    reads = [preview or _preview(), project or _project()]
    proposal = _spec("split_files_into_project").write.build(args, reads, _ctx(**ctx_over))
    _check(proposal, "split_files_into_project", args, reads)
    return proposal


def _refused(build_fn, *a, **k) -> str:
    with pytest.raises(ProposalRefused) as exc:
        build_fn(*a, **k)
    return str(exc.value)


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


def _check(proposal, name, args, reads):
    spec = _spec(name)
    assert proposal.request.route == spec.route
    canonical_json(proposal.request.json)
    canonical_json(dict(proposal.request.headers))
    target, gate = spec.write.snapshot(args, reads)
    assert (proposal.target, proposal.gate) == (target, gate)
    precondition_for(target, gate)
    for text in _walk_strings(proposal.preview):
        assert EM_DASH not in text
    assert EM_DASH not in proposal.summary
    for req in spec.write.preview_requests(args):
        assert req.route in spec.routes


# ── literals ─────────────────────────────────────────────────────────────────


def test_route_literals_equal_the_routes():
    from app.routers import projects

    assert splits.SPLIT_DROP_POST == ("POST", projects.SPLIT_DROP_ROUTE)
    assert splits.SPLIT_DROP_PREVIEW == ("GET", projects.SPLIT_DROP_PREVIEW_ROUTE)
    assert splits.SPLIT_DROP_BY_KEY == ("GET", projects.SPLIT_DROP_BY_KEY_ROUTE)
    assert splits.ATTACH_POST == ("POST", projects.ATTACH_SPLIT_JOB_ROUTE)
    assert splits.ATTACH_PREVIEW == ("GET", projects.ATTACH_SPLIT_JOB_PREVIEW_ROUTE)
    assert splits.ATTACH_GET == ("GET", projects.ATTACH_SPLIT_JOB_GET_ROUTE)
    assert splits.ASSISTANT_IDEMPOTENCY_HEADER == projects.ASSISTANT_IDEMPOTENCY_HEADER


def test_status_route_is_the_assistant_routers():
    from fastapi.routing import APIRoute

    from app.routers import assistant as assistant_router

    routes = {(m, r.path_format) for r in assistant_router.router.routes if isinstance(r, APIRoute)
              for m in r.methods or ()}
    prefix = assistant_router.router.prefix
    assert ("GET", splits.SPLIT_STATUS_GET[1].removeprefix(prefix)) in routes or splits.SPLIT_STATUS_GET in routes


def test_limits_equal_the_service():
    from app.services import split_handoff

    assert splits.MAX_FILES == split_handoff.PER_CARD_MAX_FILES
    assert splits.MAX_PAGES == split_handoff.PER_CARD_MAX_PAGES
    assert splits.SPLIT_TOOL == split_handoff.SPLIT_TOOL


def test_writes_tables_equal_the_recording_fake_copies():
    from tests import test_split_handoff

    assert _spec("split_files_into_project").write.writes_tables == test_split_handoff.SPLIT_WRITES_TABLES
    assert _spec("attach_split_job_to_project").write.writes_tables == test_split_handoff.ATTACH_WRITES_TABLES
    assert {"projects", "rfp_file_issues"} <= set(splits.SPLIT_WRITES_TABLES)
    assert {"projects", "rfp_file_issues"} <= set(splits.ATTACH_WRITES_TABLES)


# ── specs ────────────────────────────────────────────────────────────────────


def test_effects_roles_and_ceilings():
    status, split, attach = (_spec(n) for n in NEW)
    assert status.effect == "read" and status.roles == INTERNAL_ROLES and not status.confirm
    assert status.taint_fields == ("staged_filename", "job_first_filename")
    assert split.effect == "expensive" and split.confirm and split.roles == WRITER_ROLES
    assert (split.max_cards_per_turn, split.max_proposals_per_turn) == (1, 3)
    assert attach.effect == "write_internal" and attach.confirm and attach.roles == WRITER_ROLES
    assert split.write.creates == "split_task" and attach.write.creates == "split_attach"
    for spec in (status, split, attach):
        assert spec.namespace == "bidding" and spec.sub_app == "bidding" and spec.version == "1"
    for name in CONFIRMS:
        spec = _spec(name)
        assert spec.write.revert_tool is None and spec.write.recover_request is not None
        req = spec.write.recover_request({"project_id": PROJECT, "job_id": JOB}, APPROVAL)
        assert req.route in spec.extra_routes and req.route[0] == "GET" and "{" not in req.path
    by_key = split.write.recover_request(_split_args(), APPROVAL)
    assert by_key.path == f"/projects/{PROJECT}/split-drop/by-key/{APPROVAL}"
    attach_get = attach.write.recover_request({"project_id": PROJECT, "job_id": JOB}, APPROVAL)
    assert attach_get.path == f"/projects/{PROJECT}/attach-split-job/{JOB}"


def test_schemas_are_strict_and_model_text_has_no_em_dash():
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                if key == "properties":
                    for sub in value.values():
                        yield from keys(sub)
                elif key == "items":
                    yield from keys(value)

    for name in NEW:
        spec = _spec(name)
        assert set(keys(spec.schema)) <= allowed, name
        assert spec.schema["additionalProperties"] is False
        assert set(spec.schema["required"]) == set(spec.schema["properties"])
        assert EM_DASH not in json.dumps(spec.definition())
    assert set(_spec("split_files_into_project").schema["properties"]) == {"project_id", "file_ids"}
    assert set(_spec("attach_split_job_to_project").schema["properties"]) == {"project_id", "job_id"}
    assert set(_spec("splitter_status").schema["properties"]) == {"project_id", "response_format"}


def _user(role):
    return SimpleNamespace(id="u", role=role, is_active=True)


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES))
def test_toolsets(role):
    names = {s.name for s in registry.toolset_for(_user(role), ON)} & set(NEW)
    if role == Role.ACCOUNTANT:
        assert names == {"splitter_status"}
    else:
        assert names == set(NEW)
    review = {s.name for s in registry.toolset_for(_user(role), ON, mode=registry.MODE_MAILBOX_REVIEW)}
    assert not review & set(CONFIRMS)
    assert not {s.name for s in registry.toolset_for(_user(role), {**ON, "bidding": False})} & set(NEW)


def test_turn_allowlist_reaches_the_previews_never_the_writes():
    allow = registry.turn_allowlist(registry.all_tools().values())
    assert splits.SPLIT_DROP_POST not in allow and splits.ATTACH_POST not in allow
    assert {splits.SPLIT_DROP_PREVIEW, splits.ATTACH_PREVIEW, splits.SPLIT_STATUS_GET} <= allow


def test_preview_paths_resolve_to_their_templates():
    import app.main
    from app.assistant import firewall

    for path, template in ((f"/projects/{PROJECT}/split-drop/preview", splits.SPLIT_DROP_PREVIEW[1]),
                           (f"/projects/{PROJECT}/attach-split-job/preview", splits.ATTACH_PREVIEW[1])):
        scope = {"type": "http", "method": "GET", "path": path}
        assert firewall.resolve_path_format(app.main.app, scope) == template


# ── splitter_status ──────────────────────────────────────────────────────────


def _status_body() -> dict:
    return {
        "splitter": {"available": True, "reason": None, "max_pages_per_file": 600},
        "limits": {"max_files_per_card": 10, "max_pages_per_card": 400},
        "staged": [{"file_id": F1, "staged_filename": "E-101 Lighting.pdf", "pages": 23, "size_bytes": 12998000,
                    "size_mb": 12.4, "message_id": TASK, "expires_at": "2026-10-29T17:00:00+00:00"}],
        "jobs": [{"job_id": JOB, "created_at": "2026-10-21T17:00:00+00:00", "status": "done", "files": 2,
                  "files_done": 2, "files_failed": 0, "job_first_filename": "Bid set.pdf"}],
        "tasks": [{"task_id": TASK, "status": "waiting_external", "project_id": PROJECT,
                   "project_number": NUMBER, "files": 2, "failure_reason": None,
                   "created_at": "2026-10-22T17:00:00+00:00"}],
        "project": {"id": PROJECT, "number": NUMBER, "handoff_locked": False},
        "spend": {"user_remaining_usd": "4.50", "global_remaining_usd": "48.00"},
    }


def _trim_ctx(fmt="concise"):
    return registry.TrimContext(response_format=fmt, limit=20, short=lambda v: str(v)[:8])


def test_status_requests():
    spec = _spec("splitter_status")
    bare = spec.requests({"project_id": None, "response_format": "concise"})
    assert [(r.route, r.path, dict(r.params)) for r in bare] == [(splits.SPLIT_STATUS_GET, "/assistant/split/status", {})]
    scoped = spec.requests({"project_id": PROJECT, "response_format": "concise"})
    assert dict(scoped[0].params) == {"project_id": PROJECT}
    with pytest.raises(ToolArgumentError):
        spec.requests({"project_id": "nope", "response_format": "concise"})


def test_status_trim_shortens_ids_and_keeps_names_under_their_fields():
    out = _spec("splitter_status").response_trimmer([_status_body()], {}, _trim_ctx())
    assert out["staged"] == [{"file_id": F1[:8], "staged_filename": "E-101 Lighting.pdf", "pages": 23,
                              "size_mb": 12.4}]
    assert out["jobs"][0]["job_id"] == JOB[:8] and out["jobs"][0]["job_first_filename"] == "Bid set.pdf"
    assert out["jobs"][0]["created_at"].endswith("PT")
    assert out["tasks"][0] == {"task_id": TASK[:8], "status": "waiting_external", "project_number": NUMBER,
                               "files": 2, "created_at": out["tasks"][0]["created_at"]}
    assert out["project"] == {"number": NUMBER, "handoff_locked": False}
    assert out["limits"] == {"max_files_per_card": 10, "max_pages_per_card": 400}
    assert out["spend"] == {"user_remaining_usd": "4.50", "global_remaining_usd": "48.00"}
    assert "filename" not in json.dumps(out).replace("staged_filename", "").replace("job_first_filename", "")
    assert "staged_hint" not in out
    detailed = _spec("splitter_status").response_trimmer([_status_body()], {}, _trim_ctx("detailed"))
    assert detailed["staged"][0]["expires"].endswith("PT")


def test_status_trim_keeps_empty_lists_and_hints():
    body = {**_status_body(), "staged": [], "jobs": [], "tasks": [], "project": None}
    out = _spec("splitter_status").response_trimmer([body], {}, _trim_ctx())
    assert out["staged"] == [] and out["jobs"] == [] and out["tasks"] == [] and out["project"] is None
    assert "attach" in out["staged_hint"]
    off = _spec("splitter_status").response_trimmer(
        [{**body, "splitter": {"available": False, "reason": "the job queue is off"}}], {}, _trim_ctx())
    assert off["splitter"] == {"available": False, "reason": "the job queue is off"}


# ── split_files_into_project ─────────────────────────────────────────────────


def test_split_request_target_and_card():
    p = _split()
    assert p.request.route == splits.SPLIT_DROP_POST and p.request.path == f"/projects/{PROJECT}/split-drop"
    assert p.request.json == {"file_ids": [F1, F2], "max_usd": "1.23"}
    assert dict(p.request.headers) == {splits.ASSISTANT_IDEMPOTENCY_HEADER: APPROVAL}
    assert p.project_id == PROJECT
    assert p.target == {
        "files": sorted([[F1, F1[:2] * 32, 23, None], [F2, F2[:2] * 32, 12, None]]),
        "locked": False, "splitter": True, "estimate_usd": "1.23", "model": "claude-opus-5",
        "provider": "anthropic",
    }
    assert p.gate == {"closed": False, "phase": "open"}
    card = p.preview
    assert card["title"] == f"Split 2 files into {NUMBER}"
    rows = {r["field"]: r["after"] for r in card["rows"]}
    assert rows == {"project": NUMBER, "files": "2", "pages": "35", "estimate_usd": "up to $1.23",
                    "user_remaining_usd": "$4.50", "global_remaining_usd": "$48.00"}
    assert [r["label"] for r in card["rows"]] == [
        "Project", "Files", "Pages", "Estimated cost", "Your remaining today", "Company remaining today"]
    assert card["items"] == [
        {"label": "E-f1 Lighting.pdf", "before": "12.4 MB", "after": "23 pages"},
        {"label": "E-f2 Lighting.pdf", "before": "391 KB", "after": "12 pages"},
    ]
    assert card["cost"] == {"estimate_usd": "1.23", "user_remaining_usd": "4.50", "global_remaining_usd": "48.00"}
    assert card["totals"] == {"files": 2, "pages": 35}
    assert [w["code"] for w in card["warnings"]] == ["split_cost", "split_async"]
    assert card["project"] == {"id": PROJECT, "number": NUMBER, "name": "ZZ TEST Split"}
    # The title names the project number only, never a file name.
    assert "Lighting" not in card["title"] and "Lighting" not in p.summary
    one = _split(_preview([_file(F1, pages=1)]), args=_split_args([F1]))
    assert one.preview["title"] == f"Split 1 file into {NUMBER}"
    assert one.preview["items"][0]["after"] == "1 page"


def test_split_card_survives_the_approvals_preview_cleaner():
    p = _split()
    final = approvals._final_preview(p.preview, _spec("split_files_into_project"), tainted=False, level="none",
                                     stripped=[])
    assert final["cost"] == p.preview["cost"] and final["totals"] == p.preview["totals"]
    assert all(isinstance(v, str) for v in final["cost"].values())
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in final["totals"].values())
    canonical_json(final)


def test_split_refuses_a_closed_or_locked_project_and_an_unavailable_splitter():
    assert "abandoned" in _refused(_split, project=_project(abandoned_at="2026-10-01T00:00:00Z"))
    locked = _refused(_split, _preview(handoff_locked=True))
    assert NUMBER in locked and "Revisions on the To Estimator step" in locked
    off = _refused(_split, _preview(splitter={"available": False, "reason": "the job queue is off"}))
    assert "the job queue is off" in off


@pytest.mark.parametrize("problem,phrase", [
    ("not_found", "no longer staged"), ("consumed", "no longer staged"), ("expired", "no longer staged"),
    ("not_attached", "was not attached in this conversation"),
    ("other_conversation", "was not attached in this conversation"),
    ("in_open_card", "already in a split card waiting for approval"),
])
def test_split_refuses_each_file_problem(problem, phrase):
    text = _refused(_split, _preview([_file(F1), _file(F2, problem=problem)]))
    assert phrase in text and F2[:8] in text


def test_split_refuses_a_file_missing_from_the_preview():
    assert "no longer staged" in _refused(_split, _preview([_file(F1)]))


def test_split_refuses_a_duplicate():
    dupe = {"project_file_id": TASK, "filename": "E-101.pdf", "category": "drawing"}
    text = _refused(_split, _preview([_file(F1), _file(F2, duplicate_of=dupe)]))
    assert text == f"File {F2[:8]} is already on {NUMBER} as drawing."


def test_split_limits():
    many = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(11)]
    with pytest.raises(ToolArgumentError):
        _spec("split_files_into_project").write.build(_split_args(many), [_preview(), _project()], _ctx())
    with pytest.raises(ToolArgumentError):
        _spec("split_files_into_project").requests(_split_args(many))
    with pytest.raises(ToolArgumentError):
        _spec("split_files_into_project").requests(_split_args([]))
    with pytest.raises(ToolArgumentError):
        _spec("split_files_into_project").requests(_split_args([F1, F1]))
    with pytest.raises(ToolArgumentError):
        _spec("split_files_into_project").requests(_split_args(["nope"]))
    text = _refused(_split, _preview([_file(F1, pages=300), _file(F2, pages=101)]))
    assert "401 pages" in text and "400" in text
    _split(_preview([_file(F1, pages=300), _file(F2, pages=100)]))


def test_split_refuses_an_unpriced_estimate():
    assert "cannot be estimated" in _refused(
        _split, _preview(estimate={"usd": None, "model": "x", "provider": "openai", "priced": False}))


def test_split_refuses_an_estimate_above_either_remaining():
    user = _refused(_split, _preview(spend={"user_remaining_usd": "1.00", "global_remaining_usd": "48.00"}))
    assert user == ("The split would cost up to $1.23; your remaining today is $1.00. Split fewer pages or ask "
                    "an Executive to raise the cap.")
    company = _refused(_split, _preview(spend={"user_remaining_usd": "9.00", "global_remaining_usd": "0.50"}))
    assert "the company's remaining today is $0.50" in company
    # The reserve RPC refuses spent + held + amount >= cap: an estimate equal
    # to the remaining is refused here too, never shown as fitting.
    exact = _refused(_split, _preview(spend={"user_remaining_usd": "1.23", "global_remaining_usd": "48.00"}))
    assert "your remaining today is $1.23" in exact
    fits = _split(_preview(spend={"user_remaining_usd": "1.24", "global_remaining_usd": "1.24"}))
    assert fits.request.json["max_usd"] == "1.23"


def test_a_zero_estimate_with_nothing_remaining_is_refused():
    zero = {"usd": "0.00", "model": "gemma", "provider": "self_hosted", "priced": True}
    assert "remaining today is $0.00" in _refused(
        _split, _preview(estimate=zero, spend={"user_remaining_usd": "0.00", "global_remaining_usd": "48.00"}))


def test_self_hosted_estimate_of_zero_is_priced():
    p = _split(_preview(estimate={"usd": "0.00", "model": "gemma", "provider": "self_hosted", "priced": True}))
    assert p.request.json["max_usd"] == "0.00" and p.preview["cost"]["estimate_usd"] == "0.00"


def _precondition(preview, project=None):
    return precondition_for(*_spec("split_files_into_project").write.snapshot(
        _split_args(), [preview, project or _project()]))


@pytest.mark.parametrize("change", [
    {"files": [_file(F1, problem="consumed"), _file(F2, pages=12, size_bytes=400_000)]},
    {"files": [_file(F1, problem="expired"), _file(F2, pages=12, size_bytes=400_000)]},
    {"handoff_locked": True},
    {"splitter": {"available": False, "reason": "the job queue is off"}},
    {"estimate": {"usd": "1.24", "model": "claude-opus-5", "provider": "anthropic", "priced": True}},
    {"estimate": {"usd": "1.23", "model": "gpt-5.4", "provider": "anthropic", "priced": True}},
    {"estimate": {"usd": "1.23", "model": "claude-opus-5", "provider": "self_hosted", "priced": True}},
])
def test_the_precondition_changes(change):
    assert _precondition(_preview(**change)) != _precondition(_preview())


def test_the_precondition_ignores_spend_and_another_open_card():
    base = _precondition(_preview())
    assert _precondition(_preview(spend={"user_remaining_usd": "0.01", "global_remaining_usd": "0.01"})) == base
    other = _preview([_file(F1, problem="in_open_card"), _file(F2, pages=12, size_bytes=400_000)])
    assert _precondition(other) == base
    assert _precondition(_preview(), _project(abandoned_at="x")) != base


def _drop_out(**task_over) -> dict:
    from app.services import split_handoff

    task = {"id": TASK, "split_job_id": JOB, "project_id": PROJECT,
            "payload": {"file_ids": [F1, F2], "pages": 35, "estimate_usd": "1.23", "max_usd": "1.23",
                        "files": [{"assistant_file_id": F1, "split_file_id": TASK, "pages": 23},
                                  {"assistant_file_id": F2, "split_file_id": JOB, "pages": 12}]}}
    task.update(task_over)
    return split_handoff.split_drop_out(task)


def test_split_result_and_created_ref_from_every_body():
    spec = _spec("split_files_into_project")
    body = _drop_out()      # the 201, the replay and the by-key route all answer split_drop_out
    summary = spec.write.result_summary(body)
    assert summary == {"task_id": TASK, "job_id": JOB, "files": 2, "pages": 35, "estimate_usd": "1.23"}
    canonical_json(summary)
    created = spec.write.created_ref(_split_args(), body)
    assert created == {"kind": "split_task", "id": TASK, "label": "2 files into the project",
                       "href": f"/projects/{PROJECT}"}
    # The execute job's recovery passes the stored summary instead.
    assert spec.write.created_ref(_split_args(), summary) == created
    assert spec.write.created_ref(_split_args(), {}) is None
    assert spec.write.created_ref(_split_args(), {"task_id": "nope"}) is None


# ── attach_split_job_to_project ──────────────────────────────────────────────


def _attach_preview(**over) -> dict:
    """GET /projects/{id}/attach-split-job/preview as split_handoff.attach_preview serves it."""
    base = {
        "project": {"id": PROJECT, "number": NUMBER, "name": "ZZ TEST Split"},
        "handoff_locked": False,
        "job": {"id": JOB, "created_at": "2026-10-21T17:00:00+00:00", "status": "done_with_errors",
                "file_count": 3, "files_done": 2, "files_failed": 1, "source": "manual",
                "linked_project_id": None, "own": True, "source_bytes": 2048},
        "files": [{"filename": "Bid set.pdf", "status": "done", "pages": 120, "kind": "drawing"},
                  {"filename": "Specs.pdf", "status": "done", "pages": 300, "kind": "spec"},
                  {"filename": "Broken.pdf", "status": "failed", "pages": 0, "kind": None}],
        "problem": None,
    }
    base.update(over)
    return base


def _attach_args() -> dict:
    return {"project_id": PROJECT, "job_id": JOB}


def _attach(preview=None, project=None):
    args = _attach_args()
    reads = [preview or _attach_preview(), project or _project()]
    proposal = _spec("attach_split_job_to_project").write.build(args, reads, _ctx())
    _check(proposal, "attach_split_job_to_project", args, reads)
    return proposal


def test_attach_request_target_and_card():
    p = _attach()
    assert p.request.route == splits.ATTACH_POST and p.request.json == {"job_id": JOB}
    assert dict(p.request.headers) == {splits.ASSISTANT_IDEMPOTENCY_HEADER: APPROVAL}
    assert p.target == {"job_id": JOB, "status": "done_with_errors", "linked_project_id": None,
                        "files_done": 2, "files_failed": 1}
    card = p.preview
    assert card["title"] == f"File split job into {NUMBER}"
    rows = {r["field"]: r["after"] for r in card["rows"]}
    assert rows["files_done"] == "2" and rows["files_failed"] == "1" and rows["job_created"].endswith("PT")
    assert [r["label"] for r in card["rows"]] == ["Project", "Job created", "Files done", "Files failed (not filed)"]
    assert card["items"] == [{"label": "Bid set.pdf", "before": "drawing", "after": "120 pages"},
                             {"label": "Specs.pdf", "before": "spec", "after": "300 pages"}]
    assert [w["code"] for w in card["warnings"]] == ["failed_not_filed", "job_becomes_project"]
    assert card["totals"] == {"files": 2, "pages": 420}
    clean = _attach(_attach_preview(job={**_attach_preview()["job"], "files_failed": 0, "status": "done"},
                                    files=_attach_preview()["files"][:2]))
    assert [w["code"] for w in clean.preview["warnings"]] == ["job_becomes_project"]
    with pytest.raises(ToolArgumentError):
        _spec("attach_split_job_to_project").requests({"project_id": PROJECT, "job_id": "nope"})


@pytest.mark.parametrize("problem", sorted(splits.ATTACH_PROBLEM_TEXTS))
def test_attach_refuses_each_problem(problem):
    assert _refused(_attach, _attach_preview(problem=problem)) == splits.ATTACH_PROBLEM_TEXTS[problem]


def test_attach_problem_codes_cover_the_service():
    from app.services import split_handoff

    assert set(splits.ATTACH_PROBLEM_TEXTS) == set(split_handoff._ATTACH_MESSAGES) - {"handoff_locked"}


def test_attach_refuses_closed_locked_missing_and_already_attached():
    assert "abandoned" in _refused(_attach, project=_project(current_stage="declined"))
    assert "Revisions" in _refused(_attach, _attach_preview(handoff_locked=True))
    assert "not found" in _refused(_attach, _attach_preview(job=None))
    here = _attach_preview(job={**_attach_preview()["job"], "linked_project_id": PROJECT})
    assert _refused(_attach, here) == f"That splitter job is already filed into {NUMBER}."


def test_attach_precondition_changes_when_the_job_moves():
    spec = _spec("attach_split_job_to_project")

    def pre(preview):
        return precondition_for(*spec.write.snapshot(_attach_args(), [preview, _project()]))

    base = pre(_attach_preview())
    job = _attach_preview()["job"]
    assert pre(_attach_preview(job={**job, "linked_project_id": OTHER_PROJECT})) != base
    assert pre(_attach_preview(job={**job, "status": "processing"})) != base
    assert pre(_attach_preview(job={**job, "files_done": 3, "files_failed": 0})) != base


def test_attach_result_and_created_ref():
    spec = _spec("attach_split_job_to_project")
    outcome = {"job_id": JOB, "project_id": PROJECT, "job": {"id": JOB, "status": "done"}, "files": [],
               "ready": True, "split": 1, "whole": 1, "failed": 1, "missing": 0, "pending": 0, "documents": 7}
    summary = spec.write.result_summary(outcome)
    assert summary == {"job_id": JOB, "split": 1, "whole": 1, "documents": 7}
    created = spec.write.created_ref(_attach_args(), outcome)
    assert created["kind"] == "split_attach" and created["id"] == JOB and created["href"] == f"/projects/{PROJECT}"
    assert spec.write.created_ref(_attach_args(), summary) == created


# ── skill and evals ──────────────────────────────────────────────────────────


def test_skill_loads_under_its_folder_name_for_writers_with_bidding_on():
    skill = skills_index.load_all()["splitting-bid-files"]
    assert skill.path.endswith("/splitting-bid-files/SKILL.md")
    assert skill.roles == WRITER_ROLES and skill.requires_flags == ("bidding",) and skill.version == "1"
    assert EM_DASH not in Path(skill.path).read_text(encoding="utf-8")
    assert skills_index.get("splitting-bid-files", Role.ACCOUNTANT, ON) is None
    assert skills_index.get("splitting-bid-files", Role.EXECUTIVE, {**ON, "bidding": False}) is None
    for tool in NEW:
        assert tool in skill.body and tool in registry.all_tools()
    for phrase in ("Revisions on the To Estimator step", "10 files and 400 pages", "untrusted",
                   "Word and Excel", "Filed whole, not split"):
        assert phrase in skill.body, phrase


def test_run_evals_check_passes():
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-m", "app.assistant.evals.run_evals", "--check"], cwd=root,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    summary = json.loads(done.stdout)
    assert summary["file_cases"] >= 4


# ── end to end through the approvals engine ──────────────────────────────────


class FakeSplitBDR:
    """The routes behind the split tools, in memory (the preview reads and
    the two writes; the services are tested in tests/test_split_handoff.py)."""

    def __init__(self):
        self.project = _project()
        self.preview = _preview()
        self.attach_preview = _attach_preview()
        self.sent: list[dict] = []
        self.write_status = 201
        self.attach_status = 200
        self.committed = False

    def send(self, req, token):
        import copy

        self.sent.append({"route": req.route, "path": req.path, "params": dict(req.params or {}),
                          "json": copy.deepcopy(req.json), "headers": dict(req.headers or {})})
        if req.route == splits.PROJECT_GET:
            return 200, copy.deepcopy(self.project)
        if req.route == splits.SPLIT_DROP_PREVIEW:
            return 200, copy.deepcopy(self.preview)
        if req.route == splits.ATTACH_PREVIEW:
            return 200, copy.deepcopy(self.attach_preview)
        if req.route == splits.SPLIT_DROP_POST:
            for f in self.preview["files"]:
                f["problem"] = "consumed"
            self.committed = True
            return self.write_status, _drop_out()
        if req.route == splits.ATTACH_POST:
            self.attach_preview["job"]["linked_project_id"] = PROJECT
            self.committed = True
            return self.attach_status, _attach_out()
        # The recovery routes after a 5xx: the body when the write committed.
        if req.route == splits.SPLIT_DROP_BY_KEY and self.committed:
            return 200, _drop_out()
        if req.route == splits.ATTACH_GET and self.committed:
            return 200, _attach_out()
        return 404, {"detail": "Not Found"}


def _attach_out() -> dict:
    """job_outcome as the attach POST and its recovery GET answer it."""
    return {"job_id": JOB, "project_id": PROJECT, "job": {"id": JOB, "status": "done"}, "files": [],
            "ready": True, "split": 2, "whole": 0, "failed": 0, "missing": 0, "pending": 0, "documents": 9}


@pytest.fixture
def env(monkeypatch):
    from app.assistant import access, jobs, principal
    from app.assistant import executor as executor_mod
    from app.core import features, supabase_client
    from app.services import notifications

    from tests.assistant_fakes import (
        SECRET,
        FakeDB,
        delegation,
        pending_answer,
        profile,
        settings_row,
    )

    s = SimpleNamespace(assistant_token_secret=SECRET, assistant_enabled=True)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    monkeypatch.setattr(access, "get_settings", lambda: s)
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[delegation()], assistant_approvals=[], assistant_tool_calls=[],
        assistant_tool_results=[], assistant_messages=[pending_answer(status="waiting_approval")],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(features, "enabled_map", lambda role=None: dict(ON))
    monkeypatch.setattr(notifications, "notify_user", lambda *a, **kw: None)
    monkeypatch.setattr(jobs, "_renew_lease", lambda: True)
    bdr = FakeSplitBDR()
    monkeypatch.setattr(executor_mod.Executor, "_send_via_shim", lambda self, req, token: bdr.send(req, token))
    return SimpleNamespace(db=db, bdr=bdr)


def _e2e_propose(name, args):
    from app.assistant.executor import Executor

    from tests.assistant_fakes import CONV, MSG_ASSISTANT, RUN, USER

    specs = {s.name: s for s in registry.toolset_for(_user(Role.EXECUTIVE), ON)}
    ex = Executor(user_id=USER, user_label="Tom Moore", run_id=RUN, specs=specs, lease_owner="worker:turn:1",
                  conversation_id=CONV, message_id=MSG_ASSISTANT, role=Role.EXECUTIVE)
    res = approvals.propose(ex, name, args, user_texts=("split these",), conversation_id=CONV,
                            message_id=MSG_ASSISTANT)
    return res


def _e2e_run(card):
    from app.assistant import approvals_job, repo

    from tests.assistant_fakes import USER

    decided = repo.decide_approval(USER, card["id"], decision="approve", payload_sha256=card["payload_sha256"],
                                   precondition=card["precondition"], session_aal="aal2")
    assert decided["ok"], decided
    approvals_job.run_approval_execute({"approval_id": card["id"], "user_id": USER})
    return repo.get_approval(USER, card["id"])


def _posts(env):
    return [s for s in env.bdr.sent if s["route"][0] == "POST"]


def test_e2e_split_card_executes_with_the_key_and_the_ceiling(env):
    res = _e2e_propose("split_files_into_project", _split_args())
    assert res.ok, res.model_output
    card = res.approval
    assert card["preview"]["cost"]["estimate_usd"] == "1.23" and card["preview"]["totals"] == {"files": 2, "pages": 35}
    created = datetime.fromisoformat(card["expires_at"]) - datetime.now(timezone.utc)
    assert 3.9 * 3600 < created.total_seconds() <= 4 * 3600
    preview_read = next(s for s in env.bdr.sent if s["route"] == splits.SPLIT_DROP_PREVIEW)
    assert preview_read["params"] == {"file_ids": f"{F1},{F2}"}
    row = _e2e_run(card)
    assert row["status"] == "executed", row
    (post,) = _posts(env)
    assert post["json"] == {"file_ids": [F1, F2], "max_usd": "1.23"}
    assert post["headers"] == {splits.ASSISTANT_IDEMPOTENCY_HEADER: card["id"]}
    assert row["result"]["created"]["kind"] == "split_task" and row["result"]["created"]["id"] == TASK
    assert row["result"]["summary"]["task_id"] == TASK


def test_e2e_a_file_consumed_before_approval_supersedes_and_sends_nothing(env):
    card = _e2e_propose("split_files_into_project", _split_args()).approval
    env.bdr.preview["files"][0]["problem"] = "consumed"
    row = _e2e_run(card)
    assert row["status"] != "executed"
    assert not _posts(env)


def test_e2e_a_new_estimate_supersedes_with_a_fresh_card(env):
    from app.assistant import repo

    from tests.assistant_fakes import USER

    card = _e2e_propose("split_files_into_project", _split_args()).approval
    env.bdr.preview["estimate"] = {"usd": "1.50", "model": "claude-opus-5", "provider": "anthropic", "priced": True}
    row = _e2e_run(card)
    assert approvals.display_status(row) == "superseded"
    fresh = repo.get_approval(USER, row["superseded_by"])
    assert fresh["status"] == "pending" and fresh["preview"]["cost"]["estimate_usd"] == "1.50"
    assert fresh["payload"]["request"]["json"]["max_usd"] == "1.50"
    assert not _posts(env)


def test_e2e_attach_card_executes(env):
    res = _e2e_propose("attach_split_job_to_project", _attach_args())
    assert res.ok, res.model_output
    row = _e2e_run(res.approval)
    assert row["status"] == "executed", row
    (post,) = _posts(env)
    assert post["json"] == {"job_id": JOB}
    assert post["headers"] == {splits.ASSISTANT_IDEMPOTENCY_HEADER: res.approval["id"]}
    assert row["result"]["summary"] == {"job_id": JOB, "split": 2, "whole": 0, "documents": 9}
    assert row["result"]["created"]["kind"] == "split_attach"


@pytest.mark.parametrize("tool", ["split", "attach"])
def test_e2e_a_5xx_after_the_commit_is_recovered_as_executed(env, tool):
    """split_drop_out and job_outcome carry task_id / job_id, not a top-level
    id: the recovery lookup still reports the write found."""
    if tool == "split":
        env.bdr.write_status = 500
        res = _e2e_propose("split_files_into_project", _split_args())
        recover_route, kind, ref = splits.SPLIT_DROP_BY_KEY, "split_task", TASK
    else:
        env.bdr.attach_status = 502
        res = _e2e_propose("attach_split_job_to_project", _attach_args())
        recover_route, kind, ref = splits.ATTACH_GET, "split_attach", JOB
    assert res.ok, res.model_output
    row = _e2e_run(res.approval)
    assert any(s["route"] == recover_route for s in env.bdr.sent)
    assert row["status"] == "executed", row
    assert row["result"]["recovered"] is True
    assert row["result"]["created"]["kind"] == kind and row["result"]["created"]["id"] == ref


def test_e2e_a_5xx_with_nothing_committed_fails(env):
    env.bdr.write_status = 500
    res = _e2e_propose("split_files_into_project", _split_args())
    original = env.bdr.send

    def no_commit(req, token):
        status, body = original(req, token)
        if req.route == splits.SPLIT_DROP_POST:
            env.bdr.committed = False
        return status, body

    env.bdr.send = no_commit
    row = _e2e_run(res.approval)
    assert any(s["route"] == splits.SPLIT_DROP_BY_KEY for s in env.bdr.sent)
    assert row["status"] == "failed", row
