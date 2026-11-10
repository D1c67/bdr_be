"""Browser v2 site registry, URL rules and URL provenance
(docs/ASSISTANT_BROWSER_V2_PLAN.md 5.1, 5.5, 7.3, 7.5).

Registry rows live in assistant_browser_sites (0192): curated rows by
migration, ad hoc rows inserted when a user approves a site that is not
curated. Every read goes through app.assistant.repo (no table access here).

URL rules (5.1): https on any port, plain http for top-level navigation
only; no userinfo; at most 2048 characters; no control characters or
backslashes; queries and fragments allowed. Hosts are IDNA (punycode)
lowercase DNS names with a registrable domain in the bundled public suffix
list (offline snapshot, `publicsuffixlist`, pinned in pyproject.toml); IP
literals, single-label hosts, `localhost` and the private-use suffixes are
refused here as a first line (the worker floor, 10.1, is the real one).
Default ports are dropped so origins compare equal to the worker's
`protocol.url_origin`.

The approval unit is the registrable domain (eTLD+1), except on a
multi-tenant domain (protocol.MULTI_TENANT_DOMAINS: exact origin plus a path
prefix) and on a domain under a suffix from the list's private section
(cloud and app hosting: exact origin, never the suffix).

Phase 6 names (`SiteSpec`, `SITES`, `READ_PATHS`, `site`, `validate_url`,
`read_origin`, `credential_allows_url`) stay at the end of this module,
unchanged, until the retired `user_browser_read` callers (the vault, the
phase 6 runtime and its type) stop calling them (plan 3.3).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Literal, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from publicsuffixlist import PublicSuffixList

from app.services.browser_agents.protocol import (
    BLOCKED_HOST_SUFFIXES,
    HOST_RE,
    SITE_KEY_RE,
    is_identity_provider_host,
    is_multi_tenant_domain,
)

logger = logging.getLogger(__name__)

Provenance = Literal["typed", "bdr_record", "search", "email", "page", "unknown"]
GrantScope = Literal["domain", "origin"]

URL_MAX = 2048
REFUSED_MESSAGE = "Browser destination is unavailable."
# Curated seed (0192 A1.16), used where no database read is wanted (lookalike
# warnings). The registry rows are the source of truth for matching.
CURATED_DOMAINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "procore": ("Procore", ("procore.com",)),
    "autodesk": ("Autodesk Construction Cloud", ("autodesk.com",)),
    "buildingconnected": ("BuildingConnected", ("buildingconnected.com", "autodesk.com")),
    # The SmartBid sign-in page is gocc.smartbid.co and its token API is
    # apicc.smartinsight.co (app/services/smartbid_client.py); 0194 corrects
    # the 0192 seed to these domains.
    "smartbid": ("SmartBid", ("smartbidnet.com", "smartbid.co", "smartinsight.co")),
}
# 7.3 step 2: a name containing one of these words without being that site
# is a lookalike. Owned domains are the brand's own registrable domains.
LOOKALIKE_BRANDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("procore", "Procore", ("procore.com",)),
    ("autodesk", "Autodesk Construction Cloud", ("autodesk.com",)),
    ("buildingconnected", "BuildingConnected", ("buildingconnected.com",)),
    ("smartbid", "SmartBid", ("smartbidnet.com", "smartbid.co", "smartinsight.co")),
    ("microsoft", "Microsoft", ("microsoft.com", "microsoftonline.com", "live.com", "office.com",
                                "sharepoint.com", "outlook.com", "microsoft365.com")),
    ("google", "Google", ("google.com", "googleusercontent.com", "googleapis.com", "gstatic.com",
                          "forms.gle", "youtube.com")),
)
# Domains compared by edit distance (2 or less, not equal) in 7.3 step 2.
LOOKALIKE_DISTANCE_DOMAINS: tuple[tuple[str, str], ...] = (
    ("procore.com", "Procore"),
    ("autodesk.com", "Autodesk Construction Cloud"),
    ("buildingconnected.com", "BuildingConnected"),
    ("smartbidnet.com", "SmartBid"),
    ("microsoft.com", "Microsoft"),
    ("google.com", "Google"),
)
# Tool names whose tainted results classify a URL (5.5). Anything else that
# is tainted counts as email: only an email link raises a card, so an
# unknown untrusted source errs toward asking.
_MAIL_TOOL_PREFIXES = ("email_", "mail_", "read_mailbox")
_BROWSER_TOOL_PREFIXES = ("user_browser", "browser_")
_SEARCH_TOOL_PREFIXES = ("web_",)
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


class UrlRefused(ValueError):
    """A URL outside the 5.1 rules. `code` is stable: invalid, too_long,
    scheme, http_not_top_level, userinfo, host, private_network, port,
    no_registrable_domain. The message never echoes the URL."""

    def __init__(self, code: str) -> None:
        super().__init__(REFUSED_MESSAGE)
        self.code = code


@dataclass(frozen=True)
class ParsedUrl:
    url: str                 # the normalized URL
    scheme: str              # https or http
    host: str                # IDNA lowercase
    port: int | None         # None for the scheme's default port
    origin: str              # scheme://host[:port]
    path: str                # "/" at least
    registrable_domain: str  # eTLD+1 (full list, private section included)

    @property
    def encrypted(self) -> bool:
        return self.scheme == "https"


@dataclass(frozen=True)
class GrantTarget:
    """What a site-access card approves (5.1, 5.2). For an existing registry
    row `site_key` is that row's key; for a new site it is the ad hoc key
    the grant RPC inserts."""
    site_key: str
    label: str
    kind: Literal["curated", "ad_hoc"]
    scope: GrantScope
    value: str                 # registrable domain (domain) or origin (origin)
    path_prefix: str | None    # origin grants on multi-tenant domains only
    multi_tenant: bool
    private_suffix: bool
    encrypted: bool            # False: the card shows "This site is not encrypted"
    exists: bool               # a registry row already has this key

    def ad_hoc(self) -> dict[str, str] | None:
        """The p_ad_hoc argument of assistant_browser_site_grant."""
        return None if self.exists else {"label": self.label}


@dataclass(frozen=True)
class ProvenanceResult:
    kind: Provenance
    source: str | None = None          # chat, mail_tool, rfp_harvest, project_field, ...
    sender_domain: str | None = None   # for the link card line "From an email from <domain>"


# ── Public suffix list (offline snapshot) ────────────────────────────────────


@lru_cache(maxsize=1)
def _psl_full() -> PublicSuffixList:
    return PublicSuffixList(accept_unknown=False)


@lru_cache(maxsize=1)
def _psl_icann() -> PublicSuffixList:
    return PublicSuffixList(accept_unknown=False, only_icann=True)


def _host_of(value: str) -> str:
    text = str(value or "").strip()
    if "://" in text:
        try:
            text = urlsplit(text).hostname or ""
        except ValueError:
            return ""
    elif text.startswith(("domain:", "origin:")):
        text = text.split(":", 1)[1].split(":", 1)[0]
    return text.lower().strip(".")


def registrable_domain(value: str) -> str | None:
    """eTLD+1 of a host or URL from the bundled list (private section
    included: `bucket.s3.amazonaws.com` is its own registrable domain).
    None for a public suffix itself, an unknown TLD or an IP literal."""
    host = _host_of(value)
    if not host or _is_ip(host):
        return None
    try:
        return _psl_full().privatesuffix(host)
    except Exception:  # noqa: BLE001 - a malformed label has no domain
        return None


def is_private_suffix(value: str) -> bool:
    """True when the host sits under a suffix from the list's private
    section (cloud storage and app hosting): the grant is the exact origin."""
    host = _host_of(value)
    if not host or _is_ip(host):
        return False
    try:
        full, icann = _psl_full().publicsuffix(host), _psl_icann().publicsuffix(host)
    except Exception:  # noqa: BLE001
        return False
    return bool(full) and full != icann


def is_multi_tenant(value: str) -> bool:
    """A host, domain, URL or ad hoc key on the multi-tenant list (5.1)."""
    host = _host_of(value)
    return bool(host) and is_multi_tenant_domain(host)


def is_identity_provider(value: str) -> bool:
    """A host, origin or URL of an identity provider (7.5): never a
    credential login origin."""
    host = _host_of(value)
    return bool(host) and is_identity_provider_host(host)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


# ── URL normalization (5.1) ──────────────────────────────────────────────────


def _ascii_part(value: str) -> str:
    """Percent-encode non-ASCII characters only; ASCII stays as typed."""
    if value.isascii():
        return value
    return quote(value, safe="".join(chr(c) for c in range(33, 127)))


def parse_url(url: str, *, top_level: bool = True) -> ParsedUrl:
    """Validate and normalize one URL (5.1). `top_level` False refuses plain
    http (http is allowed for top-level navigation only). Raises UrlRefused."""
    if not isinstance(url, str) or not url:
        raise UrlRefused("invalid")
    if len(url) > URL_MAX:
        raise UrlRefused("too_long")
    if any(ord(c) <= 32 or ord(c) == 127 or 0x80 <= ord(c) <= 0x9F for c in url) or "\\" in url:
        raise UrlRefused("invalid")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlRefused("port") from None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise UrlRefused("scheme")
    if scheme == "http" and not top_level:
        raise UrlRefused("http_not_top_level")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRefused("userinfo")
    raw_host = (parts.hostname or "").rstrip(".")
    if not raw_host:
        raise UrlRefused("host")
    if _is_ip(raw_host):
        raise UrlRefused("private_network")
    try:
        host = raw_host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise UrlRefused("host") from None
    if host == "localhost" or host.endswith(".localhost") or any(host.endswith(s) for s in BLOCKED_HOST_SUFFIXES):
        raise UrlRefused("private_network")
    if not HOST_RE.match(host):
        raise UrlRefused("host")
    if port is not None and not 1 <= port <= 65535:
        raise UrlRefused("port")
    if (scheme, port) in (("https", 443), ("http", 80)):
        port = None
    domain = registrable_domain(host)
    if not domain:
        raise UrlRefused("no_registrable_domain")
    netloc = host if port is None else f"{host}:{port}"
    path = _ascii_part(parts.path or "/")
    normalized = urlunsplit((scheme, netloc, path, _ascii_part(parts.query), _ascii_part(parts.fragment)))
    if len(normalized) > URL_MAX:
        raise UrlRefused("too_long")
    return ParsedUrl(url=normalized, scheme=scheme, host=host, port=port, origin=f"{scheme}://{netloc}",
                     path=path, registrable_domain=domain)


def normalize_url(url: str, *, top_level: bool = True) -> str:
    """The normalized URL string (5.1), or UrlRefused."""
    return parse_url(url, top_level=top_level).url


def url_origin(url: str) -> str:
    """scheme://host[:port] of a valid URL (default port dropped)."""
    return parse_url(url).origin


