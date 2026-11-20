from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from app.video_shorts import worker
from app.video_shorts.services import discovery_health, media_utils


def test_scheduled_source_cache_eviction_is_lru_and_preserves_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(media_utils, "SOURCE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(media_utils, "SOURCE_CACHE_MAX_GB", 1)
    monkeypatch.setattr(media_utils, "_SOURCE_CACHE_MIN_FREE_BYTES", 10**30)
    old = tmp_path / "old.mp4"
    new = tmp_path / "new.mp4"
    leased = tmp_path / "leased.mp4"
    for path in (old, new, leased):
        path.write_bytes(path.name.encode())
    now = time.time()
    os.utime(old, (now - 300, now - 300))
    os.utime(new, (now - 200, now - 200))
    os.utime(leased, (now - 400, now - 400))
    leases = tmp_path / ".leases"
    leases.mkdir()
    (leases / "active.lease").write_text(
        json.dumps({"pid": os.getpid(), "path": str(leased), "created_at": now})
    )

    preview = media_utils.evict_source_cache(dry_run=True)
    assert preview["removed"] == [str(old), str(new)]
    assert all(path.exists() for path in (old, new, leased))

    result = media_utils.evict_source_cache()
    assert result["removed"] == [str(old), str(new)]
    assert not old.exists()
    assert not new.exists()
    assert leased.exists()


def test_temp_janitor_dry_run_matches_only_old_minti_artifacts(monkeypatch):
    old_dir = Path(tempfile.mkdtemp(prefix="minti-split-smoke-test-", dir="/tmp"))
    recent_dir = Path(tempfile.mkdtemp(prefix="minti-split-smoke-test-", dir="/tmp"))
    try:
        (old_dir / "frame.jpg").write_bytes(b"old")
        (recent_dir / "frame.jpg").write_bytes(b"recent")
        old_time = time.time() - (49 * 3600)
        os.utime(old_dir / "frame.jpg", (old_time, old_time))
        os.utime(old_dir, (old_time, old_time))
        monkeypatch.setattr(worker, "_active_job_temp_refs", lambda: set())
        monkeypatch.setattr(worker, "_path_has_open_handle", lambda _path: False)
        result = worker.run_discovery_temp_janitor(force=True, dry_run=True)
        assert str(old_dir) in result["deleted"]
        assert str(recent_dir) not in result["deleted"]
        assert old_dir.exists()
    finally:
        shutil.rmtree(old_dir, ignore_errors=True)
        shutil.rmtree(recent_dir, ignore_errors=True)


def test_health_dry_run_reports_disk_and_queue_conditions(monkeypatch):
    disk = shutil._ntuple_diskusage(total=100, used=72, free=28)
    monkeypatch.setattr(discovery_health.shutil, "disk_usage", lambda _path: disk)
    monkeypatch.setattr(
        discovery_health,
        "_queue_snapshot",
        lambda _now: {
            "queued_over_24h": 3,
            "oldest_queued_at": "old",
            "processing": 0,
            "last_activity_at": "2026-11-20T04:00:00+00:00",
        },
    )
    monkeypatch.setattr(discovery_health, "_generating_queue_stalls", lambda _now: [])
    monkeypatch.setattr(discovery_health, "_load_alert_state", lambda _key: (None, {}))
    result = discovery_health.evaluate_discovery_health(
        gate_reason=None,
        in_night_window=False,
        dry_run=True,
        now=datetime(2026, 11, 20, 8, tzinfo=timezone.utc),
    )
    by_key = {item["alert_key"]: item for item in result["conditions"]}
    assert by_key["discovery_health:disk_70"]["would_alert"] is True
    assert by_key["discovery_health:queue_stall"]["would_alert"] is True
    assert by_key["discovery_health:gate:disk"]["would_alert"] is False
