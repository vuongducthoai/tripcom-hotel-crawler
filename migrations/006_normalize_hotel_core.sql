-- Normalize language keys and repair hotel/location data imported by older loaders.
-- Trip.com request locales remain vi-VN/en-US; database language keys are vi/en.

BEGIN;

UPDATE hotel_translations SET locale='vi' WHERE locale='vi-VN';
UPDATE hotel_translations SET locale='en' WHERE locale='en-US';
UPDATE location_translations SET locale='vi' WHERE locale='vi-VN';
UPDATE location_translations SET locale='en' WHERE locale='en-US';
UPDATE room_type_translations SET locale='vi' WHERE locale='vi-VN';
UPDATE room_type_translations SET locale='en' WHERE locale='en-US';
UPDATE hotel_amenity_translations SET locale='vi' WHERE locale='vi-VN';
UPDATE hotel_amenity_translations SET locale='en' WHERE locale='en-US';
UPDATE hotel_image_categories SET locale='vi' WHERE locale='vi-VN';
UPDATE hotel_image_categories SET locale='en' WHERE locale='en-US';
UPDATE hotel_prices SET locale='vi' WHERE locale='vi-VN';
UPDATE hotel_prices SET locale='en' WHERE locale='en-US';
UPDATE crawl_runs SET locale='vi' WHERE locale='vi-VN';
UPDATE crawl_runs SET locale='en' WHERE locale='en-US';
UPDATE room_amenity_translations SET locale='vi' WHERE locale='vi-VN';
UPDATE room_amenity_translations SET locale='en' WHERE locale='en-US';
UPDATE hotel_policy_translations SET locale='vi' WHERE locale='vi-VN';
UPDATE hotel_policy_translations SET locale='en' WHERE locale='en-US';
UPDATE hotel_nearby_place_translations SET locale='vi' WHERE locale='vi-VN';
UPDATE hotel_nearby_place_translations SET locale='en' WHERE locale='en-US';

-- Rename the localized detail map without changing the nested source payload.
UPDATE hotels
SET raw_json = jsonb_set(
    raw_json,
    '{detail_by_locale}',
    ((raw_json->'detail_by_locale') - 'vi-VN'::text - 'en-US'::text)
      || CASE WHEN raw_json->'detail_by_locale' ? 'vi-VN'
              THEN jsonb_build_object('vi', raw_json->'detail_by_locale'->'vi-VN')
              ELSE '{}'::jsonb END
      || CASE WHEN raw_json->'detail_by_locale' ? 'en-US'
              THEN jsonb_build_object('en', raw_json->'detail_by_locale'->'en-US')
              ELSE '{}'::jsonb END,
    true
)
WHERE jsonb_typeof(raw_json->'detail_by_locale')='object'
  AND (raw_json->'detail_by_locale' ? 'vi-VN'
       OR raw_json->'detail_by_locale' ? 'en-US');

-- There must be one canonical HCMC location for Trip cityId=301.
INSERT INTO locations (trip_location_id, name, name_en, type, country_code)
VALUES ('city:301', 'TP. Hồ Chí Minh', 'Ho Chi Minh City', 'city', 'VN')
ON CONFLICT (trip_location_id) DO UPDATE SET
    name='TP. Hồ Chí Minh',
    name_en='Ho Chi Minh City',
    type='city',
    country_code='VN';

DO $$
DECLARE
    canonical_id BIGINT;
BEGIN
    SELECT id INTO canonical_id FROM locations WHERE trip_location_id='city:301';

    INSERT INTO location_translations (location_id, locale, name)
    VALUES
        (canonical_id, 'vi', 'TP. Hồ Chí Minh'),
        (canonical_id, 'en', 'Ho Chi Minh City')
    ON CONFLICT (location_id, locale) DO UPDATE SET
        name=EXCLUDED.name,
        updated_at=now();

    UPDATE hotels
    SET location_id=canonical_id
    WHERE raw_json->>'city_name' IN ('TP. Hồ Chí Minh', 'Ho Chi Minh City')
       OR location_id IN (
            SELECT id FROM locations
            WHERE id<>canonical_id
              AND (trip_location_id LIKE 'city-name:%'
                   OR name IN ('TP. Hồ Chí Minh', 'Ho Chi Minh City')
                   OR name_en='Ho Chi Minh City')
       );

    UPDATE locations
    SET parent_id=canonical_id
    WHERE parent_id IN (
        SELECT id FROM locations
        WHERE id<>canonical_id
          AND (trip_location_id LIKE 'city-name:%'
               OR name IN ('TP. Hồ Chí Minh', 'Ho Chi Minh City')
               OR name_en='Ho Chi Minh City')
    );

    DELETE FROM locations
    WHERE id<>canonical_id
      AND (trip_location_id LIKE 'city-name:%'
           OR name IN ('TP. Hồ Chí Minh', 'Ho Chi Minh City')
           OR name_en='Ho Chi Minh City');
