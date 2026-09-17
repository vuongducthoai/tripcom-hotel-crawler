-- Localized hotel-level policies shown in the Trip.com Policies panel.
-- Safe to run more than once.

BEGIN;

CREATE TABLE IF NOT EXISTS hotel_policies (
    id           BIGSERIAL PRIMARY KEY,
    hotel_id     BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    policy_code  TEXT NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (hotel_id, policy_code)
);

CREATE INDEX IF NOT EXISTS idx_hotel_policies_hotel
    ON hotel_policies(hotel_id, sort_order);

CREATE TABLE IF NOT EXISTS hotel_policy_translations (
    hotel_policy_id BIGINT NOT NULL REFERENCES hotel_policies(id) ON DELETE CASCADE,
    locale          TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    raw_json        JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hotel_policy_id, locale),
    CONSTRAINT chk_hotel_policy_translations_locale
        CHECK (locale ~ '^[a-z]{2}(-[A-Z]{2})?$')
);

CREATE INDEX IF NOT EXISTS idx_hotel_policy_translations_locale
    ON hotel_policy_translations(locale);

COMMENT ON TABLE hotel_policies IS
    'Stable hotel-level policy identities such as check-in, children, pets and payment.';
COMMENT ON TABLE hotel_policy_translations IS
    'Localized policy titles and descriptions captured from Trip.com.';

COMMIT;
