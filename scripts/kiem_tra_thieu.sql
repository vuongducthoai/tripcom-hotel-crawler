-- =============================================================================
-- Báo cáo v2 còn thiếu gì — CHỈ XÉT 3 PHẦN MÀ FILE DUMP CẦN:
--     DESCRIPTION  ·  POLICY  ·  SURROUNDING (lân cận)
-- Phòng / giá / ảnh / tiện nghi KHÔNG xét (office công ty đã có API riêng).
-- Bỏ qua TP. Hồ Chí Minh.
--
--   docker cp scripts\kiem_tra_thieu.sql tripcom-postgres:/tmp/kt.sql
--   docker exec -e PGPASSWORD=... tripcom-postgres psql -U tripcom -d tripcom -f /tmp/kt.sql
-- =============================================================================
\pset border 2
SET search_path TO v2, public;

-- 1 bảng tạm duy nhất: mỗi khách sạn 6 cột CÓ/KHÔNG cho 3 phần × 2 ngôn ngữ.
-- Chính sách bỏ section_code = 'credit' (anh mentor không lấy phương thức thanh toán),
-- nên khách sạn chỉ có mỗi 'credit' vẫn tính là THIẾU chính sách.
CREATE TEMP TABLE kt AS
SELECT h.id,
       h.trip_hotel_id,
       COALESCE(NULLIF(btrim(ci.name), ''), 'city ' || c.trip_city_id::text) AS thanh_pho,
       co.iso2                                                              AS qg,
       EXISTS (SELECT 1 FROM hotel_i18n i
                WHERE i.hotel_id = h.id AND i.locale LIKE 'vi%'
                  AND COALESCE(btrim(i.description), '') <> '')             AS mo_ta_vi,
       EXISTS (SELECT 1 FROM hotel_i18n i
                WHERE i.hotel_id = h.id AND i.locale LIKE 'en%'
                  AND COALESCE(btrim(i.description), '') <> '')             AS mo_ta_en,
       EXISTS (SELECT 1 FROM hotel_policy_sections p
                WHERE p.hotel_id = h.id AND p.locale LIKE 'vi%'
                  AND p.section_code <> 'credit')                           AS policy_vi,
       EXISTS (SELECT 1 FROM hotel_policy_sections p
                WHERE p.hotel_id = h.id AND p.locale LIKE 'en%'
                  AND p.section_code <> 'credit')                           AS policy_en,
       EXISTS (SELECT 1 FROM hotel_nearby_places np
                 JOIN place_i18n pi ON pi.place_id = np.place_id
                  AND pi.locale LIKE 'vi%' AND COALESCE(btrim(pi.name), '') <> ''
                WHERE np.hotel_id = h.id)                                   AS lan_can_vi,
       EXISTS (SELECT 1 FROM hotel_nearby_places np
                 JOIN place_i18n pi ON pi.place_id = np.place_id
                  AND pi.locale LIKE 'en%' AND COALESCE(btrim(pi.name), '') <> ''
                WHERE np.hotel_id = h.id)                                   AS lan_can_en
FROM hotels h
LEFT JOIN cities c     ON c.id = h.city_id
LEFT JOIN city_i18n ci ON ci.city_id = c.id AND ci.locale LIKE 'vi%'
LEFT JOIN countries co ON co.id = c.country_id
WHERE NOT EXISTS (SELECT 1 FROM city_i18n cx
                   WHERE cx.city_id = h.city_id AND cx.name ILIKE '%Ch_ Minh%');

\echo
\echo ############ 1. TỔNG QUAN — "thieu" = so khach san KHONG co phan do ############
SELECT phan, lang, co, (SELECT count(*) FROM kt) - co AS thieu,
       round(100.0 * co / NULLIF((SELECT count(*) FROM kt), 0), 1) AS pct
FROM (
  SELECT 'description' AS phan, 'vi' AS lang, count(*) FILTER (WHERE mo_ta_vi)   AS co, 1 AS o FROM kt
  UNION ALL SELECT 'description', 'en', count(*) FILTER (WHERE mo_ta_en),   2 FROM kt
  UNION ALL SELECT 'policy',      'vi', count(*) FILTER (WHERE policy_vi),  3 FROM kt
  UNION ALL SELECT 'policy',      'en', count(*) FILTER (WHERE policy_en),  4 FROM kt
  UNION ALL SELECT 'surrounding', 'vi', count(*) FILTER (WHERE lan_can_vi), 5 FROM kt
  UNION ALL SELECT 'surrounding', 'en', count(*) FILTER (WHERE lan_can_en), 6 FROM kt
) x ORDER BY o;

\echo
\echo ############ 2. DU / THIEU THEO NGON NGU (du = co ca 3 phan) ############
SELECT count(*)                                                              AS tong,
       count(*) FILTER (WHERE mo_ta_vi AND policy_vi AND lan_can_vi)         AS du_vi,
       count(*) FILTER (WHERE NOT (mo_ta_vi AND policy_vi AND lan_can_vi))   AS thieu_vi,
       count(*) FILTER (WHERE mo_ta_en AND policy_en AND lan_can_en)         AS du_en,
       count(*) FILTER (WHERE NOT (mo_ta_en AND policy_en AND lan_can_en))   AS thieu_en
FROM kt;

\echo
\echo ############ 3. THEO THANH PHO ############
SELECT thanh_pho, qg, count(*) AS tong,
       count(*) FILTER (WHERE NOT mo_ta_vi)   AS thieu_mota_vi,
       count(*) FILTER (WHERE NOT mo_ta_en)   AS thieu_mota_en,
       count(*) FILTER (WHERE NOT policy_vi)  AS thieu_pol_vi,
       count(*) FILTER (WHERE NOT policy_en)  AS thieu_pol_en,
       count(*) FILTER (WHERE NOT lan_can_vi) AS thieu_lc_vi,
       count(*) FILTER (WHERE NOT lan_can_en) AS thieu_lc_en
FROM kt GROUP BY 1, 2 ORDER BY 3 DESC;

\echo
\echo ############ 4. 30 KHACH SAN THIEU NHIEU NHAT ############
SELECT trip_hotel_id, thanh_pho,
       mo_ta_vi, policy_vi, lan_can_vi,
       mo_ta_en, policy_en, lan_can_en,
       (NOT mo_ta_vi)::int + (NOT policy_vi)::int + (NOT lan_can_vi)::int
     + (NOT mo_ta_en)::int + (NOT policy_en)::int + (NOT lan_can_en)::int AS so_phan_thieu
FROM kt
WHERE NOT (mo_ta_vi AND policy_vi AND lan_can_vi AND mo_ta_en AND policy_en AND lan_can_en)
ORDER BY so_phan_thieu DESC, trip_hotel_id
LIMIT 30;
