-- Kimlik üretmek için sequence'lar
CREATE SEQUENCE IF NOT EXISTS seasons_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS season_phrases_id_seq START 1;

-- seasons: sezon adı + seed listesi (JSON)
CREATE TABLE IF NOT EXISTS seasons (
  id          BIGINT PRIMARY KEY DEFAULT nextval('seasons_id_seq'),
  season_name VARCHAR UNIQUE NOT NULL,
  seeds_json  JSON NOT NULL,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- season_phrases: bu sezona ait üretilen kelime öbekleri
CREATE TABLE IF NOT EXISTS season_phrases (
  id          BIGINT PRIMARY KEY DEFAULT nextval('season_phrases_id_seq'),
  season_id   BIGINT NOT NULL,
  seed        VARCHAR NOT NULL,
  phrase      VARCHAR NOT NULL,
  kept        BOOLEAN NOT NULL,
  score       DOUBLE,
  theme_hits  INTEGER,
  type_hits   INTEGER,
  drop_reason VARCHAR,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (season_id, phrase)
);

-- indeksler
CREATE INDEX IF NOT EXISTS idx_season_phrases_season ON season_phrases(season_id);
CREATE INDEX IF NOT EXISTS idx_season_phrases_kept   ON season_phrases(season_id, kept);
