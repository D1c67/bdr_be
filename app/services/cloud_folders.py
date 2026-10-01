"""Cloud-share links in a GC's invitation email (docs/RFP_HARVEST.md 2.5).

A GC that invites directly sends the bid documents as SharePoint / OneDrive
folders, Dropbox folders, Google Drive folders or single files on any of
them. `find_share_links` spots every such link in the email (plain text and
HTML), `resolve` turns one into a listing of the files behind it (relative
paths, sizes, a locator each) and `download` fetches one locator into a file.
`cloud_links.py` stays the single-file vendor-reply path; this module is the
folder-aware harvester side and shares its stance.

SSRF stance, the cloud_links one: https only, every host on one allowlist of
share hosts re-checked on every redirect hop, strictly anonymous (no Graph
app token, no stored credentials). The only cookies ever sent are the guest
`FedAuth` cookie a SharePoint share sets when it is redeemed, and only back to
the `*.sharepoint.com` host that set it: `resolve` redeems a share into a
fresh in-memory jar, lists the folder with it and parks the jar in a
process-wide registry keyed by host and folder so `download`, called later
in the same job, can present it for that folder's files. Nothing is ever
persisted. Every stream is capped on the bytes received, never on a declared
size; the Dropbox folder zip is inspected by `rfp_zip` under the same caps.

Provider behaviour (from the 2026-09-16 probes, section 2.5 of the doc):
SharePoint and OneDrive for Business answer the share URL with a 302 that
sets FedAuth and lands on the library view whose `id=` is the folder's
server-relative path, then `/_api/web/GetFolderByServerRelativeUrl` lists and
`GetFileByServerRelativeUrl(...)/$value` serves. Consumer OneDrive (1drv.ms)
goes through the anonymous shares API on api.onedrive.com. A Dropbox folder
with `dl=1` redirects to a zip on dropboxusercontent.com. Google Drive files
go through `uc?export=download` (the large-file confirm page followed once);
folders through the Drive API when a key is configured, else the keyless
embedded folder view. Box serves a file through its shared-file download
route and a folder as a web page (unsupported). ShareFile, Egnyte,
WeTransfer and Hightail are recognised for the card and never contacted.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.services import rfp_zip
from app.services.cloud_links import _AnchorParser, _filename_from, _sanitize_filename, _unwrap

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 5
_MAX_LINKS = 200               # hard cap on links one email can yield (see cloud_links)
_DOWNLOAD_CHUNK = 1024 * 1024
_MAX_PAGE_BYTES = 2 * 1024 * 1024    # an HTML page we read (confirm page, embedded view)
_MAX_JSON_BYTES = 8 * 1024 * 1024    # a listing payload
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# Every host a fetch may connect to, re-checked per hop. Share hosts plus the
# hosts the providers redirect to for the bytes.
_ALLOWED_HOSTS = {
    "1drv.ms",
    "onedrive.live.com",
    "api.onedrive.com",
    "dropbox.com",
    "www.dropbox.com",
    "drive.google.com",
    "drive.usercontent.google.com",
    "www.googleapis.com",
    "app.box.com",
}
_ALLOWED_SUFFIXES = (
    ".sharepoint.com",
    ".1drv.com",
    ".dropbox.com",
    ".dropboxusercontent.com",
    ".googleusercontent.com",
    ".box.com",
    ".boxcloud.com",
)

# Sign-in walls: a hop that lands here means the share is not anonymous.
_SP_LOGIN_HOSTS = {
    "login.microsoftonline.com",
    "login.live.com",
    "login.microsoft.com",
    "login.windows.net",
}
_SP_LOGIN_PATHS = (
    "/_layouts/15/authenticate.aspx",
    "/_layouts/15/accessdenied.aspx",
    "/_forms/default.aspx",
)
_GOOGLE_LOGIN_HOSTS = {"accounts.google.com"}

_PROVIDER_NAMES = {
    "sharepoint": "SharePoint",
    "onedrive": "OneDrive",
    "dropbox": "Dropbox",
    "gdrive": "Google Drive",
    "box": "Box",
    "sharefile": "ShareFile",
    "egnyte": "Egnyte",
    "wetransfer": "WeTransfer",
    "hightail": "Hightail",
}
_UNSUPPORTED_PROVIDERS = ("sharefile", "egnyte", "wetransfer", "hightail")

_GDRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_GDRIVE_NATIVE_PREFIX = "application/vnd.google-apps."
_GDRIVE_ONLY_NATIVE = (
    "The folder holds only Google Docs, Sheets or Slides, which must be downloaded by hand."
)


# ── Errors ───────────────────────────────────────────────────────────────────


class CloudError(Exception):
    """Base: the message is app-authored and safe to store."""


class CloudTransient(CloudError):
    """Network trouble, a 5xx or a 429 on a download: worth retrying later."""


class CloudForbidden(CloudError):
    """The host refused the file, served a page instead of bytes, or the
    file is over the cap. Permanent for this link."""


class CloudUnavailable(CloudError):
    """Kept for the harvest runner's error family; no cloud provider locks
    itself, so `locked_until` is always None here."""

    def __init__(self, message: str, *, locked_until: datetime | None = None) -> None:
        super().__init__(message)
        self.locked_until = locked_until


class _Refused(Exception):
    """Internal: a hop pointed off the allowlist or off https."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


class _LoginWall(Exception):
    """Internal: a hop matched the provider's sign-in predicate."""


class _TooManyHops(Exception):
    """Internal: the redirect cap was reached."""


class _OverCap(CloudForbidden):
    """Internal: a stream passed the caller's byte cap (a CloudForbidden to
    the outside; resolve turns a Dropbox folder zip over the cap into a
    Listing)."""

    def __init__(self) -> None:
        super().__init__("The file is larger than the per-file limit.")


class _HtmlAnswer(Exception):
    """Internal: the host served an HTML page where bytes or JSON were
    expected; `page` holds up to _MAX_PAGE_BYTES of it for the callers that
    act on it (Dropbox password form, Google Drive confirm)."""

    def __init__(self, page: str, url: str) -> None:
        super().__init__(url)
        self.page = page
        self.url = url


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class ShareLink:
    url: str          # unwrapped share URL, Outlook decoration removed
    label: str        # anchor text / attachment name, "" when none
    provider: str     # sharepoint | onedrive | dropbox | gdrive | box | sharefile | egnyte | wetransfer | hightail
    key: str          # stable dedup key
    kind: str         # folder | file | unknown
    supported: bool   # this build can resolve it


