-- =============================================================================
-- Kiểm tra phần SURROUNDING: section_type sinh ra từ group_code của Trip.com
-- và tên nhóm thật mà Trip.com trả về, tách theo ngôn ngữ.
--
--   docker exec -e PGPASSWORD=<pass> tripcom-postgres \
--     psql -U tripcom -d tripcom -f /tmp/kt.sql
--
-- Đổi :ks ở phần 2 sang trip_hotel_id khác nếu muốn soi khách sạn khác.
-- =============================================================================
\pset border 2
\set ks 744865

\echo
\echo === 1. section_type x group_code x ngôn ngữ ===
SELECT CASE np.group_code WHEN 2 THEN 'TRANSPORT'
                          WHEN 3 THEN 'LANDMARK'
                          WHEN 5 THEN 'SHOPPING'
                          ELSE 'OTHER' END          AS section_type,
       np.group_code                                AS ma_goc_tripcom,
       pi.locale                                    AS lang,
       count(DISTINCT pl.trip_poi_id)               AS so_dia_diem,
       string_agg(DISTINCT npi.group_name, ' | ')   AS ten_nhom_tripcom_tra_ve
FROM v2.hotel_nearby_places np
JOIN v2.places      pl ON pl.id = np.place_id
JOIN v2.place_i18n  pi ON pi.place_id = pl.id
LEFT JOIN v2.hotel_nearby_place_i18n npi
       ON npi.hotel_id = np.hotel_id AND npi.place_id = np.place_id
      AND npi.locale = pi.locale
WHERE btrim(pi.name) !~* '^(size\?|n/?a|null|-+|\?+)$'
GROUP BY 1, 2, 3
ORDER BY 2, 3;

\echo
\echo === 2. Chi tiết một khách sạn: VI và EN có cùng row_uuid không ===
SELECT md5(:ks || ':SURROUNDING:' || pl.trip_poi_id)::uuid      AS row_uuid,
       CASE np.group_code WHEN 2 THEN 'TRANSPORT'
                          WHEN 3 THEN 'LANDMARK'
                          WHEN 5 THEN 'SHOPPING'
                          ELSE 'OTHER' END                      AS section_type,
       pl.trip_poi_id,
       max(pi.name) FILTER (WHERE pi.locale LIKE 'vi%')         AS ten_vi,
       max(pi.name) FILTER (WHERE pi.locale LIKE 'en%')         AS ten_en,
       max(npi.group_name) FILTER (WHERE pi.locale LIKE 'vi%')  AS nhom_vi,
       max(npi.group_name) FILTER (WHERE pi.locale LIKE 'en%')  AS nhom_en,
       max(npi.distance_text)                                   AS khoang_cach
FROM v2.hotels h
JOIN v2.hotel_nearby_places np ON np.hotel_id = h.id
JOIN v2.places      pl ON pl.id = np.place_id
JOIN v2.place_i18n  pi ON pi.place_id = pl.id
LEFT JOIN v2.hotel_nearby_place_i18n npi
       ON npi.hotel_id = np.hotel_id AND npi.place_id = np.place_id
      AND npi.locale = pi.locale
WHERE h.trip_hotel_id = :ks
  AND btrim(pi.name) !~* '^(size\?|n/?a|null|-+|\?+)$'
GROUP BY pl.trip_poi_id, np.group_code, np.sort_order
ORDER BY np.sort_order
LIMIT 20;
