"""Summarize what the curated-profile migration needs from a live dev run (WI-20). Read only.

Browser v2 plan 6.3, 6.4, 8.3 and 17.5: before acceptance, live probe tasks on
`procore` and `autodesk` record each site's login paths, safe_mutations,
final_actions, final_get and code email senders and subjects; the values ship
in supabase/migrations/0194_assistant_browser_curated_profiles.sql. This
script reads what those probe tasks left on the development project and
prints a summary plus a JSON block to paste into 0194. It never writes to any
database, never prints a code, a body, a password or a full URL with its
query (step traces keep origin plus path only; hold URLs are cut to the path).

    cd bdr_be
    # 1. Summarize probe tasks (the owner's profile id; the tasks' job ids or a start time)
    uv run python scripts/browser_v2_probe_site.py summarize --site procore \\
        --owner <profile uuid> --since 2026-11-03T08:00:00-08:00 [--job <uuid> ...] [--mail] [--json]
    # 2. After pasting the values into 0194, check the file offline (no database)
    uv run python scripts/browser_v2_probe_site.py check-migration \\
        supabase/migrations/0194_assistant_browser_curated_profiles.sql

Where the values come from (all already recorded by the v2 server and worker):
- login form origins and posts: the worker's `login.exemption_open` and
  `login.exempt` steps (encrypted step records of the owner's job);
- read-style POSTs: the owner's "This was not a final action" rules
  (assistant_browser_user_safe_rules) and IT Admin suggestions;
- final actions: final-action holds (assistant_browser_holds) the owner
  declined or approved and did not mark safe, with the held path from the
  encrypted hold summary; GET final actions: click holds on links;
- WebSockets: `http.websocket` and `http.blocked` steps;
- code emails (--mail): the owner's own mailbox, subject masked, DKIM and
  To checks as the email code reader does, and whether the site regex (or the
  generic pattern) finds exactly one code. Counts only, never the code.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEV_REF = "bpidntbyvoooqvaispup"
PROD_REF = "zxqkqotlcgbalwoqeeca"
CURATED_KEYS = ("procore", "autodesk", "buildingconnected", "smartbid")
# Sender domains listed with --mail beyond the site row's own (the row is
# what is being probed). Only a DKIM-aligned one is proposed for 0194.
EXTRA_CODE_SENDERS = {"procore": ("procoretech.com",)}
# What the email code reader needs to run in this read-only process
# (assistant_ms.configured): set for this process only, never written.
# ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED only lets the settings validate while
# FULL_SELF_HOSTED_LLMS_ENABLED is on (as the acceptance helper does); this
# script calls no model.
MAIL_ENV = ("ASSISTANT_ENABLED", "ASSISTANT_OUTLOOK_ENABLED", "ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED")
# Keys the 0194 migration may set on a curated row; anything else is refused
# by both this check and the migration's own guard.
MIGRATION_KEYS = ("registrable_domains", "exact_origins", "login_origins", "login_paths", "cookie_domains",
                  "code_email_senders", "code_email_regex", "profile", "needs_headed")
PROFILE_KEYS = ("safe_mutations", "final_actions", "final_get", "websocket_ok", "websocket_reason", "logged_in_probe")
PROFILES_START = "$profiles$"
# The D2 verb list as the owner's "not a final action" route refuses it
# (app/routers/assistant_browser.py FINAL_VERBS): a safe rule never names one.
FINAL_VERBS = re.compile(r"(submit|send|accept|declin|delet|remov|pay|approv|answer|respond|sign|award|confirm)", re.I)
_NUMERIC = re.compile(r"^[0-9]+$")
_TOKEN = re.compile(r"^(?:[A-Za-z0-9_-]{20,}|[0-9a-fA-F-]{16,})$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIGITS = re.compile(r"[0-9]")
_GRAPHQL_PATH = re.compile(r"graphql", re.I)
# The class a generalized (number-free) segment becomes in a rule pattern.
TOKEN_CLASS = "[A-Za-z0-9_-]+"


class ProbeError(SystemExit):
    pass


# ── Pure helpers (tested without a database) ─────────────────────────────────


def path_regex(path: str) -> str:
    """The anchored rule pattern for one observed path, generalized the way
    the owner's safe rules are (numeric, UUID and token segments), plus the
    worker trace's `:token` placeholder. As in the safe-rule route, a long
    segment counts as a token only when it holds a digit, so a word path such
    as /sessions/submit_login_password stays exact."""
    parts = []
    for segment in (path or "/").split("/"):
        if _NUMERIC.match(segment):
            parts.append("[0-9]+")
        elif segment == ":token" or _UUID.match(segment) or (_TOKEN.match(segment) and _DIGITS.search(segment)):
            parts.append("[A-Za-z0-9_-]+")
        else:
            parts.append(re.escape(segment))
    return "^" + "/".join(parts) + "$"


def split_url(url: Any) -> tuple[str, str, str] | None:
    """(origin, host, path) of an http(s) URL; the query is dropped."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme, host = (parts.scheme or "").lower(), (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    origin = f"{scheme}://{host}" + (f":{port}" if port else "")
    return origin, host + (f":{port}" if port else ""), parts.path or "/"


