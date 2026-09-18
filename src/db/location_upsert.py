"""Shared PostgreSQL helpers for the locations hierarchy."""
from __future__ import annotations

import hashlib


# Stable city centers used when Trip.com's hotel-list payload only provides
# coordinates for individual properties, not for the city entity itself.
CITY_CENTERS = {
    "301": (10.7756, 106.7019),  # Ho Chi Minh City
}


def _fallback_city_key(name: str) -> str:
    digest = hashlib.sha1(name.strip().casefold().encode("utf-8")).hexdigest()[:20]
    return f"city-name:{digest}"


def upsert_city(
    cur, name: str | None, trip_city_id=None, language: str = "vi"
) -> int | None:
    """Return a stable city location id, reusing a name match when necessary."""
    clean_name = " ".join(str(name or "").split())
    if not clean_name:
        return None

    city_center = CITY_CENTERS.get(str(trip_city_id))
    latitude, longitude = city_center or (None, None)
    official_key = f"city:{trip_city_id}" if trip_city_id not in (None, "") else None
    if official_key:
        cur.execute("SELECT id FROM locations WHERE trip_location_id=%s", (official_key,))
        found = cur.fetchone()
        if found:
            column = "name_en" if language == "en" else "name"
            cur.execute(
                f"""
                UPDATE locations
                SET {column}=%s, type='city', country_code='VN',
                    latitude=COALESCE(latitude, %s),
                    longitude=COALESCE(longitude, %s)
                WHERE id=%s
                """,
                (clean_name, latitude, longitude, found[0]),
            )
            return found[0]

    cur.execute(
        """
        SELECT id, trip_location_id
        FROM locations
        WHERE (
              lower(name)=lower(%s)
              OR lower(COALESCE(name_en, ''))=lower(%s)
              OR EXISTS (
                  SELECT 1 FROM location_translations t
                  WHERE t.location_id=locations.id AND lower(t.name)=lower(%s)
              )
          )
          AND type='city'
          AND COALESCE(country_code, 'VN')='VN'
        ORDER BY id
        LIMIT 1
        """,
        (clean_name, clean_name, clean_name),
    )
    found = cur.fetchone()
    if found:
        location_id, current_key = found
        if official_key and (not current_key or str(current_key).startswith("city-name:")):
            cur.execute(
                "UPDATE locations SET trip_location_id=%s, country_code='VN' WHERE id=%s",
                (official_key, location_id),
            )
        return location_id

    location_key = official_key or _fallback_city_key(clean_name)
    cur.execute(
        """
        INSERT INTO locations
            (trip_location_id, name, type, country_code, latitude, longitude)
        VALUES (%s,%s,'city','VN',%s,%s)
        ON CONFLICT (trip_location_id) DO UPDATE SET
            name=EXCLUDED.name,
            type='city',
            country_code='VN',
            latitude=COALESCE(locations.latitude, EXCLUDED.latitude),
            longitude=COALESCE(locations.longitude, EXCLUDED.longitude)
        RETURNING id
        """,
        (location_key, clean_name, latitude, longitude),
    )
    location_id = cur.fetchone()[0]
    if language == "en":
        cur.execute("UPDATE locations SET name_en=%s WHERE id=%s", (clean_name, location_id))
    return location_id
