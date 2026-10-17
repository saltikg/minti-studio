-- Run with psql outside a transaction. CONCURRENTLY cannot run in a transaction block.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fct_video_daily_deltas_brand_date_channel
    ON analytics.fct_video_daily_deltas (brand_id, snapshot_date, channel_type, video_id)
    INCLUDE (views, likes, comments, reach);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fct_video_daily_metrics_brand_date_channel
    ON analytics.fct_video_daily_metrics (brand_id, snapshot_date, channel_type, video_id)
    INCLUDE (views, likes, comments, reach, impressions, saved, stats_source);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snapshots_brand_channel_video_latest
    ON main.shorts_video_daily_snapshots (brand_id, channel_type, video_id, snapshot_date DESC, effective_at DESC)
    INCLUDE (views, likes, comments, reach);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snapshots_brand_channel_video_metrics
    ON main.shorts_video_daily_snapshots (brand_id, channel_type, video_id, snapshot_date)
    INCLUDE (views, likes, stats_source);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_snapshots_dashboard_youtube_latest
    ON main.shorts_video_daily_snapshots (
        brand_id,
        channel_type,
        video_id,
        snapshot_date,
        effective_at DESC
    )
    INCLUDE (views, likes)
    WHERE COALESCE(stats_source, '') NOT LIKE 'estimated_gap_fill_from_%';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_dim_generated_videos_brand_publish
    ON analytics.dim_generated_videos (
        brand_id,
        publish_status,
        (COALESCE(youtube_published_at, published_at, planned_publish_at)::date)
    )
    INCLUDE (generated_video_id, generated_title, youtube_video_id, instagram_media_id);
