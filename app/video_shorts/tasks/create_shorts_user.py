#!/usr/bin/env python3
"""
Utility script to create or update a Shorts user account.

Usage:
  python app/video_shorts/tasks/create_shorts_user.py --username demo --password secret123 --name "Demo User" --email demo@example.com --role admin
"""
import argparse
from uuid import uuid4

from werkzeug.security import generate_password_hash

from app import create_app
from app.video_shorts.services.db import ensure_storage_user_schema, get_db
from app.video_shorts.config import DEFAULT_USER_PLAN_ID


def parse_args():
    parser = argparse.ArgumentParser(description="Create or update a Shorts user account.")
    parser.add_argument("--username", required=True, help="Unique username for login")
    parser.add_argument("--password", required=True, help="Plaintext password (will be hashed)")
    parser.add_argument("--name", required=True, help="Full name")
    parser.add_argument("--email", help="Email address")
    parser.add_argument("--plan-id", default=DEFAULT_USER_PLAN_ID, help="Default storage plan ID")
    parser.add_argument("--role", default="member", help="Role (member/admin)")
    return parser.parse_args()


def main():
    args = parse_args()
    app = create_app()
    with app.app_context():
        conn = get_db()
        ensure_storage_user_schema(conn)
        row = conn.execute(
            "SELECT id FROM shorts_users WHERE lower(username) = lower(?)",
            [args.username],
        ).fetchone()
        password_hash = generate_password_hash(args.password)
        payload = {
            "name": args.name,
            "email": args.email,
            "plan_id": args.plan_id or DEFAULT_USER_PLAN_ID,
            "role": args.role or "member",
            "password_hash": password_hash,
        }
        if row:
            conn.execute(
                """
                UPDATE shorts_users
                SET name = ?, email = ?, plan_id = ?, role = ?, password_hash = ?, updated_at = now()
                WHERE id = ?
                """,
                [
                    payload["name"],
                    payload["email"],
                    payload["plan_id"],
                    payload["role"],
                    payload["password_hash"],
                    row[0],
                ],
            )
            print(f"Updated user '{args.username}'.")
        else:
            conn.execute(
                """
                INSERT INTO shorts_users (id, username, password_hash, name, email, plan_id, role)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid4()),
                    args.username,
                    payload["password_hash"],
                    payload["name"],
                    payload["email"],
                    payload["plan_id"],
                    payload["role"],
                ],
            )
            print(f"Created user '{args.username}'.")
        conn.commit()


if __name__ == "__main__":
    main()
