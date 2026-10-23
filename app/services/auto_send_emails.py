"""Internal Auto send emails (docs/AUTO_SEND.md section 7).

Two high-importance branded emails go to every active Estimating Admin,
Accountant, Executive and IT Admin (roles.AUTO_SEND_NOTIFY_ROLES; engineers are
excluded on purpose):

  * Preflight (3:00 PM Pacific by default): what will auto send at the send
    time and with which numbers and GCs, what will not and why, and what is not
    auto sent by rule (dev estimator).
  * Digest (about 5:10 PM): today's results. Sent (GCs and recipients, the sent
    numbers, the estimator's price and the markup), Could not send (run reasons,
    per-GC failures of partial runs, missed runs) and Not auto sent by rule.

Rendering only: the scheduler (services/auto_send_scheduler) decides when to
send, claims the auto_send_email_log row per recipient and calls Graph. Every
piece of user content is escaped here; render_branded_html does not escape.
"""

import html
from datetime import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.core import roles
from app.services.due_reminders import _page_all
from app.services.email_branding import (
    _BORDER,
    _FONT,
    _MUTED,
    _NAVY,
    _RED,
    _button,
    render_branded_html,
)

_OK_GREEN = "#1e7d3c"
_AMBER = "#8a5d00"
_GREY = "#6a6f78"

_CENTS = Decimal("0.01")


# ── Labels and subjects ───────────────────────────────────────────────────


def time_label(hour: int, minute: int) -> str:
    """(17, 5) -> "5:05 PM"."""
    return time(hour, minute).strftime("%I:%M %p").lstrip("0")


def preflight_subject(date_label: str, send_label: str) -> str:
    return f"G3 BDR · [HIGH IMPORTANCE] Auto send at {send_label} today: {date_label}"


def digest_subject(date_label: str) -> str:
    return f"G3 BDR · [HIGH IMPORTANCE] Auto send results: {date_label}"


def project_url(base_url: str, project_id: str) -> str:
    """Deep link that opens the project's Auto send panel."""
    return f"{base_url.rstrip('/')}/projects/{project_id}?box=auto_send"


# ── Teams copies (0177) ───────────────────────────────────────────────────

_PREFLIGHT_WORDS = {"ready": "will auto send", "blocked": "will not auto send",
                    "skipped": "not auto sent by rule"}
_DIGEST_WORDS = {"sent": "sent", "partial": "partly sent", "failed": "could not send",
                 "missed": "did not run", "sending": "still sending",
                 "skipped": "not auto sent by rule"}


def _short_label(project: dict) -> str:
    number = f"#{project['number']} " if project.get("number") else ""
    return f"{number}{project.get('name') or 'Untitled project'}"


def preflight_teams_lines(items: list[dict]) -> list[str]:
    """One short line per project for the Teams preview: number, name and
    whether it will auto send. No amounts: Teams stays brief."""
    return [
        f"{_short_label(i['project'])} · "
        f"{_PREFLIGHT_WORDS.get((i.get('evaluation') or {}).get('outcome'), 'check it')}"
        for i in items
    ]


def digest_teams_lines(items: list[dict]) -> list[str]:
    """One short line per project for the Teams results: number, name and
    what happened. No amounts: Teams stays brief."""
    return [
        f"{_short_label(i['project'])} · "
        f"{_DIGEST_WORDS.get(i['run'].get('status'), i['run'].get('status') or 'unknown')}"
        for i in items
    ]


# ── Recipients ────────────────────────────────────────────────────────────


def notify_recipients(sb) -> list[dict]:
    """Active profiles with a role in AUTO_SEND_NOTIFY_ROLES and an email, as
    {id, full_name, email, role}. Paged (background code must not truncate)."""
    role_values = sorted(r.value for r in roles.AUTO_SEND_NOTIFY_ROLES)
    profiles = _page_all(
        lambda lo, hi: sb.table("profiles")
        .select("id, full_name, email, role")
        .in_("role", role_values)
        .eq("is_active", True)
        .order("id")
        .range(lo, hi)
    )
    allowed = set(role_values)
    return [
        p
        for p in profiles
        if p.get("role") in allowed and (p.get("email") or "").strip()
    ]


