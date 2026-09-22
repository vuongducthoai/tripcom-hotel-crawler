-- =============================================================================
-- Schema v2 — dữ liệu khách sạn Trip.com, song ngữ (vi + en), đa quốc gia.
--
-- Nằm trong schema PostgreSQL riêng "v2": KHÔNG đụng bảng cũ ở "public".
-- Dữ liệu TP.HCM cũ giữ nguyên để tham khảo.
--
-- Quy ước:
--   * Bảng lõi (không hậu tố) chứa dữ liệu KHÔNG phụ thuộc ngôn ngữ: số, mã,
--     cờ, tọa độ, giá.
--   * Bảng *_i18n chứa chữ hiển thị, khóa theo (…, locale). locale là 'vi'
--     hoặc 'en' hôm nay, nhưng không khóa cứng — thêm 'ja', 'th' không cần
--     sửa schema.
--   * Mọi id của Trip.com giữ nguyên ở cột trip_*_id để đối chiếu với web.
--   * Mỗi khối ứng với một phần trên trang chi tiết Trip.com (ghi ở đầu khối).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS v2;
SET search_path TO v2;

CREATE DOMAIN locale_code AS TEXT CHECK (VALUE ~ '^[a-z]{2}(-[A-Z]{2})?$');
CREATE DOMAIN currency_code AS TEXT CHECK (VALUE ~ '^[A-Z]{3}$');

-- Ba trạng thái cho các câu hỏi "có cho phép không?"
CREATE TYPE allow_state AS ENUM ('yes', 'no', 'on_request', 'unknown');


-- =============================================================================
-- 1. ĐỊA LÝ  (breadcrumb: Vietnam > Ho Chi Minh City > Bach Dang Riverside)
-- =============================================================================
CREATE TABLE countries (
    id               BIGSERIAL PRIMARY KEY,
    trip_country_id  INTEGER NOT NULL UNIQUE,          -- hotelBaseInfo.countryId (VN = 111)
    iso2             CHAR(2)                            -- 'VN', 'TH', 'JP'… (điền tay/tra bảng)
);

CREATE TABLE country_i18n (
    country_id  BIGINT NOT NULL REFERENCES countries(id) ON DELETE CASCADE,
    locale      locale_code NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (country_id, locale)
);

CREATE TABLE cities (
    id                BIGSERIAL PRIMARY KEY,
    trip_city_id      INTEGER NOT NULL UNIQUE,         -- hotelBaseInfo.cityId (HCM = 301)
    country_id        BIGINT NOT NULL REFERENCES countries(id),
    trip_province_id  INTEGER,                         -- hotelBaseInfo.provinceId (0 = không có)
    latitude          DOUBLE PRECISION,
    longitude         DOUBLE PRECISION,
    utc_offset_sec    INTEGER                          -- hotelBaseInfo.timeOffset (25200 = +07:00)
);
CREATE TABLE city_i18n (
    city_id        BIGINT NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
    locale         locale_code NOT NULL,
    name           TEXT NOT NULL,
    province_name  TEXT,
    PRIMARY KEY (city_id, locale)
);


