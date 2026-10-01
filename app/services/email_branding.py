"""Branded HTML rendering for outbound vendor emails.

The body text stays plain (the AI variation, PE editing, and email_log/rfq_sends
all work on text); this module wraps that text in the G3-branded HTML shell at
send time. The signature block embeds the logo as an inline (cid:) attachment —
callers must attach `logo_bytes()` with `LOGO_CONTENT_ID` on the same draft.

Theme comes from the logo: navy #202159 (primary), silver/gray (dividers and
secondary text), and red #951e2d only as a thin hairline accent — red is in the
logo art but is not a brand color, so it stays minimal.
"""

import html
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

SIGNOFF = "The G3 Estimating Team"

# The sentence that carries an over-size documents folder link in an RFQ body.
# Lives here (re-exported by rfq_sending) because the renderer drops that
# paragraph when the same link is shown as the documents card at the top.
DOCUMENTS_LINK_SENTENCE = "The drawings and specifications are available here:"

OFFICE_PHONE_DISPLAY = "(702) 916-3355"
OFFICE_PHONE_TEL = "+17029163355"

# Email-optimized logo: the source art is ~1600px / 193 KB but the signature
# only ever renders it at 88px, so we ship a downscaled 264px (~12 KB) JPEG.
# Inlined per recipient on every branded email, so the size directly affects
# deliverability and bandwidth — keep this asset small. Regenerate from the
# source with scripts/make_email_logo.py if the brand art changes.
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "g3-logo-email.jpg"
LOGO_FILENAME = "g3-logo.jpg"
LOGO_CONTENT_ID = "g3-logo"

_NAVY = "#202159"
_RED = "#951e2d"
_SILVER = "#b9bec4"
_BORDER = "#d8dbe0"
_TEXT = "#2a2d34"
_MUTED = "#6a6f78"
_PAGE_BG = "#f2f3f6"
_FONT = "font-family:Arial,Helvetica,sans-serif;"

# Animated documents card (see _documents_card). Built once by
# scripts/make_rfq_documents_card.py at 2x; shown at DOCUMENTS_CARD_SIZE.
DOCUMENTS_CARD_PATH = Path(__file__).resolve().parent.parent / "assets" / "rfq-documents-card.gif"
DOCUMENTS_CARD_FILENAME = "open-drawings-and-specs.gif"
DOCUMENTS_CARD_CONTENT_ID = "rfq-documents-card"
DOCUMENTS_CARD_SIZE = (536, 190)

_URL_RE = re.compile(r"https?://\S+")


@lru_cache(maxsize=1)
def logo_bytes() -> bytes:
    return LOGO_PATH.read_bytes()


@lru_cache(maxsize=1)
def documents_card_bytes() -> bytes:
    return DOCUMENTS_CARD_PATH.read_bytes()


def _strip_signoff(text: str) -> str:
    """Drop a trailing signoff line — the signature block renders it instead,
    so it never appears twice."""
    stripped = text.rstrip()
    if stripped.endswith(SIGNOFF):
        return stripped[: -len(SIGNOFF)].rstrip()
    return stripped


def house_link_hosts() -> frozenset[str]:
    """The hosts a notification email may render as a link: the app's own
    frontend. Every URL the house writes into a notification is an app link;
    any other URL in the text came from an outside source (an RFP-derived
    project name, an estimator note) and stays plain escaped text."""
    from app.core.config import get_settings

    host = (urlsplit(get_settings().frontend_url or "").hostname or "").lower()
    return frozenset({host}) if host else frozenset()


def _host_allowed(escaped_url: str, hosts: frozenset[str]) -> bool:
    """True when the (escaped) URL's host is exactly one of `hosts`. A netloc
    with userinfo (`@`) or a backslash is refused: both are ways to make a
    foreign host read like ours."""
    try:
        parts = urlsplit(html.unescape(escaped_url))
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if "@" in parts.netloc or "\\" in parts.netloc:
        return False
    return bool(host) and host in hosts


def _linkify(escaped: str, hosts: frozenset[str] | None = None) -> str:
    """Turn bare URLs in already-escaped text into styled anchors. With
    `hosts`, only URLs on one of those hosts become anchors; the rest stay
    as plain text."""

    def repl(m: re.Match) -> str:
        url = m.group(0)
        trailing = ""
        while url and url[-1] in ".,;:!?)":
            trailing = url[-1] + trailing
            url = url[:-1]
        if hosts is not None and not _host_allowed(url, hosts):
            return m.group(0)
        return (
            f'<a href="{url}" style="color:{_NAVY};font-weight:bold;'
            f'word-break:break-all;">{url}</a>{trailing}'
        )

    return _URL_RE.sub(repl, escaped)


def _paragraphs(
    text: str, *, linkify: bool = True, link_hosts: frozenset[str] | None = None
) -> str:
    """Escaped paragraphs; bare URLs become anchors unless `linkify` is off
    (text that quotes an outside sender, such as a project name lifted from
    an RFP email, must never render as a link). With `link_hosts`, only URLs
    on those hosts are anchored."""
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    out = []
    for p in parts:
        escaped = html.escape(p)
        body = _linkify(escaped, link_hosts) if linkify else escaped
        out.append(f'<p style="margin:0 0 16px;">{body.replace(chr(10), "<br>")}</p>')
    return "".join(out)


