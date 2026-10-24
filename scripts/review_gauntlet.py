"""The reviewer gauntlet (docs/AI_ESTIMATING_BUILD_SPEC.md WS7: "reviewer
catches the seeded E-class defects in >= 70% of variants with <= 1 false
critical per clean control").

Packs are built ONLY from bundles that exist before the core sweep (the
7154 weekend bundle, the integration and noise bundles, the smoke and older
dev-run bundles): up to --max-bundles of them, holdout cases and core or
holdout sweep exports always excluded. For each bundle the $0 replay runs
the code stages through the rules lane (plan .. review), which gives the
clean control pack (BOQ lines, priced artifact, gate findings, scope
decisions, rule findings, bands); the seeded variants apply one E-class
defect each to the BOQ and the priced artifact in code and rerun the $0
rules lane, so every variant is a replay of the same stored run with one
known error.

Usage (from bdr_be):

    uv run python scripts/review_gauntlet.py --dry-run          # packs and variants, no call
    uv run python scripts/review_gauntlet.py --live --max-bundles 4 --pack-cap-usd 1.25   # wave 4

The dry run writes gauntlet_plan.json and packs/*.json under --out
(default EVAL_ROOT/reports/gauntlet/<timestamp>/) and prints counts; it
makes no model call.

The live run happens entirely in this process (see "Live run" below): the
spine's review builder and finalizer, the WS2 tool loop on an in-memory
loop store, the backend's review tools bound to the bundle (WS1 page facts
from the bundle PDFs in inputs/ or the backup, else the text-layer
fallback; census, schedules, legend, scope policy, facts, rows and
measure from the bundle), no database read or write by the loop. It
prints the plan first (per pack cap and the least answer, the summed
caps), then arms the process as scripts/plans_reader_ab.py does
(EVAL_SWEEP_ID, AI_EVAL_LEDGER_ENFORCE=true, LLM_CASSETTE_MODE=
record_missing) and refuses without an open WS7 dev_probe sweep or when
the summed review caps exceed what that sweep has left (lower them with
--pack-cap-usd or fewer packs). A missing source PDF is a note, never a
failed pack. Dollar figures of the estimate print only with --dollars and
never land in the repo.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import eval_bundle, eval_manifest  # noqa: E402
from app.services import plans_reader_bands as bands_mod  # noqa: E402
from app.services import plans_reader_replay as replay_mod  # noqa: E402
from app.services import plans_reader_review as chain  # noqa: E402
from app.services import plans_reader_review_rules as rules_mod  # noqa: E402
from app.services import plans_reader_reviewer as rv  # noqa: E402
from app.services import plans_reader_rows as rows_mod  # noqa: E402
from app.services.plans_reader_review_rules import ReviewInputs  # noqa: E402
from app.services.plans_reader_review_schemas import REVIEW_SCHEMA  # noqa: E402

GAUNTLET_VERSION = "ws7-gauntlet-1"
MAX_BUNDLES = 8
VARIANTS_PER_BUNDLE = 2
EXCLUDED_SOURCES: frozenset[str] = frozenset({"core", "holdout"})
TOKENS_PER_CHAR = 0.25
MAX_TEXT_PAGES = 400

# E-class defect seeds (spec 3.3). Each returns the expected finding kinds
# and the line ids it touched, or None when the bundle has nothing to seed.
DEFECT_CLASSES: tuple[str, ...] = ("E2", "E4", "E5", "E10", "E11", "E13", "E14", "E22")
EXPECTED_KINDS: dict[str, tuple[str, ...]] = {
    "E2": ("missing_scope", "band_outlier"),
    "E4": ("bid_condition",),
    "E5": ("excluded_scope_carried",),
    "E10": ("unit_wrong",),
    "E11": ("quantity_suspect",),
    "E13": ("double_count",),
    "E14": ("missing_scope",),
    "E22": ("double_count", "quantity_suspect"),
}


@dataclass
class Pack:
    case_id: str
    bundle: str
    variant: str  # clean or E<n>
    pack: dict[str, Any]
    boq_spec: dict[str, Any]
    artifact: dict[str, Any] | None
    expected_kinds: tuple[str, ...] = ()
    expected_lines: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.case_id}_{self.bundle}_{self.variant}"

    def plan_row(self) -> dict[str, Any]:
        text = rv.review_pack_text(self.pack, include_money=False)
        return {
            "name": self.name, "case_id": self.case_id, "bundle": self.bundle, "variant": self.variant,
            "lines": len(self.pack.get("lines") or []), "rule_findings": len(self.pack.get("rule_findings") or []),
            "bands_out": sum(1 for b in self.pack.get("bands") or [] if b.get("severity") in ("warning", "critical")),
            "expected_kinds": list(self.expected_kinds), "expected_lines": list(self.expected_lines),
            "est_input_tokens": int(len(text) * TOKENS_PER_CHAR), "notes": list(self.notes),
        }


# Bundles ---------------------------------------------------------------------------------------


def discover_bundles(root: Path, explicit: list[Path]) -> list[Path]:
    if explicit:
        return [Path(p) for p in explicit]
    out = []
    for case_dir in sorted(root.glob("*")):
        if not case_dir.is_dir():
            continue
        for b in sorted(case_dir.glob("*")):
            if (b / "run.json").is_file():
                out.append(b)
    return out


def pre_core(bundle: eval_bundle.RunBundle) -> tuple[bool, str]:
    """A bundle is a gauntlet input only when it is not a holdout case and
    not a core or holdout sweep export."""
    case = bundle.case_id
    if case:
        try:
            if eval_manifest.tier_of(case) == "holdout":
                return False, "holdout case"
        except Exception:  # noqa: BLE001 - an unknown case is not a holdout
            pass
    purpose = str(bundle.meta.get("sweep_purpose") or bundle.meta.get("purpose") or "")
    if purpose in EXCLUDED_SOURCES:
        return False, f"{purpose} sweep export"
    return True, "ok"


def register_real_stages() -> None:
    replay_mod.register_real_stages()
    import importlib

    for mod, fn in (("app.services.pagefacts.replay", "register"), ("app.services.scope_engine", "register_replay_stages"),
                    ("app.services.plans_reader_evidence", "register_replay_stage")):
        try:
            getattr(importlib.import_module(mod), fn)()
        except Exception as exc:  # noqa: BLE001
            print(f"replay stage from {mod} not registered: {exc}")


# Packs -----------------------------------------------------------------------------------------


def _inputs(case_id: str | None, boq: Mapping[str, Any], art: Mapping[str, Any] | None, artifacts: Mapping[str, Any]) -> ReviewInputs:
    cats = [artifacts[k] for k in sorted(artifacts) if k.startswith("category:") and isinstance(artifacts[k], Mapping)]
    totals = (art or {}).get("totals") if isinstance(art, Mapping) else None
    return ReviewInputs(
        run_id=None, boq=boq, priced=art,
        gate_findings=rules_mod.gate_findings_from_report(artifacts.get("gate_report")),
        scope_report=artifacts.get("scope_report") if isinstance(artifacts.get("scope_report"), Mapping) else None,
        category_outputs=cats, page_facts=artifacts.get("page_facts") if isinstance(artifacts.get("page_facts"), list) else (),
        bid_conditions=artifacts.get("bid_conditions") if isinstance(artifacts.get("bid_conditions"), Mapping) else None,
        total_usd=float((totals or {}).get("bid_total_usd") or 0) or None if isinstance(totals, Mapping) else None,
        local_rules_fallback=False,
    )


def _band_checks(art: Mapping[str, Any] | None, project_id: str | None, project_type: str | None, bands: Any) -> list[Any]:
    if not isinstance(art, Mapping) or bands is None:
        return []
    summary = bands_mod.summary_from_priced(art, project_id=project_id, project_type=project_type)
    return bands_mod.check_bands(summary, bands, exclude_project_ids=[project_id] if project_id else ())


def build_pack(case_id: str, bundle: eval_bundle.RunBundle, variant: str, boq: Mapping[str, Any], art: Mapping[str, Any] | None,
               artifacts: Mapping[str, Any], bands: Any, *, wage_type: str | None = None) -> dict[str, Any]:
    inputs = _inputs(case_id, boq, art, artifacts)
    project_id = str(bundle.run.get("project_id") or "") or None
    checks = _band_checks(art, project_id, None, bands)
    inputs.band_checks = checks
    rules = rules_mod.run_rules(inputs)
    loaded = chain.LoadedInputs(
        inputs=inputs, boq_version=(artifacts.get("deliverable_versions") or {}).get("boq"), boq_spec=boq, artifact=art,
        documents=[{"id": str(d.get("id")), "name": d.get("name"), "category": d.get("category"),
                    "pages": d.get("page_count") or d.get("pages")} for d in bundle.documents],
        project_id=project_id, project_name=str(((bundle.run.get("ledger") or {}).get("project") or {}).get("name") or case_id),
        wage_type=wage_type or ((art or {}).get("wage_type") if isinstance(art, Mapping) else None),
        line_values=chain.line_values_from_artifact(art),
        total_usd=inputs.total_usd,
    )
    pack = chain.build_review_pack(f"gauntlet-{case_id}-{variant}", {"id": f"gauntlet-{bundle.run_id8}"}, loaded, rules, checks,
                                   base_pricing_version=0)
    pack["gauntlet"] = {"case_id": case_id, "bundle": bundle.run_id8, "variant": variant, "version": GAUNTLET_VERSION}
    return pack


# Defect seeds ----------------------------------------------------------------------------------


def _lines(boq: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(rows_mod.iter_boq_lines(boq))


def _item_at(spec: dict[str, Any], path: str) -> tuple[list[dict[str, Any]], int]:
    parts = path.replace("]", "").split(".")
    node: Any = spec
    for part in parts[:-1]:
        name, idx = part.split("[")
        node = node[name][int(idx)]
    name, idx = parts[-1].split("[")
    return node[name], int(idx)


def _art_line(art: dict[str, Any] | None, line_id: str) -> dict[str, Any] | None:
    if not isinstance(art, Mapping):
        return None
    return next((ln for ln in art.get("lines") or [] if ln.get("line_id") == line_id), None)


def _rescale_art(art: dict[str, Any] | None, line_id: str, qty: float | None, unit: str | None = None) -> None:
    ln = _art_line(art, line_id)
    if ln is None:
        return
    if qty is None:
        art["lines"] = [x for x in art["lines"] if x is not ln]
        return
    ln["qty"] = qty
    ln["qty_base"] = qty
    if unit:
        ln["unit"] = unit


def _match(line: Mapping[str, Any], *words: str) -> bool:
    text = f"{line.get('group')} {line.get('subgroup')} {line.get('description')}".upper()
    return all(w in text for w in words)


def seed_defect(klass: str, boq: Mapping[str, Any], art: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any] | None, list[str], str] | None:
    """Apply one E-class defect. Returns (boq, artifact, touched line ids,
    note) or None when the bundle has nothing to seed for that class."""
    spec = copy.deepcopy(dict(boq))
    for row in _lines(spec):
        items, i = _item_at(spec, row["path"])
        items[i]["line_id"] = row["line_id"]
    new_art = copy.deepcopy(dict(art)) if isinstance(art, Mapping) else None
    lines = _lines(spec)
    if klass == "E2":
        wire = [r for r in lines if r.get("group") == "WIRING" and r.get("unit") == "FT"]
        if not wire:
            return None
        for r in wire:
            items, _i = _item_at(spec, r["path"])
            items[:] = [it for it in items if it.get("line_id") != r["line_id"]]
            _rescale_art(new_art, r["line_id"], None)
        return spec, new_art, [r["line_id"] for r in wire], "every FT line of WIRING removed (zero branch wire and conduit)"
    if klass == "E4":
        if new_art is None:
            return None
        new_art["wage_type"] = "non_prevailing_wage"
        return spec, new_art, [], "wage type flipped to non_prevailing_wage on the priced artifact"
    if klass == "E5":
        target = next((r for r in lines if r.get("section") == "NEW WORK"), None)
        if target is None:
            return None
        items, _i = _item_at(spec, target["path"])
        lid = rows_mod.RowSeq({"B": max(int(r["line_id"][1:]) for r in lines)}).next("B")
        items.append({"line_id": lid, "description": "HONEYWELL FIRE ALARM DEVICES AND PANEL (BY OWNER VENDOR)", "quantity": 12,
                      "unit": "EA", "sheet_refs": ["E0.00"], "caveat": None, "material_note": None,
                      "material_category": "Low Voltage", "wastage_applies": False, "is_accessory": False})
        if new_art is not None:
            tmpl = dict(new_art["lines"][0]) if new_art.get("lines") else {}
            new_art["lines"].append({**tmpl, "line_id": lid, "description": "HONEYWELL FIRE ALARM DEVICES AND PANEL (BY OWNER VENDOR)",
                                     "unit": "EA", "qty": 12, "qty_base": 12, "excluded": False, "priced": True, "kind": "other"})
        return spec, new_art, [lid], "an owner-vendor fire alarm line carried as EC scope"
    if klass == "E10":
        target = next((r for r in lines if r.get("unit") == "FT" and _match(r, "LIGHTING")), None) or next(
            (r for r in lines if r.get("unit") == "FT" and any(w in str(r.get("description")).upper() for w in ("UNDERCABINET", "TRACK", "STRIP", "TAPE"))), None)
        if target is None:
            return None
        items, i = _item_at(spec, target["path"])
        items[i]["unit"] = "EA"
        items[i]["quantity"] = 7
        _rescale_art(new_art, target["line_id"], 7, "EA")
        return spec, new_art, [target["line_id"]], f"{target.get('description')} changed from FT to 7 EA"
    if klass == "E11":
        counts = [r for r in lines if r.get("unit") == "EA" and float(r.get("quantity") or 0) >= 12]
        if not counts:
            return None
        target = max(counts, key=lambda r: float(r.get("quantity") or 0))
        items, i = _item_at(spec, target["path"])
        new_q = max(1, int(round(float(target["quantity"]) * 0.3)))
        items[i]["quantity"] = new_q
        items[i]["caveat"] = "Enlarged tile subtotal only"
        _rescale_art(new_art, target["line_id"], new_q)
        return spec, new_art, [target["line_id"]], f"{target.get('description')} cut from {target['quantity']} to {new_q} (tile subtotal promoted)"
    if klass == "E13":
        target = next((r for r in lines if r.get("unit") == "EA" and float(r.get("quantity") or 0) >= 5), None)
        if target is None:
            return None
        areas = spec.get("areas") or []
        dup_area = {"area": f"{areas[0].get('area') or 'BASE BID'} (ENLARGED)", "notes": [], "sections": [{"heading": target.get("section") or "NEW WORK",
                    "groups": [{"name": target.get("group"), "subgroups": [{"name": target.get("subgroup"), "items": []}]}]}]}
        lid = rows_mod.RowSeq({"B": max(int(r["line_id"][1:]) for r in lines)}).next("B")
        dup = {k: v for k, v in target.items() if k in ("description", "quantity", "unit", "sheet_refs", "caveat", "material_note",
                                                        "material_category", "wastage_applies", "is_accessory")}
        dup["line_id"] = lid
        dup_area["sections"][0]["groups"][0]["subgroups"][0]["items"].append(dup)
        areas.append(dup_area)
        if new_art is not None and _art_line(new_art, target["line_id"]) is not None:
            new_art["lines"].append({**_art_line(new_art, target["line_id"]), "line_id": lid})
        return spec, new_art, [target["line_id"], lid], f"{target.get('description')} counted again on an enlarged-plan area"
    if klass == "E14":
        targets = [r for r in lines if any(w in str(r.get("description")).upper() for w in ("THERMOSTAT", "CONTROL CABLE", "18/4", "#18"))]
        if not targets:
            return None
        for r in targets:
            items, _i = _item_at(spec, r["path"])
            items[:] = [it for it in items if it.get("line_id") != r["line_id"]]
            _rescale_art(new_art, r["line_id"], None)
        return spec, new_art, [r["line_id"] for r in targets], "thermostat relocations and control cable dropped"
    if klass == "E22":
        target = next((r for r in lines if r.get("unit") == "EA" and float(r.get("quantity") or 0) >= 9), None)
        if target is None:
            return None
        items, i = _item_at(spec, target["path"])
        q = int(float(target["quantity"]))
        parts = [q // 3, q // 3, q - 2 * (q // 3)]
        base = dict(items[i])
        items.pop(i)
        seq = rows_mod.RowSeq({"B": max(int(r["line_id"][1:]) for r in lines)})
        new_ids = []
        for n, part in enumerate(parts):
            lid = seq.next("B")
            new_ids.append(lid)
            items.insert(i + n, {**base, "line_id": lid, "quantity": part, "description": f"{base.get('description')} ({'EMERGENCY' if n == 0 else 'DEDICATED' if n == 1 else 'GENERAL'})"})
        if new_art is not None:
            tmpl = _art_line(new_art, target["line_id"])
            new_art["lines"] = [x for x in new_art["lines"] if x.get("line_id") != target["line_id"]]
            if tmpl is not None:
                for lid, part in zip(new_ids, parts, strict=True):
                    new_art["lines"].append({**tmpl, "line_id": lid, "qty": part, "qty_base": part})
        return spec, new_art, new_ids, f"{target.get('description')} split three ways by unpriced modifiers"
    return None


def variants_for(case_id: str, bundle: eval_bundle.RunBundle, artifacts: Mapping[str, Any], boq: Mapping[str, Any],
                 art: Mapping[str, Any] | None, bands: Any, *, per_bundle: int, classes: tuple[str, ...]) -> list[Pack]:
    out: list[Pack] = []
    for klass in classes:
        if len(out) >= per_bundle:
            break
        seeded = seed_defect(klass, boq, art)
        if seeded is None:
            continue
        v_boq, v_art, touched, note = seeded
        wage = (v_art or {}).get("wage_type") if isinstance(v_art, Mapping) else None
        pack = build_pack(case_id, bundle, klass, v_boq, v_art, artifacts, bands, wage_type=wage)
        out.append(Pack(case_id=case_id, bundle=bundle.run_id8, variant=klass, pack=pack, boq_spec=v_boq, artifact=v_art,
                        expected_kinds=EXPECTED_KINDS[klass], expected_lines=tuple(touched), notes=[note]))
    return out


def packs_for_bundle(root: Path, bundle_dir: Path, bands: Any, *, backup: Path, per_bundle: int, classes: tuple[str, ...],
                     out_dir: Path, keep: dict[str, Any] | None = None) -> tuple[list[Pack], str]:
    """The clean control and the seeded variants of one bundle. `keep` (by bundle run id8)
    receives the replay artifacts the live run's fallback services read."""
    bundle = eval_bundle.load_bundle(bundle_dir)
    ok, why = pre_core(bundle)
    if not ok:
        return [], why
    case_id = bundle.case_id or bundle_dir.parent.name
    reference = expected = None
    try:
        from app.services import eval_reference as R

        reference = R.load_reference(root, case_id)
    except Exception:  # noqa: BLE001 - no reference: the price stage still runs from the backup
        reference = None
    expected_path = root / "references" / case_id / "expected.json"
    if expected_path.is_file():
        expected = json.loads(expected_path.read_text())
    result = replay_mod.replay(bundle, reference=reference, expected=expected, case_id=case_id, from_stage="plan", to_stage="review",
                               out_dir=out_dir / "replays" / f"{case_id}_{bundle.run_id8}", backup_dir=backup)
    arts = result.artifacts
    boq = arts.get("boq_spec") if isinstance(arts.get("boq_spec"), Mapping) else None
    if boq is None:
        return [], "no BOQ spec after replay"
    art = arts.get("priced_estimate") if isinstance(arts.get("priced_estimate"), Mapping) else None
    if keep is not None:
        keep[bundle.run_id8] = arts
    clean = build_pack(case_id, bundle, "clean", boq, art, arts, bands)
    packs = [Pack(case_id=case_id, bundle=bundle.run_id8, variant="clean", pack=clean, boq_spec=dict(boq), artifact=dict(art) if art else None,
                  notes=[f"replay {'ok' if result.ok else 'with errors'}: " + "; ".join(s.name for s in result.stages if s.error)])]
    packs += variants_for(case_id, bundle, arts, boq, art, bands, per_bundle=per_bundle, classes=classes)
    return packs, "ok"


