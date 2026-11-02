"""In-memory Supabase stand-in for UCP handler tests.

Shared across the A4/A5/A6/A7/A8 subagent test suites. Mirrors the
chainable surface of :mod:`supabase_client` so handler code can be
tested without any HTTP or DB. Backend is just a ``dict[str, list[dict]]``
keyed by table name.

Use::

    from fixtures.supabase_mock import FakeSupabase, reset, seed

    fake = FakeSupabase()
    seed("products", [{"id": "p1", "shop_id": "s1", "price_eur": 2.50,
                        "price_minor": 250, "name": "Apple"}])
    monkeypatch.setattr(cart, "_get_supabase", lambda: fake)

Not a full PostgREST emulator — supports the ops the cart / checkout /
recipe handlers actually use (``eq`` / ``neq`` / ``in_`` / ``is_``,
``order``, ``limit``, ``single``; insert/upsert/update/delete + returning
representation). Generated / default columns (``id`` uuid, timestamps,
``price_minor``) are filled on insert.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

# ── shared backend ──────────────────────────────────────────────────
_BACKEND: dict[str, list[dict]] = {}


def reset() -> None:
    _BACKEND.clear()


def seed(table: str, rows: list[dict]) -> None:
    bucket = _BACKEND.setdefault(table, [])
    for r in rows:
        bucket.append(_apply_insert_defaults(table, r))


def dump(table: str) -> list[dict]:
    return copy.deepcopy(_BACKEND.get(table, []))


# ── defaults for generated / default columns ───────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DEFAULT_COLS: dict[str, dict[str, Any]] = {
    "carts": {
        "status": "open",
        "currency": "EUR",
    },
    "cart_line_items": {
        "base_amount_minor": 0,
        "discount_minor": 0,
        "subtotal_minor": 0,
        "tax_minor": 0,
        "total_minor": 0,
    },
}


def _apply_insert_defaults(table: str, row: dict) -> dict:
    out = dict(row)
    out.setdefault("id", str(uuid.uuid4()))
    out.setdefault("created_at", _now())
    out.setdefault("updated_at", _now())
    for k, v in _DEFAULT_COLS.get(table, {}).items():
        out.setdefault(k, v)
    # products: mirror generated ``price_minor`` column
    if table == "products" and "price_minor" not in out and "price_eur" in out:
        try:
            out["price_minor"] = int(round(float(out["price_eur"]) * 100))
        except (TypeError, ValueError):
            out["price_minor"] = 0
    return out


# ── filter DSL ──────────────────────────────────────────────────────
class _Filter:
    __slots__ = ("col", "op", "val")

    def __init__(self, col: str, op: str, val: Any) -> None:
        self.col = col
        self.op = op
        self.val = val

    def match(self, row: dict) -> bool:
        if "->>" in self.col:
            path = self.col.split("->>", 1)
            if len(path) == 2 and path[0] == "metadata":
                key = path[1]
                meta = row.get("metadata")
                if not isinstance(meta, dict):
                    return False
                cur = meta.get(key)
                if self.op == "eq":
                    return cur == self.val
                if self.op == "neq":
                    return cur != self.val
                raise AssertionError(f"unsupported op {self.op} for json path")
        cur = row.get(self.col)
        if self.op == "eq":
            return cur == self.val
        if self.op == "neq":
            return cur != self.val
        if self.op == "in":
            return cur in self.val
        if self.op == "is":
            if self.val in (None, "null"):
                return cur is None
            if self.val in (True, "true"):
                return cur is True
            if self.val in (False, "false"):
                return cur is False
            return cur == self.val
        if self.op == "gt":
            if cur is None:
                return False
            try:
                from datetime import datetime as _dt

                def _parse_ts(x: Any) -> _dt:
                    s = str(x).replace("Z", "+00:00")
                    return _dt.fromisoformat(s)

                return _parse_ts(cur) > _parse_ts(self.val)
            except Exception:
                try:
                    return float(cur) > float(self.val)  # type: ignore[arg-type]
                except Exception:
                    return str(cur) > str(self.val)
        raise AssertionError(f"unsupported op {self.op}")


# ── chainable surface ──────────────────────────────────────────────
class FakeSupabase:
    def table(self, name: str) -> "FakeTable":
        return FakeTable(name)


class FakeTable:
    def __init__(self, name: str) -> None:
        self.name = name

    def select(self, columns: str = "*") -> "FakeQuery":
        return FakeQuery(self, "SELECT")

    def insert(self, row: dict | list[dict]) -> "FakeQuery":
        q = FakeQuery(self, "INSERT")
        q._body = row
        return q

    def upsert(
        self,
        row: dict | list[dict],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
    ) -> "FakeQuery":
        q = FakeQuery(self, "UPSERT")
        q._body = row
        q._on_conflict = [col.strip() for col in (on_conflict or "id").split(",") if col.strip()]
        q._ignore_duplicates = ignore_duplicates
        return q

    def update(self, patch: dict) -> "FakeQuery":
        q = FakeQuery(self, "UPDATE")
        q._body = patch
        return q

    def delete(self) -> "FakeQuery":
        return FakeQuery(self, "DELETE")


class FakeQuery:
    def __init__(self, table: FakeTable, method: str) -> None:
        self.table = table
        self.method = method
        self._filters: list[_Filter] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._single = False
        self._body: Any = None
        self._on_conflict: list[str] = ["id"]
        self._ignore_duplicates = False

    def eq(self, col: str, val: Any) -> "FakeQuery":
        self._filters.append(_Filter(col, "eq", val))
        return self

    def neq(self, col: str, val: Any) -> "FakeQuery":
        self._filters.append(_Filter(col, "neq", val))
        return self

    def in_(self, col: str, vals: Iterable[Any]) -> "FakeQuery":
        self._filters.append(_Filter(col, "in", list(vals)))
        return self

    def is_(self, col: str, val: Any) -> "FakeQuery":
        self._filters.append(_Filter(col, "is", val))
        return self

    def gt(self, col: str, val: Any) -> "FakeQuery":
        self._filters.append(_Filter(col, "gt", val))
        return self

    def order(self, col: str, desc: bool = False) -> "FakeQuery":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._limit = n
        return self

    def single(self) -> "FakeQuery":
        self._single = True
        return self

    def _rows(self) -> list[dict]:
        return _BACKEND.setdefault(self.table.name, [])

    def _matching(self, rows: list[dict]) -> list[dict]:
        return [r for r in rows if all(f.match(r) for f in self._filters)]

    def execute(self) -> Any:
        rows = self._rows()

        if self.method == "SELECT":
            out = self._matching(rows)
            if self._order is not None:
                col, desc = self._order
                out = sorted(out, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._limit is not None:
                out = out[: self._limit]
            out = copy.deepcopy(out)
            if self._single:
                return out[0] if out else None
            return out

        if self.method == "INSERT":
            body = self._body
            batch = body if isinstance(body, list) else [body]
            inserted = [_apply_insert_defaults(self.table.name, r) for r in batch]
            rows.extend(inserted)
            inserted = copy.deepcopy(inserted)
            if self._single:
                return inserted[0] if inserted else None
            return inserted if isinstance(body, list) else (inserted[0] if inserted else None)

        if self.method == "UPSERT":
            body = self._body
            batch = body if isinstance(body, list) else [body]
            out: list[dict] = []
            for raw in batch:
                incoming = _apply_insert_defaults(self.table.name, raw)
                match = next(
                    (
                        r
                        for r in rows
                        if all(r.get(col) == incoming.get(col) for col in self._on_conflict)
                    ),
                    None,
                )
                if match is None:
                    rows.append(incoming)
                    out.append(incoming)
                elif not self._ignore_duplicates:
                    match.update(incoming)
                    out.append(match)
            out = copy.deepcopy(out)
            if self._single:
                return out[0] if out else None
            return out if isinstance(body, list) else (out[0] if out else None)

        if self.method == "UPDATE":
            patch = self._body or {}
            patch = {**patch, "updated_at": _now()}
            matched = self._matching(rows)
            for r in matched:
                r.update(patch)
            out = copy.deepcopy(matched)
            if self._single:
                return out[0] if out else None
            return out

        if self.method == "DELETE":
            keep: list[dict] = []
            removed: list[dict] = []
            for r in rows:
                if all(f.match(r) for f in self._filters):
                    removed.append(r)
                else:
                    keep.append(r)
            _BACKEND[self.table.name] = keep
            out = copy.deepcopy(removed)
            if self._single:
                return out[0] if out else None
            return out

        raise AssertionError(f"unsupported method {self.method}")
