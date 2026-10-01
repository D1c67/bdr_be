"""Pure decision helpers for the RFP email intake (docs/RFP_EMAIL_INGESTION.md).

Everything here is I/O free so the security-relevant policy can be unit
tested exhaustively and reused by both the poller (services/rfp_email_ingest)
and the settings router without either dragging in Supabase or Graph:

- parse_authentication_results: reads the Authentication-Results header that
  Exchange Online stamped and applies the doc 3.3 pass/fail policy. Only the
  tenant's own header counts; a sender can forge any other one.
- keyword_hits: the cheap word-boundary floor from doc 3.4.
- normalize_message_id: one identity per message across mailboxes (doc 3.1).
- validate_rule: the settings form's server-side validation (doc 9), including
  the refusal to authorize a whole public mail provider (doc 3.7).
- validate_block: the same for a blocked-sender row (doc 3.1), where a whole
  public provider is refused because blocking it would drop every GC contact
  who uses it.
- evaluate_authorization: the sender precedence from docs 3.7 and 3.8.

The From address is the RFC 5322 From, already authenticated by the auth
step, never the display name and never Reply-To.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Invitation methods (doc 3.8) ─────────────────────────────────────────────
# One platform, Procore, the GC-portal source, plus the three non-platform
# sources. BuildingConnected and NGEM (IonWave) were methods until 2026-09-15
# and PlanHub until 2026-09-16; their mail is now sanitized out at listing
# time (RFP_EMAIL_INGESTION_BLOCKED_DOMAINS) like internal and vendor
# senders, and migrations 0124 and 0128 narrowed both check constraints.
# Migration 0127 added gc_portal and 0129 added pipelinesuite (2026-09-16).

METHOD_ORGANIC = "organic"                 # the sender is a known GC domain
METHOD_PROCORE = "procore"
# PipelineSuite (PreconSuite): a GC's own plan room at <gc>.pipelinesuite.com.
# The mail comes from the GC's own domain, so the method is granted by a
# locked domain rule on that domain (0129 seeds cgandbinc.com and
# shfcontracting.com); the harvester needs nothing but the email body
# (portal host, Project ID, Security Key). docs/RFP_PIPELINESUITE.md.
METHOD_PIPELINESUITE = "pipelinesuite"
# A GC that invites through its own bidding portal. Every such portal is
# different, so the harvest step picks the scraper by the sender's domain
# (rfp_harvest.GC_PORTAL_SCRAPERS); the method itself is granted by a locked
# rule on that domain, which is how it outranks the GC's organic match.
METHOD_GC_PORTAL = "gc_portal"
METHOD_GENERAL = "general"                 # a user-added rule
METHOD_NONORGANIC = "nonorganic"           # a human "continue" on an unauthorized sender

INVITATION_METHODS = (
    METHOD_ORGANIC,
    METHOD_PROCORE,
    METHOD_PIPELINESUITE,
    METHOD_GC_PORTAL,
    METHOD_GENERAL,
    METHOD_NONORGANIC,
)

# Methods a rule row may grant (the rfp_authorized_senders.method check).
RULE_METHODS = (
    METHOD_PROCORE,
    METHOD_PIPELINESUITE,
    METHOD_GC_PORTAL,
    METHOD_GENERAL,
)

RULE_KINDS = ("address", "domain")

# Authorization kinds stored on rfp_emails.authorization_kind.
KIND_ADDRESS = "address"
KIND_DOMAIN = "domain"
KIND_GC_DOMAIN = "gc_domain"
KIND_OVERRIDE = "override"

# ── Public mailbox providers (doc 3.7) ───────────────────────────────────────
# A GC contact at one of these never authorizes the whole provider, and a
# `domain` rule for one is refused outright: authorizing gmail.com would
# authorize every Gmail user on earth.

PUBLIC_MAILBOX_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "mail.com",
        "ymail.com",
    }
)


def is_public_mailbox_domain(domain: str | None) -> bool:
    return (domain or "").strip().lower() in PUBLIC_MAILBOX_DOMAINS


# ── Authentication-Results (doc 3.3) ─────────────────────────────────────────

AUTH_PASS = "pass"
AUTH_FAIL = "fail"

_AUTH_HEADER_NAME = "authentication-results"
_TENANT_AUTHSERV_SUFFIX = "mail.protection.outlook.com"
# `method=result`, where the result is the bare token before any comment or
# property (`spf=pass (sender IP is ...) smtp.mailfrom=...`).
_AUTH_METHOD_RE = re.compile(r"(?<![\w.-])(spf|dkim|dmarc|compauth)\s*=\s*([a-z]+)", re.I)
# The domain each pass was judged for: `smtp.mailfrom=` (what SPF checked)
# and `header.d=` (what DKIM signed). `header.from=` is only the fallback for
# the From domain when the caller has no From address. A pass is worth
# nothing unless its domain aligns with the From domain (doc 3.3).
_AUTH_PROP_RE = re.compile(
    r"(?<![\w.-])(smtp\.mailfrom|header\.d|header\.from)\s*=\s*([^\s;()]+)", re.I
)


@dataclass(frozen=True)
class AuthResults:
    spf: str | None
    dkim: str | None
    dmarc: str | None
    compauth: str | None
    raw: str | None          # the full tenant header, for the review screen
    verdict: str             # AUTH_PASS | AUTH_FAIL
    spf_domain: str | None = None    # smtp.mailfrom's domain, lowercased
    dkim_domain: str | None = None   # header.d, lowercased


def _is_tenant_header(value: str) -> bool:
    """Does the header have the shape Exchange Online stamps? Two shapes exist:
    the RFC 8601 form with an authserv-id up front (`<host>; spf=...`), and the
    common EXO form with no authserv-id at all but a `compauth=` result, which
    Microsoft's composite authentication emits.

    A shape check only: a sender can write a header of either shape. What makes
    a header the tenant's is its position, see parse_authentication_results."""
    head = value.split(";", 1)[0].strip()
    # The authserv-id may carry a version number: `host.example 1`.
    authserv = head.split()[0].lower() if head else ""
    if authserv.endswith(_TENANT_AUTHSERV_SUFFIX):
        return True
    return "compauth=" in value.lower()


