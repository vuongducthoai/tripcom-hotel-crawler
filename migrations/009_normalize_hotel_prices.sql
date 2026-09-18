-- Store all VND/USD quotes in hotel_prices, independently from language.

BEGIN;

ALTER TABLE hotel_prices RENAME COLUMN locale TO language;
ALTER TABLE hotel_prices
    ADD COLUMN price_type TEXT NOT NULL DEFAULT 'room';

ALTER TABLE hotel_prices DROP CONSTRAINT IF EXISTS chk_hotel_prices_locale;
ALTER TABLE hotel_prices
    ADD CONSTRAINT chk_hotel_prices_language CHECK (language IN ('vi','en'));
ALTER TABLE hotel_prices
    ADD CONSTRAINT chk_hotel_prices_type CHECK (price_type IN ('overview','room'));
ALTER TABLE hotel_prices
    ADD CONSTRAINT chk_hotel_prices_currency CHECK (currency IN ('VND','USD'));
ALTER TABLE hotel_prices
    ADD CONSTRAINT chk_hotel_prices_dates CHECK (check_out > check_in);
ALTER TABLE hotel_prices
    ADD CONSTRAINT chk_hotel_prices_amount CHECK (price IS NULL OR price >= 0);

DROP INDEX IF EXISTS uq_hotel_prices_daily_market;
DROP INDEX IF EXISTS idx_hotel_prices_locale_currency;

CREATE UNIQUE INDEX uq_hotel_prices_room_daily
    ON hotel_prices (
        hotel_id, room_type_id, check_in, check_out,
        currency, language, captured_date
    )
    WHERE price_type='room' AND room_type_id IS NOT NULL;

CREATE UNIQUE INDEX uq_hotel_prices_overview_daily
    ON hotel_prices (
        hotel_id, check_in, check_out, currency, language, captured_date
    )
    WHERE price_type='overview' AND room_type_id IS NULL;

CREATE INDEX idx_hotel_prices_language_currency
    ON hotel_prices(language, currency, captured_date);

-- Preserve the existing overview quote before removing it from hotels.
INSERT INTO hotel_prices (
    hotel_id, room_type_id, check_in, check_out, price, currency,
    language, price_type, tax_included, captured_at, captured_date
)
SELECT
    id,
    NULL,
    (first_seen_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date + 1,
    (first_seen_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date + 2,
    price_from,
    currency,
    'vi',
    'overview',
    NULL,
    first_seen_at,
    (first_seen_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
FROM hotels
WHERE price_from IS NOT NULL
ON CONFLICT (
    hotel_id, check_in, check_out, currency, language, captured_date
) WHERE price_type='overview' AND room_type_id IS NULL
DO UPDATE SET
    price=EXCLUDED.price,
    captured_at=GREATEST(hotel_prices.captured_at, EXCLUDED.captured_at);

DROP INDEX IF EXISTS idx_hotels_price;
ALTER TABLE hotels
    DROP COLUMN price_from,
    DROP COLUMN currency;

COMMENT ON COLUMN hotel_prices.language IS
    'Content language key (vi/en), independent from quote currency.';
COMMENT ON COLUMN hotel_prices.price_type IS
    'overview for hotel-list minimum quote; room for a room inventory quote.';

COMMIT;
