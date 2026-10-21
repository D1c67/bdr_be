"""Per-user file access tracking (0161 file_access_events, app/services/file_access.py).

Pins the write points on the project files router (download intents, the
cached-open ping, the ZIP export), that the recorder never raises, the report
aggregation (counts, prefetch exclusion, grouping) and the internal-only gate
on both read endpoints. A small in-memory fake stands in for Supabase; route
handlers are called directly, as the other router tests do.
"""

import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import analytics as analytics_mod
from app.routers import files as files_mod
from app.services import file_access
from app.services import file_export

PROJECT = "11111111-1111-1111-1111-111111111111"
FILE_A = "22222222-2222-2222-2222-222222222222"
FILE_B = "33333333-3333-3333-3333-333333333333"
ALICE = "44444444-4444-4444-4444-444444444444"
BOB = "55555555-5555-5555-5555-555555555555"


def _user(role=Role.ESTIMATING_ADMIN, uid=ALICE):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True)


class _Q:
    """Just enough of the PostgREST builder: eq/neq/in_ filters are honored,
    the rest are chainable no-ops; insert/update write into the fake."""

    def __init__(self, sb, name):
        self._sb = sb
        self._name = name
        self._filters = []
        self._op = "select"
        self._payload = None
        self._single = False
        self._range = None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def neq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: r.get(c) != v)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._filters.append(lambda r, c=col: r.get(c) in vals)
        return self

    def gte(self, *a, **k):
        return self

    def lte(self, *a, **k):
        return self

    def or_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def single(self):
        self._single = True
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def execute(self):
        if self._sb.fail_on == self._name:
            raise RuntimeError("boom")
        table = self._sb.tables.setdefault(self._name, [])
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            table.extend(rows)
            self._sb.inserts.setdefault(self._name, []).append(rows)
            return SimpleNamespace(data=rows)
        rows = [r for r in table if all(f(r) for f in self._filters)]
        if self._op == "update":
            for r in rows:
                r.update(self._payload)
            return SimpleNamespace(data=rows)
        if self._range:
            rows = rows[self._range[0] : self._range[1] + 1]
        if self._single:
            return SimpleNamespace(data=rows[0] if rows else None)
        return SimpleNamespace(data=rows)


class _SB:
    def __init__(self, tables=None, fail_on=None):
        self.tables = tables or {}
        self.inserts: dict[str, list] = {}
        self.fail_on = fail_on

    def table(self, name):
        return _Q(self, name)


def _file(fid=FILE_A, filename="E1.pdf", category="electrical_drawing"):
    return {
        "id": fid,
        "project_id": PROJECT,
        "category": category,
        "filename": filename,
        "storage_path": f"p/{fid}",
        "size_bytes": 10,
        "uploaded_by": None,
        "is_source_set": False,
        "sent_to_estimators_at": None,
        "preview_status": "ready",
        "preview_path": f"p/{fid}.preview.pdf",
    }


@pytest.fixture
def sb(monkeypatch):
    fake = _SB({"project_files": [_file(), _file(FILE_B, "Specs.pdf", "specification")]})
    monkeypatch.setattr(files_mod, "get_supabase", lambda: fake)
    monkeypatch.setattr(file_access, "get_supabase", lambda: fake)
    monkeypatch.setattr(files_mod.storage, "signed_url", lambda *a, **k: "https://signed")
    audits: list[tuple] = []
    monkeypatch.setattr(files_mod, "audit", lambda *a, **k: audits.append(a))
    fake.audits = audits
    return fake


def _events(fake):
    return fake.tables.get("file_access_events", [])


# ── write points ─────────────────────────────────────────────────────────────


def test_prefetch_intent_records_prefetch_and_writes_no_audit(sb):
    out = files_mod.download_url(PROJECT, FILE_A, intent="prefetch", user=_user())
    assert out["url"] == "https://signed"
    (ev,) = _events(sb)
    assert ev["action"] == "prefetch"
    assert ev["source"] == "project_file"
    assert sb.audits == []


