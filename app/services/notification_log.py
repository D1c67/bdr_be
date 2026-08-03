"""The per-project notification log: every notice this project sent, as EVENTS.

Two independent streams record what went out for a project, and both are keyed
on the same `projects.id` — so a project that wins its bid and moves into
Project Management carries its whole history with it, no copying involved:

  * `notifications` — in-app bell rows. Dismissed rows are a soft-delete
    (0035), so the history is complete even though the bell has moved on.
  * `email_log`     — every outbound email. Each project-scoped sender sets
    `project_id`, so filtering on it yields the full outbound record.

The log's unit is the EVENT, not the row. Two things are collapsed:

  1. A bell row and the branded email that mirrored it are ONE notice that
     reached one person two ways — joined on `notifications.email_log_id`
     (0091), with a bounded time-window fallback for rows created before that
     migration existed.
  2. A fan-out send is ONE act with many recipients. A role broadcast inserts
     one bell row per user in a single statement, so they share an exact
     `created_at`; an RFQ/package/submittal send loops one email per contact
     (per-recipient personalisation, and `graph_email.send_mail` puts every
     address in `toRecipients` — there is no BCC path), so those are grouped by
     the first-class send record that already links them (`rfq_sends`,
     `file_send_recipients`, `submittal_request_sends`, `proposal_sends`,
     `submittal_packages`) rather than by guessing from subjects.

Everything is read-only over tables that already exist; the log never writes.

Recipients are resolved to people — internal staff and estimators via
`profiles`, vendors via `vendor_contacts`, GCs via `gc_contacts` — so the UI can
say "A. Chen (Estimating Admin)" instead of an address. An address that matches
nothing is shown as-is rather than dropped: an unrecognised recipient is
precisely the kind of thing someone reads this log to find.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services.notification_email import heading_for

logger = logging.getLogger(__name__)

# Bounds on the two base reads. A busy project runs to dozens of entries; these
# exist so one pathological project can never turn a modal open into an
# unbounded scan. Hitting either sets `truncated` on the response.
MAX_NOTIFICATIONS = 500
MAX_EMAILS = 500

# PostgREST puts `in.()` lists in the URL — chunk them so a long id list can
# never blow the server's URL limit (which would surface as a raw 500).
_IN_CHUNK = 80

# A fan-out send loops with a throttle sleep between messages, so its rows are
# seconds apart. Consecutive rows sharing a group key chain into one entry while
# they stay inside this window; a genuine re-send hours or days later starts a
# new entry.
_GROUP_WINDOW = timedelta(minutes=30)

# Legacy mirror matching, for bell rows created before 0091 gave them an
# explicit `email_log_id`. Both senders that use this subject prefix
# (notification_email, revision_email) send exactly one email per bell row,
# within seconds of it.
_MIRROR_SUBJECT_PREFIX = "G3 BDR · "
_MIRROR_WINDOW = timedelta(minutes=5)

_NOTIFICATION_SELECT = (
    "id, user_id, type, message, created_at, read_at, dismissed_at, rfq_id, email_log_id"
)
_EMAIL_SELECT = "id, to_addrs, subject, status, error, created_at, rfq_id, sent_by"


# ── small helpers ───────────────────────────────────────────────────────────


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _ts(value) -> datetime | None:
    """Parse a PostgREST timestamptz, normalised to aware UTC. Returns None for
    anything unparseable so a single odd row degrades to "ungroupable" instead
    of failing the whole log — and so two datetimes are never subtracted with
    mismatched awareness."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _addresses(to_addrs: str | None) -> list[str]:
    """Split an `email_log.to_addrs` cell back into addresses (senders join with
    ', '). Order and duplicates-removal are stable so the UI list is stable."""
    seen: dict[str, None] = {}
    for part in (to_addrs or "").split(","):
        addr = part.strip()
        if addr:
            seen.setdefault(addr, None)
    return list(seen)


