"""WI-20 probe helper and the 0194 curated-profile draft, offline only."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("browser_v2_probe_site", ROOT / "scripts/browser_v2_probe_site.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

MIGRATION = ROOT / "supabase/migrations/0194_assistant_browser_curated_profiles.sql"
PROCORE = {"site_key": "procore", "registrable_domains": ["procore.com"], "exact_origins": [],
           "login_origins": ["https://login.procore.com"], "login_paths": [], "cookie_domains": [".procore.com"],
           "code_email_senders": ["procore.com"], "code_email_regex": "x", "profile": {}}


@pytest.mark.parametrize("path", ["/", "/api/v1/projects/123/filters", "/rest/v1.0/projects/12/rfis",
                                  "/a/0f8fad5b-d9cb-469f-a165-70867728950e/b", "/x/abc4efghijklmnopqrstuvwx/y"])
def test_path_regex_matches_the_owner_safe_rule_shape(path):
    from app.routers.assistant_browser import safe_path_pattern

    assert probe.path_regex(path) == safe_path_pattern(path)
    assert re.fullmatch(probe.path_regex(path), path)


def test_trace_token_placeholder_is_generalized_and_long_words_stay_exact():
    assert probe.path_regex("/files/:token/view") == "^/files/[A-Za-z0-9_-]+/view$"
    assert probe.path_regex("/sessions/submit_login_password") == "^/sessions/submit_login_password$"


def steps():
    return [
        {"kind": "tool.navigate", "tool_name": "navigate", "ok": True, "url_before": None,
         "url_after": "https://app.procore.com/123/project/rfi/list"},
        {"kind": "login.exemption_open", "result": {"origin": "https://login.procore.com", "kind": "curated"}},
        {"kind": "login.exempt", "result": {"method": "POST", "url": "https://login.procore.com/sessions/submit_login_email"}},
        {"kind": "login.exempt", "result": {"method": "POST", "url": "https://login.procore.com/sessions/submit_login_password"}},
        {"kind": "tool.fill_credential", "tool_name": "fill_credential", "ok": False,
         "url_before": "https://sso.example.net/login"},
        {"kind": "browser.hold_click", "result": {"url": "https://app.procore.com/123/project/bids/9/accept"}},
        {"kind": "http.websocket", "result": {"url": "wss://realtime.procore.com/socket"}},
        {"kind": "http.blocked", "result": {"method": "POST", "url": "https://collector.example.org/t", "reason": "analytics"}},
        {"kind": "tool.read_page", "tool_name": "read_page", "ok": True,
         "url_before": "https://elsewhere.example.com/page", "url_after": None},
    ]


def holds():
    return [
        {"id": "h1", "kind": "final_action", "status": "declined", "decision": "decline", "method": "POST",
         "origin": "https://app.procore.com", "label": None,
         "summary": {"trigger": "network", "url": "https://app.procore.com/rest/v1.0/projects/12/rfis/34/replies?x=secret",
                     "element_label": "Submit response"}},
        {"id": "h2", "kind": "final_action", "status": "approved", "decision": "approve", "method": "POST",
         "origin": "https://app.procore.com", "marked_safe": True,
         "summary": {"url": "https://app.procore.com/rest/v1.0/projects/12/rfis/filters"}},
        {"id": "h3", "kind": "final_action", "status": "declined", "decision": "decline", "method": "POST",
         "origin": "https://api.segment.io", "summary": {"url": "https://api.segment.io/v1/t"}},
        {"id": "h4", "kind": "site", "status": "approved", "decision": "approve", "method": "GET",
         "origin": "https://docs.example.com", "summary": {}},
        {"id": "h5", "kind": "final_action", "status": "expired", "decision": None, "method": "POST",
         "origin": "https://app.procore.com", "summary": {"url": "https://app.procore.com/graphql", "graphql_operation": "X"}},
    ]


def test_summary_proposes_login_safe_and_final_rules_from_the_run():
    rules = [{"method": "POST", "host": "app.procore.com", "path_pattern": "^/rest/v1\\.0/projects/[0-9]+/rfis/filters$",
              "graphql_operation": None, "source_hold_id": "h2"},
             {"method": "POST", "host": "app.procore.com", "path_pattern": "^/bids/[0-9]+/submit$"},
             {"method": "POST", "host": "tracker.example.org", "path_pattern": "^/t$"}]
    suggestions = [{"rule": {"method": "POST", "host": "app.procore.com", "path_pattern": "^/graphql$",
                             "graphql_operation": "ProjectList"}}]
    out = probe.summarize(PROCORE, steps(), holds(), rules, suggestions)
    proposed = out["proposed"]
    assert proposed["login_origins"] == ["https://login.procore.com"]
    assert proposed["login_paths"] == ["^/sessions/submit_login_email$", "^/sessions/submit_login_password$"]
    safe = proposed["profile"]["safe_mutations"]
    assert {"method": "POST", "host": "app.procore.com", "path_regex": "^/graphql$",
            "graphql_operations": ["ProjectList"]} in safe
    assert all("submit" not in r["path_regex"] and r["host"] == "app.procore.com" for r in safe)
    finals = proposed["profile"]["final_actions"]
    assert {"method": "POST", "host": "app.procore.com", "path_regex": "^/rest/v1\\.0/projects/[0-9]+/rfis/[0-9]+/replies$",
            "label": "Submit response"} in finals
    assert not any("filters" in r["path_regex"] for r in finals)  # marked safe by the owner
    assert proposed["profile"]["final_get"][0]["path_regex"] == "^/[0-9]+/project/bids/[0-9]+/accept$"
    assert proposed["profile"]["websocket_ok"] is False
    observed = out["observed"]
    assert observed["posts_outside_site"] == [{"method": "POST", "host": "api.segment.io", "path": "/v1/t", "count": 1}]
    assert [p["origin"] for p in observed["pages"]] == ["https://app.procore.com"]
    text = json.dumps(out)
    assert "secret" not in text and "?x=" not in text  # hold queries never printed
    warnings = " ".join(out["warnings"])
    assert "final-action verb" in warnings and "outside the site's domains" in warnings
    assert "https://sso.example.net" in warnings and "WebSocket" in warnings and "GraphQL" in warnings


def test_entry_without_a_login_keeps_a_todo_and_adds_aligned_senders_only():
    out = probe.summarize(PROCORE, [], [])
    assert any("no login form post" in w for w in out["warnings"])
    mail = [{"from_domain": "procore.com", "dkim_aligned": True}, {"from_domain": "evil.example", "dkim_aligned": False}]
    entry = probe.migration_entry(out, mail)
    assert "TODO" in entry["login_paths"][0] and entry["code_email_senders"] == ["procore.com"]
    assert "TODO" in json.dumps(entry["profile"]["logged_in_probe"])


def test_mask_subject_never_shows_digits():
    assert probe.mask_subject("Your Procore verification code is 123456") == "Your Procore verification code is ######"


def test_the_0194_draft_parses_and_refuses_while_todo_remains():
    sql = MIGRATION.read_text(encoding="utf-8")
    data = probe.extract_profiles(sql)
    assert set(data) == {"procore", "autodesk", "smartbid"}
    problems = probe.check_profiles(data)
    assert problems and "TODO values remain" in problems[0]
    # The SmartBid correction comes from the codebase and is complete.
    assert probe.check_profiles({"smartbid": data["smartbid"]}) == []
    assert data["smartbid"]["login_origins"][0] == "https://gocc.smartbid.co"
    # The SQL guard refuses the same way and the file stays additive.
    assert "if v::text ~ 'TODO' then" in sql
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert not re.search(r"\b(drop|delete|truncate|revoke|create)\b", code, re.I)
    assert "\u2014" not in sql and "\u2013" not in sql


def filled(**overrides):
    entry = {
        "login_origins": ["https://login.procore.com"],
        "login_paths": ["^/sessions/submit_login_email$", "^/sessions/submit_login_password$"],
        "code_email_senders": ["procore.com"],
        "profile": {
            "safe_mutations": [{"method": "POST", "host": "app.procore.com", "path_regex": "^/graphql$",
                                "graphql_operations": ["ProjectList"]}],
            "final_actions": [{"method": "POST", "host": "app.procore.com",
                               "path_regex": "^/rest/v1\\.0/projects/[0-9]+/rfis/[0-9]+/replies$",
                               "label": "Submit RFI response"}],
            "final_get": [],
            "websocket_ok": False,
            "websocket_reason": None,
            "logged_in_probe": {"path": "/", "selector": "[data-qa=user-menu]"},
        },
    }
    entry.update(overrides)
    return {"procore": entry}


def test_a_filled_block_passes_the_offline_check_and_the_cli(tmp_path, capsys):
    data = filled()
    assert probe.check_profiles(data) == []
    sql = MIGRATION.read_text(encoding="utf-8")
    start = sql.index("$profiles$") + len("$profiles$")
    end = sql.index("$profiles$", start)
    path = tmp_path / "0194.sql"
    path.write_text(sql[:start] + "\n" + json.dumps(data, indent=2) + "\n" + sql[end:], encoding="utf-8")
    assert probe.main(["check-migration", str(path)]) == 0
    assert "OK: the profiles block can be applied." in capsys.readouterr().out
    assert probe.main(["check-migration", str(MIGRATION)]) == 1


@pytest.mark.parametrize("change,needle", [
    ({"login_origins": ["http://login.procore.com"]}, "https origin"),
    ({"login_origins": ["https://login.procore.com.evil.net"]}, "https origin"),
    ({"login_paths": ["/sessions/("]}, "not a valid regex"),
    ({"login_paths": ["/sessions/submit_login_email"]}, "anchored"),
    ({"code_email_senders": ["not a domain"]}, "must be a domain"),
    ({"code_email_regex": "code ([0-9]{6})"}, "anchored at the subject start"),
    ({"surprise": 1}, "unknown keys"),
    ({"profile": {"safe_mutations": [{"method": "POST", "host": "app.procore.com", "path_regex": "^/bids/submit$"}]}},
     "final-action verb"),
    ({"profile": {"safe_mutations": [{"method": "POST", "host": "app.procore.com", "path_regex": "^/graphql$",
                                      "graphql_operations": ["AcceptBid"]}]}}, "final-action verb"),
    ({"profile": {"final_actions": [{"method": "POST", "host": "evil.example.com", "path_regex": "^/x$", "label": "X"}]}},
     "outside the site's domains"),
    ({"profile": {"websocket_ok": True}}, "websocket_reason"),
    ({"profile": {"final_get": [{"host": "app.procore.com", "path_regex": "(", "label": "x"}]}}, "UserSiteProfile"),
    ({"profile": {"extra": []}}, "profile keys"),
])
def test_check_refuses_wrong_values(change, needle):
    problems = probe.check_profiles(filled(**change))
    assert any(needle in p for p in problems), problems


def test_check_refuses_unknown_sites_and_a_second_profiles_block():
    assert any("not a curated site" in p for p in probe.check_profiles({"example": {}}))
    with pytest.raises(probe.ProbeError):
        probe.extract_profiles("$profiles${}$profiles$ $profiles${}$profiles$")
    with pytest.raises(probe.ProbeError):
        probe.extract_profiles("$profiles$ not json $profiles$")


def test_summary_reads_only_and_only_the_dev_project(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(supabase_url="https://zxqkqotlcgbalwoqeeca.supabase.co"))
    with pytest.raises(probe.ProbeError):
        probe._guard_dev()
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(supabase_url=f"https://{probe.DEV_REF}.supabase.co/"))
    probe._guard_dev()


def test_run_summary_uses_reads_only(monkeypatch):
    from app.assistant import repo
    from app.core import config, supabase_client
    from app.services import assistant_browser_holds, assistant_browser_records

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(supabase_url=f"https://{probe.DEV_REF}.supabase.co"))
    job = {"id": "j1", "agent_type": "user_browser", "status": "succeeded", "created_at": "2026-11-03T16:00:00+00:00"}
    monkeypatch.setattr(repo, "browser_site", lambda owner, key: dict(PROCORE))
    monkeypatch.setattr(repo, "browser_jobs", lambda owner, limit: [job, {**job, "id": "old", "agent_type": "user_browser_read"}])
    monkeypatch.setattr(repo, "browser_safe_rules", lambda owner, site_key: [])
    rows = [{"id": f"r{i}", "created_at": "2026-11-03T16:00:00+00:00"} for i in range(len(steps()))]
    monkeypatch.setattr(repo, "browser_records", lambda owner, job_id, kinds, before, limit: rows if before is None else [])
    by_id = dict(zip([r["id"] for r in rows], steps()))
    monkeypatch.setattr(assistant_browser_records, "unpack_record", lambda owner, job_id, row: by_id[row["id"]])
    monkeypatch.setattr(repo, "browser_holds", lambda owner, job_id, limit: holds()[:1])
    monkeypatch.setattr(assistant_browser_holds, "summary", lambda owner, hold: hold["summary"])

    class Query:
        def __getattr__(self, name):
            if name in ("insert", "update", "upsert", "delete", "rpc"):
                pytest.fail(f"the probe called {name}")
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(data=[])

    monkeypatch.setattr(supabase_client, "get_supabase", lambda: SimpleNamespace(table=lambda name: Query()))
    args = SimpleNamespace(owner="o1", site="procore", since="2026-11-03T08:00:00-08:00", job=None, mail=False)
    out = probe.run_summary(args)
    assert [j["id"] for j in out["jobs"]] == ["j1"]
    assert out["migration_entry"]["procore"]["login_paths"] == ["^/sessions/submit_login_email$",
                                                                "^/sessions/submit_login_password$"]
    with pytest.raises(probe.ProbeError):
        probe.run_summary(SimpleNamespace(**{**vars(args), "since": "2026-11-03T08:00:00"}))


# ── Review fixes ─────────────────────────────────────────────────────────────


def test_0194_brings_saved_logins_to_the_new_origins_and_refuses_identity_providers():
    sql = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "update public.assistant_credentials c set login_origins = t.login_origins" in code
    assert "c.site_key = k and c.status = 'active'" in code
    assert "if s ? 'login_origins' or s ? 'cookie_domains' then" in code
    assert "public.assistant_browser_v2_is_idp(o)" in code


def test_smartbid_owned_domains_match_the_client():
    from app.services import assistant_browser_sites as sites
    from app.services import smartbid_client

    owned = dict((k, d) for k, _b, d in sites.LOOKALIKE_BRANDS)["smartbid"]
    assert probe.split_url(smartbid_client.SPA_ORIGIN)[1].endswith("smartbid.co") and "smartbid.co" in owned
    assert smartbid_client.API_HOST.endswith("smartinsight.co") and "smartinsight.co" in owned
    assert sites.CURATED_DOMAINS["smartbid"][1] == owned
    assert sites.lookalike_of(smartbid_client.SPA_ORIGIN) is None


def test_an_owner_rule_is_rebuilt_from_its_held_path_not_its_stored_pattern():
    """The stored pattern of a filter-preferences rule could have generalized
    the word segment; the probe rebuilds it from the held request so the
    curated rule never matches /replies (answering an RFI)."""
    held = [{"id": "h9", "kind": "final_action", "status": "approved", "decision": "approve", "method": "POST",
             "origin": "https://app.procore.com", "marked_safe": True,
             "summary": {"url": "https://app.procore.com/rest/v1.0/projects/1/rfis/12/bulk_filter_preferences"}}]
    broad = "^/rest/v1\\.0/projects/[0-9]+/rfis/[0-9]+/[A-Za-z0-9_-]+$"
    rules = [{"method": "POST", "host": "app.procore.com", "path_pattern": broad, "source_hold_id": "h9"},
             {"method": "POST", "host": "app.procore.com", "path_pattern": broad.replace("rfis", "items")}]
    out = probe.summarize(PROCORE, [], held, rules)
    safe = out["proposed"]["profile"]["safe_mutations"]
    assert [r["path_regex"] for r in safe] == ["^/rest/v1\\.0/projects/[0-9]+/rfis/[0-9]+/bulk_filter_preferences$"]
    assert re.fullmatch(safe[0]["path_regex"], "/rest/v1.0/projects/1/rfis/12/replies") is None
    assert any("no held path in this run" in w for w in out["warnings"])
    # A held path that names a final-action verb is never proposed, whatever the stored pattern.
    held[0]["summary"]["url"] = "https://app.procore.com/rest/v1.0/projects/1/rfis/12/send_reminder"
    out = probe.summarize(PROCORE, [], held, rules[:1])
    assert out["proposed"]["profile"]["safe_mutations"] == []


def test_mail_environment_turns_on_the_reader_for_this_process_only():
    env = {"ASSISTANT_TOKEN_SECRET": "short"}
    probe.mail_environment(env)
    assert env["ASSISTANT_ENABLED"] == env["ASSISTANT_OUTLOOK_ENABLED"] == "true"
    assert len(env["ASSISTANT_TOKEN_SECRET"]) >= 32
    keep = {"ASSISTANT_TOKEN_SECRET": "k" * 40}
    probe.mail_environment(keep)
    assert keep["ASSISTANT_TOKEN_SECRET"] == "k" * 40


def _summary_world(monkeypatch):
    from app.assistant import repo
    from app.core import config, supabase_client

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(supabase_url=f"https://{probe.DEV_REF}.supabase.co"))
    job = {"id": "j1", "agent_type": "user_browser", "status": "succeeded", "created_at": "2026-11-03T16:00:00+00:00"}
    monkeypatch.setattr(repo, "browser_site", lambda owner, key: dict(PROCORE))
    monkeypatch.setattr(repo, "browser_jobs", lambda owner, limit: [job])
    monkeypatch.setattr(repo, "browser_safe_rules", lambda owner, site_key: [])
    monkeypatch.setattr(repo, "browser_records", lambda *a, **k: [])
    monkeypatch.setattr(repo, "browser_holds", lambda *a, **k: [])
    query = SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
    for name in ("select", "eq", "limit"):
        setattr(query, name, lambda *a, **k: query)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: SimpleNamespace(table=lambda name: query))
    return SimpleNamespace(owner="o1", site="procore", since="2026-11-03T08:00:00-08:00", job=None, mail=True,
                           sender=["example-mfa.com"])


def test_an_unreadable_mailbox_is_a_warning_and_the_summary_still_prints(monkeypatch):
    from app.services import assistant_browser_mfa_mail as mfa

    args = _summary_world(monkeypatch)

    def refused(owner):
        raise mfa.MfaMailRefused("mailbox_unavailable")

    monkeypatch.setattr(mfa, "owner_mailbox", refused)
    out = probe.run_summary(args)
    assert "code emails could not be read: mailbox_unavailable" in out["warnings"]
    assert out["code_emails"] == [] and "procore" in out["migration_entry"]


def test_mail_lists_the_extra_senders_and_proposes_only_aligned_ones(monkeypatch):
    from app.services import assistant_browser_mfa_mail as mfa

    args = _summary_world(monkeypatch)
    seen = {}

    def fake_mail(owner, site, since, extra=()):
        seen["extra"] = list(extra)
        return [{"from_domain": "procoretech.com", "dkim_aligned": True},
                {"from_domain": "example-mfa.com", "dkim_aligned": False}]

    monkeypatch.setattr(probe, "_mail", fake_mail)
    monkeypatch.setattr(mfa, "owner_mailbox", lambda owner: pytest.fail("not reached"))
    out = probe.run_summary(args)
    assert seen["extra"] == ["example-mfa.com", "procoretech.com"]
    assert out["migration_entry"]["procore"]["code_email_senders"] == ["procore.com", "procoretech.com"]


def test_the_mail_listing_filter_takes_the_extra_domains(monkeypatch):
    from app.services import assistant_browser_mfa_mail as mfa

    listed = [{"id": "m1", "from": {"emailAddress": {"address": "no-reply@procoretech.com"}}},
              {"id": "m2", "from": {"emailAddress": {"address": "x@unrelated.example"}}}]
    monkeypatch.setattr(mfa, "owner_mailbox", lambda owner: ("me@g3.example", "token"))
    monkeypatch.setattr(mfa, "_list_recent", lambda mailbox, token, since: listed)
    read: list[str] = []
    monkeypatch.setattr(mfa, "_read_one", lambda mailbox, token, mid: read.append(mid) or {
        "from": {"emailAddress": {"address": "no-reply@procoretech.com"}}, "subject": "Code 123456",
        "body": {"content": ""}, "receivedDateTime": "2026-11-03T16:00:00Z"})
    monkeypatch.setattr(mfa, "sender_trusted", lambda headers, **k: "procoretech.com" in k["allowed_domains"])
    monkeypatch.setattr(mfa, "addressed_to", lambda message, mailbox: True)
    from datetime import datetime, timezone

    since = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert probe._mail("o1", PROCORE, since) == [] and read == []
    rows = probe._mail("o1", PROCORE, since, ["procoretech.com"])
    assert read == ["m1"] and rows[0]["from_domain"] == "procoretech.com" and rows[0]["dkim_aligned"] is True
    assert "123456" not in json.dumps(rows)
