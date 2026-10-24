"""scripts/eval_run.py: imports cleanly on top of plans_reader_live_run,
parses its arguments, resolves tiers and cases, refuses the holdout on a
dirty tree, names runs `EVAL <case>` with eval_case_id, and drives the
per-case flow through a fake backend. No network, no database, no model."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import eval_manifest as M

from tests import eval_fixtures as F

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_run.py"


def _load():
    if "eval_run" in sys.modules:
        return sys.modules["eval_run"]
    spec = importlib.util.spec_from_file_location("eval_run", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_run"] = mod
    spec.loader.exec_module(mod)
    return mod


er = _load()


def test_imports_the_live_run_script_pieces():
    from scripts import plans_reader_live_run as live

    assert er._Auth is live._Auth and er._project is live._project and er.LABELS is live.LABELS
    assert er.BACKUP == live.BACKUP and er.INPUT_CATEGORIES == live.INPUT_CATEGORIES


def test_parser_flags():
    ap = er.build_parser()
    args = ap.parse_args(["--tier", "smoke", "--workstream", "WS0", "--purpose", "smoke", "--budget", "45", "--repeat", "2",
                          "--no-cassette", "--dry-run", "--base-url", "http://127.0.0.1:5061"])
    assert args.tier == "smoke" and args.purpose == "smoke" and args.budget == Decimal("45") and args.repeat == 2
    assert args.no_cassette and args.dry_run and not args.no_start and not args.holdout_final
    args = ap.parse_args(["--cases", "7154,7177", "--sweep", "abc", "--no-start", "--holdout-final"])
    assert args.cases == "7154,7177" and args.sweep == "abc" and args.no_start and args.holdout_final
    with pytest.raises(SystemExit):
        ap.parse_args(["--tier", "pool"])
    with pytest.raises(SystemExit):
        ap.parse_args(["--purpose", "fun"])
    with pytest.raises(SystemExit):
        ap.parse_args(["--budget", "lots"])


def test_resolve_cases_by_tier_case_and_prefix():
    ap = er.build_parser()
    assert er.resolve_cases(ap.parse_args(["--tier", "smoke"])) == ["7154", "7177", "7172"]
    assert er.resolve_cases(ap.parse_args(["--tier", "core"])) == [c for c, _p, _n in M.TIER_CASES["core"]]
    assert er.resolve_cases(ap.parse_args(["--case", "7dfc64e6"])) == ["7154"]
    assert er.resolve_cases(ap.parse_args(["--cases", "7154, 7177", "--case", "7154"])) == ["7154", "7177"]
    with pytest.raises(SystemExit, match="unknown case"):
        er.resolve_cases(ap.parse_args(["--case", "9999"]))
    with pytest.raises(SystemExit, match="choose cases"):
        er.resolve_cases(ap.parse_args([]))


def test_holdout_needs_final_flag_and_clean_tree():
    ap = er.build_parser()
    with pytest.raises(SystemExit, match="holdout-final"):
        er.resolve_cases(ap.parse_args(["--tier", "holdout"]), dirty=False)
    with pytest.raises(SystemExit, match="clean"):
        er.resolve_cases(ap.parse_args(["--tier", "holdout", "--holdout-final"]), dirty=True)
    assert er.resolve_cases(ap.parse_args(["--tier", "holdout", "--holdout-final"]), dirty=False) == [c for c, _p, _n in M.TIER_CASES["holdout"]]
    with pytest.raises(SystemExit, match="holdout"):
        er.resolve_cases(ap.parse_args(["--case", "7188"]), dirty=False)


def test_tree_is_dirty_reads_git_status(monkeypatch, tmp_path):
    import subprocess

    class R:
        def __init__(self, out):
            self.stdout = out

    calls = []

    def fake(cmd, *a, **k):
        calls.append(cmd)
        return R(out)

    monkeypatch.setattr(subprocess, "run", fake)
    out = " M app/x.py\n"
    assert er.tree_is_dirty(tmp_path) is True
    # an untracked new module makes the tree dirty (the holdout must run on committed code)
    out = "?? app/services/new_module.py\n"
    assert er.tree_is_dirty(tmp_path) is True
    assert "--untracked-files=all" in calls[-1] and "--untracked-files=no" not in calls[-1]
    out = ""
    assert er.tree_is_dirty(tmp_path) is False

    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert er.tree_is_dirty(tmp_path) is True


def test_run_name_and_create_body():
    assert er.run_name("7154") == "EVAL 7154"
    body = er.create_run_body("7154", sweep_id="s1", render_mode="adaptive")
    assert body == {"name": "EVAL 7154", "source": "upload", "render_mode": "adaptive", "eval_case_id": "7154", "eval_sweep_id": "s1"}
    assert "eval_sweep_id" not in er.create_run_body("7177", sweep_id=None)


def test_case_inputs_from_backup_rows_use_the_label_map(tmp_path):
    backup = F.make_backup(tmp_path / "backup")
    (backup / "data" / "projects.json").write_text(json.dumps([{"id": F.PROJECT_ID, "number": "26.8.0001", "name": "Synthetic"}]))
    prefix = M.CASE_PREFIX["7154"]
    rows = json.loads((backup / "data" / "project_files.json").read_text())
    for r in rows:
        r["project_id"] = prefix + r["project_id"][8:]
    (backup / "data" / "project_files.json").write_text(json.dumps(rows))
    (backup / "data" / "projects.json").write_text(json.dumps([{"id": prefix + F.PROJECT_ID[8:], "number": "26.8.0001", "name": "Synthetic"}]))
    inputs = er.case_inputs("7154", backup=backup)
    assert [(i["category"], i["label"]) for i in inputs] == [("electrical_drawing", "electrical_drawings"), ("specification", "specifications")]
    assert all(len(i["sha256"]) == 64 and Path(i["path"]).is_file() for i in inputs)
    assert [i["category"] for i in er.case_inputs("7154", backup=backup, skip_specs=True)] == ["electrical_drawing"]


def test_sweep_remaining():
    rep = {"remaining_usd": Decimal("300"), "sweeps": [{"id": "s1", "budget_usd": Decimal("45"), "spent_usd": Decimal("10.5")}]}
    assert er.sweep_remaining(rep, "s1") == Decimal("34.5")
    assert er.sweep_remaining(rep, "other") == Decimal("300")
    assert er.sweep_remaining({}, None) is None


class FakeApi:
    def __init__(self, *, estimate=None, statuses=("reading", "done"), start_code=200, stamped_case=None):
        self.calls: list[tuple] = []
        self._estimate = estimate or {"cost_usd": 1.5, "cost_usd_max": 2.0}
        self._statuses = list(statuses)
        self._start_code = start_code
        self._stamped_case = stamped_case

    def status(self):
        return {"eval_ledger": {"enforced": True}}

    def create_run(self, body):
        self.calls.append(("create", dict(body)))
        return "run-1"

    def stamp_eval_ids(self, run_id, *, case_id, sweep_id):
        self.calls.append(("stamp", run_id, case_id, sweep_id))
        got = case_id if self._stamped_case is None else self._stamped_case
        return {"eval_case_id": got, "eval_sweep_id": sweep_id}

    def upload(self, run_id, path, filename, label):
        self.calls.append(("upload", run_id, filename, label))
        return 201

    def upload_quote(self, run_id, path, filename, category):
        self.calls.append(("quote", run_id, filename, category))
        return 201

    def estimate(self, run_id):
        self.calls.append(("estimate", run_id))
        return dict(self._estimate)

    def start(self, run_id):
        self.calls.append(("start", run_id))
        return self._start_code

    def get_run(self, run_id):
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return {"run": {"status": status, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1, "error": None}, "groups": []}


def _inputs(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(F.blank_pdf(1))
    return [{"filename": "Plans.pdf", "category": "electrical_drawing", "sha256": "1" * 64, "label": "electrical_drawings", "path": str(pdf)}]


def test_run_case_dry_run_stops_after_the_estimate(tmp_path):
    api = FakeApi()
    res = er.run_case(api, "7154", inputs=_inputs(tmp_path), sweep_id="s1", dry_run=True, log=lambda *_a: None)
    assert res.status == "estimated" and res.run_id == "run-1" and res.estimate["cost_usd_max"] == 2.0
    assert [c[0] for c in api.calls] == ["create", "stamp", "upload", "estimate"]
    assert api.calls[0][1]["name"] == "EVAL 7154" and api.calls[0][1]["eval_case_id"] == "7154"
    assert api.calls[1] == ("stamp", "run-1", "7154", "s1") and res.eval_ids == {"eval_case_id": "7154", "eval_sweep_id": "s1"}
    assert api.calls[2][3] == "electrical_drawings"
    assert res.uploads == [{"filename": "Plans.pdf", "category": "electrical_drawing", "label": "electrical_drawings", "status": 201}]


def test_run_case_no_start_uploads_only(tmp_path):
    api = FakeApi()
    res = er.run_case(api, "7154", inputs=_inputs(tmp_path), sweep_id=None, no_start=True, log=lambda *_a: None)
    assert res.status == "uploaded" and [c[0] for c in api.calls] == ["create", "stamp", "upload"]


def test_run_case_blind_guard_refuses_answer_files(tmp_path):
    api = FakeApi()
    inputs = _inputs(tmp_path)
    inputs[0]["filename"] = "BOQ - COUNTS.xlsx"
    with pytest.raises(M.BlindViolation):
        er.run_case(api, "7154", inputs=inputs, sweep_id="s1", dry_run=True, log=lambda *_a: None)
    assert api.calls == []
    quote = dict(_inputs(tmp_path)[0], filename="Vendor.pdf", category="quote")
    with pytest.raises(M.BlindViolation, match="quote"):
        er.run_case(api, "7154", inputs=[quote], sweep_id="s1", dry_run=True, log=lambda *_a: None)


def test_run_case_skips_when_the_estimate_exceeds_the_sweep(tmp_path):
    api = FakeApi(estimate={"cost_usd_max": 30.0})
    res = er.run_case(api, "7154", inputs=_inputs(tmp_path), sweep_id="s1", remaining_usd=Decimal("35"), log=lambda *_a: None)
    assert res.status == "skipped_budget" and "reserve" in res.error
    assert [c[0] for c in api.calls] == ["create", "stamp", "upload", "estimate"]


def test_run_case_polls_to_done_with_fake_clock(tmp_path):
    api = FakeApi(statuses=("reading", "consolidating", "done"))
    ticks = iter(range(0, 10_000, 5))
    res = er.run_case(api, "7154", inputs=_inputs(tmp_path), sweep_id="s1", remaining_usd=Decimal("40"),
                      clock=lambda: next(ticks), sleep=lambda s: None, poll_seconds=1, log=lambda *_a: None)
    assert res.status == "done" and res.run["status"] == "done" and res.timeline["finished_s"] >= 0
    assert [c[0] for c in api.calls][:5] == ["create", "stamp", "upload", "estimate", "start"]


def test_run_case_budget_refusal_is_budget_aborted(tmp_path):
    class Api(FakeApi):
        def get_run(self, run_id):
            return {"run": {"status": "failed", "error": "EvalBudgetRefused: sweep_budget"}, "groups": []}

    res = er.run_case(Api(), "7154", inputs=_inputs(tmp_path), sweep_id="s1", sleep=lambda s: None, log=lambda *_a: None)
    assert res.status == "budget_aborted"
    failed = er.run_case(FakeApi(start_code=409), "7154", inputs=_inputs(tmp_path), sweep_id="s1", log=lambda *_a: None)
    assert failed.status == "failed" and "409" in failed.error


def test_main_requires_a_sweep_for_live_runs(monkeypatch):
    monkeypatch.setattr(er, "tree_is_dirty", lambda *a, **k: False)
    with pytest.raises(SystemExit, match="--sweep"):
        er.main(["--case", "7154"])
    with pytest.raises(SystemExit, match="repeat"):
        er.main(["--case", "7154", "--dry-run", "--repeat", "0"])


def test_wait_post_run_chain_waits_for_review_and_revise_then_returns():
    import scripts.eval_run as er

    states = [
        {"groups": [{"kind": "modify", "status": "running"}]},
        {"groups": [{"kind": "modify", "status": "done"}, {"kind": "review", "status": "queued"}]},
        {"groups": [{"kind": "review", "status": "done"}, {"kind": "revise", "status": "running"}]},
        {"groups": [{"kind": "review", "status": "done"}, {"kind": "revise", "status": "done"}]},
    ]
    calls = []

    class Api:
        def get_run(self, run_id):
            calls.append(run_id)
            return states[min(len(calls) - 1, len(states) - 1)]

    t = [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s

    waited = er._wait_post_run_chain(Api(), "r1", clock=clock, sleep=sleep, poll_seconds=10, log=lambda m: None)
    assert len(calls) == 4 and waited == 30


def test_wait_post_run_chain_gives_up_after_the_grace_when_no_chain_appears():
    import scripts.eval_run as er

    class Api:
        def get_run(self, run_id):
            return {"groups": [{"kind": "main", "status": "done"}]}

    t = [0.0]
    waited = er._wait_post_run_chain(Api(), "r1", clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s), poll_seconds=30, log=lambda m: None)
    assert waited >= er.POST_RUN_GRACE_S and waited < er.POST_RUN_GRACE_S + 31


def test_export_and_score_reexports_into_the_same_bundle_and_rewrites_the_replay(monkeypatch, tmp_path):
    from types import SimpleNamespace

    src = F.make_bundle(tmp_path / "src", good=True)
    calls: list[Path] = []

    def fake_export(sb, run_id, out, *, case_id=None, settings=None, **_k):
        # what eval_bundle.export_run does to an existing directory: members replaced, directory kept
        out = Path(out)
        er.eval_bundle.import_weekend_export(src, out, case_id=case_id)
        calls.append(out)
        return out

    monkeypatch.setattr(er.eval_bundle, "export_run", fake_export)
    res = er.CaseResult(case_id="7154", run_id=F.RUN_ID, status="done")
    first = er.export_and_score(SimpleNamespace(), res, eval_root=tmp_path / "eval", backup=tmp_path / "nobackup")
    bundle = Path(first.bundle)
    assert bundle == tmp_path / "eval" / "bundles" / "7154" / F.RUN_ID[:8]
    marker = bundle / "replays" / "post_run" / "artifacts" / "stale_marker.json"
    marker.write_text("{}")
    second = er.export_and_score(SimpleNamespace(), er.CaseResult(case_id="7154", run_id=F.RUN_ID, status="done"),
                                       eval_root=tmp_path / "eval", backup=tmp_path / "nobackup")
    assert calls == [bundle, bundle] and second.bundle == str(bundle)
    assert not marker.exists() and (bundle / "replays" / "post_run" / "replay.json").is_file()
    assert len(second.acceptance) == 29


# Eval ids (GC4) and vendor quotes (E9) -------------------------------------------------------------


def _quotes(tmp_path):
    pdf = tmp_path / "q.pdf"
    pdf.write_bytes(F.blank_pdf(1, width=612, height=792))
    return [{"filename": "Acme Lighting Quote.pdf", "category": "quote", "material_category": "Lighting",
             "sha256": "2" * 64, "quote_id": "q1", "path": str(pdf), "in_manifest": True}]


@pytest.mark.parametrize("mode", ["dry_run", "no_start"])
def test_run_case_records_eval_ids_then_uploads_inputs_and_quotes(tmp_path, mode):
    api = FakeApi()
    res = er.run_case(api, "7154", inputs=_inputs(tmp_path), quotes=_quotes(tmp_path), sweep_id="s1",
                      log=lambda *_a: None, **{mode: True})
    kinds = [c[0] for c in api.calls]
    assert kinds[:4] == ["create", "stamp", "upload", "quote"]
    assert kinds[4:] == (["estimate"] if mode == "dry_run" else [])
    assert api.calls[3] == ("quote", "run-1", "Acme Lighting Quote.pdf", "Lighting")
    assert res.quotes == [{"filename": "Acme Lighting Quote.pdf", "material_category": "Lighting", "in_manifest": True, "status": 201}]
    assert res.to_dict()["eval_ids"]["eval_case_id"] == "7154" and res.to_dict()["quotes"] == res.quotes


def test_run_case_quotes_upload_before_start_on_a_live_run(tmp_path):
    api = FakeApi(statuses=("done",))
    ticks = iter(range(0, 10_000, 5))
    er.run_case(api, "7154", inputs=_inputs(tmp_path), quotes=_quotes(tmp_path), sweep_id="s1",
                clock=lambda: next(ticks), sleep=lambda s: None, poll_seconds=1, log=lambda *_a: None)
    kinds = [c[0] for c in api.calls]
    assert kinds.index("quote") < kinds.index("start")


def test_run_case_refuses_when_the_eval_case_id_does_not_read_back(tmp_path):
    api = FakeApi(stamped_case="")  # the row kept eval_case_id null
    res = er.run_case(api, "7154", inputs=_inputs(tmp_path), quotes=_quotes(tmp_path), sweep_id="s1",
                      dry_run=True, log=lambda *_a: None)
    assert res.status == "failed" and "eval_case_id" in res.error
    assert [c[0] for c in api.calls] == ["create", "stamp"]


def test_run_case_quote_blind_guard_runs_at_the_pricing_stage(tmp_path):
    api = FakeApi()
    leak = dict(_quotes(tmp_path)[0], sha256="9" * 64)
    with pytest.raises(M.BlindViolation, match="answer_hash_leak"):
        er.run_case(api, "7154", inputs=_inputs(tmp_path), quotes=[leak], sweep_id="s1", dry_run=True,
                    answer_hashes={"9" * 64: "7154:BOQ.xlsx"}, log=lambda *_a: None)
    assert api.calls == []


def _quote_backup(tmp_path):
    backup = tmp_path / "backup"
    pid = M.resolve_case_project("7154")
    rows = []
    for i, (name, cat) in enumerate((("Acme Lighting Quote.pdf", "quote"), ("Bolt Lighting Quote.pdf", "quote"),
                                     ("Gear Quote.xlsx", "quote"), ("Plans.pdf", "electrical_drawing"))):
        sp = f"{pid}/quote/{str(i) * 32}-{name}"
        rows.append({"id": f"pf-{i}", "project_id": pid, "category": cat, "filename": name, "storage_path": sp})
        path = backup / "storage" / "project-files" / sp
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(F.blank_pdf(1, width=612, height=792))
    (backup / "data").mkdir(parents=True)
    (backup / "data" / "project_files.json").write_text(json.dumps(rows))
    return backup, pid, rows


def test_case_quotes_from_the_reference_with_the_recorded_category(tmp_path):
    backup, _pid, rows = _quote_backup(tmp_path)
    ref = {"g3_materials": [
        {"category": "Lighting", "selected": {"quote_id": "rq-a", "file": {"filename": rows[0]["filename"], "storage_path": rows[0]["storage_path"], "sha256": "a" * 64}},
         "alternatives": [{"quote_id": "rq-b", "file": {"filename": rows[1]["filename"], "storage_path": rows[1]["storage_path"]}}]},
        {"category": "General Material", "selected": {"quote_id": "rq-gm", "file": None}, "alternatives": []},
    ]}
    quotes = er.case_quotes("7154", backup=backup, reference=ref)
    assert [(q["filename"], q["material_category"], q["category"], q["quote_id"]) for q in quotes] == [
        ("Acme Lighting Quote.pdf", "Lighting", "quote", "rq-a"), ("Bolt Lighting Quote.pdf", "Lighting", "quote", "rq-b")]
    assert quotes[0]["sha256"] == "a" * 64 and len(quotes[1]["sha256"]) == 64 and quotes[1]["in_manifest"] is False
    assert all(Path(q["path"]).is_file() and "usd" not in json.dumps(q).lower() for q in quotes)


def test_case_quotes_fall_back_to_the_backup_quote_rows_by_file_name(tmp_path):
    backup, _pid, _rows = _quote_backup(tmp_path)
    quotes = er.case_quotes("7154", backup=backup, reference=None)
    # PDFs of category quote whose name gives a category; the xlsx and the drawing stay out.
    assert [(q["filename"], q["material_category"]) for q in quotes] == [
        ("Acme Lighting Quote.pdf", "Lighting"), ("Bolt Lighting Quote.pdf", "Lighting")]


class _FakeAuth:
    refresh = "rt"

    def __init__(self):
        self.grants = 0

    def headers(self):
        return {"Authorization": "Bearer t"}

    def _grant(self, grant_type, body):
        self.grants += 1


def test_http_api_posts_quotes_to_the_pricing_route_with_the_category(tmp_path):
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401 if len(seen) == 1 else 201, json={})

    auth = _FakeAuth()
    api = er.HttpApi(auth, "http://127.0.0.1:5061/")
    api.client = httpx.Client(transport=httpx.MockTransport(handler))
    code = api.upload_quote("run-1", Path(_quotes(tmp_path)[0]["path"]), "Acme Lighting Quote.pdf", "Lighting")
    assert code == 201 and auth.grants == 1 and len(seen) == 2
    req = seen[-1]
    assert req.method == "POST" and str(req.url) == "http://127.0.0.1:5061/ai-estimating/pricing/runs/run-1/quotes"
    body = req.content
    assert b'name="category"' in body and b"Lighting" in body and b'filename="Acme Lighting Quote.pdf"' in body


class _FakeService:
    """The dev service client: records the update and serves the read-back."""

    def __init__(self, row):
        self.row = row
        self.ops = []

    def table(self, name):
        svc = self

        class Q:
            def __init__(self):
                self.filters, self.patch = [], None

            def update(self, patch):
                self.patch = patch
                return self

            def select(self, cols):
                return self

            def eq(self, col, val):
                self.filters.append((col, val))
                return self

            def execute(self):
                match = all(svc.row.get(c) == v for c, v in self.filters)
                if self.patch is not None:
                    svc.ops.append((name, dict(self.patch), list(self.filters)))
                    if match:
                        svc.row.update(self.patch)
                    return type("R", (), {"data": [svc.row] if match else []})()
                return type("R", (), {"data": [svc.row] if match else []})()

        return Q()


def test_http_api_stamps_the_eval_ids_on_a_created_run_and_reads_them_back():
    svc = _FakeService({"id": "run-1", "status": "created", "eval_case_id": None, "eval_sweep_id": None})
    api = er.HttpApi(_FakeAuth(), "http://x", service=svc)
    assert api.stamp_eval_ids("run-1", case_id="7154", sweep_id="s1") == {"eval_case_id": "7154", "eval_sweep_id": "s1"}
    assert svc.ops == [("plans_reader_runs", {"eval_case_id": "7154", "eval_sweep_id": "s1"}, [("id", "run-1"), ("status", "created")])]
    # A started run is never rewritten; without a service client nothing is claimed.
    started = _FakeService({"id": "run-2", "status": "reading", "eval_case_id": None, "eval_sweep_id": None})
    assert er.HttpApi(_FakeAuth(), "http://x", service=started).stamp_eval_ids("run-2", case_id="7154", sweep_id=None)["eval_case_id"] is None
    assert er.HttpApi(_FakeAuth(), "http://x").stamp_eval_ids("run-1", case_id="7154", sweep_id=None) == {}


def test_holdout_allow_list_scopes_the_dirty_tree_rule(tmp_path):
    import scripts.eval_run as er

    paths = ["app/assistant/loop.py", "app/core/config.py", "components/chat/x.tsx", "app/services/plans_reader_runs.py"]
    assert er.unallowed_dirty_paths(paths, ["app/assistant/", "components/chat/", "app/core/config.py"]) == ["app/services/plans_reader_runs.py"]
    assert er.unallowed_dirty_paths(paths, []) == paths
    allow = tmp_path / "allow.txt"
    allow.write_text("# other builds\napp/assistant/\ncomponents/chat/\n\napp/core/config.py\n")
    assert er.load_allow_dirty(str(allow)) == ["app/assistant/", "components/chat/", "app/core/config.py"]
    assert er.load_allow_dirty(None) == []


def test_auth_relogs_in_when_the_refresh_token_is_revoked(monkeypatch):
    import httpx

    from scripts import plans_reader_live_run as lr

    auth = lr._Auth.__new__(lr._Auth)
    auth._url, auth._anon, auth._service_key = "https://x.supabase.co", "anon", "svc"
    auth.access, auth.refresh, auth.expires_at = "old", "r1", 0.0
    calls = []

    def fake_grant(grant_type, body):
        calls.append(grant_type)
        if grant_type == "refresh_token":
            req = httpx.Request("POST", "https://x.supabase.co/auth/v1/token")
            raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, request=req))
        auth.access, auth.refresh, auth.expires_at = "new", "r2", 10**12

    monkeypatch.setattr(auth, "_grant", fake_grant)
    monkeypatch.setattr(auth, "_login", lambda: fake_grant("password", {}))
    assert auth.headers() == {"Authorization": "Bearer new"}
    assert calls == ["refresh_token", "password"]
    # a server-side failure other than a revoked token still raises
    def grant_500(grant_type, body):
        req = httpx.Request("POST", "https://x.supabase.co/auth/v1/token")
        raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))

    auth.expires_at = 0.0
    monkeypatch.setattr(auth, "_grant", grant_500)
    with pytest.raises(httpx.HTTPStatusError):
        auth.headers()
