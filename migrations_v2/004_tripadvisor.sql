-- =============================================================================
-- Schema v2 — bản bổ sung 004 (chạy SAU 003): liên kết khách sạn với Tripadvisor
--
-- Mỗi khách sạn v2 một dòng: location_id của Tripadvisor, điểm (thang 5),
-- số đánh giá và link trang Tripadvisor. Lấy bằng Tripadvisor Content API
-- chính thức (scripts/tripadvisor_match.py) — không cào web Tripadvisor.
--
-- Lưu ý điều khoản Content API: dữ liệu dùng để HIỂN THỊ (kèm logo/link về
-- Tripadvisor), việc lưu lâu dài theo Caching Policy của Tripadvisor —
-- nên có refreshed_at để làm mới định kỳ.
-- =============================================================================
SET search_path TO v2;

CREATE TABLE hotel_tripadvisor (
    hotel_id                 BIGINT PRIMARY KEY REFERENCES hotels(id) ON DELETE CASCADE,
    trip_hotel_id            BIGINT NOT NULL,
    tripadvisor_location_id  BIGINT,
    match_status             TEXT NOT NULL
                             CHECK (match_status IN ('matched', 'review', 'no_match', 'error')),
    -- matched  : tên giống + gần nhau → tin được
    -- review   : có ứng viên nhưng chưa chắc (tên hơi khác / hơi xa) → người xem lại
    -- no_match : Tripadvisor không có khách sạn nào phù hợp
    -- error    : gọi API lỗi (hết lượt, sai key…) → chạy lại sau
    name_similarity          NUMERIC(4,3) CHECK (name_similarity BETWEEN 0 AND 1),
    distance_m               INTEGER CHECK (distance_m >= 0),
    tripadvisor_name         TEXT,
    rating                   NUMERIC(2,1) CHECK (rating BETWEEN 0 AND 5),
    review_count             INTEGER CHECK (review_count >= 0),
    tripadvisor_url          TEXT CHECK (tripadvisor_url ~ '^https://([a-z0-9-]+\.)*tripadvisor\.[a-z.]+/'),
    searched_at              TIMESTAMPTZ,       -- lần tìm location_id
    refreshed_at             TIMESTAMPTZ,       -- lần lấy rating/review_count gần nhất
    last_error               TEXT,
    CHECK (match_status NOT IN ('matched', 'review') OR tripadvisor_location_id IS NOT NULL)
);
CREATE INDEX idx_hotel_tripadvisor_loc ON hotel_tripadvisor(tripadvisor_location_id);
CREATE INDEX idx_hotel_tripadvisor_status ON hotel_tripadvisor(match_status);