def _signature_block(signoff: str = SIGNOFF) -> str:
    """The shared signature row: inline logo + team name + office phone.

    Used by every branded email so the logo (cid:) attachment and the office
    number stay identical across vendor and internal-notification mail."""
    return f"""\
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid {_BORDER};">
          <tr>
            <td width="100" style="padding-top:18px;vertical-align:middle;">
              <img src="cid:{LOGO_CONTENT_ID}" width="88" alt="G3 Electrical" style="display:block;width:88px;height:auto;border:0;">
            </td>
            <td style="padding-top:18px;padding-left:16px;vertical-align:middle;{_FONT}">
              <div style="font-size:15px;font-weight:bold;color:{_NAVY};">{signoff}</div>
              <div style="font-size:13px;color:{_MUTED};padding-top:2px;">G3 Electrical</div>
              <div style="font-size:13px;color:{_MUTED};padding-top:6px;">Office: <a href="tel:{OFFICE_PHONE_TEL}" style="color:{_NAVY};font-weight:bold;text-decoration:none;">{OFFICE_PHONE_DISPLAY}</a></div>
            </td>
          </tr>
        </table>"""


def _button(label: str, url: str) -> str:
    """A solid navy call-to-action button (table-cell based, renders in Outlook)."""
    safe_url = html.escape(url, quote=True)
    return f"""\
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 8px;">
          <tr>
            <td style="border-radius:8px;background-color:{_NAVY};">
              <a href="{safe_url}" target="_blank" style="display:inline-block;padding:13px 32px;{_FONT}font-size:15px;font-weight:bold;color:#ffffff;text-decoration:none;border-radius:8px;">{html.escape(label)}&nbsp;&rarr;</a>
            </td>
          </tr>
        </table>"""


def _documents_card(url: str) -> str:
    """The over-size documents link as a clickable card, rendered above the
    greeting. Vendors skimmed past the link when it was a sentence in the
    middle of the body, so it now leads the email: an amber card with a light
    running around its border (an animated GIF, since email clients strip CSS
    animation; callers attach `documents_card_bytes()` under
    DOCUMENTS_CARD_CONTENT_ID). The plain link under it covers image blocking."""
    safe_url = html.escape(url, quote=True)
    w, h = DOCUMENTS_CARD_SIZE
    return f"""\
        <a href="{safe_url}" target="_blank" style="display:block;text-decoration:none;">
          <img src="cid:{DOCUMENTS_CARD_CONTENT_ID}" width="{w}" alt="Open Drawings &amp; Specs (SharePoint folder)" style="display:block;width:100%;max-width:{w}px;height:auto;border:0;{_FONT}font-size:16px;font-weight:bold;color:{_NAVY};">
        </a>
        <p style="margin:6px 0 22px;padding:0 8px;{_FONT}font-size:12px;line-height:1.5;color:{_MUTED};">Button not working? Paste this link into your browser:<br>
          <a href="{safe_url}" target="_blank" style="color:{_NAVY};word-break:break-all;">{html.escape(url)}</a></p>"""


def render_vendor_email(
    body_text: str,
    subtitle: str = "REQUEST FOR QUOTE",
    *,
    documents_link: str | None = None,
) -> str:
    """Wrap a plain-text vendor email body in the branded HTML shell with the
    G3 signature (inline logo + office phone).

    `subtitle` is the small label under the "G3 ELECTRICAL" header band — it
    names the email type (e.g. "REQUEST FOR QUOTE" for RFQs, "PROPOSAL" for the
    proposal cover email).

    `documents_link` (an over-size drawings/specs folder) renders as a boxed
    button at the very top. The body's standard link paragraph is dropped so
    the link is not shown twice. That paragraph is matched loosely (it holds
    the link and otherwise just a lead-in ending in a colon) because the AI
    rewrite may reword the sentence; any other mention of the link stays."""
    text = _strip_signoff(body_text)
    card = ""
    if documents_link:
        card = _documents_card(documents_link)

        def is_link_line(p: str) -> bool:
            if documents_link not in p:
                return False
            lead = p.replace(documents_link, "").strip().rstrip(".")
            return lead.endswith(":") and "\n" not in lead

        text = "\n\n".join(p for p in text.split("\n\n") if not is_link_line(p))
    return render_branded_html(card + _paragraphs(text), subtitle)


