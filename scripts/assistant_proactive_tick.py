"""Phase 7 dev tick: one proactive router tick at a chosen time, dev only
(docs/ASSISTANT_PHASE7_PLAN.md 11.5 steps 9, 9b and 10).

    .venv/bin/python scripts/assistant_proactive_tick.py --now <ISO> [--no-tails] [--live-past-slot] [--seed FILE] [--worker ID]

--now      the tick's clock, an ISO 8601 time. A naive value is read as
           America/Los_Angeles; a Z or offset value is used as given.
--no-tails call poll_once(now, worker, tails=False): the two table tails are
           skipped, so assistant_router_cursors is never read or written and a
           time-travel tick cannot move the shared cursors. Without it the tails
           run, and --now must lie in [real now - 24 h, real now]. Either way
           --now must lie in [real now - 30 days, real now + 8 days]: every
           tick runs the retention purge at its clock, so a far date would
           purge every dev user's router rows.
--live-past-slot
           only with live channels on: --now may lie up to 72 hours in the past
           (and no more than 5 minutes ahead) instead of within 5 minutes of the
           real now, so a digest slot that already passed can be ticked for
           real. Every other live guard is unchanged. With the tails on the 24
           hour tail window still applies. The summary says live_past_slot.
--seed     a JSON file of assistant_events rows to insert before the tick
           (source 'emit', an explicit occurred_at, subject_user_id on the
           allowlist). The inserted ids are printed so the caller can clean up.
--worker   the worker id the tick claims under (default: a fresh script id).

The router env comes from the command line only (never .env). A run needs
ASSISTANT_ENABLED=true ASSISTANT_PROACTIVE_ENABLED=true and
ASSISTANT_PROACTIVE_USER_ALLOWLIST=<ids>; ASSISTANT_PROACTIVE_EXTERNAL_ENABLED,
NOTIFICATION_EMAILS_ENABLED and TEAMS_ALERTS_ENABLED decide what leaves the
process (all three off or the external switch on with both global switches off
settles every send channel_off). Real sends need the step 9b env, including
ASSISTANT_PROACTIVE_TEST_EMAIL_TO=baseballtom33@gmail.com.

Refuses (exit 2, nothing written) unless the Supabase URL is the dev project
bpidntbyvoooqvaispup, the allowlist is set, ASSISTANT_PROACTIVE_TEST_EMAIL_TO
is empty or the owner's address, --now is inside the travel window and (with
the tails on) inside the 24 hour window. A run that can send (the external
switch on with NOTIFICATION_EMAILS_ENABLED and MS_CLIENT_ID set, or with
TEAMS_ALERTS_ENABLED on) is refused unless ASSISTANT_PROACTIVE_TEST_EMAIL_TO is
exactly the owner's address, --now is within 5 minutes of the real now (with or
without --no-tails, so the quiet-hours and staleness gates see the real clock),
RFP_TESTING_ENABLED is false and every allowlisted id is one of the two e2e
accounts (step 9b). Prints one JSON object: the poll_once result, the send
step's counts and the seeded event ids. The router calls no model.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEV_REF = "bpidntbyvoooqvaispup"
PROD_REF = "zxqkqotlcgbalwoqeeca"
OWNER_EMAIL = "baseballtom33@gmail.com"
PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
TAIL_WINDOW = timedelta(hours=24)
# Plan 11.5 step 9 travels to the coming Monday, Tuesday and Saturday; the
# purge runs at the tick's clock, so nothing further is allowed.
TRAVEL_BACK = timedelta(days=30)
TRAVEL_AHEAD = timedelta(days=8)
# A run that can send ticks at the real now (step 9b).
LIVE_WINDOW = timedelta(minutes=5)
# --live-past-slot: a live run may tick at a slot that already passed, by at
# most this long (never in the future beyond LIVE_WINDOW).
LIVE_PAST_SLOT = timedelta(hours=72)
# The two e2e accounts, the only allowlist a run that can send accepts (equal
# to assistant_delivery.TEST_TEAMS_ALLOWED; a test pins it).
E2E_EMAILS = frozenset({"bdr.e2e.electrical@g3test.dev", "bdr.e2e.admin@g3test.dev"})
SEED_FIELDS = ("event_type", "dedupe_key", "subject_user_id", "project_id", "payload", "occurred_at")


class Refused(Exception):
    """The script will not run; the message says why and nothing was written."""


def _real_now() -> datetime:
    """The wall clock (tests pin it)."""
    return datetime.now(UTC)


# ── Guards (pure: they read the settings object they are given) ──────────────


def supabase_ref(url: str | None) -> str:
    """The project ref of a hosted Supabase URL (`https://<ref>.supabase.co`),
    else an empty string (a local stack, a custom host, no URL)."""
    host = (urlsplit((url or "").strip()).hostname or "").lower()
    label, _, rest = host.partition(".")
    return label if rest == "supabase.co" else ""


def require_dev_ref(settings: Any) -> None:
    """The exact ref guard: only the dev project passes. The prod ref gets its
    own message; anything else (a local URL, a lookalike host, no URL) is
    refused as not the dev project."""
    url = str(getattr(settings, "supabase_url", "") or "")
    if PROD_REF in url:
        raise Refused("SUPABASE_URL is the production project. This script runs only against the dev project "
                      f"{DEV_REF}.")
    if supabase_ref(url) != DEV_REF:
        raise Refused(f"SUPABASE_URL is not the dev project {DEV_REF}. This script runs only against dev.")


def allowlist_ids(settings: Any) -> list[str]:
    raw = str(getattr(settings, "assistant_proactive_user_allowlist", "") or "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def require_allowlist(settings: Any) -> list[str]:
    ids = allowlist_ids(settings)
    if not ids:
        raise Refused("ASSISTANT_PROACTIVE_USER_ALLOWLIST is not set. Without it the tick would route to every "
                      "dev user. Set it on the command line to the test user ids.")
    return ids


def require_test_email(settings: Any) -> None:
    """The live-test recipient override, when set, must be the owner's own
    address (the delivery path guards it again before every send)."""
    override = str(getattr(settings, "assistant_proactive_test_email_to", "") or "").strip()
    if override and override.lower() != OWNER_EMAIL:
        raise Refused(f"ASSISTANT_PROACTIVE_TEST_EMAIL_TO must be empty or {OWNER_EMAIL}.")


def require_router_env(settings: Any) -> None:
    if not (getattr(settings, "assistant_enabled", False) and getattr(settings, "assistant_proactive_enabled", False)):
        raise Refused("ASSISTANT_ENABLED and ASSISTANT_PROACTIVE_ENABLED must both be true on the command line, "
                      "or poll_once returns without doing anything.")


def require_tail_window(now: datetime, real_now: datetime) -> None:
    if not (real_now - TAIL_WINDOW <= now <= real_now):
        raise Refused("With the tails on, --now must be within the last 24 hours and not in the future. "
                      "Use --no-tails to time travel.")


def require_travel_window(now: datetime, real_now: datetime) -> None:
    """Every tick runs the retention purge at its clock, a system sweep over
    every user's router rows, so --now is bounded whatever --no-tails says."""
    if not (real_now - TRAVEL_BACK <= now <= real_now + TRAVEL_AHEAD):
        raise Refused("--now must lie within 30 days before and 8 days after the real now. The tick runs the "
                      "retention purge at that clock, and a far date would purge every dev user's router rows.")


