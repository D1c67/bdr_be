"""Loader for the committed Q27 fixtures of AI Estimating run ff8fad25
(project 7154): tests/fixtures/plans_reader/run_7154 (trimmed JSON, no
drawings, no prices; MANIFEST.json lists the trimming rules and the source
hashes). The layout mirrors the weekend bundle, so
scripts/plans_reader_import_run.py can read it as a bundle too.

Group outputs that equal a category or the main output are not duplicated in
the fixtures; `load_7154()` attaches every stored group output to its group
and resolves the rest the way the import harness does.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

FIXTURE_7154 = Path(__file__).resolve().parent / "fixtures" / "plans_reader" / "run_7154"
EVAL_BUNDLE_7154 = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7154/ff8fad25")
ELEC_DOC_7154 = "538c95b5-3df0-4d7e-b18b-c01135083d20"
MECH_DOC_7154 = "a9720d0e-6c74-4d06-b23f-3071a6f661a1"
_BASE_DROP = ("escalation_patches", "unresolved_flags", "source")


def load_json(rel: str) -> Any:
    return json.loads((FIXTURE_7154 / rel).read_text())


def group_file(group: dict) -> Path:
    return FIXTURE_7154 / "groups" / (
        f"{group['kind']}_{group['category'] or 'none'}_{group['group_index']}_p{group['pass']}.json"
    )


def load_7154() -> dict[str, Any]:
    """{groups (outputs attached), documents, files, run, electrical, rfp,
    specifications, main, deliverables {boq, brief, estimate, marked_plans}}."""
    outputs = {
        "electrical": load_json("outputs/category_electrical_drawings_v1.json"),
        "rfp": load_json("outputs/category_rfp_v1.json"),
        "specifications": load_json("outputs/category_specifications_v1.json"),
    }
    by_category = {o["category"]: o for o in outputs.values()}
    main = load_json("outputs/main_none_v1.json")
    groups = load_json("groups.json")
    for g in groups:
        path = group_file(g)
        if path.is_file():
            g["output"] = json.loads(path.read_text())
        elif g["kind"] in ("rollup", "sub") and g["category"] in by_category:
            g["output"] = {k: copy.deepcopy(v) for k, v in by_category[g["category"]].items() if k not in _BASE_DROP}
        elif g["kind"] == "main":
            g["output"] = copy.deepcopy(main)
    return {
        "groups": groups,
        "documents": load_json("documents.json"),
        "files": load_json("files.json"),
        "run": load_json("run.json"),
        "main": main,
        "deliverables": {k: load_json(f"deliverables/{k}_v1.json") for k in ("boq", "brief", "estimate", "marked_plans")},
        **outputs,
    }


def rows_meta(d: dict[str, Any]) -> dict[str, Any]:
    """The plans_reader_rows upgrade meta for the fixture run."""
    return {"groups": d["groups"], "documents": d["documents"], "ledger": d["run"]["ledger"]}
