"""Cloud-share link ingestion for inbound vendor replies.

Vendors often reply with a OneDrive/SharePoint, Google Drive, Dropbox, or Box
share link instead of attaching the quote file. This module finds those links
in a reply (body-HTML anchors plus Outlook "reference attachments"), resolves
each to a direct-download URL, and fetches the bytes under the same size cap
as real attachments — so the caller can store the file in project storage and
run extraction exactly as if the vendor had attached it.

SSRF stance: the downloader only ever connects to an allowlist of known
cloud-storage hosts, https only, re-checked on EVERY redirect hop — a hostile
reply can never point the server at internal services or arbitrary URLs. It is
also strictly ANONYMOUS: we never authenticate a fetch with the org's Graph app
token. A vendor-supplied URL fed to a tenant-wide token would be a confused
deputy — the outsider chooses which of the company's own OneDrive/SharePoint
files the server reads — so that path does not exist. Only links whose owner has
granted "anyone with the link" access resolve, exactly as intended.
"""

import base64
import logging
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_MAX_REDIRECTS = 5

# Hard cap on how many links one email can yield. A real vendor reply carries a
# handful; anything near this is a hostile body stuffed with tens of thousands of
# distinct allowlisted-host URLs to make the caller do unbounded per-link work
# (audit rows, fetch attempts). Well above any legitimate reply, so it never
# bites a real vendor, and the fetch cap (inbound_link_max_count) is far lower.
_MAX_LINKS = 200

# Every host the downloader may connect to (share hosts + the hosts providers
# redirect to for the actual bytes). Anything else is refused, per hop.
_ALLOWED_HOSTS = {
    "1drv.ms",
    "onedrive.live.com",
    "api.onedrive.com",
    "drive.google.com",
    "drive.usercontent.google.com",
    "dropbox.com",
    "www.dropbox.com",
    "dl.dropboxusercontent.com",
    "app.box.com",
}
_ALLOWED_SUFFIXES = (
    ".sharepoint.com",
    ".1drv.com",
    ".googleusercontent.com",
    ".dropbox.com",
    ".box.com",
    ".boxcloud.com",
)


@dataclass
class CloudLink:
    url: str        # unwrapped share URL
    label: str      # anchor text / attachment name — often the real filename
    provider: str   # 'onedrive' | 'gdrive' | 'dropbox' | 'box'


@dataclass
class FetchedFile:
    filename: str
    content: bytes
    content_type: str


