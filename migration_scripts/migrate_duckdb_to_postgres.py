from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, execute_values


ROOT = Path("/home/ubuntu/apps/minti_studio")
DUCKDB_PATH = ROOT / "migration_input" / "blog_factory.duckdb"
APP_ENV_PATH = ROOT / ".env"
META_PATH = ROOT / "migration_output" / "duckdb_metadata.json"
SUMMARY_PATH = ROOT / "migration_logs" / "migration_summary.json"
LOG_PATH = ROOT / "migration_logs" / "migration_run.log"
BATCH_SIZE = 1000


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def duck_table(schema: str, table: str) -> str:
    return f"{qident(schema)}.{qident(table)}"


def get_target_column_types(pg_conn, schema: str, table: str) -> dict[str, str]:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return {name: dtype for name, dtype in cur.fetchall()}


def normalize_value(value: Any, pg_type: str) -> Any:
    if value is None:
        return None

    if pg_type in {"json", "jsonb"}:
        if isinstance(value, Json):
            return value
        if isinstance(value, str):
            try:
                return Json(json.loads(value))
            except json.JSONDecodeError:
                return Json(value)
        return Json(value)

    if pg_type == "bytea":
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        return str(value).encode("utf-8")

    if isinstance(value, memoryview):
        return value.tobytes()

    if isinstance(value, (datetime, date, Decimal, bool, int, float, str, bytes)):
        return value

    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)

    return str(value)


def normalize_batch(rows: list[tuple[Any, ...]], column_names: list[str], column_types: dict[str, str]) -> list[tuple[Any, ...]]:
    normalized: list[tuple[Any, ...]] = []
    pg_types = [column_types.get(name, "text") for name in column_names]
    for row in rows:
        normalized.append(
            tuple(normalize_value(value, pg_type) for value, pg_type in zip(row, pg_types))
        )
    return normalized


def main() -> int:
    ROOT.joinpath("migration_logs").mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(),
        ],
    )

    env = load_env(APP_ENV_PATH)
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(f"DATABASE_URL not found in {APP_ENV_PATH}")

    metadata = json.loads(META_PATH.read_text())
    summary: dict[str, Any] = {
        "duckdb_path": str(DUCKDB_PATH),
        "database_url_host": "127.0.0.1",
        "batch_size": BATCH_SIZE,
        "tables": [],
    }

    logging.info("Opening DuckDB read-only: %s", DUCKDB_PATH)
    duck = duckdb.connect(str(DUCKDB_PATH), read_only=True)

    logging.info("Connecting to PostgreSQL")
    pg = psycopg2.connect(database_url)
    pg.autocommit = False

    overall_ok = 0
    overall_issue = 0

    try:
        for table_meta in metadata:
            schema = table_meta["schema"]
            table = table_meta["table"]
            source_count = int(table_meta["row_count"])
            column_names = [col["column_name"] for col in table_meta["columns"]]
            fq_name = f"{schema}.{table}"
            table_summary: dict[str, Any] = {
                "table": fq_name,
                "source_count": source_count,
                "inserted_rows": 0,
                "status": "pending",
                "error": None,
            }

            logging.info("Starting table %s", fq_name)

            try:
                column_types = get_target_column_types(pg, schema, table)
                with pg.cursor() as cur:
                    cur.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                            sql.Identifier(schema), sql.Identifier(table)
                        )
                    )
                    existing_rows = cur.fetchone()[0]

                table_summary["existing_rows_before"] = existing_rows
                if existing_rows:
                    msg = f"target table already has {existing_rows} rows; skipping to avoid duplicate loads"
                    logging.warning("%s: %s", fq_name, msg)
                    table_summary["status"] = "skipped_nonempty"
                    table_summary["error"] = msg
                    summary["tables"].append(table_summary)
                    overall_issue += 1
                    continue

                select_sql = f"SELECT * FROM {duck_table(schema, table)}"
                duck_cur = duck.execute(select_sql)
                batch_num = 0

                while True:
                    rows = duck_cur.fetchmany(BATCH_SIZE)
                    if not rows:
                        break
                    batch_num += 1
                    payload = normalize_batch(rows, column_names, column_types)

                    with pg.cursor() as cur:
                        insert_sql = sql.SQL("INSERT INTO {}.{} ({}) VALUES %s").format(
                            sql.Identifier(schema),
                            sql.Identifier(table),
                            sql.SQL(", ").join(sql.Identifier(col) for col in column_names),
                        )
                        execute_values(cur, insert_sql.as_string(pg), payload, page_size=BATCH_SIZE)

                    pg.commit()
                    table_summary["inserted_rows"] += len(payload)
                    logging.info(
                        "Committed batch %s for %s (%s rows this batch, %s total)",
                        batch_num,
                        fq_name,
                        len(payload),
                        table_summary["inserted_rows"],
                    )

                table_summary["status"] = "ok"
                summary["tables"].append(table_summary)
                overall_ok += 1
                logging.info(
                    "Finished table %s (%s/%s rows)",
                    fq_name,
                    table_summary["inserted_rows"],
                    source_count,
                )
            except Exception as exc:
                pg.rollback()
                table_summary["status"] = "error"
                table_summary["error"] = str(exc)
                summary["tables"].append(table_summary)
                overall_issue += 1
                logging.exception("Table migration failed for %s", fq_name)

        summary["tables_ok"] = overall_ok
        summary["tables_with_issues"] = overall_issue
        SUMMARY_PATH.write_text(json.dumps(summary, indent=2) + "\n")
        logging.info(
            "Migration finished: %s tables ok, %s tables with issues",
            overall_ok,
            overall_issue,
        )
        return 0 if overall_issue == 0 else 1
    finally:
        pg.close()
        duck.close()


if __name__ == "__main__":
    raise SystemExit(main())