def live_channels(settings: Any) -> list[str]:
    """The channels a tick under these settings can really send on, from the
    inputs send_due reads (the external switch, the global email switch with
    Graph creds, the Teams switch)."""
    if not getattr(settings, "assistant_proactive_external_enabled", False):
        return []
    out = []
    if getattr(settings, "notification_emails_enabled", False) and getattr(settings, "ms_client_id", ""):
        out.append("email")
    if getattr(settings, "teams_alerts_enabled", False):
        out.append("teams")
    return out


def require_live_env(settings: Any, now: datetime, real_now: datetime, *, live_past_slot: bool = False) -> list[str]:
    """A run that can send is the step 9b run only: the owner's override, a
    tick at the real now and the RFP test bench off. Returns the live channels.
    With live_past_slot the clock may instead lie up to 72 hours in the past
    (never ahead of the 5 minute window); every other guard is unchanged."""
    live = live_channels(settings)
    if not live:
        return live
    names = " and ".join(live)
    override = str(getattr(settings, "assistant_proactive_test_email_to", "") or "").strip()
    if override != OWNER_EMAIL:
        raise Refused(f"This run can send {names} for real, so ASSISTANT_PROACTIVE_TEST_EMAIL_TO must be "
                      f"{OWNER_EMAIL} (plan 11.5 step 9b). For a channel_off run set NOTIFICATION_EMAILS_ENABLED=false "
                      "and TEAMS_ALERTS_ENABLED=false on the command line.")
    if live_past_slot:
        if not (real_now - LIVE_PAST_SLOT <= now <= real_now + LIVE_WINDOW):
            raise Refused(f"This run can send {names} for real, so with --live-past-slot --now must be at most 72 "
                          "hours in the past and not more than 5 minutes ahead of the real now.")
    elif abs(now - real_now) > LIVE_WINDOW:
        raise Refused(f"This run can send {names} for real, so --now must be within 5 minutes of the real now "
                      "(with or without --no-tails), or the quiet-hours and staleness gates see a travelled clock.")
    if getattr(settings, "rfp_testing_enabled", False):
        raise Refused(f"This run can send {names} for real, so RFP_TESTING_ENABLED must be false on the command line.")
    return live