def test_default_download_records_download_and_audits_with_context(sb):
    files_mod.download_url(PROJECT, FILE_A, user=_user())
    (ev,) = _events(sb)
    assert ev["action"] == "download"
    assert ev["file_id"] == FILE_A and ev["project_id"] == PROJECT
    assert ev["filename"] == "E1.pdf" and ev["category"] == "electrical_drawing"
    assert ev["actor_id"] == ALICE and ev["actor_role"] == Role.ESTIMATING_ADMIN.value
    (a,) = sb.audits
    assert a[1] == "file.download"
    assert a[4] == {"project_id": PROJECT, "filename": "E1.pdf", "intent": "download"}


def test_preview_url_view_records_view_and_prefetch_skips_audit(sb):
    files_mod.preview_url(PROJECT, FILE_A, user=_user())
    files_mod.preview_url(PROJECT, FILE_A, intent="prefetch", user=_user())
    assert [e["action"] for e in _events(sb)] == ["view", "prefetch"]
    assert [a[1] for a in sb.audits] == ["file.preview"]


def test_opened_records_cached_view(sb):
    resp = files_mod.file_opened(PROJECT, FILE_A, user=_user())
    assert resp.status_code == 204
    (ev,) = _events(sb)
    assert ev["action"] == "view"
    assert ev["detail"] == {"cached": True}
    assert sb.audits == []


async def test_export_writes_one_row_per_ok_file_sharing_export_id(sb, monkeypatch):
    def _build(rows, flat=False):
        # Manifest in archive order, one entry per row; the second is missing.
        ordered = file_export.export_order(rows)
        manifest = [{"file": r["filename"], "status": "ok"} for r in ordered]
        return io.BytesIO(b"zip"), manifest, 3

    monkeypatch.setattr(files_mod.file_export, "build_export_spooled", _build)
    monkeypatch.setattr(
        files_mod, "get_settings", lambda: SimpleNamespace(export_max_total_bytes=10**9)
    )
    monkeypatch.setattr(files_mod.estimator_rounds, "exclude_unsent", lambda q: q)
    sb.tables["projects"] = [{"id": PROJECT, "number": "26.1.1", "name": "Acme"}]

    resp = await files_mod.export_files(PROJECT, body=None, user=_user(), flat=False)
    assert resp.status_code == 200
    evs = _events(sb)
    assert len(evs) == 2
    assert {e["file_id"] for e in evs} == {FILE_A, FILE_B}
    assert {e["action"] for e in evs} == {"export"}
    assert len({e["export_id"] for e in evs}) == 1 and evs[0]["export_id"]
    # One batched insert, not one per file.
    assert len(sb.inserts["file_access_events"]) == 1
    # Filenames/categories map back to their own rows.
    by_id = {e["file_id"]: e for e in evs}
    assert by_id[FILE_B]["filename"] == "Specs.pdf"
    assert by_id[FILE_B]["category"] == "specification"


async def test_export_skips_missing_manifest_entries(sb, monkeypatch):
    def _build(rows, flat=False):
        ordered = file_export.export_order(rows)
        manifest = [
            {"file": r["filename"], "status": "ok" if i == 0 else "missing"}
            for i, r in enumerate(ordered)
        ]
        return io.BytesIO(b"zip"), manifest, 3

    monkeypatch.setattr(files_mod.file_export, "build_export_spooled", _build)
    monkeypatch.setattr(
        files_mod, "get_settings", lambda: SimpleNamespace(export_max_total_bytes=10**9)
    )
    monkeypatch.setattr(files_mod.estimator_rounds, "exclude_unsent", lambda q: q)
    sb.tables["projects"] = [{"id": PROJECT, "number": "26.1.1", "name": "Acme"}]

    await files_mod.export_files(PROJECT, body=None, user=_user(), flat=False)
    (ev,) = _events(sb)
    first = file_export.export_order(sb.tables["project_files"])[0]
    assert ev["file_id"] == first["id"]


def test_recorder_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(file_access, "get_supabase", lambda: _SB(fail_on="file_access_events"))
    file_access.record(
        _user(), action="view", source="project_file", file_id=FILE_A, project_id=PROJECT
    )
    file_access.record_many(
        _user(),
        [{"action": "export", "source": "project_file", "file_id": FILE_A, "project_id": PROJECT}],
    )

    def _no_client():
        raise RuntimeError("no client")

    monkeypatch.setattr(file_access, "get_supabase", _no_client)
    file_access.record(None, action="view", source="project_file", file_id=None, project_id=None)
    # A bad row shape is swallowed too.
    file_access.record_many(_user(), [{"nope": 1}])