# Live run (wave 4) -----------------------------------------------------------------------------
#
# Everything runs in this process, the way scripts/plans_reader_ab.py runs its recount and main
# tasks: an in-memory loop store (FakeLoopStore), the review group built by the spine's own seams
# (plans_reader_reviewer.build_review_loop and finalize_review_loop with sb=None), the backend's
# tool registry, and services bound to the bundle (WS1 page facts from the bundle PDFs through
# the WS4 A/B harness, else the text-layer fallback). The loop reads and writes no database: the
# tool context and the loop carry sb=None, the run ceiling gets run_spent_usd=0 and the daily cap
# gate (it reads plans_reader_calls, which an in-memory loop never writes) is replaced for the
# run by LocalCap at the planned total. The eval ledger is the one database user, as in every
# live script: llm._guarded admits each call under the armed sweep and llm_gate.log_call writes
# its llm_call_log row (spec 0.4).

LIVE_WORKSTREAM = "WS7"
LIVE_PURPOSE = "dev_probe"
LIVE_CASSETTE_MODE = "record_missing"
DEV_REF = "bpidntbyvoooqvaispup"
# The spine's review group (plans_reader_agents._LOOP_KINDS, SCHEMA_NAME_BY_KIND): mode review,
# schema name plans_review. The same names keep the request (and its cassette key) the spine's.
REVIEW_MODE = "review"
REVIEW_SCHEMA_NAME = "plans_review"
_CENT = Decimal("0.01")


