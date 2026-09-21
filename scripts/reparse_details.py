"""Tái phân tích output/details/raw/*.json bằng detail_extract.py hiện tại,
KHÔNG crawl lại mạng. Dùng khi sửa logic extract sau khi đã crawl xong.

    python scripts/reparse_details.py

Ghi ra output/data/hotel_details_reparsed_<timestamp>.json, đúng format mà
db/detail_loader.py đang đọc (source_overview/crawled_at/complete/count/
success_count/details[]).
"""
from __future__ import annotations

import json
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from detail_extract import extract_detail  # noqa: E402
import config  # noqa: E402
import raw_store  # noqa: E402


def main(args: argparse.Namespace) -> None:
    locale = args.locale
    currency = args.currency.upper()
    raw_root = config.OUTPUT_DIR / "details" / "raw"
    if args.raw_dir:
        raw_dir = Path(args.raw_dir)
        if not raw_dir.is_absolute():
            raw_dir = config.ROOT / raw_dir
    else:
        raw_dir = raw_root / locale / currency
        if not raw_dir.exists() and locale == "vi-VN" and currency == "VND":
            raw_dir = raw_root
    raw_files = list(raw_store.iter_raw_files(raw_dir))
    if not raw_files:
        raise SystemExit(f"Không tìm thấy raw capture nào trong {raw_dir}")

    details: list[dict] = []
    ok = 0
    changed_rooms = 0

    for index, path in enumerate(raw_files, 1):
        try:
            dump = raw_store.read(path)
        except Exception as exc:
            print(f"  BỎ QUA {path.name}: không đọc được ({exc})")
            continue

        target = dump.get("target") or {}
        base = dict(dump.get("normalized") or {})
        hotel_id = str(
            target.get("trip_hotel_id")
            or base.get("trip_hotel_id")
            or raw_store.hotel_id(path)
        )
        url = target.get("url") or base.get("url") or ""
        responses = dump.get("responses") or []

        old_room_count = len(base.get("rooms") or [])
        try:
            fresh = extract_detail(responses, hotel_id, url, currency, locale)
        except Exception as exc:
            print(f"  LỖI extract {path.name}: {type(exc).__name__}: {exc}")
            base.setdefault("trip_hotel_id", hotel_id)
            base.setdefault("url", url)
            base["success"] = False
            base["error"] = f"reparse_failed: {exc}"
            details.append(base)
            continue

        base["trip_hotel_id"] = hotel_id
        base["url"] = url
        base["locale"] = locale
        base["currency"] = currency
        base["name"] = fresh.get("name") or base.get("name") or target.get("name")
        base["address"] = fresh.get("address") or base.get("address") or target.get("address")
        # Do not resurrect SEO/booking text accepted by an older parser.
        # A missing verified description is intentionally stored as NULL.
        base["description"] = fresh["description"]
        # Do not retain values produced by an older parser. In particular,
        # parser v4 and earlier could mistake image/promotion categoryName for
        # the property's hotel_type.
        base["hotel_type"] = fresh["hotel_type"]
        base["images"] = fresh["images"]
        base["amenities"] = fresh["amenities"]
        base["policies"] = fresh["policies"]
        base["nearby_places"] = fresh["nearby_places"]
        # Full room source payloads remain in the raw capture. Do not retain a
        # second copy in memory/manifest; on a city-sized reparse this can use
        # several GB of RAM without adding normalized data.
        base["rooms"] = [
            {key: value for key, value in room.items() if key != "raw"}
            for room in fresh["rooms"]
        ]
        base["response_count"] = fresh["response_count"]
        base["parser_version"] = fresh["parser_version"]
        base.setdefault("success", True)
        base.setdefault("error", None)
        base.setdefault("check_in", None)
        base.setdefault("check_out", None)

        if len(fresh["rooms"]) != old_room_count:
            changed_rooms += 1
        ok += 1
        details.append(base)
        if not args.no_update_cache:
            dump["normalized"] = base
            raw_store.write(path, dump)
        if index == 1 or index % 50 == 0 or index == len(raw_files):
            print(
                f"  Tiến độ: {index}/{len(raw_files)} raw "
                f"({index * 100 // len(raw_files)}%)",
                flush=True,
            )

    out = {
        "source_overview": "reparse_from_raw_cache",
        "locale": locale,
        "currency": currency,
        "crawled_at": datetime.now().isoformat(),
        "complete": True,
        "count": len(details),
        "success_count": ok,
        "details": details,
    }

    market_tag = f"{locale}_{currency}".replace("-", "")
    out_path = config.DATA_DIR / (
        f"hotel_details_reparsed_{market_tag}_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    print(f"Đã tái phân tích {len(raw_files)} file raw -> {ok} thành công.")
    print(f"Số khách sạn có số phòng thay đổi so với lần chạy trước: {changed_rooms}")
    print(f"Ghi ra: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-update-cache", action="store_true",
        help="chỉ tạo manifest, không cập nhật normalized trong raw cache",
    )
    parser.add_argument("--locale", default="vi-VN", help="vi-VN hoặc en-US")
    parser.add_argument("--currency", default="VND", help="VND hoặc USD")
    parser.add_argument(
        "--raw-dir",
        help="thư mục raw cần reparse, ví dụ output/details/raw cho cache legacy",
    )
    main(parser.parse_args())
