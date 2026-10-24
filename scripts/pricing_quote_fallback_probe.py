"""WS6 live probe: the vendor quote LLM fallback on the quotes the text
parser cannot read (docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS6 "quote
fallback $4 (only scanned or unparseable quotes reach it)"; 0.4 ledger rules
2, 3 and 6).

How the parser decides (app/services/vendor_quotes.py): `parse_quote_pdf`
reads the text layer (`extract_pages`) and runs `parse_quote_text`. A PDF it
cannot open is `failed` (never handed to the fallback). A quote with no
text layer, a sparse one (under MIN_TEXT_CHARS with no QTY window and no
total), no item and no amount, or items but no amount is `needs_review`;
only `needs_review` reaches the fallback, and only when the fallback is
enabled and a caller is injected (production: QUOTE_LINES_LLM_FALLBACK_ENABLED
and `vendor_quotes.llm_quote_fallback`, ONE `pricing_quote` turn, step
`quote`, through `llm.complete_turn` with up to 4 page images and up to
20,000 characters of text).

Quotes scanned: the backup's vendor quote PDFs of the non-holdout golden
projects (tiers smoke, core and pool; `eval_manifest.excluded_projects(
"pricing_library")` dropped), found as the replay finds them,
`pricing_run.backup_quote_files`: the files the WS0 reference lists under
g3_materials (selected and alternatives) for a case with a reference, else
the project's `quote` rows whose file name names a category. Each is
classified at $0 as text_parsed, needs_fallback, failed or missing.

Live: the fallback runs on the needs_fallback quotes only, one at a time in
selection order (nothing interleaved), each through the production caller
exactly as `parse_quote_pdf(..., fallback_enabled=True)` runs it, while the
dollars left cover the call's ledger reserve (request input plus
--reserve-output-tokens of output; the request itself is unchanged). A
ledger refusal stops the run. Recorded per quote: status and parser after
the fallback, parsed pre-tax total, line count, cost, and, where the WS0
reference has the quote, the comparison against the estimator's quote
amount (the selected one flagged).

Dollar values (parsed totals, reference amounts) are written only under
EVAL_ROOT (results.json, table.md, plan.json); the console shows counts,
match flags and the build's own API cost.

Usage (from bdr_be):

    # $0: classification table and the estimate; no sweep, no call.
    uv run python scripts/pricing_quote_fallback_probe.py --dry-run
    # Live (wave 4 only): inside an open sweep of purpose dev_probe on WS6.
    PLANS_READER_ENABLED=true uv run python scripts/pricing_quote_fallback_probe.py --sweep <id|open>

A live run refuses without --sweep, with a sweep that is not open, not of
purpose dev_probe or not on WS6, with an unpriced model, with
PLANS_READER_ENABLED off, against a database other than dev, and when not
even the first needs_fallback quote fits what the sweep has left (capped
by --max-usd, default $4). It arms EVAL_SWEEP_ID, AI_EVAL_LEDGER_ENFORCE=true
and LLM_CASSETTE_MODE=record_missing before its first call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for _p in (ROOT, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import eval_probe_common as common  # noqa: E402
from eval_probe_common import ProbeError  # noqa: E402

NAME = "pricing_quote_fallback_probe"
RESULTS_SCHEMA = "bdr.eval.pricing_quote_fallback_probe/1"
WORKSTREAM = "WS6"
BUDGET_USD = Decimal("4")
FEATURE = "pricing_quote"
STEP = "quote"
EXCLUSION_PURPOSE = "pricing_library"
TIERS: tuple[str, ...] = common.PROBE_TIERS
CLASS_BY_STATUS: dict[str, str] = {"parsed": "text_parsed", "needs_review": "needs_fallback", "failed": "failed"}
CLASSES: tuple[str, ...] = ("text_parsed", "needs_fallback", "failed", "missing")
# Planning figure: the strict-JSON answer (about 40 tokens per line) plus
# the reasoning of a turn at the default effort. The reserve uses
# --reserve-output-tokens instead.
EXPECTED_OUTPUT_TOKENS = 4000
DEFAULT_RESERVE_OUTPUT_TOKENS = 8000
REF_MATCH_FRACTION = Decimal("0.01")
# Files in the quote category that are not vendor quotes: G3's own RFQ
# sheets, markups and other answer-named files (eval_manifest.ANSWER_NAME_RE
# words, matched across underscores), drawing sheets (first page at least
# 24 x 18 in) and byte-identical copies. The text parser marks them
# needs_review like a scanned quote, so production would hand them to the
# fallback too; the probe leaves them out of the calls unless
# --include-non-quotes (they are counted in the plan).
NON_QUOTE_NAME_RE = re.compile(
    r"(?<![A-Z])(RFQ|MARK ?UP|BOQ|TAKE ?OFF|ESTIMATE|PROPOSAL|BID RECAP)(?![A-Z])", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Quotes in the backup
# ---------------------------------------------------------------------------


@dataclass
class QuoteFile:
    case: str
    project_id: str
    tier: str
    quote_id: str
    category: str
    filename: str
    path: Path
    sha256: str | None = None
    ref: dict[str, Any] | None = None


def load_reference(eval_root: Path, case: str) -> dict[str, Any] | None:
    path = eval_root / "references" / case / "reference.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def reference_quotes(reference: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """quote_id -> the reference's quote facts (amount, tax, selected)."""
    out: dict[str, dict[str, Any]] = {}
    for mat in (reference or {}).get("g3_materials") or ():
        if not isinstance(mat, Mapping):
            continue
        for role, quote in [("selected", mat.get("selected")), *(("alternative", q) for q in mat.get("alternatives") or ())]:
            if not isinstance(quote, Mapping) or not quote.get("quote_id"):
                continue
            out.setdefault(str(quote["quote_id"]), {
                "role": role, "category": mat.get("category"), "amount": quote.get("amount"),
                "tax_included": quote.get("tax_included"), "tax_rate": quote.get("tax_rate"),
                "is_selected": bool(quote.get("is_selected")) or role == "selected",
            })
    return out