def is_encrypted(url: str) -> bool:
    """False for plain http: the site card shows "This site is not
    encrypted"; fill_credential and the email code reader never act on it."""
    return parse_url(url).encrypted


# ── Ad hoc keys and grant targets (5.1, 5.2) ─────────────────────────────────


def _short_hash(value: str, n: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def tenant_path_prefix(path: str) -> str:
    """The path prefix a multi-tenant grant covers (shown on the card): the
    path up to its second significant segment (longer than two characters),
    keeping short connector segments between them, so
    `/sites/Bids/Shared Documents/x` gives `/sites/Bids`, `/document/d/ID/edit`
    gives `/document/d/ID` and `/owner/repo/tree/main` gives `/owner/repo`.
    A root path gives "/"."""
    segments = [s for s in (path or "/").split("/") if s]
    kept: list[str] = []
    significant = 0
    for segment in segments:
        kept.append(segment)
        if len(segment) > 2:
            significant += 1
            if significant == 2:
                break
    prefix = "/" + "/".join(kept)
    return prefix[:512] if len(prefix) <= 512 else "/" + "/".join(kept[:1])[:511]


def ad_hoc_site_key(scope: GrantScope, value: str, path_prefix: str | None = None) -> str:
    """The registry key of an ad hoc site (charset ^[a-z0-9_.:-]{2,80}$).
    domain: `domain:<eTLD+1>`. origin: `origin:<host>`, then `:<port>` for a
    port that is not the scheme default, `:http` for plain http and
    `:p<hash>` for a path prefix other than "/". Too long: a hash key."""
    if scope == "domain":
        key = f"domain:{value.lower()}"
        return key if SITE_KEY_RE.match(key) else f"domain:h{_short_hash(value.lower(), 32)}"
    parts = urlsplit(value.lower())
    host = parts.hostname or ""
    key = f"origin:{host}"
    if parts.port is not None:
        key += f":{parts.port}"
    if parts.scheme == "http":
        key += ":http"
    if path_prefix and path_prefix != "/":
        key += f":p{_short_hash(path_prefix, 10)}"
    if SITE_KEY_RE.match(key):
        return key
    return f"origin:h{_short_hash(value.lower() + '|' + (path_prefix or ''), 32)}"


def grant_target_for_url(url: str) -> GrantTarget:
    """The ad hoc grant a new site gets (5.1): the registrable domain, or
    the exact origin plus a path prefix on a multi-tenant domain, or the
    exact origin under a private-section suffix. Raises UrlRefused."""
    parsed = parse_url(url)
    multi = is_multi_tenant(parsed.host)
    private = is_private_suffix(parsed.host)
    if multi:
        prefix = tenant_path_prefix(parsed.path)
        label = parsed.host + ("" if prefix == "/" else prefix)
        return GrantTarget(site_key=ad_hoc_site_key("origin", parsed.origin, prefix), label=label[:100],
                           kind="ad_hoc", scope="origin", value=parsed.origin, path_prefix=prefix,
                           multi_tenant=True, private_suffix=private, encrypted=parsed.encrypted, exists=False)
    if private:
        return GrantTarget(site_key=ad_hoc_site_key("origin", parsed.origin), label=parsed.host[:100],
                           kind="ad_hoc", scope="origin", value=parsed.origin, path_prefix=None,
                           multi_tenant=False, private_suffix=True, encrypted=parsed.encrypted, exists=False)
    domain = parsed.registrable_domain
    return GrantTarget(site_key=ad_hoc_site_key("domain", domain), label=domain[:100], kind="ad_hoc",
                       scope="domain", value=domain, path_prefix=None, multi_tenant=False, private_suffix=False,
                       encrypted=parsed.encrypted, exists=False)


def grant_target_for_site(site: Mapping[str, Any], *, url: str | None = None) -> GrantTarget:
    """The grant an existing registry row gets: its first registrable domain
    (domain scope), else its first exact origin with its path prefix."""
    domains = [str(d) for d in site.get("registrable_domains") or () if d]
    origins = [str(o) for o in site.get("exact_origins") or () if o]
    encrypted = True
    if url:
        try:
            encrypted = parse_url(url).encrypted
        except UrlRefused:
            encrypted = False
    key = str(site.get("site_key") or "")
    common = dict(site_key=key, label=str(site.get("label") or key)[:100],
                  kind="curated" if site.get("kind") == "curated" else "ad_hoc", encrypted=encrypted, exists=True)
    if domains:
        return GrantTarget(scope="domain", value=domains[0].lower(), path_prefix=None,
                           multi_tenant=any(is_multi_tenant(d) for d in domains),
                           private_suffix=any(is_private_suffix(d) for d in domains), **common)
    if not origins:
        raise ValueError("Browser site has no scope.")
    return GrantTarget(scope="origin", value=origins[0].lower(), path_prefix=site.get("path_prefix"),
                       multi_tenant=is_multi_tenant(origins[0]), private_suffix=is_private_suffix(origins[0]),
                       **common)


# ── Registry reads and matching ──────────────────────────────────────────────


def _repo():
    from app.assistant import repo

    return repo


def list_sites(user_id: str, *, include_disabled: bool = False) -> list[dict]:
    """Registry rows (curated and ad hoc), for Settings and the model's
    site list. The caller filters ad hoc rows to the user's own grants."""
    return _repo().browser_sites(user_id, include_disabled=include_disabled)


def get_site(user_id: str, site_key: str) -> dict | None:
    """One registry row, enabled or not, or None."""
    if not isinstance(site_key, str) or not SITE_KEY_RE.match(site_key):
        return None
    return _repo().browser_site(user_id, site_key)


def _path_ok(path: str, prefix: str | None) -> bool:
    if not prefix or prefix == "/":
        return True
    base = prefix.rstrip("/")
    return path == base or path.startswith(base + "/")


def site_matches(site: Mapping[str, Any], url: str) -> bool:
    """The Python twin of assistant_browser_v2_site_matches (0192 A1.19)
    plus the row's path prefix: an exact origin match (inside the path
    prefix when the row has one) or the host equal to or under one of the
    row's registrable domains."""
    try:
        parsed = parse_url(url)
    except UrlRefused:
        return False
    return _match_score(site, parsed) is not None


def _match_score(site: Mapping[str, Any], parsed: ParsedUrl) -> tuple | None:
    origins = [str(o).lower() for o in site.get("exact_origins") or ()]
    if parsed.origin in origins:
        if not _path_ok(parsed.path, site.get("path_prefix")):
            return None
        return (3, len(site.get("path_prefix") or ""), 0, 0)
    domains = [str(d).lower() for d in site.get("registrable_domains") or ()]
    for index, domain in enumerate(domains):
        if parsed.host == domain or parsed.host.endswith("." + domain):
            primary = 1 if index == 0 else 0
            curated = 1 if site.get("kind") == "curated" else 0
            return (2, primary, curated, len(domain))
    return None


def site_for_url(user_id: str, url: str, *, sites: Iterable[Mapping[str, Any]] | None = None) -> dict | None:
    """The registry row a URL belongs to (enabled or not; the caller checks
    `enabled`), or None for a site that is not registered. The most specific
    row wins: an exact origin (longest path prefix first), then a row whose
    primary domain matches, then curated rows, then the longest domain.
    `sites` skips the database read. Raises UrlRefused for a bad URL."""
    parsed = parse_url(url)
    if sites is None:
        sites = _repo().browser_sites_for_url(user_id, origin=parsed.origin,
                                              registrable_domain=parsed.registrable_domain)
    best: tuple[tuple, str, Mapping[str, Any]] | None = None
    for site in sites:
        score = _match_score(site, parsed)
        if score is None:
            continue
        key = str(site.get("site_key") or "")
        # Higher score wins; ties go to the smaller key (stable).
        if best is None or score > best[0] or (score == best[0] and key < best[1]):
            best = (score, key, site)
    return dict(best[2]) if best else None


def resolve_site(user_id: str, url: str) -> tuple[dict | None, GrantTarget]:
    """(registry row or None, the grant a card would approve) for one URL."""
    site = site_for_url(user_id, url)
    if site is not None:
        return site, grant_target_for_site(site, url=url)
    return None, grant_target_for_url(url)


# ── Lookalikes (7.3 step 2) ──────────────────────────────────────────────────


def _distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, stopping early once every cell exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _decoded(host: str) -> str:
    try:
        return host.encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        return host


def _folded(text: str) -> str:
    """Lowercase ASCII skeleton: accents removed (NFKD), so a punycode
    homoglyph such as xn--prcore-4ya.com reads as procore.com."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if c.isascii() and not unicodedata.combining(c))


# Confusable letters mapped to their Latin twin before the lookalike checks
# (Cyrillic and Greek homoglyphs are not removed by NFKD, they are dropped),
# plus the digits that stand in for letters ("proc0re").
_CONFUSABLES = str.maketrans({
    "\u0430": "a", "\u0432": "b", "\u0435": "e", "\u0451": "e", "\u043a": "k", "\u043c": "m",
    "\u043d": "h", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0442": "t", "\u0443": "y",
    "\u0445": "x", "\u0455": "s", "\u0456": "i", "\u0457": "i", "\u0458": "j", "\u04bb": "h",
    "\u0501": "d", "\u04cf": "l", "\u051b": "q", "\u051d": "w", "\u0261": "g",
    "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v", "\u03bf": "o",
    "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x", "\u03b3": "y",
    "0": "o", "1": "l", "3": "e", "5": "s",
})


def _skeleton(text: str) -> str:
    """Lowercase Latin skeleton: confusables mapped to Latin, then folded."""
    return _folded(text.lower().translate(_CONFUSABLES))


def lookalike_of(value: str, *, label: str | None = None) -> str | None:
    """The curated or identity brand label this site imitates (7.3 step 2),
    or None. `value` is a site key, domain, host or URL; `label` is the ad
    hoc row's label. A lookalike is a registrable domain within edit
    distance 2 of a curated domain (not equal), or a name containing
    procore, autodesk, buildingconnected, smartbid, microsoft or google
    without being that brand's own domain. Example: procore.com.example.net
    gives "Procore"."""
    host = _host_of(value)
    domain = registrable_domain(host) or host
    if not domain:
        return None
    names = [host, _folded(_decoded(host)), _skeleton(_decoded(host))]
    flat_names = [re.sub(r"[^a-z0-9]", "", n.lower()) for n in names]
    if label:
        flat_names.append(re.sub(r"[^a-z0-9]", "", _folded(label)))
        flat_names.append(re.sub(r"[^a-z0-9]", "", _skeleton(label)))
    for keyword, brand, owned in LOOKALIKE_BRANDS:
        if domain in owned:
            continue
        if any(keyword in name for name in flat_names):
            return brand
    skeletons = {domain, _folded(_decoded(domain)), _skeleton(_decoded(domain))}
    first_labels = {d.split(".", 1)[0] for d in skeletons if d}
    for curated, brand in LOOKALIKE_DISTANCE_DOMAINS:
        if domain == curated:
            continue
        if any(_distance(d, curated) <= 2 for d in skeletons):
            return brand
        # The first label alone ("procor.net", "g00gle-login.io" is caught
        # above): a short brand name allows one edit, a long one two.
        curated_label = curated.split(".", 1)[0]
        allowed = 1 if len(curated_label) < 8 else 2
        if any(_distance(first, curated_label) <= allowed for first in first_labels):
            return brand
    return None


# ── URL provenance (5.5) ─────────────────────────────────────────────────────


def _url_variants(url: str) -> list[str]:
    """The raw URL and its normalized forms (with and without a trailing
    slash), for exact comparisons with stored values."""
    out: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in out:
            out.append(value)

    add(url.strip() if isinstance(url, str) else None)
    try:
        normalized = normalize_url(url)
    except UrlRefused:
        return out
    add(normalized)
    parts = urlsplit(normalized)
    if not parts.query and not parts.fragment:
        if parts.path.endswith("/") and parts.path != "/":
            add(normalized.rstrip("/"))
        elif parts.path == "/":
            add(normalized[:-1])
        else:
            add(normalized + "/")
    return out


def _strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 12 or len(out) > 5000:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _strings(item, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out, depth + 1)


def _texts_hold(texts: Iterable[str], variants: Sequence[str]) -> bool:
    lowered = [v.lower() for v in variants if len(v) >= 8]
    for text in texts:
        low = text.lower()
        if any(v in low for v in lowered):
            return True
    return False


def _sender_domain(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        for key in ("from", "from_address", "sender", "fromAddress"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                value = value.get("address") or value.get("emailAddress")
                if isinstance(value, Mapping):
                    value = value.get("address")
            if isinstance(value, str):
                match = _EMAIL_IN_TEXT.search(value)
                if match:
                    return match.group(1).lower()
    return None


def _tool_class(tool_name: str) -> Provenance:
    name = tool_name or ""
    if name.startswith(_MAIL_TOOL_PREFIXES):
        return "email"
    if name.startswith(_BROWSER_TOOL_PREFIXES):
        return "page"
    if name.startswith(_SEARCH_TOOL_PREFIXES):
        return "search"
    return "email"


def provenance_detail(user_id: str, run_id: str | None, url: str, *, conversation_id: str | None = None,
                      user_texts: Sequence[str] | None = None, search_urls: Iterable[str] = (),
                      page_urls: Iterable[str] = ()) -> ProvenanceResult:
    """Where a start URL came from (5.5), strongest first:

    typed       in the user's own recent messages (mail_binding.url_is_bound);
    email       in a tainted mail tool result of this conversation, or in a
                row written from outside senders' email (RFP harvest, a
                project the RFP slice created, a portal files link, a PM link
                from the email trigger); an unknown tainted tool also counts;
    bdr_record  a project bidding link a BDR user typed, a PM link saved by
                hand, an RFI link the Procore sync built;
    search      this run's hosted search citations (`search_urls`) or a
                tainted web tool result;
    page        a browser result (`page_urls` or a tainted browser result);
    unknown     anything else.

    Email beats every class but typed: only an email link raises the
    link_open card (D6), so a URL seen in both an email and a BDR field
    asks. A failed email-source read also answers email (fail closed)."""
    from app.assistant import mail_binding

    repo = _repo()
    variants = _url_variants(url)
    if not variants:
        return ProvenanceResult("unknown")
    texts = list(user_texts) if user_texts is not None else []
    if user_texts is None and run_id:
        try:
            texts = repo.mail_user_texts(user_id, str(run_id))
        except Exception:  # noqa: BLE001 - no typed text: the URL is simply not typed
            logger.warning("browser provenance: user text read failed")
            texts = []
    if any(mail_binding.url_is_bound(v, user_texts=texts) for v in variants):
        return ProvenanceResult("typed", "chat")

    conversation = conversation_id
    if conversation is None and run_id:
        try:
            conversation = repo.conversation_for_run(user_id, str(run_id))
        except Exception:  # noqa: BLE001
            logger.warning("browser provenance: conversation read failed")
            return ProvenanceResult("email", "lookup_failed")
    tainted_classes: list[tuple[Provenance, str | None]] = []
    if conversation:
        try:
            rows = repo.tainted_results(user_id, conversation, limit=50)
        except Exception:  # noqa: BLE001 - fail closed: ask
            logger.warning("browser provenance: tainted result read failed")
            return ProvenanceResult("email", "lookup_failed")
        for row in rows:
            strings: list[str] = []
            _strings(row.get("payload"), strings)
            _strings(row.get("taint_corpus"), strings)
            if _texts_hold(strings, variants):
                tainted_classes.append((_tool_class(str(row.get("tool_name") or "")),
                                        _sender_domain(row.get("payload"))))
    for kind, sender in tainted_classes:
        if kind == "email":
            return ProvenanceResult("email", "mail_tool", sender)
    try:
        email_rows = repo.browser_url_email_sources(user_id, variants)
    except Exception:  # noqa: BLE001 - fail closed: ask
        logger.warning("browser provenance: email source read failed")
        return ProvenanceResult("email", "lookup_failed")
    if email_rows:
        first = email_rows[0]
        sender = next((r.get("sender_domain") for r in email_rows if r.get("sender_domain")), None)
        return ProvenanceResult("email", str(first.get("source") or "email"), sender)
    try:
        records = repo.browser_url_bdr_records(user_id, variants)
    except Exception:  # noqa: BLE001 - not a record: fall through
        logger.warning("browser provenance: record read failed")
        records = []
    if records:
        return ProvenanceResult("bdr_record", str(records[0].get("source") or "bdr_record"))
    normalized = set(_normalized_all(search_urls))
    if any(v in normalized for v in variants) or any(k == "search" for k, _ in tainted_classes):
        return ProvenanceResult("search", "search_citation")
    pages = set(_normalized_all(page_urls))
    if any(v in pages for v in variants) or any(k == "page" for k, _ in tainted_classes):
        return ProvenanceResult("page", "browser_result")
    return ProvenanceResult("unknown")


def _normalized_all(urls: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in urls or ():
        if isinstance(value, str):
            out.append(value.strip())
            try:
                out.append(normalize_url(value))
            except UrlRefused:
                continue
    return out


def provenance(user_id: str, run_id: str | None, url: str, **kwargs: Any) -> Provenance:
    """The provenance class of a start URL (5.5); see provenance_detail."""
    return provenance_detail(user_id, run_id, url, **kwargs).kind


def needs_link_card(kind: str) -> bool:
    """Only an email start URL needs the link_open card on an approved site
    (5.5, D6); every other class starts without one."""
    return kind == "email"


def site_card_facts(site: Mapping[str, Any] | None, target: GrantTarget, url: str | None = None) -> dict:
    """Server-built facts for the site access card (no free text from the
    page): label, kind, scope, the value in bold, path prefix, the
    not-encrypted flag, the multi-tenant note and any lookalike warning."""
    lookalike = None
    if target.kind != "curated":
        label = (site or {}).get("label") if site else target.label
        # The full host of the URL first (procore-bids.example.net), then the
        # granted value (example.net) with the label.
        lookalike = (lookalike_of(url) if url else None) or lookalike_of(target.value, label=label)
    facts = {
        "site_key": target.site_key, "label": target.label, "kind": target.kind, "scope": target.scope,
        "value": target.value, "path_prefix": target.path_prefix, "encrypted": target.encrypted,
        "multi_tenant": target.multi_tenant, "private_suffix": target.private_suffix,
        "lookalike_of": lookalike, "registered": target.exists,
    }
    if url:
        try:
            parsed = parse_url(url)
            facts["origin"] = parsed.origin
            facts["registrable_domain"] = parsed.registrable_domain
        except UrlRefused:
            pass
    return json.loads(json.dumps(facts))


# ── Phase 6 (retired user_browser_read), kept until its callers move ─────────
#
# Code-fixed Procore read destinations. The vault (`job_credential`), the
# phase 6 runtime and the retired type still call these; WI-5 and WI-6 move
# them to the registry. Do not extend.


@dataclass(frozen=True)
class SiteSpec:
    key: str
    label: str
    origins: tuple[str, ...]
    read_origins: tuple[str, ...]
    agent_type: str
    available: bool = False


SITES = {
    "procore": SiteSpec("procore", "Procore", ("https://app.procore.com", "https://login.procore.com",
                                               "https://us02.procore.com"),
                        ("https://app.procore.com", "https://us02.procore.com"), "user_browser_read", True),
    "buildingconnected": SiteSpec("buildingconnected", "BuildingConnected", (), (), "", False),
    "smartbid": SiteSpec("smartbid", "SmartBid", (), (), "", False),
}
READ_PATHS = (
    re.compile(r"^/\d{1,20}/project/(home|rfi(/list)?|rfi/show/\d{1,20})/?$"),
    re.compile(r"^/webclients/host/companies/\d{1,20}/projects/\d{1,20}/tools/(projecthome|hubs/project-hub/views/overview|rfis(/\d{1,20})?)/?$"),
)


def site(service: str) -> SiteSpec:
    value = SITES.get(service)
    if value is None or not value.available:
        raise ValueError("Browser site is unavailable.")
    return value


def validate_url(service: str, url: str, *, login: bool = False) -> str:
    spec = site(service)
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise ValueError("Browser destination is unavailable.")
    try:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query or parsed.fragment
                or origin not in (spec.origins if login else spec.read_origins)):
            raise ValueError
        if not login and not any(pattern.fullmatch(parsed.path) for pattern in READ_PATHS):
            raise ValueError
    except ValueError:
        raise ValueError("Browser destination is unavailable.") from None
    return url


def read_origin(service: str, url: str) -> str:
    """Return an exact code-allowed origin, never normalize a wider URL."""
    parsed = urlsplit(validate_url(service, url))
    return f"{parsed.scheme}://{parsed.netloc}"


def credential_allows_url(credential: dict, service: str, url: str) -> bool:
    origins = credential.get("origins")
    return isinstance(origins, list) and read_origin(service, url) in origins
