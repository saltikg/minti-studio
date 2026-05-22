import os
from pathlib import Path

import duckdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


DB_BACKEND = (os.getenv("VIDEO_SHORTS_DB_BACKEND", "duckdb") or "duckdb").strip().lower()
POSTGRES_URL = (os.getenv("VIDEO_SHORTS_DATABASE_URL") or "").strip()
DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb")


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


class _Result:
    def __init__(self, cursor):
        self._cursor = cursor
        self.description = cursor.description

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()


class _CompatConnection:
    backend_name = "postgres"

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cursor = self._conn.cursor()
        cursor.execute(_rewrite_qmark_sql(sql), list(params) if params is not None else None)
        return _Result(cursor)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _connect_postgres(*, read_only: bool):
    if not POSTGRES_URL:
        raise RuntimeError("VIDEO_SHORTS_DATABASE_URL is not set")
    if psycopg is None:
        raise RuntimeError("psycopg is required for postgres backend")
    conn = psycopg.connect(
        POSTGRES_URL,
        options="-c search_path=main,public",
        autocommit=False,
    )
    if read_only:
        conn.execute("SET default_transaction_read_only = on")
    return _CompatConnection(conn)


def connect_ro():
    if DB_BACKEND == "postgres":
        return _connect_postgres(read_only=True)
    return duckdb.connect(DB_PATH, read_only=True)


def connect_rw():
    if DB_BACKEND == "postgres":
        return _connect_postgres(read_only=False)
    return duckdb.connect(DB_PATH, read_only=False)


def json_text_expr(column_name: str, key: str) -> str:
    if DB_BACKEND == "postgres":
        return f"(CAST({column_name} AS JSONB)->>'{key}')"
    return f"json_extract_string({column_name}, '$.{key}')"


def json_int_expr(column_name: str, key: str, default: int = 0) -> str:
    if DB_BACKEND == "postgres":
        return f"CAST(COALESCE((CAST({column_name} AS JSONB)->>'{key}'), '{default}') AS INTEGER)"
    return f"CAST(json_extract({column_name}, '$.{key}') AS INTEGER)"
