"""Emailed one-time codes read by the server, never the model
(docs/ASSISTANT_BROWSER_V2_PLAN.md 8.3, WI-14).

Two halves:

1. The reader. When a v2 help with reason `mfa` or `mfa_step_up` opens,
   `start_for_help` runs `poll_for_code` on a daemon thread. It reads only
   the job owner's own primary mailbox (the profile address; a shared,
   excluded or RFP team mailbox is refused) through the existing
   application Graph identity (`assistant_ms`), every 10 seconds for the
   setting `user_mfa_mail_poll_seconds` (default 300). A message counts
   only when:
     - it was received after the login attempt began (or the step-up help
       opened, less a short skew);
     - its From domain is in the site's `code_email_senders` list (public
       mailbox providers are never trusted senders);
     - the tenant's own Authentication-Results header (the top-most one,
       matched by its authserv-id or its compauth result; every other
       Authentication-Results header is ignored) shows DKIM pass for a
       `header.d` that aligns with the From domain, and neither DMARC nor
       compauth failed;
     - it is addressed (To or Cc) to the owner's mailbox, and its Date
       header is not well before the window (a replayed genuine email);
     - exactly one code is found: the site's `code_email_regex` when the
       registry has one, else the generic pattern (a 4 to 10 character run
       of letters and digits with at least one digit, within 40 characters
       of "code", "verification", "one-time" or "passcode"). The generic
       pattern is for ad hoc sites only: a curated site without a regex
       (anchored on its code email's own subject) never reads mail, since
       its platform also sends signed mail carrying other users' text.
   Two or more codes stop the reader (the code card is the path). The code
   goes to the help path through the service-side RPC
   `assistant_browser_help_secret_from_mail`, which also marks the message
   used. The email body never goes to a model, a chat message or a log.

2. The scrub. `scrub` masks codes as `[code]` and password-reset or magic
   sign-in links as `[sign-in link]` before the model's own mail tools
   (`assistant_mail.search`, `assistant_mail.read`,
   `assistant_mail_review.review`) return or store a message. A message is
   a code email when its sender domain is in any registry
   `code_email_senders` list, or its subject or body matches any site's
   `code_email_regex` or the generic pattern; a code email reaches the
   extractor with no body. While the owner has an open `mfa` or
   `mfa_step_up` help, every message is masked the same way.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from app.assistant import mail_scope, repo
from app.core.config import get_settings
from app.services import assistant_ms
from app.services.assistant_graph import GraphTransport

logger = logging.getLogger(__name__)

CODE_REASONS = ("mfa", "mfa_step_up")
POLL_INTERVAL_SECONDS = 10
DEFAULT_WINDOW_SECONDS = 300
MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS = 30, 1800
# A step-up email can be sent a moment before the worker opens its help.
STEP_UP_SKEW = timedelta(seconds=120)
# A replayed message keeps its original Date header: one written well
# before the window opened is not this login's code.
DATE_SKEW = timedelta(minutes=10)
MAX_LISTED = 25
MAX_TEXT = 200_000
MASK_CODE = "[code]"
MASK_LINK = "[sign-in link]"
# The authserv-id suffix Exchange Online stamps on the tenant's own header.
TENANT_AUTHSERV_ID = "mail.protection.outlook.com"
# Anyone can get a DKIM-signed address at these: never a code sender.
PUBLIC_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me", "mail.com", "ymail.com",
    "gmx.com", "zoho.com", "yandex.com",
})


class MfaMailRefused(ValueError):
    """A content-free refusal with a short code (never the mailbox, body or
    code)."""

    def __init__(self, code: str = "refused"):
        self.code = code
        super().__init__("Email code reading is unavailable.")


# ── Code patterns ────────────────────────────────────────────────────────────

# A 4 to 10 run of letters and digits that is not part of a URL, address,
# amount or longer token. A trailing full stop ends a sentence, not a domain.
_TOKEN = re.compile(r"(?<![A-Za-z0-9@/._%=&?#$+\-])([A-Za-z0-9]{4,10})(?![A-Za-z0-9@/_%=&?#\-]|\.[A-Za-z0-9])")
_HAS_DIGIT = re.compile(r"[0-9]")
# The plan's generic keywords (8.3).
_KEYWORDS = re.compile(r"(?<![A-Za-z])(passcode|verification|one[- ]?time|code)(?![A-Za-z])", re.I)
# The scrub masks more widely while it is masking (never for the reader).
_BROAD_KEYWORDS = re.compile(
    r"(?<![A-Za-z])(passcode|verification|verify|one[- ]?time|code|otp|2fa|mfa|pin|sign(?:ing)?[- ]?in|log(?:ging)?[- ]?in"
    r"|authentication|authenticate|security)(?![A-Za-z])", re.I)
# "code" after one of these words is not a sign-in code (estimating mail
# talks about cost codes and the electrical code all day).
_NOT_SIGN_IN = re.compile(
    r"(?<![A-Za-z])(?:cost|zip|postal|area|country|building|electrical|electric|fire|nec|ibc|tax|promo|promotion|discount"
    r"|coupon|project|job|phase|bid|product|item|spec|specification|qr|bar|source|error|color|colour|dress"
    r"|naics|csi|sic|account|budget|gl|cpt|icd|status|reason)[\s:-]*$", re.I)
_PROXIMITY = 40
# A footer year ("(c) 2026 Procore") is not a code.
_YEAR = re.compile(r"(?:19|20)[0-9]{2}")
_COPYRIGHT = re.compile("(?:\u00a9|\\(c\\)|copyright)[\\s,.]{0,3}$", re.I)
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'`]+")
_SIGN_IN_LINK = re.compile(
    r"(?i)log[-_]?in|log[-_]?on|sign[-_]?in|signin|auth|verif|magic|reset|passw|passwd|otp|mfa|2fa|token"
    r"|confirm|activat|one[-_]?time|onetime|passcode|[?&](?:code|key)=")
_SEPARATORS = re.compile(r"[ \-]")
_CODE_SHAPE = re.compile(r"^[A-Za-z0-9]{4,12}$")


def _keyword_spans(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    spans = []
    for match in pattern.finditer(text):
        if match.group(1).casefold() == "code" and _NOT_SIGN_IN.search(text[max(0, match.start() - 24):match.start()]):
            continue
        spans.append(match.span())
    return spans


def _near(span: tuple[int, int], keywords: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(max(0, k_start - end, start - k_end) <= _PROXIMITY for k_start, k_end in keywords)


def _candidate_spans(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    text = text[:MAX_TEXT]
    keywords = _keyword_spans(text, pattern)
    if not keywords:
        return []
    return [m.span(1) for m in _TOKEN.finditer(text)
            if _HAS_DIGIT.search(m.group(1)) and _near(m.span(1), keywords)
            and not (_YEAR.fullmatch(m.group(1)) and _COPYRIGHT.search(text[max(0, m.start(1) - 16):m.start(1)]))]


def generic_codes(text: str, *, broad: bool = False) -> list[str]:
    """Distinct generic-pattern candidates in first-seen order."""
    if not isinstance(text, str) or not text:
        return []
    out: list[str] = []
    for start, end in _candidate_spans(text, _BROAD_KEYWORDS if broad else _KEYWORDS):
        value = text[start:end]
        if value not in out:
            out.append(value)
    return out


def generic_code(text: str) -> str | None:
    """Exactly one 4 to 10 character run with a digit within 40 characters of
    code, verification, one-time or passcode; else None."""
    codes = generic_codes(text)
    return codes[0] if len(codes) == 1 else None


def compile_site_regex(value: Any) -> re.Pattern[str] | None:
    if not isinstance(value, str) or not 0 < len(value) <= 500:
        return None
    try:
        return re.compile(value)
    except (re.error, TypeError, ValueError):
        return None


def _regex_spans(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    spans = []
    try:
        for match in pattern.finditer(text[:MAX_TEXT]):
            span = match.span(1) if pattern.groups >= 1 and match.group(1) is not None else match.span()
            if span[1] > span[0]:
                spans.append(span)
            if len(spans) >= 50:
                break
    except (re.error, IndexError):
        return []
    return spans


def _normalize_code(value: str) -> str | None:
    code = _SEPARATORS.sub("", value.strip())
    return code if _CODE_SHAPE.fullmatch(code) else None


def site_codes(text: str, *, site_regex: re.Pattern[str] | None) -> list[str]:
    """Distinct codes in one message text: the registry regex when the site
    has one, else the generic pattern. Codes that do not fit 4 to 12
    letters and digits (after spaces and hyphens) are dropped."""
    if not isinstance(text, str) or not text:
        return []
    raw = ([text[a:b] for a, b in _regex_spans(text, site_regex)] if site_regex is not None
           else generic_codes(text))
    out: list[str] = []
    for value in raw:
        code = _normalize_code(value)
        if code and code not in out:
            out.append(code)
    return out


# ── Sender trust (Authentication-Results) ────────────────────────────────────

_COMMENT = re.compile(r"\([^()]*\)")
_RESULT = re.compile(r"^\s*([a-z0-9_-]+)\s*=\s*([a-z]+)", re.I)
_PROP = re.compile(r"(?<![\w.-])([a-z]+\.[a-z0-9_-]+)\s*=\s*([^\s;]+)", re.I)


def _strip_comments(value: str) -> str:
    while True:
        stripped = _COMMENT.sub(" ", value)
        if stripped == value:
            return stripped
        value = stripped


def _header_list(headers: Any) -> list[tuple[str, str]]:
    if isinstance(headers, Mapping):
        items: Iterable[Any] = headers.items()
    elif isinstance(headers, (list, tuple)):
        items = headers
    else:
        return []
    out = []
    for item in items:
        if isinstance(item, Mapping):
            name, value = item.get("name"), item.get("value")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            name, value = item
        else:
            continue
        if isinstance(name, str) and isinstance(value, str):
            out.append((name.strip().casefold(), value))
    return out


def _domain(value: Any) -> str | None:
    text = str(value or "").strip().strip("\"'<>,;.").casefold()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.strip(".")
    if not text or text == "none" or "." not in text or not re.fullmatch(r"[a-z0-9.-]{3,253}", text):
        return None
    return text


def _organizational(domain: str) -> str | None:
    from app.services.assistant_browser_sites import registrable_domain

    return registrable_domain(domain)


def aligned(signing_domain: str | None, from_domain: str | None) -> bool:
    """Relaxed DMARC alignment: both domains share one registrable domain."""
    if not signing_domain or not from_domain:
        return False
    if signing_domain == from_domain:
        return True
    org = _organizational(from_domain)
    return bool(org) and org == _organizational(signing_domain)


def covered(domain: str | None, allowed: Iterable[str]) -> bool:
    """The domain is an allowed sender domain or a subdomain of one."""
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def trusted_senders(site: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    for value in (site or {}).get("code_email_senders") or ():
        domain = _domain(value)
        if domain and domain not in PUBLIC_MAIL_DOMAINS and domain not in out:
            out.append(domain)
    return out


def _is_tenant_header(value: str, tenant_authserv_id: str) -> bool:
    head = _strip_comments(value).split(";", 1)[0].strip()
    tenant = (tenant_authserv_id or "").strip().casefold()
    if head and "=" not in head:
        authserv = head.split()[0].casefold()
        return bool(tenant) and (authserv == tenant or authserv.endswith("." + tenant))
    return re.search(r"(?<![\w.-])compauth\s*=", value, re.I) is not None


def _from_address(pairs: list[tuple[str, str]], from_address: str | None) -> str | None:
    values = [v for n, v in pairs if n == "from"]
    if len(values) > 1:
        return None
    header_address = None
    if values:
        found = [a for _, a in getaddresses(values) if a]
        if len(found) != 1:
            return None
        header_address = found[0].strip().casefold()
    given = from_address.strip().casefold() if isinstance(from_address, str) and from_address.strip() else None
    if given and header_address and given != header_address:
        return None
    return given or header_address


def sender_trusted(headers: Mapping[str, str] | list[tuple[str, str]], *, allowed_domains: list[str],
                   tenant_authserv_id: str, from_address: str | None = None) -> bool:
    """DKIM pass from the tenant's own Authentication-Results header only,
    with the From domain aligned to d= and in the allowed list.

    Exchange Online prepends its header on arrival, so only the top-most
    Authentication-Results header can be the tenant's; any header below it
    (which a sender can write in any shape) is ignored, and a top-most
    header that is not the tenant's fails closed."""
    pairs = _header_list(headers)
    address = _from_address(pairs, from_address)
    from_domain = _domain(address)
    allowed = [d for d in (_domain(x) for x in allowed_domains or ()) if d and d not in PUBLIC_MAIL_DOMAINS]
    if not from_domain or not covered(from_domain, allowed):
        return False
    tenant = next((v for n, v in pairs if n == "authentication-results"), None)
    if tenant is None or not _is_tenant_header(tenant, tenant_authserv_id):
        return False
    clauses = _strip_comments(tenant).split(";")
    dkim_ok = False
    for clause in clauses:
        result = _RESULT.match(clause)
        if not result:
            continue
        method, verdict = result.group(1).casefold(), result.group(2).casefold()
        if method in {"dmarc", "compauth"} and verdict == "fail":
            return False
        if method == "dkim" and verdict == "pass":
            props = {k.casefold(): _domain(v) for k, v in _PROP.findall(clause)}
            signer = props.get("header.d") or props.get("header.i")
            if signer and aligned(signer, from_domain) and covered(signer, allowed + [from_domain]):
                dkim_ok = True
    return dkim_ok


