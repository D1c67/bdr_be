"""Browser v2 WI-1 sha256 manifest of the read-only paths
(docs/ASSISTANT_BROWSER_V2_PLAN.md 17.6 "Read only for every item", 17.7 gates).

`git diff` cannot prove these paths untouched in this tree (other builds keep
uncommitted work here), so every v2 wave gate compares file hashes against
docs/browser_v2_wi1_manifest.json instead.

  uv run python scripts/browser_v2_manifest.py check   # compare; exit 1 on any change
  uv run python scripts/browser_v2_manifest.py write   # write the baseline (WI-1 / orchestrator only)

A read-only entry that changed, vanished or appeared (a new file under a
covered directory) fails the check. Paths are relative to the BDR folder.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]            # the BDR folder
MANIFEST = ROOT / "bdr_be" / "docs" / "browser_v2_wi1_manifest.json"
SKIP_PARTS = {"__pycache__", "node_modules", ".venv", ".DS_Store", ".pytest_cache", ".ruff_cache"}

READ_ONLY: dict[str, list[str]] = {
    "org_worker": [
        "bdr_be/agent_worker/bdr_agent/browser/guard.py",
        "bdr_be/agent_worker/bdr_agent/agents/pm_apps",
        "bdr_be/agent_worker/bdr_agent/agents/rfi_from_email",
    ],
    "org_server": [
        "bdr_be/app/routers/agent_worker_rfi.py",
        "bdr_be/app/services/browser_agents/redaction.py",
    ],
    "phase7": [
        "bdr_be/app/assistant/proactive.py",
        "bdr_be/app/services/assistant_proactive.py",
        "bdr_be/app/services/assistant_delivery.py",
        "bdr_be/app/services/teams_alerts.py",
        "bdr_be/app/routers/assistant_inbox.py",
    ],
    "migrations": [f"bdr_be/supabase/migrations/{n}_*.sql" for n in ("0186", "0187", "0188", "0189", "0190", "0191")],
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
    entries: dict[str, dict] = {}
    for group, patterns in READ_ONLY.items():
        for pattern in patterns:
            for p in _files(pattern):
                entries[str(p.relative_to(ROOT))] = {"group": group, "sha256": _sha(p), "size": p.stat().st_size}
    return entries


def build() -> dict:
    return {
        "plan": "bdr_be/docs/ASSISTANT_BROWSER_V2_PLAN.md 17.6 read only, 17.7 gates (WI-1)",
        "generated_at": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds"),
        "root": "paths are relative to the BDR folder",
        "read_only": dict(sorted(current().items())),
    }


def check() -> int:
    old = json.loads(MANIFEST.read_text())["read_only"]
    now = current()
    changed = [r for r in old if r in now and now[r]["sha256"] != old[r]["sha256"]]
    missing = [r for r in old if r not in now]
    added = [r for r in now if r not in old]
    for label, rows in (("CHANGED", changed), ("MISSING", missing), ("ADDED  ", added)):
        for r in rows:
            print(f"{label}  {r}")
    bad = len(changed) + len(missing) + len(added)
    print(f"{len(old)} read-only entries, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "write":
        if MANIFEST.exists():
            raise SystemExit("manifest already exists; delete it deliberately to rewrite (it is the gate baseline)")
        data = build()
        MANIFEST.write_text(json.dumps(data, indent=1) + "\n")
        print(f"wrote {MANIFEST}: {len(data['read_only'])} read-only entries")
    elif cmd == "check":
        raise SystemExit(check())
    else:
        raise SystemExit(__doc__)
