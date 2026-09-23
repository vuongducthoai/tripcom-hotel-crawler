-- =============================================================================
-- Schema v2 — bản bổ sung 005 (chạy SAU 004): số phòng của khách sạn
--
-- Trip.com trả ở hotelDescriptionInfo.lables, dạng ["Khai Trương: 2006",
-- "Tân Trang: 2025", "Số Phòng: 198"] — khối "Về cơ sở lưu trú này".
-- Năm khai trương/tân trang đã có sẵn ở open_year/renovated_year, nên ở đây
-- chỉ bổ sung số phòng.
-- =============================================================================
SET search_path TO v2;

ALTER TABLE hotels ADD COLUMN IF NOT EXISTS room_count SMALLINT;

ALTER TABLE hotels DROP CONSTRAINT IF EXISTS ck_hotels_room_count;
ALTER TABLE hotels ADD CONSTRAINT ck_hotels_room_count
    CHECK (room_count IS NULL OR room_count BETWEEN 1 AND 10000);

COMMENT ON COLUMN hotels.room_count IS
    'Tổng số phòng của khách sạn — hotelDescriptionInfo.lables "Số Phòng: N"';
