"""Submittal-bank router helpers (pure functions, no DB)."""

from app.routers import submittals
from app.routers.submittals import _attach_files, _parse_ids


def test_parse_ids_dedupes_and_preserves_order():
    a = "11111111-1111-1111-1111-111111111111"
    b = "22222222-2222-2222-2222-222222222222"
    assert _parse_ids(f"{a}, {b}, {a}") == [a, b]


def test_parse_ids_drops_malformed_uuids():
    # A non-UUID id would otherwise reach `.in_("id", …)` and make Postgres raise
    # an invalid-uuid error after the file object + row are stored, orphaning
    # them behind a CORS-less 500. Malformed ids are dropped instead.
    good = "33333333-3333-3333-3333-333333333333"
    assert _parse_ids(f"{good},garbage,not-a-uuid,") == [good]
    assert _parse_ids("garbage") == []
    assert _parse_ids(None) == []
    assert _parse_ids("") == []


class _FakeQuery:
    """Minimal PostgREST builder stand-in that records each `.in_` chunk size."""

    def __init__(self, links, chunk_sizes):
        self._links = links
        self._chunk_sizes = chunk_sizes
        self._chunk = None

    def select(self, *_):
        return self

    def in_(self, _col, values):
        self._chunk_sizes.append(len(values))
        self._chunk = set(values)
        return self

    def execute(self):
        data = [ln for ln in self._links if ln["material_id"] in self._chunk]
        return type("R", (), {"data": data})()


class _FakeClient:
    def __init__(self, links, chunk_sizes):
        self._links = links
        self._chunk_sizes = chunk_sizes

    def table(self, _name):
        return _FakeQuery(self._links, self._chunk_sizes)


def test_attach_files_chunks_long_id_lists(monkeypatch):
    # 748 ids in a single `.in_(...)` builds a ~29 KB URL that the PostgREST
    # gateway rejects (400 → CORS-less 500 → "Failed to fetch"). Verify the
    # lookup is split into gateway-safe chunks and still attaches correctly.
    rows = [{"id": f"{i:08d}-0000-0000-0000-000000000000"} for i in range(748)]
    links = [{"material_id": rows[0]["id"], "submittal_files": {"id": "f1"}}]
    chunk_sizes: list[int] = []
    monkeypatch.setattr(
        submittals, "get_supabase", lambda: _FakeClient(links, chunk_sizes)
    )

    result = _attach_files(rows)

    assert chunk_sizes  # queried
    assert max(chunk_sizes) <= submittals._IN_FILTER_CHUNK
    assert sum(chunk_sizes) == 748  # every id looked up, once
    assert result[0]["files"] == [{"id": "f1"}]
    assert result[1]["files"] == []


def test_attach_files_empty_rows_makes_no_query(monkeypatch):
    monkeypatch.setattr(
        submittals,
        "get_supabase",
        lambda: (_ for _ in ()).throw(AssertionError("should not query")),
    )
    assert _attach_files([]) == []
