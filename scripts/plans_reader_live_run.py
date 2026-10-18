"""Drive one live AI Estimating run end to end against a running backend.

Usage (from bdr_be, with a backend started on PORT with PLANS_READER_ENABLED=true,
FULL_SELF_HOSTED_LLMS_ENABLED=false and LLM_QUEUE_ENABLED=true):

    uv run python scripts/plans_reader_live_run.py --project-prefix dc0cb2f0 \
        --base-url http://127.0.0.1:5061 --out /path/to/out_dir [--labeled] [--render-mode adaptive]

What it does:
1. Signs in as the dev E2E account (password grant; the password is set through
   the service role for the duration of the run and scrambled afterwards).
2. Creates a run named after the backup project, uploads that project's drawing,
   electrical drawing, specification, addendum and other PDFs from the local
   production backup (never from the live production project), with labels when
   --labeled is given (electrical_drawing -> electrical_drawings, specification ->
   specifications, addendum -> addenda, other -> other; plain drawings stay
   unlabeled so they go through the splitter).
3. Prints the cost estimate, starts the run and polls until it finishes.
4. Downloads every deliverable, scores the BOQ against the estimator's real BOQ
   from the backup with scripts/plans_reader_golden.py, and writes
   summary.json (tokens, cost, timings, scores) plus the raw run payload.

Nothing here touches production. The backup is read only.
"""

from __future__ import annotations

import argparse
import functools
import json
import secrets
import signal
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
E2E_USER_ID = "d4ebc8b9-2bac-4273-a00e-19ac2896d5cc"
E2E_EMAIL = "bdr.e2e.electrical@g3test.dev"
INPUT_CATEGORIES = ("drawing", "electrical_drawing", "specification", "addendum", "other")
LABELS = {
    "electrical_drawing": "electrical_drawings",
    "specification": "specifications",
    "addendum": "addenda",
    "other": "other",
}


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return env


