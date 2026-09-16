"""Nạp output crawl_detail.py vào hotels và các bảng detail.

    python src/db/detail_loader.py hotel_details_YYYYMMDD_HHMMSS.json
    python src/db/detail_loader.py hotel_details_....json

Mặc định nạp cả snapshot giá; dùng --no-prices nếu chỉ muốn
nạp thông tin tĩnh.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg2
from psycopg2.extras import Json, execute_values

import config
from db.location_upsert import upsert_city

VIETNAM_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")


def captured_at(value: str | None) -> datetime:
    try:
        result = datetime.fromisoformat(value) if value else datetime.now()
    except ValueError:
        result = datetime.now()
    if result.tzinfo is None:
        result = result.replace(tzinfo=VIETNAM_TZ)
    return result


def latest_file() -> Path:
    files = sorted(config.DATA_DIR.glob("hotel_details_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("Chưa có hotel_details_*.json. Chạy crawl_detail.py trước.")
    return files[-1]


def resolve_file(value: str | None) -> Path:
    path = Path(value) if value else latest_file()
    if not path.is_absolute() and not path.exists():
        path = config.DATA_DIR / path.name
    if not path.exists():
        raise SystemExit(f"Không tìm thấy {value}")
    return path


def main(args: argparse.Namespace) -> None:
    path = resolve_file(args.file)
    payload = json.loads(path.read_text(encoding="utf-8"))
    details = payload.get("details") or []
    locale = args.locale or payload.get("locale") or "vi-VN"
    currency = (args.currency or payload.get("currency") or "VND").upper()
    stats = {
        "hotels": 0, "missing_hotels": 0, "failed": 0,
        "locations": 0, "images": 0, "image_categories": 0,
        "amenities": 0, "rooms": 0,
        "prices": 0, "prices_skipped_no_dates": 0,
    }
    location_ids: set[int] = set()
    save_prices = not args.no_prices

    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.hotel_translations')")
        if cur.fetchone()[0] is None:
            raise SystemExit("Chưa có bảng translation. Hãy chạy migrations/002_multilingual.sql trước.")
        cur.execute("SELECT to_regclass('public.hotel_image_categories')")
        if cur.fetchone()[0] is None:
            raise SystemExit(
                "Chưa có bảng hotel_image_categories. Hãy chạy lại "
                "migrations/002_multilingual.sql trước."
            )
        replaced_hotels = 0
        if args.replace_existing:
            trip_ids = [
                str(item.get("trip_hotel_id"))
                for item in details
                if item.get("trip_hotel_id") and item.get("success")
            ]
            cur.execute(
                "SELECT id FROM hotels WHERE trip_hotel_id = ANY(%s)",
                (trip_ids,),
            )
            replace_ids = [row[0] for row in cur.fetchall()]
            replaced_hotels = len(replace_ids)
            if replace_ids:
                # Only replace the selected market. Shared entities and translations
                # belonging to other languages must survive an English re-import.
                cur.execute(
                    "DELETE FROM hotel_prices WHERE hotel_id=ANY(%s) AND locale=%s AND currency=%s",
                    (replace_ids, locale, currency),
                )
                cur.execute(
                    "DELETE FROM room_type_translations rt USING room_types r "
                    "WHERE rt.room_type_id=r.id AND r.hotel_id=ANY(%s) AND rt.locale=%s",
                    (replace_ids, locale),
                )
                cur.execute(
                    "DELETE FROM hotel_amenity_translations t USING hotel_amenities a "
                    "WHERE t.hotel_amenity_id=a.id AND a.hotel_id=ANY(%s) AND t.locale=%s",
                    (replace_ids, locale),
                )
                cur.execute(
                    "DELETE FROM hotel_translations WHERE hotel_id=ANY(%s) AND locale=%s",
                    (replace_ids, locale),
                )
                cur.execute(
                    "DELETE FROM hotel_image_categories c USING hotel_images i "
                    "WHERE c.hotel_image_id=i.id AND i.hotel_id=ANY(%s) AND c.locale=%s",
                    (replace_ids, locale),
                )
                cur.execute(
                    """
                    UPDATE hotels SET
                        raw_json=jsonb_set(
                            COALESCE(raw_json, '{}'::jsonb), '{detail_by_locale}',
                            COALESCE(raw_json->'detail_by_locale', '{}'::jsonb)-%s, true
                        )
                    WHERE id = ANY(%s)
                    """,
                    (locale, replace_ids),
                )

        cur.execute(
            "INSERT INTO crawl_runs (target, status, locale, currency) "
            "VALUES (%s, 'running', %s, %s) RETURNING id",
            (f"detail:{path.name}", locale, currency),
        )
        run_id = cur.fetchone()[0]

        for detail in details:
            hotel_id = str(detail.get("trip_hotel_id") or "")
            if not hotel_id:
                stats["failed"] += 1
                continue
            if not detail.get("success"):
                stats["failed"] += 1
                cur.execute(
                    "INSERT INTO crawl_errors (run_id, url, error_message, payload) "
                    "VALUES (%s,%s,%s,%s)",
                    (run_id, detail.get("url"), detail.get("error"), Json({"trip_hotel_id": hotel_id})),
                )
                continue

            cur.execute(
                "SELECT id, location_id, raw_json->>'city_name' "
                "FROM hotels WHERE trip_hotel_id=%s",
                (hotel_id,),
            )
            found = cur.fetchone()
            if not found:
                stats["missing_hotels"] += 1
                continue
            db_hotel_id, current_location_id, stored_city_name = found
            location_id = current_location_id or upsert_city(
                cur, detail.get("city_name") or stored_city_name
            )
            if location_id:
                location_ids.add(location_id)
                localized_city = detail.get("city_name")
                if not localized_city and locale == "vi-VN":
                    localized_city = stored_city_name
                if localized_city:
                    cur.execute(
                        """
                        INSERT INTO location_translations (location_id, locale, name)
                        VALUES (%s,%s,%s)
                        ON CONFLICT (location_id, locale) DO UPDATE SET
                            name=EXCLUDED.name, updated_at=now()
                        """,
                        (location_id, locale, localized_city),
                    )

            normalized_for_raw = {k: v for k, v in detail.items() if k != "rooms"}
            normalized_for_raw["rooms"] = [
                {k: v for k, v in room.items() if k != "raw"}
                for room in (detail.get("rooms") or [])
            ]
            cur.execute(
                """
                UPDATE hotels SET
                    location_id = COALESCE(%s, location_id),
                    description = CASE WHEN %s='vi-VN' THEN COALESCE(%s, description) ELSE description END,
                    hotel_type = CASE WHEN %s='vi-VN' THEN %s ELSE hotel_type END,
                    raw_json = jsonb_set(
                        COALESCE(raw_json, '{}'::jsonb), '{detail_by_locale}',
                        COALESCE(raw_json->'detail_by_locale', '{}'::jsonb)
                            || jsonb_build_object(%s, %s::jsonb), true
                    ) || CASE WHEN %s='vi-VN'
                              THEN jsonb_build_object('detail', %s::jsonb)
                              ELSE '{}'::jsonb END,
                    last_seen_at = now()
                WHERE id = %s
                """,
                (location_id, locale, detail.get("description"), locale,
                 detail.get("hotel_type"), locale, Json(normalized_for_raw),
                 locale, Json(normalized_for_raw), db_hotel_id),
            )
            cur.execute(
                """
                INSERT INTO hotel_translations
                    (hotel_id, locale, name, address, description, hotel_type,
                     source_url, raw_json, crawled_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (hotel_id, locale) DO UPDATE SET
                    name=COALESCE(EXCLUDED.name, hotel_translations.name),
                    address=COALESCE(EXCLUDED.address, hotel_translations.address),
                    description=COALESCE(EXCLUDED.description, hotel_translations.description),
                    hotel_type=EXCLUDED.hotel_type,
                    source_url=COALESCE(EXCLUDED.source_url, hotel_translations.source_url),
                    raw_json=EXCLUDED.raw_json,
                    crawled_at=EXCLUDED.crawled_at,
                    updated_at=now()
                """,
                (db_hotel_id, locale, detail.get("name"), detail.get("address"),
                 detail.get("description"), detail.get("hotel_type"), detail.get("url"),
                 Json(normalized_for_raw), captured_at(detail.get("crawled_at"))),
            )
            stats["hotels"] += 1

            image_rows = [
                (db_hotel_id, image.get("url"), image.get("category"), image.get("sort_order", 0))
                for image in (detail.get("images") or []) if image.get("url")
            ]
            if image_rows:
                execute_values(cur, """
                    INSERT INTO hotel_images (hotel_id, url, category, sort_order) VALUES %s
                    ON CONFLICT (hotel_id, url) DO UPDATE SET
                        category=COALESCE(hotel_images.category, EXCLUDED.category),
                        sort_order=EXCLUDED.sort_order
                """, image_rows)
                stats["images"] += len(image_rows)

                category_rows = []
                for image in detail.get("images") or []:
                    if not image.get("url"):
                        continue
                    for category in image.get("categories") or []:
                        if not category.get("code") or not category.get("name"):
                            continue
                        category_rows.append((
                            db_hotel_id, image["url"], str(category["code"]),
                            category.get("source") or "hotel", locale,
                            category["name"], category.get("image_title"),
                            category.get("sort_order", 0),
                        ))
                if category_rows:
                    execute_values(cur, """
                        INSERT INTO hotel_image_categories
                            (hotel_image_id, category_code, source, locale,
                             category_name, image_title, sort_order)
                        SELECT i.id, v.category_code, v.source, v.locale,
                               v.category_name, v.image_title, v.sort_order
                        FROM (VALUES %s) AS v(
                            hotel_id, url, category_code, source, locale,
                            category_name, image_title, sort_order
                        )
                        JOIN hotel_images i
                          ON i.hotel_id=v.hotel_id AND i.url=v.url
                        ON CONFLICT (hotel_image_id, category_code, locale) DO UPDATE SET
                            source=EXCLUDED.source,
                            category_name=EXCLUDED.category_name,
                            image_title=COALESCE(EXCLUDED.image_title,
                                                 hotel_image_categories.image_title),
                            sort_order=EXCLUDED.sort_order,
                            updated_at=now()
                    """, category_rows)
                    stats["image_categories"] += len(category_rows)

            for item in detail.get("amenities") or []:
                amenity_name = item.get("name")
                if not amenity_name:
                    continue
                amenity_code = item.get("code")
                amenity_db_id = None
                if amenity_code is not None:
                    cur.execute(
                        "SELECT id FROM hotel_amenities "
                        "WHERE hotel_id=%s AND amenity_code=%s ORDER BY id LIMIT 1",
                        (db_hotel_id, str(amenity_code)),
                    )
                    found_amenity = cur.fetchone()
                    amenity_db_id = found_amenity[0] if found_amenity else None
                if amenity_db_id is None:
                    cur.execute(
                        """
                        INSERT INTO hotel_amenities
                            (hotel_id, amenity_code, amenity_name, category)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (hotel_id, amenity_name) DO UPDATE SET
                            amenity_code=COALESCE(EXCLUDED.amenity_code, hotel_amenities.amenity_code),
                            category=CASE WHEN %s='vi-VN'
                                          THEN COALESCE(EXCLUDED.category, hotel_amenities.category)
                                          ELSE hotel_amenities.category END
                        RETURNING id
                        """,
                        (db_hotel_id, amenity_code, amenity_name, item.get("category"), locale),
                    )
                    amenity_db_id = cur.fetchone()[0]
                elif locale == "vi-VN" and item.get("category"):
                    cur.execute(
                        "UPDATE hotel_amenities SET category=%s WHERE id=%s",
                        (item.get("category"), amenity_db_id),
                    )
                cur.execute(
                    """
                    INSERT INTO hotel_amenity_translations
                        (hotel_amenity_id, locale, amenity_name, category)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (hotel_amenity_id, locale) DO UPDATE SET
                        amenity_name=EXCLUDED.amenity_name,
                        category=COALESCE(EXCLUDED.category,
                                          hotel_amenity_translations.category),
                        updated_at=now()
                    """,
                    (amenity_db_id, locale, amenity_name, item.get("category")),
                )
                stats["amenities"] += 1

            for room in detail.get("rooms") or []:
                if not room.get("trip_room_id") or not room.get("name"):
                    continue
                cur.execute("""
                    INSERT INTO room_types
                        (hotel_id, trip_room_id, name, bed_type, max_occupancy, area_sqm, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (hotel_id, trip_room_id) DO UPDATE SET
                        name=CASE WHEN %s='vi-VN' THEN EXCLUDED.name ELSE room_types.name END,
                        bed_type=CASE WHEN %s='vi-VN'
                                      THEN COALESCE(EXCLUDED.bed_type, room_types.bed_type)
                                      ELSE room_types.bed_type END,
                        max_occupancy=COALESCE(EXCLUDED.max_occupancy, room_types.max_occupancy),
                        area_sqm=COALESCE(EXCLUDED.area_sqm, room_types.area_sqm),
                        raw_json=CASE WHEN %s='vi-VN' THEN EXCLUDED.raw_json ELSE room_types.raw_json END
                    RETURNING id
                """, (
                    db_hotel_id, room["trip_room_id"], room["name"], room.get("bed_type"),
                    room.get("max_occupancy"), room.get("area_sqm"), Json(room.get("raw") or room),
                    locale, locale, locale,
                ))
                room_db_id = cur.fetchone()[0]
                stats["rooms"] += 1

                cur.execute(
                    """
                    INSERT INTO room_type_translations
                        (room_type_id, locale, name, bed_type, raw_json, crawled_at)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (room_type_id, locale) DO UPDATE SET
                        name=EXCLUDED.name,
                        bed_type=COALESCE(EXCLUDED.bed_type, room_type_translations.bed_type),
                        raw_json=EXCLUDED.raw_json,
                        crawled_at=EXCLUDED.crawled_at,
                        updated_at=now()
                    """,
                    (room_db_id, locale, room["name"], room.get("bed_type"),
                     Json(room.get("raw") or room), captured_at(detail.get("crawled_at"))),
                )

                if save_prices and room.get("price") is not None:
                    check_in, check_out = detail.get("check_in"), detail.get("check_out")
                    if not check_in or not check_out:
                        stats["prices_skipped_no_dates"] += 1
                        continue
                    captured = captured_at(detail.get("crawled_at"))
                    cur.execute(
                        """
                        SELECT id FROM hotel_prices
                        WHERE hotel_id=%s AND room_type_id=%s
                          AND check_in=%s AND check_out=%s
                          AND locale=%s AND currency=%s AND captured_date=%s
                        ORDER BY id LIMIT 1
                        """,
                        (db_hotel_id, room_db_id, check_in, check_out,
                         locale, room.get("currency") or currency, captured.date()),
                    )
                    existing_price = cur.fetchone()
                    if existing_price:
                        cur.execute(
                            """
                            UPDATE hotel_prices SET price=%s, currency=%s,
                                locale=%s, tax_included=%s, captured_at=%s,
                                captured_date=%s
                            WHERE id=%s
                            """,
                            (room.get("price"), room.get("currency") or currency, locale,
                             room.get("tax_included"), captured, captured.date(),
                             existing_price[0]),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO hotel_prices
                                (hotel_id, room_type_id, check_in, check_out, price,
                                 currency, locale, tax_included, captured_at, captured_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (db_hotel_id, room_db_id, check_in, check_out,
                             room.get("price"), room.get("currency") or currency,
                             locale, room.get("tax_included"), captured, captured.date()),
                        )
                    stats["prices"] += 1

        stats["locations"] = len(location_ids)
        stats["replaced_hotels"] = replaced_hotels

        cur.execute(
            "UPDATE crawl_runs SET status='success', finished_at=now(), stats=%s WHERE id=%s",
            (Json({"file": path.name, "locale": locale, "currency": currency, **stats}), run_id),
        )
        if args.dry_run:
            conn.rollback()

    prefix = "DRY RUN (không commit)" if args.dry_run else "Đã nạp detail"
    print(f"{prefix} từ {path.name}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.no_prices:
        print("Đã bỏ qua snapshot giá theo yêu cầu --no-prices.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="hotel_details_*.json trong output/data/")
    ap.add_argument("--prices", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-prices", action="store_true", help="không ghi snapshot hotel_prices")
    ap.add_argument("--locale", help="ghi đè locale trong manifest, ví dụ en-US")
    ap.add_argument("--currency", help="ghi đè currency trong manifest, ví dụ USD")
    ap.add_argument("--dry-run", action="store_true", help="kiểm tra SQL rồi rollback, không ghi DB")
    ap.add_argument(
        "--replace-existing", action="store_true",
        help="xóa bản dịch và giá cũ của đúng locale/currency rồi import lại",
    )
    main(ap.parse_args())
