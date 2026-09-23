-- =============================================================================
-- Xuất dữ liệu v2 sang bảng splatform_meta.trip_property_translation (bản 2)
--
--   psql ... -At -f scripts/export_trip_property_translation.sql > dump.sql
--
-- Cấu trúc mới: mỗi bản ghi logic là một row_uuid, gom nhiều dòng field/value.
--   type = DESCRIPTION  → 1 row_uuid / khách sạn
--   type = POLICY       → 1 row_uuid / mục chính sách
--   type = SURROUNDING  → 1 row_uuid / địa điểm gần đây
--
-- row_uuid sinh bằng md5 từ khoá nghiệp vụ nên KHÔNG đổi giữa các lần xuất —
-- chạy lại vẫn ra đúng uuid cũ, tiện cho việc nạp lại hoặc đối chiếu.
--
-- Đổi LIMIT / bỏ chú thích dòng lọc thành phố ở CTE "sel" nếu cần.
-- =============================================================================
WITH sel AS (
    SELECT h.id, h.trip_hotel_id, h.room_count, h.city_id
    FROM v2.hotels h
    JOIN v2.hotel_i18n i ON i.hotel_id = h.id AND i.locale LIKE 'vi%'
    -- JOIN v2.cities c ON c.id = h.city_id AND c.trip_city_id = 495   -- chỉ New Delhi
    WHERE i.description IS NOT NULL
    ORDER BY h.id
    LIMIT 10
),

-- Mã quốc gia ISO 3166-1 alpha-2 theo countryId của Trip.com.
-- Thêm dòng mới ở đây khi mở rộng sang nước khác.
ma_quoc_gia (trip_country_id, iso2) AS (VALUES
    (111, 'VN'), (27, 'DK'), (107, 'IN')
),

-- Địa điểm gần đây đã lọc rác: Trip.com thỉnh thoảng trả tên placeholder
-- kiểu "size?" — bỏ cả địa điểm đó chứ không lưu nửa vời.
dia_diem AS (
    SELECT s.trip_hotel_id, pi.locale, np.place_id, np.sort_order,
           pi.name AS ten, np.distance_km, pl.latitude, pl.longitude,
           npi.group_name
    FROM sel s
    JOIN v2.hotel_nearby_places np ON np.hotel_id = s.id
    JOIN v2.places      pl ON pl.id = np.place_id
    JOIN v2.place_i18n  pi ON pi.place_id = pl.id
    LEFT JOIN v2.hotel_nearby_place_i18n npi
           ON npi.hotel_id = np.hotel_id AND npi.place_id = np.place_id
          AND npi.locale = pi.locale
    WHERE pi.name IS NOT NULL
      AND btrim(pi.name) <> ''
      AND btrim(pi.name) !~* '^(size\?|n/?a|null|-+|\?+)$'
),