def collect_quotes(data: Any, eval_root: Path, *, tiers: Sequence[str] = TIERS,
                   only: Sequence[str] | None = None) -> tuple[list[QuoteFile], list[dict[str, Any]]]:
    """Every quote PDF of the candidate projects (selection order, then the
    order backup_quote_files gives) and the projects passed over."""
    from app.services import eval_manifest as em
    from app.services import pricing_run

    excluded = em.excluded_projects(EXCLUSION_PURPOSE)
    storage = Path(data.backup_dir) / "storage" / "project-files"
    quotes: list[QuoteFile] = []
    passed: list[dict[str, Any]] = []
    for case, pid, tier, why in common.ordered_candidates(data, tiers):
        if not common.matches_only(case, pid, only):
            continue
        if pid is None:
            passed.append({"case": case, "tier": tier, "why": why})
            continue
        if pid in excluded or pid in em.HOLDOUT_IDS:
            passed.append({"case": case, "tier": tier, "why": "excluded (holdout)"})
            continue
        reference = load_reference(eval_root, case) if tier in ("smoke", "core") else None
        refs = reference_quotes(reference)
        files = pricing_run.backup_quote_files(reference, data.backup_dir, pid)
        kept = 0
        for f in files:
            name = str(f.get("filename") or Path(str(f["storage_path"])).name)
            if not name.lower().endswith(".pdf"):
                continue
            kept += 1
            quotes.append(QuoteFile(
                case=case, project_id=pid, tier=tier, quote_id=str(f["quote_id"]), category=str(f["category"]),
                filename=name, path=storage / str(f["storage_path"]), sha256=f.get("sha256"),
                ref=refs.get(str(f["quote_id"])),
            ))
        if not kept:
            passed.append({"case": case, "tier": tier, "why": "no vendor quote PDF found"})
    return quotes, passed


