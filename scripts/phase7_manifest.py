"""Phase 7 WI-1 sha256 manifest of the read-only and Phase 6 paths
(docs/ASSISTANT_PHASE7_PLAN.md 11.6 item 9, 12.3 "Read only", 12.4 wave 0).

`git diff` cannot prove these paths untouched in this tree (they are already
modified or untracked by other builds), so every wave gate compares file
hashes against docs/phase7_wi1_manifest.json instead.

  uv run python scripts/phase7_manifest.py check      # compare; exit 1 on a read-only change
  uv run python scripts/phase7_manifest.py write      # rewrite the manifest (WI-1 / orchestrator only)

A read-only entry that changed, vanished or appeared (a new file under a
covered directory) fails the check. The four "expected_to_change" entries
(rfq_inbox.py by WI-10, notification_email.py by WI-6, and
NotificationRoutingSettings.tsx and NotificationsBell.tsx by WI-7) are
reported, never failed: the gate reads the report and confirms the diff is no
wider than the edit the plan describes. When the owning item finishes, record
its result with `record-after <path>`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]            # the BDR folder
MANIFEST = ROOT / "bdr_be" / "docs" / "phase7_wi1_manifest.json"
URGENT = ROOT / "urgent"                              # the urgent-updates worktrees

SKIP_PARTS = {"__pycache__", "node_modules", ".venv", ".DS_Store", ".pytest_cache", ".ruff_cache"}

# group -> glob patterns relative to ROOT (a directory pattern takes every file under it).
READ_ONLY: dict[str, list[str]] = {
    "backend_services": [f"bdr_be/app/services/{n}.py" for n in (
        "notifications", "notification_routing", "teams_alerts", "graph_email", "email_branding",
        "rfp_test", "due_digest", "due_reminders", "calling_in", "workflow",
        "assistant_scheduler", "assistant_recipes",
    )] + ["bdr_be/app/services/auto_send*.py"],
    "backend_routers": [f"bdr_be/app/routers/{n}.py" for n in (
        "rfqs", "projects", "notes", "notification_routing")],
    "frontend": ["bdr_fe/components/TeamsAlertsSection.tsx"],
    "migrations": [f"bdr_be/supabase/migrations/{p}" for p in (
        "0177_*.sql", "0184_*.sql", "0186_*.sql", "0187_*.sql", "0188_*.sql", "0189_*.sql")],
    "phase6_backend": [
        "bdr_be/agent_worker", "bdr_be/dist/phase6-worker-*", "bdr_be/app/services/browser_agents",
        "bdr_be/app/services/assistant_browser*.py",
        "bdr_be/app/routers/browser_agents.py", "bdr_be/app/routers/assistant_browser.py",
        "bdr_be/app/routers/assistant_browser_views.py", "bdr_be/app/assistant/tools/browser.py",
    ],
    "phase6_frontend": [
        "bdr_fe/components/assistant/AssistantBrowser*.tsx", "bdr_fe/components/BrowserAgentSettings.tsx",
        "bdr_fe/components/browserAgents", "bdr_fe/lib/browserAgents*.ts", "bdr_fe/lib/assistantBrowser*.ts",
        "bdr_fe/lib/__tests__/browserAgents*.test.mjs", "bdr_fe/lib/__tests__/assistantBrowser*.test.mjs",
        "bdr_fe/app/(app)/assistant/browser", "bdr_fe/app/(app)/settings/browser-agents",
        "bdr_fe/app/(app)/settings/assistant/browser-inspection",
    ],
}

# path -> the one work item allowed to change it (11.6 item 9).
EXPECTED_TO_CHANGE: dict[str, str] = {
    "bdr_be/app/services/rfq_inbox.py": "WI-10",
    "bdr_be/app/services/notification_email.py": "WI-6",
    "bdr_fe/components/NotificationRoutingSettings.tsx": "WI-7",
    "bdr_fe/components/NotificationsBell.tsx": "WI-7",
}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _files(pattern: str) -> list[Path]:
    out: list[Path] = []
    for hit in sorted(ROOT.glob(pattern)):
        found = [p for p in hit.rglob("*") if p.is_file()] if hit.is_dir() else [hit]
        out += [p for p in found if not (set(p.parts) & SKIP_PARTS) and not p.name.endswith(".pyc")]
    return out


def current() -> dict[str, dict]:
    """rel path -> {sha256, size, group} for every covered file on disk."""
    entries: dict[str, dict] = {}
    for group, patterns in READ_ONLY.items():
        for pattern in patterns:
            for p in _files(pattern):
                entries[str(p.relative_to(ROOT))] = {"group": group, "sha256": _sha(p), "size": p.stat().st_size}
    return entries


def build() -> dict:
    entries = current()
    expected = []
    for rel, wi in EXPECTED_TO_CHANGE.items():
        p = ROOT / rel
        urgent = URGENT / rel
        expected.append({
            "path": rel,
            "status": "expected to change",
            "changed_by": wi,
            "sha256_before": _sha(p),
            "sha256_after": None,
            "urgent_tree_sha256": _sha(urgent) if urgent.is_file() else None,
            "note": "records WI-1's hash at build start; the gate fails if any other item changes it or the diff is wider than the plan's edit (4.5, 10.1, 10.2)",
        })
    return {
        "plan": "bdr_be/docs/ASSISTANT_PHASE7_PLAN.md 11.6 item 9, 12.3, 12.4 wave 0",
        "generated_at": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds"),
        "root": "paths are relative to the BDR folder",
        "read_only": {rel: e for rel, e in sorted(entries.items())},
        "expected_to_change": expected,
    }


def check() -> int:
    old = json.loads(MANIFEST.read_text())
    now = current()
    ro = old["read_only"]
    changed = [r for r in ro if r in now and now[r]["sha256"] != ro[r]["sha256"]]
    missing = [r for r in ro if r not in now]
    added = [r for r in now if r not in ro]
    for r in changed:
        print(f"CHANGED  {r}")
    for r in missing:
        print(f"MISSING  {r}")
    for r in added:
        print(f"ADDED    {r}")
    for e in old["expected_to_change"]:
        p = ROOT / e["path"]
        h = _sha(p) if p.is_file() else None
        state = "unchanged" if h == e["sha256_before"] else ("matches recorded after" if h == e["sha256_after"] else "CHANGED")
        print(f"expected ({e['changed_by']}) {state}  {e['path']}")
    bad = len(changed) + len(missing) + len(added)
    print(f"{len(ro)} read-only entries, {bad} problem(s)")
    return 1 if bad else 0


def record_after(rel: str) -> None:
    data = json.loads(MANIFEST.read_text())
    for e in data["expected_to_change"]:
        if e["path"] == rel:
            e["sha256_after"] = _sha(ROOT / rel)
            MANIFEST.write_text(json.dumps(data, indent=1) + "\n")
            print(f"recorded after-hash for {rel}")
            return
    raise SystemExit(f"{rel} is not an expected-to-change entry")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "write":
        if MANIFEST.exists():
            raise SystemExit("manifest already exists; delete it deliberately to rewrite (it is the gate baseline)")
        data = build()
        MANIFEST.write_text(json.dumps(data, indent=1) + "\n")
        print(f"wrote {MANIFEST}: {len(data['read_only'])} read-only + {len(data['expected_to_change'])} expected-to-change")
    elif cmd == "check":
        raise SystemExit(check())
    elif cmd == "record-after" and len(sys.argv) == 3:
        record_after(sys.argv[2])
    else:
        raise SystemExit(__doc__)
