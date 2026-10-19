"""The capture shim: a Procore "session" that serves one agent capture to
pm_rfi_sync.process_run(agent=True) (docs/browser_agents/SPEC.md 5.4, D6-D8).

An agent run (pm_rfi_sync_runs.trigger 'agent') carries no Procore login of
its own. Its RFI detail is the `rfi_detail_json` artifact the worker
captured and /rfi-capture validated; `AgentCaptureSession` hands exactly that
body to the unchanged sync code (sync_project -> apply_detail ->
sync_attachments), so `map_detail` stays the single mapping and every BDR
write goes through the same collisions, matching and file rules as the
daily sweep.

  get_rfi(p, r, c)       the captured body, only for the captured project and
                         RFI id; anything else is ProcoreForbidden.
  resolve_company_id(p)  the capture's company.
  list_rfis(...)         never called (agent runs are target only).
  download(url, ...)     the cookie-less ProcoreSession.download (storage
                         host, one *.amazonaws.com redirect, the size cap),
                         after checking the signed link's projectId and
                         companyId against the seed. A storage 403/404 is
                         re-raised as ProcoreTransient: a captured link may
                         just have expired, so the file stays "retry" (the
                         row incomplete) and the next daily sweep fetches it,
                         instead of being recorded as refused.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import ValidationError

from app.core.config import Settings
from app.services import procore_client as pc
from app.services.browser_agents import protocol

logger = logging.getLogger(__name__)


class CaptureUnavailable(RuntimeError):
    """The run's capture cannot be loaded (missing or tampered artifact)."""


class AgentCaptureSession:
    def __init__(
        self,
        envelope: protocol.RfiDetailEnvelope,
        *,
        project_id: str,
        rfi_id: str,
        company_id: str | None,
        settings: Settings,
        download_session: Any | None = None,
    ) -> None:
        self.envelope = envelope
        self.project_id = str(project_id)
        self.rfi_id = str(rfi_id)
        self.company_id = str(company_id) if company_id else None
        self.captured_at: datetime = envelope.captured_at
        self._settings = settings
        self._download_session = download_session

    # -- the ProcoreSession surface sync_project uses --------------------------

    def get_rfi(self, project_id: str, rfi_id: str, company_id: str | None) -> dict:
        if str(project_id) != self.project_id or str(rfi_id) != self.rfi_id:
            raise pc.ProcoreForbidden("The agent capture holds a different RFI.")
        return copy.deepcopy(self.envelope.body)

    def resolve_company_id(self, project_id: str, **_: Any) -> str | None:
        return self.company_id if str(project_id) == self.project_id else None

    def list_rfis(self, *args: Any, **kwargs: Any) -> list[dict]:
        raise RuntimeError("An agent capture is target-only; it has no RFI list.")

    def _downloader(self):
        if self._download_session is None:
            self._download_session = pc.ProcoreSession(
                pc.ProcoreConfig(
                    email="", password="",
                    min_request_interval=self._settings.procore_min_request_interval_seconds,
                    timeout=self._settings.procore_request_timeout_seconds,
                ),
                pc.MemorySessionStore(),
            )
        return self._download_session

    def download(self, url: str, dest: Path, *, max_bytes: int) -> int:
        try:
            parsed = urlparse(str(url))
        except ValueError:
            raise pc.ProcoreForbidden("An RFI file link is not a URL.") from None
        if pc._query_id(parsed, "projectId", "project_id") != self.project_id:
            raise pc.ProcoreForbidden("An RFI file link names another Procore project.")
        company = pc._query_id(parsed, "companyId", "company_id")
        if not company or (self.company_id and company != self.company_id):
            raise pc.ProcoreForbidden("An RFI file link names another Procore company.")
        try:
            return self._downloader().download(url, dest, max_bytes=max_bytes)
        except pc.ProcoreStorageRefused as exc:
            raise pc.ProcoreTransient(
                "Procore storage refused a captured file link (it may have expired); "
                "the next sync fetches it again."
            ) from exc

    def close(self) -> None:
        if self._download_session is not None:
            try:
                self._download_session.close()
            except Exception:  # noqa: BLE001
                pass


def load_capture(sb, run: dict) -> tuple[protocol.RfiDetailEnvelope, dict]:
    """(envelope, job row) for an agent run. The blob must still hash to the
    sha256 recorded at upload (the bucket is private, but a stored object
    that changed under us is not applied)."""
    from app.services.browser_agents import artifacts

    artifact_id = run.get("detail_artifact_id")
    if not artifact_id or not run.get("agent_job_id"):
        raise CaptureUnavailable("The agent run has no capture.")
    rows = sb.table("browser_agent_artifacts").select("*").eq("id", str(artifact_id)).limit(1).execute().data or []
    if not rows or str(rows[0].get("job_id")) != str(run["agent_job_id"]):
        raise CaptureUnavailable("The agent run's capture is missing.")
    raw = artifacts.download_object(rows[0]["storage_path"])
    if hashlib.sha256(raw).hexdigest() != rows[0].get("sha256"):
        raise CaptureUnavailable("The agent run's capture does not match its hash.")
    try:
        envelope = protocol.RfiDetailEnvelope.model_validate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise CaptureUnavailable("The agent run's capture is not an RFI detail.") from None
    jobs = sb.table("browser_agent_jobs").select("*").eq("id", str(run["agent_job_id"])).limit(1).execute().data or []
    if not jobs:
        raise CaptureUnavailable("The agent run's job is missing.")
    return envelope, jobs[0]


def capture_company(envelope: protocol.RfiDetailEnvelope, job: dict, link: dict | None) -> str | None:
    """The company the capture was validated against: the envelope's, else
    the one /rfi-capture recorded on the job, else the seed's, else the link's."""
    recorded = ((job.get("result") or {}).get("capture") or {}).get("company_id")
    seed = (job.get("input") or {}).get("company_id")
    for value in (envelope.company_id, recorded, seed, (link or {}).get("external_company_id")):
        if value and protocol.PROCORE_ID_RE.match(str(value)):
            return str(value)
    return None


def factory_for(sb, run: dict) -> Callable[[str, Settings], AgentCaptureSession]:
    """The `session_factory` process_run calls for an agent run."""

    def factory(host: str, settings: Settings) -> AgentCaptureSession:
        from app.services import pm_rfi_sync

        envelope, job = load_capture(sb, run)
        link = pm_rfi_sync.get_link(sb, run["project_id"])
        project = str((job.get("input") or {}).get("external_project_id") or "")
        return AgentCaptureSession(
            envelope,
            project_id=project,
            rfi_id=str(run.get("target_external_rfi_id") or ""),
            company_id=capture_company(envelope, job, link),
            settings=settings,
        )

    return factory