class GauntletRefused(RuntimeError):
    """A live gauntlet refused before its first model call."""


def _money(value: Any) -> str:
    return f"${Decimal(str(value)).quantize(_CENT)}"


def arm_sweep(sweep: str = "open", *, eval_root: Path, settings: Any = None, cassette: bool = True) -> dict[str, Any]:
    """Check the sweep and arm this process for it, as scripts/plans_reader_ab.py arm_sweep does
    (spec 0.4 rules 2 and 6): the dev database, a sweep that exists, is open and is a WS7
    dev_probe sweep; then EVAL_SWEEP_ID, AI_EVAL_LEDGER_ENFORCE=true and
    LLM_CASSETTE_MODE=record_missing with cassettes under EVAL_ROOT/cassettes. Returns the sweep
    row of eval_ledger.report with `remaining_usd`."""
    import os
    import uuid

    from app.core.config import get_settings
    from app.services import eval_ledger

    s = settings or get_settings()
    url = str(getattr(s, "supabase_url", "") or "")
    if DEV_REF not in url:
        raise GauntletRefused(f"SUPABASE_URL must point at the dev project {DEV_REF}; refusing")
    rep = eval_ledger.report()
    wanted = rep.get("open_sweep_id") if sweep == "open" else sweep
    if not wanted:
        raise GauntletRefused(
            f"no sweep is open; open one with scripts/eval_ledger.py open --workstream {LIVE_WORKSTREAM} "
            f"--purpose {LIVE_PURPOSE} --budget ... (spec 0.4 rule 2)")
    try:
        wanted = str(uuid.UUID(str(wanted)))
    except ValueError as exc:
        raise GauntletRefused(f"--sweep must be a sweep id or 'open', got {sweep!r}") from exc
    row = next((r for r in rep.get("sweeps") or [] if str(r.get("id")) == wanted), None)
    if row is None:
        raise GauntletRefused(f"sweep {wanted} not found")
    if row.get("status") != "open":
        raise GauntletRefused(f"sweep {wanted} is {row.get('status')}, not open")
    if row.get("workstream") != LIVE_WORKSTREAM or row.get("purpose") != LIVE_PURPOSE:
        raise GauntletRefused(
            f"sweep {wanted} is {row.get('workstream')} {row.get('purpose')}; the live gauntlet needs an open "
            f"{LIVE_WORKSTREAM} {LIVE_PURPOSE} sweep")
    remaining = Decimal(str(row.get("budget_usd") or 0)) - Decimal(str(row.get("spent_usd") or 0))
    os.environ["EVAL_SWEEP_ID"] = wanted
    os.environ["AI_EVAL_LEDGER_ENFORCE"] = "true"
    if cassette:
        os.environ["LLM_CASSETTE_MODE"] = LIVE_CASSETTE_MODE
        os.environ["LLM_CASSETTE_DIR"] = str(eval_root / "cassettes")
    get_settings.cache_clear()
    return {**row, "remaining_usd": remaining}


