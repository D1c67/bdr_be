"""Pure policy helpers for the RFP email intake (services/rfp_email_auth):
the Authentication-Results parser, the keyword gate, Message-ID
normalization, rule validation and the authorization precedence matrix from
docs/RFP_EMAIL_INGESTION.md sections 3.3, 3.4, 3.1, 3.7 and 3.8.
"""

import pytest

from app.services import rfp_email_auth as auth


def _hdr(value, name="Authentication-Results"):
    return {"name": name, "value": value}


# Exchange Online's real shape: no authserv-id, compauth at the end.
EXO_PASS = (
    "spf=pass (sender IP is 203.0.113.5) smtp.mailfrom=gc.example; "
    "dkim=pass (signature was verified) header.d=gc.example;"
    "dmarc=pass action=none header.from=gc.example;compauth=pass reason=100"
)
EXO_DMARC_FAIL = (
    "spf=fail (sender IP is 198.51.100.7) smtp.mailfrom=gc.example; "
    "dkim=none (message not signed) header.d=none;"
    "dmarc=fail action=oreject header.from=gc.example;compauth=fail reason=000"
)
EXO_ALL_NONE = (
    "spf=none (sender IP is 198.51.100.7) smtp.mailfrom=gc.example; "
    "dkim=none (message not signed) header.d=none;"
    "dmarc=none action=none header.from=gc.example;compauth=none reason=405"
)
EXO_SOFTFAIL_ONLY = (
    "spf=softfail (sender IP is 198.51.100.7) smtp.mailfrom=gc.example; "
    "dkim=none (message not signed) header.d=none;"
    "dmarc=none action=none header.from=gc.example;compauth=softpass reason=200"
)
# RFC 8601 shape with the tenant authserv-id up front.
TENANT_AUTHSERV = (
    "BN8NAM11FT041.mail.protection.outlook.com; spf=pass smtp.mailfrom=gc.example; "
    "dkim=none header.d=none; dmarc=none action=none header.from=gc.example"
)
# A header the sender added themselves, claiming a pass.
FORGED = "mail.attacker.example; spf=pass smtp.mailfrom=gc.example; dkim=pass; dmarc=pass"


# ── Authentication-Results ─────────────────────────────────────────────────────


def test_parser_pass_reads_every_token():
    res = auth.parse_authentication_results([_hdr(EXO_PASS)])
    assert (res.spf, res.dkim, res.dmarc, res.compauth) == ("pass", "pass", "pass", "pass")
    assert res.raw == EXO_PASS
    assert res.verdict == "pass"


def test_parser_dmarc_fail_is_fail():
    res = auth.parse_authentication_results([_hdr(EXO_DMARC_FAIL)])
    assert res.dmarc == "fail"
    assert res.verdict == "fail"


def test_parser_all_none_is_fail():
    res = auth.parse_authentication_results([_hdr(EXO_ALL_NONE)])
    assert (res.spf, res.dkim, res.dmarc) == ("none", "none", "none")
    assert res.verdict == "fail"


def test_parser_softfail_alone_is_fail_but_softfail_with_aligned_dkim_pass_passes():
    assert auth.parse_authentication_results([_hdr(EXO_SOFTFAIL_ONLY)]).verdict == "fail"
    with_dkim = EXO_SOFTFAIL_ONLY.replace(
        "dkim=none (message not signed) header.d=none",
        "dkim=pass (signature was verified) header.d=gc.example",
    )
    res = auth.parse_authentication_results([_hdr(with_dkim)])
    assert res.spf == "softfail"
    assert res.dkim_domain == "gc.example"
    assert res.verdict == "pass"
    # The same DKIM pass for some other domain is evidence about that domain
    # only: not aligned with the From, so it counts for nothing.
    unaligned = with_dkim.replace("header.d=gc.example", "header.d=attacker.example")
    res = auth.parse_authentication_results([_hdr(unaligned)])
    assert res.dkim == "pass" and res.dkim_domain == "attacker.example"
    assert res.verdict == "fail"


