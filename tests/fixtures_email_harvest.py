"""Fakes for the email harvester suites (tests/test_rfp_email_files.py and
tests/test_rfp_email_harvest.py): the attachment and link shapes seen on the
dev database on 2026-09-16 (the probe record behind RFP_HARVEST.md 2.5),
recording stand-ins for the `cloud_folders` / `rfp_zip` functions the job
calls and for the Graph calls it makes, and the jsonb containment the reuse
query needs from the fake DB. The contract types (ShareLink, RemoteFile,
Listing, ZipMember, ZipListing) are the real ones.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import httpx

from app.services import cloud_folders, rfp_zip

PDF = b"%PDF-1.7\n" + b"y" * 200 + b"\n%%EOF\n"
ZIP_BYTES = b"PK\x03\x04" + b"z" * 300
DROPBOX_ZIP_BYTES = b"PK\x03\x04" + b"d" * 300


# ── Shapes from the dev database (probe record, 2026-09-16) ─────────────

# PDFs declared octet-stream, Outlook signature images, an image by
# extension with an octet-stream type, an attached email, a real photo.
OCTET_PDF = {
    "kind": "file", "name": "43ADG-S3999_Bid Package_Boilerplate Docs_REV 11 SEPT 2026.pdf",
    "size": 1328229, "contentType": "application/octet-stream",
}
REAL_PDF = {
    "kind": "file", "name": "RFP - 25-067CON Bus Shelters.pdf", "size": 699554,
    "contentType": "application/pdf",
}
OUTLOOK_PNG = {"kind": "file", "name": "Outlook-Global.png", "size": 7102, "contentType": "image/png"}
OUTLOOK_JPG = {"kind": "file", "name": "Outlook-3auvsbkr.jpg", "size": 205845, "contentType": "image/jpeg"}
IMAGE001 = {"kind": "file", "name": "image001.jpg", "size": 27784, "contentType": "image/jpeg"}
OCTET_PNG = {
    "kind": "file", "name": "1155E7C5@ECE3354C.A300A26A00000000.png", "size": 220783,
    "contentType": "application/octet-stream",
}
SITE_PHOTO = {"kind": "file", "name": "site-photo.jpg", "size": 2 * 1024 * 1024, "contentType": "image/jpeg"}
MSG_FILE = {"kind": "file", "name": "FW: Addendum 1.msg", "size": 45000, "contentType": "application/vnd.ms-outlook"}
ITEM_ATTACHMENT = {"kind": "item", "name": "Original invitation", "size": 12000, "contentType": None}
INLINE_PDF = {
    "kind": "file", "name": "Scope Letter.pdf", "size": 88000, "contentType": "application/pdf",
    "inline": True,
}
ZIP_FILE = {"kind": "file", "name": "Drawings.zip", "size": 5000, "contentType": "application/zip"}

SHAREPOINT_URL = (
    "https://eagle1lv.sharepoint.com/:f:/s/EOCExternal/"
    "IgBq0EfOK_k3RLqNQmoQ7ffbAWer_cGJ-U2o9utlWTMpSoo?e=SbFLil"
)
SHAREPOINT_KEY = "sharepoint:eagle1lv.sharepoint.com:IgBq0EfOK_k3RLqNQmoQ7ffbAWer_cGJ-U2o9utlWTMpSoo"
DROPBOX_URL = (
    "https://www.dropbox.com/scl/fo/cv15gi89jjt514by214om/AHvlRf6u0-J2cRcsqe9n4R8"
    "?rlkey=vt6nc04tsnd0b9b25kb3plckk&dl=0"
)
DROPBOX_KEY = "dropbox:cv15gi89jjt514by214om:vt6nc04tsnd0b9b25kb3plckk"
SHAREFILE_URL = "https://sletteninc.sharefile.com/d-s5272283033994a1b9120d20ad63b4fff"
SHAREFILE_KEY = "sharefile:sletteninc.sharefile.com:s5272283033994a1b9120d20ad63b4fff"
ONEDRIVE_URL = "https://g3electrical-my.sharepoint.com/:f:/p/t_moorejr/IgADezL9BWiKSK-Cc8YrBkNMAf6O1h4U_1l83ps6WDsw9FQ"
ONEDRIVE_KEY = "onedrive:g3electrical-my.sharepoint.com:IgADezL9BWiKSK-Cc8YrBkNMAf6O1h4U_1l83ps6WDsw9FQ"


# ── The contract types, built the way the tests read ─────────────────────


def link(url, key, *, provider="sharepoint", kind="folder", label="", supported=True):
    return cloud_folders.ShareLink(
        url=url, label=label or "", provider=provider, key=key, kind=kind, supported=supported
    )


def remote(path, size, locator):
    return cloud_folders.RemoteFile(path, size, locator)


def listing(status="listed", files=None, error=None, truncated=False, zip_path=None):
    return cloud_folders.Listing(status, list(files or []), error, truncated, zip_path)


def member(index, path, size, skipped_reason=None):
    return rfp_zip.ZipMember(index, path, size, skipped_reason)


def zip_listing(members=None, error=None, truncated=False, error_kind=None):
    return rfp_zip.ZipListing(list(members or []), error, truncated, error_kind)


SHAREPOINT_LINK = link(SHAREPOINT_URL, SHAREPOINT_KEY, label="26-080 UMC MLK Warehouse Remodel")
DROPBOX_LINK = link(DROPBOX_URL, DROPBOX_KEY, provider="dropbox")
SHAREFILE_LINK = link(SHAREFILE_URL, SHAREFILE_KEY, provider="sharefile", kind="unknown", supported=False)
ONEDRIVE_LINK = link(ONEDRIVE_URL, ONEDRIVE_KEY, provider="onedrive")

SP_FILES = [
    remote("Reference Material - Architectural - Drawing Index.pdf", 54978,
           "https://eagle1lv.sharepoint.com/sites/EOCExternal/_api/web/GetFileByServerRelativeUrl('a')/$value"),
    remote("Addendum 1 - Revised Drawings - Job Walk Photos/E1.01 Rev 1.pdf", 802000,
           "https://eagle1lv.sharepoint.com/sites/EOCExternal/_api/web/GetFileByServerRelativeUrl('b')/$value"),
    remote("Specifications/26 05 00 Common Work Results.pdf", 120500,
           "https://eagle1lv.sharepoint.com/sites/EOCExternal/_api/web/GetFileByServerRelativeUrl('c')/$value"),
]


def http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://graph.microsoft.com/v1.0/x")
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=httpx.Response(code, request=req))


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeCloud:
    """`cloud_folders` as the job uses it. `links` are what the parser
    finds: a link is "in" a text when its URL appears in it, answered in
    text order (`link_from_url` matches one URL the same way). `listings`
    maps a link key to a Listing or an exception (an unsupported link
    answers `unsupported` by itself, like the real resolver); `bytes_for` /
    `errors` drive `download` per locator (an error may be a list consumed
    one per call)."""

    def __init__(self) -> None:
        self.links: list = []
        self.listings: dict[str, object] = {}
        self.bytes_for: dict[str, bytes] = {}
        self.errors: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.on_download = None
        self.downloads = 0

    def find_share_links(self, text, html):
        self.calls.append(("find", text, html))
        haystack = f"{text or ''}\n{html or ''}"
        found, seen = [], set()
        for item in self.links:
            if item.url in haystack and item.key not in seen:
                seen.add(item.key)
                found.append((haystack.index(item.url), item))
        return [dataclasses.replace(item) for _, item in sorted(found, key=lambda pair: pair[0])]

    def link_from_url(self, url, label=""):
        self.calls.append(("link_from_url", url, label))
        for item in self.links:
            if item.url == url:
                return dataclasses.replace(item, label=label or item.label)
        return None

    def resolve(self, target, scratch, settings):
        self.calls.append(("resolve", target.key, scratch, settings))
        found = self.listings.get(target.key)
        if isinstance(found, BaseException):
            raise found
        if found is not None:
            return found
        if not target.supported:
            return listing("unsupported", [], "These links must be downloaded by hand.")
        return listing("unreachable", [], "No answer.")

    def _raise(self, key):
        err = self.errors.get(key)
        if isinstance(err, list):
            if err:
                raise err.pop(0)
        elif err is not None:
            raise err

    def download(self, locator, dest, *, max_bytes, timeout=None):
        self.calls.append(("download", locator, max_bytes))
        if self.downloads == 0 and self.on_download is not None:
            self.on_download()
        self.downloads += 1
        self._raise(locator)
        data = self.bytes_for.get(locator, PDF)
        if len(data) > max_bytes:
            raise cloud_folders.CloudForbidden("The file is larger than the per-file limit.")
        Path(dest).write_bytes(data)
        return len(data)

    def resolved(self):
        return [c[1] for c in self.calls if c[0] == "resolve"]

    def downloaded(self):
        return [c[1] for c in self.calls if c[0] == "download"]


class FakeZip:
    """`rfp_zip.inspect` keyed by the archive's bytes: a ZipListing (an
    empty one when nothing is configured)."""

    def __init__(self) -> None:
        self.listings: dict[bytes, object] = {}
        self.calls: list[dict] = []

    def inspect(self, path, *, max_members, max_total_bytes, per_member_max_bytes, is_image_name):
        data = Path(path).read_bytes()
        self.calls.append({
            "name": Path(path).name, "data": data, "max_members": max_members,
            "max_total_bytes": max_total_bytes, "per_member_max_bytes": per_member_max_bytes,
            "is_image_name": is_image_name,
        })
        found = self.listings.get(data)
        return found if found is not None else zip_listing()


class FakeGraph:
    """The three Graph touches: the attachment listing (`graph_request`),
    the reference-attachment links and the streamed download
    (`graph_inbox`), plus `get_message` for the HTML body. `gone` names
    mailboxes whose copy 404s; `listing_error` replaces the listing answer;
    `bytes_for` / `errors` drive downloads per attachment id; `pages`
    splits the listing into that many pages."""

    def __init__(self, attachments: list[dict] | None = None) -> None:
        self.attachments = list(attachments or [])
        self.references: list[dict] = []
        self.gone: set[str] = set()
        self.listing_error: BaseException | None = None
        self.reference_error: BaseException | None = None
        self.html: str | None = None
        self.bytes_for: dict[str, bytes] = {}
        self.errors: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.pages: int = 1
        self.on_download = None
        self.downloads = 0

    def graph_request(self, method, path, *, params=None, **kwargs):
        self.calls.append(("request", method, path, params))
        mailbox = path.split("/users/", 1)[1].split("/", 1)[0]
        if mailbox in self.gone:
            raise http_error(404)
        if self.listing_error is not None:
            raise self.listing_error
        page = int(path.split("$skip=", 1)[1]) if "$skip=" in path else 0
        per_page = max(1, -(-len(self.attachments) // self.pages))
        chunk = self.attachments[page * per_page:(page + 1) * per_page]
        body = {"value": [dict(a) for a in chunk]}
        if (page + 1) * per_page < len(self.attachments):
            body["@odata.nextLink"] = (
                f"https://graph.microsoft.com/v1.0{path.split('?', 1)[0]}?$skip={page + 1}"
            )
        return httpx.Response(200, json=body, request=httpx.Request("GET", "https://graph.microsoft.com/x"))

    def list_reference_links(self, message_id, *, mailbox=None):
        self.calls.append(("references", message_id, mailbox))
        if self.reference_error is not None:
            raise self.reference_error
        return [dict(r) for r in self.references]

    def _raise(self, key):
        err = self.errors.get(key)
        if isinstance(err, list):
            if err:
                raise err.pop(0)
        elif err is not None:
            raise err

    def download_attachment_to_file(self, message_id, attachment_id, *, mailbox, max_bytes, dest):
        self.calls.append(("download", mailbox, message_id, attachment_id, max_bytes))
        if self.downloads == 0 and self.on_download is not None:
            self.on_download()
        self.downloads += 1
        self._raise(attachment_id)
        data = self.bytes_for.get(attachment_id, PDF)
        if len(data) > max_bytes:
            from app.services import graph_inbox

            raise graph_inbox.AttachmentTooLarge("The attachment is larger than the per-file limit.")
        dest = Path(dest)
        if dest.exists():
            raise FileExistsError(dest)
        dest.write_bytes(data)
        return len(data)

    def get_message(self, message_id, *, mailbox=None, select=None, body_type=None):
        self.calls.append(("message", message_id, mailbox, select, body_type))
        if mailbox in self.gone:
            raise http_error(404)
        if self.html is None:
            return {"id": message_id, "body": {"contentType": "html", "content": ""}}
        return {"id": message_id, "body": {"contentType": "html", "content": self.html}}

    def downloaded(self):
        return [c[3] for c in self.calls if c[0] == "download"]


def jsonb_contains(stored, wanted) -> bool:
    """Postgres `@>` over parsed JSON: every key of a wanted object is in
    the stored object with a containing value; every element of a wanted
    array is contained by some element of the stored array."""
    if isinstance(wanted, dict):
        return isinstance(stored, dict) and all(
            k in stored and jsonb_contains(stored[k], v) for k, v in wanted.items()
        )
    if isinstance(wanted, list):
        return isinstance(stored, list) and all(
            any(jsonb_contains(s, v) for s in stored) for v in wanted
        )
    return stored == wanted
