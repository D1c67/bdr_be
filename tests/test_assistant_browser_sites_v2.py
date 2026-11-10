"""Browser v2 site registry, URL rules, provenance and grants
(docs/ASSISTANT_BROWSER_V2_PLAN.md 5.1, 5.2, 5.4, 5.5, 7.3; WI-3).

Pinned here: URL normalization (https on any port, http only for top-level
navigation, default ports dropped, IDNA hosts, private networks and unknown
TLDs refused, queries and fragments kept); the approval unit (registrable
domain, exact origin plus path prefix on multi-tenant domains, exact origin
under private-section suffixes); registry matching (most specific row,
lookalike hosts never match a curated site); lookalike warnings; URL
provenance (an RFP harvest link is `email`, typed beats everything, email
beats a BDR field, failures fail closed); grants (registry scope wins,
multi-tenant scope cannot be widened, revoke and scope extend codes); the
phase 6 names still answer for the retired type; Python and SQL agree on
the key charset and the identity providers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.assistant import repo
from app.core import supabase_client
from app.services import assistant_browser_grants as grants
from app.services import assistant_browser_sites as sites
from app.services.browser_agents import protocol
from tests.assistant_fakes import (
    BV2_JOB,
    BV2_USER2,
    CONV,
    NOW,
    PROJECT,
    PROJECT2,
    RUN,
    USER,
    FakeDB,
    admission_row,
    bv2_job_row,
    curated_site_rows,
    grant_row,
    site_row,
)

SQL = (Path(__file__).resolve().parents[1] / "supabase/migrations/0192_assistant_browser_v2.sql").read_text(
    encoding="utf-8")
EM_DASH = chr(0x2014)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(now=NOW, assistant_browser_sites=curated_site_rows())
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


# ── URL normalization (5.1) ──────────────────────────────────────────────────


@pytest.mark.parametrize("url, expected", [
    ("https://app.procore.com/1/project/rfi/list?page=2#top", "https://app.procore.com/1/project/rfi/list?page=2#top"),
    ("HTTPS://App.Procore.COM", "https://app.procore.com/"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com:8443/a", "https://example.com:8443/a"),
    ("http://example.com:8080/a?q=1", "http://example.com:8080/a?q=1"),
    ("https://xn--bcher-kva.de/x", "https://xn--bcher-kva.de/x"),
    ("https://bücher.de/x", "https://xn--bcher-kva.de/x"),
    ("https://example.com/café", "https://example.com/caf%C3%A9"),
    ("https://example.com./a", "https://example.com/a"),
])
def test_normalize_accepts(url, expected):
    assert sites.normalize_url(url) == expected


@pytest.mark.parametrize("url, code", [
    ("", "invalid"),
    ("https://example.com/" + "a" * 2048, "too_long"),
    ("https://example.com/a b", "invalid"),
    ("https://example.com/\x07", "invalid"),
    ("https://example.com\\@evil.com/", "invalid"),
    ("ftp://example.com/", "scheme"),
    ("javascript:alert(1)", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("https://user:pw@example.com/", "userinfo"),
    ("https://user@example.com/", "userinfo"),
    ("https://10.0.0.5/", "private_network"),
    ("https://192.168.1.10:8443/", "private_network"),
    ("https://8.8.8.8/", "private_network"),
    ("https://[::1]/", "private_network"),
    ("https://localhost/", "private_network"),
    ("https://printer.local/", "private_network"),
    ("https://nas.lan/", "private_network"),
    ("https://box.internal/", "private_network"),
    ("https://intranet/", "host"),
    ("https://-bad-.com/", "host"),
    ("https://example.corp/", "no_registrable_domain"),
    ("https://co.uk/", "no_registrable_domain"),
    ("https://example.com:0/", "port"),
    ("https://example.com:70000/", "port"),
    ("https://example.com:abc/", "port"),
])
def test_normalize_refuses(url, code):
    with pytest.raises(sites.UrlRefused) as info:
        sites.normalize_url(url)
    assert info.value.code == code
    assert str(info.value) == sites.REFUSED_MESSAGE  # never echoes the URL


def test_http_is_top_level_only_and_marked_unencrypted():
    assert sites.normalize_url("http://example.com/", top_level=True) == "http://example.com/"
    with pytest.raises(sites.UrlRefused) as info:
        sites.normalize_url("http://example.com/", top_level=False)
    assert info.value.code == "http_not_top_level"
    assert sites.normalize_url("https://example.com:8443/", top_level=False) == "https://example.com:8443/"
    assert sites.is_encrypted("https://example.com:8443/") is True
    assert sites.is_encrypted("http://example.com:8080/") is False
    assert sites.url_origin("http://Example.com:8080/x?y") == "http://example.com:8080"
    assert sites.url_origin("https://example.com:443/x") == "https://example.com"


def test_parsed_origin_matches_the_worker_protocol():
    for url in ("https://a.example.com:8443/x", "http://a.example.com/x", "https://a.example.com:443/"):
        assert sites.parse_url(url).origin == protocol.url_origin(url)


# ── Domains, suffixes, multi-tenant, identity providers ──────────────────────


@pytest.mark.parametrize("value, expected", [
    ("app.procore.com", "procore.com"),
    ("https://us02.procore.com/x", "procore.com"),
    ("a.b.example.co.uk", "example.co.uk"),
    ("bucket.s3.amazonaws.com", "bucket.s3.amazonaws.com"),
    ("someone.github.io", "someone.github.io"),
    ("g3.sharepoint.com", "sharepoint.com"),
    ("com", None),
    ("co.uk", None),
    ("10.0.0.1", None),
    ("intranet.corp", None),
])
def test_registrable_domain(value, expected):
    assert sites.registrable_domain(value) == expected


def test_private_section_and_multi_tenant():
    assert sites.is_private_suffix("someone.github.io")
    assert sites.is_private_suffix("myapp.herokuapp.com")
    assert not sites.is_private_suffix("app.procore.com")
    assert sites.is_multi_tenant("g3.sharepoint.com") and sites.is_multi_tenant("https://docs.google.com/x")
    assert sites.is_multi_tenant("origin:github.com:p1234")
    assert not sites.is_multi_tenant("procore.com") and not sites.is_multi_tenant("notgoogle.com")


@pytest.mark.parametrize("value", [
    "https://login.microsoftonline.com", "login.live.com", "https://accounts.google.com/o/x",
    "https://g3.okta.com", "acme.onelogin.com", "appleid.apple.com",
])
def test_identity_providers(value):
    assert sites.is_identity_provider(value)


def test_identity_providers_are_not_ordinary_sites():
    assert not sites.is_identity_provider("https://login.procore.com")
    assert not sites.is_identity_provider("okta.com")


def test_python_and_sql_agree_on_identity_providers_and_key_charset():
    for host in protocol.IDENTITY_PROVIDER_HOSTS:
        bare = host.removeprefix("*.").split(".")[0] if host.startswith("*.") else host
        assert bare in SQL, host
    assert "'^[a-z0-9_.:-]{2,80}$'" in SQL and protocol.SITE_KEY_RE.pattern == "^[a-z0-9_.:-]{2,80}$"
    assert "'^(domain|origin):'" in SQL


# ── Grant targets and ad hoc keys (5.1) ──────────────────────────────────────


def test_ordinary_site_is_granted_by_registrable_domain():
    target = sites.grant_target_for_url("https://portal.gc-builders.com:8443/bids?x=1")
    assert (target.site_key, target.scope, target.value, target.path_prefix) == (
        "domain:gc-builders.com", "domain", "gc-builders.com", None)
    assert target.encrypted and not target.multi_tenant and not target.exists
    assert target.ad_hoc() == {"label": "gc-builders.com"}


def test_http_site_target_is_not_encrypted():
    target = sites.grant_target_for_url("http://old-portal.example.com/")
    assert target.scope == "domain" and target.encrypted is False


@pytest.mark.parametrize("url, origin, prefix", [
    ("https://g3.sharepoint.com/sites/Bids/Shared%20Documents/a.aspx", "https://g3.sharepoint.com", "/sites/Bids"),
    ("https://docs.google.com/document/d/abc123/edit", "https://docs.google.com", "/document/d/abc123"),
    ("https://docs.google.com/forms/d/e/1FAIpQ/viewform", "https://docs.google.com", "/forms/d/e/1FAIpQ"),
    ("https://github.com/owner/repo/tree/main", "https://github.com", "/owner/repo"),
    ("https://www.dropbox.com/", "https://www.dropbox.com", "/"),
    ("https://acme.azurewebsites.net:8443/app/x", "https://acme.azurewebsites.net:8443", "/app/x"),
])
def test_multi_tenant_grants_are_origin_plus_path_prefix(url, origin, prefix):
    target = sites.grant_target_for_url(url)
    assert (target.scope, target.value, target.path_prefix, target.multi_tenant) == ("origin", origin, prefix, True)
    assert protocol.SITE_KEY_RE.match(target.site_key)
    assert target.site_key.startswith("origin:")


def test_two_tenants_on_one_host_get_different_keys():
    a = sites.grant_target_for_url("https://g3.sharepoint.com/sites/Bids/x")
    b = sites.grant_target_for_url("https://g3.sharepoint.com/sites/Evil/x")
    assert a.site_key != b.site_key and a.value == b.value


def test_private_suffix_grant_is_the_exact_origin():
    target = sites.grant_target_for_url("https://someone.github.io/page")
    assert (target.scope, target.value, target.path_prefix, target.private_suffix) == (
        "origin", "https://someone.github.io", None, True)
    assert target.site_key == "origin:someone.github.io"


def test_ad_hoc_keys_mark_ports_and_http_and_fit_the_charset():
    assert sites.ad_hoc_site_key("origin", "https://a.example.com:8443") == "origin:a.example.com:8443"
    assert sites.ad_hoc_site_key("origin", "http://a.example.com") == "origin:a.example.com:http"
    assert sites.ad_hoc_site_key("origin", "https://a.example.com", "/") == "origin:a.example.com"
    long_host = ".".join(["a" * 60] * 3) + ".com"
    key = sites.ad_hoc_site_key("origin", f"https://{long_host}", "/x/y")
    assert protocol.SITE_KEY_RE.match(key) and key.startswith("origin:h")
    assert protocol.SITE_KEY_RE.match(sites.ad_hoc_site_key("domain", long_host))


def test_grant_target_for_registered_sites():
    procore = site_row()
    target = sites.grant_target_for_site(procore, url="https://app.procore.com/")
    assert (target.site_key, target.scope, target.value, target.kind, target.exists) == (
        "procore", "domain", "procore.com", "curated", True)
    assert target.ad_hoc() is None
    origin_row = site_row(site_key="origin:x.github.io", kind="ad_hoc", registrable_domains=[],
                          exact_origins=["https://x.github.io"])
    target = sites.grant_target_for_site(origin_row)
    assert (target.scope, target.value, target.private_suffix) == ("origin", "https://x.github.io", True)
    with pytest.raises(ValueError):
        sites.grant_target_for_site(site_row(registrable_domains=[], exact_origins=[]))


# ── Registry matching ────────────────────────────────────────────────────────


@pytest.mark.parametrize("url, key", [
    ("https://app.procore.com/1/project/home", "procore"),
    ("https://login.procore.com/", "procore"),
    ("https://acc.autodesk.com/projects", "autodesk"),
    ("https://accounts.autodesk.com/logon", "autodesk"),
    ("https://app.buildingconnected.com/opportunities", "buildingconnected"),
    ("https://www.smartbidnet.com/", "smartbid"),
    ("https://procore.com.example.net/login", None),
    ("https://notprocore.com/", None),
    ("https://procore.co/", None),
])
def test_site_for_url_curated(db, url, key):
    found = sites.site_for_url(USER, url)
    assert (found or {}).get("site_key") == key


def test_site_for_url_ad_hoc_path_prefix_and_disabled(db):
    db.tables["assistant_browser_sites"] += [
        site_row(site_key="origin:g3.sharepoint.com:pabc", label="Bids", kind="ad_hoc", registrable_domains=[],
                 exact_origins=["https://g3.sharepoint.com"], path_prefix="/sites/Bids"),
        site_row(site_key="domain:example.com", label="example.com", kind="ad_hoc",
                 registrable_domains=["example.com"], enabled=False),
    ]
    assert sites.site_for_url(USER, "https://g3.sharepoint.com/sites/Bids/x")["site_key"] == "origin:g3.sharepoint.com:pabc"
    assert sites.site_for_url(USER, "https://g3.sharepoint.com/sites/Bids")["site_key"] == "origin:g3.sharepoint.com:pabc"
    assert sites.site_for_url(USER, "https://g3.sharepoint.com/sites/BidsEvil/x") is None
    assert sites.site_for_url(USER, "https://g3.sharepoint.com/sites/Other") is None
    disabled = sites.site_for_url(USER, "https://www.example.com/")
    assert disabled["site_key"] == "domain:example.com" and disabled["enabled"] is False
    assert sites.site_matches(db.tables["assistant_browser_sites"][4], "https://g3.sharepoint.com/sites/Bids/y")
    assert not sites.site_matches(db.tables["assistant_browser_sites"][4], "not a url")


def test_site_for_url_without_the_database():
    rows = curated_site_rows()
    assert sites.site_for_url(USER, "https://acc.autodesk.com/", sites=rows)["site_key"] == "autodesk"
    with pytest.raises(sites.UrlRefused):
        sites.site_for_url(USER, "https://10.1.1.1/", sites=rows)


def test_resolve_site(db):
    site, target = sites.resolve_site(USER, "https://app.procore.com/")
    assert site["site_key"] == "procore" and target.exists
    site, target = sites.resolve_site(USER, "https://bids.gc.example.com/")
    assert site is None and target.site_key == "domain:example.com"
    assert sites.get_site(USER, "procore")["label"] == "Procore"
    assert sites.get_site(USER, "Bad Key") is None
    assert {r["site_key"] for r in sites.list_sites(USER)} == {"procore", "autodesk", "buildingconnected", "smartbid"}


# ── Lookalikes (7.3) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, brand", [
    ("procore.com.example.net", "Procore"),
    ("https://login-procore.com/", "Procore"),
    ("domain:procore.co", "Procore"),
    ("pr0core.com", "Procore"),
    ("https://xn--prcore-4ya.com/", "Procore"),
    ("procore-login.sharepoint.com", "Procore"),
    ("autodesk.co", "Autodesk Construction Cloud"),
    ("buildingconected.com", "BuildingConnected"),
    ("smartbid.net", "SmartBid"),
    ("microsoft-sso.azurewebsites.net", "Microsoft"),
    ("gooogle.com", "Google"),
    # Cyrillic homoglyphs and leetspeak plus a suffix (review finding).
    ("https://xn--prre-65dau.com/", "Procore"),
    ("pr\u043e\u0441\u043ere.com", "Procore"),
    ("\u0440r\u043e\u0441\u043er\u0435.com", "Procore"),
    ("proc0re-secure.net", "Procore"),
    ("procor.net", "Procore"),
    ("g\u03bf\u03bfgle.com", "Google"),
    ("app.procore.com", None),
    ("live.com", None),
    ("office.com", None),
    ("1password.com", None),
    ("bidsite365.com", None),
    ("accounts.autodesk.com", None),
    ("smartbidnet.com", None),
    ("gocc.smartbid.co", None),           # the real SmartBid sign-in page (smartbid_client.SPA_ORIGIN)
    ("apicc.smartinsight.co", None),      # its token API
    ("smartbid-login.net", "SmartBid"),
    ("login.microsoftonline.com", None),
    ("docs.google.com", None),
    ("example.com", None),
])
def test_lookalikes(value, brand):
    assert sites.lookalike_of(value) == brand


def test_lookalike_by_label():
    assert sites.lookalike_of("example.com", label="Procore login") == "Procore"
    assert sites.lookalike_of("example.com", label="GC portal") is None


def test_site_card_facts():
    target = sites.grant_target_for_url("http://procore-bids.example.net/x")
    facts = sites.site_card_facts(None, target, "http://procore-bids.example.net/x")
    assert facts["encrypted"] is False and facts["lookalike_of"] == "Procore"
    assert facts["registrable_domain"] == "example.net" and facts["registered"] is False
    curated = sites.grant_target_for_site(site_row())
    assert sites.site_card_facts(site_row(), curated)["lookalike_of"] is None


# ── Provenance (5.5) ─────────────────────────────────────────────────────────


HARVEST_LINK = "https://app.buildingconnected.com/opportunities/abc123"


def tainted(tool, payload, **over):
    row = {"id": f"tr-{tool}-{len(str(payload))}", "user_id": USER, "conversation_id": CONV, "tainted": True,
           "tool_name": tool, "payload": payload, "taint_corpus": None, "created_at": NOW.isoformat()}
    row.update(over)
    return row


def prov(url, **kw):
    kw.setdefault("conversation_id", CONV)
    kw.setdefault("user_texts", [])
    return sites.provenance_detail(USER, RUN, url, **kw)


def test_rfp_harvest_link_is_email(db):
    db.tables.update(
        rfp_harvests=[{"id": "h1", "rfp_email_id": PROJECT2, "external_url": None,
                       "data": {"links": [{"key": "k1", "url": HARVEST_LINK, "status": "listed"}]}}],
        rfp_emails=[{"id": PROJECT2, "from_address": "bids@gc-builders.com"}],
    )
    result = prov(HARVEST_LINK)
    assert result == sites.ProvenanceResult("email", "rfp_harvest", "gc-builders.com")
    assert sites.needs_link_card(result.kind)


def test_rfp_created_project_bidding_link_is_email_not_bdr_record(db):
    db.tables.update(projects=[{"id": PROJECT, "bidding_url": HARVEST_LINK}],
                     rfp_created_projects=[{"project_id": PROJECT}])
    assert prov(HARVEST_LINK).kind == "email"
    assert prov(HARVEST_LINK + "/").kind == "email"  # trailing slash variant


def test_typed_project_field_is_bdr_record(db):
    db.tables["projects"] = [{"id": PROJECT, "bidding_url": "https://portal.gc-builders.com/bid/7"}]
    assert prov("https://portal.gc-builders.com/bid/7") == sites.ProvenanceResult("bdr_record", "project_field")
    assert not sites.needs_link_card("bdr_record")


def test_typed_beats_email(db):
    db.tables["rfp_harvests"] = [{"id": "h1", "rfp_email_id": None, "external_url": HARVEST_LINK}]
    result = prov(HARVEST_LINK, user_texts=[f"please open {HARVEST_LINK} and read the scope"])
    assert result == sites.ProvenanceResult("typed", "chat")


def test_typed_through_the_run(db, monkeypatch):
    monkeypatch.setattr(repo, "mail_user_texts", lambda user_id, run_id: [f"go to {HARVEST_LINK}"])
    assert sites.provenance(USER, RUN, HARVEST_LINK, conversation_id=CONV) == "typed"


def test_tainted_mail_result_is_email_with_sender(db):
    db.tables["assistant_tool_results"] = [
        tainted("email_read", {"from": {"address": "pm@gc-builders.com"}, "body": f"See {HARVEST_LINK} today"})]
    assert prov(HARVEST_LINK) == sites.ProvenanceResult("email", "mail_tool", "gc-builders.com")


def test_taint_corpus_is_searched(db):
    db.tables["assistant_tool_results"] = [
        tainted("email_search", {"n": 1}, taint_corpus=[{"f": "subject", "t": f"Bid: {HARVEST_LINK}"}])]
    assert prov(HARVEST_LINK).kind == "email"


def test_email_beats_bdr_record(db):
    db.tables["projects"] = [{"id": PROJECT, "bidding_url": HARVEST_LINK}]
    db.tables["assistant_tool_results"] = [tainted("read_mailbox", {"text": HARVEST_LINK})]
    assert prov(HARVEST_LINK).kind == "email"


def test_unknown_tainted_tool_counts_as_email(db):
    db.tables["assistant_tool_results"] = [tainted("some_future_tool", {"text": HARVEST_LINK})]
    assert prov(HARVEST_LINK).kind == "email"


def test_search_and_page(db):
    assert prov(HARVEST_LINK, search_urls=[HARVEST_LINK]).kind == "search"
    assert prov(HARVEST_LINK, page_urls=["HTTPS://APP.BUILDINGCONNECTED.COM/opportunities/abc123"]).kind == "page"
    db.tables["assistant_tool_results"] = [tainted("user_browser_status", {"result": HARVEST_LINK})]
    assert prov(HARVEST_LINK).kind == "page"
    db.tables["assistant_tool_results"] = [tainted("web_fetch", {"result": HARVEST_LINK})]
    assert prov(HARVEST_LINK).kind == "search"


def test_other_conversations_and_users_do_not_count(db):
    db.tables["assistant_tool_results"] = [
        tainted("email_read", {"body": HARVEST_LINK}, conversation_id=PROJECT),
        tainted("email_read", {"body": HARVEST_LINK}, user_id=BV2_USER2),
        tainted("email_read", {"body": HARVEST_LINK}, tainted=False),
    ]
    assert prov(HARVEST_LINK).kind == "unknown"


def test_unknown_and_bad_url(db):
    assert prov("https://random.example.org/page").kind == "unknown"
    assert prov("not a url").kind == "unknown"
    assert not sites.needs_link_card("unknown")


def test_lookup_failure_fails_closed(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "browser_url_email_sources", boom)
    assert prov(HARVEST_LINK) == sites.ProvenanceResult("email", "lookup_failed")
    monkeypatch.setattr(repo, "tainted_results", boom)
    assert prov(HARVEST_LINK).kind == "email"


def test_conversation_comes_from_the_run(db, monkeypatch):
    monkeypatch.setattr(repo, "conversation_for_run", lambda user_id, run_id: CONV)
    db.tables["assistant_tool_results"] = [tainted("email_read", {"body": HARVEST_LINK})]
    assert sites.provenance(USER, RUN, HARVEST_LINK, user_texts=[]) == "email"


# ── Grants (5.2, 5.4) ────────────────────────────────────────────────────────


def test_grant_curated_site_uses_the_registry_scope(db):
    out = grants.grant_site(USER, "procore", scope="origin", value="https://evil.example.com", path_prefix="/x",
                            granted_via="chat_card", provenance="typed")
    assert out["grant"]["scope"] == "domain" and out["grant"]["value"] == "procore.com"
    assert out["grant"]["label"] == "Procore" and out["waiting_admissions"] == []
    assert len(db.tables["assistant_browser_sites"]) == 4  # no ad hoc row


def test_grant_ad_hoc_domain_and_waiting_admissions(db):
    db.tables["assistant_browser_admissions"] = [admission_row(start_site_key="domain:gc-builders.com")]
    out = grants.grant_site(USER, "domain:gc-builders.com", scope="domain", value="gc-builders.com",
                            path_prefix=None, granted_via="chat_card", provenance="email",
                            ad_hoc_label="GC Builders\x00 portal")
    assert out["waiting_admissions"] == [admission_row()["id"]]
    row = next(s for s in db.tables["assistant_browser_sites"] if s["site_key"] == "domain:gc-builders.com")
    assert row["kind"] == "ad_hoc" and row["label"] == "GC Builders portal"
    assert grants.active_grant(USER, "domain:gc-builders.com")["version"] == 1


def test_grant_multi_tenant_needs_origin_and_prefix(db):
    with pytest.raises(grants.GrantRefused) as info:
        grants.grant_site(USER, "domain:sharepoint.com", scope="domain", value="sharepoint.com", path_prefix=None,
                          granted_via="chat_card", provenance="typed")
    assert info.value.code == "invalid"
    target = sites.grant_target_for_url("https://g3.sharepoint.com/sites/Bids/Shared%20Documents")
    with pytest.raises(grants.GrantRefused):
        grants.grant_site(USER, target.site_key, scope="origin", value=target.value, path_prefix="/",
                          granted_via="chat_card", provenance="typed")
    with pytest.raises(grants.GrantRefused):
        grants.grant_site(USER, target.site_key, scope="origin", value=target.value, path_prefix="/sites",
                          granted_via="chat_card", provenance="typed")
    out = grants.grant_site(USER, target.site_key, scope="origin", value=target.value,
                            path_prefix=target.path_prefix, granted_via="chat_card", provenance="typed")
    assert out["grant"]["path_prefix"] == "/sites/Bids"
    assert sites.site_for_url(USER, "https://g3.sharepoint.com/sites/Bids/a")["site_key"] == target.site_key
    assert sites.site_for_url(USER, "https://g3.sharepoint.com/sites/Other/a") is None


def test_grant_refusals(db):
    cases = [
        dict(site_key="procore", granted_via="model"),
        dict(site_key="procore", provenance="made_up"),
        dict(site_key="Bad Key"),
        dict(site_key="nosuchsite"),
        dict(site_key="domain:other.com", value="gc-builders.com"),  # key does not match the value
        dict(site_key="domain:procore.com", value="procore.com"),    # shadowing a curated site
        dict(site_key="origin:gc-builders.com", scope="origin", value="https://gc-builders.com"),  # widen check
        dict(site_key="domain:gc-builders.com", scope="origin", value="not an origin"),
    ]
    codes = []
    for case in cases:
        args = dict(scope="domain", value="gc-builders.com", path_prefix=None, granted_via="chat_card",
                     provenance="typed")
        key = case.pop("site_key")
        args.update(case)
        with pytest.raises(grants.GrantRefused) as info:
            grants.grant_site(USER, key, **args)
        codes.append(info.value.code)
        assert isinstance(info.value, ValueError)
    assert codes == ["invalid", "invalid", "invalid", "unknown_site", "invalid", "invalid", "invalid", "invalid"]
    assert db.tables.get("assistant_browser_site_grants", []) == []


def test_grant_rpc_refusals_map_to_codes(db):
    db.bv2_ineligible = {USER}
    with pytest.raises(grants.GrantRefused) as info:
        grants.grant_site(USER, "procore", scope="domain", value="procore.com", path_prefix=None,
                          granted_via="settings", provenance="settings")
    assert info.value.code == "ineligible"
    db.tables["assistant_browser_sites"][0]["enabled"] = False
    with pytest.raises(grants.GrantRefused) as info:
        grants.grant_site(USER, "procore", scope="domain", value="procore.com", path_prefix=None,
                          granted_via="settings", provenance="settings")
    assert info.value.code == "site_disabled"


def test_revoke_and_list(db):
    grant = grant_row(id=PROJECT)
    db.tables["assistant_browser_site_grants"] = [grant, grant_row(id=PROJECT2, site_key="smartbid",
                                                                   value="smartbidnet.com", status="revoked")]
    listed = grants.list_grants(USER)
    assert [g["id"] for g in listed] == [PROJECT, PROJECT2]
    assert listed[0]["label"] == "Procore" and "username" not in listed[0]
    with pytest.raises(grants.GrantRefused) as info:
        grants.revoke_grant(USER, PROJECT, 5)
    assert info.value.code == "version_conflict"
    with pytest.raises(grants.GrantRefused) as info:
        grants.revoke_grant(USER, PROJECT, "x")
    assert info.value.code == "invalid"
    assert grants.revoke_grant(USER, PROJECT, 1) == {"version": 2, "canceled_jobs": 0, "sessions_removed": 0}
    assert grants.active_grant(USER, "procore") is None
    assert grants.active_grant(USER, "Bad Key") is None


def test_active_grant_for_url(db):
    db.tables["assistant_browser_site_grants"] = [grant_row()]
    site, grant = grants.active_grant_for_url(USER, "https://app.procore.com/x")
    assert site["site_key"] == "procore" and grant["site_key"] == "procore"
    assert grants.active_grant_for_url(USER, "https://unregistered.example.com/") == (None, None)
    site, grant = grants.active_grant_for_url(BV2_USER2, "https://app.procore.com/x")
    assert site is not None and grant is None


def test_extend_job_scope(db):
    db.tables["assistant_browser_sites"].append(
        site_row(site_key="origin:g3.sharepoint.com:pabc", kind="ad_hoc", registrable_domains=[],
                 exact_origins=["https://g3.sharepoint.com"], path_prefix="/sites/Bids"))
    db.tables.update(
        assistant_browser_site_grants=[grant_row(site_key="smartbid", value="smartbidnet.com"),
                                       grant_row(site_key="origin:g3.sharepoint.com:pabc", scope="origin",
                                                 value="https://g3.sharepoint.com", path_prefix="/sites/Bids")],
        assistant_browser_admissions=[admission_row(site_scope=[{"site_key": "procore"}])],
        browser_agent_jobs=[bv2_job_row()],
    )
    assert grants.extend_job_scope(USER, BV2_JOB, "smartbid") == 2
    with pytest.raises(grants.GrantRefused) as info:
        grants.extend_job_scope(USER, BV2_JOB, "origin:g3.sharepoint.com:pabc")
    assert info.value.code == "multi_tenant"
    assert grants.extend_job_scope(USER, BV2_JOB, "origin:g3.sharepoint.com:pabc", allow_multi_tenant=True) == 3
    with pytest.raises(grants.GrantRefused) as info:
        grants.extend_job_scope(USER, BV2_JOB, "autodesk")
    assert info.value.code == "no_grant"
    with pytest.raises(grants.GrantRefused) as info:
        grants.extend_job_scope(USER, BV2_JOB, "nosuchsite")
    assert info.value.code == "unknown_site"
    db.tables["browser_agent_jobs"][0]["status"] = "failed"
    with pytest.raises(grants.GrantRefused) as info:
        grants.extend_job_scope(USER, BV2_JOB, "smartbid")
    assert info.value.code == "job_changed"


# ── Phase 6 names and hygiene ────────────────────────────────────────────────


def test_phase6_names_still_answer_for_the_retired_type():
    url = "https://app.procore.com/123/project/rfi/list"
    assert sites.validate_url("procore", url) == url
    assert sites.read_origin("procore", url) == "https://app.procore.com"
    assert sites.credential_allows_url({"origins": ["https://app.procore.com"]}, "procore", url)
    assert not sites.credential_allows_url({}, "procore", url)
    with pytest.raises(ValueError):
        sites.site("buildingconnected")


def test_no_em_dash_and_no_table_access_in_the_modules():
    for module in (sites, grants):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert EM_DASH not in text
        assert ".table(" not in text and "get_supabase" not in text