def register_review_tools() -> None:
    """The backend process's tool registry: app/main.py imports the same handler modules
    (WS4's AB_TOOL_MODULES), so the review group is offered what the spine offers it."""
    from app.services import plans_reader_consolidator as cons

    cons.ab_register_tools()


def offered_review_tools(settings: Any) -> list[Any]:
    """The tools the spine offers a review group: mode review, pricing tools only with
    pricing and the agent-tools child flag on (plans_reader_agents._run_loop_group)."""
    from app.services.plans_reader_tools import offered_tools

    pricing = bool(getattr(settings, "ai_pricing_enabled", False) and getattr(settings, "ai_pricing_agent_tools_enabled", False))
    return offered_tools(rv.REVIEW_KIND, mode=REVIEW_MODE, pricing_allowed=pricing)


def review_route(settings: Any) -> dict[str, Any]:
    from app.services import plans_reader_model_routes as model_routes

    route = model_routes.resolve_step(rv.REVIEW_STEP, settings).as_dict()
    route.setdefault("tools", True)
    return route


def pack_budget(settings: Any, route: Mapping[str, Any], pack_cap_usd: Any = None) -> Any:
    """The review group's LoopBudget (rv.review_budget: WS2's review budget, at most 10 tool
    calls), its dollar cap lowered (never raised) to --pack-cap-usd."""
    import dataclasses

    base = rv.review_budget(settings, dict(route))
    if pack_cap_usd is None:
        return base
    cap = Decimal(str(pack_cap_usd))
    if not cap.is_finite() or cap <= 0:
        raise GauntletRefused(f"--pack-cap-usd must be a positive number, got {pack_cap_usd!r}")
    return dataclasses.replace(base, max_usd=min(base.max_usd, cap))


