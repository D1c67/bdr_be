"""Browser v2 repo layer (docs/ASSISTANT_BROWSER_V2_PLAN.md, WI-3) and the
FakeDB twins of the 0192 site RPCs it wraps.

Invariants pinned here:

  convention  every function of the browser v2 block takes user_id first;
              the block sits before the phase 7 marker (that test pins the
              public functions from the marker to the end of the file);
              system sweeps accept only None; RPC wrappers pass exactly the
              arguments the 0192 SQL declares; money travels as strings;
              no em dash;
  reads       the v2 type row, the registry, grants, credentials re-keyed by
              site and account label, owner scoping on every owner read,
              session reads without cookie columns;
  rpcs        site grant (ad hoc insert, one active grant, request approval,
              waiting admissions, refusals), revoke cascade, scope extend;
  sources     the URL provenance lookups (RFP harvest, RFP projects, PM
              links, project fields, RFI sync links).
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from app.assistant import repo
from app.core import supabase_client
from tests.assistant_fakes import (
    BV2_ADMISSION,
    BV2_JOB,
    BV2_USER2,
    CONV,
    NOW,
    PROJECT,
    PROJECT2,
    USER,
    FakeDB,
    admission_row,
    bv2_job_row,
    credential_v2_row,
    curated_site_rows,
    grant_row,
    site_row,
)

SQL = (Path(__file__).resolve().parents[1] / "supabase/migrations/0192_assistant_browser_v2.sql").read_text(
    encoding="utf-8")
REPO_SOURCE = Path(repo.__file__).read_text(encoding="utf-8")
MARKER = "# ── Browser v2: sites, grants, admissions, credentials"
P7_MARKER = "# ── Phase 7: the proactive router"
START = REPO_SOURCE[: REPO_SOURCE.index(MARKER)].count("\n") + 1
END = REPO_SOURCE[: REPO_SOURCE.index(P7_MARKER)].count("\n") + 1
BLOCK = REPO_SOURCE[REPO_SOURCE.index(MARKER): REPO_SOURCE.index(P7_MARKER)]
OTHER = "fafafafa-fafa-4faf-8faf-fafafafafafa"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(now=NOW)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def _bv2_functions() -> dict[str, object]:
    tree = ast.parse(REPO_SOURCE)
    return {n.name: getattr(repo, n.name) for n in tree.body
            if isinstance(n, ast.FunctionDef) and START <= n.lineno < END}


SYSTEM_SWEEPS = {
    "browser_sweep_v2": lambda u: repo.browser_sweep_v2(u),
    "browser_purge_v2": lambda u: repo.browser_purge_v2(u),
    "browser_outbox_drain": lambda u: repo.browser_outbox_drain(u),
    "browser_outbox_ack": lambda u: repo.browser_outbox_ack(u, [1, 2]),
}


# ── Conventions ──────────────────────────────────────────────────────────────


def test_the_block_sits_before_the_phase7_block():
    assert REPO_SOURCE.count(MARKER) == 1
    assert REPO_SOURCE.index(MARKER) < REPO_SOURCE.index(P7_MARKER)
    assert len(_bv2_functions()) >= 45


def test_every_block_function_takes_user_id_first():
    for name, fn in _bv2_functions().items():
        if name.startswith("_bv2_"):
            continue
        params = list(inspect.signature(fn).parameters)
        assert params and params[0] == "user_id", name


def test_system_sweeps_are_exactly_the_optional_user_functions():
    optional = {n for n, fn in _bv2_functions().items() if not n.startswith("_")
                and "None" in str(next(iter(inspect.signature(fn).parameters.values())).annotation)}
    assert optional == set(SYSTEM_SWEEPS)
    for name in optional:
        assert "system sweep" in (getattr(repo, name).__doc__ or "").lower(), name


@pytest.mark.parametrize("name", sorted(SYSTEM_SWEEPS))
def test_system_sweeps_accept_only_none(db, name):
    with pytest.raises(ValueError, match="system sweep"):
        SYSTEM_SWEEPS[name](USER)
    assert db.calls == [] and db.rpc_calls == []
    rpc = {"browser_sweep_v2": repo.BV2_SWEEP_RPC, "browser_purge_v2": repo.BV2_PURGE_RPC,
           "browser_outbox_drain": repo.BV2_OUTBOX_DRAIN_RPC, "browser_outbox_ack": repo.BV2_OUTBOX_ACK_RPC}[name]
    db.bv2_rpc_results[rpc] = (lambda p: 2) if name == "browser_outbox_ack" else {"ok": True}
    SYSTEM_SWEEPS[name](None)
    assert db.rpc_calls and db.rpc_calls[-1][0] == rpc


def test_outbox_ack_with_no_ids_never_calls(db):
    assert repo.browser_outbox_ack(None, []) == 0
    assert db.rpc_calls == []


def test_no_em_dash_in_the_block():
    assert chr(0x2014) not in BLOCK


def _sql_params() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for match in re.finditer(r"create or replace function public\.(\w+)\((.*?)\)\s*returns", SQL, re.S):
        out[match.group(1)] = [chunk.split()[0] for chunk in re.split(r",\s*(?=p_)", " ".join(match.group(2).split()))
                               if chunk.strip()]
    return out


RPC_CALLS = {
    repo.BV2_SITE_GRANT_RPC: lambda: repo.browser_site_grant_put(
        USER, "procore", scope="domain", value="procore.com", path_prefix=None, granted_via="chat_card",
        provenance="typed"),
    repo.BV2_SITE_REVOKE_RPC: lambda: repo.browser_site_revoke(USER, OTHER, 1),
    repo.BV2_SCOPE_EXTEND_RPC: lambda: repo.browser_scope_extend(USER, BV2_JOB, "procore"),
    repo.BV2_ADMISSION_CREATE_RPC: lambda: repo.browser_admission_create(USER, {"goal": "g"}),
    repo.BV2_START_DECIDE_RPC: lambda: repo.browser_start_decide(USER, BV2_ADMISSION, "approve"),
    repo.BV2_REQUEST_CREATE_RPC: lambda: repo.browser_request_create(USER, {"kind": "credential"}),
    repo.BV2_REQUEST_DECIDE_RPC: lambda: repo.browser_request_decide(USER, OTHER, "decline"),
    repo.BV2_ENQUEUE_RPC: lambda: repo.browser_task_enqueue_v2(
        USER, BV2_ADMISSION, {"goal": "g"}, user_cap_default=15, global_cap_default="50"),
    repo.BV2_CREDENTIAL_PUT_RPC: lambda: repo.browser_credential_put_v2(USER, "procore", "default", 0, {"a": 1}),
    repo.BV2_CREDENTIAL_REVOKE_RPC: lambda: repo.browser_credential_revoke_v2(USER, OTHER, 1),
    repo.BV2_HELP_REPLY_RPC: lambda: repo.browser_help_reply_v2(USER, BV2_JOB, OTHER, OTHER, "code", {}),
    repo.BV2_HELP_FROM_MAIL_RPC: lambda: repo.browser_help_secret_from_mail(USER, BV2_JOB, OTHER, "a" * 64, {}),
    repo.BV2_HOLD_DECIDE_RPC: lambda: repo.browser_hold_decide(USER, OTHER, "approve", token="t", token_exp=NOW),
    repo.BV2_SAFE_RULE_ADD_RPC: lambda: repo.browser_safe_rule_add(
        USER, OTHER, method="POST", host="app.procore.com", path_pattern="^/x$"),
    repo.BV2_SAFE_RULE_REMOVE_RPC: lambda: repo.browser_safe_rule_remove(USER, OTHER),
    repo.BV2_SESSION_SIGNOUT_RPC: lambda: repo.browser_session_signout(USER, OTHER),
    repo.BV2_LIVE_OPEN_RPC: lambda: repo.browser_live_session_open(USER, BV2_JOB, "procore", "default"),
    repo.BV2_LIVE_CLOSE_RPC: lambda: repo.browser_live_session_close(USER, OTHER, "ok"),
    repo.BV2_SWEEP_RPC: lambda: repo.browser_sweep_v2(None),
    repo.BV2_PURGE_RPC: lambda: repo.browser_purge_v2(None),
    repo.BV2_OUTBOX_DRAIN_RPC: lambda: repo.browser_outbox_drain(None),
    repo.BV2_OUTBOX_ACK_RPC: lambda: repo.browser_outbox_ack(None, [3]),
}


@pytest.mark.parametrize("name", sorted(RPC_CALLS))
def test_rpc_wrappers_pass_exactly_the_declared_arguments(db, name):
    declared = _sql_params()
    assert name in declared, name
    db.bv2_rpc_results[name] = (lambda p: 1) if name == repo.BV2_OUTBOX_ACK_RPC else {"ok": True}
    RPC_CALLS[name]()
    called, params = db.rpc_calls[-1]
    assert called == name
    assert set(params) <= set(declared[name]), set(params) - set(declared[name])
    sql_defaults = {p for p in declared[name] if p not in params}
    signature = re.search(rf"function public\.{name}\((.*?)\)\s*returns", SQL, re.S).group(1)
    for missing in sql_defaults:
        assert re.search(rf"{missing}\s+\S+(\[\])?\s+default", signature), (name, missing)


def test_enqueue_money_travels_as_strings(db):
    db.bv2_rpc_results[repo.BV2_ENQUEUE_RPC] = {"ok": True}
    repo.browser_task_enqueue_v2(USER, BV2_ADMISSION, {}, user_cap_default=15, global_cap_default=50.5)
    params = db.rpc_calls[-1][1]
    assert params["p_user_cap_default"] == "15" and params["p_global_cap_default"] == "50.5"


# ── Reads ────────────────────────────────────────────────────────────────────


def test_platform_config_v2_reads_the_user_browser_type(db):
    db.tables.update(browser_agent_types=[{"key": "user_browser_read", "enabled": False},
                                          {"key": "user_browser", "enabled": False, "max_concurrency": 4}],
                     browser_agent_settings=[{"id": 1, "user_per_job_cost_cap_usd": "5.00"}])
    kind, settings = repo.browser_platform_config_v2(USER)
    assert kind["key"] == "user_browser" and kind["max_concurrency"] == 4
    assert settings["user_per_job_cost_cap_usd"] == "5.00"
    assert repo.browser_platform_config(USER)[0]["key"] == "user_browser_read"


def test_platform_config_v2_empty_tables(db):
    assert repo.browser_platform_config_v2(USER) == ({}, {})


def test_registry_reads(db):
    db.tables["assistant_browser_sites"] = curated_site_rows() + [
        site_row(site_key="domain:example.com", label="example.com", kind="ad_hoc",
                 registrable_domains=["example.com"]),
        site_row(site_key="domain:off.com", label="off.com", kind="ad_hoc", registrable_domains=["off.com"],
                 enabled=False),
    ]
    keys = [r["site_key"] for r in repo.browser_sites(USER)]
    assert "domain:off.com" not in keys and keys[:4] == ["autodesk", "buildingconnected", "procore", "smartbid"]
    assert "domain:off.com" in [r["site_key"] for r in repo.browser_sites(USER, include_disabled=True)]
    assert [r["site_key"] for r in repo.browser_sites(USER, kind="ad_hoc")] == ["domain:example.com"]
    assert repo.browser_site(USER, "domain:off.com")["enabled"] is False
    assert repo.browser_site(USER, "nope") is None
    assert set(repo.browser_sites_by_keys(USER, ["procore", "smartbid", "nope"])) == {"procore", "smartbid"}
    assert repo.browser_sites_by_keys(USER, []) == {}


def test_sites_for_url_bounds_the_read(db):
    db.tables["assistant_browser_sites"] = curated_site_rows() + [
        site_row(site_key="domain:example.com", kind="ad_hoc", registrable_domains=["example.com"]),
        site_row(site_key="domain:other.com", kind="ad_hoc", registrable_domains=["other.com"]),
        site_row(site_key="origin:x.github.io", kind="ad_hoc", registrable_domains=[],
                 exact_origins=["https://x.github.io"]),
    ]
    keys = {r["site_key"] for r in repo.browser_sites_for_url(
        USER, origin="https://www.example.com", registrable_domain="example.com")}
    assert keys == {"procore", "autodesk", "buildingconnected", "smartbid", "domain:example.com"}
    keys = {r["site_key"] for r in repo.browser_sites_for_url(
        USER, origin="https://x.github.io", registrable_domain="x.github.io")}
    assert "origin:x.github.io" in keys and "domain:other.com" not in keys


def test_grant_reads_are_owner_scoped(db):
    mine = grant_row(id=OTHER)
    db.tables["assistant_browser_site_grants"] = [
        mine,
        grant_row(user_id=BV2_USER2),
        grant_row(site_key="smartbid", value="smartbidnet.com", status="revoked"),
    ]
    assert [g["id"] for g in repo.browser_site_grants(USER)] == [OTHER]
    assert len(repo.browser_site_grants(USER, status=None)) == 2
    assert repo.browser_site_grant(USER, "procore")["id"] == OTHER
    assert repo.browser_site_grant(USER, "smartbid") is None
    assert repo.browser_site_grant_by_id(BV2_USER2, OTHER) is None
    assert repo.browser_site_grant_by_id(USER, OTHER)["id"] == OTHER


def test_credential_v2_is_keyed_by_site_and_label(db):
    first = credential_v2_row(account_label="Work", created_at="2026-10-01T00:00:00+00:00")
    default = credential_v2_row(account_label="Main", is_default=True, created_at="2026-10-02T00:00:00+00:00")
    revoked = credential_v2_row(site_key="smartbid", account_label="default", status="revoked")
    stranger = credential_v2_row(user_id=BV2_USER2, account_label="Main", is_default=True)
    db.tables["assistant_credentials"] = [first, default, revoked, stranger]
    assert repo.browser_credential_v2(USER, "procore", "Work")["id"] == first["id"]
    assert repo.browser_credential_v2(USER, "procore")["id"] == default["id"]
    default["is_default"] = False
    assert repo.browser_credential_v2(USER, "procore")["id"] == first["id"]
    assert repo.browser_credential_v2(USER, "smartbid")["id"] == revoked["id"]
    assert repo.browser_credential_v2(USER, "smartbid", active_only=True) is None
    assert repo.browser_credential_v2(USER, "procore", "Nope") is None
    assert {r["id"] for r in repo.browser_credentials_for_site(USER, "procore")} == {first["id"], default["id"]}
    assert repo.browser_credential_v2(BV2_USER2, "procore")["id"] == stranger["id"]


def test_credentials_v2_all_returns_every_row_past_20(db):
    """The password guard, the reuse check and Settings read every row
    (review finding: the v1 read stopped at 20, ordered by legacy service)."""
    mine = [credential_v2_row(site_key=f"domain:site{i:02d}.example.com", status="revoked" if i % 3 else "active",
                              fingerprint=f"fp{i}") for i in range(25)]
    stranger = credential_v2_row(user_id=BV2_USER2, fingerprint="theirs")
    db.tables["assistant_credentials"] = [*mine, stranger]
    rows = repo.browser_credentials_v2_all(USER)
    assert {r["id"] for r in rows} == {r["id"] for r in mine}
    assert repo.BV2_CREDENTIAL_LIST_MAX >= 500
    for column in ("wrapped_dek", "ciphertext", "nonce", "envelope"):
        assert column not in repo.BV2_CREDENTIAL_LIST_COLUMNS.split(",")
    assert "fingerprint" in repo.BV2_CREDENTIAL_LIST_COLUMNS.split(",")


def test_session_reads_have_no_cookie_columns(db):
    assert "cookie" not in repo.BV2_SESSION_COLUMNS and "ciphertext" not in repo.BV2_SESSION_COLUMNS
    db.tables["assistant_browser_sessions"] = [
        {"id": OTHER, "user_id": USER, "site_key": "procore", "account_label": "default",
         "origin": "https://app.procore.com", "created_at": NOW.isoformat()},
        {"id": BV2_JOB, "user_id": BV2_USER2, "site_key": "procore", "created_at": NOW.isoformat()},
    ]
    assert [r["id"] for r in repo.browser_sessions_v2(USER)] == [OTHER]
    assert repo.browser_sessions_v2(USER, site_key="smartbid") == []


def test_admission_request_hold_reads_are_owner_scoped(db):
    later = (NOW.replace(year=NOW.year + 1)).isoformat()
    db.tables.update(
        assistant_browser_admissions=[admission_row(), admission_row(id=OTHER, user_id=BV2_USER2),
                                      admission_row(id=BV2_JOB, status="enqueued")],
        assistant_browser_requests=[
            {"id": OTHER, "user_id": USER, "conversation_id": CONV, "kind": "credential", "status": "requested",
             "expires_at": later, "created_at": NOW.isoformat(), "job_id": None, "admission_id": BV2_ADMISSION},
            {"id": BV2_JOB, "user_id": BV2_USER2, "kind": "credential", "status": "requested",
             "expires_at": later, "created_at": NOW.isoformat()},
        ],
        assistant_browser_holds=[{"id": OTHER, "user_id": USER, "job_id": BV2_JOB, "status": "open",
                                  "created_at": NOW.isoformat()}],
        assistant_browser_job_credentials=[{"id": OTHER, "user_id": USER, "admission_id": BV2_ADMISSION,
                                            "job_id": BV2_JOB, "created_at": NOW.isoformat()}],
        browser_agent_jobs=[bv2_job_row()],
    )
    assert repo.browser_admission(USER, BV2_ADMISSION)["id"] == BV2_ADMISSION
    assert repo.browser_admission(USER, OTHER) is None
    assert [a["id"] for a in repo.browser_admissions_waiting(USER)] == [BV2_ADMISSION]
    assert repo.browser_admissions_waiting(USER, site_key="smartbid") == []
    assert [r["id"] for r in repo.browser_requests_open(USER, conversation_id=CONV, kind="credential")] == [OTHER]
    assert repo.browser_request(BV2_USER2, OTHER) is None
    assert repo.browser_hold(USER, OTHER)["id"] == OTHER and repo.browser_hold(BV2_USER2, OTHER) is None
    assert len(repo.browser_holds(USER, BV2_JOB, status="open")) == 1
    assert repo.browser_holds(USER, BV2_JOB, status=["decided"]) == []
    assert len(repo.browser_job_credentials(USER, job_id=BV2_JOB)) == 1
    assert repo.browser_job_credentials(BV2_USER2, admission_id=BV2_ADMISSION) == []
    with pytest.raises(ValueError):
        repo.browser_job_credentials(USER)
    assert repo.browser_job_by_admission(USER, BV2_ADMISSION)["id"] == BV2_JOB
    assert repo.browser_job_by_admission(BV2_USER2, BV2_ADMISSION) is None


# ── Site grant RPC twins ─────────────────────────────────────────────────────


def _grant(**over):
    args = dict(scope="domain", value="example.com", path_prefix=None, granted_via="chat_card",
                provenance="typed", request_id=None, ad_hoc={"label": "example.com"})
    args.update(over)
    key = args.pop("site_key", "domain:example.com")
    return repo.browser_site_grant_put(USER, key, **args)


def test_site_grant_inserts_an_ad_hoc_row_and_one_active_grant(db):
    result = _grant()
    assert result["ok"] and result["grant"]["version"] == 1 and result["waiting_admissions"] == []
    site = db.tables["assistant_browser_sites"][0]
    assert site["kind"] == "ad_hoc" and site["registrable_domains"] == ["example.com"]
    assert site["code_email_senders"] == ["example.com"] and site["created_by"] == USER
    again = _grant()
    assert again["grant"]["id"] == result["grant"]["id"]
    assert len(db.tables["assistant_browser_site_grants"]) == 1


def test_site_grant_origin_scope_keeps_the_path_prefix(db):
    result = _grant(site_key="origin:g3.sharepoint.com:pabc", scope="origin", value="https://g3.sharepoint.com",
                    path_prefix="/sites/Bids", ad_hoc={"label": "g3.sharepoint.com/sites/Bids"})
    assert result["ok"]
    site = db.tables["assistant_browser_sites"][0]
    assert site["exact_origins"] == ["https://g3.sharepoint.com"] and site["path_prefix"] == "/sites/Bids"
    assert site["registrable_domains"] == [] and site["code_email_senders"] == []


@pytest.mark.parametrize("over, code", [
    ({"site_key": "Bad Key"}, "invalid"),
    ({"scope": "everything"}, "invalid"),
    ({"value": ""}, "invalid"),
    ({"granted_via": "model"}, "invalid"),
    ({"provenance": "made_up"}, "invalid"),
    ({"ad_hoc": None}, "unknown_site"),
    ({"site_key": "newsite", "ad_hoc": {"label": "x"}}, "unknown_site"),
    ({"ad_hoc": {"label": ""}}, "unknown_site"),
])
def test_site_grant_refusals(db, over, code):
    assert _grant(**over) == {"ok": False, "code": code}
    assert db.tables.get("assistant_browser_site_grants", []) == []


def test_site_grant_refuses_ineligible_and_disabled(db):
    db.bv2_ineligible = {USER}
    assert _grant()["code"] == "ineligible"
    db.bv2_ineligible = set()
    db.tables["assistant_browser_sites"] = [site_row(site_key="domain:example.com", kind="ad_hoc",
                                                     registrable_domains=["example.com"], enabled=False)]
    assert _grant()["code"] == "site_disabled"


def test_site_grant_approves_the_request_and_lists_waiting_admissions(db):
    db.tables.update(
        assistant_browser_sites=curated_site_rows(),
        assistant_browser_requests=[{"id": OTHER, "user_id": USER, "kind": "site_access", "status": "requested"}],
        assistant_browser_admissions=[
            admission_row(),
            admission_row(id=BV2_JOB, start_site_key="smartbid",
                          site_scope=[{"site_key": "procore", "grant_id": None, "grant_version": None}]),
            admission_row(id=PROJECT, start_site_key="smartbid"),
            admission_row(id=PROJECT2, status="enqueued"),
            admission_row(id=CONV, user_id=BV2_USER2),
        ],
    )
    result = _grant(site_key="procore", value="procore.com", request_id=OTHER, ad_hoc=None)
    assert sorted(result["waiting_admissions"]) == sorted([BV2_ADMISSION, BV2_JOB])
    request = db.tables["assistant_browser_requests"][0]
    assert request["status"] == "approved" and request["result"]["grant_id"] == result["grant"]["id"]


def test_site_revoke_cascades(db):
    grant = grant_row(id=OTHER)
    db.tables.update(
        assistant_browser_site_grants=[grant],
        assistant_browser_sessions=[{"id": "s1", "user_id": USER, "site_key": "procore"},
                                    {"id": "s2", "user_id": USER, "site_key": "smartbid"},
                                    {"id": "s3", "user_id": BV2_USER2, "site_key": "procore"}],
        assistant_browser_admissions=[admission_row(site_scope=[{"site_key": "procore"}])],
        browser_agent_jobs=[bv2_job_row(id="j1", status="queued"), bv2_job_row(id="j2", status="running"),
                            bv2_job_row(id="j3", status="queued", principal_user_id=BV2_USER2)],
    )
    assert repo.browser_site_revoke(USER, OTHER, 2) == {"ok": False, "code": "version_conflict"}
    assert repo.browser_site_revoke(USER, BV2_JOB, 1) == {"ok": False, "code": "not_found"}
    result = repo.browser_site_revoke(USER, OTHER, 1)
    assert result == {"ok": True, "version": 2, "canceled_jobs": 1, "sessions_removed": 1}
    jobs = {j["id"]: j for j in db.tables["browser_agent_jobs"]}
    assert jobs["j1"]["status"] == "canceled" and jobs["j1"]["error_kind"] == "scope_revoked"
    assert jobs["j2"]["policy_version"] == 2 and jobs["j3"]["status"] == "queued"
    assert {s["id"] for s in db.tables["assistant_browser_sessions"]} == {"s2", "s3"}
    assert repo.browser_site_revoke(USER, OTHER, 2) == {"ok": False, "code": "version_conflict"}


def test_scope_extend(db):
    db.tables.update(
        assistant_browser_site_grants=[grant_row(site_key="smartbid", value="smartbidnet.com")],
        assistant_browser_admissions=[admission_row(site_scope=[{"site_key": "procore"}])],
        browser_agent_jobs=[bv2_job_row()],
    )
    assert repo.browser_scope_extend(USER, BV2_JOB, "autodesk") == {"ok": False, "code": "no_grant"}
    assert repo.browser_scope_extend(USER, BV2_JOB, "smartbid") == {"ok": True, "policy_version": 2}
    assert repo.browser_scope_extend(USER, BV2_JOB, "smartbid") == {"ok": True, "policy_version": 2}
    scope = db.tables["assistant_browser_admissions"][0]["site_scope"]
    assert [s["site_key"] for s in scope] == ["procore", "smartbid"] and scope[1]["grant_version"] == 1
    assert repo.browser_scope_extend(BV2_USER2, BV2_JOB, "smartbid") == {"ok": False, "code": "job_changed"}
    db.tables["browser_agent_jobs"][0]["status"] = "succeeded"
    assert repo.browser_scope_extend(USER, BV2_JOB, "smartbid") == {"ok": False, "code": "job_changed"}


def test_unscripted_0192_rpc_still_raises(db):
    with pytest.raises(AssertionError, match="unexpected rpc"):
        repo.browser_request_create(USER, {})


# ── URL provenance sources ───────────────────────────────────────────────────


URL = "https://app.buildingconnected.com/opportunities/abc"


def test_email_sources(db):
    db.tables.update(
        rfp_harvests=[{"id": "h1", "rfp_email_id": OTHER, "external_url": None,
                       "data": {"links": [{"key": "k", "url": URL}]}},
                      {"id": "h2", "rfp_email_id": None, "external_url": "https://bid.example.com/sheet"}],
        rfp_emails=[{"id": OTHER, "from_address": "Invites@GC-Builders.com"}],
    )
    assert repo.browser_url_email_sources(USER, [URL]) == [{"source": "rfp_harvest", "sender_domain": "gc-builders.com"}]
    assert repo.browser_url_email_sources(USER, ["https://bid.example.com/sheet"]) == [
        {"source": "rfp_harvest", "sender_domain": None}]
    assert repo.browser_url_email_sources(USER, ["https://nowhere.example.com/"]) == []
    assert repo.browser_url_email_sources(USER, []) == []


def test_project_links_split_by_origin(db):
    db.tables.update(
        projects=[{"id": PROJECT, "bidding_url": URL}, {"id": PROJECT2, "bidding_url": "https://typed.example.com/x",
                                                        "files_needed_url": "https://portal.example.com/f"}],
        rfp_created_projects=[{"project_id": PROJECT}],
        pm_external_links=[{"id": "l1", "external_url": "https://app.procore.com/1/project/home",
                            "linked_via": "manual"},
                           {"id": "l2", "external_url": "https://app.procore.com/2/project/home",
                            "linked_via": "email"}],
        rfis=[{"id": "r1", "external_url": "https://app.procore.com/1/project/rfi/show/9"}],
    )
    assert repo.browser_url_email_sources(USER, [URL]) == [{"source": "rfp_project", "sender_domain": None}]
    assert repo.browser_url_bdr_records(USER, [URL]) == []
    assert repo.browser_url_bdr_records(USER, ["https://typed.example.com/x"]) == [{"source": "project_field"}]
    assert repo.browser_url_email_sources(USER, ["https://portal.example.com/f"]) == [
        {"source": "rfp_portal", "sender_domain": None}]
    assert repo.browser_url_bdr_records(USER, ["https://app.procore.com/1/project/home"]) == [{"source": "pm_link"}]
    assert repo.browser_url_email_sources(USER, ["https://app.procore.com/2/project/home"]) == [
        {"source": "pm_link_email", "sender_domain": None}]
    assert repo.browser_url_bdr_records(USER, ["https://app.procore.com/1/project/rfi/show/9"]) == [
        {"source": "rfi_sync"}]