# ---------------------------------------------------------------------------
# Classification and plan ($0)
# ---------------------------------------------------------------------------


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def ref_compare(amount_pretax: Any, ref: Mapping[str, Any] | None) -> dict[str, Any]:
    """The parsed pre-tax total against the reference's quote amount: the
    smallest relative difference to the amount as recorded or, when the
    record says tax is included, to the amount less its tax. Dollar values
    in the returned dict stay under EVAL_ROOT."""
    if not ref:
        return {"ref_role": None, "ref_match": None, "delta_pct": None}
    amount = _dec(amount_pretax)
    ref_amount = _dec(ref.get("amount"))
    out: dict[str, Any] = {"ref_role": ref.get("role"), "ref_selected": bool(ref.get("is_selected")),
                           "ref_amount": ref_amount, "ref_tax_included": ref.get("tax_included"),
                           "ref_match": None, "delta_pct": None}
    if amount is None or ref_amount is None or ref_amount <= 0:
        return out
    targets = [ref_amount]
    rate = _dec(ref.get("tax_rate"))
    if ref.get("tax_included") and rate:
        targets.append(ref_amount / (1 + rate / 100))
    best = min(abs(amount - t) / t for t in targets if t > 0)
    out["delta_pct"] = float((best * 100).quantize(Decimal("0.01")))
    out["ref_match"] = best <= REF_MATCH_FRACTION
    return out


def classify(qf: QuoteFile) -> tuple[dict[str, Any], bytes | None, list[str] | None]:
    """(row, pdf bytes, page texts) of one quote by the text parser alone."""
    from app.services import vendor_quotes as vq

    row: dict[str, Any] = {"case": qf.case, "project_id": qf.project_id, "tier": qf.tier, "quote_id": qf.quote_id,
                           "category": qf.category, "filename": qf.filename, "ref_role": (qf.ref or {}).get("role")}
    if not qf.path.is_file():
        row.update(cls="missing", status=None, pages=None, lines=0, amount_found=False, warnings=["not_in_backup"])
        return row, None, None
    data = qf.path.read_bytes()
    row["sha256"] = hashlib.sha256(data).hexdigest()
    try:
        pages = vq.extract_pages(data)
    except Exception:  # noqa: BLE001 - parse_quote_pdf reports it as failed below
        pages = None
    parsed = vq.parse_quote_pdf(data, filename=qf.filename)
    row.update(
        cls=CLASS_BY_STATUS.get(parsed.status, parsed.status), status=parsed.status, parser=parsed.parser,
        pages=parsed.pages or (len(pages) if pages is not None else None), text_quality=parsed.text_quality,
        lines=len(parsed.lines), amount_found=parsed.amount_pretax is not None, warnings=list(parsed.warnings),
        text_amount_pretax=parsed.amount_pretax, text_vendor=parsed.vendor,
    )
    cmp = ref_compare(parsed.amount_pretax, qf.ref) if parsed.status == "parsed" else ref_compare(None, qf.ref)
    row.update({f"text_{k}" if k in ("ref_match", "delta_pct") else k: v for k, v in cmp.items()})
    return row, data, pages


def first_page_big(data: bytes) -> bool:
    """True when the first page is a drawing sheet (eval_manifest.is_big_sheet)."""
    import io

    import pypdfium2 as pdfium

    from app.services import eval_manifest as em
    from app.services import pdf_split

    try:
        with pdf_split._PDFIUM_LOCK:
            doc = pdfium.PdfDocument(io.BytesIO(data))
            try:
                if not len(doc):
                    return False
                page = doc[0]
                try:
                    w, h = page.get_size()
                finally:
                    page.close()
            finally:
                doc.close()
    except Exception:  # noqa: BLE001 - an unreadable file is not a drawing
        return False
    return em.is_big_sheet(w / 72.0, h / 72.0)


