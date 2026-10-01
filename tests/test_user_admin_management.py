"""Full account management for the IT Admin / Executive: rename, move the login
email, send a password reset, and delete an account.

The two guardrails matter as much as the features: the same two roles hold the
only keys to this surface, so an admin may not disable or delete themselves, and
the last working admin may not be deleted, disabled or demoted. Losing that
account would 403 the Users page for everyone with no in-app way back.

Fast unit tests: the Supabase client is stubbed with an in-memory `profiles`
table (filters are applied for real, so the guard queries are actually
exercised) and the handlers are called directly.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.roles import Role
from app.models.schemas import AdminUpdateUserIn
from app.routers import users
from app.services import graph_email, invite_email


# ── Supabase stub ──────────────────────────────────────────────────────────


class _Query:
    """Enough of the PostgREST builder to run the router's real filters."""

    def __init__(self, store):
        self._store = store
        self._op = "select"
        self._patch: dict | None = None
        self._filters: list[tuple[str, str, object]] = []

    def select(self, *a, **k):
        self._op = "select"
        return self

    def update(self, patch):
        self._op, self._patch = "update", patch
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def _matches(self, row) -> bool:
        for kind, col, val in self._filters:
            actual = row.get(col)
            if kind == "eq" and actual != val:
                return False
            if kind == "neq" and actual == val:
                return False
            if kind == "in" and actual not in val:
                return False
        return True

    def execute(self):
        rows = [r for r in self._store["profiles"].values() if self._matches(r)]
        if self._op == "update":
            if self._store.get("update_fails"):
                raise RuntimeError("profiles update blew up")
            for r in rows:
                r.update(self._patch or {})
            return SimpleNamespace(data=[dict(r) for r in rows])
        if self._op == "delete":
            for r in rows:
                self._store["profiles"].pop(r["id"], None)
                self._store["profile_deletes"].append(r["id"])
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[dict(r) for r in rows])


class _Admin:
    def __init__(self, store):
        self._store = store

    def update_user_by_id(self, uid, attributes):
        if self._store.get("auth_email_fails"):
            raise RuntimeError("gotrue rejected the address")
        self._store["auth_email_updates"].append((uid, attributes))

    def delete_user(self, uid, should_soft_delete=False):
        if self._store.get("auth_delete_fails"):
            raise RuntimeError("gotrue is down")
        self._store["auth_deleted"].append(uid)
        # Real GoTrue deletes auth.users, which cascades the profile row.
        self._store["profiles"].pop(uid, None)

    def generate_link(self, params):
        self._store["generate_link"].append(params)
        return SimpleNamespace(
            user=SimpleNamespace(id="newid"),
            properties=SimpleNamespace(
                hashed_token="hash123", verification_type=params["type"]
            ),
        )


class _SB:
    def __init__(self, store):
        self._store = store
        self.auth = SimpleNamespace(
            admin=_Admin(store),
            reset_password_email=lambda email, options=None: store[
                "supabase_resets"
            ].append((email, options)),
        )

    def table(self, name):
        return _Query(self._store)

    def rpc(self, name, params):
        self._store["rpcs"].append((name, params))
        if self._store.get("rpc_fails"):
            raise RuntimeError("rpc blew up")
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=1))