def render_branded_html(body_html: str, subtitle: str) -> str:
    """The branded shell around an already-built HTML body — for callers whose
    content is structured (file lists, per-file notes) rather than escaped
    paragraphs. The caller is responsible for escaping its own content."""
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background-color:{_PAGE_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_PAGE_BG};">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#ffffff;border:1px solid {_BORDER};border-radius:10px;">
    <tr>
      <td style="background-color:{_NAVY};padding:18px 32px;border-radius:9px 9px 0 0;">
        <div style="{_FONT}font-size:16px;font-weight:bold;letter-spacing:3px;color:#ffffff;">G3 ELECTRICAL</div>
        <div style="{_FONT}font-size:11px;letter-spacing:2px;color:{_SILVER};padding-top:3px;">{subtitle}</div>
      </td>
    </tr>
    <tr><td style="height:3px;line-height:3px;font-size:0;background-color:{_RED};">&nbsp;</td></tr>
    <tr>
      <td style="padding:28px 32px 6px;{_FONT}font-size:15px;line-height:1.6;color:{_TEXT};">
        {body_html}
      </td>
    </tr>
    <tr>
      <td style="padding:6px 32px 26px;">
{_signature_block()}
      </td>
    </tr>
  </table>
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
    <tr>
      <td style="padding:14px 8px;text-align:center;{_FONT}font-size:11px;color:#9aa0a8;">
        G3 Electrical &middot; Office {OFFICE_PHONE_DISPLAY}
      </td>
    </tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


def render_proposal_email(body_text: str) -> str:
    """The proposal cover email to a GC — same minimal branded shell and G3
    signature as the vendor email, with a "PROPOSAL" header label."""
    return render_vendor_email(body_text, subtitle="PROPOSAL")


def render_notification_email(
    *,
    recipient_name: str | None,
    heading: str,
    message: str,
    cta_label: str,
    cta_url: str,
    project_label: str | None = None,
    linkify: bool = True,
    link_hosts: Iterable[str] | None = None,
) -> str:
    """Branded HTML for an internal/estimator notification: a navy header, the
    notification heading + message, a deep-link button into the app, and the
    shared G3 signature (inline logo + office phone).

    `message` is plain text (the same string the in-app bell shows); it is
    HTML-escaped and, unless `linkify` is off, linkified here. Linkification
    is keyed by the source of the URL, not the notification type: the message
    interpolates text the house did not write (project names from RFP emails,
    estimator notes, vendor names), so only URLs on `link_hosts` (default:
    the app's own frontend host, `house_link_hosts()`) become anchors and any
    other URL renders as plain escaped text. `cta_url` is the absolute app
    URL the button points to (already role/page-resolved by the caller)."""
    first = (recipient_name or "").strip().split(" ")[0]
    greeting = f"Hi {html.escape(first)}," if first else "Hi there,"
    hosts = (
        frozenset(h.lower() for h in link_hosts if h)
        if link_hosts is not None
        else house_link_hosts()
    )
    body_html = _paragraphs(message, linkify=linkify, link_hosts=hosts)
    chip = ""
    if project_label:
        chip = (
            f'<div style="display:inline-block;margin:0 0 14px;padding:5px 12px;'
            f"border:1px solid {_BORDER};border-radius:999px;{_FONT}font-size:12px;"
            f'font-weight:bold;color:{_NAVY};background-color:#f5f6f9;">'
            f"{html.escape(project_label)}</div>"
        )
    safe_url = html.escape(cta_url, quote=True)
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background-color:{_PAGE_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{_PAGE_BG};">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#ffffff;border:1px solid {_BORDER};border-radius:10px;">
    <tr>
      <td style="background-color:{_NAVY};padding:18px 32px;border-radius:9px 9px 0 0;">
        <div style="{_FONT}font-size:16px;font-weight:bold;letter-spacing:3px;color:#ffffff;">G3 ELECTRICAL</div>
        <div style="{_FONT}font-size:11px;letter-spacing:2px;color:{_SILVER};padding-top:3px;">BIDDING WORKSPACE</div>
      </td>
    </tr>
    <tr><td style="height:3px;line-height:3px;font-size:0;background-color:{_RED};">&nbsp;</td></tr>
    <tr>
      <td style="padding:28px 32px 6px;{_FONT}font-size:15px;line-height:1.6;color:{_TEXT};">
        <p style="margin:0 0 14px;color:{_MUTED};">{greeting}</p>
        {chip}
        <div style="font-size:19px;font-weight:bold;color:{_NAVY};margin:0 0 14px;">{html.escape(heading)}</div>
        {body_html}
        {_button(cta_label, cta_url)}
        <p style="margin:6px 0 0;font-size:12px;color:{_MUTED};">Trouble with the button? Paste this into your browser:<br>
          <a href="{safe_url}" target="_blank" style="color:{_NAVY};word-break:break-all;">{html.escape(cta_url)}</a></p>
      </td>
    </tr>
    <tr>
      <td style="padding:6px 32px 26px;">
{_signature_block()}
      </td>
    </tr>
  </table>
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
    <tr>
      <td style="padding:14px 8px;text-align:center;{_FONT}font-size:11px;color:#9aa0a8;">
        You received this because of your role on this project in BDR.<br>
        G3 Electrical &middot; Office {OFFICE_PHONE_DISPLAY}
      </td>
    </tr>
  </table>
</td></tr>
</table>
</body>
</html>"""
