import os
import time
from pathlib import Path
from typing import Optional, Type

import duckdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DUCKDB_PATH = ROOT_DIR / "warehouse" / "blog_factory.duckdb"
POSTGRES_URL = (
    os.getenv("VIDEO_SHORTS_DATABASE_URL")
    or os.getenv("VIDEO_SHORTS_POSTGRES_URL")
    or os.getenv("DATABASE_URL")
    or ""
).strip()
_db_backend = (os.getenv("VIDEO_SHORTS_DB_BACKEND") or "").strip().lower()
if _db_backend:
    DB_BACKEND = _db_backend
elif POSTGRES_URL:
    DB_BACKEND = "postgres"
else:
    DB_BACKEND = "duckdb"
DUCKDB_PATH = os.getenv("DB_PATH", str(DEFAULT_DUCKDB_PATH))


def _rewrite_qmark_sql(sql: str) -> str:
    if "?" not in sql:
        return sql
    parts = []
    in_single = False
    in_double = False
    index = 0
    while index < len(sql):
        ch = sql[index]
        if ch == "'" and not in_double:
            if in_single and index + 1 < len(sql) and sql[index + 1] == "'":
                parts.append("''")
                index += 2
                continue
            in_single = not in_single
            parts.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            parts.append(ch)
        elif ch == "?" and not in_single and not in_double:
            parts.append("%s")
        else:
            parts.append(ch)
        index += 1
    return "".join(parts)


class _PostgresResult:
    def __init__(self, cursor):
        self._cursor = cursor
        self.description = cursor.description

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()


class _PostgresCompatConnection:
    backend_name = "postgres"

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cursor = self._conn.cursor()
        cursor.execute(_rewrite_qmark_sql(sql), list(params) if params is not None else None)
        return _PostgresResult(cursor)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _connect_postgres(read_only: bool):
    if not POSTGRES_URL:
        raise RuntimeError("VIDEO_SHORTS_DATABASE_URL is not set")
    if psycopg is None:
        raise RuntimeError("psycopg is required for postgres token store backend")
    conn = psycopg.connect(
        POSTGRES_URL,
        options="-c search_path=main,public",
        autocommit=False,
    )
    if read_only:
        conn.execute("SET default_transaction_read_only = on")
    return _PostgresCompatConnection(conn)


def connect_store(*, read_only: bool, retries: int, error_cls: Type[Exception]):
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            if DB_BACKEND == "postgres":
                return _connect_postgres(read_only=read_only)
            return duckdb.connect(DUCKDB_PATH, read_only=read_only)
        except Exception as exc:
            last_exc = exc
            message = str(exc)
            if "lock" in message.lower() and attempt + 1 < retries:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise error_cls(message) from exc
    raise error_cls(str(last_exc))  # pragma: no cover


def has_columns(conn, table_name: str) -> set[str]:
    if getattr(conn, "backend_name", "") == "postgres":
        try:
            rows = conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = ?
                """,
                [table_name],
            ).fetchall()
        except Exception:
            return set()
        return {row[0] for row in rows}
    try:
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    except Exception:
        return set()
    return {row[1] for row in rows}


def relation_missing(exc: Exception, table_name: str) -> bool:
    message = str(exc).lower()
    return table_name.lower() in message and (
        "does not exist" in message
        or "undefinedtable" in message
        or "catalog" in message
        or "no such table" in message
    )
