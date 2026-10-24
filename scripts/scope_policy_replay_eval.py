"""WS5 replay eval over stored runs (docs/AI_ESTIMATING_BUILD_SPEC.md WS5 "replay eval
over stored runs"; app/services/plans_reader_replay.py, app/services/scope_engine.py).

Usage (from bdr_be):

    uv run python scripts/scope_policy_replay_eval.py [--bundles-root EVAL_ROOT/bundles] \\
        [--bundle <dir> ...] [--out EVAL_ROOT/reports/scope_replay.md] [--real-stages] \\
        [--backup /Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01]

Every stored run bundle (the 7154 weekend bundle and every WS0 bundle under
EVAL_ROOT/bundles/<case>/<run id8>/ that has a bundle.json) is replayed at $0 from
the `derive` stage to the `enforce` stage: the WS5 scope part of derive builds the
ScopeContext from the bundle's documents (real parsed text of the backup PDFs), the
legacy consolidate carries the stored main output, and the WS5 enforce stage applies
the active scope policy. With `--real-stages` every other implemented stage is
registered too (the WS3 measure chains after the scope part of derive). The replay
refuses every provider call, so nothing here can spend.

Measured per bundle, before (stored main) and after (enforced main): extra allowances,
BOQ lines, RFIs, brief exclusions; excluded rows (`excluded_lines`, by source and by
reason); report clarifications, exclusions, decisions, needs_review codes, proposal
line suggestions; wage (type, basis, confidence, score) against the project record in
the production backup (the replay never reads the record, so this is the record-hidden
agreement); ScopeContext statistics; enforce idempotence (a second enforce changes
nothing). Writes EVAL_ROOT/reports/scope_replay.md and scope_replay.json. Counts and
ratios only: no dollar figure is read or written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import eval_bundle, eval_manifest, scope_policy  # noqa: E402
from app.services import plans_reader_replay as replay_mod  # noqa: E402
from app.services import scope_engine as se  # noqa: E402

PACIFIC = ZoneInfo("America/Los_Angeles")
REPORT_NAME = "scope_replay"
WAGE_TYPES = ("prevailing_wage", "non_prevailing_wage")
_MONEY_RE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?")
TEXT_LIMIT = 180


# Discovery and truth ------------------------------------------------------------------------------


def discover_bundles(bundles_root: Path) -> list[Path]:
    """Every bundle directory (one holding bundle.json) under <root>/<case>/<run id8>/."""
    if not bundles_root.is_dir():
        return []
    return sorted(p.parent for p in bundles_root.glob("*/*/bundle.json") if p.is_file())


def load_projects(backup_dir: Path) -> dict[str, dict[str, Any]]:
    path = backup_dir / "data" / "projects.json"
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text())
    return {str(r.get("id")): r for r in rows if isinstance(r, Mapping) and r.get("id")}


def record_wage(projects: Mapping[str, Mapping[str, Any]], project_ids: Sequence[str | None]) -> str | None:
    """The project record's wage_type (the truth of spec 8.7), or None when no
    candidate project is in the backup or the value is not one of the enum."""
    for pid in project_ids:
        row = projects.get(str(pid)) if pid else None
        if row is not None:
            value = row.get("wage_type")
            return value if value in WAGE_TYPES else None
    return None


# Measures ---------------------------------------------------------------------------------------


def scrub(text: Any, limit: int = TEXT_LIMIT) -> str:
    """Report text: dollar amounts masked, whitespace folded, capped."""
    s = _MONEY_RE.sub("$[amount]", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def main_counts(main: Mapping[str, Any] | None) -> dict[str, int]:
    """Counts of one main output (stored or enforced)."""
    if not isinstance(main, Mapping):
        return {"boq_lines": 0, "extra_allowances": 0, "rfis": 0, "brief_exclusions": 0, "excluded_lines": 0}
    estimate = main.get("estimate") if isinstance(main.get("estimate"), Mapping) else {}
    brief = main.get("brief") if isinstance(main.get("brief"), Mapping) else {}
    return {
        "boq_lines": sum(1 for _ in se.boq_items(main)),
        "extra_allowances": len(estimate.get("extra_allowances") or []),
        "rfis": len(brief.get("rfis") or []),
        "brief_exclusions": len(brief.get("exclusions") or []),
        "excluded_lines": len(main.get("excluded_lines") or []),
    }


def _reason_head(reason: Any) -> str:
    return str(reason or "unknown").split(":", 1)[0] or "unknown"


def summarize_case(
    *,
    case_id: str | None,
    run_id8: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    ctx: Mapping[str, Any] | None,
    truth_wage: str | None,
    idempotent: bool | None,
    stage_seconds: Mapping[str, float] | None = None,
    stage_errors: Sequence[str] = (),
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """One bundle's measured values (pure; the script and its test call it)."""
    report = (after or {}).get("scope_policy") if isinstance(after, Mapping) else None
    report = report if isinstance(report, Mapping) else {}
    excluded = [e for e in (after or {}).get("excluded_lines") or [] if isinstance(e, Mapping)] if after else []
    bid = (ctx or {}).get("bid_conditions") or {}
    predicted = bid.get("wage_type")
    stats = dict((ctx or {}).get("stats") or {})
    exclusions = [e for e in report.get("exclusions") or [] if isinstance(e, Mapping)]
    clarifications = [e for e in report.get("clarifications") or [] if isinstance(e, Mapping)]
    cited = sum(1 for e in exclusions + clarifications if e.get("cite"))
    proposal = Counter(str(p.get("kind")) for p in report.get("proposal_lines") or [] if isinstance(p, Mapping))
    return {
        "case_id": case_id,
        "run_id8": run_id8,
        "ok": not stage_errors and isinstance(after, Mapping),
        "stage_errors": list(stage_errors),
        "notes": list(notes),
        "before": main_counts(before),
        "after": main_counts(after),
        "excluded_by_source": dict(sorted(Counter(str(e.get("source") or "unknown") for e in excluded).items())),
        "excluded_by_reason": dict(sorted(Counter(_reason_head(e.get("reason")) for e in excluded).items())),
        "report_counts": dict(report.get("counts") or {}),
        "needs_review": dict(sorted(Counter(str(n.get("code")) for n in report.get("needs_review") or []
                                            if isinstance(n, Mapping)).items())),
        "decided_topics": len(report.get("decided_topics") or []),
        "excluded_allowances": len(report.get("excluded_allowances") or []),
        "rfis_removed": len(report.get("rfis_removed") or []),
        "rfis_rewritten": len(report.get("rfis_rewritten") or []),
        "proposal_lines": dict(sorted(proposal.items())),
        "proposal_lines_all_suggested": all(p.get("status") == "suggested" for p in report.get("proposal_lines") or []
                                            if isinstance(p, Mapping)),
        "exclusion_texts": [scrub(e.get("text")) for e in exclusions],
        "clarification_texts": [scrub(e.get("text")) for e in clarifications],
        "cited_entries": cited,
        "cited_share": round(cited / len(exclusions + clarifications), 4) if exclusions or clarifications else None,
        "wage": {
            "predicted": predicted,
            "basis": bid.get("wage_basis"),
            "confidence": bid.get("wage_confidence"),
            "pw_score": bid.get("pw_score"),
            "positive_cues": sum(1 for c in bid.get("cues") or [] if (c.get("weight") or 0) > 0),
            "labor_time": bid.get("labor_time"),
            "bond_required": bid.get("bond_required"),
            "needs_review": list(bid.get("needs_review") or []),
            "record": truth_wage,
            "agrees": (predicted == truth_wage) if truth_wage else None,
        },
        "context": {k: stats.get(k) for k in ("statements", "obligations", "excluded_obligations", "facts",
                                              "verified_facts")},
        "drawn_systems": list((ctx or {}).get("drawn_systems") or []),
        "idempotent": idempotent,
        "stage_seconds": dict(stage_seconds or {}),
    }


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Totals and ratios across the replayed bundles."""
    ok = [r for r in rows if r.get("ok")]
    with_truth = [r for r in ok if r["wage"]["record"]]
    agree = sum(1 for r in with_truth if r["wage"]["agrees"])
    return {
        "bundles": len(rows),
        "replayed": len(ok),
        "extra_allowances_before": sum(r["before"]["extra_allowances"] for r in ok),
        "extra_allowances_after": sum(r["after"]["extra_allowances"] for r in ok),
        "excluded_rows": sum(r["after"]["excluded_lines"] for r in ok),
        "clarifications": sum(int(r["report_counts"].get("clarifications") or 0) for r in ok),
        "exclusions": sum(int(r["report_counts"].get("exclusions") or 0) for r in ok),
        "needs_review": sum(int(r["report_counts"].get("needs_review") or 0) for r in ok),
        "rfis_before": sum(r["before"]["rfis"] for r in ok),
        "rfis_after": sum(r["after"]["rfis"] for r in ok),
        "wage_with_record": len(with_truth),
        "wage_agree": agree,
        "wage_agreement": round(agree / len(with_truth), 4) if with_truth else None,
        "idempotent": sum(1 for r in ok if r.get("idempotent")),
    }


# Replay -----------------------------------------------------------------------------------------


def register_stages(*, real: bool) -> None:
    """Legacy entries plus the WS5 derive and enforce stages; with `real`
    every implemented stage first (the WS5 derive then chains before WS3)."""
    replay_mod.reset_stages(real=real)
    se.register_replay_stages()


def run_case(bundle_dir: Path, *, backup_dir: Path, projects: Mapping[str, Mapping[str, Any]], out_root: Path,
             expected_project: str | None = None, settings_overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    bundle = eval_bundle.load_bundle(bundle_dir)
    before = bundle.main_output if isinstance(bundle.main_output, Mapping) else None
    out_dir = out_root / (bundle.case_id or "unknown") / f"{bundle.run_id8}_{int(time.time())}"
    result = replay_mod.replay(bundle, from_stage="derive", to_stage="enforce", out_dir=out_dir, backup_dir=backup_dir,
                               settings_overrides=dict(settings_overrides or {}))
    after = result.artifacts.get("main") if "scope_report" in result.artifacts else None
    ctx = result.artifacts.get("scope_context")
    idempotent = None
    if isinstance(after, Mapping) and isinstance(ctx, Mapping):
        settings = get_settings()
        if settings_overrides:
            settings = settings.model_copy(update=dict(settings_overrides))
        again = se.enforce(after, ctx, scope_policy.active_policy().doc, (), settings=settings)
        idempotent = json.dumps(again, sort_keys=True, default=str) == json.dumps(after, sort_keys=True, default=str)
    truth = record_wage(projects, [expected_project, bundle.run.get("project_id")])
    errors = [f"{s.name}: {s.error}" for s in result.stages if s.error and s.name in ("derive", "consolidate", "enforce")]
    return summarize_case(
        case_id=bundle.case_id, run_id8=bundle.run_id8, before=before, after=after,
        ctx=ctx if isinstance(ctx, Mapping) else None, truth_wage=truth, idempotent=idempotent,
        stage_seconds={s.name: s.seconds for s in result.stages if s.name in ("derive", "consolidate", "enforce")},
        stage_errors=errors, notes=result.notes,
    )


# Report -----------------------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, Mapping):
        return ", ".join(f"{k} {v}" for k, v in value.items()) or "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "none"
    return str(value)


def render_markdown(rows: Sequence[Mapping[str, Any]], totals: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
    lines = [
        "# WS5 scope policy replay eval",
        "",
        f"Generated {meta['generated_at']} (Pacific) by `scripts/scope_policy_replay_eval.py`. Replay at $0 from",
        "`derive` to `enforce` over every stored run bundle; provider calls refused. Counts and ratios only.",
        "",
        f"- Policy: {meta['policy_source']} v{meta['policy_version']} sha256 {str(meta['policy_sha'])[:16]}",
        f"- Stages: {meta['stages']}; settings overrides: {_fmt(meta['settings_overrides'])}",
        f"- plans_reader_demo_two_line_style: {meta['demo_two_line_style']}; ai_scope_policy_enforce (dev .env): "
        f"{meta['enforce_flag']}",
        f"- Bundles found: {totals['bundles']}; replayed: {totals['replayed']}",
        "",
        "## Totals",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Extra allowances before -> after | {totals['extra_allowances_before']} -> {totals['extra_allowances_after']} |",
        f"| Excluded rows (excluded_lines) | {totals['excluded_rows']} |",
        f"| Exclusions / clarifications | {totals['exclusions']} / {totals['clarifications']} |",
        f"| needs_review entries | {totals['needs_review']} |",
        f"| RFIs before -> after | {totals['rfis_before']} -> {totals['rfis_after']} |",
        f"| Wage agreement with the project record (record hidden) | {totals['wage_agree']} of "
        f"{totals['wage_with_record']} ({_fmt(totals['wage_agreement'])}) |",
        f"| Enforce idempotent | {totals['idempotent']} of {totals['replayed']} |",
        "",
    ]
    for r in rows:
        w = r["wage"]
        lines += [
            f"## Case {r['case_id']} (run {r['run_id8']})",
            "",
            "| Measure | Before | After |",
            "|---|---|---|",
        ]
        for key in ("extra_allowances", "boq_lines", "rfis", "brief_exclusions", "excluded_lines"):
            lines.append(f"| {key} | {r['before'][key]} | {r['after'][key]} |")
        lines += [
            "",
            f"- Stage errors: {_fmt(r['stage_errors'])}; stage seconds: {_fmt(r['stage_seconds'])}",
            f"- Excluded rows by source: {_fmt(r['excluded_by_source'])}",
            f"- Excluded rows by reason: {_fmt(r['excluded_by_reason'])}",
            f"- Report counts: {_fmt(r['report_counts'])}",
            f"- Excluded allowances: {r['excluded_allowances']}; RFIs removed {r['rfis_removed']}, rewritten "
            f"{r['rfis_rewritten']}; decided topics {r['decided_topics']}",
            f"- needs_review codes: {_fmt(r['needs_review'])}",
            f"- Proposal line suggestions: {_fmt(r['proposal_lines'])} (all suggested: {r['proposal_lines_all_suggested']})",
            f"- Exclusions and clarifications with a cite: {r['cited_entries']} ({_fmt(r['cited_share'])})",
            f"- Wage: predicted {w['predicted']} ({w['basis']}, confidence {_fmt(w['confidence'])}, score "
            f"{_fmt(w['pw_score'])}, positive cues {w['positive_cues']}); record {_fmt(w['record'])}; agrees "
            f"{_fmt(w['agrees'])}; labor time {w['labor_time']}; bond required {_fmt(w['bond_required'])}; "
            f"needs_review {_fmt(w['needs_review'])}",
            f"- ScopeContext: {_fmt(r['context'])}; drawn systems {_fmt(r['drawn_systems'])}",
            f"- Enforce idempotent: {_fmt(r['idempotent'])}",
            "",
            "Exclusions:",
            "",
        ]
        lines += [f"- {t}" for t in r["exclusion_texts"]] or ["- none"]
        lines += ["", "Clarifications:", ""]
        lines += [f"- {t}" for t in r["clarification_texts"]] or ["- none"]
        if r["notes"]:
            lines += ["", "Replay notes:", ""] + [f"- {scrub(n)}" for n in r["notes"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WS5 scope policy replay eval over stored run bundles ($0).")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--bundles-root", type=Path, default=None, help="default EVAL_ROOT/bundles")
    p.add_argument("--bundle", type=Path, action="append", default=[], help="one bundle dir (repeatable)")
    p.add_argument("--backup", type=Path, default=eval_bundle.DEFAULT_BACKUP_DIR)
    p.add_argument("--out", type=Path, default=None, help="default EVAL_ROOT/reports/scope_replay.md")
    p.add_argument("--real-stages", action="store_true", help="register every implemented stage (WS3 measure too)")
    p.add_argument("--set", action="append", default=[], help="settings override key=value (bool and int parsed)")
    return p


def _parse_override(item: str) -> tuple[str, Any]:
    key, _, raw = item.partition("=")
    value: Any = raw.strip()
    if value.lower() in ("true", "false"):
        value = value.lower() == "true"
    else:
        try:
            value = int(value)
        except ValueError:
            pass
    return key.strip(), value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    bundles = [Path(b) for b in args.bundle] or discover_bundles(args.bundles_root or root / "bundles")
    if not bundles:
        print(f"no bundles found under {args.bundles_root or root / 'bundles'}")
        return 1
    overrides = dict(_parse_override(s) for s in args.set)
    register_stages(real=args.real_stages)
    projects = load_projects(args.backup)
    rows: list[dict[str, Any]] = []
    for b in bundles:
        expected_project = None
        try:
            case = eval_bundle.load_bundle(b).case_id
            exp_path = root / "references" / str(case) / "expected.json"
            if case and exp_path.is_file():
                expected_project = json.loads(exp_path.read_text()).get("project_id")
        except Exception as exc:  # noqa: BLE001 - reported per bundle
            print(f"{b}: cannot read the bundle ({type(exc).__name__}: {exc})")
            continue
        print(f"replaying {b} ...", flush=True)
        rows.append(run_case(b, backup_dir=args.backup, projects=projects, out_root=root / "replays" / "scope",
                             expected_project=expected_project, settings_overrides=overrides))
    totals = aggregate(rows)
    settings = get_settings()
    active = scope_policy.active_policy()
    meta = {
        "generated_at": datetime.now(PACIFIC).strftime("%Y-%m-%d %H:%M"),
        "policy_source": active.source,
        "policy_version": active.version,
        "policy_sha": active.sha256,
        "stages": "every implemented stage" if args.real_stages else "WS5 derive (scope part), legacy consolidate, WS5 enforce",
        "settings_overrides": overrides,
        "demo_two_line_style": overrides.get("plans_reader_demo_two_line_style",
                                             getattr(settings, "plans_reader_demo_two_line_style", None)),
        "enforce_flag": getattr(settings, "ai_scope_policy_enforce", None),
    }
    out = args.out or root / "reports" / f"{REPORT_NAME}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(rows, totals, meta))
    out.with_suffix(".json").write_text(json.dumps({"meta": meta, "totals": totals, "cases": rows}, indent=1,
                                                   sort_keys=True, default=str) + "\n")
    print(json.dumps(totals, indent=1))
    print(out)
    return 0 if all(r["ok"] for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
