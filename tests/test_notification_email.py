"""Branded notification-email helpers: type→heading/CTA mapping, role-aware
deep links, HTML rendering, queue gating, and the per-recipient send path.

Nothing here touches the network: graph_email.send_mail and the Supabase client
are faked, and the threading hand-off is replaced with a synchronous shim.
"""

from types import SimpleNamespace

from app.core.roles import Role
from app.services import email_branding as eb
from app.services import notification_email as ne

FRONTEND = "https://bdr.example.com"


# ── type → (heading, CTA) ──────────────────────────────────────────────────


def test_meta_exact_match():
    assert ne._meta("quote.received") == ("A vendor quote came in", "View quotes")
    assert ne._meta("assigned")[1] == "Open your assignment"
    assert ne._meta("rfq.reply_received")[0] == "A vendor replied to an RFQ"


def test_meta_due_prefix_and_expired():
    assert ne._meta("due.internal_bid.2w") == ("Deadline approaching", "Open project")
    assert ne._meta("due.actual_bid.expired") == ("Deadline passed", "Open project")


def test_meta_unknown_falls_back():
    assert ne._meta("brand_new_type") == ne._DEFAULT_META


# ── role-aware deep links ───────────────────────────────────────────────────


def _patch_frontend(monkeypatch):
    monkeypatch.setattr(
        ne, "get_settings", lambda: SimpleNamespace(frontend_url=FRONTEND + "/")
    )


def test_deep_link_internal_project(monkeypatch):
    _patch_frontend(monkeypatch)
    assert ne._deep_link("p1", Role.PM.value) == f"{FRONTEND}/projects/p1"


def test_deep_link_estimator_project(monkeypatch):
    _patch_frontend(monkeypatch)
    assert ne._deep_link("p1", Role.ESTIMATOR.value) == f"{FRONTEND}/estimator/projects/p1"


def test_deep_link_no_project_falls_back_to_home(monkeypatch):
    _patch_frontend(monkeypatch)
    assert ne._deep_link(None, Role.PA.value) == f"{FRONTEND}/dashboard"
    assert ne._deep_link(None, Role.ESTIMATOR.value) == f"{FRONTEND}/estimator"


def test_meta_nudge():
    assert ne._meta("nudge") == ("You've been nudged about a to-do", "Open your to-dos")


def test_deep_link_nudge_goes_to_todos(monkeypatch):
    _patch_frontend(monkeypatch)
    assert ne._deep_link(None, Role.PM.value, "nudge") == f"{FRONTEND}/todos"


# ── labels & subjects ───────────────────────────────────────────────────────


def test_project_label_variants():
    assert ne._project_label({"number": "42", "name": "Acme"}) == "#42 · Acme"
    assert ne._project_label({"number": None, "name": "Acme"}) == "Acme"
    assert ne._project_label({"number": "42", "name": None}) == "#42"
    assert ne._project_label(None) is None


def test_subject_includes_project_tag():
    s = ne._subject("Quote received", {"number": "42", "name": "Acme"})
    assert s == "G3 BDR · Quote received — #42 Acme"
    assert ne._subject("Security alert", None) == "G3 BDR · Security alert"


# ── HTML rendering ──────────────────────────────────────────────────────────


def test_render_notification_email_has_button_logo_and_signature():
    html = eb.render_notification_email(
        recipient_name="Jane Smith",
        heading="A vendor quote came in",
        message="Quote received from Acme for $1,200 on Tower.",
        cta_label="View quotes",
        cta_url=f"{FRONTEND}/projects/p1",
        project_label="#42 · Tower",
    )
    assert "Hi Jane," in html
    assert "A vendor quote came in" in html
    assert "View quotes" in html
    assert f'href="{FRONTEND}/projects/p1"' in html        # the button + fallback link
    assert f'src="cid:{eb.LOGO_CONTENT_ID}"' in html       # inline logo
    assert eb.OFFICE_PHONE_DISPLAY in html                 # signature phone
    assert "#42 · Tower" in html                           # project chip


def test_render_notification_email_escapes_message_and_handles_no_name():
    html = eb.render_notification_email(
        recipient_name=None,
        heading="Heads up",
        message="Watch <script> & sons",
        cta_label="Open",
        cta_url=f"{FRONTEND}/dashboard",
    )
    assert "Hi there," in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


# ── queue gating ────────────────────────────────────────────────────────────


