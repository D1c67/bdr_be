"""A fake BDR for agent tests: W1's MockBdr plus scripted /turns and stateful
identities, help, artifacts, contributions and rfi-capture.

Request bodies are still validated with the contract request models (in
MockBdr._dispatch), responses with the response models, so drift fails here.

Turn script items (consumed in order, one per non-pending turn):
    ("tool", {args})               an action
    {"stop": reason, ...}          a stop reply
    "pending"                      one 202 (retry_after 0)
    callable(bdr, job, body)       returns one of the above (sees prior results)
An exhausted script answers stop cap_reached.

Help: `help_answers[reason]` is a list of message dicts delivered as soon
as a request with that reason opens (author helper unless given); `hold_seconds`
sets hold_until. Consuming, redeeming a secret, or replying moves the job
back to running (the semantics the worker assumes; see the worker summary).
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from bdr_agent import protocol as P

from .mock_bdr import ApiError, MockBdr, _iso


def _now() -> datetime:
    return datetime.now(UTC)


class AgentBdr(MockBdr):
    def __init__(self) -> None:
        super().__init__()
        self.script: list[Any] = []
        self.turn_requests: list[dict[str, Any]] = []
        self.issued: dict[tuple[UUID, int], dict[str, Any]] = {}
        self.turn_no: dict[UUID, int] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        # identities
        self.stored_jar: list[dict[str, Any]] | None = None
        self.jar_logged_in_at: datetime | None = None
        self.session_status_override: list[dict[str, Any]] = []
        self.login_begin_override: list[dict[str, Any]] = []
        self.login_email = "estimating@g3.example"
        self.login_password = "Corr3ct-Horse-Battery!"
        self.login_begins: list[dict[str, Any]] = []
        self.login_results: list[dict[str, Any]] = []
        # help
        self.helps: dict[UUID, dict[str, Any]] = {}
        self.help_answers: dict[str, list[dict[str, Any]]] = {}
        self.help_join_answers: dict[str, list[dict[str, Any]]] = {}   # delivered when a job (re)joins
        self.hold_seconds = 30.0
        self.next_message_id = 100
        self.consumed: list[int] = []
        self.replies: list[str] = []
        self.secret_values: dict[int, str] = {}
        # effects
        self.contribution_calls: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []
        self.parks: list[UUID] = []

    # Turns -------------------------------------------------------------------------------

    def _ep_turns(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"], ("running",))
        self.turn_requests.append(body)
        prev = int(body["prev_turn_no"])
        stored = self.issued.get((job.job_id, prev + 1))
        if stored is not None:
            return self._json(200, stored)
        item = self.script.pop(0) if self.script else {"stop": "cap_reached"}
        if callable(item):
            item = item(self, job, body)
        if item == "pending":
            return self._json(202, {"retry_after": 0})
        if isinstance(item, dict) and "stop" in item:
            out = {"stop": True, "reason": item["stop"], "retry_at": item.get("retry_at"), "detail": None}
            return self._json(200, self._validated("turns", out))
        tool, args = item
        n = prev + 1
        out = {"turn_no": n, "assistant_text": f"step {n}", "action": {"call_id": f"call_{n}", "tool": tool,
                                                                         "args": args},
               "caps": {"steps_left": 40 - n, "tokens_left": 100000, "cost_left_usd": 1.5}}
        self.issued[(job.job_id, n)] = out
        return self._json(200, self._validated("turns", out))

    def tool_results(self) -> list[dict[str, Any]]:
        return [r for req in self.turn_requests for r in req.get("tool_results", [])]

    def last_result(self, tool_call_id: str | None = None) -> dict[str, Any]:
        results = self.tool_results()
        if tool_call_id is not None:
            results = [r for r in results if r["call_id"] == tool_call_id]
        return json.loads(results[-1]["content"])

    # Artifacts ---------------------------------------------------------------------------

    def _ep_artifacts(self, w, params, request, body) -> httpx.Response:
        self._fence(w, request, params["job_id"])
        P.ArtifactUploadForm.model_validate({k: body.get(k) for k in ("kind", "sha256", "filename")})
        data = body.get("file", b"")
        if hashlib.sha256(data).hexdigest() != body["sha256"]:
            raise ApiError("artifact_invalid")
        aid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{params['job_id']}:{body['kind']}:{body['sha256']}"))
        existed = aid in self.artifacts
        self.artifacts[aid] = {"job_id": params["job_id"], "kind": body["kind"], "filename": body["filename"],
                               "data": data}
        return self._json(200, self._validated("artifacts", {"artifact_id": aid, "existed": existed}))

    def artifacts_of(self, kind: str) -> list[dict[str, Any]]:
        return [a for a in self.artifacts.values() if a["kind"] == kind]

    # Identities --------------------------------------------------------------------------

    def _ep_identity_session(self, w, params, request, body) -> httpx.Response:
        self._fence(w, request, params["job_id"])
        if self.session_status_override:
            return self._json(200, self._validated("identity_session", self.session_status_override.pop(0)))
        reject = body.get("reject_jar_logged_in_at")
        if self.stored_jar and (reject is None or reject != _iso(self.jar_logged_in_at)):
            return self._json(200, self._validated("identity_session", {
                "status": "ok", "cookies": self.stored_jar, "logged_in_at": _iso(self.jar_logged_in_at),
                "source": "stored"}))
        return self._json(200, self._validated("identity_session", {"status": "no_jar"}))

    def _ep_login_begin(self, w, params, request, body) -> httpx.Response:
        self._fence(w, request, params["job_id"])
        self.login_begins.append(body)
        if self.login_begin_override:
            return self._json(200, self._validated("login_begin", self.login_begin_override.pop(0)))
        out = {"granted": True, "attempt_id": str(uuid.uuid4()), "email": self.login_email,
               "password": self.login_password}
        P.LoginBeginResponse.model_validate(out)
        return self._json(200, out)

    def _ep_login_result(self, w, params, request, body) -> httpx.Response:
        self._fence(w, request, params["job_id"])
        self.login_results.append(body)
        if body["outcome"] == "ok":
            self.stored_jar = body["cookies"]
            self.jar_logged_in_at = _now().replace(microsecond=0)
            return self._json(200, self._validated("login_result", {"locked_until": None, "login_failures": 0,
                                                                    "help_reason": None}))
        return self._json(200, self._validated("login_result", {
            "locked_until": None, "login_failures": 1,
            "help_reason": P.LOGIN_OUTCOME_HELP_REASON[body["outcome"]]}))

    # Help --------------------------------------------------------------------------------

    def _msg(self, help_id: UUID, spec: dict[str, Any]) -> dict[str, Any]:
        mid = self.next_message_id
        self.next_message_id += 1
        msg = {"id": mid, "author_kind": spec.get("author_kind", "helper"),
               "author_label": spec.get("author_label", "Dana (Estimating Admin)"),
               "action": spec.get("action", "message"), "body": spec.get("body"), "secret_ref": None,
               "files": spec.get("files", []), "created_at": _iso(_now())}
        if "secret" in spec:
            msg["body"] = None
            msg["secret_ref"] = {"message_id": mid, "consumed": False}
            self.secret_values[mid] = spec["secret"]
        self.helps[help_id]["messages"].append(msg)
        self.helps[help_id]["status"] = "answered" if msg["author_kind"] == "helper" else self.helps[help_id]["status"]
        return msg

    def deliver(self, help_id: UUID, spec: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return self._msg(help_id, spec)

    def _ep_help_open(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"], ("running",))
        reason = body["reason_code"]
        scope = P.help_scope_key(reason, identity="procore", project_id="proj-1", job_id=job.job_id)
        existing = next((h for h in self.helps.values() if h["scope"] == scope and h["status"] in ("open", "answered")),
                        None)
        joined = existing is not None
        if existing is None:
            hid = uuid.uuid4()
            existing = {"id": hid, "scope": scope, "reason": reason, "status": "open", "holder": job.job_id,
                        "messages": [], "waiters": set(), "opened_message": body.get("message")}
            self.helps[hid] = existing
            if body.get("message"):
                self._msg(hid, {"author_kind": "agent", "author_label": "RFI agent", "body": body["message"]})
            for spec in self.help_answers.get(reason, []):
                self._msg(hid, spec)
        elif self.help_join_answers.get(reason):
            self._msg(existing["id"], self.help_join_answers[reason].pop(0))
        existing["waiters"].add(job.job_id)
        job.status = "waiting_help"
        hold = _now() + timedelta(seconds=self.hold_seconds)
        return self._json(200, self._validated("help_open", {
            "help_id": str(existing["id"]), "joined_existing": joined, "holder": existing["holder"] == job.job_id,
            "suppressed": False, "hold_until": _iso(hold)}))

    def _help_for(self, job, help_id: str) -> dict[str, Any]:
        h = self.helps.get(UUID(help_id))
        if h is None or job.job_id not in h["waiters"]:
            raise ApiError("idor")
        return h

    def _ep_help_messages(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        h = self._help_for(job, params["help_id"])
        after = int(request.url.params.get("after") or 0)
        msgs = [m for m in h["messages"] if m["id"] > after]
        return self._json(200, self._validated("help_messages", {"status": h["status"], "claimed_by_label": None,
                                                                 "messages": msgs}))

    def _ep_help_consume(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        self._help_for(job, params["help_id"])
        self.consumed += body["message_ids"]
        if job.status == "waiting_help":
            job.status = "running"
        return self._json(204, None)

    def _ep_help_reply(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        h = self._help_for(job, params["help_id"])
        self.replies.append(body["body"])
        msg = self._msg(h["id"], {"author_kind": "agent", "author_label": "RFI agent", "body": body["body"]})
        return self._json(200, {"id": msg["id"]})

    def _ep_help_secret_use(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        h = self._help_for(job, params["help_id"])
        mid = int(params["message_id"])
        msg = next((m for m in h["messages"] if m["id"] == mid), None)
        if msg is None or msg["secret_ref"] is None or msg["secret_ref"]["consumed"] or h["holder"] != job.job_id:
            raise ApiError("idor")
        msg["secret_ref"]["consumed"] = True
        if job.status == "waiting_help":
            job.status = "running"
        return self._json(200, {"value": self.secret_values.pop(mid)})

    def _ep_park(self, w, params, request, body) -> httpx.Response:
        resp = super()._ep_park(w, params, request, body)
        self.parks.append(UUID(params["job_id"]))
        return resp

    # Effects -----------------------------------------------------------------------------

    def _ep_contributions(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        self._help_for(job, body["help_id"])
        self.contribution_calls.append(body)
        accepted = [{"index": i, "contribution_id": str(uuid.uuid4())} for i, _ in enumerate(body["items"])]
        return self._json(200, self._validated("contributions", {"accepted": accepted, "rejected": []}))

    def _ep_rfi_capture(self, w, params, request, body) -> httpx.Response:
        job = self._fence(w, request, params["job_id"])
        art = self.artifacts.get(body["detail_artifact_id"])
        if art is None or art["job_id"] != params["job_id"] or art["kind"] != "rfi_detail_json":
            raise ApiError("capture_invalid")
        env = P.RfiDetailEnvelope.model_validate_json(art["data"])
        seed_project = job.input["external_project_id"]
        if env.request_path != f"/rest/v1.0/projects/{seed_project}/rfis/{body['external_rfi_id']}" \
                or str(env.body.get("id")) != body["external_rfi_id"]:
            raise ApiError("capture_invalid")
        self.captures.append(body)
        job.status = "applying"
        job.lease_owner = job.lease_boot = None
        return self._json(200, self._validated("rfi_capture", {"run_id": str(uuid.uuid4()), "accepted": True}))
