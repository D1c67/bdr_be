"""RFP Ingestion: the email harvester's job body (docs/RFP_HARVEST.md 2.5).

The third harvester behind `rfp_harvest.harvester_for` (key `email`) for
the `organic`, `general` and `nonorganic` methods. There is no platform to
ask: the email's own attachments and share links are the bid package, so
`data` is the email's file story (doc section 4), the facts are what the
extract step already put on the email row, and the harvest row is keyed by
the email itself (`email:{rfp_email_id}`).

`harvest` runs under `rfp_harvest._run_claimed` exactly like the Procore
and PipelineSuite bodies and reuses the shared machinery (`_harvest_files`,
`_check_caps`, `_complete_harvest`, `_update_claimed`, `_renew`,
`_scratch_dir`, `_email_html`): attachments are re-listed live from Graph
(the stored meta has no ids), zips are opened now into the job's scratch
directory, share links are resolved through `cloud_folders`, a link's files
that an earlier complete harvest already accepted are reused instead of
downloaded, and then the remaining entries go through the ordinary
download loop with an `EmailFileSession` that dispatches on the locator
prefix. Locators (Graph attachment ids, scratch paths, URLs) live only in
the in-memory `urls` list, never on a stored entry.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.services import cloud_folders, graph_inbox, rfp_test, rfp_zip
from app.services import rfp_harvest as base
from app.services.graph_email import graph_request
from app.services.rfp_email_files import (
    EMAIL_METHODS,
    FILE_EXPANDED,
    FILE_REUSED,
    HARVESTER_EMAIL,
    KIND_FILE,
    KIND_ITEM,
    KIND_REFERENCE,
    KIND_UNKNOWN,
    ORIGIN_LINK,
    ORIGIN_ZIP,
    PROVIDER_GRAPH,
    SKIP_IMAGE,
    EmailRef,
    attachment_entry,
    classify_attachments,
    image_record,
    is_image_name,
    is_zip_name,
    link_file_entry,
    zip_member_entry,
)

__all__ = ["EMAIL_METHODS", "HARVESTER_EMAIL", "EmailFileSession", "EmailRef", "harvest",
           "prior_link_files"]

logger = logging.getLogger(__name__)

# Locator prefixes the session dispatches on (in-memory only). `eml:` is
# the test bench's (docs/RFP_TESTING.md 5.2): a part of a forwarded message
# attachment, written by re-fetching the MIME and decoding that one part.
LOCATOR_GRAPH = "graph:"
LOCATOR_ZIP = "zip:"
LOCATOR_HTTPS = "https://"
LOCATOR_EML = "eml:"

LINK_UNSUPPORTED = "unsupported"
LINK_UNREACHABLE = "unreachable"
LINK_SKIPPED_CAP = "skipped_cap"

# `contentId` lives on fileAttachment only and Graph answers 400 when it is
# named in a $select over the attachments collection (verified live
# 2026-09-16); `isInline` is on the base type and is all the policy needs.
_ATTACHMENT_SELECT = "id,name,contentType,size,isInline"
_GRAPH_V1 = "https://graph.microsoft.com/v1.0"
_LISTING_PAGES_MAX = 5
_REUSE_ROWS_MAX = 20

_MSG_NO_FILES = "The email carries no files to harvest."
_MSG_TOO_MANY_FILES = "The email's files are more than the harvest accepts ({n} of {cap})."
_MSG_TOO_MANY_BYTES = "The email's files are larger than the harvest accepts ({mb} MB of {cap} MB)."
_MSG_MESSAGE_GONE = "The message is no longer in any watched mailbox."
_MSG_MAILBOX_DOWN = "The mailbox did not answer; the harvest will be retried."
_MSG_MAILBOX_REFUSED = "The mailbox refused the attachment listing (HTTP {code})."
_MSG_ATTACHMENT_TOO_LARGE = "The attachment is larger than the per-file limit."
_MSG_ATTACHMENT_REFUSED = "The mailbox refused the attachment (HTTP {code})."
_MSG_ZIP_TRUNCATED = "The zip holds more members than the harvest lists; the rest were left."
_MSG_UNKNOWN_LOCATOR = "The harvest does not know how to fetch this file."


# ── The session adapter ──────────────────────────────────────────────────────


def graph_locator(mailbox: str, message_id: str, attachment_id: str) -> str:
    return f"{LOCATOR_GRAPH}{mailbox}|{message_id}|{attachment_id}"


def zip_locator(path: Path, index: int) -> str:
    return f"{LOCATOR_ZIP}{path}|{index}"


def eml_locator(mailbox: str, message_id: str, attachment_id: str, part_index: int) -> str:
    """`eml:{mailbox}|{message_id}|{graph_attachment_id}|{part_index}`: the
    forward's own listing entry (the attached message) plus the part."""
    return f"{LOCATOR_EML}{mailbox}|{message_id}|{attachment_id}|{int(part_index)}"


