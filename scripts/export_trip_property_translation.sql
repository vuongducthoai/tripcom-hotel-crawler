-- =============================================================================
-- Xuất dữ liệu v2 sang bảng splatform_meta.trip_tmp_property_translation
--
--   psql ... -At -f scripts/export_trip_property_translation.sql            > dump.sql
--   psql ... -At -v pfx= -f scripts/export_trip_property_translation.sql   > dump.txt
--
-- Mỗi bản ghi logic là một row_uuid, gom nhiều dòng field/value:
--   type = DESCRIPTION  → 1 row_uuid / khách sạn
--   type = POLICY       → 1 row_uuid / mục chính sách
--   type = SURROUNDING  → 1 row_uuid / địa điểm gần đây
--
-- row_uuid KHÔNG chứa lang: bản tiếng Việt và bản tiếng Anh của cùng một bản
-- ghi dùng CHUNG một row_uuid, chỉ khác cột lang. Nhờ đó ghép được
-- "Thời gian nhận và trả phòng" ↔ "Check-in and Check-out Times".
-- Khoá nghiệp vụ dùng mã bất biến theo ngôn ngữ:
--   DESCRIPTION → trip_hotel_id
--   POLICY      → trip_hotel_id + section_code ('checkInAndOut')
--   SURROUNDING → trip_hotel_id + trip_poi_id (mã POI của Trip.com)
-- Sinh bằng md5 nên chạy lại vẫn ra đúng uuid cũ, tiện nạp lại / đối chiếu.
--
-- section_type giữ NGUYÊN giá trị Trip.com trả về:
--   POLICY      → tên key trong hotelPolicyInfo ('checkInAndOut', 'pet'…)
--   SURROUNDING → '<mã nhóm 2 chữ số>_<tên nhóm theo ngôn ngữ đó>', giữ nguyên
--                  tên Trip.com trả về: '02_Giao thông' (vi) / '02_Transport' (en).
--                  LƯU Ý ĐÃ BIẾT VÀ CHẤP NHẬN: Trip.com xếp nhóm khác nhau theo
--                  ngôn ngữ (cùng khách sạn 118050925, bản EN có thêm nhóm
--                  4 Dining mà bản VI không có), nên CÙNG MỘT row_uuid có thể
--                  mang hai section_type khác nhau ở hai ngôn ngữ. Hệ quả:
--                    · đếm nhóm phải luôn lọc kèm lang
--                    · lọc một nhóm chỉ ra một ngôn ngữ; muốn bản dịch kia
--                      phải tra ngược theo row_uuid
--   DESCRIPTION → 'hotelInfo' (Trip.com không có mã cho phần này)
--
-- Đổi LIMIT / bỏ chú thích dòng lọc thành phố ở CTE "sel" nếu cần.
-- =============================================================================
-- Tiền tố schema: mặc định splatform_meta. ; chạy với -v pfx= để bỏ tiền tố.
\if :{?pfx}
\else
\set pfx 'splatform_meta.'
\endif

WITH sel AS (
    -- Ưu tiên khách sạn có ĐỦ CẢ tiếng Việt và tiếng Anh, để file dump thể hiện
    -- được việc ghép cặp theo row_uuid. Khách sạn chỉ có một thứ tiếng xếp sau.
    SELECT h.id, h.trip_hotel_id, h.room_count, h.city_id
    FROM v2.hotels h
    JOIN v2.hotel_i18n vi ON vi.hotel_id = h.id AND vi.locale LIKE 'vi%'
                         AND vi.description IS NOT NULL
    LEFT JOIN v2.hotel_i18n en ON en.hotel_id = h.id AND en.locale LIKE 'en%'
                              AND en.description IS NOT NULL
    -- JOIN v2.cities c ON c.id = h.city_id AND c.trip_city_id = 495   -- chỉ New Delhi
    ORDER BY (en.hotel_id IS NULL),                                      -- 1. có bản EN trước
             (SELECT count(*) FROM v2.hotel_policy_sections ps
               WHERE ps.hotel_id = h.id AND ps.locale LIKE 'en%') DESC,  -- 2. nhiều chính sách EN
             (SELECT count(*) FROM v2.hotel_nearby_places np
                JOIN v2.place_i18n pi ON pi.place_id = np.place_id
               WHERE np.hotel_id = h.id AND pi.locale LIKE 'en%') DESC,  -- 3. có POI tiếng Anh
             h.id
    LIMIT 20
),