@dataclass
class RemoteFile:
    path: str         # relative path inside the share, "/"-separated, no leading "/"
    size: int | None
    locator: str      # what `download` accepts: an https URL, or "zip:<absolute zip path>|<member index>"


@dataclass
class Listing:
    status: str       # listed | needs_sign_in | unsupported | unreachable | html_page | too_many_files
    files: list[RemoteFile]
    error: str | None  # a user-facing sentence for anything but "listed"
    truncated: bool   # file cap or depth cap hit while walking
    # A Dropbox folder's zip in scratch (the members' `zip:` locators point at
    # it); the harvester may re-inspect it with its own image policy to record
    # the skipped members. None for every other provider.
    zip_path: str | None = None


# ── Link detection (pure) ────────────────────────────────────────────────────


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_allowed(host: str | None) -> bool:
    return bool(host) and (host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_SUFFIXES))


def _is_sharepoint_host(host: str) -> bool:
    return host.endswith(".sharepoint.com")


# Outlook decorates a pasted share link with tracking that varies per copy.
_DECORATION_PARAMS = {"xsdata", "sdata", "e"}


def _strip_decoration(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [
        (k, v) for k, v in _qs_pairs(parsed.query) if k.lower() not in _DECORATION_PARAMS
    ]
    query = urlencode(kept, safe="/:!*'()")
    return parsed._replace(query=query, fragment="").geturl()


def _qs_pairs(query: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((unquote(k), unquote(v)))
    return pairs


def _qs(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items() if v}


def _last_segment(path: str) -> str:
    return unquote(path.rstrip("/").rsplit("/", 1)[-1])


_SP_KIND_RE = re.compile(r"^/:([a-z]):/")
_SP_FILE_KINDS = set("bwxpvuit")
_DROPBOX_SCL_RE = re.compile(r"^/scl/(fo|fi)/([^/]+)")
_DROPBOX_SH_RE = re.compile(r"^/(sh|s)/([^/]+)")
_GDRIVE_FOLDER_RE = re.compile(r"/folders/([\w-]+)")
_GDRIVE_FILE_RE = re.compile(r"/file/d/([\w-]+)")
_GDOCS_RE = re.compile(r"^/(document|spreadsheets|presentation|forms|drawings)/d/([\w-]+)")
_BOX_RE = re.compile(r"^/s/([A-Za-z0-9]+)")


def _classify(url: str, label: str) -> ShareLink | None:
    """A ShareLink for `url` when it lives on a recognised share host, else
    None. `url` is already unwrapped and decoration-free."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if parsed.scheme != "https" or not host:
        return None

    if _is_sharepoint_host(host):
        provider = "onedrive" if host.endswith("-my.sharepoint.com") else "sharepoint"
        m = _SP_KIND_RE.match(path)
        if m:
            kind = "folder" if m.group(1) == "f" else "file" if m.group(1) in _SP_FILE_KINDS else "unknown"
            token = _last_segment(path)
        else:
            qs = _qs(url)
            token = qs.get("share") or qs.get("id") or qs.get("sourcedoc") or _last_segment(path)
            kind = "unknown"
        if not token:
            return None
        return ShareLink(url, label, provider, f"sharepoint:{host}:{token}", kind, True)

    if host == "1drv.ms":
        token = _last_segment(path)
        if not token:
            return None
        return ShareLink(url, label, "onedrive", f"sharepoint:{host}:{token}", "unknown", True)

    if host == "onedrive.live.com":
        qs = _qs(url)
        token = qs.get("resid") or qs.get("id") or _last_segment(path)
        if not token:
            return None
        return ShareLink(url, label, "onedrive", f"sharepoint:{host}:{token}", "unknown", True)

    if host in ("dropbox.com", "www.dropbox.com") or host.endswith(".dropbox.com"):
        m = _DROPBOX_SCL_RE.match(path)
        if m:
            rlkey = _qs(url).get("rlkey", "")
            kind = "folder" if m.group(1) == "fo" else "file"
            return ShareLink(url, label, "dropbox", f"dropbox:{m.group(2)}:{rlkey}", kind, True)
        m = _DROPBOX_SH_RE.match(path)
        if m:
            kind = "folder" if m.group(1) == "sh" else "file"
            return ShareLink(url, label, "dropbox", f"dropbox:{m.group(2)}", kind, True)
        return None

    if host == "drive.google.com":
        m = _GDRIVE_FOLDER_RE.search(path)
        if m:
            return ShareLink(url, label, "gdrive", f"gdrive:{m.group(1)}", "folder", True)
        m = _GDRIVE_FILE_RE.search(path)
        if m:
            return ShareLink(url, label, "gdrive", f"gdrive:{m.group(1)}", "file", True)
        if path in ("/open", "/uc"):
            file_id = _qs(url).get("id")
            if file_id:
                return ShareLink(url, label, "gdrive", f"gdrive:{file_id}", "file", True)
        return None

    if host == "docs.google.com":
        m = _GDOCS_RE.match(path)
        if m:
            return ShareLink(url, label, "gdrive", f"gdrive:{m.group(2)}", "file", False)
        return None

    if host == "app.box.com" or host.endswith(".box.com"):
        m = _BOX_RE.match(path)
        if m:
            return ShareLink(url, label, "box", f"box:{m.group(1)}", "unknown", True)
        return None

    if host.endswith(".sharefile.com") or host.endswith(".sharefile.eu"):
        ident = _last_segment(path)
        if not ident or path == "/":
            return None
        return ShareLink(url, label, "sharefile", f"sharefile:{host}:{ident}", "unknown", False)

    if host.endswith(".egnyte.com"):
        if path in ("", "/"):
            return None
        return ShareLink(url, label, "egnyte", f"egnyte:{host}:{path}", "unknown", False)

    if host in ("wetransfer.com", "www.wetransfer.com", "we.tl"):
        if path in ("", "/"):
            return None
        return ShareLink(url, label, "wetransfer", f"wetransfer:{path}", "unknown", False)

    if host in ("hightail.com", "www.hightail.com", "spaces.hightail.com"):
        if path in ("", "/"):
            return None
        return ShareLink(url, label, "hightail", f"hightail:{path}", "unknown", False)

    return None


def link_from_url(url: str, label: str = "") -> ShareLink | None:
    """A ShareLink for `url` when it belongs to a recognised share host
    (wrappers unwrapped, Outlook decoration stripped), else None."""
    raw = unescape(url or "").strip()
    if not raw:
        return None
    raw = _unwrap(raw)
    raw = raw.rstrip(".,;:!?>")
    try:
        cleaned = _strip_decoration(raw)
    except ValueError:
        return None
    label = " ".join((label or "").split())
    if label.lower().startswith(("http://", "https://", "www.")):
        label = ""
    return _classify(cleaned, label)


_URL_RE = re.compile(r"https://[^\s\"'<>\)\]]+")


def find_share_links(text: str | None, html: str | None) -> list[ShareLink]:
    """Every recognised share link in the email: HTML anchors (Outlook's
    `originalsrc` preferred over a Safe Links href) first, then bare URLs
    in the text and in the HTML. Deduplicated by key, the first label kept
    and a non-empty label winning over an empty one; at most _MAX_LINKS."""
    candidates: list[tuple[str, str]] = []
    if html:
        parser = _AnchorParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:  # noqa: BLE001 - hostile HTML must never break the harvest
            logger.warning("Anchor parse failed; falling back to a URL scan")
        candidates += list(parser.anchors)
    if text:
        candidates += [(u, "") for u in _URL_RE.findall(text)]
    if html:
        candidates += [(u, "") for u in _URL_RE.findall(html)]

    by_key: dict[str, ShareLink] = {}
    order: list[str] = []
    for raw, label in candidates:
        link = link_from_url(raw, label)
        if link is None:
            continue
        seen = by_key.get(link.key)
        if seen is not None:
            if not seen.label and link.label:
                seen.label = link.label
            continue
        if len(order) >= _MAX_LINKS:
            break
        by_key[link.key] = link
        order.append(link.key)
    return [by_key[k] for k in order]


# ── HTTP primitive ───────────────────────────────────────────────────────────


def _client(timeout: float) -> httpx.Client:
    # Redirects are followed manually so every hop is allowlist-checked, and
    # requests are built by hand so the client's own jar never reaches a
    # request (see the module docstring on cookies).
    return httpx.Client(
        timeout=httpx.Timeout(connect=10, read=timeout, write=timeout, pool=30),
        follow_redirects=False,
    )


@dataclass
class _Hit:
    resp: httpx.Response   # open, streamed; the caller closes it
    url: str               # the landing URL
    hops: list[str]        # every URL requested, the landing one last

    def close(self) -> None:
        self.resp.close()

    def __enter__(self) -> _Hit:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _open(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    jar: httpx.Cookies | None = None,
    refuse: Callable[[str], bool] | None = None,
) -> _Hit:
    """Streamed GET with manual redirects: every hop https and allowlisted
    (`_Refused` otherwise), `refuse(url)` answering True raises `_LoginWall`
    before the request is made, at most _MAX_REDIRECTS redirects
    (`_TooManyHops`). `jar` is presented to, and filled from, `*.sharepoint.com`
    hosts only. Returns the final response still open."""
    hops: list[str] = []
    hdrs = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    for _ in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if refuse is not None and refuse(url):
            raise _LoginWall()
        if parsed.scheme != "https" or not _host_allowed(host):
            raise _Refused(url)
        request = httpx.Request("GET", url, headers=hdrs)
        use_jar = jar is not None and _is_sharepoint_host(host)
        if use_jar:
            jar.set_cookie_header(request)
        resp = client.send(request, stream=True)
        if use_jar:
            jar.extract_cookies(resp)
        hops.append(url)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            resp.close()
            if not location:
                raise _Refused(url)
            url = urljoin(url, location)
            continue
        return _Hit(resp, url, hops)
    raise _TooManyHops()


def _content_type(resp: httpx.Response) -> str:
    return (resp.headers.get("content-type") or "").split(";")[0].strip().lower()


def _is_html_type(resp: httpx.Response) -> bool:
    return _content_type(resp) in ("text/html", "application/xhtml+xml")


def _read_bounded(resp: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
        total += len(chunk)
        chunks.append(chunk)
        if total >= limit:
            resp.close()
            break
    return b"".join(chunks)[:limit]


def _read_text(resp: httpx.Response) -> str:
    return _read_bounded(resp, _MAX_PAGE_BYTES).decode("utf-8", "replace")


def _read_json(resp: httpx.Response):
    """The response as parsed JSON, or None when it is not JSON."""
    raw = _read_bounded(resp, _MAX_JSON_BYTES)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


# ── Generic folder walk ──────────────────────────────────────────────────────


@dataclass
class _Walk:
    files: list[RemoteFile] = field(default_factory=list)
    truncated: bool = False
    too_many: bool = False
    native_skipped: int = 0


def _walk(
    root,
    list_level: Callable[[object], tuple[list[RemoteFile], list]],
    *,
    max_depth: int,
    cap: int,
    into: _Walk | None = None,
) -> _Walk:
    """Breadth-first over `list_level(node) -> (files, subnodes)` from `root`
    to `max_depth` levels of subfolders. `too_many` when the first level
    alone exceeds `cap`; otherwise the walk stops (truncated) when the next
    file would pass the cap or a subfolder lies past the depth cap. `into`
    is the record to fill (a closure may count into it while walking)."""
    out = into if into is not None else _Walk()
    files, subs = list_level(root)
    if len(files) > cap:
        out.too_many = True
        return out
    out.files.extend(files)
    pending = [(sub, 1) for sub in subs]
    while pending:
        node, depth = pending.pop(0)
        if depth > max_depth:
            out.truncated = True
            continue
        files, subs = list_level(node)
        for f in files:
            if len(out.files) >= cap:
                out.truncated = True
                break
            out.files.append(f)
        else:
            pending.extend((sub, depth + 1) for sub in subs)
            continue
        break
    return out


def _listing_from_walk(walk: _Walk, cap: int, provider: str) -> Listing:
    if walk.too_many:
        return Listing(
            "too_many_files",
            [],
            f"The {provider} folder holds more than {cap} files at its top level; "
            "the harvest accepts at most that many.",
            False,
        )
    error = None
    if not walk.files and walk.native_skipped:
        error = _GDRIVE_ONLY_NATIVE
    return Listing("listed", walk.files, error, walk.truncated)


# ── SharePoint / OneDrive ────────────────────────────────────────────────────

# Guest jars parked by `resolve` for `download`: host -> [(scope, jar)], the
# scope being the server-relative folder (or the share path for a single
# file) the jar was redeemed for. Lives for the process; a harvest resolves
# and downloads within one job, so nothing older is ever needed and
# `forget_jars()` exists for tests and for a caller that wants a clean slate.
_JARS: dict[str, list[tuple[str, httpx.Cookies]]] = {}


def _register_jar(host: str, scope: str, jar: httpx.Cookies) -> None:
    entries = _JARS.setdefault(host, [])
    entries[:] = [(s, j) for s, j in entries if s != scope]
    entries.append((scope, jar))


def _jar_for(host: str, path: str) -> httpx.Cookies | None:
    entries = _JARS.get(host)
    if not entries:
        return None
    best = None
    for scope, jar in entries:
        if path == scope or path.startswith(scope.rstrip("/") + "/"):
            if best is None or len(scope) > len(best[0]):
                best = (scope, jar)
    return best[1] if best else entries[-1][1]


def forget_jars() -> None:
    """Drop every parked SharePoint guest jar."""
    _JARS.clear()


_SITE_RE = re.compile(r"^(/(?:sites|teams|personal)/[^/]+)", re.IGNORECASE)
_SP_FILE_LOCATOR_RE = re.compile(r"/_api/web/GetFileByServerRelativeUrl\('(.*)'\)/\$value$", re.IGNORECASE)


def _sp_literal(path: str) -> str:
    """`path` as the percent-encoded body of an OData `('...')` literal."""
    return quote(path.replace("'", "''"), safe="/")


def _sp_site(path: str) -> str:
    m = _SITE_RE.match(path)
    return m.group(1) if m else ""


def _sp_file_locator(host: str, site: str, server_relative: str) -> str:
    return f"https://{host}{site}/_api/web/GetFileByServerRelativeUrl('{_sp_literal(server_relative)}')/$value"


def _sp_login_wall(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return host in _SP_LOGIN_HOSTS or any(p in path for p in _SP_LOGIN_PATHS)


def _sp_path_from_locator(url: str) -> str:
    """The server-relative path a stored SharePoint locator addresses, for
    the jar lookup: the `GetFileByServerRelativeUrl` literal, else the URL
    path itself."""
    parsed = urlparse(url)
    m = _SP_FILE_LOCATOR_RE.search(unquote(parsed.path))
    if m:
        return m.group(1).replace("''", "'")
    return parsed.path


def _sp_list_folder(
    client: httpx.Client, jar: httpx.Cookies, host: str, site: str, server_relative: str
) -> dict:
    """One `GetFolderByServerRelativeUrl` call; the parsed JSON. Raises
    _LoginWall on 401/403 and CloudTransient / CloudForbidden otherwise."""
    url = (
        f"https://{host}{site}/_api/web/GetFolderByServerRelativeUrl('{_sp_literal(server_relative)}')"
        "?$expand=Folders,Files&$select=Name,ItemCount,Folders/Name,Folders/ServerRelativeUrl,"
        "Files/Name,Files/Length,Files/ServerRelativeUrl"
    )
    with _open(
        client, url, headers={"Accept": "application/json;odata=nometadata"}, jar=jar,
        refuse=_sp_login_wall,
    ) as hit:
        if hit.resp.status_code in (401, 403):
            raise _LoginWall()
        if hit.resp.status_code == 429 or hit.resp.status_code >= 500:
            raise CloudTransient(f"SharePoint answered {hit.resp.status_code} for the folder.")
        if hit.resp.status_code != 200:
            raise CloudForbidden(f"SharePoint answered {hit.resp.status_code} for the folder.")
        if _is_html_type(hit.resp):
            raise _HtmlAnswer(_read_text(hit.resp), hit.url)
        data = _read_json(hit.resp)
    if not isinstance(data, dict):
        raise _HtmlAnswer("", url)
    return data


def _int_or_none(value) -> int | None:
    # `Length` is a string in the nometadata JSON ("54978").
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sp_file_meta(
    client: httpx.Client, jar: httpx.Cookies, host: str, site: str, server_relative: str
) -> RemoteFile | None:
    """`GetFileByServerRelativeUrl(...)?$select=Name,Length` for a share
    whose `id=` may be a file: the RemoteFile when SharePoint knows it as
    one, None when it does not (a folder, or gone)."""
    url = (
        f"https://{host}{site}/_api/web/GetFileByServerRelativeUrl('{_sp_literal(server_relative)}')"
        "?$select=Name,Length"
    )
    with _open(
        client, url, headers={"Accept": "application/json;odata=nometadata"}, jar=jar,
        refuse=_sp_login_wall,
    ) as hit:
        if hit.resp.status_code in (401, 403):
            raise _LoginWall()
        if hit.resp.status_code != 200 or _is_html_type(hit.resp):
            return None
        data = _read_json(hit.resp)
    if not isinstance(data, dict) or not data.get("Name"):
        return None
    return RemoteFile(
        str(data["Name"]), _int_or_none(data.get("Length")), _sp_file_locator(host, site, server_relative)
    )


def _resolve_sharepoint(link: ShareLink, settings) -> Listing:
    jar = httpx.Cookies()
    with _client(settings.cloud_request_timeout_seconds) as client:
        with _open(client, link.url, jar=jar, refuse=_sp_login_wall) as hit:
            status = hit.resp.status_code
            landing = hit.url
        if status in (401, 403):
            return _needs_sign_in(link)
        if status == 429 or status >= 500:
            return Listing("unreachable", [], f"The share host answered {status}.", False)
        if status != 200:
            return Listing("unreachable", [], f"The share link answered HTTP {status}.", False)
        host = _host(landing)
        item = _qs(landing).get("id", "")
        if not _is_sharepoint_host(host) or not item.startswith("/"):
            if link.kind != "folder" and _is_sharepoint_host(_host(link.url)):
                return _sp_single_file_via_download(client, jar, link)
            return Listing(
                "html_page", [], "The share opened a web page instead of a folder or a file.", False
            )
        site = _sp_site(item)
        item = item.rstrip("/")
        if link.kind == "file":
            meta = _sp_file_meta(client, jar, host, site, item)
            if meta is None:
                return _sp_single_file_via_download(client, jar, link)
            _register_jar(host, item, jar)
            return Listing("listed", [meta], None, False)
        if link.kind == "unknown":
            # A landing whose `id=` may name a file or a folder: ask for the
            # file first (one cheap call), walk it as a folder otherwise.
            meta = _sp_file_meta(client, jar, host, site, item)
            if meta is not None:
                _register_jar(host, item, jar)
                return Listing("listed", [meta], None, False)
        return _sp_walk(client, jar, host, site, item, link, settings)


def _sp_walk(client, jar, host, site, root, link, settings) -> Listing:
    prefix = root.rstrip("/") + "/"

    def list_level(folder: str) -> tuple[list[RemoteFile], list[str]]:
        data = _sp_list_folder(client, jar, host, site, folder)
        files: list[RemoteFile] = []
        for f in data.get("Files") or []:
            if not isinstance(f, dict):
                continue
            server_relative = f.get("ServerRelativeUrl") or f"{folder}/{f.get('Name', '')}"
            name = f.get("Name") or _last_segment(server_relative)
            if server_relative.startswith(prefix):
                rel = server_relative[len(prefix):]
            else:
                rel = name
            files.append(RemoteFile(rel, _int_or_none(f.get("Length")), _sp_file_locator(host, site, server_relative)))
        subs: list[str] = []
        library_root = folder.count("/") == site.count("/") + 1
        for d in data.get("Folders") or []:
            if not isinstance(d, dict):
                continue
            name = d.get("Name") or ""
            if not name or (library_root and name == "Forms"):
                continue
            subs.append(d.get("ServerRelativeUrl") or f"{folder}/{name}")
        return files, subs

    walk = _walk(
        root, list_level, max_depth=settings.rfp_harvest_folder_max_depth, cap=settings.rfp_harvest_file_cap
    )
    _register_jar(host, root, jar)
    return _listing_from_walk(walk, settings.rfp_harvest_file_cap, _PROVIDER_NAMES[link.provider])


def _sp_single_file_via_download(client, jar, link: ShareLink) -> Listing:
    """A file share whose landing page carries no path: `?download=1` on the
    share URL serves the bytes (headers read, body never fetched here)."""
    target = _with_param(link.url, "download", "1")
    with _open(client, target, jar=jar, refuse=_sp_login_wall) as hit:
        status = hit.resp.status_code
        if status in (401, 403):
            return _needs_sign_in(link)
        if status != 200 or _is_html_type(hit.resp):
            return Listing(
                "html_page", [], "The share opened a web page instead of a folder or a file.", False
            )
        name = _filename_from(
            hit.resp.headers.get("content-disposition"), link.label, hit.url,
            hit.resp.headers.get("content-type", ""),
        )
        size = _header_size(hit.resp)
    _register_jar(_host(link.url), urlparse(link.url).path, jar)
    return Listing("listed", [RemoteFile(name, size, target)], None, False)


def _header_size(resp: httpx.Response) -> int | None:
    try:
        return int(resp.headers.get("content-length", ""))
    except ValueError:
        return None


def _with_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    pairs = [(k, v) for k, v in _qs_pairs(parsed.query) if k != key]
    pairs.append((key, value))
    return parsed._replace(query=urlencode(pairs, safe="/:!*'()"), fragment="").geturl()


def _needs_sign_in(link: ShareLink) -> Listing:
    return Listing(
        "needs_sign_in",
        [],
        f"The {_PROVIDER_NAMES[link.provider]} link needs a sign-in; download it by hand.",
        False,
    )


# Consumer OneDrive (1drv.ms, onedrive.live.com): the anonymous shares API.


def _onedrive_share_id(url: str) -> str:
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _onedrive_get(client: httpx.Client, url: str, link: ShareLink):
    with _open(client, url, headers={"Accept": "application/json"}, refuse=_sp_login_wall) as hit:
        if hit.resp.status_code in (401, 403, 404):
            raise _LoginWall()
        if hit.resp.status_code == 429 or hit.resp.status_code >= 500:
            raise CloudTransient(f"OneDrive answered {hit.resp.status_code}.")
        if hit.resp.status_code != 200:
            raise CloudForbidden(f"OneDrive answered HTTP {hit.resp.status_code}.")
        if _is_html_type(hit.resp):
            raise _HtmlAnswer(_read_text(hit.resp), hit.url)
        data = _read_json(hit.resp)
    if not isinstance(data, dict):
        raise _HtmlAnswer("", url)
    return data


def _resolve_onedrive_consumer(link: ShareLink, settings) -> Listing:
    base = f"https://api.onedrive.com/v1.0/shares/{_onedrive_share_id(link.url)}"
    with _client(settings.cloud_request_timeout_seconds) as client:
        root = _onedrive_get(client, f"{base}/root?$select=id,name,size,file,folder", link)
        if "folder" not in root:
            name = _sanitize_filename(root.get("name") or link.label or "shared-file")
            size = root.get("size") if isinstance(root.get("size"), int) else None
            return Listing("listed", [RemoteFile(name, size, f"{base}/root/content")], None, False)

        def list_level(rel: str) -> tuple[list[RemoteFile], list[str]]:
            url = f"{base}/root/children" if not rel else f"{base}/root:/{quote(rel)}:/children"
            files: list[RemoteFile] = []
            subs: list[str] = []
            for _page in range(50):
                data = _onedrive_get(client, url, link)
                for item in data.get("value") or []:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    child = f"{rel}/{item['name']}" if rel else item["name"]
                    if "folder" in item:
                        subs.append(child)
                        continue
                    locator = item.get("@content.downloadUrl") or f"{base}/root:/{quote(child)}:/content"
                    size = item.get("size") if isinstance(item.get("size"), int) else None
                    files.append(RemoteFile(child, size, locator))
                nxt = data.get("@odata.nextLink")
                if not nxt or not _host_allowed(_host(nxt)):
                    break
                url = nxt
            return files, subs

        walk = _walk(
            "", list_level, max_depth=settings.rfp_harvest_folder_max_depth,
            cap=settings.rfp_harvest_file_cap,
        )
    return _listing_from_walk(walk, settings.rfp_harvest_file_cap, "OneDrive")


# ── Dropbox ──────────────────────────────────────────────────────────────────


def _dropbox_direct(url: str) -> str:
    return _with_param(url, "dl", "1")


_PASSWORD_INPUT_RE = re.compile(r"<input[^>]+(?:type|name)\s*=\s*[\"']?password", re.IGNORECASE)


def _resolve_dropbox(link: ShareLink, scratch: Path, settings) -> Listing:
    target = _dropbox_direct(link.url)
    with _client(settings.cloud_request_timeout_seconds) as client:
        if link.kind == "file":
            return _probe_single_file(client, link, target, "Dropbox")
        # A folder: the zip Dropbox serves for it, streamed into scratch.
        zip_path = Path(scratch) / (re.sub(r"[^A-Za-z0-9._-]+", "_", link.key)[:120] + ".zip")
        if zip_path.exists():
            os.unlink(zip_path)
        try:
            _stream_to_file(
                client, target, zip_path, max_bytes=settings.rfp_harvest_max_total_bytes,
                jar=None, provider="Dropbox",
            )
        except _HtmlAnswer as exc:
            if _PASSWORD_INPUT_RE.search(exc.page):
                return _needs_sign_in(link)
            return Listing(
                "html_page", [], "The Dropbox link opened a web page instead of the folder.", False
            )
        except _OverCap:
            return Listing(
                "too_many_files",
                [],
                "The Dropbox folder is larger than the harvest accepts "
                f"({settings.rfp_harvest_max_total_bytes // (1024 * 1024)} MB).",
                False,
            )
    inspected = rfp_zip.inspect(
        zip_path,
        max_members=settings.rfp_harvest_file_cap,
        max_total_bytes=settings.rfp_harvest_max_total_bytes,
        per_member_max_bytes=settings.rfp_ingest_max_file_bytes,
        is_image_name=lambda _name: False,
    )
    if inspected.error:
        status = (
            "too_many_files" if inspected.error_kind in ("too_large", "too_many_entries") else "unreachable"
        )
        return Listing(status, [], inspected.error, False, str(zip_path))
    files = [
        RemoteFile(m.path, m.size, f"zip:{zip_path}|{m.index}")
        for m in inspected.members
        if m.skipped_reason is None
    ]
    return Listing("listed", files, None, inspected.truncated, str(zip_path))


def _probe_single_file(client, link: ShareLink, target: str, provider: str, *, refuse=None) -> Listing:
    """Headers of a direct-download URL, the body never read: the filename
    and size for one RemoteFile, or the page it answered instead."""
    with _open(client, target, refuse=refuse) as hit:
        status = hit.resp.status_code
        if status in (401, 403):
            return _needs_sign_in(link)
        if status == 404 or status == 410:
            return Listing("unreachable", [], f"The {provider} link no longer works (HTTP {status}).", False)
        if status == 429 or status >= 500:
            return Listing("unreachable", [], f"{provider} answered {status}.", False)
        if status != 200:
            return Listing("unreachable", [], f"{provider} answered HTTP {status}.", False)
        if _is_html_type(hit.resp):
            page = _read_text(hit.resp)
            if _PASSWORD_INPUT_RE.search(page) or any(h in page for h in _GOOGLE_LOGIN_HOSTS):
                return _needs_sign_in(link)
            return Listing(
                "html_page", [], f"The {provider} link opened a web page instead of a file.", False
            )
        name = _filename_from(
            hit.resp.headers.get("content-disposition"), link.label, hit.url,
            hit.resp.headers.get("content-type", ""),
        )
        return Listing("listed", [RemoteFile(name, _header_size(hit.resp), target)], None, False)


# ── Google Drive ─────────────────────────────────────────────────────────────


def _gdrive_id(link: ShareLink) -> str:
    return link.key.split(":", 1)[1]


def _gdrive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={quote(file_id, safe='')}"


def _google_login_wall(url: str) -> bool:
    return _host(url) in _GOOGLE_LOGIN_HOSTS


_GDRIVE_CONFIRM_NAME_RE = re.compile(
    r'class="uc-name-size"[^>]*>\s*<a[^>]*>([^<]+)</a>', re.IGNORECASE
)


def _resolve_gdrive_file(client, link: ShareLink, settings) -> Listing:
    file_id = _gdrive_id(link)
    target = _gdrive_file_url(file_id)
    with _open(client, target, refuse=_google_login_wall) as hit:
        status = hit.resp.status_code
        if status in (401, 403, 404):
            return _needs_sign_in(link)
        if status == 429 or status >= 500:
            return Listing("unreachable", [], f"Google Drive answered {status}.", False)
        if status != 200:
            return Listing("unreachable", [], f"Google Drive answered HTTP {status}.", False)
        if _is_html_type(hit.resp):
            page = _read_text(hit.resp)
            if "confirm=" in page or 'name="uuid"' in page or "uuid=" in page:
                m = _GDRIVE_CONFIRM_NAME_RE.search(page)
                name = unescape(m.group(1)).strip() if m else ""
                name = _sanitize_filename(name or link.label or file_id)
                return Listing("listed", [RemoteFile(name, None, target)], None, False)
            if any(h in page for h in _GOOGLE_LOGIN_HOSTS):
                return _needs_sign_in(link)
            return Listing(
                "html_page", [], "The Google Drive link opened a web page instead of a file.", False
            )
        name = _filename_from(
            hit.resp.headers.get("content-disposition"), link.label, hit.url,
            hit.resp.headers.get("content-type", ""),
        )
        return Listing("listed", [RemoteFile(name, _header_size(hit.resp), target)], None, False)


def _gdrive_api_get(client, url: str) -> dict:
    with _open(client, url, headers={"Accept": "application/json"}, refuse=_google_login_wall) as hit:
        if hit.resp.status_code in (401, 403, 404):
            raise _LoginWall()
        if hit.resp.status_code == 429 or hit.resp.status_code >= 500:
            raise CloudTransient(f"Google Drive answered {hit.resp.status_code}.")
        if hit.resp.status_code != 200:
            raise CloudForbidden(f"Google Drive answered HTTP {hit.resp.status_code}.")
        if _is_html_type(hit.resp):
            raise _HtmlAnswer(_read_text(hit.resp), hit.url)
        data = _read_json(hit.resp)
    if not isinstance(data, dict):
        raise _HtmlAnswer("", url)
    return data


def _resolve_gdrive_folder_api(client, link: ShareLink, settings) -> Listing:
    key = settings.google_drive_api_key
    walk_state = _Walk()

    def list_level(node: tuple[str, str]) -> tuple[list[RemoteFile], list[tuple[str, str]]]:
        folder_id, rel = node
        files: list[RemoteFile] = []
        subs: list[tuple[str, str]] = []
        page_token = None
        for _page in range(50):
            params = [
                ("q", f"'{folder_id}' in parents"),
                ("fields", "nextPageToken,files(id,name,size,mimeType)"),
                ("pageSize", "200"),
                ("key", key),
            ]
            if page_token:
                params.append(("pageToken", page_token))
            url = "https://www.googleapis.com/drive/v3/files?" + urlencode(params, quote_via=quote)
            data = _gdrive_api_get(client, url)
            for item in data.get("files") or []:
                if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                    continue
                child = f"{rel}/{item['name']}" if rel else item["name"]
                mime = item.get("mimeType") or ""
                if mime == _GDRIVE_FOLDER_MIME:
                    subs.append((item["id"], child))
                    continue
                if mime.startswith(_GDRIVE_NATIVE_PREFIX):
                    walk_state.native_skipped += 1
                    continue
                files.append(RemoteFile(child, _int_or_none(item.get("size")), _gdrive_file_url(item["id"])))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return files, subs

    walk = _walk(
        (_gdrive_id(link), ""), list_level, max_depth=settings.rfp_harvest_folder_max_depth,
        cap=settings.rfp_harvest_file_cap, into=walk_state,
    )
    return _listing_from_walk(walk, settings.rfp_harvest_file_cap, "Google Drive")


_FLIP_ENTRY_RE = re.compile(
    r'<div class="flip-entry" id="entry-([^"]+)"(.*?)<div class="flip-entry-title">(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_FLIP_ICON_RE = re.compile(r'<[^>]*class="[^"]*flip-entry-icon[^"]*"[^>]*>', re.IGNORECASE)


def _resolve_gdrive_folder_embedded(client, link: ShareLink, settings) -> Listing:
    walk_state = _Walk()

    def list_level(node: tuple[str, str]) -> tuple[list[RemoteFile], list[tuple[str, str]]]:
        folder_id, rel = node
        url = f"https://drive.google.com/embeddedfolderview?id={quote(folder_id, safe='')}#list"
        with _open(client, url, refuse=_google_login_wall) as hit:
            if hit.resp.status_code in (401, 403, 404):
                raise _LoginWall()
            if hit.resp.status_code == 429 or hit.resp.status_code >= 500:
                raise CloudTransient(f"Google Drive answered {hit.resp.status_code}.")
            if hit.resp.status_code != 200:
                raise CloudForbidden(f"Google Drive answered HTTP {hit.resp.status_code}.")
            page = _read_text(hit.resp)
        files: list[RemoteFile] = []
        subs: list[tuple[str, str]] = []
        entries = _FLIP_ENTRY_RE.findall(page)
        if not entries and any(h in page for h in _GOOGLE_LOGIN_HOSTS):
            raise _LoginWall()
        for entry_id, body, title in entries:
            name = " ".join(unescape(re.sub(r"<[^>]+>", "", title)).split())
            if not name:
                continue
            child = f"{rel}/{name}" if rel else name
            icon = _FLIP_ICON_RE.search(body)
            icon_text = (icon.group(0) if icon else "").lower()
            if "folder" in icon_text or "/drive/folders/" in body or "embeddedfolderview" in body:
                subs.append((entry_id, child))
                continue
            if "vnd.google-apps." in icon_text:
                walk_state.native_skipped += 1
                continue
            files.append(RemoteFile(child, None, _gdrive_file_url(entry_id)))
        return files, subs

    walk = _walk(
        (_gdrive_id(link), ""), list_level, max_depth=settings.rfp_harvest_folder_max_depth,
        cap=settings.rfp_harvest_file_cap, into=walk_state,
    )
    return _listing_from_walk(walk, settings.rfp_harvest_file_cap, "Google Drive")


def _resolve_gdrive(link: ShareLink, settings) -> Listing:
    with _client(settings.cloud_request_timeout_seconds) as client:
        if link.kind == "folder":
            if settings.google_drive_api_key:
                return _resolve_gdrive_folder_api(client, link, settings)
            return _resolve_gdrive_folder_embedded(client, link, settings)
        return _resolve_gdrive_file(client, link, settings)


# ── Box ──────────────────────────────────────────────────────────────────────


def _resolve_box(link: ShareLink, settings) -> Listing:
    shared_name = link.key.split(":", 1)[1]
    target = (
        "https://app.box.com/index.php?rm=box_download_shared_file"
        f"&shared_name={quote(shared_name, safe='')}"
    )
    with _client(settings.cloud_request_timeout_seconds) as client:
        with _open(client, target) as hit:
            status = hit.resp.status_code
            if status in (401, 403):
                return _needs_sign_in(link)
            if status in (404, 410):
                return Listing("unreachable", [], f"The Box link no longer works (HTTP {status}).", False)
            if status == 429 or status >= 500:
                return Listing("unreachable", [], f"Box answered {status}.", False)
            if status != 200:
                return Listing("unreachable", [], f"Box answered HTTP {status}.", False)
            disposition = hit.resp.headers.get("content-disposition") or ""
            on_boxcloud = _host(hit.url).endswith(".boxcloud.com")
            if _is_html_type(hit.resp) and not on_boxcloud and "attachment" not in disposition.lower():
                return Listing("unsupported", [], "Box folders must be downloaded by hand.", False)
            name = _filename_from(
                disposition, link.label, hit.url, hit.resp.headers.get("content-type", "")
            )
            return Listing("listed", [RemoteFile(name, _header_size(hit.resp), target)], None, False)


# ── resolve ──────────────────────────────────────────────────────────────────


def resolve(link: ShareLink, scratch: Path, settings) -> Listing:
    """The files behind one share link (module docstring). Never raises for
    a link problem: sign-in walls, pages, refused hosts, caps and network
    trouble all come back as a Listing status with a sentence."""
    if not link.supported or link.provider in _UNSUPPORTED_PROVIDERS:
        name = _PROVIDER_NAMES.get(link.provider, "These")
        if link.provider == "gdrive":
            name = "Google Docs"
        return Listing("unsupported", [], f"{name} links must be downloaded by hand.", False)
    try:
        if link.provider in ("sharepoint", "onedrive"):
            if _host(link.url) in ("1drv.ms", "onedrive.live.com"):
                return _resolve_onedrive_consumer(link, settings)
            return _resolve_sharepoint(link, settings)
        if link.provider == "dropbox":
            return _resolve_dropbox(link, Path(scratch), settings)
        if link.provider == "gdrive":
            return _resolve_gdrive(link, settings)
        if link.provider == "box":
            return _resolve_box(link, settings)
        name = _PROVIDER_NAMES.get(link.provider, "These")
        return Listing("unsupported", [], f"{name} links must be downloaded by hand.", False)
    except _LoginWall:
        return _needs_sign_in(link)
    except _Refused:
        return Listing("unreachable", [], "The share redirected to a host outside its provider.", False)
    except _TooManyHops:
        return Listing("unreachable", [], "The share redirected too many times.", False)
    except CloudTransient as exc:
        return Listing("unreachable", [], str(exc), False)
    except _HtmlAnswer:
        return Listing(
            "html_page", [], "The share answered a web page instead of a folder or a file.", False
        )
    except CloudForbidden as exc:
        return Listing("unreachable", [], str(exc), False)
    except (httpx.RequestError, httpx.InvalidURL) as exc:
        return Listing(
            "unreachable", [], f"The share host did not answer ({type(exc).__name__}).", False
        )


# ── download ─────────────────────────────────────────────────────────────────


def _open_excl(dest: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o644)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _looks_like_html(head: bytes) -> bool:
    return head[:256].lstrip().lower().startswith((b"<!doctype", b"<html"))


def _stream_to_file(
    client: httpx.Client, url: str, dest: Path, *, max_bytes: int, jar: httpx.Cookies | None,
    provider: str,
) -> int:
    """GET `url` into `dest` (O_EXCL, removed on any failure) under a running
    cap on the bytes received. Raises _HtmlAnswer when a page comes back,
    CloudForbidden for refusals / the cap, CloudTransient for network trouble,
    5xx and 429; _Refused / _TooManyHops propagate."""
    out = _open_excl(dest)
    written = 0
    try:
        with out:
            try:
                with _open(client, url, jar=jar) as hit:
                    status = hit.resp.status_code
                    if status in (401, 403, 404, 410):
                        raise CloudForbidden(f"{provider} refused the file link (HTTP {status}).")
                    if status == 429 or status >= 500:
                        raise CloudTransient(f"{provider} answered {status} for the file.")
                    if status != 200:
                        raise CloudForbidden(f"{provider} answered HTTP {status} for the file.")
                    if _is_html_type(hit.resp):
                        raise _HtmlAnswer(_read_text(hit.resp), hit.url)
                    chunks = hit.resp.iter_bytes(_DOWNLOAD_CHUNK)
                    first = next(chunks, b"")
                    if _looks_like_html(first):
                        # A page served as octet-stream: keep reading the
                        # same iterator (a second iter_bytes is refused).
                        page = [first]
                        total = len(first)
                        for chunk in chunks:
                            page.append(chunk)
                            total += len(chunk)
                            if total >= _MAX_PAGE_BYTES:
                                break
                        hit.resp.close()
                        raise _HtmlAnswer(
                            b"".join(page)[:_MAX_PAGE_BYTES].decode("utf-8", "replace"), hit.url
                        )
                    for chunk in itertools.chain((first,), chunks):
                        written += len(chunk)
                        if written > max_bytes:
                            hit.resp.close()
                            raise _OverCap()
                        out.write(chunk)
            except httpx.TransportError as exc:
                raise CloudTransient(
                    f"{provider} did not answer ({type(exc).__name__})."
                ) from exc
    except BaseException:
        _unlink_quietly(dest)
        raise
    return written


_GDRIVE_UUID_RE = re.compile(r'name="uuid"\s+value="([^"]+)"', re.IGNORECASE)
_GDRIVE_CONFIRM_RE = re.compile(r'(?:name="confirm"\s+value="([^"]+)"|[?&]confirm=([^&"\']+))', re.IGNORECASE)


def _gdrive_confirm_url(locator: str, page: str) -> str | None:
    """The second GET for a Google Drive file behind the "cannot scan for
    viruses" page, or None when the page is not that form."""
    file_id = _qs(locator).get("id")
    if not file_id or not ("confirm=" in page or 'name="uuid"' in page or "uuid=" in page):
        return None
    m = _GDRIVE_CONFIRM_RE.search(page)
    confirm = (m.group(1) or m.group(2)) if m else "t"
    params = [("id", file_id), ("export", "download"), ("confirm", confirm or "t")]
    m = _GDRIVE_UUID_RE.search(page)
    if m:
        params.append(("uuid", m.group(1)))
    return "https://drive.usercontent.google.com/download?" + urlencode(params, quote_via=quote)


def _is_gdrive_uc(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() == "drive.google.com" and parsed.path == "/uc"


def download(locator: str, dest: Path, *, max_bytes: int, timeout: float | None = None) -> int:
    """Fetch one locator into `dest` (created O_EXCL, removed on any failure)
    in 1 MB chunks under a running cap on the bytes received. Returns the
    bytes written.

    `zip:<path>|<index>` inflates a member through rfp_zip.extract_member;
    an https URL is fetched with the per-hop allowlist (at most 5 redirects),
    presenting a parked SharePoint guest jar to `*.sharepoint.com` hosts
    only, and following Google Drive's large-file confirm page once. Raises
    CloudTransient for network trouble, a 5xx or a 429; CloudForbidden for
    401/403/404/410, a refused host or scheme, a page where bytes were
    expected, or the cap. An existing `dest` raises FileExistsError before
    anything is fetched.
    """
    dest = Path(dest)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    if locator.startswith("zip:"):
        body = locator[4:]
        zip_path, sep, index = body.rpartition("|")
        if not sep or not index.isdigit() or not zip_path:
            raise CloudForbidden("The zip locator is malformed.")
        try:
            return rfp_zip.extract_member(Path(zip_path), int(index), dest, max_bytes=max_bytes)
        except rfp_zip.ZipTooLarge as exc:
            raise CloudForbidden("The file is larger than the per-file limit.") from exc
        except rfp_zip.ZipError as exc:
            raise CloudForbidden(str(exc)) from exc

    parsed = urlparse(locator)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _host_allowed(host):
        raise CloudForbidden("The file link points outside the share providers.")
    jar = _jar_for(host, _sp_path_from_locator(locator)) if _is_sharepoint_host(host) else None
    if timeout is None:
        timeout = get_settings().cloud_request_timeout_seconds
    provider = _provider_name_for_host(host)
    target, hop_jar = locator, jar
    with _client(timeout) as client:
        try:
            for attempt in range(2):
                try:
                    return _stream_to_file(
                        client, target, dest, max_bytes=max_bytes, jar=hop_jar, provider=provider
                    )
                except _HtmlAnswer as exc:
                    confirm = None
                    if attempt == 0 and _is_gdrive_uc(locator):
                        confirm = _gdrive_confirm_url(locator, exc.page)
                    if confirm is None:
                        raise CloudForbidden(
                            f"{provider} answered a web page instead of the file."
                        ) from exc
                    target, hop_jar = confirm, None
        except _Refused as exc:
            raise CloudForbidden(
                "The file link redirected to a host outside the share providers."
            ) from exc
        except _TooManyHops as exc:
            raise CloudForbidden("The file link redirected too many times.") from exc
        except httpx.TransportError as exc:
            raise CloudTransient(f"{provider} did not answer ({type(exc).__name__}).") from exc
    raise CloudForbidden(f"{provider} answered a web page instead of the file.")


def _provider_name_for_host(host: str) -> str:
    if _is_sharepoint_host(host) or host in ("1drv.ms", "onedrive.live.com", "api.onedrive.com") or host.endswith(".1drv.com"):
        return "SharePoint"
    if "dropbox" in host:
        return "Dropbox"
    if "google" in host:
        return "Google Drive"
    if "box" in host:
        return "Box"
    return "The share host"