def test_parser_ignores_text_inside_header_comments():
    # RFC 8601 comments are free text. A `header.d=gc.example` or a
    # `dmarc=pass` planted inside one is not a property or a result: the
    # real (unaligned) props after the comment are what count.
    planted = (
        "spf=pass (sender IP is 203.0.113.5) smtp.mailfrom=attacker.example; "
        "dkim=pass (header.d=gc.example dmarc=pass (nested header.d=gc.example)) "
        "header.d=attacker.example; dmarc=none action=none header.from=gc.example;"
        "compauth=none reason=405"
    )
    res = auth.parse_authentication_results([_hdr(planted)], from_address="pm@gc.example")
    assert res.spf_domain == "attacker.example" and res.dkim_domain == "attacker.example"
    assert res.dmarc == "none"
    assert res.verdict == "fail"
    # A comment never hides a real result either.
    assert auth.parse_authentication_results([_hdr(EXO_PASS)]).verdict == "pass"


def test_parser_aligned_spf_alone_passes_when_dmarc_absent():
    # A DKIM-less small GC with intact SPF on its OWN domain is legitimate
    # (doc 3.3): smtp.mailfrom aligns with the From.
    res = auth.parse_authentication_results([_hdr(TENANT_AUTHSERV)])
    assert (res.spf, res.dkim, res.dmarc) == ("pass", "none", "none")
    assert res.compauth is None
    assert res.spf_domain == "gc.example" and res.dkim_domain is None
    assert res.verdict == "pass"
    # With the message's own From address supplied, that is the domain
    # the alignment is judged against, header.from being only the fallback.
    assert auth.parse_authentication_results(
        [_hdr(TENANT_AUTHSERV)], from_address="Estimating@GC.example").verdict == "pass"
    assert auth.parse_authentication_results(
        [_hdr(TENANT_AUTHSERV)], from_address="bids@mail.gc.example").verdict == "pass"
    assert auth.parse_authentication_results(
        [_hdr(TENANT_AUTHSERV)], from_address="pm@othergc.example").verdict == "fail"


# ── Alignment (security review 2026-09-30): a pass is worth nothing unless
# it is for the From domain. A DMARC-less GC domain used to be spoofable by
# anyone with SPF or DKIM on a domain of their own. ────────────────────────


def test_unaligned_spf_pass_with_forged_from_is_fail():
    # MAIL FROM at the attacker's own SPF-valid domain, From forged at a GC
    # domain that publishes no DMARC record (dmarc=none): the exact spoof.
    spoof = (
        "spf=pass (sender IP is 198.51.100.9) smtp.mailfrom=attacker.example; "
        "dkim=none (message not signed) header.d=none;"
        "dmarc=none action=none header.from=gc.example;compauth=fail reason=001"
    )
    res = auth.parse_authentication_results([_hdr(spoof)], from_address="estimating@gc.example")
    assert res.spf == "pass" and res.spf_domain == "attacker.example"
    assert res.verdict == "fail"
    # Even without Exchange's compauth verdict, the unaligned pass fails.
    no_compauth = spoof.replace(";compauth=fail reason=001", "")
    res = auth.parse_authentication_results(
        [_hdr("BN8NAM11FT041.mail.protection.outlook.com; " + no_compauth)],
        from_address="estimating@gc.example",
    )
    assert res.compauth is None and res.verdict == "fail"
    # An address-shaped smtp.mailfrom is reduced to its domain first.
    bounce = spoof.replace("smtp.mailfrom=attacker.example", "smtp.mailfrom=bounce@gc.example")
    bounce = bounce.replace("compauth=fail reason=001", "compauth=softpass reason=200")
    res = auth.parse_authentication_results([_hdr(bounce)], from_address="estimating@gc.example")
    assert res.spf_domain == "gc.example" and res.verdict == "pass"


