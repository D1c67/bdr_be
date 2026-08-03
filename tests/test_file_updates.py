"""Pure-logic + thin-endpoint tests for the team→estimator file-update rules:
the send-based hand-off lock, estimator visibility of updates and addenda,
addendum metadata validation, the role-branched lock/list endpoints, and the
branded package/updates/reassign email rendering.

The lock semantics changed in this feature: an active-but-UNSENT assignment no
longer locks (a package must actually have been emailed). The two lock tests the
spec named are rewritten to that send-based rule, not deleted."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import files as files_mod
from app.routers.files import (
    ADDENDUM_CATEGORY,
    ADDENDUM_NUMBER_MAX_CHARS,
    ESTIMATOR_READ,
    ESTIMATOR_WRITE,
    INITIAL_CATEGORIES,
    SENT_GATED_CATEGORIES,
    UPDATE_CATEGORIES,
    VALID_CATEGORIES,
    FileNoteIn,
    _estimator_visible,
    is_handoff_locked,
    list_files,
    lock_state,
    upload_file,
)
from app.services import estimator_email as ee
from app.services import file_sends


def _writer(role=Role.ESTIMATING_ADMIN):
    return CurrentUser(
        id="w1", email="w@g3.com", role=role, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


def _estimator(uid="est1"):
    return CurrentUser(
        id=uid, email="est@ext.com", role=Role.ESTIMATOR, is_active=True, is_dev=False,
        aal="aal2", mfa_enrolled=True,
    )


# ── is_handoff_locked (SEND-based, rewritten) ──────────────────────────────


def test_no_assignments_is_unlocked():
    assert is_handoff_locked([]) is False


def test_active_but_unsent_assignment_does_not_lock():
    # REWRITE of test_active_assignment_locks (§3.2, deliberate): an active but
    # never-emailed assignment no longer locks. This unblocks the previously
    # unrecoverable state — an estimator assigned while Graph was down, so the
    # package never left the building yet the drawings were frozen with nothing
    # sent. Nothing was sent → not locked.
    assert is_handoff_locked([{"revoked_at": None, "sent_to_estimator_at": None}]) is False


def test_revoked_never_sent_unlocks_again():
    # Assign → revoke before anything was emailed: the package never left the
    # building, so the initial blocks reopen. Under the send-based rule this is
    # now the same fact as "never sent" — an unsent row (revoked or not) never
    # locks.
    assert is_handoff_locked([{"revoked_at": "2026-07-01T00:00:00Z", "sent_to_estimator_at": None}]) is False


def test_revoked_but_sent_stays_locked():
    # The package went out — what the estimator received must stay reconstructable.
    assert (
        is_handoff_locked(
            [{"revoked_at": "2026-07-01T00:00:00Z", "sent_to_estimator_at": "2026-06-30T00:00:00Z"}]
        )
        is True
    )


def test_mixed_rows_any_sent_locks():
    # REWRITE of test_mixed_rows_any_active_locks: a mix where at least one row
    # carries sent_to_estimator_at locks; a mix of active-unsent + revoked-unsent
    # does not (nothing was ever emailed).
    locked = [
        {"revoked_at": "2026-07-01T00:00:00Z", "sent_to_estimator_at": None},
        {"revoked_at": None, "sent_to_estimator_at": "2026-06-30T00:00:00Z"},
    ]
    assert is_handoff_locked(locked) is True
    unlocked = [
        {"revoked_at": "2026-07-01T00:00:00Z", "sent_to_estimator_at": None},
        {"revoked_at": None, "sent_to_estimator_at": None},
    ]
    assert is_handoff_locked(unlocked) is False


# ── category sets ──────────────────────────────────────────────────────────


def test_update_categories_are_uploadable_and_internal_only():
    assert UPDATE_CATEGORIES <= VALID_CATEGORIES
    # The estimator can never author updates — they only receive them.
    assert not (UPDATE_CATEGORIES & ESTIMATOR_WRITE)
    # Same for the whole sent-gated family (updates + addenda).
    assert SENT_GATED_CATEGORIES <= VALID_CATEGORIES
    assert not (SENT_GATED_CATEGORIES & ESTIMATOR_WRITE)


def test_specifications_are_estimator_readable():
    # Decision reversal of migration 0037: specs are part of the package now.
    assert "specification" in ESTIMATOR_READ


def test_initial_and_update_sets_are_disjoint():
    # The canary for the "just widen a set" shortcut — keep verbatim.
    assert not (INITIAL_CATEGORIES & UPDATE_CATEGORIES)
    # Addendum belongs to NEITHER branch (it uploads on both sides of the lock).
    assert ADDENDUM_CATEGORY not in INITIAL_CATEGORIES
    assert ADDENDUM_CATEGORY not in UPDATE_CATEGORIES


def test_addendum_category_membership():
    # Spec §8.2 #26: addendum is a real, uploadable, sent-gated, internal-authored
    # category that is neither an initial block nor a post-hand-off update.
    assert ADDENDUM_CATEGORY in VALID_CATEGORIES
    assert ADDENDUM_CATEGORY in SENT_GATED_CATEGORIES
    assert ADDENDUM_CATEGORY not in ESTIMATOR_WRITE
    assert ADDENDUM_CATEGORY not in ESTIMATOR_READ
    assert ADDENDUM_CATEGORY not in UPDATE_CATEGORIES
    assert ADDENDUM_CATEGORY not in INITIAL_CATEGORIES


# ── _estimator_visible (now takes user_id) ─────────────────────────────────


def test_estimator_sees_initial_and_own_deliverables():
    uid = "me"
    for cat in ["drawing", "specification"]:
        assert _estimator_visible({"category": cat, "sent_to_estimators_at": None}, uid) is True
    # ESTIMATOR_WRITE rows are uploader-scoped — pass a matching uploaded_by.
    for cat in ["estimate", "boq", "markup"]:
        assert _estimator_visible(
            {"category": cat, "sent_to_estimators_at": None, "uploaded_by": uid}, uid
        ) is True


def test_estimator_write_reads_are_uploader_scoped():
    # F1 (§8.2 #28): a deliverable belongs to whoever uploaded it — one estimator
    # never reads a competitor's workbook, even in the same ESTIMATOR_WRITE set.
    assert _estimator_visible({"category": "estimate", "uploaded_by": "other"}, "me") is False
    assert _estimator_visible({"category": "estimate", "uploaded_by": "me"}, "me") is True


def test_estimator_never_sees_unsent_updates():
    uid = "me"
    for cat in ["revision", "additional", "addendum"]:
        assert _estimator_visible({"category": cat, "sent_to_estimators_at": None}, uid) is False


def test_estimator_sees_sent_updates_and_addenda():
    uid = "me"
    for cat in ["revision", "additional", "addendum"]:
        assert (
            _estimator_visible({"category": cat, "sent_to_estimators_at": "2026-07-01T00:00:00Z"}, uid)
            is True
        )


def test_estimator_never_sees_internal_categories():
    uid = "me"
    for cat in ["rfq_split", "quote", "proposal", "other"]:
        assert _estimator_visible({"category": cat, "sent_to_estimators_at": None}, uid) is False


def test_estimator_visible_uses_get_not_index_for_stamp():
    # Regression guard: a dict with no sent_to_estimators_at key must not KeyError
    # (build_log / list_file rows may omit it). .get(), never [].
    assert _estimator_visible({"category": "addendum"}, "me") is False


# ── FileNoteIn validation ──────────────────────────────────────────────────


def test_note_is_stripped():
    assert FileNoteIn(note="  sheet E-3 reissued  ").note == "sheet E-3 reissued"


def test_blank_note_rejected():
    with pytest.raises(ValidationError):
        FileNoteIn(note="   \n ")


def test_oversize_note_rejected():
    with pytest.raises(ValidationError):
        FileNoteIn(note="x" * 2001)


# ── addendum upload endpoint (validation + both-sides-of-the-lock) ─────────


class _FakeUpload:
    """Minimal UploadFile stand-in — only .filename/.size/.read are touched, and
    only on the happy path (every validation 400 fires before _read_capped)."""

    def __init__(self, content=b"pdfbytes", filename="addendum.pdf"):
        self.filename = filename
        self.size = len(content)
        self._chunks = [content]

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


async def _do_upload(**kw):
    """Call the upload_file coroutine with the non-Form params defaulted — when
    invoked directly (not via FastAPI) the Form(None) defaults are FieldInfo
    objects, not None, so note/material_category_id must be passed explicitly."""
    kw.setdefault("background", BackgroundTasks())
    kw.setdefault("material_category_id", None)
    kw.setdefault("note", None)
    kw.setdefault("doc_type", None)
    kw.setdefault("addendum_number", None)
    kw.setdefault("addendum_issued_on", None)
    kw.setdefault("file", _FakeUpload())
    return await upload_file(**kw)


class _InsertSB:
    """Echoes the inserted project_files payload back as the created row."""

    def __init__(self):
        self.inserted = None

    def table(self, name):
        return self

    def insert(self, payload):
        self.inserted = payload
        return self

    def execute(self):
        return SimpleNamespace(data=[{**(self.inserted or {}), "id": "new-file"}])


def _upload_env(monkeypatch, *, forbid_lock=False):
    sb = _InsertSB()
    monkeypatch.setattr(files_mod, "get_supabase", lambda: sb)
    monkeypatch.setattr(files_mod.storage, "build_object_path", lambda *a, **k: "p1/addendum/x")
    monkeypatch.setattr(files_mod.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(files_mod.office_preview, "is_convertible", lambda *a, **k: False)
    monkeypatch.setattr(files_mod, "audit", lambda *a, **k: None)
    if forbid_lock:
        def _boom(_pid):
            raise AssertionError("handoff_locked must not gate an addendum upload")
        monkeypatch.setattr(files_mod, "handoff_locked", _boom)
    return sb


async def test_addendum_upload_accepted_regardless_of_lock(monkeypatch):
    # Task #1 / §8.2 #29 (skew OK): an addendum is uploadable on BOTH sides of the
    # hand-off lock, because it is in neither UPDATE_CATEGORIES nor
    # INITIAL_CATEGORIES — upload_file never even consults handoff_locked for it.
    # forbid_lock makes handoff_locked blow up if touched. One day ahead (clock
    # skew) is accepted.
    sb = _upload_env(monkeypatch, forbid_lock=True)
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    row = await _do_upload(
        project_id="p1",
        category="addendum",
        addendum_number="3A",
        addendum_issued_on=tomorrow,
        user=_writer(),
    )
    assert row["category"] == "addendum"
    assert sb.inserted["addendum_number"] == "3A"
    assert sb.inserted["addendum_issued_on"] == tomorrow
    assert sb.inserted["note"] is None  # addenda carry no note


async def test_addendum_upload_refused_for_estimator():
    # Task #2: 'addendum' is not in ESTIMATOR_WRITE, so an estimator is 403'd at
    # the write gate — this is the only thing enforcing "estimators view addenda
    # but never upload them". Fires before any storage/DB work, so no mocks.
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1", category="addendum",
            addendum_number="1", addendum_issued_on="2026-07-14", user=_estimator(),
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "number, issued",
    [
        ("", "2026-07-14"),             # blank number
        ("x" * (ADDENDUM_NUMBER_MAX_CHARS + 1), "2026-07-14"),  # 41 chars
        ("1", ""),                       # missing date
        ("1", None),                     # missing date (None)
        ("1", "2026-13-01"),             # not a real calendar date
    ],
)
async def test_addendum_metadata_validation_rejects(number, issued):
    # Task #3 / §8.2 #29: every bad number/date 400s, all before any I/O.
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1", category="addendum",
            addendum_number=number, addendum_issued_on=issued, user=_writer(),
        )
    assert exc.value.status_code == 400


async def test_addendum_two_days_future_rejected():
    two_days = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1", category="addendum",
            addendum_number="1", addendum_issued_on=two_days, user=_writer(),
        )
    assert exc.value.status_code == 400


async def test_addendum_metadata_on_non_addendum_rejected():
    # Addendum number on a plain drawing → 400 (mirrors the DB CHECK).
    with pytest.raises(HTTPException) as exc:
        await _do_upload(
            project_id="p1", category="drawing",
            addendum_number="1", addendum_issued_on=None,
            file=_FakeUpload(filename="E-101.pdf"), user=_writer(),
        )
    assert exc.value.status_code == 400


# ── estimator deliverable notes (optional, attached at upload) ─────────────


async def test_estimator_deliverable_carries_an_optional_note(monkeypatch):
    # The estimator portal's upload boxes let the estimator type a note that is
    # attached to every file in that drop. `estimate` is in neither
    # INITIAL_CATEGORIES nor UPDATE_CATEGORIES, so the note stays OPTIONAL and
    # the lock is never consulted (forbid_lock proves it) — it is stored, and
    # trimmed, exactly like a team-side update note.
    sb = _upload_env(monkeypatch, forbid_lock=True)
    row = await _do_upload(
        project_id="p1", category="estimate", note="  Priced per Addendum 2  ",
        file=_FakeUpload(filename="estimate.xlsx"), user=_estimator(),
    )
    assert sb.inserted["note"] == "Priced per Addendum 2"
    assert sb.inserted["estimator_deliverable"] is True
    assert row["category"] == "estimate"


async def test_estimator_deliverable_without_a_note_is_accepted(monkeypatch):
    # The note is a convenience, never a gate: NOTE_REQUIRED_MESSAGE belongs to
    # UPDATE_CATEGORIES alone. A blank one normalises to NULL, not "".
    sb = _upload_env(monkeypatch, forbid_lock=True)
    await _do_upload(
        project_id="p1", category="boq", note="   ",
        file=_FakeUpload(filename="boq.xlsx"), user=_estimator(),
    )
    assert sb.inserted["note"] is None


# ── lock_state / list_files role branch ────────────────────────────────────


def _fake_stats(pid, estimator_id=None):
    if estimator_id:
        return {"batch_count": 2, "package_sent_at": None, "first_sent_at": None, "last_sent_at": None}
    return {
        "batch_count": 3,
        "package_sent_at": "2026-07-01T00:00:00Z",
        "first_sent_at": "2026-06-30T00:00:00Z",
        "last_sent_at": "2026-07-02T00:00:00Z",
    }


def test_lock_state_estimator_shape(monkeypatch):
    # §8.2 #34: the estimator gets EXACTLY {locked, sent}; `sent` reflects only
    # their own batches (batch_stats called with estimator_id).
    monkeypatch.setattr(files_mod, "handoff_locked", lambda pid: True)
    monkeypatch.setattr(file_sends, "batch_stats", _fake_stats)
    out = lock_state("p1", _estimator())
    assert out == {"locked": True, "sent": True}


def test_lock_state_internal_shape(monkeypatch):
    # Internal gets {locked, sent, batch_count, first_sent_at}; sent = a
    # kind='initial' batch EXISTS (package_sent_at not None).
    monkeypatch.setattr(files_mod, "handoff_locked", lambda pid: True)
    monkeypatch.setattr(file_sends, "batch_stats", _fake_stats)
    out = lock_state("p1", _writer())
    assert out == {
        "locked": True,
        "sent": True,
        "batch_count": 3,
        "first_sent_at": "2026-06-30T00:00:00Z",
    }


class _ListSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


def test_list_files_blanks_sent_stamp_for_estimator(monkeypatch):
    # §8.2 #35: the raw send timestamp is blanked for the estimator (it would
    # leak that earlier sends — and therefore other recipients — exist).
    monkeypatch.setattr(
        files_mod, "get_supabase",
        lambda: _ListSB([
            {"id": "f1", "category": "drawing",
             "sent_to_estimators_at": "2026-07-01T00:00:00Z", "uploaded_by": "x"}
        ]),
    )
    out = list_files("p1", _estimator())
    assert out and out[0]["sent_to_estimators_at"] is None


def test_list_files_keeps_sent_stamp_for_internal(monkeypatch):
    monkeypatch.setattr(
        files_mod, "get_supabase",
        lambda: _ListSB([
            {"id": "f1", "category": "drawing",
             "sent_to_estimators_at": "2026-07-01T00:00:00Z", "uploaded_by": "x"}
        ]),
    )
    out = list_files("p1", _writer())
    assert out[0]["sent_to_estimators_at"] == "2026-07-01T00:00:00Z"


# ── updates_label ──────────────────────────────────────────────────────────
# _UPDATE_LABELS order (addendum, revision, additional) is LOAD-BEARING: it keeps
# an addenda-only batch from mislabelling as "Changes/Revisions" and fixes the
# phrase order in mixed batches. Do not reorder.


def test_label_revisions_only():
    assert ee.updates_label([{"category": "revision"}]) == "Changes/Revisions"


def test_label_additional_only():
    assert ee.updates_label([{"category": "additional"}]) == "Additional files"


def test_label_both():
    files = [{"category": "revision"}, {"category": "additional"}]
    assert ee.updates_label(files) == "Changes/Revisions & Additional files"


def test_label_addendum_only():
    assert ee.updates_label([{"category": "addendum"}]) == "Addenda"


def test_label_addendum_and_revision():
    files = [{"category": "revision"}, {"category": "addendum"}]
    assert ee.updates_label(files) == "Addenda & Changes/Revisions"


def test_label_all_three():
    files = [{"category": "additional"}, {"category": "revision"}, {"category": "addendum"}]
    assert ee.updates_label(files) == "Addenda & Changes/Revisions & Additional files"


# ── email rendering ────────────────────────────────────────────────────────

PROJ = {"id": "p1", "name": "Van Ness <Tower>", "number": "26-014", "due_from_estimator_at": "2026-07-10"}

FILES = [
    {"category": "drawing", "filename": "E-101.pdf", "storage_path": "p1/drawing/a", "note": None},
    {"category": "specification", "filename": "spec.pdf", "storage_path": "p1/specification/b", "note": None},
    {
        "category": "revision",
        "filename": "E-101_rev2.pdf",
        "storage_path": "p1/revision/c",
        "note": "Panel schedule <changed> on sheet E-101",
    },
    {"category": "additional", "filename": "geotech.pdf", "storage_path": "p1/additional/d", "note": "FYI only"},
]

ADDENDUM_FILE = {
    "category": "addendum",
    "filename": "addendum3.pdf",
    "storage_path": "p1/addendum/e",
    "note": None,
    "addendum_number": "3A",
    "addendum_issued_on": "2026-07-14",
}

signer = lambda path: f"https://signed.example/{path}"  # noqa: E731


def test_sections_group_and_tag_initial_vs_updates():
    html = ee.render_sections(FILES, signer)
    assert "Electrical drawings" in html
    assert "Specifications" in html
    assert "Changes/Revisions" in html
    assert "Additional files" in html
    assert html.count(ee.INITIAL_TAG) == 2  # drawings + specs
    assert html.count(ee.UPDATE_TAG) == 2  # revision + additional


def test_sections_omit_empty_categories():
    html = ee.render_sections(FILES[:1], signer)
    assert "Electrical drawings" in html
    assert "Specifications" not in html
    assert ee.UPDATE_TAG not in html


def test_sections_link_via_signer_and_escape_notes():
    html = ee.render_sections(FILES, signer)
    assert "https://signed.example/p1/revision/c" in html
    # Notes render beside their file, HTML-escaped.
    assert "Panel schedule &lt;changed&gt; on sheet E-101" in html
    assert "<changed>" not in html


def test_sections_render_addendum_without_pill():
    # §8.2 #31: an addendum renders its number + issue date and carries NEITHER
    # the "Initial files" nor the "Sent after hand-off" pill (it lives on both
    # sides of the lock). Rendered in isolation so a pill on another section
    # cannot false-pass.
    html = ee.render_sections([ADDENDUM_FILE], signer)
    assert "Addenda" in html
    assert "Addendum 3A" in html
    assert "issued 2026-07-14" in html
    assert ee.INITIAL_TAG not in html
    assert ee.UPDATE_TAG not in html


def test_sections_escape_addendum_number():
    row = {**ADDENDUM_FILE, "addendum_number": "3A<b>"}
    html = ee.render_sections([row], signer)
    assert "3A&lt;b&gt;" in html
    assert "3A<b>" not in html


def test_package_email_greets_and_brands():
    html = ee.render_package_email(proj=PROJ, files=FILES, recipient_name="Jane Smith", signer=signer)
    assert "Hi Jane," in html
    assert "Van Ness &lt;Tower&gt;" in html  # project name escaped
    assert "26-014" in html
    assert "2026-07-10" in html  # due back
    assert ee.PORTAL_LINE_PACKAGE in html
    assert "G3 ELECTRICAL" in html  # branded shell
    assert "ESTIMATE FILES" in html  # header subtitle


def test_package_email_renders_and_omits_message(monkeypatch):
    # §8.2 #32: a message block renders when present, and is absent when blank.
    with_msg = ee.render_package_email(
        proj=PROJ, files=FILES, recipient_name="Jane", signer=signer, message="Addendum 2 dropped"
    )
    assert "MESSAGE FROM THE G3 TEAM" in with_msg
    assert "Addendum 2 dropped" in with_msg
    blank = ee.render_package_email(
        proj=PROJ, files=FILES, recipient_name="Jane", signer=signer, message="  "
    )
    assert "MESSAGE FROM THE G3 TEAM" not in blank


def test_reassign_email_catchup(monkeypatch):
    # §8.2 #33: the catch-up variant renders the full sections + an "Update
    # history" table + the CATCH-UP subtitle through the branded shell, with the
    # project name escaped. It adds NO red of its own — the only red is the
    # shell's accent band, which every branded email carries (verified equal to
    # the plain package email; the spec's "no _RED" refers to the body content).
    from app.services.email_branding import _RED

    prior = [
        {"kind": "initial", "sent_at": "2026-07-01T00:00:00Z", "summary": {"drawing": 2}},
        {"kind": "revision", "sent_at": "2026-07-05T00:00:00Z", "summary": {"revision": 1}},
    ]
    html = ee.render_reassign_email(
        proj=PROJ, files=FILES, recipient_name="Jane", signer=signer, prior=prior
    )
    assert "Update history" in html
    assert "CATCH-UP" in html
    assert "Electrical drawings" in html
    assert "Changes/Revisions" in html
    assert "Van Ness &lt;Tower&gt;" in html
    assert "Van Ness <Tower>" not in html
    package = ee.render_package_email(proj=PROJ, files=FILES, recipient_name="Jane", signer=signer)
    assert html.count(_RED) == package.count(_RED)  # catch-up injects no extra red


def test_updates_email_includes_message_and_notes():
    html = ee.render_updates_email(proj=PROJ, files=FILES[2:], message="Addendum 2 dropped today", signer=signer)
    assert "Addendum 2 dropped today" in html
    assert "MESSAGE FROM THE G3 TEAM" in html
    assert "FYI only" in html
    assert "FILE UPDATES" in html


def test_updates_email_omits_empty_message_block():
    html = ee.render_updates_email(proj=PROJ, files=FILES[2:], message="  ", signer=signer)
    assert "MESSAGE FROM THE G3 TEAM" not in html
