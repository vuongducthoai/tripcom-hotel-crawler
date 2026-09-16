-- Add multilingual content and market-specific daily room prices.
--
-- Existing rows are treated as Vietnamese/VND so the current application keeps
-- working while the crawlers/loaders are migrated to the translation tables.
-- This migration is safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- Localized hotel content. Language-neutral fields (coordinates, stars,
-- review score, etc.) remain in hotels.
CREATE TABLE IF NOT EXISTS hotel_translations (
    hotel_id       BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    locale         TEXT NOT NULL,
    name           TEXT,
    address        TEXT,
    description    TEXT,
    hotel_type     TEXT,
    source_url     TEXT,
    raw_json       JSONB,
    crawled_at     TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hotel_id, locale),
    CONSTRAINT chk_hotel_translations_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

CREATE INDEX IF NOT EXISTS idx_hotel_translations_locale
    ON hotel_translations(locale);

-- Localized location names. The hierarchy and coordinates stay in locations.
CREATE TABLE IF NOT EXISTS location_translations (
    location_id    BIGINT NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    locale         TEXT NOT NULL,
    name           TEXT NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (location_id, locale),
    CONSTRAINT chk_location_translations_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

CREATE INDEX IF NOT EXISTS idx_location_translations_locale
    ON location_translations(locale);

-- Localized room names and bed descriptions. Capacity/area stay in room_types.
CREATE TABLE IF NOT EXISTS room_type_translations (
    room_type_id   BIGINT NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    locale         TEXT NOT NULL,
    name           TEXT NOT NULL,
    bed_type       TEXT,
    raw_json       JSONB,
    crawled_at     TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (room_type_id, locale),
    CONSTRAINT chk_room_type_translations_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

CREATE INDEX IF NOT EXISTS idx_room_type_translations_locale
    ON room_type_translations(locale);

-- Amenity identity belongs to hotel_amenities; translated labels live here.
CREATE TABLE IF NOT EXISTS hotel_amenity_translations (
    hotel_amenity_id BIGINT NOT NULL REFERENCES hotel_amenities(id) ON DELETE CASCADE,
    locale            TEXT NOT NULL,
    amenity_name      TEXT NOT NULL,
    category          TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hotel_amenity_id, locale),
    CONSTRAINT chk_hotel_amenity_translations_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

CREATE INDEX IF NOT EXISTS idx_hotel_amenity_translations_locale
    ON hotel_amenity_translations(locale);

-- One image may appear in several album tabs (for example both Featured and
-- Room). Keep the membership and its localized label instead of forcing one
-- nullable category string onto hotel_images.
CREATE TABLE IF NOT EXISTS hotel_image_categories (
    hotel_image_id BIGINT NOT NULL REFERENCES hotel_images(id) ON DELETE CASCADE,
    category_code  TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'hotel',
    locale         TEXT NOT NULL,
    category_name  TEXT NOT NULL,
    image_title    TEXT,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hotel_image_id, category_code, locale),
    CONSTRAINT chk_hotel_image_categories_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

ALTER TABLE hotel_image_categories
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'hotel';

CREATE INDEX IF NOT EXISTS idx_hotel_image_categories_locale
    ON hotel_image_categories(locale, category_code);

-- This index helps match the same amenity across languages. It is intentionally
-- non-unique because older captures can contain duplicate codes with different
-- labels; the loader will merge them safely before a stricter constraint is used.
CREATE INDEX IF NOT EXISTS idx_hotel_amenities_hotel_code
    ON hotel_amenities(hotel_id, amenity_code)
    WHERE amenity_code IS NOT NULL;

-- ---------------------------------------------------------------------------
-- A room can have a Vietnamese/VND and an English/USD quote on the same day.
ALTER TABLE hotel_prices
    ADD COLUMN IF NOT EXISTS locale TEXT,
    ADD COLUMN IF NOT EXISTS captured_date DATE;

UPDATE hotel_prices
SET locale = 'vi-VN'
WHERE locale IS NULL;

UPDATE hotel_prices
SET currency = 'VND'
WHERE currency IS NULL;

UPDATE hotel_prices
SET captured_date = (captured_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
WHERE captured_date IS NULL;

ALTER TABLE hotel_prices
    ALTER COLUMN locale SET DEFAULT 'vi-VN',
    ALTER COLUMN locale SET NOT NULL,
    ALTER COLUMN currency SET DEFAULT 'VND',
    ALTER COLUMN currency SET NOT NULL,
    ALTER COLUMN captured_date SET DEFAULT CURRENT_DATE,
    ALTER COLUMN captured_date SET NOT NULL;

-- Keep the newest row if an old import created duplicate snapshots.
WITH ranked_prices AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY hotel_id, COALESCE(room_type_id, 0), check_in, check_out,
                         currency, locale, captured_date
            ORDER BY captured_at DESC, id DESC
        ) AS duplicate_rank
    FROM hotel_prices
)
DELETE FROM hotel_prices p
USING ranked_prices r
WHERE p.id = r.id
  AND r.duplicate_rank > 1;

-- COALESCE(room_type_id, 0) also protects optional hotel-level price rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_hotel_prices_daily_market
    ON hotel_prices (
        hotel_id,
        COALESCE(room_type_id, 0),
        check_in,
        check_out,
        currency,
        locale,
        captured_date
    );

CREATE INDEX IF NOT EXISTS idx_hotel_prices_locale_currency
    ON hotel_prices(locale, currency, captured_date);

ALTER TABLE hotel_prices
    DROP CONSTRAINT IF EXISTS chk_hotel_prices_locale;

ALTER TABLE hotel_prices
    ADD CONSTRAINT chk_hotel_prices_locale
    CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$');

-- Store run-level market metadata for monitoring and debugging.
ALTER TABLE crawl_runs
    ADD COLUMN IF NOT EXISTS locale TEXT,
    ADD COLUMN IF NOT EXISTS currency TEXT;

-- ---------------------------------------------------------------------------
-- Backfill current content without overwriting translations from a prior run.
INSERT INTO hotel_translations (
    hotel_id, locale, name, address, description, hotel_type,
    source_url, raw_json, crawled_at
)
SELECT
    id, 'vi-VN', name, address, description, hotel_type,
    source_url, raw_json, last_seen_at
FROM hotels
WHERE name IS NOT NULL
   OR address IS NOT NULL
   OR description IS NOT NULL
   OR hotel_type IS NOT NULL
ON CONFLICT (hotel_id, locale) DO NOTHING;

-- Existing name_en values are useful seeds, but they are not considered a full
-- English crawl. Only fill a missing English name.
INSERT INTO hotel_translations (hotel_id, locale, name, crawled_at)
SELECT id, 'en-US', name_en, last_seen_at
FROM hotels
WHERE name_en IS NOT NULL
  AND btrim(name_en) <> ''
ON CONFLICT (hotel_id, locale) DO UPDATE
SET name = COALESCE(hotel_translations.name, EXCLUDED.name),
    updated_at = now();

INSERT INTO location_translations (location_id, locale, name)
SELECT id, 'vi-VN', name
FROM locations
WHERE name IS NOT NULL
ON CONFLICT (location_id, locale) DO NOTHING;

INSERT INTO location_translations (location_id, locale, name)
SELECT id, 'en-US', name_en
FROM locations
WHERE name_en IS NOT NULL
  AND btrim(name_en) <> ''
ON CONFLICT (location_id, locale) DO UPDATE
SET name = COALESCE(location_translations.name, EXCLUDED.name),
    updated_at = now();

INSERT INTO room_type_translations (
    room_type_id, locale, name, bed_type, raw_json
)
SELECT id, 'vi-VN', name, bed_type, raw_json
FROM room_types
WHERE name IS NOT NULL
ON CONFLICT (room_type_id, locale) DO NOTHING;

INSERT INTO hotel_amenity_translations (
    hotel_amenity_id, locale, amenity_name, category
)
SELECT id, 'vi-VN', amenity_name, category
FROM hotel_amenities
WHERE amenity_name IS NOT NULL
ON CONFLICT (hotel_amenity_id, locale) DO NOTHING;

COMMENT ON TABLE hotel_translations IS
    'Localized hotel text; one row per hotel and locale.';
COMMENT ON TABLE location_translations IS
    'Localized location names; hierarchy remains in locations.';
COMMENT ON TABLE room_type_translations IS
    'Localized room and bed labels; physical attributes remain in room_types.';
COMMENT ON TABLE hotel_amenity_translations IS
    'Localized amenity labels linked to language-neutral amenity identities.';
COMMENT ON TABLE hotel_image_categories IS
    'Many-to-many Trip.com album membership and localized category labels for images.';
COMMENT ON COLUMN hotel_prices.locale IS
    'Locale/market used to request this quote, for example vi-VN or en-US.';
COMMENT ON COLUMN hotel_prices.captured_date IS
    'Local calendar date used for one price snapshot per market and currency.';

COMMIT;