-- Mã quốc gia ISO 3166-1 alpha-2 theo countryId của Trip.com.
-- Thêm dòng mới ở đây khi mở rộng sang nước khác.
ma_quoc_gia (trip_country_id, iso2) AS (VALUES
    (111, 'VN'), (27, 'DK'), (107, 'IN')
),

-- Trip.com thỉnh thoảng không trả group_name cho vài địa điểm. Lấy tên phổ
-- biến nhất của chính nhóm đó, trong chính ngôn ngữ đó, để section_type không
-- bị rơi thành số trần ('3' lẫn với '3_Landmarks').
ten_nhom AS (
    SELECT DISTINCT ON (locale, group_code) locale, group_code, group_name
    FROM (
        SELECT npi.locale, COALESCE(npi.group_code, np.group_code) AS group_code,
               npi.group_name, count(*) AS n
        FROM v2.hotel_nearby_place_i18n npi
        JOIN v2.hotel_nearby_places np
          ON np.hotel_id = npi.hotel_id AND np.place_id = npi.place_id
        WHERE COALESCE(npi.group_name, '') <> ''
        GROUP BY 1, 2, 3
    ) t
    ORDER BY locale, group_code, n DESC, group_name
),

-- Địa điểm gần đây đã lọc rác: Trip.com thỉnh thoảng trả tên placeholder
-- kiểu "size?" — bỏ cả địa điểm đó chứ không lưu nửa vời.
dia_diem AS (
    SELECT s.trip_hotel_id, pi.locale, pl.trip_poi_id, np.sort_order,
           -- mã nhóm theo đúng ngôn ngữ; cột cũ dùng chung chỉ là dự phòng
           COALESCE(npi.group_code, np.group_code) AS group_code,
           pi.name AS ten, np.distance_km, pl.latitude, pl.longitude,
           COALESCE(NULLIF(btrim(npi.group_name), ''), tn.group_name) AS group_name
    FROM sel s
    JOIN v2.hotel_nearby_places np ON np.hotel_id = s.id
    JOIN v2.places      pl ON pl.id = np.place_id
    JOIN v2.place_i18n  pi ON pi.place_id = pl.id
    LEFT JOIN v2.hotel_nearby_place_i18n npi
           ON npi.hotel_id = np.hotel_id AND npi.place_id = np.place_id
          AND npi.locale = pi.locale
    LEFT JOIN ten_nhom tn
           ON tn.locale = pi.locale
          AND tn.group_code = COALESCE(npi.group_code, np.group_code)
    WHERE pi.name IS NOT NULL
      AND btrim(pi.name) <> ''
      AND btrim(pi.name) !~* '^(size\?|n/?a|null|-+|\?+)$'
),

-- Các mục chính sách KHÔNG xuất sang bảng của platform.
--   credit — "Thanh toán tại khách sạn". Trip.com trả phần này chủ yếu bằng
--            ảnh logo thẻ (Visa/Mastercard/Amex/Diners) mà mình không lưu,
--            nên sau khi bỏ ảnh chỉ còn lại tiêu đề + "Tiền mặt" — thừa dữ
--            liệu. Anh Thoại yêu cầu bỏ (2026-09-24).
-- Thêm mã vào đây nếu sau này cần bỏ thêm mục khác.
muc_bo_qua (ma) AS (VALUES ('credit')),

-- Nội dung chính sách gom cả mục thành một dòng HTML:
--   dòng có nhãn    → <p><strong>Nhận phòng</strong> 15:00</p>
--   dòng không nhãn → gom chung vào <ul><li>…</li></ul>
chinh_sach_html AS (
    SELECT s.trip_hotel_id, pl.locale, pl.section_code, min(ps.sort_order) AS sort_order,
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
              ELSE '' END AS noi_dung
    FROM sel s
    JOIN v2.hotel_policy_lines pl ON pl.hotel_id = s.id
    JOIN v2.hotel_policy_sections ps
      ON ps.hotel_id = pl.hotel_id AND ps.locale = pl.locale
     AND ps.section_code = pl.section_code
    GROUP BY s.trip_hotel_id, pl.locale, pl.section_code
),