class EmailFileSession:
    """What `rfp_harvest._harvest_files` needs from a platform session:
    `provider` (the sandbox source kind) and `download(locator, dest,
    max_bytes)` raising the `cloud_folders` Transient / Forbidden pair.
    `graph:` locators stream through Graph, `zip:` and `https://` ones
    through `cloud_folders.download` (a zip member is inflated under the
    same cap). Holds nothing but the job's scratch directory."""

    provider = HARVESTER_EMAIL

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch

    def download(self, locator: str, dest: Path, *, max_bytes: int) -> int:
        if locator.startswith(LOCATOR_GRAPH):
            return self._download_graph(locator, dest, max_bytes)
        if locator.startswith(LOCATOR_EML):
            return self._download_eml_part(locator, dest, max_bytes)
        if locator.startswith(LOCATOR_ZIP) or locator.startswith(LOCATOR_HTTPS):
            return cloud_folders.download(locator, dest, max_bytes=max_bytes)
        raise cloud_folders.CloudForbidden(_MSG_UNKNOWN_LOCATOR)

    @staticmethod
    def _download_eml_part(locator: str, dest: Path, max_bytes: int) -> int:
        """Re-fetch the attached message's MIME (under the inbound cap) and
        write the one part; never more than one part is held past the
        parse."""
        parts = locator[len(LOCATOR_EML):].split("|")
        if len(parts) != 4 or not all(parts) or not parts[3].isdigit():
            raise cloud_folders.CloudForbidden(_MSG_UNKNOWN_LOCATOR)
        mailbox, message_id, attachment_id, index = parts
        try:
            data = rfp_test.fetch_eml_bytes(
                mailbox, message_id, attachment_id,
                max_bytes=int(get_settings().inbound_attachment_max_bytes),
            )
            _, payload = rfp_test.eml_part_payload(data, int(index))
        except ValueError as exc:
            raise cloud_folders.CloudForbidden(_MSG_ATTACHMENT_TOO_LARGE) from exc
        except LookupError as exc:
            raise cloud_folders.CloudForbidden(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429 or code >= 500:
                raise cloud_folders.CloudTransient(f"The mailbox answered HTTP {code}.") from exc
            raise cloud_folders.CloudForbidden(_MSG_ATTACHMENT_REFUSED.format(code=code)) from exc
        except httpx.TransportError as exc:
            raise cloud_folders.CloudTransient(_MSG_MAILBOX_DOWN) from exc
        del data
        if len(payload) > max_bytes:
            raise cloud_folders.CloudForbidden(_MSG_ATTACHMENT_TOO_LARGE)
        dest.write_bytes(payload)
        return len(payload)

    @staticmethod
    def _download_graph(locator: str, dest: Path, max_bytes: int) -> int:
        parts = locator[len(LOCATOR_GRAPH):].split("|")
        if len(parts) != 3 or not all(parts):
            raise cloud_folders.CloudForbidden(_MSG_UNKNOWN_LOCATOR)
        mailbox, message_id, attachment_id = parts
        try:
            return graph_inbox.download_attachment_to_file(
                message_id, attachment_id, mailbox=mailbox, max_bytes=max_bytes, dest=dest
            )
        except graph_inbox.AttachmentTooLarge as exc:
            raise cloud_folders.CloudForbidden(_MSG_ATTACHMENT_TOO_LARGE) from exc
        except graph_inbox.AttachmentNotStored as exc:
            raise cloud_folders.CloudForbidden(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429 or code >= 500:
                raise cloud_folders.CloudTransient(f"The mailbox answered HTTP {code}.") from exc
            raise cloud_folders.CloudForbidden(_MSG_ATTACHMENT_REFUSED.format(code=code)) from exc
        except httpx.TransportError as exc:
            raise cloud_folders.CloudTransient(_MSG_MAILBOX_DOWN) from exc


# ── Attachments (live listing) ───────────────────────────────────────────────


def _attachment_kind(odata_type: Any) -> str:
    if odata_type == "#microsoft.graph.fileAttachment":
        return KIND_FILE
    if odata_type == "#microsoft.graph.referenceAttachment":
        return KIND_REFERENCE
    if odata_type == "#microsoft.graph.itemAttachment":
        return KIND_ITEM
    return KIND_UNKNOWN


def _ordered_sightings(sb, email: dict) -> list[dict]:
    """The email's copies, primary mailbox first (the `_email_html` order)."""
    sightings = (
        sb.table("rfp_email_sightings")
        .select("mailbox, graph_message_id")
        .eq("rfp_email_id", email["id"])
        .order("created_at", desc=False)
        .execute()
    ).data or []
    # Case-insensitive for the same reason as rfp_harvest._email_html: a
    # pre-0134 sighting may not be lowercased.
    primary = (email.get("primary_mailbox") or "").strip().lower()
    return sorted(
        sightings,
        key=lambda s: 0 if (s.get("mailbox") or "").strip().lower() == primary else 1,
    )


def _list_page(path: str, *, first: bool) -> dict:
    """One page of the listing; a `@odata.nextLink` already carries the
    query string, so only the first request selects."""
    params = {"$select": _ATTACHMENT_SELECT} if first else None
    return graph_request("GET", path, params=params).json()


def _eml_parts_listing(email: dict, mailbox: str, message_id: str) -> list[dict] | None:
    """A test row whose forward came as an attached message (docs/RFP_TESTING.md
    5.2): the parts recorded at fetch, as the listing, each `id` an `eml:`
    locator so the download loop re-fetches the MIME. None otherwise."""
    meta = email.get("forward_meta") if isinstance(email.get("forward_meta"), dict) else None
    if not meta or meta.get("unwrap") != rfp_test.UNWRAP_EML or not meta.get("eml_attachment_id"):
        return None
    return [
        {
            "id": eml_locator(mailbox, message_id, str(meta["eml_attachment_id"]), int(p.get("index", i))),
            "name": p.get("name"),
            "contentType": p.get("content_type"),
            "size": p.get("size"),
            "kind": KIND_FILE,
            "inline": False,
        }
        for i, p in enumerate(meta.get("eml_parts") or [])
        if isinstance(p, dict)
    ]


def _list_attachments(sb, email: dict) -> tuple[list[dict], str, str]:
    """The live listing in the stored meta shape plus `id` and `inline`,
    with the (mailbox, message id) it came from. A copy that 404s falls
    back to the next sighting; every copy gone is permanent; the mailbox
    not answering is transient. An unwrapped `.eml` test row lists the
    attached message's parts instead of the forward's own attachments."""
    for sighting in _ordered_sightings(sb, email):
        mailbox, message_id = sighting["mailbox"], sighting["graph_message_id"]
        parts = _eml_parts_listing(email, mailbox, message_id)
        if parts is not None:
            return parts, mailbox, message_id
        path: str | None = f"/users/{mailbox}/messages/{message_id}/attachments"
        listed: list[dict] = []
        try:
            for page_index in range(_LISTING_PAGES_MAX):
                if not path:
                    break
                page = _list_page(path, first=page_index == 0)
                listed.extend(a for a in page.get("value", []) if isinstance(a, dict))
                next_link = page.get("@odata.nextLink")
                path = str(next_link).removeprefix(_GRAPH_V1) if next_link else None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                continue
            if code == 429 or code >= 500:
                raise base.RfpHarvestTransient(_MSG_MAILBOX_DOWN) from exc
            raise base.RfpHarvestPermanent(_MSG_MAILBOX_REFUSED.format(code=code)) from exc
        except httpx.TransportError as exc:
            raise base.RfpHarvestTransient(_MSG_MAILBOX_DOWN) from exc
        meta = [
            {
                "id": att.get("id"),
                "name": att.get("name"),
                "contentType": att.get("contentType"),
                "size": att.get("size"),
                "kind": _attachment_kind(att.get("@odata.type")),
                "inline": bool(att.get("isInline")),
            }
            for att in listed
        ]
        return meta, mailbox, message_id
    raise base.RfpHarvestPermanent(_MSG_MESSAGE_GONE)


def _reference_links(mailbox: str, message_id: str) -> list:
    """Outlook cloud "attachments" as ShareLinks (the attachment's name is
    the label). One that is not a recognised share host is dropped."""
    try:
        refs = graph_inbox.list_reference_links(message_id, mailbox=mailbox)
    except Exception:  # noqa: BLE001 - the body's own links still count
        logger.warning("rfp email harvest: reference attachment listing failed", exc_info=True)
        return []
    links = []
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("sourceUrl"):
            continue
        link = cloud_folders.link_from_url(str(ref["sourceUrl"]), str(ref.get("name") or ""))
        if link is not None:
            links.append(link)
    return links


# ── Zips ─────────────────────────────────────────────────────────────────────


def _fetch_to(session: EmailFileSession, locator: str, dest: Path, *, max_bytes: int) -> None:
    """Up to _DOWNLOAD_ATTEMPTS paced tries on transient trouble, the file
    kept on disk (unlike `_download_one`, which reads it back and removes
    it): the zip stays in scratch so its members can be extracted later."""
    last: Exception | None = None
    for attempt in range(base._DOWNLOAD_ATTEMPTS):
        base._unlink_quietly(dest)
        try:
            session.download(locator, dest, max_bytes=max_bytes)
            return
        except cloud_folders.CloudTransient as exc:
            last = exc
            if attempt + 1 < base._DOWNLOAD_ATTEMPTS:
                time.sleep(2.0 * (attempt + 1))
    base._unlink_quietly(dest)
    raise last or cloud_folders.CloudTransient("The file host did not answer.")


def _inspect_zip(path: Path, settings: Settings):
    return rfp_zip.inspect(
        path,
        max_members=settings.rfp_harvest_file_cap,
        max_total_bytes=settings.rfp_harvest_max_total_bytes,
        per_member_max_bytes=settings.rfp_ingest_max_file_bytes,
        is_image_name=is_image_name,
    )


class _Collector:
    """The growing harvest: aligned entries and locators, the
    `data.attachments` block, and a counter for scratch names."""

    def __init__(self, settings: Settings, scratch: Path) -> None:
        self.settings = settings
        self.scratch = scratch
        self.entries: list[dict] = []
        self.urls: list[str | None] = []
        self.images: list[dict] = []
        self.skipped: list[dict] = []
        self._zips = 0

    def add(self, entry: dict, locator: str | None) -> dict:
        self.entries.append(entry)
        self.urls.append(locator)
        return entry

    def record_member(self, member: Any, prefix: str) -> bool:
        """A skipped zip member goes to `images` (by the policy, `inline`
        False) or `skipped` (the reason rfp_zip gave: nested_zip, encrypted,
        too_large, empty, unsafe_name). True when the member was skipped."""
        reason = member.skipped_reason
        if not reason:
            return False
        name = f"{prefix}/{member.path}" if prefix else str(member.path)
        if reason == SKIP_IMAGE:
            self.images.append(
                image_record(name.rsplit("/", 1)[-1], member.size, False, self.settings)
            )
        else:
            self.skipped.append(
                {"name": name[:base._PATH_MAX_CHARS], "size": int(member.size or 0),
                 "reason": str(reason)}
            )
        return True

    def expand_zip(
        self, session: EmailFileSession, zip_entry: dict, locator: str, *,
        provider: str | None, link_key: str | None,
    ) -> None:
        """Download the zip now and enter its members after it: the zip
        entry ends `expanded` (or `too_large` / `download_failed` /
        `rejected` with the sentence, and then no members). Skipped members
        are recorded like skipped attachments; images like images."""
        self._zips += 1
        dest = self.scratch / f"zip-{self._zips:04d}.zip"
        try:
            _fetch_to(session, locator, dest, max_bytes=self.settings.rfp_ingest_max_file_bytes)
        except cloud_folders.CloudForbidden as exc:
            zip_entry["status"] = (
                base.FILE_TOO_LARGE if "larger" in str(exc) else base.FILE_DOWNLOAD_FAILED
            )
            zip_entry["error"] = base._error(exc)
            return
        except cloud_folders.CloudTransient as exc:
            zip_entry["status"] = base.FILE_DOWNLOAD_FAILED
            zip_entry["error"] = base._error(exc)
            return
        listing = _inspect_zip(dest, self.settings)
        if listing.error:
            # Not a zip, a bomb, or more declared bytes than the harvest
            # accepts: the sentence is the entry's record, nothing follows.
            zip_entry["status"] = (
                base.FILE_TOO_LARGE if listing.error_kind == "too_large" else base.FILE_REJECTED
            )
            zip_entry["error"] = base._cap(listing.error, base._ERROR_MAX_CHARS)
            base._unlink_quietly(dest)
            return
        zip_entry["status"] = FILE_EXPANDED
        zip_entry["error"] = _MSG_ZIP_TRUNCATED if listing.truncated else None
        zip_path = zip_entry["file_path"]
        for member in listing.members:
            if self.record_member(member, zip_path):
                continue
            self.add(
                zip_member_entry(zip_path, member.path, member.size, provider=provider, link_key=link_key),
                zip_locator(dest, int(member.index)),
            )

    def add_folder_zip_skips(self, zip_path: str) -> None:
        """A Dropbox folder resolves to the zip Dropbox serves for it, listed
        by `cloud_folders` with no image policy (its images are in the
        listing and recorded by the link loop); the skipped members are
        read off a second look at the archive with ours."""
        for member in _inspect_zip(Path(zip_path), self.settings).members:
            if member.skipped_reason and member.skipped_reason != SKIP_IMAGE:
                self.record_member(member, "")


# ── Links ────────────────────────────────────────────────────────────────────


def _link_row(link: Any) -> dict:
    return {
        "key": link.key,
        "provider": link.provider,
        "kind": link.kind,
        "url": link.url,
        "label": base._cap(link.label, base._NAME_MAX_CHARS),
        "status": None,
        "file_count": 0,
        "bytes": 0,
        "reused": 0,
        "error": None,
    }


def _merge_links(body_links: list, reference_links: list) -> list:
    """Body order first, reference attachments after, deduped by key."""
    seen: set[str] = set()
    out: list = []
    for link in list(body_links) + list(reference_links):
        if link.key in seen:
            continue
        seen.add(link.key)
        out.append(link)
    return out


def _resolve_links(
    session: EmailFileSession, settings: Settings, links: list, col: _Collector
) -> list[dict]:
    """Every link becomes a `data.links[]` row whatever happened; the
    listed files become entries (`origin = link`, or `zip` for the members
    of a folder zip). A zip a folder lists is opened like a zip attachment.
    Only supported links spend the resolve cap. A refusal the resolver
    raises for one link (a host off the allowlist, a redirect it will not
    follow) is that link's record, not the harvest's: only transient and
    unavailable trouble leaves this loop."""
    rows: list[dict] = []
    resolved = 0
    for link in links:
        row = _link_row(link)
        rows.append(row)
        if link.supported:
            if resolved >= settings.rfp_harvest_link_max_count:
                row["status"] = LINK_SKIPPED_CAP
                continue
            resolved += 1
            base._renew()
        # An unsupported link is answered by `resolve` without the network
        # (the sentence names the provider); it spends none of the cap.
        try:
            listing = cloud_folders.resolve(link, col.scratch, settings)
        except (cloud_folders.CloudTransient, cloud_folders.CloudUnavailable):
            raise
        except cloud_folders.CloudError as exc:
            row["status"] = LINK_UNREACHABLE
            row["error"] = base._error(exc)
            continue
        row["status"] = LINK_UNSUPPORTED if not link.supported else listing.status
        row["error"] = base._cap(listing.error, base._ERROR_MAX_CHARS)
        for remote in listing.files:
            locator = str(remote.locator)
            origin = ORIGIN_ZIP if locator.startswith(LOCATOR_ZIP) else ORIGIN_LINK
            name = str(remote.path or "").rsplit("/", 1)[-1]
            if is_image_name(name):
                # The listing carries every downloadable file; the image
                # policy is ours (a folder of job-walk photos is recorded,
                # never downloaded).
                col.images.append(image_record(name, remote.size, False, settings))
                continue
            entry = col.add(
                link_file_entry(remote.path, remote.size, provider=link.provider, link_key=link.key,
                                origin=origin),
                locator,
            )
            row["file_count"] += 1
            row["bytes"] += entry["size"]
            if origin == ORIGIN_LINK and is_zip_name(entry["file_path"]):
                col.expand_zip(session, entry, locator, provider=link.provider, link_key=link.key)
        if listing.zip_path:
            col.add_folder_zip_skips(listing.zip_path)
    return rows


# ── Reuse ────────────────────────────────────────────────────────────────────


def prior_link_files(
    sb, link_key: str, settings: Settings, *, exclude_harvest_id: str | None
) -> dict[tuple[str, int], tuple[str, str]]:
    """`(file_path, size) -> (harvest_id, sandbox_file_id)` for every file an
    earlier complete harvest inside the reuse window accepted from the same
    link key (jsonb containment on `data.links[].key`), newest harvest
    first so the freshest copy wins."""
    cutoff = base._iso(base._now() - timedelta(days=settings.rfp_harvest_reuse_days))
    query = (
        sb.table(base.TABLE)
        .select("id, files, finished_at")
        .contains("data", {"links": [{"key": link_key}]})
        .eq("status", base.STATUS_COMPLETE)
        .gte("finished_at", cutoff)
    )
    if exclude_harvest_id:
        query = query.neq("id", exclude_harvest_id)
    rows = query.order("finished_at", desc=True).limit(_REUSE_ROWS_MAX).execute().data or []
    out: dict[tuple[str, int], tuple[str, str]] = {}
    for row in rows:
        for entry in row.get("files") or []:
            if not isinstance(entry, dict) or entry.get("status") != base.FILE_ACCEPTED:
                continue
            if entry.get("link_key") != link_key or not entry.get("sandbox_file_id"):
                continue
            key = (str(entry.get("file_path")), int(entry.get("size") or 0))
            out.setdefault(key, (row["id"], entry["sandbox_file_id"]))
    return out


def _apply_reuse(sb, settings: Settings, harvest: dict, link_rows: list[dict], entries: list[dict]) -> None:
    for row in link_rows:
        if row["file_count"] == 0:
            continue
        prior = prior_link_files(sb, row["key"], settings, exclude_harvest_id=harvest.get("id"))
        if not prior:
            continue
        for entry in entries:
            if entry.get("link_key") != row["key"] or entry.get("status") is not None:
                continue
            hit = prior.get((entry["file_path"], int(entry.get("size") or 0)))
            if hit is None:
                continue
            entry["status"] = FILE_REUSED
            entry["sandbox_file_id"] = hit[1]
            entry["reused_harvest_id"] = hit[0]
            entry["error"] = None
            row["reused"] += 1


# ── The body ─────────────────────────────────────────────────────────────────


def _documents(entries: list[dict], *, count: int, size: int) -> dict:
    """`data.documents`: what this harvest holds for the creation slice,
    the given (declared, then actual) downloads plus the reused entries."""
    reused = [e for e in entries if e.get("status") == FILE_REUSED]
    return {
        "count": count + len(reused),
        "bytes": size + sum(int(e.get("size") or 0) for e in reused),
        "kinds": None,
        "disciplines": None,
    }


def harvest(
    sb, settings: Settings, email: dict, ref: EmailRef, harvest: dict, token: str, *,
    force: bool = False,
) -> None:
    """The email body of the job (doc 2.5 steps 1 to 5), run under the claim
    by `rfp_harvest._run_claimed`: attachments (live listing, zips opened),
    links (body and HTML, reference attachments merged, resolved under the
    cap), reuse per link (skipped under `force`), the facts write, the caps
    over what still needs downloading, the download loop, complete."""
    scratch = base._scratch_dir(settings)
    try:
        session = EmailFileSession(scratch)
        col = _Collector(settings, scratch)

        # 1. Attachments.
        base._renew()
        meta, mailbox, message_id = _list_attachments(sb, email)
        plan = classify_attachments(meta, settings)
        col.images.extend(plan.images)
        col.skipped.extend(plan.skipped)
        for att in plan.files:
            att_id = str(att.get("id") or "")
            locator = att_id if att_id.startswith(LOCATOR_EML) else graph_locator(mailbox, message_id, att_id)
            entry = col.add(attachment_entry(att["name"], att["size"]), locator)
            if is_zip_name(att["name"]):
                base._renew()
                col.expand_zip(session, entry, locator, provider=PROVIDER_GRAPH, link_key=None)
        attachments = {
            "count": len(meta),
            "files": len(plan.files),
            "images": col.images,
            "skipped": col.skipped,
        }

        # 2. Links: the text body, the HTML body (never stored) and the
        # reference attachments, body order, deduped by key.
        body_links = cloud_folders.find_share_links(
            email.get("body_text"), base._email_html(sb, email)
        )
        reference_links = _reference_links(mailbox, message_id) if plan.references else []
        links = _merge_links(body_links, reference_links)
        link_rows = _resolve_links(session, settings, links, col)
        entries, urls = col.entries, col.urls
        for link_row in link_rows if rfp_test.session_for_email(email) else []:
            rfp_test.record(
                sb, session_id=email["test_session_id"], source=rfp_test.SOURCE_HARVEST, kind="link",
                level=rfp_test.LEVEL_INFO if link_row.get("file_count") else rfp_test.LEVEL_WARN,
                title=f"Link {link_row.get('status')}: {link_row.get('provider')} "
                      f"({link_row.get('file_count')} file(s))",
                rfp_email_id=email["id"], harvest_id=harvest.get("id"),
                detail={
                    "host": str(link_row.get("url") or "").split("/")[2] if "//" in str(link_row.get("url") or "") else None,
                    "provider": link_row.get("provider"), "kind": link_row.get("kind"),
                    "label": link_row.get("label"), "outcome": link_row.get("status"),
                    "error": link_row.get("error"), "file_count": link_row.get("file_count"),
                    "bytes": link_row.get("bytes"),
                },
            )

        # 3. Reuse, then the facts write.
        if not force:
            _apply_reuse(sb, settings, harvest, link_rows, entries)
        pending = [e for e in entries if e.get("status") is None]
        data = {
            "platform": HARVESTER_EMAIL,
            "attachments": attachments,
            "links": link_rows,
            "documents": _documents(entries, count=len(pending), size=sum(e["size"] for e in pending)),
        }
        facts = {
            "external_url": None,
            "data": data,
            "description_text": None,
            "instructions_text": None,
            "files": entries,
            "file_count": len(entries),
            "facts_at": base._iso(base._now()),
        }
        prior_files = list(harvest.get("files") or [])
        base._update_claimed(sb, harvest["id"], token, facts)
        harvest.update(facts)
        base._check_caps(
            settings, pending, files_message=_MSG_TOO_MANY_FILES, bytes_message=_MSG_TOO_MANY_BYTES
        )

        # 4. The download loop (reused and expanded entries are skipped by
        # it); nothing to download means no sandbox run at all.
        run_id: str | None = None
        accepted = 0
        total = 0
        if pending:
            run_id, accepted, total = base._harvest_files(
                sb, session, settings, harvest, token, email["id"], entries, urls,
                prior_files=prior_files,
            )

        # 5. The documents summary from what actually landed, then complete.
        data["documents"] = _documents(entries, count=accepted, size=total)
        base._update_claimed(sb, harvest["id"], token, {"data": data})
        base._complete_harvest(
            sb, harvest, token, entries, run_id, accepted, total, no_files_message=_MSG_NO_FILES
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        cloud_folders.forget_jars()
