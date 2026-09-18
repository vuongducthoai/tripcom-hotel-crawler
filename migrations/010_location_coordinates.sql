-- Backfill the canonical city point because Trip.com's hotel-list response
-- contains property coordinates but omits coordinates for the city entity.
UPDATE locations
SET latitude = COALESCE(latitude, 10.7756),
    longitude = COALESCE(longitude, 106.7019)
WHERE trip_location_id = 'city:301';
