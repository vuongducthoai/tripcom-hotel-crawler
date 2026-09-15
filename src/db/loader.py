"""Bước 4 — nạp JSON đã crawl vào PostgreSQL.

Upsert theo trip_hotel_id nên chạy lại bao nhiêu lần cũng không nhân đôi dữ liệu.

    python src/db/loader.py                      # nạp file mới nhất trong output/data/
    python src/db/loader.py hotels_xxx.json      # nạp một file cụ thể
    python src/db/loader.py --cheap              # đánh dấu is_cheap_listing = true
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import Json, execute_values

import config

UPSERT = """
INSERT INTO hotels (
    trip_hotel_id, name, url, address, review_score,
    price_from, currency, is_cheap_listing, source_url, raw_json, last_seen_at
)
VALUES %s
ON CONFLICT (trip_hotel_id) DO UPDATE SET
    name             = COALESCE(EXCLUDED.name, hotels.name),
    url              = COALESCE(EXCLUDED.url, hotels.url),
    address          = COALESCE(EXCLUDED.address, hotels.address),
    review_score     = COALESCE(EXCLUDED.review_score, hotels.review_score),
    price_from       = COALESCE(EXCLUDED.price_from, hotels.price_from),
    -- cờ cheap chỉ bật thêm, không tự tắt khi crawl trang khác
    is_cheap_listing = hotels.is_cheap_listing OR EXCLUDED.is_cheap_listing,
    source_url       = EXCLUDED.source_url,
    raw_json         = EXCLUDED.raw_json,
    last_seen_at     = now();
"""


def as_score(raw) -> float | None:
    if raw is None:
        return None
    try:
        v = float(str(raw).replace(",", ".").split("/")[0].strip())
    except ValueError:
        return None
    return v if 0 <= v <= 10 else None


def latest_file() -> Path:
    files = sorted(config.DATA_DIR.glob("hotels_*.json"))
    if not files:
        raise SystemExit("Chưa có file nào trong output/data/. Chạy crawl_list.py trước.")
    return files[-1]


def main(args: argparse.Namespace) -> None:
    path = Path(args.file) if args.file else latest_file()
    if not path.is_absolute() and not path.exists():
        path = config.DATA_DIR / path.name
    payload = json.loads(path.read_text(encoding="utf-8"))
    hotels = payload.get("hotels", [])
    source = payload.get("source_url", "")
    is_cheap = args.cheap or "cheap" in source.lower()

    rows, skipped = [], 0
    for h in hotels:
        hid = h.get("trip_hotel_id")
        if not hid:
            skipped += 1
            continue
        rows.append(
            (
                hid,
                h.get("name"),
                h.get("url"),
                h.get("address"),
                as_score(h.get("score")),
                h.get("price_value"),
                "VND",
                is_cheap,
                source,
                Json(h),
            )
        )

    if not rows:
        raise SystemExit(f"Không có bản ghi nào có trip_hotel_id trong {path.name}.")

    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crawl_runs (target, status) VALUES (%s, 'running') RETURNING id",
            (source,),
        )
        run_id = cur.fetchone()[0]
        execute_values(cur, UPSERT, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())")
        cur.execute(
            "UPDATE crawl_runs SET status='success', finished_at=now(), stats=%s WHERE id=%s",
            (Json({"file": path.name, "upserted": len(rows), "skipped": skipped}), run_id),
        )
        cur.execute("SELECT count(*) FROM hotels")
        total = cur.fetchone()[0]

    print(f"Upsert {len(rows)} bản ghi từ {path.name} (bỏ qua {skipped} thiếu id)")
    print(f"Tổng số khách sạn trong DB: {total}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="file JSON trong output/data/")
    ap.add_argument("--cheap", action="store_true", help="đánh dấu is_cheap_listing")
    main(ap.parse_args())
