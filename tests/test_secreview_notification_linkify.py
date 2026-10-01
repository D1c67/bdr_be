"""Security review, group notification-linkify (findings 26 and 37).

Linkification in notification mirror emails is keyed by the SOURCE of a URL,
not the notification type: an estimator note preview or an RFP-derived
project name can sit inside any house-authored message, so only links to
the app's own frontend become anchors. Everything else renders as plain,
escaped text."""

from types import SimpleNamespace

from app.services import email_branding as eb
from app.services import notification_email as ne

FRONTEND = "https://bdr.example.com"
PROFILE = {"id": "u1", "full_name": "Pat", "email": "pat@g3.com", "role": "executive", "is_active": True}


def _send(monkeypatch, type_, message, project=None):
    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(frontend_url=FRONTEND + "/"))
    sent = []
    monkeypatch.setattr(ne.graph_email, "send_mail", lambda **kw: sent.append(kw) or None)
    ne._send_one(
        {"id": None, "user_id": "u1", "project_id": "p1", "type": type_, "message": message, "metadata": {}},
        PROFILE, project,
    )
    assert len(sent) == 1
    return sent[0]["body_html"]


def test_estimator_note_url_is_not_linkified(monkeypatch):
    """Finding 26: an estimator's note preview never becomes a clickable link."""
    html = _send(
        monkeypatch, "estimator_note",
        "Estimator note on Tower: Updated takeoff here: https://g3-electrical-files.example/login",
    )
    assert '<a href="https://g3-electrical-files.example/login"' not in html
    assert "https://g3-electrical-files.example/login" in html  # still readable as text
    assert f'href="{FRONTEND}/projects/p1"' in html  # the app button stays a link


def test_rfp_derived_project_name_in_house_type_is_not_linkified(monkeypatch):
    """Finding 37: an RFP-derived project name inside a routine house-authored
    type (estimate_submitted, quote.received, ...) is plain text."""
    name = "Visit https://evil.example/login now"
    for type_ in ("estimate_submitted", "quote.received", "late_quote.received", "drawing_changed"):
        html = _send(
            monkeypatch, type_, f"Estimator submitted deliverables for {name}",
            {"id": "p1", "name": name, "number": "26.9.7204"},
        )
        assert '<a href="https://evil.example/login"' not in html, type_
        assert "https://evil.example/login" in html, type_


def test_app_links_still_linkify(monkeypatch):
    html = _send(monkeypatch, "quote.received", f"See {FRONTEND}/projects/p1?box=gcs for details.")
    assert f'<a href="{FRONTEND}/projects/p1?box=gcs"' in html


def test_lookalike_hosts_are_not_linkified():
    hosts = frozenset({"bdr.example.com"})
    for url in (
        "https://bdr.example.com.evil.example/x",
        "https://bdr.example.com@evil.example/x",
        "https://evil.example/bdr.example.com",
        "https://evilbdr.example.com/x",
        "https://bdr.example.com\\@evil.example/x",
    ):
        out = eb._paragraphs(f"Go {url} now", link_hosts=hosts)
        assert "<a " not in out, url
    assert "<a " in eb._paragraphs("Go https://BDR.example.com/x now", link_hosts=hosts)


def test_render_notification_email_defaults_to_house_hosts(monkeypatch):
    """Other callers of the shared renderer (bid date change, invites) get
    the same source-keyed rule without passing anything."""
    import app.core.config as config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(frontend_url=FRONTEND))
    html = eb.render_notification_email(
        recipient_name="Pat", heading="Heads up",
        message=f"Bid date moved for https://evil.example/x. Open {FRONTEND}/projects/p1",
        cta_label="Open", cta_url=f"{FRONTEND}/projects/p1",
    )
    assert '<a href="https://evil.example/x"' not in html
    assert f'<a href="{FRONTEND}/projects/p1" style=' in html
