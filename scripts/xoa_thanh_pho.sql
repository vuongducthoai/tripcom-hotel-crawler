-- =============================================================================
-- XOÁ toàn bộ dữ liệu của MỘT thành phố khỏi schema v2
--
-- CHẠY THỬ (không xoá gì, chỉ in ra sẽ xoá bao nhiêu):
--   psql ... -v thanh_pho=<trip_city_id> -f xoa_thanh_pho.sql
--
-- XOÁ THẬT (không hoàn tác được):
--   psql ... -v thanh_pho=<trip_city_id> -v xacnhan=yes -f xoa_thanh_pho.sql
--
-- Mọi bảng con đều có ON DELETE CASCADE nên chỉ cần xoá ở v2.hotels.
-- =============================================================================
\pset border 2
\if :{?thanh_pho}
\else
\echo 'THIEU THAM SO: chay lai voi  -v thanh_pho=<trip_city_id>'
\quit 1
\endif

\echo
\echo ######## Thành phố sẽ xoá ########
SELECT c.id, c.trip_city_id, co.iso2 AS quoc_gia,
       string_agg(DISTINCT ci.locale || '=' || ci.name, ' · ') AS ten,
       (SELECT count(*) FROM v2.hotels h WHERE h.city_id = c.id) AS so_khach_san
FROM v2.cities c
LEFT JOIN v2.city_i18n ci ON ci.city_id = c.id
LEFT JOIN v2.countries co ON co.id = c.country_id
WHERE c.trip_city_id = :thanh_pho
GROUP BY c.id, c.trip_city_id, co.iso2;

BEGIN;

CREATE TEMP TABLE se_xoa AS
SELECT h.id FROM v2.hotels h
JOIN v2.cities c ON c.id = h.city_id
WHERE c.trip_city_id = :thanh_pho;

\echo
\echo ######## Số dòng sẽ mất ở từng bảng ########
SELECT 'hotels'                AS bang, count(*) FROM v2.hotels             WHERE id       IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'hotel_i18n',           count(*) FROM v2.hotel_i18n        WHERE hotel_id IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'hotel_images',         count(*) FROM v2.hotel_images      WHERE hotel_id IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'hotel_amenities',      count(*) FROM v2.hotel_amenities   WHERE hotel_id IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'hotel_policy_sections',count(*) FROM v2.hotel_policy_sections WHERE hotel_id IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'hotel_policy_lines',   count(*) FROM v2.hotel_policy_lines    WHERE hotel_id IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'hotel_nearby_places',  count(*) FROM v2.hotel_nearby_places   WHERE hotel_id IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'room_types',           count(*) FROM v2.room_types        WHERE hotel_id IN (SELECT id FROM se_xoa)
UNION ALL SELECT 'room_offers',          count(*) FROM v2.room_offers o
          JOIN v2.room_types r ON r.id = o.room_type_id WHERE r.hotel_id IN (SELECT id FROM se_xoa)
ORDER BY 1;

DELETE FROM v2.hotels WHERE id IN (SELECT id FROM se_xoa);

-- Địa điểm không còn khách sạn nào trỏ tới thì xoá luôn cho sạch
DELETE FROM v2.places p
WHERE NOT EXISTS (SELECT 1 FROM v2.hotel_nearby_places np WHERE np.place_id = p.id);

-- Thành phố rỗng thì xoá nốt
DELETE FROM v2.cities c
WHERE c.trip_city_id = :thanh_pho
  AND NOT EXISTS (SELECT 1 FROM v2.hotels h WHERE h.city_id = c.id);

\echo
\echo ######## Còn lại sau khi xoá ########
SELECT (SELECT count(*) FROM v2.hotels)  AS con_khach_san,
       (SELECT count(*) FROM v2.places)  AS con_dia_diem,
       (SELECT count(*) FROM v2.cities)  AS con_thanh_pho;

\if :{?xacnhan}
COMMIT;
\echo '>>> DA XOA THAT (COMMIT).'
\else
ROLLBACK;
\echo '>>> CHAY THU — da ROLLBACK, chua xoa gi.'
\echo '>>> Muon xoa that: them  -v xacnhan=yes'
\endif