class _SyncThread:
    """Runs the target inline so queue()'s hand-off is observable in tests."""

    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def test_queue_noop_when_disabled(monkeypatch):
    started = []
    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(
        notification_emails_enabled=False, ms_client_id="present"))
    monkeypatch.setattr(ne.threading, "Thread",
                        lambda **k: SimpleNamespace(start=lambda: started.append(True)))
    ne.queue([{"user_id": "u1", "type": "assigned", "message": "x"}])
    assert started == []


def test_queue_noop_without_graph_creds(monkeypatch):
    started = []
    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(
        notification_emails_enabled=True, ms_client_id=""))
    monkeypatch.setattr(ne.threading, "Thread",
                        lambda **k: SimpleNamespace(start=lambda: started.append(True)))
    ne.queue([{"user_id": "u1", "type": "assigned", "message": "x"}])
    assert started == []


def test_queue_drops_rows_without_user_id(monkeypatch):
    started = []
    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(
        notification_emails_enabled=True, ms_client_id="present"))
    monkeypatch.setattr(ne.threading, "Thread",
                        lambda **k: SimpleNamespace(start=lambda: started.append(True)))
    ne.queue([{"user_id": None, "type": "x", "message": "y"}])
    assert started == []


# ── end-to-end send via the fake transport ──────────────────────────────────


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeSupabase:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


def test_queue_sends_one_email_per_recipient(monkeypatch):
    sent = []
    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(
        notification_emails_enabled=True, ms_client_id="present", frontend_url=FRONTEND))
    monkeypatch.setattr(ne.threading, "Thread", _SyncThread)
    monkeypatch.setattr(ne, "get_supabase", lambda: _FakeSupabase({
        "profiles": [
            {"id": "pe1", "full_name": "Pat E", "email": "pat@g3.com",
             "role": "pe", "is_active": True},
            {"id": "off1", "full_name": "No Mail", "email": None,
             "role": "pm", "is_active": True},
            {"id": "gone", "full_name": "Inactive", "email": "x@g3.com",
             "role": "pm", "is_active": False},
        ],
        "projects": [{"id": "p1", "name": "Tower", "number": "42"}],
    }))
    monkeypatch.setattr(ne.graph_email, "send_mail",
                        lambda **kw: sent.append(kw) or {"id": "log"})

    ne.queue([
        {"user_id": "pe1", "project_id": "p1", "type": "quote.received", "message": "Quote in"},
        {"user_id": "off1", "project_id": "p1", "type": "quote.received", "message": "Quote in"},
        {"user_id": "gone", "project_id": "p1", "type": "quote.received", "message": "Quote in"},
    ])

    # Only the active recipient with an address is emailed.
    assert len(sent) == 1
    kw = sent[0]
    assert kw["to"] == ["pat@g3.com"]
    assert kw["subject"] == "G3 BDR · A vendor quote came in — #42 Tower"
    assert kw["project_id"] == "p1"
    # Inline logo travels with the message so cid:g3-logo resolves.
    assert kw["inline_images"][0][0] == eb.LOGO_CONTENT_ID
    assert f"{FRONTEND}/projects/p1" in kw["body_html"]


def test_send_failure_is_isolated(monkeypatch):
    sent = []

    def _boom_then_ok(**kw):
        if not sent:
            sent.append("boom")
            raise RuntimeError("graph down")
        sent.append(kw)
        return {"id": "log"}

    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(
        notification_emails_enabled=True, ms_client_id="present", frontend_url=FRONTEND))
    monkeypatch.setattr(ne.threading, "Thread", _SyncThread)
    monkeypatch.setattr(ne, "get_supabase", lambda: _FakeSupabase({
        "profiles": [
            {"id": "a", "full_name": "A", "email": "a@g3.com", "role": "pm", "is_active": True},
            {"id": "b", "full_name": "B", "email": "b@g3.com", "role": "pm", "is_active": True},
        ],
        "projects": [],
    }))
    monkeypatch.setattr(ne.graph_email, "send_mail", _boom_then_ok)

    # Must not raise even though the first send blows up.
    ne.queue([
        {"user_id": "a", "project_id": None, "type": "stage_handoff", "message": "m"},
        {"user_id": "b", "project_id": None, "type": "stage_handoff", "message": "m"},
    ])
    assert len(sent) == 2  # both attempted; the failure didn't abort the batch
