"""The Procore adapter: job seeds from PM emails and capture validation
(docs/browser_agents/SPEC.md 5.1 and 5.4, D20).

Seeds. Pass A (an email carrying RFI show links) seeds one job per link with
the RFI id; Pass C (a Procore RFI email with no link, only the subject)
seeds a locate job with `rfi_number_hint`. Hints come from the SUBJECT only,
by strict regex; the email body is never put in a seed.

Captures. POST /rfi-capture hands BDR an `rfi_detail_json` artifact the
worker's `capture_rfi_detail` uploaded (Procore's REST detail fetched in the
job's browser). `check_capture` refuses anything that does not prove it is
the seeded RFI of the seeded project, fresh, and still linked:

  - the artifact is this job's, kind rfi_detail_json, its bytes hash to the
    stored sha256 and parse as protocol.RfiDetailEnvelope with status 200;
  - request_path is /rest/v1.0/projects/<seed project>/rfis/<rfi id> and
    body.id is that rfi id; a seeded rfi id must match, and in locate mode
    the id must appear in one of this job's rfi_list_json artifacts;
  - captured_at is at most 30 min old and at most 2 min ahead (server clock);
  - the company agrees with the envelope, the seed and the link;
  - the live pm_external_links row is still procore, same host and project,
    sync on, for this job's BDR project;
  - a Procore draft is refused (the worker finishes nothing_to_do instead).

Attachment URLs are not checked here: the capture shim (rfi_session.py)
checks each one against the seed project and company at download time.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from app.services.browser_agents import protocol
from app.services.browser_agents.registry import PmAppAdapter

PLATFORM = "procore"
CAPTURE_MAX_AGE = timedelta(minutes=30)
CAPTURE_MAX_SKEW = timedelta(minutes=2)
MAX_JOBS_PER_EMAIL = 5

# Subject parsing (SPEC.md 5.1). Strict: a hint is letters, an optional
# dash and digits ("Const-198", "Int 07", "Const-189.1"); a bare "#20" (bid
# phase RFIs in planroom emails) is not a hint.
SUBJECT_HINT_RE = re.compile(r"#\s*([A-Za-z]+\s*-?\s*\d+(?:\.\d+)?)")
SUBJECT_CLOSED_RE = re.compile(r"\bRFI Closed\b", re.IGNORECASE)
SUBJECT_RESPONSE_RE = re.compile(r"\bResponse to RFI\b", re.IGNORECASE)
# Pass C qualifier: an RFI notification subject with a numbered hint.
PASS_C_SUBJECT_RE = re.compile(
    r"\bRFI\b(?: Closed| Response to RFI)?.*#\s*[A-Za-z]+\s*-?\s*\d+(?:\.\d+)?", re.IGNORECASE
)
_US02_SENDER_RE = re.compile(r"@([a-z0-9-]+\.)*us02\.procoretech\.com$", re.IGNORECASE)


class CaptureInvalid(ValueError):
    """A capture refused by `check_capture`. `reason` is a short fixed code
    (never page content); the job fails capture_invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── Seeds ──────────────────────────────────────────────────────────────────────


def subject_hints(subject: str | None) -> tuple[str | None, str]:
    """(rfi_number_hint | None, event_hint) from a subject line only."""
    text = str(subject or "")[:500]
    m = SUBJECT_HINT_RE.search(text)
    hint = re.sub(r"\s+", "", m.group(1))[:40] if m else None
    if SUBJECT_CLOSED_RE.search(text):
        event = "closed"
    elif SUBJECT_RESPONSE_RE.search(text):
        event = "response"
    else:
        event = "other"
    return hint, event


def sender_host(from_address: str | None) -> str:
    """The Procore web host a notification sender belongs to: the us02
    region's notifications come from @us02.procoretech.com; anything else
    is app.procore.com."""
    return "us02.procore.com" if _US02_SENDER_RE.search(str(from_address or "").strip()) else "app.procore.com"


def seed_input(email: dict, link: dict, *, rfi_id: str | None, company_id: str | None = None) -> dict:
    """The RfiFromEmailInput for one email and one RFI (rfi_id) or a locate
    (rfi_id None, hint from the subject). Raises ValueError when the email
    gives neither."""
    hint, event = subject_hints(email.get("subject"))
    message_at = email.get("message_at") or email.get("received_at") or email.get("updated_at")
    seed = protocol.RfiFromEmailInput(
        platform="procore",
        host=link.get("host") or "app.procore.com",
        external_project_id=str(link.get("external_project_id")),
        rfi_id=str(rfi_id) if rfi_id else None,
        company_id=str(company_id) if company_id and protocol.PROCORE_ID_RE.match(str(company_id)) else None,
        rfi_number_hint=hint,
        event_hint=event,
        email_message_at=message_at,
        source_email_ids=[email["id"]] if _is_uuid(email.get("id")) else [],
    )
    return seed.model_dump(mode="json")


def _is_uuid(value: Any) -> bool:
    try:
        import uuid

        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


