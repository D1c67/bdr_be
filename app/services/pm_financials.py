"""PM financials math — the computed G702/G703 layer.

Nothing derived is stored (migration 0059): contract value, per-line progress,
and per-app certificates are all recomputed here from raw rows so the ledger
can't drift. The one stored derivation is the previous_completed snapshot taken
when a pay app is created (previous_completed_by_line).

Pure functions — no DB. Decimal in, strings out (the API's money wire format);
raw PostgREST numerics (str/float/int) are accepted anywhere a value goes in.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
# percent_complete is a 0–100 percentage (the same convention retainage_percent
# uses, and what the worksheet UI shows); 2dp keeps repeating divisions finite.
_PERCENT = Decimal("0.01")


def dec(value, default: Decimal | None = None) -> Decimal | None:
    """Decimal from a raw numeric; None → default. Always via str() so floats
    don't smuggle binary noise into money math."""
    if value is None:
        return default
    return Decimal(str(value))


def money(value: Decimal) -> str:
    """Money wire format: always 2dp ("36000.00", never "36000.0" — PostgREST
    hands numerics back float-shaped, so raw str() drifts)."""
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def total_of(rows: list[dict], field: str) -> Decimal:
    return sum((dec(r.get(field), Decimal(0)) for r in rows), Decimal(0))


def retainage_of(total: Decimal, retainage_percent) -> Decimal:
    """Retainage held on a completed-and-stored total; 0 when no percent is set."""
    pct = dec(retainage_percent)
    if pct is None:
        return Decimal("0.00")
    return (pct / 100 * total).quantize(_CENT, rounding=ROUND_HALF_UP)


def line_totals(row: dict, scheduled_value) -> dict:
    """Per-line G703 computed columns. percent_complete is null when the line
    has no scheduled value to be a fraction of."""
    total = (
        dec(row.get("previous_completed"), Decimal(0))
        + dec(row.get("this_period"), Decimal(0))
        + dec(row.get("stored_materials"), Decimal(0))
    )
    scheduled = dec(scheduled_value, Decimal(0))
    percent = None
    if scheduled != 0:
        percent = (total / scheduled * 100).quantize(_PERCENT, rounding=ROUND_HALF_UP)
    return {
        "total_completed_and_stored": money(total),
        "percent_complete": str(percent) if percent is not None else None,
        "balance_to_finish": money(scheduled - total),
    }


def _app_decimals(
    lines: list[dict], retainage_percent, previous_certificates: Decimal
) -> dict[str, Decimal]:
    work = total_of(lines, "this_period")
    stored = total_of(lines, "stored_materials")
    total = total_of(lines, "previous_completed") + work + stored
    retainage = retainage_of(total, retainage_percent)
    return {
        "work_this_period": work,
        "stored_materials": stored,
        "total_completed_and_stored": total,
        "retainage_held": retainage,
        "previous_certificates": previous_certificates,
        "current_payment_due": total - retainage - previous_certificates,
    }


def app_series_totals(
    apps: list[dict], lines_by_app: dict[str, list[dict]]
) -> dict[str, dict]:
    """G702 totals for every pay app of a project, keyed by app id. Apps chain:
    each app's previous_certificates is the nearest prior NON-REJECTED app's
    certificate (total less retainage). Rejected apps still get totals of their
    own but never advance the chain."""
    out: dict[str, dict] = {}
    prev_cert = Decimal(0)
    for app in sorted(apps, key=lambda a: a["app_number"]):
        d = _app_decimals(
            lines_by_app.get(app["id"], []), app.get("retainage_percent"), prev_cert
        )
        out[app["id"]] = {k: money(v) for k, v in d.items()}
        if app.get("status") != "rejected":
            prev_cert = d["total_completed_and_stored"] - d["retainage_held"]
    return out


def previous_completed_by_line(prior_lines: list[dict]) -> dict[str, Decimal]:
    """The snapshot for a new pay app: per SOV line, everything billed as
    this_period across the prior apps (caller passes only non-rejected apps'
    lines). Prior previous_completed columns are NOT re-summed — each app
    already folded its own history in when it was created."""
    totals: dict[str, Decimal] = {}
    for row in prior_lines:
        key = row["sov_line_id"]
        totals[key] = totals.get(key, Decimal(0)) + dec(row.get("this_period"), Decimal(0))
    return totals


def financials_summary(
    original_contract_value,
    approved_change_total: Decimal,
    sov_total: Decimal,
    billed_to_date: Decimal,
    retainage_held: Decimal,
    open_change_order_count: int,
    pay_app_count: int,
) -> dict:
    """The module headline. Contract-derived figures are null (not 0) until an
    original contract value exists — unknown, not zero."""
    original = dec(original_contract_value)
    current = original + approved_change_total if original is not None else None
    balance = current - billed_to_date if current is not None else None
    return {
        "original_contract_value": money(original) if original is not None else None,
        "approved_change_total": money(approved_change_total),
        "current_contract_value": money(current) if current is not None else None,
        "sov_total": money(sov_total),
        "billed_to_date": money(billed_to_date),
        "retainage_held": money(retainage_held),
        "balance_to_finish": money(balance) if balance is not None else None,
        "open_change_order_count": open_change_order_count,
        "pay_app_count": pay_app_count,
    }
