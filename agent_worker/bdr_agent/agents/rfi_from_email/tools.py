"""Site and finish tools for `rfi_from_email` (O1, spec 4.1, 5.3, 5.4).

The RFI data BDR writes comes ONLY from the `rfi_detail_json` artifact that
`capture_rfi_detail` stores: the Procore REST detail fetched inside the
job's own browser context, wrapped in protocol.RfiDetailEnvelope. The model
gets a short summary and never authors a field value; `finish(captured)` is
accepted only when this job holds a valid detail artifact, and then the
worker (not the model) submits /rfi-capture.

Scope: every site tool is pinned to the job seed (host and Procore project;
the seed's RFI id when it has one, otherwise an id from this job's RFI list).
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from ...browser.executor import ToolError, ToolReturn
from ...client import BdrRejected
from ...protocol import (
    ARTIFACT_RULES,
    RFI_LIST_MODEL_MAX_ROWS,
    TOOL_RESULT_MAX_CHARS,
    RfiCaptureRequest,
    RfiCheckpoint,
    RfiDetailEnvelope,
    RfiFromEmailInput,
    RfiListEnvelope,
    finish_to_job,
)
from ...slot import Finish, HandedOff
from ..pm_apps.procore.identity import ProcoreIdentity
from ..pm_apps.procore.paths import COMPANY_PROJECT_RE, WEBCLIENT_RE
from ..pm_apps.procore.rest import RestResult

log = logging.getLogger("bdr_agent.rfi_from_email")

CAPTURE_FRESH_MINUTES = 30            # spec 4.5: a resumed capture younger than this is submitted as is
MAX_COMPANIES_PROBED = 10
_PHASE_RANK = {"seeded": 0, "session_ready": 1, "located": 2, "detail_captured": 3, "submitted": 4}
_ROWS_BUDGET = TOOL_RESULT_MAX_CHARS - 2_000


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_number(value: Any) -> str:
    """Spec 5.3: lowercase, whitespace removed, '-' kept."""
    return re.sub(r"\s+", "", str(value or "")).lower()


def attachment_count(body: dict[str, Any]) -> int:
    n = len(body.get("attachments") or [])
    for q in body.get("questions") or []:
        if isinstance(q, dict):
            n += len(q.get("attachments") or [])
            for a in q.get("answers") or []:
                if isinstance(a, dict):
                    n += len(a.get("attachments") or [])
    return n


class RfiState:
    """The job seed and checkpoint, plus what this attempt learned."""

    def __init__(self, ctx: Any, seed: RfiFromEmailInput, ckpt: RfiCheckpoint):
        self.ctx = ctx
        self.seed = seed
        self.ckpt = ckpt
        self.listed_ids: set[str] = set()

    @classmethod
    def load(cls, ctx: Any) -> "RfiState":
        seed: RfiFromEmailInput = ctx.input
        try:
            ckpt = RfiCheckpoint.model_validate(ctx.checkpoint or {})
        except ValidationError:
            ctx.steps.log("note", "Checkpoint unreadable; starting from the seed")
            ckpt = RfiCheckpoint()
        if ckpt.rfi_id is None and seed.rfi_id:
            ckpt = ckpt.model_copy(update={"rfi_id": seed.rfi_id})
        if ckpt.company_id is None and seed.company_id:
            ckpt = ckpt.model_copy(update={"company_id": seed.company_id})
        return cls(ctx, seed, ckpt)

    @property
    def company_id(self) -> str | None:
        return self.ckpt.company_id or self.seed.company_id

    def rfi_id(self) -> str | None:
        """The RFI contributions belong to: the seed's, else the captured or located one."""
        return self.seed.rfi_id or self.ckpt.rfi_id

    async def save(self, **changes: Any) -> None:
        phase = changes.get("phase")
        if phase is not None and _PHASE_RANK[phase] < _PHASE_RANK[self.ckpt.phase]:
            changes.pop("phase")
        self.ckpt = self.ckpt.model_copy(update=changes)
        await self.ctx.save_checkpoint(self.ckpt.model_dump(mode="json"))

    async def save_last_url(self, data: dict[str, Any]) -> None:
        await self.save(last_url=data.get("last_url"))

    async def add_contribution(self, key: str) -> None:
        if key not in self.ckpt.contributions_sent:
            await self.save(contributions_sent=[*self.ckpt.contributions_sent, key])

    def fresh_capture(self) -> bool:
        c = self.ckpt
        if c.phase != "detail_captured" or not (c.detail_artifact_id and c.rfi_id and c.company_id and c.captured_at):
            return False
        return _now() - c.captured_at < timedelta(minutes=CAPTURE_FRESH_MINUTES)


