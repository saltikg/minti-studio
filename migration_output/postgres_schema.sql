BEGIN;
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."authors" (
    "author_id" TEXT NOT NULL,
    "display_name" TEXT NOT NULL,
    "avatar_url" TEXT,
    "bio_short" TEXT,
    "created_at" TIMESTAMP,
    "author_bio" TEXT,
    "primary_category_slug" TEXT,
    "primary_category" TEXT,
    PRIMARY KEY ("author_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."blog_contents" (
    "idea_id" TEXT NOT NULL,
    "title" TEXT,
    "category_slug" TEXT,
    "slug" TEXT,
    "front_matter" TEXT,
    "introduction" TEXT,
    "product_gallery" TEXT,
    "urunler" TEXT,
    "buyers_guide" TEXT,
    "faq" TEXT,
    "conclusion" TEXT,
    "recommendations" TEXT,
    "cta" TEXT,
    "md_full" TEXT,
    "updated_at" TIMESTAMP,
    "hero_image_url" TEXT,
    "hero_alt" TEXT,
    "overview_updated" TEXT,
    "related_links_json" TEXT,
    "tags_json" TEXT,
    "faq_json" JSONB,
    "buyers_guide_json" JSONB,
    PRIMARY KEY ("idea_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."blog_posts" (
    "id" BIGINT NOT NULL,
    "season_phrase_id" BIGINT,
    "blog_title" TEXT,
    "status" TEXT,
    "created_at" TIMESTAMP,
    "idea_id" TEXT,
    "author_id" TEXT,
    "date_published" TIMESTAMP,
    "hero_image_url" TEXT,
    "hero_alt" TEXT,
    "summary" TEXT,
    PRIMARY KEY ("id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."blog_preflight_decisions" (
    "idea_id" TEXT NOT NULL,
    "decision_json" TEXT,
    "updated_at" TIMESTAMP,
    PRIMARY KEY ("idea_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."categories" (
    "slug" TEXT NOT NULL,
    "name" TEXT,
    PRIMARY KEY ("slug")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."categories_tree" (
    "slug" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "parent_slug" TEXT,
    "sort_order" INTEGER,
    "is_active" BOOLEAN,
    "nav_visible" BOOLEAN,
    "icon" TEXT,
    "created_at" TIMESTAMP,
    "updated_at" TIMESTAMP,
    PRIMARY KEY ("slug")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."categories_tree_new" (
    "slug" TEXT,
    "name" TEXT,
    "parent_slug" TEXT,
    "sort_order" INTEGER,
    "is_active" BOOLEAN,
    "nav_visible" BOOLEAN,
    "icon" TEXT,
    "created_at" TIMESTAMP,
    "updated_at" TIMESTAMPTZ
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."content_plan" (
    "id" INTEGER,
    "title" TEXT,
    "category" TEXT,
    "brand" TEXT,
    "season" TEXT,
    "reasoning" TEXT,
    "publish_date" DATE,
    "risk" DOUBLE PRECISION,
    "created_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."daily_trends" (
    "id" BIGINT,
    "topic" TEXT,
    "source" TEXT,
    "category" TEXT,
    "volume" INTEGER,
    "locale" TEXT,
    "reason" TEXT,
    "trend_date" DATE,
    "detected_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."facebook_oauth_tokens" (
    "user_id" TEXT NOT NULL,
    "page_access_token" TEXT,
    "facebook_page_id" TEXT,
    "facebook_page_name" TEXT,
    "expires_at" TEXT,
    "scopes" TEXT,
    "updated_at" TEXT,
    PRIMARY KEY ("user_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."facebook_page_tokens" (
    "user_id" TEXT NOT NULL,
    "fb_user_id" TEXT,
    "page_id" TEXT,
    "page_name" TEXT,
    "page_access_token" TEXT,
    "token_created_at" TEXT,
    "expires_at" TEXT,
    "scopes" TEXT,
    "updated_at" TEXT,
    PRIMARY KEY ("user_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."google_trends_score" (
    "phrase_id" INTEGER,
    "phrase" TEXT,
    "trend_score" REAL,
    "shopping_intent_score" REAL,
    "llm_score" REAL,
    "final_score" REAL,
    "trend_date" DATE,
    "related_keywords" JSONB,
    "created_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."idea_products" (
    "idea_id" TEXT,
    "parent_asin" TEXT,
    "source" TEXT,
    "is_primary" BOOLEAN,
    "discount_pct" DOUBLE PRECISION,
    "original_price" DOUBLE PRECISION,
    "sale_price" DOUBLE PRECISION,
    "item_end_date" TIMESTAMP,
    "availability_status" TEXT,
    "is_active" BOOLEAN,
    "last_checked_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."idea_rules_deal" (
    "idea_id" TEXT NOT NULL,
    "rules_json" TEXT NOT NULL,
    "created_at" TIMESTAMP,
    "updated_at" TIMESTAMP,
    PRIMARY KEY ("idea_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."ideas" (
    "idea_id" TEXT NOT NULL,
    "idea_title" TEXT,
    "created_at" TIMESTAMP,
    "category_slug" TEXT,
    PRIMARY KEY ("idea_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."instagram_oauth_pending" (
    "user_id" TEXT NOT NULL,
    "pages_json" TEXT,
    "expires_at" TEXT,
    "scopes" TEXT,
    "updated_at" TEXT,
    PRIMARY KEY ("user_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."instagram_oauth_tokens" (
    "user_id" TEXT NOT NULL,
    "page_access_token" TEXT,
    "instagram_business_account_id" TEXT,
    "facebook_page_id" TEXT,
    "facebook_page_name" TEXT,
    "expires_at" TEXT,
    "scopes" TEXT,
    "updated_at" TEXT,
    "instagram_username" TEXT,
    "meta_fb_user_id" TEXT,
    "selected_page_id" TEXT,
    "token_created_at" TEXT,
    PRIMARY KEY ("user_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."llm_decisions_log" (
    "id" BIGINT,
    "idea_id" TEXT,
    "decision_json" TEXT,
    "prompt_meta" TEXT,
    "created_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."product_enrichment" (
    "product_id" TEXT NOT NULL,
    "ship_type" TEXT,
    "ship_cost_value" DOUBLE PRECISION,
    "ship_cost_currency" TEXT,
    "ship_free" BOOLEAN,
    "ship_min_eta" TIMESTAMP,
    "ship_max_eta" TIMESTAMP,
    "ship_cutoff" TIMESTAMP,
    "ship_eta_min_days" INTEGER,
    "ship_eta_max_days" INTEGER,
    "returns_accepted" BOOLEAN,
    "return_window_days" INTEGER,
    "return_shipping_payer" TEXT,
    "refund_method" TEXT,
    "return_method" TEXT,
    "restocking_fee_pct" DOUBLE PRECISION,
    "condition_id" TEXT,
    "condition_name" TEXT,
    "source_json" JSONB,
    "updated_at" TIMESTAMP,
    PRIMARY KEY ("product_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."product_media" (
    "parent_asin" TEXT NOT NULL,
    "image_url" TEXT NOT NULL,
    "source" TEXT NOT NULL,
    "created_at" TIMESTAMP,
    PRIMARY KEY ("parent_asin", "image_url", "source")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."product_media_new" (
    "parent_asin" TEXT NOT NULL,
    "image_url" TEXT NOT NULL,
    "source" TEXT NOT NULL,
    "created_at" TIMESTAMP,
    PRIMARY KEY ("parent_asin", "image_url", "source")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."product_metrics" (
    "parent_asin" TEXT NOT NULL,
    "avg_rating" DOUBLE PRECISION,
    "n_reviews" INTEGER,
    PRIMARY KEY ("parent_asin")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."product_metrics_ebay" (
    "id" BIGINT NOT NULL,
    "product_id" TEXT NOT NULL,
    "seller_score" DOUBLE PRECISION,
    "feedback_pct" DOUBLE PRECISION,
    "feedback_score" BIGINT,
    "returns" TEXT,
    "eta_days" INTEGER,
    "trust_level" TEXT,
    "review_rating" DOUBLE PRECISION,
    "review_count" BIGINT,
    "summary" TEXT,
    "created_at" TIMESTAMP,
    PRIMARY KEY ("id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."product_review_summaries" (
    "parent_asin" TEXT NOT NULL,
    "review_paragraph" TEXT,
    "review_pros" TEXT,
    "review_cons" TEXT,
    "review_summary_short" TEXT,
    "review_loved" TEXT,
    "review_tips" TEXT,
    PRIMARY KEY ("parent_asin")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."product_text" (
    "parent_asin" TEXT NOT NULL,
    "description" TEXT,
    "features" TEXT,
    "pros_raw" TEXT,
    "cons_raw" TEXT,
    PRIMARY KEY ("parent_asin")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."products" (
    "parent_asin" TEXT NOT NULL,
    "product_title" TEXT,
    "brand" TEXT,
    "price" TEXT,
    "category_slug" TEXT,
    "source" TEXT,
    "external_id" TEXT,
    PRIMARY KEY ("parent_asin")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_brand_categories" (
    "brand_slug" TEXT NOT NULL,
    "category_slug" TEXT NOT NULL,
    PRIMARY KEY ("brand_slug", "category_slug")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_brands" (
    "slug" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "primary_category_slug" TEXT NOT NULL,
    PRIMARY KEY ("slug")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_categories" (
    "slug" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    PRIMARY KEY ("slug")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_decisions" (
    "run_id" TEXT NOT NULL,
    "brand" TEXT,
    "sd_category_key" TEXT,
    "filters_json" TEXT,
    "buying_options" TEXT,
    "post_tag" TEXT,
    "title_suggest" TEXT,
    "created_at" TIMESTAMP,
    PRIMARY KEY ("run_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_errors" (
    "run_id" TEXT,
    "step" TEXT,
    "error_msg" TEXT,
    "created_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_item_enrichment" (
    "run_id" TEXT NOT NULL,
    "item_id" TEXT NOT NULL,
    "ship_type" TEXT,
    "ship_cost_value" DOUBLE PRECISION,
    "ship_cost_ccy" TEXT,
    "ship_free" BOOLEAN,
    "ship_eta_min_days" INTEGER,
    "ship_eta_max_days" INTEGER,
    "returns_accepted" BOOLEAN,
    "return_window_days" INTEGER,
    "return_shipping_payer" TEXT,
    "refund_method" TEXT,
    "return_method" TEXT,
    "restocking_fee_pct" DOUBLE PRECISION,
    "condition_id" TEXT,
    "condition_name" TEXT,
    "detail_json" TEXT,
    "updated_at" TIMESTAMP,
    PRIMARY KEY ("run_id", "item_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_items" (
    "run_id" TEXT NOT NULL,
    "item_id" TEXT NOT NULL,
    "title" TEXT,
    "brand" TEXT,
    "price_value" DOUBLE PRECISION,
    "currency" TEXT,
    "raw_json" TEXT,
    PRIMARY KEY ("run_id", "item_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_publications" (
    "run_id" TEXT NOT NULL,
    "idea_id" TEXT,
    "brand" TEXT,
    "sd_category_key" TEXT,
    "date_published" DATE,
    PRIMARY KEY ("run_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_run_stats" (
    "run_id" TEXT NOT NULL,
    "total_kept" INTEGER,
    "max_discount" INTEGER,
    "avg_discount" DOUBLE PRECISION,
    "price_min" DOUBLE PRECISION,
    "price_max" DOUBLE PRECISION,
    "hero_image" TEXT,
    PRIMARY KEY ("run_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_runs" (
    "run_id" TEXT NOT NULL,
    "started_at" TIMESTAMP,
    "llm_prompt" TEXT,
    "candidate_json" TEXT,
    "season_json" TEXT,
    "decision_ctx" TEXT,
    "llm_decision" TEXT,
    "status" TEXT,
    "notes" TEXT,
    PRIMARY KEY ("run_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sd_seed_brands" (
    "brand" TEXT NOT NULL,
    PRIMARY KEY ("brand")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."season_phrases" (
    "id" BIGINT,
    "season_id" BIGINT NOT NULL,
    "seed" TEXT NOT NULL,
    "phrase" TEXT NOT NULL,
    "kept" BOOLEAN NOT NULL,
    "score" DOUBLE PRECISION,
    "theme_hits" INTEGER,
    "type_hits" INTEGER,
    "drop_reason" TEXT,
    "created_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."seasons" (
    "id" BIGINT,
    "season_name" TEXT,
    "seeds_json" JSONB,
    "theme_tokens" JSONB,
    "type_tokens" JSONB,
    "created_at" TIMESTAMP,
    "season_group" TEXT,
    "start_date" DATE,
    "end_date" DATE,
    "locale" TEXT,
    "reason" TEXT
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."sources" (
    "slug" TEXT NOT NULL,
    "name" TEXT,
    PRIMARY KEY ("slug")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."tiktok_oauth_tokens" (
    "user_id" TEXT NOT NULL,
    "access_token" TEXT,
    "refresh_token" TEXT,
    "open_id" TEXT,
    "username" TEXT,
    "scopes" TEXT,
    "expires_at" TEXT,
    "refresh_expires_at" TEXT,
    "updated_at" TEXT,
    "display_name" TEXT,
    PRIMARY KEY ("user_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."trend_feed_snapshot" (
    "id" INTEGER,
    "topic" TEXT,
    "source" TEXT,
    "change_pct" TEXT,
    "date" DATE,
    "collected_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."trend_ideas" (
    "idea_id" BIGINT NOT NULL,
    "trend_id" BIGINT,
    "title" TEXT,
    "idea_type" TEXT,
    "category_slug" TEXT,
    "status" TEXT,
    "created_at" TIMESTAMP,
    "updated_at" TIMESTAMP,
    PRIMARY KEY ("idea_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."trend_instagram_posts" (
    "insta_id" BIGINT,
    "idea_id" BIGINT,
    "image_path" TEXT,
    "caption_json" TEXT,
    "created_at" TIMESTAMP
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."trend_publications" (
    "pub_id" BIGINT NOT NULL,
    "idea_id" BIGINT,
    "published_url" TEXT,
    "published_at" TIMESTAMP,
    PRIMARY KEY ("pub_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."trend_topics" (
    "trend_id" BIGINT NOT NULL,
    "trend" TEXT NOT NULL,
    "description" TEXT,
    "slug" TEXT,
    "created_at" TIMESTAMP,
    PRIMARY KEY ("trend_id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."trends_categories" (
    "category_id" INTEGER,
    "category_name" TEXT,
    "parent_id" INTEGER
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."youtube_captions" (
    "id" BIGINT NOT NULL,
    "video_id" BIGINT NOT NULL,
    "caption_text" TEXT NOT NULL,
    "source" TEXT,
    "lang" TEXT,
    "created_at" TIMESTAMP,
    PRIMARY KEY ("id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."youtube_channels" (
    "id" BIGINT NOT NULL,
    "channel_handle" TEXT,
    "channel_url" TEXT,
    "channel_title" TEXT,
    "is_active" BOOLEAN,
    "notes" TEXT,
    "last_checked_at" TIMESTAMP,
    "created_at" TIMESTAMP,
    PRIMARY KEY ("id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."youtube_trend_ideas" (
    "id" BIGINT NOT NULL,
    "idea_date" DATE NOT NULL,
    "channel_id" BIGINT,
    "video_id" BIGINT,
    "channel_title" TEXT,
    "video_title" TEXT,
    "video_url" TEXT,
    "idea_text" TEXT NOT NULL,
    "status" TEXT,
    "notes" TEXT,
    "created_at" TIMESTAMP,
    "updated_at" TIMESTAMP,
    PRIMARY KEY ("id")
);
CREATE SCHEMA IF NOT EXISTS "main";
CREATE TABLE IF NOT EXISTS "main"."youtube_videos" (
    "id" BIGINT NOT NULL,
    "channel_id" BIGINT NOT NULL,
    "video_id" TEXT NOT NULL,
    "video_title" TEXT,
    "video_url" TEXT,
    "published_at" TIMESTAMP,
    "has_captions" BOOLEAN,
    "caption_lang" TEXT,
    "last_checked_at" TIMESTAMP,
    "created_at" TIMESTAMP,
    "duration_seconds" INTEGER,
    "is_short" BOOLEAN,
    "view_count" BIGINT,
    "like_count" BIGINT,
    "comment_count" BIGINT,
    "stats_fetched_at" TIMESTAMP,
    "download_status" TEXT,
    PRIMARY KEY ("id")
);
COMMIT;