def build_live_plan(packs: list[Pack], *, settings: Any, route: Mapping[str, Any], budget: Any, tools: list[Any]) -> dict[str, Any]:
    """Per pack: the loop cap (the most the group may spend) and the cheapest possible answer
    (round one's input, the pack plus the system prompt and tool definitions before the
    pre-call results, with the final round's output reserve). The sum of the caps is what the
    sweep must have left."""
    from app.services import plans_reader_budgets as budgets
    from app.services.plans_reader_tools import to_openai_tools

    model = str(route.get("model") or "")
    include_money = bool(getattr(settings, "ai_pricing_agent_tools_enabled", False))
    tool_chars = len(json.dumps(to_openai_tools(tools), default=str))
    rows = []
    for p in packs:
        chars = len(rv.REVIEW_SYSTEM) + len(rv.review_pack_text(p.pack, include_money=include_money)) + tool_chars
        tokens = budgets.tokens_for_chars(chars)
        try:
            least = budgets.estimate_round(model, input_tokens=tokens, output_tokens=budget.final_output_tokens).quantize(_CENT)
        except ValueError:  # an unpriced model: the ledger refuses it anyway
            least = None
        rows.append({"name": p.name, "variant": p.variant, "prefix_tokens": tokens, "cap_usd": budget.max_usd,
                     "min_answer_usd": least})
    return {
        "packs": rows, "n": len(rows), "pack_cap_usd": budget.max_usd, "cap_usd": budget.max_usd * len(rows),
        "route": dict(route), "max_rounds": budget.max_rounds, "max_tool_calls": budget.max_tool_calls,
        "tools": [t.name for t in tools],
        "unaffordable": [r["name"] for r in rows if r["min_answer_usd"] is not None and r["min_answer_usd"] > budget.max_usd],
    }


def format_live_plan(plan: Mapping[str, Any]) -> list[str]:
    route = plan["route"]
    out = [
        f"live plan: {plan['n']} packs on {route.get('provider')}:{route.get('model')}:{route.get('effort')}, "
        f"{len(plan['tools'])} tools, at most {plan['max_rounds']} rounds and {plan['max_tool_calls']} tool calls per pack",
        "| pack | prefix tokens | least answer | cap |",
        "|---|---|---|---|",
    ]
    for r in plan["packs"]:
        least = _money(r["min_answer_usd"]) if r["min_answer_usd"] is not None else "-"
        out.append(f"| {r['name']} | {r['prefix_tokens']} | {least} | {_money(r['cap_usd'])} |")
    out.append(f"worst case (sum of the review caps): {plan['n']} x {_money(plan['pack_cap_usd'])} = {_money(plan['cap_usd'])}")
    if plan["unaffordable"]:
        out.append(f"warning: the cap cannot pay for one answer on {', '.join(plan['unaffordable'])} (those loops fail at $0)")
    return out


def check_plan(plan: Mapping[str, Any], remaining_usd: Any) -> None:
    """Refuse a plan with nothing to run or whose summed caps exceed what the sweep has left."""
    from decimal import ROUND_FLOOR

    remaining = Decimal(str(remaining_usd))
    if not plan["n"]:
        raise GauntletRefused("no pack to run")
    if plan["cap_usd"] > remaining:
        fit = (remaining / plan["n"]).quantize(_CENT, rounding=ROUND_FLOOR)
        raise GauntletRefused(
            f"the plan's summed review caps ({plan['n']} packs x {_money(plan['pack_cap_usd'])} = {_money(plan['cap_usd'])}) "
            f"exceed what the sweep has left ({_money(remaining)}); pass --pack-cap-usd {fit} or fewer packs "
            "(--max-bundles, --variants-per-bundle)")


@dataclass
class BundleServices:
    """The review tools' services for one bundle and the page text the quote check reads.
    `facts_store` (WS4's AbFactsStore) serves the WS1 word blobs while `serving()` is open."""

    run_id: str
    services: Any
    page_text: Any
    facts_store: Any = None
    notes: list[str] = field(default_factory=list)
    mode: str = "facts"  # facts | text_layer

    def serving(self) -> Any:
        import contextlib

        return self.facts_store.serving() if self.facts_store is not None else contextlib.nullcontext()


def _quiet(name: str) -> Any:
    """Silence one logger for a block (WS4 logs a traceback per missing source PDF; the
    gauntlet reports those as notes)."""
    import contextlib
    import logging

    @contextlib.contextmanager
    def block() -> Any:
        lg = logging.getLogger(name)
        old = lg.disabled
        lg.disabled = True
        try:
            yield
        finally:
            lg.disabled = old

    return block()


def held_row_store(inputs: Any) -> Any:
    """plans_reader_consolidator.RunRowStore (the rows of a run outside a consolidation, what
    the review kind's get_rows, get_row and get_census read) over the bundle's inputs in memory
    instead of the run's stored outputs."""
    from app.services import plans_reader_consolidator as cons

    class HeldRowStore(cons.RunRowStore):
        def __init__(self) -> None:
            super().__init__(None)

        def _session(self, run_id: str) -> Any:
            if "held" not in self._cache:
                try:
                    self._cache["held"] = cons.Session(cons._fresh_inputs(inputs))
                except Exception as exc:  # noqa: BLE001 - no rows reads as unavailable
                    print(f"  rows unavailable in process: {type(exc).__name__}: {str(exc)[:160]}")
                    self._cache["held"] = None
            return self._cache["held"]

    return HeldRowStore()


def _text_index(bundle: eval_bundle.RunBundle, backup: Path, *, max_pages: int = MAX_TEXT_PAGES
                ) -> tuple[Any, dict[tuple[str, int], str], dict[str, bytes], list[str]]:
    """The text-layer fallback: a FakeTextIndex and page texts from the bundle's PDFs through
    WS1's pure word extractor, the PDFs found and the names of the documents without one."""
    from app.services.pagefacts import textlayer
    from app.services.plans_reader_tools import FakeTextIndex

    runs: dict[tuple[str, int], list[dict[str, Any]]] = {}
    texts: dict[tuple[str, int], str] = {}
    pdfs: dict[str, bytes] = {}
    missing: list[str] = []
    budget = max_pages
    for doc in bundle.documents:
        try:
            pdf = bundle.doc_bytes(str(doc["id"]), backup_dir=backup)
        except Exception:  # noqa: BLE001 - a missing PDF is a note, never a failed pack
            missing.append(str(doc.get("name") or doc.get("id")))
            continue
        pdfs[str(doc["id"])] = pdf
        pages = int(doc.get("page_count") or doc.get("pages") or 0)
        for page in range(1, min(pages, budget) + 1):
            try:
                words = textlayer.positioned_words(pdf, page - 1)
            except Exception:  # noqa: BLE001 - a page without text
                continue
            lines = [{"text": getattr(ln, "t", None) or (ln.get("t") if isinstance(ln, Mapping) else ""),
                      "bbox": list(getattr(ln, "b", None) or (ln.get("b") if isinstance(ln, Mapping) else []) or [])}
                     for ln in (words.lines or [])]
            lines = [ln for ln in lines if ln["text"]]
            runs[(str(doc["id"]), page)] = lines
            texts[(str(doc["id"]), page)] = " ".join(ln["text"] for ln in lines)
        budget -= min(pages, budget)
    return FakeTextIndex(runs), texts, pdfs, missing