def _profile(uid, **over):
    base = {
        "id": uid,
        "full_name": "Pat Doe",
        "email": f"{uid}@g3.com",
        "role": "estimating_admin",
        "is_active": True,
        "is_dev": False,
        "mfa_enrolled": False,
        "invite_accepted_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(over)
    return base


def _setup(monkeypatch, *rows, graph=True, send_raises=False):
    store = {
        "profiles": {r["id"]: dict(r) for r in rows},
        "auth_email_updates": [],
        "auth_deleted": [],
        "profile_deletes": [],
        "generate_link": [],
        "supabase_resets": [],
        "sent": [],
        "audits": [],
        "rpcs": [],
        "graph_mail": [],
    }
    monkeypatch.setattr(users, "get_supabase", lambda: _SB(store))
    monkeypatch.setattr(
        users, "audit", lambda *a, **k: store["audits"].append((a, k))
    )
    monkeypatch.setattr(invite_email, "graph_configured", lambda: graph)

    def _fake_send(**kwargs):
        store["sent"].append(kwargs)
        if send_raises:
            raise RuntimeError("graph down")

    monkeypatch.setattr(invite_email, "send_password_reset_email", _fake_send)
    # The old-address notice on an email change goes through Graph directly;
    # never let a test reach the real send.
    monkeypatch.setattr(
        graph_email, "send_mail", lambda **k: store["graph_mail"].append(k)
    )
    return store


_ADMIN = SimpleNamespace(id="admin1")


# ── Rename + email change ──────────────────────────────────────────────────


def test_admin_renames_a_user_and_moves_their_login_email(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", full_name="Pat Doe"))
    out = users.update_user(
        user_id="u1",
        body=AdminUpdateUserIn(full_name="  Pat Rivera  ", email="Pat.Rivera@G3.com"),
        admin=_ADMIN,
    )
    assert out["full_name"] == "Pat Rivera"  # trimmed by the schema validator
    # Lowercased to match what GoTrue actually stores, and confirmed on the spot
    # so the user can sign in with it immediately.
    assert out["email"] == "pat.rivera@g3.com"
    assert store["auth_email_updates"] == [
        ("u1", {"email": "pat.rivera@g3.com", "email_confirm": True})
    ]


def test_email_change_is_refused_when_another_user_holds_it(monkeypatch):
    store = _setup(
        monkeypatch, _profile("u1"), _profile("u2", email="taken@g3.com")
    )
    with pytest.raises(HTTPException) as ei:
        users.update_user(
            user_id="u1", body=AdminUpdateUserIn(email="taken@g3.com"), admin=_ADMIN
        )
    assert ei.value.status_code == 409
    # Refused before GoTrue was touched, so no half-applied change.
    assert store["auth_email_updates"] == []
    assert store["profiles"]["u1"]["email"] == "u1@g3.com"


def test_resubmitting_the_same_email_is_not_a_change(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", email="pat@g3.com"))
    with pytest.raises(HTTPException) as ei:
        users.update_user(
            user_id="u1", body=AdminUpdateUserIn(email="PAT@g3.com"), admin=_ADMIN
        )
    assert ei.value.status_code == 400  # "Nothing to update"
    assert store["auth_email_updates"] == []


def test_auth_rejection_leaves_the_profile_untouched(monkeypatch):
    store = _setup(monkeypatch, _profile("u1"))
    store["auth_email_fails"] = True
    with pytest.raises(HTTPException) as ei:
        users.update_user(
            user_id="u1", body=AdminUpdateUserIn(email="new@g3.com"), admin=_ADMIN
        )
    assert ei.value.status_code == 400
    assert store["profiles"]["u1"]["email"] == "u1@g3.com"


def test_failed_profile_write_rolls_the_auth_email_back(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", email="old@g3.com"))
    store["update_fails"] = True
    with pytest.raises(RuntimeError):
        users.update_user(
            user_id="u1", body=AdminUpdateUserIn(email="new@g3.com"), admin=_ADMIN
        )
    # Moved, then put back, so the user is never left signing in with an address
    # their profile doesn't know about.
    assert [e["email"] for _, e in store["auth_email_updates"]] == [
        "new@g3.com",
        "old@g3.com",
    ]


def test_query_params_still_work_and_the_body_wins(monkeypatch):
    store = _setup(monkeypatch, _profile("u1"), _profile("a2", role="it_admin"))
    # Legacy wire format: role as a query param, no body at all.
    out = users.update_user(user_id="u1", role=Role.ACCOUNTANT, admin=_ADMIN)
    assert out["role"] == "accountant"
    # Both supplied, and the body is the newer, authoritative one.
    out = users.update_user(
        user_id="u1",
        body=AdminUpdateUserIn(role=Role.EXECUTIVE),
        role=Role.ACCOUNTANT,
        admin=_ADMIN,
    )
    assert out["role"] == "executive"
    assert store["profiles"]["u1"]["role"] == "executive"


# ── Self-protection + last-admin protection ────────────────────────────────


def test_admin_cannot_disable_or_delete_their_own_account(monkeypatch):
    _setup(monkeypatch, _profile("admin1", role="it_admin"), _profile("a2", role="it_admin"))
    with pytest.raises(HTTPException) as ei:
        users.update_user(
            user_id="admin1", body=AdminUpdateUserIn(is_active=False), admin=_ADMIN
        )
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="admin1", admin=_ADMIN)
    assert ei.value.status_code == 403


@pytest.mark.parametrize(
    "call",
    [
        lambda: users.delete_user(user_id="u1", admin=_ADMIN),
        lambda: users.update_user(
            user_id="u1", body=AdminUpdateUserIn(is_active=False), admin=_ADMIN
        ),
        lambda: users.update_user(
            user_id="u1", body=AdminUpdateUserIn(role=Role.ACCOUNTANT), admin=_ADMIN
        ),
    ],
    ids=["delete", "disable", "demote"],
)
def test_the_last_account_that_can_manage_users_is_protected(monkeypatch, call):
    # u1 is the only enabled IT Admin/Executive: a disabled admin and a writer
    # who cannot administer accounts do not count as cover.
    store = _setup(
        monkeypatch,
        _profile("u1", role="it_admin"),
        _profile("u2", role="executive", is_active=False),
        _profile("u3", role="estimating_admin"),
    )
    with pytest.raises(HTTPException) as ei:
        call()
    assert ei.value.status_code == 400
    assert "last account" in ei.value.detail
    assert store["profiles"]["u1"]["is_active"] is True
    assert store["auth_deleted"] == []


def test_an_admin_can_be_removed_while_another_one_remains(monkeypatch):
    store = _setup(
        monkeypatch,
        _profile("u1", role="it_admin"),
        _profile("u2", role="executive"),
    )
    users.delete_user(user_id="u1", admin=_ADMIN)
    assert store["auth_deleted"] == ["u1"]
    assert "u1" not in store["profiles"]


def test_a_non_admin_is_never_blocked_by_the_coverage_guard(monkeypatch):
    # No admin exists at all; deleting an ordinary user must still work.
    store = _setup(monkeypatch, _profile("u1", role="estimating_admin"))
    users.delete_user(user_id="u1", admin=_ADMIN)
    assert store["auth_deleted"] == ["u1"]


# ── Delete ─────────────────────────────────────────────────────────────────


def test_delete_audits_the_email_and_role_it_is_about_to_destroy(monkeypatch):
    store = _setup(
        monkeypatch,
        _profile("u1", email="gone@g3.com", role="accountant"),
        _profile("a2", role="it_admin"),
    )
    users.delete_user(user_id="u1", admin=_ADMIN)
    (actor, action, entity, entity_id, meta), _ = store["audits"][-1]
    assert (actor, action, entity_id) == ("admin1", "user.delete", "u1")
    assert meta == {"email": "gone@g3.com", "role": "accountant"}


def test_a_failed_auth_delete_keeps_the_profile_and_writes_no_audit(monkeypatch):
    store = _setup(monkeypatch, _profile("u1"), _profile("a2", role="it_admin"))
    store["auth_delete_fails"] = True
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="u1", admin=_ADMIN)
    assert ei.value.status_code == 502
    assert "u1" in store["profiles"]
    assert store["audits"] == []


def test_deleting_an_unknown_user_is_a_404(monkeypatch):
    _setup(monkeypatch, _profile("u1"))
    with pytest.raises(HTTPException) as ei:
        users.delete_user(user_id="nope", admin=_ADMIN)
    assert ei.value.status_code == 404


# ── Password reset ─────────────────────────────────────────────────────────


def test_reset_password_sends_a_branded_recovery_link(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", email="pat@g3.com", full_name="Pat"))
    users.reset_user_password(user_id="u1", admin=_ADMIN)
    assert store["generate_link"][0]["type"] == "recovery"
    assert store["generate_link"][0]["email"] == "pat@g3.com"
    sent = store["sent"][0]
    assert sent["to"] == "pat@g3.com"
    assert "token_hash=hash123" in sent["cta_url"]
    assert "type=recovery" in sent["cta_url"]
    # Lands on the set-a-new-password screen, not the accept-invite one.
    assert "next=%2Fauth%2Freset-password" in sent["cta_url"]


def test_reset_password_is_refused_before_the_invite_is_accepted(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", invite_accepted_at=None))
    with pytest.raises(HTTPException) as ei:
        users.reset_user_password(user_id="u1", admin=_ADMIN)
    assert ei.value.status_code == 400
    assert store["sent"] == []


def test_reset_password_falls_back_to_supabase_without_graph(monkeypatch):
    store = _setup(monkeypatch, _profile("u1", email="pat@g3.com"), graph=False)
    users.reset_user_password(user_id="u1", admin=_ADMIN)
    assert store["supabase_resets"][0][0] == "pat@g3.com"
    assert store["sent"] == []  # no branded Graph send
    assert store["generate_link"] == []


def test_reset_password_surfaces_a_send_failure(monkeypatch):
    store = _setup(monkeypatch, _profile("u1"), send_raises=True)
    with pytest.raises(HTTPException) as ei:
        users.reset_user_password(user_id="u1", admin=_ADMIN)
    assert ei.value.status_code == 502
    assert store["audits"] == []  # nothing recorded for an email that never went


# ── RFP mailboxes (docs/RFP_EMAIL_VISIBILITY.md 3.5) ───────────────────────
# Which RFP review-queue rows a person sees is decided by the mailboxes mapped
# onto their profile here, so this field is the whole access-control surface of
# that page: it normalizes hard and refuses the external estimator outright.


def test_admin_maps_rfp_mailboxes_onto_a_user(monkeypatch):
    store = _setup(monkeypatch, _profile("u1"))
    out = users.update_user(
        user_id="u1",
        body=AdminUpdateUserIn(
            rfp_mailboxes=["  TMoore@G3Electrical.com ", "tmoore@g3electrical.com", ""]
        ),
        admin=_ADMIN,
    )
    # Trimmed, lowercased, blanks dropped and deduped by the schema.
    assert out["rfp_mailboxes"] == ["tmoore@g3electrical.com"]
    # Audited in the ordinary user.update patch, so the mapping has a trail.
    ((args, _),) = [a for a in store["audits"] if a[0][1] == "user.update"]
    assert args[4] == {"rfp_mailboxes": ["tmoore@g3electrical.com"]}


def test_an_empty_list_clears_the_mapping(monkeypatch):
    _setup(monkeypatch, _profile("u1", rfp_mailboxes=["tmoore@g3electrical.com"]))
    out = users.update_user(
        user_id="u1", body=AdminUpdateUserIn(rfp_mailboxes=[]), admin=_ADMIN
    )
    assert out["rfp_mailboxes"] == []


def test_leaving_the_field_out_does_not_touch_the_mapping(monkeypatch):
    _setup(monkeypatch, _profile("u1", rfp_mailboxes=["tmoore@g3electrical.com"]))
    out = users.update_user(
        user_id="u1", body=AdminUpdateUserIn(full_name="Pat Rivera"), admin=_ADMIN
    )
    assert out["rfp_mailboxes"] == ["tmoore@g3electrical.com"]


def test_an_external_estimator_is_refused_a_mailbox(monkeypatch):
    _setup(monkeypatch, _profile("u1", role="estimator"))
    with pytest.raises(HTTPException) as exc:
        users.update_user(
            user_id="u1",
            body=AdminUpdateUserIn(rfp_mailboxes=["tmoore@g3electrical.com"]),
            admin=_ADMIN,
        )
    assert exc.value.status_code == 400
    assert exc.value.headers["X-Error-Code"] == "rfp_mailboxes_internal_only"


def test_the_refusal_reads_the_role_the_patch_leaves_the_user_in(monkeypatch):
    """Demoting someone to estimator and granting a mailbox in one call must
    be refused whole, not half-applied."""
    store = _setup(monkeypatch, _profile("u1"), _profile("u2", role="it_admin"))
    with pytest.raises(HTTPException) as exc:
        users.update_user(
            user_id="u1",
            body=AdminUpdateUserIn(
                role=Role.ESTIMATOR, rfp_mailboxes=["tmoore@g3electrical.com"]
            ),
            admin=_ADMIN,
        )
    assert exc.value.status_code == 400
    assert store["profiles"]["u1"]["role"] == "estimating_admin"
    # The mirror case: promoting an estimator and mapping a mailbox is allowed.
    _setup(monkeypatch, _profile("u3", role="estimator"), _profile("u4", role="it_admin"))
    out = users.update_user(
        user_id="u3",
        body=AdminUpdateUserIn(
            role=Role.ACCOUNTANT, rfp_mailboxes=["tiesha@g3electrical.com"]
        ),
        admin=_ADMIN,
    )
    assert out["rfp_mailboxes"] == ["tiesha@g3electrical.com"]


@pytest.mark.parametrize(
    "value",
    [
        ["not-an-address"],
        ["two words@g3electrical.com"],
        ["no-domain@g3electrical"],
        [f"m{i}@g3electrical.com" for i in range(11)],   # eleven is over the cap
    ],
)
def test_malformed_or_oversized_mailbox_lists_never_reach_the_router(value):
    with pytest.raises(ValueError):
        AdminUpdateUserIn(rfp_mailboxes=value)


def test_ten_addresses_are_still_accepted():
    body = AdminUpdateUserIn(rfp_mailboxes=[f"m{i}@g3electrical.com" for i in range(10)])
    assert len(body.rfp_mailboxes) == 10


def test_profile_out_carries_the_mapping_and_defaults_to_empty():
    """GET /users and GET /users/me both answer ProfileOut, and both degrade
    gracefully on a database where 0134 has not been applied yet."""
    from app.models.schemas import ProfileOut

    base = _profile("u1")
    assert ProfileOut(**base).rfp_mailboxes == []
    mapped = ProfileOut(**_profile("u2", rfp_mailboxes=["tmoore@g3electrical.com"]))
    assert mapped.rfp_mailboxes == ["tmoore@g3electrical.com"]


@pytest.mark.parametrize(
    "value",
    [
        "a,tmoore@g3electrical.com",   # a comma would separate the ov.{} literal
        'a"b@g3electrical.com',
        "a'b@g3electrical.com",
        "a{b@g3electrical.com",
        "a}b@g3electrical.com",
        "a\\b@g3electrical.com",
        "a\tb@g3electrical.com",
    ],
)
def test_a_value_that_could_corrupt_the_array_literal_is_refused(value):
    """These reach PostgREST as `mailboxes=ov.{a,b}`, built by joining the
    values raw, so a separator character inside one value would widen the
    scope of every list query the holder makes."""
    with pytest.raises(ValueError):
        AdminUpdateUserIn(rfp_mailboxes=[value])


def test_demoting_to_estimator_clears_the_mapping_in_the_same_patch(monkeypatch):
    store = _setup(
        monkeypatch,
        _profile("u1", rfp_mailboxes=["tmoore@g3electrical.com"]),
        _profile("u2", role="it_admin"),
    )
    out = users.update_user(user_id="u1", body=AdminUpdateUserIn(role=Role.ESTIMATOR),
                            admin=_ADMIN)
    assert out["role"] == "estimator" and out["rfp_mailboxes"] == []
    ((args, _),) = [a for a in store["audits"] if a[0][1] == "user.update"]
    assert args[4]["rfp_mailboxes"] == []


def test_a_role_change_between_internal_roles_leaves_the_mapping_alone(monkeypatch):
    _setup(monkeypatch, _profile("u1", rfp_mailboxes=["tmoore@g3electrical.com"]),
           _profile("u2", role="it_admin"))
    out = users.update_user(user_id="u1", body=AdminUpdateUserIn(role=Role.ACCOUNTANT),
                            admin=_ADMIN)
    assert out["rfp_mailboxes"] == ["tmoore@g3electrical.com"]


def test_demoting_a_user_who_owns_nothing_writes_no_mapping_key(monkeypatch):
    """Nothing to clear means nothing in the patch, so "Nothing to update"
    stays reachable and the audit does not gain a noise field."""
    store = _setup(monkeypatch, _profile("u1"), _profile("u2", role="it_admin"))
    users.update_user(user_id="u1", body=AdminUpdateUserIn(role=Role.ESTIMATOR), admin=_ADMIN)
    ((args, _),) = [a for a in store["audits"] if a[0][1] == "user.update"]
    assert "rfp_mailboxes" not in args[4]