def require_e2e_allowlist(ids: list[str], lookup: Any) -> None:
    """A run that can send routes only to the two e2e accounts; `lookup`
    returns a profiles row (or None) for an id."""
    for uid in ids:
        person = lookup(uid) or {}
        if str(person.get("email") or "").strip().lower() not in E2E_EMAILS:
            raise Refused(f"This run can send for real, so every allowlisted id must be one of the two e2e accounts; "
                          f"{uid} is not.")


def check_guards(settings: Any, *, now: datetime, tails: bool, real_now: datetime | None = None,
                 live_past_slot: bool = False) -> list[str]:
    """Run every refusal; returns the allowlist ids."""
    clock = real_now or _real_now()
    require_dev_ref(settings)
    ids = require_allowlist(settings)
    require_test_email(settings)
    require_router_env(settings)
    if tails:
        require_tail_window(now, clock)
    require_travel_window(now, clock)
    require_live_env(settings, now, clock, live_past_slot=live_past_slot)
    return ids


# ── Arguments, times and the seed file ───────────────────────────────────────


def parse_when(value: str) -> datetime:
    """An aware UTC datetime; a naive value is Pacific time."""
    text = (value or "").strip()
    if not text:
        raise ValueError("a time is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not an ISO 8601 time: {text!r}") from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=PT)).astimezone(UTC)