def fallback_services(bundle: eval_bundle.RunBundle, artifacts: Mapping[str, Any], *, settings: Any, backup: Path,
                      run_id: str, notes: list[str]) -> BundleServices:
    """Text-layer-only services (no WS1 facts): page text by search from the PDFs that exist,
    the legend from the run ledger, the scope policy and facts from the replayed scope
    context, the rows and census from the replayed category outputs. A document without a PDF
    has no page text (quotes on its pages stay unverified); nothing fails the pack."""
    from app.services import plans_reader_consolidator as cons
    from app.services import plans_reader_tool_handlers as tool_handlers
    from app.services.plans_reader_tools import FakeLegendIndex, ToolServices

    index, texts, pdfs, missing = _text_index(bundle, backup)
    services = ToolServices()
    services.text = index
    ledger = artifacts.get("ledger") if isinstance(artifacts.get("ledger"), Mapping) else {}
    legend = [dict(e) for e in ledger.get("legend") or [] if isinstance(e, Mapping)]
    services.legend = FakeLegendIndex(legend)
    if pdfs:
        services.documents = tool_handlers.LocalDocuments(pdfs)
    scope_ctx = artifacts.get("scope_context")
    if isinstance(scope_ctx, Mapping):
        services.policy = cons._ab_policy_service(scope_ctx)
    try:
        services.rows = held_row_store(cons.inputs_from_artifacts(artifacts, bundle.run, settings=settings))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"rows unavailable ({type(exc).__name__}: {str(exc)[:120]})")
    for name in missing:
        notes.append(f"no PDF for {name}: no page text for it (text-layer fallback; quotes on its pages stay unverified)")
    return BundleServices(run_id=run_id, services=services, page_text=lambda d, p: texts.get((str(d), int(p))),
                          notes=notes, mode="text_layer")


def bundle_services(bundle: eval_bundle.RunBundle, artifacts: Mapping[str, Any], *, settings: Any, backup: Path,
                    prepare: Any = None) -> BundleServices:
    """The services a live review group of this bundle reads, bound like the run's
    (plans_reader_runs._run_services) but in memory: WS4's ab_prepare (WS1 page facts of every
    source PDF in inputs/ or the backup, merge, derive; $0) and ab_services (the WS1 text,
    census, schedules and legend adapters on those facts, the measure output, the scope policy
    and facts over the scope context, LocalDocuments over the PDFs), with the rows and census
    of the review kind over the prepared inputs. A document without a PDF keeps no facts (its
    tools answer unavailable) and is noted; a bundle with no PDF at all, or one the harness
    cannot prepare, gets fallback_services. Never raises."""
    from app.services import plans_reader_consolidator as cons

    run_id = str(bundle.run.get("id") or f"gauntlet-{bundle.run_id8}")
    notes: list[str] = []
    try:
        with _quiet("app.services.plans_reader_consolidator"):
            prepared = (prepare or cons.ab_prepare)(bundle, settings, backup_dir=backup)
            services = cons.ab_services(prepared.inputs, prepared=prepared, bundle=bundle, sb=None, backup_dir=backup)
    except Exception as exc:  # noqa: BLE001 - the fallback still reviews the pack
        notes.append(f"in-process page facts unavailable ({type(exc).__name__}: {str(exc)[:160]}); text-layer fallback")
        return fallback_services(bundle, artifacts, settings=settings, backup=backup, run_id=run_id, notes=notes)
    services.rows = held_row_store(prepared.inputs)
    notes.extend(str(n) for n in prepared.notes or [])
    store = prepared.facts_store
    if store is None or not getattr(store, "has_facts", False):
        notes.append("no source PDF has page facts: text-layer fallback for the page tools")
        fb = fallback_services(bundle, artifacts, settings=settings, backup=backup, run_id=run_id, notes=notes)
        fb.services.rows = services.rows
        for name in ("policy", "measure"):
            if type(getattr(services, name)).__name__ != "Unavailable":
                setattr(fb.services, name, getattr(services, name))
        return fb
    with_facts = {str(d.get("id")) for d in store.documents}
    for d in bundle.documents:
        if str(d.get("id")) not in with_facts:
            notes.append(f"no PDF or facts for {d.get('name')} ({str(d.get('id'))[:8]}): its page tools answer "
                         "unavailable and quotes on its pages stay unverified")
    return BundleServices(run_id=str(prepared.inputs.run_id or run_id), services=services,
                          page_text=rv.page_text_reader(store), facts_store=store, notes=notes, mode="facts")


class LocalCap:
    """The daily-cap gate of the in-process loops (ai_spend.check_cap is patched to `check_cap`
    for the run): refuses a round whose estimate would take the gauntlet past its planned
    total. The spine's cap sums plans_reader_calls, which an in-memory loop store never
    writes; the ledger guard still enforces the sweep on every call."""

    def __init__(self, limit_usd: Any) -> None:
        self.limit = Decimal(str(limit_usd))
        self.spent = Decimal("0")

    def check_cap(self, sb: Any = None, *, estimated_usd: Any = 0, now: Any = None) -> Decimal:
        from app.services import ai_spend

        estimate = max(Decimal(str(estimated_usd or 0)), Decimal("0"))
        if self.spent + estimate > self.limit:
            raise ai_spend.SpendCapReached(
                f"The live gauntlet's planned total of {_money(self.limit)} is used up ({_money(self.spent)} spent, "
                f"the next round needs about {_money(estimate)}).",
                cap_usd=self.limit, spent_usd=self.spent, estimated_usd=estimate)
        return self.limit - self.spent


def _store_spent(store: Any, key: str) -> float:
    try:
        return float(sum((Decimal(str(r.get("cost_usd") or 0)) for r in store.rounds(key)), Decimal("0")))
    except Exception:  # noqa: BLE001 - an unreadable store reports nothing, never a guess
        return 0.0


def score_findings(pack: Pack, findings: list[Mapping[str, Any]]) -> tuple[bool, int]:
    """(detected, false criticals): a seeded variant is detected by a warning or critical
    finding of an expected kind or on a touched line; a critical on a clean control is false."""
    strong = [f for f in findings if f.get("severity") in ("warning", "critical")]
    detected = pack.variant != "clean" and any(
        f.get("kind") in pack.expected_kinds or set(f.get("line_ids") or []) & set(pack.expected_lines) for f in strong)
    false_crit = sum(1 for f in findings if f.get("severity") == "critical") if pack.variant == "clean" else 0
    return detected, false_crit


