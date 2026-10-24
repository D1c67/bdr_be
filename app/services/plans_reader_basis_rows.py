"""The Basis and Checks sheet rows of WS6's priced workbook
(pricing_render.render_priced_estimate_xlsx `basis_rows` and `checks_rows`;
docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS8 "Basis and Checks sheet rows").

`basis_rows(evidence, priced_lines)`: one row per BOQ line, how its quantity
was made (the quantity chip, its sources, marks and sheets) and how its price
was made (the price chip: quote, library median n=, policy, missing; never
an amount, the amounts sit in the estimate sheets).

`checks_rows(findings, autonomy, review_findings)`: the autonomy checks
(key, state, reasons), then every integrity gate finding (gate id, severity
and status, message, line ids), then the review findings when WS7 stored
any. Both return dicts whose keys are the sheet headers (the renderer takes
the header from the first row), so a sheet is self-describing.

`rows_for_run(sb, run_id, priced=None)` loads everything from the run
(latest evidence output, the priced lines of the in-memory estimate or of
the latest stored version, the gate findings of every stage, the autonomy
status for the pricing audience and the stored review findings) and
returns `(basis_rows, checks_rows)`; every read is tolerant of a missing
table or row (empty rows, never an exception), because the workbook must
render even when a sibling has nothing yet. The pricing job calls it (hook
in pricing_run.execute_job, wave 3) and the workbook stays internal, so the
price chip texts belong here; the stored evidence itself never carries
them (plans_reader_evidence module docstring).

Pure apart from `rows_for_run`'s reads; no model, no network.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.services.plans_reader_evidence import price_chip

logger = logging.getLogger(__name__)

BASIS_HEADER: tuple[str, ...] = (
    "LINE", "DESCRIPTION", "QTY", "UNIT", "QUANTITY BASIS", "SOURCES", "MARKS", "SHEETS", "PRICE BASIS",
)
CHECKS_HEADER: tuple[str, ...] = ("CHECK", "STATUS", "DETAIL", "LINES")
MAX_SOURCES_TEXT = 200
MAX_DETAIL_TEXT = 400


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _qty(value: Any) -> Any:
    f = _num(value)
    if f is None:
        return ""
    return int(f) if float(f).is_integer() else round(f, 2)


def _join(items: Iterable[Any], limit: int = MAX_SOURCES_TEXT) -> str:
    text = ", ".join(str(i) for i in items if i not in (None, ""))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _sources_text(line: Mapping[str, Any]) -> str:
    refs = []
    for ref in line.get("source_refs") or []:
        if isinstance(ref, Mapping):
            r = ref.get("ref")
            mult = ref.get("multiplier")
            refs.append(f"{r} x{mult:g}" if r and _num(mult) not in (None, 1.0) else str(r or ""))
        elif ref:
            refs.append(str(ref))
    return _join(refs)


def _marks_text(line: Mapping[str, Any]) -> str:
    unit = str(line.get("unit") or "").upper()
    if unit == "FT":
        n = line.get("polylines")
        ft = line.get("polyline_ft")
        if not n:
            return "no polyline"
        return f"{int(n)} polyline(s), {float(ft):g} FT drawn" if _num(ft) is not None else f"{int(n)} polyline(s)"
    per = line.get("marks_by_method") or {}
    parts = [f"{k} {v}" for k, v in sorted(per.items()) if v]
    nl = line.get("not_located")
    if nl:
        parts.append(f"{int(nl)} not located")
    return _join(parts) if parts else "none"


def basis_rows(
    evidence: Mapping[str, Any] | None,
    priced_lines: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """One Basis row per evidence line (module docstring). Without evidence
    the sheet is empty; without priced lines every price basis reads the
    `missing` chip."""
    if not isinstance(evidence, Mapping):
        return []
    priced_by_id: dict[str, Any] = {}
    for pl in priced_lines or ():
        lid = _get(pl, "line_id")
        if lid:
            priced_by_id[str(lid)] = pl
    rows: list[dict[str, Any]] = []
    for line in evidence.get("lines") or []:
        if not isinstance(line, Mapping):
            continue
        lid = str(line.get("line_id") or "")
        chip = line.get("qty_chip") if isinstance(line.get("qty_chip"), Mapping) else {}
        rows.append(
            {
                "LINE": lid,
                "DESCRIPTION": str(line.get("description") or ""),
                "QTY": _qty(line.get("quantity")),
                "UNIT": str(line.get("unit") or ""),
                "QUANTITY BASIS": str(chip.get("text") or chip.get("kind") or ""),
                "SOURCES": _sources_text(line),
                "MARKS": _marks_text(line),
                "SHEETS": _join(line.get("sheets") or line.get("sheet_refs") or []),
                "PRICE BASIS": price_chip(priced_by_id.get(lid)).text(),
            }
        )
    return rows


def _autonomy_rows(autonomy: Any) -> list[dict[str, Any]]:
    if autonomy is None:
        return []
    data = autonomy.to_dict() if hasattr(autonomy, "to_dict") else autonomy
    if not isinstance(data, Mapping):
        return []
    rows = [
        {
            "CHECK": "autonomy",
            "STATUS": str(data.get("overall") or ""),
            "DETAIL": f"{data.get('passed', 0)} of {data.get('total', 0)} checks passing",
            "LINES": "",
        }
    ]
    for c in data.get("checks") or []:
        if not isinstance(c, Mapping):
            continue
        reasons = [str(r) for r in c.get("reasons") or []]
        rows.append(
            {
                "CHECK": f"autonomy:{c.get('key')}",
                "STATUS": str(c.get("state") or "") + (" (overridden)" if c.get("overridden") else ""),
                "DETAIL": _join(reasons, MAX_DETAIL_TEXT) if reasons else "",
                "LINES": "",
            }
        )
    return rows


def _finding_rows(findings: Iterable[Any]) -> list[dict[str, Any]]:
    rows = []
    for f in findings or ():
        gate = _get(f, "gate_id")
        if not gate:
            continue
        line_ids = f.line_ids() if hasattr(f, "line_ids") else []
        if not line_ids:
            target = _get(f, "target") or {}
            if isinstance(target, Mapping):
                lid = target.get("line_id")
                line_ids = [lid] if lid else [r for r in (target.get("row_ids") or []) if str(r).startswith("B")]
        rows.append(
            {
                "CHECK": f"gate:{_get(f, 'stage') or 'final'}:{gate}",
                "STATUS": f"{_get(f, 'severity') or ''} {_get(f, 'status') or ''}".strip(),
                "DETAIL": str(_get(f, "message") or "")[:MAX_DETAIL_TEXT],
                "LINES": _join(line_ids),
            }
        )
    return rows


def _review_rows(review_findings: Iterable[Any]) -> list[dict[str, Any]]:
    rows = []
    for f in review_findings or ():
        kind = _get(f, "kind") or _get(f, "rule_id") or "finding"
        severity = _get(f, "severity") or ""
        label = _get(f, "human_label") or _get(f, "status") or ""
        title = _get(f, "title") or _get(f, "claim") or ""
        rows.append(
            {
                "CHECK": f"review:{kind}",
                "STATUS": f"{severity} {label}".strip(),
                "DETAIL": str(title)[:MAX_DETAIL_TEXT],
                "LINES": _join(_get(f, "line_ids") or []),
            }
        )
    return rows


def checks_rows(
    findings: Iterable[Any] = (),
    autonomy: Any = None,
    review_findings: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """The Checks sheet rows: autonomy, gate findings, review findings."""
    return [*_autonomy_rows(autonomy), *_finding_rows(findings), *_review_rows(review_findings)]


# Loading ---------------------------------------------------------------------------


def _safe(fn: Any, *args: Any, default: Any = None, what: str = "") -> Any:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - a sibling without data never stops the workbook
        logger.info("basis_rows: %s unavailable", what or getattr(fn, "__name__", "read"), exc_info=True)
        return default


def _latest_priced_lines(sb: Any, run_id: str) -> list[dict[str, Any]]:
    from app.services import pricing_run

    latest = pricing_run.latest_version(sb, run_id)
    if not latest:
        return []
    return [r.get("line") for r in pricing_run.version_lines(sb, str(latest["id"])) if isinstance(r.get("line"), Mapping)]


def _review_findings(sb: Any, run_id: str) -> list[dict[str, Any]]:
    rows = sb.table("plans_reader_review_findings").select("*").eq("run_id", str(run_id)).execute().data or []
    return [r for r in rows if isinstance(r, Mapping)]


def rows_for_run(
    sb: Any,
    run_id: str,
    *,
    priced: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(basis_rows, checks_rows)` for the priced workbook of `run_id`
    (module docstring). `priced` is the in-memory PricedEstimate when the
    pricing job renders before storing; else the latest stored lines."""
    from app.services import plans_reader_autonomy as autonomy
    from app.services import plans_reader_gates as gates
    from app.services.plans_reader_render_evidence import latest_evidence

    run_id = str(run_id)
    evidence = _safe(latest_evidence, sb, run_id, default=None, what="evidence")
    lines: Sequence[Any] = ()
    if priced is not None and getattr(priced, "lines", None) is not None:
        lines = list(priced.lines)
    else:
        lines = _safe(_latest_priced_lines, sb, run_id, default=[], what="priced lines") or []
    findings = _safe(lambda: gates.findings_for_run(sb, run_id, stage=None), default=[], what="gate findings") or []
    status = _safe(lambda: autonomy.status(sb, run_id, include_pricing=True), default=None, what="autonomy")
    review = _safe(_review_findings, sb, run_id, default=[], what="review findings") or []
    return basis_rows(evidence, lines), checks_rows(findings, status, review)


__all__ = ["BASIS_HEADER", "CHECKS_HEADER", "basis_rows", "checks_rows", "rows_for_run"]
