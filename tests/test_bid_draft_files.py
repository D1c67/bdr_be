"""Files on saved New Bid drafts (0109) - upload/patch/delete/list under
/bid-drafts/{id}/files, plus the transfer that MOVES everything onto a project.

What these tests pin: the four intake categories only; addendum metadata
optional at draft stage but validated whenever present (and clearable); the
storage key scheme (drafts/{draft_id}/{category}/... with the suffix preserved
by the transfer's prefix swap, so the landed key is identical to a fresh
upload's); the project_files insert shaped exactly like a fresh intake upload
through files.py; the 400-with-nothing-moved precheck on incomplete addenda;
and transfer retryability - a mid-way failure leaves already-moved files
retired from bid_draft_files so a retry only processes the remainder.
"""

from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.core.deps import require_writer
from app.core.ratelimit import upload_rate_limit
from app.routers import bid_drafts as bd
from app.routers.bid_drafts import BidDraftFileMetaIn, BidDraftTransferIn
from tests.test_bid_drafts import FakeDB, _draft_in, _user

TODAY = date.today().isoformat()
FUTURE = (date.today() + timedelta(days=30)).isoformat()
PROJECT_ID = str(uuid4())


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeStore:
    """In-memory stand-in for the storage helpers bid_drafts.py calls."""

    def __init__(self):
        self.uploads = []  # (path, mime)
        self.deleted = []  # paths handed to delete_file
        self.moves = []  # (src, dest)
        self.swept = []  # draft ids handed to delete_draft_prefix
        self.objects = set()  # object keys currently present
        self.fail_moves = set()  # src paths whose move raises
        self.fail_deletes = set()  # paths whose delete raises
        self._n = 0

    def build_draft_object_path(self, draft_id, category, filename):
        self._n += 1
        return f"drafts/{draft_id}/{category}/obj{self._n}-{filename}"

    def upload_file(self, path, content, mime):
        self.uploads.append((path, mime))
        self.objects.add(path)

    def delete_file(self, path):
        if path in self.fail_deletes:
            raise RuntimeError("storage delete failed")
        self.deleted.append(path)
        self.objects.discard(path)

    def move_object(self, src, dest):
        if src in self.fail_moves:
            raise RuntimeError("storage move failed")
        if src not in self.objects:
            raise RuntimeError("source object missing")
        self.objects.discard(src)
        self.objects.add(dest)
        self.moves.append((src, dest))

    def object_exists(self, path):
        return path in self.objects

    def delete_draft_prefix(self, draft_id):
        self.swept.append(draft_id)
        for key in [k for k in self.objects if k.startswith(f"drafts/{draft_id}/")]:
            self.objects.discard(key)


@pytest.fixture()
def env(monkeypatch):
    db = FakeDB()
    store = FakeStore()
    audits = []
    monkeypatch.setattr(bd, "get_supabase", lambda: db)
    monkeypatch.setattr(bd, "audit", lambda *a: audits.append(a))
    monkeypatch.setattr(bd.storage, "build_draft_object_path", store.build_draft_object_path)
    monkeypatch.setattr(bd.storage, "upload_file", store.upload_file)
    monkeypatch.setattr(bd.storage, "delete_file", store.delete_file)
    monkeypatch.setattr(bd.storage, "move_object", store.move_object)
    monkeypatch.setattr(bd.storage, "object_exists", store.object_exists)
    monkeypatch.setattr(bd.storage, "delete_draft_prefix", store.delete_draft_prefix)
    monkeypatch.setattr(bd.office_preview, "is_convertible", lambda *a, **k: False)
    return SimpleNamespace(db=db, store=store, audits=audits)


class _FakeUpload:
    """Minimal UploadFile stand-in - only .filename/.size/.read are touched."""

    def __init__(self, content=b"pdfbytes", filename="file.pdf"):
        self.filename = filename
        self.size = len(content)
        self._chunks = [content]

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


def _mk_draft(name="Lab Fit-Out"):
    return bd.create_draft(_draft_in(name=name), user=_user())


async def _upload(draft_id, category="drawing", filename="E-101.pdf", content=b"pdf", **kw):
    kw.setdefault("doc_type", None)
    kw.setdefault("addendum_number", None)
    kw.setdefault("addendum_issued_on", None)
    kw.setdefault("user", _user())
    return await bd.upload_draft_file(
        draft_id=draft_id,
        category=category,
        file=_FakeUpload(content, filename),
        **kw,
    )