# ── The scrub (the model's own mail tools) ───────────────────────────────────


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _replace(text: str, spans: list[tuple[int, int]], mask: str) -> str:
    for start, end in reversed(_merge(spans)):
        text = text[:start] + mask + text[end:]
    return text


def _mask_links(text: str) -> str:
    spans = [m.span() for m in _URL.finditer(text) if _SIGN_IN_LINK.search(m.group(0))]
    return _replace(text, spans, MASK_LINK) if spans else text


def _shaped_spans(text: str) -> list[tuple[int, int]]:
    """Mask-only code-shaped tokens (plan 8.2 shape, the chat guard's own
    tokenizer): 4 to 12 letters and digits with a digit once single hyphens
    or digit-group spaces are removed, so "4829-1357", "482 913", "ABC-123",
    "code=482913" and "#482913" all count. Parts of an address, a host name
    or a path are left alone. Wider than the reader's _TOKEN on purpose:
    masking may over-match, reading may not."""
    from app.services.assistant_browser_secrets import _code_spans

    out = []
    for start, end, _code in _code_spans(text):
        before = text[start - 1] if start > 0 else ""
        after = text[end:end + 2]
        if before in "@./" or after[:1] == "@" or (after[:1] == "." and after[1:2].isalnum()):
            continue
        out.append((start, end))
    return out


