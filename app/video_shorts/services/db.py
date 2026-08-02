import logging
import time
from uuid import uuid4

import duckdb

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency until postgres rollout
    psycopg = None

from app.video_shorts.config import (
    DEFAULT_SUB_FONT_KEY,
    DEFAULT_SUB_FONT_SIZE,
    DEFAULT_TITLE_FONT_KEY,
    DEFAULT_TITLE_FONT_SIZE,
    DEFAULT_TITLE_MARGIN,
    DEFAULT_TITLE_BG_COLOR,
    DEFAULT_TITLE_BG_ALPHA,
    DEFAULT_TITLE_TEXT_COLOR,
    DEFAULT_SUBTITLE_TEXT_COLOR,
    DEFAULT_SUBTITLE_BG_COLOR,
    DEFAULT_SUBTITLE_BG_ALPHA,
    DEFAULT_SUBTITLE_TEXT_ALPHA,
    DEFAULT_VIDEO_OVERLAY_OFFSET,
    DEFAULT_STORAGE_PLANS,
    SHORTS_CATEGORY_OPTIONS,
    SUB_MARGIN_DEFAULT,
    VIDEO_SHORTS_DB,
    VIDEO_SHORTS_DATABASE_URL,
    VIDEO_SHORTS_DB_BACKEND,
    VIDEO_SHORTS_POSTGRES_CONNECT_TIMEOUT,
)

logger = logging.getLogger(__name__)


def _is_postgres_backend() -> bool:
    return VIDEO_SHORTS_DB_BACKEND == "postgres"


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

    @property
    def rowcount(self):
        return self._cursor.rowcount


class PostgresCompatConnection:
    backend_name = "postgres"

    def __init__(self, conn):
        self._conn = conn
        self.description = None

    def execute(self, sql, params=None):
        cursor = self._conn.cursor()
        cursor.execute(_rewrite_qmark_sql(sql), list(params) if params is not None else None)
        self.description = cursor.description
        return _PostgresResult(cursor)

    def executemany(self, sql, seq_of_params):
        cursor = self._conn.cursor()
        cursor.executemany(_rewrite_qmark_sql(sql), seq_of_params)
        self.description = cursor.description
        return _PostgresResult(cursor)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _connect_postgres(*, read_only: bool = False):
    if not VIDEO_SHORTS_DATABASE_URL:
        raise RuntimeError("VIDEO_SHORTS_DATABASE_URL environment variable is not set")
    if psycopg is None:
        raise RuntimeError("psycopg is required for VIDEO_SHORTS_DB_BACKEND=postgres")
    conn = psycopg.connect(
        VIDEO_SHORTS_DATABASE_URL,
        options="-c search_path=main,public",
        autocommit=False,
        connect_timeout=max(1, int(VIDEO_SHORTS_POSTGRES_CONNECT_TIMEOUT or 3)),
    )
    if read_only:
        conn.execute("SET default_transaction_read_only = on")
    return PostgresCompatConnection(conn)


def get_db():
    if _is_postgres_backend():
        return _connect_postgres(read_only=False)
    if not VIDEO_SHORTS_DB:
        raise RuntimeError("VIDEO_SHORTS_DB environment variable is not set")
    last_exc = None
    for attempt in range(3):
        try:
            return duckdb.connect(VIDEO_SHORTS_DB, read_only=False)
        except duckdb.IOException as exc:  # pragma: no cover - duckdb specific
            last_exc = exc
            if "lock" in str(exc).lower() and attempt < 2:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover


def get_db_readonly():
    if _is_postgres_backend():
        return _connect_postgres(read_only=True)
    if not VIDEO_SHORTS_DB:
        raise RuntimeError("VIDEO_SHORTS_DB environment variable is not set")
    last_exc = None
    for attempt in range(5):
        try:
            return duckdb.connect(VIDEO_SHORTS_DB, read_only=True)
        except duckdb.IOException as exc:  # pragma: no cover - duckdb specific
            last_exc = exc
            if "lock" in str(exc).lower() and attempt < 4:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover


def _schema_management_enabled() -> bool:
    # During the first Postgres cutover we rely on the imported schema instead of
    # mutating production structure at request time.
    return not _is_postgres_backend()


def table_columns(conn, table_name: str) -> set:
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


