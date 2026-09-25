-- =============================================================================
-- Schema v2 — bản bổ sung 006 (chạy SAU 005):
-- mã nhóm địa điểm lân cận phải lưu THEO TỪNG NGÔN NGỮ
--
-- LÝ DO
-- API ctGetNearbyPlaceInfo của Trip.com trả về placeInfoList[].id là mã nhóm.
-- Tụi mình từng cho rằng mã này giống nhau ở mọi ngôn ngữ nên để chung trên
-- hotel_nearby_places. Kiểm chứng trên khách sạn 118050925 (Copenhagen) cho
-- thấy KHÔNG phải:
--
--     EN:  2 Transport   3 Landmarks      4 Dining   5 Shopping
--     VI:  2 Giao thông  3 Điểm nổi bật              5 Mua Sắm
--
-- Nhóm 4 (Dining) chỉ bản tiếng Anh mới có. Với cột dùng chung, lần crawl sau
-- sẽ ghi đè mã nhóm của lần crawl trước (ON CONFLICT ... DO UPDATE trong
-- writer._write_nearby), làm hỏng dữ liệu ngôn ngữ đã crawl trước đó.
--
-- THAY ĐỔI
-- Thêm hotel_nearby_place_i18n.group_code — mỗi ngôn ngữ giữ mã nhóm riêng.
-- Cột hotel_nearby_places.group_code được GIỮ LẠI để không làm vỡ code cũ,
-- nhưng từ nay chỉ còn là giá trị dự phòng; nguồn đúng là cột mới.
--
-- An toàn khi chạy lại: chỉ thêm cột và nạp dữ liệu cho ô còn trống.
-- =============================================================================
SET search_path TO v2;

ALTER TABLE hotel_nearby_place_i18n
    ADD COLUMN IF NOT EXISTS group_code SMALLINT;

-- Nạp lại từ cột dùng chung cho dữ liệu đã crawl (hiện chỉ có tiếng Việt,
-- nên giá trị đang lưu chính là mã nhóm của bản tiếng Việt).
UPDATE hotel_nearby_place_i18n npi
   SET group_code = np.group_code
  FROM hotel_nearby_places np
 WHERE np.hotel_id = npi.hotel_id
   AND np.place_id = npi.place_id
   AND npi.group_code IS NULL;

COMMENT ON COLUMN hotel_nearby_place_i18n.group_code IS
    'placeInfoList[].id theo từng ngôn ngữ — 2 Transport, 3 Landmark, '
    '4 Dining (chỉ EN), 5 Shopping. Trip.com xếp nhóm khác nhau tuỳ ngôn ngữ '
    'nên mã này bắt buộc phải theo locale.';

COMMENT ON COLUMN hotel_nearby_places.group_code IS
    'CŨ — mã nhóm dùng chung cho mọi ngôn ngữ. Giữ lại làm giá trị dự phòng; '
    'dùng hotel_nearby_place_i18n.group_code thay thế.';