def skip_reason(row: Mapping[str, Any], data: bytes, seen_sha: Mapping[str, str]) -> str | None:
    """Why a needs_fallback file is left out of the probe's calls, or None."""
    if NON_QUOTE_NAME_RE.search(str(row.get("filename") or "")):
        return "named as an RFQ, markup or other answer file (not a vendor quote)"
    sha = row.get("sha256")
    if sha and sha in seen_sha:
        return f"duplicate of {seen_sha[sha][:8]}"
    if first_page_big(data):
        return "drawing sheet (first page at least 24 x 18 in)"
    return None


FallbackFactory = Callable[[Any], Any]


def _default_fallback_factory(settings: Any) -> Any:
    from app.services import vendor_quotes as vq

    def _call(pdf_bytes: bytes, pages: Sequence[str]) -> Any:
        return vq.llm_quote_fallback(pdf_bytes, pages, settings=settings)

    return _call


def estimate_fallback(data: bytes, pages: Sequence[str], *, settings: Any, route: Any, reserve_output_tokens: int,
                      factory: FallbackFactory = _default_fallback_factory) -> dict[str, Any]:
    """The production fallback request built and captured (nothing sent):
    its input tokens, expected dollars and ledger reserve."""
    with common.captured_requests() as seen:
        try:
            factory(settings)(data, list(pages))
        except common.PlanningCapture:
            pass
    if not seen:
        return {"input_tokens": None, "images": None, "expected_usd": None, "reserve_usd": None}
    req = seen[0]
    tokens = common.request_input_tokens(req, route.provider)
    images = sum(1 for turn in req.messages for p in getattr(turn, "parts", ()) if getattr(p, "png", None))
    return {
        "input_tokens": tokens, "images": images, "max_tokens": req.max_tokens,
        "expected_usd": common.estimate_usd(route.model, tokens, EXPECTED_OUTPUT_TOKENS),
        "reserve_usd": common.estimate_usd(route.model, tokens, reserve_output_tokens),
    }


@dataclass
class PlanState:
    rows: list[dict[str, Any]] = field(default_factory=list)
    blobs: dict[str, tuple[bytes, list[str], QuoteFile]] = field(default_factory=dict)


