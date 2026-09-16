"""Bước 4 — nạp JSON đã crawl vào PostgreSQL.

Nhận cả 2 định dạng: output của crawl_list.py (CSS, ít field hơn) và
output của crawl_api.py (JSON thật từ Trip.com, đầy đủ sao/điểm/toạ độ).
Upsert theo trip_hotel_id nên chạy lại bao nhiêu lần cũng không nhân đôi dữ liệu.

    python src/db/loader.py                      # nạp file mới nhất trong output/data/
    python src/db/loader.py api_hotels_301_xxx.json
    python src/db/loader.py --cheap              # đánh dấu is_cheap_listing = true
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg2
from psycopg2.extras import Json, execute_values

import config
from db.location_upsert import upsert_city

UPSERT = """
INSERT INTO hotels (
    trip_hotel_id, name, name_en, url, location_id, address, latitude, longitude,
    star_rating, review_score, review_count,
    price_from, currency, is_cheap_listing, source_url, raw_json, last_seen_at
)
VALUES %s
ON CONFLICT (trip_hotel_id) DO UPDATE SET
    name             = COALESCE(EXCLUDED.name, hotels.name),
    name_en          = COALESCE(EXCLUDED.name_en, hotels.name_en),
    url              = COALESCE(EXCLUDED.url, hotels.url),
    location_id      = COALESCE(EXCLUDED.location_id, hotels.location_id),
    address          = COALESCE(EXCLUDED.address, hotels.address),
    latitude         = COALESCE(EXCLUDED.latitude, hotels.latitude),
    longitude        = COALESCE(EXCLUDED.longitude, hotels.longitude),
    star_rating      = COALESCE(EXCLUDED.star_rating, hotels.star_rating),
    review_score     = COALESCE(EXCLUDED.review_score, hotels.review_score),
    review_count     = COALESCE(EXCLUDED.review_count, hotels.review_count),
    price_from       = COALESCE(EXCLUDED.price_from, hotels.price_from),
    -- cờ cheap chỉ bật thêm, không tự tắt khi crawl trang khác
    is_cheap_listing = hotels.is_cheap_listing OR EXCLUDED.is_cheap_listing,
    source_url       = EXCLUDED.source_url,
    -- Import overview again without discarding detail already loaded.
    raw_json         = COALESCE(hotels.raw_json, '{}'::jsonb) || EXCLUDED.raw_json,
    last_seen_at     = now();
"""


def as_score(raw) -> float | None:
    """Chấp nhận cả số sẵn (từ crawl_api.py) lẫn text kiểu '8,7' (từ crawl_list.py)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if 0 <= raw <= 10 else None
    try:
        v = float(str(raw).replace(",", ".").split("/")[0].strip())
    except ValueError:
        return None
    return v if 0 <= v <= 10 else None


