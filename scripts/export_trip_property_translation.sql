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
    -- 1a. Tên khách sạn
    SELECT s.trip_hotel_id AS property_id,
           i.locale::text  AS lang,
           'hotel_name'    AS filed,
           i.name          AS value,
           1 AS ord, 0 AS sub
    FROM sel s
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.name IS NOT NULL

    UNION ALL
    -- 1b. Tên khách sạn địa phương
    SELECT s.trip_hotel_id AS property_id,
           i.locale::text  AS lang,
           'local_name'    AS filed,
           i.local_name    AS value,
           1 AS ord, 1 AS sub
    FROM sel s
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.local_name IS NOT NULL

    UNION ALL
    -- 1c. Năm khai trương
    SELECT s.trip_hotel_id, i.locale::text, 'open_year', h.open_year::text,
           1, 2
    FROM sel s
    JOIN v2.hotels h ON h.id = s.id
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE h.open_year IS NOT NULL

    UNION ALL
    -- 1d. Số phòng
    SELECT s.trip_hotel_id, i.locale::text, 'room_count', h.room_count::text,
           1, 3
    FROM sel s
    JOIN v2.hotels h ON h.id = s.id
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE h.room_count IS NOT NULL

    UNION ALL
    -- 1e. Địa chỉ
    SELECT s.trip_hotel_id, i.locale::text, 'hotel_address', i.address,
           1, 4
    FROM sel s
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.address IS NOT NULL

    UNION ALL
    -- 1f. Mô tả "Về cơ sở lưu trú này"
    SELECT s.trip_hotel_id AS property_id,
           i.locale::text  AS lang,
           'description'   AS filed,
           i.description   AS value,
           1 AS ord, 5 AS sub
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
    -- 3. Nội dung chính sách — gom cả mục thành MỘT dòng HTML
    --      dòng có nhãn   → <p><strong>Nhận phòng:</strong> 15:00</p>
    --      dòng không nhãn → gom chung vào <ul><li>…</li></ul>
    --    Ký tự & < > được escape để không vỡ HTML.
    SELECT s.trip_hotel_id, pl.locale::text, 'policy_content',
           COALESCE(string_agg(
               CASE WHEN COALESCE(pl.label, '') <> '' THEN
                   '<p><strong>'
                   || replace(replace(replace(pl.label, '&', '&amp;'), '<', '&lt;'), '>', '&gt;')
                   || '</strong> '
                   || replace(replace(replace(pl.text, '&', '&amp;'), '<', '&lt;'), '>', '&gt;')
                   || '</p>'
               END, '' ORDER BY pl.line_no), '')
           || CASE WHEN count(*) FILTER (WHERE COALESCE(pl.label, '') = '') > 0 THEN
                  '<ul>' || string_agg(
                      CASE WHEN COALESCE(pl.label, '') = '' THEN
                          '<li>'
                          || replace(replace(replace(pl.text, '&', '&amp;'), '<', '&lt;'), '>', '&gt;')
                          || '</li>'
                      END, '' ORDER BY pl.line_no) || '</ul>'
              ELSE '' END,
           2, min(ps.sort_order) * 100 + 1
    FROM sel s
    JOIN v2.hotel_policy_lines pl ON pl.hotel_id = s.id
    JOIN v2.hotel_policy_sections ps
      ON ps.hotel_id = pl.hotel_id AND ps.locale = pl.locale
     AND ps.section_code = pl.section_code
    GROUP BY s.trip_hotel_id, pl.locale, pl.section_code

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

    UNION ALL
    -- 6. Vĩ độ địa điểm xung quanh
    SELECT s.trip_hotel_id, pi.locale::text, 'surrounding_lat', pl.latitude::text,
           3, COALESCE(np.sort_order, 0) * 10 + 2
    FROM sel s
    JOIN v2.hotel_nearby_places np ON np.hotel_id = s.id
    JOIN v2.places pl ON pl.id = np.place_id
    JOIN v2.place_i18n pi ON pi.place_id = np.place_id
    WHERE pl.latitude IS NOT NULL

    UNION ALL
    -- 7. Kinh độ địa điểm xung quanh
    SELECT s.trip_hotel_id, pi.locale::text, 'surrounding_lng', pl.longitude::text,
           3, COALESCE(np.sort_order, 0) * 10 + 3
    FROM sel s
    JOIN v2.hotel_nearby_places np ON np.hotel_id = s.id
    JOIN v2.places pl ON pl.id = np.place_id
    JOIN v2.place_i18n pi ON pi.place_id = np.place_id
    WHERE pl.longitude IS NOT NULL
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
