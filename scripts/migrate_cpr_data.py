"""One-off data migration: legacy Certified Payroll (CPR) Supabase -> BDR dev.

Copies ALL live data from the legacy CPR app's Supabase project into the BDR
dev project's CP tables (migrations 0063_cp_core / 0064_cp_payroll).

Idempotent by design: rows keep their ORIGINAL UUID primary keys and are
written with upsert on_conflict="id" (re-runs are no-ops); storage object
paths are deterministic (derived from source row ids) and uploaded with
upsert=true. There is NO truncation anywhere.

Usage (from bdr_be):
    uv run python scripts/migrate_cpr_data.py               # dry-run (default)
    uv run python scripts/migrate_cpr_data.py --dry-run
    uv run python scripts/migrate_cpr_data.py --execute
    uv run python scripts/migrate_cpr_data.py --execute --org-settings-user <user_id>

Env (bdr_be/.env or exported):
    CPR_SUPABASE_URL / CPR_SUPABASE_SERVICE_ROLE_KEY  — the SOURCE project
    CPR_ACTOR_EMAIL (optional) — BDR profile email stamped as created_by /
        cp_enrolled_by / uploaded_by on migrated rows; unset -> NULL

Safety: hard-aborts if the TARGET is BDR_Prod (project ref
zxqkqotlcgbalwoqeeca) or settings.environment == "production". The target
must be the BDR dev project.

RE-RUN CAVEAT: an upsert over already-migrated rows takes the ON CONFLICT
UPDATE path, which fires the set_updated_at() triggers and stamps every row's
updated_at to now() — making all migrated finalized reports read as "stale
since finalization" (check_stale_since_finalization compares updated_at >
finalized_at). The 2026-07-15 execute repaired this by backdating
(updated_at := created_at; migration-created cp_details/cp_only projects to
2026-02-01) with triggers disabled. If you re-run --execute, repeat that
repair (see the plan file / migration notes).
"""

import argparse
import base64
import hashlib
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import Client, create_client

# Allow running as a plain script (`uv run python scripts/migrate_cpr_data.py`):
# put the project root (bdr_be) on the import path so `app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services.storage import download_file, upload_file

ROOT = Path(__file__).resolve().parent.parent
BDR_PROD_REF = "zxqkqotlcgbalwoqeeca"  # NEVER a migration target
PAGE = 1000          # PostgREST fetch page size
CHUNK = 500          # upsert batch size

# Target enum values (0063/0064) — every enum-ish source column is validated
# against these before anything is written.
STATUS_VALUES = {
    "draft", "awaiting_timesheet", "awaiting_payroll_detail",
    "processing", "processed", "submitted",
}
PROJECT_GROUP_VALUES = {"prevailing_wage", "regular"}
REPORT_TYPE_VALUES = {"lcp_tracker", "comply", "paper"}
SHIFT_TYPE_VALUES = {"four_tens", "nights", "swing", "regular"}
DOC_CATEGORY_VALUES = {"w4", "i9", "certification", "license", "other"}


class CprEnv(BaseSettings):
    """Source-project credentials + actor, from bdr_be/.env (or the process env)."""

    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")

    cpr_supabase_url: str = ""
    cpr_supabase_service_role_key: str = ""
    cpr_actor_email: str = ""
    # Comma-separated source project NUMBERS to exclude from project creation
    # (junk/test rows). Excluded projects become cp_ignored_projects registry
    # entries instead — their hours stay visible as non-CP context, but no BDR
    # project row is created and no number is retired.
    cpr_skip_project_numbers: str = ""


# ── small helpers ───────────────────────────────────────────────────────────


def fail(msg: str) -> None:
    print(f"\nABORT: {msg}", file=sys.stderr)
    sys.exit(1)


def norm(s: str | None) -> str:
    return (s or "").strip().lower()


def safe_name(filename: str) -> str:
    return (filename or "file").replace("/", "_").replace("\x00", "")


_TZ_SUFFIX = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def naive_utc(ts: str | None) -> str | None:
    """Legacy naive timestamps were datetime.utcnow() — label them UTC."""
    if not ts:
        return ts
    s = str(ts)
    return s if _TZ_SUFFIX.search(s) else s + "+00:00"


