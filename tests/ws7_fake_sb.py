"""A small in-memory Supabase stand-in for the WS7 tests (review rules,
recount, bands persistence, the review chain): table().select / insert /
upsert(on_conflict) / update / delete, eq / neq / in_ / is_ filters, limit,
order (ignored), execute(). Every call is recorded in `calls` so a
test can assert which columns were sent (null columns must be omitted)."""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from typing import Any


class _Query:
    def __init__(self, db: FakeSB, table: str) -> None:
        self.db = db
        self.table = table
        self.op = "select"
        self.payload: Any = None
        self.on_conflict: str | None = None
        self.filters: list[tuple[str, str, Any]] = []

    def select(self, _cols: str = "*") -> _Query:
        self.op = "select"
        return self

    def insert(self, payload: Any) -> _Query:
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload: Any, on_conflict: str | None = None) -> _Query:
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict
        return self

    def update(self, payload: Any) -> _Query:
        self.op, self.payload = "update", payload
        return self

    def eq(self, col: str, val: Any) -> _Query:
        self.filters.append(("eq", col, val))
        return self

    def neq(self, col: str, val: Any) -> _Query:
        self.filters.append(("neq", col, val))
        return self

    def in_(self, col: str, vals: Any) -> _Query:
        self.filters.append(("in", col, list(vals)))
        return self

    def is_(self, col: str, val: Any) -> _Query:
        self.filters.append(("is", col, val))
        return self

    def limit(self, _n: int) -> _Query:
        self._limit = int(_n)
        return self

    def order(self, _col: str, **_kw: Any) -> _Query:
        return self

    def delete(self) -> _Query:
        self.op = "delete"
        return self

    def _match(self, row: dict) -> bool:
        for op, col, val in self.filters:
            if op == "eq" and row.get(col) != val:
                return False
            if op == "neq" and row.get(col) == val:
                return False
            if op == "in" and row.get(col) not in val:
                return False
            if op == "is" and row.get(col) is not (None if val in (None, "null") else val):
                return False
        return True

    def execute(self) -> SimpleNamespace:
        if self.db.fail_tables and self.table in self.db.fail_tables:
            raise RuntimeError(f"relation {self.table} does not exist")
        rows = self.db.tables.setdefault(self.table, [])
        self.db.calls.append((self.table, self.op, copy.deepcopy(self.payload), self.on_conflict,
                              list(self.filters)))
        if self.op == "select":
            out = [copy.deepcopy(r) for r in rows if self._match(r)]
            limit = getattr(self, "_limit", None)
            return SimpleNamespace(data=out[:limit] if limit else out)
        if self.op == "delete":
            keep = [r for r in rows if not self._match(r)]
            gone = [copy.deepcopy(r) for r in rows if self._match(r)]
            rows[:] = keep
            return SimpleNamespace(data=gone)
        if self.op in ("insert", "upsert"):
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            keys = [k.strip() for k in (self.on_conflict or "").split(",") if k.strip()]
            for item in items:
                item = copy.deepcopy(item)
                existing = None
                if self.op == "upsert" and keys:
                    existing = next((r for r in rows if all(r.get(k) == item.get(k) for k in keys)), None)
                if existing is not None:
                    existing.update(item)
                    out.append(copy.deepcopy(existing))
                else:
                    item.setdefault("id", str(uuid.uuid4()))
                    rows.append(item)
                    out.append(copy.deepcopy(item))
            return SimpleNamespace(data=out)
        if self.op == "update":
            out = []
            for r in rows:
                if self._match(r):
                    r.update(copy.deepcopy(self.payload))
                    out.append(copy.deepcopy(r))
            return SimpleNamespace(data=out)
        raise AssertionError(f"unsupported op {self.op}")


class FakeSB:
    def __init__(self, fail_tables: set[str] | None = None) -> None:
        self.tables: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.fail_tables = fail_tables or set()

    def table(self, name: str) -> _Query:
        return _Query(self, name)