def run_pack(pack: Pack, bsvc: BundleServices, *, settings: Any, route: Mapping[str, Any], budget: Any, tools: list[Any],
             sweep_id: str | None, dollars: bool = False) -> dict[str, Any]:
    """One review group on one pack, in process: the spine's builder (pack text and pre-calls),
    the WS2 loop on an in-memory store with sb=None, the spine's finalizer with the bundle's page
    text and the store's trace and recorded findings. A spend cap or ledger refusal comes back
    with `fatal` set (the run stops there); any other failure scores the pack from what the
    store holds."""
    import uuid

    from app.services import ai_spend, llm_errors
    from app.services import plans_reader_toolloop as toolloop
    from app.services.plans_reader_loop_store import FakeLoopStore, loop_key
    from app.services.plans_reader_tools import ToolContext

    run = {"id": bsvc.run_id, "eval_sweep_id": sweep_id, "settings_snapshot": {"routes": {rv.REVIEW_STEP: dict(route)}}}
    group_id = str(uuid.uuid4())
    group = {"id": group_id, "run_id": run["id"], "kind": rv.REVIEW_KIND, "route": dict(route),
             "loop_state": {rv.REVIEW_KIND: pack.pack}}
    store = FakeLoopStore(run_id=run["id"])
    key = loop_key(group_id=group_id)
    started = time.time()
    result = None
    error: str | None = None
    fatal = False
    with bsvc.serving():
        try:
            parts, precalls = rv.build_review_loop(None, run, group, settings)
            tool_ctx = ToolContext(run_id=run["id"], kind=rv.REVIEW_KIND, sb=None, group_id=group_id, services=bsvc.services)
            ctx = toolloop.LoopContext(
                run=run, kind=rv.REVIEW_KIND, feature=rv.REVIEW_FEATURE, system=rv.REVIEW_SYSTEM, user_parts=parts,
                tools=tools, tool_ctx=tool_ctx, store=store, sb=None, response_schema=REVIEW_SCHEMA,
                schema_name=REVIEW_SCHEMA_NAME, budget=budget, route=dict(route), step=rv.REVIEW_STEP, group_id=group_id,
                precalls=precalls, settings=settings, run_spent_usd=Decimal("0"),
                timeout=getattr(settings, "plans_reader_call_timeout_seconds", None),
            )
            result = toolloop.run_loop(ctx)
        except (ai_spend.SpendCapReached, llm_errors.EvalBudgetRefused) as exc:
            error, fatal = f"{type(exc).__name__}: {str(exc)[:300]}", True
        except toolloop.LoopFailed as exc:
            error = f"loop_failed: {str(exc)[:300]}"
        except Exception as exc:  # noqa: BLE001 - a failed pack is scored from what the store holds
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        output = rv.finalize_review_loop(result.parsed if result is not None else None, result, sb=None, run=run,
                                         group=group, page_text=bsvc.page_text, store=store)
    findings = [dict(f) for f in output.get("findings") or []]
    if not dollars:
        for f in findings:
            f["impact_usd"] = None
    detected, false_crit = score_findings(pack, findings)
    spent = float(result.spent_usd or 0) if result is not None else _store_spent(store, key)
    stored_calls = len([c for c in store.tool_calls(key) if int(c.get("round_no") or 0) > 0])
    return {
        "name": pack.name, "variant": pack.variant, "expected_kinds": list(pack.expected_kinds),
        "expected_lines": list(pack.expected_lines), "detected": detected, "false_criticals": false_crit,
        "findings": findings, "stats": output.get("stats"), "summary": output.get("summary"),
        "stop_reason": (result.stop_reason if result is not None else None) or ("error" if error else None),
        "error": error, "fatal": fatal, "seconds": round(time.time() - started, 1),
        "rounds": int(getattr(result, "rounds", 0) or 0) if result is not None else len(
            [r for r in store.rounds(key) if r.get("status") == "done"]),
        "tool_calls": int(getattr(result, "tool_calls", 0) or 0) if result is not None else stored_calls,
        "stored_tool_calls": stored_calls, "services": bsvc.mode, "spent_usd": spent,
    }


def summarize(results: list[dict[str, Any]], *, sweep_id: str | None, route: Mapping[str, Any]) -> dict[str, Any]:
    variants = [r for r in results if r["variant"] != "clean"]
    clean = [r for r in results if r["variant"] == "clean"]
    detected = sum(1 for r in variants if r["detected"])
    false_crit = sum(r["false_criticals"] for r in clean)
    return {
        "variants": len(variants), "detected": detected,
        "detection_rate": round(detected / len(variants), 4) if variants else None,
        "clean_controls": len(clean), "false_criticals": false_crit,
        "false_criticals_per_control": round(false_crit / len(clean), 4) if clean else None,
        "errors": sum(1 for r in results if r.get("error")), "sweep_id": sweep_id, "route": dict(route),
    }


