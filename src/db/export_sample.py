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


LOCALES = ("vi-VN", "en-US")


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
                    WHERE t.hotel_id=h.id AND t.locale='vi-VN'
                      AND t.raw_json IS NOT NULL
                )
                  AND EXISTS (
                    SELECT 1 FROM hotel_translations t
                    WHERE t.hotel_id=h.id AND t.locale='en-US'
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
                    f"Chỉ có {len(hotels)}/{args.limit} hotel đủ dữ liệu vi-VN và en-US. "
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
                        (hotel_id, list(LOCALES)),
                    ),
                    "location_translations": rows(
                        cur,
                        """
                        SELECT locale, name
                        FROM location_translations
                        WHERE location_id=%s AND locale=ANY(%s)
                        ORDER BY locale
                        """,
                        (hotel.get("location_id"), list(LOCALES)),
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
                        (list(LOCALES), hotel_id),
                    ),
                    "amenities": rows(
                        cur,
                        """
                        SELECT a.id, a.amenity_code,
                               jsonb_object_agg(t.locale, jsonb_build_object(
                                   'name', t.amenity_name, 'category', t.category
                               ) ORDER BY t.locale) AS translations
                        FROM hotel_amenities a
                        JOIN hotel_amenity_translations t
                          ON t.hotel_amenity_id=a.id AND t.locale=ANY(%s)
                        WHERE a.hotel_id=%s
                        GROUP BY a.id, a.amenity_code
                        ORDER BY a.id
                        """,
                        (list(LOCALES), hotel_id),
                    ),
                    "room_types": rows(
                        cur,
                        """
                        SELECT r.id, r.trip_room_id, r.max_occupancy, r.area_sqm,
                               jsonb_object_agg(t.locale, jsonb_build_object(
                                   'name', t.name, 'bed_type', t.bed_type,
                                   'crawled_at', t.crawled_at
                               ) ORDER BY t.locale) AS translations
                        FROM room_types r
                        JOIN room_type_translations t
                          ON t.room_type_id=r.id AND t.locale=ANY(%s)
                        WHERE r.hotel_id=%s
                        GROUP BY r.id, r.trip_room_id, r.max_occupancy, r.area_sqm
                        ORDER BY r.id
                        """,
                        (list(LOCALES), hotel_id),
                    ),
                    "prices": rows(
                        cur,
                        """
                        SELECT DISTINCT ON (p.room_type_id, p.locale, p.currency)
                               p.room_type_id, p.check_in, p.check_out, p.price,
                               p.currency, p.locale, p.tax_included,
                               p.captured_at, p.captured_date
                        FROM hotel_prices p
                        WHERE p.hotel_id=%s AND p.locale=ANY(%s)
                        ORDER BY p.room_type_id, p.locale, p.currency, p.captured_at DESC
                        """,
                        (hotel_id, list(LOCALES)),
                    ),
                }
                result.append(item)

    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "hotel_count": len(result),
        "locales": list(LOCALES),
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
