"""Tiny idempotent startup migrations.

`Base.metadata.create_all` creates missing tables but never adds columns to existing ones, so new
nullable columns are listed here and added with `ALTER TABLE ... ADD COLUMN` (Postgres: IF NOT EXISTS;
SQLite: checked via PRAGMA table_info first). Safe to run on every startup.
"""

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_IG_COLUMNS = [
    ("instagram_media_id", "VARCHAR"),
    ("instagram_permalink", "VARCHAR"),
    ("instagram_status", "VARCHAR"),
    ("instagram_error", "TEXT"),
]

# table -> [(column, SQL type)]; everything here must be nullable.
ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "video_generations": _IG_COLUMNS,
    "video_projects": _IG_COLUMNS,
}


def run_migrations(engine: Engine) -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for table, columns in ADDED_COLUMNS.items():
            if dialect == "sqlite":
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                if not existing:  # table doesn't exist (create_all will make it with all columns)
                    continue
                for name, sql_type in columns:
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                        log.info("migration: added %s.%s", table, name)
            else:
                for name, sql_type in columns:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sql_type}"))
        # A server restart kills any in-flight publish thread; don't leave the row blocked as 'publishing'.
        for table in ADDED_COLUMNS:
            conn.execute(
                text(
                    f"UPDATE {table} SET instagram_status = 'failed', "
                    "instagram_error = 'Interrupted by a server restart. Check Instagram before retrying.' "
                    "WHERE instagram_status = 'publishing'"
                )
            )
