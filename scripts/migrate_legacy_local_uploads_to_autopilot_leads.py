#!/usr/bin/env python3
"""Migrate the approved legacy /videos/9 outreach cohort into autopilot leads.

This is intentionally an allowlisted, one-time production repair.  It never moves
the original Gokhan-owned rows: real leads receive a new ``local_*`` source row in
their own tenant; discovery leads continue to reference the original source until
an email is supplied later.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.autopilot_leads import (  # noqa: E402
    _get_or_create_local_uploads_channel,
    _provision_or_reuse_lead_owner,
    _require_autopilot_leads_table,
    get_or_create_scoped_youtube_channel,
)
from app.video_shorts.services.db import get_db  # noqa: E402


# Approved after the corrected dry run.  Do not broaden this list without a new
# review: the migration must never absorb unrelated personal/test uploads.
REAL_LEADS = {
    7478: ("The Wizard of Words", "dan@danoconnortraining.com"),
    7477: ("Support Team of Terri Cole", "support@terricole.com"),
    7468: ("Jennifer Roelands, MD Perimenopause, Longevity", "support@precisionhealthmdoc.com"),
    7461: ("Sasha Hamdani MD", "office@drhamdanimd.com"),
    7460: ("Dr. Carissa Conrad, PT, DPT, TPS", "caconraddpt@gmail.com"),
    7458: ("Dr Tessa Damm | Metabolic Transformations™", "info@metabolictransformations.com"),
    7454: ("Taylor Mom on the Spectrum", "info@momonthespectrum.life"),
    7402: ("The Real Estate Lawyer", "estateplanning@thomasandwebber.com"),
    7401: ("Retire This Way with $500k", "retirethisway@yahoo.com"),
    7400: ("Jacqueline Schadeck, CFP® | Retirement Specialist", "hello@goldenws.com"),
    7345: ("Maria Wendt", "maria@mariawendt.com"),
    7339: ("Maria", "englishcozychatstudio@gmail.com"),
}

DISCOVERY_LEADS = {
    7486: "Maggie Sterling",
    7485: "A Dash of Therapy",
    7483: "Sofia Amirpoor",
    7481: "The Silent School",
    7471: "The Healthy Voice | Bella Payne",
    7467: "Rachel | Points & Miles",
    7464: "It's God's Plan",
    7457: "Monika Hoyt",
    7456: "Dr. Kim Sage, Licensed Psychologist",
    7448: "Law Mother",
    7445: "Dr. Cecelia Baldwin",
    7444: "Maledicta | Marina Karlova",
    7443: "Ritu",
    7442: "Hey Lady! English Speaking Community",
    7441: "Tanner Murtagh MSW, RSW",
    7440: "A Really Good Cry with Radhi Devlukia",
    7429: "Tristen O'Brien",
    7406: "Arkayla",
    7395: "Tess",
    7394: "Nicole Danner",
    7383: "Daniel Wong - Teen Coach",
    7349: "Holly Soulié",
    7343: "Grant Writing and Funding",
    7337: "Equipping Godly Women",
    7322: "VUS - Learning English Podcast",
    7317: "CelebrateMercy",
    7313: "Jodie Jackson",
    7312: "Katie Clarke",
    7311: "Niqueea",
}


def _source_row(conn, video_pk: int):
    row = conn.execute(
        """
        SELECT
            v.id, v.video_id, v.title, v.published_at, v.thumbnail_url,
            v.duration_seconds, v.view_count, v.like_count, v.comment_count, v.video_url,
            v.creator_name, v.creator_email, v.channel_id, v.local_bucket_channel_id,
            c.youtube_channel_id, c.channel_name
        FROM youtube_videos v
        JOIN youtube_channels c ON c.channel_id = v.channel_id
        WHERE v.id = ?
          AND (v.channel_id = 9 OR v.local_bucket_channel_id = 9)
        LIMIT 1
        """,
        [video_pk],
    ).fetchone()
    if not row:
        raise RuntimeError(f"Source {video_pk} is no longer in the approved /videos/9 scope.")
    if not str(row[14] or "").strip():
        raise RuntimeError(f"Source {video_pk} has no YouTube channel ID.")
    return row


def _was_emailed(conn, video_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM shorts_generated_videos gv
        JOIN short_share_links sl ON CAST(sl.generated_video_id AS VARCHAR) = CAST(gv.id AS VARCHAR)
        WHERE CAST(gv.source_video_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND sl.emailed_at IS NOT NULL
        LIMIT 1
        """,
        [video_id],
    ).fetchone()
    return bool(row)


def _existing_lead_id(conn, *, youtube_channel_id: str, email: str | None) -> str | None:
    row = conn.execute(
        """
        SELECT id
        FROM autopilot_leads
        WHERE youtube_channel_id = ?
           OR lower(coalesce(creator_email, '')) = lower(coalesce(?, ''))
        LIMIT 1
        """,
        [youtube_channel_id, email],
    ).fetchone()
    return str(row[0]) if row else None


def _assert_no_existing_account(conn, email: str) -> None:
    row = conn.execute(
        """
        SELECT id
        FROM shorts_users
        WHERE lower(coalesce(email, '')) = lower(?)
           OR lower(coalesce(username, '')) = lower(?)
        LIMIT 1
        """,
        [email, email],
    ).fetchone()
    if row:
        raise RuntimeError(f"An account already exists for {email}; refusing to repurpose it.")