# ── Formatting helpers ────────────────────────────────────────────────────


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _money(value) -> str | None:
    d = _decimal(value)
    if d is None:
        return None
    return f"${d.quantize(_CENTS, rounding=ROUND_HALF_UP):,.2f}"


def _pct(value) -> str | None:
    d = _decimal(value)
    if d is None:
        return None
    text = format(d.quantize(_CENTS, rounding=ROUND_HALF_UP).normalize(), "f")
    return f"{text}%"


def _greeting(recipient_name: str | None) -> str:
    first = (recipient_name or "").strip().split(" ")[0]
    return f"Hi {html.escape(first)}," if first else "Hi there,"


def _banner(text: str) -> str:
    """Solid red band above the heading, same build as revision_email._banner
    (table based so it renders in Outlook). `text` is a fixed literal."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 16px;"><tr>'
        f'<td style="background-color:{_RED};border-radius:6px;padding:10px 16px;'
        f'{_FONT}font-size:13px;font-weight:bold;letter-spacing:1px;color:#ffffff;">'
        f"{html.escape(text)}</td></tr></table>"
    )


def _project_label(project: dict) -> str:
    name = project.get("name") or "Untitled project"
    number = project.get("number")
    return f"{number} · {name}" if number else name


def _project_link(project: dict, base_url: str) -> str:
    url = project_url(base_url, project["id"])
    return (
        f'<a href="{html.escape(url, quote=True)}" target="_blank" '
        f'style="color:{_NAVY};font-weight:bold;text-decoration:underline;">'
        f"{html.escape(_project_label(project))}</a>"
    )


def _recipients_text(recipients) -> str:
    if isinstance(recipients, (list, tuple)):
        return ", ".join(str(r) for r in recipients if r)
    return str(recipients or "")


def _line(text_html: str, color: str = _MUTED) -> str:
    return (
        f'<div style="font-size:13px;line-height:1.5;color:{color};padding-top:3px;">'
        f"{text_html}</div>"
    )


def _list(items_html: list[str]) -> str:
    if not items_html:
        return ""
    return (
        '<ul style="margin:4px 0 0;padding-left:20px;font-size:13px;line-height:1.5;">'
        + "".join(f'<li style="margin:0 0 3px;">{i}</li>' for i in items_html)
        + "</ul>"
    )


def _numbers_line(material, labor, total) -> str:
    parts = []
    for label, value in (("Materials", material), ("Labor", labor), ("Total", total)):
        money = _money(value)
        if money is not None:
            parts.append(f"{label} <b>{html.escape(money)}</b>")
    return _line(" · ".join(parts), color="#2a2d34") if parts else ""


def _estimator_line(base_material, base_labor, markup_pct) -> str:
    parts = []
    mat = _money(base_material)
    lab = _money(base_labor)
    if mat is not None or lab is not None:
        bits = []
        if mat is not None:
            bits.append(f"materials {html.escape(mat)}")
        if lab is not None:
            bits.append(f"labor {html.escape(lab)}")
        parts.append("Estimator price: " + ", ".join(bits))
    pct = _pct(markup_pct)
    if pct is not None:
        parts.append(f"Markup {html.escape(pct)}")
    return _line(" · ".join(parts)) if parts else ""


def _reason_messages(reasons) -> list[str]:
    out = []
    for r in reasons or []:
        if not isinstance(r, dict):
            continue
        message = r.get("message") or r.get("code")
        if message:
            out.append(str(message))
    return out


def _block(head_html: str, body_html: str, accent: str) -> str:
    return (
        f'<div style="margin:0 0 10px;padding:10px 12px;border:1px solid {_BORDER};'
        f'border-left:4px solid {accent};border-radius:6px;{_FONT}">'
        f'<div style="font-size:14px;">{head_html}</div>{body_html}</div>'
    )


def _section(title: str, accent: str, blocks: list[str]) -> str:
    if not blocks:
        return ""
    return (
        f'<div style="margin:18px 0 8px;font-size:16px;font-weight:bold;color:{accent};">'
        f"{html.escape(title)} ({len(blocks)})</div>" + "".join(blocks)
    )


# ── Preflight ─────────────────────────────────────────────────────────────


def _preflight_ready_block(project: dict, ev: dict, base_url: str) -> str:
    amounts = ev.get("amounts") or {}
    body = _numbers_line(amounts.get("material"), amounts.get("labor"), amounts.get("total"))
    body += _estimator_line(
        amounts.get("base_material"), amounts.get("base_labor"), ev.get("markup_pct")
    )
    gc_items = []
    for gc in ev.get("gcs") or []:
        name = html.escape(gc.get("gc_name") or "GC")
        reason = gc.get("reason")
        if reason:
            message = reason.get("message") or reason.get("code") or ""
            gc_items.append(
                f'<b>{name}</b>: <span style="color:{_RED};">will not receive it. '
                f"{html.escape(str(message))}</span>"
            )
        else:
            recipients = html.escape(_recipients_text(gc.get("recipients")))
            gc_items.append(f"<b>{name}</b>: {recipients}" if recipients else f"<b>{name}</b>")
    if gc_items:
        body += _line("GCs:") + _list(gc_items)
    return _block(_project_link(project, base_url), body, _OK_GREEN)


def _reasons_block(
    project: dict, messages: list[str], base_url: str, accent: str, tag: str | None = None
) -> str:
    head = _project_link(project, base_url)
    if tag:
        head += (
            f' <span style="font-size:12px;color:{accent};font-weight:bold;">'
            f"{html.escape(tag)}</span>"
        )
    body = _list([html.escape(m) for m in messages]) if messages else ""
    return _block(head, body, accent)


def render_preflight_email(
    *,
    recipient_name: str | None,
    date_label: str,
    send_label: str,
    items: list[dict],
    base_url: str,
) -> str:
    """Branded HTML for the preflight. Each item is {project, evaluation} with
    `project` holding id / name / number and `evaluation` an
    auto_send.evaluate() result. Silent evaluations are left out."""
    ready, blocked, skipped = [], [], []
    for item in items:
        project, ev = item["project"], item.get("evaluation") or {}
        outcome = ev.get("outcome")
        if outcome == "ready":
            ready.append(_preflight_ready_block(project, ev, base_url))
        elif outcome == "blocked":
            blocked.append(
                _reasons_block(project, _reason_messages(ev.get("reasons")), base_url, _RED)
            )
        elif outcome == "skipped":
            skipped.append(
                _reasons_block(project, _reason_messages(ev.get("reasons")), base_url, _GREY)
            )

    intro = (
        f'<p style="margin:0 0 14px;">These projects have an internal bid date of '
        f"today ({html.escape(date_label)}). At {html.escape(send_label)} Pacific the "
        "app emails the proposal below to each GC, unless someone sends the proposal "
        "or marks it submitted first.</p>"
    )
    body = (
        _banner(f"HIGH IMPORTANCE: AUTO SEND AT {send_label.upper()}")
        + f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + intro
        + _section(f"Will auto send at {send_label}", _OK_GREEN, ready)
        + _section("Will not auto send", _RED, blocked)
        + _section("Not auto sent by rule", _GREY, skipped)
        + f'<p style="margin:16px 0 14px;font-size:12px;color:{_MUTED};">'
        "Click a project to open its Auto send panel.</p>"
        + _button("Open Bids Today", f"{base_url.rstrip('/')}/bids-today")
    )
    return render_branded_html(body, subtitle="AUTO SEND PREFLIGHT")


# ── Digest ────────────────────────────────────────────────────────────────

_FAILED_GC_STATUSES = ("failed", "unknown")
_STATUS_TAG = {
    "failed": "Could not send",
    "missed": "Missed",
    "partial": "Partly sent",
    "sending": "Still sending",
}
_STILL_SENDING = (
    "This was still sending when this email went out. Check the Auto send panel."
)


def _digest_sent_block(project: dict, run: dict, gcs: list[dict], base_url: str) -> str:
    head = _project_link(project, base_url)
    if run.get("status") == "partial":
        head += (
            f' <span style="font-size:12px;color:{_AMBER};font-weight:bold;">'
            "Partly sent</span>"
        )
    body = _numbers_line(
        run.get("material_amount"), run.get("labor_amount"), run.get("total_amount")
    )
    estimate = run.get("estimate") or {}
    body += _estimator_line(
        estimate.get("materials"), estimate.get("labor"), run.get("markup_pct")
    )
    gc_items = []
    for gc in gcs:
        if gc.get("status") != "sent":
            continue
        name = html.escape(gc.get("gc_name") or "GC")
        recipients = html.escape(_recipients_text(gc.get("recipients")))
        gc_items.append(f"<b>{name}</b>: {recipients}" if recipients else f"<b>{name}</b>")
    if gc_items:
        body += _line("Sent to:") + _list(gc_items)
    if run.get("redirected_to"):
        body += _line(
            "Test mode: every GC email went only to "
            f"{html.escape(str(run['redirected_to']))}.",
            color=_AMBER,
        )
    return _block(head, body, _OK_GREEN)


def _digest_problem_messages(run: dict, gcs: list[dict]) -> list[str]:
    """Run reasons plus every failed / timed out GC not already covered by a
    per-GC reason."""
    reasons = [r for r in (run.get("reasons") or []) if isinstance(r, dict)]
    messages = _reason_messages(reasons)
    covered = {r.get("gc_id") for r in reasons if r.get("gc_id")}
    for gc in gcs:
        if gc.get("status") not in _FAILED_GC_STATUSES or gc.get("gc_id") in covered:
            continue
        name = gc.get("gc_name") or "GC"
        if gc.get("status") == "unknown":
            detail = gc.get("error") or (
                "The email timed out and may or may not have gone out. Check the "
                "bids mailbox before sending again."
            )
        else:
            detail = gc.get("error") or "The email failed."
        messages.append(f"{name}: {detail}")
    if run.get("status") == "sending" and not messages:
        messages.append(_STILL_SENDING)
    return messages


def render_digest_email(
    *,
    recipient_name: str | None,
    date_label: str,
    items: list[dict],
    base_url: str,
) -> str:
    """Branded HTML for the results digest. Each item is {project, run, gcs}:
    an auto_send_runs row for today, its auto_send_gcs rows and the project's
    id / name / number."""
    sent, failed, skipped = [], [], []
    for item in items:
        project, run = item["project"], item["run"]
        gcs = item.get("gcs") or []
        status = run.get("status")
        if status in ("sent", "partial"):
            sent.append(_digest_sent_block(project, run, gcs, base_url))
        if status in ("failed", "missed", "partial", "sending"):
            messages = _digest_problem_messages(run, gcs)
            if status != "partial" or messages:
                failed.append(
                    _reasons_block(project, messages, base_url, _RED, _STATUS_TAG[status])
                )
        elif status == "skipped":
            skipped.append(
                _reasons_block(project, _reason_messages(run.get("reasons")), base_url, _GREY)
            )

    intro = (
        f'<p style="margin:0 0 14px;">Auto send results for projects with an internal '
        f"bid date of today ({html.escape(date_label)}).</p>"
    )
    body = (
        _banner("HIGH IMPORTANCE: AUTO SEND RESULTS")
        + f'<p style="margin:0 0 14px;color:{_MUTED};">{_greeting(recipient_name)}</p>'
        + intro
        + _section("Sent", _OK_GREEN, sent)
        + _section("Could not send", _RED, failed)
        + _section("Not auto sent by rule", _GREY, skipped)
        + f'<p style="margin:16px 0 14px;font-size:12px;color:{_MUTED};">'
        "Click a project to open its Auto send panel, where you can send it now or "
        "accept the auto send as submitted.</p>"
        + _button("Open Bids Today", f"{base_url.rstrip('/')}/bids-today")
    )
    return render_branded_html(body, subtitle="AUTO SEND RESULTS")