def host_in_site(host: str, site: Mapping[str, Any]) -> bool:
    bare = host.split(":", 1)[0]
    if any(bare == d or bare.endswith("." + d) for d in site.get("registrable_domains") or ()):
        return True
    return any((split_url(o) or ("", "", ""))[1] == host for o in site.get("exact_origins") or ())


def normalize_kind(kind: Any) -> str:
    """Guard steps reach the ledger as browser.hold_<x> (runner.guard_step_logger)."""
    text = str(kind or "")
    if text.startswith("browser.hold_"):
        return "hold." + text[len("browser.hold_"):]
    return text


def mask_subject(subject: Any) -> str:
    """A code email subject safe to print: every digit becomes #."""
    return _DIGITS.sub("#", str(subject or ""))[:200]


def _rule_key(rule: Mapping[str, Any]) -> tuple:
    return (rule.get("method"), rule.get("host"), rule.get("path_regex"), tuple(rule.get("graphql_operations") or ()))


def summarize(site: Mapping[str, Any], steps: Iterable[Mapping[str, Any]], holds: Iterable[Mapping[str, Any]],
              safe_rules: Iterable[Mapping[str, Any]] = (), suggestions: Iterable[Mapping[str, Any]] = ()) -> dict:
    """What the probe tasks show for `site`. `steps` are decrypted step
    records; `holds` are hold rows, each with an optional `summary` (the
    decrypted hold summary) and `marked_safe` (a safe rule came from it)."""
    pages: Counter = Counter()
    form_origins: set[str] = set()
    login_posts: Counter = Counter()
    fill_refused: Counter = Counter()
    websocket: Counter = Counter()
    blocked: Counter = Counter()
    final_get: dict[tuple, dict] = {}
    warnings: list[str] = []
    for step in steps:
        kind = normalize_kind(step.get("kind"))
        result = step.get("result") if isinstance(step.get("result"), dict) else {}
        for key in ("url_before", "url_after"):
            parsed = split_url(step.get(key))
            if parsed and host_in_site(parsed[1], site):
                pages[(parsed[0], parsed[2])] += 1
        if kind == "login.exemption_open" and isinstance(result.get("origin"), str):
            form_origins.add(result["origin"])
        elif kind == "login.exempt":
            parsed = split_url(result.get("url"))
            if parsed:
                login_posts[(str(result.get("method") or "POST"), parsed[0], parsed[2])] += 1
        elif kind.startswith("tool.") and step.get("tool_name") == "fill_credential" and step.get("ok") is False:
            parsed = split_url(step.get("url_before"))
            if parsed:
                fill_refused[parsed[0]] += 1
        elif kind == "http.websocket":
            parsed = split_url(str(result.get("url") or "").replace("wss://", "https://", 1).replace("ws://", "http://", 1))
            if parsed:
                websocket[(parsed[1], parsed[2])] += 1
        elif kind == "http.blocked":
            parsed = split_url(str(result.get("url") or "").replace("wss://", "https://", 1).replace("ws://", "http://", 1))
            if parsed:
                blocked[(str(result.get("method") or ""), parsed[1], parsed[2], str(result.get("reason") or ""))] += 1
        elif kind == "hold.click":
            parsed = split_url(result.get("url"))
            if parsed and host_in_site(parsed[1], site):
                rule = {"host": parsed[1], "path_regex": path_regex(parsed[2]),
                        "label": "TODO: name this action (seen as a click hold)"}
                final_get[(rule["host"], rule["path_regex"])] = rule

    holds = list(holds)
    # The held path of each hold, so a safe rule is rebuilt from what was
    # really sent: a stored pattern may have turned a long word segment into
    # a token class and so match a final action next to it.
    held_paths: dict[str, str] = {}
    for hold in holds:
        summary = hold.get("summary") if isinstance(hold.get("summary"), dict) else {}
        parsed = split_url(summary.get("url"))
        if hold.get("id") and parsed:
            held_paths[str(hold["id"])] = parsed[2]
    safe: dict[tuple, dict] = {}
    for row in [*safe_rules, *(s.get("rule") or {} for s in suggestions)]:
        if not isinstance(row, Mapping) or not row.get("method") or not row.get("host"):
            continue
        source = row.get("source_hold_id")
        raw_path = held_paths.get(str(source)) if source else None
        pattern = path_regex(raw_path) if raw_path is not None else (row.get("path_regex") or row.get("path_pattern"))
        op = row.get("graphql_operation")
        ops = list(row.get("graphql_operations") or ([op] if op else []))
        if (not isinstance(pattern, str) or FINAL_VERBS.search(pattern) or FINAL_VERBS.search(raw_path or "")
                or any(FINAL_VERBS.search(o) for o in ops)):
            warnings.append(f"skipped a safe rule naming a final-action verb: {row.get('method')} {pattern}")
            continue
        if raw_path is None and TOKEN_CLASS in pattern:
            warnings.append(f"skipped a safe rule with a generalized segment and no held path in this run: "
                            f"{row.get('method')} {row.get('host')} {pattern}; review it by hand before proposing it")
            continue
        rule = {"method": str(row["method"]).upper(), "host": str(row["host"]).lower(), "path_regex": pattern}
        if ops:
            rule["graphql_operations"] = sorted(set(ops))
        if not host_in_site(rule["host"], site):
            warnings.append(f"safe rule host {rule['host']} is outside the site's domains; not proposed")
            continue
        safe[_rule_key(rule)] = rule

    finals: dict[tuple, dict] = {}
    outside: Counter = Counter()
    hold_states: Counter = Counter()
    for hold in holds:
        hold_states[(str(hold.get("kind")), str(hold.get("status")), str(hold.get("decision")))] += 1
        if hold.get("kind") != "final_action":
            continue
        summary = hold.get("summary") if isinstance(hold.get("summary"), dict) else {}
        parsed = split_url(summary.get("url")) or split_url(hold.get("origin"))
        if not parsed:
            continue
        method = str(hold.get("method") or summary.get("method") or "").upper()
        if not host_in_site(parsed[1], site):
            outside[(method, parsed[1], parsed[2])] += 1
            continue
        if hold.get("marked_safe"):
            continue
        pattern = path_regex(parsed[2])
        if summary.get("graphql_operation") or _GRAPHQL_PATH.search(parsed[2]):
            warnings.append(f"{method} {parsed[1]}{parsed[2]} looks like GraphQL: a final_actions path rule labels "
                            "every operation on it; record the operation name by hand if it matters")
        label = hold.get("label") or summary.get("profile_label") or summary.get("element_label")
        if not isinstance(label, str) or not label.strip() or label.startswith(method + " "):
            label = "TODO: name this action"
        if summary.get("trigger") == "click" and method in ("GET", "HEAD"):
            final_get[(parsed[1], pattern)] = {"host": parsed[1], "path_regex": pattern, "label": label[:120]}
            continue
        finals[(method, parsed[1], pattern)] = {"method": method, "host": parsed[1], "path_regex": pattern,
                                                "label": label[:120]}

    login_origins = sorted(form_origins | {origin for _, origin, _ in login_posts})
    login_paths = sorted({path_regex(path) for _, _, path in login_posts})
    if fill_refused:
        warnings.append("fill_credential was refused on "
                        + ", ".join(sorted(fill_refused))
                        + ": if that is the real sign-in page, add its origin to login_origins")
    if not login_posts:
        warnings.append("no login form post was recorded: run a probe task that signs in (saved session absent)")
    for (host, path) in websocket:
        warnings.append(f"a WebSocket was opened to {host}{path}: websocket_ok needs a stated reason")
    current_profile = site.get("profile") if isinstance(site.get("profile"), dict) else {}
    return {
        "site_key": site.get("site_key"),
        "current": {
            "login_origins": list(site.get("login_origins") or []),
            "login_paths": list(site.get("login_paths") or []),
            "cookie_domains": list(site.get("cookie_domains") or []),
            "code_email_senders": list(site.get("code_email_senders") or []),
            "code_email_regex_set": bool(site.get("code_email_regex")),
            "profile_rule_counts": {k: len(current_profile.get(k) or []) for k in ("safe_mutations", "final_actions",
                                                                                  "final_get")},
        },
        "observed": {
            "pages": [{"origin": o, "path_regex": path_regex(p), "count": n} for (o, p), n in sorted(pages.items())],
            "login_form_origins": sorted(form_origins),
            "login_posts": [{"method": m, "origin": o, "path": p, "count": n}
                            for (m, o, p), n in sorted(login_posts.items())],
            "websockets": [{"host": h, "path": p, "count": n} for (h, p), n in sorted(websocket.items())],
            "blocked": [{"method": m, "host": h, "path": p, "reason": r, "count": n}
                        for (m, h, p, r), n in sorted(blocked.items())],
            "holds": [{"kind": k, "status": s, "decision": d, "count": n} for (k, s, d), n in sorted(hold_states.items())],
            "posts_outside_site": [{"method": m, "host": h, "path": p, "count": n}
                                   for (m, h, p), n in sorted(outside.items())],
        },
        "proposed": {
            "login_origins": login_origins,
            "login_paths": login_paths,
            "profile": {
                "safe_mutations": sorted(safe.values(), key=_rule_key),
                "final_actions": sorted(finals.values(), key=lambda r: (r["host"], r["path_regex"], r["method"])),
                "final_get": sorted(final_get.values(), key=lambda r: (r["host"], r["path_regex"])),
                "websocket_ok": False,
                "websocket_reason": None,
                "logged_in_probe": current_profile.get("logged_in_probe")
                or "TODO: {\"path\": \"/...\", \"selector\": \"...\"} seen only when signed in, or null",
            },
        },
        "warnings": warnings,
    }


