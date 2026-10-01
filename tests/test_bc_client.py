"""The BuildingConnected client and token store (app/services/bc_client)
driven through httpx.MockTransport with the in-memory FakeDB, a clock that
advances only on the patched sleep, and the fixture rows in
tests/fixtures_bc (docs/RFP_BUILDINGCONNECTED.md section 4; BUILD_CONTRACT
D26, D27, 3.1).

Pinned: config from a Settings-like object (never the secret in repr); the
authorize URL and the updatedAt filter; state create/consume (single use,
expired, missing); the code exchange and users/me; the stored connection
and the status view (never a token); the fresh-token path (no HTTP); the
refresh under the lock with the new refresh token written BEFORE the API
call that uses the access token; 401 -> refresh -> retry once and the
2-legged detail on the second 401; 429 with Retry-After, its cap and
BcRateLimited past max_retries; 5xx and transport retries; cursorState
paging over the 24 fixture rows with renew() between pages; lock
contention (the waiter reads the winner's token without refreshing, and
times out when nobody finishes); invalid_grant -> disconnected row + the
bell to IT Admins and Executives + BcDisconnected; a failed refresh
releasing the lock; and that no secret reaches a log line.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.roles import Role
from app.services import bc_client as bc
from tests.test_rfp_email_ingest import FakeDB, _Query

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures_bc"
OPPORTUNITIES = json.loads((FIXTURES / "opportunities.json").read_text(encoding="utf-8"))
ME = json.loads((FIXTURES / "users_me.json").read_text(encoding="utf-8"))

CLIENT_ID = "client-id-123"
CLIENT_SECRET = "s3cr3t-never-logged"
REDIRECT = "http://localhost:5051/rfp-portal/buildingconnected/callback"
ACCESS_OLD = "access-old-token-aaaa"
ACCESS_NEW = "access-new-token-bbbb"
REFRESH_OLD = "refresh-old-token-cccc"
REFRESH_NEW = "refresh-new-token-dddd"
TWO_LEGGED = "You are not authorized to use a 2-legged token"

API_PREFIX = "/construction/buildingconnected/v2"
TOKEN_PATH = "/authentication/v2/token"


def _iso(dt):
    return dt.isoformat()


def _config(**over) -> bc.BcConfig:
    base = dict(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_url=REDIRECT,
        timeout_seconds=5.0,
        test_mode=False,
        max_retries=3,
    )
    base.update(over)
    return bc.BcConfig(**base)


class Clock:
    """`now()` for the client; `sleep()` advances it and records the waits."""

    def __init__(self, start=NOW):
        self.now_value = start
        self.sleeps: list[float] = []
        self.on_sleep = None

    def now(self):
        return self.now_value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now_value = self.now_value + timedelta(seconds=seconds)
        if self.on_sleep is not None:
            self.on_sleep(self, len(self.sleeps))


class RecordingQuery(_Query):
    def execute(self):
        result = super().execute()
        if self._op in ("insert", "update", "upsert", "delete"):
            self.db.events.append(("db", self._op, self.table, dict(self._payload or {})))
        return result


class RecordingDB(FakeDB):
    """FakeDB whose writes land in `events` next to the transport's
    requests, so the order of DB writes vs HTTP calls can be asserted."""

    def __init__(self, tables=None):
        super().__init__(tables)
        self.events: list[tuple] = []

    def table(self, name):
        return RecordingQuery(self, name)


def _connected_row(**over):
    row = {
        "provider": bc.PROVIDER,
        "status": "connected",
        "access_token": ACCESS_OLD,
        "refresh_token": REFRESH_OLD,
        "expires_at": _iso(NOW + timedelta(hours=1)),
        "scope": "data:read",
        "connected_by": "u-it",
        "connected_at": _iso(NOW - timedelta(days=3)),
        "external_user_id": ME["id"],
        "external_user_name": "Fixture Admin",
        "external_user_email": ME["email"],
        "external_company_id": ME["companyId"],
        "view_all": True,
        "last_refresh_at": None,
        "last_used_at": None,
        "last_error": None,
        "refresh_lock_until": None,
        "refresh_lock_owner": None,
        "disconnected_at": None,
    }
    row.update(over)
    return row


def _db(row=None, tables=None) -> RecordingDB:
    base = {"rfp_oauth_connections": [], "rfp_oauth_states": [], "notifications": [], "profiles": []}
    base.update(tables or {})
    if row is not None:
        base["rfp_oauth_connections"] = [row]
    return RecordingDB(base)


def _row(db) -> dict:
    return db.tables["rfp_oauth_connections"][0]


class FakeAutodesk:
    """MockTransport handler: the token endpoint and the Bid Board API.
    `api_modes` is a queue of answers for GET requests under the API
    prefix (consumed in order; the last one repeats); `token_mode` shapes
    the token endpoint. Records every request and can log into a shared
    event list next to the DB writes."""

    def __init__(self, *, events=None, page_size=100):
        self.requests: list[httpx.Request] = []
        self.events = events
        self.api_modes: list = []
        self.token_mode = "ok"          # ok | invalid_grant | invalid_client | 500 | transport
        self.token_calls = 0
        self.page_size = page_size
        self.issued_access = ACCESS_NEW
        self.issued_refresh = REFRESH_NEW

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.events is not None:
            self.events.append(("http", request.method, request.url.path))
        path = request.url.path
        if path == TOKEN_PATH:
            return self._token(request)
        if path.startswith(API_PREFIX):
            return self._api(request)
        return httpx.Response(404, json={"message": "no such route"})

    # -- token endpoint -------------------------------------------------------

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        if self.token_mode == "transport":
            raise httpx.ConnectError("boom", request=request)
        if self.token_mode == "500":
            return httpx.Response(500, json={"developerMessage": "upstream"})
        if self.token_mode == "invalid_grant":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "The refresh token is invalid or expired"}
            )
        if self.token_mode == "invalid_client":
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "client secret rejected"})
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": self.issued_access,
                "refresh_token": self.issued_refresh,
                "expires_in": 3599,
                "scope": "data:read",
            },
        )

    @property
    def token_forms(self) -> list[dict]:
        return [
            {k: v[-1] for k, v in parse_qs(r.content.decode(), keep_blank_values=True).items()}
            for r in self.requests
            if r.url.path == TOKEN_PATH
        ]

    # -- API ------------------------------------------------------------------

    def _api(self, request: httpx.Request) -> httpx.Response:
        mode = self.api_modes.pop(0) if len(self.api_modes) > 1 else (self.api_modes[0] if self.api_modes else "ok")
        if mode == "transport":
            raise httpx.ReadTimeout("slow", request=request)
        if isinstance(mode, tuple) and mode[0] == 429:
            return httpx.Response(429, headers={"Retry-After": str(mode[1])}, json={"message": "Too Many Requests"})
        if mode == 429:
            return httpx.Response(429, json={"message": "Too Many Requests"})
        if mode == 401:
            return httpx.Response(401, json={"developerMessage": TWO_LEGGED, "errorCode": "AUTH-001"})
        if mode == 500:
            return httpx.Response(500, json={"message": "Internal Server Error"})
        if mode == 503:
            return httpx.Response(503, text="Service Unavailable")
        if mode == 404:
            return httpx.Response(404, json={"message": "Not Found"})
        if mode == 400:
            return httpx.Response(400, json={"message": "query.limit should be <= 100"})
        return self._ok(request)

    def _ok(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path[len(API_PREFIX):]
        if path == "/users/me":
            return httpx.Response(200, json=ME)
        if path == "/opportunities":
            params = dict(request.url.params)
            limit = int(params.get("limit", "100"))
            if limit > 100:
                return httpx.Response(400, json={"message": "query.limit should be <= 100"})
            size = min(limit, self.page_size)
            start = int(params.get("cursorState", "0"))
            rows = OPPORTUNITIES[start:start + size]
            nxt = start + size
            pagination = {"limit": size, "cursorState": str(nxt)} if nxt < len(OPPORTUNITIES) else {"limit": size}
            return httpx.Response(200, json={"pagination": pagination, "results": rows})
        if path.startswith("/opportunities/"):
            oid = path.rsplit("/", 1)[-1]
            row = next((r for r in OPPORTUNITIES if r["id"] == oid), None)
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "no such path"})

    @property
    def api_calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path[len(API_PREFIX):]) for r in self.requests if r.url.path.startswith(API_PREFIX)]

    @property
    def bearers(self) -> list[str]:
        return [
            r.headers.get("authorization", "").replace("Bearer ", "")
            for r in self.requests
            if r.url.path.startswith(API_PREFIX)
        ]


def _client(db, fake, clock, config=None, renew=None) -> bc.BcClient:
    return bc.BcClient(
        db,
        config or _config(),
        transport=httpx.MockTransport(fake),
        sleep=clock.sleep,
        now=clock.now,
        renew=renew,
    )


@pytest.fixture
def bells(monkeypatch):
    """`notify_role` recorded instead of touching the real client."""
    rung: list[dict] = []

    def fake_notify_role(role, project_id, type_, message, rfq_id=None, mirror_email=True, metadata=None):
        rung.append(
            {"role": role, "project_id": project_id, "type": type_, "message": message,
             "mirror_email": mirror_email, "metadata": metadata}
        )

    monkeypatch.setattr(bc, "notify_role", fake_notify_role)
    return rung


# ── Config, URLs, filter ────────────────────────────────────────────────────


def test_config_from_settings_reads_attributes_and_hides_the_secret():
    settings = SimpleNamespace(
        building_connected_client_id="  id-1 ",
        building_connected_client_secret="top-secret",
        rfp_bc_redirect_url=" https://api.example/rfp-portal/buildingconnected/callback ",
        rfp_bc_request_timeout_seconds=42,
        rfp_bc_test_mode=True,
    )
    config = bc.config_from_settings(settings)
    assert config == bc.BcConfig(
        client_id="id-1",
        client_secret="top-secret",
        redirect_url="https://api.example/rfp-portal/buildingconnected/callback",
        timeout_seconds=42.0,
        test_mode=True,
        max_retries=3,
    )
    assert "top-secret" not in repr(config)
    assert "top-secret" not in str(config)
    blank = bc.config_from_settings(SimpleNamespace())
    assert blank.client_id == "" and blank.client_secret == "" and blank.redirect_url == ""
    assert blank.timeout_seconds == 60.0 and blank.test_mode is False and blank.max_retries == 3
    assert bc.PROVIDER == "buildingconnected"
    assert bc.BC_BASE == "https://developer.api.autodesk.com/construction/buildingconnected/v2"
    assert bc.AUTH_BASE == "https://developer.api.autodesk.com/authentication/v2"
    assert bc.DEEP_LINK == "https://app.buildingconnected.com/opportunities/{id}/info"


def test_authorize_url_carries_the_redirect_byte_for_byte():
    url = bc.authorize_url(_config(), "state-xyz")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://developer.api.autodesk.com/authentication/v2/authorize"
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    assert query == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "scope": "data:read",
        "state": "state-xyz",
    }
    assert "scope=account%3Aread" in bc.authorize_url(_config(), "s", scope="account:read")
    assert CLIENT_SECRET not in url


def test_updated_filter_format():
    since = datetime(2026, 9, 26, 11, 30, 5, 123456, tzinfo=timezone.utc)
    assert bc.updated_filter(since) == "2026-09-26T11:30:05.123Z.."
    pacific = datetime(2026, 9, 26, 4, 30, 5, tzinfo=timezone(timedelta(hours=-7)))
    assert bc.updated_filter(pacific) == "2026-09-26T11:30:05.000Z.."


# ── State ───────────────────────────────────────────────────────────────────


def test_state_is_single_use_and_bound_to_the_actor(monkeypatch):
    db = _db()
    monkeypatch.setattr(bc, "_now", lambda: NOW)
    state = bc.new_state(db, "u-exec")
    assert len(state) >= 32
    rows = db.tables["rfp_oauth_states"]
    assert len(rows) == 1
    assert rows[0]["state"] == state
    assert rows[0]["provider"] == "buildingconnected"
    assert rows[0]["actor_id"] == "u-exec"
    assert rows[0]["expires_at"] == _iso(NOW + timedelta(seconds=600))
    assert rows[0]["created_at"] == _iso(NOW)
    second = bc.new_state(db, "u-exec", ttl_seconds=60)
    assert second != state
    consumed = bc.consume_state(db, state)
    assert consumed["actor_id"] == "u-exec"
    assert [r["state"] for r in db.tables["rfp_oauth_states"]] == [second]
    assert bc.consume_state(db, state) is None          # single use
    assert bc.consume_state(db, "never-issued") is None
    assert bc.consume_state(db, "") is None
    assert bc.consume_state(db, None) is None


def test_expired_state_is_refused_and_removed(monkeypatch):
    db = _db()
    monkeypatch.setattr(bc, "_now", lambda: NOW)
    state = bc.new_state(db, "u-exec", ttl_seconds=600)
    monkeypatch.setattr(bc, "_now", lambda: NOW + timedelta(seconds=601))
    assert bc.consume_state(db, state) is None
    assert db.tables["rfp_oauth_states"] == []
    # Another provider's state with the same value is never ours.
    db.tables["rfp_oauth_states"].append(
        {"state": "shared", "provider": "other", "actor_id": "u", "expires_at": _iso(NOW + timedelta(hours=1))}
    )
    assert bc.consume_state(db, "shared") is None
    assert len(db.tables["rfp_oauth_states"]) == 1


# ── Code exchange, users/me, the stored connection ──────────────────────────


def test_exchange_code_posts_basic_auth_and_the_redirect():
    fake = FakeAutodesk()
    tokens = bc.exchange_code(_config(), "auth-code-1", transport=httpx.MockTransport(fake))
    assert tokens["access_token"] == ACCESS_NEW and tokens["refresh_token"] == REFRESH_NEW
    assert tokens["expires_in"] == 3599
    request = fake.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://developer.api.autodesk.com/authentication/v2/token"
    assert request.headers["authorization"].startswith("Basic ")
    import base64

    assert base64.b64decode(request.headers["authorization"][6:]).decode() == f"{CLIENT_ID}:{CLIENT_SECRET}"
    assert fake.token_forms[0] == {"grant_type": "authorization_code", "code": "auth-code-1", "redirect_uri": REDIRECT}
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")


def test_exchange_code_failures_classify():
    fake = FakeAutodesk()
    fake.token_mode = "invalid_grant"
    with pytest.raises(bc.BcPermanent) as info:
        bc.exchange_code(_config(), "bad-code", transport=httpx.MockTransport(fake))
    assert info.value.status == 400
    assert "invalid_grant" in str(info.value) or "invalid or expired" in str(info.value)
    fake.token_mode = "500"
    with pytest.raises(bc.BcTransient):
        bc.exchange_code(_config(), "code", transport=httpx.MockTransport(fake))
    fake.token_mode = "transport"
    with pytest.raises(bc.BcTransient):
        bc.exchange_code(_config(), "code", transport=httpx.MockTransport(fake))


def test_fetch_me_sends_the_bearer_and_the_test_header():
    fake = FakeAutodesk()
    me = bc.fetch_me(ACCESS_NEW, _config(test_mode=True), transport=httpx.MockTransport(fake))
    assert me["bidBoardPermissions"]["viewAll"] is True
    assert bc.view_all(me) is True
    assert bc.view_all({"bidBoardPermissions": {"viewAll": False}}) is False
    assert bc.view_all({}) is False
    request = fake.requests[0]
    assert request.url.path == f"{API_PREFIX}/users/me"
    assert request.headers["authorization"] == f"Bearer {ACCESS_NEW}"
    assert request.headers["x-bc-mode"] == "test"
    plain = FakeAutodesk()
    bc.fetch_me(ACCESS_NEW, _config(), transport=httpx.MockTransport(plain))
    assert "x-bc-mode" not in plain.requests[0].headers
    plain.api_modes = [401]
    with pytest.raises(bc.BcDisconnected) as info:
        bc.fetch_me("two-legged", _config(), transport=httpx.MockTransport(plain))
    assert TWO_LEGGED in str(info.value)


def test_store_connection_and_status_never_expose_tokens(monkeypatch):
    db = _db()
    monkeypatch.setattr(bc, "_now", lambda: NOW)
    tokens = {"access_token": ACCESS_NEW, "refresh_token": REFRESH_NEW, "expires_in": 3599, "scope": "data:read"}
    row = bc.store_connection(db, tokens, ME, "u-exec")
    stored = _row(db)
    assert row["status"] == "connected" and stored["status"] == "connected"
    assert stored["access_token"] == ACCESS_NEW
    assert stored["refresh_token"] == REFRESH_NEW
    assert stored["expires_at"] == _iso(NOW + timedelta(seconds=3599))
    assert stored["connected_by"] == "u-exec"
    assert stored["connected_at"] == _iso(NOW)
    assert stored["external_user_id"] == ME["id"]
    assert stored["external_user_name"] == "Fixture Admin"
    assert stored["external_user_email"] == "fixture.admin@example.com"
    assert stored["external_company_id"] == ME["companyId"]
    assert stored["view_all"] is True
    assert stored["refresh_lock_until"] is None and stored["refresh_lock_owner"] is None
    assert stored["disconnected_at"] is None
    status = bc.connection_status(db)
    assert status == {
        "status": "connected",
        "connected_by": "u-exec",
        "connected_at": _iso(NOW),
        "external_user_name": "Fixture Admin",
        "external_user_email": "fixture.admin@example.com",
        "view_all": True,
        "last_refresh_at": None,
        "last_used_at": None,
        "last_error": None,
    }
    assert ACCESS_NEW not in json.dumps(status) and REFRESH_NEW not in json.dumps(status)
    assert bc.connection(db)["refresh_token"] == REFRESH_NEW      # the full row, callers redact
    # Reconnecting replaces the row in place (one row per provider).
    bc.store_connection(db, {**tokens, "access_token": "third"}, ME, "u-it")
    assert len(db.tables["rfp_oauth_connections"]) == 1
    assert _row(db)["access_token"] == "third" and _row(db)["connected_by"] == "u-it"


def test_connection_status_without_a_row_and_disconnect(monkeypatch):
    db = _db()
    assert bc.connection(db) is None
    assert bc.connection_status(db)["status"] == "disconnected"
    assert set(bc.connection_status(db)) == {
        "status", "connected_by", "connected_at", "external_user_name", "external_user_email",
        "view_all", "last_refresh_at", "last_used_at", "last_error",
    }
    monkeypatch.setattr(bc, "_now", lambda: NOW)
    bc.disconnect(db)
    assert _row(db)["status"] == "disconnected" and _row(db)["last_error"] is None
    db = _db(_connected_row(refresh_lock_owner="w", refresh_lock_until=_iso(NOW + timedelta(seconds=10))))
    bc.disconnect(db, error="manual")
    stored = _row(db)
    assert stored["status"] == "disconnected"
    assert stored["access_token"] is None and stored["refresh_token"] is None and stored["expires_at"] is None
    assert stored["last_error"] == "manual"
    assert stored["disconnected_at"] == _iso(NOW)
    assert stored["refresh_lock_until"] is None and stored["refresh_lock_owner"] is None
    assert stored["external_user_name"] == "Fixture Admin"     # identity kept for the settings block
    assert bc.connection_status(db)["last_error"] == "manual"


# ── access_token: fresh, refresh, the lock ──────────────────────────────────


def test_fresh_token_is_returned_without_any_http():
    db = _db(_connected_row())
    fake = FakeAutodesk()
    clock = Clock()
    token = bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert token == ACCESS_OLD
    assert fake.requests == []
    assert clock.sleeps == []
    assert _row(db)["refresh_lock_owner"] is None


def test_no_connection_raises_disconnected():
    fake = FakeAutodesk()
    for db in (_db(), _db(_connected_row(status="disconnected"))):
        with pytest.raises(bc.BcDisconnected):
            bc.access_token(db, _config(), transport=httpx.MockTransport(fake))
    assert fake.requests == []


@pytest.mark.parametrize("expires_in", [-60, 0, 119])
def test_expiring_token_is_refreshed_and_rotated(expires_in):
    db = _db(_connected_row(expires_at=_iso(NOW + timedelta(seconds=expires_in))))
    fake = FakeAutodesk()
    clock = Clock()
    token = bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert token == ACCESS_NEW
    assert fake.token_calls == 1
    assert fake.token_forms == [{"grant_type": "refresh_token", "refresh_token": REFRESH_OLD}]
    stored = _row(db)
    assert stored["access_token"] == ACCESS_NEW
    assert stored["refresh_token"] == REFRESH_NEW
    assert stored["expires_at"] == _iso(NOW + timedelta(seconds=3599))
    assert stored["last_refresh_at"] == _iso(NOW)
    assert stored["last_error"] is None
    assert stored["refresh_lock_until"] is None and stored["refresh_lock_owner"] is None
    assert stored["status"] == "connected"
    # A second call now finds the fresh token: no HTTP.
    assert bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now) == ACCESS_NEW
    assert fake.token_calls == 1


def test_token_120_seconds_before_expiry_is_still_fresh():
    db = _db(_connected_row(expires_at=_iso(NOW + timedelta(seconds=121))))
    fake = FakeAutodesk()
    clock = Clock()
    assert bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now) == ACCESS_OLD
    assert fake.token_calls == 0


def test_new_refresh_token_is_written_before_the_api_call_uses_it():
    """D27 last sentence: the DB write carrying the rotated refresh token
    precedes the GET that uses the new access token, and the lock claim
    precedes the token POST."""
    db = _db(_connected_row(expires_at=_iso(NOW + timedelta(seconds=30))))
    fake = FakeAutodesk(events=db.events)
    clock = Clock()
    with _client(db, fake, clock) as client:
        me = client.me()
    assert me["id"] == ME["id"]
    events = db.events
    kinds = [(e[0], e[1], e[2]) for e in events]
    claim = next(i for i, e in enumerate(events) if e[0] == "db" and e[1] == "update" and e[3].get("refresh_lock_owner") == bc._WORKER_TOKEN)
    post = next(i for i, e in enumerate(events) if e[0] == "http" and e[1] == "POST")
    write = next(i for i, e in enumerate(events) if e[0] == "db" and e[1] == "update" and e[3].get("refresh_token") == REFRESH_NEW)
    get = next(i for i, e in enumerate(events) if e[0] == "http" and e[1] == "GET")
    assert claim < post < write < get, kinds
    # The rotation write also clears the lock in the same statement.
    write_payload = events[write][3]
    assert write_payload["refresh_lock_until"] is None and write_payload["refresh_lock_owner"] is None
    assert write_payload["access_token"] == ACCESS_NEW
    assert fake.bearers == [ACCESS_NEW]


def test_401_refreshes_once_and_retries_once():
    db = _db(_connected_row())     # fresh by expiry, yet the API refuses it
    fake = FakeAutodesk()
    fake.api_modes = [401, "ok"]
    clock = Clock()
    with _client(db, fake, clock) as client:
        body = client.get("/opportunities", {"limit": 5})
    assert len(body["results"]) == 5
    assert [r.method for r in fake.requests] == ["GET", "POST", "GET"]
    assert fake.bearers == [ACCESS_OLD, ACCESS_NEW]
    assert fake.token_forms == [{"grant_type": "refresh_token", "refresh_token": REFRESH_OLD}]
    assert _row(db)["refresh_token"] == REFRESH_NEW
    assert _row(db)["last_used_at"] == _iso(NOW)


def test_401_twice_disconnects_the_row_and_rings_the_bell(bells):
    """A freshly refreshed token the Bid Board still refuses (the user lost
    Bid Board access, or the app its grant): the row is disconnected and
    the disconnected bell rings, exactly like a rejected refresh, so the
    scan run does not park forever behind a row that says connected."""
    db = _db(_connected_row())
    fake = FakeAutodesk()
    fake.api_modes = [401, 401]
    clock = Clock()
    with _client(db, fake, clock) as client:
        with pytest.raises(bc.BcDisconnected) as info:
            client.get("/opportunities")
    assert info.value.status == 401
    assert TWO_LEGGED in str(info.value)
    assert [r.method for r in fake.requests] == ["GET", "POST", "GET"]
    assert fake.token_calls == 1
    stored = _row(db)
    assert stored["status"] == "disconnected" and stored["disconnected_at"]
    assert stored["access_token"] is None and stored["refresh_token"] is None
    assert "freshly issued" in stored["last_error"] and "reconnect" in stored["last_error"]
    assert [b["role"] for b in bells] == [Role.IT_ADMIN, Role.EXECUTIVE]
    assert all(b["type"] == bc.NOTIFY_DISCONNECTED and b["mirror_email"] is True for b in bells)
    assert "refused a freshly issued token" in bells[0]["message"]
    assert TWO_LEGGED in bells[0]["metadata"]["error"]
    # A later call finds no connection: no token POST, no rotation.
    with pytest.raises(bc.BcDisconnected):
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert fake.token_calls == 1


def test_401_uses_another_workers_newer_token_before_refreshing():
    """The stored token differs from the one that failed (another worker
    refreshed meanwhile): use it, no token POST."""
    db = _db(_connected_row())
    fake = FakeAutodesk()
    clock = Clock()
    client = _client(db, fake, clock)

    original_get = client._client.get
    calls = {"n": 0}

    def swapping_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _row(db)["access_token"] = "other-worker-token"
        return original_get(url, **kwargs)

    client._client.get = swapping_get
    fake.api_modes = [401, "ok"]
    client.get("/users/me")
    assert fake.token_calls == 0
    assert fake.bearers == [ACCESS_OLD, "other-worker-token"]
    assert _row(db)["refresh_lock_owner"] is None
    client.close()


def test_429_sleeps_retry_after_capped_and_gives_up_past_max_retries():
    db = _db(_connected_row())
    fake = FakeAutodesk()
    fake.api_modes = [(429, "7"), (429, "500"), 429, "ok"]
    clock = Clock()
    with _client(db, fake, clock) as client:
        body = client.get("/users/me")
    assert body["id"] == ME["id"]
    assert clock.sleeps == [7.0, 120.0, 5.0]        # header, capped, default when absent
    assert len(fake.api_calls) == 4
    fake = FakeAutodesk()
    fake.api_modes = [(429, "1"), (429, "1"), (429, "1"), (429, "9"), "ok"]
    clock = Clock()
    with _client(db, fake, clock) as client:
        with pytest.raises(bc.BcRateLimited) as info:
            client.get("/users/me")
    assert info.value.retry_after == 9.0
    assert info.value.status == 429
    assert clock.sleeps == [1.0, 1.0, 1.0]           # max_retries sleeps, then the fourth 429 raises
    assert len(fake.api_calls) == 4
    fake = FakeAutodesk()
    fake.api_modes = [(429, "3"), "ok"]
    clock = Clock()
    with _client(db, fake, clock, config=_config(max_retries=0)) as client:
        with pytest.raises(bc.BcRateLimited):
            client.get("/users/me")
    assert clock.sleeps == []


def test_5xx_and_transport_errors_retry_twice_with_backoff():
    db = _db(_connected_row())
    fake = FakeAutodesk()
    fake.api_modes = [500, "transport", "ok"]
    clock = Clock()
    with _client(db, fake, clock) as client:
        assert client.get("/users/me")["id"] == ME["id"]
    assert clock.sleeps == [2.0, 5.0]
    fake = FakeAutodesk()
    fake.api_modes = [503, 500, 500]
    clock = Clock()
    with _client(db, fake, clock) as client:
        with pytest.raises(bc.BcTransient) as info:
            client.get("/users/me")
    assert info.value.status == 500
    assert len(fake.api_calls) == 3
    fake = FakeAutodesk()
    fake.api_modes = ["transport"]
    clock = Clock()
    with _client(db, fake, clock) as client:
        with pytest.raises(bc.BcTransient) as info:
            client.get("/users/me")
    assert "ReadTimeout" in str(info.value)
    assert len(fake.api_calls) == 3


def test_other_4xx_is_permanent_with_the_detail():
    db = _db(_connected_row())
    fake = FakeAutodesk()
    fake.api_modes = [400]
    clock = Clock()
    with _client(db, fake, clock) as client:
        with pytest.raises(bc.BcPermanent) as info:
            client.get("/opportunities", {"limit": 200})
    assert info.value.status == 400
    assert "query.limit should be <= 100" in str(info.value)
    fake.api_modes = [404]
    with _client(db, fake, clock) as client:
        with pytest.raises(bc.BcPermanent) as info:
            client.opportunity("nope")
    assert info.value.status == 404
    assert clock.sleeps == []


def test_error_hierarchy():
    assert issubclass(bc.BcDisconnected, bc.BcError)
    assert issubclass(bc.BcRateLimited, bc.BcError)
    assert issubclass(bc.BcTransient, bc.BcError)
    assert issubclass(bc.BcPermanent, bc.BcError)
    assert bc.BcError("x").status is None
    assert bc.BcPermanent("x", status=418).status == 418
    assert bc.BcRateLimited("x", retry_after=3.5).retry_after == 3.5


# ── Paging ──────────────────────────────────────────────────────────────────


def test_iter_opportunities_pages_on_cursor_state_and_renews_between_pages():
    db = _db(_connected_row())
    fake = FakeAutodesk()
    clock = Clock()
    renewals = []
    with _client(db, fake, clock, config=_config(test_mode=True), renew=lambda: renewals.append(1)) as client:
        rows = list(client.iter_opportunities(limit=10))
    assert len(rows) == 24
    assert [r["id"] for r in rows] == [r["id"] for r in OPPORTUNITIES]
    assert all("_fixture_role" not in r for r in rows)
    assert fake.api_calls == [("GET", "/opportunities")] * 3
    params = [dict(r.url.params) for r in fake.requests]
    assert params[0] == {"limit": "10"}
    assert params[1] == {"limit": "10", "cursorState": "10"}
    assert params[2] == {"limit": "10", "cursorState": "20"}
    assert len(renewals) == 2
    assert all(r.headers["x-bc-mode"] == "test" for r in fake.requests)
    assert fake.bearers == [ACCESS_OLD] * 3


def test_iter_opportunities_incremental_filter_default_limit_and_page_cap():
    db = _db(_connected_row())
    fake = FakeAutodesk(page_size=5)
    clock = Clock()
    since = datetime(2026, 9, 26, 11, 30, tzinfo=timezone.utc)
    with _client(db, fake, clock) as client:
        rows = list(client.iter_opportunities(updated_since=since))
    assert len(rows) == 24 and client.truncated is False     # the board's last page was reached
    first = dict(fake.requests[0].url.params)
    assert first == {"limit": "100", "filter[updatedAt]": "2026-09-26T11:30:00.000Z.."}
    assert all(dict(r.url.params)["filter[updatedAt]"] == "2026-09-26T11:30:00.000Z.." for r in fake.requests)
    assert len(fake.api_calls) == 5
    # The client never asks for more than 100 and honours a page cap.
    fake = FakeAutodesk(page_size=5)
    renews = []
    with _client(db, fake, clock, renew=lambda: renews.append(1)) as client:
        rows = list(client.iter_opportunities(limit=250, max_pages=2))
    assert dict(fake.requests[0].url.params)["limit"] == "100"
    assert len(rows) == 10
    assert len(fake.api_calls) == 2
    # The capped pull says so (more pages were pending), and the cap is checked
    # before the between-pages renew, so nothing is renewed for a page never asked for.
    assert client.truncated is True and len(renews) == 1
    # A cap the board fits under is not a truncation.
    fake = FakeAutodesk(page_size=5)
    with _client(db, fake, clock) as client:
        assert len(list(client.iter_opportunities(max_pages=5))) == 24
    assert client.truncated is False


def test_opportunity_and_me():
    db = _db(_connected_row())
    fake = FakeAutodesk()
    clock = Clock()
    with _client(db, fake, clock) as client:
        row = client.opportunity(OPPORTUNITIES[3]["id"])
        me = client.me()
    assert row["name"] == OPPORTUNITIES[3]["name"]
    assert "_fixture_role" not in row
    assert me["email"] == ME["email"]
    assert fake.api_calls == [("GET", f"/opportunities/{OPPORTUNITIES[3]['id']}"), ("GET", "/users/me")]


# ── Lock contention (D27) ───────────────────────────────────────────────────


def test_waiter_reads_the_winners_token_without_refreshing():
    """Another worker holds the lock. This one polls every second; when the
    winner's rotation lands the waiter returns that token and never posts
    to the token endpoint."""
    db = _db(
        _connected_row(
            expires_at=_iso(NOW + timedelta(seconds=10)),
            refresh_lock_until=_iso(NOW + timedelta(seconds=25)),
            refresh_lock_owner="other-worker",
        )
    )
    fake = FakeAutodesk()
    clock = Clock()

    def winner_finishes(clk, n):
        if n == 3:
            _row(db).update(
                access_token="winner-access",
                refresh_token="winner-refresh",
                expires_at=_iso(clk.now_value + timedelta(seconds=3599)),
                refresh_lock_until=None,
                refresh_lock_owner=None,
            )

    clock.on_sleep = winner_finishes
    token = bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert token == "winner-access"
    assert fake.token_calls == 0
    assert clock.sleeps == [1.0, 1.0, 1.0]
    assert _row(db)["refresh_token"] == "winner-refresh"
    assert _row(db)["refresh_lock_owner"] is None


def test_waiter_times_out_when_nobody_finishes():
    db = _db(
        _connected_row(
            expires_at=_iso(NOW - timedelta(seconds=1)),
            refresh_lock_until=_iso(NOW + timedelta(seconds=3600)),
            refresh_lock_owner="other-worker",
        )
    )
    fake = FakeAutodesk()
    clock = Clock()
    with pytest.raises(bc.BcTransient) as info:
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert "wait timed out" in str(info.value)
    assert clock.sleeps == [1.0] * 30
    assert fake.token_calls == 0
    assert _row(db)["refresh_lock_owner"] == "other-worker"     # never stolen


def test_expired_lock_is_taken_over():
    db = _db(
        _connected_row(
            expires_at=_iso(NOW - timedelta(seconds=1)),
            refresh_lock_until=_iso(NOW - timedelta(seconds=1)),
            refresh_lock_owner="dead-worker",
        )
    )
    fake = FakeAutodesk()
    clock = Clock()
    token = bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert token == ACCESS_NEW
    assert fake.token_calls == 1
    assert clock.sleeps == []
    assert _row(db)["refresh_lock_owner"] is None


def test_waiter_sees_a_disconnect_while_waiting():
    db = _db(
        _connected_row(
            expires_at=_iso(NOW - timedelta(seconds=1)),
            refresh_lock_until=_iso(NOW + timedelta(seconds=25)),
            refresh_lock_owner="other-worker",
        )
    )
    fake = FakeAutodesk()
    clock = Clock()
    clock.on_sleep = lambda clk, n: _row(db).update(status="disconnected", last_error="rejected upstream")
    with pytest.raises(bc.BcDisconnected) as info:
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert "rejected upstream" in str(info.value)
    assert fake.token_calls == 0


def test_claim_is_the_conditional_update_the_contract_names():
    """The fake models the conditional UPDATE: a live lock held by someone
    else matches nothing, a free or expired one matches the row."""
    held = _db(_connected_row(refresh_lock_until=_iso(NOW + timedelta(seconds=5)), refresh_lock_owner="w"))
    assert bc._claim_refresh(held, NOW) is None
    assert _row(held)["refresh_lock_owner"] == "w"
    free = _db(_connected_row())
    claimed = bc._claim_refresh(free, NOW)
    assert claimed["refresh_lock_owner"] == bc._WORKER_TOKEN
    assert claimed["refresh_lock_until"] == _iso(NOW + timedelta(seconds=30))
    assert _row(free)["refresh_lock_owner"] == bc._WORKER_TOKEN
    assert bc._claim_refresh(free, NOW + timedelta(seconds=1)) is None      # ours, still live
    assert bc._claim_refresh(free, NOW + timedelta(seconds=31)) is not None  # expired
    disconnected = _db(_connected_row(status="disconnected"))
    assert bc._claim_refresh(disconnected, NOW) is None
    assert len(bc._WORKER_TOKEN) == 32


# ── invalid_grant and other refresh failures ────────────────────────────────


def test_invalid_grant_disconnects_rings_the_bell_and_raises(bells):
    db = _db(_connected_row(expires_at=_iso(NOW - timedelta(seconds=1))))
    fake = FakeAutodesk()
    fake.token_mode = "invalid_grant"
    clock = Clock()
    with pytest.raises(bc.BcDisconnected) as info:
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert "reconnect" in str(info.value).lower()
    stored = _row(db)
    assert stored["status"] == "disconnected"
    assert stored["access_token"] is None and stored["refresh_token"] is None and stored["expires_at"] is None
    assert stored["last_error"] == bc._MSG_REFRESH_REJECTED
    assert stored["disconnected_at"] is not None
    assert stored["refresh_lock_until"] is None and stored["refresh_lock_owner"] is None
    assert [(b["role"], b["type"], b["mirror_email"], b["project_id"]) for b in bells] == [
        (Role.IT_ADMIN, "rfp_bc.disconnected", True, None),
        (Role.EXECUTIVE, "rfp_bc.disconnected", True, None),
    ]
    assert all("reconnect" in b["message"].lower() for b in bells)
    assert all(b["metadata"]["provider"] == "buildingconnected" for b in bells)
    assert all(REFRESH_OLD not in json.dumps(b) for b in bells)
    # Everything after is disconnected: no HTTP.
    fake.token_calls = 0
    with pytest.raises(bc.BcDisconnected):
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert fake.token_calls == 0
    assert bc.NOTIFY_DISCONNECTED == "rfp_bc.disconnected"


def test_invalid_grant_inside_the_client_get(bells):
    db = _db(_connected_row())
    fake = FakeAutodesk()
    fake.api_modes = [401]
    fake.token_mode = "invalid_grant"
    clock = Clock()
    with _client(db, fake, clock) as client:
        with pytest.raises(bc.BcDisconnected):
            client.get("/opportunities")
    assert _row(db)["status"] == "disconnected"
    assert [b["role"] for b in bells] == [Role.IT_ADMIN, Role.EXECUTIVE]
    assert [r.method for r in fake.requests] == ["GET", "POST"]


def test_disconnected_bell_is_deduped_while_unread(bells):
    db = _db(
        _connected_row(expires_at=_iso(NOW - timedelta(seconds=1))),
        tables={"notifications": [{"id": "n1", "type": "rfp_bc.disconnected", "read_at": None, "dismissed_at": None}]},
    )
    fake = FakeAutodesk()
    fake.token_mode = "invalid_grant"
    clock = Clock()
    with pytest.raises(bc.BcDisconnected):
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert bells == []
    assert _row(db)["status"] == "disconnected"
    # Once read, the next incident rings again.
    db = _db(
        _connected_row(expires_at=_iso(NOW - timedelta(seconds=1))),
        tables={"notifications": [{"id": "n1", "type": "rfp_bc.disconnected", "read_at": _iso(NOW), "dismissed_at": None}]},
    )
    with pytest.raises(bc.BcDisconnected):
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert len(bells) == 2


def test_bell_failure_never_masks_the_disconnect(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("notifications down")

    monkeypatch.setattr(bc, "notify_role", boom)
    db = _db(_connected_row(expires_at=_iso(NOW - timedelta(seconds=1))))
    fake = FakeAutodesk()
    fake.token_mode = "invalid_grant"
    clock = Clock()
    with pytest.raises(bc.BcDisconnected):
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert _row(db)["status"] == "disconnected"


@pytest.mark.parametrize("mode, exc_type", [("500", bc.BcTransient), ("transport", bc.BcTransient), ("invalid_client", bc.BcPermanent)])
def test_other_refresh_failures_release_the_lock_and_keep_the_tokens(mode, exc_type, bells):
    db = _db(_connected_row(expires_at=_iso(NOW - timedelta(seconds=1))))
    fake = FakeAutodesk()
    fake.token_mode = mode
    clock = Clock()
    with pytest.raises(exc_type):
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    stored = _row(db)
    assert stored["status"] == "connected"
    assert stored["refresh_token"] == REFRESH_OLD and stored["access_token"] == ACCESS_OLD
    assert stored["refresh_lock_until"] is None and stored["refresh_lock_owner"] is None
    assert bells == []


def test_refresh_without_a_refresh_token_is_disconnected():
    db = _db(_connected_row(expires_at=_iso(NOW - timedelta(seconds=1)), refresh_token=None))
    fake = FakeAutodesk()
    clock = Clock()
    with pytest.raises(bc.BcDisconnected):
        bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    assert fake.token_calls == 0
    assert _row(db)["refresh_lock_owner"] is None


# ── Secrets never reach a log line ──────────────────────────────────────────


def test_no_secret_reaches_a_log_line(caplog, bells):
    db = _db(_connected_row(expires_at=_iso(NOW - timedelta(seconds=1))))
    fake = FakeAutodesk()
    fake.api_modes = [(429, "1"), 500, "ok"]
    clock = Clock()
    with caplog.at_level(logging.DEBUG, logger="app.services.bc_client"):
        with _client(db, fake, clock) as client:
            client.get("/opportunities", {"limit": 3})
        fake.token_mode = "invalid_grant"
        _row(db).update(expires_at=_iso(NOW - timedelta(seconds=1)))
        with pytest.raises(bc.BcDisconnected):
            bc.access_token(db, _config(), transport=httpx.MockTransport(fake), sleep=clock.sleep, now=clock.now)
    text = caplog.text
    assert text                                     # something was logged
    for secret in (CLIENT_SECRET, ACCESS_OLD, ACCESS_NEW, REFRESH_OLD, REFRESH_NEW, "Basic "):
        assert secret not in text, secret
    assert "GET /opportunities -> 429" in text
    assert "POST /token -> 200" in text
    assert "token refreshed" in text