rows AS (
    -- ------------------------------------------------ DESCRIPTION
    SELECT md5(s.trip_hotel_id || ':DESCRIPTION')::uuid AS row_uuid,
           s.trip_hotel_id AS property_id,
           'DESCRIPTION'   AS type,
           'hotelInfo'     AS section_type,
           i.locale::text  AS lang,
           x.field, x.value, 1 AS ord, x.sub
    FROM sel s
    JOIN v2.hotel_i18n i ON i.hotel_id = s.id
    LEFT JOIN v2.cities c     ON c.id = s.city_id
    LEFT JOIN v2.countries co ON co.id = c.country_id
    LEFT JOIN ma_quoc_gia q   ON q.trip_country_id = co.trip_country_id
    CROSS JOIN LATERAL (VALUES
        ('hotel_name',    i.name,              1),
        ('local_name',    i.local_name,        2),
        ('hotel_address', i.address,           3),
        ('countryCode',   q.iso2,              4),
        ('room_count',    s.room_count::text,  5),
        ('description',   i.description,       6)
    ) AS x(field, value, sub)

    -- ------------------------------------------------ POLICY
    UNION ALL
    SELECT md5(s.trip_hotel_id || ':POLICY:' || ps.section_code)::uuid,
           s.trip_hotel_id, 'POLICY', ps.section_code, ps.locale::text,
           'policy_title', ps.title, 2, ps.sort_order * 10
    FROM sel s JOIN v2.hotel_policy_sections ps ON ps.hotel_id = s.id
    WHERE ps.section_code <> ALL (SELECT ma FROM muc_bo_qua)

    UNION ALL
    SELECT md5(h.trip_hotel_id || ':POLICY:' || h.section_code)::uuid,
           h.trip_hotel_id, 'POLICY', h.section_code, h.locale::text,
           'policy_content', h.noi_dung, 2, h.sort_order * 10 + 1
    FROM chinh_sach_html h
    WHERE h.section_code <> ALL (SELECT ma FROM muc_bo_qua)

    -- ------------------------------------------------ SURROUNDING
    UNION ALL
    SELECT md5(d.trip_hotel_id || ':SURROUNDING:' || d.trip_poi_id)::uuid,
           d.trip_hotel_id, 'SURROUNDING',
           -- section_type = <mã nhóm>_<tên nhóm theo đúng ngôn ngữ đó>
           -- ví dụ: '2_Giao thông' (vi)  /  '2_Transport' (en)
           -- mã nhóm đệm 0 cho đủ 2 chữ số để sắp xếp theo chuỗi vẫn đúng
           -- thứ tự ('02' < '03' < '04'), còn split_part(...,'_',1)::int
           -- vẫn ra đúng số gốc của Trip.com.
           lpad(d.group_code::text, 2, '0')
             || CASE WHEN COALESCE(d.group_name, '') <> ''
                     THEN '_' || d.group_name ELSE '' END,
           d.locale::text,
           y.field, y.value, 3, COALESCE(d.sort_order, 0) * 10 + y.sub
    FROM dia_diem d
    CROSS JOIN LATERAL (VALUES
        -- Không xuất surrounding_group / surrounding_group_id nữa: mã và tên
        -- nhóm đã nằm trong section_type, tách ra bằng
        --   split_part(section_type, '_', 1)                    -> mã nhóm
        --   substr(section_type, strpos(section_type,'_') + 1)  -> tên nhóm
        ('surrounding_name',       d.ten,                 1),
        ('surrounding_distance',
            CASE WHEN d.distance_km < 1
                 THEN round(d.distance_km * 1000)::bigint::text || 'm'
                 ELSE replace(round(d.distance_km, 1)::text, '.', ',') || 'km' END, 2),
        ('surrounding_distance_m', round(d.distance_km * 1000)::bigint::text, 3),
        ('surrounding_lat',        d.latitude::text,      4),
        ('surrounding_lng',        d.longitude::text,     5)
    ) AS y(field, value, sub)
)
SELECT 'INSERT INTO ' || :'pfx' || 'trip_tmp_property_translation '
       || '(row_uuid, property_id, type, section_type, lang, field, value) VALUES ('
       || quote_literal(row_uuid::text) || ', '
       || property_id || ', '
       || quote_literal(type) || ', '
       || quote_literal(section_type) || ', '
       || quote_literal(lang) || ', '
       || quote_literal(field) || ', '
       || quote_nullable(value) || ');'
FROM rows
WHERE value IS NOT NULL AND btrim(value) <> ''
ORDER BY property_id, lang, ord, sub;