def migration_entry(summary: Mapping[str, Any], mail: Iterable[Mapping[str, Any]] = ()) -> dict:
    """The JSON object for this site inside 0194's $profiles$ block."""
    proposed = summary["proposed"]
    entry: dict[str, Any] = {
        "login_origins": proposed["login_origins"] or ["TODO: the sign-in page origin (no login was recorded)"],
        "login_paths": proposed["login_paths"] or ["TODO: the login form post paths (no login was recorded)"],
        "profile": proposed["profile"],
    }
    senders = {m["from_domain"] for m in mail if m.get("dkim_aligned") and m.get("from_domain")}
    if senders:
        # Added to the row's senders, never replacing them: a run that only
        # saw a procoretech.com code still keeps procore.com.
        current = (summary.get("current") or {}).get("code_email_senders") or []
        entry["code_email_senders"] = sorted(senders | set(current))
    return entry


def extract_profiles(sql: str) -> dict:
    """The JSON object between the two $profiles$ markers of 0194."""
    first = sql.find(PROFILES_START)
    second = sql.find(PROFILES_START, first + len(PROFILES_START)) if first >= 0 else -1
    if first < 0 or second < 0 or sql.find(PROFILES_START, second + len(PROFILES_START)) >= 0:
        raise ProbeError("0194 must hold exactly one $profiles$ ... $profiles$ JSON block")
    try:
        data = json.loads(sql[first + len(PROFILES_START):second])
    except ValueError as exc:
        raise ProbeError(f"the $profiles$ block is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ProbeError("the $profiles$ block must be a JSON object keyed by site_key")
    return data


def _site_domains(key: str, entry: Mapping[str, Any]) -> list[str]:
    from app.services.assistant_browser_sites import CURATED_DOMAINS

    domains = entry.get("registrable_domains")
    if isinstance(domains, list) and domains:
        return [str(d) for d in domains]
    return list(CURATED_DOMAINS.get(key, ("", ()))[1])


def check_profiles(data: Mapping[str, Any]) -> list[str]:
    """Problems that would make 0194 wrong or refused by the worker; [] when
    the block can be applied. Offline: no database."""
    from pydantic import ValidationError

    from app.services.assistant_browser_mfa_mail import compile_site_regex
    from app.services.browser_agents import protocol

    problems: list[str] = []
    if "TODO" in json.dumps(data):
        problems.append("TODO values remain (the migration refuses to apply until every TODO is replaced)")
    for key, entry in data.items():
        where = f"{key}:"
        if key not in CURATED_KEYS:
            problems.append(f"{where} not a curated site ({', '.join(CURATED_KEYS)})")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{where} must be an object")
            continue
        extra = sorted(set(entry) - set(MIGRATION_KEYS))
        if extra:
            problems.append(f"{where} unknown keys {extra}")
        site = {"registrable_domains": _site_domains(key, entry), "exact_origins": entry.get("exact_origins") or []}
        for name in ("registrable_domains", "exact_origins", "login_origins", "login_paths", "cookie_domains",
                     "code_email_senders"):
            if name in entry and (not isinstance(entry[name], list) or not all(isinstance(v, str) for v in entry[name])):
                problems.append(f"{where} {name} must be a list of strings")
        if "TODO" in json.dumps(entry):
            continue
        for origin in entry.get("login_origins") or []:
            parsed = split_url(origin)
            if not protocol.HTTPS_ORIGIN_RE.match(origin) or not parsed or not host_in_site(parsed[1], site):
                problems.append(f"{where} login origin {origin} must be an https origin on the site's domains")
        for pattern in entry.get("login_paths") or []:
            try:
                re.compile(pattern)
            except re.error:
                problems.append(f"{where} login path {pattern!r} is not a valid regex")
                continue
            if not pattern.startswith("^") or not pattern.endswith("$"):
                problems.append(f"{where} login path {pattern!r} should be anchored with ^ and $")
        for sender in entry.get("code_email_senders") or []:
            if not protocol.HOST_RE.match(sender):
                problems.append(f"{where} code email sender {sender!r} must be a domain")
        if "code_email_regex" in entry and entry["code_email_regex"] is not None:
            regex = entry["code_email_regex"]
            if compile_site_regex(regex) is None:
                problems.append(f"{where} code_email_regex does not compile (or is over 500 characters)")
            elif "\\A" not in regex or regex.count("(") - regex.count("(?") < 1:
                problems.append(f"{where} code_email_regex must be anchored at the subject start (\\A) and capture the code")
        if "profile" not in entry:
            continue
        profile = entry["profile"]
        if not isinstance(profile, dict) or set(profile) - set(PROFILE_KEYS):
            problems.append(f"{where} profile keys must be among {list(PROFILE_KEYS)}")
            continue
        try:
            parsed_profile = protocol.UserSiteProfile.model_validate(profile)
        except ValidationError as exc:
            reasons = "; ".join(str(e.get("msg")) for e in exc.errors(include_input=False))
            problems.append(f"{where} profile refused by protocol.UserSiteProfile: {reasons}")
            continue
        if parsed_profile.websocket_ok and not parsed_profile.websocket_reason:
            problems.append(f"{where} websocket_ok needs websocket_reason (plan 6.2)")
        for rule in parsed_profile.safe_mutations:
            if not host_in_site(rule.host, site):
                problems.append(f"{where} safe mutation host {rule.host} is outside the site's domains")
            if FINAL_VERBS.search(rule.path_regex) or any(FINAL_VERBS.search(o) for o in rule.graphql_operations or []):
                problems.append(f"{where} safe mutation {rule.method} {rule.path_regex} names a final-action verb")
        for rule in [*parsed_profile.final_actions, *parsed_profile.final_get]:
            if not host_in_site(rule.host, site):
                problems.append(f"{where} final rule host {rule.host} is outside the site's domains")
    return problems


# ── Database reads (main only) ───────────────────────────────────────────────


def _guard_dev() -> None:
    from app.core.config import get_settings

    url = (get_settings().supabase_url or "").rstrip("/")
    if PROD_REF in url or url != f"https://{DEV_REF}.supabase.co":
        raise ProbeError("This probe reads only the BDR development project.")


def _steps(owner: str, job_id: str) -> list[dict]:
    from app.assistant import repo
    from app.services import assistant_browser_records as records

    out: list[dict] = []
    cursor = None
    for _ in range(50):
        rows = repo.browser_records(owner, job_id, kinds=("step",), before=cursor, limit=201)
        for row in rows[:200]:
            try:
                out.append(records.unpack_record(owner, job_id, row))
            except records.BrowserContentUnavailable:
                continue
        if len(rows) <= 200:
            break
        cursor = (rows[199]["created_at"], rows[199]["id"])
    return out


def _jobs(owner: str, job_ids: list[str], since: datetime | None) -> list[dict]:
    from app.assistant import repo

    if job_ids:
        rows = [repo.browser_job(owner, j) for j in job_ids]
        missing = [j for j, r in zip(job_ids, rows) if not r]
        if missing:
            raise ProbeError(f"not a personal job of this owner: {', '.join(missing)}")
        return [r for r in rows if r]
    rows = [r for r in repo.browser_jobs(owner, limit=50) if r.get("agent_type") == "user_browser"]
    if since is not None:
        rows = [r for r in rows if datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00")) >= since]
    return rows


def _mail(owner: str, site: Mapping[str, Any], since: datetime, extra_senders: Iterable[str] = ()) -> list[dict]:
    """Code emails in the owner's own mailbox since `since`: sender domain,
    DKIM alignment, To check, masked subject, code counts. Never a code or body.
    `extra_senders` widen the listing (and the alignment check) to sender
    domains the site row does not name yet, such as procoretech.com."""
    from app.services import assistant_browser_mfa_mail as mfa

    domains = sorted(set(site.get("code_email_senders") or []) | set(site.get("registrable_domains") or [])
                     | {str(d).lower() for d in extra_senders})
    regex = mfa.compile_site_regex(site.get("code_email_regex"))
    mailbox, token = mfa.owner_mailbox(owner)
    out = []
    for row in mfa._list_recent(mailbox, token, since):
        listed = mfa._domain(((row.get("from") or {}).get("emailAddress") or {}).get("address"))
        if not mfa.covered(listed, domains):
            continue
        message = mfa._read_one(mailbox, token, str(row["id"]))
        address = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        text = str(message.get("subject") or "") + "\n" + str(body.get("content") or "")
        out.append({
            "received": message.get("receivedDateTime"),
            "from_domain": mfa._domain(address),
            "dkim_aligned": mfa.sender_trusted(message.get("internetMessageHeaders") or [], allowed_domains=domains,
                                               tenant_authserv_id=mfa.TENANT_AUTHSERV_ID,
                                               from_address=address if isinstance(address, str) else None),
            "addressed_to_owner": mfa.addressed_to(message, mailbox),
            "subject_masked": mask_subject(message.get("subject")),
            "site_regex_codes": len(mfa.site_codes(text, site_regex=regex)) if regex else None,
            "generic_codes": len(mfa.generic_codes(text)),
        })
        del text, body, message
    return out


def mail_environment(environ: Any = None) -> None:
    """Turn on, for this read-only process only, the flags the email code
    reader needs. The development .env keeps them off (runbook part A), and
    ASSISTANT_ENABLED needs an assistant token secret to validate; a random
    one is made for this process when none is set (it signs nothing here)."""
    import os
    import secrets

    env = os.environ if environ is None else environ
    for name in MAIL_ENV:
        env[name] = "true"
    if len((env.get("ASSISTANT_TOKEN_SECRET") or "").strip()) < 32:
        env["ASSISTANT_TOKEN_SECRET"] = secrets.token_urlsafe(48)
    if environ is None:
        from app.core.config import get_settings

        get_settings.cache_clear()


def run_summary(args: argparse.Namespace) -> dict:
    from app.assistant import repo
    from app.core.supabase_client import get_supabase
    from app.services import assistant_browser_holds as holds_service

    _guard_dev()
    owner = args.owner
    site = repo.browser_site(owner, args.site)
    if not site:
        raise ProbeError(f"no site row {args.site!r}")
    since = datetime.fromisoformat(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        raise ProbeError("--since needs a time zone, for example 2026-11-03T08:00:00-08:00")
    jobs = _jobs(owner, args.job or [], since)
    if not jobs:
        raise ProbeError("no user_browser jobs found for this owner in that window")
    rules = repo.browser_safe_rules(owner, site_key=args.site)
    rule_holds = {str(r.get("source_hold_id")) for r in rules if r.get("source_hold_id")}
    suggestions = (get_supabase().table("assistant_browser_profile_suggestions").select("rule,status")
                   .eq("site_key", args.site).eq("status", "new").limit(500).execute().data) or []
    steps: list[dict] = []
    holds: list[dict] = []
    for job in jobs:
        steps.extend(_steps(owner, str(job["id"])))
        for hold in repo.browser_holds(owner, str(job["id"]), limit=200):
            holds.append({**hold, "summary": holds_service.summary(owner, hold),
                          "marked_safe": str(hold.get("id")) in rule_holds})
    summary = summarize(site, steps, holds, rules, suggestions)
    summary["jobs"] = [{"id": j["id"], "status": j.get("status"), "created_at": j.get("created_at")} for j in jobs]
    mail: list[dict] = []
    if args.mail:
        window = since or min(datetime.fromisoformat(str(j["created_at"]).replace("Z", "+00:00")) for j in jobs)
        from app.services.assistant_browser_mfa_mail import MfaMailRefused

        extra = sorted({*EXTRA_CODE_SENDERS.get(args.site, ()), *(getattr(args, "sender", None) or [])})
        try:
            mail = _mail(owner, site, window - timedelta(minutes=5), extra)
        except MfaMailRefused as exc:
            summary["warnings"].append(f"code emails could not be read: {exc.code}")
        summary["code_emails"] = mail
    summary["migration_entry"] = {args.site: migration_entry(summary, mail)}
    return summary


def _print_text(summary: Mapping[str, Any]) -> None:
    print(f"Site {summary['site_key']}: {len(summary.get('jobs') or [])} probe job(s)")
    print("Current row:", json.dumps(summary["current"], sort_keys=True))
    observed = summary["observed"]
    for name in ("login_form_origins", "login_posts", "posts_outside_site", "websockets", "blocked", "holds"):
        print(f"{name}: {json.dumps(observed[name], sort_keys=True)}")
    print(f"pages seen on the site: {len(observed['pages'])}")
    for item in summary.get("code_emails") or []:
        print("code email:", json.dumps(item, sort_keys=True))
    for warning in summary["warnings"]:
        print("WARNING:", warning)
    print("\nPaste into 0194's $profiles$ block (replace this site's TODO entry):")
    print(json.dumps(summary["migration_entry"], indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("summarize", help="read a live dev run (read only)")
    s.add_argument("--site", required=True, choices=CURATED_KEYS)
    s.add_argument("--owner", required=True, help="the owner's profile id")
    s.add_argument("--job", action="append", help="a probe job id (repeatable); default: recent jobs")
    s.add_argument("--since", help="ISO time with zone; jobs created from then")
    s.add_argument("--mail", action="store_true", help="also list code emails in the owner's own mailbox")
    s.add_argument("--sender", action="append", help="with --mail: one more sender domain to list (repeatable)")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("check-migration", help="check the 0194 $profiles$ block offline")
    c.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "check-migration":
        problems = check_profiles(extract_profiles(args.path.read_text(encoding="utf-8")))
        for problem in problems:
            print("PROBLEM:", problem)
        print("OK: the profiles block can be applied." if not problems else f"{len(problems)} problem(s).")
        return 0 if not problems else 1
    if args.mail:
        mail_environment()
    summary = run_summary(args)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        _print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