def _ensure_transcript_schema(conn) -> set:
    """
    Make sure youtube_transcripts has the new whisper_segments_json column.
    Safe to call repeatedly.
    """
    if not _schema_management_enabled():
        return set()
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info('youtube_transcripts')").fetchall()
        }
    except Exception:
        return set()
    if "whisper_segments_json" not in cols:
        try:
            conn.execute("ALTER TABLE youtube_transcripts ADD COLUMN whisper_segments_json TEXT")
            conn.commit()
        except Exception:
            pass
        cols.add("whisper_segments_json")
    return cols


def ensure_postgres_youtube_transcripts_id_default(conn) -> None:
    """
    Older imported Postgres schemas may have youtube_transcripts.id as NOT NULL
    without a backing sequence/default. Ensure inserts can omit id safely.
    """
    if getattr(conn, "backend_name", "") != "postgres":
        return
    try:
        row = conn.execute(
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'youtube_transcripts'
              AND column_name = 'id'
            """
        ).fetchone()
    except Exception:
        return
    current_default = str((row[0] if row else "") or "").strip()
    if current_default.startswith("nextval("):
        return

    sequence_name = "youtube_transcripts_id_seq"
    try:
        conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {sequence_name}")
    except Exception:
        pass
    try:
        conn.execute(
            f"SELECT setval('{sequence_name}', COALESCE((SELECT MAX(id) FROM youtube_transcripts), 0), true)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            f"ALTER TABLE youtube_transcripts ALTER COLUMN id SET DEFAULT nextval('{sequence_name}')"
        )
    except Exception:
        pass


def _ensure_video_crop_schema(conn) -> set:
    """
    Ensure the youtube_videos table exposes crop ratio columns.
    """
    try:
        cols = table_columns(conn, "youtube_videos")
    except Exception:
        return set()
    if not cols:
        return set()
    changed = False
    definitions = [
        ("split_enabled", "BOOLEAN", False),
        ("crop_x_ratio", "DOUBLE", None),
        ("crop_y_ratio", "DOUBLE", None),
        ("crop_w_ratio", "DOUBLE", None),
        ("crop_h_ratio", "DOUBLE", None),
        ("crop2_x_ratio", "DOUBLE", None),
        ("crop2_y_ratio", "DOUBLE", None),
        ("crop2_w_ratio", "DOUBLE", None),
        ("crop2_h_ratio", "DOUBLE", None),
        ("crop_aspect", "VARCHAR", "landscape"),
        ("title_font_key", "VARCHAR", DEFAULT_TITLE_FONT_KEY),
        ("title_font_size", "INTEGER", DEFAULT_TITLE_FONT_SIZE),
        ("subtitle_font_key", "VARCHAR", DEFAULT_SUB_FONT_KEY),
        ("subtitle_font_size", "INTEGER", DEFAULT_SUB_FONT_SIZE),
        ("subtitle_margin", "INTEGER", SUB_MARGIN_DEFAULT),
        ("subtitle_style", "VARCHAR", "plain"),
        ("title_margin", "INTEGER", DEFAULT_TITLE_MARGIN),
        ("title_line_spacing", "INTEGER", -4),
        ("title_bg_color", "VARCHAR", DEFAULT_TITLE_BG_COLOR),
        ("title_bg_alpha", "INTEGER", DEFAULT_TITLE_BG_ALPHA),
        ("title_text_color", "VARCHAR", DEFAULT_TITLE_TEXT_COLOR),
        ("subtitle_text_color", "VARCHAR", DEFAULT_SUBTITLE_TEXT_COLOR),
        ("subtitle_bg_color", "VARCHAR", DEFAULT_SUBTITLE_BG_COLOR),
        ("subtitle_bg_alpha", "INTEGER", DEFAULT_SUBTITLE_BG_ALPHA),
        ("subtitle_text_alpha", "INTEGER", DEFAULT_SUBTITLE_TEXT_ALPHA),
        ("video_date_text", "VARCHAR", None),
        ("video_date_top", "INTEGER", 1006),
        ("show_title", "BOOLEAN", True),
        ("show_subtitle", "BOOLEAN", True),
        ("subscribe_overlay_enabled", "BOOLEAN", True),
        ("is_music_only", "BOOLEAN", False),
        ("static_visual_key", "VARCHAR", None),
        ("video_overlay_offset", "INTEGER", DEFAULT_VIDEO_OVERLAY_OFFSET),
        ("downloaded_at", "TIMESTAMP", None),
        ("background_visual_key", "VARCHAR", None),
        ("podcast_audio_filename", "VARCHAR", None),
        ("visual_mode", "VARCHAR", "video"),
        ("podcast_overlay_short_ids", "VARCHAR", None),
    ]
    for column, col_type, default in definitions:
        if column not in cols:
            resolved_type = "DOUBLE PRECISION" if _is_postgres_backend() and col_type == "DOUBLE" else col_type
            ddl = f"ALTER TABLE youtube_videos ADD COLUMN {column} {resolved_type}"
            if default is not None:
                if isinstance(default, str):
                    ddl += f" DEFAULT '{default}'"
                else:
                    ddl += f" DEFAULT {default}"
            try:
                conn.execute(ddl)
                cols.add(column)
                changed = True
            except Exception:
                pass
    if changed:
        try:
            conn.commit()
        except Exception:
            pass
    return cols


def ensure_storage_user_schema(conn):
    """
    Ensure tables used for user storage quotas exist and default plans are seeded.
    """
    if not _schema_management_enabled():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_storage_plans (
            plan_id VARCHAR PRIMARY KEY,
            label VARCHAR NOT NULL,
            quota_bytes BIGINT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_users (
            id UUID PRIMARY KEY DEFAULT uuid(),
            name VARCHAR NOT NULL,
            email VARCHAR,
            plan_id VARCHAR,
            custom_limit_bytes BIGINT,
            username VARCHAR,
            password_hash VARCHAR,
            role VARCHAR DEFAULT 'member',
            time_zone VARCHAR DEFAULT 'America/Los_Angeles',
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_storage_assets (
            file_key VARCHAR PRIMARY KEY,
            file_path VARCHAR NOT NULL,
            file_type VARCHAR NOT NULL,
            size_bytes BIGINT NOT NULL,
            user_id UUID,
            status VARCHAR DEFAULT 'active',
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_user_storage_events (
            id UUID PRIMARY KEY DEFAULT uuid(),
            user_id UUID,
            file_key VARCHAR,
            delta_bytes BIGINT,
            created_at TIMESTAMP DEFAULT now()
        )
        """
    )
    user_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info('shorts_users')").fetchall()
    }
    column_defs = [
        ("username", "VARCHAR"),
        ("password_hash", "VARCHAR"),
        ("role", "VARCHAR DEFAULT 'member'"),
        ("email", "VARCHAR"),
        ("google_sub", "VARCHAR"),
        ("stripe_customer_id", "VARCHAR"),
        ("stripe_subscription_id", "VARCHAR"),
        ("subscription_status", "VARCHAR"),
        ("subscription_current_period_end", "TIMESTAMP"),
        ("subscription_cancel_at_period_end", "BOOLEAN"),
        ("billing_interval", "VARCHAR"),
        ("time_zone", "VARCHAR DEFAULT 'America/Los_Angeles'"),
        ("email_verified", "BOOLEAN DEFAULT FALSE"),
        ("email_verified_at", "TIMESTAMP"),
        ("email_verification_token_hash", "VARCHAR"),
        ("email_verification_expires_at", "TIMESTAMP"),
        ("email_verification_sent_at", "TIMESTAMP"),
    ]
    for col_name, definition in column_defs:
        if col_name not in user_columns:
            conn.execute(f"ALTER TABLE shorts_users ADD COLUMN {col_name} {definition}")
    if "email_verified" not in user_columns:
        try:
            conn.execute(
                """
                UPDATE shorts_users
                SET email_verified = TRUE,
                    email_verified_at = COALESCE(email_verified_at, now())
                WHERE email_verified IS NULL OR email_verified = FALSE
                """
            )
        except Exception:
            pass

    try:
        asset_columns = table_columns(conn, "shorts_storage_assets")
        if "status" not in asset_columns:
            conn.execute("ALTER TABLE shorts_storage_assets ADD COLUMN status VARCHAR")
            conn.execute("UPDATE shorts_storage_assets SET status = 'active' WHERE status IS NULL")
        if "label" not in asset_columns:
            conn.execute("ALTER TABLE shorts_storage_assets ADD COLUMN label VARCHAR")
        if "brand_id" not in asset_columns:
            conn.execute("ALTER TABLE shorts_storage_assets ADD COLUMN brand_id VARCHAR")
    except Exception:
        pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_users_username ON shorts_users(lower(username))")
    except Exception:
        pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_users_email ON shorts_users(lower(email))")
    except Exception:
        pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_users_google_sub ON shorts_users(google_sub) WHERE google_sub IS NOT NULL")
    except Exception:
        pass

    existing = {
        row[0]
        for row in conn.execute("SELECT plan_id FROM shorts_storage_plans").fetchall()
    }
    for index, plan in enumerate(DEFAULT_STORAGE_PLANS):
        if plan["plan_id"] in existing:
            continue
        conn.execute(
            """
            INSERT INTO shorts_storage_plans (plan_id, label, quota_bytes, sort_order)
            VALUES (?, ?, ?, ?)
            """,
            [
                plan["plan_id"],
                plan["label"],
                plan["quota_bytes"],
                plan.get("sort_order", index),
            ],
        )