def _transfer(draft_id, project_id=PROJECT_ID, background=None, uid="admin-1"):
    return bd.transfer_draft(
        draft_id,
        BidDraftTransferIn(project_id=project_id),
        background or BackgroundTasks(),
        user=_user(uid=uid),
    )


def _seed_project(db, abandoned_at=None):
    db.tables["projects"] = [{"id": PROJECT_ID, "abandoned_at": abandoned_at}]


# ── Role gate + rate limit wiring ────────────────────────────────────────────


def _route(path, method):
    for r in bd.router.routes:
        if r.path == path and method in r.methods:
            return r
    raise AssertionError(f"no route {method} {path}")


def test_new_routes_are_writer_gated_and_upload_is_rate_limited():
    for path, method in [
        ("/bid-drafts/{draft_id}/files", "POST"),
        ("/bid-drafts/{draft_id}/files/{file_id}", "PATCH"),
        ("/bid-drafts/{draft_id}/files/{file_id}", "DELETE"),
        ("/bid-drafts/{draft_id}/transfer", "POST"),
    ]:
        route = _route(path, method)
        assert any(
            d.call is require_writer for d in route.dependant.dependencies
        ), f"{method} {path} missing require_writer"
    # The upload carries the SAME limiter as the project upload (files.py).
    upload = _route("/bid-drafts/{draft_id}/files", "POST")
    assert any(d.call is upload_rate_limit for d in upload.dependant.dependencies)


# ── Upload ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "category", ["drawing", "electrical_drawing", "specification", "addendum"]
)
async def test_upload_happy_path_per_category(env, category):
    draft = _mk_draft()
    out = await _upload(draft["id"], category=category, filename="F.pdf", content=b"abc")
    assert set(out) == set(bd._FILE_OUT_FIELDS)  # exactly the contract, no storage_path
    assert out["category"] == category and out["filename"] == "F.pdf"
    assert out["size_bytes"] == 3
    row = env.db.tables["bid_draft_files"][0]
    assert row["draft_id"] == str(draft["id"])
    assert row["storage_path"].startswith(f"drafts/{draft['id']}/{category}/")
    assert row["content_type"] == "application/pdf"
    assert env.store.uploads == [(row["storage_path"], "application/pdf")]


async def test_upload_unknown_draft_404s_before_any_storage_write(env):
    with pytest.raises(HTTPException) as exc:
        await _upload("missing")
    assert exc.value.status_code == 404
    assert env.store.uploads == []


@pytest.mark.parametrize("category", ["revision", "additional", "other", "boq", ""])
async def test_upload_rejects_non_intake_categories(env, category):
    draft = _mk_draft()
    with pytest.raises(HTTPException) as exc:
        await _upload(draft["id"], category=category)
    assert exc.value.status_code == 400
    assert env.store.uploads == []


async def test_addendum_metadata_is_optional_at_draft_stage(env):
    draft = _mk_draft()
    out = await _upload(draft["id"], category="addendum", filename="add.pdf")
    assert out["addendum_number"] is None
    assert out["addendum_issued_on"] is None
    assert out["doc_type"] is None


async def test_addendum_metadata_is_stored_when_present(env):
    draft = _mk_draft()
    out = await _upload(
        draft["id"],
        category="addendum",
        addendum_number="  3A  ",
        addendum_issued_on=TODAY,
        doc_type="drawing",
    )
    assert out["addendum_number"] == "3A"  # stripped
    assert out["addendum_issued_on"] == TODAY
    assert out["doc_type"] == "drawing"


@pytest.mark.parametrize(
    "kw",
    [
        {"category": "addendum", "addendum_issued_on": "not-a-date"},
        {"category": "addendum", "addendum_issued_on": FUTURE},
        {"category": "addendum", "doc_type": "plans"},
        {"category": "addendum", "addendum_number": "x" * 41},
        {"category": "drawing", "addendum_number": "1"},
        {"category": "specification", "addendum_issued_on": TODAY},
        {"category": "drawing", "doc_type": "drawing"},
    ],
)
async def test_bad_or_misplaced_metadata_is_400_before_storage(env, kw):
    draft = _mk_draft()
    with pytest.raises(HTTPException) as exc:
        await _upload(draft["id"], **kw)
    assert exc.value.status_code == 400
    assert env.store.uploads == []


async def test_tomorrow_is_accepted_as_clock_skew(env):
    draft = _mk_draft()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    out = await _upload(
        draft["id"], category="addendum", addendum_number="1", addendum_issued_on=tomorrow
    )
    assert out["addendum_issued_on"] == tomorrow


