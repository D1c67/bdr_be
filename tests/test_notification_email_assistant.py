"""The assistant's approval and split task notices in the notification email
helpers (app/services/notification_email.py; docs/ASSISTANT_PHASE1_PLAN.md
3.15, docs/ASSISTANT_PHASE2_PLAN.md 3.9.7):
heading and button per type, the Activity tab as the deep link even when
the card names a project, and a subject with no em dash."""

from types import SimpleNamespace

from app.assistant import approvals_job
from app.core.roles import Role
from app.services import notification_email as ne

FRONTEND = "https://bdr.example.com"
EM_DASH = chr(0x2014)
TYPES = (approvals_job.BELL_SUPERSEDED, approvals_job.BELL_FAILED, approvals_job.BELL_EXPIRED,
         "assistant.task_finished", "assistant.task_failed")


def _patch_frontend(monkeypatch):
    monkeypatch.setattr(ne, "get_settings", lambda: SimpleNamespace(frontend_url=FRONTEND + "/"))


def test_private_browser_notices_link_only_to_owned_ui(monkeypatch):
    _patch_frontend(monkeypatch)
    job = "97134da7-ddff-488c-a6d6-f62264f6f3a6"
    for kind in ("assistant.browser_help", "assistant.browser_task"):
        assert ne._deep_link(None, Role.EXECUTIVE.value, kind, {"browser_job_id": job}) == f"{FRONTEND}/assistant/browser/{job}"
        assert ne._deep_link(None, Role.EXECUTIVE.value, kind, {"browser_job_id": "https://evil.test/"}) == f"{FRONTEND}/assistant/browser"


def test_each_assistant_type_has_its_heading_and_button():
    assert ne._meta("assistant.approval_superseded") == ("An assistant change needs a fresh look", "Review the change")
    assert ne._meta("assistant.approval_failed") == ("An assistant change did not go through", "See what happened")
    assert ne._meta("assistant.approval_expired") == ("An assistant change was not made", "See why")
    # Phase 2 split task bells (docs/ASSISTANT_PHASE2_PLAN.md 3.9.7).
    assert ne._meta("assistant.task_finished") == ("An assistant split finished", "See the result")
    assert ne._meta("assistant.task_failed") == ("An assistant split did not finish", "See what happened")
    for type_ in TYPES:
        assert type_ in ne._TYPE_META
        heading, cta = ne._meta(type_)
        assert EM_DASH not in heading and EM_DASH not in cta
        assert ne.heading_for(type_) == heading


def test_assistant_notices_open_the_activity_tab(monkeypatch):
    _patch_frontend(monkeypatch)
    for type_ in TYPES:
        for project_id in ("p1", None):
            link = ne._deep_link(project_id, Role.EXECUTIVE.value, type_, {"approval_id": "a1"})
            assert link == f"{FRONTEND}/assistant?tab=activity", (type_, project_id)
        assert ne._deep_link("p1", Role.ESTIMATING_ENGINEER_LABOR.value, type_) == f"{FRONTEND}/assistant?tab=activity"
    # Estimators have no assistant: their floor link is unchanged.
    assert ne._deep_link("p1", Role.ESTIMATOR.value, TYPES[0]) == f"{FRONTEND}/estimator/projects/p1"


def test_assistant_subjects_carry_no_em_dash():
    project = {"number": "26.9.7130", "name": "ZZ TEST Probe"}
    for type_ in TYPES:
        subject = ne._subject(ne.heading_for(type_), project, type_)
        assert EM_DASH not in subject
        assert subject == f"G3 BDR · {ne.heading_for(type_)} · #26.9.7130 ZZ TEST Probe"
        assert ne._subject(ne.heading_for(type_), None, type_) == f"G3 BDR · {ne.heading_for(type_)}"
    # Older types keep their subject unchanged.
    assert EM_DASH in ne._subject("Quote received", project, "quote.received")


# Phase 3 routines (docs/ASSISTANT_PHASE3_PLAN.md 3.11).
SCHEDULE_TYPES = ("assistant.schedule_delivered", "assistant.schedule_paused",
                  "assistant.schedule_resumed", "assistant.schedule_ended")


def test_each_routine_type_has_its_heading_and_button():
    assert ne._meta("assistant.schedule_delivered") == ("Your assistant routine is ready", "Open Routines")
    assert ne._meta("assistant.schedule_paused") == ("An assistant routine was paused", "Open Routines")
    assert ne._meta("assistant.schedule_resumed") == ("An assistant routine resumed", "Open Routines")
    assert ne._meta("assistant.schedule_ended") == ("An assistant routine ended", "Open Routines")
    for type_ in SCHEDULE_TYPES:
        heading, cta = ne._meta(type_)
        assert EM_DASH not in heading and EM_DASH not in cta
        assert EM_DASH not in ne._subject(heading, {"number": "26.10.7267", "name": "Probe"}, type_)