def test_unaligned_dkim_pass_with_forged_from_is_fail():
    spoof = (
        "spf=none (sender IP is 198.51.100.9) smtp.mailfrom=attacker.example; "
        "dkim=pass (signature was verified) header.d=attacker.example;"
        "dmarc=none action=none header.from=gc.example;compauth=none reason=405"
    )
    res = auth.parse_authentication_results([_hdr(spoof)], from_address="estimating@gc.example")
    assert res.dkim == "pass" and res.dkim_domain == "attacker.example"
    assert res.verdict == "fail"


def test_compauth_is_the_primary_aligned_signal():
    # Exchange's composite verdict already checked alignment: pass wins even
    # with no other pass token, fail loses even with an aligned SPF pass.
    only_compauth = (
        "spf=none smtp.mailfrom=gc.example; dkim=none header.d=none;"
        "dmarc=none action=none header.from=gc.example;compauth=pass reason=109"
    )
    assert auth.parse_authentication_results([_hdr(only_compauth)]).verdict == "pass"
    compauth_fail = (
        "spf=pass smtp.mailfrom=gc.example; dkim=none header.d=none;"
        "dmarc=none action=none header.from=gc.example;compauth=fail reason=001"
    )
    res = auth.parse_authentication_results([_hdr(compauth_fail)], from_address="pm@gc.example")
    assert res.verdict == "fail"
    assert auth.auth_fail_reason("pass", "none", "none", "fail", tenant_header_present=True) == "compauth_fail"


def test_domains_aligned_is_relaxed_on_a_label_boundary():
    a = auth.domains_aligned
    assert a("gc.example", "gc.example")
    assert a("mail.gc.example", "gc.example")
    assert a("gc.example", "bids.gc.example")
    assert a("GC.Example.", "gc.example")
    assert not a("attacker.example", "gc.example")
    assert not a("notgc.example", "gc.example")
    assert not a("example", "gc.example")      # a bare top-level label never aligns
    assert not a(None, "gc.example") and not a("gc.example", None) and not a("", "")


def test_auth_fail_reason_names_the_unaligned_case():
    r = auth.auth_fail_reason
    assert r("pass", None, None, None, tenant_header_present=False) == "no_tenant_auth_header"
    assert r("pass", "pass", "fail", "fail", tenant_header_present=True) == "dmarc_fail"
    assert r("pass", "none", "none", "fail", tenant_header_present=True) == "compauth_fail"
    assert r("pass", "none", "none", "none", tenant_header_present=True) == "unaligned_pass"
    assert r("none", "pass", "none", None, tenant_header_present=True) == "unaligned_pass"
    assert r("none", "none", "none", "none", tenant_header_present=True) == "no_auth_pass"
    assert r("softfail", "temperror", None, None, tenant_header_present=True) == "no_auth_pass"


def test_parser_missing_header_is_fail_with_nothing_stored():
    res = auth.parse_authentication_results([_hdr("v=1", name="Received")])
    assert res == auth.AuthResults(None, None, None, None, None, "fail")
    assert auth.parse_authentication_results([]).verdict == "fail"
    assert auth.parse_authentication_results(None).verdict == "fail"


def test_parser_ignores_forged_non_tenant_header():
    assert auth.parse_authentication_results([_hdr(FORGED)]).verdict == "fail"
    # Exchange prepends its header on arrival, so the tenant's is always the
    # top-most one. A header the sender wrote sits below it and is never
    # consulted, whatever it claims and whatever shape it takes, Exchange's
    # own shape included.
    res = auth.parse_authentication_results([_hdr(EXO_DMARC_FAIL), _hdr(FORGED)])
    assert res.raw == EXO_DMARC_FAIL
    assert res.verdict == "fail"
    res = auth.parse_authentication_results([_hdr(EXO_DMARC_FAIL), _hdr(EXO_PASS)])
    assert res.raw == EXO_DMARC_FAIL
    assert res.verdict == "fail"
    res = auth.parse_authentication_results([_hdr(EXO_PASS), _hdr(FORGED)])
    assert res.raw == EXO_PASS
    assert res.verdict == "pass"