rows AS (
    -- ------------------------------------------------ DESCRIPTION
    SELECT md5(s.trip_hotel_id || ':DESCRIPTION:' || i.locale)::uuid AS row_uuid,
           s.trip_hotel_id AS property_id,
           'DESCRIPTION'   AS type,
           i.locale::text  AS lang,
           'hotel_name'    AS field,
           i.name          AS value,
           1 AS ord, 1 AS sub
    FROM sel s JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.name IS NOT NULL

    UNION ALL
    SELECT md5(s.trip_hotel_id || ':DESCRIPTION:' || i.locale)::uuid, s.trip_hotel_id,
           'DESCRIPTION', i.locale::text, 'local_name', i.local_name, 1, 2
    FROM sel s JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.local_name IS NOT NULL

    UNION ALL
    SELECT md5(s.trip_hotel_id || ':DESCRIPTION:' || i.locale)::uuid, s.trip_hotel_id,
           'DESCRIPTION', i.locale::text, 'hotel_address', i.address, 1, 3
    FROM sel s JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.address IS NOT NULL

    UNION ALL
    SELECT md5(s.trip_hotel_id || ':DESCRIPTION:' || i.locale)::uuid, s.trip_hotel_id,
           'DESCRIPTION', i.locale::text, 'countryCode', q.iso2, 1, 4
    FROM sel s
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    JOIN v2.cities c    ON c.id = s.city_id
    JOIN v2.countries co ON co.id = c.country_id
    JOIN ma_quoc_gia q  ON q.trip_country_id = co.trip_country_id

    UNION ALL
    SELECT md5(s.trip_hotel_id || ':DESCRIPTION:' || i.locale)::uuid, s.trip_hotel_id,
           'DESCRIPTION', i.locale::text, 'room_count', s.room_count::text, 1, 5
    FROM sel s JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE s.room_count IS NOT NULL

    UNION ALL
    SELECT md5(s.trip_hotel_id || ':DESCRIPTION:' || i.locale)::uuid, s.trip_hotel_id,
           'DESCRIPTION', i.locale::text, 'description', i.description, 1, 6
    FROM sel s JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    WHERE i.description IS NOT NULL

    -- ------------------------------------------------ POLICY
    UNION ALL
    SELECT md5(s.trip_hotel_id || ':POLICY:' || ps.locale || ':' || ps.section_code)::uuid,
           s.trip_hotel_id, 'POLICY', ps.locale::text, 'policy_title', ps.title,
           2, ps.sort_order * 10
    FROM sel s JOIN v2.hotel_policy_sections ps ON ps.hotel_id = s.id

    UNION ALL
    -- Nội dung gom cả mục thành một dòng HTML:
    --   dòng có nhãn    → <p><strong>Nhận phòng</strong> 15:00</p>
    --   dòng không nhãn → gom chung vào <ul><li>…</li></ul>
    SELECT md5(s.trip_hotel_id || ':POLICY:' || pl.locale || ':' || pl.section_code)::uuid,
           s.trip_hotel_id, 'POLICY', pl.locale::text, 'policy_content',
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
           2, min(ps.sort_order) * 10 + 1
    FROM sel s
    JOIN v2.hotel_policy_lines pl ON pl.hotel_id = s.id
    JOIN v2.hotel_policy_sections ps
      ON ps.hotel_id = pl.hotel_id AND ps.locale = pl.locale
     AND ps.section_code = pl.section_code
    GROUP BY s.trip_hotel_id, pl.locale, pl.section_code

    -- ------------------------------------------------ SURROUNDING
    UNION ALL
    SELECT md5(d.trip_hotel_id || ':SURROUNDING:' || d.locale || ':' || d.place_id)::uuid,
           d.trip_hotel_id, 'SURROUNDING', d.locale::text, 'surrounding_group',
           d.group_name, 3, COALESCE(d.sort_order, 0) * 10 + 1
    FROM dia_diem d WHERE d.group_name IS NOT NULL

    UNION ALL
    SELECT md5(d.trip_hotel_id || ':SURROUNDING:' || d.locale || ':' || d.place_id)::uuid,
           d.trip_hotel_id, 'SURROUNDING', d.locale::text, 'surrounding_name',
           d.ten, 3, COALESCE(d.sort_order, 0) * 10 + 2
    FROM dia_diem d

    UNION ALL
    -- Khoảng cách định dạng giống trang Trip.com: "720m", "1,6km"
    SELECT md5(d.trip_hotel_id || ':SURROUNDING:' || d.locale || ':' || d.place_id)::uuid,
           d.trip_hotel_id, 'SURROUNDING', d.locale::text, 'surrounding_distance',
           CASE WHEN d.distance_km < 1
                THEN round(d.distance_km * 1000)::bigint::text || 'm'
                ELSE replace(round(d.distance_km, 1)::text, '.', ',') || 'km' END,
           3, COALESCE(d.sort_order, 0) * 10 + 3
    FROM dia_diem d WHERE d.distance_km IS NOT NULL

    UNION ALL
    -- Khoảng cách dạng số mét, để sắp xếp và tính toán
    SELECT md5(d.trip_hotel_id || ':SURROUNDING:' || d.locale || ':' || d.place_id)::uuid,
           d.trip_hotel_id, 'SURROUNDING', d.locale::text, 'surrounding_distance_m',
           round(d.distance_km * 1000)::bigint::text,
           3, COALESCE(d.sort_order, 0) * 10 + 4
    FROM dia_diem d WHERE d.distance_km IS NOT NULL

    UNION ALL
    SELECT md5(d.trip_hotel_id || ':SURROUNDING:' || d.locale || ':' || d.place_id)::uuid,
           d.trip_hotel_id, 'SURROUNDING', d.locale::text, 'surrounding_lat',
           d.latitude::text, 3, COALESCE(d.sort_order, 0) * 10 + 5
    FROM dia_diem d WHERE d.latitude IS NOT NULL

    UNION ALL
    SELECT md5(d.trip_hotel_id || ':SURROUNDING:' || d.locale || ':' || d.place_id)::uuid,
           d.trip_hotel_id, 'SURROUNDING', d.locale::text, 'surrounding_lng',
           d.longitude::text, 3, COALESCE(d.sort_order, 0) * 10 + 6
    FROM dia_diem d WHERE d.longitude IS NOT NULL
)
SELECT 'INSERT INTO splatform_meta.trip_property_translation '
       || '(row_uuid, property_id, type, lang, field, value) VALUES ('
       || quote_literal(row_uuid::text) || ', '
       || property_id || ', '
       || quote_literal(type) || ', '
       || quote_literal(lang) || ', '
       || quote_literal(field) || ', '
       || quote_nullable(value) || ');'
FROM rows
WHERE value IS NOT NULL AND btrim(value) <> ''
ORDER BY property_id, lang, ord, sub;
