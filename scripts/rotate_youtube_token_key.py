#!/usr/bin/env python3
"""Rotate using the staged new/previous env keys. Never display either key."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from app.video_shorts.services.youtube_token_store import rotate_refresh_token_encryption
if __name__ == "__main__":
    print("ROTATION_OK", rotate_refresh_token_encryption())