class RfiTools:
    def __init__(self, ctx: Any, executor: Any, state: RfiState, identity: ProcoreIdentity):
        self.ctx = ctx
        self.ex = executor
        self.state = state
        self.identity = identity
        self.rest = identity.rest

    def handlers(self) -> dict[str, Any]:
        return {"ensure_login": self.ensure_login, "procore_resolve_company": self.resolve_company,
                "procore_list_rfis": self.list_rfis, "capture_rfi_detail": self.capture_rfi_detail,
                "finish": self.finish}

    # Scope ----------------------------------------------------------------------------

    def _scope(self, host: str, project_id: str) -> None:
        if host != self.state.seed.host or project_id != self.state.seed.external_project_id:
            raise ToolError("out_of_scope", "use the host and project id from the job seed")

    def _rest_error(self, res: RestResult, what: str) -> ToolError:
        if res.kind == "unauthorized":
            self.identity.mark_stale()
            return ToolError("unauthorized", "the Procore session is gone; call ensure_login, then retry")
        if res.kind == "challenge":
            return ToolError("challenge", f"Procore showed a verification page instead of the {what}; open the "
                                          "RFI page with navigate, wait_for a few seconds, then retry once")
        return ToolError(res.kind if res.kind in ("forbidden", "not_found", "not_json", "too_large") else "error",
                         f"Procore answered {res.status} for the {what}")

    # Tools -------------------------------------------------------------------------------

    async def ensure_login(self, args: Any) -> ToolReturn:
        return await self.identity.ensure(args.host)

    async def resolve_company(self, args: Any) -> ToolReturn:
        self._scope(args.host, args.project_id)
        if self.state.company_id:
            return ToolReturn({"company_id": self.state.company_id, "source": "seed"}, screenshot=False)
        pid = args.project_id
        found: str | None = None
        home = await self.rest.home_redirect(pid)
        if home.kind == "unauthorized":
            raise self._rest_error(home, "project home")
        if home.location:
            parts = urlsplit(home.location)
            if (parts.hostname or "").lower() == self.state.seed.host:
                m = WEBCLIENT_RE.match(parts.path or "") or COMPANY_PROJECT_RE.match(parts.path or "")
                if m and m.group(2) == pid:
                    found = m.group(1)
        elif home.ok and isinstance(home.data, str):
            m = re.search(rf"/webclients/host/companies/(\d{{1,20}})/projects/{re.escape(pid)}\b", home.data)
            if m:
                found = m.group(1)
        if found is None:
            companies = await self.rest.get_json("/rest/v1.0/companies")
            if not companies.ok:
                raise self._rest_error(companies, "company list")
            for company in (companies.data or [])[:MAX_COMPANIES_PROBED]:
                cid = str((company or {}).get("id") or "") if isinstance(company, dict) else ""
                if not cid.isdigit():
                    continue
                project = await self.rest.get_json(f"/rest/v1.0/projects/{pid}", company_id=cid,
                                                   params={"company_id": cid})
                if project.kind == "unauthorized":
                    raise self._rest_error(project, "project")
                if project.ok and isinstance(project.data, dict) and str(project.data.get("id")) == pid:
                    found = cid
                    break
        if found is None:
            raise ToolError("not_found", "no company on this account owns the project")
        await self.state.save(company_id=found)
        return ToolReturn({"company_id": found}, screenshot=False)

    def _referer(self, company_id: str, project_id: str) -> str:
        return f"https://{self.state.seed.host}/webclients/host/companies/{company_id}/projects/{project_id}/tools/rfis"

    async def list_rfis(self, args: Any) -> ToolReturn:
        self._scope(args.host, args.project_id)
        path = f"/rest/v1.0/projects/{args.project_id}/rfis"
        res = await self.rest.get_json(path, company_id=args.company_id,
                                       referer=self._referer(args.company_id, args.project_id))
        if not res.ok:
            raise self._rest_error(res, "RFI list")
        if not isinstance(res.data, list):
            raise ToolError("not_json", "the RFI list was not a list")
        rows = [r for r in res.data if isinstance(r, dict)]
        envelope = RfiListEnvelope(request_path=path, status=res.status, captured_at=_now(),
                                   company_id=args.company_id, pages=1, body=rows)
        blob = envelope.model_dump_json().encode("utf-8")
        if len(blob) > ARTIFACT_RULES["rfi_list_json"]["max_bytes"]:
            raise ToolError("too_large", "the RFI list is larger than 8 MB")
        art = await self.ctx.client.upload_artifact(self.ctx.lease, "rfi_list_json",
                                                    f"rfi-list-{args.project_id}.json", blob, "application/json",
                                                    retry=self.ctx.client.in_job_retry)
        self.state.listed_ids = {str(r.get("id")) for r in rows if str(r.get("id") or "").isdigit()}
        if args.company_id != self.state.company_id:
            await self.state.save(company_id=args.company_id)

        def view(r: dict[str, Any]) -> dict[str, Any]:
            return {"id": str(r.get("id")), "number": r.get("full_number") or r.get("number"),
                    "revision": r.get("revision"), "subject": r.get("subject"), "status": r.get("status"),
                    "current_revision": r.get("current_revision"), "updated_at": r.get("updated_at")}

        hint = self.state.seed.rfi_number_hint
        matches: list[dict[str, Any]] = []
        if hint:
            want = normalize_number(hint)
            matches = [r for r in rows if want in (normalize_number(r.get("full_number")), normalize_number(r.get("number")))]
            matches.sort(key=lambda r: (bool(r.get("current_revision")), r.get("revision") or 0), reverse=True)
            current = [r for r in matches if r.get("current_revision")]
            if len(current) == 1:
                await self.state.save(phase="located", rfi_id=str(current[0].get("id")))
        ordered = sorted(rows, key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        shown: list[dict[str, Any]] = []
        used = 0
        # The observation must fit TOOL_RESULT_MAX_CHARS: newest rows first,
        # leaving room for the hint matches.
        budget = _ROWS_BUDGET // 2 if matches else _ROWS_BUDGET
        for r in ordered[:RFI_LIST_MODEL_MAX_ROWS]:
            item = view(r)
            size = len(json.dumps(item, default=str)) + 1
            if used + size > budget:
                break
            shown.append(item)
            used += size
        content = {"count": len(rows), "rows_shown": len(shown), "truncated": len(shown) < len(rows),
                   "rows": shown}
        if hint:
            content["hint"] = hint
            content["hint_matches"] = [view(r) for r in matches[:20]]
        return ToolReturn(content, ledger={"count": len(rows), "artifact_id": str(art.artifact_id),
                                           "hint_matches": [str(r.get("id")) for r in matches[:20]]},
                          screenshot=False)

    async def capture_rfi_detail(self, args: Any) -> ToolReturn:
        self._scope(args.host, args.project_id)
        seed = self.state.seed
        if seed.rfi_id and args.rfi_id != seed.rfi_id:
            raise ToolError("out_of_scope", "capture the RFI id from the job seed")
        if not seed.rfi_id and args.rfi_id not in self.state.listed_ids and args.rfi_id != self.state.ckpt.rfi_id:
            raise ToolError("not_in_list", "call procore_list_rfis and pick an id from it first")
        path = f"/rest/v1.0/projects/{args.project_id}/rfis/{args.rfi_id}"
        res = await self.rest.get_json(path, company_id=args.company_id,
                                       referer=self._referer(args.company_id, args.project_id))
        if not res.ok:
            raise self._rest_error(res, "RFI detail")
        body = res.data
        if not isinstance(body, dict):
            raise ToolError("not_json", "the RFI detail was not an object")
        if str(body.get("id")) != args.rfi_id:
            raise ToolError("id_mismatch", "Procore answered with a different RFI")
        captured_at = _now()
        envelope = RfiDetailEnvelope(request_path=path, status=res.status, captured_at=captured_at,
                                     company_id=args.company_id, body=body)
        blob = envelope.model_dump_json().encode("utf-8")
        if len(blob) > ARTIFACT_RULES["rfi_detail_json"]["max_bytes"]:
            raise ToolError("too_large", "the RFI detail is larger than 8 MB")
        art = await self.ctx.client.upload_artifact(self.ctx.lease, "rfi_detail_json",
                                                    f"rfi-{args.rfi_id}.json", blob, "application/json",
                                                    retry=self.ctx.client.in_job_retry)
        await self.state.save(phase="detail_captured", rfi_id=args.rfi_id, company_id=args.company_id,
                              detail_artifact_id=art.artifact_id, captured_at=captured_at)
        summary = {"number": body.get("full_number") or body.get("number"), "subject": body.get("subject"),
                   "status": body.get("status"), "revision": body.get("revision"),
                   "attachment_count": attachment_count(body), "draft": bool(body.get("draft"))}
        return ToolReturn(summary, ledger={**summary, "artifact_id": str(art.artifact_id)}, screenshot=False)

    async def submit_capture(self) -> HandedOff | Finish:
        c = self.state.ckpt
        req = RfiCaptureRequest(platform="procore", detail_artifact_id=c.detail_artifact_id,
                                external_rfi_id=c.rfi_id, company_id=c.company_id)
        try:
            resp = await self.ctx.client.rfi_capture(self.ctx.lease, req)
        except BdrRejected as exc:
            self.ctx.steps.log("error.capture", f"BDR refused the capture ({exc.code})", ok=False)
            return Finish("failed", "BDR refused the capture", error_kind="other", error=exc.code or "capture_invalid")
        # No checkpoint after this: the job is `applying` on BDR and the lease
        # is gone; BDR never hands an applying job out again.
        return HandedOff(f"Capture of RFI {c.rfi_id} submitted (run {resp.run_id})")

    async def finish(self, args: Any) -> ToolReturn:
        action = finish_to_job("rfi_from_email", args)
        if action["action"] == "rfi_capture":
            # BDR refuses a capture older than 30 min; so does the worker.
            if not self.state.fresh_capture():
                raise ToolError("no_capture", "call capture_rfi_detail first; captured needs a fresh capture")
            terminal = await self.submit_capture()
            return ToolReturn({"submitted": isinstance(terminal, HandedOff)}, screenshot=False, terminal=terminal)
        if action["status"] == "succeeded":
            return ToolReturn({"finished": True}, screenshot=False,
                              terminal=Finish("succeeded", args.summary or "Nothing to import",
                                              outcome=action["outcome"]))
        return ToolReturn({"finished": True}, screenshot=False, terminal=Finish(
            "failed", args.summary or "Could not complete", error_kind=action["error_kind"],
            error=(args.summary or None)))
