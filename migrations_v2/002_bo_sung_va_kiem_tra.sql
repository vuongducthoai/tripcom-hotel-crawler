-- =============================================================================
-- Schema v2 — bản bổ sung 002 (chạy SAU 001_schema_v2.sql)
--
--   A. Trường còn thiếu, phát hiện khi đối chiếu schema với raw thật
--      (EN 972 hotel / 7141 gói, VI 882 hotel / 4822 gói)
--   B. Sửa chỗ sai của 001 (khoảng cách đường chim bay, diện tích dạng khoảng)
--   C. Ràng buộc CHECK — chốt chặn cuối: code quên kiểm tra thì DB vẫn từ chối
--   D. Bảng theo dõi kiểm tra dữ liệu: load_runs, load_rejects
--
-- File 001 giữ nguyên như bản mentor đã duyệt; mọi thay đổi nằm ở đây.
-- =============================================================================

SET search_path TO v2;

-- =============================================================================
-- A. TRƯỜNG CÒN THIẾU
-- =============================================================================

-- Loại phòng -------------------------------------------------------------------
ALTER TABLE room_types
    -- Trip.com hay ghi diện tích dạng khoảng "236–322 ft²":
    --   area_sqm = số nhỏ, area_sqm_max = số lớn (NULL nếu chỉ một số)
    ADD COLUMN area_sqm_max   NUMERIC(8,2),
    ADD COLUMN rent_type      SMALLINT,        -- houseTypeInfo.rentType (1 = nguyên căn, 2 = phòng riêng trong nhà chung)
    ADD COLUMN property_type  SMALLINT,        -- houseTypeInfo.propertyType
    ADD COLUMN view_id        INTEGER;         -- outdoorLandscapeInfo.id (240 = hướng thành phố)

ALTER TABLE room_type_i18n
    ADD COLUMN floor_text     TEXT,            -- floorInfo "Floor: 1-4" (≈35% phòng)
    ADD COLUMN rent_text      TEXT,            -- rent "2 bedrooms, Full Rental" (≈25%)
    ADD COLUMN house_note     TEXT,            -- houseTypeExtraDesc "Phòng riêng trong nhà chung…"
    ADD COLUMN special_note   TEXT;            -- policyInfo.specialNote (≈2%): "gồm 2 phòng thông nhau"

-- Gói giá ----------------------------------------------------------------------
ALTER TABLE room_offers
    ADD COLUMN free_cancel_until  TIMESTAMPTZ,                     -- hạn hủy miễn phí (giờ khách sạn)
    ADD COLUMN is_partner_offer   BOOLEAN NOT NULL DEFAULT FALSE;  -- "Do đối tác cung cấp" (≈30%)

-- Các mức phí hủy theo thời gian (cancelInfo.ladderDetailInfo, ≈28% gói):
--   trước 23:59 20/9 → miễn phí (ratio 0); sau đó → mất 100% (ratio 1)
-- Chỉ lưu tỷ lệ; số tiền = ratio × tổng tiền ở từng tiền tệ.
CREATE TABLE room_offer_cancel_tiers (
    offer_id       BIGINT   NOT NULL REFERENCES room_offers(id) ON DELETE CASCADE,
    tier_no        SMALLINT NOT NULL,
    starts_at      TIMESTAMPTZ,
    ends_at        TIMESTAMPTZ,
    penalty_ratio  NUMERIC(5,4) NOT NULL CHECK (penalty_ratio BETWEEN 0 AND 1),
    PRIMARY KEY (offer_id, tier_no),
    CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)
);

ALTER TABLE room_offer_i18n
    ADD COLUMN cancel_tiers     JSONB NOT NULL DEFAULT '[]',   -- [{"when":"Trước 23:59 20/9","title":"Hủy miễn phí"}]
    ADD COLUMN discount_labels  JSONB NOT NULL DEFAULT '[]',   -- [{"text":"Giảm 74%","hover":"…"}] (≈49%)
    ADD COLUMN partner_text     TEXT;                          -- "Do đối tác của chúng tôi cung cấp"

-- Lưu ý đặt phòng của khách sạn (getDetailAdditionalInfo.hotelReservationTips,
-- ≈99% hotel: "Không nhận trẻ em", "Phí phòng"…) KHÔNG cần bảng mới: lưu vào
-- hotel_policy_sections / hotel_policy_lines với section_code = 'reservationTip'.