def ensure_user_events_schema(conn) -> None:
    if not _schema_management_enabled():
        return
    json_type = "JSONB" if getattr(conn, "backend_name", "") == "postgres" else "TEXT"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS user_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id VARCHAR NOT NULL,
            event_name VARCHAR NOT NULL,
            video_id VARCHAR,
            short_id VARCHAR,
            platform VARCHAR,
            status VARCHAR,
            metadata {json_type},
            created_at TIMESTAMP DEFAULT now()
        )
        """
    )
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_events_user_created_at ON user_events(user_id, created_at DESC)"
        )
    except Exception:
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_events_user_created_at ON user_events(user_id, created_at)"
            )
        except Exception:
            pass


def ensure_auth_user_schema(conn) -> None:
    user_columns = table_columns(conn, "shorts_users")
    if not user_columns:
        return
    column_defs = [
        ("username", "VARCHAR"),
        ("password_hash", "VARCHAR"),
        ("role", "VARCHAR DEFAULT 'member'"),
        ("email", "VARCHAR"),
        ("google_sub", "VARCHAR"),
        ("stripe_customer_id", "VARCHAR"),
        ("stripe_subscription_id", "VARCHAR"),
        ("subscription_status", "VARCHAR"),
        ("subscription_current_period_end", "TIMESTAMP"),
        ("subscription_cancel_at_period_end", "BOOLEAN"),
        ("billing_interval", "VARCHAR"),
        ("time_zone", "VARCHAR DEFAULT 'America/Los_Angeles'"),
        ("email_verified", "BOOLEAN DEFAULT FALSE"),
        ("email_verified_at", "TIMESTAMP"),
        ("email_verification_token_hash", "VARCHAR"),
        ("email_verification_expires_at", "TIMESTAMP"),
        ("email_verification_sent_at", "TIMESTAMP"),
        ("password_reset_token_hash", "VARCHAR"),
        ("password_reset_expires_at", "TIMESTAMP"),
        ("password_reset_sent_at", "TIMESTAMP"),
        ("onboarding_dismissed", "BOOLEAN DEFAULT FALSE"),
        ("admin_users_last_seen_at", "TIMESTAMP"),
    ]
    changed = False
    for col_name, definition in column_defs:
        if col_name in user_columns:
            continue
        try:
            conn.execute(f"ALTER TABLE shorts_users ADD COLUMN {col_name} {definition}")
            user_columns.add(col_name)
            changed = True
        except Exception:
            pass
    if changed and "email_verified" in user_columns:
        try:
            conn.execute(
                """
                UPDATE shorts_users
                SET email_verified = TRUE,
                    email_verified_at = COALESCE(email_verified_at, now())
                WHERE email_verified IS NULL OR email_verified = FALSE
                """
            )
        except Exception:
            pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_users_username ON shorts_users(lower(username))")
    except Exception:
        pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_users_email ON shorts_users(lower(email))")
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_users_google_sub ON shorts_users(google_sub) WHERE google_sub IS NOT NULL"
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass
    conn.commit()


def ensure_channel_owner_schema(conn):
    """
    Make sure youtube_channels and youtube_videos expose owner_user_id for per-user access control.
    """
    if not _schema_management_enabled():
        return
    try:
        channel_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info('youtube_channels')").fetchall()
        }
    except Exception:
        channel_cols = set()
    if "owner_user_id" not in channel_cols:
        try:
            conn.execute("ALTER TABLE youtube_channels ADD COLUMN owner_user_id VARCHAR")
        except Exception:
            pass
    try:
        video_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info('youtube_videos')").fetchall()
        }
    except Exception:
        video_cols = set()
    if "owner_user_id" not in video_cols:
        try:
            conn.execute("ALTER TABLE youtube_videos ADD COLUMN owner_user_id VARCHAR")
        except Exception:
            pass
    try:
        conn.execute(
            """
            UPDATE youtube_videos
            SET owner_user_id = (
                SELECT owner_user_id
                FROM youtube_channels c
                WHERE c.channel_id = youtube_videos.channel_id
            )
            WHERE owner_user_id IS NULL
        """
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_categories_schema(conn, owner_id: str | None = None) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_categories (
            id UUID PRIMARY KEY DEFAULT uuid(),
            user_id VARCHAR,
            brand_id VARCHAR,
            name VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    cols = table_columns(conn, "shorts_categories")
    if "user_id" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_categories ADD COLUMN user_id VARCHAR")
            cols.add("user_id")
        except Exception:
            pass
    if "brand_id" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_categories ADD COLUMN brand_id VARCHAR")
            cols.add("brand_id")
        except Exception:
            pass
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_categories_user_brand_name ON shorts_categories(lower(name), user_id, brand_id)"
        )
    except Exception:
        pass

    def _has_rows_for_user(uid: str) -> bool:
        try:
            return bool(
                conn.execute(
                    "SELECT COUNT(*) FROM shorts_categories WHERE user_id = ?",
                    [uid],
                ).fetchone()[0]
            )
        except Exception:
            return False

    if owner_id:
        if not _has_rows_for_user(owner_id):
            try:
                templates = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM shorts_categories WHERE user_id IS NULL ORDER BY lower(name)"
                    ).fetchall()
                    if row and row[0]
                ]
            except Exception:
                templates = []
            if not templates:
                templates = list(dict.fromkeys(SHORTS_CATEGORY_OPTIONS))
            for item in templates:
                try:
                    conn.execute(
                        "INSERT INTO shorts_categories (user_id, name) VALUES (?, ?)",
                        [owner_id, item],
                    )
                except Exception:
                    pass
    else:
        try:
            has_any = conn.execute("SELECT 1 FROM shorts_categories LIMIT 1").fetchone()
        except Exception:
            has_any = None
        if not has_any and SHORTS_CATEGORY_OPTIONS:
            for item in SHORTS_CATEGORY_OPTIONS:
                try:
                    conn.execute(
                        "INSERT INTO shorts_categories (name) VALUES (?)",
                        [item],
                    )
                except Exception:
                    pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_knowledge_base_schema(conn) -> None:
    if not _schema_management_enabled():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_kb_generations (
            id UUID PRIMARY KEY,
            owner_user_id VARCHAR,
            source_video_pk INTEGER NOT NULL,
            source_entry_key VARCHAR,
            source_video_id VARCHAR,
            source_plan_index VARCHAR,
            source_short_video_id VARCHAR,
            source_title TEXT,
            source_published_at TIMESTAMP,
            generation_status VARCHAR DEFAULT 'draft',
            main_question TEXT,
            short_answer TEXT,
            transcript_summary TEXT,
            source_video_url VARCHAR,
            generated_with_model VARCHAR,
            raw_payload_json TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_kb_similar_questions (
            id UUID PRIMARY KEY,
            generation_id UUID NOT NULL,
            question TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            decision VARCHAR DEFAULT 'pending',
            page_id UUID,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_kb_pages (
            id UUID PRIMARY KEY,
            owner_user_id VARCHAR,
            source_video_pk INTEGER NOT NULL,
            generation_id UUID,
            similar_question_id UUID,
            page_type VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'draft',
            question TEXT NOT NULL,
            answer TEXT,
            transcript_summary TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_kb_source_reviews (
            source_entry_key VARCHAR PRIMARY KEY,
            owner_user_id VARCHAR,
            is_relevant BOOLEAN DEFAULT true,
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_kb_generations_source
            ON shorts_kb_generations(source_video_pk)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shorts_kb_generations_owner
            ON shorts_kb_generations(owner_user_id, updated_at)
            """
        )
    except Exception:
        pass
    generation_cols = table_columns(conn, "shorts_kb_generations")
    generation_column_defs = [
        ("source_entry_key", "VARCHAR"),
        ("source_video_id", "VARCHAR"),
        ("source_plan_index", "VARCHAR"),
        ("source_short_video_id", "VARCHAR"),
        ("source_title", "TEXT"),
        ("source_published_at", "TIMESTAMP"),
    ]
    for col_name, definition in generation_column_defs:
        if col_name not in generation_cols:
            try:
                conn.execute(f"ALTER TABLE shorts_kb_generations ADD COLUMN {col_name} {definition}")
            except Exception:
                pass
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_kb_generations_entry_key
            ON shorts_kb_generations(source_entry_key)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shorts_kb_similar_generation
            ON shorts_kb_similar_questions(generation_id, sort_order)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shorts_kb_pages_source
            ON shorts_kb_pages(source_video_pk, page_type)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shorts_kb_pages_status
            ON shorts_kb_pages(status, updated_at)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shorts_kb_source_reviews_relevant
            ON shorts_kb_source_reviews(is_relevant, updated_at)
            """
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_prompt_settings_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_prompt_settings (
            key VARCHAR PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT now(),
            updated_by VARCHAR
        )
        """
    )