def test_parser_fails_closed_when_the_top_header_is_not_the_tenants():
    # Only the top-most header is trusted. If it is not the tenant's, nothing
    # further down is either: the parser must not read on until it finds a
    # header it likes, because that is what would let a forged one through on
    # the day Exchange's own header is missing or takes a shape we don't know.
    for below in (EXO_PASS, EXO_DMARC_FAIL, TENANT_AUTHSERV):
        res = auth.parse_authentication_results([_hdr(FORGED), _hdr(below)])
        assert res.verdict == "fail"
        assert res.raw is None
    # Headers of other names above it are not a first header.
    res = auth.parse_authentication_results(
        [
            _hdr("v=1", name="Received"),
            _hdr(FORGED, name="ARC-Authentication-Results"),
            _hdr(EXO_PASS),
        ]
    )
    assert res.raw == EXO_PASS
    assert res.verdict == "pass"


def test_parser_header_name_is_case_insensitive_and_arc_is_not_it():
    assert auth.parse_authentication_results([_hdr(EXO_PASS, name="authentication-results")]).verdict == "pass"
    assert auth.parse_authentication_results([_hdr(EXO_PASS, name="ARC-Authentication-Results")]).verdict == "fail"


def test_verdict_from_results_policy_table():
    v = auth.verdict_from_results
    gc = dict(from_domain="gc.example")
    # SPF alone passes only when smtp.mailfrom aligns with the From domain.
    assert v("pass", None, None, tenant_header_present=True, spf_domain="gc.example", **gc) == "pass"
    assert v("pass", None, None, tenant_header_present=True, spf_domain="mail.gc.example", **gc) == "pass"
    assert v("pass", None, None, tenant_header_present=True, spf_domain="attacker.example", **gc) == "fail"
    assert v("pass", None, None, tenant_header_present=True, **gc) == "fail"      # domain unknown
    assert v("pass", None, None, tenant_header_present=True) == "fail"           # nothing to align
    # DKIM alone: header.d must align.
    assert v(None, "pass", None, tenant_header_present=True, dkim_domain="gc.example", **gc) == "pass"
    assert v(None, "pass", None, tenant_header_present=True, dkim_domain="attacker.example", **gc) == "fail"
    assert v(None, "pass", None, tenant_header_present=True) == "fail"
    # DMARC and compauth are Exchange's own aligned verdicts.
    assert v("fail", "pass", "pass", tenant_header_present=True) == "pass"
    assert v("pass", "pass", "fail", tenant_header_present=True, spf_domain="gc.example", **gc) == "fail"
    assert v("none", "none", "none", tenant_header_present=True, compauth="pass") == "pass"
    assert v("pass", "pass", "none", tenant_header_present=True, compauth="fail",
             spf_domain="gc.example", dkim_domain="gc.example", **gc) == "fail"
    assert v("temperror", "permerror", "none", tenant_header_present=True) == "fail"
    assert v("pass", "pass", "pass", tenant_header_present=False) == "fail"


# ── Keyword gate ───────────────────────────────────────────────────────────────


def test_keywords_word_boundaries():
    assert auth.keyword_hits("Your feedback", "Thanks Steve, see you") == []
    assert auth.keyword_hits("Invitation to Bid", "") == ["invitation", "bid"]
    assert auth.keyword_hits("", "the DB migration") == ["db"]
    assert auth.keyword_hits("", "VE options attached") == ["ve"]


def test_keywords_distinct_lowercased_first_seen_order():
    hits = auth.keyword_hits("RFP: Project X", "This project needs a bid. BID due Friday. rfp attached")
    assert hits == ["rfp", "project", "bid"]


def test_keywords_longest_alternative_wins():
    assert auth.keyword_hits("bidding", "") == ["bidding"]
    assert auth.keyword_hits("rebid requote", "") == ["rebid", "requote"]


def test_keywords_tolerate_none():
    assert auth.keyword_hits(None, None) == []


# ── Message-ID normalization ───────────────────────────────────────────────────


