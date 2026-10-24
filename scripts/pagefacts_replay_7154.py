"""Replay the 7154 reading plan with the page facts at $0 (WS1 step 13).

Builds the planned 7154 sub requests from the backup PDFs through the
`plan` replay stage (app/services/pagefacts/replay.py) with the page facts
flags on, compares the plan with the stored run (groups, input tokens and
the estimated USD of the reading part against the stored calls), asserts
the spec 6 WS1 budget (planned input at or below the stored run's input
tokens) and writes the `planned_requests` artifact that
plans_reader_acceptance reads for E16 and E18, plus a report, under
EVAL_ROOT (never into the repo: the report carries the stored run's spend).

    cd bdr_be && uv run python scripts/pagefacts_replay_7154.py
        [--bundle EVAL_ROOT/bundles/7154/ff8fad25] [--out EVAL_ROOT/replays/7154/pagefacts_plan]
        [--flags-off]   (plan with every page-facts flag off: the T-INT-1 baseline)

No model is called (the replay refuses every provider call). Exit 1 when
an assertion fails; the report is written either way.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import eval_bundle, llm_pricing  # noqa: E402
from app.services import plans_reader_pack as pack  # noqa: E402
from app.services import plans_reader_replay as rp  # noqa: E402
from app.services.pagefacts import replay as pf_replay  # noqa: E402

PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_BUNDLE = Path(get_settings().eval_root) / "bundles" / "7154" / "ff8fad25"
DEFAULT_OUT = Path(get_settings().eval_root) / "replays" / "7154" / "pagefacts_plan"
FLAGS_ON = {
    "plans_reader_page_facts_enabled": True,
    "plans_reader_page_facts_in_subs": True,
    "plans_reader_spec_select": True,
    "plans_reader_facts_relabel": True,
}
FLAGS_OFF = {
    "plans_reader_page_facts_enabled": False,
    "plans_reader_facts_relabel": False,
}
READING_KINDS = ("sub", "rollup", "escalation")


def stored_reading(bundle: eval_bundle.RunBundle) -> dict:
    """The stored run's reading part: sub groups, input tokens of every
    call and the spend of the reading calls (sub, rollup, escalation)."""
    calls = [c for c in bundle.calls if c.get("ok")]
    reading = [c for c in calls if c.get("kind") in READING_KINDS]
    return {
        "sub_groups": len(bundle.groups_of("sub")),
        "sub_input_tokens": sum(int(c.get("input_tokens") or 0) for c in calls if c.get("kind") == "sub"),
        "reading_input_tokens": sum(int(c.get("input_tokens") or 0) for c in reading),
        "all_input_tokens": sum(int(c.get("input_tokens") or 0) for c in calls),
        "reading_cost_usd": float(sum(Decimal(str(c.get("cost_usd") or 0)) for c in reading)),
        "spec_pages_sent": sum(len(g.get("pages") or []) for g in bundle.groups_of("sub", "specifications")),
        "model": bundle.run.get("model"),
    }


def planned_cost(groups: list[dict], model: str | None) -> float:
    """Estimated USD of the planned sub calls (input at the plan estimate,
    output per pack.group_output_tokens, long-context surcharge as the
    cost preview applies it)."""
    price = llm_pricing.price_for(model)
    if price is None:
        return 0.0
    total = 0.0
    for g in groups:
        grp = pack.Group(str(g.get("category")), int(g.get("group_index") or 0), items=list(g.get("pages") or []))
        out_tokens = pack.group_output_tokens(grp)
        total += pack._call_cost(
            int(g.get("est_input_tokens") or 0), out_tokens, float(price.input), float(price.output),
            int(price.long_context_threshold or pack.LONG_CONTEXT_THRESHOLD),
        )
    return round(total, 4)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--flags-off", action="store_true", help="plan with the page-facts flags off (baseline)")
    args = ap.parse_args(argv)

    bundle = eval_bundle.load_bundle(args.bundle)
    pf_replay.register()
    overrides = FLAGS_OFF if args.flags_off else FLAGS_ON
    result = rp.replay(bundle, from_stage="plan", to_stage="plan", out_dir=args.out, settings_overrides=overrides)
    stage = result.stage("plan")
    if stage.error:
        print(f"plan stage failed: {stage.error}")
        return 1
    art = result.artifacts
    summary = art["plan_summary"]
    requests = art["planned_requests"]
    stored = stored_reading(bundle)
    planned_tokens = int(summary["planned_input_tokens"])
    planned_usd = planned_cost(art["planned_groups"], stored["model"])
    checks = {
        "planned_groups_at_most_stored_subs_plus_2": len(art["planned_groups"]) <= stored["sub_groups"] + 2,
        "planned_input_at_or_below_stored_input": planned_tokens <= stored["all_input_tokens"],
        "planned_usd_at_or_below_stored_reading_usd": planned_usd <= stored["reading_cost_usd"] or stored["reading_cost_usd"] == 0,
        "legend_text_in_every_drawing_sub": all(
            r["has_legend_text"] for r in requests if str(r["category"]).endswith("_drawings")
        ) if not args.flags_off else True,
        "facts_block_after_every_page": all(r["pages_without_facts_block"] == 0 for r in requests) if not args.flags_off else True,
        "spec_pages_sent_at_most_68": summary["spec_pages_sent"] <= 68 if not args.flags_off else True,
        "mech_relabeled_and_reduced": any(
            d.get("relabel") and d.get("reduced_reading") and d.get("pages_planned", 0) < (d.get("pages") or 0)
            for d in summary["documents"]
        ) if not args.flags_off else True,
    }
    report = {
        "schema": "bdr.eval.pagefacts_plan/1",
        "at": datetime.now(PACIFIC).isoformat(),
        "bundle": str(args.bundle),
        "flags": summary["flags"],
        "stored": stored,
        "planned": {
            "groups": len(art["planned_groups"]),
            "by_category": summary["planned_by_category"],
            "input_tokens": planned_tokens,
            "input_ratio_vs_stored_all": round(planned_tokens / stored["all_input_tokens"], 4) if stored["all_input_tokens"] else None,
            "estimated_usd": planned_usd,
            "spec_pages_sent": summary["spec_pages_sent"],
            "spec_pages": summary["spec_pages"],
            "legend_entries": summary["legend_entries"],
            "legend_pages": summary["legend_pages"],
            "ledger_est_tokens": summary["ledger_est_tokens"],
            "documents": summary["documents"],
        },
        "checks": checks,
        "notes": result.notes,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "planned_requests.json").write_text(json.dumps(requests, indent=2))
    (args.out / "plan_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (args.out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (args.out / "documents.json").write_text(json.dumps(art["documents"], indent=2, default=str))
    (args.out / "ledger.json").write_text(json.dumps(art["ledger"], indent=2, default=str))
    print(f"planned groups {len(art['planned_groups'])} (stored subs {stored['sub_groups']}); by category {summary['planned_by_category']}")
    print(f"planned input tokens {planned_tokens} vs stored {stored['all_input_tokens']} (ratio {report['planned']['input_ratio_vs_stored_all']})")
    print(f"spec pages sent {summary['spec_pages_sent']} of {summary['spec_pages']}; legend entries {summary['legend_entries']}; legend pages {summary['legend_pages']}")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    print(f"report: {args.out / 'report.json'}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
