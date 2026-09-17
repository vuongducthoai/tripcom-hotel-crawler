-- Keep hotels language-neutral. Localized content belongs to hotel_translations.

BEGIN;

-- Preserve every useful legacy value before dropping duplicate columns.
INSERT INTO hotel_translations (
    hotel_id, locale, name, address, description, hotel_type,
    source_url, raw_json, crawled_at
)
SELECT
    id, 'vi', name, address, description, hotel_type,
    url, raw_json, last_seen_at
FROM hotels
ON CONFLICT (hotel_id, locale) DO UPDATE SET
    name=COALESCE(hotel_translations.name, EXCLUDED.name),
    address=COALESCE(hotel_translations.address, EXCLUDED.address),
    description=COALESCE(hotel_translations.description, EXCLUDED.description),
    hotel_type=COALESCE(hotel_translations.hotel_type, EXCLUDED.hotel_type),
    source_url=COALESCE(hotel_translations.source_url, EXCLUDED.source_url),
    updated_at=now();

-- SEO booking prompts are not hotel descriptions.
UPDATE hotel_translations
SET description=NULL,
    updated_at=now()
WHERE description IS NOT NULL
  AND (
      lower(description) LIKE '%bạn đang tìm đặt phòng%'
      OR lower(description) LIKE '%hãy chọn phòng cho bạn%'
      OR lower(description) LIKE '%so sánh giá cả và đặt%'
      OR lower(description) LIKE '%looking to book%'
      OR lower(description) LIKE '%select rooms%'
      OR lower(description) LIKE '%compare prices and book%'
  );

INSERT INTO hotel_translations (hotel_id, locale, name, source_url, crawled_at)
SELECT id, 'en', name_en, url, last_seen_at
FROM hotels
WHERE name_en IS NOT NULL AND btrim(name_en)<>''
ON CONFLICT (hotel_id, locale) DO UPDATE SET
    name=COALESCE(hotel_translations.name, EXCLUDED.name),
    source_url=COALESCE(hotel_translations.source_url, EXCLUDED.source_url),
    updated_at=now();

DROP INDEX IF EXISTS idx_hotels_cheap;

ALTER TABLE hotels
    DROP COLUMN name,
    DROP COLUMN name_en,
    DROP COLUMN address,
    DROP COLUMN description,
    DROP COLUMN hotel_type,
    DROP COLUMN source_url,
    DROP COLUMN is_cheap_listing;

COMMENT ON TABLE hotels IS
    'Language-neutral Trip.com property identity and latest overview metrics.';
COMMENT ON COLUMN hotels.price_from IS
    'Temporary canonical VI/VND overview quote; migrate to price snapshots before removing.';
COMMENT ON COLUMN hotels.currency IS
    'Currency of temporary overview quote; currently canonicalized to VND.';

COMMIT;