-- =============================================================================
-- 2. KHÁCH SẠN — phần đầu trang: tên, sao, huy hiệu, địa chỉ, bản đồ
-- =============================================================================
CREATE TABLE hotels (
    id               BIGSERIAL PRIMARY KEY,
    trip_hotel_id    BIGINT NOT NULL UNIQUE,           -- masterHotelId
    city_id          BIGINT REFERENCES cities(id),
    star_level       SMALLINT CHECK (star_level BETWEEN 0 AND 5),   -- starInfo.level
    star_type        TEXT,                             -- 'star' | 'diamond' (tự xếp hạng)
    is_super_star    BOOLEAN,
    medal_type       SMALLINT,                         -- medalInfo.type
    open_year        SMALLINT,                         -- openYear
    renovated_year   SMALLINT,                         -- fitmentYear
    latitude         DOUBLE PRECISION,
    longitude        DOUBLE PRECISION,
    is_private_host  BOOLEAN,                          -- policy.privateHostInfo có mặt
    detail_url       TEXT,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_hotels_city ON hotels(city_id);
CREATE INDEX idx_hotels_star ON hotels(star_level);

CREATE TABLE hotel_i18n (
    hotel_id            BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    locale              locale_code NOT NULL,
    name                TEXT NOT NULL,                 -- nameInfo.name (tên hiển thị theo ngôn ngữ)
    local_name          TEXT,                          -- nameInfo.localNameTip, bỏ tiền tố "Local hotel name:"
    address             TEXT,                          -- hotelPositionInfo.address
    zone_name           TEXT,                          -- hotelPositionInfo.zoneName (khu vực)
    traffic_desc        TEXT,                          -- trafficInfo.trafficDesc ("cách ga … 15 phút đi bộ")
    hotel_type          TEXT,                          -- "Khách sạn", "Căn hộ"…
    description         TEXT,                          -- hotelDescriptionInfo.sectionList[].desc ghép lại
    description_source  TEXT CHECK (description_source IN ('intro', 'meta', 'json_ld')),
    last_booked_text    TEXT,                          -- "Last booked 1 hr ago" (chỉ để hiển thị)
    PRIMARY KEY (hotel_id, locale)
);

-- "Highlights" dưới tên khách sạn (Dịch vụ tuyệt vời, Cho thuê xe…)
CREATE TABLE hotel_highlights (
    hotel_id     BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    locale       locale_code NOT NULL,
    sort_order   SMALLINT NOT NULL,
    trip_tag_id  TEXT,
    title        TEXT NOT NULL,
    description  TEXT,
    icon_url     TEXT,
    PRIMARY KEY (hotel_id, locale, sort_order)
);


-- =============================================================================
-- 3. ẢNH — album: tab "Featured / Exterior / Rooms…", ảnh khách sạn + ảnh khách
-- =============================================================================
CREATE TABLE image_categories (
    trip_category_id  INTEGER PRIMARY KEY              -- imgTabs[].categoryId (-1 = Featured)
);
CREATE TABLE image_category_i18n (
    trip_category_id  INTEGER NOT NULL REFERENCES image_categories ON DELETE CASCADE,
    locale            locale_code NOT NULL,
    name              TEXT NOT NULL,
    PRIMARY KEY (trip_category_id, locale)
);

CREATE TABLE hotel_images (
    id                BIGSERIAL PRIMARY KEY,
    hotel_id          BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    trip_picture_id   BIGINT,
    url               TEXT NOT NULL,                   -- ảnh gốc, không watermark
    uploader          TEXT NOT NULL CHECK (uploader IN ('hotel', 'guest')),
    trip_category_id  INTEGER REFERENCES image_categories,
    sort_order        INTEGER,
    is_cover          BOOLEAN NOT NULL DEFAULT FALSE,  -- nằm trong hotelTopImage (ảnh đầu trang)
    UNIQUE (hotel_id, url)
);
CREATE INDEX idx_hotel_images_hotel ON hotel_images(hotel_id, trip_category_id, sort_order);


-- =============================================================================
-- 4. TIỆN NGHI — "Services & Amenities": danh mục chung theo mã Trip.com
--    (cùng mã = cùng tiện nghi ở mọi ngôn ngữ, không nhận diện theo tên nữa)
-- =============================================================================
CREATE TABLE amenity_categories (
    trip_category_id  INTEGER PRIMARY KEY              -- category_code (2 = Internet, 4 = Parking…)
);
CREATE TABLE amenity_category_i18n (
    trip_category_id  INTEGER NOT NULL REFERENCES amenity_categories ON DELETE CASCADE,
    locale            locale_code NOT NULL,
    name              TEXT NOT NULL,
    PRIMARY KEY (trip_category_id, locale)
);

CREATE TABLE amenities (
    trip_amenity_id   INTEGER PRIMARY KEY,             -- facility id (102 = Wi-Fi khu công cộng)
    trip_category_id  INTEGER REFERENCES amenity_categories,
    icon              TEXT
);
CREATE TABLE amenity_i18n (
    trip_amenity_id  INTEGER NOT NULL REFERENCES amenities ON DELETE CASCADE,
    locale           locale_code NOT NULL,
    name             TEXT NOT NULL,
    PRIMARY KEY (trip_amenity_id, locale)
);

CREATE TABLE hotel_amenities (
    hotel_id         BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    trip_amenity_id  INTEGER NOT NULL REFERENCES amenities,
    is_available     BOOLEAN NOT NULL DEFAULT TRUE,    -- FALSE = trang ghi rõ "không có"
    is_popular       BOOLEAN NOT NULL DEFAULT FALSE,   -- nằm trong "Most popular amenities"
    fee              TEXT CHECK (fee IN ('free', 'paid', 'unknown')),
    PRIMARY KEY (hotel_id, trip_amenity_id)
);
-- Chi tiết kèm theo (Loại: bãi riêng · Vị trí: trong khuôn viên · Đặt trước: không cần)
CREATE TABLE hotel_amenity_details (
    hotel_id         BIGINT NOT NULL,
    trip_amenity_id  INTEGER NOT NULL,
    locale           locale_code NOT NULL,
    fee_label        TEXT,                             -- "Miễn phí", "Tính phí"
    details          JSONB NOT NULL DEFAULT '[]',      -- [{"title":"Type","text":["Private…"]}, …]
    PRIMARY KEY (hotel_id, trip_amenity_id, locale),
    FOREIGN KEY (hotel_id, trip_amenity_id)
        REFERENCES hotel_amenities(hotel_id, trip_amenity_id) ON DELETE CASCADE
);


-- =============================================================================
-- 5. CHÍNH SÁCH — lấy từ hotelDetailResponse.hotelPolicyInfo (có cấu trúc),
--    KHÔNG copy chữ từ popup nữa.
--    Hai tầng:
--      hotel_policies       : giá trị đã chuẩn hóa để LỌC/TRUY VẤN
--      hotel_policy_lines   : từng dòng đúng như Trip.com hiển thị
-- =============================================================================
CREATE TABLE hotel_policies (
    hotel_id                BIGINT PRIMARY KEY REFERENCES hotels(id) ON DELETE CASCADE,
    checkin_from            TIME,                      -- "After 14:00"
    checkin_until           TIME,                      -- "15:00-23:00" → 23:00
    checkout_until          TIME,                      -- "Before 12:00"
    front_desk_24h          BOOLEAN,
    min_checkin_age         SMALLINT,                  -- ageLimit: "at least 18 years old"
    children_allowed        allow_state,               -- childPolicy
    child_min_age           SMALLINT,                  -- "Children aged 5 and above…"
    child_free_max_age      SMALLINT,                  -- "0–5 tuổi ở miễn phí nếu không thêm giường"
    extra_bed               allow_state,               -- cribAndExtraBed
    crib                    allow_state,
    breakfast_available     BOOLEAN,                   -- breakfast
    deposit_required        BOOLEAN,                   -- deposit
    pets                    allow_state,               -- pet
    service_animals         allow_state,               -- serviceAnimal
    quiet_hours_from        TIME,
    quiet_hours_until       TIME,
    payment_methods         TEXT[],                    -- credit: ['cash','visa','mastercard'…]
    parsed_from_locale      locale_code,               -- chuẩn hóa từ bản 'en' (ổn định nhất)
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_policies_pets ON hotel_policies(pets);

-- Mục chính sách: checkInAndOut, childPolicy, cribAndExtraBed, breakfast,
-- deposit, pet, serviceAnimal, ageLimit, credit, privateHostInfo,
-- reservationTip (getDetailAdditionalInfo), quietHours …
CREATE TABLE hotel_policy_sections (
    hotel_id      BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    locale        locale_code NOT NULL,
    section_code  TEXT NOT NULL,
    sort_order    SMALLINT NOT NULL,
    title         TEXT NOT NULL,                       -- "Thời gian nhận và trả phòng"
    PRIMARY KEY (hotel_id, locale, section_code)
);
CREATE TABLE hotel_policy_lines (
    hotel_id      BIGINT NOT NULL,
    locale        locale_code NOT NULL,
    section_code  TEXT NOT NULL,
    line_no       SMALLINT NOT NULL,
    label         TEXT,                                -- "Nhận phòng:" (có thể trống)
    text          TEXT NOT NULL,                       -- "Sau 14:00"
    PRIMARY KEY (hotel_id, locale, section_code, line_no),
    FOREIGN KEY (hotel_id, locale, section_code)
        REFERENCES hotel_policy_sections(hotel_id, locale, section_code) ON DELETE CASCADE
);


-- =============================================================================
-- 6. LOẠI PHÒNG — mỗi "thẻ phòng" trên Trip.com (physicRoomMap + roomPopInfo)
-- =============================================================================
CREATE TABLE room_types (
    id                 BIGSERIAL PRIMARY KEY,
    hotel_id           BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    trip_room_id       BIGINT NOT NULL,                -- physicRoomMap key / id
    area_sqm           NUMERIC(8,2),                   -- đổi từ "839 ft²" hoặc "78 m²"
    max_adults         SMALLINT,                       -- roomBasicInfo.guestInfo "4 adults"
    bed_count          SMALLINT,                       -- houseTypeInfo.bedCount
    bedroom_count      SMALLINT,                       -- -1 của Trip.com → NULL
    bathroom_count     SMALLINT,
    living_room_count  SMALLINT,
    window_type        SMALLINT,                       -- windowInfo.type (0 = không cửa sổ)
    smoking            TEXT CHECK (smoking IN ('non_smoking', 'smoking', 'partial', 'unknown')),
    wifi               TEXT CHECK (wifi IN ('free', 'paid', 'none', 'unknown')),
    extra_bed          allow_state,
    extra_bed_price    NUMERIC(12,2),
    extra_bed_currency currency_code,
    sort_order         SMALLINT,                       -- physicRank
    UNIQUE (hotel_id, trip_room_id)
);

CREATE TABLE room_type_i18n (
    room_type_id     BIGINT NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    locale           locale_code NOT NULL,
    name             TEXT NOT NULL,                    -- "Suite 2 phòng ngủ hướng phố"
    bed_summary      TEXT,                             -- "2 giường queen"
    bed_details      JSONB NOT NULL DEFAULT '[]',      -- [{"room":"Phòng ngủ 1","beds":["2 giường queen (rộng 1,8m)"]}]
    area_text        TEXT,                             -- giữ chữ gốc "839 ft²"
    view_text        TEXT,                             -- "Hướng phố"
    guest_text       TEXT,                             -- "4 người lớn"
    extra_bed_text   TEXT,
    child_policy     TEXT,                             -- policyInfo.childPolicy riêng của phòng
    PRIMARY KEY (room_type_id, locale)
);

CREATE TABLE room_images (
    room_type_id  BIGINT NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    sort_order    SMALLINT,
    PRIMARY KEY (room_type_id, url)
);

-- Tiện nghi phòng dùng chung danh mục amenities (cùng không gian mã facility)
CREATE TABLE room_amenities (
    room_type_id     BIGINT NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    trip_amenity_id  INTEGER NOT NULL REFERENCES amenities,
    is_highlight     BOOLEAN NOT NULL DEFAULT FALSE,   -- nằm trong topPopularFacility
    fee              TEXT CHECK (fee IN ('free', 'paid', 'unknown')),
    PRIMARY KEY (room_type_id, trip_amenity_id)
);


-- =============================================================================
-- 7. GÓI GIÁ — mỗi dòng "Room only / Có bữa sáng · Hủy miễn phí · $370" trong
--    thẻ phòng (saleRoomMap). Một loại phòng có nhiều gói.
--    Giá theo (ngày ở, tiền tệ, ngày lấy) → giữ được lịch sử giá.
-- =============================================================================
CREATE TABLE room_offers (
    id                 BIGSERIAL PRIMARY KEY,
    room_type_id       BIGINT NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    -- Khóa của saleRoomMap = id + "_" + roomCode. Cùng một id có thể là HAI
    -- gói khác nhau (vd: 1 khách + 1 bữa sáng / 2 khách + 2 bữa sáng), nên
    -- phải dùng cả khóa. Khóa này trùng nhau giữa lượt vi và en nếu cào
    -- cùng ngày nhận phòng → ghép được giá VND + USD và chữ vi + en.
    trip_offer_key     TEXT NOT NULL,                  -- "1557159472_ORC9WY-1S-9-B"
    trip_sale_room_id  BIGINT NOT NULL,                -- saleRoomMap[].id
    room_code          TEXT,                           -- roomCode "ORC9WY-1S-9-B"
    check_in           DATE NOT NULL,
    check_out          DATE NOT NULL,
    adults             SMALLINT NOT NULL DEFAULT 2,
    captured_date      DATE NOT NULL,
    -- điều kiện gói (không phụ thuộc ngôn ngữ/tiền tệ)
    meal_type          SMALLINT,                       -- mealInfo.mealType (0 = không bữa ăn)
    breakfast_included BOOLEAN,
    cancel_type        SMALLINT,                       -- cancelInfo.type (5 = không hoàn tiền)
    free_cancellation  BOOLEAN,
    payment_type       TEXT CHECK (payment_type IN ('prepay', 'pay_at_hotel', 'unknown')),
    instant_confirm    BOOLEAN,
    max_guests         SMALLINT,                       -- guestCountInfo.guestCount
    remaining_rooms    SMALLINT,                       -- remainRoomQuantity; 9999 = "còn nhiều" → NULL
    is_sold_out        BOOLEAN NOT NULL DEFAULT FALSE,
    is_lowest_price    BOOLEAN NOT NULL DEFAULT FALSE, -- isStartPriceRoom
    UNIQUE (trip_offer_key, check_in, check_out, adults, captured_date)
);
CREATE INDEX idx_offers_room ON room_offers(room_type_id, captured_date DESC);

-- Giá của gói theo từng tiền tệ (VND từ lượt vi, USD từ lượt en)
CREATE TABLE room_offer_prices (
    offer_id        BIGINT NOT NULL REFERENCES room_offers(id) ON DELETE CASCADE,
    currency        currency_code NOT NULL,
    price_per_night NUMERIC(14,2),                     -- priceInfo.price
    total_price     NUMERIC(14,2),                     -- totalPriceInfo.total (gồm thuế & phí)
    taxes_fees      NUMERIC(14,2),                     -- totalPriceInfo.payTax.price
    original_price  NUMERIC(14,2),                     -- deletePrice (giá gạch), nếu có
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (offer_id, currency)
);

-- Chữ hiển thị của gói
CREATE TABLE room_offer_i18n (
    offer_id       BIGINT NOT NULL REFERENCES room_offers(id) ON DELETE CASCADE,
    locale         locale_code NOT NULL,
    title          TEXT,                               -- titleInfo.title "Room only"
    meal_text      TEXT,                               -- "Bữa sáng $10.38 (tùy chọn)"
    cancel_title   TEXT,                               -- "Không hoàn tiền" / "Hủy miễn phí trước …"
    cancel_detail  TEXT,
    payment_text   TEXT,                               -- "Thanh toán trực tuyến"
    confirm_text   TEXT,                               -- "Xác nhận trong 12 giờ"
    PRIMARY KEY (offer_id, locale)
);

-- Giá thấp nhất cấp khách sạn (thẻ ở trang danh sách, JSON-LD "From $102").
-- Luôn có kể cả khi không lấy được danh sách phòng.
CREATE TABLE hotel_price_snapshots (
    hotel_id       BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    check_in       DATE NOT NULL,
    check_out      DATE NOT NULL,
    currency       currency_code NOT NULL,
    captured_date  DATE NOT NULL,
    min_price      NUMERIC(14,2),
    min_total      NUMERIC(14,2),                      -- gồm thuế
    source         TEXT NOT NULL CHECK (source IN ('list', 'detail', 'json_ld')),
    PRIMARY KEY (hotel_id, check_in, check_out, currency, captured_date, source)
);


-- =============================================================================
-- 8. ĐÁNH GIÁ (tóm tắt) — khung điểm: 9.0 Great · Vị trí 9.4 · Tiện nghi 8.7…
-- =============================================================================
CREATE TABLE hotel_review_summary (
    hotel_id          BIGINT PRIMARY KEY REFERENCES hotels(id) ON DELETE CASCADE,
    rating_overall    NUMERIC(3,1),                    -- commentRating.ratingAll
    rating_location   NUMERIC(3,1),
    rating_facility   NUMERIC(3,1),
    rating_service    NUMERIC(3,1),
    rating_cleanliness NUMERIC(3,1),                   -- ratingRoom ("Cleanliness")
    review_count      INTEGER,                         -- totalCount
    rating_scale      SMALLINT DEFAULT 10,
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE hotel_review_summary_i18n (
    hotel_id      BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    locale        locale_code NOT NULL,
    level_text    TEXT,                                -- "Great" / "Tuyệt vời"
    ai_summary    JSONB,                               -- aiSummaryEntities.summaryItemList
    PRIMARY KEY (hotel_id, locale)
);
-- Nhãn "Phòng rộng (107)", "Hồ bơi tuyệt (38)", "Điều hòa kém (5)"
CREATE TABLE hotel_review_tags (
    hotel_id      BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    locale        locale_code NOT NULL,
    trip_tag_id   INTEGER NOT NULL,
    name          TEXT NOT NULL,
    mention_count INTEGER,
    sentiment     TEXT CHECK (sentiment IN ('positive', 'negative', 'neutral')),
    PRIMARY KEY (hotel_id, locale, trip_tag_id)
);


-- =============================================================================
-- 9. VỊ TRÍ LÂN CẬN — "Around the hotel": giao thông, điểm tham quan, ăn uống…
--    Một địa điểm dùng chung cho nhiều khách sạn (trước đây bị nhân bản).
-- =============================================================================
CREATE TABLE places (
    id           BIGSERIAL PRIMARY KEY,
    trip_poi_id  BIGINT NOT NULL UNIQUE,
    poi_type     SMALLINT,
    latitude     DOUBLE PRECISION,
    longitude    DOUBLE PRECISION
);
CREATE TABLE place_i18n (
    place_id  BIGINT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    locale    locale_code NOT NULL,
    name      TEXT NOT NULL,                           -- "Ba Son"
    kind      TEXT,                                    -- tagNames[0] "Ga tàu điện"
    PRIMARY KEY (place_id, locale)
);

CREATE TABLE hotel_nearby_places (
    hotel_id       BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    place_id       BIGINT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    group_code     SMALLINT NOT NULL,                  -- placeInfoList[].id (2 = Transport)
    distance_km    NUMERIC(8,3),
    travel_mode    TEXT CHECK (travel_mode IN ('walk', 'drive', 'unknown')),
    sort_order     SMALLINT,
    PRIMARY KEY (hotel_id, place_id)
);
CREATE TABLE hotel_nearby_place_i18n (
    hotel_id       BIGINT NOT NULL,
    place_id       BIGINT NOT NULL,
    locale         locale_code NOT NULL,
    group_name     TEXT,                               -- "Giao thông"
    distance_text  TEXT,                               -- "Khoảng 9 phút đi bộ (600 m)"
    PRIMARY KEY (hotel_id, place_id, locale),
    FOREIGN KEY (hotel_id, place_id)
        REFERENCES hotel_nearby_places(hotel_id, place_id) ON DELETE CASCADE
);


-- =============================================================================
-- 10. THEO DÕI CÀO — mỗi lần cào một khách sạn ở một ngôn ngữ
-- =============================================================================
CREATE TABLE hotel_crawls (
    id              BIGSERIAL PRIMARY KEY,
    hotel_id        BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    locale          locale_code NOT NULL,
    currency        currency_code NOT NULL,
    crawled_at      TIMESTAMPTZ NOT NULL,
    parser_version  INTEGER,
    raw_path        TEXT,                              -- đường dẫn file raw để đối chiếu
    -- mục nào lấy được: {"base":true,"policies":true,"rooms":false,"blocked":"4030"}
    sections        JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_crawls_hotel ON hotel_crawls(hotel_id, locale, crawled_at DESC);