class _Auth:
    """Password-grant session for the E2E account. Access tokens live one
    hour and a run can take longer, so `headers()` refreshes the token when
    it is within five minutes of expiry, and `retry_401()` refreshes after
    an unexpected 401. The password is scrambled by the caller afterwards."""

    def __init__(self, env: dict[str, str]) -> None:
        from supabase import create_client

        self._url = env["SUPABASE_URL"]
        self._anon = (
            env.get("SUPABASE_ANON_KEY") or env.get("SUPABASE_PUBLISHABLE_KEY")
            or env.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        )
        sb = create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
        pw = secrets.token_urlsafe(24)
        sb.auth.admin.update_user_by_id(E2E_USER_ID, {"password": pw})
        self._grant("password", {"email": E2E_EMAIL, "password": pw})

    def _grant(self, grant_type: str, body: dict) -> None:
        r = httpx.post(
            f"{self._url}/auth/v1/token?grant_type={grant_type}",
            headers={"apikey": self._anon, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        self.access = data["access_token"]
        self.refresh = data["refresh_token"]
        self.expires_at = time.time() + int(data.get("expires_in") or 3600)

    def headers(self) -> dict[str, str]:
        if time.time() > self.expires_at - 300:
            self._grant("refresh_token", {"refresh_token": self.refresh})
        return {"Authorization": f"Bearer {self.access}"}

    def retry_401(self, client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
        r = client.request(method, url, headers=self.headers(), **kw)
        if r.status_code == 401:
            self._grant("refresh_token", {"refresh_token": self.refresh})
            r = client.request(method, url, headers=self.headers(), **kw)
        return r


def _scramble(env: dict[str, str]) -> None:
    from supabase import create_client

    sb = create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    sb.auth.admin.update_user_by_id(E2E_USER_ID, {"password": secrets.token_urlsafe(32)})


def _project(prefix: str) -> tuple[dict, list[dict]]:
    projects = json.loads((BACKUP / "data/projects.json").read_text())
    files = json.loads((BACKUP / "data/project_files.json").read_text())
    proj = next(p for p in projects if p["id"].startswith(prefix))
    rows = [
        f for f in files
        if f["project_id"] == proj["id"] and f["category"] in INPUT_CATEGORIES
        and f["filename"].lower().endswith(".pdf")
        and (BACKUP / "storage/project-files" / f["storage_path"]).exists()
    ]
    return proj, rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-prefix", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:5061")
    ap.add_argument("--out", required=True)
    ap.add_argument("--labeled", action="store_true", help="pre-label files by their project category")
    ap.add_argument("--render-mode", default="adaptive", choices=("adaptive", "tiles_260"))
    ap.add_argument("--poll-seconds", type=float, default=10)
    ap.add_argument("--max-minutes", type=float, default=90)
    ap.add_argument("--skip-specs", action="store_true")
    ap.add_argument("--run-id", default=None,
                    help="attach to an existing run: skip create, upload, estimate and start; poll and collect")
    args = ap.parse_args(argv)
    # Unbuffered progress when redirected to a file, and a SIGTERM still
    # runs the finally block (summary written, E2E password scrambled).
    global print
    print = functools.partial(print, flush=True)  # noqa: A001
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    proj, rows = _project(args.project_prefix)
    if args.skip_specs:
        rows = [r for r in rows if r["category"] != "specification"]
    print(f"project {proj['number']} {proj['name']}: {len(rows)} input files")

    auth = _Auth(env)
    base = args.base_url.rstrip("/") + "/ai-estimating"
    client = httpx.Client(timeout=600)
    summary: dict = {"project": proj["number"], "name": proj["name"], "files": [], "timeline": {}}
    t0 = time.time()
    try:
        status = auth.retry_401(client, "GET", f"{base}/status").json()
        print("status", json.dumps(status)[:400])
        if args.run_id:
            run_id = args.run_id
            print("attached to run", run_id)
            rows = []
        else:
            run = auth.retry_401(
                client, "POST", f"{base}/runs",
                json={"name": f"GOLDEN {proj['number']} {proj['name']}"[:120], "source": "upload", "render_mode": args.render_mode},
            )
            run.raise_for_status()
            run_id = run.json()["id"] if "id" in run.json() else run.json()["run"]["id"]
            print("run", run_id)
        h = auth.headers()
        for f in rows:
            path = BACKUP / "storage/project-files" / f["storage_path"]
            data = {}
            if args.labeled and f["category"] in LABELS:
                data["label"] = LABELS[f["category"]]
            with path.open("rb") as fh:
                r = client.post(f"{base}/runs/{run_id}/files", headers=h, data=data,
                                files={"file": (f["filename"], fh, "application/pdf")})
            ok = r.status_code < 300
            print(f"  upload {f['category']:20s} {f['filename'][:60]:60s} {r.status_code}")
            summary["files"].append({"filename": f["filename"], "category": f["category"], "status": r.status_code,
                                     "error": None if ok else r.text[:300]})
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "10")))
                with path.open("rb") as fh:
                    r = client.post(f"{base}/runs/{run_id}/files", headers=h, data=data,
                                    files={"file": (f["filename"], fh, "application/pdf")})
                summary["files"][-1]["status"] = r.status_code
        summary["timeline"]["uploaded_s"] = round(time.time() - t0)
        if not args.run_id:
            est = auth.retry_401(client, "GET", f"{base}/runs/{run_id}/estimate")
            print("estimate", est.status_code, est.text[:600])
            summary["estimate"] = est.json() if est.status_code < 300 else {"error": est.text[:600]}
            st = auth.retry_401(client, "POST", f"{base}/runs/{run_id}/start")
            print("start", st.status_code, st.text[:400])
            if st.status_code >= 300:
                summary["start_error"] = st.text[:600]
                return 2
        summary["timeline"]["started_s"] = round(time.time() - t0)
        last = None
        while time.time() - t0 < args.max_minutes * 60:
            time.sleep(args.poll_seconds)
            p = auth.retry_401(client, "GET", f"{base}/runs/{run_id}")
            if p.status_code >= 300:
                print("poll", p.status_code, p.text[:200])
                continue
            payload = p.json()
            run_row = payload.get("run", payload)
            groups = payload.get("groups", [])
            line = (f"{run_row.get('status'):16s} groups {sum(1 for g in groups if g.get('status') == 'done')}/{len(groups)} "
                    f"tokens in {run_row.get('input_tokens')} out {run_row.get('output_tokens')} cost {run_row.get('cost_usd')}")
            if line != last:
                print(f"[{round(time.time() - t0):5d}s] {line}")
                last = line
            if run_row.get("status") in ("done", "done_with_errors", "failed", "canceled", "over_cap"):
                break
        (out / "run_payload.json").write_text(json.dumps(payload, indent=1, default=str))
        summary["timeline"]["finished_s"] = round(time.time() - t0)
        summary["run"] = {k: run_row.get(k) for k in ("status", "input_tokens", "cached_tokens", "output_tokens",
                                                     "reasoning_tokens", "cost_usd", "estimated_cost_usd", "error",
                                                     "documents_total", "groups_total", "groups_done", "groups_failed")}
        summary["groups"] = [{k: g.get(k) for k in ("kind", "category", "group_index", "pass", "status", "est_input_tokens",
                                                       "input_tokens", "output_tokens", "cost_usd", "error")} for g in groups]
        summary["calls"] = payload.get("calls")
        # deliverables
        delivs = payload.get("deliverables", [])
        summary["deliverables"] = []
        for d in delivs:
            kind, ver = d["kind"], d["version"]
            dl = auth.retry_401(client, "GET", f"{base}/runs/{run_id}/deliverables/{kind}/{ver}/download")
            rec = {"kind": kind, "version": ver, "filename": d.get("filename"), "status": dl.status_code}
            if dl.status_code < 300:
                url = dl.json().get("url")
                blob = httpx.get(url, timeout=600)
                target = out / (d.get("filename") or f"{kind}_v{ver}")
                target.write_bytes(blob.content)
                rec["saved"] = str(target)
                rec["bytes"] = len(blob.content)
            summary["deliverables"].append(rec)
            if kind == "boq" and dl.status_code < 300:
                try:
                    from scripts.plans_reader_golden import (
                        find_backup_boq, match_rows, parse_estimator_boq, report_json, report_markdown, rows_from_spec,
                    )

                    est_path = find_backup_boq(args.project_prefix)
                    est_rows = parse_estimator_boq(Path(est_path).read_bytes())
                    # The run payload carries no spec_json; the rendered
                    # workbook has the estimator's layout, so parse that.
                    if d.get("spec_json"):
                        ai_rows = rows_from_spec(d["spec_json"])
                    else:
                        ai_rows = parse_estimator_boq(target.read_bytes())
                    rep = match_rows(ai_rows, est_rows)
                    (out / "boq_score.md").write_text(report_markdown(rep))
                    summary["boq_score"] = report_json(rep)
                    print(report_markdown(rep)[:3000])
                except Exception as exc:  # noqa: BLE001
                    summary["boq_score_error"] = repr(exc)[:500]
        # outputs
        for o in payload.get("outputs", []):
            scope = o.get("category") or "main"
            r = auth.retry_401(client, "GET", f"{base}/runs/{run_id}/outputs/{scope}")
            if r.status_code < 300:
                (out / f"output_{scope}.json").write_text(json.dumps(r.json(), indent=1, default=str))
        return 0
    finally:
        (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
        _scramble(env)
        print("E2E password scrambled; summary at", out / "summary.json")


if __name__ == "__main__":
    raise SystemExit(main())