def test_missing_table_is_swallowed_quietly(monkeypatch, caplog):
    class _Missing(Exception):
        code = "PGRST205"

    class _MissingSB(_SB):
        def table(self, name):
            q = _Q(self, name)

            def _raise():
                raise _Missing("table not found")

            q.execute = _raise
            return q

    monkeypatch.setattr(file_access, "get_supabase", lambda: _MissingSB())
    file_access.record(
        _user(), action="view", source="project_file", file_id=FILE_A, project_id=PROJECT
    )
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    # The read side treats a missing table as empty.
    out = file_access.access_report(date_from=None, date_to=None)
    assert out == {"summary": [], "events": [], "events_total": 0, "truncated": False}


# ── read side ────────────────────────────────────────────────────────────────


def _ev(i, actor, action, fid=FILE_A, at="2026-10-0{}T10:00:00+00:00", **extra):
    return {
        "id": f"e{i}",
        "created_at": at.format(i) if "{}" in at else at,
        "actor_id": actor,
        "actor_role": "estimating_admin",
        "project_id": PROJECT,
        "source": "project_file",
        "file_id": fid,
        "filename": "E1.pdf" if fid == FILE_A else "Specs.pdf",
        "category": "electrical_drawing",
        "action": action,
        "export_id": None,
        "detail": None,
        **extra,
    }


@pytest.fixture
def report_sb(monkeypatch):
    # Newest first, as the real query orders them.
    events = [
        _ev(9, ALICE, "view"),
        _ev(8, ALICE, "prefetch"),
        _ev(7, ALICE, "download"),
        _ev(6, BOB, "view"),
        _ev(5, ALICE, "view"),
        _ev(4, ALICE, "export"),
        _ev(3, ALICE, "view", fid=FILE_B),
    ]
    fake = _SB(
        {
            "file_access_events": events,
            "profiles": [
                {"id": ALICE, "full_name": "Alice", "email": "a@g3.com", "role": "estimating_admin"},
                {"id": BOB, "full_name": "Bob", "email": "b@g3.com", "role": "executive"},
            ],
            "projects": [{"id": PROJECT, "number": "26.1.1", "name": "Acme"}],
            "project_files": [_file()],
        }
    )
    monkeypatch.setattr(file_access, "get_supabase", lambda: fake)
    monkeypatch.setattr(files_mod, "get_supabase", lambda: fake)
    return fake


def test_summary_counts_and_excludes_prefetch(report_sb):
    out = file_access.access_report(date_from=None, date_to=None)
    assert out["events_total"] == 6
    assert all(e["action"] != "prefetch" for e in out["events"])
    assert out["truncated"] is False

    groups = {(g["file_id"], g["actor_id"]): g for g in out["summary"]}
    alice_a = groups[(FILE_A, ALICE)]
    assert (alice_a["views"], alice_a["downloads"], alice_a["exports"]) == (2, 1, 1)
    assert alice_a["prefetches"] == 0
    assert alice_a["total"] == 4
    assert alice_a["first_at"].startswith("2026-10-04")
    assert alice_a["last_at"].startswith("2026-10-09")
    assert alice_a["actor_name"] == "Alice" and alice_a["actor_email"] == "a@g3.com"
    assert alice_a["project_number"] == "26.1.1" and alice_a["project_name"] == "Acme"
    assert groups[(FILE_A, BOB)]["views"] == 1
    assert groups[(FILE_B, ALICE)]["total"] == 1
    # Sorted last_at desc.
    lasts = [g["last_at"] for g in out["summary"]]
    assert lasts == sorted(lasts, reverse=True)

    ev = out["events"][0]
    assert ev["actor_name"] == "Alice" and ev["project_name"] == "Acme"
    assert set(ev) == {
        "id", "created_at", "actor_id", "actor_name", "actor_role", "project_id",
        "project_number", "project_name", "source", "file_id", "filename", "category",
        "action", "export_id", "detail",
    }


def test_include_prefetch_counts_prefetches_but_not_in_total(report_sb):
    out = file_access.access_report(date_from=None, date_to=None, include_prefetch=True)
    assert out["events_total"] == 7
    g = next(g for g in out["summary"] if g["file_id"] == FILE_A and g["actor_id"] == ALICE)
    assert g["prefetches"] == 1 and g["total"] == 4


