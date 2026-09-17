"""Export a compact bilingual hotel sample for review.

The output intentionally excludes raw_json/network payloads so it is small
enough to send to a mentor while still showing every normalized table.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg2
from psycopg2.extras import RealDictCursor

import config


LANGUAGES = ("vi", "en")


def rows(cur, query: str, params: tuple) -> list[dict]:
    cur.execute(query, params)
    return [dict(row) for row in cur.fetchall()]


def main(args: argparse.Namespace) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(args.output) if args.output else (
        config.DATA_DIR / f"mentor_sample_{args.limit}_hotels_{stamp}.json"
    )
    if not output.is_absolute():
        output = config.ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)

    with psycopg2.connect(config.dsn(), cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT h.id, h.trip_hotel_id, h.url, h.latitude, h.longitude,
                       h.star_rating, h.review_score, h.review_count,
                       h.location_id, h.last_seen_at
                FROM hotels h
                WHERE EXISTS (
                    SELECT 1 FROM hotel_translations t
                    WHERE t.hotel_id=h.id AND t.locale='vi'
                      AND t.raw_json IS NOT NULL
                )
                  AND EXISTS (
                    SELECT 1 FROM hotel_translations t
                    WHERE t.hotel_id=h.id AND t.locale='en'
                      AND t.raw_json IS NOT NULL
                  )
                ORDER BY h.id
                LIMIT %s
                """,
                (args.limit,),
            )
            hotels = [dict(row) for row in cur.fetchall()]
            if len(hotels) < args.limit:
                raise SystemExit(
                    f"Chỉ có {len(hotels)}/{args.limit} hotel đủ dữ liệu vi và en. "
                    "Hãy crawl + import detail cả hai locale trước."
                )

            result = []
            for hotel in hotels:
                hotel_id = hotel.pop("id")
                item = {
                    "hotel": hotel,
                    "translations": rows(
                        cur,
                        """
                        SELECT locale, name, address, description, hotel_type,
                               source_url, crawled_at, updated_at
                        FROM hotel_translations
                        WHERE hotel_id=%s AND locale=ANY(%s)
                        ORDER BY locale
                        """,
                        (hotel_id, list(LANGUAGES)),
                    ),
                    "location_translations": rows(
                        cur,
                        """
                        SELECT locale, name
                        FROM location_translations
                        WHERE location_id=%s AND locale=ANY(%s)
                        ORDER BY locale
                        """,
                        (hotel.get("location_id"), list(LANGUAGES)),
                    ) if hotel.get("location_id") else [],
                    "images": rows(
                        cur,
                        """
                        SELECT i.url, i.sort_order,
                               COALESCE(
                                   jsonb_agg(
                                       jsonb_build_object(
                                           'code', c.category_code,
                                           'source', c.source,
                                           'locale', c.locale,
                                           'name', c.category_name,
                                           'image_title', c.image_title,
                                           'sort_order', c.sort_order
                                       ) ORDER BY c.locale, c.sort_order
                                   ) FILTER (WHERE c.hotel_image_id IS NOT NULL),
                                   '[]'::jsonb
                               ) AS categories
                        FROM hotel_images i
                        LEFT JOIN hotel_image_categories c
                          ON c.hotel_image_id=i.id AND c.locale=ANY(%s)
                        WHERE i.hotel_id=%s
                        GROUP BY i.id, i.url, i.sort_order
                        ORDER BY i.sort_order, i.id
                        """,
                        (list(LANGUAGES), hotel_id),
                    ),
                    "amenities": rows(
                        cur,
                        """
                        SELECT a.id, a.amenity_code, a.free_type, a.is_highlight,
                               (to_jsonb(a)->>'is_available')::boolean AS is_available,
                               jsonb_object_agg(t.locale, jsonb_build_object(
                                   'name', t.amenity_name, 'category', t.category,
                                   'fee_label', t.fee_label,
                                   'additional_info', t.additional_info
                               ) ORDER BY t.locale) AS translations
                        FROM hotel_amenities a
                        JOIN hotel_amenity_translations t
                          ON t.hotel_amenity_id=a.id AND t.locale=ANY(%s)
                        WHERE a.hotel_id=%s
                        GROUP BY a.id, a.amenity_code, a.free_type, a.is_highlight
                        ORDER BY a.id
                        """,
                        (list(LANGUAGES), hotel_id),
                    ),
                    "nearby_places": rows(
                        cur,
                        """
                        SELECT p.trip_poi_id, p.category_code, p.poi_type,
                               p.latitude, p.longitude, p.distance_km, p.arrival_type,
                               jsonb_object_agg(t.locale, jsonb_build_object(
                                   'name', t.name, 'category', t.category_name,
                                   'distance_text', t.distance_text,
                                   'description', t.description, 'tags', t.tags
                               ) ORDER BY t.locale) AS translations
                        FROM hotel_nearby_places p
                        JOIN hotel_nearby_place_translations t
                          ON t.nearby_place_id=p.id AND t.locale=ANY(%s)
                        WHERE p.hotel_id=%s
                        GROUP BY p.id ORDER BY p.sort_order, p.id
                        """,
                        (list(LANGUAGES), hotel_id),
                    ),
                    "policies": rows(
                        cur,
                        """
                        SELECT p.policy_code, p.sort_order,
                               jsonb_object_agg(t.locale, jsonb_build_object(
                                   'title', t.title,
                                   'description', t.description
                               ) ORDER BY t.locale) AS translations
                        FROM hotel_policies p
                        JOIN hotel_policy_translations t
                          ON t.hotel_policy_id=p.id AND t.locale=ANY(%s)
                        WHERE p.hotel_id=%s
                        GROUP BY p.id, p.policy_code, p.sort_order
                        ORDER BY p.sort_order, p.id
                        """,
                        (list(LANGUAGES), hotel_id),
                    ),
                    "room_types": rows(
                        cur,
                        """
                        SELECT r.id, r.trip_room_id, r.max_occupancy, r.area_sqm,
                               r.bedroom_count, r.bathroom_count, r.bed_count,
                               jsonb_object_agg(t.locale, jsonb_build_object(
                                   'name', t.name, 'bed_type', t.bed_type,
                                   'view_name', t.view_name,
                                   'smoking_policy', t.smoking_policy,
                                   'wifi', t.wifi,
                                   'floor_label', t.floor_label,
                                   'extra_bed_policy', t.extra_bed_policy,
                                   'crawled_at', t.crawled_at
                               ) ORDER BY t.locale) AS translations,
                               COALESCE((
                                   SELECT jsonb_agg(jsonb_build_object(
                                       'url', i.url,
                                       'category_code', i.category_code,
                                       'sort_order', i.sort_order
                                   ) ORDER BY i.sort_order, i.id)
                                   FROM room_images i WHERE i.room_type_id=r.id
                               ), '[]'::jsonb) AS images,
                               COALESCE((
                                   SELECT jsonb_agg(jsonb_build_object(
                                       'key', a.amenity_key,
                                       'code', a.amenity_code,
                                       'category_code', a.category_code,
                                       'is_highlight', a.is_highlight,
                                       'free_type', a.free_type,
                                       'translations', (
                                           SELECT jsonb_object_agg(at.locale, jsonb_build_object(
                                               'name', at.amenity_name,
                                               'category', at.category_name,
                                               'additional_info', at.additional_info
                                           ) ORDER BY at.locale)
                                           FROM room_amenity_translations at
                                           WHERE at.room_amenity_id=a.id
                                             AND at.locale=ANY(%s)
                                       )
                                   ) ORDER BY a.id)
                                   FROM room_amenities a WHERE a.room_type_id=r.id
                               ), '[]'::jsonb) AS amenities
                        FROM room_types r
                        JOIN room_type_translations t
                          ON t.room_type_id=r.id AND t.locale=ANY(%s)
                        WHERE r.hotel_id=%s
                        GROUP BY r.id, r.trip_room_id, r.max_occupancy, r.area_sqm,
                                 r.bedroom_count, r.bathroom_count, r.bed_count
                        ORDER BY r.id
                        """,
                        (list(LANGUAGES), list(LANGUAGES), hotel_id),
                    ),
                    "prices": rows(
                        cur,
                        """
                        SELECT DISTINCT ON (p.room_type_id, p.language, p.currency, p.price_type)
                               p.room_type_id, p.check_in, p.check_out, p.price,
                               p.currency, p.language, p.price_type, p.tax_included,
                               p.captured_at, p.captured_date
                        FROM hotel_prices p
                        WHERE p.hotel_id=%s AND p.language=ANY(%s)
                        ORDER BY p.room_type_id, p.language, p.currency,
                                 p.price_type, p.captured_at DESC
                        """,
                        (hotel_id, list(LANGUAGES)),
                    ),
                }
                result.append(item)

    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "hotel_count": len(result),
        "languages": list(LANGUAGES),
        "hotels": result,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Đã xuất {len(result)} hotel → {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5, help="số hotel cần xuất")
    parser.add_argument("--output", help="đường dẫn JSON đầu ra")
    main(parser.parse_args())
