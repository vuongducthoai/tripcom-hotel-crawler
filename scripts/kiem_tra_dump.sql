-- =============================================================================
-- Kiểm tra dữ liệu đã nạp vào splatform_meta.trip_tmp_property_translation
-- Đổi :ks sang trip_hotel_id khác nếu muốn soi khách sạn khác.
-- =============================================================================
\pset border 2
\set ks 744865

\echo
\echo ============ 1. DESCRIPTION — VI và EN cùng row_uuid ============
SELECT field,
       max(value) FILTER (WHERE lang = 'vi') AS vi,
       max(value) FILTER (WHERE lang = 'en') AS en,
       count(DISTINCT row_uuid)              AS so_uuid   -- phải = 1
FROM splatform_meta.trip_tmp_property_translation
WHERE property_id = :ks AND type = 'DESCRIPTION'
GROUP BY field ORDER BY field;

\echo
\echo ============ 2. POLICY — ghép tiêu đề VI/EN theo row_uuid ============
SELECT section_type,
       left(max(value) FILTER (WHERE lang = 'vi'), 45) AS tieu_de_vi,
       left(max(value) FILTER (WHERE lang = 'en'), 45) AS tieu_de_en,
       count(DISTINCT row_uuid)                        AS so_uuid   -- phải = 1
FROM splatform_meta.trip_tmp_property_translation
WHERE property_id = :ks AND type = 'POLICY' AND field = 'policy_title'
GROUP BY section_type ORDER BY section_type;

\echo
\echo ============ 3. POLICY — mục nào thiếu một thứ tiếng ============
SELECT section_type, string_agg(DISTINCT lang, ', ' ORDER BY lang) AS co_ngon_ngu
FROM splatform_meta.trip_tmp_property_translation
WHERE property_id = :ks AND type = 'POLICY'
GROUP BY row_uuid, section_type
HAVING count(DISTINCT lang) < 2
ORDER BY section_type;

\echo
\echo ============ 4. SURROUNDING — section_type và số địa điểm ============
SELECT section_type, lang, count(DISTINCT row_uuid) AS so_dia_diem
FROM splatform_meta.trip_tmp_property_translation
WHERE property_id = :ks AND type = 'SURROUNDING'
GROUP BY section_type, lang ORDER BY section_type, lang;

\echo
\echo ============ 5. SURROUNDING — 10 địa điểm gần nhất ============
SELECT section_type,
       max(value) FILTER (WHERE field = 'surrounding_name')     AS ten,
       max(value) FILTER (WHERE field = 'surrounding_group')    AS nhom,
       max(value) FILTER (WHERE field = 'surrounding_distance') AS khoang_cach,
       string_agg(DISTINCT lang, ',' ORDER BY lang)             AS ngon_ngu
FROM splatform_meta.trip_tmp_property_translation
WHERE property_id = :ks AND type = 'SURROUNDING'
GROUP BY row_uuid, section_type
ORDER BY max(value) FILTER (WHERE field = 'surrounding_distance_m')::int
LIMIT 10;

\echo
\echo ============ 6. Tổng kết ghép cặp của khách sạn này ============
SELECT type,
       count(*)                              AS so_ban_ghi,
       count(*) FILTER (WHERE n = 2)         AS du_vi_en,
       count(*) FILTER (WHERE n = 1)         AS chi_1_thu_tieng
FROM (SELECT row_uuid, type, count(DISTINCT lang) AS n
      FROM splatform_meta.trip_tmp_property_translation
      WHERE property_id = :ks GROUP BY 1, 2) t
GROUP BY type ORDER BY type;

\echo
\echo ============ 7. Trùng lặp — phải ra 0 dòng ============
SELECT row_uuid, lang, field, count(*) AS so_lan
FROM splatform_meta.trip_tmp_property_translation
GROUP BY 1, 2, 3 HAVING count(*) > 1 LIMIT 10;
