-- Normalize detail that belongs to a specific physical room.
-- Safe to run more than once after migrations/001_init.sql and 002_multilingual.sql.

BEGIN;

ALTER TABLE room_types
    ADD COLUMN IF NOT EXISTS bedroom_count INTEGER,
    ADD COLUMN IF NOT EXISTS bathroom_count INTEGER,
    ADD COLUMN IF NOT EXISTS bed_count INTEGER;

ALTER TABLE room_type_translations
    ADD COLUMN IF NOT EXISTS view_name TEXT,
    ADD COLUMN IF NOT EXISTS smoking_policy TEXT,
    ADD COLUMN IF NOT EXISTS wifi TEXT,
    ADD COLUMN IF NOT EXISTS floor_label TEXT,
    ADD COLUMN IF NOT EXISTS extra_bed_policy TEXT;

CREATE TABLE IF NOT EXISTS room_images (
    id            BIGSERIAL PRIMARY KEY,
    room_type_id  BIGINT NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    category_code TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (room_type_id, url)
);

CREATE INDEX IF NOT EXISTS idx_room_images_room_type
    ON room_images(room_type_id, sort_order);

CREATE TABLE IF NOT EXISTS room_amenities (
    id              BIGSERIAL PRIMARY KEY,
    room_type_id    BIGINT NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    amenity_key     TEXT NOT NULL,
    amenity_code    TEXT,
    category_code   TEXT,
    is_highlight    BOOLEAN,
    free_type       INTEGER,
    UNIQUE (room_type_id, amenity_key)
);

CREATE INDEX IF NOT EXISTS idx_room_amenities_room_type
    ON room_amenities(room_type_id);

CREATE TABLE IF NOT EXISTS room_amenity_translations (
    room_amenity_id BIGINT NOT NULL REFERENCES room_amenities(id) ON DELETE CASCADE,
    locale          TEXT NOT NULL,
    amenity_name    TEXT NOT NULL,
    category_name   TEXT,
    additional_info JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (room_amenity_id, locale),
    CONSTRAINT chk_room_amenity_translations_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

CREATE INDEX IF NOT EXISTS idx_room_amenity_translations_locale
    ON room_amenity_translations(locale);

COMMENT ON TABLE room_images IS
    'Images linked to one physical room type instead of only to the hotel album.';
COMMENT ON TABLE room_amenities IS
    'Language-neutral room amenity identity and flags.';
COMMENT ON TABLE room_amenity_translations IS
    'Localized room amenity and category labels.';

COMMIT;
