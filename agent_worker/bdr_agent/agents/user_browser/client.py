"""A closed endpoint map: personal jobs never call organization job APIs."""
from typing import Any

from ...client import BdrClient, BdrRejected, SyncBdrClient
from ...protocol import (UserSessionRequest, UserLoginBeginRequest, UserLoginResultRequest,
                         UserHelpConsumeRequest, UserHelpOpenRequest, UserHelpSecretUseRequest)
from .guard import APP_HOSTS, LOGIN

ENDPOINT_MAP = {name: "user_" + name for name in (
    "steps", "checkpoint", "artifacts", "turns", "finish", "requeue", "park",
    "identity_session", "login_begin", "login_result", "help_open", "help_messages",
    "help_consume", "help_reply", "help_secret_use",
)}


class UserClient(BdrClient):
    def __init__(self, base: BdrClient):
        # Reuse the authenticated transport, never duplicate a token or close it.
        self.base = base
        self.in_job_retry = base.in_job_retry
        self.login_preflight: Any = None
        self.login_origin: Any = None
        self.login_attempt_id = None
        self.host = None

    def bind_host(self, host: str) -> None:
        if host not in APP_HOSTS or (self.host is not None and self.host != host):
            raise BdrRejected("identity_session", 403, "idor", "Personal identity host refused")
        self.host = host

    async def call(self, endpoint: str, **kw: Any) -> Any:
        target = ENDPOINT_MAP.get(endpoint)
        if target is None:
            raise BdrRejected(endpoint, 403, "idor", "Personal task endpoint refused")
        if endpoint in ("identity_session", "login_begin", "login_result"):
            if kw.get("path_params", {}).get("identity") != "procore":
                raise BdrRejected(endpoint, 403, "idor", "Personal identity refused")
            body = kw["body"].model_dump()
            if self.host not in APP_HOSTS or (endpoint != "login_result" and body.get("host") != self.host):
                raise BdrRejected(endpoint, 403, "idor", "Personal identity host refused")
            if endpoint == "identity_session":
                kw["body"] = UserSessionRequest(**body, origin="https://" + self.host)
            elif endpoint == "login_begin":
                if self.login_preflight is None:
                    raise BdrRejected(endpoint, 403, "idor", "Login page is not ready")
                origin = await self.login_preflight()
                if origin != "https://" + LOGIN:
                    raise BdrRejected(endpoint, 403, "idor", "Personal login origin refused")
                kw["body"] = UserLoginBeginRequest(**body, origin=origin)
            else:
                if self.login_origin is None:
                    raise BdrRejected(endpoint, 403, "idor", "Login page is not ready")
                origin = self.login_origin()
                expected = "https://" + (self.host if body["outcome"] == "ok" else LOGIN)
                if origin != expected:
                    raise BdrRejected(endpoint, 403, "idor", "Personal login result origin refused")
                kw["body"] = UserLoginResultRequest(**body, origin=origin)
        if endpoint == "help_open":
            body = kw["body"].model_dump()
            body["screenshot_artifact_id"] = None
            kw["body"] = UserHelpOpenRequest(**body, login_attempt_id=self.login_attempt_id)
        response = await self.base.call(target, **kw)
        if endpoint == "login_begin" and response.granted:
            self.login_attempt_id = response.attempt_id
        if endpoint == "login_result" and kw["body"].outcome != "mfa":
            self.login_attempt_id = None
        return response

    async def help_messages(self, lease, help_id, after=None):
        return await self.call("help_messages", lease=lease, path_params={"help_id": help_id},
                               retry=self.in_job_retry)

    async def help_consume(self, lease, help_id, message_ids):
        await self.call("help_consume", lease=lease, path_params={"help_id": help_id},
                        body=UserHelpConsumeRequest(message_ids=message_ids), retry=self.in_job_retry)

    async def help_secret_use(self, lease, help_id, message_id):
        if self.login_attempt_id is None:
            raise BdrRejected("help_secret_use", 403, "idor", "Login attempt is unavailable")
        return await self.call("help_secret_use", lease=lease,
                               path_params={"help_id": help_id, "message_id": message_id},
                               body=UserHelpSecretUseRequest(login_attempt_id=self.login_attempt_id),
                               idem_key=f"secret:{help_id}:{message_id}", retry=self.in_job_retry)


class UserSyncClient(SyncBdrClient):
    def __init__(self, base: SyncBdrClient):
        self.base = base

    def call(self, endpoint: str, **kw: Any) -> Any:
        if endpoint != "steps":
            raise BdrRejected(endpoint, 403, "idor", "Personal task endpoint refused")
        return self.base.call("user_steps", **kw)
