BEGIN;

ALTER TABLE public.hotel_amenities
    ADD COLUMN IF NOT EXISTS is_available BOOLEAN;

COMMENT ON COLUMN public.hotel_amenities.is_available IS
    'Source availability: true=provided, false=unavailable/struck through, NULL=unknown. Independent of fee status.';

COMMIT;
