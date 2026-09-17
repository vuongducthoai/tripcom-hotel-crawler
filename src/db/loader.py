"""Bước 4 — nạp JSON đã crawl vào PostgreSQL.

Nhận cả 2 định dạng: output của crawl_list.py (CSS, ít field hơn) và
output của crawl_api.py (JSON thật từ Trip.com, đầy đủ sao/điểm/toạ độ).
Upsert theo trip_hotel_id nên chạy lại bao nhiêu lần cũng không nhân đôi dữ liệu.

    python src/db/loader.py                      # nạp file mới nhất trong output/data/
    python src/db/loader.py api_hotels_301_xxx.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg2
from psycopg2.extras import Json, execute_values

import config
from db.location_upsert import upsert_city
from db.i18n import language_key

UPSERT = """
INSERT INTO hotels (
    trip_hotel_id, url, location_id, latitude, longitude,
    star_rating, review_score, review_count, raw_json, last_seen_at
)
VALUES %s
ON CONFLICT (trip_hotel_id) DO UPDATE SET
    url              = COALESCE(EXCLUDED.url, hotels.url),
    location_id      = COALESCE(EXCLUDED.location_id, hotels.location_id),
    latitude         = COALESCE(EXCLUDED.latitude, hotels.latitude),
    longitude        = COALESCE(EXCLUDED.longitude, hotels.longitude),
    star_rating      = COALESCE(EXCLUDED.star_rating, hotels.star_rating),
    review_score     = COALESCE(EXCLUDED.review_score, hotels.review_score),
    review_count     = COALESCE(EXCLUDED.review_count, hotels.review_count),
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
    source = payload.get("source") or path.name
    city_name = payload.get("city_name")
    city_id = payload.get("city_id")
    locale = args.locale or payload.get("locale") or "vi-VN"
    language = language_key(locale)
    currency = (args.currency or payload.get("currency") or "VND").upper()
    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.hotel_translations')")
        if cur.fetchone()[0] is None:
            raise SystemExit("Chưa có bảng translation. Hãy chạy migrations/002_multilingual.sql trước.")
        location_id = upsert_city(cur, city_name, city_id, language)
        if location_id and city_name:
            cur.execute(
                """
                INSERT INTO location_translations (location_id, locale, name)
                VALUES (%s,%s,%s)
                ON CONFLICT (location_id, locale) DO UPDATE SET
                    name=EXCLUDED.name, updated_at=now()
                """,
                (location_id, language, city_name),
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
                    h.get("url"),
                    location_id or upsert_city(cur, h.get("city_name"), language=language),
                    h.get("latitude"),
                    h.get("longitude"),
                    h.get("star_rating"),
                    as_score(h.get("review_score") if h.get("review_score") is not None else h.get("score")),
                    h.get("review_count"),
                    Json(h if language == "vi" else {}),
                )
            )

        if not rows:
            raise SystemExit(f"Không có bản ghi nào có trip_hotel_id trong {path.name}.")

        cur.execute(
            "INSERT INTO crawl_runs (target, status, locale, currency) "
            "VALUES (%s, 'running', %s, %s) RETURNING id",
            (source, language, currency),
        )
        run_id = cur.fetchone()[0]
        execute_values(
            cur, UPSERT, rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,now())",
        )
        try:
            captured = datetime.fromisoformat(str(payload.get("crawled_at") or ""))
        except ValueError:
            captured = datetime.now()
        try:
            check_in = date.fromisoformat(str(payload.get("check_in")))
        except ValueError:
            check_in = captured.date() + timedelta(days=1)
        try:
            check_out = date.fromisoformat(str(payload.get("check_out")))
        except ValueError:
            check_out = check_in + timedelta(days=1)
        overview_prices = [
            (
                str(h["trip_hotel_id"]), check_in, check_out,
                h.get("price_value"), h.get("currency") or currency,
                language, captured, captured.date(),
            )
            for h in hotels
            if h.get("trip_hotel_id") and h.get("price_value") is not None
        ]
        if overview_prices:
            execute_values(
                cur,
                """
                INSERT INTO hotel_prices (
                    hotel_id, room_type_id, check_in, check_out, price, currency,
                    language, price_type, captured_at, captured_date
                )
                SELECT h.id, NULL, v.check_in, v.check_out, v.price, v.currency,
                       v.language, 'overview', v.captured_at, v.captured_date
                FROM (VALUES %s) AS v(
                    trip_hotel_id, check_in, check_out, price, currency,
                    language, captured_at, captured_date
                )
                JOIN hotels h ON h.trip_hotel_id=v.trip_hotel_id
                ON CONFLICT (
                    hotel_id, check_in, check_out, currency, language, captured_date
                ) WHERE price_type='overview' AND room_type_id IS NULL
                DO UPDATE SET
                    price=EXCLUDED.price,
                    captured_at=EXCLUDED.captured_at
                """,
                overview_prices,
                template="(%s,%s,%s,%s,%s,%s,%s,%s)",
            )
        translation_rows = []
        for h in hotels:
            if not h.get("trip_hotel_id"):
                continue
            translation_rows.append((
                str(h["trip_hotel_id"]), language, h.get("name"), h.get("address"),
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
        if language == "vi":
            english_seed_rows = [
                (str(h["trip_hotel_id"]), h.get("name_en"))
                for h in hotels
                if h.get("trip_hotel_id") and h.get("name_en")
            ]
            if english_seed_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO hotel_translations (hotel_id, locale, name)
                    SELECT h.id, 'en', v.name
                    FROM (VALUES %s) AS v(trip_hotel_id, name)
                    JOIN hotels h ON h.trip_hotel_id=v.trip_hotel_id
                    ON CONFLICT (hotel_id, locale) DO UPDATE SET
                        name=COALESCE(hotel_translations.name, EXCLUDED.name),
                        updated_at=now()
                    """,
                    english_seed_rows,
                    template="(%s,%s)",
                )
        cur.execute(
            "UPDATE crawl_runs SET status='success', finished_at=now(), stats=%s WHERE id=%s",
            (Json({"file": path.name, "upserted": len(rows), "skipped": skipped,
                   "overview_prices": len(overview_prices),
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
    ap.add_argument("--locale", help="ghi đè locale trong JSON, ví dụ en-US")
    ap.add_argument("--currency", help="ghi đè currency trong JSON, ví dụ USD")
    ap.add_argument("--dry-run", action="store_true", help="kiểm tra SQL rồi rollback")
    main(ap.parse_args())