def test_normalize_message_id():
    n = auth.normalize_message_id
    assert n("  <ABC@Mail.Example>  ", mailbox="a@x.com", graph_id="g") == "abc@mail.example"
    assert n("abc@mail.example", mailbox="a@x.com", graph_id="g") == "abc@mail.example"
    assert n(None, mailbox="Bids@X.com", graph_id="G1") == "graph:bids@x.com:G1"
    assert n("<>", mailbox="a@x.com", graph_id="G1") == "graph:a@x.com:G1"


# ── Rule validation ────────────────────────────────────────────────────────────


def test_validate_rule_normalizes_and_accepts():
    assert auth.validate_rule("address", "  Bids@GC.Example ") == (
        "address", "bids@gc.example")
    assert auth.validate_rule("domain", "Bids.ExampleGC.com") == ("domain", "bids.examplegc.com")
    assert auth.validate_rule("address", "<bids@gc.example>") == ("address", "bids@gc.example")


def test_validate_rule_rejects_public_provider_domain_with_guidance():
    with pytest.raises(ValueError) as exc:
        auth.validate_rule("domain", "gmail.com")
    msg = str(exc.value)
    assert "gmail.com" in msg
    assert "full email address" in msg
    assert "public" in msg.lower()
    # A gmail ADDRESS is fine.
    assert auth.validate_rule("address", "estimator@gmail.com") == ("address", "estimator@gmail.com")


@pytest.mark.parametrize(
    "kind, value, fragment",
    [
        ("address", "not-an-address", "not a valid email address"),
        ("address", "", "Enter an email address"),
        ("domain", "", "Enter a domain"),
        ("domain", "someone@gc.example", "looks like an email address"),
        ("domain", "https://gc.example/bids", "bare domain"),
        ("domain", "localhost", "not a valid domain"),
        ("domain", "-bad.example", "not a valid domain"),
        ("mailbox", "x", "Choose a rule type"),
    ],
)
def test_validate_rule_messages(kind, value, fragment):
    with pytest.raises(ValueError) as exc:
        auth.validate_rule(kind, value)
    assert fragment in str(exc.value)


# ── Block validation (doc 3.1, migration 0133) ─────────────────────────────────


def test_validate_block_normalizes_and_accepts():
    assert auth.validate_block("address", "  NoReply@Spam.Example ") == (
        "address", "noreply@spam.example")
    assert auth.validate_block("domain", "SpamCo.Example") == ("domain", "spamco.example")
    assert auth.validate_block("address", "<noreply@spam.example>") == (
        "address", "noreply@spam.example")


def test_validate_block_rejects_a_public_provider_domain_for_the_opposite_reason():
    """Authorizing gmail.com would trust everyone there; BLOCKING it would
    silently drop every GC contact who uses it. Both are refused, and the
    sentence here points at the individual address."""
    with pytest.raises(ValueError) as exc:
        auth.validate_block("domain", "gmail.com")
    msg = str(exc.value)
    assert "gmail.com" in msg
    assert "individual email address" in msg
    # One gmail ADDRESS is exactly how a nuisance sender there is stopped.
    assert auth.validate_block("address", "nuisance@gmail.com") == (
        "address", "nuisance@gmail.com")


@pytest.mark.parametrize(
    "kind, value, fragment",
    [
        ("address", "not-an-address", "not a valid email address"),
        ("address", "", "Enter the email address to block"),
        ("domain", "", "Enter the domain to block"),
        ("domain", "someone@spam.example", "this sender only"),
        ("domain", "https://spam.example/x", "bare domain"),
        ("domain", "localhost", "not a valid domain"),
        ("domain", "-bad.example", "not a valid domain"),
        ("mailbox", "x", "Choose what to block"),
    ],
)
def test_validate_block_messages(kind, value, fragment):
    with pytest.raises(ValueError) as exc:
        auth.validate_block(kind, value)
    assert fragment in str(exc.value)


# ── Authorization precedence ───────────────────────────────────────────────────

INTERNAL = {"g3electrical.com"}


def _rule(id_, kind, value, method="general", locked=False):
    return {"id": id_, "kind": kind, "value": value, "method": method, "locked": locked}


