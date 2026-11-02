"""Assistant staging (app/services/assistant_staging.py) and the staged-file
repo functions (docs/ASSISTANT_PHASE2_PLAN.md 3.1, WI-2).

Invariants pinned here:

  name      control, format and bidi characters dropped, separators made
            "-", whitespace collapsed, a ".pdf" suffix kept or added, 255
            characters at most, an empty name refused;
  check     the splitter's own per-file check runs at staging: not a PDF is
            415 not_pdf, a PDF that does not open is 415 unreadable_pdf, 601
            pages is 413 too_many_pages with the count;
  caps      2 GB of live staging per person (the caps trigger) is 409
            staging_full and the stored object is deleted again;
  dedupe    the same unlinked bytes come back reused (no second object,
            expires_at refreshed); a row that expires within 4 hours, a
            linked row and a claimed row are never reused;
  purge     objects of expired and consumed rows go, purged_at is set, rows
            stay; a legal-hold owner's objects are never touched, by the
            system sweep or by the owner's own sweep;
  remove    the row is retired (freeing the cap) before its object goes;
  claim     the split-drop claim takes only unclaimed rows, once.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant import repo
from app.core import supabase_client
from app.core.deps import CurrentUser
from app.core.roles import Role
from app.services import assistant_staging as staging
from app.services import notifications

from tests.assistant_fakes import (
    CONV, MSG_USER, NOW, OTHER_USER, PROJECT, STAGED, STAGED2, TASK, USER, FakeDB, FakeStorage, UniqueViolation,
    approval, message, staged_file,
)

EM_DASH = chr(0x2014)


def _pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch):
    db = FakeDB(assistant_files=[], assistant_profiles=[], assistant_messages=[], audit_log=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    store = FakeStorage().install(monkeypatch)
    audits: list[tuple] = []
    monkeypatch.setattr(notifications, "audit", lambda *a, **k: audits.append(a))
    user = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    return SimpleNamespace(db=db, store=store, audits=audits, user=user)


# ── names ────────────────────────────────────────────────────────────────────


def test_normalize_filename_drops_controls_bidi_and_separators():
    raw = "Spec‮\n Book/Part\\2\tfinal⁦.PDF"
    assert staging.normalize_filename(raw) == "Spec Book-Part-2final.PDF"
    assert staging.normalize_filename("drawings") == "drawings.pdf"
    assert staging.normalize_filename("  A-101.pdf  ") == "A-101.pdf"
    assert staging.normalize_filename("a\x00b.pdf") == "ab.pdf"
    assert staging.normalize_filename("x" * 400 + ".pdf") == "x" * 251 + ".pdf"
    assert len(staging.normalize_filename("y" * 400)) == 255
    for empty in ("", None, ".pdf", "‮\n", "///", "   .pdf"):
        assert staging.normalize_filename(empty) == "", empty


# ── stage ────────────────────────────────────────────────────────────────────


def test_stage_records_pages_sha_and_the_object(env):
    content = _pdf(3)
    row, reused = staging.stage(env.user, "A-101.pdf", content)
    assert reused is False
    assert row["filename"] == "A-101.pdf" and row["page_count"] == 3 and row["mime"] == "application/pdf"
    import hashlib

    assert row["sha256"] == hashlib.sha256(content).hexdigest() and row["size_bytes"] == len(content)
    assert row["storage_path"].startswith(f"assistant-files/{USER}/") and row["storage_path"].endswith("-A-101.pdf")
    assert env.store.objects[row["storage_path"]] == content
    assert row["message_id"] is None and row["conversation_id"] is None
    # The audit row carries ids and sizes, never the (tainted) name.
    assert len(env.audits) == 1
    actor, action, entity, entity_id, payload = env.audits[0]
    assert (actor, action, entity, entity_id) == (USER, "assistant.file_staged", "assistant_files", row["id"])
    assert payload == {"file_id": row["id"], "size_bytes": len(content), "pages": 3}


def test_stage_stores_a_hostile_name_normalized(env):
    row, _ = staging.stage(env.user, "evil‮FDP.exe\nnext/../x", _pdf())
    assert row["filename"] == "evilFDP.exenext-..-x.pdf"
    assert "‮" not in row["storage_path"] and "\n" not in row["storage_path"]
    assert row["storage_path"].count("/") == 2


def test_stage_refuses_what_the_splitter_refuses(env):
    with pytest.raises(staging.StagingRefused) as exc:
        staging.stage(env.user, "notes.docx", b"PK\x03\x04 not a pdf")
    assert (exc.value.code, exc.value.status) == ("not_pdf", 415)
    with pytest.raises(staging.StagingRefused) as exc:
        staging.stage(env.user, "fake.pdf", b"plain text")
    assert (exc.value.code, exc.value.status) == ("not_pdf", 415)
    with pytest.raises(staging.StagingRefused) as exc:
        staging.stage(env.user, "broken.pdf", b"%PDF-1.4\nthis is not a pdf body")
    assert (exc.value.code, exc.value.status) == ("unreadable_pdf", 415)
    with pytest.raises(staging.StagingRefused) as exc:
        staging.stage(env.user, "huge.pdf", _pdf(601))
    assert (exc.value.code, exc.value.status, exc.value.detail) == ("too_many_pages", 413, {"pages": 601})
    with pytest.raises(staging.StagingRefused) as exc:
        staging.stage(env.user, "‮", _pdf())
    assert (exc.value.code, exc.value.status) == ("bad_filename", 400)
    assert env.db.tables["assistant_files"] == [] and env.store.objects == {}
    for refused in ("not_pdf", "unreadable_pdf", "too_many_pages", "bad_filename", "staging_full"):
        assert EM_DASH not in refused


def test_staging_full_deletes_the_object_again(env):
    sizes = [429496729] * 4 + [2147483548 - 4 * 429496729]
    env.db.tables["assistant_files"] = [
        staged_file(id=f"f{i}", size_bytes=n, sha256=f"{i}" * 64, storage_path=f"p{i}") for i, n in enumerate(sizes)
    ]
    with pytest.raises(staging.StagingRefused) as exc:
        staging.stage(env.user, "one-more.pdf", _pdf())
    assert (exc.value.code, exc.value.status) == ("staging_full", 409)
    assert env.store.objects == {} and len(env.store.deleted) == 1
    assert len(env.db.tables["assistant_files"]) == 5
    # Another user's staging does not count against this person.
    for row in env.db.tables["assistant_files"]:
        row["user_id"] = OTHER_USER
    row, _ = staging.stage(env.user, "one-more.pdf", _pdf())
    assert row["user_id"] == USER


def test_same_unlinked_bytes_are_reused_and_refreshed(env):
    content = _pdf(2)
    first, _ = staging.stage(env.user, "A.pdf", content)
    first_row = env.db.tables["assistant_files"][0]
    first_row["expires_at"] = (NOW + timedelta(hours=5)).isoformat()
    again, reused = staging.stage(env.user, "A copy.pdf", content)
    assert reused is True and again["id"] == first["id"] and again["filename"] == "A.pdf"
    assert len(env.db.tables["assistant_files"]) == 1 and len(env.store.objects) == 1
    assert again["expires_at"] > (NOW + timedelta(days=6)).isoformat()
    assert first_row["expires_at"] == again["expires_at"]


@pytest.mark.parametrize("over", [
    {"expires_at": (NOW + timedelta(hours=3)).isoformat()},   # a 4-hour card could outlive it
    {"message_id": MSG_USER, "conversation_id": CONV},         # already sent
    {"consumed_task_id": TASK},                                # claimed by a split
    {"consumed_at": NOW.isoformat(), "consumed_task_id": TASK},
    {"purged_at": NOW.isoformat()},
    {"user_id": OTHER_USER},
])
def test_rows_that_must_not_be_reused(env, over):
    content = _pdf(1)
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    env.db.tables["assistant_files"] = [staged_file(sha256=sha, **over)]
    row, reused = staging.stage(env.user, "B.pdf", content)
    assert reused is False and row["id"] != STAGED
    assert len(env.db.tables["assistant_files"]) == 2


# ── purge ────────────────────────────────────────────────────────────────────


def _purge_fixture(env):
    past = (NOW - timedelta(hours=1)).isoformat()
    env.db.tables["assistant_files"] = [
        staged_file(id="expired", storage_path="o/expired", expires_at=past),
        staged_file(id="consumed", storage_path="o/consumed", consumed_at=past, consumed_task_id=TASK),
        staged_file(id="live", storage_path="o/live"),
        staged_file(id="done", storage_path="o/done", expires_at=past, purged_at=past),
        staged_file(id="held", user_id=OTHER_USER, storage_path="o/held", expires_at=past),
    ]
    for path in ("o/expired", "o/consumed", "o/live", "o/held"):
        env.store.objects[path] = b"%PDF-"


def test_purge_deletes_objects_of_expired_and_consumed_rows_and_keeps_rows(env):
    _purge_fixture(env)
    assert staging.purge_expired(user_id=None, limit=20) == 3
    assert set(env.store.deleted) == {"o/expired", "o/consumed", "o/held"}
    assert set(env.store.objects) == {"o/live"}
    rows = {r["id"]: r for r in env.db.tables["assistant_files"]}
    assert len(rows) == 5
    assert all(rows[i]["purged_at"] for i in ("expired", "consumed", "held"))
    assert rows["live"]["purged_at"] is None


@pytest.mark.parametrize("user_id", [None, USER])
def test_purge_never_takes_a_freshly_claimed_object(env, user_id):
    """split-drop claims at step 7 and copies at step 10: a sweep in that
    window must not delete the object (even when the row also expired)."""
    past = (NOW - timedelta(hours=1)).isoformat()
    env.db.tables["assistant_files"] = [
        staged_file(id="fresh", storage_path="o/fresh", consumed_at=NOW.isoformat(), consumed_task_id=TASK),
        staged_file(id="fresh-expired", storage_path="o/fresh-expired", consumed_at=NOW.isoformat(),
                    consumed_task_id=TASK, expires_at=past),
        staged_file(id="old", storage_path="o/old", consumed_at=past, consumed_task_id=TASK),
    ]
    ids = {r["id"] for r in repo.staged_objects_to_purge(user_id, limit=20)}
    assert ids == {"old"}


def test_purge_skips_legal_hold_owners_in_both_modes(env):
    _purge_fixture(env)
    env.db.tables["assistant_profiles"] = [{"user_id": OTHER_USER, "legal_hold": True}]
    assert staging.purge_expired(user_id=None, limit=20) == 2
    assert "o/held" in env.store.objects
    assert env.db.tables["assistant_files"][4]["purged_at"] is None
    # The held owner's own sweep (GET /assistant/files) deletes nothing.
    env.store.deleted.clear()
    assert staging.purge_expired(user_id=OTHER_USER, limit=20) == 0
    assert env.store.deleted == [] and "o/held" in env.store.objects
    # And a per-user sweep never reaches another person's rows.
    env.db.tables["assistant_profiles"] = []
    _purge_fixture(env)
    staging.purge_expired(user_id=USER, limit=20)
    assert "o/held" in env.store.objects


def test_purge_is_bounded_and_never_raises(env, monkeypatch):
    _purge_fixture(env)
    assert staging.purge_expired(user_id=None, limit=1) == 1
    env.store.fail_delete = True
    assert staging.purge_expired(user_id=None, limit=20) == 0
    monkeypatch.setattr(repo, "staged_objects_to_purge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert staging.purge_expired(user_id=None, limit=20) == 0


def test_stage_runs_a_bounded_system_sweep(env, monkeypatch):
    calls = []
    monkeypatch.setattr(staging, "purge_expired", lambda user_id, limit: calls.append((user_id, limit)) or 0)
    staging.stage(env.user, "A.pdf", _pdf())
    assert calls == [(None, 20)]


# ── remove ───────────────────────────────────────────────────────────────────


def test_remove_retires_the_row_frees_the_cap_and_deletes_the_object(env):
    env.db.tables["assistant_files"] = [staged_file(size_bytes=2147483600, storage_path="o/big")]
    env.store.objects["o/big"] = b"%PDF-"
    with pytest.raises(staging.StagingRefused):
        staging.stage(env.user, "A.pdf", _pdf())
    staging.remove(env.user, STAGED)
    row = env.db.tables["assistant_files"][0]
    assert row["purged_at"] and row["expires_at"] < (NOW + timedelta(minutes=5)).isoformat()
    assert "o/big" not in env.store.objects
    # The trigger compares expires_at with the database clock, which has moved on.
    env.db.now = datetime.now(timezone.utc) + timedelta(seconds=1)
    staged_now, _ = staging.stage(env.user, "A.pdf", _pdf())
    assert staged_now["id"] != STAGED
    # Idempotent: a retired row answers again without error.
    staging.remove(env.user, STAGED)


def test_remove_refuses_claimed_missing_and_foreign_rows(env):
    env.db.tables["assistant_files"] = [
        staged_file(consumed_task_id=TASK),
        staged_file(id=STAGED2, user_id=OTHER_USER, storage_path="o/other"),
    ]
    with pytest.raises(staging.StagingRefused) as exc:
        staging.remove(env.user, STAGED)
    assert (exc.value.code, exc.value.status) == ("file_consumed", 409)
    with pytest.raises(staging.StagingRefused) as exc:
        staging.remove(env.user, STAGED2)
    assert (exc.value.code, exc.value.status) == ("file_not_found", 404)


def _split_card(status: str, file_ids: list[str], **over) -> dict:
    return approval(id=over.pop("id", "card-1"), action_kind="split_files_into_project", status=status,
                    payload={"v": 1, "args": {"file_ids": file_ids},
                             "request": {"json": {"file_ids": file_ids}}}, **over)


@pytest.mark.parametrize("status", ["pending", "approved", "executing"])
def test_remove_refuses_a_sent_file_an_open_split_card_still_needs(env, status):
    """A stale composer chip of a file another surface already sent must not
    retire it while that message's split card (pending, or approved and not
    yet executed) still needs the object."""
    env.db.tables["assistant_files"] = [staged_file(storage_path="o/sent", message_id="m-1", conversation_id="c-1")]
    env.db.tables["assistant_approvals"] = [_split_card(status, [STAGED])]
    env.store.objects["o/sent"] = b"%PDF-"
    with pytest.raises(staging.StagingRefused) as exc:
        staging.remove(env.user, STAGED)
    assert (exc.value.code, exc.value.status) == ("file_consumed", 409)
    row = env.db.tables["assistant_files"][0]
    assert "o/sent" in env.store.objects and row["expires_at"] > NOW.isoformat() and row["purged_at"] is None
    # The repo fence holds on its own too (a link racing the read).
    assert repo.retire_staged_file(USER, STAGED) is False


@pytest.mark.parametrize("status", ["rejected", "expired", "failed", None])
def test_remove_retires_a_sent_file_no_open_card_needs(env, status):
    """Plan 3.1.3: only consumed or claimed rows are refused. A file sent
    with a message but never split (the split was refused, or its card was
    rejected or expired) is removed like a tray file, so it stops holding
    the staging cap (live test 2026-10-22: these rows answered 409 and
    stayed for 7 days)."""
    env.db.tables["assistant_files"] = [staged_file(storage_path="o/sent", message_id="m-1", conversation_id="c-1")]
    env.db.tables["assistant_approvals"] = [_split_card(status, [STAGED])] if status else []
    env.store.objects["o/sent"] = b"%PDF-"
    staging.remove(env.user, STAGED)
    row = env.db.tables["assistant_files"][0]
    assert "o/sent" not in env.store.objects and row["purged_at"]
    assert row["expires_at"] < (NOW + timedelta(minutes=5)).isoformat()
    # Idempotent.
    staging.remove(env.user, STAGED)


def test_remove_of_a_sent_file_keeps_the_other_cards_files(env):
    """An open card naming only another file does not block this one."""
    env.db.tables["assistant_files"] = [
        staged_file(storage_path="o/a", message_id="m-1", conversation_id="c-1"),
        staged_file(id=STAGED2, storage_path="o/b", message_id="m-1", conversation_id="c-1"),
    ]
    env.db.tables["assistant_approvals"] = [_split_card("pending", [STAGED2])]
    env.store.objects.update({"o/a": b"%PDF-", "o/b": b"%PDF-"})
    staging.remove(env.user, STAGED)
    assert "o/a" not in env.store.objects and "o/b" in env.store.objects
    with pytest.raises(staging.StagingRefused):
        staging.remove(env.user, STAGED2)


def test_remove_under_legal_hold_keeps_the_object(env):
    env.db.tables["assistant_files"] = [staged_file(storage_path="o/x")]
    env.db.tables["assistant_profiles"] = [{"user_id": USER, "legal_hold": True}]
    env.store.objects["o/x"] = b"%PDF-"
    staging.remove(env.user, STAGED)
    row = env.db.tables["assistant_files"][0]
    assert "o/x" in env.store.objects and row["purged_at"] is None
    assert row["expires_at"] < (NOW + timedelta(minutes=5)).isoformat()


# ── repo: link, claim and the explicit message id ────────────────────────────


def test_claim_fence_takes_only_unclaimed_rows_once(env):
    env.db.tables["assistant_files"] = [
        staged_file(message_id=MSG_USER, conversation_id=CONV),
        staged_file(id=STAGED2, storage_path="p2", consumed_task_id="someone-else"),
    ]
    assert repo.claim_staged_files(USER, [STAGED, STAGED2], task_id=TASK, project_id=PROJECT) == 1
    first = env.db.tables["assistant_files"][0]
    assert first["consumed_task_id"] == TASK and first["consumed_project_id"] == PROJECT and first["consumed_at"]
    # A second card over the same ids gets nothing.
    assert repo.claim_staged_files(USER, [STAGED], task_id="t2", project_id=PROJECT) == 0
    assert repo.claim_staged_files(OTHER_USER, [STAGED], task_id="t3", project_id=PROJECT) == 0
    # Unclaim is fenced on the task that claimed.
    assert repo.unclaim_staged_files(USER, "t2") == 0
    assert repo.unclaim_staged_files(USER, TASK) == 1
    assert first["consumed_task_id"] is None and first["consumed_at"] is None
    assert env.db.tables["assistant_files"][1]["consumed_task_id"] == "someone-else"


def test_link_is_fenced_and_files_for_messages_returns_rows(env):
    env.db.tables["assistant_files"] = [staged_file(), staged_file(id=STAGED2, storage_path="p2",
                                                                   message_id="other-msg")]
    assert [r["id"] for r in repo.staged_files_for_link(USER, [STAGED, STAGED2])] == [STAGED]
    assert repo.link_files(USER, [STAGED, STAGED2], MSG_USER, CONV) == 1
    rows = repo.files_for_messages(USER, [MSG_USER])
    assert [r["id"] for r in rows] == [STAGED] and rows[0]["conversation_id"] == CONV
    assert repo.files_for_messages(OTHER_USER, [MSG_USER]) == []
    assert repo.list_staged_files(USER) == []


def test_insert_message_keeps_an_explicit_id_and_refuses_a_duplicate(env):
    env.db.tables["assistant_messages"] = []
    row = repo.insert_message(USER, {"id": MSG_USER, "conversation_id": CONV, "role": "assistant",
                                     "status": "done", "content": "x"})
    assert row["id"] == MSG_USER and env.db.tables["assistant_messages"][0]["user_id"] == USER
    with pytest.raises(UniqueViolation) as exc:
        repo.insert_message(USER, message(role="assistant", status="done"))
    assert "23505" in str(exc.value)
