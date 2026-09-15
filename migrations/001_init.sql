-- Schema khởi tạo cho crawler Trip.com.
-- Chạy:  psql -h localhost -U tripcom -d tripcom -f migrations/001_init.sql
-- Hoặc tự động khi docker compose up lần đầu (file được mount vào initdb).

CREATE TABLE IF NOT EXISTS locations (
    id                BIGSERIAL PRIMARY KEY,
    trip_location_id  TEXT UNIQUE,
    name              TEXT NOT NULL,
    name_en           TEXT,
    type              TEXT,                    -- country | province | city | district
    parent_id         BIGINT REFERENCES locations(id),
    country_code      TEXT,
    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hotels (
    id                BIGSERIAL PRIMARY KEY,
    trip_hotel_id     TEXT NOT NULL UNIQUE,    -- natural key để upsert
    name              TEXT,
    name_en           TEXT,
    url               TEXT,
    location_id       BIGINT REFERENCES locations(id),
    address           TEXT,
    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    star_rating       NUMERIC(2,1),
    review_score      NUMERIC(3,1),
    review_count      INTEGER,
    hotel_type        TEXT,
    description       TEXT,
    price_from        NUMERIC(14,2),
    currency          TEXT DEFAULT 'VND',
    is_cheap_listing  BOOLEAN NOT NULL DEFAULT FALSE,
    source_url        TEXT,
    raw_json          JSONB,                   -- giữ nguyên bản, cứu anh khi site đổi layout
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_hotels_location ON hotels(location_id);
CREATE INDEX IF NOT EXISTS idx_hotels_price    ON hotels(price_from);
CREATE INDEX IF NOT EXISTS idx_hotels_cheap    ON hotels(is_cheap_listing) WHERE is_cheap_listing;

CREATE TABLE IF NOT EXISTS hotel_images (
    id         BIGSERIAL PRIMARY KEY,
    hotel_id   BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    category   TEXT,
    sort_order INTEGER DEFAULT 0,
    UNIQUE (hotel_id, url)
);

CREATE TABLE IF NOT EXISTS hotel_amenities (
    id           BIGSERIAL PRIMARY KEY,
    hotel_id     BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    amenity_code TEXT,
    amenity_name TEXT NOT NULL,
    category     TEXT,
    UNIQUE (hotel_id, amenity_name)
);

CREATE TABLE IF NOT EXISTS room_types (
    id             BIGSERIAL PRIMARY KEY,
    hotel_id       BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    trip_room_id   TEXT,
    name           TEXT NOT NULL,
    bed_type       TEXT,
    max_occupancy  INTEGER,
    area_sqm       NUMERIC(6,1),
    raw_json       JSONB,
    UNIQUE (hotel_id, trip_room_id)
);

-- Time-series giá. Chỉ dùng nếu anh Vũ chốt là cần giá theo ngày.
CREATE TABLE IF NOT EXISTS hotel_prices (
    id            BIGSERIAL PRIMARY KEY,
    hotel_id      BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    room_type_id  BIGINT REFERENCES room_types(id) ON DELETE CASCADE,
    check_in      DATE NOT NULL,
    check_out     DATE NOT NULL,
    price         NUMERIC(14,2),
    currency      TEXT DEFAULT 'VND',
    tax_included  BOOLEAN,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_prices_hotel_date ON hotel_prices(hotel_id, check_in);

-- Vận hành: biết run nào chạy khi nào, hỏng ở đâu.
CREATE TABLE IF NOT EXISTS crawl_runs (
    id          BIGSERIAL PRIMARY KEY,
    target      TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | success | failed
    stats       JSONB
);

CREATE TABLE IF NOT EXISTS crawl_errors (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT REFERENCES crawl_runs(id) ON DELETE CASCADE,
    url           TEXT,
    http_status   INTEGER,
    error_message TEXT,
    payload       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