def _insert_local_source_copy(conn, *, source, owner_user_id: str, brand_id: str, creator_name: str, creator_email: str) -> int:
    local_channel_id = _get_or_create_local_uploads_channel(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    local_video_id = f"local_{uuid4().hex}"
    conn.execute(
        """
        INSERT INTO youtube_videos (
            channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
            duration_seconds, view_count, like_count, comment_count, video_url,
            local_bucket_channel_id, owner_user_id, brand_id, download_status,
            transcript_status, subtitle_style, creator_name, creator_email
        )
        VALUES (?, ?, ?, ?, ?, FALSE, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', 'karaoke', ?, ?)
        """,
        [
            local_channel_id,
            local_video_id,
            source[2],
            source[3],
            source[4],
            source[5],
            source[6],
            source[7],
            source[8],
            source[9],
            local_channel_id,
            owner_user_id,
            brand_id,
            creator_name,
            creator_email,
        ],
    )
    row = conn.execute(
        """
        SELECT id FROM youtube_videos
        WHERE video_id = ? AND owner_user_id = ? AND brand_id = ?
        LIMIT 1
        """,
        [local_video_id, owner_user_id, brand_id],
    ).fetchone()
    if not row:
        raise RuntimeError("Copied local source row was not created.")
    return int(row[0])


def _preflight(conn) -> tuple[list[tuple[str, int, object, str | None]], list[tuple[str, int, str]]]:
    prepared = []
    skipped = []
    for video_pk, (expected_name, expected_email) in REAL_LEADS.items():
        source = _source_row(conn, video_pk)
        name = str(source[10] or "").strip()
        email = str(source[11] or "").strip().lower()
        if name != expected_name or email != expected_email:
            raise RuntimeError(f"Source {video_pk} no longer matches its approved creator identity.")
        if _was_emailed(conn, str(source[1] or "")):
            raise RuntimeError(f"Source {video_pk} was emailed after dry run; refusing migration.")
        existing_lead_id = _existing_lead_id(conn, youtube_channel_id=str(source[14]), email=email)
        if existing_lead_id:
            raise RuntimeError(f"An autopilot lead already exists for real lead source {video_pk} ({existing_lead_id}).")
        _assert_no_existing_account(conn, email)
        prepared.append(("real", video_pk, source, email))

    for video_pk, expected_name in DISCOVERY_LEADS.items():
        source = _source_row(conn, video_pk)
        name = str(source[10] or "").strip()
        email = str(source[11] or "").strip()
        if name != expected_name or email:
            raise RuntimeError(f"Source {video_pk} no longer matches its approved discovery identity.")
        if _was_emailed(conn, str(source[1] or "")):
            raise RuntimeError(f"Discovery source {video_pk} was emailed after dry run; refusing migration.")
        existing_lead_id = _existing_lead_id(conn, youtube_channel_id=str(source[14]), email=None)
        if existing_lead_id:
            skipped.append(("discovery", video_pk, existing_lead_id))
            continue
        prepared.append(("discovery", video_pk, source, None))
    return prepared, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write the approved batch after preflight succeeds.")
    args = parser.parse_args()

    conn = get_db()
    try:
        _require_autopilot_leads_table(conn)
        prepared, skipped = _preflight(conn)
        prepared_real = sum(1 for kind, *_rest in prepared if kind == "real")
        prepared_discovery = sum(1 for kind, *_rest in prepared if kind == "discovery")
        print(f"PREFLIGHT_OK real={prepared_real} discovery={prepared_discovery} skipped_existing={len(skipped)}")
        for kind, source_pk, lead_id in skipped:
            print(f"SKIPPED_EXISTING kind={kind} source_pk={source_pk} lead_id={lead_id}")
        if not args.apply:
            return 0

        created = []
        for kind, source_pk, source, email in prepared:
            creator_name = str(source[10]).strip()
            youtube_channel_id = str(source[14]).strip()
            channel_name = str(source[15] or creator_name).strip() or creator_name
            if kind == "discovery":
                lead_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO autopilot_leads (
                        id, creator_email, creator_name, youtube_channel_id, channel_id,
                        first_video_id, user_id, brand_id, created_at, converted_at
                    )
                    VALUES (?, NULL, ?, ?, ?, ?, NULL, NULL, now(), NULL)
                    """,
                    [lead_id, creator_name, youtube_channel_id, source[12], source_pk],
                )
                created.append((kind, creator_name, lead_id, source_pk))
                continue

            owner = _provision_or_reuse_lead_owner(conn, email=email, channel_name=channel_name)
            owner_user_id = owner["user_id"]
            brand_id = owner["brand_id"]
            scoped_channel_id = get_or_create_scoped_youtube_channel(
                conn,
                meta={"channel_id": youtube_channel_id, "channel_title": channel_name},
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                notes="Migrated legacy outreach lead",
            )
            if scoped_channel_id is None:
                raise RuntimeError(f"Could not create scoped channel for {creator_name}.")
            copied_video_pk = _insert_local_source_copy(
                conn,
                source=source,
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                creator_name=creator_name,
                creator_email=email,
            )
            lead_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO autopilot_leads (
                    id, creator_email, creator_name, youtube_channel_id, channel_id,
                    first_video_id, user_id, brand_id, created_at, converted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, now(), NULL)
                """,
                [lead_id, email, creator_name, youtube_channel_id, scoped_channel_id, copied_video_pk, owner_user_id, brand_id],
            )
            created.append((kind, creator_name, lead_id, copied_video_pk))

        conn.commit()
        for kind, creator_name, lead_id, video_pk in created:
            print(f"CREATED kind={kind} creator={creator_name} lead_id={lead_id} first_video_pk={video_pk}")
        print(f"APPLIED real={prepared_real} discovery={prepared_discovery} skipped_existing={len(skipped)}")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