END $$;

-- Older loader stored city_name in source_url. Keep the actual Trip page URL.
UPDATE hotels
SET source_url=url
WHERE url IS NOT NULL
  AND (source_url IS NULL OR source_url !~ '^https?://');

-- hotels.price_from is the canonical VI/VND overview quote for compatibility.
UPDATE hotels
SET price_from=(raw_json->>'price_value')::numeric,
    currency=COALESCE(NULLIF(raw_json->>'currency',''), 'VND')
WHERE raw_json->>'price_value' ~ '^[0-9]+([.][0-9]+)?$'
  AND COALESCE(raw_json->>'currency','VND')='VND';

-- Keep legacy localized columns synchronized with Vietnamese translations while
-- callers are migrated to hotel_translations.
UPDATE hotels h
SET name=COALESCE(t.name,h.name),
    address=COALESCE(t.address,h.address),
    description=COALESCE(t.description,h.description),
    hotel_type=COALESCE(t.hotel_type,h.hotel_type)
FROM hotel_translations t
WHERE t.hotel_id=h.id AND t.locale='vi';

-- Enforce language keys at the database boundary.
ALTER TABLE hotel_translations DROP CONSTRAINT IF EXISTS chk_hotel_translations_locale;
ALTER TABLE hotel_translations ADD CONSTRAINT chk_hotel_translations_locale CHECK (locale IN ('vi','en'));
ALTER TABLE location_translations DROP CONSTRAINT IF EXISTS chk_location_translations_locale;
ALTER TABLE location_translations ADD CONSTRAINT chk_location_translations_locale CHECK (locale IN ('vi','en'));
ALTER TABLE room_type_translations DROP CONSTRAINT IF EXISTS chk_room_type_translations_locale;
ALTER TABLE room_type_translations ADD CONSTRAINT chk_room_type_translations_locale CHECK (locale IN ('vi','en'));
ALTER TABLE hotel_amenity_translations DROP CONSTRAINT IF EXISTS chk_hotel_amenity_translations_locale;
ALTER TABLE hotel_amenity_translations ADD CONSTRAINT chk_hotel_amenity_translations_locale CHECK (locale IN ('vi','en'));
ALTER TABLE room_amenity_translations DROP CONSTRAINT IF EXISTS chk_room_amenity_translations_locale;
ALTER TABLE room_amenity_translations ADD CONSTRAINT chk_room_amenity_translations_locale CHECK (locale IN ('vi','en'));
ALTER TABLE hotel_policy_translations DROP CONSTRAINT IF EXISTS chk_hotel_policy_translations_locale;
ALTER TABLE hotel_policy_translations ADD CONSTRAINT chk_hotel_policy_translations_locale CHECK (locale IN ('vi','en'));
ALTER TABLE hotel_nearby_place_translations DROP CONSTRAINT IF EXISTS chk_hotel_nearby_place_translations_locale;
ALTER TABLE hotel_nearby_place_translations ADD CONSTRAINT chk_hotel_nearby_place_translations_locale CHECK (locale IN ('vi','en'));
ALTER TABLE hotel_prices DROP CONSTRAINT IF EXISTS chk_hotel_prices_locale;
ALTER TABLE hotel_prices ALTER COLUMN locale SET DEFAULT 'vi';
ALTER TABLE hotel_prices ADD CONSTRAINT chk_hotel_prices_locale CHECK (locale IN ('vi','en'));
ALTER TABLE hotel_image_categories DROP CONSTRAINT IF EXISTS chk_hotel_image_categories_locale;
ALTER TABLE hotel_image_categories ADD CONSTRAINT chk_hotel_image_categories_locale CHECK (locale IN ('vi','en'));
ALTER TABLE crawl_runs DROP CONSTRAINT IF EXISTS chk_crawl_runs_locale;
ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_runs_locale CHECK (locale IS NULL OR locale IN ('vi','en'));

COMMIT;
