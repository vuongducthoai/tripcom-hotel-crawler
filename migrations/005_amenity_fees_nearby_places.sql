BEGIN;

ALTER TABLE hotel_amenities
    ADD COLUMN IF NOT EXISTS free_type INTEGER,
    ADD COLUMN IF NOT EXISTS is_highlight BOOLEAN;

ALTER TABLE hotel_amenity_translations
    ADD COLUMN IF NOT EXISTS fee_label TEXT,
    ADD COLUMN IF NOT EXISTS additional_info JSONB;

CREATE TABLE IF NOT EXISTS hotel_nearby_places (
    id BIGSERIAL PRIMARY KEY,
    hotel_id BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    trip_poi_id TEXT NOT NULL,
    category_code TEXT,
    poi_type INTEGER,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    distance_km DOUBLE PRECISION,
    arrival_type TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (hotel_id, trip_poi_id)
);

CREATE TABLE IF NOT EXISTS hotel_nearby_place_translations (
    nearby_place_id BIGINT NOT NULL REFERENCES hotel_nearby_places(id) ON DELETE CASCADE,
    locale TEXT NOT NULL,
    name TEXT NOT NULL,
    category_name TEXT,
    distance_text TEXT,
    description TEXT,
    tags JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (nearby_place_id, locale),
    CONSTRAINT chk_hotel_nearby_place_translations_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

CREATE INDEX IF NOT EXISTS idx_hotel_nearby_places_hotel
    ON hotel_nearby_places(hotel_id, category_code, sort_order);
CREATE INDEX IF NOT EXISTS idx_hotel_nearby_place_translations_locale
    ON hotel_nearby_place_translations(locale);

COMMIT;
