"""Numbered SQL migrations applied in order and recorded in ``schema_migrations``."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"

_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def _ensure_registry(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def discover_migrations(sql_dir: Optional[Path] = None) -> list[tuple[int, str, Path]]:
    """Return ``(version, name, path)`` for every migration file, ordered by version."""
    directory = sql_dir or SQL_DIR
    found: list[tuple[int, str, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise ValueError(
                f"Migration filename must be NNNN_snake_case.sql, got {path.name!r}"
            )
        found.append((int(match.group(1)), match.group(2), path))

    versions = [v for v, _, _ in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise ValueError(f"Duplicate migration versions: {sorted(duplicates)}")
    return found


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Versions already recorded as applied."""
    _ensure_registry(conn)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def run_migrations(
    conn: sqlite3.Connection, sql_dir: Optional[Path] = None
) -> list[int]:
    """Apply pending migrations in version order. Returns versions applied this run."""
    _ensure_registry(conn)
    done = applied_versions(conn)
    applied: list[int] = []

    for version, name, path in discover_migrations(sql_dir):
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        # executescript() implicitly commits, so the guard row goes in the same call.
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (version, name),
        )
        conn.commit()
        applied.append(version)
        logger.info("Applied migration %04d_%s", version, name)

    return applied
