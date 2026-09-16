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
    stats = {
        "hotels": 0, "missing_hotels": 0, "failed": 0,
        "locations": 0, "images": 0, "amenities": 0, "rooms": 0,
        "prices": 0, "prices_skipped_no_dates": 0,
    }
    location_ids: set[int] = set()
    save_prices = not args.no_prices

    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
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
                # Clear only detail-owned data. The hotels overview rows and their
                # city association remain intact. Everything is in this transaction,
                # so a failed import restores the old detail automatically.
                cur.execute("DELETE FROM hotel_prices WHERE hotel_id = ANY(%s)", (replace_ids,))
                cur.execute("DELETE FROM room_types WHERE hotel_id = ANY(%s)", (replace_ids,))
                cur.execute("DELETE FROM hotel_images WHERE hotel_id = ANY(%s)", (replace_ids,))
                cur.execute("DELETE FROM hotel_amenities WHERE hotel_id = ANY(%s)", (replace_ids,))
                cur.execute(
                    """
                    UPDATE hotels SET description=NULL, hotel_type=NULL,
                        raw_json=COALESCE(raw_json, '{}'::jsonb)-'detail'
                    WHERE id = ANY(%s)
                    """,
                    (replace_ids,),
                )

        cur.execute(
            "INSERT INTO crawl_runs (target, status) VALUES (%s, 'running') RETURNING id",
            (f"detail:{path.name}",),
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
                "SELECT id, raw_json->>'city_name' FROM hotels WHERE trip_hotel_id=%s",
                (hotel_id,),
            )
            found = cur.fetchone()
            if not found:
                stats["missing_hotels"] += 1
                continue
            db_hotel_id, stored_city_name = found
            location_id = upsert_city(cur, detail.get("city_name") or stored_city_name)
            if location_id:
                location_ids.add(location_id)

            normalized_for_raw = {k: v for k, v in detail.items() if k != "rooms"}
            normalized_for_raw["rooms"] = [
                {k: v for k, v in room.items() if k != "raw"}
                for room in (detail.get("rooms") or [])
            ]
            cur.execute(
                """
                UPDATE hotels SET
                    location_id = COALESCE(%s, location_id),
                    description = COALESCE(%s, description),
                    hotel_type = COALESCE(%s, hotel_type),
                    raw_json = COALESCE(raw_json, '{}'::jsonb)
                               || jsonb_build_object('detail', %s::jsonb),
                    last_seen_at = now()
                WHERE id = %s
                """,
                (location_id, detail.get("description"), detail.get("hotel_type"),
                 Json(normalized_for_raw), db_hotel_id),
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
                        category=COALESCE(EXCLUDED.category, hotel_images.category),
                        sort_order=EXCLUDED.sort_order
                """, image_rows)
                stats["images"] += len(image_rows)

            amenity_rows = [
                (db_hotel_id, item.get("code"), item.get("name"), item.get("category"))
                for item in (detail.get("amenities") or []) if item.get("name")
            ]
            if amenity_rows:
                execute_values(cur, """
                    INSERT INTO hotel_amenities
                        (hotel_id, amenity_code, amenity_name, category) VALUES %s
                    ON CONFLICT (hotel_id, amenity_name) DO UPDATE SET
                        amenity_code=COALESCE(EXCLUDED.amenity_code, hotel_amenities.amenity_code),
                        category=COALESCE(EXCLUDED.category, hotel_amenities.category)
                """, amenity_rows)
                stats["amenities"] += len(amenity_rows)

            for room in detail.get("rooms") or []:
                if not room.get("trip_room_id") or not room.get("name"):
                    continue
                room_raw = dict(room.get("raw")) if isinstance(room.get("raw"), dict) else dict(room)
                room_raw["images"] = room.get("images") or []
                cur.execute("""
                    INSERT INTO room_types
                        (hotel_id, trip_room_id, name, bed_type, max_occupancy, area_sqm, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (hotel_id, trip_room_id) DO UPDATE SET
                        name=EXCLUDED.name,
                        bed_type=COALESCE(EXCLUDED.bed_type, room_types.bed_type),
                        max_occupancy=COALESCE(EXCLUDED.max_occupancy, room_types.max_occupancy),
                        area_sqm=COALESCE(EXCLUDED.area_sqm, room_types.area_sqm),
                        raw_json=EXCLUDED.raw_json
                    RETURNING id
                """, (
                    db_hotel_id, room["trip_room_id"], room["name"], room.get("bed_type"),
                    room.get("max_occupancy"), room.get("area_sqm"), Json(room_raw),
                ))
                room_db_id = cur.fetchone()[0]
                stats["rooms"] += 1

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
                          AND (captured_at AT TIME ZONE %s)::date=%s
                        ORDER BY id LIMIT 1
                        """,
                        (db_hotel_id, room_db_id, check_in, check_out,
                         config.TIMEZONE, captured.date()),
                    )
                    existing_price = cur.fetchone()
                    if existing_price:
                        cur.execute(
                            """
                            UPDATE hotel_prices SET price=%s, currency=%s,
                                tax_included=%s, captured_at=%s
                            WHERE id=%s
                            """,
                            (room.get("price"), room.get("currency") or "VND",
                             room.get("tax_included"), captured, existing_price[0]),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO hotel_prices
                                (hotel_id, room_type_id, check_in, check_out, price,
                                 currency, tax_included, captured_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (db_hotel_id, room_db_id, check_in, check_out,
                             room.get("price"), room.get("currency") or "VND",
                             room.get("tax_included"), captured),
                        )
                    stats["prices"] += 1

        stats["locations"] = len(location_ids)
        stats["replaced_hotels"] = replaced_hotels

        cur.execute(
            "UPDATE crawl_runs SET status='success', finished_at=now(), stats=%s WHERE id=%s",
            (Json({"file": path.name, **stats}), run_id),
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
    ap.add_argument("--dry-run", action="store_true", help="kiểm tra SQL rồi rollback, không ghi DB")
    ap.add_argument(
        "--replace-existing", action="store_true",
        help="xóa detail cũ của các hotel trong file rồi import lại trong 1 transaction",
    )
    main(ap.parse_args())
