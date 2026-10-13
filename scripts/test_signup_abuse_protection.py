#!/usr/bin/env python3
"""Technical checks for signup abuse protection."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

tmp_dir = tempfile.TemporaryDirectory()
db_path = Path(tmp_dir.name) / "signup_abuse.duckdb"
os.environ["VIDEO_SHORTS_DB_BACKEND"] = "duckdb"
os.environ["VIDEO_SHORTS_DB"] = str(db_path)
os.environ["SECRET_KEY"] = "test-secret"
os.environ["SIGNUP_ALIAS_ALLOWLIST"] = "gokhansaltik@gmail.com"

from werkzeug.security import generate_password_hash

from app import create_app
from app.video_shorts.routes.auth import (
    _is_disposable_signup_email,
    _signup_alias_allowed,
    normalize_signup_email,
    registration_abuse_error,
)
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db


def _insert_user(conn, email: str, *, ip_hash: str = "ip-a") -> str:
    user_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO shorts_users (
            id, username, password_hash, name, email, email_normalized,
            registration_ip_hash, role, plan_id, email_verified, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'member', 'plan_free', TRUE, now())
        """,
        [
            user_id,
            email,
            generate_password_hash("StrongPass123!"),
            email.split("@", 1)[0],
            email,
            normalize_signup_email(email),
            ip_hash,
        ],
    )
    return user_id


def main() -> int:
    app = create_app()
    with app.app_context():
        conn = get_db()
        try:
            ensure_storage_user_schema(conn)
            ensure_auth_user_schema(conn)
            ensure_storage_user_schema(conn)
            ensure_auth_user_schema(conn)
            _insert_user(conn, "a@gmail.com", ip_hash="ip-1")
            assert registration_abuse_error(
                conn,
                email="a+x@gmail.com",
                registration_ip_hash="ip-2",
            ) == "An account with this email already exists — sign in instead."
            _insert_user(conn, "ab@gmail.com", ip_hash="ip-3")
            assert registration_abuse_error(
                conn,
                email="a.b@gmail.com",
                registration_ip_hash="ip-4",
            ) == "An account with this email already exists — sign in instead."
            assert _is_disposable_signup_email("someone@cwsgear.com")
            assert registration_abuse_error(
                conn,
                email="someone@cwsgear.com",
                registration_ip_hash="ip-5",
            ) == "Please use a permanent email address."
            assert _signup_alias_allowed("gokhansaltik+test@gmail.com")
            assert registration_abuse_error(
                conn,
                email="g.o.k.h.a.n.s.a.l.t.i.k+test@gmail.com",
                registration_ip_hash="ip-1",
            ) == ""
            _insert_user(conn, "one@example.com", ip_hash="ip-limit")
            _insert_user(conn, "two@example.com", ip_hash="ip-limit")
            _insert_user(conn, "three@example.com", ip_hash="ip-limit")
            assert registration_abuse_error(
                conn,
                email="four@example.com",
                registration_ip_hash="ip-limit",
            ) == "Too many successful registrations from this network. Please try again later."
            conn.commit()
        finally:
            conn.close()
    print("SIGNUP_ABUSE_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