async def test_oversize_upload_is_413(env):
    from app.core.config import get_settings

    draft = _mk_draft()
    upload = _FakeUpload(b"tiny", "big.pdf")
    upload.size = get_settings().upload_max_bytes + 1
    with pytest.raises(HTTPException) as exc:
        await bd.upload_draft_file(
            draft_id=draft["id"],
            category="drawing",
            doc_type=None,
            addendum_number=None,
            addendum_issued_on=None,
            file=upload,
            user=_user(),
        )
    assert exc.value.status_code == 413
    assert env.store.uploads == []


async def test_insert_failure_reclaims_the_uploaded_object(env, monkeypatch):
    draft = _mk_draft()

    def table(name):
        q = FakeDB.table(env.db, name)
        if name == "bid_draft_files":
            def _boom():
                raise RuntimeError("insert failed")

            q.execute = _boom
        return q

    monkeypatch.setattr(env.db, "table", table)
    with pytest.raises(RuntimeError):
        await _upload(draft["id"])
    assert len(env.store.uploads) == 1
    assert env.store.deleted == [env.store.uploads[0][0]]


# ── PATCH metadata ───────────────────────────────────────────────────────────


async def test_patch_sets_and_clears_metadata(env):
    draft = _mk_draft()
    row = await _upload(draft["id"], category="addendum", filename="add.pdf")
    out = bd.update_draft_file(
        draft["id"],
        row["id"],
        BidDraftFileMetaIn.model_validate(
            {"addendum_number": "2", "addendum_issued_on": TODAY, "doc_type": "specification"}
        ),
        user=_user(),
    )
    assert (out["addendum_number"], out["addendum_issued_on"], out["doc_type"]) == (
        "2",
        TODAY,
        "specification",
    )
    # Explicit nulls clear - allowed at draft stage.
    out = bd.update_draft_file(
        draft["id"],
        row["id"],
        BidDraftFileMetaIn.model_validate(
            {"addendum_number": None, "addendum_issued_on": None, "doc_type": None}
        ),
        user=_user(),
    )
    assert out["addendum_number"] is None
    assert out["addendum_issued_on"] is None
    assert out["doc_type"] is None


async def test_patch_leaves_absent_fields_untouched(env):
    draft = _mk_draft()
    row = await _upload(
        draft["id"], category="addendum", addendum_number="7", addendum_issued_on=TODAY
    )
    out = bd.update_draft_file(
        draft["id"],
        row["id"],
        BidDraftFileMetaIn.model_validate({"doc_type": "drawing"}),
        user=_user(),
    )
    assert out["addendum_number"] == "7"
    assert out["addendum_issued_on"] == TODAY
    assert out["doc_type"] == "drawing"


async def test_patch_is_addendum_only(env):
    draft = _mk_draft()
    row = await _upload(draft["id"], category="drawing")
    with pytest.raises(HTTPException) as exc:
        bd.update_draft_file(
            draft["id"],
            row["id"],
            BidDraftFileMetaIn.model_validate({"addendum_number": "1"}),
            user=_user(),
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"addendum_issued_on": "nope"},
        {"addendum_issued_on": FUTURE},
        {"doc_type": "plans"},
        {"addendum_number": "x" * 41},
    ],
)
async def test_patch_validates_like_the_upload(env, body):
    draft = _mk_draft()
    row = await _upload(draft["id"], category="addendum")
    with pytest.raises(HTTPException) as exc:
        bd.update_draft_file(
            draft["id"], row["id"], BidDraftFileMetaIn.model_validate(body), user=_user()
        )
    assert exc.value.status_code == 400


async def test_patch_404s_on_unknown_or_foreign_file(env):
    draft_a = _mk_draft("A")
    draft_b = _mk_draft("B")
    row = await _upload(draft_a["id"], category="addendum")
    body = BidDraftFileMetaIn.model_validate({"addendum_number": "1"})
    for draft_id, file_id in [(draft_a["id"], "missing"), (draft_b["id"], row["id"])]:
        with pytest.raises(HTTPException) as exc:
            bd.update_draft_file(draft_id, file_id, body, user=_user())
        assert exc.value.status_code == 404


# ── DELETE file ──────────────────────────────────────────────────────────────


async def test_delete_file_removes_row_then_object(env):
    draft = _mk_draft()
    row = await _upload(draft["id"])
    path = env.db.tables["bid_draft_files"][0]["storage_path"]
    assert bd.delete_draft_file(draft["id"], row["id"], user=_user()) is None  # 204
    assert env.db.tables["bid_draft_files"] == []
    assert env.store.deleted == [path]
    # Gone means 404 on a repeat.
    with pytest.raises(HTTPException) as exc:
        bd.delete_draft_file(draft["id"], row["id"], user=_user())
    assert exc.value.status_code == 404