def test_routine_notices_open_the_routines_tab(monkeypatch):
    _patch_frontend(monkeypatch)
    for type_ in SCHEDULE_TYPES:
        for project_id in ("p1", None):
            link = ne._deep_link(project_id, Role.ACCOUNTANT.value, type_, {"schedule_id": "s1"})
            assert link == f"{FRONTEND}/assistant?tab=routines", (type_, project_id)
    # The other assistant notices still open Activity.
    for type_ in TYPES:
        assert ne._deep_link("p1", Role.EXECUTIVE.value, type_) == f"{FRONTEND}/assistant?tab=activity"
    assert ne._deep_link("p1", Role.ESTIMATOR.value, SCHEDULE_TYPES[0]) == f"{FRONTEND}/estimator/projects/p1"


# Phase 7 proactive router (docs/ASSISTANT_PHASE7_PLAN.md 10.1, decision G).
INBOX_TYPES = ("assistant.inbox", "assistant.digest")


def test_the_router_bell_types_have_their_heading_and_button():
    assert ne._meta("assistant.inbox") == ("An assistant item needs a look", "Open your inbox")
    assert ne._meta("assistant.digest") == ("Your assistant digest", "Open your inbox")
    for type_ in INBOX_TYPES:
        assert type_ in ne._TYPE_META
        heading, cta = ne._meta(type_)
        assert EM_DASH not in heading and EM_DASH not in cta
        assert ne.heading_for(type_) == heading


def test_the_router_bell_subjects_carry_no_em_dash():
    project = {"number": "26.10.7301", "name": "ZZ TEST Probe"}
    for type_ in INBOX_TYPES:
        heading = ne.heading_for(type_)
        assert ne._subject(heading, project, type_) == f"G3 BDR · {heading} · #26.10.7301 ZZ TEST Probe"
        assert ne._subject(heading, None, type_) == f"G3 BDR · {heading}"
        assert EM_DASH not in ne._subject(heading, project, type_)


# The two entries are the only change to _TYPE_META: every earlier entry keeps
# its heading and button. Pinned as a digest of the pre-phase-7 table.
_PRE_PHASE_7_ENTRIES = {
    "verified": ("Pricing committed " + EM_DASH + " ready to send out", "Open Send Out"),
    "stage_handoff": ("A project moved to your stage", "Open project"),
    "gono_go": ("Project accepted " + EM_DASH + " send to estimator", "Open project"),
    "assigned": ("You were assigned to a project", "Open your assignment"),
    "estimate_revised": ("Estimator sent changes/revisions", "Review changes"),
    "rfq.initial_send": ("RFQs sent to vendors", "View RFQs"),
    "quote.received": ("A vendor quote came in", "View quotes"),
    "late_quote.received": ("A quote came in after the bid was submitted", "View quotes"),
    "note_mention": ("You were mentioned in a project note", "Open the conversation"),
    "nudge": ("You've been nudged about a to-do", "Open your to-dos"),
    "assistant.approval_superseded": ("An assistant change needs a fresh look", "Review the change"),
    "assistant.approval_failed": ("An assistant change did not go through", "See what happened"),
    "assistant.approval_expired": ("An assistant change was not made", "See why"),
    "assistant.browser_help": ("Your browser task needs help", "Open your browser task"),
    "assistant.browser_task": ("Your browser task has an update", "Open your browser task"),
    "assistant.task_finished": ("An assistant split finished", "See the result"),
    "assistant.task_failed": ("An assistant split did not finish", "See what happened"),
    "assistant.schedule_delivered": ("Your assistant routine is ready", "Open Routines"),
    "assistant.schedule_paused": ("An assistant routine was paused", "Open Routines"),
    "assistant.schedule_resumed": ("An assistant routine resumed", "Open Routines"),
    "assistant.schedule_ended": ("An assistant routine ended", "Open Routines"),
}


def test_every_pre_existing_type_meta_entry_is_unchanged():
    for key, value in _PRE_PHASE_7_ENTRIES.items():
        assert ne._TYPE_META[key] == value, key
    added = set(ne._TYPE_META) - set(_PRE_PHASE_7_ENTRIES)
    assert set(INBOX_TYPES) <= added


def test_the_router_bell_types_stay_off_the_email_path_deep_link_default(monkeypatch):
    _patch_frontend(monkeypatch)
    # _deep_link is not edited: the router passes its own link, and these bell
    # types never reach queue(); the default is the Activity tab like any assistant.* type.
    for type_ in INBOX_TYPES:
        assert ne._deep_link(None, Role.EXECUTIVE.value, type_, {}) == f"{FRONTEND}/assistant?tab=activity"
