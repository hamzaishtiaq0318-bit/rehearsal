"""Snapshot SQLite databases and work out exactly which rows changed between two of them."""
from __future__ import annotations

import sqlite3
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def copy_database(src: Path, dst: Path) -> None:
    """A consistent copy, even while the app is writing (SQLite online backup)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    with closing(sqlite3.connect(src, timeout=10)) as s, closing(sqlite3.connect(dst)) as d:
        s.backup(d)


def restore_database(snapshot: Path, live: Path) -> None:
    """Write a snapshot back into the live database in place, under SQLite's own locking."""
    with closing(sqlite3.connect(snapshot)) as s, closing(sqlite3.connect(live, timeout=10)) as d:
        s.backup(d)


def _readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = sorted((r for r in conn.execute(f"PRAGMA table_info({_q(table)})") if r[5] > 0), key=lambda r: r[5])
    return [r[1] for r in cols] or ["rowid"]


def _rows(conn: sqlite3.Connection, table: str, pk: list[str]) -> dict[tuple, dict]:
    select = f"SELECT rowid AS rowid, * FROM {_q(table)}" if pk == ["rowid"] else f"SELECT * FROM {_q(table)}"
    return {tuple(row[k] for k in pk): dict(row) for row in conn.execute(select)}


@dataclass
class RowChange:
    table: str
    key: dict[str, Any]
    kind: str  # added, changed or removed
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    @property
    def changed_columns(self) -> list[str]:
        if self.kind != "changed":
            return []
        return [c for c in self.after if self.before.get(c) != self.after.get(c)]

    def signature(self, volatile: frozenset[str]) -> tuple:
        """What must be identical between a rehearsal and its live replay."""
        if self.kind == "added":
            data = tuple(sorted((c, v) for c, v in self.after.items() if c not in volatile))
        elif self.kind == "changed":
            data = tuple(sorted((c, self.after[c]) for c in self.changed_columns if c not in volatile))
        else:
            data = ()
        return self.table, tuple(sorted(self.key.items())), self.kind, data


def diff_databases(before: Path, after: Path) -> list[RowChange]:
    # Known limit: full scan of every table per diff, fine for app databases up to a few hundred
    # thousand rows; larger ones would use SQLite's session extension to record changesets.
    changes: list[RowChange] = []
    with closing(_readonly(before)) as b, closing(_readonly(after)) as a:
        tb, ta = set(_tables(b)), set(_tables(a))
        for table in sorted(tb | ta):
            pk = _primary_key(a if table in ta else b, table)
            rb = _rows(b, table, pk) if table in tb else {}
            ra = _rows(a, table, pk) if table in ta else {}
            for key in sorted(rb.keys() | ra.keys(), key=lambda k: tuple(str(v) for v in k)):
                old, new = rb.get(key), ra.get(key)
                if old == new:
                    continue
                kind = "added" if old is None else "removed" if new is None else "changed"
                changes.append(RowChange(table, dict(zip(pk, key)), kind, old, new))
    return changes


def meaningful(changes: list[RowChange], volatile: frozenset[str]) -> list[RowChange]:
    """Drop changes that only touch volatile columns such as timestamps."""
    return [c for c in changes if c.kind != "changed" or any(col not in volatile for col in c.changed_columns)]


def same_effect(a: list[RowChange], b: list[RowChange], volatile: frozenset[str]) -> bool:
    return sorted(c.signature(volatile) for c in meaningful(a, volatile)) == \
        sorted(c.signature(volatile) for c in meaningful(b, volatile))


class OwnerResolver:
    """Trace a row through foreign keys to the owner table (for example, to its customer)."""

    def __init__(self, path: Path, owner_table: str | None, max_hops: int = 4):
        self.conn = _readonly(path)
        self.owner_table = owner_table
        self.max_hops = max_hops
        self._paths: dict[str, list[tuple[str, str, str]] | None] = {}

    def close(self) -> None:
        self.conn.close()

    def _path(self, table: str) -> list[tuple[str, str, str]] | None:
        if table not in self._paths:
            self._paths[table] = self._find_path(table)
        return self._paths[table]

    def _find_path(self, start: str) -> list[tuple[str, str, str]] | None:
        queue = deque([(start, [])])
        seen = {start}
        while queue:
            table, path = queue.popleft()
            if table == self.owner_table:
                return path
            if len(path) >= self.max_hops:
                continue
            for fk in self.conn.execute(f"PRAGMA foreign_key_list({_q(table)})"):
                ref_table, from_col, to_col = fk[2], fk[3], fk[4] or "rowid"
                if ref_table not in seen:
                    seen.add(ref_table)
                    queue.append((ref_table, path + [(from_col, ref_table, to_col)]))
        return None

    def owner_of(self, table: str, row: dict[str, Any]) -> Any:
        if not self.owner_table:
            return None
        path = self._path(table)
        if path is None:
            return None
        if not path:  # the row is itself an owner
            return row.get(_primary_key(self.conn, table)[0])
        current = row
        for i, (from_col, ref_table, to_col) in enumerate(path):
            value = current.get(from_col)
            if value is None:
                return None
            if i == len(path) - 1:
                return value
            found = self.conn.execute(f"SELECT * FROM {_q(ref_table)} WHERE {_q(to_col)} = ?", (value,)).fetchone()
            if found is None:
                return None
            current = dict(found)
        return None