# The one locked seed row left (0120, narrowed by 0124 and again by 0128),
# one locked gc_portal rule on a narrow subdomain (the shape the IT Admin adds
# for a GC's own portal, 0127), plus one locked ADDRESS rule that is not a
# real seed: it keeps the address-kind branches covered now that the seed is
# a domain rule.
SEED = [
    _rule("r-procore", "domain", "procoretech.com", "procore", True),
    _rule("r-portal", "domain", "bids.example.com", "gc_portal", True),
    _rule("r-procore-addr", "address", "invites@procore.example", "procore", True),
]


def test_locked_rules_give_their_method():
    r = auth.evaluate_authorization("noreply@procoretech.com", SEED, set(), INTERNAL)
    assert r == auth.AuthorizationResult(True, "domain", "r-procore", "procore")
    r = auth.evaluate_authorization("noreply@bids.example.com", SEED, set(), INTERNAL)
    assert r == auth.AuthorizationResult(True, "domain", "r-portal", "gc_portal")
    r = auth.evaluate_authorization("Invites@Procore.example", SEED, set(), INTERNAL)
    assert r == auth.AuthorizationResult(True, "address", "r-procore-addr", "procore")


def test_domain_rule_covers_subdomains_but_never_the_parent():
    # A rule on bids.example.com never widens to example.com itself.
    assert not auth.evaluate_authorization("x@example.com", SEED, set(), INTERNAL).authorized
    # Subdomains of a rule ARE covered (Procore's regional us02.procoretech.com).
    assert auth.evaluate_authorization("x@sub.bids.example.com", SEED, set(), INTERNAL).authorized
    assert auth.evaluate_authorization("x@app.procoretech.com", SEED, set(), INTERNAL).method == "procore"
    # Only on a label boundary.
    assert not auth.evaluate_authorization("x@fakeprocoretech.com", SEED, set(), INTERNAL).authorized


def test_gc_domain_is_organic_with_no_rule_id():
    r = auth.evaluate_authorization("pm@gc.example", SEED, {"gc.example"}, INTERNAL)
    assert r == auth.AuthorizationResult(True, "gc_domain", None, "organic")


def test_user_rule_is_general_whatever_its_column_says():
    rules = SEED + [_rule("r-user", "domain", "newgc.example", "procore", False)]
    r = auth.evaluate_authorization("a@newgc.example", rules, set(), INTERNAL)
    assert r == auth.AuthorizationResult(True, "domain", "r-user", "general")


def test_gc_domain_also_a_user_rule_gc_wins():
    rules = SEED + [_rule("r-user", "domain", "gc.example")]
    r = auth.evaluate_authorization("pm@gc.example", rules, {"gc.example"}, INTERNAL)
    assert r.kind == "gc_domain" and r.method == "organic" and r.rule_id is None


def test_locked_rule_also_a_gc_domain_locked_wins():
    r = auth.evaluate_authorization(
        "bot@procoretech.com", SEED, {"procoretech.com"}, INTERNAL
    )
    assert r.kind == "domain" and r.method == "procore" and r.rule_id == "r-procore"


def test_locked_address_beats_locked_domain():
    # The address rule grants a different method from the domain rule it sits
    # inside, so the method proves which rule won.
    rules = SEED + [_rule("r-addr", "address", "special@procoretech.com", "gc_portal", True)]
    r = auth.evaluate_authorization("special@procoretech.com", rules, set(), INTERNAL)
    assert r.rule_id == "r-addr" and r.method == "gc_portal"


def test_user_address_beats_user_domain():
    rules = [_rule("r-d", "domain", "gc.example"), _rule("r-a", "address", "pm@gc.example")]
    r = auth.evaluate_authorization("pm@gc.example", rules, set(), INTERNAL)
    assert r.rule_id == "r-a" and r.kind == "address"