-- =============================================================================
-- B. SỬA CHỖ SAI CỦA 001
-- =============================================================================

-- 95% khoảng cách lân cận là "theo đường thẳng" (arrivalType = LINEAR_DISTANCE),
-- 001 chỉ cho 'walk' | 'drive' → thêm 'straight_line'.
ALTER TABLE hotel_nearby_places DROP CONSTRAINT hotel_nearby_places_travel_mode_check;
ALTER TABLE hotel_nearby_places ADD CONSTRAINT hotel_nearby_places_travel_mode_check
    CHECK (travel_mode IN ('walk', 'drive', 'straight_line', 'unknown'));


-- =============================================================================
-- C. RÀNG BUỘC — lớp kiểm tra cuối cùng trong database
--    (code Python đã kiểm tra trước; đây là lưới an toàn)
-- =============================================================================

ALTER TABLE hotels
    ADD CONSTRAINT ck_hotels_trip_id      CHECK (trip_hotel_id > 0),
    ADD CONSTRAINT ck_hotels_lat          CHECK (latitude  BETWEEN -90  AND 90),
    ADD CONSTRAINT ck_hotels_lng          CHECK (longitude BETWEEN -180 AND 180),
    ADD CONSTRAINT ck_hotels_star_type    CHECK (star_type IN ('star', 'diamond')),
    ADD CONSTRAINT ck_hotels_open_year    CHECK (open_year      BETWEEN 1800 AND 2100),
    ADD CONSTRAINT ck_hotels_reno_year    CHECK (renovated_year BETWEEN 1800 AND 2100);

ALTER TABLE hotel_i18n
    ADD CONSTRAINT ck_hotel_i18n_name     CHECK (btrim(name) <> '');

ALTER TABLE cities
    ADD CONSTRAINT ck_cities_lat          CHECK (latitude  BETWEEN -90  AND 90),
    ADD CONSTRAINT ck_cities_lng          CHECK (longitude BETWEEN -180 AND 180);

ALTER TABLE hotel_images
    ADD CONSTRAINT ck_images_url          CHECK (url ~ '^https?://');
ALTER TABLE room_images
    ADD CONSTRAINT ck_room_images_url     CHECK (url ~ '^https?://');

ALTER TABLE room_types
    ADD CONSTRAINT ck_rooms_area          CHECK (area_sqm > 0 AND area_sqm < 10000),
    ADD CONSTRAINT ck_rooms_area_max      CHECK (area_sqm_max IS NULL OR area_sqm_max >= area_sqm),
    ADD CONSTRAINT ck_rooms_adults        CHECK (max_adults    BETWEEN 1 AND 50),
    ADD CONSTRAINT ck_rooms_beds          CHECK (bed_count     BETWEEN 0 AND 50),
    ADD CONSTRAINT ck_rooms_bedrooms      CHECK (bedroom_count BETWEEN 0 AND 50),   -- -1 phải thành NULL
    ADD CONSTRAINT ck_rooms_bathrooms     CHECK (bathroom_count BETWEEN 0 AND 50),
    ADD CONSTRAINT ck_rooms_living        CHECK (living_room_count BETWEEN 0 AND 50),
    ADD CONSTRAINT ck_rooms_extra_bed_px  CHECK (extra_bed_price >= 0);

ALTER TABLE room_type_i18n
    ADD CONSTRAINT ck_room_i18n_name      CHECK (btrim(name) <> '');

ALTER TABLE room_offers
    ADD CONSTRAINT ck_offers_dates        CHECK (check_out > check_in),
    ADD CONSTRAINT ck_offers_adults       CHECK (adults BETWEEN 1 AND 50),
    ADD CONSTRAINT ck_offers_guests       CHECK (max_guests BETWEEN 1 AND 50),
    ADD CONSTRAINT ck_offers_remaining    CHECK (remaining_rooms >= 0 AND remaining_rooms < 9999);  -- 9999 phải thành NULL