async def test_delete_file_storage_failure_is_swallowed(env):
    draft = _mk_draft()
    row = await _upload(draft["id"])
    env.store.fail_deletes.add(env.db.tables["bid_draft_files"][0]["storage_path"])
    assert bd.delete_draft_file(draft["id"], row["id"], user=_user()) is None
    assert env.db.tables["bid_draft_files"] == []  # the row delete stands


# ── GET includes files / draft delete sweeps ─────────────────────────────────


async def test_get_draft_lists_files_in_upload_order(env):
    draft = _mk_draft()
    a = await _upload(draft["id"], filename="first.pdf")
    b = await _upload(draft["id"], category="specification", filename="second.pdf")
    got = bd.get_draft(draft["id"], user=_user())
    assert [f["id"] for f in got["files"]] == [a["id"], b["id"]]
    assert all(set(f) == set(bd._FILE_OUT_FIELDS) for f in got["files"])


async def test_delete_draft_sweeps_the_storage_prefix(env):
    draft = _mk_draft()
    await _upload(draft["id"])
    bd.delete_draft(draft["id"], user=_user())
    assert env.db.tables["bid_drafts"] == []
    assert env.store.swept == [str(draft["id"])]


# ── Transfer ─────────────────────────────────────────────────────────────────


async def test_transfer_moves_inserts_and_retires(env):
    draft = _mk_draft()
    await _upload(draft["id"], category="drawing", filename="E-101.pdf", content=b"abc")
    await _upload(
        draft["id"],
        category="addendum",
        filename="add2.pdf",
        content=b"defg",
        addendum_number="2",
        addendum_issued_on=TODAY,
        doc_type="drawing",
    )
    _seed_project(env.db)
    out = _transfer(draft["id"], uid="admin-9")
    assert out == {"moved": 2}

    # Every move is a pure prefix swap: the key suffix survives, so the landed
    # key is exactly what a fresh upload of that file would have produced.
    assert len(env.store.moves) == 2
    for src, dest in env.store.moves:
        assert src.startswith(f"drafts/{draft['id']}/")
        assert dest == f"{PROJECT_ID}/" + src[len(f"drafts/{draft['id']}/") :]

    pf = env.db.tables["project_files"]
    assert len(pf) == 2
    drawing = next(r for r in pf if r["category"] == "drawing")
    drawing_dest = next(d for s, d in env.store.moves if "E-101.pdf" in s)
    expected = {
        "project_id": PROJECT_ID,
        "category": "drawing",
        "storage_path": drawing_dest,
        "filename": "E-101.pdf",
        "material_category_id": None,
        "uploaded_by": "admin-9",
        "mime_type": "application/pdf",
        "size_bytes": 3,
        "preview_status": "none",
        "note": None,
        "doc_type": None,
        "addendum_number": None,
        "addendum_issued_on": None,
        "estimator_deliverable": False,
    }
    assert {k: drawing[k] for k in expected} == expected
    # Column parity with a fresh files.py intake upload: no extra, no missing.
    assert set(drawing) == set(expected) | {"id", "created_at", "updated_at"}

    addendum = next(r for r in pf if r["category"] == "addendum")
    assert addendum["addendum_number"] == "2"
    assert addendum["addendum_issued_on"] == TODAY
    assert addendum["doc_type"] == "drawing"

    assert env.db.tables["bid_draft_files"] == []
    assert env.db.tables["bid_drafts"] == []
    assert env.store.swept == [str(draft["id"])]
    # The intake-upload side effect mirror: one audit entry per landed file.
    assert [a[1] for a in env.audits] == ["file.upload", "file.upload"]
    assert env.audits[0][0] == "admin-9"


async def test_transfer_precheck_incomplete_addendum_moves_nothing(env):
    draft = _mk_draft()
    await _upload(draft["id"], category="drawing", filename="ok.pdf")
    await _upload(draft["id"], category="addendum", filename="no-number.pdf",
                  addendum_issued_on=TODAY)
    await _upload(draft["id"], category="addendum", filename="no-date.pdf",
                  addendum_number="4")
    _seed_project(env.db)
    with pytest.raises(HTTPException) as exc:
        _transfer(draft["id"])
    assert exc.value.status_code == 400
    assert "no-number.pdf" in exc.value.detail and "no-date.pdf" in exc.value.detail
    assert "ok.pdf" not in exc.value.detail
    assert env.store.moves == []
    assert len(env.db.tables["bid_draft_files"]) == 3
    assert env.db.tables["bid_drafts"]  # the draft survives
    assert "project_files" not in env.db.tables or env.db.tables["project_files"] == []