def test_gc_contact_at_gmail_authorizes_exact_address_only():
    # The whole provider domain never authorizes, even when a GC contact lives there.
    assert not auth.evaluate_authorization("anyone@gmail.com", SEED, {"gmail.com"}, INTERNAL).authorized
    rules = SEED + [_rule("r-gm", "address", "bob.gc@gmail.com")]
    r = auth.evaluate_authorization("bob.gc@gmail.com", rules, {"gmail.com"}, INTERNAL)
    assert r == auth.AuthorizationResult(True, "address", "r-gm", "general")
    assert not auth.evaluate_authorization("eve@gmail.com", rules, {"gmail.com"}, INTERNAL).authorized


def test_internal_domain_never_authorizes():
    rules = SEED + [_rule("r-int", "domain", "g3electrical.com"),
                    _rule("r-int-a", "address", "tmoore@g3electrical.com")]
    gc = {"g3electrical.com"}
    assert not auth.evaluate_authorization("tmoore@g3electrical.com", rules, gc, INTERNAL).authorized


def test_unknown_sender_and_junk_input():
    assert not auth.evaluate_authorization("who@nowhere.example", SEED, set(), INTERNAL).authorized
    assert not auth.evaluate_authorization("", SEED, set(), INTERNAL).authorized
    assert not auth.evaluate_authorization(None, SEED, set(), INTERNAL).authorized
    assert not auth.evaluate_authorization("no-at-sign", SEED, {"no-at-sign"}, INTERNAL).authorized


def test_method_for_rule_and_constants():
    assert auth.method_for_rule(None, "gc_domain") == "organic"
    assert auth.method_for_rule(None, "override") == "nonorganic"
    assert auth.method_for_rule(_rule("x", "domain", "d", "procore", True), "domain") == "procore"
    assert auth.method_for_rule(_rule("x", "domain", "d", "procore", False), "domain") == "general"
    assert set(auth.RULE_METHODS) < set(auth.INVITATION_METHODS)
    assert "nonorganic" in auth.INVITATION_METHODS and "organic" in auth.INVITATION_METHODS
    # 0124: BuildingConnected and NGEM mail is sanitized out at listing time
    # and neither is a method any more. 0127: gc_portal joined both lists.
    # 0128: PlanHub went the same way as 0124's two (blocked at listing time,
    # no longer a method). 0129: pipelinesuite joined both lists, after
    # procore (docs/RFP_PIPELINESUITE.md section 4).
    assert set(auth.INVITATION_METHODS) == {
        "organic", "procore", "pipelinesuite", "gc_portal", "general", "nonorganic"
    }
    assert set(auth.RULE_METHODS) == {"procore", "pipelinesuite", "gc_portal", "general"}
    assert auth.METHOD_GC_PORTAL == "gc_portal"
    assert auth.METHOD_PIPELINESUITE == "pipelinesuite"
    assert auth.INVITATION_METHODS.index("pipelinesuite") == auth.INVITATION_METHODS.index("procore") + 1
    assert auth.RULE_METHODS.index("pipelinesuite") == auth.RULE_METHODS.index("procore") + 1
    for gone in ("buildingconnected", "ngem", "planhub"):
        assert gone not in auth.INVITATION_METHODS and gone not in auth.RULE_METHODS
        assert not hasattr(auth, f"METHOD_{gone.upper()}")


# ── gc_portal: a GC's own bidding portal (2026-09-16) ────────────────────────


def test_locked_gc_portal_rule_outranks_the_same_gcs_organic_match():
    """The portal rule sits on the GC's own domain, which is also a GC-contact
    domain. Locked rules come first, so the method is gc_portal and the
    harvest step can pick that GC's scraper; without the rule the same sender
    is plain organic."""
    rules = SEED + [_rule("r-portal", "domain", "gc.example", "gc_portal", True)]
    r = auth.evaluate_authorization("bids@gc.example", rules, {"gc.example"}, INTERNAL)
    assert r == auth.AuthorizationResult(True, "domain", "r-portal", "gc_portal")
    # Subdomains of the portal domain are covered (portal.gc.example).
    r = auth.evaluate_authorization("noreply@portal.gc.example", rules, {"gc.example"}, INTERNAL)
    assert r.method == "gc_portal" and r.rule_id == "r-portal"
    # Without the rule the GC domain is organic, as before.
    r = auth.evaluate_authorization("bids@gc.example", SEED, {"gc.example"}, INTERNAL)
    assert r == auth.AuthorizationResult(True, "gc_domain", None, "organic")


