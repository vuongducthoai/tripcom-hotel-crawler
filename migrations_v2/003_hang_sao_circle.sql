-- =============================================================================
-- Schema v2 — bản bổ sung 003 (chạy SAU 002)
--
-- Trip.com có 3 kiểu hạng: 'star' (sao chính thức), 'diamond' và 'circle'
-- (Trip.com tự xếp hạng khi khách sạn chưa có sao chính thức). 002 chỉ cho
-- 'star' | 'diamond' → hotel kiểu 'circle' bị để trống star_type.
-- Gặp thật ở hotel 134013415 (starInfo.type = "circle", level 3).
-- =============================================================================
SET search_path TO v2;

ALTER TABLE hotels DROP CONSTRAINT ck_hotels_star_type;
ALTER TABLE hotels ADD CONSTRAINT ck_hotels_star_type
    CHECK (star_type IN ('star', 'diamond', 'circle'));

COMMENT ON COLUMN hotels.star_type IS
    'star = sao chính thức; diamond/circle = Trip.com tự xếp hạng (hiển thị kim cương / vòng tròn)';