_COMMENT_RE = re.compile(r"\([^()]*\)")


def _strip_comments(value: str) -> str:
    """Drop the RFC 8601 comments (`spf=pass (sender IP is 1.2.3.4)`) before
    reading results and properties, innermost first, so text inside a
    comment can never be read as a `header.d=` or `spf=` of its own."""
    while True:
        stripped = _COMMENT_RE.sub(" ", value)
        if stripped == value:
            return stripped
        value = stripped


def _results_from_header(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for method, result in _AUTH_METHOD_RE.findall(_strip_comments(value)):
        out.setdefault(method.lower(), result.lower())  # first occurrence wins
    return out


def _auth_domain(value: str | None) -> str | None:
    """The bare domain of an Authentication-Results property value: a domain
    (`smtp.mailfrom=gc.example`) or a full address
    (`smtp.mailfrom=bounce@gc.example`). `none` and empty mean absent."""
    text = (value or "").strip().strip("\"'<>,;.").lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if not text or text == "none":
        return None
    return text


def _props_from_header(value: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for prop, val in _AUTH_PROP_RE.findall(_strip_comments(value)):
        out.setdefault(prop.lower(), _auth_domain(val))  # first occurrence wins
    return out


def domains_aligned(domain: str | None, from_domain: str | None) -> bool:
    """Relaxed alignment (RFC 7489 section 3.1): the authenticated domain is
    the From domain, or the two share an organizational domain, judged as one
    being a subdomain of the other on a label boundary. `mail.gc.example`
    aligns with `gc.example`; `attacker.example` never does. Either side
    missing, or a bare top-level label, is not aligned."""
    a = (domain or "").strip().lower().strip(".")
    b = (from_domain or "").strip().lower().strip(".")
    if not a or not b or "." not in a or "." not in b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def verdict_from_results(
    spf: str | None,
    dkim: str | None,
    dmarc: str | None,
    *,
    tenant_header_present: bool,
    compauth: str | None = None,
    from_domain: str | None = None,
    spf_domain: str | None = None,
    dkim_domain: str | None = None,
) -> str:
    """The doc 3.3 policy over stored tokens, so the auth step can re-run it
    from the columns the fetch step wrote (crash-resumable without re-fetching).

    - fail when the tenant never stamped a header at all
    - fail when DMARC or compauth is fail (Exchange found the From spoofed)
    - pass when DMARC or compauth is pass (Exchange already checked alignment)
    - pass on an ALIGNED SPF pass (smtp.mailfrom in the From's organizational
      domain: a DKIM-less small GC with intact SPF on its own domain is
      legitimate) or an ALIGNED DKIM pass (header.d likewise)
    - fail otherwise: a pass for some other domain is evidence about that
      domain, not about the From, and a DMARC-less From is exactly what a
      spoofer would pick
    """
    if not tenant_header_present:
        return AUTH_FAIL
    dmarc_t = (dmarc or "").strip().lower()
    compauth_t = (compauth or "").strip().lower()
    if dmarc_t == "fail" or compauth_t == "fail":
        return AUTH_FAIL
    if dmarc_t == "pass" or compauth_t == "pass":
        return AUTH_PASS
    if (spf or "").strip().lower() == "pass" and domains_aligned(spf_domain, from_domain):
        return AUTH_PASS
    if (dkim or "").strip().lower() == "pass" and domains_aligned(dkim_domain, from_domain):
        return AUTH_PASS
    return AUTH_FAIL


def auth_fail_reason(
    spf: str | None, dkim: str | None, dmarc: str | None, compauth: str | None, *,
    tenant_header_present: bool,
) -> str:
    """The flag_reason for a failed verdict, in the order the policy applies."""
    if not tenant_header_present:
        return "no_tenant_auth_header"
    if (dmarc or "").strip().lower() == "fail":
        return "dmarc_fail"
    if (compauth or "").strip().lower() == "fail":
        return "compauth_fail"
    if any((t or "").strip().lower() == "pass" for t in (spf, dkim)):
        return "unaligned_pass"
    return "no_auth_pass"


def parse_authentication_results(
    headers: list[dict] | None, *, from_address: str | None = None
) -> AuthResults:
    """Read Graph's `internetMessageHeaders` (`[{"name", "value"}, ...]`) and
    extract the tenant-stamped verdicts.

    Only the top-most `Authentication-Results` header counts. Exchange Online
    prepends its header on arrival, so on inbound mail the tenant's header is
    always the first one, above anything the sender wrote. A hostile sender can
    add their own `Authentication-Results: ...dmarc=pass`, in Exchange's own
    shape if they like, but it can only ever sit below the tenant's, and it
    must count for nothing. If the first header is not the tenant's, the
    message fails closed (flagged for review) instead of the parser reading on
    down the list until it finds a header it likes. Graph returns the headers
    in message order.

    `from_address` is the message's RFC 5322 From, the domain every SPF or
    DKIM pass must align with; without it the header's own `header.from=` is
    used."""
    for header in headers or []:
        if (header.get("name") or "").strip().lower() != _AUTH_HEADER_NAME:
            continue
        value = header.get("value") or ""
        if not _is_tenant_header(value):
            break
        results = _results_from_header(value)
        props = _props_from_header(value)
        spf, dkim, dmarc = results.get("spf"), results.get("dkim"), results.get("dmarc")
        compauth = results.get("compauth")
        spf_domain, dkim_domain = props.get("smtp.mailfrom"), props.get("header.d")
        from_domain = address_domain(from_address) or props.get("header.from")
        return AuthResults(
            spf=spf,
            dkim=dkim,
            dmarc=dmarc,
            compauth=compauth,
            raw=value,
            verdict=verdict_from_results(
                spf, dkim, dmarc, tenant_header_present=True, compauth=compauth,
                from_domain=from_domain, spf_domain=spf_domain, dkim_domain=dkim_domain,
            ),
            spf_domain=spf_domain,
            dkim_domain=dkim_domain,
        )
    return AuthResults(None, None, None, None, None, AUTH_FAIL)


# ── Keyword gate (doc 3.4) ───────────────────────────────────────────────────

KEYWORDS: tuple[str, ...] = (
    "bid", "package", "request", "proposal", "quote", "propose", "rfp",
    "invite", "invitation", "budgetary", "itb", "ifb", "rfq", "solicitation",
    "project", "tender", "bidder", "bidding", "lrb", "rebid", "requote", "ve",
    "gmp", "db", "dbb", "cmar", "proposed", "quotation", "quoting", "pursue",
    "pursuing", "pursuit",
)

# Word boundaries are what keep `feedback` from hitting `db` and `Steve` from
# hitting `ve`. Longest alternatives first so `bidding` is not reported as `bid`.
_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, KEYWORDS), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def keyword_hits(subject: str | None, body: str | None) -> list[str]:
    """Distinct lowercased keywords found in subject + body, in first-seen
    order (stored on the row so the review screen can show why it advanced)."""
    text = f"{subject or ''}\n{body or ''}"
    seen: dict[str, None] = {}
    for match in _KEYWORD_RE.finditer(text):
        seen.setdefault(match.group(0).lower(), None)
    return list(seen)


# ── Message-ID normalization (doc 3.1) ───────────────────────────────────────


def normalize_message_id(raw: str | None, *, mailbox: str, graph_id: str) -> str:
    """One identity per message across mailboxes: trimmed, angle brackets
    removed, lowercased. A message with no Message-ID gets a synthetic id
    scoped to the mailbox and Graph id so it is still unique."""
    text = (raw or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    text = text.lower()
    if not text:
        return f"graph:{mailbox.strip().lower()}:{graph_id}"
    return text


def address_domain(address: str | None) -> str:
    """The bare domain of an address, lowercased ('' when it has none)."""
    text = (address or "").strip().lower()
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[1]


# Second-level labels under which a two-letter country code hands out
# registrations (co.uk, com.au, ac.jp, gov.za ...): a sender's organizational
# domain is then the last THREE labels. Short and built in; a Public Suffix
# List dependency is deliberately not taken (security review 2026-09-30).
_SECOND_LEVEL_PUBLIC_SUFFIXES: frozenset[str] = frozenset(
    {"co", "com", "net", "org", "gov", "edu", "ac"}
)

# Hosting suffixes many unrelated organizations register a label under
# (every Microsoft 365 tenant gets <tenant>.onmicrosoft.com): the
# organizational domain is the last three labels there too, so one tenant
# cannot spend every other tenant's classify budget.
_SHARED_HOST_SUFFIXES: frozenset[str] = frozenset({"onmicrosoft.com"})

# Mailboxes that are one person's, not one organization's: the shared
# public-provider list plus the consumer ISP mailboxes (the same set
# rfq_inbox keeps for the vendor side). The classify budget keys on the
# whole address at these, never on the provider.
BUDGET_ADDRESS_DOMAINS: frozenset[str] = PUBLIC_MAILBOX_DOMAINS | frozenset(
    {
        "att.net", "bellsouth.net", "centurylink.net", "charter.net", "comcast.net",
        "cox.net", "earthlink.net", "frontier.com", "gmx.com", "gmx.net", "juno.com",
        "netzero.net", "optonline.net", "pm.me", "roadrunner.com", "rocketmail.com",
        "rr.com", "sbcglobal.net", "verizon.net", "yandex.com", "zoho.com",
        "hotmail.co.uk", "yahoo.co.uk", "outlook.co.uk", "hotmail.fr", "yahoo.fr",
        "yahoo.ca", "yahoo.com.au", "live.co.uk", "hotmail.de", "web.de", "gmx.de",
    }
)

# Gmail ignores dots in the local part and delivers googlemail.com to the
# same mailbox as gmail.com.
GMAIL_DOMAINS: frozenset[str] = frozenset({"gmail.com", "googlemail.com"})

BUDGET_KEY_ADDRESS = "address"
BUDGET_KEY_DOMAIN = "domain"


def registrable_domain(domain: str | None) -> str:
    """The organizational (registrable) domain a hostname belongs to: the last
    two labels, or the last three when the second-to-last label is a public
    second-level suffix under a two-letter country code (`bids.gc.co.uk` is
    `gc.co.uk`; `a0.evil.example` is `evil.example`) or the last two labels
    are a shared hosting suffix (`t1.onmicrosoft.com`). Rotating sibling
    subdomains therefore shares one classify budget (doc 3.5). A bare label
    or an empty value comes back as given, lowercased."""
    text = (domain or "").strip().lower().strip(".")
    labels = [p for p in text.split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    if budget_parent_suffix(".".join(labels[-3:])):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def budget_parent_suffix(domain: str | None) -> str:
    """The public suffix a three-label registrable domain hangs under
    (`gc.co.uk` -> `co.uk`, `t1.onmicrosoft.com` -> `onmicrosoft.com`), or
    '' for an ordinary two-label domain. Whoever owns such a suffix (or a
    domain shaped like one, `co.de`) could mint a fresh registrable domain
    per message, so the suffix as a whole carries a second, larger classify
    budget (doc 3.5)."""
    labels = [p for p in (domain or "").strip().lower().strip(".").split(".") if p]
    if len(labels) != 3:
        return ""
    parent = ".".join(labels[-2:])
    if parent in _SHARED_HOST_SUFFIXES:
        return parent
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_PUBLIC_SUFFIXES:
        return parent
    return ""


def canonical_mailbox_address(address: str | None) -> str:
    """One mailbox, one spelling: lowercased, the `+tag` dropped from the
    local part (every provider in BUDGET_ADDRESS_DOMAINS delivers
    `name+anything@` to `name@`), and at Gmail the dots dropped too with
    googlemail.com folded into gmail.com. `spammer+7@gmail.com`,
    `s.p.a.m.m.e.r@googlemail.com` and `spammer@gmail.com` are the same
    mailbox, so they share one classify budget (doc 3.5)."""
    text = (address or "").strip().lower()
    if "@" not in text:
        return text
    local, _, domain = text.rpartition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def classify_budget_key(address: str | None) -> tuple[str, str]:
    """What the per-sender classify budget (doc 3.5) is counted on for this
    From address: `(address, <canonical address>)` when the domain is a
    public mail provider or consumer ISP mailbox, so one free account
    cannot spend every Gmail user's budget and plus-tag or dot variants of
    one account cannot mint fresh budgets; `(domain, <registrable domain>)`
    otherwise, so a sender rotating addresses or sibling subdomains inside
    one organization shares one budget. The value is '' when the address
    has no domain."""
    text = (address or "").strip().lower()
    domain = address_domain(text)
    if not domain:
        return BUDGET_KEY_DOMAIN, ""
    if domain in BUDGET_ADDRESS_DOMAINS:
        return BUDGET_KEY_ADDRESS, canonical_mailbox_address(text)
    return BUDGET_KEY_DOMAIN, registrable_domain(domain)


# ── Rule validation (docs 3.7, 9) ────────────────────────────────────────────

_ADDRESS_RE = re.compile(r"^[^@\s<>,;\"]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
_DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]+(?:\.(?!-)[a-z0-9-]+)+$")


def validate_rule(kind: str | None, value: str | None) -> tuple[str, str]:
    """Server-side validation for an authorized-sender rule. Returns the
    normalized (kind, value) pair or raises ValueError with a message written
    for the person filling in the settings form."""
    kind = (kind or "").strip().lower()
    if kind not in RULE_KINDS:
        raise ValueError("Choose a rule type: an email address or a domain.")
    value = (value or "").strip().lower()
    if not value:
        raise ValueError(
            "Enter an email address or a domain to authorize."
            if kind == "address" else "Enter a domain to authorize."
        )

    if kind == "address":
        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1].strip()
        if not _ADDRESS_RE.match(value):
            raise ValueError(
                f"'{value}' is not a valid email address. Enter the sender's full "
                "address, for example bids@example.com."
            )
        return kind, value

    # domain
    if "@" in value:
        raise ValueError(
            f"'{value}' looks like an email address. Choose the address rule type "
            "for a single sender, or enter just the domain (the part after the @)."
        )
    if "://" in value or "/" in value:
        raise ValueError(
            f"'{value}' is not a bare domain. Enter just the hostname, for example "
            "example.com, without http:// or a path."
        )
    if not _DOMAIN_RE.match(value):
        raise ValueError(
            f"'{value}' is not a valid domain. Enter a bare hostname such as "
            "example.com or procoretech.com."
        )
    if is_public_mailbox_domain(value):
        raise ValueError(
            f"'{value}' is a public email provider, and public providers like "
            "gmail.com cannot be authorized as a whole domain: that would authorize "
            "every account at the provider. Add each person's full email address as "
            "an address rule instead. A GC contact at a public provider is matched "
            "by their full address only, never by the provider domain."
        )
    return kind, value


def validate_block(kind: str | None, value: str | None) -> tuple[str, str]:
    """Server-side validation for a blocked-sender row (doc 3.1, table
    `rfp_blocked_senders` from 0133). Returns the normalized (kind, value)
    pair or raises ValueError with a message written for the person who
    pressed Block.

    Shape validation is the authorized-rule one: an address must be an
    address, a domain must be a bare hostname. The public-provider refusal is
    kept for DOMAIN blocks only, and for the opposite reason it exists on the
    authorized side: blocking gmail.com would silently drop every GC contact
    who uses Gmail. Blocking one gmail ADDRESS is fine and is the way to stop
    a single nuisance sender there.
    """
    kind = (kind or "").strip().lower()
    if kind not in RULE_KINDS:
        raise ValueError("Choose what to block: one email address, or a whole domain.")
    value = (value or "").strip().lower()
    if not value:
        raise ValueError(
            "Enter the email address to block."
            if kind == KIND_ADDRESS else "Enter the domain to block."
        )

    if kind == KIND_ADDRESS:
        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1].strip()
        if not _ADDRESS_RE.match(value):
            raise ValueError(
                f"'{value}' is not a valid email address. Enter the sender's full "
                "address, for example noreply@example.com."
            )
        return kind, value

    # domain
    if "@" in value:
        raise ValueError(
            f"'{value}' looks like an email address. Choose 'this sender only' to "
            "block one person, or enter just the domain (the part after the @) to "
            "block everyone at that company."
        )
    if "://" in value or "/" in value:
        raise ValueError(
            f"'{value}' is not a bare domain. Enter just the hostname, for example "
            "example.com, without http:// or a path."
        )
    if not _DOMAIN_RE.match(value):
        raise ValueError(
            f"'{value}' is not a valid domain. Enter a bare hostname such as "
            "example.com."
        )
    if is_public_mailbox_domain(value):
        raise ValueError(
            f"'{value}' is a public email provider, and blocking a whole public "
            "provider would silently drop every invitation from every GC contact "
            "who uses it. Block the individual email address instead."
        )
    return kind, value


# ── Authorization (docs 3.7, 3.8) ────────────────────────────────────────────


@dataclass(frozen=True)
class AuthorizationResult:
    authorized: bool
    kind: str | None = None        # KIND_ADDRESS | KIND_DOMAIN | KIND_GC_DOMAIN
    rule_id: str | None = None     # null for a GC-domain match
    method: str | None = None      # the invitation method the winner grants


def method_for_rule(rule: dict | None, kind: str | None) -> str:
    """The invitation method a match grants (doc 3.8): a locked rule gives its
    own method (a platform, or gc_portal for a GC's own bidding portal), a GC
    domain is organic, a user-added rule is general whatever its column says
    (non-locked rows are always general by design)."""
    if kind == KIND_GC_DOMAIN:
        return METHOD_ORGANIC
    if kind == KIND_OVERRIDE:
        return METHOD_NONORGANIC
    if rule and rule.get("locked"):
        return rule.get("method") or METHOD_GENERAL
    return METHOD_GENERAL


def domain_covered_by(domain: str, rule_domain: str) -> bool:
    """A domain rule covers the domain itself AND its subdomains, on a label
    boundary: `procoretech.com` covers `us02.procoretech.com` (Procore sends
    from regional subdomains, seen live 2026-09-10) but never
    `notprocoretech.com`. A rule on a subdomain stays narrow: a rule on
    `bids.example.com` does not cover `example.com` itself."""
    domain = (domain or "").strip().lower().strip(".")
    rule_domain = (rule_domain or "").strip().lower().strip(".")
    if not domain or not rule_domain:
        return False
    return domain == rule_domain or domain.endswith("." + rule_domain)


def rule_matches(rule: dict, address: str | None) -> bool:
    """Does this authorized-sender rule cover the (lowercased) From address?"""
    address = (address or "").strip().lower()
    value = (rule.get("value") or "").strip().lower()
    if not address or not value:
        return False
    if rule.get("kind") == KIND_ADDRESS:
        return value == address
    if rule.get("kind") == KIND_DOMAIN:
        return domain_covered_by(address_domain(address), value)
    return False


def _rule_matches(rule: dict, address: str, domain: str) -> bool:
    return rule_matches(rule, address)


def evaluate_authorization(
    from_address: str | None,
    rules: list[dict],
    gc_domains: set[str],
    internal_domains: set[str],
) -> AuthorizationResult:
    """Decide whether an authenticated sender is authorized and which method
    they get.

    Match sources, on the exact address, or on the domain and its subdomains:
      1. locked rules (address before domain): the platform seeds, and the
         gc_portal rules the IT Admin adds on a GC's own domain. Sitting
         above the GC-domain source is what lets a portal rule outrank the
         same GC's organic match.
      2. GC domains: distinct gc_contacts domains minus internal and public
         providers. A GC contact at gmail.com authorizes nothing here; add
         their address as a rule instead.
      3. user-added rules (address before domain)

    When several sources match the same sender (a GC domain a user also added
    as a rule), the higher source wins, so the method is decided by the most
    trusted evidence. An internal-domain sender never authorizes: the poller
    skips those before a row exists, and this guard keeps a rule from
    reintroducing them.
    """
    address = (from_address or "").strip().lower()
    domain = address_domain(address)
    internal = {d.strip().lower() for d in internal_domains if d}
    if not address or not domain or domain in internal:
        return AuthorizationResult(False)

    def _kind_order(rule: dict) -> int:
        return 0 if rule.get("kind") == KIND_ADDRESS else 1

    matched = [r for r in rules if _rule_matches(r, address, domain)]
    locked = sorted((r for r in matched if r.get("locked")), key=_kind_order)
    if locked:
        rule = locked[0]
        return AuthorizationResult(
            True, rule["kind"], rule.get("id"), method_for_rule(rule, rule["kind"])
        )

    gc = {d.strip().lower() for d in gc_domains if d}
    if domain in gc and domain not in internal and not is_public_mailbox_domain(domain):
        return AuthorizationResult(True, KIND_GC_DOMAIN, None, METHOD_ORGANIC)

    user = sorted((r for r in matched if not r.get("locked")), key=_kind_order)
    if user:
        rule = user[0]
        return AuthorizationResult(
            True, rule["kind"], rule.get("id"), method_for_rule(rule, rule["kind"])
        )
    return AuthorizationResult(False)
