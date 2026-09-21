-- Bảng "còn thiếu gì" cho toàn bộ khách sạn hiện có trong DB.
-- Chạy: docker exec -i tripcom-postgres psql -U tripcom -d tripcom -f - < gap_report.sql
WITH t AS (SELECT count(*)::numeric AS tong FROM hotels),
m(muc, co) AS (
    SELECT 'Toa do (lat/lon)', count(*) FROM hotels
      WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    UNION ALL SELECT 'Hang sao', count(*) FROM hotels WHERE star_rating IS NOT NULL
    UNION ALL SELECT 'Diem danh gia', count(*) FROM hotels WHERE review_score IS NOT NULL
    UNION ALL SELECT 'Ten [vi]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='vi' AND name IS NOT NULL AND btrim(name) <> ''
    UNION ALL SELECT 'Ten [en]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='en' AND name IS NOT NULL AND btrim(name) <> ''
    UNION ALL SELECT 'Dia chi [vi]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='vi' AND address IS NOT NULL AND btrim(address) <> ''
    UNION ALL SELECT 'Dia chi [en]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='en' AND address IS NOT NULL AND btrim(address) <> ''
    UNION ALL SELECT 'Mo ta [vi]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='vi' AND description IS NOT NULL AND btrim(description) <> ''
    UNION ALL SELECT 'Mo ta [en]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='en' AND description IS NOT NULL AND btrim(description) <> ''
    UNION ALL SELECT 'Loai hinh [vi]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='vi' AND hotel_type IS NOT NULL
    UNION ALL SELECT 'Loai hinh [en]', count(DISTINCT hotel_id) FROM hotel_translations
      WHERE locale='en' AND hotel_type IS NOT NULL
    UNION ALL SELECT 'Anh khach san', count(DISTINCT hotel_id) FROM hotel_images
    UNION ALL SELECT 'Nhan album anh [en]', count(DISTINCT i.hotel_id)
      FROM hotel_images i JOIN hotel_image_categories c ON c.hotel_image_id = i.id
      WHERE c.locale='en'
    UNION ALL SELECT 'Tien nghi KS [vi]', count(DISTINCT a.hotel_id)
      FROM hotel_amenities a JOIN hotel_amenity_translations x ON x.hotel_amenity_id = a.id
      WHERE x.locale='vi'
    UNION ALL SELECT 'Tien nghi KS [en]', count(DISTINCT a.hotel_id)
      FROM hotel_amenities a JOIN hotel_amenity_translations x ON x.hotel_amenity_id = a.id
      WHERE x.locale='en'
    UNION ALL SELECT 'Loai phong [vi]', count(DISTINCT r.hotel_id)
      FROM room_types r JOIN room_type_translations x ON x.room_type_id = r.id
      WHERE x.locale='vi'
    UNION ALL SELECT 'Loai phong [en]', count(DISTINCT r.hotel_id)
      FROM room_types r JOIN room_type_translations x ON x.room_type_id = r.id
      WHERE x.locale='en'
    UNION ALL SELECT 'Anh phong', count(DISTINCT r.hotel_id)
      FROM room_types r JOIN room_images i ON i.room_type_id = r.id
    UNION ALL SELECT 'Tien nghi phong [vi]', count(DISTINCT r.hotel_id)
      FROM room_types r JOIN room_amenities a ON a.room_type_id = r.id
      JOIN room_amenity_translations x ON x.room_amenity_id = a.id WHERE x.locale='vi'
    UNION ALL SELECT 'Tien nghi phong [en]', count(DISTINCT r.hotel_id)
      FROM room_types r JOIN room_amenities a ON a.room_type_id = r.id
      JOIN room_amenity_translations x ON x.room_amenity_id = a.id WHERE x.locale='en'
    UNION ALL SELECT 'Gia phong [vi/VND]', count(DISTINCT hotel_id) FROM hotel_prices
      WHERE language='vi' AND price_type='room'
    UNION ALL SELECT 'Gia phong [en/USD]', count(DISTINCT hotel_id) FROM hotel_prices
      WHERE language='en' AND price_type='room'
    UNION ALL SELECT 'Chinh sach [vi]', count(DISTINCT p.hotel_id)
      FROM hotel_policies p JOIN hotel_policy_translations x ON x.hotel_policy_id = p.id
      WHERE x.locale='vi'
    UNION ALL SELECT 'Chinh sach [en]', count(DISTINCT p.hotel_id)
      FROM hotel_policies p JOIN hotel_policy_translations x ON x.hotel_policy_id = p.id
      WHERE x.locale='en'
    UNION ALL SELECT 'Dia diem lan can [vi]', count(DISTINCT n.hotel_id)
      FROM hotel_nearby_places n JOIN hotel_nearby_place_translations x
        ON x.nearby_place_id = n.id WHERE x.locale='vi'
    UNION ALL SELECT 'Dia diem lan can [en]', count(DISTINCT n.hotel_id)
      FROM hotel_nearby_places n JOIN hotel_nearby_place_translations x
        ON x.nearby_place_id = n.id WHERE x.locale='en'
)
SELECT m.muc AS "Hang muc",
       m.co AS "Co",
       (t.tong - m.co)::int AS "Thieu",
       round(100 * m.co / t.tong, 1) AS "Phan tram"
FROM m CROSS JOIN t
ORDER BY 4 DESC, 1;
