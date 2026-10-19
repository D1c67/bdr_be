"""The rfi_from_email agent, BDR side (docs/browser_agents/SPEC.md 5.2, 5.4, 5.6, 10.1).

Two fakes: the browser agent FakeDB (tests/browser_agents_fake.py) behind the
real worker routes for /rfi-capture and the claim-time prepare, and the PM
RFI sync FakeDB (tests/test_pm_rfi_sync.py, the 0156 unique indexes) for
applying captures with the real fixtures (detail_27914290: Const-198, closed,
two files; detail_27699412: Const-189.1, open).

Pinned: capture validation (wrong id, project, host, company, stale or
future captured_at, a foreign or tampered artifact, a draft, a locate id not
in the job's own list); a resend returns the same run; a refused capture
fails the job; prepare's no_link (quiet) and already_current;
process_run(agent=True): no list call, provisional rows (external_updated_at
null), import_method on insert only, files only through the cookie-less shim
with the projectId/companyId check, a storage 403 left as retry (not
refused), the stale guard, the link status never touched, the summary only
after the backfill; the next web-session sweep verifies and completes the
row; agent runs wait behind a running run and are never joined; helper
contributions apply once and later when the RFI appears; retry after
apply_failed makes apply_attempt + 1.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import pm_rfi_sync as svc
from app.services import procore_client as pc
from app.services.browser_agents import artifacts, contributions, protocol, queue, registry, rfi_session
from app.services.browser_agents.pm_apps import procore as procore_app
from app.services.browser_agents.types import rfi_from_email
from tests.browser_agents_fake import iso
from tests.browser_agents_harness import API, Harness, error_code
from tests.test_pm_rfi_sync import (
    COMPANY,
    DETAILS,
    PROCORE_PROJECT,
    PROJECT,
    FakeSession,
    _Bells,
    _db,
    _link,
    _settings,
)

RFI_198 = "27914290"
RFI_189 = "27699412"


@pytest.fixture(autouse=True)
def _storage(monkeypatch):
    from app.services import storage

    uploads = []
    monkeypatch.setattr(storage, "upload_file", lambda path, content, ctype, **k: uploads.append((path, content)))
    monkeypatch.setattr(storage, "build_object_path", lambda pid, cat, name: f"{pid}/{cat}/x-{name}")
    return uploads


def _envelope(*, project=PROCORE_PROJECT, rfi=RFI_198, captured: datetime | None = None, company=COMPANY,
              body=None, status=200) -> dict:
    detail = copy.deepcopy(body if body is not None else DETAILS.get(rfi) or DETAILS[RFI_198])
    detail["id"] = int(rfi)
    return {"request_path": f"/rest/v1.0/projects/{project}/rfis/{rfi}", "status": status,
            "captured_at": (captured or datetime.now(timezone.utc)).isoformat(), "company_id": company,
            "body": detail}


def _raw(env: dict) -> bytes:
    return json.dumps(env).encode()


# ── 1. Capture validation (pure) ───────────────────────────────────────────────

NOW = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)


def _case(**over):
    seed = {"platform": "procore", "host": "app.procore.com", "external_project_id": PROCORE_PROJECT,
            "rfi_id": RFI_198, "company_id": None, "email_message_at": iso(NOW - timedelta(hours=1))}
    seed.update(over.pop("seed", {}))
    job = {"id": "j1", "project_id": PROJECT, "input": seed}
    env = over.pop("env", None) or _envelope(captured=NOW - timedelta(minutes=5))
    raw = over.pop("raw", None) or _raw(env)
    artifact = {"id": "a1", "job_id": "j1", "kind": "rfi_detail_json", "sha256": hashlib.sha256(raw).hexdigest()}
    artifact.update(over.pop("artifact", {}))
    link = _link(**over.pop("link", {}))
    request = protocol.RfiCaptureRequest(platform="procore", detail_artifact_id=uuid.uuid4(),
                                         external_rfi_id=over.pop("rfi", RFI_198),
                                         company_id=over.pop("company", COMPANY))
    lists = over.pop("lists", [])
    assert not over
    return job, request, artifact, raw, link, lists


def test_a_good_capture_passes():
    env = procore_app.check_capture(*_case(), NOW)
    assert env.body["id"] == int(RFI_198)


@pytest.mark.parametrize("over,reason", [
    ({"artifact": {"job_id": "other"}}, "artifact_not_this_job"),
    ({"artifact": {"kind": "rfi_list_json"}}, "artifact_wrong_kind"),
    ({"artifact": {"sha256": "0" * 64}}, "artifact_hash_mismatch"),
    ({"raw": b"not json"}, "artifact_hash_mismatch"),
    ({"env": _envelope(status=403, captured=NOW)}, "detail_not_ok"),
    ({"env": _envelope(project="999", captured=NOW)}, "path_mismatch"),
    ({"rfi": "1", "env": _envelope(rfi="1", captured=NOW)}, "not_the_seeded_rfi"),
    ({"env": {**_envelope(captured=NOW), "body": {**DETAILS[RFI_198], "id": 5}}}, "rfi_id_mismatch"),
    ({"env": _envelope(captured=NOW - timedelta(minutes=31))}, "capture_stale"),
    ({"env": _envelope(captured=NOW + timedelta(minutes=3))}, "capture_stale"),
    ({"company": "999"}, "company_mismatch"),
    ({"env": _envelope(captured=NOW, company="1")}, "company_mismatch"),
    ({"link": {"host": "us02.procore.com"}}, "link_changed"),
    ({"link": {"external_project_id": "1"}}, "link_changed"),
    ({"link": {"sync_enabled": False}}, "link_changed"),
    ({"link": {"project_id": "p2"}}, "link_changed"),
    ({"env": _envelope(captured=NOW, body={**DETAILS[RFI_198], "draft": True})}, "draft"),
])
def test_bad_captures_are_refused(over, reason):
    case = _case(**over)
    if "raw" in over:
        case = (*case[:2], {**case[2], "sha256": hashlib.sha256(over["raw"]).hexdigest()}, *case[3:])
        reason = "artifact_not_an_rfi_detail"
    with pytest.raises(procore_app.CaptureInvalid) as exc:
        procore_app.check_capture(*case, NOW)
    assert exc.value.reason == reason


def test_a_locate_capture_needs_the_id_in_the_jobs_own_list():
    listing = {"request_path": f"/rest/v1.0/projects/{PROCORE_PROJECT}/rfis", "status": 200,
               "captured_at": NOW.isoformat(), "company_id": COMPANY, "pages": 1,
               "body": [{"id": int(RFI_198), "number": "198"}]}
    seed = {"rfi_id": None, "rfi_number_hint": "Const-198"}
    with pytest.raises(procore_app.CaptureInvalid) as exc:
        procore_app.check_capture(*_case(seed=seed), NOW)
    assert exc.value.reason == "rfi_not_in_list"
    other_project = {**listing, "request_path": "/rest/v1.0/projects/1/rfis"}
    with pytest.raises(procore_app.CaptureInvalid):
        procore_app.check_capture(*_case(seed=seed, lists=[_raw(other_project)]), NOW)
    assert procore_app.check_capture(*_case(seed=seed, lists=[_raw(listing)]), NOW).body["id"] == int(RFI_198)


def test_subject_hints_and_sender_regions():
    assert procore_app.subject_hints(
        "1426, CCRPM- Lake Mead: RFI Closed, #Const-198, EF3 Circuit Clarification") == ("Const-198", "closed")
    assert procore_app.subject_hints(
        "1426, CCRPM: Response to RFI #Const-189.1, Floor closers") == ("Const-189.1", "response")
    assert procore_app.subject_hints("RFI #20 - Mechanical Controls") == (None, "other")
    assert procore_app.sender_host("gc_notifications@us02.procoretech.com") == "us02.procore.com"
    assert procore_app.sender_host("Cobblestone_notifications@procoretech.com") == "app.procore.com"


# ── 2. POST /rfi-capture through the worker API ─────────────────────────────────


class Capture:
    def __init__(self, monkeypatch):
        self.h = Harness(monkeypatch)
        self.worker, self.token, self.boot, self.job = self.h.active_with_job("rfi_from_email")
        self.h.link_project(external_project_id="123", external_company_id="77")

    def headers(self):
        return self.h.lease_headers(self.token, self.job["id"], int(self.job["lease_epoch"]), self.boot)

    def upload(self, env: dict, kind="rfi_detail_json") -> str:
        raw = _raw(env)
        resp = self.h.client.post(
            f"{API}/jobs/{self.job['id']}/artifacts", headers=self.headers(),
            data={"kind": kind, "sha256": hashlib.sha256(raw).hexdigest(), "filename": f"{kind}.json"},
            files={"file": (f"{kind}.json", raw, "application/json")},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["artifact_id"]

    def capture(self, artifact_id: str, rfi="456", company="77"):
        return self.h.client.post(
            f"{API}/jobs/{self.job['id']}/rfi-capture", headers=self.headers(),
            json={"platform": "procore", "detail_artifact_id": artifact_id, "external_rfi_id": rfi,
                  "company_id": company},
        )


def test_a_valid_capture_queues_one_agent_run_and_a_resend_returns_it(monkeypatch):
    c = Capture(monkeypatch)
    art = c.upload(_envelope(project="123", rfi="456", company="77", captured=c.h.db.now))
    resp = c.capture(art)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    run_id = resp.json()["run_id"]
    [run] = c.h.db.rows("pm_rfi_sync_runs")
    assert (run["id"], run["trigger"], run["agent_job_id"], run["apply_attempt"], run["detail_artifact_id"],
            run["target_external_rfi_id"]) == (run_id, "agent", c.job["id"], 0, art, "456")
    job = c.h.db.one("browser_agent_jobs", id=c.job["id"])
    assert (job["status"], job["outcome"], job["sync_run_id"], job["lease_owner"]) == (
        "applying", "pending", run_id, None)
    assert job["result"]["capture"]["company_id"] == "77"
    again = c.capture(art)
    assert again.status_code == 200 and again.json()["run_id"] == run_id
    assert len(c.h.db.rows("pm_rfi_sync_runs")) == 1


def test_a_refused_capture_fails_the_job(monkeypatch):
    c = Capture(monkeypatch)
    art = c.upload(_envelope(project="123", rfi="999", company="77", captured=c.h.db.now))
    resp = c.capture(art, rfi="999")
    assert resp.status_code == 422 and error_code(resp) == "capture_invalid"
    job = c.h.db.one("browser_agent_jobs", id=c.job["id"])
    assert (job["status"], job["error_kind"]) == ("failed", "capture_invalid")
    assert "not_the_seeded_rfi" in job["error"]
    assert [n["type"] for n in c.h.notices if n["type"] == "browser_agent.job_failed"] == ["browser_agent.job_failed"]
    assert c.h.db.rows("pm_rfi_sync_runs") == []


def test_a_capture_from_a_stale_lease_does_not_fail_the_job(monkeypatch):
    c = Capture(monkeypatch)
    art = c.upload(_envelope(project="123", rfi="999", company="77", captured=c.h.db.now))
    stale = c.h.lease_headers(c.token, c.job["id"], int(c.job["lease_epoch"]) + 3, c.boot)
    resp = c.h.client.post(f"{API}/jobs/{c.job['id']}/rfi-capture", headers=stale, json={
        "platform": "procore", "detail_artifact_id": art, "external_rfi_id": "999", "company_id": "77"})
    assert resp.status_code == 409 and error_code(resp) == "lease_lost"
    assert c.h.db.one("browser_agent_jobs", id=c.job["id"])["status"] == "running"


def test_a_canary_cannot_capture(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job("canary")
    resp = h.client.post(f"{API}/jobs/{job['id']}/rfi-capture",
                         headers=h.lease_headers(token, job["id"], int(job["lease_epoch"]), boot),
                         json={"platform": "procore", "detail_artifact_id": str(uuid.uuid4()),
                               "external_rfi_id": "1", "company_id": "1"})
    assert resp.status_code == 422 and error_code(resp) == "invalid_request"


# ── 3. prepare() at claim ────────────────────────────────────────────────────


def _prep(h, **cols):
    job = h.job("rfi_from_email", **cols)
    return registry.get_type("rfi_from_email").prepare(job)


def test_prepare_runs_a_linked_job_and_refuses_removed_or_moved_links(monkeypatch):
    h = Harness(monkeypatch)
    assert _prep(h).action == "run"
    h.link_project(sync_enabled=False)
    res = _prep(h)
    assert (res.error_kind, res.notify) == ("no_link", False)
    h.link_project(sync_enabled=True, external_project_id="999")
    assert _prep(h).error_kind == "no_link"
    h.link_project(external_project_id="123", host="us02.procore.com")
    assert _prep(h).error_kind == "no_link"
    h.db.tables["pm_external_links"] = []
    assert _prep(h, project_id=h.PROJECT_ID).error_kind == "no_link"


def test_prepare_short_circuits_already_current(monkeypatch):
    h = Harness(monkeypatch)
    pid = h.link_project()["project_id"]
    rfis = h.db.tables.setdefault("rfis", [])
    rfis.append({"id": "r1", "project_id": pid, "external_id": "456", "status": "closed",
                 "external_updated_at": iso(h.db.now), "external_synced_at": iso(h.db.now - timedelta(days=9))})
    res = _prep(h)
    assert (res.status, res.outcome) == ("succeeded", "already_current")
    rfis[0].update(status="open", external_synced_at=iso(h.db.now + timedelta(minutes=1)))
    assert _prep(h).outcome == "already_current"                 # synced after the email arrived
    rfis[0].update(external_synced_at=iso(h.db.now - timedelta(hours=2)))
    assert _prep(h).action == "run"
    locate = {"platform": "procore", "host": "app.procore.com", "external_project_id": "123",
              "rfi_number_hint": "Const-198", "email_message_at": iso(h.db.now)}
    assert _prep(h, input=locate).action == "run"


def test_a_no_link_job_is_closed_at_claim_without_a_bell(monkeypatch):
    h = Harness(monkeypatch)
    h.enable_type("rfi_from_email")
    job = h.job("rfi_from_email")
    h.db.tables["pm_external_links"] = []
    _, token = h.worker()
    _, boot = h.hello(token)
    assert h.claim(token, boot, {"rfi_from_email": 1}).json() == {"jobs": []}
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["error_kind"]) == ("failed", "no_link")
    assert [n for n in h.notices if n["type"] == "browser_agent.job_failed"] == []


# ── 4. process_run(agent=True) ───────────────────────────────────────────────


class Downloads:
    """The cookie-less download session behind the shim."""

    def __init__(self, refuse_storage: set[str] | None = None):
        self.urls: list[str] = []
        self.refuse_storage = refuse_storage or set()

    def download(self, url, dest, *, max_bytes):
        self.urls.append(url)
        if any(key in url for key in self.refuse_storage):
            raise pc.ProcoreStorageRefused("Procore storage refused the document link.")
        dest.write_bytes(b"%PDF-1.7 agent")
        return 14

    def close(self):
        pass


def _shim(rfi=RFI_198, captured=None, downloads=None, company=COMPANY, body=None):
    env = protocol.RfiDetailEnvelope.model_validate(
        _envelope(rfi=rfi, captured=captured or datetime.now(timezone.utc), body=body))
    return rfi_session.AgentCaptureSession(env, project_id=PROCORE_PROJECT, rfi_id=rfi, company_id=company,
                                           settings=_settings(), download_session=downloads or Downloads())


def _agent_run(db, rfi=RFI_198, job_id="job-1", **over):
    run = {"id": f"run-{uuid.uuid4().hex[:6]}", "project_id": PROJECT, "trigger": "agent", "status": "running",
           "lease_owner": svc.WORKER_ID,
           "lease_until": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
           "attempts": 1, "agent_job_id": job_id, "apply_attempt": 0, "detail_artifact_id": "art-1",
           "target_external_rfi_id": rfi, "source_email_id": None, "created_at": "2026-10-19T10:00:00+00:00",
           **over}
    db.tables.setdefault("pm_rfi_sync_runs", []).append(run)
    return run


def _run(db, session, bells=None, **over):
    run = _agent_run(db, rfi=session.rfi_id, **over)
    status = svc.process_run(db, run, _settings(), session_factory=lambda h, s: session,
                             notify=bells or _Bells(), agent=True)
    return status, db.rows("pm_rfi_sync_runs", id=run["id"])[0]


def test_an_agent_run_writes_one_provisional_row_through_the_shim(_storage):
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00", last_sync_status="ok")])
    downloads = Downloads()
    status, run = _run(db, _shim(downloads=downloads))
    assert status == "done" and run["status"] == "done"
    assert run["stats"]["agent_outcome"] == "applied" and run["stats"]["backfill"] is False
    [row] = db.rows("rfis", project_id=PROJECT)
    assert (row["source"], row["external_id"], row["import_method"], row["imported_by_agent_job_id"]) == (
        "procore", RFI_198, "agent", "job-1")
    assert row["external_updated_at"] is None and row["external_synced_at"]      # provisional
    assert len(downloads.urls) == 2 and all("projectId=3167322" in u for u in downloads.urls)
    assert len(db.rows("rfi_attachments")) == 2
    link = svc.get_link(db, PROJECT)
    assert link["last_sync_status"] == "ok" and "last_synced_at" not in link      # never touched


def test_list_rfis_is_never_called_and_other_rfis_are_refused():
    shim = _shim()
    with pytest.raises(RuntimeError):
        shim.list_rfis(PROCORE_PROJECT, COMPANY)
    with pytest.raises(pc.ProcoreForbidden):
        shim.get_rfi(PROCORE_PROJECT, RFI_189, COMPANY)
    with pytest.raises(pc.ProcoreForbidden):
        shim.get_rfi("1", RFI_198, COMPANY)
    assert shim.resolve_company_id(PROCORE_PROJECT) == COMPANY and shim.resolve_company_id("1") is None


def test_a_storage_403_leaves_the_file_to_retry_not_refused(tmp_path):
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    status, run = _run(db, _shim(downloads=Downloads(refuse_storage={"01M4C5FMTMT4Z0S1WBYQ0TZRXM"})))
    assert status == "done"
    reasons = sorted(f["reason"] for f in run["stats"]["skipped_files"])
    assert reasons == ["retry"] and run["stats"]["attachments_failed"] == 1
    assert len(db.rows("rfi_attachments")) == 1
    assert db.rows("rfis", project_id=PROJECT)[0]["external_updated_at"] is None


def test_a_file_link_for_another_project_or_company_is_refused(tmp_path):
    shim = _shim()
    for url in ("https://storage.procore.com/f?companyId=6333&projectId=1&sig=x",
                "https://storage.procore.com/f?companyId=1&projectId=3167322&sig=x",
                "https://storage.procore.com/f?projectId=3167322&sig=x"):
        with pytest.raises(pc.ProcoreForbidden):
            shim.download(url, tmp_path / "f.bin", max_bytes=10)
    assert shim._download_session.urls == []
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    body = copy.deepcopy(DETAILS[RFI_198])
    body["questions"][0]["attachments"][0]["url"] = body["questions"][0]["attachments"][0]["url"].replace(
        "projectId=3167322", "projectId=42")
    status, run = _run(db, _shim(body=body))
    assert status == "done" and [f["reason"] for f in run["stats"]["skipped_files"]] == ["refused"]


def test_the_stale_guard_skips_a_capture_older_than_local_data():
    synced = datetime.now(timezone.utc)
    local = {"id": "r1", "project_id": PROJECT, "source": "procore", "external_id": RFI_198,
             "external_number": "Const-198", "external_revision": "0", "status": "open", "subject": "Old",
             "external_synced_at": synced.isoformat(), "external_updated_at": None}
    db = _db(rfis=[local], pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    status, run = _run(db, _shim(captured=synced - timedelta(minutes=1)))
    assert status == "done" and run["stats"]["agent_outcome"] == "skipped_stale"
    assert db.rows("rfis", id="r1")[0]["subject"] == "Old"
    # Procore's own updated_at already at or past the captured one: stale too.
    local.update(external_synced_at=None, external_updated_at="2026-10-16T00:00:00+00:00")
    db = _db(rfis=[local], pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    assert _run(db, _shim())[1]["stats"]["agent_outcome"] == "skipped_stale"


def test_a_finished_row_or_a_draft_is_not_applied():
    done = {"id": "r1", "project_id": PROJECT, "source": "procore", "external_id": RFI_198, "status": "closed",
            "external_updated_at": "2026-10-15T21:25:15Z"}
    db = _db(rfis=[done], pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    assert _run(db, _shim())[1]["stats"]["agent_outcome"] == "already_current"
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    assert _run(db, _shim(body={**DETAILS[RFI_198], "draft": True}))[1]["stats"]["agent_outcome"] == "nothing_to_do"
    assert db.rows("rfis") == []


def test_before_the_backfill_an_agent_run_is_target_only_and_quiet():
    db = _db()                                                        # backfilled_at None
    bells = _Bells()
    status, run = _run(db, _shim(rfi=RFI_189), bells=bells)
    assert status == "done" and run["stats"]["backfill"] is False
    assert len(db.rows("rfis")) == 1 and bells.calls == []
    assert svc.get_link(db, PROJECT)["backfilled_at"] is None
    db2 = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    bells2 = _Bells()
    _run(db2, _shim(rfi=RFI_189), bells=bells2)
    assert [c[2] for c in bells2.calls] == [svc.NOTIFY_SUMMARY, svc.NOTIFY_SUMMARY]


def test_the_web_session_sweep_verifies_and_completes_an_agent_row():
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    forged = {**DETAILS[RFI_189], "status": "closed"}                 # a capture claiming closed
    _run(db, _shim(rfi=RFI_189, body=forged))
    row = db.rows("rfis", external_id=RFI_189)[0]
    assert row["status"] == "closed" and row["import_method"] == "agent"
    # A later provisional capture never rewrites who imported the row.
    _run(db, _shim(rfi=RFI_189, captured=datetime.now(timezone.utc) + timedelta(seconds=30)), job_id="job-2")
    assert db.rows("rfis", external_id=RFI_189)[0]["imported_by_agent_job_id"] == "job-1"
    # The daily sweep (web session) reads Procore itself: open, complete, verified.
    stats = svc.sync_project(db, svc.get_link(db, PROJECT), FakeSession(), _settings())
    row = db.rows("rfis", external_id=RFI_189)[0]
    assert stats["updated"] >= 1
    assert (row["status"], row["import_method"]) == (svc.map_status(DETAILS[RFI_189]), "web_session")
    assert row["status"] != "closed"                                  # Procore's own status, not the capture's
    assert row["external_updated_at"] and row["imported_by_agent_job_id"] == "job-1"


def test_failed_agent_runs_never_touch_the_link():
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00", last_sync_status="ok")])

    class Broken(rfi_session.AgentCaptureSession):
        def get_rfi(self, *a):
            raise RuntimeError("boom")

    env = protocol.RfiDetailEnvelope.model_validate(_envelope())
    broken = Broken(env, project_id=PROCORE_PROJECT, rfi_id=RFI_198, company_id=COMPANY, settings=_settings())
    status, run = _run(db, broken)
    assert status == "failed" and run["status"] == "failed"
    assert svc.get_link(db, PROJECT)["last_sync_status"] == "ok"
    run2 = _agent_run(db)
    out = svc.process_run(db, run2, _settings(), agent=True,
                          session_factory=lambda h, s: (_ for _ in ()).throw(rfi_session.CaptureUnavailable("gone")))
    assert out == "failed" and "gone" in db.rows("pm_rfi_sync_runs", id=run2["id"])[0]["error"]
    assert svc.get_link(db, PROJECT)["last_sync_status"] == "ok"


def test_an_abandoned_agent_run_leaves_the_link_alone():
    db = _db(pm_external_links=[_link(last_sync_status="ok")])
    s = _settings(pm_rfi_sync_max_attempts=2)
    _agent_run(db, lease_until=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
               lease_owner="w1", attempts=2)
    assert svc.claim_next(db, s, datetime.now(timezone.utc)) is None
    assert db.rows("pm_rfi_sync_runs")[0]["status"] == "failed"
    assert svc.get_link(db, PROJECT)["last_sync_status"] == "ok"


def test_an_agent_run_waits_behind_a_running_run_and_is_never_joined():
    db = _db()
    now = datetime.now(timezone.utc)
    db.tables["pm_rfi_sync_runs"] = [{
        "id": "daily", "project_id": PROJECT, "trigger": "daily", "status": "running", "lease_owner": "w1",
        "lease_until": (now + timedelta(minutes=5)).isoformat(), "attempts": 1,
        "created_at": "2026-10-19T06:30:00+00:00"}]
    _agent_run(db, status="queued", lease_owner=None, lease_until=None, attempts=0)
    assert svc.claim_next(db, _settings(), now) is None
    # Sync now and the email scan never join an agent run.
    db.tables["pm_rfi_sync_runs"] = [r for r in db.tables["pm_rfi_sync_runs"] if r["trigger"] == "agent"]
    manual, created = svc.enqueue_run(db, PROJECT, "manual")
    assert created and manual["trigger"] == "manual"
    email, created = svc.enqueue_run(db, PROJECT, "email", target_external_rfi_id=RFI_198, source_email_id="e1")
    assert not created and email["id"] == manual["id"]               # joins the full run, not the agent's
    with pytest.raises(ValueError):
        svc.enqueue_run(db, PROJECT, "agent")


def test_run_pending_hands_agent_runs_the_capture_shim(monkeypatch):
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    _agent_run(db, status="queued", lease_owner=None, lease_until=None, attempts=0)
    env = _envelope(captured=datetime.now(timezone.utc))
    raw = _raw(env)
    db.tables["browser_agent_artifacts"] = [{"id": "art-1", "job_id": "job-1", "kind": "rfi_detail_json",
                                             "storage_path": "jobs/job-1/x.json",
                                             "sha256": hashlib.sha256(raw).hexdigest()}]
    db.tables["browser_agent_jobs"] = [{"id": "job-1", "project_id": PROJECT,
                                        "input": {"external_project_id": PROCORE_PROJECT},
                                        "result": {"capture": {"company_id": COMPANY}}}]
    monkeypatch.setattr(artifacts, "download_object", lambda path: raw)
    monkeypatch.setattr(rfi_session.AgentCaptureSession, "_downloader", lambda self: Downloads())
    assert svc.run_pending(db, _settings(), session_factory=lambda h, s: pytest.fail("web session used")) == 1
    run = db.rows("pm_rfi_sync_runs")[0]
    assert run["status"] == "done" and run["stats"]["agent_outcome"] == "applied"
    # A tampered blob is never applied.
    _agent_run(db, status="queued", lease_owner=None, lease_until=None, attempts=0, rfi=RFI_189)
    monkeypatch.setattr(artifacts, "download_object", lambda path: raw + b" ")
    svc.run_pending(db, _settings())
    bad = [r for r in db.rows("pm_rfi_sync_runs") if r["target_external_rfi_id"] == RFI_189][0]
    assert bad["status"] == "failed" and "hash" in bad["error"]


# ── 5. Contributions ─────────────────────────────────────────────────────────


def _contrib(db, **over):
    row = {"id": str(uuid.uuid4()), "project_id": PROJECT, "platform": "procore", "external_rfi_id": RFI_198,
           "kind": "note", "note_text": "Use the EF-2 circuit.", "note_author_label": "Ana (Estimating Admin)",
           "help_id": "h1", "job_id": "job-1", "artifact_id": None, "applied_at": None, "applied_rfi_id": None,
           "created_at": "2026-10-19T17:00:00+00:00", **over}
    db.tables.setdefault("browser_agent_rfi_contributions", []).append(row)
    return row


def test_contributions_wait_for_their_rfi_then_apply_once(monkeypatch, _storage):
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    note = _contrib(db)
    blob = b"%PDF-1.7 helper sketch"
    sha = hashlib.sha256(blob).hexdigest()
    db.tables["browser_agent_artifacts"] = [
        {"id": "f1", "kind": "helper_upload", "attachable": True, "filename": "sketch.pdf",
         "storage_path": "help/h1/sketch.pdf", "sha256": sha},
        {"id": "f2", "kind": "helper_upload", "attachable": True, "filename": "model.dwg",
         "storage_path": "help/h1/model.dwg", "sha256": "b" * 64},
    ]
    monkeypatch.setattr(artifacts, "download_object", lambda path: blob)
    sketch = _contrib(db, kind="file_answer", note_text=None, artifact_id="f1")
    dwg = _contrib(db, kind="file_question", note_text=None, artifact_id="f2")
    assert contributions.apply_pending(db, PROJECT) == 0               # no RFI yet: nothing, no row invented
    assert db.rows("rfis") == []
    _run(db, _shim())                                                  # the agent apply imports the RFI ...
    rfi = db.rows("rfis", external_id=RFI_198)[0]
    assert all(c["applied_at"] for c in (note, sketch, dwg))           # ... and applies them in the same run
    assert rfi["external_supplement"] == [{
        "text": "Use the EF-2 circuit.", "by": "Ana (Estimating Admin)", "at": note["created_at"],
        "help_id": "h1", "contribution_id": note["id"]}]
    help_atts = [a for a in db.rows("rfi_attachments", rfi_id=rfi["id"]) if a["external_file_id"].startswith("help:")]
    assert [(a["kind"], a["external_file_id"]) for a in help_atts] == [("answer", f"help:{sha}")]
    [doc] = [d for d in db.rows("pm_documents") if "agent-help-file" in (d.get("note") or "")]
    assert f"[agent-help-file:{sha}]" in doc["note"]
    # Applying again changes nothing (applied rows, the supplement id, the doc marker).
    for c in (note, sketch):
        c["applied_at"] = None
    assert contributions.apply_pending(db, PROJECT) == 2
    assert len(db.rows("rfis", external_id=RFI_198)[0]["external_supplement"]) == 1
    assert len([d for d in db.rows("pm_documents") if "agent-help-file" in (d.get("note") or "")]) == 1
    assert len([a for a in db.rows("rfi_attachments", rfi_id=rfi["id"]) if a["external_file_id"].startswith("help:")]) == 1


def test_the_sweeper_applies_contributions_for_rfis_that_appear_later(monkeypatch):
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    note = _contrib(db, external_rfi_id=RFI_189)
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    assert contributions.sweep_pending(datetime.now(timezone.utc), {}) == 0
    svc.sync_project(db, svc.get_link(db, PROJECT), FakeSession(), _settings())   # the daily sweep imports it
    assert contributions.sweep_pending(datetime.now(timezone.utc), {}) == 1
    assert note["applied_rfi_id"] == db.rows("rfis", external_id=RFI_189)[0]["id"]


# ── 6. Retry after apply_failed ──────────────────────────────────────────────


def test_retry_after_apply_failed_makes_a_new_attempt(monkeypatch):
    h = Harness(monkeypatch)
    job = h.job("rfi_from_email")
    first = {"id": "run-0", "project_id": job["project_id"], "trigger": "agent", "status": "failed",
             "agent_job_id": job["id"], "apply_attempt": 0, "detail_artifact_id": "art-1",
             "target_external_rfi_id": "456", "source_email_id": None}
    h.db.tables["pm_rfi_sync_runs"] = [first]
    job.update(status="failed", outcome="apply_failed", error_kind="apply_failed", sync_run_id="run-0")
    run = rfi_from_email.retry_apply(job["id"])
    assert (run["apply_attempt"], run["detail_artifact_id"], run["trigger"]) == (1, "art-1", "agent")
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["outcome"], row["apply_attempt"], row["sync_run_id"]) == (
        "applying", "pending", 1, run["id"])
    with pytest.raises(rfi_from_email.RetryRefused):
        rfi_from_email.retry_apply(job["id"])                          # it is applying now
    # The sweeper then follows the new run.
    h.db.rows("pm_rfi_sync_runs", id=run["id"])[0].update(status="done", stats={"agent_outcome": "applied"})
    queue.reconcile_applying(h.db.now, {})
    assert h.db.one("browser_agent_jobs", id=job["id"])["status"] == "succeeded"


# ── 7. Fixes after the review (0158) ─────────────────────────────────────────


def test_a_provisional_write_stamps_the_capture_time_not_the_apply_time(_storage):
    db = _db(pm_external_links=[_link(backfilled_at="2026-10-01T00:00:00+00:00")])
    captured = datetime.now(timezone.utc) - timedelta(minutes=3)
    status, _ = _run(db, _shim(captured=captured))
    assert status == "done"
    [row] = db.rows("rfis", project_id=PROJECT)
    assert svc._parse_ts(row["external_synced_at"]) == captured
    # A second capture taken after the first one but applied later is not "stale".
    db.rows("pm_rfi_sync_runs")[0]["status"] = "done"
    later = captured + timedelta(minutes=1)
    status, run = _run(db, _shim(captured=later), job_id="job-2")
    assert run["stats"]["agent_outcome"] == "applied"


def test_a_note_append_keeps_an_entry_another_writer_added(monkeypatch):
    db = _db(rfis=[{"id": "r1", "project_id": PROJECT, "external_id": RFI_198,
                    "external_supplement": [{"text": "other", "contribution_id": "c-other"}]}])
    note = _contrib(db)
    contributions._apply_note(db, note, {"id": "r1"})
    contributions._apply_note(db, note, {"id": "r1"})                  # once only
    supplement = db.rows("rfis", id="r1")[0]["external_supplement"]
    assert [e["contribution_id"] for e in supplement] == ["c-other", note["id"]]


def test_retry_apply_refuses_a_second_run_and_points_the_job_at_the_new_run_in_one_write(monkeypatch):
    h = Harness(monkeypatch)
    job = h.job("rfi_from_email")
    first = {"id": "run-0", "project_id": job["project_id"], "trigger": "agent", "status": "failed",
             "agent_job_id": job["id"], "apply_attempt": 0, "detail_artifact_id": "art-1",
             "target_external_rfi_id": "456", "source_email_id": None}
    rival = {**first, "id": "run-rival", "status": "queued", "apply_attempt": 1}
    h.db.tables["pm_rfi_sync_runs"] = [first, rival]
    job.update(status="failed", outcome="apply_failed", error_kind="apply_failed", sync_run_id="run-0")
    with pytest.raises(rfi_from_email.RetryRefused):
        rfi_from_email.retry_apply(job["id"])                          # another retry already made attempt 1
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["sync_run_id"], row["apply_attempt"]) == ("failed", "run-0", 0)
    h.db.tables["pm_rfi_sync_runs"] = [first]
    updates = []
    real_update = type(h.db.table("browser_agent_jobs")).update

    def spy(self, payload):
        if self.table == "browser_agent_jobs":
            updates.append(dict(payload))
        return real_update(self, payload)

    monkeypatch.setattr(type(h.db.table("browser_agent_jobs")), "update", spy)
    run = rfi_from_email.retry_apply(job["id"])
    [moved] = [u for u in updates if u.get("status") == "applying"]
    assert moved["sync_run_id"] == run["id"]                           # never 'applying' with the old run