def _when(value: str) -> datetime:
    try:
        return parse_when(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant_proactive_tick.py", description="One proactive router tick at a chosen time (dev only).")
    parser.add_argument("--now", required=True, type=_when, help="tick time, ISO 8601; naive means America/Los_Angeles")
    parser.add_argument("--no-tails", action="store_true", help="skip the table tails; never touch the cursors")
    parser.add_argument("--live-past-slot", action="store_true",
                        help="live channels only: allow --now up to 72 hours in the past (a digest slot that already "
                             "passed); every other live guard still applies")
    parser.add_argument("--seed", metavar="FILE", help="JSON file of assistant_events rows to insert first")
    parser.add_argument("--worker", metavar="ID", help="worker id (default: a fresh script id)")
    return parser


def default_worker() -> str:
    return f"tick-script:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def load_seed(path: str, allowed_ids: list[str]) -> list[dict]:
    """Read and validate a seed file: a JSON list (or {"events": [...]}) of
    objects with event_type, subject_user_id (on the allowlist) and an
    explicit occurred_at; dedupe_key is computed from the cleaned payload when
    omitted. Returns assistant_events rows with source 'emit'."""
    from app.assistant import proactive

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("events") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise Refused("The seed file must hold a non-empty list of events.")
    allowed = set(allowed_ids)
    rows: list[dict] = []
    for index, item in enumerate(items):
        where = f"seed event {index}"
        if not isinstance(item, dict):
            raise Refused(f"{where} is not an object.")
        unknown = sorted(set(item) - set(SEED_FIELDS))
        if unknown:
            raise Refused(f"{where} has unknown fields: {', '.join(unknown)}.")
        kind = str(item.get("event_type") or "")
        if kind not in proactive.EVENT_TYPES:
            raise Refused(f"{where} has an unknown event_type.")
        user = str(item.get("subject_user_id") or "").strip().lower()
        if user not in allowed:
            raise Refused(f"{where} names a user who is not on the allowlist.")
        if not item.get("occurred_at"):
            raise Refused(f"{where} needs an explicit occurred_at.")
        try:
            occurred = parse_when(str(item["occurred_at"]))
        except ValueError as exc:
            raise Refused(f"{where}: {exc}") from None
        project_id = item.get("project_id") or None
        payload = proactive.clean_payload(kind, item.get("payload") or {})
        key = item.get("dedupe_key")
        if not key:
            try:
                key = proactive.event_dedupe_key(kind, payload, project_id)
            except ValueError as exc:
                raise Refused(f"{where}: {exc}") from None
        rows.append({
            "event_type": kind, "dedupe_key": str(key), "project_id": project_id, "subject_user_id": user,
            "payload": payload, "occurred_at": occurred.isoformat(), "source": "emit", "status": "pending",
        })
    return rows


def seed_events(rows: list[dict]) -> dict[str, Any]:
    """Insert the rows (a duplicate dedupe_key is ignored) and report the ids
    this call inserted and the keys that already existed."""
    from app.assistant import repo

    inserted = repo.insert_events(None, rows)
    got = {str(r.get("dedupe_key")) for r in inserted}
    return {"ids": [str(r["id"]) for r in inserted if r.get("id")],
            "existing_keys": [r["dedupe_key"] for r in rows if r["dedupe_key"] not in got]}


# ── Main ─────────────────────────────────────────────────────────────────────


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    from app.core.config import get_settings

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - an invalid env is a refusal, never a traceback with values
        raise Refused(f"The settings did not load ({exc.__class__.__name__}). Check the command line env.") from None
    ids = check_guards(settings, now=args.now, tails=not args.no_tails, live_past_slot=args.live_past_slot)
    if live_channels(settings):
        from app.assistant import repo

        require_e2e_allowlist(ids, repo.human_profile)
    rows = load_seed(args.seed, ids) if args.seed else []
    worker = args.worker or default_worker()
    seeded: dict[str, Any] = {"ids": [], "existing_keys": []}
    if rows:
        seeded = seed_events(rows)
    from app.services import assistant_proactive

    result = assistant_proactive.poll_once(args.now, worker=worker, tails=not args.no_tails)
    sends = result.pop("sends", None) if isinstance(result, dict) else None
    return {
        "now": args.now.isoformat(), "now_pacific": args.now.astimezone(PT).isoformat(), "worker": worker,
        "tails": not args.no_tails, "external": bool(settings.assistant_proactive_external_enabled),
        "live_channels": live_channels(settings),
        "live_past_slot": bool(args.live_past_slot and live_channels(settings)),
        "test_email_override": bool(str(settings.assistant_proactive_test_email_to or "").strip()),
        "seeded_event_ids": seeded["ids"], "seed_existing_keys": seeded["existing_keys"],
        "poll": result, "sends": sends,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        summary = run(argv)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
