"""Shared PostgreSQL helpers for the locations hierarchy."""
from __future__ import annotations

import hashlib


def _fallback_city_key(name: str) -> str:
    digest = hashlib.sha1(name.strip().casefold().encode("utf-8")).hexdigest()[:20]
    return f"city-name:{digest}"


def upsert_city(cur, name: str | None, trip_city_id=None) -> int | None:
    """Return a stable city location id, reusing a name match when necessary."""
    clean_name = " ".join(str(name or "").split())
    if not clean_name:
        return None

    official_key = f"city:{trip_city_id}" if trip_city_id not in (None, "") else None
    if official_key:
        cur.execute("SELECT id FROM locations WHERE trip_location_id=%s", (official_key,))
        found = cur.fetchone()
        if found:
            cur.execute(
                "UPDATE locations SET name=%s, type='city', country_code='VN' WHERE id=%s",
                (clean_name, found[0]),
            )
            return found[0]

    cur.execute(
        """
        SELECT id, trip_location_id
        FROM locations
        WHERE lower(name)=lower(%s) AND type='city'
          AND COALESCE(country_code, 'VN')='VN'
        ORDER BY id
        LIMIT 1
        """,
        (clean_name,),
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
        INSERT INTO locations (trip_location_id, name, type, country_code)
        VALUES (%s,%s,'city','VN')
        ON CONFLICT (trip_location_id) DO UPDATE SET
            name=EXCLUDED.name,
            type='city',
            country_code='VN'
        RETURNING id
        """,
        (location_key, clean_name),
    )
    return cur.fetchone()[0]