def latest_file() -> Path:
    files = sorted(
        list(config.DATA_DIR.glob("hotels_*.json")) + list(config.DATA_DIR.glob("api_hotels_*.json")),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        raise SystemExit(
            "Chưa có file nào trong output/data/. Chạy crawl_api.py hoặc crawl_list.py trước."
        )
    return files[-1]


def main(args: argparse.Namespace) -> None:
    path = Path(args.file) if args.file else latest_file()
    if not path.is_absolute() and not path.exists():
        path = config.DATA_DIR / path.name
    payload = json.loads(path.read_text(encoding="utf-8"))
    hotels = payload.get("hotels", [])
    source = payload.get("source_url") or payload.get("city_name") or path.name
    city_name = payload.get("city_name")
    city_id = payload.get("city_id")
    locale = args.locale or payload.get("locale") or "vi-VN"
    currency = (args.currency or payload.get("currency") or "VND").upper()
    is_cheap = args.cheap or "cheap" in source.lower()

    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.hotel_translations')")
        if cur.fetchone()[0] is None:
            raise SystemExit("Chưa có bảng translation. Hãy chạy migrations/002_multilingual.sql trước.")
        location_id = upsert_city(cur, city_name, city_id)
        if location_id and city_name:
            cur.execute(
                """
                INSERT INTO location_translations (location_id, locale, name)
                VALUES (%s,%s,%s)
                ON CONFLICT (location_id, locale) DO UPDATE SET
                    name=EXCLUDED.name, updated_at=now()
                """,
                (location_id, locale, city_name),
            )
        rows, skipped = [], 0
        for h in hotels:
            hid = h.get("trip_hotel_id")
            if not hid:
                skipped += 1
                continue
            rows.append(
                (
                    hid,
                    h.get("name") if locale == "vi-VN" else None,
                    h.get("name") if locale.startswith("en") else h.get("name_en"),
                    h.get("url"),
                    location_id or upsert_city(cur, h.get("city_name")),
                    h.get("address") if locale == "vi-VN" else None,
                    h.get("latitude"),
                    h.get("longitude"),
                    h.get("star_rating"),
                    as_score(h.get("review_score") if h.get("review_score") is not None else h.get("score")),
                    h.get("review_count"),
                    h.get("price_value"),
                    h.get("currency") or currency,
                    is_cheap,
                    source,
                    Json(h if locale == "vi-VN" else {}),
                )
            )

        if not rows:
            raise SystemExit(f"Không có bản ghi nào có trip_hotel_id trong {path.name}.")

        cur.execute(
            "INSERT INTO crawl_runs (target, status, locale, currency) "
            "VALUES (%s, 'running', %s, %s) RETURNING id",
            (source, locale, currency),
        )
        run_id = cur.fetchone()[0]
        execute_values(
            cur, UPSERT, rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())",
        )
        translation_rows = []
        for h in hotels:
            if not h.get("trip_hotel_id"):
                continue
            translation_rows.append((
                str(h["trip_hotel_id"]), locale, h.get("name"), h.get("address"),
                h.get("url"), Json(h),
            ))
        if translation_rows:
            execute_values(
                cur,
                """
                INSERT INTO hotel_translations
                    (hotel_id, locale, name, address, source_url, raw_json, crawled_at)
                SELECT h.id, v.locale, v.name, v.address, v.source_url,
                       v.raw_json::jsonb, now()
                FROM (VALUES %s) AS v(trip_hotel_id, locale, name, address, source_url, raw_json)
                JOIN hotels h ON h.trip_hotel_id=v.trip_hotel_id
                ON CONFLICT (hotel_id, locale) DO UPDATE SET
                    name=COALESCE(EXCLUDED.name, hotel_translations.name),
                    address=COALESCE(EXCLUDED.address, hotel_translations.address),
                    source_url=COALESCE(EXCLUDED.source_url, hotel_translations.source_url),
                    raw_json=EXCLUDED.raw_json,
                    crawled_at=EXCLUDED.crawled_at,
                    updated_at=now()
                """,
                translation_rows,
                template="(%s,%s,%s,%s,%s,%s)",
            )
        cur.execute(
            "UPDATE crawl_runs SET status='success', finished_at=now(), stats=%s WHERE id=%s",
            (Json({"file": path.name, "upserted": len(rows), "skipped": skipped,
                   "location_id": location_id}), run_id),
        )
        cur.execute("SELECT count(*) FROM hotels")
        total = cur.fetchone()[0]
        if args.dry_run:
            conn.rollback()

    prefix = "DRY RUN" if args.dry_run else "Upsert"
    print(f"{prefix} {len(rows)} bản ghi từ {path.name} (bỏ qua {skipped} thiếu id)")
    print(f"Tổng số khách sạn trong DB: {total}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="file JSON trong output/data/")
    ap.add_argument("--cheap", action="store_true", help="đánh dấu is_cheap_listing")
    ap.add_argument("--locale", help="ghi đè locale trong JSON, ví dụ en-US")
    ap.add_argument("--currency", help="ghi đè currency trong JSON, ví dụ USD")
    ap.add_argument("--dry-run", action="store_true", help="kiểm tra SQL rồi rollback")
    main(ap.parse_args())