def run_packs(packs: list[Pack], services: Mapping[str, BundleServices], *, settings: Any, route: Mapping[str, Any],
              budget: Any, sweep_id: str | None, limit_usd: Any, dollars: bool = False, tools: list[Any] | None = None,
              log: Any = print) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run every pack in process (run_pack) under LocalCap(limit_usd) and the eval ledger's
    round hook (each round's estimate is the reserve of its call); stops at the first spend
    cap or ledger refusal."""
    from unittest import mock

    from app.services import ai_spend, eval_ledger

    if tools is None:
        register_review_tools()
        tools = offered_review_tools(settings)
    cap = LocalCap(limit_usd)
    results: list[dict[str, Any]] = []
    previous = ai_spend.ledger_hook()
    ai_spend.set_ledger_hook(eval_ledger.gate_round_hook)
    try:
        with mock.patch.object(ai_spend, "check_cap", cap.check_cap):
            for pack in packs:
                if cap.spent + Decimal(str(budget.max_usd)) > cap.limit:
                    log(f"  {pack.name}: skipped (its cap no longer fits the planned total)")
                    continue
                res = run_pack(pack, services[pack.bundle], settings=settings, route=route, budget=budget, tools=tools,
                               sweep_id=sweep_id, dollars=dollars)
                cap.spent += Decimal(str(res["spent_usd"] or 0))
                if not dollars:
                    res["spent_usd"] = None
                results.append(res)
                verdict = "detected" if res["detected"] else "missed" if pack.variant != "clean" else "clean"
                log(f"  {pack.name}: {verdict}; {len(res['findings'])} findings, {res['false_criticals']} false critical, "
                    f"{res['tool_calls']} tool calls, stop {res['stop_reason']}" + (f" ({res['error']})" if res["error"] else ""))
                if res["fatal"]:
                    log(f"stopping: {res['error']}")
                    break
    finally:
        ai_spend.set_ledger_hook(previous)
    return results, summarize(results, sweep_id=sweep_id, route=route)


def run_live(packs: list[Pack], bundles: dict[str, eval_bundle.RunBundle], artifacts: Mapping[str, Mapping[str, Any]], *,
             backup: Path, out_dir: Path, eval_root: Path, sweep: str = "open", pack_cap_usd: Any = None, dollars: bool = False,
             settings: Any = None, prepare: Any = None) -> dict[str, Any]:
    """The live gauntlet: the plan (printed before anything else), the sweep armed and checked
    (arm_sweep; refused when the summed caps exceed what it has left), the services of every
    bundle built at $0, then run_packs. Raises GauntletRefused before the first call."""
    from app.core.config import get_settings

    s = settings or get_settings()
    register_review_tools()
    tool_specs = offered_review_tools(s)
    route = review_route(s)
    budget = pack_budget(s, route, pack_cap_usd)
    plan = build_live_plan(packs, settings=s, route=route, budget=budget, tools=tool_specs)
    for line in format_live_plan(plan):
        print(line)
    row = arm_sweep(sweep, eval_root=eval_root, settings=s)
    s = get_settings() if settings is None else s
    remaining = Decimal(str(row["remaining_usd"]))
    print(f"sweep {row['id']} ({row.get('label') or ''}): {_money(remaining)} left of {_money(row.get('budget_usd') or 0)}")
    check_plan(plan, remaining)
    (out_dir / "live_plan.json").write_text(json.dumps({**plan, "sweep_id": row["id"], "remaining_usd": remaining},
                                                       indent=1, default=str) + "\n")
    services: dict[str, BundleServices] = {}
    for key in sorted({p.bundle for p in packs}):
        bsvc = bundle_services(bundles[key], artifacts.get(key) or {}, settings=s, backup=backup, prepare=prepare)
        services[key] = bsvc
        print(f"  services {key}: {bsvc.mode}" + "".join(f"\n    {n}" for n in bsvc.notes))
    results, summary = run_packs(packs, services, settings=s, route=route, budget=budget, sweep_id=str(row["id"]),
                                 limit_usd=plan["cap_usd"], dollars=dollars, tools=tool_specs)
    notes = {k: v.notes for k, v in services.items()}
    (out_dir / "gauntlet_results.json").write_text(json.dumps({"summary": summary, "plan": plan, "service_notes": notes,
                                                               "results": results}, indent=1, default=str) + "\n")
    return summary


# Main ------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build reviewer gauntlet packs from pre-core bundles; run the reviewer inside a sweep.")
    p.add_argument("--bundle", action="append", default=[], type=Path, help="a bundle directory (repeatable); default: EVAL_ROOT/bundles/*/*")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=eval_bundle.DEFAULT_BACKUP_DIR)
    p.add_argument("--max-bundles", type=int, default=MAX_BUNDLES)
    p.add_argument("--variants-per-bundle", type=int, default=VARIANTS_PER_BUNDLE)
    p.add_argument("--classes", default=",".join(DEFECT_CLASSES), help="defect classes to seed, in order")
    p.add_argument("--out", type=Path, default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="build packs and variants only ($0, the default)")
    mode.add_argument("--live", action="store_true", help="run the reviewer in process inside the open WS7 dev_probe sweep (wave 4)")
    p.add_argument("--sweep", default=None, help="live: the open sweep id, or 'open' (default: EVAL_SWEEP_ID, else the open sweep)")
    p.add_argument("--pack-cap-usd", default=None, help="live: lower each pack's review loop cap to this many dollars")
    p.add_argument("--dollars", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.eval_root) if args.eval_root else eval_manifest.eval_root()
    out = args.out or root / "reports" / "gauntlet" / time.strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "packs").mkdir(exist_ok=True)
    classes = tuple(c.strip() for c in args.classes.split(",") if c.strip() in EXPECTED_KINDS)
    register_real_stages()
    bands = None
    try:
        latest = bands_mod.latest_bands_path(root)
        bands = bands_mod.load_bands(latest, root) if latest else None
    except Exception as exc:  # noqa: BLE001
        print(f"bands unavailable: {exc}")
    dirs = discover_bundles(root / "bundles", args.bundle)[: max(1, args.max_bundles)]
    packs: list[Pack] = []
    bundles: dict[str, eval_bundle.RunBundle] = {}
    skipped: list[tuple[str, str]] = []
    artifacts: dict[str, Any] = {}
    for d in dirs:
        try:
            got, why = packs_for_bundle(root, d, bands, backup=args.backup, per_bundle=args.variants_per_bundle, classes=classes,
                                        out_dir=out, keep=artifacts)
        except Exception as exc:  # noqa: BLE001 - a broken bundle is skipped, not fatal
            skipped.append((str(d), f"error: {str(exc)[:200]}"))
            continue
        if not got:
            skipped.append((str(d), why))
            continue
        bundles[got[0].bundle] = eval_bundle.load_bundle(d)
        packs += got
    plan = {"version": GAUNTLET_VERSION, "bundles": [str(d) for d in dirs], "skipped": skipped, "classes": list(classes),
            "packs": [p.plan_row() for p in packs], "built_at": eval_bundle.now_pacific_iso(), "live": bool(args.live)}
    (out / "gauntlet_plan.json").write_text(json.dumps(plan, indent=1, default=str) + "\n")
    for p in packs:
        doc = copy.deepcopy(p.pack)
        if not args.dollars:
            doc.pop("total_usd", None)
            for ln in doc.get("lines") or []:
                ln.pop("ext_usd", None)
            for f in doc.get("rule_findings") or []:
                f["impact_usd"] = None
            doc["bands"] = [{**b, **({"value": None, "p10": None, "p25": None, "p50": None, "p75": None, "p90": None}
                                     if b.get("metric") in bands_mod.MONEY_METRICS else {})} for b in doc.get("bands") or []]
        (out / "packs" / f"{p.name}.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    print(f"gauntlet packs -> {out}")
    print("| pack | variant | lines | rule findings | bands out | expected kinds | est input tokens |")
    print("|---|---|---|---|---|---|---|")
    for p in packs:
        row = p.plan_row()
        print(f"| {row['name']} | {row['variant']} | {row['lines']} | {row['rule_findings']} | {row['bands_out']} | "
              f"{', '.join(row['expected_kinds']) or '-'} | {row['est_input_tokens']} |")
    for d, why in skipped:
        print(f"skipped {d}: {why}")
    n_var = sum(1 for p in packs if p.variant != "clean")
    print(f"{len(bundles)} bundles, {len(packs)} packs ({len(packs) - n_var} clean controls, {n_var} variants); "
          f"est input tokens {sum(p.plan_row()['est_input_tokens'] for p in packs)}; "
          f"{'no model call (dry run)' if not args.live else 'live'}")
    if not args.live:
        return 0
    try:
        summary = run_live(packs, bundles, artifacts, backup=args.backup, out_dir=out, eval_root=root,
                           sweep=args.sweep or os.environ.get("EVAL_SWEEP_ID") or "open", pack_cap_usd=args.pack_cap_usd,
                           dollars=args.dollars)
    except GauntletRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=1, default=str))
    print(f"results: {out / 'gauntlet_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