ALTER TABLE room_offer_prices
    ADD CONSTRAINT ck_prices_night        CHECK (price_per_night > 0),
    ADD CONSTRAINT ck_prices_total        CHECK (total_price > 0),
    ADD CONSTRAINT ck_prices_tax          CHECK (taxes_fees >= 0),
    ADD CONSTRAINT ck_prices_tax_le_total CHECK (taxes_fees IS NULL OR total_price IS NULL OR taxes_fees <= total_price),
    ADD CONSTRAINT ck_prices_original     CHECK (original_price IS NULL OR original_price > price_per_night);

ALTER TABLE hotel_price_snapshots
    ADD CONSTRAINT ck_snap_dates          CHECK (check_out > check_in),
    ADD CONSTRAINT ck_snap_price          CHECK (min_price > 0),
    ADD CONSTRAINT ck_snap_total          CHECK (min_total > 0);

ALTER TABLE hotel_review_summary
    ADD CONSTRAINT ck_review_scale        CHECK (rating_scale IN (5, 10)),
    ADD CONSTRAINT ck_review_overall      CHECK (rating_overall     BETWEEN 0 AND rating_scale),
    ADD CONSTRAINT ck_review_location     CHECK (rating_location    BETWEEN 0 AND rating_scale),
    ADD CONSTRAINT ck_review_facility     CHECK (rating_facility    BETWEEN 0 AND rating_scale),
    ADD CONSTRAINT ck_review_service      CHECK (rating_service     BETWEEN 0 AND rating_scale),
    ADD CONSTRAINT ck_review_clean        CHECK (rating_cleanliness BETWEEN 0 AND rating_scale),
    ADD CONSTRAINT ck_review_count        CHECK (review_count >= 0);

ALTER TABLE hotel_review_tags
    ADD CONSTRAINT ck_review_tags_count   CHECK (mention_count >= 0);

ALTER TABLE places
    ADD CONSTRAINT ck_places_lat          CHECK (latitude  BETWEEN -90  AND 90),
    ADD CONSTRAINT ck_places_lng          CHECK (longitude BETWEEN -180 AND 180);

ALTER TABLE hotel_nearby_places
    ADD CONSTRAINT ck_nearby_distance     CHECK (distance_km >= 0);


-- =============================================================================
-- D. THEO DÕI KIỂM TRA DỮ LIỆU
-- =============================================================================

-- Mỗi lần chạy loader (kể cả --validate-only) là một dòng.
CREATE TABLE load_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    locale          locale_code NOT NULL,
    currency        currency_code NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('validate_only', 'load')),
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'done', 'gate_failed', 'failed')),
    files_seen      INTEGER,
    hotels_ok       INTEGER,          -- đạt (có thể kèm cảnh báo)
    hotels_rejected INTEGER,          -- bị loại cả khách sạn
    stats           JSONB NOT NULL DEFAULT '{}',   -- tỷ lệ có phòng/ảnh/tiện nghi…, đếm theo quy tắc
    gate_reasons    TEXT[]                          -- lý do chốt chặn cả đợt không cho nạp
);
CREATE INDEX idx_load_runs_locale ON load_runs(locale, started_at DESC);

-- Từng lỗi / cảnh báo. Tự sửa (9999 → NULL…) chỉ đếm trong load_runs.stats.
--   severity = 'error'   → bản ghi (hoặc cả khách sạn nếu entity='hotel') KHÔNG được lưu
--   severity = 'warning' → vẫn lưu, ghi lại để xem
CREATE TABLE load_rejects (
    id             BIGSERIAL PRIMARY KEY,
    run_id         BIGINT NOT NULL REFERENCES load_runs(id) ON DELETE CASCADE,
    trip_hotel_id  BIGINT,
    locale         locale_code,
    layer          SMALLINT NOT NULL CHECK (layer BETWEEN 1 AND 4),
    severity       TEXT NOT NULL CHECK (severity IN ('error', 'warning')),
    rule           TEXT NOT NULL,     -- mã quy tắc, vd 'price_not_positive'
    entity         TEXT NOT NULL,     -- 'hotel' | 'room' | 'offer' | 'image' | …
    entity_key     TEXT,              -- trip_room_id, trip_offer_key…
    field          TEXT,
    value          TEXT,
    message        TEXT NOT NULL,
    raw_path       TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_load_rejects_run   ON load_rejects(run_id, severity, rule);
CREATE INDEX idx_load_rejects_hotel ON load_rejects(trip_hotel_id);