class CloudLinkError(Exception):
    """A link that could not be turned into file bytes; `reason` is one of
    'auth_required', 'html_page', 'too_large', 'unreachable', 'unsupported'."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(detail or reason)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_allowed(host: str | None) -> bool:
    return bool(host) and (host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_SUFFIXES))


def _provider(url: str) -> str | None:
    h = _host(url)
    if h in ("1drv.ms", "onedrive.live.com") or h.endswith(".sharepoint.com"):
        return "onedrive"
    if h == "drive.google.com":
        return "gdrive"
    if h in ("dropbox.com", "www.dropbox.com") or h.endswith(".dropbox.com"):
        return "dropbox"
    if h == "app.box.com" or h.endswith(".app.box.com"):
        return "box"
    return None


def _unwrap(url: str) -> str:
    """Undo redirect/tracking wrappers (Outlook Safe Links, Google's /url) so
    provider detection sees the real target."""
    for _ in range(3):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        qs = parse_qs(parsed.query)
        if host.endswith(".safelinks.protection.outlook.com") and qs.get("url"):
            url = qs["url"][0]
            continue
        if host in ("www.google.com", "google.com") and parsed.path == "/url":
            target = qs.get("q") or qs.get("url")
            if target:
                url = target[0]
                continue
        return url
    return url


def link_from_url(url: str, label: str = "") -> CloudLink | None:
    """A CloudLink for `url` when it belongs to a supported provider."""
    url = _unwrap(unescape(url or "").strip())
    provider = _provider(url)
    if not provider:
        return None
    return CloudLink(url=url, label=(label or "").strip(), provider=provider)


class _AnchorParser(HTMLParser):
    """Collects (target-url, anchor-text) pairs. Outlook rewrites href to Safe
    Links but keeps the original URL in `originalsrc` — prefer it."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._current: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        d = dict(attrs)
        self._current = d.get("originalsrc") or d.get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self.anchors.append((self._current, "".join(self._text).strip()))
            self._current = None


_URL_RE = re.compile(r"https://[^\s\"'<>\)\]]+")

# Query params that carry the share's identity (everything else is tracking
# noise that varies between the anchor and its Safe-Links twin).
_IDENTITY_PARAMS = ("id", "resid", "authkey", "share", "shared_name")


def _dedup_key(url: str) -> tuple:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    keep = tuple(
        (k, tuple(qs[k])) for k in _IDENTITY_PARAMS if k in qs
    )
    return ((parsed.hostname or "").lower(), parsed.path, keep)


def find_cloud_links(body: str) -> list[CloudLink]:
    """Every supported cloud-share link in an email body (HTML or plain text),
    deduplicated, anchor-labelled ones first."""
    if not body:
        return []
    parser = _AnchorParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:  # noqa: BLE001 — hostile HTML must never break ingestion
        logger.warning("Anchor parse failed; falling back to regex scan")
    candidates = list(parser.anchors)
    candidates += [(u, "") for u in _URL_RE.findall(body)]

    seen: set[tuple] = set()
    links: list[CloudLink] = []
    for raw, label in candidates:
        link = link_from_url(raw, label)
        if link is None:
            continue
        key = _dedup_key(link.url)
        if key in seen:
            continue
        seen.add(key)
        links.append(link)
        if len(links) >= _MAX_LINKS:
            break
    return links


def merge_links(*groups: list[CloudLink]) -> list[CloudLink]:
    seen: set[tuple] = set()
    merged: list[CloudLink] = []
    for group in groups:
        for link in group:
            key = _dedup_key(link.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(link)
            if len(merged) >= _MAX_LINKS:
                return merged
    return merged


# ── Download-candidate resolution ───────────────────────────────────────────


def _share_id(url: str) -> str:
    """Graph/OneDrive sharing-URL token (the documented u!<base64url> form)."""
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _with_param(url: str, key: str, value: str) -> str:
    sep = "&" if urlparse(url).query else "?"
    return f"{url}{sep}{key}={value}"


def _gdrive_file_id(url: str) -> str | None:
    parsed = urlparse(url)
    m = re.search(r"/file/d/([\w-]+)", parsed.path)
    if m:
        return m.group(1)
    return (parse_qs(parsed.query).get("id") or [None])[0]


def _candidates(link: CloudLink) -> list[tuple[str, str]]:
    """(kind, target) download attempts in order; kind 'graph' is a Graph API
    path called with the app token, 'plain' an unauthenticated https GET."""
    url = link.url
    if link.provider == "onedrive":
        return [
            # Works for "Anyone with the link" business/SharePoint shares.
            ("plain", _with_param(url, "download", "1")),
            # Anonymous redemption for consumer OneDrive (1drv.ms) shares.
            ("plain", f"https://api.onedrive.com/v1.0/shares/{_share_id(url)}/root/content"),
            # NB: no Graph /shares candidate. Resolving a vendor-supplied URL with
            # the tenant-wide app token would read the ORG's own files on an
            # outsider's command (confused deputy) — see the module docstring.
            # A share that refuses anonymous download simply fails 'auth_required'.
        ]
    if link.provider == "gdrive":
        file_id = _gdrive_file_id(url)
        if not file_id:  # a folder or a native Doc/Sheet — no single file to pull
            return []
        return [("plain", f"https://drive.google.com/uc?export=download&id={file_id}")]
    if link.provider == "dropbox":
        direct = re.sub(r"([?&])dl=0\b", r"\g<1>dl=1", url)
        if "dl=1" not in direct:
            direct = _with_param(direct, "dl", "1")
        return [("plain", direct)]
    if link.provider == "box":
        m = re.search(r"/s/([A-Za-z0-9]+)", urlparse(url).path)
        if not m:
            return []
        return [
            ("plain",
             f"https://{_host(url)}/index.php?rm=box_download_shared_file"
             f"&shared_name={m.group(1)}"),
        ]
    return []


# ── Fetching ────────────────────────────────────────────────────────────────


def _client() -> httpx.Client:
    # Redirects are followed manually so every hop is allowlist-checked.
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=False)


def _http_fetch(url: str, max_bytes: int) -> tuple[bytes, str, str | None, str]:
    """GET with per-hop host validation. Returns (content, content_type,
    content_disposition, final_url)."""
    with _client() as client:
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not _host_allowed(host):
                raise CloudLinkError("unreachable", f"host not allowed: {host or '?'}")
            with client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise CloudLinkError("unreachable", "redirect without location")
                    url = urljoin(url, location)
                    continue
                if resp.status_code in (401, 403):
                    raise CloudLinkError("auth_required", f"HTTP {resp.status_code}")
                if resp.status_code != 200:
                    raise CloudLinkError("unreachable", f"HTTP {resp.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise CloudLinkError("too_large", f"exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return (
                    b"".join(chunks),
                    resp.headers.get("content-type", ""),
                    resp.headers.get("content-disposition"),
                    url,
                )
        raise CloudLinkError("unreachable", "too many redirects")


def _looks_like_html(content: bytes, content_type: str) -> bool:
    if (content_type or "").split(";")[0].strip().lower() in ("text/html", "application/xhtml+xml"):
        return True
    head = content[:256].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html"))


_CD_FILENAME_STAR = re.compile(r"filename\*\s*=\s*utf-8''([^;]+)", re.IGNORECASE)
_CD_FILENAME = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)
_EXT_FOR_TYPE = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/csv": ".csv",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def _sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip().strip(".")
    return name[:150] or "linked-file"


def _filename_from(
    content_disposition: str | None, label: str, final_url: str, content_type: str
) -> str:
    if content_disposition:
        m = _CD_FILENAME_STAR.search(content_disposition)
        if m:
            return _sanitize_filename(unquote(m.group(1)))
        m = _CD_FILENAME.search(content_disposition)
        if m:
            return _sanitize_filename(m.group(1))
    if label and "." in label:
        return _sanitize_filename(label)
    path_name = unquote(urlparse(final_url).path.rsplit("/", 1)[-1])
    if "." in path_name:
        return _sanitize_filename(path_name)
    ext = _EXT_FOR_TYPE.get((content_type or "").split(";")[0].strip().lower(), "")
    return _sanitize_filename((label or path_name or "linked-file") + ext)


# Lower rank = more actionable for the PE; kept when candidates fail differently.
_REASON_RANK = {"auth_required": 0, "html_page": 1, "too_large": 2, "unreachable": 3}


def fetch(link: CloudLink, *, max_bytes: int) -> FetchedFile:
    """Download the file behind a share link, trying each candidate strategy.
    Raises CloudLinkError (with the most actionable candidate reason) when
    none of them yields real file bytes."""
    candidates = _candidates(link)
    if not candidates:
        raise CloudLinkError("unsupported", f"no download strategy for {link.url}")
    best: CloudLinkError | None = None

    def _keep(err: CloudLinkError) -> None:
        nonlocal best
        if best is None or _REASON_RANK.get(err.reason, 9) < _REASON_RANK.get(best.reason, 9):
            best = err

    for _kind, target in candidates:
        try:
            content, ctype, disposition, final_url = _http_fetch(target, max_bytes)
        except CloudLinkError as exc:
            if exc.reason == "too_large":
                raise  # every candidate serves the same file; retrying can't shrink it
            _keep(exc)
            continue
        except Exception as exc:  # noqa: BLE001 — DNS/TLS/protocol failures
            _keep(CloudLinkError("unreachable", str(exc)))
            continue
        if not content or _looks_like_html(content, ctype):
            # A 200 that renders a page (sign-in / interstitial), not the file.
            _keep(CloudLinkError("html_page", "got a web page, not a file"))
            continue
        return FetchedFile(
            filename=_filename_from(disposition, link.label, final_url, ctype),
            content=content,
            content_type=ctype or "application/octet-stream",
        )
    raise best or CloudLinkError("unreachable")
