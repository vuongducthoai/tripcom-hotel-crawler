-- =============================================================================
-- Xuất dữ liệu v2 sang dạng bảng splatform_meta.trip_property_translation
--
--   psql ... -At -f scripts/export_trip_property_translation.sql > dump_translation.sql
--
-- property_id = trip_hotel_id (mã khách sạn của Trip.com).
-- Đổi LIMIT / thêm bộ lọc thành phố ở CTE "sel" bên dưới nếu cần.
-- =============================================================================
WITH sel AS (
    SELECT h.id, h.trip_hotel_id
    FROM v2.hotels h
    JOIN v2.hotel_i18n i ON i.hotel_id = h.id AND i.locale LIKE 'vi%'
    -- JOIN v2.cities c ON c.id = h.city_id AND c.trip_city_id = 495   -- chỉ New Delhi
    WHERE i.description IS NOT NULL
    ORDER BY h.id
    LIMIT 20
),
rows AS (
    -- 1. Mô tả "Về cơ sở lưu trú này"
    SELECT s.trip_hotel_id AS property_id,
           i.locale::text  AS lang,
           'description'   AS filed,
           i.description   AS value,
           1 AS ord, 0 AS sub
    FROM sel s
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.description IS NOT NULL

    UNION ALL
    -- 2. Tiêu đề từng mục chính sách
    SELECT s.trip_hotel_id, ps.locale::text, 'policy_title', ps.title,
           2, ps.sort_order * 100
    FROM sel s
    JOIN v2.hotel_policy_sections ps ON ps.hotel_id = s.id

    UNION ALL
    -- 3. Nội dung từng dòng chính sách (ghép nhãn + nội dung)
    SELECT s.trip_hotel_id, pl.locale::text, 'policy_content',
           CASE WHEN COALESCE(pl.label, '') = '' THEN pl.text
                ELSE pl.label || ' ' || pl.text END,
           2, ps.sort_order * 100 + pl.line_no
    FROM sel s
    JOIN v2.hotel_policy_lines pl ON pl.hotel_id = s.id
    JOIN v2.hotel_policy_sections ps
      ON ps.hotel_id = pl.hotel_id AND ps.locale = pl.locale
     AND ps.section_code = pl.section_code

    UNION ALL
    -- 4. Tên địa điểm xung quanh
    SELECT s.trip_hotel_id, pi.locale::text, 'surrounding_name', pi.name,
           3, COALESCE(np.sort_order, 0) * 10
    FROM sel s
    JOIN v2.hotel_nearby_places np ON np.hotel_id = s.id
    JOIN v2.place_i18n pi ON pi.place_id = np.place_id

    UNION ALL
    -- 5. Khoảng cách, định dạng giống trang Trip.com ("720m", "1,0km")
    SELECT s.trip_hotel_id, pi.locale::text, 'surrounding_distance',
           CASE
               WHEN np.distance_km IS NULL THEN npi.distance_text
               WHEN np.distance_km < 1
                   THEN round(np.distance_km * 1000)::bigint::text || 'm'
               ELSE replace(round(np.distance_km, 1)::text, '.', ',') || 'km'
           END,
           3, COALESCE(np.sort_order, 0) * 10 + 1
    FROM sel s
    JOIN v2.hotel_nearby_places np ON np.hotel_id = s.id
    JOIN v2.place_i18n pi ON pi.place_id = np.place_id
    LEFT JOIN v2.hotel_nearby_place_i18n npi
           ON npi.hotel_id = np.hotel_id AND npi.place_id = np.place_id
          AND npi.locale = pi.locale
)
SELECT 'INSERT INTO splatform_meta.trip_property_translation '
       || '(property_id, lang, filed, value) VALUES ('
       || property_id || ', '
       || quote_literal(lang) || ', '
       || quote_literal(filed) || ', '
       || quote_nullable(value) || ');'
FROM rows
WHERE value IS NOT NULL AND value <> ''
ORDER BY property_id, lang, ord, sub;
