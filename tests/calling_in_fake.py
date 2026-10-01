"""In-memory Supabase stand-in for the Calling In tests.

Covers exactly the builder surface app/services/calling_in.py and
app/routers/calling_in.py use: select / insert / update / delete, eq / neq /
in_ / is_ / not_.in_ / gte / lte / lt / or_ (the actual_bid_at shapes the
loaders send),
order / range / limit, the `proposal_sends!inner(id)` embed on projects, the
`metadata->>key` filter, and the call_in_entries partial unique index (a
second OPEN entry for the same project and round raises a 23505 APIError,
exactly like Postgres does for the multi-worker claim).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from postgrest.exceptions import APIError

UTC = timezone.utc


def _ts(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _cmp_value(value):
    return _ts(value) if isinstance(value, str) and len(value) >= 10 and value[4] == "-" else value


def _split_top(expr: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def _eval_term(row: dict, term: str) -> bool:
    term = term.strip()
    if term.startswith("and(") and term.endswith(")"):
        return all(_eval_term(row, t) for t in _split_top(term[4:-1]))
    col, op, val = term.split(".", 2)
    have = row.get(col)
    if op == "is":
        return have is None if val == "null" else False
    if have is None:
        return False
    a, b = _cmp_value(have), _cmp_value(val)
    return {"gte": a >= b, "gt": a > b, "lte": a <= b, "lt": a < b, "eq": a == b}[op]


class _Not:
    def __init__(self, q):
        self._q = q

    def in_(self, col, vals):
        allowed = list(vals)
        self._q._filters.append(lambda r: r.get(col) not in allowed)
        return self._q

    def is_(self, col, val):
        self._q._filters.append(lambda r: r.get(col) is not None)
        return self._q


class _Query:
    def __init__(self, db: FakeSB, table: str):
        self.db = db
        self.table = table
        self._op = "select"
        self._cols = "*"
        self._payload = None
        self._filters: list = []
        self._order: list[tuple[str, bool]] = []
        self._range: tuple[int, int] | None = None
        self._limit: int | None = None
        self._single = False

    # verbs
    def select(self, cols="*", *a, **k):
        self._op, self._cols = "select", cols
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    # filters
    def eq(self, col, val):
        if "->>" in col:
            base, key = col.split("->>", 1)
            self._filters.append(lambda r: (r.get(base) or {}).get(key) == val)
        elif "." in col:
            pass  # an embedded-resource filter; the embed itself is applied in execute
        else:
            self._filters.append(lambda r: r.get(col) == val)
        return self

    def neq(self, col, val):
        self._filters.append(lambda r: r.get(col) != val)
        return self

    def in_(self, col, vals):
        allowed = list(vals)
        self._filters.append(lambda r: r.get(col) in allowed)
        return self

    def is_(self, col, val):
        assert val == "null"
        self._filters.append(lambda r: r.get(col) is None)
        return self

    @property
    def not_(self):
        return _Not(self)

    def or_(self, expr):
        terms = _split_top(expr)
        self._filters.append(lambda r: any(_eval_term(r, t) for t in terms))
        return self

    def gte(self, col, val):
        self._filters.append(
            lambda r: r.get(col) is not None and _cmp_value(r[col]) >= _cmp_value(val)
        )
        return self

    def lte(self, col, val):
        self._filters.append(
            lambda r: r.get(col) is not None and _cmp_value(r[col]) <= _cmp_value(val)
        )
        return self

    def lt(self, col, val):
        self._filters.append(
            lambda r: r.get(col) is not None and _cmp_value(r[col]) < _cmp_value(val)
        )
        return self

    def like(self, col, pattern):
        prefix = pattern.rstrip("%")
        self._filters.append(lambda r: str(r.get(col) or "").startswith(prefix))
        return self

    # shaping
    def order(self, col, desc=False, **k):
        self._order.append((col, desc))
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def _matches(self, row) -> bool:
        return all(f(row) for f in self._filters)

    def _embed_filter(self, rows):
        if self.table == "projects" and "proposal_sends!inner" in str(self._cols):
            sent = {
                s["project_id"]
                for s in self.db.tables.get("proposal_sends", [])
                if s.get("status") == "sent"
            }
            rows = [dict(r, proposal_sends=[{"id": "x"}]) for r in rows if r["id"] in sent]
        return rows

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        self.db.calls.append((self.table, self._op))
        if self._op == "select":
            hits = self._embed_filter([dict(r) for r in rows if self._matches(r)])
            for col, desc in reversed(self._order):
                hits.sort(key=lambda r: (r.get(col) is None, str(r.get(col) or "")), reverse=desc)
            if self._range is not None:
                lo, hi = self._range
                hits = hits[lo : hi + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            if self._single:
                return SimpleNamespace(data=hits[0] if hits else None)
            return SimpleNamespace(data=hits)
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", str(uuid.uuid4()))
                now = datetime.now(UTC).isoformat()
                row.setdefault("created_at", now)
                if self.table == "call_in_entries":
                    row.setdefault("closed_at", None)
                    row.setdefault("close_reason", None)
                    row.setdefault("notified_at", None)
                    clash = any(
                        e["project_id"] == row["project_id"]
                        and e["round"] == row["round"]
                        and e.get("closed_at") is None
                        for e in rows
                    )
                    if clash or self.db.force_claim_conflict:
                        raise APIError(
                            {
                                "code": "23505",
                                "message": "duplicate key value violates unique "
                                'constraint "call_in_entries_one_open"',
                                "details": None,
                                "hint": None,
                            }
                        )
                if self.table == "call_in_calls":
                    row.setdefault("updated_at", row.get("called_at") or now)
                    row.setdefault("edited_by", None)
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            gone = [dict(r) for r in rows if self._matches(r)]
            rows[:] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=gone)
        return SimpleNamespace(data=[])


class FakeSB:
    def __init__(self, tables: dict | None = None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.calls: list[tuple[str, str]] = []
        self.force_claim_conflict = False

    def table(self, name):
        return _Query(self, name)