def _chunks(values: list, size: int = _IN_CHUNK):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _fetch_in(sb, table: str, select: str, column: str, values: list[str]) -> list[dict]:
    """`select … where column in (values)`, chunked. Empty `values` skips the
    round-trip entirely."""
    rows: list[dict] = []
    for chunk in _chunks(sorted({v for v in values if v})):
        rows += (sb.table(table).select(select).in_(column, chunk).execute()).data or []
    return rows


# ── recipient identity ──────────────────────────────────────────────────────


class _People:
    """Address/id → person lookups, resolved once per log build."""

    def __init__(self, by_id: dict[str, dict], by_email: dict[str, dict]):
        self.by_id = by_id
        self.by_email = by_email

    def for_user(self, user_id: str) -> dict:
        p = self.by_id.get(user_id) or {}
        return {
            "name": p.get("full_name"),
            "email": p.get("email"),
            "role": p.get("role"),
            "audience": "estimator" if p.get("role") == Role.ESTIMATOR.value else "internal",
        }

    def for_address(self, address: str) -> dict:
        p = self.by_email.get(address.lower())
        if not p:
            # Not a known profile or contact — a one-off address someone typed,
            # or a contact deleted since. Show it rather than hide it.
            return {"name": None, "email": address, "role": None, "audience": "external"}
        entry = dict(p)
        entry["email"] = address
        return entry


def _resolve_people(sb, user_ids: list[str], addresses: list[str]) -> _People:
    by_id: dict[str, dict] = {}
    by_email: dict[str, dict] = {}

    profiles = _fetch_in(sb, "profiles", "id, full_name, email, role", "id", user_ids)
    if addresses:
        profiles += _fetch_in(
            sb, "profiles", "id, full_name, email, role", "email", addresses
        )
    for p in profiles:
        by_id[p["id"]] = p
        if p.get("email"):
            by_email[p["email"].lower()] = {
                "name": p.get("full_name"),
                "role": p.get("role"),
                "audience": (
                    "estimator" if p.get("role") == Role.ESTIMATOR.value else "internal"
                ),
            }

    # Vendor and GC contacts: only the addresses this project actually mailed.
    vendor_contacts = _fetch_in(
        sb, "vendor_contacts", "name, email, vendor_id", "email", addresses
    )
    gc_contacts = _fetch_in(sb, "gc_contacts", "name, email, gc_id", "email", addresses)

    vendors = _fetch_in(
        sb, "vendors", "id, name", "id", [c["vendor_id"] for c in vendor_contacts]
    )
    gcs = _fetch_in(
        sb, "general_contractors", "id, name", "id", [c["gc_id"] for c in gc_contacts]
    )
    vendor_name = {v["id"]: v.get("name") for v in vendors}
    gc_name = {g["id"]: g.get("name") for g in gcs}

    # Profiles win on a collision: a staff address is who the person IS to us.
    contacts = [(c, "vendor", vendor_name.get(c.get("vendor_id"))) for c in vendor_contacts]
    contacts += [(c, "gc", gc_name.get(c.get("gc_id"))) for c in gc_contacts]
    for contact, audience, org_name in contacts:
        key = (contact.get("email") or "").lower()
        if key and key not in by_email:
            by_email[key] = {
                "name": contact.get("name"),
                "role": None,
                "audience": audience,
                "org": org_name,
            }

    return _People(by_id, by_email)


# ── email classification (what kind of send an email_log row belongs to) ────

_PACKAGE_TITLES = {
    "initial": "File package sent to the estimator",
    "revision": "Revised files sent to the estimator",
    "reassign": "Files sent to the reassigned estimator",
}