def test_a_non_locked_gc_portal_rule_is_still_general():
    rules = SEED + [_rule("r-user", "domain", "gc.example", "gc_portal", False)]
    r = auth.evaluate_authorization("bids@gc.example", rules, set(), INTERNAL)
    assert r.method == "general"
    assert auth.method_for_rule(_rule("x", "domain", "d", "gc_portal", True), "domain") == "gc_portal"


# ── pipelinesuite: a GC whose plan room is a PipelineSuite portal (2026-09-16,
# docs/RFP_PIPELINESUITE.md section 1; 0129 seeds cgandbinc.com and
# shfcontracting.com as locked domain rules) ──────────────────────────────────


def test_locked_pipelinesuite_rule_outranks_the_same_gcs_organic_match():
    """The invitation comes from the GC's own domain (through SendGrid), which
    is also a GC-contact domain. The locked rule on that domain wins over the
    organic match, so the row is labeled pipelinesuite and the harvest step
    picks the PipelineSuite harvester; without the rule the same sender is
    plain organic."""
    rules = SEED + [_rule("r-ps", "domain", "cgandbinc.com", "pipelinesuite", True)]
    r = auth.evaluate_authorization("bids@cgandbinc.com", rules, {"cgandbinc.com"}, INTERNAL)
    assert r == auth.AuthorizationResult(True, "domain", "r-ps", "pipelinesuite")
    # Subdomains of the rule's domain are covered on a label boundary.
    r = auth.evaluate_authorization("noreply@mail.cgandbinc.com", rules, {"cgandbinc.com"}, INTERNAL)
    assert r.method == "pipelinesuite" and r.rule_id == "r-ps"
    assert not auth.evaluate_authorization("x@fakecgandbinc.com", rules, set(), INTERNAL).authorized
    # Without the rule the GC domain is organic, as before.
    r = auth.evaluate_authorization("bids@cgandbinc.com", SEED, {"cgandbinc.com"}, INTERNAL)
    assert r == auth.AuthorizationResult(True, "gc_domain", None, "organic")


def test_a_non_locked_pipelinesuite_rule_is_still_general():
    rules = SEED + [_rule("r-user", "domain", "shfcontracting.com", "pipelinesuite", False)]
    r = auth.evaluate_authorization("bids@shfcontracting.com", rules, set(), INTERNAL)
    assert r.method == "general"
    assert auth.method_for_rule(_rule("x", "domain", "d", "pipelinesuite", True), "domain") == "pipelinesuite"
    assert auth.method_for_rule(_rule("x", "domain", "d", "pipelinesuite", False), "domain") == "general"


# ── Domain rules cover subdomains (live finding 2026-09-10: Procore sends from
# us02.procoretech.com, the seed rule is procoretech.com) ─────────────────────


def test_domain_rule_covers_subdomains_on_a_label_boundary():
    from app.services.rfp_email_auth import domain_covered_by, rule_matches

    assert domain_covered_by("us02.procoretech.com", "procoretech.com")
    assert domain_covered_by("procoretech.com", "procoretech.com")
    assert not domain_covered_by("notprocoretech.com", "procoretech.com")
    assert not domain_covered_by("example.com", "bids.example.com")
    rule = {"kind": "domain", "value": "procoretech.com", "method": "procore", "locked": True}
    assert rule_matches(rule, "boyd_martin@us02.procoretech.com")
    assert not rule_matches(rule, "x@evilprocoretech.com")


def test_evaluate_authorization_subdomain_gets_the_platform_method():
    from app.services.rfp_email_auth import evaluate_authorization

    rules = [{"id": "r1", "kind": "domain", "value": "procoretech.com", "method": "procore", "locked": True}]
    out = evaluate_authorization("gc_notifications@us02.procoretech.com", rules, set(), {"g3electrical.com"})
    assert out.authorized and out.method == "procore" and out.rule_id == "r1"