# ── Capture validation ──────────────────────────────────────────────────────────


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_detail(raw: bytes) -> protocol.RfiDetailEnvelope:
    try:
        return protocol.RfiDetailEnvelope.model_validate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise CaptureInvalid("artifact_not_an_rfi_detail") from None


def list_ids(raws: list[bytes], project_id: str) -> set[str]:
    """RFI ids in this job's rfi_list_json artifacts for `project_id`."""
    ids: set[str] = set()
    for raw in raws:
        try:
            env = protocol.RfiListEnvelope.model_validate(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            continue
        if env.request_path != f"/rest/v1.0/projects/{project_id}/rfis" or env.status != 200:
            continue
        ids.update(str(r.get("id")) for r in env.body if isinstance(r, dict) and r.get("id") is not None)
    return ids


def check_capture(
    job: dict,
    request: protocol.RfiCaptureRequest,
    artifact: dict | None,
    raw: bytes | None,
    link: dict | None,
    list_raws: list[bytes],
    now: datetime,
) -> protocol.RfiDetailEnvelope:
    """Every rule in the module docstring; returns the parsed envelope."""
    if artifact is None or str(artifact.get("job_id")) != str(job["id"]):
        raise CaptureInvalid("artifact_not_this_job")
    if artifact.get("kind") != "rfi_detail_json":
        raise CaptureInvalid("artifact_wrong_kind")
    if raw is None or hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
        raise CaptureInvalid("artifact_hash_mismatch")
    env = parse_detail(raw)
    if env.status != 200:
        raise CaptureInvalid("detail_not_ok")
    try:
        seed = protocol.RfiFromEmailInput.model_validate(job.get("input") or {})
    except ValidationError:
        raise CaptureInvalid("seed_invalid") from None
    if request.platform != seed.platform:
        raise CaptureInvalid("platform_mismatch")
    rfi_id = request.external_rfi_id
    if env.request_path != f"/rest/v1.0/projects/{seed.external_project_id}/rfis/{rfi_id}":
        raise CaptureInvalid("path_mismatch")
    if str(env.body.get("id")) != rfi_id:
        raise CaptureInvalid("rfi_id_mismatch")
    if seed.rfi_id is not None:
        if seed.rfi_id != rfi_id:
            raise CaptureInvalid("not_the_seeded_rfi")
    elif rfi_id not in list_ids(list_raws, seed.external_project_id):
        raise CaptureInvalid("rfi_not_in_list")
    captured = _ts(env.captured_at)
    if captured is None or captured < now - CAPTURE_MAX_AGE or captured > now + CAPTURE_MAX_SKEW:
        raise CaptureInvalid("capture_stale")
    for known in (env.company_id, seed.company_id, (link or {}).get("external_company_id")):
        if known and str(known) != request.company_id:
            raise CaptureInvalid("company_mismatch")
    if not (
        link
        and link.get("platform") == "procore"
        and link.get("sync_enabled")
        and link.get("host") == seed.host
        and str(link.get("external_project_id") or "") == seed.external_project_id
        and str(link.get("project_id")) == str(job.get("project_id"))
    ):
        raise CaptureInvalid("link_changed")
    if bool(env.body.get("draft")) or str(env.body.get("status") or "").lower() == "draft":
        raise CaptureInvalid("draft")
    return env


def load_and_check(sb, job: dict, request: protocol.RfiCaptureRequest, now: datetime) -> protocol.RfiDetailEnvelope:
    """`check_capture` with its inputs read from the database and storage."""
    from app.services import pm_rfi_sync
    from app.services.browser_agents import artifacts

    rows = (
        sb.table("browser_agent_artifacts").select("*").eq("id", str(request.detail_artifact_id))
        .limit(1).execute()
    ).data or []
    artifact = rows[0] if rows else None
    raw = None
    if artifact is not None and str(artifact.get("job_id")) == str(job["id"]):
        try:
            raw = artifacts.download_object(artifact["storage_path"])
        except Exception:  # noqa: BLE001 - an unreadable blob is an invalid capture
            raw = None
    list_raws: list[bytes] = []
    try:
        seed = protocol.RfiFromEmailInput.model_validate(job.get("input") or {})
        locate = seed.rfi_id is None
    except ValidationError:
        locate = False
    if locate:
        for a in (
            sb.table("browser_agent_artifacts").select("id, storage_path, sha256").eq("job_id", str(job["id"]))
            .eq("kind", "rfi_list_json").execute()
        ).data or []:
            try:
                blob = artifacts.download_object(a["storage_path"])
            except Exception:  # noqa: BLE001 - a missing list only narrows what can be proved
                continue
            if hashlib.sha256(blob).hexdigest() == a.get("sha256"):
                list_raws.append(blob)
    link = pm_rfi_sync.get_link(sb, job["project_id"]) if job.get("project_id") else None
    return check_capture(job, request, artifact, raw, link, list_raws, now)


ADAPTER = PmAppAdapter(
    platform=PLATFORM,
    identity="procore",
    hosts=protocol.PROCORE_HOSTS,
    login_host=protocol.PROCORE_LOGIN_HOST,
    agent_types=("rfi_from_email",),
    seed=seed_input,
    validate_capture=check_capture,
)