def _classify_emails(sb, project_id: str) -> dict[str, dict]:
    """email_log_id → {type, title, group_key, category} for every email this
    project sent through a first-class send record.

    Every lookup is scoped by project first, so each query is bounded by the
    project's own history rather than by the id list we happen to be holding.
    Emails with no match (RFI sends, estimator lifecycle notices, anything a
    future sender adds) fall through to a subject-titled entry — unclassified,
    never dropped.
    """
    classes: dict[str, dict] = {}
    category_ids: set[str] = set()

    def _record(email_log_id, **fields):
        if email_log_id:
            classes[email_log_id] = fields

    # RFQs → vendors, one email per vendor contact.
    rfqs = (
        sb.table("rfqs")
        .select("id, material_category_id")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    rfq_category = {r["id"]: r.get("material_category_id") for r in rfqs}
    for send in _fetch_in(
        sb, "rfq_sends", "rfq_id, email_log_id", "rfq_id", list(rfq_category)
    ):
        cat = rfq_category.get(send["rfq_id"])
        category_ids.add(cat)
        _record(
            send.get("email_log_id"),
            type="rfq_send",
            title="RFQ sent to vendors",
            group_key=f"rfq:{send['rfq_id']}",
            category_id=cat,
        )

    # Proposals → GCs, one email per GC.
    for send in (
        sb.table("proposal_sends")
        .select("email_log_id, gc_name")
        .eq("project_id", project_id)
        .execute()
    ).data or []:
        _record(
            send.get("email_log_id"),
            type="proposal_send",
            title="Proposal sent to the GC",
            group_key="proposal",
            category_id=None,
        )

    # File packages → estimators, one email per recipient, grouped by batch.
    batches = (
        sb.table("file_send_batches")
        .select("id, kind")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    batch_kind = {b["id"]: b.get("kind") for b in batches}
    for rec in _fetch_in(
        sb, "file_send_recipients", "batch_id, email_log_id", "batch_id", list(batch_kind)
    ):
        _record(
            rec.get("email_log_id"),
            type="file_package",
            title=_PACKAGE_TITLES.get(
                batch_kind.get(rec["batch_id"]), "Files sent to the estimator"
            ),
            group_key=f"batch:{rec['batch_id']}",
            category_id=None,
        )

    # Submittal requests → vendors, one email per contact, grouped per category.
    requests = (
        sb.table("submittal_requests")
        .select("id")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    for send in _fetch_in(
        sb,
        "submittal_request_sends",
        "request_id, material_category_id, email_log_id",
        "request_id",
        [r["id"] for r in requests],
    ):
        cat = send.get("material_category_id")
        category_ids.add(cat)
        _record(
            send.get("email_log_id"),
            type="submittal_request",
            title="Submittal request sent to vendors",
            group_key=f"submittal_request:{send['request_id']}:{cat}",
            category_id=cat,
        )

    # Submittal approval packages → the GC, one email (To + CC) per package.
    for pkg in (
        sb.table("submittal_packages")
        .select("id, number, email_log_id")
        .eq("project_id", project_id)
        .execute()
    ).data or []:
        _record(
            pkg.get("email_log_id"),
            type="submittal_package",
            title=f"Submittal approval package #{pkg.get('number')} sent to the GC",
            group_key=f"submittal_package:{pkg['id']}",
            category_id=None,
        )

    # Resolve category names in one go and fold them into the classifications.
    names = {
        c["id"]: c.get("name")
        for c in _fetch_in(
            sb, "material_categories", "id, name", "id", [c for c in category_ids if c]
        )
    }
    for cls in classes.values():
        cls["category"] = names.get(cls.pop("category_id", None))
    return classes


# ── assembly (pure — no I/O, so it is directly testable) ────────────────────


def _notification_groups(notifs: list[dict], people: _People) -> list[dict]:
    """One entry per bell EVENT. A role broadcast inserts every row in a single
    statement, so its rows share an exact `created_at` — that, with the type and
    message, is the group identity."""
    groups: dict[tuple, dict] = {}
    for row in sorted(notifs, key=lambda r: str(r.get("created_at") or "")):
        key = (row.get("type"), row.get("message"), str(row.get("created_at")))
        group = groups.get(key)
        if group is None:
            type_ = row.get("type") or ""
            group = groups[key] = {
                "source": "notification",
                "type": type_,
                "title": heading_for(type_),
                "message": row.get("message"),
                "subject": None,
                "category": None,
                "at": row.get("created_at"),
                "_at": _ts(row.get("created_at")),
                "recipients": [],
            }
        recipient = people.for_user(row.get("user_id"))
        recipient.update(
            {
                "read_at": row.get("read_at"),
                "dismissed_at": row.get("dismissed_at"),
                "email_status": None,
                "email_error": None,
            }
        )
        group["recipients"].append(recipient)
        # Explicit link (0091) — the mirror email attaches straight to this row.
        if row.get("email_log_id"):
            group.setdefault("_by_email_log", {})[row["email_log_id"]] = recipient
    return list(groups.values())


def _attach_delivery(recipient: dict, email: dict) -> None:
    recipient["email_status"] = email.get("status")
    recipient["email_error"] = email.get("error")
    recipient["emailed_at"] = email.get("created_at")


def _absorb_mirrors(
    groups: list[dict], emails: list[dict], people: _People
) -> list[dict]:
    """Fold each mirror email into the bell entry it delivered; return the
    emails that remain (i.e. are events in their own right).

    Two passes. The explicit `notifications.email_log_id` link (0091) is exact
    and always wins. The legacy pass exists only for bell rows written before
    that column, and is deliberately narrow: the subject prefix identifies the
    two senders that mail one message per bell row, every recipient must resolve
    to a profile, and the match must land within minutes on a recipient slot
    that no other email has claimed.
    """
    linked: dict[str, dict] = {}
    for group in groups:
        linked.update(group.get("_by_email_log") or {})

    # Recipient slots the legacy pass may still claim, indexed by user.
    open_slots: dict[str, list[tuple[datetime | None, dict]]] = defaultdict(list)
    for group in groups:
        for recipient in group["recipients"]:
            if recipient.get("email_status") is None:
                key = (recipient.get("email") or "").lower()
                open_slots[key].append((group["_at"], recipient))

    remaining: list[dict] = []
    for email in sorted(emails, key=lambda e: str(e.get("created_at") or "")):
        recipient = linked.get(email["id"])
        if recipient is not None:
            _attach_delivery(recipient, email)
            continue
        recipient = _legacy_mirror_slot(email, open_slots, people)
        if recipient is not None:
            _attach_delivery(recipient, email)
            continue
        remaining.append(email)
    return remaining


def _legacy_mirror_slot(
    email: dict,
    open_slots: dict[str, list[tuple[datetime | None, dict]]],
    people: _People,
) -> dict | None:
    if not str(email.get("subject") or "").startswith(_MIRROR_SUBJECT_PREFIX):
        return None
    addresses = _addresses(email.get("to_addrs"))
    # A mirror goes to exactly one person; anything else is a different kind of
    # mail that happens to share the prefix.
    if len(addresses) != 1:
        return None
    address = addresses[0].lower()
    if people.by_email.get(address, {}).get("audience") not in ("internal", "estimator"):
        return None
    sent_at = _ts(email.get("created_at"))
    if sent_at is None:
        return None

    best: tuple[timedelta, dict] | None = None
    for group_at, recipient in open_slots.get(address, []):
        if group_at is None or recipient.get("email_status") is not None:
            continue
        gap = abs(sent_at - group_at)
        if gap <= _MIRROR_WINDOW and (best is None or gap < best[0]):
            best = (gap, recipient)
    return best[1] if best else None


def _email_groups(
    emails: list[dict], classes: dict[str, dict], people: _People
) -> list[dict]:
    """One entry per outbound send. Rows sharing a group key chain together
    while consecutive members stay within `_GROUP_WINDOW`, so a later re-send of
    the same RFQ is its own entry rather than being folded into the first."""
    groups: list[dict] = []
    open_groups: dict[str, dict] = {}

    for email in sorted(emails, key=lambda e: str(e.get("created_at") or "")):
        cls = classes.get(email["id"]) or {}
        subject = email.get("subject")
        key = cls.get("group_key") or f"subject:{subject}"
        at = _ts(email.get("created_at"))

        group = open_groups.get(key)
        if group is not None and not (
            group["_last"] is not None and at is not None and at - group["_last"] <= _GROUP_WINDOW
        ):
            group = None  # too far from the previous member — start a new event
        if group is None:
            group = {
                "source": "email",
                "type": cls.get("type") or "email",
                # An unclassified send is titled by what the recipient actually
                # saw in their inbox, which beats inventing a label for it.
                "title": cls.get("title") or subject or "Email sent",
                "message": None,
                "subject": subject,
                "category": cls.get("category"),
                "at": email.get("created_at"),
                "_at": at,
                "_last": at,
                "recipients": [],
                "_seen": set(),
                "sent_by": email.get("sent_by"),
            }
            groups.append(group)
            open_groups[key] = group
        group["_last"] = at or group["_last"]

        for address in _addresses(email.get("to_addrs")):
            if address in group["_seen"]:
                continue  # same person re-listed (To + CC on one message)
            group["_seen"].add(address)
            recipient = people.for_address(address)
            recipient.update({"read_at": None, "dismissed_at": None})
            _attach_delivery(recipient, email)
            group["recipients"].append(recipient)
    return groups


def _finalize(group: dict, index: int, people: _People) -> dict:
    recipients = group["recipients"]
    channels = ["in_app"] if group["source"] == "notification" else []
    if any(r.get("email_status") for r in recipients):
        channels.append("email")

    sender = people.by_id.get(group.get("sent_by")) if group.get("sent_by") else None
    return {
        # Positional, because an event has no row of its own to borrow an id
        # from — it is several rows. Stable for one response, which is all the
        # React key and the hover-detail lookup need.
        "id": f"{group['source']}-{index}",
        "at": group["at"],
        "source": group["source"],
        "channels": channels,
        "type": group["type"],
        "title": group["title"],
        "message": group.get("message"),
        "subject": group.get("subject"),
        "category": group.get("category"),
        "sent_by": (
            {"name": sender.get("full_name"), "role": sender.get("role")} if sender else None
        ),
        "recipients": recipients,
        "counts": {
            "recipients": len(recipients),
            "emailed": sum(1 for r in recipients if r.get("email_status") == "sent"),
            "failed": sum(1 for r in recipients if r.get("email_status") == "failed"),
            "read": sum(1 for r in recipients if r.get("read_at")),
        },
    }


def assemble(
    notifs: list[dict], emails: list[dict], classes: dict[str, dict], people: _People
) -> list[dict]:
    """Bell rows + emails → one list of events, newest first."""
    groups = _notification_groups(notifs, people)
    leftover = _absorb_mirrors(groups, emails, people)
    groups += _email_groups(leftover, classes, people)
    # Sort on the parsed timestamp, not the raw string: an unparseable one sorts
    # last instead of poisoning the comparison.
    groups.sort(key=lambda g: g.get("_at") or _EPOCH, reverse=True)
    return [_finalize(g, i, people) for i, g in enumerate(groups)]


# ── entry point ─────────────────────────────────────────────────────────────


def build(project_id: str) -> dict:
    """The whole log for one project, newest event first."""
    sb = get_supabase()

    notifs = (
        sb.table("notifications")
        .select(_NOTIFICATION_SELECT)
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(MAX_NOTIFICATIONS)
        .execute()
    ).data or []
    emails = (
        sb.table("email_log")
        .select(_EMAIL_SELECT)
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(MAX_EMAILS)
        .execute()
    ).data or []

    addresses = sorted(
        {a for e in emails for a in _addresses(e.get("to_addrs"))}
    )
    user_ids = [n["user_id"] for n in notifs if n.get("user_id")]
    user_ids += [e["sent_by"] for e in emails if e.get("sent_by")]
    people = _resolve_people(sb, user_ids, addresses)
    classes = _classify_emails(sb, project_id)

    return {
        "entries": assemble(notifs, emails, classes, people),
        # The reads are capped; say so rather than let the oldest entries
        # silently vanish and read as "nothing happened before this".
        "truncated": len(notifs) >= MAX_NOTIFICATIONS or len(emails) >= MAX_EMAILS,
    }