def chunks(seq: list, n: int = CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def fetch_all(client: Client, table: str, columns: str = "*", order: str = "id") -> list[dict]:
    rows: list[dict] = []
    page = 0
    while True:
        start = page * PAGE
        res = (
            client.table(table)
            .select(columns)
            .order(order)
            .range(start, start + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        page += 1


def count_exact(query) -> int:
    return query.limit(1).execute().count or 0


def sum_decimal(values) -> Decimal:
    total = Decimal("0")
    for v in values:
        if v is not None:
            total += Decimal(str(v))
    return total


def strip_keys(row: dict, drop: tuple[str, ...], ts_fields: tuple[str, ...]) -> dict:
    out = {k: v for k, v in row.items() if k not in drop}
    for f in ts_fields:
        if f in out:
            out[f] = naive_utc(out[f])
    return out


# ── stats ───────────────────────────────────────────────────────────────────


@dataclass
class TableStat:
    copied: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class MigrationStats:
    tables: dict[str, TableStat] = field(default_factory=dict)

    def t(self, name: str) -> TableStat:
        return self.tables.setdefault(name, TableStat())

    def dump(self) -> None:
        print("\nPER-TABLE RESULT")
        for name, st in self.tables.items():
            print(f"  {name:<28} copied={st.copied:<6} skipped={st.skipped}")
            for note in st.notes:
                print(f"      - {note}")


def upsert_chunked(sb: Client, table: str, rows: list[dict], stat: TableStat,
                   on_conflict: str = "id") -> None:
    for chunk in chunks(rows):
        sb.table(table).upsert(chunk, on_conflict=on_conflict).execute()
        stat.copied += len(chunk)
    print(f"[copy] {table}: {stat.copied} rows upserted, {stat.skipped} skipped")


# ── BYTEA handling (PostgREST returns bytea as \\x-hex or base64 strings) ───


def fetch_blob(client: Client, table: str, row_id: str):
    res = client.table(table).select("file_data").eq("id", row_id).limit(1).execute()
    if not res.data:
        fail(f"{table}: row {row_id} vanished while fetching file_data")
    return res.data[0]["file_data"]


def detect_codec(raw, expected: int, label: str) -> str:
    """One-row smoke test: figure out how bytea arrived over PostgREST."""
    diag: list[str] = []
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) == expected:
            return "raw"
        diag.append(f"raw bytes length {len(raw)}")
    if isinstance(raw, str):
        if raw.startswith("\\x"):
            try:
                n = len(bytes.fromhex(raw[2:]))
                if n == expected:
                    return "hex"
                diag.append(f"hex decodes to {n} bytes")
            except ValueError as exc:
                diag.append(f"hex decode failed: {exc}")
        try:
            n = len(base64.b64decode(raw, validate=True))
            if n == expected:
                return "base64"
            diag.append(f"base64 decodes to {n} bytes")
        except Exception as exc:  # binascii.Error and friends
            diag.append(f"base64 decode failed: {exc}")
    fail(
        f"BYTEA smoke test failed for {label}: expected {expected} bytes; "
        + "; ".join(diag)
        + f"; value prefix {str(raw)[:48]!r}"
    )
    raise AssertionError  # unreachable — fail() exits


def decode_blob(raw, codec: str) -> bytes:
    if codec == "raw":
        return bytes(raw)
    if codec == "hex":
        return bytes.fromhex(str(raw)[2:])
    return base64.b64decode(str(raw))


def smoke_test(client: Client, table: str, meta_rows: list[dict], label: str) -> str | None:
    if not meta_rows:
        print(f"[smoke] {label}: no rows — nothing to test")
        return None
    sample = meta_rows[0]
    raw = fetch_blob(client, table, sample["id"])
    codec = detect_codec(raw, sample["file_size"], label)
    print(
        f"[smoke] {label}: encoding={codec}, decoded length matches file_size "
        f"({sample['file_size']} bytes, row {sample['id']})"
    )
    return codec


def upload_and_verify(path: str, data: bytes, content_type: str | None) -> None:
    upload_file(path, data, content_type or "application/octet-stream", upsert=True)
    echo = download_file(path)
    if hashlib.sha256(echo).digest() != hashlib.sha256(data).digest():
        fail(f"SHA256 mismatch after upload: {path}")


# ── project decision engine ─────────────────────────────────────────────────


@dataclass
class ProjectDecision:
    source: dict
    kind: str                       # MATCH | CREATE | REGISTRY | MATCH-NO-ENROLL
    target_project_id: str | None = None
    registry_id: str | None = None
    proposed_number: str | None = None
    enroll: bool = False
    notes: list[str] = field(default_factory=list)


def decide_projects(
    src_projects: list[dict],
    bdr_projects: list[dict],
    existing_details: set[str],
    registry_rows: list[dict],
    blockers: list[str],
    skip_norms: set[str] = frozenset(),
) -> tuple[list[ProjectDecision], dict[str, tuple[str, str]]]:
    """Classify every source project and build source_id -> (kind, target_id)."""
    bdr_by_norm = {norm(p["number"]): p for p in bdr_projects}
    used_norms = set(bdr_by_norm)
    reg_by_name = {norm(r["raw_name"]): r["id"] for r in registry_rows}
    reg_by_num = {
        norm(r["raw_number"]): r["id"] for r in registry_rows if norm(r.get("raw_number"))
    }

    dupes = Counter(
        norm(p["project_number"]) for p in src_projects if norm(p["project_number"])
    )
    for number, n in sorted(dupes.items()):
        if n > 1:
            titles = [p["project_title"] for p in src_projects
                      if norm(p["project_number"]) == number]
            blockers.append(
                f"duplicate normalized project number {number!r} across {n} source "
                f"projects: {titles} — reconcile in the source before migrating"
            )

    decisions: list[ProjectDecision] = []
    project_map: dict[str, tuple[str, str]] = {}
    ordered = sorted(src_projects, key=lambda p: (p["project_number"] or "~", p["project_title"]))
    for p in ordered:
        n = norm(p["project_number"])
        is_pw = p["project_group"] == "prevailing_wage"
        skipped = bool(n) and n in skip_norms
        if skipped:
            # Operator-excluded (junk/test rows): registry entry, never a project.
            is_pw = False
            n = ""  # fall through to the REGISTRY branch below
        if n and n in bdr_by_norm:
            row = bdr_by_norm[n]
            kind = "MATCH" if is_pw else "MATCH-NO-ENROLL"
            d = ProjectDecision(p, kind, target_project_id=row["id"], enroll=is_pw)
            if is_pw and row["id"] in existing_details:
                d.notes.append("cp_details already exists — will be left untouched")
            if is_pw and row.get("cp_enrolled_at"):
                d.notes.append("already enrolled — enrollment timestamps kept")
            project_map[p["id"]] = ("project", row["id"])
        elif is_pw:
            raw = (p["project_number"] or "").strip()
            if raw and norm(raw) not in used_norms:
                number = raw
            else:
                number = f"CP-{uuid.UUID(p['id']).hex[:8]}"
            used_norms.add(norm(number))
            d = ProjectDecision(p, "CREATE", target_project_id=p["id"],
                                proposed_number=number, enroll=True)
            if number != raw:
                d.notes.append("synthetic number (permanently retired by the unique index)")
            project_map[p["id"]] = ("project", p["id"])
        else:
            name_key = norm(p["project_title"])
            num_key = norm(p["project_number"])
            existing = reg_by_name.get(name_key) or (reg_by_num.get(num_key) if num_key else None)
            if existing and existing != p["id"]:
                d = ProjectDecision(p, "REGISTRY", registry_id=existing)
                d.notes.append("collides with existing registry row — mapped, not inserted")
            else:
                d = ProjectDecision(p, "REGISTRY", registry_id=p["id"])
                reg_by_name[name_key] = p["id"]
                if num_key:
                    reg_by_num[num_key] = p["id"]
            if skipped:
                d.notes.append("excluded via CPR_SKIP_PROJECT_NUMBERS — no project created")
            project_map[p["id"]] = ("registry", d.registry_id)
        decisions.append(d)
    return decisions, project_map


def print_decisions(decisions: list[ProjectDecision], entries_by_project: Counter) -> None:
    print("\nPROJECT DECISIONS")
    for d in decisions:
        p = d.source
        number = p["project_number"] or "(no number)"
        count = entries_by_project.get(p["id"], 0)
        if d.kind == "MATCH":
            detail = f"MATCH {number}"
        elif d.kind == "MATCH-NO-ENROLL":
            detail = f"MATCH-NO-ENROLL {number}"
        elif d.kind == "CREATE":
            detail = f"CREATE {d.proposed_number}"
        else:
            detail = "REGISTRY"
        print(f"DECISION {number} / {p['project_title']} -> {detail} (time_entries={count})")
        for note in d.notes:
            print(f"    note: {note}")
    tally = Counter(d.kind for d in decisions)
    print(
        f"  totals: {len(decisions)} source projects — "
        + ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    )


# ── source auth (GoTrue admin) ──────────────────────────────────────────────


def fetch_source_auth_emails(url: str, service_key: str) -> dict[str, str]:
    """Source user_id -> email via the SOURCE project's GoTrue admin API."""
    out: dict[str, str] = {}
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    page = 1
    with httpx.Client(timeout=30) as client:
        while True:
            r = client.get(
                f"{url.rstrip('/')}/auth/v1/admin/users",
                params={"page": page, "per_page": 1000},
                headers=headers,
            )
            r.raise_for_status()
            payload = r.json()
            users = payload.get("users", payload if isinstance(payload, list) else [])
            for u in users:
                out[u["id"]] = u.get("email") or ""
            if len(users) < 1000:
                return out
            page += 1


# ── per-table transforms ────────────────────────────────────────────────────


def tx_employee(row: dict, actor: str | None, stat: TableStat) -> dict:
    out = strip_keys(row, drop=("user_id", "ssn_encrypted"), ts_fields=("created_at", "updated_at"))
    ssn = out.get("ssn_last_four")
    if ssn is not None and not re.fullmatch(r"\d{4}", str(ssn)):
        stat.notes.append(
            f"employee {row['first_name']} {row['last_name']}: ssn_last_four "
            f"not 4 digits — nulled"
        )
        out["ssn_last_four"] = None
    if out.get("employee_id") is not None and not str(out["employee_id"]).strip():
        out["employee_id"] = None
    out["created_by"] = actor
    return out


def tx_report(row: dict, actor: str | None) -> dict:
    return {
        "id": row["id"],
        "week_start_date": row["week_start_date"],
        "week_end_date": row["week_end_date"],
        "status": row["status"],
        "timesheet_filename": row.get("original_filename"),
        "payroll_detail_filename": row.get("payroll_detail_filename"),
        # storage paths stay NULL — the legacy app never persisted upload bytes
        "total_hours": row.get("total_hours"),
        "total_employees": row.get("total_employees"),
        "finalized_at": naive_utc(row.get("finalized_at")),
        "created_by": actor,
        "created_at": naive_utc(row.get("created_at")),
        "updated_at": naive_utc(row.get("updated_at")),
    }


def tx_time_entry(row: dict, project_map: dict[str, tuple[str, str]]) -> dict:
    # start_time / end_time copied VERBATIM: naive local wall-clock on purpose
    # (legacy CPR migration 020; the target columns are timestamp-without-tz).
    out = strip_keys(row, drop=("user_id",), ts_fields=("created_at",))
    mapping = project_map.get(row["project_id"]) if row.get("project_id") else None
    if mapping and mapping[0] == "project":
        out["project_id"] = mapping[1]
    else:
        out["project_id"] = None
        out["is_project_matched"] = False
    return out


def tx_detail_entry(row: dict) -> dict:
    # Column list comes from the source row itself (all ~55 numeric columns
    # ride along verbatim) — nothing hand-typed beyond the drops.
    return strip_keys(row, drop=("user_id",), ts_fields=("created_at",))


def tx_record(row: dict) -> dict:
    out = strip_keys(row, drop=("user_id",), ts_fields=("created_at",))
    out["created_by"] = None  # legacy auth uids don't map to BDR profiles
    return out


def details_payload(p: dict, target_project_id: str) -> dict:
    return {
        "project_id": target_project_id,
        "contract_id": p.get("contract_id") or "",
        "report_type": p.get("report_type"),
        "shift_type": p.get("shift_type") or "regular",
        "shift_start_time": p.get("shift_start_time"),
        "pwp_number": p.get("pwp_number"),
        "public_body_awarding_contract": p.get("public_body_awarding_contract"),
        "contractor_address_street": p.get("contractor_address_street"),
        "contractor_address_city": p.get("contractor_address_city"),
        "contractor_address_state": p.get("contractor_address_state"),
        "contractor_address_zip": p.get("contractor_address_zip"),
        "customer_name": p.get("customer"),
        "is_active": p.get("is_active", True),
    }


# ── blob table migrations (row-at-a-time to bound memory) ───────────────────


def migrate_employee_documents(
    src: Client, tgt: Client, docs_meta: list[dict], codec: str | None,
    actor: str | None, stats: MigrationStats,
) -> None:
    stat = stats.t("employee_documents")
    for meta in docs_meta:
        data = decode_blob(fetch_blob(src, "employee_documents", meta["id"]), codec)
        if len(data) != meta["file_size"]:
            fail(
                f"employee_documents {meta['id']}: decoded {len(data)} bytes, "
                f"file_size says {meta['file_size']}"
            )
        path = f"payroll/employees/{meta['employee_id']}/{meta['id']}-{safe_name(meta['filename'])}"
        upload_and_verify(path, data, meta.get("content_type"))
        tgt.table("employee_documents").upsert(
            {
                "id": meta["id"],
                "employee_id": meta["employee_id"],
                "category": meta["category"],
                "storage_path": path,
                "filename": meta["filename"],
                "mime_type": meta.get("content_type") or "application/octet-stream",
                "size_bytes": meta["file_size"],
                "uploaded_by": actor,
                "created_at": naive_utc(meta.get("created_at")),
            },
            on_conflict="id",
        ).execute()
        stat.copied += 1
    print(f"[copy] employee_documents: {stat.copied} files uploaded + verified (SHA256)")


def migrate_record_files(
    src: Client, tgt: Client, files_meta: list[dict], codec: str | None,
    report_by_record: dict[str, str], stats: MigrationStats,
) -> None:
    stat = stats.t("cp_record_files")
    for meta in files_meta:
        report_id = report_by_record.get(meta["record_id"])
        if report_id is None:
            fail(f"certified_payroll_files {meta['id']}: unknown record {meta['record_id']}")
        data = decode_blob(fetch_blob(src, "certified_payroll_files", meta["id"]), codec)
        if len(data) != meta["file_size"]:
            fail(
                f"certified_payroll_files {meta['id']}: decoded {len(data)} bytes, "
                f"file_size says {meta['file_size']}"
            )
        path = (
            f"payroll/reports/{report_id}/cpr/{meta['record_id']}/"
            f"{meta['id']}-{safe_name(meta['filename'])}"
        )
        upload_and_verify(path, data, meta.get("content_type"))
        tgt.table("cp_record_files").upsert(
            {
                "id": meta["id"],
                "record_id": meta["record_id"],
                "filename": meta["filename"],
                "content_type": meta.get("content_type") or "application/octet-stream",
                "storage_path": path,
                "size_bytes": meta["file_size"],
            },
            on_conflict="id",
        ).execute()
        stat.copied += 1
    print(f"[copy] cp_record_files: {stat.copied} files uploaded + verified (SHA256)")


# ── project decisions applied ───────────────────────────────────────────────


def apply_project_decisions(
    tgt: Client, decisions: list[ProjectDecision], existing_details: set[str],
    actor: str | None, stats: MigrationStats,
) -> None:
    proj_stat = stats.t("projects (CREATE)")
    det_stat = stats.t("cp_details")
    reg_stat = stats.t("cp_ignored_projects")
    now = datetime.now(timezone.utc).isoformat()
    for d in decisions:
        p = d.source
        if d.kind == "CREATE":
            tgt.table("projects").upsert(
                {
                    "id": p["id"],
                    "name": p["project_title"],
                    "number": d.proposed_number,
                    "current_stage": "cp_only",
                    "created_by": actor,
                },
                on_conflict="id",
            ).execute()
            proj_stat.copied += 1
        if d.kind == "REGISTRY":
            if d.registry_id != p["id"]:
                reg_stat.skipped += 1
                reg_stat.notes.append(
                    f"{p['project_title']!r} mapped to existing registry row {d.registry_id}"
                )
            else:
                raw_number = p.get("project_number")
                if raw_number is not None and not raw_number.strip():
                    raw_number = None
                tgt.table("cp_ignored_projects").upsert(
                    {
                        "id": p["id"],
                        "raw_number": raw_number,
                        "raw_name": p["project_title"],
                        "shift_type": p.get("shift_type") or "regular",
                        "note": "Imported from legacy CPR (regular project)",
                        "created_by": actor,
                    },
                    on_conflict="id",
                ).execute()
                reg_stat.copied += 1
            continue
        if not d.enroll:
            continue
        # Enroll: stamp timestamps ONLY where currently null (idempotent).
        tgt.table("projects").update(
            {"cp_enrolled_at": now, "cp_enrolled_by": actor}
        ).eq("id", d.target_project_id).is_("cp_enrolled_at", "null").execute()
        # cp_details: never overwrite an existing row.
        if d.target_project_id in existing_details:
            det_stat.skipped += 1
            det_stat.notes.append(
                f"CONFLICT cp_details exists for project {d.target_project_id} "
                f"(source {p['project_number'] or p['project_title']!r}) — left untouched"
            )
            continue
        tgt.table("cp_details").upsert(
            details_payload(p, d.target_project_id), on_conflict="project_id"
        ).execute()
        existing_details.add(d.target_project_id)
        det_stat.copied += 1
    print(
        f"[copy] projects: {proj_stat.copied} created; cp_details: {det_stat.copied} "
        f"inserted, {det_stat.skipped} conflicts skipped; registry: {reg_stat.copied} "
        f"inserted, {reg_stat.skipped} mapped to existing"
    )


# ── verification ────────────────────────────────────────────────────────────


@dataclass
class Check:
    label: str
    expected: object
    actual: object

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def run_verification(tgt: Client, expect: dict, project_map: dict) -> list[Check]:
    checks: list[Check] = []

    def tgt_count(table: str) -> int:
        # "*" not "id": cp_signer_profiles keys on profile_id, cp_settings on a
        # bool PK — a hardcoded id column 42703s on both.
        return count_exact(tgt.table(table).select("*", count="exact"))

    for src_table, tgt_table, n in expect["counts"]:
        checks.append(Check(f"{src_table} -> {tgt_table} row count", n, tgt_count(tgt_table)))

    hours = sum_decimal(
        r["total_hours"] for r in fetch_all(tgt, "cp_time_entries", "id,total_hours")
    )
    checks.append(Check("sum(time_entries.total_hours)", expect["sum_hours"], hours))

    detail_rows = fetch_all(tgt, "cp_payroll_detail_entries", "id,gross_pay_total,net_pay")
    checks.append(
        Check(
            "sum(detail.gross_pay_total)",
            expect["sum_gross"],
            sum_decimal(r["gross_pay_total"] for r in detail_rows),
        )
    )
    checks.append(
        Check("sum(detail.net_pay)", expect["sum_net"],
              sum_decimal(r["net_pay"] for r in detail_rows))
    )

    matched = count_exact(
        tgt.table("cp_time_entries").select("id", count="exact").not_.is_("project_id", "null")
    )
    checks.append(
        Check("matched time entries (source matched - registry-remapped)",
              expect["expected_matched"], matched)
    )

    tgt_project_ids = {p["id"] for p in fetch_all(tgt, "projects", "id")}
    tgt_registry_ids = {r["id"] for r in fetch_all(tgt, "cp_ignored_projects", "id")}
    missing = 0
    for kind, target_id in project_map.values():
        pool = tgt_project_ids if kind == "project" else tgt_registry_ids
        if target_id not in pool:
            missing += 1
    checks.append(Check("project decision targets present in target", 0, missing))

    detail_ids = {r["project_id"] for r in fetch_all(tgt, "cp_details", "id,project_id")}
    checks.append(
        Check("enrolled projects with cp_details row", 0,
              sum(1 for pid in expect["enrolled_ids"] if pid not in detail_ids))
    )

    checks.append(Check("cp_settings singleton rows", expect["settings_rows"],
                        tgt_count("cp_settings")))
    checks.append(Check("cp_signer_profiles rows", expect["signer_rows"],
                        tgt_count("cp_signer_profiles")))
    checks.append(Check("storage files uploaded == SHA256-verified",
                        expect["files_total"], expect["files_verified"]))
    return checks


def print_checks(checks: list[Check]) -> bool:
    print("\nVERIFICATION")
    ok = True
    for c in checks:
        status = "PASS" if c.ok else "FAIL"
        ok = ok and c.ok
        print(f"  [{status}] {c.label:<58} expected={c.expected} actual={c.actual}")
    return ok


# ── main ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="report only, no writes anywhere (default)")
    mode.add_argument("--execute", action="store_true", help="perform the migration")
    parser.add_argument("--org-settings-user", default="", metavar="USER_ID",
                        help="source user_id whose organization_settings row wins "
                             "(required only when the source has more than one row)")
    return parser.parse_args()


def resolve_actor(tgt: Client, email: str, blockers: list[str]) -> str | None:
    if not email:
        print("[plan] CPR_ACTOR_EMAIL unset — created_by / cp_enrolled_by / uploaded_by = NULL")
        return None
    profiles = fetch_all(tgt, "profiles", "id,email")
    for p in profiles:
        if norm(p["email"]) == norm(email):
            print(f"[plan] actor: {p['email']} -> profile {p['id']}")
            return p["id"]
    blockers.append(f"CPR_ACTOR_EMAIL {email!r} does not match any BDR profile")
    return None


def check_enums(src_data: dict, blockers: list[str]) -> None:
    scans = [
        ("payroll_reports.status", src_data["reports"], "status", STATUS_VALUES),
        ("projects.project_group", src_data["projects"], "project_group", PROJECT_GROUP_VALUES),
        ("projects.report_type", src_data["projects"], "report_type", REPORT_TYPE_VALUES),
        ("projects.shift_type", src_data["projects"], "shift_type", SHIFT_TYPE_VALUES),
        ("employee_documents.category", src_data["docs_meta"], "category", DOC_CATEGORY_VALUES),
    ]
    for label, rows, col, allowed in scans:
        values = {r[col] for r in rows if r.get(col) is not None}
        strangers = values - allowed
        if strangers:
            blockers.append(f"{label}: unknown enum values {sorted(strangers)}")
        else:
            print(f"[enum] {label}: {sorted(values) or ['(no values)']} — OK")


def preflight(src_data: dict, tgt: Client, args: argparse.Namespace,
              blockers: list[str]) -> dict | None:
    """Cross-row collision checks. Returns the surviving org-settings row (or None)."""
    # employee_id must be unique on lower(btrim()) company-wide in the target.
    emp_ids = Counter(
        norm(e["employee_id"]) for e in src_data["employees"] if norm(e.get("employee_id"))
    )
    for key, n in sorted(emp_ids.items()):
        if n > 1:
            blockers.append(f"employee_id {key!r} appears {n} times in source employees")
    tgt_emps = fetch_all(tgt, "employees", "id,employee_id")
    src_by_id = {e["id"] for e in src_data["employees"]}
    for te in tgt_emps:
        key = norm(te.get("employee_id"))
        if key and key in emp_ids and te["id"] not in src_by_id:
            blockers.append(
                f"target already has a different employee with employee_id {key!r} ({te['id']})"
            )

    # One report per week, company-wide (target unique index).
    weeks = Counter(r["week_start_date"] for r in src_data["reports"])
    for week, n in sorted(weeks.items()):
        if n > 1:
            rows = [(r["id"], r["user_id"]) for r in src_data["reports"]
                    if r["week_start_date"] == week]
            blockers.append(
                f"duplicate week_start_date {week} across source user_ids — "
                f"reconcile before migrating: {rows}"
            )
    tgt_weeks = {r["week_start_date"]: r["id"]
                 for r in fetch_all(tgt, "cp_payroll_reports", "id,week_start_date")}
    for r in src_data["reports"]:
        existing = tgt_weeks.get(r["week_start_date"])
        if existing and existing != r["id"]:
            blockers.append(
                f"target already has a different report for week {r['week_start_date']} "
                f"({existing})"
            )

    # organization_settings -> cp_settings singleton.
    org_rows = src_data["org_rows"]
    chosen: dict | None = None
    if len(org_rows) == 1:
        chosen = org_rows[0]
    elif len(org_rows) > 1:
        if args.org_settings_user:
            picks = [r for r in org_rows if r["user_id"] == args.org_settings_user]
            if picks:
                chosen = picks[0]
            else:
                blockers.append(
                    f"--org-settings-user {args.org_settings_user!r} matches no source row"
                )
        else:
            listing = [(r["id"], r["user_id"], r.get("name")) for r in org_rows]
            blockers.append(
                "organization_settings has multiple rows — pick one with "
                f"--org-settings-user=<user_id>: {listing}"
            )
    if chosen:
        print(f"[plan] cp_settings source row: {chosen['id']} (name={chosen.get('name')!r})")
    else:
        print(f"[plan] organization_settings rows: {len(org_rows)}")
    return chosen


def plan_signers(src_data: dict, tgt: Client, cpr_env: CprEnv) -> list[tuple[dict, str | None]]:
    """Pair each source user_profile with a BDR profile id (or None -> skip)."""
    profiles = src_data["user_profiles"]
    if not profiles:
        print("[plan] user_profiles: none")
        return []
    emails = fetch_source_auth_emails(
        cpr_env.cpr_supabase_url, cpr_env.cpr_supabase_service_role_key
    )
    bdr_by_email = {norm(p["email"]): p["id"] for p in fetch_all(tgt, "profiles", "id,email")}
    plan: list[tuple[dict, str | None]] = []
    seen: set[str] = set()
    for up in profiles:
        email = emails.get(up["user_id"], "")
        pid = bdr_by_email.get(norm(email)) if email else None
        if pid in seen:
            print(f"[signer] {up['user_id']} -> {email} -> profile {pid} ALREADY TAKEN — skip")
            pid = None
        elif pid:
            seen.add(pid)
            print(f"[signer] {up['user_id']} -> {email} -> BDR profile {pid}")
        else:
            print(f"[signer] {up['user_id']} -> {email or '(no auth email)'} -> NO MATCH — skip")
        plan.append((up, pid))
    return plan


def main() -> None:
    args = parse_args()
    execute = args.execute
    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"=== CPR -> BDR data migration [{mode}] ===")

    cpr_env = CprEnv()
    settings = get_settings()

    # ── safety guard: the target must be the BDR dev project ──
    if BDR_PROD_REF in (settings.supabase_url or ""):
        fail(
            "target SUPABASE_URL points at BDR_Prod (zxqkqotlcgbalwoqeeca). "
            "This script may only run against the BDR dev project."
        )
    if settings.environment == "production":
        fail("settings.environment == 'production' — refusing to run against production.")
    if not cpr_env.cpr_supabase_url or not cpr_env.cpr_supabase_service_role_key:
        fail("CPR_SUPABASE_URL and CPR_SUPABASE_SERVICE_ROLE_KEY must be set (bdr_be/.env)")
    if norm(cpr_env.cpr_supabase_url).rstrip("/") == norm(settings.supabase_url).rstrip("/"):
        fail("source and target are the same Supabase project")
    print(f"[plan] source: {cpr_env.cpr_supabase_url}")
    print(f"[plan] target: {settings.supabase_url}")

    src = create_client(cpr_env.cpr_supabase_url, cpr_env.cpr_supabase_service_role_key)
    tgt = get_supabase()

    # 0063/0064 must be applied.
    for table in ("cp_details", "cp_payroll_reports", "cp_ignored_projects",
                  "cp_signer_profiles", "employees"):
        try:
            tgt.table(table).select("*").limit(1).execute()
        except Exception as exc:
            fail(f"target table {table!r} unavailable — are 0063/0064 applied? ({exc})")

    blockers: list[str] = []
    actor = resolve_actor(tgt, cpr_env.cpr_actor_email.strip(), blockers)

    # ── fetch the whole source (metadata only for the BYTEA tables) ──
    src_data = {
        "classifications": fetch_all(src, "classifications"),
        "rates": fetch_all(src, "rates"),
        "employees": fetch_all(src, "employees"),
        "docs_meta": fetch_all(
            src, "employee_documents",
            "id,employee_id,filename,content_type,file_size,category,created_at",
        ),
        "projects": fetch_all(src, "projects"),
        "reports": fetch_all(src, "payroll_reports"),
        "entries": fetch_all(src, "time_entries"),
        "details": fetch_all(src, "payroll_detail_entries"),
        "records": fetch_all(src, "certified_payroll_records"),
        "files_meta": fetch_all(
            src, "certified_payroll_files", "id,record_id,filename,content_type,file_size"
        ),
        "org_rows": fetch_all(src, "organization_settings"),
        "user_profiles": fetch_all(src, "user_profiles"),
        "prp_rows": fetch_all(src, "payroll_report_projects", "id"),
    }
    print("\nSOURCE INVENTORY")
    for name, rows in src_data.items():
        print(f"  {name:<18} {len(rows)}")
    print(
        f"[plan] payroll_report_projects: {len(src_data['prp_rows'])} source rows — "
        "intentionally NOT migrated (dead table)"
    )
    ssn_count = sum(1 for e in src_data["employees"] if e.get("ssn_encrypted"))
    print(f"[plan] employees.ssn_encrypted non-null values DISCARDED by migration: {ssn_count}")

    # ── preflights (read-only; run in both modes) ──
    check_enums(src_data, blockers)
    chosen_org = preflight(src_data, tgt, args, blockers)
    doc_codec = smoke_test(src, "employee_documents", src_data["docs_meta"],
                           "employee_documents.file_data")
    file_codec = smoke_test(src, "certified_payroll_files", src_data["files_meta"],
                            "certified_payroll_files.file_data")

    # ── project decision engine ──
    bdr_projects = fetch_all(tgt, "projects", "id,name,number,current_stage,cp_enrolled_at")
    existing_details = {
        d["project_id"] for d in fetch_all(tgt, "cp_details", "id,project_id")
    }
    registry_rows = fetch_all(tgt, "cp_ignored_projects", "id,raw_name,raw_number")
    skip_norms = {
        norm(x) for x in cpr_env.cpr_skip_project_numbers.split(",") if norm(x)
    }
    if skip_norms:
        print(f"[plan] CPR_SKIP_PROJECT_NUMBERS excludes: {sorted(skip_norms)}")
    decisions, project_map = decide_projects(
        src_data["projects"], bdr_projects, existing_details, registry_rows, blockers,
        skip_norms=skip_norms,
    )
    entries_by_project = Counter(
        e["project_id"] for e in src_data["entries"] if e.get("project_id")
    )
    print_decisions(decisions, entries_by_project)
    unmatched_entries = sum(1 for e in src_data["entries"] if not e.get("project_id"))
    print(f"[plan] source time_entries with no project: {unmatched_entries}")

    signer_plan = plan_signers(src_data, tgt, cpr_env)

    # ── expectations shared by the dry-run preview and post-execute verification ──
    src_matched = sum(1 for e in src_data["entries"] if e.get("project_id"))
    registry_remapped = sum(
        1 for e in src_data["entries"]
        if e.get("project_id") and project_map.get(e["project_id"], ("", ""))[0] == "registry"
    )
    expect = {
        "counts": [
            ("classifications", "cp_classifications", len(src_data["classifications"])),
            ("rates", "cp_rates", len(src_data["rates"])),
            ("employees", "employees", len(src_data["employees"])),
            ("employee_documents", "employee_documents", len(src_data["docs_meta"])),
            ("payroll_reports", "cp_payroll_reports", len(src_data["reports"])),
            ("time_entries", "cp_time_entries", len(src_data["entries"])),
            ("payroll_detail_entries", "cp_payroll_detail_entries", len(src_data["details"])),
            ("certified_payroll_records", "cp_records", len(src_data["records"])),
            ("certified_payroll_files", "cp_record_files", len(src_data["files_meta"])),
        ],
        "sum_hours": sum_decimal(e["total_hours"] for e in src_data["entries"]),
        "sum_gross": sum_decimal(d.get("gross_pay_total") for d in src_data["details"]),
        "sum_net": sum_decimal(d.get("net_pay") for d in src_data["details"]),
        "expected_matched": src_matched - registry_remapped,
        "enrolled_ids": [d.target_project_id for d in decisions if d.enroll],
        "settings_rows": 1 if chosen_org else 0,
        "signer_rows": sum(1 for _, pid in signer_plan if pid),
        "files_total": len(src_data["docs_meta"]) + len(src_data["files_meta"]),
        "files_verified": 0,  # filled during execute
    }

    if not execute:
        print("\nDRY-RUN VERIFICATION PREVIEW (source-side)")
        for src_table, tgt_table, n in expect["counts"]:
            print(f"  {src_table:<28} -> {tgt_table:<28} rows={n}")
        print(f"  sum(time_entries.total_hours)              = {expect['sum_hours']}")
        print(f"  sum(detail.gross_pay_total)                = {expect['sum_gross']}")
        print(f"  sum(detail.net_pay)                        = {expect['sum_net']}")
        print(f"  matched entries {src_matched} - registry-remapped {registry_remapped} "
              f"-> expected target matched = {expect['expected_matched']}")
        print(f"  storage files to migrate                   = {expect['files_total']}")
        if blockers:
            print("\nBLOCKERS (must be resolved before --execute):")
            for b in blockers:
                print(f"  - {b}")
            sys.exit(1)
        print("\nDry-run complete — no blockers. Nothing was written.")
        return

    if blockers:
        print("\nBLOCKERS — refusing to execute:")
        for b in blockers:
            print(f"  - {b}")
        sys.exit(1)

    # ── EXECUTE (FK order) ──
    stats = MigrationStats()
    upsert_chunked(
        tgt, "cp_classifications",
        [strip_keys(r, (), ("created_at", "updated_at")) for r in src_data["classifications"]],
        stats.t("cp_classifications"),
    )
    upsert_chunked(
        tgt, "cp_rates",
        [strip_keys(r, (), ("created_at", "updated_at")) for r in src_data["rates"]],
        stats.t("cp_rates"),
    )
    emp_stat = stats.t("employees")
    upsert_chunked(
        tgt, "employees",
        [tx_employee(r, actor, emp_stat) for r in src_data["employees"]], emp_stat,
    )
    migrate_employee_documents(src, tgt, src_data["docs_meta"], doc_codec, actor, stats)
    apply_project_decisions(tgt, decisions, existing_details, actor, stats)
    upsert_chunked(
        tgt, "cp_payroll_reports",
        [tx_report(r, actor) for r in src_data["reports"]], stats.t("cp_payroll_reports"),
    )
    upsert_chunked(
        tgt, "cp_time_entries",
        [tx_time_entry(r, project_map) for r in src_data["entries"]],
        stats.t("cp_time_entries"),
    )
    upsert_chunked(
        tgt, "cp_payroll_detail_entries",
        [tx_detail_entry(r) for r in src_data["details"]],
        stats.t("cp_payroll_detail_entries"),
    )
    upsert_chunked(
        tgt, "cp_records", [tx_record(r) for r in src_data["records"]], stats.t("cp_records")
    )
    report_by_record = {r["id"]: r["payroll_report_id"] for r in src_data["records"]}
    migrate_record_files(src, tgt, src_data["files_meta"], file_codec, report_by_record, stats)

    if chosen_org:
        tgt.table("cp_settings").upsert(
            {
                "id": True,
                "name": chosen_org.get("name"),
                "street_address": chosen_org.get("street_address"),
                "city": chosen_org.get("city"),
                "state": chosen_org.get("state"),
                "zip_code": chosen_org.get("zip_code"),
                "phone": chosen_org.get("phone"),
                "license_number": chosen_org.get("license_number"),
            },
            on_conflict="id",
        ).execute()
        stats.t("cp_settings").copied = 1
        print("[copy] cp_settings: singleton upserted")

    signer_stat = stats.t("cp_signer_profiles")
    for up, pid in signer_plan:
        if not pid:
            signer_stat.skipped += 1
            continue
        tgt.table("cp_signer_profiles").upsert(
            {
                "profile_id": pid,
                "first_name": up.get("first_name"),
                "last_name": up.get("last_name"),
                "job_title": up.get("job_title"),
                "personal_email": up.get("personal_email"),
                "date_of_birth": up.get("date_of_birth"),
                "profile_completed": up.get("profile_completed", False),
            },
            on_conflict="profile_id",
        ).execute()
        signer_stat.copied += 1
    print(
        f"[copy] cp_signer_profiles: {signer_stat.copied} upserted, "
        f"{signer_stat.skipped} skipped (no BDR profile match)"
    )

    stats.dump()

    # Every uploaded file was SHA256-verified inline (upload_and_verify fails hard
    # on the first mismatch), so uploaded == verified here by construction.
    expect["files_verified"] = (
        stats.t("employee_documents").copied + stats.t("cp_record_files").copied
    )
    checks = run_verification(tgt, expect, project_map)
    if not print_checks(checks):
        print("\nVERIFICATION FAILED — see FAIL lines above.", file=sys.stderr)
        sys.exit(1)
    print("\nMigration complete — all verification checks passed.")


if __name__ == "__main__":
    main()
