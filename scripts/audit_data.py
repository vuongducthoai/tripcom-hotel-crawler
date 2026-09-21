"""Kiểm tra toàn diện dữ liệu trong PostgreSQL — chạy trên máy có kết nối DB.

    python scripts/audit_data.py

In ra màn hình + ghi báo cáo JSON vào output/data/audit_report_<timestamp>.json
để gửi lại cho Claude đọc/đánh giá (Claude không kết nối được DB từ sandbox).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import psycopg2
import psycopg2.extras
import config


def main() -> None:
    report: dict = {"generated_at": datetime.now().isoformat()}

    with psycopg2.connect(config.dsn()) as conn:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        def q(sql, params=None):
            cur.execute(sql, params or ())
            return cur.fetchall()

        # ---------------------------------------------------------- 1. Row counts
        tables = [
            "locations", "hotels", "hotel_translations", "location_translations",
            "hotel_images", "hotel_image_categories",
            "hotel_amenities", "hotel_amenity_translations",
            "room_types", "room_type_translations",
            "room_images", "room_amenities", "room_amenity_translations",
            "hotel_policies", "hotel_policy_translations",
            "hotel_nearby_places", "hotel_nearby_place_translations",
            "hotel_prices", "crawl_runs", "crawl_errors",
        ]
        counts = {}
        for t in tables:
            try:
                counts[t] = q(f"SELECT count(*) AS n FROM {t}")[0]["n"]
            except Exception as exc:
                counts[t] = f"ERROR: {exc}"
                conn.rollback()
                conn.autocommit = True
        report["row_counts"] = counts

        # ---------------------------------------------------- 2. price/currency bug scan
        try:
            bad_price = q("""
                SELECT h.trip_hotel_id, p.price, p.currency,
                       p.language, p.price_type
                FROM hotel_prices p
                JOIN hotels h ON h.id=p.hotel_id
                WHERE p.currency='VND' AND p.price IS NOT NULL AND p.price<5000
                ORDER BY p.price
            """)
            report["suspicious_vnd_price_too_low"] = {
                "count": len(bad_price),
                "sample": bad_price[:20],
            }
        except Exception as exc:
            report["suspicious_vnd_price_too_low"] = f"ERROR: {exc}"

        # ------------------------------------------------- 3. overview vs detail
        try:
            overview_total = q("SELECT count(*) AS n FROM hotels")[0]["n"]
            with_rooms = q("SELECT count(DISTINCT hotel_id) AS n FROM room_types")[0]["n"]
            with_images = q("SELECT count(DISTINCT hotel_id) AS n FROM hotel_images")[0]["n"]
            with_amenities = q("SELECT count(DISTINCT hotel_id) AS n FROM hotel_amenities")[0]["n"]
            with_location = q("SELECT count(*) AS n FROM hotels WHERE location_id IS NOT NULL")[0]["n"]
            report["coverage"] = {
                "hotels_total": overview_total,
                "hotels_with_rooms": with_rooms,
                "hotels_with_images": with_images,
                "hotels_with_amenities": with_amenities,
                "hotels_with_location_id": with_location,
            }
        except Exception as exc:
            report["coverage"] = f"ERROR: {exc}"

        # -------------------------------------------- 4. locale completeness
        def locale_complete(table, id_col):
            try:
                rows = q(f"""
                    SELECT {id_col}, array_agg(DISTINCT locale) AS locales
                    FROM {table}
                    GROUP BY {id_col}
                """)
                total = len(rows)
                both = sum(1 for r in rows if set(r["locales"]) >= {"vi", "en"})
                return {"entities": total, "with_both_locales": both,
                        "pct_bilingual": round(100 * both / total, 1) if total else None}
            except Exception as exc:
                return f"ERROR: {exc}"

        report["locale_completeness"] = {
            "hotel_translations": locale_complete("hotel_translations", "hotel_id"),
            "location_translations": locale_complete("location_translations", "location_id"),
            "room_type_translations": locale_complete("room_type_translations", "room_type_id"),
            "hotel_amenity_translations": locale_complete("hotel_amenity_translations", "hotel_amenity_id"),
            "room_amenity_translations": locale_complete("room_amenity_translations", "room_amenity_id"),
        }

        # ---------------------------------------------------- 5. hotel_prices sanity
        try:
            price_stats = q("""
                SELECT price_type, language, currency, count(*) AS n, min(price) AS min_price,
                       max(price) AS max_price, avg(price)::numeric(14,2) AS avg_price
                FROM hotel_prices
                GROUP BY price_type, language, currency
                ORDER BY price_type, language, currency
            """)
            null_or_zero = q("SELECT count(*) AS n FROM hotel_prices WHERE price IS NULL OR price <= 0")[0]["n"]
            report["hotel_prices_sanity"] = {"by_currency": price_stats, "null_or_zero_price": null_or_zero}
        except Exception as exc:
            report["hotel_prices_sanity"] = f"ERROR: {exc}"

        # ---------------------------------------------------- 6. duplicate / null checks
        try:
            dup_hotel = q("""
                SELECT trip_hotel_id, count(*) AS n FROM hotels
                GROUP BY trip_hotel_id HAVING count(*) > 1
            """)
            null_name = q("""
                SELECT count(*) AS n
                FROM hotels h
                WHERE NOT EXISTS (
                    SELECT 1 FROM hotel_translations t
                    WHERE t.hotel_id=h.id AND t.locale='vi'
                      AND t.name IS NOT NULL AND btrim(t.name)<>''
                )
            """)[0]["n"]
            report["integrity"] = {
                "duplicate_trip_hotel_id": len(dup_hotel),
                "hotels_with_null_name": null_name,
            }
        except Exception as exc:
            report["integrity"] = f"ERROR: {exc}"

        # -------------------------------- 6b. phần dễ bị bỏ sót: chính sách, lân cận,
        #                                       cờ giá rẻ, phân bố theo thành phố
        def scalar(sql):
            try:
                return q(sql)[0]["n"]
            except Exception as exc:
                conn.rollback()
                conn.autocommit = True
                return f"ERROR: {exc}"

        report["coverage_extra"] = {
            "hotels_with_policies": scalar(
                "SELECT count(DISTINCT hotel_id) AS n FROM hotel_policies"),
            "hotels_with_nearby_places": scalar(
                "SELECT count(DISTINCT hotel_id) AS n FROM hotel_nearby_places"),
            "hotels_with_prices": scalar(
                "SELECT count(DISTINCT hotel_id) AS n FROM hotel_prices"),
            # Trang "Khách sạn giá rẻ" — 1 trong 2 trang được giao ban đầu.
            "hotels_flagged_cheap": scalar(
                "SELECT count(*) AS n FROM hotels WHERE is_cheap_listing"),
            # Chi tiết tiếng Anh thật sự (không tính tên seed từ name_en).
            "hotels_with_english_rooms": scalar("""
                SELECT count(DISTINCT rt.hotel_id) AS n
                FROM room_type_translations t
                JOIN room_types rt ON rt.id = t.room_type_id
                WHERE left(t.locale, 2) = 'en'"""),
            "hotels_with_english_amenities": scalar("""
                SELECT count(DISTINCT a.hotel_id) AS n
                FROM hotel_amenity_translations t
                JOIN hotel_amenities a ON a.id = t.hotel_amenity_id
                WHERE left(t.locale, 2) = 'en'"""),
        }

        try:
            report["hotels_per_location"] = q("""
                SELECT l.trip_location_id, l.name, count(h.id) AS hotels
                FROM locations l LEFT JOIN hotels h ON h.location_id = l.id
                GROUP BY l.trip_location_id, l.name ORDER BY hotels DESC
            """)
        except Exception as exc:
            report["hotels_per_location"] = f"ERROR: {exc}"

        # Giá trị locale thật trong DB: 'vi'/'en' hay 'vi-VN'/'en-US' — lẫn lộn 2 kiểu
        # sẽ làm mọi thống kê song ngữ sai, nên liệt kê thẳng ra.
        locale_values = {}
        for table in ("hotel_translations", "location_translations", "room_type_translations",
                      "hotel_amenity_translations", "room_amenity_translations",
                      "hotel_policy_translations", "hotel_nearby_place_translations",
                      "hotel_image_categories", "hotel_prices"):
            try:
                locale_values[table] = q(
                    f"SELECT locale, count(*) AS n FROM {table} GROUP BY locale ORDER BY locale")
            except Exception as exc:
                conn.rollback()
                conn.autocommit = True
                locale_values[table] = f"ERROR: {exc}"
        report["locale_values"] = locale_values

        # ---------------------------------------------------- 7. crawl_runs / errors
        try:
            recent_runs = q("""
                SELECT id, target, status, started_at, finished_at
                FROM crawl_runs ORDER BY started_at DESC LIMIT 10
            """)
            error_count = q("SELECT count(*) AS n FROM crawl_errors")[0]["n"]
            report["crawl_runs_recent"] = recent_runs
            report["crawl_errors_total"] = error_count
        except Exception as exc:
            report["crawl_runs_recent"] = f"ERROR: {exc}"

    out_path = config.DATA_DIR / f"audit_report_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\nĐã ghi báo cáo: {out_path}")


if __name__ == "__main__":
    main()