def _mask_spans(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """Code-shaped tokens within 40 characters of one of the keywords."""
    keywords = _keyword_spans(text, pattern)
    if not keywords:
        return []
    return [span for span in _shaped_spans(text) if _near(span, keywords)]


def mask_text(text: Any, *, regexes: list[re.Pattern[str]], code_email: bool = False) -> Any:
    """Codes become [code] and sign-in links [sign-in link]. In a code
    email every code-shaped token is masked, keyword or not. A text longer
    than the scan bound has its unscanned tail dropped."""
    if not isinstance(text, str) or not text:
        return text
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
    text = _mask_links(text)
    spans = _candidate_spans(text, _BROAD_KEYWORDS)
    spans.extend(_shaped_spans(text) if code_email else _mask_spans(text, _BROAD_KEYWORDS))
    for pattern in regexes:
        spans.extend(_regex_spans(text, pattern))
    return _replace(text, spans, MASK_CODE) if spans else text


def _sender_domain(message: Mapping[str, Any]) -> str | None:
    for key in ("from", "sender"):
        value = message.get(key)
        if isinstance(value, Mapping):
            address = (value.get("emailAddress") or {}).get("address") if isinstance(
                value.get("emailAddress"), Mapping) else None
            domain = _domain(address)
            if domain:
                return domain
    return None


def _texts(message: Mapping[str, Any]) -> list[str]:
    out = []
    for value in (message.get("subject"), message.get("bodyPreview"),
                  (message.get("body") or {}).get("content") if isinstance(message.get("body"), Mapping) else None,
                  (message.get("uniqueBody") or {}).get("content") if isinstance(message.get("uniqueBody"), Mapping)
                  else None):
        if isinstance(value, str) and value:
            out.append(value[:MAX_TEXT])
    return out


def scrub_context(sites: Iterable[Mapping[str, Any]] | None) -> tuple[list[str], list[re.Pattern[str]]]:
    senders: list[str] = []
    regexes: list[re.Pattern[str]] = []
    for site in sites or ():
        if not isinstance(site, Mapping):
            continue
        for value in site.get("code_email_senders") or ():
            domain = _domain(value)
            if domain and domain not in senders:
                senders.append(domain)
        pattern = compile_site_regex(site.get("code_email_regex"))
        if pattern is not None and pattern.pattern not in {p.pattern for p in regexes}:
            regexes.append(pattern)
    return senders, regexes


def is_code_email(message: Mapping[str, Any], *, sites: Iterable[Mapping[str, Any]] | None) -> bool:
    if not isinstance(message, Mapping):
        return False
    senders, regexes = scrub_context(sites)
    if covered(_sender_domain(message), senders):
        return True
    for text in _texts(message):
        if (any(_regex_spans(text, p) for p in regexes) or _candidate_spans(text, _KEYWORDS)
                or _mask_spans(text, _KEYWORDS)):
            return True
    return False


def scrub_view(message: dict[str, Any], *, sites: Iterable[Mapping[str, Any]] | None,
               owner_mfa_open: bool) -> tuple[dict[str, Any], bool]:
    """(masked copy, is a code email). Nothing changes for an ordinary
    message while no code help is open."""
    if not isinstance(message, dict):
        return message, False
    sites = list(sites or ())
    code_email = is_code_email(message, sites=sites)
    if not code_email and not owner_mfa_open:
        return message, False
    _, regexes = scrub_context(sites)
    out = copy.deepcopy(message)
    for key in ("subject", "bodyPreview"):
        if key in out:
            out[key] = mask_text(out[key], regexes=regexes, code_email=code_email)
    for key in ("body", "uniqueBody"):
        if isinstance(out.get(key), dict) and "content" in out[key]:
            out[key]["content"] = mask_text(out[key]["content"], regexes=regexes, code_email=code_email)
    return out, code_email


def scrub(message: dict[str, Any], *, sites: list[dict[str, Any]], owner_mfa_open: bool) -> dict[str, Any]:
    """Mask codes as [code] and sign-in links as [sign-in link] before the
    mail tools return or store a message."""
    return scrub_view(message, sites=sites, owner_mfa_open=owner_mfa_open)[0]


class MailScrubber:
    """One mail call's scrub: the registry and the owner's code-help state
    are read once (scrub_inputs) and applied to every message."""

    def __init__(self, sites: Iterable[Mapping[str, Any]] | None, owner_mfa_open: bool):
        self.sites = [s for s in (sites or ()) if isinstance(s, Mapping)]
        self.owner_mfa_open = bool(owner_mfa_open)
        self._regexes = scrub_context(self.sites)[1]

    def view(self, message: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """(masked copy, is a code email)."""
        return scrub_view(message, sites=self.sites, owner_mfa_open=self.owner_mfa_open)

    def text(self, value: Any, *, code_email: bool = False) -> Any:
        """Mask a derived text (an extracted summary) the same way."""
        if not (code_email or self.owner_mfa_open):
            return value
        # Keyword masking only: a summary spans every message of the call,
        # and a code email's own text never reached the extractor unmasked.
        return mask_text(value, regexes=self._regexes)


def scrubber_for(user_id: str) -> MailScrubber:
    return MailScrubber(*scrub_inputs(user_id))


def _profile_id_for(address: Any) -> str | None:
    """The profile id whose email is exactly this address, or None (no
    match, several, or an unsafe address)."""
    from app.core.supabase_client import get_supabase

    safe = mail_scope.normalize_address(address)
    if safe is None:
        return None
    pattern = safe.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (get_supabase().table("profiles").select("id,email").ilike("email", pattern).limit(3).execute().data
            or [])
    ids = {str(r["id"]) for r in rows if r.get("id") and str(r.get("email") or "").strip().casefold() == safe}
    return ids.pop() if len(ids) == 1 else None


def review_scrubber_for(reviewer_id: str, target_upn: Any) -> MailScrubber:
    """The scrub of a Mailbox review: every message is masked while the
    mailbox OWNER (the reviewed person) or the reviewer has an open code
    help. A target that cannot be resolved to one profile fails closed."""
    sites, reviewer_open = scrub_inputs(reviewer_id)
    if reviewer_open:
        return MailScrubber(sites, True)
    try:
        target_id = _profile_id_for(target_upn)
        target_open = True if target_id is None else bool(repo.browser_owner_code_help_open(target_id))
    except Exception:  # noqa: BLE001 - fail closed
        target_open = True
    return MailScrubber(sites, target_open)


def scrub_inputs(user_id: str) -> tuple[list[dict], bool]:
    """(registry sites, owner has an open code help) for one mail call. A
    read failure fails closed: no sites, but every message masked."""
    try:
        sites = list(repo.browser_sites(user_id, include_disabled=True, limit=500) or ())
    except Exception:  # noqa: BLE001 - fail closed below
        return [], True
    try:
        mfa_open = bool(repo.browser_owner_code_help_open(user_id))
    except Exception:  # noqa: BLE001
        mfa_open = True
    return sites, mfa_open


# ── The reader ───────────────────────────────────────────────────────────────


def _shared_mailboxes() -> set[str]:
    settings = get_settings()
    out: set[str] = set()
    for attr in ("shared_mailbox_set", "mailbox_exclusion_set"):
        try:
            out |= {str(x).strip().casefold() for x in (getattr(settings, attr, None) or ())}
        except TypeError:
            continue
    raw = getattr(settings, "rfp_email_ingestion_shared_mailboxes", "") or ""
    if isinstance(raw, str):
        out |= {x.strip().casefold() for x in raw.split(",") if x.strip()}
    return {x for x in out if x}


def owner_mailbox(user_id: str) -> tuple[str, str]:
    """(the owner's own primary mailbox, app token). Only the live profile
    address can be read; a shared, excluded or RFP team mailbox is refused
    even when it is the profile address."""
    try:
        primary = mail_scope.normalize_address(assistant_ms.sender_mailbox(user_id))
    except Exception:  # noqa: BLE001
        raise MfaMailRefused("mailbox_unavailable") from None
    if primary is None:
        raise MfaMailRefused("mailbox_unavailable")
    if primary in _shared_mailboxes():
        raise MfaMailRefused("shared_mailbox")
    try:
        mailbox, token = assistant_ms.for_mailbox(user_id, primary)
    except Exception:  # noqa: BLE001
        raise MfaMailRefused("mailbox_unavailable") from None
    if mail_scope.normalize_address(mailbox) != primary:
        raise MfaMailRefused("mailbox_unavailable")
    return primary, token


def _stamp(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo is not None else None


def _message_key(mailbox: str, message: Mapping[str, Any]) -> str:
    internet = str(message.get("internetMessageId") or "").strip().strip("<>").casefold()
    basis = "imid:" + internet if internet else "graph:" + mailbox + ":" + str(message.get("id") or "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _poll_window(user_id: str) -> int:
    try:
        _, settings = repo.browser_platform_config_v2(user_id)
        value = int((settings or {}).get("user_mfa_mail_poll_seconds") or DEFAULT_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001
        value = DEFAULT_WINDOW_SECONDS
    return max(MIN_WINDOW_SECONDS, min(MAX_WINDOW_SECONDS, value))


def _help_open(user_id: str, job_id: str, help_id: str) -> bool:
    state = repo.browser_help_state(user_id, job_id) or {}
    expires = _stamp(state.get("expires_at"))
    return bool(str(state.get("id") or "") == str(help_id) and state.get("status") == "open"
                and state.get("reason_code") in CODE_REASONS
                and expires is not None and expires > datetime.now(timezone.utc))


def _https_only(site: Mapping[str, Any], credential: Mapping[str, Any] | None) -> bool:
    origins = list((credential or {}).get("login_origins") or ()) or list(site.get("login_origins") or ())
    return all(isinstance(o, str) and o.casefold().startswith("https://") for o in origins)


def deliver_code(user_id: str, job_id: str, help_id: str, code: str, message_sha256: str) -> dict:
    """Seal the code as a help secret and hand it to the service-side RPC.
    Returns the RPC result ({"ok", "code"|"record_id", "code_delivered"})."""
    from app.services import assistant_browser_records as records
    from app.services import assistant_browser_vault as vault
    from app.services.assistant_browser_redaction import code_digest

    record_id = str(uuid4())
    row = records.pack_record(user_id, job_id, "help_secret", {"secret": code}, record_id=record_id, seq=0,
                              help_id=help_id, client_msg_id=record_id)
    key_id, key = vault._key()
    row["secret_hash"] = code_digest(key, code)
    row["secret_key_id"] = key_id
    return repo.browser_help_secret_from_mail(user_id, job_id, help_id, message_sha256, row) or {}


def _mailbox_path(mailbox: str) -> str:
    from urllib.parse import quote

    return f"/v1.0/users/{quote(mailbox, safe='@')}/messages"


def _list_recent(mailbox: str, token: str, since: datetime) -> list[dict]:
    page = GraphTransport(token, reader=True).read_json(
        _mailbox_path(mailbox),
        params={"$select": "id,from,receivedDateTime", "$orderby": "receivedDateTime desc",
                "$filter": "receivedDateTime ge " + since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "$top": str(MAX_LISTED)})
    rows = page.get("value") if isinstance(page, dict) else None
    return [r for r in rows[:MAX_LISTED] if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else []


def _read_one(mailbox: str, token: str, message_id: str) -> dict:
    from urllib.parse import quote

    value = GraphTransport(token, reader=True).read_json(
        _mailbox_path(mailbox) + "/" + quote(str(message_id), safe=""),
        params={"$select": "id,internetMessageId,from,toRecipients,ccRecipients,receivedDateTime,subject,body,"
                           "internetMessageHeaders"},
        prefer='outlook.body-content-type="text"')
    return value if isinstance(value, dict) else {}


def addressed_to(message: Mapping[str, Any], mailbox: str) -> bool:
    """The owner's mailbox is a To or Cc recipient. DKIM covers the From and
    To headers but not the SMTP envelope, so a genuine code email sent to
    someone else (an attacker's own account) and re-sent to the owner still
    passes DKIM: it is refused here."""
    owner = mail_scope.normalize_address(mailbox)
    if owner is None:
        return False
    for key in ("toRecipients", "ccRecipients"):
        for row in message.get(key) or ():
            address = ((row or {}).get("emailAddress") or {}).get("address") if isinstance(row, Mapping) else None
            if isinstance(address, str) and mail_scope.normalize_address(address.casefold()) == owner:
                return True
    return False


def _date_header_ok(message: Mapping[str, Any], since: datetime) -> bool:
    """False when the Date header says the message was written well before
    the window opened (a stale replay); a missing or unreadable Date passes
    (the received time already bounds it)."""
    values = [v for n, v in _header_list(message.get("internetMessageHeaders") or []) if n == "date"]
    if not values:
        return True
    try:
        written = parsedate_to_datetime(values[0])
    except (TypeError, ValueError, IndexError):
        return True
    if written is None or written.tzinfo is None:
        return True
    return written >= since - DATE_SKEW


def scan_once(user_id: str, job_id: str, help_id: str, *, mailbox: str, senders: list[str],
              site_regex: re.Pattern[str] | None, since: datetime, seen: set[str],
              tenant_authserv_id: str = TENANT_AUTHSERV_ID) -> str:
    """One pass over the owner's recent mail: "delivered", "stop" (two or
    more codes, or the help changed) or "none"."""
    mailbox, token = owner_mailbox(user_id)
    for row in _list_recent(mailbox, token, since):
        graph_id = str(row["id"])
        if graph_id in seen:
            continue
        received = _stamp(row.get("receivedDateTime"))
        listed_from = _domain(((row.get("from") or {}).get("emailAddress") or {}).get("address"))
        if received is None or received < since or not covered(listed_from, senders):
            seen.add(graph_id)
            continue
        mailbox, token = owner_mailbox(user_id)
        message = _read_one(mailbox, token, graph_id)
        # Marked only after a successful read, so a provider error retries it.
        seen.add(graph_id)
        address = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
        received = _stamp(message.get("receivedDateTime"))
        if str(message.get("id") or "") != graph_id or received is None or received < since:
            continue
        if not sender_trusted(message.get("internetMessageHeaders") or [], allowed_domains=senders,
                              tenant_authserv_id=tenant_authserv_id,
                              from_address=address if isinstance(address, str) else None):
            continue
        if not addressed_to(message, mailbox) or not _date_header_ok(message, since):
            continue
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        if str(body.get("contentType") or "text").casefold() != "text":
            continue
        text = str(message.get("subject") or "") + "\n" + str(body.get("content") or "")
        codes = site_codes(text, site_regex=site_regex)
        del text, body
        if not codes:
            continue
        if len(codes) > 1:
            return "stop"
        result = deliver_code(user_id, job_id, help_id, codes[0], _message_key(mailbox, message))
        if result.get("ok") is True:
            return "delivered"
        if result.get("code") == "message_used":
            continue
        return "stop"
    return "none"


def poll_for_code(user_id: str, job_id: str, help_id: str, *, site: dict[str, Any], since: datetime,
                  credential: Mapping[str, Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                  monotonic: Callable[[], float] = time.monotonic) -> bool:
    """Poll the owner's own mailbox every 10 s for the configured window;
    returns True when a code was delivered to the help path."""
    senders = trusted_senders(site)
    if not senders or not isinstance(since, datetime) or since.tzinfo is None or not _https_only(site, credential):
        return False
    regex = compile_site_regex(site.get("code_email_regex"))
    if regex is None and site.get("kind") != "ad_hoc":
        # A curated platform (Procore, Autodesk) also sends DKIM-signed mail
        # that carries other users' text (messages, RFI comments, bid
        # notes): the generic pattern could read a code a GC wrote. Only a
        # template regex anchored on the code email's own subject counts.
        return False
    try:
        mailbox, _ = owner_mailbox(user_id)
    except MfaMailRefused:
        return False
    deadline = monotonic() + _poll_window(user_id)
    seen: set[str] = set()
    while True:
        try:
            if not _help_open(user_id, job_id, help_id):
                return False
            outcome = scan_once(user_id, job_id, help_id, mailbox=mailbox, senders=senders, site_regex=regex,
                                since=since, seen=seen)
        except MfaMailRefused:
            return False
        except Exception as error:  # noqa: BLE001 - a provider failure is retried until the deadline
            logger.warning("assistant mfa mail: scan failed (%s) job=%s", type(error).__name__, job_id)
            outcome = "none"
        if outcome == "delivered":
            logger.info("assistant mfa mail: code delivered job=%s", job_id)
            return True
        if outcome == "stop" or monotonic() + POLL_INTERVAL_SECONDS > deadline:
            return False
        sleep(POLL_INTERVAL_SECONDS)


# ── Starting the reader when a code help opens ───────────────────────────────

_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def reader_inputs(job: Mapping[str, Any], help_state: Mapping[str, Any]) -> tuple[dict, dict | None, datetime] | None:
    """(site row, credential or None, since) for one open code help, or None
    when the reader should not run."""
    user_id = str(job.get("principal_user_id") or "")
    if (job.get("agent_type") != "user_browser" or job.get("principal_kind") != "user"
            or help_state.get("status") != "open" or help_state.get("reason_code") not in CODE_REASONS):
        return None
    credential = None
    if help_state.get("credential_id"):
        credential = repo.browser_credential_by_id(user_id, str(help_state["credential_id"]))
        if not credential or credential.get("status") != "active":
            return None
    site_key = (credential or {}).get("site_key") or job.get("platform") or (job.get("input") or {}).get("start_site_key")
    site = repo.browser_site(user_id, str(site_key)) if site_key else None
    if not site:
        return None
    opened = _stamp(help_state.get("created_at")) or datetime.now(timezone.utc)
    if help_state.get("is_login_scope"):
        since = _stamp((credential or {}).get("last_login_attempt_at"))
        if since is None:
            return None
    else:
        since = opened - STEP_UP_SKEW
        started = _stamp(job.get("started_at")) or _stamp(job.get("created_at"))
        if started is not None and started > since:
            since = started
    return site, credential, since


def _run(job: dict, help_id: str) -> None:
    user_id, job_id = str(job.get("principal_user_id") or ""), str(job.get("id") or "")
    try:
        state = repo.browser_help_state(user_id, job_id) or {}
        if str(state.get("id") or "") != help_id:
            return
        inputs = reader_inputs(job, state)
        if inputs is None:
            return
        site, credential, since = inputs
        poll_for_code(user_id, job_id, help_id, site=site, since=since, credential=credential)
    except Exception as error:  # noqa: BLE001 - the code card stays the path
        logger.warning("assistant mfa mail: reader stopped (%s) job=%s", type(error).__name__, job_id)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(help_id)


def start_for_help(job: Mapping[str, Any], *, help_id: str, reason: str, created: bool = True) -> bool:
    """Start the reader on a daemon thread for a newly opened mfa or
    mfa_step_up help (one reader per help per process). Returns whether a
    reader started. Call it after assistant_browser_help.open_help_v2."""
    if (not created or reason not in CODE_REASONS or not isinstance(job, Mapping)
            or job.get("agent_type") != "user_browser" or not assistant_ms.configured()):
        return False
    help_id = str(help_id)
    with _ACTIVE_LOCK:
        if help_id in _ACTIVE:
            return False
        _ACTIVE.add(help_id)
    try:
        threading.Thread(target=_run, args=(dict(job), help_id), name="assistant-mfa-mail", daemon=True).start()
    except Exception:  # noqa: BLE001
        with _ACTIVE_LOCK:
            _ACTIVE.discard(help_id)
        return False
    return True