async def test_transfer_404s_and_abandoned_409(env):
    with pytest.raises(HTTPException) as exc:
        _transfer("missing")
    assert exc.value.status_code == 404

    draft = _mk_draft()
    with pytest.raises(HTTPException) as exc:  # no such project
        _transfer(draft["id"])
    assert exc.value.status_code == 404

    _seed_project(env.db, abandoned_at="2026-08-01T00:00:00+00:00")
    with pytest.raises(HTTPException) as exc:
        _transfer(draft["id"])
    assert exc.value.status_code == 409
    assert env.store.moves == []


async def test_transfer_retry_processes_only_the_remainder(env):
    draft = _mk_draft()
    await _upload(draft["id"], category="drawing", filename="one.pdf")
    await _upload(draft["id"], category="specification", filename="two.pdf")
    _seed_project(env.db)
    env.store.fail_moves.add(env.db.tables["bid_draft_files"][1]["storage_path"])

    with pytest.raises(HTTPException) as exc:
        _transfer(draft["id"])
    assert exc.value.status_code == 502
    assert "two.pdf" in exc.value.detail
    # The first file is fully landed and retired; the second remains queued.
    assert len(env.db.tables["project_files"]) == 1
    assert [f["filename"] for f in env.db.tables["bid_draft_files"]] == ["two.pdf"]
    assert env.db.tables["bid_drafts"]  # the draft row survives the failure
    assert env.store.swept == []

    env.store.fail_moves.clear()
    out = _transfer(draft["id"])
    assert out == {"moved": 1}  # only the remainder
    assert len(env.store.moves) == 2  # the first file was never moved twice
    assert len(env.db.tables["project_files"]) == 2
    assert env.db.tables["bid_draft_files"] == []
    assert env.db.tables["bid_drafts"] == []
    assert env.store.swept == [str(draft["id"])]


async def test_transfer_recovers_a_file_the_dead_attempt_already_moved(env):
    draft = _mk_draft()
    await _upload(draft["id"], category="drawing", filename="moved.pdf")
    _seed_project(env.db)
    # Simulate a previous attempt that died between the move and the insert:
    # the object already sits at the destination, the source is gone.
    src = env.db.tables["bid_draft_files"][0]["storage_path"]
    dest = f"{PROJECT_ID}/" + src[len(f"drafts/{draft['id']}/") :]
    env.store.objects.discard(src)
    env.store.objects.add(dest)

    out = _transfer(draft["id"])
    assert out == {"moved": 1}
    assert env.store.moves == []  # recognized, not moved twice
    rows = env.db.tables["project_files"]
    assert len(rows) == 1 and rows[0]["storage_path"] == dest
    assert env.db.tables["bid_draft_files"] == []


async def test_transfer_recovery_does_not_duplicate_an_inserted_row(env):
    draft = _mk_draft()
    await _upload(draft["id"], category="drawing", filename="landed.pdf")
    _seed_project(env.db)
    src = env.db.tables["bid_draft_files"][0]["storage_path"]
    dest = f"{PROJECT_ID}/" + src[len(f"drafts/{draft['id']}/") :]
    # The dead attempt got further: object moved AND row inserted, but the
    # bid_draft_files row survived. The retry must not insert a second row.
    env.store.objects.discard(src)
    env.store.objects.add(dest)
    env.db.tables["project_files"] = [
        {"id": "pf-1", "project_id": PROJECT_ID, "storage_path": dest, "category": "drawing"}
    ]

    out = _transfer(draft["id"])
    assert out == {"moved": 1}
    assert len(env.db.tables["project_files"]) == 1
    assert env.audits == []  # nothing new landed, nothing to audit
    assert env.db.tables["bid_draft_files"] == []


async def test_transfer_mirrors_the_preview_side_effect(env, monkeypatch):
    monkeypatch.setattr(
        bd.office_preview, "is_convertible", lambda fn, cat: (fn or "").endswith(".xlsx")
    )
    generated = []
    monkeypatch.setattr(bd.office_preview, "generate_preview", lambda fid: generated.append(fid))
    draft = _mk_draft()
    await _upload(draft["id"], category="specification", filename="schedule.xlsx")
    _seed_project(env.db)
    background = BackgroundTasks()
    _transfer(draft["id"], background=background)
    row = env.db.tables["project_files"][0]
    assert row["preview_status"] == "pending"
    for task in background.tasks:
        task.func(*task.args, **task.kwargs)
    assert generated == [row["id"]]