def test_action_prefetch_filter_returns_only_prefetches(report_sb):
    out = file_access.access_report(date_from=None, date_to=None, action="prefetch")
    assert [e["action"] for e in out["events"]] == ["prefetch"]


def test_pagination_and_truncation(report_sb, monkeypatch):
    out = file_access.access_report(date_from=None, date_to=None, limit=2, offset=1)
    assert out["events_total"] == 6
    assert [e["id"] for e in out["events"]] == ["e7", "e6"]
    monkeypatch.setattr(file_access, "SCAN_CAP", 3)
    monkeypatch.setattr(file_access, "_PAGE", 2)
    rows, truncated = file_access._fetch_events(report_sb, cap=3)
    assert truncated is True and len(rows) == 3


def test_null_file_id_groups_by_filename(monkeypatch):
    rows = [
        {**_ev(2, ALICE, "download", fid=None), "source": "ai_estimating_deliverable",
         "filename": "boq-v2.xlsx"},
        {**_ev(1, ALICE, "download", fid=None), "source": "ai_estimating_deliverable",
         "filename": "brief-v1.docx"},
    ]
    assert len(file_access.summarize(rows)) == 2


def test_file_access_endpoint_per_user(report_sb):
    out = files_mod.file_access_log(PROJECT, FILE_A, user=_user())
    assert out["file_id"] == FILE_A and out["filename"] == "E1.pdf"
    users = {u["actor_id"]: u for u in out["users"]}
    assert (users[ALICE]["views"], users[ALICE]["downloads"], users[ALICE]["exports"]) == (2, 1, 1)
    assert users[ALICE]["total"] == 4 and "prefetches" not in users[ALICE]
    assert users[BOB]["actor_name"] == "Bob"
    assert all(e["action"] != "prefetch" for e in out["events"])
    assert len(out["events"]) == 5


# ── gates ────────────────────────────────────────────────────────────────────


async def test_estimator_gets_403_on_access_endpoints(report_sb):
    est = _user(role=Role.ESTIMATOR)
    with pytest.raises(HTTPException) as ei:
        analytics_mod.file_access_report(
            range_="month", date_from=None, date_to=None, project_id=None, file_id=None,
            actor_id=None, source=None, action=None, include_prefetch=False, limit=100,
            offset=0, user=est,
        )
    assert ei.value.status_code == 403

    # The per-file route is gated by its require_internal dependency.
    from app.core.deps import require_internal

    route = next(
        r for r in files_mod.router.routes
        if getattr(r, "path", "").endswith("/{file_id}/access")
    )
    deps = [d.call for d in route.dependant.dependencies]
    assert require_internal in deps
    with pytest.raises(HTTPException) as ei:
        await require_internal(est)
    assert ei.value.status_code == 403


def test_analytics_route_rejects_bad_ids(report_sb):
    with pytest.raises(HTTPException) as ei:
        analytics_mod.file_access_report(
            range_="month", date_from=None, date_to=None, project_id="nope", file_id=None,
            actor_id=None, source=None, action=None, include_prefetch=False, limit=100,
            offset=0, user=_user(),
        )
    assert ei.value.status_code == 400


def test_preview_url_pending_records_nothing(sb):
    sb.tables["project_files"][0]["preview_status"] = "pending"
    resp = files_mod.preview_url(PROJECT, FILE_A, user=_user())
    assert resp.status_code == 202
    assert _events(sb) == [] and sb.audits == []


def test_opened_accepts_assigned_estimator(sb):
    from app.core.deps import require_project_assignment

    route = next(
        r for r in files_mod.router.routes if getattr(r, "path", "").endswith("/{file_id}/opened")
    )
    assert require_project_assignment in [d.call for d in route.dependant.dependencies]
    files_mod.file_opened(PROJECT, FILE_A, user=_user(role=Role.ESTIMATOR))
    (ev,) = _events(sb)
    assert ev["action"] == "view" and ev["actor_role"] == Role.ESTIMATOR.value


def test_limit_pages_events_not_summary(report_sb):
    out = file_access.access_report(date_from=None, date_to=None, limit=1, offset=0)
    assert len(out["events"]) == 1
    assert len(out["summary"]) == 3


def test_source_filter(report_sb):
    out = file_access.access_report(date_from=None, date_to=None, source="submittal_file")
    assert out["events_total"] == 0 and out["summary"] == []