def ensure_static_images_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_static_images (
            id UUID PRIMARY KEY DEFAULT uuid(),
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            asset_kind VARCHAR DEFAULT 'background',
            category_id UUID,
            use_as_background BOOLEAN DEFAULT false,
            label VARCHAR,
            filename VARCHAR NOT NULL,
            file_size BIGINT,
            file_ext VARCHAR,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    cols = table_columns(conn, "shorts_static_images")
    if "is_active" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_static_images ADD COLUMN is_active BOOLEAN DEFAULT true")
        except Exception:
            pass
    if "category_id" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_static_images ADD COLUMN category_id UUID")
        except Exception:
            pass
    if "use_as_background" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_static_images ADD COLUMN use_as_background BOOLEAN DEFAULT false")
        except Exception:
            pass
    if "brand_id" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_static_images ADD COLUMN brand_id VARCHAR")
        except Exception:
            pass
    if "asset_kind" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_static_images ADD COLUMN asset_kind VARCHAR DEFAULT 'background'")
        except Exception:
            pass
    try:
        conn.execute(
            """
            UPDATE shorts_static_images
            SET asset_kind = 'background'
            WHERE asset_kind IS NULL OR trim(asset_kind) = ''
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_static_images_user ON shorts_static_images(user_id)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_static_images_active ON shorts_static_images(user_id, is_active)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_static_images_category ON shorts_static_images(user_id, category_id)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_static_images_background ON shorts_static_images(user_id, use_as_background)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_static_images_kind ON shorts_static_images(user_id, brand_id, asset_kind)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            UPDATE shorts_static_images
            SET category_id = NULL
            WHERE category_id IS NOT NULL
              AND category_id NOT IN (SELECT id FROM shorts_static_image_categories)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            UPDATE shorts_static_images AS i
            SET use_as_background = true
            FROM shorts_static_image_categories AS c
            WHERE i.category_id = c.id
              AND COALESCE(i.use_as_background, false) = false
              AND lower(trim(coalesce(c.name, ''))) IN ('background', 'backgrounds', 'arka plan', 'arkaplan')
            """
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_background_preferences_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_background_preferences (
            id UUID PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            background_key VARCHAR,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    cols = table_columns(conn, "shorts_background_preferences")
    if "brand_id" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_background_preferences ADD COLUMN brand_id VARCHAR")
        except Exception:
            pass
    if "background_key" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_background_preferences ADD COLUMN background_key VARCHAR")
        except Exception:
            pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shorts_background_preferences_user_brand ON shorts_background_preferences(user_id, brand_id)"
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_user_preferences_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_user_preferences (
            id UUID PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            preference_key VARCHAR NOT NULL,
            preference_value VARCHAR,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    cols = table_columns(conn, "shorts_user_preferences")
    if "preference_key" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_user_preferences ADD COLUMN preference_key VARCHAR")
        except Exception:
            pass
    if "preference_value" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_user_preferences ADD COLUMN preference_value VARCHAR")
        except Exception:
            pass
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_user_preferences_user_key
            ON shorts_user_preferences(user_id, preference_key)
            """
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_static_image_categories_schema(
    conn,
    owner_id: str | None = None,
    brand_id: str | None = None,
) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_static_image_categories (
            id UUID PRIMARY KEY DEFAULT uuid(),
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            name VARCHAR NOT NULL,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    cols = table_columns(conn, "shorts_static_image_categories")
    if "is_active" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_static_image_categories ADD COLUMN is_active BOOLEAN DEFAULT true")
        except Exception:
            pass
    if "brand_id" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_static_image_categories ADD COLUMN brand_id VARCHAR")
        except Exception:
            pass
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_static_image_categories_user_brand_name
            ON shorts_static_image_categories(lower(name), user_id, brand_id)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_static_image_categories_user_active
            ON shorts_static_image_categories(user_id, is_active)
            """
        )
    except Exception:
        pass

    if owner_id:
        try:
            has_rows = conn.execute(
                """
                SELECT COUNT(*)
                FROM shorts_static_image_categories
                WHERE user_id = ?
                  AND COALESCE(is_active, true) = true
                  AND (? IS NULL OR brand_id = ?)
                """,
                [owner_id, brand_id, brand_id],
            ).fetchone()
        except Exception:
            has_rows = None
        if not has_rows or not has_rows[0]:
            try:
                conn.execute(
                    """
                    INSERT INTO shorts_static_image_categories (id, user_id, brand_id, name, is_active)
                    VALUES (?, ?, ?, ?, true)
                    """,
                    [str(uuid4()), owner_id, brand_id, "Genel"],
                )
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_image_to_video_jobs_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_to_video_jobs (
            job_id UUID PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            brand_id VARCHAR,
            image_ids_json TEXT,
            payload_json TEXT,
            status VARCHAR,
            progress DOUBLE,
            output_url VARCHAR,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    cols = table_columns(conn, "image_to_video_jobs")
    if "brand_id" not in cols:
        try:
            conn.execute("ALTER TABLE image_to_video_jobs ADD COLUMN brand_id VARCHAR")
        except Exception:
            pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_to_video_jobs_user ON image_to_video_jobs(user_id)"
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def ensure_instagram_queue_schema(conn):
    """
    Ensure we have a queue for Instagram short uploads.
    """
    if not _schema_management_enabled():
        return
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shorts_instagram_queue (
                id VARCHAR PRIMARY KEY,
                user_id VARCHAR,
                video_id VARCHAR,
                plan_index VARCHAR,
                clip_filename VARCHAR,
                caption_text VARCHAR,
                publish_at VARCHAR,
                status VARCHAR,
                status_detail VARCHAR,
                created_at VARCHAR,
                updated_at VARCHAR,
                instagram_business_account_id VARCHAR,
                instagram_username VARCHAR,
                instagram_media_id VARCHAR,
                published_at VARCHAR,
                youtube_video_id VARCHAR,
                youtube_short_id VARCHAR,
                plan_title VARCHAR,
                permalink VARCHAR,
                like_count INTEGER,
                comment_count INTEGER,
                impressions INTEGER,
                reach INTEGER,
                saved INTEGER,
                shares INTEGER,
                media_type VARCHAR DEFAULT 'reel'
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('shorts_instagram_queue')").fetchall()
    }
    if "last_seen_comment_count" not in cols:
        try:
            conn.execute(
                "ALTER TABLE shorts_instagram_queue ADD COLUMN last_seen_comment_count INTEGER DEFAULT 0"
            )
        except Exception:
            pass


def ensure_facebook_queue_schema(conn):
    """
    Ensure we have a queue for Facebook Page uploads.
    """
    if not _schema_management_enabled():
        return
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shorts_facebook_queue (
                id VARCHAR PRIMARY KEY,
                user_id VARCHAR,
                video_id VARCHAR,
                plan_index VARCHAR,
                clip_filename VARCHAR,
                caption_text VARCHAR,
                publish_at VARCHAR,
                status VARCHAR,
                status_detail VARCHAR,
                created_at VARCHAR,
                updated_at VARCHAR,
                page_id VARCHAR,
                page_name VARCHAR,
                facebook_video_id VARCHAR,
                published_at VARCHAR,
                plan_title VARCHAR,
                permalink VARCHAR,
                media_type VARCHAR DEFAULT 'feed',
                view_count INTEGER,
                reach INTEGER,
                impressions INTEGER,
                reactions INTEGER,
                comment_count INTEGER
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('shorts_facebook_queue')").fetchall()
    }
    if "media_type" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_facebook_queue ADD COLUMN media_type VARCHAR DEFAULT 'feed'")
        except Exception:
            pass
    for col_name, col_type in (
        ("view_count", "INTEGER"),
        ("reach", "INTEGER"),
        ("impressions", "INTEGER"),
        ("reactions", "INTEGER"),
        ("comment_count", "INTEGER"),
    ):
        if col_name not in cols:
            try:
                conn.execute(f"ALTER TABLE shorts_facebook_queue ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    if "last_seen_comment_count" not in cols:
        try:
            conn.execute(
                "ALTER TABLE shorts_facebook_queue ADD COLUMN last_seen_comment_count INTEGER DEFAULT 0"
            )
        except Exception:
            pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facebook_queue_status ON shorts_facebook_queue(status)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facebook_queue_video ON shorts_facebook_queue(video_id)"
        )
    except Exception:
        pass


def ensure_tiktok_queue_schema(conn):
    """
    Ensure we have a queue for TikTok short uploads.
    """
    if not _schema_management_enabled():
        return
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shorts_tiktok_queue (
                id VARCHAR PRIMARY KEY,
                user_id VARCHAR,
                video_id VARCHAR,
                plan_index VARCHAR,
                clip_filename VARCHAR,
                caption_text VARCHAR,
                publish_at VARCHAR,
                status VARCHAR,
                status_detail VARCHAR,
                created_at VARCHAR,
                updated_at VARCHAR,
                tiktok_open_id VARCHAR,
                tiktok_username VARCHAR,
                tiktok_video_id VARCHAR,
                tiktok_publish_id VARCHAR,
                published_at VARCHAR,
                plan_title VARCHAR,
                last_error_code TEXT,
                last_error_message TEXT,
                last_error_logid TEXT,
                last_error_payload TEXT,
                last_http_status INTEGER,
                last_step TEXT
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('shorts_tiktok_queue')").fetchall()
    }
    if "tiktok_publish_id" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_tiktok_queue ADD COLUMN tiktok_publish_id VARCHAR")
        except Exception:
            pass
    if "last_error_code" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_tiktok_queue ADD COLUMN last_error_code TEXT")
        except Exception:
            pass
    if "last_error_message" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_tiktok_queue ADD COLUMN last_error_message TEXT")
        except Exception:
            pass
    if "last_error_logid" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_tiktok_queue ADD COLUMN last_error_logid TEXT")
        except Exception:
            pass
    if "last_error_payload" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_tiktok_queue ADD COLUMN last_error_payload TEXT")
        except Exception:
            pass
    if "last_http_status" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_tiktok_queue ADD COLUMN last_http_status INTEGER")
        except Exception:
            pass
    if "last_step" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_tiktok_queue ADD COLUMN last_step TEXT")
        except Exception:
            pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tiktok_queue_status ON shorts_tiktok_queue(status)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tiktok_queue_video ON shorts_tiktok_queue(video_id)"
        )
    except Exception:
        pass
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('shorts_instagram_queue')").fetchall()
    }
    if "media_type" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_instagram_queue ADD COLUMN media_type VARCHAR DEFAULT 'reel'")
        except Exception:
            pass
    if "impressions" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_instagram_queue ADD COLUMN impressions INTEGER")
        except Exception:
            pass
    if "reach" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_instagram_queue ADD COLUMN reach INTEGER")
        except Exception:
            pass
    if "saved" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_instagram_queue ADD COLUMN saved INTEGER")
        except Exception:
            pass
    if "shares" not in cols:
        try:
            conn.execute("ALTER TABLE shorts_instagram_queue ADD COLUMN shares INTEGER")
        except Exception:
            pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_instagram_queue_status ON shorts_instagram_queue(status)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_instagram_queue_video ON shorts_instagram_queue(video_id)"
        )
    except Exception:
        pass

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS instagram_comment_cache (
                media_id VARCHAR PRIMARY KEY,
                like_count INTEGER,
                comment_count INTEGER,
                last_synced VARCHAR,
                comments_json TEXT
            )
            """
        )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def ensure_interview_practice_schema(conn):
    """
    Ensure interview practice tables exist for both DuckDB and Postgres backends.
    Uses int_* prefix to isolate this feature from existing tables.
    """
    blob_type = "BYTEA" if getattr(conn, "backend_name", "") == "postgres" else "BLOB"

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS int_interviews (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS int_tags (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS int_interview_tags (
            interview_id VARCHAR NOT NULL,
            tag_id VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS int_recordings (
            id VARCHAR PRIMARY KEY,
            interview_id VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            note TEXT,
            transcript TEXT,
            secondary_text TEXT,
            mime_type VARCHAR,
            audio_blob {blob_type} NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS int_interview_materials (
            id VARCHAR PRIMARY KEY,
            interview_id VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            sort_order INTEGER DEFAULT 0,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_int_interviews_user_created ON int_interviews(user_id, created_at)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_int_tags_user_name ON int_tags(user_id, name)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_int_interview_tags_interview ON int_interview_tags(interview_id)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_int_recordings_interview_created ON int_recordings(interview_id, created_at)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_int_materials_interview_order ON int_interview_materials(interview_id, sort_order, created_at)"
        )
    except Exception:
        pass

    try:
        conn.commit()
    except Exception:
        pass