def build_plan(quotes: Sequence[QuoteFile], passed: Sequence[Mapping[str, Any]], *, settings: Any,
               budget_usd: Decimal = BUDGET_USD, reserve_output_tokens: int = DEFAULT_RESERVE_OUTPUT_TOKENS,
               max_quotes: int | None = None, include_non_quotes: bool = False,
               factory: FallbackFactory = _default_fallback_factory,
               log: Callable[[str], None] = lambda _m: None) -> tuple[dict[str, Any], PlanState]:
    from app.services import llm_pricing
    from app.services import plans_reader_model_routes as model_routes

    route = model_routes.resolve_step(STEP, settings)
    state = PlanState()
    problems: list[str] = []
    seen_sha: dict[str, str] = {}
    for qf in quotes:
        try:
            row, data, pages = classify(qf)
        except Exception as exc:  # noqa: BLE001 - one bad file never stops the scan
            problems.append(f"{qf.case} {qf.filename}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        row["skip_reason"] = None
        if row["cls"] == "needs_fallback" and data is not None and not include_non_quotes:
            row["skip_reason"] = skip_reason(row, data, seen_sha)
        if row.get("sha256"):
            seen_sha.setdefault(row["sha256"], qf.quote_id)
        if row["cls"] == "needs_fallback" and data is not None and not row["skip_reason"]:
            log(f"estimating the fallback request for {qf.case} {qf.filename[:50]}")
            try:
                row.update(estimate_fallback(data, pages or [], settings=settings, route=route,
                                             reserve_output_tokens=reserve_output_tokens, factory=factory))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{qf.case} {qf.filename}: request build failed: {type(exc).__name__}: {str(exc)[:160]}")
            state.blobs[qf.quote_id] = (data, list(pages or []), qf)
        state.rows.append(row)
    fallback_rows = [r for r in state.rows if r["cls"] == "needs_fallback" and not r.get("skip_reason")]
    candidates = fallback_rows[:max_quotes] if max_quotes is not None else fallback_rows
    planned: list[str] = []
    running = Decimal("0")
    for r in candidates:
        est = r.get("expected_usd")
        if est is None:
            continue
        if running + Decimal(str(est)) > budget_usd:
            break
        running += Decimal(str(est))
        planned.append(r["quote_id"])
    all_expected = [r.get("expected_usd") for r in candidates]
    counts = Counter(r["cls"] for r in state.rows)
    comparable = [r for r in state.rows if r.get("text_ref_match") is not None]
    plan = {
        "name": NAME,
        "route": {"step": STEP, "feature": FEATURE, "provider": route.provider, "model": route.model,
                  "effort": route.effort, "priced": llm_pricing.price_for(route.model) is not None},
        "quotes": state.rows,
        "passed_over": list(passed),
        "problems": problems,
        "counts": {c: counts.get(c, 0) for c in CLASSES},
        "left_out": dict(Counter(str(r["skip_reason"]).split(" of ")[0] for r in state.rows if r.get("skip_reason"))),
        "projects": len({r["case"] for r in state.rows}),
        "text_ref_compared": len(comparable),
        "text_ref_matched": sum(1 for r in comparable if r["text_ref_match"]),
        "fallback_candidates": [r["quote_id"] for r in candidates],
        "planned": planned,
        "planned_expected_usd": running,
        "all_expected_usd": None if any(v is None for v in all_expected) else sum(all_expected, Decimal("0")),
        "budget_usd": budget_usd,
        "reserve_output_tokens": reserve_output_tokens,
        "model_calls_made": 0,
    }
    return plan, state


def _flag(v: Any, pct: Any = None) -> str:
    if v is None:
        return "-"
    return "yes" if v else (f"no ({pct}%)" if pct is not None else "no")


def format_plan(plan: Mapping[str, Any], *, verbose: bool = False) -> list[str]:
    """The console view: no dollar values of a project, only counts, flags
    and the build's own API estimate."""
    r = plan["route"]
    c = plan["counts"]
    lines = [
        f"quote fallback probe plan: {len(plan['quotes'])} quote PDFs in {plan['projects']} projects; "
        f"text_parsed {c['text_parsed']}, needs_fallback {c['needs_fallback']}, failed {c['failed']}, "
        f"missing {c['missing']} (feature {r['feature']}, step {r['step']}: {r['provider']} {r['model']} "
        f"effort {r['effort'] or 'default'}" + ("" if r["priced"] else ", NOT PRICED: a live run refuses it") + ")",
        "",
        "case | tier | quote PDFs | text_parsed | needs_fallback | failed | missing | text total vs reference (match/compared)",
    ]
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    for q in plan["quotes"]:
        by_case.setdefault(q["case"], []).append(q)
    for case, rows in by_case.items():
        cnt = Counter(q["cls"] for q in rows)
        comp = [q for q in rows if q.get("text_ref_match") is not None]
        lines.append(" | ".join([case, rows[0]["tier"], str(len(rows)), *(str(cnt.get(k, 0)) for k in CLASSES),
                                 f"{sum(1 for q in comp if q['text_ref_match'])}/{len(comp)}" if comp else "-"]))
    detail = [q for q in plan["quotes"] if verbose or q["cls"] != "text_parsed"]
    if detail:
        lines += ["", "case | quote | category | file | pages | class | text quality | lines | amount found | ref | input tok | images | est | reserve | warnings or left out"]
        for q in detail:
            lines.append(" | ".join([
                q["case"], q["quote_id"][:8], q["category"], q["filename"][:40], str(q.get("pages")), q["cls"],
                str(q.get("text_quality") or "-"), str(q.get("lines")), "yes" if q.get("amount_found") else "no",
                q.get("ref_role") or "-",
                str(q.get("input_tokens") if q.get("input_tokens") is not None else "-"),
                str(q.get("images") if q.get("images") is not None else "-"),
                common.money(q.get("expected_usd")) if q.get("expected_usd") is not None else "-",
                common.money(q.get("reserve_usd")) if q.get("reserve_usd") is not None else "-",
                f"LEFT OUT: {q['skip_reason']}" if q.get("skip_reason") else ", ".join(q.get("warnings") or [])[:80],
            ]))
    for p in plan["passed_over"]:
        lines.append(f"passed over: {p['case']} ({p['tier']}): {p['why']}")
    for prob in plan["problems"]:
        lines.append(f"problem: {prob}")
    lines.append(
        f"text parser vs reference quote amounts: {plan['text_ref_matched']} of {plan['text_ref_compared']} "
        f"within {int(REF_MATCH_FRACTION * 100)}%"
    )
    if plan["left_out"]:
        lines.append("needs_fallback left out of the calls: " + ", ".join(f"{k} {v}" for k, v in plan["left_out"].items()))
    lines.append(
        f"fallback: {len(plan['fallback_candidates'])} candidates, expected {common.money(plan['all_expected_usd'])} for all; "
        f"{len(plan['planned'])} fit the {common.money(plan['budget_usd'])} budget at expected "
        f"{common.money(plan['planned_expected_usd'])} (reserve per call: input + {plan['reserve_output_tokens']} output tokens)"
    )
    lines.append("model calls made: 0")
    return lines


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


def run_quote(row: Mapping[str, Any], data: bytes, qf: QuoteFile, *, settings: Any,
              factory: FallbackFactory = _default_fallback_factory) -> dict[str, Any]:
    """One needs_fallback quote through parse_quote_pdf with the fallback
    enabled and the production caller (metered)."""
    from app.services import vendor_quotes as vq

    meter = common.CallMeter(row.get("reserve_usd"))
    parsed = vq.parse_quote_pdf(data, filename=qf.filename, fallback_enabled=True, llm_fallback=meter.wrap(factory(settings)))
    rec = meter.record
    out = {
        "case": qf.case, "project_id": qf.project_id, "tier": qf.tier, "quote_id": qf.quote_id,
        "category": qf.category, "filename": qf.filename,
        "status_before": row.get("status"), "status_after": parsed.status, "parser": parsed.parser,
        "amount_pretax": parsed.amount_pretax, "tax_included": parsed.tax_included, "vendor": parsed.vendor,
        "lines": len(parsed.lines), "fixture_lines": len(parsed.fixture_lines()),
        "warnings": list(parsed.warnings), "called": rec.called, "cost_usd": rec.cost_usd,
        "replayed": rec.replayed, "input_tokens": rec.input_tokens, "output_tokens": rec.output_tokens,
        "seconds": rec.seconds, "reserve_usd": rec.reserve_usd, "error": rec.error,
        "ledger_refused": rec.ledger_refused,
        **ref_compare(parsed.amount_pretax, qf.ref),
    }
    return out


def format_table(results: Sequence[Mapping[str, Any]], skipped: Sequence[Mapping[str, Any]]) -> list[str]:
    """table.md under EVAL_ROOT (dollar values allowed there)."""
    lines = ["| case | quote | category | before | after | parser | lines | parsed pre-tax | reference | delta % | cost | note |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        ref = "-"
        if r.get("ref_role"):
            ref = f"{r['ref_role']}{' (selected)' if r.get('ref_selected') else ''} {common.money(r.get('ref_amount'))}"
        note = []
        if r.get("replayed"):
            note.append("cassette")
        if r.get("error"):
            note.append(str(r["error"])[:80])
        lines.append("| " + " | ".join([
            r["case"], r["quote_id"][:8], r["category"], str(r.get("status_before")), str(r.get("status_after")),
            str(r.get("parser")), str(r.get("lines")), common.money(r.get("amount_pretax")), ref,
            "-" if r.get("delta_pct") is None else str(r["delta_pct"]), f"{r.get('cost_usd') or 0:.4f}", "; ".join(note),
        ]) + " |")
    for s in skipped:
        lines.append(f"| {s['case']} | {s['quote_id'][:8]} | | | | | | | | | | skipped: {s['why']} |")
    spent = sum(float(r.get("cost_usd") or 0) for r in results)
    parsed = sum(1 for r in results if r.get("status_after") == "parsed")
    compared = [r for r in results if r.get("ref_match") is not None]
    lines += ["", f"fallback calls {sum(1 for r in results if r.get('called'))}, parsed after the fallback {parsed}, "
                  f"reference matches {sum(1 for r in compared if r['ref_match'])} of {len(compared)}, spent ${spent:.4f}"]
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WS6 probe: the vendor quote LLM fallback on unparseable quotes.")
    p.add_argument("--dry-run", action="store_true", help="classification table and estimate; no call ($0)")
    p.add_argument("--sweep", default=None, help="open sweep id of purpose dev_probe on WS6, or 'open' (live only)")
    p.add_argument("--only", default=None, help="comma list of case ids or project id prefixes")
    p.add_argument("--tiers", default=",".join(TIERS), help="comma list of smoke, core, pool (holdout never)")
    p.add_argument("--max-quotes", type=int, default=None, help="cap on fallback calls")
    p.add_argument("--max-usd", type=float, default=float(BUDGET_USD), help="cap on this run's spend (default 4)")
    p.add_argument("--include-non-quotes", action="store_true",
                   help="also call on RFQ-named, markup, drawing-sheet and duplicate files the parser cannot read")
    p.add_argument("--reserve-output-tokens", type=int, default=DEFAULT_RESERVE_OUTPUT_TOKENS,
                   help="output tokens in each call's ledger reserve (the request itself is unchanged)")
    p.add_argument("--verbose", action="store_true", help="list every quote, not only the ones the parser cannot read")
    p.add_argument("--no-cassette", action="store_true", help="do not use cassettes (default record_missing, rule 6)")
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--backup", type=Path, default=None, help="production backup dir (default eval_reference.DEFAULT_BACKUP_DIR)")
    p.add_argument("--out", type=Path, default=None,
                   help="output dir (default EVAL_ROOT/sweeps/<sweep>/pricing_quote_fallback_probe_<time>); "
                        "a dry run writes plan.json there only when given (keep it under EVAL_ROOT)")
    return p


def main(argv: list[str] | None = None, *, settings: Any = None, data: Any = None,
         fallback_factory: FallbackFactory | None = None) -> int:
    args = build_parser().parse_args(argv)
    from app.core.config import get_settings
    from app.services.eval_reference import DEFAULT_BACKUP_DIR, BackupData

    s = settings or get_settings()
    eval_root = common.eval_root_for(args.eval_root, s)
    factory = fallback_factory or _default_fallback_factory
    try:
        if args.max_usd <= 0:
            raise ProbeError("--max-usd must be positive")
        if args.max_quotes is not None and args.max_quotes < 1:
            raise ProbeError("--max-quotes must be at least 1")
        if args.reserve_output_tokens < 1:
            raise ProbeError("--reserve-output-tokens must be positive")
        tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
        if "holdout" in tiers:
            raise ProbeError("the holdout is never probed (spec 8.1)")
        data = data or BackupData(args.backup or DEFAULT_BACKUP_DIR)
        quotes, passed = collect_quotes(data, eval_root, tiers=tiers, only=args.only.split(",") if args.only else None)
        if not quotes:
            raise ProbeError("no vendor quote PDF found")
        budget = Decimal(str(args.max_usd))
        plan, state = build_plan(quotes, passed, settings=s, budget_usd=budget,
                                 reserve_output_tokens=args.reserve_output_tokens, max_quotes=args.max_quotes,
                                 include_non_quotes=args.include_non_quotes, factory=factory, log=lambda m: print(m, file=sys.stderr))
        for line in format_plan(plan, verbose=args.verbose):
            print(line)
        if args.dry_run:
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / "plan.json").write_text(json.dumps(plan, indent=1, default=common.json_default))
                print(f"plan: {args.out / 'plan.json'}")
            return 0
        if not args.sweep:
            raise ProbeError(
                "a live probe needs --sweep <open sweep id of purpose dev_probe on WS6> or --sweep open "
                "(spec 0.4 rule 2); use --dry-run for the classification and the estimate"
            )
        from app.services import plans_reader_model_routes as model_routes

        common.check_dev(s)
        common.check_route(model_routes.resolve_step(STEP, s), s)
        if not plan["fallback_candidates"]:
            raise ProbeError("no quote needs the fallback; nothing to probe")
        rows = {r["quote_id"]: r for r in state.rows}
        first = rows[plan["fallback_candidates"][0]]
        if first.get("reserve_usd") is None:
            raise ProbeError("the plan has no price for the route's model (spec 0.4 rule 3)")
        sweep = common.arm_sweep(args.sweep, workstream=WORKSTREAM, eval_root=eval_root, settings=s,
                                 cassette=not args.no_cassette)
        s = get_settings() if settings is None else s
        left = min(Decimal(str(sweep["remaining_usd"])), Decimal(str(args.max_usd)))
        if Decimal(str(first["reserve_usd"])) > left:
            raise ProbeError(
                f"the first fallback call's reserve ({common.money(first['reserve_usd'])}) exceeds what this run may "
                f"spend ({common.money(left)}: sweep {sweep['id']} remaining, capped by --max-usd)"
            )
        started = time.time()
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        stop = None
        for qid in plan["fallback_candidates"]:
            row = rows[qid]
            if stop:
                skipped.append({"case": row["case"], "quote_id": qid, "why": stop})
                continue
            reserve = row.get("reserve_usd")
            if reserve is None or qid not in state.blobs:
                skipped.append({"case": row["case"], "quote_id": qid, "why": "no request estimate"})
                continue
            if Decimal(str(reserve)) > left:
                skipped.append({"case": row["case"], "quote_id": qid,
                                "why": f"budget (reserve {common.money(reserve)}, left {common.money(left)})"})
                continue
            data_bytes, _pages, qf = state.blobs[qid]
            res = run_quote(row, data_bytes, qf, settings=s, factory=factory)
            results.append(res)
            left -= Decimal(str(res["cost_usd"] or 0))
            print(f"{row['case']} {qid[:8]}: {res['status_before']} -> {res['status_after']} ({res['parser']}), "
                  f"lines {res['lines']}, reference {_flag(res.get('ref_match'), res.get('delta_pct'))}, "
                  f"cost {res['cost_usd']:.4f}" + (f" ERROR {res['error']}" if res["error"] else ""))
            if res["ledger_refused"]:
                stop = "the ledger refused the previous call"
        out_dir = common.out_dir_for(eval_root, str(sweep["id"]), NAME, args.out)
        table = format_table(results, skipped)
        payload = {
            "schema": RESULTS_SCHEMA, "sweep_id": sweep["id"], "workstream": WORKSTREAM, "plan": plan,
            "results": results, "skipped": skipped, "elapsed_s": round(time.time() - started, 1),
            "spent_usd": round(sum(float(r.get("cost_usd") or 0) for r in results), 6),
        }
        path = common.write_results(out_dir, payload, table)
        print(f"fallback calls {len(results)}, skipped {len(skipped)}, spent ${payload['spent_usd']:.4f}")
        print(f"results (dollar values under EVAL_ROOT only): {path}")
        return 0
    except ProbeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "QuoteFile", "build_plan", "classify", "collect_quotes", "estimate_fallback", "format_plan", "format_table",
    "main", "ref_compare", "reference_quotes", "run_quote",
]


if __name__ == "__main__":
    raise SystemExit(main())
